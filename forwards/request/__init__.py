"""请求转发链路（原 ForwardMessagesAuto 插件整包移植）。

对外入口：

- `ForwardingRuntime`：一次插件生命周期内共享的运行时（装配全部服务、
  生命周期、Planner Hook 编排与 Tool 委托），由 `plugin.py` 持有；
- `RequestService`：`forwards.request.request.RequestService`，请求入口
  `create` 负责校验、**已投递状态**拒重与真实顺序投递；
- `ForwardRuntimeConfig` / `adapt_forward_config`：本插件 `ForwardSection`
  的配置适配（请求链路不读旧配置字段）；
- `build_delivered_key`：与**自动转发**共用的**已投递状态**键；
- `FORWARD_TOOL_NAME` / `FORWARD_AUTHORIZATION_ROUND_KWARG` /
  `append_user_reminder`：`plugin.py` 声明 Tool 与 Hook 时需要的常量与
  载荷辅助函数。

本包不依赖 `maibot_sdk`，也不引用 `forwards` 之外的插件模块：既可被
`plugin.py` 以插件包成员身份导入，也支持顶层 `import forwards.request`。
"""

from __future__ import annotations

from .config import ForwardRuntimeConfig, GroupIdList, adapt_forward_config
from .request import RequestService
from .runtime import ForwardingRuntime, read_plugin_version
from .shared_state import build_delivered_key
from .source.authorization import (
    FORWARD_AUTHORIZATION_ROUND_KWARG,
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
    ForwardInvocationGate,
)
from .source.history import VIEW_FORWARD_TOOL_NAME
from .source.output_items import append_user_reminder

__all__ = [
    "FORWARD_AUTHORIZATION_ROUND_KWARG",
    "FORWARD_CONTEXT_TOKEN_ARGUMENT",
    "FORWARD_TOOL_NAME",
    "VIEW_FORWARD_TOOL_NAME",
    "ForwardInvocationGate",
    "ForwardRuntimeConfig",
    "ForwardingRuntime",
    "GroupIdList",
    "RequestService",
    "adapt_forward_config",
    "append_user_reminder",
    "build_delivered_key",
    "read_plugin_version",
]
