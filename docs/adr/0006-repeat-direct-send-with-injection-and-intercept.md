---
status: accepted
date: 2026-09-05
---

# 复读/打断出口改协议端直发 + 上下文注入 + blocking 拦截触发消息（取代 ADR 0005 的发送与模式决策）

ADR 0005 的两个核心决策被本 ADR 取代：(a) 复读检测 handler 从 observe 改 **blocking**；(b) 复读/打断发送从 `ctx.send.text` 改**协议端直调 `send_group_msg`**。0005 的其余内容（hook 选点、形态容错口径、检测层设计）继续有效。

## Context

复读功能在线上暴露两个问题：**双重发言**（复读链达标时，麦麦的复读与 LLM planner 对同一条触发消息的回复同时发出）与**发送链路受扰**（`ctx.send.text` 走主程序 send_service，路过的其他插件 blocking hook 可改写/取消，且发送前置依赖 stream_id 查询成功）。源码实证（`REFERENCE/MaiBot` @ repeat-fix 调查）：

- 主程序不存在消息级「跳过 planner」机制：hook 动作词表只有 `continue`/`abort`（`plugin_runtime/host/hook_dispatcher.py:535-549`）。`abort` = 整条消息从主链消失（不写库、不进上下文、不进 planner）。
- `ctx.send.text` 两参调用默认 `sync_to_maisaka_history=False`，不进内存上下文——麦麦当场不知道自己复读过什么。
- `ctx.maisaka.context.append` 直接写 planner 历史（`runtime._chat_history`），不写库、不触发 turn——与表情回应注入（ADR 0003/0004）同一通路。
- SnowLuma `send_group_msg` 响应信封与 `set_msg_emoji_like` 一致（`status`/`retcode`/`data`），现有 `protocol_call_failed` 判定可复用；群发自发消息默认不上报（`reportSelfMessage=false`，`http-post-adapter.ts` + `event-filter.ts`），直发消息不会以 user 身份回流主程序入站链。

## Decision

复读链达标、掷骰命中**复读或打断出口**时：

1. **blocking 拦截**：现 `repeat_detector` handler 从 observe 改 blocking（`order="late"` 不变，仍在 `guard_reaction_notify` 之后）。门禁 → `record`（链终结）→ `decide_repeat` 的同步原子段结构不变；命中复读/打断出口即对触发消息返回 `{"action": "abort"}`——planner 永远看不到这条消息，杜绝双重发言。沉默出口照常 `continue`（消息正常进主链，麦麦可自行决定如何回应）。
2. **触发消息注入**：abort 造成触发消息不写库、不进上下文，由后台任务用 `ctx.maisaka.context.append` 注入触发消息原文（谁发了什么）补偿——**无条件执行**，与直发成败解耦。
3. **协议端直发**：延迟（沿用 `behavior.delay_min/max`）后直调 `send_group_msg`（`{group_id, message: [{"type":"text","data":{"text": …}}]}`），绕过主程序发送链。直发只需群号，不再依赖 stream_id。
4. **行为文本注入**：直发成功后，把复读/打断文本注入上下文（麦麦知道自己发了什么）。直发失败时该注入不做，记 warning 不重试——上下文与事实一致（有人发了消息、麦麦没动作）。

## Considered Options

- **保留 observe + 会话级 `frequency.set_adjust` 压制 planner**：粒度是整群会话而非单条消息，副作用是压制窗口内麦麦对一切消息静默，误伤面不可控。
- **`ctx.send.text` 传 `sync_to_maisaka_history=True`**：只解决「麦麦知道」不解决双重发言与发送链受扰；且该同步要求 heartflow runtime 已存在，否则静默跳过，行为不确定。
- **blocking abort 一刀切（沉默出口也 abort）**：沉默意味着麦麦保留对触发消息的正常反应权，abort 会把消息从上下文里抹掉且无补偿理由；拒绝。
- **保留 observe 另挂 blocking 拦截 handler**：两个 handler 同 hook 同点，拦截条件依赖检测状态，跨 handler 传递决策（实例属性）引入时序耦合；单 handler blocking 化更简单，且 blocking 同步执行反而强化了同步原子段（原设计里 observe 任务并发双触发的担忧，blocking 串行下天然消失）。

## Consequences

- **复读注入不复用表情回应的 `InjectionState`**：去重窗口/聚合窗口/回滚是外部事件流聚合的需求；复读注入是自身行为的即时一次性记录（触发消息一条 + 行为文本一条），无聚合无重试。
- 发送失败语义沿用：记 warning 不重试不熔断；冷却与链终结在出口命中时已写入，与发送成败解耦。
- 触发消息注入依赖 stream_id（`maisaka.context.append` 需要）；取不到 stream 时注入降级记 warning，**拦截照常生效**（宁可上下文缺一条，不可回到双重发言）。
- manifest 移除 `send.text` 能力声明（不再调用）；`maisaka.context.append` / `chat.get_stream_by_group_id` / `config.get` 声明已存在，无需新增。
- blocking 化后主链同步等待本 handler 返回：handler 全程纯内存判定（门禁/record/decide），无 IO，微秒级返回；真正的 IO（注入、直发）全部 spawn 到后台任务。
- 部署前提：协议端 webhook 不上报 self message（SnowLuma 默认 `reportSelfMessage=false`）。若部署者打开该开关，直发的复读消息会以普通 user 消息身份回流主程序入站链（主程序入站链无 is_bot_self 过滤），形成「麦麦看到自己复读过、但以 user 身份」的形态污染——README 需提示不要开启。
- `_send_repeat_decision` 的 `ctx.send.text` 通路删除；AGENTS.md 复读域规则随实现同步改写。
