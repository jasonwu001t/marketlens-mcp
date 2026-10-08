"""Crypto market data (capability ``market``). Pairs are BASE/QUOTE tickers;
the data location is ``providers.alpaca.crypto_location`` (default us)."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput
from marketlens_schema.base import Delay
from marketlens_schema.market import Bar, OrderBookLevel, Quote, Snapshot, Trade

from .. import mappers
from ..client import AlpacaClient
from ..convert import iso_z, timeframe_to_alpaca, to_ticker
from .common import (
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
    latest_t,
    missing_note,
    output,
    request_of,
    spec,
    window,
)
from .marketdata import fetch_grouped, fetch_once

GOLDEN = "tests/alpaca/test_alpaca_crypto.py"
CR = "crypto"
BASE = "/v1beta3/crypto/{loc}"
QUOTE_SKIP = ("bid_exchange", "ask_exchange", "conditions", "tape")
TRADE_SKIP = ("exchange", "conditions", "tape")
NO_VENUE = "Alpaca crypto {what} carry no exchange, conditions or tape."
NO_TRADES_DOC = (
    "A bar with trade_count 0 had no trades: Alpaca fills it from quotes, and notes name the pairs."
)


class BarsIn(Inputs):
    tickers: CryptoPairs
    timeframe: TimeframeIn = Field("1h", description=TIMEFRAME_DOC)
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + " Default P1D.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


class TicksIn(Inputs):
    tickers: CryptoPairs
    start: Instant | None = Field(None, description=START_DOC)
    end: Instant | None = Field(None, description=END_DOC)
    lookback: Duration | None = Field(None, description=LOOKBACK_DOC + " Default PT15M.")
    sort: Literal["asc", "desc"] = Field("asc", description=SORT_DOC)
    page_token: str | None = Field(None, max_length=2000, description=CONTINUE_DOC)


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
    ctx: ToolContext, args: BarsIn | TicksIn, kind: Literal["bars", "quotes", "trades"]
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
        page = await fetch_grouped(
            ctx,
            api,
            path=f"{BASE.format(loc=loc)}/{kind}",
            params=params,
            key=kind,
            mapper=rows,
            start_token=args.page_token,
        )
    return output(
        ctx,
        model,
        page.rows,
        route=f"GET {BASE}/{kind}",
        operation=operation,
        request=request_of(args, **win.fields(), location=loc),
        feed=loc,
        delay=Delay.REALTIME,
        as_of=win.end or ctx.now(),
        page=page,
        absent=_absent(kind),
        notes=no_trades_note(page.rows) if kind == "bars" else [],
        skips=skips,
    )


async def crypto_bars(ctx: ToolContext, args: BarsIn) -> ToolOutput:
    return await _history(ctx, args, "bars")


async def crypto_quotes(ctx: ToolContext, args: TicksIn) -> ToolOutput:
    return await _history(ctx, args, "quotes")


async def crypto_trades(ctx: ToolContext, args: TicksIn) -> ToolOutput:
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
        notes=missing_note(args.tickers, page.rows) + (no_trades_note(page.rows) if kind == "bars" else []),
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
        notes=missing_note(args.tickers, page.rows),
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
        "(default P1D). " + NO_TRADES_DOC + _SIZE,
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
        "start/end or lookback (default PT15M)." + _SIZE,
        readme="Historical crypto trades",
        input_model=TicksIn,
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
        "OHLCV and VWAP, previous close, and change / change_pct (fraction) derived from them.",
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
        "dropped and counted in notes.",
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
