"""forwards.nodes pure-logic tests: forward-segment extraction and OneBot node
conversion."""

from __future__ import annotations

import pytest

# forwards is a subpackage of the synthetic package registered by the root
# conftest; the hyphenated repo dir cannot be imported by name directly.
from repeater_plugin.forwards.nodes import (
    ForwardNode,
    extract_forward_nodes,
    nodes_to_onebot_messages,
)

GROUP_ID = "10001"
SENDER_QQ = "20002"


def _message(*segments: dict) -> dict:
    """Build a serialized SessionMessage dict in the hook payload shape."""
    return {
        "message_id": "9001",
        "message_info": {
            "group_info": {"group_id": GROUP_ID, "group_name": "源群"},
            "user_info": {"user_id": SENDER_QQ, "user_nickname": "某人"},
        },
        "raw_message": list(segments),
    }


def _node(
    content: list | None = None,
    user_id: str | None = SENDER_QQ,
    nickname: str | None = "某人",
    cardname: str | None = None,
    message_id: str = "5001",
) -> dict:
    node: dict = {"message_id": message_id, "content": content if content is not None else []}
    if user_id is not None:
        node["user_id"] = user_id
    if nickname is not None:
        node["user_nickname"] = nickname
    if cardname is not None:
        node["user_cardname"] = cardname
    return node


def _forward(nodes: list) -> dict:
    return {"type": "forward", "data": nodes}


def _text(text: str) -> dict:
    return {"type": "text", "data": text}


# ---- extract_forward_nodes: qualification ----

def test_extract_forward_nodes_plain_text_returns_none():
    assert extract_forward_nodes(_message(_text("普通消息"))) is None


def test_extract_forward_nodes_single_node_is_extracted():
    nodes = extract_forward_nodes(_message(_forward([_node([_text("第一条")])])))
    assert nodes is not None
    assert len(nodes) == 1
    assert nodes[0] == ForwardNode(
        user_id=SENDER_QQ,
        nickname="某人",
        message_id="5001",
        content=({"type": "text", "data": "第一条"},),
    )


def test_extract_forward_nodes_no_raw_message_returns_none():
    message = _message()
    message.pop("raw_message")
    assert extract_forward_nodes(message) is None


def test_extract_forward_nodes_all_empty_content_returns_none():
    assert extract_forward_nodes(_message(_forward([_node([])]))) is None
    assert extract_forward_nodes(_message(_forward([_node(content=None, message_id="x")]))) is None


def test_extract_forward_nodes_skips_nodes_without_content():
    nodes = extract_forward_nodes(
        _message(_forward([_node([]), _node([_text("有效")], message_id="5002")]))
    )
    assert nodes is not None
    assert [node.message_id for node in nodes] == ["5002"]


def test_extract_forward_nodes_takes_first_forward_segment_only():
    nodes = extract_forward_nodes(
        _message(
            _forward([_node([_text("第一段")], message_id="5001")]),
            _forward([_node([_text("第二段")], message_id="5002")]),
        )
    )
    assert nodes is not None
    assert [node.message_id for node in nodes] == ["5001"]


def test_extract_forward_nodes_segment_type_is_case_insensitive():
    """段类型判定大小写不敏感（与请求侧统一后的语义）。"""
    nodes = extract_forward_nodes(
        _message({"type": " Forward ", "data": [_node([_text("大小写")], message_id="5003")]})
    )
    assert nodes is not None
    assert [node.message_id for node in nodes] == ["5003"]


def test_extract_forward_nodes_does_not_fall_back_to_later_forward_segments():
    """首个 forward 段无有效节点即判无节点，不向后回落（与请求侧定位策略不同）。"""
    assert (
        extract_forward_nodes(
            _message(
                _forward([_node([])]),
                _forward([_node([_text("第二段有效")], message_id="5002")]),
            )
        )
        is None
    )


def test_extract_forward_nodes_display_name_falls_back_to_nickname_then_qq():
    nodes = extract_forward_nodes(
        _message(
            _forward(
                [
                    _node([_text("a")], nickname="昵称", cardname="群名片", message_id="1"),
                    _node([_text("b")], nickname="昵称", cardname=None, message_id="2"),
                    _node([_text("c")], nickname=None, cardname=None, message_id="3"),
                ]
            )
        )
    )
    assert nodes is not None
    assert [node.nickname for node in nodes] == ["群名片", "昵称", SENDER_QQ]


def test_extract_forward_nodes_drops_non_dict_content_segments():
    nodes = extract_forward_nodes(
        _message(_forward([_node([_text("a"), "垃圾", {"no_type": 1}, {"type": ""}])]))
    )
    assert nodes is not None
    assert nodes[0].content == ({"type": "text", "data": "a"},)


@pytest.mark.parametrize("bad", [None, "text", 42, [], {"raw_message": []}])
def test_extract_forward_nodes_non_dict_input_does_not_raise(bad):
    assert extract_forward_nodes(bad) is None


def test_extract_forward_nodes_tolerates_components_container():
    message = _message()
    message["raw_message"] = {"components": [_forward([_node([_text("组件形态")])])]}
    nodes = extract_forward_nodes(message)
    assert nodes is not None
    assert nodes[0].content == ({"type": "text", "data": "组件形态"},)


def test_extract_forward_nodes_keeps_nested_forward_segment_in_content():
    inner = {"type": "forward", "data": [_node([_text("内层")], message_id="6001")]}
    nodes = extract_forward_nodes(_message(_forward([_node([_text("外层"), inner])])))
    assert nodes is not None
    assert [segment["type"] for segment in nodes[0].content] == ["text", "forward"]


# ---- nodes_to_onebot_messages ----

def _node_of(content: tuple[dict, ...], **overrides) -> ForwardNode:
    fields = {
        "user_id": SENDER_QQ,
        "nickname": "某人",
        "message_id": "5001",
        "content": content,
    }
    fields.update(overrides)
    return ForwardNode(**fields)


def test_nodes_to_onebot_messages_text_node_structure():
    assert nodes_to_onebot_messages([_node_of((_text("你好"),))]) == [
        {
            "type": "node",
            "data": {
                "user_id": SENDER_QQ,
                "nickname": "某人",
                "content": [{"type": "text", "data": {"text": "你好"}}],
            },
        }
    ]


def test_nodes_to_onebot_messages_maps_at_to_qq():
    node = _node_of(({"type": "at", "data": {"target_user_id": "30003"}},))
    assert nodes_to_onebot_messages([node])[0]["data"]["content"] == [
        {"type": "at", "data": {"qq": "30003"}}
    ]


def test_nodes_to_onebot_messages_prefers_binary_image():
    segment = {
        "type": "image",
        "data": "https://example.com/a.png",
        "hash": "abc",
        "binary_data_base64": "aGVsbG8=",
    }
    assert nodes_to_onebot_messages([_node_of((segment,))])[0]["data"]["content"] == [
        {"type": "image", "data": {"file": "base64://aGVsbG8="}}
    ]


def test_nodes_to_onebot_messages_maps_emoji_with_binary_to_image():
    segment = {"type": "emoji", "data": "emoji_ref", "binary_data_base64": "aGk="}
    assert nodes_to_onebot_messages([_node_of((segment,))])[0]["data"]["content"] == [
        {"type": "image", "data": {"file": "base64://aGk="}}
    ]


def test_nodes_to_onebot_messages_falls_back_to_segment_reference():
    segment = {"type": "image", "data": "https://example.com/a.png"}
    assert nodes_to_onebot_messages([_node_of((segment,))])[0]["data"]["content"] == [
        {"type": "image", "data": {"file": "https://example.com/a.png"}}
    ]


def test_nodes_to_onebot_messages_media_without_reference_becomes_text_placeholder():
    image = {"type": "image", "hash": "abc"}
    emoji = {"type": "emoji", "hash": "def"}
    content = nodes_to_onebot_messages([_node_of((image, emoji))])[0]["data"]["content"]
    assert content == [
        {"type": "text", "data": {"text": "[图片]"}},
        {"type": "text", "data": {"text": "[表情]"}},
    ]


def test_nodes_to_onebot_messages_nested_forward_becomes_inner_node_list():
    inner = [_node([_text("嵌套")], user_id="1", nickname="内层", message_id="6")]
    nested = {"type": "forward", "data": inner}
    content = nodes_to_onebot_messages([_node_of((nested,))])[0]["data"]["content"]
    assert content == [
        {
            "type": "node",
            "data": {
                "user_id": "1",
                "nickname": "内层",
                "content": [{"type": "text", "data": {"text": "嵌套"}}],
            },
        }
    ]


def test_nodes_to_onebot_messages_nested_forward_type_is_case_insensitive():
    """嵌套 forward 段判定的段类型大小写不敏感（与提取侧统一）。"""
    nested = {"type": "FORWARD", "data": [_node([_text("嵌套")], user_id="1", message_id="6")]}
    content = nodes_to_onebot_messages([_node_of((nested,))])[0]["data"]["content"]
    assert [segment["type"] for segment in content] == ["node"]


def test_nodes_to_onebot_messages_skips_unmapped_and_empty_segments():
    node = _node_of(({"type": "reply", "data": {"target_message_id": "1"}}, _text("")))
    assert nodes_to_onebot_messages([node]) == []


def test_nodes_to_onebot_messages_invalid_input_returns_empty_list():
    assert nodes_to_onebot_messages(None) == []
    assert nodes_to_onebot_messages([]) == []


def test_nodes_to_onebot_messages_drops_node_without_content():
    assert nodes_to_onebot_messages([_node_of(())]) == []

