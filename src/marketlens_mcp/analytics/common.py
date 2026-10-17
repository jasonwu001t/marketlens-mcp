"""What the analytics tools share (owner: ml-analytics).

Every tool works on result handles of this session through the results_api
protocols only (``ToolContext.results``): it validates the columns it is
given against ``ResultInfo.columns``, computes in the store's locked-down
DuckDB session with server-authored SQL, and returns an Arrow table in the
output model's canonical types. Large outputs are stored by the server's
offload (``ToolOutput(table=...)``); dynamic outputs that are large are
stored here so that they keep their time and series columns.

Time: TIMESTAMP WITH TIME ZONE inputs are turned into naive UTC timestamps
(``timezone('UTC', t)``) before any bucketing or arithmetic, so buckets are
UTC-aligned whatever the connection's time zone (``time_bucket`` defaults:
weeks start on Monday, months on the 1st); outputs are turned back into UTC
instants.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import math
import re
import types
import typing
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa
from pydantic import BaseModel, Field

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput
from marketlens_mcp.results_api import (
    CLOCK_NAMES,
    RESULT_ID_RE,
    ColumnInfo,
    OutputRisk,
    ResultInfo,
    ResultNotFound,
    ResultSession,
    RowsOf,
    implied_time,
    known_late,
    known_model,
    known_span,
    period_rows,
    source_rows,
)
from marketlens_schema import ALIGNED_SCHEMA_NAME, BUILTIN_MODELS, QUERY_ROW_SCHEMA_NAME, CanonicalModel
from marketlens_schema.base import TIMEFRAME_RE, AbsenceCode, AbsenceReason, Delay, Provenance

CAPABILITY = "analytics"
PROVIDER = "local"
#: The contract's default inline limits (results.inline_max_rows / inline_max_tokens).
INLINE_ROWS = 200
INLINE_TOKENS = 6000
SERIES_ALL = "_all"

BAR_MODELS = ("marketlens.Bar", "marketlens.OptionBar")
RETURNS_MODEL = "marketlens.ReturnPoint"
#: Units whose values are prices or money: correlation turns them into returns.
PRICE_UNITS = frozenset({"price", "USD", "percent_of_par"})

_NUMERIC = frozenset(
    {
        "DOUBLE",
        "FLOAT",
        "REAL",
        "BIGINT",
        "INTEGER",
        "SMALLINT",
        "TINYINT",
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
        "UHUGEINT",
    }
)
_TZ_TIMESTAMP = "TIMESTAMP WITH TIME ZONE"
_NAIVE_TIMESTAMPS = frozenset({"TIMESTAMP", "TIMESTAMP_NS", "TIMESTAMP_MS", "TIMESTAMP_S"})


def result_id_field(description: str) -> Any:
    return Field(pattern=RESULT_ID_RE.pattern, description=description)


def optional_result_id_field(description: str) -> Any:
    return Field(None, pattern=RESULT_ID_RE.pattern, description=description)


def store_field() -> Any:
    return Field(
        False,
        description="Keep the output as a stored result even when it is small, and get its result_id (for "
        "results_query or another analytics tool); larger outputs are always stored.",
    )


def refuse(code: str, message: str, hint: str | None = None) -> ToolError:
    return ToolError(code, message, hint=hint)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def names(items: Iterable[str]) -> str:
    items = list(items)
    return ", ".join(items) if items else "(none)"


# --- SQL text ---------------------------------------------------------------------------------


def qi(name: str) -> str:
    """A quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def ql(text: str) -> str:
    """A quoted SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


def finite(expr: str) -> str:
    """SQL: ``expr`` is a finite number (NULL for NULL). NaN and +-infinity are
    not: in DuckDB NaN > 0 is true and NaN sorts above every number."""
    return f"isfinite(CAST({expr} AS DOUBLE))"


def out_time(expr: str) -> str:
    """A naive UTC timestamp expression back to a UTC instant."""
    return f"timezone('UTC', {expr})"


def rows(session: ResultSession, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return session.query(sql, params).to_pylist()


def scalar(session: ResultSession, sql: str, params: Sequence[Any] = ()) -> Any:
    table = session.query(sql, params)
    return table.column(0)[0].as_py() if table.num_rows else None


# --- timeframes and durations -------------------------------------------------------------------

_TF_PARTS = re.compile(r"^(\d+)(min|h|d|w|mo)$")
_TF_SQL_UNIT = {"min": "minute", "h": "hour", "d": "day", "w": "week", "mo": "month"}
_TF_SECONDS = {"min": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400}  # months: nominal


def tf_parts(tf: str) -> tuple[int, str]:
    m = _TF_PARTS.match(tf)
    if not m or not TIMEFRAME_RE.match(tf):
        raise ValueError(f"not a canonical timeframe: {tf!r}")
    return int(m.group(1)), m.group(2)


def tf_interval(tf: str) -> str:
    n, u = tf_parts(tf)
    return f"INTERVAL '{n} {_TF_SQL_UNIT[u]}'"


def tf_seconds(tf: str) -> int:
    n, u = tf_parts(tf)
    return n * _TF_SECONDS[u]


#: The zone whose midnight starts a stock's or an option's bar of a day or longer (a crypto pair's: UTC).
BAR_ZONE = "America/New_York"


def span_end(ts: str, span: str) -> str:
    """SQL: when a bar ends that starts at ``ts`` (a naive UTC timestamp
    expression) and covers ``span`` (a timeframe expression: 15min, 4h, 1d, 1w,
    3mo): N minutes or hours later, or N days, weeks or months later on the
    calendar of the zone it starts at midnight in, New York (stocks and
    options: a week across a clock change is an hour longer or shorter in UTC)
    or UTC (crypto), and at a time midnight in neither, the later of the two;
    NULL for a span that is not a timeframe."""
    n = f"CAST(regexp_extract({span}, '^[0-9]+') AS INTEGER)"
    unit = f"regexp_extract({span}, '[a-z]+$')"
    local = f"timezone('{BAR_ZONE}', timezone('UTC', {ts}))"
    days = f"CASE {unit} WHEN 'd' THEN to_days({n}) WHEN 'w' THEN to_days(7 * {n}) WHEN 'mo' THEN to_months({n}) END"
    utc, zoned = f"{ts} + {days}", f"timezone('UTC', timezone('{BAR_ZONE}', {local} + {days}))"
    return (
        f"CASE WHEN NOT regexp_full_match({span}, '{TIMEFRAME_RE.pattern.strip('^$')}') THEN NULL "
        f"WHEN {unit} = 'min' THEN {ts} + to_minutes({n}) WHEN {unit} = 'h' THEN {ts} + to_hours({n}) "
        f"WHEN {local} = date_trunc('day', {local}) THEN {zoned} WHEN {ts} = date_trunc('day', {ts}) THEN {utc} "
        f"ELSE greatest({utc}, {zoned}) END"
    )


def known_time(inp: Input, cls: type[CanonicalModel] | None) -> str | None:
    """SQL: when each row of ``inp`` became known by the rule its rows follow
    (``cls``, its ``known_model``; None for a bucket of other rows,
    ``period_rows``), as a naive UTC timestamp: the end of the period it
    covers (a bar's, a bucket's: ``span_end``) and ``lag_seconds`` after it,
    or its fetch (a snapshot); None when that is its time column itself
    (quotes, trades)."""
    info = inp.info
    src = info.rows_of
    if cls is not None and cls.known_at_fetch:
        at = (src.fetched_at if src is not None else info.provenance.fetched_at).astimezone(dt.UTC)
        return f"TIMESTAMP '{at:%Y-%m-%d %H:%M:%S.%f}'"
    span = known_span(info, cls)
    lag = src.lag_seconds if src is not None else 0
    if span is None and not lag:
        return None
    end = span_end(inp.ts(), span) if span is not None else inp.ts()
    return f"({end} + to_seconds({lag}))" if lag else end


def derived_rows_of(
    session: ResultSession,
    inputs: Sequence[Input],
    *,
    period: str | None = None,
    levels: bool = True,
    keeps_clock: bool = False,
) -> RowsOf | None:
    """What an output computed from ``inputs`` records of the rows its own
    come from (``ResultInfo.rows_of``), so analytics_align takes each of its
    rows as known no earlier than the last of the input rows it covers: with
    ``period``, a row dated at a bucket's start covers the bucket's rows,
    known at its end, or ``lag_seconds`` after it when one of them is known
    later (a stock's day bar ends at midnight New York time, after a UTC
    bucket); without, a row dated as an input row is known when that row is (a
    return, a drawdown, a window ending there). ``levels``: the output holds
    price levels, so it keeps the inputs' adjustment (returns, volatility and
    beta hold ratios of prices, which a later split or dividend leaves as they
    were). ``keeps_clock``: the output carries its inputs' other columns, a
    clock among them (knowledge_time), which analytics_align matches on.

    Fetched market data is known by its model's rule. Other rows (a
    dataset's) are known no earlier than their time: a bucket of them records
    its span (``period_rows``); rows dated as theirs record nothing (matched on
    their time, not shown point in time), unless they show when they became
    known only by a clock the output leaves out (returns of a dataset with a
    knowledge_time). Marked not shown then, or when an input's rows are not
    known by the same kind of rule as the others' or cover different spans and
    are known only after their time (``known_late``: bars, snapshots)."""
    found = [(inp, known_model(inp.info), source_rows(inp.info)) for inp in inputs]
    srcs = [src for _, _, src in found if src is not None]
    clocked = [inp for inp, cls, _ in found if cls is None and (inp.available or inp.unmarked_clock)]
    dropped = [] if keeps_clock else clocked
    if not srcs:
        fetched_at = max(inp.info.provenance.fetched_at for inp in inputs)
        if dropped:
            return RowsOf(model=dropped[0].model, fetched_at=fetched_at, shown=False)
        if period is None or clocked:
            return None
        return RowsOf(model=inputs[0].model, fetched_at=fetched_at, span=period)
    late = [x for x in srcs if known_late(x)]
    adjusted = [x.adjustment for x in srcs if x.adjustment not in (None, "raw")]
    out = RowsOf(
        model=(late or srcs)[0].model,
        fetched_at=max(x.fetched_at for x in srcs),
        adjustment=(adjusted[0] if adjusted else srcs[0].adjustment) if levels else None,
    )
    hidden = out.model_copy(update={"shown": False}) if late or dropped else None
    classes = [cls for _, cls, _ in found]
    at_fetch = {cls is not None and cls.known_at_fetch for cls in classes}
    ruled = all(cls is not None or period_rows(inp.info) is not None for inp, cls, _ in found)
    if dropped or not ruled or len(at_fetch) > 1:
        return hidden
    if at_fetch == {True}:
        return out
    if period is not None:
        lag = 0
        for inp, cls, _ in found:
            known = known_time(inp, cls) or inp.ts()
            end = span_end(f"time_bucket({tf_interval(period)}, {inp.ts()})", ql(period))
            gap = scalar(
                session,
                f"SELECT max(epoch_us({known}) - epoch_us({end})) FROM {inp.table} WHERE {qi(inp.time)} IS NOT NULL",
            )
            if gap is not None and gap > 0:
                lag = max(lag, math.ceil(gap / 1_000_000))
        return out.model_copy(update={"span": period, "lag_seconds": lag})
    spans: set[str | None] = set()
    for inp, cls, _ in found:
        span = known_span(inp.info, cls)
        if span is None:
            spans.add(None)
            continue
        spans |= {
            r["s"]
            for r in rows(
                session,
                f"SELECT DISTINCT {span} AS s FROM {inp.table} WHERE {qi(inp.time)} IS NOT NULL AND {span} IS NOT NULL",
            )
        }
    if len(spans) > 1:
        return hidden
    return out.model_copy(
        update={"span": next(iter(spans), None), "lag_seconds": max(x.lag_seconds for x in srcs)}
    )


def tf_nests(source: str, target: str) -> bool:
    """Whether every ``target`` bucket holds whole ``source`` bars (``target``
    already known to be coarser)."""
    sn, su = tf_parts(source)
    tn, tu = tf_parts(target)
    if tu in ("min", "h"):
        return tf_seconds(target) % tf_seconds(source) == 0
    if su in ("min", "h"):
        return 86400 % tf_seconds(source) == 0
    if tu == "w":
        return su == "d"
    if tu == "mo":
        return su == "d" or (su == "mo" and tn % sn == 0)
    return False


def default_periods_per_year(tf: str, *, crypto: bool = False, extended: bool = False) -> float | None:
    """Contract 6.2, by market. US equity sessions: 1d 252, 1w 52, 1mo 12,
    1h 1638 (252 x 6.5), Nmin 98280 / N (252 x 390 / N); with extended hours
    (04:00-20:00 New York, 16 hours) 1h 4032 and Nmin 241920 / N. Crypto,
    every day around the clock: 1d 365, 1w 52, 1mo 12, Nh 8760 / N, Nmin
    525600 / N. Other timeframes have no default."""
    n, u = tf_parts(tf)
    if crypto:
        if u == "min":
            return 525600 / n
        if u == "h":
            return 8760 / n
        return {"1d": 365.0, "1w": 52.0, "1mo": 12.0}.get(tf)
    minutes = 960 if extended else 390
    if u == "min":
        return 252 * minutes / n
    if tf == "1h":
        return 252 * minutes / 60
    return {"1d": 252.0, "1w": 52.0, "1mo": 12.0}.get(tf)


_DURATION = re.compile(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text: str) -> dt.timedelta | None:
    """An ISO-8601 duration of weeks, days, hours, minutes and seconds
    ("PT30S", "PT1H30M", "P2D", "PT0S" for none); None for anything else
    (months and years are not fixed lengths)."""
    m = _DURATION.match(text)
    if not m or text.endswith("T") or not any(m.groups()):
        return None
    w, d, h, mi, s = m.groups()
    try:
        delta = dt.timedelta(
            weeks=int(w or 0),
            days=int(d or 0),
            hours=int(h or 0),
            minutes=int(mi or 0),
            seconds=float(s or 0),
        )
    except OverflowError:  # beyond timedelta's range: not a usable duration
        return None
    return delta


# --- canonical Arrow types of the output models ---------------------------------------------------

_ABSENT_TYPE = pa.map_(pa.string(), pa.string())


def _arrow_type(annotation: Any) -> tuple[pa.DataType, bool]:
    nullable = False
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        elif origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            nullable = nullable or len(args) != len(typing.get_args(annotation))
            (annotation,) = args
        else:
            break
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return pa.string(), nullable
    if origin is list:
        return pa.list_(_arrow_type(typing.get_args(annotation)[0])[0]), nullable
    if origin is dict:
        return _ABSENT_TYPE, True
    for py, arrow in (
        (bool, pa.bool_()),
        (str, pa.string()),
        (enum.Enum, pa.string()),
        (int, pa.int64()),
        (float, pa.float64()),
        (Decimal, pa.decimal128(38, 12)),
        (dt.datetime, pa.timestamp("us", tz="UTC")),
        (dt.date, pa.date32()),
    ):
        if isinstance(annotation, type) and issubclass(annotation, py):
            return arrow, nullable
    raise TypeError(f"no canonical Arrow type for {annotation!r}")


def canonical_schema(model: type[CanonicalModel]) -> pa.Schema:
    """The model's canonical Arrow schema (contract 2.4): fields in declaration
    order with ``absent`` last as map<string, string>."""
    fields = []
    for name in [n for n in model.model_fields if n != "absent"] + ["absent"]:
        typ, nullable = _arrow_type(model.model_fields[name].annotation)
        fields.append(pa.field(name, typ, nullable or name == "absent"))
    return pa.schema(fields)


def to_model_table(table: pa.Table, model: type[CanonicalModel]) -> pa.Table:
    schema = canonical_schema(model)
    return table.select(schema.names).cast(schema)


# --- resolving an input handle -----------------------------------------------------------------


def is_numeric(column: ColumnInfo) -> bool:
    return column.type in _NUMERIC or column.type.startswith("DECIMAL")


def is_timestamp(column: ColumnInfo) -> bool:
    return column.type == _TZ_TIMESTAMP or column.type in _NAIVE_TIMESTAMPS


@dataclass(frozen=True)
class Input:
    """One result handle, resolved: its info, time column and series column;
    ``value`` is the default value column a query answer keeps from its parent."""

    info: ResultInfo
    time: str
    group: str | None
    notes: tuple[str, ...] = ()
    value: str | None = None

    @property
    def rid(self) -> str:
        return self.info.result_id

    @property
    def model(self) -> str:
        return self.info.model

    @property
    def label(self) -> str:
        return f"{self.rid} ({self.model})"

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.info.columns if c.name == name), None)

    @property
    def table(self) -> str:
        return qi(self.rid)

    def ts(self, alias: str | None = None, *, column: str | None = None) -> str:
        """The time column (or another timestamp ``column``) as a naive UTC
        timestamp expression."""
        name = column or self.time
        ref = f"{alias}.{qi(name)}" if alias else qi(name)
        col = self.column(name)
        if col is not None and col.type == _TZ_TIMESTAMP:
            return f"timezone('UTC', {ref})"
        return f"CAST({ref} AS TIMESTAMP)"

    @property
    def available(self) -> str | None:
        """The timestamp column holding when each row became known (role
        available), other than the time column; None without one."""
        return next(
            (
                c.name
                for c in self.info.columns
                if c.role == "available" and c.name != self.time and is_timestamp(c)
            ),
            None,
        )

    @property
    def unmarked_clock(self) -> str | None:
        """Without a column with role available, a timestamp column named as
        a point-in-time clock (``knowledge_time``, ``ingested_at``) other than
        the time column, which has lost the role (a query answer that cannot
        show when its rows became known: its query computed the clock, or rows
        from others, or read a computed result without the role); None
        otherwise."""
        if self.available is not None:
            return None
        return next(
            (
                c.name
                for c in self.info.columns
                if c.name in _CLOCKS and c.name != self.time and is_timestamp(c)
            ),
            None,
        )

    def series(self, alias: str | None = None) -> str:
        if self.group is None:
            return ql(SERIES_ALL)
        ref = f"{alias}.{qi(self.group)}" if alias else qi(self.group)
        return f"CAST({ref} AS VARCHAR)"

    def numeric_columns(self) -> list[str]:
        return [c.name for c in self.info.columns if is_numeric(c)]

    def units(self) -> dict[str, str]:
        """The unit of each column that has one."""
        return {c.name: c.unit for c in self.info.columns if c.unit}

    def sources(self) -> dict[str, str]:
        """The model that computed each column that has a unit: the one it
        recorded (a carried column), else this result's, unless this is a query
        answer or aligned result, which records none of its own (its other
        columns' source is not known)."""
        own = None if self.model in (QUERY_ROW_SCHEMA_NAME, ALIGNED_SCHEMA_NAME) else self.model
        return {c.name: s for c in self.info.columns if c.unit and (s := c.source_model or own)}


def _parent_value(ctx: ToolContext, info: ResultInfo) -> str | None:
    """A stored query answer over one parent whose time column it kept: the
    parent model's default value column, when it is still there with the same
    type."""
    if info.model != QUERY_ROW_SCHEMA_NAME or len(info.parents) != 1 or info.time_column is None:
        return None
    try:
        parent = ctx.results.info(info.parents[0])
    except ResultNotFound:
        return None
    cls = BUILTIN_MODELS.get(parent.model)
    if cls is None or not cls.value_columns or parent.time_column != info.time_column:
        return None
    name = cls.value_columns[0]
    mine = next((c for c in info.columns if c.name == name), None)
    theirs = next((c for c in parent.columns if c.name == name), None)
    return name if mine is not None and theirs is not None and mine.type == theirs.type else None


def clocked_ancestor(ctx: ToolContext, info: ResultInfo) -> tuple[str, str] | None:
    """A result ``info`` was computed from (its parents, their parents, ...)
    that has a column with role available (when each row became known), as
    (result id, column), the nearest first; None when none still stored has
    one."""
    queue, seen = list(info.parents), set()
    while queue:
        rid = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        try:
            parent = ctx.results.info(rid)
        except ResultNotFound:
            continue
        clock = next((c.name for c in parent.columns if c.role == "available"), None)
        if clock is not None:
            return rid, clock
        queue.extend(parent.parents)
    return None


#: Point-in-time clock columns next to ``event_time`` (a datastore's values):
#: when a value was known or stored, not the time it describes.
_CLOCKS = CLOCK_NAMES


def resolve(ctx: ToolContext, tool: str, result_id: str, *, series_column: str | None = None) -> Input:
    """The handle's info (ResultNotFound propagates: the server turns it into
    R15) with its time column and series column checked."""
    info = ctx.results.info(result_id)
    by_name = {c.name: c for c in info.columns}
    notes: list[str] = []
    label = f"{info.result_id} ({info.model})"
    time = info.time_column
    if time is None and info.model not in BUILTIN_MODELS:
        stamps = [c.name for c in info.columns if is_timestamp(c)]
        time = implied_time(stamps)
        clocks = [c for c in stamps if c != "event_time"] if time == "event_time" and len(stamps) > 1 else []
        if time is not None:
            notes.append(
                f"{info.result_id} has no recorded time column; time column '{time}' was used"
                + (
                    f" as the time each row describes; {names(clocks)} "
                    + ("says" if len(clocks) == 1 else "say")
                    + " when it became known."
                    if clocks
                    else "."
                )
            )
        elif stamps:
            raise refuse(
                "not_time_series",
                f"{tool} needs a time series, but {label} has several timestamp columns ({names(stamps)}) "
                "and none is its time column.",
                hint="Select exactly one timestamp column with results_query (store=true) and try again.",
            )
    if time is None or time not in by_name or not is_timestamp(by_name[time]):
        raise refuse(
            "not_time_series",
            f"{tool} needs a time series, but {label} has no time column.",
            hint="Use a result with timestamps (bars, quotes, trades, returns, portfolio history).",
        )
    group = series_column if series_column is not None else info.group_column
    if group is not None:
        col = by_name.get(group)
        if col is None:
            raise refuse(
                "unknown_column",
                f"{tool}: column '{group}' is not in {label}. Columns: {names(by_name)}.",
            )
        if col.type.startswith(("MAP", "STRUCT")) or col.type.endswith("]"):
            raise refuse(
                "unknown_column", f"{tool}: column '{group}' of {label} cannot name a series ({col.type})."
            )
        if group == time:
            raise refuse("unknown_column", f"{tool}: the series column cannot be the time column '{time}'.")
    return Input(info=info, time=time, group=group, notes=tuple(notes), value=_parent_value(ctx, info))


def value_column(tool: str, inp: Input, given: str | None, *, param: str) -> str:
    """The numeric column to use: ``given``, or the built-in model's first
    value column (a query answer's: its parent's). Refusals name the numeric columns."""
    numeric = inp.numeric_columns()
    if given is None:
        cls = BUILTIN_MODELS.get(inp.model)
        given = cls.value_columns[0] if cls is not None and cls.value_columns else inp.value
        if given is None:
            raise refuse(
                "column_required",
                f"{tool} needs {param}: {inp.label} has no default value column. Numeric columns: {names(numeric)}.",
            )
    if given not in numeric:
        raise refuse(
            "not_numeric",
            f"{tool} needs a numeric column; '{given}' is not one of {names(numeric)} in {inp.label}.",
        )
    return given


#: Units of ratios: not price levels, so not inputs of returns, drawdown or beta.
RATIO_UNITS = frozenset({"fraction", "fraction_per_year"})


def price_column(tool: str, inp: Input, given: str | None, *, param: str) -> str:
    """``value_column``, refusing a ratio column (a return, a percent change):
    the tools that read prices would skip its negative values silently."""
    col = value_column(tool, inp, given, param=param)
    unit = inp.column(col).unit
    if unit in RATIO_UNITS:
        raise refuse(
            "not_prices",
            f"{tool} works on prices or values; '{col}' of {inp.label} is a ratio (unit {unit}). Pass a price "
            "column, or the price result it came from.",
        )
    return col


def points_sql(inp: Input, value: str, *, positive: bool) -> str:
    """SELECT s, ts, v: the usable points of each series (time, series and value
    present, value finite; with ``positive`` only v > 0)."""
    where = [f"{qi(inp.time)} IS NOT NULL", f"{qi(value)} IS NOT NULL", finite(qi(value))]
    if inp.group is not None:
        where.append(f"{qi(inp.group)} IS NOT NULL")
    if positive:
        where.append(f"CAST({qi(value)} AS DOUBLE) > 0")
    return (
        f"SELECT {inp.series()} AS s, {inp.ts()} AS ts, CAST({qi(value)} AS DOUBLE) AS v "
        f"FROM {inp.table} WHERE {' AND '.join(where)}"
    )


def skipped_notes(
    session: ResultSession, inp: Input, value: str, *, positive: bool, hint: str | None = None
) -> list[str]:
    """Notes counting the rows ``points_sql`` leaves out; ``hint`` follows the
    count of non-positive values."""
    t, v = qi(inp.time), qi(value)
    parts = [
        f"count(*) FILTER (WHERE {t} IS NULL)",
        f"count(*) FILTER (WHERE {t} IS NOT NULL AND {v} IS NULL)",
        f"count(*) FILTER (WHERE {t} IS NOT NULL AND {v} IS NOT NULL AND NOT {finite(v)})",
    ]
    parts.append(
        f"count(*) FILTER (WHERE {t} IS NOT NULL AND {v} IS NOT NULL AND {qi(inp.group)} IS NULL)"
        if inp.group is not None
        else "0"
    )
    parts.append(
        f"count(*) FILTER (WHERE {t} IS NOT NULL AND {v} IS NOT NULL AND {finite(v)} AND CAST({v} AS DOUBLE) <= 0)"
        if positive
        else "0"
    )
    (row,) = rows(
        session, f"SELECT {', '.join(f'{p} AS c{i}' for i, p in enumerate(parts))} FROM {inp.table}"
    )
    no_time, no_value, non_finite, no_group, nonpositive = (row[f"c{i}"] for i in range(5))
    notes = []
    if no_time:
        notes.append(f"Skipped {plural(no_time, 'row')} with a NULL {inp.time}.")
    if no_value:
        notes.append(f"Skipped {plural(no_value, 'row')} with a NULL {value}.")
    if non_finite:
        notes.append(f"Skipped {plural(non_finite, 'row')} with a non-finite {value} (NaN or infinity).")
    if no_group:
        notes.append(f"Skipped {plural(no_group, 'row')} with a NULL {inp.group}.")
    if nonpositive:
        notes.append(
            f"Skipped {plural(nonpositive, 'row')} with {value} <= 0 (returns and drawdowns need positive values)."
            + (f" {hint}" if hint else "")
        )
    return notes


def check_unique_times(session: ResultSession, tool: str, inp: Input, points: str) -> None:
    """Refuse a series with two rows at the same time (the order of returns
    would be ambiguous)."""
    n = scalar(
        session, f"SELECT count(*) FROM (SELECT s, ts FROM ({points}) GROUP BY s, ts HAVING count(*) > 1)"
    )
    if n:
        where = f"series column '{inp.group}'" if inp.group else "no series column"
        raise refuse(
            "duplicate_timestamps",
            f"{tool}: {inp.label} has more than one row at the same time within a series ({plural(n, 'repeated timestamp')}, "
            f"{where}). Pass series_column to split it into series, or remove the duplicates with results_query.",
        )


def bar_timeframe(ctx: ToolContext, session: ResultSession, tool: str, inp: Input) -> str | None:
    """The input's single bar timeframe: its ``timeframe`` column, a returns
    result's period, or the timeframe of the prices a returns result came from.
    None when unknown; mixed timeframes are refused."""
    col = inp.column("timeframe")
    if col is not None and col.type == "VARCHAR":
        found = [
            r["tf"]
            for r in rows(
                session,
                f"SELECT DISTINCT {qi('timeframe')} AS tf FROM {inp.table} WHERE {qi('timeframe')} IS NOT NULL ORDER BY 1",
            )
        ]
        if len(found) > 1:
            raise refuse(
                "mixed_timeframes",
                f"{tool}: {inp.label} mixes bar timeframes ({names(found)}).",
                hint="Keep one timeframe with results_query (store=true) first.",
            )
        if found and TIMEFRAME_RE.match(found[0]):
            return found[0]
    if inp.model == RETURNS_MODEL:
        periods = [
            r["p"]
            for r in rows(session, f"SELECT DISTINCT period AS p FROM {inp.table} WHERE period IS NOT NULL")
        ]
        if len(periods) == 1:
            return periods[0]
        for parent in inp.info.parents:
            try:
                pinfo = ctx.results.info(parent)
            except ResultNotFound:
                continue
            tf = next((c for c in pinfo.columns if c.name == "timeframe"), None)
            if tf is not None and isinstance(tf.min, str) and tf.min == tf.max and TIMEFRAME_RE.match(tf.min):
                return tf.min
    return None


def returns_sql(points: str, *, kind: str, period: str | None = None) -> str:
    """SELECT s, ts, r: returns between consecutive points of each series;
    with ``period``, between the last points of consecutive non-empty buckets
    (ts = bucket start)."""
    src = points
    if period is not None:
        src = (
            f"SELECT s, b AS ts, v FROM (SELECT s, time_bucket({tf_interval(period)}, ts) AS b, arg_max(v, ts) AS v "
            f"FROM ({points}) GROUP BY s, b)"
        )
    expr = "ln(v / v0)" if kind == "log" else "v / v0 - 1"
    return (
        f"SELECT s, ts, {expr} AS r FROM (SELECT s, ts, v, lag(v) OVER (PARTITION BY s ORDER BY ts) AS v0 "
        f"FROM ({src})) WHERE v0 IS NOT NULL"
    )


def series_counts(session: ResultSession, sql: str) -> dict[str, int]:
    """Rows per series of a query with a column ``s``."""
    return {
        r["s"]: r["n"] for r in rows(session, f"SELECT s, count(*) AS n FROM ({sql}) GROUP BY s ORDER BY s")
    }


def no_period(period: str | None) -> dict[str, AbsenceReason]:
    """The response-level reason for a None ``period`` column."""
    if period is not None:
        return {}
    return {
        "period": AbsenceReason(
            code=AbsenceCode.NOT_APPLICABLE, detail="No period: consecutive observations."
        )
    }


def adjustment_note(inp: Input) -> str | None:
    adjustment = inp.info.provenance.request.get("adjustment")
    if inp.model == "marketlens.Bar" and adjustment in ("raw", "dividend"):
        return (
            f"The bars of {inp.rid} are not split-adjusted (adjustment={adjustment}): a split shows as a price "
            "jump. Fetch bars with adjustment=all (or split) for returns, volatility, drawdown and beta."
        )
    return None


# --- what every output carries -------------------------------------------------------------------


def provenance(ctx: ToolContext, tool: str, inputs: Sequence[ResultInfo], args: BaseModel) -> Provenance:
    """provider "marketlens", route "duckdb:<tool>", derived_from = the input
    ids, request = every parameter; feed, delay and environment carried over
    when the inputs agree; as_of = the latest input as_of."""
    ps = [i.provenance for i in inputs]
    feeds = {p.feed for p in ps}
    delays = {p.delay for p in ps}
    envs = {p.environment for p in ps}
    incomplete = [i.result_id for i in inputs if i.provenance.truncated]
    return Provenance(
        provider="marketlens",
        route=f"duckdb:{tool}",
        fetched_at=ctx.now(),
        as_of=max((p.as_of for p in ps if p.as_of is not None), default=None),
        feed=feeds.pop() if len(feeds) == 1 else None,
        delay=delays.pop() if len(delays) == 1 else Delay.UNKNOWN,
        environment=envs.pop() if len(envs) == 1 else None,
        request=args.model_dump(mode="json"),
        truncated=bool(incomplete),
        truncation_note=(
            f"Computed from {names(incomplete)}, which stopped at the fetch limits: the input is incomplete."
            if incomplete
            else None
        ),
        derived_from=list(dict.fromkeys(i.result_id for i in inputs)),
    )


def risk(inputs: Sequence[ResultInfo]) -> OutputRisk | None:
    """external_text when any input carries third-party text, else the spec's."""
    return "external_text" if any(i.risk == "external_text" for i in inputs) else None


@dataclass
class DerivedOutput(ToolOutput):
    """A ToolOutput that records the rows its own come from (``rows_of``,
    ``derived_rows_of``): the server's offload stores it with them when it
    stores the output. ToolOutput's own fields are the contract's."""

    rows_of: RowsOf | None = None


def model_output(
    table: pa.Table,
    model: type[CanonicalModel],
    prov: Provenance,
    *,
    notes: Sequence[str] = (),
    absent: dict[str, AbsenceReason] | None = None,
    inputs: Sequence[ResultInfo] = (),
    store: bool = False,
    rows_of: RowsOf | None = None,
) -> ToolOutput:
    """A canonical-model output; the server's offload decides inline or
    stored (always stored with ``store``), and stores it with ``rows_of``
    (the rows its own come from, ``derived_rows_of``)."""
    return DerivedOutput(
        model=model,
        provenance=prov,
        table=to_model_table(table, model),
        notes=list(notes),
        absent=dict(absent or {}),
        risk=risk(inputs),
        offload="always" if store else "auto",
        rows_of=rows_of,
    )


def utc_times(table: pa.Table) -> pa.Table:
    """Every zoned timestamp column labelled UTC (DuckDB labels them with the
    connection's zone; the instants are unchanged). Field metadata (a
    column's unit) is kept."""
    fields = [
        f.with_type(pa.timestamp(f.type.unit, tz="UTC"))
        if pa.types.is_timestamp(f.type) and f.type.tz is not None
        else f
        for f in table.schema
    ]
    return table.cast(pa.schema(fields))


def with_units(
    table: pa.Table, units: Mapping[str, str], sources: Mapping[str, str] | None = None
) -> pa.Table:
    """``table`` with each named column's unit in its Arrow field metadata
    (``{b"unit": ...}``), and with ``sources`` the model that computed it
    (``{b"source_model": ...}``): how a dynamic table labels its columns' units,
    which the inline answer and the store both read."""
    sources = sources or {}
    fields = []
    for f in table.schema:
        meta = {
            k: v.encode("utf-8")
            for k, v in ((b"unit", units.get(f.name)), (b"source_model", sources.get(f.name)))
            if v
        }
        fields.append(f.with_metadata({**(f.metadata or {}), **meta}) if meta else f)
    return table.cast(pa.schema(fields))


def _small(table: pa.Table) -> bool:
    if table.num_rows > INLINE_ROWS:
        return False
    text = json.dumps(table.to_pylist(), default=str, separators=(",", ":"), ensure_ascii=False)
    return math.ceil(len(text.encode("utf-8")) / 4) <= INLINE_TOKENS


def dynamic_output(
    ctx: ToolContext,
    tool: str,
    table: pa.Table,
    model: str,
    prov: Provenance,
    *,
    time_column: str | None,
    group_column: str | None,
    notes: Sequence[str] = (),
    absent: dict[str, AbsenceReason] | None = None,
    inputs: Sequence[ResultInfo] = (),
    units: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
    store: bool = False,
    rows_of: RowsOf | None = None,
) -> ToolOutput:
    """A dynamic-schema output. Small (and not ``store``): the table (inline).
    Large: stored here with its time and series columns, which the offload
    cannot know for a dynamic schema, so the stored result chains into other
    analytics tools. ``units`` labels columns (a price stays a price for the
    next tool), and ``sources`` the model each carried column was computed by
    (a drawdown stays a drawdown after its result is gone). ``rows_of``: the
    rows its own come from (``derived_rows_of``), stored with it either way."""
    out_risk = risk(inputs)
    units = {k: v for k, v in (units or {}).items() if v}
    table = with_units(utc_times(table), units, sources)
    if not store and _small(table):
        return DerivedOutput(
            model=model,
            provenance=prov,
            table=table,
            notes=list(notes),
            absent=dict(absent or {}),
            risk=out_risk,
            rows_of=rows_of,
        )
    info = ctx.results.put(
        table,
        tool=tool,
        model=model,
        provenance=prov,
        absent=dict(absent or {}),
        risk=out_risk or "api_structured",
        parents=list(prov.derived_from),
        time_column=time_column,
        group_column=group_column,
        column_units=units or None,
        # marketlens's own store's (the contract pins the ResultStore protocol's put).
        **({"rows_of": rows_of} if rows_of is not None else {}),
    )
    return ToolOutput(
        model=model, provenance=prov, stored=info, notes=list(notes), absent=dict(absent or {}), risk=out_risk
    )
