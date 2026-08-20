"""表情跟风插件。

群里有人给某条消息贴表情回应后，麦麦按概率跟着贴同款；触发表情命中反义映射时，
按另一套概率贴语义相反的表情。术语见 CONTEXT.md，通路与取舍见 docs/adr/。
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
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin

from .config import SendingEmojisConfig
from .reactions import (
    OPPOSITE,
    Decision,
    ReactionEvent,
    ReactionState,
    build_opposite_map,
    coerce_message_id,
    decide_reaction,
    group_allowed,
    is_missing_action,
    is_reaction_notice,
    parse_message_author,
    parse_reaction_report,
    parse_self_qq,
    protocol_call_failed,
)

_PROTOCOL_TIMEOUT_SECONDS = 10


class SendingEmojisPlugin(MaiBotPlugin):
    config_model = SendingEmojisConfig

    _runner: web.AppRunner | None
    _site: web.BaseSite | None
    _state: ReactionState
    _opposite_map: dict[str, str]
    _self_qq: str
    _tasks: set[asyncio.Task]
    _emoji_action_supported: bool

    async def on_load(self) -> None:
        self._runner = None
        self._site = None
        self._tasks = set()
        self._self_qq = ""
        self._emoji_action_supported = True
        self._rebuild_runtime()

        if self.config.plugin.enabled:
            await self._start_webhook()
        try:
            await self._startup_checks()
        except Exception as exc:
            self.ctx.logger.warning(f"启动自检失败: {exc}")
        self.ctx.logger.info("表情跟风插件已加载")

    async def on_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await self._stop_webhook()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        del config_data, version
        self._rebuild_runtime()
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
        configured_self_qq = str(self.config.protocol.self_qq or "").strip()
        if configured_self_qq:
            self._self_qq = configured_self_qq

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

    def _spawn(self, coro: Any) -> None:
        """后台执行并持有引用，避免任务被 GC；卸载时统一取消。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- 决策与执行 ----

    async def _on_reaction(self, event: ReactionEvent) -> None:
        try:
            decision = await self._evaluate(event)
            if decision is None:
                return
            now = time.time()
            # 先占表情槽再发，避免同一条消息的并发上报重复贴。
            self._state.take_slot(event.message_id, now)
            self._state.mark_group(event.group_id, now)
            await self._delay()
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
        ignored = {str(qq).strip() for qq in (scope.ignore_reactors or []) if str(qq).strip()}
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

        # 命中反义映射的表情走反义分支，不需要知道原作者是谁。
        is_self_message = False
        if event.emoji_id not in self._opposite_map:
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

    async def _delay(self) -> None:
        behavior = self.config.behavior
        low = max(float(behavior.delay_min_seconds), 0.0)
        high = max(float(behavior.delay_max_seconds), low)
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
            detail = str(response.get("wording") or response.get("message") or response.get("retcode"))
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
        if not (self.config.protocol.base_url or "").strip():
            self.ctx.logger.warning("protocol.base_url 未配置，插件不会贴任何表情")
            return

        response = await self._call_protocol("get_login_info", {})
        if response is None:
            self.ctx.logger.warning("协议端不可达，请检查 protocol.base_url 与网络")
            return
        if protocol_call_failed(response):
            detail = str(response.get("wording") or response.get("message") or response.get("retcode"))
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


def create_plugin() -> SendingEmojisPlugin:
    return SendingEmojisPlugin()