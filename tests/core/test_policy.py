"""Policy resolution (contract 4.3): locked -> on; configured -> that value;
else the declaration's default. A plugin capability exists only when its
plugin is loaded. Write tools cannot be enabled because they do not exist."""

from __future__ import annotations

import dataclasses

import pytest

from marketlens_mcp import config as cfg
from marketlens_mcp import policy
from marketlens_mcp.plugin_api import BUILTIN_CAPABILITIES, CapabilitySpec


def load(tmp_path, text=""):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return cfg.load_config()


def test_builtin_defaults(tmp_path):
    pol = policy.resolve(load(tmp_path), BUILTIN_CAPABILITIES)
    assert pol.enabled == frozenset({"market", "reference", "news", "analytics", "results"})
    assert pol.state("portfolio").enabled is False
    assert pol.state("results.export").enabled is False
    assert pol.state("provider.docs").enabled is False
    assert {s.source for s in pol.states} == {"default"}
    assert [s.spec.id for s in pol.states] == [c.id for c in BUILTIN_CAPABILITIES]


def test_config_overrides_defaults(tmp_path):
    pol = policy.resolve(
        load(tmp_path, "capabilities:\n  portfolio: true\n  news: false\n"), BUILTIN_CAPABILITIES
    )
    assert pol.is_enabled("portfolio")
    assert not pol.is_enabled("news")
    assert pol.state("portfolio").source == "config"
    assert pol.state("market").source == "default"


def test_locked_capability_is_always_on(tmp_path):
    pol = policy.resolve(load(tmp_path), BUILTIN_CAPABILITIES)
    assert pol.is_enabled("results")
    assert pol.state("results").spec.locked


def test_r3_unknown_capability(tmp_path):
    c = load(tmp_path, "capabilities:\n  research: true\n")
    with pytest.raises(cfg.ConfigError) as info:
        policy.resolve(c, BUILTIN_CAPABILITIES)
    shown = str(tmp_path / "xdg" / "marketlens" / "config.yaml")
    assert str(info.value) == (
        f"marketlens config {shown}: unknown capability 'research'. Known: market, reference, news, analytics, "
        "portfolio, results, results.export, provider.docs. A plugin's capability is known only when the "
        "plugin is installed and listed in plugins.enabled."
    )


def test_plugin_capability_default_applies_once_the_plugin_is_loaded(tmp_path):
    research = CapabilitySpec("research", "Research", "Notes and theses.", True, declared_by="myplugin")
    hidden = CapabilitySpec("myplugin.extra", "Extra", "Off by default.", False, declared_by="myplugin")
    pol = policy.resolve(load(tmp_path), (*BUILTIN_CAPABILITIES, research, hidden))
    assert pol.is_enabled("research")
    assert pol.state("research").source == "plugin default"
    assert not pol.is_enabled("myplugin.extra")
    pol = policy.resolve(
        load(tmp_path, "capabilities:\n  research: false\n"), (*BUILTIN_CAPABILITIES, research)
    )
    assert not pol.is_enabled("research")
    assert pol.state("research").source == "config"


@pytest.mark.parametrize("name", ["trading", "orders", "portfolio.write", "orders.place", "write"])
def test_no_configuration_can_enable_a_write_capability(tmp_path, name):
    c = load(tmp_path, f"capabilities:\n  {name}: true\n")
    with pytest.raises(cfg.ConfigError, match="unknown capability"):
        policy.resolve(c, BUILTIN_CAPABILITIES)


def test_live_warning_text():
    assert (
        policy.LIVE_WARNING
        == "portfolio.environment is live: portfolio tools read your LIVE brokerage account."
    )


def test_capability_with_a_locked_flag_ignores_config(tmp_path):
    locked = dataclasses.replace(BUILTIN_CAPABILITIES[0], id="always", locked=True)
    pol = policy.resolve(load(tmp_path), (locked,))
    assert pol.is_enabled("always")
