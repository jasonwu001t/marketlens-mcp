"""The data tool modules. Each exports ``SPECS: tuple[ToolSpec, ...]`` and
``DATASETS`` (tool name -> the marketlens-data dataset ids it reads; checked
against ``parity.PARITY``). Importing them imports no omni."""

from __future__ import annotations

import importlib

from marketlens_mcp.plugin_api import ToolSpec

MODULES: tuple[str, ...] = ("macro", "sec", "fed_treasury", "calendars")


def all_specs() -> tuple[ToolSpec, ...]:
    out: list[ToolSpec] = []
    for name in MODULES:
        out.extend(importlib.import_module(f"{__name__}.{name}").SPECS)
    return tuple(out)


def datasets_by_tool() -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for name in MODULES:
        out.update(importlib.import_module(f"{__name__}.{name}").DATASETS)
    return out
