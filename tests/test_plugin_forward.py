"""plugin.py 转发域测试：编排层 + 端到端集成 + manifest 能力面。

覆盖自动路径 Hook（门禁 / 防重）、**摘要就绪**后的掷骰与冷却写入、直发 / 决策
两种模式与转发决策 Tool、投递编舞（延迟 → 可用性校验 → 协议端直发 → 富摘要
注入 → 强制发言）、源群留痕、请求链路委托与生命周期接线。纯逻辑（掷骰、状态、
节点、文案、决策票）的单测在 tests/test_forwards_*.py。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import Any

import pytest
import repeater_plugin.plugin as plugin_module
from repeater_plugin.forwards.request import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
    build_delivered_key,
)
from repeater_plugin.forwards.request.source.history import ViewToolObservation
from repeater_plugin.forwards.request.source.models import ViewObservationKind
from repeater_plugin.forwards.source import ForwardSource, parse_forward_source
from repeater_plugin.forwards.state import ForwardState
from repeater_plugin.forwards.texts import (
    build_forced_comment_intent,
    build_forward_content_text,
    build_forward_trace_text,
)

from config import RepeaterConfig
from plugin import (
    _FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS,
    DECIDE_FORWARD_TOOL_NAME,
    RepeaterPlugin,
)
from tests.fake_maibot_sdk import FakePluginContext

BOT_QQ = "40004"
SOURCE_GROUP = "10001"
TARGET_A = "20001"
TARGET_B = "20002"
SOURCE_STREAM = "stream-10001"

_DEFAULT_PROTOCOL_RESPONSE: dict[str, Any] = {"status": "ok", "retcode": 0, "data": {}}

# 消息就绪替身：真实时序里 `processed_plain_text` 在消息入库后才出现（ADR 0011），
# 工厂默认首次查询即就绪；需要观察轮询 / 超时的用例自行覆盖 `ctx.message`。
DEFAULT_RICH_SUMMARY = "【合并转发消息（测试）】群友甲：有趣的消息"


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

    plugin._runner = None
    plugin._site = None
    plugin._tasks = set()
    plugin._forward_tasks = set()
    plugin._flush_loop_task = None
    plugin._self_qq = BOT_QQ
    plugin._emoji_action_supported = True
    plugin._rebuild_runtime()
    # 源群 Planner 入口提示恒定开启：真实运行时会给每条合格源消息起一个等待消息
    # 落库的后台任务，自动路径用例不需要它。这里按旧「源群触发开关关闭」的等价
    # 行为挡掉触发服务本身（调试统计仍照常），委托断言用替身运行时单独覆盖。
    plugin._forward_runtime.source_trigger.observe = (  # type: ignore[method-assign]
        lambda message: False
    )
    # 消息替身默认就绪：编排任务第一次查询即可拿到富摘要（轮询用例再覆盖）。
    plugin.ctx.message.default_result = {"processed_plain_text": DEFAULT_RICH_SUMMARY}

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


def hook_text_message(
    *, group: str, message_id: str, session_id: str, group_name: str = "某群"
) -> dict[str, Any]:
    """hook 载荷形态的普通文本消息（携带主程序算好的 `session_id`）。"""
    return {
        "message_id": message_id,
        "platform": "qq",
        "session_id": session_id,
        "is_notify": False,
        "message_info": {
            "group_info": {"group_id": group, "group_name": group_name},
            "user_info": {"user_id": "30003", "user_nickname": "群友甲", "user_cardname": ""},
        },
        "raw_message": [{"type": "text", "data": "随便聊聊"}],
    }


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
        "session_id": f"stream-{group}",  # 与 fake chat 能力的流 ID 口径一致（生产里两者同值）
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
    """编排任务 spawn 出、尚未执行的按目标群投递任务。"""
    return [
        task
        for task in plugin._tasks
        if not task.done() and "_deliver_forward_to_target" in coro_name(task)
    ]


def pending_orchestrators(plugin: RepeaterPlugin) -> list[asyncio.Task]:
    """同步段 spawn 出、尚未完成的自动路径编排任务。"""
    return [
        task
        for task in plugin._tasks
        if not task.done() and "_orchestrate_forward_delivery" in coro_name(task)
    ]


async def pump_until(predicate: Any, turns: int = 200) -> None:
    """让事件循环空转若干轮，直到条件成立（用于等待后台任务的中间状态）。"""
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("等待条件未在给定轮次内满足")


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


def observe_cooling_at_send(plugin: RepeaterPlugin) -> list[tuple[bool, bool]]:
    """包一层协议端 spy，观测每次直发时 (源群冷却, 目标群冷却) 是否已写入。

    直发是投递编舞里第一个可断言的公共事件，观测序列按直发调用顺序排列；冷却
    TTL 取当前配置，调用方需已把相应冷却窗口设为正值（否则观测恒为未冷却）。
    """
    observed: list[tuple[bool, bool]] = []
    raw_call_protocol = plugin._call_protocol

    async def spy_call_protocol(action_name: str, params: dict[str, Any]) -> Any:
        if action_name == "send_group_forward_msg":
            now = time.time()
            observed.append(
                (
                    plugin._forward_state.is_group_cooling(
                        SOURCE_GROUP, float(plugin.config.forward.cooldown_seconds), now
                    ),
                    plugin._forward_state.is_target_cooling(
                        str(params["group_id"]),
                        float(plugin.config.forward.target_cooldown_seconds),
                        now,
                    ),
                )
            )
        return await raw_call_protocol(action_name, params)

    plugin._call_protocol = spy_call_protocol  # type: ignore[assignment]
    return observed


def mark_forward_timeline(
    plugin: RepeaterPlugin, monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> list[float]:
    """装配「轮询 → 掷骰 → 延迟」时间线标记，返回每次 `asyncio.sleep` 的秒数。

    `ctx.message.get_by_id` spy 追加 `get_by_id`；`random.random` 固定 0.0 并
    先追加 `roll`；`asyncio.sleep` 记录秒数、追加 `delay` 且不真正等待（延迟
    取值已受控），因此本辅助只适用于不需要真实等待的用例。
    """
    raw_get_by_id = plugin.ctx.message.get_by_id

    async def spy_get_by_id(message_id: str, **kwargs: Any) -> Any:
        events.append("get_by_id")
        return await raw_get_by_id(message_id, **kwargs)

    monkeypatch.setattr(plugin.ctx.message, "get_by_id", spy_get_by_id)
    monkeypatch.setattr(random, "random", lambda: events.append("roll") or 0.0)
    monkeypatch.setattr(random, "uniform", lambda low, high: low)
    sleeps: list[float] = []

    async def spy_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        events.append("delay")

    monkeypatch.setattr(asyncio, "sleep", spy_sleep)
    return sleeps


# 占位观测（task 2.3）用例的全序事件基线：就绪查询与占位 info 两个观测标记、
# 两次掷骰、逐目标群「直发 → 注入 → trigger」、源群留痕；两个占位用例共用。
FULL_DELIVERY_EVENT_SEQUENCE = [
    "get_by_id",
    "placeholder_log",
    "roll",
    "roll",
    "get_group_list",
    "send_group_forward_msg",
    "context.append:forward",
    "proactive.trigger",
    "send_group_forward_msg",
    "context.append:forward",
    "proactive.trigger",
    "context.append:forward_action",
]


def placeholder_log_message(count: int) -> str:
    """占位观测 info 的定稿文案（`mark_placeholder_log_timeline` 按前缀识别）。"""
    return (
        f"自动转发富摘要媒体占位 {count} 处"
        "（主程序 images 表缺 hash 时麦麦多模态降级）msg=90001 group=10001"
    )


def mark_placeholder_log_timeline(
    plugin: RepeaterPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把占位观测 info 记进 `ctx.events` 时间线，供与轮询 / 掷骰的相对序断言。

    只按定稿文案前缀识别、原样转发调用（不改日志内容），与
    `mark_forward_timeline` 同用 `monkeypatch.setattr` 的观测口径。
    """
    raw_info = plugin.ctx.logger.info

    def spy_info(message: Any, *args: Any, **kwargs: Any) -> None:
        if "自动转发富摘要媒体占位" in str(message):
            plugin.ctx.events.append("placeholder_log")
        raw_info(message, *args, **kwargs)

    monkeypatch.setattr(plugin.ctx.logger, "info", spy_info)


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


def test_forward_ready_hit_writes_cooldown_before_delivering(tmp_path: Path):
    """就绪后命中：先写冷却再 spawn 投递，任务按 target_groups 顺序执行。

    冷却是否先于投递写入，通过每次直发调用时的源群/目标群冷却状态观测
    （`observe_cooling_at_send`）——协议端直发是编舞里第一个可断言的公共事件。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.cooldown_seconds = 60
    plugin.config.forward.target_cooldown_seconds = 60
    cooling_at_send = observe_cooling_at_send(plugin)

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert cooling_at_send == [(True, True), (True, True)]  # 两次直发都在冷却写入之后
    now = time.time()
    assert plugin._forward_state.is_duplicate("90001", 1800, now) is True
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is True
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is True
    assert plugin._forward_state.is_target_cooling(TARGET_B, 60, now) is True
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
        int(TARGET_A),
        int(TARGET_B),
    ]
    assert [call["visible_text"] for call in injections(plugin, "forward_action")] == [
        build_forward_trace_text([TARGET_A, TARGET_B])
    ]


def test_forward_non_source_group_has_no_side_effects(tmp_path: Path):
    """非白名单源群：不掷骰、不起任务、不写冷却、不写防重。"""
    plugin = make_forward_plugin(tmp_path)

    asyncio.run(plugin.handle_forward_message(message=forward_message(group="99999")))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    now = time.time()
    assert plugin._forward_state.is_duplicate("90001", 1800, now) is False
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False


def test_forward_own_message_has_no_side_effects(tmp_path: Path):
    """麦麦自己发出的合并转发不参与转发域判定。"""
    plugin = make_forward_plugin(tmp_path)

    asyncio.run(plugin.handle_forward_message(message=forward_message(sender=BOT_QQ)))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    now = time.time()
    assert plugin._forward_state.is_duplicate("90001", 1800, now) is False
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False


def test_forward_domain_disabled_has_no_side_effects(tmp_path: Path):
    """forward.enabled=false：转发域整域停用，命中权重拉满也不产生任何任务。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.enabled = False
    plugin._rebuild_runtime()

    asyncio.run(plugin.handle_forward_message(message=forward_message()))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    now = time.time()
    assert plugin._forward_state.is_duplicate("90001", 1800, now) is False
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
    assert plugin._forward_state.is_duplicate("90001", 1800, now) is False
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is False


def test_forward_message_without_valid_nodes_has_no_side_effects(tmp_path: Path):
    """转发段存在但节点全无内容（适配器展开失败）：不参与判定。"""
    plugin = make_forward_plugin(tmp_path)

    asyncio.run(plugin.handle_forward_message(message=forward_message(nodes=[{"content": []}])))

    assert plugin._tasks == set()
    assert plugin.protocol_calls == []  # type: ignore[attr-defined]
    assert plugin._forward_state.is_duplicate("90001", 1800, time.time()) is False


def test_forward_silence_outlet_writes_no_cooldown(tmp_path: Path):
    """沉默出口：掷骰发生但不写任何冷却（权重反向拉满，不用全零走不掷骰分支）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.weight_send = 0
    plugin.config.forward.weight_silence = 1000
    plugin.config.forward.cooldown_seconds = 60

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)
        assert plugin._tasks == set()

    asyncio.run(scenario())

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
    """Tool 声明面：两个转发 Tool 都有**顶层** `chat_scope="group"`、处理器名与超时。

    Runner 只读组件声明**顶层**字段（缺省按 `"all"`），metadata 里的同名键到
    不了注册表，因此群聊可见性必须断言在顶层；少了这层覆写，两个转发 Tool 在
    私聊会话里同样可见（可见性过滤失效）。`timeout_ms=120000` 对齐原插件：
    两条路径都要等真实顺序投递 / 投递编舞完成，Host 默认 60s 可能不够。
    """
    plugin = make_forward_plugin(tmp_path)

    tools = [component for component in plugin.get_components() if component["type"] == "TOOL"]

    assert sorted(component["name"] for component in tools) == sorted(
        [FORWARD_TOOL_NAME, DECIDE_FORWARD_TOOL_NAME]
    )
    assert all(component["chat_scope"] == "group" for component in tools)
    assert all("chat_scope" not in component["metadata"] for component in tools)
    assert all(component["metadata"]["timeout_ms"] == 120000 for component in tools)
    request_tool = next(
        component for component in tools if component["name"] == FORWARD_TOOL_NAME
    )
    assert request_tool["metadata"]["handler_name"] == "request_cross_group_forward"
    decision_tool = next(
        component for component in tools if component["name"] == DECIDE_FORWARD_TOOL_NAME
    )
    assert decision_tool["metadata"]["handler_name"] == "decide_forward_delivery"
    parameters = decision_tool["metadata"]["parameters_raw"]
    assert parameters["message_id"]["type"] == "string"
    assert parameters["message_id"]["required"] is True
    assert parameters["deliver"]["type"] == "boolean"
    assert parameters["deliver"]["required"] is True


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


def test_on_unload_cancels_inflight_orchestration_and_deliveries(tmp_path: Path):
    """卸载取消在途编排与投递：卸载返回后不残留任何转发域后台任务。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.delay_min_seconds = 30.0
    plugin.config.forward.delay_max_seconds = 30.0

    async def scenario() -> tuple[bool, set[asyncio.Task]]:
        await plugin.handle_forward_message(message=forward_message())
        orchestrators = pending_orchestrators(plugin)
        assert len(orchestrators) == 1
        # 编排任务先轮询到就绪、掷骰后再 spawn 两个投递任务，随后停在延迟里。
        await pump_until(lambda: len(pending_delivery_tasks(plugin)) == 2)
        deliveries = pending_delivery_tasks(plugin)
        assert not deliveries[0].done()
        await plugin.on_unload()
        await asyncio.sleep(0)
        live = [
            task
            for task in asyncio.all_tasks()
            if "_deliver_forward_to_target" in coro_name(task)
            or "_orchestrate_forward_delivery" in coro_name(task)
        ]
        return orchestrators[0].cancelled(), plugin._forward_tasks | set(live)

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
        await pump_until(lambda: bool(pending_delivery_tasks(plugin)))
        old_task = pending_delivery_tasks(plugin)[0]
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
    """把 hook 形态的合并转发挂到消息 capability，供请求路径读取。

    替身同时带上 `processed_plain_text`：真实入库形态本就有该字段，自动路径的
    就绪轮询与请求路径的读取共用这一份替身。
    """
    plugin.ctx.message.results[message_id] = {
        **forward_message(message_id=message_id),
        "session_id": SOURCE_STREAM,
        "processed_plain_text": DEFAULT_RICH_SUMMARY,
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
        await drain_forward_tasks(plugin)
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


# ---- 组 3：同步段收敛、摘要就绪编排、模式与决策 Tool ----


def test_forward_sync_section_spawns_orchestration_without_rolling(tmp_path: Path, monkeypatch):
    """同步段只门禁 + 写防重 + spawn 一个编排任务，不掷骰不写冷却（spec 触发时机）。

    断言在 handler 返回后立即发生——此时编排任务还没获得事件循环，掷骰与冷却
    写入都没有机会发生；`random.random` 换成计数哨兵，掷骰若留在同步段立刻暴露。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.cooldown_seconds = 60
    plugin.config.forward.target_cooldown_seconds = 60
    rolls: list[float] = []
    monkeypatch.setattr(random, "random", lambda: rolls.append(0.0) or 0.0)

    async def scenario() -> tuple[list[str], dict[str, Any]]:
        await plugin.handle_forward_message(message=forward_message())
        now = time.time()
        observations = {
            "spawned": [coro_name(task) for task in plugin._tasks if not task.done()],
            "rolls": list(rolls),
            "polls": list(plugin.ctx.message.calls),
            "protocol": list(plugin.protocol_calls),  # type: ignore[attr-defined]
            "duplicate": plugin._forward_state.is_duplicate("90001", 1800, now),
            "group_cooling": plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now),
            "target_cooling": plugin._forward_state.is_target_cooling(TARGET_A, 60, now),
        }
        await drain_forward_tasks(plugin)
        return observations.pop("spawned"), observations

    spawned, observations = asyncio.run(scenario())

    assert spawned == ["RepeaterPlugin._orchestrate_forward_delivery"]
    assert observations["rolls"] == []
    assert observations["polls"] == []
    assert observations["protocol"] == []
    assert observations["duplicate"] is True  # 防重标记先于等待写入
    assert observations["group_cooling"] is False
    assert observations["target_cooling"] is False


def test_orchestration_waits_for_summary_before_rolling(tmp_path: Path, monkeypatch):
    """摘要就绪前不掷骰 / 不写冷却 / 不投递；轮询全程只在编排任务内（spec 触发时机）。

    三次 `get_by_id` 依次返回「未落库、未落库、就绪」；哨兵在每次查询前记录
    当时的掷骰计数、协议端与注入调用、存活任务集合——前两次查询时也没有任何
    副作用，就绪轮询不是另起的轮询任务。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.cooldown_seconds = 60
    plugin.ctx.message.sequences["90001"] = [
        None,
        None,
        {"processed_plain_text": DEFAULT_RICH_SUMMARY},
    ]
    monkeypatch.setattr(plugin_module, "next_poll_interval", lambda attempt: 0.0)
    rolls: list[float] = []
    monkeypatch.setattr(random, "random", lambda: rolls.append(0.0) or 0.0)
    observations: list[dict[str, Any]] = []
    raw_get_by_id = plugin.ctx.message.get_by_id

    async def spy_get_by_id(message_id: str, **kwargs: Any) -> Any:
        observations.append(
            {
                "rolls": len(rolls),
                "protocol": list(plugin.protocol_calls),  # type: ignore[attr-defined]
                "injections": list(plugin.ctx.maisaka.context.calls),
                "live_tasks": [coro_name(task) for task in plugin._tasks if not task.done()],
                "group_cooling": plugin._forward_state.is_group_cooling(
                    SOURCE_GROUP, 60, time.time()
                ),
            }
        )
        return await raw_get_by_id(message_id, **kwargs)

    monkeypatch.setattr(plugin.ctx.message, "get_by_id", spy_get_by_id)

    async def scenario() -> None:
        # 走真实同步段 spawn 路径：轮询期间存活的后台任务就是编排任务本身。
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert len(observations) == 3
    assert all(observed["rolls"] == 0 for observed in observations)
    assert all(observed["protocol"] == [] for observed in observations)
    assert all(observed["injections"] == [] for observed in observations)
    assert all(observed["group_cooling"] is False for observed in observations)
    assert all(
        observed["live_tasks"] == ["RepeaterPlugin._orchestrate_forward_delivery"]
        for observed in observations
    )
    polls = plugin.ctx.message.calls
    assert [poll["message_id"] for poll in polls] == ["90001"] * 3
    # 就绪轮询按消息 ID 单独查，不带 stream_id（陈旧 stream 永远查不到）。
    assert all(poll["stream_id"] == "" for poll in polls)
    assert all(poll["include_binary_data"] is False for poll in polls)
    assert plugin.ctx.logger.texts("warning") == []


def test_orchestration_gives_up_after_summary_timeout(tmp_path: Path, monkeypatch):
    """超时未就绪：warning 含 msg 与源群号，放弃且不掷骰 / 不写冷却 / 不投递。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.summary_timeout_seconds = 1
    plugin.config.forward.cooldown_seconds = 60
    plugin.ctx.message.default_result = None
    monkeypatch.setattr(plugin_module, "next_poll_interval", lambda attempt: 0.05)
    rolls: list[float] = []
    monkeypatch.setattr(random, "random", lambda: rolls.append(0.0) or 0.0)

    asyncio.run(plugin._orchestrate_forward_delivery(parse_source()))

    assert rolls == []
    assert send_group_forward_calls(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []
    now = time.time()
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is False
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is False
    warnings = plugin.ctx.logger.texts("warning")
    assert len(warnings) == 1
    assert "摘要就绪超时" in warnings[0]
    assert "90001" in warnings[0] and SOURCE_GROUP in warnings[0]


def test_orchestration_keeps_polling_after_query_exception(tmp_path: Path, monkeypatch):
    """get_by_id 抛异常按未就绪续等（debug 不刷 warning），不放弃候选（design D2）。"""
    plugin = make_forward_plugin(tmp_path)
    attempts: list[dict[str, Any]] = []
    raw_get_by_id = plugin.ctx.message.get_by_id

    async def flaky_get_by_id(message_id: str, **kwargs: Any) -> Any:
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("模拟查询失败")
        return await raw_get_by_id(message_id, **kwargs)

    monkeypatch.setattr(plugin.ctx.message, "get_by_id", flaky_get_by_id)

    asyncio.run(plugin._orchestrate_forward_delivery(parse_source()))

    assert len(attempts) == 2  # 异常后继续轮询，第二次拿到就绪结果
    assert plugin.ctx.logger.texts("warning") == []
    assert any("未就绪续等" in text for text in plugin.ctx.logger.texts("debug"))


def test_orchestration_polls_by_message_id_without_stream(tmp_path: Path, monkeypatch):
    """就绪轮询不带 `stream_id`（消息 ID 在平台内唯一，带陈旧 stream 会永远查不到）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin._stream_ids.clear()
    plugin.ctx.chat.group_streams = [
        stream for stream in plugin.ctx.chat.group_streams if stream["group_id"] != SOURCE_GROUP
    ]
    seen: list[dict[str, Any]] = []
    raw_get_by_id = plugin.ctx.message.get_by_id

    async def spy_get_by_id(message_id: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return await raw_get_by_id(message_id, **kwargs)

    monkeypatch.setattr(plugin.ctx.message, "get_by_id", spy_get_by_id)

    asyncio.run(plugin._orchestrate_forward_delivery(parse_source()))

    assert seen == [{"include_binary_data": False}]


def test_orchestration_survives_stale_stream_cache(tmp_path: Path, monkeypatch):
    """群 stream 缓存陈旧（平滑迁移会话后的旧 stream 查不到消息）仍能就绪投递。

    回归用例：就绪轮询若带上按群解析出的**陈旧** stream_id，主机侧按
    「stream + message_id」过滤会返回空，插件会 11 轮全空、超时放弃——2026-09-27
    线上实测即此形态（同一群在 chat_sessions 里有多个 session）。
    """
    plugin = make_forward_plugin(tmp_path)
    rich = "【合并转发消息】陈旧 stream 也要能取到富摘要"
    plugin._stream_ids[SOURCE_GROUP] = "stale-stream-id"
    calls: list[dict[str, Any]] = []

    async def stale_stream_get_by_id(message_id: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs.get("stream_id"):
            return None  # 陈旧 stream 过滤下主机侧查不到
        return {"processed_plain_text": rich}

    monkeypatch.setattr(plugin.ctx.message, "get_by_id", stale_stream_get_by_id)

    asyncio.run(plugin._orchestrate_forward_delivery(parse_source()))

    assert calls == [{"include_binary_data": False}]
    assert all("stream_id" not in kwargs for kwargs in calls)
    assert [call["visible_text"] for call in injections(plugin, "forward")]  # 真的投递并注入
    sends = send_group_forward_calls(plugin)
    assert len(sends) == 2
    assert all(call["group_id"] in (int(TARGET_A), int(TARGET_B)) for call in sends)



def test_forward_delivery_prefers_hook_observed_session(tmp_path: Path):
    """群 → 活跃聊天流优先取 hook 上报的 session，能力首匹配只作兜底。

    回归：同一群在 `chat_sessions` 里有新旧多个 session 时，`get_stream_by_group_id`
    返回列表首匹配（可能是**陈旧**会话）。若**转发内容注入**、**强制发言**触发与
    **已投递状态**登记落在陈旧会话，麦麦会在死会话里生成回复、发送必然失败
    （2026-09-27 线上实测：`未找到目标平台: qq` → `legacy.send.qq` 失败），注入的
    转发内容也不会出现在麦麦真正在读的会话里。
    """
    plugin = make_forward_plugin(tmp_path)
    # 主机侧能力查询返回「陈旧」首匹配（同群多 session 时的真实行为）
    for stream in plugin.ctx.chat.group_streams:
        stream["stream_id"] = f"stale-{stream['group_id']}"
        stream["session_id"] = f"stale-{stream['group_id']}"
    plugin._stream_ids.clear()

    # 三个群各来一条普通消息：hook 载荷带主程序算好的活跃 session
    observations = {
        SOURCE_GROUP: "live-source",
        TARGET_A: "live-target-a",
        TARGET_B: "live-target-b",
    }

    async def scenario() -> None:
        for index, (group, observed_session) in enumerate(observations.items()):
            await plugin.handle_forward_message(
                message=hook_text_message(
                    group=group, message_id=f"7000{index}", session_id=observed_session
                )
            )
        await plugin._orchestrate_forward_delivery(parse_source())

    asyncio.run(scenario())

    injected = sorted(call["stream_id"] for call in injections(plugin, "forward"))
    assert injected == ["live-target-a", "live-target-b"]
    triggered = sorted(call["stream_id"] for call in plugin.ctx.maisaka.proactive.calls)
    assert triggered == ["live-target-a", "live-target-b"]
    assert [call["stream_id"] for call in injections(plugin, "forward_action")] == ["live-source"]
    assert not any("stale" in str(call["stream_id"]) for call in plugin.ctx.maisaka.context.calls)
    delivered = plugin._forward_delivered.targets_for(
        build_delivered_key("live-source", "90001"), time.time()
    )
    assert sorted(delivered) == [TARGET_A, TARGET_B]
    # 观察到的群根本不需要再问能力：解析器不该被调用
    assert [call for call in plugin.ctx.chat.calls if call["method"] == "get_stream_by_group_id"] == []


def test_orchestration_injects_rich_summary_and_intent(tmp_path: Path):
    """富摘要贯通：就绪时取到的富摘要成为注入与 intent 的材料，不走节点摘要兜底。"""
    plugin = make_forward_plugin(tmp_path)
    rich = "【合并转发消息（共 2 条）】小明：今晚打牌吗？小红：好啊"
    plugin.ctx.message.default_result = {"processed_plain_text": rich}
    source = parse_source()

    asyncio.run(plugin._orchestrate_forward_delivery(source))

    contents = [call["visible_text"] for call in injections(plugin, "forward")]
    assert len(contents) == 2
    assert all(rich in text for text in contents)
    # 节点摘要（`1. 群友甲：有趣的消息`）是富摘要缺失时的兜底，本次不得出现。
    assert all("有趣的消息" not in text for text in contents)
    intents = [call["intent"] for call in plugin.ctx.maisaka.proactive.calls]
    assert len(intents) == 2
    assert all(rich in intent for intent in intents)
    assert all("有趣的消息" not in intent for intent in intents)


def test_orchestration_sanitizes_control_chars_in_display_text(tmp_path: Path):
    """来源群名含控制字符：注入与 intent 输出都已清洗（design D3）。"""
    plugin = make_forward_plugin(tmp_path)
    source = parse_source(forward_message(group_name="<\x11\x10>柴犬的新窝"))

    asyncio.run(plugin._orchestrate_forward_delivery(source))

    text = injections(plugin, "forward")[0]["visible_text"]
    assert "\x11" not in text and "\x10" not in text
    assert "柴犬的新窝" in text
    intent = plugin.ctx.maisaka.proactive.calls[0]["intent"]
    assert "\x11" not in intent and "\x10" not in intent
    assert "柴犬的新窝" in intent


def test_orchestration_logs_placeholder_count_before_rolling(tmp_path: Path, monkeypatch):
    """占位观测（2.3）：就绪拿到含 2 处 `[图片]` 的富摘要后、掷骰之前记占位 info。

    全序断言：`get_by_id → 占位 info → 掷骰 → 直发 → 注入 → trigger → 留痕`；
    占位按主程序原文保留进注入与 intent，观测不改写材料、不进行为分支。
    """
    plugin = make_forward_plugin(tmp_path)
    rich = "【合并转发消息】群友甲：[图片] 群友乙：[图片]"
    plugin.ctx.message.default_result = {"processed_plain_text": rich}
    mark_forward_timeline(plugin, monkeypatch, plugin.ctx.events)
    mark_placeholder_log_timeline(plugin, monkeypatch)

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert placeholder_log_message(2) in plugin.ctx.logger.texts("info")
    assert plugin.ctx.events == FULL_DELIVERY_EVENT_SEQUENCE
    # 占位观测不改写注入与 intent 材料：富摘要原文（含两处占位）照常贯通。
    contents = [call["visible_text"] for call in injections(plugin, "forward")]
    assert len(contents) == 2
    assert all(rich in text for text in contents)


def test_orchestration_zero_placeholder_keeps_delivery_sequence(tmp_path: Path, monkeypatch):
    """占位观测（2.3）：富摘要无占位 → 计数 0；掷骰 / 投递全序与既有基线逐项一致。

    期望事件序列就是无该日志时的既有全链路基线（`get_group_list → 逐目标群
    「直发 → 注入 → trigger」→ 留痕`），仅前置两个观测标记（`get_by_id` 与
    占位 info 同为观测，不改变行为步骤及其相对序）。
    """
    plugin = make_forward_plugin(tmp_path)
    mark_forward_timeline(plugin, monkeypatch, plugin.ctx.events)
    mark_placeholder_log_timeline(plugin, monkeypatch)

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert placeholder_log_message(0) in plugin.ctx.logger.texts("info")
    assert plugin.ctx.events == FULL_DELIVERY_EVENT_SEQUENCE
    assert len(send_group_forward_calls(plugin)) == 2


@pytest.mark.parametrize(
    ("auto_enabled", "request_enabled", "expect_orchestration", "expect_hint"),
    [
        (True, True, True, True),
        (False, True, False, False),
        (True, False, True, False),
        (False, False, False, False),
    ],
)
def test_forward_path_switch_matrix(
    tmp_path: Path,
    auto_enabled: bool,
    request_enabled: bool,
    expect_orchestration: bool,
    expect_hint: bool,
):
    """路径开关矩阵：auto_enabled 只停自动编排与自动 Hook 入口提示，request_enabled 只停请求入口。

    自动 Hook 同步段是自动路径的入口（含源群 Planner 入口提示），整体短路；
    请求转发 Tool 单独跟随 `request_enabled`，两种开关互不牵扯（spec 转发路径开关）。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.auto_enabled = auto_enabled
    plugin.config.forward.request_enabled = request_enabled
    runtime = FakeForwardRuntime()
    plugin._forward_runtime = runtime  # type: ignore[assignment]
    message = forward_message()

    async def scenario() -> tuple[int, dict[str, Any]]:
        await plugin.handle_forward_message(message=message)
        spawned = len([task for task in plugin._tasks if not task.done()])
        result = await plugin.request_cross_group_forward(msg_id="90001")
        await drain_forward_tasks(plugin)
        return spawned, result

    spawned, result = asyncio.run(scenario())

    assert spawned == (1 if expect_orchestration else 0)
    assert runtime.observed == ([message] if expect_hint else [])
    if expect_orchestration:
        assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
            int(TARGET_A),
            int(TARGET_B),
        ]
    else:
        assert send_group_forward_calls(plugin) == []
    if request_enabled:
        assert result == {"success": True, "content": "已投递"}
        assert len(runtime.requests) == 1
    else:
        assert result["success"] is False
        assert result["content"]
        assert runtime.requests == []


def test_planner_mode_triggers_decision_without_delivering(tmp_path: Path):
    """决策模式命中：不直发不注入，逐命中目标群签发决策票并触发 Planner 决策（spec 转发模式）。

    票里同时登记**转发源消息快照**与**富摘要**：模型决策回来时快照即发送字节的
    唯一来源（ADR 0011）。intent 必须写明 message_id 与目标群号供模型回传。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"
    plugin.config.forward.cooldown_seconds = 60
    plugin.config.forward.target_cooldown_seconds = 60
    rich = "【合并转发消息】小明：今晚打牌吗？"
    plugin.ctx.message.default_result = {"processed_plain_text": rich}
    source = parse_source()

    asyncio.run(plugin._orchestrate_forward_delivery(source))

    assert send_group_forward_calls(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []
    triggers = plugin.ctx.maisaka.proactive.calls
    assert [call["stream_id"] for call in triggers] == ["stream-20001", "stream-20002"]
    assert [call["metadata"]["trigger_kind"] for call in triggers] == [
        "forward_decision",
        "forward_decision",
    ]
    assert [call["metadata"]["source_message_id"] for call in triggers] == ["90001", "90001"]
    assert [call["metadata"]["target_group_id"] for call in triggers] == [TARGET_A, TARGET_B]
    intent = triggers[0]["intent"]
    assert "90001" in intent and TARGET_A in intent and rich in intent
    assert DECIDE_FORWARD_TOOL_NAME in intent and "deliver" in intent
    infos = plugin.ctx.logger.texts("info")
    assert any(
        "决策模式" in text and TARGET_A in text for text in infos
    )
    # 触发成功日志带内部票号（便于对账），但票号不得出现在 intent 里（design D4）。
    ticket_logs = [text for text in infos if "ticket=" in text]
    assert len(ticket_logs) == 2
    assert any(TARGET_A in text for text in ticket_logs)
    assert any(TARGET_B in text for text in ticket_logs)
    tickets = [text.rsplit("ticket=", 1)[1] for text in ticket_logs]
    assert all(ticket and ticket not in intent for ticket in tickets)
    # 掷骰先行：命中即写冷却，与 direct 模式同一口径（决策结果不影响冷却写入）。
    now = time.time()
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, now) is True
    assert plugin._forward_state.is_target_cooling(TARGET_A, 60, now) is True
    # 票已签发并携带快照 + 富摘要。
    payload = plugin._forward_tickets.consume("90001", TARGET_A, now=now)
    assert payload is not None
    issued_source, issued_summary = payload
    assert issued_source == source
    assert issued_summary == rich


def test_planner_mode_trigger_failure_warns_and_does_not_deliver(tmp_path: Path):
    """决策触发失败：按「不转发」收尾——记 warning、焚票、不投递不注入（spec 转发模式）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"
    plugin.ctx.maisaka.proactive.fail_next = True

    asyncio.run(plugin._orchestrate_forward_delivery(parse_source()))

    assert send_group_forward_calls(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []
    warnings = plugin.ctx.logger.texts("warning")
    assert any("决策" in text and TARGET_A in text for text in warnings)
    assert plugin._forward_tickets.consume("90001", TARGET_A, now=time.time()) is None


def test_planner_mode_warns_when_decision_tickets_expire_unconsumed(tmp_path: Path):
    """决策票 TTL 到期仍未消费：下一轮决策编排逐票记 warning，含 msg 与目标群号。

    模型始终不调用决策 Tool 时，票由 TTL 清理；清理必须留下可归因的 warning
    并按「不转发」处理（spec 转发模式：决策结果不可用 MUST 记 warning），不得
    静默丢弃。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"
    plugin._forward_tickets.ttl_seconds = 0.05

    async def scenario() -> None:
        await plugin._orchestrate_forward_delivery(parse_source())
        await asyncio.sleep(0.08)  # 首轮票过期且未被消费
        await plugin._orchestrate_forward_delivery(
            parse_source(forward_message(message_id="90002"))
        )

    asyncio.run(scenario())

    timeout_warnings = [
        text for text in plugin.ctx.logger.texts("warning") if "决策票超时未消费" in text
    ]
    assert len(timeout_warnings) == 2
    assert any("90001" in text and TARGET_A in text for text in timeout_warnings)
    assert any("90001" in text and TARGET_B in text for text in timeout_warnings)
    # 超时票不投递：两轮编排都只触发决策，没有任何直发与注入。
    assert send_group_forward_calls(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []


def test_direct_mode_has_no_decision_tickets_or_triggers(tmp_path: Path):
    """直发模式：命中即投递，无决策票、无决策触发（spec 转发模式）。"""
    plugin = make_forward_plugin(tmp_path)

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    assert len(send_group_forward_calls(plugin)) == 2
    assert plugin._forward_tickets.consume("90001", TARGET_A, now=time.time()) is None
    assert [call["metadata"]["trigger_kind"] for call in plugin.ctx.maisaka.proactive.calls] == [
        "auto_forward",
        "auto_forward",
    ]


def test_invalid_forward_mode_warns_and_falls_back_to_direct(tmp_path: Path):
    """非法 mode：记 warning 并按 direct 处理（spec 转发模式 / design D6）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "smart"

    asyncio.run(plugin._orchestrate_forward_delivery(parse_source()))

    assert len(send_group_forward_calls(plugin)) == 2
    assert plugin._forward_tickets.consume("90001", TARGET_A, now=time.time()) is None
    assert [call["metadata"]["trigger_kind"] for call in plugin.ctx.maisaka.proactive.calls] == [
        "auto_forward",
        "auto_forward",
    ]
    warnings = plugin.ctx.logger.texts("warning")
    assert any("smart" in text and "direct" in text for text in warnings)


def test_decision_tool_delivers_with_valid_ticket_and_burns_it(tmp_path: Path):
    """决策 Tool 持票转发：走同一套投递编舞（直发 → 富摘要注入 → 强制发言 → 登记留痕），票被焚。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"
    rich = "【合并转发消息】小明：今晚打牌吗？"
    plugin.ctx.message.default_result = {"processed_plain_text": rich}

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await plugin._orchestrate_forward_delivery(parse_source())
        first = await plugin.decide_forward_delivery(
            message_id="90001", deliver=True, group_id=TARGET_A, stream_id="stream-20001"
        )
        second = await plugin.decide_forward_delivery(
            message_id="90001", deliver=True, group_id=TARGET_A, stream_id="stream-20001"
        )
        await drain_forward_tasks(plugin)
        return first, second

    first, second = asyncio.run(scenario())

    assert first["success"] is True
    assert second["success"] is False  # 同票二次调用被拒
    assert second["content"]
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [int(TARGET_A)]
    content = injections(plugin, "forward")
    assert [call["stream_id"] for call in content] == ["stream-20001"]
    assert rich in content[0]["visible_text"]
    # 决策后的投递照常登记**已投递状态**并注入源群**转发留痕**（决策模式不留痕盲区）。
    key = build_delivered_key(SOURCE_STREAM, "90001")
    assert plugin._forward_delivered.is_delivered(key, TARGET_A, time.time()) is True
    assert [call["visible_text"] for call in injections(plugin, "forward_action")] == [
        build_forward_trace_text([TARGET_A])
    ]
    # 两个目标群的决策触发在前，投递成功后的**强制发言**触发在后。
    assert [call["metadata"]["trigger_kind"] for call in plugin.ctx.maisaka.proactive.calls] == [
        "forward_decision",
        "forward_decision",
        "auto_forward",
    ]
    # 决策消费与无票拒绝都有可 grep 的 info 文案（含目标群号与消息 ID）。
    infos = plugin.ctx.logger.texts("info")
    assert any(
        "目标群选择转发" in text and TARGET_A in text and "90001" in text for text in infos
    )
    assert any("拒绝无票调用" in text and TARGET_A in text for text in infos)


def test_decision_tool_deliver_true_but_delivery_fails(tmp_path: Path):
    """决策 Tool 选择转发但投递失败：返回失败，不注入、不触发强制发言（spec 转发模式）。

    票已在决策时消费（决策本身生效），投递失败只影响本次结果：不重试、不写
    **转发内容注入**、不触发**强制发言**，也不产生源群**转发留痕**。
    """
    plugin = make_forward_plugin(
        tmp_path,
        protocol_responses={
            "send_group_forward_msg": {"status": "failed", "retcode": 1200, "wording": "禁言中"}
        },
    )
    plugin.config.forward.mode = "planner"

    async def scenario() -> dict[str, Any]:
        await plugin._orchestrate_forward_delivery(parse_source())
        result = await plugin.decide_forward_delivery(
            message_id="90001", deliver=True, group_id=TARGET_A, stream_id="stream-20001"
        )
        await drain_forward_tasks(plugin)
        return result

    result = asyncio.run(scenario())

    assert result["success"] is False
    assert result["content"]
    assert len(send_group_forward_calls(plugin)) == 1  # 只尝试一次，不重试
    assert injections(plugin, "forward") == []
    assert injections(plugin, "forward_action") == []
    # 只留下决策触发的 trigger_kind：投递失败不得触发**强制发言**。
    assert [call["metadata"]["trigger_kind"] for call in plugin.ctx.maisaka.proactive.calls] == [
        "forward_decision",
        "forward_decision",
    ]
    assert any(
        TARGET_A in text and "投递失败" in text for text in plugin.ctx.logger.texts("warning")
    )


def test_decision_tool_decline_logs_and_does_not_deliver(tmp_path: Path):
    """决策 Tool 持票不转发：只记 info 收尾，不投递不注入，票已焚（spec 转发模式）。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"

    async def scenario() -> dict[str, Any]:
        await plugin._orchestrate_forward_delivery(parse_source())
        result = await plugin.decide_forward_delivery(
            message_id="90001", deliver=False, group_id=TARGET_A
        )
        await drain_forward_tasks(plugin)
        return result

    result = asyncio.run(scenario())

    assert result["success"] is True
    assert send_group_forward_calls(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []
    assert any(
        "不转发" in text and TARGET_A in text for text in plugin.ctx.logger.texts("info")
    )
    assert plugin._forward_tickets.consume("90001", TARGET_A, now=time.time()) is None


def test_decision_tool_rejects_unknown_and_unrelated_group_calls(tmp_path: Path):
    """决策 Tool 无票 / 无关群调用被拒且不投递；无关群不焚掉合法目标群的票。

    与命中无关的群（未签发任何票）拿同一条 message_id 调用也匹配不到票——
    每个**目标群**的票是独立的，互不代偿。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"
    other_group = "20003"

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await plugin._orchestrate_forward_delivery(parse_source())
        unknown = await plugin.decide_forward_delivery(
            message_id="99999", deliver=True, group_id=TARGET_A
        )
        unrelated_group = await plugin.decide_forward_delivery(
            message_id="90001", deliver=True, group_id=other_group
        )
        await drain_forward_tasks(plugin)
        return unknown, unrelated_group

    unknown, unrelated_group = asyncio.run(scenario())

    assert unknown["success"] is False and unknown["content"]
    assert unrelated_group["success"] is False and unrelated_group["content"]
    assert send_group_forward_calls(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []
    now = time.time()
    assert plugin._forward_tickets.consume("90001", TARGET_A, now=now) is not None
    assert plugin._forward_tickets.consume("90001", TARGET_B, now=now) is not None


def test_decision_tool_rejects_calls_without_group_context(tmp_path: Path):
    """决策 Tool 缺群上下文：按无票拒绝并记 warning，不投递。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"

    async def scenario() -> dict[str, Any]:
        await plugin._orchestrate_forward_delivery(parse_source())
        return await plugin.decide_forward_delivery(message_id="90001", deliver=True)

    result = asyncio.run(scenario())

    assert result["success"] is False and result["content"]
    assert send_group_forward_calls(plugin) == []
    assert any(
        "群上下文" in text for text in plugin.ctx.logger.texts("warning")
    )


# ---- 组 4：集成测试（全链路 / 图片保全 / 决策三路径 / 冷却新语义 / 两路共用回归） ----


def test_direct_mode_full_chain_order_from_poll_to_trace(tmp_path: Path, monkeypatch):
    """直发模式全链路（4.1）：轮询 → 掷骰 → 延迟 → 冷却写入 → 直发（hook 载荷图片字节）。

    编舞尾段（共享群列表刷新 → 逐群「直发 → 注入 → 触发」→ 源群留痕）由
    `test_auto_path_end_to_end_hook_to_trace` 覆盖，本用例只断言独有增量：
    就绪轮询先于两次掷骰与两次延迟、冷却在每次直发时已写入，以及图片段的
    `base64://` 快照字节（入库形态不含二进制，字节只能来自 hook 载荷）。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.delay_min_seconds = 0.01
    plugin.config.forward.delay_max_seconds = 0.01
    plugin.config.forward.cooldown_seconds = 60
    plugin.config.forward.target_cooldown_seconds = 60
    image_b64 = base64.b64encode(b"snapshot-image-bytes").decode("ascii")
    nodes = [
        {
            "user_id": "30003",
            "user_nickname": "群友甲",
            "user_cardname": "",
            "message_id": "80001",
            "content": [
                {"type": "text", "data": "看图"},
                {"type": "image", "data": "[图片：一只猫]", "binary_data_base64": image_b64},
            ],
        }
    ]
    # 入库形态沿用工厂默认（只含 `DEFAULT_RICH_SUMMARY`）：图片字节不在其中
    # （真实入库形态不回填内存二进制），直发参数只能取自 hook 载荷快照。
    sleeps = mark_forward_timeline(plugin, monkeypatch, plugin.ctx.events)
    cooling_at_send = observe_cooling_at_send(plugin)

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message(nodes=nodes))
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    # 本用例独有前缀：轮询就绪 → 两个目标群掷骰 → 各自延迟，都在投递之前。
    assert plugin.ctx.events[:5] == ["get_by_id", "roll", "roll", "delay", "delay"]
    assert sleeps == [0.01, 0.01]
    assert cooling_at_send == [(True, True), (True, True)]  # 冷却先于每次直发写入
    # 富摘要注入/intent 的完整口径（两条注入与两条 intent、兜底摘要不出现）由
    # `test_orchestration_injects_rich_summary_and_intent` 覆盖；这里只钉一句
    # 「注入文本取自轮询到的入库形态」。
    assert DEFAULT_RICH_SUMMARY in injections(plugin, "forward")[0]["visible_text"]
    # 图片字节来自 hook 载荷快照：`base64://` 数据与快照逐字节一致。
    assert send_group_forward_calls(plugin)[0]["messages"] == [
        {
            "type": "node",
            "data": {
                "user_id": "30003",
                "nickname": "群友甲",
                "content": [
                    {"type": "text", "data": {"text": "看图"}},
                    {"type": "image", "data": {"file": f"base64://{image_b64}"}},
                ],
            },
        }
    ]


def test_delivery_uses_snapshot_image_bytes_when_stored_form_lacks_binary(tmp_path: Path):
    """图片保全（4.2）：入库形态不含二进制（模拟 2.8% 恢复失败）时，直发仍带 hook 载荷字节。

    图片段的 `data` 是主程序写入的中文描述文本（不是文件引用），内存字节只在
    hook 载荷里；fake `get_by_id` 的入库形态照此构造，`binary_data_base64` 缺失。
    发送字节必须来自**转发源消息快照**：直发参数里 `base64://` 解码后与 hook
    载荷字节逐字节一致，且入库形态确实无可恢复的字节。
    """
    plugin = make_forward_plugin(tmp_path)
    image_bytes = b"snapshot-image-payload"
    hook_segment = {
        "type": "image",
        "data": "[图片：一只猫]",
        "binary_data_base64": base64.b64encode(image_bytes).decode("ascii"),
    }
    nodes = [
        {
            "user_id": "30003",
            "user_nickname": "群友甲",
            "user_cardname": "",
            "message_id": "80001",
            "content": [hook_segment],
        }
    ]
    stored_segment = {"type": "image", "data": "[图片：一只猫]"}  # 入库形态：无二进制
    plugin.ctx.message.default_result = {
        "processed_plain_text": DEFAULT_RICH_SUMMARY,
        "raw_message": [{"type": "forward", "data": [{"content": [stored_segment]}]}],
    }

    async def scenario() -> None:
        await plugin.handle_forward_message(message=forward_message(nodes=nodes))
        await drain_forward_tasks(plugin)

    asyncio.run(scenario())

    # 入库形态构造上就没有二进制字段（恢复失败语义）：直发参数仍必须带 hook
    # 载荷快照的字节，逐字节相等即证明字节来源是快照而非入库形态。
    sends = send_group_forward_calls(plugin)
    assert len(sends) == 2
    for params in sends:
        content = params["messages"][0]["data"]["content"]
        assert content == [
            {"type": "image", "data": {"file": f"base64://{hook_segment['binary_data_base64']}"}}
        ]
        sent_bytes = base64.b64decode(content[0]["data"]["file"].removeprefix("base64://"))
        assert sent_bytes == image_bytes  # 字节与 hook 载荷一致


def test_decision_mode_three_paths_in_one_flow(tmp_path: Path):
    """决策模式三路径（4.3）：命中发票 →（a）Tool 转发（b）Tool 不转发（c）重放 / 无票被拒。

    三条路径各自的单路径行为由聚焦用例覆盖；本用例的增量只在一次流程里并存：
    四条 Tool 调用走完仍只有 A 的一次直发、一次内容注入、一条源群留痕；B 的
    不转发只焚票、不登记**已投递状态**，重放与无票调用同样不直发不注入。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.mode = "planner"
    # **富摘要**沿用工厂默认的 `DEFAULT_RICH_SUMMARY`（入库形态首查即就绪）。

    async def scenario() -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
    ]:
        await plugin.handle_forward_message(message=forward_message())
        await drain_forward_tasks(plugin)
        forwarded = await plugin.decide_forward_delivery(
            message_id="90001", deliver=True, group_id=TARGET_A, stream_id="stream-20001"
        )
        declined = await plugin.decide_forward_delivery(
            message_id="90001", deliver=False, group_id=TARGET_B, stream_id="stream-20002"
        )
        repeated = await plugin.decide_forward_delivery(
            message_id="90001", deliver=True, group_id=TARGET_B, stream_id="stream-20002"
        )
        no_ticket = await plugin.decide_forward_delivery(
            message_id="99999", deliver=True, group_id=TARGET_A, stream_id="stream-20001"
        )
        await drain_forward_tasks(plugin)
        return forwarded, declined, repeated, no_ticket

    forwarded, declined, repeated, no_ticket = asyncio.run(scenario())

    # 四条调用的返回结果：转发成功、不转发成功，重放与无票被拒（各自带说明文案）。
    assert forwarded["success"] is True
    assert declined["success"] is True
    assert declined["content"]
    assert repeated["success"] is False and repeated["content"]
    assert no_ticket["success"] is False and no_ticket["content"]
    # 三路径互不干扰：仍只有 A 的一次直发、一次内容注入、一条源群留痕；B 的
    # 不转发不登记**已投递状态**（`repeated` 被拒即是 B 的票已焚的证据）。
    key = build_delivered_key(SOURCE_STREAM, "90001")
    assert plugin._forward_delivered.targets_for(key, time.time()) == [TARGET_A]
    assert len(send_group_forward_calls(plugin)) == 1
    assert len(injections(plugin, "forward")) == 1
    assert len(injections(plugin, "forward_action")) == 1
    assert plugin.ctx.send.forward_calls == []


def test_second_forward_during_ready_wait_rolls_independently(tmp_path: Path, monkeypatch):
    """冷却新语义（4.4）：就绪等待窗口内到达的第二条合并转发独立掷骰，不被第一条冷却压制。

    第一条消息就绪慢（首次查询未就绪、退避后再查）；第二条在它的就绪等待窗口
    内进门——冷却尚未写入（写入时机已后移到就绪之后），第二条必须独立掷骰并
    投递。退避等待换成事件闸门：第一条停在闸门上、第二条投递完成前，冷却不可能
    被第一条写入，进门时机不再依赖真实调度时序。为证明这不是「冷却失效」：
    源群冷却窗口设为正值，事后第三条消息必须被已写入的冷却压制、零轮询。
    """
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.cooldown_seconds = 60
    plugin.config.forward.target_cooldown_seconds = 0
    plugin.ctx.message.sequences["90001"] = [None, {"processed_plain_text": "第一条"}]
    plugin.ctx.message.results["90002"] = {"processed_plain_text": "第二条"}
    # 退避间隔替换成事件闸门：第一条首次查询未就绪后停在闸门上，第二条进门的
    # 时机由用例显式放行，不再依赖「必须快于 0.01s 退避窗口」的抢跑。
    poll_gate = asyncio.Event()
    gate_seconds = 3600.0
    real_sleep = asyncio.sleep

    async def gated_sleep(seconds: float) -> None:
        if seconds == gate_seconds:
            await poll_gate.wait()
            return
        await real_sleep(seconds)

    monkeypatch.setattr(plugin_module, "next_poll_interval", lambda attempt: gate_seconds)
    monkeypatch.setattr(asyncio, "sleep", gated_sleep)

    async def scenario() -> bool:
        await plugin.handle_forward_message(message=forward_message(message_id="90001"))
        # 第一条完成首次查询（未就绪），现在停在闸门上：冷却必然尚未写入。
        await pump_until(lambda: len(plugin.ctx.message.calls) == 1)
        assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, time.time()) is False
        await plugin.handle_forward_message(message=forward_message(message_id="90002"))
        admitted_without_cooldown = not plugin._forward_state.is_group_cooling(
            SOURCE_GROUP, 60, time.time()
        )
        # 第二条立即就绪并完成两个目标群的直发，全程不等第一条的退避。
        await pump_until(lambda: len(send_group_forward_calls(plugin)) == 2)
        poll_gate.set()  # 放行第一条：第二次查询就绪 → 掷骰 → 冷却 → 投递
        await drain_forward_tasks(plugin)
        return admitted_without_cooldown

    admitted_without_cooldown = asyncio.run(scenario())

    assert admitted_without_cooldown is True  # 第二条进门时冷却尚未写入
    assert [call["message_id"] for call in plugin.ctx.message.calls] == ["90001", "90002", "90001"]
    # 两条消息各自独立掷骰并投递两个目标群（后方的是等待窗口内进门的那条）。
    assert [params["group_id"] for params in send_group_forward_calls(plugin)] == [
        int(TARGET_A),
        int(TARGET_B),
        int(TARGET_A),
        int(TARGET_B),
    ]
    assert [call["message_id"] for call in injections(plugin, "forward")] == [
        "90002",
        "90002",
        "90001",
        "90001",
    ]
    assert sorted(call["message_id"] for call in injections(plugin, "forward_action")) == [
        "90001",
        "90002",
    ]
    assert plugin._forward_state.is_group_cooling(SOURCE_GROUP, 60, time.time()) is True

    # 冷却窗口有效：冷却写入后的第三条消息在同步段被压制，不产生任何轮询与投递。
    asyncio.run(plugin.handle_forward_message(message=forward_message(message_id="90003")))
    assert "90003" not in [call["message_id"] for call in plugin.ctx.message.calls]
    assert len(send_group_forward_calls(plugin)) == 4


def test_request_path_skips_only_auto_delivered_targets(tmp_path: Path):
    """两路共用（4.5）：请求路径只跳过自动路径登记过的目标群，未登记的照常投递。

    预置 TARGET_A 的**已投递状态**（自动路径投递成功后按「源聊天流 + 转发源消息」
    键登记的同款写入）；请求转发跳过 A、把 B 投递成功，并在结果里说明被跳过群。
    """
    plugin = make_forward_plugin(tmp_path)
    wire_request_source_message(plugin)
    seed_request_view(plugin)
    plugin._forward_delivered.register(
        build_delivered_key(SOURCE_STREAM, "90001"), [TARGET_A], time.time()
    )

    result = asyncio.run(invoke_request_forward_via_hooks(plugin))

    assert result["success"] is True
    assert result["accepted"] is True
    assert "已跳过自动转发已投递的目标群" in result["content"]
    assert TARGET_A in result["content"]
    assert [call["stream_id"] for call in plugin.ctx.send.forward_calls] == ["stream-20002"]


def test_request_path_stays_usable_when_auto_path_disabled(tmp_path: Path):
    """两路共用（4.5）：`auto_enabled=false` 时自动路径零副作用，请求链路照常走完。"""
    plugin = make_forward_plugin(tmp_path)
    plugin.config.forward.auto_enabled = False
    wire_request_source_message(plugin)
    seed_request_view(plugin)

    async def scenario() -> tuple[int, list[str], dict[str, Any]]:
        await plugin.handle_forward_message(message=forward_message())
        spawned = len(plugin._tasks)
        result = await invoke_request_forward_via_hooks(plugin)
        await drain_forward_tasks(plugin)
        return spawned, [name for name, _ in plugin.protocol_calls], result  # type: ignore[attr-defined]

    spawned, protocol_names, result = asyncio.run(scenario())

    assert spawned == 0  # Hook 整段短路，不 spawn 任何编排
    # 自动路径无任何协议端副作用（请求路径自己的可用性刷新不算）。
    assert "send_group_forward_msg" not in protocol_names
    assert result["success"] is True
    assert [call["stream_id"] for call in plugin.ctx.send.forward_calls] == [
        "stream-20001",
        "stream-20002",
    ]


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
