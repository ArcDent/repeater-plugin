<!-- markdownlint-disable -->

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
![Platform](https://img.shields.io/badge/Platform-Docker%20%7C%20Linux-blue?style=for-the-badge)

**✨ 让麦麦像群友一样接龙复读、跟着贴表情 ✨**

纯规则 + 概率的两域聚合插件：复读链一次掷骰选三出口、表情回应两级决策

[项目简介](#项目简介) • [功能特性](#功能特性) • [项目结构](#项目结构) • [快速部署](#快速部署) • [配置](#配置) • [迁移](#迁移) • [开发](#开发)

</div>

---

## 项目简介

repeater-plugin 是一个 MaiBot 群聊行为插件，把两种「群友会做的事」聚合到一起：

1. **复读**：群内同一句话接龙达标后，麦麦掷一次骰按**出口权重**选中恰好一个出口——把**复读文本**原样发出、发一条**打断文本**唱反调、或沉默。
2. **表情回应跟风**：有人给某条消息贴**表情回应**（消息气泡下方那排小表情）后，麦麦按概率**跟风**贴同款，或按概率贴语义相反的**反义回应**。

> [!NOTE]
> 边界与前提：本插件只碰**表情回应**，不发送表情包（那是 `ctx.send.emoji` 的事）；两个域都只认群聊消息。术语定义见 [CONTEXT.md](CONTEXT.md)，决策记录见 [docs/adr/](docs/adr/)。

### 为什么选择 repeater-plugin？

| 特点 | 说明 |
|:-----|:-----|
| **纯规则 + 概率** | 两个域都不依赖 LLM 判断：复读链达标 / 收到表情回应上报即掷骰，行为可控、频率可调、不烧 token |
| **协议端直发** | 复读/打断经协议端 HTTP 直调发送，只依赖群号，与主程序发送链和聊天流路由解耦 |
| **两域并列不混装** | 每域有独立开关、独立冷却、独立延迟；配置改名互不牵扯，出问题先按域定位 |
| **完全可单测** | `repeats.py` / `reactions.py` 都是不依赖 SDK 的纯逻辑层，342 条用例覆盖边界 |
| **记忆与事实一致** | 直发出去的复读/打断会注入麦麦的聊天历史，麦麦知道自己说过什么 |

---

## 功能特性

### 复读

**复读链**按（群，归一后的**复读文本**）累积：滚动窗口（`repeat.window_seconds`，默认 120 秒）内同一文本被多人接龙、计数达到 `repeat.threshold`（默认 3）即达标，取得一次决策资格。计数默认只计不同**发送者**（同一人连发不增计数），`distinct_senders = false` 时逐条计数。

比对前做宽松归一（去首尾空白、连续空白折叠、大小写不敏感）；只有**纯文本消息**产出**复读文本**（含 @、引用、图片、表情包段的消息不参与）；发出时用链内最后一条的**原始文本**。

达标后掷一次骰，三出口**严格互斥**：

| 出口 | 默认权重 | 行为 |
|:-----|:---------|:-----|
| **复读** `weight_repeat` | 85 | 拦截触发消息（planner 看不到它，杜绝双发），把**复读文本**经协议端直发；触发消息与复读文本注入上下文 |
| **打断** `weight_interrupt` | 15 | 同样拦截触发消息，只发**打断文本**池里随机一条，绝不附带**复读文本** |
| **沉默** `weight_silence` | 0 | 触发消息照常放行，麦麦自行决定如何回应 |

三个权重只有比例意义（170/30/0 与 85/15/0 同分布）；三者全为 0 时不掷骰、按沉默处理。默认沉默权重 0 = 链一达标必出声，唯一放行口是复读群冷却的压制期；要「偶尔沉默」就给 `weight_silence` 留正值。

防护与节奏：

| 机制 | 默认 | 说明 |
|:-----|:-----|:-----|
| **链终结** `chain_ttl_seconds` | 1800 | 掷骰后（含沉默出口）同文本在此时长内不再触发新一轮，防止一轮接龙里麦麦多次出声 |
| **复读群冷却** `cooldown_seconds` | 120 | 复读/打断命中后进入冷却，冷却期内链照常累积但不掷骰；与表情回应的群冷却分开计时 |
| **发送延迟** `delay_min/max_seconds` | 0.3~1.5 | 复读段自有延迟，避免机械秒回；与 `[behavior]` 的 1~6 秒互相独立（见 [ADR 0008](docs/adr/0008-repeat-own-delay-and-concurrent-trigger-injection.md)） |

### 表情回应跟风

两级决策，最多掷两次骰：

| 骰子 | 条件 | 默认概率 | 未命中时 |
|:-----|:-----|:---------|:---------|
| **回应骰** `bandwagon.probability` | 有人给**他人**的消息贴表情回应 | 0.25 | 不贴任何表情回应 |
| **回应骰** `bandwagon.self_message_probability` | 有人给**麦麦自己**的消息贴表情回应 | 0.12 | 同上 |
| **反义骰** `opposite.probability` | 回应后，表情 ID 命中**反义映射** | 0.35 | **回落跟风**贴同款 |

- 一条消息麦麦最多贴一次（**表情槽**）；同群两次贴表情间隔不小于 `behavior.cooldown_seconds`（默认 60 秒）
- 贴之前按 `behavior.delay_min/max`（默认 1~6 秒）随机等待，与复读域延迟互不牵扯
- 命中反义映射但与跟风**严格互斥**（见 [ADR 0002](docs/adr/0002-exclusive-branches-and-in-memory-slots.md)）

内置**反义映射**（`opposite.include_defaults = true` 时生效，双向对称）：

| | | | | | | | |
|---|---|---|---|---|---|---|---|
| 👍↔👎 | ❤↔💔 | 😂↔😭 | 😄↔😡 | ✅↔❌ | 🔥↔❄ | ⬆↔⬇ | 😍↔🤢 |
| 😈↔😇 | 🤮↔😋 | 🥶↔🥵 | ☀↔🌧 | 🚀↔🐢 | 😊↔😠 | 🎉↔😢 | ➕↔➖ |

自定义反义对，可用 emoji 字符或协议端表情编号：

```toml
[opposite]
extra_pairs = ["🎉=😢", "76=78"]
disabled_emojis = ["👍"]   # 把 👍 从反义映射里摘掉，回落到跟风
```

---

## 项目结构

```
repeat-plugin/
├── plugin.py               # 插件主类：两域的 Hook 编排、协议端直发、上下文注入、启动自检
├── repeats.py              # 复读域纯逻辑层：归一、链状态机、三出口决策、注入文案
├── reactions.py            # 表情回应域纯逻辑层：上报解析、反义映射、两级决策、表情槽
├── config.py               # 配置模型（plugin/webhook/protocol/bandwagon/opposite/repeat/behavior/context/scope）
├── config.toml             # 默认配置（值必须与 config.py 的 default 一致）
├── _manifest.json          # 插件清单 v2（id/版本/依赖/能力声明）
├── tests/                  # pytest：纯逻辑单测 + 插件级集成（fake maibot_sdk 注入）
├── docs/
│   ├── adr/                # 决策记录 0001~0014（0001~0012 已接受；0013 调研草案、0014 提案）
│   └── emoji-id-reference.md  # 表情 ID 两套编号体系查表
└── openspec/               # 行为契约（specs/ 主 spec + changes/archive 变更归档）
```

---

## 部署之前需要注意

### 前置环境

| 依赖 | 版本 | 说明 |
|:-----|:-----|:-----|
| **MaiBot** | ≥ 1.0.0 | 插件运行宿主（实测 1.2.3） |
| **maibot-plugin-sdk** | ≥ 2.0.0 | 随 MaiBot 提供 |
| **SnowLuma / NapCat 协议端** | OneBot v11 HTTP | 复读直发与表情回应贴表情都直调协议端 |
| **Python** | ≥ 3.10 | 插件运行时 |
| **aiohttp** | ≥ 3.9.0 | 唯一第三方依赖（`pip install aiohttp`） |

### 协议端上报（表情回应必需）

麦麦插件 SDK 的事件类型里没有 notice 通道，表情回应事件只能由协议端直接 POST 给插件（理由见 [ADR 0001](docs/adr/0001-webhook-instead-of-sdk-event.md)）：

1. 填 `[protocol]`：协议端 OneBot HTTP 地址与 token（复读直发也用它）
2. 给协议端**新增一个 HTTP 上报目标**，URL 指向 `http://<插件所在容器>:18081/maibot/emoji_reaction`

同机若已有好友申请插件占用了 18080，这里要**再加一个**、端口不同即可。`webhook.secret` 与协议端 secret 对应（带 `X-Signature: sha1=<hmac>` 头）；**用 SnowLuma 时留空**——它发的是 `Authorization: Bearer` 头，靠 Docker 网络隔离即可。

### 勿开 self-message 上报

> [!WARNING]
> 协议端 webhook **不要开启 self-message 上报**（SnowLuma 的 `reportSelfMessage`，默认关闭）。复读/打断经协议端直调发送，不走主程序发送链；一旦打开，直发消息会以 **普通 user 消息的身份**回流主程序入站链，planner 上下文里会出现「麦麦以 user 身份看到自己复读过」的形态污染（同一条复读既是注入里麦麦发的、又是入站里某个 user 发的）。见 [ADR 0006](docs/adr/0006-repeat-direct-send-with-injection-and-intercept.md)。

---

## 快速部署

### 推送插件（rsync）

目录名即插件 id（`arcdent.repeater-plugin`），整个放进 MaiBot 的 `plugins/`，无需打包；`config.toml` 排除，避免覆盖服务器上的配置（含 token）：

```bash
rsync -av --delete \
  --exclude .git --exclude .local --exclude .serena --exclude .superpowers \
  --exclude .pytest_cache --exclude .ruff_cache --exclude __pycache__ \
  --exclude tests --exclude docs/superpowers \
  --exclude .cursor --exclude openspec \
  --exclude conftest.py --exclude pyrightconfig.json --exclude config.toml \
  ./ arclinux:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.repeater-plugin/
```

### 写配置 → 重启 → 验证

```bash
# 部署配置（含协议端 token，勿入库；未写的节用默认值，首次部署执行一次）
ssh arclinux 'cat > /home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.repeater-plugin/config.toml << "EOF"
[webhook]
secret = ""    # SnowLuma 上报带 Authorization 头，X-Signature 校验不适用，留空

[protocol]
base_url = "http://maim-bot-snowluma:3000"
access_token = "<snowluma OneBot HTTP accessToken>"
self_qq = ""   # 留空启动时自动获取
EOF'

# 重启主容器让 MaiBot 重新发现插件
ssh arclinux 'docker restart maim-bot-core'

# 验证加载
ssh arclinux 'docker logs --since 2m maim-bot-core | grep -E "复读插件已加载"'
```

### 首次部署验证（日志检查点）

| 日志 | 含义 |
|:-----|:-----|
| `复读插件已加载（复读 / 表情回应两域）` | 插件加载成功 |
| `表情回应上报已监听` | webhook 端点起来了 |
| `复读链达标` → `已直发复读文本` / `已直发打断文本` | 复读链路通（三人接龙一次即可验证） |
| `表情回应触发` / `已跟风贴表情` | 表情回应链路通 |
| `表情回应上报形态不认识，已忽略: {...}` | 协议端形态变了，贴回原始 JSON 补解析分支 |
| `协议端不支持 set_msg_emoji_like（retcode=1404）` | 协议端不支持贴表情动作，插件自动停止尝试（复读不受影响） |

---

## 配置

配置模型见 [config.py](config.py)，默认值见 [config.toml](config.toml)，两处必须逐项一致。

### `[plugin]`（总开关）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `true` | 插件总开关，关闭后复读与表情回应两个域全部停用（不再拦截、不发送、不贴表情） |
| `config_version` | `1.5.0` | 配置结构版本，由插件维护（WebUI 隐藏）；升级时按它重建 `config.toml` 骨架并保留用户值 |

### `[repeat]`（复读）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `true` | 复读独立开关：关闭后仅停用复读，表情回应跟风不受影响 |
| `window_seconds` | `120` | 滚动窗口：窗口内归一后相同的**复读文本**按口径累积 |
| `threshold` | `3` | 窗口内计数达该值时链达标，取得一次决策资格 |
| `distinct_senders` | `true` | 仅计不同**发送者**人头；关闭后逐条消息计数 |
| `weight_repeat` | `85` | **复读**出口权重。0 = 从不复读 |
| `weight_interrupt` | `15` | **打断**出口权重。0 = 从不打断 |
| `weight_silence` | `0` | 沉默出口权重；默认 0 = 链达标必出声。三者全 0 时不掷骰直接沉默 |
| `interrupt_texts` | 五条内置 | **打断文本**变体池；全部留空回落内置默认池并记日志 |
| `delay_min_seconds` / `delay_max_seconds` | `0.3` / `1.5` | 复读/打断发送前随机等待区间（秒），与表情回应延迟独立 |
| `chain_ttl_seconds` | `1800` | **链终结**标记保留时长。0 = 不做链终结 |
| `cooldown_seconds` | `120` | 复读专用群冷却；冷却期内链达标不掷骰、不消耗资格。0 = 不限 |
| `max_text_length` | `200` | 超过此长度的文本不参与累积。0 = 不限 |
| `max_chains` | `2000` | 内存中**复读链**条数上界，超出丢弃最旧的链 |

### `[bandwagon]` / `[opposite]` / `[behavior]`（表情回应）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `bandwagon.probability` | `0.25` | **回应骰**：给他人消息贴表情时决定是否回应 |
| `bandwagon.self_message_probability` | `0.12` | **回应骰**：给麦麦自己消息贴表情时使用 |
| `opposite.enabled` | `true` | 反义分支总开关；关闭后回应一律贴同款 |
| `opposite.probability` | `0.35` | **反义骰**：命中反义映射后决定贴反义还是同款；填 0 则从不唱反调 |
| `opposite.include_defaults` | `true` | 是否使用内置反义映射 |
| `opposite.extra_pairs` / `disabled_emojis` | `[]` / `[]` | 追加/移除反义对，写法 `"A=B"` |
| `behavior.cooldown_seconds` | `60` | 同群两次贴表情的最小间隔。0 = 不限 |
| `behavior.delay_min/max_seconds` | `1.0` / `6.0` | 决定贴表情后的随机等待区间，避免瞬间贴上显得机械 |
| `behavior.slot_ttl_seconds` / `max_slots` | `3600` / `2000` | 表情槽（「这条消息已贴过」）的记忆时长与条数上界 |

### `[context]`（上下文注入，实验性）

只管**表情回应域**的注入，整块功能试验性地开、关；复读域注入跟随 `repeat.enabled`，不受此开关约束。开启后表情回应去重、聚合为独立上下文消息（`source_kind="reaction"`），同时拦截主程序消息链上的表情回应通知消息（见 [ADR 0003](docs/adr/0003-reaction-context-injection-with-defensive-intercept.md)）。

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `false` | 注入与拦截总开关 |
| `dedup_ttl_seconds` | `3600` | **去重**：同一（回应者、被回应消息、表情 ID）组合只注入一次。0 = 不启用 |
| `merge_window_seconds` | `30` | **聚合**：同一被回应消息同一表情的多条回应汇入一条注入消息 |
| `max_reactions_per_message` | `10` | **上限**：单条被回应消息最多产生的注入消息条数 |
| `self_display_name` | `""` | 麦麦显示名；留空取主程序 `bot.nickname`，再取不到则不写名字 |

### `[webhook]` / `[protocol]`

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `webhook.host` / `port` / `path` | `0.0.0.0` / `18081` / `/maibot/emoji_reaction` | 接收协议端表情回应上报的端点 |
| `webhook.secret` | `""` | 对应协议端上报 secret；SnowLuma 留空 |
| `protocol.base_url` | `""` | 协议端 OneBot HTTP 地址；不填则复读直发与贴表情都不动作 |
| `protocol.access_token` | `""` | 协议端 accessToken |
| `protocol.self_qq` | `""` | 麦麦自己 QQ 号；留空用 `get_login_info` 自动获取 |

### `[scope]`

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `group_whitelist` / `group_blacklist` | `[]` / `[]` | 生效群名单（留空 = 除黑名单外所有群） |
| `ignore_reactors` | `[]` | 忽略这些 QQ 贴出的表情回应，通常填其他机器人 |
| `ignore_senders` | `[]` | 忽略这些 QQ 发出的消息（不进**复读链**），与 `ignore_reactors` 是两个概念 |

---

## 迁移

### 从 0.8.x 升级（0.9.0：跨群转发整体移除）

`config_version` 1.4.0 → 1.5.0：**跨群转发域已完整移除**——`[forward]` 配置段、转发 Tool 与 Hook、自动/请求两条转发路径、转发注入与专属后台状态全部不再存在；主程序按结构版本重建 `config.toml` 骨架并保留剩余两域的用户值。

| 事项 | 说明 |
|:-----|:-----|
| **BREAKING：转发功能移除** | 本插件不再提供任何转发能力；旧 `[forward]` 配置不再生效，留着也不影响加载，无需迁移 |
| **转发改由独立插件承担** | 转发能力移至独立插件「麦麦搬运喵」（`maimai-courier-plugin`）——该插件**尚未验收、当前无可部署版本**，请先确认确实需要转发能力，再按它自己的文档单独部署与验证 |
| **复读与表情回应不受影响** | 两个域的行为、配置、冷却与注入路径不变；插件加载日志变为「复读插件已加载（复读 / 表情回应两域）」 |
| **旧转发历史数据** | 不自动迁移、不自动清理，由部署者按需处理 |

### 从 sending-emojis-plugin（项目改名）

manifest id 从 `arcdent.sending-emojis-plugin` 变为 `arcdent.repeater-plugin`，对 MaiBot 而言是新插件，老部署不会自动升级：

| 步骤 | 操作 |
|:-----|:-----|
| 1 | 容器内删除旧目录 `data/MaiMBot/plugins/arcdent.sending-emojis-plugin/`，放进 `arcdent.repeater-plugin/`（目录名即 id），重启让 MaiBot 重新发现 |
| 2 | 旧 `config.toml` 直接复制：旧字段全兼容，新增节缺省用默认值 |
| 3 | 协议端上报目标无需改动（地址、端口、路径都没变） |
| 4 | 回滚：重装旧版本目录，配置向后兼容 |

### 从旧版本升级（复读三出口权重）

`repeat.probability` / `repeat.interrupt_probability` 已被三个**出口权重**（`weight_repeat` / `weight_interrupt` / `weight_silence`）取代，旧值**不会自动迁移**，升级后请按需重调一次；同时复读延迟改由复读段自有的 `repeat.delay_min/max_seconds` 控制（见 [CHANGELOG](CHANGELOG.md) 0.5.0）。

---

## 已知边界

| 边界 | 说明 |
|:-----|:-----|
| **只处理群聊** | 两个域都只认群消息；SnowLuma 的 notice 类型里只有 `group_msg_emoji_like`，没有私聊版本，没有 `group_id` 的上报被静默忽略 |
| **内存态** | 复读链、终结标记、表情槽与冷却只存内存，热重载或重启后清零，短时间内可能对重启前处理过的消息再出一次声（有意为之，见 [ADR 0002](docs/adr/0002-exclusive-branches-and-in-memory-slots.md)） |
| **`get_msg` 依赖协议端存储** | 判断「有人贴的是不是麦麦自己的消息」依赖协议端 `get_msg`，协议端重启前的旧消息查不到，查不到时按他人消息处理 |
| **麦麦 QQ 号取不到时** | 协议端 `get_login_info` 不可用且没手填 `protocol.self_qq` 时，上述判断退化为「都按他人消息处理」；复读侧按 `user_id` 判定，不受影响 |
| **自定义命令前缀（复读）** | 复读检测按文本 `/` 开头排除命令（`before_process` 时点主程序还没写命令标记），部署改过前缀时那些命令会被当作普通文本参与**复读链** |
| **图片与表情包不参与复读** | 只有纯文本消息产出**复读文本**；图片按指纹比对同一并重发留作未来扩展 |

---

## 开发

```bash
# 全部测试（342 条：纯逻辑单测 + 插件级集成）
uv run --with pytest --with aiohttp pytest tests/ -q

# 类型检查（生产代码零错误）
uv run --with pyright --with aiohttp pyright

# 代码风格（19 条有意偏离见 AGENTS.md「验证」节）
uv run --with ruff ruff check .
```

架构约定：`repeats.py`（复读域）、`reactions.py`（表情回应域）都是**不依赖 `maibot_sdk` 的纯逻辑层**，全部可单测；`plugin.py` 只做编排与 I/O。新增可测逻辑按领域写进对应纯逻辑层，不往 `plugin.py` 里塞。行为契约与规则真源见 [AGENTS.md](AGENTS.md)（软链 `CLAUDE.md`），术语见 [CONTEXT.md](CONTEXT.md)，行为 spec 见 [openspec/specs/](openspec/specs/)。

---

## 致谢

- [MaiBot](https://github.com/Mai-with-u/MaiBot) — 插件宿主与插件运行时
- [maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk) — 插件 SDK
- [SnowLuma](https://github.com/Mai-with-u/SnowLuma) — 协议端实现与事件/动作形态参考

---

<div align="center">

**Made with ❤️ by [ArcDent](https://github.com/ArcDent)**

**Star ⭐ 如果这个项目对你有帮助！**

</div>

<!-- markdownlint-restore -->
