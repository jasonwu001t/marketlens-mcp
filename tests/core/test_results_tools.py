"""The results tools (contract 5.5): results_query, results_describe,
results_sample, results_list, results_drop and results_export."""

from __future__ import annotations

import json
import os
import pathlib
import re

import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pytest
from coresupport import bar_rows, bar_table, news_rows, provenance, run

from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.results import tools as rt
from marketlens_mcp.results.arrow import rows_to_table
from marketlens_mcp.results.store import StoreLimits, StoreRoot
from marketlens_mcp.results_api import InlineResult, ResultMarker
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
    ctx, *_ = setup
    with pytest.raises(ToolError) as info:
        call(ctx, "results_query", sql="SELECT 1", max_rows=201)
    assert info.value.code == "invalid_arguments"


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
