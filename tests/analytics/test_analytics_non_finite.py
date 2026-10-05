"""Non-finite values (NaN, +inf, -inf) in an input.

A result stored by results_query (store=true) can hold 'nan'::DOUBLE, and in
DuckDB NaN > 0 is true and NaN sorts above every number. Every analytics tool
skips the rows whose value is not finite, counts them in a note, and gives the
same values as on the cleaned series (the same rows without the non-finite
ones). Expected values are hand-computed or come from the cleaned input.
"""

from __future__ import annotations

import math

import pyarrow as pa
import pytest
from analytics_harness import DAY, MINUTE, bars, dyn_rows, output_rows, run, unexplained_dynamic, utc

from marketlens_mcp.analytics.tools.beta import SPEC as BETA
from marketlens_mcp.analytics.tools.correlation import SPEC as CORRELATION
from marketlens_mcp.analytics.tools.drawdown import SPEC as DRAWDOWN
from marketlens_mcp.analytics.tools.resample import SPEC as RESAMPLE
from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS
from marketlens_mcp.analytics.tools.volatility import SPEC as VOLATILITY
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.analytics import (
    BetaPoint,
    BetaResult,
    CorrelationCell,
    DrawdownPoint,
    DrawdownSummary,
    ReturnPoint,
    VolatilityPoint,
)
from marketlens_schema.market import Bar

NAN, INF = math.nan, math.inf
T0 = utc(2026, 1, 5, 21)
T = [T0 + i * DAY for i in range(12)]
M0 = utc(2026, 1, 5, 13, 30)


def finite_part(values, times):
    keep = [i for i, v in enumerate(values) if math.isfinite(v)]
    return [values[i] for i in keep], [times[i] for i in keep]


def put_pair(store, ticker, closes):
    """Two stored bar results: ``closes`` as given, and the cleaned series (the
    same rows without the non-finite closes)."""
    times = T[: len(closes)]
    dirty = store.put_rows(bars(ticker, closes, times=times))
    clean_closes, clean_times = finite_part(closes, times)
    clean = store.put_rows(bars(ticker, clean_closes, times=clean_times))
    return dirty, clean


def skip_note(n, column):
    return f"Skipped {n} row{'s' if n != 1 else ''} with a non-finite {column} (NaN or infinity)."


# --- returns --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["simple", "log"])
@pytest.mark.parametrize("period", [None, "1w"])
def test_returns_skip_nan_and_infinite_prices(store, ctx, kind, period):
    dirty, clean = put_pair(store, "AAPL", [100, 110, NAN, 99, INF, 108.9, -INF, 119.79])
    out = run(RETURNS, ctx, result_id=dirty, kind=kind, period=period)
    rows = output_rows(out, ReturnPoint)
    assert rows == output_rows(run(RETURNS, ctx, result_id=clean, kind=kind, period=period), ReturnPoint)
    assert all(math.isfinite(r.ret) for r in rows)
    assert skip_note(3, "close") in out.notes
    # -inf is not finite: it is not counted as a non-positive price.
    assert not any("<= 0" in n for n in out.notes)


def test_returns_hand_computed_around_non_finite_prices(store, ctx):
    dirty, _ = put_pair(store, "AAPL", [100, 110, NAN, 99, INF, 108.9, -INF, 119.79])
    rows = output_rows(run(RETURNS, ctx, result_id=dirty), ReturnPoint)
    assert [r.t for r in rows] == [T[1], T[3], T[5], T[7]]
    assert [r.ret for r in rows] == pytest.approx([0.1, -0.1, 0.1, 0.1], abs=1e-12)


def test_returns_of_a_query_result_holding_nan(store, ctx):
    # What results_query (store=true) keeps for SELECT ... 'nan'::DOUBLE AS px.
    times = T[:4]
    rid = store.put_dynamic(
        pa.table({"t": pa.array(times, pa.timestamp("us", tz="UTC")), "px": [100.0, NAN, 110.0, INF]}),
        time_column="t",
    )
    out = run(RETURNS, ctx, result_id=rid, price_column="px")
    (r,) = output_rows(out, ReturnPoint)
    assert (r.t, r.ret) == (times[2], pytest.approx(0.1, abs=1e-12))
    assert skip_note(2, "px") in out.notes


# --- drawdown ---------------------------------------------------------------------------------


def test_drawdown_skips_a_nan_value(store, ctx):
    dirty, clean = put_pair(store, "AAPL", [100, 120, NAN, 90, 130])
    out = run(DRAWDOWN, ctx, result_id=dirty)
    (s,) = output_rows(out, DrawdownSummary)
    assert (s.max_drawdown, s.n_obs, s.peak_t, s.trough_t, s.recovery_t) == (-0.25, 4, T[1], T[3], T[4])
    assert skip_note(1, "close") in out.notes
    assert [s] == output_rows(run(DRAWDOWN, ctx, result_id=clean), DrawdownSummary)


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
@pytest.mark.parametrize("mode", ["max", "series"])
def test_drawdown_equals_the_cleaned_series(store, ctx, bad, mode):
    dirty, clean = put_pair(store, "AAPL", [100, bad, 120, 90, bad, 60, 130])
    model = DrawdownSummary if mode == "max" else DrawdownPoint
    out = run(DRAWDOWN, ctx, result_id=dirty, mode=mode)
    assert output_rows(out, model) == output_rows(run(DRAWDOWN, ctx, result_id=clean, mode=mode), model)
    assert skip_note(2, "close") in out.notes


# --- volatility -------------------------------------------------------------------------------


@pytest.mark.parametrize("return_kind", ["log", "simple"])
def test_volatility_skips_infinite_and_nan_prices(store, ctx, return_kind):
    # An inf price used to reach ln(v / v0) with v0 = inf: "cannot take logarithm of zero".
    dirty, clean = put_pair(store, "AAPL", [100, 101, INF, 102, 99, NAN, 103, 104, -INF, 101])
    out = run(VOLATILITY, ctx, result_id=dirty, window=3, return_kind=return_kind)
    rows = output_rows(out, VolatilityPoint)
    assert rows == output_rows(
        run(VOLATILITY, ctx, result_id=clean, window=3, return_kind=return_kind), VolatilityPoint
    )
    assert len(rows) == 4 and all(math.isfinite(r.volatility) for r in rows)
    assert skip_note(3, "close") in out.notes


def test_volatility_of_a_returns_result_skips_non_finite_returns(store, ctx):
    rets = [0.01, NAN, -0.02, 0.015, INF, 0.0, -INF, -0.01]

    def put(values, times):
        return store.put_rows(
            [
                ReturnPoint(series="AAPL", t=t, ret=r, kind="log", period=None, price_column="close")
                for r, t in zip(values, times, strict=True)
            ],
            tool="analytics_returns",
        )

    dirty = put(rets, T[: len(rets)])
    clean = put(*finite_part(rets, T[: len(rets)]))
    out = run(VOLATILITY, ctx, result_id=dirty, window=2, periods_per_year=252)
    rows = output_rows(out, VolatilityPoint)
    assert rows == output_rows(
        run(VOLATILITY, ctx, result_id=clean, window=2, periods_per_year=252), VolatilityPoint
    )
    assert len(rows) == 4 and all(math.isfinite(r.volatility) for r in rows)
    assert skip_note(3, "ret") in out.notes


# --- correlation ------------------------------------------------------------------------------

A = [100, 101, NAN, 103, 102, 104, 105, 103]
B = [50, INF, 51, 52, 51.5, 53, -INF, 52.5]


def test_correlation_of_prices_skips_non_finite_rows(store, ctx):
    times = T[: len(A)]
    dirty = store.put_rows(bars("AAA", A, times=times) + bars("BBB", B, times=times))
    (ca, ta), (cb, tb) = finite_part(A, times), finite_part(B, times)
    clean = store.put_rows(bars("AAA", ca, times=ta) + bars("BBB", cb, times=tb))
    out = run(CORRELATION, ctx, result_id=dirty, min_overlap=2)
    rows = output_rows(out, CorrelationCell)
    assert rows == output_rows(run(CORRELATION, ctx, result_id=clean, min_overlap=2), CorrelationCell)
    assert all(r.correlation is not None and -1 <= r.correlation <= 1 for r in rows)
    assert skip_note(3, "close") in out.notes


def test_correlation_of_a_returns_column_skips_non_finite_rows(store, ctx):
    # A non-price column (no unit) is correlated as given: the non-positive filter is not used.
    ra = [0.01, -0.02, NAN, 0.03, 0.0, -0.01, 0.02]
    rb = [0.02, INF, -0.01, 0.025, 0.005, -0.02, 0.01]
    times = T[: len(ra)]

    def put(a, ta, b, tb):
        stamps = pa.array(list(ta) + list(tb), pa.timestamp("us", tz="UTC"))
        return store.put_dynamic(
            pa.table({"t": stamps, "sym": ["A"] * len(a) + ["B"] * len(b), "ret": list(a) + list(b)}),
            time_column="t",
            group_column="sym",
        )

    dirty = put(ra, times, rb, times)
    clean = put(*finite_part(ra, times), *finite_part(rb, times))
    out = run(CORRELATION, ctx, result_id=dirty, min_overlap=2)
    rows = output_rows(out, CorrelationCell)
    assert rows == output_rows(run(CORRELATION, ctx, result_id=clean, min_overlap=2), CorrelationCell)
    assert all(r.correlation is not None and math.isfinite(r.correlation) for r in rows)
    assert skip_note(2, "ret") in out.notes


# --- beta -----------------------------------------------------------------------------------

ASSET = [100, 102, NAN, 101, 104, 103, 106, INF, 107]
BENCH = [400, 404, 402, -INF, 410, 408, 412, 414, NAN]


@pytest.mark.parametrize("window", [None, 3])
def test_beta_skips_non_finite_prices_in_both_results(store, ctx, window):
    times = T[: len(ASSET)]
    (ca, ta), (cb, tb) = finite_part(ASSET, times), finite_part(BENCH, times)
    asset, asset_clean = (
        store.put_rows(bars("AAPL", ASSET, times=times)),
        store.put_rows(bars("AAPL", ca, times=ta)),
    )
    bench, bench_clean = (
        store.put_rows(bars("SPY", BENCH, times=times)),
        store.put_rows(bars("SPY", cb, times=tb)),
    )
    model = BetaResult if window is None else BetaPoint
    out = run(BETA, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=2, window=window)
    rows = output_rows(out, model)
    clean = run(
        BETA, ctx, asset_result_id=asset_clean, benchmark_result_id=bench_clean, min_obs=2, window=window
    )
    assert rows == output_rows(clean, model)
    assert rows and all(r.beta is not None and math.isfinite(r.beta) for r in rows)
    assert out.notes.count(skip_note(2, "close")) == 2


# --- resample -------------------------------------------------------------------------------

CLOSE = [10.2, 10.4, 10.0, 10.7, 10.8, 10.9, 11.1, 10.6, 10.5, 10.4]


def minute_bars(rows):
    """1min bars of AAPL from (index, close, high, volume, vwap) tuples; open 10.0, low 9.9."""
    return [
        bars(
            "AAPL",
            [c],
            times=[M0 + i * MINUTE],
            timeframe="1min",
            opens=[10.0],
            highs=[h],
            lows=[9.9],
            volumes=[v],
            vwaps=[w],
        )[0]
        for i, c, h, v, w in rows
    ]


def test_resample_skips_bars_with_a_non_finite_value(store, ctx):
    rows = [(i, c, c + 0.2, 100.0, c) for i, c in enumerate(CLOSE)]
    rows[1] = (1, NAN, 10.6, 100.0, 10.3)  # close
    rows[3] = (3, 10.7, INF, 100.0, 10.5)  # high
    rows[6] = (6, 11.1, 11.3, 100.0, NAN)  # vwap
    rows[8] = (8, 10.5, 10.7, INF, 10.5)  # volume
    dirty = store.put_rows(minute_bars(rows))
    clean = store.put_rows(minute_bars([r for k, r in enumerate(rows) if k not in (1, 3, 6, 8)]))
    out = run(RESAMPLE, ctx, result_id=dirty, timeframe="5min")
    got = output_rows(out, Bar)
    assert got == output_rows(run(RESAMPLE, ctx, result_id=clean, timeframe="5min"), Bar)
    assert len(got) == 2
    assert all(math.isfinite(x) for b in got for x in (b.open, b.high, b.low, b.close, b.volume, b.vwap))
    assert (
        "Skipped 4 rows with a non-finite value (NaN or infinity) in high, close, volume, vwap." in out.notes
    )


@pytest.mark.parametrize("agg", ["last", "first", "mean", "sum", "min", "max"])
def test_resample_of_a_query_result_skips_non_finite_rows(store, ctx, agg):
    px = [1.0, NAN, 3.0, 4.0, INF, 6.0, 7.0, -INF, 9.0, 10.0, NAN, INF]
    qty = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0]
    times = [M0 + i * MINUTE for i in range(len(px))]
    keep = [i for i, v in enumerate(px) if math.isfinite(v)]

    def put(idx):
        return store.put_dynamic(
            pa.table(
                {
                    "t": pa.array([times[i] for i in idx], pa.timestamp("us", tz="UTC")),
                    "px": [px[i] for i in idx],
                    "qty": [qty[i] for i in idx],
                }
            ),
            time_column="t",
        )

    dirty, clean = put(range(len(px))), put(keep)
    out = run(RESAMPLE, ctx, result_id=dirty, timeframe="5min", agg=agg)
    got = dyn_rows(out)
    assert got == dyn_rows(run(RESAMPLE, ctx, result_id=clean, timeframe="5min", agg=agg))
    # The third bucket held only non-finite prices: it is skipped like an empty bucket.
    assert [r["t"] for r in got] == [M0, M0 + 5 * MINUTE]
    assert all(math.isfinite(r["px"]) for r in got)
    assert unexplained_dynamic(got, out.absent) == []
    assert "Skipped 5 rows with a non-finite value (NaN or infinity) in px." in out.notes


def test_resample_refuses_when_no_row_is_finite(store, ctx):
    rid = store.put_dynamic(
        pa.table({"t": pa.array([M0, M0 + MINUTE], pa.timestamp("us", tz="UTC")), "px": [NAN, INF]}),
        time_column="t",
    )
    with pytest.raises(ToolError) as e:
        run(RESAMPLE, ctx, result_id=rid, timeframe="5min")
    assert e.value.code == "too_few_points"
    assert "2 rows with a non-finite value" in e.value.message
