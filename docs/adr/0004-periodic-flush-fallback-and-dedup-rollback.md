---
status: accepted
---

# 表情回应上下文注入：定时 flush 兜底与失败回滚

ADR 0003 落地的注入链路是纯事件驱动的：聚合窗口（`merge_window_seconds`）到期并不会让聚合体自己注入，而是等**下一条上报**到来时在 `_inject_reaction` 尾部顺带 flush。这在事件流连续时工作正常，但有两个结构性缺陷：

1. **静默期延迟无上界**：表情回应本身低频，群冷清时最后一条聚合可能长期等不到「下一条上报」——最坏情况下直到进程重启都不会注入。
2. **失败永久占坑**：flush 失败（stream 取不到、append 调用异常、`success != True`）时，去重键已被 `record()` 写入、聚合已被 `due()` 取出移除——这批表情回应既没进上下文，去重窗口内也不会再有第二次机会。

决定两个缺陷一并修：

- **定时 flush 兜底**：注入开启时经 `_spawn` 启动单一常驻循环，每 `sleep(5)` 秒后 flush 全部到期聚合；tick 硬编码为 `_INJECTION_FLUSH_TICK_SECONDS = 5`，不加配置项——远小于默认 `merge_window_seconds=30` 的量级，可配性没有收益。每轮重读 `self._injection_state`，配置热更新后自动引用新聚合表且不叠加重复循环；注入关闭时循环在下一个 tick 自然退出；`on_unload` 由 `_tasks` 统一取消。循环内单轮失败只打 warning，不杀循环。
- **失败回滚去重键**：`InjectionAggregate` 记录聚合覆盖的全部回应者 QQ；`_flush_aggregate` 三个失败分支统一调用 `InjectionState.rollback(aggregate)` 删除这些去重键，后续同组合上报重新走 `record()` → 聚合 → 注入；`mark_injected` 仅在成功路径调用，失败不消耗 `max_reactions_per_message` 配额。

事件驱动 flush 保留：`_inject_reaction` 尾部与循环共用 `_flush_due_aggregates`。双路径不双发的保证是 `due()` 同步执行、取出即移除、无 await 点——asyncio 单线程下两个触发路径串行进入，先取走者 flush、后者见空集，不需要锁。

## Considered Options

**兜底机制：单一常驻循环 vs 每聚合一个延迟任务**

- 每条聚合一个 `sleep(merge_window)` 延迟任务：到期即注入、零额外延迟，但要给 `InjectionState` 加单聚合 flush 接口（破坏 `due()` 的批量语义）、任务数随聚合数增长、还要处理热更新时的任务回收。弃。
- 维持纯事件驱动：静默期缺陷保留。弃（本次变更的目标就是修掉它）。
- 单一常驻循环（选定）：一个任务、一个入口、复用事件路径的 `_flush_due_aggregates`；代价是注入延迟上限为聚合窗口 + 5 秒。

**失败恢复：回滚去重键 vs 重试队列**

- 失败聚合放回队列按窗口重试：需要 requeue 接口、重试计数防死循环、失败聚合与正常聚合的状态区分，复杂度显著更高；且典型失败（stream 未建立、主程序侧拒写）多为持续性，定时重试大概率继续失败。弃。
- 回滚去重键（选定）：失败后回到「没见过这批上报」的状态，重试时机交给事件流与兜底循环——冷群等不到新上报时，5 秒 tick 也会用新聚合重试；无新增状态机。

## Consequences

- 注入延迟上限为聚合窗口 + 5 秒（tick 硬编码，已知行为，可接受）。
- 持久失败下反复聚合又丢弃：同一组合每来一条新上报就重新聚合、重新尝试、重新失败。由注入上限与 warning 日志兜底——`mark_injected` 仅成功路径计数，失败不消耗配额；warning 持续可见即「该群注入持续失败」的运维信号，不引入重试计数。
- 循环每轮重读 `self._injection_state`：热更新重建注入态后循环自动切换到新状态；代价是循环体内不得缓存状态引用。
- 常驻循环必须存活：`_flush_due_aggregates` 单轮异常由循环侧宽捕获兜住并记日志（与 webhook 回调同属 BLE001 有意偏离），不允许一个失败的 tick 终止兜底机制本身。
