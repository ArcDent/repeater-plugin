# 表情跟风（sending-emojis-plugin）

MaiBot 插件。群里有人给某条消息贴**表情回应**（消息气泡下方那排小表情）后，麦麦按概率跟着贴同款；如果那个表情在**反义映射**里，则按另一套概率贴一个语义相反的表情。

不涉及表情包发送——那是 `ctx.send.emoji` 的事，本插件只碰表情回应。术语定义见 [CONTEXT.md](CONTEXT.md)。

## 行为规则

两级决策，最多掷两次骰：

1. **回应骰**：有人给**他人**的消息贴表情 → 以 `bandwagon.probability`（默认 0.25）决定回应；给**麦麦自己**的消息贴 → 以 `bandwagon.self_message_probability`（默认 0.12）决定回应。掷不中就不贴任何表情。
2. **反义骰**：回应后，触发表情若命中**反义映射**，再以 `opposite.probability`（默认 0.35）决定贴相反表情还是贴同款；掷不中**回落跟风**贴同款（见 [ADR 0002](docs/adr/0002-exclusive-branches-and-in-memory-slots.md)）。

- 一条消息麦麦最多贴一次；同一个群两次贴表情间隔不小于 `behavior.cooldown_seconds`（默认 60 秒）
- 贴之前随机等 1~6 秒，避免上报一到就瞬间贴上

内置反义映射（`opposite.include_defaults = true` 时生效）：

| | | | | | | | |
|---|---|---|---|---|---|---|---|
| 👍↔👎 | ❤↔💔 | 😂↔😭 | 😄↔😡 | ✅↔❌ | 🔥↔❄ | ⬆↔⬇ | 😍↔🤢 |
| 😈↔😇 | 🤮↔😋 | 🥶↔🥵 | ☀↔🌧 | 🚀↔🐢 | 😊↔😠 | 🎉↔😢 | ➕↔➖ |

映射双向生效。要加自己的对，在 `opposite.extra_pairs` 里按 `"A=B"` 写，可用 emoji 字符或协议端表情编号：

```toml
extra_pairs = ["🎉=😢", "76=78"]
disabled_emojis = ["👍"]   # 把 👍 从反义映射里摘掉，让它回落到跟风
```

## 上下文注入（实验性，默认关闭）

`context.enabled` 默认 `false`，整块功能试验性地开、试验性地关。开启后：

- **注入**：表情回应（含麦麦自己贴的）去重、聚合后作为**独立上下文消息**写入聊天历史（`source_kind="reaction"`，`message_id` 指向被回应消息），麦麦回复时会感知到群里发生过表情回应
- **拦截**：插件挂 `chat.receive.before_process` Hook，把主程序消息链上的表情回应通知消息 abort 掉，避免适配器转发 notice 时历史里出现两份记录（见 [ADR 0003](docs/adr/0003-reaction-context-injection-with-defensive-intercept.md)）

注入限流里有三个窗口概念，不要混：

- **去重**（`dedup_ttl_seconds`，默认 3600）：同一「回应者 + 被回应消息 + 表情」组合在窗口内只注入一次
- **聚合**（`merge_window_seconds`，默认 30）：同一被回应消息同一表情的多条回应在此窗口内汇入同一条注入消息，窗口到期写入——「A、B、C 给消息 X 贴了 👍」是一条注入消息而不是三条
- **上限**（`max_reactions_per_message`，默认 10）：单条被回应消息最多产生的注入消息条数，防表情回应风暴撑爆上下文

注入的门禁复用 `[scope]` 的白黑名单；麦麦自己贴的表情回应不受 `ignore_reactors` 影响。注入文本里麦麦自己的显示名来源链：`context.self_display_name` → 主程序配置 `bot.nickname` → 都取不到就不写名字。

```toml
[context]
enabled = false
# 同一（回应者、被回应消息、表情）组合在此窗口内只注入一次
dedup_ttl_seconds = 3600
# 同一被回应消息同一表情的多条回应在此窗口内汇入同一条注入消息
merge_window_seconds = 30
# 单条被回应消息最多产生的注入消息条数
max_reactions_per_message = 10
# 麦麦自己的显示名；留空自动取主程序 bot.nickname，再取不到则不写名字
self_display_name = ""
```

## 安装

1. 目录整个放进 MaiBot 的 `plugins/`，无需打包
2. 装依赖：`pip install aiohttp`
3. 填 `config.toml` 的 `[protocol]`：协议端 OneBot HTTP 地址与 token
4. **给协议端新增一个 HTTP 上报目标**，URL 指向 `http://<插件所在容器>:18081/maibot/emoji_reaction`

第 4 步是必需的。麦麦插件 SDK 的事件类型里没有 notice 通道，表情回应事件只能由协议端直接 POST 给插件，理由见 [ADR 0001](docs/adr/0001-webhook-instead-of-sdk-event.md)。如果同机的好友申请插件已经配过一个上报目标，这里要**再加一个**，两者端口不同（18080 / 18081）。

配了 `webhook.secret` 时，协议端上报需带 `X-Signature: sha1=<hmac>` 头，与协议端的 secret 配置一致即可。注意 SnowLuma 的 HTTP 上报带的是 `Authorization: Bearer` 头而非 `X-Signature`，所以配 SnowLuma 时 `webhook.secret` 应留空，靠 Docker 网络隔离。

## 部署（arclinux 服务器）

服务器：`arclinux`（SSH 别名），MaiBot 容器化部署在 `/home/arc/maimai/MaiBot`，第三方插件目录（宿主机）为 `data/MaiMBot/plugins/<插件ID>/`（挂载进容器 `/MaiMBot/plugins/`）。协议端为 `maim-bot-snowluma` 容器（OneBot HTTP :3000），表情回应上报已配置指向 `http://maim-bot-core:18081/maibot/emoji_reaction`。

```bash
# 1. 推送插件文件（排除本地开发与内部文件）
rsync -av --delete \
  --exclude .git --exclude .local --exclude .serena \
  --exclude .pytest_cache --exclude .ruff_cache --exclude __pycache__ \
  --exclude tests --exclude docs/superpowers \
  ./ arclinux:/home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.sending-emojis-plugin/

# 2. 在服务器上写部署配置（含协议端 token，勿入库）
ssh arclinux 'cat > /home/arc/maimai/MaiBot/data/MaiMBot/plugins/arcdent.sending-emojis-plugin/config.toml << "EOF"
[webhook]
secret = ""    # SnowLuma 上报带 Authorization 头，X-Signature 校验不适用，留空

[protocol]
base_url = "http://maim-bot-snowluma:3000"
access_token = "<snowluma OneBot HTTP accessToken>"
self_qq = ""   # 留空启动时自动获取

[context]
enabled = false   # 实验性功能，按需开启
EOF'

# 3. 重启主容器让 MaiBot 重新发现插件
ssh arclinux 'docker restart maim-bot-core'

# 4. 验证加载
ssh arclinux 'docker logs --since 2m maim-bot-core | grep -i "表情\|sending"'
```

部署后验证链路：去群里给任意消息贴个表情，日志应出现 `表情回应上报已监听` → `表情回应触发` / `已跟风贴表情`；若出现 `表情回应上报形态不认识，已忽略` 则协议端形态变了，把日志里的原始 JSON 贴回来。

## 首次部署验证

上报体形态与动作参数已对照 SnowLuma 源码核实（`packages/onebot/src/event-converter/to-notice.ts`、`actions/extended.ts`、`actions/message.ts`），字段布局与本插件一致。装好后去群里给任意消息贴个表情，看日志确认链路：

- 出现 `表情回应上报已监听` → 端点起来了
- 出现 `表情回应触发` / `已跟风贴表情` → 整条链路通了
- 出现 `表情回应上报形态不认识，已忽略: {...}` → 协议端版本或实现变了，把日志里那段原始 JSON 贴出来，按实测补 `reactions.py` 的解析分支
- 出现 `协议端不支持 set_msg_emoji_like（retcode=1404）` → 协议端不是 NapCat/SnowLuma 系实现，贴表情动作不可用，插件会自动停止尝试

## 已知边界

- 只处理群聊。SnowLuma 的 notice 类型全集里只有 `group_msg_emoji_like`，没有私聊版本；插件对没有 `group_id` 的表情回应上报静默忽略。
- 表情槽与冷却只存内存，插件热重载或麦麦重启后清零，短时间内可能对重启前处理过的消息再贴一次。这是有意为之，见 ADR 0002。
- 判断「有人贴的是不是麦麦自己的消息」依赖协议端 `get_msg`，而它只能查到协议端存储过的消息（协议端重启前的旧消息查不到）。查不到时按他人消息的概率处理。
- 取不到麦麦自己的 QQ 号时（协议端 `get_login_info` 不可用且没手填 `protocol.self_qq`），上面那条判断整体退化为「都按他人消息处理」。
- `_manifest.json` 里的 `id` / `author` / `urls.repository` 目前按本地账号名填的，提交插件中心前需核对成真实 GitHub 账号。

## 开发

```bash
uv run --with pytest --with aiohttp pytest tests/ -q
```

`reactions.py` 是纯逻辑层，不依赖 `maibot_sdk`，全部可单测；`plugin.py` 只做编排与 I/O。新增可测逻辑先写进 `reactions.py`。