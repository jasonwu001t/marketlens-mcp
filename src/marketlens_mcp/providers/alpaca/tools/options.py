"""Option market data (capability ``market``): contracts by OCC symbol, chains
by underlying. The feed of the latest/snapshot endpoints is
``providers.alpaca.options_feed`` (indicative by default: delayed; opra needs a
subscription)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput
from marketlens_schema.market import OptionBar, OptionQuote, OptionSnapshot, OptionTrade

from .. import mappers
from ..client import AlpacaClient
from ..convert import iso_z, path_symbol, timeframe_to_alpaca
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


async def options_bars(ctx: ToolContext, args: BarsIn) -> ToolOutput:
    async with AlpacaClient(ctx) as api:
        win = window(ctx, args.start, args.end, args.lookback, "P30D", timeframe=args.timeframe)
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
        as_of=win.end or ctx.now(),
        page=page,
        skips=skips,
    )


async def options_trades(ctx: ToolContext, args: TradesIn) -> ToolOutput:
    async with AlpacaClient(ctx) as api:
        win = window(ctx, args.start, args.end, args.lookback, "P1D")
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
        as_of=win.end or ctx.now(),
        page=page,
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
        notes=[GREEKS_NOTE, *missing_note(args.occ_symbols, page.rows, "occ_symbol")],
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
    return output(
        ctx,
        OptionSnapshot,
        page.rows,
        route="GET /v1beta1/options/snapshots/{underlying_symbol}",
        operation="OptionChain",
        request=request_of(args, feed=feed),
        feed=feed,
        delay=delay,
        as_of=latest_t(page.rows),
        page=page,
        notes=[GREEKS_NOTE],
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
        "in contracts). timeframe default 1d; window by start/end or lookback (default P30D)." + _SIZE,
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
        "contracts). Window by start/end or lookback (default P1D)." + _SIZE,
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
        "today's bar, previous close; expiration, strike and type parsed from the OCC symbol." + _SIZE,
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
        "filter by expiration and strike." + _SIZE,
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
