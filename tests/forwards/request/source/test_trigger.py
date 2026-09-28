"""source 合并转发消息强制触发 Planner 的回归测试。

原文件中断言 ``chat.receive.after_process`` Hook 组件声明的用例属于
`plugin.py` 接线（change task 4.4），不随本组迁移。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from repeater_plugin.forwards.request.core.models import ForwardJob
from repeater_plugin.forwards.request.source.message import SourceForwardMessageParser

from tests.forwards.request.support import (
    FakeDelayedMessageCapability,
    FakeMessageCapability,
    build_forward_message,
    build_plugin,
    wait_for_background_tasks,
)


def test_source_trigger_waits_for_real_message_and_only_queues_planner(tmp_path: Path) -> None:
    """验证开关启用后会等待真实消息就绪，并且只触发 source Planner。

    Hook 首次查询模拟消息尚未落库，第二次才返回同一聊天流的合并转发。
    期望最终只出现 source Planner 事件，不发生物理发送；metadata 保留同流
    ``msg_id``。该测试防止主动任务抢在真实消息入库前运行，或把强制触发
    扩张为自动转发；Planner 可见文本不属于本测试范围。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存永久触发防重键。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        plugin.ctx.message = FakeDelayedMessageCapability(build_forward_message())

        await plugin.observe_source_message(message=build_forward_message())
        await wait_for_background_tasks(plugin)

        assert plugin.ctx.message.calls == [
            ("forward-message", "source-stream", False),
            ("forward-message", "source-stream", False),
        ]
        assert plugin.ctx.events == [("planner", "source-stream")]
        assert plugin.ctx.send.messages_by_stream == {}
        assert plugin.ctx.maisaka.proactive.metadata_by_stream["source-stream"] == {
            "trigger_kind": "source_forward_message",
            "source_message_id": "forward-message",
        }
        await plugin.on_unload()

    asyncio.run(scenario())


def test_source_trigger_always_fires_for_qualifying_source_message(tmp_path: Path) -> None:
    """验证入口提示固化开启：没有任何触发开关时，合格源消息照常触发本群 Planner。

    源群白名单命中且消息可查询到后必须出现一次 planner 事件；该测试防止
    删掉源群触发开关判定时把整条入口提示一起删掉。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存永久触发防重键。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        plugin.ctx.message = FakeDelayedMessageCapability(build_forward_message())

        await plugin.observe_source_message(message=build_forward_message())
        await wait_for_background_tasks(plugin)

        assert plugin.ctx.message.calls == [
            ("forward-message", "source-stream", False),
            ("forward-message", "source-stream", False),
        ]
        assert plugin.ctx.events == [("planner", "source-stream")]
        await plugin.on_unload()

    asyncio.run(scenario())


def test_source_trigger_requires_source_whitelist(tmp_path: Path) -> None:
    """验证固化开启后仍只认 source 白名单：非 source 群不查询、不触发。

    该测试防止入口提示固化开启被误读为「任意群都触发」。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存永久触发防重键。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path / "non-source")
        await plugin.on_load()
        await plugin.observe_source_message(
            message=build_forward_message(group_id="30001", stream_id="other-stream")
        )
        await wait_for_background_tasks(plugin)
        assert plugin.ctx.message.calls == []
        assert plugin.ctx.events == []
        await plugin.on_unload()

    asyncio.run(scenario())


def test_source_trigger_does_not_fire_after_unload(tmp_path: Path) -> None:
    """验证卸载后入口提示停摆：`_is_unloading` 仍拦住新的触发安排。

    删掉源群触发开关判定后，卸载守卫是仅剩的停止条件；卸载后
    再收到合格源消息不得查询消息或触发 Planner。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存永久触发防重键。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        await plugin.on_unload()

        await plugin.observe_source_message(message=build_forward_message())
        await wait_for_background_tasks(plugin)

        assert plugin.ctx.message.calls == []
        assert plugin.ctx.events == []

    asyncio.run(scenario())


def test_message_ready_polling_aborts_during_unload(tmp_path: Path) -> None:
    """验证等待窗口轮询守卫只删配置检查、保留卸载中止（task 1.7 的 seam）。

    轮询期间卸载（`stop()` 置 `_is_unloading`）后再进入就绪判定，必须立即
    返回 ``False`` 且不查询消息——「消息不可见就放弃并记录 warning」的结果
    不变。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存永久触发防重键。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        observation = SourceForwardMessageParser.parse(build_forward_message())
        assert observation is not None
        plugin.ctx.message = FakeMessageCapability({})

        await plugin.runtime.source_trigger.stop()

        assert await plugin.runtime.source_trigger._wait_until_message_ready(observation) is False
        assert plugin.ctx.message.calls == []

    asyncio.run(scenario())


def test_source_trigger_skips_self_and_known_forward_echoes(tmp_path: Path) -> None:
    """验证账号来源标记和目标消息 ID 都能阻止机器人转发回声触发。

    第一条消息的发送者与适配器 ``self_id`` 相同，应在解析阶段忽略；第二条
    消息模拟插件此前发送到同一个 source/target 重叠群的真实目标消息 ID，
    即使来源标记缺失也应由转发状态拦截。该测试防止机器人自己的跨群转发
    回声重新唤醒 source Planner 并形成循环。

    Args:
        tmp_path: pytest 提供的隔离状态目录，用于保存转发和触发状态。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()

        await plugin.observe_source_message(
            message=build_forward_message(sender_id="99999", self_id="99999")
        )
        echo_job = ForwardJob(
            job_id="echo-job",
            state_key=plugin.runtime.state.build_state_key("origin-stream", "origin-message"),
            source_stream_id="origin-stream",
            source_group_id="40001",
            source_message_id="origin-message",
            target_group_ids=["10001"],
            forward_messages=[{"segments": [{"type": "text", "data": "回声"}]}],
            sharing_reason="测试回声",
        )
        await plugin.runtime.state.record_target_sent(echo_job, "10001", "forward-message")
        await plugin.observe_source_message(message=build_forward_message(self_id=""))
        await wait_for_background_tasks(plugin)

        assert plugin.ctx.message.calls == []
        assert plugin.ctx.events == []
        await plugin.on_unload()

    asyncio.run(scenario())


def test_source_trigger_is_permanently_deduplicated_across_reload(tmp_path: Path) -> None:
    """验证同一 source stream 与 msg_id 成功触发后跨重载仍只执行一次。

    首个实例连续观察两次同一消息时只应安排一个任务，成功后状态文件保存
    一个匿名稳定键；第二个实例加载同一数据目录后再次观察仍不得查询消息或
    触发 Planner。该测试防止 Hook 重复投递或插件重载打破永久防重。

    Args:
        tmp_path: pytest 提供且在两个插件实例之间复用的持久化目录。
    """

    async def scenario() -> None:
        first_plugin = build_plugin(tmp_path)
        await first_plugin.on_load()
        message = build_forward_message()
        await first_plugin.observe_source_message(message=message)
        await first_plugin.observe_source_message(message=message)
        await wait_for_background_tasks(first_plugin)
        assert first_plugin.ctx.events == [("planner", "source-stream")]
        await first_plugin.on_unload()

        state_payload = json.loads((tmp_path / "source_planner_trigger_state.json").read_text(encoding="utf-8"))
        assert state_payload["version"] == 1
        assert len(state_payload["triggered_keys"]) == 1
        assert "source-stream" not in state_payload["triggered_keys"][0]
        assert "forward-message" not in state_payload["triggered_keys"][0]

        reloaded_plugin = build_plugin(tmp_path)
        await reloaded_plugin.on_load()
        await reloaded_plugin.observe_source_message(message=message)
        await wait_for_background_tasks(reloaded_plugin)
        assert reloaded_plugin.ctx.message.calls == []
        assert reloaded_plugin.ctx.events == []
        await reloaded_plugin.on_unload()

    asyncio.run(scenario())
