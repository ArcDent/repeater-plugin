"""导入身份回归：`forwards` 的两种名字必须指向同一批模块对象。

`conftest.py` 把仓库根注册为合成包 `repeater_plugin`，并把 `forwards`
子树的顶层别名一并注册（`sys.modules["forwards"]`、`sys.modules["forwards.request"]` 等）。
本用例防止后续改动删掉别名后，`import forwards.request` 与
`repeater_plugin.forwards.*` 各自加载出第二份模块——双身份会让
isinstance、单例状态与 monkeypatch 全部失效。
"""

from __future__ import annotations

import importlib
import sys

from repeater_plugin.forwards.request.core.messages import ForwardMessageParser

CANONICAL_PREFIX = "repeater_plugin.forwards"


def test_top_level_forwards_alias_is_the_synthetic_package_member() -> None:
    """验证顶层 `forwards` 与合成包成员是同一模块对象。"""

    assert importlib.import_module(CANONICAL_PREFIX) is sys.modules["forwards"]
    assert sys.modules["forwards"] is importlib.import_module("forwards")


def test_forwards_submodules_keep_single_module_identity() -> None:
    """验证子模块的两种名字不各自加载出第二份模块。"""

    requested = importlib.import_module("forwards.request.core.messages")
    assert requested is sys.modules[f"{CANONICAL_PREFIX}.request.core.messages"]
    assert requested.ForwardMessageParser is ForwardMessageParser
