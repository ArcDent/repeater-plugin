"""forwards.summary pure-logic tests: rich-summary extraction and poll pacing."""

from __future__ import annotations

from repeater_plugin.forwards.summary import (
    extract_rich_summary,
    next_poll_interval,
    should_give_up,
)


def test_extract_rich_summary_strips_surrounding_whitespace():
    assert extract_rich_summary({"processed_plain_text": "  【合并转发消息…】  "}) == "【合并转发消息…】"


def test_extract_rich_summary_requires_a_message_dict():
    assert extract_rich_summary(None) == ""
    assert extract_rich_summary(["processed_plain_text"]) == ""
    assert extract_rich_summary("processed_plain_text") == ""


def test_extract_rich_summary_missing_or_blank_field_is_empty():
    assert extract_rich_summary({}) == ""
    assert extract_rich_summary({"other": "x"}) == ""
    assert extract_rich_summary({"processed_plain_text": "   "}) == ""


def test_extract_rich_summary_non_string_field_is_empty():
    assert extract_rich_summary({"processed_plain_text": None}) == ""
    assert extract_rich_summary({"processed_plain_text": 42}) == ""


def test_next_poll_interval_starts_at_half_second_and_caps_at_one():
    assert next_poll_interval(0) == 0.5
    assert next_poll_interval(1) == 1.0
    assert next_poll_interval(2) == 1.0
    assert next_poll_interval(3) == 1.0


def test_should_give_up_at_timeout_boundary():
    assert should_give_up(9.9, 10) is False
    assert should_give_up(10.0, 10) is True


def test_should_give_up_immediately_for_non_positive_timeout():
    assert should_give_up(0.0, 0) is True
    assert should_give_up(0.0, -5) is True
