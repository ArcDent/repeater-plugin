## Context

动机与需求边界见 `proposal.md`；行为契约见 `specs/cross-group-forward/spec.md`，本文只记录「如何实现」层面的决策。

现状约束：

- 本插件现有两个行为域，`plugin.py` 承担全部 Hook/编排，纯逻辑在 `reactions.py` / `repeats.py`（扁平文件、无 SDK 依赖、可单测）；协议端 HTTP 直调通道（`_call_protocol`）与 `maisaka.context.append` 注入通道已在复读域跑通。
- 原插件 `papaya0481/maibot-ForwardMessagesAuto-plugin`（v0.2.12）是完整包结构：`core/`（capabilities / messages / models / state / streams）、`source/`（授权、历史解析、查看资格、触发、视图流）、`target/`（顺序投递）、`runtime.py` / `request.py` / `config_recovery.py`，并带完整单测与持久化状态（`forward_state.json`）。
- 两个 Hook 世界并存：复读域已注册 `chat.receive.before_process`（blocking）；原插件的源群触发走 `chat.receive.after_process`（observe），Planner 编排走 `maisaka.planner.*`。
- 线上故障根因（ADR 0010）：陈旧 stream 让 Host 出站路由回落到 `legacy.send.qq`，失败又被吞成「未知错误」。

## Goals / Non-Goals

**Goals**

- 自动转发路径：Hook 同步段纯内存完成解析、门禁、掷骰，命中后后台编舞（延迟 → 协议端直发 → 目标群注入 → 强制发言 → 源群留痕）。
- 请求转发路径：原插件链路与 Tool 原样进口，含查看资格与一次性授权。
- 两条路径共用已投递状态；投递前统一做目标群可用性校验。
- 转发域纯逻辑（节点转换、掷骰、状态、文案）不依赖 SDK，单测覆盖。

**Non-Goals**

- 不改造原插件的查看资格、授权凭据、顺序投递机制本身。
- 不扩展消息类型（仍只支持合并转发；私聊转发、普通消息转发不做）。
- 不保证媒体零丢失（图片二进制不可得时按降级处理，见 Risks）。
- 不做自动路径的跨重启防重（消息只在收到时处理一次，内存 TTL 充分）。

## Decisions

### D1 代码组织：`forwards/` 子包，请求路径整包进口

- 新建 `forwards/` 子包：`forward_segments.py`（共享叶子模块：转发段定位与节点校验，两条路径复用）、`source.py`（转发源消息解析与资格判定）、`nodes.py`（转发节点 OneBot 转换）、`auto.py`（掷骰与决策）、`state.py`（防重/冷却/已投递/群可用性缓存）、`texts.py`（注入文本与 intent）、`request/`（原 `forward_messages_auto/` 全量移植，保留内部结构以最小化改动与测试迁移成本）。
- `plugin.py` 只保留 Hook/Tool 声明与编排，遵循「不往 plugin.py 塞可测逻辑」的既有规则。
- 备选：全部塞进单文件 `forwards.py` —— 转发域体量是复读域的数倍，单文件不可维护。

### D2 自动路径挂在 `chat.receive.before_process`（observe，late）

- 与复读域同 Hook 注册（observe 模式，不参与 abort 决策链）：转发域**不拦截**源消息。
- 同步段只做纯内存工作：形态校验 → 源群门禁 → 防重/冷却判定 → 逐目标群掷骰 → spawn 后台任务；命中后的冷却写入与复读域同款「先写冷却再 spawn」。
- 源群 Planner 触发（请求转发路径的入口提示）在后台任务里排队并等待消息就绪，沿用原插件的等待窗口语义，超时记 warning 跳过。
- 备选：`chat.receive.after_process`（原插件做法）——多注册一个 Hook 增加主链开销，且自动掷骰并不需要「消息已入库」，放弃。

### D3 转发节点转换：MaiBot 段 → OneBot node

- `send_group_forward_msg` 要求 `messages: [{type:"node", data:{user_id, nickname, content:[OneBot 段]}}]`（SnowLuma 实测格式，参考 `packages/onebot/src/modules/message-actions.ts`）；本插件在 `forwards/nodes.py` 做转换，纯函数、可单测。
- 段映射：`text` → `text`；`at` → `at`（带 QQ）；`image` / `emoji` → `image`（优先 `binary_data_base64` → `base64://`，缺失时退回段内引用，再缺失降级为文本占位）；节点 content 内的嵌套 `forward` 段递归转换为 node 列表。
- 无效节点（无 content）跳过；全部无效则不投递（与 spec 的「无有效节点不参与」一致）。
- 备选：Host `ctx.send.forward`（写库 + 上下文同步）—— 已由 ADR 0010 否决，陈旧 stream 路由不可控。

### D4 目标群可用性校验：协议端 `get_group_list` + 内存缓存

- `forwards/state.py` 维护群列表缓存（TTL 300s）；自动路径每次投递前与请求路径每次投递前校验目标群在列表内。
- 缓存查询失败（协议端不可达）时放行投递并记 warning：校验是「减少无效投递」的优化，不允许它变成新的静默失败源。
- 备选：每次投递都查（RPC 放大）；不校验（本次故障重现）。

### D5 已投递状态：内存 registry 为主，请求路径持久化 store 保留

- 自动路径投递成功后写入 `DeliveredRegistry`（键：源聊天流 + 转发源消息 ID → 已投递目标群集合，TTL 沿用防重 TTL）。
- 请求转发的 Tool 入口先查 registry：已登记则直接返回「已转发过」；否则走原链路的持久化状态（`ForwardStateStore`）做 job 幂等。
- 自动路径不读请求路径的持久化 store：自动路径能命中说明消息刚到这里，此时不存在同消息的请求转发 job。
- 备选：两条路径统一持久化 —— 自动路径不需要跨重启语义，统一只会引入无谓 IO。

### D6 配置：新增 `[forward]` 段，`config_version` 升级

- 字段：`enabled`（默认 false）、`source_groups`、`target_groups`、`weight_send`（默认 30）、`weight_silence`（默认 70）、`dedup_ttl_seconds`（默认 1800）、`cooldown_seconds`（源群，默认 60）、`target_cooldown_seconds`（默认 60）、`delay_min_seconds` / `delay_max_seconds`（默认 0.5 / 2.0）、`trigger_source_planner`（默认 true）、`trigger_target_planner`（默认 true）、`view_failure_fallback_threshold`（默认 2）、`inject_max_chars`（注入文本上限，默认 800）。
- `plugin.config_version` 升级触发主程序重建配置骨架（沿用 ADR 0007 的迁移机制）；原插件 `config.toml` 不自动迁移，升级后按新字段重填并显式删除失效群号。
- 转发域**不复用** `scope` 白黑名单与 `behavior.delay_*`（spec 明文约束），只复用 `plugin.enabled` 总开关与 `protocol` 协议端配置。

### D7 编舞顺序与并发

- 每命中一个目标群 spawn 一个独立后台任务，任务内串行：延迟 → 协议端直发 → 目标群注入 → 触发 Planner（软强制）。目标群之间按 `target_groups` 顺序启动、各自独立推进（不复用复读域的 gather 并发编舞：这里没有「注入与延迟并发」的需求）。
- 源群留痕注入在至少一个目标群投递成功后执行一次，内容列出实际成功的目标群集合。
- 目标群不可用被跳过、直发失败时，该目标群不产生注入、不触发 Planner。
- 注入文本与 Planner intent 的构造全部在 `forwards/texts.py`（纯逻辑、可单测），`plugin.py` 只调用。

### D8 协议端调用与失败口径

- 复用现有 `_call_protocol` 通道：`send_group_forward_msg`、`get_group_list`、`get_forward_msg`（如需补全嵌套内容）。
- 成功判定沿用既有口径：不可达返回 `None`、HTTP 200 + `status != ok` 视为失败；失败记 warning、不重试、不熔断（与复读域一致）。
- 自动路径的投递日志 MUST 带「自动转发」前缀与目标群号，请求路径日志沿用原插件前缀，排查时先分清路径。

## Risks / Trade-offs

- [节点媒体保真：图片二进制不可得时转发内容缩水] → 转换层降级为文本占位并记 warning；首个真实部署用例里对照源消息逐条核对。
- [before_process 时消息尚未入库，源群 Planner 触发的等待窗口会变长] → 沿用原插件的轮询等待与超时告警；实测窗口不足时只影响请求转发路径的入口提示，不影响自动路径。
- [两条路径两套发送机制（直发 vs Host 发送）] → 日志前缀区分；文档（AGENTS.md/README）显式说明目标群消息库只有请求路径会写入。
- [请求路径进口后与原插件的配置字段名不同（`[routing]`/`[behavior]` → `[forward]`）] → `config_version` 升级 + README 迁移段；不写自动化迁移脚本。
- [自动路径直发消息不写库、不回流入站链] → 目标群上下文靠**转发内容注入**补偿；麦麦后续无法用平台 msg_id 引用这条转发（已知代价，与 ADR 0006 的直发属性一致）。
- [插件卸载/重载会取消在途后台任务] → 沿用复读域的 `on_unload` 取消语义；未完成的投递不回滚已写入的注入。

## Migration Plan

1. 本地完成实现与验证：`pytest` 全绿、`pyright` 零错误、`ruff` 保持既有偏离清单。
2. 部署 arclinux：rsync 新版本（排除 `config.toml`），确认 `[forward]` 段填写（源群 4 个、目标群去掉已失效的 `747839793`）。
3. **停用** `papaya0481.forward-messages-auto` 插件（配置 `enabled=false` 或移出 plugins 目录）后再启用转发域，避免双链路重复处理。
4. 人工验证：让群友在源群发一条合并转发，确认目标群收到直发内容、麦麦在目标群发言、源群出现留痕注入、日志同时出现路径前缀与可用性校验结果。
5. 回滚：禁用转发域、恢复旧插件插件目录与配置；已写入的注入无法回滚（可接受）。
