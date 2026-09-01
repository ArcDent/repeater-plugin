# 修复上下文注入的功能性缺陷并收敛

## Why

上下文注入（ADR 0003）落地时存在一处致命缺陷与多处可靠性缺陷：插件 manifest 未声明任何能力，主程序授权层（`AuthorizationManager.check_capability`）会拒绝 `maisaka.context.append`、`chat.get_stream_by_group_id`、`config.get` 三项调用——**注入功能在真实环境从未可用**，测试全绿只因 fake SDK 不模拟授权。此外聚合 flush 依赖后续上报触发、flush 失败后去重键占坑导致注入永久丢失、去重窗口 0 值语义反转等问题需要一并修正。全部结论已对最新 MaiBot 源码（`390637a60`）核实。

## What Changes

- `_manifest.json` 补能力声明：`maisaka.context.append`、`chat.get_stream_by_group_id`、`config.get`（能力名已对照主程序 capability registry 核实）
- stream 查询失败不再缓存空结果：主程序会话懒创建，失败会自愈，下次上报重查
- 新增常驻 flush 循环：注入开启期间每 5 秒 tick（硬编码，不加配置项），聚合窗口到期即注入，不再依赖「恰好有下一条上报」；事件驱动 flush 保留，靠 `due()` 的取出即移除语义保证不双发
- flush 失败时回滚该聚合覆盖的去重键（聚合需记录各回应者 QQ），后续上报可重新聚合注入；不做重试队列，`注入上限` 兜底
- **BREAKING**（配置语义）：`dedup_ttl_seconds = 0` 从「永久去重」改为「不启用去重」，`config.py` hint 与 `config.toml` 注释同步
- 显示名解析加一次性标记：查过一次（无论空否）即定型，配置热更新时重置
- 新建 ADR 0004（定时 flush 兜底 + 失败回滚），ADR 0003 Consequences 补记 guard 拦截不受 scope 约束的 rationale（行为不变）
- 删除 `docs/superpowers/plans/`（1175 行过程文档，功能已由 ADR 0003 与测试覆盖）

## Capabilities

### New Capabilities

- `context-injection`: 上下文注入功能的可靠性与语义契约——能力授权前置条件、注入时机（事件驱动 + 定时兜底）、flush 失败恢复、去重窗口语义（含 0 值）、stream 查询与显示名解析的缓存策略。项目尚无主 spec，本 change 首建该 capability 的完整需求集。

### Modified Capabilities

（无——项目尚无既有主 spec）

## Impact

- 代码：`plugin.py`（flush 循环、stream 缓存、显示名标记、回滚调用）、`reactions.py`（`InjectionState`：失败回滚、dedup 0 语义、聚合记录 QQ）、`config.py` + `config.toml`（hint/注释）、`_manifest.json`（capabilities）
- 测试：`tests/test_plugin.py`、`tests/test_reactions.py` 新增覆盖（授权无法在 fake 层模拟，以 manifest 内容断言代替）
- 文档：`docs/adr/0004-*.md` 新建、`docs/adr/0003-*.md` 追记、`docs/superpowers/plans/` 删除（`CONTEXT.md` 术语已在阶段一更新）
- 外部接口：协议端调用与 webhook 形态零变化；主程序侧仅消耗已注册的既有能力
