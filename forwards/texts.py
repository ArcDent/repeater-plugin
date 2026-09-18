"""转发域注入文案与 Planner intent（design D1/D7，纯逻辑）。

**转发内容注入**、**转发留痕注入**与**强制发言**的可见文案都从这里构造，
`plugin.py` 只调用；文本风格与复读 / 表情回应两域的注入文案一致，带方括号
来源标签。来源群与发送者字段统一从**转发源消息**快照（`ForwardSource`）取，
不逐字段透传。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..repeats import clamp_non_negative
from .nodes import SEGMENT_PLACEHOLDERS, ForwardNode, extract_at_qq
from .source import ForwardSource

_UNKNOWN_SOURCE = "未知来源群"
_EMPTY_NODE = "[空节点]"


def sender_display_name(
    sender_cardname: Any = "",
    sender_nickname: Any = "",
    sender_qq: Any = "",
) -> str:
    """发送者显示名：群名片 → 昵称 → QQ 号，三级都取不到返回空串。"""
    for candidate in (sender_cardname, sender_nickname, sender_qq):
        name = str(candidate or "").strip()
        if name:
            return name
    return ""


def _source_label(source_group_name: Any, source_group_id: Any) -> str:
    """来源群展示：群名 → 群号 → 未知。"""
    name = str(source_group_name or "").strip()
    if name:
        return f"「{name}」"
    group_id = str(source_group_id or "").strip()
    if group_id:
        return f"「群{group_id}」"
    return _UNKNOWN_SOURCE


def _segment_summary(segment: dict[str, Any]) -> str:
    """节点内段的可读摘要（非 OneBot 转换，供注入文本阅读）。"""
    segment_type = str(segment.get("type") or "").strip()
    if segment_type == "text":
        return str(segment.get("data") or "")
    if segment_type == "at":
        qq = extract_at_qq(segment)
        return f"@{qq}" if qq else ""
    if segment_type in SEGMENT_PLACEHOLDERS:
        return SEGMENT_PLACEHOLDERS[segment_type]
    if segment_type == "forward":
        return "[嵌套转发]"
    return ""


def _node_summary(node: ForwardNode) -> str:
    summary = "".join(_segment_summary(segment) for segment in node.content).strip()
    return summary or _EMPTY_NODE


def _truncate(text: str, max_chars: Any) -> str:
    """按上限截断，超长时以省略号收尾（省略号计入上限）。

    上限走 clamp_non_negative 同口径容错：负数 / 非数字 / None 一律按下限 1
    （截断上限的最小有效值）处理，不抛异常——`inject_max_chars` 由配置侧
    透传，值域不由本层把守。
    """
    limit = max(int(clamp_non_negative(max_chars)), 1)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_forward_content_text(
    source: ForwardSource,
    *,
    max_chars: Any = 800,
) -> str:
    """**转发内容注入**的可见文案：来源群 + 发送者 + 节点摘要，按上限截断。"""
    node_list = list(source.nodes)
    parts = [
        (
            f"[跨群转发] 来自{_source_label(source.group_name, source.group_id)}"
            f"的合并转发（{len(node_list)} 条）"
        )
    ]
    sender = sender_display_name(source.sender_cardname, source.sender_nickname, source.sender_qq)
    if sender:
        parts.append(f"，发送者：{sender}")
    if node_list:
        summaries = " ".join(
            f"{index}. {node.nickname}：{_node_summary(node)}"
            for index, node in enumerate(node_list, start=1)
        )
        parts.append(f"；内容：{summaries}")
    return _truncate("".join(parts), max_chars)


def build_forward_trace_text(delivered_targets: Iterable[Any]) -> str:
    """**转发留痕注入**的可见文案：只列实际投递成功的**目标群**；无成功群返回空串。"""
    groups = [str(target or "").strip() for target in delivered_targets]
    listed = [group for group in groups if group]
    if not listed:
        return ""
    return f"[转发留痕] 麦麦把这条合并转发分享到了：{'、'.join(listed)}"


def build_forced_comment_intent(source: ForwardSource) -> str:
    """**强制发言**的 Planner intent：要求麦麦针对这条转发说一句自然的话。"""
    sender = sender_display_name(source.sender_cardname, source.sender_nickname, source.sender_qq)
    sender_part = f"，发送者：{sender}" if sender else ""
    return (
        f"[强制发言] 你刚把来自{_source_label(source.group_name, source.group_id)}的"
        f"合并转发（{len(source.nodes)} 条{sender_part}）分享到了本群："
        f"必须针对这条转发说一句自然的话，可以点评或追问，不要复述原文。"
    )
