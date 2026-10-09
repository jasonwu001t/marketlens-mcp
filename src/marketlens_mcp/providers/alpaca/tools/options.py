"""Option market data (capability ``market``): contracts by OCC symbol, chains
by underlying. The feed of the latest/snapshot endpoints is
``providers.alpaca.options_feed`` (indicative by default: delayed; opra needs a
subscription). The historical bars and trades are OPRA's, with no feed choice:
without real-time OPRA access Alpaca serves them up to 15 minutes ago."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput
from marketlens_schema.base import AbsenceCode, Delay
from marketlens_schema.market import OptionBar, OptionQuote, OptionSnapshot, OptionTrade

from .. import mappers
from ..client import AlpacaClient
from ..convert import NEW_YORK, alpaca_symbol, integer, iso_z, parse_date, path_symbol, timeframe_to_alpaca
from .common import (
    CONTINUE_DOC,
    END_DOC,
    LOOKBACK_DOC,
    SORT_DOC,
    START_DOC,
    STORED_NOTE,
    TIMEFRAME_DOC,
    Duration,
    EquityTicker,
    Inputs,
    Instant,
    OccSymbols,
    Skips,
    TimeframeIn,
    Window,
    absence,
    check_option_filters,
    collect,
    latest_t,
    missing_note,
    option_feed,
    output,
    request_of,
    spec,
    window,
)
from .marketdata import fetch_grouped, fetch_once

GOLDEN = "tests/alpaca/test_alpaca_options.py"
SNAPSHOT_PAGE_MAX = 1000
#: Alpaca's historical option bars and trades are OPRA's. Without real-time
#: OPRA access their default end is 15 minutes ago, and a later end is refused
#: with a 403 ("OPRA agreement is not signed").
HISTORY_FEED = "opra"
OPRA_DELAY = timedelta(minutes=15)
HISTORY_DOC = (
    " The data is OPRA's; without real-time OPRA access (options_feed other than opra) Alpaca serves it up "
    "to 15 minutes ago, so a later end is moved back to then, with a note."
)
DAY_BAR_DOC = (
    "day_* are the contract's latest daily bar, dated by day_t: an earlier session than today's for a contract "
    "that has not traded today; prev_close is the close of the bar before it (prev_t)."
)
CONTRACTS_PAGE_MAX = 10_000
OI_FIELDS = ("open_interest", "open_interest_date")
OI_DOC = (
    "open_interest (contracts) is joined from Alpaca's contract list, as reference_option_contracts gives it; "
    "open_interest_date is usually the trading day before day_t (OCC publishes it the next morning)."
)
SNAPSHOT_OI_ABSENT = absence(
    "not_provided_by_source",
    "Alpaca's snapshots carry no open interest; options_chain and reference_option_contracts give it.",
)
GREEKS_NOTE = (
    "Greeks and implied volatility are Alpaca's Black-Scholes values per share (implied_volatility as an "
    "annualised fraction); they are null when Alpaca does not compute them for a contract."
)


class BarsIn(Inputs):
    occ_symbols: OccSymbols
    timeframe: TimeframeIn = Field("1d", description=TIMEFRAME_DOC)
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + " Default P30D.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class TradesIn(Inputs):
    occ_symbols: OccSymbols
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + " Default P1D.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class LatestIn(Inputs):
    occ_symbols: OccSymbols


class SnapshotsIn(Inputs):
    occ_symbols: OccSymbols
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class ChainIn(Inputs):
    underlying: EquityTicker = Field(description="Underlying stock ticker, e.g. AAPL or BRK-B.")
    option_type: Literal["call", "put"] | None = Field(None, description="Only calls or only puts.")
    strike_min: float | None = Field(None, ge=0, description="Lowest strike (USD), inclusive.")
    strike_max: float | None = Field(None, ge=0, description="Highest strike (USD), inclusive.")
    expiration: date | None = Field(None, description="Exact expiration date (YYYY-MM-DD).")
    expiration_from: date | None = Field(None, description="Earliest expiration date, inclusive.")
    expiration_to: date | None = Field(None, description="Latest expiration date, inclusive.")
    root_symbol: str | None = Field(None, max_length=10, description="OCC root, for adjusted contracts.")
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)

    @model_validator(mode="after")
    def _ranges(self) -> ChainIn:
        check_option_filters(self.strike_min, self.strike_max, self.expiration_from, self.expiration_to)
        return self


def history_window(
    ctx: ToolContext,
    api: AlpacaClient,
    args: BarsIn | TradesIn,
    default: str,
    *,
    timeframe: str | None = None,
) -> tuple[Window, Delay, datetime, list[str]]:
    """The window of options_bars and options_trades, the delay of their OPRA
    data, the instant it reflects and a note. With options_feed other than
    opra, an end later than 15 minutes ago is moved back to then (Alpaca
    refuses it), and a window that starts after then is refused here."""
    win = window(ctx, args.start, args.end, args.lookback, default, timeframe=timeframe)
    if option_feed(api.settings.options_feed)[1] is Delay.REALTIME:
        return win, Delay.REALTIME, win.end or ctx.now(), []
    latest = ctx.now() - OPRA_DELAY
    if win.start >= latest:
        raise ToolError(
            "invalid_arguments",
            f"The window starts at {iso_z(win.start)}, within the last 15 minutes, which Alpaca serves only "
            "with real-time OPRA access; ask for an earlier window.",
            hint="Set providers.alpaca.options_feed: opra in the marketlens config if your plan includes OPRA.",
        )
    if win.end is None or win.end <= latest:
        return win, Delay.DELAYED, win.end or latest, []
    note = (
        f"end {iso_z(win.end)} is later than Alpaca serves without real-time OPRA access (options_feed: "
        f"{api.settings.options_feed}), so the window ends 15 minutes ago, at {iso_z(latest)}. Set "
        "providers.alpaca.options_feed: opra if your plan includes OPRA."
    )
    return Window(win.start, latest, win.lookback), Delay.DELAYED, latest, [note]


async def options_bars(ctx: ToolContext, args: BarsIn) -> ToolOutput:
    async with AlpacaClient(ctx) as api:
        win, delay, as_of, notes = history_window(ctx, api, args, "P30D", timeframe=args.timeframe)
        skips = Skips(OptionBar.schema_name)

        def rows(symbol: str, records: list) -> list:
            return collect(
                skips,
                records,
                lambda r: mappers.option_bar(r, occ=symbol, timeframe=args.timeframe),
                lambda r: symbol,
            )

        params = {
            "symbols": args.occ_symbols,
            "timeframe": timeframe_to_alpaca(args.timeframe),
            "start": iso_z(win.start),
            "end": iso_z(win.end),
            "sort": args.sort,
        }
        page = await fetch_grouped(
            ctx,
            api,
            path="/v1beta1/options/bars",
            params=params,
            key="bars",
            mapper=rows,
            start_token=args.page_token,
        )
    return output(
        ctx,
        OptionBar,
        page.rows,
        route="GET /v1beta1/options/bars",
        operation="optionBars",
        request=request_of(args, **win.fields()),
        feed=HISTORY_FEED,
        delay=delay,
        as_of=as_of,
        page=page,
        notes=notes,
        skips=skips,
    )


async def options_trades(ctx: ToolContext, args: TradesIn) -> ToolOutput:
    async with AlpacaClient(ctx) as api:
        win, delay, as_of, notes = history_window(ctx, api, args, "P1D")
        skips = Skips(OptionTrade.schema_name)

        def rows(symbol: str, records: list) -> list:
            return collect(skips, records, lambda r: mappers.option_trade(r, occ=symbol), lambda r: symbol)

        params = {
            "symbols": args.occ_symbols,
            "start": iso_z(win.start),
            "end": iso_z(win.end),
            "sort": args.sort,
        }
        page = await fetch_grouped(
            ctx,
            api,
            path="/v1beta1/options/trades",
            params=params,
            key="trades",
            mapper=rows,
            start_token=args.page_token,
        )
    return output(
        ctx,
        OptionTrade,
        page.rows,
        route="GET /v1beta1/options/trades",
        operation="OptionTrades",
        request=request_of(args, **win.fields()),
        feed=HISTORY_FEED,
        delay=delay,
        as_of=as_of,
        page=page,
        notes=notes,
        skips=skips,
    )


async def _latest(ctx: ToolContext, args: LatestIn, kind: Literal["trades", "quotes"]) -> ToolOutput:
    model = OptionTrade if kind == "trades" else OptionQuote
    skips = Skips(model.schema_name)
    async with AlpacaClient(ctx) as api:
        feed, delay = option_feed(api.settings.options_feed)

        async def fetch() -> list:
            data = await api.get(
                "data", f"/v1beta1/options/{kind}/latest", {"symbols": args.occ_symbols, "feed": feed}
            )
            out = []
            for symbol, rec in (data.get(kind) or {}).items():
                m = (
                    (lambda r, s=symbol: mappers.option_trade(r, occ=s))
                    if kind == "trades"
                    else (lambda r, s=symbol: mappers.option_quote(r, occ=s))
                )
                out.extend(collect(skips, [rec], m, lambda r, s=symbol: s))
            return out

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        model,
        page.rows,
        route=f"GET /v1beta1/options/{kind}/latest",
        operation="OptionLatestTrades" if kind == "trades" else "OptionLatestQuotes",
        request=request_of(args, feed=feed),
        feed=feed,
        delay=delay,
        as_of=latest_t(page.rows),
        page=page,
        notes=missing_note(args.occ_symbols, page.rows, "occ_symbol"),
        skips=skips,
    )


async def options_latest_trades(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "trades")


async def options_latest_quotes(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "quotes")


async def _open_interest(
    ctx: ToolContext, api: AlpacaClient, args: ChainIn, rows: list[OptionSnapshot]
) -> tuple[list[OptionSnapshot], list[str]]:
    """Fill open_interest and open_interest_date from GET /v2/options/contracts,
    asked for exactly the underlying, type, root, expirations and strikes the
    rows span. A contract missing from that list is no_match, or withheld when
    the list stopped at the fetch limit before it was complete; a failed lookup
    leaves the chain as it is, with source_error and a note."""
    if not rows:
        return rows, []
    params = {
        "underlying_symbols": alpaca_symbol(args.underlying),
        "type": args.option_type,
        "root_symbol": args.root_symbol,
        "expiration_date_gte": min(r.expiration_date for r in rows).isoformat(),
        "expiration_date_lte": max(r.expiration_date for r in rows).isoformat(),
        "strike_price_gte": min(r.strike for r in rows),
        "strike_price_lte": max(r.strike for r in rows),
    }

    async def fetch_page(token: str | None, limit: int):
        data = await api.get(
            "trading",
            "/v2/options/contracts",
            {**params, "limit": min(limit, CONTRACTS_PAGE_MAX), "page_token": token},
        )
        out = []
        for c in data.get("option_contracts") or []:
            try:
                out.append(
                    (
                        str(c["symbol"]),
                        integer(c.get("open_interest")),
                        parse_date(c.get("open_interest_date")),
                    )
                )
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return out, data.get("next_page_token") or None

    try:
        listed = await ctx.paginate(fetch_page)
    except ToolError as e:
        failed = dict.fromkeys(OI_FIELDS, AbsenceCode.SOURCE_ERROR)
        note = f"open_interest is null: Alpaca's contract list failed ({e.code}: {e.message})"
        return [_with(r, {}, failed) for r in rows], [note]
    found = {occ: (oi, day) for occ, oi, day in listed.rows}
    unseen = AbsenceCode.NO_MATCH if listed.complete else AbsenceCode.WITHHELD
    out, unmatched = [], 0
    for r in rows:
        if r.occ_symbol not in found:
            unmatched += 1
            out.append(_with(r, {}, dict.fromkeys(OI_FIELDS, unseen)))
            continue
        values = dict(zip(OI_FIELDS, found[r.occ_symbol], strict=True))
        missing = {k: AbsenceCode.NOT_PROVIDED_BY_SOURCE for k, v in values.items() if v is None}
        out.append(_with(r, values, missing))
    if not unmatched:
        return out, []
    if listed.complete:
        return out, [
            f"open_interest is null (no_match) for {unmatched} contract(s) Alpaca's contract list did not return."
        ]
    return out, [
        f"open_interest is null (withheld) for {unmatched} contract(s): Alpaca's contract list stopped at the "
        f"fetch limit after {len(listed.rows)} contracts, before reaching them; narrow the chain by expiration, "
        "strike or option_type to fill them."
    ]


def _with(row: OptionSnapshot, values: dict, codes: dict) -> OptionSnapshot:
    absent = {**(row.absent or {}), **codes}
    return row.model_copy(update={**values, "absent": absent or None})


def _no_chain_note(args: ChainIn, rows: list, skips: Skips) -> list[str]:
    if rows or skips.labels:
        return []
    return [
        f"No data from Alpaca for: {args.underlying} (no listed contract matches the underlying and filters; "
        "check the ticker and the filters)."
    ]


def stale_day_bar_note(rows: list[OptionSnapshot]) -> list[str]:
    """A note counting the rows whose daily bar is from an earlier session than
    the newest one in the answer (summing their day_volume mixes sessions)."""
    days = [r.day_t for r in rows if r.day_t is not None]
    if not days:
        return []
    newest = max(days)
    older = sum(1 for d in days if d < newest)
    if not older:
        return []
    session = newest.astimezone(NEW_YORK).date().isoformat()
    return [
        f"day_* is each contract's latest daily bar: for {older} of {len(days)} contracts with one it is from a "
        f"session before {session} (day_t), so filter on day_t before summing day_volume across contracts."
    ]


async def _snapshots(
    ctx: ToolContext,
    api: AlpacaClient,
    path: str,
    params: dict,
    underlying: str | None,
    start_token: str | None,
    skips: Skips,
):
    async def fetch_page(token: str | None, limit: int):
        data = await api.get(
            "data", path, {**params, "limit": min(limit, SNAPSHOT_PAGE_MAX), "page_token": token}
        )
        out = []
        for symbol, rec in (data.get("snapshots") or {}).items():
            out.extend(
                collect(
                    skips,
                    [rec],
                    lambda r, s=symbol: mappers.option_snapshot(r, occ=s, underlying=underlying),
                    lambda r, s=symbol: s,
                )
            )
        return out, data.get("next_page_token") or None

    return await ctx.paginate(fetch_page, start_token=start_token)


async def options_snapshots(ctx: ToolContext, args: SnapshotsIn) -> ToolOutput:
    skips = Skips(OptionSnapshot.schema_name)
    async with AlpacaClient(ctx) as api:
        feed, delay = option_feed(api.settings.options_feed)
        page = await _snapshots(
            ctx,
            api,
            "/v1beta1/options/snapshots",
            {"symbols": args.occ_symbols, "feed": feed},
            None,
            args.page_token,
            skips,
        )
    return output(
        ctx,
        OptionSnapshot,
        page.rows,
        route="GET /v1beta1/options/snapshots",
        operation="OptionSnapshots",
        request=request_of(args, feed=feed),
        feed=feed,
        delay=delay,
        as_of=latest_t(page.rows),
        page=page,
        absent={"open_interest": SNAPSHOT_OI_ABSENT, "open_interest_date": SNAPSHOT_OI_ABSENT},
        notes=[
            GREEKS_NOTE,
            *missing_note(args.occ_symbols, page.rows, "occ_symbol"),
            *stale_day_bar_note(page.rows),
        ],
        skips=skips,
    )


async def options_chain(ctx: ToolContext, args: ChainIn) -> ToolOutput:
    skips = Skips(OptionSnapshot.schema_name)
    async with AlpacaClient(ctx) as api:
        feed, delay = option_feed(api.settings.options_feed)
        params = {
            "feed": feed,
            "type": args.option_type,
            "strike_price_gte": args.strike_min,
            "strike_price_lte": args.strike_max,
            "expiration_date": args.expiration.isoformat() if args.expiration else None,
            "expiration_date_gte": args.expiration_from.isoformat() if args.expiration_from else None,
            "expiration_date_lte": args.expiration_to.isoformat() if args.expiration_to else None,
            "root_symbol": args.root_symbol,
        }
        page = await _snapshots(
            ctx,
            api,
            f"/v1beta1/options/snapshots/{path_symbol(args.underlying)}",
            params,
            args.underlying,
            args.page_token,
            skips,
        )
        rows, oi_notes = await _open_interest(ctx, api, args, page.rows)
    return output(
        ctx,
        OptionSnapshot,
        rows,
        route="GET /v1beta1/options/snapshots/{underlying_symbol}",
        operation="OptionChain",
        request=request_of(args, feed=feed),
        feed=feed,
        delay=delay,
        # the open-interest lookup reads the trading API of this account (none for an empty chain)
        environment=ctx.portfolio_environment if rows else None,
        as_of=latest_t(rows),
        page=page,
        notes=[GREEKS_NOTE, *_no_chain_note(args, rows, skips), *stale_day_bar_note(rows), *oi_notes],
        skips=skips,
    )


_SIZE = " " + STORED_NOTE
_R = " (market data API)"
SPECS = (
    spec(
        name="options_bars",
        capability="market",
        title="Option bars",
        description="Historical OHLCV bars for option contracts by OCC symbol (premium per share in USD, volume "
        "in contracts). timeframe default 1d; window by start/end or lookback (default P30D)."
        + HISTORY_DOC
        + _SIZE,
        readme="OHLCV bars for option contracts",
        input_model=BarsIn,
        output_model=OptionBar,
        route=f"GET /v1beta1/options/bars{_R}",
        handler=options_bars,
        golden_test=GOLDEN,
        operations=("optionBars",),
        parity=("get_option_bars",),
    ),
    spec(
        name="options_trades",
        capability="market",
        title="Option trades",
        description="Historical trades for option contracts by OCC symbol (premium per share in USD, size in "
        "contracts). Window by start/end or lookback (default P1D)." + HISTORY_DOC + _SIZE,
        readme="Historical option trades",
        input_model=TradesIn,
        output_model=OptionTrade,
        route=f"GET /v1beta1/options/trades{_R}",
        handler=options_trades,
        golden_test=GOLDEN,
        operations=("OptionTrades",),
        parity=("get_option_trades",),
    ),
    spec(
        name="options_latest_trades",
        capability="market",
        title="Latest option trades",
        description="The latest trade per option contract (OCC symbols), from the configured options_feed.",
        readme="Latest trade per contract",
        input_model=LatestIn,
        output_model=OptionTrade,
        route=f"GET /v1beta1/options/trades/latest{_R}",
        handler=options_latest_trades,
        golden_test=GOLDEN,
        operations=("OptionLatestTrades",),
        parity=("get_option_latest_trade",),
    ),
    spec(
        name="options_latest_quotes",
        capability="market",
        title="Latest option quotes",
        description="The latest best bid and ask per option contract (sizes in contracts); a side with no "
        "quote is null (no_data).",
        readme="Latest quote per contract",
        input_model=LatestIn,
        output_model=OptionQuote,
        route=f"GET /v1beta1/options/quotes/latest{_R}",
        handler=options_latest_quotes,
        golden_test=GOLDEN,
        operations=("OptionLatestQuotes",),
        parity=("get_option_latest_quote",),
    ),
    spec(
        name="options_snapshots",
        capability="market",
        title="Option snapshots",
        description="Latest state per option contract with greeks (delta, gamma, theta, vega, rho; Alpaca's "
        "Black-Scholes, per share) and implied volatility (annualised fraction): last trade, best bid/ask, "
        "the latest daily bar and the close before it; expiration, strike and type parsed from the OCC symbol. "
        + DAY_BAR_DOC
        + _SIZE,
        readme="Contract snapshots with greeks and IV",
        input_model=SnapshotsIn,
        output_model=OptionSnapshot,
        route=f"GET /v1beta1/options/snapshots{_R}",
        handler=options_snapshots,
        golden_test=GOLDEN,
        operations=("OptionSnapshots",),
        parity=("get_option_snapshot",),
    ),
    spec(
        name="options_chain",
        capability="market",
        title="Option chain",
        description="The option chain of one underlying: a snapshot row per contract with greeks and implied "
        "volatility, filtered by type, strike range (USD) and expiration (exact or from/to). Chains are large; "
        "filter by expiration and strike. " + DAY_BAR_DOC + " " + OI_DOC + _SIZE,
        readme="Chain snapshots for an underlying",
        input_model=ChainIn,
        output_model=OptionSnapshot,
        route=f"GET /v1beta1/options/snapshots/{{underlying_symbol}}{_R}",
        handler=options_chain,
        golden_test=GOLDEN,
        operations=("OptionChain",),
        parity=("get_option_chain",),
    ),
)
