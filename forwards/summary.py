"""摘要就绪判定与**富摘要**提取（design D2，纯逻辑、不依赖 SDK）。

**摘要就绪**的判据是主程序 `message.get_by_id` 返回的消息字典里
`processed_plain_text`（strip 后）非空；该文本即**富摘要**，注入与 intent 的
内容主体都从它来。轮询节奏（0.5s 起步、退避至 1s 封顶）与总时长判据也在这里，
`plugin.py` 只调用本模块的纯函数，不自己算间隔。就绪观测用的媒体占位计数
（`[图片]` / `[表情]` / `[嵌套转发]`）同样放在这里，与提取共用纯逻辑口径。
"""

from __future__ import annotations

from typing import Any

from .nodes import NESTED_FORWARD_PLACEHOLDER, SEGMENT_PLACEHOLDERS

# 轮询节奏（design D2）：首次立即查，之后 0.5s 起步、退避至 1s 封顶。
_POLL_INTERVAL_INITIAL_SECONDS = 0.5
_POLL_INTERVAL_MAX_SECONDS = 1.0

# 富摘要里的媒体占位词表（change design D3）：`[图片]` / `[表情]` 取段类型占位
# 表的唯一来源，`[嵌套转发]` 取 `forwards/nodes.py` 的同名常量（与
# `forwards/texts.py` 共用单处定义）。富摘要只是主程序处理后的纯文本，已无段
# 类型信息，占位只能按文本计数。
_PLACEHOLDER_TEXTS = (*SEGMENT_PLACEHOLDERS.values(), NESTED_FORWARD_PLACEHOLDER)


def extract_rich_summary(message: Any) -> str:
    """从 `message.get_by_id` 的消息字典里取**富摘要**（`processed_plain_text`）。

    非 dict / 字段缺失 / 非字符串 / strip 后为空一律返回空串——空串即「未就绪」，
    调用方据此继续轮询。不抛异常：轮询期间的异常形态由调用方兜底，本层只做
    形态判定。
    """
    if not isinstance(message, dict):
        return ""
    text = message.get("processed_plain_text")
    if not isinstance(text, str):
        return ""
    return text.strip()


def count_rich_summary_placeholders(text: Any) -> int:
    """统计**富摘要**里媒体占位（`[图片]` / `[表情]` / `[嵌套转发]`）的出现次数。

    主程序 `images` 表缺 hash 时，麦麦多模态 prompt 只能看到这些降级占位；
    计数只用于就绪时的观测日志（change design D3），不改写富摘要、不参与任何
    行为分支。非字符串 / 空串一律返回 0，与 `extract_rich_summary` 同款防御口径。
    """
    if not isinstance(text, str):
        return 0
    return sum(text.count(placeholder) for placeholder in _PLACEHOLDER_TEXTS)


def next_poll_interval(attempt: int) -> float:
    """第 `attempt` 次等待的轮询间隔（0 起算）：0.5s 起步、退避至 1s 封顶。"""
    interval = _POLL_INTERVAL_INITIAL_SECONDS * (2 ** max(int(attempt), 0))
    return min(interval, _POLL_INTERVAL_MAX_SECONDS)


def should_give_up(elapsed: float, timeout: float) -> bool:
    """等待已耗时 `elapsed` 秒时是否放弃：`elapsed >= timeout` 即放弃。

    `timeout <= 0` 为禁用等待，立即放弃（不轮询）。
    """
    limit = float(timeout)
    if limit <= 0.0:
        return True
    return float(elapsed) >= limit
