"""跨群转发域（design D1）：自动转发与请求转发两条路径共用的纯逻辑与运行时。

模块划分：

- `nodes`：转发节点解析与 OneBot node 转换（纯逻辑，可单测）
- `source`：转发源消息的资格解析与快照（纯逻辑，可单测）
- `auto`：逐目标群掷骰与决策（纯逻辑，可单测）
- `state`：防重 / 冷却 / 已投递登记 / 群可用性缓存（纯逻辑，全同步）
- `texts`：转发内容注入、行为留痕与 Planner intent 文案（纯逻辑，可单测）
- `request`：原 ForwardMessagesAuto 插件链路的整包移植（请求转发运行时与 Tool）

本包不依赖 `maibot_sdk`；Hook/Tool 声明与编排留在 `plugin.py`。
"""

from __future__ import annotations
