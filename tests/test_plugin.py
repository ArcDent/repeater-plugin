"""plugin.py 编排层测试：门禁、分流、自我循环防护、协议端交互。"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from typing import Any

# repeats 只经合成包名导入（conftest 未给它建顶层别名，其相对导入决定
# 顶层形态加载不了），与 tests/test_repeats.py 的导入方式一致。
from repeater_plugin.repeats import REPEAT

from config import RepeaterConfig
from plugin import RepeaterPlugin, create_plugin
from reactions import BANDWAGON, OPPOSITE, ReactionEvent

THUMBS_UP = str(ord("👍"))
THUMBS_DOWN = str(ord("👎"))
OTHER_EMOJI = "76"
BOT_QQ = "40004"


class FakeLogger:
    """logging.Logger 接口形态的替身：记录 (level, 文本) 便于断言。

    warning 等方法按标准 logging 签名收 *args/**kwargs——plugin 调用方可能
    传 exc_info=（生产侧是标准 logger，测试侧必须兼容同形签名）。
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        self.records.append((level, message % args if args else str(message)))

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, *args, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, *args, **kwargs)

    def texts(self, level: str) -> list[str]:
        return [message for lvl, message in self.records if lvl == level]


class FakeConfig:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.get_calls = 0

    async def get(self, key: str, default: Any = None) -> Any:
        self.get_calls += 1
        return self.values.get(key, default)


class FakeChat:
    def __init__(self) -> None:
        self.streams: dict[str, dict[str, Any] | None] = {}
        self.queries: list[str] = []

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> Any:
        self.queries.append(str(group_id))
        return self.streams.get(str(group_id))


class FakeMaisakaContext:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False

    async def append(self, stream_id: str, segments: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"stream_id": stream_id, "segments": segments, **kwargs})
        return {"success": not self.fail_next, "index": len(self.calls) - 1}


class FakeMaisaka:
    def __init__(self) -> None:
        self.context = FakeMaisakaContext()


class FakeCtx:
    def __init__(self) -> None:
        self.logger = FakeLogger()
        self.config = FakeConfig()
        self.chat = FakeChat()
        self.maisaka = FakeMaisaka()


class FakeRequest:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._body


def make_plugin(protocol_responses: dict[str, Any] | None = None) -> RepeaterPlugin:
    """构造一个绕过 MaiBot 运行时的插件实例，协议端调用被替换为查表返回。

    所有骰默认拉满（概率与出口权重同口径）：这样「断言不触发」的门禁用例在
    门禁失效时必然失败，而不是靠默认值蒙对。需要验证分流的用例自行覆盖具体值。
    """
    plugin = RepeaterPlugin()
    plugin.config = RepeaterConfig()
    plugin.ctx = FakeCtx()
    plugin.config.protocol.base_url = "http://protocol.test:3000"
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.behavior.delay_min_seconds = 0.0
    plugin.config.behavior.delay_max_seconds = 0.0
    plugin.config.behavior.cooldown_seconds = 0
    plugin.config.opposite.probability = 1.0
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 1.0
    # 复读骰同样定死（权重口径）：复读权重拉满（1000）、打断与沉默归零，
    # 「断言不触发」的门禁用例在门禁失效时必然失败，不靠默认权重蒙对；
    # 复读延迟归零——不归零的话每个复读用例都会真睡 0.3~1.5 秒；冷却归零
    # 避免跨用例牵扯，需冷却的用例自行开启。
    plugin.config.repeat.weight_repeat = 1000
    plugin.config.repeat.weight_interrupt = 0
    plugin.config.repeat.weight_silence = 0
    plugin.config.repeat.delay_min_seconds = 0.0
    plugin.config.repeat.delay_max_seconds = 0.0
    plugin.config.repeat.cooldown_seconds = 0
    # 聚合窗口拉平：注入测试不控制时钟，聚合必须随事件即时 flush 才可断言。
    plugin.config.context.merge_window_seconds = 0

    plugin._runner = None
    plugin._site = None
    plugin._tasks = set()
    plugin._flush_loop_task = None
    plugin._detected_self_qq = ""  # 有效 QQ = 配置值（上面的 BOT_QQ）或启动探测值
    # 主程序配置与聊天流替身：与 event() 默认 group_id="10001" 对齐。
    plugin.ctx.config.values["bot.nickname"] = "麦麦"
    plugin.ctx.chat.streams["10001"] = {
        "stream_id": "stream-10001", "session_id": "stream-10001",
        "group_id": "10001", "is_group_session": True,
    }
    plugin.ctx.chat.streams["20002"] = {
        "stream_id": "stream-20002", "session_id": "stream-20002",
        "group_id": "20002", "is_group_session": True,
    }
    plugin._rebuild_runtime()

    responses = dict(protocol_responses or {})
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(action_name: str, params: dict[str, Any]) -> Any:
        calls.append((action_name, params))
        return responses.get(action_name, {"status": "ok", "retcode": 0, "data": {}})

    plugin._call_protocol = fake_call  # type: ignore[assignment]
    plugin.protocol_calls = calls  # type: ignore[attr-defined]
    return plugin


def event(**overrides: Any) -> ReactionEvent:
    base: dict[str, Any] = {
        "group_id": "10001",
        "message_id": "555",
        "reactor_qq": "20002",
        "emoji_id": THUMBS_UP,
    }
    base.update(overrides)
    return ReactionEvent(**base)


def actions_of(plugin: RepeaterPlugin) -> list[str]:
    return [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]


# ---- 复读检测（chat.receive.before_process observe Hook）----

def group_message(text: str, sender: str, group: str = "10001") -> dict[str, Any]:
    """构造 hook 载荷形态的序列化 SessionMessage（仅含文本段）。"""
    return {
        "is_notify": False,
        "message_info": {
            "group_info": {"group_id": group, "group_name": "某群"},
            "user_info": {"user_id": sender, "user_nickname": "某人"},
        },
        "raw_message": [{"type": "text", "data": text}],
    }


async def drain_spawned(plugin: RepeaterPlugin) -> None:
    """等齐 handler spawn 出的后台任务（发送在任务内完成，不拖真实延迟）。"""
    pending = [task for task in plugin._tasks if not task.done()]
    if pending:
        await asyncio.gather(*pending)


class StepClock:
    """可控时钟：基准取真实 time.time()（TTL 判定铁律），测试内按需步进。"""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def sent_texts(plugin: RepeaterPlugin) -> list[str]:
    """直发通路断言辅助：按调用序取 send_group_msg 的文本段内容。"""
    return [
        params["message"][0]["data"]["text"]
        for name, params in plugin.protocol_calls  # type: ignore[attr-defined]
        if name == "send_group_msg"
    ]


def sent_group_calls(plugin: RepeaterPlugin) -> list[tuple[str, Any]]:
    """直发参数形态断言辅助：(文本, group_id) 对，群号按协议端惯例为 int。"""
    return [
        (params["message"][0]["data"]["text"], params["group_id"])
        for name, params in plugin.protocol_calls  # type: ignore[attr-defined]
        if name == "send_group_msg"
    ]


def test_plugin_disabled_messages_do_not_join_chain():
    """plugin.enabled=false 直接返回不进链：禁用期消息不累积，恢复后从头计数。"""
    plugin = make_plugin()
    plugin.config.plugin.enabled = False

    async def scenario() -> tuple[list[str], list[str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        # 恢复开关后仅一个发送者发言：若禁用期消息进了链，此时计数已满应触发
        plugin.config.plugin.enabled = True
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        mid = sent_texts(plugin)
        await plugin.handle_repeat_message(message=group_message("草", "20005"))
        await plugin.handle_repeat_message(message=group_message("草", "20006"))
        await drain_spawned(plugin)
        return mid, sent_texts(plugin)

    mid, final = asyncio.run(scenario())
    assert mid == []
    assert final == ["草"]


def test_repeat_switch_off_messages_do_not_join_chain():
    """repeat.enabled=false 仅停复读：消息不进链，恢复后从头计数。"""
    plugin = make_plugin()
    plugin.config.repeat.enabled = False

    async def scenario() -> tuple[list[str], list[str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        plugin.config.repeat.enabled = True
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        mid = sent_texts(plugin)
        await plugin.handle_repeat_message(message=group_message("草", "20005"))
        await plugin.handle_repeat_message(message=group_message("草", "20006"))
        await drain_spawned(plugin)
        return mid, sent_texts(plugin)

    mid, final = asyncio.run(scenario())
    assert mid == []
    assert final == ["草"]


def test_repeat_passes_through_when_protocol_base_url_missing():
    """protocol.base_url 未配置：复读整体放行——不拦截、不写冷却、不注入、不进链。

    拦截触发消息是为了防双重发言（ADR 0006），前提是复读发得出去；地址没填时
    拦了只会让触发消息白白消失，所以与 repeat.enabled=false 同口径放行。
    """
    plugin = make_plugin()
    plugin.config.protocol.base_url = ""
    plugin.config.repeat.cooldown_seconds = 120

    async def scenario() -> tuple[list[str], list[str]]:
        actions = []
        for sender in ("20002", "20003", "20004"):
            result = await plugin.handle_repeat_message(message=group_message("草", sender))
            actions.append(result["action"])
        await drain_spawned(plugin)
        # 补上地址后只来一个新发送者：若未配置期间的消息进了链，此时计数已满会触发
        plugin.config.protocol.base_url = "http://protocol.test:3000"
        result = await plugin.handle_repeat_message(message=group_message("草", "20005"))
        actions.append(result["action"])
        await drain_spawned(plugin)
        return actions, sent_texts(plugin)

    actions, texts = asyncio.run(scenario())
    assert actions == ["continue"] * 4
    assert texts == []
    assert plugin.ctx.maisaka.context.calls == []
    assert not plugin._repeat_cooldown.is_cooling_down("10001", 120, time.time())


def test_blacklisted_group_does_not_trigger_repeat():
    plugin = make_plugin()
    plugin.config.scope.group_blacklist = ["10001"]

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == []


def test_ignored_sender_messages_do_not_join_chain():
    """ignore_senders 名内发送者（通常是别的 bot）的消息不进复读链。"""
    plugin = make_plugin()
    plugin.config.scope.ignore_senders = ["20002"]

    async def scenario() -> tuple[list[str], list[str]]:
        await plugin.handle_repeat_message(message=group_message("草", "20002"))
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        await plugin.handle_repeat_message(message=group_message("草", "20004"))
        # 若忽略名单失效：{20002, 20003, 20004} 已达阈值，此处应已发送
        mid = sent_texts(plugin)
        await plugin.handle_repeat_message(message=group_message("草", "20005"))
        await drain_spawned(plugin)
        return mid, sent_texts(plugin)

    mid, final = asyncio.run(scenario())
    assert mid == []
    assert final == ["草"]


def test_third_sender_consumes_qualification_and_rolls():
    """三人接龙第三次调用消耗资格并掷骰；后续同文本发送者不再触发。"""
    plugin = make_plugin()

    async def scenario() -> tuple[int, list[tuple[str, Any]]]:
        await plugin.handle_repeat_message(message=group_message("草", "20002"))
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        after_two = len(sent_texts(plugin))
        await plugin.handle_repeat_message(message=group_message(" 草 ", "20004"))
        await drain_spawned(plugin)
        # 资格已消耗（链终结）：D、E 只续记录
        await plugin.handle_repeat_message(message=group_message("草", "20005"))
        await plugin.handle_repeat_message(message=group_message("草", "20006"))
        await drain_spawned(plugin)
        return after_two, sent_group_calls(plugin)

    after_two, calls = asyncio.run(scenario())
    assert after_two == 0
    assert calls == [(" 草 ", 10001)]


def test_cooldown_suppresses_and_restores_qualification(monkeypatch):
    """冷却期内另一文本达标不消耗资格不掷骰；冷却结束后新发送者加入即触发。

    make_plugin 的默认沉默权重为 0（链达标必出声），因此压制期一并覆盖
    delta spec「沉默权重为零时冷却压制期仍放行触发消息」：被压制的那条
    触发消息 MUST 拿到 continue（不拦截、照常进主程序消息链）——沉默权重
    为 0 时这是触发消息唯一的放行口。
    """
    plugin = make_plugin()
    plugin.config.repeat.cooldown_seconds = 5
    clock = StepClock()
    monkeypatch.setattr("time.time", clock)

    async def scenario() -> tuple[list[str], list[str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 冷却期内（起点 +1s < 5s）：另一文本「哈哈」三人接龙达标 → 压制不发送
        clock.now += 1
        suppressed = []
        for sender in ("20002", "20003", "20004"):
            suppressed.append(
                await plugin.handle_repeat_message(message=group_message("哈哈", sender))
            )
        await drain_spawned(plugin)
        # 冷却结束（起点 +6s ≥ 5s）：新发送者加入「哈哈」链 → 直接取得资格掷骰
        clock.now += 5
        await plugin.handle_repeat_message(message=group_message("哈哈", "20005"))
        await drain_spawned(plugin)
        return sent_texts(plugin), [result["action"] for result in suppressed]

    texts, suppressed_actions = asyncio.run(scenario())
    assert texts == ["草", "哈哈"]
    # 压制期内三条（含使链达标的那条）全部放行，无一被拦截
    assert suppressed_actions == ["continue"] * 3


def test_silence_outlet_does_not_enter_cooldown():
    """沉默出口消耗资格（链终结）但不进入群冷却：另一文本照常可触发。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 0
    plugin.config.repeat.weight_silence = 1000
    plugin.config.repeat.cooldown_seconds = 600

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        # 若沉默也进冷却，「哈哈」链会被压制；权重翻回复读拉满验证它仍能触发
        plugin.config.repeat.weight_repeat = 1000
        plugin.config.repeat.weight_silence = 0
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["哈哈"]


def test_interrupt_outlet_enters_cooldown():
    """打断出口与复读出口同样进入群冷却。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 1000
    plugin.config.repeat.interrupt_texts = ["停！"]
    plugin.config.repeat.cooldown_seconds = 600

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 冷却期内另一文本达标 → 压制，不发送
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["停！"]


def test_send_failure_keeps_cooldown():
    """直发成败不影响已进入的冷却：直发失败后冷却照常压制下一链。"""
    plugin = make_plugin({"send_group_msg": None})
    plugin.config.repeat.cooldown_seconds = 600

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 直发已失败，但冷却已写入：另一文本达标应被压制，不再尝试直发
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]  # 只有一次尝试，失败不重试
    assert any("直发失败" in t for t in plugin.ctx.logger.texts("warning"))


def test_malformed_message_logs_debug_not_warning():
    """形态异常（非 dict / 缺 message_info）打 debug 不打 warning；合法非候选静默。"""
    plugin = make_plugin()

    async def scenario() -> None:
        await plugin.handle_repeat_message(message="not a dict")
        await plugin.handle_repeat_message(message={})
        # 通知消息是合法非候选：不打 debug 也不打 warning
        await plugin.handle_repeat_message(
            message={"is_notify": True, "message_info": {"additional_config": {}}}
        )
        # 私聊同样合法：group_info 为 None
        message = group_message("草", "20002")
        message["message_info"]["group_info"] = None
        await plugin.handle_repeat_message(message=message)

    asyncio.run(scenario())
    assert plugin.ctx.logger.texts("warning") == []
    debugs = [t for t in plugin.ctx.logger.texts("debug") if "形态异常" in t]
    assert len(debugs) == 2


def test_private_chat_message_is_silent():
    plugin = make_plugin()

    async def scenario() -> list[str]:
        message = group_message("草", "20002")
        message["message_info"]["group_info"] = None
        await plugin.handle_repeat_message(message=message)
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == []
    assert plugin.ctx.logger.texts("debug") == []


def test_observe_handler_never_raises():
    """handler 兜底未捕获异常：记 warning，返回 continue 且随附原 kwargs。

    CancelledError 不在兜底之列（照常上抛），由专门用例验证。
    """
    plugin = make_plugin()
    plugin.config.repeat.max_text_length = "garbage"  # 非法配置制造内部异常
    message = group_message("草", "20002")

    result = asyncio.run(plugin.handle_repeat_message(message=message))
    assert result == {"action": "continue", "modified_kwargs": {"message": message}}
    assert any("复读检测处理消息失败" in t for t in plugin.ctx.logger.texts("warning"))


def test_handler_cancelled_error_propagates(monkeypatch):
    """CancelledError 照常上抛：不吞、不记 warning（design D1 兜底的例外）。"""
    plugin = make_plugin()

    def cancelled_processor(message: Any) -> bool:
        raise asyncio.CancelledError()

    monkeypatch.setattr(plugin, "_process_repeat_message", cancelled_processor)

    async def scenario() -> str:
        try:
            await plugin.handle_repeat_message(message=group_message("草", "20002"))
        except asyncio.CancelledError:
            return "propagated"
        return "swallowed"

    assert asyncio.run(scenario()) == "propagated"
    assert plugin.ctx.logger.texts("warning") == []


# ---- blocking 返回值与触发消息拦截（planner 拦截，ADR 0006）----

def test_blocking_continues_while_chain_accumulates():
    """链未达标的消息放行：continue 且随附原 kwargs（blocking 正常形态）。"""
    plugin = make_plugin()
    first_message = group_message("草", "20002")
    second_message = group_message("草", "20003")

    async def scenario() -> list[dict[str, Any]]:
        return [
            await plugin.handle_repeat_message(message=first_message),
            await plugin.handle_repeat_message(message=second_message),
        ]

    first, second = asyncio.run(scenario())
    assert first == {"action": "continue", "modified_kwargs": {"message": first_message}}
    assert second == {"action": "continue", "modified_kwargs": {"message": second_message}}


def test_blocking_continue_on_silence_outlet():
    """沉默出口放行触发消息：continue（沉默权重拉满、复读打断归零——用沉默
    日志证明掷骰确实发生，非门禁失效，也非三权重全 0 的不掷骰分支）。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 0
    plugin.config.repeat.weight_silence = 1000
    messages = [group_message("草", sender) for sender in ("20002", "20003", "20004")]

    async def scenario() -> list[dict[str, Any]]:
        results = []
        for message in messages:
            results.append(await plugin.handle_repeat_message(message=message))
        await drain_spawned(plugin)
        return results

    results = asyncio.run(scenario())
    assert [result["action"] for result in results] == ["continue"] * 3
    assert results[2]["modified_kwargs"] == {"message": messages[2]}
    assert sent_texts(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []  # 沉默出口零注入
    assert any("保持沉默" in t for t in plugin.ctx.logger.texts("info"))


def test_blocking_continue_on_non_candidate():
    """非候选消息（通知/私聊/形态异常）一律 continue 放行。"""
    plugin = make_plugin()
    notify = {"is_notify": True, "message_info": {"additional_config": {}}}
    private = group_message("草", "20002")
    private["message_info"]["group_info"] = None

    async def scenario() -> list[dict[str, Any]]:
        return [
            await plugin.handle_repeat_message(message=notify),
            await plugin.handle_repeat_message(message=private),
            await plugin.handle_repeat_message(message="not a dict"),
        ]

    notify_result, private_result, malformed_result = asyncio.run(scenario())
    assert notify_result == {"action": "continue", "modified_kwargs": {"message": notify}}
    assert private_result == {"action": "continue", "modified_kwargs": {"message": private}}
    assert malformed_result == {"action": "continue", "modified_kwargs": {"message": "not a dict"}}


def test_blocking_continue_when_repeat_disabled():
    """repeat.enabled=false：本可触发复读出口的消息也放行、不拦截不注入。

    context.enabled=true 不改变该结果（delta spec：复读侧开关语义不受
    表情回应域 context.enabled 约束）。
    """
    plugin = make_plugin()
    plugin.config.repeat.enabled = False
    enable_injection(plugin)
    messages = [group_message("草", sender) for sender in ("20002", "20003", "20004")]

    async def scenario() -> list[dict[str, Any]]:
        results = []
        for message in messages:
            results.append(await plugin.handle_repeat_message(message=message))
        await drain_spawned(plugin)
        return results

    results = asyncio.run(scenario())
    assert [result["action"] for result in results] == ["continue"] * 3
    assert results[2]["modified_kwargs"] == {"message": messages[2]}
    assert sent_texts(plugin) == []
    assert plugin.ctx.maisaka.context.calls == []


def test_blocking_abort_on_repeat_outlet():
    """复读出口命中：spawn 后台发送后 abort 触发消息（planner 拦截）。"""
    plugin = make_plugin()
    messages = [group_message("草", sender) for sender in ("20002", "20003", "20004")]

    async def scenario() -> tuple[list[dict[str, Any]], list[str]]:
        results = []
        for message in messages:
            results.append(await plugin.handle_repeat_message(message=message))
        await drain_spawned(plugin)
        return results, sent_texts(plugin)

    results, texts = asyncio.run(scenario())
    assert [result["action"] for result in results] == ["continue", "continue", "abort"]
    assert results[2]["modified_kwargs"] == {"message": messages[2]}
    assert texts == ["草"]


def test_blocking_abort_on_interrupt_outlet():
    """打断出口同样拦截触发消息：abort，后台只发打断文本。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 1000
    plugin.config.repeat.interrupt_texts = ["停！"]
    messages = [group_message("草", sender) for sender in ("20002", "20003", "20004")]

    async def scenario() -> tuple[list[dict[str, Any]], list[str]]:
        results = []
        for message in messages:
            results.append(await plugin.handle_repeat_message(message=message))
        await drain_spawned(plugin)
        return results, sent_texts(plugin)

    results, texts = asyncio.run(scenario())
    assert [result["action"] for result in results] == ["continue", "continue", "abort"]
    assert results[2]["modified_kwargs"] == {"message": messages[2]}
    assert texts == ["停！"]


def test_background_task_receives_trigger_context_and_decision():
    """后台任务收到触发消息打包（群号/发送者/原始文本/message_id）+ 决策共五项。

    原始文本取发送时的样子（含原始空白），不是链键控用的归一产物。
    """
    plugin = make_plugin()
    captured: list[tuple[Any, Any]] = []

    async def capture(candidate: Any, decision: Any) -> None:
        captured.append((candidate, decision))

    plugin._send_repeat_decision = capture  # type: ignore[assignment]

    async def scenario() -> None:
        await plugin.handle_repeat_message(message=group_message("草", "20002"))
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        trigger = group_message(" 草 ", "20004")
        trigger["message_id"] = "msg-42"
        await plugin.handle_repeat_message(message=trigger)
        await drain_spawned(plugin)

    asyncio.run(scenario())
    assert len(captured) == 1
    candidate, decision = captured[0]
    assert candidate.group_id == "10001"
    assert candidate.sender_qq == "20004"
    assert candidate.text == " 草 "
    assert candidate.message_id == "msg-42"
    assert decision.kind == REPEAT
    assert decision.text == " 草 "


def test_concurrent_trigger_boundary_spawns_once():
    """并发到达的两条边界消息只消费一次资格：一 abort 一 continue、单次打包。

    并发双触发防护语义（AGENTS.md 复读域）：record 同步原子段写终结标记
    先于掷骰，同一链上的资格绝无二次授出。
    """
    plugin = make_plugin()
    captured: list[tuple[Any, Any]] = []

    async def capture(candidate: Any, decision: Any) -> None:
        captured.append((candidate, decision))

    plugin._send_repeat_decision = capture  # type: ignore[assignment]

    async def scenario() -> list[dict[str, Any]]:
        await plugin.handle_repeat_message(message=group_message("草", "20002"))
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        results = await asyncio.gather(
            plugin.handle_repeat_message(message=group_message("草", "20004")),
            plugin.handle_repeat_message(message=group_message("草", "20005")),
        )
        await drain_spawned(plugin)
        return results

    results = asyncio.run(scenario())
    assert sorted(result["action"] for result in results) == ["abort", "continue"]
    assert len(captured) == 1
    candidate, decision = captured[0]
    assert candidate.group_id == "10001"
    assert candidate.sender_qq in ("20004", "20005")
    assert decision.kind == REPEAT
    assert decision.text == "草"


# ---- 复读/打断发送编排 ----

def test_repeat_outlet_sends_last_raw_text():
    """复读出口发送链内最后一条的原始文本（含原始空白），非归一产物。"""
    plugin = make_plugin()

    async def scenario() -> list[str]:
        await plugin.handle_repeat_message(message=group_message("草", "20002"))
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        await plugin.handle_repeat_message(message=group_message(" 草 ", "20004"))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == [" 草 "]


def test_interrupt_outlet_sends_single_pool_text_without_repeat_text():
    """打断出口只直发池内一条文本，不附带复读文本。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 1000
    # 单条池子让随机取值确定化
    plugin.config.repeat.interrupt_texts = ["停！"]

    async def scenario() -> list[tuple[str, Any]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_group_calls(plugin)

    assert asyncio.run(scenario()) == [("停！", 10001)]


def test_direct_send_carries_group_id_and_text_segment():
    """send_group_msg 完整参数形态：group_id 数字化、message 为单条 text 段。

    two outlets share one shape: 第 1 链走复读出口（默认权重已拉满），第 2 链权重
    反向拉满走打断出口，整包比对（对齐贴表情侧 test_send_carries_* 惯例）。
    """
    plugin = make_plugin()
    plugin.config.repeat.interrupt_texts = ["停！"]

    async def scenario() -> list[dict[str, Any]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        plugin.config.repeat.weight_repeat = 0
        plugin.config.repeat.weight_interrupt = 1000
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return [
            params
            for name, params in plugin.protocol_calls  # type: ignore[attr-defined]
            if name == "send_group_msg"
        ]

    assert asyncio.run(scenario()) == [
        {"group_id": 10001, "message": [{"type": "text", "data": {"text": "草"}}]},
        {"group_id": 10001, "message": [{"type": "text", "data": {"text": "停！"}}]},
    ]


def test_silence_outlet_sends_nothing():
    """沉默权重拉满、复读打断归零：链达标掷骰后保持沉默，零调用（决策机制
    由后续触发验证活着）。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 0
    plugin.config.repeat.weight_silence = 1000

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 机制活性探针：权重翻回复读拉满后另一文本的链必须能发出，证明前面
        # 的零调用来自沉默出口而非门禁失效
        plugin.config.repeat.weight_repeat = 1000
        plugin.config.repeat.weight_silence = 0
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["哈哈"]


def test_send_waits_repeat_delay_before_sending(monkeypatch):
    """直发经 repeat.delay_min/max 随机延迟后执行，不出现决策即发。"""
    plugin = make_plugin()
    plugin.config.repeat.delay_min_seconds = 1.5
    plugin.config.repeat.delay_max_seconds = 1.5
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        # handler 自身不得延迟（observe 必须快进快出）：此刻延迟尚未发生
        assert sleeps == []
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]
    assert sleeps == [1.5]  # min==max 时区间内随机值唯一


def test_repeat_delay_high_below_low_wait_uses_low(monkeypatch):
    """复读段延迟上限低于下限时按下限等待（_delay 既有归一逻辑保留）。"""
    plugin = make_plugin()
    plugin.config.repeat.delay_min_seconds = 2.0
    plugin.config.repeat.delay_max_seconds = 0.5  # 上限低于下限：归一按 low
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]
    assert sleeps == [2.0]  # uniform(2.0, 2.0) == 2.0


def test_behavior_delay_config_does_not_affect_repeat_delay(monkeypatch):
    """只改 behavior.delay_min/max 不影响复读延迟：直发仍按 repeat 段时长。

    delta spec「复读延迟与表情回应延迟互不牵扯」：behavior 段即使拉到 9 秒，
    复读路径仍读 repeat 段的区间。
    """
    plugin = make_plugin()
    plugin.config.behavior.delay_min_seconds = 9.0
    plugin.config.behavior.delay_max_seconds = 9.0
    plugin.config.repeat.delay_min_seconds = 0.7
    plugin.config.repeat.delay_max_seconds = 0.7
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]
    assert sleeps == [0.7]  # behavior 9 秒不生效，min==max 时随机值唯一


def test_reaction_wait_uses_behavior_delay_range(monkeypatch):
    """表情回应路径的延迟取 behavior.delay_min/max，与复读段互不牵扯。

    复读段延迟即使拉满也不影响表情回应路径（change design D3：延迟按域
    取值）；表情回应侧既有延迟用例仍取 behavior 段。
    """
    plugin = make_plugin()
    plugin.config.repeat.delay_min_seconds = 9.0
    plugin.config.repeat.delay_max_seconds = 9.0
    plugin.config.behavior.delay_min_seconds = 2.0
    plugin.config.behavior.delay_max_seconds = 2.0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(plugin._on_reaction(event()))
    assert "set_msg_emoji_like" in actions_of(plugin)
    assert sleeps == [2.0]  # min==max 时区间内随机值唯一


def test_stream_id_resolved_once_per_group_across_sends():
    """stream_id 复用 _get_stream_id 缓存：同群两出口的注入只查一次聊天流。

    直发只需群号、不依赖 stream（design D2）；触发消息与行为文本两路注入
    （每出口各两次 append）都取 stream，成功缓存保证同群只解析一次。
    """
    plugin = make_plugin()

    async def scenario() -> tuple[list[tuple[str, Any]], list[str], list[str]]:
        for text in ("草", "哈哈"):
            for sender in ("20002", "20003", "20004"):
                await plugin.handle_repeat_message(message=group_message(text, sender))
            await drain_spawned(plugin)
        calls = sent_group_calls(plugin)
        streams = [c["stream_id"] for c in plugin.ctx.maisaka.context.calls]
        return calls, streams, plugin.ctx.chat.queries

    calls, streams, queries = asyncio.run(scenario())
    assert calls == [("草", 10001), ("哈哈", 10001)]
    assert streams == ["stream-10001"] * 4  # 2 出口 × (触发消息 + 行为文本)
    assert queries == ["10001"]


def test_send_failure_result_logged_as_warning():
    """直发返回失败（HTTP 200 + status=failed）：记 warning，止步不重试。"""
    plugin = make_plugin(
        {"send_group_msg": {"status": "failed", "retcode": 1200, "wording": "禁言中"}}
    )

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]  # 只调用一次
    # 止步于触发消息注入：直发失败后无行为文本注入
    assert len(plugin.ctx.maisaka.context.calls) == 1
    warnings = plugin.ctx.logger.texts("warning")
    assert any("直发失败" in t for t in warnings)
    assert any("禁言中" in t for t in warnings)


def test_empty_interrupt_pool_falls_back_with_warning():
    """配置池被清空：回落内置默认池（design D6 五条）并打 warning，不静默哑火。"""
    plugin = make_plugin()
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 1000
    plugin.config.repeat.interrupt_texts = []

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    # 期望值取自 design D6 的默认池字面量（独立来源），不引自实现常量
    texts = asyncio.run(scenario())
    assert len(texts) == 1
    assert texts[0] in ("打断！", "够了！", "停！", "打住！", "别念了！")
    assert texts[0] != "草"  # 不是复读文本
    assert any("打断文本池为空" in t for t in plugin.ctx.logger.texts("warning"))


def test_direct_send_success_choreography_order(monkeypatch):
    """成功路径编舞（ADR 0008 gather 语义）：直发之前注入与延迟都已完成、
    直发之后是行为文本注入。

    触发消息注入与延迟同进 gather，两者先后不再确定，故不钉它们的相对
    顺序；可钉死的边界是：直发（protocol:send_group_msg）之前两者都发生
    过、行为文本注入（第二个 append）在直发之后——source_kind 序
    repeat_trigger → repeat/interrupt 由 await 顺序保证，不会因并发反转。
    """
    plugin = make_plugin(
        {"send_group_msg": {"status": "ok", "retcode": 0, "data": {"message_id": 987001}}}
    )
    plugin.config.repeat.delay_min_seconds = 1.5
    plugin.config.repeat.delay_max_seconds = 1.5
    events: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        events.append("delay")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    original_append = plugin.ctx.maisaka.context.append

    async def recording_append(**kwargs: Any) -> Any:
        events.append("append")
        return await original_append(**kwargs)

    plugin.ctx.maisaka.context.append = recording_append  # type: ignore[method-assign]

    original_call = plugin._call_protocol

    async def recording_call(action_name: str, params: dict[str, Any]) -> Any:
        events.append(f"protocol:{action_name}")
        return await original_call(action_name, params)

    plugin._call_protocol = recording_call  # type: ignore[assignment]

    async def scenario() -> None:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)

    asyncio.run(scenario())
    assert events.count("append") == 2  # 触发消息注入 + 行为文本注入
    assert events.count("delay") == 1
    protocol_pos = events.index("protocol:send_group_msg")
    first_append, second_append = [
        position for position, name in enumerate(events) if name == "append"
    ]
    # 直发在注入与延迟之后（两者同进 gather，相对顺序不钉死）
    assert first_append < protocol_pos
    assert events.index("delay") < protocol_pos
    # 行为文本注入仍在直发之后（历史顺序：先触发消息后行为文本）
    assert protocol_pos < second_append
    assert events[-1] == "append"


def test_direct_send_waits_for_slow_trigger_injection(monkeypatch):
    """注入慢于延迟（ADR 0008 gather 语义）：直发不在延迟到期就提前发出。

    delta spec「注入不叠加在延迟之前」：触发消息注入与延迟并发，延迟（fake
    sleep 即时返回）先到期时直发仍必须等注入完成——可见延迟是两者的较大值
    而非之和。
    """
    plugin = make_plugin(
        {"send_group_msg": {"status": "ok", "retcode": 0, "data": {"message_id": 987001}}}
    )
    plugin.config.repeat.delay_min_seconds = 1.0
    plugin.config.repeat.delay_max_seconds = 1.0
    events: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    # 真 sleep 必须在 patch 前保存：假 sleep 没有 await 点，测试自身轮询若用
    # 被替换的 asyncio.sleep 就不交还事件循环——后台任务在轮询期间根本不被
    # 调度，用例退化成只测 drain 之后的顺序，串行实现也会照常通过（回归防护
    # 空洞，修法见下）。fake_sleep 只管「延迟到期」这个事件本身。
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        events.append("delay")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def slow_append(**kwargs: Any) -> Any:
        if kwargs["source_kind"] == "repeat_trigger":
            # 触发消息注入：挂起直到测试放行（模拟慢注入），期间延迟已可到期。
            events.append("inject-start")
            started.set()
            await release.wait()
            events.append("inject-end")
        return {"success": True, "index": 0}

    plugin.ctx.maisaka.context.append = slow_append  # type: ignore[method-assign]

    original_call = plugin._call_protocol

    async def recording_call(action_name: str, params: dict[str, Any]) -> Any:
        events.append(f"protocol:{action_name}")
        return await original_call(action_name, params)

    plugin._call_protocol = recording_call  # type: ignore[assignment]

    async def scenario() -> None:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        # 轮询一律 sleep(0) 交还事件循环（真 sleep），后台任务才会被调度。
        # 先等注入协程的 started 信号、确认它真的挂起在 release 上；再等延迟
        # 分支先到期——串行实现下延迟排在注入完成之后、此循环必然超时，这就
        # 是「注入与延迟并发」的可观测证据（inject-start 与 delay 的相对先后
        # 是 gather 子任务的调度细节，不构成契约，docstring 同口径）。
        for _ in range(1000):
            if started.is_set():
                break
            await real_sleep(0)
        else:
            raise AssertionError("注入协程未在放行前挂起：并发结构缺失？")
        for _ in range(1000):
            if "delay" in events:
                break
            await real_sleep(0)
        else:
            raise AssertionError("延迟未在注入完成前启动：注入仍串在延迟之前？")
        # 注入已确认挂起、延迟已到期：直发不得提前发生
        assert not [name for name in events if name.startswith("protocol:")]
        release.set()
        await drain_spawned(plugin)

    asyncio.run(scenario())
    # 并发语义可钉死的只有完成边界：延迟先到期并完成、注入随后完成，两者都
    # 在直发之前；inject-start 与 delay 的先后是 gather 子任务的调度细节，
    # 不构成契约。
    assert "inject-start" in events
    assert events.index("delay") < events.index("inject-end")
    assert events.index("inject-end") < events.index("protocol:send_group_msg")
    assert events.index("delay") < events.index("protocol:send_group_msg")
    assert events[-1] == "protocol:send_group_msg"  # 行为文本注入不再记事件


def test_injection_coroutine_exception_keeps_direct_send(monkeypatch):
    """触发消息注入协程抛未预期异常（gather return_exceptions 兜底，
    design D4）：异常项记 warning 后继续，直发照常发生。

    _append_context 自身吞掉常规异常；这里直接替换成抛异常的注入
    协程，专打 gather 的异常兜底——没有 return_exceptions 时异常会上抛、
    外层 except 会把直发一并丢掉。
    """
    plugin = make_plugin()
    original_append_context = plugin._append_context

    async def flaky_injection(
        group_id: str, text: str, source_kind: str, message_id: Any
    ) -> None:
        if source_kind == "repeat_trigger":
            raise RuntimeError("注入协程炸了")
        await original_append_context(group_id, text, source_kind, message_id)

    plugin._append_context = flaky_injection  # type: ignore[assignment]

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]  # 注入异常不阻断直发
    warnings = plugin.ctx.logger.texts("warning")
    assert any("并发协程异常" in t for t in warnings)  # 异常项有显式 warning


def test_direct_send_unreachable_stops_after_trigger_injection():
    """直发不可达（返回 None）：记 warning 止步，行为文本注入不出现（design D2）。"""
    plugin = make_plugin({"send_group_msg": None})

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]  # 只尝试一次直发
    # 止步于触发消息注入：恰好一条 append（触发消息），无行为文本注入
    assert len(plugin.ctx.maisaka.context.calls) == 1
    assert any("直发失败" in t for t in plugin.ctx.logger.texts("warning"))


def test_trigger_injection_stream_missing_still_direct_sends():
    """触发消息注入失败（stream 查不到）记 warning 继续：直发照常执行（design D2）。"""
    plugin = make_plugin()
    plugin.ctx.chat.streams["10001"] = None

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]
    assert plugin.ctx.maisaka.context.calls == []  # 两路注入都因无 stream 跳过
    assert any(
        t.startswith("未写入 Maisaka 上下文:") and "原因=取不到 stream" in t
        for t in plugin.ctx.logger.texts("warning")
    )


def test_append_failure_does_not_block_direct_send_or_intercept():
    """注入失败（append 返回 success=false）不阻断直发、不回滚拦截、不重试。

    delta spec「注入失败不回滚拦截」的主程序返回失败形态（fake 的
    fail_next）：触发消息与行为文本两路注入各试一次即定格，直发照常成功，
    handler 对触发消息照常 abort（拦截在同步段已定，与注入成败解耦）。
    """
    plugin = make_plugin()
    plugin.ctx.maisaka.context.fail_next = True

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        results = []
        for sender in ("20002", "20003", "20004"):
            results.append(await plugin.handle_repeat_message(message=group_message("草", sender)))
        await drain_spawned(plugin)
        return results[2], sent_texts(plugin)

    trigger_result, texts = asyncio.run(scenario())
    assert trigger_result["action"] == "abort"  # 注入失败不回滚拦截
    assert texts == ["草"]  # 注入失败不阻断直发
    calls = plugin.ctx.maisaka.context.calls
    assert [c["source_kind"] for c in calls] == ["repeat_trigger", "repeat"]
    failed = [
        t
        for t in plugin.ctx.logger.texts("warning")
        if t.startswith("未写入 Maisaka 上下文:") and "原因=回执失败" in t
    ]
    assert len(failed) == 2  # 两路注入各失败一次，无重试


def test_append_exception_does_not_block_direct_send_or_intercept():
    """注入抛异常（主程序侧故障形态）同样不阻断直发、不回滚拦截、不重试。"""
    plugin = make_plugin()
    attempted: list[str] = []

    async def boom_append(**kwargs: Any) -> Any:
        attempted.append(str(kwargs["source_kind"]))
        raise RuntimeError("append 内部异常")

    plugin.ctx.maisaka.context.append = boom_append  # type: ignore[method-assign]

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        results = []
        for sender in ("20002", "20003", "20004"):
            results.append(await plugin.handle_repeat_message(message=group_message("草", sender)))
        await drain_spawned(plugin)
        return results[2], sent_texts(plugin)

    trigger_result, texts = asyncio.run(scenario())
    assert trigger_result["action"] == "abort"
    assert texts == ["草"]
    assert attempted == ["repeat_trigger", "repeat"]  # 各试一次，次数定格
    failed = [
        t
        for t in plugin.ctx.logger.texts("warning")
        if t.startswith("未写入 Maisaka 上下文:") and "原因=调用异常 append 内部异常" in t
    ]
    assert len(failed) == 2


def test_repeat_outlet_injection_payloads():
    """复读出口双注入载荷（design D3）：source_kind / message_id / 文案。"""
    plugin = make_plugin(
        {"send_group_msg": {"status": "ok", "retcode": 0, "data": {"message_id": 987001}}}
    )
    trigger = group_message(" 草 ", "20004")
    trigger["message_id"] = "msg-42"
    trigger["message_info"]["user_info"]["user_nickname"] = "触发者"

    async def scenario() -> None:
        await plugin.handle_repeat_message(message=group_message("草", "20002"))
        await plugin.handle_repeat_message(message=group_message("草", "20003"))
        await plugin.handle_repeat_message(message=trigger)
        await drain_spawned(plugin)

    asyncio.run(scenario())
    calls = plugin.ctx.maisaka.context.calls
    assert [c["source_kind"] for c in calls] == ["repeat_trigger", "repeat"]
    trigger_call, behavior_call = calls
    # 触发消息注入：附触发消息自身 ID，呈现发送者显示名与原文（原始空白保留）
    assert trigger_call["message_id"] == "msg-42"
    assert trigger_call["stream_id"] == "stream-10001"
    assert "触发者" in trigger_call["visible_text"]
    assert " 草 " in trigger_call["visible_text"]
    assert trigger_call["segments"] == [{"type": "text", "data": trigger_call["visible_text"]}]
    # 行为文本注入：附直发响应 data.message_id，呈现复读文本且可区分行为类型
    assert behavior_call["message_id"] == 987001
    assert "[复读]" in behavior_call["visible_text"]
    assert " 草 " in behavior_call["visible_text"]


def test_interrupt_outlet_injection_payloads():
    """打断出口的行为文本注入：source_kind=interrupt、只呈现打断文本。"""
    plugin = make_plugin(
        {"send_group_msg": {"status": "ok", "retcode": 0, "data": {"message_id": 987002}}}
    )
    plugin.config.repeat.weight_repeat = 0
    plugin.config.repeat.weight_interrupt = 1000
    plugin.config.repeat.interrupt_texts = ["停！"]
    trigger = group_message("草", "20004")
    trigger["message_id"] = "msg-43"

    async def scenario() -> None:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)

    asyncio.run(scenario())
    calls = plugin.ctx.maisaka.context.calls
    assert [c["source_kind"] for c in calls] == ["repeat_trigger", "interrupt"]
    behavior_call = calls[1]
    assert behavior_call["message_id"] == 987002
    assert "[打断]" in behavior_call["visible_text"]
    assert "停！" in behavior_call["visible_text"]
    assert "草" not in behavior_call["visible_text"]  # 绝不附带复读文本


# ---- 组件声明（Host 组件注册表据此登记并派发 Hook）----

def test_components_are_exactly_the_two_before_process_hooks():
    """组件面恰为两个 blocking Hook：通知 guard 在前（early）、复读检测在后（late）。

    guard 先观察会话并 abort 表情回应通知，复读检测随后才看到其余消息（ADR
    0003/0006）；元数据里的 handler_name 必须指向真实可调用的方法，Host 按它派发。
    """
    plugin = create_plugin()
    components = {component["name"]: component for component in plugin.get_components()}

    assert set(components) == {"emoji_reaction_notify_guard", "repeat_detector"}
    for component in components.values():
        metadata = component["metadata"]
        assert component["type"] == "HOOK_HANDLER"
        assert metadata["hook"] == "chat.receive.before_process"
        assert metadata["mode"] == "blocking"
        assert callable(getattr(plugin, metadata["handler_name"], None))
    assert components["emoji_reaction_notify_guard"]["metadata"]["order"] == "early"
    assert components["repeat_detector"]["metadata"]["order"] == "late"


# ---- Manifest（repeat-and-rebrand 改名与新 capability 声明）----

MANIFEST_JSON = Path(__file__).resolve().parent.parent / "_manifest.json"
PLUGIN_PY = Path(__file__).resolve().parent.parent / "plugin.py"
# 能力消费面按生产源码整包统计：转发域退役后运行包只剩 plugin.py 一个模块，
# 声明漏一个就与 manifest 双向比对失败（Host 授权层缺声明一律拒绝，2026-09-05
# 事故的同类漂移）。
PLUGIN_SOURCES = [PLUGIN_PY]

# ctx.logger rides the logging channel, not the capability authorization layer.
_NON_CAPABILITY_PROXIES = {"logger"}
# plugin.py 用 self.ctx；`_ctx` 是包内历史上用过的 ctx 别名，一并计入扫描面，
# 免得将来引入别名后这条漂移检查悄悄漏计。
_CTX_ACCESSORS = {"ctx", "_ctx"}


def _ctx_call_capabilities(source: str) -> set[str]:
    """Collect `self.<ctx|_ctx>...<method>()` call sites as capability names.

    三段式直调（`self.ctx.call_host_method(...)` 这类 Host 受限原始通道）同样
    计入：它不在 manifest 里，一出现就让双向比对判红，不能绕过能力声明。
    """
    tree = ast.parse(source)
    used: set[str] = set()

    def dotted(node: ast.AST) -> list[str] | None:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            if base is not None:
                return base + [node.attr]
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts = dotted(node.func)
        if parts is None or len(parts) < 3 or parts[0] != "self":
            continue
        if parts[1] not in _CTX_ACCESSORS or parts[2] in _NON_CAPABILITY_PROXIES:
            continue
        used.add(".".join(parts[2:]))
    return used


def _hook_capabilities(source: str) -> set[str]:
    """Collect `@HookHandler("<hook>", ...)` subscriptions as capability names."""
    tree = ast.parse(source)
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            if not (isinstance(decorator.func, ast.Name) and decorator.func.id == "HookHandler"):
                continue
            hook = decorator.args[0]
            if isinstance(hook, ast.Constant) and isinstance(hook.value, str):
                used.add(hook.value)
    return used


def _plugin_capabilities() -> set[str]:
    """整包消费的 capability 调用与 Hook 订阅集合。"""
    consumed: set[str] = set()
    for path in PLUGIN_SOURCES:
        source = path.read_text(encoding="utf-8")
        consumed |= _ctx_call_capabilities(source) | _hook_capabilities(source)
    return consumed


def test_manifest_identity():
    """id/name/description 覆盖双行为；仓库改名是用户手动步骤，URL 本组不动。"""
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)  # JSON 语法合法由 load 本身保证
    assert manifest["id"] == "arcdent.repeater-plugin"
    assert manifest["name"] == "复读插件"
    description = manifest["description"]
    assert "复读" in description and "打断" in description  # 复读 + 打断
    assert "表情" in description  # 表情回应跟风/反义
    assert manifest["urls"]["repository"] == "https://github.com/arcdent/repeater-plugin"


def test_manifest_declares_every_consumed_capability():
    """manifest capabilities 必须与代码实际消费的集合一致。

    2026-09-05 线上事故：复读发送走 ctx.send.text 但 manifest 未声明，
    Host 授权层逐次以 E_CAPABILITY_DENIED 拒绝；当时本文件用硬编码等值
    断言，清单跟着代码一起漏。改为从生产源码整包（退役转发域后即
    plugin.py）派生消费集合再比对，漏声明与死声明双向都拦。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    consumed = _plugin_capabilities()
    declared = set(manifest["capabilities"])
    assert declared == consumed, (
        f"manifest capabilities 与代码消费漂移: 缺 {sorted(consumed - declared)},"
        f" 多余 {sorted(declared - consumed)}"
    )


# ---- 复读与表情回应的边界（两域独立）----

def test_repeat_cooldown_does_not_affect_reaction_cooldown():
    """刚复读完（复读冷却已进入），表情上报照常走表情自身冷却判定。"""
    plugin = make_plugin()
    plugin.config.behavior.cooldown_seconds = 600  # 表情群冷却开启
    plugin.config.repeat.cooldown_seconds = 600  # 复读群冷却开启

    async def scenario() -> tuple[list[str], list[str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 复读冷却期内表情上报到来：表情冷却独立计时，不受复读冷却影响
        await plugin._on_reaction(event())
        return sent_texts(plugin), actions_of(plugin)

    texts, actions = asyncio.run(scenario())
    assert texts == ["草"]
    assert "set_msg_emoji_like" in actions


def test_repeat_disabled_bandwagon_still_fires():
    """repeat.enabled=false 仅停复读，表情跟风照常触发、零复读行为。"""
    plugin = make_plugin()
    plugin.config.repeat.enabled = False

    async def scenario() -> tuple[list[str], list[str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.handle_repeat_message(message=group_message("草", sender))
        await drain_spawned(plugin)
        await plugin._on_reaction(event())
        return sent_texts(plugin), actions_of(plugin)

    texts, actions = asyncio.run(scenario())
    assert texts == []
    assert "set_msg_emoji_like" in actions


# ---- 门禁 ----

def test_disabled_plugin_does_nothing():
    plugin = make_plugin()
    plugin.config.plugin.enabled = False
    assert asyncio.run(plugin._evaluate(event())) is None


def test_removed_reaction_does_nothing():
    plugin = make_plugin()
    assert asyncio.run(plugin._evaluate(event(removed=True))) is None


def test_blacklisted_group_is_skipped():
    plugin = make_plugin()
    plugin.config.scope.group_blacklist = ["10001"]
    assert asyncio.run(plugin._evaluate(event())) is None


def test_whitelist_excludes_other_groups():
    plugin = make_plugin()
    plugin.config.scope.group_whitelist = ["99999"]
    assert asyncio.run(plugin._evaluate(event())) is None


def test_ignored_reactor_is_skipped():
    plugin = make_plugin()
    plugin.config.scope.ignore_reactors = ["20002"]
    assert asyncio.run(plugin._evaluate(event())) is None


def test_own_reaction_is_skipped():
    plugin = make_plugin()
    plugin.config.protocol.self_qq = BOT_QQ
    assert asyncio.run(plugin._evaluate(event(reactor_qq=BOT_QQ))) is None


def test_taken_slot_is_skipped():
    plugin = make_plugin()
    # 必须用当前时间：传 0.0 会被 TTL 判成过期，槽位形同虚设，用例就测不到东西。
    plugin._state.take_slot("555", now=time.time())
    assert asyncio.run(plugin._evaluate(event())) is None


def test_cooldown_blocks_second_reaction_in_group():
    plugin = make_plugin()
    plugin.config.behavior.cooldown_seconds = 600
    plugin.config.opposite.probability = 1.0
    asyncio.run(plugin._on_reaction(event(message_id="555")))
    assert "set_msg_emoji_like" in actions_of(plugin)

    plugin.protocol_calls.clear()  # type: ignore[attr-defined]
    assert asyncio.run(plugin._evaluate(event(message_id="666"))) is None


# ---- 分流 ----

def test_opposite_branch_queries_message_author():
    """两级决策下反义表情也要过回应骰，回应骰需要知道原作者是谁。"""
    plugin = make_plugin()
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.opposite.probability = 1.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=THUMBS_UP)))
    assert decision is not None
    assert decision.kind == OPPOSITE
    assert decision.emoji_id == THUMBS_DOWN
    assert "get_msg" in actions_of(plugin)


def test_bandwagon_on_other_message_uses_main_probability():
    plugin = make_plugin({"get_msg": {"status": "ok", "data": {"sender": {"user_id": "30003"}}}})
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 0.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI)))
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == OTHER_EMOJI
    assert "get_msg" in actions_of(plugin)


def test_bandwagon_on_own_message_uses_self_probability():
    plugin = make_plugin({"get_msg": {"status": "ok", "data": {"sender": {"user_id": BOT_QQ}}}})
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 0.0

    assert asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI))) is None


def test_unknown_author_degrades_to_other_message():
    plugin = make_plugin({"get_msg": {"status": "failed", "retcode": 1404}})
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 0.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI)))
    assert decision is not None and decision.kind == BANDWAGON


def test_author_lookup_skipped_when_self_qq_unknown():
    plugin = make_plugin()
    plugin.config.protocol.self_qq = ""
    plugin.config.bandwagon.probability = 1.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI)))
    assert decision is not None and decision.kind == BANDWAGON
    assert "get_msg" not in actions_of(plugin)


def test_author_is_cached_across_reports():
    plugin = make_plugin({"get_msg": {"status": "ok", "data": {"sender": {"user_id": "30003"}}}})
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 0.0  # 不触发，槽位保持空闲

    asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI, reactor_qq="20002")))
    asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI, reactor_qq="20003")))
    assert actions_of(plugin).count("get_msg") == 1


def test_disabled_opposite_falls_back_to_bandwagon():
    plugin = make_plugin()
    plugin.config.opposite.enabled = False
    plugin._rebuild_runtime()
    plugin.config.protocol.self_qq = ""
    plugin.config.bandwagon.probability = 1.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=THUMBS_UP)))
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == THUMBS_UP


def test_custom_pair_overrides_default_map():
    plugin = make_plugin()
    plugin.config.opposite.extra_pairs = ["👍=🤢"]
    plugin.config.opposite.probability = 1.0
    plugin._rebuild_runtime()

    decision = asyncio.run(plugin._evaluate(event(emoji_id=THUMBS_UP)))
    assert decision is not None
    assert decision.emoji_id == str(ord("🤢"))


# ---- 执行与自我循环防护 ----

def test_send_carries_set_flag_and_numeric_message_id():
    plugin = make_plugin()
    plugin.config.opposite.probability = 1.0
    asyncio.run(plugin._on_reaction(event()))

    sends = [params for name, params in plugin.protocol_calls if name == "set_msg_emoji_like"]  # type: ignore[attr-defined]
    assert len(sends) == 1
    assert sends[0] == {"message_id": 555, "emoji_id": THUMBS_DOWN, "set": True}


def test_slot_breaks_self_reaction_loop():
    """麦麦贴完后，同一条消息的后续上报（含麦麦自己那次）一律不再处理。"""
    plugin = make_plugin()
    plugin.config.protocol.self_qq = ""  # 认不出自己，只靠表情槽断环
    plugin.config.opposite.probability = 1.0

    asyncio.run(plugin._on_reaction(event(reactor_qq="20002")))
    assert actions_of(plugin).count("set_msg_emoji_like") == 1

    asyncio.run(plugin._on_reaction(event(reactor_qq=BOT_QQ)))
    asyncio.run(plugin._on_reaction(event(reactor_qq="20003")))
    assert actions_of(plugin).count("set_msg_emoji_like") == 1


def _slow_author_lookup(plugin: RepeaterPlugin, author: str = "30003") -> None:
    """让 get_msg 真的挂起一拍（模拟协议端 RTT），暴露查原作者期间的并发窗口。"""
    calls = plugin.protocol_calls  # type: ignore[attr-defined]

    async def slow_call(action_name: str, params: dict[str, Any]) -> Any:
        calls.append((action_name, params))
        if action_name == "get_msg":
            await asyncio.sleep(0)
            return {"status": "ok", "retcode": 0, "data": {"sender": {"user_id": author}}}
        return {"status": "ok", "retcode": 0, "data": {}}

    plugin._call_protocol = slow_call  # type: ignore[assignment]


def test_concurrent_reports_on_same_message_react_once():
    """同一条消息的两条上报并发到达：查原作者（get_msg）期间第二条也过了预检，
    占槽前的同步复查必须挡下它——一条被回应消息至多一个表情槽（CONTEXT.md）。"""
    plugin = make_plugin()
    _slow_author_lookup(plugin)

    async def scenario() -> None:
        await asyncio.gather(
            plugin._on_reaction(event(message_id="777", reactor_qq="20002")),
            plugin._on_reaction(event(message_id="777", reactor_qq="20003")),
        )

    asyncio.run(scenario())
    assert actions_of(plugin).count("get_msg") == 2  # 两条都过了预检：并发窗口真实存在
    assert actions_of(plugin).count("set_msg_emoji_like") == 1


def test_concurrent_reports_in_cooling_group_react_once():
    """同群两条消息的上报并发到达且群冷却开启：冷却期内只贴出一个表情回应。"""
    plugin = make_plugin()
    plugin.config.behavior.cooldown_seconds = 600
    _slow_author_lookup(plugin)

    async def scenario() -> None:
        await asyncio.gather(
            plugin._on_reaction(event(message_id="801")),
            plugin._on_reaction(event(message_id="802")),
        )

    asyncio.run(scenario())
    assert actions_of(plugin).count("get_msg") == 2
    assert actions_of(plugin).count("set_msg_emoji_like") == 1


def test_missing_action_disables_further_attempts():
    plugin = make_plugin({"set_msg_emoji_like": {"status": "failed", "retcode": 1404}})
    plugin.config.opposite.probability = 1.0

    asyncio.run(plugin._on_reaction(event(message_id="555")))
    assert plugin._emoji_action_supported is False
    assert any("1404" in text for text in plugin.ctx.logger.texts("error"))

    assert asyncio.run(plugin._evaluate(event(message_id="666"))) is None


def test_rejected_send_is_logged_as_warning():
    plugin = make_plugin({"set_msg_emoji_like": {"status": "failed", "retcode": 1200, "wording": "禁言中"}})
    plugin.config.opposite.probability = 1.0

    asyncio.run(plugin._on_reaction(event()))
    assert any("禁言中" in text for text in plugin.ctx.logger.texts("warning"))
    assert plugin._emoji_action_supported is True


def test_unreachable_protocol_is_logged():
    plugin = make_plugin({"set_msg_emoji_like": None})
    plugin.config.opposite.probability = 1.0

    asyncio.run(plugin._on_reaction(event()))
    assert any("协议端不可达" in text for text in plugin.ctx.logger.texts("warning"))


# ---- Webhook ----

def _post(plugin: RepeaterPlugin, payload: Any, headers: dict[str, str] | None = None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    spawned: list[Any] = []

    def fake_spawn(coro: Any) -> None:
        spawned.append(coro)
        coro.close()

    plugin._spawn = fake_spawn  # type: ignore[assignment]
    response = asyncio.run(plugin._handle_webhook(FakeRequest(body, headers)))
    return response, spawned


def test_webhook_dispatches_known_reaction_report():
    plugin = make_plugin()
    response, spawned = _post(plugin, {
        "post_type": "notice", "notice_type": "group_msg_emoji_like",
        "group_id": 10001, "user_id": 20002, "message_id": 555,
        "likes": [{"emoji_id": "76", "count": 1}],
    })
    assert response.status == 200
    assert len(spawned) == 1


def test_webhook_warns_on_unknown_reaction_shape():
    plugin = make_plugin()
    response, spawned = _post(plugin, {
        "post_type": "notice", "notice_type": "group_msg_emoji_like", "group_id": 10001,
    })
    assert response.status == 200
    assert spawned == []
    warnings = plugin.ctx.logger.texts("warning")
    assert any("形态不认识" in text for text in warnings)
    assert any("group_msg_emoji_like" in text for text in warnings)


def test_webhook_ignores_unrelated_events_silently():
    plugin = make_plugin()
    response, spawned = _post(plugin, {"post_type": "notice", "notice_type": "group_increase"})
    assert response.status == 200
    assert spawned == []
    assert plugin.ctx.logger.texts("warning") == []


def test_webhook_ignores_private_reaction_without_warning():
    """私聊表情回应不在插件范围内，不该被当成「形态未知」污染告警日志。"""
    plugin = make_plugin()
    response, spawned = _post(plugin, {
        "post_type": "notice", "notice_type": "friend_msg_emoji_like",
        "user_id": 20002, "message_id": 555, "likes": [{"emoji_id": "76"}],
    })
    assert response.status == 200
    assert spawned == []
    assert plugin.ctx.logger.texts("warning") == []
    assert any("非群聊" in text for text in plugin.ctx.logger.texts("debug"))


def test_webhook_rejects_invalid_json():
    plugin = make_plugin()
    response, spawned = _post(plugin, b"{not json")
    assert response.status == 400
    assert spawned == []


def test_webhook_rejects_non_object_payload():
    plugin = make_plugin()
    response, _ = _post(plugin, [1, 2, 3])
    assert response.status == 400


def test_webhook_signature_is_enforced_when_secret_set():
    import hmac
    from hashlib import sha1

    plugin = make_plugin()
    plugin.config.webhook.secret = "s3cret"
    payload = {"post_type": "notice", "notice_type": "group_increase"}
    body = json.dumps(payload).encode("utf-8")

    response, _ = _post(plugin, body, headers={"X-Signature": "sha1=deadbeef"})
    assert response.status == 401

    good = "sha1=" + hmac.new(b"s3cret", body, sha1).hexdigest()
    response, _ = _post(plugin, body, headers={"X-Signature": good})
    assert response.status == 200


# ---- 自检与配置热更新 ----

def test_startup_checks_fill_self_qq():
    plugin = make_plugin({"get_login_info": {"status": "ok", "data": {"user_id": BOT_QQ}}})
    plugin.config.protocol.self_qq = ""
    asyncio.run(plugin._startup_checks())
    assert plugin._self_qq == BOT_QQ


def test_startup_checks_warn_when_self_qq_unavailable():
    plugin = make_plugin({"get_login_info": {"status": "ok", "data": {}}})
    plugin.config.protocol.self_qq = ""
    asyncio.run(plugin._startup_checks())
    assert any("取不到麦麦自己的 QQ 号" in text for text in plugin.ctx.logger.texts("warning"))


def test_startup_checks_warn_without_base_url():
    plugin = make_plugin()
    plugin.config.protocol.base_url = ""
    asyncio.run(plugin._startup_checks())
    assert any("base_url 未配置" in text for text in plugin.ctx.logger.texts("warning"))
    assert actions_of(plugin) == []


def test_config_update_ignores_other_scopes():
    plugin = make_plugin()
    plugin.config.opposite.enabled = False
    asyncio.run(plugin.on_config_update("bot", {}, "1"))
    # 未重建，反义映射仍是加载时那份
    assert plugin._opposite_map != {}


def test_config_update_rebuilds_opposite_map():
    plugin = make_plugin()
    plugin.config.plugin.enabled = False  # 避免真的去起 webhook
    plugin.config.opposite.enabled = False
    asyncio.run(plugin.on_config_update("self", {}, "1"))
    assert plugin._opposite_map == {}


def test_reload_with_cleared_self_qq_falls_back_to_detected():
    """配置的 self_qq 清空后热重载：有效 QQ 回到启动自检探测到的值，不再沿用旧配置。"""
    plugin = make_plugin({"get_login_info": {"status": "ok", "data": {"user_id": 50005}}})
    plugin.config.protocol.self_qq = "11111"
    plugin._rebuild_runtime()
    asyncio.run(plugin._startup_checks())
    assert plugin._self_qq == "11111"  # 配置值优先于探测值

    plugin.config.protocol.self_qq = ""
    plugin._rebuild_runtime()  # 配置热重载的运行时重建
    assert plugin._self_qq == "50005"


def test_reload_resets_missing_action_flag():
    """1404 停用贴表情后热重载：重建运行时复位停用标记，下一次上报重新判定。

    部署者为此换了协议端地址并热重载时，不用重启就能恢复；协议端确实不支持时，
    重载后第一次贴表情会再失败一次、再记一条 error。
    """
    plugin = make_plugin({"set_msg_emoji_like": {"status": "failed", "retcode": 1404}})
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin._emoji_action_supported is False

    plugin._rebuild_runtime()  # 配置热重载的运行时重建
    asyncio.run(plugin._on_reaction(event(message_id="1002")))
    assert actions_of(plugin).count("set_msg_emoji_like") == 2


def test_on_unload_cancels_pending_tasks():
    plugin = make_plugin()

    async def scenario() -> bool:
        async def sleeper() -> None:
            await asyncio.sleep(10)

        plugin._spawn(sleeper())
        task = next(iter(plugin._tasks))
        await plugin.on_unload()
        await asyncio.sleep(0)
        return task.cancelled()

    assert asyncio.run(scenario()) is True


# ---- 上下文注入（实验性）----

def enable_injection(plugin: RepeaterPlugin) -> None:
    plugin.config.context.enabled = True
    plugin._rebuild_runtime()


def test_injection_disabled_by_default_no_append():
    plugin = make_plugin()
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_enabled_appends_visible_text():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    call = plugin.ctx.maisaka.context.calls[0]
    assert call["stream_id"] == "stream-10001"
    assert call["segments"] == [{"type": "text", "data": call["visible_text"]}]
    assert call["source_kind"] == "reaction"
    assert call["message_id"] == "1001"
    assert "20002" in call["visible_text"]
    assert "👍" in call["visible_text"]


def test_injection_dedupes_same_key():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1


def test_injection_merges_same_emoji_same_message(monkeypatch):
    """聚合窗口内同消息同表情的多人回应合并为一条注入。

    make_plugin 默认窗口为 0（即时 flush），本用例显式拉到 30s 并外拨时钟
    越过窗口，让两条回应汇入同一聚合后一次性写入。
    """
    plugin = make_plugin()
    plugin.config.context.merge_window_seconds = 30
    enable_injection(plugin)

    class FakeClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    monkeypatch.setattr("time.time", clock)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    clock.now = 1031.0  # 越过 30s 聚合窗口
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq="20003")))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    text = plugin.ctx.maisaka.context.calls[0]["visible_text"]
    assert "20002" in text and "20003" in text


def test_injection_self_reaction_uses_bot_display_name():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq=BOT_QQ)))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    text = plugin.ctx.maisaka.context.calls[0]["visible_text"]
    assert "麦麦" in text


def test_injection_display_name_falls_back_to_config():
    plugin = make_plugin()
    plugin.ctx.config.values["bot.nickname"] = ""
    plugin.config.context.self_display_name = "麦麦酱"
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq=BOT_QQ)))
    assert "麦麦酱" in plugin.ctx.maisaka.context.calls[0]["visible_text"]


def test_injection_no_display_name_omits_name():
    plugin = make_plugin()
    plugin.ctx.config.values["bot.nickname"] = ""
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq=BOT_QQ)))
    text = plugin.ctx.maisaka.context.calls[0]["visible_text"]
    assert text.startswith("[表情回应] 给某条消息")


def test_injection_blacklisted_group_skipped():
    plugin = make_plugin()
    plugin.config.scope.group_blacklist = ["10001"]
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_ignored_reactor_skipped_but_self_injected():
    plugin = make_plugin()
    plugin.config.scope.ignore_reactors = ["20002"]
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []
    asyncio.run(plugin._on_reaction(event(message_id="1002", reactor_qq=BOT_QQ)))
    assert len(plugin.ctx.maisaka.context.calls) == 1


def test_injection_unknown_stream_skipped():
    """取不到 stream：不写入，记 warning「未写入 … 原因=取不到 stream」，字段序同成功日志。"""
    plugin = make_plugin()
    plugin.ctx.chat.streams["10001"] = None
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []
    expected = (
        "未写入 Maisaka 上下文: kind=reaction text='[表情回应] 20002 给某条消息(id=1001)贴了 👍', "
        "group=10001, msg=1001, emoji=128077, responders=1, 原因=取不到 stream"
    )
    assert plugin.ctx.logger.texts("warning") == [expected]


def test_injection_append_failure_logs_warning():
    """回执失败：记 warning「未写入 … 原因=回执失败 …」，已取到的 stream 附在末尾。"""
    plugin = make_plugin()
    enable_injection(plugin)
    plugin.ctx.maisaka.context.fail_next = True
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    expected = (
        "未写入 Maisaka 上下文: kind=reaction text='[表情回应] 20002 给某条消息(id=1001)贴了 👍', "
        "group=10001, msg=1001, emoji=128077, responders=1, "
        "原因=回执失败 {'success': False, 'index': 0}, stream=stream-10001"
    )
    assert plugin.ctx.logger.texts("warning") == [expected]


def test_injection_removed_reaction_not_injected():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", removed=True)))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_flushes_aggregate_to_its_own_group(monkeypatch):
    """A 群聚合未到期时由 B 群事件触发 flush——聚合注入 A 群 stream，不串群。"""
    plugin = make_plugin()
    plugin.config.context.merge_window_seconds = 30
    enable_injection(plugin)

    class FakeClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    monkeypatch.setattr("time.time", clock)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))  # A 群聚合开启，未到期
    assert plugin.ctx.maisaka.context.calls == []
    clock.now = 1031.0  # 越过窗口，B 群事件到来触发 flush
    asyncio.run(plugin._on_reaction(event(group_id="20002", message_id="2001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    call = plugin.ctx.maisaka.context.calls[0]
    assert call["stream_id"] == "stream-10001"
    assert "1001" in call["visible_text"]


def test_injection_exception_does_not_block_behavior_path(monkeypatch):
    """注入路径抛未预期异常时，记 warning 后行为路径照常执行。"""
    plugin = make_plugin()
    enable_injection(plugin)

    async def boom(event):
        raise RuntimeError("注入内部异常")

    monkeypatch.setattr(plugin, "_inject_reaction", boom)
    asyncio.run(plugin._on_reaction(event()))
    assert "set_msg_emoji_like" in actions_of(plugin)
    assert any("注入表情回应上下文异常" in t for t in plugin.ctx.logger.texts("warning"))


def test_injection_stream_failure_not_cached_and_recovers():
    """A failed stream lookup must not cache the empty result (design D6).

    The main program resolves group sessions lazily, so the lookup can fail
    for a group that has one moments later: the next report has to retry
    the query and inject, which also proves the failed flush rolled its
    dedup keys back.
    """
    plugin = make_plugin()
    plugin.ctx.chat.streams["10001"] = None
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []
    assert "10001" not in plugin._stream_ids

    # The session shows up later; the same report combination re-aggregates.
    plugin.ctx.chat.streams["10001"] = {
        "stream_id": "stream-10001", "session_id": "stream-10001",
        "group_id": "10001", "is_group_session": True,
    }
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    # Successful lookups keep being cached as before.
    assert plugin._stream_ids == {"10001": "stream-10001"}


def test_injection_display_name_resolved_once_until_rebuild():
    """bot.nickname is resolved once per runtime rebuild (design D6).

    An empty nickname must not trigger a config.get on every injection;
    _rebuild_runtime (the config-reload path) resets it to unresolved so the
    next injection resolves the display name again.
    """
    plugin = make_plugin()
    plugin.ctx.config.values["bot.nickname"] = ""
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    asyncio.run(plugin._on_reaction(event(message_id="1002")))
    assert plugin.ctx.config.get_calls == 1

    plugin._rebuild_runtime()  # config reload resets the one-shot resolution
    asyncio.run(plugin._on_reaction(event(message_id="1003")))
    assert plugin.ctx.config.get_calls == 2


def test_flush_loop_spawned_when_injection_enabled_and_cancelled_on_unload():
    """The resident flush loop (design D2) is spawned while injection is on
    and is cancelled together with every other task on unload."""
    plugin = make_plugin()

    async def scenario() -> bool:
        async def no_webhook() -> None:
            return None

        plugin._start_webhook = no_webhook  # type: ignore[assignment]
        plugin._stop_webhook = no_webhook  # type: ignore[assignment]
        plugin.config.context.enabled = True
        await plugin.on_config_update("self", {}, "1")
        task = plugin._flush_loop_task
        assert task is not None and not task.done()
        assert task in plugin._tasks
        await plugin.on_unload()
        await asyncio.sleep(0)
        return task.cancelled()

    assert asyncio.run(scenario()) is True


def test_flush_loop_exits_when_injection_turned_off(monkeypatch):
    """Turning injection off lets the loop exit on its own at the next tick
    (self._injection_state is None), without an explicit cancel."""
    plugin = make_plugin()

    async def scenario() -> bool:
        async def no_webhook() -> None:
            return None

        plugin._start_webhook = no_webhook  # type: ignore[assignment]
        plugin._stop_webhook = no_webhook  # type: ignore[assignment]
        plugin.config.context.enabled = True
        await plugin.on_config_update("self", {}, "1")
        task = plugin._flush_loop_task
        assert task is not None and not task.done()
        plugin.config.context.enabled = False
        await plugin.on_config_update("self", {}, "1")
        await asyncio.sleep(0.05)
        return task.done() and not task.cancelled()

    monkeypatch.setattr("plugin._INJECTION_FLUSH_TICK_SECONDS", 0.01)
    assert asyncio.run(scenario()) is True


def test_flush_loop_not_duplicated_when_injection_stays_enabled():
    """A config reload that keeps injection enabled reuses the running loop
    (it re-reads self._injection_state each tick) instead of stacking a
    second one."""
    plugin = make_plugin()

    async def scenario() -> int:
        async def no_webhook() -> None:
            return None

        plugin._start_webhook = no_webhook  # type: ignore[assignment]
        plugin._stop_webhook = no_webhook  # type: ignore[assignment]
        plugin.config.context.enabled = True
        await plugin.on_config_update("self", {}, "1")
        first = plugin._flush_loop_task
        await plugin.on_config_update("self", {}, "1")  # reload, injection still on
        assert plugin._flush_loop_task is first
        return len(plugin._tasks)

    assert asyncio.run(scenario()) == 1


def test_injection_failure_rolls_back_dedup_and_quota():
    """A failed injection write rolls the aggregate's dedup keys back
    (design D4): the same reactor+message+emoji combination re-aggregates on
    the next report and retries, and the failure consumes no
    max_reactions_per_message quota (mark_injected is success-only)."""
    plugin = make_plugin()
    plugin.config.context.max_reactions_per_message = 1
    enable_injection(plugin)
    plugin.ctx.maisaka.context.fail_next = True
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1  # attempted and failed

    plugin.ctx.maisaka.context.fail_next = False
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 2  # re-aggregated and injected


def test_silent_period_fallback_flush_and_no_double_delivery(monkeypatch):
    """Silent-period fallback (design D2/D3): an aggregate whose window
    elapsed with no further reports is delivered by the flush loop's single
    tick method, and due()'s remove-on-read keeps the event-driven and tick
    paths from double-delivering."""
    plugin = make_plugin()
    plugin.config.context.merge_window_seconds = 30
    enable_injection(plugin)

    class FakeClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    monkeypatch.setattr("time.time", clock)

    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []  # window not yet elapsed

    clock.now = 1031.0  # window elapsed, no new report arrives
    asyncio.run(plugin._flush_due_aggregates())
    assert len(plugin.ctx.maisaka.context.calls) == 1

    clock.now = 1062.0  # a second message starts aggregating
    asyncio.run(plugin._on_reaction(event(message_id="1002", reactor_qq="20003")))
    assert len(plugin.ctx.maisaka.context.calls) == 1

    clock.now = 1093.0  # a later report drives the event-driven flush
    asyncio.run(plugin._on_reaction(event(message_id="1002", reactor_qq="20004")))
    assert len(plugin.ctx.maisaka.context.calls) == 2

    asyncio.run(plugin._flush_due_aggregates())  # the tick must not re-deliver
    assert len(plugin.ctx.maisaka.context.calls) == 2


# ---- 防御式拦截（chat.receive.before_process Hook）----

def _notify_message(notice_type: str = "group_msg_emoji_like") -> dict[str, Any]:
    return {
        "is_notify": True,
        "message_info": {"additional_config": {"napcat_notice_type": notice_type}},
    }


def test_hook_aborts_reaction_notify_when_enabled():
    plugin = make_plugin()
    enable_injection(plugin)
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message()))
    assert result["action"] == "abort"


def test_hook_passes_through_when_disabled():
    plugin = make_plugin()
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message()))
    assert result["action"] == "continue"


def test_plugin_disabled_plus_context_enabled_no_intercept_no_inject():
    """plugin.enabled=false 时即使 context.enabled=true 也不建注入态：
    Hook 一律放行，也不产生任何注入（开关语义，见 ADR 0003）。"""
    plugin = make_plugin()
    plugin.config.plugin.enabled = False
    enable_injection(plugin)
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message()))
    assert result["action"] == "continue"
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_hook_passes_plain_message():
    plugin = make_plugin()
    enable_injection(plugin)
    result = asyncio.run(
        plugin.guard_reaction_notify(message={"is_notify": False, "message_info": {}})
    )
    assert result["action"] == "continue"


def test_hook_passes_unrelated_notify():
    plugin = make_plugin()
    enable_injection(plugin)
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message("group_recall")))
    assert result["action"] == "continue"


def test_guard_intercepts_notify_from_blacklisted_group():
    """Guard interception is orthogonal to the scope filter (spec-fixed
    behavior, ADR 0003): even a group blacklisted from bandwagon reactions
    still has its reaction notify messages aborted, because the guard
    protects the main context from notify spam, not the reaction scope."""
    plugin = make_plugin()
    plugin.config.scope.group_blacklist = ["10001"]
    enable_injection(plugin)
    message = _notify_message()
    message["message_info"]["group_info"] = {"group_id": "10001"}
    result = asyncio.run(plugin.guard_reaction_notify(message=message))
    assert result["action"] == "abort"
