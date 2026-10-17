"""The result store's public interface: what tools, analytics and plugins use.

Owner: ml-core (seeded verbatim from the contract; implemented in
``marketlens_mcp.results``). ml-analytics and plugins code against the
Protocols here and never import the implementation.

Model-facing words: a stored result is named by its ``result_id``
(``r_`` + 10 lowercase hex). The same string is the table name in SQL
(``SELECT * FROM r_0123456789``). No filesystem path ever leaves the store.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from marketlens_schema import BUILTIN_MODELS, CanonicalModel
from marketlens_schema.base import AbsenceReason, PaginationState, Provenance, UtcDatetime

if TYPE_CHECKING:  # pyarrow is a runtime dependency of the server, not of this interface
    import pyarrow as pa

RESULT_ID_RE = re.compile(r"^r_[0-9a-f]{10}$")

OutputRisk = Literal["api_structured", "external_text"]
JSONScalar = str | int | float | bool | None

INLINE_KIND = "inline"
STORED_KIND = "stored"

#: Names of a timestamp column holding when each row became known, beside the
#: time the row describes (a dataset's knowledge_time, a store's ingested_at).
#: Without its role available (a query answer that cannot show it),
#: analytics_align still matches on such a column by default, as a lower bound,
#: and says the match is not shown point in time.
CLOCK_NAMES = ("knowledge_time", "ingested_at")


class ColumnInfo(BaseModel):
    """One column of a result, typed from the canonical model (or from DuckDB
    for dynamic results). ``min``/``max``/``nulls`` are filled for stored
    results (the marker's per-column summary); None on inline results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str = Field(
        description='DuckDB type name: VARCHAR, DOUBLE, BIGINT, BOOLEAN, DATE, "TIMESTAMP WITH TIME ZONE", "DECIMAL(38,10)", VARCHAR[], STRUCT(...), MAP(VARCHAR, VARCHAR)'
    )
    unit: str | None = None
    nullable: bool = True
    nulls: int | None = None
    min: JSONScalar = None
    max: JSONScalar = None
    null_reason: AbsenceReason | None = None
    description: str | None = None
    source_model: str | None = Field(
        None,
        # Only carried columns have one: every other column's JSON stays as it was.
        exclude_if=lambda v: v is None,
        description="A column a query answer or aligned result carried, with its unit, from another result: the "
        "model of the result that computed it (e.g. marketlens.DrawdownPoint), recorded when this result was "
        "created, so it outlives that result. Left out when None.",
    )
    role: Literal["available"] | None = Field(
        None,
        exclude_if=lambda v: v is None,
        description="available: a timestamp column holding when each row became known (published or first "
        "available), as opposed to the time column, the time the row describes; analytics_align matches a row "
        "from the later of the two by default, so no row is paired with a time before it was known or before "
        "its own. A model states it in its field's "
        'JSON Schema (x-role), a dynamic table in the column\'s Arrow field metadata ({b"role": b"available"}), '
        "and a stored query answer keeps it on such a column it passes through unchanged (same name and type) "
        "or builds as max() or greatest() of such columns from every result it read that has one, with each row "
        "computed from the rows it keeps the clocks of (no window function, subquery in an expression or "
        "self-join; a joined result dated by its time column, such as quotes, with that time passed through as the "
        "answer's or in the clock), and only when every result it read shows when its rows became known: by such "
        "a column, or as fetched rows known at their time column and fixed from then on (quotes, trades: not bars, "
        "known only at their end, nor snapshots, as of their fetch, nor orders or news, whose fields change later, "
        "nor a plugin's rows dated by the period they describe) or holding at every time (exchanges, condition "
        "codes: not assets or positions, whose values are as fetched); never a computed result without the role. "
        "analytics_align matches fetched bars, an answer passing them through or an analytics output of them "
        "(rows_of) from the end of the period each row covers, a snapshot from its fetch, a bucket of other rows "
        "from its end, refuses a result computed from dated rows that shows neither unless match_on=time, and "
        "says a match on a result with no such column is not shown point in time unless it holds such fetched "
        "rows as they were then (not bars adjusted for later splits and dividends). Analytics outputs have none, "
        "whatever their model states. Left out when None.",
    )


class RowsOf(BaseModel):
    """The rows a stored result holds or was computed from: what shows when
    each of its rows became known after those rows are gone. A row is known no
    earlier than the end of the period it covers (a bar's, a bucket's) and a
    snapshot at its fetch. Fetched market data is known by its model's rule;
    other rows (a dataset's) only no earlier than their time, so a bucket of
    them is recorded with its span and matched from its end, not shown point
    in time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(
        description="Its model, e.g. marketlens.Bar, or marketlens.QueryRow for a dataset's rows"
    )
    fetched_at: UtcDatetime
    adjustment: str | None = Field(
        None,
        description="Its price adjustment (market_bars: raw, split, dividend or all) where the result holds price "
        "levels; None when it has none, or holds only ratios of them (returns, volatility, beta)",
    )
    span: str | None = Field(
        None,
        description="The period each row covers from its time column (a bar's timeframe, a bucket's, e.g. 1d), "
        "when the result has no column of its model's span; None when each row is known at its time",
    )
    lag_seconds: int = Field(
        0,
        ge=0,
        description="How long after the end of that period a row can still be known: a UTC bucket's last stock "
        "bar ends at midnight New York time, 4 or 5 hours later; 0 for most",
    )
    shown: bool = Field(
        True,
        description="False when the result was computed from these rows in a way that shows neither when each of "
        "its rows became known nor when its period ends (a bucket dated at its start, lead() or another window, "
        "a join of bars, rows aligned forward, a knowledge_time left out): analytics_align refuses it unless "
        "match_on=time",
    )


class ResultInfo(BaseModel):
    """Everything known about one stored result (the sidecar JSON next to its
    Parquet file, minus the file path)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(pattern=RESULT_ID_RE.pattern)
    tool: str
    model: str = Field(
        description="Schema name of the rows, e.g. marketlens.Bar, or marketlens.QueryRow / marketlens.Aligned for dynamic results"
    )
    schema_version: str
    row_count: int
    bytes: int
    columns: list[ColumnInfo]
    created_at: UtcDatetime
    expires_at: UtcDatetime
    provenance: Provenance
    absent: dict[str, AbsenceReason] = Field(default_factory=dict)
    pagination: PaginationState | None = None
    risk: OutputRisk = "api_structured"
    parents: list[str] = Field(default_factory=list, description="Result ids this result was derived from")
    time_column: str | None = None
    group_column: str | None = None
    rows_of: RowsOf | None = Field(
        None,
        description="The rows its own hold or were computed from: a query answer passing one result's rows through "
        "unchanged (filtered or projected, its time column and a bar's timeframe kept as they are or left out), or "
        "an analytics output of fetched market data or a bucket of other rows, so analytics_align takes each row as "
        "known no earlier than the rows it covers were (a bar at its end, a bucket at its end, a snapshot at its "
        "fetch); marked not shown for a result computed from dated rows that shows neither; None otherwise",
    )


def known_model(info: ResultInfo) -> type[CanonicalModel] | None:
    """The built-in model whose fetched rows ``info`` holds or was computed
    from, when that model's rows are known by a rule (``known_at_time``: by
    their time column, or the end of their span, and fixed from then on;
    ``known_at_fetch``: at their fetch): the result as fetched, or a query
    answer or analytics output that records such rows (``rows_of``) and shows
    when its own rows became known; None for any other result."""
    if info.rows_of is not None:
        if not info.rows_of.shown:
            return None
        name = info.rows_of.model
    elif info.parents or info.tool == "results_query":
        return None
    else:
        name = info.model
    cls = BUILTIN_MODELS.get(name)
    if cls is None or not (cls.known_at_time or cls.known_at_fetch) or info.time_column != cls.time_column:
        return None
    if cls.span_column is not None and known_span(info, cls) is None:
        return None
    return cls


def source_rows(info: ResultInfo) -> RowsOf | None:
    """The rows ``info`` holds or was computed from: what it records
    (``rows_of``), or itself when it is a fetch of market data known by a rule
    (``known_model``); None otherwise."""
    if info.rows_of is not None:
        return info.rows_of
    cls = known_model(info)
    if cls is None:
        return None
    adjustment = info.provenance.request.get("adjustment")
    return RowsOf(
        model=cls.schema_name,
        fetched_at=info.provenance.fetched_at,
        adjustment=None if adjustment is None else str(adjustment),
    )


def known_late(src: RowsOf) -> bool:
    """Whether rows of ``src`` are known only after their time column: bars
    (at their end), snapshots (at their fetch), a bucket of rows (at its end),
    or a result already marked not shown; so a result computed from them that
    shows none of it is marked not shown, whatever its time column."""
    cls = BUILTIN_MODELS.get(src.model)
    return (
        not src.shown
        or src.span is not None
        or src.lag_seconds > 0
        or (cls is not None and (cls.span_column is not None or cls.known_at_fetch))
    )


def period_rows(info: ResultInfo) -> RowsOf | None:
    """The rows ``info`` was computed from when they are not fetched market
    data known by a rule (``known_model``) and it records the period each of
    its rows covers (``rows_of`` shown, with a span: a week's bucket of a
    dataset's rows): each row is known no earlier than that period's end, and
    nothing shows more; None otherwise."""
    src = info.rows_of
    if src is None or not src.shown or src.span is None or known_model(info) is not None:
        return None
    return src


def implied_time(stamps: Sequence[str]) -> str | None:
    """The time column of a result that records none, from its timestamp
    columns: the only one, ``t`` among several, or ``event_time`` beside
    clocks (``CLOCK_NAMES``) alone; None otherwise."""
    if len(stamps) > 1 and "t" in stamps:
        return "t"
    if len(stamps) > 1 and "event_time" in stamps and set(stamps) - {"event_time"} <= set(CLOCK_NAMES):
        return "event_time"
    return stamps[0] if len(stamps) == 1 else None


def known_span(info: ResultInfo, cls: type[CanonicalModel] | None) -> str | None:
    """SQL giving the span of each row of ``info`` (``cls.span_column``: a
    bar's timeframe): that column, else the one span its rows were recorded
    with (``rows_of``: a bucket's too); None without either."""
    if cls is not None and cls.span_column in {c.name for c in info.columns}:
        return '"' + cls.span_column.replace('"', '""') + '"'
    if info.rows_of is not None and info.rows_of.span is not None:
        return "'" + info.rows_of.span.replace("'", "''") + "'"
    return None


def unshown_known(info: ResultInfo) -> str | None:
    """Why nothing shows when each row of a result with no column of role
    available became known, as a clause following its result id (results_query
    keeps no role on an answer reading it; analytics_align says a match on it is
    not shown point in time); None for rows fetched as a built-in model that
    is known by a rule (``known_model``: quotes, trades at their time; bars at
    their end; snapshots at their fetch; exchanges, condition codes), or
    computed from such rows recording when its own are, unless the fetch
    adjusted them for what happened after their time (bars with adjustment
    other than raw)."""
    cls = known_model(info)
    period = period_rows(info)
    if period is not None:
        return (
            f"was computed by {info.tool} from {period.model} rows with no column of role available (when each "
            "became known), dated by the time each describes, which can be before it was published"
        )
    if cls is None and (info.parents or info.tool == "results_query"):
        return (
            f"was computed by {info.tool} with no column of role available (when each row became known), so its "
            "rows can hold values known after their time (a full-sample average, lead(), a bucket dated at its start)"
        )
    src = info.rows_of
    fetched_at = src.fetched_at if src is not None else info.provenance.fetched_at
    fetched = f"as fetched at {fetched_at.astimezone(UTC):%Y-%m-%dT%H:%MZ}"
    if cls is not None:
        adjustment = src.adjustment if src is not None else info.provenance.request.get("adjustment")
        if adjustment in (None, "raw"):
            return None
        holds = (
            "holds"
            if info.tool == "results_query" or not info.parents
            else f"was computed by {info.tool} from"
        )
        return (
            f"{holds} {cls.schema_name} rows adjusted for corporate actions (adjustment={adjustment}) {fetched}: their "
            "prices are rescaled for each split or dividend up to then, after their time too, so a price level holds "
            "what became known later (a return between two of them does not); fetch them with adjustment=raw to "
            "match point in time"
        )
    cls = BUILTIN_MODELS.get(info.model)
    if info.time_column and cls is None:
        return (
            f"holds {info.model} rows with no column of role available (when each became known), dated by "
            f"{info.time_column}, the time a row describes, which can be before it was published (only fetched "
            "market data, such as quotes or bars, is taken as known by its time)"
        )
    if info.time_column:
        return (
            f"holds {info.model} rows with no column of role available (when each became known), {fetched}, which "
            f"can hold what became known after their time column {info.time_column} (an order's later fill, a news "
            "item's update, a day's activity placed at its start)"
        )
    dates = [c.name for c in info.columns if c.type.startswith(("DATE", "TIMESTAMP"))]
    if dates:
        return (
            f"holds rows dated ({', '.join(dates)}) with neither a time column nor a column of role available (when "
            "each became known), so a row can be dated or published after the time it is put beside"
        )
    return (
        f"holds {info.model} rows with no date column, {fetched}, so nothing shows a row was known before then (a "
        "position's price or an asset's status is as of then, and a date held as text is not read as one)"
    )


class ReadyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str
    sql: str


class Preview(BaseModel):
    """A time-series-shaped look at a stored result without loading it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    time_column: str | None = None
    group_column: str | None = None
    time_span_start: UtcDatetime | None = None
    time_span_end: UtcDatetime | None = None
    groups_count: int | None = None
    groups_sample: list[str] = Field(default_factory=list, description="Up to 20 group values")
    first_rows: list[dict[str, Any]] = Field(
        default_factory=list, description="First 3 rows by time (or storage order)"
    )
    last_rows: list[dict[str, Any]] = Field(
        default_factory=list, description="Last 3 rows by time (or storage order)"
    )


class ResultMarker(BaseModel):
    """What a tool returns instead of rows when the result is stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["stored"] = "stored"
    result_id: str
    tool: str
    model: str
    schema_version: str
    row_count: int
    bytes: int
    columns: list[ColumnInfo]
    preview: Preview
    provenance: Provenance
    absent: dict[str, AbsenceReason] = Field(default_factory=dict)
    pagination: PaginationState | None = None
    expires_at: UtcDatetime
    risk: OutputRisk
    parents: list[str] = Field(default_factory=list)
    queries: list[ReadyQuery] = Field(min_length=3, max_length=3)
    notes: list[str] = Field(default_factory=list)
    how_to: str = Field(description="One fixed sentence telling the model how to use the handle")


class InlineResult(BaseModel):
    """What a tool returns when the result is small enough to show."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["inline"] = "inline"
    tool: str
    model: str
    schema_version: str
    row_count: int
    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]
    provenance: Provenance
    absent: dict[str, AbsenceReason] = Field(default_factory=dict)
    pagination: PaginationState | None = None
    notes: list[str] = Field(default_factory=list)


ToolResponse = InlineResult | ResultMarker


class StoreUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: int
    bytes: int
    max_bytes: int
    oldest_created_at: datetime | None = None


class ResultNotFound(LookupError):
    """A result id the store cannot serve. ``reason`` is what the model is told."""

    def __init__(self, result_id: str, reason: Literal["unknown", "expired", "evicted", "dropped"]):
        super().__init__(f"result {result_id} is {reason}")
        self.result_id = result_id
        self.reason = reason


class QueryRefused(ValueError):
    """The query guard refused a statement. ``message`` is readable and names
    the rule; it never contains a filesystem path."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@runtime_checkable
class ResultSession(Protocol):
    """A read-only working set over some stored results: a fresh in-memory
    DuckDB connection that loaded exactly those results as tables named by
    their result ids, then turned external access off and locked its
    configuration. Server-authored SQL only (analytics); the model's SQL goes
    through the query guard instead."""

    def table(self, result_id: str) -> str:
        """The SQL identifier of a loaded result (equal to the result id).
        Raises KeyError for a result this session did not load."""
        ...

    def info(self, result_id: str) -> ResultInfo: ...

    def query(self, sql: str, params: Sequence[Any] = ()) -> pa.Table:
        """Run one server-authored statement; return all rows as Arrow.
        Bounded by the store's memory limit and query timeout."""
        ...


@runtime_checkable
class ResultStore(Protocol):
    """One MCP session's view of the store."""

    @property
    def session_key(self) -> str:
        """Opaque key of this session's directory (never shown to the model)."""
        ...

    def info(self, result_id: str) -> ResultInfo:
        """Raises ResultNotFound."""
        ...

    def list(self) -> list[ResultInfo]:
        """This session's live results, newest first."""
        ...

    def put(
        self,
        table: pa.Table,
        *,
        tool: str,
        model: str,
        provenance: Provenance,
        absent: dict[str, AbsenceReason] | None = None,
        pagination: PaginationState | None = None,
        risk: OutputRisk = "api_structured",
        parents: Sequence[str] = (),
        time_column: str | None = None,
        group_column: str | None = None,
        column_units: dict[str, str] | None = None,
    ) -> ResultInfo:
        """Write ``table`` as a new Parquet result in this session, compute its
        column summary, run eviction, and return its info. ``table`` must use
        the canonical Arrow types for ``model`` (marketlens_mcp.results.arrow)
        or, for dynamic models, any DuckDB-readable Arrow types, whose fields
        may carry a column's unit (``{b"unit": b"percent"}``) and role
        (``{b"role": b"available"}``: when each row became known) in metadata.
        marketlens's own store also takes ``rows_of`` (``RowsOf``: the rows a
        computed result's rows come from, and when each became known), which
        the server's offload passes for an analytics output recording them, as
        an analytics tool storing a dynamic output itself does. This
        signature is the contract's (tests/core/test_interfaces.py pins it)."""
        ...

    def open(self, result_ids: Sequence[str]) -> AbstractContextManager[ResultSession]:
        """A ResultSession over exactly these results (ResultNotFound if any is
        not live in this session)."""
        ...

    def read(self, result_id: str, *, columns: Sequence[str] | None = None, limit: int) -> pa.Table:
        """Up to ``limit`` rows (storage order). For previews and exports."""
        ...

    def drop(self, result_id: str) -> bool:
        """Delete one of this session's results. False if it was not live."""
        ...

    def usage(self) -> StoreUsage:
        """Totals across all sessions in the cache directory."""
        ...
