"""Crypto market data (capability ``market``). Pairs are BASE/QUOTE tickers;
the data location is ``providers.alpaca.crypto_location`` (default us)."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from marketlens_mcp.plugin_api import ToolContext, ToolOutput
from marketlens_schema.base import Delay
from marketlens_schema.market import Bar, OrderBookLevel, Quote, Snapshot, Trade

from .. import mappers
from ..client import AlpacaClient
from ..convert import iso_z, timeframe_to_alpaca, to_ticker
from .bars import LAST_BAR_DOC, cap_last_bars, first_bar_note
from .common import (
    BARS_LOOKBACK_DOC,
    CONTINUE_DOC,
    END_DOC,
    LOOKBACK_DOC,
    SKIP_SAMPLE,
    SORT_DOC,
    START_DOC,
    STORED_NOTE,
    TIMEFRAME_DOC,
    CryptoPairs,
    Duration,
    Inputs,
    Instant,
    Skips,
    TimeframeIn,
    absence,
    collect,
    day_close_basis_note,
    latest_t,
    missing_in_window_note,
    missing_note,
    named,
    output,
    request_of,
    spec,
    stale_rows_note,
    window,
)
from .marketdata import HISTORY_PAGE_MAX, fetch_grouped, fetch_once

GOLDEN = "tests/alpaca/test_alpaca_crypto.py"
CR = "crypto"
BASE = "/v1beta3/crypto/{loc}"
QUOTE_SKIP = ("bid_exchange", "ask_exchange", "conditions", "tape")
TRADE_SKIP = ("exchange", "conditions", "tape")
NO_VENUE = "Alpaca crypto {what} carry no exchange, conditions or tape."
STALE_TRADE = (
    "their last trade is older than the current day bar, so last_price is stale and the change is the day "
    "bar's close against the previous one"
)
NO_TRADES_DOC = (
    "A bar with trade_count 0 had no trades: Alpaca fills it from quotes, and notes name the pairs."
)


class BarsIn(Inputs):
    tickers: CryptoPairs
    timeframe: TimeframeIn = Field("1h", description=TIMEFRAME_DOC)
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + BARS_LOOKBACK_DOC + " Default P1D.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class TicksIn(Inputs):
    tickers: CryptoPairs
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + " Default PT15M.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class TradesIn(TicksIn):
    limit: int | None = Field(
        None,
        ge=1,
        le=HISTORY_PAGE_MAX,
        description="At most this many trades per pair in the window, the first in sort order: sort desc with "
        "limit 100 gives each pair's latest 100 (one request per pair; not with page_token).",
    )

    @model_validator(mode="after")
    def _one_way(self) -> TradesIn:
        if self.limit is not None and self.page_token is not None:
            raise ValueError("limit takes each pair's first trades; page_token continues a fetch without it")
        return self


class LatestIn(Inputs):
    tickers: CryptoPairs


class OrderbooksIn(Inputs):
    tickers: CryptoPairs
    depth: int = Field(20, ge=1, le=1000, description="Price levels per side, best first (1-1000).")


def _absent(kind: str) -> dict:
    if kind == "quotes":
        return {f: absence("not_applicable", NO_VENUE.format(what="quotes")) for f in QUOTE_SKIP}
    if kind == "trades":
        return {f: absence("not_applicable", NO_VENUE.format(what="trades")) for f in TRADE_SKIP}
    return {}


def quote_currency(pair: str) -> str:
    """Prices of a pair are in its quote leg (ETH/BTC is priced in BTC)."""
    return pair.partition("/")[2] or "USD"


def no_trades_note(rows: list[Bar]) -> list[str]:
    """Alpaca still sends a bar for an interval without trades: trade_count and
    volume are 0 and its prices and vwap come from quotes. Name the pairs."""
    pairs = Counter(r.ticker for r in rows if r.trade_count == 0)
    if not pairs:
        return []
    named = ", ".join(p if n == 1 else f"{p} ({n})" for p, n in pairs.most_common(SKIP_SAMPLE))
    if len(pairs) > SKIP_SAMPLE:
        named += f" and {len(pairs) - SKIP_SAMPLE} more pair(s)"
    return [
        f"{pairs.total()} of {len(rows)} bar(s) had no trades (trade_count 0, volume 0): {named}. Alpaca built "
        "them from quotes, so their prices and vwap are not trade prices; trade_count > 0 keeps the traded bars."
    ]


def _mapper(kind: str, ticker: str, timeframe: str = "1min"):
    ccy = quote_currency(ticker)
    if kind == "bars":
        return lambda r: mappers.bar(r, ticker=ticker, asset_class=CR, timeframe=timeframe, currency=ccy)
    if kind == "quotes":
        return lambda r: mappers.quote(r, ticker=ticker, asset_class=CR, currency=ccy, skip=QUOTE_SKIP)
    return lambda r: mappers.trade(r, ticker=ticker, asset_class=CR, currency=ccy, skip=TRADE_SKIP)


async def _history(
    ctx: ToolContext, args: BarsIn | TicksIn | TradesIn, kind: Literal["bars", "quotes", "trades"]
) -> ToolOutput:
    model = {"bars": Bar, "quotes": Quote, "trades": Trade}[kind]
    operation = {"bars": "CryptoBars", "quotes": "CryptoQuotes", "trades": "CryptoTrades"}[kind]
    timeframe = getattr(args, "timeframe", None)
    async with AlpacaClient(ctx) as api:
        loc = api.settings.crypto_location
        win = window(
            ctx,
            args.start,
            args.end,
            args.lookback,
            "P1D" if kind == "bars" else "PT15M",
            timeframe=timeframe,
        )
        skips = Skips(model.schema_name)

        def rows(symbol: str, records: list) -> list:
            return collect(
                skips, records, _mapper(kind, to_ticker(symbol, CR), timeframe or "1min"), lambda r: symbol
            )

        params = {
            "symbols": args.tickers,
            "timeframe": timeframe_to_alpaca(timeframe) if timeframe else None,
            "start": iso_z(win.start),
            "end": iso_z(win.end),
            "sort": args.sort,
        }
        limit = getattr(args, "limit", None)
        capped: list[str] = []
        if limit is None:
            page = await fetch_grouped(
                ctx,
                api,
                path=f"{BASE.format(loc=loc)}/{kind}",
                params=params,
                key=kind,
                mapper=rows,
                start_token=args.page_token,
            )
            kept = page.rows
            if kind == "bars":
                kept, capped = await cap_last_bars(
                    ctx,
                    api,
                    win,
                    kept,
                    timeframe,
                    path=f"{BASE.format(loc=loc)}/bars",
                    params=params,
                    key="ticker",
                    unit=" pair(s)",
                )
        else:

            async def first_of_each() -> list:
                """One request per pair: Alpaca's limit counts across all the pairs of a request."""
                out = []
                for pair in args.tickers:
                    data = await api.get(
                        "data",
                        f"{BASE.format(loc=loc)}/{kind}",
                        {**params, "symbols": [pair], "limit": limit},
                    )
                    for symbol, records in (data.get(kind) or {}).items():
                        out.extend(rows(symbol, list(records or [])[:limit]))
                return out

            page = await fetch_once(ctx, first_of_each)
            kept = page.rows
    return output(
        ctx,
        model,
        kept,
        route=f"GET {BASE}/{kind}",
        operation=operation,
        request=request_of(args, **win.fields(), location=loc),
        feed=loc,
        delay=Delay.REALTIME,
        as_of=win.as_of,
        page=page,
        absent=_absent(kind),
        notes=[
            *win.notes,
            *(first_bar_note(win, timeframe) if kind == "bars" else []),
            *missing_in_window_note(args.tickers, page, args.page_token),
            *(no_trades_note(kept) if kind == "bars" else []),
            *capped,
        ],
        skips=skips,
    )


async def crypto_bars(ctx: ToolContext, args: BarsIn) -> ToolOutput:
    return await _history(ctx, args, "bars")


async def crypto_quotes(ctx: ToolContext, args: TicksIn) -> ToolOutput:
    return await _history(ctx, args, "quotes")


async def crypto_trades(ctx: ToolContext, args: TradesIn) -> ToolOutput:
    return await _history(ctx, args, "trades")


async def _latest(ctx: ToolContext, args: LatestIn, kind: Literal["bars", "quotes", "trades"]) -> ToolOutput:
    model = {"bars": Bar, "quotes": Quote, "trades": Trade}[kind]
    operation = {"bars": "CryptoLatestBars", "quotes": "CryptoLatestQuotes", "trades": "CryptoLatestTrades"}[
        kind
    ]
    skips = Skips(model.schema_name)
    async with AlpacaClient(ctx) as api:
        loc = api.settings.crypto_location

        async def fetch() -> list:
            data = await api.get("data", f"{BASE.format(loc=loc)}/latest/{kind}", {"symbols": args.tickers})
            out = []
            for symbol, rec in (data.get(kind) or {}).items():
                out.extend(collect(skips, [rec], _mapper(kind, to_ticker(symbol, CR)), lambda r, s=symbol: s))
            return out

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        model,
        page.rows,
        route=f"GET {BASE}/latest/{kind}",
        operation=operation,
        request=request_of(args, location=loc),
        feed=loc,
        delay=Delay.REALTIME,
        as_of=latest_t(page.rows),
        page=page,
        absent=_absent(kind),
        notes=missing_note(args.tickers, page.rows)
        + stale_rows_note(page.rows)
        + (no_trades_note(page.rows) if kind == "bars" else []),
        skips=skips,
    )


async def crypto_latest_bars(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "bars")


async def crypto_latest_quotes(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "quotes")


async def crypto_latest_trades(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    return await _latest(ctx, args, "trades")


async def crypto_snapshots(ctx: ToolContext, args: LatestIn) -> ToolOutput:
    skips = Skips(Snapshot.schema_name)
    async with AlpacaClient(ctx) as api:
        loc = api.settings.crypto_location

        async def fetch() -> list:
            data = await api.get("data", f"{BASE.format(loc=loc)}/snapshots", {"symbols": args.tickers})
            out = []
            for symbol, rec in (data.get("snapshots") or {}).items():
                out.extend(
                    collect(
                        skips,
                        [rec],
                        lambda r, s=symbol: mappers.snapshot(
                            r,
                            ticker=to_ticker(s, CR),
                            asset_class=CR,
                            currency=quote_currency(to_ticker(s, CR)),
                        ),
                        lambda r, s=symbol: s,
                    )
                )
            return out

        page = await fetch_once(ctx, fetch)
    return output(
        ctx,
        Snapshot,
        page.rows,
        route=f"GET {BASE}/snapshots",
        operation="CryptoSnapshots",
        request=request_of(args, location=loc),
        feed=loc,
        delay=Delay.REALTIME,
        as_of=latest_t(page.rows),
        page=page,
        notes=missing_note(args.tickers, page.rows)
        + stale_rows_note(page.rows)
        + day_close_basis_note(page.rows, STALE_TRADE),
        skips=skips,
    )


async def crypto_orderbooks(ctx: ToolContext, args: OrderbooksIn) -> ToolOutput:
    skips = Skips(OrderBookLevel.schema_name)
    empty = 0
    async with AlpacaClient(ctx) as api:
        loc = api.settings.crypto_location

        async def fetch() -> list:
            nonlocal empty
            data = await api.get(
                "data", f"{BASE.format(loc=loc)}/latest/orderbooks", {"symbols": args.tickers}
            )
            out = []
            for symbol, book in (data.get("orderbooks") or {}).items():
                levels, n = mappers.orderbook(book, ticker=to_ticker(symbol, CR), depth=args.depth)
                empty += n
                out.extend(levels)
            return out

        page = await fetch_once(ctx, fetch)
    notes = missing_note(args.tickers, page.rows)
    if empty:
        notes.append(f"Dropped {empty} price level(s) of size 0 (no liquidity).")
    sides = Counter((r.ticker, r.side) for r in page.rows)
    short = [
        f"{t} {sides[(t, 'bid')]} bid and {sides[(t, 'ask')]} ask"
        for t in dict.fromkeys(r.ticker for r in page.rows)
        if min(sides[(t, "bid")], sides[(t, "ask")]) < args.depth
    ]
    if short:
        notes.append(
            f"Alpaca's book had fewer levels than the depth of {args.depth} asked for: {named(short)} level(s)."
        )
    return output(
        ctx,
        OrderBookLevel,
        page.rows,
        route=f"GET {BASE}/latest/orderbooks",
        operation="CryptoLatestOrderbooks",
        request=request_of(args, location=loc),
        feed=loc,
        delay=Delay.REALTIME,
        as_of=latest_t(page.rows),
        page=page,
        notes=notes,
        skips=skips,
    )


_SIZE = " " + STORED_NOTE
_R = " (market data API)"
SPECS = (
    spec(
        name="crypto_bars",
        capability="market",
        title="Crypto bars",
        description="Historical OHLCV bars for crypto pairs (prices in the quote currency, volume in base "
        "units), one row per pair and bar start (UTC). timeframe default 1h; window by start/end or lookback "
        "(default P1D). " + NO_TRADES_DOC + LAST_BAR_DOC + _SIZE,
        readme="OHLCV bars for crypto pairs",
        input_model=BarsIn,
        output_model=Bar,
        route=f"GET {BASE}/bars{_R}",
        handler=crypto_bars,
        golden_test=GOLDEN,
        operations=("CryptoBars",),
        parity=("get_crypto_bars",),
    ),
    spec(
        name="crypto_quotes",
        capability="market",
        title="Crypto quotes",
        description="Historical best bid and ask for crypto pairs (sizes in base units); a side with no quote "
        "is null (no_data). Window by start/end or lookback (default PT15M)." + _SIZE,
        readme="Historical crypto quotes",
        input_model=TicksIn,
        output_model=Quote,
        route=f"GET {BASE}/quotes{_R}",
        handler=crypto_quotes,
        golden_test=GOLDEN,
        operations=("CryptoQuotes",),
        parity=("get_crypto_quotes",),
    ),
    spec(
        name="crypto_trades",
        capability="market",
        title="Crypto trades",
        description="Historical crypto trades (size in base units) with the taker side (buy/sell). Window by "
        "start/end or lookback (default PT15M); limit keeps each pair's first trades in sort order (sort desc: "
        "the latest)." + _SIZE,
        readme="Historical crypto trades",
        input_model=TradesIn,
        output_model=Trade,
        route=f"GET {BASE}/trades{_R}",
        handler=crypto_trades,
        golden_test=GOLDEN,
        operations=("CryptoTrades",),
        parity=("get_crypto_trades",),
    ),
    spec(
        name="crypto_latest_bars",
        capability="market",
        title="Latest crypto bars",
        description="The latest one-minute bar for each crypto pair. " + NO_TRADES_DOC,
        readme="Latest minute bar per pair",
        input_model=LatestIn,
        output_model=Bar,
        route=f"GET {BASE}/latest/bars{_R}",
        handler=crypto_latest_bars,
        golden_test=GOLDEN,
        operations=("CryptoLatestBars",),
        parity=("get_crypto_latest_bar",),
    ),
    spec(
        name="crypto_latest_quotes",
        capability="market",
        title="Latest crypto quotes",
        description="The latest best bid and ask for each crypto pair (sizes in base units).",
        readme="Latest quote per pair",
        input_model=LatestIn,
        output_model=Quote,
        route=f"GET {BASE}/latest/quotes{_R}",
        handler=crypto_latest_quotes,
        golden_test=GOLDEN,
        operations=("CryptoLatestQuotes",),
        parity=("get_crypto_latest_quote",),
    ),
    spec(
        name="crypto_latest_trades",
        capability="market",
        title="Latest crypto trades",
        description="The latest trade for each crypto pair, with the taker side.",
        readme="Latest trade per pair",
        input_model=LatestIn,
        output_model=Trade,
        route=f"GET {BASE}/latest/trades{_R}",
        handler=crypto_latest_trades,
        golden_test=GOLDEN,
        operations=("CryptoLatestTrades",),
        parity=("get_crypto_latest_trade",),
    ),
    spec(
        name="crypto_snapshots",
        capability="market",
        title="Crypto snapshots",
        description="Latest state per crypto pair: last trade, best bid/ask, latest minute close, today's "
        "OHLCV and VWAP (UTC day), previous close, and change / change_pct (fraction) against the previous close: "
        "from the last trade while it is from today, else from today's bar close (a pair whose last trade is "
        "older), as change_basis says, with a note.",
        readme="Last trade, quote, day bar, change",
        input_model=LatestIn,
        output_model=Snapshot,
        route=f"GET {BASE}/snapshots{_R}",
        handler=crypto_snapshots,
        golden_test=GOLDEN,
        operations=("CryptoSnapshots",),
        parity=("get_crypto_snapshot",),
    ),
    spec(
        name="crypto_orderbooks",
        capability="market",
        title="Crypto order books",
        description="The latest order book per crypto pair as one row per price level and side (level 0 is "
        "the best price; size in base units), up to depth levels per side (default 20). Empty levels are "
        "dropped and counted in notes, and a book with fewer levels than depth is named in notes.",
        readme="Order book levels per pair",
        input_model=OrderbooksIn,
        output_model=OrderBookLevel,
        route=f"GET {BASE}/latest/orderbooks{_R}",
        handler=crypto_orderbooks,
        golden_test=GOLDEN,
        operations=("CryptoLatestOrderbooks",),
        parity=("get_crypto_latest_orderbook",),
    ),
)
