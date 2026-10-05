"""The MCP server end to end over an in-memory client: only enabled tools are
listed, schemas are top-level and closed, annotations are read-only, every
result is enveloped, refusals are readable, and no path leaks."""

from __future__ import annotations

import json
import re

import ml_example_plugin as ex
import pytest
from coresupport import bar_table, provenance, run
from fastmcp import Client

from marketlens_mcp import server
from marketlens_schema.market import Bar


class EP:
    def __init__(self, name, obj):
        self.name, self.value, self.distribution, self._obj = name, f"x:{name}", "example-dist", obj

    def load(self):
        return self._obj


def runtime(tmp_path, text="", eps=()):
    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return server.build_runtime(discover=lambda: list(eps))


async def session(rt, fn):
    mcp = server.build_server(rt)
    async with Client(mcp) as client:
        return await fn(client)


def test_lists_only_enabled_tools_with_closed_schemas(tmp_path):
    rt = runtime(tmp_path)

    async def go(c):
        return await c.list_tools()

    tools = {t.name: t for t in run(session(rt, go))}
    assert {"results_query", "results_describe", "results_sample", "results_list", "results_drop"} <= set(
        tools
    )
    assert "results_export" not in tools  # results.export is off by default
    q = tools["results_query"]
    assert q.inputSchema["additionalProperties"] is False
    assert set(q.inputSchema["properties"]) == {"sql", "max_rows", "store"}
    assert q.annotations.readOnlyHint is True and q.annotations.destructiveHint is False
    assert q.annotations.idempotentHint is True and q.annotations.openWorldHint is False
    assert q.title == "Query stored results"
    assert q.outputSchema is None
    assert set(tools) == set(server.enabled_tool_names(rt))


def test_plugin_tools_follow_the_policy(tmp_path):
    rt = runtime(tmp_path, "plugins:\n  enabled: [example]\n", eps=[EP("example", ex.PLUGIN)])
    assert "example_things" not in server.enabled_tool_names(rt)  # capability "example" is off by default
    rt = runtime(
        tmp_path,
        "plugins:\n  enabled: [example]\ncapabilities:\n  example: true\n",
        eps=[EP("example", ex.PLUGIN)],
    )

    async def go(c):
        names = [t.name for t in await c.list_tools()]
        res = await c.call_tool("example_things", {"count": 3})
        return names, res

    names, res = run(session(rt, go))
    assert "example_things" in names
    env = res.structured_content
    assert env["_marketlens"]["tool"] == "example_things"
    assert env["_marketlens"]["risk"] == "api_structured"
    assert env["data"]["kind"] == "inline" and env["data"]["row_count"] == 3
    assert json.loads(res.content[0].text) == env


def test_results_round_trip_over_mcp(tmp_path):
    rt = runtime(
        tmp_path,
        "plugins:\n  enabled: [example]\ncapabilities:\n  example: true\n",
        eps=[EP("example", ex.PLUGIN)],
    )

    async def go(c):
        stored = (await c.call_tool("example_things", {"count": 900})).structured_content["data"]
        q = await c.call_tool(
            "results_query",
            {"sql": f"SELECT name, count(*) AS n FROM {stored['result_id']} GROUP BY name ORDER BY name"},
        )
        bad = await c.call_tool(
            "results_query", {"sql": "SELECT * FROM read_csv('/etc/passwd')"}, raise_on_error=False
        )
        gone = await c.call_tool("results_describe", {"result_id": "r_0000000000"}, raise_on_error=False)
        args = await c.call_tool("results_query", {"sql": "SELECT 1", "bogus": 1}, raise_on_error=False)
        listed = await c.call_tool("results_list", {})
        return stored, q.structured_content, bad, gone, args, listed.structured_content

    stored, q, bad, gone, args, listed = run(session(rt, go))
    assert stored["kind"] == "stored" and stored["row_count"] == 900
    assert q["data"]["rows"] == [
        {"name": "thing0", "n": 300},
        {"name": "thing1", "n": 300},
        {"name": "thing2", "n": 300},
    ]
    assert bad.is_error and bad.structured_content["error"]["code"] == "sql_function"
    assert gone.is_error and gone.structured_content["error"]["code"] == "result_unknown"
    assert args.is_error and args.structured_content["error"]["code"] == "invalid_arguments"
    assert listed["data"]["rows"][0]["result_id"] == stored["result_id"]
    everything = json.dumps([stored, q, bad.structured_content, gone.structured_content, listed])
    assert str(tmp_path) not in everything
    assert not re.search(r'"/(?:private|var|Users|home|tmp)/', everything)


def test_sessions_are_isolated(tmp_path):
    rt = runtime(tmp_path)
    mcp = server.build_server(rt)

    async def go():
        info = None
        async with Client(mcp) as a:
            # put a result into session A through the store the server uses for it
            await a.call_tool("results_list", {})
            key = next(iter(rt.store_root._sessions))
            info = rt.store_root.session(key).put(
                bar_table(n=10), tool="market_bars", model=Bar.schema_name, provenance=provenance()
            )
            seen_a = (await a.call_tool("results_list", {})).structured_content["data"]["rows"]
        async with Client(mcp) as b:
            seen_b = (await b.call_tool("results_list", {})).structured_content["data"]["rows"]
            other = await b.call_tool("results_describe", {"result_id": info.result_id}, raise_on_error=False)
        return info, seen_a, seen_b, other

    info, seen_a, seen_b, other = run(go())
    assert [r["result_id"] for r in seen_a] == [info.result_id]
    assert seen_b == []
    assert other.structured_content["error"]["code"] == "result_unknown"


def test_disabled_tool_called_by_name_is_refused_r14(tmp_path):
    rt = runtime(tmp_path)

    async def go(c):
        return await c.call_tool(
            "results_export", {"result_id": "r_0123456789", "format": "csv"}, raise_on_error=False
        )

    res = run(session(rt, go))
    assert res.is_error
    assert res.structured_content["error"] == {
        "code": "capability_disabled",
        "message": "Tool 'results_export' is not enabled: its capability 'results.export' is off in the marketlens config.",
        "hint": None,
        "retryable": False,
    }


def test_unknown_tool_is_an_enveloped_error(tmp_path):
    rt = runtime(tmp_path)

    async def go(c):
        return await c.call_tool("place_stock_order", {"symbol": "AAPL"}, raise_on_error=False)

    res = run(session(rt, go))
    assert res.is_error and res.structured_content["error"]["code"] == "unknown_tool"


def test_startup_notices(tmp_path):
    rt = runtime(tmp_path, "portfolio:\n  environment: live\nplugins:\n  enabled: [ghost]\n")
    notices = server.startup_notices(rt)
    assert "portfolio.environment is live: portfolio tools read your LIVE brokerage account." in notices
    assert "plugin 'ghost' not loaded: not installed" in notices


def test_tools_are_grouped_into_one_sub_server_per_capability(tmp_path):
    rt = runtime(tmp_path)
    mcp = server.build_server(rt)
    # The built-ins at their defaults: portfolio and provider.docs are off, so
    # neither has a sub-server (and no tool of theirs is listed).
    assert {s.name for s in server.sub_servers(mcp)} == {
        "marketlens-analytics",
        "marketlens-market",
        "marketlens-news",
        "marketlens-reference",
        "marketlens-results",
    }


@pytest.mark.parametrize("text", ["capabilities:\n  bogus: true\n", "nonsense: 1\n"])
def test_bad_config_refuses_to_build(tmp_path, text):
    from marketlens_mcp.config import ConfigError

    with pytest.raises(ConfigError):
        runtime(tmp_path, text)
