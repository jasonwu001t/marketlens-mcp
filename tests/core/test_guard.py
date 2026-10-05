"""The query guard (contract 5.4): one SELECT/WITH statement over this
session's results only, functions that touch files refused, a forced LIMIT,
then a DuckDB connection that cannot touch files at all."""

from __future__ import annotations

import pathlib
import re

import pytest
from coresupport import bar_rows, provenance

from marketlens_mcp.results import guard
from marketlens_mcp.results.arrow import rows_to_table
from marketlens_mcp.results.store import StoreLimits, StoreRoot
from marketlens_mcp.results_api import QueryRefused
from marketlens_schema.market import Bar

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "sql_attacks.txt"


def corpus() -> list[tuple[str, str]]:
    cases = []
    for line in CORPUS.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        code, _, sql = line.partition("\t")
        cases.append((code, sql.replace("\\n", "\n")))
    return cases


@pytest.fixture
def two_sessions(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits())
    mine, theirs = root.session("mine"), root.session("theirs")
    table = rows_to_table(bar_rows(n=30), Bar)
    ids = [
        mine.put(
            table,
            tool="market_bars",
            model=Bar.schema_name,
            provenance=provenance(),
            time_column="t",
            group_column="ticker",
        ).result_id
        for _ in range(9)
    ]
    other = theirs.put(table, tool="market_bars", model=Bar.schema_name, provenance=provenance()).result_id
    return mine, ids, other


def expand(sql: str, ids: list[str], other: str) -> str:
    return (
        sql.replace("{A}", ids[0])
        .replace("{B}", ids[1])
        .replace("{OTHER}", other)
        .replace("{NINE}", ", ".join(ids))
    )


def test_corpus_is_large_enough():
    cases = corpus()
    assert len([c for c in cases if c[0] != "ok"]) >= 40
    assert len([c for c in cases if c[0] == "ok"]) >= 10


@pytest.mark.parametrize(("code", "sql"), corpus(), ids=lambda v: v[:60] if isinstance(v, str) else v)
def test_corpus(two_sessions, code, sql):
    mine, ids, other = two_sessions
    sql = expand(sql, ids, other)
    if code == "ok":
        checked = guard.check(sql, live_ids=mine.live_ids())
        assert set(checked.result_ids) <= set(ids)
        out = mine.query(sql, max_rows=7)
        assert out.row_count <= 7
        assert "LIMIT" in out.executed_sql.upper()
    else:
        with pytest.raises(QueryRefused) as info:
            guard.check(sql, live_ids=mine.live_ids())
        assert info.value.code == code, info.value.message
        # The refusal is readable and never names a filesystem path.
        assert str(mine.root) not in info.value.message


def test_refusal_names_the_statement_kind():
    with pytest.raises(QueryRefused) as info:
        guard.check("INSERT INTO r_0123456789 VALUES (1)", live_ids={"r_0123456789"})
    assert "Insert" in info.value.message
    with pytest.raises(QueryRefused) as info:
        guard.check("LOAD httpfs", live_ids=set())
    assert "Command" in info.value.message


def test_sql_table_lists_live_ids(two_sessions):
    mine, ids, other = two_sessions
    with pytest.raises(QueryRefused) as info:
        guard.check(f"SELECT * FROM {other}", live_ids=mine.live_ids())
    assert info.value.code == "sql_table"
    assert ids[0] in info.value.message
    assert other in info.value.message  # it names what was refused


def test_a_cte_name_used_out_of_its_scope_says_so():
    sql = "SELECT name, setting FROM (WITH pg_settings AS (SELECT 1) SELECT 1) q, pg_settings"
    with pytest.raises(QueryRefused) as info:
        guard.check(sql, live_ids={"r_0123456789"})
    assert info.value.code == "sql_table"
    assert "pg_settings" in info.value.message
    assert "only inside the WITH that defines it" in info.value.message
    assert "r_0123456789" in info.value.message


def test_scope_analysis_failure_fails_closed(monkeypatch):
    def broken(_root):
        raise RuntimeError("cannot scope this")

    monkeypatch.setattr(guard, "_cte_references", broken)
    with pytest.raises(QueryRefused) as info:
        guard.check("SELECT * FROM r_0123456789", live_ids={"r_0123456789"})
    assert info.value.code == "sql_parse"
    assert "table references" in info.value.message


def test_too_long_and_nul():
    with pytest.raises(QueryRefused) as info:
        guard.check("SELECT 1 " + " " * 20_001, live_ids=set())
    assert info.value.code == "sql_too_long"
    with pytest.raises(QueryRefused) as info:
        guard.check("SELECT 1\x00", live_ids=set())
    assert info.value.code == "sql_empty"


def test_forced_limit_rules():
    def limited(sql, cap):
        return guard.render(guard.force_limit(guard.check(sql, live_ids={"r_0123456789"}).ast, cap))

    assert limited("SELECT * FROM r_0123456789", 50).endswith("LIMIT 51")
    assert limited("SELECT * FROM r_0123456789 LIMIT 10", 50).endswith("LIMIT 10")
    assert limited("SELECT * FROM r_0123456789 LIMIT 1000", 50).endswith("LIMIT 51")
    assert limited("SELECT * FROM r_0123456789 LIMIT 1000 OFFSET 3", 50).endswith("LIMIT 51 OFFSET 3")
    union = limited("SELECT 1 AS a UNION SELECT 2 AS a", 5)
    assert union.startswith("SELECT * FROM (") and union.endswith(") AS q LIMIT 6")
    sub = limited("SELECT * FROM r_0123456789 LIMIT (SELECT 3)", 5)
    assert sub.endswith("LIMIT 6")


def test_cap_and_truncation(two_sessions):
    mine, ids, _ = two_sessions
    out = mine.query(f"SELECT * FROM {ids[0]}", max_rows=10)
    assert out.row_count == 10
    assert out.truncated is True
    out = mine.query(f"SELECT * FROM {ids[0]} LIMIT 3", max_rows=10)
    assert out.row_count == 3 and out.truncated is False


def test_recursive_cte_past_the_timeout_is_interrupted(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits(query_timeout_seconds=1))
    s = root.session("s")
    with pytest.raises(QueryRefused) as info:
        s.query(
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT count(*) FROM t",
            max_rows=5,
        )
    assert info.value.code == "sql_timeout"


def test_duckdb_errors_are_first_line_and_scrubbed(two_sessions):
    mine, ids, _ = two_sessions
    with pytest.raises(QueryRefused) as info:
        mine.query(f"SELECT no_such_column FROM {ids[0]}", max_rows=5)
    assert info.value.code == "sql_error"
    assert "\n" not in info.value.message
    assert "no_such_column" in info.value.message


def test_scrub_replaces_paths_but_not_pairs_or_words():
    cache = "/var/folders/xy/T/pytest-1/cache"
    msg = f'Permission Error: Cannot access file "{cache}/results/abc/r_0123456789.parquet" - disabled'
    assert guard.scrub(msg, cache) == 'Permission Error: Cannot access file "<path>" - disabled'
    assert guard.scrub("Cannot open C:\\Users\\me\\x.db now") == "Cannot open <path> now"
    assert guard.scrub("ticker BTC/USD and 1/2 are fine") == "ticker BTC/USD and 1/2 are fine"
    assert guard.scrub("open /etc/hosts") == "open <path>"


def test_lockdown_proofs_in_a_result_session(two_sessions):
    """Server-side SQL in a ResultSession cannot touch files either: the
    guard is the belt, the connection settings are the wall."""
    mine, ids, _ = two_sessions
    attempts = [
        "SELECT * FROM read_csv('/etc/hosts')",
        f"COPY {ids[0]} TO '{mine.root}/x.csv'",
        f"ATTACH '{mine.root}/x.db'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "SELECT * FROM glob('/*')",
    ]
    with mine.open([ids[0]]) as session:
        for sql in attempts:
            with pytest.raises(Exception) as info:  # noqa: B017 - any DuckDB error is the point
                session.query(sql)
            assert not re.search(r"(?<![\w.\-/<])/(?:etc|var|tmp|private|Users|home)/", str(info.value)), str(
                info.value
            )
        assert session.query("SELECT current_setting('TimeZone') AS tz").to_pylist() == [{"tz": "UTC"}]
    assert not (mine.root / "x.csv").exists()
    assert not (mine.root / "x.db").exists()
