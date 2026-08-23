# Changelog

本项目变更记录，格式遵循 Keep a Changelog，语义化版本见 `_manifest.json`。

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
