"""Host 合并转发消息解析的回归测试。"""

from __future__ import annotations

from repeater_plugin.forwards.request.core.messages import ForwardMessageParser

from tests.forwards.request.support import build_forward_message


def test_extract_forward_payload_preserves_nodes_and_binary_data() -> None:
    """验证合并转发解析同时生成上下文段和发送节点。

    期望解析结果保留 ``forward`` 类型、节点昵称以及图片
    ``binary_data_base64``。该测试防止消息规范化过程中丢失发送者信息或
    媒体二进制数据，导致目标群收到不完整内容。
    """

    payload = ForwardMessageParser.extract(build_forward_message())
    assert payload is not None
    segment, messages = payload
    assert segment["type"] == "forward"
    assert messages[0]["nickname"] == "群友甲"
    assert messages[0]["segments"][1]["binary_data_base64"] == "aW1hZ2U="


def test_extract_forward_segment_type_is_case_insensitive() -> None:
    """验证段类型判定大小写不敏感（与 Hook 侧统一后的语义）。

    该测试锁定共享叶子模块的 `forward` 段判定口径：大小写变体与两侧空白
    都不得让一条有效的合并转发消息被误判为普通消息。
    """

    message = build_forward_message()
    message["raw_message"][0]["type"] = " FORWARD "
    payload = ForwardMessageParser.extract(message)
    assert payload is not None
    assert payload[0]["type"] == "forward"


def test_extract_forward_payload_skips_forward_segment_without_valid_nodes() -> None:
    """验证首个 forward 段无有效节点时继续向后取段（请求侧定位策略）。

    Hook 侧「只看首个 forward 段」，请求侧取首个**含有效节点**的段；该差异
    是两侧既有契约，共享叶子模块通过逐段产出节点列表保留它。
    """

    message = build_forward_message()
    message["raw_message"].insert(0, {"type": "forward", "data": [{"content": []}]})
    payload = ForwardMessageParser.extract(message)
    assert payload is not None
    assert payload[1][0]["message_id"] == "node-1"


def test_extract_forward_payload_uses_original_index_as_placeholder_id() -> None:
    """验证占位消息 ID 与昵称回落按原始下标分配（请求侧对外契约）。"""

    message = build_forward_message()
    raw_nodes = message["raw_message"][0]["data"]
    raw_nodes.insert(0, {"content": "非法节点"})
    raw_nodes[1].pop("user_nickname")
    raw_nodes[1].pop("message_id")
    payload = ForwardMessageParser.extract(message)
    assert payload is not None
    _, messages = payload
    assert messages[0]["nickname"] == "未知用户"
    assert messages[0]["message_id"] == "forward_node_1"
