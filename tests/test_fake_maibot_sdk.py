"""fake_maibot_sdk 的 HookHandler 假装饰器契约测试。

真实 SDK 的 HookMode/HookOrder 是字符串枚举（maibot_sdk/types.py）：
mode ∈ {blocking, observe}、order ∈ {early, normal, late}，pydantic v2 校验
非法值抛 ValidationError。fake 必须对齐这个校验（大写值同样拒绝），
防止 fake 过宽把生产代码里的错误枚举值放行进真实容器。
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


def test_message_capability_sequence_returns_staged_results():
    """sequences 按消息 ID 预置「第 N 次调用返回什么」：就绪轮询测试需要先 None 后 dict。"""
    capability = fake_maibot_sdk.FakeMessageCapability(
        sequences={"90001": [None, None, {"processed_plain_text": "【合并转发消息】"}]}
    )

    async def scenario():
        first = await capability.get_by_id("90001")
        second = await capability.get_by_id("90001")
        third = await capability.get_by_id("90001")
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first is None
    assert second is None
    assert third == {"processed_plain_text": "【合并转发消息】"}
    assert [call["message_id"] for call in capability.calls] == ["90001"] * 3


def test_message_capability_sequence_exhausted_falls_back():
    """序列耗尽后回落 results / default_result；fail_next 仍优先返回 None。"""
    capability = fake_maibot_sdk.FakeMessageCapability(sequences={90001: [None]})
    capability.results["90001"] = {"processed_plain_text": "兜底"}

    async def scenario():
        staged = await capability.get_by_id("90001")
        fallback = await capability.get_by_id("90001")
        capability.fail_next = True
        failed = await capability.get_by_id("90001")
        return staged, fallback, failed

    staged, fallback, failed = asyncio.run(scenario())
    assert staged is None
    assert fallback == {"processed_plain_text": "兜底"}
    assert failed is None
