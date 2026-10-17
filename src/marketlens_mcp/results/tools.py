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
import sqlglot
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from marketlens_schema import QUERY_ROW_SCHEMA_NAME, Delay, Provenance

from ..manifest import DYNAMIC_MODELS
from ..offload import estimate_tokens
from ..plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from ..results_api import (
    CLOCK_NAMES,
    RESULT_ID_RE,
    QueryRefused,
    ResultInfo,
    RowsOf,
    known_model,
    unshown_known,
)
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
        50,
        ge=1,
        description="Rows to return inline (default 50, at most results.query_max_rows, 200 unless configured "
        "lower). A larger max_rows or LIMIT is lowered to it, with a note. An answer too large to show is stored "
        "whole instead, as with store=true.",
    )
    store: bool = Field(
        False,
        description="Keep the full answer (up to fetch.max_rows rows) as a new result and return its marker.",
    )


def _leaves(sql: str, name: str, schema: dict, *, plain: bool = False) -> set[tuple[str, str]] | None:
    """The (result_id, column) pairs the answer's column ``name`` is taken
    from, by its lineage (sqlglot's, through subqueries, CTEs and set
    operations), when it is one of them passed through, or max() (an
    aggregate) or greatest() of them, never earlier than any (``plain``: only
    passed through); None when the query computes it otherwise (min() of a
    week's, a time_bucket of it) or sqlglot cannot trace it."""
    try:
        node = lineage(name, sql, schema=schema, dialect="duckdb")
    except Exception:  # a query sqlglot cannot trace keeps no role; the answer is stored all the same
        return None
    leaves = set()
    for n in node.walk():
        if not n.downstream:
            if not isinstance(n.source, exp.Table):
                return None
            leaves.add((n.source.name.lower(), exp.to_column(n.name, dialect="duckdb").name.lower()))
            continue
        e = n.expression.unalias()
        if not isinstance(e, exp.Column) and not (
            not plain
            and isinstance(e, exp.Max | exp.Greatest)
            and all(isinstance(a, exp.Column) for a in e.iter_expressions())
        ):
            return None
    return leaves


def _reads(source: exp.Table | Scope) -> set[str]:
    """The results a query source reads for its rows: a table's own, or a
    derived table's or CTE's sources', through set operations."""
    if isinstance(source, exp.Table):
        return {source.name.lower()}
    out = {r for _, s in source.selected_sources.values() for r in _reads(s)}
    return out.union(*(_reads(b) for b in source.set_operation_scopes))


def _from_others(sql: str, schema: dict, dated: set[str]) -> str | None:
    """Why the answer's rows can hold values of rows other than the ones each
    keeps the clock of, some known later, when the query reads a ``dated``
    result (one whose rows have a clock or a time) so: a window function
    (avg() OVER (), lead(), a QUALIFY filter), a subquery in an expression, a
    LATERAL subquery or a SEMI or ANTI join (a filter on its maximum, on a
    later row), or such a result read twice for one row (a self-join); or that
    sqlglot cannot trace the query's rows. None otherwise."""
    try:
        return _other_rows(
            traverse_scope(
                qualify(sqlglot.parse_one(sql, dialect="duckdb"), schema=schema, dialect="duckdb")
            ),
            dated,
        )
    except Exception:  # a query sqlglot cannot trace keeps no role; the answer is stored all the same
        return "sqlglot cannot trace how its query computes rows"


def _other_rows(scopes: list[Scope], dated: set[str]) -> str | None:
    """``_from_others`` over the query's scopes (sqlglot's)."""
    for scope in scopes:
        reads = {alias: _reads(s) & dated for alias, s in scope.sources.items()}
        here = {r for alias in scope.selected_sources for r in reads[alias]}
        here |= {r for b in scope.set_operation_scopes for r in _reads(b) & dated}
        filters = [
            r
            for j in scope.find_all(exp.Join)
            if j.kind in ("SEMI", "ANTI")
            for r in reads.get(j.alias_or_name, ())
        ]
        if (
            scope.is_subquery
            and here
            and isinstance(getattr(scope.expression.parent, "parent", None), exp.Lateral)
        ):
            return (
                f"its query reads {sorted(here)[0]} in a LATERAL subquery (one run for each row it joins), so a row "
                "can depend on other rows, some known later"
            )
        if (scope.is_subquery and here) or filters:
            return (
                f"its query reads {sorted(here | set(filters))[0]} in a subquery inside an expression (a scalar "
                "subquery, IN or EXISTS) or a SEMI or ANTI join, so a row can depend on other rows, some known later"
            )
        if here and next(scope.find_all(exp.Window), None) is not None:
            return (
                "its query computes rows from other rows, some known later, with a window function (avg() OVER (), "
                "lead(), a QUALIFY filter); align first and compute over the aligned answer to stay point in time"
            )
        seen = [r for alias in scope.selected_sources for r in reads[alias]]
        twice = sorted({r for r in seen if seen.count(r) > 1})
        if twice:
            return (
                f"its query reads {twice[0]} twice for one row (a self-join), so a row can hold another row's values, "
                "known later"
            )
    return None


def _unproven(parent: ResultInfo) -> str | None:
    """Why nothing shows when each row of a result read without a column of
    role available became known (``unshown_known``), so an answer reading it
    can keep no role: computed from other results, a plugin's dated rows, rows
    whose fields change after their time (an order's fill), rows as of their
    fetch (positions, assets) or adjusted after their time (bars, unless raw);
    or fetched rows known only at the end of their span (bars), which no column
    of the answer shows. None for fetched rows known at their time column and
    fixed from then on (quotes, trades), or that hold at every time
    (exchanges, condition codes)."""
    why = unshown_known(parent)
    cls = known_model(parent)
    advice = ""
    if why is None and cls is not None and cls.span_column is not None:
        why = (
            f"holds {cls.schema_name} rows dated by their start {cls.time_column} and known only at their end "
            f"({cls.time_column} plus their {cls.span_column}), which no column of the answer shows"
        )
        advice = "; align them with analytics_align instead, which matches each bar from its end"
    elif cls is None and (parent.parents or parent.tool == "results_query"):
        advice = "; read the results it was computed from in this query instead"
    return f"it reads {parent.result_id}, which {why}{advice}" if why else None


def _rows_of(store: FileResultStore, out: QueryOutcome) -> RowsOf | None:
    """The fetched result of market data (``known_model``) a stored answer of
    that one result passes the rows of through unchanged: rows filtered or
    grouped, columns projected or computed from each row's own, with its time
    column passed through (or max() of the rows'), a bar's timeframe passed
    through or left out (then recorded, when all the rows have one), and no
    row computed from others (``_from_others``); so analytics_align takes the
    answer's rows as known when that result's are (a bar at its end). None
    otherwise."""
    if len(out.result_ids) != 1:
        return None
    parent = store.info(out.result_ids[0])
    cls = known_model(parent)
    if cls is None or cls.time_column is None:
        return None
    rid, sql = parent.result_id, out.executed_sql
    types = {f.name: duckdb_type(f.type) for f in out.table.schema}
    schema = {rid: {c.name: c.type for c in parent.columns}}
    span = None
    if cls.span_column is not None and cls.span_column not in types:
        # Left out: the one span all the parent's rows have.
        col = next((c for c in parent.columns if c.name == cls.span_column), None)
        if col is not None and col.nulls == 0 and isinstance(col.min, str) and col.min == col.max:
            span = col.min
        elif parent.rows_of is not None and parent.rows_of.span is not None:
            span = parent.rows_of.span
        else:
            return None
    for name, plain in ((cls.time_column, False), (cls.span_column, True)):
        if name is None or (plain and span is not None):
            continue
        if types.get(name) != schema[rid].get(name) or _leaves(sql, name, schema, plain=plain) != {
            (rid, name.lower())
        }:
            return None
    if _from_others(sql, schema, {rid}) is not None:
        return None
    if parent.rows_of is not None:
        return parent.rows_of.model_copy(update={"span": span})
    adjustment = parent.provenance.request.get("adjustment")
    return RowsOf(
        model=cls.schema_name,
        fetched_at=parent.provenance.fetched_at,
        adjustment=None if adjustment is None else str(adjustment),
        span=span,
    )


def _clocks(store: FileResultStore, out: QueryOutcome) -> tuple[dict[str, str], str | None, str | None]:
    """The answer's columns that keep role available (when each row became
    known), each with its role; the time column the answer needs for them
    (over several results, see below); and why none keeps it when the query
    reads a result that does not show when its rows became known
    (``_unproven``), computes rows from others (``_from_others``) or joins a
    result dated by its time column without that time in the clock.

    A column keeps the role with the name and type of such a column of a
    result read, built from such columns (``_leaves``): fed by every result
    read that has one, and by the time column of every other result read that
    has one (quotes joined to a dataset's rows: a joined row is known no
    earlier than its quote; never bars, known only at their end), unless
    those results share a time column the answer passes through (or takes
    max() or greatest() of), which is then its time column, so
    analytics_align matches a row from the later of the two."""
    types = {f.name: duckdb_type(f.type) for f in out.table.schema}
    parents = [store.info(rid) for rid in out.result_ids]
    named = {c.name for p in parents for c in p.columns if c.role and types.get(c.name) == c.type}
    if not named:
        return {}, None, None
    unproven = [
        why
        for p in sorted(parents, key=lambda p: p.result_id)
        if not any(c.role == "available" for c in p.columns) and (why := _unproven(p))
    ]
    if unproven:
        return {}, None, unproven[0]
    sql = out.executed_sql
    schema = {p.result_id: {c.name: c.type for c in p.columns} for p in parents}
    clocks = {(p.result_id, c.name.lower()) for p in parents for c in p.columns if c.role == "available"}
    clocked = {rid for rid, _ in clocks}
    times = {
        (p.result_id, p.time_column.lower()) for p in parents if p.result_id not in clocked and p.time_column
    }
    timed = {rid for rid, _ in times}
    why = _from_others(sql, schema, clocked | timed)
    if why:
        return {}, None, why
    time, by_time = None, set()
    if len({c for _, c in times}) == 1:
        (shared,) = {c for _, c in times}
        time = next((n for n in types if n.lower() == shared), None)
        leaves = _leaves(sql, time, schema) if time else None
        if leaves and leaves <= times:
            by_time = leaves
        else:
            time = None
    roles: dict[str, str] = {}
    undated: set[str] = set()
    for name in named:
        leaves = _leaves(sql, name, schema)
        if leaves is None or not leaves <= clocks | times or not {r for r, _ in leaves & clocks} >= clocked:
            continue
        missing = timed - {r for r, _ in (leaves | by_time) & times}
        if missing:
            undated |= missing
        else:
            roles[name] = "available"
    if undated and not roles:
        rid = sorted(undated)[0]
        col = next(c for r, c in times if r == rid)
        why = (
            f"it joins {rid}, whose rows are dated by {col}, without passing {col} through as its time column or "
            f"taking greatest() of it and the clock, so a row can hold a {rid} row dated after it (an ASOF JOIN "
            "forward, say)"
        )
    return roles, (time if roles and by_time else None), why


def _inherited(store: FileResultStore, out: QueryOutcome, roles: dict[str, str], time: str | None) -> dict:
    """What a stored answer keeps of the results it read: ``roles``, the role
    of each column that still says when each row became known (``_clocks``),
    whatever its time column, so analytics_align stays point in time on the
    answer, with the ``time`` column those roles need over several results.
    Over one parent, also its time and
    series columns and its column units, with the
    model each such column came from (the one the parent recorded for it, else
    the parent's, unless the parent is itself a query answer or aligned result,
    which records none of its own), only for columns still there with the same
    name and type, and only when the time column is one of them; so the answer
    chains into analytics as the parent would, after the parent is gone too;
    and the fetched rows it passes through unchanged (``_rows_of``), so
    analytics_align takes them as known when the parent's are."""
    types = {f.name: duckdb_type(f.type) for f in out.table.schema}
    parents = [store.info(rid) for rid in out.result_ids]
    kept: dict = {"column_roles": roles} if roles else {}
    if len(parents) != 1:
        return {**kept, "time_column": time} if time else kept
    parent = parents[0]
    own = None if parent.model in DYNAMIC_MODELS else parent.model
    same = {c.name: c for c in parent.columns if types.get(c.name) == c.type}
    if parent.time_column not in same:
        return kept
    return {
        **kept,
        "time_column": parent.time_column,
        "group_column": parent.group_column if parent.group_column in same else None,
        "column_units": {n: c.unit for n, c in same.items() if c.unit},
        "column_sources": {n: s for n, c in same.items() if c.unit and (s := c.source_model or own)},
        "rows_of": _rows_of(store, out),
    }


def _lost_clocks(
    store: FileResultStore, out: QueryOutcome, roles: dict[str, str], why: str | None
) -> list[str]:
    """Notes naming each column of when a row became known (role available)
    of the results read that a stored answer leaves out (or changes the type
    of), or keeps but computes or keeps beside rows computed from others (not
    in ``roles``; ``why``, when ``_clocks`` says): without one,
    analytics_align matches the answer on its time column."""
    types = {f.name: duckdb_type(f.type) for f in out.table.schema}
    clocks = [(rid, c) for rid in out.result_ids for c in store.info(rid).columns if c.role == "available"]
    lost = list(dict.fromkeys(f"{c.name} of {rid}" for rid, c in clocks if types.get(c.name) != c.type))
    computed = list(
        dict.fromkeys(c.name for _, c in clocks if types.get(c.name) == c.type and c.name not in roles)
    )
    notes = []
    if lost:
        notes.append(
            f"The answer leaves out {', '.join(lost)} (role available: when each row became known), so "
            "analytics_align matches it on its time column, which can pair a left row with a value published after "
            "it; keep the column, with its name and type, to match point in time."
        )
    if computed:
        why = why or (
            "a query keeps it only on such a column passed through, or on max() or greatest() of such columns from "
            "every result read that has one, never earlier than any row's (min() of a week's, say, is earlier than "
            "some of the week's values)"
        )
        clock = next((c for c in computed if c in CLOCK_NAMES), None)
        align = (
            f"analytics_align says a match on the answer is not shown point in time, and matches each row from the "
            f"later of its time column and {clock}, a lower bound on when it became known."
            if clock
            else "analytics_align matches the answer on its time column, which can pair a left row with a value "
            "published after it."
        )
        notes.append(
            f"The answer's {', '.join(computed)} keeps no role available (when each row became known): {why}. "
            + align
        )
    return notes


def _incomplete_parents(store: FileResultStore, out: QueryOutcome) -> str | None:
    """A note naming the results read that are themselves cut (truncated):
    an answer over one is incomplete too, whether or not it was cut itself."""
    cut = [rid for rid in out.result_ids if store.info(rid).provenance.truncated]
    if not cut:
        return None
    which = f"{cut[0]}, which is" if len(cut) == 1 else f"{', '.join(cut)}, which are"
    return (
        f"Computed from {which} incomplete (truncated: results_describe says where it stopped), so this answer "
        "may be incomplete too."
    )


async def results_query(ctx: ToolContext, args: QueryArgs) -> ToolOutput:
    store = _store(ctx)
    limits = store.limits
    max_rows = min(args.max_rows, limits.query_max_rows)
    out = store.query(args.sql, max_rows=max_rows, store=args.store)
    first = out
    rows = table_rows(out.table) if not args.store else None
    size = len(json.dumps(rows, separators=(",", ":")).encode()) if rows is not None else 0
    too_big = size > limits.query_max_bytes
    keep = args.store or too_big or (rows is not None and estimate_tokens(rows) > limits.inline_max_tokens)
    slow = None
    if keep and not args.store and out.truncated:
        # max_rows bounds an answer shown inline; one too large to show is
        # stored whole (up to fetch.max_rows), as store=true keeps it. If the
        # whole answer runs out of time, the rows already read are stored, cut.
        try:
            out = store.query(args.sql, max_rows=max_rows, store=True)
        except QueryRefused as exc:
            if exc.code != "sql_timeout":
                raise
            slow = (
                f"the whole answer took longer than {limits.query_timeout_seconds:g} seconds to run, so only "
                f"the first {max_rows} rows, read before, are kept"
            )
    cap = limits.fetch_max_rows if keep and slow is None else max_rows
    notes = [f"Executed: {out.executed_sql}"]
    if max_rows < args.max_rows and not keep:
        notes.append(
            f"max_rows {args.max_rows} is above results.query_max_rows ({limits.query_max_rows}), so it was lowered "
            "to that; pass store=true to keep the full answer as a new result."
        )
    truncation = None
    inherited = _incomplete_parents(store, out)
    if out.truncated:
        if slow:
            advice = "Aggregate or filter so that the whole answer runs in time."
        elif keep:
            advice = (
                f"Aggregate or filter to keep the whole answer: fetch.max_rows ({cap}) bounds a stored one."
            )
        else:
            advice = "Aggregate, filter, or use store=true to keep the full answer as a new result."
        kept = slow or f"more than {cap} rows matched and only the first {cap} are kept"
        truncation = f"The answer is cut: {kept}. {advice}"
        notes.append(f"Truncated: yes; {kept}. {advice}")
        if inherited:
            notes.append(inherited)
            truncation += f" {inherited}"
    elif inherited:
        truncation = inherited
        notes.append(f"Truncated: yes; {inherited}")
    else:
        notes.append("Truncated: no")
    if keep and not args.store:
        limit = (
            f"results.query_max_bytes ({limits.query_max_bytes:,} bytes)"
            if too_big
            else f"results.inline_max_tokens ({limits.inline_max_tokens:,} tokens)"
        )
        if first.truncated:
            kept = (
                f"its first {out.row_count} rows were" if out.truncated else f"all {out.row_count} rows were"
            )
            notes.append(
                f"The answer (its first {first.row_count} rows alone: {size:,} bytes as JSON) is larger than "
                f"{limit}, so {kept} stored as a new result (max_rows bounds only an answer shown inline): query "
                "it, or select fewer columns or rows to see it inline."
            )
        else:
            notes.append(
                f"The answer ({out.row_count} rows, {size:,} bytes as JSON) is larger than {limit}, so it was "
                "stored as a new result: query it, or select fewer columns or rows to see it inline."
            )
    roles, time, why = _clocks(store, out) if keep else ({}, None, None)
    if keep:
        notes.extend(_lost_clocks(store, out, roles, why))
    request = {"sql": args.sql[:4000], "max_rows": args.max_rows, "store": args.store}
    prov = _provenance(ctx, "duckdb:results_query", request, list(out.result_ids))
    if truncation:
        prov = prov.model_copy(update={"truncated": True, "truncation_note": truncation})
    if keep:
        info = store.put(
            out.table,
            tool="results_query",
            model=QUERY_ROW_SCHEMA_NAME,
            provenance=prov,
            risk=out.risk,
            parents=list(out.result_ids),
            **_inherited(store, out, roles, time),
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
    prov = info.provenance
    # An incomplete result says so again, as its first answer did.
    notes = [prov.truncation_note] if prov.truncated and prov.truncation_note else []
    return ToolOutput(model=info.model, provenance=prov, stored=info, notes=notes, risk=info.risk)


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
        "Run ONE read-only SQL SELECT (DuckDB; WITH, joins, windows, ASOF JOIN and "
        "time_bucket allowed; PIVOT and UNPIVOT are not: use max(x) FILTER (WHERE ...) per column) over at most 8 "
        "results of this session, each result_id a table name. Returns at most max_rows rows "
        "(default 50, at most 200; a larger max_rows or LIMIT is lowered). An answer over results.query_max_bytes "
        "(24,000 bytes of JSON, default) or about 6,000 tokens, or store=true, is kept whole as a new result "
        "(max_rows bounds only an inline answer; fetch.max_rows, 50,000 rows by default, a stored one; an answer "
        "cut, or over a cut result, says truncated) and you get its result_id; a query of one result keeps its "
        "unchanged time and series columns, and a clock's role passed through (or max; no window, subquery, "
        "self-join, bars, role-less input but quotes, trades), so it chains into analytics. Exact decimals come "
        "back as strings (CAST to DOUBLE); whole-number sums as numbers. Files, settings, other sessions and every "
        "write are refused.",
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
