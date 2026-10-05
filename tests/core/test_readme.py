"""The generated README tool table (contract 9.1) and the JSON Schema
export (2.3)."""

from __future__ import annotations

import dataclasses

import ml_example_plugin as ex
import pytest

from marketlens_mcp import readme
from marketlens_mcp.plugin_api import BUILTIN_CAPABILITIES, CapabilitySpec
from marketlens_mcp.registry import ToolEntry
from marketlens_schema import BUILTIN_MODELS, jsonschema


def entries():
    news = dataclasses.replace(
        ex.THINGS,
        name="news_things",
        capability="news",
        output_risk="external_text",
        provider="alpaca",
        route="GET /v1beta1/news",
        env=("ALPACA_API_KEY",),
    )
    piped = dataclasses.replace(ex.THINGS, name="market_things", capability="market", readme="a | b")
    off = dataclasses.replace(ex.THINGS, name="portfolio_things", capability="portfolio")
    return [
        ToolEntry(spec=s, plugin="builtin:x", env=s.env)
        for s in (off, news, piped, dataclasses.replace(ex.THINGS, name="market_alpha", capability="market"))
    ]


def test_table_groups_by_capability_order_then_name():
    table = readme.tool_table(entries(), BUILTIN_CAPABILITIES)
    lines = table.splitlines()
    assert lines[0] == "| Tool | Capability (default) | Provider route | Returns | Env vars | Notes |"
    assert lines[1] == "|---|---|---|---|---|---|"
    names = [line.split("|")[1].strip() for line in lines[2:]]
    assert names == ["`market_alpha`", "`market_things`", "`news_things`", "`portfolio_things`"]
    assert "| market (on) |" in lines[2]
    assert "| portfolio (off) |" in lines[5]
    assert "untrusted text" in lines[4]
    assert "`GET /v1beta1/news` (alpaca)" in lines[4]
    assert "ALPACA_API_KEY" in lines[4]
    assert "a \\| b" in lines[3]
    assert "example.Thing" in lines[2]


def test_installed_table_marks_disabled_tools():
    states = {"market": True, "news": True, "portfolio": False}
    table = readme.tool_table(entries(), BUILTIN_CAPABILITIES, states=states, show_disabled=True)
    assert table.splitlines()[0].startswith("| Tool | Capability | ")
    assert "| portfolio (off) |" in table
    assert "| market |" in table
    hidden = readme.tool_table(entries(), BUILTIN_CAPABILITIES, states=states)
    assert "portfolio_things" not in hidden


def test_plugin_capabilities_come_after_builtins():
    caps = (*BUILTIN_CAPABILITIES, CapabilitySpec("example", "Example", "x", False, declared_by="example"))
    e = [ToolEntry(spec=ex.THINGS, plugin="example", env=())] + entries()
    names = [line.split("|")[1].strip() for line in readme.tool_table(e, caps).splitlines()[2:]]
    assert names[-1] == "`example_things`"


def test_update_between_markers():
    text = "intro\n<!-- tools:start -->\nold\n<!-- tools:end -->\noutro\n"
    new = readme.replace_table(text, "| t |")
    assert new == "intro\n<!-- tools:start -->\n| t |\n<!-- tools:end -->\noutro\n"
    with pytest.raises(ValueError, match="markers"):
        readme.replace_table("no markers", "x")


def test_builtin_table_ignores_the_owners_config(tmp_path):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("capabilities:\n  results.export: false\n  news: false\n", encoding="utf-8")
    table = readme.builtin_table()
    assert "`results_export`" in table and "results.export (off)" in table


def test_json_schema_export():
    schemas = jsonschema.export_all()
    assert set(schemas) == set(BUILTIN_MODELS)
    bar = schemas["marketlens.Bar"]
    assert bar["$id"] == "urn:marketlens:schema:1:marketlens.Bar"
    assert bar["x-schema-version"] == "1.1.0"
    assert bar["properties"]["close"]["x-unit"] == "price"
    acct = schemas["marketlens.Account"]
    assert acct["properties"]["cash"]["type"] == "string"
    index = jsonschema.index(schemas)
    assert index == {"schema_version": "1.1.0", "models": {n: f"{n}.json" for n in sorted(schemas)}}


def test_readme_capability_table_matches_the_declarations():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    for cap in BUILTIN_CAPABILITIES:
        default = "always on" if cap.locked else ("on" if cap.default_enabled else "off")
        assert f"| `{cap.id}` | {default} |" in text, cap.id


def test_readme_sql_examples_pass_the_guard():
    import pathlib
    import re

    from marketlens_mcp.results import guard

    text = (pathlib.Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    block = text.split("```sql", 1)[1].split("```", 1)[0]
    statements = [s.strip() for s in re.split(r"\n\s*\n", block) if s.strip()]
    assert len(statements) == 2
    for sql in statements:
        guard.check(sql, live_ids={"r_8c1f0a9d3e", "r_51b0c2d4e6"})
