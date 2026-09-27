"""配置模型测试：默认值，以及 config.toml 与 config.py 不漂移。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from config import ForwardSection, RepeaterConfig

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
        "forward",
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


def test_config_version_updated_to_1_3_0():
    """config_version 是配置结构版本：新增四个跨群转发字段时从 1.2.0 升到 1.3.0。"""
    cfg = RepeaterConfig()
    assert cfg.plugin.config_version == "1.3.0"


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


# ---- 跨群转发域配置（design D6）----

def test_forward_section_defaults():
    """[forward] 节默认值与 design D6 表逐项一致（独立字面量，不引自实现）。

    转发域默认关闭（enabled=False）：升级后不配置即是「装了但不动」。
    """
    cfg = RepeaterConfig()
    assert cfg.forward.enabled is False
    assert cfg.forward.mode == "direct"
    assert cfg.forward.auto_enabled is True
    assert cfg.forward.request_enabled is True
    assert cfg.forward.summary_timeout_seconds == 10
    assert cfg.forward.source_groups == []
    assert cfg.forward.target_groups == []
    assert cfg.forward.weight_send == 30
    assert cfg.forward.weight_silence == 70
    assert cfg.forward.dedup_ttl_seconds == 1800
    assert cfg.forward.cooldown_seconds == 60
    assert cfg.forward.target_cooldown_seconds == 60
    assert cfg.forward.delay_min_seconds == 0.5
    assert cfg.forward.delay_max_seconds == 2.0
    assert cfg.forward.trigger_source_planner is True
    assert cfg.forward.trigger_target_planner is True
    assert cfg.forward.view_failure_fallback_threshold == 2
    assert cfg.forward.inject_max_chars == 800


def test_forward_section_is_exported():
    """seam：`config.ForwardSection` 是模块导出类，根配置挂载的是同一个类。"""
    assert isinstance(RepeaterConfig().forward, ForwardSection)


def test_forward_list_fields_are_independent_per_instance():
    """source/target 群列表是可变列表，两个实例不得共享同一份默认值。"""
    first, second = RepeaterConfig(), RepeaterConfig()
    first.forward.source_groups.append("10001")
    first.forward.target_groups.append("20002")
    assert second.forward.source_groups == []
    assert second.forward.target_groups == []


def test_forward_delay_window_is_ordered():
    """转发延迟区间语义与其余延迟字段一致：最小不超过最大。"""
    cfg = RepeaterConfig()
    assert cfg.forward.delay_min_seconds <= cfg.forward.delay_max_seconds


def test_forward_config_rejects_negative_numbers():
    """负权重 / 负数冷却不得生效：数值字段以 ge 下界声明非负，真实 SDK 的 pydantic
    校验层直接拒绝（本机对真实 maibot_sdk 实测为 ValidationError，ValidationError
    是 ValueError 子类）；fake SDK 对齐这条校验，防止「负值悄悄按负值生效」在本地
    测试里被放行。运行时另有非负 clamp 兜底（forwards.auto，第 2 组）。
    """
    with pytest.raises(ValueError):
        RepeaterConfig(forward={"weight_send": -5})
    with pytest.raises(ValueError):
        RepeaterConfig(forward={"cooldown_seconds": -1})
    with pytest.raises(ValueError):
        RepeaterConfig(forward={"delay_min_seconds": -0.5})


def test_forward_summary_timeout_rejects_out_of_range():
    """摘要就绪超时以 ge=1, le=300 声明：0 与 301 都必须被配置校验拒绝（与
    repeat 域数值字段的 ge= 口径同构）。"""
    with pytest.raises(ValueError):
        RepeaterConfig(forward={"summary_timeout_seconds": 0})
    with pytest.raises(ValueError):
        RepeaterConfig(forward={"summary_timeout_seconds": 301})


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 的 tomllib")
def test_config_toml_repeat_defaults_match_design():
    """[repeat] 节与 [plugin].config_version 的 toml 默认值与 design D2 表逐项一致（独立字面量）；scope.ignore_senders 的默认空列表一并在此钉死（与 config.py 默认同口径，防 toml 侧漂移）。"""
    with CONFIG_TOML.open("rb") as fp:
        raw = tomllib.load(fp)
    repeat = raw.get("repeat")
    assert isinstance(repeat, dict), "config.toml 缺少 [repeat] 节"
    assert raw["plugin"]["config_version"] == "1.3.0"
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
def test_config_toml_forward_defaults_match_design():
    """[forward] 节的 toml 默认值与 design D6 表逐项一致（独立字面量，防 toml 侧漂移）。

    字段全集一并在此钉死：config.toml 多写废弃键或漏写新键都会红。
    """
    with CONFIG_TOML.open("rb") as fp:
        raw = tomllib.load(fp)
    forward = raw.get("forward")
    assert isinstance(forward, dict), "config.toml 缺少 [forward] 节"
    assert set(forward) == {
        "enabled",
        "mode",
        "auto_enabled",
        "request_enabled",
        "source_groups",
        "target_groups",
        "weight_send",
        "weight_silence",
        "dedup_ttl_seconds",
        "summary_timeout_seconds",
        "cooldown_seconds",
        "target_cooldown_seconds",
        "delay_min_seconds",
        "delay_max_seconds",
        "trigger_source_planner",
        "trigger_target_planner",
        "view_failure_fallback_threshold",
        "inject_max_chars",
    }
    assert forward["enabled"] is False
    assert forward["mode"] == "direct"
    assert forward["auto_enabled"] is True
    assert forward["request_enabled"] is True
    assert forward["source_groups"] == []
    assert forward["target_groups"] == []
    assert forward["weight_send"] == 30
    assert forward["weight_silence"] == 70
    assert forward["dedup_ttl_seconds"] == 1800
    assert forward["summary_timeout_seconds"] == 10
    assert forward["cooldown_seconds"] == 60
    assert forward["target_cooldown_seconds"] == 60
    assert forward["delay_min_seconds"] == 0.5
    assert forward["delay_max_seconds"] == 2.0
    assert forward["trigger_source_planner"] is True
    assert forward["trigger_target_planner"] is True
    assert forward["view_failure_fallback_threshold"] == 2
    assert forward["inject_max_chars"] == 800


def test_config_toml_forward_new_keys_have_chinese_comments():
    """[forward] 段四个新字段各自紧邻一行中文注释（部署时改配置能看懂取值含义）。

    只做文本行扫描、不用 tomllib，故不挂 skipif：Python 3.10 下同样可收集。
    """
    text = CONFIG_TOML.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index("[forward]")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    section = lines[start + 1 : end]
    for key in ("mode", "auto_enabled", "request_enabled", "summary_timeout_seconds"):
        key_idx = next(
            (i for i, line in enumerate(section) if re.match(rf"\s*{key}\s*=", line)),
            None,
        )
        assert key_idx is not None, f"[forward] 段缺少 {key} 行"
        comment_lines = []
        cursor = key_idx - 1
        while cursor >= 0 and section[cursor].lstrip().startswith("#"):
            comment_lines.append(section[cursor])
            cursor -= 1
        assert comment_lines, f"[forward].{key} 上一行不是注释"
        assert any(re.search(r"[\u4e00-\u9fff]", comment) for comment in comment_lines), (
            f"[forward].{key} 的注释不含中文: {comment_lines!r}"
        )


def test_config_toml_forward_key_order_mirrors_model_order():
    """[forward] 段的 toml 行序与 `ForwardSection` 的字段声明序一致（声明序即 webui `order` 编号序）。

    文本行扫描即可，不用 tomllib：测试替身不保留 `json_schema_extra`，
    `order` 在运行时不可读，声明序是等价且可读的口径。
    """
    text = CONFIG_TOML.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index("[forward]")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    toml_order = [
        match.group(1)
        for line in lines[start + 1 : end]
        if (match := re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line))
    ]
    model_order = [
        name for name in ForwardSection.__annotations__ if not name.startswith("__")
    ]
    assert toml_order == model_order


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


def test_manifest_version_matches_changelog_head():
    """manifest version 与最新 CHANGELOG 条目标题一致（0.7.1：就绪轮询与活跃会话解析修复）。

    仓库没有独立的插件版本常量，manifest 的 version 与 CHANGELOG 首条标题是
    两个独立来源，互为校验；写错任一侧都会红。
    """
    with MANIFEST_JSON.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    assert manifest["version"] == "0.7.1"
    changelog = (MANIFEST_JSON.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    head = re.search(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)
    assert head is not None, "CHANGELOG.md 缺少 '## [x.y.z]' 形式的版本条目"
    assert head.group(1) == manifest["version"]


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
    # 本次升级不新增能力，数量保持 11（test_plugin.py 另有消费集合与声明的双向校验）
    assert len(capabilities) == 11
    assert REQUIRED_CAPABILITIES <= set(capabilities), (
        f"manifest capabilities 漂移: 缺 {REQUIRED_CAPABILITIES - set(capabilities)}"
    )
