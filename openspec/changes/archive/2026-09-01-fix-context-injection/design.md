# fix-context-injection 设计

## Context

注入路径现状：`plugin.py` 编排（`_inject_reaction` / `_flush_aggregate` / `_get_stream_id` / `_fetch_bot_display_name`），`reactions.py` 持纯内存态（`InjectionState` 的去重表、聚合表、注入计数）。主程序侧三个关键事实（对 `MaiBot@390637a60` 核实）：

1. 授权层硬校验：manifest 声明的能力列表经 bootstrap 签发令牌，`check_capability` 对无令牌插件一律拒绝；`_ALWAYS_ALLOWED_CAPABILITIES` 仅 `api.replace_dynamic`
2. `chat.get_stream_by_group_id` 只查 `chat_manager.sessions`（内存中已建立的会话，懒创建）
3. `maisaka.context.append` 失败返回 `{"success": False}`，不抛异常

现有 `due()` 是同步「取出即移除」语义，这是双路径 flush 并发安全的基石。

## Goals / Non-Goals

**Goals:**

- 注入功能在真实主程序环境可用（授权、stream、写入三层都通）
- **聚合窗口**到期注入有确定性兜底，不依赖事件流连续性
- flush 失败后**去重窗口**不永久占坑，可恢复
- 配置语义无隐式陷阱（0 值、缓存策略）

**Non-Goals:**

- 不改**跟风**/**反义回应**行为分支（ADR 0002 域）
- 不新增任何配置项（tick 间隔、回滚策略均硬编码）
- guard 不引入 scope 检查（行为契约已在 spec 固化，ADR 0003 补记理由）
- 不做失败重试队列（回滚 dedup 键 + 上限兜底已足够）

## Decisions

### D1. manifest 静态声明三项能力

`capabilities: ["maisaka.context.append", "chat.get_stream_by_group_id", "config.get"]`，名称逐一对照主程序 `capabilities/registry.py` 核实。无替代方案：授权层不提供运行时申请通道，兄弟插件 friend-request-handler 同模式。

### D2. 兜底循环：单一常驻任务，tick 5 秒硬编码

注入开启时经 `_spawn` 启动 `while True: sleep(5); flush(due(now))` 循环；关闭或卸载时取消。`on_config_update` 重建注入态后循环引用新 `InjectionState`（读 `self._injection_state`，非闭包捕获）。

- 备选一：每条聚合一个 `sleep(merge_window)` 延迟任务——需给 `InjectionState` 加单聚合 flush 接口，破坏批量语义，任务数随聚合数增长。弃。
- 备选二：维持纯事件驱动——缺陷 C 保留。弃（已定修复）。
- tick 取 5s：远小于默认 `merge_window_seconds=30` 的量级，注入延迟上限为「窗口 + 5s」，无需可配。

### D3. 事件驱动 flush 保留，双路径靠 `due()` 原子性去重

`due()` 同步执行、无 await 点，asyncio 单线程下两个触发路径串行进入，先取走者 flush、后者见空集。不引入锁。

### D4. flush 失败回滚：聚合记录回应者 QQ，失败时删键

`InjectionAggregate` 增加 `reactor_qqs: list[str]`（与显示名并行记录）。`_flush_aggregate` 失败分支调用新方法 `InjectionState.rollback(aggregate)` 删除该聚合覆盖的全部 dedup 键。后续同组合上报重新走 `record()` → 聚合 → 注入。

- 备选：失败聚合放回队列按窗口重试——需要 requeue 接口 + 重试计数防死循环，复杂度更高。弃。
- 持久失败下的行为：反复聚合又丢弃，`注入上限`不消耗（仅成功计数），warning 日志持续可见。

### D5. 去重窗口 0 值改为「不启用去重」

`record()` 的跳过条件改为 `dedup_ttl_seconds > 0 and not expired`；0 值时永不跳过。`config.py` hint 与 `config.toml` 注释写明语义；CONTEXT.md 术语已更新（阶段一）。

### D6. stream 失败不缓存 / 显示名一次性解析

两个相反方向同源于「按失败是否自愈决定缓存」：

- `_get_stream_id`：查不到直接返回空串，不写入 `_stream_ids`；成功值照旧缓存（session_id 为确定性计算，主程序重启不变）
- `_fetch_bot_display_name`：新增 `_bot_display_name_resolved` 标记，查过一次即定型；`_rebuild_runtime` 重置（配置热更新后 `self_display_name` 可能改变）

### D7. 文档动作

- 新建 ADR 0004：定时 flush 兜底 + 失败回滚机制（满足三标准：移除即回到缺陷、无上下文会疑惑双路径、单任务 vs 每聚合任务的真实取舍）
- ADR 0003 Consequences 追记 guard 不受 scope 约束的理由
- 删除 `docs/superpowers/plans/`（过程文档，1175 行，AGENTS.md 结构清单随之自然对齐）

## Risks / Trade-offs

- [注入延迟上限为聚合窗口 + 5s] → 可接受，ADR 0004 记录为已知行为
- [持久失败下反复聚合丢弃] → `注入上限` 兜底 + warning 持续提醒；不引入重试计数
- [stream 失败群每次上报多一次轻量 RPC] → 表情回应低频，主程序侧为内存查询，可接受
- [manifest 能力名与主程序耦合] → 能力改名属主程序 breaking change，`host_application.max_version` 已提供版本闸门
- [`dedup_ttl_seconds=0` 语义变更为 BREAKING] → 原「永久去重」是隐式危险语义，真实部署默认 3600 不受影响；proposal 已标注
- [fake SDK 无法模拟授权层] → 测试以 manifest 内容断言代替端到端授权验证，授权链路正确性靠主程序源码核对（D1 已做）

## Migration Plan

纯内存态、无数据迁移：部署新版本并重启主程序即生效。回滚 = revert 提交，旧的去重表与聚合表随进程终止消失，无残留。

## Open Questions

（无——全部决策已在阶段一对齐）
