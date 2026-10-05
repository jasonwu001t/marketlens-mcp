"""Reference data (capability ``reference``): assets, option contracts and
exchanges, the market calendar and clock, corporate actions. The asset,
contract, calendar, clock and announcement endpoints live on the trading API
(paper or live base by portfolio.environment); the rest on the data API."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator

from marketlens_mcp.plugin_api import ToolContext, ToolOutput
from marketlens_schema.market import (
    Asset,
    CorporateAction,
    CorporateActionAnnouncement,
    MarketCalendarDay,
    MarketClock,
    OptionContract,
    OptionExchange,
)

from .. import mappers_reference as m
from ..client import AlpacaClient
from ..convert import alpaca_symbol, path_symbol
from .common import (
    CONTINUE_DOC,
    STORED_NOTE,
    EquityTicker,
    EquityTickers,
    Inputs,
    OccSymbol,
    Skips,
    TickerOrPair,
    absence,
    collect,
    output,
    request_of,
    spec,
)
from .marketdata import fetch_once

GOLDEN = "tests/alpaca/test_alpaca_reference.py"
CONTRACTS_PAGE_MAX = 10_000
CA_PAGE_MAX = 1000
ANNOUNCEMENT_MAX_DAYS = 90
CALENDAR_DEFAULT_DAYS = 31
ActionType = Literal[
    "cash_dividend", "stock_dividend", "forward_split", "reverse_split", "unit_split", "spin_off", "cash_merger",
    "stock_merger", "stock_and_cash_merger", "redemption", "name_change", "worthless_removal",
    "rights_distribution", "partial_call", "reorganization", "capital_gains_distribution",
]  # fmt: skip


class AssetsIn(Inputs):
    status: Literal["active", "inactive"] | None = Field(None, description="Only active or inactive assets.")
    asset_class: Literal["us_equity", "us_option", "crypto"] | None = Field(
        None, description="Asset class; Alpaca's default is us_equity."
    )
    exchange: Literal["AMEX", "ARCA", "BATS", "NYSE", "NASDAQ", "NYSEARCA", "OTC", "CRYPTO"] | None = Field(
        None, description="Listing venue."
    )
    attributes: list[str] | None = Field(
        None, max_length=10, description="Assets having any of these attributes, e.g. has_options, ipo."
    )


class AssetIn(Inputs):
    ticker: TickerOrPair = Field(description="Stock ticker (BRK-B) or crypto pair (BTC/USD).")


class ContractsIn(Inputs):
    underlyings: EquityTickers | None = Field(None, description="Underlying stock tickers.")
    status: Literal["active", "inactive"] | None = Field(None, description="Alpaca's default is active only.")
    expiration: date | None = Field(None, description="Exact expiration date.")
    expiration_from: date | None = Field(None, description="Earliest expiration date, inclusive.")
    expiration_to: date | None = Field(None, description="Latest expiration date, inclusive.")
    option_type: Literal["call", "put"] | None = None
    style: Literal["american", "european"] | None = None
    strike_min: float | None = Field(None, ge=0, description="Lowest strike (USD), inclusive.")
    strike_max: float | None = Field(None, ge=0, description="Highest strike (USD), inclusive.")
    root_symbol: str | None = Field(None, max_length=10, description="OCC root, for adjusted contracts.")
    show_deliverables: bool = Field(False, description="Include each contract's deliverables.")
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class ContractIn(Inputs):
    occ_symbol: OccSymbol = Field(description="OCC option symbol, e.g. AAPL250117C00150000.")


class CalendarIn(Inputs):
    start: date | None = Field(None, description="First trading day (default today, UTC).")
    end: date | None = Field(None, description="Last trading day (default start + 31 days).")


class NoArgs(Inputs):
    pass


class AnnouncementsIn(Inputs):
    ca_types: list[Literal["dividend", "merger", "spinoff", "split"]] = Field(
        min_length=1, description="Announcement types."
    )
    since: date = Field(description="Window start (inclusive), by date_type.")
    until: date = Field(description="Window end (inclusive); at most 90 days after since.")
    ticker: EquityTicker | None = Field(None, description="Only announcements initiated by this ticker.")
    cusip: str | None = Field(None, max_length=12, description="Only announcements initiated by this CUSIP.")
    date_type: Literal["declaration_date", "ex_date", "record_date", "payable_date"] | None = Field(
        None, description="Which date since/until filter on."
    )

    @model_validator(mode="after")
    def _check(self) -> AnnouncementsIn:
        if self.until < self.since:
            raise ValueError("until must not be before since")
        if (self.until - self.since).days > ANNOUNCEMENT_MAX_DAYS:
            raise ValueError("since and until may be at most 90 days apart (an Alpaca limit)")
        return self


class AnnouncementIn(Inputs):
    announcement_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class CorporateActionsIn(Inputs):
    tickers: EquityTickers | None = None
    cusips: list[str] | None = Field(None, max_length=200)
    types: list[ActionType] | None = Field(None, description="Action types; omit for all.")
    start: date | None = Field(None, description="First process date (Alpaca's default: today).")
    end: date | None = Field(None, description="Last process date (Alpaca's default: today).")
    ids: list[str] | None = Field(None, max_length=200, description="Specific action ids (no other filter).")
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)

    @model_validator(mode="after")
    def _ids_alone(self) -> CorporateActionsIn:
        if self.ids and any((self.tickers, self.cusips, self.types, self.start, self.end)):
            raise ValueError("ids cannot be combined with other filters (an Alpaca rule)")
        return self


# --- handlers --------------------------------------------------------------------------------


async def reference_assets(ctx: ToolContext, args: AssetsIn) -> ToolOutput:
    skips = Skips(Asset.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            params = {
                "status": args.status,
                "asset_class": args.asset_class,
                "exchange": args.exchange,
                "attributes": args.attributes,
            }
            data = await api.get("trading", "/v2/assets", params)
            return collect(skips, data or [], m.asset, lambda r: str(r.get("symbol")))

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        Asset,
        page.rows,
        route="GET /v2/assets",
        operation="get-v2-assets",
        request=request_of(args),
        page=page,
        skips=skips,
    )


async def reference_asset(ctx: ToolContext, args: AssetIn) -> ToolOutput:
    skips = Skips(Asset.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("trading", f"/v2/assets/{path_symbol(args.ticker)}")
            return collect(skips, [data], m.asset, lambda r: args.ticker)

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        Asset,
        page.rows,
        route="GET /v2/assets/{symbol_or_asset_id}",
        operation="get-v2-assets-symbol_or_asset_id",
        request=request_of(args),
        page=page,
        skips=skips,
    )


async def reference_option_contracts(ctx: ToolContext, args: ContractsIn) -> ToolOutput:
    skips = Skips(OptionContract.schema_name)
    async with AlpacaClient(ctx) as api:
        params = {
            "underlying_symbols": [alpaca_symbol(t) for t in args.underlyings] if args.underlyings else None,
            "status": args.status,
            "expiration_date": args.expiration.isoformat() if args.expiration else None,
            "expiration_date_gte": args.expiration_from.isoformat() if args.expiration_from else None,
            "expiration_date_lte": args.expiration_to.isoformat() if args.expiration_to else None,
            "type": args.option_type,
            "style": args.style,
            "strike_price_gte": args.strike_min,
            "strike_price_lte": args.strike_max,
            "root_symbol": args.root_symbol,
            "show_deliverables": args.show_deliverables,
        }

        async def fetch_page(token: str | None, limit: int):
            data = await api.get(
                "trading",
                "/v2/options/contracts",
                {**params, "limit": min(limit, CONTRACTS_PAGE_MAX), "page_token": token},
            )
            rows = collect(
                skips,
                data.get("option_contracts") or [],
                lambda r: m.option_contract(r, with_deliverables=args.show_deliverables),
                lambda r: str(r.get("symbol")),
            )
            return rows, data.get("next_page_token") or None

        page = await ctx.paginate(fetch_page, start_token=args.page_token)
    absent = (
        {}
        if args.show_deliverables
        else {
            "deliverables": absence("not_provided_by_source", "Not requested: pass show_deliverables=true.")
        }
    )
    return output(
        ctx,
        OptionContract,
        page.rows,
        route="GET /v2/options/contracts",
        operation="get-options-contracts",
        request=request_of(args),
        page=page,
        absent=absent,
        skips=skips,
    )


async def reference_option_contract(ctx: ToolContext, args: ContractIn) -> ToolOutput:
    skips = Skips(OptionContract.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("trading", f"/v2/options/contracts/{args.occ_symbol}")
            return collect(
                skips,
                [data],
                lambda r: m.option_contract(r, with_deliverables=True),
                lambda r: args.occ_symbol,
            )

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        OptionContract,
        page.rows,
        route="GET /v2/options/contracts/{symbol_or_id}",
        operation="get-option-contract-symbol_or_id",
        request=request_of(args),
        page=page,
        skips=skips,
    )


async def reference_calendar(ctx: ToolContext, args: CalendarIn) -> ToolOutput:
    start = args.start or ctx.now().date()
    span = timedelta(days=CALENDAR_DEFAULT_DAYS)
    end = args.end or (start + span if date.max - start > span else date.max)
    skips = Skips(MarketCalendarDay.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get(
                "trading", "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()}
            )
            return collect(skips, data or [], m.calendar_day, lambda r: str(r.get("date")))

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        MarketCalendarDay,
        page.rows,
        route="GET /v2/calendar",
        operation="LegacyCalendar",
        request=request_of(args, start=start.isoformat(), end=end.isoformat()),
        page=page,
        skips=skips,
        notes=["Session times are New York wall-clock times converted to UTC."],
    )


async def reference_clock(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    skips = Skips(MarketClock.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            return collect(skips, [await api.get("trading", "/v2/clock")], m.clock, lambda r: "clock")

        page = await fetch_once(ctx, fetch)
    as_of = page.rows[0].t if page.rows else None
    return output(
        ctx,
        MarketClock,
        page.rows,
        route="GET /v2/clock",
        operation="LegacyClock",
        request={},
        as_of=as_of,
        page=page,
        skips=skips,
    )


async def reference_corporate_action_announcements(ctx: ToolContext, args: AnnouncementsIn) -> ToolOutput:
    skips = Skips(CorporateActionAnnouncement.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            params = {
                "ca_types": [t.capitalize() for t in args.ca_types],
                "since": args.since.isoformat(),
                "until": args.until.isoformat(),
                "symbol": alpaca_symbol(args.ticker) if args.ticker else None,
                "cusip": args.cusip,
                "date_type": args.date_type,
            }
            data = await api.get("trading", "/v2/corporate_actions/announcements", params)
            return collect(skips, data or [], m.announcement, lambda r: str(r.get("id")))

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        CorporateActionAnnouncement,
        page.rows,
        route="GET /v2/corporate_actions/announcements",
        operation="get-v2-corporate_actions-announcements",
        request=request_of(args),
        page=page,
        skips=skips,
    )


async def reference_corporate_action_announcement(ctx: ToolContext, args: AnnouncementIn) -> ToolOutput:
    skips = Skips(CorporateActionAnnouncement.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("trading", f"/v2/corporate_actions/announcements/{args.announcement_id}")
            return collect(skips, [data], m.announcement, lambda r: args.announcement_id)

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        CorporateActionAnnouncement,
        page.rows,
        route="GET /v2/corporate_actions/announcements/{id}",
        operation="get-v2-corporate_actions-announcements-id",
        request=request_of(args),
        page=page,
        skips=skips,
    )


async def reference_corporate_actions(ctx: ToolContext, args: CorporateActionsIn) -> ToolOutput:
    skips = Skips(CorporateAction.schema_name)
    async with AlpacaClient(ctx) as api:
        params = {
            "symbols": [alpaca_symbol(t) for t in args.tickers] if args.tickers else None,
            "cusips": args.cusips,
            "types": args.types,
            "start": args.start.isoformat() if args.start else None,
            "end": args.end.isoformat() if args.end else None,
            "ids": args.ids,
        }

        async def fetch_page(token: str | None, limit: int):
            data = await api.get(
                "data",
                "/v1/corporate-actions",
                {**params, "limit": min(limit, CA_PAGE_MAX), "page_token": token},
            )
            rows = []
            for key, records in (data.get("corporate_actions") or {}).items():
                action_type = m.CA_ARRAYS.get(key)
                if action_type is None:
                    skips.labels.extend(f"{key}:{r.get('id')}" for r in records or [])
                    continue
                rows += collect(
                    skips,
                    records or [],
                    lambda r, a=action_type: m.corporate_action(r, a),
                    lambda r, k=key: f"{k}:{r.get('id')}",
                )
            return rows, data.get("next_page_token") or None

        page = await ctx.paginate(fetch_page, start_token=args.page_token)
    return output(
        ctx,
        CorporateAction,
        page.rows,
        route="GET /v1/corporate-actions",
        operation="CorporateActions",
        request=request_of(args),
        page=page,
        skips=skips,
    )


async def reference_option_exchanges(ctx: ToolContext, args: NoArgs) -> ToolOutput:
    skips = Skips(OptionExchange.schema_name)
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            data = await api.get("data", "/v1beta1/options/meta/exchanges")
            return collect(
                skips,
                list((data or {}).items()),
                lambda kv: m.option_exchange(kv[0], kv[1]),
                lambda kv: str(kv[0]),
            )

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        OptionExchange,
        page.rows,
        route="GET /v1beta1/options/meta/exchanges",
        operation="OptionMetaExchanges",
        request={},
        page=page,
        skips=skips,
    )


_T = " (trading API)"
_M = " (market data API)"
SPECS = (
    spec(
        name="reference_assets",
        capability="reference",
        title="Assets",
        description="Tradable assets with their attributes: ticker, class, venue, status, tradable, marginable, "
        "shortable, easy to borrow, fractionable, margin requirements (fractions), crypto order increments. "
        "Filter by status, class, exchange or attributes; the full list is large. " + STORED_NOTE,
        readme="Asset list with trading attributes",
        input_model=AssetsIn,
        output_model=Asset,
        route=f"GET /v2/assets{_T}",
        handler=reference_assets,
        golden_test=GOLDEN,
        operations=("get-v2-assets",),
        parity=("get_all_assets",),
    ),
    spec(
        name="reference_asset",
        capability="reference",
        title="Asset",
        description="One asset by stock ticker (BRK-B) or crypto pair (BTC/USD), with its trading attributes.",
        readme="One asset's attributes",
        input_model=AssetIn,
        output_model=Asset,
        route=f"GET /v2/assets/{{symbol_or_asset_id}}{_T}",
        handler=reference_asset,
        golden_test=GOLDEN,
        operations=("get-v2-assets-symbol_or_asset_id",),
        parity=("get_asset",),
    ),
    spec(
        name="reference_option_contracts",
        capability="reference",
        title="Option contracts",
        description="Listed option contracts (OCC symbol, underlying, expiration, strike in USD, type, style, "
        "multiplier, open interest, last close) filtered by underlyings, expiration (exact or from/to), type, "
        "style, strike range and root; deliverables on request. " + STORED_NOTE,
        readme="Option contract reference data",
        input_model=ContractsIn,
        output_model=OptionContract,
        route=f"GET /v2/options/contracts{_T}",
        handler=reference_option_contracts,
        golden_test=GOLDEN,
        operations=("get-options-contracts",),
        parity=("get_option_contracts",),
    ),
    spec(
        name="reference_option_contract",
        capability="reference",
        title="Option contract",
        description="One option contract by OCC symbol, with its deliverables.",
        readme="One contract with deliverables",
        input_model=ContractIn,
        output_model=OptionContract,
        route=f"GET /v2/options/contracts/{{symbol_or_id}}{_T}",
        handler=reference_option_contract,
        golden_test=GOLDEN,
        operations=("get-option-contract-symbol_or_id",),
        parity=("get_option_contract",),
    ),
    spec(
        name="reference_calendar",
        capability="reference",
        title="Market calendar",
        description="US equity trading days between start and end (default: today to 31 days ahead) with core "
        "open/close and extended session times converted from New York time to UTC (early closes included) "
        "and the settlement date.",
        readme="Trading days and session times (UTC)",
        input_model=CalendarIn,
        output_model=MarketCalendarDay,
        route=f"GET /v2/calendar{_T}",
        handler=reference_calendar,
        golden_test=GOLDEN,
        operations=("LegacyCalendar",),
        parity=("get_calendar",),
    ),
    spec(
        name="reference_clock",
        capability="reference",
        title="Market clock",
        description="Whether the US equity market is open now, with the next open and close (UTC).",
        readme="Market open now? Next open and close",
        input_model=NoArgs,
        output_model=MarketClock,
        route=f"GET /v2/clock{_T}",
        handler=reference_clock,
        golden_test=GOLDEN,
        operations=("LegacyClock",),
        parity=("get_clock",),
    ),
    spec(
        name="reference_corporate_action_announcements",
        capability="reference",
        title="Corporate action announcements",
        description="Announced corporate actions (dividends, mergers, spin-offs, splits) in a window of at most "
        "90 days by declaration, ex, record or payable date, optionally for one ticker or CUSIP: dates, cash per "
        "share (USD) and old/new rates.",
        readme="Announced dividends, splits, mergers",
        input_model=AnnouncementsIn,
        output_model=CorporateActionAnnouncement,
        route=f"GET /v2/corporate_actions/announcements{_T}",
        handler=reference_corporate_action_announcements,
        golden_test=GOLDEN,
        operations=("get-v2-corporate_actions-announcements",),
        parity=("get_corporate_action_announcements",),
    ),
    spec(
        name="reference_corporate_action_announcement",
        capability="reference",
        title="Corporate action announcement",
        description="One corporate action announcement by its id.",
        readme="One announcement by id",
        input_model=AnnouncementIn,
        output_model=CorporateActionAnnouncement,
        route=f"GET /v2/corporate_actions/announcements/{{id}}{_T}",
        handler=reference_corporate_action_announcement,
        golden_test=GOLDEN,
        operations=("get-v2-corporate_actions-announcements-id",),
        parity=("get_corporate_action_announcement",),
    ),
    spec(
        name="reference_corporate_actions",
        capability="reference",
        title="Corporate actions",
        description="Processed corporate actions, one row per action with action_type (16 types: dividends, "
        "splits, mergers, spin-offs, name changes, ...): process, ex, record, payable and effective dates, cash "
        "rate per share (USD), old/new ratio legs, related tickers; columns that do not apply to a type are null "
        "(not_applicable). Filter by tickers, CUSIPs, types and process-date window (default today). "
        + STORED_NOTE,
        readme="Processed corporate actions",
        input_model=CorporateActionsIn,
        output_model=CorporateAction,
        route=f"GET /v1/corporate-actions{_M}",
        handler=reference_corporate_actions,
        golden_test=GOLDEN,
        operations=("CorporateActions",),
        parity=("get_corporate_actions",),
    ),
    spec(
        name="reference_option_exchanges",
        capability="reference",
        title="Option exchanges",
        description="Option exchange codes and names (to read the exchange columns of option quotes and trades).",
        readme="Option exchange codes",
        input_model=NoArgs,
        output_model=OptionExchange,
        route=f"GET /v1beta1/options/meta/exchanges{_M}",
        handler=reference_option_exchanges,
        golden_test=GOLDEN,
        operations=("OptionMetaExchanges",),
        parity=("get_option_exchange_codes",),
    ),
)
