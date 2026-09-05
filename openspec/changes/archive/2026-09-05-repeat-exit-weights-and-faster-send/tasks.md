# Tasks: repeat-exit-weights-and-faster-send

> 执行顺序即组顺序。组 1 改 `decide_repeat` 签名后 `tests/test_plugin.py` 会红到组 3 结束为止（调用点在 `plugin.py`）；中间态可红，全量绿的闸门是 3.8 与 4.4。
> 组 3 的公共 seam 统一为：tests 直调 `RepeaterPlugin.handle_repeat_message(**kwargs)`（blocking handler 的返回值）+ 其 spawn 的后台任务的三个可观测边界——monkeypatch `asyncio.sleep`、monkeypatch `_call_protocol`、fake `ctx.maisaka.context.append` 的调用记录。各条只写与该条相关的差异。

## 1. 纯逻辑层：出口权重决策

- [x] 1.1 在 `repeats.py` 新增出口权重归一化纯函数：三个权重各自按非负数 clamp（负数 / 非数字 / `None` 一律按 0），返回 clamp 后的三元组与总和
  - seam：`repeats` 模块导出（新函数），落点 `tests/test_repeats.py`
  - 验收：`-5` / `"很高"` / `None` → 0；`85` 原样；总和为三者之和；不改 `reactions.clamp_probability`（它只管 [0,1]）

- [x] 1.2 改写 `repeats.decide_repeat`：参数从 `probability` / `interrupt_probability` 换成 `weight_repeat` / `weight_interrupt` / `weight_silence`，一次 `roll()` 乘总和后按 复读 → 打断 → 沉默 顺序比较累加阈值；总和为 0 时不掷骰直接返回 `None`
  - seam：`repeats.decide_repeat(...)`（`roll` / `choice` 注入点保持）
  - 验收：给定 `roll` 值可精确命中三个出口各一次；`RepeatDecision.reason` 改述权重（不再出现 `p=`）；打断出口仍只带打断文本、`used_default_pool` 空池回落语义不变

- [x] 1.3 改写 `tests/test_repeats.py` 全部决策用例（`test_repeat_outlet_when_roll_below_probability`、`test_interrupt_outlet_starts_at_the_probability_boundary`、`test_silence_when_both_outlets_miss`、`test_silence_when_roll_reaches_one`、`test_interrupt_picks_from_the_configured_pool`、`test_empty_interrupt_pool_falls_back_to_builtin_defaults`、`test_missing_interrupt_pool_falls_back_to_builtin_defaults`、`test_garbage_probability_is_treated_as_zero` 及其余 `probability=` 传参处）
  - seam：同 1.2
  - 验收：新增「权重同比例放大（85/15/0 → 170/30/0）时同一 `roll` 值命中同一出口」与「三权重全 0 时 `roll` 替身未被调用」两条用例；`uv run --with pytest --with aiohttp pytest tests/test_repeats.py -q` 全绿

## 2. 配置层：字段增删与版本升级

- [x] 2.1 改 `config.py` 的 `RepeatSection`：删 `probability` / `interrupt_probability`，加 `weight_repeat=85` / `weight_interrupt=15` / `weight_silence=0`（`int`，`ge=0`、`le=1000`、`step=5`）与 `delay_min_seconds=0.3` / `delay_max_seconds=1.5`（`float`，`ge=0`、`le=300`、`step=0.1`）；`PluginSection.config_version` 改 `1.1.0`
  - seam：`RepeaterConfig().repeat` 与 `RepeaterConfig().plugin`
  - 验收：权重 hint 含「85 / 15 / 0 = 85% 复读、15% 打断、从不沉默」与「只有比例意义、同比例放大等价」；延迟 label 写「复读发送延迟（最小/最大）」，与 `[behavior]` 的「最小/最大延迟」不重名；`order` 序号在 `[repeat]` 内无冲突

- [x] 2.2 同步 `config.toml`：`[repeat]` 增删同款字段与默认值、`config_version` 同步；重写 `[repeat]` 段注释（`config.toml:49` 的「先掷复读，未中再掷打断」必须删掉）
  - seam：`config.toml` ↔ `RepeaterConfig` 默认值一致性（既有 `tests/test_config.py::test_config_toml_matches_model_defaults` 钉死）
  - 验收：该用例绿；toml 内无「两颗骰 / 第一颗 / 第二颗 / 未中再掷」残留；`[repeat]` 注释说明权重是一次掷骰的三个面

- [x] 2.3 更新 `tests/test_config.py`：`test_repeat_section_defaults` 与 `test_config_toml_repeat_defaults_match_design` 改断五个新字段，补一条复读延迟区间有序断言（对齐既有 `test_delay_window_is_ordered`）
  - seam：`RepeaterConfig().repeat`
  - 验收：`pytest tests/test_config.py -q` 全绿；断言用独立字面量（不 import 实现常量）；`config_version == "1.1.0"` 被断言

## 3. 编排层与 plugin 侧测试

- [x] 3.1 把 `_delay()` 改成 `_delay(low, high)`：表情回应路径传 `behavior.delay_min/max_seconds`，复读路径传 `repeat.delay_min/max_seconds`
  - seam：两条路径各自的 `asyncio.sleep` 时长（monkeypatch 记录）
  - 验收：`high < low` 按 `low`、`high <= 0` 不等待的既有归一逻辑保留；两条路径互不读对方的配置

- [x] 3.2 `_process_repeat_message` 的 `decide_repeat` 调用点改传三权重
  - seam：handler 返回值（`abort` / `continue`）
  - 验收：同步段仍无 await 点（门禁 → `record` → `decide_repeat` → `mark_group` → `_spawn`）；沉默出口的 info 日志与放行语义不变

- [x] 3.3 `_send_repeat_decision` 的触发消息注入与延迟改 `asyncio.gather(..., return_exceptions=True)`，异常项记 warning 后继续；直发与行为文本注入的先后不变
  - seam：`ctx.maisaka.context.append` 调用记录 + `_call_protocol("send_group_msg")` 调用记录
  - 验收：注入协程抛异常时直发照常发生且日志有 warning；直发在两者都完成后才执行；成功路径 append 的 `source_kind` 序仍为 `repeat_trigger` → `repeat`/`interrupt`

- [x] 3.4 改 `make_plugin`：复读侧默认值从 `probability=1.0` / `interrupt_probability=0.0` 改为权重（复读拉满、打断与沉默归零），并把 `repeat.delay_min/max_seconds` 归零
  - seam：`tests/test_plugin.py::make_plugin`
  - 验收：复读延迟不归零会让每个复读用例真睡 0.3~1.5 秒——归零后整套用例耗时与改造前同量级；「断言不触发」的门禁用例在门禁失效时必然失败（不靠默认值蒙对）

- [x] 3.5 把沉默出口用例（`test_silence_outlet_does_not_enter_cooldown`、`test_blocking_continue_on_silence_outlet`、`test_silence_outlet_sends_nothing`）改为**沉默权重拉满、复读与打断归零**
  - seam：handler 返回值 + 零调用断言
  - 验收：不使用「三权重全 0」（那走不掷骰分支，证明不了掷骰发生过）；沉默日志断言保留

- [x] 3.6 改 `test_send_waits_behavior_delay_before_sending` 断复读段延迟，并新增「只改 `behavior.delay_min/max` 不影响复读延迟」用例
  - seam：monkeypatch `asyncio.sleep` 记录时长
  - 验收：睡眠时长等于 `repeat.delay_min/max_seconds` 设定值；新用例里 `behavior` 段设成 9 秒而复读仍按 `repeat` 段时长；表情回应侧既有延迟用例仍取 `behavior` 段

- [x] 3.7 改写 `test_direct_send_success_choreography_order`：`gather` 后「触发消息注入」与「延迟」的先后不再确定，断言改为「直发之前两者都已发生（集合/计数断言）+ 直发之后是行为文本注入」
  - seam：`events` 序列记录（append / delay / protocol 三类事件）
  - 验收：用例不再断言 `append` 与 `delay` 的相对顺序；仍钉死 `protocol:send_group_msg` 在两者之后、行为注入在直发之后；用例 docstring 更新为 gather 语义（不再写「四步编舞」）

- [x] 3.8 新增并发用例两条：注入耗时长于延迟时直发仍在两者完成后发出；注入抛异常时直发照常
  - seam：fake `context.append` 内插入可控 `await asyncio.sleep`
  - 验收：覆盖 delta spec 的「注入不叠加在延迟之前」与「历史顺序为先触发消息后行为文本」两个 Scenario；`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿

## 4. 文档与验证

- [x] 4.1 改 AGENTS.md 三处（CLAUDE.md 是其软链接，不单独改）：后台编舞规则的「延迟（沿用 `behavior.delay_min/max`，不新增配置项）」→ 复读段自有延迟 + 注入与延迟并发；三出口规则改述为出口权重；「测试注意」的概率口径 → 权重口径；`docs/adr` 索引补 0007 / 0008 并标注 0006 决策 3 被取代
  - seam：AGENTS.md 的复读域章节、结构章节的 adr 索引行、测试注意章节
  - 验收：全文无「沿用 `behavior.delay_min/max`」「复读概率 1.0、打断概率 0」「把复读概率与打断概率都归零」表述残留；`ls -l CLAUDE.md` 仍是指向 AGENTS.md 的软链接

- [x] 4.2 改 README：复读章节的三出口段改权重口径（含 85/15/0 的默认分布与「要恢复偶尔沉默就给沉默权重」），延迟描述区分复读域与表情回应域，配置表增删五个字段
  - seam：README「复读」章节与 `[repeat]` 配置表
  - 验收：无 `probability ≤ roll < probability + interrupt_probability` 式旧表述；配置表含 `weight_*` 三项与 `delay_min/max_seconds`，无两个旧概率字段

- [x] 4.3 在 CHANGELOG 记本次 BREAKING：两个概率字段删除、旧值不迁移需重调一次、默认沉默权重 0 的行为含义、复读延迟独立
  - seam：CHANGELOG 顶部条目
  - 验收：条目含「BREAKING」标记、`config_version` 1.1.0、以及「升级后请重调权重」的操作提示

- [x] 4.4 跑全量验证三条命令
  - seam：三条验证命令
  - 验收：`uv run --with pytest --with aiohttp pytest tests/ -q` 全过；`uv run --with pyright --with aiohttp pyright` 生产代码零错误；`ruff check` 无已知四类偏离之外的新增项

- [x] 4.5 归档或 sync 之后，把主 spec 里 `三出口决策与发送` 的 Scenario 标题「双概率皆未命中保持沉默」改为「沉默出口命中」
  - seam：`openspec/specs/message-repeat/spec.md`（delta 的 MODIFIED 不支持 Scenario 改名——`openspec validate --strict` 会把改名判成漏抄 Scenario，只能在主 spec 上直接改）
  - 验收：主 spec 无「双概率」表述；`openspec validate --specs --strict` 通过
