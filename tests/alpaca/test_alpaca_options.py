"""Option tools: options_bars, options_trades, options_latest_trades,
options_latest_quotes, options_snapshots, options_chain.

options_chain's default read (no expiration filter: the nearest listed
expiration only) is driven against ``FakeChain``, a synthetic chain behind
Alpaca's two endpoints with their filters and paging, so each test counts the
requests a read takes."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple
from urllib.parse import parse_qsl

import httpx
import pytest
from alpaca_harness import GOLDEN, TEST_KEYS, FakeContext, call, check_golden, load_fixture, output_doc
from pydantic import ValidationError

from marketlens_mcp import testing
from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_mcp.providers.alpaca.tools.options import GREEKS_NOTE
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
    alpaca.add("/v1beta1/options/trades", {"trades": {C1: [{"t": "2026-10-09T14:00:00Z", "p": 10.5}]}})
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
        "start": "2026-10-08T03:59:59.999999Z",
        "end": "2026-10-12T04:00:00Z",
        "limit": "1000",
    }
    assert alpaca.params(2) == {
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
        "(2026-10-08T03:59:59.999999Z), and trades after end fall in it: it is rebuilt from the 1d bars of "
        "2026-10-05 through 2026-10-07 (New York dates), so it covers those days only; its vwap is null "
        "(insufficient_data), which the 1d bars do not give exactly."
    )


def test_options_bars_name_the_stretch_from_start_no_bar_covers(specs, alpaca):
    alpaca.add("/v1beta1/options/bars", {"bars": {}, "next_page_token": None})
    out = call(
        specs["options_bars"],
        FakeContext(fixed_now=datetime(2026, 10, 16, 20, tzinfo=UTC)),
        occ_symbols=[C1],
        timeframe="1mo",
        start="2026-08-15",
        end="2026-09-30",
    )
    assert (
        "Alpaca leaves out the 1mo bar from 2026-08-01T04:00:00Z, which starts before start "
        "(2026-08-15T00:00:00Z): no bar covers start to 2026-09-01T04:00:00Z. A start of 2026-08-01T04:00:00Z "
        "takes in that whole bar."
    ) in out.notes


def test_the_newest_bar_of_a_delayed_feed_is_not_said_to_run_past_now(specs, ctx, alpaca):
    # the indicative feed serves bars to 19:45Z, 15 minutes before now: the 19:45 bar closed at 19:50Z
    bars = [_bar(t, 1, 2, 1, 2, 10, 1) for t in ("2026-10-02T19:40:00Z", "2026-10-02T19:45:00Z")]
    alpaca.add("/v1beta1/options/bars", {"bars": {C1: bars}, "next_page_token": None})
    out = call(specs["options_bars"], ctx, occ_symbols=[C1], timeframe="5min", lookback="PT1H")
    assert len(out.rows) == 2
    assert out.notes[-1] == (
        f"The 5min bar from 2026-10-02T19:45:00Z ({C1}) is the newest one served: its period runs to "
        "2026-10-02T19:50:00Z, after the newest data the feed serves (2026-10-02T19:45:00Z), so it holds the "
        "trades to then, and any later trade in its period would change it."
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
    out = call(specs["options_chain"], ctx, underlying="BRK-B", option_type="put", all_expirations=True)
    assert out.rows == [] and alpaca.params()["type"] == "put"
    # the default read asks the contract list for the same Alpaca symbol
    alpaca.add("/v2/options/contracts", {"option_contracts": [], "next_page_token": None})
    call(specs["options_chain"], ctx, underlying="BRK-B", option_type="put")
    assert alpaca.requests[-1].url.path == "/v2/options/contracts"
    assert (alpaca.params()["underlying_symbols"], alpaca.params()["type"]) == ("BRK.B", "put")


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


NO_CHAIN = (
    "No data from Alpaca for: ZZZZQ (no listed contract matches the underlying and filters; check the ticker and "
    "the filters)."
)


def test_options_chain_with_nothing_listed_says_so(specs, ctx, alpaca):
    alpaca.add("/v1beta1/options/snapshots/ZZZZQ", {"snapshots": {}, "next_page_token": None})
    alpaca.add("/v2/options/contracts", {"option_contracts": [], "next_page_token": None})
    # the default read: the contract list lists nothing from today on, so no snapshot is asked for
    out = call(specs["options_chain"], ctx, underlying="zzzzq")
    assert [r.url.path for r in alpaca.requests] == ["/v2/options/contracts"]
    assert alpaca.params() == {
        "underlying_symbols": "ZZZZQ",
        "expiration_date_gte": "2026-10-02",
        "limit": "10000",
    }
    assert out.rows == [] and out.notes == [GREEKS_NOTE, NO_CHAIN]
    assert out.pagination.complete and out.pagination.pages_fetched == 0
    assert "expiration" not in out.provenance.request
    # all_expirations reads the whole chain, as before: one snapshot request, no contract list
    out = call(specs["options_chain"], ctx, underlying="zzzzq", all_expirations=True)
    assert alpaca.requests[-1].url.path == "/v1beta1/options/snapshots/ZZZZQ" and len(alpaca.requests) == 2
    assert out.rows == [] and out.notes == [GREEKS_NOTE, NO_CHAIN]


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


def test_options_chain_sends_an_exact_expiration_without_the_range_that_holds_it(specs, ctx, alpaca):
    # Alpaca took expiration_date beside expiration_date_gte as the range's upper bound (three expirations).
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.fixture("/v2/options/contracts", "OptionContracts__chain")
    call(
        specs["options_chain"],
        ctx,
        underlying="AAPL",
        expiration="2026-10-16",
        expiration_from="2026-10-01",
        expiration_to="2026-10-31",
    )
    assert alpaca.params(0) == {"feed": "indicative", "limit": "1000", "expiration_date": "2026-10-16"}
    # an explicit expiration reads as before: the chain, then the open interest of what it spans
    assert [r.url.path for r in alpaca.requests] == [
        "/v1beta1/options/snapshots/AAPL",
        "/v2/options/contracts",
    ]


def test_options_chain_refuses_an_expiration_with_all_expirations(specs, ctx):
    with pytest.raises(
        ValidationError, match=re.escape("pass expiration (one expiration) or all_expirations")
    ):
        call(specs["options_chain"], ctx, underlying="AAPL", expiration="2026-10-16", all_expirations=True)


@pytest.mark.parametrize("tool", ["options_chain", "reference_option_contracts"])
@pytest.mark.parametrize(
    ("args", "says"),
    [
        (
            {"expiration": "2026-11-20", "expiration_from": "2026-12-01"},
            "expiration (2026-11-20) must not be before expiration_from (2026-12-01)",
        ),
        (
            {"expiration": "2026-11-20", "expiration_to": "2026-11-01"},
            "expiration (2026-11-20) must not be after expiration_to (2026-11-01)",
        ),
    ],
)
def test_option_filters_refuse_an_exact_expiration_outside_the_range(specs, ctx, tool, args, says):
    first = {"underlying": "AAPL"} if tool == "options_chain" else {"underlyings": ["AAPL"]}
    with pytest.raises(ValidationError, match=re.escape(says)):
        call(specs[tool], ctx, **first, **args)


def test_options_chain_open_interest_absences(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", {"option_contracts": [], "next_page_token": None})
    out = call(specs["options_chain"], ctx, underlying="AAPL", all_expirations=True)
    assert out.rows[0].absent["open_interest"] == "no_match"
    assert (
        "open_interest is null (no_match) for 1 contract(s) Alpaca's contract list did not return."
        in out.notes
    )


def test_options_chain_default_read_joins_open_interest_from_the_list_it_read(specs, ctx, alpaca):
    # the list names 2026-10-16 by its call C1 alone; the chain's put P1 of that day is no_match
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    listed = {**load_fixture("OptionContracts__chain")["option_contracts"][0]}
    alpaca.add("/v2/options/contracts", {"option_contracts": [listed], "next_page_token": None})
    out = call(specs["options_chain"], ctx, underlying="AAPL")
    assert [r.url.path for r in alpaca.requests] == [
        "/v2/options/contracts",
        "/v1beta1/options/snapshots/AAPL",
    ]
    assert alpaca.params() == {"feed": "indicative", "expiration_date": "2026-10-16", "limit": "1000"}
    assert out.rows[0].occ_symbol == P1 and out.rows[0].absent["open_interest"] == "no_match"
    assert (
        "open_interest is null (no_match) for 1 contract(s) Alpaca's contract list did not return."
        in out.notes
    )


def test_options_chain_open_interest_cut_at_the_fetch_limit_is_withheld(specs, alpaca):
    # the contract list spans the rows' strike and expiration box, so it can hold more than the chain
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    listed = [{"symbol": s, "open_interest": "7", "open_interest_date": "2026-10-01"} for s in (C1, C2)]
    alpaca.add("/v2/options/contracts", {"option_contracts": listed, "next_page_token": "bW9yZQ=="})
    out = call(
        specs["options_chain"],
        FakeContext(limits=FetchLimits(max_rows=2)),
        underlying="AAPL",
        all_expirations=True,
    )
    assert out.rows[0].occ_symbol == P1 and out.rows[0].absent["open_interest"] == "withheld"
    assert (
        "open_interest is null (withheld) for 1 contract(s): Alpaca's contract list stopped at the fetch "
        "limit after 2 contracts, before reaching them; narrow the chain by expiration, strike or "
        "option_type to fill them." in out.notes
    )
    assert not any("did not return" in n for n in out.notes)


def test_options_chain_names_the_account_its_contract_list_went_through(specs, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", {"option_contracts": [], "next_page_token": None})
    live = FakeContext(portfolio_environment=Environment.LIVE)
    out = call(specs["options_chain"], live, underlying="AAPL", all_expirations=True)
    assert alpaca.requests[-1].url.host == "api.alpaca.markets"
    assert out.provenance.environment == Environment.LIVE
    # with all_expirations an empty chain makes no trading-API call, so it names no account
    alpaca.add("/v1beta1/options/snapshots/ZZZZQ", {"snapshots": {}, "next_page_token": None})
    empty = call(specs["options_chain"], live, underlying="ZZZZQ", all_expirations=True)
    assert alpaca.requests[-1].url.host == "data.alpaca.markets" and empty.provenance.environment is None
    # the default read always asks the contract list first, so even an empty chain names the account
    empty = call(specs["options_chain"], live, underlying="ZZZZQ")
    assert alpaca.requests[-1].url.host == "api.alpaca.markets"
    assert empty.rows == [] and empty.provenance.environment == Environment.LIVE


def test_options_chain_survives_a_failed_open_interest_lookup(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", httpx.Response(403, json={"message": "forbidden"}))
    out = call(specs["options_chain"], ctx, underlying="AAPL", expiration="2026-10-16")
    assert out.rows[0].occ_symbol == P1 and out.rows[0].absent["open_interest_date"] == "source_error"
    assert any(
        n.startswith("open_interest is null: Alpaca's contract list failed (alpaca_auth") for n in out.notes
    )


def test_options_chain_reads_the_whole_chain_when_its_contract_list_fails(specs, ctx, alpaca):
    # The default read cannot name the expirations, so it reads every one (today's whole-chain read) and
    # takes the same failure for open interest instead of asking the failing list again.
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page2")
    alpaca.add("/v2/options/contracts", httpx.Response(403, json={"message": "forbidden"}))
    out = call(specs["options_chain"], ctx, underlying="AAPL")
    assert [r.url.path for r in alpaca.requests] == [
        "/v2/options/contracts",
        "/v1beta1/options/snapshots/AAPL",
    ]
    assert alpaca.params() == {"feed": "indicative", "limit": "1000"}
    assert out.rows[0].occ_symbol == P1 and out.rows[0].absent["open_interest"] == "source_error"
    failed = "(alpaca_auth: Alpaca refused the API keys (HTTP 403: forbidden)"
    assert out.notes[0].startswith(
        f"No expiration filter was given, but Alpaca's contract list, which names the expirations, failed {failed}"
    )
    assert out.notes[0].endswith("so every expiration was read, as with all_expirations=true.")
    assert out.notes[-1].startswith(f"open_interest is null: Alpaca's contract list failed {failed}")
    assert "expiration" not in out.provenance.request and out.provenance.environment == Environment.PAPER


# --- options_chain's default read: the nearest expiration --------------------------------------------------


class Contract(NamedTuple):
    expiration: date
    kind: str  # C or P
    strike: float
    root: str

    @property
    def symbol(self) -> str:
        return f"{self.root}{self.expiration:%y%m%d}{self.kind}{round(self.strike * 1000):08d}"


class FakeChain:
    """A synthetic chain, a call and a put per strike of each expiration, behind Alpaca's chain endpoint (at
    most 1,000 snapshots a page) and contract list (at most 10,000 contracts a page), each with Alpaca's
    filters (type, strike_price_gte/lte, expiration_date, expiration_date_gte/lte, root_symbol) and paging; a
    larger page is refused (422). Without an expiration filter the contract list stops at the coming
    weekend, as Alpaca's does. ``order`` is the order the list gives contracts in: Alpaca promises none."""

    def __init__(
        self,
        today: date,
        strikes: Mapping[date, tuple[float, ...]],
        *,
        root: str = "SPY",
        t: str = "2026-10-02T19:59:00Z",
        order: Callable[[Contract], object] | None = None,
    ):
        self.today, self.root, self.t = today, root, t
        self.contracts = sorted(
            (Contract(e, kind, float(s), root) for e, xs in strikes.items() for s in xs for kind in "CP"),
            key=lambda c: c.symbol,
        )
        self.listed = sorted(self.contracts, key=order) if order else self.contracts

    def __len__(self) -> int:
        return len(self.contracts)

    def serve(self, alpaca, underlying: str = "SPY") -> None:
        alpaca.add("/v2/options/contracts", self.contract_list)
        alpaca.add(f"/v1beta1/options/snapshots/{underlying}", self.chain)

    def _select(self, contracts: list[Contract], q: dict[str, str], *, listing: bool) -> list[Contract]:
        exact, lo, hi = q.get("expiration_date"), q.get("expiration_date_gte"), q.get("expiration_date_lte")
        if listing and not (exact or lo or hi):
            hi = (self.today + timedelta(days=6 - self.today.weekday())).isoformat()
        kind = {"call": "C", "put": "P"}.get(q.get("type", ""))
        low, high = float(q.get("strike_price_gte", "-inf")), float(q.get("strike_price_lte", "inf"))
        return [
            c
            for c in contracts
            if (not exact or c.expiration.isoformat() == exact)
            and (not lo or c.expiration.isoformat() >= lo)
            and (not hi or c.expiration.isoformat() <= hi)
            and (not kind or c.kind == kind)
            and low <= c.strike <= high
            and q.get("root_symbol", c.root) == c.root
        ]

    @staticmethod
    def _page(items: list[Contract], q: dict[str, str], maximum: int) -> tuple[list[Contract], str | None]:
        limit = int(q.get("limit", "100"))
        if not 1 <= limit <= maximum:
            raise ValueError(f"limit {limit} is outside 1..{maximum}")
        start = int(q.get("page_token", "o0")[1:])
        end = start + limit
        return items[start:end], (f"o{end}" if end < len(items) else None)

    def contract_list(self, request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(request.url.query.decode()))
        try:
            rows, token = self._page(self._select(self.listed, q, listing=True), q, 10_000)
        except ValueError as e:
            return httpx.Response(422, json={"message": str(e)})
        contracts = [
            {
                "symbol": c.symbol,
                "status": "active",
                "expiration_date": c.expiration.isoformat(),
                "root_symbol": c.root,
                "type": "call" if c.kind == "C" else "put",
                "strike_price": f"{c.strike:g}",
                "open_interest": str(round(c.strike)),
                "open_interest_date": "2026-10-01",
            }
            for c in rows
        ]
        return httpx.Response(200, json={"option_contracts": contracts, "next_page_token": token})

    def chain(self, request: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(request.url.query.decode()))
        try:
            rows, token = self._page(self._select(self.contracts, q, listing=False), q, 1000)
        except ValueError as e:
            return httpx.Response(422, json={"message": str(e)})
        quote = {"t": self.t, "bx": "C", "bp": 1.0, "bs": 10, "ax": "C", "ap": 1.1, "as": 12, "c": "A"}
        greeks = {"delta": 0.5, "gamma": 0.01, "theta": -0.1, "vega": 0.2, "rho": 0.01}
        snapshots = {
            c.symbol: {"latestQuote": quote, "impliedVolatility": 0.25, "greeks": greeks} for c in rows
        }
        return httpx.Response(200, json={"snapshots": snapshots, "next_page_token": token})


def asked(alpaca) -> list[tuple[str, dict[str, str]]]:
    return [(r.url.path, dict(parse_qsl(r.url.query.decode()))) for r in alpaca.requests]


FRIDAY = date(2026, 10, 2)  # the harness's NOW, 2026-10-02T20:00Z, is 16:00 that Friday in New York
LIST, SNAP = "/v2/options/contracts", "/v1beta1/options/snapshots/AAPL"


def aapl_chain(**kw) -> FakeChain:
    strikes = {
        date(2026, 10, 9): (240, 250, 260),
        date(2026, 10, 16): (240, 250, 260, 270),
        date(2026, 11, 20): (200, 250, 300),
    }
    return FakeChain(FRIDAY, strikes, root="AAPL", **kw)


def test_options_chain_reads_the_nearest_expiration_and_lists_the_others(specs, ctx, alpaca):
    aapl_chain().serve(alpaca, "AAPL")
    out = call(specs["options_chain"], ctx, underlying="AAPL")
    # the contract list names the expirations; only the nearest one's snapshots are read (one page)
    assert asked(alpaca) == [
        (LIST, {"underlying_symbols": "AAPL", "expiration_date_gte": "2026-10-02", "limit": "10000"}),
        (SNAP, {"feed": "indicative", "expiration_date": "2026-10-09", "limit": "1000"}),
    ]
    assert {r.expiration_date for r in out.rows} == {date(2026, 10, 9)} and len(out.rows) == 6
    # open interest comes from the list already read: no second contract-list request
    assert [(r.occ_symbol, r.open_interest) for r in out.rows[:2]] == [
        ("AAPL261009C00240000", 240),
        ("AAPL261009C00250000", 250),
    ]
    assert out.notes[:3] == [
        "No expiration filter was given, so only the nearest listed expiration was read: 2026-10-09, the first "
        "listed on or after 2026-10-02 (today in New York), 6 contracts listed. Ask for another with "
        "expiration=YYYY-MM-DD from the list below, or pass all_expirations=true for every one (3 expirations, "
        "20 contracts).",
        "Listed expirations from 2026-10-02 on, with their contracts: 2026-10-09 (6), 2026-10-16 (8), "
        "2026-11-20 (6).",
        GREEKS_NOTE,
    ]
    prov = out.provenance
    assert prov.request == {
        "underlying": "AAPL",
        "all_expirations": False,
        "feed": "indicative",
        "expiration": "2026-10-09",
    }
    assert (prov.environment, prov.pages_fetched, out.pagination.complete) == (Environment.PAPER, 1, True)
    # another listed expiration, asked for by name, is the explicit read, as before
    out = call(specs["options_chain"], ctx, underlying="AAPL", expiration="2026-10-16")
    span = {"expiration_date_gte": "2026-10-16", "expiration_date_lte": "2026-10-16"}
    assert asked(alpaca)[2:] == [
        (SNAP, {"feed": "indicative", "expiration_date": "2026-10-16", "limit": "1000"}),
        (LIST, {"underlying_symbols": "AAPL", **span, "strike_price_gte": "240.0", "strike_price_lte": "270.0",
                "limit": "10000"}),
    ]  # fmt: skip
    assert len(out.rows) == 8 and not any(n.startswith("No expiration filter") for n in out.notes)


def test_options_chain_filters_choose_among_the_contracts_they_match(specs, ctx, alpaca):
    # 2026-10-09 lists no put from 265 up: the nearest expiration with one is 2026-10-16
    aapl_chain().serve(alpaca, "AAPL")
    out = call(
        specs["options_chain"],
        ctx,
        underlying="AAPL",
        option_type="put",
        strike_min=265,
        strike_max=300,
        root_symbol="AAPL",
    )
    filters = {"type": "put", "strike_price_gte": "265.0", "strike_price_lte": "300.0", "root_symbol": "AAPL"}
    assert asked(alpaca) == [
        (
            LIST,
            {"underlying_symbols": "AAPL", **filters, "expiration_date_gte": "2026-10-02", "limit": "10000"},
        ),
        (SNAP, {"feed": "indicative", **filters, "expiration_date": "2026-10-16", "limit": "1000"}),
    ]
    assert [r.occ_symbol for r in out.rows] == ["AAPL261016P00270000"]
    assert out.notes[:2] == [
        "No expiration filter was given, so only the nearest listed expiration was read: 2026-10-16, the first on "
        "or after 2026-10-02 (today in New York) with a contract matching the filters, 1 contract listed. Ask for "
        "another with expiration=YYYY-MM-DD from the list below, or pass all_expirations=true for every one (2 "
        "expirations, 2 contracts).",
        "Listed expirations from 2026-10-02 on, with their contracts matching the filters: 2026-10-16 (1), "
        "2026-11-20 (1).",
    ]


def test_options_chain_on_a_0dte_day_reads_the_expiration_of_today_in_new_york(specs, alpaca):
    strikes = {
        date(2026, 11, 23): (700, 710),
        date(2026, 11, 24): (700, 710, 720),
        date(2026, 11, 25): (700,),
    }
    FakeChain(date(2026, 11, 23), strikes, t="2026-11-23T15:00:00Z").serve(alpaca)
    # 10:00 in New York, and 21:30 there (02:30 UTC the next day): both are Monday 2026-11-23 in New York
    for now in (datetime(2026, 11, 23, 15, 0, tzinfo=UTC), datetime(2026, 11, 24, 2, 30, tzinfo=UTC)):
        out = call(specs["options_chain"], FakeContext(fixed_now=now), underlying="SPY")
        assert alpaca.params(-2)["expiration_date_gte"] == "2026-11-23"
        assert alpaca.params()["expiration_date"] == "2026-11-23"
        assert {r.expiration_date for r in out.rows} == {date(2026, 11, 23)} and len(out.rows) == 4
        assert out.notes[0].startswith(
            "No expiration filter was given, so only the nearest listed expiration was read: 2026-11-23, which is "
            "today in New York (0DTE: it expires today), 4 contracts listed."
        )
        assert out.notes[1] == (
            "Listed expirations from 2026-11-23 on, with their contracts: 2026-11-23 (4), 2026-11-24 (6), "
            "2026-11-25 (2)."
        )


#: An index ETF's expirations on Sunday 2026-11-22 (29): daily for two weeks, then weekly, month-end,
#: quarterly and LEAPS.
ETF_EXPIRATIONS = [
    date.fromisoformat(d)
    for d in (
        "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02",
        "2026-12-03", "2026-12-04", "2026-12-11", "2026-12-18", "2026-12-24", "2026-12-31", "2027-01-15",
        "2027-01-29", "2027-02-19", "2027-03-19", "2027-03-31", "2027-04-16", "2027-05-21", "2027-06-17",
        "2027-06-30", "2027-09-17", "2027-09-30", "2027-12-17", "2028-01-21", "2028-06-16", "2028-12-15",
        "2029-01-19",
    )
]  # fmt: skip
SPY_LIST, SPY_SNAP = "/v2/options/contracts", "/v1beta1/options/snapshots/SPY"


@pytest.mark.parametrize(
    ("expirations", "strikes", "size", "before", "after"),
    [
        # SPY's chain as large as a whole read found it on 2026-11-21 (12,026 contracts), over 29 expirations
        (
            ETF_EXPIRATIONS,
            [217] + [207] * 28,
            12_026,
            {SPY_SNAP: 13, SPY_LIST: 2},
            {SPY_LIST: 2, SPY_SNAP: 1},
        ),
        # a chain of 37 snapshot pages (36,500 contracts over 50 weekly expirations)
        (
            [date(2026, 11, 23) + timedelta(weeks=i) for i in range(50)],
            [365] * 50,
            36_500,
            {SPY_SNAP: 37, SPY_LIST: 4},
            {SPY_LIST: 4, SPY_SNAP: 1},
        ),
    ],
    ids=["spy", "37-pages"],
)
def test_options_chain_request_count_for_an_spy_sized_chain(
    specs, alpaca, expirations, strikes, size, before, after
):
    sunday = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)
    grid = {e: tuple(range(600, 600 + n)) for e, n in zip(expirations, strikes, strict=True)}
    chain = FakeChain(sunday.date(), grid, t="2026-11-21T20:59:00Z")
    assert len(chain) == size
    chain.serve(alpaca)
    # before: the whole chain, every snapshot page, then the open interest of what it spans (fetch.max_pages
    # raised to 50 so the read is whole; at the default 20 a chain over 20 pages stops there, cut)
    whole = FakeContext(fixed_now=sunday, limits=FetchLimits(max_pages=50))
    out = call(specs["options_chain"], whole, underlying="SPY", all_expirations=True)
    assert Counter(r.url.path for r in alpaca.requests) == before and len(out.rows) == size
    # after: the contract list names the expirations, then the nearest one's snapshots fit one page
    alpaca.requests.clear()
    out = call(specs["options_chain"], FakeContext(fixed_now=sunday), underlying="SPY")
    assert Counter(r.url.path for r in alpaca.requests) == after
    assert {r.expiration_date for r in out.rows} == {expirations[0]} and len(out.rows) == 2 * strikes[0]
    assert all(r.open_interest is not None for r in out.rows)
    assert f"({len(expirations)} expirations, {size} contracts)" in out.notes[0]


def test_options_chain_all_expirations_reads_the_whole_chain_as_before(specs, ctx, alpaca):
    alpaca.fixture(
        "/v1beta1/options/snapshots/AAPL", "OptionChain__page2", match={"page_token": CHAIN_CURSOR}
    )
    alpaca.fixture("/v1beta1/options/snapshots/AAPL", "OptionChain__page1")
    alpaca.fixture("/v2/options/contracts", "OptionContracts__chain")
    out = call(specs["options_chain"], ctx, underlying="AAPL", all_expirations=True)
    # no expiration filter is sent: every snapshot page, then the open interest of what the rows span
    assert asked(alpaca) == [
        (SNAP, {"feed": "indicative", "limit": "1000"}),
        (SNAP, {"feed": "indicative", "limit": "1000", "page_token": CHAIN_CURSOR}),
        (LIST, {"underlying_symbols": "AAPL", "expiration_date_gte": "2026-10-16",
                "expiration_date_lte": "2026-10-16", "strike_price_gte": "240.0", "strike_price_lte": "250.0",
                "limit": "10000"}),
    ]  # fmt: skip
    # the same rows, notes and pagination as the range read of these fixtures (the golden)
    golden = json.loads((GOLDEN / "options_chain.json").read_text(encoding="utf-8"))
    doc = output_doc(out)
    assert (doc["rows"], doc["notes"], doc["pagination"]) == (
        golden["rows"],
        golden["notes"],
        golden["pagination"],
    )
    assert doc["provenance"]["request"] == {
        "underlying": "AAPL",
        "all_expirations": True,
        "feed": "indicative",
    }


def strike_first(c: Contract) -> tuple[float, int]:
    """Highest strike first, then the farthest expiration: an order that reaches the nearest expiration last."""
    return -c.strike, -c.expiration.toordinal()


def wide_aapl_chain() -> FakeChain:
    strikes = {
        date(2026, 10, 9): (240, 250),
        date(2026, 10, 16): (240, 250, 260, 270),
        date(2026, 11, 20): (250, 300, 350, 400),
    }
    return FakeChain(FRIDAY, strikes, root="AAPL", order=strike_first)


def test_options_chain_contract_list_cut_at_the_fetch_limit_is_asked_again_up_to_its_nearest_expiration(
    specs, alpaca
):
    # The first 12 contracts reach 2026-11-20 and 2026-10-16, not 2026-10-09; asked again up to 2026-10-16,
    # the list is whole (12 contracts) and names 2026-10-09, the nearest.
    wide_aapl_chain().serve(alpaca, "AAPL")
    out = call(specs["options_chain"], FakeContext(limits=FetchLimits(max_rows=12)), underlying="AAPL")
    assert asked(alpaca) == [
        (LIST, {"underlying_symbols": "AAPL", "expiration_date_gte": "2026-10-02", "limit": "12"}),
        (LIST, {"underlying_symbols": "AAPL", "expiration_date_gte": "2026-10-02",
                "expiration_date_lte": "2026-10-16", "limit": "12"}),
        (SNAP, {"feed": "indicative", "expiration_date": "2026-10-09", "limit": "12"}),
    ]  # fmt: skip
    assert len(out.rows) == 4 and all(r.open_interest is not None for r in out.rows)
    assert out.notes[2] == (
        "Alpaca's contract list stopped at the fetch limit (fetch.max_rows, fetch.max_pages), so the expirations "
        "above may be incomplete; narrow by strike_min, strike_max or option_type to list them all."
    )


def test_options_chain_says_when_a_cut_contract_list_leaves_the_nearest_expiration_unsure(specs, alpaca):
    # At 6 contracts both reads stop within 2026-11-20: it is read, and the answer says an earlier expiration
    # may exist, withholds the open interest the list did not reach, and says how to continue that expiration.
    wide_aapl_chain().serve(alpaca, "AAPL")
    six = FakeContext(limits=FetchLimits(max_rows=6))
    out = call(specs["options_chain"], six, underlying="AAPL")
    assert [p for _, p in asked(alpaca)][1]["expiration_date_lte"] == "2026-11-20"
    assert alpaca.params()["expiration_date"] == "2026-11-20" and out.pagination.next_page_token == "o6"
    assert out.notes[0].startswith("Stopped after 1 pages and 6 rows")
    assert out.notes[3] == (
        "Alpaca's contract list stopped at the fetch limit (fetch.max_rows, fetch.max_pages), so the expirations "
        "above may be incomplete; narrow by strike_min, strike_max or option_type to list them all. An expiration "
        "before 2026-11-20 may also be missing."
    )
    assert out.notes[4] == (
        'To continue, call options_chain again with expiration="2026-11-20" and the page_token, so the next page '
        "is of the same expiration."
    )
    withheld = [r.occ_symbol for r in out.rows if (r.absent or {}).get("open_interest") == "withheld"]
    assert withheld == ["AAPL261120C00250000", "AAPL261120P00250000"]
    assert out.notes[-1].startswith("open_interest is null (withheld) for 2 contract(s)")
    # continuing by name reads the rest of that expiration
    out = call(specs["options_chain"], six, underlying="AAPL", expiration="2026-11-20", page_token="o6")
    assert alpaca.params(-2) == {"feed": "indicative", "expiration_date": "2026-11-20", "limit": "6",
                                 "page_token": "o6"}  # fmt: skip
    assert [r.occ_symbol for r in out.rows] == ["AAPL261120P00350000", "AAPL261120P00400000"]


def test_options_chain_nearest_expiration_is_stored_with_its_expiration_notes(tmp_path, specs, alpaca):
    # Through the server's own offload: SPY's nearest expiration (434 rows) is too large to show inline, and
    # the stored result's marker carries the notes that say which expiration was read and list the others.
    sunday = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)
    grid = {e: tuple(range(600, 600 + (217 if i == 0 else 207))) for i, e in enumerate(ETF_EXPIRATIONS)}
    FakeChain(sunday.date(), grid, t="2026-11-21T20:59:00Z").serve(alpaca)
    ctx = testing.make_context(
        tool="options_chain",
        store=testing.temp_store(tmp_path / "store"),
        env=TEST_KEYS,
        capabilities=["market"],
        now=sunday,
    )
    resp = asyncio.run(testing.call_tool(specs["options_chain"], ctx, underlying="SPY"))
    assert (resp.kind, resp.row_count) == ("stored", 434)
    assert resp.notes[0].startswith(
        "No expiration filter was given, so only the nearest listed expiration was read: 2026-11-23, the first "
        "listed on or after 2026-11-22 (today in New York), 434 contracts listed."
    )
    assert resp.notes[1].startswith(
        "Listed expirations from 2026-11-22 on, with their contracts: 2026-11-23 (434), 2026-11-24 (414),"
    )
    assert resp.notes[1].endswith("2028-12-15 (414), 2029-01-19 (414).")
    assert resp.provenance.request["expiration"] == "2026-11-23"
