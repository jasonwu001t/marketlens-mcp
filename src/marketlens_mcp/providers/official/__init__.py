"""The built-in official provider: headlines from government and central-bank
feeds (Federal Reserve Board, SEC, BLS, BEA, CFTC, ECB), one live tool,
``news_official``, under the built-in ``news`` capability.

Registered by ``marketlens_mcp.builtins`` through ``PLUGIN``. Keyless and
read-only: every request is a GET to a URL of the fixed allow-list in
``feeds``; SEC and BLS are asked only with the contact SEC_CONTACT_EMAIL
names. Importing this package performs no I/O.
"""

from __future__ import annotations

from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, PluginContext, PluginInfo, Registry

from .tool import SPECS


def register(registry: Registry, ctx: PluginContext) -> None:
    """Add news_official. Its capability (news) and model (NewsItem) are built in."""
    for spec in SPECS:
        registry.add_tool(spec)


PLUGIN = PluginInfo(
    name="official",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description="Official headlines from the Federal Reserve Board, SEC, BLS, BEA, CFTC and ECB feeds.",
    env=("SEC_CONTACT_EMAIL",),
)
