---
status: accepted
---

# 表情回应事件走插件自建 webhook，不走 SDK 事件链

麦麦插件 SDK 的事件类型是固定枚举：`ON_START` / `ON_STOP` / `ON_MESSAGE_PRE_PROCESS` / `ON_MESSAGE` / `ON_PLAN` / `POST_LLM` / `AFTER_LLM` / `POST_SEND_PRE_PROCESS` / `POST_SEND` / `AFTER_SEND`，内置 Hook 清单里也没有任何与表情回应相关的槽位。表情回应在 OneBot 侧是 `post_type=notice` 的 `group_msg_emoji_like` 事件，既不是消息也不在任何 Hook 点上，SDK 这条路上没有出口。同机 `friend-request-handler` 插件遇到过同一类问题（`request` 事件同样拿不到），已验证的解法是插件自己起 aiohttp HTTP 服务接收协议端上报、动作直接 POST 到协议端 OneBot HTTP 端点，见该插件 `docs/adr/0001`。本插件沿用同一条通路。

## Considered Options

- **等 SDK 补 notice 事件通道**：抽象最干净，但要改上游 SDK 与 Host，周期不可控，且本插件只自用。
- **从适配器侧拿 notice**：snowluma-adapter 确实转发 `post_type=message/notice` 的事件，理论上表情回应会进入消息链。但它转成什么形态、`message_id` 与 `likes` 字段是否保留，全都无法在开发机验证——真实 SDK 只存在于 MaiBot 容器内，本机既没有 SDK 也没有 Host 源码。押注一个观察不到的数据形态，等于把整个插件建在猜测上。
- **轮询消息历史比对表情回应**：`ctx.db` 与 `ctx.message` 都不保存表情回应，无从比对。

## Consequences

- 协议端需要再配一个 HTTP 上报目标（`friend-request-handler` 已占一个），换端口或换 token 时两处都要同步。
- 插件依赖协议端容器在同一 Docker 网络内可达，不只依赖 MaiBot 进程内 RPC。
- 上报体形态与动作参数已对照 SnowLuma 源码核实（clone 在 `arcwsl:~/github/REFERENCE/snowluma`）：
  - `packages/onebot/src/event-converter/to-notice.ts` 的 `convertGroupMsgEmojiLike` 产出 `notice_type: 'group_msg_emoji_like'`、`sub_type: 'add' | 'remove'`、`group_id`、`user_id`、`operator_id`、`message_id`、`message_seq`、`likes: [{ emoji_id, count }]`，与本插件解析层一致
  - `packages/onebot/src/actions/extended.ts` 的 `set_msg_emoji_like` 接 `message_id` / `emoji_id` / `set`（默认 true），与本插件发送参数一致
  - `packages/onebot/src/actions/message.ts` 的 `get_msg` 返回首次收到时存储的消息副本，含 `sender.user_id`，与本插件读取原作者的方式一致
- `get_msg` 只能查到协议端存储过的消息，miss 时返回 `retcode=ACTION_FAILED`。因此判定「原作者是不是麦麦自己」必须容许失败并降级为按他人消息处理，不能把它当作可靠信号。
- 解析层对认不出的形态仍降级为忽略并打 warning 附原始 payload：核实的是当前版本的 SnowLuma，协议端升级或换实现后这条日志是唯一的早期信号。
- `set_msg_emoji_like` 是 NapCat 系扩展动作，go-cqhttp 等协议端不一定有。协议端返回 `retcode=1404` 时插件记 error 并停止重试该动作，不做静默失败。