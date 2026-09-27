"""群聊流双向解析的回归测试。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from repeater_plugin.forwards.request.core.streams import GroupStreamRegistry

from tests.forwards.request.support import FakeChatCapability


def test_group_stream_registry_prefers_observed_session() -> None:
    """hook 上报过的活跃 session 优先于能力首匹配（同群多 session 的陈旧陷阱）。

    回归：`get_stream_by_group_id` 在主机侧是「列表首匹配」，同群存在新旧两个
    session 时会返回陈旧会话；请求转发链路若把目标群解析到死会话，投递与触发
    都会落在没人读的会话里。
    """

    async def scenario() -> None:
        # 预设列表为空：若解析落回能力路径，`open_session` 会造出 `opened-20001`
        chat = FakeChatCapability([])
        context = SimpleNamespace(chat=chat, logger=logging.getLogger("test.forward-plugin"))
        observed = {"20001": "live-target-a"}
        registry = GroupStreamRegistry(
            context, observed_session=lambda group_id: observed.get(group_id, "")
        )

        assert await registry.resolve("20001") == "live-target-a"
        assert [stream for stream in chat.streams if stream["stream_id"].startswith("opened-")] == []
        # 反查（stream → 群号）也认这条观察，不必再拉全量群聊流
        assert await registry.resolve_group("live-target-a") == "20001"

    asyncio.run(scenario())


def test_group_stream_registry_resolves_targets_on_demand() -> None:
    """验证聊天流注册表只在实际投递时按需解析目标群。

    已有 target 应通过 ``get_stream_by_group_id`` 查询，未知 target 应通过
    ``open_session`` 创建；可信 source 会话应通过群聊流列表反查真实群号。
    该测试防止重新引入启动期预加载依赖、信任消息元数据中的来源群，或把
    SDK 解包后的聊天流结构误判为失败。
    """

    async def scenario() -> None:
        context = SimpleNamespace(
            chat=FakeChatCapability(
                [
                    {"platform": "qq", "group_id": "10001", "stream_id": "source-stream"},
                    {"platform": "qq", "group_id": "20001", "stream_id": "target-a"},
                ]
            ),
            logger=logging.getLogger("test.forward-plugin"),
        )
        registry = GroupStreamRegistry(context)
        assert await registry.resolve_group("source-stream") == "10001"
        assert await registry.resolve("20001") == "target-a"
        assert await registry.resolve("30001") == "opened-30001"

    asyncio.run(scenario())
