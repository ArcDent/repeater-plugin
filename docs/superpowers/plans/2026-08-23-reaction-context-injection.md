# 表情回应上下文注入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增实验性「上下文注入」：表情回应（含麦麦自己的）去重、聚合后注入 Maisaka 聊天历史，并防御式拦截主程序消息链上的表情回应通知消息；配套 emoji 编号文档。

**Architecture:** 纯逻辑（去重/聚合/可见文本/形态判定）进 `reactions.py`；编排（stream 解析、`maisaka.context.append` 调用、显示名来源链）进 `plugin.py`；新配置 section `context` 总开关（默认关）。拦截走 `chat.receive.before_process` Hook（`allow_abort=True`，主程序内置）。决策依据见 `docs/adr/0003`，术语见 `CONTEXT.md`。

**Tech Stack:** Python 3.12+ / aiohttp / maibot_sdk（fake 替身测试）/ pytest

## Global Constraints

- 术语约束（CONTEXT.md）：**表情回应**（贴在消息上）与**表情包**（图片消息）不得混用；「去重」= (回应者,被回应消息,表情ID) 窗口消重；「聚合」= 同消息同表情多人合并一条注入消息
- 表情 ID 归一必须走 `normalize_emoji_id`（AGENTS.md 关键规则）
- 纯逻辑一律进 `reactions.py` + 单测，不往 `plugin.py` 塞可测逻辑
- 「断言不触发」的用例必须概率拉满（`make_plugin` 已默认 1.0）；涉及 TTL 的用例时间戳传 `time.time()`，不传 0.0
- 内置反义映射只用 Unicode emoji，不臆造 QQ 私有编号语义；文档不得编造 QQ 小黄脸编号对照
- 测试跑法：`uv run --with pytest --with aiohttp pytest tests/ -q`；生产代码 `uv run --with pyright --with aiohttp pyright` 零错误
- 每次 commit 前必须问用户确认（全局 CLAUDE.md：不自动 commit）
- 四个 ruff 有意偏离（BLE001/N999/SIM103/SIM117）不得「顺手修掉」

---

### Task 1: 配置模型 — `context` section

**Files:**
- Modify: `config.py`（`BehaviorSection` 之后、`ScopeSection` 之前插入 `ContextSection`；`SendingEmojisConfig` 加字段）
- Modify: `config.toml`（`[scope]` 之前插入 `[context]`）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.ContextSection`（字段见下），`SendingEmojisConfig.context: ContextSection`
- 值必须与 `config.toml` 一致（AGENTS.md 规则）

- [ ] **Step 1: 写失败测试**

`tests/test_config.py` 追加：

```python
def test_context_section_defaults():
    cfg = SendingEmojisConfig()
    assert cfg.context.enabled is False
    assert cfg.context.dedup_ttl_seconds == 3600
    assert cfg.context.merge_window_seconds == 30
    assert cfg.context.max_reactions_per_message == 10
    assert cfg.context.self_display_name == ""


def test_context_section_accepts_overrides():
    cfg = SendingEmojisConfig(context={"enabled": True, "dedup_ttl_seconds": 120,
                                       "merge_window_seconds": 5,
                                       "max_reactions_per_message": 3,
                                       "self_display_name": "麦麦"})
    assert cfg.context.enabled is True
    assert cfg.context.dedup_ttl_seconds == 120
    assert cfg.context.merge_window_seconds == 5
    assert cfg.context.max_reactions_per_message == 3
    assert cfg.context.self_display_name == "麦麦"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --with pytest --with aiohttp pytest tests/test_config.py -q`
Expected: FAIL（`SendingEmojisConfig` 无 `context` 属性）

- [ ] **Step 3: 实现**

`config.py` 在 `BehaviorSection` 后插入：

```python
class ContextSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "上下文注入"
    __ui_order__: ClassVar[int] = 6

    enabled: bool = Field(
        default=False,
        json_schema_extra={
            "hint": (
                "实验性功能总开关：开启后表情回应（含麦麦自己的）去重、聚合后"
                "注入聊天历史，同时拦截主程序消息链上的表情回应通知消息。默认关闭。"
            ),
            "label": "启用上下文注入",
            "order": 0,
        },
    )
    dedup_ttl_seconds: int = Field(
        default=3600, ge=0, le=604800,
        json_schema_extra={
            "hint": "同一（回应者、被回应消息、表情）组合在窗口内只注入一次。",
            "label": "去重窗口（秒）",
            "order": 1,
            "step": 60,
        },
    )
    merge_window_seconds: int = Field(
        default=30, ge=0, le=3600,
        json_schema_extra={
            "hint": (
                "同一被回应消息同一表情的多条回应在此窗口内汇入同一条注入消息，"
                "窗口到期后写入聊天历史。"
            ),
            "label": "聚合窗口（秒）",
            "order": 2,
            "step": 5,
        },
    )
    max_reactions_per_message: int = Field(
        default=10, ge=1, le=100,
        json_schema_extra={
            "hint": "单条被回应消息最多产生的注入消息条数，防表情回应风暴撑爆上下文。",
            "label": "注入上限（每条消息）",
            "order": 3,
            "step": 1,
        },
    )
    self_display_name: str = Field(
        default="",
        json_schema_extra={
            "hint": (
                "注入消息里麦麦自己的显示名。留空则自动取主程序配置 bot.nickname；"
                "两者都取不到时注入文本不写名字。"
            ),
            "label": "麦麦显示名（兜底）",
            "order": 4,
            "placeholder": "留空自动获取",
        },
    )
```

`SendingEmojisConfig` 中 `scope` 前加：`context: ContextSection = Field(default_factory=ContextSection)`，并把 `scope` 的 `__ui_order__` 语义保持（ContextSection 为 6，ScopeSection 原为 6 改为 7）。

`config.toml` 在 `[scope]` 前插入：

```toml
# 上下文注入（实验性，默认关闭）。开启后表情回应去重、聚合后注入聊天历史，
# 同时拦截主程序消息链上的表情回应通知消息。
[context]
enabled = false
# 同一（回应者、被回应消息、表情）组合在此窗口内只注入一次
dedup_ttl_seconds = 3600
# 同一被回应消息同一表情的多条回应在此窗口内汇入同一条注入消息
merge_window_seconds = 30
# 单条被回应消息最多产生的注入消息条数
max_reactions_per_message = 10
# 麦麦自己的显示名；留空自动取主程序 bot.nickname，再取不到则不写名字
self_display_name = ""
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run --with pytest --with aiohttp pytest tests/test_config.py -q`
Expected: PASS（2 个新用例 + 原有全绿）

- [ ] **Step 5: Commit（先问用户）**

```bash
git add config.py config.toml tests/test_config.py
git commit -m "feat: add context injection config section"
```

---

### Task 2: 纯逻辑层 — 去重/聚合/可见文本/形态判定

**Files:**
- Modify: `reactions.py`（`ReactionState` 之后追加）
- Test: `tests/test_reactions.py`

**Interfaces:**
- Consumes: `ReactionEvent`（已有：`group_id/message_id/reactor_qq/emoji_id/removed`）
- Produces（后续 Task 3/4 依赖）:
  - `class InjectionAggregate`（`message_id: str`、`emoji_id: str`、`responders: list[str]`、`first_ts: float`）
  - `class InjectionState(dedup_ttl_seconds: float = 3600.0, merge_window_seconds: float = 30.0, max_reactions_per_message: int = 10, max_slots: int = 2000)`，方法：
    - `record(event: ReactionEvent, display_reactor: str, now: float) -> str`：返回 `"new"`（开启新聚合）/ `"merged"`（并入现有）/ `"skip"`（去重命中或超上限）
    - `due(now: float) -> list[InjectionAggregate]`：取出并移除到期聚合（`now - first_ts >= merge_window`）
    - `mark_injected(message_id: str) -> None`：flush 成功后计数，供上限判断
    - `prune(now: float) -> None`：TTL 清理去重表
  - `build_reactor_display(reactor_qq: str, self_qq: str, bot_display_name: str) -> str`：麦麦自己 → 显示名（可能空串），他人 → QQ 号
  - `emoji_id_to_display(emoji_id: str) -> str`：长度 > 3 的纯数字当 Unicode 码点转字符，其余原样（与协议端 `code.length > 3` 判型一致）
  - `build_reaction_visible_text(message_id: str, emoji_id: str, responders: list[str]) -> str`
  - `is_reaction_notice_message(message: Any) -> bool`：Hook 载荷判定

- [ ] **Step 1: 写失败测试**

`tests/test_reactions.py` 追加：

```python
def make_event(message_id="1001", reactor="20002", emoji="128077", group="30003"):
    return ReactionEvent(group_id=group, message_id=message_id,
                         reactor_qq=reactor, emoji_id=emoji)


class TestInjectionState:
    def test_same_key_deduped_within_window(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(), "20002", now + 1) == "skip"

    def test_same_key_allowed_after_window_expiry(self):
        st = InjectionState(dedup_ttl_seconds=3600, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        st.due(now + 3601)  # 真实调用序列里旧聚合早已到期 flush，这里手动清掉
        assert st.record(make_event(), "20002", now + 3601) == "new"

    def test_same_key_after_expiry_merges_if_aggregate_alive(self):
        st = InjectionState(dedup_ttl_seconds=10, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        # dedup 过期但聚合未到期（真实序列里 dedup_ttl > merge_window 时会命中）
        assert st.record(make_event(), "20002", now + 11) == "merged"
        (agg,) = st.due(now + 31)
        assert agg.responders == ["20002"]

    def test_same_message_same_emoji_different_reactors_merge(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(reactor="20003"), "20003", now + 1) == "merged"
        (agg,) = st.due(now + 31)
        assert agg.responders == ["20002", "20003"]

    def test_same_reactor_not_duplicated_in_aggregate(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        # 同键应被 dedup 拦，换一个键但同 reactor（不同 emoji）验证不重复列名
        ev = make_event(emoji="129303")
        assert st.record(ev, "20002", now + 1) == "new"

    def test_different_emojis_get_separate_aggregates(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        assert st.record(make_event(), "20002", now) == "new"
        assert st.record(make_event(emoji="129303"), "20002", now + 1) == "new"
        due = st.due(now + 31)
        assert len(due) == 2

    def test_injection_cap_drops_extra_aggregates(self):
        st = InjectionState(merge_window_seconds=30, max_reactions_per_message=2)
        now = time.time()
        assert st.record(make_event(emoji="128077"), "20002", now) == "new"
        assert st.record(make_event(emoji="129303"), "20002", now + 1) == "new"
        assert st.record(make_event(emoji="128512"), "20002", now + 2) == "skip"

    def test_due_only_returns_expired_aggregates(self):
        st = InjectionState(merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        st.record(make_event(emoji="129303", reactor="20003"), "20003", now + 5)
        due = st.due(now + 31)
        assert len(due) == 1
        assert due[0].emoji_id == "128077"
        # 第二个聚合 first_ts=now+5，now+35 到期，now+40 应返回且取出即移除
        assert len(st.due(now + 40)) == 1
        assert st.due(now + 40) == []

    def test_prune_clears_stale_dedup_keys(self):
        st = InjectionState(dedup_ttl_seconds=10, merge_window_seconds=30)
        now = time.time()
        st.record(make_event(), "20002", now)
        st.prune(now + 11)
        st.due(now + 31)  # 清掉到期聚合（merge_window=30），避免 merged
        assert st.record(make_event(), "20002", now + 11) == "new"


class TestVisibleText:
    def test_single_reactor(self):
        text = build_reaction_visible_text("1001", "128077", ["20002"])
        assert text == "[表情回应] 20002 给某条消息(id=1001)贴了 👍"

    def test_multiple_reactors_with_count(self):
        text = build_reaction_visible_text("1001", "128077", ["20002", "20003"])
        assert text == "[表情回应] 20002、20003 给某条消息(id=1001)贴了 👍（2人）"

    def test_no_name_when_responders_empty(self):
        text = build_reaction_visible_text("1001", "76", [])
        assert text == "[表情回应] 给某条消息(id=1001)贴了 76"

    def test_long_message_id_truncated(self):
        text = build_reaction_visible_text("12345678901234567890", "128077", ["20002"])
        assert text == "[表情回应] 20002 给某条消息(id=12345678…)贴了 👍"


class TestEmojiDisplay:
    def test_qq_face_id_passthrough(self):
        assert emoji_id_to_display("76") == "76"

    def test_unicode_codepoint_to_char(self):
        assert emoji_id_to_display("128077") == "👍"

    def test_emoji_char_passthrough(self):
        assert emoji_id_to_display("👍") == "👍"


class TestReactorDisplay:
    def test_self_uses_bot_display_name(self):
        assert build_reactor_display("40004", "40004", "麦麦") == "麦麦"

    def test_self_without_display_name_empty(self):
        assert build_reactor_display("40004", "40004", "") == ""

    def test_other_uses_qq(self):
        assert build_reactor_display("20002", "40004", "麦麦") == "20002"


class TestReactionNoticeMessage:
    def test_emoji_like_notify_matched(self):
        msg = {"is_notify": True,
               "message_info": {"additional_config": {"napcat_notice_type": "group_msg_emoji_like"}}}
        assert is_reaction_notice_message(msg) is True

    def test_plain_message_not_matched(self):
        msg = {"is_notify": False, "message_info": {"additional_config": {}}}
        assert is_reaction_notice_message(msg) is False

    def test_unrelated_notify_not_matched(self):
        msg = {"is_notify": True,
               "message_info": {"additional_config": {"napcat_notice_type": "group_recall"}}}
        assert is_reaction_notice_message(msg) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --with pytest --with aiohttp pytest tests/test_reactions.py -q`
Expected: FAIL（ImportError: 无 InjectionState 等）

- [ ] **Step 3: 实现**

`reactions.py` 末尾追加：

```python
@dataclass
class InjectionAggregate:
    """聚合窗口内同消息同表情的多条回应的合并体。"""

    message_id: str
    emoji_id: str
    responders: list[str]
    first_ts: float


class InjectionState:
    """表情回应的去重与聚合，纯内存 + TTL 裁剪。见 docs/adr/0003。

    - 去重键 (responder, message_id, emoji_id)：窗口内同键只注入一次
    - 聚合键 (message_id, emoji_id)：窗口内多人同表情合并为一条注入消息
    - 注入上限：单条消息最多开启 max_reactions_per_message 个聚合
    """

    def __init__(
        self,
        dedup_ttl_seconds: float = 3600.0,
        merge_window_seconds: float = 30.0,
        max_reactions_per_message: int = 10,
        max_slots: int = 2000,
    ) -> None:
        self.dedup_ttl_seconds = max(float(dedup_ttl_seconds), 0.0)
        self.merge_window_seconds = max(float(merge_window_seconds), 0.0)
        self.max_reactions_per_message = max(int(max_reactions_per_message), 1)
        self.max_slots = max(int(max_slots), 1)
        self._dedup: dict[tuple[str, str, str], float] = {}
        self._aggregates: dict[tuple[str, str], InjectionAggregate] = {}
        self._injected_count: dict[str, int] = {}

    def record(self, event: ReactionEvent, display_reactor: str, now: float) -> str:
        """记录一次表情回应，返回 'new' / 'merged' / 'skip'。

        去重键按 reactor_qq（回应者 = QQ 用户，见 CONTEXT.md）；display_reactor
        只是显示名。dedup 过期后的同键回应：聚合仍存在则并入（responders 已去重，
        不会重复列名），否则开启新聚合。
        """
        key = (event.reactor_qq, event.message_id, event.emoji_id)
        last = self._dedup.get(key)
        if last is not None and not (self.dedup_ttl_seconds and now - last > self.dedup_ttl_seconds):
            return "skip"
        self._dedup[key] = now

        agg_key = (event.message_id, event.emoji_id)
        aggregate = self._aggregates.get(agg_key)
        if aggregate is None:
            active = sum(1 for mid, _ in self._aggregates if mid == event.message_id)
            if active + self._injected_count.get(event.message_id, 0) >= self.max_reactions_per_message:
                return "skip"
            self._aggregates[agg_key] = InjectionAggregate(
                message_id=event.message_id,
                emoji_id=event.emoji_id,
                responders=[display_reactor] if display_reactor else [],
                first_ts=now,
            )
            return "new"
        if display_reactor and display_reactor not in aggregate.responders:
            aggregate.responders.append(display_reactor)
        return "merged"

    def due(self, now: float) -> list[InjectionAggregate]:
        """取出并移除全部到期聚合。"""
        due = [
            agg
            for agg in self._aggregates.values()
            if now - agg.first_ts >= self.merge_window_seconds
        ]
        for agg in due:
            self._aggregates.pop((agg.message_id, agg.emoji_id), None)
        return due

    def mark_injected(self, message_id: str) -> None:
        key = str(message_id or "")
        if key:
            self._injected_count[key] = self._injected_count.get(key, 0) + 1

    def prune(self, now: float) -> None:
        """按 TTL 清理去重表与注入计数中的过期条目，并裁剪总量上界。"""
        if self.dedup_ttl_seconds:
            expired = [k for k, ts in self._dedup.items() if now - ts > self.dedup_ttl_seconds]
            for key in expired:
                self._dedup.pop(key, None)
        overflow = len(self._dedup) - self.max_slots
        if overflow > 0:
            for key, _ in sorted(self._dedup.items(), key=lambda kv: kv[1])[:overflow]:
                self._dedup.pop(key, None)


def build_reactor_display(reactor_qq: str, self_qq: str, bot_display_name: str) -> str:
    """回应者在注入文本里的显示名：麦麦自己用机器人显示名，他人用 QQ 号。"""
    if self_qq and reactor_qq == self_qq:
        return str(bot_display_name or "").strip()
    return str(reactor_qq or "").strip()


def emoji_id_to_display(emoji_id: str) -> str:
    """把归一后的表情 ID 还原为展示形式：长纯数字当 Unicode 码点转字符，其余原样。

    与协议端 set-reaction 的 `code.length > 3` 判型保持一致（见 AGENTS.md）。
    """
    text = str(emoji_id or "").strip()
    if text.isdigit() and len(text) > 3:
        try:
            return chr(int(text))
        except ValueError:
            return text
    return text


def build_reaction_visible_text(message_id: str, emoji_id: str, responders: list[str]) -> str:
    """构建注入消息的可见文本。回应者可能为空（显示名取不到时）。"""
    mid = str(message_id or "")
    if len(mid) > 8:
        mid = mid[:8] + "…"
    names = "、".join(responders)
    count = f"（{len(responders)}人）" if len(responders) > 1 else ""
    prefix = f"{names} " if names else ""
    return f"[表情回应] {prefix}给某条消息(id={mid})贴了 {emoji_id_to_display(emoji_id)}{count}"


def is_reaction_notice_message(message: Any) -> bool:
    """判定主程序消息链上的一条通知消息是否为表情回应形态（防御式拦截用）。

    只认主程序适配器约定写入的 notice_type 字段（bot.py 注释：additional_config
    含 napcat_notice_type / napcat_notice_sub_type / napcat_notice_payload），
    认不出返回 False 放行——不臆测未知形态（ADR 0003）。
    """
    if not isinstance(message, dict):
        return False
    if not message.get("is_notify"):
        return False
    message_info = message.get("message_info")
    if not isinstance(message_info, dict):
        return False
    additional = message_info.get("additional_config")
    if not isinstance(additional, dict):
        return False
    notice_type = str(
        additional.get("napcat_notice_type") or additional.get("notice_type") or ""
    ).lower()
    return "emoji_like" in notice_type or "reaction" in notice_type
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run --with pytest --with aiohttp pytest tests/test_reactions.py -q`
Expected: PASS（新增用例 + 原有全绿）

- [ ] **Step 5: Commit（先问用户）**

```bash
git add reactions.py tests/test_reactions.py
git commit -m "feat: add reaction dedup/aggregation pure logic"
```

---

### Task 3: 编排层 — 注入路径接入 webhook 处理

**Files:**
- Modify: `tests/fake_maibot_sdk.py`（补 `HookHandler` 假装饰器）
- Modify: `tests/test_plugin.py`（扩展 FakeCtx：`config.get` / `chat.get_stream_by_group_id` / `maisaka.context.append`）
- Modify: `plugin.py`（`_injection_state`、`_bot_display_name`、`_stream_ids`、`_inject_reaction`、`_on_reaction` 接入）
- Test: `tests/test_plugin.py`

**Interfaces:**
- Consumes: Task 1 `ContextSection`；Task 2 `InjectionState` / `build_reactor_display` / `build_reaction_visible_text`
- Produces: `plugin.SendingEmojisPlugin._inject_reaction(event)`（内部）、`_fetch_bot_display_name()`（内部）、`_get_stream_id(group_id)`（内部）
- 已知 SDK 行为：`ctx.config.get(key, default)` 返回 unwrap 后的 `value`；`ctx.chat.get_stream_by_group_id(group_id)` 返回 stream dict（含 `stream_id`）或 None；`ctx.maisaka.context.append(...)` 返回完整 dict（含 `success` 键）
- segments 格式：`[{"type": "text", "data": <文本>}]`（host `_component_from_dict`：text 段取 `data` 为字符串）

- [ ] **Step 1: 扩展测试替身**

`tests/fake_maibot_sdk.py` 追加（plugin.py 即将从 maibot_sdk import HookHandler）：

```python
class _FakeHookInfo:
    def __init__(self, hook: str, **kwargs: Any) -> None:
        self.hook = hook
        self.kwargs = kwargs


def HookHandler(hook: str, **kwargs: Any) -> Any:
    """假装饰器：附着元数据并原样返回函数，测试只直接调用被装饰方法。"""

    def decorator(func: Any) -> Any:
        setattr(func, "_hook_info", _FakeHookInfo(hook, **kwargs))
        return func

    return decorator
```

`tests/test_plugin.py` 的 FakeCtx 扩展（在现有 FakeCtx 类内加能力替身）：

```python
class FakeConfig:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class FakeChat:
    def __init__(self) -> None:
        self.streams: dict[str, dict[str, Any] | None] = {}

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> Any:
        return self.streams.get(str(group_id))


class FakeMaisakaContext:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False

    async def append(self, stream_id: str, segments: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"stream_id": stream_id, "segments": segments, **kwargs})
        return {"success": not self.fail_next, "index": len(self.calls) - 1}


class FakeMaisaka:
    def __init__(self) -> None:
        self.context = FakeMaisakaContext()
```

`FakeCtx.__init__` 里加：`self.config = FakeConfig()`、`self.chat = FakeChat()`、`self.maisaka = FakeMaisaka()`。

`make_plugin` 里在 `_rebuild_runtime()` 前补默认替身数据（注意：现有测试 helper `event()` 默认 `group_id="10001"`，stream 表键必须对齐）：

```python
    plugin.ctx.config.values["bot.nickname"] = "麦麦"
    plugin.ctx.chat.streams["10001"] = {
        "stream_id": "stream-10001", "session_id": "stream-10001",
        "group_id": "10001", "is_group_session": True,
    }
```

- [ ] **Step 2: 写失败测试**

`tests/test_plugin.py` 追加：

```python
def enable_injection(plugin: SendingEmojisPlugin) -> None:
    plugin.config.context.enabled = True
    plugin._rebuild_runtime()


def test_injection_disabled_by_default_no_append():
    plugin = make_plugin()
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_enabled_appends_visible_text():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    call = plugin.ctx.maisaka.context.calls[0]
    assert call["stream_id"] == "stream-10001"
    assert call["segments"] == [{"type": "text", "data": call["visible_text"]}]
    assert call["source_kind"] == "reaction"
    assert call["message_id"] == "1001"
    assert "20002" in call["visible_text"]
    assert "👍" in call["visible_text"]


def test_injection_dedupes_same_key():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert len(plugin.ctx.maisaka.context.calls) == 1


def test_injection_merges_same_emoji_same_message():
    plugin = make_plugin()
    plugin.config.context.merge_window_seconds = 0  # 立即 flush，仍能验证合并
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq="20003")))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    text = plugin.ctx.maisaka.context.calls[0]["visible_text"]
    assert "20002" in text and "20003" in text


def test_injection_self_reaction_uses_bot_display_name():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq=BOT_QQ)))
    assert len(plugin.ctx.maisaka.context.calls) == 1
    text = plugin.ctx.maisaka.context.calls[0]["visible_text"]
    assert "麦麦" in text


def test_injection_display_name_falls_back_to_config():
    plugin = make_plugin()
    plugin.ctx.config.values["bot.nickname"] = ""
    plugin.config.context.self_display_name = "麦麦酱"
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq=BOT_QQ)))
    assert "麦麦酱" in plugin.ctx.maisaka.context.calls[0]["visible_text"]


def test_injection_no_display_name_omits_name():
    plugin = make_plugin()
    plugin.ctx.config.values["bot.nickname"] = ""
    plugin._bot_display_name = ""
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", reactor_qq=BOT_QQ)))
    text = plugin.ctx.maisaka.context.calls[0]["visible_text"]
    assert text.startswith("[表情回应] 给某条消息")


def test_injection_blacklisted_group_skipped():
    plugin = make_plugin()
    plugin.config.scope.group_blacklist = ["10001"]
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_ignored_reactor_skipped_but_self_injected():
    plugin = make_plugin()
    plugin.config.scope.ignore_reactors = ["20002"]
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []
    asyncio.run(plugin._on_reaction(event(message_id="1002", reactor_qq=BOT_QQ)))
    assert len(plugin.ctx.maisaka.context.calls) == 1


def test_injection_unknown_stream_skipped():
    plugin = make_plugin()
    plugin.ctx.chat.streams["10001"] = None
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert plugin.ctx.maisaka.context.calls == []


def test_injection_append_failure_logs_warning():
    plugin = make_plugin()
    enable_injection(plugin)
    plugin.ctx.maisaka.context.fail_next = True
    asyncio.run(plugin._on_reaction(event(message_id="1001")))
    assert any("注入" in t for t in plugin.ctx.logger.texts("warning"))


def test_injection_removed_reaction_not_injected():
    plugin = make_plugin()
    enable_injection(plugin)
    asyncio.run(plugin._on_reaction(event(message_id="1001", removed=True)))
    assert plugin.ctx.maisaka.context.calls == []
```

注意：复用现有 helper `event(**overrides)`（默认 `group_id="10001"`、`message_id="555"`、`reactor_qq="20002"`），不新增 helper；所有 async 调用统一 `asyncio.run(...)`（与现有测试一致）。

- [ ] **Step 3: 运行确认失败**

Run: `uv run --with pytest --with aiohttp pytest tests/test_plugin.py -q`
Expected: FAIL（ImportError: HookHandler 缺失；`_inject_reaction` 未实现）

- [ ] **Step 4: 实现**

`plugin.py` 修改：

a) import 追加（`from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin`；`from .reactions import` 追加 `InjectionState, build_reactor_display, build_reaction_visible_text`）：

```python
from .reactions import (
    OPPOSITE,
    Decision,
    InjectionState,
    ReactionEvent,
    ReactionState,
    build_opposite_map,
    build_reactor_display,
    build_reaction_visible_text,
    coerce_message_id,
    decide_reaction,
    group_allowed,
    is_missing_action,
    is_reaction_notice,
    parse_message_author,
    parse_reaction_report,
    parse_self_qq,
    protocol_call_failed,
)
```

b) 类属性追加：

```python
    _injection_state: InjectionState | None
    _bot_display_name: str
    _stream_ids: dict[str, str]
```

c) `on_load` 中 `_rebuild_runtime()` 后追加：

```python
        if self._injection_state is not None:
            self._bot_display_name = await self._fetch_bot_display_name()
            self.ctx.logger.info(
                f"上下文注入已启用，麦麦显示名: {self._bot_display_name or '（无，注入时不写名字）'}"
            )
```

d) `_rebuild_runtime` 尾部追加（context.enabled 时重建注入状态；显示名与 stream 缓存随状态重建）：

```python
        context = self.config.context
        self._stream_ids = {}
        self._bot_display_name = ""
        self._injection_state = (
            InjectionState(
                dedup_ttl_seconds=float(context.dedup_ttl_seconds),
                merge_window_seconds=float(context.merge_window_seconds),
                max_reactions_per_message=int(context.max_reactions_per_message),
            )
            if context.enabled
            else None
        )
```

e) `_on_reaction` 开头注入路径（行为路径保持不动）：

```python
    async def _on_reaction(self, event: ReactionEvent) -> None:
        try:
            if self._injection_state is not None:
                await self._inject_reaction(event)
            decision = await self._evaluate(event)
            if decision is None:
                return
            now = time.time()
            # 先占表情槽再发，避免同一条消息的并发上报重复贴。
            self._state.take_slot(event.message_id, now)
            self._state.mark_group(event.group_id, now)
            await self._delay()
            await self._send_reaction(event, decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"处理表情回应失败: {exc}")
```

f) `_evaluate` 之后新增注入路径方法（放在 `_is_self_message` 前）：

```python
    # ---- 上下文注入（实验性）----

    async def _inject_reaction(self, event: ReactionEvent) -> None:
        """去重、聚合后把表情回应注入聊天历史。失败只记日志，不阻塞行为路径。"""
        if self._injection_state is None or event.removed:
            return
        scope = self.config.scope
        if not group_allowed(event.group_id, scope.group_whitelist, scope.group_blacklist):
            return
        ignored = {str(qq).strip() for qq in (scope.ignore_reactors or []) if str(qq).strip()}
        if event.reactor_qq and event.reactor_qq in ignored and event.reactor_qq != self._self_qq:
            return

        now = time.time()
        display = build_reactor_display(event.reactor_qq, self._self_qq, self._bot_display_name)
        result = self._injection_state.record(event, display, now)
        if result == "skip":
            return
        for aggregate in self._injection_state.due(now):
            await self._flush_aggregate(event.group_id, aggregate)
        self._injection_state.prune(now)

    async def _flush_aggregate(
        self, group_id: str, aggregate: Any
    ) -> None:
        stream_id = await self._get_stream_id(group_id)
        if not stream_id:
            self.ctx.logger.debug(f"取不到群 {group_id} 的 stream，跳过注入")
            return
        text = build_reaction_visible_text(aggregate.message_id, aggregate.emoji_id, aggregate.responders)
        try:
            response = await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "data": text}],
                visible_text=text,
                source_kind="reaction",
                message_id=aggregate.message_id,
            )
        except Exception as exc:
            self.ctx.logger.warning(f"注入表情回应上下文调用失败: {exc}")
            return
        if not (isinstance(response, dict) and response.get("success")):
            self.ctx.logger.warning(f"注入表情回应上下文失败: {response}")
            return
        self._injection_state.mark_injected(aggregate.message_id)
        self.ctx.logger.info(
            f"已注入表情回应上下文 group={group_id} msg={aggregate.message_id} "
            f"emoji={aggregate.emoji_id} 回应者={aggregate.responders}"
        )

    async def _get_stream_id(self, group_id: str) -> str:
        """群号 → 聊天流 ID，带缓存；查不到返回空串。"""
        cached = self._stream_ids.get(group_id)
        if cached is not None:
            return cached
        stream_id = ""
        try:
            stream = await self.ctx.chat.get_stream_by_group_id(group_id)
        except Exception as exc:
            self.ctx.logger.debug(f"查群 {group_id} 的 stream 失败: {exc}")
            stream = None
        if isinstance(stream, dict):
            stream_id = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        self._stream_ids[group_id] = stream_id
        return stream_id

    async def _fetch_bot_display_name(self) -> str:
        """机器人显示名来源链：配置兜底 → 主程序 bot.nickname → 空。"""
        configured = str(self.config.context.self_display_name or "").strip()
        if configured:
            return configured
        try:
            result = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:
            self.ctx.logger.debug(f"取主程序 bot.nickname 失败: {exc}")
            return ""
        return str(result or "").strip()
```

g) `on_unload` 无需改动（Hook 由运行时管理）。

- [ ] **Step 5: 运行确认通过**

Run: `uv run --with pytest --with aiohttp pytest tests/test_plugin.py -q`
Expected: PASS（新增用例 + 原有全绿）

- [ ] **Step 6: Commit（先问用户）**

```bash
git add plugin.py tests/fake_maibot_sdk.py tests/test_plugin.py
git commit -m "feat: inject deduped reaction context via maisaka.context.append"
```

---

### Task 4: 防御式拦截 — `chat.receive.before_process` Hook

**Files:**
- Modify: `plugin.py`
- Test: `tests/test_plugin.py`

**Interfaces:**
- Consumes: Task 2 `is_reaction_notice_message`；Task 1 `context.enabled`
- 已知 Hook 契约：`@HookHandler("chat.receive.before_process", mode=HookMode.BLOCKING, order=HookOrder.EARLY)`，回调 `async def f(self, **kwargs)`，返回 `{"action": "continue"|"abort", "modified_kwargs": kwargs}`（`allow_abort=True` 已核实）
- fake 里 `HookHandler` 装饰器已由 Task 3 提供（假装饰器，测试直接调方法）

- [ ] **Step 1: 写失败测试**

`tests/test_plugin.py` 追加：

```python
def _notify_message(notice_type: str = "group_msg_emoji_like") -> dict[str, Any]:
    return {
        "is_notify": True,
        "message_info": {"additional_config": {"napcat_notice_type": notice_type}},
    }


def test_hook_aborts_reaction_notify_when_enabled():
    plugin = make_plugin()
    enable_injection(plugin)
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message()))
    assert result["action"] == "abort"


def test_hook_passes_through_when_disabled():
    plugin = make_plugin()
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message()))
    assert result["action"] == "continue"


def test_hook_passes_plain_message():
    plugin = make_plugin()
    enable_injection(plugin)
    result = asyncio.run(
        plugin.guard_reaction_notify(message={"is_notify": False, "message_info": {}})
    )
    assert result["action"] == "continue"


def test_hook_passes_unrelated_notify():
    plugin = make_plugin()
    enable_injection(plugin)
    result = asyncio.run(plugin.guard_reaction_notify(message=_notify_message("group_recall")))
    assert result["action"] == "continue"
```

（`asyncio.run` 与现有测试风格一致；测试文件已有 `import asyncio`。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run --with pytest --with aiohttp pytest tests/test_plugin.py -q`
Expected: FAIL（`guard_reaction_notify` 不存在）

- [ ] **Step 3: 实现**

`plugin.py`：import 处改为

```python
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin
```

`from .reactions import` 追加 `is_reaction_notice_message`。类内新增（放在注入方法块之后）：

```python
    @HookHandler(
        "chat.receive.before_process",
        name="emoji_reaction_notify_guard",
        mode="blocking",
        order="early",
        description="防御式拦截表情回应通知消息，避免其进入主程序上下文（ADR 0003）。",
    )
    async def guard_reaction_notify(self, **kwargs: Any) -> dict[str, Any]:
        """表情回应通知消息在主链上 abort；其余放行。

        命中时打 info 日志——这条日志就是「适配器是否把表情回应当通知转发」的
        线上答案（ADR 0001/0003）。上下文注入关闭时不拦截。
        """
        if self._injection_state is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        message = kwargs.get("message")
        if is_reaction_notice_message(message):
            self.ctx.logger.info(
                f"拦截表情回应通知消息进入主程序: {str(message)[:200]}"
            )
            return {"action": "abort", "modified_kwargs": kwargs}
        return {"action": "continue", "modified_kwargs": kwargs}
```

注意：`mode`/`order` 传**小写**字符串——真实 SDK 的 `HookMode.BLOCKING = "blocking"`、`HookOrder.EARLY = "early"`（`REFERENCE/maibot-plugin-sdk/maibot_sdk/types.py`，str Enum，值为小写）。`HookHandler` 装饰时构造 pydantic 模型，传大写 `"BLOCKING"` 会抛 ValidationError 导致插件加载失败。直接传小写字符串值最稳（pydantic 接受值转枚举成员，fake 假装饰器接受任意 kwargs 亦兼容）；pyright 若报类型错误再改从 `maibot_sdk.types` 导入枚举。

- [ ] **Step 4: 运行确认通过**

Run: `uv run --with pytest --with aiohttp pytest tests/test_plugin.py -q`
Expected: PASS（新增用例 + 原有全绿）

- [ ] **Step 5: pyright 检查（mode/order 字符串是否报错）**

Run: `uv run --with pyright --with aiohttp pyright`
Expected: 零错误。若 `HookHandler(mode="BLOCKING")` 报类型错误，改为：

```python
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder
...
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
```

并确认 fake_maibot_sdk 无此枚举（假装饰器接受任意 kwargs，无碍）。

- [ ] **Step 6: Commit（先问用户）**

```bash
git add plugin.py tests/test_plugin.py
git commit -m "feat: defensively intercept reaction notify on chat receive hook"
```

---

### Task 5: emoji 编号参考文档

**Files:**
- Create: `docs/emoji-id-reference.md`

**Interfaces:**
- Consumes: `reactions.py` 的 `normalize_emoji_id` / `DEFAULT_OPPOSITE_PAIRS`；SnowLuma `packages/core/src/sys-face-catalog.ts`、`packages/onebot/src/actions/system-face.ts`（REFERENCE 已 clone）
- 约束：不臆造 QQ 小黄脸编号语义（CLAUDE.md 关键规则）；文档正文中文，技术术语保留英文

- [ ] **Step 1: 写文档**

`docs/emoji-id-reference.md`：

```markdown
# 表情 ID 参考

表情回应在协议端的标识（**表情 ID**，见 CONTEXT.md）有两套编号并存。本文件是
配置与排查时的查表参考；全量目录在协议端运行时侧，见文末。

## 两套编号体系

| 体系 | 形态 | 示例 | 归一后 |
|---|---|---|---|
| QQ 小黄脸 | 1~3 位纯数字编号 | `4`、`76` | 原样 |
| Unicode emoji | 码点十进制（≥4 位数字） | 👍 → `128077` | 原样 |
| emoji 字符（配置输入） | 字符本身 | `👍` | 取首码点 `128077` |

归一规则见 `reactions.py` 的 `normalize_emoji_id`：纯数字原样保留；emoji 字符取
首个码点。协议端发送侧用 `code.length > 3` 判类型（≤3 位当 QQ 小黄脸、更长当
Unicode 码点），归一正好抹平「上报给字符、发送要码点」的不对称。

## QQ 小黄脸编号边界

编号语义没有公开权威静态表，以下边界来自 SnowLuma（`REFERENCE/snowluma`）
`packages/protocol/src/sys-face-store.ts` 的 `faceWireFor`（core 下同名文件只是
storage 绑定模块）：

- `face_id < 260`：classic 小黄脸
- `260 <= face_id`：small（新式表情）
- 超级表情：目录条目带 `ani_sticker_pack_id` / `ani_sticker_id` /
  `ani_sticker_type` 字段，不是普通编号

本插件**不内置** QQ 小黄脸编号→语义的映射（不臆造私有编号语义），配置反义对
时如需私有编号，请从协议端全量目录（下文）查证后填入 `extra_pairs`。

## 内置反义映射的编号对照

内置反义映射（`reactions.py` 的 `DEFAULT_OPPOSITE_PAIRS`）全部使用 Unicode
emoji，归一后编号如下：

| 表情 | 码点 | 表情 | 码点 |
|---|---|---|---|
| 👍 | 128077 | 👎 | 128078 |
| ❤ | 10084 | 💔 | 128148 |
| 😂 | 128514 | 😭 | 128557 |
| 😄 | 128516 | 😡 | 128545 |
| ✅ | 9989 | ❌ | 10060 |
| 🔥 | 128293 | ❄ | 10052 |
| ⬆ | 11014 | ⬇ | 11015 |
| 😍 | 128525 | 🤢 | 129314 |
| 😈 | 128520 | 😇 | 128519 |
| 🤮 | 129326 | 😋 | 128523 |
| 🥶 | 129398 | 🥵 | 129397 |
| ☀ | 9728 | 🌧 | 127783 |
| 🚀 | 128640 | 🐢 | 128034 |
| 😊 | 128522 | 😠 | 128544 |
| 🎉 | 127881 | 😢 | 128546 |
| ➕ | 10133 | ➖ | 10134 |

（码点可在 Python 里复核：`str(ord("👍"))`。）

## 全量目录获取

QQ 系统表情目录在协议端运行时从 QQ 服务器拉取并落盘。SnowLuma 暴露
`fetch_sys_faces` OneBot 动作（`packages/onebot/src/actions/system-face.ts`）：

```
POST http://<协议端地址>/fetch_sys_faces
{"refresh": false}
```

返回 `packs: [{ pack_name, emojis: [{ q_sid, q_des, em_code, q_cid, is_super, ... }] }]`。
同机部署时目录快照在协议端容器的 `data/sys-face-catalog.json`。`q_sid` 是目录
标识（部分为 Unicode 表情字符串）、`q_des` 是表情描述——这是查证私有编号语义
的权威来源。
```

- [ ] **Step 2: 复核码点表**

Run:

```bash
cd /Users/arcdent/github/SmallProjects/sending-emojis-plugin && python3 -c "
from reactions import DEFAULT_OPPOSITE_PAIRS
for a, b in DEFAULT_OPPOSITE_PAIRS:
    print(a, ord(a), b, ord(b))
"
```

Expected: 输出与文档表格一致（若不一致，修正文档——以代码为准）。

- [ ] **Step 3: Commit（先问用户）**

```bash
git add docs/emoji-id-reference.md
git commit -m "docs: add emoji id reference"
```

---

### Task 6: 收尾 — 文档同步与全量验证

**Files:**
- Modify: `README.md`（功能说明补上下文注入；配置表补 `[context]`）
- Modify: `AGENTS.md`（结构补 `docs/emoji-id-reference.md`；关键规则补注入语义；验证命令不变）

**Interfaces:**
- Consumes: 全部已完成任务

- [ ] **Step 1: 更新 README.md**

在功能描述与配置说明处补充上下文注入（实验性、默认关闭、去重+聚合语义、显示名来源链），配置表加 `[context]` 一节（照抄 config.toml 注释语义）。

- [ ] **Step 2: 更新 AGENTS.md**

- 结构段：`docs/` 下补 `emoji-id-reference.md`（编号体系查表）与 ADR 0003
- 关键规则段补：上下文注入默认关闭（`context.enabled`）；注入路径复用 scope 白黑名单但麦麦自己的回应不受 `ignore_reactors` 影响；去重≠聚合
- 其余规则不动

- [ ] **Step 3: 全量验证**

Run: `uv run --with pytest --with aiohttp pytest tests/ -q`
Expected: 全部通过

Run: `uv run --with pyright --with aiohttp pyright`
Expected: 生产代码零错误

Run: `uv run --with ruff ruff check .`
Expected: 仅四类已知偏离（BLE001/N999/SIM103/SIM117），无新增

- [ ] **Step 4: 核对 config 与 config.toml 一致**

Run: `diff <(grep -E "^\s+\w+:" config.py | sort) <(grep -E "^[a-z_]+ =" config.toml | sed 's/ =.*//' | sort)`（目测核对 `[context]` 各字段两边一致）

- [ ] **Step 5: Commit（先问用户）**

```bash
git add README.md AGENTS.md
git commit -m "docs: document context injection and emoji id reference"
```

---

## Self-Review

**1. Spec coverage:**
- 需求 1（文档）→ Task 5 ✓
- 需求 2（拦截+去重+注入）→ Task 2 去重/聚合 + Task 3 注入编排 + Task 4 主链拦截 ✓
- 需求 3（麦麦自己）→ Task 2 `build_reactor_display` + Task 3 测试 ✓
- 需求 4（主程序后处理拦截）→ Task 4 Hook ✓
- 需求 5（实验性开关）→ Task 1 `context.enabled` 默认 False ✓
- 显示名来源链（用户追加要求）→ Task 3 `_fetch_bot_display_name` + Task 2 `build_reactor_display` ✓
- scope 复用 + 麦麦自己例外 → Task 3 `_inject_reaction` 门禁 + 测试 ✓
- 风暴限流（上限 + 同表情合并）→ Task 2 `max_reactions_per_message` + 聚合 ✓

**2. Placeholder scan:** 无 TBD/「类似 Task N」；所有代码步骤含完整实现。

**3. Type consistency:** `InjectionState.record` 返回 `"new"/"merged"/"skip"` 三值贯穿 Task 2/3；`build_reaction_visible_text(message_id, emoji_id, responders)` 签名 Task 2 定义、Task 3 调用一致；`is_reaction_notice_message` Task 2 定义、Task 4 调用一致；`_inject_reaction`/`_flush_aggregate`/`_get_stream_id`/`_fetch_bot_display_name` 均为 Task 3 内部方法，Task 4 不引用。可见文本格式断言（单/多/空/截断）在 Task 2 测试中先行锁定，Task 3 测试只断言子串存在，不会互相矛盾。
