# Changelog

本项目变更记录，格式遵循 Keep a Changelog，语义化版本见 `_manifest.json`。

## [0.4.0] - 2026-09-05

### BREAKING

- **复读/打断出口 planner 拦截**：检测 handler `repeat_detector` 从 observe 改 blocking 模式——复读/打断出口命中即对触发消息返回 `abort`（不写库、不进主程序上下文、不被 planner 决策），根除「复读与 LLM 回复双重发言」；沉默出口 / 非候选 / `repeat.enabled=false` 照常放行。触发消息经 abort 后不进主程序消息库（重启后上下文恢复缺该条，由注入补偿当前会话可见性）。决策见 ADR 0006（取代 ADR 0005 的模式与发送决策，检测层设计仍有效）。
- **复读/打断发送改协议端直发** `send_group_msg`（绕过主程序发送链与其他插件 blocking hook 干扰，直发只需群号、与 stream_id 解耦），不再走 `ctx.send.text`；manifest 移除 `send.text` 能力声明。发送失败记 warning 不重试，冷却与链终结不受影响。

### Added

- **触发消息注入**（`source_kind="repeat_trigger"`，文案 `[复读触发] {发送者}：{原文}`）：planner 拦截后无条件把触发消息原文写入麦麦聊天历史（`maisaka.context.append`），补偿拦截造成的上下文缺失；与直发成败解耦，注入失败不回滚拦截。
- **行为文本注入**（`source_kind="repeat"/"interrupt"`，文案 `[复读]`/`[打断] {文本}`）：直发成功后把复读/打断文本写入麦麦聊天历史，麦麦知道自己发出了什么；直发失败不注入（上下文与事实一致）。
- 拦截、直发与注入整体跟随 `repeat.enabled`（关闭时不拦截不注入不发送），与表情回应域的 `context.enabled` 解耦，两域开关互不影响。
- 复读域纯逻辑扩展：`RepeatCandidate` 携带 `message_id` 与发送者显示名（群名片 → 昵称 → QQ 号三级回落）；注入文案构造 `build_trigger_visible_text` / `build_behavior_visible_text`；协议失败详情提取 `protocol_failure_detail`（顺带修复非 dict 响应的可达 `AttributeError`）。

### 部署注意

- 协议端 webhook 勿开 self-message 上报（SnowLuma `reportSelfMessage`，默认关闭即正确）：开启后直发消息会以普通 user 消息身份回流主程序入站链，污染 planner 上下文（详见 README 与 ADR 0006）。

## [0.3.0] - 2026-09-05

### BREAKING

- **项目全面改名**：`sending-emojis-plugin` → `repeater-plugin`。manifest id 从 `arcdent.sending-emojis-plugin` 变更为 `arcdent.repeater-plugin`（对 MaiBot 而言是新插件，已部署实例需按 README 迁移段落重装）；类名 `SendingEmojisPlugin`/`SendingEmojisConfig` → `RepeaterPlugin`/`RepeaterConfig`。发布时需同步 bump `_manifest.json` 的 version；GitHub 仓库与 MaiBot 容器目录改名由用户手动执行。

### Added

- **复读功能**：滚动时间窗口内同一**复读文本**（宽松归一比对，默认仅计不同发送者人头）累积达阈值后，麦麦掷骰三出口互斥决策——**复读**（把链内最后一条的原始**复读文本**作为一条新消息发出）、**打断**（只发变体池随机一条**打断文本**）、沉默。检测走 `chat.receive.before_process` hook observe 通路（零阻塞主链，ADR 0005），发送走 SDK 标准能力 `ctx.send.text`（消息进麦麦上下文）。
- **链终结防护**：达标掷骰后（含沉默出口）即终结，按（群，**复读文本**）记 TTL 标记，防止一轮群友复读里麦麦多次出声；TTL 过期后新一轮可再触发（与表情槽同构）。
- **复读独立群冷却与门禁**：与表情回应的群冷却分开计时；新增 `[repeat]` 配置节（enabled/window_seconds/threshold/distinct_senders/probability/interrupt_probability/interrupt_texts/chain_ttl_seconds/cooldown_seconds/max_text_length/max_chains）与 `scope.ignore_senders` 忽略发送者名单。复读/打断发送前按 `behavior.delay_min/max` 随机延迟。

### Fixed

- **manifest 能力声明补上 `send.text`**：改版部署当日 `_manifest.json` 的 capabilities 漏声明复读/打断发送消费的 `ctx.send.text`，Host 授权层逐次以 `E_CAPABILITY_DENIED` 拒绝（线上三次复读/打断全数发送失败）。manifest 能力断言同步从硬编码清单改为从 `plugin.py` 源码派生消费集合，漏声明与死声明双向拦截，防同类漂移。

## [0.2.0] - 2026-08-23

### Added

- **上下文注入（实验性，默认关闭）**：表情回应（含麦麦自己贴的）去重、聚合后作为独立上下文消息写入 Maisaka 聊天历史（`source_kind="reaction"`）。新配置 `[context]`：`enabled` 总开关、`dedup_ttl_seconds` 去重窗口、`merge_window_seconds` 聚合窗口、`max_reactions_per_message` 注入上限、`self_display_name` 麦麦显示名兜底。决策见 ADR 0003。
- **防御式拦截**：挂 `chat.receive.before_process` Hook，abort 主程序消息链上的表情回应通知消息；命中日志即「适配器是否转发表情回应通知」的线上答案。
- **显示名来源链**：注入文本里麦麦自己的显示名按 `context.self_display_name` → 主程序 `bot.nickname` → 取不到不写名字。
- **emoji 编号参考文档**：`docs/emoji-id-reference.md`（两套编号体系、归一规则、classic/small/超级表情边界、内置反义映射码点对照、全量目录获取方法）。

### Changed

- 门禁复用：注入路径与跟风行为共用 `[scope]` 白黑名单；麦麦自己的回应不受 `ignore_reactors` 影响。

## [0.1.0] - 2026-08-21

### Added

- 表情回应 webhook 上报监听（ADR 0001），直调协议端 `set_msg_emoji_like` 贴表情。
- 两级决策（ADR 0002）：回应骰按概率决定是否回应，反义骰决定贴反义还是同款，反义骰不中回落跟风。
- 内置反义映射 16 对，支持 `extra_pairs` 追加/覆盖与 `disabled_emojis` 排除。
- 表情槽与群冷却（内存态 + TTL + 条数上界）。
- 群白黑名单、忽略回应者、原作者识别（self 消息独立概率）。
