# Design: repeat-and-rebrand

## Context

插件现有通路三件套：webhook 收表情回应上报（ADR 0001）、协议端直调贴表情、`chat.receive.before_process` blocking hook 做防御式拦截（ADR 0003）；纯逻辑集中在 `reactions.py`（表情回应领域）。复读功能引入两条新通路（hook observe 观测 + `ctx.send.text` 发送，决策与备选见 `docs/adr/0005`）与一个新领域（消息复读）。行为需求见 `specs/message-repeat/spec.md`，此处只记 HOW。

## Goals / Non-Goals

**Goals:**
- 复读检测、三出口决策、链终结防护以纯逻辑落地，可脱离 SDK 单测
- 复读与表情回应两个领域互不纠缠，各自可独立关闭
- 全面改名后所有入口（类名、manifest、文档）一致

**Non-Goals:**
- 图片/表情包复读（指纹比对与重发通路，留未来扩展）
- 复读/打断消息的上下文注入（走主程序发送链天然进上下文，不经注入体系）
- 表情回应行为的任何修改（零变化承诺）

## Decisions

### D1 纯逻辑新模块 `repeats.py`，不并入 `reactions.py`

复读领域（归一、链状态机、三出口决策）与表情回应领域（上报解析、反义映射、表情槽）无共享概念，混装会让两个领域的单测与内存态纠缠。命名对齐 `reactions.py` ↔ `repeats.py`。唯一复用件是 `group_allowed`（scope 判定），从 `reactions.py` 导入，不复制。
替代：并入 `reactions.py`（领域混杂）；抽公共 `scope.py`（只有一个函数，过度设计）。

### D2 `RepeatState` 内存态：按（group_id, 归一文本）键控，终结标记独立存储

每链记录：发送者去重集合（或逐条计数器，随 `distinct_senders` 切换）、最后一条原始文本、各条时间戳序列。窗口语义在每次写入时惰性清理：剔除早于 `now - window_seconds` 的条目后再计数。终结标记独立 dict（键同链，值=终结时间戳），TTL 判定与链记录分离——标记过期看 `chain_ttl_seconds`，链数据过期看 `window_seconds`，两者独立演进。链数上界 `max_chains` LRU 淘汰，对标 `max_slots`。
替代：全局消息 deque 重放（每条消息全量扫描，无谓开销）。

### D3 掷骰资格的原子交接：同步段内「清理→写入→判定→拿走资格→写终结标记」

observe handler 由主程序 `asyncio.create_task` 后台调度，同一群两条同文本消息可能并发进入 handler。状态机的 `record_and_maybe_trigger` 设计为**纯同步方法**（无 await 点），在单事件循环内天然原子：一次调用内完成窗口清理、计数、达标判定、资格消耗（写终结标记）。掷骰紧随其后，发送含延迟的部分交 `_spawn` 后台任务——先占标记再掷骰，对标表情槽「先占槽再发」的必需防护。
替代：达标后异步掷骰（并发窗口内同一链可双触发）。

### D4 hook 消息解析：原始段判定 + 防御式排除，解析失败打 debug 不打 warning

`kwargs.message` 为序列化 SessionMessage dict。取 `message_info.group_info.group_id`（缺失视为私聊跳过）、`message_info.user_info.user_id`、`raw_message` 消息段列表。「仅含文本段」= 段类型全为 `text` 且拼接 trim 非空。排除项：`is_notify` 消息、`self_qq` 消息、命令消息（段文本以 `/` 开头——主程序命令标记在 before_process 时点尚未写入，只能按前缀判；自定义命令前缀的部署属已知盲区，见 Risks）、超 `max_text_length`。
解析失败（非 dict、结构缺失）打 **debug** 不打 warning：与 webhook 上报不同（ADR 0001 依赖 warning 做部署验证），hook 每条消息都过，warning 会刷屏；消息段形态由 QQ 客户端决定、高度稳定。

### D5 发送与打断变体池：`ctx.send.text`，空池回落内置默认

stream_id 走现有 `_get_stream_id` 缓存；`ctx.send.text` 异常或返回失败记 warning 不重试（SDK 核心能力，无 1404 类永久失效语义，不设 `_supported` 熔断标记）。打断文本 `random.choice(interrupt_texts)`；配置池被清空时回落内置默认池（与 config 默认值同源）并打 warning，不静默哑火。
替代：协议端直调 `send_group_msg`（消息不进麦麦上下文，ADR 0005 已否决）。

### D6 配置切分：`repeat` 节 + `scope.ignore_senders`

`repeat`: `enabled`(true) / `window_seconds`(120) / `threshold`(3) / `distinct_senders`(true) / `probability`(0.6) / `interrupt_probability`(0.15) / `interrupt_texts`(打断！/够了！/停！/打住！/别念了！) / `chain_ttl_seconds`(1800) / `cooldown_seconds`(120) / `max_text_length`(200) / `max_chains`(2000)。`scope` 追加 `ignore_senders`（与 `ignore_reactors` 并列，语义分别为消息发送者/表情回应者）。`config.toml` 默认值与 `config.py` 严格一致（项目既有铁律）。`plugin.enabled` 仍为总开关，`repeat.enabled` 只停复读。

### D7 改名落地：代码标识全改，仓库目录名由用户手动改

类名 `SendingEmojisPlugin`→`RepeaterPlugin`、`SendingEmojisConfig`→`RepeaterConfig`；`_manifest.json` id→`arcdent.repeater-plugin`、name→「复读插件」、description 覆盖双行为、capabilities 追加 `chat.receive.before_process` 观测声明（保留现有 blocking 声明，同名 hook 两个 handler 组件）；README 重写定位与安装说明（含迁移段落：旧 id 卸载、新 id 安装）；AGENTS.md 结构与规则补复读条目；`plugin.py`/`config.py` docstring 更新。仓库根目录（即插件目录）改名是文件系统操作，由用户手动执行，代码内全部相对导入不受影响。

### D8 fake SDK 扩展与测试策略

`tests/fake_maibot_sdk.py` 补：`ctx.send.text(text, stream_id)` 替身（记录调用供断言）；`HookHandler` 装饰器已有，observe 模式参数直接透传（fake 不模拟调度，测试直接调 handler 方法）。测试分层：`repeats.py` 纯逻辑全单测（归一、双口径计数、窗口滚动、链终结、三出口决策、门禁）；`test_plugin.py` 插件级用例直接调 hook handler 模拟主程序分发，断言 `ctx.send.text` 调用与不调用。「断言不触发」用例把概率拉满（项目既有铁律）；时间戳用 `time.time()`（TTL 判定铁律）。

## Risks / Trade-offs

- [observe 并发下同一链双触发] → D3 同步原子段 + 先写终结标记再掷骰
- [hook 消息形态随主程序版本漂移] → 防御式 isinstance 检查 + debug 日志；形态剧变时功能静默失效但不崩溃，`openspec` 部署验证清单含「发三人接龙观察日志」
- [自定义命令前缀的部署下 `/` 判定漏排命令] → 已知盲区写入 README 配置说明；主程序默认前缀即 `/`
- [归一化过度合并两句本不相干的话] → 仅 trim/空白折叠/大小写三级，不动全半角与 emoji
- [链与标记内存增长] → `max_chains` LRU 上界；终结标记随链淘汰
- [manifest id 变更视同新插件，老部署不知情] → README 迁移段落 + CHANGELOG BREAKING 标注
- [打断池被清空] → 回落内置默认池 + warning（D5）

## Migration Plan

1. 合并后打 tag，GitHub 仓库改名为 `repeater-plugin`（用户手动）
2. MaiBot 容器内：卸载旧目录 `sending-emojis-plugin`，按新 id 安装 `repeater-plugin`，配置迁移（旧 `config.toml` 字段全兼容，新增节用默认值）
3. 回滚：重装旧 tag，配置向后兼容（新增节缺省即可）

## Open Questions

（无——剩余细节（如 `interrupt_texts` 的 UI 标签文案）不影响 spec、方案与任务分解）
