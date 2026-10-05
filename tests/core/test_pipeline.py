"""validate -> handler -> offload, as the server runs every tool, through
marketlens_mcp.testing (the same code path)."""

from __future__ import annotations

import dataclasses
import datetime as dt

import ml_example_plugin as ex
import pytest
from coresupport import FIXED_NOW, bar_table, provenance, run

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput
from marketlens_mcp.results_api import InlineResult, ResultMarker
from marketlens_mcp.testing import call_tool, make_context
from marketlens_schema import Environment
from marketlens_schema.market import Bar


def test_make_context_defaults(store):
    ctx = make_context(store=store, now=FIXED_NOW, env={"A": "1"}, capabilities=["market"], settings={"x": 1})
    assert isinstance(ctx, ToolContext)
    assert ctx.tool == "test_tool"
    assert ctx.now() == FIXED_NOW
    assert ctx.env("A") == "1" and ctx.env("B") is None
    assert ctx.capabilities == frozenset({"market"})
    assert ctx.settings == {"x": 1}
    assert ctx.portfolio_environment is Environment.PAPER
    assert ctx.results is store
    assert ctx.session_id


def test_make_context_without_store_creates_one_under_the_cache_dir(tmp_path):
    ctx = make_context()
    info = ctx.results.put(bar_table(n=5), tool="t", model="marketlens.Bar", provenance=provenance())
    assert ctx.results.info(info.result_id).row_count == 15
    assert (tmp_path / "cache" / "results").is_dir()


def test_invalid_arguments_are_readable(store):
    ctx = make_context(store=store)
    with pytest.raises(ToolError) as info:
        run(call_tool(ex.THINGS, ctx, count=0))
    assert info.value.code == "invalid_arguments"
    assert "count" in info.value.message and "example_things" in info.value.message
    with pytest.raises(ToolError) as info:
        run(call_tool(ex.THINGS, ctx, colour="red"))
    assert "colour" in info.value.message


def test_unexpected_exceptions_become_internal_error(store, caplog):
    async def broken(ctx, args):
        raise ValueError(f"secret path {store.root}/x")

    spec = dataclasses.replace(ex.THINGS, handler=broken)
    with pytest.raises(ToolError) as info:
        run(call_tool(spec, make_context(store=store), count=1))
    assert info.value.code == "internal_error"
    assert info.value.message == "The tool failed unexpectedly; see the server log."
    assert str(store.root) not in info.value.message


def test_tool_error_messages_are_scrubbed(store):
    async def leaky(ctx, args):
        raise ToolError("x", f"could not open {store.root}/results/a.parquet", hint="see /etc/hosts")

    spec = dataclasses.replace(ex.THINGS, handler=leaky)
    with pytest.raises(ToolError) as info:
        run(call_tool(spec, make_context(store=store), count=1))
    assert str(store.root) not in info.value.message
    assert info.value.message == "could not open <path>"
    assert info.value.hint == "see <path>"


def test_large_handler_output_is_stored(store):
    async def big(ctx, args):
        return ToolOutput(model=Bar, provenance=provenance(), table=bar_table(n=300))

    spec = dataclasses.replace(ex.THINGS, handler=big, output_model=Bar)
    out = run(call_tool(spec, make_context(store=store), count=1))
    assert isinstance(out, ResultMarker) and out.row_count == 900


def test_handler_must_return_tool_output(store):
    async def wrong(ctx, args):
        return {"rows": []}

    spec = dataclasses.replace(ex.THINGS, handler=wrong)
    with pytest.raises(ToolError) as info:
        run(call_tool(spec, make_context(store=store), count=1))
    assert info.value.code == "internal_error"


def test_result_not_found_becomes_r15(store):
    async def missing(ctx, args):
        ctx.results.info("r_0123456789")

    spec = dataclasses.replace(ex.THINGS, handler=missing)
    with pytest.raises(ToolError) as info:
        run(call_tool(spec, make_context(store=store), count=1))
    assert info.value.code == "result_unknown"
    assert info.value.message == (
        "Result r_0123456789 is unknown. Results live 24 hours, only in the session that created them; "
        "fetch the data again."
    )


def test_inline_output_from_example(store):
    out = run(call_tool(ex.THINGS, make_context(store=store, now=FIXED_NOW), count=2))
    assert isinstance(out, InlineResult)
    assert out.provenance.fetched_at == FIXED_NOW
    assert out.rows[0] == {
        "name": "thing0",
        "t": "2026-01-01T00:00:00Z",
        "size": None,
        "absent": {"size": "no_data"},
    }
    assert out.rows[1]["t"] == (dt.datetime(2026, 1, 1, 0, 1, tzinfo=dt.UTC)).isoformat().replace(
        "+00:00", "Z"
    )
