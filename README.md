<p align="center">
  <pre>
  ██████╗  ███████╗ ██████╗  ███████╗ █████╗   ████████╗ ███████╗ ██████╗
  ██╔══██╗ ██╔════╝ ██╔══██╗ ██╔════╝ ██╔══██╗ ╚══██╔══╝ ██╔════╝ ██╔══██╗
  ██████╔╝ █████╗   ██████╔╝ █████╗   ███████║    ██║    █████╗   ██████╔╝
  ██╔══██╗ ██╔══╝   ██╔═══╝  ██╔══╝   ██╔══██║    ██║    ██╔══╝   ██╔══██╗
  ██║  ██║ ███████╗ ██║      ███████╗ ██║  ██║    ██║    ███████╗ ██║  ██║
  ╚═╝  ╚═╝ ╚══════╝ ╚═╝      ╚══════╝ ╚═╝  ╚═╝    ╚═╝    ╚══════╝ ╚═╝  ╚═╝
        MaiBot Repeater Plugin — 复读 / 表情回应
  </pre>
</p>

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![MaiBot](https://img.shields.io/badge/MaiBot-1.0.0%2B-6E4AFF?style=for-the-badge)
![Version](https://img.shields.io/badge/Version-0.9.0-2F81F7?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Stars](https://img.shields.io/github/stars/ArcDent/repeater-plugin?style=for-the-badge&logo=github)

**✨ 让麦麦像群友一样接龙复读、跟着贴表情 ✨**

纯规则 + 概率的两域聚合插件：复读链一次掷骰选三出口、表情回应两级决策，不烧 token

[项目简介](#-项目简介) • [功能特点](#-功能特点) • [快速开始](#-快速开始) • [掷骰与开关](#-掷骰与开关) • [常见问题](#-常见问题) • [完整配置](#-完整配置)

</div>

---

## 🎯 项目简介

repeater-plugin 是一个 MaiBot 群聊行为插件，把两种「群友会做的事」聚合到一起：

- **复读**：群内同一句话接龙达标后，麦麦掷一次骰按**出口权重**选中恰好一个出口——把**复读文本**原样发出、发一条**打断文本**唱反调、或沉默。
- **表情回应跟风**：有人给消息贴**表情回应**后，麦麦按概率跟风贴同款，或按概率贴语义相反的**反义回应**。

> 💡 **MaiBot**：LLM 群聊智能体宿主，插件运行其上（实测 1.2.3）。
> 💡 **协议端**：SnowLuma / NapCat 等 OneBot v11 实现；复读直发与贴表情都直调协议端 HTTP API。

---

## ✨ 功能特点

| 功能 | 描述 |
|:---|:---|
| **三出口复读** | 链达标后一次掷骰：复读（85）/ 打断（15）/ 沉默（0），权重只有比例意义，严格互斥 |
| **表情跟风** | 他人消息 25%、麦麦自己消息 12% 概率跟风贴同款 |
| **反义骰** | 命中反义映射后 35% 概率贴反义（👍↔👎 等 16 对内置，可自定义） |
| **纯规则 + 概率** | 不依赖 LLM 判断，行为可控、频率可调、零 token 消耗 |
| **协议端直发** | 复读/打断经协议端 HTTP 直调，与主程序发送链解耦，只依赖群号 |
| **完全可单测** | `repeats.py` / `reactions.py` 纯逻辑层，342 条用例覆盖边界 |

---

## 📦 快速开始

前置：MaiBot ≥ 1.0.0、maibot-plugin-sdk ≥ 2.0.0、OneBot v11 协议端（SnowLuma / NapCat）、Python ≥ 3.10、`aiohttp ≥ 3.9.0`（唯一第三方依赖）。

```bash
# 1. 推送插件（目录名即插件 id，config.toml 排除避免覆盖线上配置）
rsync -av --delete \
  --exclude .git --exclude .local --exclude .serena --exclude .superpowers \
  --exclude .pytest_cache --exclude .ruff_cache --exclude __pycache__ \
  --exclude tests --exclude docs/superpowers \
  --exclude .cursor --exclude openspec \
  --exclude conftest.py --exclude pyrightconfig.json --exclude config.toml \
  ./ arclinux:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.repeater-plugin/

# 2. 部署最小配置（SnowLuma 上报带 Authorization 头，webhook.secret 留空）
ssh arclinux 'cat > /home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.repeater-plugin/config.toml << "EOF"
[webhook]
secret = ""

[protocol]
base_url = "http://maim-bot-snowluma:3000"
access_token = "<snowluma OneBot HTTP accessToken>"
self_qq = ""
EOF'

# 3. 重启主容器并验证加载
ssh arclinux 'docker restart maim-bot-core'
ssh arclinux 'docker logs --since 2m maim-bot-core | grep -E "复读插件已加载"'
```

表情回应上报需在协议端新增一个 HTTP 上报目标，指向 `http://插件容器IP:18081/maibot/emoji_reaction`（同机已有其他插件占用 18080 时端口错开即可）。

部署验证检查点：`复读插件已加载（复读 / 表情回应两域）`、`表情回应上报已监听`、三人接龙后 `复读链达标` → `已直发复读文本`、贴表情后 `表情回应触发` / `已跟风贴表情`。

---

## 🎲 掷骰与开关

**复读三出口**（达标即掷，严格互斥）：

| 出口 | 默认权重 | 行为 |
|:-----|:---------|:-----|
| **复读** `weight_repeat` | 85 | 拦截触发消息，复读文本经协议端直发，并注入麦麦上下文 |
| **打断** `weight_interrupt` | 15 | 拦截触发消息，只发打断文本池随机一条 |
| **沉默** `weight_silence` | 0 | 触发消息照常放行，麦麦自行回应 |

**防护与节奏**：

| 机制 | 默认 | 说明 |
|:-----|:-----|:-----|
| **链终结** `chain_ttl_seconds` | 1800 | 掷骰后同文本此时长内不再触发新一轮 |
| **复读群冷却** `cooldown_seconds` | 120 | 冷却期内链照常累积但不掷骰，与表情回应冷却分开计时 |
| **发送延迟** `delay_min/max_seconds` | 0.3~1.5 | 复读段自有随机延迟，与 `[behavior]` 的 1~6 秒互相独立 |

**表情回应两级骰**：

| 骰子 | 默认概率 | 未命中 |
|:-----|:---------|:-------|
| 回应骰（他人消息）`bandwagon.probability` | 0.25 | 不贴表情 |
| 回应骰（麦麦消息）`bandwagon.self_message_probability` | 0.12 | 不贴表情 |
| 反义骰 `opposite.probability`（回应后命中反义映射时） | 0.35 | 回落跟风贴同款 |

---

## 🖼️ 效果示意

```
┌─ 群聊 ───────────────────────────────────────────┐
│ 小A:  晚上吃火锅？                                │
│ 小B:  晚上吃火锅？                                │
│ 小C:  晚上吃火锅？        ← 接龙达标（阈值 3）     │
│ 麦麦: 晚上吃火锅？        ← 掷骰 85% 复读出口      │
│                                                  │
│ 小D 给「周末去哪」贴了 👍                         │
│ 麦麦: （给「周末去哪」贴 👍）  ← 25% 跟风          │
│      （给「周末去哪」贴 👎）  ← 命中反义且 35% 唱反调│
└──────────────────────────────────────────────────┘
```

---

## ❓ 常见问题

### Q: 转发功能去哪了？

**A:** 0.9.0 起跨群转发域整体移除，由独立插件 [maimai-courier-plugin](https://github.com/ArcDent/maimai-courier-plugin)（麦麦搬运喵）承担——它尚未验收、当前无可部署版本。旧 `[forward]` 配置留着不影响加载。详见 [CHANGELOG](CHANGELOG.md)。

### Q: 为什么不要开启协议端的 self-message 上报？

**A:** 复读/打断经协议端直调发送，不走主程序发送链；开启 `reportSelfMessage` 后直发消息会以普通 user 消息身份回流主程序入站链，planner 上下文出现「麦麦以 user 身份看到自己复读过」的形态污染。保持默认关闭即可。见 [ADR 0006](docs/adr/0006-repeat-direct-send-with-injection-and-intercept.md)。

### Q: 表情回应上报没反应？

**A:** 依次检查：协议端是否已新增 HTTP 上报目标指向 `http://插件容器IP:18081/maibot/emoji_reaction`；`webhook.secret` 是否与协议端对应（SnowLuma 发 `Authorization: Bearer` 头，secret 留空）；同机已有其他插件占用 18080 时确认端口不冲突。日志出现 `表情回应上报形态不认识，已忽略` 说明协议端形态变了，需补解析分支。

---

## 🔧 完整配置

<details>
<summary>🔧 完整配置字段表（config.toml，默认值与 config.py 逐项一致）</summary>

### `[plugin]`（总开关）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `true` | 总开关，关闭后两域全部停用 |
| `config_version` | `1.5.0` | 配置结构版本，由插件维护 |

### `[repeat]`（复读）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `true` | 复读独立开关，不影响表情回应 |
| `window_seconds` | `120` | 滚动窗口时长 |
| `threshold` | `3` | 窗口内计数达标值 |
| `distinct_senders` | `true` | 仅计不同发送者人头 |
| `weight_repeat` / `weight_interrupt` / `weight_silence` | `85` / `15` / `0` | 三出口权重，三者全 0 不掷骰 |
| `interrupt_texts` | 五条内置 | 打断文本变体池，全空回落内置 |
| `delay_min_seconds` / `delay_max_seconds` | `0.3` / `1.5` | 复读发送前随机等待 |
| `chain_ttl_seconds` | `1800` | 链终结标记保留时长，0 不做链终结 |
| `cooldown_seconds` | `120` | 复读专用群冷却，0 不限 |
| `max_text_length` | `200` | 超长文本不参与累积，0 不限 |
| `max_chains` | `2000` | 内存链条数上界 |

### `[bandwagon]` / `[opposite]` / `[behavior]`（表情回应）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `bandwagon.probability` / `self_message_probability` | `0.25` / `0.12` | 回应骰概率 |
| `opposite.enabled` / `probability` | `true` / `0.35` | 反义开关与反义骰 |
| `opposite.include_defaults` | `true` | 使用内置反义映射 |
| `opposite.extra_pairs` / `disabled_emojis` | `[]` / `[]` | 追加/移除反义对，写法 `"A=B"` 或协议端表情编号 |
| `behavior.cooldown_seconds` | `60` | 同群贴表情最小间隔 |
| `behavior.delay_min/max_seconds` | `1.0` / `6.0` | 贴表情前随机等待 |
| `behavior.slot_ttl_seconds` / `max_slots` | `3600` / `2000` | 表情槽记忆时长与条数上界 |

### `[context]`（表情回应域上下文注入，实验性）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `false` | 注入与拦截总开关（复读域注入跟随 `repeat.enabled`，不受此开关约束） |
| `dedup_ttl_seconds` | `3600` | 同一（回应者、被回应消息、表情 ID）只注入一次 |
| `merge_window_seconds` | `30` | 同消息同表情多条回应聚合为一条注入 |
| `max_reactions_per_message` | `10` | 单条被回应消息的注入上限 |
| `self_display_name` | `""` | 留空取主程序 `bot.nickname` |

### `[webhook]` / `[protocol]`

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `webhook.host` / `port` / `path` | `0.0.0.0` / `18081` / `/maibot/emoji_reaction` | 接收协议端表情回应上报的端点 |
| `webhook.secret` | `""` | SnowLuma 留空 |
| `protocol.base_url` / `access_token` / `self_qq` | `""` | 协议端 OneBot HTTP 地址、令牌、麦麦 QQ（留空自动获取） |

### `[scope]`

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `group_whitelist` / `group_blacklist` | `[]` / `[]` | 生效群名单，留空 = 除黑名单外所有群 |
| `ignore_reactors` | `[]` | 忽略这些 QQ 贴的表情回应（通常填其他机器人） |
| `ignore_senders` | `[]` | 忽略这些 QQ 发出的消息（不进复读链） |

</details>

<details>
<summary>📁 项目结构</summary>

```
repeat-plugin/
├── plugin.py               # 插件主类：两域 Hook 编排、协议端直发、上下文注入、启动自检
├── repeats.py              # 复读域纯逻辑层：归一、链状态机、三出口决策、注入文案
├── reactions.py            # 表情回应域纯逻辑层：上报解析、反义映射、两级决策、表情槽
├── config.py               # 配置模型
├── config.toml             # 默认配置（与 config.py 逐项一致）
├── _manifest.json          # 插件清单 v2
├── tests/                  # pytest：纯逻辑单测 + 插件级集成
├── docs/
│   ├── adr/                # 决策记录 0001~0014
│   └── emoji-id-reference.md  # 表情 ID 编号体系查表
└── openspec/               # 行为契约（specs/ 主 spec + changes/archive 变更归档）
```

</details>

---

## 📄 许可证

本项目采用 [MIT](LICENSE) 许可证开源。

---

## 🙏 致谢

- [MaiBot](https://github.com/Mai-with-u/MaiBot) — 插件宿主与插件运行时
- [maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk) — 插件 SDK
- [SnowLuma](https://github.com/Mai-with-u/SnowLuma) — 协议端实现与事件/动作形态参考

---

<div align="center">

**Made with ❤️ by [ArcDent](https://github.com/ArcDent)**

**Star ⭐ 如果这个项目对你有帮助！**

</div>
