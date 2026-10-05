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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from marketlens_schema.base import AbsenceReason, PaginationState, Provenance, UtcDatetime

if TYPE_CHECKING:  # pyarrow is a runtime dependency of the server, not of this interface
    import pyarrow as pa

RESULT_ID_RE = re.compile(r"^r_[0-9a-f]{10}$")

OutputRisk = Literal["api_structured", "external_text"]
JSONScalar = str | int | float | bool | None

INLINE_KIND = "inline"
STORED_KIND = "stored"


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
        or, for dynamic models, any DuckDB-readable Arrow types."""
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
