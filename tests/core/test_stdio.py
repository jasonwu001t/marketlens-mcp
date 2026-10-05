"""The real console entry over stdio, in a child process: the bare command
serves MCP, stdout carries only the protocol (no banner), tools list."""

from __future__ import annotations

import os
import pathlib
import sys

from coresupport import run
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_bare_command_serves_over_stdio(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(SRC),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "MARKETLENS_CACHE_DIR": str(tmp_path / "cache"),
        "MARKETLENS_LOG_LEVEL": "WARNING",
    }
    transport = StdioTransport(
        command=sys.executable, args=["-m", "marketlens_mcp"], env=env, cwd=str(tmp_path)
    )

    async def go():
        async with Client(transport) as c:
            tools = [t.name for t in await c.list_tools()]
            listed = await c.call_tool("results_list", {})
            return tools, listed.structured_content

    tools, listed = run(go())
    assert "results_query" in tools and "results_export" not in tools
    assert listed["_marketlens"]["tool"] == "results_list"
    assert listed["data"]["rows"] == []
