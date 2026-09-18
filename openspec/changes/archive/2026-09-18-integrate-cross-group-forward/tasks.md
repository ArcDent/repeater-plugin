## 1. 配置与骨架

- [x] 1.1 `config.py` 新增 `ForwardSection` 并挂到本插件根配置类（`RepeaterConfig`）上，字段：`enabled=False`、`source_groups=[]`、`target_groups=[]`、`weight_send=30`、`weight_silence=70`、`dedup_ttl_seconds=1800`、`cooldown_seconds=60`、`target_cooldown_seconds=60`、`delay_min_seconds=0.5`、`delay_max_seconds=2.0`、`trigger_source_planner=True`、`trigger_target_planner=True`、`view_failure_fallback_threshold=2`、`inject_max_chars=800`
  - seam：`config.ForwardSection`（模块导出类）
  - 验收：`tests/test_config.py` 断言每个字段默认值；数值字段以非负边界声明、负值被配置校验拒绝（与 repeat 域 `ge=0` 口径同构；运行时「负值按 0 计」的 clamp 属决策层，由 2.3 实现并测试）
- [x] 1.2 `config.toml` 新增 `[forward]` 段，值与 `config.py` default 逐项一致（含中文注释）
  - seam：`config.toml` + `tests/test_config.py` 的默认值一致性用例
  - 验收：用 tomllib 解析 `config.toml` 得到的每个字段值与 `ForwardSection` 默认值相等
- [x] 1.3 版本升级：`plugin.config_version` 升版、`_manifest.json` 的 `version` 同步、CHANGELOG 增条目
  - seam：`config.PluginSection.config_version` 与 `_manifest.json`（文件）
  - 验收：`tests/test_config.py` 断言 config_version 为新值；`_manifest.json` 版本与 CHANGELOG 首条版本互校（仓库无插件版本常量，两来源互校即可）
- [x] 1.4 建立 `forwards/` 子包骨架：`__init__.py`、`nodes.py`、`auto.py`、`state.py`、`texts.py`、`request/`（占位）
  - seam：`import forwards`（包导入边界）
  - 验收：`uv run python -c "import forwards"` 通过；`forwards.nodes` 等模块可导入
- [x] 1.5 工程设施：`pyrightconfig.json` 的 `include` 纳入 `forwards/**`；`tests/fake_maibot_sdk.py` 扩展 fake 能力（`maisaka.proactive.trigger`、`maisaka.context.append`、`message.get_by_id`、`chat.get_group_streams`）
  - seam：`tests/fake_maibot_sdk.py`（测试替身公共面）
  - 验收：`uv run --with pyright --with aiohttp pyright` 对 `forwards/**` 零错误；fake 能力可被调用并记录调用参数

## 2. 自动转发纯逻辑（seam 统一为 `forwards` 子包模块导出函数）

- [x] 2.1 `forwards/nodes.py`：`extract_forward_nodes(message)` 提取首个 `forward` 段并规范化节点（无 content 节点跳过），无有效节点返回 `None`
  - seam：`forwards.nodes.extract_forward_nodes`
  - 验收：纯文本消息 → `None`；单节点合并转发 → 1 个节点；节点 content 全空 → `None`；非 dict 输入不抛异常
- [x] 2.2 `forwards/nodes.py`：`nodes_to_onebot_messages(nodes)` 转 `send_group_forward_msg` 的 `messages` 参数（`{"type":"node","data":{user_id,nickname,content}}`），段映射 text→text、at→at、image/emoji→image（`binary_data_base64` 优先转 `base64://`，缺失时退回段内引用，再无则文本占位），嵌套 forward 递归转 node
  - seam：`forwards.nodes.nodes_to_onebot_messages`
  - 验收：文本节点往返结构正确；含图节点带 `base64://` 前缀；无二进制图片降级为占位文本；嵌套转发输出内层 node 列表
- [x] 2.3 `forwards/auto.py`：`decide_forward_targets(target_group_ids, source_group_id, weight_send, weight_silence, roll)` 逐目标群独立掷骰，返回命中群列表（保持输入顺序），跳过与源群相同的项；权重按非负数处理，单群两权重全 0 → 沉默不掷骰
  - seam：`forwards.auto.decide_forward_targets`
  - 验收：`roll` 注入常量验证命中/沉默边界；`weight_send=100, weight_silence=0` 必命中；两权重全 0 不消费 `roll`（用计数 roll 断言）；源群项被跳过；三目标群可部分命中
- [x] 2.4 `forwards/state.py`：`ForwardState` 提供转发源消息防重（`is_duplicate(message_id, ttl, now)` / `mark_message`）、源群冷却（`is_group_cooling(group_id, ttl, now)` / `mark_group`）、目标群冷却（`is_target_cooling(group_id, ttl, now)` / `mark_targets`），全部纯同步
  - seam：`forwards.state.ForwardState`
  - 验收：TTL 内重复判定为 True、过期后为 False；`mark_targets` 只影响写入的群；方法签名无 `async`（`inspect.iscoroutinefunction` 为 False）
- [x] 2.5 `forwards/state.py`：`DeliveredRegistry`（`register(source_key, targets)` / `is_delivered(source_key, group_id)` / `targets_for(source_key)`，带 TTL 清理）与 `GroupAvailabilityCache`（`update(groups, now)` / `is_available(group_id, now)`，TTL 过期视为未知 → 放行）
  - seam：`forwards.state.DeliveredRegistry`、`forwards.state.GroupAvailabilityCache`
  - 验收：登记后可查到已投递目标群；TTL 过期后查询为空；缓存未更新时 `is_available` 返回 `True`（未知放行）且另有 `has_fresh_data` 供日志使用
- [x] 2.6 `forwards/texts.py`：`build_forward_content_text(...)`（含来源群、发送者显示名三级回落、节点摘要、`inject_max_chars` 截断）、`build_forward_trace_text(delivered_targets)`、`build_forced_comment_intent(...)`
  - seam：`forwards.texts.*`
  - 验收：超长节点文本按上限截断且带省略标记；显示名回落顺序为群名片 → 昵称 → QQ 号；留痕文本只列实际成功群；intent 文本含「必须」语义关键词
- [x] 2.7 上述纯逻辑单测补齐（含边界：空节点、权重全零、冷却期、TTL 过期、媒体缺失、截断）
  - seam：`tests/test_forwards_nodes.py`、`tests/test_forwards_auto.py`、`tests/test_forwards_state.py`、`tests/test_forwards_texts.py`
  - 验收：`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿，新增用例数 ≥ 20

## 3. 请求转发链路移植

- [x] 3.1 将 `FORK/maibot-ForwardMessagesAuto-plugin/forward_messages_auto/` 整包进口为 `forwards/request/`，改写内部 import（相对导入保持包内闭合，不引用原插件顶层）
  - seam：`forwards.request.ForwardingRuntime`（原插件公共入口）
  - 验收：`uv run python -c "import forwards.request"` 通过；`grep -r "forward_messages_auto" forwards/` 无残留引用
- [x] 3.2 迁移原插件单测到 `tests/forwards/request/`（保留 `core/source/target` 分组），修正 import 路径
  - seam：原插件测试文件（整组）
  - 验收：迁移后的测试全部通过，且原插件核心用例（投递顺序、查看资格、授权）无删除
- [x] 3.3 配置适配：原 `ForwardMessagesAutoConfig`（`plugin/routing/behavior`）映射到本插件 `ForwardSection` 与新 ProtocolSection，运行时不直接读旧字段
  - seam：`forwards.request` 的运行时代理读取回调（`config_provider`）
  - 验收：伪造 `ForwardSection` 后，源群/目标群/阈值/触发开关读取正确；`grep -rn "RoutingConfig\|BehaviorConfig" forwards/request/` 无残留
- [x] 3.4 请求路径接入共用状态：投递前查 `DeliveredRegistry` 并以「已转发过」拒绝重复；投递前用 `GroupAvailabilityCache` 校验每个目标群，不可用群返回明确原因（不出现「未知错误」）
  - seam：`forwards.request.RequestService.create`（原 `request.py` 的请求入口）
  - 验收：已登记消息返回 `accepted=False` 与已转发说明；目标群不在缓存列表时返回含群号与原因的失败结果

## 4. `plugin.py` 编排

- [x] 4.1 自动路径 Hook：`chat.receive.before_process`（observe/late）内同步完成形态校验 → 源群门禁 → 防重/冷却 → 逐目标群掷骰 → 命中则先写冷却再 spawn 后台任务；非候选/沉默/关闭一律不产生副作用
  - seam：`RepeaterPlugin.handle_forward_message`（Hook handler）
  - 验收：`tests/test_plugin_forward.py` 中命中用例断言任务被创建、冷却已写；非白名单群、麦麦自身消息、`forward.enabled=false` 三种用例断言无任务、无冷却
- [x] 4.2 投递编舞：每个命中目标群一个后台任务——延迟（`forward.delay_*`）→ 目标群可用性校验 → `send_group_forward_msg` 直发 → 成功后注入转发内容 → 触发强制发言；任一步失败按 spec 口径止步并记 warning
  - seam：`RepeaterPlugin._deliver_forward_to_target`（后台任务入口，测试直接 await）
  - 验收：伪造协议端成功时按序发生直发→注入→触发（用调用记录断言顺序）；直发失败时无注入无触发；校验不可用时跳过且不直发
- [x] 4.3 源群留痕注入：至少一个目标群投递成功后，向源群注入 `source_kind=forward_action` 的留痕，内容只列成功群
  - seam：`RepeaterPlugin._append_forward_trace`
  - 验收：成功集合为 A 时留痕只含 A；全部失败时不注入；注入失败记 warning 且不回滚
- [x] 4.4 请求路径 Hook/Tool 声明与委托：源群 Planner 触发、`maisaka.planner.before_request`/`after_response` 编排、`request_cross_group_forward` Tool（`chat_scope="group"`）全部委托 `forwards/request/` 运行时
  - seam：`RepeaterPlugin.request_cross_group_forward`（Tool handler）
  - 验收：`get_components()` 含 Tool 声明且 `chat_scope="group"`；Tool 调用委托进入运行时（伪造运行时断言调用）
- [x] 4.5 生命周期接线：`on_load` 装配转发域状态与协议端自检、`on_unload` 取消后台任务、`on_config_update` 重建状态并取消未开始任务
  - seam：`RepeaterPlugin.on_load` / `on_unload` / `on_config_update`
  - 验收：卸载后无存活转发任务（`asyncio.all_tasks` 断言）；配置更新后新配置生效且旧任务被取消
- [x] 4.6 启动自检：`forward.enabled=true` 时校验 `protocol.base_url` 与群列表可用性，缺失时打 warning 并在命中时明确放弃投递（不静默）
  - seam：`RepeaterPlugin._startup_checks`（已有方法的扩展）
  - 验收：`protocol.base_url` 为空时启动日志含 warning；此时命中转发记 warning 且不调用协议端

## 5. 协议端能力

- [x] 5.1 `send_group_forward_msg` 直发封装：参数 `{group_id, messages}`，成功判定沿用既有口径（不可达 `None`、HTTP 200 + `status != ok` 失败），返回体兼容 `{message_id, res_id, forward_id}`
  - seam：`RepeaterPlugin._call_protocol`（既有通道）+ 新增直发函数
  - 验收：伪造成功响应视为成功；伪造 `status=failed` 视为失败；不可达返回 `None`
- [x] 5.2 `get_group_list` 群可用性接入：TTL 缓存（300s）+ 未命中时查询；查询失败放行并记 warning
  - seam：`forwards.state.GroupAvailabilityCache` 与查询任务（`RepeaterPlugin._refresh_group_list`）
  - 验收：缓存新鲜时不重复调用协议端（调用计数断言）；查询失败时 `is_available` 返回 `True` 且日志含 warning
- [x] 5.3 失败口径统一与日志前缀：自动路径日志带「自动转发」前缀与目标群号；请求路径沿用原插件前缀；失败一律 warning、不重试、不熔断
  - seam：日志断言（`FakeLogger`）
  - 验收：单目标群失败后其他目标群仍被处理（调用序列断言）；`grep` 日志记录含路径前缀

## 6. 验证与文档

- [x] 6.1 全量校验：`uv run --with pytest --with aiohttp pytest tests/ -q` 全绿、`uv run --with pyright --with aiohttp pyright` 零错误、`ruff check` 仅剩 AGENTS.md 声明的六类有意偏离
  - seam：仓库级命令
  - 验收：三条命令输出满足上述条件
- [x] 6.2 集成用例：伪造协议端 + fake SDK 覆盖自动路径全链路（掷骰 → 直发 → 注入 → 触发）与两条路径互斥（自动登记后请求被拒）
  - seam：`tests/test_plugin_forward.py`（集成组）
  - 验收：新增集成用例 ≥ 6 条且全部通过
- [x] 6.3 文档同步：`AGENTS.md`（结构与转发域规则）、`README.md`（配置说明与迁移段）、`CONTEXT.md`（已更新，按需补例对话）
  - seam：文档文件
  - 验收：README 含 `[forward]` 全字段说明与「停用旧插件」迁移提示；AGENTS.md 结构段列出 `forwards/`
- [x] 6.4 arclinux 部署与人工验证：rsync 新版本（排除 `config.toml`）、停用 `papaya0481.forward-messages-auto`、填 `[forward]` 段（目标群去掉 `747839793`）、群内实测直发/注入/发言/留痕
  - seam：线上验证（人工）
  - 验收：目标群收到直发内容且麦麦发言；源群出现留痕注入；日志无「未知错误」，不可用群以明确原因出现（若配置里保留失效群）
