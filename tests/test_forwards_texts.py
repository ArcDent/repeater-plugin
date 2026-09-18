"""forwards.texts pure-logic tests: injection texts and Planner intent."""

from __future__ import annotations

from repeater_plugin.forwards.nodes import ForwardNode
from repeater_plugin.forwards.source import ForwardSource
from repeater_plugin.forwards.texts import (
    build_forced_comment_intent,
    build_forward_content_text,
    build_forward_trace_text,
    sender_display_name,
)


def _node(
    *content: dict,
    user_id: str = "20002",
    nickname: str = "某人",
    message_id: str = "5001",
) -> ForwardNode:
    return ForwardNode(
        user_id=user_id,
        nickname=nickname,
        message_id=message_id,
        content=tuple(content),
    )


def _text(text: str) -> dict:
    return {"type": "text", "data": text}


def _source(
    *nodes: ForwardNode,
    group_id: str = "10001",
    group_name: str = "",
    sender_cardname: str = "",
    sender_nickname: str = "",
    sender_qq: str = "",
) -> ForwardSource:
    """文案测试用的**转发源消息**快照：只给关心的字段，其余取默认。"""
    return ForwardSource(
        group_id=group_id,
        group_name=group_name,
        message_id="90001",
        nodes=tuple(nodes),
        sender_cardname=sender_cardname,
        sender_nickname=sender_nickname,
        sender_qq=sender_qq,
    )


# ---- sender_display_name ----

def test_sender_display_name_prefers_cardname_then_nickname_then_qq():
    assert sender_display_name("群名片", "昵称", "20002") == "群名片"
    assert sender_display_name("", "昵称", "20002") == "昵称"
    assert sender_display_name("", "", "20002") == "20002"
    assert sender_display_name("  ", "  ", "20002") == "20002"


def test_sender_display_name_without_any_field_is_empty():
    assert sender_display_name() == ""
    assert sender_display_name("", "", "") == ""


# ---- build_forward_content_text ----

def test_content_text_names_source_group_sender_and_nodes():
    text = build_forward_content_text(
        _source(_node(_text("第一条消息"), nickname="张三"), group_name="源群", sender_cardname="李四")
    )
    assert "源群" in text
    assert "李四" in text
    assert "第一条消息" in text
    assert "1" in text


def test_content_text_falls_back_to_source_group_id_and_sender_qq():
    text = build_forward_content_text(
        _source(_node(_text("内容")), sender_qq="20002")
    )
    assert "10001" in text
    assert "20002" in text


def test_content_text_summarizes_media_and_mentions():
    text = build_forward_content_text(
        _source(
            _node(
                _text("看图"),
                {"type": "image", "data": "https://example.com/a.png"},
                {"type": "emoji", "data": "ref"},
                {"type": "at", "data": {"target_user_id": "30003"}},
            )
        )
    )
    assert "[图片]" in text
    assert "[表情]" in text
    assert "30003" in text


def test_content_text_truncates_to_limit_with_marker():
    long_text = "长" * 500
    text = build_forward_content_text(
        _source(_node(_text(long_text))),
        max_chars=40,
    )
    assert len(text) <= 40
    assert text.endswith("…")


def test_content_text_short_content_is_not_truncated():
    text = build_forward_content_text(
        _source(_node(_text("短内容")), group_name="源群"),
        max_chars=800,
    )
    assert not text.endswith("…")
    assert "短内容" in text


def test_content_text_clamps_invalid_max_chars_instead_of_raising():
    # 非数值上限走 clamp 口径（按 0 → 下限 1），不再抛异常。
    long_text = "长" * 500
    for bad_limit in ("垃圾", None, -5):
        text = build_forward_content_text(
            _source(_node(_text(long_text))),
            max_chars=bad_limit,
        )
        assert len(text) <= 1


def test_content_text_with_no_nodes_still_describes_the_message():
    text = build_forward_content_text(_source())
    assert "10001" in text


def test_content_text_marks_node_without_readable_segments():
    text = build_forward_content_text(_source(_node()))
    assert "[空节点]" in text


# ---- build_forward_trace_text ----

def test_trace_text_lists_only_delivered_targets():
    text = build_forward_trace_text(["20001", "20002"])
    assert "20001" in text
    assert "20002" in text
    assert "20003" not in text


def test_trace_text_without_targets_is_empty():
    assert build_forward_trace_text([]) == ""
    assert build_forward_trace_text(["", "  "]) == ""


# ---- build_forced_comment_intent ----

def test_forced_comment_intent_demands_speaking():
    text = build_forced_comment_intent(
        _source(_node(), _node(), group_name="源群", sender_cardname="李四")
    )
    assert "必须" in text
    assert "源群" in text
    assert "李四" in text
    assert "2" in text


def test_forced_comment_intent_without_names_still_works():
    text = build_forced_comment_intent(_source(_node()))
    assert "必须" in text
