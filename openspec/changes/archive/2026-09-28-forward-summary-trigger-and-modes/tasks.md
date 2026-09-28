## 1. 配置与骨架

> 阶段四执行组 1，无前置依赖。先行落地配置字段，后续章节的测试与文案按新默认值编写。

- [x] 1.1 `config.py` 的 `ForwardSection` 新增 `mode: str = "direct"` 字段（中文 `label` / `hint` 说明 direct 与 planner 两种取值，`order` 续排）
  - seam：`config.ForwardSection`（模块导出类）
  - 验收：`tests/test_config.py` 断言默认值为 `"direct"`
- [x] 1.2 `config.py` 的 `ForwardSection` 新增 `auto_enabled: bool = True` 与 `request_enabled: bool = True`（中文 `label` / `hint` 说明分别控制自动与请求两条路径，`order` 续排）
  - seam：`config.ForwardSection`
  - 验收：`tests/test_config.py` 断言两个字段默认值均为 `True`
- [x] 1.3 `config.py` 的 `ForwardSection` 新增 `summary_timeout_seconds: int = 10`（`ge=1, le=300`，中文 `label` / `hint` 说明为「等待转发卡片摘要就绪的上限」，`order` 续排）
  - seam：`config.ForwardSection`
  - 验收：`tests/test_config.py` 断言默认值 10；传入 0 被配置校验拒绝（与 repeat 域 `ge=` 口径同构）
- [x] 1.4 `config.toml` 的 `[forward]` 段同步四个新字段，值与 `config.py` 默认值逐项一致，每行中文注释（`mode` 注明两种取值与默认行为，两个开关注明控制的路径，超时注明单位与放弃语义）
  - seam：`config.toml` + `tests/test_config.py` 默认值一致性用例
  - 验收：tomllib 解析 `config.toml` 得到的四个字段值与 `ForwardSection` 默认值相等；`[forward]` 段新增行均有中文注释
- [x] 1.5 版本升级：`plugin.config_version` 1.2.0 → 1.3.0、`_manifest.json` 的 `version` 同步、CHANGELOG 增条目
  - seam：`config.PluginSection.config_version` 与 `_manifest.json`
  - 验收：`tests/test_config.py` 断言 config_version 为新值；`_manifest.json` 与 CHANGELOG 首条版本互校；capabilities 数量不变（11）
- [x] 1.6 扩展测试替身：`tests/fake_maibot_sdk.py` 的 `FakeMessageCapability` 支持按消息 ID 预置「第 N 次调用后返回就绪 dict」的序列（轮询测试需要先返回 None、再返回带 `processed_plain_text` 的 dict）
  - seam：`tests/fake_maibot_sdk.py::FakeMessageCapability`
  - 验收：用例中可构造「前两次 None、第三次就绪」的替身；既有用例不回归

## 2. 转发域纯逻辑（`forwards/`）

> 阶段四执行组 2，依赖组 1 的配置默认值（仅文案与默认值测试引用）。全部为可单测纯逻辑，不依赖 SDK。

- [x] 2.1 `forwards/nodes.py`：`_media_segment` 收紧为三分支——有 `binary_data_base64` 转 `base64://` 图片段；否则段内 `data` 为 `http(s)://` / `base64://` / `file://` 前缀或含路径分隔符时按该引用重建；否则返回 `None`（段不产出）
  - seam：`forwards.nodes.nodes_to_onebot_messages`（既有导出函数的行为修订）
  - 验收：带二进制图片输出 `base64://` 前缀；http 引用原样输出；中文描述文本（`[图片：…]`）不再产生任何段
- [x] 2.2 `forwards/nodes.py`：确认并补强空节点口径——重建后 content 为空的节点整条丢弃、全部落空返回 `[]`（既有行为，补场景断言）
  - seam：`forwards.nodes.nodes_to_onebot_messages`
  - 验收：单节点仅含不可用图片 → `[]`；混合节点（文本 + 不可用图片）只保留文本段
- [x] 2.3 新建 `forwards/summary.py`：`extract_rich_summary(message_dict)` 取 `processed_plain_text` 并 strip，非 dict / 字段缺失 / 空串一律返回空串；不依赖 SDK
  - seam：`forwards.summary.extract_rich_summary`
  - 验收：`{"processed_plain_text": "  【合并转发消息…】  "}` → 去空白文本；`None` / `{}` / 无该字段 → 空串
- [x] 2.4 `forwards/summary.py`：`next_poll_interval(attempt)`（0.5s 起步、退避至 1s 封顶）与 `should_give_up(elapsed, timeout)`（`elapsed >= timeout` 即放弃；`timeout <= 0` 按立即放弃）
  - seam：`forwards.summary.next_poll_interval` / `should_give_up`
  - 验收：`next_poll_interval(0/1/2/3)` → 0.5 / 1.0 / 1.0 / 1.0；`should_give_up(10.0, 10)` 为 True、`(9.9, 10)` 为 False、`(0.0, 0)` 为 True
- [x] 2.5 `forwards/texts.py`：`build_forward_content_text` 与 `build_forced_comment_intent` 增加 `rich_summary` 关键字参数——富摘要非空时以富摘要为内容主体（保留来源群前缀、发送者与 `inject_max_chars` 截断），为空时回落既有节点摘要结构
  - seam：`forwards.texts.build_forward_content_text` / `build_forced_comment_intent`
  - 验收：富摘要非空时注入文本含富摘要全文（截断后）；富摘要为空时输出结构与 v0.6.0 一致；intent 含「必须」语义关键词且材料为富摘要
- [x] 2.6 `forwards/texts.py`：新增 `sanitize_display_text(text)`——剔除 C0/C1 控制字符与孤立代理项，其余字符原样保留
  - seam：`forwards.texts.sanitize_display_text`
  - 验收：输入含 `\x11` / `\x00` 的输出不含控制字符；正常中文与 emoji 原样通过；空串 / 非字符串输入不抛异常
- [x] 2.7 新建 `forwards/decision.py`：`DecisionTickets` 一次性决策票注册表——`issue(message_id, target_group_id, now)` 返回票号、`consume(ticket, now)` 消费（存在 / 未过期 / 目标群匹配）、纯同步无 await，内存 dict + TTL 清理
  - seam：`forwards.decision.DecisionTickets`（类）
  - 验收：签发后可消费一次、二次消费失败；TTL 过期后消费失败；目标群不匹配拒绝；`inspect.iscoroutinefunction` 对 `consume` 为 False
- [x] 2.8 组 2 单测齐全：`tests/test_forwards_nodes.py`（既有「无二进制图片降级为占位文本」用例按新口径改为「节点丢弃」，新增三分支各一）、`tests/test_forwards_summary.py`、`tests/test_forwards_decision.py`、`tests/test_forwards_texts.py`（富摘要 / 截断 / 清洗）
  - seam：上述四个测试文件
  - 验收：`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿；无残留占位文本断言

## 3. `plugin.py` 编排改造

> 阶段四执行组 3，依赖组 2 的纯逻辑（`forwards.summary` / `forwards.decision` / `forwards.texts`）。组内按 3.1 → 3.10 依赖序实现：先收敛同步段，再建编排任务的就绪与决策两段，最后模式分支、Tool 与日志。

- [x] 3.1 同步段收敛：`_process_forward_message` 删除掷骰与 `mark_group` / `mark_targets`，保留「域开关 → 解析 → 源群门禁 → 请求路径入口提示 → 防重 `mark_message` → spawn 编排任务」；掷骰与冷却写入移入 3.3
  - seam：`RepeaterPlugin._process_forward_message` / `handle_forward_message`
  - 验收：同步段用例断言「不掷骰、不写冷却、只 mark_message 与 spawn」；非候选 / 非白名单 / 麦麦自身 / 域关闭四种用例断言无任务无状态写入（回归）
- [x] 3.2 编排任务就绪段：`_orchestrate_forward_delivery(source)` 轮询 `ctx.message.get_by_id`（**只带消息 ID，不带 `stream_id`**——Host 按「流 + 消息 ID」过滤，陈旧流会恒空；见 design「生产实测补记」）等待**摘要就绪**，首次即查、之后按 `forwards.summary` 退避间隔，总超时 `forward.summary_timeout_seconds`；就绪则取得富摘要；超时记 warning（含 msg_id 与源群号）并放弃；`get_by_id` 异常按未就绪续等
  - seam：`RepeaterPlugin._orchestrate_forward_delivery`（后台任务入口，测试直接 await）
  - 验收：就绪前无任何掷骰 / 冷却 / 投递（计数 roll 与调用记录断言）；超时放弃时日志含 warning；就绪轮询全程只发生在一个任务内
- [x] 3.3 编排任务决策段：就绪后 `decide_forward_targets` 逐目标群掷骰 → 命中先写冷却（`mark_group` + `mark_targets`）→ 每命中群 spawn 既有 `_deliver_forward_to_target`（传入富摘要）→ spawn 既有 `_finalize_forward_delivery`；无命中按既有口径记 info 收尾；编排任务经 `_spawn_forward` 登记（卸载取消覆盖）
  - seam：`RepeaterPlugin._orchestrate_forward_delivery`
  - 验收：就绪后命中则投递任务按序创建且冷却已写；全沉默不写冷却；`on_unload` 后无存活编排 / 投递任务（`asyncio.all_tasks` 断言）
- [x] 3.4 富摘要贯通：`_deliver_forward_to_target` / `_append_forward_content` / `_trigger_forward_comment` 扩展参数，注入与 intent 文案走 `forwards.texts` 富摘要版本并对来源字段过 `sanitize_display_text`
  - seam：`RepeaterPlugin._deliver_forward_to_target` / `_append_forward_content` / `_trigger_forward_comment`
  - 验收：投递成功后注入文本与 intent 含富摘要内容（fake get_by_id 预置 `processed_plain_text`）；群名含控制字符时注入与 intent 输出已清洗；注入失败 warning 口径不变
- [x] 3.5 路径开关短路：`_forward_enabled` 保持域总闸（`plugin.enabled` ∧ `forward.enabled`）；自动 Hook 入口按 `forward.auto_enabled` 短路；请求路径入口（`observe_source_message` 与转发 Tool）按 `forward.request_enabled` 短路
  - seam：`RepeaterPlugin._forward_enabled` 与两处入口判定
  - 验收：`auto_enabled=false` 时无编排任务、源群 Planner 提示不触发；`request_enabled=false` 时 Tool 返回不可用说明；两开关组合矩阵四种用例互不干扰
- [x] 3.6 决策模式分支：编排任务按 `forward.mode` 分岔——`direct` 走 3.3 既有投递 spawn；`planner` 进入 3.7 决策流；非法值按 direct 并记 warning
  - seam：`RepeaterPlugin._orchestrate_forward_delivery` 的模式分支
  - 验收：direct 模式无票无 trigger；非法值（如 `"smart"`）警告且走 direct
- [x] 3.7 决策触发与发票：`planner` 模式命中后对每个命中目标群 `forwards.decision` 发票（键含 message_id + 目标群，TTL 沿用 `dedup_ttl_seconds` 量级）并向目标群 `proactive.trigger`（intent 要求调用决策 Tool 传「转发 / 不转发」与理由，材料为富摘要，metadata 含 trigger_kind）；触发失败记 warning 并按不转发收尾
  - seam：`RepeaterPlugin._orchestrate_forward_delivery` 的 planner 分支
  - 验收：planner 模式命中后无投递、有发票与 trigger（metadata 含 trigger_kind）；trigger 失败时 warning 且无投递
- [x] 3.8 决策 Tool：新增 `decide_forward_delivery` Tool（`chat_scope="group"`，参数 `message_id` 与 `deliver`）；handler 校验票（存在 / 未消费 / 目标群匹配 / 未过期）后：`deliver=true` 走 3.3 同套投递编舞（复用 `_deliver_forward_to_target` + 收尾与登记），`false` 记 info 收尾；无票 / 错群 / 重复消费返回明确拒绝
  - seam：`RepeaterPlugin.decide_forward_delivery`（Tool handler）+ `forwards.decision.DecisionTickets`
  - 验收：持票调用转发 → 投递编舞发生且票被焚；持票调用不转发 → 无投递有日志；无票调用被拒且不投递；同票二次调用被拒
- [x] 3.9 `get_components()` 覆写纳入新 Tool 的 `chat_scope="group"`（保留既有转发 Tool 覆写条目，承载性 hack 不得删）
  - seam：`RepeaterPlugin.get_components`
  - 验收：组件声明顶层含两个转发 Tool 且 `chat_scope="group"`；私聊会话不可见（既有断言回归）
- [x] 3.10 日志口径：新增「摘要就绪超时」（warning，含 msg_id 与群号）、「决策模式触发 / 消费 / 不转发」（info）、非法 mode（warning）；既有「自动转发」前缀与目标群号口径不回退
  - seam：`FakeLogger` 断言（`tests/test_plugin_forward.py`）
  - 验收：各新增日志可在用例中断言到；投递 / 注入 / 留痕日志文案与 v0.6.0 一致（回归）

## 4. 集成测试

> 阶段四执行组 4，依赖组 3。全部经 `tests/test_plugin_forward.py`（含假协议端与 fake SDK），不连真实协议端。

- [x] 4.1 直发模式全链路：覆盖「同步段门禁 → 就绪轮询 → 掷骰 → 冷却 → 延迟 → 直发 → 富摘要注入 → 强制发言 → 源群留痕」全序
  - seam：`tests/test_plugin_forward.py`（集成组）
  - 验收：调用记录断言顺序（get_by_id → 掷骰 → 直发 → 注入 → trigger → 留痕）；快照图片以 `base64://` 出现在直发参数中
- [x] 4.2 图片保全用例：源转发节点图片仅存在于 hook 载荷二进制（fake get_by_id 返回的入库形态不含二进制，模拟 2.8% 恢复失败），断言直发参数仍带该图字节
  - seam：`tests/test_plugin_forward.py`
  - 验收：直发 `messages` 中对应节点含 `base64://` 图片段，字节与 hook 载荷一致
- [x] 4.3 决策模式三路径：命中 → 发票 + 决策 trigger →（a）Tool 转发走完编舞（b）Tool 不转发无投递（c）无票调用被拒
  - seam：`tests/test_plugin_forward.py`（决策模式组）
  - 验收：三条路径各至少一条用例；决策成功后留痕与已投递登记照常发生；不转发与拒列均无直发调用
- [x] 4.4 三层防护回归与冷却新语义：防重 / 源群冷却 / 目标群冷却 / 沉默不写冷却；新增「就绪等待窗口内第二条合并转发独立掷骰」（冷却写入后移的显式用例）
  - seam：`tests/test_plugin_forward.py` + `tests/test_forwards_auto.py` + `tests/test_forwards_state.py`
  - 验收：上述场景在时机改造后全部通过；新语义用例断言第二条消息未被第一条的冷却压制
- [x] 4.5 两路共用回归：自动登记已投递后请求被拒、沉默后请求可执行、`auto_enabled=false` 时请求链路不受影响
  - seam：`tests/test_plugin_forward.py` + `tests/forwards/request/`（既有用例回归）
  - 验收：请求路径既有用例全绿；开关短路后无转发 Tool 副作用

## 5. 验证与文档

> 阶段四执行组 5，依赖组 4 全绿。5.3 为人工线上步骤，由用户执行。

- [x] 5.1 全量校验：`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿、`uv run --with pyright --with aiohttp pyright` 零错误、`ruff check` 仅剩 AGENTS.md 声明的六类有意偏离
  - seam：仓库级命令
  - 验收：三条命令输出满足上述条件
- [x] 5.2 文档同步：`README.md`（`[forward]` 新字段全表 + `mode` 取值语义 + 部署提示）、`AGENTS.md`（转发域规则改为「同步段门禁 + 就绪后掷骰」口径、新增模式与路径开关条目）、`CHANGELOG.md`；`CONTEXT.md` 随阶段一已更新（如有偏差回改）
  - seam：文档文件
  - 验收：README 含四个新字段的中文说明与模式语义；AGENTS.md 转发域不再描述「同步段掷骰」，`forwards/` 结构段列出 `summary.py` / `decision.py`
- [x] 5.3 arclinux 部署与人工验证（按 design Migration Plan）：rsync 新版本（排除 `config.toml`）、`config.toml` 增加四字段（保持 `mode = "direct"`）、重启；源群发含图合并转发，核对目标群带图转发、富摘要注入、麦麦发言内容；切 `planner` 模式验证决策流转
  - seam：线上验证（人工）
  - 验收：目标群收到带图转发且图片与源消息一致；注入文本含已缓存图描述；决策模式转发 / 不转发 / 无票拒绝三条路径符合预期；日志无「未知错误」
  - 实测补记（2026-09-28）：部署与核心链路已验证——planner 决策「转发」路径现场走通（目标群收到转发卡片、富摘要注入、麦麦发言、源群留痕，日志无「未知错误」；09-28 10:07–10:08）、「不转发」路径已验证（09-27 18:36）；无票拒绝由集成用例覆盖（4.3c）；**含图转发的图片对照未现场复现**——图片保全由 `test_plugin_forward.py` 图片保全用例（快照字节直发，不依赖 DB 恢复）与 0.7.1+0.8.0 全量回归保证

## 6. 上线后修复（2026-09-27 生产实测暴露，已随 0.7.1 部署）

> 详见 design.md「生产实测补记」；两项都不是本 change 计划内的行为面，而是插件与 Host 会话/过滤口径的接口问题。

- [x] 6.1 就绪轮询改为只带消息 ID（不带 `stream_id`）：Host 按「聊天流 + 消息 ID」过滤，同群存在新旧多个 session 时插件解析到的陈旧 stream 会让查询恒空 → 超时放弃
  - seam：`RepeaterPlugin._fetch_forward_source_message` + `tests/test_plugin_forward.py::test_orchestration_survives_stale_stream_cache`
  - 验收：陈旧流缓存下仍能取到富摘要并完成投递；轮询调用参数不含 `stream_id`
- [x] 6.2 群 → 活跃聊天流优先取 hook 上报的 `session_id`：`chat.get_stream_by_group_id` 是会话列表首匹配，可能返回陈旧会话，导致注入 / 触发 / 已投递登记落进死会话（麦麦回复 `未找到目标平台` 发送失败）
  - seam：`RepeaterPlugin._observe_group_session` / `_get_stream_id` + `forwards/request/core/streams.py::GroupStreamRegistry(observed_session=…)` + `tests/test_plugin_forward.py::test_forward_delivery_prefers_hook_observed_session` / `tests/forwards/request/core/test_streams.py::test_group_stream_registry_prefers_observed_session`
  - 验收：观察到的活跃会话优先于能力首匹配；自动路径注入 / 触发 / 登记与请求路径解析共用同一 stream 口径
- [x] 6.3 回归与文档：新增回归用例（旧逻辑下确实变红）、全量 pytest / pyright / ruff 通过；AGENTS.md 转发域补两条解析口径、CHANGELOG 增 0.7.1 条目、design 补记主机侧根因
  - seam：仓库级命令 + 文档
  - 验收：`pytest tests/ -q` 全绿（682 通过）、`pyright` 零错误、`ruff` 仍只剩声明的六类偏离
