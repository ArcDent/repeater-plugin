"""QQ 群号与 MaiBot 聊天流的映射。"""

from __future__ import annotations

from typing import Any


class GroupStreamRegistry:
    """按需解析 QQ 群号对应的聊天流，并缓存成功结果。"""

    def __init__(self, context: Any, observed_session: Any = None) -> None:
        """创建绑定到当前插件上下文的空聊天流缓存。

        Args:
            context: MaiBot 注入的 ``PluginContext``。该服务使用其中的
                ``chat`` capability 查询或创建聊天流，并使用 ``logger``
                记录可恢复错误。
            observed_session: 可选的「群号 → 活跃聊天流」回调（本插件的适配
                注入）：Host 的 ``get_stream_by_group_id`` 是群会话列表的
                **首匹配**，同一群存在新旧多个 session 时会返回陈旧会话；
                插件从 hook 上报里学到的活跃 session 可信度更高，优先使用。
                省略时维持只按能力解析的原语义。
        """

        self._ctx = context
        self._observed_session = observed_session
        self._streams_by_group: dict[str, str] = {}
        self._groups_by_stream: dict[str, str] = {}

    async def resolve(self, group_id: str) -> str:
        """按 QQ 群号解析目标聊天流，必要时创建群聊会话。

        解析顺序为：hook 上报过的活跃 session、内存缓存、调用
        ``get_stream_by_group_id``、最后调用 ``open_session``。SDK 会将这两项
        capability 的成功响应解包为聊天流字典，本方法也兼容旧版成功包装结构。
        查询失败允许降级到创建；创建失败会记录日志。该流程只在实际投递时
        运行，不依赖插件启动阶段的全量聊天流快照。

        Args:
            group_id: target 白名单中的 QQ 群号字符串。

        Returns:
            成功时返回目标 ``stream_id``，无法查询或创建时返回空字符串。
        """

        if stream_id := self._observed_stream_id(group_id):
            self._remember(group_id, stream_id)
            return stream_id

        if stream_id := self._streams_by_group.get(group_id):
            return stream_id

        try:
            result = await self._ctx.chat.get_stream_by_group_id(group_id, platform="qq")
        except Exception as exc:
            self._ctx.logger.debug("按群号查询聊天流失败: group=%s error=%s", group_id, exc)
            result = None
        stream_id = self._extract_stream_id(result)
        if stream_id:
            self._remember(group_id, stream_id)
            return stream_id

        try:
            result = await self._ctx.chat.open_session(
                platform="qq",
                chat_type="group",
                group_id=group_id,
            )
        except Exception as exc:
            self._ctx.logger.warning("创建目标群聊天流失败: group=%s error=%s", group_id, exc)
            return ""
        stream_id = self._extract_stream_id(result)
        if stream_id:
            self._remember(group_id, stream_id)
        return stream_id

    async def resolve_group(self, stream_id: str) -> str:
        """从 Host 的真实群聊流列表反查 source 群号。

        该方法不读取模型提供的 ``group_id``。首次查询会通过
        ``chat.get_group_streams`` 获取 QQ 群聊流，并同时刷新正反向缓存；
        capability 失败或目标流不存在时记录警告并返回空字符串，不会尝试
        创建 source 会话。

        Args:
            stream_id: ``after_response`` Hook 绑定并由一次性凭据恢复的真实
                Planner 会话 ID。

        Returns:
            找到对应 QQ 群聊流时返回群号字符串，否则返回空字符串。
        """

        normalized_stream_id = str(stream_id or "").strip()
        if not normalized_stream_id:
            return ""
        if group_id := self._groups_by_stream.get(normalized_stream_id):
            return group_id

        try:
            result = await self._ctx.chat.get_group_streams(platform="qq")
        except Exception as exc:
            self._ctx.logger.warning(
                "读取 source 群聊流失败: stream=%s error=%s",
                normalized_stream_id,
                exc,
            )
            return ""

        streams = self._extract_streams(result)
        for stream in streams:
            candidate_group_id = str(stream.get("group_id") or "").strip()
            if not candidate_group_id:
                continue
            # 观察到的活跃会话优先：全量列表里的「首匹配」可能陈旧，不能覆盖它；
            # 但仍要把这条观察写进正反向缓存，反查（流 → 群）才认得出源群。
            if observed := self._observed_stream_id(candidate_group_id):
                self._remember(candidate_group_id, observed)
                continue
            candidate_stream_id = self._extract_stream_id(stream)
            if not candidate_stream_id:
                continue
            self._remember(candidate_group_id, candidate_stream_id)
        return self._groups_by_stream.get(normalized_stream_id, "")

    def _observed_stream_id(self, group_id: Any) -> str:
        """取 hook 上报学到的活跃 session；没有观察记录或回调异常时返回空串。"""

        if self._observed_session is None:
            return ""
        try:
            return str(self._observed_session(str(group_id or "").strip()) or "").strip()
        except Exception as exc:  # 宽捕获是有意的（BLE001 口径）：回调失败按无观察处理
            self._ctx.logger.debug("读取活跃会话观察失败: group=%s error=%s", group_id, exc)
            return ""

    def _remember(self, group_id: str, stream_id: str) -> None:
        """同时写正反向缓存（群 → 流、流 → 群）。"""

        self._streams_by_group[group_id] = stream_id
        self._groups_by_stream[stream_id] = group_id

    @staticmethod
    def _extract_streams(result: Any) -> list[dict[str, Any]]:
        """规范化群聊流 capability 的当前与旧版成功响应。

        Args:
            result: ``chat.get_group_streams`` 返回值。支持 SDK 解包后的列表，
                以及旧版 ``success/streams`` 包装字典。

        Returns:
            仅包含字典项的独立列表；失败或格式异常时返回空列表。
        """

        if isinstance(result, list):
            return [dict(item) for item in result if isinstance(item, dict)]
        if not isinstance(result, dict) or result.get("success") is False:
            return []
        streams = result.get("streams")
        if not isinstance(streams, list):
            return []
        return [dict(item) for item in streams if isinstance(item, dict)]

    @staticmethod
    def _extract_stream_id(result: Any) -> str:
        """从聊天 capability 返回值中读取聊天流 ID。

        Args:
            result: ``get_stream_by_group_id`` 或 ``open_session`` 的返回值。
                支持 SDK 解包后的聊天流字典、旧版 ``stream`` 包装字典和失败
                响应。

        Returns:
            成功响应包含 ID 时返回去除空白的 ``stream_id`` 或 ``session_id``；
            失败或格式不合法时返回空字符串。
        """

        if not isinstance(result, dict):
            return ""
        if result.get("success") is False:
            return ""
        stream = result.get("stream", result)
        if isinstance(stream, dict):
            return str(stream.get("stream_id") or stream.get("session_id") or "").strip()
        return ""
