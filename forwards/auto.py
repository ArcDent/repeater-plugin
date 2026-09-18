"""自动转发的逐目标群掷骰与决策（design D1/D6，纯逻辑）。

每个**目标群**独立掷一次骰决定是否分享这条**合并转发**；编排（门禁、冷却
写入、后台投递）留在 `plugin.py`。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

# 权重 clamp 与复读域共用同一公共 helper（跨域共用，不另起副本）：
# 负数 / 非数字 / None 一律按 0。
from ..repeats import clamp_non_negative


def decide_forward_targets(
    target_group_ids: Iterable[Any],
    source_group_id: Any,
    weight_send: Any,
    weight_silence: Any,
    roll: Callable[[], float],
) -> list[str]:
    """逐**目标群**掷骰，返回命中**转发**出口的群号列表（保持输入顺序）。

    权重按非负数归一（负数 / 非法值按 0）；两权重全为 0 的群视为沉默且不掷
    骰。与**源群**相同的条目、空条目一律跳过；重复条目只掷一次（同一轮不向
    同一个群投递两次），与配置名单在其它域的集合语义一致。
    """
    source = str(source_group_id or "").strip()
    send_weight = clamp_non_negative(weight_send)
    silence_weight = clamp_non_negative(weight_silence)
    total = send_weight + silence_weight
    hits: list[str] = []
    seen: set[str] = set()
    for raw_group_id in target_group_ids:
        group_id = str(raw_group_id or "").strip()
        if not group_id or group_id == source or group_id in seen:
            continue
        seen.add(group_id)
        if total <= 0.0:
            continue  # 两权重全 0：沉默，不掷骰
        if roll() * total < send_weight:
            hits.append(group_id)
    return hits
