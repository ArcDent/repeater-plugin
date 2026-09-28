"""请求转发运行时的配置视图（change design D1/D6）。

原插件的配置模块（`ForwardMessagesAutoConfig` 的 `plugin` / `routing` /
`behavior` 三节 + 独立 `config.toml` 机制）不再移植：转发域配置统一来自本
插件的 `ForwardSection` 与 `PluginSection`（ADR 0009、design D6）。本模块把
新配置适配成请求转发链路读取的扁平稳类型视图，运行时只读这里定义的字段，
不读旧字段名。

本模块不依赖 `maibot_sdk`：`config_provider` 返回的可以是本插件根配置、
`ForwardSection` 本身，或任意同形对象（测试替身）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class GroupIdList:
    """清洗 QQ 群号并保持首次出现顺序（自原插件 config.py 原样迁移）。"""

    @staticmethod
    def normalize(values: Iterable[Any] | None) -> list[str]:
        """将配置中的任意群号值规范化为有序字符串列表。

        每个值都会先转换为字符串并去除首尾空白。空值和重复值会被忽略，
        重复项保留第一次出现的位置。

        Args:
            values: 可迭代的群号配置值，元素可以是字符串、整数或空值；
                传入 ``None`` 时返回空列表。

        Returns:
            去空、去重且保持首次出现顺序的 QQ 群号字符串列表。

        Examples:
            ``GroupIdList.normalize([" 100 ", 200, "100"])`` 返回
            ``["100", "200"]``。
        """

        result: list[str] = []
        seen: set[str] = set()
        for value in values or ():
            group_id = str(value or "").strip()
            if not group_id or group_id in seen:
                continue
            seen.add(group_id)
            result.append(group_id)
        return result


@dataclass(frozen=True, slots=True)
class ForwardRuntimeConfig:
    """请求转发链路读取的转发域配置。

    ``enabled`` 是转发域总闸：`plugin.enabled` 与 `forward.enabled` 的合取
    （spec「转发域开关与配置」：域开关独立生效，插件总开关关闭时整域一并
    停用）。其余字段一一对应 `ForwardSection` 的同名字段。
    """

    enabled: bool = False
    source_groups: tuple[str, ...] = ()
    target_groups: tuple[str, ...] = ()
    dedup_ttl_seconds: float = 1800.0
    trigger_target_planner: bool = True


def adapt_forward_config(source: Any) -> ForwardRuntimeConfig:
    """把本插件配置（或其转发节）适配为 `ForwardRuntimeConfig`。

    支持三种输入：已经是 `ForwardRuntimeConfig` 时原样返回；带 ``forward``
    属性的根配置（`RepeaterConfig`）取其转发节并用同根 ``plugin.enabled``
    判断总开关；其余对象按转发节本身处理（总开关视为开启，便于单测直接伪造
    `ForwardSection`）。字段缺失时回落默认值。

    Args:
        source: 配置提供器返回的对象。

    Returns:
        运行时读取的不可变配置视图。
    """

    if isinstance(source, ForwardRuntimeConfig):
        return source

    forward = getattr(source, "forward", None)
    plugin_enabled = True
    if forward is None:
        forward = source
    else:
        plugin_section = getattr(source, "plugin", None)
        plugin_enabled = bool(getattr(plugin_section, "enabled", True))

    enabled = plugin_enabled and bool(getattr(forward, "enabled", False))
    raw_source_groups = getattr(forward, "source_groups", ()) or ()
    raw_target_groups = getattr(forward, "target_groups", ()) or ()
    return ForwardRuntimeConfig(
        enabled=enabled,
        source_groups=tuple(GroupIdList.normalize(raw_source_groups)),
        target_groups=tuple(GroupIdList.normalize(raw_target_groups)),
        dedup_ttl_seconds=_float_field(forward, "dedup_ttl_seconds", 1800.0),
        trigger_target_planner=bool(getattr(forward, "trigger_target_planner", True)),
    )


def _float_field(source: Any, name: str, default: float) -> float:
    """读取数值字段并安全转换为浮点数。

    Args:
        source: 配置节对象。
        name: 字段名称。
        default: 缺失或不可转换时的回退值。

    Returns:
        可用于比较与 TTL 计算的浮点数。
    """

    try:
        return float(getattr(source, name, default))
    except (TypeError, ValueError):
        return default
