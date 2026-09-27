"""转发域注入文案与 Planner intent（design D1/D7，纯逻辑）。

**转发内容注入**、**转发留痕注入**与**强制发言**的可见文案都从这里构造，
`plugin.py` 只调用；文本风格与复读 / 表情回应两域的注入文案一致，带方括号
来源标签。来源群与发送者字段统一从**转发源消息**快照（`ForwardSource`）取，
不逐字段透传。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..repeats import clamp_non_negative
from .decision import DECIDE_FORWARD_TOOL_NAME
from .nodes import SEGMENT_PLACEHOLDERS, ForwardNode, extract_at_qq
from .source import ForwardSource

_UNKNOWN_SOURCE = "未知来源群"
_EMPTY_NODE = "[空节点]"

# 展示文案要过的字符清洗（design D3）：C0 / C1 控制字符与孤立代理项。生产
# `group_name` 曾出现损坏字节（`<\$ÿĀ\x11\x10>柴犬的新窝`）直接泄漏进文案。
_DISPLAY_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def sanitize_display_text(text: Any) -> Any:
    """剔除控制字符与孤立代理项，其余字符原样保留（design D3）。

    只做字符过滤：不 trim、不折叠空白、不做其它改写；非字符串输入原样返回——
    清洗是展示层关注点，本函数不抛异常。
    """
    if not isinstance(text, str):
        return text
    return _DISPLAY_CONTROL_CHARS.sub("", text)


def sender_display_name(
    sender_cardname: Any = "",
    sender_nickname: Any = "",
    sender_qq: Any = "",
) -> str:
    """发送者显示名：群名片 → 昵称 → QQ 号，三级都取不到返回空串；结果过清洗。"""
    for candidate in (sender_cardname, sender_nickname, sender_qq):
        name = str(sanitize_display_text(candidate) or "").strip()
        if name:
            return name
    return ""


def _source_label(source_group_name: Any, source_group_id: Any) -> str:
    """来源群展示：群名 → 群号 → 未知；群名过清洗，避免损坏字节进文案。"""
    name = str(sanitize_display_text(source_group_name) or "").strip()
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


def _rich_summary_text(rich_summary: Any) -> str:
    """**富摘要**的展示文本：strip → 清洗 → 再 strip，空值回落空串（design D3）。

    清洗只剔除控制字符与孤立代理项，不改变中文 / emoji / 标点；清洗可能在
    首尾露出新的空白（如 `"\\x11 摘要"`），再 strip 一次，避免清洗痕迹变成
    可见的首尾空白差异。富摘要为空时返回空串，调用方照旧回落节点摘要结构。
    """
    text = str(rich_summary or "").strip()
    if not text:
        return ""
    return str(sanitize_display_text(text)).strip()


def build_forward_content_text(
    source: ForwardSource,
    *,
    rich_summary: Any = "",
    max_chars: Any = 800,
) -> str:
    """**转发内容注入**的可见文案：来源群 + 发送者 + 内容主体，按上限截断。

    内容主体优先用**富摘要**（主程序 `processed_plain_text`，design D3）；为空时
    回落既有节点摘要结构——那是异常路径的兜底，正常投递不会走到。
    """
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
    summary = _rich_summary_text(rich_summary)
    if summary:
        parts.append(f"；内容：{summary}")
    elif node_list:
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


def build_forced_comment_intent(source: ForwardSource, *, rich_summary: Any = "") -> str:
    """**强制发言**的 Planner intent：要求麦麦针对这条转发说一句自然的话。

    决策材料用**富摘要**（`转发内容：…`，design D3）；为空时保持既有来源标签
    结构，不追加材料段。
    """
    sender = sender_display_name(source.sender_cardname, source.sender_nickname, source.sender_qq)
    sender_part = f"，发送者：{sender}" if sender else ""
    intent = (
        f"[强制发言] 你刚把来自{_source_label(source.group_name, source.group_id)}的"
        f"合并转发（{len(source.nodes)} 条{sender_part}）分享到了本群："
        f"必须针对这条转发说一句自然的话，可以点评或追问，不要复述原文。"
    )
    summary = _rich_summary_text(rich_summary)
    if not summary:
        return intent
    return f"{intent}转发内容：{summary}"


def build_forward_decision_intent(
    source: ForwardSource, target_group_id: Any, rich_summary: Any = ""
) -> str:
    """**决策模式**的 Planner intent：要求调用决策 Tool 回传「转发 / 不转发」与理由。

    与**强制发言** intent 同款：来源群与发送者过清洗，材料用**富摘要**，为空时
    回落基础结构、不追加材料段。`message_id` 与目标群号明确写进 intent——模型
    要把前者原样回传给决策 Tool，后者用于模型自述上下文；**票号不暴露给模型**
    （票在插件内存里按 message_id + 目标群匹配，design D4，spec 转发模式）。
    """
    sender = sender_display_name(
        source.sender_cardname, source.sender_nickname, source.sender_qq
    )
    sender_part = f"，发送者：{sender}" if sender else ""
    intent = (
        f"[转发决策] {_source_label(source.group_name, source.group_id)}有一条合并转发"
        f"（消息 ID：{source.message_id}{sender_part}）可能分享到本群"
        f"（目标群：{target_group_id}）。请决定是否转发：调用 {DECIDE_FORWARD_TOOL_NAME} "
        f"工具，message_id 传 {source.message_id}，deliver 传 true（转发）或 "
        f"false（不转发），并用一句话说明理由。"
    )
    summary = _rich_summary_text(rich_summary)
    if not summary:
        return intent
    return f"{intent}决策材料：{summary}"
