# Design: 复读出口协议端直发 + planner 拦截 + 上下文注入

## Context

现状（见 proposal Why 与 `docs/adr/0005`）：`repeat_detector` 以 observe 模式挂 `chat.receive.before_process`，出口命中后经 `ctx.send.text(text, stream_id)` 发送。源码实证（`docs/adr/0006`）：主程序 hook 只有 `continue`/`abort` 两个动作，`abort` 使消息整条从主链消失；`ctx.send.text` 走 send_service（路过的 blocking hook 可改写/取消）且默认不进内存上下文；`maisaka.context.append` 直接写 planner 历史且与表情回应注入同通路。SnowLuma `send_group_msg` 响应信封与 `set_msg_emoji_like` 一致，现有 `_call_protocol` + `protocol_call_failed` 判定可复用；群发自发消息默认不上报（`reportSelfMessage=false`）。

## Goals / Non-Goals

**Goals:**
- 消除双重发言：planner 永远看不到被复读出口消费的触发消息
- 消除发送链路受扰：直发只依赖协议端可达与群号
- 上下文完整性：拦截的消息与麦麦自己的发声都以注入形式进 planner 历史

**Non-Goals:**
- 不改表情回应域任何行为、不改 `InjectionState` 机制
- 不改三出口决策、链终结、冷却、门禁语义
- 不做发送重试/熔断、注入重试
- 不解决「链内前 N-1 条消息已被 planner 处理」的历史回合（拦截只作用于触发消息这一条）

## Decisions

### D1: handler blocking 化与异常兜底方向

`repeat_detector` 从 observe 改 blocking，`order="late"` 不变（仍在 `guard_reaction_notify` 之后，被 guard 拦的通知消息到不了本 handler，与现状一致）。同步段保持纯内存原子（门禁 → `record` → `decide_repeat` → 出口判定），全程无 await 点——原「observe 并发任务双触发」的防护在 blocking 串行下天然消失，但 `record` 同步规则不放松。出口判定后：沉默/非候选 → 返回 `{"action": "continue", "modified_kwargs": kwargs}`（blocking 模式下随附 kwargs 是正常形态，对齐 guard 的写法）；复读/打断 → spawn 后台任务后返回 `{"action": "abort", "modified_kwargs": kwargs}`。

**异常兜底方向**：handler 任何未捕获异常 MUST 记 warning 后返回 `continue`——blocking 挂在主链上，宁可这一条漏拦截，不可让插件异常干扰主程序消息处理。备选（异常时也 abort）被否：拦截是优化不是安全边界。

### D2: 后台任务编舞

单个 spawn 任务按序执行（`_send_repeat_decision` 改造）：

1. **触发消息注入**：`_get_stream_id(group_id)` → 取到则 `maisaka.context.append` 注入触发消息原文；取不到或调用失败记 warning，**继续**（不阻断后续直发）
2. **延迟**：沿用 `behavior.delay_min/max`（与表情回应共用节奏配置，不新增配置项）
3. **协议端直发**：`_call_protocol("send_group_msg", {group_id, message: [text 段]})`；响应 `None`（不可达）或 `protocol_call_failed` → 记 warning，**任务止步**（不做行为文本注入）。`send_group_msg` 是 OneBot 11 核心动作，不做 `retcode=1404` 式永久停用（对比 `set_msg_emoji_like` 的 `_emoji_action_supported`——那是 NapCat 特有扩展动作）
4. **行为文本注入**：直发成功（`status=ok`）后注入复读/打断文本；失败记 warning 不重试

直发与 stream_id 彻底解耦：直发只需群号（`send_group_msg` 参数），stream_id 仅注入需要。

### D3: 注入文本与 source_kind

- 触发消息注入：`source_kind="repeat_trigger"`，visible_text 呈现发送者与文本（如 `（复读触发）123456：草` 的风格）；发送者显示名按 hook 消息内 `user_cardname` → `user_nickname` → QQ 号三级回落（查证结论见 Open Questions）；`message_id` 附触发消息自身 ID
- 行为文本注入：`source_kind="repeat"` / `"interrupt"` 按出口，visible_text 呈现行为类型与文本（对齐 `build_reaction_visible_text` 的可见文案风格，具体措辞实现期定）；`message_id` 附直发响应 `data.message_id`
- 不复用 `InjectionState`（去重/聚合/回滚是外部事件流聚合需求；复读注入是即时一次性记录，无幂等键需求）

### D4: 开关语义（用户决策）

拦截 + 两路注入 + 直发整体跟随 `repeat.enabled`；`context.enabled` 只管表情回应域，对复读域无任何作用。理由：拦截是复读行为的组成部分（防双发），注入是拦截的补偿，二者不是可独立关闭的实验特性；两域并列不混装是既有原则。`repeat.enabled=false` 时 handler 对所有消息返回 continue（含不拦截不注入）。

### D5: manifest 清理

移除 `send.text` 能力声明（不再调用）；`maisaka.context.append` / `chat.get_stream_by_group_id` 已声明，覆盖注入路径。README 增部署提示：协议端 webhook 勿开 self-message 上报（`reportSelfMessage`），否则直发消息以普通 user 消息身份回流主程序入站链，污染 planner 上下文。

### D6: 测试策略

- blocking 返回值断言：沉默/非候选/开关关闭 → `continue`；复读/打断 → `abort`；异常路径 → `continue`
- `_call_protocol` mock：断言 `send_group_msg` 参数形态（group_id、text 段）；失败（None / status=failed）路径断言止步于触发消息注入
- `maisaka.context.append` 调用断言：次数、顺序（触发在前行为在后）、source_kind、message_id
- 概率约定延续：断言触发的用例概率拉满（复读 1.0 打断 0）；沉默用例双概率归零；时间戳用 `time.time()`
- 既有 `ctx.send.text` 相关断言全部随通路删除

## Risks / Trade-offs

- [blocking handler 异常影响主链] → D1 兜底：异常记 warning 返回 continue
- [触发消息不写库，重启后上下文恢复缺该条] → 已接受（ADR 0006）：注入仅补偿当前会话的 planner 可见性，与表情回应注入（同样不写库）口径一致
- [直发消息无主程序库记录，审计/统计不可见] → 接受：复读插件自带 info 日志记录每次直发
- [链内前 N-1 条消息已进 planner，可能已触发 LLM 回复] → 本变更不处理：turn gate 的回复必要性评分天然过滤多数复读文本；彻底解决需拦截整条链（过度拦截，拒绝）
- [部署者开启协议端 self-message 上报] → README 提示 + ADR 0006 记录形态污染后果
- [主程序未来版本变更 hook abort 语义] → ADR 0006 已声明实证针对当前版本，升级需重审

## Migration Plan

无状态迁移（`RepeatState` 为内存态，重启即新）。部署新版本即生效；回滚 = 回退插件版本（manifest `send.text` 声明随版本回退恢复，无残留状态）。

## Open Questions

- **已结论（原问题：hook 消息 dict 是否携带发送者昵称字段）**：携带。`message_info.user_info` 序列化含 `user_nickname` 与 `user_cardname`（`REFERENCE/MaiBot/src/plugin_runtime/host/message_utils.py:28-31` 的 `UserInfoDict` TypedDict 定义、`:392-396` 的 `_message_info_to_dict` 序列化），hook 载荷经 `serialize_session_message` → `_session_message_to_dict` 原样透传（`src/plugin_runtime/hook_payloads.py:26-33`、`message_utils.py:412`）；顶层 `message_id` 恒写入（`message_utils.py:427`）。D3 显示名策略定为**群名片 → 昵称 → QQ 号**三级回落：主程序 planner 侧显示名惯例即 `user_cardname or user_nickname or user_id`（`src/maisaka/runtime.py:508`、`src/maisaka/context/history.py:53` 等），注入进 planner 历史取同级口径。QQ 号兜底保证显示名永不为空
