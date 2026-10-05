"""The complete example in WRITING_A_PLUGIN.md runs as written: it
registers, lists under its capability, and pages through a (mocked) API."""

from __future__ import annotations

import pathlib
import sys
import types

import httpx
from coresupport import run

from marketlens_mcp import config as cfg
from marketlens_mcp import registry
from marketlens_mcp.plugin_api import FetchLimits
from marketlens_mcp.testing import call_tool, make_context

DOC = pathlib.Path(__file__).resolve().parents[2] / "WRITING_A_PLUGIN.md"


def load_example() -> types.ModuleType:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## A complete example", 1)[1]
    code = section.split("```python", 1)[1].split("```", 1)[0]
    module = types.ModuleType("my_plugin_from_doc")
    sys.modules[module.__name__] = module  # pydantic resolves annotations through sys.modules
    exec(compile(code, str(DOC), "exec"), module.__dict__)  # noqa: S102 - our own documentation
    return module


def test_the_documented_plugin_registers(tmp_path):
    mod = load_example()

    class EP:
        name, value, distribution = "myplugin", "my_plugin:PLUGIN", "marketlens-myplugin"

        def load(self):
            return mod.PLUGIN

    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("plugins:\n  enabled: [myplugin]\n")
    cat = registry.build_catalog(cfg.load_config(), discover=lambda: [EP()])
    status = next(s for s in cat.plugins if s.name == "myplugin")
    assert status.loaded, status.error
    assert cat.tools["myplugin_daily_prices"].env == ("EXAMPLE_API_TOKEN",)
    assert "myplugin.DailyPrice" in cat.models


def test_the_documented_tool_pages_through_the_api(store):
    mod = load_example()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        assert request.headers["authorization"] == "Bearer test-token"
        if "page_token" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "bars": [{"t": "2026-01-02T21:00:00Z", "c": 101.5, "v": 1200}],
                    "next_page_token": "p2",
                },
            )
        return httpx.Response(200, json={"bars": [{"t": "2026-01-05T21:00:00Z", "c": 102.0}]})

    mod.TRANSPORT = httpx.MockTransport(handler)
    ctx = make_context(store=store, env={"EXAMPLE_API_TOKEN": "test-token"}, limits=FetchLimits(max_rows=100))
    out = run(call_tool(mod.DAILY_PRICES, ctx, ticker="brk.b"))
    assert [r["ticker"] for r in out.rows] == ["BRK-B", "BRK-B"]
    assert out.rows[1]["volume"] is None and out.rows[1]["absent"] == {"volume": "not_provided_by_source"}
    assert out.pagination.pages_fetched == 2 and out.pagination.complete
    assert seen[0] == {"symbol": "BRK-B", "limit": "100"}
    assert out.provenance.provider == "example-prices"
