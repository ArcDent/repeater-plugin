# HANDOFF

## 最近操作（2026-09-18）

- 完整走完 spec-driven 工作流 `integrate-cross-group-forward`（0.6.0，ADR 0009 + 0010）：把 arclinux 上独立运行的 `papaya0481/maibot-ForwardMessagesAuto-plugin` 并入本插件，形成第三个行为域「跨群转发」。双路径并存——**自动转发**（源群收到合并转发后对每个目标群独立掷骰 `weight_send`/`weight_silence`，命中即**协议端直发** `send_group_forward_msg`，成功后在目标群注入 `forward` 内容并触发**强制发言**、在源群注入 `forward_action` 留痕）与**请求转发**（原插件完整链路整包移植到 `forwards/request/`：源群 Planner 触发、查看资格、一次性授权、顺序投递，不掷骰、不强制发言）。两条路径共用**已投递状态**；三层防护（消息防重 / 源群冷却 / 目标群冷却）；投递前用协议端 `get_group_list`（TTL 300s + 失败 30s 退避）校验目标群可用性
- 纯逻辑全部落在 `forwards/` 子包（`source`/`nodes`/`auto`/`state`/`texts` + 共享叶子模块 `forward_segments.py`），`plugin.py` 只做 Hook/Tool 编排；`get_components()` 覆写把转发 Tool 的 `chat_scope` 提到组件顶层（承载性 hack，不写就私聊可见）
- `config_version` 1.1.0 → 1.2.0（新增 `[forward]` 14 字段，默认 `enabled=false`）；manifest 0.5.0 → 0.6.0（capabilities 4 → 11）；AGENTS.md 新增「转发域」章节、README 补 `[forward]` 全字段表与旧插件迁移段、CONTEXT.md 补转发域术语与例对话
- **已部署 arclinux 并群内实测通过**：插件 v0.6.0 上线、旧插件 `papaya0481_forward-messages-auto` 置 `enabled=false`（原配置备份 `config.toml.bak-before-repeater-migration`）、`[forward]` 段填 源群 3 个（新生群/柴犬新窝/蹲饼群，去掉失效的 `747839793`）目标群 2 个（新生群/柴犬新窝）；容器内实测 `get_group_list` 返回 ok/6 群，目标群均在列表
- 已归档至 `openspec/changes/archive/2026-09-18-integrate-cross-group-forward/`（29/29 任务全勾选），delta 已 sync 为新建主 spec `openspec/specs/cross-group-forward/spec.md`（10 需求 / 30 场景）
- 验证基线：pytest **604 passed** / pyright **0 errors** / ruff 51 项（全部为 AGENTS.md 声明的六类有意偏离）
- 远端仓库：`gh repo create ArcDent/repeater-plugin --private` 创建并推送 `main`（此前该插件从未推送过任何远端）；manifest `urls.repository` 同步指向新地址，测试合成包名 `sending_emojis_plugin` → `repeater_plugin` 一并清理

## 进行中

- 无活跃 openspec change；改动已提交并推送至 `ArcDent/repeater-plugin`（**私有**仓库，`origin` 已配置，`main` 跟踪 `origin/main`）

## 下一步（用户手动）

1. 观测自动转发的线上表现：`[forward]` 默认 `weight_send=30 / weight_silence=70`、源群与目标群冷却各 60s——按实际刷屏情况调权重与冷却；日志按「自动转发」前缀与目标群号过滤
2. 若计划提交 MaiBot 插件中心（其要求**公开**仓库），需把 `ArcDent/repeater-plugin` 在 Settings 里改为 public；manifest 的 `urls.repository` 已指向该仓库

## 关键发现

- **旧插件转发失败的根因**（本次排查）：目标群已不在机器人群列表，但 MaiBot 数据库仍留 NapCat 时代的陈旧会话；插件按群号解析到该 stream 后，Host 出站路由匹配不到 SnowLuma 网关绑定，回落 `legacy.send.qq` 后备驱动而失败，且失败被归一成无 `error` 字段的结果、只报「未知错误」。教训：**投递前必须校验群可用性，且直发比走 Host 发送链更能与路由解耦**（ADR 0010）
- `chat_scope` 只在 Runner 读取的**组件声明顶层**生效：SDK 的 `@Tool(chat_scope=…)` 只落进 `metadata`，Host 顶层优先 → 必须靠 `get_components()` 覆写补齐，否则 Tool 在私聊可见（ADR 0009 相关的承载性 hack）
- SDK 的 `HookHandler.error_policy` 默认 `ErrorPolicy.SKIP`、系统级 Hook 超时兜底 60s（`plugin_runtime.hook_blocking_timeout_sec`）——不声明也不会中断主链，比原插件显式声明的 3~6s 更宽松
- `forwards/request/` 必须保持「不 import `forwards/nodes.py`」：后者依赖 `repeats.py`，会让 `import forwards.request` 在裸目录下失败；共享解析核心走无依赖的叶子模块 `forwards/forward_segments.py`
- 主程序 `EventType.ON_MESSAGE` 事件分发代码被注释（`REFERENCE/MaiBot/src/chat/message_receive/bot.py:846`），是死事件；普通消息观测的活口是 `chat.receive.before_process` hook
- hook 消息解析失败打 debug、webhook 上报形态不认识打 warning——两个口径相反且各有理由，详见 AGENTS.md 复读域条目
- 改配置语义必须四源同改（config.py hint、config.toml 注释、README、AGENTS.md）；改版本号必须与 `_manifest.json` 同源
- openspec delta 的 MODIFIED 块不能改名 Scenario 标题（`validate --strict` 判漏抄）；sync 到主 spec 时新建 capability 要保留 delta 的 `## Purpose`
- 结构与规则真源见 `AGENTS.md`（软链 `CLAUDE.md`），术语见 `CONTEXT.md`，本文件不重复维护
