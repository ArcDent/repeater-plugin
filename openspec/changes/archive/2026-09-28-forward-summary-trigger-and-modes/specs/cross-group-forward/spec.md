## ADDED Requirements

### Requirement: 自动转发的触发时机与摘要就绪

**自动转发**的 `before_process` 同步段 MUST 只做纯内存工作：形态门禁、**源群**门禁、**转发防重** mark、抽取**转发源消息快照**（**转发节点**原始段 + 内存二进制）并 spawn 后台编排；同步段 MUST NOT 掷骰、MUST NOT 写冷却。后台任务 MUST 等待**摘要就绪**后才掷骰：就绪判定为轮询 `message.get_by_id` 直到该**转发源消息**已入库且 `processed_plain_text` 非空。等待超过 `forward.summary_timeout_seconds` 仍未就绪的候选 MUST 记 warning 并放弃本次（不掷骰、不写冷却、不投递、不注入）。就绪判定 MUST NOT 依赖识图描述回填：首见图的描述不进消息本体，插件 MUST NOT 等待一个不可观测的条件。

#### Scenario: 摘要就绪后才掷骰

- **WHEN** 一条合并转发通过同步段门禁并进入后台等待
- **THEN** 掷骰发生在该消息入库且 `processed_plain_text` 非空之后

#### Scenario: 超时未就绪则放弃

- **WHEN** 候选在 `summary_timeout_seconds` 内始终未就绪
- **THEN** 记 warning 后放弃本次，不掷骰、不写冷却、不投递



#### Scenario: 同步段无副作用除防重与快照

- **WHEN** 一条合并转发通过门禁
- **THEN** 同步段只写防重标记并 spawn 后台任务，冷却与掷骰均未发生



#### Scenario: 快照携带原始段与内存字节

- **WHEN** 快照在 `before_process` 时点生成
- **THEN** 快照含该转发的节点原始段与图片二进制，供就绪后的发送重建使用



### Requirement: 转发模式

`forward.mode` MUST 支持 `direct` 与 `planner` 两个取值。`direct` 模式下掷骰命中即进入投递编舞，MUST NOT 经过 LLM。`planner` 模式下掷骰命中后 MUST NOT 直发，MUST 把该**合并转发**的**富摘要**交**目标群** Planner 决策「转发 / 不转发」并据结果执行：决策为转发时走同一套投递编舞，决策为不转发时仅记录日志、不投递不注入。**转发权重** MUST 对两个模式都先生效（先掷骰再决策）。决策结果不可用（触发失败 / 超时 / 返回无法解析）时 MUST 记 warning 并按「不转发」处理。

#### Scenario: 直发模式命中即投递

- **WHEN** `forward.mode=direct` 且某目标群掷骰命中转发
- **THEN** 直接进入该目标群的投递编舞，不经任何 LLM 决策



#### Scenario: 决策模式命中后交 Planner 定夺

- **WHEN** `forward.mode=planner` 且某目标群掷骰命中转发
- **THEN** 不直发；插件把该转发的富摘要交给目标群 Planner 做转发与否的决策



#### Scenario: 决策为不转发则不投递

- **WHEN** 决策模式下 Planner 返回「不转发」
- **THEN** 该目标群不投递、不注入、不触发强制发言，仅记录日志



#### Scenario: 决策结果不可用按不转发处理

- **WHEN** 决策模式下 Planner 触发失败或返回无法解析
- **THEN** 记 warning 并按「不转发」处理该目标群



#### Scenario: 转发权重对两个模式都先生效

- **WHEN** 任一模式下某目标群掷骰命中沉默出口
- **THEN** 不投递、不进入 LLM 决策、不写冷却



### Requirement: 转发路径开关

`forward.auto_enabled` 与 `forward.request_enabled` MUST 分别独立控制**自动转发**与**请求转发**两条路径。任一为 false 时，对应路径 MUST 完全停用（不掷骰、不触发源群 Planner、不投递、不登记已投递状态），另一条路径 MUST 不受影响。两条路径的总闸仍是 `forward.enabled`（与 `plugin.enabled` 合取）。

#### Scenario: 仅停用自动路径

- **WHEN** `forward.auto_enabled=false` 且 `forward.request_enabled=true`
- **THEN** 源群合并转发不触发自动掷骰与投递，请求转发 Tool 仍可正常使用



#### Scenario: 仅停用请求路径

- **WHEN** `forward.request_enabled=false` 且 `forward.auto_enabled=true`
- **THEN** 请求转发 Tool 不可用，自动转发路径行为不变



#### Scenario: 域总闸同时停用两条路径

- **WHEN** `forward.enabled=false`
- **THEN** 两条路径均停用，与 `auto_enabled` / `request_enabled` 的取值无关



### Requirement: 转发节点重建的媒体兜底

快照段中的媒体段（image / emoji）MUST 按以下顺序重建：有二进制时转为 `base64://` 图片段；无二进制但段内数据为 `http(s)://`、`base64://` 或文件路径形态时按该引用重建；两者皆无时该媒体段 MUST NOT 产生任何输出（所在**转发节点**因此可能变空）。重建后内容为空的节点 MUST 被整条丢弃；全部节点落空时 MUST 放弃本次投递并记含原因的 warning。插件 MUST NOT 把非引用形态的段内数据（如主程序处理后写入的中文描述文本）当作文件引用发出。

#### Scenario: 媒体无字节且无可用引用时所在节点丢弃

- **WHEN** 某节点的唯一内容是既无二进制也无可用引用的图片段
- **THEN** 该节点被整条丢弃，不产生占位文本段



#### Scenario: 非引用形态的段内数据不得作为文件引用

- **WHEN** 某图片段的二进制缺失且段内数据是中文描述文本（如 `[图片：…]`）
- **THEN** 插件不把该文本当作 `file` 引用发出，该媒体段不产生输出



#### Scenario: 全部节点落空放弃投递

- **WHEN** 一条合并转发的所有节点重建后均为空
- **THEN** 放弃本次投递并记 warning，不调用协议端



## MODIFIED Requirements



### Requirement: 自动转发的防重与冷却

**自动转发**MUST 有三层防护：同一**转发源消息**（按消息 ID）在 `forward.dedup_ttl_seconds` 内 MUST 只参与一次掷骰；**源群**冷却期内（`forward.cooldown_seconds`）新到达的合并转发 MUST NOT 触发掷骰；**目标群**冷却期内（`forward.target_cooldown_seconds`）该目标群 MUST NOT 参与掷骰。**转发防重**的标记 MUST 写在 Hook 同步段（spawn 之前），不等**摘要就绪**；等待摘要是后台任务行为，MUST NOT 推迟防重标记。冷却 MUST 在掷骰命中「转发」出口时写入（无论投递成败），沉默出口 MUST NOT 写入冷却。源群冷却 MUST 在至少一个目标群命中转发时写入。因掷骰推迟到**摘要就绪**之后，冷却写入时刻相应后移：就绪等待窗口内到达的新**合并转发**会各自独立通过门禁并参与掷骰。

#### Scenario: 同一消息不重复掷骰

- **WHEN** 同一转发源消息在 `dedup_ttl_seconds` 内被重复上报
- **THEN** 该消息不再参与掷骰



#### Scenario: 防重标记先于等待写入

- **WHEN** 一条合并转发通过门禁进入后台等待摘要就绪
- **THEN** 其消息 ID 的防重标记在等待开始前已写入，等待期间重复上报不产生第二次编排



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

自动转发命中后 MUST 经**协议端直发**（`send_group_forward_msg`）投递到目标群，MUST NOT 走主程序标准发送链。掷骰 MUST 发生在**摘要就绪**之后；命中后 MUST 先写冷却再启动投递，投递 MUST 在命中后延迟 `forward.delay_min_seconds` / `forward.delay_max_seconds` 范围内的随机时长执行。各目标群 MUST 独立投递：单个目标群失败、被跳过或较慢 MUST NOT 阻塞其他目标群的投递；任务启动顺序 MUST 与 `target_groups` 列表一致，但物理发送先后不要求严格串行。投递前 MUST 校验目标群仍在机器人群列表中；不在列表的目标群 MUST 被跳过并记录含群号与原因的 warning。单次投递失败 MUST 记 warning、MUST NOT 重试、MUST NOT 熔断后续目标群与后续消息。投递内容 MUST 由**转发源消息快照**的**转发节点**重建，段与二进制均取自 `before_process` 时点的快照，MUST NOT 从消息事后存储形态恢复二进制（主程序处理后会丢弃内存字节且入库形态不回填）。

#### Scenario: 命中后直发目标群

- **WHEN** 某目标群命中转发出口
- **THEN** 插件在配置的延迟区间内通过协议端发送该合并转发，不走主程序发送链



#### Scenario: 发送内容取自快照

- **WHEN** 一条合并转发的图片在主程序处理后已不可从存储形态恢复
- **THEN** 投递内容仍包含该图片的二进制（来自 `before_process` 快照）



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

**自动转发**投递成功后 MUST 把该**合并转发**的**富摘要**（主程序 `processed_plain_text`，含转发内文本与已缓存图片的描述）注入**目标群**麦麦聊天历史，`source_kind` 为 `forward`。注入 MUST 在触发该目标群 Planner（**强制发言**）之前完成。投递失败的目标群 MUST NOT 产生注入。注入失败 MUST 记 warning、MUST NOT 重试。富摘要中首见图为占位时 MUST 按主程序原文注入，插件 MUST NOT 自行改写、补写或丢弃主程序生成的摘要文本。

#### Scenario: 投递成功后注入

- **WHEN** 某目标群的自动转发投递成功
- **THEN** 该目标群的聊天历史中出现一条 `source_kind=forward` 的注入，内容为该转发的富摘要（而非插件自有段摘要）



#### Scenario: 富摘要含已缓存图片的描述

- **WHEN** 该合并转发包含此前已被识别的图片
- **THEN** 注入文本包含这些图片的描述，而非 `[图片]` 占位



#### Scenario: 投递失败不注入

- **WHEN** 某目标群的自动转发投递失败
- **THEN** 该目标群不产生任何转发内容注入



#### Scenario: 注入先于 Planner 触发

- **WHEN** 投递成功且该目标群需要触发强制发言
- **THEN** 转发内容注入先写入，Planner 触发后能读到该注入



### Requirement: 强制发言

**自动转发**投递成功后 MUST 对该目标群触发一次 Planner 主动任务，意图 MUST 要求针对这条转发说一句自然的话（软约束：Planner 仍可能沉默，插件 MUST NOT 检测或补发）。意图材料 MUST 取自该转发的**富摘要**。**决策模式**下，强制发言 MUST 只在实际发生投递后触发；Planner 决策为「不转发」时 MUST NOT 触发。**请求转发**路径 MUST NOT 应用强制发言，其目标群 Planner 保持「自主决定、可沉默」的原有语义。

#### Scenario: 自动转发成功后触发发言

- **WHEN** 自动转发在某目标群投递成功
- **THEN** 该群收到一次要求针对该转发发言的 Planner 主动任务，意图含该转发的富摘要材料



#### Scenario: 决策模式不转发不触发

- **WHEN** 决策模式下目标群 Planner 决策为「不转发」
- **THEN** 不投递、不注入、不触发强制发言



#### Scenario: 请求转发不强制发言

- **WHEN** 一次请求转发投递成功
- **THEN** 目标群 Planner 按原语义自主决定是否发言，不受强制发言约束



### Requirement: 转发域开关与配置

转发域 MUST 由 `forward.enabled` 独立开关控制，整域（自动转发、请求转发、源群触发、两类注入、强制发言）MUST 跟随该开关；插件总开关 `plugin.enabled` 为 false 时转发域 MUST 一并停用。两条路径 MUST 另有独立开关 `forward.auto_enabled` 与 `forward.request_enabled`。转发域 MUST NOT 复用表情回应域的 `behavior.delay_min/max` 延迟字段，也 MUST NOT 复用 `scope` 白黑名单作为源群过滤。协议端 HTTP 通道未配置时，自动转发 MUST 记 warning 并放弃投递（不静默失败），启动自检 MUST 提示该状态。`forward.mode`、`forward.auto_enabled`、`forward.request_enabled`、`forward.summary_timeout_seconds` MUST 为转发域配置字段，默认值 MUST 与 `config.toml` 声明逐项一致，且 MUST 具备中文界面标签与注释。

#### Scenario: 域开关独立生效

- **WHEN** `forward.enabled=false` 且 `plugin.enabled=true`
- **THEN** 转发域不掷骰、不触发源群 Planner、不产生注入



#### Scenario: 协议端未配置时的可见性

- **WHEN** `forward.enabled=true` 但 `protocol.base_url` 为空
- **THEN** 启动自检给出提示，自动转发命中时记 warning 并放弃投递



#### Scenario: 新配置字段默认值一致

- **WHEN** 部署使用默认 `config.toml`
- **THEN** `forward.mode`、`forward.auto_enabled`、`forward.request_enabled`、`forward.summary_timeout_seconds` 的取值与配置模型默认值一致

