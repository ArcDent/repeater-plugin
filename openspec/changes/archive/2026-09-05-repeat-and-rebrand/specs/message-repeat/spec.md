# Delta Spec: message-repeat

## Purpose

让麦麦参与群聊复读：滚动窗口内同一文本被多人接龙达阈值后，按概率复读原文、发打断文本唱反调、或保持沉默；配套链终结防护与门禁冷却。作用对象是普通文本消息，与表情回应跟风（`reactions.py` 承载的既有能力）并列。

## ADDED Requirements

### Requirement: 复读文本的消息资格

只有**纯文本消息**（消息仅含文本段且 trim 后非空）MUST 参与复读链累积。以下消息 MUST NOT 产出复读文本：含 @ 段、引用段、图片段、表情包段或其他富媒体段的消息；通知消息（`is_notify` 标记）；命令消息；文本长度超过 `repeat.max_text_length` 的消息；麦麦自己（`self_qq`）发出的消息。

#### Scenario: 纯文本消息进入复读链
- **WHEN** 群内一条仅含文本段、trim 后非空的消息到达
- **THEN** 该消息的文本参与复读链累积

#### Scenario: 含图片的消息不参与
- **WHEN** 一条消息同时含文本段与图片段
- **THEN** 该消息不产出复读文本，不参与累积

#### Scenario: 通知消息不参与
- **WHEN** 一条 `is_notify=True` 的通知消息（如戳一戳、撤回）到达
- **THEN** 该消息不产出复读文本

#### Scenario: 麦麦自己的消息不参与
- **WHEN** 发送者 QQ 等于 `self_qq` 的消息到达
- **THEN** 该消息不进入复读链累积

### Requirement: 宽松归一比对

两条消息的文本 MUST 在归一化后比对一致才算同一复读文本：去首尾空白、连续空白折叠为单个、大小写不敏感。**复读发送时 MUST 使用链内最后一条消息的原始文本**，不得使用归一化产物。

#### Scenario: 首尾空白不打散链
- **WHEN** 发送者 A 发 `草`、发送者 B 发 ` 草 `（首尾带空格）
- **THEN** 两条消息归一后一致，计入同一条复读链

#### Scenario: 大小写不敏感
- **WHEN** 发送者 A 发 `HHH`、发送者 B 发 `hhh`
- **THEN** 两条消息计入同一条复读链

#### Scenario: 复读发出原始文本
- **WHEN** 链内最后一条消息原文为 ` 草 `（含首尾空格）且复读出口命中
- **THEN** 麦麦发出的文本为 ` 草 ` 的原始内容，而非归一化后的 `草`

### Requirement: 滚动窗口与计数口径

复读链 MUST 在滚动时间窗口（`repeat.window_seconds`）内累积：统计口径默认仅计**不同发送者**人头（同一发送者的重复发送不增加计数）；`repeat.distinct_senders` 为 false 时 MUST 改为逐条消息计数。计数达到 `repeat.threshold` 时链达标，取得一次决策资格。窗口内未达标的历史消息随窗口滚动失效。

#### Scenario: 不同发送者先后接龙达标
- **WHEN** 窗口内 A、B、C 三个不同发送者先后发出归一后相同的文本，`threshold=3`
- **THEN** 复读链达标，取得一次决策资格

#### Scenario: 同一发送者重复发送不增计数
- **WHEN** `distinct_senders=true`，发送者 A 连发三条相同文本
- **THEN** 链上计数仍为 1，不达标

#### Scenario: 关闭去重后逐条计数
- **WHEN** `distinct_senders=false`，发送者 A 连发三条相同文本，`threshold=3`
- **THEN** 链上计数为 3，达标

#### Scenario: 窗口滚动使早期消息失效
- **WHEN** A 在窗口起点发过目标文本，此后超过 `window_seconds` 无同文本消息，B 再发该文本
- **THEN** A 的消息已随窗口滚动失效，链上仅 B 计 1

### Requirement: 三出口决策与发送

链达标后 MUST 掷骰一次，三出口严格互斥：**复读**（按 `repeat.probability` 命中，发送链内最后一条的原始文本）、**打断**（按 `repeat.interrupt_probability` 命中，从 `repeat.interrupt_texts` 变体池随机取一条发送，MUST NOT 附带复读文本）、**沉默**（两者皆未命中，不发送任何内容）。复读与打断 MUST 经主程序标准发送链（`ctx.send.text`）发出，使消息进入麦麦的聊天上下文；发送 MUST 在决策后延迟 `behavior.delay_min/max` 范围内的随机时长执行。发送失败 MUST 记日志且不重试。

#### Scenario: 复读出口命中
- **WHEN** 链达标且掷骰命中复读出口
- **THEN** 麦麦经标准发送链发出链内最后一条消息的原始文本

#### Scenario: 打断出口命中只发打断文本
- **WHEN** 链达标且掷骰命中打断出口
- **THEN** 麦麦只发出变体池中随机一条打断文本，不附带复读文本

#### Scenario: 双概率皆未命中保持沉默
- **WHEN** 链达标且掷骰未命中复读与打断任一出口
- **THEN** 麦麦不发送任何消息

#### Scenario: 发送前有自然延迟
- **WHEN** 决策出口为复读或打断
- **THEN** 发送在延迟区间内的随机时长后执行，不出现决策即发

### Requirement: 链终结

决策资格一经消耗（掷骰完成，无论出口为何）MUST 即刻链终结：按（群，复读文本）记录终结标记，标记 TTL（`repeat.chain_ttl_seconds`）内同文本的新发送者只续记录、MUST NOT 触发新的决策资格。标记 TTL 过期后，同一文本重新累积的新一轮复读 MUST 可再次取得决策资格。

#### Scenario: 决策后后续接龙不再触发
- **WHEN** 链达标已掷骰，D、E 随后发出同文本
- **THEN** 不产生新的决策资格，麦麦不再出声

#### Scenario: 沉默同样消耗资格
- **WHEN** 链达标掷骰出口为沉默，D 随后发出同文本
- **THEN** 不产生新的决策资格

#### Scenario: 标记过期后新一轮可触发
- **WHEN** 链终结标记 TTL 已过期，新一轮 A、B、C 接龙同文本达标
- **THEN** 新一轮取得决策资格，可再次掷骰

### Requirement: 门禁与冷却

复读功能 MUST 复用现有 `scope.group_whitelist`/`group_blacklist` 群白黑名单（黑名单优先）。`scope.ignore_senders` 名单内发送者的消息 MUST NOT 进入复读链。复读 MUST 使用独立于贴表情的群冷却（`repeat.cooldown_seconds`）：冷却期内链达标不消耗决策资格、不掷骰；冷却结束后同文本新发送者加入链可触发决策。`plugin.enabled` 关闭时复读功能 MUST 整体停用；`repeat.enabled` 关闭时 MUST 仅停用复读（表情回应跟风不受影响）。

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
