# message-repeat Delta

## MODIFIED Requirements

### Requirement: 三出口决策与发送

链达标后 MUST 掷骰一次，三出口严格互斥：**复读**（按 `repeat.probability` 命中，发送链内最后一条的原始文本）、**打断**（按 `repeat.interrupt_probability` 命中，从 `repeat.interrupt_texts` 变体池随机取一条发送，MUST NOT 附带复读文本）、**沉默**（两者皆未命中，不发送任何内容）。复读与打断 MUST 经**协议端直发**（`send_group_msg`）发出，MUST NOT 走主程序标准发送链；发送 MUST 在决策后延迟 `behavior.delay_min/max` 范围内的随机时长执行。发送失败 MUST 记日志且不重试。

#### Scenario: 复读出口命中
- **WHEN** 链达标且掷骰命中复读出口
- **THEN** 麦麦经协议端直发发出链内最后一条消息的原始文本

#### Scenario: 打断出口命中只发打断文本
- **WHEN** 链达标且掷骰命中打断出口
- **THEN** 麦麦只发出变体池中随机一条打断文本，不附带复读文本

#### Scenario: 双概率皆未命中保持沉默
- **WHEN** 链达标且掷骰未命中复读与打断任一出口
- **THEN** 麦麦不发送任何消息

#### Scenario: 发送前有自然延迟
- **WHEN** 决策出口为复读或打断
- **THEN** 发送在延迟区间内的随机时长后执行，不出现决策即发

#### Scenario: 发送失败不重试
- **WHEN** 协议端直发失败（不可达或返回失败）
- **THEN** 记 warning 日志，不重试；已进入的冷却与链终结不受影响

### Requirement: 门禁与冷却

复读功能 MUST 复用现有 `scope.group_whitelist`/`group_blacklist` 群白黑名单（黑名单优先）。`scope.ignore_senders` 名单内发送者的消息 MUST NOT 进入复读链。复读 MUST 使用独立于贴表情的群冷却（`repeat.cooldown_seconds`）：冷却期内链达标不消耗决策资格、不掷骰；冷却结束后同文本新发送者加入链可触发决策。`plugin.enabled` 关闭时复读功能 MUST 整体停用；`repeat.enabled` 关闭时 MUST 仅停用复读（表情回应跟风不受影响），复读侧的 **planner 拦截**与两路**复读注入**随 `repeat.enabled` 一并停用，且 MUST NOT 受表情回应域 `context.enabled` 开关约束。

#### Scenario: 黑名单群不触发
- **WHEN** 黑名单群内出现达标复读链
- **THEN** 不取得决策资格，麦麦无动作

#### Scenario: 忽略名单内发送者不进链
- **WHEN** `ignore_senders` 内的其他 bot 连发相同文本
- **THEN** 其消息不进入复读链累积

#### Scenario: 独立冷却不与贴表情互相牵扯
- **WHEN** 麦麦刚完成一次复读（进入复读冷却），随后有表情回应上报命中跟风概率
- **THEN** 贴表情不受复读冷却影响，按表情自身冷却判定

#### Scenario: 冷却期内达标不掷骰
- **WHEN** 复读冷却期内链达标，冷却结束后同文本又来一个新发送者
- **THEN** 冷却期内不消耗决策资格；冷却结束后链因新发送者仍可取得决策资格并掷骰

#### Scenario: 复读独立开关关闭不影响表情跟风
- **WHEN** `repeat.enabled=false` 且表情回应上报命中跟风概率
- **THEN** 麦麦照常贴表情，不产生任何复读行为

#### Scenario: 复读开关关闭不拦截不注入
- **WHEN** `repeat.enabled=false`，一条本可触发复读出口的消息到达主程序消息链
- **THEN** 该消息不被拦截，也不产生任何复读注入，即使 `context.enabled` 为 true

## ADDED Requirements

### Requirement: planner 拦截

复读/打断出口命中时，插件 MUST 在主程序消息链上对**触发消息**（使复读链最终达标的那条消息）执行拦截：该消息 MUST NOT 写入主程序消息库、MUST NOT 进入主程序聊天上下文、MUST NOT 被 planner 决策。沉默出口 MUST 放行触发消息（麦麦保留对触发消息的正常反应权）。拦截动作 MUST 在同步决策段内完成，不引入异步等待点。

#### Scenario: 触发消息不被 planner 决策
- **WHEN** 复读出口命中，触发消息到达主程序消息链
- **THEN** 触发消息被拦截，planner 不会基于该消息发起回复（不出现复读与 LLM 回复双重发言）

#### Scenario: 打断出口同样拦截
- **WHEN** 打断出口命中，触发消息到达主程序消息链
- **THEN** 触发消息同样被拦截

#### Scenario: 沉默出口放行
- **WHEN** 沉默出口命中（或链未达标），触发消息到达主程序消息链
- **THEN** 触发消息正常进入主程序链路，planner 照常决策

### Requirement: 触发消息注入

planner 拦截生效的每一次复读/打断出口，插件 MUST 把**触发消息**的原文（发送者与文本内容）作为上下文消息写入麦麦聊天历史，补偿拦截造成的上下文缺失。该注入 MUST 无条件执行，与协议端直发成败解耦。注入失败（聊天流查不到或主程序返回失败）MUST 记日志且不重试，拦截照常生效。注入内容 MUST 呈现发送者与消息文本，使麦麦能识别「谁发了什么」。

#### Scenario: 拦截后上下文仍有触发消息
- **WHEN** 复读出口命中，触发消息被拦截
- **THEN** 麦麦聊天历史中存在一条呈现该消息发送者与文本的上下文消息

#### Scenario: 直发失败时触发消息注入仍执行
- **WHEN** 复读出口命中，触发消息注入已完成，随后协议端直发失败
- **THEN** 麦麦聊天历史中仍保留触发消息的注入，上下文反映「有人发了消息、麦麦没有动作」的事实

#### Scenario: 注入失败不回滚拦截
- **WHEN** 触发消息注入因聊天流查不到而失败
- **THEN** 记 warning 日志，拦截与直发照常执行，不重试注入

### Requirement: 行为文本注入

协议端直发成功后，插件 MUST 把发出的复读文本或打断文本作为上下文消息写入麦麦聊天历史，使麦麦知道自己发出了什么。直发失败时 MUST NOT 注入行为文本（上下文与事实一致：麦麦没有发出任何内容）。注入失败 MUST 记日志且不重试，不影响已完成的状态。

#### Scenario: 复读成功后麦麦知道自己复读过
- **WHEN** 复读出口命中且协议端直发成功
- **THEN** 麦麦聊天历史中存在一条呈现复读文本的上下文消息，后续 planner 唤醒时可见

#### Scenario: 直发失败不注入行为文本
- **WHEN** 复读出口命中但协议端直发失败
- **THEN** 麦麦聊天历史中不存在复读文本的注入消息

#### Scenario: 注入文案区分行为类型
- **WHEN** 行为文本注入发生
- **THEN** 注入消息可区分本次行为是复读还是打断
