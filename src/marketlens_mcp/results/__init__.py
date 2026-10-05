"""The session result store and the results_* tools (a built-in plugin).

The store's interface is ``marketlens_mcp.results_api``; the implementation
is ``results.store``. This module registers the results tools.
"""

from __future__ import annotations

from ..plugin_api import PLUGIN_API_VERSION, PluginContext, PluginInfo, Registry


def register(registry: Registry, ctx: PluginContext) -> None:
    from .tools import SPECS

    for spec in SPECS:
        registry.add_tool(spec)


PLUGIN = PluginInfo(
    name="results",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description="Query, describe, sample, list, drop and export results stored by this session.",
)
