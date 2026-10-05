"""Brokerage-account reads (capability portfolio, off by default): portfolio_account,
portfolio_account_config, portfolio_history, portfolio_activities, portfolio_orders,
portfolio_order, portfolio_positions, portfolio_position, portfolio_broker_watchlists,
portfolio_broker_watchlist, portfolio_locates, portfolio_locate, portfolio_locate_quotes."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alpaca_harness import FakeContext, call, check_golden, load_fixture
from pydantic import ValidationError

from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_schema.base import Environment

LOCATES_CURSOR = "TE9DfDI="  # synthetic cursor in Locates__page1


def test_portfolio_account_golden_masks_the_number_and_keeps_exact_money(specs, ctx, alpaca):
    alpaca.fixture("/v2/account", "Account")
    doc = check_golden(call(specs["portfolio_account"], ctx), "portfolio_account")
    row = doc["rows"][0]
    assert row["account_number_masked"] == "****ABCD"
    assert "PA0102030ABCD" not in json.dumps(doc)
    assert (row["cash"], row["equity"], row["multiplier"]) == ("-23140.2", "103820.56", "4")
    assert row["environment"] == "paper" and doc["provenance"]["environment"] == "paper"
    assert doc["provenance"]["delay"] == "realtime"
    assert alpaca.requests[0].url.host == "paper-api.alpaca.markets"


def test_live_portfolio_reads_the_live_account(specs, alpaca):
    alpaca.fixture("/v2/account", "Account")
    out = call(specs["portfolio_account"], FakeContext(portfolio_environment=Environment.LIVE))
    assert alpaca.requests[0].url.host == "api.alpaca.markets"
    assert out.rows[0].environment == "live" and out.provenance.environment == "live"


def test_portfolio_account_config_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/account/configurations", "AccountConfig")
    doc = check_golden(call(specs["portfolio_account_config"], ctx), "portfolio_account_config")
    assert doc["rows"][0]["max_margin_multiplier"] == "4"


def test_portfolio_history_golden_one_row_per_timestamp(specs, ctx, alpaca):
    alpaca.fixture("/v2/account/portfolio/history", "PortfolioHistory")
    out = call(specs["portfolio_history"], ctx, period="1W", timeframe="1d", pnl_reset="no_reset")
    doc = check_golden(out, "portfolio_history")
    assert alpaca.params() == {"period": "1W", "timeframe": "1D", "pnl_reset": "no_reset"}
    rows = doc["rows"]
    assert [r["equity"] for r in rows] == ["8425.21", "8639.77", None, "8835.31"]
    assert rows[2]["absent"] == {"equity": "no_data", "profit_loss": "no_data", "profit_loss_pct": "no_data"}
    assert rows[0]["base_value"] == "8413.04" and rows[0]["timeframe"] == "1d"
    assert rows[0]["t"] == "2026-09-28T00:00:00Z"


@pytest.mark.parametrize(("tf", "sent"), [("1min", "1Min"), ("15min", "15Min"), ("1h", "1H")])
def test_portfolio_history_timeframes(specs, ctx, alpaca, tf, sent):
    alpaca.fixture("/v2/account/portfolio/history", "PortfolioHistory")
    call(specs["portfolio_history"], ctx, timeframe=tf, start="2026-09-28", end="2026-10-02")
    assert alpaca.params() == {
        "timeframe": sent,
        "start": "2026-09-28T00:00:00Z",
        "end": "2026-10-02T00:00:00Z",
    }


def test_portfolio_activities_golden_trade_and_non_trade(specs, ctx, alpaca):
    alpaca.add(
        "/v2/account/activities", load_fixture("Activities__page1") + load_fixture("Activities__page2")
    )
    doc = check_golden(call(specs["portfolio_activities"], ctx), "portfolio_activities")
    assert alpaca.params() == {"direction": "desc", "page_size": "100"}
    fill, opt, div, csd = doc["rows"]
    assert (fill["category"], fill["ticker"], fill["price"], fill["absent"]["net_amount"]) == (
        "trade",
        "BRK-B",
        "174.78",
        "not_applicable",
    )
    assert (opt["ticker"], opt["occ_symbol"]) == ("AAPL", "AAPL261016C00250000")
    assert (div["category"], div["t"], div["date"], div["net_amount"]) == (
        "non_trade",
        "2026-09-09T00:00:00Z",
        "2026-09-09",
        "1.02",
    )
    assert div["absent"]["side"] == "not_applicable"
    assert csd["description"] == "Cash deposit" and csd["ticker"] is None


def _activities(n: int) -> list[dict]:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    return [
        {
            "activity_type": "FEE",
            "id": f"2026090100000{i:04d}::fee-{i}",
            "date": (base - timedelta(days=i)).date().isoformat(),
            "net_amount": "-0.01",
            "status": "executed",
        }
        for i in range(n)
    ]


def _serve(items: list[dict], size_param: str):
    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        start = 0
        if q.get("page_token"):
            start = [i["id"] for i in items].index(q["page_token"]) + 1
        return httpx.Response(200, json=items[start : start + int(q[size_param])])

    return handler


def test_portfolio_activities_pages_by_the_last_id(specs, ctx, alpaca):
    items = _activities(150)
    alpaca.add("/v2/account/activities", _serve(items, "page_size"))
    out = call(specs["portfolio_activities"], ctx, category="non_trade")
    assert len(out.rows) == 150 and out.pagination.pages_fetched == 2 and out.pagination.complete
    assert alpaca.params(1)["page_token"] == items[99]["id"]
    assert alpaca.params(0)["category"] == "non_trade_activity"


def test_portfolio_activities_row_cap_note(specs, alpaca):
    items = _activities(10)
    alpaca.add("/v2/account/activities", _serve(items, "page_size"))
    out = call(specs["portfolio_activities"], FakeContext(limits=FetchLimits(max_rows=3)))
    assert out.pagination.row_cap_hit and out.pagination.next_page_token == items[2]["id"]
    assert f'page_token="{items[2]["id"]}"' in out.notes[0]


def test_one_activity_type_uses_the_by_type_route(specs, ctx, alpaca):
    alpaca.fixture("/v2/account/activities/DIV", "Activities__div")
    out = call(specs["portfolio_activities"], ctx, activity_types=["div"], after="2026-09-01")
    assert out.provenance.operation == "getAccountActivitiesByActivityType"
    assert out.provenance.route == "GET /v2/account/activities/{activity_type}"
    assert alpaca.params() == {"after": "2026-09-01T00:00:00Z", "direction": "desc", "page_size": "100"}


def test_category_and_types_together_are_refused(specs, ctx):
    with pytest.raises(ValidationError, match="category"):
        call(specs["portfolio_activities"], ctx, activity_types=["FILL"], category="trade")


def test_unknown_activity_type_is_refused(specs, ctx):
    with pytest.raises(ValidationError, match="'NOPE'"):
        call(specs["portfolio_activities"], ctx, activity_types=["NOPE"])


def test_portfolio_orders_golden_with_multi_leg(specs, ctx, alpaca):
    alpaca.fixture("/v2/orders", "Orders__page1")
    doc = check_golden(call(specs["portfolio_orders"], ctx, status="all"), "portfolio_orders")
    assert alpaca.params() == {"status": "all", "direction": "desc", "nested": "true", "limit": "500"}
    equity, mleg = doc["rows"]
    assert (equity["ticker"], equity["order_class"], equity["limit_price"], equity["filled_qty"]) == (
        "BRK-B",
        "simple",
        "150",
        "0",
    )
    assert equity["absent"]["legs"] == "not_applicable"
    assert (mleg["asset_class"], mleg["ticker"], mleg["side"], mleg["occ_symbol"]) == (
        "us_option",
        "AAPL",
        None,
        None,
    )
    assert mleg["absent"]["occ_symbol"] == "not_applicable" and mleg["absent"]["side"] == "not_applicable"
    assert [(leg["occ_symbol"], leg["side"], leg["ratio_qty"]) for leg in mleg["legs"]] == [
        ("AAPL261016C00250000", "buy", "3"),
        ("AAPL261016C00260000", "sell", "1"),
    ]


def test_portfolio_orders_trailing_percent_is_a_fraction(specs, ctx, alpaca):
    alpaca.fixture("/v2/orders", "Orders__page2")
    out = call(specs["portfolio_orders"], ctx)
    o = out.rows[0]
    assert (o.ticker, o.trail_percent, str(o.hwm), o.order_type) == (
        "BTC/USD",
        0.015,
        "62000.5",
        "trailing_stop",
    )


def _orders(n: int) -> list[dict]:
    base = datetime(2026, 10, 1, 12, tzinfo=UTC)
    out = []
    for i in range(n):
        t = (base - timedelta(minutes=i)).isoformat().replace("+00:00", ".123456789Z")
        out.append(
            {
                "id": f"00000000-0000-4000-8000-{i:012d}",
                "asset_class": "us_equity",
                "symbol": "AAPL",
                "side": "buy",
                "type": "market",
                "order_class": "",
                "time_in_force": "day",
                "status": "filled",
                "qty": "1",
                "filled_qty": "1",
                "created_at": t,
                "submitted_at": t,
            }
        )
    return out


def test_portfolio_orders_page_by_an_opaque_until_token(specs, ctx, alpaca):
    items = _orders(520)

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        kept = [o for o in items if "until" not in q or o["submitted_at"] < q["until"]]
        return httpx.Response(200, json=kept[: int(q["limit"])])

    alpaca.add("/v2/orders", handler)
    out = call(specs["portfolio_orders"], ctx, status="closed")
    assert len(out.rows) == 520 and out.pagination.pages_fetched == 2 and out.pagination.complete
    assert alpaca.params(1)["until"] == items[499]["submitted_at"]


def test_portfolio_orders_resume_from_a_token(specs, alpaca):
    items = _orders(5)
    alpaca.add("/v2/orders", lambda request: httpx.Response(200, json=items[:2]))
    first = call(specs["portfolio_orders"], FakeContext(limits=FetchLimits(max_rows=2)))
    token = first.pagination.next_page_token
    assert token and items[1]["submitted_at"] not in token  # opaque
    alpaca.add("/v2/orders", lambda request: httpx.Response(200, json=[]))
    alpaca.routes.reverse()
    call(specs["portfolio_orders"], FakeContext(), page_token=token)
    assert alpaca.params()["until"] == items[1]["submitted_at"]


def test_a_forged_order_token_is_refused(specs, ctx, alpaca):
    bad = "o1." + base64.urlsafe_b64encode(b'{"evil": 1}').decode()
    with pytest.raises(ToolError) as e:
        call(specs["portfolio_orders"], ctx, page_token=bad)
    assert e.value.code == "invalid_page_token"
    assert alpaca.requests == []


def test_portfolio_order_golden_by_id(specs, ctx, alpaca):
    alpaca.fixture("/v2/orders/83f37e9f-6b1f-49ed-8fc6-3e6af716323f", "Order__mleg")
    out = call(specs["portfolio_order"], ctx, order_id="83f37e9f-6b1f-49ed-8fc6-3e6af716323f")
    check_golden(out, "portfolio_order")
    assert out.provenance.operation == "getOrderByOrderID" and alpaca.params() == {"nested": "true"}


def test_portfolio_order_by_client_order_id(specs, ctx, alpaca):
    alpaca.fixture("/v2/orders:by_client_order_id", "Order__equity")
    out = call(specs["portfolio_order"], ctx, client_order_id="5680c4bc-9ac1-4a12-a44c-df427ba53032")
    assert out.provenance.operation == "getOrderByClientOrderId"
    assert alpaca.params() == {"client_order_id": "5680c4bc-9ac1-4a12-a44c-df427ba53032"}


@pytest.mark.parametrize("args", [{}, {"order_id": "a", "client_order_id": "b"}])
def test_portfolio_order_needs_exactly_one_id(specs, ctx, args):
    with pytest.raises(ValidationError, match="exactly one"):
        call(specs["portfolio_order"], ctx, **args)


def test_portfolio_positions_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/positions", "Positions")
    doc = check_golden(call(specs["portfolio_positions"], ctx), "portfolio_positions")
    brk, btc, opt = doc["rows"]
    assert (brk["ticker"], brk["qty"], brk["change_today"], brk["unrealized_plpc"]) == (
        "BRK-B",
        "5",
        0.0084,
        0.2,
    )
    assert (btc["ticker"], btc["asset_class"], btc["exchange"]) == ("BTC/USD", "crypto", None)
    assert (opt["ticker"], opt["occ_symbol"], opt["side"], opt["qty"]) == (
        "AAPL",
        "AAPL261016C00250000",
        "short",
        "-1",
    )
    assert brk["absent"]["occ_symbol"] == "not_applicable"


def test_portfolio_position_golden_by_ticker(specs, ctx, alpaca):
    alpaca.fixture("/v2/positions/BRK.B", "Position__brk")
    check_golden(call(specs["portfolio_position"], ctx, ticker="brk-b"), "portfolio_position")


def test_portfolio_position_by_occ_symbol(specs, ctx, alpaca):
    alpaca.add("/v2/positions/AAPL261016C00250000", load_fixture("Positions")[2])
    out = call(specs["portfolio_position"], ctx, occ_symbol="AAPL261016C00250000")
    assert out.rows[0].occ_symbol == "AAPL261016C00250000"


def test_portfolio_position_needs_exactly_one_instrument(specs, ctx):
    with pytest.raises(ValidationError, match="exactly one"):
        call(specs["portfolio_position"], ctx, ticker="AAPL", occ_symbol="AAPL261016C00250000")


def test_portfolio_broker_watchlists_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/watchlists", "Watchlists")
    doc = check_golden(call(specs["portfolio_broker_watchlists"], ctx), "portfolio_broker_watchlists")
    assert doc["rows"][0]["tickers"] is None and doc["absent"]["tickers"]["code"] == "not_provided_by_source"


def test_portfolio_broker_watchlist_golden(specs, ctx, alpaca):
    alpaca.fixture("/v2/watchlists/3174d6df-7726-44b4-a5bd-7fda5ae6e009", "Watchlist")
    out = call(specs["portfolio_broker_watchlist"], ctx, watchlist_id="3174d6df-7726-44b4-a5bd-7fda5ae6e009")
    doc = check_golden(out, "portfolio_broker_watchlist")
    assert doc["rows"][0]["tickers"] == ["TSLA", "BRK-B", "BTC/USD"]


def test_portfolio_locates_golden_two_pages(specs, ctx, alpaca):
    alpaca.fixture("/v1/locates", "Locates__page2", match={"page_token": LOCATES_CURSOR})
    alpaca.fixture("/v1/locates", "Locates__page1")
    out = call(specs["portfolio_locates"], ctx, start="2026-10-01", end="2026-10-03")
    doc = check_golden(out, "portfolio_locates")
    assert alpaca.params(0) == {"start": "2026-10-01", "end": "2026-10-03", "limit": "10000"}
    assert doc["rows"][1]["status"] == "rejected" and doc["rows"][1]["ticker"] == "BRK-B"
    assert doc["rows"][1]["absent"]["located_qty"] == "not_provided_by_source"


def test_portfolio_locate_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1/locates/550e8400-e29b-41d4-a716-446655440000", "Locate")
    out = call(specs["portfolio_locate"], ctx, locate_id="550e8400-e29b-41d4-a716-446655440000")
    check_golden(out, "portfolio_locate")


def test_portfolio_locate_quotes_golden(specs, ctx, alpaca):
    alpaca.fixture("/v1/locates/quotes", "LocateQuotes")
    out = call(specs["portfolio_locate_quotes"], ctx, tickers=["TSLA", "BRK-B", "META"])
    doc = check_golden(out, "portfolio_locate_quotes")
    assert alpaca.params() == {"symbols": "TSLA,BRK.B,META"}
    assert doc["rows"][1]["price"] is None and doc["rows"][1]["absent"]["price"] == "no_data"
    assert any("META" in n and "symbol not found" in n for n in doc["notes"])


def test_ids_in_paths_are_refused_when_they_could_change_the_route(specs, ctx):
    for tool, arg in (
        ("portfolio_order", "order_id"),
        ("portfolio_broker_watchlist", "watchlist_id"),
        ("portfolio_locate", "locate_id"),
    ):
        with pytest.raises(ValidationError):
            call(specs[tool], ctx, **{arg: "../orders"})
