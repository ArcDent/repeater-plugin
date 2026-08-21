"""reactions.py 纯逻辑层测试。"""

from __future__ import annotations

import pytest

from reactions import (
    BANDWAGON,
    DEFAULT_OPPOSITE_PAIRS,
    OPPOSITE,
    ReactionEvent,
    ReactionState,
    build_opposite_map,
    coerce_message_id,
    decide_reaction,
    group_allowed,
    is_missing_action,
    is_reaction_notice,
    normalize_emoji_id,
    parse_message_author,
    parse_pair,
    parse_reaction_report,
    parse_self_qq,
    protocol_call_failed,
)

THUMBS_UP = str(ord("👍"))
THUMBS_DOWN = str(ord("👎"))
HEART = str(ord("❤"))
BROKEN_HEART = str(ord("💔"))


def _event(**overrides):
    base = {
        "group_id": "10001",
        "message_id": "555",
        "reactor_qq": "20002",
        "emoji_id": THUMBS_UP,
    }
    base.update(overrides)
    return ReactionEvent(**base)


def _roll_never_called():
    raise AssertionError("概率为 0 或 1 时不应该掷骰")


# ---- normalize_emoji_id ----

def test_normalize_keeps_plain_digits():
    assert normalize_emoji_id("76") == "76"
    assert normalize_emoji_id(76) == "76"
    assert normalize_emoji_id(" 76 ") == "76"


def test_normalize_converts_emoji_to_codepoint():
    assert normalize_emoji_id("👍") == THUMBS_UP


def test_normalize_ignores_variation_selector_and_skin_tone():
    assert normalize_emoji_id("❤️") == normalize_emoji_id("❤")
    assert normalize_emoji_id("👍🏻") == normalize_emoji_id("👍")


def test_normalize_empty_inputs():
    assert normalize_emoji_id(None) == ""
    assert normalize_emoji_id("") == ""
    assert normalize_emoji_id("   ") == ""


# ---- 上报解析 ----

def test_is_reaction_notice_accepts_known_and_keyword_types():
    assert is_reaction_notice({"post_type": "notice", "notice_type": "group_msg_emoji_like"})
    assert is_reaction_notice({"post_type": "notice", "notice_type": "reaction"})
    # friend_msg_emoji_like 是假想形态：SnowLuma 当前只上报群聊的 group_msg_emoji_like。
    # 这里验证的是关键字宽容匹配，让协议端换实现时也能被识别到并进入形态诊断。
    assert is_reaction_notice({"post_type": "notice", "notice_type": "friend_msg_emoji_like"})


def test_is_reaction_notice_rejects_unrelated():
    assert not is_reaction_notice({"post_type": "message"})
    assert not is_reaction_notice({"post_type": "notice", "notice_type": "group_increase"})
    assert not is_reaction_notice({"post_type": "request", "request_type": "friend"})
    assert not is_reaction_notice("not a dict")


def test_parse_napcat_likes_form():
    event = parse_reaction_report({
        "post_type": "notice",
        "notice_type": "group_msg_emoji_like",
        "group_id": 10001,
        "user_id": 20002,
        "message_id": 555,
        "likes": [{"emoji_id": "76", "count": 1}],
    })
    assert event == ReactionEvent("10001", "555", "20002", "76", False)


def test_parse_reaction_form_with_code_and_operator():
    event = parse_reaction_report({
        "post_type": "notice",
        "notice_type": "reaction",
        "sub_type": "add",
        "group_id": 10001,
        "operator_id": 20002,
        "message_id": 555,
        "code": "👍",
    })
    assert event is not None
    assert event.reactor_qq == "20002"
    assert event.emoji_id == THUMBS_UP
    assert event.removed is False


def test_parse_marks_removal():
    removed_by_subtype = parse_reaction_report({
        "post_type": "notice", "notice_type": "reaction", "sub_type": "remove",
        "group_id": 1, "message_id": 2, "user_id": 3, "code": "76",
    })
    removed_by_notice_type = parse_reaction_report({
        "post_type": "notice", "notice_type": "group_msg_emoji_like_remove",
        "group_id": 1, "message_id": 2, "user_id": 3, "likes": [{"emoji_id": "76"}],
    })
    assert removed_by_subtype is not None and removed_by_subtype.removed is True
    assert removed_by_notice_type is not None and removed_by_notice_type.removed is True


def test_parse_takes_first_like_only():
    event = parse_reaction_report({
        "post_type": "notice", "notice_type": "group_msg_emoji_like",
        "group_id": 1, "message_id": 2, "user_id": 3,
        "likes": [{"emoji_id": "76"}, {"emoji_id": "78"}],
    })
    assert event is not None and event.emoji_id == "76"


@pytest.mark.parametrize("payload", [
    {"post_type": "notice", "notice_type": "group_msg_emoji_like", "message_id": 2, "likes": [{"emoji_id": "76"}]},
    {"post_type": "notice", "notice_type": "group_msg_emoji_like", "group_id": 1, "likes": [{"emoji_id": "76"}]},
    {"post_type": "notice", "notice_type": "group_msg_emoji_like", "group_id": 1, "message_id": 2},
    {"post_type": "notice", "notice_type": "group_msg_emoji_like", "group_id": 1, "message_id": 2, "likes": []},
])
def test_parse_returns_none_on_incomplete_payload(payload):
    assert parse_reaction_report(payload) is None


def test_parse_returns_none_for_unrelated_event():
    assert parse_reaction_report({"post_type": "message", "message_id": 1}) is None


# ---- 反义映射 ----

@pytest.mark.parametrize("token,expected", [
    ("👍=👎", (THUMBS_UP, THUMBS_DOWN)),
    ("👍↔👎", (THUMBS_UP, THUMBS_DOWN)),
    ("76:78", ("76", "78")),
    ("76|78", ("76", "78")),
    (" 👍 = 👎 ", (THUMBS_UP, THUMBS_DOWN)),
])
def test_parse_pair_accepts_separators(token, expected):
    assert parse_pair(token) == expected


@pytest.mark.parametrize("token", ["", None, "👍", "👍=", "=👎", "76=76"])
def test_parse_pair_rejects_invalid(token):
    assert parse_pair(token) is None


def test_default_map_is_bidirectional_and_covers_all_pairs():
    mapping = build_opposite_map()
    assert mapping[THUMBS_UP] == THUMBS_DOWN
    assert mapping[THUMBS_DOWN] == THUMBS_UP
    assert len(mapping) == len(DEFAULT_OPPOSITE_PAIRS) * 2
    for emoji_id, opposite_id in mapping.items():
        assert mapping[opposite_id] == emoji_id


def test_map_can_skip_defaults():
    assert build_opposite_map(include_defaults=False) == {}
    mapping = build_opposite_map(["76=78"], include_defaults=False)
    assert mapping == {"76": "78", "78": "76"}


def test_extra_pair_overrides_default_and_stays_symmetric():
    mapping = build_opposite_map(["👍=💔"])
    assert mapping[THUMBS_UP] == BROKEN_HEART
    assert mapping[BROKEN_HEART] == THUMBS_UP
    # 旧的 👍↔👎 与 ❤↔💔 整对失效，不留下单向残留
    assert THUMBS_DOWN not in mapping
    assert HEART not in mapping
    for emoji_id, opposite_id in mapping.items():
        assert mapping[opposite_id] == emoji_id


def test_disabled_emoji_removes_whole_pair():
    mapping = build_opposite_map(disabled=["👍"])
    assert THUMBS_UP not in mapping
    assert THUMBS_DOWN not in mapping
    assert mapping[HEART] == BROKEN_HEART


def test_invalid_extra_pairs_are_ignored():
    mapping = build_opposite_map(["", "garbage", None, "76=76"])
    assert mapping == build_opposite_map()


# ---- 生效范围 ----

def test_blacklist_wins_over_whitelist():
    assert group_allowed("1", whitelist=["1"], blacklist=["1"]) is False


def test_whitelist_limits_when_non_empty():
    assert group_allowed("1", whitelist=["2"]) is False
    assert group_allowed("2", whitelist=["2"]) is True


def test_empty_lists_allow_everything_but_empty_group():
    assert group_allowed("1") is True
    assert group_allowed("") is False


# ---- 决策 ----

def test_removed_reaction_never_triggers():
    assert decide_reaction(
        _event(removed=True),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=1.0,
        opposite_probability=1.0,
        roll=_roll_never_called,
    ) is None


def test_opposite_branch_hits():
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=1.0,
        opposite_probability=0.5,
        roll=lambda: 0.1,
    )
    assert decision is not None
    assert decision.kind == OPPOSITE
    assert decision.emoji_id == THUMBS_DOWN


def test_opposite_miss_falls_back_to_bandwagon():
    """两级决策（ADR 0002）：回应骰掷中后反义骰不中，回落贴同款。"""
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=1.0,
        opposite_probability=0.5,
        roll=lambda: 0.9,
    )
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == THUMBS_UP


def test_opposite_emoji_respond_roll_miss_does_nothing():
    """两级决策：回应骰不中时，即使反义概率拉满也不回应、不掷反义骰。"""
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=0.5,
        self_message_probability=1.0,
        opposite_probability=1.0,
        roll=lambda: 0.9,
    )
    assert decision is None


def test_opposite_emoji_uses_self_message_probability_for_respond():
    """反义表情的回应骰同样区分 self/other 概率。"""
    decision = decide_reaction(
        _event(),
        is_self_message=True,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=0.1,
        opposite_probability=1.0,
        roll=lambda: 0.5,
    )
    assert decision is None


def test_opposite_emoji_gated_by_respond_roll_first():
    """两级决策：命中反义映射的表情先过回应骰，回应骰不中即使反义概率拉满也不回应。"""
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=0.5,
        self_message_probability=0.5,
        opposite_probability=1.0,
        roll=lambda: 0.9,
    )
    assert decision is None


def test_opposite_emoji_rolls_respond_then_contrarian():
    """命中反义映射的表情掷两次骰：回应骰中后再掷反义骰，反义骰不中回落跟风。

    self_message_probability=0.0 同时验证非 self 消息的回应骰用的是跟风概率
    （若误用 self 概率，回应骰概率为 0 直接不回应，用例会失败）。
    """
    # 回应中 + 反义中 → 反义回应
    rolls = iter([0.1, 0.1])
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=0.5,
        self_message_probability=0.0,
        opposite_probability=0.5,
        roll=lambda: next(rolls),
    )
    assert decision is not None
    assert decision.kind == OPPOSITE
    assert decision.emoji_id == THUMBS_DOWN

    # 回应中 + 反义不中 → 回落跟风贴同款
    rolls = iter([0.1, 0.9])
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=0.5,
        self_message_probability=1.0,
        opposite_probability=0.5,
        roll=lambda: next(rolls),
    )
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == THUMBS_UP


def test_opposite_emoji_on_self_message_uses_self_probability():
    """反义表情的回应骰同样区分 self/other 概率：self 消息概率不中则不回应。"""
    decision = decide_reaction(
        _event(),
        is_self_message=True,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=0.1,
        opposite_probability=1.0,
        roll=lambda: 0.5,
    )
    assert decision is None


def test_bandwagon_uses_other_message_probability():
    rolls = iter([0.4])
    decision = decide_reaction(
        _event(emoji_id="76"),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=0.5,
        self_message_probability=0.0,
        opposite_probability=1.0,
        roll=lambda: next(rolls),
    )
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == "76"


def test_bandwagon_uses_self_message_probability():
    decision = decide_reaction(
        _event(emoji_id="76"),
        is_self_message=True,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=0.1,
        opposite_probability=1.0,
        roll=lambda: 0.5,
    )
    assert decision is None


def test_zero_probability_skips_roll():
    assert decide_reaction(
        _event(emoji_id="76"),
        is_self_message=False,
        opposite_map={},
        bandwagon_probability=0.0,
        self_message_probability=0.0,
        opposite_probability=0.0,
        roll=_roll_never_called,
    ) is None


def test_zero_opposite_probability_skips_second_roll_and_bandwagons():
    """反义概率为 0 = 从不唱反调：命中反义映射的表情只掷回应骰，掷中即贴同款。"""
    rolls = iter([0.1])
    decision = decide_reaction(
        _event(),
        is_self_message=False,
        opposite_map=build_opposite_map(),
        bandwagon_probability=1.0,
        self_message_probability=1.0,
        opposite_probability=0.0,
        roll=lambda: next(rolls),
    )
    assert decision is not None
    assert decision.kind == BANDWAGON
    assert decision.emoji_id == THUMBS_UP


def test_probability_above_one_always_hits():
    decision = decide_reaction(
        _event(emoji_id="76"),
        is_self_message=False,
        opposite_map={},
        bandwagon_probability=5.0,
        self_message_probability=0.0,
        opposite_probability=0.0,
        roll=lambda: 0.999999,
    )
    assert decision is not None and decision.kind == BANDWAGON


def test_garbage_probability_is_treated_as_zero():
    assert decide_reaction(
        _event(emoji_id="76"),
        is_self_message=False,
        opposite_map={},
        bandwagon_probability="很高",
        self_message_probability=0.0,
        opposite_probability=0.0,
        roll=_roll_never_called,
    ) is None


# ---- 表情槽与冷却 ----

def test_slot_blocks_second_reaction_on_same_message():
    state = ReactionState(slot_ttl_seconds=100.0)
    assert state.is_slot_taken("555", now=0.0) is False
    state.take_slot("555", now=0.0)
    assert state.is_slot_taken("555", now=50.0) is True


def test_slot_expires_after_ttl():
    state = ReactionState(slot_ttl_seconds=100.0)
    state.take_slot("555", now=0.0)
    assert state.is_slot_taken("555", now=101.0) is False


def test_slot_table_respects_max_entries():
    state = ReactionState(slot_ttl_seconds=1000.0, max_slots=3)
    for index in range(6):
        state.take_slot(f"msg-{index}", now=float(index))
    assert state.is_slot_taken("msg-0", now=6.0) is False
    assert state.is_slot_taken("msg-5", now=6.0) is True


def test_cooldown_window():
    state = ReactionState()
    assert state.is_cooling_down("10001", 60.0, now=0.0) is False
    state.mark_group("10001", now=0.0)
    assert state.is_cooling_down("10001", 60.0, now=30.0) is True
    assert state.is_cooling_down("10001", 60.0, now=61.0) is False
    assert state.is_cooling_down("10002", 60.0, now=30.0) is False


def test_zero_cooldown_never_blocks():
    state = ReactionState()
    state.mark_group("10001", now=0.0)
    assert state.is_cooling_down("10001", 0.0, now=0.0) is False


def test_author_cache_roundtrip_and_expiry():
    state = ReactionState(slot_ttl_seconds=100.0)
    assert state.recall_author("555", now=0.0) == ""
    state.remember_author("555", "30003", now=0.0)
    assert state.recall_author("555", now=50.0) == "30003"
    assert state.recall_author("555", now=101.0) == ""


# ---- 协议端响应解析 ----

@pytest.mark.parametrize("message_id,expected", [("555", 555), (555, 555), ("abc-1", "abc-1"), ("", "")])
def test_coerce_message_id(message_id, expected):
    assert coerce_message_id(message_id) == expected


def test_parse_message_author_from_sender():
    assert parse_message_author({"status": "ok", "data": {"sender": {"user_id": 30003}}}) == "30003"


def test_parse_message_author_falls_back_to_user_id():
    assert parse_message_author({"data": {"user_id": 30003}}) == "30003"


def test_parse_message_author_missing():
    assert parse_message_author({"status": "ok", "data": {}}) == ""
    assert parse_message_author(None) == ""


def test_parse_self_qq():
    assert parse_self_qq({"status": "ok", "data": {"user_id": 40004}}) == "40004"
    assert parse_self_qq({"user_id": 40004}) == "40004"
    assert parse_self_qq({"status": "ok", "data": {}}) == ""


@pytest.mark.parametrize("response,failed", [
    ({"status": "ok", "retcode": 0}, False),
    ({"status": "failed", "retcode": 1404}, True),
    ({"retcode": 1200}, True),
    ({"retcode": 0}, False),
    ({}, False),
    (None, True),
    ("boom", True),
])
def test_protocol_call_failed(response, failed):
    assert protocol_call_failed(response) is failed


def test_is_missing_action():
    assert is_missing_action({"retcode": 1404}) is True
    assert is_missing_action({"retcode": 0}) is False
    assert is_missing_action(None) is False