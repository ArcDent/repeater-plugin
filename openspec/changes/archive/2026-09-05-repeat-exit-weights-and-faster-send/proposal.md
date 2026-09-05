# 复读三出口改出口权重 + 复读域独立延迟与注入并发

## Why

两个问题。**配置口径与实现不一致**：三出口决策自始至终只掷一次骰（`repeats.decide_repeat` 单次 `roll()` + 累积阈值比较），但配置层把它暴露成两个各自独立的概率，`config.py:249/259` 的 hint 与 `config.toml:49` 的注释还写着「第一颗骰 / 第二颗骰、未中再掷」——默认 `0.6/0.15` 下实现给的是打断 15% / 沉默 25%，文案描述的条件概率是 6% / 34%，部署者照文案调概率必然调错（ADR 0007）。**复读发送太慢**：`_send_repeat_decision` 里触发消息注入被 `await` 在延迟之前，延迟又沿用表情回应域的 `behavior.delay_min/max`（1~6 秒，均值 3.5 秒），可见延迟 ≈ 注入 RTT + 1~6 秒 + 发送 RTT；群友的复读接龙是秒级的，麦麦跟上时话题已经过去（ADR 0008）。

## What Changes

- **BREAKING**：`repeat.probability` / `repeat.interrupt_probability` 删除，改为三个整数**出口权重** `repeat.weight_repeat` / `repeat.weight_interrupt` / `repeat.weight_silence`（默认 **85 / 15 / 0**，`ge=0`、上限 1000）。一次掷骰按归一化权重选中恰好一个出口；三个权重全为 0 时视为沉默、不掷骰。三出口严格互斥与「打断出口绝不附带复读文本」不变。
- **BREAKING**：默认沉默权重为 0 —— 复读链一达标必出声，触发消息也就必被 **planner 拦截**；唯一放行口是复读群冷却的压制期（冷却期内链达标不消耗资格、不拦截）。要恢复「偶尔沉默」必须显式给沉默权重。
- **BREAKING**：新增 `repeat.delay_min_seconds` / `repeat.delay_max_seconds`（float，默认 **0.3 / 1.5**），复读/打断直发只读这一对，不再沿用 `behavior.delay_min/max`；表情回应侧的 1~6 秒不变。ADR 0006 决策 3「延迟沿用 `behavior`、不新增配置项」作废。
- 触发消息注入与延迟**并发**（`asyncio.gather`）：可见延迟从「注入 RTT + 延迟」变为 `max(注入 RTT, 延迟)`。行为文本注入仍在直发之后，因此麦麦历史里「谁发了什么」始终早于「麦麦复读/打断了什么」。编舞的其余语义（注入无条件、直发失败止步、失败不重试）不变。
- `plugin.config_version` 升到 `1.1.0`：迁移交给主程序（`plugin_runtime/runner/runner_main.py` 检出版本升级后以最新默认配置为骨架重建 `config.toml`，补新键、删废弃键、保留同名同类型旧值）。旧的概率值因键名变化不会被带过来。
- 文档同步：`config.py` / `config.toml` 的「两颗骰」表述、README 的三出口与延迟段、AGENTS.md 的「延迟沿用 `behavior`」与测试口径两条规则、ADR 目录清单。
- 不改表情回应域：那里的两级概率骰（**回应骰** + **反义骰**）问的是两个不同问题（要不要回应 / 回什么），ADR 0002 继续有效。

## Capabilities

### New Capabilities

（无——全部落在既有能力的 delta 上）

### Modified Capabilities

- `message-repeat`: 「三出口决策与发送」需求改写（概率 → 归一化**出口权重**、一次掷骰的判定口径、延迟改用复读域自己的区间）；「触发消息注入」需求补充与延迟并发、两路注入的先后顺序保证；「门禁与冷却」需求补充「沉默权重为 0 时冷却压制期是触发消息唯一的放行口」。

## Impact

- `repeats.py`：`decide_repeat` 参数与决策口径改写（两个概率 → 三个权重 + 归一化选择），其余纯逻辑（归一、状态机、注入文案）不动。
- `config.py` / `config.toml`：`RepeatSection` 增删字段、`config_version` 升级；两文件默认值须逐项一致（项目铁律）。
- `plugin.py`：`_process_repeat_message` 传参改权重；`_send_repeat_decision` 改 `gather` 编舞；`_delay` 需要按域取延迟区间（表情回应仍用 `behavior`）。
- `tests/`：`test_repeats.py` 决策用例按新签名重写；`test_plugin.py` 的 `make_plugin` 概率默认值改权重，「断言不触发」的沉默用例从「两概率归零」改为「沉默权重拉满、其余归零」。
- 文档：README、AGENTS.md（CLAUDE.md 为其软链接，同一份）、`docs/adr/0007`、`docs/adr/0008`（均已写）、`CONTEXT.md`（**出口权重**术语已增补）。
- 部署：升级后需重调一次权重；config UI 出现两组延迟字段，label 必须写清作用域，否则会调错那一组。
