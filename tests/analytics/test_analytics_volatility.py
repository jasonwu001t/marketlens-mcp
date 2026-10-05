"""analytics_volatility: rolling annualised volatility of returns.

Expected values: statistics.stdev over the window's returns x sqrt(ppy).
"""

from __future__ import annotations

import math
import statistics

import pyarrow as pa
import pydantic
import pytest
from analytics_harness import DAY, MINUTE, bars, output_rows, run, source_provenance, utc

from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS
from marketlens_mcp.analytics.tools.volatility import SPEC
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.analytics import VolatilityPoint

CLOSES = [100, 102, 99, 103, 101, 104]


def simple(prices):
    return [b / a - 1 for a, b in zip(prices, prices[1:], strict=False)]


def logr(prices):
    return [math.log(b / a) for a, b in zip(prices, prices[1:], strict=False)]


def rolling(rets, window, ppy):
    return [
        statistics.stdev(rets[i - window + 1 : i + 1]) * math.sqrt(ppy) for i in range(window - 1, len(rets))
    ]


def test_rolling_volatility_of_simple_returns_hand_computed(store, ctx):
    rid = store.put_rows(bars("AAPL", CLOSES))
    out = run(SPEC, ctx, result_id=rid, window=3, return_kind="simple")
    rows = output_rows(out, VolatilityPoint)
    t = [utc(2026, 1, 5, 21) + i * DAY for i in range(6)]
    assert [r.t for r in rows] == t[3:]
    assert [r.volatility for r in rows] == pytest.approx(rolling(simple(CLOSES), 3, 252), rel=1e-12)
    assert {(r.series, r.window, r.periods_per_year, r.return_kind) for r in rows} == {
        ("AAPL", 3, 252.0, "simple")
    }
    assert any("periods_per_year = 252" in n and "1d" in n for n in out.notes)
    assert any("window" in n and "omitted" in n for n in out.notes)
    assert out.provenance.route == "duckdb:analytics_volatility" and out.provenance.derived_from == [rid]


def test_default_is_log_returns_and_window_21(store, ctx):
    closes = [100 * (1 + 0.01 * ((i * 7) % 5 - 2)) ** i for i in range(30)]
    rid = store.put_rows(bars("AAPL", closes))
    rows = output_rows(run(SPEC, ctx, result_id=rid), VolatilityPoint)
    assert len(rows) == 29 - 21 + 1
    assert [r.volatility for r in rows] == pytest.approx(rolling(logr(closes), 21, 252), rel=1e-10)
    assert {r.return_kind for r in rows} == {"log"}


def test_on_a_returns_result_uses_its_kind_and_the_parent_timeframe(store, ctx):
    prices = store.put_rows(bars("AAPL", CLOSES, timeframe="1h", step=60 * MINUTE))
    rets = store.keep(run(RETURNS, ctx, result_id=prices, kind="log"), "analytics_returns")
    rows = output_rows(run(SPEC, ctx, result_id=rets, window=3), VolatilityPoint)
    assert [r.volatility for r in rows] == pytest.approx(rolling(logr(CLOSES), 3, 1638), rel=1e-12)
    assert {(r.return_kind, r.periods_per_year) for r in rows} == {("log", 1638.0)}


def test_return_kind_of_a_returns_result_cannot_be_overridden(store, ctx):
    prices = store.put_rows(bars("AAPL", CLOSES))
    rets = store.keep(run(RETURNS, ctx, result_id=prices, kind="simple"), "analytics_returns")
    out = run(SPEC, ctx, result_id=rets, window=3, return_kind="log")
    assert {r.return_kind for r in output_rows(out, VolatilityPoint)} == {"simple"}
    assert any("already holds simple returns" in n for n in out.notes)


@pytest.mark.parametrize(
    ("timeframe", "step", "ppy"),
    [
        ("1d", DAY, 252),
        ("1w", 7 * DAY, 52),
        ("1mo", 30 * DAY, 12),
        ("1h", 60 * MINUTE, 1638),
        ("5min", 5 * MINUTE, 19656),
    ],
)
def test_periods_per_year_defaults_from_the_bar_timeframe(store, ctx, timeframe, step, ppy):
    rid = store.put_rows(bars("AAPL", CLOSES, timeframe=timeframe, step=step))
    rows = output_rows(run(SPEC, ctx, result_id=rid, window=2), VolatilityPoint)
    assert {r.periods_per_year for r in rows} == {float(ppy)}


def test_periods_per_year_is_required_when_the_timeframe_is_unknown(store, ctx):
    rid = store.put_rows(bars("AAPL", CLOSES, timeframe="2h", step=120 * MINUTE))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=2)
    assert e.value.code == "periods_per_year_required" and "periods_per_year" in e.value.message
    rows = output_rows(run(SPEC, ctx, result_id=rid, window=2, periods_per_year=365), VolatilityPoint)
    assert {r.periods_per_year for r in rows} == {365.0}


def test_dynamic_prices_need_periods_per_year(store, ctx):
    t0 = utc(2026, 3, 2)
    table = pa.table(
        {
            "t": pa.array([t0 + i * DAY for i in range(4)], pa.timestamp("us", tz="UTC")),
            "px": [1.0, 1.1, 1.0, 1.2],
        }
    )
    rid = store.put_dynamic(table)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=2, price_column="px")
    assert e.value.code == "periods_per_year_required"
    rows = output_rows(
        run(
            SPEC, ctx, result_id=rid, window=2, price_column="px", periods_per_year=365, return_kind="simple"
        ),
        VolatilityPoint,
    )
    assert [r.volatility for r in rows] == pytest.approx(rolling(simple([1.0, 1.1, 1.0, 1.2]), 2, 365))


def test_too_few_returns_for_the_window_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", CLOSES))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=21)
    assert e.value.code == "too_few_points"
    assert "21" in e.value.message and "5" in e.value.message


def test_single_row_input_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [100]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=2)
    assert e.value.code == "too_few_points"


def test_short_series_is_omitted_with_a_note(store, ctx):
    rid = store.put_rows(bars("AAPL", CLOSES) + bars("MSFT", [10, 11, 12]))
    out = run(SPEC, ctx, result_id=rid, window=3)
    assert {r.series for r in output_rows(out, VolatilityPoint)} == {"AAPL"}
    assert any("MSFT" in n for n in out.notes)


def test_window_bounds_are_validated():
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "r_0000000001", "window": 1})
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "r_0000000001", "window": 2521})
    with pytest.raises(ValueError):
        SPEC.input_model.model_validate({"result_id": "r_0000000001", "periods_per_year": 0})


def test_gaps_use_consecutive_observations(store, ctx):
    times = [utc(2026, 1, 5, 21), utc(2026, 1, 6, 21), utc(2026, 1, 9, 21), utc(2026, 1, 12, 21)]
    rid = store.put_rows(bars("AAPL", [100, 104, 93.6, 98.28], times=times))
    rows = output_rows(run(SPEC, ctx, result_id=rid, window=3, return_kind="simple"), VolatilityPoint)
    assert [r.t for r in rows] == [times[3]]
    assert rows[0].volatility == pytest.approx(statistics.stdev([0.04, -0.1, 0.05]) * math.sqrt(252))


def test_split_adjusted_series_versus_raw(store, ctx):
    raw = store.put_rows(
        bars("AAPL", [100, 102, 51.5, 52]), provenance=source_provenance(request={"adjustment": "raw"})
    )
    adj = store.put_rows(
        bars("AAPL", [50, 51, 51.5, 52]), provenance=source_provenance(request={"adjustment": "all"})
    )
    raw_out = run(SPEC, ctx, result_id=raw, window=3, return_kind="simple")
    adj_out = run(SPEC, ctx, result_id=adj, window=3, return_kind="simple")
    raw_vol = output_rows(raw_out, VolatilityPoint)[0].volatility
    adj_vol = output_rows(adj_out, VolatilityPoint)[0].volatility
    assert adj_vol == pytest.approx(rolling(simple([50, 51, 51.5, 52]), 3, 252)[0])
    assert raw_vol > 10 * adj_vol
    assert any("not split-adjusted" in n for n in raw_out.notes)
    assert not any("split-adjusted" in n for n in adj_out.notes)


@pytest.mark.parametrize("ppy", [float("inf"), "inf", "Infinity", float("nan")])
def test_periods_per_year_must_be_finite(ppy):
    """An infinite annualisation factor produced null volatility with no reason;
    the input model refuses it before the tool runs."""
    with pytest.raises(pydantic.ValidationError):
        SPEC.input_model.model_validate({"result_id": "r_0123456789", "window": 2, "periods_per_year": ppy})
