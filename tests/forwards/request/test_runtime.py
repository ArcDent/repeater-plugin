"""查看资格同步与续轮提醒的运行时回归测试（原插件 test_runtime.py 的可迁移部分）。

原文件中的三条用例断言插件入口的组件声明（合成包加载、Tool 的
``chat_scope``/``visibility``/``timeout_ms``、两个 Hook 的先后顺序）。这些
属于 `plugin.py` 的 Hook/Tool 接线（change task 4.4/4.5）而非请求转发链路，
因此不随本组迁移；这里保留经运行时即可验证的资格同步与提醒语义。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

from repeater_plugin.forwards.request.source.authorization import (
    FORWARD_TOOL_NAME,
)
from repeater_plugin.forwards.request.source.models import ViewEligibilityStatus

from tests.forwards.request.support import build_plugin


def test_hook_syncs_view_eligibility_without_rewriting_tool_definitions(tmp_path: Path) -> None:
    """验证 Hook 同步上下文资格、保持工具定义并可靠维持续轮提醒。

    即使启动阶段聊天流列表为空，当前会话仍应登记 ``forward-message`` 的
    完整展开内容，且工具定义保持原样。Planner 正常响应前重复请求应继续
    收到判断提醒，响应后旧历史不再触发提醒；另一会话的资格必须隔离，
    当前上下文移除查看结果后资格必须消失。该测试防止新消息打断导致判断
    任务丢失、旧结果反复提醒或上下文外结果继续生效。

    Args:
        tmp_path: pytest 提供的临时数据目录，用于运行时加载和卸载状态隔离。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "view_forward_message",
                            "arguments": {"msg_id": "forward-message"},
                        },
                    }
                ],
            },
            {"role": "tool", "content": "完整展开内容", "tool_call_id": "call-1"},
        ]
        definitions = [
            {"type": "function", "function": {"name": FORWARD_TOOL_NAME}},
            {"type": "function", "function": {"name": "reply"}},
        ]

        plugin.ctx.chat.streams.clear()
        current_messages = deepcopy(messages)
        current_result = await plugin.capture_view_forward_result(
            session_id="current-stream",
            messages=current_messages,
            tool_definitions=definitions,
        )
        current_lookup = plugin.runtime.view_eligibility.lookup(
            "current-stream",
            "forward-message",
        )
        assert current_lookup.status is ViewEligibilityStatus.READY
        assert current_lookup.content == "完整展开内容"
        assert current_result["modified_kwargs"]["tool_definitions"] == definitions
        assert len(current_messages) == len(messages) + 1
        assert current_messages[-1]["role"] == "user"
        assert isinstance(current_messages[-1]["content"], str)
        assert current_messages[-1]["content"]

        interrupted_retry_messages = deepcopy(messages)
        await plugin.capture_view_forward_result(
            session_id="current-stream",
            messages=interrupted_retry_messages,
            tool_definitions=definitions,
        )
        assert len(interrupted_retry_messages) == len(messages) + 1
        assert interrupted_retry_messages[-1]["role"] == "user"
        assert isinstance(interrupted_retry_messages[-1]["content"], str)
        assert interrupted_retry_messages[-1]["content"]

        await plugin.orchestrate_view_before_forward(
            session_id="current-stream",
            response="我会现在判断",
            tool_calls=[],
        )
        acknowledged_messages = deepcopy(messages)
        await plugin.capture_view_forward_result(
            session_id="current-stream",
            messages=acknowledged_messages,
            tool_definitions=definitions,
        )
        assert acknowledged_messages == messages

        other_messages = deepcopy(messages)
        other_result = await plugin.capture_view_forward_result(
            session_id="target-a",
            messages=other_messages,
            tool_definitions=definitions,
        )
        assert (
            plugin.runtime.view_eligibility.lookup(
                "target-a",
                "forward-message",
            ).status
            is ViewEligibilityStatus.READY
        )
        assert other_result["modified_kwargs"]["tool_definitions"] == definitions

        await plugin.capture_view_forward_result(
            session_id="current-stream",
            messages=[],
            tool_definitions=definitions,
        )
        assert (
            plugin.runtime.view_eligibility.lookup(
                "current-stream",
                "forward-message",
            ).status
            is ViewEligibilityStatus.MISSING
        )
        await plugin.on_unload()

    asyncio.run(scenario())
