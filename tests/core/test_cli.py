"""The console script (contract 1.3)."""

from __future__ import annotations

import json
import os
import stat
import sys

import httpx
import pytest
import yaml

from marketlens_mcp import cli, config


def main(*argv):
    return cli.main(list(argv))


def write_config(tmp_path, text):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_version(capsys):
    assert main("--version") == 0
    assert capsys.readouterr().out.strip() == "marketlens-mcp 0.2.0 (schema 1.1.0, plugin API 1.0)"


def test_tools_json_format(capsys, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    assert main("tools", "--json") == 0
    body = json.loads(capsys.readouterr().out)
    assert list(body) == [
        "server",
        "version",
        "schema_version",
        "plugin_api",
        "config_path",
        "tools",
        "capabilities",
        "plugins",
    ]
    assert body["server"] == "marketlens-mcp" and body["version"] == "0.2.0"
    assert body["schema_version"] == "1.1.0" and body["plugin_api"] == [1, 0]
    assert body["config_path"] == "~/.config/marketlens/config.yaml"
    q = next(t for t in body["tools"] if t["name"] == "results_query")
    assert q == {
        "name": "results_query",
        "capability": "results",
        "provider": "local",
        "plugin": "builtin:results",
        "enabled": True,
        "output_risk": "api_structured",
    }
    assert all(t["enabled"] for t in body["tools"])
    assert "results_export" not in [t["name"] for t in body["tools"]]
    market = next(c for c in body["capabilities"] if c["id"] == "market")
    assert market == {
        "id": "market",
        "enabled": True,
        "default": True,
        "source": "default",
        "declared_by": "builtin",
    }
    assert body["plugins"] == []


def test_tools_json_all_and_plugins(capsys, tmp_path):
    write_config(tmp_path, "plugins:\n  enabled: [ghost]\n")
    assert main("tools", "--json", "--all") == 0
    body = json.loads(capsys.readouterr().out)
    export = next(t for t in body["tools"] if t["name"] == "results_export")
    assert export["enabled"] is False
    assert body["plugins"] == [
        {"name": "ghost", "enabled": True, "loaded": False, "api_version": None, "error": "not installed"}
    ]


def test_tools_markdown(capsys, tmp_path):
    assert main("tools", "--markdown") == 0
    out = capsys.readouterr().out
    assert out.startswith("| Tool | Capability | Provider route | Returns | Env vars | Notes |")
    assert "| `results_query` | results |" in out
    assert "results_export" not in out
    assert main("tools", "--markdown", "--all") == 0
    assert "| `results_export` | results.export (off) |" in capsys.readouterr().out


def test_tools_plain(capsys):
    assert main("tools") == 0
    out = capsys.readouterr().out
    assert "results_query" in out and "results_export" not in out


def test_capabilities_and_plugins_commands(capsys, tmp_path):
    write_config(tmp_path, "capabilities:\n  portfolio: true\n")
    assert main("capabilities") == 0
    out = capsys.readouterr().out
    assert "portfolio" in out and "config" in out and "results.export" in out
    assert main("plugins") == 0
    assert "No plugins" in capsys.readouterr().out


def test_config_errors_exit_2_with_the_refusal(capsys, tmp_path):
    write_config(tmp_path, "nonsense: 1\n")
    assert main("tools", "--json") == 2
    err = capsys.readouterr().err
    assert "unknown setting 'nonsense'" in err


def test_config_path_show_init(capsys, tmp_path):
    target = tmp_path / "xdg" / "marketlens" / "config.yaml"
    assert main("config", "path") == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert main("config", "init") == 0
    assert target.read_text(encoding="utf-8") == config.DEFAULT_CONFIG_TEXT
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    capsys.readouterr()
    assert main("config", "init") == 1
    assert "already exists" in capsys.readouterr().err
    assert main("config", "show") == 0
    shown = capsys.readouterr().out
    assert yaml.safe_load(shown)["capabilities"]["portfolio"] is False
    assert "# ALPACA_API_KEY: unset" in shown


def test_config_init_honours_marketlens_config(capsys, tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "ml.yaml"
    monkeypatch.setenv("MARKETLENS_CONFIG", str(target))
    assert main("config", "init") == 0
    assert target.is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_config_init_leaves_an_existing_folder_as_it_was(capsys, tmp_path, monkeypatch):
    """MARKETLENS_CONFIG in an existing project folder made that folder owner-only."""
    project = tmp_path / "project"
    project.mkdir()
    os.chmod(project, 0o740)
    monkeypatch.setenv("MARKETLENS_CONFIG", str(project / "marketlens.yaml"))
    assert main("config", "init") == 0
    assert stat.S_IMODE(project.stat().st_mode) == 0o740
    assert stat.S_IMODE((project / "marketlens.yaml").stat().st_mode) == 0o600


def test_schema_command(capsys, tmp_path):
    assert main("schema") == 0
    index = json.loads(capsys.readouterr().out)
    assert index["schema_version"] == "1.1.0"
    assert index["models"]["marketlens.Bar"] == "marketlens.Bar.json"
    out = tmp_path / "schemas"
    assert main("schema", "--out", str(out)) == 0
    bar = json.loads((out / "marketlens.Bar.json").read_text(encoding="utf-8"))
    assert bar["$id"] == "urn:marketlens:schema:1:marketlens.Bar"
    assert bar["x-schema-version"] == "1.1.0"
    assert json.loads((out / "index.json").read_text(encoding="utf-8")) == index


def test_results_list_and_purge(capsys, tmp_path):
    from coresupport import bar_table, provenance

    from marketlens_mcp.results.store import StoreRoot

    StoreRoot(tmp_path / "cache").session("s").put(
        bar_table(n=5), tool="t", model="marketlens.Bar", provenance=provenance()
    )
    assert main("results", "list") == 0
    assert "1 result" in capsys.readouterr().out
    assert main("results", "purge") == 1
    assert "--yes" in capsys.readouterr().err
    assert main("results", "purge", "--yes") == 0
    assert "Deleted 1 stored result" in capsys.readouterr().out
    assert list((tmp_path / "cache" / "results").rglob("r_*")) == []


def test_serve_http_refusals(capsys):
    assert main("serve", "--transport", "http") == 2
    assert "MARKETLENS_HTTP_TOKEN" in capsys.readouterr().err
    os.environ["MARKETLENS_HTTP_TOKEN"] = "t" * 40
    try:
        assert main("serve", "--transport", "http", "--host", "0.0.0.0") == 2
        assert "--host 0.0.0.0 refused" in capsys.readouterr().err
    finally:
        del os.environ["MARKETLENS_HTTP_TOKEN"]


def test_bare_command_serves_stdio(monkeypatch):
    seen = {}

    def fake_serve(args):
        seen["transport"] = args.transport
        return 0

    monkeypatch.setattr(cli, "cmd_serve", fake_serve)
    assert main() == 0
    assert seen == {"transport": "stdio"}


def test_doctor_without_network(capsys):
    code = main("doctor")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "query guard self-test: ok" in out
    assert "config: ok" in out


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"},
            [
                "environment ALPACA_API_KEY and ALPACA_SECRET_KEY: set",
                "environment APCA_API_KEY_ID and APCA_API_SECRET_KEY: unset (not needed: ALPACA_API_KEY and ALPACA_SECRET_KEY are set)",
                "environment MYPLUGIN_TOKEN: unset (a tool that needs it refuses and names it)",
            ],
        ),
        (
            {"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"},
            [
                "environment ALPACA_API_KEY and ALPACA_SECRET_KEY: unset (not needed: APCA_API_KEY_ID and APCA_API_SECRET_KEY are set)",
                "environment APCA_API_KEY_ID and APCA_API_SECRET_KEY: set",
                "environment MYPLUGIN_TOKEN: unset (a tool that needs it refuses and names it)",
            ],
        ),
        (
            {"ALPACA_API_KEY": "k"},
            [
                "environment ALPACA_API_KEY and ALPACA_SECRET_KEY: incomplete (ALPACA_SECRET_KEY unset; Alpaca tools refuse until one pair is complete)",
                "environment APCA_API_KEY_ID and APCA_API_SECRET_KEY: unset (Alpaca tools refuse until one pair is set)",
                "environment MYPLUGIN_TOKEN: unset (a tool that needs it refuses and names it)",
            ],
        ),
    ],
)
def test_doctor_reads_the_alpaca_key_pairs_as_one_credential(env, expected):
    """2026-10-16: doctor said every unset variable's tools 'will refuse', so the
    unused alternative Alpaca spelling and an optional plugin bearer looked broken."""
    names = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "MYPLUGIN_TOKEN",
    ]
    assert cli._env_lines(sorted(names), env) == expected


def test_doctor_names_no_alpaca_key_for_the_documentation_tools(capsys, tmp_path):
    """2026-10-19: the provider_docs_* tools, which send no key, listed both Alpaca pairs."""
    off = ("market", "reference", "news", "macro", "filings", "fed_treasury", "holidays", "calendars")
    caps = "".join(f"  {cap}: false\n" for cap in off)
    write_config(tmp_path, f"capabilities:\n{caps}  provider.docs: true\n")
    assert main("doctor") == 0
    out = capsys.readouterr().out
    assert "ALPACA" not in out and "APCA" not in out


def test_doctor_reports_a_broken_plugin(capsys, tmp_path):
    write_config(tmp_path, "plugins:\n  enabled: [ghost]\n")
    assert main("doctor") == 1
    assert "plugin 'ghost' not loaded: not installed" in capsys.readouterr().out


def test_a_missing_plugin_whose_capability_is_configured_is_reported_not_fatal(capsys, tmp_path):
    write_config(tmp_path, "plugins:\n  enabled: [ghost]\ncapabilities:\n  ghost_data: true\n")
    assert main("plugins") == 0
    assert "error: not installed" in capsys.readouterr().out
    assert main("doctor") == 1
    out = capsys.readouterr().out
    assert "config: ok" in out and "plugin 'ghost' not loaded: not installed" in out
    assert "capability 'ghost_data' in the config skipped" in out


def test_doctor_network_uses_one_clock_call(capsys, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"is_open": False, "timestamp": "2026-10-04T12:00:00-04:00"})

    # Fake values that cannot collide with the variable NAMES doctor prints.
    monkeypatch.setenv("ALPACA_API_KEY", "PKzz9fakekeyvalue")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "zz9fakesecretvalue")
    monkeypatch.setattr(cli, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    assert main("doctor", "--network") == 0
    assert len(calls) == 1
    assert str(calls[0].url) == "https://paper-api.alpaca.markets/v2/clock"
    out = capsys.readouterr().out
    assert "PKzz9fakekeyvalue" not in out and "zz9fakesecretvalue" not in out


def test_doctor_network_takes_the_apca_spelling_of_the_key_pair(capsys, monkeypatch):
    """2026-10-19: with only APCA_API_KEY_ID and APCA_API_SECRET_KEY set, which every
    Alpaca tool accepts, --network skipped the clock call and counted a failure."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"is_open": False, "timestamp": "2026-10-04T12:00:00-04:00"})

    monkeypatch.setenv("APCA_API_KEY_ID", "PKzz9altkeyvalue")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "zz9altsecretvalue")
    monkeypatch.setattr(cli, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    assert main("doctor", "--network") == 0
    assert [r.headers["APCA-API-KEY-ID"] for r in calls] == ["PKzz9altkeyvalue"]
    out = capsys.readouterr().out
    assert "Alpaca clock: ok" in out and "zz9altsecretvalue" not in out


def test_doctor_network_without_keys_names_both_spellings(capsys):
    assert main("doctor", "--network") == 1
    assert (
        "Alpaca clock: skipped (neither key pair is set: ALPACA_API_KEY and ALPACA_SECRET_KEY or "
        "APCA_API_KEY_ID and APCA_API_SECRET_KEY)"
    ) in capsys.readouterr().out


def test_doctor_network_failure(capsys, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PKTEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SECRET")
    monkeypatch.setattr(cli, "HTTP_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    assert main("doctor", "--network") == 1
    assert "HTTP 401" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [("readme", "--check"), ("readme",)])
def test_readme_command_outside_a_checkout(capsys, tmp_path, monkeypatch, argv):
    monkeypatch.chdir(tmp_path)
    assert main(*argv) == 1
    assert "README.md" in capsys.readouterr().err


def test_config_show_lists_effective_capabilities_without_a_file(capsys):
    assert main("config", "show") == 0
    caps = yaml.safe_load(capsys.readouterr().out)["capabilities"]
    assert caps["market"] is True and caps["portfolio"] is False and caps["results"] is True


def test_results_list_uses_the_configured_cap(capsys, tmp_path):
    write_config(tmp_path, "results:\n  max_store_gb: 0.5\n")
    assert main("results", "list") == 0
    assert f"of {int(0.5 * 2**30):,} bytes" in capsys.readouterr().out


def test_doctor_reports_an_invalid_config_as_a_failure(capsys, tmp_path):
    write_config(tmp_path, "nonsense: 1\n")
    assert main("doctor") == 1
    assert "config: FAILED" in capsys.readouterr().out


def test_serve_http_port_range(capsys, monkeypatch):
    monkeypatch.setenv("MARKETLENS_HTTP_TOKEN", "t" * 40)
    assert main("serve", "--transport", "http", "--port", "80") == 2
    assert "--port must be between 1024 and 65535" in capsys.readouterr().err
