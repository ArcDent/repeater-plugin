"""配置模型测试：默认值，以及 config.toml 与 config.py 不漂移。"""

from __future__ import annotations

import json
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
    for section in ("plugin", "webhook", "protocol", "bandwagon", "opposite", "repeat", "behavior", "context", "scope"):
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


def test_config_version_updated_to_1_1_0():
    """config_version 是配置结构版本：出口权重 + 复读自有延迟引入时从 1.0.0 升到 1.1.0。"""
    cfg = RepeaterConfig()
    assert cfg.plugin.config_version == "1.1.0"


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
    assert raw["plugin"]["config_version"] == "1.1.0"
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


def test_manifest_declares_injection_capabilities():
    """The manifest must declare every capability the injection path consumes.

    注入能力改用包含断言：repeat-and-rebrand 起 capabilities 还声明了复读
    观测的 chat.receive.before_process（完整集合由 test_plugin.py 的
    manifest 断言钉死）。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    capabilities = manifest.get("capabilities")
    assert isinstance(capabilities, list), "_manifest.json 缺少 capabilities 数组"
    assert REQUIRED_CAPABILITIES <= set(capabilities), (
        f"manifest capabilities 漂移: 缺 {REQUIRED_CAPABILITIES - set(capabilities)}"
    )
