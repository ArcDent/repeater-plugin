---
status: research-draft
date: 2026-09-28
---

# 全项目重构范围调研

> **调研草案，非已接受 ADR。** 文件编号仅用于归档调研，不代表方案获准实施，不取代既有 ADR、OpenSpec 或领域术语表。本次只新增本文件；业务、测试、配置与术语表均未修改。

## 结论与范围建议

本次最有实质收益的改造是：**三种转发行为共用按源消息 ID 的协议投递，删除两套合成发送所需的节点重建和字节携带，并补上请求路径失去 Host 发送后需要的目标上下文注入。** 这不是单纯移动 `plugin.py`，而是减少调用方必须理解的发送机制。

建议本次纳入候选 1、2、3，以及候选 4、10 中由发送迁移直接带来的清理；候选 5、6、7、9 的扩大改造另行决定；候选 8 的查看与授权业务保留。不要因为三域都有状态字典、请求侧文件较多，就合并其语义或整包重写。

用户已经选择、但尚未实现的方向：

- 自动直发、目标群决策后投递、请求转发均调用 `forward_group_single_msg`，使用转发源消息 ID；失败不回退合成发送。
- 覆盖合并转发内部的视频、文件、语音问题，删除旧合成发送；这不等于扩大为转发独立视频/文件/语音消息。
- 存在 `forward` 段即可成为候选，保留身份、平台、源群与消息归属门禁；不再要求节点已经展开。
- 请求转发改以富摘要注入补偿目标上下文，保留协议返回的目标消息 ID。
- 对未展开内容给专门提示；提示位置、文案及无富摘要时的行为还须写入正式规格。

## 方法、基线与证据边界

- 调研基线：`19f4ca94787d5495e59f14f13cf0bcc56eb0c72a`，工作树原有 `tests/test_plugin_forward.py` 增加 89 行；该差异原样保留。
- 按规格驱动第一阶段处理：确认现状、约束、可删除机制和待定行为，不进入实现阶段。研究采用仓库一手源码、测试、manifest、配置及 ADR；没有把历史文档中的部署统计当作本次复测结果。
- 扫描覆盖三域纯逻辑、公共 Hook/协议/注入编排，`forwards/request` 的 core/source/target/runtime/config/shared_state/debug_stats，测试目录、默认配置、manifest、类型检查配置与测试导入设施。重点函数阅读实现，其余子模块结合声明和调用关系定位；不声称逐行安全审计或全部测试执行覆盖。
- 已阅读 AGENTS、CONTEXT，以及相关 ADR 0002、0004、0006、0008、0009—0012 和转发主规格。表情回应现状存在规则冲突，见候选 10。
- 未安装依赖、未运行 pytest/pyright、未访问真实 Host/协议端、未实际发送消息。下文“测试证据”指仓库已有测试所表达的断言，不表示本次运行通过。
- `CLAUDE.md` 的 Git 索引模式是 `120000`（符号链接）；本次不改规则文件。
- 下文机制计数按**独立入口、策略或状态目的**枚举，不按文件数、字典数、重复字符串数计数；不同候选可能触及同一实现，减少量不可相加。未测性能，不估算吞吐收益或净删行数。
- 仓库当前生产代码没有所选新动作的实现；红测只有动作名与源 ID 断言，不能作为真实协议契约。新动作参数、失败码、源消息有效期、媒体权限与跨群可用性，需要协议端一手契约或后续实测确认。

## 候选 1：统一转发投递接口，删除两套合成发送

**现状与计数。** 有 3 个行为入口，但只有 2 套物理发送机制：自动直发与目标群决策共用协议端 `send_group_forward_msg`；请求路径走 Host `send.forward`。请求 Host 调用另有 2 个兼容入口：原始 `cap.call` 优先、缺方法才走 SDK 代理。它们不是三套业务。

```1314:1316:plugin.py
    async def _send_group_forward(
        self, target_group_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
```

```368:372:forwards/request/target/delivery.py
    async def _call_forward_with_details(
        self,
        job: ForwardJob,
        target_stream_id: str,
    ) -> Any:
```

```1192:1194:plugin.py
    async def _run_forward_delivery(
        self, source: ForwardSource, targets: list[str], rich_summary: str
    ) -> list[str]:
```

**可减少机制。** 2 套发送收敛为 1 个按源 ID 投递接口；移除旧 OneBot node 合成、Host 发送参数构造、发送专用原始调用/代理二选一。接口应只要求目标群、源 ID，并返回明确发送结果与可选目标消息 ID；协议响应解析应集中在转发逻辑层，不再让请求层知道 Host 的 `sent` 形态。复用现有 `_call_protocol`，不为此新增 HTTP 客户端生命周期。

**必须保留。** 自动目标并发且单群失败隔离；请求目标严格顺序、Tool 等真实完成结果；发送成功后才登记/注入；不可达及协议失败明确报告、不隐式重发；决策票先消费再投递。不能把 `res_id`/`forward_id` 当作平台目标消息 ID。

**测试证据。** 自动协议参数与失败测试、请求顺序及真实完成测试已存在：

```1288:1288:tests/test_plugin_forward.py
def test_send_group_forward_passes_params_and_returns_response(tmp_path: Path):
```

```23:23:tests/forwards/request/target/test_delivery.py
def test_tool_waits_for_real_delivery_before_reporting_success(tmp_path: Path) -> None:
```

```67:67:tests/forwards/request/target/test_delivery.py
def test_tool_sends_targets_in_order_and_deduplicates(tmp_path: Path) -> None:
```

**缺口与建议。** 本次核心范围，工作量中高（跨发送、状态、注入与测试）。必须新增三入口一致契约测试，断言源 ID 不是子节点 ID、目标群正确、恰好调用一次、新动作失败时没有旧动作或 Host 发送调用。媒体可播放/下载必须另做端到端验收；本地参数测试最多证明把保全责任交给协议端，不能证明媒体已保全。

## 候选 2：候选资格与节点展开解耦，缩小来源快照

**现状与计数。** 节点解析已有 1 个共用叶子模块、2 种字段映射，但两侧选择策略不同：自动取首个 forward 段且不向后回落，请求取首个含有效节点的 forward 段。自动来源携带节点快照；请求准备阶段默认获取二进制，构造 `ForwardJob.forward_messages`。此外，请求入口提示就绪检查也借用“能提取可发送节点”判断资格。

```35:35:forwards/source.py
def parse_forward_source(message: Any, self_qq: str = "") -> ForwardSource | None:
```

```41:41:forwards/forward_segments.py
def is_forward_segment(segment: Any) -> bool:
```

```14:14:forwards/request/core/messages.py
    def extract(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
```

```367:373:forwards/request/request.py
    async def _get_source_message(
        self,
        source_message_id: str,
        source_stream_id: str,
        *,
        include_binary_data: bool = True,
    ) -> dict[str, Any]:
```

**可减少机制。** 发送不再需要 2 套节点映射、字节快照和请求侧为发送取回二进制。复用 `is_forward_segment` 做存在性判断；删除发送节点有效性作为候选门禁。快照保留源 ID、可信来源、显示字段、必要会话信息和富摘要，不再携带媒体字节。只有确认文本生成不再消费节点后，才能删除 `ForwardNode` 与节点解析，而非直接整文件删除。

**必须保留。** QQ 群聊、非通知、自身消息排除、源群白名单、有效源 ID；请求仍验证授权会话与消息归属，不因为按 ID 发就允许模型指定任意可查询消息。自动 `parse_forward_source` 当前未明确拒绝空消息 ID，应在新契约中明确该必要条件。

**测试证据。** 现有无节点不参与用例会与新规格冲突，需要在实现阶段有意替换而不是保留双语义：

```531:531:tests/test_plugin_forward.py
def test_forward_message_without_valid_nodes_has_no_side_effects(tmp_path: Path):
```

```400:400:tests/forwards/request/test_request.py
def test_source_group_cannot_read_message_from_another_stream(tmp_path: Path) -> None:
```

**缺口与建议。** 本次纳入，工作量中。矩阵至少包含 forward `data` 为 ID/空列表/未展开对象、只有媒体节点、嵌套转发、首段空而后段有节点、无 forward、非 QQ/通知/自身消息、源 ID 缺失与跨会话伪造。候选扩大不自动意味着“摘要为空也发送”，这两个门禁必须分别定规格。

## 候选 3：请求目标上下文改富摘要补偿，保留真实目标 ID

**现状与计数。** 2 套转发目标上下文机制：自动走 `maisaka.context.append`；请求由 Host 发送的 `storage_message` 与 `sync_to_maisaka_history` 完成。另有自动源群留痕，不是目标上下文的重复副本。请求的 `eligibility_content` 当前只用于资格判断，没有传进 job，也没有用于目标注入。

```887:894:plugin.py
    async def _append_context(
        self,
        group_id: str,
        text: str,
        source_kind: str,
        message_id: Any,
        log_prefix: str = "",
    ) -> None:
```

```461:467:forwards/request/request.py
    def _resolve_view_eligibility_content(
        self,
        source_stream_id: str,
        source_message_id: str,
        content_summary: str,
        message: dict[str, Any],
    ) -> str | dict[str, Any]:
```

```420:426:forwards/request/target/delivery.py
    async def _trigger_planner(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_stream_id: str,
        target_message_id: str | None,
    ) -> str | None:
```

**可减少机制。** 转发目标上下文收敛为 1 套富摘要文本注入，移除请求 Host 历史同步参数。可复用 `build_forward_content_text` 的清洗、截断和来源文案，及现有注入调用；依赖通过回调传入请求层，避免请求层反向 import 插件类。节点数不明时不要把 0 写成真实条数。发送旧链删除后，节点摘要兜底不能继续隐性保留字节/节点模型；改成明确的未展开提示需写入规格。

**必须保留。** 请求目标 Planner 自主决定、可沉默；自动才使用强制发言语气。真实目标 ID 与 `SENT` 一起记录；源 ID 不冒充目标回复锚点。注入文字不是 Host 数据库中的真实目标消息，不能声称保留了完整的 Host 回复锚点功能。

**测试证据。** 自动富摘要和清洗测试、请求缺 ID 安全语义测试可迁移：

```2215:2215:tests/test_plugin_forward.py
def test_orchestration_injects_rich_summary_and_intent(tmp_path: Path):
```

```140:140:tests/forwards/request/target/test_delivery.py
def test_send_result_without_message_id_keeps_safe_reply_fallback(
```

**缺口与建议。** 本次纳入，工作量中高。需明确请求注入失败是否阻断 Planner、是否计入“完整完成”、重复请求是否补注入。现有 `_append_context` 返回 None 且吞失败，不能未经调整就充当可持久化的“注入已完成”回执。目标 ID 缺失、注入失败、Planner 失败、重启恢复分别测试。Host reply 是否能仅凭协议目标 ID 工作，以及未写消息库时的定位能力均未端到端验证。

## 候选 4：缩小 Host 兼容封装，不把所有返回值合并成万能解析器

**现状与计数。** 可识别 4 类兼容面：发送调用原始通道/代理二选一；capability 的 bool/sent/success 成功语义；消息和聊天流旧包装/新解包；Planner 的旧 messages/tool_calls 与新 items/output_items。后两类仍用于查看、授权、查流、触发，并不随删除 Host 发送消失。

```12:12:forwards/request/core/capabilities.py
    def succeeded(result: Any) -> bool:
```

```33:33:forwards/request/core/capabilities.py
    def message_id(result: Any) -> str | None:
```

```1730:1730:plugin.py
    async def authorize_forward_request_context(self, **kwargs: Any) -> dict[str, Any]:
```

```24:24:forwards/request/source/output_items.py
def extract_output_item_tool_calls(output_items: Any) -> list[OutputItemToolCall]:
```

**可减少机制。** 本次删除发送专用原始调用/代理分支及旧 Host 发送 ID 解释。`CapabilityResult.succeeded/error` 仍被请求与源群 Planner 使用，不能整个删掉。后续可在 Hook 边界归一两种载荷，把查看/授权的业务转换只写一次，再按原载荷形式回写；必须避免新增抽象比现有两路更复杂。

**必须保留。** OneBot 的 `status/retcode/data` 与 Host 的 `success/sent` 是不同协议，禁止混用成功判定；旧载荷兼容能否删除取决于支持版本承诺，仓库 manifest 仍声明 SDK 2.x 范围，不能用“目前部署是新版”的未验证假设删掉。

**测试证据。** 有原始 capability 保留 ID 的旧测试和最新 Context Item 授权/混合顺序测试：

```220:220:tests/forwards/request/target/test_delivery.py
def test_raw_capability_call_preserves_host_returned_target_message_id(tmp_path: Path) -> None:
```

```248:248:tests/forwards/request/source/test_output_items.py
def test_latest_output_items_keep_mixed_tool_order_and_clean_only_forward_args(tmp_path: Path) -> None:
```

**缺口与建议。** 发送兼容删除属本次（小）；Planner 双形态收敛属后续（中高）。若本次要一起砍旧载荷，先决定最低 Host/SDK 支持版本并配部署证据，不能顺手删兼容后仍宣称支持旧范围。

## 候选 5：统一群会话解析所有权，保留查询与建会话的差异

**现状与计数。** 2 个解析入口、4 张相关映射：插件 `_observed_stream_ids`、`_stream_ids`，请求注册表 `_streams_by_group`、`_groups_by_stream`。4 张表不是 4 份重复：观察来源、查询结果和反向授权查询目的不同。插件只查询不建会话；请求 `resolve` 允许 `open_session`；两边已经共用 hook 观察回调。

```638:638:plugin.py
    async def _get_stream_id(self, group_id: str) -> str:
```

```30:30:forwards/request/core/streams.py
    async def resolve(self, group_id: str) -> str:
```

```77:77:forwards/request/core/streams.py
    async def resolve_group(self, stream_id: str) -> str:
```

**可减少机制。** 可收敛为 1 个会话解析所有者，删除重复的正向缓存及返回包装解析；保留观察优先与可信反查、按调用策略允许/禁止创建会话。新协议发送本身不依赖 stream，但注入、Planner、请求授权和已投递键仍然需要，不能把所有查流代码当旧发送残留删除。

**必须保留。** 活跃 hook session 优先；失败结果不永久缓存；请求群号必须从可信会话反查，不信任模型参数。自动/复读注入不应因统一组件而开始自动创建会话。观察表目前未按 platform/account 分键，注册表映射也无容量上界；这是风险候选，不是已证实线上串群事故。

**测试证据。**

```14:14:tests/forwards/request/core/test_streams.py
def test_group_stream_registry_prefers_observed_session() -> None:
```

```1941:1941:tests/test_plugin.py
def test_injection_stream_failure_not_cached_and_recovers():
```

**缺口与建议。** 推荐后续独立改造（中），本次只确保请求协议发送不被目标 stream 缺失提前阻断，如何报告后续注入/Planner 失败另定。需补混合域调用、新旧 session 切换、反向映射、跨平台观察污染、缓存上界与 reload 测试。没有真实多账号部署证据，不扩成通用多平台路由框架。

## 候选 6：收敛后台任务管理，先明确热更新结束条件

**现状与计数。** 4 个任务所有者：插件、请求投递服务、请求源群提示服务、可选调试统计 writer。插件还维护转发任务子集；表情回应 flush 是插件总集合中的 1 个常驻任务，不另算所有者。请求投递和提示服务 stop 都 cancel 后 gather；插件转发热更新的 `_cancel_forward_tasks` 只 cancel 并清集合，卸载才对捕获的总任务列表 gather。

```339:357:plugin.py
    async def _stop_forward_runtime(self) -> None:
        """取消未开始的转发任务并落盘请求转发状态（卸载与配置更新共用）。"""
        self._cancel_forward_tasks()
        runtime = self._forward_runtime
        self._forward_runtime = None
        if runtime is None:
            return
        try:
            await runtime.stop()
        except Exception as exc:
            self.ctx.logger.warning(f"停止跨群转发运行时失败: {exc}")

    def _cancel_forward_tasks(self) -> None:
        """取消转发域后台任务（配置更新后旧配置的投递不再继续）。"""
        for task in list(self._forward_tasks):
            task.cancel()
        self._forward_tasks.clear()

    def _spawn_forward(self, coro: Any) -> asyncio.Task:
```

```102:102:forwards/request/target/delivery.py
    async def stop(self) -> None:
```

**可减少机制。** 可复用任务登记、异常消费、停止等待的实现，减少三处短任务回收样板，但保留域级停止集合；统计 writer 是“完成最后保存”而非立即 cancel，不能套同一关闭策略。暂不建立统一业务任务框架。

**必须保留。** `CancelledError` 传播；请求 Tool 取消不取消已开始的投递（shield）；卸载取消并等真实结束；表情回应 flush 重读状态且不叠加循环；配置更新使旧授权无效。转发后台逻辑多处在 await 后读取 `self` 状态，“发出取消请求”不能等同“旧任务已结束”。

**测试证据与具体缺口。** 已有卸载与热更新用例，但后者在更新返回后又 `sleep(0)`，不能独立证明更新返回时所有旧任务已退出：

```1183:1186:tests/test_plugin_forward.py
        old_task = pending_delivery_tasks(plugin)[0]
        await plugin.on_config_update("self", {}, "2")
        await asyncio.sleep(0)
        return old_state, old_task
```

**建议。** 转发热更新边界验证可作为本次安全配套；三域统一管理后续（中）。新增屏障测试：旧群列表刷新、发送与 finally 清理各自挂起，reload 返回即验证无旧任务能写新状态；再测共享群列表刷新的一位等待者取消是否应取消全部等待者。当前风险是静态时序缺口，未运行复现，不能写成已确认跨代污染。

## 候选 7：保留三域状态语义，只治理明确的存储边界

**现状与计数。** 共用冷却实现已经存在；业务上有 4 个独立冷却实例：表情回应、复读、转发源群、转发目标群。按状态目的另区分：表情槽、原作者缓存、回应注入去重、回应聚合与注入计数、复读链及终结、转发防重、决策票、自动已投递登记、群列表缓存。它们绝不是一份“通用去重”可替换。

```254:257:reactions.py
class GroupCooldown:
    """群级发送冷却时钟。表情回应域与复读域各持**独立实例**（分开计时是
    spec 要求，互不牵扯），只共用方法体：mark_group 记录起点，
    is_cooling_down 判定窗口。冷却记录无 TTL，随实例生命周期存续。"""
```

```25:28:forwards/state.py
    def __init__(self) -> None:
        self._messages: dict[str, float] = {}
        self._group_cooldown = GroupCooldown()
        self._target_cooldown = GroupCooldown()
```

```236:244:repeats.py
    def record(
        self,
        group_id: str,
        sender_qq: str,
        text: str,
        now: float,
        *,
        suppress_trigger: bool = False,
    ) -> RepeatRecordResult:
```

**可减少机制。** 不建议减少这些业务状态。可减少自动字节快照负担（候选 2），并针对 `ForwardState._messages` 只有时间判定、没有回收的方法补过期清理；这属于资源治理而不是统一状态机。决策票的超时清理目前是惰性的，没有持续触发就不会及时产生 warning；是否要及时告警应另定，不以“共享定时器”名义偷改。

**必须保留。** 槽先占后发、链同步终结后掷骰、防重先于摘要等待、冷却在命中后写且失败不回退；沉默不写冷却；自动已投递 TTL 与请求永久阶段分开。表情回应注入失败回滚去重，而复读/自动转发注入不重试，不能统一失败回滚。

**测试证据。**

```471:471:tests/test_reactions.py
def test_slot_blocks_second_reaction_on_same_message():
```

```403:403:tests/test_repeats.py
def test_eviction_drops_the_termination_marker_with_the_chain():
```

```2420:2420:tests/test_plugin_forward.py
def test_planner_mode_warns_when_decision_tickets_expire_unconsumed(tmp_path: Path):
```

**缺口与建议。** 通用状态机不做；长期资源上界后续（小到中）。过期清理需用长序列与注入时钟测试，不能提前淘汰未过期防重造成重复。请求永久记录容量问题不能直接照搬 TTL 清理，否则跨重启防重失效。

## 候选 8：查看与授权保留；可优化双载荷转换，不削弱可信来源

**现状与计数。** 请求有 2 条调用路线：已查看后直接转发、未查看时替换为系统查看并续接。还有 2 种 Host 载荷格式（见候选 4），不能算 4 套不同业务。3 个关键权限事实独立：本轮查看结果是否可见、一次性凭据绑定哪个真实会话、该会话是否可读取源消息并在源群白名单内。目标群决策票不承担请求身份授权，不能合并。

```15:15:forwards/request/source/view_state.py
class ViewEligibilityStore:
```

```289:289:forwards/request/source/authorization.py
    def consume(self, message_id: str, invocation_context: dict[str, Any]) -> str:
```

```142:142:forwards/request/source/view_flow.py
    async def transform_after_response(
```

```221:221:forwards/request/source/view_flow.py
    async def transform_after_output_items(
```

**可减少机制。** 将载荷读取/改写留在边界，减少查看续接和授权清洗的双份流程；不要为减少文件数把 history、authorization、view_state、delivery 合并。按源 ID 投递只删除发送结构要求，不构成删查看、授权或跨会话校验的理由。

**必须保留。** 一次性 token 消费即删除、消息 ID 错配拒绝、EARLY/LATE 轮次与会话校验、热更新清授权、上下文裁剪撤销查看资格。失败降级限于明确的不可见/空内容，参数错误、非转发、未知错误仍拒绝。当前 `READY` 与 `content_complete` 是两个字段，请求资格分支直接接受 READY；嵌套未完全查看是否仅提醒还是强制拒绝，应按现有测试与正式规格澄清，不能凭“完整查看”四字擅改。

**测试证据。**

```141:141:tests/forwards/request/source/test_authorization.py
def test_handler_rejects_missing_and_replayed_authorization(tmp_path: Path) -> None:
```

```124:124:tests/forwards/request/source/test_view_state.py
def test_nested_view_completeness_requires_root_and_every_discovered_path() -> None:
```

**缺口与建议。** 本次保留业务；双载荷内部收敛后续（中高）。未展开提示不应伪造成功查看结果，也不能把发送后的目标注入当作发送前的源群授权。实现前需补“节点未展开但真实查看失败后降级”“候选成立却无富摘要”的全请求链测试。

## 候选 9：持久化按目的分开，发送阶段扩展先于抽象存储

**现状与计数。** 请求运行时有 3 种磁盘状态：目标投递阶段与目标 ID、源群入口提示永久防重、默认关闭的匿名调试统计。前两者影响重复行为，统计只用于观察。`ForwardStateStore` 支持旧记录迁移与单向阶段提升，不能与自动 TTL 已投递登记混同。

```211:217:forwards/request/core/state.py
    async def record_target_sent(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_message_id: str | None,
    ) -> None:
```

```69:69:forwards/request/source/trigger_state.py
    async def mark_triggered(self, trigger_key: str) -> None:
```

```389:389:forwards/request/debug_stats.py
    async def _writer_loop(self) -> None:
```

**可减少机制。** 只可能复用底层原子 JSON 写入的样板，不能合成一张状态表或一个写入策略。统计有独立路径安全检查、临时文件排他创建、fsync、修订跟踪与失败停用；前两类行为状态没有同样策略，因此“一起抽通用持久化”不是零风险简化。统计是否删除须由用途决定，不能用当前请求次数未知推断无用。

**必须保留。** 同源消息新加目标不影响已完成群；已发送但 Planner 未完成的重试不再发送；发送成功与目标 ID 同次持久化；源群提示只在真实触发成功后记永久防重；调试统计默认关闭且不写源码树。协议成功与本地落盘之间没有分布式事务，崩溃窗口无法靠重命名文件保证 exactly-once。

**测试证据。**

```17:17:tests/forwards/request/core/test_state.py
def test_legacy_route_jobs_migrate_and_preserve_highest_target_stage(
```

```201:201:tests/forwards/request/source/test_trigger.py
def test_source_trigger_is_permanently_deduplicated_across_reload(tmp_path: Path) -> None:
```

**缺口与建议。** 本次保留 3 类存储，因请求新增注入而必要的阶段/迁移调整属于候选 3；通用存储抽取后续（中）。要测试 `SENT` 后注入失败、已注入但 Planner 失败、无目标 ID、磁盘写失败及旧 schema 重载，明确哪些能恢复、哪些只报告不重发。用户若要求自动/请求双向跨重启防重，这是新增行为，需独立规格，本次不默认扩张。

## 候选 10：配置、manifest、规格和回归测试同步，而非继续删行为开关

**现状与计数。** 根配置有 10 个领域/基础节；转发节有 16 个字段（不计 UI 元数据），请求配置视图是 5 个字段，不是第二份可编辑配置。manifest 当前声明 11 项 capability，其中 `send.forward` 随候选 1 才会失去用途。测试导入别名保证同一模块身份，属于测试承载机制，不能因目录重排顺便删掉。

```684:694:config.py
class RepeaterConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    webhook: WebhookSection = Field(default_factory=WebhookSection)
    protocol: ProtocolSection = Field(default_factory=ProtocolSection)
    bandwagon: BandwagonSection = Field(default_factory=BandwagonSection)
    opposite: OppositeSection = Field(default_factory=OppositeSection)
    repeat: RepeatSection = Field(default_factory=RepeatSection)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
    context: ContextSection = Field(default_factory=ContextSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    forward: ForwardSection = Field(default_factory=ForwardSection)
```

```159:159:plugin.py
    def get_components(self) -> list[dict[str, Any]]:
```

**可减少机制。** 删除确已无调用的 `send.forward` 声明与合成专用测试/配置说明；更新三入口共用协议端配置的说明。保留 `get_components` 顶层 `chat_scope` 覆写、两条路径开关、模式与各域独立延迟。现有 `trigger_target_planner` 实际影响请求路径，但 UI hint 只写自动路径；目标列表说明称“投递顺序”，自动实际仅保证启动顺序，应同步精确说明。

**重要规则冲突。** AGENTS 的表情回应段写“命中反义不回落”；实际代码、ADR 0002、CONTEXT 和测试均表达“两级骰，反义不中回落跟风”。这不是本次应顺手修的行为 bug。正式规格前先确认以现实现为兼容基线，之后单独修规则文本；本次未改任何规则/术语。

```304:308:reactions.py
    opposite_id = opposite_map.get(event.emoji_id, "")
    if opposite_id:
        probability = clamp_probability(opposite_probability)
        if probability > 0.0 and roll() < probability:
            return Decision(
```

```277:277:tests/test_reactions.py
def test_opposite_miss_falls_back_to_bandwagon():
```

**测试证据与缺口。** 默认值一致、manifest 版本及能力、模块单身份、请求 SDK 边界均有测试；测试要求应跟随新 contract 改，不让旧合成节点快照测试强迫保留弃用机制。

```371:371:tests/test_config.py
def test_config_toml_matches_model_defaults():
```

```84:84:tests/forwards/request/test_architecture.py
def test_request_package_never_imports_maibot_sdk() -> None:
```

**建议。** 与投递变更直接相关部分本次（小到中），继续砍路径开关/决策模式/请求整链不做；文档矛盾作为审批前待确认项。现有未验证的媒体保全表述应改成分层验收标准，而不是把旧“字节快照保全”直接替换成新“协议端保证保全”。

## 现有 89 行红测是否足够

**足以指向自动合成路径丢段，不足以验收已选新方案。** `nodes.py` 的转换仅处理 text、at、image/emoji、嵌套 forward，其余返回空列表；本例包含文本、视频、文件，因文本仍可发送，视频/文件会被遗漏。这里只是源码推导，本次未重新运行红测。

```2983:2983:tests/test_plugin_forward.py
def test_delivery_preserves_video_and_file_segments(tmp_path: Path):
```

该测试允许“按 ID 再转发”和“合成参数含 video/file”任一分支通过，仍是方案未定时的症状测试。按 ID 分支只比较两次 `message_id` 为 90001，不能证明：

1. 两次发送分别去了正确的两个目标群，也不能排除同时发生旧合成发送。
2. 请求和决策路径也用了新动作；语音、纯媒体、未展开卡片、深层嵌套也没有被门禁提前丢弃。
3. 成功响应包含真实目标消息 ID，且该 ID 进入请求持久化与 Planner 材料。
4. 失败不回退旧发送、不产生错误的成功登记、注入或重复投递。
5. QQ 客户端能打开卡片、播放视频/语音、下载文件，或 Host 能正确使用目标消息 ID 回复。

实现阶段应收紧成唯一允许的新动作契约，并补三入口及负向矩阵。现有 89 行本次原样保留。所有“断言不触发”测试必须把发送权重拉满，其余归零；沉默测试反向设置，避免随机蒙对。

## 最重要的五项范围决策

已选的“按源 ID 投递、无合成兜底、保留视频/文件/语音范围”不重复询问。下一轮只需决定：

1. **未展开候选与摘要等待：** 存在 forward 但始终无富摘要时，是否仍沿用超时不投递？未展开专门提示放在日志、Tool 返回、目标注入还是 Planner intent？建议保留自动摘要门禁，先明确它与候选资格是两层条件；不要借“放宽候选”暗改超时行为。
2. **请求注入失败及恢复：** 是物理发送成功后 warning 继续，还是必须注入成功才触发目标 Planner？重复请求是否只补注入/触发、绝不重发？建议先定义 `SENT` 与后续完成的区别，再决定是否需要新增持久化阶段。
3. **Host 兼容范围：** 本次只删 Host 发送兼容，还是连旧 Planner 载荷一起砍？建议只删发送专用部分；没有部署版本证据时保留双载荷，且承认目标 reply 锚点仍待端到端验证。
4. **基础设施改造边界：** 群会话缓存统一、后台任务管理、永久状态抽象是否同批做？建议只带上转发热更新取消边界的验证，其余另批，避免媒体迁移夹带全域生命周期变化。
5. **正式规格基线：** 表情回应按现有“两级骰可回落”冻结、不碰三域概率和状态语义；请求查看/授权/永久防重继续保留。确认后再修冲突规则，并在新 ADR/OpenSpec 中显式取代旧合成发送、字节快照和请求 Host 历史同步条款。

## 后续交付门槛

- 第一阶段结束需有确认的行为范围及未验证项清单，而不是直接进入大文件拆分。
- 正式变更应覆盖 ADR 0009 的双发送权衡、0010 的协议路径、0011 的字节快照/有效节点要求，以及主转发规格中的候选、投递、请求上下文、回执与恢复条款；既有历史事实保留，不回写成从未存在。
- 单测层证明新动作参数、源/目标 ID 区分、门禁、掷骰时序、失败隔离、注入顺序、授权、重载与持久化恢复；删去旧机制的测试，但保留其防护行为对应的新测试。
- 在用户另行授权实际发送后，端到端验证视频播放、文件下载、语音播放、未展开卡片、源消息不可用、新动作不支持、目标回执与 Host reply；验收前不保证媒体保全或回复锚点。

喵呜~
