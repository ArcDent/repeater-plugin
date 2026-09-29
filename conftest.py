"""让 pytest 能 import plugin.py（含包内相对导入）。

plugin.py 使用包相对导入（from .config / .reactions），必须以包成员的身份被加载；
仓库根目录虽是包（有 __init__.py），但目录名含连字符无法作为模块名被测试引用。
本 conftest 把根目录注册为合成包 repeater_plugin，再以该包成员身份加载
plugin，并给测试用的顶层名（plugin/config/reactions）建立别名，保证全程
单实例。仅影响 pytest 运行，MaiBot 运行时不会加载本文件。
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# plugin.py 顶部 import maibot_sdk，先装好 fake 再加载插件本体。
if "maibot_sdk" not in sys.modules:
    from tests import fake_maibot_sdk

    sys.modules["maibot_sdk"] = fake_maibot_sdk

PKG_NAME = "repeater_plugin"
if PKG_NAME not in sys.modules:
    import types

    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(ROOT)]
    pkg.__package__ = PKG_NAME
    sys.modules[PKG_NAME] = pkg

for name in ("reactions", "config", "plugin"):
    if name not in sys.modules:
        sys.modules[name] = importlib.import_module(f"{PKG_NAME}.{name}")
