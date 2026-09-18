"""转发域状态（design D1/D5，纯逻辑、全同步方法）。

**转发防重**与两处**转发冷却**（源群 / 目标群）的内存态，两处冷却与其它
行为域各自计时互不牵扯；状态实例随插件生命周期存续，不做持久化。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# 群冷却时钟与表情回应 / 复读域同款组件，各域各持独立实例。
from ..reactions import GroupCooldown


class ForwardState:
    """**转发源消息**防重与**转发冷却**。

    防重按消息 ID、冷却按群号，TTL 都由调用方按配置传入（0 表示停用该
    防护）。全部方法纯同步、无 await 点：门禁与掷骰在同一同步段内完成，
    同一事件循环上并发调度的 handler 不会对同一条消息重复掷骰。
    """

    def __init__(self) -> None:
        self._messages: dict[str, float] = {}
        self._group_cooldown = GroupCooldown()
        self._target_cooldown = GroupCooldown()

    def is_duplicate(self, message_id: Any, ttl_seconds: float, now: float) -> bool:
        """该**转发源消息**是否仍在**转发防重**窗口内。"""
        ttl = max(float(ttl_seconds), 0.0)
        if ttl <= 0.0:
            return False
        marked_at = self._messages.get(str(message_id or "").strip())
        return marked_at is not None and now - marked_at < ttl

    def mark_message(self, message_id: Any, now: float) -> None:
        """记录一条**转发源消息**已参与掷骰，作为防重窗口起点。"""
        key = str(message_id or "").strip()
        if key:
            self._messages[key] = now

    def is_group_cooling(self, group_id: Any, cooldown_seconds: float, now: float) -> bool:
        """**源群**是否处于**转发冷却**。"""
        return self._group_cooldown.is_cooling_down(group_id, cooldown_seconds, now)

    def mark_group(self, group_id: Any, now: float) -> None:
        """记录**源群**冷却起点（至少一个**目标群**命中转发时写入）。"""
        self._group_cooldown.mark_group(group_id, now)

    def is_target_cooling(self, group_id: Any, cooldown_seconds: float, now: float) -> bool:
        """**目标群**是否处于**转发冷却**。"""
        return self._target_cooldown.is_cooling_down(group_id, cooldown_seconds, now)

    def mark_targets(self, group_ids: Iterable[Any], now: float) -> None:
        """记录全部命中**转发**出口的**目标群**冷却起点。

        只影响实际命中（含投递失败）的群：沉默的目标群不被写入，冷却期内的
        目标群由调用方在掷骰前剔除。单传一个群号字符串也按一个群处理。
        """
        targets = [group_ids] if isinstance(group_ids, str) else group_ids
        for group_id in targets:
            self._target_cooldown.mark_group(group_id, now)


class DeliveredRegistry:
    """**已投递状态**：**转发源消息** → 已投递**目标群**集合（design D5）。

    键由调用方拼装（源聊天流 + 消息 ID）；同一键的多次登记做并集，TTL 从
    最后一次登记起算。**自动转发**投递成功后登记，**请求转发**投递前查询以
    拒绝重复。TTL <= 0 表示不记忆（与**转发防重**同一口径）。
    """

    def __init__(self, ttl_seconds: float = 1800.0) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self._targets: dict[str, list[str]] = {}
        self._marked_at: dict[str, float] = {}

    def register(self, source_key: Any, targets: Iterable[Any], now: float) -> None:
        """登记一次成功投递（并集去重、保持首次出现顺序，刷新 TTL 起点）。

        单传一个群号字符串也按一个目标群处理。
        """
        key = str(source_key or "").strip()
        if not key or self.ttl_seconds <= 0.0:
            return
        self._prune(now)
        delivered = self._targets.setdefault(key, [])
        candidates = [targets] if isinstance(targets, str) else targets
        for raw_group_id in candidates:
            group_id = str(raw_group_id or "").strip()
            if group_id and group_id not in delivered:
                delivered.append(group_id)
        self._marked_at[key] = now

    def is_delivered(self, source_key: Any, group_id: Any, now: float) -> bool:
        """该**转发源消息**是否已投递到该**目标群**（TTL 内）。"""
        return str(group_id or "").strip() in self.targets_for(source_key, now)

    def targets_for(self, source_key: Any, now: float) -> list[str]:
        """该**转发源消息**已投递的**目标群**列表；未登记或已过期为 []。"""
        key = str(source_key or "").strip()
        marked_at = self._marked_at.get(key)
        if marked_at is None or not self._is_fresh(marked_at, now):
            return []
        return list(self._targets.get(key, ()))

    def _is_fresh(self, marked_at: float, now: float) -> bool:
        return self.ttl_seconds > 0.0 and now - marked_at < self.ttl_seconds

    def _prune(self, now: float) -> None:
        expired = [key for key, ts in self._marked_at.items() if not self._is_fresh(ts, now)]
        for key in expired:
            self._marked_at.pop(key, None)
            self._targets.pop(key, None)


class GroupAvailabilityCache:
    """协议端群列表的内存缓存，TTL 内查表、过期或未更新视为未知（design D4）。

    未知一律放行：校验只是「减少无效投递」的优化，不允许变成新的静默失败源。
    `update([])` 视为这一轮没拿到可信列表，同样回到未知状态。
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self._groups: set[str] = set()
        self._updated_at: float | None = None

    def update(self, groups: Iterable[Any], now: float) -> None:
        """用一份协议端群列表刷新缓存；空列表或 None（查询失败）表示未知，不建立可信快照。"""
        normalized = {str(group or "").strip() for group in groups or ()}
        normalized.discard("")
        if not normalized:
            self._groups = set()
            self._updated_at = None
            return
        self._groups = normalized
        self._updated_at = now

    def is_available(self, group_id: Any, now: float) -> bool:
        """该群是否在机器人群列表中；未知（无数据或已过期）一律返回 True。"""
        if not self.has_fresh_data(now):
            return True
        return str(group_id or "").strip() in self._groups

    def has_fresh_data(self, now: float) -> bool:
        """当前是否持有一份未过期的群列表（供启动自检与日志使用）。"""
        if self._updated_at is None or self.ttl_seconds <= 0.0:
            return False
        return now - self._updated_at < self.ttl_seconds
