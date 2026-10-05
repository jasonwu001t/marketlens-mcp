"""analytics_resample: bars to a coarser timeframe with the OHLCV rules;
other time series with one aggregation per value column.
"""

from __future__ import annotations

from decimal import Decimal

import pyarrow as pa
import pytest
from analytics_harness import MINUTE, bars, output_rows, run, unexplained_dynamic, utc

from marketlens_mcp.analytics.tools.resample import BAR_RULES, SPEC
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.market import Bar, OptionBar, Trade
from marketlens_schema.portfolio import PortfolioHistoryPoint

M0 = utc(2026, 1, 5, 13, 30)
OPEN = [10.0, 10.2, 10.4, 10.0, 10.7, 10.8, 10.9, 11.1, 10.6, 10.5]
HIGH = [10.5, 10.6, 10.4, 10.8, 10.9, 11.0, 11.2, 11.1, 10.7, 10.6]
LOW = [9.8, 10.1, 9.9, 10.0, 10.6, 10.7, 10.9, 10.5, 10.4, 10.3]
CLOSE = [10.2, 10.4, 10.0, 10.7, 10.8, 10.9, 11.1, 10.6, 10.5, 10.4]
VOLUME = [100.0, 200.0, 300.0, 100.0, 300.0, 50.0, 150.0, 200.0, 100.0, 0.0]
VWAP = [10.1, 10.3, 10.2, 10.5, 10.75, 10.85, 11.05, 10.8, 10.55, None]
TRADES = [5, 6, 7, 8, 9, None, 4, 3, 2, 0]


def minute_bars(ticker="AAPL", start=M0, **kw):
    args = dict(opens=OPEN, highs=HIGH, lows=LOW, volumes=VOLUME, vwaps=VWAP, trade_counts=TRADES)
    args.update(kw)
    return bars(ticker, CLOSE, start=start, step=MINUTE, timeframe="1min", **args)


def test_one_minute_bars_to_five_minutes_hand_computed(store, ctx):
    rid = store.put_rows(minute_bars())
    out = run(SPEC, ctx, result_id=rid, timeframe="5min")
    rows = output_rows(out, Bar)
    assert out.model is Bar
    a, b = rows
    assert (a.ticker, a.timeframe, a.t) == ("AAPL", "5min", M0)
    assert (a.open, a.high, a.low, a.close, a.volume, a.trade_count) == (10.0, 10.9, 9.8, 10.8, 1000.0, 35)
    assert a.vwap == pytest.approx(10405 / 1000)
    assert a.absent is None
    assert (b.t, b.open, b.high, b.low, b.close, b.volume) == (M0 + 5 * MINUTE, 10.8, 11.2, 10.3, 10.4, 500.0)
    # One input bar had no trade_count: the sum is unknown. The zero-volume bar has no vwap and is ignored.
    assert b.trade_count is None and b.absent == {"trade_count": "not_provided_by_source"}
    assert b.vwap == pytest.approx(5415 / 500)
    assert BAR_RULES in out.notes
    assert out.provenance.route == "duckdb:analytics_resample" and out.provenance.derived_from == [rid]


def test_bar_rules_text_is_stated_verbatim():
    assert BAR_RULES == (
        "Resample rules for bars: open = the first open by time; high = the max high; low = the min low; "
        "close = the last close by time; volume = the sum of volume; trade_count = the sum, None if any input "
        "bar has none; vwap = sum(vwap x volume) / sum(volume) over the bars with volume, None "
        "(insufficient_data) when that volume is 0 and None (not_provided_by_source) when a bar with volume has "
        "no vwap; timeframe = the target; t = the bucket start, UTC-aligned (weeks start Monday 00:00 UTC); a "
        "bar belongs to the bucket that contains its start."
    )


def test_vwap_rules_for_zero_volume_and_missing_vwap(store, ctx):
    zero = bars(
        "ZERO", [5.0, 5.0], start=M0, step=MINUTE, timeframe="1min", volumes=[0.0, 0.0], vwaps=[None, None]
    )
    gap = bars(
        "GAP", [5.0, 6.0], start=M0, step=MINUTE, timeframe="1min", volumes=[10.0, 10.0], vwaps=[5.0, None]
    )
    rid = store.put_rows(zero + gap)
    rows = {r.ticker: r for r in output_rows(run(SPEC, ctx, result_id=rid, timeframe="5min"), Bar)}
    assert rows["ZERO"].vwap is None and rows["ZERO"].absent == {"vwap": "insufficient_data"}
    assert rows["GAP"].vwap is None and rows["GAP"].absent == {"vwap": "not_provided_by_source"}


def test_daily_buckets_split_at_utc_midnight(store, ctx):
    times = [utc(2026, 1, 5, 23, 58), utc(2026, 1, 5, 23, 59), utc(2026, 1, 6, 0, 0), utc(2026, 1, 6, 0, 1)]
    rid = store.put_rows(
        bars("BTC/USD", [1.0, 2.0, 3.0, 4.0], times=times, timeframe="1min", asset_class="crypto")
    )
    rows = output_rows(run(SPEC, ctx, result_id=rid, timeframe="1d"), Bar)
    assert [(r.t, r.open, r.close, r.timeframe) for r in rows] == [
        (utc(2026, 1, 5), 1.0, 2.0, "1d"),
        (utc(2026, 1, 6), 3.0, 4.0, "1d"),
    ]
    assert {r.asset_class for r in rows} == {"crypto"}


def test_weekly_buckets_start_on_monday(store, ctx):
    times = [utc(2026, 1, 9, 21), utc(2026, 1, 12, 21), utc(2026, 1, 16, 21)]  # Fri, Mon, Fri
    rid = store.put_rows(bars("AAPL", [1.0, 2.0, 3.0], times=times))
    rows = output_rows(run(SPEC, ctx, result_id=rid, timeframe="1w"), Bar)
    assert [(r.t, r.open, r.close) for r in rows] == [
        (utc(2026, 1, 5), 1.0, 1.0),
        (utc(2026, 1, 12), 2.0, 3.0),
    ]


@pytest.mark.parametrize(
    ("source", "target", "code"),
    [
        ("1d", "1h", "timeframe_not_coarser"),
        ("1d", "1d", "timeframe_not_coarser"),
        ("5min", "7min", "timeframe_not_nested"),
    ],
)
def test_target_must_be_coarser_and_nest(store, ctx, source, target, code):
    rid = store.put_rows(bars("AAPL", [1.0, 2.0], timeframe=source, step=MINUTE * 5))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, timeframe=target)
    assert e.value.code == code and source in e.value.message and target in e.value.message


def test_mixed_timeframes_are_refused(store, ctx):
    rid = store.put_rows(bars("AAPL", [1.0, 2.0], timeframe="1min") + bars("MSFT", [1.0], timeframe="5min"))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, timeframe="1h")
    assert e.value.code == "mixed_timeframes" and "1min" in e.value.message and "5min" in e.value.message


def test_option_bars_resample_by_contract(store, ctx):
    occ = "AAPL260117C00150000"
    rows = [
        OptionBar(
            occ_symbol=occ,
            underlying="AAPL",
            timeframe="1min",
            t=M0 + i * MINUTE,
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1.0,
            trade_count=1,
            vwap=c,
        )
        for i, c in enumerate([1.0, 1.5, 1.2])
    ]
    rid = store.put_rows(rows, tool="options_bars")
    (r,) = output_rows(run(SPEC, ctx, result_id=rid, timeframe="1h"), OptionBar)
    assert (r.occ_symbol, r.underlying, r.open, r.high, r.low, r.close, r.volume, r.trade_count) == (
        occ,
        "AAPL",
        1.0,
        1.5,
        1.0,
        1.2,
        3.0,
        3,
    )
    assert r.vwap == pytest.approx((1.0 + 1.5 + 1.2) / 3)


TRADE_ABSENT = {
    "trade_id": "not_provided_by_source",
    "conditions": "not_provided_by_source",
    "tape": "not_provided_by_source",
    "taker_side": "not_applicable",
}


def trades():
    def tr(sec, price, size, exch):
        return Trade(
            ticker="AAPL",
            asset_class="us_equity",
            t=M0 + sec * MINUTE / 60,
            price=price,
            size=size,
            exchange=exch,
            absent=TRADE_ABSENT,
        )

    return [tr(5, 10.0, 100.0, "V"), tr(40, 10.2, 50.0, "Q"), tr(70, 10.1, 25.0, "V")]


@pytest.mark.parametrize(
    ("agg", "first_bucket"),
    [
        ("last", (10.2, 50.0)),
        ("first", (10.0, 100.0)),
        ("mean", (10.1, 75.0)),
        ("sum", (20.2, 150.0)),
        ("min", (10.0, 50.0)),
        ("max", (10.2, 100.0)),
    ],
)
def test_non_bar_inputs_aggregate_every_value_column(store, ctx, agg, first_bucket):
    rid = store.put_rows(trades(), tool="market_trades")
    out = run(SPEC, ctx, result_id=rid, timeframe="1min", agg=agg)
    rows = output_rows(out, Trade)
    assert out.model is Trade
    assert [r.t for r in rows] == [M0, M0 + MINUTE]
    assert (rows[0].price, rows[0].size) == pytest.approx(first_bucket)
    assert (rows[1].price, rows[1].size) == (10.1, 25.0)
    # Other columns come from the bucket's last row, with that row's absence reasons.
    assert [r.exchange for r in rows] == ["Q", "V"]
    assert [r.absent for r in rows] == [TRADE_ABSENT] * 2
    assert any(f"price, size aggregated with {agg}" in n for n in out.notes)


def test_dynamic_input_with_an_empty_bucket_value(store, ctx):
    table = pa.table(
        {
            "t": pa.array(
                [M0 + 10 * MINUTE / 60, M0 + MINUTE + 10 * MINUTE / 60], pa.timestamp("us", tz="UTC")
            ),
            "px": [None, 5.0],
            "note": ["a", "b"],
        }
    )
    rid = store.put_dynamic(table)
    out = run(SPEC, ctx, result_id=rid, timeframe="1min")
    assert out.model == "marketlens.QueryRow"
    rows = out.table.to_pylist()
    assert [(r["t"], r["px"], r["note"]) for r in rows] == [(M0, None, "a"), (M0 + MINUTE, 5.0, "b")]
    assert out.absent["px"].code == "no_data"
    assert unexplained_dynamic(rows, out.absent) == []


def test_large_dynamic_output_is_stored_with_its_time_column(store, ctx):
    n = 600
    table = pa.table(
        {
            "sym": ["X"] * n,
            "t": pa.array([M0 + i * MINUTE / 2 for i in range(n)], pa.timestamp("us", tz="UTC")),
            "px": [float(i) for i in range(n)],
        }
    )
    rid = store.put_dynamic(table, group_column="sym")
    out = run(SPEC, ctx, result_id=rid, timeframe="1min", agg="max")
    assert out.table is None and out.stored is not None
    info = out.stored
    assert (info.row_count, info.time_column, info.group_column, info.model) == (
        300,
        "t",
        "sym",
        "marketlens.QueryRow",
    )
    assert info.parents == [rid] and info.provenance.derived_from == [rid]
    assert store.tables[info.result_id].column("px").to_pylist()[:2] == [1.0, 3.0]


def test_portfolio_history_keeps_exact_decimals(store, ctx):
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

    rid = store.put_rows(
        [point(5, "100.25"), point(6, "101.50"), point(12, "99.125")], tool="portfolio_history"
    )
    rows = output_rows(run(SPEC, ctx, result_id=rid, timeframe="1w"), PortfolioHistoryPoint)
    assert [(r.t, r.equity, r.timeframe) for r in rows] == [
        (utc(2026, 1, 5), Decimal("101.5"), "1w"),
        (utc(2026, 1, 12), Decimal("99.125"), "1w"),
    ]


def test_non_time_series_is_refused(store, ctx):
    table = pa.table({"x": [1.0]})
    rid = store.put_dynamic(table)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, timeframe="1d")
    assert e.value.code == "not_time_series"


def test_single_bar_and_empty_buckets(store, ctx):
    times = [M0, M0 + 2 * MINUTE * 60]  # 13:30 and 15:30: the 14:00 and 15:00 hours are empty
    rid = store.put_rows(bars("AAPL", [1.0, 2.0], times=times, timeframe="1min"))
    rows = output_rows(run(SPEC, ctx, result_id=rid, timeframe="1h"), Bar)
    assert [(r.t, r.close) for r in rows] == [(utc(2026, 1, 5, 13), 1.0), (utc(2026, 1, 5, 15), 2.0)]
    one = store.put_rows(bars("AAPL", [7.0], start=M0, timeframe="1min"))
    (r,) = output_rows(run(SPEC, ctx, result_id=one, timeframe="1d"), Bar)
    assert (r.t, r.open, r.close, r.volume) == (utc(2026, 1, 5), 7.0, 7.0, 1000.0)
