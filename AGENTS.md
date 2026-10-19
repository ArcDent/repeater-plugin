# AGENTS.md

MaiBot 插件：聚合两个群聊行为——群内复读链达标后，麦麦掷一次骰按**出口权重**选中复读（把复读文本原样发出）、打断（发打断文本唱反调）或沉默之一；有人给某条消息贴了表情回应后，麦麦按概率跟风贴同款，或按概率贴反义回应。

## 结构

- `plugin.py` — 插件主类：上报监听、表情回应门禁与掷骰、复读拦截与三出口编排、协议端直发、上下文注入与「群 → 活跃聊天流」观察、启动自检
- `reactions.py` — 表情回应域纯逻辑层（上报解析、表情 ID 归一、反义映射、跟风/反义决策、表情槽与冷却），不依赖 SDK，可单测
- `repeats.py` — 复读域纯逻辑层（复读文本宽松归一、消息资格判定、复读链状态机、三出口决策、注入文案构造），不依赖 SDK，可单测
- `config.py` — 配置模型（plugin/webhook/protocol/bandwagon/opposite/repeat/behavior/context/scope）
- `config.toml` — 默认配置，值必须与 `config.py` 的 default 一致
- `tests/` — pytest（fake maibot_sdk 注入，`uv run --with pytest --with aiohttp pytest tests/ -q`）
- `docs/emoji-id-reference.md` — 表情 ID 两套编号体系（QQ 小黄脸编号 / Unicode 码点）查表与边界
- `docs/adr/` — 0001 走 webhook 不走 SDK 事件链；0002 反义 / 跟风两级决策（反义骰不中回落跟风）+ 内存态；0003 防御式拦截通知 + 插件自建注入；0004 定时 flush 兜底 + 失败回滚；0005 复读检测走 before_process hook（检测层仍有效，observe 模式与 `ctx.send.text` 发送决策被 0006 取代）；0006 复读/打断出口协议端直发 + planner 拦截 + 上下文注入（决策 3 的延迟沿用与串行编舞被 0008 取代）；0007 复读三出口改归一化出口权重（取代双概率配置口径）；0008 复读域独立延迟 + 触发消息注入与延迟并发；0009 转发域双路径并存（自动掷骰 + 请求转发，共享已投递状态）；0010 自动转发协议端直发 + 投递前目标群可用性校验（陈旧 stream 路由故障背景）；0011 自动转发改在摘要就绪后触发 + 同步段只留门禁与字节快照 + 直发/决策双模式；0012 转发域配置收敛（入口提示固化开启、查看失败即降级）+ 富摘要占位观测——0009~0012 属转发域，该域已整域删除，四条仅作历史记录；0013 全项目重构范围调研（status: research-draft，调研草案非已接受 ADR）；0014 转发功能独立成插件（status: proposed，阶段一提案）——0013/0014 均非现行行为依据；0015 转发域整体删除、改由独立插件承担（补记 0.9.0 的决定与原因，0009–0014 由它转为历史记录）
- `CONTEXT.md` — 领域术语表

## 关键规则

### 复读域

- 复读三出口**严格互斥**（ADR 0007）：链达标后掷一次骰，按**出口权重**（`weight_repeat`/`weight_interrupt`/`weight_silence`）归一化选中复读 / 打断 / 沉默恰好一个，决策逻辑全在 `repeats.decide_repeat`；权重只有比例意义、同比例放大等价，三权重全为 0 时视为沉默、不掷骰；打断出口只发打断文本、绝不附带复读文本。改这个语义要先改 ADR 0005/0006/0007 与 change repeat-exit-weights-and-faster-send design D1
- **链终结**与表情槽同构，是自我循环的**必需**防护：先写终结标记再掷骰（`RepeatState.record` 同步原子段内完成），任何情况下不得先掷骰后写标记；`record` 必须保持纯同步方法（无 await 点），否则并发 handler 调用可在同一链上双触发（blocking 串行下该担忧天然消失，同步规则不放松，ADR 0006）
- 复读群冷却与表情回应的群冷却**分开计时、互不牵扯**（插件各持一个 `GroupCooldown` 实例）；沉默出口消耗资格（链终结同样成立）但不进冷却，发送成败不影响已进入的冷却
- 复读检测走 `chat.receive.before_process` hook 的 **blocking 模式**（`repeat_detector`，`order="late"`，仍在 `guard_reaction_notify` 之后，被 guard 拦的通知消息到不了本 handler；同 hook 两个 blocking handler 组件并存是有意设计，防御式拦截 handler 见 ADR 0003）
- **planner 拦截**（ADR 0006）：复读/打断出口命中即对触发消息返回 `abort`（不写库、不进上下文、不进 planner，杜绝双重发言）；沉默出口 / 非候选 / `repeat.enabled=false` → `continue` 放行；handler 同步段纯内存判定无 IO，未捕获异常记 warning 返回 continue（宁可漏一条拦截，不可干扰主程序消息处理），`CancelledError` 照常上抛
- 复读/打断发送走**协议端直发** `send_group_msg`（`{group_id, message: [text 段]}`），不走 `ctx.send.text`——直发只需群号，与 stream_id 彻底解耦（stream 仅注入需要）；发送异常或失败（不可达 / `protocol_call_failed`）记 warning 不重试不熔断，任务止步、不做行为文本注入
- 出口命中后的**后台编舞**（ADR 0006/0008，design D4）：触发消息注入（**无条件**，失败记 warning 继续）与延迟（复读段 `repeat.delay_min/max_seconds`，默认 0.3/1.5，与表情回应域的 `behavior.delay_min/max` 互不牵扯）经 `asyncio.gather` **并发**（`return_exceptions=True`，异常项记 warning 继续，可见延迟 = max(注入 RTT, 延迟)）→ 协议端直发（失败止步）→ 行为文本注入（仅直发成功）；两路注入均走 `maisaka.context.append`，`source_kind` 为 `repeat_trigger` / `repeat` / `interrupt`，文案构造在 `repeats.py`（`build_trigger_visible_text` / `build_behavior_visible_text`，发送者显示名按群名片 → 昵称 → QQ 号三级回落）；不复用表情回应域的 `InjectionState`（复读注入是即时一次性记录，无聚合无重试）；注入失败不重试、不回滚拦截
- 复读域的拦截、直发与注入整体跟随 `repeat.enabled`（关闭时不拦截不注入不发送）；`protocol.base_url` 未配置时同样整体放行（发不出去就不拦截，否则触发消息白白消失）。与 `context.enabled` **解耦**——后者只管表情回应域注入，两域并列不混装（design D4）
- hook 消息解析失败打 **debug 不打 warning**：hook 每条群消息都过，warning 会刷屏；这与 webhook 上报形态不认识必须打 warning（ADR 0001 的部署验证依赖）**口径相反、理由各自成立**，改哪边都要先想清楚。消息段形态由 QQ 客户端决定、高度稳定；上报形态由协议端决定、随版本漂移
- 命令消息按文本 `/` 前缀排除：主程序命令标记在 before_process 时点尚未写入，只能按前缀判；自定义命令前缀的部署属已知盲区（README 有提示）
- 只有纯文本消息产出**复读文本**（trim 后非空）；含 @、引用、图片、表情包段的消息一律不参与复读链累积；比对前宽松归一（trim / 空白折叠 / 大小写不敏感），复读发送用原始文本、不用归一产物；归一规则与链累积相互独立

### 表情回应域

- **表情回应**（贴在消息上，`set_msg_emoji_like`）与**表情包**（`ctx.send.emoji` 发的图片消息）是两个概念，代码与文档不得混用「表情」裸词
- 表情回应是**两级决策**（ADR 0002）：先掷回应骰（他人消息 / 麦麦自己的消息各一套概率）决定贴不贴，掷中后表情若命中反义映射再掷反义骰，反义骰不中**回落跟风**贴同款；一次上报至多贴一个表情回应（跟风与反义互斥）。改这个语义要先改 ADR 0002
- `set_msg_emoji_like` 必须带 `"set": True`，NapCat 4.17.25 起缺这个参数请求被静默忽略
- 表情槽是自我循环的**必需**防护：先占槽再发，任何情况下不得先发后占；识别 bot 自身 QQ 只是额外优化。查原作者（`get_msg`）是 await 点，复查表情槽 / 群冷却、掷骰、占槽、写冷却必须在它之后的同步段（`_claim_reaction`）里一并完成，中间不得引入 await，否则并发上报会在同一条消息上贴两次
- 协议端调用失败语义：不可达返回 `None`；HTTP 200 + `status != ok` 视为失败；`retcode=1404` 表示动作不存在，此时置 `_emoji_action_supported = False` 停止后续尝试（配置热重载重建运行时会复位，下一次上报重新判定）
- 上报形态不认识时必须打 warning 并附原始 payload 片段，不得静默丢弃（ADR 0001 的部署验证依赖这条日志）
- 表情 ID 归一一律走 `normalize_emoji_id`（纯数字原样，emoji 字符取首码点）。**这不是美化，删不得**：协议端上报 Unicode 表情给的是字符本身（`'⭐'`），而发送时按 `code.length > 3` 判类型（≤3 位当 QQ 小黄脸、更长当 Unicode 码点），归一正好抹平这个不对称。依据见 `arcwsl:~/github/REFERENCE/snowluma` 的 `msg-push/decoders/event-0x2dc.ts` 与 `oidb-services/reaction/set-reaction.ts`
- 内置反义映射只用 Unicode emoji，不臆造 QQ 私有编号的语义

### 公共

- 协议端行为有疑问先读源码：SnowLuma 已 clone 在 `arcwsl:~/github/REFERENCE/snowluma`，表情回应相关在 `packages/onebot/src/event-converter/to-notice.ts`（上报）、`packages/onebot/src/actions/extended.ts`（`set_msg_emoji_like`）、`packages/onebot/src/actions/message.ts`（`get_msg`）；主程序 hook 消息形态看 `arcwsl:~/github/REFERENCE/MaiBot/src/plugin_runtime/host/message_utils.py`
- 表情回应域的上下文注入默认关闭（`context.enabled`），整块试、整块关（复读域注入跟随 `repeat.enabled`，不受此开关约束）；注入路径复用 `scope` 白黑名单，但麦麦自己贴的回应不受 `ignore_reactors` 影响
- 注入限流里**去重 ≠ 聚合**：去重（`dedup_ttl_seconds`）管「同一回应者+被回应消息+表情只注入一次」，聚合（`merge_window_seconds`）管「同一被回应消息同一表情的多条回应汇入一条注入消息」，`max_reactions_per_message` 兜住单条消息的注入条数
- 注入文本显示名来源链：`context.self_display_name` → 主程序 `bot.nickname` → 空（不写名字）
- 「群 → 活跃聊天流」一律优先取 hook 上报的 `session_id`，`chat.get_stream_by_group_id` 仅作兜底：前者由 `plugin.py` 的 `_observe_group_session` 观察、`_get_stream_id` 取用；Host 的会话列表首匹配在同群新旧 session 并存时可能返回陈旧会话，注入会落进没人读的死会话。复读域的触发/行为注入与表情回应域的聚合注入都依赖这条口径
- 注入日志统一格式（对齐 smart_poke_plugin 的 `text='…', index=…, stream=…` 呈现，统一由 `plugin._append_context` 产出）：两条注入路径（表情回应聚合 / 复读触发与行为）成功记 info「已写入 Maisaka 上下文: kind → text 预览 → group/msg 定位 →（表情回应路径另附 emoji/responders）→ index/stream 回执」；失败记 warning「未写入 Maisaka 上下文:」，同一字段序，末尾为 `原因=取不到 stream` / `原因=调用异常 …` / `原因=回执失败 …`（已取到 stream 时再附 stream）。kind 即 `source_kind`，text 经 `_log_preview` 超 60 字截断。写入之外的意外异常（`注入表情回应上下文异常`、兜底 flush、后台并发协程）仍由各自的外层兜底日志记录
- 新增纯逻辑按领域写进对应纯逻辑层并加单测（表情回应域进 `reactions.py`，复读域进 `repeats.py`，两域并列、不混装）；不往 `plugin.py` 里塞可测逻辑

## 验证

```bash
uv run --with pytest --with aiohttp pytest tests/ -q       # 全部通过
uv run --with pyright --with aiohttp pyright              # 生产代码零错误
```

`pyrightconfig.json` 只检查生产代码（`config.py` / `plugin.py` / `reactions.py` / `repeats.py`），且关掉 `reportMissingImports`：真实 `maibot_sdk` 只存在于 MaiBot 容器内，本机永远解析不到；测试目录里的替身（`FakeRequest`、故意传非法概率或权重值）也会产生大量与正确性无关的类型噪声。

`ruff check` 下有四类有意偏离，改代码时不要「顺手修掉」：

- `BLE001`（宽捕获 `Exception`）— 宿主回调（webhook / Hook handler）与后台任务里任何未捕获异常都会拖垮监听或产生孤儿任务，必须兜住并记日志
- `N999`（模块名含连字符）— MaiBot 插件目录命名约定，改名会破坏插件加载；除仓库根目录外，也覆盖 `tests/__init__.py`（测试包挂在同一插件目录下）
- `SIM103`（guard 链末尾 `return True` 的早返回结构）— 早返回结构与前面的门禁检查逐条对称，改成直返条件是否定式表达，可读性更差；现存 `reactions.group_allowed` 一处
- `SIM117`（`aiohttp` 嵌套 `async with`）— 与同机 `friend-request-handler` 的 `_call_protocol` 写法保持一致，便于对读

## 测试注意

- 插件行为依赖 `random.random()`。**「断言不触发」的用例必须把目标出口权重拉满、其余归零**（`make_plugin` 已默认如此：表情回应概率 1.0；复读侧复读权重拉满、打断与沉默权重归零、延迟区间归零），否则门禁失效时用例仍会按默认值蒙对，回归检测形同虚设。
- 涉及表情槽或冷却的用例传时间戳要用 `time.time()`，不能传 `0.0`：`_evaluate` 内部取当前时间，传 0 会让记录直接被 TTL 判成过期。
- 麦麦自己的 QQ（`_self_qq`）是只读属性：配置 `protocol.self_qq` 优先，否则取启动探测值 `_detected_self_qq`。用例要「认不出自己」就把配置置空，不要给 `_self_qq` 赋值。
- 「断言不触发」的沉默用例反向操作：把沉默权重拉满、复读与打断权重归零（不能用三权重全 0——那走的是不掷骰分支，证明不了掷骰确实发生过），保证掷骰必落沉默。
