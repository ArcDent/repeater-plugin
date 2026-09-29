## Why

转发功能改由独立的“麦麦搬运喵”承担，本插件恢复为复读与表情回应两个行为域。完整移除转发而非隐藏开关或保留兼容壳，避免重复触发、过度授权与无效配置。

## What Changes

- **BREAKING**：删除 `forwards/` 整包及 `plugin.py` 中全部转发 Hook、Tool、自动/请求路径、状态、延迟、摘要轮询、注入、群列表刷新、决策票与初始化/卸载逻辑。
- **BREAKING**：删除 `[forward]` 配置模型、默认配置、WebUI 字段及转发专属 manifest 能力；旧字段不得重新启用功能。不提供转发兼容入口或动态代理。
- 删除转发专属测试与替身，新增“组件不再暴露转发、消息无转发副作用”的退役回归测试；共享代码按剩余调用方保留。
- 删除现行文档、术语与规则中的转发功能说明，保留简短迁移指引；历史 ADR/归档/CHANGELOG 保留作为历史，不当作现行行为。
- 主规格 `cross-group-forward` 全部需求移除，实施验收并同步后删除空 capability；复读和表情回应规格不改变。
- 与新项目 `introduce-maimai-courier` 配套验收和切换，禁止两套转发同时启用。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `cross-group-forward`: 整项退役，移除所有现行需求；额外清除代码中已存在但尚未同步进主规格的摘要就绪、双模式、会话学习等机制。

## Impact

- 涉及 `plugin.py`、`config.py`、`config.toml`、`_manifest.json`、`forwards/**`、测试及替身、类型检查配置、README、AGENTS/CLAUDE、CONTEXT、HANDOFF 与发布说明。
- 保留复读与表情回应使用的协议 HTTP、上下文追加、群流解析、后台任务设施；不能按 forward 字符串粗暴删除共享实现。
- 本次规划保持 `tests/test_plugin_forward.py` 既有未提交变更与 ADR 0013 原样；实施时先保存可审查差异，再随旧域退役处理测试，不回滚用户改动。
- 不自动删除线上持久化数据，不执行 Git 写操作，不改写历史归档。已有 `forward-by-id-and-runtime-consolidation` 草案不实施、不覆盖，作为冲突方向在交接中注明被新方案取代。
- 新项目目录为同级 `maimai-courier-plugin`，本变更不在旧项目安装或封装新插件。
