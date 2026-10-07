"""Option tools: options_bars, options_trades, options_latest_trades,
options_latest_quotes, options_snapshots, options_chain."""

from __future__ import annotations

import pytest
from alpaca_harness import FakeContext, call, check_golden
from pydantic import ValidationError

C1, C2, P1 = "AAPL261016C00250000", "AAPL261016C00260000", "AAPL261016P00240000"
CHAIN_CURSOR = "Q0hBSU58Mg=="  # synthetic next-page cursor in OptionChain__page1


def test_options_bars_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/bars", "OptionBars")
    out = call(specs["options_bars"], ctx, occ_symbols=[C1.lower()])
    doc = check_golden(out, "options_bars")
    assert alpaca.params() == {
        "symbols": C1,
        "timeframe": "1Day",
        "start": "2026-09-02T00:00:00Z",  # P30D back from now, floored to the UTC day
        "sort": "asc",
        "limit": "10000",
    }
    assert {(r["occ_symbol"], r["underlying"]) for r in doc["rows"]} == {(C1, "AAPL")}


def test_options_refuse_a_non_occ_symbol(specs, ctx):
    with pytest.raises(ValidationError, match="'AAPL'"):
        call(specs["options_bars"], ctx, occ_symbols=["AAPL"])


def test_options_trades_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/trades", "OptionTrades")
    out = call(specs["options_trades"], ctx, occ_symbols=[C1])
    check_golden(out, "options_trades")
    assert alpaca.params()["start"] == "2026-10-01T20:00:00Z"


def test_options_latest_trades_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/trades/latest", "OptionLatestTrades")
    out = call(specs["options_latest_trades"], ctx, occ_symbols=[C1])
    doc = check_golden(out, "options_latest_trades")
    assert alpaca.params() == {"symbols": C1, "feed": "indicative"}
    assert (doc["provenance"]["feed"], doc["provenance"]["delay"]) == ("indicative", "delayed")
    missing = call(specs["options_latest_trades"], ctx, occ_symbols=[C1, P1])
    assert f"No data from Alpaca for: {P1}." in missing.notes


def test_options_latest_quotes_golden(specs, alpaca):
    alpaca.fixture("/v1beta1/options/quotes/latest", "OptionLatestQuotes")
    out = call(
        specs["options_latest_quotes"], FakeContext(settings={"options_feed": "opra"}), occ_symbols=[C1, C2]
    )
    doc = check_golden(out, "options_latest_quotes")
    assert alpaca.params()["feed"] == "opra" and doc["provenance"]["delay"] == "realtime"
    assert doc["rows"][1]["bid_price"] is None and doc["rows"][1]["absent"]["bid_price"] == "no_data"


def test_options_snapshots_golden_greeks_and_iv(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots", "OptionSnapshots")
    out = call(specs["options_snapshots"], ctx, occ_symbols=[C1, C2])
    doc = check_golden(out, "options_snapshots")
    full, thin = doc["rows"]
    assert (full["expiration_date"], full["strike"], full["option_type"]) == ("2026-10-16", 250.0, "call")
    assert full["implied_volatility"] == 0.2856 and full["delta"] == 0.6123 and full["theta"] == -0.1342
    assert thin["delta"] is None and thin["absent"]["delta"] == "not_provided_by_source"
    assert thin["bid_price"] is None and thin["absent"]["bid_price"] == "no_data"
    assert alpaca.params()["limit"] == "1000"


def test_options_chain_golden_two_pages_and_filters(specs, ctx, alpaca):
    alpaca.fixture(
        "/v1beta1/options/snapshots/AAPL", "OptionChain__page2", match={"page_token": CHAIN_CURSOR}
    )
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page1")
    out = call(
        specs["options_chain"],
        ctx,
        underlying="aapl",
        expiration_from="2026-10-01",
        expiration_to="2026-10-31",
        strike_min=200,
        strike_max=300,
    )
    doc = check_golden(out, "options_chain")
    assert alpaca.params(0) == {
        "feed": "indicative",
        "limit": "1000",
        "expiration_date_gte": "2026-10-01",
        "expiration_date_lte": "2026-10-31",
        "strike_price_gte": "200.0",
        "strike_price_lte": "300.0",
    }
    assert alpaca.params(1)["page_token"] == CHAIN_CURSOR
    assert [r["occ_symbol"] for r in doc["rows"]] == [C1, P1]
    assert {r["underlying"] for r in doc["rows"]} == {"AAPL"}


def test_options_chain_maps_share_classes_into_the_path(specs, ctx, alpaca):
    alpaca.add("/v1beta1/options/snapshots/BRK.B", {"snapshots": {}, "next_page_token": None})
    out = call(specs["options_chain"], ctx, underlying="BRK-B", option_type="put")
    assert out.rows == [] and alpaca.params()["type"] == "put"
