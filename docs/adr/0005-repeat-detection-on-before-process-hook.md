---
status: accepted
---

# 复读检测走 chat.receive.before_process hook 观测，不走 ON_MESSAGE 事件 / webhook；复读发送走 ctx.send.text

复读功能需要看到所有普通群消息。SDK 事件枚举里有 `ON_MESSAGE`，文档也给了 `@EventHandler(event_type=EventType.ON_MESSAGE)` 的示例，但主程序当前版本的分发代码整段被注释（`src/chat/message_receive/bot.py:846-850` 的 `events_manager.handle_mai_events(EventType.ON_MESSAGE, ...)`），全代码库没有任何 `emit` 触发点——**ON_MESSAGE 是文档活着、运行时已死的事件**。相比之下，`chat.receive.before_process` hook 在入站消息主链上无条件触发（`bot.py:761`），`message` 参数是序列化的 SessionMessage dict，与本插件防御式拦截（ADR 0003）已在处理的形态相同。复读检测因此新挂一个 observe 模式的 handler 在该 hook 点上，与现有 blocking 的 guard 同点不同 handler、互不影响。复读与**打断**的发送用 SDK 标准能力 `ctx.send.text(text, stream_id)`（stream_id 复用现有群号→stream_id 缓存），消息走主程序发送链、进麦麦上下文，麦麦后续对话知道自己复读过什么。

observe 模式对主链的影响有源码保证：`plugin_runtime/host/hook_dispatcher.py:654` 对 observe 处理器一律 `asyncio.create_task` 后台调度，主链不等待插件返回，开销为一次 kwargs 拷贝加任务调度（亚毫秒级）；`HookSpec.allow_observe` 默认 True，`before_process` 未关闭。

## Considered Options

- **`EventHandler(EventType.ON_MESSAGE)` 异步监听**：SDK 文档推荐的标准姿势，但如上所述主程序不触发该事件，注册了也永远收不到。
- **webhook 全量上报普通群消息**：对主程序零开销（连 hook 都不挂），但协议端要为每条群消息多发一次 HTTP；且普通消息主程序本来就收到了，属重复通路。ADR 0001 的 webhook 动机是拿 SDK 链**没有**的表情回应 notice，普通消息不属于此类。
- **`chat.receive.after_process` hook 观测**：也能看到所有消息，但消息已完成 `process()`，媒体被转成文本描述混入 plain_text，「仅含**复读文本**要求的仅含文本段」判定反而失真；且时序更晚无收益。
- **blocking 模式挂 `before_process`**：主链同步等待插件返回，会站在消息处理的关键路径上；复读检测是纯观测，没有任何理由阻塞主链。

## Consequences

- 两个功能的通路按「SDK 链有用 SDK 链、没有才自建」的原则分野：表情回应 notice 走 webhook（ADR 0001）、贴表情动作走协议端直调（SDK 无该动作）；复读检测走 hook 观测（SDK 链有活口）、复读发送走 `ctx.send.text`（SDK 有且消息进上下文是刚需）。
- 通路判定依据从 ADR 0001 时期的「无法验证、押注猜测」升级为源码实证（`arcwsl:~/github/REFERENCE/MaiBot` 与 `arcwsl:~/github/REFERENCE/maibot-plugin-sdk` 已 clone），但实证针对的是当前版本：主程序未来若恢复 ON_MESSAGE 分发或调整 hook 形态，本 ADR 的前提需要重审。
- observe handler 收到的是 kwargs 快照（dict 深拷贝）里的序列化 SessionMessage；检测层必须容许形态变化。认不出的消息形态打 **debug** 不打 warning：与 webhook 上报不同（上报的 warning 承担 ADR 0001 的部署验证职责），hook 对每条群消息都触发，warning 会刷屏；消息段形态由 QQ 客户端决定、高度稳定（口径与 change repeat-and-rebrand 的 design D4 一致）。
- hook 挂在 `process()` 之前，消息段保持原始 OneBot 形态，「仅含 text 段」的**复读文本**判定在原始形态上做，不受主程序预处理影响。
- 麦麦自己经 `ctx.send.text` 发出的复读/打断消息不会回流到 `chat.receive` 入站链（那是入站专用），不存在「自己复读触发自己」的直接回路；群友看到复读后接续发送的**复读链**后续处理见术语表与后续决策。
- 新 handler 需要更新 `_manifest.json` 的 hook 声明（同 hook 点、不同 name），并保持现有 guard 的声明不变。
