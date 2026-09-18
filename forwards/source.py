"""转发源消息的资格解析（纯逻辑，design D1）。

Hook 同步段把主程序 hook 侧的序列化 SessionMessage 解析成**转发源消息**快照
（`ForwardSource`）：形态与身份资格在本模块判定（QQ 群聊、非通知消息、含至少
一个有效**转发节点**的**合并转发**、发送者不是麦麦自己）；**源群**白名单属配置
口径，由 `plugin.py` 判定。不依赖 SDK，可单测；Hook 声明与编排留在 `plugin.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .nodes import ForwardNode, extract_forward_nodes


@dataclass(frozen=True)
class ForwardSource:
    """一条参与**自动转发**的**转发源消息**快照。

    Hook 同步段解析一次、后台任务与两类注入只读快照，不再重复解析 hook
    载荷；`nodes` 是 Hook 侧已归一（显示名三级回落）的**转发节点**，直发与
    注入文案都从它重建。
    """

    group_id: str
    group_name: str
    message_id: str
    nodes: tuple[ForwardNode, ...]
    sender_cardname: str = ""
    sender_nickname: str = ""
    sender_qq: str = ""


def parse_forward_source(message: Any, self_qq: str = "") -> ForwardSource | None:
    """hook 载荷 → **转发源消息**快照；不满足转发域资格时返回 None。

    资格口径见 spec「转发源消息的资格」：QQ 群聊、非通知消息、含至少一个有效
    **转发节点**的**合并转发**、发送者不是麦麦自己。**源群**白名单是配置口径，
    由调用方判定——本函数只管形态与身份，不读配置。hook 每条群消息都过，
    解析必须便宜且不抛异常：形态不认识按非候选静默返回 None（debug 由调用方
    决定，与复读域同口径）。
    """
    if not isinstance(message, dict) or message.get("is_notify"):
        return None
    if str(message.get("platform") or "").strip().lower() != "qq":
        return None
    message_info = message.get("message_info")
    if not isinstance(message_info, dict):
        return None
    group_info = message_info.get("group_info")
    user_info = message_info.get("user_info")
    if not isinstance(group_info, dict) or not isinstance(user_info, dict):
        return None
    group_id = str(group_info.get("group_id") or "").strip()
    sender_qq = str(user_info.get("user_id") or "").strip()
    if not group_id or not sender_qq:
        return None
    if self_qq and sender_qq == str(self_qq).strip():
        return None
    nodes = extract_forward_nodes(message)
    if not nodes:
        return None
    return ForwardSource(
        group_id=group_id,
        group_name=str(group_info.get("group_name") or "").strip(),
        message_id=str(message.get("message_id") or "").strip(),
        nodes=tuple(nodes),
        sender_cardname=str(user_info.get("user_cardname") or "").strip(),
        sender_nickname=str(user_info.get("user_nickname") or "").strip(),
        sender_qq=sender_qq,
    )
