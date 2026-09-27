"""forwards.texts pure-logic tests: injection texts and Planner intent."""

from __future__ import annotations

from repeater_plugin.forwards.decision import DECIDE_FORWARD_TOOL_NAME
from repeater_plugin.forwards.nodes import ForwardNode
from repeater_plugin.forwards.source import ForwardSource
from repeater_plugin.forwards.texts import (
    build_forced_comment_intent,
    build_forward_content_text,
    build_forward_decision_intent,
    build_forward_trace_text,
    sanitize_display_text,
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


# ---- sanitize_display_text (design D3) ----

def test_sanitize_display_text_removes_c0_and_c1_control_chars():
    assert sanitize_display_text("柴\x11犬\x00的新巢") == "柴犬的新巢"
    assert sanitize_display_text("a\x7fb\x9fc") == "abc"


def test_sanitize_display_text_keeps_normal_text_and_emoji():
    assert sanitize_display_text("你好，世界 😀⭐") == "你好，世界 😀⭐"


def test_sanitize_display_text_removes_lone_surrogates():
    assert sanitize_display_text("坏\ud800字节\udfff") == "坏字节"


def test_sanitize_display_text_does_not_trim_or_collapse_whitespace():
    assert sanitize_display_text("  两 段  空 格  ") == "  两 段  空 格  "


def test_sanitize_display_text_tolerates_empty_and_non_string():
    assert sanitize_display_text("") == ""
    assert sanitize_display_text(None) is None
    assert sanitize_display_text(42) == 42


def test_content_text_sanitizes_source_group_name():
    text = build_forward_content_text(_source(_node(_text("内容")), group_name="源\x11群"))
    assert "源群" in text
    assert "\x11" not in text


def test_content_text_sanitizes_sender_display_name():
    text = build_forward_content_text(_source(_node(_text("内容")), sender_cardname="李\x11四"))
    assert "李四" in text
    assert "\x11" not in text


def test_forced_comment_intent_sanitizes_source_group_name():
    text = build_forced_comment_intent(_source(_node(), group_name="源\x11群"))
    assert "源群" in text
    assert "\x11" not in text


def test_content_text_sanitizes_rich_summary():
    """design D3：富摘要和群名一样要过清洗，控制字符不得泄漏进注入文案。"""
    text = build_forward_content_text(
        _source(_node(_text("节点摘要")), group_name="源群"),
        rich_summary="富\x11摘要\x0d",
    )
    assert "富摘要" in text
    assert "\x11" not in text


def test_content_text_sanitizes_lone_surrogates_in_rich_summary():
    text = build_forward_content_text(
        _source(_node(_text("节点摘要")), group_name="源群"),
        rich_summary="坏\ud800字节",
    )
    assert "坏字节" in text
    assert "\ud800" not in text


def test_forced_comment_intent_sanitizes_rich_summary():
    text = build_forced_comment_intent(
        _source(_node(), group_name="源群"),
        rich_summary="富\x11摘要\x0d",
    )
    assert "转发内容：富摘要" in text
    assert "\x11" not in text


def test_forced_comment_intent_sanitizes_lone_surrogates_in_rich_summary():
    text = build_forced_comment_intent(
        _source(_node(), group_name="源群"),
        rich_summary="坏\ud800字节",
    )
    assert "转发内容：坏字节" in text
    assert "\ud800" not in text


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


# ---- rich summary body (design D3) ----

def test_content_text_uses_rich_summary_as_body():
    text = build_forward_content_text(
        _source(_node(_text("节点摘要")), group_name="源群", sender_cardname="李四"),
        rich_summary="【合并转发消息】张三：你好",
    )
    assert "；内容：【合并转发消息】张三：你好" in text
    assert "源群" in text
    assert "李四" in text
    assert "节点摘要" not in text


def test_content_text_truncates_rich_summary_to_limit():
    text = build_forward_content_text(
        _source(_node(_text("节点摘要"))),
        rich_summary="长" * 500,
        max_chars=40,
    )
    assert len(text) <= 40
    assert text.endswith("…")


def test_content_text_without_rich_summary_keeps_v06_structure():
    text = build_forward_content_text(
        _source(_node(_text("第一条"), nickname="张三"), group_name="源群", sender_cardname="李四"),
        rich_summary="",
    )
    assert text == "[跨群转发] 来自「源群」的合并转发（1 条），发送者：李四；内容：1. 张三：第一条"


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


def test_forced_comment_intent_carries_rich_summary_and_demand():
    text = build_forced_comment_intent(
        _source(_node(), group_name="源群", sender_cardname="李四"),
        rich_summary="【合并转发消息】张三：你好",
    )
    assert "必须" in text
    assert "转发内容：【合并转发消息】张三：你好" in text


def test_forced_comment_intent_without_rich_summary_keeps_v06_structure():
    text = build_forced_comment_intent(
        _source(_node(), group_name="源群", sender_cardname="李四"),
        rich_summary="",
    )
    assert text == (
        "[强制发言] 你刚把来自「源群」的合并转发（1 条，发送者：李四）分享到了本群："
        "必须针对这条转发说一句自然的话，可以点评或追问，不要复述原文。"
    )


# ---- build_forward_decision_intent (spec 转发模式 / design D4) ----

def test_decision_intent_names_source_sender_message_id_target_and_rich_summary():
    text = build_forward_decision_intent(
        _source(_node(), group_name="源群", sender_cardname="李四"),
        "20001",
        rich_summary="【合并转发消息】张三：你好",
    )
    assert "[转发决策]" in text
    assert "源群" in text  # 来源群标签
    assert "李四" in text  # 发送者标签
    assert "90001" in text  # 模型要原样回传的 message_id
    assert "20001" in text  # 目标群号
    assert "决策材料：【合并转发消息】张三：你好" in text
    assert DECIDE_FORWARD_TOOL_NAME in text and "deliver" in text


def test_decision_intent_without_rich_summary_has_no_material_section():
    """富摘要为空：回落基础 intent（不出现空的「决策材料：」尾巴），与强制发言同款。"""
    text = build_forward_decision_intent(_source(_node(), group_name="源群"), "20001")
    assert "决策材料" not in text
    assert "90001" in text and "20001" in text


def test_decision_intent_falls_back_to_group_id_and_unknown_source():
    assert "「群10001」" in build_forward_decision_intent(_source(_node()), "20001")
    assert "未知来源群" in build_forward_decision_intent(
        _source(_node(), group_id=""), "20001"
    )


def test_decision_intent_sanitizes_group_name_sender_and_rich_summary():
    """控制字符不得经来源字段或富摘要泄漏进 intent（design D3）。"""
    text = build_forward_decision_intent(
        _source(_node(), group_name="源\x11群", sender_cardname="李\x10四"),
        "20001",
        rich_summary="富\x11摘要\x0d",
    )
    assert "源群" in text and "李四" in text and "富摘要" in text
    assert "\x11" not in text and "\x10" not in text and "\x0d" not in text
