# fix-context-injection 任务清单

## 1. 纯逻辑层（reactions.py + 单测）

- [x] 1.1 `InjectionAggregate` 增加 `reactor_qqs` 字段并在 `record()` 的 new/merged 分支同步记录
  - 验收：new 后 `reactor_qqs == [reactor_qq]`；merged 后包含两个不同 QQ；同一 QQ 重复上报不重复追加；`tests/test_reactions.py` 新增对应用例通过
- [x] 1.2 `InjectionState` 新增 `rollback(aggregate)`：删除该聚合覆盖的全部去重键
  - 验收：`record()` 返回 "new" → `due()` 取出 → `rollback(agg)` 后，同（回应者，消息，表情）再 `record()` 返回 "new" 而非 "skip"；对应用例通过
- [x] 1.3 `record()` 跳过条件改为 `dedup_ttl_seconds > 0` 才启用去重（0 = 不启用）
  - 验收：`dedup_ttl_seconds=0` 时同键两次 `record()` 均不返回 "skip"；默认 TTL 下原有去重行为不变（既有用例不回归）；对应用例通过

## 2. 编排层（plugin.py + 测试）

- [x] 2.1 `_get_stream_id` 失败不缓存空串，成功照旧缓存
  - 验收：stream 查不到时 `_stream_ids` 不写入该群；同群新上报（stream 已可用）能重新查询并产生注入；成功缓存路径的既有行为不变；对应用例通过
- [x] 2.2 新增 `_bot_display_name_resolved` 一次性解析标记，`_rebuild_runtime` 重置
  - 验收：nickname 为空时连续两次注入只触发一次 `config.get`（FakeConfig 加调用计数）；`_rebuild_runtime` 后标记重置、重新解析；对应用例通过
- [x] 2.3 新增常驻 flush 循环（tick 5 秒，`_spawn` 持有，注入开启启动、关闭/卸载取消，每轮读当前 `self._injection_state`）
  - 验收：注入开启后 `_tasks` 中存在循环任务；`on_unload` 后任务被取消；单轮动作提取为方法（如 `_flush_due_aggregates`），可直接单测；对应用例通过
- [x] 2.4 `_flush_aggregate` 失败分支（stream 取不到或 append 失败）调用 `rollback`
  - 验收：`fail_next=True` 注入失败后，同回应者同组合再上报能重新聚合并注入成功；失败不消耗 `max_reactions_per_message` 配额（`mark_injected` 仅成功路径调用）；对应用例通过
- [x] 2.5 静默期兜底 flush 与双路径不双发测试
  - 验收：聚合窗口未到期 → 无注入；越过窗口后不产生新上报、直接调单轮 flush 方法 → 恰好一条注入；事件驱动已 flush 后再跑单轮方法 → 不产生第二条；对应用例通过
- [x] 2.6 guard 拦截不受 scope 约束的显式测试（spec 场景固化）
  - 验收：群黑名单含目标群时，`guard_reaction_notify` 对该群的表情回应通知消息仍返回 `abort`；对应用例通过

## 3. 声明与配置

- [x] 3.1 `_manifest.json` 补 `capabilities: ["maisaka.context.append", "chat.get_stream_by_group_id", "config.get"]` 并加 manifest 内容断言测试
  - 验收：读取 `_manifest.json` 断言三项能力均在 `capabilities` 数组中；`openspec` 工件与 manifest 一致；对应用例通过
- [x] 3.2 `config.py` 的 `dedup_ttl_seconds` hint 与 `config.toml` 注释写明 0 = 不启用去重
  - 验收：hint 与 toml 注释均含 0 值语义说明；`config.toml` 默认值与 `config.py` default 仍一致（3600）；`tests/test_config.py` 既有断言不回归

## 4. 文档

- [x] 4.1 新建 ADR 0004：定时 flush 兜底 + 失败回滚机制（决策、备选、风险，状态 accepted）
  - 验收：`docs/adr/0004-*.md` 存在且引用 ADR 0003；涵盖「单一常驻循环 vs 每聚合任务」与「回滚 dedup vs 重试队列」两组取舍；AGENTS.md 的 adr 清单补 0004 条目
- [x] 4.2 ADR 0003 Consequences 追记 guard 不受 scope 约束的 rationale
  - 验收：0003 的 Consequences 含「拦截范围与 scope 是两个维度」的说明段落；其余内容未被改动
- [x] 4.3 删除 `docs/superpowers/plans/`（1175 行过程文档）
  - 验收：`docs/` 下仅剩 `adr/` 与 `emoji-id-reference.md`；git 状态显示该目录删除；AGENTS.md 结构清单无需改动即与实际目录一致

## 5. 验证

- [x] 5.1 `uv run --with pytest --with aiohttp pytest tests/ -q` 全部通过（含既有 165 项不回归）
- [x] 5.2 `uv run --with pyright --with aiohttp pyright` 生产代码零错误
- [x] 5.3 `openspec validate fix-context-injection --strict` 通过
- [x] 5.4 `ruff check` 不引入新告警（BLE001/N999/SIM103/SIM117 四类既有偏离除外）
