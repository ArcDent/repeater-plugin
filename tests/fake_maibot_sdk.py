"""Fake maibot_sdk，供测试导入 plugin/config（真实 SDK 只存在于 MaiBot 容器内）。"""

from __future__ import annotations

import copy
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
    ``Field(ge=/le=)`` 声明校验数值边界；未在注解里声明的键按 pydantic 默认的
    ``extra="ignore"`` 直接丢弃（旧配置残留的 [forward] 段走这条路径）。
    """

    def __init__(self, **kwargs: Any) -> None:
        declared = config_field_names(type(self))
        for key, value in kwargs.items():
            if key not in declared:
                continue
            default = getattr(type(self), key, None)
            if isinstance(default, PluginConfigBase) and isinstance(value, dict):
                value = type(default)(**value)
            _check_field_bounds(type(self), key, value)
            setattr(self, key, value)
        for key in declared:
            if key not in kwargs:
                setattr(self, key, copy.deepcopy(getattr(type(self), key, None)))


def config_field_names(model: type) -> list[str]:
    """配置模型的字段名：真实 SDK（pydantic）读 `model_fields`，本替身退化为注解清单。

    两者都只反映**声明面**；替身构造时也只接受这里列出的键（未知键按真实
    pydantic 的 ``extra="ignore"`` 丢弃），`hasattr` 断言因此与真实 SDK 同口径。
    """
    fields = getattr(model, "model_fields", None)
    if isinstance(fields, dict):
        return list(fields)
    return [name for name in model.__annotations__ if not name.startswith("__")]


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
        ``handler_name``；测试据此断言 Hook 订阅声明（Host 组件注册表按它派发）。
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
        declared = {key: value for key, value in kwargs.items() if key != "name"}
        return _attach_component(
            func,
            str(kwargs.get("name") or func.__name__),
            "HOOK_HANDLER",
            {"hook": hook, **declared},
        )

    return decorator
