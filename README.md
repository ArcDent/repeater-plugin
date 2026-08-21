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

## 安装

1. 目录整个放进 MaiBot 的 `plugins/`，无需打包
2. 装依赖：`pip install aiohttp`
3. 填 `config.toml` 的 `[protocol]`：协议端 OneBot HTTP 地址与 token
4. **给协议端新增一个 HTTP 上报目标**，URL 指向 `http://<插件所在容器>:18081/maibot/emoji_reaction`

第 4 步是必需的。麦麦插件 SDK 的事件类型里没有 notice 通道，表情回应事件只能由协议端直接 POST 给插件，理由见 [ADR 0001](docs/adr/0001-webhook-instead-of-sdk-event.md)。如果同机的好友申请插件已经配过一个上报目标，这里要**再加一个**，两者端口不同（18080 / 18081）。

配了 `webhook.secret` 时，协议端上报需带 `X-Signature: sha1=<hmac>` 头，与协议端的 secret 配置一致即可。

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