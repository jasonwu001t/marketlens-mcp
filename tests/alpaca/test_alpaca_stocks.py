"""Stock tools: market_bars, market_quotes, market_trades, market_latest_bars,
market_latest_quotes, market_latest_trades, market_snapshots, market_most_active,
market_movers (golden outputs + the behaviours behind them)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl

import httpx
import pytest
from alpaca_harness import NOW, FakeContext, call, check_golden
from pydantic import ValidationError

from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_mcp.providers.alpaca.convert import iso_z
from marketlens_schema.base import unexplained_absences

CURSOR = "QUFQTHxEfDIwMjYtMTAtMDE="  # the synthetic next-page cursor in StockBars__page1


def asset_names(alpaca, names: dict[str, str] | None = None):
    """The active-asset list market_movers reads names from (synthetic, trimmed to what a test needs)."""
    names = names or {
        "WORX": "SharpLink Gaming Ltd. Ordinary Shares",
        "BULG": "Bulgari Holdings Inc. Common Stock",
    }
    return alpaca.add(
        "/v2/assets",
        [
            {"symbol": sym, "name": name, "class": "us_equity", "status": "active"}
            for sym, name in names.items()
        ],
    )


def test_market_bars_golden_two_pages_and_brk_b(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__page2", match={"page_token": CURSOR})
    alpaca.fixture("/v2/stocks/bars", "StockBars__page1")
    out = call(specs["market_bars"], ctx, tickers=["aapl", "brk.b"])
    doc = check_golden(out, "market_bars")
    first = alpaca.params(0)
    assert first == {
        "symbols": "AAPL,BRK.B",
        "timeframe": "1Day",
        "start": "2025-10-02T00:00:00Z",  # P1Y back from now, floored to the UTC day
        "adjustment": "all",
        "feed": "iex",
        "sort": "asc",
        "limit": "10000",
    }
    assert alpaca.params(1)["page_token"] == CURSOR
    assert [r["ticker"] for r in doc["rows"]] == ["AAPL", "AAPL", "BRK-B", "BRK-B"]
    assert doc["rows"][3]["absent"] == {
        "trade_count": "not_provided_by_source",
        "vwap": "not_provided_by_source",
    }
    assert doc["pagination"]["complete"] and doc["pagination"]["pages_fetched"] == 2


def test_market_bars_intraday_default_lookback_is_five_days(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    out = call(
        specs["market_bars"],
        ctx,
        tickers=["MSFT"],
        timeframe="1h",
        end="2026-10-02T20:00:00Z",
        adjustment="raw",
    )
    check_golden(out, "market_bars__intraday")
    assert alpaca.params() == {
        "symbols": "MSFT",
        "timeframe": "1Hour",
        "start": "2026-09-27T20:00:00Z",
        "end": "2026-10-02T20:00:00Z",
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
        "limit": "10000",
    }


def test_market_bars_row_cap_stops_with_the_r16_note(specs, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__page1")
    ctx = FakeContext(limits=FetchLimits(max_rows=2, max_pages=20))
    out = call(specs["market_bars"], ctx, tickers=["AAPL", "BRK-B"])
    check_golden(out, "market_bars__row_cap")
    note = (
        "Stopped after 1 pages and 2 rows (limits fetch.max_pages=20, fetch.max_rows=2). The data is incomplete; "
        f'call market_bars again with page_token="{CURSOR}" to continue.'
    )
    assert out.notes[0] == note and out.provenance.truncation_note == note and out.provenance.truncated
    assert out.pagination.row_cap_hit and not out.pagination.complete
    assert alpaca.params()["limit"] == "2"


def test_market_bars_page_cap(specs, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__page1")
    ctx = FakeContext(limits=FetchLimits(max_rows=50_000, max_pages=1))
    out = call(specs["market_bars"], ctx, tickers=["AAPL"])
    assert out.pagination.page_cap_hit and out.pagination.next_page_token == CURSOR
    assert "fetch.max_pages=1" in out.notes[0]


def test_market_bars_continues_from_a_page_token(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__page2")
    call(specs["market_bars"], ctx, tickers=["BRK-B"], page_token=CURSOR)
    assert alpaca.params(0)["page_token"] == CURSOR


def test_delayed_sip_is_sent_as_sip_on_history_and_marked_delayed(specs, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    out = call(specs["market_bars"], ctx, tickers=["MSFT"])
    assert alpaca.params()["feed"] == "sip"
    assert (out.provenance.feed, out.provenance.delay) == ("sip", "delayed")


@pytest.mark.parametrize(
    ("timeframe", "start"),
    [
        ("1d", "2025-10-07T00:00:00Z"),
        ("1w", "2025-10-07T00:00:00Z"),
        ("1mo", "2025-10-07T00:00:00Z"),
        ("1h", "2025-10-07T08:31:05Z"),
    ],
)
def test_a_daily_lookback_starts_at_the_first_days_utc_midnight(specs, alpaca, timeframe, start):
    # The 2025-10-07 daily bar is stamped 04:00Z (midnight New York): a start at 08:31:05Z would drop it.
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    call(
        specs["market_bars"],
        FakeContext(fixed_now=datetime(2026, 10, 8, tzinfo=UTC)),
        tickers=["MSFT"],
        timeframe=timeframe,
        lookback="P1Y",
        end="2026-10-07T04:31:05-04:00",
    )
    assert alpaca.params(0)["start"] == start


@pytest.mark.parametrize(
    ("tool", "path", "fixture"),
    [
        ("market_bars", "/v2/stocks/bars", "StockBars__intraday"),
        ("market_quotes", "/v2/stocks/quotes", "StockQuotes"),
        ("market_trades", "/v2/stocks/trades", "StockTrades"),
    ],
)
def test_a_date_end_includes_that_new_york_day(specs, ctx, alpaca, tool, path, fixture):
    # A daily bar is stamped at midnight New York (04:00Z): 00:00Z of the end date would drop that day's bar.
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, tickers=["MSFT"], start="2026-09-28", end="2026-10-01")
    assert alpaca.params(0)["end"] == "2026-10-02T03:59:59.999999Z"
    assert out.provenance.request["end"] == "2026-10-02T03:59:59.999999Z"
    assert out.provenance.as_of == datetime(2026, 10, 2, 3, 59, 59, 999999, tzinfo=UTC)
    assert out.notes[0] == (
        "end 2026-10-01 is a date: the window includes that whole day, to midnight New York time "
        "(2026-10-02T03:59:59.999999Z)."
    )


def test_a_date_end_alone_with_the_same_start_is_one_day(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    call(specs["market_bars"], ctx, tickers=["MSFT"], start="2026-10-01", end="2026-10-01")
    assert (alpaca.params(0)["start"], alpaca.params(0)["end"]) == (
        "2026-10-01T00:00:00Z",
        "2026-10-02T03:59:59.999999Z",
    )


@pytest.mark.parametrize(
    ("tool", "path", "fixture"),
    [
        ("market_bars", "/v2/stocks/bars", "StockBars__intraday"),
        ("market_quotes", "/v2/stocks/quotes", "StockQuotes"),
        ("market_trades", "/v2/stocks/trades", "StockTrades"),
    ],
)
@pytest.mark.parametrize("end", ["2026-10-10", "2026-10-02T19:50:00Z"])
def test_an_end_past_the_delayed_sip_feeds_reach_is_cut_with_a_note(specs, alpaca, tool, path, fixture, end):
    # A plan without real-time SIP is refused the last 15 minutes ("recent SIP data", HTTP 403).
    alpaca.fixture(path, fixture)
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    out = call(specs[tool], ctx, tickers=["MSFT"], start="2026-10-02", end=end)
    assert alpaca.params()["end"] == "2026-10-02T19:44:00Z"  # NOW - 16 minutes
    assert out.provenance.as_of == datetime(2026, 10, 2, 19, 44, tzinfo=UTC)
    assert out.notes[0] == (
        f"end {end} is later than the newest data the delayed SIP feed serves (16 minutes before now): the "
        "window ends at 2026-10-02T19:44:00Z."
    )


def test_an_end_within_the_delayed_sip_feeds_reach_is_kept(specs, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    out = call(specs["market_bars"], ctx, tickers=["MSFT"], timeframe="1h", end="2026-10-02T19:40:00Z")
    assert alpaca.params()["end"] == "2026-10-02T19:40:00Z" and out.notes == []


def test_without_an_end_the_delayed_sip_feed_reaches_back_to_its_lag(specs, alpaca):
    alpaca.fixture("/v2/stocks/trades", "StockTrades")
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    out = call(specs["market_trades"], ctx, tickers=["BRK-B"])
    assert "end" not in alpaca.params()  # Alpaca's own default end
    assert out.provenance.as_of == datetime(2026, 10, 2, 19, 44, tzinfo=UTC)
    assert out.notes[0] == (
        "the default lookback PT20M reaches back from the newest data the delayed SIP feed serves (16 minutes "
        "before now): the window starts at 2026-10-02T19:24:00Z."
    )


def test_a_future_end_on_a_real_time_feed_is_cut_to_now(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    out = call(specs["market_bars"], ctx, tickers=["MSFT"], start="2026-10-01", end="2026-10-10")
    assert alpaca.params()["end"] == "2026-10-02T20:00:00Z"
    assert out.provenance.as_of == NOW
    assert out.notes[0] == "end 2026-10-10 is in the future: the window ends now, 2026-10-02T20:00:00Z."


def _bar(t: str, o: float, h: float, low: float, c: float, v: float, n: int | None = 100) -> dict:
    bar = {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v, "vw": round((h + low + c) / 3, 4)}
    return bar if n is None else {**bar, "n": n}


def _bars(by_symbol: dict[str, list[dict]]) -> dict:
    return {"bars": by_symbol, "next_page_token": None}


def _trades(*symbols: str, token: str | None = None) -> dict:
    """Alpaca's answer to the trades asked for after end: one trade of each of ``symbols``."""
    trade = {"t": "2026-10-09T13:30:00.1Z", "x": "V", "p": 1.0, "s": 1, "c": ["@"], "i": 1, "z": "C"}
    return {"trades": {s: [trade] for s in symbols}, "next_page_token": token}


AFTER_THE_WEEK = datetime(2026, 10, 16, 20, tzinfo=UTC)
#: A week's bar as Alpaca serves it for end 2026-10-08: whole, with Friday 10-09's trades (close 111).
WEEK = _bar("2026-10-05T04:00:00Z", 100, 112, 95, 111, 5000, 50)
#: The days of that week up to end, Monday to Thursday.
DAYS = [
    _bar("2026-10-05T04:00:00Z", 100, 104, 99, 103, 1000, 10),
    _bar("2026-10-06T04:00:00Z", 103, 105, 95, 96, 1200, 12),
    _bar("2026-10-07T04:00:00Z", 96, 101, 96, 100, 900, 9),
    _bar("2026-10-08T04:00:00Z", 100, 108, 99, 107, 1100, 11),
]


def test_a_weekly_bar_past_end_is_rebuilt_from_its_days_to_end(specs, alpaca):
    brk_week = _bar("2026-10-05T04:00:00Z", 400, 420, 390, 415, 90, None)
    brk_days = [_bar(d["t"], 400, 410, 395, 405, 20, None) for d in DAYS]
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [WEEK], "BRK.B": [brk_week]}), match={"timeframe": "1Week"})
    alpaca.add("/v2/stocks/trades", _trades("AAPL", "BRK.B"))
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": DAYS, "BRK.B": brk_days}), match={"timeframe": "1Day"})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK),
        tickers=["AAPL", "BRK-B"],
        timeframe="1w",
        start="2026-10-05",
        end="2026-10-08",
    )
    # first: is anything traded between end and the week's close, on the bars' feed
    assert alpaca.params(1) == {
        "symbols": "AAPL,BRK.B",
        "start": "2026-10-09T03:59:59.999999Z",
        "end": "2026-10-12T04:00:00Z",
        "limit": "1000",
        "feed": "iex",
    }
    assert alpaca.params(2) == {
        "symbols": "AAPL,BRK.B",
        "timeframe": "1Day",
        "start": "2026-10-05T04:00:00Z",
        "end": "2026-10-09T03:59:59.999999Z",
        "adjustment": "all",
        "feed": "iex",
        "sort": "asc",
        "limit": "10000",
    }
    aapl, brk = out.rows
    assert aapl.t == datetime(2026, 10, 5, 4, tzinfo=UTC) and aapl.timeframe == "1w"
    assert (aapl.open, aapl.high, aapl.low, aapl.close, aapl.volume, aapl.trade_count) == (
        100,
        108,
        95,
        107,  # Thursday's close, not Friday's 111
        4200,
        42,
    )
    assert aapl.vwap is None and aapl.absent == {"vwap": "insufficient_data"}
    assert (brk.ticker, brk.close, brk.volume, brk.trade_count) == ("BRK-B", 405, 80, None)
    assert brk.absent == {"vwap": "insufficient_data", "trade_count": "not_provided_by_source"}
    assert unexplained_absences(out.rows, out.absent) == []
    assert out.provenance.as_of == datetime(2026, 10, 9, 3, 59, 59, 999999, tzinfo=UTC)
    assert out.notes[1:] == [
        "The 1w bar from 2026-10-05T04:00:00Z (AAPL, BRK-B) runs to 2026-10-12T04:00:00Z, past end "
        "(2026-10-09T03:59:59.999999Z), and trades after end fall in it: it is rebuilt from the 1d bars of "
        "2026-10-05 through 2026-10-08 (New York dates), so it covers those days only; its vwap is null "
        "(insufficient_data), which the 1d bars do not give exactly."
    ]


def test_a_monthly_bar_past_a_mid_day_end_covers_its_whole_days_only(specs, alpaca):
    month = _bar("2026-10-01T04:00:00Z", 90, 120, 80, 118, 9000)
    days = [_bar(t, 90, 100, 85, 95, 500) for t in ("2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z")]
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [month]}), match={"timeframe": "1Month"})
    alpaca.add("/v2/stocks/trades", _trades("AAPL"))
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": days}), match={"timeframe": "1Day"})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK),
        tickers=["AAPL"],
        timeframe="1mo",
        start="2026-09-01",
        end="2026-10-05T15:00:00Z",
    )
    # the days before the New York midnight before end: Monday 10-05 itself is cut at 15:00Z, so it is left out
    assert (alpaca.params(2)["start"], alpaca.params(2)["end"]) == (
        "2026-10-01T04:00:00Z",
        "2026-10-05T03:59:59.999999Z",
    )
    assert [(r.close, r.volume) for r in out.rows] == [(95, 1000)]
    assert out.notes == [
        "The 1mo bar from 2026-10-01T04:00:00Z (AAPL) runs to 2026-11-01T04:00:00Z, past end "
        "(2026-10-05T15:00:00Z), and trades after end fall in it: it is rebuilt from the 1d bars of 2026-10-01 "
        "through 2026-10-04 (New York dates), so it covers those days only; its vwap is null "
        "(insufficient_data), which the 1d bars do not give exactly."
    ]


@pytest.mark.parametrize(
    ("timeframe", "end", "kept", "straddling", "runs_to"),
    [
        (
            "1h",
            "2026-10-05T14:30:00Z",
            "2026-10-05T13:00:00Z",
            "2026-10-05T14:00:00Z",
            "2026-10-05T15:00:00Z",
        ),
        (
            "1d",
            "2026-10-05T18:00:00Z",
            "2026-10-02T04:00:00Z",
            "2026-10-05T04:00:00Z",
            "2026-10-06T04:00:00Z",
        ),
        # no whole day of the week falls before end, so nothing rebuilds it
        (
            "1w",
            "2026-10-05T18:00:00Z",
            "2026-09-28T04:00:00Z",
            "2026-10-05T04:00:00Z",
            "2026-10-12T04:00:00Z",
        ),
    ],
)
def test_a_day_or_shorter_bar_past_end_is_left_out(specs, alpaca, timeframe, end, kept, straddling, runs_to):
    alpaca.add(
        "/v2/stocks/bars",
        _bars(
            {
                "AAPL": [_bar(straddling, 1, 2, 1, 2, 10)],
                "MSFT": [_bar(kept, 1, 2, 1, 2, 10), _bar(straddling, 1, 2, 1, 2, 10)],
            }
        ),
    )
    alpaca.add("/v2/stocks/trades", _trades("AAPL", "MSFT"))
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK),
        tickers=["AAPL", "MSFT"],
        timeframe=timeframe,
        start="2026-09-28",
        end=end,
    )
    assert len(alpaca.requests) == 2  # the bars, then the trades after end
    assert (alpaca.params(1)["start"], alpaca.params(1)["end"]) == (end, runs_to)
    assert [(r.ticker, iso_z(r.t)) for r in out.rows] == [("MSFT", kept)]
    # AAPL had a bar in the window: it is not called missing
    assert out.notes == [
        f"The {timeframe} bar from {straddling} (AAPL, MSFT) runs to {runs_to}, past end ({end}), and trades "
        "after end fall in it: it is left out; bars of a shorter timeframe cover its start to end."
    ]


@pytest.mark.parametrize(
    ("settings", "end", "first_note"),
    [
        ({}, None, None),
        ({"stock_feed": "delayed_sip"}, "2026-10-10", "end 2026-10-10 is later than the newest data"),
    ],
)
def test_the_bar_still_forming_now_is_kept_and_named(specs, alpaca, settings, end, first_note):
    week = [_bar("2026-09-21T04:00:00Z", 1, 2, 1, 2, 10), _bar("2026-09-28T04:00:00Z", 2, 3, 2, 3, 10)]
    alpaca.add("/v2/stocks/bars", _bars({"MSFT": week}))
    args = {"end": end} if end else {}
    out = call(
        specs["market_bars"],
        FakeContext(settings=settings),
        tickers=["MSFT"],
        timeframe="1w",
        lookback="P14D",
        **args,
    )
    assert len(alpaca.requests) == 1 and len(out.rows) == 2 and out.rows[1].close == 3
    assert out.notes[-1] == (
        "The 1w bar from 2026-09-28T04:00:00Z (MSFT) is the current one: its period runs to "
        "2026-10-05T04:00:00Z, after now (2026-10-02T20:00:00Z), so it holds the trades so far, and any later "
        "trade in its period would change it."
    )
    assert first_note is None or out.notes[0].startswith(first_note)


@pytest.mark.parametrize(
    ("days", "limits", "why"),
    [
        (
            httpx.Response(429, json={"message": "too many requests"}),
            FetchLimits(),
            "it is left out (its 1d bars failed: alpaca_rate_limited).",
        ),
        (
            _bars({"AAPL": DAYS}),
            FetchLimits(max_rows=3),
            "it is left out (its 1d bars stopped at the fetch limits; narrow the request).",
        ),
        (
            _bars({}),
            FetchLimits(),
            "it is left out, as no 1d bar of it falls on 2026-10-05 through 2026-10-08 (New York dates).",
        ),
    ],
)
def test_a_weekly_bar_its_days_cannot_rebuild_is_left_out(specs, alpaca, days, limits, why):
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [WEEK]}), match={"timeframe": "1Week"})
    alpaca.add("/v2/stocks/trades", _trades("AAPL"))
    alpaca.add("/v2/stocks/bars", days, match={"timeframe": "1Day"})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK, limits=limits),
        tickers=["AAPL"],
        timeframe="1w",
        start="2026-10-05",
        end="2026-10-08",
    )
    assert out.rows == []
    assert out.notes[-1] == (
        "The 1w bar from 2026-10-05T04:00:00Z (AAPL) runs to 2026-10-12T04:00:00Z, past end "
        f"(2026-10-09T03:59:59.999999Z), and trades after end fall in it: {why}"
    )


@pytest.mark.parametrize(
    ("timeframe", "bar", "end", "close"),
    [
        # a week whose end is a Friday: nothing trades on the SIP feed from then to Monday
        ("1w", WEEK, "2026-10-09", "2026-10-12T04:00:00Z"),
        # a day whose end is 20:00 New York with no print after it (AAPL's on SIP; SPY prints at 20:00:00.015)
        ("1d", DAYS[3], "2026-10-09T00:00:00Z", "2026-10-09T04:00:00Z"),
    ],
)
def test_a_bar_past_end_with_no_trade_after_end_is_kept_whole(specs, alpaca, timeframe, bar, end, close):
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [bar]}))
    alpaca.add("/v2/stocks/trades", {"trades": {}, "next_page_token": None})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK),
        tickers=["AAPL"],
        timeframe=timeframe,
        start="2026-10-05",
        end=end,
    )
    assert len(alpaca.requests) == 2 and alpaca.params(1)["end"] == close  # no 1d bars to rebuild it
    (row,) = out.rows
    assert (row.close, row.volume, row.trade_count, row.vwap) == (bar["c"], bar["v"], bar["n"], bar["vw"])
    assert not row.absent
    # its period is over: no note names it (as past end, the current one or the newest one served)
    assert not [n for n in out.notes if f"bar from {iso_z(row.t)}" in n]


#: Saturday after the week of 2026-10-12: nothing trades from end 2026-10-16 to now, but October runs on.
SATURDAY = datetime(2026, 10, 17, 5, 5, tzinfo=UTC)
MONTH = _bar("2026-10-01T04:00:00Z", 330, 345, 325, 343, 9000, 90)
MONTH_IS_CURRENT = (
    "The 1mo bar from 2026-10-01T04:00:00Z (AAPL) is the current one: its period runs to 2026-11-01T04:00:00Z, "
    "after now (2026-10-17T05:05:00Z), so it holds the trades so far (none after end), and any later trade in "
    "its period would change it."
)
MONTH_PAST_END = (
    "The 1mo bar from 2026-10-01T04:00:00Z (MSFT) runs to 2026-11-01T04:00:00Z, past end "
    "(2026-10-17T03:59:59.999999Z), and trades after end fall in it: "
)


def test_a_bar_kept_whole_whose_period_runs_past_now_is_the_current_one(specs, alpaca):
    # nothing trades after end, so AAPL's October bar for end 2026-10-16 is kept whole, but its period runs on
    # to November; the reported case, with nothing else to rebuild or leave out
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [MONTH]}))
    alpaca.add("/v2/stocks/trades", {"trades": {}, "next_page_token": None})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=SATURDAY),
        tickers=["AAPL"],
        timeframe="1mo",
        start="2026-09-01",
        end="2026-10-16",
    )
    assert len(alpaca.requests) == 2  # the bars, then the trades after end: no 1d bars
    assert (alpaca.params(1)["start"], alpaca.params(1)["end"]) == (
        "2026-10-17T03:59:59.999999Z",
        "2026-10-17T05:05:00Z",
    )
    (aapl,) = out.rows
    assert (aapl.close, aapl.volume, aapl.vwap) == (343, 9000, MONTH["vw"]) and not aapl.absent
    assert out.notes[1:] == [MONTH_IS_CURRENT]


def test_the_current_bar_kept_whole_is_named_beside_a_rebuilt_one(specs, alpaca):
    # MSFT's, which trades after end, is rebuilt
    msft_days = [_bar(t, 500, 510, 495, 505, 20, 2) for t in ("2026-10-01T04:00:00Z", "2026-10-16T04:00:00Z")]
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [MONTH], "MSFT": [MONTH]}), match={"timeframe": "1Month"})
    alpaca.add("/v2/stocks/trades", _trades("MSFT"))
    alpaca.add("/v2/stocks/bars", _bars({"MSFT": msft_days}), match={"timeframe": "1Day"})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=SATURDAY),
        tickers=["AAPL", "MSFT"],
        timeframe="1mo",
        start="2026-09-01",
        end="2026-10-16",
    )
    assert alpaca.params(2)["symbols"] == "MSFT"
    aapl, msft = out.rows
    assert (aapl.close, aapl.volume, aapl.vwap) == (343, 9000, MONTH["vw"]) and not aapl.absent
    assert (msft.close, msft.volume, msft.vwap) == (505, 40, None)
    rebuilt = (
        "it is rebuilt from the 1d bars of 2026-10-01 through 2026-10-16 (New York dates), so it covers those days "
        "only; its vwap is null (insufficient_data), which the 1d bars do not give exactly."
    )
    assert out.notes[1:] == [MONTH_PAST_END + rebuilt, MONTH_IS_CURRENT]


@pytest.mark.parametrize(
    ("days", "limits", "why"),
    [
        (
            httpx.Response(429, json={"message": "too many requests"}),
            FetchLimits(),
            "it is left out (its 1d bars failed: alpaca_rate_limited).",
        ),
        (
            _bars({"MSFT": DAYS}),
            FetchLimits(max_rows=3),
            "it is left out (its 1d bars stopped at the fetch limits; narrow the request).",
        ),
    ],
)
def test_the_current_bar_kept_whole_is_named_beside_one_its_days_cannot_rebuild(
    specs, alpaca, days, limits, why
):
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [MONTH], "MSFT": [MONTH]}), match={"timeframe": "1Month"})
    alpaca.add("/v2/stocks/trades", _trades("MSFT"))
    alpaca.add("/v2/stocks/bars", days, match={"timeframe": "1Day"})
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=SATURDAY, limits=limits),
        tickers=["AAPL", "MSFT"],
        timeframe="1mo",
        start="2026-09-01",
        end="2026-10-16",
    )
    assert [(r.ticker, r.close) for r in out.rows] == [("AAPL", 343)]
    assert out.notes[1:] == [MONTH_PAST_END + why, MONTH_IS_CURRENT]


def test_the_current_bar_kept_whole_is_named_beside_a_shorter_one_left_out(specs, alpaca):
    # Friday afternoon: MSFT trades after end, so its day is left out; AAPL does not, so its day is kept whole,
    # and it is still open
    day = _bar("2026-10-16T04:00:00Z", 1, 2, 1, 2, 10)
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [day], "MSFT": [day]}))
    alpaca.add("/v2/stocks/trades", _trades("MSFT"))
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=datetime(2026, 10, 16, 19, tzinfo=UTC)),
        tickers=["AAPL", "MSFT"],
        timeframe="1d",
        start="2026-10-16",
        end="2026-10-16T16:00:00Z",
    )
    assert len(alpaca.requests) == 2 and alpaca.params(1)["end"] == "2026-10-16T19:00:00Z"
    assert [r.ticker for r in out.rows] == ["AAPL"]
    assert out.notes == [
        "The 1d bar from 2026-10-16T04:00:00Z (MSFT) runs to 2026-10-17T04:00:00Z, past end "
        "(2026-10-16T16:00:00Z), and trades after end fall in it: it is left out; bars of a shorter timeframe "
        "cover its start to end.",
        "The 1d bar from 2026-10-16T04:00:00Z (AAPL) is the current one: its period runs to 2026-10-17T04:00:00Z, "
        "after now (2026-10-16T19:00:00Z), so it holds the trades so far (none after end), and any later trade "
        "in its period would change it.",
    ]


@pytest.mark.parametrize(
    ("trades", "says"),
    [
        # a fuller answer than asked for settles the instruments it shows only
        (_trades("AAPL", token="next"), "may fall"),
        (_trades("AAPL", "MSFT", token="next"), "fall"),
        (httpx.Response(500, json={"message": "internal error"}), "may fall"),
    ],
)
def test_an_instrument_the_trades_after_end_do_not_settle_may_trade(specs, alpaca, trades, says):
    day = _bar("2026-10-08T04:00:00Z", 1, 2, 1, 2, 10)
    alpaca.add("/v2/stocks/bars", _bars({"AAPL": [day], "MSFT": [day]}))
    alpaca.add("/v2/stocks/trades", trades)
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK),
        tickers=["AAPL", "MSFT"],
        timeframe="1d",
        start="2026-10-05",
        end="2026-10-08T18:00:00Z",
    )
    assert out.rows == []
    assert out.notes[-1] == (
        "The 1d bar from 2026-10-08T04:00:00Z (AAPL, MSFT) runs to 2026-10-09T04:00:00Z, past end "
        f"(2026-10-08T18:00:00Z), and trades after end {says} in it: it is left out; bars of a shorter timeframe "
        "cover its start to end."
    )


FIRST_BAR = "Alpaca leaves out the {} bar from {}, which starts before start ({}): no bar covers start to {}."


@pytest.mark.parametrize(
    ("timeframe", "window", "note"),
    [
        # a Wednesday: the week from Monday has no bar
        (
            "1w",
            {"start": "2026-10-07"},
            ("2026-10-05T04:00:00Z", "2026-10-07T00:00:00Z", "2026-10-12T04:00:00Z"),
        ),
        ("1w", {"start": "2026-10-05"}, None),  # a Monday, at midnight New York
        (
            "1w",
            {"lookback": "P14D"},
            ("2026-09-28T04:00:00Z", "2026-10-02T00:00:00Z", "2026-10-05T04:00:00Z"),
        ),
        (
            "3mo",
            {"start": "2026-02-15"},
            ("2026-01-01T05:00:00Z", "2026-02-15T00:00:00Z", "2026-04-01T04:00:00Z"),
        ),
        (
            "1mo",
            {"start": "2026-08-15"},
            ("2026-08-01T04:00:00Z", "2026-08-15T00:00:00Z", "2026-09-01T04:00:00Z"),
        ),
        ("1d", {"start": "2026-10-07"}, None),
        (
            "1d",
            {"start": "2026-10-07T14:00:00Z"},
            ("2026-10-07T04:00:00Z", "2026-10-07T14:00:00Z", "2026-10-08T04:00:00Z"),
        ),
        # 7min bars start at 13:34Z, counted from Go's zero time
        (
            "7min",
            {"start": "2026-10-06T13:30:00Z"},
            ("2026-10-06T13:27:00Z", "2026-10-06T13:30:00Z", "2026-10-06T13:34:00Z"),
        ),
        ("1h", {"start": "2026-10-06T13:00:00Z"}, None),
        ("1h", {"lookback": "PT90M"}, None),  # a lookback's start for intraday bars is arbitrary
        # the whole window inside the left-out week
        (
            "1w",
            {"start": "2026-10-07", "end": "2026-10-08"},
            ("2026-10-05T04:00:00Z", "2026-10-07T00:00:00Z", "2026-10-09T03:59:59.999999Z"),
        ),
    ],
)
def test_the_stretch_from_start_no_bar_covers_is_named(specs, alpaca, timeframe, window, note):
    alpaca.add("/v2/stocks/bars", _bars({}))
    window = {"end": "2026-10-14", **window} if "start" in window else {"end": "2026-10-16", **window}
    out = call(
        specs["market_bars"],
        FakeContext(fixed_now=AFTER_THE_WEEK),
        tickers=["AAPL"],
        timeframe=timeframe,
        **window,
    )
    named = [n for n in out.notes if n.startswith("Alpaca leaves out")]
    if note is None:
        assert named == []
    else:
        first, start, upto = note
        assert named == [
            FIRST_BAR.format(timeframe, first, start, upto) + f" A start of {first} takes in that whole bar."
        ]


@pytest.mark.parametrize(
    ("t", "timeframe", "zone", "starts"),
    [
        # Alpaca's bar starts, as it sent them: N minutes or hours from Go's zero time, not from midnight
        ("2026-10-06T13:30:00Z", "7min", "UTC", "2026-10-06T13:27:00Z"),
        ("2026-10-06T02:00:00Z", "5h", "UTC", "2026-10-05T23:00:00Z"),
        ("2026-10-06T02:00:00Z", "4h", "UTC", "2026-10-06T00:00:00Z"),
        ("2026-10-07T00:00:00Z", "1w", "UTC", "2026-10-05T00:00:00Z"),
        # days, Mondays and months of N from January at midnight New York, across the clock change too
        ("2026-10-12T03:00:00Z", "1w", "NY", "2026-10-05T04:00:00Z"),  # Sunday 23:00 New York
        ("2026-11-04T12:00:00Z", "1w", "NY", "2026-11-02T05:00:00Z"),
        ("2026-02-15T00:00:00Z", "3mo", "NY", "2026-01-01T05:00:00Z"),
        ("2026-12-05T12:00:00Z", "2mo", "NY", "2026-11-01T04:00:00Z"),
        ("2026-08-15T00:00:00Z", "6mo", "NY", "2026-07-01T04:00:00Z"),
        ("2026-08-15T00:00:00Z", "12mo", "NY", "2026-01-01T05:00:00Z"),
    ],
)
def test_bar_start(t, timeframe, zone, starts):
    from marketlens_mcp.providers.alpaca.convert import NEW_YORK, parse_ts
    from marketlens_mcp.providers.alpaca.tools.bars import bar_start

    assert iso_z(bar_start(parse_ts(t), timeframe, NEW_YORK if zone == "NY" else UTC)) == starts


@pytest.mark.parametrize(
    ("t", "timeframe", "closes"),
    [
        # stocks' and options' bars of a day or longer start at midnight New York, also across the clock change
        ("2026-10-26T04:00:00Z", "1w", "2026-11-02T05:00:00Z"),
        ("2026-11-01T04:00:00Z", "1d", "2026-11-02T05:00:00Z"),
        ("2026-10-01T04:00:00Z", "3mo", "2027-01-01T05:00:00Z"),
        ("2026-01-01T05:00:00Z", "12mo", "2027-01-01T05:00:00Z"),
        ("2026-11-01T05:30:00Z", "4h", "2026-11-01T09:30:00Z"),
    ],
)
def test_bar_end_in_new_york(t, timeframe, closes):
    from marketlens_mcp.providers.alpaca.convert import NEW_YORK, parse_ts
    from marketlens_mcp.providers.alpaca.tools.bars import bar_end

    assert iso_z(bar_end(parse_ts(t), timeframe, NEW_YORK)) == closes


def test_a_start_past_the_delayed_sip_feeds_reach_is_refused(specs, alpaca):
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    with pytest.raises(ToolError) as e:
        call(
            specs["market_trades"],
            ctx,
            tickers=["MSFT"],
            start="2026-10-02T19:50:00Z",
            end="2026-10-02T19:55:00Z",
        )
    assert e.value.code == "invalid_arguments"
    assert e.value.message == (
        "start (2026-10-02T19:50:00Z) is not before the newest data the delayed SIP feed serves "
        "(2026-10-02T19:44:00Z); pass an earlier start."
    )
    assert alpaca.requests == []


@pytest.mark.parametrize(
    ("tool", "start"),
    [("market_trades", "2026-10-02T19:55:00Z"), ("market_quotes", "2026-10-02T19:44:00Z")],
)
def test_a_start_past_the_delayed_sip_feeds_reach_without_an_end_is_refused(specs, alpaca, tool, start):
    # Live: start now-5min, no end -> Alpaca 403 "recent SIP data"; start now-30min, no end -> served.
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    with pytest.raises(ToolError) as e:
        call(specs[tool], ctx, tickers=["AAPL"], start=start)
    assert e.value.code == "invalid_arguments"
    assert e.value.message == (
        f"start ({start}) is not before the newest data the delayed SIP feed serves (2026-10-02T19:44:00Z); "
        "pass an earlier start."
    )
    assert alpaca.requests == []


def test_a_lookback_without_an_end_reaches_back_from_the_delayed_sip_feeds_newest_data(specs, alpaca):
    alpaca.fixture("/v2/stocks/trades", "StockTrades")
    ctx = FakeContext(settings={"stock_feed": "delayed_sip"})
    out = call(specs["market_trades"], ctx, tickers=["AAPL"], lookback="PT5M")
    assert alpaca.params()["start"] == "2026-10-02T19:39:00Z" and "end" not in alpaca.params()
    assert out.provenance.as_of == datetime(2026, 10, 2, 19, 44, tzinfo=UTC)
    assert out.notes[0] == (
        "lookback PT5M reaches back from the newest data the delayed SIP feed serves (16 minutes before now): "
        "the window starts at 2026-10-02T19:39:00Z."
    )


def test_a_lookback_without_an_end_on_a_real_time_feed_says_nothing_of_a_lag(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/trades", "StockTrades")
    out = call(specs["market_trades"], ctx, tickers=["AAPL"], lookback="PT5M")
    assert alpaca.params()["start"] == "2026-10-02T19:55:00Z"
    assert not any("reaches back" in n for n in out.notes)


def test_a_future_start_without_an_end_is_refused(specs, ctx, alpaca):
    with pytest.raises(ToolError) as e:
        call(specs["market_bars"], ctx, tickers=["AAPL"], start="2026-10-03")
    assert e.value.message == (
        "start (2026-10-03T00:00:00Z) is not before the newest data Alpaca serves (2026-10-02T20:00:00Z); "
        "pass an earlier start."
    )
    assert alpaca.requests == []


def test_start_and_lookback_together_are_refused(specs, ctx, alpaca):
    with pytest.raises(ToolError) as e:
        call(specs["market_bars"], ctx, tickers=["AAPL"], start="2026-01-02", lookback="P5D")
    assert e.value.code == "invalid_arguments"
    assert alpaca.requests == []


def test_a_lookback_reaching_before_year_one_is_refused(specs, ctx, alpaca):
    with pytest.raises(ToolError) as e:
        call(specs["market_bars"], ctx, tickers=["AAPL"], lookback="P5000Y")
    assert e.value.code == "invalid_arguments" and "P5000Y" in e.value.message
    assert alpaca.requests == []


def test_a_lookback_too_long_for_a_timedelta_is_a_validation_error(specs, ctx, alpaca):
    with pytest.raises(ValidationError, match="too long"):
        call(specs["market_bars"], ctx, tickers=["AAPL"], lookback="P99999999999999D")
    assert alpaca.requests == []


@pytest.mark.parametrize(("arg", "message"), [(["BTC/USD"], "crypto pair"), (["AA PL!"], "'AA PL!'")])
def test_bad_tickers_are_refused_by_name(specs, ctx, arg, message):
    with pytest.raises(ValidationError, match=message):
        call(specs["market_bars"], ctx, tickers=arg)


def test_market_quotes_golden_empty_bid_and_round_lots(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/quotes", "StockQuotes")
    out = call(specs["market_quotes"], ctx, tickers=["AAPL"], start="2025-10-31", end="2026-10-02T20:00:00Z")
    doc = check_golden(out, "market_quotes")
    old, new, empty = doc["rows"]
    assert (old["bid_size"], old["ask_size"]) == (300.0, 200.0)  # round lots x 100 before 2025-11-03
    assert (new["bid_size"], new["ask_size"]) == (300.0, 120.0)
    assert empty["bid_price"] is None and empty["absent"]["bid_price"] == "no_data"
    assert alpaca.params()["limit"] == "10000"


def test_market_quotes_default_lookback_is_twenty_minutes(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/quotes", "StockQuotes")
    call(specs["market_quotes"], ctx, tickers=["AAPL"])
    assert alpaca.params()["start"] == "2026-10-02T19:40:00Z"


def test_market_trades_golden_drops_canceled_trades(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/trades", "StockTrades")
    out = call(specs["market_trades"], ctx, tickers=["BRK-B"])
    doc = check_golden(out, "market_trades")
    assert [r["trade_id"] for r in doc["rows"]] == ["1", "3"]
    assert any("canceled" in n for n in doc["notes"])


@pytest.mark.parametrize(
    ("tool", "path", "fixture", "has"),
    [
        ("market_bars", "/v2/stocks/bars", "StockBars__intraday", "MSFT"),
        ("market_quotes", "/v2/stocks/quotes", "StockQuotes", "AAPL"),
        ("market_trades", "/v2/stocks/trades", "StockTrades", "BRK-B"),
        ("crypto_bars", "/v1beta3/crypto/us/bars", "CryptoBars", "BTC/USD"),
        ("crypto_quotes", "/v1beta3/crypto/us/quotes", "CryptoQuotes", "BTC/USD"),
        ("crypto_trades", "/v1beta3/crypto/us/trades", "CryptoTrades", "ETH/USD"),
    ],
)
def test_history_tools_name_the_instruments_alpaca_returned_nothing_for(
    specs, ctx, alpaca, tool, path, fixture, has
):
    alpaca.fixture(path, fixture)
    other = "FOO/USD" if "/" in has else "ZZZZQ"
    out = call(specs[tool], ctx, tickers=[has, other])
    assert {r.ticker for r in out.rows} == {has}
    assert f"No data from Alpaca for: {other} in this window." in out.notes


def test_a_cut_or_continued_history_fetch_names_no_missing_instrument(specs, alpaca):
    # Alpaca pages through the tickers in turn: a cut fetch lacks the later ones for that reason alone.
    alpaca.fixture("/v2/stocks/bars", "StockBars__page2", match={"page_token": CURSOR})
    alpaca.fixture("/v2/stocks/bars", "StockBars__page1")
    cut = call(
        specs["market_bars"],
        FakeContext(limits=FetchLimits(max_rows=2, max_pages=20)),
        tickers=["AAPL", "BRK-B"],
    )
    assert not any("No data from Alpaca" in n for n in cut.notes)
    continued = call(specs["market_bars"], FakeContext(), tickers=["AAPL", "BRK-B"], page_token=CURSOR)
    assert {r.ticker for r in continued.rows} == {"BRK-B"}
    assert not any("No data from Alpaca" in n for n in continued.notes)


def test_market_latest_bars_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars/latest", "StockLatestBars")
    out = call(specs["market_latest_bars"], ctx, tickers=["AAPL", "BRK-B"])
    doc = check_golden(out, "market_latest_bars")
    assert {r["timeframe"] for r in doc["rows"]} == {"1min"}
    assert alpaca.params() == {"symbols": "AAPL,BRK.B", "feed": "iex"}


def test_market_latest_quotes_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/quotes/latest", "StockLatestQuotes")
    out = call(specs["market_latest_quotes"], ctx, tickers=["AAPL", "BRK-B"])
    doc = check_golden(out, "market_latest_quotes")
    brk = doc["rows"][1]
    assert brk["bid_price"] is None and brk["ask_price"] is None
    assert brk["absent"]["ask_price"] == "no_data"


def test_market_latest_trades_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/trades/latest", "StockLatestTrades")
    out = call(specs["market_latest_trades"], ctx, tickers=["AAPL"])
    check_golden(out, "market_latest_trades")


@pytest.mark.parametrize(
    ("tool", "path", "fixture"),
    [
        ("market_latest_bars", "/v2/stocks/bars/latest", "StockLatestBars"),
        ("market_latest_quotes", "/v2/stocks/quotes/latest", "StockLatestQuotes"),
        ("market_latest_trades", "/v2/stocks/trades/latest", "StockLatestTrades"),
    ],
)
def test_market_latest_names_tickers_alpaca_returned_nothing_for(specs, ctx, alpaca, tool, path, fixture):
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, tickers=["AAPL", "QQQQX"])
    assert "AAPL" in {r.ticker for r in out.rows}
    assert "No data from Alpaca for: QQQQX." in out.notes


STALE_QRVO = (
    "QRVO (2026-09-25T21:53:31.828953Z) is more than a day older than the newest row (2026-10-02T00:00:00.015000Z), "
    "which provenance.as_of shows: stopped trading (delisted, halted), or seldom traded; each row's t says when."
)
OFFICIAL_SPY = (
    "The latest record of SPY is a market center's official close or open message (SIP condition M, Q or 9), "
    "not a trade: its price is that venue's official price and its size is not one trade's size; market_trades "
    "with sort desc shows the trades before it."
)


def test_market_latest_trades_label_an_official_close_and_name_a_stale_ticker(specs, ctx, alpaca):
    alpaca.add(
        "/v2/stocks/trades/latest",
        {
            "trades": {
                # NYSE Arca's official close at 20:00 New York: a message, not a trade of 1,174,832 shares.
                "SPY": {
                    "t": "2026-10-02T00:00:00.015Z",
                    "p": 773.93,
                    "s": 1174832,
                    "x": "P",
                    "c": [" ", "M"],
                    "i": 1,
                    "z": "B",
                },
                "QRVO": {
                    "t": "2026-09-25T21:53:31.828953Z",
                    "p": 115.0,
                    "s": 152,
                    "x": "D",
                    "c": ["@", "T"],
                    "i": 2,
                    "z": "C",
                },
            }
        },
    )
    out = call(specs["market_latest_trades"], ctx, tickers=["SPY", "QRVO"])
    assert {r.ticker for r in out.rows} == {"SPY", "QRVO"}  # labelled, not dropped
    assert out.provenance.as_of == datetime(2026, 10, 2, 0, 0, 0, 15000, tzinfo=UTC)
    assert out.notes == [STALE_QRVO, OFFICIAL_SPY]


def test_market_latest_quotes_name_a_stale_ticker(specs, ctx, alpaca):
    quote = {"bp": 1.0, "bs": 1, "bx": "V", "ap": 1.1, "as": 1, "ax": "V", "c": ["R"], "z": "C"}
    alpaca.add(
        "/v2/stocks/quotes/latest",
        {"quotes": {"QRVO": {**quote, "t": "2026-09-25T23:33:48Z"}}},
        match={"symbols": "QRVO"},
    )
    alpaca.add(
        "/v2/stocks/quotes/latest",
        {
            "quotes": {
                "AAPL": {**quote, "t": "2026-10-02T19:59:58Z"},
                "QRVO": {**quote, "t": "2026-09-25T23:33:48Z"},
            }
        },
    )
    out = call(specs["market_latest_quotes"], ctx, tickers=["AAPL", "QRVO"])
    assert out.notes == [
        "QRVO (2026-09-25T23:33:48Z) is more than a day older than the newest row (2026-10-02T19:59:58Z), which "
        "provenance.as_of shows: stopped trading (delisted, halted), or seldom traded; each row's t says when."
    ]
    one = call(specs["market_latest_quotes"], ctx, tickers=["QRVO"])
    assert not any("older than the newest row" in n for n in one.notes)


def test_market_snapshots_label_an_official_close_as_the_last_trade(specs, ctx, alpaca):
    alpaca.add(
        "/v2/stocks/snapshots",
        {"SPY": _snap("2026-10-02T00:00:00.015Z", 773.93, [" ", "M"], "2026-10-01T04:00:00Z", 773.5, 770.0)},
    )
    out = call(specs["market_snapshots"], ctx, tickers=["SPY"])
    assert out.rows[0].change_basis == "day_close"  # 20:00 New York is after the session
    assert out.notes[-1] == OFFICIAL_SPY


def test_market_snapshots_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/snapshots", "StockSnapshots")
    out = call(specs["market_snapshots"], ctx, tickers=["AAPL", "BRK-B", "ZZZZ"])
    doc = check_golden(out, "market_snapshots")
    aapl, brk = doc["rows"]
    assert aapl["change"] == pytest.approx(5.8) and aapl["change_pct"] == pytest.approx(5.8 / 250.0)
    assert brk["change"] is None and brk["absent"]["change"] == "not_provided_by_source"
    assert brk["bid_price"] is None and brk["absent"]["bid_price"] == "no_data"
    assert any("ZZZZ" in n for n in doc["notes"])


def _snap(last_t: str, last: float, conditions: list[str], day_t: str, day_close: float, prev: float) -> dict:
    bar = {"o": day_close, "h": day_close, "l": day_close, "v": 1000, "n": 10, "vw": day_close}
    return {
        "latestTrade": {"t": last_t, "p": last, "s": 100, "x": "P", "c": conditions, "z": "C", "i": 1},
        "dailyBar": {**bar, "t": day_t, "c": day_close},
        "prevDailyBar": {**bar, "t": "2026-10-01T04:00:00Z", "c": prev},
    }


AFTER = (
    "not last_price: their last trade falls outside the latest session's regular hours (an extended-hours "
    "trade, or none since the session), so the change is that session's close against the one before."
)


def test_market_snapshots_after_the_close_give_the_sessions_change(specs, ctx, alpaca):
    # NVDA traded at 230.7446 after hours (18:00 New York); the session closed at 230.48 against 237.47.
    alpaca.add(
        "/v2/stocks/snapshots",
        {
            "NVDA": _snap(
                "2026-10-02T22:00:00Z", 230.7446, ["@", "T"], "2026-10-02T04:00:00Z", 230.48, 237.47
            ),
            "AAPL": _snap("2026-10-02T19:59:59Z", 255.8, ["@"], "2026-10-02T04:00:00Z", 255.7, 250.0),
        },
    )
    out = call(specs["market_snapshots"], ctx, tickers=["NVDA", "AAPL"])
    nvda, aapl = sorted(out.rows, key=lambda r: r.ticker != "NVDA")
    assert nvda.change_basis == "day_close" and nvda.last_price == 230.7446
    assert nvda.change == pytest.approx(230.48 - 237.47)
    assert nvda.change_pct == pytest.approx(
        (230.48 - 237.47) / 237.47
    )  # -2.94 %, not the -2.83 % of the trade
    assert aapl.change_basis == "last_trade" and aapl.change == pytest.approx(5.8)
    assert out.notes == [
        f"change and change_pct of NVDA compare day_close with prev_close (change_basis day_close), {AFTER}"
    ]


@pytest.mark.parametrize(
    ("last_t", "conditions", "day_t", "basis"),
    [
        ("2026-10-02T13:30:00Z", ["@"], "2026-10-02T04:00:00Z", "last_trade"),  # 09:30 New York, the open
        ("2026-10-02T19:59:59Z", ["@"], "2026-10-02T04:00:00Z", "last_trade"),
        ("2026-10-02T20:00:00.2Z", ["@", "6"], "2026-10-02T04:00:00Z", "day_close"),  # 16:00 closing print
        ("2026-10-02T13:29:59Z", ["@", "T"], "2026-10-02T04:00:00Z", "day_close"),  # pre-market
        ("2026-10-02T18:00:00Z", ["@", "T"], "2026-10-02T04:00:00Z", "day_close"),  # early close: Form T
        ("2026-10-03T12:00:00Z", ["@", "T"], "2026-10-02T04:00:00Z", "day_close"),  # next morning
        ("2026-09-25T19:00:00Z", ["@"], "2026-10-02T04:00:00Z", "day_close"),  # stopped trading
        ("2026-01-05T15:00:00Z", ["@"], "2026-01-05T05:00:00Z", "last_trade"),  # winter: 10:00 New York
        ("2026-01-05T21:00:00Z", ["@"], "2026-01-05T05:00:00Z", "day_close"),  # winter: 16:00 New York
    ],
)
def test_stock_change_basis_is_the_day_bars_regular_session(last_t, conditions, day_t, basis):
    from marketlens_mcp.providers.alpaca.convert import parse_ts
    from marketlens_mcp.providers.alpaca.mappers import change_basis

    got = change_basis("us_equity", parse_ts(last_t), parse_ts(day_t), conditions, has_day_close=True)
    assert got == basis


def test_change_basis_without_a_day_close_is_the_last_trade():
    from marketlens_mcp.providers.alpaca.convert import parse_ts
    from marketlens_mcp.providers.alpaca.mappers import change_basis

    t = parse_ts("2026-10-02T22:00:00Z")
    assert change_basis("us_equity", t, None, ["T"], has_day_close=False) == "last_trade"


def test_snapshot_columns_say_what_change_is():
    from marketlens_schema.market import Snapshot

    for name in ("change", "change_pct", "change_basis", "day_close", "prev_close", "last_price"):
        assert Snapshot.model_fields[name].description, name


def test_market_most_active_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/most-actives", "MostActives")
    alpaca.fixture("/v2/stocks/snapshots", "StockSnapshots__most_actives")
    out = call(specs["market_most_active"], ctx, by="trades", top=2)
    doc = check_golden(out, "market_most_active")
    assert alpaca.params(0) == {"by": "trades", "top": "2"}
    assert alpaca.params(1) == {"symbols": "NVDA,BRK.B", "feed": "iex"}  # one snapshot request for the list
    assert [(r["rank"], r["ticker"]) for r in doc["rows"]] == [(1, "NVDA"), (2, "BRK-B")]
    nvda, brk = doc["rows"]
    assert (nvda["price"], nvda["change_basis"]) == (187.6, "last_trade")
    assert nvda["change"] == pytest.approx(2.6) and nvda["change_pct"] == pytest.approx(2.6 / 185.0)
    # BRK-B last traded after hours: its change is the session's, as market_snapshots gives it.
    assert (brk["price"], brk["change_basis"]) == (503.4, "day_close")
    assert brk["change"] == pytest.approx(2.8)
    assert doc["notes"][-1].startswith("change and change_pct of BRK-B compare day_close with prev_close")


def _actives(*symbols: str) -> dict:
    return {
        "most_actives": [
            {"symbol": s, "volume": 1000 - i, "trade_count": 100 - i} for i, s in enumerate(symbols)
        ],
        "last_updated": "2026-10-02T19:55:01Z",
    }


def _price(p: float) -> dict:
    return {
        "latestTrade": {"t": "2026-10-02T19:54:59Z", "p": p, "s": 1, "x": "Q", "c": ["@"], "z": "C", "i": 1}
    }


def test_market_most_active_min_price_screens_the_top_100(specs, ctx, alpaca):
    alpaca.add("/v1beta1/screener/stocks/most-actives", _actives("SNDL", "NVDA", "PENY", "AAPL", "GONE"))
    alpaca.add(
        "/v2/stocks/snapshots",
        {"SNDL": _price(1.9), "NVDA": _price(187.6), "PENY": _price(0.4), "AAPL": _price(255.8)},
    )
    out = call(specs["market_most_active"], ctx, top=3, min_price=5)
    assert alpaca.params(0) == {"by": "volume", "top": "100"}
    assert [(r.rank, r.ticker, r.price) for r in out.rows] == [(1, "NVDA", 187.6), (2, "AAPL", 255.8)]
    assert out.notes == [
        "min_price dropped 3 of the 5 most active Alpaca ranked: 2 priced below min_price 5, 1 without a snapshot "
        "price.",
        "Only 2 of the 3 asked for passed min_price.",
    ]


def test_market_most_active_min_price_does_not_blame_itself_for_a_short_list(specs, ctx, alpaca):
    alpaca.add("/v1beta1/screener/stocks/most-actives", _actives("NVDA", "AAPL"))
    alpaca.add("/v2/stocks/snapshots", {"NVDA": _price(187.6), "AAPL": _price(255.8)})
    out = call(specs["market_most_active"], ctx, top=3, min_price=5)
    assert [r.ticker for r in out.rows] == ["NVDA", "AAPL"]
    assert out.notes == ["Alpaca ranked 2, fewer than the 3 asked for."]


def test_market_most_active_without_snapshots_still_ranks(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/most-actives", "MostActives")
    alpaca.add(
        "/v2/stocks/snapshots", {"message": "subscription does not permit querying SIP data"}, status=403
    )
    out = call(specs["market_most_active"], ctx, by="trades", top=2)
    assert [r.ticker for r in out.rows] == ["NVDA", "BRK-B"]
    assert out.rows[0].price is None and out.rows[0].absent["price"] == "no_data"
    assert out.notes == [
        "Prices could not be read from the snapshots (alpaca_not_entitled): price and change are null."
    ]


def test_market_movers_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks")
    out = call(specs["market_movers"], ctx, top=1)
    doc = check_golden(out, "market_movers")
    assert alpaca.params() == {"top": "1"}  # no filter: Alpaca's own top, as before
    assert doc["rows"][0]["percent_change"] == pytest.approx(1.4556)
    assert doc["rows"][1]["direction"] == "loser"


def test_market_movers_filters_screen_alpacas_top_50_and_say_what_they_dropped(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks_screened")
    asset_names(alpaca)
    out = call(specs["market_movers"], ctx, top=2, min_price=1, exclude_warrants_rights_units=True)
    doc = check_golden(out, "market_movers__screened")
    assert alpaca.params() == {"top": "50"}
    # Kept in Alpaca's order, ranked from 1 among the kept, cut to top (SXTC is the third gainer kept).
    assert [(r["direction"], r["rank"], r["ticker"]) for r in doc["rows"]] == [
        ("gainer", 1, "WORX"),
        ("gainer", 2, "PFAI"),
        ("loser", 1, "BULG"),
    ]
    assert doc["notes"] == [
        "Filters dropped 4 of 7 gainers and 2 of 3 losers Alpaca ranked: 3 priced below min_price 1, "
        "3 warrants, rights or units by symbol.",
        "Only 1 of 2 losers passed the filters.",
    ]


def test_market_movers_min_price_alone_keeps_warrants_priced_above_it(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks_screened")
    out = call(specs["market_movers"], ctx, top=3, min_price=1)
    assert [r.ticker for r in out.rows if r.direction == "gainer"] == ["CCAQU", "WORX", "AAC-WS"]
    assert out.notes[0] == (
        "Filters dropped 2 of 7 gainers and 1 of 3 losers Alpaca ranked: 3 priced below min_price 1."
    )


def test_market_movers_a_short_list_from_alpaca_is_not_blamed_on_the_filters(specs, ctx, alpaca):
    # One gainer and one loser, both above $1: nothing is filtered.
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks")
    out = call(specs["market_movers"], ctx, top=3, min_price=1)
    assert [r.ticker for r in out.rows] == ["AGRI", "BRK-B"]
    assert out.notes == ["Alpaca ranked 1 gainer and 1 loser, fewer than the 3 asked for."]


@pytest.mark.parametrize(
    ("top", "short"),
    [
        # Alpaca ranked 7 gainers: the filters left 3 of them; it ranked 3 losers, fewer than top.
        (
            5,
            [
                "Only 3 of 5 gainers passed the filters.",
                "Alpaca ranked 3 losers, fewer than the 5 asked for.",
            ],
        ),
        # Fewer than top in both directions before any filter.
        (10, ["Alpaca ranked 7 gainers and 3 losers, fewer than the 10 asked for."]),
    ],
)
def test_market_movers_names_the_filters_only_where_alpaca_ranked_top_or_more(specs, ctx, alpaca, top, short):
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks_screened")
    asset_names(alpaca)
    out = call(specs["market_movers"], ctx, top=top, min_price=1, exclude_warrants_rights_units=True)
    assert out.notes == [
        "Filters dropped 4 of 7 gainers and 2 of 3 losers Alpaca ranked: 3 priced below min_price 1, "
        "3 warrants, rights or units by symbol.",
        *short,
    ]


def test_market_movers_an_empty_direction_is_alpacas_not_a_filter_count(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/screener/stocks/movers",
        {
            "gainers": [],
            "losers": [
                {"symbol": "BULG", "percent_change": -38.08, "change": -12.99, "price": 21.11},
                {"symbol": "GGROW", "percent_change": -50.82, "change": -0.0031, "price": 0.003},
                {"symbol": "PFAI", "percent_change": -16.67, "change": -1.01, "price": 5.07},
            ],
            "market_type": "stocks",
            "last_updated": "2026-10-02T19:55:00Z",
        },
    )
    out = call(specs["market_movers"], ctx, top=2, min_price=1)
    assert [(r.direction, r.rank, r.ticker) for r in out.rows] == [("loser", 1, "BULG"), ("loser", 2, "PFAI")]
    assert out.notes == [
        "Filters dropped 1 of 3 losers Alpaca ranked: 1 priced below min_price 1.",
        "Alpaca ranked no gainers, fewer than the 2 asked for.",
    ]


def test_market_movers_records_skipped_before_the_filters_are_not_blamed_on_them(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/screener/stocks/movers",
        {
            # Three ranked, one skipped (no price), one dropped: one short of top without the filters.
            "gainers": [
                {"symbol": "BADX", "percent_change": 61.2, "change": 2.1},
                {"symbol": "GGROW", "percent_change": 50.82, "change": 0.0031, "price": 0.003},
                {"symbol": "WORX", "percent_change": 44.25, "change": 1.76, "price": 5.75},
            ],
            # Three ranked, one skipped, none dropped.
            "losers": [
                {"symbol": "BULG", "percent_change": -38.08, "change": -12.99, "price": 21.11},
                {"symbol": "NOPR", "percent_change": -20.5, "change": -1.3},
                {"symbol": "PFAI", "percent_change": -16.67, "change": -1.01, "price": 5.07},
            ],
            "market_type": "stocks",
            "last_updated": "2026-10-02T19:55:00Z",
        },
    )
    out = call(specs["market_movers"], ctx, top=3, min_price=1)
    assert [(r.direction, r.rank, r.ticker) for r in out.rows] == [
        ("gainer", 1, "WORX"),
        ("loser", 1, "BULG"),
        ("loser", 2, "PFAI"),
    ]
    # Not "Only 1 of 3 gainers passed the filters": the gainers' shortfall names the skip too. The
    # losers' is the skip's alone, which the skips note covers.
    assert out.notes == [
        "Filters dropped 1 of 3 gainers Alpaca ranked: 1 priced below min_price 1.",
        "Only 1 of 3 gainers fit the model and passed the filters.",
        "Skipped 2 upstream record(s) that do not fit marketlens.Mover: BADX, NOPR.",
    ]


def test_market_movers_a_shortfall_the_filters_share_with_a_skip_is_still_noted(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/screener/stocks/movers",
        {
            # Four ranked, one skipped, two dropped: the filters cause most of the shortfall.
            "gainers": [
                {"symbol": "BADX", "percent_change": 61.2, "change": 2.1},
                {"symbol": "GGROW", "percent_change": 50.82, "change": 0.0031, "price": 0.003},
                {"symbol": "PENY", "percent_change": 47.1, "change": 0.16, "price": 0.5},
                {"symbol": "WORX", "percent_change": 44.25, "change": 1.76, "price": 5.75},
            ],
            # Five ranked, one skipped, two dropped: the filters alone leave fewer than top.
            "losers": [
                {"symbol": "NOPR", "percent_change": -60.5, "change": -1.3},
                {"symbol": "LOWA", "percent_change": -55.2, "change": -0.4, "price": 0.32},
                {"symbol": "BULG", "percent_change": -38.08, "change": -12.99, "price": 21.11},
                {"symbol": "LOWB", "percent_change": -30.1, "change": -0.2, "price": 0.46},
                {"symbol": "PFAI", "percent_change": -16.67, "change": -1.01, "price": 5.07},
            ],
            "market_type": "stocks",
            "last_updated": "2026-10-02T19:55:00Z",
        },
    )
    out = call(specs["market_movers"], ctx, top=4, min_price=1)
    assert [(r.direction, r.rank, r.ticker) for r in out.rows] == [
        ("gainer", 1, "WORX"),
        ("loser", 1, "BULG"),
        ("loser", 2, "PFAI"),
    ]
    assert out.notes == [
        "Filters dropped 2 of 4 gainers and 2 of 5 losers Alpaca ranked: 4 priced below min_price 1.",
        "Only 2 of 4 losers passed the filters.",
        "Only 1 of 4 gainers fit the model and passed the filters.",
        "Skipped 2 upstream record(s) that do not fit marketlens.Mover: BADX, NOPR.",
    ]


def test_market_movers_filter_notes_count_one_mover_in_the_singular(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/screener/stocks/movers",
        {
            "gainers": [{"symbol": "GGROW", "percent_change": 50.82, "change": 0.0031, "price": 0.003}],
            "losers": [
                {"symbol": "NRSNW", "percent_change": -34.42, "change": -0.79, "price": 1.515},
                {"symbol": "BULG", "percent_change": -38.08, "change": -12.99, "price": 21.11},
            ],
            "market_type": "stocks",
            "last_updated": "2026-10-02T19:55:00Z",
        },
    )
    asset_names(alpaca)
    out = call(specs["market_movers"], ctx, top=1, min_price=1, exclude_warrants_rights_units=True)
    assert [r.ticker for r in out.rows] == ["BULG"]
    assert out.notes == [
        "Filters dropped 1 of 1 gainer and 1 of 2 losers Alpaca ranked: 1 priced below min_price 1, "
        "1 warrant, right or unit by symbol.",
        "Only 0 of 1 gainer passed the filters.",
    ]


def test_market_movers_symbol_filter_is_for_stocks_only(specs, ctx):
    with pytest.raises(ValidationError, match="exclude_warrants_rights_units applies to stocks only"):
        call(specs["market_movers"], ctx, market_type="crypto", exclude_warrants_rights_units=True)


MOVERS_BY_NAME = {
    "gainers": [
        {"symbol": "NE.WSA", "percent_change": 40.0, "change": 2.0, "price": 7.0},
        {"symbol": "ZWRT", "percent_change": 30.0, "change": 1.5, "price": 6.5},
        {"symbol": "ET", "percent_change": 9.0, "change": 1.5, "price": 18.1},
        {"symbol": "RTON", "percent_change": 8.0, "change": 0.6, "price": 8.1},
        {"symbol": "NEWCO", "percent_change": 7.0, "change": 0.5, "price": 7.6},
    ],
    "losers": [],
    "market_type": "stocks",
    "last_updated": "2026-10-02T19:55:00Z",
}
NAMES = {
    "NE.WSA": "Noble Corporation plc Tranche 2 Warrants",
    "ZWRT": "Zeta Holdings Tranche 3 Warrants",  # synthetic: a warrant whose symbol says nothing
    "ET": "Energy Transfer LP Common Units",
    "RTON": "Right On Brands Inc Common Stock",
}


def test_market_movers_drop_warrants_by_symbol_and_by_asset_name(specs, ctx, alpaca):
    alpaca.add("/v1beta1/screener/stocks/movers", MOVERS_BY_NAME)
    names = asset_names(alpaca, NAMES)
    out = call(specs["market_movers"], ctx, top=3, exclude_warrants_rights_units=True)
    # NE-WSA by symbol, ZWRT by name; a partnership's common units and "Right On" stay; NEWCO is not listed.
    assert [r.ticker for r in out.rows] == ["ET", "RTON", "NEWCO"]
    assert out.notes == [
        "Filters dropped 2 of 5 gainers Alpaca ranked: 1 warrant, right or unit by symbol, 1 warrant, right or "
        "unit by asset name.",
        "Alpaca ranked no losers, fewer than the 3 asked for.",
    ]
    assert dict(parse_qsl(names_request(alpaca).url.query.decode())) == {
        "status": "active",
        "asset_class": "us_equity",
    }
    # The names are read once and kept: a second screen asks Alpaca for the movers only.
    call(specs["market_movers"], ctx, top=3, exclude_warrants_rights_units=True)
    assert names.calls == 1


def names_request(alpaca):
    return next(r for r in alpaca.requests if r.url.path == "/v2/assets")


def test_market_movers_judge_by_symbol_alone_when_the_names_cannot_be_read(specs, ctx, alpaca):
    alpaca.add("/v1beta1/screener/stocks/movers", MOVERS_BY_NAME)
    alpaca.add("/v2/assets", {"message": "forbidden"}, status=403)
    out = call(specs["market_movers"], ctx, top=5, exclude_warrants_rights_units=True)
    assert [r.ticker for r in out.rows] == ["ZWRT", "ET", "RTON", "NEWCO"]
    assert out.notes[-1] == (
        "Alpaca's asset names could not be read (alpaca_auth), so warrants, rights and units were judged by "
        "symbol alone."
    )


def test_name_rule_is_conservative():
    from marketlens_mcp.providers.alpaca.tools.stocks import NAME_RE

    for name in (
        "Noble Corporation plc Tranche 2 Warrants",
        "KAIROUS ACQUISITION CORP LTD Warrant   09/15/2026",
        "AIB Acquisition Corporation Rights",
        "AA Mission Acquisition Corp. II Units, each consisting of one Class A ordinary share",
    ):
        assert NAME_RE.search(name), name
    for name in (
        "Energy Transfer LP Common Units",
        "USD PARTNERS LP COM UNIT REPSTG LTD PARTNER INTS",
        "NextEra Energy, Inc. 7.299% Corporate Units",
        "Right On Brands Inc Common Stock",
        "UnitedHealth Group Incorporated Common Stock",
        "Uniti Group Inc. Common Stock",
    ):
        assert not NAME_RE.search(name), name


@pytest.mark.parametrize(
    ("ticker", "dropped"),
    [
        ("NRSNW", True),  # Nasdaq fifth letter W: warrant
        ("CHARR", True),  # R: right
        ("CCAQU", True),  # U: unit
        ("BCAT-RT", True),  # Alpaca's BCAT.RT
        ("AAC-WS", True),
        ("AAC-WS-A", True),
        ("NE-WSA", True),  # Noble's tranche 2 warrants: a WS series
        ("XYZ-WSB", True),
        ("ABC-RTWI", True),  # rights, when issued
        ("XYZ-U", True),
        ("XYZ-UN", True),
        ("AAPL", False),
        ("GOOGL", False),
        ("BRK-B", False),
        ("SNOW", False),  # four letters: the fifth-letter rule does not apply
        ("PENU", False),
        ("AMPGZ", False),  # Z is "miscellaneous", left alone
        ("BAC-PL", False),
        ("NEE-PRS", False),  # NextEra's corporate units trade as a preferred series
        ("BRK-A", False),
        ("XYZ-WI", False),  # when issued, not a warrant
    ],
)
def test_warrant_right_or_unit_is_judged_by_symbol(ticker, dropped):
    from marketlens_mcp.providers.alpaca.tools.stocks import warrant_right_or_unit

    assert warrant_right_or_unit(ticker) is dropped


@pytest.mark.parametrize(
    ("tool", "path", "fixture", "args"),
    [
        ("market_bars", "/v2/stocks/bars", "StockBars__intraday", {"tickers": ["MSFT"]}),
        ("market_quotes", "/v2/stocks/quotes", "StockQuotes", {"tickers": ["AAPL"]}),
        ("market_trades", "/v2/stocks/trades", "StockTrades", {"tickers": ["BRK-B"]}),
        ("crypto_bars", "/v1beta3/crypto/us/bars", "CryptoBars", {"tickers": ["BTC/USD"]}),
        ("crypto_trades", "/v1beta3/crypto/us/trades", "CryptoTrades", {"tickers": ["ETH/USD"]}),
        ("options_bars", "/v1beta1/options/bars", "OptionBars", {"occ_symbols": ["AAPL261016C00250000"]}),
        (
            "options_trades",
            "/v1beta1/options/trades",
            "OptionTrades",
            {"occ_symbols": ["AAPL261016C00250000"]},
        ),
    ],
)
def test_history_tools_accept_sort(specs, ctx, alpaca, tool, path, fixture, args):
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, sort="desc", **args)
    assert alpaca.params()["sort"] == "desc" and out.provenance.request["sort"] == "desc"
