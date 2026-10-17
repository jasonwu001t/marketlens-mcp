"""Option tools: options_bars, options_trades, options_latest_trades,
options_latest_quotes, options_snapshots, options_chain."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import pytest
from alpaca_harness import FakeContext, call, check_golden
from pydantic import ValidationError

from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_schema.base import Environment

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


@pytest.mark.parametrize(
    ("tool", "path", "fixture"),
    [
        ("options_bars", "/v1beta1/options/bars", "OptionBars"),
        ("options_trades", "/v1beta1/options/trades", "OptionTrades"),
    ],
)
def test_an_option_date_end_includes_that_new_york_day(specs, ctx, alpaca, tool, path, fixture):
    # Option daily bars are stamped at midnight New York (04:00Z), as stock bars are.
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, occ_symbols=[C1], start="2026-09-28", end="2026-10-01")
    assert alpaca.params()["end"] == "2026-10-02T03:59:59.999999Z"
    assert out.notes[0].startswith(
        "end 2026-10-01 is a date: the window includes that whole day, to midnight New York"
    )


@pytest.mark.parametrize(
    ("tool", "path", "fixture"),
    [
        ("options_bars", "/v1beta1/options/bars", "OptionBars"),
        ("options_trades", "/v1beta1/options/trades", "OptionTrades"),
    ],
)
def test_option_history_names_the_contracts_alpaca_returned_nothing_for(
    specs, ctx, alpaca, tool, path, fixture
):
    alpaca.fixture(path, fixture)
    out = call(specs[tool], ctx, occ_symbols=[C1, P1])
    assert f"No data from Alpaca for: {P1} in this window." in out.notes


def _bar(t: str, o: float, h: float, low: float, c: float, v: int, n: int) -> dict:
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v, "n": n, "vw": round((h + low + c) / 3, 4)}


def test_options_weekly_bar_past_end_is_rebuilt_from_its_days_to_end(specs, alpaca):
    # Alpaca's 1w bar of the week of 2026-10-05 holds Friday 10-09's trades even with end 2026-10-07.
    weeks = [
        _bar("2026-09-28T04:00:00Z", 14.65, 15.75, 7.4, 9.95, 8697, 2244),
        _bar("2026-10-05T04:00:00Z", 9.56, 13.55, 8.0, 10.5, 10358, 2670),
    ]
    days = [
        _bar("2026-10-05T04:00:00Z", 9.56, 11.07, 9.1, 9.3, 818, 260),
        _bar("2026-10-06T04:00:00Z", 9.1, 9.9, 8.22, 9.35, 733, 252),
        _bar("2026-10-07T04:00:00Z", 11.2, 12.1, 9.35, 11.25, 1813, 538),
    ]
    alpaca.add(
        "/v1beta1/options/bars", {"bars": {C1: weeks}, "next_page_token": None}, match={"timeframe": "1Week"}
    )
    alpaca.add(
        "/v1beta1/options/bars", {"bars": {C1: days}, "next_page_token": None}, match={"timeframe": "1Day"}
    )
    out = call(
        specs["options_bars"],
        FakeContext(fixed_now=datetime(2026, 10, 16, 20, tzinfo=UTC)),
        occ_symbols=[C1],
        timeframe="1w",
        start="2026-09-28",
        end="2026-10-07",
    )
    assert alpaca.params(1) == {
        "symbols": C1,
        "timeframe": "1Day",
        "start": "2026-10-05T04:00:00Z",
        "end": "2026-10-08T03:59:59.999999Z",
        "sort": "asc",
        "limit": "10000",
    }
    rows = [(r.open, r.high, r.low, r.close, r.volume, r.trade_count, r.vwap) for r in out.rows]
    assert rows == [
        (14.65, 15.75, 7.4, 9.95, 8697, 2244, 11.0333),  # a whole week before end, as Alpaca sent it
        (9.56, 12.1, 8.22, 11.25, 3364, 1050, None),  # Monday to Wednesday, not Friday's close 10.5
    ]
    assert out.notes[-1] == (
        f"The 1w bar from 2026-10-05T04:00:00Z ({C1}) runs to 2026-10-12T04:00:00Z, past end "
        "(2026-10-08T03:59:59.999999Z), and Alpaca's bar holds the trades after end: it is rebuilt from the 1d "
        "bars of 2026-10-05 through 2026-10-07 (New York dates), so it covers those days only; its vwap is null "
        "(insufficient_data), which the 1d bars do not give exactly."
    )


def test_options_refuse_a_non_occ_symbol(specs, ctx):
    with pytest.raises(ValidationError, match="'AAPL'"):
        call(specs["options_bars"], ctx, occ_symbols=["AAPL"])


def test_options_trades_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/trades", "OptionTrades")
    out = call(specs["options_trades"], ctx, occ_symbols=[C1])
    check_golden(out, "options_trades")
    assert alpaca.params()["start"] == "2026-10-01T19:45:00Z"  # P1D back from 15 minutes ago


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
    assert doc["absent"]["open_interest"]["code"] == "not_provided_by_source"
    assert "open_interest" not in (full.get("absent") or {})


def test_options_chain_golden_two_pages_and_filters(specs, ctx, alpaca):
    alpaca.fixture(
        "/v1beta1/options/snapshots/AAPL", "OptionChain__page2", match={"page_token": CHAIN_CURSOR}
    )
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page1")
    alpaca.fixture("/v2/options/contracts", "OptionContracts__chain")
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
    # open interest comes from the contract list, asked for what the rows span
    assert alpaca.requests[2].url.path == "/v2/options/contracts"
    assert alpaca.params(2) == {
        "underlying_symbols": "AAPL",
        "expiration_date_gte": "2026-10-16",
        "expiration_date_lte": "2026-10-16",
        "strike_price_gte": "240.0",
        "strike_price_lte": "250.0",
        "limit": "10000",
    }
    call_row, put_row = doc["rows"]
    assert (call_row["open_interest"], call_row["open_interest_date"]) == (12011, "2026-10-01")
    assert put_row["open_interest"] is None and put_row["absent"]["open_interest"] == "not_provided_by_source"


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
    assert any(
        n.startswith("end 2026-10-03 is later than the newest data the indicative options feed serves")
        for n in out.notes
    )
    assert "15 minutes" in specs[tool].description


def test_option_history_with_realtime_opra_keeps_a_recent_end_and_cuts_a_future_one(specs, alpaca):
    # With real-time OPRA the last 15 minutes are served: an end inside them is kept as given.
    alpaca.fixture("/v1beta1/options/trades", "OptionTrades")
    opra = FakeContext(settings={"options_feed": "opra"})
    out = call(
        specs["options_trades"], opra, occ_symbols=[C1], start="2026-10-01", end="2026-10-02T19:55:00Z"
    )
    assert alpaca.params()["end"] == "2026-10-02T19:55:00Z"
    assert not any("later than" in n or "in the future" in n for n in out.notes)
    assert (out.provenance.feed, out.provenance.delay) == ("opra", "realtime")
    # An end in the future is cut to now, as for every tool with a window (a date is a New York day).
    alpaca.fixture("/v1beta1/options/trades", "OptionTrades")
    out = call(specs["options_trades"], opra, occ_symbols=[C1], start="2026-10-01", end="2026-10-03")
    assert alpaca.params()["end"] == "2026-10-02T20:00:00Z"
    assert any(n.startswith("end 2026-10-03 is in the future") for n in out.notes)


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


def test_options_chain_open_interest_absences(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", {"option_contracts": [], "next_page_token": None})
    out = call(specs["options_chain"], ctx, underlying="AAPL")
    assert out.rows[0].absent["open_interest"] == "no_match"
    assert (
        "open_interest is null (no_match) for 1 contract(s) Alpaca's contract list did not return."
        in out.notes
    )


def test_options_chain_open_interest_cut_at_the_fetch_limit_is_withheld(specs, alpaca):
    # the contract list spans the rows' strike and expiration box, so it can hold more than the chain
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    listed = [{"symbol": s, "open_interest": "7", "open_interest_date": "2026-10-01"} for s in (C1, C2)]
    alpaca.add("/v2/options/contracts", {"option_contracts": listed, "next_page_token": "bW9yZQ=="})
    out = call(specs["options_chain"], FakeContext(limits=FetchLimits(max_rows=2)), underlying="AAPL")
    assert out.rows[0].occ_symbol == P1 and out.rows[0].absent["open_interest"] == "withheld"
    assert (
        "open_interest is null (withheld) for 1 contract(s): Alpaca's contract list stopped at the fetch "
        "limit after 2 contracts, before reaching them; narrow the chain by expiration, strike or "
        "option_type to fill them." in out.notes
    )
    assert not any("did not return" in n for n in out.notes)


def test_options_chain_names_the_account_its_open_interest_lookup_went_through(specs, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", {"option_contracts": [], "next_page_token": None})
    live = FakeContext(portfolio_environment=Environment.LIVE)
    out = call(specs["options_chain"], live, underlying="AAPL")
    assert alpaca.requests[-1].url.host == "api.alpaca.markets"
    assert out.provenance.environment == Environment.LIVE
    # an empty chain makes no trading-API call, so it names no account
    alpaca.add("/v1beta1/options/snapshots/ZZZZQ", {"snapshots": {}, "next_page_token": None})
    assert call(specs["options_chain"], live, underlying="ZZZZQ").provenance.environment is None


def test_options_chain_survives_a_failed_open_interest_lookup(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", httpx.Response(403, json={"message": "forbidden"}))
    out = call(specs["options_chain"], ctx, underlying="AAPL")
    assert out.rows[0].occ_symbol == P1 and out.rows[0].absent["open_interest_date"] == "source_error"
    assert any(
        n.startswith("open_interest is null: Alpaca's contract list failed (alpaca_auth") for n in out.notes
    )
