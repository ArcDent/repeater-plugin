"""forwards.summary pure-logic tests: rich-summary extraction, placeholder counting, and poll pacing."""

from __future__ import annotations

from repeater_plugin.forwards.nodes import (
    NESTED_FORWARD_PLACEHOLDER,
    SEGMENT_PLACEHOLDERS,
)
from repeater_plugin.forwards.summary import (
    count_rich_summary_placeholders,
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


def test_count_rich_summary_placeholders_counts_each_media_kind():
    assert count_rich_summary_placeholders("看图[图片]") == 1
    assert count_rich_summary_placeholders("笑了[表情]") == 1
    assert count_rich_summary_placeholders("聊天记录里还有[嵌套转发]") == 1
    # 规格场景字面量：富摘要含 2 处 `[图片]` 占位。
    assert (
        count_rich_summary_placeholders("【合并转发消息】群友甲：[图片] 群友乙：[图片]") == 2
    )


def test_count_rich_summary_placeholders_counts_repeated_and_mixed_occurrences():
    assert count_rich_summary_placeholders("[图片][图片][表情]") == 3
    assert (
        count_rich_summary_placeholders("甲：[图片]，乙：[表情]，丙转发自[嵌套转发]") == 3
    )


def test_count_rich_summary_placeholders_covers_every_segment_placeholder_text():
    for placeholder in SEGMENT_PLACEHOLDERS.values():
        assert count_rich_summary_placeholders(f"前面{placeholder}后面") == 1


def test_nested_forward_placeholder_is_a_single_shared_definition():
    """`[嵌套转发]` 文案取 `forwards/nodes.py` 的唯一来源，不散落字面量。

    `forward` 段不进 `SEGMENT_PLACEHOLDERS`：该表的键集同时是「按图片重建的
    段类型」判定集，嵌套转发段在发送侧递归展开、不按图片重建。
    """
    assert NESTED_FORWARD_PLACEHOLDER == "[嵌套转发]"
    assert "forward" not in SEGMENT_PLACEHOLDERS
    assert count_rich_summary_placeholders(f"转发自{NESTED_FORWARD_PLACEHOLDER}") == 1


def test_count_rich_summary_placeholders_zero_for_plain_text():
    assert count_rich_summary_placeholders("普通文本，没有任何媒体占位") == 0
    assert count_rich_summary_placeholders("") == 0


def test_count_rich_summary_placeholders_tolerates_non_string_input():
    assert count_rich_summary_placeholders(None) == 0
    assert count_rich_summary_placeholders(42) == 0
    assert count_rich_summary_placeholders(["图片"]) == 0
