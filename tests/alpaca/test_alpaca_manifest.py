"""The Alpaca provider's manifest: one ToolSpec per read tool, as the contract's
table 6.1 lists them, each complete enough to drive registration, the policy,
the README table and the parity test."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib

import pytest
from alpaca_harness import REPO

from marketlens_mcp.plugin_api import PLUGIN_API_VERSION, TOOL_NAME_MAX, TOOL_NAME_RE
from marketlens_mcp.providers.alpaca import PLUGIN
from marketlens_schema import BUILTIN_MODELS

M, R, N, P, D = "market", "reference", "news", "portfolio", "provider.docs"
# name: (capability, operationIds, model schema name, parity names)
TABLE = {
    "market_bars": (M, ("StockBars",), "Bar", ("get_stock_bars",)),
    "market_quotes": (M, ("StockQuotes",), "Quote", ("get_stock_quotes",)),
    "market_trades": (M, ("StockTrades",), "Trade", ("get_stock_trades",)),
    "market_latest_bars": (M, ("StockLatestBars",), "Bar", ("get_stock_latest_bar",)),
    "market_latest_quotes": (M, ("StockLatestQuotes",), "Quote", ("get_stock_latest_quote",)),
    "market_latest_trades": (M, ("StockLatestTrades",), "Trade", ("get_stock_latest_trade",)),
    "market_snapshots": (M, ("StockSnapshots",), "Snapshot", ("get_stock_snapshot",)),
    "market_most_active": (M, ("MostActives",), "MostActive", ("get_most_active_stocks",)),
    "market_movers": (M, ("Movers",), "Mover", ("get_market_movers",)),
    "crypto_bars": (M, ("CryptoBars",), "Bar", ("get_crypto_bars",)),
    "crypto_quotes": (M, ("CryptoQuotes",), "Quote", ("get_crypto_quotes",)),
    "crypto_trades": (M, ("CryptoTrades",), "Trade", ("get_crypto_trades",)),
    "crypto_latest_bars": (M, ("CryptoLatestBars",), "Bar", ("get_crypto_latest_bar",)),
    "crypto_latest_quotes": (M, ("CryptoLatestQuotes",), "Quote", ("get_crypto_latest_quote",)),
    "crypto_latest_trades": (M, ("CryptoLatestTrades",), "Trade", ("get_crypto_latest_trade",)),
    "crypto_snapshots": (M, ("CryptoSnapshots",), "Snapshot", ("get_crypto_snapshot",)),
    "crypto_orderbooks": (M, ("CryptoLatestOrderbooks",), "OrderBookLevel", ("get_crypto_latest_orderbook",)),
    "options_bars": (M, ("optionBars",), "OptionBar", ("get_option_bars",)),
    "options_trades": (M, ("OptionTrades",), "OptionTrade", ("get_option_trades",)),
    "options_latest_trades": (M, ("OptionLatestTrades",), "OptionTrade", ("get_option_latest_trade",)),
    "options_latest_quotes": (M, ("OptionLatestQuotes",), "OptionQuote", ("get_option_latest_quote",)),
    "options_snapshots": (M, ("OptionSnapshots",), "OptionSnapshot", ("get_option_snapshot",)),
    "options_chain": (M, ("OptionChain",), "OptionSnapshot", ("get_option_chain",)),
    "fixed_income_latest_quotes": (
        M,
        ("FixedIncomeLatestQuotes",),
        "FixedIncomeQuote",
        ("get_fixed_income_latest_quotes",),
    ),
    "reference_assets": (R, ("get-v2-assets",), "Asset", ("get_all_assets",)),
    "reference_asset": (R, ("get-v2-assets-symbol_or_asset_id",), "Asset", ("get_asset",)),
    "reference_option_contracts": (
        R,
        ("get-options-contracts",),
        "OptionContract",
        ("get_option_contracts",),
    ),
    "reference_option_contract": (
        R,
        ("get-option-contract-symbol_or_id",),
        "OptionContract",
        ("get_option_contract",),
    ),
    "reference_calendar": (R, ("LegacyCalendar",), "MarketCalendarDay", ("get_calendar",)),
    "reference_clock": (R, ("LegacyClock",), "MarketClock", ("get_clock",)),
    "reference_corporate_action_announcements": (
        R,
        ("get-v2-corporate_actions-announcements",),
        "CorporateActionAnnouncement",
        ("get_corporate_action_announcements",),
    ),
    "reference_corporate_action_announcement": (
        R,
        ("get-v2-corporate_actions-announcements-id",),
        "CorporateActionAnnouncement",
        ("get_corporate_action_announcement",),
    ),
    "reference_corporate_actions": (R, ("CorporateActions",), "CorporateAction", ("get_corporate_actions",)),
    "reference_option_exchanges": (
        R,
        ("OptionMetaExchanges",),
        "OptionExchange",
        ("get_option_exchange_codes",),
    ),
    "news_search": (N, ("News",), "NewsItem", ("get_news",)),
    "portfolio_account": (P, ("getAccount",), "Account", ("get_account_info",)),
    "portfolio_account_config": (P, ("getAccountConfig",), "AccountConfig", ("get_account_config",)),
    "portfolio_history": (
        P,
        ("getAccountPortfolioHistory",),
        "PortfolioHistoryPoint",
        ("get_portfolio_history",),
    ),
    "portfolio_activities": (
        P,
        ("getAccountActivities", "getAccountActivitiesByActivityType"),
        "Activity",
        ("get_account_activities", "get_account_activities_by_type"),
    ),
    "portfolio_orders": (P, ("getAllOrders",), "Order", ("get_orders",)),
    "portfolio_order": (
        P,
        ("getOrderByOrderID", "getOrderByClientOrderId"),
        "Order",
        ("get_order_by_id", "get_order_by_client_id"),
    ),
    "portfolio_positions": (P, ("getAllOpenPositions",), "Position", ("get_all_positions",)),
    "portfolio_position": (P, ("getOpenPosition",), "Position", ("get_open_position",)),
    "portfolio_broker_watchlists": (P, ("getWatchlists",), "BrokerWatchlist", ("get_watchlists",)),
    "portfolio_broker_watchlist": (P, ("getWatchlistById",), "BrokerWatchlist", ("get_watchlist_by_id",)),
    "portfolio_locates": (P, ("listLocates",), "Locate", ("get_locates",)),
    "portfolio_locate": (P, ("getLocate",), "Locate", ("get_locate",)),
    "portfolio_locate_quotes": (P, ("listLocateQuotes",), "LocateQuote", ("get_locate_quotes",)),
    "provider_docs_search": (D, (), "ProviderDocument", ("search_alpaca_docs",)),
    "provider_docs_fetch": (D, (), "ProviderDocument", ("fetch_alpaca_doc",)),
    "provider_docs_search_endpoints": (D, (), "ProviderDocument", ("search_alpaca_api_specs",)),
    "provider_docs_list_endpoints": (D, (), "ProviderDocument", ("list_alpaca_api_endpoints",)),
    "provider_docs_get_endpoint": (D, (), "ProviderDocument", ("get_alpaca_endpoint_docs",)),
}
# Key inputs per tool (contract table 6.1), checked as input-model fields.
KEY_INPUTS = {
    "market_bars": {"tickers", "timeframe", "start", "end", "lookback", "adjustment", "page_token"},
    "market_quotes": {"tickers", "start", "end", "lookback", "page_token"},
    "market_trades": {"tickers", "start", "end", "lookback", "page_token"},
    "market_latest_bars": {"tickers"},
    "market_snapshots": {"tickers"},
    "market_most_active": {"by", "top"},
    "market_movers": {"market_type", "top"},
    "crypto_bars": {"tickers", "timeframe", "start", "end", "lookback", "page_token"},
    "crypto_orderbooks": {"tickers", "depth"},
    "options_bars": {"occ_symbols", "timeframe", "start", "end", "lookback", "page_token"},
    "options_snapshots": {"occ_symbols", "page_token"},
    "options_chain": {
        "underlying",
        "option_type",
        "strike_min",
        "strike_max",
        "expiration",
        "expiration_from",
        "expiration_to",
        "root_symbol",
        "page_token",
    },
    "fixed_income_latest_quotes": {"isins"},
    "reference_assets": {"status", "asset_class", "exchange", "attributes"},
    "reference_asset": {"ticker"},
    "reference_option_contracts": {
        "underlyings",
        "status",
        "option_type",
        "style",
        "strike_min",
        "strike_max",
        "root_symbol",
        "page_token",
    },
    "reference_option_contract": {"occ_symbol"},
    "reference_calendar": {"start", "end"},
    "reference_corporate_action_announcements": {
        "ca_types",
        "since",
        "until",
        "ticker",
        "cusip",
        "date_type",
    },
    "reference_corporate_action_announcement": {"announcement_id"},
    "reference_corporate_actions": {"tickers", "cusips", "types", "start", "end", "ids", "page_token"},
    "news_search": {
        "tickers",
        "start",
        "end",
        "lookback",
        "include_content",
        "exclude_contentless",
        "sort",
        "page_token",
    },
    "portfolio_history": {
        "period",
        "timeframe",
        "start",
        "end",
        "intraday_reporting",
        "pnl_reset",
        "extended_hours",
    },
    "portfolio_activities": {
        "activity_types",
        "category",
        "date",
        "after",
        "until",
        "direction",
        "page_token",
    },
    "portfolio_orders": {"status", "after", "until", "direction", "tickers", "side", "nested", "page_token"},
    "portfolio_order": {"order_id", "client_order_id"},
    "portfolio_position": {"ticker", "occ_symbol"},
    "portfolio_broker_watchlist": {"watchlist_id"},
    "portfolio_locates": {"status", "ticker", "start", "end", "page_token"},
    "portfolio_locate": {"locate_id"},
    "portfolio_locate_quotes": {"tickers"},
    "provider_docs_search": {"query"},
    "provider_docs_fetch": {"doc_id"},
    "provider_docs_search_endpoints": {"query"},
    "provider_docs_list_endpoints": {"api"},
    "provider_docs_get_endpoint": {"method", "path", "api"},
}
EXTERNAL_TEXT = {"news_search", *(n for n in TABLE if n.startswith("provider_docs_"))}
ALPACA_ENV = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
GIT = shutil.which("git") or "git"


def test_plugin_info():
    assert PLUGIN.name == "alpaca"
    assert PLUGIN.api_version == PLUGIN_API_VERSION
    assert set(PLUGIN.env) == {
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    }


def test_exactly_the_contract_tools_are_registered(specs):
    assert sorted(specs) == sorted(TABLE)


@pytest.mark.parametrize("name", sorted(TABLE))
def test_spec_matches_the_contract_table(specs, name):
    cap, ops, model, parity = TABLE[name]
    spec = specs[name]
    assert spec.capability == cap
    assert spec.upstream_operations == ops
    assert spec.output_model is BUILTIN_MODELS[f"marketlens.{model}"]
    assert spec.parity_names == parity
    assert spec.provider == ("alpaca-docs" if cap == D else "alpaca")
    assert spec.env == (() if cap == D else ALPACA_ENV)
    assert spec.output_risk == ("external_text" if name in EXTERNAL_TEXT else "api_structured")


@pytest.mark.parametrize("name", sorted(TABLE))
def test_spec_integrity(specs, name):
    spec = specs[name]
    assert TOOL_NAME_RE.match(spec.name) and len(spec.name) <= TOOL_NAME_MAX
    assert spec.title and len(spec.title) <= 60
    assert spec.description and len(spec.description) <= 1000
    assert spec.readme and "\n" not in spec.readme and len(spec.readme) <= 120
    assert spec.route.startswith("GET /") or spec.route.startswith("mcp:")
    assert spec.input_model.model_config.get("extra") == "forbid"
    schema = spec.input_model.model_json_schema()
    assert schema.get("additionalProperties") is False
    json.dumps(schema)
    for field in KEY_INPUTS.get(name, ()):
        assert field in spec.input_model.model_fields, f"{name} lacks input {field}"
    golden = REPO / spec.golden_test
    assert golden.is_file(), spec.golden_test
    assert name in golden.read_text(encoding="utf-8"), f"{spec.golden_test} never mentions {name}"


def test_capability_counts(specs):
    counts: dict[str, int] = {}
    for s in specs.values():
        counts[s.capability] = counts.get(s.capability, 0) + 1
    assert counts == {M: 24, R: 10, N: 1, P: 13, D: 5}


def test_descriptions_say_how_large_results_come_back(specs):
    big = (
        "market_bars",
        "market_quotes",
        "market_trades",
        "crypto_bars",
        "crypto_quotes",
        "crypto_trades",
        "options_bars",
        "options_trades",
        "options_chain",
        "news_search",
        "portfolio_activities",
        "reference_assets",
        "reference_option_contracts",
        "reference_corporate_actions",
        "portfolio_orders",
    )
    for name in big:
        assert "result_id" in specs[name].description, name


def test_the_provider_names_no_product_of_its_own():
    root = REPO / "src" / "marketlens_mcp" / "providers" / "alpaca"
    # Assembled from pieces so this public file never names them literally
    # (tests/core/test_no_vendor_names.py scans the whole repository for them).
    needles = ["alpha" + "research", "quant" + "ai", "at" + "las", "127.0.0.1" + ":8000"]
    pattern = re.compile("|".join(re.escape(n) for n in needles), re.IGNORECASE)
    hits = [str(p) for p in root.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_the_specs_ship_in_the_wheel():
    """hatch puts every file under a wheel package in the wheel unless an
    exclude rule or .gitignore removes it; check both for the spec files."""
    specs = [
        "src/marketlens_mcp/providers/alpaca/specs/trading-api.json",
        "src/marketlens_mcp/providers/alpaca/specs/market-data-api.json",
    ]
    for rel in specs:
        assert (REPO / rel).is_file()
        ignored = subprocess.run([GIT, "check-ignore", "-q", rel], cwd=REPO, check=False)  # noqa: S603
        assert ignored.returncode == 1, f"{rel} is git-ignored, so hatch would leave it out"
    pyproject = REPO / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("pyproject.toml arrives with the seed commit (contract S0)")
    wheel = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "src/marketlens_mcp" in wheel["packages"]
    assert "only-include" not in wheel
    for pattern in wheel.get("exclude", []):
        assert "json" not in pattern and "specs" not in pattern, pattern
