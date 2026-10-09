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

from marketlens_mcp.analytics.tools.align import SPEC as ALIGN
from marketlens_mcp.analytics.tools.drawdown import SPEC as DRAWDOWN
from marketlens_mcp.analytics.tools.returns import SPEC as RETURNS
from marketlens_mcp.analytics.tools.volatility import SPEC
from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.analytics import VolatilityPoint
from marketlens_schema.market import OptionBar

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


#: 09:30 New York (EST) on a Monday: intraday fixtures inside the regular session.
OPEN_BELL = utc(2026, 1, 5, 14, 30)


def test_on_a_returns_result_uses_its_kind_and_the_parent_timeframe(store, ctx):
    prices = store.put_rows(bars("AAPL", CLOSES, timeframe="1h", start=OPEN_BELL, step=60 * MINUTE))
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
    rid = store.put_rows(bars("AAPL", CLOSES, timeframe=timeframe, start=OPEN_BELL, step=step))
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


def ppy_of(out):
    return {r.periods_per_year for r in output_rows(out, VolatilityPoint)}


@pytest.mark.parametrize(
    ("timeframe", "step", "ppy"),
    [("1d", DAY, 365), ("1h", 60 * MINUTE, 8760), ("15min", 15 * MINUTE, 35040), ("1w", 7 * DAY, 52)],
)
def test_crypto_trades_every_day(store, ctx, timeframe, step, ppy):
    """Daily crypto bars were annualised with 252 (ETH/USD 0.376 where 365 gives
    0.453) and only a note said so: a crypto result defaults to 365 days, 24 hours each."""
    rid = store.put_rows(bars("ETH/USD", CLOSES, timeframe=timeframe, step=step, asset_class="crypto"))
    out = run(SPEC, ctx, result_id=rid, window=2)
    assert ppy_of(out) == {float(ppy)}
    if timeframe != "1w":
        assert any(f"periods_per_year = {ppy}" in n and "crypto trades every day" in n for n in out.notes)


def test_crypto_returns_and_query_answers_are_crypto_too(store, ctx):
    prices = store.put_rows(bars("BTC/USD", CLOSES, asset_class="crypto"))
    rets = store.keep(run(RETURNS, ctx, result_id=prices), "analytics_returns")
    assert ppy_of(run(SPEC, ctx, result_id=rets, window=2)) == {365.0}
    # A query answer with no asset_class column: series named BASE/QUOTE are pairs.
    t0 = utc(2026, 3, 2)
    table = pa.table(
        {
            "t": pa.array([t0 + i * DAY for i in range(6)], pa.timestamp("us", tz="UTC")),
            "ticker": ["SOL/USD"] * 6,
            "timeframe": ["1d"] * 6,
            "close": [float(c) for c in CLOSES],
        }
    )
    rid = store.put_dynamic(table, time_column="t", group_column="ticker")
    assert ppy_of(run(SPEC, ctx, result_id=rid, window=2, price_column="close")) == {365.0}


def test_crypto_mixed_with_stocks_needs_periods_per_year(store, ctx):
    rid = store.put_rows(bars("AAPL", CLOSES) + bars("ETH/USD", CLOSES, asset_class="crypto"))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=2)
    assert e.value.code == "periods_per_year_required" and "mixes crypto" in e.value.message
    assert ppy_of(run(SPEC, ctx, result_id=rid, window=2, periods_per_year=300)) == {300.0}


#: 04:00 New York (EST) on a Monday: the start of the pre-market session.
PRE_MARKET = utc(2026, 1, 5, 9)


def day_times(per_day, *, start, step=15 * MINUTE, days=3, skip=()):
    """``per_day`` consecutive bar times a day from ``start``, on ``days`` days, but the ``skip`` ones."""
    return [start + d * DAY + i * step for d in range(days) for i in range(per_day) if i not in skip]


def day_bars(ticker, per_day, *, start, step=15 * MINUTE, timeframe="15min", skip=()):
    times = day_times(per_day, start=start, step=step, skip=skip)
    return bars(ticker, [100.0 + (i * 7) % 5 for i in range(len(times))], times=times, timeframe=timeframe)


def test_extended_hours_bars_count_the_bars_of_a_session(store, ctx):
    """SPY 15min bars from 04:00 to 20:00 New York were annualised as 26 bars a
    session (98280/15), understating the volatility about 1.56 times: bars that
    include extended hours count the bars of the median New York day, here 64."""
    rid = store.put_rows(day_bars("SPY", 64, start=PRE_MARKET))
    out = run(SPEC, ctx, result_id=rid, window=2)
    assert ppy_of(out) == {252 * 64.0}
    assert any(
        "periods_per_year = 16128" in n
        and "extended-hours bars" in n
        and "median 64 bars a New York day" in n
        for n in out.notes
    )
    regular = store.put_rows(bars("SPY", CLOSES, timeframe="15min", start=OPEN_BELL, step=15 * MINUTE))
    out = run(SPEC, ctx, result_id=regular, window=2)
    assert ppy_of(out) == {252 * 26.0}
    assert any("6.5 hours, 09:30-16:00 New York" in n for n in out.notes)
    # An hourly bar that starts at 09:00 overlaps the session: not extended.
    nine = store.put_rows(
        bars("SPY", CLOSES, timeframe="1h", start=OPEN_BELL - 30 * MINUTE, step=60 * MINUTE)
    )
    assert ppy_of(run(SPEC, ctx, result_id=nine, window=2)) == {1638.0}


@pytest.mark.parametrize(("per_day", "ppy"), [(30, 7560.0), (27, 6804.0)])
def test_a_few_after_hours_bars_are_not_a_sixteen_hour_session(store, ctx, per_day, ppy):
    """One bar at or after 16:00 New York made every session 16 hours: CROX
    15min bars, 26 regular and a few after hours a day, were annualised with
    16128 instead of about 252 x 30, overstating volatility about 1.46 times,
    and a single 16:00 bar a day (27) did the same."""
    rid = store.put_rows(day_bars("CROX", per_day, start=OPEN_BELL))
    out = run(SPEC, ctx, result_id=rid, window=2)
    assert ppy_of(out) == {ppy}
    assert any(f"median {per_day} bars a New York day" in n for n in out.notes)


def test_option_bars_to_16_15_count_their_bars(store, ctx):
    """SPY options trade until 16:15 New York: their 16:00 bar is real trading,
    counted as the 27th of the session, not a sign of 16-hour sessions."""
    occ = "SPY260116C00600000"
    rows = [
        OptionBar(
            occ_symbol=occ,
            underlying="SPY",
            timeframe="15min",
            t=t,
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1.0,
            trade_count=1,
            vwap=c,
        )
        for t, c in zip(
            day_times(27, start=OPEN_BELL), [5.0 + (i * 3) % 4 / 10 for i in range(81)], strict=True
        )
    ]
    rid = store.put_rows(rows, tool="options_bars")
    assert ppy_of(run(SPEC, ctx, result_id=rid, window=2)) == {252 * 27.0}


def test_counted_sessions_are_never_shorter_than_the_regular_one(store, ctx):
    """Counting cannot fall below 6.5-hour sessions: 1min bars with regular
    minutes missing (295 bars a day, 3 of them after hours) keep 98280/1, as
    without the after-hours bars; and six pre-market bars of one morning, too
    few to count a session, keep the regular 98280/15."""
    sparse = day_bars("CROX", 393, start=OPEN_BELL, step=MINUTE, timeframe="1min", skip=set(range(0, 390, 4)))
    rid = store.put_rows(sparse)
    assert ppy_of(run(SPEC, ctx, result_id=rid, window=2)) == {98280.0}
    morning = store.put_rows(bars("SPY", CLOSES, timeframe="15min", start=PRE_MARKET, step=15 * MINUTE))
    out = run(SPEC, ctx, result_id=morning, window=2)
    assert ppy_of(out) == {6552.0}
    assert any(
        "at least the 6552 of 6.5-hour sessions and at most the 16128 of 16 hours" in n for n in out.notes
    )


def test_a_weekly_sample_of_daily_bars_is_annualised_weekly(store, ctx):
    """A results_query of Fridays from daily bars keeps timeframe 1d, so
    periods_per_year was 252 for weekly returns: observations much further
    apart than the timeframe are counted per year instead, with a note."""
    friday = utc(2026, 1, 9, 21)
    closes = [100 * (1 + 0.01 * ((i * 7) % 5 - 2)) ** i for i in range(30)]
    rid = store.put_rows(bars("AAPL", closes, start=friday, step=7 * DAY))
    out = run(SPEC, ctx, result_id=rid, window=4)
    assert ppy_of(out) == {52.0}
    assert [r.volatility for r in output_rows(out, VolatilityPoint)] == pytest.approx(
        rolling(logr(closes), 4, 52), rel=1e-12
    )
    assert any("labelled 1d" in n and "a median 7 days apart" in n and "52" in n for n in out.notes)


def test_sparse_intraday_bars_count_their_observations_a_day(store, ctx):
    times = [OPEN_BELL + d * DAY + i * 5 * MINUTE for d in range(3) for i in range(10)]
    closes = [100 + (i % 4) for i in range(30)]
    rid = store.put_rows(bars("AAPL", closes, times=times, timeframe="1min"))
    out = run(SPEC, ctx, result_id=rid, window=5)
    # 30 bars, 10 a day: 29 returns, 10 on the median New York day (9 on the first).
    assert ppy_of(out) == {2520.0}
    assert any("labelled 1min" in n and "5 minutes apart" in n and "252 sessions" in n for n in out.notes)


def test_a_weekly_or_monthly_sample_of_intraday_bars_is_counted_over_its_span(store, ctx):
    """One 1h bar a Friday (a results_query filter keeps timeframe 1h) was
    annualised as 252 sessions x 1 bar a New York day, overstating volatility
    2.2 times: observations more than a day apart are counted per year over
    their span, whatever the timeframe says."""
    friday = utc(2026, 1, 9, 19)
    for timeframe, step, n, ppy in (("1h", 7 * DAY, 20, 52.0), ("15min", 30 * DAY, 12, 12.0)):
        closes = [100 * (1 + 0.01 * ((i * 7) % 5 - 2)) ** i for i in range(n)]
        rid = store.put_rows(bars("AAPL", closes, start=friday, step=step, timeframe=timeframe))
        out = run(SPEC, ctx, result_id=rid, window="all")
        assert ppy_of(out) == {ppy}, timeframe
        assert any(
            f"labelled {timeframe}" in note and "their number per year over the time they span" in note
            for note in out.notes
        )
    # A weekly query answer with no timeframe column takes the 15min of its bars, and is weekly all the same.
    prices = store.put_rows(day_bars("AAPL", 26, start=OPEN_BELL))
    weekly = store.put(
        pa.table(
            {
                "t": pa.array([friday + i * 7 * DAY for i in range(10)], pa.timestamp("us", tz="UTC")),
                "series": ["AAPL"] * 10,
                "close": [100.0 + (i * 7) % 5 for i in range(10)],
            }
        ),
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=source_provenance(route="duckdb:results_query"),
        time_column="t",
        group_column="series",
        column_units={"close": "price"},
        parents=[prices],
    ).result_id
    out = run(SPEC, ctx, result_id=weekly, window="all")
    assert ppy_of(out) == {52.0}
    assert any(
        f"{weekly} was computed from 15min results" in n and "a median 7 days apart" in n for n in out.notes
    )


def test_a_short_daily_sample_over_a_weekend_stays_daily(store, ctx):
    times = [utc(2026, 1, 8, 21), utc(2026, 1, 9, 21), utc(2026, 1, 12, 21)]
    rid = store.put_rows(bars("AAPL", [100, 101, 99], times=times))
    assert ppy_of(run(SPEC, ctx, result_id=rid, window=2)) == {252.0}


def test_without_a_timeframe_the_refusal_says_how_far_apart_the_observations_are(store, ctx):
    t0 = utc(2026, 3, 2)
    table = pa.table(
        {
            "t": pa.array([t0 + i * 7 * DAY for i in range(4)], pa.timestamp("us", tz="UTC")),
            "px": [1.0, 1.1, 1.0, 1.2],
        }
    )
    rid = store.put_dynamic(table)
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=2, price_column="px")
    assert e.value.code == "periods_per_year_required" and "a median 7 days apart" in e.value.message
    assert "too few to count per year (2 intervals between them, at least 5)" in e.value.message


def test_window_all_is_the_full_sample_volatility_of_each_series(store, ctx):
    """252 bars and window=250 gave two rows a series and no full-sample mode:
    window=all gives one row a series over all its returns."""
    msft = [10, 11, 12]
    rid = store.put_rows(bars("AAPL", CLOSES) + bars("MSFT", msft) + bars("NVDA", [5, 6]))
    out = run(SPEC, ctx, result_id=rid, window="all", return_kind="simple")
    rows = output_rows(out, VolatilityPoint)
    assert [(r.series, r.window, r.t) for r in rows] == [
        ("AAPL", 5, utc(2026, 1, 5, 21) + 5 * DAY),
        ("MSFT", 2, utc(2026, 1, 5, 21) + 2 * DAY),
    ]
    assert rows[0].volatility == pytest.approx(statistics.stdev(simple(CLOSES)) * math.sqrt(252), rel=1e-12)
    assert rows[1].volatility == pytest.approx(statistics.stdev(simple(msft)) * math.sqrt(252), rel=1e-12)
    assert any("window=all" in n and "all simple returns" in n for n in out.notes)
    assert any("fewer than 2 returns are omitted: NVDA" in n for n in out.notes)
    single = store.put_rows(bars("AAPL", [100, 101]))
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=single, window="all")
    assert e.value.code == "too_few_points" and "window=all" in e.value.message


def test_store_keeps_a_small_output(store, ctx):
    rid = store.put_rows(bars("AAPL", CLOSES))
    assert run(SPEC, ctx, result_id=rid, window=3).offload == "auto"
    assert run(SPEC, ctx, result_id=rid, window=3, store=True).offload == "always"


def query_returns(store, rets, units=None, times=None, parents=(), series="PORT"):
    t0 = utc(2026, 3, 2)
    table = pa.table(
        {
            "t": pa.array(times or [t0 + i * DAY for i in range(len(rets))], pa.timestamp("us", tz="UTC")),
            "series": [series] * len(rets),
            "ret": rets,
        }
    )
    return store.put(
        table,
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=source_provenance(route="duckdb:results_query"),
        time_column="t",
        group_column="series",
        column_units=units,
        parents=parents,
    ).result_id


RETS = [0.01, -0.02, 0.015, -0.005, 0.02]


def test_a_column_of_returns_is_used_as_it_is(store, ctx):
    """A portfolio's returns from results_query had no way in: read as prices,
    the negative ones were skipped and the rest turned into returns of returns.
    input_kind=returns takes them as they are; a column in fraction units is
    taken as returns by default."""
    plain = query_returns(store, RETS)
    out = run(SPEC, ctx, result_id=plain, window="all", input_kind="returns", periods_per_year=252)
    (row,) = output_rows(out, VolatilityPoint)
    assert row.volatility == pytest.approx(statistics.stdev(RETS) * math.sqrt(252), rel=1e-12)
    assert row.window == 5
    assert any("input_kind=returns" in n and "taken as returns as they are" in n for n in out.notes)
    as_prices = run(SPEC, ctx, result_id=plain, window=2, periods_per_year=252, price_column="ret")
    assert any("<= 0" in n and "pass input_kind=returns" in n for n in as_prices.notes)
    labelled = query_returns(store, RETS, units={"ret": "fraction"})
    out = run(SPEC, ctx, result_id=labelled, window="all", periods_per_year=252, price_column="ret")
    assert output_rows(out, VolatilityPoint)[0].volatility == pytest.approx(row.volatility, rel=1e-12)
    assert any("ret is in fraction units" in n for n in out.notes)


def test_a_volatility_column_is_not_a_price(store, ctx):
    rid = query_returns(store, [0.2, 0.21, 0.19, 0.2, 0.22], units={"ret": "fraction_per_year"})
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=rid, window=2, periods_per_year=252, price_column="ret")
    assert e.value.code == "not_prices" and "input_kind=returns" in e.value.message


def test_a_drawdown_column_is_not_taken_as_returns(store, ctx):
    """A stored drawdown series (unit fraction) was read as returns with only a
    note: a ratio column of a model other than returns, or a query answer's
    copy of it, is refused unless input_kind=returns says it holds returns."""
    prices = store.put_rows(bars("AAPL", CLOSES))
    dd = store.keep(run(DRAWDOWN, ctx, result_id=prices, mode="series"), "analytics_drawdown")
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=dd, window=2, periods_per_year=252)
    assert e.value.code == "not_prices" and "input_kind=returns" in e.value.message
    kept = store.put(
        store.tables[dd].select(["t", "series", "drawdown"]),
        tool="results_query",
        model="marketlens.QueryRow",
        provenance=source_provenance(route="duckdb:results_query"),
        time_column="t",
        group_column="series",
        column_units={"drawdown": "fraction"},
        parents=[dd],
    ).result_id
    with pytest.raises(ToolError) as e:
        run(SPEC, ctx, result_id=kept, window=2, periods_per_year=252, price_column="drawdown")
    assert e.value.code == "not_prices"
    out = run(SPEC, ctx, result_id=dd, window=2, periods_per_year=252, input_kind="returns")
    assert any("input_kind=returns" in n and "taken as returns" in n for n in out.notes)


def test_a_drawdown_column_through_align_or_a_query_of_a_query_is_not_taken_as_returns(store, ctx):
    """A drawdown column keeps its fraction unit through analytics_align and
    through a query of a query, and was read as returns there: a column is
    followed back to the result that computed it (an aligned result's right
    column without its suffix)."""
    aapl, spy = store.put_rows(bars("AAPL", CLOSES)), store.put_rows(bars("SPY", CLOSES[::-1]))
    dd, spy_dd = (
        store.keep(run(DRAWDOWN, ctx, result_id=p, mode="series"), "analytics_drawdown") for p in (aapl, spy)
    )

    def aligned(left, right):
        return run(
            ALIGN, ctx, left_result_id=left, right_result_id=right, tolerance="PT0S", store=True
        ).stored

    def query(parent):
        return store.put(
            store.tables[dd].select(["t", "series", "drawdown"]),
            tool="results_query",
            model="marketlens.QueryRow",
            provenance=source_provenance(route="duckdb:results_query"),
            time_column="t",
            group_column="series",
            column_units={"drawdown": "fraction"},
            parents=[parent],
        ).result_id

    for rid, col in (
        (aligned(spy, dd).result_id, "drawdown"),
        (aligned(dd, spy_dd).result_id, "drawdown_right"),
        (query(query(dd)), "drawdown"),
    ):
        with pytest.raises(ToolError) as e:
            run(SPEC, ctx, result_id=rid, window=2, periods_per_year=252, price_column=col)
        assert e.value.code == "not_prices" and "input_kind=returns" in e.value.message, col
    # Returns stay returns: a right column renamed ret_right is its returns result's ret.
    rets, spy_rets = (store.keep(run(RETURNS, ctx, result_id=p), "analytics_returns") for p in (aapl, spy))
    both = aligned(rets, spy_rets)
    assert {c.name: c.unit for c in both.columns}["ret_right"] == "fraction"
    out = run(SPEC, ctx, result_id=both.result_id, window=2, periods_per_year=252, price_column="ret_right")
    assert any("ret_right is in fraction units" in n and "taken as returns" in n for n in out.notes)


#: Weekdays of four weeks from Monday 2 March 2026, at the close.
WEEKDAYS = [utc(2026, 3, 2, 21) + d * DAY for d in range(26) if d % 7 < 5]


def test_without_a_timeframe_the_observations_are_counted(store, ctx):
    """A portfolio's daily returns from results_query had no timeframe, so
    periods_per_year was required: with at least 5 returns they are counted.
    A day apart or less, per New York day (UTC day for crypto) x 252 sessions
    (365 days): ten weekdays from a Monday were 299 a year over their calendar
    span, which leaves out the weekends at its ends. Further apart, their
    number per year over the time they span."""
    for days in (WEEKDAYS, WEEKDAYS[:10]):
        rets = [0.01 * ((i * 7) % 5 - 2) for i in range(len(days))]
        rid = query_returns(store, rets, times=days)
        out = run(SPEC, ctx, result_id=rid, window="all", input_kind="returns")
        (row,) = output_rows(out, VolatilityPoint)
        assert row.periods_per_year == 252.0, len(days)
        assert row.volatility == pytest.approx(statistics.stdev(rets) * math.sqrt(252), rel=1e-12)
        assert any(
            f"periods_per_year = 252: {rid} has no bar timeframe, and its observations are a median 1 day apart"
            in n
            and "252 sessions a year x the median 1 observation a New York day" in n
            for n in out.notes
        )
    every_day = [utc(2026, 3, 2) + i * DAY for i in range(10)]
    crypto = query_returns(store, RETS * 2, times=every_day, series="BTC/USD")
    assert ppy_of(run(SPEC, ctx, result_id=crypto, window="all", input_kind="returns")) == {365.0}
    fridays = [utc(2026, 3, 6, 21) + i * 7 * DAY for i in range(8)]
    weekly = query_returns(store, [0.01 * ((i * 7) % 5 - 2) for i in range(8)], times=fridays)
    out = run(SPEC, ctx, result_id=weekly, window="all", input_kind="returns")
    assert ppy_of(out) == {float(round(7 / 49 * 365.25))}  # 7 intervals over 49 days: 52
    assert any("their number per year over the time they span" in n for n in out.notes)


def test_a_query_answer_takes_the_timeframe_it_was_computed_from(store, ctx):
    """A query answer over a returns result (a portfolio, or a filter) has no
    timeframe column: it takes the 1d of the bars the returns came from, and
    their asset class."""
    prices = store.put_rows(
        bars("AAPL", [100.0 + (i * 7) % 5 for i in range(21)], times=[*WEEKDAYS, WEEKDAYS[-1] + 3 * DAY])
    )
    rets = store.keep(run(RETURNS, ctx, result_id=prices), "analytics_returns")
    port = query_returns(
        store,
        [0.01 * ((i * 7) % 5 - 2) for i in range(20)],
        times=WEEKDAYS[1:] + [WEEKDAYS[-1] + 3 * DAY],
        units={"ret": "fraction"},
        parents=[rets],
    )
    out = run(SPEC, ctx, result_id=port, window="all")
    assert ppy_of(out) == {252.0}
    assert any(
        f"from the 1d timeframe of the results {port} was computed from (US equity" in n for n in out.notes
    )
    crypto = store.put_rows(bars("BTC/USD", CLOSES, asset_class="crypto"))
    crets = store.keep(run(RETURNS, ctx, result_id=crypto), "analytics_returns")
    cport = query_returns(
        store, [0.01, -0.02, 0.015, -0.005, 0.02], units={"ret": "fraction"}, parents=[crets]
    )
    assert ppy_of(run(SPEC, ctx, result_id=cport, window="all")) == {365.0}
