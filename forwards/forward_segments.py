"""转发段解析核心：定位 `forward` 段并抽出节点原始字段（叶子模块）。

Hook 侧（`forwards/nodes.py`）与请求侧（`forwards/request/core/messages.py`）
共用这一份「段类型判定 + 首个 `forward` 段定位 + 节点级校验」，两侧只保留
各自的字段映射（昵称回落、占位 ID、媒体字段保留策略）。

本模块是叶子：不依赖 `repeats.py`、不依赖 `forwards/nodes.py`。请求侧因此
不必碰依赖 `repeats` 的 `forwards/nodes.py`，保住 `import forwards.request`
在裸目录下的独立导入。

`forward` 段判定统一为大小写不敏感（`.strip().lower()`），是两侧唯一的
段类型语义。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

FORWARD_SEGMENT_TYPE = "forward"


@dataclass(frozen=True)
class RawForwardNode:
    """一个通过节点级校验的**转发节点**原始字段（未做任何映射）。

    `index` 是节点在段 `data` 列表中的下标（含被跳过的无效节点），供请求侧
    生成占位消息 ID；`content` 是浅拷贝过的 dict 段元组，段类型过滤留给各自
    的映射层（Hook 侧要求段类型非空，请求侧保留全部 dict 段）。
    """

    index: int
    user_id: str
    user_cardname: str
    user_nickname: str
    message_id: str
    content: tuple[dict[str, Any], ...]


def is_forward_segment(segment: Any) -> bool:
    """判定一个消息段是否为 `forward` 段（只认段类型，不校验 `data` 形态）。

    Args:
        segment: 序列化消息中的任意段。

    Returns:
        段为 dict 且类型字段 trim + 小写后等于 `forward` 时返回 `True`。
    """

    return (
        isinstance(segment, dict)
        and str(segment.get("type") or "").strip().lower() == FORWARD_SEGMENT_TYPE
    )


def normalize_forward_nodes(raw_nodes: Any) -> list[RawForwardNode]:
    """逐节点校验并抽出原始字段，不改写字段内容。

    校验口径：节点为 dict、`content` 为列表且至少含一个 dict 段；段类型不在
    本层过滤，由各自的映射层按需处理（两侧对外契约不同）。`index` 在跳过
    无效节点前分配，保持请求侧占位 ID 与原始下标一致。

    Args:
        raw_nodes: `forward` 段的 `data` 字段。

    Returns:
        通过校验的原始节点列表；`raw_nodes` 非法或无有效节点时为空列表。
    """

    if not isinstance(raw_nodes, list):
        return []
    nodes: list[RawForwardNode] = []
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            continue
        raw_content = raw_node.get("content")
        if not isinstance(raw_content, list):
            continue
        content = tuple(dict(segment) for segment in raw_content if isinstance(segment, dict))
        if not content:
            continue
        nodes.append(
            RawForwardNode(
                index=index,
                user_id=str(raw_node.get("user_id") or ""),
                user_cardname=str(raw_node.get("user_cardname") or ""),
                user_nickname=str(raw_node.get("user_nickname") or ""),
                message_id=str(raw_node.get("message_id") or ""),
                content=content,
            )
        )
    return nodes


def iter_forward_nodes(segments: Any) -> Iterator[list[RawForwardNode]]:
    """按序产出每个 `forward` 段的节点列表（段内无有效节点时为空列表）。

    空列表与「没有下一个 `forward` 段」可区分，调用方据此选择定位策略：
    Hook 侧取首个 `forward` 段（不向后回落），请求侧取首个有节点的段。

    Args:
        segments: 序列化消息的段列表。

    Yields:
        每个 `forward` 段对应的原始节点列表，顺序与段顺序一致。
    """

    if not isinstance(segments, (list, tuple)):
        return
    for segment in segments:
        if is_forward_segment(segment):
            yield normalize_forward_nodes(segment.get("data"))
