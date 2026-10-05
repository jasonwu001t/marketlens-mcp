"""Crypto tools: crypto_bars, crypto_quotes, crypto_trades, crypto_latest_bars,
crypto_latest_quotes, crypto_latest_trades, crypto_snapshots, crypto_orderbooks;
plus market_movers for crypto."""

from __future__ import annotations

import pytest
from alpaca_harness import FakeContext, call, check_golden
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
