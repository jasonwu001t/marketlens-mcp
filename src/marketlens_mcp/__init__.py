"""marketlens-mcp: a read-only MCP server for market data and analytics.

Owner: ml-core. Importing this package must not import fastmcp, open files or
touch the network (plugins import plugin_api and results_api from here).
"""

from __future__ import annotations

from .plugin_api import ENTRY_POINT_GROUP, PLUGIN_API_VERSION

__version__ = "0.1.0"

__all__ = ["ENTRY_POINT_GROUP", "PLUGIN_API_VERSION", "__version__"]
