# 复读出口改协议端直发 + planner 拦截 + 上下文注入

## Why

复读功能线上暴露两个问题：**双重发言**（复读链达标时麦麦的复读与 LLM planner 对触发消息的回复同时发出）与**发送链路受扰**（`ctx.send.text` 走主程序 send_service，路过的其他插件 blocking hook 可改写/取消消息，且发送前置依赖 stream_id 查询成功）。源码实证见 `docs/adr/0006`：主程序不存在消息级「跳过 planner」机制，hook 只能 `continue`/`abort`；`ctx.send.text` 默认不进内存上下文，麦麦不知道自己复读过什么。

## What Changes

- **BREAKING**：复读/打断出口命中时，`repeat_detector` handler（`chat.receive.before_process`）从 observe 改 **blocking**，对触发消息返回 abort——触发消息不再写库、不进主程序上下文、不被 planner 决策。沉默出口照常放行。
- 复读/打断发送从 `ctx.send.text` 改**协议端直调 `send_group_msg`**（绕过主程序发送链，只需群号）。发送失败记 warning 不重试，冷却与链终结不受影响。
- 新增**触发消息注入**：abort 后无条件把触发消息原文（谁发了什么）写入麦麦上下文（`ctx.maisaka.context.append`），补偿拦截造成的上下文缺失。
- 新增**行为文本注入**：直发成功后把复读/打断文本写入麦麦上下文；直发失败不注入（上下文与事实一致）。
- 拦截与两路注入跟随 `repeat.enabled`，与表情回应注入的 `context.enabled` 解耦（两域并列不混装）。
- 复读注入不复用表情回应的 `InjectionState`（去重/聚合/回滚是外部事件流聚合的需求；复读注入是即时一次性记录）。
- `_manifest.json` 移除 `send.text` 能力声明（通路已换）。
- **BREAKING**：触发消息经 abort 后不再进主程序消息库（重启后上下文恢复缺这条），由注入补偿当前会话内的可见性。

## Capabilities

### New Capabilities

（无——全部落在既有能力的 delta 上）

### Modified Capabilities

- `message-repeat`: 「三出口决策与发送」需求的发送通路与 planner 关系改写（协议端直发替代 `ctx.send.text`）；新增 planner 拦截、触发消息注入、行为文本注入三项需求；沉默出口语义不变。

## Impact

- `plugin.py`：`observe_repeat`/`_process_repeat_message` 改 blocking 并返回 abort；`_send_repeat_decision` 改协议端直调；新增触发消息注入与行为文本注入路径；`guard_reaction_notify` 不动。
- `_manifest.json`：移除 `send.text` 声明（`maisaka.context.append` 等已有声明继续覆盖注入路径）。
- `repeats.py`：决策层出口判定不变，预期小改或零改（blocking 化后同步原子段保持）。
- `tests/`：复读链相关用例改写（blocking 返回值、直调 mock、注入断言）；「断言不触发」用例的概率约定不变。
- 文档：`docs/adr/0006`（已写）取代 ADR 0005 的模式与发送决策；`CONTEXT.md`（已增补术语）；README 增部署提示（协议端勿开 self-message 上报，否则直发消息以 user 身份回流主程序）。
- 不新增配置项（开关语义复用 `repeat.enabled`；延迟复用 `behavior.delay_min/max`）。
