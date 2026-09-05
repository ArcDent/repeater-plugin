# HANDOFF

## 最近操作（2026-09-05）

- 完整走完 spec-driven 工作流 `repeat-direct-send-intercept`（0.4.0，ADR 0006）：复读/打断出口改为 blocking abort 触发消息（不写库、不进上下文与 planner，杜绝双重发言）；发送改协议端直发 `send_group_msg`（只带 group_id，与 stream_id 解耦，manifest 移除 `send.text` 能力声明）；出口命中后双注入（触发消息 `repeat_trigger` 无条件补偿 abort + 行为文本 `repeat`/`interrupt` 仅直发成功后）走 `maisaka.context.append`；已归档至 `openspec/changes/archive/2026-09-05-repeat-direct-send-intercept/`，delta spec 已同步主 specs（`openspec/specs/message-repeat/spec.md`，9 需求 20 场景）
- manifest 0.3.0 → 0.4.0；CHANGELOG 0.4.0 条目（两条 BREAKING + 部署注意段）
- 更早（今日）：`repeat-and-rebrand`（表情跟风插件 → 复读插件，0.3.0）、线上修复 `E_CAPABILITY_DENIED`（capabilities 漏声明 `send.text`，能力断言改为从 `plugin.py` 源码 AST 派生，双向拦截漂移）
- `repeat-fix` 分支已合并 `main`（fast-forward 至 `02f781d`）
- 验证基线：pytest **315 passed** / pyright **0 errors** / ruff 19 项（全部为 AGENTS.md 声明的有意偏离）

## 进行中

- 无活跃 openspec change

## 下一步（用户手动）

1. 部署 0.4.0 至 arclinux 容器（流程见 README 部署段；插件 id `arcdent.repeater-plugin` 不变，覆盖后重启生效）
2. 决定是否 push `main` 至远端
3. GitHub 仓库改名 `repeater-plugin`（`git remote set-url` 同步，README 迁移段有完整清单）；改名后同步 manifest 的 `urls.repository`

## 关键发现

- 主程序 `EventType.ON_MESSAGE` 事件分发代码被注释（`REFERENCE/MaiBot/src/chat/message_receive/bot.py:846`），是死事件；普通消息观测的活口是 `chat.receive.before_process` hook（检测层 ADR 0005 沿用，出口与发送模式被 0006 取代）
- hook 消息解析失败打 debug、webhook 上报形态不认识打 warning——两个口径相反且各有理由，详见 AGENTS.md 复读域条目
- 结构与规则真源见 `AGENTS.md`（软链 `CLAUDE.md`），术语见 `CONTEXT.md`，本文件不重复维护
