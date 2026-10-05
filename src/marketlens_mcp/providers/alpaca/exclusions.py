"""Every Alpaca operation this provider does NOT map, and why.

The parity test (tests/alpaca/test_parity.py) requires every operationId in the
two pinned OpenAPI specs to be either mapped by exactly one registered tool
(``ToolSpec.upstream_operations``) or listed here, never both. It also requires
every alpaca-mcp-server tool to be covered by a tool's ``parity_names`` or to be
one of the 17 writes in ``WRITE_TOOLS``.

v1 is read-only: the writes are not implemented, not registered and cannot be
switched on by any configuration. Nothing in this module is callable.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

ExclusionKind = Literal["write", "streaming", "out_of_scope", "candidate", "covered", "not_data"]

READ_ONLY_V1 = (
    "v1 is read-only (owner decision 2026-10-04): not registered, and no configuration can enable it."
)


class Exclusion(NamedTuple):
    kind: ExclusionKind
    reason: str
    alpaca_tools: tuple[str, ...] = ()


#: alpaca-mcp-server's 17 write tools -> the operationId each would call.
WRITE_TOOLS: dict[str, str] = {
    "update_account_config": "patchAccountConfig",
    "place_stock_order": "postOrder",
    "place_crypto_order": "postOrder",
    "place_option_order": "postOrder",
    "replace_order_by_id": "patchOrderByOrderId",
    "cancel_order_by_id": "deleteOrderByOrderID",
    "cancel_all_orders": "deleteAllOrders",
    "close_position": "deleteOpenPosition",
    "close_all_positions": "deleteAllOpenPositions",
    "exercise_options_position": "optionExercise",
    "do_not_exercise_options_position": "optionDoNotExercise",
    "create_watchlist": "postWatchlist",
    "update_watchlist_by_id": "updateWatchlistById",
    "delete_watchlist_by_id": "deleteWatchlistById",
    "add_asset_to_watchlist_by_id": "addAssetToWatchlist",
    "remove_asset_from_watchlist_by_id": "removeAssetFromWatchlist",
    "create_locate": "createLocates",
}


def _writes() -> dict[str, Exclusion]:
    by_op: dict[str, list[str]] = {}
    for tool, op in WRITE_TOOLS.items():
        by_op.setdefault(op, []).append(tool)
    return {op: Exclusion("write", READ_ONLY_V1, tuple(tools)) for op, tools in by_op.items()}


_MONEY = (
    "Writes that move money or crypto, or change broker-side state; alpaca-mcp-server never exposed them. "
    + READ_ONLY_V1
)
_BY_NAME = "Broker watchlist write addressed by name; alpaca-mcp-server never exposed it. " + READ_ONLY_V1
_STREAMING = "Server-sent event stream; v1 has no streaming tools (every tool is a bounded request)."
_FUNDING = (
    "Account funding, wallet or tokenization read: not market, reference or research data, and it would "
    "put transfer details in front of the model. Out of scope for v1."
)
_CANDIDATE = "A read worth adding in a 1.x release (alpaca-mcp-server does not expose it)."
_COVERED = "Single-symbol variant of an endpoint that is mapped with a list of symbols ({tool})."

EXCLUDED_OPERATIONS: dict[str, Exclusion] = {
    **_writes(),
    # writes alpaca-mcp-server never exposed
    "postTokenizationMint": Exclusion("write", _MONEY),
    "createCryptoTransferForAccount": Exclusion("write", _MONEY),
    "createWhitelistedAddress": Exclusion("write", _MONEY),
    "deleteWhitelistedAddress": Exclusion("write", _MONEY),
    "updateWhitelistedAddressTravelRuleInfo": Exclusion("write", _MONEY),
    "deleteWatchlistByName": Exclusion("write", _BY_NAME),
    "addAssetToWatchlistByName": Exclusion("write", _BY_NAME),
    "updateWatchlistByName": Exclusion("write", _BY_NAME),
    # streaming
    "subscribeToActivitiesSSE": Exclusion("streaming", _STREAMING),
    "SubscribeToCorporateActionsEventsSSE": Exclusion("streaming", _STREAMING),
    # funding and tokenization reads
    "getTokenizationRequests": Exclusion("out_of_scope", _FUNDING),
    "getTokenizationRequest": Exclusion("out_of_scope", _FUNDING),
    "getTokenizationRequestByClientRequestID": Exclusion("out_of_scope", _FUNDING),
    "listCryptoFundingWallets": Exclusion("out_of_scope", _FUNDING),
    "getCryptoTransferEstimate": Exclusion("out_of_scope", _FUNDING),
    "listCryptoFundingTransfers": Exclusion("out_of_scope", _FUNDING),
    "getCryptoFundingTransfer": Exclusion("out_of_scope", _FUNDING),
    "searchVASPs": Exclusion("out_of_scope", _FUNDING),
    "listWhitelistedAddress": Exclusion("out_of_scope", _FUNDING),
    # candidates for 1.x
    "Calendar": Exclusion(
        "candidate", _CANDIDATE + " Multi-market v3 calendar; reference_calendar maps the v2 one."
    ),
    "Clock": Exclusion("candidate", _CANDIDATE + " Multi-market v3 clock; reference_clock maps the v2 one."),
    "FixedIncomeLatestPrices": Exclusion("candidate", _CANDIDATE + " Latest fixed-income prices."),
    "LatestRates": Exclusion("candidate", _CANDIDATE + " Latest forex rates."),
    "Rates": Exclusion("candidate", _CANDIDATE + " Historical forex rates."),
    "OptionMetaConditions": Exclusion("candidate", _CANDIDATE + " Option trade and quote condition codes."),
    "StockMetaConditions": Exclusion("candidate", _CANDIDATE + " Stock trade and quote condition codes."),
    "StockMetaExchanges": Exclusion("candidate", _CANDIDATE + " Stock exchange codes."),
    "StockAuctions": Exclusion("candidate", _CANDIDATE + " Opening and closing auction prices."),
    "StockAuctionSingle": Exclusion(
        "candidate",
        _CANDIDATE + " Single-symbol auctions; the list endpoint (StockAuctions) is a candidate too.",
    ),
    "getWatchlistByName": Exclusion(
        "candidate", _CANDIDATE + " Broker watchlist by name; portfolio_broker_watchlist reads one by id."
    ),
    # single-symbol variants of mapped list endpoints
    "StockBarSingle": Exclusion("covered", _COVERED.format(tool="market_bars")),
    "StockLatestBarSingle": Exclusion("covered", _COVERED.format(tool="market_latest_bars")),
    "StockQuoteSingle": Exclusion("covered", _COVERED.format(tool="market_quotes")),
    "StockLatestQuoteSingle": Exclusion("covered", _COVERED.format(tool="market_latest_quotes")),
    "StockSnapshotSingle": Exclusion("covered", _COVERED.format(tool="market_snapshots")),
    "StockTradeSingle": Exclusion("covered", _COVERED.format(tool="market_trades")),
    "StockLatestTradeSingle": Exclusion("covered", _COVERED.format(tool="market_latest_trades")),
    # not data
    "Logos": Exclusion("not_data", "Returns a binary logo image, not data a model can analyse."),
}
