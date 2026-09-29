# 实施约束与依赖

本清单仅负责 `repeat-plugin` 的 `remove-cross-group-forward`，不得修改同级新插件项目。章节是阶段四执行组，按 1 → 2 → 3 → 4 → 5 → 6 → 7 串行推进；组内可有暂态导入失败，但不得将暂态当作组验收通过。所有任务仍未实施。

第 1 组只建立基线；第 2～4 组完成运行代码、配置与测试退役；第 5～6 组收敛文档并验证；第 7 组的主规格同步须在实现验收后获得明确授权。没有同步授权时保持相应任务未勾选，交接注明待同步，不自动归档或部署。全程禁止 Git 写操作，不删除线上状态数据。

`seam` 表示公开接口或可重复执行的检查入口；删除私有函数不单独成为测试落点。同组共用落点写在组首，各任务仍列出具体断言。

## 1. 固定基线与删除清单

- [x] 1.1 记录用户既有改动和受保护文件
  - seam：`git status --short`、`git diff -- tests/test_plugin_forward.py`、ADR 0013 与历史归档的文件内容。
  - 验收：在本任务的执行记录中保留原测试差异及文件指纹/清单；准确区分执行前已有修改与本次删除，不 reset、stash 或覆盖用户草案；记录可用于收尾核对。
- [x] 1.2 列出全部转发公开组件及生命周期入口
  - seam：`create_plugin()` 返回实例的 `get_components()`、`on_load()`、`on_config_update()`、`on_unload()`。
  - 验收：清单包含两个旧 Tool、forward_detector、forward_view_result_capture、forward_request_authorize、forward_auto_view_orchestrator 及生命周期对转发的调用；覆盖摘要轮询、决策票、群列表刷新和转发持久状态，不只照抄主规格。
- [x] 1.3 建立共享依赖与测试保留清单并运行基线
  - seam：manifest 能力声明、`RepeaterConfig` 导出、pytest 收集入口与生产 pyright 入口。
  - 验收：每项候选删除能力标注剩余消费者或无消费者；明确保留 aiohttp、复读/表情回应使用的上下文与协议设施及非候选消息样例；记录基线测试/类型检查结果，已有红测单列而非伪称基线全绿；旧 forward-by-id-and-runtime-consolidation 不进入实施范围。

## 2. 移除转发组件与运行时

本组共用 seam：`create_plugin()` / `get_components()` 与三个生命周期方法；测试从宿主可见接口观察行为，不为已删除的私有转发方法建立新测试。

- [x] 2.1 删除旧转发 Tool 和 Planner Hook 声明及实现
  - seam：`get_components()` 的完整注册清单。
  - 验收：清单不含 request_cross_group_forward、decide_forward_delivery 及三个转发 Planner Hook；无同义别名、代理或 disabled 兼容壳；若 get_components 覆写仅服务旧 Tool，随之删除，剩余组件声明不变。
- [x] 2.2 删除自动检测与全部转发发送编排
  - seam：`get_components()` 和宿主派发到剩余 `chat.receive.before_process` handler 的消息路径。
  - 验收：forward_detector 不再注册；删除摘要等待、掷骰、目标决策、延迟、节点重建、目标可用性刷新、内容/留痕注入和强制发言分支；合并转发不造成任何转发发送或 Planner 主动任务。
- [x] 2.3 删除转发生命周期和后台状态
  - seam：`on_load()` → `on_config_update()` → `on_unload()`，以记录型 Host 替身及临时数据目录观察。
  - 验收：上述调用不加载/写入旧转发账本、不创建轮询或转发任务、不输出转发启动自检；预放入的旧状态文件内容不变；仍正确启停表情回应聚合任务和 HTTP 资源。
- [x] 2.4 删除 forwards 整包并清理无消费者代码
  - seam：插件包导入、`create_plugin()` 及生产模块依赖扫描。
  - 验收：forwards 目录及对其 import 全部消失；仅安装本插件即可导入创建实例，不依赖新插件；共享协议调用、上下文追加、日志预览和群流解析按第 1 组消费者清单保留。后续配置/测试清理引起的暂态失败明确记录，最终由第 4 组关闭。

## 3. 移除配置和多余能力

本组共用 seam：`RepeaterConfig` 的模型校验、序列化与 JSON Schema，默认 TOML 解析，以及 `_manifest.json` 的公开能力声明。

- [x] 3.1 删除转发配置模型、默认值与 WebUI 字段
  - seam：`RepeaterConfig`、配置模块公开导出、`config.toml`。
  - 验收：无 ForwardSection 导出、无有效 forward 字段、无 [forward] 默认段、无转发 UI 选项；剩余配置字段与默认 TOML 对齐，不能保留空段或强制 false 的转发开关。
- [x] 3.2 验证旧配置升级且保留剩余设置
  - seam：插件经 SDK 支持的配置规范化入口及 `on_config_update()`；测试复用已有配置替身或真实模型，不新增私有迁移入口。
  - 验收：输入含 forward.enabled=true、旧权重和目标群的旧配置，输出有效配置无转发域、组件不恢复；自定义 repeat、bandwagon、opposite、context、scope、protocol、webhook 值不被重置；SDK 契约不明确时先核实，不凭空假定升级 API。
- [x] 3.3 清理 manifest、类型检查范围与版本声明
  - seam：manifest capabilities/description、`pyrightconfig.json` 和配置版本公开值。
  - 验收：删除无剩余消费者的七项转发候选能力；保留 maisaka.context.append、chat.get_stream_by_group_id、config.get、chat.receive.before_process 及 aiohttp；类型检查不再指向 forwards；发布版本和配置版本的对应声明及测试一致。

## 4. 退役专属测试并补齐回归证明

共用 seam：`pytest tests/`；优先复用 tests/test_plugin.py、tests/test_config.py 的公开入口测试设施。新增断言不得通过调用已经删除的私有方法来证明退役。

- [x] 4.1 删除专属测试并最小清理共享替身
  - seam：pytest 测试收集与 tests/fake_maibot_sdk.py 的现有公共替身接口。
  - 验收：tests/test_plugin_forward.py、tests/test_forwards_*.py、tests/forwards/** 及专属 fixture 被退役；混合文件只删无消费者部分；原用户测试差异的去向可对照第 1 组记录；剩余测试可正常收集，保留复读排除 forward 段的输入样例。
- [x] 4.2 添加组件、配置和能力退役断言
  - seam：`get_components()`、`RepeaterConfig` 序列化/Schema、manifest JSON。
  - 验收：明确断言旧 Tool/Hook/forward 字段/专属能力不存在，并正向断言复读检测与通知 guard 仍注册、共享能力仍授权；旧配置样例不会复活转发。
- [x] 4.3 添加消息路径无转发副作用断言
  - seam：通过组件元数据派发剩余 Hook，观察 Host 的消息查询、发送、上下文追加及主动任务调用记录。
  - 验收：普通合并转发、重复上报、未展开转发、机器人自发转发均不产生摘要查询、群列表刷新、转发发送、源/目标 Planner 触发、forward/forward_action 注入；在正常放行场景结果为 continue；仍保持通知 guard 既有语义。
- [x] 4.4 验证复读与表情回应行为未变
  - seam：现有复读 Hook、表情回应 webhook 及 pytest 的两域行为测试。
  - 验收：复读三出口互斥、同步终结、独立冷却、协议直发与并发注入延迟测试通过；表情槽、反义/跟风互斥、Unicode 归一、聚合 flush 和失败回滚测试通过；负向门禁测试使用确定性权重/概率，不靠随机沉默蒙对。

## 5. 收敛现行说明与规则

- [x] 5.1 更新当前功能、配置和目录说明为两域
  - seam：README、CONTEXT、AGENTS、HANDOFF 当前章节及 manifest description。
  - 验收：不再宣称本插件提供转发、转发配置、工具或后台服务；移除现行转发术语和规则，仅保留简短迁移提示；AGENTS 仍保留复读与表情回应关键约束，CLAUDE 是指向 AGENTS.md 的软链接。
- [x] 5.2 补充破坏性移除发布说明并保护历史
  - seam：CHANGELOG 新条目、历史 ADR、openspec/changes/archive 与受保护草案。
  - 验收：新条目说明转发已完整移除且需独立插件；历史条目/归档/ADR 0013 与基线对照无改写；旧冲突草案仅在当前交接中标注不实施，不删除或覆盖。
- [x] 5.3 输出精确残留检查结果
  - seam：对生产代码、配置、测试、当前文档分别执行内容与依赖扫描。
  - 验收：旧模块 import、工具/Hook 声明、转发配置、专属状态/日志/发送调用为零；每项剩余 forward 命中能归入历史记录、退役断言、迁移提示或复读非候选样例；禁止通过全仓删关键词破坏历史与边界测试。

## 6. 验证本项目并交接切换条件

- [x] 6.1 执行本项目完整自动化验证
  - seam：`uv run --with pytest --with aiohttp pytest tests/ -q`、`uv run --with pyright --with aiohttp pyright`、`openspec validate remove-cross-group-forward --strict`。
  - 验收：全部保留/新增测试通过、生产类型检查零错误、变更严格校验通过；记录各命令退出码与结果，对照基线说明退役测试清单，不以减少测试数充当成功；安装临时依赖前告知用户。
- [x] 6.2 核对工作树修改范围和原改动去向
  - seam：`git diff --stat`、`git diff`、`git status --short` 与第 1 组基线记录。
  - 验收：变化均属于本次本地转发退役；用户原测试差异有明确退役/保留说明；无新插件项目修改、无主规格提前同步、无 Git 写操作或线上数据删除。
- [x] 6.3 编写本项目切换与回滚交接
  - seam：本项目 HANDOFF 的操作清单与本地验证报告。
  - 验收：写明部署前须取得新插件验收证据、先停旧转发再开新插件、回滚先停新插件；新插件结果作为外部交接输入，未提供时标记部署条件未满足，不代验收或修改对方；旧数据不自动迁移/删除，真实群消息测试和部署均不自动执行。

## 7. 获授权后同步主规格

本组依赖第 6 组通过和用户明确同步授权。没有授权不得执行；不因 CLI 显示规划完成就自动改主规格。

- [x] 7.1 同步本变更的全部 REMOVED 需求
  - seam：按 `/openspec-sync-specs` 获取的本 change 工件路径与本项目主规格。
  - 验收：同步前重读指引和 delta；cross-group-forward 的 11 条原需求全部移除，空 capability 删除；context-injection、message-repeat 内容与同步前一致；不触碰新项目、不自动归档其他草案。
- [x] 7.2 校验同步结果并完成本地交接
  - seam：本项目 OpenSpec 规格校验、工作树差异与 HANDOFF。
  - 验收：同步后现行主规格仅保留本项目仍支持的行为且校验通过；明确代码验收、规格同步和部署状态各自结果；提示用户自行提交，不执行 add/commit/push。
