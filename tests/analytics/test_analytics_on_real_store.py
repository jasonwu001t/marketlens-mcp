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
from marketlens_mcp.results_api import InlineResult, ResultMarker, ResultNotFound, RowsOf  # noqa: E402
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


def test_a_stored_point_in_time_result_is_aligned_on_when_its_rows_became_known(real):
    """N2 through the real store and pipeline: a plugin stores a dataset's rows
    with time column event_time and knowledge_time marked role available in its
    field metadata (as datastore_query does); analytics_align then gives each
    bar only a value known by the bar's time, also through a stored query of
    the result, and match_on=time says how many matches were look-ahead."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    days = [M0 + i * 24 * 60 * MINUTE for i in range(250)]
    left = put_bars(store, bars("SPY", [100.0 + i for i in range(250)], start=days[0], step=24 * 60 * MINUTE))
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            pa.field("event_time", ts, metadata={b"unit": b"UTC"}),
            pa.field("knowledge_time", ts, metadata={b"unit": b"UTC", b"role": b"available"}),
            pa.field("series_id", pa.string()),
            pa.field("value", pa.float64(), metadata={b"unit": b"percent"}),
        ]
    )
    # A daily value, known 22:00 UTC the next day (FRED's DGS10 in the store).
    event = [utc(d.year, d.month, d.day) for d in days]
    known = [e + dt.timedelta(hours=46) for e in event]
    table = pa.table([event, known, ["DGS10"] * 250, [4.0 + i / 100 for i in range(250)]], schema=schema)
    right = store.put(
        table,
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=Provenance(provider="plugin", route="GET /rows", fetched_at=M0),
        time_column="event_time",
        group_column="series_id",
    ).result_id
    of_right = call(query, ctx, sql=f"SELECT * FROM {right} WHERE value > 0", store=True).result_id
    for rid in (right, of_right):
        out = call(
            ALIGN, ctx, left_result_id=left, right_result_id=rid, right_columns=["value", "knowledge_time"]
        )
        assert isinstance(out, ResultMarker)
        late = call(
            query,
            ctx,
            sql=f"SELECT count(*) FILTER (WHERE knowledge_time > t) AS late, count(value) AS matched FROM {out.result_id}",
        )
        assert late.rows == [{"late": 0, "matched": 248}]
        assert any(
            n.startswith(f"Point in time: each row of {rid} is matched from when it became usable")
            for n in out.notes
        )
        # The aligned result's knowledge_time is a carried column, not its own clock.
        assert {c.name: c.role for c in store.info(out.result_id).columns}["knowledge_time"] is None
    ahead = call(
        ALIGN, ctx, left_result_id=left, right_result_id=right, right_columns=["value"], match_on="time"
    )
    assert any(
        "250 of 250 matched left rows got a right row known only after their time" in n for n in ahead.notes
    )


def test_an_answer_too_large_to_show_is_stored_whole_and_a_cut_one_says_so_downstream(tmp_path):
    """N5 through the real store: results_query stored an oversized answer cut
    to max_rows, marked complete, so analytics computed on part of the data
    without a word. It is stored whole; one cut at fetch.max_rows is flagged
    truncated, and an analytics output computed from it says so."""
    store = StoreRoot(tmp_path, StoreLimits(fetch_max_rows=600)).session("test")
    ctx = testing.make_context(store=store, capabilities=["analytics", "results"])
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    three = put_bars(
        store,
        bars("SPY", [100.0 + i % 9 for i in range(252)], start=M0)
        + bars("TLT", [90.0 + i % 7 for i in range(252)], start=M0)
        + bars("IEF", [95.0 + i % 5 for i in range(252)], start=M0),
    )
    whole = call(query, ctx, sql=f"SELECT * FROM {three} WHERE ticker <> 'IEF'", max_rows=200)
    assert isinstance(whole, ResultMarker) and whole.row_count == 504
    rets = call(RETURNS, ctx, result_id=whole.result_id, store=True)
    assert rets.row_count == 502 and rets.preview.groups_sample == ["SPY", "TLT"]
    assert rets.provenance.truncated is False
    cut = call(query, ctx, sql=f"SELECT * FROM {three}", store=True)
    assert cut.row_count == 600 and cut.provenance.truncated is True
    on_cut = call(RETURNS, ctx, result_id=cut.result_id, store=True)
    assert on_cut.provenance.truncated is True
    assert on_cut.provenance.truncation_note.startswith(f"Computed from {cut.result_id}, which stopped")
    # A query over the cut answer is incomplete too, and says so to analytics
    # (it said truncated=false, so returns and describe took IEF's first 96
    # days for its year).
    of_cut = call(query, ctx, sql=f"SELECT * FROM {cut.result_id} WHERE ticker = 'IEF'", store=True)
    assert of_cut.row_count == 96 and of_cut.provenance.truncated is True
    assert of_cut.provenance.truncation_note.startswith(f"Computed from {cut.result_id}, which is incomplete")
    rets_of_cut = call(RETURNS, ctx, result_id=of_cut.result_id, price_column="close", store=True)
    assert rets_of_cut.provenance.truncated is True


def test_a_data_models_knowledge_time_is_matched_point_in_time_through_a_query(real):
    """Review of N2: a built-in model's knowledge_time states x-role available,
    so the stored column has the role, and a stored query that turns the date
    into the instant analytics_align needs (the time column changed) keeps it.
    The bars of September 2 and 30 got September's CPI, dated September 1 and
    published October 15, with nothing said; each now gets the month known by
    then."""
    from marketlens_schema.data import EconomicObservation

    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    cpi = [
        EconomicObservation(
            source="fred",
            series_id="CPIAUCSL",
            date=dt.date(2026, m, 1),
            value=320.0 + m,
            knowledge_time=utc(2026, m + 1, 15, 12, 30),
            pit="vintage",
        )
        for m in (7, 8, 9)
    ]
    econ = store.put(
        table_of(EconomicObservation, cpi),
        tool="econ_series",
        model=EconomicObservation.schema_name,
        provenance=Provenance(provider="fred", route="GET /fred/series/observations", fetched_at=M0),
    ).result_id
    assert {c.name: c.role for c in store.info(econ).columns}["knowledge_time"] == "available"
    as_t = call(
        query,
        ctx,
        sql=f"SELECT series_id, CAST(date AS TIMESTAMPTZ) AS t, value, knowledge_time FROM {econ}",
        store=True,
    ).result_id
    assert {c.name: c.role for c in store.info(as_t).columns}["knowledge_time"] == "available"
    left = put_bars(
        store,
        bars("SPY", [100.0] * 3, times=[utc(2026, 9, 2, 20), utc(2026, 9, 30, 20), utc(2026, 10, 16, 20)]),
    )
    out = call(
        ALIGN, ctx, left_result_id=left, right_result_id=as_t, right_columns=["value", "knowledge_time"]
    )
    assert [(r["value"], r["matched_t"][:10]) for r in out.rows] == [
        (327.0, "2026-07-01"),
        (328.0, "2026-08-01"),
        (329.0, "2026-09-01"),
    ]
    assert any(
        n.startswith(f"Point in time: each row of {as_t} is matched from when it became usable")
        for n in out.notes
    )


def test_a_release_schedule_is_matched_from_each_release_not_from_when_it_was_announced(real):
    """Review of N2: ReleaseScheduleEntry's knowledge_time states x-role
    available, and every release of the year is known in advance; matched on
    that alone, the bars of March 20 and May 1 both got the June release. A row
    counts from the later of its time and when it became known, so each bar
    gets the last release at or before it, as before the role."""
    from marketlens_schema.data import ReleaseScheduleEntry

    store, ctx = real
    days = [(1, 13), (2, 11), (3, 11), (4, 10), (5, 12), (6, 10)]
    schedule = [
        ReleaseScheduleEntry(
            release="cpi",
            reference_month=dt.date(2026, m - 1, 1) if m > 1 else dt.date(2025, 12, 1),
            start_date=dt.date(2026, m, 1),
            end_date=dt.date(2026, m, 28),
            scheduled_at=utc(2026, m, d, 13, 30),
            knowledge_time=utc(2025, 12, 1),
        )
        for m, d in days
    ]
    rid = store.put(
        table_of(ReleaseScheduleEntry, schedule),
        tool="bls_schedule",
        model=ReleaseScheduleEntry.schema_name,
        provenance=Provenance(provider="bls", route="GET /schedule", fetched_at=M0),
    ).result_id
    assert {c.name: c.role for c in store.info(rid).columns}["knowledge_time"] == "available"
    left = put_bars(store, bars("SPY", [100.0] * 2, times=[utc(2026, 3, 20, 20), utc(2026, 5, 1, 20)]))
    for match_on in ("available", "time"):
        out = call(
            ALIGN,
            ctx,
            left_result_id=left,
            right_result_id=rid,
            right_columns=["reference_month"],
            match_on=match_on,
        )
        assert [(r["matched_t"][:10], r["reference_month"]) for r in out.rows] == [
            ("2026-03-11", "2026-02-01"),
            ("2026-04-10", "2026-03-01"),
        ], match_on


def test_a_query_joining_bars_to_a_dataset_is_matched_no_later_than_each_bar(real):
    """Review of N2: a stored join of bars with a dataset's rows keeps the
    dataset's knowledge_time beside the bars' own time. Matched on
    knowledge_time alone, every joined row was known before the first bar, so a
    left row got the last bar of the join, dated after it. Round 6: a bar is
    known only at its end, which the join does not show, so knowledge_time
    keeps no role. Round 7: matched from the later of its t and knowledge_time,
    the lower bound left out the bar's end, so a left row got a bar that ended
    after it; the answer records that it was computed from bars without
    showing when its rows became known, and analytics_align refuses it unless
    match_on=time, which counts the look-ahead it can see."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    spy = put_bars(store, bars("SPY", [100.0 + i for i in range(10)], start=utc(2026, 9, 1, 20)))
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            pa.field("event_time", ts, metadata={b"unit": b"UTC"}),
            pa.field("knowledge_time", ts, metadata={b"unit": b"UTC", b"role": b"available"}),
            pa.field("value", pa.float64(), metadata={b"unit": b"percent"}),
        ]
    )
    table = pa.table([[utc(2026, 7, 1)], [utc(2026, 8, 14, 12, 30)], [2.7]], schema=schema)
    macro = store.put(
        table,
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=Provenance(provider="plugin", route="GET /rows", fetched_at=M0),
        time_column="event_time",
    ).result_id
    joined = call(
        query,
        ctx,
        sql=f"SELECT b.t, b.close, m.value, m.knowledge_time FROM {spy} b ASOF JOIN {macro} m ON b.t >= m.event_time",
        store=True,
    ).result_id
    assert {c.name: c.role for c in store.info(joined).columns}["knowledge_time"] is None
    assert store.info(joined).rows_of == RowsOf(model="marketlens.Bar", fetched_at=M0, shown=False)
    left = put_bars(store, bars("QQQ", [50.0] * 2, times=[utc(2026, 9, 3, 12), utc(2026, 9, 7, 12)]))
    with pytest.raises(ToolError) as e:
        call(ALIGN, ctx, left_result_id=left, right_result_id=joined, right_columns=["close", "value"])
    assert e.value.code == "not_point_in_time"
    assert e.value.message == (
        f"analytics_align: {joined} was computed by results_query from marketlens.Bar rows (fetched at "
        "2026-01-05T13:30Z) in a way that shows neither when each of its rows became known nor when the period it "
        "covers ends (a bucket dated at its start, lead() or another window, a join of bars, rows aligned forward, "
        "a knowledge_time left out), so matching on its time column t could pair a left row with values known only "
        "after it. Align onto the rows it was computed from (analytics_resample records when each of its buckets "
        "ends), or pass match_on=time to match on t anyway, look-ahead included."
    )
    out = call(
        ALIGN,
        ctx,
        left_result_id=left,
        right_result_id=joined,
        right_columns=["close", "value"],
        match_on="time",
    )
    assert [(r["matched_t"][:13], r["close_right"]) for r in out.rows] == [
        ("2026-09-02T20", 101.0),
        ("2026-09-06T20", 105.0),
    ]
    assert not any(n.startswith("Point in time") for n in out.notes)


def test_bars_filtered_to_one_series_are_matched_as_the_bars_are(real):
    """Review of N2, round 6: analytics_align refuses a right result of
    several series and advises keeping one with results_query; that answer was
    then said not shown point in time, for a full-sample average or lead() it
    never had, while the bars themselves were matched without a word, from
    their start. The answer passes the bars through, so it is matched as they
    are, from each bar's end, point in time, with their timeframe or without
    it (all of them 1d); bars adjusted after their time, filtered so, say why
    they are not."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    days = [utc(2026, 1, d, 5) for d in (5, 6, 7)]
    rows = bars("SPY", [10.0, 11.0, 12.0], times=days) + bars("QQQ", [20.0, 21.0, 22.0], times=days)
    hours = [utc(2026, 1, 6, 14), utc(2026, 1, 7, 15)]
    left = put_bars(store, bars("SPY", [1.0] * 2, times=hours, timeframe="1h"))

    def fetched(request):
        return store.put(
            table_of(type(rows[0]), rows),
            tool="market_bars",
            model=type(rows[0]).schema_name,
            provenance=Provenance(
                provider="synthetic", route="GET /v2/stocks/bars", fetched_at=M0, request=request
            ),
        ).result_id

    raw, adjusted = fetched({"adjustment": "raw"}), fetched({"adjustment": "all"})
    with pytest.raises(ToolError) as e:
        call(ALIGN, ctx, left_result_id=left, right_result_id=raw, right_columns=["close"])
    assert "keep one series with results_query first" in e.value.message
    for right, columns in ((raw, "*"), (raw, "t, close"), (adjusted, "*")):
        spy = call(
            query, ctx, sql=f"SELECT {columns} FROM {right} WHERE ticker = 'SPY'", store=True
        ).result_id
        out = call(ALIGN, ctx, left_result_id=left, right_result_id=spy, right_columns=["close"])
        assert [(r["matched_t"][:10], r["close_right"]) for r in out.rows] == [
            ("2026-01-05", 10.0),
            ("2026-01-06", 11.0),
        ]
        assert not any("computed by results_query" in n for n in out.notes)
        said = [n for n in out.notes if "point in time" in n.lower()]
        if right == raw:
            assert said == [
                f"Point in time: each bar of {spy} is matched from its end, t plus its timeframe, when its close "
                "became known: each left row gets the bar with the latest t that ended at or before its time. "
                "match_on=time matches on t, a bar's start, instead."
            ]
        else:
            assert len(said) == 1 and said[0].startswith(
                f"Not shown point in time: {spy} holds marketlens.Bar rows adjusted for corporate actions "
                "(adjustment=all) as fetched at 2026-01-05T13:30Z"
            )


def test_a_value_computed_from_rows_known_later_is_not_matched_as_point_in_time(real):
    """Review of N2, rounds 3 and 4: a stored query passing knowledge_time
    through kept its role beside a full-sample z-score, so a bar of January 7
    got -49.5, which averages in the 100.0s published from January 10, under a
    note that none was published after it; the z-score joined back to the
    dataset's knowledge_time got the role again, and the same note. Neither
    keeps the role, so analytics_align says the match is not shown point in
    time, and matches from the later of each row's time and its knowledge_time
    all the same (match_on=time counts the look-ahead that shows, at least); an
    answer that dropped the clock is matched on its time column, and says so.
    Round 8: rows computed from others (the z-score, and the join reading it)
    show not when they became known, so analytics_align refuses them unless
    match_on=time, as it does a bucket of bars."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            pa.field("event_time", ts),
            pa.field("knowledge_time", ts, metadata={b"role": b"available"}),
            pa.field("value", pa.float64()),
        ]
    )
    days = [utc(2026, 1, d) for d in range(5, 15)]
    known = [d + dt.timedelta(hours=12) for d in days]
    series = store.put(
        pa.table([days, known, [1.0] * 5 + [100.0] * 5], schema=schema),
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=Provenance(provider="plugin", route="GET /rows", fetched_at=M0),
        time_column="event_time",
    ).result_id
    z = call(
        query,
        ctx,
        sql=f"SELECT event_time, knowledge_time, value - avg(value) OVER () AS value FROM {series}",
        store=True,
    ).result_id
    back = call(
        query,
        ctx,
        sql=f"SELECT z.event_time, s.knowledge_time, z.value FROM {series} s JOIN {z} z ON z.event_time = s.event_time",
        store=True,
    ).result_id
    left = put_bars(store, bars("SPY", [100.0] * 2, times=[utc(2026, 1, 7, 6), utc(2026, 1, 7, 20)]))
    for rid in (z, back):
        assert {c.name: c.role for c in store.info(rid).columns}["knowledge_time"] is None
        assert store.info(rid).rows_of == RowsOf(model="marketlens.QueryRow", fetched_at=M0, shown=False)
        with pytest.raises(ToolError) as e:
            call(ALIGN, ctx, left_result_id=left, right_result_id=rid, right_columns=["value"])
        assert e.value.code == "not_point_in_time"
        ahead = call(
            ALIGN, ctx, left_result_id=left, right_result_id=rid, right_columns=["value"], match_on="time"
        )
        assert [r["matched_t"][:10] for r in ahead.rows] == ["2026-01-07"] * 2
        assert any(
            "at least 1 of 2 matched left rows got a right row with knowledge_time after their time (look-ahead)"
            in n
            for n in ahead.notes
        )
    dropped = call(query, ctx, sql=f"SELECT event_time, value FROM {series}", store=True).result_id
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=dropped, right_columns=["value"])
    assert [r["value"] for r in out.rows] == [1.0, 1.0]
    assert (
        f"Not shown point in time: {dropped} was computed by results_query with no column of role available (when "
        "each row became known), so its rows can hold values known after their time (a full-sample average, lead(), "
        "a bucket dated at its start); its rows were matched on the time column event_time alone, which can pair a "
        f"left row with a value published after it. It was computed from {series}, whose knowledge_time has that "
        "role: keep knowledge_time in a query answer (passed through, or its max()) to match point in time."
    ) in out.notes


def test_an_analytics_output_carries_no_role_from_its_model(real):
    """Review of N2, round 4: analytics_resample keeps its input's model, and
    a built-in data model states knowledge_time's role, so a week's mean of
    earnings figures, dated at the week's start beside its last row's
    knowledge time, kept the role, though the mean took in a figure revised
    on January 16: analytics_align called a bar of January 12 getting it point
    in time. A result computed from others has the role only where the tool
    shows it (a query answer's), so the match is not called point in time.
    Round 8: the bar still got the mean, as the bucket carried its last row's
    knowledge time; it carries the latest of its rows', so the mean is matched
    only from when the revised figure was known."""
    from marketlens_schema.data import EarningsFigure

    store, ctx = real
    figures = [
        EarningsFigure(
            ticker="AAPL",
            cik="320193",
            accession_number=f"0000320193-26-0000{d:02d}",
            accepted_at=utc(2026, 1, d, 21),
            exhibit="EX-99.1",
            exhibit_url="https://example.com/ex99.htm",
            metric="eps_diluted_gaap",
            value=float(d),
            unit="USD/share",
            basis="diluted",
            source_text="EPS",
            parse_rule="table",
            knowledge_time=utc(2026, 1, 16, 21) if d == 6 else utc(2026, 1, d, 21),
        )
        for d in range(5, 12)
    ]
    rid = store.put(
        table_of(EarningsFigure, figures),
        tool="earnings_figures",
        model=EarningsFigure.schema_name,
        provenance=Provenance(provider="sec", route="GET /figures", fetched_at=M0),
    ).result_id
    assert {c.name: c.role for c in store.info(rid).columns}["knowledge_time"] == "available"
    week = call(RESAMPLE, ctx, result_id=rid, timeframe="1w", agg="mean", store=True).result_id
    assert {c.name: c.role for c in store.info(week).columns}["knowledge_time"] is None
    left = put_bars(store, bars("SPY", [100.0] * 2, times=[utc(2026, 1, 12, 20), utc(2026, 1, 16, 21)]))
    out = call(
        ALIGN, ctx, left_result_id=left, right_result_id=week, right_columns=["value", "knowledge_time"]
    )
    assert [(r["value"], r["knowledge_time"]) for r in out.rows] == [
        (None, None),
        (8.0, "2026-01-16T21:00:00Z"),
    ]
    assert not any(n.startswith("Point in time") for n in out.notes)
    assert any(
        n.startswith(
            f"Not shown point in time: knowledge_time of {week} is named as when each row became known"
        )
        and "(analytics_resample gives its outputs none)" in n
        for n in out.notes
    )


def test_an_analytics_output_of_bars_is_matched_from_the_end_of_the_period_it_covers(real):
    """Review of round 6 (round 7): an analytics output of bars was matched on
    its t, the start of the bar it ends on, by default, under only a note:
    returns of SPY's daily bars gave the hourly bar of 2026-10-08T13:00Z
    (09:00 New York, before the open) that day's return, 773.93 / 777.22 - 1,
    known only at the close. The output records the bars its rows come from
    (rows_of: a return is known when its later bar ends), so analytics_align
    matches each row from the end of the period it covers, also after the bars
    are gone; match_on=time counts the look-ahead."""
    store, ctx = real
    days = [utc(2026, 10, d, 4) for d in (6, 7, 8)]
    spy = put_bars(store, bars("SPY", [770.0, 777.22, 773.93], times=days))
    rets = call(RETURNS, ctx, result_id=spy, store=True).result_id
    assert store.info(rets).rows_of == RowsOf(model="marketlens.Bar", fetched_at=M0, span="1d")
    store.drop(spy)
    hours = [utc(2026, 10, 8, 13), utc(2026, 10, 9, 4)]
    left = put_bars(store, bars("SPY", [1.0, 1.0], times=hours, timeframe="1h"))
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=rets, right_columns=["ret"])
    assert [(r["matched_t"][:13], round(r["ret"], 6)) for r in out.rows] == [
        ("2026-10-07T04", round(777.22 / 770.0 - 1, 6)),
        ("2026-10-08T04", round(773.93 / 777.22 - 1, 6)),
    ]
    assert (
        f"Point in time: each row of {rets} is matched from the end of the period it covers, t plus 1d, when the "
        "marketlens.Bar rows it was computed from had all become known: each left row gets the row with the latest "
        "t whose period ended at or before its time. match_on=time matches on t, the period's start, instead."
    ) in out.notes
    assert not any(n.startswith("Not shown") for n in out.notes)
    ahead = call(
        ALIGN, ctx, left_result_id=left, right_result_id=rets, right_columns=["ret"], match_on="time"
    )
    assert [r["matched_t"][:13] for r in ahead.rows] == ["2026-10-08T04"] * 2
    assert (
        f"match_on=time: matched on the time column t of {rets}, each period's start, not the end of the period it "
        "covers (t plus 1d), when the marketlens.Bar rows it was computed from had all become known: 1 of 2 matched "
        "left rows got a row whose period ended after their time (look-ahead). match_on=available (the default) "
        "matches each row from its end."
    ) in ahead.notes
    # A filter of the returns passes their rows through, and records what they do.
    one = call(
        next(s for s in RESULTS_SPECS if s.name == "results_query"),
        ctx,
        sql=f"SELECT t, series, ret FROM {rets} WHERE ret > -1",
        store=True,
    ).result_id
    assert store.info(one).rows_of == store.info(rets).rows_of
    again = call(ALIGN, ctx, left_result_id=left, right_result_id=one, right_columns=["ret"])
    assert [r["matched_t"][:13] for r in again.rows] == ["2026-10-07T04", "2026-10-08T04"]


def test_a_bucket_of_bars_is_matched_from_its_end_or_its_last_bars(real):
    """Review of round 6 (round 7): analytics_resample of SPY's 1h bars to 1d
    gave the hourly bar of 14:00 UTC the close of that day's 20:00 hour, and
    daily bars resampled to 1w gave Tuesday 2026-01-06 the Friday close of its
    week: a bucket was matched on its start. A bucket is matched from its end;
    a UTC month of New York day bars from the end of its last bar, 4 hours on
    in summer (lag_seconds), and returns sampled to a period alike."""
    store, ctx = real
    hours = [utc(2026, 10, d, h) for d in (7, 8) for h in range(13, 21)]
    spy = put_bars(store, bars("SPY", [700.0 + i for i in range(16)], times=hours, timeframe="1h"))
    daily = call(RESAMPLE, ctx, result_id=spy, timeframe="1d", store=True).result_id
    assert store.info(daily).rows_of == RowsOf(model="marketlens.Bar", fetched_at=M0, span="1d")
    left = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 10, 8, 14)], timeframe="1min"))
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=daily, right_columns=["close"])
    assert [(r["matched_t"][:10], r["close_right"]) for r in out.rows] == [("2026-10-07", 707.0)]
    assert (
        f"Point in time: each bar of {daily} is matched from its end, t plus its timeframe, when its close became "
        "known: each left row gets the bar with the latest t that ended at or before its time. match_on=time "
        "matches on t, a bar's start, instead."
    ) in out.notes
    weeks = [utc(2025, 12, d, 5) for d in (29, 30, 31)] + [utc(2026, 1, d, 5) for d in (2, 5, 6, 7, 8, 9)]
    days = put_bars(
        store, bars("SPY", [96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0], times=weeks)
    )
    weekly = call(RESAMPLE, ctx, result_id=days, timeframe="1w", store=True).result_id
    left = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 1, 6, 15)], timeframe="1h"))
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=weekly, right_columns=["close"])
    assert [(r["matched_t"][:10], r["close_right"]) for r in out.rows] == [("2025-12-29", 99.0)]
    # Day bars of New York in UTC months: September's last ends 2026-10-01T04:00Z.
    month_days = [utc(2026, 8, 31, 4), utc(2026, 9, 29, 4), utc(2026, 9, 30, 4), utc(2026, 10, 1, 4)]
    nyc = put_bars(store, bars("SPY", [10.0, 11.0, 12.0, 13.0], times=month_days))
    monthly = call(RESAMPLE, ctx, result_id=nyc, timeframe="1mo", store=True).result_id
    assert store.info(monthly).rows_of == RowsOf(
        model="marketlens.Bar", fetched_at=M0, span="1mo", lag_seconds=4 * 3600
    )
    left = put_bars(
        store, bars("SPY", [1.0] * 2, times=[utc(2026, 10, 1, 2), utc(2026, 10, 1, 4)], timeframe="1h")
    )
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=monthly, right_columns=["close"])
    assert [(r["matched_t"][:10], r["close_right"]) for r in out.rows] == [
        ("2026-08-01", 10.0),
        ("2026-09-01", 12.0),
    ]
    assert (
        f"Point in time: each row of {monthly} is matched from the end of the period it covers, t plus its "
        "timeframe and 4 hours more, as late as one of the rows in it ends, when the marketlens.Bar rows it was "
        "computed from had all become known: each left row gets the row with the latest t whose period ended at "
        "or before its time. match_on=time matches on t, the period's start, instead."
    ) in out.notes
    sampled = call(RETURNS, ctx, result_id=nyc, period="1d", store=True).result_id
    assert store.info(sampled).rows_of == RowsOf(
        model="marketlens.Bar", fetched_at=M0, span="1d", lag_seconds=4 * 3600
    )
    late = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 10, 1, 2)], timeframe="1h"))
    out = call(ALIGN, ctx, left_result_id=late, right_result_id=sampled, right_columns=["ret"])
    assert [r["matched_t"][:10] for r in out.rows] == ["2026-09-29"]  # Sep 30's close ends at 04:00Z


def test_every_analytics_output_dated_by_bars_records_them(real):
    """Round 7: volatility, a drawdown series and a rolling beta are known
    when the last bar they cover is, as are aligned rows with bars on the left
    matched point in time; a ratio of adjusted bars is not adjusted (a later
    split leaves it as it was), a drawdown's price levels are. An output with
    no time (a summary) records nothing; aligned rows matched forward show not
    when they became known, and are refused."""
    store, ctx = real
    times = [utc(2026, 1, 5, 5) + i * dt.timedelta(days=1) for i in range(8)]
    adjusted = store.put(
        table_of(type(bars("SPY", [1.0])[0]), bars("SPY", [100.0 + (i % 3) for i in range(8)], times=times)),
        tool="market_bars",
        model="marketlens.Bar",
        provenance=Provenance(
            provider="synthetic", route="GET /v2/stocks/bars", fetched_at=M0, request={"adjustment": "all"}
        ),
    ).result_id
    qqq = put_bars(store, bars("QQQ", [50.0 + (i % 4) for i in range(8)], times=times))
    ratio = RowsOf(model="marketlens.Bar", fetched_at=M0, span="1d")
    vol = call(VOLATILITY, ctx, result_id=adjusted, window=3, periods_per_year=252, store=True).result_id
    assert store.info(vol).rows_of == ratio
    dd = call(DRAWDOWN, ctx, result_id=adjusted, mode="series", store=True).result_id
    assert store.info(dd).rows_of == ratio.model_copy(update={"adjustment": "all"})
    assert store.info(call(DRAWDOWN, ctx, result_id=adjusted, store=True).result_id).rows_of is None
    beta = call(BETA, ctx, result_id=qqq, benchmark_result_id=adjusted, window=3, store=True).result_id
    assert store.info(beta).rows_of == ratio
    hourly = put_bars(store, bars("SPY", [1.0] * 3, times=[t + dt.timedelta(hours=10) for t in times[:3]]))
    aligned = call(
        ALIGN, ctx, left_result_id=qqq, right_result_id=hourly, right_columns=["close"], store=True
    ).result_id
    assert store.info(aligned).rows_of == ratio
    forward = call(
        ALIGN,
        ctx,
        left_result_id=qqq,
        right_result_id=hourly,
        right_columns=["close"],
        direction="forward",
        store=True,
    ).result_id
    assert store.info(forward).rows_of == ratio.model_copy(update={"span": None, "shown": False})
    with pytest.raises(ToolError) as e:
        call(ALIGN, ctx, left_result_id=hourly, right_result_id=forward, right_columns=["close"])
    assert e.value.code == "not_point_in_time" and "pass match_on=time" in e.value.message


def test_bars_bucketed_by_a_query_are_refused_unless_match_on_time(real):
    """Review of round 6 (round 7): a results_query time_bucket of bars, dated
    at the bucket's start, was matched on that start by default (a 14:00 bar
    got that day's 20:00 close) with only a note: nothing shows when its rows
    became known. The answer records the bars it was computed from, marked not
    shown, and analytics_align refuses it, naming match_on=time as the way to
    match on its time anyway; the refusal holds after the bars are gone."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    hours = [utc(2026, 10, d, h) for d in (7, 8) for h in range(13, 21)]
    spy = put_bars(store, bars("SPY", [700.0 + i for i in range(16)], times=hours, timeframe="1h"))
    days = call(
        query,
        ctx,
        sql=f"SELECT time_bucket(INTERVAL 1 DAY, t) AS t, ticker, timeframe, last(close ORDER BY t) AS close "
        f"FROM {spy} GROUP BY ALL",
        store=True,
    ).result_id
    store.drop(spy)
    left = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 10, 8, 14)], timeframe="1min"))
    with pytest.raises(ToolError) as e:
        call(ALIGN, ctx, left_result_id=left, right_result_id=days, right_columns=["close"])
    assert e.value.code == "not_point_in_time"
    assert "pass match_on=time to match on t anyway, look-ahead included" in e.value.message
    out = call(
        ALIGN, ctx, left_result_id=left, right_result_id=days, right_columns=["close"], match_on="time"
    )
    assert [r["close_right"] for r in out.rows] == [715.0]
    assert any(
        n.startswith(f"Not shown point in time: {days} was computed by results_query") for n in out.notes
    )


def test_a_snapshot_is_known_at_its_fetch(real):
    """Review of round 6 (round 7): options_chain rows were taken as known at
    t, the newest of their parts, though they carry open interest published
    the next morning and greeks as fetched: a contract's Friday 20:00Z row,
    fetched on Monday, gave a Friday 20:30Z bar Monday's open interest. A
    snapshot is matched from its fetch; a query joining it keeps no role."""
    from marketlens_schema.market import OptionSnapshot

    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    fetched = utc(2026, 10, 12, 12)
    snap = OptionSnapshot(
        occ_symbol="SPY261120C00700000",
        underlying="SPY",
        expiration_date=dt.date(2026, 11, 20),
        strike=700.0,
        option_type="call",
        t=utc(2026, 10, 9, 20),
        open_interest=12345,
        open_interest_date=dt.date(2026, 10, 9),
        implied_volatility=0.2,
    )
    chain = store.put(
        table_of(OptionSnapshot, [snap]),
        tool="options_chain",
        model=OptionSnapshot.schema_name,
        provenance=Provenance(
            provider="synthetic", route="GET /v1beta1/options/snapshots", fetched_at=fetched
        ),
    ).result_id
    left = put_bars(
        store,
        bars("SPY", [1.0] * 2, times=[utc(2026, 10, 9, 20, 30), utc(2026, 10, 12, 13)], timeframe="1min"),
    )
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=chain, right_columns=["open_interest"])
    assert [r["open_interest"] for r in out.rows] == [None, 12345]
    assert (
        f"Point in time: each row of {chain} is matched from its fetch at 2026-10-12T12:00Z, when the values it "
        "holds were current (a snapshot's open interest, greeks and session still trading are as of its fetch): "
        "each left row gets the row with the latest t fetched at or before its time. match_on=time matches on t "
        "instead."
    ) in out.notes
    ahead = call(
        ALIGN,
        ctx,
        left_result_id=left,
        right_result_id=chain,
        right_columns=["open_interest"],
        match_on="time",
    )
    assert [r["open_interest"] for r in ahead.rows] == [12345, 12345]
    assert any(
        "1 of 2 matched left rows got a row fetched after their time (look-ahead)" in n for n in ahead.notes
    )
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            pa.field("event_time", ts),
            pa.field("knowledge_time", ts, metadata={b"role": b"available"}),
            pa.field("value", pa.float64()),
        ]
    )
    rates = store.put(
        pa.table([[utc(2026, 10, 9)], [utc(2026, 10, 9, 21)], [4.1]], schema=schema),
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=Provenance(provider="plugin", route="GET /rows", fetched_at=M0),
        time_column="event_time",
    ).result_id
    joined = call(
        query,
        ctx,
        sql=f"SELECT s.t, greatest(r.knowledge_time, s.t) AS knowledge_time, r.value, s.open_interest "
        f"FROM {chain} s ASOF JOIN {rates} r ON s.t >= r.event_time",
        store=True,
    )
    assert {c.name: c.role for c in store.info(joined.result_id).columns}["knowledge_time"] is None
    assert any(
        f"it reads {chain}, which holds marketlens.OptionSnapshot rows as fetched at 2026-10-12T12:00Z, known only "
        "then" in n
        for n in joined.notes
    )


def _dataset(store, values, *, start, step=dt.timedelta(days=1), known_after=None):
    """A dataset's rows as a plugin answer stores them: event_time and value,
    and a knowledge_time with role available ``known_after`` each event_time."""
    ts = pa.timestamp("us", tz="UTC")
    times = [start + i * step for i in range(len(values))]
    fields, columns = [pa.field("event_time", ts)], [times]
    if known_after is not None:
        fields.append(pa.field("knowledge_time", ts, metadata={b"role": b"available"}))
        columns.append([t + known_after for t in times])
    fields.append(pa.field("value", pa.float64()))
    columns.append(list(values))
    return store.put(
        pa.table(columns, schema=pa.schema(fields)),
        tool="datastore_query",
        model="marketlens.QueryRow",
        provenance=Provenance(provider="plugin", route="GET /rows", fetched_at=M0),
        time_column="event_time",
    ).result_id


def test_a_query_bucket_of_trades_is_refused_unless_match_on_time(real):
    """Review of round 7 (round 8): a results_query time_bucket of trades, a
    day's last price dated at the day's start, was matched on that start by
    default: a 14:00Z bar got 713.0, the 20:00Z trade, with only a note.
    Trades are known at their time, but a bucket dated at its start is not:
    the answer records them marked not shown and analytics_align refuses it
    unless match_on=time; a bucket dated at its last trade, max(t), is matched
    on that time."""
    from marketlens_schema.market import Trade

    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    times = [utc(2026, 10, 8, 13, 30) + i * dt.timedelta(minutes=30) for i in range(14)]
    trades = store.put(
        table_of(
            Trade,
            [
                Trade(ticker="SPY", asset_class="us_equity", t=t, price=700.0 + i, size=1.0)
                for i, t in enumerate(times)
            ],
        ),
        tool="market_trades",
        model=Trade.schema_name,
        provenance=Provenance(provider="synthetic", route="GET /v2/stocks/trades", fetched_at=M0),
    ).result_id
    day = call(
        query,
        ctx,
        sql=f"SELECT time_bucket(INTERVAL 1 DAY, t) AS t, ticker, last(price ORDER BY t) AS close FROM {trades} "
        "GROUP BY ALL",
        store=True,
    ).result_id
    assert store.info(day).rows_of == RowsOf(model="marketlens.Trade", fetched_at=M0, shown=False)
    left = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 10, 8, 14)], timeframe="1min"))
    with pytest.raises(ToolError) as e:
        call(ALIGN, ctx, left_result_id=left, right_result_id=day, right_columns=["close"])
    assert (
        e.value.code == "not_point_in_time"
        and f"{day} was computed by results_query from marketlens.Trade" in (e.value.message)
    )
    ahead = call(
        ALIGN, ctx, left_result_id=left, right_result_id=day, right_columns=["close"], match_on="time"
    )
    assert [(r["matched_t"], r["close_right"]) for r in ahead.rows] == [("2026-10-08T00:00:00Z", 713.0)]
    last = call(
        query,
        ctx,
        sql=f"SELECT max(t) AS t, ticker, last(price ORDER BY t) AS close FROM {trades} "
        "GROUP BY ticker, time_bucket(INTERVAL 1 HOUR, t)",
        store=True,
    ).result_id
    assert store.info(last).rows_of == RowsOf(model="marketlens.Trade", fetched_at=M0)
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=last, right_columns=["close"])
    assert [(r["matched_t"], r["close_right"]) for r in out.rows] == [("2026-10-08T13:30:00Z", 700.0)]


def test_a_bucket_of_a_datasets_rows_is_matched_from_its_end(real):
    """Review of round 7 (round 8): analytics_resample of a dataset's rows
    with no knowledge_time (research_macro_series) to 1w gave a bar of
    Tuesday 2026-09-08 the value of Sunday 09-13, the last of the week dated
    09-07, with only a note: the general rule held only for buckets of market
    data. Any bucket records its span, so analytics_align matches it from its
    end, saying the rows themselves are not shown point in time; a filter of
    it, and returns sampled to a period, are matched so too."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    daily = _dataset(store, [100.0 + i * i for i in range(14)], start=utc(2026, 9, 1))
    week = call(RESAMPLE, ctx, result_id=daily, timeframe="1w", store=True).result_id
    assert store.info(week).rows_of == RowsOf(model="marketlens.QueryRow", fetched_at=M0, span="1w")
    left = put_bars(
        store, bars("SPY", [1.0] * 2, times=[utc(2026, 9, 8, 12), utc(2026, 9, 14, 12)], timeframe="1min")
    )
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=week, right_columns=["value"])
    assert [(r["matched_t"][:10], r["value"]) for r in out.rows] == [
        ("2026-08-31", 125.0),
        ("2026-09-07", 244.0),
    ]
    assert (
        f"Not shown point in time: {week} was computed by analytics_resample from marketlens.QueryRow rows with no "
        "column of role available (when each became known), dated by the time each describes, which can be before "
        "it was published. Each row was matched all the same from the end of the period it covers, event_time plus "
        "1w, the safe default, which keeps out the rows whose period ended after the left time. match_on=time "
        "matches on event_time, the period's start, instead."
    ) in out.notes
    ahead = call(
        ALIGN, ctx, left_result_id=left, right_result_id=week, right_columns=["value"], match_on="time"
    )
    assert [r["value"] for r in ahead.rows] == [244.0, 269.0]
    store.drop(daily)
    kept = call(query, ctx, sql=f"SELECT * FROM {week} WHERE value > 0", store=True).result_id
    assert store.info(kept).rows_of == store.info(week).rows_of
    again = call(ALIGN, ctx, left_result_id=left, right_result_id=kept, right_columns=["value"])
    assert [r["value"] for r in again.rows] == [125.0, 244.0]
    daily = _dataset(store, [100.0 + i * i for i in range(14)], start=utc(2026, 9, 1))
    sampled = call(RETURNS, ctx, result_id=daily, price_column="value", period="1w", store=True).result_id
    assert store.info(sampled).rows_of == RowsOf(model="marketlens.QueryRow", fetched_at=M0, span="1w")
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=sampled, right_columns=["ret"])
    assert [r["matched_t"] for r in out.rows] == [None, "2026-09-07T00:00:00Z"]


def test_an_analytics_output_leaving_out_its_inputs_clock_is_refused_unless_match_on_time(real):
    """Review of round 7 (round 8): analytics_returns of a dataset's values,
    each known 46 hours after its event_time, left knowledge_time out, so a bar
    of 2026-09-10T12:00Z got the return of 09-10, known only on 09-11 at
    22:00, matched on event_time with only a note. An output that leaves out
    the clock its input shows when its rows became known by records the input
    marked not shown, and analytics_align refuses it unless match_on=time."""
    store, ctx = real
    clocked = _dataset(
        store, [100.0 + i for i in range(12)], start=utc(2026, 9, 1), known_after=dt.timedelta(hours=46)
    )
    rets = call(RETURNS, ctx, result_id=clocked, price_column="value", store=True).result_id
    assert store.info(rets).rows_of == RowsOf(model="marketlens.QueryRow", fetched_at=M0, shown=False)
    vol = call(
        VOLATILITY, ctx, result_id=clocked, price_column="value", window=3, periods_per_year=252, store=True
    )
    assert store.info(vol.result_id).rows_of == RowsOf(
        model="marketlens.QueryRow", fetched_at=M0, shown=False
    )
    left = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 9, 10, 12)], timeframe="1min"))
    with pytest.raises(ToolError) as e:
        call(ALIGN, ctx, left_result_id=left, right_result_id=rets, right_columns=["ret"])
    assert e.value.code == "not_point_in_time"
    ahead = call(
        ALIGN, ctx, left_result_id=left, right_result_id=rets, right_columns=["ret"], match_on="time"
    )
    assert [r["matched_t"] for r in ahead.rows] == ["2026-09-10T00:00:00Z"]
    # A resample carries the clock, the latest of each bucket's: matched from it.
    week = call(RESAMPLE, ctx, result_id=clocked, timeframe="1w", store=True).result_id
    assert store.info(week).rows_of is None
    out = call(
        ALIGN, ctx, left_result_id=left, right_result_id=week, right_columns=["value", "knowledge_time"]
    )
    assert [(r["value"], r["knowledge_time"]) for r in out.rows] == [(105.0, "2026-09-07T22:00:00Z")]


def test_aligned_rows_not_matched_point_in_time_are_refused_whatever_the_left(real):
    """Review of round 7 (round 8): aligned rows were marked not shown only
    when their left side held market data, so a dataset's rows aligned with
    bars on time or forward were not, and aligning onto them gave a 14:30Z bar
    the 14:00-15:00 bar's close, 709, with a note calling it a lower bound.
    Rows matched forward, on time or onto a right result that shows not when
    its rows became known are marked not shown whatever the left, so aligning
    onto them is refused unless match_on=time; so are bars aligned onto a
    clock that lost its role, which a variable named twice let through as
    point in time."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    dataset = _dataset(store, [1.0], start=utc(2026, 10, 8, 14), known_after=dt.timedelta(0))
    hours = [utc(2026, 10, 8, h) for h in range(13, 17)]
    spy = put_bars(store, bars("SPY", [707.0, 709.0, 711.0, 713.0], times=hours, timeframe="1h"))
    minute = put_bars(store, bars("SPY", [1.0], times=[utc(2026, 10, 8, 14, 30)], timeframe="1min"))
    for how in ({"match_on": "time"}, {"direction": "forward"}):
        aligned = call(
            ALIGN,
            ctx,
            left_result_id=dataset,
            right_result_id=spy,
            right_columns=["close"],
            store=True,
            **how,
        ).result_id
        assert store.info(aligned).rows_of == RowsOf(model="marketlens.Bar", fetched_at=M0, shown=False), how
        with pytest.raises(ToolError) as e:
            call(ALIGN, ctx, left_result_id=minute, right_result_id=aligned, right_columns=["close"])
        assert e.value.code == "not_point_in_time", how
    # Matched point in time, backward from each bar's end: known when the left row is.
    settled = call(
        ALIGN, ctx, left_result_id=dataset, right_result_id=spy, right_columns=["close"], store=True
    )
    assert store.info(settled.result_id).rows_of is None
    # Bars aligned onto a clock that lost its role (a lower bound) show not when their rows became known.
    lower = call(
        query,
        ctx,
        sql=f"SELECT event_time, time_bucket(INTERVAL 1 DAY, knowledge_time) AS knowledge_time, value FROM {dataset}",
        store=True,
    ).result_id
    assert store.info(lower).rows_of is None
    onto = call(ALIGN, ctx, left_result_id=spy, right_result_id=lower, right_columns=["value"], store=True)
    assert store.info(onto.result_id).rows_of == RowsOf(model="marketlens.Bar", fetched_at=M0, shown=False)


def test_a_result_keyed_by_a_naive_timestamp_is_stored_and_aligned(real):
    """Storing a result whose time column is a naive TIMESTAMP failed the
    marker's validation (its time span had no zone), so analytics_align of
    such a result failed as internal_error, stored or too large to show."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    left = put_bars(
        store, bars("AAPL", [float(i + 1) for i in range(300)], start=M0, step=MINUTE, timeframe="1min")
    )
    naive = call(query, ctx, sql=f"SELECT t::TIMESTAMP AS ts, close FROM {left}", store=True).result_id
    info = store.info(naive)
    assert [c.type for c in info.columns][:1] == ["TIMESTAMP"]
    for keep in (False, True):
        aligned = call(
            ALIGN, ctx, left_result_id=naive, right_result_id=left, right_columns=["close"], store=keep
        )
        assert isinstance(aligned, ResultMarker) and aligned.row_count == 300
        assert aligned.preview.time_column == "ts" and aligned.preview.time_span_start is None


def test_a_date_indexed_data_model_is_a_time_series(real):
    """Every analytics tool refused the data models whose time column is a
    DATE (macro series, the yield curve, reference rates) as having 'no time
    column', although the result records one: a date is its midnight UTC."""
    from marketlens_mcp.results.arrow import rows_to_table
    from marketlens_schema.data import EconomicObservation, YieldCurvePoint

    store, ctx = real
    prov = Provenance(provider="synthetic", route="GET /fred/series/observations", fetched_at=M0)
    rows = [
        EconomicObservation(
            source="fred",
            series_id=sid,
            date=dt.date(2023 + m // 12, m % 12 + 1, 1),
            value=base + m + (m % 3),
            knowledge_time=M0,
            pit="vintage",
        )
        for sid, base in (("CPI", 300.0), ("PCE", 120.0))
        for m in range(40)
    ]
    macro = store.put(
        rows_to_table(rows, EconomicObservation),
        tool="macro_fred_series",
        model=EconomicObservation.schema_name,
        provenance=prov,
    ).result_id
    assert {c.name: c.type for c in store.info(macro).columns}["date"] == "DATE"
    rets = call(RETURNS, ctx, result_id=macro)
    assert rets.row_count == 78 and rets.rows[0]["t"] == "2023-02-01T00:00:00Z"
    assert call(DRAWDOWN, ctx, result_id=macro).row_count == 2
    monthly = call(RESAMPLE, ctx, result_id=macro, timeframe="1mo")
    assert monthly.row_count == 80 and monthly.rows[0]["date"] == "2023-01-01"
    assert call(CORRELATION, ctx, result_id=macro).row_count == 4
    curve = [
        YieldCurvePoint(
            date=dt.date(2026, 1, 1) + dt.timedelta(days=d),
            tenor=t,
            rate=0.04 + d / 1e4 + i / 1e3,
            knowledge_time=M0,
        )
        for d in range(30)
        for i, t in enumerate(("1M", "10Y"))
    ]
    yc = store.put(
        rows_to_table(curve, YieldCurvePoint),
        tool="treasury_yield_curve",
        model=YieldCurvePoint.schema_name,
        provenance=prov,
    ).result_id
    assert call(RESAMPLE, ctx, result_id=yc, timeframe="1w").row_count == 10
    # A time column of another type is named with its type, not called missing.
    text = store.put(
        pa.table({"d": ["2026-01-01", "2026-01-02"], "v": [1.0, 2.0]}),
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=prov,
        time_column="d",
    ).result_id
    with pytest.raises(ToolError) as e:
        call(RETURNS, ctx, result_id=text, price_column="v")
    assert e.value.code == "not_time_series" and "the time column 'd' of" in e.value.message
    assert "is VARCHAR, not a timestamp or a date" in e.value.message


@pytest.mark.parametrize("inline_max_rows", [50, 1000])
def test_dynamic_outputs_follow_the_configured_inline_limits(tmp_path, inline_max_rows):
    """dynamic_output compared against the default 200 rows, not
    results.inline_max_rows: with 50, a 122-row resample was left to the
    offload, which stored it with no time or series column (the next resample
    then averaged every ticker into one series); with 1000, a 242-row resample
    was stored although it fits inline."""
    store = StoreRoot(tmp_path, StoreLimits(inline_max_rows=inline_max_rows)).session("test")
    ctx = testing.make_context(store=store, capabilities=["analytics", "results"])
    hours = [M0 + i * 60 * MINUTE for i in range(60 * 24)]
    table = pa.table(
        {
            "t": pa.array(hours * 2, pa.timestamp("us", tz="UTC")),
            "ticker": ["AAA"] * len(hours) + ["BBB"] * len(hours),
            "px": [100.0] * len(hours) + [1.0] * len(hours),
        }
    )
    prov = Provenance(provider="synthetic", route="duckdb:results_query", fetched_at=M0)
    rid = store.put(
        table,
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=prov,
        time_column="t",
        group_column="ticker",
    ).result_id
    if inline_max_rows == 50:
        daily = call(RESAMPLE, ctx, result_id=rid, timeframe="1d")
        assert isinstance(daily, ResultMarker) and daily.row_count == 122
        info = store.info(daily.result_id)
        assert (info.time_column, info.group_column) == ("t", "ticker")
        weekly = call(RESAMPLE, ctx, result_id=daily.result_id, timeframe="1w", agg="mean")
        assert {r["ticker"]: r["px"] for r in weekly.rows} == {"AAA": 100.0, "BBB": 1.0}
    else:
        half_days = call(RESAMPLE, ctx, result_id=rid, timeframe="12h")
        assert isinstance(half_days, InlineResult) and half_days.row_count == 242


def test_a_stored_query_keeps_its_default_value_column_after_its_parent_is_gone(real):
    """A stored query of bars defaulted to close only while the bars result
    was live: once it was dropped (or expired, which a parent always does
    first), analytics_returns refused with column_required."""
    store, ctx = real
    (query,) = [s for s in RESULTS_SPECS if s.name == "results_query"]
    both = put_bars(store, bars("AAPL", [100.0, 110.0, 99.0]) + bars("SPY", [400.0, 404.0, 402.0]))
    spy = call(query, ctx, sql=f"SELECT * FROM {both} WHERE ticker = 'SPY'", store=True).result_id
    before = call(RETURNS, ctx, result_id=spy)
    store.drop(both)
    after = call(RETURNS, ctx, result_id=spy)
    assert after.rows == before.rows and after.rows[0]["price_column"] == "close"
