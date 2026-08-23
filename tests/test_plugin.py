"""plugin.py 编排层测试：门禁、分流、自我循环防护、协议端交互。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from config import SendingEmojisConfig
from plugin import SendingEmojisPlugin
from reactions import BANDWAGON, OPPOSITE, ReactionEvent

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

    async def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class FakeChat:
    def __init__(self) -> None:
        self.streams: dict[str, dict[str, Any] | None] = {}

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> Any:
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


def make_plugin(protocol_responses: dict[str, Any] | None = None) -> SendingEmojisPlugin:
    """构造一个绕过 MaiBot 运行时的插件实例，协议端调用被替换为查表返回。

    所有概率默认拉满：这样「断言不触发」的门禁用例在门禁失效时必然失败，
    而不是靠默认概率蒙对。需要验证概率分流的用例自行覆盖具体值。
    """
    plugin = SendingEmojisPlugin()
    plugin.config = SendingEmojisConfig()
    plugin.ctx = FakeCtx()
    plugin.config.protocol.base_url = "http://protocol.test:3000"
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.behavior.delay_min_seconds = 0.0
    plugin.config.behavior.delay_max_seconds = 0.0
    plugin.config.behavior.cooldown_seconds = 0
    plugin.config.opposite.probability = 1.0
    plugin.config.bandwagon.probability = 1.0
    plugin.config.bandwagon.self_message_probability = 1.0
    # 聚合窗口拉平：注入测试不控制时钟，聚合必须随事件即时 flush 才可断言。
    plugin.config.context.merge_window_seconds = 0

    plugin._runner = None
    plugin._site = None
    plugin._tasks = set()
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


def actions_of(plugin: SendingEmojisPlugin) -> list[str]:
    return [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]


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

def _post(plugin: SendingEmojisPlugin, payload: Any, headers: dict[str, str] | None = None):
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

def enable_injection(plugin: SendingEmojisPlugin) -> None:
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