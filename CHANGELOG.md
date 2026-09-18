# Changelog

本项目变更记录，格式遵循 Keep a Changelog，语义化版本见 `_manifest.json`。

## [0.6.0] - 2026-09-18

### Added

- **跨群转发域**（第三行为域，与复读、表情回应并列）：新增 `[forward]` 配置段与 `forwards/` 子包。源群（`forward.source_groups`）出现**合并转发**后，对每个目标群（`forward.target_groups`）独立按**转发权重**（`weight_send` / `weight_silence`，默认 30 / 70）掷骰决定是否自动转发；投递走协议端直发 `send_group_forward_msg`（不走主程序发送链），投递前校验目标群仍在机器人群列表，成功后向目标群注入转发内容（`source_kind=forward`）、按需触发一次要求针对该转发发言的 Planner 主动任务，并向源群注入只列成功群的行为留痕（`source_kind=forward_action`）。三层防护：源消息防重（`dedup_ttl_seconds`）、源群冷却（`cooldown_seconds`，至少一个目标群命中才写）、目标群冷却（`target_cooldown_seconds`）。
- **请求转发路径**：源群 Planner 触发提示（`trigger_source_planner`）、`request_cross_group_forward` Tool 与 `maisaka.planner.*` 编排由原 `papaya0481/maibot-ForwardMessagesAuto-plugin`（v0.2.12）链路整包移植到 `forwards/request/`；保留查看资格与一次性授权语义，投递前统一校验目标群可用性并把不可用原因返回 Planner（不再出现「未知错误」）。两条路径共用**已投递状态**：自动转发登记过的源消息，请求转发拒绝重复投递。
- **日志与失败口径**：自动路径投递日志带「自动转发」前缀与目标群号，请求路径沿用原插件前缀，排查时先分路径；共享的群列表刷新失败按未知放行并记 warning、失败后 30 秒退避不重查；`forward.enabled=true` 但 `protocol.base_url` 未配置时启动自检给出提示，命中转发记 warning 并放弃投递（不静默失败）。
- `plugin.config_version` 1.1.0 → 1.2.0（新增配置段，主程序自动重建 `config.toml` 骨架，已填的旧字段值保留）。

### 部署注意

- **`forward.enabled` 默认 false**：升级后转发域不工作，需显式开启并填写 `source_groups` / `target_groups`；转发域不复用 `[scope]` 白黑名单与 `[behavior]` 延迟，源群过滤只认 `forward.source_groups`。
- **开启前请停用旧插件** `papaya0481.forward-messages-auto`（配置 `enabled=false` 或移出 plugins 目录）：两条链路同时启用会对同一批合并转发重复处理。
- 目标群收到的自动转发消息**不写主程序消息库**（协议端直发，与复读/打断出口同属性），目标群上下文由转发内容注入补偿；目标群消息库只有请求转发路径会写入。

## [0.5.0] - 2026-09-05

### BREAKING

- **复读出口概率字段删除，改归一化出口权重**：`repeat.probability` / `repeat.interrupt_probability` 移除，换成 `weight_repeat` / `weight_interrupt` / `weight_silence`（默认 85 / 15 / 0 ≈ 85% 复读、15% 打断、从不沉默）。链达标仍掷一次骰，三个权重按比例恰好选中一个出口，只有比例意义、同比例放大等价。**升级后请重调一次权重**：主程序检出 `config_version` 1.0.0 → 1.1.0 后自动重建 `config.toml`（补新键、删废弃键、保留其余用户值），手调过的旧概率值键名已变、不会迁移，三个权重取新默认值。决策见 ADR 0007。
- **默认沉默权重 0 的行为含义**：链一达标必出声（复读或打断），触发消息也就必被 **planner 拦截**——复读文本不再有机会当普通消息交给 planner 回复。唯一的放行口是复读群冷却的压制期（冷却期内链达标不掷骰、不拦截，消息照常进主链）。要恢复「偶尔沉默」就给 `weight_silence` 设正值。

### Changed

- **复读延迟独立**：新增 `repeat.delay_min_seconds` / `delay_max_seconds`（默认 0.3~1.5 秒），复读/打断直发只读这一对，不再沿用表情回应的 `behavior.delay_min/max`（默认 1~6 秒不变）——复读接龙是秒级节奏，按表情回应的节奏等待会错过时机，两域延迟从此分开调（ADR 0008）。
- **触发消息注入与延迟并发**：后台编舞里触发消息注入不再串在延迟之前，改为 `asyncio.gather(触发消息注入, 延迟, return_exceptions=True)`，可见延迟从「注入 RTT + 延迟」变成「两者取较大值」；行为文本注入仍在直发成功之后，麦麦聊天历史里「谁发了什么」始终早于「麦麦复读了/打断了什么」。ADR 0008 取代 ADR 0006 决策 3 的这两点，0006 其余内容（planner 拦截、协议端直发、两路注入语义）仍有效。

### 部署注意

- 回滚到 0.4.0 前，手工清掉 `config.toml` 里的 `weight_*` / `repeat.delay_*` 残留键并把 `config_version` 改回 `1.0.0`（旧版本对未知键的容忍度取决于 SDK `PluginConfigBase`，容器内未验证）。

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
