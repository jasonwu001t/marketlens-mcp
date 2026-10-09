"""Stock tools: market_bars, market_quotes, market_trades, market_latest_bars,
market_latest_quotes, market_latest_trades, market_snapshots, market_most_active,
market_movers (golden outputs + the behaviours behind them)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alpaca_harness import NOW, FakeContext, call, check_golden
from pydantic import ValidationError

from marketlens_mcp.plugin_api import FetchLimits, ToolError

CURSOR = "QUFQTHxEfDIwMjYtMTAtMDE="  # the synthetic next-page cursor in StockBars__page1


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
    assert alpaca.params()["start"] == start


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
    assert alpaca.params()["end"] == "2026-10-02T03:59:59.999999Z"
    assert out.provenance.request["end"] == "2026-10-02T03:59:59.999999Z"
    assert out.provenance.as_of == datetime(2026, 10, 2, 3, 59, 59, 999999, tzinfo=UTC)
    assert out.notes[0] == (
        "end 2026-10-01 is a date: the window includes that whole day, to midnight New York time "
        "(2026-10-02T03:59:59.999999Z)."
    )


def test_a_date_end_alone_with_the_same_start_is_one_day(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    call(specs["market_bars"], ctx, tickers=["MSFT"], start="2026-10-01", end="2026-10-01")
    assert (alpaca.params()["start"], alpaca.params()["end"]) == (
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


def test_a_future_end_on_a_real_time_feed_is_cut_to_now(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/bars", "StockBars__intraday")
    out = call(specs["market_bars"], ctx, tickers=["MSFT"], start="2026-10-01", end="2026-10-10")
    assert alpaca.params()["end"] == "2026-10-02T20:00:00Z"
    assert out.provenance.as_of == NOW
    assert out.notes[0] == "end 2026-10-10 is in the future: the window ends now, 2026-10-02T20:00:00Z."


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
    out = call(specs["market_most_active"], ctx, by="trades", top=2)
    doc = check_golden(out, "market_most_active")
    assert alpaca.params() == {"by": "trades", "top": "2"}
    assert [(r["rank"], r["ticker"]) for r in doc["rows"]] == [(1, "NVDA"), (2, "BRK-B")]


def test_market_movers_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks")
    out = call(specs["market_movers"], ctx, top=1)
    doc = check_golden(out, "market_movers")
    assert alpaca.params() == {"top": "1"}  # no filter: Alpaca's own top, as before
    assert doc["rows"][0]["percent_change"] == pytest.approx(1.4556)
    assert doc["rows"][1]["direction"] == "loser"


def test_market_movers_filters_screen_alpacas_top_50_and_say_what_they_dropped(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/stocks/movers", "Movers__stocks_screened")
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


@pytest.mark.parametrize(
    ("ticker", "dropped"),
    [
        ("NRSNW", True),  # Nasdaq fifth letter W: warrant
        ("CHARR", True),  # R: right
        ("CCAQU", True),  # U: unit
        ("BCAT-RT", True),  # Alpaca's BCAT.RT
        ("AAC-WS", True),
        ("AAC-WS-A", True),
        ("XYZ-U", True),
        ("AAPL", False),
        ("GOOGL", False),
        ("BRK-B", False),
        ("SNOW", False),  # four letters: the fifth-letter rule does not apply
        ("PENU", False),
        ("AMPGZ", False),  # Z is "miscellaneous", left alone
        ("BAC-PL", False),
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
