"""The built-in Alpaca provider: read-only market data, reference data, news
and brokerage-account reads, mapped onto the canonical schema.

Owner: ml-alpaca. Registered by ``marketlens_mcp.builtins`` through ``PLUGIN``.
Every tool is a GET; Alpaca's write operations are listed in ``exclusions`` and
never registered. Importing this package performs no I/O.
"""

from __future__ import annotations

from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, PluginContext, PluginInfo, Registry

from .tools import all_specs


def register(registry: Registry, ctx: PluginContext) -> None:
    """Add every Alpaca read tool. Capabilities and models are built-in, so
    nothing else is declared here."""
    for spec in all_specs():
        registry.add_tool(spec)


PLUGIN = PluginInfo(
    name="alpaca",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description="Alpaca market data, reference data, news and brokerage-account reads.",
    env=("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
)
