# Design: 复读三出口改出口权重 + 复读域独立延迟与注入并发

## Context

现状与动机见 proposal Why、`docs/adr/0007`、`docs/adr/0008`。三处既有事实约束本设计：

- `repeats.decide_repeat` 已经是**单次** `roll()` + 累积阈值比较，改的是入参口径（两个概率 → 三个权重）与阈值构造方式，掷骰次数本来就是一次。`roll` / `choice` 依赖注入的可测形态保留。
- `plugin._process_repeat_message` 的同步原子段（门禁 → `record` 写终结标记 → `decide_repeat` → 出口判定 → 写冷却 → spawn）不得引入 await 点（ADR 0006 / 既有 design D3）。本变更只改传参与延迟取值，不动这个结构。
- 配置迁移由主程序负责：`plugin_runtime/runner/runner_main.py` 的 `_prepare_plugin_config_for_version_update` 在 `plugin.config_version` 升级时调 `rebuild_plugin_config_data(default_config, raw_config)`，以最新默认配置为骨架补新键、删废弃键、保留同名同类型旧值，并写回 `config.toml`。插件侧不需要自写迁移代码。

## Goals / Non-Goals

**Goals:**
- 配置口径与实现口径一致：部署者读到的就是「一颗骰、三个面」
- 复读的可见延迟压到亚秒级到 1.5 秒，且不牵动表情回应的节奏
- 注入不再站在发送的关键路径上，同时保住两路注入的先后顺序

**Non-Goals:**
- 不改表情回应域的任何行为与配置（两级概率骰、`behavior` 段默认值原样）
- 不改复读链累积、链终结、群冷却、门禁、planner 拦截语义
- 不引入注入超时、不引入发送重试/熔断、不改协议端调用的 session 生命周期
- 不做百分比 UI 展示或权重合法性联动校验（权重之和无约束）

## Decisions

### D1: 出口权重的形态与选择算法

`RepeatSection` 三个整数字段 `weight_repeat` / `weight_interrupt` / `weight_silence`（`ge=0`、`le=1000`、`step=5`），默认 **85 / 15 / 0**。`decide_repeat` 改签名收三个权重，算法：非负 clamp（负值/非法值按 0，对齐 `clamp_probability` 的垃圾值口径）→ 求和 → 和为 0 直接返回 `None`（沉默，不掷骰）→ `roll() * total` 与累加阈值按 复读 → 打断 → 沉默 的固定顺序比较，命中即返回。

沿用「一次 `roll()`」而不是 `random.choices`：`roll` 是既有的注入点，测试靠给定值精确命中某个出口；`random.choices` 要换成注入 `choices` 才可测，收益为零。

复用而非新增：`clamp_probability` 只处理 [0,1]，权重要的是非负实数，故在 `repeats.py` 内新增一个小的非负归一化辅助（纯函数、可单测），不改 `reactions.clamp_probability`。

### D2: 配置变更与版本升级

删除 `repeat.probability` / `repeat.interrupt_probability`，新增三个权重字段与 `repeat.delay_min_seconds` / `repeat.delay_max_seconds`（float，`ge=0`、`le=300`、默认 0.3 / 1.5，与 `behavior` 的延迟字段同型）。`plugin.config_version` 从 `1.0.0` 升到 `1.1.0`，`config.py` 与 `config.toml` 两侧默认值逐项一致（项目铁律）。

UI 文案要点：权重 hint 必须举例（「85 / 15 / 0 = 85% 复读、15% 打断、从不沉默」）并说明「三个数只有比例意义、同比例放大等价」；复读延迟的 label 写「复读发送延迟（最小/最大）」，与 `behavior` 段表情回应的「最小/最大延迟」区分开。

### D3: 延迟按域取值

`_delay()` 改成接收区间参数（`_delay(low, high)`）而不是复制第二份函数：表情回应路径传 `behavior.delay_min/max`，复读路径传 `repeat.delay_min/max_seconds`。`high < low` 时按 `low` 处理、`high <= 0` 时不等待的既有归一逻辑不变。

### D4: 编舞改并发

`_send_repeat_decision` 的第 1、2 步合并为 `asyncio.gather(触发消息注入, 延迟, return_exceptions=True)`，之后照原样直发 → 行为文本注入。

- **顺序保证**：行为文本注入仍在直发之后，直发在 gather 之后，故「触发消息注入 → 行为文本注入」的历史顺序天然成立。完全脱钩（fire-and-forget）会丢掉这个保证，已在 ADR 0008 拒绝。
- **`return_exceptions=True` 是必需的**：`_append_repeat_context` 自身已吞掉 stream 查询与 append 的异常，但 gather 若把任何未预期异常抛出来，外层 `except` 会直接 return、把直发一并丢掉——那违反「注入失败记 warning 继续」的既有语义。gather 返回的异常项记 warning 后继续。
- **耗时口径**：可见延迟从 `注入 RTT + 延迟` 变为 `max(注入 RTT, 延迟)`。注入 RTT 的真实量级未实测（本机 fake SDK 立即返回），线上用四条 info 日志的时间戳自证即可。

### D5: 测试策略

- `test_repeats.py`：权重签名的决策用例——注入固定 `roll` 值精确命中三个出口各一次；权重同比例放大后同一 `roll` 值命中同一出口；权重全 0 时不掷骰（注入会抛的 `roll` 替身证明未被调用，沿用 `test_reactions.py` 既有手法）；负值/非法值按 0。
- `test_plugin.py`：`make_plugin` 的概率默认值改权重默认值（复读权重拉满、打断与沉默归零），「断言不触发」的沉默用例改为**沉默权重拉满、其余归零**——不能用「全部归零」，那走的是不掷骰分支，证明不了掷骰发生过。
- 延迟与并发：断言复读路径读的是 `repeat` 段延迟（改 `behavior` 段不影响复读）；断言 `maisaka.context.append` 的调用顺序仍为 `repeat_trigger` 先、行为 `source_kind` 后；注入协程抛异常时断言直发照常发生。
- 既有用例里 `probability=` / `interrupt_probability=` 的传参全量替换。

### D6: 文档同步范围

AGENTS.md（CLAUDE.md 为其软链接）改两处规则：后台编舞那条的「延迟（沿用 `behavior.delay_min/max`，不新增配置项）」→ 复读段自有延迟 + 注入与延迟并发；「测试注意」的概率口径 → 权重口径。ADR 目录清单补 0007 / 0008 并标注 0006 决策 3 被取代。README 改三出口段（概率 → 权重、示例分布）与延迟描述。CHANGELOG 记 BREAKING（旧概率值不迁移）。CONTEXT.md 的**出口权重**术语已在阶段一写入，无需再改。

## Risks / Trade-offs

- [部署者手调过的旧概率值升级后丢失] → 键名变化导致主程序无法迁移值，已在 ADR 0007 接受；README / CHANGELOG 写明「升级后请重调一次权重」
- [默认沉默权重 0 使麦麦对达标复读必出声、触发消息必被拦截] → spec 明确「冷却压制期是唯一放行口」；README 说明恢复偶尔沉默的做法（给沉默权重）
- [注入 RTT 长期大于 1.5 秒时 gather 上界由 RTT 决定] → 不加超时（ADR 0008 已拒绝）；日志时间戳可自证，真出现再回到被拒选项
- [两组延迟字段部署者调错] → D2 的 label 与 hint 区分作用域
- [回滚到旧插件版本时磁盘上已是 1.1.0 配置] → 旧版本读不到 `probability` 键会用其 pydantic 默认（0.6/0.15），行为回到旧默认；但**残留的 `weight_*` / `repeat.delay_*` 键能否被旧版本容忍取决于 SDK `PluginConfigBase` 是否禁止未知字段，本机无法验证**（真实 SDK 只在容器内）。回滚时最稳的做法是手工删掉残留键并把 `config_version` 改回 `1.0.0`

## Migration Plan

1. 部署新版本插件；主程序检出 `config_version` 1.0.0 → 1.1.0，自动重建并写回 `config.toml`（补权重与延迟字段、删两个概率字段、保留其余用户值）
2. 部署者按需重调三个权重与复读延迟（旧概率值不会带过来）
3. 观测线上四条 info 日志的时间戳，确认可见延迟落在预期区间
4. 回滚：回退插件版本，并手工清掉 `config.toml` 里的 `weight_*` / `repeat.delay_*` 残留键、把 `config_version` 改回 `1.0.0`（见 Risks 的未验证项）

## Open Questions

- SDK `PluginConfigBase` 对未知字段的处理（忽略 / 报错）——只影响**回滚**路径的手工步骤多少，不影响本变更的 specs、实现方式与任务拆分；容器内实测一次即可结论化。
