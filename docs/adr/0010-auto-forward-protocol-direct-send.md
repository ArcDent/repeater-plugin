---
status: accepted
date: 2026-09-18
---

# 自动转发走协议端直发，且投递前校验目标群可用性

## Context

2026-09-17 的线上日志暴露了一条完整的失败链（目标群 `747839793`，泰拉最高议会驻厦大办事处）：

1. 该群**已不在机器人所在群列表**——SnowLuma 的 `snowluma_identity.db` `groups` 表只有 7 个群，无此群；SnowLuma 日志 9/11–9/17 中该群号零出现。
2. MaiBot 数据库 `chat_sessions` 仍留着该群 2026-06-05 创建、2026-07-15 后再无活动的陈旧会话（NapCat 时代遗留）。
3. 原插件 `GroupStreamRegistry.resolve()` 用 `chat.get_stream_by_group_id` 解析到这个陈旧 stream。
4. Host 侧发送时，`send_service._inherit_platform_io_route_metadata` 从陈旧 stream 的上下文消息继承路由键，`PlatformIOBroker.resolve_drivers` 匹配不到 SnowLuma gateway 的绑定，回落到平台级后备驱动 `legacy.send.qq`。
5. 旧适配器链路已不存在：`[SendService] Platform IO 发送失败: platform=qq driver=legacy.send.qq status=DeliveryStatus.FAILED error=旧链发送失败`。
6. `_build_outbound_session_message` / `_send_to_target_with_message` 把失败吞成 `None`，Host 的 `_build_send_result` 此时不带 `error` 字段，插件侧的 `CapabilityResult.error()` 只能读出**「未知错误」**——真实原因在插件日志里完全不可见。

同一个 job 的其他目标群（8 月 SnowLuma 上线后新建的会话）路由命中 `snowluma_gateway`，投递全部成功——差异只在 stream 的年龄与路由归属。

## Decision

**自动转发**不再走主程序发送链，改协议端直发，并在两条路径上补齐目标群可用性校验：

1. **自动转发**发送调用插件的协议端 HTTP 通道 `send_group_forward_msg`（复用 `protocol` 配置段），只依赖群号，与 Host 的聊天流/Platform IO 路由彻底解耦——陈旧 stream 不再可能把发送引向 legacy 后备驱动。
2. **投递前校验**：两条路径在发送目标群前，用协议端 `get_group_list`（或等价的群可用性来源）确认目标群仍在机器人群列表；不在列表的群跳过该次投递，自动路径记 warning、请求转发路径把具体原因返回给 Planner，不再出现「未知错误」。
3. 协议端直发依赖 `protocol.base_url` 可用：未配置或不可达时自动路径记 warning 并放弃本次转发（不静默、不重试），配置自检在启动日志里显式提示。

## Considered Options

- **继续走 Host `send.forward` + 增加群可用性校验**：保留写库与 `sync_to_maisaka_history` 锚点，但路由仍由陈旧 stream 上下文决定；校验只能覆盖「群不存在」这一类，无法覆盖同一群里仍存在的陈旧 stream 路由。
- **按群号重建会话/会话刷新后再走 Host 发送**：MaiBot 没有对外暴露的「重建 stream / 重绑路由」能力，插件无法可靠地修正 Host 侧的路由绑定。
- **只依赖适配器上报的群列表缓存**：绕开协议端调用，但缓存新鲜度不可控，且失败方式仍不透明。

## Consequences

- **自动转发**的直发消息不进主程序消息库、不回流入站链（ADR 0006 的直发属性），目标群的上下文可见性全靠**转发内容注入**；请求转发链路保持 Host 发送语义不变。
- 目标群可用性校验是一次额外的协议端调用：放在配置加载/更新时的缓存校验最省，但群列表随时会变；运行时逐次投递前校验最准，代价是每次转发多一跳 RPC。实现取「缓存 + 投递前快速校验」的组合，具体在 change design 定。
- 该修复只对两条路径的**投递前置**生效；「已知群不在列表仍被配置为 target」属于部署错误，插件的责任是让它以明确错误出现在日志与 Tool 返回值里，而不是静默变成「未知错误」。
