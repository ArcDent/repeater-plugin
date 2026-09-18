"""转发节点解析与 OneBot node 转换（纯逻辑）。

从主程序 hook 侧的序列化 SessionMessage 里取出**转发节点**，再按
`send_group_forward_msg` 的 `messages` 参数形态重建节点。不依赖 SDK，
可单测；Hook 声明与编排留在 `plugin.py`。

段定位与节点级校验共用叶子模块 `forwards/forward_segments.py`（与请求侧
`forwards/request/core/messages.py` 同一份实现），本模块只保留 Hook 侧的
字段映射：显示名三级回落与内容段类型过滤。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 复读域的消息段解析与转发域共用同一份 hook 序列化形态，直接复用不另起副本。
from ..repeats import message_segments
from .forward_segments import (
    RawForwardNode,
    is_forward_segment,
    iter_forward_nodes,
    normalize_forward_nodes,
)

# 段类型 → 文本占位文案的唯一来源：image / emoji 都按 OneBot image 段重建，
# 取不到媒体来源时降级为本映射里的占位文本；键集同时是「按图片重建的段类型」
# 判定集。forwards/texts.py 的段摘要直接引用本映射，不另起副本。
SEGMENT_PLACEHOLDERS: dict[str, str] = {"image": "[图片]", "emoji": "[表情]"}


@dataclass(frozen=True)
class ForwardNode:
    """一个已归一的**转发节点**。

    user_id / message_id 取自节点的原始发送者与消息标识；nickname 是节点的
    显示名，按 群名片 → 昵称 → QQ 号 三级回落（主程序 planner 侧惯例）。
    content 只保留形态可用的段（dict 且 type 非空），顺序原样。
    """

    user_id: str
    nickname: str
    message_id: str
    content: tuple[dict[str, Any], ...]


def _to_forward_nodes(raw_nodes: list[RawForwardNode]) -> list[ForwardNode]:
    """原始节点 → Hook 侧 **转发节点**（显示名三级回落 + 段类型过滤）。

    段类型为空的段被丢弃，因此叶子模块按「至少含一个 dict 段」判有效的
    节点可能在这里变空，此时整条节点跳过。

    Args:
        raw_nodes: 叶子模块抽出的原始节点列表。

    Returns:
        供 Hook 侧使用的转发节点列表；全部节点无有效内容时为空列表。
    """

    nodes: list[ForwardNode] = []
    for raw in raw_nodes:
        content = tuple(
            segment for segment in raw.content if str(segment.get("type") or "").strip()
        )
        if not content:
            continue
        user_id = raw.user_id.strip()
        nodes.append(
            ForwardNode(
                user_id=user_id,
                nickname=raw.user_cardname.strip() or raw.user_nickname.strip() or user_id,
                message_id=raw.message_id.strip(),
                content=content,
            )
        )
    return nodes


def extract_forward_nodes(message: Any) -> list[ForwardNode] | None:
    """从一条序列化 SessionMessage 里提取首个 `forward` 段的**转发节点**。

    段定位、段类型判定（大小写不敏感）与节点级校验在叶子模块
    `forwards/forward_segments.py`；本函数只做 Hook 侧字段映射，并保持
    「只看首个 `forward` 段、不向后回落」的定位策略。没有 `forward` 段、
    节点全无效或消息形态不认识时返回 None；非 dict 输入不抛异常。调用方
    据此判定这条消息是否参与转发域。
    """
    if not isinstance(message, dict):
        return None
    segments = message_segments(message.get("raw_message"))
    if segments is None:
        return None
    first_segment_nodes = next(iter_forward_nodes(segments), None)
    if first_segment_nodes is None:
        return None
    return _to_forward_nodes(first_segment_nodes) or None


def _text_segment(text: str) -> dict[str, Any]:
    """OneBot text 段（`text/plain` 放在 data.text 里）。"""
    return {"type": "text", "data": {"text": text}}


def _media_segment(segment_type: str, segment: dict[str, Any]) -> dict[str, Any]:
    """image / emoji 段 → OneBot image 段。

    优先用 `binary_data_base64` 转 `base64://`，其次用段内引用（`data` 字段），
    两者皆无时降级为文本占位。
    """
    binary = str(segment.get("binary_data_base64") or "").strip()
    if binary:
        return {"type": "image", "data": {"file": f"base64://{binary}"}}
    reference = str(segment.get("data") or "").strip()
    if reference:
        return {"type": "image", "data": {"file": reference}}
    return _text_segment(SEGMENT_PLACEHOLDERS[segment_type])


def extract_at_qq(segment: dict[str, Any]) -> str:
    """从 at 段里取目标 QQ（`target_user_id` → `qq` → 段内裸值），取不到返回空串。

    **跨模块共用**：OneBot at 段重建（`_at_segment`）与注入文本的段摘要
    （forwards/texts.py）共用同一份取 QQ 归一，不另起副本。
    """
    data = segment.get("data")
    if isinstance(data, dict):
        return str(data.get("target_user_id") or data.get("qq") or "").strip()
    return str(data or "").strip()


def _at_segment(segment: dict[str, Any]) -> dict[str, Any] | None:
    """at 段 → OneBot at 段；取不到 QQ 时返回 None（整段丢弃）。"""
    qq = extract_at_qq(segment)
    if not qq:
        return None
    return {"type": "at", "data": {"qq": qq}}


def _segment_to_onebot(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """一个节点内段 → OneBot 段列表（嵌套 forward 展开为内层 node 列表）。"""
    segment_type = str(segment.get("type") or "").strip()
    if segment_type == "text":
        text = str(segment.get("data") or "")
        return [_text_segment(text)] if text else []
    if segment_type == "at":
        at = _at_segment(segment)
        return [at] if at is not None else []
    if segment_type in SEGMENT_PLACEHOLDERS:
        return [_media_segment(segment_type, segment)]
    if is_forward_segment(segment):
        return nodes_to_onebot_messages(
            _to_forward_nodes(normalize_forward_nodes(segment.get("data")))
        )
    return []


def nodes_to_onebot_messages(nodes: Any) -> list[dict[str, Any]]:
    """**转发节点**列表 → `send_group_forward_msg` 的 `messages` 参数。

    每个节点输出 `{type: node, data: {user_id, nickname, content}}`，content 为
    OneBot 段列表；内容重建不出的节点整条跳过（不投递空节点）。
    """
    if not isinstance(nodes, (list, tuple)):
        return []
    messages: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, ForwardNode):
            continue
        content: list[dict[str, Any]] = []
        for segment in node.content:
            content.extend(_segment_to_onebot(segment))
        if not content:
            continue
        messages.append(
            {
                "type": "node",
                "data": {
                    "user_id": node.user_id,
                    "nickname": node.nickname,
                    "content": content,
                },
            }
        )
    return messages
