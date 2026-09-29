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
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin

from .config import RepeaterConfig
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
# 「群 → 活跃聊天流」观察表的上界：群数量级远小于它，只为防御无限增长。
_OBSERVED_STREAM_IDS_LIMIT = 200
# Resident flush-loop tick (design.md D2): hardcoded on purpose — far below
# the default 30s merge window, so worst-case injection lag is window + tick.
_INJECTION_FLUSH_TICK_SECONDS = 5
# 注入文本的日志预览长度：Maisaka 上下文写入日志统一「kind → text → 定位 →
# 回执」字段序（对齐 smart_poke_plugin 的 `text='…', index=…, stream=…` 风格），
# 超限截断带省略号，免得长文本把单行日志撑爆。
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
    _opposite_map: dict[str, str]
    _self_qq: str
    _tasks: set[asyncio.Task]
    _emoji_action_supported: bool
    _injection_state: InjectionState | None
    _bot_display_name: str
    _bot_display_name_resolved: bool
    _stream_ids: dict[str, str]
    _flush_loop_task: asyncio.Task | None
    _observed_stream_ids: dict[str, str]

    async def on_load(self) -> None:
        self._runner = None
        self._site = None
        self._tasks = set()
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
            # 等取消真正落地：卸载返回后不得再有存活的后台任务。
            await asyncio.gather(*pending, return_exceptions=True)

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        del config_data, version
        # 重建内存运行时（反义映射与各状态容器），随后按新配置重启上报监听。
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
        configured_self_qq = str(self.config.protocol.self_qq or "").strip()
        if configured_self_qq:
            self._self_qq = configured_self_qq
        context = self.config.context
        self._stream_ids = {}
        # **群 → 活跃聊天流**的被动观察表：hook 每条消息都带主程序按路由口径算好
        # 的 `session_id`，比 `get_stream_by_group_id` 的「列表首匹配」可信得多
        # （同一群在 chat_sessions 里可能有新旧多个 session，首匹配可能是陈旧会话）。
        # 与其他状态容器同生命周期：随重建清空，下一次群消息到达即重新学到。
        self._observed_stream_ids = {}
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
            f"已写入 Maisaka 上下文: kind=reaction text='{_log_preview(text)}', "
            f"group={group_id}, msg={aggregate.message_id}, "
            f"emoji={aggregate.emoji_id}, responders={len(aggregate.responders)}, "
            f"index={response.get('index')}, stream={stream_id}"
        )

    async def _get_stream_id(self, group_id: str) -> str:
        """群号 → 聊天流 ID；优先 hook 观察到的**活跃聊天流**，其次能力查询（design.md D6）。

        主程序按路由口径算出的 `session_id`（hook 每条消息都带）比
        `get_stream_by_group_id` 的「会话列表首匹配」可信：同一群在
        `chat_sessions` 里可能有新旧多个 session，首匹配可能是陈旧会话，注入会
        落在没人读的死会话里（2026-09-27 线上实测：麦麦回复「未找到目标
        平台」发送失败）。

        The main program looks sessions up in memory and creates them lazily,
        so a failed lookup is transient: caching the empty string would pin
        the group as "no stream" forever. Success values are deterministic
        (session_id is stable), so they are cached as before.
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
        description=(
            "防御式拦截表情回应通知消息，避免其进入主程序上下文（ADR 0003）；"
            "并被动观察「群 → 活跃聊天流」映射，供两域注入与查询优先取用 hook 上报的 session_id。"
        ),
    )
    async def guard_reaction_notify(self, **kwargs: Any) -> dict[str, Any]:
        """表情回应通知消息在主链上 abort；其余放行。

        命中时打 info 日志——这条日志就是「适配器是否把表情回应当通知转发」的
        线上答案（ADR 0001/0003）。上下文注入关闭时不拦截。

        **群 → 活跃聊天流**的被动观察（纯内存、有上界）放在本 handler 最前，
        且在注入开关判断之前：它不是任何一个域的行为，复读与表情回应的注入
        都靠 `_get_stream_id` 优先拿到 hook 上报的活跃 `session_id`（Host 的
        `get_stream_by_group_id` 是会话列表首匹配，可能是陈旧会话），因此不
        跟随 `_injection_state` 或复读开关短路。
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
    ) -> None:
        """把一条域内上下文消息写入麦麦聊天历史（change design D3）。

        注入是 planner 拦截 / 协议端直发的即时一次性补偿，无幂等键需求，不复用
        表情回应域的 InjectionState（去重/聚合/回滚是外部事件流聚合的需求）。
        失败（stream 查不到 / append 异常或失败）记 warning 即返：不重试、不回滚，
        调用方照常走后续步骤——注入与直发成败解耦。成功日志统一「kind → text
        预览 → group/msg 定位 → index/stream 回执」字段序，与表情回应域注入
        同一格式。
        """
        stream_id = await self._get_stream_id(group_id)
        if not stream_id:
            self.ctx.logger.warning(
                f"取不到群 {group_id} 的 stream，跳过注入 source_kind={source_kind}"
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
                f"注入上下文异常 group={group_id} source_kind={source_kind}: {exc}"
            )
            return
        if not (isinstance(response, dict) and response.get("success")):
            self.ctx.logger.warning(
                f"注入上下文失败 group={group_id} source_kind={source_kind}: {response}"
            )
            return
        self.ctx.logger.info(
            f"已写入 Maisaka 上下文: kind={source_kind} "
            f"text='{_log_preview(text)}', group={group_id}, msg={message_id}, "
            f"index={response.get('index')}, stream={stream_id}"
        )

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
        真实支持情况在首次贴表情时按 retcode 判定。
        """
        base_url = (self.config.protocol.base_url or "").strip()
        if not base_url:
            self.ctx.logger.warning("protocol.base_url 未配置，插件不会贴任何表情")
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


def create_plugin() -> RepeaterPlugin:
    return RepeaterPlugin()
