# Tasks: repeat-direct-send-intercept

## 1. 事实查证与决策层准备

- [x] 1.1 查 `arcwsl:~/github/REFERENCE/MaiBot/src/plugin_runtime/host/message_utils.py` 确认 hook 消息 dict 的 sender 昵称字段可得性，按 design D3 定触发消息注入的发送者显示名策略
  - seam：design.md 的 Open Questions 段（结论回写处，非代码任务）
  - 验收：Open Questions 更新为结论（昵称可得 → 注入显示名用昵称；不可得 → 用 QQ 号），附源码文件:行号依据

- [x] 1.2 检查 `repeats.py` 在 blocking 拦截场景下的语义适配：三出口互斥、文本携带不变；如需纯逻辑微调（触发消息上下文打包辅助），在 `repeats.py` 实现并补单测
  - seam：`repeats` 模块导出（`decide_repeat` / `RepeatState` / `extract_repeat_candidate`），落点 `tests/test_repeats.py`
  - 验收：`tests/test_repeats.py` 全绿，三出口与链终结既有用例零改动（若确需新增辅助，配新单测且不删既有用例）；不往 `plugin.py` 塞可测逻辑

## 2. handler blocking 化与拦截

- [x] 2.1 `observe_repeat` 的 `@HookHandler` 从 `mode="observe"` 改 `mode="blocking"`（`order="late"` 不变），实现按出口判定的返回值
  - seam：`RepeaterPlugin.observe_repeat(**kwargs)` 返回值（tests 直调 handler）
  - 验收：沉默出口 / 非候选 / `repeat.enabled=false` → `{"action": "continue", "modified_kwargs": <原 kwargs>}`；复读/打断出口 → spawn 后台任务后返回 `{"action": "abort", "modified_kwargs": <原 kwargs>}`

- [x] 2.2 `_process_repeat_message` 同步段保持纯内存无 await（门禁 → `record` → `decide_repeat` → `_spawn`），并把触发消息信息（群号、发送者 QQ、原始文本、message_id）与 `RepeatDecision` 打包传给后台任务
  - seam：handler 同步段内部结构（经 `RepeatState.record` 原子段单测 + 并发双触发防护语义保障）
  - 验收：`record` → `decide_repeat` → `_spawn` 之间无 await 点；后台任务收到 group_id、sender_qq、原始文本（非归一产物）、message_id、decision 五项

- [x] 2.3 handler 异常兜底（design D1）：未捕获异常记 warning 后返回 continue，`CancelledError` 照常上抛
  - seam：handler 入口（monkeypatch 内部处理抛 `Exception` / `CancelledError`）
  - 验收：抛 `Exception` 时返回 `{"action": "continue", ...}` 且 logger 有 warning；抛 `CancelledError` 时不吞、继续上抛

## 3. 直发与注入通路

- [x] 3.1 `_send_repeat_decision` 改造为四步编舞（design D2）：触发消息注入（失败记 warning 继续）→ 延迟 → `send_group_msg` 直发（失败止步）→ 行为文本注入（仅直发成功）
  - seam：monkeypatch `_call_protocol`（记录 `send_group_msg` 调用）+ fake `ctx.maisaka.context.append` 调用记录
  - 验收：成功路径调用序 = append(触发消息) → 延迟 → `send_group_msg` → append(行为文本)；直发返回 `None` 或 `protocol_call_failed` 时止步于触发消息注入；全程不再调用 `ctx.send.text`

- [x] 3.2 注入实现按 design D3：`source_kind` = `repeat_trigger` / `repeat` / `interrupt`；`message_id` 分别附触发消息 ID 与直发响应 `data.message_id`；visible_text 呈现发送者与文本、区分行为类型
  - seam：fake `ctx.maisaka.context.append` 的调用参数记录
  - 验收：三出口路径的 `source_kind` 与 `message_id` 断言通过；触发消息注入含发送者标识与原文；行为文本注入可区分复读/打断

- [x] 3.3 `_manifest.json` 移除 `send.text` 能力声明
  - seam：`tests/test_plugin.py::test_manifest_declares_every_consumed_capability`（既有能力一致性测试）
  - 验收：manifest `capabilities` 数组无 `send.text`；能力一致性与 `test_manifest_identity` 用例绿

## 4. 测试改造

- [x] 4.1 改写既有复读发送用例（`test_repeat_outlet_sends_last_raw_text`、`test_interrupt_outlet_sends_single_pool_text_without_repeat_text`、`test_send_waits_behavior_delay_before_sending`、`test_send_failure_result_logged_as_warning`、`test_send_failure_keeps_cooldown` 等）
  - seam：tests 直调 handler + monkeypatch `_call_protocol`（沿用贴表情用例的协议端 mock 惯例）
  - 验收：断言 `send_group_msg` 参数形态（`group_id`、`[{"type":"text","data":{"text":…}}]`）；复读发出原始文本、打断只发打断文本、失败只记 warning；`FakeSendCapability.text_calls` 为空

- [x] 4.2 新增 blocking 返回值用例
  - seam：`observe_repeat(**kwargs)` 直调
  - 验收：覆盖四类 continue（沉默出口 / 非候选 / `repeat.enabled=false` / handler 异常）与两类 abort（复读 / 打断）；`test_observe_handler_never_raises` 改造为断言返回 continue；`repeat.enabled=false` 用例同时断言 `context.enabled=true` 下也不拦截不注入

- [x] 4.3 新增注入用例（对照 delta spec 三个 ADDED Requirement 的全部 Scenario）
  - seam：fake `ctx.maisaka.context.append` 调用记录 + monkeypatch `_call_protocol`
  - 验收：拦截后上下文存在触发消息注入；直发失败时触发消息注入仍在、行为文本注入不出现；注入失败（stream 查不到 / append 失败）不阻断直发、不回滚拦截；文案区分复读/打断

- [x] 4.4 核对概率与时间戳约定（AGENTS.md 测试注意）
  - seam：新增与改写用例的配置值（`make_plugin` 覆盖）
  - 验收：断言触发的用例复读概率 1.0/打断 0（或反向拉满打断）；沉默用例双概率归零；涉及链终结/冷却 TTL 的用例时间戳用 `time.time()`；逐条审阅不存在依赖默认概率的「蒙对」用例

## 5. 文档与全量验证

- [x] 5.1 README 增部署提示：协议端 webhook 勿开 self-message 上报（`reportSelfMessage`）
  - seam：README 部署提示段落
  - 验收：提示段存在，含 `reportSelfMessage` 关键字与 ADR 0006 引用，说明开启后直发消息以 user 身份回流污染 planner 上下文的后果

- [x] 5.2 AGENTS.md 复读域规则改写：发送通路（`ctx.send.text` → 协议端直发）、检测模式（observe → blocking 拦截）、拦截与注入跟随 `repeat.enabled`
  - seam：AGENTS.md 复读域章节（CLAUDE.md 为同一软链接，不单独改）
  - 验收：复读域无 `ctx.send.text` 与 observe 模式表述残留；新规则与 ADR 0006 一致；「链终结同步原子段」等不变量规则保留；docs/adr 索引行补 0006

- [x] 5.3 全量验证
  - seam：三条验证命令
  - 验收：`uv run --with pytest --with aiohttp pytest tests/ -q` 全过；`uv run --with pyright --with aiohttp pyright` 生产代码零错误；`ruff check` 无已知四类偏离之外的新增项
