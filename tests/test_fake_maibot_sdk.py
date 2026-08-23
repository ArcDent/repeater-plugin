"""fake_maibot_sdk 的 HookHandler 假装饰器契约测试。

真实 SDK 的 HookMode/HookOrder 是字符串枚举（maibot_sdk/types.py）：
mode ∈ {blocking, observe}、order ∈ {early, normal, late}，pydantic v2 校验
非法值抛 ValidationError。fake 必须对齐这个校验（大写值同样拒绝），
防止 fake 过宽把生产代码里的错误枚举值放行进真实容器。
"""

from __future__ import annotations

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
