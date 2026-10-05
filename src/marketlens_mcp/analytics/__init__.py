"""The built-in analytics tools: returns, rolling volatility, correlation,
resample, as-of align, drawdown and beta, computed locally in DuckDB on result
handles of the session (nothing is loaded into the model's context).

Owner: ml-analytics. Registered by ``marketlens_mcp.builtins`` through
``PLUGIN`` under the built-in ``analytics`` capability (on by default). The
tools use only the results_api protocols (``ToolContext.results``); they make
no upstream call and need no environment variable. Importing this package
performs no I/O.
"""

from __future__ import annotations

from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, PluginContext, PluginInfo, Registry

from .tools import all_specs


def register(registry: Registry, ctx: PluginContext) -> None:
    """Add the seven analytics tools. The capability and the output models are
    built-in, so nothing else is declared here."""
    for spec in all_specs():
        registry.add_tool(spec)


PLUGIN = PluginInfo(
    name="analytics",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description="Returns, volatility, correlation, resample, as-of align, drawdown and beta on stored results.",
)
