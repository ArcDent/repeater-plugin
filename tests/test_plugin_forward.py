"""plugin.py 转发域测试：编排层 + 端到端集成 + manifest 能力面。

覆盖自动路径 Hook（门禁 / 防重 / 冷却 / 逐目标群掷骰）、投递编舞（延迟 →
可用性校验 → 协议端直发 → 内容注入 → 强制发言）、源群留痕、请求链路委托与
生命周期接线。纯逻辑（掷骰、状态、节点、文案）的单测在 tests/test_forwards_*.py。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from sending_emojis_plugin.forwards.request import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
    build_delivered_key,
)
from sending_emojis_plugin.forwards.request.source.history import ViewToolObservation
from sending_emojis_plugin.forwards.request.source.models import ViewObservationKind
from sending_emojis_plugin.forwards.source import ForwardSource, parse_forward_source
from sending_emojis_plugin.forwards.state import ForwardState
from sending_emojis_plugin.forwards.texts import (
    build_forced_comment_intent,
    build_forward_content_text,
    build_forward_trace_text,
)

from config import RepeaterConfig
from plugin import _FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS, RepeaterPlugin
from tests.fake_maibot_sdk import FakePluginContext

BOT_QQ = "40004"
SOURCE_GROUP = "10001"
TARGET_A = "20001"
TARGET_B = "20002"
SOURCE_STREAM = "stream-10001"

_DEFAULT_PROTOCOL_RESPONSE: dict[str, Any] = {"status": "ok", "retcode": 0, "data": {}}


def make_forward_plugin(
    tmp_path: Path,
    *,
    protocol_responses: dict[str, Any] | None = None,
) -> RepeaterPlugin:
    """构造启用转发域的插件实例：协议端调用查表返回，ctx 为完整 capability 替身。

    掷骰默认定死：转发权重拉满（1000）、沉默归零，任何门禁失效都会直接产生
    投递任务而不是靠默认权重蒙对（AGENTS.md「测试注意」）；需要沉默的用例
    自行反向覆盖权重。延迟与冷却默认归零，相关用例单独开启。
    """
    plugin = RepeaterPlugin()
    plugin.config = RepeaterConfig()
    plugin.ctx = FakePluginContext(tmp_path, events=[])
    plugin.config.protocol.base_url = "http://protocol.test:3000"
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.plugin.enabled = True
    forward = plugin.config.forward
    forward.enabled = True
    forward.source_groups = [SOURCE_GROUP]
    forward.target_groups = [TARGET_A, TARGET_B]
    forward.weight_send = 1000
    forward.weight_silence = 0
    forward.delay_min_seconds = 0.0
    forward.delay_max_seconds = 0.0
    forward.cooldown_seconds = 0
    forward.target_cooldown_seconds = 0
    # 源群 Planner 触发默认关闭：真实运行时会为每条合格源消息起一个等待消息
    # 落库的后台任务，自动路径用例不需要它；委托断言用替身运行时单独覆盖。
    forward.trigger_source_planner = False

    plugin._runner = None
    plugin._site = None
    plugin._tasks = set()
    plugin._forward_tasks = set()
    plugin._flush_loop_task = None
    plugin._self_qq = BOT_QQ
    plugin._emoji_action_supported = True
    plugin._rebuild_runtime()

    plugin.ctx.chat.group_streams = [
        {
            "platform": "qq",
            "group_id": SOURCE_GROUP,
            "stream_id": SOURCE_STREAM,
            "session_id": SOURCE_STREAM,
            "is_group_session": True,
        },
        {
            "platform": "qq",
            "group_id": TARGET_A,
            "stream_id": "stream-20001",
            "session_id": "stream-20001",
            "is_group_session": True,
        },
        {
            "platform": "qq",
            "group_id": TARGET_B,
            "stream_id": "stream-20002",
            "session_id": "stream-20002",
            "is_group_session": True,
        },
    ]
    plugin.ctx.config.values["bot.nickname"] = "麦麦"

    responses = {
        # 群列表默认返回三个已知群：投递路径的可用性刷新（task 5.2）据此建立可信
        # 快照、目标群可用。需要「不在列表 / 查询失败」的用例自行覆盖这一项。
        "get_group_list": {
            "status": "ok",
            "retcode": 0,
            "data": [
                {"group_id": int(SOURCE_GROUP)},
                {"group_id": int(TARGET_A)},
                {"group_id": int(TARGET_B)},
            ],
        },
        **dict(protocol_responses or {}),
    }
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(action_name: str, params: dict[str, Any]) -> Any:
        calls.append((action_name, params))
        plugin.ctx.events.append(action_name)
        response = responses.get(action_name, _DEFAULT_PROTOCOL_RESPONSE)
        return response(params) if callable(response) else response

    plugin._call_protocol = fake_call  # type: ignore[assignment]
    plugin.protocol_calls = calls  # type: ignore[attr-defined]
    return plugin


def forward_node(text: str = "有趣的消息", *, nickname: str = "群友甲") -> dict[str, Any]:
    """一个 `forward` 段内的原始节点（与主程序 hook 侧字段同形）。"""
    return {
        "user_id": "30003",
        "user_nickname": nickname,
        "user_cardname": "",
        "message_id": "80001",
        "content": [{"type": "text", "data": text}],
    }


def forward_message(
    *,
    group: str = SOURCE_GROUP,
    sender: str = "30003",
    message_id: str = "90001",
    nodes: Any = None,
    group_name: str = "源群",
) -> dict[str, Any]:
    """hook 载荷形态的合并转发序列化 SessionMessage。"""
    payload = [forward_node()] if nodes is None else nodes
    return {
        "message_id": message_id,
        "platform": "qq",
        "session_id": f"session-{group}",
        "is_notify": False,
        "message_info": {
            "group_info": {"group_id": group, "group_name": group_name},
            "user_info": {"user_id": sender, "user_nickname": "群友甲", "user_cardname": ""},
        },
        "raw_message": [{"type": "forward", "data": payload}],
    }


def parse_source(message: dict[str, Any] | None = None) -> ForwardSource:
    """按自动路径同步段的解析入口取源消息快照（投递用例直接 await 的入参）。"""
    source = parse_forward_source(message if message is not None else forward_message(), BOT_QQ)
    assert source is not None
    return source


def coro_name(task: asyncio.Task) -> str:
    return str(getattr(task.get_coro(), "__qualname__", ""))


def pending_delivery_tasks(plugin: RepeaterPlugin) -> list[asyncio.Task]:
    """handler 同步段 spawn 出、尚未执行的按目标群投递任务。"""
    return [
        task
        for task in plugin._tasks
        if not task.done() and "_deliver_forward_to_target" in coro_name(task)
    ]


def pending_finalizers(plugin: RepeaterPlugin) -> list[asyncio.Task]:
    return [
        task
        for task in plugin._tasks
        if not task.done() and "_finalize_forward_delivery" in coro_name(task)
    ]


async def drain_forward_tasks(plugin: RepeaterPlugin) -> None:
    """等齐转发域后台任务（延迟与协议端调用都在任务内完成）。"""
    pending = [task for task in list(plugin._tasks) if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def send_group_forward_calls(plugin: RepeaterPlugin) -> list[dict[str, Any]]:
    return [params for name, params in plugin.protocol_calls if name == "send_group_forward_msg"]  # type: ignore[attr-defined]


def injections(plugin: RepeaterPlugin, source_kind: str) -> list[dict[str, Any]]:
    return [
        call
        for call in plugin.ctx.maisaka.context.calls
        if call.get("source_kind") == source_kind
    ]


class FakeForwardRuntime:
    """转发域运行时替身：只记录 plugin.py 的委托调用，不复制任何业务判断。

    `plugin.py` 对请求转发链路的职责是「声明 Hook/Tool 并把载荷原样交给运行
    时」，接线断言用替身比装配真实运行时更能定位回归（运行时自身行为由
    tests/forwards/request/** 覆盖）。
    """

    def __init__(self) -> None:
        self.observed: list[Any] = []
        self.captured: list[tuple[str, Any]] = []
        self.reminder = ""
        self.authorized_calls: list[tuple[str, Any]] = []
        self.authorized_output_items: list[tuple[str, Any]] = []
        self.authorize_result: tuple[Any, str] = ([], "round-1")
        self.transformed: list[tuple[str, Any, Any, str]] = []
        self.transformed_output_items: list[tuple[str, Any, str]] = []
        self.transform_result: tuple[str, Any] = ("", [])
        self.transform_items_result: Any = []
        self.requests: list[dict[str, Any]] = []
        self.request_result: dict[str, Any] = {"success": True, "content": "已投递"}

    def observe_source_message(self, message: Any) -> bool:
        self.observed.append(message)
        return True

    def capture_view_results(self, session_id: str, history_items: Any) -> list[str]:
        self.captured.append((session_id, history_items))
        return []

    def build_view_judgment_reminder(self, session_id: str) -> str:
        return self.reminder

    def authorize_forward_calls(self, session_id: str, tool_calls: Any) -> tuple[Any, str]:
        self.authorized_calls.append((session_id, tool_calls))
        return self.authorize_result

    def authorize_forward_output_items(
        self, session_id: str, output_items: Any
    ) -> tuple[Any, str]:
        self.authorized_output_items.append((session_id, output_items))
        return self.authorize_result

    async def transform_after_response(
        self, session_id: str, response: Any, tool_calls: Any, authorization_round: str
    ) -> tuple[str, Any]:
        self.transformed.append((session_id, response, tool_calls, authorization_round))
        return self.transform_result

    async def transform_after_output_items(
        self, session_id: str, output_items: Any, authorization_round: str
    ) -> Any:
        self.transformed_output_items.append((session_id, output_items, authorization_round))
        return self.transform_items_result

    async def request_forward(
        self,
        msg_id: str,
        sharing_reason: str,
        content_summary: str,
        invocation_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.requests.append(
            {
                "msg_id": msg_id,
                "sharing_reason": sharing_reason,
                "content_summary": content_summary,
                "context": invocation_context,
            }
        )
        return self.request_result


# ---- 4.1 自动路径 Hook：门禁、防重、冷却与掷骰 ----


def test_forward_hit_spawns_delivery_tasks_and_writes_cooldowns(tmp_path: Path):
    """命中转发：每个目标群一个投递任务 + 留痕收尾任务，冷却与防重同步写入。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.cooldown_seconds = 60
    plugin.config.forward.target_cooldown_seconds = 60

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        # handler 必须在同步段完成编排：任务尚未执行，但已经建立
        assert len(pending_delivery_tasks(plugin)) == 2
        assert len(pending_finalizers(plugin)) == 1
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    now = time.time()
    assert plugin._forward_state.is_duplicate("90001", 1800, now) is True
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is True
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is True
    assert plugin._forward_state.is_target_cooling(TARGET_B, 60, now) is True
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
        int(TARGET_A),
        int(TARGET_B),
    ]


def test_forward_non_source_group_has_no_side_effects(tmp_path: Path):
    """非白名单源群：不掷骰、不起任务、不写冷却。"""
    plugin = make_forward_plugin(tmp_path)

    asyncio.run(plugin.handle_forward_message(message=forward_message(group="99999")))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, time.time()) is False


def test_forward_own_message_has_no_side_effects(tmp_path: Path):
    """麦麦自己发出的合并转发不参与转发域判定。"""
    plugin = make_forward_plugin(tmp_path)

    asyncio.run(plugin.handle_forward_message(message=forward_message(sender=BOT_QQ)))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, time.time()) is False


def test_forward_domain_disabled_has_no_side_effects(tmp_path: Path):
    """forward.enabled=false：转发域整域停用，命中权重拉满也不产生任何任务。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.enabled = False
    plugin._rebuild_runtime()

    asyncio.run(plugin.handle_forward_message(message=forward_message()))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    now = time.time()
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is False


def test_forward_plugin_disabled_has_no_side_effects(tmp_path: Path):
    """plugin.enabled=false：总开关关闭时转发域一并停用（spec 转发域开关）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.plugin.enabled = False
    plugin._rebuild_runtime()

    asyncio.run(plugin.handle_forward_message(message=forward_message()))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    now = time.time()
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is False


def test_forward_message_without_valid_nodes_has_no_side_effects(tmp_path: Path):
    """转发段存在但节点全无内容（适配器展开失败）：不参与判定。"""
    plugin = make_forward_plugin(tmp_path)

    asyncio.run(plugin.handle_forward_message(message=forward_message(nodes=[{"content": []}])))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]


def test_forward_silence_outlet_writes_no_cooldown(tmp_path: Path):
    """沉默出口：掷骰发生但不写任何冷却（权重反向拉满，不用全零走不掷骰分支）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.weight_send = 0
    plugin.config.forward.weight_silence = 1000
    plugin.config.forward.cooldown_seconds = 60

    asyncio.run(plugin.handle_forward_message(message=forward_message()))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    now = time.time()
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is False


def test_forward_duplicate_message_rolls_once(tmp_path: Path):
    """同一转发源消息在防重窗口内只参与一次掷骰。"""
    plugin = make_forward_plugin(tmp_path)
    message = forward_message()

    async def scenario() -> None:
        await plugin.handle_forward_message(message=message)
        await plugin.handle_forward_message(message=message)
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert len(send_group_forward_calls(plugin)) == 2  # 只有第一次的两个目标群
    assert len(injections(plugin, "forward_action")) == 1


def test_forward_source_cooldown_suppresses_new_message(tmp_path: Path):
    """源群冷却期内新到的合并转发不触发掷骰；冷却过后恢复。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.cooldown_seconds = 600

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message(message_id="90001"))
        await drain_forward_tasks(plugin)
        await plugin.handle_forward_message(message=forward_message(message_id="90002"))
        assert plugin._tasks == set()

    asyncio.run(scenario())

    assert len(send_group_forward_calls(plugin)) == 2


def test_forward_target_cooldown_only_suppresses_that_target(tmp_path: Path):
    """目标群冷却只压制该群：另一目标群照常命中投递。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.target_cooldown_seconds = 600
    plugin._forward_state.mark_targets([TARGET_A], time.time())

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [int(TARGET_B)]


def test_forward_handler_swallows_unexpected_exception(tmp_path: Path, monkeypatch):
    """observe handler 不参与 abort 链：异常记 warning 后放行，绝不外抛。"""
    plugin = make_forward_plugin(tmp_path)

    def boom(message: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_process_forward_message", boom)

    result = asyncio.run(plugin.handle_forward_message(message=forward_message()))

    assert result is None  # observe 模式返回 None：不改写 kwargs、不请求 abort
    assert any("自动转发" in text for text in plugin.ctx.logger.texts("warning"))


# ---- 4.2 投递编舞 ----


def test_deliver_forward_sends_then_injects_then_triggers(tmp_path: Path):
    """投递成功：可用性刷新 → 直发 → 内容注入 → 强制发言，顺序与文案都由纯逻辑层构造。

    缓存尚未建立（工厂不预置启动自检结果）时，直发前先补一次群列表查询
    （task 5.2）；此后 TTL 内不再重复查询。
    """
    plugin = make_forward_plugin(tmp_path)
    source = parse_source()

    result = asyncio.run(plugin._deliver_forward_to_target(source, TARGET_A))

    assert result is True
    assert plugin.ctx.events == [
        "get_group_list",
        "send_group_forward_msg",
        "context.append:forward",
        "proactive.trigger",
    ]
    sent = send_group_forward_calls(plugin)[0]
    assert sent["group_id"] == int(TARGET_A)
    assert sent["messages"] == [
        {
            "type": "node",
            "data": {
                "user_id": "30003",
                "nickname": "群友甲",
                "content": [{"type": "text", "data": {"text": "有趣的消息"}}],
            },
        }
    ]
    content = injections(plugin, "forward")
    assert content[0]["stream_id"] == "stream-20001"
    assert content[0]["message_id"] == source.message_id
    # 期望文案按字面来源字段重建（而非复用插件手里那份 source）：文案里的来源群
    # 与发送者必须真的来自 hook 载荷，字段错配在插件层就能被抓住。
    expected_source = ForwardSource(
        group_id=SOURCE_GROUP,
        group_name="源群",
        message_id="90001",
        nodes=source.nodes,
        sender_nickname="群友甲",
    )
    assert content[0]["visible_text"] == build_forward_content_text(expected_source)
    trigger = plugin.ctx.maisaka.proactive.calls[0]
    assert trigger["stream_id"] == "stream-20001"
    assert trigger["intent"] == build_forced_comment_intent(expected_source)


def test_deliver_forward_registers_delivered_state(tmp_path: Path):
    """投递成功后登记**已投递状态**：请求转发据此拒绝重复（design D5）。"""
    plugin = make_forward_plugin(tmp_path)

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    key = build_delivered_key(SOURCE_STREAM, "90001")
    assert plugin._forward_delivered.is_delivered(key, TARGET_A, time.time()) is True
    assert plugin._forward_delivered.is_delivered(key, TARGET_B, time.time()) is False


def test_deliver_forward_send_failure_skips_injection_and_trigger(tmp_path: Path):
    """直发失败：不注入、不触发，记 warning 且不重试。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": {"status": "failed", "retcode": 1200, "wording": "禁言中"}
        },
    )

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is False
    assert plugin.ctx.maisaka.context.calls == []
    assert plugin.ctx.maisaka.proactive.calls == []
    assert len(send_group_forward_calls(plugin)) == 1  # 不重试
    assert any(TARGET_A in text for text in plugin.ctx.logger.texts("warning"))


def test_deliver_forward_unreachable_protocol_skips_injection_and_trigger(tmp_path: Path):
    """协议端不可达（返回 None）：同样止步，不产生注入与触发。"""
    plugin = make_forward_plugin(tmp_path, protocol_responses={"send_group_forward_msg": None})

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is False
    assert plugin.ctx.maisaka.context.calls == []
    assert plugin.ctx.maisaka.proactive.calls == []
    assert any(TARGET_A in text for text in plugin.ctx.logger.texts("warning"))


def test_deliver_forward_unavailable_target_skipped(tmp_path: Path):
    """目标群不在机器人群列表：跳过投递，warning 带群号与原因。"""
    plugin = make_forward_plugin(tmp_path)
    plugin._forward_availability.update(["30001"], time.time())

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is False
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    assert any(TARGET_A in text for text in plugin.ctx.logger.texts("warning"))


def test_deliver_forward_without_base_url_warns_and_skips_protocol(tmp_path: Path):
    """协议端未配置：明确放弃投递（不静默），且不调用协议端。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.protocol.base_url = ""

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is False
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    assert any("自动转发" in text and TARGET_A in text for text in plugin.ctx.logger.texts("warning"))


def test_deliver_forward_waits_forward_delay_range(tmp_path: Path, monkeypatch):
    """投递前按 forward.delay_min/max 延迟（不复用表情回应域的 behavior 延迟）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.delay_min_seconds = 1.5
    plugin.config.forward.delay_max_seconds = 1.5
    plugin.config.behavior.delay_min_seconds = 9.0
    plugin.config.behavior.delay_max_seconds = 9.0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    assert asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A)) is True
    assert sleeps == [1.5]


def test_deliver_forward_target_trigger_can_be_disabled(tmp_path: Path):
    """trigger_target_planner=false：直发与注入照常，不触发目标群 Planner。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.trigger_target_planner = False

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    assert [call["source_kind"] for call in plugin.ctx.maisaka.context.calls] == ["forward"]
    assert plugin.ctx.maisaka.proactive.calls == []


def test_deliver_forward_trigger_failure_keeps_delivery_result(tmp_path: Path):
    """强制发言触发失败（软约束）：只记 warning，投递与注入结果不受影响。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.ctx.maisaka.proactive.fail_next = True

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    assert len(injections(plugin, "forward")) == 1
    assert any("强制发言" in text for text in plugin.ctx.logger.texts("warning"))


def test_deliver_forward_missing_target_stream_skips_trigger(tmp_path: Path):
    """目标群查不到 stream：直发与注入不依赖 stream，强制发言跳过并记 warning。"""
    plugin = make_forward_plugin(tmp_path)
    plugin._stream_ids.clear()
    plugin.ctx.chat.group_streams = [
        stream for stream in plugin.ctx.chat.group_streams if stream["group_id"] != TARGET_A
    ]

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    assert plugin.ctx.maisaka.proactive.calls == []
    assert any("强制发言" in text for text in plugin.ctx.logger.texts("warning"))


# ---- 4.3 源群留痕注入 ----


def test_forward_trace_lists_only_delivered_targets(tmp_path: Path):
    """一条成功一条失败：源群留痕只列成功群，文案由纯逻辑层构造。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": lambda params: (
                {"status": "failed", "retcode": 1200}
                if str(params["group_id"]) == TARGET_B
                else {"status": "ok", "retcode": 0, "data": {}}
            )
        },
    )

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    traces = injections(plugin, "forward_action")
    assert len(traces) == 1
    assert traces[0]["stream_id"] == SOURCE_STREAM
    assert traces[0]["message_id"] == "90001"
    assert traces[0]["visible_text"] == build_forward_trace_text([TARGET_A])
    assert TARGET_B not in traces[0]["visible_text"]


def test_forward_trace_skipped_when_all_targets_fail(tmp_path: Path):
    """全部目标群投递失败：源群不产生留痕注入。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": {"status": "failed", "retcode": 1200, "wording": "禁言中"}
        },
    )

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert injections(plugin, "forward_action") == []
    assert len(send_group_forward_calls(plugin)) == 2  # 两个目标群都试过一次


def test_forward_trace_injection_failure_does_not_rollback(tmp_path: Path):
    """留痕注入失败：记 warning，不回滚已完成的投递。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.ctx.maisaka.context.fail_next = True

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert len(send_group_forward_calls(plugin)) == 2
    assert any(
        "forward_action" in text for text in plugin.ctx.logger.texts("warning")
    )


# ---- 4.4 请求路径 Hook / Tool 委托 ----


def test_forward_tool_declared_with_group_chat_scope(tmp_path: Path):
    """Tool 声明面：名字、处理器、**顶层** `chat_scope="group"` 与超时。

    Runner 只读组件声明**顶层**字段（缺省按 `"all"`），metadata 里的同名键到
    不了注册表，因此群聊可见性必须断言在顶层；原插件正为此外加覆写
    `get_components()`。`timeout_ms=120000` 对齐原插件：请求路径要等真实顺序
    投递，Host 默认 60s 可能不够。
    """
    plugin = make_forward_plugin(tmp_path)

    tools = [component for component in plugin.get_components() if component["type"] == "TOOL"]

    assert [component["name"] for component in tools] == [FORWARD_TOOL_NAME]
    assert tools[0]["chat_scope"] == "group"
    assert "chat_scope" not in tools[0]["metadata"]
    assert tools[0]["metadata"]["timeout_ms"] == 120000
    assert tools[0]["metadata"]["handler_name"] == "request_cross_group_forward"


def test_forward_hooks_declared_on_planner_chain(tmp_path: Path):
    """Planner Hook 声明面：before_request 与 after_response 各就各位。"""
    plugin = make_forward_plugin(tmp_path)

    hooks = {
        component["name"]: component["metadata"]
        for component in plugin.get_components()
        if component["type"] == "HOOK_HANDLER"
    }

    assert hooks["forward_view_result_capture"]["hook"] == "maisaka.planner.before_request"
    assert hooks["forward_request_authorize"]["hook"] == "maisaka.planner.after_response"
    assert hooks["forward_request_authorize"]["order"] == "early"
    assert hooks["forward_auto_view_orchestrator"]["hook"] == "maisaka.planner.after_response"
    assert hooks["forward_auto_view_orchestrator"]["order"] == "late"
    assert hooks["forward_detector"]["mode"] == "observe"


def test_forward_tool_delegates_to_runtime(tmp_path: Path):
    """Tool 调用原样委托运行时：包含 Host 注入的一次性凭据与其他上下文字段。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    plugin._forward_runtime = runtime  # type: ignore[assignment]

    result = asyncio.run(
        plugin.request_cross_group_forward(
            msg_id="90001",
            sharing_reason="值得分享",
            content_summary="摘要",
            platform="qq",
            group_id=SOURCE_GROUP,
            stream_id=SOURCE_STREAM,
            **{FORWARD_CONTEXT_TOKEN_ARGUMENT: "token-1"},
        )
    )

    assert result == {"success": True, "content": "已投递"}
    assert runtime.requests == [
        {
            "msg_id": "90001",
            "sharing_reason": "值得分享",
            "content_summary": "摘要",
            "context": {
                "platform": "qq",
                "group_id": SOURCE_GROUP,
                "stream_id": SOURCE_STREAM,
                FORWARD_CONTEXT_TOKEN_ARGUMENT: "token-1",
            },
        }
    ]


def test_forward_tool_without_runtime_returns_failure(tmp_path: Path):
    """转发域未启用（无运行时）：Tool 明确拒绝，不抛异常。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.enabled = False
    plugin._rebuild_runtime()

    result = asyncio.run(plugin.request_cross_group_forward(msg_id="90001"))

    assert result["success"] is False
    assert result["content"]


def test_before_request_hook_captures_view_results_and_appends_reminder(tmp_path: Path):
    """before_request Hook：同步查看资格，并按需在 Items 末尾追加续轮提醒。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    runtime.reminder = "<system-reminder>看完请判断</system-reminder>"
    plugin._forward_runtime = runtime  # type: ignore[assignment]

    result = asyncio.run(
        plugin.capture_view_forward_result(session_id=SOURCE_STREAM, items=[{"item_type": "A"}])
    )

    assert runtime.captured == [(SOURCE_STREAM, [{"item_type": "A"}])]
    assert result["action"] == "continue"
    items = result["modified_kwargs"]["items"]
    assert [item["item_type"] for item in items] == ["A", "UserMessageItem"]
    assert items[-1]["parts"] == [{"type": "text", "text": runtime.reminder}]


def test_before_request_hook_without_reminder_keeps_kwargs(tmp_path: Path):
    """没有待判断消息时不追加 Item，kwargs 原样返回（Hook 只在有提醒时改写）。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    plugin._forward_runtime = runtime  # type: ignore[assignment]
    items = [{"item_type": "A"}]

    result = asyncio.run(
        plugin.capture_view_forward_result(session_id=SOURCE_STREAM, items=items)
    )

    assert result["modified_kwargs"]["items"] is items


def test_after_response_hooks_round_trip_authorization_round(tmp_path: Path):
    """after_response 两跳：EARLY 清洗授权并把本轮标记交给 LATE 编排。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    runtime.authorize_result = ([{"call_id": "sanitized"}], "round-7")
    runtime.transform_items_result = [{"call_id": "after-late"}]
    plugin._forward_runtime = runtime  # type: ignore[assignment]

    async def scenario() -> dict[str, Any]:
        early = await plugin.authorize_forward_request_context(
            session_id=SOURCE_STREAM,
            output_items=[{"call_id": "raw"}],
        )
        assert early["action"] == "continue"
        return await plugin.orchestrate_view_before_forward(**early["modified_kwargs"])

    late = asyncio.run(scenario())

    assert runtime.authorized_output_items == [(SOURCE_STREAM, [{"call_id": "raw"}])]
    assert runtime.transformed_output_items == [
        (SOURCE_STREAM, [{"call_id": "sanitized"}], "round-7")
    ]
    assert late["modified_kwargs"]["output_items"] == [{"call_id": "after-late"}]
    assert FORWARD_AUTHORIZATION_ROUND_KWARG not in late["modified_kwargs"]


def test_after_response_hooks_support_legacy_tool_calls_shape(tmp_path: Path):
    """旧版响应形态（response + tool_calls）同样走 EARLY/LATE 两跳委托。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    runtime.authorize_result = ([{"id": "sanitized"}], "round-9")
    runtime.transform_result = ("改写后的响应", [{"id": "after-late"}])
    plugin._forward_runtime = runtime  # type: ignore[assignment]

    async def scenario() -> dict[str, Any]:
        early = await plugin.authorize_forward_request_context(
            session_id=SOURCE_STREAM,
            response="原始响应",
            tool_calls=[{"id": "raw"}],
        )
        return await plugin.orchestrate_view_before_forward(**early["modified_kwargs"])

    late = asyncio.run(scenario())

    assert runtime.authorized_calls == [(SOURCE_STREAM, [{"id": "raw"}])]
    assert runtime.transformed == [(SOURCE_STREAM, "原始响应", [{"id": "sanitized"}], "round-9")]
    assert late["modified_kwargs"]["response"] == "改写后的响应"
    assert late["modified_kwargs"]["tool_calls"] == [{"id": "after-late"}]


def test_forward_hooks_pass_through_without_runtime(tmp_path: Path):
    """转发域关闭（无运行时）：Planner Hook 全部放行，不改写 kwargs。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.enabled = False
    plugin._rebuild_runtime()

    async def scenario() -> list[dict[str, Any]]:
        return [
            await plugin.capture_view_forward_result(session_id=SOURCE_STREAM, items=[]),
            await plugin.authorize_forward_request_context(
                session_id=SOURCE_STREAM, tool_calls=[]
            ),
            await plugin.orchestrate_view_before_forward(
                session_id=SOURCE_STREAM, output_items=[]
            ),
        ]

    results = asyncio.run(scenario())

    assert all(result["action"] == "continue" for result in results)
    assert results[1]["modified_kwargs"] == {
        "session_id": SOURCE_STREAM,
        "tool_calls": [],
    }


def test_source_planner_trigger_delegates_qualifying_message(tmp_path: Path):
    """源群合格合并转发：入口提示（源群 Planner 触发）委托给运行时判定。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    plugin._forward_runtime = runtime  # type: ignore[assignment]
    message = forward_message()

    async def scenario() -> None:
        await plugin.handle_forward_message(message=message)
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert runtime.observed == [message]


def test_source_planner_trigger_not_delegated_for_other_groups(tmp_path: Path):
    """非白名单源群：不掷骰，也不触发源群 Planner 提示（spec 转发源消息的资格）。"""
    plugin = make_forward_plugin(tmp_path)
    runtime = FakeForwardRuntime()
    plugin._forward_runtime = runtime  # type: ignore[assignment]

    asyncio.run(plugin.handle_forward_message(message=forward_message(group="99999")))

    assert runtime.observed == []
    assert plugin._tasks == set()


# ---- 4.5 生命周期接线 ----


def stub_webhook(plugin: RepeaterPlugin, monkeypatch) -> None:
    """把上报监听的起停换成 no-op：生命周期用例不占端口、不碰网络。"""

    async def noop() -> None:
        return None

    monkeypatch.setattr(plugin, "_start_webhook", noop)
    monkeypatch.setattr(plugin, "_stop_webhook", noop)


def test_on_load_wires_forward_runtime_and_unload_stops_it(tmp_path: Path, monkeypatch):
    """on_load 装配并启动转发运行时（同时刷新群列表自检），on_unload 释放。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": BOT_QQ}},
            "get_group_list": {
                "status": "ok",
                "retcode": 0,
                "data": [{"group_id": int(TARGET_A)}, {"group_id": int(TARGET_B)}],
            },
        },
    )
    stub_webhook(plugin, monkeypatch)
    plugin._forward_runtime = None  # on_load 之外的装配不算数

    async def scenario() -> None:
        await plugin.on_load()
        assert plugin._forward_runtime is not None
        assert plugin._forward_availability.is_available("747839793", time.time()) is False
        await plugin.on_unload()
        assert plugin._forward_runtime is None

    asyncio.run(scenario())

    assert [name for name, _ in plugin.protocol_calls] == ["get_login_info", "get_group_list"]  # type: ignore[attr-defined]


def test_on_unload_cancels_inflight_forward_deliveries(tmp_path: Path):
    """卸载取消在途投递：卸载返回后不残留转发域后台任务。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.delay_min_seconds = 30.0
    plugin.config.forward.delay_max_seconds = 30.0

    async def scenario() -> tuple[bool, set[asyncio.Task]]:
        await plugin.handle_forward_message(message=forward_message())
        deliveries = pending_delivery_tasks(plugin)
        assert len(deliveries) == 2
        await asyncio.sleep(0)  # 让任务进入延迟
        assert not deliveries[0].done()
        await plugin.on_unload()
        await asyncio.sleep(0)
        live = [
            task
            for task in asyncio.all_tasks()
            if "_deliver_forward_to_target" in coro_name(task)
            or "_finalize_forward_delivery" in coro_name(task)
        ]
        return deliveries[0].cancelled(), plugin._forward_tasks | set(live)

    cancelled, leftovers = asyncio.run(scenario())

    assert cancelled is True
    assert leftovers == set()


def test_config_update_rebuilds_forward_state_and_cancels_old_tasks(tmp_path: Path, monkeypatch):
    """配置更新：旧投递任务取消、状态容器换新，新配置立即生效。"""
    plugin = make_forward_plugin(tmp_path)
    stub_webhook(plugin, monkeypatch)
    plugin.config.forward.delay_min_seconds = 30.0
    plugin.config.forward.delay_max_seconds = 30.0
    plugin.config.forward.target_groups = [TARGET_A]

    async def scenario() -> tuple[ForwardState, asyncio.Task]:
        await plugin.handle_forward_message(message=forward_message())
        old_state = plugin._forward_state
        old_task = pending_delivery_tasks(plugin)[0]
        await asyncio.sleep(0)
        await plugin.on_config_update("self", {}, "2")
        await asyncio.sleep(0)
        return old_state, old_task

    old_state, old_task = asyncio.run(scenario())

    assert old_task.cancelled() is True
    assert plugin._forward_state is not old_state  # 防重/冷却/已投递记录换新
    assert plugin._forward_runtime is not None
    assert plugin._forward_runtime.target_groups() == [TARGET_A]
    assert plugin._tasks == set()


# ---- 4.6 启动自检 ----


def test_startup_checks_warn_when_forward_enabled_without_base_url(tmp_path: Path):
    """forward.enabled=true 但 protocol.base_url 为空：启动自检给出明确提示。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.protocol.base_url = ""

    asyncio.run(plugin._startup_checks())

    warnings = plugin.ctx.logger.texts("warning")
    assert any("base_url 未配置" in text for text in warnings)
    assert any("跨群转发" in text for text in warnings)
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]


def test_startup_checks_refresh_group_availability(tmp_path: Path):
    """协议端可用时刷新**群可用性缓存**：不在列表的群判为不可用。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": BOT_QQ}},
            "get_group_list": {
                "status": "ok",
                "retcode": 0,
                "data": [{"group_id": int(TARGET_A)}, {"group_id": int(TARGET_B)}],
            },
        },
    )

    asyncio.run(plugin._startup_checks())

    now = time.time()
    assert plugin._forward_availability.has_fresh_data(now) is True
    assert plugin._forward_availability.is_available(TARGET_A, now) is True
    assert plugin._forward_availability.is_available("747839793", now) is False
    assert [name for name, _ in plugin.protocol_calls] == ["get_login_info", "get_group_list"]  # type: ignore[attr-defined]


def test_startup_checks_group_list_failure_warns_and_allows(tmp_path: Path):
    """群列表查询失败：记 warning，缓存保持未知（可用性校验放行，不静默失败）。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": BOT_QQ}},
            "get_group_list": None,
        },
    )

    asyncio.run(plugin._startup_checks())

    assert any("群列表" in text for text in plugin.ctx.logger.texts("warning"))
    now = time.time()
    assert plugin._forward_availability.has_fresh_data(now) is False
    assert plugin._forward_availability.is_available("747839793", now) is True


def test_startup_checks_skip_group_list_when_forward_disabled(tmp_path: Path):
    """forward.enabled=false：不查群列表（转发域整域停用，不做多余协议端调用）。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "get_login_info": {"status": "ok", "retcode": 0, "data": {"user_id": BOT_QQ}}
        },
    )
    plugin.config.forward.enabled = False

    asyncio.run(plugin._startup_checks())

    assert [name for name, _ in plugin.protocol_calls] == ["get_login_info"]  # type: ignore[attr-defined]


def test_forward_hit_without_base_url_abandons_delivery_with_warning(tmp_path: Path):
    """命中转发但协议端未配置：记 warning 并放弃投递，不调用协议端（不静默）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.protocol.base_url = ""

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    warnings = plugin.ctx.logger.texts("warning")
    assert any("自动转发" in text and "base_url" in text for text in warnings)


# ---- 5 协议端能力 ----


def test_send_group_forward_passes_params_and_returns_response(tmp_path: Path):
    """5.1 直发封装：`{group_id, messages}` 原样进协议端，成功返回完整响应体。"""
    response = {
        "status": "ok",
        "retcode": 0,
        "data": {"message_id": "fwd-90001", "res_id": "res-1", "forward_id": "fid-1"},
    }
    plugin = make_forward_plugin(tmp_path, protocol_responses={"send_group_forward_msg": response})
    messages = [{"type": "node", "data": {"user_id": "30003", "nickname": "群友甲", "content": []}}]

    result = asyncio.run(plugin._send_group_forward(TARGET_A, messages))

    assert result == response
    assert send_group_forward_calls(plugin) == [{"group_id": int(TARGET_A), "messages": messages}]


def test_send_group_forward_accepts_any_receipt_key(tmp_path: Path):
    """5.1 返回体兼容：只给 `res_id` / `forward_id` 的成功响应同样算成功。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": {
                "status": "ok",
                "retcode": 0,
                "data": {"res_id": "res-1", "forward_id": "fid-1"},
            }
        },
    )

    result = asyncio.run(plugin._send_group_forward(TARGET_A, [{"type": "node", "data": {}}]))

    assert result is not None
    assert result["data"]["res_id"] == "res-1"
    assert result["data"]["forward_id"] == "fid-1"


def test_send_group_forward_failed_status_is_failure(tmp_path: Path):
    """5.1 协议端失败（HTTP 200 + status=failed）：判失败，warning 带路径前缀与群号。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": {"status": "failed", "retcode": 1200, "wording": "禁言中"}
        },
    )

    result = asyncio.run(plugin._send_group_forward(TARGET_A, [{"type": "node", "data": {}}]))

    assert result is None
    assert any(
        "自动转发" in text and TARGET_A in text for text in plugin.ctx.logger.texts("warning")
    )


def test_send_group_forward_unreachable_is_failure(tmp_path: Path):
    """5.1 协议端不可达（_call_protocol 返回 None）：判失败并记 warning。"""
    plugin = make_forward_plugin(tmp_path, protocol_responses={"send_group_forward_msg": None})

    result = asyncio.run(plugin._send_group_forward(TARGET_A, [{"type": "node", "data": {}}]))

    assert result is None
    assert any(
        "自动转发" in text and TARGET_A in text for text in plugin.ctx.logger.texts("warning")
    )


def test_delivery_queries_group_list_once_for_concurrent_targets(tmp_path: Path):
    """5.2 缓存未命中：投递前补一次群列表查询，同批目标群共用在途查询（不是每群一次）。

    协议桩在 `get_group_list` 上真实挂起（`asyncio.Event` 闸门），保证两个
    目标群的投递任务同时进入刷新路径：桩在释放前计数。若去掉 in-flight 复用
    （每个投递任务各自刷新），第二个目标群会在闸门释放前发起第二次查询，本
    用例即失败（变异验证已确认）。
    """
    group_list_queries: list[dict[str, Any]] = []
    refresh_started = asyncio.Event()
    refresh_release = asyncio.Event()
    plugin = make_forward_plugin(tmp_path)
    raw_call_protocol = plugin._call_protocol

    async def gated_call_protocol(action_name: str, params: dict[str, Any]) -> Any:
        if action_name == "get_group_list":
            group_list_queries.append(params)
            refresh_started.set()
            await refresh_release.wait()
        return await raw_call_protocol(action_name, params)

    plugin._call_protocol = gated_call_protocol  # type: ignore[assignment]

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        drain_task = asyncio.create_task(drain_forward_tasks(plugin))
        await refresh_started.wait()
        # 让第二个目标群的投递任务进入刷新路径：它必须等待同一条在途查询。
        await asyncio.sleep(0)
        refresh_release.set()
        await drain_task

    asyncio.run(scenario())

    assert len(group_list_queries) == 1
    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert names.count("get_group_list") == 1
    assert names.count("send_group_forward_msg") == 2
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
        int(TARGET_A),
        int(TARGET_B),
    ]


def test_delivery_skips_group_list_query_while_cache_is_fresh(tmp_path: Path):
    """5.2 缓存新鲜：TTL 内的投递不重复调用协议端（调用计数断言）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin._forward_availability.update([SOURCE_GROUP, TARGET_A, TARGET_B], time.time())

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert "get_group_list" not in names
    assert names.count("send_group_forward_msg") == 2


def test_delivery_requeries_group_list_after_cache_expires(tmp_path: Path):
    """5.2 TTL 过期视为未命中：过期快照不作数，重新查询后按新列表判定。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "get_group_list": {
                "status": "ok",
                "retcode": 0,
                "data": [{"group_id": int(TARGET_B)}],
            }
        },
    )
    # 预置一份已过 TTL 的快照（曾被判定可用的 TARGET_A 在新列表里已不存在）
    expired_at = time.time() - plugin._forward_availability.ttl_seconds - 1.0
    plugin._forward_availability.update([TARGET_A], expired_at)

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is False
    assert [name for name, _ in plugin.protocol_calls] == ["get_group_list"]  # type: ignore[attr-defined]
    assert any(
        "自动转发" in text and TARGET_A in text for text in plugin.ctx.logger.texts("warning")
    )


def test_delivery_backs_off_group_list_requery_after_failure(tmp_path: Path):
    """5.2 查询失败：警告后按未知放行，投递照常；退避窗口内不重试、窗口外重试。

    旧行为是失败后「下次投递立刻重查」（既不建立可信快照，也不建立负缓存）——
    协议端不可达时每条转发消息都吃一次协议端超时；现在改为
    `_FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS` 窗口内跳过重查。
    """
    plugin = make_forward_plugin(tmp_path, protocol_responses={"get_group_list": None})

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    now = time.time()
    assert plugin._forward_availability.has_fresh_data(now) is False
    assert plugin._forward_availability.is_available(TARGET_A, now) is True
    warnings = plugin.ctx.logger.texts("warning")
    assert any("自动转发" in text and "群列表" in text for text in warnings)
    warnings_after_failure = len(warnings)

    # 退避窗口内：不发起查询、不重复刷 warning，投递仍按未知放行。
    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert names.count("get_group_list") == 1
    assert len(plugin.ctx.logger.texts("warning")) == warnings_after_failure

    # 显式把失败时刻挪出退避窗口（时间戳用 time.time() 基准，不能用 0.0）：重查。
    plugin._forward_group_list_failed_at = (
        time.time() - _FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS - 1.0
    )

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert names.count("get_group_list") == 2


# ---- 请求转发路径：可用性缓存刷新（缓存过期时不得静默放行） ----


def expire_availability_snapshot(plugin: RepeaterPlugin, groups: list[str]) -> None:
    """预置一份已过 TTL 的可用性快照：TTL 内查表不再作数。"""
    expired_at = time.time() - plugin._forward_availability.ttl_seconds - 1.0
    plugin._forward_availability.update(groups, expired_at)


def wire_request_source_message(plugin: RepeaterPlugin, message_id: str = "90001") -> None:
    """把 hook 形态的合并转发挂到消息 capability，供请求路径读取。"""
    plugin.ctx.message.results[message_id] = {
        **forward_message(message_id=message_id),
        "session_id": SOURCE_STREAM,
    }


def seed_request_view(plugin: RepeaterPlugin, message_id: str = "90001") -> None:
    """为请求路径预置 source 消息的完整查看资格。"""
    runtime = plugin._forward_runtime
    assert runtime is not None
    runtime.view_eligibility.sync_context(
        SOURCE_STREAM,
        [
            ViewToolObservation(
                call_id=f"seed-{message_id}",
                message_id=message_id,
                content="完整展开内容",
                kind=ViewObservationKind.SUCCESS,
            )
        ],
    )


async def invoke_request_forward_via_hooks(
    plugin: RepeaterPlugin,
    *,
    msg_id: str = "90001",
) -> dict[str, Any]:
    """按真实 EARLY Hook → Tool handler 顺序执行一次请求转发（不替换运行时）。"""
    tool_call = {
        "id": "test-forward-request",
        "function": {
            "name": FORWARD_TOOL_NAME,
            "arguments": {"msg_id": msg_id, "sharing_reason": "", "content_summary": ""},
        },
    }
    hook_result = await plugin.authorize_forward_request_context(
        session_id=SOURCE_STREAM,
        response="",
        tool_calls=[tool_call],
    )
    arguments = hook_result["modified_kwargs"]["tool_calls"][0]["function"]["arguments"]
    return await plugin.request_cross_group_forward(
        **arguments,
        platform="qq",
        group_id=SOURCE_GROUP,
        stream_id=SOURCE_STREAM,
    )


def test_request_path_refreshes_expired_group_list_before_target_check(tmp_path: Path):
    """缓存过期：请求路径投递前刷新群列表，刷新后不可用的目标群返回明确原因。

    过期快照里 TARGET_A 仍被视为“可用”，但协议端最新群列表已不含它。修复前
    请求路径只读过期缓存、按未知静默放行并把内容投递到已失效的群；期望刷新后
    TARGET_A 得到含群号与原因的失败结果（不出现「未知错误」），TARGET_B 照常投递。
    """
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "get_group_list": {
                "status": "ok",
                "retcode": 0,
                "data": [{"group_id": int(SOURCE_GROUP)}, {"group_id": int(TARGET_B)}],
            }
        },
    )
    expire_availability_snapshot(plugin, [SOURCE_GROUP, TARGET_A, TARGET_B])
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    result = asyncio.run(invoke_request_forward_via_hooks(plugin))

    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert names.count("get_group_list") >= 1
    assert result["success"] is False
    assert TARGET_A in result["content"]
    assert "该群不在机器人所在群列表" in result["content"]
    assert "未知错误" not in result["content"]
    # 请求路径经 Host send.forward 投递：只有刷新后仍可用的 TARGET_B 被发送。
    assert [call["stream_id"] for call in plugin.ctx.send.forward_calls] == ["stream-20002"]


def test_request_path_allows_group_list_refresh_failure_with_warning(tmp_path: Path):
    """刷新失败：请求路径按未知放行投递，warning 带「请求转发」前缀与群列表字样。"""
    plugin = make_forward_plugin(tmp_path, protocol_responses={"get_group_list": None})
    expire_availability_snapshot(plugin, [TARGET_A, TARGET_B])
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    result = asyncio.run(invoke_request_forward_via_hooks(plugin))

    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert names.count("get_group_list") >= 1
    assert plugin._forward_availability.has_fresh_data(time.time()) is False
    assert result["success"] is True
    assert [call["stream_id"] for call in plugin.ctx.send.forward_calls] == [
        "stream-20001",
        "stream-20002",
    ]
    warnings = plugin.ctx.logger.texts("warning")
    assert any("请求转发" in text and "群列表" in text for text in warnings)


def test_request_path_skips_group_list_refresh_while_cache_is_fresh(tmp_path: Path):
    """缓存新鲜：请求路径不重复调用协议端群列表（调用计数断言）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin._forward_availability.update([SOURCE_GROUP, TARGET_A, TARGET_B], time.time())
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    result = asyncio.run(invoke_request_forward_via_hooks(plugin))

    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert "get_group_list" not in names
    assert result["success"] is True
    assert [call["stream_id"] for call in plugin.ctx.send.forward_calls] == [
        "stream-20001",
        "stream-20002",
    ]


def test_shared_refresh_failure_logs_each_waiter_own_prefix(tmp_path: Path):
    """归属修正：请求路径等到自动路径发起的刷新任务，失败日志仍带「请求转发」。

    刷新任务由先到者创建、两条路径共享等待：任务自身的日志只用稳定域 tag
    「跨群转发」，按路径的成败由各等待方在 await 处自行记录——若沿用先到者
    的前缀，请求路径会等到一条带「自动转发」的刷新日志，路径归属错位。
    """
    refresh_started = asyncio.Event()
    refresh_release = asyncio.Event()
    request_waiter_reached = asyncio.Event()
    plugin = make_forward_plugin(tmp_path, protocol_responses={"get_group_list": None})
    raw_call_protocol = plugin._call_protocol

    async def gated_call_protocol(action_name: str, params: dict[str, Any]) -> Any:
        if action_name == "get_group_list":
            refresh_started.set()
            await refresh_release.wait()
        return await raw_call_protocol(action_name, params)

    plugin._call_protocol = gated_call_protocol  # type: ignore[assignment]
    raw_ensure = plugin._ensure_group_availability

    async def spy_ensure(log_prefix: str) -> None:
        if log_prefix == "请求转发":
            request_waiter_reached.set()
        await raw_ensure(log_prefix)

    plugin._ensure_group_availability = spy_ensure  # type: ignore[method-assign]
    expire_availability_snapshot(plugin, [SOURCE_GROUP, TARGET_A, TARGET_B])
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    async def scenario() -> dict[str, Any]:
        await plugin.handle_forward_message(message=forward_message())
        await refresh_started.wait()  # 自动路径已发起共享刷新任务
        request_task = asyncio.create_task(invoke_request_forward_via_hooks(plugin))
        await request_waiter_reached.wait()  # 请求路径已等到同一条在途刷新
        refresh_release.set()
        result = await request_task
        await drain_forward_tasks(plugin)
        return result

    result = asyncio.run(scenario())

    assert result["success"] is True
    names = [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]
    assert names.count("get_group_list") == 1  # 两条路径共用同一次刷新
    warnings = plugin.ctx.logger.texts("warning")
    assert any("跨群转发" in text and "群列表" in text for text in warnings)
    assert any("请求转发" in text and "群列表" in text for text in warnings)


def test_one_target_failure_does_not_block_other_targets(tmp_path: Path):
    """5.3 单目标群失败不熔断：另一个目标群照常投递，成功群照常留痕，失败群不重试。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": lambda params: (
                {"status": "failed", "retcode": 1200, "wording": "禁言中"}
                if str(params["group_id"]) == TARGET_A
                else {"status": "ok", "retcode": 0, "data": {"message_id": "fwd-20002"}}
            )
        },
    )

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    # 调用序列：A 失败后 B 仍被尝试，各自只调用一次（不重试）
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
        int(TARGET_A),
        int(TARGET_B),
    ]
    assert [call["stream_id"] for call in injections(plugin, "forward")] == ["stream-20002"]
    assert [call["visible_text"] for call in injections(plugin, "forward_action")] == [
        build_forward_trace_text([TARGET_B])
    ]


def test_automatic_path_failure_logs_carry_prefix_and_target(tmp_path: Path):
    """5.3 自动路径失败日志：一律 warning，带「自动转发」前缀；直发失败带目标群号。"""
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": {"status": "failed", "retcode": 1200, "wording": "禁言中"},
            "get_group_list": None,
        },
    )

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is False
    assert len(send_group_forward_calls(plugin)) == 1  # 不重试
    assert plugin.ctx.maisaka.context.calls == []
    assert plugin.ctx.maisaka.proactive.calls == []
    warnings = plugin.ctx.logger.texts("warning")
    assert any("自动转发" in text and "群列表" in text for text in warnings)
    assert any("自动转发" in text and "投递失败" in text and TARGET_A in text for text in warnings)


def test_post_send_failures_are_warnings_with_automatic_prefix(tmp_path: Path):
    """5.3 投递后处理失败（内容注入 / 强制发言）：warning 带前缀与群号，不改变投递结论。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.ctx.maisaka.context.fail_next = True
    plugin.ctx.maisaka.proactive.fail_next = True

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    warnings = plugin.ctx.logger.texts("warning")
    assert any(
        "自动转发" in text and "注入上下文失败" in text and TARGET_A in text
        for text in warnings
    )
    assert any(
        "自动转发" in text and "强制发言" in text and TARGET_A in text for text in warnings
    )


def test_delivery_registration_warns_with_prefix_without_source_stream(tmp_path: Path):
    """5.3 源群 stream 缺失：已投递登记跳过，warning 带「自动转发」前缀与源群号。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.ctx.chat.group_streams = [
        stream for stream in plugin.ctx.chat.group_streams if stream["group_id"] != SOURCE_GROUP
    ]

    result = asyncio.run(plugin._deliver_forward_to_target(parse_source(), TARGET_A))

    assert result is True
    assert any(
        "自动转发" in text and "已投递状态" in text and SOURCE_GROUP in text
        for text in plugin.ctx.logger.texts("warning")
    )


# ---- 6.2 集成用例：自动路径全链路、两路互斥与域间独立 ----


def test_auto_path_end_to_end_hook_to_trace(tmp_path: Path):
    """自动路径全链路：hook 载荷 → 命中 → 延迟 → 直发 → 目标群注入 → 强制发言 → 源群留痕。

    本用例的增量：两个目标群**全部投递成功**（零失败、零跳过）时的一条完整
    有序调用时间线——共享一次群列表刷新、逐群「直发 → 内容注入 → 触发」各自
    成组、收尾任务等齐两个投递任务后才注入唯一一条源群留痕。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.delay_min_seconds = 0.01
    plugin.config.forward.delay_max_seconds = 0.01

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    # 完整调用时间线：共享一次群列表刷新 → 逐目标群「直发 → 内容注入 → 触发」
    # → 最后一条源群留痕（收尾任务等齐全部投递任务后才注入）。
    # 隐含假设：伪协议端与伪 SDK 能力的 await 同步返回、不让出事件循环，两个
    # 投递任务因此按 spawn 顺序各自连续跑完、不交错；真实部署的日志允许交错，
    # 本断言钉住的是每个目标群内部的步骤顺序与留痕在全部投递之后。
    assert plugin.ctx.events == [
        "get_group_list",
        "send_group_forward_msg",
        "context.append:forward",
        "proactive.trigger",
        "send_group_forward_msg",
        "context.append:forward",
        "proactive.trigger",
        "context.append:forward_action",
    ]
    # 直发按 target_groups 顺序（任务启动顺序）；内容注入先于该群的强制发言。
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
        int(TARGET_A),
        int(TARGET_B),
    ]
    assert [(call["stream_id"], call["source_kind"]) for call in injections(plugin, "forward")] == [
        ("stream-20001", "forward"),
        ("stream-20002", "forward"),
    ]
    assert [call["stream_id"] for call in plugin.ctx.maisaka.proactive.calls] == [
        "stream-20001",
        "stream-20002",
    ]
    # 源群留痕只注入一次、列出全部成功群；自动路径不走主程序发送链。
    traces = injections(plugin, "forward_action")
    assert len(traces) == 1
    assert traces[0]["stream_id"] == SOURCE_STREAM
    assert traces[0]["visible_text"] == build_forward_trace_text([TARGET_A, TARGET_B])
    assert plugin.ctx.send.forward_calls == []
    assert plugin.ctx.send.text_calls == []


def test_auto_delivered_message_rejects_request_forward_tool(tmp_path: Path):
    """两路互斥：自动路径已投递登记 → 请求转发 Tool 返回「已转发过」。

    自动路径先对同一**转发源消息**的全部目标群投递成功并登记**已投递状态**；
    随后按真实 EARLY Hook → Tool handler 顺序发起请求转发（源消息、查看资格等
    前置全部就位），期望被拒绝且请求路径不产生任何发送。
    """
    plugin = make_forward_plugin(tmp_path)
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    async def scenario() -> dict[str, Any]:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)
        return await invoke_request_forward_via_hooks(plugin)

    result = asyncio.run(scenario())

    assert len(send_group_forward_calls(plugin)) == 2  # 自动路径确实投递过两群
    assert result["success"] is True
    assert result["accepted"] is False
    assert "已转发过" in result["content"]
    assert TARGET_A in result["content"]
    assert TARGET_B in result["content"]
    assert plugin.ctx.send.forward_calls == []  # 请求路径未重复投递


def test_silence_keeps_request_forward_tool_available(tmp_path: Path):
    """沉默不登记**已投递状态**：自动路径沉默后，请求转发 Tool 仍可正常执行。

    沉默用例反向拉满沉默权重（转发归零），保证掷骰真实发生而不是走「两权重
    全 0 不掷骰」分支；随后请求路径必须能照常顺序投递到两个目标群。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.weight_send = 0
    plugin.config.forward.weight_silence = 1000
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    async def scenario() -> dict[str, Any]:
        await plugin.handle_forward_message(message=forward_message())
        assert plugin._tasks == set()  # 沉默：无任何后台任务
        return await invoke_request_forward_via_hooks(plugin)

    result = asyncio.run(scenario())

    assert send_group_forward_calls(plugin) == []  # 自动路径沉默，无直发
    key = build_delivered_key(SOURCE_STREAM, "90001")
    assert plugin._forward_delivered.targets_for(key, time.time()) == []
    assert result["success"] is True
    assert result["accepted"] is True
    assert [call["stream_id"] for call in plugin.ctx.send.forward_calls] == [
        "stream-20001",
        "stream-20002",
    ]


def test_partial_target_hit_delivers_only_hit_target(tmp_path: Path, monkeypatch):
    """多目标部分命中：命中群投递，未命中群无任何动作。

    逐目标群独立掷骰的意义就在部分命中；用受控 roll 序列固定结果——权重 1:1
    时 A 命中（roll=0.1）、B 沉默（roll=0.9）。沉默群不得出现直发、注入、
    触发，也不得写入**目标群冷却**；命中群照常全程并进入冷却。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.weight_send = 1
    plugin.config.forward.weight_silence = 1
    plugin.config.forward.target_cooldown_seconds = 600
    # 受控 roll 序列：权重 1:1 时阈值 0.5，A 命中（0.1）、B 沉默（0.9）。
    # 隐含假设：两次 roll 是 handler 同步门禁段内仅有的 `random.random` 消耗
    # （延迟归零时 `_delay` 不取值），中间不插入其它随机来源。
    rolls = iter([0.1, 0.9])
    monkeypatch.setattr(random, "random", lambda: next(rolls))

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert plugin.ctx.events == [
        "get_group_list",
        "send_group_forward_msg",
        "context.append:forward",
        "proactive.trigger",
        "context.append:forward_action",
    ]
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [int(TARGET_A)]
    assert [call["stream_id"] for call in injections(plugin, "forward")] == ["stream-20001"]
    assert [call["stream_id"] for call in plugin.ctx.maisaka.proactive.calls] == ["stream-20001"]
    assert [call["visible_text"] for call in injections(plugin, "forward_action")] == [
        build_forward_trace_text([TARGET_A])
    ]
    now = time.time()
    assert plugin._forward_state.is_target_cooling(TARGET_A, 600, now) is True
    assert plugin._forward_state.is_target_cooling(TARGET_B, 600, now) is False


def test_content_injection_failure_keeps_trigger_and_trace(tmp_path: Path):
    """直发成功但**转发内容注入**失败：强制发言与源群留痕照常。

    注入失败按 spec 口径记 warning、不重试、不回滚投递（与直发失败「止步」是
    两回事）。单目标群下第一次 append 就是内容注入、第二次才是留痕：只让内容
    注入失败一次，验证后续步骤不被牵连。

    与既有用例的差异：直发计数、内容注入与强制发言三类断言分别与
    `test_deliver_forward_sends_then_injects_then_triggers`（投递全绿）和
    `test_post_send_failures_are_warnings_with_automatic_prefix`（注入与触发
    同时失败、只查 warning）重复，保留它们是本用例完整结论的一部分；本用例的
    增量是——内容注入失败后，源群**转发留痕**仍照常注入并列出该目标群。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.target_groups = [TARGET_A]
    plugin.ctx.maisaka.context.fail_next = True  # 第一次 append（内容注入）失败

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert len(send_group_forward_calls(plugin)) == 1  # 直发与投递结论不受影响
    content = injections(plugin, "forward")
    assert len(content) == 1  # 失败尝试同样留痕在调用记录里
    assert [call["stream_id"] for call in content] == ["stream-20001"]
    assert [call["stream_id"] for call in plugin.ctx.maisaka.proactive.calls] == ["stream-20001"]
    assert [call["visible_text"] for call in injections(plugin, "forward_action")] == [
        build_forward_trace_text([TARGET_A])
    ]
    assert any(
        "自动转发" in text and "注入上下文失败" in text and TARGET_A in text
        for text in plugin.ctx.logger.texts("warning")
    )


def test_merged_forward_does_not_accumulate_repeat_chain(tmp_path: Path):
    """域间独立：一条合并转发不产出**复读文本**，转发域与复读域互不干扰。

    同一条 hook 载荷喂给两个域的真实 handler：转发域照常投递，复读域不认它
    为候选（含 forward 段的消息不产出**复读文本**）——复读链一条不建、源消息
    照常 `continue` 进主链。
    """
    plugin = make_forward_plugin(tmp_path)
    message = forward_message()

    async def scenario() -> dict[str, Any]:
        result = await plugin.handle_repeat_message(message=message)
        await plugin.handle_forward_message(message=message)
        await drain_forward_tasks(plugin)
        return result

    result = asyncio.run(scenario())

    assert result["action"] == "continue"  # 复读域不拦截，也不产生任何复读出口
    assert plugin._repeat_state._chains == {}
    assert len(send_group_forward_calls(plugin)) == 2  # 转发域照常完成两群投递


# ---- manifest 能力面 ----


def test_manifest_declares_forward_domain_capabilities():
    """转发域新增的 capability 与 Planner Hook 订阅必须写进 manifest。

    整包一致性由 tests/test_plugin.py 的 AST 派生守护用例保证（缺声明与死声明
    双向拦）；这里固定住本组新增的声明面，漏掉时指向明确。
    """
    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "_manifest.json").read_text(encoding="utf-8")
    )

    assert {
        "maisaka.proactive.trigger",
        "chat.get_group_streams",
        "chat.open_session",
        "message.get_by_id",
        "send.forward",
        "maisaka.planner.before_request",
        "maisaka.planner.after_response",
    } <= set(manifest["capabilities"])
