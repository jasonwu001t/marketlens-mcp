"""No telemetry (contract 7.2): building the server, listing tools and
running results tools make no outbound connection of any kind."""

from __future__ import annotations

import socket

import httpx
import ml_example_plugin as ex
from coresupport import run
from fastmcp import Client

from marketlens_mcp import server


class EP:
    name, value, distribution = "example", "x:example", "example-dist"

    def load(self):
        return ex.PLUGIN


def test_no_connection_is_attempted(tmp_path, monkeypatch):
    attempts = []

    def record(*args, **kwargs):
        attempts.append((args, kwargs))
        raise RuntimeError("outbound connection attempted")

    monkeypatch.setattr(socket.socket, "connect", record)
    monkeypatch.setattr(socket.socket, "connect_ex", record)
    monkeypatch.setattr(socket, "create_connection", record)
    monkeypatch.setattr(socket, "getaddrinfo", record)
    monkeypatch.setattr(httpx.Client, "send", record)
    monkeypatch.setattr(httpx.AsyncClient, "send", record)

    p = tmp_path / "xdg" / "marketlens" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("plugins:\n  enabled: [example]\ncapabilities:\n  example: true\n", encoding="utf-8")
    rt = server.build_runtime(discover=lambda: [EP()])
    mcp = server.build_server(rt)

    async def go():
        async with Client(mcp) as c:
            await c.list_tools()
            stored = (await c.call_tool("example_things", {"count": 500})).structured_content["data"]
            await c.call_tool("results_query", {"sql": f"SELECT count(*) FROM {stored['result_id']}"})
            await c.call_tool("results_sample", {"result_id": stored["result_id"], "method": "random"})
            await c.call_tool("results_describe", {"result_id": stored["result_id"]})
            await c.call_tool("results_list", {})
            await c.call_tool("results_drop", {"result_id": stored["result_id"]})

    run(go())
    assert attempts == []
