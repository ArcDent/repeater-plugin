"""请求转发子包的依赖方向与 SDK 边界回归测试（原插件 test_architecture.py 的移植）。

原文件校验 ``core`` / ``source`` / ``target`` 的单向依赖边界；本文件把包根改为
`forwards/request`，并追加本插件特有的边界：请求转发子包不得依赖
``maibot_sdk``（该包在 MaiBot 容器外不可导入，且本插件的纯逻辑层约定不依赖
SDK；见 change task 3.1 与 design D1）。
"""

from __future__ import annotations

import ast
from pathlib import Path

REQUEST_ROOT = Path(__file__).resolve().parents[3] / "forwards" / "request"
PACKAGE_PREFIX = "forwards.request."


def _resolve_import(module_name: str, node: ast.ImportFrom) -> str:
    """把相对 ``from`` 导入解析为包内绝对模块名。

    Args:
        module_name: 当前源码文件对应的完整 Python 模块名。
        node: AST 中待解析的 ``ImportFrom`` 节点。

    Returns:
        可用于判断领域边界的绝对模块名；无法解析的空模块返回空字符串。
    """

    imported_module = node.module or ""
    if node.level == 0:
        return imported_module
    current_parts = module_name.split(".")
    base_parts = current_parts[: -node.level]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ".".join(base_parts)


def _iter_source_files() -> list[Path]:
    """返回请求转发子包中的全部生产源码文件。

    Returns:
        按路径排序的 ``*.py`` 文件列表。
    """

    return sorted(REQUEST_ROOT.rglob("*.py"))


def test_domain_packages_follow_one_way_import_boundaries() -> None:
    """验证 core、source 与 target 遵守单向依赖边界。

    ``core`` 不得依赖外层领域或应用服务，``source`` 与 ``target`` 不得互相
    导入，两个领域也不得反向依赖 ``request``、``runtime`` 或调试统计。
    该测试防止后续功能把跨域协调重新下沉，导致循环依赖和目录再次扁平化。
    """

    forbidden_roots = {
        "core": {"source", "target", "request", "runtime", "debug_stats"},
        "source": {"target", "request", "runtime", "debug_stats"},
        "target": {"source", "request", "runtime", "debug_stats"},
    }

    violations: list[str] = []
    for domain, forbidden in forbidden_roots.items():
        for source_path in sorted((REQUEST_ROOT / domain).glob("*.py")):
            module_name = f"{PACKAGE_PREFIX}{domain}.{source_path.stem}"
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            imported_modules = [
                _resolve_import(module_name, node) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            ]
            imported_modules.extend(
                alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
            )
            for imported_module in imported_modules:
                if not imported_module.startswith(PACKAGE_PREFIX):
                    continue
                imported_root = imported_module.removeprefix(PACKAGE_PREFIX).split(".", 1)[0]
                if imported_root in forbidden:
                    violations.append(f"{source_path.relative_to(REQUEST_ROOT)} -> {imported_module}")

    assert violations == []


def test_request_package_never_imports_maibot_sdk() -> None:
    """验证请求转发子包零 `maibot_sdk` 依赖。

    真实 SDK 只存在于 MaiBot 容器内，本机 `uv run python -c "import
    forwards.request"` 必须成功；运行时代理改为读取回调传入的本插件配置视图，
    不再导入任何 SDK 类型。该测试防止后续改动把配置模型或 SDK 类型重新引入。
    """

    violations: list[str] = []
    for source_path in _iter_source_files():
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            if any(name == "maibot_sdk" or name.startswith("maibot_sdk.") for name in imported):
                violations.append(str(source_path.relative_to(REQUEST_ROOT)))

    assert violations == []
