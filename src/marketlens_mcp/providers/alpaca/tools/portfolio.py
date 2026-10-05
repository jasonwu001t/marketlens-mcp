"""Brokerage-account reads (capability ``portfolio``, off by default): what the
model reads here leaves the machine with its requests. The paper account is
read unless ``portfolio.environment`` is ``live``. Read only: nothing here can
place, change or cancel anything (see exclusions)."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import date as Date
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from marketlens_mcp.plugin_api import PageResult, ToolContext, ToolError, ToolOutput
from marketlens_schema.base import Delay
from marketlens_schema.portfolio import (
    Account,
    AccountConfig,
    Activity,
    BrokerWatchlist,
    Locate,
    LocateQuote,
    Order,
    PortfolioHistoryPoint,
    Position,
)

from .. import mappers_portfolio as m
from ..client import AlpacaClient
from ..convert import alpaca_symbol, iso_z, parse_instant, path_symbol
from .common import (
    CONTINUE_DOC,
    STORED_NOTE,
    EquityTicker,
    EquityTickers,
    Inputs,
    Instant,
    OccSymbol,
    Skips,
    TickerOrPair,
    absence,
    collect,
    latest_t,
    output,
    request_of,
    spec,
)
from .marketdata import fetch_once

GOLDEN = "tests/alpaca/test_alpaca_portfolio.py"
ACTIVITIES_PAGE_MAX = 100
ORDERS_PAGE_MAX = 500
LOCATES_PAGE_MAX = 10_000
ORDER_CURSOR_PREFIX = "o1."
#: Alpaca's activity types (trading API spec, ActivityType).
ACTIVITY_TYPES = frozenset(
    [
        "FILL",
        "TRANS",
        "MISC",
        "ACATC",
        "ACATS",
        "CFEE",
        "CGD",
        "CSD",
        "CSW",
        "DIV",
        "DIVCGL",
        "DIVCGS",
        "DIVFEE",
        "DIVFT",
        "DIVNRA",
        "DIVROC",
        "DIVTW",
        "DIVTXEX",
        "FEE",
        "INT",
        "INTNRA",
        "INTTW",
        "JNL",
        "JNLC",
        "JNLS",
        "MA",
        "NC",
        "OPASN",
        "OPCA",
        "OPCSH",
        "OPEXC",
        "OPEXP",
        "OPTRD",
        "PTC",
        "PTR",
        "REO",
        "REORG",
        "SPIN",
        "SPLIT",
        "FOPT",
        "OCT",
    ]
)
_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


def _activity_types(v: list[str]) -> list[str]:
    out = []
    for item in v:
        t = item.strip().upper()
        if t not in ACTIVITY_TYPES:
            raise ValueError(f"'{item}' is not an Alpaca activity type (e.g. FILL, DIV, FEE, CSD, JNLC)")
        out.append(t)
    return out


ActivityTypes = Annotated[list[str], Field(min_length=1, max_length=41), AfterValidator(_activity_types)]


class NoArgs(Inputs):
    pass


class HistoryIn(Inputs):
    period: str | None = Field(
        None, pattern=r"^\d+[DWMA]$", description="Length, e.g. 1D, 1W, 3M, 1A (year)."
    )
    timeframe: Literal["1min", "5min", "15min", "1h", "1d"] | None = Field(
        None, description="Resolution; Alpaca picks one from the period when omitted."
    )
    start: Instant | None = Field(None, description="Start instant (ISO date or datetime with zone).")
    end: Instant | None = Field(None, description="End instant.")
    intraday_reporting: Literal["market_hours", "extended_hours", "continuous"] | None = None
    pnl_reset: Literal["no_reset", "per_day"] | None = Field(None, description="Baseline for intraday P&L.")
    extended_hours: bool | None = Field(None, description="Deprecated by Alpaca; prefer intraday_reporting.")


class ActivitiesIn(Inputs):
    activity_types: ActivityTypes | None = Field(
        None, description="Only these types (FILL, DIV, FEE, CSD, ...)."
    )
    category: Literal["trade", "non_trade"] | None = Field(None, description="Fills or everything else.")
    date: Date | None = Field(None, description="Only activities created on this date.")
    after: Instant | None = Field(None, description="Created after this instant.")
    until: Instant | None = Field(None, description="Created before this instant.")
    direction: Literal["asc", "desc"] = Field("desc", description="Chronological order; desc = newest first.")
    page_token: str | None = Field(None, max_length=200, description=CONTINUE_DOC)

    @model_validator(mode="after")
    def _exclusive(self) -> ActivitiesIn:
        if self.category and self.activity_types:
            raise ValueError("category cannot be combined with activity_types (an Alpaca rule)")
        return self


class OrdersIn(Inputs):
    status: Literal["open", "closed", "all"] = Field(
        "open", description="open (Alpaca's default), closed or all."
    )
    after: Instant | None = Field(None, description="Submitted after this instant.")
    until: Instant | None = Field(None, description="Submitted before this instant.")
    direction: Literal["asc", "desc"] = Field("desc", description="By submission time; desc = newest first.")
    tickers: list[TickerOrPair] | None = Field(
        None, max_length=200, description="Only these tickers or pairs."
    )
    side: Literal["buy", "sell"] | None = None
    nested: bool = Field(True, description="Roll multi-leg orders up under their parent's legs.")
    page_token: str | None = Field(None, max_length=500, description=CONTINUE_DOC)


class OrderIn(Inputs):
    order_id: str | None = Field(None, pattern=_ID, description="Alpaca order id.")
    client_order_id: str | None = Field(None, max_length=128, description="Your client order id.")

    @model_validator(mode="after")
    def _one(self) -> OrderIn:
        if (self.order_id is None) == (self.client_order_id is None):
            raise ValueError("pass exactly one of order_id or client_order_id")
        return self


class PositionIn(Inputs):
    ticker: TickerOrPair | None = Field(None, description="Stock ticker or crypto pair.")
    occ_symbol: OccSymbol | None = Field(None, description="OCC option symbol.")

    @model_validator(mode="after")
    def _one(self) -> PositionIn:
        if (self.ticker is None) == (self.occ_symbol is None):
            raise ValueError("pass exactly one of ticker or occ_symbol")
        return self


class WatchlistIn(Inputs):
    watchlist_id: str = Field(pattern=_ID, description="Broker watchlist id.")


class LocatesIn(Inputs):
    status: Literal["active", "expired", "rejected"] | None = None
    ticker: EquityTicker | None = None
    start: Date | None = Field(None, description="Locate trading date on or after.")
    end: Date | None = Field(None, description="Locate trading date before (exclusive).")
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class LocateIn(Inputs):
    locate_id: str = Field(pattern=_ID, description="Locate id.")


class LocateQuotesIn(Inputs):
    tickers: Annotated[EquityTickers, Field(max_length=100)]


# --- helpers ---------------------------------------------------------------------------------


def _out(
    ctx: ToolContext,
    model,
    rows,
    *,
    route: str,
    operation: str,
    args,
    page: PageResult | None = None,
    as_of=None,
    absent=None,
    notes=None,
    skips=None,
    **request_extra,
) -> ToolOutput:
    return output(
        ctx,
        model,
        rows,
        route=route,
        operation=operation,
        request=request_of(args, **request_extra),
        delay=Delay.REALTIME,
        environment=ctx.portfolio_environment,
        as_of=as_of,
        page=page,
        absent=absent,
        notes=notes,
        skips=skips,
    )


async def _single(ctx: ToolContext, path: str, mapper, label: str, model, params=None):
    skips = Skips(model.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("trading", path, params, decimals=True)
            return collect(skips, data if isinstance(data, list) else [data], mapper, lambda r: label)

        page = await fetch_once(ctx, fetch)
    return page, skips


def _order_token(position: dict[str, str]) -> str:
    return ORDER_CURSOR_PREFIX + base64.urlsafe_b64encode(json.dumps(position).encode()).decode()


def _order_position(token: str) -> dict[str, str]:
    try:
        if not token.startswith(ORDER_CURSOR_PREFIX):
            raise ValueError
        pos = json.loads(base64.urlsafe_b64decode(token[len(ORDER_CURSOR_PREFIX) :].encode()))
        if not isinstance(pos, dict) or len(pos) != 1 or not set(pos) <= {"until", "after"}:
            raise ValueError
        (value,) = pos.values()
        parse_instant(str(value))
        return {k: str(v) for k, v in pos.items()}
    except (ValueError, binascii.Error, json.JSONDecodeError) as e:
        raise ToolError(
            "invalid_page_token",
            "This page_token was not issued by portfolio_orders.",
            hint="Pass the page_token from the previous portfolio_orders response unchanged.",
        ) from e


# --- handlers --------------------------------------------------------------------------------


async def portfolio_account(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    env = ctx.portfolio_environment
    page, skips = await _single(ctx, "/v2/account", lambda r: m.account(r, env), "account", Account)
    return _out(
        ctx,
        Account,
        page.rows,
        route="GET /v2/account",
        operation="getAccount",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_account_config(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    env = ctx.portfolio_environment
    page, skips = await _single(
        ctx, "/v2/account/configurations", lambda r: m.account_config(r, env), "config", AccountConfig
    )
    return _out(
        ctx,
        AccountConfig,
        page.rows,
        route="GET /v2/account/configurations",
        operation="getAccountConfig",
        args=args,
        page=page,
        skips=skips,
    )


_HISTORY_TF = {"1min": "1Min", "5min": "5Min", "15min": "15Min", "1h": "1H", "1d": "1D"}


async def portfolio_history(ctx: ToolContext, args: HistoryIn) -> ToolOutput:
    env = ctx.portfolio_environment
    params = {
        "period": args.period,
        "timeframe": _HISTORY_TF[args.timeframe] if args.timeframe else None,
        "start": iso_z(parse_instant(args.start)) if args.start else None,
        "end": iso_z(parse_instant(args.end)) if args.end else None,
        "intraday_reporting": args.intraday_reporting,
        "pnl_reset": args.pnl_reset,
        "extended_hours": args.extended_hours,
    }
    skips = Skips(PortfolioHistoryPoint.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("trading", "/v2/account/portfolio/history", params, decimals=True)
            return collect(skips, [data], lambda r: m.history(r, env), lambda r: "history")

        page = await fetch_once(ctx, fetch)
    return _out(
        ctx,
        PortfolioHistoryPoint,
        page.rows,
        route="GET /v2/account/portfolio/history",
        operation="getAccountPortfolioHistory",
        args=args,
        page=page,
        as_of=latest_t(page.rows),
        skips=skips,
    )


async def portfolio_activities(ctx: ToolContext, args: ActivitiesIn) -> ToolOutput:
    env = ctx.portfolio_environment
    by_type = args.activity_types is not None and len(args.activity_types) == 1
    path = f"/v2/account/activities/{args.activity_types[0]}" if by_type else "/v2/account/activities"
    params = {
        "activity_types": None if by_type else args.activity_types,
        "category": f"{args.category}_activity" if args.category else None,
        "date": args.date.isoformat() if args.date else None,
        "after": iso_z(parse_instant(args.after)) if args.after else None,
        "until": iso_z(parse_instant(args.until)) if args.until else None,
        "direction": args.direction,
    }
    skips = Skips(Activity.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch_page(token: str | None, limit: int):
            size = min(limit, ACTIVITIES_PAGE_MAX)
            data = await api.get(
                "trading", path, {**params, "page_size": size, "page_token": token}, decimals=True
            )
            data = data or []
            rows = collect(skips, data, lambda r: m.activity(r, env), lambda r: str(r.get("id")))
            return rows, (str(data[-1]["id"]) if len(data) >= size and data else None)

        page = await ctx.paginate(fetch_page, start_token=args.page_token)
    return _out(
        ctx,
        Activity,
        page.rows,
        route="GET /v2/account/activities/{activity_type}" if by_type else "GET /v2/account/activities",
        operation="getAccountActivitiesByActivityType" if by_type else "getAccountActivities",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_orders(ctx: ToolContext, args: OrdersIn) -> ToolOutput:
    env = ctx.portfolio_environment
    if args.page_token:
        _order_position(args.page_token)  # refuse a forged token before any request
    base = {
        "status": args.status,
        "after": iso_z(parse_instant(args.after)) if args.after else None,
        "until": iso_z(parse_instant(args.until)) if args.until else None,
        "direction": args.direction,
        "symbols": [alpaca_symbol(t) for t in args.tickers] if args.tickers else None,
        "side": args.side,
        "nested": args.nested,
    }
    skips = Skips(Order.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch_page(token: str | None, limit: int):
            size = min(limit, ORDERS_PAGE_MAX)
            params = {**base, **(_order_position(token) if token else {}), "limit": size}
            data = await api.get("trading", "/v2/orders", params, decimals=True) or []
            rows = collect(skips, data, lambda r: m.order(r, env), lambda r: str(r.get("id")))
            if len(data) < size or not data:
                return rows, None
            key = "until" if args.direction == "desc" else "after"
            last = data[-1].get("submitted_at") or data[-1].get("created_at")
            return rows, (_order_token({key: last}) if last else None)

        page = await ctx.paginate(fetch_page, start_token=args.page_token)
    return _out(
        ctx,
        Order,
        page.rows,
        route="GET /v2/orders",
        operation="getAllOrders",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_order(ctx: ToolContext, args: OrderIn) -> ToolOutput:
    env = ctx.portfolio_environment
    if args.order_id:
        path, params, route, op = (
            f"/v2/orders/{args.order_id}",
            {"nested": True},
            "GET /v2/orders/{order_id}",
            "getOrderByOrderID",
        )
    else:
        path, params, route, op = (
            "/v2/orders:by_client_order_id",
            {"client_order_id": args.client_order_id},
            "GET /v2/orders:by_client_order_id",
            "getOrderByClientOrderId",
        )
    page, skips = await _single(ctx, path, lambda r: m.order(r, env), args.order_id or "order", Order, params)
    return _out(ctx, Order, page.rows, route=route, operation=op, args=args, page=page, skips=skips)


async def portfolio_positions(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    env = ctx.portfolio_environment
    page, skips = await _single(ctx, "/v2/positions", lambda r: m.position(r, env), "position", Position)
    return _out(
        ctx,
        Position,
        page.rows,
        route="GET /v2/positions",
        operation="getAllOpenPositions",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_position(ctx: ToolContext, args: PositionIn) -> ToolOutput:
    env = ctx.portfolio_environment
    segment = args.occ_symbol or path_symbol(args.ticker or "")
    page, skips = await _single(
        ctx, f"/v2/positions/{segment}", lambda r: m.position(r, env), segment, Position
    )
    return _out(
        ctx,
        Position,
        page.rows,
        route="GET /v2/positions/{symbol_or_asset_id}",
        operation="getOpenPosition",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_broker_watchlists(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    env = ctx.portfolio_environment
    page, skips = await _single(
        ctx, "/v2/watchlists", lambda r: m.watchlist(r, env, with_tickers=False), "watchlist", BrokerWatchlist
    )
    absent = {
        "tickers": absence(
            "not_provided_by_source",
            "The list endpoint omits contents; read one with portfolio_broker_watchlist.",
        )
    }
    return _out(
        ctx,
        BrokerWatchlist,
        page.rows,
        route="GET /v2/watchlists",
        operation="getWatchlists",
        args=args,
        page=page,
        absent=absent,
        skips=skips,
    )


async def portfolio_broker_watchlist(ctx: ToolContext, args: WatchlistIn) -> ToolOutput:
    env = ctx.portfolio_environment
    page, skips = await _single(
        ctx,
        f"/v2/watchlists/{args.watchlist_id}",
        lambda r: m.watchlist(r, env, with_tickers=True),
        args.watchlist_id,
        BrokerWatchlist,
    )
    return _out(
        ctx,
        BrokerWatchlist,
        page.rows,
        route="GET /v2/watchlists/{watchlist_id}",
        operation="getWatchlistById",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_locates(ctx: ToolContext, args: LocatesIn) -> ToolOutput:
    env = ctx.portfolio_environment
    params = {
        "status": args.status,
        "symbol": alpaca_symbol(args.ticker) if args.ticker else None,
        "start": args.start.isoformat() if args.start else None,
        "end": args.end.isoformat() if args.end else None,
    }
    skips = Skips(Locate.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch_page(token: str | None, limit: int):
            data = await api.get(
                "trading",
                "/v1/locates",
                {**params, "limit": min(limit, LOCATES_PAGE_MAX), "page_token": token},
                decimals=True,
            )
            rows = collect(
                skips, data.get("locates") or [], lambda r: m.locate(r, env), lambda r: str(r.get("id"))
            )
            return rows, data.get("next_page_token") or None

        page = await ctx.paginate(fetch_page, start_token=args.page_token)
    return _out(
        ctx,
        Locate,
        page.rows,
        route="GET /v1/locates",
        operation="listLocates",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_locate(ctx: ToolContext, args: LocateIn) -> ToolOutput:
    env = ctx.portfolio_environment
    page, skips = await _single(
        ctx, f"/v1/locates/{args.locate_id}", lambda r: m.locate(r, env), args.locate_id, Locate
    )
    return _out(
        ctx,
        Locate,
        page.rows,
        route="GET /v1/locates/{locate_id}",
        operation="getLocate",
        args=args,
        page=page,
        skips=skips,
    )


async def portfolio_locate_quotes(ctx: ToolContext, args: LocateQuotesIn) -> ToolOutput:
    env = ctx.portfolio_environment
    skips = Skips(LocateQuote.schema_name)
    errors: list[str] = []
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get(
                "trading",
                "/v1/locates/quotes",
                {"symbols": [alpaca_symbol(t) for t in args.tickers]},
                decimals=True,
            )
            errors.extend(
                f"{e.get('symbol')}: {e.get('message') or e.get('code')}" for e in data.get("errors") or []
            )
            return collect(
                skips,
                data.get("quotes") or [],
                lambda r: m.locate_quote(r, env),
                lambda r: str(r.get("symbol")),
            )

        page = await fetch_once(ctx, fetch)
    notes = [f"Alpaca could not quote: {'; '.join(errors)}."] if errors else []
    return _out(
        ctx,
        LocateQuote,
        page.rows,
        route="GET /v1/locates/quotes",
        operation="listLocateQuotes",
        args=args,
        page=page,
        notes=notes,
        skips=skips,
    )


_P = " Brokerage data: the paper account unless portfolio.environment is live."
_T = " (trading API)"
SPECS = (
    spec(
        name="portfolio_account",
        capability="portfolio",
        title="Account",
        description="The brokerage account's status and balances (USD, exact decimals): cash, equity, buying "
        "powers, market values, margins, day-trade count, blocks, options levels. The account number is masked "
        "to its last four characters." + _P,
        readme="Balances and account status",
        input_model=NoArgs,
        output_model=Account,
        route=f"GET /v2/account{_T}",
        handler=portfolio_account,
        golden_test=GOLDEN,
        operations=("getAccount",),
        parity=("get_account_info",),
    ),
    spec(
        name="portfolio_account_config",
        capability="portfolio",
        title="Account configuration",
        description="The account's trading configuration (read only): day-trade and PDT checks, shorting, "
        "fractional trading, margin multiplier, options level, overnight trading." + _P,
        readme="Account trading settings (read only)",
        input_model=NoArgs,
        output_model=AccountConfig,
        route=f"GET /v2/account/configurations{_T}",
        handler=portfolio_account_config,
        golden_test=GOLDEN,
        operations=("getAccountConfig",),
        parity=("get_account_config",),
    ),
    spec(
        name="portfolio_history",
        capability="portfolio",
        title="Portfolio history",
        description="Account equity and profit/loss over time, one row per timestamp (left-labelled, UTC): "
        "equity and P&L in USD, P&L as a fraction of the base value. Choose period (1D, 1W, 3M, 1A) or "
        "start/end, and timeframe (1min, 5min, 15min, 1h, 1d)." + _P,
        readme="Equity and P&L over time",
        input_model=HistoryIn,
        output_model=PortfolioHistoryPoint,
        route=f"GET /v2/account/portfolio/history{_T}",
        handler=portfolio_history,
        golden_test=GOLDEN,
        operations=("getAccountPortfolioHistory",),
        parity=("get_portfolio_history",),
    ),
    spec(
        name="portfolio_activities",
        capability="portfolio",
        title="Account activities",
        description="Account activities: fills (category trade) and non-trade events such as dividends, fees, "
        "deposits, journals and corporate actions (category non_trade), filtered by type or category and time. "
        "Amounts in USD, exact. " + STORED_NOTE + _P,
        readme="Fills, dividends, fees, transfers",
        input_model=ActivitiesIn,
        output_model=Activity,
        route=f"GET /v2/account/activities{_T}",
        handler=portfolio_activities,
        golden_test=GOLDEN,
        operations=("getAccountActivities", "getAccountActivitiesByActivityType"),
        parity=("get_account_activities", "get_account_activities_by_type"),
    ),
    spec(
        name="portfolio_orders",
        capability="portfolio",
        title="Orders",
        description="Orders (read only) by status (open, closed or all), time window, tickers and side, with "
        "multi-leg orders nested under their legs; prices and quantities exact; trail_percent as a fraction. "
        + STORED_NOTE
        + _P,
        readme="Order history (read only)",
        input_model=OrdersIn,
        output_model=Order,
        route=f"GET /v2/orders{_T}",
        handler=portfolio_orders,
        golden_test=GOLDEN,
        operations=("getAllOrders",),
        parity=("get_orders",),
    ),
    spec(
        name="portfolio_order",
        capability="portfolio",
        title="Order",
        description="One order (read only) by order_id or by client_order_id (exactly one)." + _P,
        readme="One order by id",
        input_model=OrderIn,
        output_model=Order,
        route=f"GET /v2/orders/{{order_id}}{_T}",
        handler=portfolio_order,
        golden_test=GOLDEN,
        operations=("getOrderByOrderID", "getOrderByClientOrderId"),
        parity=("get_order_by_id", "get_order_by_client_id"),
    ),
    spec(
        name="portfolio_positions",
        capability="portfolio",
        title="Positions",
        description="Open positions: quantity, side, average entry, cost basis, market value, current and "
        "previous close, unrealized P&L in USD and as fractions. Options carry occ_symbol (ticker = the "
        "underlying)." + _P,
        readme="Open positions with P&L",
        input_model=NoArgs,
        output_model=Position,
        route=f"GET /v2/positions{_T}",
        handler=portfolio_positions,
        golden_test=GOLDEN,
        operations=("getAllOpenPositions",),
        parity=("get_all_positions",),
    ),
    spec(
        name="portfolio_position",
        capability="portfolio",
        title="Position",
        description="One open position by ticker / crypto pair or by OCC option symbol (exactly one)." + _P,
        readme="One open position",
        input_model=PositionIn,
        output_model=Position,
        route=f"GET /v2/positions/{{symbol_or_asset_id}}{_T}",
        handler=portfolio_position,
        golden_test=GOLDEN,
        operations=("getOpenPosition",),
        parity=("get_open_position",),
    ),
    spec(
        name="portfolio_broker_watchlists",
        capability="portfolio",
        title="Broker watchlists",
        description="Watchlists kept at the broker (names and ids; contents via portfolio_broker_watchlist)."
        + _P,
        readme="Broker watchlists (names)",
        input_model=NoArgs,
        output_model=BrokerWatchlist,
        route=f"GET /v2/watchlists{_T}",
        handler=portfolio_broker_watchlists,
        golden_test=GOLDEN,
        operations=("getWatchlists",),
        parity=("get_watchlists",),
    ),
    spec(
        name="portfolio_broker_watchlist",
        capability="portfolio",
        title="Broker watchlist",
        description="One broker watchlist by id with its tickers." + _P,
        readme="One broker watchlist's tickers",
        input_model=WatchlistIn,
        output_model=BrokerWatchlist,
        route=f"GET /v2/watchlists/{{watchlist_id}}{_T}",
        handler=portfolio_broker_watchlist,
        golden_test=GOLDEN,
        operations=("getWatchlistById",),
        parity=("get_watchlist_by_id",),
    ),
    spec(
        name="portfolio_locates",
        capability="portfolio",
        title="Short locates",
        description="Short-sale locates (read only): requested and located shares, fee per share and total fee "
        "(USD), status, rejection reason, filtered by status, ticker and trading-date window." + _P,
        readme="Short-sale locates (read only)",
        input_model=LocatesIn,
        output_model=Locate,
        route=f"GET /v1/locates{_T}",
        handler=portfolio_locates,
        golden_test=GOLDEN,
        operations=("listLocates",),
        parity=("get_locates",),
    ),
    spec(
        name="portfolio_locate",
        capability="portfolio",
        title="Short locate",
        description="One short-sale locate by id (read only)." + _P,
        readme="One locate by id",
        input_model=LocateIn,
        output_model=Locate,
        route=f"GET /v1/locates/{{locate_id}}{_T}",
        handler=portfolio_locate,
        golden_test=GOLDEN,
        operations=("getLocate",),
        parity=("get_locate",),
    ),
    spec(
        name="portfolio_locate_quotes",
        capability="portfolio",
        title="Locate quotes",
        description="Quotes for short-sale locates (no request is made): shares available and fee per share "
        "(USD) per ticker; tickers Alpaca cannot quote are listed in notes." + _P,
        readme="Locate availability and fees",
        input_model=LocateQuotesIn,
        output_model=LocateQuote,
        route=f"GET /v1/locates/quotes{_T}",
        handler=portfolio_locate_quotes,
        golden_test=GOLDEN,
        operations=("listLocateQuotes",),
        parity=("get_locate_quotes",),
    ),
)
