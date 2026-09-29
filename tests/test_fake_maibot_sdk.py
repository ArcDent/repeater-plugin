"""fake_maibot_sdk 的契约测试：HookHandler 假装饰器与记录型 Host 替身。

HookHandler：真实 SDK 的 HookMode/HookOrder 是字符串枚举（maibot_sdk/types.py）：
mode ∈ {blocking, observe}、order ∈ {early, normal, late}，pydantic v2 校验
非法值抛 ValidationError。fake 必须对齐这个校验（大写值同样拒绝），
防止 fake 过宽把生产代码里的错误枚举值放行进真实容器。

FakePluginContext：转发退役回归把这些调用记录当成「零副作用」的证据，本文件
先证明每类调用都会落进对应记录（发生了就一定抓得到）。
"""

from __future__ import annotations

import asyncio

import pytest

from tests import fake_maibot_sdk


def test_hook_handler_accepts_valid_lowercase_values() -> None:
    assert fake_maibot_sdk.HookHandler("hook", mode="blocking", order="early") is not None
    assert fake_maibot_sdk.HookHandler("hook", mode="observe", order="late") is not None
    assert fake_maibot_sdk.HookHandler("hook") is not None  # 不传 mode/order 也合法


@pytest.mark.parametrize("mode", ["BLOCKING", "Observe", "block", "x"])
def test_hook_handler_rejects_invalid_mode(mode: str) -> None:
    with pytest.raises(ValueError):
        fake_maibot_sdk.HookHandler("hook", mode=mode)


@pytest.mark.parametrize("order", ["EARLY", "Normal", "first", "x"])
def test_hook_handler_rejects_invalid_order(order: str) -> None:
    with pytest.raises(ValueError):
        fake_maibot_sdk.HookHandler("hook", order=order)


def test_plugin_context_records_capability_calls() -> None:
    """记录型 Host 替身的契约：调用必须落进各自的 calls。

    转发退役回归靠这些记录断言「没有发生」（消息查询 / 群列表刷新 / 主动任务 /
    Host 受限通道），所以先证明「发生了就一定会被记到」。
    """
    ctx = fake_maibot_sdk.FakePluginContext()

    async def scenario() -> None:
        await ctx.message.get_by_id("90001")
        await ctx.chat.get_group_streams()
        await ctx.chat.open_session("qq", "group", group_id="10001")
        await ctx.maisaka.proactive.trigger("stream-10001", "转发决策")
        await ctx.call_host_method("cap.call", payload={"capability": "send.forward"})

    asyncio.run(scenario())

    assert [call["message_id"] for call in ctx.message.calls] == ["90001"]
    assert [call["method"] for call in ctx.chat.calls] == ["get_group_streams", "open_session"]
    assert [call["intent"] for call in ctx.maisaka.proactive.calls] == ["转发决策"]
    assert ctx.host_calls == [
        {"method": "cap.call", "payload": {"capability": "send.forward"}}
    ]
