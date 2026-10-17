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
    dataset's knowledge_time, with its role, beside the bars' own time. Matched
    on knowledge_time alone, every joined row was known before the first bar,
    so a left row got the last bar of the join, dated after it."""
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
    assert {c.name: c.role for c in store.info(joined).columns}["knowledge_time"] == "available"
    left = put_bars(store, bars("QQQ", [50.0] * 2, times=[utc(2026, 9, 3, 12), utc(2026, 9, 7, 12)]))
    out = call(ALIGN, ctx, left_result_id=left, right_result_id=joined, right_columns=["close", "value"])
    assert [(r["matched_t"][:13], r["close_right"]) for r in out.rows] == [
        ("2026-09-02T20", 101.0),
        ("2026-09-06T20", 105.0),
    ]
