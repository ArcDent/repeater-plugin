## Context

动机与需求边界见 `proposal.md`；行为契约见 `specs/cross-group-forward/spec.md`（delta）。术语（**转发源消息快照** / **摘要就绪** / **富摘要** / **直发模式** / **决策模式**）见 `CONTEXT.md`，决策背景见 ADR 0011，本文只记录「如何实现」。

现状约束：

- 自动路径现有骨架（v0.6.0）：`before_process` observe handler（`forward_detector`，`order="late"`）同步段完成「门禁 → 防重 → 冷却 → 逐目标群掷骰」，命中后 spawn「每目标群一个投递任务 + 一个收尾任务」；`_deliver_forward_to_target` 串行「延迟 → 可用性校验 → 协议端直发 → 注入 → 强制发言」。
- `ForwardSource`（ frozen dataclass）本就是「同步段解析一次、后台只读」的快照结构——`nodes` 内的段字典在 hook 载荷形态下已携带 `binary_data_base64`。**「快照」不是新类型，而是把既有 `ForwardSource` 的使用时机从「同步段即刻掷骰」改为「就绪后掷骰」**。
- `message.get_by_id(message_id, stream_id=..., include_binary_data=...)` 返回 Host 消息字典（SDK 已解包），请求路径已在用；轮询就绪只需 `include_binary_data=False`。
- `maisaka.proactive.trigger` 返回的是**入队结果**（`{"success": ...}`），不是模型输出——「让 LLM 决定是否转发」无法用 trigger 的返回值承载，必须经 Tool 回路拿决策（见 D4）。

## Goals / Non-Goals

**Goals**

- 同步段收敛为「门禁 + 防重 mark + 快照 + spawn」；掷骰、冷却写入、投递、注入全部挪到**摘要就绪**之后，就绪只轮询一次、结果共享给全部目标群。
- 发送字节 100% 来自快照；注入与 intent 材料改用**富摘要**（`processed_plain_text`）。
- `forward.mode` 双模式；`auto_enabled` / `request_enabled` 路径开关；新配置全中文。
- 媒体兜底收紧，杜绝垃圾 file 引用拖垮整包。

**Non-Goals**

- 不等 VLM 识图描述回填（不可观测，见 ADR 0011 证据 5）；不为提升描述覆盖率改主程序。
- 不修请求路径在 adapter 内的媒体引用回落（中文描述 data → `file://` 垃圾引用）；请求路径按既有失败口径返回明确原因（见 Risks）。
- 不给决策模式做「模型超时未决策则补投」——按 spec 口径按不转发处理。
- 不做跨重启的掷骰持久化（沿用内存语义）。

## Decisions

### D1 双段式编排：同步段只留门禁与快照，掷骰移入后台就绪后

- `handle_forward_message` 同步段保持既有顺序：域开关 → `parse_forward_source` → 源群门禁 → 请求路径入口提示（`observe_source_message`，不变）→ 防重 `mark_message` → spawn **一个编排任务**（`_orchestrate_forward_delivery(source)`）。掷骰与 `mark_group` / `mark_targets` 从同步段删除。
- 编排任务内：等**摘要就绪**（D2）→ `decide_forward_targets` 逐目标群掷骰 → 命中则先写冷却（`mark_group` + `mark_targets`）→ 每命中群 spawn 现有投递任务（`_deliver_forward_to_target` 原样复用，内部编舞不变）→ spawn 收尾任务（`_finalize_forward_delivery` 原样复用）。
- 就绪等待只做一次：轮询结果（富摘要文本）随编排任务共享给全部目标群的注入与 intent。
- 备选：保持「同步段掷骰、只推迟投递」——用户明确要求时机含决策本身（planner 模式本就须等摘要才能给 LLM 材料），否决。

### D2 摘要就绪：轮询 `get_by_id` 直到入库且摘要非空

- 就绪判定：`await ctx.message.get_by_id(source.message_id, include_binary_data=False)` 返回 dict 且 `processed_plain_text`（strip 后）非空。**只带消息 ID、不带 `stream_id`**：Host 按「聊天流 + 消息 ID」过滤，而同群在 `chat_sessions` 里可能存在新旧多个 session（群改名 / 会话平滑迁移后），插件解析到的 stream 可能是陈旧的，带上它查询恒为空（生产实测，见文末补记）；消息 ID 在平台内唯一，本就是充分条件。
- 轮询节奏：首次立即查，之后 0.5s 间隔退避到 1s 上限；总时长受 `forward.summary_timeout_seconds`（默认 10s）约束。超时 → warning「摘要就绪超时」并放弃本次（不掷骰、不写冷却、不投递）。
- `get_by_id` 异常按「未就绪」处理（记 debug 继续轮询），不因此放弃；只有超时才放弃。
- 备选：固定延迟（不可观测的入库时刻，要么白等要么漏等）；等识图描述填满（19.6% 首见图永远不成立，ADR 0011 证据 5），均否决。
- 富摘要读取：就绪的那次 `get_by_id` 结果里直接取 `processed_plain_text`，不额外再查。

### D3 富摘要注入与文案清洗

- `forwards/texts.py`：`build_forward_content_text(source, *, rich_summary, max_chars)` 与 `build_forced_comment_intent(source, *, rich_summary)` 改以富摘要为内容主体（保留「[跨群转发] 来自X的合并转发（N 条）」式来源前缀与发送者信息、`inject_max_chars` 截断）；节点级段摘要（`_node_summary`）退居为富摘要缺失时的兜底（超时放弃后不会注入，兜底实际只对异常路径有意义）。
- 控制字符清洗：新增纯函数 `sanitize_display_text(text)`（剔除 C0/C1 控制字符与孤立代理项），注入文案与 intent 的群名、富摘要均过一遍——生产 `group_name` 含损坏字节（`<\$ÿĀ\x11\x10>柴犬的新窝`），现状直接泄漏进文案。
- `build_forward_trace_text` 只列群号集合，不过群名，无需清洗（保持）。

### D4 决策模式：一次性 decision ticket + 转发决策 Tool

- 掷骰命中后（仅 `mode=planner`）不进入投递，而是：生成**一次性决策票**（内存 dict：`message_id + target_group_id → ticket`，TTL 取 `dedup_ttl_seconds` 同量级）→ 向目标群 `proactive.trigger`，intent 要求 Planner 调用转发决策 Tool 并传「转发 / 不转发」与一句理由，材料为富摘要。
- 新增 Tool（`plugin.py` 声明 + `forwards/` 决策执行编排）：参数 `message_id`、`deliver: bool`；handler 校验票存在且未消费（消费即焚）、目标群与票一致、消息仍在票 TTL 内；`deliver=true` 时走**同一套投递编舞**（延迟 → 可用性 → 直发 → 富摘要注入 → 强制发言 → 留痕登记）；`false` 时记 info 日志收尾。
- 票的滥用防护：群友不能凭空构造有效票（票只在插件掷骰命中时生成、一次性）；Tool 声明 `chat_scope="group"` 并沿用 `get_components()` 覆写（承载性 hack，AGENTS.md 明文不得删）。
- 备选：解析 planner 文本回复拿决策——文本形态不可靠、无回路；用 trigger 返回值——只是入队结果不是模型输出（已验证）。均否决。
- Tool 命名与 intent 文案在实现期定稿（实现细节，不影响 spec 行为面）。

### D5 媒体兜底：`_media_segment` 三分支与空节点丢弃

- `forwards/nodes.py::_media_segment` 改为：有 `binary_data_base64` → `base64://` 图片段；否则段内 `data` 匹配 `^(https?://|base64://|file://)` 或含路径分隔符 → 按引用重建；否则返回 `None`（段不产出，节点可能变空）。
- 上层沿用现有空转逻辑：`nodes_to_onebot_messages` 跳过空节点、全空返回 `[]`；`_deliver_forward_to_target` 对 `[]` 记 warning 放弃（既有行为，spec「转发节点重建的媒体兜底」）。
- 占位文本（`[图片]` / `[表情]`）从**自动路径的发送转换**中移除；`forwards/texts.py` 注入摘要中对首见图的占位来自主程序富摘要原文，不经过本映射。
- 测试影响：`tests/test_forwards_nodes.py` 中断言「无二进制图片降级为占位文本」的用例按新口径改为「节点丢弃」（tasks 修订）。

### D6 配置：四个新字段，默认值保持现有部署行为

- `forward.mode: str = "direct"`（非法值运行时按 direct 并记 warning）、`forward.auto_enabled: bool = True`、`forward.request_enabled: bool = True`、`forward.summary_timeout_seconds: int = 10`（`ge=1, le=300`）。
- 全部中文 `label` / `hint`（含 webui `order` 续排），`config.toml` 同步中文注释；`config_version` 1.2.0 → 1.3.0，`_manifest.json` 版本与 CHANGELOG 同步；capabilities 数量不变。
- 默认 `mode=direct` + 两个路径开关默认 true：升级后部署行为与 v0.6.0 完全一致（除掷骰时机后移），用户显式切换才启用新行为。

### D7 失败与日志口径（沿用 + 新增）

- 新增日志：就绪超时（warning，含 msg_id 与群号）、决策模式触发与消费（info）、决策为不转发（info）。沿用「自动转发」前缀与目标群号口径；投递失败 / 不可用 / 放弃口径不变。
- 卸载取消：编排任务与投递任务都进 `_forward_tasks`，`on_unload` 一并取消（沿用现有 `_cancel_forward_tasks`）。

## Risks / Trade-offs

- [就绪等待拉长转发延迟] → 默认超时 10s、轮询退避上限 1s；生产实测入库发生在 after_process 后毫秒级；部署后用就绪耗时分布复核默认值。
- [等待窗口内突发转发各自掷骰，冷却压制减弱] → ADR 0011 已记录的已知语义变化；窗口通常亚秒级，影响限于同群几乎同时到达多条合并转发的极端场景。
- [决策 Tool 被群友诱导滥用] → 一次性票（命中时生成、消费即焚、TTL 受限）+ 目标群匹配校验；无票调用返回明确拒绝。
- [富摘要对首见图仍占位（约两成媒体）] → 主程序边界（描述不回填消息本体）；注入按原文保留占位，插件不改写。
- [请求路径媒体引用回落仍在 adapter 内（中文描述 data → `file://` 垃圾引用 → 协议端抛错）] → 本 change 不修 adapter；请求路径按既有口径把失败原因返回 Planner；列为后续独立问题。
- [`get_by_id` 在就绪前抛异常] → 按未就绪继续轮询（debug），仅超时放弃；不因单次异常放弃候选。

## Migration Plan

1. 本地实现 + 单测 + 全量校验（pytest 全绿 / pyright 零错误 / ruff 维持六类偏离）。
2. 部署 arclinux：rsync 新版本（排除 `config.toml`），`config.toml` 增加四个新字段（保持 `mode = "direct"`），重启容器。
3. 人工验证（直发模式）：源群发**含图**合并转发 → 目标群收到**带图**转发（对照源消息逐节点核对）→ 目标群注入为富摘要（已缓存图带描述）→ 麦麦发言能引用转发内容 → 日志无「未知错误」。
4. 人工验证（决策模式）：切 `mode = "planner"`，源群发合并转发 → 目标群 Planner 收到决策请求（含富摘要）→ 模型经 Tool 回复转发 → 走完投递编舞；再验证「不转发」与无票调用被拒。
5. 回滚：`mode` 回 `direct` 或关闭域开关；已写入的注入不回滚（既有语义）。

## Open Questions

- `summary_timeout_seconds` 默认值（10s）按部署后实测就绪耗时分布调整；不影响行为面。
- 决策 Tool 命名与 intent 文案定稿（实现期细节）。

## 生产实测补记（2026-09-27，arclinux）

两条非本 change 计划内、但由本次上线暴露的主机侧口径问题，已在 `plugin.py` / `forwards/request/core/streams.py` 内修掉（插件 0.7.1）：

1. **就绪轮询带 `stream_id` 会恒空**：Host `message.get_by_id` 按「聊天流 + 消息 ID」过滤，而同群在 `chat_sessions` 里存在新旧多个 session（群名被改后主程序按新路由组件算出了新会话）。插件按群解析到的是陈旧 session（`get_stream_by_group_id` 是会话列表首匹配），11 轮轮询全空后超时放弃；主机侧同参数直接查（不带流过滤）能查到。修复：就绪判定只带消息 ID。
2. **群 → 活跃聊天流的解析必须优先 hook 上报的 `session_id`**：主程序在派发 `chat.receive.before_process` 之前已按路由口径算好 `session_id`（消息入库与 Planner 读写都用它）。此前插件用 `get_stream_by_group_id` 的首匹配，把**转发内容注入**、**强制发言**触发与**已投递状态**登记都写进了陈旧会话——麦麦在死会话里生成回复后发送失败（`未找到目标平台: qq` → `legacy.send.qq` 失败），注入内容也不出现在麦麦真正在读的会话里。修复：Hook 同步段被动学习「群 → 活跃会话」并优先使用，能力查询退居兜底；请求转发的 `GroupStreamRegistry` 接受同一来源的 `observed_session` 注入（两条路径保持同一 stream 口径，共享**已投递状态**才不会错位）。

同批观察（**不修**，主机侧行为）：麦麦构建多模态 prompt 时按 `emoji_hash` / `image_hash` 回填二进制，命中不到就退化为文本占位——`images` 表里确实没有那几条 hash（表内另有 2203 条 EMOJI / 3405 条 IMAGE），且转发卡片经协议端直发不会写回主程序消息库（`mai_messages` 里查不到），属主程序图片存储的覆盖缺口，插件侧无可修项（注入文本按主程序原文保留占位）。

