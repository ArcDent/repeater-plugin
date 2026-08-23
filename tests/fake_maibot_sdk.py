"""Fake maibot_sdk，供测试导入 plugin/config（真实 SDK 只存在于 MaiBot 容器内）。"""

from __future__ import annotations

import copy
from typing import Any

CONFIG_RELOAD_SCOPE_SELF = "self"


class PluginConfigBase:
    """按注解逐字段取默认值；默认值深拷贝，保证每个实例的嵌套 section 互相独立。

    kwargs 里的 dict 覆盖值自动转为对应嵌套 section（对齐真实 maibot_sdk 的
    pydantic 语义，如 ``SendingEmojisConfig(context={"enabled": True})``）。
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


class _FakeHookInfo:
    def __init__(self, hook: str, **kwargs: Any) -> None:
        self.hook = hook
        self.kwargs = kwargs


def HookHandler(hook: str, **kwargs: Any) -> Any:
    """假装饰器：附着元数据并原样返回函数，测试只直接调用被装饰方法。"""

    def decorator(func: Any) -> Any:
        func._hook_info = _FakeHookInfo(hook, **kwargs)  # type: ignore[attr-defined]
        return func

    return decorator