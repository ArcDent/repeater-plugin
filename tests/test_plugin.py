"""plugin.py 编排层测试：门禁、分流、自我循环防护、协议端交互。"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from config import RepeaterConfig
from plugin import RepeaterPlugin
from reactions import BANDWAGON, OPPOSITE, ReactionEvent
from tests.fake_maibot_sdk import FakeSendCapability

THUMBS_UP = str(ord("👍"))
THUMBS_DOWN = str(ord("👎"))
OTHER_EMOJI = "76"
BOT_QQ = "40004"


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, message: str) -> None:
        self.records.append((level, str(message)))

    def info(self, message: str) -> None:
        self._log("info", message)

    def warning(self, message: str) -> None:
        self._log("warning", message)

    def error(self, message: str) -> None:
        self._log("error", message)

    def debug(self, message: str) -> None:
        self._log("debug", message)

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
        self.send = FakeSendCapability()


class FakeRequest:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._body


def make_plugin(protocol_responses: dict[str, Any] | None = None) -> RepeaterPlugin:
    """构造一个绕过 MaiBot 运行时的插件实例，协议端调用被替换为查表返回。

    所有概率默认拉满：这样「断言不触发」的门禁用例在门禁失效时必然失败，
    而不是靠默认概率蒙对。需要验证概率分流的用例自行覆盖具体值。
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
    # 复读骰同样定死：复读概率拉满、打断归零，「断言不触发」的门禁用例不靠
    # 默认概率蒙对；冷却归零避免跨用例牵扯，需冷却的用例自行开启。
    plugin.config.repeat.probability = 1.0
    plugin.config.repeat.interrupt_probability = 0.0
    plugin.config.repeat.cooldown_seconds = 0
    # 聚合窗口拉平：注入测试不控制时钟，聚合必须随事件即时 flush 才可断言。
    plugin.config.context.merge_window_seconds = 0

    plugin._runner = None
    plugin._site = None
    plugin._tasks = set()
    plugin._flush_loop_task = None
    plugin._self_qq = ""
    plugin._emoji_action_supported = True
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


# ---- fake ctx.send 替身（design D8）----

def test_fake_send_records_text_calls():
    """替身契约：记录每次 (text, stream_id) 调用，容忍 return_details 透传。"""
    send = FakeSendCapability()
    asyncio.run(send.text("草", "stream-10001"))
    asyncio.run(send.text("停！", "stream-20002", return_details=True))
    assert send.text_calls == [
        {"text": "草", "stream_id": "stream-10001", "return_details": False},
        {"text": "停！", "stream_id": "stream-20002", "return_details": True},
    ]
    assert asyncio.run(send.text("草", "stream-10001")) is True


def test_fake_send_supports_failure_injection():
    send = FakeSendCapability()
    send.raise_on_text = RuntimeError("boom")
    try:
        asyncio.run(send.text("草", "stream-10001"))
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("raise_on_text 应让 text() 抛出注入的异常")
    # 调用先记录再抛：断言「只尝试一次」时仍能在记录里看到这次调用
    assert len(send.text_calls) == 1

    send.raise_on_text = None
    send.text_result = False
    assert asyncio.run(send.text("草", "stream-10001")) is False


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
    return [call["text"] for call in plugin.ctx.send.text_calls]


def test_plugin_disabled_messages_do_not_join_chain():
    """plugin.enabled=false 直接返回不进链：禁用期消息不累积，恢复后从头计数。"""
    plugin = make_plugin()
    plugin.config.plugin.enabled = False

    async def scenario() -> tuple[list[str], list[str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        # 恢复开关后仅一个发送者发言：若禁用期消息进了链，此时计数已满应触发
        plugin.config.plugin.enabled = True
        await plugin.observe_repeat(message=group_message("草", "20003"))
        mid = sent_texts(plugin)
        await plugin.observe_repeat(message=group_message("草", "20005"))
        await plugin.observe_repeat(message=group_message("草", "20006"))
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
            await plugin.observe_repeat(message=group_message("草", sender))
        plugin.config.repeat.enabled = True
        await plugin.observe_repeat(message=group_message("草", "20003"))
        mid = sent_texts(plugin)
        await plugin.observe_repeat(message=group_message("草", "20005"))
        await plugin.observe_repeat(message=group_message("草", "20006"))
        await drain_spawned(plugin)
        return mid, sent_texts(plugin)

    mid, final = asyncio.run(scenario())
    assert mid == []
    assert final == ["草"]


def test_blacklisted_group_does_not_trigger_repeat():
    plugin = make_plugin()
    plugin.config.scope.group_blacklist = ["10001"]

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == []


def test_ignored_sender_messages_do_not_join_chain():
    """ignore_senders 名内发送者（通常是别的 bot）的消息不进复读链。"""
    plugin = make_plugin()
    plugin.config.scope.ignore_senders = ["20002"]

    async def scenario() -> tuple[list[str], list[str]]:
        await plugin.observe_repeat(message=group_message("草", "20002"))
        await plugin.observe_repeat(message=group_message("草", "20003"))
        await plugin.observe_repeat(message=group_message("草", "20004"))
        # 若忽略名单失效：{20002, 20003, 20004} 已达阈值，此处应已发送
        mid = sent_texts(plugin)
        await plugin.observe_repeat(message=group_message("草", "20005"))
        await drain_spawned(plugin)
        return mid, sent_texts(plugin)

    mid, final = asyncio.run(scenario())
    assert mid == []
    assert final == ["草"]


def test_third_sender_consumes_qualification_and_rolls():
    """三人接龙第三次调用消耗资格并掷骰；后续同文本发送者不再触发。"""
    plugin = make_plugin()

    async def scenario() -> tuple[int, list[tuple[str, str]]]:
        await plugin.observe_repeat(message=group_message("草", "20002"))
        await plugin.observe_repeat(message=group_message("草", "20003"))
        after_two = len(plugin.ctx.send.text_calls)
        await plugin.observe_repeat(message=group_message(" 草 ", "20004"))
        await drain_spawned(plugin)
        # 资格已消耗（链终结）：D、E 只续记录
        await plugin.observe_repeat(message=group_message("草", "20005"))
        await plugin.observe_repeat(message=group_message("草", "20006"))
        await drain_spawned(plugin)
        calls = [(c["text"], c["stream_id"]) for c in plugin.ctx.send.text_calls]
        return after_two, calls

    after_two, calls = asyncio.run(scenario())
    assert after_two == 0
    assert calls == [(" 草 ", "stream-10001")]


def test_cooldown_suppresses_and_restores_qualification(monkeypatch):
    """冷却期内另一文本达标不消耗资格不掷骰；冷却结束后新发送者加入即触发。"""
    plugin = make_plugin()
    plugin.config.repeat.cooldown_seconds = 5
    clock = StepClock()
    monkeypatch.setattr("time.time", clock)

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 冷却期内（起点 +1s < 5s）：另一文本「哈哈」三人接龙达标 → 压制不发送
        clock.now += 1
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        # 冷却结束（起点 +6s ≥ 5s）：新发送者加入「哈哈」链 → 直接取得资格掷骰
        clock.now += 5
        await plugin.observe_repeat(message=group_message("哈哈", "20005"))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草", "哈哈"]


def test_silence_outlet_does_not_enter_cooldown():
    """沉默出口消耗资格（链终结）但不进入群冷却：另一文本照常可触发。"""
    plugin = make_plugin()
    plugin.config.repeat.probability = 0.0
    plugin.config.repeat.interrupt_probability = 0.0
    plugin.config.repeat.cooldown_seconds = 600

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        # 若沉默也进冷却，「哈哈」链会被压制；概率翻回拉满验证它仍能触发
        plugin.config.repeat.probability = 1.0
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["哈哈"]


def test_interrupt_outlet_enters_cooldown():
    """打断出口与复读出口同样进入群冷却。"""
    plugin = make_plugin()
    plugin.config.repeat.probability = 0.0
    plugin.config.repeat.interrupt_probability = 1.0
    plugin.config.repeat.interrupt_texts = ["停！"]
    plugin.config.repeat.cooldown_seconds = 600

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 冷却期内另一文本达标 → 压制，不发送
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["停！"]


def test_send_failure_keeps_cooldown():
    """发送成败不影响已进入的冷却：发送抛异常后冷却照常压制下一链。"""
    plugin = make_plugin()
    plugin.config.repeat.cooldown_seconds = 600
    plugin.ctx.send.raise_on_text = RuntimeError("boom")

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 发送已失败，但冷却已写入：另一文本达标应被压制，不再尝试发送
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]  # 只有一次尝试，失败不重试
    assert any("复读/打断发送失败" in t for t in plugin.ctx.logger.texts("warning"))


def test_malformed_message_logs_debug_not_warning():
    """形态异常（非 dict / 缺 message_info）打 debug 不打 warning；合法非候选静默。"""
    plugin = make_plugin()

    async def scenario() -> None:
        await plugin.observe_repeat(message="not a dict")
        await plugin.observe_repeat(message={})
        # 通知消息是合法非候选：不打 debug 也不打 warning
        await plugin.observe_repeat(
            message={"is_notify": True, "message_info": {"additional_config": {}}}
        )
        # 私聊同样合法：group_info 为 None
        message = group_message("草", "20002")
        message["message_info"]["group_info"] = None
        await plugin.observe_repeat(message=message)

    asyncio.run(scenario())
    assert plugin.ctx.logger.texts("warning") == []
    debugs = [t for t in plugin.ctx.logger.texts("debug") if "形态异常" in t]
    assert len(debugs) == 2


def test_private_chat_message_is_silent():
    plugin = make_plugin()

    async def scenario() -> list[str]:
        message = group_message("草", "20002")
        message["message_info"]["group_info"] = None
        await plugin.observe_repeat(message=message)
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == []
    assert plugin.ctx.logger.texts("debug") == []


def test_observe_handler_never_raises():
    """handler 全程不得抛异常：内部异常兜住记 warning，返回继续动作。"""
    plugin = make_plugin()
    plugin.config.repeat.max_text_length = "garbage"  # 非法配置制造内部异常

    result = asyncio.run(plugin.observe_repeat(message=group_message("草", "20002")))
    assert result["action"] == "continue"
    assert any("复读检测处理消息失败" in t for t in plugin.ctx.logger.texts("warning"))


# ---- 复读/打断发送编排 ----

def test_repeat_outlet_sends_last_raw_text():
    """复读出口发送链内最后一条的原始文本（含原始空白），非归一产物。"""
    plugin = make_plugin()

    async def scenario() -> list[str]:
        await plugin.observe_repeat(message=group_message("草", "20002"))
        await plugin.observe_repeat(message=group_message("草", "20003"))
        await plugin.observe_repeat(message=group_message(" 草 ", "20004"))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == [" 草 "]


def test_interrupt_outlet_sends_single_pool_text_without_repeat_text():
    """打断出口只发池内一条文本，不附带复读文本。"""
    plugin = make_plugin()
    plugin.config.repeat.probability = 0.0
    plugin.config.repeat.interrupt_probability = 1.0
    # 单条池子让随机取值确定化
    plugin.config.repeat.interrupt_texts = ["停！"]

    async def scenario() -> list[tuple[str, str]]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        return [(c["text"], c["stream_id"]) for c in plugin.ctx.send.text_calls]

    assert asyncio.run(scenario()) == [("停！", "stream-10001")]


def test_silence_outlet_sends_nothing():
    """双概率皆 0：链达标掷骰后保持沉默，零调用（决策机制由后续触发验证活着）。"""
    plugin = make_plugin()
    plugin.config.repeat.probability = 0.0
    plugin.config.repeat.interrupt_probability = 0.0

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        # 机制活性探针：概率翻满后另一文本的链必须能发出，证明前面的零调用
        # 来自沉默出口而非门禁失效
        plugin.config.repeat.probability = 1.0
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("哈哈", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["哈哈"]


def test_send_waits_behavior_delay_before_sending(monkeypatch):
    """发送经 behavior.delay_min/max 随机延迟后执行，不出现决策即发。"""
    plugin = make_plugin()
    plugin.config.behavior.delay_min_seconds = 1.5
    plugin.config.behavior.delay_max_seconds = 1.5
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        # handler 自身不得延迟（observe 必须快进快出）：此刻延迟尚未发生
        assert sleeps == []
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]
    assert sleeps == [1.5]  # min==max 时区间内随机值唯一


def test_stream_id_resolved_once_per_group_across_sends():
    """stream_id 复用 _get_stream_id 缓存：同群两次发送只查一次聊天流。"""
    plugin = make_plugin()

    async def scenario() -> tuple[list[tuple[str, str]], list[str]]:
        for text in ("草", "哈哈"):
            for sender in ("20002", "20003", "20004"):
                await plugin.observe_repeat(message=group_message(text, sender))
            await drain_spawned(plugin)
        calls = [(c["text"], c["stream_id"]) for c in plugin.ctx.send.text_calls]
        return calls, plugin.ctx.chat.queries

    calls, queries = asyncio.run(scenario())
    assert calls == [("草", "stream-10001"), ("哈哈", "stream-10001")]
    assert queries == ["10001"]


def test_send_failure_result_logged_as_warning():
    """ctx.send.text 返回失败：记 warning，不抛不重试。"""
    plugin = make_plugin()
    plugin.ctx.send.text_result = False

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    assert asyncio.run(scenario()) == ["草"]  # 只调用一次
    assert any("复读/打断发送失败" in t for t in plugin.ctx.logger.texts("warning"))


def test_empty_interrupt_pool_falls_back_with_warning():
    """配置池被清空：回落内置默认池（design D6 五条）并打 warning，不静默哑火。"""
    plugin = make_plugin()
    plugin.config.repeat.probability = 0.0
    plugin.config.repeat.interrupt_probability = 1.0
    plugin.config.repeat.interrupt_texts = []

    async def scenario() -> list[str]:
        for sender in ("20002", "20003", "20004"):
            await plugin.observe_repeat(message=group_message("草", sender))
        await drain_spawned(plugin)
        return sent_texts(plugin)

    # 期望值取自 design D6 的默认池字面量（独立来源），不引自实现常量
    texts = asyncio.run(scenario())
    assert len(texts) == 1
    assert texts[0] in ("打断！", "够了！", "停！", "打住！", "别念了！")
    assert texts[0] != "草"  # 不是复读文本
    assert any("打断文本池为空" in t for t in plugin.ctx.logger.texts("warning"))


# ---- Manifest（repeat-and-rebrand 改名与新 capability 声明）----

MANIFEST_JSON = Path(__file__).resolve().parent.parent / "_manifest.json"
PLUGIN_PY = Path(__file__).resolve().parent.parent / "plugin.py"

# Hook subscriptions are consumed via decorators, not self.ctx.* calls.
HOOK_CAPABILITIES = {"chat.receive.before_process"}

# ctx.logger rides the logging channel, not the capability authorization layer.
_NON_CAPABILITY_PROXIES = {"logger"}


def _ctx_call_capabilities(source: str) -> set[str]:
    """Collect `self.ctx.<proxy>...<method>()` call sites as capability names."""
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
        if parts is None or len(parts) < 4 or parts[0] != "self" or parts[1] != "ctx":
            continue
        if parts[2] in _NON_CAPABILITY_PROXIES:
            continue
        used.add(".".join(parts[2:]))
    return used


def test_manifest_identity():
    """id/name/description 覆盖双行为；仓库改名是用户手动步骤，URL 本组不动。"""
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)  # JSON 语法合法由 load 本身保证
    assert manifest["id"] == "arcdent.repeater-plugin"
    assert manifest["name"] == "复读插件"
    description = manifest["description"]
    assert "复读" in description and "打断" in description  # 复读 + 打断
    assert "表情" in description  # 表情回应跟风/反义
    assert manifest["urls"]["repository"] == "https://github.com/arcdent/sending-emojis-plugin"


def test_manifest_declares_every_consumed_capability():
    """manifest capabilities 必须与代码实际消费的集合一致。

    2026-09-05 线上事故：复读发送走 ctx.send.text 但 manifest 未声明，
    Host 授权层逐次以 E_CAPABILITY_DENIED 拒绝；当时本文件用硬编码等值
    断言，清单跟着代码一起漏。改为从 plugin.py 源码派生消费集合再比对，
    漏声明与死声明双向都拦。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    with PLUGIN_PY.open("r", encoding="utf-8") as fp:
        source = fp.read()
    consumed = _ctx_call_capabilities(source) | HOOK_CAPABILITIES
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
            await plugin.observe_repeat(message=group_message("草", sender))
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
            await plugin.observe_repeat(message=group_message("草", sender))
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
    plugin._self_qq = BOT_QQ
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
    plugin._self_qq = BOT_QQ
    plugin.config.opposite.probability = 1.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=THUMBS_UP)))
    assert decision is not None
    assert decision.kind == OPPOSITE
    assert decision.emoji_id == THUMBS_DOWN
    assert "get_msg" in actions_of(plugin)


def test_bandwagon_on_other_message_uses_main_probability():
    plugin = make_plugin({"get_msg": {"status": "ok", "data": {"sender": {"user_id": "30003"}}}})
    plugin._self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 0.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI)))
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == OTHER_EMOJI
    assert "get_msg" in actions_of(plugin)


def test_bandwagon_on_own_message_uses_self_probability():
    plugin = make_plugin({"get_msg": {"status": "ok", "data": {"sender": {"user_id": BOT_QQ}}}})
    plugin._self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 0.0

    assert asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI))) is None


def test_unknown_author_degrades_to_other_message():
    plugin = make_plugin({"get_msg": {"status": "failed", "retcode": 1404}})
    plugin._self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 0.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI)))
    assert decision is not None and decision.kind == BANDWAGON


def test_author_lookup_skipped_when_self_qq_unknown():
    plugin = make_plugin()
    plugin._self_qq = ""
    plugin.config.bandwagon.probability = 1.0

    decision = asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI)))
    assert decision is not None and decision.kind == BANDWAGON
    assert "get_msg" not in actions_of(plugin)


def test_author_is_cached_across_reports():
    plugin = make_plugin({"get_msg": {"status": "ok", "data": {"sender": {"user_id": "30003"}}}})
    plugin._self_qq = BOT_QQ
    plugin.config.bandwagon.probability = 0.0  # 不触发，槽位保持空闲

    asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI, reactor_qq="20002")))
    asyncio.run(plugin._evaluate(event(emoji_id=OTHER_EMOJI, reactor_qq="20003")))
    assert actions_of(plugin).count("get_msg") == 1


def test_disabled_opposite_falls_back_to_bandwagon():
    plugin = make_plugin()
    plugin.config.opposite.enabled = False
    plugin._rebuild_runtime()
    plugin._self_qq = ""
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
    plugin._self_qq = ""  # 认不出自己，只靠表情槽断环
    plugin.config.opposite.probability = 1.0

    asyncio.run(plugin._on_reaction(event(reactor_qq="20002")))
    assert actions_of(plugin).count("set_msg_emoji_like") == 1

    asyncio.run(plugin._on_reaction(event(reactor_qq=BOT_QQ)))
    asyncio.run(plugin._on_reaction(event(reactor_qq="20003")))
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
    plugin._self_qq = ""
    asyncio.run(plugin._startup_checks())
    assert plugin._self_qq == BOT_QQ


def test_startup_checks_warn_when_self_qq_unavailable():
    plugin = make_plugin({"get_login_info": {"status": "ok", "data": {}}})
    plugin.config.protocol.self_qq = ""
    plugin._self_qq = ""
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
    plugin._bot_display_name = ""
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
    plugin = make_plugin()
    plugin.ctx.chat.streams["10001"] = None
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_append_failure_logs_warning():
    plugin = make_plugin()
    enable_injection(plugin)
    plugin.ctx.maisaka.context.fail_next = True
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert any("注入" in t for t in plugin.ctx.logger.texts("warning"))


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
    _rebuild_runtime (the config-reload path) resets the flag so the next
    injection resolves the display name again.
    """
    plugin = make_plugin()
    plugin.ctx.config.values["bot.nickname"] = ""
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    asyncio.run(plugin._on_reaction(event(message_id="1002")))
    assert plugin.ctx.config.get_calls == 1

    plugin._rebuild_runtime()  # config reload resets the one-shot flag
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
