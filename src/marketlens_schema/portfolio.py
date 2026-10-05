"""Canonical brokerage-account models (schema 1.x), served under the
``portfolio`` capability only (off by default).

Owner: ml-core (seeded verbatim from the contract). Built by ml-alpaca.
Every model carries ``environment`` (paper or live). Money is DecimalStr.
Instrument rule for rows that can hold any asset class: ``ticker`` is the
equity ticker or crypto pair, or for an option the underlying; ``occ_symbol``
is set for options only (None with not_applicable otherwise).
"""

from __future__ import annotations

from datetime import date as Date
from typing import ClassVar, Literal

from pydantic import Field

from .base import (
    AssetClass,
    CanonicalModel,
    DecimalStr,
    Environment,
    OccSymbol,
    OrderClass,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    Ticker,
    Timeframe,
    TimeInForce,
    UtcDatetime,
    unit,
)


class Account(CanonicalModel):
    """Balances and status. The account number is masked to its last four
    characters (``****1234``); the full number is never served (withheld)."""

    schema_name: ClassVar[str] = "marketlens.Account"

    environment: Environment
    account_id: str = Field(description="The provider's opaque account id")
    account_number_masked: str | None = None
    status: str
    crypto_status: str | None = None
    currency: str = "USD"
    cash: DecimalStr = unit("USD")
    equity: DecimalStr = unit("USD")
    last_equity: DecimalStr | None = unit("USD", default=None)
    buying_power: DecimalStr = unit("USD")
    regt_buying_power: DecimalStr | None = unit("USD", default=None)
    daytrading_buying_power: DecimalStr | None = unit("USD", default=None)
    non_marginable_buying_power: DecimalStr | None = unit("USD", default=None)
    options_buying_power: DecimalStr | None = unit("USD", default=None)
    long_market_value: DecimalStr | None = unit("USD", default=None)
    short_market_value: DecimalStr | None = unit("USD", default=None)
    initial_margin: DecimalStr | None = unit("USD", default=None)
    maintenance_margin: DecimalStr | None = unit("USD", default=None)
    last_maintenance_margin: DecimalStr | None = unit("USD", default=None)
    sma: DecimalStr | None = unit("USD", default=None)
    accrued_fees: DecimalStr | None = unit("USD", default=None)
    pending_transfer_in: DecimalStr | None = unit("USD", default=None)
    pending_transfer_out: DecimalStr | None = unit("USD", default=None)
    multiplier: DecimalStr | None = unit(
        "multiplier", default=None, description="Margin multiplier (1, 2 or 4)"
    )
    daytrade_count: int | None = unit("count", default=None)
    pattern_day_trader: bool | None = None
    trading_blocked: bool | None = None
    transfers_blocked: bool | None = None
    account_blocked: bool | None = None
    trade_suspended_by_user: bool | None = None
    shorting_enabled: bool | None = None
    options_approved_level: int | None = None
    options_trading_level: int | None = None
    created_at: UtcDatetime | None = unit("UTC", default=None)
    balance_as_of: Date | None = None


class AccountConfig(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.AccountConfig"

    environment: Environment
    dtbp_check: str | None = None
    pdt_check: str | None = None
    trade_confirm_email: str | None = None
    suspend_trade: bool | None = None
    no_shorting: bool | None = None
    fractional_trading: bool | None = None
    max_margin_multiplier: DecimalStr | None = unit("multiplier", default=None)
    max_options_trading_level: int | None = None
    ptp_no_exception_entry: bool | None = None
    disable_overnight_trading: bool | None = None


class Position(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Position"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("market_value", "unrealized_pl", "unrealized_plpc")

    environment: Environment
    asset_class: AssetClass
    ticker: Ticker
    occ_symbol: OccSymbol | None = None
    exchange: str | None = None
    side: PositionSide
    qty: DecimalStr = unit("by_asset_class")
    qty_available: DecimalStr | None = unit("by_asset_class", default=None)
    avg_entry_price: DecimalStr = unit("USD")
    cost_basis: DecimalStr = unit("USD")
    market_value: DecimalStr | None = unit("USD", default=None)
    current_price: DecimalStr | None = unit("USD", default=None)
    lastday_price: DecimalStr | None = unit("USD", default=None)
    change_today: float | None = unit("fraction", default=None)
    unrealized_pl: DecimalStr | None = unit("USD", default=None)
    unrealized_plpc: float | None = unit("fraction", default=None)
    unrealized_intraday_pl: DecimalStr | None = unit("USD", default=None)
    unrealized_intraday_plpc: float | None = unit("fraction", default=None)
    asset_marginable: bool | None = None
    provider_asset_id: str | None = None


class OrderLeg(CanonicalModel):
    """One leg of a multi-leg or bracket order (same fields as Order, no legs)."""

    schema_name: ClassVar[str] = "marketlens.OrderLeg"

    order_id: str
    client_order_id: str | None = None
    asset_class: AssetClass
    ticker: Ticker
    occ_symbol: OccSymbol | None = None
    side: Side | None = None
    position_intent: str | None = None
    order_type: OrderType | None = None
    status: OrderStatus
    qty: DecimalStr | None = unit("by_asset_class", default=None)
    ratio_qty: DecimalStr | None = unit("count", default=None)
    filled_qty: DecimalStr | None = unit("by_asset_class", default=None)
    filled_avg_price: DecimalStr | None = unit("USD", default=None)
    limit_price: DecimalStr | None = unit("USD", default=None)
    stop_price: DecimalStr | None = unit("USD", default=None)
    created_at: UtcDatetime | None = unit("UTC", default=None)
    filled_at: UtcDatetime | None = unit("UTC", default=None)


class Order(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Order"
    time_column: ClassVar[str | None] = "created_at"
    group_column: ClassVar[str | None] = "ticker"

    environment: Environment
    order_id: str
    client_order_id: str | None = None
    asset_class: AssetClass
    ticker: Ticker
    occ_symbol: OccSymbol | None = None
    side: Side | None = Field(default=None, description="None for a multi-leg parent (see legs)")
    order_type: OrderType | None = None
    order_class: OrderClass | None = None
    time_in_force: TimeInForce | None = None
    status: OrderStatus
    position_intent: str | None = None
    qty: DecimalStr | None = unit("by_asset_class", default=None)
    notional: DecimalStr | None = unit("USD", default=None)
    filled_qty: DecimalStr = unit("by_asset_class")
    filled_avg_price: DecimalStr | None = unit("USD", default=None)
    limit_price: DecimalStr | None = unit("USD", default=None)
    stop_price: DecimalStr | None = unit("USD", default=None)
    trail_price: DecimalStr | None = unit("USD", default=None)
    trail_percent: float | None = unit("fraction", default=None)
    hwm: DecimalStr | None = unit("USD", default=None)
    extended_hours: bool | None = None
    created_at: UtcDatetime = unit("UTC")
    updated_at: UtcDatetime | None = unit("UTC", default=None)
    submitted_at: UtcDatetime | None = unit("UTC", default=None)
    filled_at: UtcDatetime | None = unit("UTC", default=None)
    expired_at: UtcDatetime | None = unit("UTC", default=None)
    expires_at: UtcDatetime | None = unit("UTC", default=None)
    canceled_at: UtcDatetime | None = unit("UTC", default=None)
    failed_at: UtcDatetime | None = unit("UTC", default=None)
    replaced_at: UtcDatetime | None = unit("UTC", default=None)
    replaced_by: str | None = None
    replaces: str | None = None
    legs: list[OrderLeg] | None = None


class Activity(CanonicalModel):
    """One account activity: a fill (category trade) or a non-trade event
    (dividend, fee, transfer, corporate action, ...)."""

    schema_name: ClassVar[str] = "marketlens.Activity"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "ticker"

    environment: Environment
    activity_id: str
    activity_type: str = Field(description="The provider's activity code, e.g. FILL, DIV, FEE, CSD, JNLC")
    category: Literal["trade", "non_trade"]
    t: UtcDatetime = unit(
        "UTC",
        description="Transaction time; a date-only activity is placed at 00:00 UTC of its date (see `date`)",
    )
    date: Date | None = None
    ticker: Ticker | None = None
    occ_symbol: OccSymbol | None = None
    side: Side | None = None
    qty: DecimalStr | None = unit("by_asset_class", default=None)
    price: DecimalStr | None = unit("USD", default=None)
    cum_qty: DecimalStr | None = unit("by_asset_class", default=None)
    leaves_qty: DecimalStr | None = unit("by_asset_class", default=None)
    order_id: str | None = None
    order_status: OrderStatus | None = None
    net_amount: DecimalStr | None = unit("USD", default=None)
    per_share_amount: DecimalStr | None = unit("USD", default=None)
    currency: str | None = None
    status: str | None = None
    description: str | None = None


class PortfolioHistoryPoint(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.PortfolioHistoryPoint"
    time_column: ClassVar[str | None] = "t"
    value_columns: ClassVar[tuple[str, ...]] = ("equity", "profit_loss", "profit_loss_pct")

    environment: Environment
    timeframe: Timeframe
    t: UtcDatetime = unit("UTC")
    equity: DecimalStr | None = unit("USD", default=None)
    profit_loss: DecimalStr | None = unit("USD", default=None)
    profit_loss_pct: float | None = unit("fraction", default=None)
    base_value: DecimalStr | None = unit("USD", default=None)


class BrokerWatchlist(CanonicalModel):
    """A watchlist kept at the broker (not a product list). ``tickers`` is
    None on the list endpoint (not_provided_by_source)."""

    schema_name: ClassVar[str] = "marketlens.BrokerWatchlist"

    environment: Environment
    watchlist_id: str
    name: str
    created_at: UtcDatetime | None = unit("UTC", default=None)
    updated_at: UtcDatetime | None = unit("UTC", default=None)
    tickers: list[Ticker] | None = None


class Locate(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Locate"
    group_column: ClassVar[str | None] = "ticker"

    environment: Environment
    locate_id: str
    ticker: Ticker
    status: str
    requested_qty: int = unit("shares")
    located_qty: int | None = unit("shares", default=None)
    located_price: DecimalStr | None = unit("USD", default=None)
    limit_price: DecimalStr | None = unit("USD", default=None)
    total_fee: DecimalStr | None = unit("USD", default=None)
    all_or_none: bool | None = None
    rejection_reason: str | None = None
    created_at: UtcDatetime | None = unit("UTC", default=None)
    expires_at: UtcDatetime | None = unit("UTC", default=None)


class LocateQuote(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.LocateQuote"
    group_column: ClassVar[str | None] = "ticker"

    environment: Environment
    ticker: Ticker
    available_qty: int | None = unit("shares", default=None)
    price: DecimalStr | None = unit("USD", default=None, description="Locate fee per share")
    quoted_at: UtcDatetime | None = unit("UTC", default=None)


PORTFOLIO_MODELS: tuple[type[CanonicalModel], ...] = (
    Account,
    AccountConfig,
    Position,
    OrderLeg,
    Order,
    Activity,
    PortfolioHistoryPoint,
    BrokerWatchlist,
    Locate,
    LocateQuote,
)
