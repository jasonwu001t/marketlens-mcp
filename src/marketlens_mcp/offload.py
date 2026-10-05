"""Inline or stored (contract 5.2).

After a handler returns, its rows become Arrow in the canonical types. The
result is returned inline iff it has at most ``inline_max_rows`` rows (200)
and its JSON is at most ``inline_max_tokens`` estimated tokens (bytes / 4,
6,000), unless the handler asked for ``offload="always"`` (store) or
``"never"`` (inline; only for small results bounded by construction).
Otherwise it is stored and the model gets the marker. A truncated upstream
fetch carries the R16 note.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from marketlens_schema import BUILTIN_MODELS, SCHEMA_VERSION, CanonicalModel, PaginationState, Provenance

from .plugin_api import FetchLimits, ToolOutput
from .results.arrow import (
    column_infos,
    column_infos_for,
    column_infos_from_schema,
    field_names,
    json_safe,
    rows_to_table,
    table_rows,
)
from .results.store import StoreLimits
from .results_api import InlineResult, OutputRisk, ResultStore, ToolResponse


def estimate_tokens(rows: Sequence[Mapping[str, Any]]) -> int:
    """ceil(len(JSON bytes) / 4)."""
    return math.ceil(len(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) / 4)


def truncation_note(tool: str, pagination: PaginationState, limits: FetchLimits) -> str:
    """R16."""
    return (
        f"Stopped after {pagination.pages_fetched} pages and {pagination.rows_fetched} rows (limits "
        f"fetch.max_pages={limits.max_pages}, fetch.max_rows={limits.max_rows}). The data is incomplete; call "
        f'{tool} again with page_token="{pagination.next_page_token}" to continue.'
    )


def _row_json(row: CanonicalModel) -> dict[str, Any]:
    data = row.model_dump(mode="json")
    out = {name: json_safe(data.get(name)) for name in field_names(type(row))}
    if out.get("absent") is None:
        out.pop("absent", None)
    return out


def _resolve_model(
    out: ToolOutput, models: Mapping[str, type[CanonicalModel]]
) -> tuple[str, type[CanonicalModel] | None]:
    model = out.model
    if isinstance(model, type):
        return model.schema_name, model
    if out.rows:
        cls = type(out.rows[0])
        return model, cls
    return model, models.get(model) or BUILTIN_MODELS.get(model)


def finalize(
    out: ToolOutput,
    *,
    tool: str,
    default_risk: OutputRisk,
    store: ResultStore,
    limits: FetchLimits,
    models: Mapping[str, type[CanonicalModel]] | None = None,
) -> ToolResponse:
    """Turn a handler's ToolOutput into the response the model sees."""
    given = [x for x in (out.rows, out.table, out.stored) if x is not None]
    if len(given) != 1:
        raise ValueError("a ToolOutput must carry exactly one of rows, table or stored")
    notes = list(out.notes)
    provenance: Provenance = out.provenance
    p = out.pagination
    # R16 tells the model to continue with the page token; without one (a result
    # read at once and cut at fetch.max_rows) the handler's own note stands.
    if p is not None and not p.complete and (p.row_cap_hit or p.page_cap_hit) and p.next_page_token:
        note = truncation_note(tool, p, limits)
        if note not in notes:
            notes.append(note)
        if not provenance.truncated:
            provenance = provenance.model_copy(update={"truncated": True, "truncation_note": note})
    marker = getattr(store, "marker", None)
    if out.stored is not None:
        if marker is None:
            raise ValueError("this result store cannot build markers")
        return marker(out.stored.result_id, notes=notes)

    risk: OutputRisk = out.risk or default_risk
    store_limits: StoreLimits = getattr(store, "limits", None) or StoreLimits()
    all_models = dict(models or getattr(store, "models", None) or BUILTIN_MODELS)
    name, cls = _resolve_model(out, all_models)

    if out.rows is not None:
        if cls is None:
            raise ValueError(f"rows need a canonical model; {name!r} is not registered")
        row_count = len(out.rows)
        table: pa.Table | None = None
        json_rows = (
            [_row_json(r) for r in out.rows]
            if row_count <= store_limits.inline_max_rows or out.offload == "never"
            else None
        )
    else:
        table = out.table
        row_count = table.num_rows
        json_rows = (
            table_rows(table) if row_count <= store_limits.inline_max_rows or out.offload == "never" else None
        )

    inline = out.offload == "never" or (
        out.offload == "auto"
        and json_rows is not None
        and row_count <= store_limits.inline_max_rows
        and estimate_tokens(json_rows) <= store_limits.inline_max_tokens
    )
    if inline:
        if table is None or cls is None:
            columns = column_infos(cls) if cls is not None else column_infos_from_schema(table.schema)
        elif table.column_names == field_names(cls):
            columns = column_infos(cls)
        else:
            columns = column_infos_for(table.schema, cls)
        return InlineResult(
            tool=tool,
            model=name,
            schema_version=SCHEMA_VERSION,
            row_count=row_count,
            columns=columns,
            rows=json_rows or [],
            provenance=provenance,
            absent=dict(out.absent),
            pagination=out.pagination,
            notes=notes,
        )
    if table is None:
        table = rows_to_table(out.rows, cls)
    info = store.put(
        table,
        tool=tool,
        model=name,
        provenance=provenance,
        absent=dict(out.absent),
        pagination=out.pagination,
        risk=risk,
        parents=list(provenance.derived_from),
        time_column=cls.time_column if cls else None,
        group_column=cls.group_column if cls else None,
    )
    if marker is None:
        raise ValueError("this result store cannot build markers")
    return marker(info.result_id, notes=notes)
