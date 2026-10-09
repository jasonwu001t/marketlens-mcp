"""Option tools: options_bars, options_trades, options_latest_trades,
options_latest_quotes, options_snapshots, options_chain."""

from __future__ import annotations

import re

import pytest
from alpaca_harness import FakeContext, call, check_golden
from pydantic import ValidationError

from marketlens_mcp.plugin_api import ToolError

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
    prov = doc["provenance"]
    assert (prov["feed"], prov["delay"], prov["as_of"]) == ("opra", "delayed", "2026-10-02T19:45:00Z")


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


ADJ = "ETHA1261009C00005000"  # an adjusted contract: its root carries a digit


def test_adjusted_contracts_are_option_symbols_with_their_underlying(specs, ctx, alpaca):
    bar = {"t": "2026-10-01T04:00:00Z", "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 3, "n": 2, "vw": 1.05}
    alpaca.add("/v1beta1/options/bars", {"bars": {ADJ: [bar]}, "next_page_token": None})
    out = call(specs["options_bars"], ctx, occ_symbols=[ADJ.lower()])
    assert alpaca.params()["symbols"] == ADJ
    assert [(r.occ_symbol, r.underlying) for r in out.rows] == [(ADJ, "ETHA")]


@pytest.mark.parametrize(("tool", "path", "fixture"), [
    ("options_bars", "/v1beta1/options/bars", "OptionBars"),
    ("options_trades", "/v1beta1/options/trades", "OptionTrades"),
])  # fmt: skip
def test_option_history_ends_15_minutes_ago_without_realtime_opra(specs, ctx, alpaca, tool, path, fixture):
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, occ_symbols=[C1], start="2026-10-01", end="2026-10-03")
    assert alpaca.params()["end"] == "2026-10-02T19:45:00Z"  # now (20:00Z) minus 15 minutes
    assert out.provenance.request["end"] == "2026-10-02T19:45:00Z"
    assert (out.provenance.feed, out.provenance.delay, out.provenance.as_of.isoformat()) == (
        "opra",
        "delayed",
        "2026-10-02T19:45:00+00:00",
    )
    assert any(n.startswith("end 2026-10-03T00:00:00Z is later than Alpaca serves") for n in out.notes)
    assert "15 minutes" in specs[tool].description


def test_option_history_with_realtime_opra_keeps_the_end(specs, alpaca):
    alpaca.fixture("/v1beta1/options/trades", "OptionTrades")
    opra = FakeContext(settings={"options_feed": "opra"})
    out = call(specs["options_trades"], opra, occ_symbols=[C1], start="2026-10-01", end="2026-10-03")
    assert alpaca.params()["end"] == "2026-10-03T00:00:00Z" and out.notes == []
    assert (out.provenance.feed, out.provenance.delay) == ("opra", "realtime")


def test_option_history_inside_the_last_15_minutes_is_refused_without_realtime_opra(specs, ctx, alpaca):
    with pytest.raises(ToolError) as e:
        call(specs["options_trades"], ctx, occ_symbols=[C1], start="2026-10-02T19:50:00Z")
    assert e.value.code == "invalid_arguments" and "within the last 15 minutes" in e.value.message
    assert alpaca.requests == []


def test_snapshot_day_bars_are_dated_and_a_stale_one_is_noted(specs, ctx, alpaca):
    def snap(day: str, prev: str, volume: int) -> dict:
        bar = {"o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "n": 1, "vw": 1.0}
        return {
            "latestTrade": {"t": f"{day}T15:28:00Z", "x": "C", "p": 1.0, "s": 1, "c": "a"},
            "dailyBar": {**bar, "t": f"{day}T04:00:00Z", "v": volume},
            "prevDailyBar": {**bar, "t": f"{prev}T04:00:00Z", "v": 2},
        }

    stale = {C1: snap("2026-10-02", "2026-10-01", 900), C2: snap("2026-08-31", "2026-08-14", 8)}
    alpaca.add("/v1beta1/options/snapshots", {"snapshots": stale, "next_page_token": None})
    out = call(specs["options_snapshots"], ctx, occ_symbols=[C1, C2])
    by = {r.occ_symbol: r for r in out.rows}
    assert by[C2].day_t.isoformat() == "2026-08-31T04:00:00+00:00" and by[C2].day_volume == 8
    assert by[C2].prev_t.isoformat() == "2026-08-14T04:00:00+00:00"
    note = (
        "day_* is each contract's latest daily bar: for 1 of 2 contracts with one it is from a session before "
        "2026-10-02 (day_t), so filter on day_t before summing day_volume across contracts."
    )
    assert note in out.notes
    for tool in ("options_snapshots", "options_chain"):
        assert "today's bar" not in specs[tool].description and "day_t" in specs[tool].description


def test_options_chain_with_nothing_listed_says_so(specs, ctx, alpaca):
    alpaca.add("/v1beta1/options/snapshots/ZZZZQ", {"snapshots": {}, "next_page_token": None})
    out = call(specs["options_chain"], ctx, underlying="zzzzq")
    assert out.rows == []
    assert any(n.startswith("No data from Alpaca for: ZZZZQ (no listed contract matches") for n in out.notes)


@pytest.mark.parametrize("tool", ["options_chain", "reference_option_contracts"])
@pytest.mark.parametrize(
    ("args", "says"),
    [
        ({"strike_min": 400, "strike_max": 300}, "strike_min (400) must not be above strike_max (300)"),
        (
            {"expiration_from": "2026-12-01", "expiration_to": "2026-11-01"},
            "expiration_from (2026-12-01) must not be after expiration_to (2026-11-01)",
        ),
    ],
)
def test_option_filters_refuse_an_inverted_range(specs, ctx, tool, args, says):
    first = {"underlying": "AAPL"} if tool == "options_chain" else {"underlyings": ["AAPL"]}
    with pytest.raises(ValidationError, match=re.escape(says)):
        call(specs[tool], ctx, **first, **args)
