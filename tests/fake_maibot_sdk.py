"""Fake maibot_sdk，供测试导入 plugin/config（真实 SDK 只存在于 MaiBot 容器内）。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

CONFIG_RELOAD_SCOPE_SELF = "self"


class _BoundedDefault:
    """带数值边界的字段默认值描述符（对齐真实 SDK 的 pydantic 约束）。

    类属性访问返回默认值本身（与纯值默认行为一致），`__set_name__` 把
    ``(ge, le)`` 记进 `_FIELD_BOUNDS`，供 ``PluginConfigBase`` 构造时做与
    pydantic 同口径的边界校验——fake 过宽会把「负权重按负值生效」这类生产侧
    不可能发生的行为放行（真实 SDK 在此抛 ValidationError）。
    仅非数据描述符：实例赋值照旧写入实例字典，测试仍可直接注入非法值。
    """

    __slots__ = ("ge", "le", "value")

    def __init__(self, value: Any, ge: float | None, le: float | None) -> None:
        self.value = value
        self.ge = ge
        self.le = le

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        return self.value

    def __set_name__(self, owner: type, name: str) -> None:
        _FIELD_BOUNDS[(owner, name)] = (self.ge, self.le)


_FIELD_BOUNDS: dict[tuple[type, str], tuple[float | None, float | None]] = {}


def _field_bound(owner: type, field: str) -> tuple[float | None, float | None] | None:
    """沿 MRO 查字段声明的 ``(ge, le)``；未声明时返回 None。"""

    for klass in owner.__mro__:
        bound = _FIELD_BOUNDS.get((klass, field))
        if bound is not None:
            return bound
    return None


def _check_field_bounds(owner: type, field: str, value: Any) -> None:
    """按 ``Field(ge=…, le=…)`` 的声明校验数值边界。

    仅校验 int/float（bool 排除），越界抛 ``ValueError``——真实 SDK 抛的
    pydantic ``ValidationError`` 是 ``ValueError`` 子类，两侧同口径。
    """

    bound = _field_bound(owner, field)
    if bound is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    ge, le = bound
    if ge is not None and value < ge:
        raise ValueError(f"{owner.__name__}.{field} 小于最小允许值 {ge}: {value!r}")
    if le is not None and value > le:
        raise ValueError(f"{owner.__name__}.{field} 大于最大允许值 {le}: {value!r}")


class PluginConfigBase:
    """按注解逐字段取默认值；默认值深拷贝，保证每个实例的嵌套 section 互相独立。

    kwargs 里的 dict 覆盖值自动转为对应嵌套 section（对齐真实 maibot_sdk 的
    pydantic 语义，如 ``RepeaterConfig(context={"enabled": True})``），并按
    ``Field(ge=/le=)`` 声明校验数值边界。
    """

    def __init__(self, **kwargs: Any) -> None:
        annotations: dict[str, Any] = getattr(self.__class__, "__annotations__", {})
        for key, value in kwargs.items():
            default = getattr(type(self), key, None)
            if isinstance(default, PluginConfigBase) and isinstance(value, dict):
                value = type(default)(**value)
            _check_field_bounds(type(self), key, value)
            setattr(self, key, value)
        for key in annotations:
            if key not in kwargs:
                setattr(self, key, copy.deepcopy(getattr(type(self), key, None)))


def Field(
    default: Any = None,
    *,
    default_factory: Any = None,
    description: str = "",
    json_schema_extra: Any = None,
    ge: float | None = None,
    le: float | None = None,
    **_: Any,
) -> Any:
    if default_factory is not None:
        return default_factory()
    if ge is None and le is None:
        return default
    return _BoundedDefault(default, ge, le)


class _FakeComponentInfo:
    """组件声明元数据（对齐真实 SDK 的 ``ComponentInfo`` 子类共同字段）。"""

    __slots__ = ("metadata", "name", "type")

    def __init__(self, name: str, component_type: str, metadata: dict[str, Any]) -> None:
        self.name = str(name)
        self.type = component_type
        self.metadata = dict(metadata)


def _attach_component(
    func: Any,
    name: str,
    component_type: str,
    metadata: dict[str, Any],
) -> Any:
    """把组件声明附着到函数（真实 SDK 用 ``_COMPONENT_INFO_ATTR`` 标记）。"""

    func._component_info = _FakeComponentInfo(name, component_type, metadata)
    return func


class MaiBotPlugin:
    def __init__(self) -> None:
        self.ctx: Any = None
        self.config: Any = None

    def get_components(self) -> list[dict[str, Any]]:
        """收集被装饰器标记的组件声明（对齐真实 SDK `collect_components` 形态）。

        产出 ``{"name", "type", "metadata"}`` 三元组，metadata 里附
        ``handler_name``；测试据此断言 Hook 订阅与 Tool 声明（含 ``chat_scope``
        这类由 Host 组件注册表消费的额外元数据）。
        """

        components: list[dict[str, Any]] = []
        for attr_name in dir(self):
            attr = getattr(self, attr_name, None)
            info = getattr(attr, "_component_info", None)
            if not callable(attr) or not isinstance(info, _FakeComponentInfo):
                continue
            components.append(
                {
                    "name": info.name,
                    "type": info.type,
                    "metadata": {**info.metadata, "handler_name": attr_name},
                }
            )
        return components


class FakeSendCapability:
    """ctx.send 替身（design D8）：记录每次调用供断言，不做真实校验。

    对齐真实 SDK 的 SendCapability.text / forward 签名（maibot_sdk/capabilities/
    send.py）。raise_on_text 置为异常实例时每次 text 调用都抛出（失败注入）；
    text_result 控制 text 返回值（True/False，或 SendResult 形态的 dict），默认
    成功 True。forward 的失败注入用 fail_forward_next，或用 forward_result
    覆盖返回值；未覆盖时按 return_details 生成与真实 SDK 同形的结果。
    """

    def __init__(self) -> None:
        self.text_calls: list[dict[str, Any]] = []
        self.raise_on_text: BaseException | None = None
        self.text_result: Any = True
        self.forward_calls: list[dict[str, Any]] = []
        self.fail_forward_next = False
        self.forward_result: Any = None

    async def text(
        self,
        text: str,
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.text_calls.append(
            {"text": text, "stream_id": stream_id, "return_details": return_details, **kwargs}
        )
        if self.raise_on_text is not None:
            raise self.raise_on_text
        return self.text_result

    async def forward(
        self,
        messages: list[dict[str, Any]],
        stream_id: str,
        *,
        return_details: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.forward_calls.append(
            {
                "messages": messages,
                "stream_id": stream_id,
                "return_details": return_details,
                **kwargs,
            }
        )
        if self.fail_forward_next:
            return {"sent": False, "message_id": None, "error": "模拟转发发送失败"}
        if self.forward_result is not None:
            return self.forward_result
        if return_details:
            return {"sent": True, "message_id": f"forward-{len(self.forward_calls)}"}
        return True


class FakeMaisakaContextCapability:
    """ctx.maisaka.context 替身（对齐 maibot_sdk/capabilities/maisaka.py 的 append 签名）。

    记录每次调用的全部参数供断言；fail_next 置 True 时返回
    ``{"success": False}``（注入失败语义是不抛异常、返回失败信封）。传入
    ``events`` 时额外追加 ``context.append:<source_kind>`` 时间线标记，供跨替身
    断言调用顺序（与 tests/forwards/request/support.py 同款写法）。
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False
        self.events = events

    async def append(
        self,
        stream_id: str,
        segments: list[dict[str, Any]],
        *,
        visible_text: str = "",
        source_kind: str = "",
        message_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "stream_id": stream_id,
                "segments": segments,
                "visible_text": visible_text,
                "source_kind": source_kind,
                "message_id": message_id,
                **kwargs,
            }
        )
        if self.events is not None:
            self.events.append(f"context.append:{source_kind}")
        return {"success": not self.fail_next, "index": len(self.calls) - 1}


class FakeMaisakaProactiveCapability:
    """ctx.maisaka.proactive 替身（对齐真实 SDK 的 trigger 签名）。

    fail_next 置 True 时返回 ``{"success": False, "error": ...}``，供
    「强制发言触发失败」路径断言；传入 ``events`` 时追加 ``proactive.trigger``
    时间线标记。
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False
        self.events = events

    async def trigger(
        self,
        stream_id: str,
        intent: str,
        *,
        reason: str = "",
        priority: str = "",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "stream_id": stream_id,
                "intent": intent,
                "reason": reason,
                "priority": priority,
                "metadata": dict(metadata or {}),
                **kwargs,
            }
        )
        if self.events is not None:
            self.events.append("proactive.trigger")
        if self.fail_next:
            return {"success": False, "error": "模拟主动任务失败"}
        return {"success": True, "queued": True, "task_id": f"task-{len(self.calls)}"}


class FakeMaisakaCapability:
    """ctx.maisaka 替身：context / proactive 两个子能力入口（同真实 SDK 的组合结构）。"""

    def __init__(self, events: list[str] | None = None) -> None:
        self.context = FakeMaisakaContextCapability(events)
        self.proactive = FakeMaisakaProactiveCapability(events)

    async def append_context(
        self,
        stream_id: str,
        segments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.context.append(stream_id, segments, **kwargs)

    async def trigger_proactive(
        self,
        stream_id: str,
        intent: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.proactive.trigger(stream_id, intent, **kwargs)


class FakeMessageCapability:
    """ctx.message 替身（对齐真实 SDK message.get_by_id 签名）。

    results 按消息 ID 预置返回值，未命中回落 default_result（默认 None，
    对齐 before_process 时消息尚未落库的时序）；sequences 按消息 ID 预置
    「第 N 次调用返回什么」的队列（就绪轮询测试用：前两次 None、第三次就绪），
    队列耗尽后回落 results / default_result；fail_next 时返回 None。
    """

    def __init__(self, sequences: dict[Any, list[Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}
        self.default_result: Any = None
        self.fail_next = False
        self.sequences: dict[str, list[Any]] = {
            str(message_id): list(values) for message_id, values in (sequences or {}).items()
        }

    async def get_by_id(
        self,
        message_id: str,
        *,
        chat_id: str = "",
        stream_id: str = "",
        include_binary_data: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "message_id": message_id,
                "chat_id": chat_id,
                "stream_id": stream_id,
                "include_binary_data": include_binary_data,
                **kwargs,
            }
        )
        if self.fail_next:
            return None
        queue = self.sequences.get(str(message_id))
        if queue:
            return queue.pop(0)
        return self.results.get(str(message_id), self.default_result)


class FakeChatCapability:
    """ctx.chat 替身（对齐真实 SDK 的 chat capability 三个方法签名）。

    group_streams 是预设的群聊流列表（返回副本，防调用方原地改写）；calls 记录
    每次调用（method + 参数）供「缓存新鲜时不重复查询」类断言；fail_next 时
    查询返回 None、open_session 也返回 None（不可达语义，由调用方决定放行或
    记 warning）。
    """

    def __init__(self) -> None:
        self.group_streams: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False

    async def get_group_streams(self, platform: str = "qq") -> list[dict[str, Any]] | None:
        self.calls.append({"method": "get_group_streams", "platform": platform})
        if self.fail_next:
            return None
        return [dict(stream) for stream in self.group_streams]

    async def get_stream_by_group_id(
        self, group_id: str, platform: str = "qq"
    ) -> dict[str, Any] | None:
        """按群号查预设群聊流；命中不到返回 None（真实 SDK 的未命中语义）。"""

        self.calls.append(
            {"method": "get_stream_by_group_id", "group_id": str(group_id), "platform": platform}
        )
        if self.fail_next:
            return None
        for stream in self.group_streams:
            if str(stream.get("group_id") or "") == str(group_id):
                return dict(stream)
        return None

    async def open_session(
        self,
        platform: str,
        chat_type: str,
        *,
        group_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """按群号建流并追加进预设列表（真实 SDK 的 open_session 成功解包形态）。"""

        self.calls.append(
            {
                "method": "open_session",
                "platform": platform,
                "chat_type": chat_type,
                "group_id": str(group_id),
                "user_id": str(user_id),
            }
        )
        if self.fail_next:
            return None
        session_id = f"opened-{group_id or user_id}"
        stream = {
            "platform": platform,
            "group_id": str(group_id),
            "user_id": str(user_id),
            "stream_id": session_id,
            "session_id": session_id,
            "is_group_session": chat_type == "group",
            "chat_type": chat_type,
        }
        self.group_streams.append(stream)
        return dict(stream)


class FakeConfigCapability:
    """ctx.config 替身（对齐真实 SDK 的 ``config.get(key, default)`` 签名）。"""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.get_calls = 0

    async def get(self, key: str, default: Any = None) -> Any:
        self.get_calls += 1
        return self.values.get(key, default)


class FakeLogger:
    """logging.Logger 接口形态的替身：记录 (level, 文本) 便于断言。

    warning 等方法按标准 logging 签名收 *args/**kwargs：生产侧调用点既有
    %-style 参数形态（forwards/request 包），也有 exc_info= 形态，替身必须兼容。
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        self.records.append((level, message % args if args else str(message)))

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, *args, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, *args, **kwargs)

    def texts(self, level: str) -> list[str]:
        return [message for lvl, message in self.records if lvl == level]


class FakePluginPaths:
    """ctx.paths 替身：请求转发状态与调试统计文件落在 data_dir 下。"""

    def __init__(self, data_dir: Any) -> None:
        self.data_dir = Path(data_dir)
        self.runtime_dir = self.data_dir / "runtime"


class FakePluginContext:
    """PluginContext 替身：装配本插件在转发域消费的全部 capability 面。

    plugin.py 的转发域接线与 `forwards.request.ForwardingRuntime` 共用同一只
    ctx（logger / config / paths / chat / message / send / maisaka /
    call_host_method）。`call_host_method("cap.call")` 走真实 SDK 的受限原始
    通道形态，这里把 ``send.forward`` 调用转给 `self.send.forward` 并用
    host_calls 记录每次原始调用。
    """

    def __init__(self, data_dir: Any, events: list[str] | None = None) -> None:
        self.logger = FakeLogger()
        self.config = FakeConfigCapability()
        self.paths = FakePluginPaths(data_dir)
        self.chat = FakeChatCapability()
        self.message = FakeMessageCapability()
        self.send = FakeSendCapability()
        self.events: list[str] = [] if events is None else events
        self.maisaka = FakeMaisakaCapability(self.events)
        self.host_calls: list[dict[str, Any]] = []
        self.host_call_result: Any = None

    async def call_host_method(self, method: str, **kwargs: Any) -> Any:
        self.host_calls.append({"method": method, **kwargs})
        if self.host_call_result is not None:
            return self.host_call_result
        payload = kwargs.get("payload")
        if isinstance(payload, dict) and payload.get("capability") == "send.forward":
            args = dict(payload.get("args") or {})
            messages = args.pop("messages", [])
            stream_id = args.pop("stream_id", "")
            return await self.send.forward(messages, stream_id, **args)
        return {"success": True}


class _FakeHookInfo:
    def __init__(self, hook: str, **kwargs: Any) -> None:
        self.hook = hook
        self.kwargs = kwargs


_HOOK_MODES = {"blocking", "observe"}
_HOOK_ORDERS = {"early", "normal", "late"}


def HookHandler(hook: str, **kwargs: Any) -> Any:
    """假装饰器：附着元数据并原样返回函数，测试只直接调用被装饰方法。

    对 mode/order 做值校验（对齐真实 SDK 的 pydantic 枚举校验，
    maibot_sdk/types.py 的 HookMode/HookOrder），传大写等非法值直接抛
    ValueError，防止 fake 过宽放行生产代码里的错误枚举值。
    """

    mode = kwargs.get("mode")
    if mode is not None and mode not in _HOOK_MODES:
        raise ValueError(f"invalid hook mode: {mode!r} (allowed: {sorted(_HOOK_MODES)})")
    order = kwargs.get("order")
    if order is not None and order not in _HOOK_ORDERS:
        raise ValueError(f"invalid hook order: {order!r} (allowed: {sorted(_HOOK_ORDERS)})")

    def decorator(func: Any) -> Any:
        func._hook_info = _FakeHookInfo(hook, **kwargs)  # type: ignore[attr-defined]
        declared = {key: value for key, value in kwargs.items() if key != "name"}
        return _attach_component(
            func,
            str(kwargs.get("name") or func.__name__),
            "HOOK_HANDLER",
            {"hook": hook, **declared},
        )

    return decorator


def Tool(
    name: str,
    description: str = "",
    brief_description: str = "",
    detailed_description: str = "",
    parameters: list[Any] | dict[str, Any] | None = None,
    **metadata: Any,
) -> Any:
    """假装饰器：附着 Tool 组件声明（对齐真实 SDK 的 Tool 元数据面）。

    parameters 支持结构化列表或旧式 dict；额外关键字（如 ``chat_scope``）进入
    metadata 原样保留——Host 组件注册表就是从这个字段读取聊天类型适用范围的。
    """

    def decorator(func: Any) -> Any:
        declared = {
            "description": description or brief_description,
            "brief_description": brief_description or description,
            "detailed_description": detailed_description,
            "parameters": parameters if isinstance(parameters, list) else [],
            "parameters_raw": parameters if isinstance(parameters, dict) else {},
            "invoke_method": "plugin.invoke_tool",
            **metadata,
        }
        return _attach_component(func, name, "TOOL", declared)

    return decorator