# Proposal: repeat-and-rebrand

## Why

插件目前只覆盖「表情回应跟风」一个群聊行为，而群聊里最高发的社交现象是**复读**——同一句话被多个人接龙。麦麦对复读完全缺席，既不跟也不闹。聚合「普通对话复读 + 表情回应跟风」两个行为后，插件从单一功能升级为群聊氛围参与插件，名实相符（改叫复读插件）。

## What Changes

- 新增**复读**功能：滚动时间窗口内，同一**复读文本**（宽松归一比对）被累积达阈值（默认按不同发送者人头数，可切换为逐条计数）后，麦麦掷骰三出口互斥决策——**复读**原文（发链内最后一条的原始文本）、**打断**（只发变体池随机一条短文本，不复读原文）、沉默（什么都不发）
- 新增消息观测通路：`chat.receive.before_process` hook 新挂 observe 模式 handler 做复读检测，主链零阻塞（决策依据与备选见 `docs/adr/0005`）
- 新增复读/打断发送：走 SDK 标准能力 `ctx.send.text`（消息进麦麦上下文，麦麦后续对话知道自己复读过什么），stream_id 复用现有群号缓存
- 新增**链终结**防护：链达标掷骰后即终结，按（群，复读文本）记 TTL 标记，防止一轮群友复读里麦麦多次出声；TTL 过期后新一轮可再触发（与表情槽同构）
- 新增门禁：`scope.ignore_senders` 忽略发送者名单（防其他 bot 刷屏带偏）、复读独立群冷却（与贴表情冷却分开计时）
- 决策后自然延迟再发送（复用 `behavior.delay_min/max` 节奏），避免机械秒回
- **BREAKING** 项目全面改名：`sending-emojis-plugin` → `repeater-plugin`，manifest id 变更为 `arcdent.repeater-plugin`（对 MaiBot 而言是新插件），目录名、类名（`SendingEmojisPlugin`/`SendingEmojisConfig` → `RepeaterPlugin`/`RepeaterConfig`）、README、AGENTS.md 同步改名；已部署实例需按新 id 重装
- 表情回应跟风、反义回应、上下文注入的行为**零变化**

## Capabilities

### New Capabilities
- `message-repeat`: 复读检测（窗口、归一、计数口径、消息资格）与三出口决策（复读/打断/沉默）、链终结、门禁与冷却的行为需求

### Modified Capabilities

（无——表情回应跟风与上下文注入的既有需求不变；复读/打断消息走主程序发送链天然进上下文，不经上下文注入体系，`context-injection` 无需修改）

## Impact

- **代码**：`plugin.py`（新 hook handler、复读编排与发送）、纯逻辑层新模块（复读文本归一、复读链状态机、三出口决策——独立于 `reactions.py` 的表情回应逻辑，具体切分见 design）、`config.py` 与 `config.toml`（新增 `repeat` 配置节、`scope.ignore_senders`）
- **`_manifest.json`**：id/name/description 改名，capabilities 追加新 hook 观测声明
- **测试**：`tests/fake_maibot_sdk.py` 需补 `ctx.send.text` 替身与 hook observe 语义；新增复读纯逻辑单测与插件级用例
- **文档**：README 重写定位、AGENTS.md 更新结构与规则、CONTEXT.md（阶段一已更新）、`docs/adr/0005`（已写）
- **部署**：MaiBot 容器内插件目录需同步改名重装；仓库/GitHub 远端重命名由用户手动执行
- **外部接口**：协议端调用与 webhook 形态零变化
