"""analytics_drawdown: drawdown series and the maximum drawdown per series,
with peak, trough and recovery times.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from analytics_harness import DAY, bars, output_rows, run, source_provenance, utc

from marketlens_mcp.analytics.tools.drawdown import SPEC
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.analytics import DrawdownPoint, DrawdownSummary
from marketlens_schema.portfolio import PortfolioHistoryPoint

T0 = utc(2026, 1, 5, 21)
T = [T0 + i * DAY for i in range(8)]


def test_known_drawdown_and_recovery(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 120, 90, 60, 90, 130, 125]))
    out = run(SPEC, ctx, result_id=rid)
    (s,) = output_rows(out, DrawdownSummary)
    assert out.model is DrawdownSummary
    assert (s.series, s.max_drawdown, s.peak_t, s.trough_t, s.recovery_t) == ("AAPL", -0.5, T[1], T[3], T[5])
    assert (s.peak_to_trough_days, s.n_obs, s.value_column) == (2.0, 7, "close")
    assert s.absent is None
    assert out.provenance.route == "duckdb:analytics_drawdown"


def test_series_mode_hand_computed(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 120, 90, 60, 90, 130, 125]))
    out = run(SPEC, ctx, result_id=rid, mode="series")
    rows = output_rows(out, DrawdownPoint)
    assert out.model is DrawdownPoint
    assert [r.t for r in rows] == T[:7]
    assert [r.running_peak for r in rows] == [100, 120, 120, 120, 120, 130, 130]
    assert [r.drawdown for r in rows] == pytest.approx([0, 0, -0.25, -0.5, -0.25, 0, 125 / 130 - 1])
    assert [r.value for r in rows] == [100, 120, 90, 60, 90, 130, 125]


def test_never_recovering_series(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 80, 70, 75]))
    (s,) = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert (s.max_drawdown, s.peak_t, s.trough_t, s.recovery_t) == (pytest.approx(-0.3), T[0], T[2], None)
    assert s.absent == {"recovery_t": "not_recovered"}


def test_series_that_never_falls(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 101, 101, 105]))
    out = run(SPEC, ctx, result_id=rid)
    (s,) = output_rows(out, DrawdownSummary)
    assert (s.max_drawdown, s.peak_t, s.trough_t, s.recovery_t, s.peak_to_trough_days) == (
        0.0,
        T[0],
        T[0],
        None,
        0.0,
    )
    assert s.absent == {"recovery_t": "not_applicable"}


def test_equal_troughs_take_the_earliest(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 50, 100, 50, 120]))
    (s,) = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert (s.max_drawdown, s.peak_t, s.trough_t, s.recovery_t) == (-0.5, T[0], T[1], T[2])


def test_peak_is_the_last_time_at_the_peak_before_the_trough(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 120, 110, 120, 80, 121]))
    (s,) = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert (s.peak_t, s.trough_t, s.recovery_t) == (T[3], T[4], T[5])
    assert s.max_drawdown == pytest.approx(80 / 120 - 1)


def test_intraday_peak_to_trough_in_fractional_days(store, ctx):
    times = [utc(2026, 1, 5, 14), utc(2026, 1, 5, 20), utc(2026, 1, 6, 2)]
    rid = store.put_rows(bars("BTC/USD", [100, 120, 60], times=times, timeframe="1h", asset_class="crypto"))
    (s,) = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert s.peak_to_trough_days == pytest.approx(0.25)


def test_several_series(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 50, 100]) + bars("MSFT", [10, 9, 8]))
    rows = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert [(r.series, r.max_drawdown) for r in rows] == [("AAPL", -0.5), ("MSFT", pytest.approx(-0.2))]


def test_portfolio_equity_decimals(store, ctx):
    def point(day, equity):
        return PortfolioHistoryPoint(
            environment="paper",
            timeframe="1d",
            t=utc(2026, 1, day),
            equity=Decimal(equity),
            profit_loss=Decimal("0"),
            profit_loss_pct=0.0,
            base_value=Decimal("100"),
        )

    rid = store.put_rows([point(5, "1000"), point(6, "900"), point(7, "1100")], tool="portfolio_history")
    (s,) = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert (s.series, s.value_column, s.max_drawdown) == ("_all", "equity", pytest.approx(-0.1))
    assert s.recovery_t == utc(2026, 1, 7)


def test_split_adjusted_versus_raw(store, ctx):
    raw = store.put_rows(
        bars("AAPL", [100, 102, 51, 52]), provenance=source_provenance(request={"adjustment": "raw"})
    )
    adj = store.put_rows(
        bars("AAPL", [50, 51, 51, 52]), provenance=source_provenance(request={"adjustment": "split"})
    )
    raw_out, adj_out = run(SPEC, ctx, result_id=raw), run(SPEC, ctx, result_id=adj)
    assert output_rows(raw_out, DrawdownSummary)[0].max_drawdown == pytest.approx(-0.5)
    assert output_rows(adj_out, DrawdownSummary)[0].max_drawdown == 0.0
    assert any("not split-adjusted" in n for n in raw_out.notes)
    assert not any("split-adjusted" in n for n in adj_out.notes)


def test_single_row_input_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [100]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid)
    assert e.value.code == "too_few_points" and "at least 2" in e.value.message


def test_non_numeric_value_column_is_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [100, 90]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, value_column="timeframe")
    assert e.value.code == "not_numeric" and "'timeframe'" in e.value.message


def test_gaps_recover_at_the_next_observation(store, ctx):
    times = [T[0], T[1], T[4], T[7]]  # missing days between observations
    rid = store.put_rows(bars("AAPL", [100, 80, 90, 101], times=times))
    (s,) = output_rows(run(SPEC, ctx, result_id=rid), DrawdownSummary)
    assert (s.max_drawdown, s.peak_t, s.trough_t, s.recovery_t, s.n_obs) == (
        pytest.approx(-0.2),
        T[0],
        T[1],
        T[7],
        4,
    )


def test_a_ratio_column_is_not_a_price(store, ctx):
    def point(day, pct):
        return PortfolioHistoryPoint(
            environment="paper",
            timeframe="1d",
            t=utc(2026, 1, day),
            equity=Decimal("100"),
            profit_loss=Decimal("0"),
            profit_loss_pct=pct,
            base_value=Decimal("100"),
        )

    rid = store.put_rows([point(5, 0.01), point(6, -0.02)], tool="portfolio_history")
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, value_column="profit_loss_pct")
    assert e.value.code == "not_prices" and "'profit_loss_pct'" in e.value.message
