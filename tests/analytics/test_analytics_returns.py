"""analytics_returns: simple and log returns per series, optionally by period.

Hand-computed fixtures: two tickers with known returns, a series with a gap,
weekly sampling across a UTC week boundary, a single-row input, and a raw
series with a 2:1 split next to its split-adjusted version.
"""

from __future__ import annotations

import math

import pyarrow as pa
import pytest
from analytics_harness import DAY, bars, output_rows, run, source_provenance, utc

from marketlens_mcp.analytics.tools.returns import SPEC
from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.results_api import ResultNotFound
from marketlens_schema.analytics import ReturnPoint
from marketlens_schema.market import Asset


def test_simple_returns_per_series_hand_computed(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 110, 99, 99, 108.9]) + bars("MSFT", [50, 55, 66]))
    out = run(SPEC, ctx, result_id=rid)
    rows = output_rows(out, ReturnPoint)
    got = [(r.series, r.t, r.ret) for r in rows]
    t = [utc(2026, 1, 5, 21) + i * DAY for i in range(5)]
    expected = [
        ("AAPL", t[1], 0.1),
        ("AAPL", t[2], -0.1),
        ("AAPL", t[3], 0.0),
        ("AAPL", t[4], 0.1),
        ("MSFT", t[1], 0.1),
        ("MSFT", t[2], 0.2),
    ]
    assert [(s, tt) for s, tt, _ in got] == [(s, tt) for s, tt, _ in expected]
    assert [r for _, _, r in got] == pytest.approx([r for _, _, r in expected], abs=1e-12)
    assert {r.kind for r in rows} == {"simple"}
    assert {r.period for r in rows} == {None}
    assert {r.price_column for r in rows} == {"close"}
    assert out.model is ReturnPoint
    assert out.offload == "auto"
    p = out.provenance
    assert (p.provider, p.route, p.derived_from) == ("marketlens", "duckdb:analytics_returns", [rid])
    assert p.request["result_id"] == rid and p.request["kind"] == "simple"
    assert p.as_of == utc(2026, 10, 2, 20) and p.feed == "iex"
    assert any("consecutive observations" in n for n in out.notes)


def test_log_returns(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 110, 99]))
    rows = output_rows(run(SPEC, ctx, result_id=rid, kind="log"), ReturnPoint)
    assert [r.ret for r in rows] == pytest.approx([math.log(1.1), math.log(0.9)], abs=1e-12)
    assert {r.kind for r in rows} == {"log"}


def test_gaps_are_not_filled(store, ctx):
    # Mon, Tue, then Fri: the Friday return spans the gap.
    times = [utc(2026, 1, 5, 21), utc(2026, 1, 6, 21), utc(2026, 1, 9, 21)]
    rid = store.put_rows(bars("AAPL", [100, 104, 93.6], times=times))
    rows = output_rows(run(SPEC, ctx, result_id=rid), ReturnPoint)
    assert [(r.t, r.ret) for r in rows] == [
        (times[1], pytest.approx(0.04)),
        (times[2], pytest.approx(-0.1)),
    ]


def test_period_samples_the_last_price_per_utc_week(store, ctx):
    # The Sunday 23:30Z bar is already Monday in the fake session's zone (Tokyo):
    # UTC weeks put it in the week of Jan 5, so there are three weekly prices.
    times = [utc(2026, 1, 11, 23, 30), utc(2026, 1, 12, 0, 30), utc(2026, 1, 19, 1)]
    rid = store.put_rows(bars("BTC/USD", [100, 110, 121], times=times, timeframe="1h", asset_class="crypto"))
    out = run(SPEC, ctx, result_id=rid, period="1w")
    rows = output_rows(out, ReturnPoint)
    assert [(r.t, r.ret) for r in rows] == [
        (utc(2026, 1, 12), pytest.approx(0.1)),
        (utc(2026, 1, 19), pytest.approx(0.1)),
    ]
    assert {r.period for r in rows} == {"1w"}
    assert any("last price in each 1w bucket" in n for n in out.notes)


def test_period_over_daily_bars(store, ctx):
    closes = [10, 11, 12, 13, 14, 15, 14, 13, 12, 12.6, 12.0, 13.86]
    times = [utc(2026, 1, 5 + i, 21) for i in range(5)] + [utc(2026, 1, 12 + i, 21) for i in range(5)]
    times += [utc(2026, 1, 19, 21), utc(2026, 1, 20, 21)]
    rid = store.put_rows(bars("AAPL", closes, times=times))
    rows = output_rows(run(SPEC, ctx, result_id=rid, period="1w"), ReturnPoint)
    assert [(r.t, r.ret) for r in rows] == [
        (utc(2026, 1, 12), pytest.approx(12.6 / 14 - 1)),
        (utc(2026, 1, 19), pytest.approx(13.86 / 12.6 - 1)),
    ]


def test_period_finer_than_the_input_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 101, 102]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, period="1h")
    assert e.value.code == "mismatched_frequency"
    assert "1h" in e.value.message and "1d" in e.value.message


def test_single_row_input_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [100]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "too_few_points"
    assert rid in e.value.message and "at least 2" in e.value.message


def test_a_series_with_one_price_is_omitted_with_a_note(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 110]) + bars("MSFT", [50]))
    out = run(SPEC, ctx, result_id=rid)
    assert [r.series for r in output_rows(out, ReturnPoint)] == ["AAPL"]
    assert any("MSFT" in n and "fewer than 2" in n for n in out.notes)


def test_split_adjusted_series_has_no_false_crash(store, ctx):
    raw = store.put_rows(
        bars("AAPL", [100, 102, 51.5, 52]), provenance=source_provenance(request={"adjustment": "raw"})
    )
    adjusted = store.put_rows(
        bars("AAPL", [50, 51, 51.5, 52]), provenance=source_provenance(request={"adjustment": "all"})
    )
    raw_out = run(SPEC, ctx, result_id=raw)
    adj_out = run(SPEC, ctx, result_id=adjusted)
    assert [r.ret for r in output_rows(raw_out, ReturnPoint)] == pytest.approx(
        [0.02, 51.5 / 102 - 1, 52 / 51.5 - 1]
    )
    assert [r.ret for r in output_rows(adj_out, ReturnPoint)] == pytest.approx(
        [0.02, 51.5 / 51 - 1, 52 / 51.5 - 1]
    )
    assert any("not split-adjusted" in n and "adjustment=raw" in n for n in raw_out.notes)
    assert not any("split-adjusted" in n for n in adj_out.notes)


def test_dynamic_input_null_and_nonpositive_prices_are_skipped(store, ctx):
    t0 = utc(2026, 3, 2, 15)
    table = pa.table(
        {
            "sym": ["X"] * 6,
            "t": pa.array([t0 + i * DAY for i in range(6)], pa.timestamp("us", tz="UTC")),
            "px": [10.0, None, 11.0, 0.0, -1.0, 12.1],
        }
    )
    rid = store.put_dynamic(table)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "column_required" and "price_column" in e.value.message and "px" in e.value.message
    out = run(SPEC, ctx, result_id=rid, price_column="px")
    rows = output_rows(out, ReturnPoint)
    assert [(r.series, r.t, r.ret) for r in rows] == [
        ("_all", t0 + 2 * DAY, pytest.approx(0.1)),
        ("_all", t0 + 5 * DAY, pytest.approx(0.1)),
    ]
    assert any("1 row with a NULL px" in n for n in out.notes)
    assert any("2 rows with px <= 0" in n for n in out.notes)
    assert any("time column 't'" in n for n in out.notes)  # inferred for a dynamic result


def test_duplicate_timestamps_are_refused_until_split_by_series(store, ctx):
    t0 = utc(2026, 3, 2, 15)
    table = pa.table(
        {
            "ticker": ["A", "B", "A", "B"],
            "t": pa.array([t0, t0, t0 + DAY, t0 + DAY], pa.timestamp("us", tz="UTC")),
            "px": [10.0, 20.0, 11.0, 22.0],
        }
    )
    rid = store.put_dynamic(table)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, price_column="px")
    assert e.value.code == "duplicate_timestamps" and "series_column" in e.value.message
    rows = output_rows(run(SPEC, ctx, result_id=rid, price_column="px", series_column="ticker"), ReturnPoint)
    assert [(r.series, r.ret) for r in rows] == [("A", pytest.approx(0.1)), ("B", pytest.approx(0.1))]


def test_non_numeric_column_is_refused_with_the_numeric_columns(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 101]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, price_column="ticker")
    assert e.value.code == "not_numeric"
    assert e.value.message == (
        "analytics_returns needs a numeric column; 'ticker' is not one of open, high, low, close, volume, "
        f"trade_count, vwap in {rid} (marketlens.Bar)."
    )
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, price_column="nope")
    assert e.value.code == "not_numeric" and "'nope'" in e.value.message


def test_unknown_series_column_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 101]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, series_column="sector")
    assert e.value.code == "unknown_column" and "'sector'" in e.value.message


def test_non_time_series_result_is_refused(store, ctx):
    rid = store.put_rows(
        [Asset(ticker="AAPL", asset_class="us_equity", status="active", tradable=True)],
        tool="reference_assets",
    )
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "not_time_series"
    assert rid in e.value.message and "marketlens.Asset" in e.value.message


def test_unknown_result_propagates_result_not_found(store, ctx):
    with pytest.raises(ResultNotFound):
        run(SPEC, ctx, result_id="r_00000000ff")


def test_result_id_is_validated():
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "/etc/passwd"})
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "r_0000000001", "extra": 1})


def test_a_ratio_column_is_not_a_price(store, ctx):
    prices = store.put_rows(bars("AAPL", [100, 110, 99]))
    rets = store.keep(run(SPEC, ctx, result_id=prices), "analytics_returns")
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rets)
    assert e.value.code == "not_prices"
    assert (
        "'ret'" in e.value.message
        and "fraction" in e.value.message
        and "marketlens.ReturnPoint" in e.value.message
    )


def datastore_values(**extra):
    times = [utc(2026, 1, d) for d in (1, 2, 3)]
    stamp = pa.timestamp("us", tz="UTC")
    cols = {
        "event_time": pa.array(times, stamp),
        "knowledge_time": pa.array([t + DAY for t in times], stamp),
        "ingested_at": pa.array([utc(2026, 2, 1)] * 3, stamp),
        "value": [100.0, 110.0, 99.0],
    }
    return pa.table({**cols, **extra})


def test_event_time_is_the_time_column_next_to_the_point_in_time_clocks(store, ctx):
    rid = store.put_dynamic(datastore_values())
    out = run(SPEC, ctx, result_id=rid, price_column="value")
    assert [r["t"] for r in out.table.to_pylist()] == [utc(2026, 1, 2), utc(2026, 1, 3)]
    assert (
        f"{rid} has no recorded time column; time column 'event_time' was used, not the point-in-time clock "
        "knowledge_time, ingested_at." in out.notes
    )
    # Another timestamp besides the clocks: still ambiguous.
    other = store.put_dynamic(
        datastore_values(release_time=pa.array([utc(2026, 1, 9)] * 3, pa.timestamp("us", tz="UTC")))
    )
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=other, price_column="value")
    assert e.value.code == "not_time_series" and "several timestamp columns" in e.value.message
