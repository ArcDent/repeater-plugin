## REMOVED Requirements

### Requirement: 转发源消息的资格
**Reason**: 本插件完整退出转发域，MUST NOT 保留检测入口或转发副作用。
**Migration**: 由独立麦麦搬运喵处理；本插件收到合并转发只按剩余通用处理链放行。

#### Scenario: 转发消息不触发旧域
- **WHEN** 本插件收到合并转发
- **THEN** 不进行转发资格检查、源群提示或转发任务创建

### Requirement: 自动转发的目标群独立掷骰
**Reason**: 自动转发概率行为删除，MUST NOT 留下权重决策或兼容调用。
**Migration**: 新插件由源群 Maisaka 决定分享，不继承旧权重。

#### Scenario: 旧权重不再生效
- **WHEN** 遗留配置包含转发权重
- **THEN** 本插件不执行任何转发掷骰

### Requirement: 自动转发的防重与冷却
**Reason**: 本插件不再转发，MUST NOT 创建转发防重或源群、目标群冷却状态。
**Migration**: 新插件维护自己的持久投递记录；旧运行时数据不自动清除或盲目导入。

#### Scenario: 生命周期无转发状态
- **WHEN** 本插件加载、热更新或卸载
- **THEN** 不读取、创建、刷新或持久化旧转发状态

### Requirement: 自动转发的投递
**Reason**: MUST 完整删除协议直发、节点重建、群列表校验和投递任务。
**Migration**: 需要转发的部署使用麦麦搬运喵；不在本插件提供转发代理。

#### Scenario: 无自动发送
- **WHEN** 本插件处理原本可命中转发的消息
- **THEN** 不调用转发发送接口，不创建转发延迟任务

### Requirement: 转发内容注入
**Reason**: 本插件 MUST NOT 追加转发内容上下文。
**Migration**: 新插件使用主程序正常发送记录，不移植转发注入。

#### Scenario: 无转发注入
- **WHEN** 群内出现合并转发
- **THEN** 本插件不产生 source_kind 为 forward 的注入，复读和表情回应注入不受影响

### Requirement: 转发留痕注入
**Reason**: 本插件 MUST NOT 产生跨群投递留痕。
**Migration**: 新插件工具返回真实结果，不复制旧 forward_action 注入。

#### Scenario: 无源群留痕
- **WHEN** 新插件完成跨群发送
- **THEN** 本插件不监听或注入 forward_action 留痕

### Requirement: 强制发言
**Reason**: 转发后的目标群主动任务 MUST 从本插件删除。
**Migration**: 新插件只发送工具自带的可选评论，固定间隔 0.5 秒，不由旧插件补充评论。

#### Scenario: 无目标群触发
- **WHEN** 本插件运行或收到转发相关消息
- **THEN** 不因转发调用目标群 Planner 主动任务

### Requirement: 请求转发链路
**Reason**: MUST 删除 request_cross_group_forward、decide_forward_delivery 和相关 Planner Hook、查看资格、授权及续轮编排。
**Migration**: 新插件使用 forward_message(message_id, target_group_id, comment)；旧名称无兼容别名。

#### Scenario: 不暴露旧工具
- **WHEN** 主程序收集本插件组件，或旧 Planner 尝试调用旧工具
- **THEN** 本插件不注册旧转发工具及 Hook，不执行转发；失效工具由宿主正常报告不可用

### Requirement: 已投递状态的两路共用
**Reason**: 本插件的两条转发路径均退役，MUST NOT 维护共享投递状态。
**Migration**: 新插件自行持久化；跨项目状态迁移需单独核对来源与目标，不以旧内存状态充当持久事实。

#### Scenario: 无共享依赖
- **WHEN** 复读插件运行而新插件未安装
- **THEN** 正常完成复读与表情回应，不依赖任何转发记录或模块

### Requirement: 转发域开关与配置
**Reason**: MUST 删除配置模型、默认 TOML、WebUI Schema 和转发专属能力声明，不保留永久关闭的兼容壳。
**Migration**: 用户在新插件单独填写白名单；旧 forward 配置不得影响剩余两域，不自动复制运行配置或凭证。

#### Scenario: 默认配置没有转发域
- **WHEN** 生成本插件默认配置与组件 Schema
- **THEN** 不出现 forward 配置段或转发选项

#### Scenario: 旧配置升级
- **WHEN** 旧配置含 forward 字段并经升级规范化
- **THEN** 转发字段不进入有效配置、不恢复工具，剩余复读和表情回应设置保留

### Requirement: 自动转发的富摘要占位观测
**Reason**: MUST 删除转发摘要轮询及专属占位日志，不保留闲置观测任务。
**Migration**: 历史日志和 ADR 可保留查阅；现行功能和说明不再宣称本插件处理转发。

#### Scenario: 无摘要后台任务
- **WHEN** 收到带媒体的合并转发
- **THEN** 本插件不为转发查询摘要、统计占位或记录自动转发专属日志
