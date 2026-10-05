"""Alpaca JSON -> canonical rows. One function per upstream shape.

Absence rules applied here: a field Alpaca leaves out is None with
``not_provided_by_source``; a 0 price (or size) that Alpaca documents as "no
active bid/ask" is None with ``no_data``; a field that cannot apply to the row
is None with ``not_applicable``. Fields that are absent for a whole response
(crypto quotes never carry an exchange) are passed in ``skip`` and explained
once in the response-level ``absent`` map by the tool.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from marketlens_schema.base import TICKER_RE, AbsenceCode
from marketlens_schema.market import (
    Bar,
    FixedIncomeQuote,
    MostActive,
    Mover,
    NewsItem,
    OptionBar,
    OptionQuote,
    OptionSnapshot,
    OptionTrade,
    OrderBookLevel,
    Quote,
    Snapshot,
    Trade,
)

from .convert import build_row, integer, num, parse_occ, parse_ts, text, to_ticker

NO_DATA = AbsenceCode.NO_DATA
NOT_PROVIDED = AbsenceCode.NOT_PROVIDED_BY_SOURCE
NOT_APPLICABLE = AbsenceCode.NOT_APPLICABLE
INSUFFICIENT = AbsenceCode.INSUFFICIENT_DATA

#: Alpaca reported stock quote sizes in round lots before this instant
#: (2025-11-03 00:00 New York) and in shares from then on.
QUOTE_LOT_CUTOFF = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
ROUND_LOT = 100
#: Trades Alpaca marks as no longer valid.
INVALID_TRADE_UPDATES = frozenset({"canceled", "incorrect"})

Raw = Mapping[str, Any]


def bar(raw: Raw, *, ticker: str, asset_class: str, timeframe: str, currency: str = "USD") -> Bar:
    return build_row(
        Bar,
        {
            "ticker": ticker,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "t": parse_ts(raw["t"]),
            "open": num(raw["o"]),
            "high": num(raw["h"]),
            "low": num(raw["l"]),
            "close": num(raw["c"]),
            "volume": num(raw["v"]),
            "trade_count": integer(raw.get("n")),
            "vwap": num(raw.get("vw")),
            "currency": currency,
        },
    )


def _side(raw: Raw, prefix: str, *, lots: bool) -> tuple[dict[str, Any], dict[str, AbsenceCode]]:
    """bid_* or ask_* fields of a quote; a 0 or missing price means no active side."""
    name = "bid" if prefix == "b" else "ask"
    price, size = num(raw.get(f"{prefix}p")), num(raw.get(f"{prefix}s"))
    if not price:
        code = NO_DATA if price == 0 else NOT_PROVIDED
        return {}, {f"{name}_price": code, f"{name}_size": code, f"{name}_exchange": code}
    if lots and size is not None:
        size *= ROUND_LOT
    return {f"{name}_price": price, f"{name}_size": size, f"{name}_exchange": text(raw.get(f"{prefix}x"))}, {}


def quote(
    raw: Raw, *, ticker: str, asset_class: str, currency: str = "USD", skip: Iterable[str] = ()
) -> Quote:
    t = parse_ts(raw["t"])
    lots = asset_class == "us_equity" and t is not None and t < QUOTE_LOT_CUTOFF
    data: dict[str, Any] = {
        "ticker": ticker,
        "asset_class": asset_class,
        "t": t,
        "currency": currency,
        "conditions": raw.get("c"),
        "tape": text(raw.get("z")),
    }
    codes: dict[str, AbsenceCode] = {}
    for prefix in ("b", "a"):
        values, side_codes = _side(raw, prefix, lots=lots)
        data.update(values)
        codes.update(side_codes)
    return build_row(Quote, data, codes, skip=skip)


def trade(
    raw: Raw, *, ticker: str, asset_class: str, currency: str = "USD", skip: Iterable[str] = ()
) -> Trade:
    taker = {"B": "buy", "S": "sell"}.get(str(raw.get("tks") or ""))
    trade_id = raw.get("i")
    return build_row(
        Trade,
        {
            "ticker": ticker,
            "asset_class": asset_class,
            "t": parse_ts(raw["t"]),
            "price": num(raw["p"]),
            "size": num(raw["s"]),
            "exchange": text(raw.get("x")),
            "trade_id": None if trade_id is None else str(trade_id),
            "conditions": raw.get("c"),
            "tape": text(raw.get("z")),
            "taker_side": taker,
            "currency": currency,
        },
        skip=skip,
    )


def valid_trades(items: Iterable[Raw]) -> tuple[list[Raw], int]:
    """Drop trades Alpaca marks canceled or incorrect; return (kept, dropped)."""
    items = list(items)
    kept = [t for t in items if t.get("u") not in INVALID_TRADE_UPDATES]
    return kept, len(items) - len(kept)


def snapshot(raw: Raw | None, *, ticker: str, asset_class: str, currency: str = "USD") -> Snapshot | None:
    """A stock or crypto snapshot; None when Alpaca has nothing for the symbol."""
    if not raw:
        return None
    lt, lq = raw.get("latestTrade") or {}, raw.get("latestQuote") or {}
    mb, db, pdb = raw.get("minuteBar") or {}, raw.get("dailyBar") or {}, raw.get("prevDailyBar") or {}
    stamps = [parse_ts(p["t"]) for p in (lt, lq, mb, db) if p.get("t")]
    if not stamps:
        return None
    quote_t = parse_ts(lq.get("t")) if lq else None
    lots = asset_class == "us_equity" and quote_t is not None and quote_t < QUOTE_LOT_CUTOFF
    data: dict[str, Any] = {
        "ticker": ticker,
        "asset_class": asset_class,
        "t": max(stamps),
        "currency": currency,
        "last_price": num(lt.get("p")),
        "last_size": num(lt.get("s")),
        "last_t": parse_ts(lt.get("t")),
        "quote_t": quote_t,
        "minute_close": num(mb.get("c")),
        "minute_t": parse_ts(mb.get("t")),
        "day_open": num(db.get("o")),
        "day_high": num(db.get("h")),
        "day_low": num(db.get("l")),
        "day_close": num(db.get("c")),
        "day_volume": num(db.get("v")),
        "day_vwap": num(db.get("vw")),
        "prev_close": num(pdb.get("c")),
        "prev_volume": num(pdb.get("v")),
    }
    codes: dict[str, AbsenceCode] = {}
    if lq:
        for prefix in ("b", "a"):
            values, side_codes = _side(lq, prefix, lots=lots)
            data.update({k: v for k, v in values.items() if not k.endswith("_exchange")})
            codes.update({k: v for k, v in side_codes.items() if not k.endswith("_exchange")})
    last, prev = data["last_price"], data["prev_close"]
    if last is not None and prev is not None:
        data["change"] = last - prev
        if prev:
            data["change_pct"] = (last - prev) / prev
        else:
            codes["change_pct"] = INSUFFICIENT
    return build_row(Snapshot, data, codes)


def most_active(raw: Raw, *, rank: int, ranked_by: str, as_of: datetime) -> MostActive:
    return build_row(
        MostActive,
        {
            "ticker": to_ticker(raw["symbol"], "us_equity"),
            "rank": rank,
            "ranked_by": ranked_by,
            "volume": num(raw["volume"]),
            "trade_count": integer(raw["trade_count"]),
            "as_of": as_of,
        },
    )


def mover(raw: Raw, *, rank: int, market_type: str, direction: str, as_of: datetime) -> Mover:
    asset_class = "crypto" if market_type == "crypto" else "us_equity"
    return build_row(
        Mover,
        {
            "ticker": to_ticker(raw["symbol"], asset_class),
            "market_type": market_type,
            "direction": direction,
            "rank": rank,
            "price": num(raw["price"]),
            "change": num(raw["change"]),
            "percent_change": num(raw["percent_change"]) / 100,
            "as_of": as_of,
        },
    )


def orderbook(raw: Raw, *, ticker: str, depth: int) -> tuple[list[OrderBookLevel], int]:
    """One crypto order book as levels (bids best-first by descending price,
    asks by ascending price), at most ``depth`` per side. Levels of size 0 carry
    no liquidity and are dropped; the count is returned for a note."""
    t = parse_ts(raw["t"])
    rows: list[OrderBookLevel] = []
    empty = 0
    for side, key, descending in (("bid", "b", True), ("ask", "a", False)):
        levels = [lv for lv in raw.get(key) or [] if num(lv.get("s"))]
        empty += len(raw.get(key) or []) - len(levels)
        levels.sort(key=lambda lv: num(lv["p"]) or 0.0, reverse=descending)
        for i, lv in enumerate(levels[:depth]):
            rows.append(
                build_row(
                    OrderBookLevel,
                    {
                        "ticker": ticker,
                        "asset_class": "crypto",
                        "t": t,
                        "side": side,
                        "level": i,
                        "price": num(lv["p"]),
                        "size": num(lv["s"]),
                        "currency": ticker.partition("/")[2] or "USD",
                    },
                )
            )
    return rows, empty


# --- options ---------------------------------------------------------------------------------


def option_bar(raw: Raw, *, occ: str, timeframe: str) -> OptionBar:
    return build_row(
        OptionBar,
        {
            "occ_symbol": occ,
            "underlying": parse_occ(occ).root,
            "timeframe": timeframe,
            "t": parse_ts(raw["t"]),
            "open": num(raw["o"]),
            "high": num(raw["h"]),
            "low": num(raw["l"]),
            "close": num(raw["c"]),
            "volume": num(raw["v"]),
            "trade_count": integer(raw.get("n")),
            "vwap": num(raw.get("vw")),
        },
    )


def option_quote(raw: Raw, *, occ: str) -> OptionQuote:
    data: dict[str, Any] = {
        "occ_symbol": occ,
        "underlying": parse_occ(occ).root,
        "t": parse_ts(raw["t"]),
        "condition": text(raw.get("c")),
    }
    codes: dict[str, AbsenceCode] = {}
    for prefix in ("b", "a"):
        values, side_codes = _side(raw, prefix, lots=False)
        data.update(values)
        codes.update(side_codes)
    return build_row(OptionQuote, data, codes)


def option_trade(raw: Raw, *, occ: str) -> OptionTrade:
    return build_row(
        OptionTrade,
        {
            "occ_symbol": occ,
            "underlying": parse_occ(occ).root,
            "t": parse_ts(raw["t"]),
            "price": num(raw["p"]),
            "size": num(raw["s"]),
            "exchange": text(raw.get("x")),
            "condition": text(raw.get("c")),
        },
    )


def option_snapshot(raw: Raw | None, *, occ: str, underlying: str | None = None) -> OptionSnapshot | None:
    """Expiration, strike and type come from the OCC symbol; the underlying is
    the one requested (options_chain) or the OCC root. Greeks and implied
    volatility are Alpaca's (Black-Scholes), absent when Alpaca omits them."""
    if not raw:
        return None
    lt, lq = raw.get("latestTrade") or {}, raw.get("latestQuote") or {}
    mb, db, pdb = raw.get("minuteBar") or {}, raw.get("dailyBar") or {}, raw.get("prevDailyBar") or {}
    stamps = [parse_ts(p["t"]) for p in (lt, lq, mb, db) if p.get("t")]
    if not stamps:
        return None
    parts = parse_occ(occ)
    greeks = raw.get("greeks") or {}
    data: dict[str, Any] = {
        "occ_symbol": occ,
        "underlying": underlying or parts.root,
        "expiration_date": parts.expiration,
        "strike": parts.strike,
        "option_type": parts.option_type,
        "t": max(stamps),
        "last_price": num(lt.get("p")),
        "last_size": num(lt.get("s")),
        "last_t": parse_ts(lt.get("t")),
        "quote_t": parse_ts(lq.get("t")),
        "day_open": num(db.get("o")),
        "day_high": num(db.get("h")),
        "day_low": num(db.get("l")),
        "day_close": num(db.get("c")),
        "day_volume": num(db.get("v")),
        "prev_close": num(pdb.get("c")),
        "implied_volatility": num(raw.get("impliedVolatility")),
        **{g: num(greeks.get(g)) for g in ("delta", "gamma", "theta", "vega", "rho")},
    }
    codes: dict[str, AbsenceCode] = {}
    if lq:
        for prefix in ("b", "a"):
            values, side_codes = _side(lq, prefix, lots=False)
            data.update({k: v for k, v in values.items() if not k.endswith("_exchange")})
            codes.update({k: v for k, v in side_codes.items() if not k.endswith("_exchange")})
    return build_row(OptionSnapshot, data, codes)


# --- fixed income and news -------------------------------------------------------------------


def fixed_income_quote(raw: Raw, *, isin: str) -> FixedIncomeQuote:
    """Prices stay in percent of par; yields arrive in percent and become
    fractions. A 0 price means no active side: that side's price, size and
    yields are None with no_data."""
    data: dict[str, Any] = {"isin": isin, "t": parse_ts(raw["t"])}
    codes: dict[str, AbsenceCode] = {}
    for prefix, name in (("b", "bid"), ("a", "ask")):
        price = num(raw.get(f"{prefix}p"))
        fields = (f"{name}_price", f"{name}_size", f"{name}_ytm", f"{name}_ytw")
        if not price:
            codes.update(dict.fromkeys(fields, NO_DATA if price == 0 else NOT_PROVIDED))
            continue
        ytm, ytw = num(raw.get(f"{prefix}ytm")), num(raw.get(f"{prefix}ytw"))
        data.update(
            {
                f"{name}_price": price,
                f"{name}_size": num(raw.get(f"{prefix}s")) or None,
                f"{name}_ytm": None if ytm is None else ytm / 100,
                f"{name}_ytw": None if ytw is None else ytw / 100,
            }
        )
    return build_row(FixedIncomeQuote, data, codes)


def news_item(raw: Raw, *, include_content: bool) -> tuple[NewsItem, int]:
    """A news article; returns the row and how many of its symbols were not
    tickers (dropped). Text fields are untrusted third-party prose."""
    tickers, dropped = [], 0
    for sym in raw.get("symbols") or []:
        t = to_ticker(str(sym), "us_equity")
        if TICKER_RE.match(t):
            tickers.append(t)
        else:
            dropped += 1
    row = build_row(
        NewsItem,
        {
            "news_id": str(raw["id"]),
            "headline": raw["headline"],
            "summary": text(raw.get("summary")),
            "content": text(raw.get("content")) if include_content else None,
            "author": text(raw.get("author")),
            "publisher": text(raw.get("source")),
            "url": text(raw.get("url")),
            "tickers": tickers,
            "created_at": parse_ts(raw["created_at"]),
            "updated_at": parse_ts(raw.get("updated_at")),
        },
        skip=() if include_content else ("content",),
    )
    return row, dropped
