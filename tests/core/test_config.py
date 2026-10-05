"""Configuration file: defaults, validation and the verbatim refusal texts
(contract 4.2 and 4.4)."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from marketlens_mcp import config as cfg
from marketlens_schema import Environment


def write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def refusal(tmp_path, text) -> str:
    write(tmp_path, text)
    with pytest.raises(cfg.ConfigError) as info:
        cfg.load_config()
    return str(info.value)


def shown(tmp_path) -> str:
    # The isolated XDG dir is under tmp_path, not under the home directory,
    # so the path is shown in full.
    return str(tmp_path / "xdg" / "marketlens" / "config.yaml")


def test_missing_default_file_means_all_defaults():
    c = cfg.load_config()
    assert not c.exists
    assert c.capabilities == {}
    assert c.portfolio_environment is Environment.PAPER
    assert c.results["inline_max_rows"] == 200
    assert c.results["inline_max_tokens"] == 6000
    assert c.results["query_max_rows"] == 200
    assert c.results["query_max_bytes"] == 24000
    assert c.results["query_timeout_seconds"] == 10
    assert c.results["query_memory_limit"] == "1GB"
    assert c.results["ttl_hours"] == 24
    assert c.results["max_store_gb"] == 5
    assert c.results["export_dir"] is None
    assert c.fetch_limits.max_rows == 50_000 and c.fetch_limits.max_pages == 20
    assert c.plugins_enabled == ()
    assert c.http_port == 8765
    assert c.provider_settings("alpaca")["stock_feed"] == "iex"
    assert c.provider_settings("alpaca")["rate_limit_per_minute"] == 190
    assert c.provider_settings("nothing") == {}


def test_default_text_is_the_init_file_and_round_trips(tmp_path):
    text = cfg.DEFAULT_CONFIG_TEXT
    assert text.startswith("# marketlens-mcp configuration. Restart the server after editing.\nversion: 1\n")
    assert "results.export: false   # write stored results to files in results.export_dir" in text
    assert "enabled: []             # entry-point names of installed plugins to load, e.g. [myplugin]" in text
    write(tmp_path, text)
    c = cfg.load_config()
    assert c.exists
    assert c.capabilities == {
        "market": True,
        "reference": True,
        "news": True,
        "analytics": True,
        "portfolio": False,
        "results.export": False,
        "provider.docs": False,
    }
    assert dict(c.results) == dict(cfg.DEFAULTS["results"])
    assert "capabilities" not in cfg.DEFAULTS


def test_empty_file_means_defaults(tmp_path):
    write(tmp_path, "")
    assert cfg.load_config().capabilities == {}


def test_partial_sections_merge_with_defaults(tmp_path):
    write(tmp_path, "results:\n  inline_max_rows: 50\nportfolio:\n  environment: live\n")
    c = cfg.load_config()
    assert c.results["inline_max_rows"] == 50
    assert c.results["ttl_hours"] == 24
    assert c.portfolio_environment is Environment.LIVE


def test_r1_invalid_yaml(tmp_path):
    msg = refusal(tmp_path, "results: [unclosed\n")
    assert msg.startswith(f"marketlens config {shown(tmp_path)} is not valid YAML: ")
    assert "\n" not in msg


def test_r2_unknown_key_at_top_level(tmp_path):
    msg = refusal(tmp_path, "resluts: {}\n")
    assert msg == (
        f"marketlens config {shown(tmp_path)}: unknown setting 'resluts'. Known settings here: "
        "version, capabilities, portfolio, providers, results, fetch, plugins, http."
    )


def test_r2_unknown_nested_key(tmp_path):
    msg = refusal(tmp_path, "results:\n  inline_rows: 3\n")
    assert msg.startswith(
        f"marketlens config {shown(tmp_path)}: unknown setting 'results.inline_rows'. Known settings here: inline_max_rows, "
    )
    msg = refusal(tmp_path, "providers:\n  polygon: {}\n")
    assert (
        msg
        == f"marketlens config {shown(tmp_path)}: unknown setting 'providers.polygon'. Known settings here: alpaca."
    )


def test_r2_plugin_settings_only_for_enabled_plugins(tmp_path):
    msg = refusal(tmp_path, "plugins:\n  enabled: [myplugin]\n  settings:\n    other: {}\n")
    assert msg == (
        f"marketlens config {shown(tmp_path)}: unknown setting 'plugins.settings.other'. Known settings here: myplugin."
    )
    write(tmp_path, "plugins:\n  enabled: [myplugin]\n  settings:\n    myplugin: {x: 1}\n")
    c = cfg.load_config()
    assert c.plugins_enabled == ("myplugin",)
    assert c.plugin_settings("myplugin") == {"x": 1}
    assert c.plugin_settings("absent") == {}


def test_r4_results_cannot_be_configured(tmp_path):
    assert refusal(tmp_path, "capabilities:\n  results: false\n") == (
        f"marketlens config {shown(tmp_path)}: capability 'results' is always on and cannot be configured."
    )


@pytest.mark.parametrize(
    ("text", "key", "lo", "hi", "got"),
    [
        ("results:\n  inline_max_rows: 0\n", "results.inline_max_rows", 1, 1000, 0),
        ("results:\n  inline_max_tokens: 100\n", "results.inline_max_tokens", 500, 20000, 100),
        ("results:\n  query_max_rows: 201\n", "results.query_max_rows", 1, 200, 201),
        ("results:\n  query_max_bytes: 999\n", "results.query_max_bytes", 1000, 200000, 999),
        ("results:\n  query_timeout_seconds: 121\n", "results.query_timeout_seconds", 1, 120, 121),
        ("results:\n  ttl_hours: 169\n", "results.ttl_hours", 1, 168, 169),
        ("results:\n  max_store_gb: 0.05\n", "results.max_store_gb", 0.1, 100, 0.05),
        ("fetch:\n  max_rows: 1000001\n", "fetch.max_rows", 1, 1000000, 1000001),
        ("fetch:\n  max_pages: 0\n", "fetch.max_pages", 1, 200, 0),
        (
            "providers:\n  alpaca:\n    rate_limit_per_minute: 200\n",
            "providers.alpaca.rate_limit_per_minute",
            1,
            199,
            200,
        ),
        ("http:\n  port: 80\n", "http.port", 1024, 65535, 80),
    ],
)
def test_r5_out_of_range(tmp_path, text, key, lo, hi, got):
    assert refusal(tmp_path, text) == (
        f"marketlens config {shown(tmp_path)}: {key} must be between {lo} and {hi} (got {got})."
    )


@pytest.mark.parametrize(
    ("text", "key", "type_text", "got"),
    [
        ("results:\n  inline_max_rows: lots\n", "results.inline_max_rows", "an integer", "'lots'"),
        ("results:\n  inline_max_rows: true\n", "results.inline_max_rows", "an integer", "True"),
        ("results:\n  ttl_hours: '24'\n", "results.ttl_hours", "a number", "'24'"),
        ("capabilities:\n  market: 'yes'\n", "capabilities.market", "true or false", "'yes'"),
        (
            "portfolio:\n  environment: production\n",
            "portfolio.environment",
            "one of paper, live",
            "'production'",
        ),
        (
            "providers:\n  alpaca:\n    stock_feed: nasdaq\n",
            "providers.alpaca.stock_feed",
            "one of iex, sip, delayed_sip, boats, overnight",
            "'nasdaq'",
        ),
        ("plugins:\n  enabled: myplugin\n", "plugins.enabled", "a list of plugin names", "'myplugin'"),
        ("plugins:\n  enabled: [My-Plugin]\n", "plugins.enabled", "a list of plugin names", "['My-Plugin']"),
        ("results: 5\n", "results", "a mapping", "5"),
        ("results:\n  export_dir: 7\n", "results.export_dir", "a string or null", "7"),
        (
            "results:\n  query_memory_limit: lots\n",
            "results.query_memory_limit",
            "a size such as 1GB or 512MB",
            "'lots'",
        ),
        ("version: 2\n", "version", "1", "2"),
    ],
)
def test_r6_wrong_type(tmp_path, text, key, type_text, got):
    assert refusal(tmp_path, text) == (
        f"marketlens config {shown(tmp_path)}: {key} must be {type_text} (got {got})."
    )


def test_top_level_must_be_a_mapping(tmp_path):
    assert refusal(tmp_path, "- a\n- b\n") == (
        f"marketlens config {shown(tmp_path)}: the top level must be a mapping (got ['a', 'b'])."
    )


def test_r7_marketlens_config_missing(tmp_path, monkeypatch):
    target = tmp_path / "nope.yaml"
    monkeypatch.setenv("MARKETLENS_CONFIG", str(target))
    with pytest.raises(cfg.ConfigError) as info:
        cfg.load_config()
    assert str(info.value) == f"MARKETLENS_CONFIG points to {target}, which does not exist."


def test_marketlens_config_is_used_when_it_exists(tmp_path, monkeypatch):
    target = tmp_path / "mine.yaml"
    target.write_text("http:\n  port: 9000\n", encoding="utf-8")
    monkeypatch.setenv("MARKETLENS_CONFIG", str(target))
    assert cfg.load_config().http_port == 9000


def test_r8_export_needs_an_existing_absolute_folder(tmp_path):
    expected = (
        f"marketlens config {shown(tmp_path)}: results.export is on, so results.export_dir must be an "
        "existing absolute folder."
    )
    assert refusal(tmp_path, "capabilities:\n  results.export: true\n") == expected
    assert (
        refusal(tmp_path, "capabilities:\n  results.export: true\nresults:\n  export_dir: relative\n")
        == expected
    )
    missing = tmp_path / "missing"
    assert (
        refusal(tmp_path, f"capabilities:\n  results.export: true\nresults:\n  export_dir: '{missing}'\n")
        == expected
    )
    (tmp_path / "out").mkdir()
    write(tmp_path, f"capabilities:\n  results.export: true\nresults:\n  export_dir: '{tmp_path / 'out'}'\n")
    assert cfg.load_config().results["export_dir"] == str(tmp_path / "out")


def test_home_is_shown_as_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME")
    p = home / ".config" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError) as info:
        cfg.load_config()
    assert str(info.value).startswith(
        "marketlens config ~/.config/marketlens/config.yaml: unknown setting 'bogus'."
    )


def test_secrets_never_come_from_the_file(tmp_path):
    msg = refusal(tmp_path, "ALPACA_API_KEY: abc\n")
    assert "unknown setting 'ALPACA_API_KEY'" in msg
    assert "abc" not in msg


def test_unknown_capabilities_are_kept_for_the_policy_to_judge(tmp_path):
    # A plugin's capability is only known after plugins load; the policy refuses
    # unknown ids (R3), the loader only checks the value type.
    write(tmp_path, "capabilities:\n  research: true\n")
    assert cfg.load_config().capabilities == {"research": True}


def test_effective_yaml_shows_secrets_only_as_set_or_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PKSECRETVALUE")
    text = cfg.load_config().effective_yaml()
    assert "PKSECRETVALUE" not in text
    assert "# ALPACA_API_KEY: set" in text
    assert "# ALPACA_SECRET_KEY: unset" in text
    assert "# MARKETLENS_HTTP_TOKEN: unset" in text
    body = yaml.safe_load(text)
    assert body["results"]["inline_max_rows"] == 200
