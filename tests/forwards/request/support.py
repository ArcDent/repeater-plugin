"""请求转发链路单测的替身与运行时装配。

本模块自原插件 `tests/support/__init__.py` 移植：capability 替身与
`build_forward_message` 保持原样，`build_plugin` 改为直接装配
`ForwardingRuntime`（本插件中请求转发的公共入口），并返回 `RequestHarness`。

`RequestHarness` 是 `plugin.py`（第 4 组接线）之前的 Hook 契约替身：它只把
运行时公共方法包装成原插件测试里 `plugin.<hook>` 的调用形态，不复制任何业务
判断，因此迁移后的用例只需修正 import 路径。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time
from types import SimpleNamespace
from typing import Any

from repeater_plugin.forwards.request import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_TOOL_NAME,
    ForwardingRuntime,
    append_user_reminder,
    build_delivered_key,
)
from repeater_plugin.forwards.request.source.history import ViewToolObservation
from repeater_plugin.forwards.request.source.models import ViewObservationKind
from repeater_plugin.forwards.state import (
    DeliveredRegistry,
    GroupAvailabilityCache,
)

from config import ForwardSection, RepeaterConfig

# 调试统计只把版本当分桶名；测试用固定值，避免依赖 manifest 内容。
TEST_PLUGIN_VERSION = "0.0.0-test"


def build_forward_message(
    *,
    stream_id: str = "source-stream",
    group_id: str = "10001",
    sender_id: str = "42",
    self_id: str = "99999",
) -> dict[str, Any]:
    """构造包含文字、图片和二进制数据的 Host 合并转发消息。

    Args:
        stream_id: 消息所属的 MaiBot 聊天流 ID。
        group_id: 消息所属的 QQ 群号。
        sender_id: 入站消息发送者 QQ 号。
        self_id: 适配器写入路由元数据的机器人 QQ 号。

    Returns:
        可供 ``FakeMessageCapability`` 返回的消息字典。固定消息 ID 为
        ``forward-message``，并包含一个昵称为“群友甲”的转发节点。
    """

    return {
        "message_id": "forward-message",
        "session_id": stream_id,
        "group_id": group_id,
        "platform": "qq",
        "message_info": {
            "user_info": {
                "user_id": sender_id,
                "user_nickname": "群友甲",
                "user_cardname": "",
            },
            "group_info": {
                "group_id": group_id,
                "group_name": "测试群",
            },
            "additional_config": {"self_id": self_id},
        },
        "processed_plain_text": "合并转发预览",
        "raw_message": [
            {
                "type": "forward",
                "data": [
                    {
                        "user_id": "42",
                        "user_nickname": "群友甲",
                        "user_cardname": "",
                        "message_id": "node-1",
                        "content": [
                            {"type": "text", "data": "有趣的消息"},
                            {
                                "type": "image",
                                "data": "图片描述",
                                "hash": "image-hash",
                                "binary_data_base64": "aW1hZ2U=",
                            },
                        ],
                    }
                ],
            }
        ],
    }


class FakeMessageCapability:
    def __init__(self, message: dict[str, Any]) -> None:
        """创建始终返回指定消息的查询能力替身。

        Args:
            message: 每次 ``get_by_id`` 返回的消息字典，模拟当前 SDK 对
                Host 成功响应的自动解包。
        """

        self.message = message
        self.calls: list[tuple[str, str, bool]] = []

    async def get_by_id(
        self,
        message_id: str,
        *,
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """记录消息查询参数并返回 SDK 解包后的预设消息。

        Args:
            message_id: 被查询的消息 ID。
            stream_id: 调用方用于限制消息归属的聊天流 ID。
            include_binary_data: 是否请求媒体二进制字段。
            **kwargs: 本测试替身忽略的其他 capability 参数。

        Returns:
            当前 SDK 解包后的 ``self.message`` 消息字典。
        """

        del kwargs
        self.calls.append((message_id, stream_id, include_binary_data))
        return self.message


class FakeStaticMessageCapability:
    def __init__(self, result: Any) -> None:
        """创建返回任意固定结果的消息能力替身。

        Args:
            result: ``get_by_id`` 应原样返回的结果，用于覆盖旧版包装、失败
                包装、空结果和非法格式。
        """

        self.result = result

    async def get_by_id(
        self,
        message_id: str,
        *,
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> Any:
        """返回预设结果并校验请求保留消息范围和二进制数据参数。

        Args:
            message_id: 被查询的消息 ID，测试要求不能为空。
            stream_id: source 聊天流 ID，测试要求为 ``source-stream``。
            include_binary_data: 是否请求媒体二进制字段，测试要求为 ``True``。
            **kwargs: 本测试替身忽略的其他 capability 参数。

        Returns:
            构造实例时传入的固定结果。
        """

        del kwargs
        assert message_id
        assert stream_id == "source-stream"
        assert include_binary_data is True
        return self.result


class FakeDelayedMessageCapability(FakeMessageCapability):
    """在固定查询次数后才返回真实消息的能力替身。"""

    def __init__(self, message: dict[str, Any], unavailable_attempts: int = 1) -> None:
        """创建模拟 Hook 早于消息落库时序的查询能力。

        Args:
            message: 等待次数耗尽后返回的真实消息字典。
            unavailable_attempts: 前多少次查询返回 ``None``，默认一次。
        """

        super().__init__(message)
        self.unavailable_attempts = unavailable_attempts

    async def get_by_id(
        self,
        message_id: str,
        *,
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """记录查询，并在模拟落库完成后返回预设消息。

        Args:
            message_id: 被查询的 source 消息 ID。
            stream_id: 限制消息归属的 source 聊天流 ID。
            include_binary_data: 是否请求媒体二进制字段。
            **kwargs: 本测试替身忽略的其他 capability 参数。

        Returns:
            前 ``unavailable_attempts`` 次返回 ``None``，之后返回预设消息。
        """

        del kwargs
        self.calls.append((message_id, stream_id, include_binary_data))
        if len(self.calls) <= self.unavailable_attempts:
            return None
        return self.message


class FakeChatCapability:
    def __init__(self, streams: list[dict[str, Any]]) -> None:
        """创建使用可变聊天流列表的能力替身。

        Args:
            streams: 初始 QQ 群聊流列表。``open_session`` 会向该列表追加
                新建会话，便于后续查询复用。
        """

        self.streams = streams

    async def get_group_streams(self, platform: str = "qq") -> list[dict[str, Any]]:
        """返回当前平台下全部预设 QQ 群聊流。

        Args:
            platform: 待查询平台；测试要求为 ``"qq"``。

        Returns:
            群聊流字典的独立列表，模拟 SDK 对 ``streams`` 的自动解包。
        """

        assert platform == "qq"
        return [dict(stream) for stream in self.streams if stream.get("platform") == platform]

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> dict[str, Any] | None:
        """按群号查找第一条匹配的预设聊天流。

        Args:
            group_id: 要查找的 QQ 群号。
            platform: 待查询平台；测试要求为 ``"qq"``。

        Returns:
            找到时返回原始聊天流字典，未找到时返回 ``None``，模拟 SDK 对成功
            响应的解包结果。
        """

        assert platform == "qq"
        stream = next((item for item in self.streams if item["group_id"] == group_id), None)
        return stream

    async def open_session(
        self,
        platform: str,
        chat_type: str,
        *,
        group_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """模拟为此前未知的 QQ 群创建聊天流。

        Args:
            platform: 会话平台，必须为 ``"qq"``。
            chat_type: 会话类型，必须为 ``"group"``。
            group_id: 要创建聊天流的 QQ 群号。
            **kwargs: 本测试替身忽略的其他建流参数。

        Returns:
            新聊天流字典。新 ID 使用 ``opened-<group_id>``，并同步追加到
            ``streams``，模拟 SDK 对成功响应的解包结果。
        """

        del kwargs
        assert platform == "qq"
        assert chat_type == "group"
        stream = {
            "platform": "qq",
            "group_id": group_id,
            "stream_id": f"opened-{group_id}",
            "session_id": f"opened-{group_id}",
        }
        self.streams.append(stream)
        return stream


class FakeSendCapability:
    def __init__(self, events: list[tuple[str, str]], failed_streams: set[str] | None = None) -> None:
        """创建记录发送顺序并可定向失败的发送替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
            failed_streams: 应返回模拟发送失败的目标聊天流集合；省略时全部
                发送成功。
        """

        self.events = events
        self.failed_streams = failed_streams or set()
        self.messages_by_stream: dict[str, list[dict[str, Any]]] = {}
        self.message_ids_by_stream: dict[str, str] = {}

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """记录一次合并转发调用并按配置返回成功或失败。

        Args:
            messages: 传给 ``ctx.send.forward`` 的规范化转发节点。
            stream_id: 接收消息的目标聊天流 ID。
            **kwargs: 发送选项；测试会断言历史同步和详细结果均已启用。

        Returns:
            目标位于 ``failed_streams`` 时返回不含消息 ID 的详细失败结果；
            否则返回带平台最终目标消息 ID 的详细成功结果。
        """

        assert kwargs["sync_to_maisaka_history"] is True
        assert kwargs["return_details"] is True
        self.events.append(("send", stream_id))
        self.messages_by_stream[stream_id] = messages
        if stream_id in self.failed_streams:
            return {"sent": False, "message_id": None, "error": "模拟发送失败"}
        message_id = f"sent-{stream_id}"
        self.message_ids_by_stream[stream_id] = message_id
        return {"sent": True, "message_id": message_id}


class BlockingSendCapability(FakeSendCapability):
    """在测试释放闸门前阻塞首次物理发送。"""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        """创建带开始通知和释放闸门的发送替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
        """

        super().__init__(events)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """等待测试放行后执行父类发送逻辑。

        Args:
            messages: 传给 ``ctx.send.forward`` 的规范化转发节点。
            stream_id: 接收消息的目标聊天流 ID。
            **kwargs: 透传给父类替身的发送选项。

        Returns:
            闸门释放后父类生成的成功结果字典。
        """

        self.started.set()
        await self.release.wait()
        return await super().forward(messages, stream_id, **kwargs)


class FakeMaisakaProactiveCapability:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        """创建记录目标 Planner 触发顺序的能力替身。

        Args:
            events: 所有 Fake capability 共享的事件列表。
        """

        self.events = events
        self.intents_by_stream: dict[str, str] = {}
        self.metadata_by_stream: dict[str, dict[str, Any]] = {}

    async def trigger(self, stream_id: str, intent: str, **kwargs: Any) -> dict[str, Any]:
        """记录主动任务参数并模拟 Planner 成功入队。

        Args:
            stream_id: 要触发 Planner 的目标聊天流 ID。
            intent: 投递服务生成的 Planner 意图文本，仅记录而不校验文案。
            **kwargs: 主动任务的 reason、priority 和 metadata；测试保存
                metadata 供调用方验证功能字段。

        Returns:
            包含 ``success=True``、``queued=True`` 和可预测任务 ID 的字典。
        """

        metadata = kwargs.get("metadata")
        assert isinstance(metadata, dict)
        assert isinstance(intent, str)
        self.intents_by_stream[stream_id] = intent
        self.metadata_by_stream[stream_id] = metadata
        self.events.append(("planner", stream_id))
        return {"success": True, "queued": True, "task_id": f"task-{stream_id}"}


class FakeContext:
    def __init__(
        self,
        *,
        data_dir: Path,
        message: dict[str, Any],
        streams: list[dict[str, Any]],
        failed_streams: set[str] | None = None,
    ) -> None:
        """组装插件测试所需的最小 ``PluginContext`` 替身。

        Args:
            data_dir: 状态文件和运行时目录使用的 pytest 临时路径。
            message: 消息查询能力应返回的 source 消息。
            streams: 初始 QQ 群聊流列表。
            failed_streams: 应模拟物理发送失败的聊天流集合。

        Note:
            ``events`` 在发送和 Planner 能力间共享，用于断言跨 target 的
            全局调用顺序。
        """

        self.logger = logging.getLogger("test.forward-plugin")
        self.paths = SimpleNamespace(data_dir=data_dir, runtime_dir=data_dir / "runtime")
        self.events: list[tuple[str, str]] = []
        self.message = FakeMessageCapability(message)
        self.chat = FakeChatCapability(streams)
        self.send = FakeSendCapability(self.events, failed_streams)
        self.maisaka = SimpleNamespace(
            proactive=FakeMaisakaProactiveCapability(self.events),
        )


class RequestHarness:
    """`ForwardingRuntime` 的 Hook 契约替身（第 4 组接线前的测试入口）。"""

    def __init__(self, context: FakeContext, runtime: ForwardingRuntime) -> None:
        """保存测试上下文与运行时。

        Args:
            context: capability 替身集合。
            runtime: 已装配全部服务的请求转发运行时。
        """

        self.ctx = context
        self.runtime = runtime

    async def on_load(self) -> None:
        """启动运行时（等价原插件 ``on_load``）。"""

        await self.runtime.start()

    async def on_unload(self) -> None:
        """停止运行时并保存状态（等价原插件 ``on_unload``）。"""

        await self.runtime.stop()

    async def on_config_update(
        self,
        *,
        scope: str = "",
        config_data: Any = None,
        version: str = "",
    ) -> None:
        """模拟配置热更新，取消未发送的路径 B 续接请求。

        Args:
            scope: 热更新范围，仅用于保持原调用形态。
            config_data: 新配置字典；本替身不消费（配置由 provider 提供）。
            version: 热更新版本标识，仅用于保持原调用形态。
        """

        del scope, config_data, version
        self.runtime.cancel_pending_direct_forwards()

    async def capture_view_forward_result(self, **kwargs: Any) -> dict[str, Any]:
        """等价原插件 ``before_request`` Hook：同步资格并按需追加提醒。

        Args:
            **kwargs: Hook 参数字典；读取 ``session_id`` 与
                ``items`` / ``messages`` 历史，其余字段原样保留。

        Returns:
            ``{"action": "continue", "modified_kwargs": ...}``，与 Host Hook
            返回值同形。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            history_items = kwargs.get("items") if "items" in kwargs else kwargs.get("messages")
            self.runtime.capture_view_results(session_id, history_items)
            reminder = self.runtime.build_view_judgment_reminder(session_id)
            if reminder:
                if "items" in kwargs and isinstance(kwargs.get("items"), list):
                    kwargs["items"] = append_user_reminder(kwargs["items"], reminder)
                elif isinstance(kwargs.get("messages"), list):
                    kwargs["messages"].append({"role": "user", "content": reminder})
        return {"action": "continue", "modified_kwargs": kwargs}

    async def authorize_forward_request_context(self, **kwargs: Any) -> dict[str, Any]:
        """等价原插件 EARLY ``after_response`` Hook：清洗并签发一次性凭据。

        Args:
            **kwargs: Hook 参数字典；读取 ``session_id`` 与
                ``output_items`` 或 ``tool_calls``。

        Returns:
            ``modified_kwargs`` 中包含清洗后的载荷与本轮随机标记。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        if "output_items" in kwargs:
            output_items, authorization_round = self.runtime.authorize_forward_output_items(
                session_id,
                kwargs.get("output_items"),
            )
            kwargs["output_items"] = output_items
        else:
            tool_calls, authorization_round = self.runtime.authorize_forward_calls(
                session_id,
                kwargs.get("tool_calls"),
            )
            kwargs["tool_calls"] = tool_calls
        kwargs[FORWARD_AUTHORIZATION_ROUND_KWARG] = authorization_round
        return {"action": "continue", "modified_kwargs": kwargs}

    async def orchestrate_view_before_forward(self, **kwargs: Any) -> dict[str, Any]:
        """等价原插件 LATE ``after_response`` Hook：编排自动查看续接。

        Args:
            **kwargs: EARLY Hook 的 ``modified_kwargs``，含本轮随机标记。

        Returns:
            ``modified_kwargs`` 中包含路径 B 改写后的载荷。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        authorization_round = str(kwargs.pop(FORWARD_AUTHORIZATION_ROUND_KWARG, "") or "").strip()
        if session_id:
            if "output_items" in kwargs:
                kwargs["output_items"] = await self.runtime.transform_after_output_items(
                    session_id,
                    kwargs.get("output_items"),
                    authorization_round,
                )
            else:
                response, tool_calls = await self.runtime.transform_after_response(
                    session_id,
                    kwargs.get("response"),
                    kwargs.get("tool_calls"),
                    authorization_round,
                )
                kwargs["response"] = response
                kwargs["tool_calls"] = tool_calls
        return {"action": "continue", "modified_kwargs": kwargs}

    async def request_cross_group_forward(
        self,
        msg_id: str,
        sharing_reason: str = "",
        content_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """等价原插件 Tool handler：委托运行时请求服务。

        Args:
            msg_id: 要分享的源合并转发消息 ID。
            sharing_reason: Planner 给出的分享理由。
            content_summary: 降级摘要。
            **kwargs: Runner 传入的完整调用参数（含一次性凭据与 Host 上下文字段）。

        Returns:
            Planner 可读的 Tool 结果字典。
        """

        return await self.runtime.request_forward(
            msg_id,
            sharing_reason,
            content_summary,
            kwargs,
        )

    async def trigger_source_planner_for_forward_message(
        self,
        *,
        message: Any = None,
        **kwargs: Any,
    ) -> None:
        """等价原插件 ``after_process`` Hook：筛选并安排 source Planner 触发。

        Args:
            message: 入站消息载荷。
            **kwargs: Hook 未来可能增加的其他参数，本替身不读取。
        """

        del kwargs
        self.runtime.observe_source_message(message)


def build_config(
    *,
    source_groups: list[str] | None = None,
    target_groups: list[str] | None = None,
    view_failure_fallback_threshold: int = 2,
    trigger_source_planner: bool = True,
    trigger_target_planner: bool = True,
    forward_enabled: bool = True,
    plugin_enabled: bool = True,
) -> RepeaterConfig:
    """构造带指定转发节的插件配置模型。

    Args:
        source_groups: source 白名单；省略时使用默认单源群。
        target_groups: target 白名单；省略时使用默认两个目标群。
        view_failure_fallback_threshold: 查看失败降级阈值。
        trigger_source_planner: 是否安排 source Planner 触发。
        trigger_target_planner: 投递成功后是否触发目标群 Planner。
        forward_enabled: 转发域独立开关。
        plugin_enabled: 插件总开关。

    Returns:
        已注入转发节的本插件根配置模型；运行时通过 `adapt_forward_config`
        读取其 `forward` 节与 `plugin.enabled`。
    """

    return RepeaterConfig(
        plugin={"enabled": plugin_enabled},
        forward=ForwardSection(
            enabled=forward_enabled,
            source_groups=["10001"] if source_groups is None else list(source_groups),
            target_groups=["20001", "20002"] if target_groups is None else list(target_groups),
            view_failure_fallback_threshold=view_failure_fallback_threshold,
            trigger_source_planner=trigger_source_planner,
            trigger_target_planner=trigger_target_planner,
        ),
    )


def build_plugin(
    tmp_path: Path,
    *,
    failed_streams: set[str] | None = None,
    source_groups: list[str] | None = None,
    target_groups: list[str] | None = None,
    view_failure_fallback_threshold: int = 2,
    trigger_source_planner: bool = True,
    trigger_target_planner: bool = True,
    forward_enabled: bool = True,
    plugin_enabled: bool = True,
    delivered_registry: DeliveredRegistry | None = None,
    availability_cache: GroupAvailabilityCache | None = None,
    group_list_refresher: Callable[[], Awaitable[None]] | None = None,
) -> RequestHarness:
    """构造启用状态下、带一个 source 和两个 target 的请求转发运行时。

    Args:
        tmp_path: pytest 提供的临时目录，用于隔离持久化状态文件。
        failed_streams: 应由发送能力模拟失败的目标聊天流集合。
        source_groups: 可选的 source 群白名单；省略时使用 ``["10001"]``。
        target_groups: 可选的 target 群号配置；省略时使用两个默认目标群。
        view_failure_fallback_threshold: 允许摘要或预览降级前，同一消息需要
            连续累计的可重试查看故障次数。
        trigger_source_planner: source 群收到合并转发时是否强制触发本群
            Planner，默认开启；传入 ``False`` 可覆盖默认值以测试关闭路径。
        trigger_target_planner: 发送成功后是否触发目标群 Planner。
        forward_enabled: 转发域独立开关。
        plugin_enabled: 插件总开关。
        delivered_registry: 与自动路径共用的**已投递状态**；省略时新建。
        availability_cache: 与自动路径共用的**群可用性缓存**；省略时新建。
        group_list_refresher: 请求路径投递前刷新的**群可用性缓存**回调
            （本插件的适配注入）；省略时维持只读缓存语义。

    Returns:
        已装配 ``FakeContext`` 与运行时、但尚未调用 ``on_load`` 的测试入口。
    """

    config = build_config(
        source_groups=source_groups,
        target_groups=target_groups,
        view_failure_fallback_threshold=view_failure_fallback_threshold,
        trigger_source_planner=trigger_source_planner,
        trigger_target_planner=trigger_target_planner,
        forward_enabled=forward_enabled,
        plugin_enabled=plugin_enabled,
    )
    streams = [
        {"platform": "qq", "group_id": "10001", "stream_id": "source-stream"},
        {"platform": "qq", "group_id": "20001", "stream_id": "target-a"},
        {"platform": "qq", "group_id": "20002", "stream_id": "target-b"},
    ]
    context = FakeContext(
        data_dir=tmp_path,
        message=build_forward_message(),
        streams=streams,
        failed_streams=failed_streams,
    )
    runtime = ForwardingRuntime(
        context,
        lambda: config,
        delivered_registry=delivered_registry if delivered_registry is not None else DeliveredRegistry(),
        availability_cache=availability_cache if availability_cache is not None else GroupAvailabilityCache(),
        group_list_refresher=group_list_refresher,
        plugin_version=TEST_PLUGIN_VERSION,
    )
    return RequestHarness(context, runtime)


async def invoke_forward_tool(
    plugin: RequestHarness,
    msg_id: str,
    sharing_reason: str = "",
    content_summary: str = "",
    *,
    session_id: str = "source-stream",
    **host_context: Any,
) -> dict[str, Any]:
    """按真实 Host 顺序授权并调用自主转发 Tool。

    测试先执行无 I/O 的 EARLY ``after_response`` Hook，取得绑定
    ``session_id + msg_id`` 的一次性凭据，再模拟 Host 注入执行上下文并调用
    Tool handler。该辅助函数不会执行路径 B 的自动查看 Hook，便于请求与投递
    测试独立控制当前查看资格。

    Args:
        plugin: 已执行 ``on_load`` 并初始化运行时的测试入口。
        msg_id: Planner 请求分享的源消息 ID。
        sharing_reason: Planner 提供的分享理由，可为空。
        content_summary: 达到既有 fallback 边界后使用的摘要，可为空。
        session_id: EARLY Hook 提供的真实 Planner 会话 ID。
        **host_context: 模拟 Host 在工具执行时注入的上下文字段；缺失字段会
            使用与默认测试 source 一致的 QQ 群聊值。

    Returns:
        ``request_cross_group_forward`` 正式 handler 返回的结果字典。
    """

    tool_call = {
        "id": "test-forward-request",
        "function": {
            "name": FORWARD_TOOL_NAME,
            "arguments": {
                "msg_id": msg_id,
                "sharing_reason": sharing_reason,
                "content_summary": content_summary,
            },
        },
    }
    authorization = await plugin.authorize_forward_request_context(
        session_id=session_id,
        response="",
        tool_calls=[tool_call],
    )
    arguments = authorization["modified_kwargs"]["tool_calls"][0]["function"]["arguments"]
    invocation_context = {
        "platform": "qq",
        "group_id": "10001",
        "stream_id": session_id,
        **host_context,
    }
    return await plugin.request_cross_group_forward(
        **arguments,
        **invocation_context,
    )


async def wait_for_background_tasks(plugin: RequestHarness) -> None:
    """等待当前快照中的全部后台投递任务结束。

    Args:
        plugin: 已加载并可能安排了转发任务的测试入口。

    Note:
        方法先复制任务集合，避免任务完成回调在等待期间修改原集合。
    """

    tasks = list(plugin.runtime.background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def seed_successful_view(
    plugin: RequestHarness,
    *,
    stream_id: str = "source-stream",
    message_id: str = "forward-message",
    content: str = "完整展开内容",
) -> None:
    """为与查看流程无关的测试同步一条当前上下文成功查看资格。

    Args:
        plugin: 已执行 ``on_load`` 并初始化运行时的测试入口。
        stream_id: 查看资格所属的 source 聊天流 ID。
        message_id: 已经完整查看的合并转发消息 ID。
        content: 登记到 source 当前上下文查看资格中的完整文本。
    """

    plugin.runtime.view_eligibility.sync_context(
        stream_id,
        [
            ViewToolObservation(
                call_id=f"seed-{message_id}",
                message_id=message_id,
                content=content,
                kind=ViewObservationKind.SUCCESS,
            )
        ],
    )


def sync_failed_views(
    plugin: RequestHarness,
    kinds: list[ViewObservationKind],
    *,
    stream_id: str = "source-stream",
    message_id: str = "forward-message",
) -> None:
    """把指定失败序列同步为 Planner 当前上下文的查看观察。

    Args:
        plugin: 已执行 ``on_load`` 并初始化运行时的测试入口。
        kinds: 按当前上下文顺序排列的失败分类列表。
        stream_id: 失败观察所属的 source 聊天流 ID。
        message_id: 查看失败对应的合并转发消息 ID。
    """

    plugin.runtime.view_eligibility.sync_context(
        stream_id,
        [
            ViewToolObservation(
                call_id=f"failure-{index}",
                message_id=message_id,
                content=f"分类失败 {index}",
                kind=kind,
            )
            for index, kind in enumerate(kinds, start=1)
        ],
    )


def mark_delivered(
    plugin: RequestHarness,
    targets: list[str],
    *,
    stream_id: str = "source-stream",
    message_id: str = "forward-message",
    now: float | None = None,
) -> None:
    """把一条**转发源消息**登记为已由**自动转发**投递到指定目标群。

    Args:
        plugin: 已装配运行时的测试入口。
        targets: 已投递的目标群号列表。
        stream_id: 源消息所在聊天流 ID。
        message_id: **转发源消息** ID。
        now: 登记时间戳；省略时取当前时间。
    """

    plugin.runtime.delivered_registry.register(
        build_delivered_key(stream_id, message_id),
        targets,
        time() if now is None else now,
    )


def set_group_list(plugin: RequestHarness, groups: list[str], *, now: float | None = None) -> None:
    """用一份协议端群列表刷新**群可用性缓存**。

    Args:
        plugin: 已装配运行时的测试入口。
        groups: 当前机器人群号列表（空列表表示这一轮没有可信数据）。
        now: 刷新时间戳；省略时取当前时间。
    """

    plugin.runtime.availability_cache.update(groups, time() if now is None else now)
