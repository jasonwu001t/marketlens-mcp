"""analytics_beta: beta, alpha and R squared of each asset series against a
one-series benchmark, static or rolling.

Expected values: statistics.covariance / variance / correlation / mean over
the returns of the timestamps both results share.
"""

from __future__ import annotations

import math
import statistics

import pytest
from analytics_harness import DAY, MINUTE, bars, output_rows, run, source_provenance, utc

from marketlens_mcp.analytics.tools.beta import SPEC
from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.analytics import BetaPoint, BetaResult

T0 = utc(2026, 1, 5, 21)
RB = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02]
RA_NOISY = [0.02, -0.03, 0.01, 0.012, -0.025, 0.03]


def prices(start, rets):
    out = [start]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out


def simple(ps):
    return [b / a - 1 for a, b in zip(ps, ps[1:], strict=False)]


def expected(ra, rb):
    beta = statistics.covariance(ra, rb) / statistics.variance(rb)
    return beta, statistics.mean(ra) - beta * statistics.mean(rb), statistics.correlation(ra, rb) ** 2


def test_exact_linear_relation(store, ctx):
    asset = store.put_rows(bars("AAPL", prices(100, [1.5 * r + 0.001 for r in RB])))
    bench = store.put_rows(bars("SPY", prices(400, RB)))
    out = run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3)
    (r,) = output_rows(out, BetaResult)
    assert out.model is BetaResult
    assert (r.series, r.benchmark, r.n_obs, r.return_kind, r.period) == ("AAPL", "SPY", 6, "simple", None)
    assert r.beta == pytest.approx(1.5, rel=1e-9)
    assert r.alpha == pytest.approx(0.001, abs=1e-12)
    assert r.r_squared == pytest.approx(1.0, rel=1e-12)
    assert (r.start, r.end) == (T0 + DAY, T0 + 6 * DAY)
    assert out.provenance.derived_from == [asset, bench]
    assert out.provenance.route == "duckdb:analytics_beta"


def test_noisy_series_against_statistics(store, ctx):
    asset = store.put_rows(bars("AAPL", prices(100, RA_NOISY)))
    bench = store.put_rows(bars("SPY", prices(400, RB)))
    (r,) = output_rows(
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3), BetaResult
    )
    beta, alpha, r2 = expected(simple(prices(100, RA_NOISY)), simple(prices(400, RB)))
    assert (r.beta, r.alpha, r.r_squared) == (
        pytest.approx(beta, rel=1e-10),
        pytest.approx(alpha, abs=1e-12),
        pytest.approx(r2, rel=1e-10),
    )


def test_log_returns(store, ctx):
    pa_, pb = prices(100, RA_NOISY), prices(400, RB)
    asset, bench = store.put_rows(bars("AAPL", pa_)), store.put_rows(bars("SPY", pb))
    out = run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3, return_kind="log")
    (r,) = output_rows(out, BetaResult)
    la = [math.log(b / a) for a, b in zip(pa_, pa_[1:], strict=False)]
    lb = [math.log(b / a) for a, b in zip(pb, pb[1:], strict=False)]
    assert r.beta == pytest.approx(expected(la, lb)[0], rel=1e-10) and r.return_kind == "log"


def test_returns_cover_the_same_interval_across_a_gap(store, ctx):
    pa_, pb = prices(100, RA_NOISY), prices(400, RB)
    times = [T0 + i * DAY for i in range(7)]
    asset = store.put_rows(bars("AAPL", pa_))
    bench = store.put_rows(
        bars("SPY", [p for i, p in enumerate(pb) if i != 3], times=[t for i, t in enumerate(times) if i != 3])
    )
    (r,) = output_rows(
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3), BetaResult
    )
    keep = [0, 1, 2, 4, 5, 6]
    beta, _, _ = expected(simple([pa_[i] for i in keep]), simple([pb[i] for i in keep]))
    assert r.n_obs == 5 and r.beta == pytest.approx(beta, rel=1e-10)


def test_too_few_joined_observations_give_none(store, ctx):
    asset = store.put_rows(bars("AAPL", prices(100, RA_NOISY)))
    bench = store.put_rows(bars("SPY", prices(400, RB)))
    (r,) = output_rows(run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench), BetaResult)
    assert (r.beta, r.alpha, r.r_squared, r.n_obs) == (None, None, None, 6)
    assert r.absent == {
        "beta": "insufficient_data",
        "alpha": "insufficient_data",
        "r_squared": "insufficient_data",
    }


def test_flat_benchmark_gives_none_not_applicable(store, ctx):
    asset = store.put_rows(bars("AAPL", prices(100, RA_NOISY)))
    bench = store.put_rows(bars("SPY", [400.0] * 7))
    (r,) = output_rows(
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3), BetaResult
    )
    assert (r.beta, r.alpha, r.r_squared) == (None, None, None)
    assert r.absent == {"beta": "not_applicable", "alpha": "not_applicable", "r_squared": "not_applicable"}


def test_one_row_per_asset_series(store, ctx):
    asset = store.put_rows(
        bars("AAPL", prices(100, RA_NOISY)) + bars("MSFT", prices(50, [2 * r for r in RB]))
    )
    bench = store.put_rows(bars("SPY", prices(400, RB)))
    rows = output_rows(
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3), BetaResult
    )
    assert [r.series for r in rows] == ["AAPL", "MSFT"]
    assert rows[1].beta == pytest.approx(2.0, rel=1e-9)


def test_rolling_beta(store, ctx):
    pa_, pb = prices(100, RA_NOISY), prices(400, RB)
    asset, bench = store.put_rows(bars("AAPL", pa_)), store.put_rows(bars("SPY", pb))
    out = run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, window=3)
    rows = output_rows(out, BetaPoint)
    assert out.model is BetaPoint
    ra, rb = simple(pa_), simple(pb)
    want = [
        statistics.covariance(ra[i - 2 : i + 1], rb[i - 2 : i + 1]) / statistics.variance(rb[i - 2 : i + 1])
        for i in range(2, 6)
    ]
    assert [r.t for r in rows] == [T0 + (i + 1) * DAY for i in range(2, 6)]
    assert [r.beta for r in rows] == pytest.approx(want, rel=1e-10)
    assert {(r.series, r.benchmark, r.window) for r in rows} == {("AAPL", "SPY", 3)}


def test_benchmark_must_be_one_series(store, ctx):
    asset = store.put_rows(bars("AAPL", prices(100, RA_NOISY)))
    bench = store.put_rows(bars("SPY", prices(400, RB)) + bars("QQQ", prices(300, RB)))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench)
    assert e.value.code == "benchmark_not_single_series" and "2" in e.value.message


def test_mismatched_frequencies_need_a_period(store, ctx):
    hours = []
    for d in range(5):
        hours += [utc(2026, 1, 5 + d, 15), utc(2026, 1, 5 + d, 20)]
    hourly = [100 + i + (i % 3) for i in range(10)]
    asset = store.put_rows(bars("AAPL", hourly, times=hours, timeframe="1h"))
    daily = [400, 404, 401, 409, 405]
    bench = store.put_rows(bars("SPY", daily, times=[utc(2026, 1, 5 + d, 21) for d in range(5)]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3)
    assert e.value.code == "mismatched_frequency" and "1h" in e.value.message and "1d" in e.value.message
    assert "period" in e.value.message
    (r,) = output_rows(
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, min_obs=3, period="1d"), BetaResult
    )
    beta, _, _ = expected(simple(hourly[1::2]), simple(daily))
    assert (r.period, r.n_obs) == ("1d", 4) and r.beta == pytest.approx(beta, rel=1e-10)
    assert (r.start, r.end) == (utc(2026, 1, 6), utc(2026, 1, 9))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench, period="5min")
    assert e.value.code == "mismatched_frequency"


def test_returns_results_are_refused(store, ctx):
    asset = store.put_rows(bars("AAPL", prices(100, RA_NOISY), step=MINUTE, timeframe="1min"))
    rets = store.keep(run(RETURNS, ctx, result_id=asset), "analytics_returns")
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, asset_result_id=rets, benchmark_result_id=asset)
    assert (
        e.value.code == "not_prices"
        and "marketlens.ReturnPoint" in e.value.message
        and "fraction" in e.value.message
    )


def test_single_row_input_is_refused(store, ctx):
    asset = store.put_rows(bars("AAPL", [100]))
    bench = store.put_rows(bars("SPY", prices(400, RB)))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, asset_result_id=asset, benchmark_result_id=bench)
    assert e.value.code == "too_few_points"


def test_split_in_raw_asset_prices_distorts_beta(store, ctx):
    adjusted = prices(50, [1.5 * r for r in RB])
    raw = [p * (2 if i < 3 else 1) for i, p in enumerate(adjusted)]  # a 2:1 split before day 3
    bench = store.put_rows(bars("SPY", prices(400, RB)))
    adj_id = store.put_rows(
        bars("AAPL", adjusted), provenance=source_provenance(request={"adjustment": "all"})
    )
    raw_id = store.put_rows(bars("AAPL", raw), provenance=source_provenance(request={"adjustment": "raw"}))
    (adj,) = output_rows(
        run(SPEC, ctx, asset_result_id=adj_id, benchmark_result_id=bench, min_obs=3), BetaResult
    )
    raw_out = run(SPEC, ctx, asset_result_id=raw_id, benchmark_result_id=bench, min_obs=3)
    (rawr,) = output_rows(raw_out, BetaResult)
    assert adj.beta == pytest.approx(1.5, rel=1e-9)
    assert abs(rawr.beta - 1.5) > 1
    assert any("not split-adjusted" in n for n in raw_out.notes)
