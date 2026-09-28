## Why

v0.6.0 转发域（ADR 0009/0010）上线后暴露四个问题：**目标群模型上下文里没有转发内容**（生产实锤：自动转发成功后麦麦在目标群回复「欸 我转发什么啦」，群友追问「为啥小小有时候会转发聊天记录」——注入文案是插件自有的有损段摘要，图片/表情/嵌套转发全是 `[图片]`/`[表情]`/`[嵌套转发]` 占位）；**转出的转发消息图片丢失**（用户报告收端看到 `[图片]` 占位文本）；**没有模式选择**（掷骰命中后只能插件直发，不能让 LLM 决定是否转发）；配置项偏少且缺少路径级开关。

根因（阶段一诊断，ADR 0011）：自动转发挂在 `chat.receive.before_process`，该时点主程序**尚未处理消息**——`processed_plain_text` 不存在、图片无描述，插件只能用自有摘要注入；而一旦等主程序处理完，`process_image_component` 会把图片内存字节主动清空，晚触发只能靠 DB 按 hash 恢复（生产全量扫描 4286 个转发内媒体段：2.8% 按 hash 取不回字节、19.6% 连描述都永远为空）。识图描述异步构建且只回填 Maisaka 内存历史、消息本体入库后永不更新。

## What Changes

- **触发时机后移**：`before_process` 同步段收敛为门禁（形态 + **源群**）+ **转发防重** mark + **转发源消息快照**（**转发节点**原始段 + 内存字节）→ spawn 后台编排；**掷骰与投递整体推迟到「摘要就绪」**之后——轮询 `message.get_by_id` 直到消息入库且 `processed_plain_text` 非空，`forward.summary_timeout_seconds` 兜底，超时记 warning 放弃本次。**不等 VLM 描述回填**（不可观测：首见图占 19.6%，永远等不到）。
- **发送用快照**：协议端直发一律用快照的 before_process 内存字节，不经 DB 恢复，图片 100% 保全（adapter 下载失败或超限被主程序预先丢弃的除外，按媒体兜底口径处理）。
- **富摘要注入**：目标群**转发内容注入**、源群**转发留痕注入**、**强制发言** intent 的材料从插件自有段摘要改为主程序 `processed_plain_text`（转发内文本全量 + 已缓存图的描述；首见图降级 `[图片]` 占位，覆盖从「全部占位」降为「约两成占位」）。
- **BREAKING（配置）**：新增 `forward.mode = direct | planner`。`direct` 维持现语义（命中即直发，不经过 LLM）；`planner` 命中后不直发，把该**合并转发**的富摘要交给**目标群** Planner 决策「转发 / 不转发」，据结果走同一套投递编舞。**转发权重**对两个模式都先生效（先掷骰再决策）。
- **BREAKING（配置）**：新增 `forward.auto_enabled` / `forward.request_enabled` 两条路径的独立开关；`forward.enabled` 仍是转发域总闸（与 `plugin.enabled` 合取）。
- **媒体兜底收紧**：快照段中不可发送的媒体，所在**转发节点**整条丢弃，全部节点落空则放弃本次投递；`forwards/nodes.py` 的段内引用回落收紧为「仅 `http(s)://` / `base64://` / 文件路径形态当文件引用，否则文本占位」——杜绝把处理后中文描述当 file 引用发出、被协议端 `statSync` 抛异常拖垮整包发送（请求路径同样受益）。
- **文案清洗**：注入与 intent 文案生成前清洗群名等来源字段的控制字符（生产 `group_name` 含损坏字节，现状直接泄漏进文案）。
- 全部新增配置字段中文 `label` / `hint`，`config.toml` 同步中文注释；`config_version` 升级、`_manifest.json` 与 CHANGELOG 同步。

## Capabilities

### New Capabilities

（无：全部变更落在既有 `cross-group-forward` 能力上。）

### Modified Capabilities

- `cross-group-forward`: 自动转发的触发时机（**摘要就绪**后掷骰）、发送字节来源（**转发源消息快照**）、注入材料（**富摘要**）、模式选择（`forward.mode` 直发 / 决策）、路径开关（`auto_enabled` / `request_enabled`）、媒体兜底与引用回落口径、就绪超时放弃语义。

## Impact

- **代码**：`plugin.py`（Hook 同步段收敛、后台编排前置就绪轮询、planner 模式的决策触发与结果执行）、`forwards/`（新增快照 / 就绪 / 富摘要纯逻辑层，可单测）、`forwards/nodes.py`（媒体回落收紧）、`forwards/texts.py`（富摘要注入文案、决策 intent、控制字符清洗）、`config.py` / `config.toml` / `_manifest.json`、`README.md` / `AGENTS.md` / `CONTEXT.md`（术语已于阶段一先行更新）。
- **依赖能力**：`message.get_by_id`（就绪轮询 + 富摘要读取，请求路径已在用）、协议端 HTTP（`send_group_forward_msg` / `get_group_list`）、`maisaka.context.append`、`maisaka.proactive.trigger`。
- **行为变化**：掷骰推迟 ⇒ **源群** / **目标群**冷却写入时刻后移（就绪等待窗口内到达的新**合并转发**会各自独立掷骰，「同步段原子写冷却压制突发」的强度下降）；**转发防重** mark 仍在同步段，语义不变。
- **已知边界**：首见图的 VLM 描述在插件可观测的任何接口里都拿不到（主程序不回填消息本体），富摘要对约两成媒体仍是占位——这是主程序边界，不是插件降级。
- **验证基线**：pytest 全绿、pyright 零错误、ruff 维持 AGENTS.md 声明的六类有意偏离。
