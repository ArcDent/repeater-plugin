# Tasks: simplify-forward-config-and-placeholder-log

> 阶段三细化版（阶段四执行前重组为可独立收口的 4 组）。章节即阶段四执行组，组内按依赖序排列。seam 一律取公共接口边界（模块导出 / 配置模型 / 测试替身入口），验收可独立执行；每组交付时全量测试可绿。

## 1. 删除两个字段（全链收口）

> 配置模型 → 默认配置 → 运行时适配 → 判定删除 → 全部测试引用点，一次收口；本组结束时全量测试必须绿。

- [x] 1.1 `config.py` 的 `ForwardSection` 删除 `trigger_source_planner` 字段，`order` 在其后的字段全部前移重排连续
  - seam：`config.ForwardSection`（模块导出类）
  - 验收：`tests/test_config.py` 断言 `hasattr(cfg.forward, "trigger_source_planner")` 为 False
- [x] 1.2 `config.py` 的 `ForwardSection` 删除 `view_failure_fallback_threshold` 字段，`order` 同上重排
  - seam：`config.ForwardSection`
  - 验收：`hasattr(cfg.forward, "view_failure_fallback_threshold")` 为 False；`ForwardSection.model_fields` 总数 16
- [x] 1.3 `config.toml` 的 `[forward]` 段删除两行及其中文注释，其余行值与 `config.py` 默认逐项一致
  - seam：`config.toml` + `tests/test_config.py` 默认值一致性用例
  - 验收：tomllib 解析的键集合与 `ForwardSection` 一致，无两字段残留
- [x] 1.4 版本三件套：`config.PluginSection.config_version` 1.3.0 → 1.4.0、`_manifest.json` 的 `version` 同步、`CHANGELOG.md` 增条目（标注两个 BREAKING 字段删除与占位观测新增）
  - seam：`config.PluginSection.config_version` 与 `_manifest.json`
  - 验收：`tests/test_config.py` 断言新值；manifest 与 CHANGELOG 首条版本互校；capabilities 数量仍为 11
- [x] 1.5 `forwards/request/config.py`：`ForwardRuntimeConfig` 删除两个字段，`adapt_forward_config` 不再读取透传
  - seam：`forwards.request.config.adapt_forward_config`
  - 验收：适配结果结构不含两字段；无残留 `getattr` 读取
- [x] 1.6 `forwards/request/source/trigger.py` 删除入口筛选与就绪后两处 `not config.trigger_source_planner` 判定（约 88 / 161 行）
  - seam：`forwards.request.source.trigger.SourceTriggerService`
  - 验收：原「开关关闭不触发」用例改为「恒定触发」；卸载（`_is_unloading=True`）仍不触发，用例显式覆盖
- [x] 1.7 `forwards/request/source/trigger.py` 等待窗口轮询守卫（约 207 行）删除配置检查，**保留** `_is_unloading` 检查
  - seam：`forwards.request.source.trigger._wait_until_message_ready`
  - 验收：等待期间卸载仍中断轮询返回 False
- [x] 1.8 查看资格判定处删除 `view_failure_fallback_threshold` 计数：一次查看失败（消息不可见或内容取不到）即用摘要降级，删除计数状态与重试分支
  - seam：查看资格判定（`forwards/request/source/view_flow.py` 及请求编排调用处）
  - 验收：原 `test_tool_uses_configured_view_failure_fallback_threshold` 改写为「首次失败即降级」；「查看成功用全文」路径不回归
- [x] 1.9 `tests/forwards/request/support.py` 的 `build_plugin` 等替身入口删除 `trigger_source_planner` / `view_failure_fallback_threshold` 两个关键字参数与透传
  - seam：`tests/forwards/request/support.py::build_plugin`
  - 验收：替身签名不再暴露两参数
- [x] 1.10 清理全部测试引用点：`tests/test_config.py`（两字段断言改不存在、字段清单表）、`tests/forwards/request/test_config_adapter.py`、`tests/forwards/request/source/test_trigger.py`、`tests/forwards/request/test_debug_stats.py`、`tests/forwards/request/test_request.py`、`tests/test_plugin_forward.py`（`trigger_source_planner = False` 的用例改等价行为）
  - seam：上述测试文件
  - 验收：grep 两个字段名在 `tests/` 下零命中（除兼容用例的残留行字面量）
- [x] 1.11 兼容用例：构造含 `trigger_source_planner` / `view_failure_fallback_threshold` 两行残留的 TOML 文本，经配置加载路径加载成功
  - seam：配置加载路径 + `tests/test_config.py`
  - 验收：残留行被忽略、默认值生效、无解析异常；若运行环境基类为 forbid，改为加载期容错并在 README 部署提示注明（以实测为准）
- [x] 1.12 本组收口验证：全量 `uv run --with pytest --with aiohttp pytest tests/ -q` 全绿、`uv run --with pyright --with aiohttp pyright` 零错误
  - seam：仓库级命令
  - 验收：两条命令输出满足条件

## 2. 富摘要占位观测

> 纯逻辑 → 单测 → 编排 info → 集成断言，零配置项、零行为分支改动。

- [x] 2.1 `forwards/summary.py` 新增纯函数 `count_rich_summary_placeholders(text) -> int`：统计 `[图片]` / `[表情]` / `[嵌套转发]` 出现次数；词表取 `forwards/nodes.py` 的 `SEGMENT_PLACEHOLDERS` 值 + `[嵌套转发]` 字面量，单处定义不复制
  - seam：`forwards.summary.count_rich_summary_placeholders`（既有导出模块的新函数）
  - 验收：纯函数无 SDK 依赖；`from .summary import count_rich_summary_placeholders` 可用
- [x] 2.2 新建 `tests/test_forwards_summary.py`（或追加既有文件）覆盖 2.1：含 2 处占位 → 2；无占位 → 0；空串 / `None` / 非字符串输入 → 0 不抛异常
  - seam：`tests/test_forwards_summary.py`
  - 验收：新用例单独可跑并全绿
- [x] 2.3 `_orchestrate_forward_delivery` 在拿到富摘要、掷骰之前记占位 info，**文案定稿**：`自动转发富摘要媒体占位 {n} 处（主程序 images 表缺 hash 时麦麦多模态降级）msg={id} group={group}`；注入成功日志字段集不动
  - seam：`RepeaterPlugin._orchestrate_forward_delivery`
  - 验收：富摘要含 2 处占位时日志含「媒体占位 2 处」；无占位时计数 0 且掷骰 / 冷却 / 投递全序与无该日志时逐项一致
- [x] 2.4 `tests/test_plugin_forward.py` 新增占位 info 断言（含 2 处 / 0 处两态）与全序回归：`get_by_id → 占位 info → 掷骰 → 直发 → 注入 → trigger → 留痕`
  - seam：`tests/test_plugin_forward.py`（集成组）
  - 验收：新增用例红→绿落地；既有直发 / 决策模式集成用例不回归

## 3. 文档同步

- [x] 3.1 `README.md`：`[forward]` 配置全字段表删除两项、新增部署提示（`config.toml` 删两行或保留由配置层忽略）与行为变化说明（入口提示固化开启、查看失败即摘要降级）
  - seam：`README.md`
  - 验收：字段表与 `ForwardSection` 逐项对齐；部署提示含两个 BREAKING 说明
- [x] 3.2 `AGENTS.md` 与 `CONTEXT.md`：转发域「转发路径开关」条目改为 `request_enabled` 单闸口径（入口提示固化开启、删除 `trigger_source_planner` 描述）；请求链路条目同步「查看失败即摘要降级」；公共节补占位观测日志口径（含文案与不计入注入成功格式的说明）；`CONTEXT.md` 的**查看资格**术语定义从「连续可重试故障达到阈值后降级使用摘要」改为「查看失败即降级用摘要」
  - seam：`AGENTS.md` 与 `CONTEXT.md`
  - 验收：与 delta spec 三条场景逐条对齐（入口固化开启 / 查看失败降级 / 占位计数可观测）；`CONTEXT.md` 无与新语义矛盾的残留定义；`README.md` 两个已删字段清零（3.1 负责）

## 4. 验证与部署

- [x] 4.1 全量校验：`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿、`uv run --with pyright --with aiohttp pyright` 零错误、`ruff check` 仅剩 AGENTS.md 声明的六类有意偏离
  - seam：仓库级命令
  - 验收：三条命令输出满足上述条件，用例数不低于改动前（实测：699 passed / 0 errors / 54 findings 全在六类内；README 计数同步 53→54）
- [x] 4.2 `openspec validate simplify-forward-config-and-placeholder-log --strict` 通过；tasks 全勾后按 `/openspec-sync-specs` 把 delta 并入 `openspec/specs/cross-group-forward/spec.md`，再 `/openspec-archive-change` 归档
  - 验收：validate 无 error/warning；归档后主 spec 含新需求「自动转发的富摘要占位观测」与改后的「请求转发链路」（实测：validate --specs 3 passed；主 spec 11 条需求，新增需求含 2 场景、请求转发链路 4 场景；归档至 `archive/2026-09-28-simplify-forward-config-and-placeholder-log/`）
- [x] 4.3 arclinux 部署与人工验证（用户执行）：rsync 新版本（排除 `config.toml`）、`config.toml` 删两行、重启容器；源群发合并转发确认入口提示照常触发、自动路径全序不受影响；有权重命中时确认占位 info 出现
  - 验收：日志仍有 `source Planner 已触发`；自动转发投递 / 注入 / 留痕全序正常；含图转发的就绪 info 带占位计数（实测 2026-09-28 10:07–10:08：入口提示触发 → 占位 info「0 处」→ 命中双目标群 → 决策选「转发」→ 投递成功 → 注入 `kind=forward` index=26 + 留痕 `kind=forward_action` index=23 → 麦麦发言；第二条 10:09 同链路触发）
