"""复读插件 — 配置模型。

覆盖复读与表情回应两个行为域。字段默认值须与 config.toml 逐项一致（项目铁律）。
"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

from .repeats import DEFAULT_INTERRUPT_TEXTS


class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={
            "hint": "插件总开关，关闭后复读与表情回应全部停用。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.5.0",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class WebhookSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "上报监听"
    __ui_order__: ClassVar[int] = 1

    host: str = Field(
        default="0.0.0.0",
        json_schema_extra={
            "hint": "监听协议端 HTTP 上报的地址。协议端与麦麦不同容器时需填 0.0.0.0。",
            "label": "监听地址",
            "order": 0,
            "placeholder": "0.0.0.0",
        },
    )
    port: int = Field(
        default=18081, ge=1, le=65535,
        json_schema_extra={
            "hint": "监听端口，需与协议端 HTTP 上报配置中的端口一致。默认 18081，避开好友申请插件的 18080。",
            "label": "监听端口",
            "order": 1,
            "step": 1,
        },
    )
    path: str = Field(
        default="/maibot/emoji_reaction",
        json_schema_extra={
            "hint": "HTTP 路径，协议端上报 URL 应填 http://host:port/path。",
            "label": "HTTP 路径",
            "order": 2,
            "placeholder": "/maibot/emoji_reaction",
        },
    )
    secret: str = Field(
        default="",
        json_schema_extra={
            "hint": "可选，对应协议端 HTTP 上报的 secret，留空则不校验签名。",
            "label": "Secret",
            "order": 3,
            "input_type": "password",
        },
    )


class ProtocolSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "协议端 HTTP 直调"
    __ui_order__: ClassVar[int] = 2

    base_url: str = Field(
        default="",
        json_schema_extra={
            "hint": (
                "协议端 OneBot HTTP 地址。不填时复读整体停用（不拦截、不发送），贴表情也无法执行。"
            ),
            "label": "OneBot HTTP 地址",
            "order": 0,
            "placeholder": "http://maim-bot-snowluma:3000",
        },
    )
    access_token: str = Field(
        default="",
        json_schema_extra={
            "hint": "协议端 OneBot HTTP 的 accessToken，留空则不带 Authorization 头。",
            "label": "Access Token",
            "order": 1,
            "input_type": "password",
        },
    )
    self_qq: str = Field(
        default="",
        json_schema_extra={
            "hint": (
                "麦麦自己的 QQ 号。留空则启动时用 get_login_info 自动获取；"
                "两者都取不到时仍可工作，只是无法识别「有人给麦麦自己的消息贴表情」。"
            ),
            "label": "麦麦 QQ 号",
            "order": 2,
            "placeholder": "留空自动获取",
        },
    )


class BandwagonSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "跟风"
    __ui_order__: ClassVar[int] = 3

    probability: float = Field(
        default=0.25, ge=0.0, le=1.0,
        json_schema_extra={
            "hint": "有人给他人消息贴表情时麦麦决定回应的概率，0 = 从不回应。",
            "label": "回应概率（他人消息）",
            "order": 0,
            "step": 0.05,
        },
    )
    self_message_probability: float = Field(
        default=0.12, ge=0.0, le=1.0,
        json_schema_extra={
            "hint": (
                "有人给麦麦自己的消息贴表情时的回应概率，建议低于他人消息的概率。"
            ),
            "label": "回应概率（麦麦自己的消息）",
            "order": 1,
            "step": 0.05,
        },
    )


class OppositeSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "反义回应"
    __ui_order__: ClassVar[int] = 4

    enabled: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "关闭后所有表情都只走跟风分支，不再唱反调。",
            "label": "启用反义回应",
            "order": 0,
        },
    )
    probability: float = Field(
        default=0.35, ge=0.0, le=1.0,
        json_schema_extra={
            "hint": "回应骰掷中后，命中反义映射的表情贴出相反表情的概率；掷不中贴同款，0 = 只贴同款。",
            "label": "反义回应概率",
            "order": 1,
            "step": 0.05,
        },
    )
    include_defaults: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "是否加载内置反义映射（16 对，见 docs/emoji-id-reference.md）。",
            "label": "使用内置反义映射",
            "order": 2,
        },
    )
    extra_pairs: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": (
                "追加或覆盖反义映射，每行一对，写法「A=B」。"
                "可填 emoji 字符（如 🎉=😢）或协议端表情编号（如 76=78）。"
                "映射双向生效；新对里出现过的表情会让旧对失效。"
            ),
            "label": "自定义反义对",
            "order": 3,
            "placeholder": "🎉=😢",
        },
    )
    disabled_emojis: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": "这些表情所在的反义对整对移除，使它们回落到跟风分支。填 emoji 字符或表情编号。",
            "label": "排除出反义映射的表情",
            "order": 4,
            "placeholder": "👍",
        },
    )


class RepeatSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "复读"
    __ui_order__: ClassVar[int] = 5

    enabled: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "复读独立开关：关闭后仅停用复读，表情回应跟风不受影响；插件总开关关闭时全部停用。",
            "label": "启用复读",
            "order": 0,
        },
    )
    window_seconds: int = Field(
        default=120, ge=1, le=3600,
        json_schema_extra={
            "hint": "滚动窗口：窗口内归一后相同的复读文本按口径累积，窗口外的历史消息失效。",
            "label": "滚动窗口（秒）",
            "order": 1,
            "step": 10,
        },
    )
    threshold: int = Field(
        default=3, ge=2, le=50,
        json_schema_extra={
            "hint": "窗口内计数达到该值时复读链达标，取得一次决策资格。",
            "label": "达标阈值",
            "order": 2,
            "step": 1,
        },
    )
    distinct_senders: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "统计口径：开启时仅计不同发送者人头，同一发送者连发不增计数；关闭后逐条消息计数。",
            "label": "仅计不同发送者",
            "order": 3,
        },
    )
    weight_repeat: int = Field(
        default=85, ge=0, le=1000,
        json_schema_extra={
            "hint": (
                "三个出口权重是一次掷骰的三个面（复读 / 打断 / 沉默），按比例恰好命中一个、严格互斥。"
                "默认 85 / 15 / 0 = 85% 复读、15% 打断、从不沉默；只有比例意义，同比例放大等价。"
            ),
            "label": "复读权重",
            "order": 4,
            "step": 5,
        },
    )
    weight_interrupt: int = Field(
        default=15, ge=0, le=1000,
        json_schema_extra={
            "hint": "打断出口的权重：命中则从打断文本池随机取一条发出，绝不附带复读文本。",
            "label": "打断权重",
            "order": 5,
            "step": 5,
        },
    )
    weight_silence: int = Field(
        default=0, ge=0, le=1000,
        json_schema_extra={
            "hint": "沉默出口的权重：命中则不出声，触发消息放行进主程序消息链。0 = 从不沉默。",
            "label": "沉默权重",
            "order": 6,
            "step": 5,
        },
    )
    interrupt_texts: list[str] = Field(
        default_factory=lambda: list(DEFAULT_INTERRUPT_TEXTS),
        json_schema_extra={
            "hint": "打断出口的变体池，每次随机取一条。全部留空时回落内置默认池并记日志，不会静默哑火。",
            "label": "打断文本池",
            "order": 7,
            "placeholder": "打断！",
        },
    )
    delay_min_seconds: float = Field(
        default=0.3, ge=0.0, le=300.0,
        json_schema_extra={
            "hint": "复读 / 打断发送前的最短等待秒数，与表情回应的延迟互相独立。",
            "label": "复读发送延迟下限（秒）",
            "order": 8,
            "step": 0.1,
        },
    )
    delay_max_seconds: float = Field(
        default=1.5, ge=0.0, le=300.0,
        json_schema_extra={
            "hint": "复读 / 打断发送前的最大等待秒数，实际等待在最小 / 最大之间随机取。",
            "label": "复读发送延迟上限（秒）",
            "order": 9,
            "step": 0.1,
        },
    )
    chain_ttl_seconds: int = Field(
        default=1800, ge=0, le=604800,
        json_schema_extra={
            "hint": "掷骰后同一复读文本在此时长内不再触发新一轮。0 = 不做链终结。",
            "label": "链终结保留时长（秒）",
            "order": 10,
            "step": 60,
        },
    )
    cooldown_seconds: int = Field(
        default=120, ge=0, le=86400,
        json_schema_extra={
            "hint": "复读专用的群冷却，与表情回应的群冷却互相独立。0 = 不限。",
            "label": "复读群冷却（秒）",
            "order": 11,
            "step": 5,
        },
    )
    max_text_length: int = Field(
        default=200, ge=0, le=4096,
        json_schema_extra={
            "hint": "超过此长度的文本消息不产出复读文本、不参与复读链累积。0 = 不限。",
            "label": "复读文本长度上限",
            "order": 12,
            "step": 50,
        },
    )
    max_chains: int = Field(
        default=2000, ge=100, le=100000,
        json_schema_extra={
            "hint": "内存中复读链记录的条数上界，超出后丢弃最旧的链，与表情槽条数上界同理。",
            "label": "复读链条数上界",
            "order": 13,
            "step": 100,
        },
    )


class BehaviorSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "节奏与限流"
    __ui_order__: ClassVar[int] = 6

    cooldown_seconds: int = Field(
        default=60, ge=0, le=86400,
        json_schema_extra={
            "hint": "同一个群内两次贴表情的最小间隔秒数，防止刷屏。0 = 不限。",
            "label": "群冷却（秒）",
            "order": 0,
            "step": 5,
        },
    )
    delay_min_seconds: float = Field(
        default=1.0, ge=0.0, le=300.0,
        json_schema_extra={
            "hint": "决定贴表情后的最小等待秒数，避免上报一到就瞬间贴上显得机械。",
            "label": "最小延迟（秒）",
            "order": 1,
            "step": 0.5,
        },
    )
    delay_max_seconds: float = Field(
        default=6.0, ge=0.0, le=300.0,
        json_schema_extra={
            "hint": "决定贴表情后的最大等待秒数。小于最小延迟时按最小延迟处理。",
            "label": "最大延迟（秒）",
            "order": 2,
            "step": 0.5,
        },
    )
    slot_ttl_seconds: int = Field(
        default=3600, ge=60, le=604800,
        json_schema_extra={
            "hint": "「这条消息麦麦已贴过表情」的记忆保留时长，超时后同一条消息可再贴一次。",
            "label": "表情槽保留时长（秒）",
            "order": 3,
            "step": 60,
        },
    )
    max_slots: int = Field(
        default=2000, ge=100, le=100000,
        json_schema_extra={
            "hint": "表情槽记忆的条数上界，超出后丢弃最旧的记录。",
            "label": "表情槽条数上界",
            "order": 4,
            "step": 100,
        },
    )


class ContextSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "上下文注入"
    __ui_order__: ClassVar[int] = 7

    enabled: bool = Field(
        default=False,
        json_schema_extra={
            "hint": "实验性开关：开启后表情回应去重、聚合后注入聊天历史，默认关闭。",
            "label": "启用上下文注入",
            "order": 0,
        },
    )
    dedup_ttl_seconds: int = Field(
        default=3600, ge=0, le=604800,
        json_schema_extra={
            "hint": "同一（回应者、被回应消息、表情）组合在窗口内只注入一次。0 = 不启用去重，重复上报不再被跳过。",
            "label": "去重窗口（秒）",
            "order": 1,
            "step": 60,
        },
    )
    merge_window_seconds: int = Field(
        default=30, ge=0, le=3600,
        json_schema_extra={
            "hint": (
                "同一被回应消息同一表情的多条回应在此窗口内汇入同一条注入消息，"
                "窗口到期后写入聊天历史。"
            ),
            "label": "聚合窗口（秒）",
            "order": 2,
            "step": 5,
        },
    )
    max_reactions_per_message: int = Field(
        default=10, ge=1, le=100,
        json_schema_extra={
            "hint": "单条被回应消息最多产生的注入消息条数，防表情回应风暴撑爆上下文。",
            "label": "注入上限（每条消息）",
            "order": 3,
            "step": 1,
        },
    )
    self_display_name: str = Field(
        default="",
        json_schema_extra={
            "hint": (
                "注入消息里麦麦自己的显示名。留空则自动取主程序配置 bot.nickname；"
                "两者都取不到时注入文本不写名字。"
            ),
            "label": "麦麦显示名（兜底）",
            "order": 4,
            "placeholder": "留空自动获取",
        },
    )


class ScopeSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "生效范围"
    __ui_order__: ClassVar[int] = 8

    group_whitelist: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": "只在这些群生效。留空 = 除黑名单外所有群都生效。",
            "label": "群白名单",
            "order": 0,
            "placeholder": "请输入群号",
        },
    )
    group_blacklist: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": "这些群一律不生效，优先于白名单。",
            "label": "群黑名单",
            "order": 1,
            "placeholder": "请输入群号",
        },
    )
    ignore_reactors: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": "忽略这些 QQ 贴出的表情回应，通常用来填其他机器人，避免两个 bot 互相跟风。",
            "label": "忽略的回应者",
            "order": 2,
            "placeholder": "请输入 QQ 号",
        },
    )
    ignore_senders: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "hint": (
                "忽略这些 QQ 发出的消息，其文本不进入复读链累积；"
                "与「忽略的回应者」（针对表情回应）是两个名单。通常填其他机器人，防止互相复读。"
            ),
            "label": "忽略的发送者",
            "order": 3,
            "placeholder": "请输入 QQ 号",
        },
    )


class RepeaterConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    webhook: WebhookSection = Field(default_factory=WebhookSection)
    protocol: ProtocolSection = Field(default_factory=ProtocolSection)
    bandwagon: BandwagonSection = Field(default_factory=BandwagonSection)
    opposite: OppositeSection = Field(default_factory=OppositeSection)
    repeat: RepeatSection = Field(default_factory=RepeatSection)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
    context: ContextSection = Field(default_factory=ContextSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
