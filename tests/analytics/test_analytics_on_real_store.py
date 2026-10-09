"""Post-merge checks against ml-core's real result store and pipeline
(``marketlens_mcp.testing``). Skipped until the core lane is merged.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pyarrow as pa
import pytest

testing = pytest.importorskip("marketlens_mcp.testing")

from analytics_harness import MINUTE, bars, schema_of, table_of, utc  # noqa: E402

from marketlens_mcp.analytics.common import canonical_schema  # noqa: E402
from marketlens_mcp.analytics.tools import all_specs  # noqa: E402
from marketlens_mcp.analytics.tools.align import SPEC as ALIGN  # noqa: E402
from marketlens_mcp.analytics.tools.beta import SPEC as BETA  # noqa: E402
from marketlens_mcp.analytics.tools.correlation import SPEC as CORRELATION  # noqa: E402
from marketlens_mcp.analytics.tools.drawdown import SPEC as DRAWDOWN  # noqa: E402
from marketlens_mcp.analytics.tools.resample import SPEC as RESAMPLE  # noqa: E402
from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS  # noqa: E402
from marketlens_mcp.analytics.tools.volatility import SPEC as VOLATILITY  # noqa: E402
from marketlens_mcp.plugin_api import ToolError  # noqa: E402
from marketlens_mcp.results.store import StoreLimits, StoreRoot  # noqa: E402
from marketlens_mcp.results.tools import SPECS as RESULTS_SPECS  # noqa: E402
from marketlens_mcp.results_api import InlineResult, ResultMarker, ResultNotFound  # noqa: E402
from marketlens_schema import BUILTIN_MODELS  # noqa: E402
from marketlens_schema.analytics import ANALYTICS_MODELS  # noqa: E402
from marketlens_schema.base import Provenance  # noqa: E402

M0 = utc(2026, 1, 5, 13, 30)


def put_bars(store, rows):
    return store.put(
        table_of(type(rows[0]), rows),
        tool="market_bars",
        model=type(rows[0]).schema_name,
        provenance=Provenance(provider="synthetic", route="GET /v2/stocks/bars", fetched_at=M0),
    ).result_id


def call(spec, ctx, **arguments):
    return asyncio.run(testing.call_tool(spec, ctx, **arguments))


@pytest.fixture
def real(tmp_path):
    store = testing.temp_store(tmp_path)
    return store, testing.make_context(store=store, capabilities=["analytics", "results"])


def test_small_output_is_inline_and_large_output_is_a_marker(real):
    store, ctx = real
    small = put_bars(store, bars("AAPL", [100, 110, 99]))
    out = call(RETURNS, ctx, result_id=small)
    assert isinstance(out, InlineResult) and out.model == "marketlens.ReturnPoint" and out.row_count == 2
    big = put_bars(
        store, bars("AAPL", [100 + (i % 7) for i in range(400)], start=M0, step=MINUTE, timeframe="1min")
    )
    marker = call(RETURNS, ctx, result_id=big)
    assert isinstance(marker, ResultMarker) and marker.row_count == 399
    info = store.info(marker.result_id)
    assert (info.model, info.time_column, info.group_column, info.parents) == (
        "marketlens.ReturnPoint",
        "t",
        "series",
        [big],
    )
    # The stored returns chain into another analytics tool, with ppy from the parent's 1min timeframe.
    vol = call(VOLATILITY, ctx, result_id=marker.result_id, window=5)
    assert isinstance(vol, ResultMarker) and vol.row_count == 395


def test_dynamic_outputs_keep_their_time_column(real):
    store, ctx = real
    left = put_bars(
        store, bars("AAPL", [float(i + 1) for i in range(300)], start=M0, step=MINUTE, timeframe="1min")
    )
    right = put_bars(store, bars("SPY", [1.0, 2.0], start=M0, step=60 * MINUTE, timeframe="1h"))
    aligned = call(ALIGN, ctx, left_result_id=left, right_result_id=right, right_columns=["close"])
    assert isinstance(aligned, ResultMarker) and aligned.model == "marketlens.Aligned"
    assert store.info(aligned.result_id).time_column == "t"
    bars_5 = call(RESAMPLE, ctx, result_id=left, timeframe="5min")
    assert isinstance(bars_5, InlineResult) and bars_5.row_count == 60 and bars_5.model == "marketlens.Bar"
    dd = call(DRAWDOWN, ctx, result_id=aligned.result_id, value_column="close_right")
    assert isinstance(dd, InlineResult) and dd.rows[0]["series"] == "AAPL"


def test_errors_from_the_real_store(real):
    store, ctx = real
    with pytest.raises(ToolError) as e:
        call(RETURNS, ctx, result_id="r_00000000ff")
    assert e.value.code == "result_unknown"


def test_canonical_schema_matches_the_store_arrow_types():
    from marketlens_mcp.results.arrow import arrow_schema

    for model in ANALYTICS_MODELS:
        assert canonical_schema(model).equals(arrow_schema(model))
        assert schema_of(model).equals(arrow_schema(model))


def test_specs_pass_the_server_manifest_validation():
    from marketlens_mcp.manifest import validate_spec
    from marketlens_mcp.plugin_api import BUILTIN_CAPABILITIES

    caps = {c.id for c in BUILTIN_CAPABILITIES}
    for spec in all_specs():
        validate_spec(spec, capabilities=caps, models=BUILTIN_MODELS)


def test_a_stored_query_of_one_ticker_is_a_benchmark_like_its_parent(real):
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    closes = [100 * (1 + 0.01 * ((i * 7) % 5 - 2)) for i in range(30)]
    both = put_bars(store, bars("AAPL", closes) + bars("SPY", [c * 4 + i for i, c in enumerate(closes)]))
    spy = call(query, ctx, sql=f"SELECT * FROM {both} WHERE ticker = 'SPY'", store=True)
    assert isinstance(spy, ResultMarker) and spy.model == "marketlens.QueryRow"
    aapl = call(query, ctx, sql=f"SELECT * FROM {both} WHERE ticker = 'AAPL'", store=True)
    out = call(BETA, ctx, asset_result_id=aapl.result_id, benchmark_result_id=spy.result_id)
    assert isinstance(out, InlineResult)
    (row,) = out.rows
    assert (row["series"], row["benchmark"], row["n_obs"]) == ("AAPL", "SPY", 29)
    assert row["beta"] is not None


def test_the_analytics_act_on_the_units_a_dynamic_tables_fields_name(real):
    """A dynamic table's field metadata is its columns' unit (a datastore_query
    answer labels close USD and implied_vol fraction), and the analytics read
    units: a USD column is a price, so correlation turns it into returns as it
    does market_bars' close; a fraction is a ratio, so returns refuses it. The
    same table unlabelled is correlated as levels."""
    store, ctx = real
    times = [M0 + i * 24 * 60 * MINUTE for i in range(6)]
    closes = {"AAA": [100, 102, 101, 105, 104, 108], "BBB": [50, 50.5, 50.2, 51.5, 51.0, 52.6]}

    def put(labelled: bool) -> str:
        def field(name, kind, unit):
            return pa.field(name, kind, metadata={b"unit": unit} if labelled else None)

        schema = pa.schema(
            [
                pa.field("ticker", pa.string()),
                pa.field("event_time", pa.timestamp("us", tz="UTC")),
                field("close", pa.float64(), b"USD"),
                field("implied_vol", pa.float64(), b"fraction"),
            ]
        )
        rows = [(t, ts, float(c), 0.3) for t, cs in closes.items() for ts, c in zip(times, cs, strict=True)]
        table = pa.Table.from_pylist([dict(zip(schema.names, r, strict=True)) for r in rows], schema=schema)
        return store.put(
            table,
            tool="datastore_query",
            model="marketlens.QueryRow",
            provenance=Provenance(provider="synthetic", route="GET /rows", fetched_at=M0),
            time_column="event_time",
            group_column="ticker",
        ).result_id

    labelled, plain = put(True), put(False)
    assert {c.name: c.unit for c in store.info(labelled).columns}["close"] == "USD"
    as_returns = call(CORRELATION, ctx, result_id=labelled, value_column="close", min_overlap=2)
    as_levels = call(CORRELATION, ctx, result_id=plain, value_column="close", min_overlap=2)
    assert any("simple returns" in n for n in as_returns.notes)
    assert not any("simple returns" in n for n in as_levels.notes)
    with pytest.raises(ToolError) as e:
        call(RETURNS, ctx, result_id=labelled, price_column="implied_vol")
    assert e.value.code == "not_prices" and "fraction" in e.value.message
    assert isinstance(call(RETURNS, ctx, result_id=plain, price_column="implied_vol"), InlineResult)


def test_aligned_and_resampled_columns_keep_their_units(real):
    """The aligned answer (inline or stored) and a resampled query answer keep
    each column's unit, so the next tool still knows a price is a price."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    left = put_bars(store, bars("AAPL", [1.0, 2.0, 3.0], start=M0, step=MINUTE, timeframe="1min"))
    right = put_bars(store, bars("SPY", [5.0, 6.0], start=M0, step=MINUTE, timeframe="1min"))
    inline = call(ALIGN, ctx, left_result_id=left, right_result_id=right, right_columns=["close"])
    assert isinstance(inline, InlineResult)
    units = {c.name: c.unit for c in inline.columns}
    assert (units["close"], units["close_right"], units["matched_t"]) == ("price", "price", "UTC")
    big = put_bars(
        store, bars("AAPL", [float(i + 1) for i in range(300)], start=M0, step=MINUTE, timeframe="1min")
    )
    stored = call(ALIGN, ctx, left_result_id=big, right_result_id=right, right_columns=["close"])
    assert isinstance(stored, ResultMarker)
    assert {c.name: c.unit for c in store.info(stored.result_id).columns}["close_right"] == "price"
    kept = call(query, ctx, sql=f"SELECT t, ticker, close FROM {big}", store=True)
    resampled = call(RESAMPLE, ctx, result_id=kept.result_id, timeframe="5min")
    assert isinstance(resampled, InlineResult)
    assert {c.name: c.unit for c in resampled.columns}["close"] == "price"


def test_store_true_keeps_a_small_output_of_every_analytics_tool(real):
    """analytics_drawdown mode=max came back inline with no result_id and no way
    to keep it: every analytics tool takes store=true."""
    store, ctx = real
    closes = [100 * (1 + 0.01 * ((i * 7) % 5 - 2)) for i in range(25)]
    both = put_bars(store, bars("AAPL", closes) + bars("SPY", [c * 4 + i for i, c in enumerate(closes)]))
    spy = put_bars(store, bars("SPY", [c * 4 + i for i, c in enumerate(closes)]))
    calls = {
        DRAWDOWN: {"result_id": both},
        RETURNS: {"result_id": both},
        VOLATILITY: {"result_id": both, "window": "all"},
        CORRELATION: {"result_id": both},
        BETA: {"asset_result_id": both, "benchmark_result_id": spy},
        RESAMPLE: {"result_id": both, "timeframe": "1w"},
        ALIGN: {"left_result_id": both, "right_result_id": spy, "right_columns": ["close"]},
    }
    assert {spec.name for spec in calls} == {spec.name for spec in all_specs()}
    for spec, arguments in calls.items():
        assert isinstance(call(spec, ctx, **arguments), InlineResult), spec.name
        marker = call(spec, ctx, **arguments, store=True)
        assert isinstance(marker, ResultMarker), spec.name
        assert store.info(marker.result_id).parents[0] == both


def test_a_carried_columns_source_outlives_the_result_that_computed_it(tmp_path):
    """A drawdown column in an aligned result, or in a query of a query, was
    read as returns again once the drawdown result had expired (each result's
    24 hours run from its creation, so a parent goes first) or a result between
    them was dropped: analytics_align, analytics_resample and results_query
    record each carried column's source_model when they create the result,
    and never an aligned result's or query answer's own model."""
    now = [M0]
    store = StoreRoot(tmp_path, StoreLimits(ttl_hours=24), clock=lambda: now[0]).session("test")
    ctx = testing.make_context(store=store, capabilities=["analytics", "results"])
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    closes = [100.0, 102.0, 99.0, 103.0, 101.0, 104.0]
    aapl, spy = put_bars(store, bars("AAPL", closes)), put_bars(store, bars("SPY", closes[::-1]))
    dd = call(DRAWDOWN, ctx, result_id=aapl, mode="series", store=True).result_id
    rets, spy_rets = (call(RETURNS, ctx, result_id=p, store=True).result_id for p in (aapl, spy))
    now[0] += dt.timedelta(hours=12)
    aligned = call(ALIGN, ctx, left_result_id=spy, right_result_id=dd, tolerance="PT0S", store=True)
    middle = call(query, ctx, sql=f"SELECT t, series, drawdown FROM {dd}", store=True).result_id
    nested = call(query, ctx, sql=f"SELECT * FROM {middle}", store=True).result_id
    both = call(ALIGN, ctx, left_result_id=rets, right_result_id=spy_rets, tolerance="PT0S", store=True)
    weekly = call(RESAMPLE, ctx, result_id=aligned.result_id, timeframe="1w", store=True).result_id
    of_aligned = call(query, ctx, sql=f"SELECT * FROM {aligned.result_id}", store=True).result_id
    # Shown with the columns it was recorded for, and only those.
    columns = {c["name"]: c for c in aligned.model_dump(mode="json")["columns"]}
    assert columns["drawdown"]["source_model"] == "marketlens.DrawdownPoint"
    assert "source_model" not in columns["ticker"] and "source_model" not in columns["matched_t"]
    assert {c.name: c.source_model for c in store.info(nested).columns}[
        "drawdown"
    ] == "marketlens.DrawdownPoint"
    # Carried on by a resample or a query of the aligned result; its own matched_t
    # is not recorded as computed by marketlens.Aligned.
    for rid in (weekly, of_aligned):
        recorded = {c.name: (c.unit, c.source_model) for c in store.info(rid).columns}
        assert recorded["drawdown"] == ("fraction", "marketlens.DrawdownPoint")
        assert recorded["close"] == ("price", "marketlens.Bar") and recorded["matched_t"] == ("UTC", None)
    store.drop(middle)
    now[0] += dt.timedelta(hours=13)
    with pytest.raises(ResultNotFound) as gone:
        store.info(dd)
    assert gone.value.reason == "expired"
    for rid in (aligned.result_id, nested, weekly, of_aligned):
        with pytest.raises(ToolError) as e:
            call(VOLATILITY, ctx, result_id=rid, price_column="drawdown", window=2, periods_per_year=252)
        assert (
            e.value.code == "not_prices"
            and "computed by a marketlens.DrawdownPoint result" in e.value.message
        )
    out = call(
        VOLATILITY, ctx, result_id=both.result_id, price_column="ret_right", window=2, periods_per_year=252
    )
    assert any("ret_right was computed by a marketlens.ReturnPoint result" in n for n in out.notes)
