"""Stock tools: market_bars, market_quotes, market_trades, market_latest_bars,
market_latest_quotes, market_latest_trades, market_snapshots, market_most_active,
market_movers (golden outputs + the behaviours behind them)."""

from __future__ import annotations

import pytest
from alpaca_harness import FakeContext, call, check_golden
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
        "start": "2025-10-02T20:00:00Z",
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


def test_market_snapshots_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/stocks/snapshots", "StockSnapshots")
    out = call(specs["market_snapshots"], ctx, tickers=["AAPL", "BRK-B", "ZZZZ"])
    doc = check_golden(out, "market_snapshots")
    aapl, brk = doc["rows"]
    assert aapl["change"] == pytest.approx(5.8) and aapl["change_pct"] == pytest.approx(5.8 / 250.0)
    assert brk["change"] is None and brk["absent"]["change"] == "not_provided_by_source"
    assert brk["bid_price"] is None and brk["absent"]["bid_price"] == "no_data"
    assert any("ZZZZ" in n for n in doc["notes"])


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
    assert doc["rows"][0]["percent_change"] == pytest.approx(1.4556)
    assert doc["rows"][1]["direction"] == "loser"


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
