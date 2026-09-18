"""forwards.auto pure-logic tests: per-target-group independent roll."""

from __future__ import annotations

from repeater_plugin.forwards.auto import decide_forward_targets


class _RollCounter:
    """Counting roll source: returns the same value but records each call."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


def test_weight_send_only_always_hits():
    hits = decide_forward_targets(["2"], "1", weight_send=100, weight_silence=0, roll=lambda: 0.99)
    assert hits == ["2"]


def test_weight_silence_only_always_silent():
    roll = _RollCounter(0.0)
    hits = decide_forward_targets(
        ["2"], "1", weight_send=0, weight_silence=100, roll=roll
    )
    assert hits == []
    assert roll.calls == 1  # 沉默出口仍然掷骰（权重非全零）


def test_roll_landing_decides_the_outlet():
    # 归一后 3:7：落点 < 0.3 命中转发，否则沉默。
    assert decide_forward_targets(
        ["2"], "1", weight_send=3, weight_silence=7, roll=lambda: 0.29
    ) == ["2"]
    assert (
        decide_forward_targets(["2"], "1", weight_send=3, weight_silence=7, roll=lambda: 0.3)
        == []
    )


def test_zero_weights_are_silent_without_rolling():
    roll = _RollCounter(0.0)
    hits = decide_forward_targets(["2"], "1", weight_send=0, weight_silence=0, roll=roll)
    assert hits == []
    assert roll.calls == 0


def test_negative_and_invalid_weights_are_clamped_to_zero():
    roll = _RollCounter(0.0)
    assert (
        decide_forward_targets(["2"], "1", weight_send=-5, weight_silence="垃圾", roll=roll)
        == []
    )
    assert roll.calls == 0
    # 负权重按 0 计，不影响另一权重的命中：send=10 / silence=-3 归一后必中。
    assert decide_forward_targets(
        ["2"], "1", weight_send=10, weight_silence=-3, roll=lambda: 0.99
    ) == ["2"]


def test_source_group_entry_is_skipped():
    roll = _RollCounter(0.0)
    hits = decide_forward_targets(
        ["1", "2"], "1", weight_send=100, weight_silence=0, roll=roll
    )
    assert hits == ["2"]
    assert roll.calls == 1


def test_targets_roll_independently_and_keep_input_order():
    values = iter([0.05, 0.95, 0.05])
    hits = decide_forward_targets(
        ["2", "3", "4"], "1", weight_send=3, weight_silence=7, roll=lambda: next(values)
    )
    assert hits == ["2", "4"]


def test_empty_and_blank_targets_are_skipped():
    roll = _RollCounter(0.0)
    assert decide_forward_targets([], "1", weight_send=10, weight_silence=0, roll=roll) == []
    assert decide_forward_targets(
        ["", "  ", "2"], "1", weight_send=10, weight_silence=0, roll=roll
    ) == ["2"]
    assert roll.calls == 1


def test_targets_are_normalized_to_strings():
    hits = decide_forward_targets([20002], "1", weight_send=10, weight_silence=0, roll=lambda: 0)
    assert hits == ["20002"]


def test_targets_accept_an_iterable_once():
    generator = (group for group in ["2", "3"])
    hits = decide_forward_targets(generator, "1", weight_send=10, weight_silence=0, roll=lambda: 0)
    assert hits == ["2", "3"]


def test_duplicate_target_entry_rolls_and_delivers_once():
    roll = _RollCounter(0.0)
    hits = decide_forward_targets(
        ["2", "2", "3"], "1", weight_send=10, weight_silence=0, roll=roll
    )
    assert hits == ["2", "3"]
    assert roll.calls == 2
