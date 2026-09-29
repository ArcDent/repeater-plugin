"""配置模型测试：默认值，以及 config.toml 与 config.py 不漂移。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from config import RepeaterConfig

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

CONFIG_TOML = Path(__file__).resolve().parent.parent / "config.toml"
MANIFEST_JSON = Path(__file__).resolve().parent.parent / "_manifest.json"

# The three host capabilities consumed by the context-injection path.
# The fake SDK cannot simulate the authorization layer, so we assert the
# manifest content instead (see openspec change fix-context-injection, D1).
REQUIRED_CAPABILITIES = {
    "maisaka.context.append",
    "chat.get_stream_by_group_id",
    "config.get",
}


def test_default_probabilities():
    config = RepeaterConfig()
    assert config.plugin.enabled is True
    assert config.bandwagon.probability == 0.25
    assert config.bandwagon.self_message_probability == 0.12
    assert config.opposite.enabled is True
    assert config.opposite.probability == 0.35
    assert config.opposite.include_defaults is True


def test_self_message_probability_not_higher_than_others():
    """给麦麦自己的消息跟风应更保守，否则观感像自吹。"""
    config = RepeaterConfig()
    assert config.bandwagon.self_message_probability <= config.bandwagon.probability


def test_default_webhook_port_avoids_friend_request_plugin():
    config = RepeaterConfig()
    assert config.webhook.port == 18081
    assert config.webhook.path.startswith("/")


def test_delay_window_is_ordered():
    config = RepeaterConfig()
    assert config.behavior.delay_min_seconds <= config.behavior.delay_max_seconds


def test_list_fields_are_independent_per_instance():
    first, second = RepeaterConfig(), RepeaterConfig()
    first.scope.group_blacklist.append("10001")
    assert second.scope.group_blacklist == []


def test_all_sections_present():
    config = RepeaterConfig()
    for section in (
        "plugin",
        "webhook",
        "protocol",
        "bandwagon",
        "opposite",
        "repeat",
        "behavior",
        "context",
        "scope",
    ):
        assert getattr(config, section) is not None


def test_context_section_defaults():
    cfg = RepeaterConfig()
    assert cfg.context.enabled is False
    assert cfg.context.dedup_ttl_seconds == 3600
    assert cfg.context.merge_window_seconds == 30
    assert cfg.context.max_reactions_per_message == 10
    assert cfg.context.self_display_name == ""


def test_context_section_accepts_overrides():
    cfg = RepeaterConfig(context={"enabled": True, "dedup_ttl_seconds": 120,
                                       "merge_window_seconds": 5,
                                       "max_reactions_per_message": 3,
                                       "self_display_name": "麦麦"})
    assert cfg.context.enabled is True
    assert cfg.context.dedup_ttl_seconds == 120
    assert cfg.context.merge_window_seconds == 5
    assert cfg.context.max_reactions_per_message == 3
    assert cfg.context.self_display_name == "麦麦"


def test_config_version_updated_to_1_5_0():
    """config_version 是配置结构版本：删除整个跨群转发域时从 1.4.0 升到 1.5.0。"""
    cfg = RepeaterConfig()
    assert cfg.plugin.config_version == "1.5.0"


def test_repeat_section_defaults():
    """repeat 节默认值与 design D2 表逐项一致（独立字面量，不引自实现）。"""
    cfg = RepeaterConfig()
    assert cfg.repeat.enabled is True
    assert cfg.repeat.window_seconds == 120
    assert cfg.repeat.threshold == 3
    assert cfg.repeat.distinct_senders is True
    assert cfg.repeat.weight_repeat == 85
    assert cfg.repeat.weight_interrupt == 15
    assert cfg.repeat.weight_silence == 0
    assert cfg.repeat.delay_min_seconds == 0.3
    assert cfg.repeat.delay_max_seconds == 1.5
    assert cfg.repeat.interrupt_texts == ["打断！", "够了！", "停！", "打住！", "别念了！"]
    assert cfg.repeat.chain_ttl_seconds == 1800
    assert cfg.repeat.cooldown_seconds == 120
    assert cfg.repeat.max_text_length == 200
    assert cfg.repeat.max_chains == 2000


def test_repeat_delay_window_is_ordered():
    """复读发送延迟的区间语义与 [behavior] 延迟一样：最小不超过最大。"""
    cfg = RepeaterConfig()
    assert cfg.repeat.delay_min_seconds <= cfg.repeat.delay_max_seconds


def test_repeat_interrupt_texts_independent_per_instance():
    """打断文本池是可变列表，两个实例不得共享同一份默认值。"""
    first, second = RepeaterConfig(), RepeaterConfig()
    first.repeat.interrupt_texts.append("自定义")
    assert second.repeat.interrupt_texts == ["打断！", "够了！", "停！", "打住！", "别念了！"]


def test_scope_ignore_senders_defaults_empty():
    """scope.ignore_senders 默认空列表，与 ignore_reactors 并列。"""
    cfg = RepeaterConfig()
    assert cfg.scope.ignore_senders == []


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 的 tomllib")
def test_config_toml_repeat_defaults_match_design():
    """[repeat] 节与 [plugin].config_version 的 toml 默认值与 design D2 表逐项一致（独立字面量）；scope.ignore_senders 的默认空列表一并在此钉死（与 config.py 默认同口径，防 toml 侧漂移）。"""
    with CONFIG_TOML.open("rb") as fp:
        raw = tomllib.load(fp)
    repeat = raw.get("repeat")
    assert isinstance(repeat, dict), "config.toml 缺少 [repeat] 节"
    assert raw["plugin"]["config_version"] == "1.5.0"
    assert repeat["enabled"] is True
    assert repeat["window_seconds"] == 120
    assert repeat["threshold"] == 3
    assert repeat["distinct_senders"] is True
    assert repeat["weight_repeat"] == 85
    assert repeat["weight_interrupt"] == 15
    assert repeat["weight_silence"] == 0
    assert repeat["delay_min_seconds"] == 0.3
    assert repeat["delay_max_seconds"] == 1.5
    assert repeat["interrupt_texts"] == ["打断！", "够了！", "停！", "打住！", "别念了！"]
    assert repeat["chain_ttl_seconds"] == 1800
    assert repeat["cooldown_seconds"] == 120
    assert repeat["max_text_length"] == 200
    assert repeat["max_chains"] == 2000
    assert raw["scope"]["ignore_senders"] == []


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 的 tomllib")
def test_config_toml_matches_model_defaults():
    assert tomllib is not None
    with CONFIG_TOML.open("rb") as fp:
        raw = tomllib.load(fp)
    config = RepeaterConfig()

    for section_name, entries in raw.items():
        section = getattr(config, section_name, None)
        assert section is not None, f"config.toml 里的 [{section_name}] 在 config.py 中不存在"
        for key, toml_value in entries.items():
            model_value = getattr(section, key, "__missing__")
            assert model_value != "__missing__", f"config.toml 里的 {section_name}.{key} 在 config.py 中不存在"
            assert model_value == toml_value, (
                f"{section_name}.{key} 默认值漂移: config.py={model_value!r} config.toml={toml_value!r}"
            )


LEGACY_FORWARD_TOML = """
# 旧版 config.toml 残留：v0.8.0 的整段 [forward]（含两个已删字段）与自定义复读设置。
[forward]
enabled = true
mode = "direct"
auto_enabled = true
request_enabled = true
source_groups = ["10001"]
target_groups = ["20001"]
weight_send = 30
weight_silence = 70
dedup_ttl_seconds = 1800
cooldown_seconds = 0
target_cooldown_seconds = 0
delay_min_seconds = 0.5
delay_max_seconds = 2.0
trigger_source_planner = false
trigger_target_planner = true
view_failure_fallback_threshold = 5
inject_max_chars = 600
summary_timeout_seconds = 10

[repeat]
window_seconds = 90
weight_repeat = 90
weight_interrupt = 10
weight_silence = 0
"""


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 的 tomllib")
def test_legacy_config_residual_forward_section_loads_without_forward_domain():
    """旧配置整段 [forward] 残留时：加载成功、转发域不进有效配置、剩余域值保留。

    替身与真实 SDK 同口径（pydantic 默认 ``extra="ignore"``）：未知键直接丢弃，
    不落成实例属性。本用例据此把「残留被忽略」钉在实例面（``hasattr`` 为假）
    与剩余域值上；声明面（模型字段集合不含 forward）由
    test_forward_retirement.py 的配置面断言覆盖。
    """
    assert tomllib is not None
    legacy_cfg = RepeaterConfig(**tomllib.loads(LEGACY_FORWARD_TOML))

    assert not hasattr(legacy_cfg, "forward")
    assert legacy_cfg.repeat.window_seconds == 90
    assert legacy_cfg.repeat.weight_repeat == 90
    assert legacy_cfg.repeat.weight_interrupt == 10
    assert legacy_cfg.plugin.config_version == "1.5.0"


def test_manifest_version_matches_changelog_head():
    """manifest version 与最新 CHANGELOG 条目标题一致（0.9.0：完整移除跨群转发域）。

    仓库没有独立的插件版本常量，manifest 的 version 与 CHANGELOG 首条标题是
    两个独立来源，互为校验；写错任一侧都会红。本版 CHANGELOG 条目由第 5 组
    补写，补上之前本用例是已知红。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    assert manifest["version"] == "0.9.0"
    changelog = (MANIFEST_JSON.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    head = re.search(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)
    assert head is not None, "CHANGELOG.md 缺少 '## [x.y.z]' 形式的版本条目"
    assert head.group(1) == manifest["version"]


def test_manifest_declares_injection_capabilities():
    """The manifest must declare every capability the injection path consumes.

    注入能力用包含断言：repeat-and-rebrand 起 capabilities 还声明了复读观测的
    chat.receive.before_process（完整集合由 test_plugin.py 的源码派生断言与
    test_forward_retirement.py 的整集合等值断言钉死，这里不重复计数）。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    capabilities = manifest.get("capabilities")
    assert isinstance(capabilities, list), "_manifest.json 缺少 capabilities 数组"
    assert REQUIRED_CAPABILITIES <= set(capabilities), (
        f"manifest capabilities 漂移: 缺 {REQUIRED_CAPABILITIES - set(capabilities)}"
    )
