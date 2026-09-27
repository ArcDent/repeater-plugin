<!-- markdownlint-disable -->

<p align="center">
  <pre>
  ██████╗  ███████╗ ██████╗  ███████╗ █████╗   ████████╗ ███████╗ ██████╗
  ██╔══██╗ ██╔════╝ ██╔══██╗ ██╔════╝ ██╔══██╗ ╚══██╔══╝ ██╔════╝ ██╔══██╗
  ██████╔╝ █████╗   ██████╔╝ █████╗   ███████║    ██║    █████╗   ██████╔╝
  ██╔══██╗ ██╔══╝   ██╔═══╝  ██╔══╝   ██╔══██║    ██║    ██╔══╝   ██╔══██╗
  ██║  ██║ ███████╗ ██║      ███████╗ ██║  ██║    ██║    ███████╗ ██║  ██║
  ╚═╝  ╚═╝ ╚══════╝ ╚═╝      ╚══════╝ ╚═╝  ╚═╝    ╚═╝    ╚══════╝ ╚═╝  ╚═╝
            MaiBot Repeater Plugin — 复读 / 表情回应 / 跨群转发
  </pre>
</p>

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![MaiBot](https://img.shields.io/badge/MaiBot-1.0.0%2B-6E4AFF?style=for-the-badge)
![Version](https://img.shields.io/badge/Version-0.7.0-2F81F7?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Platform](https://img.shields.io/badge/Platform-Docker%20%7C%20Linux-blue?style=for-the-badge)

**✨ 让麦麦像群友一样接龙复读、跟着贴表情、分享合并转发 ✨**

纯规则 + 概率的三域聚合插件：复读链一次掷骰选三出口、表情回应两级决策、跨群转发双路径

[项目简介](#项目简介) • [功能特性](#功能特性) • [项目结构](#项目结构) • [快速部署](#快速部署) • [配置](#配置) • [迁移](#迁移) • [开发](#开发)

</div>

---

## 项目简介

repeater-plugin 是一个 MaiBot 群聊行为插件，把三种「群友会做的事」聚合到一起：

1. **复读**：群内同一句话接龙达标后，麦麦掷一次骰按**出口权重**选中恰好一个出口——把**复读文本**原样发出、发一条**打断文本**唱反调、或沉默。
2. **表情回应跟风**：有人给某条消息贴**表情回应**（消息气泡下方那排小表情）后，麦麦按概率**跟风**贴同款，或按概率贴语义相反的**反义回应**。
3. **跨群转发**（默认关闭）：**源群**里出现**合并转发**后，麦麦或按**转发权重**对每个**目标群**独立掷骰**自动转发**，或响应群友的对话请求执行**请求转发**。

> [!NOTE]
> 边界与前提：本插件只碰**表情回应**，不发送表情包（那是 `ctx.send.emoji` 的事）；转发域只处理**合并转发**，不转发普通单条消息；三个域都只认群聊消息。术语定义见 [CONTEXT.md](CONTEXT.md)，决策记录见 [docs/adr/](docs/adr/)。

### 为什么选择 repeater-plugin？

| 特点 | 说明 |
|:-----|:-----|
| **纯规则 + 概率** | 默认直发模式下三个域都不依赖 LLM 判断：达标/上报/收转发即掷骰，行为可控、频率可调、不烧 token（`planner` 决策模式才把「是否转发」交目标群 Planner） |
| **协议端直发** | 复读/打断与自动转发经协议端 HTTP 直调发送，只依赖群号，与主程序发送链和聊天流路由解耦 |
| **三域并列不混装** | 每域有独立开关、独立冷却、独立延迟；配置改名互不牵扯，出问题先按域定位 |
| **完全可单测** | `repeats.py` / `reactions.py` / `forwards/` 都是不依赖 SDK 的纯逻辑层，679 条用例覆盖边界 |
| **记忆与事实一致** | 直发出去的复读/转发会注入麦麦的聊天历史，麦麦知道自己说过什么、转过什么 |

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

### 跨群转发（默认关闭）

第三个行为域。`forward.enabled` 开启后，**源群**（`forward.source_groups`）里出现**合并转发**时触发两条路径：

**自动转发**（掷骰，默认不经过 LLM）：源群收到合并转发后，`before_process` 同步段只做门禁与**转发防重**标记，并留一份**转发源消息快照**（原始段 + 内存字节）；后台任务先等**摘要就绪**（轮询 `message.get_by_id` 直到该转发入库且 `processed_plain_text` 非空，超时 `forward.summary_timeout_seconds` 则放弃本次），再对**目标群**逐个独立掷骰。命中后按 `forward.mode` 分两条路：

- `direct`（默认）——直接进入下面的五步编舞，不经过 LLM；
- `planner`——不直发，插件为目标群签发一次性**决策票**并把**富摘要**交给该群 Planner，模型经 `decide_forward_delivery` Tool 回传「转发 / 不转发」，只有「转发」才进入同一套编舞（决策票无票 / 错群 / 重复消费 / 过期一律拒绝，模型不决策则按不转发处理）。

五步编舞（两个模式共用）：

| 步骤 | 动作 | 失败时 |
|:-----|:-----|:-------|
| 1 | 延迟（`forward.delay_*`，默认 0.5~2.0 秒） | — |
| 2 | 目标群可用性校验（`get_group_list` 缓存 300 秒） | 跳过该群，日志给群号与原因 |
| 3 | 协议端直发 `send_group_forward_msg`（字节取自**转发源消息快照**，图片不丢） | 记 warning 止步，不重试 |
| 4 | 目标群**转发内容注入**（`source_kind=forward`，内容为**富摘要**） | 记 warning，不阻断后续 |
| 5 | 触发目标群 Planner 针对该转发发言（软约束，可沉默） | 记 warning，不阻断 |

至少一个目标群投递成功后，向源群注入一条**转发留痕**（`source_kind=forward_action`，只列实际成功的群）。

为什么等摘要：主程序处理完会丢弃内存里的图片字节，早发只能发空壳；注入文案也要等转发入库后的**富摘要**（转发内文本 + 已缓存图片描述）才有内容。就绪判定**不等识图描述回填**——描述只进 Maisaka 内存历史、消息本体永不更新，等不到；首见图在富摘要里仍是占位，这是主程序边界。

媒体兜底收紧：节点里的图片段只有在带二进制（转 `base64://`）或段内数据是可用引用（`http(s)://` / `base64://` / `file://` 前缀或含路径分隔符）时才发出；两者皆无的媒体段不产出，节点因此变空就整条丢弃，不再发 `[图片]` / `[表情]` 占位文本；全部节点落空则放弃本次投递并记 warning。

**请求转发**（对话请求，不掷骰）：群友让麦麦分享某条合并转发时，走 `request_cross_group_forward` Tool 的完整链路——**查看资格**（已完整查看内容，或连续可重试故障达 `view_failure_fallback_threshold` 后降级用摘要）、一次性会话授权、按 `target_groups` 顺序投递、目标群 Planner 自主决定是否发言。该路径不受**转发权重**影响，**强制发言**只适用于自动路径。

防护与校验：

| 机制 | 默认 | 说明 |
|:-----|:-----|:-----|
| **转发防重** `dedup_ttl_seconds` | 1800 | 同一条**转发源消息**在此窗口内只掷一次骰；标记在 `before_process` 同步段写入，先于摘要等待，等待期间重复上报不触发第二次编排 |
| **摘要就绪超时** `summary_timeout_seconds` | 10 | 等待该转发入库且**富摘要**非空的上限（秒）；超时记 warning 并放弃本次，不掷骰、不写冷却、不投递 |
| **源群冷却** `cooldown_seconds` | 60 | 至少一个目标群命中转发才写；冷却期内该群新到的合并转发不掷骰，全沉默不写 |
| **目标群冷却** `target_cooldown_seconds` | 60 | 该群命中转发后写入，只压制该群，与其他目标群无关 |
| **已投递状态** | — | 自动路径投递成功的目标群会被请求转发跳过；全部已投递时返回「已转发过」 |
| **可用性校验** | 缓存 300 秒 | 投递前校验目标群仍在机器人群列表；不在列表则跳过并给出明确原因，绝无「未知错误」 |

因为掷骰与冷却写入推迟到**摘要就绪**之后，就绪等待窗口内到达的新合并转发会各自独立掷骰（冷却写入随之后移，通常窗口为亚秒级）。两条路径可分别用 `forward.auto_enabled` / `forward.request_enabled` 单独停用，互不影响；转发域总闸仍是 `forward.enabled`（与 `plugin.enabled` 合取）。

自动路径投递的日志带「自动转发」前缀与目标群号，请求路径沿用原插件的中文文案，共享群列表刷新等待方的成败另以「请求转发」前缀记录——排查时先分清是哪条路径。转发域**不复用** `[scope]` 白黑名单与 `[behavior]` 延迟字段。

---

## 项目结构

```
repeat-plugin/
├── plugin.py               # 插件主类：三域的 Hook/Tool 编排、协议端直发、注入、启动自检
├── repeats.py              # 复读域纯逻辑层：归一、链状态机、三出口决策、注入文案
├── reactions.py            # 表情回应域纯逻辑层：上报解析、反义映射、两级决策、表情槽
├── config.py               # 配置模型（plugin/webhook/protocol/bandwagon/opposite/repeat/behavior/context/scope/forward）
├── config.toml             # 默认配置（值必须与 config.py 的 default 一致）
├── _manifest.json          # 插件清单 v2（id/版本/依赖/能力声明）
├── forwards/               # 转发域子包（纯逻辑，不依赖 SDK）
│   ├── source.py           #   转发源消息解析与资格判定
│   ├── nodes.py            #   转发节点解析与 OneBot 转换（含嵌套转发与媒体三分支兜底）
│   ├── forward_segments.py #   段定位与节点校验的共享叶子模块（两条路径复用）
│   ├── auto.py             #   逐目标群独立掷骰
│   ├── summary.py          #   摘要就绪提取（富摘要）与轮询节奏
│   ├── decision.py         #   转发决策票注册表（决策模式一次性凭据）
│   ├── state.py            #   防重 / 冷却 / 已投递状态 / 群可用性缓存
│   ├── texts.py            #   内容注入、留痕、强制发言与决策 intent 文案（富摘要 + 控制字符清洗）
│   └── request/            #   请求转发链路（原 ForwardMessagesAuto v0.2.12 整包移植）
├── tests/                  # pytest：纯逻辑单测 + 插件级集成（fake maibot_sdk 注入）
├── docs/
│   ├── adr/                # 决策记录 0001~0011
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
| **SnowLuma / NapCat 协议端** | OneBot v11 HTTP | 复读直发、自动转发、表情回应贴表情都直调协议端 |
| **Python** | ≥ 3.10 | 插件运行时 |
| **aiohttp** | ≥ 3.9.0 | 唯一第三方依赖（`pip install aiohttp`） |

### 协议端上报（表情回应必需）

麦麦插件 SDK 的事件类型里没有 notice 通道，表情回应事件只能由协议端直接 POST 给插件（理由见 [ADR 0001](docs/adr/0001-webhook-instead-of-sdk-event.md)）：

1. 填 `[protocol]`：协议端 OneBot HTTP 地址与 token（复读直发与自动转发也用它）
2. 给协议端**新增一个 HTTP 上报目标**，URL 指向 `http://<插件所在容器>:18081/maibot/emoji_reaction`

同机若已有好友申请插件占用了 18080，这里要**再加一个**、端口不同即可。`webhook.secret` 与协议端 secret 对应（带 `X-Signature: sha1=<hmac>` 头）；**用 SnowLuma 时留空**——它发的是 `Authorization: Bearer` 头，靠 Docker 网络隔离即可。

### 勿开 self-message 上报

> [!WARNING]
> 协议端 webhook **不要开启 self-message 上报**（SnowLuma 的 `reportSelfMessage`，默认关闭）。复读/打断与自动转发都经协议端直调发送，不走主程序发送链；一旦打开，直发消息会以 **普通 user 消息的身份**回流主程序入站链，planner 上下文里会出现「麦麦以 user 身份看到自己复读过」的形态污染（同一条复读既是注入里麦麦发的、又是入站里某个 user 发的）。见 [ADR 0006](docs/adr/0006-repeat-direct-send-with-injection-and-intercept.md)。

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

[forward]
enabled = true
source_groups = ["<源群号>"]
target_groups = ["<目标群号A>", "<目标群号B>"]
EOF'

# 重启主容器让 MaiBot 重新发现插件
ssh arclinux 'docker restart maim-bot-core'

# 验证加载与转发域启用
ssh arclinux 'docker logs --since 2m maim-bot-core | grep -E "复读插件已加载|跨群转发已启用"'
```

### 首次部署验证（日志检查点）

| 日志 | 含义 |
|:-----|:-----|
| `复读插件已加载（复读 / 表情回应 / 跨群转发三域）` | 插件加载成功 |
| `跨群转发已启用：源群 N 个 / 目标群 M 个` | 转发域配置生效 |
| `表情回应上报已监听` | webhook 端点起来了 |
| `复读链达标` → `已直发复读文本` / `已直发打断文本` | 复读链路通（三人接龙一次即可验证） |
| `表情回应触发` / `已跟风贴表情` | 表情回应链路通 |
| `自动转发命中` → `自动转发已投递` | 自动转发掷骰命中且目标群直发成功 |
| `自动转发摘要就绪超时` | 转发长时间未入库或摘要为空，本次放弃（对照 `summary_timeout_seconds` 调参） |
| `自动转发决策模式已触发目标群决策` | `planner` 模式下已请目标群 Planner 决策，等待 `decide_forward_delivery` Tool 回传结果 |
| `跳过不可用目标群` | 配置里有失效群，按日志群号清理 |
| `表情回应上报形态不认识，已忽略: {...}` | 协议端形态变了，贴回原始 JSON 补解析分支 |
| `协议端不支持 set_msg_emoji_like（retcode=1404）` | 协议端不支持贴表情动作，插件自动停止尝试（复读不受影响） |

---

## 配置

配置模型见 [config.py](config.py)，默认值见 [config.toml](config.toml)，两处必须逐项一致。

### `[forward]`（跨群转发）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `false` | 转发域独立开关；关闭时自动转发、请求转发、源群触发与两类注入全部停用 |
| `source_groups` | `[]` | **源群**白名单：只处理这些 QQ 群里的合并转发；留空则没有源群 |
| `target_groups` | `[]` | **目标群**列表：自动转发的候选群，也是请求转发的投递顺序；与源群相同的项被跳过 |
| `weight_send` | `30` | 每个目标群独立掷骰的「转发」权重，只有比例意义、同比例放大等价 |
| `weight_silence` | `70` | 「沉默」权重；单群两权重全为 0 时视为沉默、不掷骰 |
| `dedup_ttl_seconds` | `1800` | **转发防重**窗口：同一条**转发源消息**只参与一次掷骰。0 = 不防重 |
| `cooldown_seconds` | `60` | **源群冷却**：至少一个目标群命中转发后写入；全沉默不写。0 = 不限 |
| `target_cooldown_seconds` | `60` | **目标群冷却**：只压制该群，与源群冷却分开计时。0 = 不限 |
| `delay_min_seconds` | `0.5` | 命中转发后发送前的最短等待（秒），与另两域延迟互相独立 |
| `delay_max_seconds` | `2.0` | 发送前等待上限（秒），实际在区间内随机取 |
| `trigger_source_planner` | `true` | 源群收到合并转发时是否安排一次本群 Planner 触发（**请求转发**入口提示） |
| `trigger_target_planner` | `true` | 自动转发投递成功后是否触发目标群 Planner 发言（软约束，仍可沉默） |
| `view_failure_fallback_threshold` | `2` | 请求转发校验**查看资格**时，连续可重试故障达该次数后降级用摘要 |
| `inject_max_chars` | `800` | 转发内容注入文本的长度上限，超长截断并带省略标记 |
| `mode` | `"direct"` | 转发模式：`direct` = 掷骰命中即直发目标群（默认，升级后行为不变）；`planner` = 命中后把**富摘要**交给目标群 Planner，经 `decide_forward_delivery` Tool 决定是否转发。非法值按 `direct` 处理并记 warning |
| `auto_enabled` | `true` | **自动转发**路径开关：关闭后源群合并转发不再掷骰、不投递；请求转发路径不受影响 |
| `request_enabled` | `true` | **请求转发**路径开关：关闭后转发 Tool 与源群 Planner 入口提示都不再生效；自动转发路径不受影响 |
| `summary_timeout_seconds` | `10` | 等待**摘要就绪**的上限（秒，1~300）：超时记 warning 并放弃本次，不掷骰、不写冷却、不投递 |

调参提示：`weight_send` / `weight_silence` 只有比例意义——「三成转发」是 30/70，「偶尔转发」可改 5/95；两权重全 0 表示该目标群永远沉默。失效群号不会变成「未知错误」，但每次投递前都会被跳过并记 warning，建议及时清理。切 `planner` 模式需要目标群 Planner 可用：模型没在决策票 TTL 内调用 `decide_forward_delivery` 时按「不转发」处理并记 warning，插件不做超时补发。

### `[repeat]`（复读）

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `enabled` | `true` | 复读独立开关：关闭后仅停用复读，另两域不受影响 |
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
| `protocol.base_url` | `""` | 协议端 OneBot HTTP 地址；不填则复读直发、自动转发、贴表情都不动作 |
| `protocol.access_token` | `""` | 协议端 accessToken |
| `protocol.self_qq` | `""` | 麦麦自己 QQ 号；留空用 `get_login_info` 自动获取 |

### `[scope]`

| 字段 | 默认 | 说明 |
|:-----|:-----|:-----|
| `group_whitelist` / `group_blacklist` | `[]` / `[]` | 生效群名单（留空 = 除黑名单外所有群）；**转发域不复用**这两个名单 |
| `ignore_reactors` | `[]` | 忽略这些 QQ 贴出的表情回应，通常填其他机器人 |
| `ignore_senders` | `[]` | 忽略这些 QQ 发出的消息（不进**复读链**），与 `ignore_reactors` 是两个概念 |

---

## 迁移

### 从 0.6.0 升级（转发模式与路径开关）

`config_version` 1.2.0 → 1.3.0：`[forward]` 新增 `mode` / `auto_enabled` / `request_enabled` / `summary_timeout_seconds` 四个字段，主程序自动重建 `config.toml` 骨架（保留已填的旧字段值、补新键默认值）。

| 事项 | 说明 |
|:-----|:-----|
| **默认行为不变** | 默认 `mode = "direct"` + 两个路径开关为 `true`：升级后行为与 0.6.0 一致，仅自动转发的掷骰与投递时机后移到**摘要就绪**之后（通常亚秒级） |
| **图片保全** | 发送字节 100% 取自 `before_process` 时点的**转发源消息快照**，不再依赖事后从消息库恢复图片二进制 |
| **就绪窗口语义** | 冷却写入随掷骰后移：就绪等待窗口内到达的新合并转发会各自独立掷骰，不再被前一条的冷却压制 |
| **切 `planner` 模式** | 需要目标群 Planner 可用；命中后由模型经 `decide_forward_delivery` Tool 决定是否转发，模型不决策则按「不转发」处理（记 warning，不补发） |
| **媒体兜底** | 无二进制且段内数据不是可用引用的媒体段不再产生 `[图片]` / `[表情]` 占位文本，所在节点为空则丢弃；全部节点落空会放弃投递并记 warning |

从更早版本（0.6.0 之前）升级时，先按下一节填好 `[forward]` 的 `source_groups` / `target_groups`，再对照本表新增四个字段。

### 从 ForwardMessagesAuto（跨群转发旧插件）

原先部署 `papaya0481/maibot-ForwardMessagesAuto-plugin` 的，启用转发域前**必须停用旧插件**（`enabled = false` 或移出 plugins 目录）：两个插件都会在源群触发 Planner 并处理转发 Tool，同时启用会对同一批合并转发重复处理。原插件的 `[routing]` / `[behavior]` 不自动迁移，按 `[forward]` 表重填。

| 事项 | 说明 |
|:-----|:-----|
| **失效目标群** | 目标群必须仍在机器人群列表；旧配置里已退群的群号请删除——保留时投递被跳过，日志给群号与「不在机器人群列表」原因（不再是「未知错误」） |
| **版本升级** | `config_version` 1.1.0 → 1.2.0，主程序重建 `config.toml` 骨架（保留旧值、补默认 `[forward]` 段）；升级后填 `source_groups` / `target_groups` 并显式 `enabled = true` |
| **回滚** | 禁用 `forward.enabled` 并恢复旧插件目录与配置即可；自动转发的直发消息不写主程序消息库，回滚不涉及数据清理 |

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
| **只处理群聊** | 三域都只认群消息；SnowLuma 的 notice 类型里只有 `group_msg_emoji_like`，没有私聊版本，没有 `group_id` 的上报被静默忽略 |
| **内存态** | 复读链、终结标记、表情槽、冷却与已投递状态只存内存，热重载或重启后清零，短时间内可能对重启前处理过的消息再出一次声（有意为之，见 [ADR 0002](docs/adr/0002-exclusive-branches-and-in-memory-slots.md)） |
| **`get_msg` 依赖协议端存储** | 判断「有人贴的是不是麦麦自己的消息」依赖协议端 `get_msg`，协议端重启前的旧消息查不到，查不到时按他人消息处理 |
| **麦麦 QQ 号取不到时** | 协议端 `get_login_info` 不可用且没手填 `protocol.self_qq` 时，上述判断退化为「都按他人消息处理」；复读侧按 `user_id` 判定，不受影响 |
| **自定义命令前缀（复读）** | 复读检测按文本 `/` 开头排除命令（`before_process` 时点主程序还没写命令标记），部署改过前缀时那些命令会被当作普通文本参与**复读链** |
| **图片与表情包不参与复读** | 只有纯文本消息产出**复读文本**；图片按指纹比对同一并重发留作未来扩展 |
| **自动转发不写库** | 自动转发的直发消息不进主程序消息库，目标群上下文靠**转发内容注入**补偿；只有请求转发路径会写库 |

---

## 开发

```bash
# 全部测试（679 条：纯逻辑单测 + 插件级集成）
uv run --with pytest --with aiohttp pytest tests/ -q

# 类型检查（生产代码零错误）
uv run --with pyright --with aiohttp pyright

# 代码风格（53 条有意偏离见 AGENTS.md「验证」节）
uv run --with ruff ruff check .
```

架构约定：`repeats.py`（复读域）、`reactions.py`（表情回应域）、`forwards/`（转发域子包）都是**不依赖 `maibot_sdk` 的纯逻辑层**，全部可单测；`plugin.py` 只做编排与 I/O。新增可测逻辑按领域写进对应纯逻辑层，不往 `plugin.py` 里塞。行为契约与规则真源见 [AGENTS.md](AGENTS.md)（软链 `CLAUDE.md`），术语见 [CONTEXT.md](CONTEXT.md)，行为 spec 见 [openspec/specs/](openspec/specs/)。

---

## 致谢

- [MaiBot](https://github.com/Mai-with-u/MaiBot) — 插件宿主与插件运行时
- [maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk) — 插件 SDK
- [SnowLuma](https://github.com/Mai-with-u/SnowLuma) — 协议端实现与事件/动作形态参考
- [maibot-ForwardMessagesAuto-plugin](https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin) — 转发域「请求转发」链路的原始实现（v0.2.12 整包移植）

---

<div align="center">

**Made with ❤️ by [ArcDent](https://github.com/ArcDent)**

**Star ⭐ 如果这个项目对你有帮助！**

</div>

<!-- markdownlint-restore -->
