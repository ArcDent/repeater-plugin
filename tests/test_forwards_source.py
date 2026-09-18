"""forwards.source pure-logic tests: forward-source qualification and snapshot."""

from __future__ import annotations

from typing import Any

import pytest

# forwards is a subpackage of the synthetic package registered by the root
# conftest; the hyphenated repo dir cannot be imported by name directly.
from repeater_plugin.forwards.nodes import ForwardNode
from repeater_plugin.forwards.source import ForwardSource, parse_forward_source

BOT_QQ = "40004"
SOURCE_GROUP = "10001"
SENDER_QQ = "20002"


def _node(
    text: str = "第一条",
    *,
    user_id: str = "30003",
    nickname: str = "节点主人",
    cardname: str = "",
) -> dict:
    return {
        "user_id": user_id,
        "user_nickname": nickname,
        "user_cardname": cardname,
        "message_id": "80001",
        "content": [{"type": "text", "data": text}],
    }


def _message(
    *nodes: dict,
    group_id: Any = SOURCE_GROUP,
    group_name: Any = "源群",
    sender: Any = SENDER_QQ,
    cardname: Any = "",
    nickname: Any = "群友甲",
    **overrides: Any,
) -> dict:
    """A standard qualifying hook payload; overrides replace top-level fields.

    Passing ``None`` for a top-level field drops the key entirely (missing-field
    cases), so tests never have to hand-build payload fragments.
    """
    payload: dict[str, Any] = {
        "message_id": "9001",
        "platform": "qq",
        "is_notify": False,
        "message_info": {
            "group_info": {"group_id": group_id, "group_name": group_name},
            "user_info": {
                "user_id": sender,
                "user_nickname": nickname,
                "user_cardname": cardname,
            },
        },
        "raw_message": [{"type": "forward", "data": list(nodes) or [_node()]}],
    }
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


# ---- parse_forward_source: snapshot fields ----

def test_parse_extracts_full_snapshot():
    source = parse_forward_source(_message(), BOT_QQ)
    assert source is not None
    assert source.group_id == SOURCE_GROUP
    assert source.group_name == "源群"
    assert source.message_id == "9001"
    assert source.sender_cardname == ""
    assert source.sender_nickname == "群友甲"
    assert source.sender_qq == SENDER_QQ
    assert source.nodes == (
        ForwardNode(
            user_id="30003",
            nickname="节点主人",
            message_id="80001",
            content=({"type": "text", "data": "第一条"},),
        ),
    )
    assert isinstance(source.nodes, tuple)


def test_parse_strips_whitespace_around_fields():
    source = parse_forward_source(
        _message(
            group_id=" 10001 ",
            group_name=" 源群 ",
            sender=" 20002 ",
            cardname=" 名片 ",
            nickname=" 群友甲 ",
        ),
        BOT_QQ,
    )
    assert source == ForwardSource(
        group_id=SOURCE_GROUP,
        group_name="源群",
        message_id="9001",
        nodes=(
            ForwardNode(
                user_id="30003",
                nickname="节点主人",
                message_id="80001",
                content=({"type": "text", "data": "第一条"},),
            ),
        ),
        sender_cardname="名片",
        sender_nickname="群友甲",
        sender_qq=SENDER_QQ,
    )


def test_parse_keeps_multiple_nodes_in_order():
    source = parse_forward_source(
        _message(_node("第一条"), _node("第二条", nickname="第二人")), BOT_QQ
    )
    assert source is not None
    assert [node.content[0]["data"] for node in source.nodes] == ["第一条", "第二条"]
    assert source.nodes[1].nickname == "第二人"


# ---- parse_forward_source: qualification boundaries ----

@pytest.mark.parametrize("message", [None, "文本", [1, 2], 123, object()])
def test_parse_non_dict_input_returns_none_without_raising(message: Any):
    assert parse_forward_source(message, BOT_QQ) is None


def test_parse_notify_message_returns_none():
    assert parse_forward_source(_message(is_notify=True), BOT_QQ) is None


def test_parse_non_qq_platform_returns_none():
    assert parse_forward_source(_message(platform="wx"), BOT_QQ) is None
    assert parse_forward_source(_message(platform=None), BOT_QQ) is None


def test_parse_platform_check_is_case_insensitive():
    assert parse_forward_source(_message(platform="QQ"), BOT_QQ) is not None


def test_parse_missing_message_info_returns_none():
    assert parse_forward_source(_message(message_info=None), BOT_QQ) is None
    assert parse_forward_source(_message(message_info="垃圾"), BOT_QQ) is None


def test_parse_missing_group_or_user_info_returns_none():
    incomplete = {"group_info": {"group_id": SOURCE_GROUP}, "user_info": None}
    assert parse_forward_source(_message(message_info=incomplete), BOT_QQ) is None
    assert parse_forward_source(
        _message(message_info={"group_info": "垃圾", "user_info": {"user_id": SENDER_QQ}}), BOT_QQ
    ) is None


def test_parse_missing_group_or_sender_id_returns_none():
    assert parse_forward_source(_message(group_id=""), BOT_QQ) is None
    assert parse_forward_source(_message(group_id="  "), BOT_QQ) is None
    assert parse_forward_source(_message(sender=""), BOT_QQ) is None
    assert parse_forward_source(_message(sender="  "), BOT_QQ) is None


def test_parse_own_message_returns_none():
    assert parse_forward_source(_message(sender=BOT_QQ), BOT_QQ) is None
    assert parse_forward_source(_message(sender=BOT_QQ), f" {BOT_QQ} ") is None


def test_parse_own_message_check_only_applies_with_self_qq():
    assert parse_forward_source(_message(sender=BOT_QQ)) is not None
    assert parse_forward_source(_message(sender=BOT_QQ), "") is not None


def test_parse_without_forward_segment_returns_none():
    plain = _message(raw_message=[{"type": "text", "data": "普通消息"}])
    assert parse_forward_source(plain, BOT_QQ) is None
    assert parse_forward_source(_message(raw_message=None), BOT_QQ) is None
    assert parse_forward_source(_message(raw_message=[]), BOT_QQ) is None


def test_parse_nodes_without_content_return_none():
    nodes = [{"user_id": "30003", "content": []}, {"content": [{"type": ""}]}]
    assert parse_forward_source(_message(*nodes), BOT_QQ) is None
