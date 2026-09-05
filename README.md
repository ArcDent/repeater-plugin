# 复读插件（repeater-plugin）

MaiBot 插件，聚合两个群聊行为：

1. **复读**：群内**复读链**（滚动时间窗口内同一**复读文本**被累积达阈值）达标后，麦麦按概率把链内最后一条的**复读文本**原样发出，或按概率发一条**打断文本**唱反调，或保持沉默——三出口互斥。
2. **表情回应跟风**：有人给某条消息贴了**表情回应**（消息气泡下方那排小表情）后，麦麦按概率**跟风**贴同款，或按概率贴语义相反的**反义回应**。

不涉及表情包发送——那是 `ctx.send.emoji` 的事，本插件只碰表情回应。术语定义见 [CONTEXT.md](CONTEXT.md)。

## 复读

**复读链**按（群，归一后的**复读文本**）累积：滚动窗口（`repeat.window_seconds`，默认 120 秒）内同一文本被多个人接龙，计数达到阈值（`repeat.threshold`，默认 3）即达标，取得一次决策资格。计数口径默认只计不同**发送者**（同一人连发不增计数），`repeat.distinct_senders = false` 时切换为逐条消息计数。

**复读文本**的比对做宽松归一：去首尾空白、连续空白折叠为单个、大小写不敏感；只有仅含文本段的消息能产出**复读文本**（含 @、引用、图片、表情包段的消息一律不参与）；**复读**发出时用链内最后一条的原始文本，不用归一产物。

达标后掷一次骰，三出口**严格互斥**：

- `roll < probability`（默认 0.6）→ **复读**：拦截触发消息（planner 看不到它，杜绝麦麦对同一条消息双发），把链内最后一条的原始**复读文本**经协议端直发（`send_group_msg`）发出，并把触发消息与复读文本注入上下文（麦麦知道群里谁触发的、自己复读了什么）
- `probability ≤ roll < probability + interrupt_probability`（默认再 0.15）→ **打断**：同样拦截触发消息，只发**打断文本**池里随机一条短文本（默认「打断！/够了！/停！/打住！/别念了！」），绝不附带**复读文本**
- 其余 → 沉默：触发消息照常放行进主链，麦麦可自行决定如何回应

防护与节奏：

- **链终结**（`repeat.chain_ttl_seconds`，默认 1800 秒）：达标掷骰后（含沉默出口）即终结，该（群，**复读文本**）在 TTL 内不再取得新的决策资格，防止一轮群友复读里麦麦多次出声；标记过期后新一轮可重新触发
- **复读群冷却**（`repeat.cooldown_seconds`，默认 120 秒）：复读/打断出口命中后进入冷却，冷却期内链照常累积但不掷骰、不消耗资格；与表情回应的群冷却**分开计时**互不牵扯
- 发送前按 `behavior.delay_min/max`（默认 1~6 秒）随机等待，避免机械秒回

## 表情回应跟风

两级决策，最多掷两次骰：

1. **回应骰**：有人给**他人**的消息贴表情回应 → 以 `bandwagon.probability`（默认 0.25）决定回应；给**麦麦自己**的消息贴 → 以 `bandwagon.self_message_probability`（默认 0.12）决定回应。掷不中就不贴任何表情回应。
2. **反义骰**：回应后，触发表情 ID 命中**反义映射**时，再以 `opposite.probability`（默认 0.35）决定贴**反义回应**还是贴同款；掷不中**回落跟风**贴同款（见 [ADR 0002](docs/adr/0002-exclusive-branches-and-in-memory-slots.md)）。

- 一条消息麦麦最多贴一次（**表情槽**）；同一个群两次贴表情回应间隔不小于 `behavior.cooldown_seconds`（默认 60 秒）
- 贴之前随机等 1~6 秒，避免上报一到就瞬间贴上

内置**反义映射**（`opposite.include_defaults = true` 时生效）：

| | | | | | | | |
|---|---|---|---|---|---|---|---|
| 👍↔👎 | ❤↔💔 | 😂↔😭 | 😄↔😡 | ✅↔❌ | 🔥↔❄ | ⬆↔⬇ | 😍↔🤢 |
| 😈↔😇 | 🤮↔😋 | 🥶↔🥵 | ☀↔🌧 | 🚀↔🐢 | 😊↔😠 | 🎉↔😢 | ➕↔➖ |

映射双向生效。要加自己的对，在 `opposite.extra_pairs` 里按 `"A=B"` 写，可用 emoji 字符或协议端表情 ID：

```toml
extra_pairs = ["🎉=😢", "76=78"]
disabled_emojis = ["👍"]   # 把 👍 从反义映射里摘掉，让它回落到跟风
```

## 上下文注入（实验性，默认关闭）

`context.enabled` 默认 `false`，只管**表情回应域**的注入，整块功能试验性地开、试验性地关。复读域的拦截与注入跟随 `repeat.enabled`，不受这个开关约束（见 [ADR 0006](docs/adr/0006-repeat-direct-send-with-injection-and-intercept.md)）。开启后：

- **注入**：表情回应（含麦麦自己贴的）去重、聚合后作为**独立上下文消息**写入聊天历史（`source_kind="reaction"`，`message_id` 指向被回应消息），麦麦回复时会感知到群里发生过表情回应
- **拦截**：插件挂 `chat.receive.before_process` Hook（blocking 模式），把主程序消息链上的表情回应通知消息 abort 掉，避免适配器转发 notice 时历史里出现两份记录（见 [ADR 0003](docs/adr/0003-reaction-context-injection-with-defensive-intercept.md)）

注入限流里有三个窗口概念，不要混：

- **去重**（`dedup_ttl_seconds`，默认 3600）：同一「回应者 + 被回应消息 + 表情 ID」组合在窗口内只注入一次
- **聚合**（`merge_window_seconds`，默认 30）：同一被回应消息同一表情 ID 的多条回应在此窗口内汇入同一条注入消息，窗口到期写入——「A、B、C 给消息 X 贴了 👍」是一条注入消息而不是三条
- **上限**（`max_reactions_per_message`，默认 10）：单条被回应消息最多产生的注入消息条数，防表情回应风暴撑爆上下文

注入的门禁复用 `[scope]` 的白黑名单；麦麦自己贴的表情回应不受 `ignore_reactors` 影响。注入文本里麦麦自己的显示名来源链：`context.self_display_name` → 主程序配置 `bot.nickname` → 都取不到就不写名字。

```toml
[context]
enabled = false
# 同一（回应者、被回应消息、表情 ID）组合在此窗口内只注入一次
dedup_ttl_seconds = 3600
# 同一被回应消息同一表情 ID 的多条回应在此窗口内汇入同一条注入消息
merge_window_seconds = 30
# 单条被回应消息最多产生的注入消息条数
max_reactions_per_message = 10
# 麦麦自己的显示名；留空自动取主程序 bot.nickname，再取不到则不写名字
self_display_name = ""
```

## 安装

1. 目录整个放进 MaiBot 的 `plugins/`（目录名用插件 id `arcdent.repeater-plugin`，从旧版本迁移见下方迁移段落），无需打包
2. 装依赖：`pip install aiohttp`
3. 复读功能需要配 `[protocol]`：检测与拦截走主程序 hook（blocking 模式），发送走协议端直调（`send_group_msg`），不依赖 webhook 上报目标
4. 表情回应跟风需要两步：
   - 填 `config.toml` 的 `[protocol]`：协议端 OneBot HTTP 地址与 token
   - **给协议端新增一个 HTTP 上报目标**，URL 指向 `http://<插件所在容器>:18081/maibot/emoji_reaction`

第 4 步对表情回应跟风是必需的。麦麦插件 SDK 的事件类型里没有 notice 通道，表情回应事件只能由协议端直接 POST 给插件，理由见 [ADR 0001](docs/adr/0001-webhook-instead-of-sdk-event.md)。如果同机的好友申请插件已经配过一个上报目标，这里要**再加一个**，两者端口不同（18080 / 18081）。`[protocol]` 同时服务于表情回应跟风与复读直发；上报目标只影响表情回应。不配 `[protocol]` 时，表情回应跟风与复读直发都不动作（复读出口命中后拦截与注入照常，直发失败记 warning）。

配了 `webhook.secret` 时，协议端上报需带 `X-Signature: sha1=<hmac>` 头，与协议端的 secret 配置一致即可。注意 SnowLuma 的 HTTP 上报带的是 `Authorization: Bearer` 头而非 `X-Signature`，所以配 SnowLuma 时 `webhook.secret` 应留空，靠 Docker 网络隔离。

## 部署注意：勿开 self-message 上报

协议端 webhook **不要开启 self-message 上报**（SnowLuma 的 `reportSelfMessage` 开关，默认关闭）。本插件的复读/打断出口经协议端直调 `send_group_msg` 发送，直发的消息不走主程序发送链，麦麦知道自己发过什么靠的是插件注入；协议端默认也不把麦麦自己发的群消息上报回主程序。一旦打开 `reportSelfMessage`，直发出去的复读/打断消息会以**普通 user 消息的身份**回流主程序入站链（主程序入站链对 bot 自发消息无过滤），planner 上下文里会出现「麦麦以 user 身份看到自己复读过」的形态污染——同一条复读既在注入记录里是麦麦发的、又在入站记录里是某个 user 发的。保持默认关闭即可，见 [ADR 0006](docs/adr/0006-repeat-direct-send-with-injection-and-intercept.md)。

## 配置：`[repeat]` 节

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 复读独立开关：关闭后仅停用复读，表情回应跟风不受影响；插件总开关关闭时全部停用 |
| `window_seconds` | `120` | 滚动窗口：窗口内归一后相同的**复读文本**按口径累积，早于窗口的历史消息随滚动失效 |
| `threshold` | `3` | 窗口内计数达到该值时**复读链**达标，取得一次决策资格 |
| `distinct_senders` | `true` | 计数口径：开启时仅计不同**发送者**人头；关闭后逐条消息计数 |
| `probability` | `0.6` | **复读**概率：命中则把链内最后一条的原始**复读文本**发出。0 = 从不复读 |
| `interrupt_probability` | `0.15` | **打断**概率：复读骰未中后再掷，命中则发池内随机一条**打断文本**。0 = 从不打断 |
| `interrupt_texts` | 五条内置 | **打断文本**变体池，每次随机一条；全部留空时回落内置默认池并记日志，不会静默哑火 |
| `chain_ttl_seconds` | `1800` | **链终结**标记保留时长：掷骰后同一**复读文本**在此时长内不再触发新一轮，过期后重新累积可再达标。0 = 不做链终结 |
| `cooldown_seconds` | `120` | 复读专用群冷却，与 `[behavior]` 里表情回应的冷却互相独立。冷却期内链达标不掷骰、不消耗资格。0 = 不限 |
| `max_text_length` | `200` | 超过此长度的文本不产出**复读文本**、不参与累积。0 = 不限 |
| `max_chains` | `2000` | 内存中**复读链**条数上界，超出后丢弃最旧的链 |

`[scope]` 节里与复读相关的名单：

- `ignore_senders`：忽略这些 QQ 发出的消息，其文本不进入**复读链**累积；通常填其他机器人，防止互相复读。与 `ignore_reactors`（忽略贴表情回应的**回应者**）是两个名单、两个概念。

已知盲区（复读）：

- **自定义命令前缀**：复读检测按文本以 `/` 开头排除命令消息——主程序的命令标记在 `before_process` 时点尚未写入，只能按前缀判，而主程序默认前缀正是 `/`。若部署把命令前缀改成了别的字符，那些命令消息会被当成普通文本参与**复读链**累积
- **图片与表情包不参与复读**：只有纯文本消息产出**复读文本**；@、引用、图片、表情包消息一律不参与。图片按指纹比对同一并重发留作未来扩展

## 迁移（从 sending-emojis-plugin）

**BREAKING**：manifest id 从 `arcdent.sending-emojis-plugin` 变更为 `arcdent.repeater-plugin`，对 MaiBot 而言是一个新插件，老部署不会自动升级，需手动迁移：

1. **MaiBot 容器内**：删除旧插件目录 `data/MaiMBot/plugins/arcdent.sending-emojis-plugin/`，将本仓库放进 `data/MaiMBot/plugins/arcdent.repeater-plugin/`（目录名即新 id），重启主容器让 MaiBot 重新发现插件
2. **配置**：旧 `config.toml` 直接复制到新目录即可——旧字段全部兼容，新增的 `[repeat]` 节与 `scope.ignore_senders` 缺省时用默认值，无需改写
3. **GitHub 仓库改名**（手动）：仓库 Settings → General → Repository name 改为 `repeater-plugin`；GitHub 会对旧 URL 自动重定向，本地执行 `git remote set-url origin git@github.com:<账号>/repeater-plugin.git` 同步远端地址
4. **协议端上报目标**：webhook 的地址、端口、路径都没变，无需改动
5. **回滚**：重装旧版本目录即可，配置向后兼容（新增节缺省即可生效）

## 部署（arclinux 服务器）

服务器：`arclinux`（SSH 别名），MaiBot 容器化部署在 `/home/arc/maimai/MaiBot`，第三方插件目录（宿主机）为 `data/MaiMBot/plugins/<插件ID>/`（挂载进容器 `/MaiMBot/plugins/`）。协议端为 `maim-bot-snowluma` 容器（OneBot HTTP :3000），表情回应上报已配置指向 `http://maim-bot-core:18081/maibot/emoji_reaction`。

```bash
# 1. 推送插件文件（排除本地开发与内部文件；目录名即插件 id）
rsync -av --delete \
  --exclude .git --exclude .local --exclude .serena --exclude .superpowers \
  --exclude .pytest_cache --exclude .ruff_cache --exclude __pycache__ \
  --exclude tests --exclude docs/superpowers \
  --exclude conftest.py --exclude pyrightconfig.json \
  ./ arclinux:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.repeater-plugin/

# 2. 在服务器上写部署配置（含协议端 token，勿入库；未写的节用默认值）
ssh arclinux 'cat > /home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.repeater-plugin/config.toml << "EOF"
[webhook]
secret = ""    # SnowLuma 上报带 Authorization 头，X-Signature 校验不适用，留空

[protocol]
base_url = "http://maim-bot-snowluma:3000"
access_token = "<snowluma OneBot HTTP accessToken>"
self_qq = ""   # 留空启动时自动获取

[context]
enabled = false   # 实验性功能，只管表情回应域注入（复读域注入跟随 repeat.enabled），按需开启
EOF'

# 3. 重启主容器让 MaiBot 重新发现插件
ssh arclinux 'docker restart maim-bot-core'

# 4. 验证加载
ssh arclinux 'docker logs --since 2m maim-bot-core | grep -i "复读\|表情"'
```

首次从旧 id 迁移时，先删除旧目录再执行上面的推送：

```bash
ssh arclinux 'rm -rf /home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.sending-emojis-plugin'
```

部署后验证链路：

- **复读**：去群里三个人接同一句话（如「草」），日志应出现 `复读链达标` → `已直发复读文本`（或 `已直发打断文本`）
- **表情回应**：给任意消息贴个表情回应，日志应出现 `表情回应上报已监听` → `表情回应触发`；若出现 `表情回应上报形态不认识，已忽略` 则协议端形态变了，把日志里的原始 JSON 贴回来

## 首次部署验证

上报体形态与动作参数已对照 SnowLuma 源码核实（`packages/onebot/src/event-converter/to-notice.ts`、`actions/extended.ts`、`actions/message.ts`），字段布局与本插件一致；复读检测的消息形态取自主程序 hook 侧（`src/plugin_runtime/host/message_utils.py`）。装好后分两条链路验证：

- 出现 `复读链达标` / `已直发复读文本` → 复读链路通了（三人接龙一次即可验证）
- 出现 `表情回应上报已监听` → webhook 端点起来了
- 出现 `表情回应触发` / `已跟风贴表情` → 表情回应链路通了
- 出现 `表情回应上报形态不认识，已忽略: {...}` → 协议端版本或实现变了，把日志里那段原始 JSON 贴出来，按实测补 `reactions.py` 的解析分支
- 出现 `复读检测收到形态异常的消息`（debug 级，量异常大时） → 主程序消息形态漂移，按日志片段核对 `repeats.py` 的解析分支
- 出现 `协议端不支持 set_msg_emoji_like（retcode=1404）` → 协议端不是 NapCat/SnowLuma 系实现，贴表情回应动作不可用，插件会自动停止尝试（复读不受影响）

## 已知边界

- 只处理群聊。复读链与表情回应都只认群消息；SnowLuma 的 notice 类型全集里只有 `group_msg_emoji_like`，没有私聊版本，插件对没有 `group_id` 的表情回应上报静默忽略。
- 复读链、终结标记、表情槽与冷却只存内存，插件热重载或麦麦重启后清零，短时间内可能对重启前处理过的消息再出一次声。这是有意为之，见 ADR 0002。
- 判断「有人贴的是不是麦麦自己的消息」依赖协议端 `get_msg`，而它只能查到协议端存储过的消息（协议端重启前的旧消息查不到）。查不到时按他人消息处理。
- 取不到麦麦自己的 QQ 号时（协议端 `get_login_info` 不可用且没手填 `protocol.self_qq`），上面那条判断整体退化为「都按他人消息处理」；复读侧麦麦自己的消息按 user_id 判定，不受此影响。
- `_manifest.json` 里的 `id` / `author` / `urls.repository` 目前按本地账号名填的，提交插件中心前需核对成真实 GitHub 账号（仓库改名后同步更新 repository URL）。

## 开发

```bash
uv run --with pytest --with aiohttp pytest tests/ -q
```

`reactions.py`（表情回应域）与 `repeats.py`（复读域）都是纯逻辑层，不依赖 `maibot_sdk`，全部可单测；`plugin.py` 只做编排与 I/O。新增可测逻辑按领域写进对应纯逻辑层。

## 更名说明

本项目原名 `sending-emojis-plugin`，聚合复读功能后更名 `repeater-plugin`。旧名的类名、manifest id、目录名均已按上文迁移段落变更。
