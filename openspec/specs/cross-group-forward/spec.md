# cross-group-forward Specification

## Purpose

跨群转发能力的行为契约：**源群**里出现**合并转发**后，麦麦可以按**转发权重**对每个**目标群**独立掷骰并**自动转发**，也可以响应群友的对话请求执行**请求转发**；两条路径共用**已投递状态**，转发成功后向目标群注入转发内容、向源群注入行为留痕，并可由自动路径触发目标群 Planner 针对该转发发言。与复读、表情回应两个行为域并列。

## Requirements

### Requirement: 转发源消息的资格

只有**源群**白名单（`forward.source_groups`）内的 QQ 群聊消息 MUST 参与转发域判定。参与判定的消息 MUST 同时满足：平台为 QQ、含至少一个**有效转发节点**（节点内容非空）的**合并转发**、发送者不是麦麦自己（`self_qq`）。不满足条件的消息 MUST NOT 触发**自动转发**掷骰，也 MUST NOT 触发**请求转发**的源群 Planner 提示。转发域**不拦截**源消息：源消息 MUST 照常进入主程序处理链与麦麦上下文。

#### Scenario: 白名单源群的合并转发参与判定
- **WHEN** `forward.source_groups` 包含群 G，群 G 内一名外部用户发出一条含有效转发节点的合并转发
- **THEN** 该消息参与转发域判定，且不受任何拦截、照常进入主程序处理链

#### Scenario: 非白名单群不参与
- **WHEN** 一条合并转发出现在不在 `forward.source_groups` 中的群
- **THEN** 不掷骰、不触发源群 Planner 提示

#### Scenario: 无有效节点不参与
- **WHEN** 一条消息的转发段存在但所有节点内容为空（适配器展开失败）
- **THEN** 该消息不参与转发域判定

#### Scenario: 麦麦自己的消息不参与
- **WHEN** 发送者 QQ 等于 `self_qq` 的合并转发出现
- **THEN** 该消息不参与转发域判定

### Requirement: 自动转发的目标群独立掷骰

**自动转发**MUST 对 `forward.target_groups` 中的每个**目标群**独立掷骰一次：按 `forward.weight_send` / `forward.weight_silence` 归一化后的比例选中「转发」或「沉默」；单个目标群的两个权重全为 0 时 MUST 视为沉默且不掷骰。权重 MUST 按非负数处理（负值与非法值按 0 计），权重之间只有比例意义，单个权重的数值本身 MUST NOT 被当作概率。与**源群**相同的目标群项 MUST 被跳过。同一**转发源消息**在不同目标群上的掷骰结果 MUST 互相独立（可能转发到 A 群而不转发到 B 群）。

#### Scenario: 命中转发
- **WHEN** 某目标的权重为 `weight_send=100 / weight_silence=0`，掷骰发生
- **THEN** 该目标群命中转发出口

#### Scenario: 权重全零视为沉默
- **WHEN** 某目标群的 `weight_send=0` 且 `weight_silence=0`
- **THEN** 该目标群视为沉默且不掷骰

#### Scenario: 目标群结果互相独立
- **WHEN** 三个目标群的权重相同，对同一条转发源消息掷骰
- **THEN** 各目标群的掷骰结果各自独立，允许部分命中、部分沉默

#### Scenario: 源群自身被跳过
- **WHEN** `forward.target_groups` 中出现了某个源群
- **THEN** 该群不参与本次自动转发的掷骰与投递

### Requirement: 自动转发的防重与冷却

**自动转发**MUST 有三层防护：同一**转发源消息**（按消息 ID）在 `forward.dedup_ttl_seconds` 内 MUST 只参与一次掷骰；**源群**冷却期内（`forward.cooldown_seconds`）新到达的合并转发 MUST NOT 触发掷骰；**目标群**冷却期内（`forward.target_cooldown_seconds`）该目标群 MUST NOT 参与掷骰。冷却 MUST 在掷骰命中「转发」出口时写入（无论投递成败），沉默出口 MUST NOT 写入冷却。源群冷却 MUST 在至少一个目标群命中转发时写入。

#### Scenario: 同一消息不重复掷骰
- **WHEN** 同一转发源消息在 `dedup_ttl_seconds` 内被重复上报
- **THEN** 该消息不再参与掷骰

#### Scenario: 源群冷却压制新转发
- **WHEN** 源群在此前的冷却期内已有目标群命中转发，冷却尚未过期，该群又收到一条新的合并转发
- **THEN** 新消息不触发掷骰

#### Scenario: 目标群冷却只压制该群
- **WHEN** 目标群 A 处于冷却期，另一目标群 B 不在冷却期，出现新的转发源消息
- **THEN** A 不参与掷骰，B 正常参与

#### Scenario: 沉默不写冷却
- **WHEN** 所有目标群的掷骰结果均为沉默
- **THEN** 不写入任何冷却，后续消息仍可正常参与掷骰

### Requirement: 自动转发的投递

自动转发命中后 MUST 经**协议端直发**（`send_group_forward_msg`）投递到目标群，MUST NOT 走主程序标准发送链。投递 MUST 在掷骰命中后延迟 `forward.delay_min_seconds` / `forward.delay_max_seconds` 范围内的随机时长执行。各目标群 MUST 独立投递：单个目标群失败、被跳过或较慢 MUST NOT 阻塞其他目标群的投递；任务启动顺序 MUST 与 `target_groups` 列表一致，但物理发送先后不要求严格串行。投递前 MUST 校验目标群仍在机器人群列表中；不在列表的目标群 MUST 被跳过并记录含群号与原因的 warning。单次投递失败 MUST 记 warning、MUST NOT 重试、MUST NOT 熔断后续目标群与后续消息。投递内容 MUST 由**转发源消息**中的**转发节点**重建。

#### Scenario: 命中后直发目标群
- **WHEN** 某目标群命中转发出口
- **THEN** 插件在配置的延迟区间内通过协议端发送该合并转发，不走主程序发送链

#### Scenario: 目标群不可用被跳过
- **WHEN** 某目标群已不在机器人群列表
- **THEN** 该目标群被跳过，日志给出群号与明确原因，且该消息的其他目标群不受影响

#### Scenario: 投递失败不重试不熔断
- **WHEN** 某目标群的协议端发送返回失败
- **THEN** 记 warning 后继续处理下一个目标群，且不对该目标群重试

#### Scenario: 单群较慢不阻塞其他群
- **WHEN** 同一条转发源消息命中目标群 A 与 B，A 的随机延迟明显长于 B
- **THEN** B 正常完成投递，且不等待 A 完成

### Requirement: 转发内容注入

**自动转发**投递成功后 MUST 把**合并转发**的可读文本（含来源群与节点内容摘要）注入**目标群**麦麦聊天历史，`source_kind` 为 `forward`。注入 MUST 在触发该目标群 Planner（**强制发言**）之前完成。投递失败的目标群 MUST NOT 产生注入。注入失败 MUST 记 warning、MUST NOT 重试。

#### Scenario: 投递成功后注入
- **WHEN** 某目标群的自动转发投递成功
- **THEN** 该目标群的聊天历史中出现一条 `source_kind=forward` 的转发内容注入

#### Scenario: 投递失败不注入
- **WHEN** 某目标群的自动转发投递失败
- **THEN** 该目标群不产生任何转发内容注入

#### Scenario: 注入先于 Planner 触发
- **WHEN** 投递成功且该目标群需要触发强制发言
- **THEN** 转发内容注入先写入，Planner 触发后能读到该注入

### Requirement: 转发留痕注入

**自动转发**投递成功后 MUST 把「麦麦把这条转发分享到了哪些目标群」注入**源群**麦麦聊天历史，`source_kind` 为 `forward_action`。留痕内容 MUST 反映实际投递成功的目标群集合。注入失败 MUST 记 warning、MUST NOT 重试、MUST NOT 回滚已完成的投递。

#### Scenario: 成功后源群留痕
- **WHEN** 一条自动转发对目标群 A 投递成功、对目标群 B 投递失败
- **THEN** 源群出现一条只列出 A 的 `source_kind=forward_action` 留痕注入

#### Scenario: 全部失败不留痕
- **WHEN** 一条自动转发的所有目标群投递均失败
- **THEN** 源群不产生留痕注入

### Requirement: 强制发言

**自动转发**投递成功后 MUST 对该目标群触发一次 Planner 主动任务，意图 MUST 要求针对这条转发说一句自然的话（软约束：Planner 仍可能沉默，插件 MUST NOT 检测或补发）。**请求转发**路径 MUST NOT 应用强制发言，其目标群 Planner 保持「自主决定、可沉默」的原有语义。

#### Scenario: 自动转发成功后触发发言
- **WHEN** 自动转发在某目标群投递成功
- **THEN** 该群收到一次要求针对该转发发言的 Planner 主动任务

#### Scenario: 请求转发不强制发言
- **WHEN** 一次请求转发投递成功
- **THEN** 目标群 Planner 按原语义自主决定是否发言，不受强制发言约束

### Requirement: 请求转发链路

`forward.request_enabled` 开启时，**源群**收到**合并转发** MUST 触发一次本群 Planner 作为请求转发路径的入口提示（入口提示 MUST 随 `request_enabled` 固化开启，MUST NOT 有独立的入口配置开关）。麦麦通过转发 Tool 发起的**请求转发** MUST 依次满足：仅在 QQ 群聊生效；投递前校验**查看资格**——已完整查看该转发内容，或查看失败（消息在等待窗口内不可见或内容取不到）时直接使用摘要降级，MUST NOT 因查看失败拒绝投递或要求重试；只信任一次性会话授权、MUST NOT 信任模型提供的平台/群号/聊天流字段；按 `target_groups` 顺序投递并等待真实结果；投递前校验目标群可用性，不可用目标群 MUST 把明确原因返回给 Planner。该路径 MUST NOT 掷骰，MUST NOT 受**转发权重**影响。

#### Scenario: 源群触发本群 Planner
- **WHEN** `request_enabled=true` 且源群收到一条合并转发
- **THEN** 插件为该源群安排一次 Planner 触发

#### Scenario: 入口提示随请求路径开关固化开启
- **WHEN** 部署未配置任何入口提示相关字段，`request_enabled=true`
- **THEN** 源群收到合并转发时入口提示照常触发，行为等同于历史默认开启状态

#### Scenario: 未查看内容不投递
- **WHEN** Planner 请求转发一条查看失败（消息在等待窗口内不可见或内容取不到）的合并转发
- **THEN** 请求不因查看失败被拒绝，插件改用摘要内容降级投递

#### Scenario: 目标群不可用返回明确原因
- **WHEN** 请求转发的某个目标群已不在机器人群列表
- **THEN** Tool 结果包含该群不可用的明确原因，而不是「未知错误」

### Requirement: 已投递状态的两路共用

**自动转发**的某目标群投递成功后 MUST 登记该**转发源消息**的**已投递状态**；**请求转发**在**已投递状态**已登记时 MUST 拒绝重复投递并返回已转发过的说明。**自动转发**掷骰为沉默时 MUST NOT 登记，此后**请求转发**仍可正常执行。

#### Scenario: 自动转发后拒绝对话重复
- **WHEN** 一条转发源消息已被自动转发投递到某目标群，随后 Planner 请求把同一消息转发到同一目标群
- **THEN** 请求被拒绝并返回已转发过的说明

#### Scenario: 沉默后对话请求仍可执行
- **WHEN** 一条转发源消息的自动转发掷骰全部命中沉默，随后 Planner 请求转发该消息
- **THEN** 请求正常执行

### Requirement: 转发域开关与配置

转发域 MUST 由 `forward.enabled` 独立开关控制，整域（自动转发、请求转发、源群触发、两类注入、强制发言）MUST 跟随该开关；插件总开关 `plugin.enabled` 为 false 时转发域 MUST 一并停用。转发域 MUST NOT 复用表情回应域的 `behavior.delay_min/max` 延迟字段，也 MUST NOT 复用 `scope` 白黑名单作为源群过滤。协议端 HTTP 通道未配置时，自动转发 MUST 记 warning 并放弃投递（不静默失败），启动自检 MUST 提示该状态。

#### Scenario: 域开关独立生效
- **WHEN** `forward.enabled=false` 且 `plugin.enabled=true`
- **THEN** 转发域不掷骰、不触发源群 Planner、不产生注入

#### Scenario: 协议端未配置时的可见性
- **WHEN** `forward.enabled=true` 但 `protocol.base_url` 为空
- **THEN** 启动自检给出提示，自动转发命中时记 warning 并放弃投递

### Requirement: 自动转发的富摘要占位观测

**自动转发**的编排任务在**摘要就绪**时 MUST 记录一条 info 日志，含本次**富摘要**中媒体占位（`[图片]` / `[表情]` / `[嵌套转发]`）的出现计数。该观测 MUST NOT 改写注入文本或 intent 材料（占位按主程序原文保留），MUST NOT 新增配置项，MUST NOT 影响掷骰、冷却、投递与注入的任何行为分支。

#### Scenario: 就绪后记录占位计数
- **WHEN** 编排任务轮询到**摘要就绪**，且富摘要含 2 处 `[图片]` 占位
- **THEN** 记录一条含占位计数 2 的 info，随后照常进入掷骰与投递

#### Scenario: 富摘要无占位
- **WHEN** 编排任务**摘要就绪**且富摘要不含任何媒体占位
- **THEN** 照常记录 info，计数为 0，转发域行为与无该日志时一致
