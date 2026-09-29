## Context

范围见 proposal.md。主规格 cross-group-forward 含 11 条需求；全部以 REMOVED delta 退役。现行代码还包含摘要就绪后投递、目标群决策票、群到活动会话学习等已实现机制，不能只按主规格文字删除而遗漏这些入口。

本项目有用户未提交的 tests/test_plugin_forward.py 修改和 ADR 0013 调研，必须保留审查依据。另有 CLI 可见的 forward-by-id-and-runtime-consolidation 草案，与本次完整删除方向冲突，不实施也不擅自清除。

## Goals / Non-Goals

**Goals:** 运行包零转发入口、零转发配置、零转发依赖；复读和表情回应行为不变；通过组件、配置和消息路径回归证明退役。

**Non-Goals:** 保留 disabled 壳、重定向旧工具到新插件、清理线上历史账本、修改新插件代码、改写历史 ADR 或已归档 OpenSpec。

## Decisions

### D1 删除整个转发域而非配置关闭

删除 forwards/**，plugin.py 内所有转发 import、字段、常量、Hook、Tool、自动/请求运行时、摘要轮询、决策票、群列表刷新与转发专属注入/日志。沿生命周期调用逐一清理：构造、加载、热更新、卸载、启动自检及后台任务跟踪都不能再触及转发。

get_components 若只为两个转发 Tool 修补 chat_scope，应随 Tool 删除；若有其他真实用途，则只保留剩余用途。不创建旧工具兼容别名或隐藏开关。

### D2 按依赖保留共享设施

保留仍供复读/表情回应使用的 _call_protocol、HTTP 会话、上下文追加、日志预览、配置读取、通用后台任务管理和群流解析。尤其 maisaka.context.append、chat.get_stream_by_group_id、config.get、chat.receive.before_process 仍为剩余域使用，不能随转发删除。aiohttp 仍用于 webhook/协议端，不是转发专属依赖。

manifest 中候选删除项为 maisaka.proactive.trigger、chat.get_group_streams、chat.open_session、message.get_by_id、send.forward、maisaka.planner.before_request、maisaka.planner.after_response；最终以剩余调用图核查，不能未经验证保留无用授权或删掉实际消费者能力。注入函数移除转发前缀/分支时保持复读与表情回应既有日志字段。

### D3 配置与升级

删除 ForwardSection、PluginConfig.forward、默认 [forward] 与全部 UI 元数据；清理构造和默认配置测试。依据 SDK 配置版本机制提升配置版本，生成的新配置不再有 forward；旧配置中剩余两域的用户值必须保留。允许 SDK 正常忽略旧未知键，但不能为旧键新增可执行兼容逻辑。默认配置中不留空 forward 段。

发布版本、配置版本、manifest 和当前说明同步；不修改尚未发布的业务版本作为本次纯规划的副作用。

### D4 测试退役与保留证明

删除 tests/test_plugin_forward.py、tests/test_forwards_*.py、tests/forwards/** 及仅用于该域的 fixtures/fake SDK 方法；混合测试文件只删转发断言，保留共用 fake 与其协议正确性测试。类型检查配置移除 forwards 检查路径。

新增轻量退役回归：组件清单无转发 Hook/Tool；无 forward 默认配置及 UI 字段；普通合并转发进入消息路径不触发摘要查询、转发发送或 proactive；携带旧 forward 配置也不能恢复功能。保留复读非候选判定所需的 forward 消息类型样例——这是剩余域的输入边界，不是转发实现残留。

执行前保留用户测试差异的可审查记录，不通过 reset/stash 抹掉变化。已由用户要求完整删除的旧域测试可以在实现中退役，但先从中提取仍适合新插件的行为要求；不机械迁移已经与新语义冲突的协议直发断言。

### D5 文档和规格边界

README、AGENTS、CONTEXT、manifest 描述及 HANDOFF 当前操作指引恢复为两域；移除转发命令、配置表、架构树和现行规则，仅保留简短独立插件迁移提示。CLAUDE 始终是 AGENTS 的软链接，不能写成第二份规则副本。

历史 CHANGELOG、已接受的历史 ADR 与 openspec/changes/archive 不删除，保留用于解释旧版本；用新退役说明明确其非当前支持范围。ADR 0013 草案不改写。主 cross-group-forward 规格在实现完成并显式 sync/archive 时按全部 REMOVED 需求清空并删除 capability，不在规划阶段提前删除。context-injection、message-repeat 无需求变化，不创建虚假的修改 delta。

当前存在的旧草案 forward-by-id-and-runtime-consolidation 只记录冲突，不覆盖、不自动归档；后续由用户处理，不应同时实施。

### D6 残留检查的精确定义

验收不是全仓 grep forward 必须为零。运行代码中的旧模块依赖、发送/工具/Hook 名称、配置与专属状态必须为零；历史材料、移除规格、迁移提示与剩余域排除合并转发的测试样例是允许残留。不得为了清空关键词而破坏非候选测试或删改历史事实。

## Risks / Trade-offs

- [误删共享权限与发送设施] → 先核查剩余消费者，全量 pytest 与生产 pyright 验证。
- [测试数量减少掩盖回归] → 转发专属测试有清单，保留两域测试并新增退役断言；不能以总测试数减少当作通过证据。
- [主规格滞后于代码] → 除 11 个 REMOVED 外按 plugin.py 生命周期和全部组件清单核验。
- [新旧部署重复] → 切换先停旧功能再开新插件，回滚先停新插件；不自动执行线上动作。
- [旧配置污染升级] → 覆盖旧配置迁移，确认剩余用户值不重置且转发字段无效。

## Migration Plan

1. 在两个项目独立完成实现及本地验证，旧项目全量测试与类型检查通过。
2. 新插件通过最低发送类型和活跃/冷会话上下文验收后，再执行部署切换；默认新插件关闭。
3. 用户备份旧配置和账本，部署本项目退役版本；确认没有旧转发组件、后台任务或授权残留。
4. 用户启用新插件并配置白名单；不自动导入旧账本、不声称新账本覆盖旧历史。
5. 如需回滚，先停新插件再由用户恢复旧插件版本；不删除任一方历史数据。
