# HANDOFF

## 最近操作（2026-09-05）

- 完整走完 spec-driven 工作流 `repeat-exit-weights-and-faster-send`（0.5.0，ADR 0007 + 0008）：复读三出口从「双概率」改归一化**出口权重** `weight_repeat`/`weight_interrupt`/`weight_silence`（默认 85/15/0，`config_version` 1.0.0→1.1.0，主程序自动重建 config.toml，旧概率值不迁移需重调）；复读发送延迟从共用 `behavior.delay_min/max`（1~6s）改复读段自有 `repeat.delay_min/max_seconds`（0.3~1.5s）；触发消息注入与延迟改 `asyncio.gather` 并发（`return_exceptions=True`），可见延迟 = max(注入 RTT, 延迟)，行为文本注入仍在直发后（历史序不变）。已归档至 `openspec/changes/archive/2026-09-05-repeat-exit-weights-and-faster-send/`，delta 已同步主 spec（`openspec/specs/message-repeat/spec.md`，需求口径全量更新）
- 默认沉默权重 0 = 链一达标必出声（复读或打断）、触发消息必被 **planner 拦截**——唯一放行口是复读群冷却压制期；要恢复「偶尔沉默」给 `weight_silence` 设正值（README/CHANGELOG 均有升级提示）
- manifest 0.4.0 → 0.5.0；CHANGELOG 0.5.0 条目（两条 BREAKING + Changed + 部署注意）；AGENTS.md/README/CONTEXT.md 已同步（CLAUDE.md 软链未拆）
- 更早（今日）：`repeat-direct-send-intercept`（0.4.0，ADR 0006，blocking 拦截 + 协议端直发 + 双注入）；0.4.0 已部署 arclinux（rsync 排除清单含 config.toml）
- 验证基线：pytest **328 passed** / pyright **0 errors** / ruff 19 项（全部为 AGENTS.md 声明的有意偏离）

## 进行中

- 无活跃 openspec change；改动未提交（工作树 13 文件 + 2 新 ADR + 归档目录）

## 下一步（用户手动）

1. 群内实测 0.5.0：三人接龙触发复读/打断，验证权重 85/15/0 的实际频率、0.3~1.5s 的发送节奏、注入与直发的时序；升级后按需重调三个权重
2. 决定是否 push `main` 至远端
3. GitHub 仓库改名 `repeater-plugin`（`git remote set-url` 同步，README 迁移段有完整清单）；改名后同步 manifest 的 `urls.repository`

## 关键发现

- 主程序 `EventType.ON_MESSAGE` 事件分发代码被注释（`REFERENCE/MaiBot/src/chat/message_receive/bot.py:846`），是死事件；普通消息观测的活口是 `chat.receive.before_process` hook
- hook 消息解析失败打 debug、webhook 上报形态不认识打 warning——两个口径相反且各有理由，详见 AGENTS.md 复读域条目
- 配置口径与实现的漂移会以「用户可感知的错误行为预期」漏出：复读三出口实现自始至终只掷一次骰，而 config hint/注释写着「两颗骰」，默认 0.6/0.15 下两种口径的打断频率差 2.5 倍（15% vs 6%）——教训是改配置语义时必须四源同改（config.py hint、config.toml 注释、README、AGENTS.md）
- openspec delta 的 MODIFIED 块不能改名 Scenario 标题（`validate --strict` 判漏抄），改名要 sync 后直接改主 spec；CHANGELOG 版本号必须与 `_manifest.json` 同源
- 结构与规则真源见 `AGENTS.md`（软链 `CLAUDE.md`），术语见 `CONTEXT.md`，本文件不重复维护
