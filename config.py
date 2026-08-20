"""表情跟风插件 — 配置模型。"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={
            "hint": "插件总开关，关闭后不再监听表情回应，也不再贴任何表情。",
            "label": "启用插件",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="1.0.0",
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
                "协议端 OneBot HTTP 地址，如 http://maim-bot-snowluma:3000。"
                "贴表情动作 set_msg_emoji_like 直调该地址，不填则插件不做任何动作。"
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
            "hint": "有人给他人消息贴表情时，麦麦跟着贴同款表情的概率。0 = 从不跟风。",
            "label": "跟风概率（他人消息）",
            "order": 0,
            "step": 0.05,
        },
    )
    self_message_probability: float = Field(
        default=0.12, ge=0.0, le=1.0,
        json_schema_extra={
            "hint": (
                "有人给麦麦自己发的消息贴表情时，麦麦跟着贴同款的概率。"
                "建议低于他人消息的概率，自己给自己的消息点赞观感较差。"
            ),
            "label": "跟风概率（麦麦自己的消息）",
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
            "hint": (
                "触发表情命中反义映射时，麦麦贴出相反表情的概率。"
                "命中反义映射的表情不会再跟风，掷不中就什么都不做。"
            ),
            "label": "反义回应概率",
            "order": 1,
            "step": 0.05,
        },
    )
    include_defaults: bool = Field(
        default=True,
        json_schema_extra={
            "hint": "是否加载内置反义映射（👍↔👎、❤↔💔、😂↔😭、😄↔😡、✅↔❌、🔥↔❄、⬆↔⬇、😍↔🤢）。",
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


class BehaviorSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "节奏与限流"
    __ui_order__: ClassVar[int] = 5

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


class ScopeSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "生效范围"
    __ui_order__: ClassVar[int] = 6

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


class SendingEmojisConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    webhook: WebhookSection = Field(default_factory=WebhookSection)
    protocol: ProtocolSection = Field(default_factory=ProtocolSection)
    bandwagon: BandwagonSection = Field(default_factory=BandwagonSection)
    opposite: OppositeSection = Field(default_factory=OppositeSection)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)