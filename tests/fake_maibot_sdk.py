"""Fake maibot_sdk，供测试导入 plugin/config（真实 SDK 只存在于 MaiBot 容器内）。"""

from __future__ import annotations

import copy
from typing import Any

CONFIG_RELOAD_SCOPE_SELF = "self"


class PluginConfigBase:
    """按注解逐字段取默认值；默认值深拷贝，保证每个实例的嵌套 section 互相独立。

    kwargs 里的 dict 覆盖值自动转为对应嵌套 section（对齐真实 maibot_sdk 的
    pydantic 语义，如 ``RepeaterConfig(context={"enabled": True})``）。
    """

    def __init__(self, **kwargs: Any) -> None:
        annotations: dict[str, Any] = getattr(self.__class__, "__annotations__", {})
        for key, value in kwargs.items():
            default = getattr(type(self), key, None)
            if isinstance(default, PluginConfigBase) and isinstance(value, dict):
                value = type(default)(**value)
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
    **_: Any,
) -> Any:
    if default_factory is not None:
        return default_factory()
    return default


class MaiBotPlugin:
    def __init__(self) -> None:
        self.ctx: Any = None
        self.config: Any = None


class FakeSendCapability:
    """ctx.send 替身（design D8）：记录每次调用供断言，不做真实校验。

    对齐真实 SDK 的 SendCapability.text 签名（maibot_sdk/capabilities/send.py：
    ``text(text, stream_id, *, return_details=False, **kwargs) -> bool | SendResult``）。
    raise_on_text 置为异常实例时每次调用都抛出（失败注入）；text_result 控制
    返回值（True/False，或 SendResult 形态的 dict），默认成功 True。
    """

    def __init__(self) -> None:
        self.text_calls: list[dict[str, Any]] = []
        self.raise_on_text: BaseException | None = None
        self.text_result: Any = True

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
        return func

    return decorator