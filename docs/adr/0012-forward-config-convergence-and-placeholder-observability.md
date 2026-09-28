---
status: accepted
date: 2026-09-27
---

# 转发域配置收敛：入口提示固化开启、查看失败即降级，并新增富摘要占位观测

## Context

转发域（ADR 0009 / 0010 / 0011）配置项累计到 18 个，用户提出「逻辑和可配置项太多，要精简」。阶段一逐项拷问 + 生产实录（2026-09-27 arclinux 全天日志）得到两组事实：

1. **`forward.trigger_source_planner` 与 `forward.request_enabled` 层级重叠**：前者是运行时内层细档（`forwards/request/source/trigger.py` 三处判定），只管源群 Planner 入口提示；后者是插件层外层总闸（Hook 同步段 + 转发 Tool handler），同时管入口提示与 Tool 可用性。组合矩阵里唯一只有内层开关能实现的差异是「入口静默 + Tool 可见」，而当天实录：入口提示触发 **8 次、模型零响应**；转发 Tool、查看资格、授权、投递**全程 0 次**——该组合没有任何实证价值。
2. **`forward.view_failure_fallback_threshold` 只服务请求路径**（查看资格连续失败 N 次后降级用摘要，默认 2），而请求路径实测零使用；保留计数状态机与重试语义只增加分支。

同批排查暴露**问题 B**（主机侧缺口，插件无可修项）：麦麦构建多模态 prompt 时按 `emoji_hash` / `image_hash` 去主程序 `images` 表回填二进制，失败即退化为文本占位——失败 hash 不在表内（表内另有 2203 条 EMOJI / 3405 条 IMAGE），且转发卡片经协议端直发不写回主程序消息库、插件注入为纯文本，与回填无关；同类失败早于本次转发、跨群出现。插件侧此前的**可观测性为零**：富摘要里主程序留下多少媒体占位（`[图片]` / `[表情]` / `[嵌套转发]`）没有任何记录。

用户口径（阶段一决策）：砍 `trigger_source_planner`（入口固化开启，请求路径收敛为 `request_enabled` 单闸）；删 `view_failure_fallback_threshold` 连阈值计数一并删（**直接用摘要**）；占位**只加观测、不改写**注入文本（按主程序原文保留是既定规则，回填失败发生在主程序 prompt 构建时，改写无效）；planner 决策模式与请求转发整包**保留不动**（前者试用一轮 0 产出但挂起，后者保留待命）。

## Decision

1. **删除 `forward.trigger_source_planner`**：源群 Planner 入口提示随 `forward.request_enabled` **固化开启**，请求转发路径只保留 `request_enabled` 一个开关（同时管入口提示与转发 Tool）。`trigger.py` 三处配置判定删除，等待窗口轮询守卫只保留 `_is_unloading` 卸载检查。
2. **删除 `forward.view_failure_fallback_threshold` 及阈值计数机制**：查看资格校验改为「已完整查看用全文；查看失败（消息在等待窗口内不可见或内容取不到）**首次即**用摘要降级」——不计数、不重试、不拒绝投递。`view_policy.py` 与 `ViewEligibilityStore` 的失败计数状态一并删除。
3. **新增富摘要媒体占位观测**：自动转发**摘要就绪**后、掷骰之前记一条 info（`自动转发富摘要媒体占位 {n} 处（主程序 images 表缺 hash 时麦麦多模态降级）msg=… group=…`）。计数为 `forwards/summary.py` 的纯函数 `count_rich_summary_placeholders`，词表复用 `forwards/nodes.py` 的 `SEGMENT_PLACEHOLDERS` 与 `NESTED_FORWARD_PLACEHOLDER`（单处定义）。**纯观测**：不改写注入文本与 intent 材料、不新增配置项、不参与任何行为分支。
4. **明确不动**：planner 决策模式整链（`forward.mode` / `forwards/decision.py` / `decide_forward_delivery` Tool）、请求转发整包（`forwards/request/` 除上述两处判定外）、`forwards/texts.py` 的节点摘要兜底（富摘要缺失时的防御层，非死分支）。

## Considered Options

- **砍 `request_enabled` 而保留 `trigger_source_planner`**：否决——内层开关管不了 Tool，「只关请求路径、不动自动路径」将不可能（域总闸会连带关掉自动转发）。
- **两个开关都保留**：否决——与精简出发点矛盾，且唯一差异组合无实证价值。
- **`view_failure_fallback_threshold` 写死 2 保留计数**：否决——保留计数状态机却去掉调节意义；用户口径是「直接用摘要」。
- **连摘要降级一并删（失败即拒绝）**：否决——丢失唯一兜底路径。
- **占位观测并入注入成功日志的 `placeholders=` 字段**：否决——信息挤在一行、与回执字段混排；改为就绪处独立 info。
- **改写注入文本里的占位**：否决——违反「按主程序原文保留」既定规则，且不解决主程序回填失败。

## Consequences

- `ForwardSection` 配置项 18 → 16；`config_version` 1.3.0 → 1.4.0（BREAKING：旧 `config.toml` 残留两行按额外字段忽略、不报错）。
- `request_enabled` 语义扩为「入口提示 + 转发 Tool」单一闸；「入口静默 + Tool 可见」组合与「查看失败重试」语义消失（已知 BREAKING，实测零使用）。
- 问题 B 从「完全不可观测」变为「每次就绪记录一次占位计数」，可量化主程序 `images` 表缺口对注入文本的影响；根因修复归主程序侧（images 表写入覆盖），插件不改写占位。
- 挂起项素材留档：planner 试用实录（2 票、1 次选「不转发」、1 票悬空、0 次真转发；悬空票的超时 warning 因惰性清理基本打不出来）；请求入口 8 次触发零响应。将来若要砍 planner 整链，证据已齐。
