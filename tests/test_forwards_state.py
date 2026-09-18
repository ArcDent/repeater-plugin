"""forwards.state pure-logic tests: dedup, cooldowns, delivered registry,
group availability cache."""

from __future__ import annotations

import inspect

from sending_emojis_plugin.forwards.state import (
    DeliveredRegistry,
    ForwardState,
    GroupAvailabilityCache,
)


def test_message_dedup_hits_within_ttl():
    state = ForwardState()
    state.mark_message("9001", 100.0)
    assert state.is_duplicate("9001", 60, 159.9) is True
    assert state.is_duplicate("9002", 60, 159.9) is False


def test_message_dedup_expires_after_ttl():
    state = ForwardState()
    state.mark_message("9001", 100.0)
    assert state.is_duplicate("9001", 60, 160.0) is False


def test_message_dedup_disabled_with_non_positive_ttl():
    state = ForwardState()
    state.mark_message("9001", 100.0)
    assert state.is_duplicate("9001", 0, 100.0) is False
    assert state.is_duplicate("9001", -5, 100.0) is False


def test_message_dedup_ignores_blank_ids():
    state = ForwardState()
    state.mark_message("", 100.0)
    assert state.is_duplicate("", 60, 100.0) is False
    assert state.is_duplicate("  ", 60, 100.0) is False


def test_group_cooldown_window():
    state = ForwardState()
    state.mark_group("10001", 100.0)
    assert state.is_group_cooling("10001", 60, 159.9) is True
    assert state.is_group_cooling("10001", 60, 160.0) is False
    assert state.is_group_cooling("10002", 60, 100.0) is False


def test_group_cooldown_disabled_when_ttl_zero():
    state = ForwardState()
    state.mark_group("10001", 100.0)
    assert state.is_group_cooling("10001", 0, 100.0) is False


def test_target_cooldown_only_affects_marked_groups():
    state = ForwardState()
    state.mark_targets(["20001", "20002"], 100.0)
    assert state.is_target_cooling("20001", 60, 100.0) is True
    assert state.is_target_cooling("20002", 60, 100.0) is True
    assert state.is_target_cooling("20003", 60, 100.0) is False
    assert state.is_target_cooling("20001", 60, 160.0) is False


def test_source_and_target_cooldowns_are_separate_clocks():
    state = ForwardState()
    state.mark_group("30001", 100.0)
    state.mark_targets(["30002"], 100.0)
    assert state.is_group_cooling("30001", 60, 100.0) is True
    assert state.is_target_cooling("30001", 60, 100.0) is False
    assert state.is_target_cooling("30002", 60, 100.0) is True
    assert state.is_group_cooling("30002", 60, 100.0) is False


def test_mark_targets_accepts_a_bare_group_id():
    state = ForwardState()
    state.mark_targets("20001", 100.0)
    assert state.is_target_cooling("20001", 60, 100.0) is True
    assert state.is_target_cooling("2", 60, 100.0) is False


def test_state_methods_are_all_synchronous():
    state = ForwardState()
    for name in (
        "is_duplicate",
        "mark_message",
        "is_group_cooling",
        "mark_group",
        "is_target_cooling",
        "mark_targets",
    ):
        method = getattr(state, name)
        assert inspect.iscoroutinefunction(method) is False, name


# ---- DeliveredRegistry ----

def test_delivered_registry_registers_and_queries():
    registry = DeliveredRegistry(ttl_seconds=100)
    registry.register("stream-1:9001", ["2", "3"], 100.0)
    assert registry.is_delivered("stream-1:9001", "2", 100.0) is True
    assert registry.is_delivered("stream-1:9001", "4", 100.0) is False
    assert registry.is_delivered("stream-2:9001", "2", 100.0) is False


def test_delivered_registry_targets_are_deduped_and_ordered():
    registry = DeliveredRegistry(ttl_seconds=100)
    registry.register("k", ["2", "3", "2"], 100.0)
    assert registry.targets_for("k", 100.0) == ["2", "3"]


def test_delivered_registry_unions_across_registers():
    registry = DeliveredRegistry(ttl_seconds=100)
    registry.register("k", ["2"], 100.0)
    registry.register("k", ["3"], 110.0)
    assert registry.targets_for("k", 110.0) == ["2", "3"]


def test_delivered_registry_expires_after_ttl():
    registry = DeliveredRegistry(ttl_seconds=100)
    registry.register("k", ["2"], 100.0)
    assert registry.is_delivered("k", "2", 199.9) is True
    assert registry.is_delivered("k", "2", 200.0) is False
    assert registry.targets_for("k", 200.0) == []


def test_delivered_registry_with_non_positive_ttl_never_records():
    registry = DeliveredRegistry(ttl_seconds=0)
    registry.register("k", ["2"], 100.0)
    assert registry.targets_for("k", 100.0) == []
    assert registry.is_delivered("k", "2", 100.0) is False


def test_delivered_registry_ignores_blank_entries():
    registry = DeliveredRegistry(ttl_seconds=100)
    registry.register("", ["2"], 100.0)
    registry.register("k", ["", "  "], 100.0)
    assert registry.targets_for("", 100.0) == []
    assert registry.targets_for("k", 100.0) == []
    assert registry.is_delivered("k", "", 100.0) is False


def test_delivered_registry_accepts_a_bare_group_id():
    registry = DeliveredRegistry(ttl_seconds=100)
    registry.register("k", "2", 100.0)
    assert registry.targets_for("k", 100.0) == ["2"]
    assert registry.is_delivered("k", "2", 100.0) is True


# ---- GroupAvailabilityCache ----

def test_group_cache_unknown_groups_are_allowed():
    cache = GroupAvailabilityCache(ttl_seconds=300)
    assert cache.has_fresh_data(100.0) is False
    assert cache.is_available("1", 100.0) is True


def test_group_cache_membership_after_update():
    cache = GroupAvailabilityCache(ttl_seconds=300)
    cache.update(["1", "2"], 100.0)
    assert cache.has_fresh_data(100.0) is True
    assert cache.is_available("1", 100.0) is True
    assert cache.is_available("3", 100.0) is False


def test_group_cache_expires_back_to_unknown():
    cache = GroupAvailabilityCache(ttl_seconds=300)
    cache.update(["1"], 100.0)
    assert cache.has_fresh_data(399.9) is True
    assert cache.is_available("3", 399.9) is False
    assert cache.has_fresh_data(400.0) is False
    assert cache.is_available("3", 400.0) is True


def test_group_cache_empty_update_means_unknown():
    cache = GroupAvailabilityCache(ttl_seconds=300)
    cache.update(["1"], 100.0)
    cache.update([], 110.0)
    assert cache.has_fresh_data(110.0) is False
    assert cache.is_available("3", 110.0) is True
    cache.update(None, 120.0)
    assert cache.has_fresh_data(120.0) is False


def test_group_cache_normalizes_ids():
    cache = GroupAvailabilityCache(ttl_seconds=300)
    cache.update([10001, " 10002 ", ""], 100.0)
    assert cache.is_available("10001", 100.0) is True
    assert cache.is_available("10002", 100.0) is True
    assert cache.is_available("", 100.0) is False


def test_registry_and_cache_methods_are_all_synchronous():
    registry = DeliveredRegistry()
    cache = GroupAvailabilityCache()
    for method in (
        registry.register,
        registry.is_delivered,
        registry.targets_for,
        cache.update,
        cache.is_available,
        cache.has_fresh_data,
    ):
        assert inspect.iscoroutinefunction(method) is False, method


def test_delivered_registry_unknown_key_has_no_targets():
    registry = DeliveredRegistry(ttl_seconds=100)
    assert registry.targets_for("unknown", 100.0) == []
    assert registry.is_delivered("unknown", "2", 100.0) is False


def test_group_cache_update_refreshes_the_ttl_window():
    cache = GroupAvailabilityCache(ttl_seconds=300)
    cache.update(["1"], 100.0)
    cache.update(["1", "2"], 350.0)
    assert cache.is_available("2", 600.0) is True
    assert cache.is_available("3", 600.0) is False
    assert cache.has_fresh_data(649.9) is True
    assert cache.has_fresh_data(650.0) is False

