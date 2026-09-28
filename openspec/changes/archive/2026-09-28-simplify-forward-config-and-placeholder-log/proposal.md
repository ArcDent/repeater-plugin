## Why

转发域配置项累计到 18 个，其中两项被生产实录证明不值得保留：`forward.trigger_source_planner` 与 `forward.request_enabled` 是同一条链路外层总闸与内层细档的关系，功能高度重叠（差异只剩「入口静默 + Tool 可见」一种组合，而该组合没有任何实证价值）；`forward.view_failure_fallback_threshold` 只服务于请求转发链路的查看资格降级，而该路径 2026-09-27 全天实录为入口触发 8 次、查看 / 授权 / Tool / 投递全部 0 次。同时，问题 B（麦麦构建多模态 prompt 时按 `emoji_hash` / `image_hash` 回填二进制失败、主程序 `images` 表缺 hash）已定性为主机侧覆盖缺口、插件侧无可修项，但当前**完全不可观测**：富摘要里主程序留下了多少媒体占位（`[图片]` / `[表情]`），插件没有任何记录，只能靠人工翻 prompt dump。

## What Changes

- **BREAKING（配置）**：删除 `forward.trigger_source_planner`。源群 Planner 入口提示**固化开启**，请求转发路径的开关收敛为 `forward.request_enabled` 一个（外层总闸，同时管入口提示与转发 Tool）。删除后「只关入口、保留 Tool」的旋钮消失，想安静只能 `request_enabled=false` 连 Tool 一并关
- **BREAKING（配置）**：删除 `forward.view_failure_fallback_threshold` 并连带删除请求转发链路的阈值判定逻辑。查看资格失败（消息在等待窗口内不可见或内容取不到）后**直接用摘要降级投递**，不再计数重试
- 新增**占位观测**：自动路径摘要就绪时（就绪的那次 `get_by_id` 拿到富摘要后）记一条 info，含富摘要的媒体占位计数。计数为纯函数（词表复用 `forwards/nodes.py` 的 `SEGMENT_PLACEHOLDERS` 与 `[嵌套转发]`），**不新增任何配置项**；注入文本本身不改写（占位按主程序原文保留的既定规则不变）
- 明确**不动**：planner 决策模式整链（`forward.mode` / `forwards/decision.py` / `decide_forward_delivery` Tool）、请求转发整包移植（`forwards/request/` 除上述两处判定外）、`forwards/texts.py` 的节点摘要兜底（富摘要缺失时的防御层，非死分支）

## Capabilities

### New Capabilities

（无：全部变更落在既有 `cross-group-forward` 能力上。）

### Modified Capabilities

- `cross-group-forward`: 请求转发路径的开关口径（`trigger_source_planner` 删除、入口提示固化开启）、查看资格的降级口径（阈值删除、失败即用摘要）、自动转发的就绪观测口径（富摘要媒体占位计数 info）。

## Impact

- **代码**：`config.py` / `config.toml`（删两个字段，`ForwardSection` 18 → 16）、`plugin.py`（Hook 入口短路条件、就绪处新增 info）、`forwards/request/config.py`（适配层删两个字段的透传）、`forwards/request/source/trigger.py`（三处 `trigger_source_planner` 判定删除）、查看资格判定处（阈值计数删除）、`forwards/summary.py`（新增占位计数纯函数）、`tests/` 替身与用例（约 12 处引用两个字段）
- **文档**：`README.md`（`[forward]` 全字段表）、`AGENTS.md`（转发域开关与请求链路条目）、`CHANGELOG.md`；`CONTEXT.md` 不动（无术语变化）
- **兼容**：旧 `config.toml` 里残留的 `trigger_source_planner` / `view_failure_fallback_threshold` 两行——删除字段后由配置基类按未知字段处理（实现期确认 pydantic 基类的 `extra` 行为，预期忽略而非报错）
- **版本**：`config_version` 1.3.0 → 1.4.0、manifest 与 CHANGELOG 同步；升级后部署行为与 0.7.1 完全一致（入口本默认开启；查看失败降级从「连续 2 次」收紧为「首次即降级」，只影响请求路径，该路径实测零使用）
- **问题 B 边界**：占位计数让「主程序 images 表缺口对注入文本的影响」变得可量化，但根因修复在主程序侧（表写入覆盖），不在本 change 范围
