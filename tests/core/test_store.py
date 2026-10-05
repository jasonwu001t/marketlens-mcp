"""The DuckDB-over-Parquet result store (contract 5.1): per-session
directories, TTL, a size cap with oldest-first eviction, orphan cleanup, a
portable lock, and no filesystem path in anything it returns."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
import time

import pyarrow as pa
import pytest
from coresupport import FIXED_NOW, bar_rows, bar_table, news_rows, provenance

from marketlens_mcp.results.arrow import rows_to_table
from marketlens_mcp.results.store import StoreLimits, StoreLockTimeout, StoreRoot
from marketlens_mcp.results_api import RESULT_ID_RE, ResultInfo, ResultNotFound, ResultStore
from marketlens_schema import AbsenceCode, AbsenceReason
from marketlens_schema.market import Bar, NewsItem


class Clock:
    def __init__(self, now=FIXED_NOW):
        self.now = now

    def __call__(self):
        return self.now


def put_bars(store, n=10, **kw):
    table = rows_to_table(bar_rows(n=n), Bar)
    return store.put(
        table,
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
        **kw,
    )


def test_temp_store_satisfies_the_protocol(store):
    assert isinstance(store, ResultStore)
    assert store.session_key == "test"


def test_put_returns_info_with_column_summary(tmp_path):
    clock = Clock()
    root = StoreRoot(tmp_path / "cache", StoreLimits(), clock=clock)
    s = root.session("s1")
    absent = {"vwap": AbsenceReason(code=AbsenceCode.NOT_PROVIDED_BY_SOURCE, detail="omitted on some bars")}
    info = put_bars(s, absent=absent)
    assert RESULT_ID_RE.match(info.result_id)
    assert info.row_count == 30
    assert info.bytes > 0
    assert info.created_at == FIXED_NOW
    assert info.expires_at == FIXED_NOW + dt.timedelta(hours=24)
    assert info.time_column == "t" and info.group_column == "ticker"
    cols = {c.name: c for c in info.columns}
    assert cols["ticker"].min == "AAPL" and cols["ticker"].max == "NVDA" and cols["ticker"].nulls == 0
    assert cols["t"].type == "TIMESTAMP WITH TIME ZONE"
    assert cols["t"].min == "2026-01-05T14:30:00Z"
    assert cols["close"].unit == "price"
    assert cols["vwap"].nulls == 6
    assert cols["vwap"].null_reason == absent["vwap"]
    assert cols["absent"].min is None and cols["absent"].max is None
    assert s.info(info.result_id) == info


def test_varchar_min_max_truncated_to_64(store):
    t = pa.table({"s": ["x" * 100, "y" * 3]})
    info = store.put(t, tool="t", model="marketlens.QueryRow", provenance=provenance())
    col = info.columns[0]
    assert len(col.min) == 64 and col.max == "yyy"


def test_list_is_newest_first_and_session_scoped(tmp_path):
    clock = Clock()
    root = StoreRoot(tmp_path / "cache", StoreLimits(), clock=clock)
    a, b = root.session("a"), root.session("b")
    first = put_bars(a)
    clock.now += dt.timedelta(seconds=5)
    second = put_bars(a)
    other = put_bars(b)
    assert [i.result_id for i in a.list()] == [second.result_id, first.result_id]
    assert a.live_ids() == {first.result_id, second.result_id}
    with pytest.raises(ResultNotFound) as info:
        a.info(other.result_id)
    assert info.value.reason == "unknown"


def test_read_and_open(store):
    info = put_bars(store)
    t = store.read(info.result_id, columns=["ticker", "close"], limit=4)
    assert t.column_names == ["ticker", "close"] and t.num_rows == 4
    with store.open([info.result_id]) as session:
        assert session.table(info.result_id) == info.result_id
        assert session.info(info.result_id) == info
        out = session.query(f"SELECT count(*) AS n FROM {info.result_id}")
        assert out.to_pylist() == [{"n": 30}]
        with pytest.raises(KeyError):
            session.table("r_0000000000")
    with pytest.raises(ResultNotFound):
        store.open(["r_0000000000"]).__enter__()


def test_drop_then_reasons(store):
    info = put_bars(store)
    assert store.drop(info.result_id) is True
    assert store.drop(info.result_id) is False
    with pytest.raises(ResultNotFound) as e:
        store.info(info.result_id)
    assert e.value.reason == "dropped"
    with pytest.raises(ResultNotFound) as e:
        store.info("r_0123456789")
    assert e.value.reason == "unknown"


def test_ttl_expiry(tmp_path):
    clock = Clock()
    root = StoreRoot(tmp_path / "cache", StoreLimits(ttl_hours=1), clock=clock)
    s = root.session("s")
    info = put_bars(s)
    clock.now += dt.timedelta(minutes=59)
    assert s.info(info.result_id).result_id == info.result_id
    clock.now += dt.timedelta(minutes=2)
    with pytest.raises(ResultNotFound) as e:
        s.info(info.result_id)
    assert e.value.reason == "expired"
    root.evict()
    assert list((tmp_path / "cache" / "results").rglob("r_*")) == []


def test_size_cap_evicts_oldest_first_down_to_90_percent(tmp_path):
    clock = Clock()
    probe_root = StoreRoot(tmp_path / "probe", StoreLimits(), clock=clock)
    one = put_bars(probe_root.session("p"), n=200).bytes
    cap = int(one * 3.5)
    root = StoreRoot(tmp_path / "cache", StoreLimits(max_bytes=cap), clock=clock)
    a, b = root.session("a"), root.session("b")
    infos = []
    for i in range(4):
        clock.now += dt.timedelta(seconds=1)
        infos.append(put_bars(a if i % 2 == 0 else b, n=200))
    usage = root.usage()
    assert usage.bytes <= cap * 0.9
    with pytest.raises(ResultNotFound) as e:
        a.info(infos[0].result_id)
    assert e.value.reason == "evicted"
    assert b.info(infos[3].result_id)


def test_orphans_and_empty_session_dirs_are_removed(tmp_path):
    clock = Clock()
    root = StoreRoot(tmp_path / "cache", StoreLimits(), clock=clock)
    s = root.session("s")
    put_bars(s)
    results = tmp_path / "cache" / "results"
    old_orphan = results / "s" / "r_aaaaaaaaaa.parquet"
    young_orphan = results / "s" / "r_bbbbbbbbbb.parquet"
    old_orphan.write_bytes(b"x")
    young_orphan.write_bytes(b"x")
    two_hours_ago = FIXED_NOW.timestamp() - 7200
    os.utime(old_orphan, (two_hours_ago, two_hours_ago))
    os.utime(young_orphan, (FIXED_NOW.timestamp(), FIXED_NOW.timestamp()))
    (results / "empty_session").mkdir()
    root.evict()
    assert not old_orphan.exists()
    assert young_orphan.exists()
    assert not (results / "empty_session").exists()


def test_stale_lock_is_broken_and_a_held_lock_times_out(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits(), lock_timeout=0.3)
    s = root.session("s")
    lock = tmp_path / "cache" / "results" / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("x")
    old = time.time() - 120
    os.utime(lock, (old, old))
    put_bars(s)  # stale lock (older than 60 s) is broken
    assert not lock.exists()
    lock.write_text("x")
    with pytest.raises(StoreLockTimeout):
        root.evict()
    lock.unlink()


def test_sidecar_has_no_path_and_files_are_private(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits())
    s = root.session("s")
    info = put_bars(s)
    sidecar = tmp_path / "cache" / "results" / "s" / f"{info.result_id}.json"
    text = sidecar.read_text()
    assert str(tmp_path) not in text
    ResultInfo.model_validate(json.loads(text)["info"])
    assert str(tmp_path) not in info.model_dump_json()
    if sys.platform != "win32":
        parquet = sidecar.with_suffix(".parquet")
        assert stat.S_IMODE(parquet.stat().st_mode) == 0o600
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
        assert stat.S_IMODE(sidecar.parent.stat().st_mode) == 0o700


def test_usage_counts_every_session(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits())
    put_bars(root.session("a"))
    put_bars(root.session("b"))
    u = root.session("a").usage()
    assert u.results == 2
    assert u.max_bytes == StoreLimits().max_bytes
    assert u.oldest_created_at is not None


def test_risk_and_parents_are_kept(store):
    info = store.put(
        rows_to_table(news_rows(), NewsItem),
        tool="news_search",
        model=NewsItem.schema_name,
        provenance=provenance(),
        risk="external_text",
        parents=["r_0123456789"],
    )
    assert info.risk == "external_text"
    assert info.parents == ["r_0123456789"]


def test_large_put_is_fast_enough(store):
    t0 = time.perf_counter()
    info = store.put(
        bar_table(),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
    )
    assert info.row_count == 60_000
    assert time.perf_counter() - t0 < 20


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_cache_and_results_dirs_are_private(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits())
    put_bars(root.session("s"))
    for d in (tmp_path / "cache", tmp_path / "cache" / "results"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_put_survives_a_busy_lock(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits(), lock_timeout=0.2)
    s = root.session("s")
    lock = tmp_path / "cache" / "results" / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held by another process")
    info = put_bars(s)  # eviction is skipped (it runs again later), the result is kept
    assert s.info(info.result_id).row_count == 30
    lock.unlink()
