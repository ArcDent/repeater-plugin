"""配置模型测试：默认值，以及 config.toml 与 config.py 不漂移。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from config import RepeaterConfig
from tests.fake_maibot_sdk import config_field_names

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

CONFIG_TOML = Path(__file__).resolve().parent.parent / "config.toml"
MANIFEST_JSON = Path(__file__).resolve().parent.parent / "_manifest.json"


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
def test_config_toml_matches_model_defaults():
    """config.toml 与 config.py 双向一致：toml 的每个键在模型里且默认值相等，
    模型的每个字段也都出现在 toml 里（默认配置不缺键）。"""
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

    for section_name in config_field_names(RepeaterConfig):
        assert section_name in raw, f"config.py 的 [{section_name}] 在 config.toml 中缺失"
        for key in config_field_names(type(getattr(config, section_name))):
            assert key in raw[section_name], f"config.py 的 {section_name}.{key} 在 config.toml 中缺失"


def test_manifest_version_matches_changelog_head():
    """manifest version 与最新 CHANGELOG 条目标题一致。

    仓库没有独立的插件版本常量，manifest 的 version 与 CHANGELOG 首条标题是
    两个独立来源，互为校验；写错任一侧都会红。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    changelog = (MANIFEST_JSON.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    head = re.search(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)
    assert head is not None, "CHANGELOG.md 缺少 '## [x.y.z]' 形式的版本条目"
    assert head.group(1) == manifest["version"]
