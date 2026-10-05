"""The result store (contract 5.1 and 5.4 steps 6 and 8-11).

Layout::

    <cache_dir>/results/
      .lock                      portable lock file
      <session_key>/
        r_<10 hex>.parquet       zstd; written to a temp name, then renamed
        r_<10 hex>.json          sidecar: {"info": ResultInfo, "preview": Preview}

A ``StoreRoot`` owns the cache directory (eviction, usage, purge); a
``FileResultStore`` is one MCP session's view (it implements the
``results_api.ResultStore`` protocol). No filesystem path ever leaves this
module in a return value or an error text.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from marketlens_schema import BUILTIN_MODELS, SCHEMA_VERSION, CanonicalModel, PaginationState, Provenance

from ..results_api import (
    RESULT_ID_RE,
    AbsenceReason,
    ColumnInfo,
    OutputRisk,
    Preview,
    QueryRefused,
    ResultInfo,
    ResultMarker,
    ResultNotFound,
    StoreUsage,
)
from . import evict as _evict
from . import guard
from .arrow import column_infos, column_infos_for, column_infos_from_schema, json_safe, sql_ident
from .evict import StoreLockTimeout
from .marker import GROUPS_SAMPLE, PREVIEW_ROWS, build_marker, truncate_strings, with_null_reasons

__all__ = ["FileResultStore", "QueryOutcome", "StoreLimits", "StoreLockTimeout", "StoreRoot"]

_NESTED_PREFIXES = ("STRUCT", "MAP")

log = logging.getLogger("marketlens")


@dataclass(frozen=True)
class StoreLimits:
    """Result settings (config ``results.*`` plus ``fetch.max_rows``)."""

    inline_max_rows: int = 200
    inline_max_tokens: int = 6000
    query_max_rows: int = 200
    query_max_bytes: int = 24_000
    query_timeout_seconds: float = 10
    query_memory_limit: str = "1GB"
    ttl_hours: float = 24
    max_bytes: int = 5 * 2**30
    fetch_max_rows: int = 50_000
    export_dir: str | None = None

    @classmethod
    def from_config(cls, results: Mapping[str, Any], fetch_max_rows: int = 50_000) -> StoreLimits:
        return cls(
            inline_max_rows=results["inline_max_rows"],
            inline_max_tokens=results["inline_max_tokens"],
            query_max_rows=results["query_max_rows"],
            query_max_bytes=results["query_max_bytes"],
            query_timeout_seconds=results["query_timeout_seconds"],
            query_memory_limit=results["query_memory_limit"],
            ttl_hours=results["ttl_hours"],
            max_bytes=int(results["max_store_gb"] * 2**30),
            fetch_max_rows=fetch_max_rows,
            export_dir=results.get("export_dir"),
        )


@dataclass(frozen=True)
class QueryOutcome:
    """What a guarded results_query produced (before inline-or-store)."""

    table: pa.Table
    row_count: int
    truncated: bool
    executed_sql: str
    result_ids: tuple[str, ...]
    risk: OutputRisk


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _private_file(path: pathlib.Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o600)


def _private_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def connect(limits: StoreLimits) -> duckdb.DuckDBPyConnection:
    """A fresh in-memory connection with extension auto-install/-load off."""
    con = duckdb.connect(
        ":memory:",
        config={
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "threads": 2,
            "memory_limit": limits.query_memory_limit,
            "max_temp_directory_size": "0B",
        },
    )
    con.execute("SET TimeZone = 'UTC'")
    return con


def lock_down(con: duckdb.DuckDBPyConnection) -> None:
    """After this, the connection cannot read or write any file, install or
    load an extension, or change its configuration back."""
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")


class StoreRoot:
    """The cache directory's result store: sessions, eviction, usage."""

    def __init__(
        self,
        cache_dir: pathlib.Path,
        limits: StoreLimits | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        lock_timeout: float = 5.0,
        models: Mapping[str, type[CanonicalModel]] | None = None,
    ):
        self.cache_dir = pathlib.Path(cache_dir)
        self.results_dir = self.cache_dir / "results"
        self.limits = limits or StoreLimits()
        self.clock = clock or _utcnow
        self.lock_timeout = lock_timeout
        self.models: dict[str, type[CanonicalModel]] = dict(BUILTIN_MODELS)
        if models:
            self.models.update(models)
        self._sessions: dict[str, FileResultStore] = {}
        self._mutex = threading.Lock()

    def now(self) -> datetime:
        return self.clock()

    def session(self, session_key: str) -> FileResultStore:
        with self._mutex:
            if session_key not in self._sessions:
                self._sessions[session_key] = FileResultStore(self, session_key)
            return self._sessions[session_key]

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        _private_dir(self.cache_dir)
        _private_dir(self.results_dir)
        with _evict.store_lock(self.results_dir, timeout=self.lock_timeout):
            yield

    def evict(self, *, protect: Sequence[str] = ()) -> _evict.EvictReport:
        with self.lock():
            return _evict.evict(
                self.results_dir, now=self.now(), max_bytes=self.limits.max_bytes, protect=protect
            )

    def usage(self) -> StoreUsage:
        entries = [e for e in _evict.scan(self.results_dir) if e.expires_at > self.now()]
        return StoreUsage(
            results=len(entries),
            bytes=sum(e.bytes for e in entries),
            max_bytes=self.limits.max_bytes,
            oldest_created_at=min((e.created_at for e in entries), default=None),
        )

    def purge(self) -> int:
        """Delete every stored result in the cache directory (all sessions)."""
        with self.lock():
            entries = _evict.scan(self.results_dir)
            for e in entries:
                for f in e.files:
                    with contextlib.suppress(FileNotFoundError):
                        f.unlink()
            _evict.evict(self.results_dir, now=self.now(), max_bytes=self.limits.max_bytes)
            return len(entries)


class _Session:
    """ResultSession: a locked-down in-memory DuckDB over some results."""

    def __init__(self, store: FileResultStore, infos: dict[str, ResultInfo]):
        self._store = store
        self._infos = infos
        self._con = connect(store.limits)
        try:
            for rid in infos:
                self._con.execute(
                    f"CREATE TABLE {rid} AS SELECT * FROM read_parquet(?)", [str(store._parquet(rid))]
                )
            lock_down(self._con)
        except Exception:
            self._con.close()
            raise

    def table(self, result_id: str) -> str:
        if result_id not in self._infos:
            raise KeyError(result_id)
        return result_id

    def info(self, result_id: str) -> ResultInfo:
        if result_id not in self._infos:
            raise KeyError(result_id)
        return self._infos[result_id]

    def query(self, sql: str, params: Sequence[Any] = ()) -> pa.Table:
        return self._store._run(self._con, sql, params)

    def close(self) -> None:
        self._con.close()


class FileResultStore:
    """One MCP session's view of the store (``results_api.ResultStore``)."""

    def __init__(self, root: StoreRoot, session_key: str):
        self._root = root
        self._key = session_key
        self._created: dict[str, datetime] = {}
        self._dropped: set[str] = set()

    # -- protocol ---------------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return self._key

    @property
    def limits(self) -> StoreLimits:
        return self._root.limits

    @property
    def root(self) -> pathlib.Path:
        """The cache directory (for scrubbing; never shown to the model)."""
        return self._root.cache_dir

    @property
    def models(self) -> Mapping[str, type[CanonicalModel]]:
        return self._root.models

    def now(self) -> datetime:
        return self._root.now()

    def info(self, result_id: str) -> ResultInfo:
        info, _ = self._load(result_id)
        return info

    def list(self) -> list[ResultInfo]:
        out = []
        if self._dir.is_dir():
            for sidecar in self._dir.glob("r_*.json"):
                try:
                    out.append(self.info(sidecar.stem))
                except ResultNotFound:
                    continue
        return sorted(out, key=lambda i: (i.created_at, i.result_id), reverse=True)

    def live_ids(self) -> set[str]:
        return {i.result_id for i in self.list()}

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
        absent = dict(absent or {})
        cls = self.models.get(model)
        table, columns = self._columns(table, cls, column_units or {})
        if cls is not None:
            time_column = time_column or cls.time_column
            group_column = group_column or cls.group_column
        names = set(table.column_names)
        time_column = time_column if time_column in names else None
        group_column = group_column if group_column in names else None
        columns, preview = self._summarise(table, columns, time_column, group_column)
        columns = with_null_reasons(columns, absent)
        _private_dir(self._dir)
        rid = self._new_id()
        parquet = self._parquet(rid)
        tmp = parquet.with_name(parquet.name + ".tmp")
        pq.write_table(table, tmp, compression="zstd")
        _private_file(tmp)
        os.replace(tmp, parquet)
        now = self.now()
        info = ResultInfo(
            result_id=rid,
            tool=tool,
            model=model,
            schema_version=SCHEMA_VERSION,
            row_count=table.num_rows,
            bytes=parquet.stat().st_size,
            columns=columns,
            created_at=now,
            expires_at=now + timedelta(hours=self.limits.ttl_hours),
            provenance=provenance,
            absent=absent,
            pagination=pagination,
            risk=risk,
            parents=list(parents),
            time_column=time_column,
            group_column=group_column,
        )
        sidecar = self._sidecar(rid)
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        tmp.write_text(
            json.dumps({"info": info.model_dump(mode="json"), "preview": preview.model_dump(mode="json")}),
            encoding="utf-8",
        )
        _private_file(tmp)
        os.replace(tmp, sidecar)
        self._created[rid] = info.expires_at
        try:
            self._root.evict(protect=[rid])
        except StoreLockTimeout:
            log.warning("result store eviction skipped after a put: the store is locked by another process")
        return info

    def open(self, result_ids: Sequence[str]) -> contextlib.AbstractContextManager[_Session]:
        infos = {rid: self.info(rid) for rid in dict.fromkeys(result_ids)}

        @contextlib.contextmanager
        def _ctx() -> Iterator[_Session]:
            session = _Session(self, infos)
            try:
                yield session
            finally:
                session.close()

        return _ctx()

    def read(self, result_id: str, *, columns: Sequence[str] | None = None, limit: int) -> pa.Table:
        self.info(result_id)
        table = pq.read_table(
            self._parquet(result_id), columns=list(columns) if columns is not None else None
        )
        return table.slice(0, max(0, limit))

    def drop(self, result_id: str) -> bool:
        try:
            self.info(result_id)
        except ResultNotFound:
            return False
        for f in (self._parquet(result_id), self._sidecar(result_id)):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
        self._dropped.add(result_id)
        return True

    def usage(self) -> StoreUsage:
        return self._root.usage()

    # -- beyond the protocol (results tools) ------------------------------------------------

    def marker(self, result_id: str, *, notes: Sequence[str] = ()) -> ResultMarker:
        info, preview = self._load(result_id)
        return build_marker(info, preview, notes=notes, models=self.models)

    def query(self, sql: str, *, max_rows: int, store: bool = False) -> QueryOutcome:
        """Guard, force the limit, run in a locked-down connection (5.4)."""
        checked = guard.check(sql, live_ids=self.live_ids())
        cap = self.limits.fetch_max_rows if store else max_rows
        executed = guard.render(guard.force_limit(checked.ast, cap))
        with self.open(checked.result_ids) as session:
            table = session.query(executed)
            risks = {session.info(rid).risk for rid in checked.result_ids}
        truncated = table.num_rows > cap
        table = table.slice(0, cap)
        return QueryOutcome(
            table=table,
            row_count=table.num_rows,
            truncated=truncated,
            executed_sql=executed,
            result_ids=checked.result_ids,
            risk="external_text" if "external_text" in risks else "api_structured",
        )

    # -- internals ---------------------------------------------------------------------------

    @property
    def _dir(self) -> pathlib.Path:
        return self._root.results_dir / self._key

    def _parquet(self, rid: str) -> pathlib.Path:
        return self._dir / f"{rid}.parquet"

    def _sidecar(self, rid: str) -> pathlib.Path:
        return self._dir / f"{rid}.json"

    def _new_id(self) -> str:
        while True:
            rid = "r_" + secrets.token_hex(5)
            if not self._sidecar(rid).exists() and rid not in self._created:
                return rid

    def _load(self, result_id: str) -> tuple[ResultInfo, Preview]:
        if not isinstance(result_id, str) or not RESULT_ID_RE.match(result_id):
            raise ResultNotFound(str(result_id)[:40], "unknown")
        sidecar = self._sidecar(result_id)
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            info = ResultInfo.model_validate(data["info"])
            preview = Preview.model_validate(data.get("preview") or {})
        except FileNotFoundError:
            info = None
        except (ValueError, KeyError, TypeError):
            raise ResultNotFound(result_id, "evicted") from None
        now = self.now()
        if info is not None and self._parquet(result_id).exists():
            if info.expires_at <= now:
                raise ResultNotFound(result_id, "expired")
            return info, preview
        if result_id in self._dropped:
            raise ResultNotFound(result_id, "dropped")
        if result_id in self._created:
            raise ResultNotFound(result_id, "expired" if self._created[result_id] <= now else "evicted")
        raise ResultNotFound(result_id, "unknown")

    def _run(self, con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> pa.Table:
        timer = threading.Timer(self.limits.query_timeout_seconds, con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            return con.execute(sql, list(params)).to_arrow_table()
        except duckdb.InterruptException:
            raise QueryRefused(
                "sql_timeout",
                f"The query ran longer than {self.limits.query_timeout_seconds:g} seconds and was stopped.",
            ) from None
        except duckdb.Error as exc:
            text = guard._first_line(str(exc))
            raise QueryRefused("sql_error", guard.scrub(text, str(self._root.cache_dir))[:500]) from None
        finally:
            timer.cancel()

    def _columns(
        self, table: pa.Table, cls: type[CanonicalModel] | None, units: Mapping[str, str]
    ) -> tuple[pa.Table, list[ColumnInfo]]:
        if cls is not None:
            from .arrow import arrow_schema

            schema = arrow_schema(cls)
            if table.column_names == schema.names:
                with contextlib.suppress(pa.ArrowInvalid, pa.ArrowNotImplementedError):
                    table = table.cast(schema)
                if table.schema.equals(schema):
                    infos = column_infos(cls)
                    return table, [
                        c.model_copy(update={"unit": units[c.name]}) if c.name in units else c for c in infos
                    ]
            return table, column_infos_for(table.schema, cls, units)
        return table, column_infos_from_schema(table.schema, units)

    def _summarise(
        self, table: pa.Table, columns: list[ColumnInfo], time_column: str | None, group_column: str | None
    ) -> tuple[list[ColumnInfo], Preview]:
        con = connect(self.limits)
        try:
            lock_down(con)
            con.register("t", table)
            parts = []
            for i, c in enumerate(columns):
                ident = sql_ident(c.name)
                parts.append(f"count(*) - count({ident}) AS n{i}")
                if not c.type.startswith(_NESTED_PREFIXES) and not c.type.endswith("]"):
                    parts.append(f"min({ident}) AS lo{i}, max({ident}) AS hi{i}")
            stats = (
                con.execute(f"SELECT {', '.join(parts)} FROM t").to_arrow_table().to_pylist()[0]
                if parts
                else {}
            )
            out = []
            for i, c in enumerate(columns):
                lo, hi = json_safe(stats.get(f"lo{i}")), json_safe(stats.get(f"hi{i}"))
                if isinstance(lo, str):
                    lo = lo[:64]
                if isinstance(hi, str):
                    hi = hi[:64]
                if isinstance(lo, (dict, list)):
                    lo = hi = None
                out.append(c.model_copy(update={"nulls": int(stats.get(f"n{i}", 0)), "min": lo, "max": hi}))
            preview = self._preview(con, out, time_column, group_column, table.num_rows)
        finally:
            con.close()
        return out, preview

    def _preview(
        self,
        con: duckdb.DuckDBPyConnection,
        columns: list[ColumnInfo],
        time_column: str | None,
        group_column: str | None,
        n: int,
    ) -> Preview:
        def rows(sql: str) -> list[dict[str, Any]]:
            out = []
            for r in con.execute(sql).to_arrow_table().to_pylist():
                d = {k: truncate_strings(json_safe(v)) for k, v in r.items() if k != "_ord"}
                if d.get("absent") is None:
                    d.pop("absent", None)
                out.append(d)
            return out

        cols = ", ".join(sql_ident(c.name) for c in columns)
        if time_column:
            t = sql_ident(time_column)
            g = sql_ident(group_column) if group_column else None
            asc = f"{t} ASC NULLS LAST" + (f", {g} ASC" if g else "")
            desc = f"{t} DESC NULLS LAST" + (f", {g} DESC" if g else "")
            first = rows(f"SELECT {cols} FROM t ORDER BY {asc} LIMIT {PREVIEW_ROWS}")
            last = rows(f"SELECT {cols} FROM t ORDER BY {desc} LIMIT {PREVIEW_ROWS}")[::-1]
        else:
            first = rows(f"SELECT {cols} FROM t LIMIT {PREVIEW_ROWS}")
            start = max(0, n - PREVIEW_ROWS)
            last = rows(f"SELECT {cols} FROM t LIMIT {PREVIEW_ROWS} OFFSET {start}")
        span_start = span_end = None
        by_name = {c.name: c for c in columns}
        if time_column and by_name[time_column].type.startswith("TIMESTAMP"):
            span_start, span_end = by_name[time_column].min, by_name[time_column].max
        groups_count, groups_sample = None, []
        if group_column:
            g = sql_ident(group_column)
            groups_count = (
                con.execute(f"SELECT count(DISTINCT {g}) AS n FROM t").to_arrow_table().to_pylist()[0]["n"]
            )
            groups_sample = [
                str(json_safe(r["g"]))
                for r in con.execute(
                    f"SELECT DISTINCT {g} AS g FROM t WHERE {g} IS NOT NULL ORDER BY 1 LIMIT {GROUPS_SAMPLE}"
                )
                .to_arrow_table()
                .to_pylist()
            ]
        return Preview(
            time_column=time_column,
            group_column=group_column,
            time_span_start=span_start,
            time_span_end=span_end,
            groups_count=groups_count,
            groups_sample=groups_sample,
            first_rows=first,
            last_rows=last,
        )
