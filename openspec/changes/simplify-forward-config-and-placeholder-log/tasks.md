## 1. 配置骨架（删字段）

> 阶段四执行组 1，无前置依赖。先行删字段，后续测试替身与文案按新字段集编写。

- [ ] 1.1 `config.py` 的 `ForwardSection` 删除 `trigger_source_planner` 与 `view_failure_fallback_threshold` 两个字段，其余字段 `order` 重排连续
  - seam：`config.ForwardSection`（模块导出类）
  - 验收：`tests/test_config.py` 断言两个字段不再存在（`hasattr` 为 False）；字段数 18 → 16
- [ ] 1.2 `config.toml` 的 `[forward]` 段删除两行及其中文注释，与 `config.py` 默认值逐项一致
  - seam：`config.toml` + `tests/test_config.py` 默认值一致性用例
  - 验收：tomllib 解析的字段集合与 `ForwardSection` 一致；无残留两字段
- [ ] 1.3 版本升级：`plugin.config_version` 1.3.0 → 1.4.0、`_manifest.json` 的 `version` 同步、CHANGELOG 增条目（标注 BREAKING 两项）
  - seam：`config.PluginSection.config_version` 与 `_manifest.json`
  - 验收：`tests/test_config.py` 断言新值；`_manifest.json` 与 CHANGELOG 首条版本互校；capabilities 数量不变（11）
- [ ] 1.4 扩展测试替身：`tests/forwards/request/support.py` 的 `build_plugin` 等入口删除 `trigger_source_planner` / `view_failure_fallback_threshold` 参数及透传
  - seam：`tests/forwards/request/support.py`
  - 验收：替身签名不再暴露两参数；既有用例改调用点后全绿

## 2. 运行时适配与删除判定

> 阶段四执行组 2，依赖组 1 的字段删除（适配层与运行时按新字段集编译）。

- [ ] 2.1 `forwards/request/config.py::adapt_forward_config` 删除两个字段的透传（`trigger_source_planner` 不再从插件配置读取；运行时拿到的配置结构不含两字段）
  - seam：`forwards.request.config.adapt_forward_config` / `ForwardRuntimeConfig`
  - 验收：`tests/forwards/request/test_config_adapter.py` 删两字段断言，改断言入口行为不依赖两字段
- [ ] 2.2 `forwards/request/source/trigger.py` 删除三处 `trigger_source_planner` 判定（入口筛选 / 就绪后 / 等待窗口轮询守卫），**保留** `_is_unloading` 检查
  - seam：`forwards.request.source.trigger`（SourceTriggerService）
  - 验收：`tests/forwards/request/source/test_trigger.py` 原「开关关闭不触发」用例改为「恒定触发」；卸载中止用例显式保留并断言
- [ ] 2.3 查看资格判定处删除 `view_failure_fallback_threshold` 计数：查看失败一次即用摘要降级，无阈值分支
  - seam：查看资格判定（`forwards/request/source/view_flow.py` 与请求编排调用处）
  - 验收：`tests/forwards/request/test_request.py` 原 `test_tool_uses_configured_view_failure_fallback_threshold` 改为「查看失败即降级」断言；`tests/forwards/request/test_debug_stats.py` 相关用例同步
- [ ] 2.4 `forwards/summary.py` 新增纯函数 `count_rich_summary_placeholders(text)`：统计 `[图片]` / `[表情]` / `[嵌套转发]` 出现次数，词表从 `forwards/nodes.py` 的 `SEGMENT_PLACEHOLDERS` 取值 + `[嵌套转发]` 字面量
  - seam：`forwards.summary.count_rich_summary_placeholders`（既有导出模块的新函数）
  - 验收：新建 `tests/test_forwards_summary.py` 用例：含 2 处占位 → 2；无占位 → 0；空串 / 非字符串输入 → 0 不抛异常

## 3. plugin.py 编排与兼容

> 阶段四执行组 3，依赖组 2 的纯逻辑与运行时。

- [ ] 3.1 `_orchestrate_forward_delivery` 就绪处（拿到富摘要、掷骰之前）记一条 info，含富摘要媒体占位计数；注入成功日志字段集不动
  - seam：`RepeaterPlugin._orchestrate_forward_delivery`
  - 验收：`tests/test_plugin_forward.py` 断言富摘要含占位时 info 含计数；无占位时计数为 0 且行为与无该日志一致；掷骰 / 投递时序不受影响（回归既有全序断言）
- [ ] 3.2 兼容用例：旧 `config.toml` 残留 `trigger_source_planner` / `view_failure_fallback_threshold` 两行时可正常加载（按 pydantic 基类 `extra` 行为处理，预期忽略）
  - seam：配置加载路径 + `tests/test_config.py`
  - 验收：构造含两残留行的 TOML 文本加载成功；若基类行为为 forbid，改为加载期容错并在 README 部署提示注明

## 4. 文档同步

> 阶段四执行组 4，依赖组 1-3 完成。

- [ ] 4.1 `README.md`：`[forward]` 全字段表删除两项、版本段补 BREAKING 说明与部署提示（`config.toml` 删两行）
- [ ] 4.2 `AGENTS.md`：转发域「转发路径开关」条目改为 `request_enabled` 单闸口径（入口固化开启）；请求链路条目同步「查看失败即摘要降级」；公共节补占位观测日志口径
  - 验收：与 delta spec 两条需求逐条对齐；`CONTEXT.md` 不动（无术语变化）的理由记录在 CHANGELOG

## 5. 验证与部署

> 阶段四执行组 5，依赖组 4。5.3 为人工线上步骤，由用户执行。

- [ ] 5.1 全量校验：`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿、`uv run --with pyright --with aiohttp pyright` 零错误、`ruff check` 仅剩 AGENTS.md 声明的六类有意偏离
- [ ] 5.2 `openspec validate --strict` 通过；tasks 全勾后按 `/openspec-sync-specs` 合并 delta 到主 spec、`/openspec-archive-change` 归档
- [ ] 5.3 arclinux 部署与人工验证：rsync（排除 `config.toml`）、`config.toml` 删两行、重启；源群发合并转发 → 入口提示照常触发、自动路径不受影响；观察占位 info 出现
  - 验收：`source Planner 已触发` 日志照旧；自动转发投递全序回归；含图转发的就绪 info 带占位计数
