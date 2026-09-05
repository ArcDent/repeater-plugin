# HANDOFF

## 最近操作（2026-09-05）

- 完整走完 spec-driven 工作流 `repeat-and-rebrand`：表情跟风插件 → **复读插件**（新增复读/打断功能 + 全面改名），已归档至 `openspec/changes/archive/2026-09-05-repeat-and-rebrand/`，delta spec 已同步主 specs（`openspec/specs/message-repeat/spec.md`，6 需求 23 场景）
- 收敛清理（两条评审遗留清零）：`GroupCooldown` 组合小类消除 `reactions.py`/`repeats.py` 冷却方法双份实现（方法体 2→1，实例仍各持独立、分开计时语义不变）；`_qq_set` 提升公共名 `qq_set`
- 验证基线：pytest **279 passed** / pyright **0 errors** / ruff **18 项**（全部为 AGENTS.md 声明的四类有意偏离）

## 进行中

- 无活跃 openspec change

## 下一步（用户手动）

1. GitHub 仓库改名 `repeater-plugin`（`git remote set-url` 同步，README 迁移段有完整清单）
2. MaiBot 容器内卸载旧 id `arcdent.sending-emojis-plugin`，按新 id `arcdent.repeater-plugin` 重装
3. 发布时 bump `_manifest.json` version 并落 CHANGELOG 日期；仓库改名后同步 manifest 的 `urls.repository`

## 关键发现

- 主程序 `EventType.ON_MESSAGE` 事件分发代码被注释（`REFERENCE/MaiBot/src/chat/message_receive/bot.py:846`），是死事件；普通消息观测的活口是 `chat.receive.before_process` hook 的 observe 模式（ADR 0005）
- hook 消息解析失败打 debug、webhook 上报形态不认识打 warning——两个口径相反且各有理由，详见 AGENTS.md 复读域条目
- 结构与规则真源见 `AGENTS.md`（软链 `CLAUDE.md`），术语见 `CONTEXT.md`，本文件不重复维护
