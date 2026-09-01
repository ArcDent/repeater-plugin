"""配置模型测试：默认值，以及 config.toml 与 config.py 不漂移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import SendingEmojisConfig

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
    config = SendingEmojisConfig()
    assert config.plugin.enabled is True
    assert config.bandwagon.probability == 0.25
    assert config.bandwagon.self_message_probability == 0.12
    assert config.opposite.enabled is True
    assert config.opposite.probability == 0.35
    assert config.opposite.include_defaults is True


def test_self_message_probability_not_higher_than_others():
    """给麦麦自己的消息跟风应更保守，否则观感像自吹。"""
    config = SendingEmojisConfig()
    assert config.bandwagon.self_message_probability <= config.bandwagon.probability


def test_default_webhook_port_avoids_friend_request_plugin():
    config = SendingEmojisConfig()
    assert config.webhook.port == 18081
    assert config.webhook.path.startswith("/")


def test_delay_window_is_ordered():
    config = SendingEmojisConfig()
    assert config.behavior.delay_min_seconds <= config.behavior.delay_max_seconds


def test_list_fields_are_independent_per_instance():
    first, second = SendingEmojisConfig(), SendingEmojisConfig()
    first.scope.group_blacklist.append("10001")
    assert second.scope.group_blacklist == []


def test_all_sections_present():
    config = SendingEmojisConfig()
    for section in ("plugin", "webhook", "protocol", "bandwagon", "opposite", "behavior", "context", "scope"):
        assert getattr(config, section) is not None


def test_context_section_defaults():
    cfg = SendingEmojisConfig()
    assert cfg.context.enabled is False
    assert cfg.context.dedup_ttl_seconds == 3600
    assert cfg.context.merge_window_seconds == 30
    assert cfg.context.max_reactions_per_message == 10
    assert cfg.context.self_display_name == ""


def test_context_section_accepts_overrides():
    cfg = SendingEmojisConfig(context={"enabled": True, "dedup_ttl_seconds": 120,
                                       "merge_window_seconds": 5,
                                       "max_reactions_per_message": 3,
                                       "self_display_name": "麦麦"})
    assert cfg.context.enabled is True
    assert cfg.context.dedup_ttl_seconds == 120
    assert cfg.context.merge_window_seconds == 5
    assert cfg.context.max_reactions_per_message == 3
    assert cfg.context.self_display_name == "麦麦"


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 的 tomllib")
def test_config_toml_matches_model_defaults():
    assert tomllib is not None
    with CONFIG_TOML.open("rb") as fp:
        raw = tomllib.load(fp)
    config = SendingEmojisConfig()

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
    """The manifest must declare every capability the injection path consumes."""
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    capabilities = manifest.get("capabilities")
    assert isinstance(capabilities, list), "_manifest.json 缺少 capabilities 数组"
    assert set(capabilities) == REQUIRED_CAPABILITIES, (
        f"manifest capabilities 漂移: 缺 {REQUIRED_CAPABILITIES - set(capabilities)}，"
        f"多 {set(capabilities) - REQUIRED_CAPABILITIES}"
    )