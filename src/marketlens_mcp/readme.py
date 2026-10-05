"""The README tool table (contract 9.1), generated from the manifest.

``builtin_table()`` is what README.md holds between the markers (the built-in
specs under their default capabilities, whatever the owner's config says);
``marketlens-mcp tools --markdown`` prints the same table for the installed
configuration.
"""

from __future__ import annotations

import copy
import pathlib
from collections.abc import Iterable, Mapping, Sequence

from .config import DEFAULTS, Config
from .manifest import output_model_name
from .plugin_api import CapabilitySpec
from .registry import ToolEntry, build_catalog

START = "<!-- tools:start -->"
END = "<!-- tools:end -->"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def tool_table(
    entries: Iterable[ToolEntry],
    capabilities: Sequence[CapabilitySpec],
    *,
    states: Mapping[str, bool] | None = None,
    show_disabled: bool = False,
) -> str:
    """Markdown table. Without ``states`` (README mode) the capability column
    shows each capability's default; with ``states`` (installed mode) it shows
    the capability, marked "(off)" for disabled tools listed by
    ``show_disabled``."""
    order = {c.id: i for i, c in enumerate(capabilities)}
    defaults = {c.id: (c.default_enabled or c.locked) for c in capabilities}
    rows = []
    for e in sorted(entries, key=lambda e: (order.get(e.spec.capability, len(order)), e.spec.name)):
        spec = e.spec
        if states is None:
            cap = f"{spec.capability} ({'on' if defaults.get(spec.capability) else 'off'})"
        else:
            enabled = states.get(spec.capability, False)
            if not enabled and not show_disabled:
                continue
            cap = spec.capability if enabled else f"{spec.capability} (off)"
        notes = "untrusted text" if spec.output_risk == "external_text" else spec.readme
        env = ", ".join(e.env) if e.env else "-"
        rows.append(
            f"| `{spec.name}` | {_cell(cap)} | `{_cell(spec.route)}` ({_cell(spec.provider)}) | "
            f"{_cell(output_model_name(spec))} | {_cell(env)} | {_cell(notes)} |"
        )
    header = "| Tool | Capability (default) | " if states is None else "| Tool | Capability | "
    header += "Provider route | Returns | Env vars | Notes |"
    return "\n".join([header, "|---|---|---|---|---|---|", *rows])


def default_config() -> Config:
    """Every setting at its default, no file (README and schema generation)."""
    return Config(path=pathlib.Path("defaults"), exists=False, data=copy.deepcopy(DEFAULTS))


def builtin_table() -> str:
    cat = build_catalog(default_config(), discover=list)
    return tool_table(cat.tools.values(), cat.capabilities)


def replace_table(text: str, table: str) -> str:
    if START not in text or END not in text or text.index(START) > text.index(END):
        raise ValueError(f"README has no {START} ... {END} markers")
    head, _, rest = text.partition(START)
    _, _, tail = rest.partition(END)
    return f"{head}{START}\n{table}\n{END}{tail}"
