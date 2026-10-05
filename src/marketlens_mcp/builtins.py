"""The built-in providers, registered in this order through the same plugin
API as installed plugins (contract 3.2). A built-in that fails to register
stops the server."""

from __future__ import annotations

BUILTIN_PLUGINS: tuple[str, ...] = (
    "marketlens_mcp.results:PLUGIN",  # results_* tools (always-on session results)
    "marketlens_mcp.providers.alpaca:PLUGIN",  # Alpaca market, reference, news, portfolio reads
    "marketlens_mcp.analytics:PLUGIN",  # analytics_* tools on stored results
    "marketlens_mcp.providers.data:PLUGIN",  # official data through marketlens-data (the data extra)
)
