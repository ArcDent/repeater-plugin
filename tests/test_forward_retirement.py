"""转发域退役回归（openspec change remove-cross-group-forward，design D4）。

本文件是转发域删除的回归证明，三条落点全部走公共接口：

1. 组件面：`create_plugin().get_components()` 恰为两个保留 Hook，无旧 Tool、
   无 Planner Hook；
2. 配置与能力面：`RepeaterConfig` 的模型声明与 manifest capabilities 不再含
   转发字段 / 能力，旧配置样例不复活任何转发面；
3. 消息路径：按组件元数据（`handler_name`）派发剩余 Hook，普通合并转发、
   重复上报、未展开转发、机器人自发转发都不产生摘要查询、群列表刷新、转发
   发送、Planner 主动任务与 forward 注入，结果一律 continue。

期望值就地自证，不依赖仓库外记录：

- 组件面：退役后存活的两个域各一个 Hook（通知 guard 与复读检测），恰 2 个；
- 配置面：config.py 的 9 个 section（AGENTS.md「结构」的配置模型口径）；
- 能力面：存活域只消费 4 项主程序能力（注入路径三项 + 复读观测 Hook）；
- 退役面：下方 REMOVED_* 常量逐项列举旧转发域的 6 个组件与 7 项能力，任何
  一项重新出现都会被整集合等值断言判红。

不 import 已删除的 forwards 包，也不调用已删除的私有转发方法。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from config import RepeaterConfig
from plugin import RepeaterPlugin, create_plugin
from tests.fake_maibot_sdk import FakePluginContext, config_field_names

BOT_QQ = "40004"
SOURCE_GROUP = "10001"
SOURCE_STREAM = "stream-10001"
MANIFEST_JSON = Path(__file__).resolve().parent.parent / "_manifest.json"

# 退役前 8 个组件里的 6 个旧转发组件（两个旧 Tool 与四个转发路径 Hook）；
# 存活的两个各属一个保留域——表情回应通知 guard 与复读检测。
RETAINED_COMPONENTS = {"emoji_reaction_notify_guard", "repeat_detector"}
REMOVED_COMPONENTS = {
    "request_cross_group_forward",
    "decide_forward_delivery",
    "forward_detector",
    "forward_view_result_capture",
    "forward_request_authorize",
    "forward_auto_view_orchestrator",
}

# config.py 在转发域退役后剩 9 个 section（AGENTS.md「结构」的配置模型口径）。
RETAINED_SECTIONS = {
    "plugin",
    "webhook",
    "protocol",
    "bandwagon",
    "opposite",
    "repeat",
    "behavior",
    "context",
    "scope",
}

# 存活域留存的 4 项能力授权：注入路径三项（maisaka.context.append /
# chat.get_stream_by_group_id / config.get）+ 复读观测 Hook
# chat.receive.before_process（源码派生等值断言在 test_plugin.py）。
RETAINED_CAPABILITIES = {
    "maisaka.context.append",
    "chat.get_stream_by_group_id",
    "config.get",
    "chat.receive.before_process",
}
# 旧转发域的候选能力（自动与请求两条路径各自消费）；退役后一项不留。
REMOVED_CAPABILITIES = {
    "maisaka.proactive.trigger",
    "chat.get_group_streams",
    "chat.open_session",
    "message.get_by_id",
    "send.forward",
    "maisaka.planner.before_request",
    "maisaka.planner.after_response",
}

# 旧版 config.toml 的 [forward] 段残留（含 v0.8.0 已删的两个字段）。升级口径：
# 转发字段不进入有效配置，也不复活任何组件，剩余域的值照常生效。
LEGACY_FORWARD_PAYLOAD: dict[str, Any] = {
    "forward": {
        "enabled": True,
        "mode": "direct",
        "auto_enabled": True,
        "request_enabled": True,
        "source_groups": ["10001"],
        "target_groups": ["20001"],
        "weight_send": 30,
        "weight_silence": 70,
        "trigger_source_planner": False,
        "view_failure_fallback_threshold": 5,
    },
    "repeat": {"window_seconds": 90},
}


def components_by_name(plugin: RepeaterPlugin) -> dict[str, dict[str, Any]]:
    return {component["name"]: component for component in plugin.get_components()}


async def dispatch_hook(
    plugin: RepeaterPlugin, component_name: str, message: dict[str, Any]
) -> dict[str, Any]:
    """按组件元数据派发一个 Hook：从 `get_components()` 取 `handler_name` 再调用。

    测试不硬编码 handler 方法名——注册表指向哪个方法就调哪个，顺带证明元数据
    指向真实可调用对象。
    """
    component = components_by_name(plugin)[component_name]
    handler = getattr(plugin, component["metadata"]["handler_name"])
    return await handler(message=message)


def dispatch(
    plugin: RepeaterPlugin, component_name: str, message: dict[str, Any]
) -> dict[str, Any]:
    return asyncio.run(dispatch_hook(plugin, component_name, message))


def pending_tasks(plugin: RepeaterPlugin) -> list[asyncio.Task]:
    return [task for task in plugin._tasks if not task.done()]


async def drain(plugin: RepeaterPlugin) -> None:
    tasks = pending_tasks(plugin)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def make_plugin() -> RepeaterPlugin:
    """只带记录型 Host 替身的插件实例：协议端调用查表记录，不启监听。

    复读出口权重定死（复读拉满、打断与沉默归零）且延迟归零：负向用例在门禁
    失效时必然失败，而不是靠默认权重蒙对（AGENTS.md「测试注意」）；正向控制
    用例也因此必定落复读出口，证明派发通道确实走到了出口代码。
    """
    plugin = RepeaterPlugin()
    plugin.config = RepeaterConfig()
    plugin.ctx = FakePluginContext()
    plugin.config.protocol.base_url = "http://protocol.test:3000"
    plugin.config.protocol.self_qq = BOT_QQ
    plugin.config.repeat.weight_repeat = 1000
    plugin.config.repeat.weight_interrupt = 0
    plugin.config.repeat.weight_silence = 0
    plugin.config.repeat.delay_min_seconds = 0.0
    plugin.config.repeat.delay_max_seconds = 0.0
    plugin.config.repeat.cooldown_seconds = 0
    plugin._runner = None
    plugin._site = None
    plugin._tasks = set()
    plugin._flush_loop_task = None
    plugin._self_qq = BOT_QQ
    plugin._emoji_action_supported = True
    # 注入文案要落进群流：hook 上报的 session_id 优先，chat 能力查询是兜底
    # （AGENTS.md「群 → 活跃聊天流的解析」），这里给兜底预备一条可用流。
    plugin.ctx.chat.group_streams = [
        {
            "platform": "qq",
            "group_id": SOURCE_GROUP,
            "stream_id": SOURCE_STREAM,
            "session_id": SOURCE_STREAM,
            "is_group_session": True,
        }
    ]
    plugin._rebuild_runtime()

    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(action_name: str, params: dict[str, Any]) -> Any:
        calls.append((action_name, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 70001}}

    plugin._call_protocol = fake_call  # type: ignore[assignment]
    plugin.protocol_calls = calls  # type: ignore[attr-defined]
    return plugin


def protocol_actions(plugin: RepeaterPlugin) -> list[str]:
    return [name for name, _ in plugin.protocol_calls]  # type: ignore[attr-defined]


# ---- hook 载荷（主程序 hook 侧序列化 SessionMessage 形态）----

def text_message(text: str, sender: str, *, message_id: str = "msg-text") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "platform": "qq",
        "session_id": SOURCE_STREAM,
        "is_notify": False,
        "message_info": {
            "group_info": {"group_id": SOURCE_GROUP, "group_name": "某群"},
            "user_info": {"user_id": sender, "user_nickname": "某人", "user_cardname": ""},
        },
        "raw_message": [{"type": "text", "data": text}],
    }


def forward_node(text: str = "转发内容") -> dict[str, Any]:
    return {
        "user_id": "20002",
        "user_nickname": "群友甲",
        "user_cardname": "",
        "message_id": "80001",
        "content": [{"type": "text", "data": text}],
    }


def merged_forward_message(
    *,
    nodes: Any = "default",
    segments: list[dict[str, Any]] | None = None,
    sender: str = "20002",
    message_id: str = "msg-forward",
) -> dict[str, Any]:
    """合并转发的 hook 载荷：`raw_message` 里一个 `forward` 段。

    `nodes="default"` 给一个展开节点；`nodes=[]` / `nodes=None` 是**未展开转发**
    （协议端没给节点数据）；`segments` 覆盖整段列表（混合文本 + 转发用）。
    """
    if segments is None:
        data = [forward_node()] if nodes == "default" else nodes
        segments = [{"type": "forward", "data": data}]
    return {
        "message_id": message_id,
        "platform": "qq",
        "session_id": SOURCE_STREAM,
        "is_notify": False,
        "message_info": {
            "group_info": {"group_id": SOURCE_GROUP, "group_name": "某群"},
            "user_info": {"user_id": sender, "user_nickname": "某人", "user_cardname": ""},
        },
        "raw_message": segments,
    }


def notify_message(notice_type: str = "group_msg_emoji_like") -> dict[str, Any]:
    return {
        "message_id": "msg-notify",
        "platform": "qq",
        "session_id": SOURCE_STREAM,
        "is_notify": True,
        "message_info": {
            "group_info": {"group_id": SOURCE_GROUP, "group_name": "某群"},
            "additional_config": {"napcat_notice_type": notice_type},
        },
    }


def assert_no_forward_side_effect_calls(plugin: RepeaterPlugin) -> None:
    """零转发副作用：摘要查询 / 聊天流查询 / 转发发送 / 主动任务 / 注入 / 任务全空。"""
    ctx = plugin.ctx
    assert ctx.message.calls == [], "消息路径不得查询消息（摘要就绪轮询已退役）"
    assert ctx.chat.calls == [], "消息路径不得查询聊天流能力（群列表刷新 / 建流已退役）"
    assert ctx.maisaka.proactive.calls == [], "消息路径不得触发源 / 目标群 Planner 主动任务"
    assert ctx.maisaka.context.calls == [], "消息路径不得产生转发内容 / 留痕注入"
    assert ctx.send.text_calls == [], "消息路径不得走主程序发送链"
    assert ctx.host_calls == [], "消息路径不得走 Host 受限原始通道（send.forward）"
    assert plugin.protocol_calls == [], "消息路径不得直发转发（send_group_forward_msg / get_group_list）"
    assert pending_tasks(plugin) == [], "消息路径不得创建后台任务"


def assert_no_forward_side_effects(plugin: RepeaterPlugin, result: dict[str, Any]) -> None:
    assert result["action"] == "continue"
    assert_no_forward_side_effect_calls(plugin)


# ---- 4.2 组件、配置与能力退役断言 ----

def test_components_are_exactly_the_two_retained_hooks():
    """组件面恰为两个保留 Hook：无旧 Tool、无转发 Planner Hook、无兼容别名。"""
    plugin = create_plugin()
    components = plugin.get_components()
    by_name = components_by_name(plugin)

    assert set(by_name) == RETAINED_COMPONENTS
    # RETAINED/REMOVED 都是文件内字面量：上面整集合等值已覆盖任何转发组件
    # 残留，这条只自检两份清单互斥，避免退役清单与保留清单重复计数。
    assert RETAINED_COMPONENTS.isdisjoint(REMOVED_COMPONENTS)
    assert {component["type"] for component in components} == {"HOOK_HANDLER"}
    assert {component["metadata"]["hook"] for component in components} == {
        "chat.receive.before_process"
    }
    for component in components:
        assert component["metadata"]["mode"] == "blocking"
        assert callable(getattr(plugin, component["metadata"]["handler_name"], None))
    assert by_name["emoji_reaction_notify_guard"]["metadata"]["order"] == "early"
    assert by_name["repeat_detector"]["metadata"]["order"] == "late"


def test_config_model_declares_only_retained_sections():
    """配置模型声明面：删除后恰剩 9 个 section，`forward` 不再存在。"""
    field_names = config_field_names(RepeaterConfig)
    assert set(field_names) == RETAINED_SECTIONS

    default = RepeaterConfig()
    for name in RETAINED_SECTIONS:
        assert getattr(default, name, None) is not None


def test_manifest_capabilities_are_exactly_retained_four():
    """能力面：manifest 恰授权 4 项共享能力，7 项转发候选能力一项不剩。"""
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    declared = set(manifest["capabilities"])

    assert declared == RETAINED_CAPABILITIES
    # 同组件面：能力残留由上面等值断言覆盖，这条只自检文件内两份清单互斥。
    assert RETAINED_CAPABILITIES.isdisjoint(REMOVED_CAPABILITIES)


def test_legacy_forward_payload_does_not_revive_forward_domain():
    """旧配置样例（含 forward.enabled=true 与旧权重）不进有效配置，也不重置剩余域值。"""
    plugin = create_plugin()
    plugin.config = RepeaterConfig(**LEGACY_FORWARD_PAYLOAD)

    # 组件声明是类体静态面，与配置载荷无因果（组件面等值断言在
    # test_components_are_exactly_the_two_retained_hooks）：这里只钉配置面与
    # 剩余域值——旧 [forward] 段既不进模型声明，也不重置用户自定义的复读值。
    assert "forward" not in config_field_names(RepeaterConfig)
    assert plugin.config.repeat.window_seconds == 90  # 剩余域用户值不被重置


# ---- 4.3 消息路径无转发副作用断言 ----

def test_plain_merged_forward_is_passed_through_without_forward_side_effects():
    plugin = make_plugin()
    result = dispatch(plugin, "repeat_detector", merged_forward_message())
    assert_no_forward_side_effects(plugin, result)


def test_repeated_merged_forward_reports_stay_side_effect_free():
    """同一合并转发重复上报：两次都放行，第二次仍不产生任何新副作用。"""
    plugin = make_plugin()
    payload = merged_forward_message()
    assert_no_forward_side_effects(plugin, dispatch(plugin, "repeat_detector", payload))
    assert_no_forward_side_effects(plugin, dispatch(plugin, "repeat_detector", payload))


def test_unexpanded_forward_is_passed_through_without_forward_side_effects():
    """未展开转发（forward 段无 data / data 为空 / data 无节点）同样只放行。"""
    plugin = make_plugin()
    payloads = [
        merged_forward_message(segments=[{"type": "forward"}], message_id="msg-no-data"),
        merged_forward_message(nodes=[], message_id="msg-empty-data"),
        merged_forward_message(nodes=None, message_id="msg-none-data"),
    ]
    for payload in payloads:
        assert_no_forward_side_effects(plugin, dispatch(plugin, "repeat_detector", payload))


def test_bot_self_merged_forward_is_passed_through_without_forward_side_effects():
    """机器人自己发出的合并转发不触发任何域（自查自我保护 + 非文本段二重排除）。"""
    plugin = make_plugin()
    result = dispatch(
        plugin, "repeat_detector", merged_forward_message(sender=BOT_QQ, message_id="msg-self")
    )
    assert_no_forward_side_effects(plugin, result)


def test_forward_segment_does_not_extend_repeat_chain_and_is_not_intercepted():
    """含 `forward` 段的消息不参与复读链，也不触发复读出口。

    前两条纯文本（两个不同发送者）让链停在 2；第三条「文本 + forward 段」是
    **非候选**（只要有一个非文本段就整条排除），既不累加也不拦截；第四条纯文本
    到达阈值 3 才拦截——证明链确实只数到两条，也证明派发通道走到了出口代码
    （协议直发与两路注入真实发生）。
    """
    plugin = make_plugin()
    for index, sender in enumerate(("20002", "20003")):
        assert_no_forward_side_effects(
            plugin,
            dispatch(
                plugin, "repeat_detector", text_message("草", sender, message_id=f"m{index}")
            ),
        )

    mixed = merged_forward_message(
        segments=[
            {"type": "text", "data": "草"},
            {"type": "forward", "data": [forward_node()]},
        ],
        sender="20004",
        message_id="m2",
    )
    assert_no_forward_side_effects(plugin, dispatch(plugin, "repeat_detector", mixed))

    async def trigger() -> dict[str, Any]:
        result = await dispatch_hook(
            plugin, "repeat_detector", text_message("草", "20005", message_id="m3")
        )
        await drain(plugin)
        return result

    assert asyncio.run(trigger())["action"] == "abort"
    assert protocol_actions(plugin) == ["send_group_msg"]
    assert [call["source_kind"] for call in plugin.ctx.maisaka.context.calls] == [
        "repeat_trigger",
        "repeat",
    ]


def test_notify_guard_still_aborts_notify_and_passes_merged_forward():
    """通知 guard 既有语义不变：通知 abort、其余放行，两处都不碰转发面。"""
    plugin = make_plugin()
    plugin.config.context.enabled = True
    plugin._rebuild_runtime()

    aborted = dispatch(plugin, "emoji_reaction_notify_guard", notify_message())
    assert aborted["action"] == "abort"
    assert_no_forward_side_effect_calls(plugin)

    assert_no_forward_side_effects(
        plugin, dispatch(plugin, "emoji_reaction_notify_guard", merged_forward_message())
    )

    plugin.config.context.enabled = False
    plugin._rebuild_runtime()
    assert dispatch(plugin, "emoji_reaction_notify_guard", notify_message())["action"] == "continue"
