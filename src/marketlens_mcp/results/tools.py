"""The results tools (contract 5.5): query, describe, sample, list and drop
(capability ``results``, always on) and export (capability
``results.export``, off by default; the only tool that writes a file)."""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import secrets
from typing import Literal

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketlens_schema import QUERY_ROW_SCHEMA_NAME, Delay, Provenance

from ..offload import estimate_tokens
from ..plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from ..results_api import RESULT_ID_RE
from .arrow import duckdb_type, json_safe, sql_ident, table_rows
from .store import FileResultStore, QueryOutcome

GOLDEN = "tests/core/test_results_tools.py"
RESULT_ID_FIELD = Field(
    pattern=RESULT_ID_RE.pattern, description="A result_id from this session, e.g. r_8c1f0a9d3e"
)
LIST_MAX = 200


def _store(ctx: ToolContext) -> FileResultStore:
    store = ctx.results
    if not isinstance(store, FileResultStore):
        raise ToolError("internal_error", "The results tools need marketlens's own result store.")
    return store


def _provenance(ctx: ToolContext, route: str, request: dict, derived_from: list[str]) -> Provenance:
    return Provenance(
        provider="marketlens",
        route=route,
        fetched_at=ctx.now(),
        delay=Delay.UNKNOWN,
        request=request,
        derived_from=derived_from,
    )


# --- results_query ------------------------------------------------------------------------------


class QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(
        description="One read-only SELECT (or WITH ... SELECT) over this session's results; use each "
        "result_id as a table name, e.g. SELECT ticker, max(close) FROM r_8c1f0a9d3e GROUP BY ticker."
    )
    max_rows: int = Field(
        50, ge=1, le=200, description="Rows to return (1-200). A LIMIT above it is lowered."
    )
    store: bool = Field(
        False,
        description="Keep the full answer (up to fetch.max_rows rows) as a new result and return its marker.",
    )


def _inherited(store: FileResultStore, out: QueryOutcome) -> dict:
    """What a stored answer over one parent keeps of it: the parent's time and
    series columns and its column units, only for columns that are still there
    with the same name and type, and only when the time column is one of them;
    so the answer chains into analytics as the parent would."""
    if len(out.result_ids) != 1:
        return {}
    parent = store.info(out.result_ids[0])
    types = {f.name: duckdb_type(f.type) for f in out.table.schema}
    same = {c.name: c for c in parent.columns if types.get(c.name) == c.type}
    if parent.time_column not in same:
        return {}
    return {
        "time_column": parent.time_column,
        "group_column": parent.group_column if parent.group_column in same else None,
        "column_units": {n: c.unit for n, c in same.items() if c.unit},
    }


async def results_query(ctx: ToolContext, args: QueryArgs) -> ToolOutput:
    store = _store(ctx)
    limits = store.limits
    if args.max_rows > limits.query_max_rows:
        raise ToolError(
            "invalid_arguments", f"max_rows must be at most {limits.query_max_rows} in this configuration."
        )
    out = store.query(args.sql, max_rows=args.max_rows, store=args.store)
    cap = limits.fetch_max_rows if args.store else args.max_rows
    notes = [f"Executed: {out.executed_sql}"]
    if out.truncated:
        notes.append(
            f"Truncated: yes; more than {cap} rows matched and only the first {cap} are kept. Aggregate, filter, "
            "or use store=true to keep the full answer as a new result."
        )
    else:
        notes.append("Truncated: no")
    request = {"sql": args.sql[:4000], "max_rows": args.max_rows, "store": args.store}
    prov = _provenance(ctx, "duckdb:results_query", request, list(out.result_ids))
    rows = table_rows(out.table) if not args.store else None
    keep = args.store or (
        rows is not None
        and (
            len(json.dumps(rows, separators=(",", ":")).encode()) > limits.query_max_bytes
            or estimate_tokens(rows) > limits.inline_max_tokens
        )
    )
    if keep and not args.store:
        notes.append(
            f"The answer is larger than results.query_max_bytes ({limits.query_max_bytes} bytes), so it was "
            "stored as a new result."
        )
    if keep:
        info = store.put(
            out.table,
            tool="results_query",
            model=QUERY_ROW_SCHEMA_NAME,
            provenance=prov,
            risk=out.risk,
            parents=list(out.result_ids),
            **_inherited(store, out),
        )
        return ToolOutput(
            model=QUERY_ROW_SCHEMA_NAME, provenance=prov, stored=info, notes=notes, risk=out.risk
        )
    return ToolOutput(
        model=QUERY_ROW_SCHEMA_NAME,
        provenance=prov,
        table=out.table,
        notes=notes,
        risk=out.risk,
        offload="never",
    )


# --- results_describe -------------------------------------------------------------------------


class ResultIdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = RESULT_ID_FIELD


async def results_describe(ctx: ToolContext, args: ResultIdArgs) -> ToolOutput:
    info = _store(ctx).info(args.result_id)
    return ToolOutput(model=info.model, provenance=info.provenance, stored=info, risk=info.risk)


# --- results_sample ---------------------------------------------------------------------------


class SampleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = RESULT_ID_FIELD
    n: int = Field(10, ge=1, le=50, description="Rows to show (1-50)")
    method: Literal["first", "last", "random"] = Field(
        "first", description="first/last by the result's time column (storage order without one), or random"
    )
    columns: list[str] | None = Field(None, min_length=1, max_length=200, description="Only these columns")


async def results_sample(ctx: ToolContext, args: SampleArgs) -> ToolOutput:
    store = _store(ctx)
    info = store.info(args.result_id)
    names = [c.name for c in info.columns]
    if args.columns:
        unknown = [c for c in args.columns if c not in names]
        if unknown:
            raise ToolError(
                "unknown_column",
                f"{', '.join(repr(u) for u in unknown)} not in {args.result_id}. Its columns: {', '.join(names)}.",
            )
    chosen = args.columns or names
    cols = ", ".join(sql_ident(c) for c in chosen)
    rid = args.result_id
    t = sql_ident(info.time_column) if info.time_column else None
    reverse = False
    if args.method == "random":
        sql = f"SELECT {cols} FROM {rid} USING SAMPLE {args.n} ROWS (reservoir, 42)"
    elif args.method == "first":
        sql = (
            f"SELECT {cols} FROM {rid}" + (f" ORDER BY {t} ASC NULLS LAST" if t else "") + f" LIMIT {args.n}"
        )
    elif t:
        sql = f"SELECT {cols} FROM {rid} ORDER BY {t} DESC NULLS LAST LIMIT {args.n}"
        reverse = True
    else:
        sql = f"SELECT {cols} FROM {rid} LIMIT {args.n} OFFSET {max(0, info.row_count - args.n)}"
    with store.open([rid]) as session:
        table = session.query(sql)
    if reverse:
        table = table.take(pa.array(range(table.num_rows - 1, -1, -1)))
    notes = []
    limit = store.limits.query_max_bytes
    shown = table.num_rows
    while (
        shown > 0
        and len(json.dumps(table_rows(table.slice(0, shown)), separators=(",", ":")).encode()) > limit
    ):
        shown -= 1
    if shown < table.num_rows:
        notes.append(
            f"Showing {shown} of {table.num_rows} rows: fewer rows to stay under results.query_max_bytes "
            f"({limit} bytes). Ask for fewer columns, or query the result with results_query."
        )
        table = table.slice(0, shown)
    request = {"result_id": rid, "n": args.n, "method": args.method, "columns": args.columns}
    return ToolOutput(
        model=QUERY_ROW_SCHEMA_NAME,
        provenance=_provenance(ctx, "duckdb:results_sample", request, [rid]),
        table=table,
        notes=notes,
        risk=info.risk,
        offload="never",
    )


# --- results_list -----------------------------------------------------------------------------


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def results_list(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    infos = _store(ctx).list()
    notes = []
    if len(infos) > LIST_MAX:
        notes.append(f"Showing the newest {LIST_MAX} of {len(infos)} results.")
        infos = infos[:LIST_MAX]
    table = pa.table(
        {
            "result_id": [i.result_id for i in infos],
            "tool": [i.tool for i in infos],
            "model": [i.model for i in infos],
            "row_count": pa.array([i.row_count for i in infos], pa.int64()),
            "created_at": pa.array([i.created_at for i in infos], pa.timestamp("us", tz="UTC")),
            "expires_at": pa.array([i.expires_at for i in infos], pa.timestamp("us", tz="UTC")),
            "risk": [i.risk for i in infos],
        }
    )
    return ToolOutput(
        model=QUERY_ROW_SCHEMA_NAME,
        provenance=_provenance(ctx, "store:results_list", {}, []),
        table=table,
        notes=notes,
        offload="never",
    )


# --- results_drop -----------------------------------------------------------------------------


async def results_drop(ctx: ToolContext, args: ResultIdArgs) -> ToolOutput:
    dropped = _store(ctx).drop(args.result_id)
    table = pa.table({"result_id": [args.result_id], "dropped": [dropped]})
    return ToolOutput(
        model=QUERY_ROW_SCHEMA_NAME,
        provenance=_provenance(ctx, "store:results_drop", {"result_id": args.result_id}, []),
        table=table,
        offload="never",
    )


# --- results_export ---------------------------------------------------------------------------


class ExportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = RESULT_ID_FIELD
    format: Literal["csv", "parquet"] = Field(description="csv or parquet")
    file_name: str | None = Field(
        None,
        pattern=r"^[A-Za-z0-9._-]{1,64}$",
        description="A bare file name (letters, digits, . _ -; at most 64); the extension is added. "
        "Default: the result_id.",
    )
    overwrite: bool = Field(False, description="Replace an existing file of that name")

    @field_validator("file_name")
    @classmethod
    def _not_dots(cls, v: str | None) -> str | None:
        if v is not None and set(v) == {"."}:
            raise ValueError("a file name cannot be only dots")
        return v


def _csv_ready(table: pa.Table) -> pa.Table:
    cols = []
    for col in table.columns:
        t = col.type
        if pa.types.is_map(t) or pa.types.is_list(t) or pa.types.is_struct(t) or pa.types.is_large_list(t):
            col = pa.array(
                [
                    None if v is None else json.dumps(json_safe(v), separators=(",", ":"))
                    for v in col.to_pylist()
                ],
                pa.string(),
            )
        cols.append(col)
    return pa.table(cols, names=table.column_names)


async def results_export(ctx: ToolContext, args: ExportArgs) -> ToolOutput:
    store = _store(ctx)
    export_dir = ctx.settings.get("export_dir") or store.limits.export_dir
    folder = pathlib.Path(export_dir) if export_dir else None
    if folder is None or not folder.is_absolute() or not folder.is_dir():
        raise ToolError(
            "export_dir_missing",
            "results.export_dir is not set to an existing absolute folder, so results cannot be exported.",
            hint="Set results.export_dir in the marketlens config and restart the server.",
        )
    info = store.info(args.result_id)
    name = args.file_name or args.result_id
    if not name.endswith(f".{args.format}"):
        name = f"{name}.{args.format}"
    target = folder / name
    if target.parent.resolve() != folder.resolve():
        raise ToolError("invalid_arguments", "The file name must be a bare name inside the export folder.")
    if target.is_symlink():
        raise ToolError(
            "export_exists", f"{name} exists in the export folder as a link; choose another name."
        )
    if target.exists() and not args.overwrite:
        raise ToolError(
            "export_exists",
            f"{name} already exists in the export folder.",
            hint="Pass overwrite=true to replace it.",
        )
    table = store.read(args.result_id, limit=info.row_count)
    tmp = folder / f".{name}.{secrets.token_hex(4)}.tmp"
    try:
        if args.format == "csv":
            pacsv.write_csv(_csv_ready(table), tmp)
        else:
            pq.write_table(table, tmp, compression="zstd")
        if args.overwrite:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise ToolError("export_exists", f"{name} already exists in the export folder.") from None
            except OSError:
                if target.exists():
                    raise ToolError("export_exists", f"{name} already exists in the export folder.") from None
                os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    size = target.stat().st_size
    result = pa.table(
        {
            "result_id": [args.result_id],
            "file_name": [name],
            "format": [args.format],
            "rows": pa.array([table.num_rows], pa.int64()),
            "bytes": pa.array([size], pa.int64()),
        }
    )
    request = {
        "result_id": args.result_id,
        "format": args.format,
        "file_name": name,
        "overwrite": args.overwrite,
    }
    return ToolOutput(
        model=QUERY_ROW_SCHEMA_NAME,
        provenance=_provenance(ctx, "file:results_export", request, [args.result_id]),
        table=result,
        offload="never",
    )


# --- the manifest -----------------------------------------------------------------------------


def _spec(name, capability, title, description, readme, input_model, handler, route) -> ToolSpec:
    return ToolSpec(
        name=name,
        capability=capability,
        title=title,
        description=description,
        readme=readme,
        input_model=input_model,
        output_model=QUERY_ROW_SCHEMA_NAME,
        provider="local",
        route=route,
        handler=handler,
        golden_test=GOLDEN,
    )


SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "results_query",
        "results",
        "Query stored results",
        "Run ONE read-only SQL SELECT (DuckDB dialect; WITH, joins, window functions, ASOF JOIN and "
        "time_bucket allowed) over results stored in this session, using each result_id as a table name. "
        "Returns at most max_rows rows (default 50, at most 200; a larger LIMIT is lowered). Larger answers, "
        "or store=true, are kept as a new result and you get its result_id; a query of one result keeps its "
        "time and series columns when they are unchanged, so it chains into analytics. Files, settings, other sessions "
        "and every write are refused.",
        "Read-only SQL over this session's results (one SELECT, forced LIMIT)",
        QueryArgs,
        results_query,
        "duckdb:results_query",
    ),
    _spec(
        "results_describe",
        "results",
        "Describe a stored result",
        "Show a stored result's summary again: typed columns with units, nulls and min/max, a preview of the "
        "first and last rows, provenance, pagination and three ready-made queries.",
        "A stored result's marker: columns, preview, ready-made queries",
        ResultIdArgs,
        results_describe,
        "store:results_describe",
    ),
    _spec(
        "results_sample",
        "results",
        "Sample a stored result",
        "Show n rows (1-50, default 10) of a stored result: the first or last by its time column, or a "
        "repeatable random sample; optionally only some columns.",
        "First, last or random rows of a stored result",
        SampleArgs,
        results_sample,
        "duckdb:results_sample",
    ),
    _spec(
        "results_list",
        "results",
        "List stored results",
        "List the results stored in this session, newest first: result_id, tool, model, row count, "
        "created and expiry time, and risk.",
        "This session's stored results, newest first",
        NoArgs,
        results_list,
        "store:results_list",
    ),
    _spec(
        "results_drop",
        "results",
        "Drop a stored result",
        "Delete one result stored in this session. Returns whether it was dropped.",
        "Delete one stored result",
        ResultIdArgs,
        results_drop,
        "store:results_drop",
    ),
    _spec(
        "results_export",
        "results.export",
        "Export a stored result",
        "Write a stored result to a CSV or Parquet file in the owner's export folder (results.export_dir) and "
        "return the bare file name, row count and size. Refuses to replace a file unless overwrite is true.",
        "Write a stored result to CSV or Parquet in results.export_dir",
        ExportArgs,
        results_export,
        "file:results_export",
    ),
)
