## Why

把 arclinux 上独立运行的 `papaya0481/maibot-ForwardMessagesAuto-plugin`（自主跨群转发）并入本插件，形成第三个行为域，并复用本插件已有的概率掷骰、协议端直发与上下文注入能力。

原插件当前在线上存在两类问题：

1. **转发静默失败且原因不可见**：目标群 `747839793` 已不在机器人群列表，但 MaiBot 数据库仍留有 6 月 NapCat 时代的陈旧会话；聊天流解析命中该陈旧 stream 后，Platform IO 路由回落到 `legacy.send.qq` 后备驱动，旧链发送失败，Host 把失败归一成无 `error` 字段的结果，插件只能记录「未知错误」。
2. **触发完全依赖 LLM 判断**：源群强制触发 Planner 后由模型决定是否分享，既不掷骰也不受本插件既有概率体系约束，与用户期望的「可控概率自动化」不符。

## What Changes

- 新增转发域（与复读、表情回应并列）：**源群白名单收到合并转发 → 对每个目标群独立掷骰（`weight_send` / `weight_silence`）→ 命中即协议端直发到目标群**，全程不经过 LLM 判断。
- **BREAKING（配置）**：`config_version` 升级，`config.toml` 新增 `[forward]` 段；旧部署升级后需填群白名单与权重，转发域默认关闭。
- 搬运原插件的**请求转发**完整链路（源群强制触发 Planner、查看资格、一次性授权、顺序投递、转发 Tool），保留「被 @ 请求转发」的既有使用习惯；该路径不掷骰。
- 两条路径共用**已投递状态**：自动路径命中并投递成功后登记，请求转发拒绝重复。
- 转发成功后复用上下文注入：目标群注入转发内容（`source_kind=forward`），源群注入行为留痕（`source_kind=forward_action`）；目标群 Planner 由自动路径触发并软约束其针对转发说一句话。
- **投递前置校验**：两条路径发送前确认目标群仍在机器人群列表，不在列表的群跳过并给出明确原因，不再产生「未知错误」。
- 防护三件套：同一转发源消息的内存防重、源群冷却、目标群冷却。

## Capabilities

### New Capabilities

- `cross-group-forward`: 跨群转发的行为契约——转发源消息资格、自动转发的掷骰与三件套防护、协议端直发与目标群可用性校验、转发内容/留痕注入、强制发言、请求转发完整链路（查看资格 / 授权 / 顺序投递）、双路径共用的已投递状态。

### Modified Capabilities

（无：现有 `message-repeat` 与 `context-injection` 的需求口径不变。）

## Impact

- **代码**：`plugin.py`（新增 Hook 与 Tool 编排）、新增转发域纯逻辑层、`config.py` / `config.toml`（`[forward]` 段）、`_manifest.json`（版本与 capabilities）、`README.md` / `AGENTS.md` / `CONTEXT.md`。
- **依赖能力**：协议端 HTTP 通道（`send_group_forward_msg`、`get_group_list`、`get_forward_msg`）、`maisaka.context.append`、`maisaka.proactive.trigger`、`message.get_by_id`、`chat.get_group_streams` 等（以 change design 的清单为准）。
- **部署**：arclinux 上必须停用原 `papaya0481.forward-messages-auto` 插件，否则同一源消息会被两套链路重复处理。
- **验证基线**：pytest / pyright 全绿；转发域纯逻辑单测覆盖掷骰、防重、冷却与状态机。
