# AGENTS.md

MaiBot 插件：聚合三个群聊行为——群内复读链达标后，麦麦掷一次骰按**出口权重**选中复读（把复读文本原样发出）、打断（发打断文本唱反调）或沉默之一；有人给某条消息贴了表情回应后，麦麦按概率跟风贴同款，或按概率贴反义回应；源群出现**合并转发**后，麦麦或按**转发权重**对每个目标群独立掷骰**自动转发**，或响应对话请求执行**请求转发**。

## 结构

- `plugin.py` — 插件主类：上报监听、门禁与掷骰编排、协议端直发、复读拦截与直发、上下文注入、转发域自动路径 Hook 与双段式编排（同步段门禁 + 摘要就绪后掷骰投递）、请求路径与转发决策两个 Tool、启动自检
- `reactions.py` — 表情回应域纯逻辑层（上报解析、表情 ID 归一、反义映射、跟风/反义决策、表情槽与冷却），不依赖 SDK，可单测
- `repeats.py` — 复读域纯逻辑层（复读文本宽松归一、消息资格判定、复读链状态机、三出口决策、注入文案构造），不依赖 SDK，可单测
- `forwards/` — 转发域子包（纯逻辑层，不依赖 SDK，可单测）：`source.py`（转发源消息资格解析）、`nodes.py`（转发节点解析与 OneBot 转换）、`auto.py`（逐目标群掷骰）、`summary.py`（摘要就绪判定、富摘要提取与轮询节奏）、`decision.py`（决策模式一次性决策票注册表与模式词表）、`state.py`（转发防重 / 冷却 / 已投递状态 / 群可用性缓存）、`texts.py`（内容注入、留痕、强制发言与决策 intent 文案，富摘要 + 控制字符清洗）
- `forwards/forward_segments.py` — 转发段解析核心叶子模块：Hook 侧与请求侧共用「段类型判定 + 首个 `forward` 段定位 + 节点级校验」；不依赖 `repeats.py`，保住 `import forwards.request` 的独立导入
- `forwards/request/` — 请求转发链路（原 `papaya0481/maibot-ForwardMessagesAuto-plugin` v0.2.12 整包移植）：源群 Planner 触发、查看资格、一次性授权、顺序投递与持久化状态
- `config.py` — 配置模型（plugin/webhook/protocol/bandwagon/opposite/repeat/behavior/context/scope/forward）
- `config.toml` — 默认配置，值必须与 `config.py` 的 default 一致
- `tests/` — pytest（fake maibot_sdk 注入，`uv run --with pytest --with aiohttp pytest tests/ -q`）
- `docs/emoji-id-reference.md` — 表情 ID 两套编号体系（QQ 小黄脸编号 / Unicode 码点）查表与边界
- `docs/adr/` — 0001 走 webhook 不走 SDK 事件链；0002 分支严格互斥 + 内存态；0003 防御式拦截通知 + 插件自建注入；0004 定时 flush 兜底 + 失败回滚；0005 复读检测走 before_process hook（检测层仍有效，observe 模式与 `ctx.send.text` 发送决策被 0006 取代）；0006 复读/打断出口协议端直发 + planner 拦截 + 上下文注入（决策 3 的延迟沿用与串行编舞被 0008 取代）；0007 复读三出口改归一化出口权重（取代双概率配置口径）；0008 复读域独立延迟 + 触发消息注入与延迟并发；0009 转发域双路径并存（自动掷骰 + 请求转发，共享已投递状态）；0010 自动转发协议端直发 + 投递前目标群可用性校验（陈旧 stream 路由故障背景）；0011 自动转发改在摘要就绪后触发 + 同步段只留门禁与字节快照 + 直发/决策双模式
- `CONTEXT.md` — 领域术语表

## 关键规则

### 复读域

- 复读三出口**严格互斥**（ADR 0007）：链达标后掷一次骰，按**出口权重**（`weight_repeat`/`weight_interrupt`/`weight_silence`）归一化选中复读 / 打断 / 沉默恰好一个，决策逻辑全在 `repeats.decide_repeat`；权重只有比例意义、同比例放大等价，三权重全为 0 时视为沉默、不掷骰；打断出口只发打断文本、绝不附带复读文本。改这个语义要先改 ADR 0005/0006/0007 与 change repeat-exit-weights-and-faster-send design D1
- **链终结**与表情槽同构，是自我循环的**必需**防护：先写终结标记再掷骰（`RepeatState.record` 同步原子段内完成），任何情况下不得先掷骰后写标记；`record` 必须保持纯同步方法（无 await 点），否则并发 handler 调用可在同一链上双触发（blocking 串行下该担忧天然消失，同步规则不放松，ADR 0006）
- 复读群冷却与表情回应的群冷却**分开计时、互不牵扯**（各自 state 实例）；沉默出口消耗资格（链终结同样成立）但不进冷却，发送成败不影响已进入的冷却
- 复读检测走 `chat.receive.before_process` hook 的 **blocking 模式**（`repeat_detector`，`order="late"`，仍在 `guard_reaction_notify` 之后，被 guard 拦的通知消息到不了本 handler；同 hook 两个 blocking handler 组件并存是有意设计，防御式拦截 handler 见 ADR 0003）
- **planner 拦截**（ADR 0006）：复读/打断出口命中即对触发消息返回 `abort`（不写库、不进上下文、不进 planner，杜绝双重发言）；沉默出口 / 非候选 / `repeat.enabled=false` → `continue` 放行；handler 同步段纯内存判定无 IO，未捕获异常记 warning 返回 continue（宁可漏一条拦截，不可干扰主程序消息处理），`CancelledError` 照常上抛
- 复读/打断发送走**协议端直发** `send_group_msg`（`{group_id, message: [text 段]}`），不走 `ctx.send.text`——直发只需群号，与 stream_id 彻底解耦（stream 仅注入需要）；发送异常或失败（不可达 / `protocol_call_failed`）记 warning 不重试不熔断，任务止步、不做行为文本注入
- 出口命中后的**后台编舞**（ADR 0006/0008，design D4）：触发消息注入（**无条件**，失败记 warning 继续）与延迟（复读段 `repeat.delay_min/max_seconds`，默认 0.3/1.5，与表情回应域的 `behavior.delay_min/max` 互不牵扯）经 `asyncio.gather` **并发**（`return_exceptions=True`，异常项记 warning 继续，可见延迟 = max(注入 RTT, 延迟)）→ 协议端直发（失败止步）→ 行为文本注入（仅直发成功）；两路注入均走 `maisaka.context.append`，`source_kind` 为 `repeat_trigger` / `repeat` / `interrupt`，文案构造在 `repeats.py`（`build_trigger_visible_text` / `build_behavior_visible_text`，发送者显示名按群名片 → 昵称 → QQ 号三级回落）；不复用表情回应域的 `InjectionState`（复读注入是即时一次性记录，无聚合无重试）；注入失败不重试、不回滚拦截
- 复读域的拦截、直发与注入整体跟随 `repeat.enabled`（关闭时不拦截不注入不发送），与 `context.enabled` **解耦**——后者只管表情回应域注入，两域并列不混装（design D4）
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
- 表情 ID 归一一律走 `normalize_emoji_id`（纯数字原样，emoji 字符取首码点）。**这不是美化，删不得**：协议端上报 Unicode 表情给的是字符本身（`'⭐'`），而发送时按 `code.length > 3` 判类型（≤3 位当 QQ 小黄脸、更长当 Unicode 码点），归一正好抹平这个不对称。依据见 `arcwsl:~/github/REFERENCE/snowluma` 的 `msg-push/decoders/event-0x2dc.ts` 与 `oidb-services/reaction/set-reaction.ts`
- 内置反义映射只用 Unicode emoji，不臆造 QQ 私有编号的语义

### 转发域

- 双路径并存（ADR 0009）：**自动转发**挂在 `chat.receive.before_process` 的 **observe 模式** handler（`forward_detector`，`order="late"`），同步段纯内存完成「域开关 → 形态门禁 → **源群**门禁 → 请求路径入口提示 → **转发防重** `mark_message` → spawn **一个**编排任务」；掷骰、冷却写入、投递与注入全部推迟到**摘要就绪**之后（ADR 0011）；转发域**不拦截**源消息。**请求转发**是原插件完整链路（源群 Planner 触发、**查看资格**、一次性授权、顺序投递）整包移植在 `forwards/request/`，不掷骰、不受**转发权重**影响；**强制发言**只适用于自动路径
- **摘要就绪**（ADR 0011 / design D2）：编排任务轮询 `message.get_by_id`（首次即查，之后 0.5s 起步、退避至 1s 封顶）直到**转发源消息**入库且 `processed_plain_text`（**富摘要**）非空；总时长受 `forward.summary_timeout_seconds`（默认 10s）约束，超时记「自动转发摘要就绪超时」warning（含 msg_id 与源群号）并放弃本次（不掷骰、不写冷却、不投递、不注入）；`get_by_id` 异常按未就绪续等（debug），只有超时才放弃。就绪判定**不等识图描述回填**——描述只进 Maisaka 内存历史、消息本体永不更新，等不到；就绪只轮询一次，**富摘要**共享给本次全部目标群的注入与 intent。就绪轮询**只带消息 ID、不带 `stream_id`**：Host 按「聊天流 + 消息 ID」过滤，而同群可能存在新旧多个 session，陈旧 stream 会让查询恒空（消息 ID 在平台内唯一，本就无需流过滤）
- **群 → 活跃聊天流的解析**：一律**优先 hook 上报的 `session_id`**（主程序在派发 `chat.receive.before_process` 前已按路由口径 platform + group_id + account_id + scope 算好，消息入库与 Planner 读写都用它），`chat.get_stream_by_group_id` 只作兜底——它是 Host 会话列表的**首匹配**，同群多 session 时可能返回陈旧会话，注入与触发会落进没人读的死会话（麦麦在该会话里生成回复会发送失败：`未找到目标平台` → `legacy.send.qq`）。插件在 Hook 同步段被动学习「群 → 活跃会话」（纯内存、有上界），自动路径的注入 / 触发 / **已投递状态**登记与请求转发链路的 `GroupStreamRegistry` 共用同一口径
- **转发防重**的 `mark_message` 仍在同步段、先于等待写入（等待期间重复上报不产生第二次编排），同步段还含只读的源群冷却预检；冷却写入（`mark_group` + `mark_targets`）后移到就绪掷骰命中时，就绪等待窗口内到达的新**合并转发**各自独立掷骰（冷却写入时刻后移是 ADR 0011 记录的已知语义变化）
- `forward.mode` 双模式：`direct`（默认）掷骰命中即进入投递编舞；`planner` 命中不直发，为目标群签发一次性**决策票**（`forwards/decision.py`，键为 message_id + 目标群，TTL 与 `dedup_ttl_seconds` 同量级）并向目标群 `proactive.trigger`（intent 材料为**富摘要**，metadata `trigger_kind=forward_decision`）。模型经 `decide_forward_delivery` Tool（`chat_scope="group"`，参数 `message_id` + `deliver`）回传「转发 / 不转发」：`deliver=true` 走同一套投递编舞（票里携带**转发源消息快照** + 富摘要，发送字节仍取自快照），`false` 仅记 info；无票 / 错群 / 重复消费 / 过期票一律明确拒绝，触发失败与决策票超时未消费按「不转发」处理并记 warning。非法 `mode` 值运行时按 direct 并记 warning
- **转发路径开关**：`forward.auto_enabled` 关闭即 Hook 整段短路（含请求路径入口提示）；`forward.request_enabled` 关闭即转发 Tool（`request_cross_group_forward`）与入口提示停用；两开关互不影响，`forward.enabled`（与 `plugin.enabled` 合取）仍是转发域总闸
- 自动路径的**投递编舞**（change integrate-cross-group-forward design D7，直发模式与决策模式 `deliver=true` 共用）：每个命中目标群一个独立后台任务，任务内串行「延迟（`forward.delay_*`，与 `repeat` / `behavior` 两域延迟互不牵扯）→ 目标群可用性校验 → 协议端直发 `send_group_forward_msg` → 目标群**转发内容注入**（`source_kind=forward`）→ **强制发言**触发」；至少一个目标群投递成功后由收尾任务向源群注入一次**转发留痕**（`source_kind=forward_action`，只列成功群）；任一步失败按 spec 口径记 warning 止步，不重试、不熔断其它目标群与后续消息
- 注入与 intent 材料用**富摘要**（主程序 `processed_plain_text`）而非插件自有节点摘要：来源群名、发送者与富摘要一律过 `forwards.texts.sanitize_display_text`（剔除 C0/C1 控制字符与孤立代理项）；首见图的占位按主程序原文保留，插件不改写。发送字节只从**转发源消息快照**重建，绝不从消息事后存储形态恢复二进制（处理后会丢弃内存字节且入库形态不回填）
- 自动路径**协议端直发**不走主程序发送链（ADR 0010）：只依赖群号，与 Host 聊天流 / Platform IO 路由解耦，陈旧 stream 不可能把投递引向 legacy 后备驱动；目标群消息库只有请求转发路径会写入，目标群上下文靠**转发内容注入**补偿
- 投递前**目标群可用性校验**（ADR 0010）：协议端 `get_group_list` + TTL 300s 内存缓存（`_FORWARD_GROUP_LIST_TTL_SECONDS`），缓存未命中或过期时刷新、失败按未知放行并记 warning（失败后 30s 退避，不每条投递都吃协议端超时）；不在列表的目标群跳过并记含群号与原因的 warning，绝不出现「未知错误」
- 两条路径共用**已投递状态**（change integrate-cross-group-forward design D5）：自动路径投递成功后按「源聊天流 + **转发源消息** ID」登记，请求转发的 Tool 入口据此跳过已投递目标群；候选目标群全部已投递时返回「已转发过」。自动路径沉默不登记，请求转发仍可执行。自动路径不读请求路径的持久化 store
- 三层防护：同一**转发源消息**按消息 ID 在 `dedup_ttl_seconds` 内只掷一次骰；**源群**冷却（`cooldown_seconds`，至少一个目标群命中才写）；**目标群**冷却（`target_cooldown_seconds`，只压制该群）。沉默出口不写任何冷却；冷却写入在就绪掷骰命中之后、spawn 投递任务之前，投递成败不影响已进入的冷却
- `get_components()` 覆写是**承载性 hack**：Runner 只读组件声明**顶层**的 `chat_scope`，`@Tool(chat_scope=…)` 经 SDK 只落进 `metadata`——不覆写这一层，两个转发 Tool（`request_cross_group_forward` 与 `decide_forward_delivery`，名单见 `_FORWARD_TOOL_NAMES`）在私聊会话里同样可见（可见性过滤失效）。改 Tool 声明面时不得删掉这层覆写
- **转发节点**转换在 `forwards/nodes.py`：段映射 text→text、at→at、image/emoji→image（三分支：有 `binary_data_base64` 转 `base64://`；否则段内 `data` 为 `http(s)://` / `base64://` / `file://` 前缀或含路径分隔符时按引用重建；否则该媒体段不产出，**不再发占位文本**），嵌套 forward 递归转 node；重建后内容为空的节点整条丢弃，全部节点落空放弃投递并记 warning；插件绝不用非引用形态的段内数据（如主程序写入的中文描述文本）当文件引用发出
- 日志口径：自动路径的**投递**日志一律带「自动转发」前缀与目标群号（检测层与沉默出口的日志不带目标群号）；请求路径沿用原插件的中文文案、无统一字面前缀，共享群列表刷新等待方的刷新成败由 `plugin.py` 另以「请求转发」前缀记录；刷新任务本身只用稳定域 tag「跨群转发」（按路径的成败由各等待方在 `await` 处自行记录）。新增口径：摘要就绪超时 warning（含 msg_id 与源群号）、决策模式触发 / 决策消费 / 选择不转发 info、非法 mode warning、决策票超时未消费 warning
- 转发域不复用 `scope` 白黑名单与 `behavior.delay_*`，源群过滤只认 `forward.source_groups`；整域跟随 `forward.enabled`，`plugin.enabled` 为总开关

### 公共

- 协议端行为有疑问先读源码：SnowLuma 已 clone 在 `arcwsl:~/github/REFERENCE/snowluma`，表情回应相关在 `packages/onebot/src/event-converter/to-notice.ts`（上报）、`packages/onebot/src/actions/extended.ts`（`set_msg_emoji_like`）、`packages/onebot/src/actions/message.ts`（`get_msg`）；主程序 hook 消息形态看 `arcwsl:~/github/REFERENCE/MaiBot/src/plugin_runtime/host/message_utils.py`
- 表情回应域的上下文注入默认关闭（`context.enabled`），整块试、整块关（复读域注入跟随 `repeat.enabled`，不受此开关约束）；注入路径复用 `scope` 白黑名单，但麦麦自己贴的回应不受 `ignore_reactors` 影响
- 注入限流里**去重 ≠ 聚合**：去重（`dedup_ttl_seconds`）管「同一回应者+被回应消息+表情只注入一次」，聚合（`merge_window_seconds`）管「同一被回应消息同一表情的多条回应汇入一条注入消息」，`max_reactions_per_message` 兜住单条消息的注入条数
- 注入文本显示名来源链：`context.self_display_name` → 主程序 `bot.nickname` → 空（不写名字）
- 新增纯逻辑按领域写进对应纯逻辑层并加单测（表情回应域进 `reactions.py`，复读域进 `repeats.py`，转发域进 `forwards/`，三域并列、不混装）；不往 `plugin.py` 里塞可测逻辑

## 验证

```bash
uv run --with pytest --with aiohttp pytest tests/ -q       # 全部通过
uv run --with pyright --with aiohttp pyright              # 生产代码零错误
```

`pyrightconfig.json` 只检查生产代码（`config.py` / `plugin.py` / `reactions.py` / `repeats.py` 与 `forwards/**`），且关掉 `reportMissingImports`：真实 `maibot_sdk` 只存在于 MaiBot 容器内，本机永远解析不到；测试目录里的替身（`FakeRequest`、故意传非法概率或权重值）也会产生大量与正确性无关的类型噪声。

`ruff check` 下有六类有意偏离，改代码时不要「顺手修掉」：

- `BLE001`（宽捕获 `Exception`）— 宿主回调（webhook / Hook handler）与后台任务里任何未捕获异常都会拖垮监听或产生孤儿任务，必须兜住并记日志
- `N999`（模块名含连字符）— MaiBot 插件目录命名约定，改名会破坏插件加载；除仓库根目录外，也覆盖 `forwards/**/__init__.py`（目录名含连字符的插件包结构）与 `tests/__init__.py`（测试包挂在同一插件目录下）
- `SIM103`（guard 链末尾 `return True` 的早返回结构）— 早返回结构与前面的门禁检查逐条对称，改成直返条件是否定式表达，可读性更差；现存 `reactions.group_allowed` 与 `forwards/request/request.py` 两处
- `SIM117`（`aiohttp` 嵌套 `async with`）— 与同机 `friend-request-handler` 的 `_call_protocol` 写法保持一致，便于对读
- `DTZ005`（`datetime.now()` 未带时区）— 仅限 `forwards/request/source/output_items.py`：Host Context Item 的 timestamp 以 Host schema 口径为准（naive datetime），不是本地时间偏好
- `TRY004`（类型不符时抛 `ValueError` 而非 `TypeError`）— 仅限 `forwards/request/debug_stats.py`：持久化 JSON 统计文件的 schema 校验路径统一抛 `ValueError`

## 测试注意

- 插件行为依赖 `random.random()`。**「断言不触发」的用例必须把目标出口权重拉满、其余归零**（`make_plugin` 已默认如此：表情回应概率 1.0；复读侧复读权重拉满、打断与沉默权重归零、延迟区间归零），否则门禁失效时用例仍会按默认值蒙对，回归检测形同虚设。
- 涉及表情槽或冷却的用例传时间戳要用 `time.time()`，不能传 `0.0`：`_evaluate` 内部取当前时间，传 0 会让记录直接被 TTL 判成过期。
- 「断言不触发」的沉默用例反向操作：把沉默权重拉满、复读与打断权重归零（不能用三权重全 0——那走的是不掷骰分支，证明不了掷骰确实发生过），保证掷骰必落沉默。
