"""Canonical market and reference models (schema 1.x).

Owner: ml-core (seeded verbatim from the contract). Built by the Alpaca
adapter (ml-alpaca) and any future provider. Field units are in ``x-unit``.
One row = one observation; nested lists appear only where a row genuinely owns
a small list (an option contract's deliverables, a news item's tickers).
"""

from __future__ import annotations

from datetime import date as Date
from typing import ClassVar, Literal

from pydantic import Field

from .base import (
    AssetClass,
    CanonicalModel,
    DecimalStr,
    Isin,
    OccSymbol,
    OptionStyle,
    OptionType,
    Side,
    Ticker,
    Timeframe,
    UtcDatetime,
    unit,
)

# --- reference ------------------------------------------------------------------------------


class Asset(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Asset"
    group_column: ClassVar[str | None] = "ticker"

    ticker: Ticker
    asset_class: AssetClass
    name: str | None = None
    exchange: str | None = Field(
        default=None, description="Listing venue as the provider names it (NASDAQ, NYSE, ARCA, CRYPTO, ...)"
    )
    status: Literal["active", "inactive"]
    tradable: bool
    marginable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    fractionable: bool | None = None
    min_order_size: DecimalStr | None = unit("by_asset_class", default=None)
    min_trade_increment: DecimalStr | None = unit("by_asset_class", default=None)
    price_increment: DecimalStr | None = unit("price", default=None)
    margin_requirement_long: float | None = unit("fraction", default=None)
    margin_requirement_short: float | None = unit("fraction", default=None)
    cusip: str | None = None
    attributes: list[str] = Field(
        default_factory=list, description="Provider attribute flags, e.g. ptp_no_exception, has_options"
    )
    provider_asset_id: str | None = Field(default=None, description="The provider's opaque id for this asset")


class MarketClock(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.MarketClock"
    time_column: ClassVar[str | None] = "t"

    market: str = Field(description='The market this clock describes, e.g. "US equities"')
    t: UtcDatetime = unit("UTC", description="The provider's current time")
    is_open: bool
    next_open: UtcDatetime = unit("UTC")
    next_close: UtcDatetime = unit("UTC")


class MarketCalendarDay(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.MarketCalendarDay"

    market: str = Field(description='e.g. "US equities"')
    date: Date
    open: UtcDatetime = unit("UTC", description="Core session open")
    close: UtcDatetime = unit("UTC", description="Core session close (early closes included)")
    session_open: UtcDatetime | None = unit("UTC", default=None, description="Extended session open")
    session_close: UtcDatetime | None = unit("UTC", default=None, description="Extended session close")
    settlement_date: Date | None = None


class OptionDeliverable(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.OptionDeliverable"

    type: Literal["cash", "equity"]
    ticker: Ticker | None = None
    amount: DecimalStr = unit(
        "by_asset_class", description="Shares (equity) or USD (cash) delivered per contract"
    )
    allocation_pct: float | None = unit("fraction", default=None)
    settlement_type: str | None = None
    settlement_method: str | None = None
    delayed_settlement: bool | None = None


class OptionContract(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.OptionContract"
    group_column: ClassVar[str | None] = "occ_symbol"

    occ_symbol: OccSymbol
    underlying: Ticker
    root_symbol: str
    name: str | None = None
    status: Literal["active", "inactive"]
    tradable: bool
    expiration_date: Date
    strike: float = unit("price")
    option_type: OptionType
    style: OptionStyle
    multiplier: float = unit("multiplier")
    size: float | None = unit("multiplier", default=None, description="Contract size in shares")
    open_interest: int | None = unit("contracts", default=None)
    open_interest_date: Date | None = Field(
        default=None,
        description="The date of the open interest count. Open interest is published the next morning, so this "
        "is usually one trading day before close_price_date",
    )
    close_price: float | None = unit("price", default=None)
    close_price_date: Date | None = Field(default=None, description="The trading day of close_price")
    deliverables: list[OptionDeliverable] | None = None
    provider_contract_id: str | None = None


class OptionExchange(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.OptionExchange"

    code: str
    name: str


class CorporateAction(CanonicalModel):
    """One processed corporate action. Columns that do not apply to the
    action's type are None with code not_applicable (response-level)."""

    schema_name: ClassVar[str] = "marketlens.CorporateAction"
    time_column: ClassVar[str | None] = None
    group_column: ClassVar[str | None] = "ticker"

    action_id: str
    action_type: Literal[
        "cash_dividend",
        "stock_dividend",
        "forward_split",
        "reverse_split",
        "unit_split",
        "spin_off",
        "cash_merger",
        "stock_merger",
        "stock_and_cash_merger",
        "redemption",
        "name_change",
        "worthless_removal",
        "rights_distribution",
        "partial_call",
        "reorganization",
        "capital_gains_distribution",
    ]
    ticker: Ticker | None = Field(
        default=None, description="The instrument the action applies to (for a merger, the acquiree)"
    )
    cusip: str | None = None
    isin: Isin | None = None
    currency: str | None = None
    process_date: Date | None = None
    ex_date: Date | None = None
    record_date: Date | None = None
    payable_date: Date | None = None
    effective_date: Date | None = None
    rate: float | None = unit(
        "price", default=None, description="Cash per share (dividends, redemptions, cash mergers)"
    )
    cash_rate: float | None = unit("price", default=None)
    old_rate: float | None = unit("fraction", default=None, description="Split/merger ratio leg (old)")
    new_rate: float | None = unit("fraction", default=None, description="Split/merger ratio leg (new)")
    new_ticker: Ticker | None = None
    old_ticker: Ticker | None = None
    acquirer_ticker: Ticker | None = None
    acquiree_ticker: Ticker | None = None
    source_ticker: Ticker | None = None
    special: bool | None = None
    foreign: bool | None = None


class CorporateActionAnnouncement(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.CorporateActionAnnouncement"
    group_column: ClassVar[str | None] = "initiating_ticker"

    announcement_id: str
    corporate_action_id: str | None = None
    ca_type: Literal["dividend", "merger", "spinoff", "split", "name_change", "other"]
    ca_sub_type: str | None = None
    initiating_ticker: Ticker | None = None
    initiating_cusip: str | None = None
    target_ticker: Ticker | None = None
    target_cusip: str | None = None
    declaration_date: Date | None = None
    ex_date: Date | None = None
    record_date: Date | None = None
    payable_date: Date | None = None
    effective_date: Date | None = None
    cash: DecimalStr | None = unit("USD", default=None, description="Cash per share")
    old_rate: DecimalStr | None = unit("fraction", default=None)
    new_rate: DecimalStr | None = unit("fraction", default=None)


# --- stocks and crypto ------------------------------------------------------------------------


class Bar(CanonicalModel):
    """One OHLCV bar of a stock or a crypto pair."""

    schema_name: ClassVar[str] = "marketlens.Bar"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("close", "open", "high", "low", "vwap", "volume")

    ticker: Ticker
    asset_class: AssetClass
    timeframe: Timeframe
    t: UtcDatetime = unit("UTC", description="Bar start")
    open: float = unit("price")
    high: float = unit("price")
    low: float = unit("price")
    close: float = unit("price")
    volume: float = unit("by_asset_class")
    trade_count: int | None = unit("count", default=None)
    vwap: float | None = unit("price", default=None)
    currency: str = "USD"


class Quote(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Quote"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("bid_price", "ask_price", "bid_size", "ask_size")

    ticker: Ticker
    asset_class: AssetClass
    t: UtcDatetime = unit("UTC")
    bid_price: float | None = unit("price", default=None)
    bid_size: float | None = unit(
        "by_asset_class",
        default=None,
        description="Shares (stocks) or base units (crypto). If a feed reports round lots, the adapter converts to shares and says so in the tool description",
    )
    bid_exchange: str | None = None
    ask_price: float | None = unit("price", default=None)
    ask_size: float | None = unit("by_asset_class", default=None)
    ask_exchange: str | None = None
    conditions: list[str] | None = None
    tape: str | None = None
    currency: str = "USD"


class Trade(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Trade"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("price", "size")

    ticker: Ticker
    asset_class: AssetClass
    t: UtcDatetime = unit("UTC")
    price: float = unit("price")
    size: float = unit("by_asset_class")
    exchange: str | None = None
    trade_id: str | None = None
    conditions: list[str] | None = None
    tape: str | None = None
    taker_side: Side | None = Field(default=None, description="Crypto only: the aggressor side")
    currency: str = "USD"


class Snapshot(CanonicalModel):
    """The latest state of one stock or crypto pair. ``change`` and
    ``change_pct`` are derived by the adapter from last_price and prev_close."""

    schema_name: ClassVar[str] = "marketlens.Snapshot"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("last_price", "change_pct")

    ticker: Ticker
    asset_class: AssetClass
    t: UtcDatetime = unit("UTC", description="The newest timestamp among the snapshot's parts")
    last_price: float | None = unit("price", default=None)
    last_size: float | None = unit("by_asset_class", default=None)
    last_t: UtcDatetime | None = unit("UTC", default=None)
    bid_price: float | None = unit("price", default=None)
    bid_size: float | None = unit("by_asset_class", default=None)
    ask_price: float | None = unit("price", default=None)
    ask_size: float | None = unit("by_asset_class", default=None)
    quote_t: UtcDatetime | None = unit("UTC", default=None)
    minute_close: float | None = unit("price", default=None)
    minute_t: UtcDatetime | None = unit("UTC", default=None)
    day_open: float | None = unit("price", default=None)
    day_high: float | None = unit("price", default=None)
    day_low: float | None = unit("price", default=None)
    day_close: float | None = unit("price", default=None)
    day_volume: float | None = unit("by_asset_class", default=None)
    day_vwap: float | None = unit("price", default=None)
    prev_close: float | None = unit("price", default=None)
    prev_volume: float | None = unit("by_asset_class", default=None)
    change: float | None = unit("price", default=None)
    change_pct: float | None = unit("fraction", default=None)
    currency: str = "USD"


class OrderBookLevel(CanonicalModel):
    """One price level of a crypto order book; level 0 is the best price."""

    schema_name: ClassVar[str] = "marketlens.OrderBookLevel"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("price", "size")

    ticker: Ticker
    asset_class: AssetClass
    t: UtcDatetime = unit("UTC")
    side: Literal["bid", "ask"]
    level: int = Field(ge=0)
    price: float = unit("price")
    size: float = unit("base_units")
    currency: str = "USD"


class MostActive(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.MostActive"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("volume", "trade_count")

    ticker: Ticker
    rank: int = Field(ge=1)
    ranked_by: Literal["volume", "trades"]
    volume: float = unit("shares")
    trade_count: int = unit("count")
    as_of: UtcDatetime = unit("UTC", description="The screener's last update")


class Mover(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Mover"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("percent_change", "change", "price")

    ticker: Ticker
    market_type: Literal["stocks", "crypto"]
    direction: Literal["gainer", "loser"]
    rank: int = Field(ge=1)
    price: float = unit("price")
    change: float = unit("price")
    percent_change: float = unit("fraction", description="Alpaca reports percent; the adapter divides by 100")
    as_of: UtcDatetime = unit("UTC")


# --- options --------------------------------------------------------------------------------


class OptionBar(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.OptionBar"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "occ_symbol"
    value_columns: ClassVar[tuple[str, ...]] = ("close", "open", "high", "low", "vwap", "volume")

    occ_symbol: OccSymbol
    underlying: Ticker
    timeframe: Timeframe
    t: UtcDatetime = unit("UTC", description="Bar start")
    open: float = unit("price", description="Premium per share")
    high: float = unit("price")
    low: float = unit("price")
    close: float = unit("price")
    volume: float = unit("contracts")
    trade_count: int | None = unit("count", default=None)
    vwap: float | None = unit("price", default=None)
    currency: str = "USD"


class OptionQuote(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.OptionQuote"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "occ_symbol"
    value_columns: ClassVar[tuple[str, ...]] = ("bid_price", "ask_price")

    occ_symbol: OccSymbol
    underlying: Ticker
    t: UtcDatetime = unit("UTC")
    bid_price: float | None = unit("price", default=None)
    bid_size: float | None = unit("contracts", default=None)
    bid_exchange: str | None = None
    ask_price: float | None = unit("price", default=None)
    ask_size: float | None = unit("contracts", default=None)
    ask_exchange: str | None = None
    condition: str | None = None
    currency: str = "USD"


class OptionTrade(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.OptionTrade"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "occ_symbol"
    value_columns: ClassVar[tuple[str, ...]] = ("price", "size")

    occ_symbol: OccSymbol
    underlying: Ticker
    t: UtcDatetime = unit("UTC")
    price: float = unit("price")
    size: float = unit("contracts")
    exchange: str | None = None
    condition: str | None = None
    currency: str = "USD"


class OptionSnapshot(CanonicalModel):
    """Latest state of one contract with greeks and implied volatility (the
    rows of options_snapshots and options_chain). Expiration, strike and type
    are parsed from the OCC symbol. Greeks are the provider's, per share:
    delta per 1.00 move of the underlying, gamma per 1.00 move squared, theta
    in premium per calendar day, vega in premium per 1 point (0.01) of IV, rho
    in premium per 1 point (0.01) of rate. ml-alpaca verifies these conventions
    against Alpaca's documentation and corrects the descriptions if they differ."""

    schema_name: ClassVar[str] = "marketlens.OptionSnapshot"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "occ_symbol"
    value_columns: ClassVar[tuple[str, ...]] = (
        "implied_volatility",
        "delta",
        "last_price",
        "bid_price",
        "ask_price",
    )

    occ_symbol: OccSymbol
    underlying: Ticker
    expiration_date: Date
    strike: float = unit("price")
    option_type: OptionType
    t: UtcDatetime = unit("UTC", description="The newest timestamp among the snapshot's parts")
    last_price: float | None = unit("price", default=None)
    last_size: float | None = unit("contracts", default=None)
    last_t: UtcDatetime | None = unit("UTC", default=None)
    bid_price: float | None = unit("price", default=None)
    bid_size: float | None = unit("contracts", default=None)
    ask_price: float | None = unit("price", default=None)
    ask_size: float | None = unit("contracts", default=None)
    quote_t: UtcDatetime | None = unit("UTC", default=None)
    day_open: float | None = unit("price", default=None)
    day_high: float | None = unit("price", default=None)
    day_low: float | None = unit("price", default=None)
    day_close: float | None = unit("price", default=None)
    day_volume: float | None = unit("contracts", default=None)
    prev_close: float | None = unit("price", default=None)
    implied_volatility: float | None = unit("fraction_per_year", default=None)
    delta: float | None = unit("per_share_greek", default=None)
    gamma: float | None = unit("per_share_greek", default=None)
    theta: float | None = unit("per_share_greek", default=None)
    vega: float | None = unit("per_share_greek", default=None)
    rho: float | None = unit("per_share_greek", default=None)
    currency: str = "USD"


# --- fixed income, news, provider docs ----------------------------------------------------------


class FixedIncomeQuote(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.FixedIncomeQuote"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "isin"
    value_columns: ClassVar[tuple[str, ...]] = ("bid_price", "ask_price", "bid_ytm", "ask_ytm")

    isin: Isin
    t: UtcDatetime = unit("UTC")
    bid_price: float | None = unit("percent_of_par", default=None)
    ask_price: float | None = unit("percent_of_par", default=None)
    bid_size: float | None = unit("USD", default=None, description="Face value")
    ask_size: float | None = unit("USD", default=None, description="Face value")
    bid_ytm: float | None = unit("fraction", default=None)
    ask_ytm: float | None = unit("fraction", default=None)
    bid_ytw: float | None = unit("fraction", default=None)
    ask_ytw: float | None = unit("fraction", default=None)


class NewsItem(CanonicalModel):
    """A news article. headline, summary and content are UNTRUSTED third-party
    text (the trust envelope marks every response carrying this model)."""

    schema_name: ClassVar[str] = "marketlens.NewsItem"
    time_column: ClassVar[str | None] = "created_at"

    news_id: str
    headline: str
    summary: str | None = None
    content: str | None = Field(
        default=None, description="Full body, only when include_content was requested"
    )
    author: str | None = None
    publisher: str | None = Field(
        default=None, description="The outlet that published it (Alpaca's `source`)"
    )
    url: str | None = None
    tickers: list[Ticker] = Field(default_factory=list)
    created_at: UtcDatetime = unit("UTC")
    updated_at: UtcDatetime | None = unit("UTC", default=None)


class ProviderDocument(CanonicalModel):
    """A result from a provider's documentation service (provider.docs).
    UNTRUSTED third-party text."""

    schema_name: ClassVar[str] = "marketlens.ProviderDocument"

    provider: str
    kind: Literal["search_hit", "document", "endpoint", "endpoint_list"]
    doc_id: str | None = None
    title: str | None = None
    url: str | None = None
    method: str | None = None
    path: str | None = None
    text: str | None = None


MARKET_MODELS: tuple[type[CanonicalModel], ...] = (
    Asset,
    MarketClock,
    MarketCalendarDay,
    OptionDeliverable,
    OptionContract,
    OptionExchange,
    CorporateAction,
    CorporateActionAnnouncement,
    Bar,
    Quote,
    Trade,
    Snapshot,
    OrderBookLevel,
    MostActive,
    Mover,
    OptionBar,
    OptionQuote,
    OptionTrade,
    OptionSnapshot,
    FixedIncomeQuote,
    NewsItem,
    ProviderDocument,
)
