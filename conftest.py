"""让 pytest 能 import plugin.py（含包内相对导入）。

plugin.py 使用包相对导入（from .config / .reactions），必须以包成员的身份被加载；
仓库根目录虽是包（有 __init__.py），但目录名含连字符无法作为模块名被测试引用。
本 conftest 把根目录注册为合成包 sending_emojis_plugin，再以该包成员身份加载
plugin，并给测试用的顶层名（plugin/config/reactions/forwards）建立别名，保证全程
单实例。仅影响 pytest 运行，MaiBot 运行时不会加载本文件。
"""

import importlib
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# plugin.py 顶部 import maibot_sdk，先装好 fake 再加载插件本体。
if "maibot_sdk" not in sys.modules:
    from tests import fake_maibot_sdk

    sys.modules["maibot_sdk"] = fake_maibot_sdk

PKG_NAME = "sending_emojis_plugin"
if PKG_NAME not in sys.modules:
    import types

    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(ROOT)]
    pkg.__package__ = PKG_NAME
    sys.modules[PKG_NAME] = pkg

for name in ("reactions", "config", "plugin", "forwards"):
    if name not in sys.modules:
        sys.modules[name] = importlib.import_module(f"{PKG_NAME}.{name}")

# `forwards` 子树的两种名字必须指向同一批模块对象：测试统一走
# `sending_emojis_plugin.forwards.*`，而 `import forwards.request`（task 3.1
# 验收）与历史写法仍要命中同一对象。整棵子树按别名注册一遍，避免各自加载出
# 第二份模块——双身份会让 isinstance、单例状态与 monkeypatch 全部失效。
forwards_pkg = sys.modules["forwards"]
for module_info in pkgutil.walk_packages(forwards_pkg.__path__, prefix=f"{PKG_NAME}.forwards."):
    module = importlib.import_module(module_info.name)
    sys.modules.setdefault(module_info.name.removeprefix(f"{PKG_NAME}."), module)
