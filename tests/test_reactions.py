"""reactions.py 纯逻辑层测试。"""

from __future__ import annotations

import time

import pytest

from reactions import (
    BANDWAGON,
    DEFAULT_OPPOSITE_PAIRS,
    OPPOSITE,
    GroupCooldown,
    InjectionState,
    ReactionEvent,
    ReactionState,
    build_opposite_map,
    build_reaction_visible_text,
    build_reactor_display,
    coerce_message_id,
    decide_reaction,
    emoji_id_to_display,
    group_allowed,
    is_missing_action,
    is_reaction_notice,
    is_reaction_notice_message,
    normalize_emoji_id,
    parse_message_author,
    parse_pair,
    parse_reaction_report,
    parse_self_qq,
    protocol_call_failed,
    protocol_failure_detail,
    qq_set,
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


# ---- 名单归一（scope.ignore_reactors / ignore_senders 共用）----

def test_qq_set_strips_and_filters_empty_entries():
    assert qq_set([" 40004 ", "20002", "   ", ""]) == {"20002", "40004"}


def test_qq_set_tolerates_none_and_empty_list():
    assert qq_set(None) == set()
    assert qq_set([]) == set()


def test_qq_set_deduplicates_and_coerces_to_str():
    assert qq_set([20002, "20002", "20003"]) == {"20002", "20003"}


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
    """群冷却时钟（两域各持一个实例，共用这份方法体）：按群计时，到点即解除。"""
    cooldown = GroupCooldown()
    assert cooldown.is_cooling_down("10001", 60.0, now=0.0) is False
    cooldown.mark_group("10001", now=0.0)
    assert cooldown.is_cooling_down("10001", 60.0, now=30.0) is True
    assert cooldown.is_cooling_down("10001", 60.0, now=59.9) is True
    assert cooldown.is_cooling_down("10001", 60.0, now=60.0) is False  # 严格小于：到点即解除
    assert cooldown.is_cooling_down("10002", 60.0, now=30.0) is False


def test_zero_cooldown_never_blocks():
    cooldown = GroupCooldown()
    cooldown.mark_group("10001", now=0.0)
    assert cooldown.is_cooling_down("10001", 0.0, now=0.0) is False


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


def test_protocol_failure_detail_prefers_wording():
    response = {"wording": "token 失效", "message": "internal error", "retcode": 1404}
    assert protocol_failure_detail(response) == "token 失效"


def test_protocol_failure_detail_falls_back_to_message():
    response = {"message": "internal error", "retcode": 1404}
    assert protocol_failure_detail(response) == "internal error"


def test_protocol_failure_detail_falls_back_to_retcode():
    assert protocol_failure_detail({"retcode": 1200}) == "1200"


def test_protocol_failure_detail_skips_blank_fallback_values():
    assert protocol_failure_detail({"wording": "", "message": "internal error"}) == "internal error"


def test_protocol_failure_detail_empty_response():
    # 与既有内联行为一致：三级全缺时 str(None) → "None"
    assert protocol_failure_detail({}) == "None"


def test_protocol_failure_detail_non_dict_response():
    assert protocol_failure_detail("boom") == "boom"
    assert protocol_failure_detail(None) == "None"


def test_is_missing_action():
    assert is_missing_action({"retcode": 1404}) is True
    assert is_missing_action({"retcode": 0}) is False
    assert is_missing_action(None) is False


# ---- 注入聚合（Task 2：去重/聚合/可见文本/形态判定）----

def make_event(message_id="1001", reactor="20002", emoji="128077", group="30003"):
    return ReactionEvent(group_id=group, message_id=message_id,
                         reactor_qq=reactor, emoji_id=emoji)


class TestInjectionState:
    def test_same_key_deduped_within_window(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(), "20002", now + 1) == "skip"

    def test_same_key_allowed_after_window_expiry(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        st.due(now + 3601)  # 真实调用序列里旧聚合早已到期 flush，这里手动清掉
        assert st.record(make_event(), "20002", now + 3601) == "new"

    def test_same_key_after_expiry_merges_if_aggregate_alive(self):
        st = InjectionState(dedup_ttl_seconds=10, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        # dedup 过期但聚合未到期（真实序列里 dedup_ttl > merge_window 时会命中）
        assert st.record(make_event(), "20002", now + 11) == "merged"
        (agg,) = st.due(now + 31)
        assert agg.responders == ["20002"]

    def test_same_message_same_emoji_different_reactors_merge(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(reactor="20003"), "20003", now + 1) == "merged"
        (agg,) = st.due(now + 31)
        assert agg.responders == ["20002", "20003"]

    def test_same_reactor_not_duplicated_in_aggregate(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        # 同键应被 dedup 拦，换一个键但同 reactor（不同 emoji）验证不重复列名
        ev = make_event(emoji="129303")
        assert st.record(ev, "20002", now + 1) == "new"

    def test_different_emojis_get_separate_aggregates(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(emoji="129303"), "20002", now + 1) == "new"
        due = st.due(now + 31)
        assert len(due) == 2

    def test_injection_cap_drops_extra_aggregates(self):
        st = InjectionState(merge_window_seconds=30, max_reactions_per_message=2)
        now = time.time()
        assert st.record(make_event(emoji="128077"), "20002", now) == "new"
        assert st.record(make_event(emoji="129303"), "20002", now + 1) == "new"
        assert st.record(make_event(emoji="128512"), "20002", now + 2) == "skip"

    def test_due_only_returns_expired_aggregates(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        st.record(make_event(emoji="129303", reactor="20003"), "20003", now + 5)
        due = st.due(now + 31)
        assert len(due) == 1
        assert due[0].emoji_id == "128077"
        # 第二个聚合 first_ts=now+5，now+35 到期，now+40 应返回且取出即移除
        assert len(st.due(now + 40)) == 1
        assert st.due(now + 40) == []

    def test_prune_clears_stale_dedup_keys(self):
        st = InjectionState(dedup_ttl_seconds=10, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        st.prune(now + 11)
        st.due(now + 31)  # 聚合 30s 到期后由 due 取出，避免 merged
        assert st.record(make_event(), "20002", now + 11) == "new"

    def test_mark_injected_count_cleared_by_prune_ttl(self):
        st = InjectionState(
            dedup_ttl_seconds=10, merge_window_seconds=30, max_reactions_per_message=1
        )
        now = time.time()
        st.mark_injected("1001", now)
        # TTL 未过时注入计数仍在：同消息不能再开新聚合
        assert st.record(make_event(), "20002", now + 1) == "skip"
        st.prune(now + 12)
        # TTL 过期计数清空：同消息可再次开新聚合
        assert st.record(make_event(), "20002", now + 12) == "new"

    def test_mark_injected_prune_trims_overflow_oldest(self):
        st = InjectionState(
            dedup_ttl_seconds=0, merge_window_seconds=30,
            max_slots=2, max_reactions_per_message=1,
        )
        now = time.time()
        st.mark_injected("1001", now)
        st.mark_injected("1002", now + 1)
        st.mark_injected("1003", now + 2)
        st.prune(now + 3)
        # 超上界时按时间戳最旧丢弃：1001 的计数被清，1003 的仍生效
        assert st.record(make_event(message_id="1001"), "20002", now + 4) == "new"
        assert st.record(make_event(message_id="1003"), "20002", now + 5) == "skip"

    # ---- Task 1.1: reactor_qqs tracking on the aggregate ----

    def test_reactor_qqs_after_new(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        (agg,) = st.due(now + 31)
        assert agg.reactor_qqs == ["20002"]

    def test_reactor_qqs_merged_contains_both_reactors(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(reactor="20002"), "20002", now) == "new"
        assert st.record(make_event(reactor="20003"), "20003", now + 1) == "merged"
        (agg,) = st.due(now + 31)
        assert agg.reactor_qqs == ["20002", "20003"]

    def test_reactor_qqs_same_reactor_not_duplicated(self):
        # Dedup disabled (ttl=0) so the same reactor re-enters the aggregate;
        # the QQ must still not be appended twice.
        st = InjectionState(dedup_ttl_seconds=0, merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(reactor="20002"), "20002", now) == "new"
        assert st.record(make_event(reactor="20002"), "20002", now + 1) == "merged"
        (agg,) = st.due(now + 31)
        assert agg.reactor_qqs == ["20002"]

    def test_reactor_qqs_tracks_qq_not_display_name(self):
        # display name may be empty while the dedup identity is still tracked.
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(reactor="20002"), "", now) == "new"
        (agg,) = st.due(now + 31)
        assert agg.responders == []
        assert agg.reactor_qqs == ["20002"]

    # ---- Task 1.2: rollback drops the aggregate's dedup keys ----

    def test_rollback_lets_same_key_record_new_again(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        (agg,) = st.due(now + 31)
        st.rollback(agg)
        # Same (reactor, message, emoji) must re-aggregate as new, not skip.
        assert st.record(make_event(), "20002", now + 32) == "new"

    def test_rollback_drops_all_reactor_keys_of_merged_aggregate(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(reactor="20002"), "20002", now)
        st.record(make_event(reactor="20003"), "20003", now + 1)
        (agg,) = st.due(now + 31)
        assert agg.reactor_qqs == ["20002", "20003"]
        st.rollback(agg)
        # Both reactors' keys were covered by the aggregate and got dropped.
        assert st.record(make_event(reactor="20002"), "20002", now + 32) == "new"
        assert st.record(make_event(reactor="20003"), "20003", now + 33) == "merged"

    def test_rollback_does_not_touch_other_aggregates_keys(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(emoji="128077"), "20002", now)
        st.record(make_event(emoji="129303", reactor="20003"), "20003", now + 5)
        (agg,) = st.due(now + 31)  # only the first aggregate is due
        st.rollback(agg)
        # The rolled-back key is gone, so the report re-aggregates...
        assert st.record(make_event(emoji="128077"), "20002", now + 32) == "new"
        # ...while the untouched key still dedups within its window.
        assert st.record(make_event(emoji="129303", reactor="20003"), "20003", now + 33) == "skip"

    # ---- Task 1.3: dedup_ttl_seconds=0 disables dedup entirely ----

    def test_zero_dedup_ttl_disables_dedup(self):
        st = InjectionState(dedup_ttl_seconds=0, merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        # Same (reactor, message, emoji) twice: neither may return "skip".
        assert st.record(make_event(), "20002", now + 1) == "merged"
        # After the aggregate is flushed out, a fresh report starts a new one.
        st.due(now + 31)
        assert st.record(make_event(), "20002", now + 32) == "new"

    def test_positive_dedup_ttl_still_dedups_after_expiry(self):
        # Regression guard for the rewritten skip condition: with a positive
        # TTL the original behavior (skip inside the window, re-record after
        # expiry) is unchanged.
        st = InjectionState(dedup_ttl_seconds=10, merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(), "20002", now + 1) == "skip"
        assert st.record(make_event(), "20002", now + 11) == "merged"


class TestVisibleText:
    def test_single_reactor(self):
        text = build_reaction_visible_text("1001", "128077", ["20002"])
        assert text == "[表情回应] 20002 给某条消息(id=1001)贴了 👍"

    def test_multiple_reactors_with_count(self):
        text = build_reaction_visible_text("1001", "128077", ["20002", "20003"])
        assert text == "[表情回应] 20002、20003 给某条消息(id=1001)贴了 👍（2人）"

    def test_no_name_when_responders_empty(self):
        text = build_reaction_visible_text("1001", "76", [])
        assert text == "[表情回应] 给某条消息(id=1001)贴了 76"

    def test_long_message_id_truncated(self):
        text = build_reaction_visible_text("12345678901234567890", "128077", ["20002"])
        assert text == "[表情回应] 20002 给某条消息(id=12345678…)贴了 👍"


class TestEmojiDisplay:
    def test_qq_face_id_passthrough(self):
        assert emoji_id_to_display("76") == "76"

    def test_unicode_codepoint_to_char(self):
        assert emoji_id_to_display("128077") == "👍"

    def test_emoji_char_passthrough(self):
        assert emoji_id_to_display("👍") == "👍"


class TestReactorDisplay:
    def test_self_uses_bot_display_name(self):
        assert build_reactor_display("40004", "40004", "麦麦") == "麦麦"

    def test_self_without_display_name_empty(self):
        assert build_reactor_display("40004", "40004", "") == ""

    def test_other_uses_qq(self):
        assert build_reactor_display("20002", "40004", "麦麦") == "20002"


class TestReactionNoticeMessage:
    def test_emoji_like_notify_matched(self):
        msg = {"is_notify": True,
               "message_info": {"additional_config": {"napcat_notice_type": "group_msg_emoji_like"}}}
        assert is_reaction_notice_message(msg) is True

    def test_plain_message_not_matched(self):
        msg = {"is_notify": False, "message_info": {"additional_config": {}}}
        assert is_reaction_notice_message(msg) is False

    def test_unrelated_notify_not_matched(self):
        msg = {"is_notify": True,
               "message_info": {"additional_config": {"napcat_notice_type": "group_recall"}}}
        assert is_reaction_notice_message(msg) is False