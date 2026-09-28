## Context

动机见 proposal.md Why。工程事实（2026-09-27 arclinux 实录 + 代码核查）：

- `forward.request_enabled` 是**外层总闸**：`plugin.py` 的 Hook 同步段（`request_enabled=false` 时跳过 `runtime.observe_source_message`）与转发 Tool handler（返回不可用）两处短路
- `forward.trigger_source_planner` 是**内层细档**：`forwards/request/source/trigger.py` 三处判定（入口筛选 88 行、就绪后 161 行、等待窗口轮询守卫 207 行），只影响入口提示，Tool 不受其约束
- `forward.view_failure_fallback_threshold` 只作用于请求路径的查看资格降级（`forwards/request/config.py` 适配层透传 + 查看判定处计数）
- 生产实录：入口提示 8 次全部零响应；转发 Tool、查看、授权、投递全程 0 次；富摘要媒体占位当前无任何日志记录

## Goals / Non-Goals

**Goals:**

- `ForwardSection` 配置项 18 → 16，删除的两个字段各自有充分的功能重叠或零使用证据
- 入口提示固化开启后，请求路径的开关语义收敛为 `request_enabled` 一个旋钮，行为面在 delta spec 中显式锁定
- 查看失败 → 摘要降级的路径不再有计数分支（代码分支减少，行为收紧为「首次失败即降级」）
- 富摘要媒体占位计数零新增配置、零行为影响，仅一条 info 日志

**Non-Goals:**

- 不砍 planner 决策模式整链、不砍请求转发整包、不动 `forwards/texts.py` 节点摘要兜底（本轮阶段一共识已锁定，见 proposal What Changes「明确不动」）
- 不改写注入文本中的占位（按主程序原文保留是既定规则，问题 B 的根因在主程序 `images` 表写入侧）
- 不动复读 / 表情回应两域的任何配置与行为

## Decisions

### D1 删除 `trigger_source_planner`，入口提示固化开启

- `forwards/request/config.py::adapt_forward_config` 不再透传该字段（或直接以 `True` 常量喂给运行时）；`trigger.py` 三处 `not config.trigger_source_planner` 判定删除
- 轮询守卫（`_wait_until_message_ready` 内）保留 `_is_unloading` 检查，只删配置项检查——卸载中止语义不受删除影响
- 备选「砍 `request_enabled` 保 `trigger_source_planner`」否决：后者管不了 Tool，「只关请求路径不动自动路径」将不可能（域总闸会连带关掉自动转发）；备选「两个都留」与精简出发点矛盾

### D2 删除 `view_failure_fallback_threshold`，查看失败即摘要降级

- 查看判定处不再计数，一次失败即走摘要降级（等效阈值恒为 1，取用户「直接用摘要」的决策）；`config.py` 适配层同步删除字段
- 保留「查看成功用全文、查看失败用摘要」的两级材料结构，只消灭阈值分支
- 备选「字段写死 2」否决：保留计数状态机却去掉调节意义，与删除的初衷不合；备选「连摘要降级一并删、失败即拒绝」否决：丢失唯一的兜底路径

### D3 占位观测：就绪处一条独立 info + 纯函数计数

- 新增纯函数 `count_rich_summary_placeholders(text) -> int`（放 `forwards/summary.py`，与 `extract_rich_summary` 同模块）：统计 `[图片]` / `[表情]` / `[嵌套转发]` 出现次数；词表从 `forwards/nodes.py` 的 `SEGMENT_PLACEHOLDERS` 取值 + `[嵌套转发]` 字面量，单处定义
- `plugin.py` 编排任务就绪处（拿到富摘要、掷骰之前）记 info，文案含计数字段；不动注入成功日志的字段集
- 备选「注入成功日志加 `placeholders=N` 字段」已在本轮被用户否决（信息挤在一行、与回执字段混排）

### D4 删字段的配置兼容

- pydantic v2 模型默认 `extra='ignore'`，旧 `config.toml` 残留两行预期被忽略而非报错；tasks 中以「残留字段可正常加载」用例固化该行为
- `config.toml` / README / AGENTS.md 四源同删；`config_version` 1.3.0 → 1.4.0、`_manifest.json` 与 CHANGELOG 同步

## Risks / Trade-offs

- [旧配置残留两行在新版解析失败] → 实现期验证 `PluginConfigBase` 的 `extra` 行为并补兼容用例；若基类为 forbid，tasks 追加加载期容错（读不到就按默认）而不是让部署崩
- [删 trigger 判定时误删卸载守卫] → 只删配置检查、显式保留 `_is_unloading` 分支，测试替身覆盖卸载路径
- [入口固化开启，嫌 planner 调用吵的用户失去细粒度开关] → proposal 已声明 BREAKING 语义；`request_enabled=false` 可整路径静默；日级 8 次调用的开销实测可忽略
- [主程序改占位文案导致计数失真] → 计数只进日志、不影响任何行为分支；词表漂移时表现为计数偏低，配合 AGENTS.md 的词表记录人工同步
- [查看失败即降级收紧请求路径语义（连续 2 次 → 首次即降级）] → 该路径全天 0 次使用；降级链路本身已有测试覆盖，语义变化在 delta spec 显式锁定

## Migration Plan

1. 本地实现 + 单测（含删字段回归与占位计数用例）+ 全量校验（pytest / pyright / ruff 六类偏离不变）
2. 部署 arclinux：rsync 新版本（排除 `config.toml`），`config.toml` 删除两行（或保留、由基类忽略），重启容器
3. 人工验证：源群发合并转发 → 入口提示照常触发（日志 `source Planner 已触发`）→ 自动路径不受影响；有权重时观察占位 info 出现
4. 回滚：恢复两个字段即可回到 0.7.1 行为（默认值与固化值一致，无状态迁移）

## Open Questions

（无。占位计数 info 的最终文案在 tasks 组 3 定稿。）
