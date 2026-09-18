"""Host 合并转发消息解析。"""

from __future__ import annotations

from typing import Any

from ...forward_segments import RawForwardNode, iter_forward_nodes


class ForwardMessageParser:
    """将 Host 消息转换为可发送的合并转发数据。"""

    @staticmethod
    def extract(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """从 Host 消息中提取首个有效的合并转发消息段。

        解析器按 ``raw_message`` 顺序扫描，取首个含有效节点的 ``forward``
        段（大小写不敏感的段类型判定），跳过非 ``forward`` 段、空节点和
        缺少有效消息内容的节点。段定位与节点级校验共用叶子模块
        ``forwards/forward_segments.py``，本类只做 Host 侧字段映射。媒体段
        中的二进制字段会原样复制，以便后续 ``ctx.send.forward`` 重建消息。

        Args:
            message: ``ctx.message.get_by_id`` 返回的 Host 消息字典。

        Returns:
            成功时返回二元组：第一个元素是适合写入 Maisaka 上下文的
            ``{"type": "forward", "data": ...}`` 消息段；第二个元素是
            适合传给 ``ctx.send.forward`` 的节点列表。没有有效合并转发
            段时返回 ``None``。
        """

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return None

        for raw_nodes in iter_forward_nodes(raw_message):
            payload = ForwardMessageParser._map_raw_nodes(raw_nodes)
            if payload is not None:
                return payload
        return None

    @staticmethod
    def _map_raw_nodes(
        raw_nodes: list[RawForwardNode],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """把叶子模块抽出的原始节点映射为上下文与发送两种表示。

        无效节点已被叶子模块跳过。缺失昵称时使用“未知用户”，缺失消息 ID 时
        按节点在段 ``data`` 中的原始下标生成稳定的占位 ID；消息内容中的未知
        字段和媒体二进制数据会通过浅拷贝保留。

        Args:
            raw_nodes: 叶子模块抽出的原始节点列表。

        Returns:
            至少存在一个有效节点时，返回上下文消息段与发送节点列表组成的
            二元组；所有节点均无效时返回 ``None``。
        """

        normalized_nodes: list[dict[str, Any]] = []
        forward_messages: list[dict[str, Any]] = []
        for raw_node in raw_nodes:
            node = {
                "user_id": raw_node.user_id,
                "user_nickname": raw_node.user_nickname or "未知用户",
                "user_cardname": raw_node.user_cardname,
                "message_id": raw_node.message_id or f"forward_node_{raw_node.index}",
                "content": list(raw_node.content),
            }
            normalized_nodes.append(node)
            forward_messages.append(
                {
                    "user_id": node["user_id"],
                    "nickname": node["user_nickname"],
                    "user_cardname": node["user_cardname"],
                    "message_id": node["message_id"],
                    "segments": node["content"],
                }
            )

        if not normalized_nodes:
            return None
        return {"type": "forward", "data": normalized_nodes}, forward_messages
