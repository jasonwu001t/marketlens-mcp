"""Stock market data and screeners (capability ``market``)."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from marketlens_mcp.plugin_api import ToolContext, ToolOutput
from marketlens_schema.base import Delay
from marketlens_schema.market import Bar, MostActive, Mover, Quote, Snapshot, Trade

from .. import mappers
from ..client import AlpacaClient
from ..convert import alpaca_symbol, iso_z, parse_ts, timeframe_to_alpaca, to_ticker
from .common import (
    CONTINUE_DOC,
    END_DOC,
    LOOKBACK_DOC,
    SORT_DOC,
    START_DOC,
    STORED_NOTE,
    TIMEFRAME_DOC,
    Duration,
    EquityTickers,
    Inputs,
    Instant,
    Skips,
    TimeframeIn,
    absence,
    collect,
    latest_t,
    missing_note,
    output,
    request_of,
    spec,
    stock_feed,
    window,
)
from .marketdata import fetch_grouped, fetch_once

GOLDEN = "tests/alpaca/test_alpaca_stocks.py"
EQ = "us_equity"
NO_TAKER = "Stock trades carry no aggressor side."
LOT_NOTE = (
    "Quote sizes are shares; Alpaca reported round lots before 2025-11-03, which are converted at 100 shares."
)
#: Alpaca's movers screener ranks at most this many gainers and losers.
MOVERS_MAX = 50
#: Nasdaq's fifth letter of a five-letter symbol for warrants, rights and units (NRSNW, CHARR, CCAQU).
FIFTH_LETTERS = frozenset("WRU")
#: Suffixes after the root for warrants, rights and units (Alpaca's AAC.WS and BCAT.RT, our AAC-WS, BCAT-RT).
SUFFIXES = frozenset({"WS", "RT", "U"})


class BarsIn(Inputs):
    tickers: EquityTickers
    timeframe: TimeframeIn = Field("1d", description=TIMEFRAME_DOC)
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(
        None, description=LOOKBACK_DOC + " Default P5D intraday, P1Y daily or longer."
    )
    adjustment: Literal["raw", "split", "dividend", "all"] = Field(
        "all",
        description="Price adjustment: all (split and dividend adjusted, right for returns), split, dividend, "
        "or raw (as traded).",
    )
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class TicksIn(Inputs):
    tickers: EquityTickers
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + " Default PT20M.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class LatestIn(Inputs):
    tickers: EquityTickers


class MostActiveIn(Inputs):
    by: Literal["volume", "trades"] = Field("volume", description="Rank by share volume or by trade count.")
    top: int = Field(10, ge=1, le=100, description="How many tickers (1-100).")


class MoversIn(Inputs):
    market_type: Literal["stocks", "crypto"] = Field("stocks", description="Screen stocks or crypto.")
    top: int = Field(10, ge=1, le=50, description="How many gainers and how many losers (1-50).")
    min_price: float | None = Field(
        None,
        ge=0,
        description="Drop movers priced below this (USD), e.g. 1 to leave out sub-dollar listings.",
    )
    exclude_warrants_rights_units: bool = Field(
        False,
        description="Stocks only: drop warrants, rights and units, judged by symbol alone: five letters ending "
        "in W, R or U (Nasdaq's fifth letter: NRSNW, CHARR, CCAQU) or a WS, RT or U suffix (AAC-WS, AAC-WS-A, "
        "BCAT-RT, XYZ-U).",
    )

    @model_validator(mode="after")
    def _stocks_only(self) -> MoversIn:
        if self.exclude_warrants_rights_units and self.market_type != "stocks":
            raise ValueError("exclude_warrants_rights_units applies to stocks only")
        return self


def _intraday(tf: str) -> bool:
    return tf.endswith("min") or tf.endswith("h")


async def market_bars(ctx: ToolContext, args: BarsIn) -> ToolOutput:
    async with AlpacaClient(ctx) as api:
        feed, delay = stock_feed(api.settings.stock_feed, historical=True)
        win = window(
            ctx,
            args.start,
            args.end,
            args.lookback,
            "P5D" if _intraday(args.timeframe) else "P1Y",
            timeframe=args.timeframe,
        )
        skips = Skips(Bar.schema_name)

        def rows(symbol: str, records: list) -> list:
            tk = to_ticker(symbol, EQ)
            return collect(
                skips,
                records,
                lambda r: mappers.bar(r, ticker=tk, asset_class=EQ, timeframe=args.timeframe),
                lambda r: symbol,
            )

        params = {
            "symbols": [alpaca_symbol(t) for t in args.tickers],
            "timeframe": timeframe_to_alpaca(args.timeframe),
            "start": iso_z(win.start),
            "end": iso_z(win.end),
            "adjustment": args.adjustment,
            "feed": feed,
            "sort": args.sort,
        }
        page = await fetch_grouped(
            ctx,
            api,
            path="/v2/stocks/bars",
            params=params,
            key="bars",
            mapper=rows,
            start_token=args.page_token,
        )
    return output(
        ctx,
        Bar,
        page.rows,
        route="GET /v2/stocks/bars",
        operation="StockBars",
        request=request_of(args, **win.fields(), feed=feed),
        feed=feed,
        delay=delay,
        as_of=win.end or ctx.now(),
        page=page,
        skips=skips,
    )


async def _ticks(ctx: ToolContext, args: TicksIn, kind: Literal["quotes", "trades"]) -> ToolOutput:
    model = Quote if kind == "quotes" else Trade
    async with AlpacaClient(ctx) as api:
        feed, delay = stock_feed(api.settings.stock_feed, historical=True)
        win = window(ctx, args.start, args.end, args.lookback, "PT20M")
        skips = Skips(model.schema_name)
        dropped = 0

        def rows(symbol: str, records: list) -> list:
            nonlocal dropped
            tk = to_ticker(symbol, EQ)
            if kind == "quotes":
                return collect(
                    skips, records, lambda r: mappers.quote(r, ticker=tk, asset_class=EQ), lambda r: symbol
                )
            kept, n = mappers.valid_trades(records)
            dropped += n
            return collect(
                skips,
                kept,
                lambda r: mappers.trade(r, ticker=tk, asset_class=EQ, skip=("taker_side",)),
                lambda r: symbol,
            )

        params = {
            "symbols": [alpaca_symbol(t) for t in args.tickers],
            "start": iso_z(win.start),
            "end": iso_z(win.end),
            "feed": feed,
            "sort": args.sort,
        }
        page = await fetch_grouped(
            ctx,
            api,
            path=f"/v2/stocks/{kind}",
            params=params,
            key=kind,
            mapper=rows,
            start_token=args.page_token,
        )
    notes = [LOT_NOTE] if kind == "quotes" else []
    if dropped:
        notes.append(f"Dropped {dropped} trade(s) Alpaca marks canceled or incorrect.")
    absent = {} if kind == "quotes" else {"taker_side": absence("not_applicable", NO_TAKER)}
    return output(
        ctx,
        model,
        page.rows,
        route=f"GET /v2/stocks/{kind}",
        operation="StockQuotes" if kind == "quotes" else "StockTrades",
        request=request_of(args, **win.fields(), feed=feed),
        feed=feed,
        delay=delay,
        as_of=win.end or ctx.now(),
        page=page,
        absent=absent,
        notes=notes,
        skips=skips,
    )


async def market_quotes(ctx: ToolContext, args: TicksIn) -> ToolOutput:
    return await _ticks(ctx, args, "quotes")


async def market_trades(ctx: ToolContext, args: TicksIn) -> ToolOutput:
    return await _ticks(ctx, args, "trades")


async def _latest(ctx: ToolContext, args: LatestIn, kind: Literal["bars", "quotes", "trades"]) -> ToolOutput:
    model = {"bars": Bar, "quotes": Quote, "trades": Trade}[kind]
    operation = {"bars": "StockLatestBars", "quotes": "StockLatestQuotes", "trades": "StockLatestTrades"}[
        kind
    ]
    skips = Skips(model.schema_name)
    async with AlpacaClient(ctx) as api:
        feed, delay = stock_feed(api.settings.stock_feed, historical=False)

        async def fetch() -> list:
            data = await api.get(
                "data",
                f"/v2/stocks/{kind}/latest",
                {"symbols": [alpaca_symbol(t) for t in args.tickers], "feed": feed},
            )
            out = []
            for symbol, rec in (data.get(kind) or {}).items():
                tk = to_ticker(symbol, EQ)
                if kind == "bars":
                    m = lambda r, tk=tk: mappers.bar(r, ticker=tk, asset_class=EQ, timeframe="1min")  # noqa: E731
                elif kind == "quotes":
                    m = lambda r, tk=tk: mappers.quote(r, ticker=tk, asset_class=EQ)  # noqa: E731
                else:
                    m = lambda r, tk=tk: mappers.trade(r, ticker=tk, asset_class=EQ, skip=("taker_side",))  # noqa: E731
                out.extend(collect(skips, [rec], m, lambda r, s=symbol: s))
            return out

        page = await fetch_once(ctx, fetch)
    absent = {"taker_side": absence("not_applicable", NO_TAKER)} if kind == "trades" else {}
    return output(
        ctx,
        model,
        page.rows,
        route=f"GET /v2/stocks/{kind}/latest",
        operation=operation,
        request=request_of(args, feed=feed),
        feed=feed,
        delay=delay,
        as_of=latest_t(page.rows),
        page=page,
        notes=missing_note(args.tickers, page.rows),
        absent=absent,
        skips=skips,
    )


async def market_latest_bars(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "bars")


async def market_latest_quotes(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "quotes")


async def market_latest_trades(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "trades")


async def market_snapshots(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    skips = Skips(Snapshot.schema_name)
    async with AlpacaClient(ctx) as api:
        feed, delay = stock_feed(api.settings.stock_feed, historical=False)

        async def fetch() -> list:
            data = await api.get(
                "data",
                "/v2/stocks/snapshots",
                {"symbols": [alpaca_symbol(t) for t in args.tickers], "feed": feed},
            )
            out = []
            for symbol, rec in data.items():
                tk = to_ticker(symbol, EQ)
                out.extend(
                    collect(
                        skips,
                        [rec],
                        lambda r, tk=tk: mappers.snapshot(r, ticker=tk, asset_class=EQ),
                        lambda r, s=symbol: s,
                    )
                )
            return out

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        Snapshot,
        page.rows,
        route="GET /v2/stocks/snapshots",
        operation="StockSnapshots",
        request=request_of(args, feed=feed),
        feed=feed,
        delay=delay,
        as_of=latest_t(page.rows),
        page=page,
        notes=missing_note(args.tickers, page.rows),
        skips=skips,
    )


async def market_most_active(ctx: ToolContext, args: MostActiveIn) -> ToolOutput:
    skips = Skips(MostActive.schema_name)
    as_of = None
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            nonlocal as_of
            data = await api.get(
                "data", "/v1beta1/screener/stocks/most-actives", {"by": args.by, "top": args.top}
            )
            as_of = parse_ts(data.get("last_updated"))
            ranked = list(enumerate(data.get("most_actives") or [], 1))
            return collect(
                skips,
                ranked,
                lambda p: mappers.most_active(p[1], rank=p[0], ranked_by=args.by, as_of=as_of),
                lambda p: str(p[1].get("symbol")),
            )

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        MostActive,
        page.rows,
        route="GET /v1beta1/screener/stocks/most-actives",
        operation="MostActives",
        request=request_of(args),
        delay=Delay.UNKNOWN,
        as_of=as_of,
        page=page,
        skips=skips,
    )


def warrant_right_or_unit(ticker: str) -> bool:
    """By symbol alone: a five-letter symbol whose fifth letter is W, R or U, or
    a root followed by a WS, RT or U suffix. Four-letter symbols and other fifth
    letters (Z, miscellaneous) are kept."""
    root, _, suffix = ticker.partition("-")
    if suffix:
        return suffix.split("-")[0] in SUFFIXES
    return len(root) == 5 and root.isalpha() and root[-1] in FIFTH_LETTERS


def _screen(rows: list[Mover], args: MoversIn, reasons: Counter[tuple[str, str]]) -> list[Mover]:
    """The movers that pass the filters, in Alpaca's order and ranked from 1
    among themselves; each dropped one is counted under its first reason, as
    (one, many)."""
    kept: list[Mover] = []
    for row in rows:
        if args.min_price is not None and row.price < args.min_price:
            reasons[(f"priced below min_price {args.min_price:g}",) * 2] += 1
        elif args.exclude_warrants_rights_units and warrant_right_or_unit(row.ticker):
            reasons[("warrant, right or unit by symbol", "warrants, rights or units by symbol")] += 1
        else:
            kept.append(row.model_copy(update={"rank": len(kept) + 1}))
    return kept


def _movers(n: int, direction: str) -> str:
    return direction if n == 1 else f"{direction}s"


def _screen_notes(
    top: int, counts: dict[str, tuple[int, int, int]], reasons: Counter[tuple[str, str]]
) -> list[str]:
    """counts: direction -> (ranked by Alpaca, dropped, kept). The filters are
    named only for a direction they dropped movers from, and for its shortfall
    only when the movers they screened (kept + dropped, after any skipped
    record) numbered top or more; a direction Alpaca ranked fewer than top of is
    short before any filter, and said so apart."""
    notes = []
    if reasons:
        dropped = " and ".join(f"{d} of {n} {_movers(n, k)}" for k, (n, d, _) in counts.items() if d)
        why = ", ".join(f"{n} {one if n == 1 else many}" for (one, many), n in reasons.items())
        notes.append(f"Filters dropped {dropped} Alpaca ranked: {why}.")
    short = [
        f"{kept} of {top} {_movers(top, k)}" for k, (_, d, kept) in counts.items() if kept < top <= kept + d
    ]
    if short:
        notes.append(f"Only {' and '.join(short)} passed the filters.")
    few = [f"{n} {_movers(n, k)}" if n else f"no {k}s" for k, (n, _, _) in counts.items() if n < top]
    if few:
        notes.append(f"Alpaca ranked {' and '.join(few)}, fewer than the {top} asked for.")
    return notes


async def market_movers(ctx: ToolContext, args: MoversIn) -> ToolOutput:
    skips = Skips(Mover.schema_name)
    as_of = None
    screened = args.min_price is not None or args.exclude_warrants_rights_units
    counts: dict[str, tuple[int, int, int]] = {}
    reasons: Counter[tuple[str, str]] = Counter()
    async with AlpacaClient(ctx) as api:

        async def fetch() -> list:
            nonlocal as_of
            top = MOVERS_MAX if screened else args.top
            data = await api.get("data", f"/v1beta1/screener/{args.market_type}/movers", {"top": top})
            as_of = parse_ts(data.get("last_updated"))
            out = []
            for direction, key in (("gainer", "gainers"), ("loser", "losers")):
                ranked = list(enumerate(data.get(key) or [], 1))
                rows = collect(
                    skips,
                    ranked,
                    lambda p, d=direction: mappers.mover(
                        p[1], rank=p[0], market_type=args.market_type, direction=d, as_of=as_of
                    ),
                    lambda p: str(p[1].get("symbol")),
                )
                if screened:
                    kept = _screen(rows, args, reasons)
                    counts[direction] = (len(ranked), len(rows) - len(kept), len(kept))
                    rows = kept[: args.top]
                out += rows
            return out

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        Mover,
        page.rows,
        route="GET /v1beta1/screener/{market_type}/movers",
        operation="Movers",
        request=request_of(args),
        delay=Delay.UNKNOWN,
        as_of=as_of,
        page=page,
        notes=_screen_notes(args.top, counts, reasons),
        skips=skips,
    )


_SIZE = " " + STORED_NOTE
SPECS = (
    spec(
        name="market_bars",
        capability="market",
        title="Stock bars",
        description="Historical OHLCV bars for US stocks (prices in USD, volume in shares), one row per ticker "
        "and bar start (UTC). timeframe Nmin/Nh/1d/1w/Nmo (default 1d); window by start/end or lookback "
        "(default P5D intraday, P1Y daily); adjustment default all (split and dividend adjusted, what return "
        "analytics need; raw = as traded). The feed is the configured stock_feed." + _SIZE,
        readme="OHLCV bars for stocks",
        input_model=BarsIn,
        output_model=Bar,
        route="GET /v2/stocks/bars (market data API)",
        handler=market_bars,
        golden_test=GOLDEN,
        operations=("StockBars",),
        parity=("get_stock_bars",),
    ),
    spec(
        name="market_quotes",
        capability="market",
        title="Stock quotes",
        description="Historical best bid and ask quotes for US stocks (prices USD, sizes in shares), one row "
        "per quote (Alpaca's round lots before 2025-11-03 are converted at 100 shares). A side with no active quote is null (no_data), never 0. Window by start/end or lookback "
        "(default PT20M); quotes are dense, keep windows short." + _SIZE,
        readme="Historical NBBO quotes",
        input_model=TicksIn,
        output_model=Quote,
        route="GET /v2/stocks/quotes (market data API)",
        handler=market_quotes,
        golden_test=GOLDEN,
        operations=("StockQuotes",),
        parity=("get_stock_quotes",),
    ),
    spec(
        name="market_trades",
        capability="market",
        title="Stock trades",
        description="Historical trades for US stocks (price USD, size shares), one row per trade; trades "
        "Alpaca marks canceled or incorrect are dropped and counted in notes. Window by start/end or lookback "
        "(default PT20M)." + _SIZE,
        readme="Historical trades",
        input_model=TicksIn,
        output_model=Trade,
        route="GET /v2/stocks/trades (market data API)",
        handler=market_trades,
        golden_test=GOLDEN,
        operations=("StockTrades",),
        parity=("get_stock_trades",),
    ),
    spec(
        name="market_latest_bars",
        capability="market",
        title="Latest stock bars",
        description="The latest one-minute bar for each stock ticker (prices USD, volume shares).",
        readme="Latest minute bar per ticker",
        input_model=LatestIn,
        output_model=Bar,
        route="GET /v2/stocks/bars/latest (market data API)",
        handler=market_latest_bars,
        golden_test=GOLDEN,
        operations=("StockLatestBars",),
        parity=("get_stock_latest_bar",),
    ),
    spec(
        name="market_latest_quotes",
        capability="market",
        title="Latest stock quotes",
        description="The latest best bid and ask for each stock ticker (prices USD, sizes shares); a side "
        "with no active quote is null (no_data).",
        readme="Latest NBBO quote per ticker",
        input_model=LatestIn,
        output_model=Quote,
        route="GET /v2/stocks/quotes/latest (market data API)",
        handler=market_latest_quotes,
        golden_test=GOLDEN,
        operations=("StockLatestQuotes",),
        parity=("get_stock_latest_quote",),
    ),
    spec(
        name="market_latest_trades",
        capability="market",
        title="Latest stock trades",
        description="The latest trade for each stock ticker (price USD, size shares).",
        readme="Latest trade per ticker",
        input_model=LatestIn,
        output_model=Trade,
        route="GET /v2/stocks/trades/latest (market data API)",
        handler=market_latest_trades,
        golden_test=GOLDEN,
        operations=("StockLatestTrades",),
        parity=("get_stock_latest_trade",),
    ),
    spec(
        name="market_snapshots",
        capability="market",
        title="Stock snapshots",
        description="Latest state per stock ticker: last trade, best bid/ask, latest minute close, today's "
        "OHLCV and VWAP, previous close, and change / change_pct (fraction) derived from the last trade and "
        "the previous close. Tickers Alpaca has nothing for are listed in notes.",
        readme="Last trade, quote, day bar, change",
        input_model=LatestIn,
        output_model=Snapshot,
        route="GET /v2/stocks/snapshots (market data API)",
        handler=market_snapshots,
        golden_test=GOLDEN,
        operations=("StockSnapshots",),
        parity=("get_stock_snapshot",),
    ),
    spec(
        name="market_most_active",
        capability="market",
        title="Most active stocks",
        description="Today's most active US stocks ranked by share volume or trade count (cumulative for "
        "the current trading day); as_of is the screener's last update.",
        readme="Most active stocks today",
        input_model=MostActiveIn,
        output_model=MostActive,
        route="GET /v1beta1/screener/stocks/most-actives (market data API)",
        handler=market_most_active,
        golden_test=GOLDEN,
        operations=("MostActives",),
        parity=("get_most_active_stocks",),
    ),
    spec(
        name="market_movers",
        capability="market",
        title="Top movers",
        description="Today's top gainers and losers for stocks or crypto: price, change (USD) and "
        "percent_change as a fraction (0.05 = 5 %), ranked within each direction. Alpaca's lists include "
        "warrants, rights, units and sub-penny listings: min_price and exclude_warrants_rights_units (stocks, "
        "by symbol) screen Alpaca's top 50 instead, rank counts the movers kept, and notes say how many were "
        "dropped and why.",
        readme="Top gainers and losers",
        input_model=MoversIn,
        output_model=Mover,
        route="GET /v1beta1/screener/{market_type}/movers (market data API)",
        handler=market_movers,
        golden_test=GOLDEN,
        operations=("Movers",),
        parity=("get_market_movers",),
    ),
)
