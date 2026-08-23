# AGENTS.md

MaiBot 插件：群里有人贴表情回应后，麦麦按概率跟风贴同款，或按概率贴语义相反的表情。

## 结构

- `plugin.py` — 插件主类：上报监听、门禁与掷骰编排、协议端直调、启动自检
- `reactions.py` — 纯逻辑层（上报解析、表情 ID 归一、反义映射、决策、表情槽与冷却），不依赖 SDK，可单测
- `config.py` — 配置模型（plugin/webhook/protocol/bandwagon/opposite/behavior/context/scope）
- `config.toml` — 默认配置，值必须与 `config.py` 的 default 一致
- `tests/` — pytest（fake maibot_sdk 注入，`uv run --with pytest --with aiohttp pytest tests/ -q`）
- `docs/emoji-id-reference.md` — 表情 ID 两套编号体系（QQ 小黄脸编号 / Unicode 码点）查表与边界
- `docs/adr/` — 0001 走 webhook 不走 SDK 事件链；0002 分支严格互斥 + 内存态；0003 防御式拦截通知 + 插件自建注入
- `CONTEXT.md` — 领域术语表

## 关键规则

- **表情回应**（贴在消息上，`set_msg_emoji_like`）与**表情包**（`ctx.send.emoji` 发的图片消息）是两个概念，代码与文档不得混用「表情」裸词
- 反义分支与跟风分支**严格互斥**：命中反义映射的表情不掷跟风、不回落，改这个语义要先改 ADR 0002
- `set_msg_emoji_like` 必须带 `"set": True`，NapCat 4.17.25 起缺这个参数请求被静默忽略
- 表情槽是自我循环的**必需**防护：先占槽再发，任何情况下不得先发后占；识别 bot 自身 QQ 只是额外优化
- 协议端调用失败语义：不可达返回 `None`；HTTP 200 + `status != ok` 视为失败；`retcode=1404` 表示动作不存在，此时置 `_emoji_action_supported = False` 停止后续尝试
- 上报形态不认识时必须打 warning 并附原始 payload 片段，不得静默丢弃（ADR 0001 的部署验证依赖这条日志）
- 表情 ID 归一一律走 `normalize_emoji_id`（纯数字原样，emoji 字符取首码点）。**这不是美化，删不得**：协议端上报 Unicode 表情给的是字符本身（`'⭐'`），而发送时按 `code.length > 3` 判类型（≤3 位当 QQ 小黄脸、更长当 Unicode 码点），归一正好抹平这个不对称。依据见 `REFERENCE/snowluma` 的 `msg-push/decoders/event-0x2dc.ts` 与 `oidb-services/reaction/set-reaction.ts`
- 内置反义映射只用 Unicode emoji，不臆造 QQ 私有编号的语义
- 协议端行为有疑问先读源码：SnowLuma 已 clone 在 `/Users/arcdent/github/REFERENCE/snowluma`，表情回应相关在 `packages/onebot/src/event-converter/to-notice.ts`（上报）、`packages/onebot/src/actions/extended.ts`（`set_msg_emoji_like`）、`packages/onebot/src/actions/message.ts`（`get_msg`）
- 上下文注入默认关闭（`context.enabled`），整块试、整块关；注入路径复用 `scope` 白黑名单，但麦麦自己贴的回应不受 `ignore_reactors` 影响
- 注入限流里**去重 ≠ 聚合**：去重（`dedup_ttl_seconds`）管「同一回应者+被回应消息+表情只注入一次」，聚合（`merge_window_seconds`）管「同一被回应消息同一表情的多条回应汇入一条注入消息」，`max_reactions_per_message` 兜住单条消息的注入条数
- 注入文本显示名来源链：`context.self_display_name` → 主程序 `bot.nickname` → 空（不写名字）
- 新增纯逻辑先写进 `reactions.py` 并加单测；不往 `plugin.py` 里塞可测逻辑

## 验证

```bash
uv run --with pytest --with aiohttp pytest tests/ -q       # 全部通过
uv run --with pyright --with aiohttp pyright              # 生产代码零错误
```

`pyrightconfig.json` 只检查三个生产文件，且关掉 `reportMissingImports`：真实 `maibot_sdk` 只存在于 MaiBot 容器内，本机永远解析不到；测试目录里的替身（`FakeRequest`、故意传非法概率值）也会产生大量与正确性无关的类型噪声。

`ruff check` 下有四类有意偏离，改代码时不要「顺手修掉」：

- `BLE001`（宽捕获 `Exception`）— webhook 回调与后台任务里任何未捕获异常都会拖垮监听或产生孤儿任务，必须兜住并记日志
- `N999`（模块名含连字符）— MaiBot 插件目录命名约定，改名会破坏插件加载
- `SIM103`（`group_allowed` 未直接返回条件）— 早返回结构与前面的黑名单检查对称，改成 `not (white and ...)` 是双重否定，可读性更差
- `SIM117`（`aiohttp` 嵌套 `async with`）— 与同机 `friend-request-handler` 的 `_call_protocol` 写法保持一致，便于对读

## 测试注意

- 插件行为依赖 `random.random()`。**「断言不触发」的用例必须把概率拉满**（`make_plugin` 已默认设为 1.0），否则门禁失效时用例仍会按默认概率蒙对，回归检测形同虚设。
- 涉及表情槽或冷却的用例传时间戳要用 `time.time()`，不能传 `0.0`：`_evaluate` 内部取当前时间，传 0 会让记录直接被 TTL 判成过期。