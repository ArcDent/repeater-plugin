"""repeats.py pure-logic tests: normalization, message qualification,
chain state machine, three-outlet decision."""

from __future__ import annotations

import time

import pytest

# repeats.py uses package-relative imports like plugin.py; the root
# conftest registers the hyphenated repo dir as the synthetic package
# "repeater_plugin" and only aliases plugin/config/reactions at
# top level, so repeats is imported through the package name.
from repeater_plugin.repeats import (
    ACCUMULATE,
    DEFAULT_INTERRUPT_TEXTS,
    INTERRUPT,
    REPEAT,
    SUPPRESSED,
    TERMINATED,
    TRIGGER,
    RepeatCandidate,
    RepeatDecision,
    RepeatState,
    behavior_kind_label,
    build_behavior_visible_text,
    build_trigger_visible_text,
    decide_repeat,
    extract_repeat_candidate,
    normalize_exit_weights,
    normalize_repeat_text,
)

GROUP_ID = "10001"
BOT_QQ = "40004"


def _message(
    text: str = "草",
    sender: str = "20002",
    group: str | None = GROUP_ID,
    is_notify: bool = False,
    segments: list | None = None,
) -> dict:
    """Build a serialized SessionMessage dict in the hook payload shape.

    group=None writes group_info: None, the real private-chat form from
    _message_info_to_dict (the key is present, the value is None).
    """
    if segments is None:
        segments = [{"type": "text", "data": text}]
    message_info: dict = {"user_info": {"user_id": sender, "user_nickname": "某人"}}
    if group is None:
        message_info["group_info"] = None
    else:
        message_info["group_info"] = {"group_id": group, "group_name": "某群"}
    return {
        "is_notify": is_notify,
        "message_info": message_info,
        "raw_message": segments,
    }


# ---- normalize_repeat_text ----

def test_normalize_trims_and_collapses_whitespace():
    assert normalize_repeat_text(" 草 ") == "草"
    assert normalize_repeat_text("哈  哈") == "哈 哈"
    assert normalize_repeat_text("\t哈\n\n 哈喽 \t") == "哈 哈喽"


def test_normalize_is_case_insensitive():
    assert normalize_repeat_text("HHH") == "hhh"
    assert normalize_repeat_text("HHH") == normalize_repeat_text("hhh")


def test_normalize_empty_and_blank_inputs_do_not_crash():
    assert normalize_repeat_text("") == ""
    assert normalize_repeat_text("   \t\n ") == ""
    assert normalize_repeat_text(None) == ""


# ---- extract_repeat_candidate ----

def test_pure_text_message_yields_candidate_with_raw_text():
    candidate = extract_repeat_candidate(_message(" 草 "), self_qq=BOT_QQ)
    assert candidate is not None
    assert candidate.group_id == GROUP_ID
    assert candidate.sender_qq == "20002"
    assert candidate.text == " 草 "  # raw text, whitespace preserved


def test_multiple_text_segments_are_concatenated_raw():
    segments = [
        {"type": "text", "data": "哈"},
        {"type": "text", "data": " 哈喽 "},
    ]
    candidate = extract_repeat_candidate(_message(segments=segments))
    assert candidate is not None
    assert candidate.text == "哈 哈喽 "


def test_numeric_ids_are_coerced_to_strings():
    message = _message()
    message["message_info"]["group_info"] = {"group_id": 10001, "group_name": "某群"}
    message["message_info"]["user_info"] = {"user_id": 20002, "user_nickname": "某人"}
    candidate = extract_repeat_candidate(message)
    assert candidate is not None
    assert candidate.group_id == "10001"
    assert candidate.sender_qq == "20002"


def test_components_dict_form_is_tolerated():
    message = _message()
    message["raw_message"] = {"components": [{"type": "text", "data": "草"}]}
    candidate = extract_repeat_candidate(message)
    assert candidate is not None
    assert candidate.text == "草"


@pytest.mark.parametrize("segment", [
    {"type": "at", "data": {"target_user_id": "40004"}},
    {"type": "reply", "data": "555"},
    {"type": "image", "data": "base64...", "hash": "abc"},
    {"type": "face", "data": "76"},
    {"type": "emoji", "data": "[表情包]", "hash": "abc"},
])
def test_message_with_non_text_segment_is_rejected(segment):
    message = _message(segments=[{"type": "text", "data": "草"}, segment])
    assert extract_repeat_candidate(message) is None


def test_notify_message_is_rejected():
    assert extract_repeat_candidate(_message(is_notify=True)) is None


def test_command_message_is_rejected():
    assert extract_repeat_candidate(_message("/help")) is None
    assert extract_repeat_candidate(_message("  /help  ")) is None


def test_over_length_text_is_rejected():
    assert extract_repeat_candidate(_message("草" * 201), max_text_length=200) is None
    # exactly at the limit still qualifies
    candidate = extract_repeat_candidate(_message("草" * 200), max_text_length=200)
    assert candidate is not None


def test_self_message_is_rejected():
    assert extract_repeat_candidate(_message(sender=BOT_QQ), self_qq=BOT_QQ) is None


def test_private_chat_is_rejected():
    assert extract_repeat_candidate(_message(group=None)) is None


def test_blank_text_is_rejected():
    assert extract_repeat_candidate(_message("   ")) is None
    assert extract_repeat_candidate(_message(segments=[])) is None


@pytest.mark.parametrize("message", [
    None,
    "not a dict",
    12345,
    {},
    {"is_notify": False},
    {"message_info": "garbage", "raw_message": []},
    {"message_info": {}},
    {"message_info": {"group_info": None, "user_info": {"user_id": "20002"}}, "raw_message": []},
    {"message_info": {"group_info": {"group_id": ""}, "user_info": {"user_id": "20002"}}, "raw_message": []},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {}}, "raw_message": []},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {"user_id": "20002"}}},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {"user_id": "20002"}}, "raw_message": "garbage"},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {"user_id": "20002"}}, "raw_message": {"components": "garbage"}},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {"user_id": "20002"}}, "raw_message": ["text"]},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {"user_id": "20002"}}, "raw_message": [{"data": "草"}]},
    {"message_info": {"group_info": {"group_id": "10001"}, "user_info": {"user_id": "20002"}}, "raw_message": [{"type": "text", "data": 123}]},
])
def test_malformed_payloads_return_none(message):
    assert extract_repeat_candidate(message, self_qq=BOT_QQ) is None


# ---- 触发消息上下文打包（blocking 拦截 / 注入用，change design D3）----

def test_candidate_carries_trigger_message_id():
    """触发消息自身 ID 随候选打包：拦截后的触发消息注入要附这个 ID。"""
    message = _message()
    message["message_id"] = "90001"
    candidate = extract_repeat_candidate(message)
    assert candidate is not None
    assert candidate.message_id == "90001"


def test_missing_message_id_still_qualifies_with_empty_id():
    """形态漂移缺 message_id 不拒收候选（防御式口径），回落空串。"""
    candidate = extract_repeat_candidate(_message())
    assert candidate is not None
    assert candidate.message_id == ""


def test_display_name_prefers_cardname_over_nickname():
    """群名片优先于昵称——对齐主程序 planner 侧的显示名惯例。"""
    message = _message()
    message["message_info"]["user_info"] = {
        "user_id": "20002",
        "user_nickname": "某人",
        "user_cardname": "群名片名",
    }
    candidate = extract_repeat_candidate(message)
    assert candidate is not None
    assert candidate.sender_display_name == "群名片名"


def test_display_name_falls_back_to_nickname_without_cardname():
    """群名片未设置（None / 空串 / 缺键 / 空白）时回落昵称。"""
    for cardname in (None, "", "   "):
        message = _message()
        message["message_info"]["user_info"] = {
            "user_id": "20002",
            "user_nickname": "某人",
            "user_cardname": cardname,
        }
        candidate = extract_repeat_candidate(message)
        assert candidate is not None, f"cardname={cardname!r}"
        assert candidate.sender_display_name == "某人", f"cardname={cardname!r}"


def test_display_name_falls_back_to_qq_when_names_unavailable():
    """昵称与名片全缺失或全空白：显示名回落 QQ 号（design D3「取不到用 QQ 号」）。"""
    for user_info in (
        {"user_id": "20002"},
        {"user_id": "20002", "user_nickname": "", "user_cardname": None},
        {"user_id": "20002", "user_nickname": "   ", "user_cardname": "  "},
    ):
        message = _message()
        message["message_info"]["user_info"] = user_info
        candidate = extract_repeat_candidate(message)
        assert candidate is not None, f"user_info={user_info!r}"
        assert candidate.sender_display_name == "20002", f"user_info={user_info!r}"


def test_display_name_is_trimmed():
    message = _message()
    message["message_info"]["user_info"] = {
        "user_id": "20002",
        "user_nickname": " 某人 ",
        "user_cardname": "  群名片  ",
    }
    candidate = extract_repeat_candidate(message)
    assert candidate is not None
    assert candidate.sender_display_name == "群名片"


def test_packed_fields_coerce_non_string_values():
    """形态漂移防御：非字符串 message_id / 昵称按既有 id 强转口径处理。"""
    message = _message()
    message["message_id"] = 90001
    message["message_info"]["user_info"] = {
        "user_id": 20002,
        "user_nickname": 12345,
    }
    candidate = extract_repeat_candidate(message)
    assert candidate is not None
    assert candidate.message_id == "90001"
    assert candidate.sender_display_name == "12345"


# ---- RepeatState ----

def _state(**overrides) -> RepeatState:
    params = {
        "window_seconds": 120.0,
        "threshold": 3,
        "distinct_senders": True,
        "chain_ttl_seconds": 1800.0,
        "max_chains": 2000,
    }
    params.update(overrides)
    return RepeatState(**params)


def test_three_distinct_senders_trigger():
    state = _state()
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    assert state.record("10001", "B", "草", now + 1).status == ACCUMULATE
    assert state.record("10001", "C", "草", now + 2).status == TRIGGER


def test_chain_key_spans_lenient_normalization():
    """首尾空白不打散链: "草" and " 草 " feed the same chain."""
    state = _state(threshold=3)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    assert state.record("10001", "B", " 草 ", now + 1).status == ACCUMULATE
    assert state.record("10001", "C", "草", now + 2).status == TRIGGER


def test_chain_key_is_case_insensitive():
    state = _state(threshold=2)
    now = time.time()
    assert state.record("10001", "A", "HHH", now).status == ACCUMULATE
    assert state.record("10001", "B", "hhh", now + 1).status == TRIGGER


def test_same_sender_does_not_increase_count():
    state = _state(threshold=3, distinct_senders=True)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    assert state.record("10001", "A", "草", now + 1).status == ACCUMULATE
    assert state.record("10001", "A", "草", now + 2).status == ACCUMULATE  # one distinct sender


def test_per_message_counting_when_distinct_senders_disabled():
    state = _state(threshold=3, distinct_senders=False)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    assert state.record("10001", "A", "草", now + 1).status == ACCUMULATE
    assert state.record("10001", "A", "草", now + 2).status == TRIGGER


def test_trigger_carries_last_raw_text_not_normalized():
    state = _state(threshold=2)
    now = time.time()
    state.record("10001", "A", "草", now)
    result = state.record("10001", "B", " 草 ", now + 1)
    assert result.status == TRIGGER
    assert result.text == " 草 "  # raw text of the chain's last message


def test_window_rolling_expires_early_entries():
    state = _state(threshold=3, window_seconds=60.0)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    # A's entry is now older than the window: only B, C, D count
    later = now + 61
    assert state.record("10001", "B", "草", later).status == ACCUMULATE
    assert state.record("10001", "C", "草", later + 1).status == ACCUMULATE
    assert state.record("10001", "D", "草", later + 2).status == TRIGGER


def test_followup_senders_get_no_qualification_within_marker_ttl():
    state = _state(threshold=3, chain_ttl_seconds=1800.0)
    now = time.time()
    state.record("10001", "A", "草", now)
    state.record("10001", "B", "草", now + 1)
    state.record("10001", "C", "草", now + 2)  # trigger, marker written
    # The qualification is consumed inside record(), before any dice —
    # whichever outlet the caller later rolls (silence included), D and
    # E only extend the record within the marker TTL.
    assert state.record("10001", "D", "草", now + 10).status == TERMINATED
    assert state.record("10001", "E", "草", now + 11).status == TERMINATED


def test_new_round_triggers_after_marker_ttl_expiry():
    state = _state(threshold=3, chain_ttl_seconds=100.0, window_seconds=50.0)
    now = time.time()
    state.record("10001", "A", "草", now)
    state.record("10001", "B", "草", now + 1)
    state.record("10001", "C", "草", now + 2)  # trigger + marker at now+2
    # Both the marker TTL (100s) and the window (50s) have elapsed.
    later = now + 200
    assert state.record("10001", "A", "草", later).status == ACCUMULATE
    assert state.record("10001", "B", "草", later + 1).status == ACCUMULATE
    assert state.record("10001", "C", "草", later + 2).status == TRIGGER


def test_terminated_sender_entry_counts_after_marker_expiry():
    """只续记录: a sender arriving while the marker is active still gets an
    entry; once the marker expires and the original senders have fallen out
    of the window, that entry counts toward the next qualification."""
    state = _state(threshold=2, chain_ttl_seconds=50.0, window_seconds=100.0)
    now = time.time()
    state.record("10001", "A", "草", now)
    state.record("10001", "B", "草", now + 1)
    state.record("10001", "C", "草", now + 2)  # trigger, marker at now+2
    assert state.record("10001", "D", "草", now + 10).status == TERMINATED
    # At now+105: marker expired (103 > 50), A/B/C entries out of the
    # 100s window, D's entry (now+10) still inside — D + E reach 2.
    assert state.record("10001", "E", "草", now + 105).status == TRIGGER


def test_chains_are_scoped_per_group():
    state = _state(threshold=2)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    # Same text in another group is a different chain: B alone doesn't reach 2
    assert state.record("10002", "B", "草", now + 1).status == ACCUMULATE


def test_max_chains_evicts_least_recently_active_chain():
    state = _state(threshold=2, window_seconds=3600.0, max_chains=1)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    # Recording another text overflows max_chains=1: the least recently
    # active chain ("草") is evicted.
    assert state.record("10001", "A", "哈哈", now + 1).status == ACCUMULATE
    # "草" starts a fresh chain: B alone cannot reach threshold 2
    assert state.record("10001", "B", "草", now + 2).status == ACCUMULATE


def test_eviction_drops_the_termination_marker_with_the_chain():
    state = _state(threshold=2, window_seconds=3600.0, max_chains=1)
    now = time.time()
    state.record("10001", "A", "草", now)
    state.record("10001", "B", "草", now + 1)  # trigger, marker written
    # Evicting the "草" chain takes its marker along (终结标记随链淘汰)
    state.record("10001", "A", "哈哈", now + 2)
    # A fresh round for "草" may qualify again immediately
    assert state.record("10001", "A", "草", now + 3).status == ACCUMULATE
    assert state.record("10001", "B", "草", now + 4).status == TRIGGER


# ---- 冷却压制（suppress_trigger 与复读专用群冷却）----

def test_suppressed_threshold_consumes_no_qualification():
    """冷却压制：压制期间达标不写终结标记、不消耗资格。

    压制结束后新发送者的消息到达时，链已达标的直接取得资格（group 3 报告的
    API 缺口；spec「冷却期内达标不掷骰」场景的状态机层语义）。
    """
    state = _state(threshold=3, chain_ttl_seconds=1800.0)
    now = time.time()
    assert state.record("10001", "A", "草", now, suppress_trigger=True).status == ACCUMULATE
    assert state.record("10001", "B", "草", now + 1, suppress_trigger=True).status == ACCUMULATE
    assert state.record("10001", "C", "草", now + 2, suppress_trigger=True).status == SUPPRESSED
    # 压制持续期间：后续发送者照常累积（仍达标、仍压制），始终不写终结标记
    assert state.record("10001", "D", "草", now + 3, suppress_trigger=True).status == SUPPRESSED
    # 压制结束：这一次 record 直接取得资格，触发携带链内最后一条原始文本
    result = state.record("10001", "E", " 草 ", now + 10)
    assert result.status == TRIGGER
    assert result.text == " 草 "


def test_active_marker_terminates_even_when_suppressed():
    """终结标记优先于压制：资格已消耗的链在标记 TTL 内不因压制复活。"""
    state = _state(threshold=2, chain_ttl_seconds=1800.0)
    now = time.time()
    state.record("10001", "A", "草", now)
    state.record("10001", "B", "草", now + 1)  # TRIGGER，写终结标记
    assert state.record("10001", "C", "草", now + 2, suppress_trigger=True).status == TERMINATED


def test_unsuppressed_trigger_still_consumes_qualification():
    """未压制时既有语义不变：达标即写终结标记（沉默口径同样终结）。"""
    state = _state(threshold=2, chain_ttl_seconds=1800.0)
    now = time.time()
    assert state.record("10001", "A", "草", now).status == ACCUMULATE
    assert state.record("10001", "B", "草", now + 1).status == TRIGGER
    assert state.record("10001", "C", "草", now + 2).status == TERMINATED


def test_group_repeat_cooldown_marks_and_expires():
    """复读专用群冷却：与 reactions.ReactionState 的表情群冷却互不相干。"""
    state = RepeatState()
    now = time.time()
    assert state.is_cooling_down("10001", 120.0, now) is False
    state.mark_group("10001", now)
    assert state.is_cooling_down("10001", 120.0, now + 119.9) is True
    assert state.is_cooling_down("10001", 120.0, now + 120.0) is False


def test_group_repeat_cooldown_zero_disables():
    state = RepeatState()
    now = time.time()
    state.mark_group("10001", now)
    assert state.is_cooling_down("10001", 0.0, now) is False


def test_group_repeat_cooldown_scoped_per_group():
    state = RepeatState()
    now = time.time()
    state.mark_group("10001", now)
    assert state.is_cooling_down("10002", 120.0, now) is False


# ---- normalize_exit_weights（出口权重非负归一，ADR 0007 / design D1）----

def test_normalize_exit_weights_negative_garbage_none_become_zero():
    """负数 / 非数字 / None 一律按 0，三个都非法时总和为 0。"""
    triple, total = normalize_exit_weights(-5, "很高", None)
    assert triple == (0.0, 0.0, 0.0)
    assert total == 0.0


def test_normalize_exit_weights_keeps_numbers_and_sums_them():
    """85 原样（85.0），总和为三者之和。"""
    triple, total = normalize_exit_weights(85, 15, 0)
    assert triple == (85.0, 15.0, 0.0)
    assert total == 100.0


def test_normalize_exit_weights_invalid_single_weight_keeps_others():
    """单个权重非法只把自身按 0 计，不影响其余权重与总和。"""
    triple, total = normalize_exit_weights("很高", 15, -3)
    assert triple == (0.0, 15.0, 0.0)
    assert total == 15.0


# ---- decide_repeat ----

def _roll_never_called():
    raise AssertionError("三个权重全为 0——不应掷骰")


def test_repeat_outlet_when_scaled_roll_below_repeat_weight():
    """权重 85/15/0：roll 落点 0.5 × 100 = 50 < 85 → 复读出口。"""
    decision = decide_repeat(
        " 草 ",
        weight_repeat=85,
        weight_interrupt=15,
        weight_silence=0,
        roll=lambda: 0.5,
    )
    assert decision is not None
    assert decision.kind == REPEAT
    assert decision.text == " 草 "  # carries the raw text
    assert decision.reason == "复读出口命中（权重 85/15/0）"


def test_interrupt_outlet_starts_at_the_repeat_weight_boundary():
    """落点恰等于复读权重上界（0.85 × 100 == 85.0）→ 打断区间，不是复读。

    复读区间 [0, 85)，打断区间 [85, 85 + 15)（上界 = 0.85 + 0.15 = 1.0 的
    100 倍，触及总和）。"""
    decision = decide_repeat(
        "草",
        weight_repeat=85,
        weight_interrupt=15,
        weight_silence=0,
        interrupt_texts=["停！"],
        roll=lambda: 0.85,
        choice=lambda pool: pool[0],
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert decision.text == "停！"  # interrupt text only, never the repeat text
    assert decision.reason == "打断出口命中（权重 85/15/0）"


def test_silence_outlet_when_roll_lands_in_silence_weight():
    """权重 60/15/25：roll 落点 0.9 × 100 = 90 ≥ 60 + 15 → 沉默出口。"""
    decision = decide_repeat(
        "草",
        weight_repeat=60,
        weight_interrupt=15,
        weight_silence=25,
        roll=lambda: 0.9,
    )
    assert decision is None


def test_silence_outlet_when_roll_approaches_one():
    """roll 逼近 1（0.999999 × 100 = 99.9999）仍落在沉默区间。"""
    decision = decide_repeat(
        "草",
        weight_repeat=60,
        weight_interrupt=15,
        weight_silence=25,
        roll=lambda: 0.999999,
    )
    assert decision is None


def test_interrupt_picks_from_the_configured_pool():
    picked: list[list[str]] = []

    def fake_choice(pool):
        picked.append(list(pool))
        return pool[-1]

    decision = decide_repeat(
        "草",
        weight_repeat=0,
        weight_interrupt=1,
        weight_silence=0,
        interrupt_texts=["够了！", "停！"],
        roll=lambda: 0.3,
        choice=fake_choice,
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert decision.text == "停！"
    assert picked == [["够了！", "停！"]]


def test_empty_interrupt_pool_falls_back_to_builtin_defaults():
    picked: list[list[str]] = []

    def fake_choice(pool):
        picked.append(list(pool))
        return pool[0]

    decision = decide_repeat(
        "草",
        weight_repeat=0,
        weight_interrupt=1,
        weight_silence=0,
        interrupt_texts=[],
        roll=lambda: 0.3,
        choice=fake_choice,
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert picked == [list(DEFAULT_INTERRUPT_TEXTS)]
    assert decision.text == DEFAULT_INTERRUPT_TEXTS[0]
    # 回落事实由决策结果携带（used_default_pool），调用方只读标志打 warning
    assert decision.used_default_pool is True


def test_configured_pool_keeps_used_default_pool_false():
    decision = decide_repeat(
        "草",
        weight_repeat=0,
        weight_interrupt=1,
        weight_silence=0,
        interrupt_texts=["够了！", "停！"],
        roll=lambda: 0.3,
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert decision.used_default_pool is False


def test_repeat_outlet_carries_used_default_pool_false():
    decision = decide_repeat(
        " 草 ",
        weight_repeat=85,
        weight_interrupt=15,
        weight_silence=0,
        roll=lambda: 0.5,
    )
    assert decision is not None
    assert decision.kind == REPEAT
    assert decision.used_default_pool is False


def test_all_blank_pool_falls_back_to_builtin_defaults():
    decision = decide_repeat(
        "草",
        weight_repeat=0,
        weight_interrupt=1,
        weight_silence=0,
        interrupt_texts=["   ", ""],
        roll=lambda: 0.3,
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert decision.text in DEFAULT_INTERRUPT_TEXTS
    assert decision.used_default_pool is True


def test_missing_interrupt_pool_falls_back_to_builtin_defaults():
    decision = decide_repeat(
        "草",
        weight_repeat=0,
        weight_interrupt=1,
        weight_silence=0,
        interrupt_texts=None,
        roll=lambda: 0.3,
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert decision.text in DEFAULT_INTERRUPT_TEXTS
    assert decision.used_default_pool is True


@pytest.mark.parametrize("roll_value", [0.2, 0.5, 0.9])
def test_same_roll_hits_same_outlet_when_weights_scaled(roll_value):
    """权重同比例放大（85/15/0 → 170/30/0）只换数轴刻度，不换出口。

    roll_value × 100 与 × 200 落在同一累计阈值区间：0.2 与 0.5 都进复读
    区间、0.9 都进打断区间（design D1 的 85/15/0 示例，0.85 + 0.15 = 1.0
    触及上界）。"""
    def outlet_of(weight_repeat: int, weight_interrupt: int, weight_silence: int) -> str:
        decision = decide_repeat(
            "草",
            weight_repeat=weight_repeat,
            weight_interrupt=weight_interrupt,
            weight_silence=weight_silence,
            interrupt_texts=["停！"],
            roll=lambda: roll_value,
        )
        return decision.kind if decision is not None else "silence"

    assert outlet_of(85, 15, 0) == outlet_of(170, 30, 0)


def test_all_zero_weights_skip_the_roll():
    assert decide_repeat(
        "草",
        weight_repeat=0,
        weight_interrupt=0,
        weight_silence=0,
        roll=_roll_never_called,
    ) is None


def test_garbage_weight_is_treated_as_zero():
    """非法权重 clamp 成 0：复读份额为 0、打断权重独占——掷骰照常、不报错。"""
    decision = decide_repeat(
        "草",
        weight_repeat="很高",
        weight_interrupt=15,
        weight_silence=0,
        interrupt_texts=["停！"],
        roll=lambda: 0.5,
        choice=lambda pool: pool[0],
    )
    assert decision is not None
    assert decision.kind == INTERRUPT
    assert decision.text == "停！"
    assert decision.used_default_pool is False


# ---- 注入文案构造（visible_text，change design D3；对齐 reactions 的
# build_reaction_visible_text 先例：构造在纯逻辑层，plugin 只编排）----

def test_build_trigger_visible_text_renders_display_name_and_raw_text():
    candidate = RepeatCandidate(
        group_id=GROUP_ID,
        sender_qq="20002",
        text=" 草 ",
        message_id="msg-42",
        sender_display_name="某人",
    )
    # 原始文本原样呈现（含首尾空白），显示名取 candidate 三级回落产物
    assert build_trigger_visible_text(candidate) == "[复读触发] 某人： 草 "


def test_build_trigger_visible_text_uses_sender_qq_when_no_display_name():
    candidate = RepeatCandidate(
        group_id=GROUP_ID, sender_qq="20002", text="草", sender_display_name="20002"
    )
    assert build_trigger_visible_text(candidate) == "[复读触发] 20002：草"


def test_build_behavior_visible_text_labels_repeat_outlet():
    decision = RepeatDecision(kind=REPEAT, text=" 草 ", reason="复读出口命中")
    assert build_behavior_visible_text(decision) == "[复读]  草 "


def test_build_behavior_visible_text_labels_interrupt_outlet():
    decision = RepeatDecision(kind=INTERRUPT, text="够了！", reason="打断出口命中")
    assert build_behavior_visible_text(decision) == "[打断] 够了！"


def test_behavior_kind_label_maps_outlets():
    assert behavior_kind_label(REPEAT) == "复读"
    assert behavior_kind_label(INTERRUPT) == "打断"
