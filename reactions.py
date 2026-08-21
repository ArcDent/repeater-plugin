"""表情跟风插件 — 纯逻辑层。

只包含不依赖 maibot_sdk 的纯函数与纯内存状态：上报解析、表情 ID 归一、
反义映射构建、跟风/反义决策、表情槽与群冷却。编排逻辑在 plugin.py。

术语见 CONTEXT.md；分支互斥与内存态的取舍见 docs/adr/0002。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# 已知的表情回应类 notice_type。NapCat 系用 group_msg_emoji_like，
# 部分协议端用 reaction + sub_type=add/remove。见 docs/adr/0001。
REACTION_NOTICE_TYPES = frozenset({
    "group_msg_emoji_like",
    "group_msg_emoji_like_remove",
    "reaction",
    "group_reaction",
})

# 内置反义映射，只用 Unicode emoji（QQ 私有小黄脸编号的语义无公开权威表，
# 不内置猜测值；需要私有编号请在 config.toml 的 extra_pairs 里按实测填）。
# 选取原则：语义公认、无歧义、与已有对不重复（😄/😡、🔥/❄、😍/🤢、😭、👎、
# ❌、💔 已被占用时改选相邻语义的字符，如 😠、🥵、😋、😢）。
DEFAULT_OPPOSITE_PAIRS: tuple[tuple[str, str], ...] = (
    ("👍", "👎"),
    ("❤", "💔"),
    ("😂", "😭"),
    ("😄", "😡"),
    ("✅", "❌"),
    ("🔥", "❄"),
    ("⬆", "⬇"),
    ("😍", "🤢"),
    ("😈", "😇"),
    ("🤮", "😋"),
    ("🥶", "🥵"),
    ("☀", "🌧"),
    ("🚀", "🐢"),
    ("😊", "😠"),
    ("🎉", "😢"),
    ("➕", "➖"),
)

BANDWAGON = "bandwagon"
OPPOSITE = "opposite"


@dataclass(frozen=True)
class ReactionEvent:
    """一次表情回应上报，已归一为插件内部形态。"""

    group_id: str
    message_id: str
    reactor_qq: str
    emoji_id: str
    removed: bool = False


@dataclass(frozen=True)
class Decision:
    """麦麦要贴的表情回应。kind 取 BANDWAGON 或 OPPOSITE。"""

    kind: str
    emoji_id: str
    reason: str


def normalize_emoji_id(token: Any) -> str:
    """把配置或上报里的表情写法归一为协议端用的表情 ID。

    纯数字原样保留；emoji 字符取首个码点的十进制值，因此 ❤️ 与 ❤、👍🏻 与 👍
    归一到同一 ID。

    这一步不是可选的美化，它抹平了协议端上报与发送两侧的形式不对称
    （依据 SnowLuma 源码，见 REFERENCE/snowluma）：

    - 上报侧 `event-0x2dc.ts` 直接透传 QQ 给的 code：QQ 小黄脸是数字串（'76'），
      Unicode 表情是**字符本身**（其测试断言 `event.emojiId === '⭐'`）
    - 发送侧 `oidb-services/reaction/set-reaction.ts` 用 `code.length > 3` 判类型：
      1~3 位当 QQ 小黄脸（type=1），更长的当 Unicode 码点十进制（type=2）

    所以把上报的 '⭐' 归一成 '11088' 后回传，长度 5 > 3 会被正确识别为 Unicode；
    '76' 保持 2 位，仍被识别为 QQ 小黄脸。内置反义映射用码点也正因如此可用。
    """
    if token is None:
        return ""
    text = str(token).strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    return str(ord(text[0]))


def is_reaction_notice(payload: Any) -> bool:
    """是否是表情回应类 notice。用于区分「形态未知需要告警」与「无关事件可静默丢弃」。"""
    if not isinstance(payload, dict):
        return False
    if str(payload.get("post_type") or "") != "notice":
        return False
    notice_type = str(payload.get("notice_type") or "").strip().lower()
    if notice_type in REACTION_NOTICE_TYPES:
        return True
    return "emoji_like" in notice_type or "reaction" in notice_type


def _first_emoji_from_likes(payload: dict[str, Any]) -> str:
    likes = payload.get("likes")
    if isinstance(likes, dict):
        likes = [likes]
    if not isinstance(likes, list):
        return ""
    for item in likes:
        if isinstance(item, dict):
            emoji_id = normalize_emoji_id(item.get("emoji_id") or item.get("code") or item.get("id"))
            if emoji_id:
                return emoji_id
        else:
            emoji_id = normalize_emoji_id(item)
            if emoji_id:
                return emoji_id
    return ""


def parse_reaction_report(payload: Any) -> ReactionEvent | None:
    """解析协议端上报的表情回应事件；形态不认识或字段不全时返回 None。

    一次上报可能带多个 like，只取第一个：一条被回应消息只有一个表情槽，
    多取也会被槽位拦掉（见 CONTEXT.md「表情槽」）。
    """
    if not isinstance(payload, dict) or not is_reaction_notice(payload):
        return None

    group_id = str(payload.get("group_id") or "").strip()
    message_id = str(payload.get("message_id") or "").strip()
    reactor_qq = str(payload.get("user_id") or payload.get("operator_id") or "").strip()
    emoji_id = normalize_emoji_id(
        payload.get("emoji_id") or payload.get("code") or payload.get("face_id")
    ) or _first_emoji_from_likes(payload)

    if not group_id or not message_id or not emoji_id:
        return None

    notice_type = str(payload.get("notice_type") or "").strip().lower()
    sub_type = str(payload.get("sub_type") or "").strip().lower()
    removed = sub_type in {"remove", "unset", "cancel", "delete"} or "remove" in notice_type

    return ReactionEvent(
        group_id=group_id,
        message_id=message_id,
        reactor_qq=reactor_qq,
        emoji_id=emoji_id,
        removed=removed,
    )


def parse_pair(token: Any) -> tuple[str, str] | None:
    """解析一条 "A=B" 形式的反义配置；两端归一后任一为空或相同则返回 None。"""
    if token is None:
        return None
    text = str(token).strip()
    if not text:
        return None
    for sep in ("=", "↔", ":", "|"):
        if sep in text:
            left, _, right = text.partition(sep)
            break
    else:
        return None
    a, b = normalize_emoji_id(left), normalize_emoji_id(right)
    if not a or not b or a == b:
        return None
    return a, b


def build_opposite_map(
    extra_pairs: Iterable[Any] | None = None,
    *,
    include_defaults: bool = True,
    disabled: Iterable[Any] | None = None,
) -> dict[str, str]:
    """构建双向对称的反义映射。

    后加入的对覆盖先前的：新对里的任一表情 ID 若已出现在旧对中，旧对整对失效，
    以保证「A 的反义是 B 则 B 的反义是 A」这一对称性（见 CONTEXT.md Relationships）。
    disabled 里的表情 ID 所在的对整对移除，使该表情回落到跟风分支。
    """
    pairs: list[tuple[str, str]] = []

    def upsert(a: str, b: str) -> None:
        pairs[:] = [p for p in pairs if a not in p and b not in p]
        pairs.append((a, b))

    if include_defaults:
        for raw_a, raw_b in DEFAULT_OPPOSITE_PAIRS:
            a, b = normalize_emoji_id(raw_a), normalize_emoji_id(raw_b)
            if a and b and a != b:
                upsert(a, b)

    for token in extra_pairs or ():
        parsed = parse_pair(token)
        if parsed is not None:
            upsert(*parsed)

    blocked = {normalize_emoji_id(token) for token in (disabled or ())}
    blocked.discard("")
    if blocked:
        pairs[:] = [p for p in pairs if p[0] not in blocked and p[1] not in blocked]

    mapping: dict[str, str] = {}
    for a, b in pairs:
        mapping[a] = b
        mapping[b] = a
    return mapping


def group_allowed(
    group_id: str,
    whitelist: Sequence[Any] | None = None,
    blacklist: Sequence[Any] | None = None,
) -> bool:
    """群是否在生效范围内。黑名单优先；白名单非空时只放行名单内的群。"""
    gid = str(group_id or "").strip()
    if not gid:
        return False
    black = {str(item).strip() for item in (blacklist or ()) if str(item).strip()}
    if gid in black:
        return False
    white = {str(item).strip() for item in (whitelist or ()) if str(item).strip()}
    if white and gid not in white:
        return False
    return True


def _clamp(probability: Any) -> float:
    try:
        value = float(probability)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0.0:
        return 0.0
    return min(1.0, value)


def decide_reaction(
    event: ReactionEvent,
    *,
    is_self_message: bool,
    opposite_map: dict[str, str],
    bandwagon_probability: float,
    self_message_probability: float,
    opposite_probability: float,
    roll: Callable[[], float],
) -> Decision | None:
    """决定麦麦对这次表情回应做什么。两级决策，最多掷两次骰：

    1. 回应骰：按跟风概率（self 消息用独立概率）决定是否贴表情，掷不中就什么都不做；
    2. 反义骰：回应骰掷中后，若表情命中反义映射，再按反义概率决定贴相反表情
       还是贴同款——反义骰掷不中**回落跟风**（见 docs/adr/0002）。

    未命中反义映射的表情只有回应骰，掷中即贴同款。概率为 0 时不掷骰，为 1 时必中。
    """
    if event.removed:
        return None

    respond_probability = _clamp(
        self_message_probability if is_self_message else bandwagon_probability
    )
    if respond_probability <= 0.0:
        return None
    if roll() >= respond_probability:
        return None

    opposite_id = opposite_map.get(event.emoji_id, "")
    if opposite_id:
        probability = _clamp(opposite_probability)
        if probability > 0.0 and roll() < probability:
            return Decision(
                kind=OPPOSITE,
                emoji_id=opposite_id,
                reason=(
                    f"{event.emoji_id} 命中反义映射 → {opposite_id}"
                    f"（回应 p={respond_probability}，反义 p={probability}）"
                ),
            )

    target = "麦麦自己的消息" if is_self_message else "他人消息"
    return Decision(
        kind=BANDWAGON,
        emoji_id=event.emoji_id,
        reason=f"跟风 {event.emoji_id}（{target}，p={respond_probability}）",
    )


def coerce_message_id(message_id: Any) -> Any:
    """协议端的 message_id 参数：纯数字转 int，其余原样传（部分协议端用字符串 ID）。"""
    text = str(message_id or "").strip()
    if not text:
        return ""
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return text
    return text


def parse_message_author(response: Any) -> str:
    """从 get_msg 响应里取出被回应消息的原作者 QQ，取不到返回空串。"""
    if not isinstance(response, dict):
        return ""
    data = response.get("data")
    if not isinstance(data, dict):
        data = response
    sender = data.get("sender")
    if isinstance(sender, dict):
        candidate = sender.get("user_id") or sender.get("uin")
        if candidate not in (None, ""):
            return str(candidate).strip()
    candidate = data.get("user_id")
    return "" if candidate in (None, "") else str(candidate).strip()


def parse_self_qq(response: Any) -> str:
    """从 get_login_info 响应里取出麦麦自己的 QQ 号，取不到返回空串。"""
    if not isinstance(response, dict):
        return ""
    data = response.get("data")
    if isinstance(data, dict):
        candidate = data.get("user_id") or data.get("uin")
        if candidate not in (None, ""):
            return str(candidate).strip()
    candidate = response.get("user_id") or response.get("uin")
    return "" if candidate in (None, "") else str(candidate).strip()


def protocol_call_failed(response: Any) -> bool:
    """OneBot 动作响应是否失败。协议端失败时可能 HTTP 200 + status=failed，必须查响应体。"""
    if not isinstance(response, dict):
        return True
    status = str(response.get("status") or "").lower()
    if status == "ok":
        return False
    if status:
        return True
    retcode = response.get("retcode")
    return isinstance(retcode, (int, float)) and retcode != 0


def is_missing_action(response: Any) -> bool:
    """协议端不支持该动作（OneBot HTTP 约定 retcode=1404）。"""
    return isinstance(response, dict) and response.get("retcode") == 1404


class ReactionState:
    """表情槽与群冷却，纯内存 + 双重裁剪（条数上界 & TTL）。见 docs/adr/0002。"""

    def __init__(self, slot_ttl_seconds: float = 3600.0, max_slots: int = 2000) -> None:
        self.slot_ttl_seconds = max(float(slot_ttl_seconds), 0.0)
        self.max_slots = max(int(max_slots), 1)
        self._slots: dict[str, float] = {}
        self._group_last_sent: dict[str, float] = {}
        self._authors: dict[str, tuple[float, str]] = {}

    def is_slot_taken(self, message_id: str, now: float) -> bool:
        key = str(message_id or "")
        taken_at = self._slots.get(key)
        if taken_at is None:
            return False
        if self.slot_ttl_seconds and now - taken_at > self.slot_ttl_seconds:
            self._slots.pop(key, None)
            return False
        return True

    def take_slot(self, message_id: str, now: float) -> None:
        key = str(message_id or "")
        if not key:
            return
        self._slots[key] = now
        self.prune(now)

    def is_cooling_down(self, group_id: str, cooldown_seconds: float, now: float) -> bool:
        cooldown = max(float(cooldown_seconds), 0.0)
        if cooldown <= 0.0:
            return False
        last = self._group_last_sent.get(str(group_id or ""))
        return last is not None and now - last < cooldown

    def mark_group(self, group_id: str, now: float) -> None:
        key = str(group_id or "")
        if key:
            self._group_last_sent[key] = now

    def remember_author(self, message_id: str, author_qq: str, now: float) -> None:
        """缓存被回应消息的原作者，避免同一条消息被多人贴表情时重复查协议端。"""
        key = str(message_id or "")
        if key and author_qq:
            self._authors[key] = (now, str(author_qq))

    def recall_author(self, message_id: str, now: float) -> str:
        key = str(message_id or "")
        entry = self._authors.get(key)
        if entry is None:
            return ""
        cached_at, author_qq = entry
        if self.slot_ttl_seconds and now - cached_at > self.slot_ttl_seconds:
            self._authors.pop(key, None)
            return ""
        return author_qq

    def prune(self, now: float) -> None:
        """先按 TTL 清理，仍超上界则丢弃最旧的条目。"""
        if self.slot_ttl_seconds:
            expired = [k for k, ts in self._slots.items() if now - ts > self.slot_ttl_seconds]
            for key in expired:
                self._slots.pop(key, None)
            stale = [k for k, (ts, _) in self._authors.items() if now - ts > self.slot_ttl_seconds]
            for key in stale:
                self._authors.pop(key, None)
        overflow = len(self._slots) - self.max_slots
        if overflow > 0:
            for key, _ in sorted(self._slots.items(), key=lambda kv: kv[1])[:overflow]:
                self._slots.pop(key, None)
        author_overflow = len(self._authors) - self.max_slots
        if author_overflow > 0:
            for key, _ in sorted(self._authors.items(), key=lambda kv: kv[1][0])[:author_overflow]:
                self._authors.pop(key, None)