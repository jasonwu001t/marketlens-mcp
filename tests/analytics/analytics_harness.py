"""Test harness of the analytics tools (owner: ml-analytics). Imported by
conftest.py (fixtures) and by the test modules (helpers); the module name is
unique across tests/ so it never collides with another lane's helpers.

Everything here is synthetic and offline. The result store is a local fake
implementing the ``results_api.ResultStore`` protocol: a dict of Arrow tables
which ``open()`` loads into a fresh in-memory DuckDB with the same lock-down as
the real store (external access off, configuration locked). The fake session
deliberately runs with a non-UTC time zone so that every test proves the
analytics bucket and label times in UTC whatever the connection's zone.
Expected values are computed in the tests by hand or with ``statistics``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import enum
import logging
import socket
import sys
import types
import typing
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pytest

# Before the core lane merges, ``marketlens_mcp`` is imported from the interface
# stubs (PYTHONPATH=src:<stub>), whose placeholder ``analytics`` package would
# shadow this checkout's ``src/marketlens_mcp/analytics``. Put this checkout's
# package directory first. Once ``src/marketlens_mcp`` is the real package this
# is a no-op (the directory already is the package's path).
_SRC_PKG = Path(__file__).resolve().parents[2] / "src" / "marketlens_mcp"
import marketlens_mcp as _pkg  # noqa: E402

if _SRC_PKG.is_dir() and str(_SRC_PKG) not in [str(Path(p).resolve()) for p in _pkg.__path__]:
    _pkg.__path__.insert(0, str(_SRC_PKG))
    for _name in [
        m for m in sys.modules if m == "marketlens_mcp.analytics" or m.startswith("marketlens_mcp.analytics.")
    ]:
        sys.modules.pop(_name)

from marketlens_mcp.plugin_api import FetchLimits, ToolOutput, ToolSpec  # noqa: E402
from marketlens_mcp.results_api import ColumnInfo, ResultInfo, ResultNotFound, StoreUsage  # noqa: E402
from marketlens_schema import BUILTIN_MODELS, SCHEMA_VERSION, CanonicalModel  # noqa: E402
from marketlens_schema.base import (  # noqa: E402
    AbsenceReason,
    Environment,
    Provenance,
    unexplained_absences,
)
from marketlens_schema.market import Bar  # noqa: E402

NOW = dt.datetime(2026, 10, 2, 20, 0, tzinfo=dt.UTC)
FAKE_SESSION_TIMEZONE = "Asia/Tokyo"  # deliberately not UTC
DAY = dt.timedelta(days=1)
MINUTE = dt.timedelta(minutes=1)


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


# --- network guard (the shared tests/conftest.py adds the same guard after the merge) ---------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost") or isinstance(address, str):
            return real_connect(self, address)
        raise RuntimeError(f"test tried to reach the network: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield


# --- canonical Arrow tables (independent of the code under test) ------------------------------

_MAP = pa.map_(pa.string(), pa.string())


def _arrow(annotation: Any) -> tuple[pa.DataType, bool]:
    nullable = False
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        if origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            nullable = nullable or len(args) != len(typing.get_args(annotation))
            annotation = args[0]
            continue
        break
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return pa.string(), nullable
    if origin is list:
        return pa.list_(_arrow(typing.get_args(annotation)[0])[0]), nullable
    if origin is dict:
        return _MAP, True
    if issubclass(annotation, bool):
        return pa.bool_(), nullable
    if issubclass(annotation, (str, enum.Enum)):
        return pa.string(), nullable
    if issubclass(annotation, int):
        return pa.int64(), nullable
    if issubclass(annotation, float):
        return pa.float64(), nullable
    if issubclass(annotation, Decimal):
        return pa.decimal128(38, 12), nullable
    if issubclass(annotation, dt.datetime):
        return pa.timestamp("us", tz="UTC"), nullable
    if issubclass(annotation, dt.date):
        return pa.date32(), nullable
    raise TypeError(annotation)


def schema_of(model: type[CanonicalModel]) -> pa.Schema:
    """Contract 2.4: fields in declaration order, ``absent`` last."""
    names = [n for n in model.model_fields if n != "absent"] + ["absent"]
    fields = []
    for n in names:
        typ, nullable = _arrow(model.model_fields[n].annotation)
        fields.append(pa.field(n, typ, nullable or n == "absent"))
    return pa.schema(fields)


def table_of(model: type[CanonicalModel], rows: Sequence[CanonicalModel]) -> pa.Table:
    schema = schema_of(model)
    data = []
    for r in rows:
        d = r.model_dump(mode="python")
        if d.get("absent") is not None:
            d["absent"] = {str(k): str(getattr(v, "value", v)) for k, v in d["absent"].items()}
        data.append({k: (v.value if isinstance(v, enum.Enum) else v) for k, v in d.items()})
    return pa.Table.from_pylist(data, schema=schema)


def rows_of(table: pa.Table, model: type[CanonicalModel]) -> list[CanonicalModel]:
    out = []
    for row in table.to_pylist():
        if row.get("absent") is not None:
            row["absent"] = dict(row["absent"])
        out.append(model.model_validate(row))
    return out


def bars(
    ticker: str,
    closes: Sequence[float],
    *,
    start: dt.datetime = utc(2026, 1, 5, 21),
    step: dt.timedelta = DAY,
    times: Sequence[dt.datetime] | None = None,
    timeframe: str = "1d",
    opens: Sequence[float] | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
    vwaps: Sequence[float | None] | None = None,
    trade_counts: Sequence[int | None] | None = None,
    asset_class: str = "us_equity",
) -> list[Bar]:
    times = list(times) if times is not None else [start + i * step for i in range(len(closes))]
    out = []
    for i, c in enumerate(closes):
        vw = vwaps[i] if vwaps is not None else c
        tc = trade_counts[i] if trade_counts is not None else 10
        absent = {}
        if vw is None:
            absent["vwap"] = "not_provided_by_source"
        if tc is None:
            absent["trade_count"] = "not_provided_by_source"
        out.append(
            Bar(
                ticker=ticker,
                asset_class=asset_class,
                timeframe=timeframe,
                t=times[i],
                open=opens[i] if opens is not None else c,
                high=highs[i] if highs is not None else c,
                low=lows[i] if lows is not None else c,
                close=c,
                volume=volumes[i] if volumes is not None else 1000.0,
                trade_count=tc,
                vwap=vw,
                absent=absent or None,
            )
        )
    return out


def source_provenance(
    *, route: str = "GET /v2/stocks/bars", request: Mapping[str, Any] | None = None, **extra: Any
) -> Provenance:
    return Provenance(
        provider="synthetic",
        route=route,
        fetched_at=NOW,
        as_of=utc(2026, 10, 2, 20),
        feed="iex",
        delay="realtime",
        request=dict(request or {}),
        **extra,
    )


# --- the fake result store --------------------------------------------------------------------

_DUCKDB_CONFIG = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "threads": 2,
    "memory_limit": "1GB",
    "max_temp_directory_size": "0B",
}


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:", config=dict(_DUCKDB_CONFIG))
    con.execute(f"SET TimeZone = '{FAKE_SESSION_TIMEZONE}'")
    return con


def _json_scalar(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if isinstance(v, (dt.date, Decimal)):
        return str(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return None


def _unit(model: type[CanonicalModel] | None, name: str) -> str | None:
    if model is None or name not in model.model_fields:
        return None
    extra = model.model_fields[name].json_schema_extra
    return extra.get("x-unit") if isinstance(extra, dict) else None


class FakeSession:
    """ResultSession over some of the fake store's tables."""

    def __init__(self, store: FakeStore, result_ids: Sequence[str]):
        self._store = store
        self._infos = {rid: store.info(rid) for rid in dict.fromkeys(result_ids)}
        self.con = _connect()
        for rid in self._infos:
            self.con.register("_incoming", store.tables[rid])
            self.con.execute(f"CREATE TABLE {rid} AS SELECT * FROM _incoming")
            self.con.unregister("_incoming")
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

    def table(self, result_id: str) -> str:
        if result_id not in self._infos:
            raise KeyError(result_id)
        return result_id

    def info(self, result_id: str) -> ResultInfo:
        if result_id not in self._infos:
            raise KeyError(result_id)
        return self._infos[result_id]

    def query(self, sql: str, params: Sequence[Any] = ()) -> pa.Table:
        self._store.queries.append(sql)
        return self.con.execute(sql, list(params)).to_arrow_table()


@dataclass
class FakeStore:
    """``results_api.ResultStore`` over in-memory Arrow tables."""

    tables: dict[str, pa.Table] = field(default_factory=dict)
    infos: dict[str, ResultInfo] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)
    puts: list[dict[str, Any]] = field(default_factory=list)
    _n: int = 0

    @property
    def session_key(self) -> str:
        return "fake"

    def info(self, result_id: str) -> ResultInfo:
        if result_id in self.infos:
            return self.infos[result_id]
        raise ResultNotFound(result_id, "dropped" if result_id in self.dropped else "unknown")

    def list(self) -> list[ResultInfo]:
        return sorted(self.infos.values(), key=lambda i: i.created_at, reverse=True)

    def put(
        self,
        table: pa.Table,
        *,
        tool: str,
        model: str,
        provenance: Provenance,
        absent: dict[str, AbsenceReason] | None = None,
        pagination: Any = None,
        risk: str = "api_structured",
        parents: Sequence[str] = (),
        time_column: str | None = None,
        group_column: str | None = None,
        column_units: dict[str, str] | None = None,
    ) -> ResultInfo:
        cls = BUILTIN_MODELS.get(model)
        if cls is not None:
            time_column = time_column or cls.time_column
            group_column = group_column or cls.group_column
        names = set(table.column_names)
        time_column = time_column if time_column in names else None
        group_column = group_column if group_column in names else None
        con = duckdb.connect()
        con.register("t", table)
        described = con.execute("DESCRIBE SELECT * FROM t").fetchall()
        con.close()
        columns = []
        for (name, typ, *_rest), arrow_field in zip(described, table.schema, strict=True):
            col = table.column(name)
            lo = hi = None
            if not (pa.types.is_nested(arrow_field.type)) and col.null_count < len(col):
                mm = pc.min_max(col)
                lo, hi = _json_scalar(mm["min"].as_py()), _json_scalar(mm["max"].as_py())
            columns.append(
                ColumnInfo(
                    name=name,
                    type=typ,
                    unit=(column_units or {}).get(name) or _unit(cls, name),
                    nullable=arrow_field.nullable,
                    nulls=col.null_count,
                    min=lo,
                    max=hi,
                )
            )
        self._n += 1
        rid = f"r_{self._n:010x}"
        info = ResultInfo(
            result_id=rid,
            tool=tool,
            model=model,
            schema_version=SCHEMA_VERSION,
            row_count=table.num_rows,
            bytes=table.nbytes,
            columns=columns,
            created_at=NOW + self._n * dt.timedelta(seconds=1),
            expires_at=NOW + dt.timedelta(hours=24),
            provenance=provenance,
            absent=dict(absent or {}),
            pagination=pagination,
            risk=risk,
            parents=list(parents),
            time_column=time_column,
            group_column=group_column,
        )
        self.tables[rid] = table
        self.infos[rid] = info
        self.puts.append({"result_id": rid, "tool": tool, "model": model, "parents": list(parents)})
        return info

    @contextlib.contextmanager
    def _session(self, result_ids: Sequence[str]) -> Iterator[FakeSession]:
        session = FakeSession(self, result_ids)
        try:
            yield session
        finally:
            session.con.close()

    def open(self, result_ids: Sequence[str]) -> contextlib.AbstractContextManager[FakeSession]:
        return self._session(result_ids)

    def read(self, result_id: str, *, columns: Sequence[str] | None = None, limit: int) -> pa.Table:
        t = self.tables[self.info(result_id).result_id]
        return (t.select(list(columns)) if columns else t).slice(0, limit)

    def drop(self, result_id: str) -> bool:
        if result_id not in self.infos:
            return False
        del self.infos[result_id], self.tables[result_id]
        self.dropped.add(result_id)
        return True

    def usage(self) -> StoreUsage:
        return StoreUsage(results=len(self.infos), bytes=0, max_bytes=5 * 2**30)

    # -- helpers for tests ---------------------------------------------------------------------

    def put_rows(
        self,
        rows: Sequence[CanonicalModel],
        *,
        tool: str = "market_bars",
        provenance: Provenance | None = None,
        absent: dict[str, AbsenceReason] | None = None,
        risk: str = "api_structured",
    ) -> str:
        model = type(rows[0])
        return self.put(
            table_of(model, rows),
            tool=tool,
            model=model.schema_name,
            provenance=provenance or source_provenance(),
            absent=absent,
            risk=risk,
        ).result_id

    def put_dynamic(
        self,
        table: pa.Table,
        *,
        model: str = "marketlens.QueryRow",
        tool: str = "results_query",
        risk: str = "api_structured",
        time_column: str | None = None,
        group_column: str | None = None,
        absent: dict[str, AbsenceReason] | None = None,
    ) -> str:
        return self.put(
            table,
            tool=tool,
            model=model,
            provenance=Provenance(provider="marketlens", route="duckdb:results_query", fetched_at=NOW),
            risk=risk,
            time_column=time_column,
            group_column=group_column,
            absent=absent,
        ).result_id

    def keep(self, out: ToolOutput, tool: str) -> str:
        """Store a handler's table output the way the server's offload does."""
        model = out.model if isinstance(out.model, str) else out.model.schema_name
        return self.put(
            out.table,
            tool=tool,
            model=model,
            provenance=out.provenance,
            absent=out.absent,
            risk=out.risk or "api_structured",
            parents=out.provenance.derived_from,
        ).result_id


# --- the fake tool context ------------------------------------------------------------------


@dataclass
class FakeContext:
    """``plugin_api.ToolContext`` for analytics handlers (no upstream calls)."""

    results: FakeStore
    tool: str = "analytics_test"
    session_id: str = "fake-session"
    settings: Mapping[str, Any] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset({"analytics", "results", "market"})
    portfolio_environment: Environment = Environment.PAPER
    limits: FetchLimits = field(default_factory=FetchLimits)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("marketlens.test"))

    def env(self, name: str) -> str | None:
        raise KeyError(name)

    def now(self) -> dt.datetime:
        return NOW

    async def paginate(self, fetch_page, *, start_token=None):  # pragma: no cover - never used
        raise AssertionError("analytics never paginate")

    def limiter(self, key: str, per_minute: int):  # pragma: no cover - never used
        raise AssertionError("analytics never call upstream")


def run(spec: ToolSpec, ctx: FakeContext, **arguments: Any) -> ToolOutput:
    """Validate the arguments and run the handler, as the server does before offload."""
    ctx.tool = spec.name
    args = spec.input_model.model_validate(arguments)
    out = asyncio.run(spec.handler(ctx, args))
    assert isinstance(out, ToolOutput)
    return out


def output_rows(out: ToolOutput, model: type[CanonicalModel]) -> list[CanonicalModel]:
    """The output's rows as model instances; asserts the canonical Arrow schema
    and the absence rule on the way."""
    assert out.table is not None, "expected a table output"
    assert out.table.schema.equals(schema_of(model)), f"{out.table.schema}\n!=\n{schema_of(model)}"
    rows = rows_of(out.table, model)
    assert unexplained_absences(rows, out.absent) == []
    return rows


def dyn_rows(out: ToolOutput) -> list[dict[str, Any]]:
    assert out.table is not None
    return out.table.to_pylist()


def unexplained_dynamic(
    rows: list[dict[str, Any]], response_absent: Mapping[str, Any]
) -> list[tuple[int, str]]:
    """The absence rule for dynamic outputs: every None explained by the row's
    absent map or the response-level map."""
    missing = []
    for i, row in enumerate(rows):
        own = dict(row.get("absent") or {})
        for k, v in row.items():
            if k != "absent" and v is None and k not in own and k not in response_absent:
                missing.append((i, k))
    return missing


__all__ = [
    "DAY",
    "MINUTE",
    "NOW",
    "FakeContext",
    "FakeStore",
    "bars",
    "dyn_rows",
    "output_rows",
    "rows_of",
    "run",
    "schema_of",
    "source_provenance",
    "table_of",
    "unexplained_dynamic",
    "utc",
]
