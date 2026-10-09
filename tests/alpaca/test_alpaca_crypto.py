"""Crypto tools: crypto_bars, crypto_quotes, crypto_trades, crypto_latest_bars,
crypto_latest_quotes, crypto_latest_trades, crypto_snapshots, crypto_orderbooks;
plus market_movers for crypto."""

from __future__ import annotations

import pytest
from alpaca_harness import NOW, FakeContext, call, check_golden
from pydantic import ValidationError


def test_crypto_bars_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/bars", "CryptoBars")
    out = call(specs["crypto_bars"], ctx, tickers=["btc/usd"])
    doc = check_golden(out, "crypto_bars")
    assert alpaca.params() == {
        "symbols": "BTC/USD",
        "timeframe": "1Hour",
        "start": "2026-10-01T20:00:00Z",
        "sort": "asc",
        "limit": "10000",
    }
    assert {r["asset_class"] for r in doc["rows"]} == {"crypto"}
    assert (doc["provenance"]["feed"], doc["provenance"]["delay"]) == ("us", "realtime")
    assert doc["provenance"]["route"] == "GET /v1beta3/crypto/{loc}/bars"


def test_crypto_daily_bars_lookback_starts_at_utc_midnight(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/bars", "CryptoBars")
    call(specs["crypto_bars"], ctx, tickers=["BTC/USD"], timeframe="1d", lookback="P3D")
    assert alpaca.params()["start"] == "2026-09-29T00:00:00Z"  # three days back from 20:00Z, floored


@pytest.mark.parametrize(
    ("tool", "path", "fixture"),
    [
        ("crypto_bars", "/v1beta3/crypto/us/bars", "CryptoBars"),
        ("crypto_quotes", "/v1beta3/crypto/us/quotes", "CryptoQuotes"),
        ("crypto_trades", "/v1beta3/crypto/us/trades", "CryptoTrades"),
    ],
)
def test_a_crypto_date_end_includes_that_utc_day(specs, ctx, alpaca, tool, path, fixture):
    # Crypto daily bars are stamped at 00:00 UTC.
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, tickers=["BTC/USD"], start="2026-09-28", end="2026-10-01")
    assert alpaca.params()["end"] == "2026-10-01T23:59:59.999999Z"
    assert out.notes[0] == (
        "end 2026-10-01 is a date: the window includes that whole day, to midnight UTC "
        "(2026-10-01T23:59:59.999999Z)."
    )


def test_a_future_crypto_end_is_cut_to_now_and_as_of_is_now(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/bars", "CryptoBars")
    out = call(
        specs["crypto_bars"], ctx, tickers=["BTC/USD"], timeframe="1d", start="2026-09-30", end="2027-01-01"
    )
    assert alpaca.params()["end"] == "2026-10-02T20:00:00Z"
    assert out.provenance.as_of == NOW  # not 2027-01-01
    assert out.notes[0] == "end 2027-01-01 is in the future: the window ends now, 2026-10-02T20:00:00Z."


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        (
            "crypto_latest_quotes",
            {"tickers": ["/USD"]},
            "Invalid arguments for crypto_latest_quotes: tickers: Value error, '/USD' is not a crypto pair "
            "(expected BASE/QUOTE, e.g. BTC/USD).",
        ),
        (
            "crypto_trades",
            {"tickers": ["BTC/USD"], "start": "2026-10-08T00:00:00+25:00"},
            "Invalid arguments for crypto_trades: start: Value error, '2026-10-08T00:00:00+25:00' has a UTC offset "
            "out of range; offsets run from -23:59 to +23:59.",
        ),
    ],
)
def test_refusals_echo_the_input_and_end_in_one_period(specs, ctx, tool, arguments, message):
    import asyncio

    from marketlens_mcp.pipeline import execute
    from marketlens_mcp.plugin_api import ToolError

    with pytest.raises(ToolError) as e:
        asyncio.run(execute(specs[tool], ctx, arguments))
    assert e.value.code == "invalid_arguments"
    assert e.value.message == message


def _daily(t: str, close: float) -> dict:
    return {"t": t, "o": close, "h": close, "l": close, "c": close, "v": 1.5, "n": 12, "vw": close}


def test_crypto_bars_say_the_newest_bar_is_still_forming(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta3/crypto/us/bars",
        {
            "bars": {
                "BTC/USD": [_daily("2026-10-01T00:00:00Z", 61000), _daily("2026-10-02T00:00:00Z", 62150.75)],
                "ETH/USD": [_daily("2026-10-01T00:00:00Z", 2500), _daily("2026-10-02T00:00:00Z", 2576.45)],
            },
            "next_page_token": None,
        },
    )
    out = call(specs["crypto_bars"], ctx, tickers=["BTC/USD", "ETH/USD"], timeframe="1d", lookback="P1D")
    assert len(out.rows) == 4  # the rows are unchanged
    assert out.notes == [
        "The 1d bar from 2026-10-02T00:00:00Z (BTC/USD, ETH/USD) is still forming: it holds trades up to now "
        "(2026-10-02T20:00:00Z) only, not a whole 1d."
    ]


@pytest.mark.parametrize(
    ("timeframe", "start", "forming"),
    [
        ("1h", "2026-10-02T19:00:00Z", False),  # closes at 20:00, now
        ("1h", "2026-10-02T19:30:00Z", True),
        ("15min", "2026-10-02T19:45:00Z", False),
        ("1w", "2026-09-28T00:00:00Z", True),
        ("1mo", "2026-09-01T00:00:00Z", False),
        ("1mo", "2026-10-01T00:00:00Z", True),
        ("12mo", "2026-01-01T00:00:00Z", True),
    ],
)
def test_forming_bar_by_timeframe(timeframe, start, forming):
    from marketlens_mcp.providers.alpaca import mappers
    from marketlens_mcp.providers.alpaca.tools.crypto import forming_bar_note

    row = mappers.bar(_daily(start, 1.0), ticker="BTC/USD", asset_class="crypto", timeframe=timeframe)
    assert bool(forming_bar_note([row], timeframe, NOW)) is forming


def test_crypto_location_setting_picks_the_path(specs, alpaca):
    alpaca.fixture("/v1beta3/crypto/eu-1/bars", "CryptoBars")
    out = call(specs["crypto_bars"], FakeContext(settings={"crypto_location": "eu-1"}), tickers=["BTC/USD"])
    assert out.provenance.feed == "eu-1" and len(out.rows) == 2


def test_crypto_tools_refuse_stock_tickers(specs, ctx):
    with pytest.raises(ValidationError, match="'AAPL' is not a crypto pair"):
        call(specs["crypto_bars"], ctx, tickers=["AAPL"])


def test_crypto_quotes_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/quotes", "CryptoQuotes")
    out = call(specs["crypto_quotes"], ctx, tickers=["BTC/USD"])
    doc = check_golden(out, "crypto_quotes")
    assert alpaca.params()["start"] == "2026-10-02T19:45:00Z"
    assert set(doc["absent"]) == {"bid_exchange", "ask_exchange", "conditions", "tape"}
    assert doc["rows"][1]["absent"] == {"bid_price": "no_data", "bid_size": "no_data"}


def test_crypto_trades_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/trades", "CryptoTrades")
    out = call(specs["crypto_trades"], ctx, tickers=["ETH/USD"], lookback="PT1H")
    doc = check_golden(out, "crypto_trades")
    assert [r["taker_side"] for r in doc["rows"]] == ["sell", "buy"]
    assert set(doc["absent"]) == {"exchange", "conditions", "tape"}


def test_crypto_latest_bars_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/latest/bars", "CryptoLatestBars")
    out = call(specs["crypto_latest_bars"], ctx, tickers=["BTC/USD"])
    check_golden(out, "crypto_latest_bars")
    assert alpaca.params() == {"symbols": "BTC/USD"}
    missing = call(specs["crypto_latest_bars"], ctx, tickers=["BTC/USD", "ZZZ/USD"])
    assert "No data from Alpaca for: ZZZ/USD." in missing.notes


NO_TRADES = (
    "Alpaca built them from quotes, so their prices and vwap are not trade prices; "
    "trade_count > 0 keeps the traded bars."
)


def test_crypto_latest_bars_note_the_pairs_whose_bar_had_no_trades(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/latest/bars", "CryptoLatestBars__no_trades")
    out = call(specs["crypto_latest_bars"], ctx, tickers=["BTC/USD", "LTC/USD", "ETH/USD"])
    # The rows stay as Alpaca sent them; only the note is added.
    assert [(r.ticker, r.volume, r.trade_count, r.vwap) for r in out.rows] == [
        ("BTC/USD", 0.51, 41, 62147.2),
        ("LTC/USD", 0.0, 0, 66.0256),
        ("ETH/USD", 0.0, 0, 2576.45),
    ]
    assert out.notes == [
        f"2 of 3 bar(s) had no trades (trade_count 0, volume 0): LTC/USD, ETH/USD. {NO_TRADES}"
    ]


def test_crypto_bars_note_the_bars_without_trades_per_pair(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/bars", "CryptoBars__no_trades")
    out = call(specs["crypto_bars"], ctx, tickers=["BTC/USD", "ETH/USD"], timeframe="1min")
    assert len(out.rows) == 4
    assert out.notes == [
        f"3 of 4 bar(s) had no trades (trade_count 0, volume 0): ETH/USD (2), BTC/USD. {NO_TRADES}"
    ]


def test_no_trades_note_names_five_pairs_at_most():
    from marketlens_mcp.providers.alpaca import mappers
    from marketlens_mcp.providers.alpaca.tools.crypto import no_trades_note

    raw = {"t": "2026-10-02T19:57:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 0, "n": 0, "vw": 1}
    pairs = [f"C{i}/USD" for i in range(7)]
    rows = [mappers.bar(raw, ticker=p, asset_class="crypto", timeframe="1min") for p in pairs]
    (note,) = no_trades_note(rows)
    assert note.startswith(
        "7 of 7 bar(s) had no trades (trade_count 0, volume 0): C0/USD, C1/USD, C2/USD, C3/USD, C4/USD "
        "and 2 more pair(s). "
    )
    assert no_trades_note([]) == []


def test_crypto_latest_quotes_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/latest/quotes", "CryptoLatestQuotes")
    check_golden(call(specs["crypto_latest_quotes"], ctx, tickers=["BTC/USD"]), "crypto_latest_quotes")


def test_crypto_latest_trades_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/latest/trades", "CryptoLatestTrades")
    doc = check_golden(
        call(specs["crypto_latest_trades"], ctx, tickers=["BTC/USD", "ETH/BTC"]), "crypto_latest_trades"
    )
    # prices are in the pair's quote currency
    assert [(r["ticker"], r["currency"]) for r in doc["rows"]] == [("BTC/USD", "USD"), ("ETH/BTC", "BTC")]


def test_crypto_snapshots_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/snapshots", "CryptoSnapshots")
    out = call(specs["crypto_snapshots"], ctx, tickers=["BTC/USD", "ETH/USD"])
    doc = check_golden(out, "crypto_snapshots")
    assert doc["rows"][0]["change"] == pytest.approx(1150.75)
    assert any("ETH/USD" in n for n in doc["notes"])


def test_crypto_orderbooks_golden_sorted_levels_and_depth(specs, ctx, alpaca):
    alpaca.fixture("/v1beta3/crypto/us/latest/orderbooks", "CryptoLatestOrderbooks")
    out = call(specs["crypto_orderbooks"], ctx, tickers=["BTC/USD"], depth=2)
    doc = check_golden(out, "crypto_orderbooks")
    bids = [(r["level"], r["price"]) for r in doc["rows"] if r["side"] == "bid"]
    asks = [(r["level"], r["price"]) for r in doc["rows"] if r["side"] == "ask"]
    assert bids == [(0, 62149.5), (1, 62140.0)]
    assert asks == [(0, 62151.0), (1, 62160.0)]
    assert any("size 0" in n for n in doc["notes"])


def test_market_movers_crypto_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/screener/crypto/movers", "Movers__crypto")
    out = call(specs["market_movers"], ctx, market_type="crypto", top=1)
    doc = check_golden(out, "market_movers__crypto")
    assert [r["ticker"] for r in doc["rows"]] == ["SOL/USD", "DOGE/USD"]


def test_market_movers_crypto_min_price_counts_only_what_it_dropped(specs, ctx, alpaca):
    alpaca.add(
        "/v1beta1/screener/crypto/movers",
        {
            "gainers": [
                {"symbol": "SOL/USD", "percent_change": 8.5, "change": 11.9, "price": 151.9},
                {"symbol": "PEPE/USD", "percent_change": 6.1, "change": 0.0000006, "price": 0.0000104},
            ],
            "losers": [],
            "market_type": "crypto",
            "last_updated": "2026-10-02T19:55:00Z",
        },
    )
    out = call(specs["market_movers"], ctx, market_type="crypto", top=10, min_price=1)
    assert [r.ticker for r in out.rows] == ["SOL/USD"]
    # Not "1 of 2 gainers and 0 of 0 losers", nor "0 of 10 losers passed the filters".
    assert out.notes == [
        "Filters dropped 1 of 2 gainers Alpaca ranked: 1 priced below min_price 1.",
        "Alpaca ranked 2 gainers and no losers, fewer than the 10 asked for.",
    ]
