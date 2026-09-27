"""摘要就绪判定与**富摘要**提取（design D2，纯逻辑、不依赖 SDK）。

**摘要就绪**的判据是主程序 `message.get_by_id` 返回的消息字典里
`processed_plain_text`（strip 后）非空；该文本即**富摘要**，注入与 intent 的
内容主体都从它来。轮询节奏（0.5s 起步、退避至 1s 封顶）与总时长判据也在这里，
`plugin.py` 只调用本模块的纯函数，不自己算间隔。
"""

from __future__ import annotations

from typing import Any

# 轮询节奏（design D2）：首次立即查，之后 0.5s 起步、退避至 1s 封顶。
_POLL_INTERVAL_INITIAL_SECONDS = 0.5
_POLL_INTERVAL_MAX_SECONDS = 1.0


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
