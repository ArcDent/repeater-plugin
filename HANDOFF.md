# HANDOFF

> **当前状态（2026-09-29）**：change `remove-cross-group-forward` 正在实施——跨群转发域（自动转发 / 请求转发、两个转发 Tool、四个转发路径 Hook、`[forward]` 配置、转发注入与专属状态）已从运行代码完整移除，第 5 组完成现行文档与规则收敛，第 6 组完成本项目全量验证与切换条件交接（见下节）；部署与回滚动作均不自动执行。
>
> 转发域的部署 / 配置 / 调试指引已随该域移除失效，不再适用。旧草案 `openspec/changes/forward-by-id-and-runtime-consolidation/` 已由用户于 2026-09-28 自行清除（该目录现为空），本次实施不涉及该草案。
>
> 口径差异备注：`docs/adr/0014-standalone-forward-plugin.md`（阶段一提案，受保护不改）内文仍写「两参数工具」，与用户最终确认的转发工具三参数口径（含 `comment`，新项目 ADR 0001 已记录）不一致；本仓库现行文档不承诺该签名，新插件的工具签名以其自身规格为准。
>
> 通用发布待办（与本次变更无关）：若计划提交 MaiBot 插件中心（要求**公开**仓库），仍需把 `ArcDent/repeater-plugin` 在 Settings 里改为 public。

## 当前变更交接：remove-cross-group-forward（2026-09-29，第 6 组）

- **本项目已完成**：转发域整域移除（`forwards/**` 33 文件、两个转发 Tool、四个转发 Hook、`[forward]` 配置、七项专属能力与全部专属测试），配置 / 能力 / 文档收敛为两域；验证 pytest **342 passed**、pyright **0 errors**、`openspec validate --strict` 通过（valid；该项属流程级命令，由编排者在组外补执行）。
- **切换前置条件（当前未满足，禁止直接部署）**：转发能力的新归属是独立插件「麦麦搬运喵」（同级目录 `maimai-courier-plugin`，实现尚未开始）。必须先取得该插件的验收证据（最低发送类型 + 活跃/冷会话上下文）并确认交付版本；本插件不代验收、不修改对方项目。
- **部署顺序**：① 确认旧转发功能已停用——本版本（0.9.0）已移除全部转发入口，部署它即完成停用；② 再启用新插件并配置其白名单。不要同时运行新旧两条转发路径。
- **回滚**：先停新插件，再恢复本插件上一版本（0.8.0）；「新旧转发路径同时运行」不在可接受回滚范围内。
- **数据边界**：旧运行时转发账本不自动迁移、不自动删除，由部署者按需处理；用户未提交的 `tests/test_plugin_forward.py`（+89 行）已备份至 `.local/backup-user-test_plugin_forward.py` 与 `.local/backup-user-test_plugin_forward.patch`（SHA-256 前 7 位 `09f6079` / `e3fd1b0`，全量与第 1 组基线记录一致）。
- **第 7 组已完成（2026-09-29）**：主规格 `openspec/specs/cross-group-forward` 的 11 条需求已随 delta 同步移除、空 capability 已删除（`openspec/specs/` 仅剩 `context-injection` 与 `message-repeat`）；本变更已归档至 `openspec/changes/archive/2026-09-29-remove-cross-group-forward/`。
- **未执行**：真实群消息端到端测试、部署与 Git 提交均未执行（提交交由用户）。

## 历史：simplify-forward-config-and-placeholder-log（2026-09-27 晚，simplify-config 分支）

- 完成 change `simplify-forward-config-and-placeholder-log` 阶段一至四（规划 + 实现 + 双轴评审）：
  - 阶段一（grill + 生产实录排查，SSH arclinux 审全天日志）锁定两项 BREAKING + 一项观测：删 `forward.trigger_source_planner`（入口提示随 `request_enabled` 固化开启）、删 `forward.view_failure_fallback_threshold` 连计数机制（查看失败首次即摘要降级）、新增富摘要媒体占位计数 info（就绪后掷骰前，纯观测零配置）
  - 生产实录（17:00–18:37）：planner 模式试用一轮（2 票、1 次选「不转发」、1 票悬空、0 次真转发，随后切回 direct）；请求转发入口触发 8 次、模型零响应、Tool 0 调用；问题 B（`images` 表缺 hash 致麦麦多模态回填失败）定为主机侧缺口、插件无可修项（占位改为可观测，不改写）
  - 阶段四：组 1（删两字段全链：模型 / 默认配置 / 适配层 / trigger 三处判定 / 查看计数链路 / 全部测试引用点）、组 2（`count_rich_summary_placeholders` 纯函数 + 就绪 info + 两层测试）、组 3（README / AGENTS.md / CONTEXT.md 文档同步）均由子代理按 TDD 完成，Spec 轴与 Standards 轴评审均 PASS；4.1 / 4.2 前半（全量校验 + `validate --strict`）已执行
  - 产物：ADR 0012（转发域配置收敛 + 占位观测）；配置 18 → 16、`config_version` 1.4.0、manifest 0.8.0
  - 验证基线：pytest **699 passed** / pyright **0 errors** / ruff 54 项（全在 AGENTS.md 声明的六类内，当时口径，现为四类）；`openspec validate --strict` 通过
  - **未提交**（用户口径：部署验证通过后再提交）；tasks 4.3（arclinux 部署人工验证）未勾
  - 现状补记（2026-09-29，不改上文历史事实）：该 change 已提交（`95780ab`）并归档至 `openspec/changes/archive/2026-09-28-simplify-forward-config-and-placeholder-log/`（21/21 全部勾选），上文「未提交 / 4.3 未勾」是当时状态

- 早前同一日（已提交 `36bb7dd`）：上下文注入成功日志统一「已写入 Maisaka 上下文: kind=… text='…' group=… msg=… index=… stream=…」字段序（对齐 smart_poke_plugin 呈现，`_log_preview` 60 字截断），三域注入路径同一格式，AGENTS.md 补口径

## 历史：forward-summary-trigger-and-modes（0.7.0 / 0.7.1，已随容器部署）

- 自动转发改**摘要就绪**后触发（ADR 0011）：同步段只留门禁 + **转发源消息快照**，轮询 `get_by_id` 等 `processed_plain_text` 非空后掷骰；发送用快照字节（图片 100% 保全）、注入与 intent 用**富摘要**；新增 `forward.mode = direct|planner` 与 `auto_enabled` / `request_enabled`、`summary_timeout_seconds`
- 0.7.1 生产修复两项 Host 口径问题：就绪轮询只带消息 ID（带 `stream_id` 因同群新旧 session 恒空）；群 → 活跃聊天流优先 hook 上报的 `session_id`（`get_stream_by_group_id` 首匹配可能是陈旧会话，注入 / 触发 / 登记会落进死会话）
- 本 change 归档动作（sync delta → archive）当时**尚未执行**；后已于 2026-09-28 归档至 `openspec/changes/archive/2026-09-28-forward-summary-trigger-and-modes/`（35/35 全部勾选）

## 历史：integrate-cross-group-forward（2026-09-18，已归档 0.6.0）

- 完整走完 spec-driven 工作流 `integrate-cross-group-forward`（0.6.0，ADR 0009 + 0010）：把 arclinux 上独立运行的 `papaya0481/maibot-ForwardMessagesAuto-plugin` 并入本插件，形成第三个行为域「跨群转发」。双路径并存——**自动转发**（源群收到合并转发后对每个目标群独立掷骰 `weight_send`/`weight_silence`，命中即**协议端直发** `send_group_forward_msg`，成功后在目标群注入 `forward` 内容并触发**强制发言**、在源群注入 `forward_action` 留痕）与**请求转发**（原插件完整链路整包移植到 `forwards/request/`：源群 Planner 触发、查看资格、一次性授权、顺序投递，不掷骰、不强制发言）。两条路径共用**已投递状态**；三层防护（消息防重 / 源群冷却 / 目标群冷却）；投递前用协议端 `get_group_list`（TTL 300s + 失败 30s 退避）校验目标群可用性
- 纯逻辑全部落在 `forwards/` 子包（`source`/`nodes`/`auto`/`state`/`texts` + 共享叶子模块 `forward_segments.py`），`plugin.py` 只做 Hook/Tool 编排；`get_components()` 覆写把转发 Tool 的 `chat_scope` 提到组件顶层（承载性 hack，不写就私聊可见）
- `config_version` 1.1.0 → 1.2.0（新增 `[forward]` 14 字段，默认 `enabled=false`）；manifest 0.5.0 → 0.6.0（capabilities 4 → 11）；AGENTS.md 新增「转发域」章节、README 补 `[forward]` 全字段表与旧插件迁移段、CONTEXT.md 补转发域术语与例对话
- **已部署 arclinux 并群内实测通过**：插件 v0.6.0 上线、旧插件 `papaya0481_forward-messages-auto` 置 `enabled=false`（原配置备份 `config.toml.bak-before-repeater-migration`）、`[forward]` 段填 源群 3 个（新生群/柴犬新窝/蹲饼群，去掉失效的 `747839793`）目标群 2 个（新生群/柴犬新窝）；容器内实测 `get_group_list` 返回 ok/6 群，目标群均在列表
- 已归档至 `openspec/changes/archive/2026-09-18-integrate-cross-group-forward/`（29/29 任务全勾选），delta 已 sync 为新建主 spec `openspec/specs/cross-group-forward/spec.md`（10 需求 / 30 场景）
- 验证基线：pytest **604 passed** / pyright **0 errors** / ruff 51 项（全部为 AGENTS.md 声明的六类有意偏离；当时口径，现为四类）
- 远端仓库：`gh repo create ArcDent/repeater-plugin --private` 创建并推送 `main`（此前该插件从未推送过任何远端）；manifest `urls.repository` 同步指向新地址，测试合成包名 `sending_emojis_plugin` → `repeater_plugin` 一并清理

## 关键发现

- SDK 的 `HookHandler.error_policy` 默认 `ErrorPolicy.SKIP`、系统级 Hook 超时兜底 60s（`plugin_runtime.hook_blocking_timeout_sec`）——不声明也不会中断主链
- 主程序 `EventType.ON_MESSAGE` 事件分发代码被注释（`arcwsl:~/github/REFERENCE/MaiBot/src/chat/message_receive/bot.py:846`），是死事件；普通消息观测的活口是 `chat.receive.before_process` hook
- hook 消息解析失败打 debug、webhook 上报形态不认识打 warning——两个口径相反且各有理由，详见 AGENTS.md 复读域条目
- 主程序多模态回填缺口（原「问题 B」）：`images` 表缺 hash 时麦麦识图失败，取证素材已齐（3 个失败 hash、`images` 表内 2203 EMOJI / 3405 IMAGE 计数），可作给主程序提 issue 的材料
- 改配置语义必须四源同改（config.py hint、config.toml 注释、README、AGENTS.md）；改版本号必须与 `_manifest.json` 同源
- openspec delta 的 MODIFIED 块不能改名 Scenario 标题（`validate --strict` 判漏抄）；sync 到主 spec 时新建 capability 要保留 delta 的 `## Purpose`
- 生产日志排查入口（arclinux）：`ssh arclinux`，日志落盘 `/home/arc/maimai/MaiBot/data/MaiMBot/logs/app_YYYYMMDD_HHMMSS.log.jsonl`（jsonl，字段 `event`/`logger`/`timestamp`；容器时区 UTC，本地需 +8；同一条日志双写是主程序日志配置现象）
- 结构与规则真源见 `AGENTS.md`（软链 `CLAUDE.md`），术语见 `CONTEXT.md`，本文件不重复维护
