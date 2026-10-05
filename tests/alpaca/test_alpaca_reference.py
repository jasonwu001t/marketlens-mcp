"""Reference tools: reference_assets, reference_asset, reference_option_contracts,
reference_option_contract, reference_calendar, reference_clock,
reference_corporate_action_announcements, reference_corporate_action_announcement,
reference_corporate_actions, reference_option_exchanges."""

from __future__ import annotations

import httpx
import pytest
from alpaca_harness import FakeContext, call, check_golden, load_fixture
from pydantic import ValidationError

from marketlens_mcp.plugin_api import ToolError
from marketlens_schema.base import Environment

CONTRACTS_CURSOR = "T1BUQ3wy"  # synthetic cursors in the fixtures
CA_CURSOR = "Q0F8Mg=="


def test_reference_assets_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/assets", "Assets")
    out = call(specs["reference_assets"], ctx, status="active", attributes=["has_options"])
    doc = check_golden(out, "reference_assets")
    assert alpaca.requests[0].url.host == "paper-api.alpaca.markets"
    assert alpaca.params() == {"status": "active", "attributes": "has_options"}
    aapl, brk, btc = doc["rows"]
    assert aapl["margin_requirement_long"] == pytest.approx(0.30)
    assert brk["ticker"] == "BRK-B" and brk["easy_to_borrow"] is True
    assert btc["ticker"] == "BTC/USD" and btc["min_order_size"] == "0.0001"
    assert aapl["absent"]["min_order_size"] == "not_applicable"
    assert btc["absent"]["margin_requirement_long"] == "not_applicable"
    assert doc["provenance"]["delay"] == "unknown"


def test_reference_assets_use_the_live_base_in_live_mode(specs, alpaca):
    alpaca.fixture("/v2/assets", "Assets")
    call(specs["reference_assets"], FakeContext(portfolio_environment=Environment.LIVE))
    assert alpaca.requests[0].url.host == "api.alpaca.markets"


def test_reference_asset_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/assets/BRK.B", "Asset__brk")
    out = call(specs["reference_asset"], ctx, ticker="brk.b")
    check_golden(out, "reference_asset")


def test_reference_asset_crypto_pair_path_has_no_slash(specs, ctx, alpaca):
    alpaca.add("/v2/assets/BTCUSD", load_fixture("Assets")[2])
    out = call(specs["reference_asset"], ctx, ticker="BTC/USD")
    assert out.rows[0].ticker == "BTC/USD"


def test_reference_asset_not_found(specs, ctx, alpaca):
    alpaca.add("/v2/assets/ZZZZ", httpx.Response(404, json={"code": 40410000, "message": "asset not found"}))
    with pytest.raises(ToolError) as e:
        call(specs["reference_asset"], ctx, ticker="ZZZZ")
    assert e.value.code == "alpaca_rejected" and "asset not found" in e.value.message


def test_reference_option_contracts_golden_two_pages(specs, ctx, alpaca):
    alpaca.fixture("/v2/options/contracts", "OptionContracts__page2", match={"page_token": CONTRACTS_CURSOR})
    alpaca.fixture("/v2/options/contracts", "OptionContracts__page1")
    out = call(
        specs["reference_option_contracts"],
        ctx,
        underlyings=["AAPL", "BRK-B"],
        expiration_to="2026-10-31",
        strike_min=100,
    )
    doc = check_golden(out, "reference_option_contracts")
    assert alpaca.params(0) == {
        "underlying_symbols": "AAPL,BRK.B",
        "expiration_date_lte": "2026-10-31",
        "strike_price_gte": "100.0",
        "show_deliverables": "false",
        "limit": "10000",
    }
    assert [r["underlying"] for r in doc["rows"]] == ["AAPL", "BRK-B"]
    assert doc["absent"]["deliverables"]["code"] == "not_provided_by_source"
    assert doc["rows"][1]["absent"]["open_interest"] == "not_provided_by_source"


def test_reference_option_contract_golden_with_deliverables(specs, ctx, alpaca):
    alpaca.fixture("/v2/options/contracts/AAPL261016C00250000", "OptionContract")
    out = call(specs["reference_option_contract"], ctx, occ_symbol="AAPL261016C00250000")
    doc = check_golden(out, "reference_option_contract")
    d = doc["rows"][0]["deliverables"][0]
    assert (d["type"], d["ticker"], d["amount"], d["allocation_pct"]) == ("equity", "AAPL", "100", 1.0)


def test_reference_calendar_golden_converts_new_york_times(specs, ctx, alpaca):
    alpaca.fixture("/v2/calendar", "Calendar")
    out = call(specs["reference_calendar"], ctx, start="2025-12-23", end="2026-06-24")
    doc = check_golden(out, "reference_calendar")
    assert alpaca.params() == {"start": "2025-12-23", "end": "2026-06-24"}
    normal, early, summer = doc["rows"]
    assert (normal["open"], normal["close"]) == ("2025-12-23T14:30:00Z", "2025-12-23T21:00:00Z")
    assert early["close"] == "2025-12-24T18:00:00Z"
    assert (summer["open"], summer["session_close"]) == ("2026-06-24T13:30:00Z", "2026-06-25T00:00:00Z")


def test_reference_calendar_defaults_to_the_next_month(specs, ctx, alpaca):
    alpaca.fixture("/v2/calendar", "Calendar")
    call(specs["reference_calendar"], ctx)
    assert alpaca.params() == {"start": "2026-10-02", "end": "2026-11-02"}


def test_reference_calendar_default_end_stops_at_the_last_date(specs, ctx, alpaca):
    alpaca.fixture("/v2/calendar", "Calendar")
    call(specs["reference_calendar"], ctx, start="9999-12-20")
    assert alpaca.params() == {"start": "9999-12-20", "end": "9999-12-31"}


def test_reference_clock_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/clock", "Clock")
    doc = check_golden(call(specs["reference_clock"], ctx), "reference_clock")
    row = doc["rows"][0]
    assert (row["t"], row["next_close"]) == ("2026-10-02T18:15:22.123456Z", "2026-10-02T20:00:00Z")
    assert doc["provenance"]["as_of"] == "2026-10-02T18:15:22.123456Z"


def test_reference_corporate_action_announcements_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/corporate_actions/announcements", "Announcements")
    out = call(
        specs["reference_corporate_action_announcements"],
        ctx,
        ca_types=["dividend", "merger"],
        since="2026-09-01",
        until="2026-09-30",
        ticker="brk.b",
    )
    doc = check_golden(out, "reference_corporate_action_announcements")
    assert alpaca.params() == {
        "ca_types": "Dividend,Merger",
        "since": "2026-09-01",
        "until": "2026-09-30",
        "symbol": "BRK.B",
    }
    assert [r["ca_type"] for r in doc["rows"]] == ["dividend", "other"]
    assert doc["rows"][1]["initiating_ticker"] == "BRK-B"


def test_announcement_windows_over_90_days_are_refused(specs, ctx):
    with pytest.raises(ValidationError, match="90 days"):
        call(
            specs["reference_corporate_action_announcements"],
            ctx,
            ca_types=["split"],
            since="2026-01-01",
            until="2026-06-01",
        )


def test_reference_corporate_action_announcement_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/corporate_actions/announcements/be3c368a-4c7c-4384-808e-f02c9f5a8afe", "Announcement")
    out = call(
        specs["reference_corporate_action_announcement"],
        ctx,
        announcement_id="be3c368a-4c7c-4384-808e-f02c9f5a8afe",
    )
    doc = check_golden(out, "reference_corporate_action_announcement")
    assert doc["rows"][0]["cash"] == "0.018"


def test_reference_corporate_actions_golden_one_row_per_action(specs, ctx, alpaca):
    alpaca.fixture("/v1/corporate-actions", "CorporateActions__page2", match={"page_token": CA_CURSOR})
    alpaca.fixture("/v1/corporate-actions", "CorporateActions__page1")
    out = call(
        specs["reference_corporate_actions"],
        ctx,
        start="2025-12-01",
        end="2026-09-30",
        types=[
            "cash_dividend",
            "forward_split",
            "name_change",
            "stock_and_cash_merger",
            "spin_off",
            "capital_gains_distribution",
            "rights_distribution",
        ],
    )
    doc = check_golden(out, "reference_corporate_actions")
    assert alpaca.params(0)["types"].startswith("cash_dividend,forward_split")
    assert alpaca.params(0)["limit"] == "1000"
    by_type = {r["action_type"]: r for r in doc["rows"]}
    assert (
        by_type["cash_dividend"]["rate"] == 0.125
        and by_type["cash_dividend"]["absent"]["old_rate"] == "not_applicable"
    )
    assert (
        by_type["cash_dividend"]["isin"] is None
        and by_type["cash_dividend"]["absent"]["isin"] == "not_provided_by_source"
    )
    assert (by_type["forward_split"]["old_rate"], by_type["forward_split"]["new_rate"]) == (1.0, 2.0)
    assert (by_type["name_change"]["old_ticker"], by_type["name_change"]["new_ticker"]) == ("BSAQ", "VFS")
    m = by_type["stock_and_cash_merger"]
    assert (m["ticker"], m["acquirer_ticker"], m["new_rate"], m["cash_rate"]) == ("MLVF", "FRBA", 0.7733, 7.8)
    assert by_type["spin_off"]["old_rate"] == 19.35 and by_type["spin_off"]["new_ticker"] == "SRM"
    assert by_type["capital_gains_distribution"]["rate"] == pytest.approx(0.56874)
    assert by_type["rights_distribution"]["new_ticker"] == "IFN-RTWI"


def test_corporate_action_ids_exclude_other_filters(specs, ctx):
    with pytest.raises(ValidationError, match="ids"):
        call(
            specs["reference_corporate_actions"],
            ctx,
            ids=["11cfd108-292e-4cc6-bfbf-5999cdbc4029"],
            tickers=["FCF"],
        )


def test_reference_option_exchanges_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1beta1/options/meta/exchanges", "OptionMetaExchanges")
    doc = check_golden(call(specs["reference_option_exchanges"], ctx), "reference_option_exchanges")
    assert [r["code"] for r in doc["rows"]] == ["A", "Q"]
