"""The built-in official provider, two live tools: ``news_official`` (the
built-in ``news`` capability), headlines from government and central-bank
feeds (Federal Reserve Board, SEC, BLS, BEA, CFTC, ECB), and
``filings_search`` (``filings``), SEC EDGAR's full-text search, the one
other fixed URL.

Registered by ``marketlens_mcp.builtins`` through ``PLUGIN``. Keyless and
read-only: every request is a GET to a URL of the fixed allow-list in
``feeds`` or to ``search.SEARCH_URL``; SEC and BLS are asked only with the
contact SEC_CONTACT_EMAIL names. Importing this package performs no I/O.
"""

from __future__ import annotations

from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, PluginContext, PluginInfo, Registry

from . import search, tool


def register(registry: Registry, ctx: PluginContext) -> None:
    """Add news_official and filings_search. Their models (NewsItem,
    FilingSearchHit) are built in."""
    # filings is declared by the data provider, registered before this one, whether or not the extra is installed
    for spec in (*tool.SPECS, *search.SPECS):
        registry.add_tool(spec)


PLUGIN = PluginInfo(
    name="official",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description=(
        "Official headlines from the Federal Reserve Board, SEC, BLS, BEA, CFTC and ECB feeds, and SEC EDGAR "
        "full-text search."
    ),
    env=("SEC_CONTACT_EMAIL",),
)
