"""The query guard (contract 5.4): one SELECT/WITH statement over this
session's results only, functions that touch files refused, a forced LIMIT,
then a DuckDB connection that cannot touch files at all."""

from __future__ import annotations

import pathlib
import re

import duckdb
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
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
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
    assert info.value.hint is None


def test_scrub_replaces_paths_but_not_pairs_or_words():
    cache = "/var/folders/xy/T/pytest-1/cache"
    msg = f'Permission Error: Cannot access file "{cache}/results/abc/r_0123456789.parquet" - disabled'
    assert guard.scrub(msg, cache) == 'Permission Error: Cannot access file "<path>" - disabled'
    assert guard.scrub("Cannot open C:\\Users\\me\\x.db now") == "Cannot open <path> now"
    assert guard.scrub("ticker BTC/USD and 1/2 are fine") == "ticker BTC/USD and 1/2 are fine"
    assert guard.scrub("open /etc/hosts") == "open <path>"
    # An echoed one-segment input names no directory: it stays as typed.
    assert guard.scrub("'/USD' is not a crypto pair") == "'/USD' is not a crypto pair"
    assert guard.scrub("see /tmp/") == "see <path>"


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


def test_using_sample_runs_with_and_without_its_own_order_and_limit(two_sessions):
    """sqlglot writes USING SAMPLE after ORDER BY and LIMIT, where DuckDB
    refuses it, so every USING SAMPLE query failed with a parser error. Over
    one table it is that table's TABLESAMPLE; over a join it is wrapped, and
    refused by name next to ORDER BY or LIMIT."""
    mine, ids, _ = two_sessions
    a, b = ids[0], ids[1]
    for sql, rows in (
        (f"SELECT * FROM {a} USING SAMPLE 10 ROWS (reservoir, 42)", 10),
        (f"SELECT close FROM {a} USING SAMPLE 10 ROWS (reservoir, 42) ORDER BY close LIMIT 5", 5),
        (f"SELECT * FROM {a} x WHERE x.close > 0 USING SAMPLE 50% (bernoulli, 7) LIMIT 3", 3),
        (f"SELECT * FROM (SELECT * FROM {a} USING SAMPLE 4 ROWS (reservoir, 1) LIMIT 2) q", 2),
        (f"SELECT x.t FROM {a} x JOIN {b} y ON x.t = y.t USING SAMPLE 6 ROWS (reservoir, 3)", 6),
    ):
        out = mine.query(sql, max_rows=50)
        assert out.row_count == rows, sql
        assert "USING SAMPLE" not in out.executed_sql or "LIMIT 51" in out.executed_sql
    same = mine.query(
        f"SELECT close FROM {a} USING SAMPLE 10 ROWS (reservoir, 42) ORDER BY close", max_rows=50
    )
    assert same.executed_sql.endswith(
        "TABLESAMPLE RESERVOIR (10 ROWS) REPEATABLE (42) ORDER BY close LIMIT 51"
    )
    with pytest.raises(QueryRefused) as info:
        mine.query(f"SELECT x.t FROM {a} x JOIN {b} y ON x.t = y.t USING SAMPLE 6 ROWS LIMIT 2", max_rows=50)
    assert info.value.code == "sql_sample" and "subquery" in info.value.message


def test_using_sample_over_a_subquery_is_its_tablesample(two_sessions):
    """2026-10-19: USING SAMPLE over a subquery, next to an ORDER BY or LIMIT
    of its own, was refused as sql_sample "over a join", though there is no
    join and DuckDB takes a subquery's TABLESAMPLE."""
    mine, ids, _ = two_sessions
    sql = f"SELECT * FROM (SELECT close FROM {ids[0]}) q USING SAMPLE 5 ROWS (reservoir, 42) ORDER BY close LIMIT 3"
    out = mine.query(sql, max_rows=50)
    assert out.row_count == 3
    assert out.executed_sql.endswith(
        ") AS q TABLESAMPLE RESERVOIR (5 ROWS) REPEATABLE (42) ORDER BY close LIMIT 3"
    )
    with pytest.raises(QueryRefused) as info:
        mine.query("SELECT * FROM (VALUES (1), (2)) v(x) USING SAMPLE 1 ROWS ORDER BY x", max_rows=50)
    assert info.value.code == "sql_sample" and "over a join, VALUES or UNNEST" in info.value.message


def test_a_percent_or_with_ties_limit_is_wrapped_not_lowered(two_sessions):
    """LIMIT 100 PERCENT (or 100%) was lowered to LIMIT 51 PERCENT, a share of
    every row rather than a row count, so the whole answer of a cross join
    reached Python before the cap; WITH TIES likewise returns more rows than
    its count."""

    def limited(sql, cap):
        return guard.render(guard.force_limit(guard.check(sql, live_ids={"r_0123456789"}).ast, cap))

    for sql in (
        "SELECT * FROM r_0123456789 LIMIT 100 PERCENT",
        "SELECT * FROM r_0123456789 LIMIT 100%",
        "SELECT * FROM r_0123456789 ORDER BY 1 LIMIT 5 WITH TIES",
    ):
        rendered = limited(sql, 50)
        assert rendered.startswith("SELECT * FROM (SELECT * FROM r_0123456789") and rendered.endswith(
            ") AS q LIMIT 51"
        ), rendered
    mine, ids, _ = two_sessions
    a = ids[0]
    out = mine.query(f"SELECT x.close FROM {a} x, {a} y LIMIT 100 PERCENT", max_rows=5)
    assert out.executed_sql.endswith(") AS q LIMIT 6")
    assert out.row_count == 5 and out.truncated is True


def test_a_keyword_used_as_a_with_name_gets_a_hint(two_sessions):
    """2026-10-20: a WITH query named map failed as "Expecting (. Line 3,
    Col: 21." with nothing saying that map is a keyword (a DuckDB function)."""
    mine, ids, _ = two_sessions
    sql = (
        f"WITH h AS (SELECT ticker FROM {ids[0]}),\n"
        f"map AS (SELECT ticker, close FROM {ids[0]})\n"
        "SELECT h.ticker, map.close FROM h JOIN map ON h.ticker = map.ticker"
    )
    with pytest.raises(QueryRefused) as info:
        guard.check(sql, live_ids=mine.live_ids())
    assert info.value.code == "sql_parse"
    assert info.value.message == "The SQL does not parse: Expecting (. Line 3, Col: 21."
    assert info.value.hint == guard.keyword_hint(["map"])
    assert info.value.hint == (
        "map is a SQL keyword here, so it cannot be a bare name (a WITH name, alias or column): "
        'rename it, or double-quote it wherever it names something ("map", "map".col).'
    )


def test_a_reserved_word_as_a_with_name():
    with pytest.raises(QueryRefused) as info:
        guard.check("WITH union AS (SELECT 1 AS a) SELECT * FROM union", live_ids=set())
    assert info.value.code == "sql_parse"
    assert info.value.hint == guard.keyword_hint(["union"])
    assert guard.keyword_hint(["union", "map", "end", "left"]) == (
        "union, map, end are SQL keywords here, so they cannot be bare names (WITH names, aliases or "
        'columns): rename them, or double-quote them wherever they name something ("union", "union".col).'
    )
    assert guard.keyword_hint([]) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT close FROM {A} WHERE AND close = 1",
        "SELECT (close FROM {A}",
        "SELECT close FROM {A} ORDER close",
        "WITH map AS (SELECT * FROM {A}) SELECT * FROM map WHERE (",
    ],
)
def test_ordinary_syntax_errors_get_no_hint(two_sessions, sql):
    mine, ids, other = two_sessions
    with pytest.raises(QueryRefused) as info:
        guard.check(expand(sql, ids, other), live_ids=mine.live_ids())
    assert info.value.code == "sql_parse"
    assert info.value.hint is None


def test_double_quoted_keywords_run(two_sessions):
    mine, ids, _ = two_sessions
    out = mine.query(
        f'WITH "map" AS (SELECT ticker FROM {ids[0]}) SELECT "map".ticker FROM "map"', max_rows=5
    )
    assert out.row_count == 5


@pytest.mark.parametrize(
    ("sql", "word", "start"),
    [
        ('SELECT end FROM (SELECT 1 AS "end") s', "end", 'Parser Error: syntax error at or near "end"'),
        ('SELECT left FROM (SELECT 1 AS "left") s', "left", "Parser Error: syntax error at or near"),
        ("SELECT close FROM {A} AS group", "group", 'Parser Error: syntax error at or near "group"'),
    ],
)
def test_duckdb_refusing_a_keyword_name_gets_a_hint(two_sessions, sql, word, start):
    """sqlglot reads these and DuckDB's parser does not: end is a column of
    the correlation and beta results."""
    mine, ids, other = two_sessions
    with pytest.raises(QueryRefused) as info:
        mine.query(expand(sql, ids, other), max_rows=5)
    assert info.value.code == "sql_error"
    assert info.value.message.startswith(start)
    assert info.value.hint == guard.keyword_hint([word])
    assert mine.query('SELECT r.end FROM (SELECT 1 AS "end") r', max_rows=5).row_count == 1


@pytest.mark.parametrize(
    ("sql", "hint"),
    [
        ("SELECT ticker, sum(volume) + close FROM {A} GROUP BY ALL", "GROUP BY ALL groups by every"),
        ("SELECT ticker, sum(volume) FROM {A}", "ticker is used outside an aggregate"),
        ("SELECT ticker FROM {A} GROUP BY ticker HAVING close > 0", "close is used outside an aggregate"),
        ("SELECT sum(volume) FROM {A} WHERE sum(close) > 0", "WHERE filters rows before they are grouped"),
        ("SELECT avg(sum(volume)) FROM {A}", "Aggregate in a WITH query"),
    ],
)
def test_grouping_mistakes_get_a_hint(two_sessions, sql, hint):
    mine, ids, other = two_sessions
    with pytest.raises(QueryRefused) as info:
        mine.query(expand(sql, ids, other), max_rows=5)
    assert info.value.code == "sql_error"
    assert info.value.message.startswith("Binder Error:")
    assert info.value.hint.startswith(hint)


#: A keyword as a WITH name, a table alias, a column alias and a column.
NAME_PROBES = (
    "WITH x AS (SELECT 1 AS a), {q} AS (SELECT 2 AS b) SELECT x.a, {q}.b FROM x JOIN {q} ON true",
    "SELECT {q}.a FROM r_0123456789 AS {q}",
    "SELECT a AS {q} FROM r_0123456789",
    'SELECT {q} FROM (SELECT 1 AS "{w}") s',
)


def test_every_keyword_name_breaks_unquoted_and_runs_quoted():
    """KEYWORD_NAMES is what sqlglot 30.21 and DuckDB 1.5 refuse as a bare
    name: a version that accepts one of them fails here, and the word goes."""
    con = duckdb.connect()
    con.execute("CREATE TABLE r_0123456789 AS SELECT 1 AS a, 2 AS b")

    def runs(sql: str) -> bool:
        try:
            checked = guard.check(sql, live_ids={"r_0123456789"})
            con.execute(guard.render(checked.ast)).fetchall()
        except (QueryRefused, duckdb.Error):
            return False
        return True

    for w in sorted(guard.KEYWORD_NAMES):
        assert not all(runs(p.format(q=w, w=w)) for p in NAME_PROBES), w
        assert all(runs(p.format(q=f'"{w}"', w=w)) for p in NAME_PROBES), w
    for w in ("order", "table", "list", "struct", "filter", "window", "limit"):
        assert w not in guard.KEYWORD_NAMES
        assert all(runs(p.format(q=w, w=w)) for p in NAME_PROBES), w
