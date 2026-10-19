"""复读插件：复读链达标后按出口权重掷骰选中复读、打断或沉默，表情回应按概率跟风或贴反义。

术语见 CONTEXT.md，取舍见 docs/adr/。
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
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin

from .config import RepeaterConfig
from .reactions import (
    OPPOSITE,
    Decision,
    GroupCooldown,
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
# 「群 → 活跃聊天流」观察表上界，防御无限增长。
_OBSERVED_STREAM_IDS_LIMIT = 200
# 兜底 flush 心跳远小于默认聚合窗口，注入最坏延迟 ≈ 窗口 + tick。
_INJECTION_FLUSH_TICK_SECONDS = 5
# 注入日志里 text 的预览截断长度，超限加省略号。
_LOG_TEXT_PREVIEW_CHARS = 60


def _log_preview(text: str) -> str:
    """注入文本的单行预览：超 `_LOG_TEXT_PREVIEW_CHARS` 截断并加省略号。"""
    return text if len(text) <= _LOG_TEXT_PREVIEW_CHARS else f"{text[:_LOG_TEXT_PREVIEW_CHARS]}…"


class RepeaterPlugin(MaiBotPlugin):
    config_model = RepeaterConfig

    _runner: web.AppRunner | None
    _site: web.BaseSite | None
    _state: ReactionState
    _repeat_state: RepeatState
    _reaction_cooldown: GroupCooldown
    _repeat_cooldown: GroupCooldown
    _opposite_map: dict[str, str]
    _detected_self_qq: str
    _tasks: set[asyncio.Task]
    _emoji_action_supported: bool
    _injection_state: InjectionState | None
    _bot_display_name: str | None
    _stream_ids: dict[str, str]
    _flush_loop_task: asyncio.Task | None
    _observed_stream_ids: dict[str, str]

    async def on_load(self) -> None:
        self._runner = None
        self._site = None
        self._tasks = set()
        self._flush_loop_task = None
        self._detected_self_qq = ""
        self._rebuild_runtime()
        if self._injection_state is not None:
            self._bot_display_name = await self._fetch_bot_display_name()
            self._ensure_flush_loop()
            self.ctx.logger.info(
                f"上下文注入已启用，麦麦显示名: {self._bot_display_name or '（无，注入时不写名字）'}"
            )

        if self.config.plugin.enabled:
            await self._start_webhook()
        try:
            await self._startup_checks()
        except Exception as exc:
            self.ctx.logger.warning(f"启动自检失败: {exc}")
        self.ctx.logger.info("复读插件已加载（复读 / 表情回应两域）")

    async def on_unload(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        self._tasks.clear()
        await self._stop_webhook()
        if pending:
            # 等取消落地，卸载返回后不留存活的后台任务。
            await asyncio.gather(*pending, return_exceptions=True)

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        del config_data, version
        # 重建内存运行时，按新配置重启上报监听。
        self._rebuild_runtime()
        self._ensure_flush_loop()
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
        # 两域群冷却各持一个实例、分开计时，随重建清零。
        self._reaction_cooldown = GroupCooldown()
        self._repeat_cooldown = GroupCooldown()
        # 1404 停用标记随重建复位，下一次上报重新判定。
        self._emoji_action_supported = True
        context = self.config.context
        self._stream_ids = {}
        # hook 上报的「群 → 活跃聊天流」观察表，随重建清空。
        self._observed_stream_ids = {}
        # None = 尚未解析；解析一次（即使为空）即停止重复查询，仅重建时复位。
        self._bot_display_name = None
        self._injection_state = (
            InjectionState(
                dedup_ttl_seconds=float(context.dedup_ttl_seconds),
                merge_window_seconds=float(context.merge_window_seconds),
                max_reactions_per_message=int(context.max_reactions_per_message),
            )
            if context.enabled and self.config.plugin.enabled
            else None
        )

    @property
    def _self_qq(self) -> str:
        """麦麦自己的 QQ：配置 `protocol.self_qq` 优先，否则取启动探测值；每次现算，不存派生值。"""
        return str(self.config.protocol.self_qq or "").strip() or self._detected_self_qq

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
                # 群聊事件却解析不出来 = 形态未知，打日志附原始体便于补解析分支。
                self.ctx.logger.warning(
                    f"表情回应上报形态不认识，已忽略: {json.dumps(payload, ensure_ascii=False)[:300]}"
                )
            else:
                # 私聊等无 group_id 的上报，本插件只做群聊，静默忽略。
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
                # 注入失败不拖垮行为路径。
                self.ctx.logger.warning(f"注入表情回应上下文异常: {exc}")
            # 返回时表情槽与群冷却已在 _claim_reaction 同步段占好。
            decision = await self._evaluate(event)
            if decision is None:
                return
            behavior = self.config.behavior
            await self._delay(behavior.delay_min_seconds, behavior.delay_max_seconds)
            await self._send_reaction(event, decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"处理表情回应失败: {exc}")

    async def _evaluate(self, event: ReactionEvent) -> Decision | None:
        """门禁 → 查原作者 → 同步段内复查、掷骰并占槽；返回 None 表示不做任何事。

        查原作者（get_msg）是唯一 await 点；真正的判定在其后的 `_claim_reaction`
        同步段完成，await 前的槽位/冷却预检只为尽早挡掉、省一次查询。
        """
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
        # 麦麦自己贴的表情不触发跟风。
        if self._self_qq and event.reactor_qq == self._self_qq:
            return None

        now = time.time()
        if self._reaction_blocked(event, now):
            return None

        # 回应骰需要知道原作者是谁（self 消息用独立概率），反义表情也不例外。
        is_self_message = await self._is_self_message(event.message_id, now)
        return self._claim_reaction(event, is_self_message)

    def _reaction_blocked(self, event: ReactionEvent, now: float) -> bool:
        """这条消息的表情槽已占，或本群表情回应冷却未过。"""
        if self._state.is_slot_taken(event.message_id, now):
            return True
        return self._reaction_cooldown.is_cooling_down(
            event.group_id, self.config.behavior.cooldown_seconds, now
        )

    def _claim_reaction(self, event: ReactionEvent, is_self_message: bool) -> Decision | None:
        """同步段：复查表情槽与群冷却 → 掷骰 → 掷中即占槽、写冷却。

        刻意是同步方法：查原作者的 await 之后不再有 await 点，同一条消息的并发
        上报只有一条能贴出表情回应（与复读域 record 的同步原子段同构）。
        """
        now = time.time()
        if self._reaction_blocked(event, now):
            return None
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
        if decision is None:
            return None
        self._state.take_slot(event.message_id, now)
        self._reaction_cooldown.mark_group(event.group_id, now)
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
        bot_display_name = self._bot_display_name
        if bot_display_name is None:
            # 每次重建只解析一次，注入路径不再重复查询。
            bot_display_name = self._bot_display_name = await self._fetch_bot_display_name()
        display = build_reactor_display(event.reactor_qq, self._self_qq, bot_display_name)
        if self._injection_state.record(event, display, now) == "skip":
            return
        await self._flush_due_aggregates()

    async def _flush_due_aggregates(self) -> None:
        """到期聚合全部 flush；due() 同步移除聚合，事件路径与兜底循环不会重复投递。"""
        state = self._injection_state
        if state is None:
            return
        now = time.time()
        for aggregate in state.due(now):
            # flush 用聚合自己的群，不得借用触发事件的群。
            await self._flush_aggregate(aggregate.group_id, aggregate)
        state.prune(now)

    def _ensure_flush_loop(self) -> None:
        """注入开启时确保兜底 flush 循环存活；循环每 tick 重读注入状态，重载复用不堆叠。"""
        if self._injection_state is None:
            return
        task = self._flush_loop_task
        if task is not None and not task.done():
            return
        self._flush_loop_task = self._spawn(self._injection_flush_loop())

    async def _injection_flush_loop(self) -> None:
        """兜底 flush 循环：事件驱动 flush 依赖后续上报，这里保证静默期的到期聚合仍按时注入。

        每 tick 重读注入状态，注入关闭时循环自行退出；tick 硬编码不设配置。
        """
        while True:
            await asyncio.sleep(_INJECTION_FLUSH_TICK_SECONDS)
            if self._injection_state is None:
                return
            try:
                await self._flush_due_aggregates()
            except Exception as exc:
                # 单次失败不杀循环，宽捕获有意（BLE001 口径）。
                self.ctx.logger.warning(f"兜底 flush 表情回应聚合失败: {exc}")

    async def _flush_aggregate(
        self, group_id: str, aggregate: InjectionAggregate
    ) -> None:
        state = self._injection_state
        if state is None:
            return
        text = build_reaction_visible_text(aggregate.message_id, aggregate.emoji_id, aggregate.responders)
        written = await self._append_context(
            group_id,
            text,
            "reaction",
            aggregate.message_id,
            log_extra=f"emoji={aggregate.emoji_id}, responders={len(aggregate.responders)}, ",
        )
        if not written:
            # 注入失败时回滚聚合的去重键，同批上报可重新聚合重试。
            state.rollback(aggregate)
            return
        state.mark_injected(aggregate.message_id, time.time())

    async def _get_stream_id(self, group_id: str) -> str:
        """群号 → 聊天流 ID：优先 hook 观察到的活跃聊天流，其次能力查询（可缓存）。

        主程序按路由口径算出的 session_id 比会话列表首匹配可信，后者同群新旧
        session 并存时可能返回陈旧会话；查询失败属瞬时态，不缓存空串。
        """
        observed = self._observed_stream_id(group_id)
        if observed:
            return observed
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
        """机器人显示名来源链：配置兜底 → 主程序 bot.nickname → 空；结果由调用方缓存。"""
        configured = str(self.config.context.self_display_name or "").strip()
        if configured:
            return configured
        try:
            result = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:
            self.ctx.logger.debug(f"取主程序 bot.nickname 失败: {exc}")
            result = ""
        return str(result or "").strip()

    # ---- 防御式拦截（chat.receive.before_process Hook）----

    @HookHandler(
        "chat.receive.before_process",
        name="emoji_reaction_notify_guard",
        mode="blocking",
        order="early",
        description=(
            "防御式拦截表情回应通知消息，避免其进入主程序上下文（ADR 0003）；"
            "并被动观察「群 → 活跃聊天流」映射，供两域注入与查询优先取用 hook 上报的 session_id。"
        ),
    )
    async def guard_reaction_notify(self, **kwargs: Any) -> dict[str, Any]:
        """表情回应通知消息在主链上 abort，其余放行；命中打 info（适配器是否转发通知的线上答案）。

        「群 → 活跃聊天流」观察放在本 handler 最前、不跟随注入开关短路：
        它不属于任何一个域，两域注入都靠它优先拿到活跃 session_id。
        """
        self._observe_group_session(kwargs.get("message"))
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
        """复读检测入口：复读/打断出口命中 → 后台发送并 abort 触发消息，其余 continue 放行。

        blocking 挂在主链上，未捕获异常记 warning 后放行——宁可漏一条拦截，
        不让插件异常干扰主程序；CancelledError 照常上抛。
        """
        try:
            intercepted = self._process_repeat_message(kwargs.get("message"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"复读检测处理消息失败: {exc}")
            return {"action": "continue", "modified_kwargs": kwargs}
        action = "abort" if intercepted else "continue"
        return {"action": action, "modified_kwargs": kwargs}

    def _process_repeat_message(self, message: Any) -> bool:
        """门禁 → 链累积 → 三出口决策；返回触发消息是否被出口消费（拦截）。

        刻意是同步方法：从门禁到写冷却、spawn 结构上无 await 点，同群同文本的
        并发任务不可能在同一链上双触发（终结标记在 record 内先于掷骰写入）。
        """
        repeat = self.config.repeat
        if not self.config.plugin.enabled or not repeat.enabled:
            return False
        if not self._protocol_base_url():
            # 发不出去就不拦截，否则触发消息白白消失。
            return False
        if not isinstance(message, dict) or not isinstance(message.get("message_info"), dict):
            # 形态漂移打 debug 不打 warning：hook 每条群消息都过，warning 会刷屏。
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
        suppressed = self._repeat_cooldown.is_cooling_down(
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
            # 沉默出口：资格已在 record 内消耗，不进群冷却。
            self.ctx.logger.info(
                f"复读链达标后保持沉默 group={candidate.group_id} text={result.text!r}"
            )
            return False
        if decision.used_default_pool:
            # 空池回落内置默认池，回落事实由决策结果携带，只记日志不静默哑火。
            self.ctx.logger.warning("打断文本池为空，已回落内置默认池")
        # 复读/打断出口：先写冷却再转后台发送——发送成败不影响已进入的冷却。
        self._repeat_cooldown.mark_group(candidate.group_id, now)
        self.ctx.logger.info(
            f"复读链达标 group={candidate.group_id} {decision.reason} text={result.text!r}"
        )
        self._spawn(self._send_repeat_decision(candidate, decision))
        return True

    async def _send_repeat_decision(
        self, candidate: RepeatCandidate, decision: RepeatDecision
    ) -> None:
        """复读/打断出口的后台编舞：触发消息注入与延迟并发 → 协议端直发 → 行为文本注入。

        并发让可见延迟取注入 RTT 与延迟的较大值而非串行叠加；直发只需群号、
        不走主程序发送链，冷却与链终结在出口命中时已写入，与本任务成败解耦。
        """
        group_id = candidate.group_id
        repeat = self.config.repeat
        try:
            # 注入与延迟并发；return_exceptions 必需，否则未预期异常会落到外层
            # except、把直发一并丢掉。
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
                    # 异常项显式记 warning 后继续；exc_info 需三元组形式才能带出
                    # traceback（exc 是返回的异常实例，logger.exception 在此不可用）。
                    self.ctx.logger.warning(
                        f"复读/打断后台并发协程异常: {exc}",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            response = await self._call_protocol(
                "send_group_msg",
                {
                    # 纯数字转 int（协议端惯例，与 message_id 一致）。
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
        # 4. 行为文本注入：让麦麦知道自己发出了什么（仅直发成功）。
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
        *,
        log_extra: str = "",
    ) -> bool:
        """两域注入唯一的写入入口：取 stream → `maisaka.context.append` → 判回执。

        成功记 info「已写入」、失败记 warning「未写入」，同一字段序
        （kind → text 预览 → 定位 → log_extra → 回执或原因）。不重试：复读注入
        是即时一次性补偿，表情回应聚合由调用方按返回值回滚去重键再重试。
        """
        where = (
            f"kind={source_kind} text='{_log_preview(text)}', "
            f"group={group_id}, msg={message_id}, {log_extra}"
        )
        stream_id = await self._get_stream_id(group_id)
        if not stream_id:
            self.ctx.logger.warning(f"未写入 Maisaka 上下文: {where}原因=取不到 stream")
            return False
        try:
            response = await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "data": text}],
                visible_text=text,
                source_kind=source_kind,
                message_id=message_id,
            )
        except Exception as exc:
            # 宽捕获是有意的：注入失败不得拖垮调用方的后续步骤（BLE001 口径）。
            self.ctx.logger.warning(
                f"未写入 Maisaka 上下文: {where}原因=调用异常 {exc}, stream={stream_id}"
            )
            return False
        if not (isinstance(response, dict) and response.get("success")):
            self.ctx.logger.warning(
                f"未写入 Maisaka 上下文: {where}原因=回执失败 {response}, stream={stream_id}"
            )
            return False
        self.ctx.logger.info(
            f"已写入 Maisaka 上下文: {where}index={response.get('index')}, stream={stream_id}"
        )
        return True

    def _observe_group_session(self, message: Any) -> None:
        """从 hook 载荷里记下「群号 → 主程序路由算出的 session_id」（纯内存）。

        主程序在派发 `chat.receive.before_process` 之前就已按路由口径
        （platform + group_id + account_id + scope）算好 `session_id`，消息入库
        与 Planner 读写都用它——因此这是该群**活跃聊天流**的权威来源。Host 的
        `chat.get_stream_by_group_id` 是会话列表**首匹配**，同群存在新旧多个
        session 时可能返回陈旧会话（2026-09-27 线上实测：注入落在死会话，
        麦麦回复发送失败）。

        观察表有上界：超出 `_OBSERVED_STREAM_IDS_LIMIT` 时按插入顺序淘汰最旧项；
        群数量级远小于该上界，淘汰只为防御异常输入导致的无限增长。
        """
        if not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return
        group_info = message_info.get("group_info")
        if not isinstance(group_info, dict):
            return
        group_id = str(group_info.get("group_id") or "").strip()
        if not group_id:
            return
        self._observed_stream_ids[group_id] = session_id
        while len(self._observed_stream_ids) > _OBSERVED_STREAM_IDS_LIMIT:
            self._observed_stream_ids.pop(next(iter(self._observed_stream_ids)))

    def _observed_stream_id(self, group_id: Any) -> str:
        """取 hook 上报学到的活跃 session；没有观察记录时返回空串。"""

        return self._observed_stream_ids.get(str(group_id or "").strip(), "")

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
        """按 (low, high) 区间随机延迟；high < low 按 low、high <= 0 不等待。"""
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

    def _protocol_base_url(self) -> str:
        """协议端 OneBot HTTP 地址（去空白与末尾斜杠）；直调、自检与复读门禁共用。"""
        return (self.config.protocol.base_url or "").strip().rstrip("/")

    async def _call_protocol(self, action_name: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """直连协议端 OneBot HTTP（路径风格 + Bearer）。见 docs/adr/0001。

        不可达返回 None；协议端失败时可能 HTTP 200 + status=failed，故一律返回响应体交由调用方判定。
        """
        base_url = self._protocol_base_url()
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
        """探活协议端并探测麦麦自己的 QQ 号；失败只告警不阻断加载。

        不探测 set_msg_emoji_like：无副作用探测方式，首次贴表情时按 retcode 判定。
        """
        if not self._protocol_base_url():
            self.ctx.logger.warning(
                "protocol.base_url 未配置：复读已停用（不拦截触发消息），贴表情无法执行"
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

        self._detected_self_qq = parse_self_qq(response)
        if self._self_qq:
            self.ctx.logger.info(f"麦麦 QQ 号: {self._self_qq}")
        else:
            self.ctx.logger.warning(
                "取不到麦麦自己的 QQ 号，「有人给麦麦自己消息贴表情」将按他人消息的概率处理；"
                "可在配置里手填 protocol.self_qq"
            )


def create_plugin() -> RepeaterPlugin:
    return RepeaterPlugin()
