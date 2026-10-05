"""Read-only on the wire. Every registered tool's golden case is replayed here
against the fake Alpaca and every request it sends must be a GET. The five
provider_docs tools read Alpaca's documentation server and must send nothing
to the trading or market data APIs. A tool without a case here fails
test_every_registered_tool_has_a_read_only_case."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from alpaca_harness import FakeContext, call, check_golden, load_fixture
from test_alpaca_provider_docs import ENDPOINTS, FETCH, LIST, SEARCH, FakeDocsClient, text_result
from test_alpaca_provider_docs import GET as GET_ENDPOINT

from marketlens_mcp.providers.alpaca import docs

C1, C2 = "AAPL261016C00250000", "AAPL261016C00260000"
ORDER_ID = "83f37e9f-6b1f-49ed-8fc6-3e6af716323f"
WATCHLIST_ID = "3174d6df-7726-44b4-a5bd-7fda5ae6e009"
LOCATE_ID = "550e8400-e29b-41d4-a716-446655440000"
ANNOUNCEMENT_ID = "be3c368a-4c7c-4384-808e-f02c9f5a8afe"
CA_TYPES = [
    "cash_dividend",
    "forward_split",
    "name_change",
    "stock_and_cash_merger",
    "spin_off",
    "capital_gains_distribution",
    "rights_distribution",
]


@dataclass(frozen=True)
class Case:
    """One golden case: routes are (path, fixture names, query match); several
    fixture names are concatenated into one list body."""

    routes: tuple[tuple[str, tuple[str, ...], dict[str, str] | None], ...]
    args: dict[str, Any]
    golden: str
    settings: dict[str, Any] = field(default_factory=dict)


def r(path: str, *names: str, match: dict[str, str] | None = None):
    return (path, names, match)


CRYPTO = "/v1beta3/crypto/us"
HTTP_CASES: dict[str, Case] = {
    # market: stocks
    "market_bars": Case(
        (
            r("/v2/stocks/bars", "StockBars__page2", match={"page_token": "QUFQTHxEfDIwMjYtMTAtMDE="}),
            r("/v2/stocks/bars", "StockBars__page1"),
        ),
        {"tickers": ["aapl", "brk.b"]},
        "market_bars",
    ),
    "market_quotes": Case(
        (r("/v2/stocks/quotes", "StockQuotes"),),
        {"tickers": ["AAPL"], "start": "2025-10-31", "end": "2026-10-02T20:00:00Z"},
        "market_quotes",
    ),
    "market_trades": Case((r("/v2/stocks/trades", "StockTrades"),), {"tickers": ["BRK-B"]}, "market_trades"),
    "market_latest_bars": Case(
        (r("/v2/stocks/bars/latest", "StockLatestBars"),),
        {"tickers": ["AAPL", "BRK-B"]},
        "market_latest_bars",
    ),
    "market_latest_quotes": Case(
        (r("/v2/stocks/quotes/latest", "StockLatestQuotes"),),
        {"tickers": ["AAPL", "BRK-B"]},
        "market_latest_quotes",
    ),
    "market_latest_trades": Case(
        (r("/v2/stocks/trades/latest", "StockLatestTrades"),), {"tickers": ["AAPL"]}, "market_latest_trades"
    ),
    "market_snapshots": Case(
        (r("/v2/stocks/snapshots", "StockSnapshots"),),
        {"tickers": ["AAPL", "BRK-B", "ZZZZ"]},
        "market_snapshots",
    ),
    "market_most_active": Case(
        (r("/v1beta1/screener/stocks/most-actives", "MostActives"),),
        {"by": "trades", "top": 2},
        "market_most_active",
    ),
    "market_movers": Case(
        (r("/v1beta1/screener/stocks/movers", "Movers__stocks"),), {"top": 1}, "market_movers"
    ),
    # market: crypto
    "crypto_bars": Case((r(f"{CRYPTO}/bars", "CryptoBars"),), {"tickers": ["btc/usd"]}, "crypto_bars"),
    "crypto_quotes": Case(
        (r(f"{CRYPTO}/quotes", "CryptoQuotes"),), {"tickers": ["BTC/USD"]}, "crypto_quotes"
    ),
    "crypto_trades": Case(
        (r(f"{CRYPTO}/trades", "CryptoTrades"),),
        {"tickers": ["ETH/USD"], "lookback": "PT1H"},
        "crypto_trades",
    ),
    "crypto_latest_bars": Case(
        (r(f"{CRYPTO}/latest/bars", "CryptoLatestBars"),), {"tickers": ["BTC/USD"]}, "crypto_latest_bars"
    ),
    "crypto_latest_quotes": Case(
        (r(f"{CRYPTO}/latest/quotes", "CryptoLatestQuotes"),),
        {"tickers": ["BTC/USD"]},
        "crypto_latest_quotes",
    ),
    "crypto_latest_trades": Case(
        (r(f"{CRYPTO}/latest/trades", "CryptoLatestTrades"),),
        {"tickers": ["BTC/USD", "ETH/BTC"]},
        "crypto_latest_trades",
    ),
    "crypto_snapshots": Case(
        (r(f"{CRYPTO}/snapshots", "CryptoSnapshots"),),
        {"tickers": ["BTC/USD", "ETH/USD"]},
        "crypto_snapshots",
    ),
    "crypto_orderbooks": Case(
        (r(f"{CRYPTO}/latest/orderbooks", "CryptoLatestOrderbooks"),),
        {"tickers": ["BTC/USD"], "depth": 2},
        "crypto_orderbooks",
    ),
    # market: options
    "options_bars": Case(
        (r("/v1beta1/options/bars", "OptionBars"),), {"occ_symbols": [C1.lower()]}, "options_bars"
    ),
    "options_trades": Case(
        (r("/v1beta1/options/trades", "OptionTrades"),), {"occ_symbols": [C1]}, "options_trades"
    ),
    "options_latest_trades": Case(
        (r("/v1beta1/options/trades/latest", "OptionLatestTrades"),),
        {"occ_symbols": [C1]},
        "options_latest_trades",
    ),
    "options_latest_quotes": Case(
        (r("/v1beta1/options/quotes/latest", "OptionLatestQuotes"),),
        {"occ_symbols": [C1, C2]},
        "options_latest_quotes",
        {"options_feed": "opra"},
    ),
    "options_snapshots": Case(
        (r("/v1beta1/options/snapshots", "OptionSnapshots"),), {"occ_symbols": [C1, C2]}, "options_snapshots"
    ),
    "options_chain": Case(
        (
            r("/v1beta1/options/snapshots/AAPL", "OptionChain__page2", match={"page_token": "Q0hBSU58Mg=="}),
            r("/v1beta1/options/snapshots/AAPL", "OptionChain__page1"),
        ),
        {
            "underlying": "aapl",
            "expiration_from": "2026-10-01",
            "expiration_to": "2026-10-31",
            "strike_min": 200,
            "strike_max": 300,
        },
        "options_chain",
    ),
    # market: fixed income
    "fixed_income_latest_quotes": Case(
        (r("/v1beta1/fixed_income/latest/quotes", "FixedIncomeLatestQuotes"),),
        {"isins": ["us912797sx61", "US91282CJL54"]},
        "fixed_income_latest_quotes",
    ),
    # news
    "news_search": Case(
        (
            r("/v1beta1/news", "News__page2", match={"page_token": "TkVXU3wy"}),
            r("/v1beta1/news", "News__page1"),
        ),
        {"tickers": ["aapl", "brk.b"]},
        "news_search",
    ),
    # reference
    "reference_assets": Case(
        (r("/v2/assets", "Assets"),), {"status": "active", "attributes": ["has_options"]}, "reference_assets"
    ),
    "reference_asset": Case((r("/v2/assets/BRK.B", "Asset__brk"),), {"ticker": "brk.b"}, "reference_asset"),
    "reference_option_contracts": Case(
        (
            r("/v2/options/contracts", "OptionContracts__page2", match={"page_token": "T1BUQ3wy"}),
            r("/v2/options/contracts", "OptionContracts__page1"),
        ),
        {"underlyings": ["AAPL", "BRK-B"], "expiration_to": "2026-10-31", "strike_min": 100},
        "reference_option_contracts",
    ),
    "reference_option_contract": Case(
        (r(f"/v2/options/contracts/{C1}", "OptionContract"),), {"occ_symbol": C1}, "reference_option_contract"
    ),
    "reference_calendar": Case(
        (r("/v2/calendar", "Calendar"),), {"start": "2025-12-23", "end": "2026-06-24"}, "reference_calendar"
    ),
    "reference_clock": Case((r("/v2/clock", "Clock"),), {}, "reference_clock"),
    "reference_corporate_action_announcements": Case(
        (r("/v2/corporate_actions/announcements", "Announcements"),),
        {"ca_types": ["dividend", "merger"], "since": "2026-09-01", "until": "2026-09-30", "ticker": "brk.b"},
        "reference_corporate_action_announcements",
    ),
    "reference_corporate_action_announcement": Case(
        (r(f"/v2/corporate_actions/announcements/{ANNOUNCEMENT_ID}", "Announcement"),),
        {"announcement_id": ANNOUNCEMENT_ID},
        "reference_corporate_action_announcement",
    ),
    "reference_corporate_actions": Case(
        (
            r("/v1/corporate-actions", "CorporateActions__page2", match={"page_token": "Q0F8Mg=="}),
            r("/v1/corporate-actions", "CorporateActions__page1"),
        ),
        {"start": "2025-12-01", "end": "2026-09-30", "types": CA_TYPES},
        "reference_corporate_actions",
    ),
    "reference_option_exchanges": Case(
        (r("/v1beta1/options/meta/exchanges", "OptionMetaExchanges"),), {}, "reference_option_exchanges"
    ),
    # portfolio (brokerage-account reads)
    "portfolio_account": Case((r("/v2/account", "Account"),), {}, "portfolio_account"),
    "portfolio_account_config": Case(
        (r("/v2/account/configurations", "AccountConfig"),), {}, "portfolio_account_config"
    ),
    "portfolio_history": Case(
        (r("/v2/account/portfolio/history", "PortfolioHistory"),),
        {"period": "1W", "timeframe": "1d", "pnl_reset": "no_reset"},
        "portfolio_history",
    ),
    "portfolio_activities": Case(
        (r("/v2/account/activities", "Activities__page1", "Activities__page2"),), {}, "portfolio_activities"
    ),
    "portfolio_orders": Case((r("/v2/orders", "Orders__page1"),), {"status": "all"}, "portfolio_orders"),
    "portfolio_order": Case(
        (r(f"/v2/orders/{ORDER_ID}", "Order__mleg"),), {"order_id": ORDER_ID}, "portfolio_order"
    ),
    "portfolio_positions": Case((r("/v2/positions", "Positions"),), {}, "portfolio_positions"),
    "portfolio_position": Case(
        (r("/v2/positions/BRK.B", "Position__brk"),), {"ticker": "brk-b"}, "portfolio_position"
    ),
    "portfolio_broker_watchlists": Case(
        (r("/v2/watchlists", "Watchlists"),), {}, "portfolio_broker_watchlists"
    ),
    "portfolio_broker_watchlist": Case(
        (r(f"/v2/watchlists/{WATCHLIST_ID}", "Watchlist"),),
        {"watchlist_id": WATCHLIST_ID},
        "portfolio_broker_watchlist",
    ),
    "portfolio_locates": Case(
        (
            r("/v1/locates", "Locates__page2", match={"page_token": "TE9DfDI="}),
            r("/v1/locates", "Locates__page1"),
        ),
        {"start": "2026-10-01", "end": "2026-10-03"},
        "portfolio_locates",
    ),
    "portfolio_locate": Case(
        (r(f"/v1/locates/{LOCATE_ID}", "Locate"),), {"locate_id": LOCATE_ID}, "portfolio_locate"
    ),
    "portfolio_locate_quotes": Case(
        (r("/v1/locates/quotes", "LocateQuotes"),),
        {"tickers": ["TSLA", "BRK-B", "META"]},
        "portfolio_locate_quotes",
    ),
}

DOCS_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "provider_docs_search": ({"query": "paper trading"}, "provider_docs_search"),
    "provider_docs_fetch": ({"doc_id": "market-data-faq"}, "provider_docs_fetch"),
    "provider_docs_search_endpoints": ({"query": "bars"}, "provider_docs_search_endpoints"),
    "provider_docs_list_endpoints": ({"api": "market_data"}, "provider_docs_list_endpoints"),
    "provider_docs_get_endpoint": (
        {"method": "get", "path": "/v2/clock", "api": "trading"},
        "provider_docs_get_endpoint",
    ),
}
#: The documentation server's own read tools, the only ones the provider calls there.
DOCS_SERVER_READS = {"search", "fetch", "search-endpoints", "list-endpoints", "get-endpoint"}


def _body(names: tuple[str, ...]) -> Any:
    if len(names) == 1:
        return load_fixture(names[0])
    return [item for name in names for item in load_fixture(name)]


def test_every_registered_tool_has_a_read_only_case(specs):
    assert not set(HTTP_CASES) & set(DOCS_CASES)
    assert set(HTTP_CASES) | set(DOCS_CASES) == set(specs)


@pytest.mark.parametrize("tool", sorted(HTTP_CASES))
def test_every_request_of_a_golden_case_is_a_get(specs, alpaca, tool):
    case = HTTP_CASES[tool]
    for path, names, match in case.routes:
        alpaca.add(path, _body(names), match=match)
    check_golden(call(specs[tool], FakeContext(settings=case.settings), **case.args), case.golden)
    assert alpaca.requests, f"{tool} sent no request"
    assert [(q.method, q.url.path) for q in alpaca.requests] == [("GET", q.url.path) for q in alpaca.requests]


@pytest.mark.parametrize("tool", sorted(DOCS_CASES))
def test_documentation_tools_send_nothing_to_the_trading_or_data_apis(specs, alpaca, tool):
    fake = FakeDocsClient(
        {
            "search": SEARCH,
            "fetch": text_result(FETCH),
            "search-endpoints": ENDPOINTS,
            "list-endpoints": LIST,
            "get-endpoint": GET_ENDPOINT,
        }
    )
    args, golden = DOCS_CASES[tool]
    with docs.use_client_factory(fake):
        check_golden(call(specs[tool], FakeContext(environ={}), **args), golden)
    assert alpaca.requests == []
    assert [name for name, _ in fake.calls] and {name for name, _ in fake.calls} <= DOCS_SERVER_READS
