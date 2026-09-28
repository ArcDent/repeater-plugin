"""配置适配与插件元数据读取的回归测试（原插件 test_config.py 的改写）。

原插件的独立 `config.toml` + 最近有效快照机制已随 `config_recovery.py` 一并
丢弃（本插件配置由 MaiBot SDK 加载）。这里改为覆盖 change task 3.3 的适配契约：
伪造本插件 `ForwardSection` 后，请求转发链路读取的源群与目标群必须正确，
且域开关同时受 `forward.enabled` 与 `plugin.enabled` 约束。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from repeater_plugin.forwards.request import (
    ForwardingRuntime,
    read_plugin_version,
)
from repeater_plugin.forwards.request.config import (
    ForwardRuntimeConfig,
    adapt_forward_config,
)
from repeater_plugin.forwards.request.source.models import ViewObservationKind
from repeater_plugin.forwards.state import (
    DeliveredRegistry,
    GroupAvailabilityCache,
)

from config import ForwardSection
from tests.forwards.request.support import (
    TEST_PLUGIN_VERSION,
    FakeContext,
    RequestHarness,
    build_forward_message,
    build_plugin,
    invoke_forward_tool,
    seed_successful_view,
    sync_failed_views,
    wait_for_background_tasks,
)

MANIFEST_JSON = Path(__file__).resolve().parents[3] / "_manifest.json"


def _bare_section_harness(tmp_path: Path, section: ForwardSection) -> RequestHarness:
    """直接用伪造的 `ForwardSection` 装配运行时（不经插件根配置）。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
        section: 待注入的转发节。

    Returns:
        已装配运行时但尚未启动的测试入口。
    """

    context = FakeContext(
        data_dir=tmp_path,
        message=build_forward_message(),
        streams=[
            {"platform": "qq", "group_id": "10001", "stream_id": "source-stream"},
            {"platform": "qq", "group_id": "20001", "stream_id": "target-a"},
            {"platform": "qq", "group_id": "20002", "stream_id": "target-b"},
        ],
    )
    runtime = ForwardingRuntime(
        context,
        lambda: section,
        delivered_registry=DeliveredRegistry(),
        availability_cache=GroupAvailabilityCache(),
        plugin_version=TEST_PLUGIN_VERSION,
    )
    return RequestHarness(context, runtime)


def test_bare_forward_section_supplies_groups(tmp_path: Path) -> None:
    """验证直接伪造 ForwardSection 时源群/目标群被规范化读取。

    群号列表允许混入空值与重复项，运行时必须去空、去重并保持首次出现顺序；
    域开关由该节自身的 ``enabled`` 与缺失的根配置（视为总开关开启）共同决定。
    该测试防止运行时回读已删除的旧配置字段名或使用未规范化的群号。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    section = ForwardSection(
        enabled=True,
        source_groups=[" 10001 ", "10002", "10001", ""],
        target_groups=["20002", " 20001 ", "20002"],
    )
    plugin = _bare_section_harness(tmp_path, section)

    assert plugin.runtime.config.enabled is True
    assert plugin.runtime.source_groups() == ["10001", "10002"]
    assert plugin.runtime.target_groups() == ["20002", "20001"]


def test_forward_section_gate_reaches_request_service(tmp_path: Path) -> None:
    """验证转发节上的域开关真正约束请求路径。

    直接伪造 ``enabled=False`` 的转发节时，请求必须在读取消息前被拒绝；该
    测试防止适配层只映射群号而丢掉开关。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    disabled = ForwardSection(
        enabled=False,
        source_groups=["10001"],
        target_groups=["20001"],
    )
    disabled_plugin = _bare_section_harness(tmp_path / "disabled", disabled)
    assert disabled_plugin.runtime.config.enabled is False

    enabled = ForwardSection(
        enabled=True,
        source_groups=["10001"],
        target_groups=["20001", "20002"],
    )
    plugin = _bare_section_harness(tmp_path / "enabled", enabled)
    assert plugin.runtime.config.enabled is True


def test_first_view_failure_falls_back_without_threshold_config(tmp_path: Path) -> None:
    """验证降级不再依赖任何阈值配置：首次可重试失败即用摘要投递。

    适配层与运行时视图都已删除查看失败阈值字段，请求路径必须照样在首次
    失败后创建任务；该测试防止运行时回读已删除字段（AttributeError）或
    重新引入计数分支。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        plugin = build_plugin(tmp_path)
        await plugin.on_load()
        sync_failed_views(plugin, [ViewObservationKind.RETRYABLE_FAILURE])

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            content_summary="首次失败后的降级摘要",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
        assert result["success"] is True
        assert result["accepted"] is True
        await wait_for_background_tasks(plugin)
        assert plugin.ctx.events[:2] == [("send", "target-a"), ("planner", "target-a")]
        await plugin.on_unload()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("forward_enabled", "plugin_enabled"),
    [(True, False), (False, True), (False, False)],
)
def test_domain_switch_requires_both_forward_and_plugin_enabled(
    tmp_path: Path,
    forward_enabled: bool,
    plugin_enabled: bool,
) -> None:
    """验证域开关是 forward.enabled 与 plugin.enabled 的合取。

    任一开关关闭时请求都必须被拒绝且不产生任何发送；该测试防止总开关失效，
    也防止把域开关实现成只看其中一个字段。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
        forward_enabled: 转发域独立开关取值。
        plugin_enabled: 插件总开关取值。
    """

    async def scenario() -> None:
        plugin = build_plugin(
            tmp_path,
            forward_enabled=forward_enabled,
            plugin_enabled=plugin_enabled,
        )
        await plugin.on_load()
        seed_successful_view(plugin)

        result = await invoke_forward_tool(
            plugin,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )

        assert result["success"] is False
        assert "未启用" in result["content"]
        assert plugin.ctx.events == []
        assert plugin.ctx.message.calls == []
        await plugin.on_unload()

    asyncio.run(scenario())


def test_target_trigger_switch_comes_from_forward_section(tmp_path: Path) -> None:
    """验证目标群触发开关从 ForwardSection 读取。

    源群入口提示已固化开启（不再有开关，行为由 source/test_trigger.py 覆盖）；
    ``trigger_target_planner=false`` 时投递成功后只发送、不触发目标群 Planner。
    该测试防止该开关在适配层被丢失或被写死。

    Args:
        tmp_path: pytest 提供的隔离数据目录。
    """

    async def scenario() -> None:
        target_off = build_plugin(tmp_path / "target-off", trigger_target_planner=False)
        await target_off.on_load()
        seed_successful_view(target_off)
        result = await invoke_forward_tool(
            target_off,
            "forward-message",
            platform="qq",
            group_id="10001",
            stream_id="source-stream",
        )
        assert result["success"] is True
        await wait_for_background_tasks(target_off)
        assert target_off.ctx.events == [("send", "target-a"), ("send", "target-b")]
        await target_off.on_unload()

    asyncio.run(scenario())


def test_adapted_config_drops_removed_forward_fields() -> None:
    """适配结果结构不含两个已删除字段：旧对象残留属性不再进入运行时视图。

    输入刻意构造成旧配置形态（带 `trigger_source_planner` /
    `view_failure_fallback_threshold`），适配层必须忽略它们：声明字段全集
    就是运行时读取的全集，防止删字段后仍有代码路径回读旧名。
    """

    legacy_section = SimpleNamespace(
        enabled=True,
        source_groups=["10001"],
        target_groups=["20001"],
        dedup_ttl_seconds=900.0,
        trigger_source_planner=False,
        trigger_target_planner=False,
        view_failure_fallback_threshold=9,
    )

    adapted = adapt_forward_config(legacy_section)

    assert isinstance(adapted, ForwardRuntimeConfig)
    assert {field.name for field in dataclasses.fields(adapted)} == {
        "enabled",
        "source_groups",
        "target_groups",
        "dedup_ttl_seconds",
        "trigger_target_planner",
    }
    assert adapted.trigger_target_planner is False


def test_plugin_version_is_read_from_manifest() -> None:
    """验证调试统计的版本分桶名来自插件根 manifest。

    本插件没有源码版本常量，运行时按 `forwards/request/runtime.py` 的相对位置
    读取 `_manifest.json`；路径写错或文件不可读时统计会静默降级，因此这里
    固定住相对位置与读取结果。
    """

    assert read_plugin_version() == json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))["version"]
    assert read_plugin_version(MANIFEST_JSON.parent / "missing-manifest.json") == ""
