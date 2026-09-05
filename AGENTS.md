# AGENTS.md

MaiBot 插件：聚合两个群聊行为——群内复读链达标后，麦麦按概率复读（把复读文本原样发出）或按概率发打断文本唱反调；有人给某条消息贴了表情回应后，麦麦按概率跟风贴同款，或按概率贴反义回应。

## 结构

- `plugin.py` — 插件主类：上报监听、门禁与掷骰编排、协议端直调、复读观测与发送、启动自检
- `reactions.py` — 表情回应域纯逻辑层（上报解析、表情 ID 归一、反义映射、跟风/反义决策、表情槽与冷却），不依赖 SDK，可单测
- `repeats.py` — 复读域纯逻辑层（复读文本宽松归一、消息资格判定、复读链状态机、三出口决策），不依赖 SDK，可单测
- `config.py` — 配置模型（plugin/webhook/protocol/bandwagon/opposite/repeat/behavior/context/scope）
- `config.toml` — 默认配置，值必须与 `config.py` 的 default 一致
- `tests/` — pytest（fake maibot_sdk 注入，`uv run --with pytest --with aiohttp pytest tests/ -q`）
- `docs/emoji-id-reference.md` — 表情 ID 两套编号体系（QQ 小黄脸编号 / Unicode 码点）查表与边界
- `docs/adr/` — 0001 走 webhook 不走 SDK 事件链；0002 分支严格互斥 + 内存态；0003 防御式拦截通知 + 插件自建注入；0004 定时 flush 兜底 + 失败回滚；0005 复读检测走 before_process observe 通路 + `ctx.send.text` 发送
- `CONTEXT.md` — 领域术语表

## 关键规则

### 复读域

- 复读三出口**严格互斥**：一次掷骰只命中复读 / 打断 / 沉默之一，决策逻辑全在 `repeats.decide_repeat`；打断出口只发打断文本、绝不附带复读文本。改这个语义要先改 ADR 0005 与 change design D3/D5
- **链终结**与表情槽同构，是自我循环的**必需**防护：先写终结标记再掷骰（`RepeatState.record` 同步原子段内完成），任何情况下不得先掷骰后写标记；`record` 必须保持纯同步方法（无 await 点），否则并发 observe 任务可在同一链上双触发
- 复读群冷却与表情回应的群冷却**分开计时、互不牵扯**（各自 state 实例）；沉默出口消耗资格（链终结同样成立）但不进冷却，发送成败不影响已进入的冷却
- 复读检测走 `chat.receive.before_process` hook 的 **observe 模式**（零阻塞主链，决策依据见 ADR 0005）；同 hook 上另有 blocking 模式的防御式拦截 handler（ADR 0003），同名 hook 两个 handler 组件并存是有意设计
- 复读/打断发送走 SDK 标准能力 `ctx.send.text`（消息进麦麦上下文，麦麦知道自己在复读），stream_id 复用 `_get_stream_id` 群号缓存；发送异常或失败记 warning 不重试不熔断
- hook 消息解析失败打 **debug 不打 warning**：hook 每条群消息都过，warning 会刷屏；这与 webhook 上报形态不认识必须打 warning（ADR 0001 的部署验证依赖）**口径相反、理由各自成立**，改哪边都要先想清楚。消息段形态由 QQ 客户端决定、高度稳定；上报形态由协议端决定、随版本漂移
- 命令消息按文本 `/` 前缀排除：主程序命令标记在 before_process 时点尚未写入，只能按前缀判；自定义命令前缀的部署属已知盲区（README 有提示）
- 只有纯文本消息产出**复读文本**（trim 后非空）；含 @、引用、图片、表情包段的消息一律不参与复读链累积；比对前宽松归一（trim / 空白折叠 / 大小写不敏感），复读发送用原始文本、不用归一产物；归一规则与链累积相互独立

### 表情回应域

- **表情回应**（贴在消息上，`set_msg_emoji_like`）与**表情包**（`ctx.send.emoji` 发的图片消息）是两个概念，代码与文档不得混用「表情」裸词
- 反义分支与跟风分支**严格互斥**：命中反义映射的表情不掷跟风、不回落，改这个语义要先改 ADR 0002
- `set_msg_emoji_like` 必须带 `"set": True`，NapCat 4.17.25 起缺这个参数请求被静默忽略
- 表情槽是自我循环的**必需**防护：先占槽再发，任何情况下不得先发后占；识别 bot 自身 QQ 只是额外优化
- 协议端调用失败语义：不可达返回 `None`；HTTP 200 + `status != ok` 视为失败；`retcode=1404` 表示动作不存在，此时置 `_emoji_action_supported = False` 停止后续尝试
- 上报形态不认识时必须打 warning 并附原始 payload 片段，不得静默丢弃（ADR 0001 的部署验证依赖这条日志）
- 表情 ID 归一一律走 `normalize_emoji_id`（纯数字原样，emoji 字符取首码点）。**这不是美化，删不得**：协议端上报 Unicode 表情给的是字符本身（`'⭐'`），而发送时按 `code.length > 3` 判类型（≤3 位当 QQ 小黄脸、更长当 Unicode 码点），归一正好抹平这个不对称。依据见 `REFERENCE/snowluma` 的 `msg-push/decoders/event-0x2dc.ts` 与 `oidb-services/reaction/set-reaction.ts`
- 内置反义映射只用 Unicode emoji，不臆造 QQ 私有编号的语义

### 公共

- 协议端行为有疑问先读源码：SnowLuma 已 clone 在 `/Users/arcdent/github/REFERENCE/snowluma`，表情回应相关在 `packages/onebot/src/event-converter/to-notice.ts`（上报）、`packages/onebot/src/actions/extended.ts`（`set_msg_emoji_like`）、`packages/onebot/src/actions/message.ts`（`get_msg`）；主程序 hook 消息形态看 `REFERENCE/MaiBot/src/plugin_runtime/host/message_utils.py`
- 上下文注入默认关闭（`context.enabled`），整块试、整块关；注入路径复用 `scope` 白黑名单，但麦麦自己贴的回应不受 `ignore_reactors` 影响
- 注入限流里**去重 ≠ 聚合**：去重（`dedup_ttl_seconds`）管「同一回应者+被回应消息+表情只注入一次」，聚合（`merge_window_seconds`）管「同一被回应消息同一表情的多条回应汇入一条注入消息」，`max_reactions_per_message` 兜住单条消息的注入条数
- 注入文本显示名来源链：`context.self_display_name` → 主程序 `bot.nickname` → 空（不写名字）
- 新增纯逻辑按领域写进对应纯逻辑层并加单测（表情回应域进 `reactions.py`，复读域进 `repeats.py`，两域并列、不混装）；不往 `plugin.py` 里塞可测逻辑

## 验证

```bash
uv run --with pytest --with aiohttp pytest tests/ -q       # 全部通过
uv run --with pyright --with aiohttp pyright              # 生产代码零错误
```

`pyrightconfig.json` 只检查四个生产文件（config/plugin/reactions/repeats），且关掉 `reportMissingImports`：真实 `maibot_sdk` 只存在于 MaiBot 容器内，本机永远解析不到；测试目录里的替身（`FakeRequest`、故意传非法概率值）也会产生大量与正确性无关的类型噪声。

`ruff check` 下有四类有意偏离，改代码时不要「顺手修掉」：

- `BLE001`（宽捕获 `Exception`）— webhook 回调与后台任务里任何未捕获异常都会拖垮监听或产生孤儿任务，必须兜住并记日志
- `N999`（模块名含连字符）— MaiBot 插件目录命名约定，改名会破坏插件加载
- `SIM103`（`group_allowed` 未直接返回条件）— 早返回结构与前面的黑名单检查对称，改成 `not (white and ...)` 是双重否定，可读性更差
- `SIM117`（`aiohttp` 嵌套 `async with`）— 与同机 `friend-request-handler` 的 `_call_protocol` 写法保持一致，便于对读

## 测试注意

- 插件行为依赖 `random.random()`。**「断言不触发」的用例必须把概率拉满**（`make_plugin` 已默认设为 1.0，复读侧同样：复读概率 1.0、打断概率 0），否则门禁失效时用例仍会按默认概率蒙对，回归检测形同虚设。
- 涉及表情槽或冷却的用例传时间戳要用 `time.time()`，不能传 `0.0`：`_evaluate` 内部取当前时间，传 0 会让记录直接被 TTL 判成过期。
- 「断言不触发」的沉默用例反向操作：把复读概率与打断概率都归零，保证掷骰必落沉默。
