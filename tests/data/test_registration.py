"""With marketlens-data importable: the 25 tools register through the plugin
API, pass the manifest rules, and are listed by the server under their
capabilities (calendars off by default)."""

from __future__ import annotations

import json
import pathlib

from marketlens_mcp import cli, registry, server
from marketlens_mcp import config as cfg
from marketlens_mcp.providers.data import runtime
from marketlens_mcp.providers.data.tools import all_specs

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_the_extra_is_importable_here():
    assert runtime.available() and runtime.api_problems() == []


def test_25_specs_register_and_satisfy_the_manifest():
    cat = registry.build_catalog(cfg.load_config(), discover=list)
    data = {n: e for n, e in cat.tools.items() if e.plugin == "builtin:data"}
    assert len(data) == 25 and set(data) == {s.name for s in all_specs()}
    for name, entry in data.items():
        spec = entry.spec
        assert spec.input_model.model_config["extra"] == "forbid"
        assert len(spec.description) <= 1000, name
        assert cat.models[spec.output_model.schema_name] is spec.output_model
        golden = REPO / spec.golden_test
        assert golden.is_file() and name in golden.read_text(encoding="utf-8"), name
        assert set(entry.env) >= set(spec.env) and "OMNI_DATA_DIR" in entry.env


def test_tools_json_lists_68_with_defaults_and_72_with_calendars(capsys, tmp_path):
    assert cli.main(["tools", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert len(body["tools"]) == 68
    assert not any(t["name"].startswith("calendar_") for t in body["tools"])
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("capabilities:\n  calendars: true\n", encoding="utf-8")
    assert cli.main(["tools", "--json"]) == 0
    tools = {t["name"]: t for t in json.loads(capsys.readouterr().out)["tools"]}
    assert len(tools) == 72
    assert tools["calendar_economic"] == {
        "name": "calendar_economic",
        "capability": "calendars",
        "provider": "nasdaq",
        "plugin": "builtin:data",
        "enabled": True,
        "output_risk": "external_text",
    }


def test_the_server_mounts_the_data_capabilities(tmp_path):
    rt = server.build_runtime()
    mcp = server.build_server(rt)
    names = {s.name for s in server.sub_servers(mcp)}
    assert {"marketlens-macro", "marketlens-filings", "marketlens-fed_treasury"} <= names
    assert "marketlens-calendars" not in names
