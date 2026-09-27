"""复读插件。

聚合两个群聊行为：群内**复读链**达标后，麦麦按概率**复读**（把链内最后一条的
**复读文本**原样发出），或按概率唱反调**打断**；有人给某条消息贴了**表情回应**
后，麦麦按概率**跟风**贴同款，或按概率贴**反义回应**。术语见 CONTEXT.md，
通路与取舍见 docs/adr/。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import random
import time
from hashlib import sha1
from typing import Any

import aiohttp
from aiohttp import web
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin, Tool

from .config import RepeaterConfig
from .forwards.auto import decide_forward_targets
from .forwards.decision import (
    DECIDE_FORWARD_TOOL_NAME,
    FORWARD_MODE_DIRECT,
    FORWARD_MODE_PLANNER,
    FORWARD_MODES,
    DecisionTickets,
)
from .forwards.nodes import nodes_to_onebot_messages
from .forwards.request import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_TOOL_NAME,
    ForwardingRuntime,
    GroupIdList,
    append_user_reminder,
    build_delivered_key,
    read_plugin_version,
)
from .forwards.source import ForwardSource, parse_forward_source
from .forwards.state import DeliveredRegistry, ForwardState, GroupAvailabilityCache
from .forwards.summary import extract_rich_summary, next_poll_interval, should_give_up
from .forwards.texts import (
    build_forced_comment_intent,
    build_forward_content_text,
    build_forward_decision_intent,
    build_forward_trace_text,
)
from .reactions import (
    OPPOSITE,
    Decision,
    InjectionAggregate,
    InjectionState,
    ReactionEvent,
    ReactionState,
    build_opposite_map,
    build_reaction_visible_text,
    build_reactor_display,
    coerce_message_id,
    decide_reaction,
    group_allowed,
    is_missing_action,
    is_reaction_notice,
    is_reaction_notice_message,
    parse_message_author,
    parse_reaction_report,
    parse_self_qq,
    protocol_call_failed,
    protocol_failure_detail,
    qq_set,
)
from .repeats import (
    TRIGGER,
    RepeatCandidate,
    RepeatDecision,
    RepeatState,
    behavior_kind_label,
    build_behavior_visible_text,
    build_trigger_visible_text,
    decide_repeat,
    extract_repeat_candidate,
)

_PROTOCOL_TIMEOUT_SECONDS = 10
# 转发域的工具声明需要**顶层** `chat_scope` 覆写（承载性 hack，见 get_components）：
# 请求转发 Tool 与转发决策 Tool 同为一等公民（决策 Tool 名定义在
# `forwards.decision`，与决策 intent 文案共用同一字面量）。
_FORWARD_TOOL_NAMES = (FORWARD_TOOL_NAME, DECIDE_FORWARD_TOOL_NAME)
# Planner 触发的 metadata `trigger_kind` 词汇，两处定义在一处便于观测区分：
# **强制发言**（投递成功后要求发言）与**决策模式**（等待目标群转发决策）各一个。
_FORWARD_COMMENT_TRIGGER_KIND = "auto_forward"
_FORWARD_DECISION_TRIGGER_KIND = "forward_decision"
# Resident flush-loop tick (design.md D2): hardcoded on purpose — far below
# the default 30s merge window, so worst-case injection lag is window + tick.
_INJECTION_FLUSH_TICK_SECONDS = 5
# **群可用性缓存** TTL（change design D4）：硬编码而非配置项——刷新频率是校验
# 语义的一部分，不是部署偏好；过期后由下一次投递前校验触发重查。
_FORWARD_GROUP_LIST_TTL_SECONDS = 300.0
# 群列表刷新失败后的退避窗口：协议端不可达时，每条投递消息都重查会让批量延迟
# 被每次协议端超时放大；窗口内跳过重查、保持「未知放行」，窗口外再试。
_FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS = 30.0


def _consume_task_exception(task: asyncio.Task) -> None:
    """取走任务异常，避免无人等待时 asyncio 打「Task exception was never retrieved」。

    只消费不记录：有等待者时异常由 `await` 处的 warning 负责记录（带路径
    前缀），全部等待者提前取消时也没有可归因的路径上下文可记；取消不是异常。
    """
    if task.cancelled():
        return
    task.exception()


class RepeaterPlugin(MaiBotPlugin):
    config_model = RepeaterConfig

    _runner: web.AppRunner | None
    _site: web.BaseSite | None
    _state: ReactionState
    _repeat_state: RepeatState
    _opposite_map: dict[str, str]
    _self_qq: str
    _tasks: set[asyncio.Task]
    _emoji_action_supported: bool
    _injection_state: InjectionState | None
    _bot_display_name: str
    _bot_display_name_resolved: bool
    _stream_ids: dict[str, str]
    _flush_loop_task: asyncio.Task | None
    _forward_state: ForwardState
    _forward_tickets: DecisionTickets
    _forward_delivered: DeliveredRegistry
    _forward_availability: GroupAvailabilityCache
    _forward_group_list_task: asyncio.Task | None
    _forward_group_list_failed_at: float | None
    _forward_runtime: ForwardingRuntime | None
    _forward_tasks: set[asyncio.Task]

    def get_components(self) -> list[dict[str, Any]]:
        """在 SDK 收集的组件声明上把转发 Tool 的 `chat_scope` 提到**顶层**。

        Runner 只读组件声明**顶层**的 `chat_scope`（缺省回落 `"all"`），
        `@Tool(chat_scope=…)` 经 SDK 只会落进组件 `metadata`，到不了注册表——
        不覆写这一层，转发 Tool（请求转发与转发决策两个）在私聊会话里同样
        可见（可见性过滤失效）。因此这里对转发 Tool 设置顶层
        `chat_scope="group"` 并移除 metadata 中重复的同名键，其余组件原样返回。
        """
        components = super().get_components()
        for component in components:
            if str(component.get("name") or "") not in _FORWARD_TOOL_NAMES:
                continue
            component["chat_scope"] = "group"
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("chat_scope", None)
        return components

    async def on_load(self) -> None:
        self._runner = None
        self._site = None
        self._tasks = set()
        self._forward_tasks = set()
        self._flush_loop_task = None
        self._self_qq = ""
        self._emoji_action_supported = True
        self._rebuild_runtime()
        if self._injection_state is not None:
            self._bot_display_name = await self._fetch_bot_display_name()
            self._ensure_flush_loop()
            self.ctx.logger.info(
                f"上下文注入已启用，麦麦显示名: {self._bot_display_name or '（无，注入时不写名字）'}"
            )

        await self._start_forward_runtime()
        if self.config.plugin.enabled:
            await self._start_webhook()
        try:
            await self._startup_checks()
        except Exception as exc:
            self.ctx.logger.warning(f"启动自检失败: {exc}")
        self.ctx.logger.info("复读插件已加载（复读 / 表情回应 / 跨群转发三域）")

    async def on_unload(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        self._tasks.clear()
        await self._stop_forward_runtime()
        await self._stop_webhook()
        if pending:
            # 等取消真正落地：卸载返回后不得再有存活的后台任务（含转发投递）。
            await asyncio.gather(*pending, return_exceptions=True)

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        del config_data, version
        # 先停旧运行时再重建：旧配置的投递任务与请求授权不得跨越配置边界，
        # 新配置的状态容器（防重/冷却/已投递）随 _rebuild_runtime 换新实例。
        await self._stop_forward_runtime()
        self._rebuild_runtime()
        self._ensure_flush_loop()
        await self._start_forward_runtime()
        await self._stop_webhook()
        if self.config.plugin.enabled:
            await self._start_webhook()

    def _rebuild_runtime(self) -> None:
        """按当前配置重建反义映射与内存状态容器。"""
        opposite = self.config.opposite
        self._opposite_map = (
            build_opposite_map(
                opposite.extra_pairs,
                include_defaults=bool(opposite.include_defaults),
                disabled=opposite.disabled_emojis,
            )
            if opposite.enabled
            else {}
        )
        behavior = self.config.behavior
        self._state = ReactionState(
            slot_ttl_seconds=float(behavior.slot_ttl_seconds),
            max_slots=int(behavior.max_slots),
        )
        repeat = self.config.repeat
        self._repeat_state = RepeatState(
            window_seconds=float(repeat.window_seconds),
            threshold=int(repeat.threshold),
            distinct_senders=bool(repeat.distinct_senders),
            chain_ttl_seconds=float(repeat.chain_ttl_seconds),
            max_chains=int(repeat.max_chains),
        )
        configured_self_qq = str(self.config.protocol.self_qq or "").strip()
        if configured_self_qq:
            self._self_qq = configured_self_qq
        context = self.config.context
        self._stream_ids = {}
        self._bot_display_name = ""
        # One-shot resolution flag (design.md D6): display-name lookups stop
        # once resolved; only a rebuild (config reload) resets it.
        self._bot_display_name_resolved = False
        self._injection_state = (
            InjectionState(
                dedup_ttl_seconds=float(context.dedup_ttl_seconds),
                merge_window_seconds=float(context.merge_window_seconds),
                max_reactions_per_message=int(context.max_reactions_per_message),
            )
            if context.enabled and self.config.plugin.enabled
            else None
        )
        # 转发域状态随每次重建换新实例：配置热更新后旧的防重/冷却/已投递记录
        # 立即作废，不给新旧配置混用一份过期状态的机会（change task 4.5）。
        forward = self.config.forward
        self._forward_state = ForwardState()
        # **决策票**与防重/冷却同生命周期：TTL 沿用量级同源的 dedup_ttl_seconds
        # （design D4），配置热更新后旧票随之作废。
        self._forward_tickets = DecisionTickets(
            ttl_seconds=float(forward.dedup_ttl_seconds)
        )
        self._forward_delivered = DeliveredRegistry(
            ttl_seconds=float(forward.dedup_ttl_seconds)
        )
        self._forward_availability = GroupAvailabilityCache(_FORWARD_GROUP_LIST_TTL_SECONDS)
        # 在途的群列表刷新任务（in-flight 标记，task 5.2）：同一批并发的目标群
        # 共用一个查询任务，不各查一次协议端；随重建置空（旧任务跑完也只会写
        # 旧缓存实例，不影响新配置）。
        self._forward_group_list_task = None
        # 最近一次群列表刷新失败时刻（None = 尚无失败记录）：失败退避窗口的
        # 起点，随重建清零，新配置下的首次投递不被旧配置的失败牵连。
        self._forward_group_list_failed_at = None
        self._forward_runtime = (
            ForwardingRuntime(
                self.ctx,
                lambda: self.config,
                # 两条路径共用同一份已投递状态与群可用性缓存（design D5，task 3.4）：
                # 请求链路据此拒绝自动路径已投递过的重复转发。
                delivered_registry=self._forward_delivered,
                availability_cache=self._forward_availability,
                # 本插件的适配注入：请求路径投递每个目标群前复用自动路径同款
                # 刷新语义（TTL 判断 + in-flight 合并）；不注入的话，缓存过期后
                # 请求路径只读过期快照、会静默放行到已失效的群（design D4）。
                group_list_refresher=lambda: self._ensure_group_availability("请求转发"),
                plugin_version=read_plugin_version(),
            )
            if self._forward_enabled()
            else None
        )

    def _forward_enabled(self) -> bool:
        """转发域总闸：`plugin.enabled` 与 `forward.enabled` 的合取（spec 转发域开关）。"""
        return bool(self.config.plugin.enabled and self.config.forward.enabled)

    # ---- 转发域：生命周期 ----

    async def _start_forward_runtime(self) -> None:
        """启动转发域运行时；转发域关闭或启动失败时不保留运行时（整域停用）。"""
        runtime = self._forward_runtime
        if runtime is None:
            return
        try:
            await runtime.start()
        except Exception as exc:
            self._forward_runtime = None
            self.ctx.logger.warning(f"跨群转发运行时启动失败，转发域已停用: {exc}")
            return
        forward = self.config.forward
        self.ctx.logger.info(
            f"跨群转发已启用：源群 {len(forward.source_groups)} 个 / 目标群 {len(forward.target_groups)} 个"
        )

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
        """转发域后台任务：同时进入总任务集（卸载统一取消）与转发任务集（配置更新取消）。"""
        task = self._spawn(coro)
        self._forward_tasks.add(task)
        task.add_done_callback(self._forward_tasks.discard)
        return task

    # ---- Webhook 服务 ----

    async def _start_webhook(self) -> None:
        webhook = self.config.webhook
        path = webhook.path if webhook.path.startswith("/") else f"/{webhook.path}"

        app = web.Application()
        app.router.add_post(path, self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, webhook.host, int(webhook.port))
            await self._site.start()
        except OSError as exc:
            self.ctx.logger.error(
                f"表情回应上报监听失败 host={webhook.host} port={webhook.port}: {exc}"
            )
            await self._stop_webhook()
            return

        self.ctx.logger.info(
            f"表情回应上报已监听: http://{webhook.host}:{webhook.port}{path}"
        )

    async def _stop_webhook(self) -> None:
        site, runner = self._site, self._runner
        self._site = self._runner = None
        try:
            if site is not None:
                await site.stop()
        except Exception as exc:
            self.ctx.logger.warning(f"停止上报监听失败: {exc}")
        try:
            if runner is not None:
                await runner.cleanup()
        except Exception as exc:
            self.ctx.logger.warning(f"清理上报监听资源失败: {exc}")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()
        if not self._verify_signature(request, raw):
            return web.Response(status=401, text="invalid signature")
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return web.Response(status=400, text="invalid json")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="invalid payload")

        if is_reaction_notice(payload):
            event = parse_reaction_report(payload)
            if event is not None:
                self._spawn(self._on_reaction(event))
            elif str(payload.get("group_id") or "").strip():
                # 群聊事件却解析不出来 = 形态未知。ADR 0001 要求打日志附原始体，
                # 便于按实测补解析分支。
                self.ctx.logger.warning(
                    f"表情回应上报形态不认识，已忽略: {json.dumps(payload, ensure_ascii=False)[:300]}"
                )
            else:
                # 私聊等无 group_id 的表情回应：本插件只做群聊，静默忽略。
                self.ctx.logger.debug(
                    f"忽略非群聊的表情回应上报: notice_type={payload.get('notice_type')}"
                )
        return web.json_response({})

    def _verify_signature(self, request: web.Request, raw: bytes) -> bool:
        secret = (self.config.webhook.secret or "").strip()
        if not secret:
            return True
        signature = request.headers.get("X-Signature", "")
        if not signature.startswith("sha1="):
            return False
        expected = "sha1=" + hmac.new(secret.encode("utf-8"), raw, sha1).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _spawn(self, coro: Any) -> asyncio.Task:
        """后台执行并持有引用，避免任务被 GC；卸载时统一取消。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- 决策与执行 ----

    async def _on_reaction(self, event: ReactionEvent) -> None:
        try:
            try:
                if self._injection_state is not None:
                    await self._inject_reaction(event)
            except Exception as exc:
                # 注入失败不得拖垮行为路径：记日志后照常走跟风/反义决策。
                self.ctx.logger.warning(f"注入表情回应上下文异常: {exc}")
            decision = await self._evaluate(event)
            if decision is None:
                return
            now = time.time()
            # 先占表情槽再发，避免同一条消息的并发上报重复贴。
            self._state.take_slot(event.message_id, now)
            self._state.mark_group(event.group_id, now)
            # 表情回应路径取 behavior 段延迟区间（change design D3：延迟按域
            # 取值，与复读段互不牵扯）。
            behavior = self.config.behavior
            # 归一逻辑在 _delay 内部（docstring：high < low 按 low、high <= 0
            # 不等待），调用点只传区间字段即可。
            await self._delay(behavior.delay_min_seconds, behavior.delay_max_seconds)
            await self._send_reaction(event, decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"处理表情回应失败: {exc}")

    async def _evaluate(self, event: ReactionEvent) -> Decision | None:
        """门禁 + 掷骰。返回 None 表示这次不做任何事。"""
        if not self.config.plugin.enabled or event.removed:
            return None
        if not self._emoji_action_supported:
            return None

        scope = self.config.scope
        if not group_allowed(event.group_id, scope.group_whitelist, scope.group_blacklist):
            return None
        ignored = qq_set(scope.ignore_reactors)
        if event.reactor_qq and event.reactor_qq in ignored:
            return None
        # 麦麦自己贴的表情不触发跟风；表情槽是这条环路的兜底防护。
        if self._self_qq and event.reactor_qq == self._self_qq:
            return None

        now = time.time()
        if self._state.is_slot_taken(event.message_id, now):
            return None
        if self._state.is_cooling_down(event.group_id, self.config.behavior.cooldown_seconds, now):
            return None

        # 回应骰需要知道原作者是谁（self 消息用独立概率），反义表情也不例外：
        # 反义分支只是回应后的第二次掷骰，不能绕过回应骰。
        is_self_message = await self._is_self_message(event.message_id, now)

        bandwagon = self.config.bandwagon
        decision = decide_reaction(
            event,
            is_self_message=is_self_message,
            opposite_map=self._opposite_map,
            bandwagon_probability=bandwagon.probability,
            self_message_probability=bandwagon.self_message_probability,
            opposite_probability=self.config.opposite.probability,
            roll=random.random,
        )
        if decision is not None:
            self.ctx.logger.info(
                f"表情回应触发 group={event.group_id} msg={event.message_id} "
                f"reactor={event.reactor_qq or '未知'} {decision.reason}"
            )
        return decision

    # ---- 上下文注入（实验性）----

    async def _inject_reaction(self, event: ReactionEvent) -> None:
        """去重、聚合后把表情回应注入聊天历史。失败只记日志，不阻塞行为路径。"""
        if self._injection_state is None or event.removed:
            return
        scope = self.config.scope
        if not group_allowed(event.group_id, scope.group_whitelist, scope.group_blacklist):
            return
        ignored = qq_set(scope.ignore_reactors)
        if event.reactor_qq and event.reactor_qq in ignored and event.reactor_qq != self._self_qq:
            return

        now = time.time()
        if not self._bot_display_name_resolved:
            # Resolved once per runtime rebuild (design.md D6): bot.nickname
            # rarely changes, so injections must not re-query it; the flag
            # is reset only by _rebuild_runtime (config reload path).
            self._bot_display_name = await self._fetch_bot_display_name()
        display = build_reactor_display(event.reactor_qq, self._self_qq, self._bot_display_name)
        if self._injection_state.record(event, display, now) == "skip":
            return
        await self._flush_due_aggregates()

    async def _flush_due_aggregates(self) -> None:
        """Flush every aggregate whose merge window has elapsed.

        Shared by the event-driven path in _inject_reaction and the resident
        flush loop. due() removes aggregates synchronously (no await point),
        which is what keeps the two trigger paths from double-delivering
        (design.md D3).
        """
        state = self._injection_state
        if state is None:
            return
        now = time.time()
        for aggregate in state.due(now):
            # The aggregate belongs to its own group: flush must not borrow the
            # triggering event's group (crosses groups under concurrency).
            await self._flush_aggregate(aggregate.group_id, aggregate)
        state.prune(now)

    def _ensure_flush_loop(self) -> None:
        """Ensure the resident flush loop is running while injection is on.

        The loop re-reads self._injection_state every tick, so a config
        reload that keeps injection enabled reuses the running loop instead
        of stacking a duplicate; a reload that turns injection off lets the
        loop exit on its own at the next tick.
        """
        if self._injection_state is None:
            return
        task = self._flush_loop_task
        if task is not None and not task.done():
            return
        self._flush_loop_task = self._spawn(self._injection_flush_loop())

    async def _injection_flush_loop(self) -> None:
        """Resident fallback flush loop (design.md D2).

        Event-driven flush depends on later reports arriving; this loop
        guarantees an aggregate whose merge window elapsed during a silent
        period is still injected within window + tick. The tick is hardcoded
        (not a config item). Each iteration re-reads self._injection_state so
        a config reload switches to the fresh state, and the loop exits by
        itself once injection is turned off.
        """
        while True:
            await asyncio.sleep(_INJECTION_FLUSH_TICK_SECONDS)
            if self._injection_state is None:
                return
            try:
                await self._flush_due_aggregates()
            except Exception as exc:
                # One failed tick must not kill the resident loop (BLE001-style
                # broad catch is intentional here, same as other task bodies).
                self.ctx.logger.warning(f"兜底 flush 表情回应聚合失败: {exc}")

    async def _flush_aggregate(
        self, group_id: str, aggregate: InjectionAggregate
    ) -> None:
        state = self._injection_state
        if state is None:
            return
        stream_id = await self._get_stream_id(group_id)
        if not stream_id:
            # The aggregate was already removed by due(); roll its dedup keys
            # back so the same reports can re-aggregate and retry once the
            # stream becomes available (design.md D4).
            state.rollback(aggregate)
            self.ctx.logger.debug(f"取不到群 {group_id} 的 stream，跳过注入")
            return
        text = build_reaction_visible_text(aggregate.message_id, aggregate.emoji_id, aggregate.responders)
        try:
            response = await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "data": text}],
                visible_text=text,
                source_kind="reaction",
                message_id=aggregate.message_id,
            )
        except Exception as exc:
            state.rollback(aggregate)
            self.ctx.logger.warning(f"注入表情回应上下文调用失败: {exc}")
            return
        if not (isinstance(response, dict) and response.get("success")):
            state.rollback(aggregate)
            self.ctx.logger.warning(f"注入表情回应上下文失败: {response}")
            return
        state.mark_injected(aggregate.message_id, time.time())
        self.ctx.logger.info(
            f"已注入表情回应上下文 group={group_id} msg={aggregate.message_id} "
            f"emoji={aggregate.emoji_id} 回应者={aggregate.responders}"
        )

    async def _get_stream_id(self, group_id: str) -> str:
        """群号 → 聊天流 ID；成功缓存，失败返回空串且不缓存（design.md D6）。

        The main program looks sessions up in memory and creates them lazily,
        so a failed lookup is transient: caching the empty string would pin
        the group as "no stream" forever. Success values are deterministic
        (session_id is stable), so they are cached as before.
        """
        cached = self._stream_ids.get(group_id)
        if cached is not None:
            return cached
        stream_id = ""
        try:
            stream = await self.ctx.chat.get_stream_by_group_id(group_id)
        except Exception as exc:
            self.ctx.logger.debug(f"查群 {group_id} 的 stream 失败: {exc}")
            stream = None
        if isinstance(stream, dict):
            stream_id = str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        if stream_id:
            self._stream_ids[group_id] = stream_id
        return stream_id

    async def _fetch_bot_display_name(self) -> str:
        """机器人显示名来源链：配置兜底 → 主程序 bot.nickname → 空。

        Resolved once (design.md D6): whatever the outcome (configured value,
        nickname, or lookup failure), _bot_display_name_resolved is set so the
        injection path stops re-querying; only _rebuild_runtime resets the
        flag (config reload may change self_display_name).
        """
        configured = str(self.config.context.self_display_name or "").strip()
        if configured:
            self._bot_display_name_resolved = True
            return configured
        try:
            result = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:
            self.ctx.logger.debug(f"取主程序 bot.nickname 失败: {exc}")
            result = ""
        self._bot_display_name_resolved = True
        return str(result or "").strip()

    # ---- 防御式拦截（chat.receive.before_process Hook）----

    @HookHandler(
        "chat.receive.before_process",
        name="emoji_reaction_notify_guard",
        mode="blocking",
        order="early",
        description="防御式拦截表情回应通知消息，避免其进入主程序上下文（ADR 0003）。",
    )
    async def guard_reaction_notify(self, **kwargs: Any) -> dict[str, Any]:
        """表情回应通知消息在主链上 abort；其余放行。

        命中时打 info 日志——这条日志就是「适配器是否把表情回应当通知转发」的
        线上答案（ADR 0001/0003）。上下文注入关闭时不拦截。
        """
        if self._injection_state is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        message = kwargs.get("message")
        if is_reaction_notice_message(message):
            self.ctx.logger.info(
                f"拦截表情回应通知消息进入主程序: {str(message)[:200]}"
            )
            return {"action": "abort", "modified_kwargs": kwargs}
        return {"action": "continue", "modified_kwargs": kwargs}

    # ---- 复读检测（chat.receive.before_process blocking Hook）----

    @HookHandler(
        "chat.receive.before_process",
        name="repeat_detector",
        mode="blocking",
        order="late",
        description="累积复读链，达标掷骰：复读/打断出口命中时拦截触发消息并转后台发送，沉默与非候选放行（ADR 0005/0006）。",
    )
    async def handle_repeat_message(self, **kwargs: Any) -> dict[str, Any]:
        """复读检测入口（blocking 模式：返回值决定触发消息在主链上的去留）。

        复读/打断出口命中 → spawn 后台发送任务后返回 abort：触发消息不写入
        主程序消息库、不进 planner 上下文（planner 拦截，design D1）。沉默
        出口、非候选、开关关闭 → continue 放行。未捕获异常记 warning 后返回
        continue——blocking 挂在主链上，宁可漏一条拦截也不让插件异常干扰
        主程序消息处理；CancelledError 照常上抛。
        """
        try:
            intercepted = await self._process_repeat_message(kwargs.get("message"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"复读检测处理消息失败: {exc}")
            return {"action": "continue", "modified_kwargs": kwargs}
        action = "abort" if intercepted else "continue"
        return {"action": action, "modified_kwargs": kwargs}

    async def _process_repeat_message(self, message: Any) -> bool:
        """门禁 → 链累积 → 三出口决策；返回触发消息是否被出口消费（拦截）。

        返回 True 即复读/打断出口命中：本方法内已完成写冷却与 spawn，handler
        据此对触发消息 abort。沉默出口、非候选、门禁不过一律返回 False
        （continue 放行）。从门禁到写冷却、spawn 为止没有任何 await 点：同群
        同文本的并发任务不可能在同一链上双触发（design D3 的同步原子段，
        终结标记在 record 内先于掷骰写入）。
        """
        repeat = self.config.repeat
        if not self.config.plugin.enabled or not repeat.enabled:
            return False
        if not isinstance(message, dict) or not isinstance(message.get("message_info"), dict):
            # 形态漂移打 debug 不打 warning（design D4 / ADR 0005）：hook 每条
            # 群消息都过，warning 会刷屏；序列化 SessionMessage 必含 dict 形态
            # 的 message_info（主程序 message_utils._session_message_to_dict）。
            self.ctx.logger.debug(f"复读检测收到形态异常的消息: {str(message)[:120]}")
            return False
        candidate = extract_repeat_candidate(
            message,
            self_qq=self._self_qq,
            max_text_length=int(repeat.max_text_length),
        )
        if candidate is None:
            # 私聊/通知/富媒体/命令/超长/麦麦自己：合法非候选，静默返回。
            return False
        scope = self.config.scope
        if not group_allowed(candidate.group_id, scope.group_whitelist, scope.group_blacklist):
            return False
        if candidate.sender_qq in qq_set(scope.ignore_senders):
            return False

        now = time.time()
        suppressed = self._repeat_state.is_cooling_down(
            candidate.group_id, float(repeat.cooldown_seconds), now
        )
        result = self._repeat_state.record(
            candidate.group_id,
            candidate.sender_qq,
            candidate.text,
            now,
            suppress_trigger=suppressed,
        )
        if result.status != TRIGGER:
            return False

        decision = decide_repeat(
            result.text,
            weight_repeat=repeat.weight_repeat,
            weight_interrupt=repeat.weight_interrupt,
            weight_silence=repeat.weight_silence,
            interrupt_texts=repeat.interrupt_texts,
            roll=random.random,
        )
        if decision is None:
            # 沉默出口：资格已在 record 内消耗（链终结同样成立），但不进群冷却。
            self.ctx.logger.info(
                f"复读链达标后保持沉默 group={candidate.group_id} text={result.text!r}"
            )
            return False
        if decision.used_default_pool:
            # 空池回落内置默认池（design D5）：回落事实由决策结果携带，此处
            # 只读标志记 warning，不静默哑火，也不重复推断池是否为空。
            self.ctx.logger.warning("打断文本池为空，已回落内置默认池")
        # 复读/打断出口：先写冷却再转后台发送——发送成败不影响已进入的冷却。
        self._repeat_state.mark_group(candidate.group_id, now)
        self.ctx.logger.info(
            f"复读链达标 group={candidate.group_id} {decision.reason} text={result.text!r}"
        )
        self._spawn(self._send_repeat_decision(candidate, decision))
        return True

    async def _send_repeat_decision(
        self, candidate: RepeatCandidate, decision: RepeatDecision
    ) -> None:
        """复读/打断出口的后台编舞（ADR 0006/0008，change design D4）。

        触发消息注入与延迟并发（gather，ADR 0008）：注入 RTT 与延迟取较大值、
        不串行叠加；之后 send_group_msg 协议端直发（失败止步）→ 行为文本注入
        （仅直发成功）——「谁发了什么」始终早于「麦麦复读/打断了什么」。
        直发只需群号，与 stream_id 彻底解耦（stream 仅注入需要），不走主程序
        标准发送链——ctx.send.text 路过的 blocking hook 可改写/取消消息且默认
        不进内存上下文（ADR 0006）。冷却与链终结在出口命中时已写入，与本任务
        成败解耦；注入失败不重试、不回滚、不复用表情回应域的 InjectionState。
        """
        group_id = candidate.group_id
        repeat = self.config.repeat
        try:
            # 1+2. 触发消息注入与延迟并发（ADR 0008）：补偿 planner 拦截造成
            #    的上下文缺失，同时避免决策即发。注入协程自身已吞常规异常，
            #    return_exceptions 兜住任何未预期异常——否则异常会落到外层
            #    except、把直发一并丢掉（design D4，return_exceptions 必需）。
            outcomes = await asyncio.gather(
                self._append_context(
                    group_id,
                    build_trigger_visible_text(candidate),
                    "repeat_trigger",
                    candidate.message_id,
                ),
                self._delay(repeat.delay_min_seconds, repeat.delay_max_seconds),
                return_exceptions=True,
            )
            for exc in outcomes:
                if isinstance(exc, Exception):
                    # 异常项显式记 warning 后继续（design D4）。CancelledError
                    # 不是 Exception 子类，不会被这里吞掉。exc 是 gather 返回
                    # 的异常实例而非当前抛出的异常，exc_info 需三元组形式
                    # 才能带出 traceback（logger.exception 在此不可用）。
                    self.ctx.logger.warning(
                        f"复读/打断后台并发协程异常: {exc}",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            # 3. 协议端直发（OneBot 11 核心动作，不做 retcode=1404 永久停用）。
            response = await self._call_protocol(
                "send_group_msg",
                {
                    # 群号与 message_id 同一协议端惯例：纯数字转 int。
                    "group_id": coerce_message_id(group_id),
                    "message": [{"type": "text", "data": {"text": decision.text}}],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"复读/打断直发失败（异常）: {exc}")
            return
        if response is None:
            self.ctx.logger.warning(f"复读/打断直发失败（协议端不可达）: group={group_id}")
            return
        if protocol_call_failed(response):
            detail = protocol_failure_detail(response)
            self.ctx.logger.warning(f"复读/打断直发失败: group={group_id} 原因={detail}")
            return
        self.ctx.logger.info(
            f"已直发{behavior_kind_label(decision.kind)}文本 group={group_id} text={decision.text!r}"
        )
        # 4. 行为文本注入：让麦麦知道自己发出了什么（仅直发成功）。文案
        #    区分行为类型（design D3），message_id 附直发响应 data.message_id。
        data = response.get("data")
        sent_message_id = data.get("message_id") if isinstance(data, dict) else None
        await self._append_context(
            group_id, build_behavior_visible_text(decision), decision.kind, sent_message_id
        )

    async def _append_context(
        self,
        group_id: str,
        text: str,
        source_kind: str,
        message_id: Any,
        log_prefix: str = "",
    ) -> None:
        """把一条域内上下文消息写入麦麦聊天历史（change design D3）。

        复读与转发两域共用：注入都是 planner 拦截 / 协议端直发的即时一次性补偿，
        无幂等键需求，不复用表情回应域的 InjectionState（去重/聚合/回滚是外部
        事件流聚合的需求）。失败（stream 查不到 / append 异常或失败）记 warning
        即返：不重试、不回滚，调用方照常走后续步骤——注入与直发成败解耦。

        `log_prefix` 只影响 warning 文案：转发域的注入日志带「自动转发」前缀
        （design D8 的路径前缀口径，便于按路径 grep），复读域不传、沿用
        `source_kind` 自带语境。
        """
        prefix = f"{log_prefix} " if log_prefix else ""
        stream_id = await self._get_stream_id(group_id)
        if not stream_id:
            self.ctx.logger.warning(
                f"{prefix}取不到群 {group_id} 的 stream，跳过注入 source_kind={source_kind}"
            )
            return
        try:
            response = await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "data": text}],
                visible_text=text,
                source_kind=source_kind,
                message_id=message_id,
            )
        except Exception as exc:
            # 宽捕获是有意的：注入失败不得拖垮编舞的后续步骤（BLE001 口径）。
            self.ctx.logger.warning(
                f"{prefix}注入上下文异常 group={group_id} source_kind={source_kind}: {exc}"
            )
            return
        if not (isinstance(response, dict) and response.get("success")):
            self.ctx.logger.warning(
                f"{prefix}注入上下文失败 group={group_id} source_kind={source_kind}: {response}"
            )
            return
        self.ctx.logger.info(
            f"已注入上下文 group={group_id} source_kind={source_kind} message_id={message_id}"
        )

    # ---- 转发域：自动路径（chat.receive.before_process observe Hook）----

    @HookHandler(
        "chat.receive.before_process",
        name="forward_detector",
        mode="observe",
        order="late",
        description="观察源群合并转发：门禁与防重后 spawn 后台编排，摘要就绪后才掷骰投递（ADR 0009/0011）。",
    )
    async def handle_forward_message(self, **kwargs: Any) -> None:
        """**自动转发**入口（observe 模式：不参与主链 abort 决策）。

        转发域**不拦截**源消息——源消息照常进主程序处理链与麦麦上下文。同步段
        只做纯内存工作（形态校验 → **源群**门禁 → **转发防重** mark → spawn
        后台编排），掷骰、冷却写入与投递全部推迟到**摘要就绪**之后（spec 自动
        转发的触发时机与摘要就绪）。observe 模式的返回值不改写 kwargs、不请求
        abort，因此返回 None；未捕获异常记 warning 后放行——挂在每条群消息的
        主链上，宁可漏一次转发也不干扰主程序消息处理。
        """
        try:
            self._process_forward_message(kwargs.get("message"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"自动转发检测处理消息失败: {exc}")

    def _process_forward_message(self, message: Any) -> None:
        """门禁 → 防重 → spawn 编排；全程无 await 点（change design D1）。

        同步段只做纯内存工作：形态与**源群**门禁、请求转发链路的入口提示、
        **转发防重** mark，然后 spawn 一个编排任务；掷骰、冷却写入、投递、注入
        都在编排任务里等**摘要就绪**之后才发生（spec 自动转发的触发时机与摘要
        就绪）。防重标记先于等待写入：等待期间重复上报不产生第二次编排。
        自动路径整段跟随 `forward.auto_enabled`（入口提示另行跟随
        `forward.request_enabled`）。
        """
        forward = self.config.forward
        if not self._forward_enabled():
            return
        if not forward.auto_enabled:
            # 自动路径停用：Hook 整段短路（含源群 Planner 入口提示），请求转发
            # 路径由 request_enabled 与转发 Tool 单独控制（spec 转发路径开关）。
            return
        source = parse_forward_source(message, self._self_qq)
        if source is None:
            self.ctx.logger.debug(f"自动转发跳过非候选消息: {str(message)[:120]}")
            return
        if source.group_id not in qq_set(forward.source_groups):
            return
        if self._forward_runtime is not None and forward.request_enabled:
            # 请求转发链路的入口提示（spec 请求转发链路）：源群 Planner 触发由
            # 运行时按 trigger_source_planner / 永久防重自行判定，冷却与防重
            # 只约束自动路径，不压制这条提示。
            self._forward_runtime.observe_source_message(message)
        now = time.time()
        if self._forward_state.is_duplicate(
            source.message_id, float(forward.dedup_ttl_seconds), now
        ):
            return
        if self._forward_state.is_group_cooling(
            source.group_id, float(forward.cooldown_seconds), now
        ):
            return
        self._forward_state.mark_message(source.message_id, now)
        self._spawn_forward(self._orchestrate_forward_delivery(source))

    async def _orchestrate_forward_delivery(self, source: ForwardSource) -> None:
        """自动路径后台编排：等**摘要就绪** → 逐目标群掷骰 → 写冷却 → 后台投递。

        就绪等待只做一次，取得的**富摘要**共享给本次全部目标群的注入与 intent。
        全沉默不写任何冷却；命中先写**源群**与**目标群**冷却再 spawn 投递，
        投递成败不影响已进入的冷却（change design D1/D2/D7）。
        """
        rich_summary = await self._wait_for_forward_summary(source)
        if rich_summary is None:
            return
        forward = self.config.forward
        now = time.time()
        # **目标群**冷却只压制该群：先按请求路径同款口径归一（去空、去重、去
        # 首尾空白），再剔除冷却中的群，最后对剩余群逐个独立掷骰。
        candidates = [
            group_id
            for group_id in GroupIdList.normalize(forward.target_groups)
            if not self._forward_state.is_target_cooling(
                group_id, float(forward.target_cooldown_seconds), now
            )
        ]
        hits = decide_forward_targets(
            candidates,
            source.group_id,
            forward.weight_send,
            forward.weight_silence,
            random.random,
        )
        if not hits:
            # 沉默出口：不写任何冷却，后续消息仍可正常参与掷骰。
            self.ctx.logger.info(
                f"自动转发掷骰全部沉默 group={source.group_id} msg={source.message_id}"
            )
            return
        # 先写冷却再 spawn：投递成败不影响已进入的冷却（与复读域同款顺序）。
        self._forward_state.mark_group(source.group_id, now)
        self._forward_state.mark_targets(hits, now)
        self.ctx.logger.info(
            f"自动转发命中 group={source.group_id} msg={source.message_id} targets={hits}"
        )
        if self._resolve_forward_mode() == FORWARD_MODE_PLANNER:
            # 决策模式：命中后不直发，把**富摘要**交目标群 Planner 决策
            # 「转发 / 不转发」（spec 转发模式）。
            await self._dispatch_forward_decisions(source, hits, rich_summary)
            return
        await self._run_forward_delivery(source, hits, rich_summary)

    def _resolve_forward_mode(self) -> str:
        """`forward.mode` 归一：`direct` / `planner` 之外的值按 direct 处理并记 warning。

        非法值不静默：配置里写错模式名时，日志必须能解释为什么走了直发（design D6）。
        """
        mode = str(self.config.forward.mode or "").strip().lower()
        if mode in FORWARD_MODES:
            return mode
        self.ctx.logger.warning(
            f"自动转发模式非法（mode={self.config.forward.mode!r}），按 direct 处理"
        )
        return FORWARD_MODE_DIRECT

    async def _dispatch_forward_decisions(
        self, source: ForwardSource, targets: list[str], rich_summary: str
    ) -> None:
        """决策模式：逐命中目标群签发**决策票**并触发该群 Planner 决策（design D4）。

        票里登记**转发源消息快照**与**富摘要**——模型决策回来时经决策 Tool 取出
        快照，发送字节仍 100% 来自 `before_process` 时点。触发失败（取不到 stream /
        异常 / 失败信封）按「不转发」收尾：记 warning、焚掉已签发的票，不投递。
        票 TTL 到期仍未被消费（模型始终没调决策 Tool）同样按「不转发」处理：每张
        超时票记一条含 msg_id 与目标群号的 warning，不得由 TTL 清理静默丢弃
        （spec 转发模式）。
        """
        for target_group_id in targets:
            # 每次签发前先清算超时票（含先前目标群等待期间过期的票）：清算与
            # 签发之间无 await，`issue` 的内部清理不会再遇到未被报告的过期票。
            expired_keys = self._forward_tickets.discard_expired(time.time())
            for message_id, expired_target in expired_keys:
                self.ctx.logger.warning(
                    f"自动转发决策票超时未消费（按不转发处理）: "
                    f"target={expired_target} msg={message_id}"
                )
            now = time.time()
            issued = self._forward_tickets.issue(
                source.message_id, target_group_id, (source, rich_summary), now=now
            )
            if issued is None:
                self.ctx.logger.warning(
                    f"自动转发决策模式无法签发决策票（防重窗口为 0？），按不转发处理: "
                    f"target={target_group_id} msg={source.message_id}"
                )
                continue
            stream_id = await self._get_stream_id(target_group_id)
            if not stream_id:
                self._burn_forward_ticket(
                    source.message_id, target_group_id, "跳过目标群（取不到 stream）"
                )
                continue
            intent = build_forward_decision_intent(source, target_group_id, rich_summary)
            try:
                response = await self.ctx.maisaka.proactive.trigger(
                    stream_id,
                    intent=intent,
                    reason="自动转发命中，等待目标群转发决策",
                    priority="normal",
                    metadata={
                        "trigger_kind": _FORWARD_DECISION_TRIGGER_KIND,
                        "source_message_id": source.message_id,
                        "target_group_id": target_group_id,
                    },
                )
            except Exception as exc:
                # 宽捕获是有意的（BLE001 口径）：单个目标群触发失败不熔断其余目标群。
                self._burn_forward_ticket(
                    source.message_id, target_group_id, f"触发异常（原因={exc}）"
                )
                continue
            if not (isinstance(response, dict) and response.get("success")):
                self._burn_forward_ticket(
                    source.message_id, target_group_id, f"触发失败（响应={response}）"
                )
                continue
            self.ctx.logger.info(
                f"自动转发决策模式已触发目标群决策 target={target_group_id} "
                f"msg={source.message_id} ticket={issued}"
            )

    def _burn_forward_ticket(
        self, message_id: str, target_group_id: str, reason: str
    ) -> None:
        """焚掉不再可用的**决策票**并按「不转发」收尾：记 warning（spec 转发模式）。

        `reason` 由调用方拼好（取不到 stream / 触发异常 / 触发失败），本方法只
        负责焚票与统一文案，避免三处同构分支各写一遍「consume + warning」。
        """
        self._forward_tickets.consume(message_id, target_group_id, now=time.time())
        self.ctx.logger.warning(
            f"自动转发决策模式{reason}，按不转发处理: "
            f"target={target_group_id} msg={message_id}"
        )

    async def _run_forward_delivery(
        self, source: ForwardSource, targets: list[str], rich_summary: str
    ) -> list[str]:
        """spawn 每个目标群的投递任务并等齐，成功后注入**转发留痕**（design D1/D7）。

        直发模式（编排任务内）与决策模式（决策 Tool 命中后）共用同一条编舞与
        留痕口径；**富摘要**随本次就绪结果共享给全部目标群的注入与 intent。
        投递任务经 `_spawn_forward` 登记，卸载取消覆盖这些投递任务本身；等齐
        （`gather`）发生在调用方任务内——直发模式的调用方是转发域编排任务，
        决策模式的调用方是 Host 的 Tool handler 任务（不在转发域任务登记内）。
        """
        tasks = [
            self._spawn_forward(
                self._deliver_forward_to_target(source, target, rich_summary=rich_summary)
            )
            for target in targets
        ]
        return await self._finalize_forward_delivery(source, targets, tasks)

    async def _wait_for_forward_summary(self, source: ForwardSource) -> str | None:
        """轮询等待**摘要就绪**；就绪返回**富摘要**，超时记 warning 返回 None（design D2）。

        首次立即查，之后按 `forwards.summary` 的退避间隔等待（0.5s 起步、1s
        封顶）；总时长受 `forward.summary_timeout_seconds` 约束。查询异常按
        未就绪续等（debug，不放弃本次）——只有超时才放弃，超时后不掷骰、不写
        冷却、不投递、不注入（spec 自动转发的触发时机与摘要就绪）。
        """
        timeout = float(self.config.forward.summary_timeout_seconds)
        started = time.monotonic()
        attempt = 0
        while True:
            message = await self._fetch_forward_source_message(source)
            rich_summary = extract_rich_summary(message)
            if rich_summary:
                return rich_summary
            elapsed = time.monotonic() - started
            if should_give_up(elapsed, timeout):
                self.ctx.logger.warning(
                    f"自动转发摘要就绪超时（放弃本次，不掷骰不写冷却不投递）: "
                    f"msg={source.message_id} group={source.group_id}"
                )
                return None
            await asyncio.sleep(next_poll_interval(attempt))
            attempt += 1

    async def _fetch_forward_source_message(self, source: ForwardSource) -> Any:
        """查一次**转发源消息**的入库形态；异常按未就绪返回 None（design D2）。

        只按消息 ID 查（消息 ID 在平台内唯一），**不带 stream_id**：同一群在
        `chat_sessions` 里可能有多个 session，主程序平滑迁移会话后插件缓存里的
        旧 stream 会变成一个查不到任何消息的陈旧键——带上它过滤会让就绪判定
        永远为假（2026-09-27 线上实测：主机侧按消息 ID 查得到、按旧 stream 查
        不到，插件恒收到空）。`include_binary_data=False` 只读就绪状态与
        **富摘要**，媒体字节一律取自 `before_process` 快照。
        """
        query: dict[str, Any] = {"include_binary_data": False}
        try:
            return await self.ctx.message.get_by_id(source.message_id, **query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 宽捕获是有意的（BLE001 口径）：单次查询失败不放弃候选，只当未就绪
            # 续等；轮询期间不刷 warning，避免每一轮都打一条无人可用的告警。
            self.ctx.logger.debug(
                f"自动转发查询消息就绪状态失败（按未就绪续等）: msg={source.message_id} 原因={exc}"
            )
            return None

    async def _deliver_forward_to_target(
        self, source: ForwardSource, target_group_id: str, *, rich_summary: str = ""
    ) -> bool:
        """单个**目标群**的投递编舞（design D7）：延迟 → 可用性校验 → 直发 → 注入 → 强制发言。

        返回该目标群是否投递成功（供**转发留痕**汇总）。任一步失败记 warning
        后返回 False：不重试、不熔断其它目标群与后续消息。内容注入先于
        **强制发言**（spec 转发内容注入），且只在直发成功后发生（spec 自动转发
        的投递）。`rich_summary` 由编排任务在**摘要就绪**时取到、随调用传入，
        投递任务内不再查询消息。
        """
        forward = self.config.forward
        try:
            await self._delay(forward.delay_min_seconds, forward.delay_max_seconds)
            if not (self.config.protocol.base_url or "").strip():
                # 协议端未配置：明确放弃投递（spec 转发域开关，不静默失败）。
                self.ctx.logger.warning(
                    f"自动转发放弃投递：protocol.base_url 未配置 target={target_group_id}"
                )
                return False
            # 可用性校验（design D4）：缓存新鲜时直接查表，未命中或过期时先补
            # 一次群列表查询；查询失败按未知放行——校验是减少无效投递的优化，
            # 不允许变成新的静默失败源（task 5.2）。
            await self._ensure_group_availability("自动转发")
            if not self._forward_availability.is_available(target_group_id, time.time()):
                self.ctx.logger.warning(
                    f"自动转发跳过不可用目标群 target={target_group_id}（不在机器人群列表）"
                )
                return False
            messages = nodes_to_onebot_messages(source.nodes)
            if not messages:
                self.ctx.logger.warning(
                    f"自动转发放弃投递：转发节点重建为空 target={target_group_id}"
                )
                return False
            # 协议端直发：只依赖群号，与 Host 聊天流/Platform IO 路由解耦（ADR 0010）。
            if await self._send_group_forward(target_group_id, messages) is None:
                return False
            self.ctx.logger.info(
                f"自动转发已投递 target={target_group_id} msg={source.message_id}"
            )
            await self._register_forward_delivered(source, target_group_id)
            await self._append_forward_content(source, target_group_id, rich_summary=rich_summary)
            if forward.trigger_target_planner:
                await self._trigger_forward_comment(
                    source, target_group_id, rich_summary=rich_summary
                )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"自动转发投递异常: target={target_group_id} 原因={exc}")
            return False

    async def _send_group_forward(
        self, target_group_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """**协议端直发**一条合并转发（`send_group_forward_msg`，ADR 0010，task 5.1）。

        成功返回协议端完整响应体：`data` 只保证兼容 `message_id` / `res_id` /
        `forward_id` 三种键（不同协议端实现给的不一样），调用方不得依赖其中某
        一个。失败返回 None：不可达与 HTTP 200 + `status != ok` 沿用既有成功
        判定口径，带「自动转发」前缀与目标群号记 warning，不重试、不熔断后续
        目标群与后续消息（design D8）。
        """
        response = await self._call_protocol(
            "send_group_forward_msg",
            {
                # 群号按协议端惯例：纯数字转 int（与复读直发同款归一）。
                "group_id": coerce_message_id(target_group_id),
                "messages": messages,
            },
        )
        if response is None:
            self.ctx.logger.warning(
                f"自动转发投递失败（协议端不可达）: target={target_group_id}"
            )
            return None
        if protocol_call_failed(response):
            detail = protocol_failure_detail(response)
            self.ctx.logger.warning(
                f"自动转发投递失败: target={target_group_id} 原因={detail}"
            )
            return None
        return response

    async def _finalize_forward_delivery(
        self,
        source: ForwardSource,
        targets: list[str],
        tasks: list[asyncio.Task],
    ) -> list[str]:
        """等齐本次命中的投递任务，成功后向**源群**注入**转发留痕**（design D7）。

        成功集合按 `target_groups` 顺序取（与配置顺序一致，文案稳定）；全部失败
        不注入（spec 转发留痕注入）。任务取消或异常一律按未成功处理，不阻断
        其它目标群。返回实际投递成功的目标群列表，供调用方（决策 Tool）汇报结果。
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        delivered = [target for target, result in zip(targets, results) if result is True]
        if not delivered:
            self.ctx.logger.info(f"自动转发本次无成功目标群 msg={source.message_id}")
            return []
        await self._append_forward_trace(source, delivered)
        return delivered

    async def _append_forward_trace(
        self, source: ForwardSource, delivered_targets: list[str]
    ) -> None:
        """**转发留痕注入**：向**源群**注入实际投递成功的目标群清单。

        `source_kind=forward_action`（spec 转发留痕注入）；文案只列成功群，
        空清单不注入。注入失败由 `_append_context` 记 warning，不回滚已完成的投递。
        """
        text = build_forward_trace_text(delivered_targets)
        if not text:
            return
        await self._append_context(
            source.group_id, text, "forward_action", source.message_id, log_prefix="自动转发"
        )

    async def _register_forward_delivered(
        self, source: ForwardSource, target_group_id: str
    ) -> None:
        """登记**已投递状态**：键为源聊天流 + **转发源消息** ID（design D5）。

        **请求转发**按同一个键拒绝重复投递。源群 stream 查不到时跳过登记
        （键不完整就无从与请求路径对齐），只记 warning。
        """
        stream_id = await self._get_stream_id(source.group_id)
        if not stream_id:
            self.ctx.logger.warning(
                f"自动转发无法登记已投递状态：取不到源群 {source.group_id} 的 stream "
                f"target={target_group_id}"
            )
            return
        self._forward_delivered.register(
            build_delivered_key(stream_id, source.message_id),
            [target_group_id],
            time.time(),
        )

    async def _append_forward_content(
        self, source: ForwardSource, target_group_id: str, *, rich_summary: str = ""
    ) -> None:
        """**转发内容注入**：把这条合并转发的可读文本注入**目标群**聊天历史。

        `source_kind=forward`（spec 转发内容注入），内容主体是**富摘要**
        （design D3）；来源字段与富摘要的清洗在 `forwards.texts` 内完成。
        注入失败只记 warning，不重试、不阻断后续的**强制发言**。
        """
        text = build_forward_content_text(
            source,
            rich_summary=rich_summary,
            max_chars=int(self.config.forward.inject_max_chars),
        )
        await self._append_context(
            target_group_id, text, "forward", source.message_id, log_prefix="自动转发"
        )

    async def _trigger_forward_comment(
        self, source: ForwardSource, target_group_id: str, *, rich_summary: str = ""
    ) -> None:
        """**强制发言**：向**目标群**触发一次要求针对这条转发发言的 Planner 主动任务。

        软约束（spec 强制发言）：Planner 仍可能沉默，插件不检测也不补发；意图
        材料取自**富摘要**（design D3）。触发失败只记 warning，不影响已完成的
        投递与注入。
        """
        stream_id = await self._get_stream_id(target_group_id)
        if not stream_id:
            self.ctx.logger.warning(
                f"自动转发跳过强制发言触发：取不到群 {target_group_id} 的 stream"
            )
            return
        intent = build_forced_comment_intent(source, rich_summary=rich_summary)
        try:
            response = await self.ctx.maisaka.proactive.trigger(
                stream_id,
                intent=intent,
                reason="自动转发投递成功",
                priority="normal",
                metadata={
                    "trigger_kind": _FORWARD_COMMENT_TRIGGER_KIND,
                    "source_message_id": source.message_id,
                },
            )
        except Exception as exc:
            self.ctx.logger.warning(f"自动转发强制发言触发异常: target={target_group_id} 原因={exc}")
            return
        if not (isinstance(response, dict) and response.get("success")):
            self.ctx.logger.warning(
                f"自动转发强制发言触发失败: target={target_group_id} 响应={response}"
            )
            return
        self.ctx.logger.info(f"自动转发已触发目标群发言 target={target_group_id}")

    async def _refresh_group_list(self) -> bool:
        """协议端 `get_group_list` → **群可用性缓存**（design D4，task 5.2）。

        返回本次是否拿到可信群列表。查询失败（不可达 / 协议端失败 / 返回体不
        认识）时按未知处理：不建立可信快照，`is_available` 未知放行并只记
        warning——校验是「减少无效投递」的优化，不允许变成新的静默失败源。
        失败同时登记时刻，退避语义见 `_ensure_group_availability`。

        日志一律用稳定域 tag「跨群转发」：本任务可能被自动 / 请求两条路径
        共享等待，不能携带任何单一调用方的路径前缀（按路径的成败由各等待方
        在 `await` 处自行记录）。
        """
        response = await self._call_protocol("get_group_list", {})
        if response is None:
            self._mark_forward_group_list_failure()
            self.ctx.logger.warning(
                "跨群转发取群列表失败（协议端不可达），目标群可用性校验按未知放行"
            )
            return False
        if protocol_call_failed(response):
            detail = protocol_failure_detail(response)
            self._mark_forward_group_list_failure()
            self.ctx.logger.warning(f"跨群转发取群列表失败: 原因={detail}")
            return False
        data = response.get("data")
        groups = [
            str(item.get("group_id") or "").strip()
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict)
        ]
        self._forward_availability.update(groups, time.time())
        if not groups:
            self._mark_forward_group_list_failure()
            self.ctx.logger.warning(
                "跨群转发群列表返回体不认识（没有可用群号），可用性校验按未知放行"
            )
            return False
        self._forward_group_list_failed_at = None
        self.ctx.logger.info(f"跨群转发群列表已刷新: {len(groups)} 个群")
        return True

    async def _ensure_group_availability(self, log_prefix: str) -> None:
        """确保**群可用性缓存**新鲜（design D4，task 5.2）。

        缓存新鲜（TTL 300s 内）直接返回，不触碰协议端；未命中或过期时查一次
        群列表。并发投递共用一个在途刷新任务（in-flight 标记）：第一个到达者
        发起查询，其余等待同一结果，不会每个目标群各查一次。查询失败只记
        warning，缓存保持未知——调用方的 `is_available` 按未知放行。

        刷新失败后 `_FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS` 内不再发起
        新查询：首次失败由刷新任务记 warning，退避窗口内跳过且不刷 warning，
        窗口外再试——协议端不可达时不至于每条投递都吃一次协议端超时。

        `log_prefix` 是**等待方**的路径前缀（自动投递传「自动转发」、请求转发
        传「请求转发」），无默认值：本次刷新的成败由各等待方在 `await` 处按
        自己的前缀记录，刷新任务本身只用稳定域 tag（见 `_refresh_group_list`）。
        """
        now = time.time()
        if self._forward_availability.has_fresh_data(now):
            return
        task = self._forward_group_list_task
        if task is None or task.done():
            if self._forward_group_list_backoff_active(now):
                # 退避窗口内：不发起新查询、不重复刷 warning（失败已记录）。
                return
            task = self._spawn_forward(self._refresh_group_list())
            self._forward_group_list_task = task
            # 刷新任务可能先失去全部等待者（等待方被取消）再抛非取消异常：
            # 创建处消费一次异常，避免 asyncio 打「Task exception was never
            # retrieved」；有等待者时 await 照常把异常抛给等待方记录。
            task.add_done_callback(_consume_task_exception)
        try:
            refreshed = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 宽捕获是有意的（BLE001 口径）：刷新任务失败不得拖垮投递，未知放行。
            self._mark_forward_group_list_failure()
            self.ctx.logger.warning(f"{log_prefix}群列表刷新任务异常: {exc}")
            return
        if refreshed:
            # 成功细节由刷新任务按域 tag 记 info，这里只补路径归属的调试记录。
            self.ctx.logger.debug(f"{log_prefix}群列表刷新成功，可用性校验按最新列表判定")
        else:
            self.ctx.logger.warning(f"{log_prefix}群列表刷新失败，目标群可用性校验按未知放行")

    def _mark_forward_group_list_failure(self) -> None:
        """登记最近一次群列表刷新失败时刻，作为退避窗口起点。"""
        self._forward_group_list_failed_at = time.time()

    def _forward_group_list_backoff_active(self, now: float) -> bool:
        """距最近一次刷新失败是否仍在退避窗口内（无失败记录即不在窗口内）。"""
        failed_at = self._forward_group_list_failed_at
        if failed_at is None:
            return False
        return now - failed_at < _FORWARD_GROUP_LIST_FAILURE_BACKOFF_SECONDS

    async def _is_self_message(self, message_id: str, now: float) -> bool:
        """被回应消息的原作者是否是麦麦自己。查不到时按他人消息处理。"""
        if not self._self_qq:
            return False
        cached = self._state.recall_author(message_id, now)
        if cached:
            return cached == self._self_qq
        response = await self._call_protocol("get_msg", {"message_id": coerce_message_id(message_id)})
        author = parse_message_author(response)
        if not author:
            self.ctx.logger.debug(f"get_msg 取不到 {message_id} 的原作者，按他人消息处理")
            return False
        self._state.remember_author(message_id, author, now)
        return author == self._self_qq

    async def _delay(self, low: float, high: float) -> None:
        """按调用方给定的 (low, high) 区间随机延迟（change design D3）。

        参数化区间而不是复制第二份延迟函数：表情回应路径传 behavior 段、
        复读路径传 repeat 段，两域延迟从此分开调。归一逻辑留在函数内：
        high < low 时按 low 处理、high <= 0 时不等待。
        """
        low = max(float(low), 0.0)
        high = max(float(high), low)
        if high <= 0.0:
            return
        await asyncio.sleep(random.uniform(low, high))

    async def _send_reaction(self, event: ReactionEvent, decision: Decision) -> None:
        response = await self._call_protocol(
            "set_msg_emoji_like",
            {
                "message_id": coerce_message_id(event.message_id),
                "emoji_id": decision.emoji_id,
                # NapCat 4.17.25 起该动作必须显式带 set，否则请求被静默忽略。
                "set": True,
            },
        )
        if response is None:
            self.ctx.logger.warning(f"贴表情失败（协议端不可达）: emoji={decision.emoji_id}")
            return
        if is_missing_action(response):
            self._emoji_action_supported = False
            self.ctx.logger.error(
                "协议端不支持 set_msg_emoji_like（retcode=1404），已停止贴表情。"
                "请确认协议端是否为 NapCat 系实现。"
            )
            return
        if protocol_call_failed(response):
            detail = protocol_failure_detail(response)
            self.ctx.logger.warning(f"贴表情被协议端拒绝: emoji={decision.emoji_id} 原因={detail}")
            return
        kind = "反义回应" if decision.kind == OPPOSITE else "跟风"
        self.ctx.logger.info(
            f"已{kind}贴表情 group={event.group_id} msg={event.message_id} emoji={decision.emoji_id}"
        )

    # ---- 协议端直调 ----

    async def _call_protocol(self, action_name: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """直连协议端 OneBot HTTP（路径风格 + Bearer）。见 docs/adr/0001。

        不可达返回 None；协议端失败时可能 HTTP 200 + status=failed，故一律返回响应体交由调用方判定。
        """
        base_url = (self.config.protocol.base_url or "").strip().rstrip("/")
        if not base_url:
            self.ctx.logger.warning(f"protocol.base_url 未配置，动作 {action_name} 无法执行")
            return None

        headers = {"Content-Type": "application/json"}
        token = (self.config.protocol.access_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(total=_PROTOCOL_TIMEOUT_SECONDS)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{base_url}/{action_name}", json=params or {}, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        self.ctx.logger.debug(
                            f"协议端动作 {action_name} HTTP {resp.status}: {text[:200]}"
                        )
                        return None
        except Exception as exc:
            self.ctx.logger.debug(f"调用协议端动作 {action_name} 失败: {exc}")
            return None

        try:
            response = json.loads(text) if text.strip() else {}
        except Exception:
            response = {}
        return response if isinstance(response, dict) else {}

    async def _startup_checks(self) -> None:
        """探活协议端并补齐麦麦自己的 QQ 号。失败只告警，不阻断加载。

        不探测 set_msg_emoji_like：该动作没有无副作用的探测方式，
        真实支持情况在首次贴表情时按 retcode 判定。转发域开启时额外校验
        协议端 HTTP 通道与群列表可用性（spec 转发域开关：协议端未配置时必须
        在启动自检里可见，自动转发命中时放弃投递且不静默）。
        """
        base_url = (self.config.protocol.base_url or "").strip()
        if not base_url:
            self.ctx.logger.warning("protocol.base_url 未配置，插件不会贴任何表情")
            if self._forward_enabled():
                self.ctx.logger.warning(
                    "跨群转发已启用但 protocol.base_url 未配置，自动转发命中时将放弃投递"
                )
            return

        response = await self._call_protocol("get_login_info", {})
        if response is None:
            self.ctx.logger.warning("协议端不可达，请检查 protocol.base_url 与网络")
            return
        if protocol_call_failed(response):
            detail = protocol_failure_detail(response)
            self.ctx.logger.warning(f"协议端 get_login_info 返回失败（{detail}），请检查 access_token")
            return

        if not self._self_qq:
            self._self_qq = parse_self_qq(response)
        if self._self_qq:
            self.ctx.logger.info(f"麦麦 QQ 号: {self._self_qq}")
        else:
            self.ctx.logger.warning(
                "取不到麦麦自己的 QQ 号，「有人给麦麦自己消息贴表情」将按他人消息的概率处理；"
                "可在配置里手填 protocol.self_qq"
            )
        if self._forward_enabled():
            await self._refresh_group_list()

    # ---- 转发域：请求路径（Planner Hook / Tool，全部委托 ForwardingRuntime）----

    @HookHandler(
        "maisaka.planner.before_request",
        name="forward_view_result_capture",
        mode="blocking",
        order="late",
        description="同步源群查看资格，并在待判断时向 Planner 请求追加一次续轮提醒（请求转发链路）。",
    )
    async def capture_view_forward_result(self, **kwargs: Any) -> dict[str, Any]:
        """源群 Planner 请求前：同步查看资格 + 续轮判断提醒（原插件链路契约）。

        kwargs 改写（追加提醒）只在 blocking 模式下生效，因此本 handler 与
        after_response 两个 handler 都用 blocking（allow_abort=false，返回
        continue 即放行）。委托失败记 warning 后放行——转发域不得拖垮主链。
        """
        runtime = self._forward_runtime
        session_id = str(kwargs.get("session_id") or "").strip()
        if runtime is None or not session_id:
            return {"action": "continue", "modified_kwargs": kwargs}
        try:
            history_items = kwargs.get("items") if "items" in kwargs else kwargs.get("messages")
            runtime.capture_view_results(session_id, history_items)
            reminder = runtime.build_view_judgment_reminder(session_id)
            if reminder:
                if isinstance(kwargs.get("items"), list):
                    kwargs["items"] = append_user_reminder(kwargs["items"], reminder)
                elif isinstance(kwargs.get("messages"), list):
                    kwargs["messages"].append({"role": "user", "content": reminder})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"转发域查看资格同步失败: {exc}")
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.after_response",
        name="forward_request_authorize",
        mode="blocking",
        order="early",
        description="清洗转发 Tool 参数并签发一次性会话凭据（请求转发链路，EARLY）。",
    )
    async def authorize_forward_request_context(self, **kwargs: Any) -> dict[str, Any]:
        """模型响应后（EARLY）：清洗参数、签发一次性凭据并标记本轮。

        只接受本 Hook 签发的凭据，正式 Tool handler 不信任模型可写的
        platform / group_id / stream_id 字段（spec 请求转发链路）。
        """
        runtime = self._forward_runtime
        if runtime is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id") or "").strip()
        try:
            if "output_items" in kwargs:
                output_items, authorization_round = runtime.authorize_forward_output_items(
                    session_id, kwargs.get("output_items")
                )
                kwargs["output_items"] = output_items
            else:
                tool_calls, authorization_round = runtime.authorize_forward_calls(
                    session_id, kwargs.get("tool_calls")
                )
                kwargs["tool_calls"] = tool_calls
            kwargs[FORWARD_AUTHORIZATION_ROUND_KWARG] = authorization_round
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"转发域请求授权失败: {exc}")
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.after_response",
        name="forward_auto_view_orchestrator",
        mode="blocking",
        order="late",
        description="按当前查看资格编排自动查看续接，替换/清洗本轮输出（请求转发链路，LATE）。",
    )
    async def orchestrate_view_before_forward(self, **kwargs: Any) -> dict[str, Any]:
        """模型响应后（LATE）：编排自动查看续接（路径 B 替换本轮输出）。

        本轮标记由 EARLY handler 写入、LATE handler 消费（消费即弹出，避免
        标记泄漏到 Host 的 kwargs）。
        """
        runtime = self._forward_runtime
        authorization_round = str(kwargs.pop(FORWARD_AUTHORIZATION_ROUND_KWARG, "") or "")
        session_id = str(kwargs.get("session_id") or "").strip()
        if runtime is None or not session_id:
            return {"action": "continue", "modified_kwargs": kwargs}
        try:
            if "output_items" in kwargs:
                kwargs["output_items"] = await runtime.transform_after_output_items(
                    session_id, kwargs.get("output_items"), authorization_round
                )
            else:
                response, tool_calls = await runtime.transform_after_response(
                    session_id,
                    kwargs.get("response"),
                    kwargs.get("tool_calls"),
                    authorization_round,
                )
                kwargs["response"] = response
                kwargs["tool_calls"] = tool_calls
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"转发域响应编排失败: {exc}")
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        FORWARD_TOOL_NAME,
        description=(
            "把本群的一条合并转发消息分享到其它群聊。只在 QQ 群聊可用；"
            "调用前必须已经完整查看该转发内容，msg_id 取自查看结果。"
        ),
        parameters={
            "msg_id": {
                "type": "string",
                "description": "要分享的合并转发消息 ID（msg_id）",
                "required": True,
            },
            "sharing_reason": {
                "type": "string",
                "description": "为什么值得分享（一句话，可空）",
            },
            "content_summary": {
                "type": "string",
                "description": "查看连续失败达到降级阈值后使用的忠实内容摘要（可空）",
            },
        },
        chat_scope="group",
        # 请求路径要等真实顺序投递，Host 默认 60s 可能不够（对齐原插件声明）。
        timeout_ms=120000,
    )
    async def request_cross_group_forward(
        self,
        msg_id: str,
        sharing_reason: str = "",
        content_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """转发 Tool（`chat_scope="group"`）：整段委托请求转发运行时。

        运行时不读模型可写的平台/群号/聊天流字段，只消费 EARLY Hook 签发的
        一次性凭据（在 kwargs 里）；因此这里把 Host 注入的完整上下文字典原样透传。
        请求路径整段跟随 `forward.request_enabled`（spec 转发路径开关）。
        """
        runtime = self._forward_runtime
        if runtime is None:
            return {"success": False, "content": "跨群转发未启用，无法执行转发请求。"}
        if not self.config.forward.request_enabled:
            return {"success": False, "content": "跨群转发请求路径已关闭，无法执行转发请求。"}
        return await runtime.request_forward(msg_id, sharing_reason, content_summary, kwargs)

    @Tool(
        DECIDE_FORWARD_TOOL_NAME,
        description=(
            "决定是否把一条合并转发投递到本群。只在收到转发决策请求时调用；"
            "message_id 与目标群号取自该请求，deliver 表示转发（true）或不转发（false）。"
        ),
        parameters={
            "message_id": {
                "type": "string",
                "description": "转发决策请求里给出的合并转发消息 ID",
                "required": True,
            },
            "deliver": {
                "type": "boolean",
                "description": "true = 转发到本群；false = 不转发",
                "required": True,
            },
        },
        chat_scope="group",
        # 决策为转发时要等真实投递完成，与请求转发路径同款超时口径。
        timeout_ms=120000,
    )
    async def decide_forward_delivery(
        self, message_id: str, deliver: bool, **kwargs: Any
    ) -> dict[str, Any]:
        """转发决策 Tool（`chat_scope="group"`，spec 转发模式）。

        用调用上下文里的**目标群号** + `message_id` 精确匹配一次性**决策票**
        （存在 / 未过期 / 未消费；票号不暴露给模型，错群自然匹配不到）。`deliver=true`
        走与直发模式同一套投递编舞（延迟 → 可用性 → 直发 → 富摘要注入 → 强制
        发言 → 已投递登记与源群留痕），`false` 只记 info 收尾。无票 / 错群 /
        重复消费一律返回明确拒绝且不投递（spec 转发模式）。
        """
        target_group_id = str(kwargs.get("group_id") or "").strip()
        if not target_group_id:
            self.ctx.logger.warning("自动转发决策 Tool 缺少群上下文，已拒绝本次决策")
            return {"success": False, "content": "无法确定当前群聊上下文，已拒绝本次决策。"}
        payload = self._forward_tickets.consume(message_id, target_group_id, now=time.time())
        if payload is None:
            self.ctx.logger.info(
                f"自动转发决策 Tool 拒绝无票调用 target={target_group_id} msg={message_id}"
            )
            return {
                "success": False,
                "content": "该转发决策不存在、已过期或已被处理，无法执行。",
            }
        # 票里登记的是 `(转发源消息快照, 富摘要)`：发送字节取自快照，不经存储重取。
        source, rich_summary = payload
        if not deliver:
            self.ctx.logger.info(
                f"自动转发决策：目标群选择不转发 target={target_group_id} msg={message_id}"
            )
            return {"success": True, "content": "已记录「不转发」，本次不投递。"}
        self.ctx.logger.info(
            f"自动转发决策：目标群选择转发 target={target_group_id} msg={message_id}"
        )
        delivered = await self._run_forward_delivery(source, [target_group_id], rich_summary)
        if delivered:
            return {"success": True, "content": "已按决策把该合并转发投递到本群。"}
        return {
            "success": False,
            "content": "已按决策执行转发，但本次投递未成功，详情见插件日志。",
        }


def create_plugin() -> RepeaterPlugin:
    return RepeaterPlugin()