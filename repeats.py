"""复读插件 — 复读域纯逻辑层（reactions.py 的表情回应域之外的另一个域）。

不依赖 maibot_sdk 的纯函数与纯内存状态：**复读文本**的宽松归一、序列化
SessionMessage dict 的消息资格判定、**复读链**状态机（滚动窗口计数、
**链终结**、LRU 上界）、三出口决策（**复读** / **打断** / 沉默）。编排逻辑在
plugin.py；术语见 CONTEXT.md，消息形态与防御式规则见 ADR 0005 与 change
design（D1–D5）。

序列化消息形态取自主程序 hook 侧（REFERENCE/MaiBot/src/plugin_runtime/
host/message_utils.py）：顶层 message_id、message_info.group_info.group_id
（私聊为 None）、message_info.user_info 的 user_id / user_nickname /
user_cardname（发送者显示名三级回落的来源）、raw_message 为段列表，其中
文本段是 {"type": "text", "data": <str>}。形态不认识时返回 None 而非抛
异常——hook 每条群消息都过，解析失败必须便宜且安静（调用方打 debug，
design D4）。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .reactions import GroupCooldown

# 内置打断文本变体池，与配置默认值同源（design D6）。配置池为空时回落到这里。
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
    """一条可能进入**复读链**的群消息。

    text 是原始文本（发送时的样子）：首尾空白与大小写原样保留——归一
    只用于链键控，**复读**发回去的永远是原始文本。message_id 与
    sender_display_name 是**触发消息**上下文打包（change design D3）：
    拦截命中后的触发消息注入要附 message_id、visible_text 要呈现发送者，
    显示名按 群名片 → 昵称 → QQ 号 三级回落（对齐主程序 planner 侧惯例），
    永不为空（QQ 号兜底）。
    """

    group_id: str
    sender_qq: str
    text: str
    message_id: str = ""
    sender_display_name: str = ""


def normalize_repeat_text(text: Any) -> str:
    """**复读文本**比对用的宽松归一。

    只做三级：去首尾空白、连续空白折叠为单个空格、casefold 大小写不敏感——
    全半角与 emoji 刻意不动（design Risks）。归一产物只用于链键控；
    **复读**发回去的始终是原始文本。
    """
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    return " ".join(raw.split()).casefold()


def _message_segments(raw_message: Any) -> list[Any] | None:
    """从序列化 raw_message 字段里取出段列表。

    hook 侧把它序列化为普通列表；带 "components" 列表的 dict 形态做防御式
    容忍（防主程序形态漂移）。其余一律按形态不认识处理。
    """
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
    """判定一条序列化 SessionMessage 能否进入**复读链**。

    仅纯文本消息合格（仅含 text 段且拼接 trim 非空）。通知消息、私聊、
    麦麦自己的消息、命令消息（文本以 "/" 开头）、超长文本、带任何非文本段
    （@/reply/image/face/表情包/...）的消息一律返回 None。防御式设计：
    形态异常或漂移时返回 None 而非抛异常（design D4）——hook 每条群消息都
    过，解析失败必须便宜，由调用方打 debug。

    通过判定的候选同时打包**触发消息**上下文（message_id 与
    sender_display_name，change design D3）：链达标的这条消息就是触发
    消息，拦截后的注入与直发编舞直接取候选字段，调用方无需二次解析
    消息 dict。
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

    segments = _message_segments(message.get("raw_message"))
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
SUPPRESSED = "suppressed"  # 已达标但被复读群冷却压制：条目照常累积，不写标记不消耗资格，资格保留待冷却结束


@dataclass(frozen=True)
class RepeatRecordResult:
    """RepeatState.record 的结果。

    status 区分继续累积（ACCUMULATE）/ 已达标待决策（TRIGGER）/ 已终结不触发
    （TERMINATED）/ 已达标但被群冷却压制（SUPPRESSED）。TRIGGER 时 text 携带
    链内最后一条原始文本（非归一产物）交给决策掷骰；其余状态为空。
    """

    status: str
    group_id: str
    sender_qq: str
    text: str = ""


@dataclass
class _ChainRecord:
    """单链账本：窗口内条目 + 最后一条原始文本。

    entries 每条记录一个（sender_qq, ts），窗口过期在每次写入时惰性清理；
    last_ts 兼作 max_chains 上界的 LRU 时钟（design D2）。
    """

    entries: list[tuple[str, float]] = field(default_factory=list)
    last_text: str = ""
    last_ts: float = 0.0


class RepeatState:
    """**复读链**状态机，纯内存态（design D2/D3）。

    链按（group_id, 归一文本）键控。**链终结**标记存独立 dict、按
    chain_ttl_seconds 过期；链条目按 window_seconds 过期——两者独立演进。
    record() 刻意是**纯同步方法**、无 await 点：窗口清理 → 计数 → 达标判定 →
    资格消耗（写终结标记）在一次调用内原子完成，同一事件循环上并发调度的
    observe handler 不可能在同一链上双触发（design D3，**表情槽**「先占槽
    再发」防护在复读域的对等物）。

    群冷却存储（mark_group / is_cooling_down）是 reactions.ReactionState 同名
    机制在复读域的对等物：独立实例、独立计时，与表情回应的冷却绝不共享。

    退化的零值边界：window_seconds=0 只保留同一时间戳写入的条目；
    chain_ttl_seconds=0 完全不做链终结（对齐去重窗口 0 即停用的惯例）。
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
        self._group_cooldown = GroupCooldown()

    def record(
        self,
        group_id: str,
        sender_qq: str,
        text: str,
        now: float,
        *,
        suppress_trigger: bool = False,
    ) -> RepeatRecordResult:
        """记录一条**复读文本**消息，可能取得一次决策资格。

        单次同步调用：窗口清理 → 计数（默认按不同**发送者**人头，关
        distinct_senders 后逐条）→ 达标判定 → 写终结标记。资格一旦授出即
        被消耗，与后续掷骰结果无关——沉默口径同样终结（沉默出口也消耗资格）。
        标记生效期间，后续**发送者**只续记录（只续记录不触发），不再取得
        新资格。

        suppress_trigger=True 即复读群冷却**冷却压制**：消息照常累积，但
        达标的链不写终结标记、不消耗资格——压制结束后下一条 record 会发现
        链已达标，直接授出资格（冷却结束后新发送者的消息到达时，链已达标
        的直接取得资格）。压制路径同在一次同步调用内，design D3 的原子性
        保证原样成立。生效中的终结标记仍优先于压制：已消耗的资格不回来。
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
                self._write_entry(key, sender, raw, now)
                self._evict_overflow()
                return RepeatRecordResult(TERMINATED, gid, sender, "")
            self._terminated.pop(key, None)  # 标记已过期

        self._write_entry(key, sender, raw, now)
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

    def is_cooling_down(self, group_id: str, cooldown_seconds: float, now: float) -> bool:
        """复读专用群冷却判定（与表情回应的群冷却分开计时，各自实例）。"""
        return self._group_cooldown.is_cooling_down(group_id, cooldown_seconds, now)

    def mark_group(self, group_id: str, now: float) -> None:
        """记录一次复读/打断出口命中，作为复读群冷却的起点。"""
        self._group_cooldown.mark_group(group_id, now)

    def _write_entry(
        self, key: tuple[str, str], sender_qq: str, text: str, now: float
    ) -> None:
        """先做窗口清理，再追加条目并刷新 LRU 字段。"""
        chain = self._chains.get(key)
        if chain is None:
            chain = _ChainRecord()
            self._chains[key] = chain
        cutoff = now - self.window_seconds
        chain.entries = [entry for entry in chain.entries if entry[1] >= cutoff]
        chain.entries.append((sender_qq, now))
        chain.last_text = text
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
    """一条已取得资格的链该做的事：kind 为 REPEAT 时携带原始**复读文本**，
    kind 为 INTERRUPT 时携带一条**打断文本**、绝不携带**复读文本**。沉默用
    None 表示，对齐 reactions.decide_reaction。used_default_pool 标记
    **打断文本**取自内置回落池（空池回落），调用方据此打日志即可，无需从
    配置文本里再推一遍空池事实。"""

    kind: str
    text: str
    reason: str
    used_default_pool: bool = False


def behavior_kind_label(kind: str) -> str:
    """决策出口 → 中文标签的**唯一映射来源**：日志与注入文案共用，
    plugin.py 不得自带副本（AGENTS.md「不往 plugin.py 里塞可测逻辑」）。"""
    return "复读" if kind == REPEAT else "打断"


def build_trigger_visible_text(candidate: RepeatCandidate) -> str:
    """**触发消息**注入的可见文案（change design D3）：呈现发送者显示名与
    原始文本。显示名已由 extract_repeat_candidate 按 群名片 → 昵称 → QQ 号
    三级回落打包进候选，此处只取用。"""
    return f"[复读触发] {candidate.sender_display_name}：{candidate.text}"


def build_behavior_visible_text(decision: RepeatDecision) -> str:
    """**行为文本**注入的可见文案（change design D3）：标签区分复读/打断，
    文案风格对齐 reactions.build_reaction_visible_text。"""
    return f"[{behavior_kind_label(decision.kind)}] {decision.text}"


def _interrupt_pool(interrupt_texts: Sequence[Any] | None) -> tuple[list[str], bool]:
    """生效中的打断文本池 + 是否用了内置默认：配置列表的非空白项；
    全部不可用时回落内置默认池（第二个返回值为 True）。"""
    texts = [str(item) for item in (interrupt_texts or ())]
    pool = [text for text in texts if text.strip()]
    if pool:
        return pool, False
    return list(DEFAULT_INTERRUPT_TEXTS), True


def _clamp_non_negative(value: Any) -> float:
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
    """**出口权重**非负归一：返回 clamp 后的（复读, 打断, 沉默）三元组与其总和。

    三个权重各自按非负数 clamp：负数 / 非数字 / None 一律按 0（口径对齐
    reactions.clamp_probability 的垃圾值处理，但只按 0 不设 [0,1] 上界——
    权重只有比例意义，上界由配置模型约束）。单个权重非法不影响其余权重。
    总和为 0 表示必落沉默，decide_repeat 据此直接不掷骰（ADR 0007）。
    """
    repeat = _clamp_non_negative(weight_repeat)
    interrupt = _clamp_non_negative(weight_interrupt)
    silence = _clamp_non_negative(weight_silence)
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
    """对一条已取得资格的**复读链**做**三出口决策**（出口权重口径，ADR 0007）。

    三个权重先各自非负归一（负数 / 非数字 / None 按 0，见
    normalize_exit_weights）；归一后求和，总和为 0 时出口必为沉默，直接返回
    None 不掷骰。否则只掷一次骰：把 roll()（[0,1)）乘总和得到数轴落点，按
    **复读 → 打断 → 沉默** 的固定顺序与累加阈值比较，命中即返回——落点 <
    复读权重 → REPEAT（发链内最后一条原始**复读文本**）；落点 < 复读权重 +
    打断权重 → INTERRUPT（从池内随机发一条**打断文本**，绝不带**复读文本**）；
    其余 → 沉默（None，什么都不发）。三出口严格互斥；权重只有比例意义，
    同比例放大命中分布不变。

    interrupt_texts 为空（或全空白）时回落内置默认池不抛异常，回落事实记在
    决策结果的 used_default_pool 上，由调用方打日志（design D5）。
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
