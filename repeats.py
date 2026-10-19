"""复读插件 — 复读域纯逻辑层。

不依赖 maibot_sdk 的纯函数与纯内存状态：复读文本宽松归一、消息资格判定、
复读链状态机、三出口决策。编排逻辑在 plugin.py，术语见 CONTEXT.md。

序列化消息形态取自主程序 hook 侧（message_utils.py）：顶层 message_id、
message_info.group_info.group_id（私聊为 None）、user_info 三字段、raw_message
段列表（文本段 {"type": "text", "data": <str>}）。形态不认识时返回 None 而非
抛异常——hook 每条群消息都过，解析失败必须便宜且安静。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# 内置打断文本变体池，与配置默认值同源；配置池为空时回落到这里。
DEFAULT_INTERRUPT_TEXTS: tuple[str, ...] = (
    "打断！",
    "够了！",
    "停！",
    "打住！",
    "别念了！",
)

REPEAT = "repeat"
INTERRUPT = "interrupt"


@dataclass(frozen=True)
class RepeatCandidate:
    """一条可能进入复读链的群消息。

    text 是原始文本，归一只用于链键控，复读发回去的永远是原始文本。
    message_id 与 sender_display_name 是触发消息上下文打包：注入与文案直接
    取候选字段，显示名按 群名片 → 昵称 → QQ 号 三级回落，永不为空。
    """

    group_id: str
    sender_qq: str
    text: str
    message_id: str = ""
    sender_display_name: str = ""


def normalize_repeat_text(text: Any) -> str:
    """复读文本比对用的宽松归一：去首尾空白、连续空白折叠、casefold——
    全半角与 emoji 刻意不动。归一产物只用于链键控，发送用原始文本。"""
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    return " ".join(raw.split()).casefold()


def message_segments(raw_message: Any) -> list[Any] | None:
    """从序列化 raw_message 字段里取出段列表；带 components 的 dict 形态做防御式容忍。"""
    if isinstance(raw_message, list):
        return raw_message
    if isinstance(raw_message, dict):
        components = raw_message.get("components")
        if isinstance(components, list):
            return components
    return None


def extract_repeat_candidate(
    message: Any,
    *,
    self_qq: str = "",
    max_text_length: int = 200,
) -> RepeatCandidate | None:
    """判定一条序列化 SessionMessage 能否进入复读链；仅纯文本合格。

    通知、私聊、麦麦自己、"/" 命令、超长、含任何非文本段（@/图片/表情包等）
    一律返回 None；形态异常也返回 None 而非抛异常（调用方打 debug）。通过的
    候选同时打包触发消息上下文（message_id 与显示名）。
    """
    if not isinstance(message, dict):
        return None
    if message.get("is_notify"):
        return None
    message_info = message.get("message_info")
    if not isinstance(message_info, dict):
        return None
    group_info = message_info.get("group_info")
    if not isinstance(group_info, dict):
        return None  # 私聊（group_info 为 None）——不进复读链
    group_id = str(group_info.get("group_id") or "").strip()
    if not group_id:
        return None
    user_info = message_info.get("user_info")
    if not isinstance(user_info, dict):
        return None
    sender_qq = str(user_info.get("user_id") or "").strip()
    if not sender_qq:
        return None
    if self_qq and sender_qq == str(self_qq).strip():
        return None

    # 触发消息上下文打包（change design D3）：message_id 顶层恒在（形态
    # 漂移时容忍缺失，回落空串）；显示名按 群名片 → 昵称 → QQ 号 三级
    # 回落，缺谁跳谁，QQ 号兜底。
    message_id = str(message.get("message_id") or "").strip()
    cardname = str(user_info.get("user_cardname") or "").strip()
    nickname = str(user_info.get("user_nickname") or "").strip()
    sender_display_name = cardname or nickname or sender_qq

    segments = message_segments(message.get("raw_message"))
    if segments is None:
        return None
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        if str(segment.get("type") or "") != "text":
            return None
        data = segment.get("data")
        if not isinstance(data, str):
            return None
        parts.append(data)
    text = "".join(parts)
    if not text.strip():
        return None
    if text.strip().startswith("/"):
        return None
    if max_text_length and len(text) > max_text_length:
        return None
    return RepeatCandidate(
        group_id=group_id,
        sender_qq=sender_qq,
        text=text,
        message_id=message_id,
        sender_display_name=sender_display_name,
    )


# RepeatState.record 的四种结果状态。
ACCUMULATE = "accumulate"  # 链仍在累积，尚未达标
TRIGGER = "trigger"  # 已达标，取得一次决策资格（链随之终结）
TERMINATED = "terminated"  # 终结标记仍在生效，不触发
SUPPRESSED = "suppressed"  # 达标但被群冷却压制：照常累积，不写标记不消耗资格


@dataclass(frozen=True)
class RepeatRecordResult:
    """RepeatState.record 的结果：status 区分四种状态；TRIGGER 时 text 携带链内
    最后一条原始文本交决策掷骰，其余状态为空。"""

    status: str
    group_id: str
    sender_qq: str
    text: str = ""


@dataclass
class _ChainRecord:
    """单链账本：窗口内条目 + 最近一次写入时刻（last_ts 兼作 LRU 时钟）。"""

    entries: list[tuple[str, float]] = field(default_factory=list)
    last_ts: float = 0.0


class RepeatState:
    """复读链状态机，纯内存态；链按（group_id, 归一文本）键控。

    链终结标记按 chain_ttl_seconds 过期，链条目按 window_seconds 过期，两者
    独立演进。record() 刻意是纯同步方法、无 await 点：清理 → 计数 → 达标判定 →
    资格消耗（写终结标记）一次调用内原子完成，并发 handler 不可能在同一链上
    双触发。复读群冷却不在本类，由调用方算好后经 suppress_trigger 传入。

    零值边界：window_seconds=0 只保留同一时间戳的条目；chain_ttl_seconds=0
    完全不做链终结。
    """

    def __init__(
        self,
        window_seconds: float = 120.0,
        threshold: int = 3,
        distinct_senders: bool = True,
        chain_ttl_seconds: float = 1800.0,
        max_chains: int = 2000,
    ) -> None:
        self.window_seconds = max(float(window_seconds), 0.0)
        self.threshold = max(int(threshold), 1)
        self.distinct_senders = bool(distinct_senders)
        self.chain_ttl_seconds = max(float(chain_ttl_seconds), 0.0)
        self.max_chains = max(int(max_chains), 1)
        self._chains: dict[tuple[str, str], _ChainRecord] = {}
        self._terminated: dict[tuple[str, str], float] = {}

    def record(
        self,
        group_id: str,
        sender_qq: str,
        text: str,
        now: float,
        *,
        suppress_trigger: bool = False,
    ) -> RepeatRecordResult:
        """记录一条复读文本消息，可能取得一次决策资格。

        资格一旦授出即被消耗，与掷骰结果无关——沉默同样终结。标记生效期间
        后续发送者只续记录、不取得新资格。suppress_trigger=True（群冷却压制）
        时消息照常累积，但达标链不写标记不消耗资格，压制结束后下一条 record
        直接授出资格；生效中的终结标记仍优先于压制，已消耗的资格不回来。
        """
        gid = str(group_id or "").strip()
        sender = str(sender_qq or "").strip()
        raw = text if isinstance(text, str) else str(text or "")
        if not gid or not sender or not raw.strip():
            # 防御式空操作：形态异常的输入不触碰链状态。
            return RepeatRecordResult(ACCUMULATE, gid, sender, "")
        key = (gid, normalize_repeat_text(raw))

        marker_ts = self._terminated.get(key)
        if marker_ts is not None:
            if self.chain_ttl_seconds > 0 and now - marker_ts <= self.chain_ttl_seconds:
                self._write_entry(key, sender, now)
                self._evict_overflow()
                return RepeatRecordResult(TERMINATED, gid, sender, "")
            self._terminated.pop(key, None)  # 标记已过期

        self._write_entry(key, sender, now)
        chain = self._chains[key]
        if self.distinct_senders:
            count = len({entry_sender for entry_sender, _ in chain.entries})
        else:
            count = len(chain.entries)

        self._evict_overflow()

        if count >= self.threshold:
            if suppress_trigger:
                # 群冷却生效中：条目保留、不写标记、不消耗资格——资格
                # 留给压制结束后的第一条 record。
                return RepeatRecordResult(SUPPRESSED, gid, sender, "")
            # 资格在此消耗，就在这次同步调用内：任何骰子掷出之前标记已写上。
            self._terminated[key] = now
            return RepeatRecordResult(TRIGGER, gid, sender, raw)
        return RepeatRecordResult(ACCUMULATE, gid, sender, "")

    def _write_entry(self, key: tuple[str, str], sender_qq: str, now: float) -> None:
        """先做窗口清理，再追加条目并刷新 LRU 字段。"""
        chain = self._chains.get(key)
        if chain is None:
            chain = _ChainRecord()
            self._chains[key] = chain
        cutoff = now - self.window_seconds
        chain.entries = [entry for entry in chain.entries if entry[1] >= cutoff]
        chain.entries.append((sender_qq, now))
        chain.last_ts = now

    def _evict_overflow(self) -> None:
        """LRU 上界：淘汰超出 max_chains 的最久未活跃链，其终结标记随之淘汰。"""
        overflow = len(self._chains) - self.max_chains
        if overflow > 0:
            oldest = sorted(self._chains.items(), key=lambda item: item[1].last_ts)[:overflow]
            for key, _ in oldest:
                self._chains.pop(key, None)
                self._terminated.pop(key, None)


@dataclass(frozen=True)
class RepeatDecision:
    """一条已取得资格的链该做的事：REPEAT 携带原始复读文本，INTERRUPT 携带
    一条打断文本、绝不携带复读文本；沉默用 None 表示。used_default_pool 标记
    打断文本取自内置回落池，调用方据此打日志即可。"""

    kind: str
    text: str
    reason: str
    used_default_pool: bool = False


def behavior_kind_label(kind: str) -> str:
    """决策出口 → 中文标签的唯一映射来源，日志与注入文案共用，plugin.py 不自带副本。"""
    return "复读" if kind == REPEAT else "打断"


def build_trigger_visible_text(candidate: RepeatCandidate) -> str:
    """触发消息注入的可见文案：呈现发送者显示名与原始文本。"""
    return f"[复读触发] {candidate.sender_display_name}：{candidate.text}"


def build_behavior_visible_text(decision: RepeatDecision) -> str:
    """行为文本注入的可见文案：标签区分复读/打断。"""
    return f"[{behavior_kind_label(decision.kind)}] {decision.text}"


def _interrupt_pool(interrupt_texts: Sequence[Any] | None) -> tuple[list[str], bool]:
    """生效中的打断文本池 + 是否用了内置默认：配置列表的非空白项；
    全部不可用时回落内置默认池（第二个返回值为 True）。"""
    texts = [str(item) for item in (interrupt_texts or ())]
    pool = [text for text in texts if text.strip()]
    if pool:
        return pool, False
    return list(DEFAULT_INTERRUPT_TEXTS), True


def clamp_non_negative(value: Any) -> float:
    """非负 clamp：负数 / 非数字 / None 一律按 0，正数原样返回。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def normalize_exit_weights(
    weight_repeat: Any,
    weight_interrupt: Any,
    weight_silence: Any,
) -> tuple[tuple[float, float, float], float]:
    """出口权重非负归一：返回 clamp 后的（复读, 打断, 沉默）三元组与其总和。

    负数 / 非数字 / None 一律按 0，不设上界——权重只有比例意义，上界由配置
    模型约束。总和为 0 表示必落沉默，decide_repeat 据此直接不掷骰。
    """
    repeat = clamp_non_negative(weight_repeat)
    interrupt = clamp_non_negative(weight_interrupt)
    silence = clamp_non_negative(weight_silence)
    return (repeat, interrupt, silence), repeat + interrupt + silence


def decide_repeat(
    text: str,
    *,
    weight_repeat: Any,
    weight_interrupt: Any,
    weight_silence: Any,
    interrupt_texts: Sequence[Any] | None = None,
    roll: Callable[[], float] = random.random,
    choice: Callable[[Sequence[str]], str] = random.choice,
) -> RepeatDecision | None:
    """对一条已取得资格的复读链做三出口决策（ADR 0007）。

    权重先各自非负归一，总和为 0 时必落沉默、直接返回 None 不掷骰。否则只掷
    一次骰：roll() 乘总和得数轴落点，按 复读 → 打断 → 沉默 的固定顺序与累加
    阈值比较——落点 < 复读权重 → REPEAT（发原始复读文本）；< 复读 + 打断权重 →
    INTERRUPT（池内随机一条打断文本）；其余 → 沉默。三出口严格互斥，同比例
    放大权重分布不变。空池回落内置默认池，事实记在 used_default_pool 上。
    """
    (repeat_weight, interrupt_weight, silence_weight), total = normalize_exit_weights(
        weight_repeat, weight_interrupt, weight_silence
    )
    if total <= 0.0:
        return None

    pool, used_default_pool = _interrupt_pool(interrupt_texts)
    value = roll() * total
    weight_summary = f"{repeat_weight:g}/{interrupt_weight:g}/{silence_weight:g}"
    if value < repeat_weight:
        return RepeatDecision(
            kind=REPEAT,
            text=text,
            reason=f"复读出口命中（权重 {weight_summary}）",
        )
    if value < repeat_weight + interrupt_weight:
        return RepeatDecision(
            kind=INTERRUPT,
            text=choice(pool),
            reason=f"打断出口命中（权重 {weight_summary}）",
            used_default_pool=used_default_pool,
        )
    return None
