"""转发决策票：一次性决策票注册表（design D4，纯逻辑、全同步方法）。

**决策模式**下掷骰命中后不直接投递：插件为目标群签发一张一次性**决策票**，
并把**富摘要**交给该群 Planner 决策；Planner 经转发决策 Tool 回传结果时，
handler 用「消息 ID + 目标群号」在此消费（存在 / 未过期 / 未消费，消费即焚）。
票只在插件掷骰命中时生成，群友无法凭空构造；内存 dict 不做持久化，超时即失效。

票里携带一个**不透明快照**（`Any`，由调用方决定内容）：决策模式靠它把
`before_process` 时点的**转发源消息快照**与**富摘要**带到模型决策回来的时刻——
主程序入库形态不含内存二进制（ADR 0011），事后重取消息必然丢图。本模块不
import `forwards/source.py`，只当它是任意对象。
"""

from __future__ import annotations

import secrets
from typing import Any

# `forward.mode` 的合法取值（spec 转发模式）：`direct` 命中即投递，`planner`
# 命中后先请目标群 Planner 决策「转发 / 不转发」。词表定义在本层，供插件侧
# 分支与文案层共用同一字面量，避免两边各写一份。
FORWARD_MODE_DIRECT = "direct"
FORWARD_MODE_PLANNER = "planner"
FORWARD_MODES = (FORWARD_MODE_DIRECT, FORWARD_MODE_PLANNER)

# **决策模式**下要求目标群 Planner 调用的转发决策 Tool 名（spec 转发模式，design D4）：
# 插件声明 Tool 与决策 intent 文案引用同一字面量，改名时不会漏改文案。
DECIDE_FORWARD_TOOL_NAME = "decide_forward_delivery"


class DecisionTickets:
    """`message_id + 目标群` 的一次性决策票注册表（TTL 受限，携带来源快照）。

    TTL <= 0 表示停用（不签发任何票）；过期条目由 `discard_expired` 主动清理，
    `issue` / `consume` 也顺手清理，不做后台计时。全部方法纯同步、无 await 点：
    Tool handler 在异步环境
    里直接调用，校验与焚票必须在同一同步段内完成，避免同一张票并发双消费。
    **票号不暴露给模型**：消费按 `(message_id, 目标群)` 配对，模型只需回传
    message_id，目标群号取自 Tool 调用上下文。
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        # (message_id, target_group_id) → (snapshot, issued_at)
        self._tickets: dict[tuple[str, str], tuple[Any, float]] = {}

    def issue(
        self,
        message_id: Any,
        target_group_id: Any,
        snapshot: Any,
        *,
        now: float,
    ) -> str | None:
        """为一条**转发源消息**与一个**目标群**签发决策票；键 / 快照不完整或停用时返回 None。

        返回票号（内部日志用，不进 intent）；重复签发同一 `(message_id, 目标群)`
        覆盖旧记录并重置 TTL 起点。
        """
        message_key = str(message_id or "").strip()
        target_key = str(target_group_id or "").strip()
        if not message_key or not target_key or snapshot is None or self.ttl_seconds <= 0.0:
            return None
        self.discard_expired(now)
        ticket = secrets.token_urlsafe(16)
        self._tickets[(message_key, target_key)] = (snapshot, now)
        return ticket

    def consume(self, message_id: Any, target_group_id: Any, *, now: float) -> Any | None:
        """按 `(message_id, 目标群)` 消费一张票，返回登记时的快照；不可用返回 None。

        不可用含：无票、已过期、已消费、目标群不匹配——错群的键根本查不到票，
        因此不会焚掉合法目标群的票（校验通过才消费即焚）。
        """
        message_key = str(message_id or "").strip()
        target_key = str(target_group_id or "").strip()
        if not message_key or not target_key:
            return None
        entry = self._tickets.get((message_key, target_key))
        if entry is None:
            return None
        snapshot, issued_at = entry
        if not self._is_fresh(issued_at, now):
            self._tickets.pop((message_key, target_key), None)
            return None
        self._tickets.pop((message_key, target_key), None)
        return snapshot

    def discard_expired(self, now: float) -> list[tuple[str, str]]:
        """移除并返回全部已过期且未被消费的票据键 `(message_id, target_group_id)`。

        已消费的票在 `consume` 时即出表，因此返回值天然只含「签发后 TTL 内未
        被消费」的票；调用方据此对决策超时记 warning（spec 转发模式：决策结果
        不可用按「不转发」处理），TTL 清理不得静默吞掉决策结果。
        """
        expired = [
            key
            for key, (_, issued_at) in self._tickets.items()
            if not self._is_fresh(issued_at, now)
        ]
        for key in expired:
            self._tickets.pop(key, None)
        return expired

    def _is_fresh(self, issued_at: float, now: float) -> bool:
        return self.ttl_seconds > 0.0 and now - issued_at < self.ttl_seconds
