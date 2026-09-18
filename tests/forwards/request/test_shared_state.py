"""请求路径接入共用状态的回归测试（change task 3.4）。

覆盖两条 spec 需求：

- 「已投递状态的两路共用」：**自动转发**登记后**请求转发**拒绝重复，
  未登记（沉默/未命中）时请求仍可正常执行；
- 「请求转发链路」的目标群可用性：不可用目标群返回含群号与原因的失败结果，
  不再出现「未知错误」，且不熔断其他目标群。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from repeater_plugin.forwards.request.core.state import ForwardStateStore
from repeater_plugin.forwards.request.source.authorization import (
    FORWARD_TOOL_NAME,
)

from tests.forwards.request.source.support import (
    build_forward_call,
    run_after_response_hooks,
)
from tests.forwards.request.support import (
    build_plugin,
    invoke_forward_tool,
    mark_delivered,
    seed_successful_view,
    set_group_list,
    wait_for_background_tasks,
)


def test_request_rejects_message_already_delivered_by_auto_forward(tmp_path: Path) -> None:
    """验证自动转发登记后请求路径拒绝重复投递。

    已投递状态覆盖本次全部候选目标群时，请求应返回 ``accepted=False`` 与
    已转发过的说明，且不读取源消息、不产生任何发送。该测试防止两条路径对
    同一消息重复投递。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        seed_successful_view(plugin)
        mark_delivered(plugin, ["20001", "20002"])

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )

        assert result["success"] is True
        assert result["accepted"] is False
        assert "已转发过" in result["content"]
        assert "20001" in result["content"]
        assert "20002" in result["content"]
        assert plugin.ctx.message.calls == []
        assert plugin.ctx.events == []
        await plugin.on_unload()

    asyncio.run(scenario())


def test_request_delivers_only_targets_not_yet_delivered(tmp_path: Path) -> None:
    """验证部分目标群已投递时只投递剩余目标群。

    ``20001`` 已由自动转发投递、``20002`` 未投递时，请求应被接受，只向
    ``20002`` 发送并触发其 Planner，结果文本说明被跳过的目标群。该测试防止
    已投递状态把整条请求一并拒绝，或对已投递群重复发送。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        seed_successful_view(plugin)
        mark_delivered(plugin, ["20001"])

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
        assert result["accepted"] is True
        assert result["target_count"] == 1
        await wait_for_background_tasks(plugin)

        assert plugin.ctx.events == [
            ("send", "target-b"),
            ("planner", "target-b"),
        ]
        assert "20001" in result["content"]
        await plugin.on_unload()

    asyncio.run(scenario())


def test_silent_auto_forward_leaves_request_path_available(tmp_path: Path) -> None:
    """验证自动转发未登记（沉默或未命中）时请求转发仍可执行。

    已投递状态为空时请求应正常投递全部候选目标群。该测试固定「沉默出口不
    登记、请求路径不受影响」的共用状态语义。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        seed_successful_view(plugin)

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
        assert result["accepted"] is True
        assert result["success"] is True
        await wait_for_background_tasks(plugin)
        assert plugin.ctx.events == [
            ("send", "target-a"),
            ("planner", "target-a"),
            ("send", "target-b"),
            ("planner", "target-b"),
        ]
        await plugin.on_unload()

    asyncio.run(scenario())


def test_auto_view_preflight_skips_message_already_delivered(tmp_path: Path) -> None:
    """验证已投递消息不会触发路径 B 的系统查看。

    已投递状态覆盖全部候选目标群时，未查看的直接请求应保持原转发调用交给
    正式处理器返回「已转发过」，而不是先消耗一次完整查看。该测试防止共用
    状态只在正式 handler 生效、让重复请求多走一轮查看。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        mark_delivered(plugin, ["20001", "20002"])

        result = await run_after_response_hooks(
            plugin,
            session_id="source-stream",
            response="重复请求",
            tool_calls=[build_forward_call()],
        )
        retained = result["modified_kwargs"]["tool_calls"][0]
        assert retained["function"]["name"] == FORWARD_TOOL_NAME
        assert plugin.ctx.message.calls == []
        assert plugin.ctx.events == []
        await plugin.on_unload()

    asyncio.run(scenario())


def test_unavailable_target_group_reports_explicit_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证不在机器人群列表的目标群被跳过并返回明确原因。

    ``20001`` 不在缓存的群列表、``20002`` 可用时，结果应给出群号与原因、
    不出现「未知错误」，只向 ``20002`` 发送并触发其 Planner，同时记录含
    群号的 warning。该测试防止投递再次落入陈旧会话的静默失败（ADR 0010）。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
        caplog: pytest 日志捕获器，用于确认跳过的目标群以警告可见。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        caplog.set_level(logging.WARNING, logger="test.forward-plugin")
        set_group_list(plugin, ["10001", "20002"])
        await plugin.on_load()
        seed_successful_view(plugin)

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
        assert result["accepted"] is True
        assert result["success"] is False
        assert result["sent_target_count"] == 1
        assert "20001" in result["content"]
        assert "不在机器人所在群列表" in result["content"]
        assert "未知错误" not in result["content"]
        await wait_for_background_tasks(plugin)

        assert plugin.ctx.events == [
            ("send", "target-b"),
            ("planner", "target-b"),
        ]
        assert any(
            record.levelno == logging.WARNING
            and "目标群不在机器人群列表" in record.getMessage()
            and "20001" in record.getMessage()
            for record in caplog.records
        )
        await plugin.on_unload()

    asyncio.run(scenario())


def test_unknown_group_list_allows_delivery(tmp_path: Path) -> None:
    """验证没有可信群列表时可用性校验放行投递。

    缓存收到空列表表示这一轮没拿到可信数据，校验必须放行并正常投递全部
    目标群；该测试防止把「未知」当成「不可用」而制造新的静默失败源。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        set_group_list(plugin, [])
        await plugin.on_load()
        seed_successful_view(plugin)

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
        assert result["success"] is True
        await wait_for_background_tasks(plugin)
        assert plugin.ctx.events == [
            ("send", "target-a"),
            ("planner", "target-a"),
            ("send", "target-b"),
            ("planner", "target-b"),
        ]
        await plugin.on_unload()

    asyncio.run(scenario())


def test_already_sent_target_is_not_failed_by_availability_check(tmp_path: Path) -> None:
    """验证已发送完成的目标群不会被可用性校验改判为失败。

    持久化状态里 ``20001`` 已是 ``sent``、群列表缓存又不含 ``20001`` 时，该
    目标群应继续完成 Planner 阶段而不重新发送；只有尚未发送的目标群才受
    可用性校验约束。该测试防止续投流程把已完成的目标群重新标记为失败或
    重复投递。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        state_key = ForwardStateStore.build_state_key("source-stream", "forward-message")
        (tmp_path / "forward_state.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "updated_at": 0,
                    "jobs": {
                        state_key: {
                            "job_id": state_key.rsplit(":", 1)[-1],
                            "source_stream_id": "source-stream",
                            "source_group_id": "10001",
                            "source_message_id": "forward-message",
                            "target_group_ids": ["20001", "20002"],
                            "created_at": 0,
                            "updated_at": 0,
                            "targets": {"20001": "sent"},
                            "target_message_ids": {"20001": "persisted-target-a"},
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plugin = build_plugin(tmp_path)
        set_group_list(plugin, ["10001", "20002"])
        await plugin.on_load()
        seed_successful_view(plugin)

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )

        assert result["success"] is True
        assert result["sent_target_count"] == 2
        await wait_for_background_tasks(plugin)
        assert plugin.ctx.events == [
            ("planner", "target-a"),
            ("send", "target-b"),
            ("planner", "target-b"),
        ]
        assert plugin.ctx.maisaka.proactive.metadata_by_stream["target-a"] == {
            "job_id": result["job_id"],
            "target_message_id": "persisted-target-a",
        }
        await plugin.on_unload()

    asyncio.run(scenario())
