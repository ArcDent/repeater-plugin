# HANDOFF

## 最近操作（2026-09-27 晚，simplify-config 分支）

- 完成 change `simplify-forward-config-and-placeholder-log` 阶段一至四（规划 + 实现 + 双轴评审）：
  - 阶段一（grill + 生产实录排查，SSH arclinux 审全天日志）锁定两项 BREAKING + 一项观测：删 `forward.trigger_source_planner`（入口提示随 `request_enabled` 固化开启）、删 `forward.view_failure_fallback_threshold` 连计数机制（查看失败首次即摘要降级）、新增富摘要媒体占位计数 info（就绪后掷骰前，纯观测零配置）
  - 生产实录（17:00–18:37）：planner 模式试用一轮（2 票、1 次选「不转发」、1 票悬空、0 次真转发，随后切回 direct）；请求转发入口触发 8 次、模型零响应、Tool 0 调用；问题 B（`images` 表缺 hash 致麦麦多模态回填失败）定为主机侧缺口、插件无可修项（占位改为可观测，不改写）
  - 阶段四：组 1（删两字段全链：模型 / 默认配置 / 适配层 / trigger 三处判定 / 查看计数链路 / 全部测试引用点）、组 2（`count_rich_summary_placeholders` 纯函数 + 就绪 info + 两层测试）、组 3（README / AGENTS.md / CONTEXT.md 文档同步）均由子代理按 TDD 完成，Spec 轴与 Standards 轴评审均 PASS；4.1 / 4.2 前半（全量校验 + `validate --strict`）已执行
  - 产物：ADR 0012（转发域配置收敛 + 占位观测）；配置 18 → 16、`config_version` 1.4.0、manifest 0.8.0
  - 验证基线：pytest **699 passed** / pyright **0 errors** / ruff 54 项（全在 AGENTS.md 声明的六类内）；`openspec validate --strict` 通过
  - **未提交**（用户口径：部署验证通过后再提交）；tasks 4.3（arclinux 部署人工验证）未勾

- 早前同一日（已提交 `36bb7dd`）：上下文注入成功日志统一「已写入 Maisaka 上下文: kind=… text='…' group=… msg=… index=… stream=…」字段序（对齐 smart_poke_plugin 呈现，`_log_preview` 60 字截断），三域注入路径同一格式，AGENTS.md 补口径

## 历史：forward-summary-trigger-and-modes（0.7.0 / 0.7.1，已随容器部署）

- 自动转发改**摘要就绪**后触发（ADR 0011）：同步段只留门禁 + **转发源消息快照**，轮询 `get_by_id` 等 `processed_plain_text` 非空后掷骰；发送用快照字节（图片 100% 保全）、注入与 intent 用**富摘要**；新增 `forward.mode = direct|planner` 与 `auto_enabled` / `request_enabled`、`summary_timeout_seconds`
- 0.7.1 生产修复两项 Host 口径问题：就绪轮询只带消息 ID（带 `stream_id` 因同群新旧 session 恒空）；群 → 活跃聊天流优先 hook 上报的 `session_id`（`get_stream_by_group_id` 首匹配可能是陈旧会话，注入 / 触发 / 登记会落进死会话）
- 本 change 归档动作（sync delta → archive）**尚未执行**；`openspec/changes/forward-summary-trigger-and-modes/` 仍在活跃区（34/35）

## 历史：integrate-cross-group-forward（2026-09-18，已归档 0.6.0）

- 完整走完 spec-driven 工作流 `integrate-cross-group-forward`（0.6.0，ADR 0009 + 0010）：把 arclinux 上独立运行的 `papaya0481/maibot-ForwardMessagesAuto-plugin` 并入本插件，形成第三个行为域「跨群转发」。双路径并存——**自动转发**（源群收到合并转发后对每个目标群独立掷骰 `weight_send`/`weight_silence`，命中即**协议端直发** `send_group_forward_msg`，成功后在目标群注入 `forward` 内容并触发**强制发言**、在源群注入 `forward_action` 留痕）与**请求转发**（原插件完整链路整包移植到 `forwards/request/`：源群 Planner 触发、查看资格、一次性授权、顺序投递，不掷骰、不强制发言）。两条路径共用**已投递状态**；三层防护（消息防重 / 源群冷却 / 目标群冷却）；投递前用协议端 `get_group_list`（TTL 300s + 失败 30s 退避）校验目标群可用性
- 纯逻辑全部落在 `forwards/` 子包（`source`/`nodes`/`auto`/`state`/`texts` + 共享叶子模块 `forward_segments.py`），`plugin.py` 只做 Hook/Tool 编排；`get_components()` 覆写把转发 Tool 的 `chat_scope` 提到组件顶层（承载性 hack，不写就私聊可见）
- `config_version` 1.1.0 → 1.2.0（新增 `[forward]` 14 字段，默认 `enabled=false`）；manifest 0.5.0 → 0.6.0（capabilities 4 → 11）；AGENTS.md 新增「转发域」章节、README 补 `[forward]` 全字段表与旧插件迁移段、CONTEXT.md 补转发域术语与例对话
- **已部署 arclinux 并群内实测通过**：插件 v0.6.0 上线、旧插件 `papaya0481_forward-messages-auto` 置 `enabled=false`（原配置备份 `config.toml.bak-before-repeater-migration`）、`[forward]` 段填 源群 3 个（新生群/柴犬新窝/蹲饼群，去掉失效的 `747839793`）目标群 2 个（新生群/柴犬新窝）；容器内实测 `get_group_list` 返回 ok/6 群，目标群均在列表
- 已归档至 `openspec/changes/archive/2026-09-18-integrate-cross-group-forward/`（29/29 任务全勾选），delta 已 sync 为新建主 spec `openspec/specs/cross-group-forward/spec.md`（10 需求 / 30 场景）
- 验证基线：pytest **604 passed** / pyright **0 errors** / ruff 51 项（全部为 AGENTS.md 声明的六类有意偏离）
- 远端仓库：`gh repo create ArcDent/repeater-plugin --private` 创建并推送 `main`（此前该插件从未推送过任何远端）；manifest `urls.repository` 同步指向新地址，测试合成包名 `sending_emojis_plugin` → `repeater_plugin` 一并清理

## 进行中

- change `simplify-forward-config-and-placeholder-log` 实现完成（`simplify-config` 分支，四工件 + ADR 0012），**未提交**（用户口径：部署验证通过后再提交）；tasks 4.3 未勾
- change `forward-summary-trigger-and-modes` 尚未归档（34/35，差 5.3 人工验证的勾选）

## 下一步（用户手动）

1. 部署 arclinux 验证（tasks 4.3）：rsync 新版本（排除 `config.toml`）、删 `trigger_source_planner` / `view_failure_fallback_threshold` 两行、重启容器；源群发合并转发确认入口提示照常触发（`source Planner 已触发`）、自动路径全序正常、含图转发的就绪日志出现「自动转发富摘要媒体占位 N 处」
2. 验证通过后：一个 commit（删字段 + 占位观测 + 文档 + 规划工件）；`/openspec-sync-specs` 合并 delta → `/openspec-archive-change` 归档
3. 问题 B 根因：给主程序提 issue 的素材已齐（3 个失败 hash、18:35 时间点、`images` 表内 2203 EMOJI / 3405 IMAGE 计数）
4. 观测自动转发的线上表现（沿用）：`[forward]` 权重与冷却按实际刷屏情况调；日志按「自动转发」前缀过滤
5. 若计划提交 MaiBot 插件中心（要求**公开**仓库），需把 `ArcDent/repeater-plugin` 在 Settings 里改为 public

## 关键发现

- **旧插件转发失败的根因**（0.6.0 排查）：目标群已不在机器人群列表，但 MaiBot 数据库仍留 NapCat 时代的陈旧会话；插件按群号解析到该 stream 后，Host 出站路由匹配不到 SnowLuma 网关绑定，回落 `legacy.send.qq` 后备驱动而失败，且失败被归一成无 `error` 字段的结果、只报「未知错误」。教训：**投递前必须校验群可用性，且直发比走 Host 发送链更能与路由解耦**（ADR 0010）
- `chat_scope` 只在 Runner 读取的**组件声明顶层**生效：SDK 的 `@Tool(chat_scope=…)` 只落进 `metadata`，Host 顶层优先 → 必须靠 `get_components()` 覆写补齐，否则 Tool 在私聊可见（ADR 0009 相关的承载性 hack）
- SDK 的 `HookHandler.error_policy` 默认 `ErrorPolicy.SKIP`、系统级 Hook 超时兜底 60s（`plugin_runtime.hook_blocking_timeout_sec`）——不声明也不会中断主链，比原插件显式声明的 3~6s 更宽松
- `forwards/request/` 必须保持「不 import `forwards/nodes.py`」：后者依赖 `repeats.py`，会让 `import forwards.request` 在裸目录下失败；共享解析核心走无依赖的叶子模块 `forwards/forward_segments.py`。`forwards/summary.py` 同理不被 request 侧引用（新增占位计数后仍成立）
- 主程序 `EventType.ON_MESSAGE` 事件分发代码被注释（`arcwsl:~/github/REFERENCE/MaiBot/src/chat/message_receive/bot.py:846`），是死事件；普通消息观测的活口是 `chat.receive.before_process` hook
- hook 消息解析失败打 debug、webhook 上报形态不认识打 warning——两个口径相反且各有理由，详见 AGENTS.md 复读域条目
- 改配置语义必须四源同改（config.py hint、config.toml 注释、README、AGENTS.md）；改版本号必须与 `_manifest.json` 同源
- openspec delta 的 MODIFIED 块不能改名 Scenario 标题（`validate --strict` 判漏抄）；sync 到主 spec 时新建 capability 要保留 delta 的 `## Purpose`
- 生产日志排查入口（arclinux）：`ssh arclinux`，日志落盘 `/home/arc/maimai/MaiBot/data/MaiMBot/logs/app_YYYYMMDD_HHMMSS.log.jsonl`（jsonl，字段 `event`/`logger`/`timestamp`；容器时区 UTC，本地需 +8；同一条日志双写是主程序日志配置现象）
- 结构与规则真源见 `AGENTS.md`（软链 `CLAUDE.md`），术语见 `CONTEXT.md`，本文件不重复维护
