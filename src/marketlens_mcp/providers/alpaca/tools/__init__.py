"""The Alpaca tool modules. Each module exports ``SPECS: tuple[ToolSpec, ...]``;
``MODULES`` lists them in registration order (the scaffold prints a reminder to
add a new module here)."""

from __future__ import annotations

import importlib

from marketlens_mcp.plugin_api import ToolSpec

MODULES: tuple[str, ...] = (
    "stocks",
    "crypto",
    "options",
    "fixed_income",
    "news",
    "reference",
    "portfolio",
    "provider_docs",
)


def all_specs() -> tuple[ToolSpec, ...]:
    out: list[ToolSpec] = []
    for name in MODULES:
        out.extend(importlib.import_module(f"{__name__}.{name}").SPECS)
    return tuple(out)
