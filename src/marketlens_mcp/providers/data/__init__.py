"""The built-in data provider: official macro, SEC EDGAR, Fed and Treasury
data, US market holidays, and the Nasdaq calendars, through marketlens-data
(import name ``omni``), installed with the ``marketlens-mcp[data]`` extra.

Registered by ``marketlens_mcp.builtins`` through ``PLUGIN``. The five
capabilities are always declared (a config that names them is never refused);
the 25 tools are added only when omni is importable. Importing this package
imports no omni and performs no I/O.
"""

from __future__ import annotations

from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, PluginContext, PluginInfo, Registry

from . import runtime
from .capabilities import CAPABILITIES
from .tools import all_specs


def register(registry: Registry, ctx: PluginContext) -> None:
    for spec in CAPABILITIES:
        registry.add_capability(spec)
    if not runtime.available():
        return
    for spec in all_specs():
        registry.add_tool(spec)


PLUGIN = PluginInfo(
    name="data",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description=(
        "Official macro, SEC EDGAR, Fed and Treasury data, US market holidays, and the Nasdaq calendars, "
        "through marketlens-data."
    ),
    env=(
        "FRED_API_KEY",
        "BLS_API_KEY",
        "BEA_API_KEY",
        "SEC_CONTACT_EMAIL",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "OMNI_DATA_DIR",
    ),
)
