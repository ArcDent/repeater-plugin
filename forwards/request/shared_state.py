"""两条转发路径共用的状态接口（change design D4/D5）。

**已投递状态**与**群可用性缓存**的实现在 `forwards.state`（自动路径与请求
路径共用同一实例，由 `plugin.py` 装配）。本模块只声明请求转发链路消费的
窄接口与共享键构造：

- 请求路径不直接导入 `forwards.state`，避免顶层 `import forwards.request`
  时被 `forwards.state` 的插件包内相对导入拖累；
- `build_delivered_key` 与 `ForwardStateStore.build_state_key` 同源，保证
  自动路径登记、请求路径查询使用同一个键格式。
"""

from __future__ import annotations

from typing import Any, Protocol

from .core.state import ForwardStateStore


class DeliveredRegistryLike(Protocol):
    """**已投递状态**的读取面（实现见 `forwards.state.DeliveredRegistry`）。"""

    def is_delivered(self, source_key: Any, group_id: Any, now: float) -> bool:
        """该**转发源消息**是否已投递到该**目标群**。"""
        ...

    def targets_for(self, source_key: Any, now: float) -> list[str]:
        """该**转发源消息**已投递的**目标群**列表。"""
        ...


class GroupAvailabilityCacheLike(Protocol):
    """**群可用性缓存**的读取面（实现见 `forwards.state.GroupAvailabilityCache`）。"""

    def is_available(self, group_id: Any, now: float) -> bool:
        """该群是否在机器人群列表中；未知（无数据或已过期）一律返回 ``True``。"""
        ...


def build_delivered_key(source_stream_id: str, source_message_id: str) -> str:
    """构造**已投递状态**的共享键：源聊天流 + **转发源消息** ID。

    **自动转发**投递成功后按该键登记，**请求转发**投递前按该键查询，
    两条路径必须使用同一个构造入口。

    Args:
        source_stream_id: 源消息所在聊天流 ID。
        source_message_id: 转发源平台消息 ID。

    Returns:
        与请求路径持久化幂等键同源的稳定字符串键。
    """

    return ForwardStateStore.build_state_key(source_stream_id, source_message_id)
