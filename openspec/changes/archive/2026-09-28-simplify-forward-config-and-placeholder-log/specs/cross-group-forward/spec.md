## ADDED Requirements

### Requirement: 自动转发的富摘要占位观测

**自动转发**的编排任务在**摘要就绪**时 MUST 记录一条 info 日志，含本次**富摘要**中媒体占位（`[图片]` / `[表情]` / `[嵌套转发]`）的出现计数。该观测 MUST NOT 改写注入文本或 intent 材料（占位按主程序原文保留），MUST NOT 新增配置项，MUST NOT 影响掷骰、冷却、投递与注入的任何行为分支。

#### Scenario: 就绪后记录占位计数
- **WHEN** 编排任务轮询到**摘要就绪**，且富摘要含 2 处 `[图片]` 占位
- **THEN** 记录一条含占位计数 2 的 info，随后照常进入掷骰与投递

#### Scenario: 富摘要无占位
- **WHEN** 编排任务**摘要就绪**且富摘要不含任何媒体占位
- **THEN** 照常记录 info，计数为 0，转发域行为与无该日志时一致

## MODIFIED Requirements

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
