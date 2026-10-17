"""The first and last bars of a window (market_bars, options_bars, crypto_bars).

Alpaca returns the bars that start from ``start`` through ``end``, each whole:
the week whose Wednesday is ``start`` has no bar, and the 1w bar of a week
whose Thursday is ``end`` holds Friday's trades too. The stretch from ``start``
that no bar covers is named. A bar whose period runs past an ``end`` before
the newest data is kept whole when no trade falls between ``end`` and its
close (a week ending on a Friday), and otherwise rebuilt from the daily bars of
its whole days up to ``end`` (1w and Nmo bars, whose open, high, low, close,
volume and trade count are their days') or left out (1d and shorter bars, which
only trades would rebuild exactly). A bar still open, its period running past
the newest data, is named: the window reaches the newest data, or nothing
traded from end to it."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from marketlens_mcp.plugin_api import ToolContext, ToolError
from marketlens_schema.base import AbsenceCode

from ..client import AlpacaClient
from ..convert import integer, iso_z, parse_ts
from .common import Window, named
from .marketdata import fetch_grouped

TICK = timedelta(microseconds=1)
#: Alpaca starts Nmin and Nh bars at multiples of N from Go's zero time (7min bars at 13:34Z, 5h bars at 04:00Z).
ZERO_TIME = datetime(1, 1, 1, tzinfo=UTC)
#: Trades asked for to learn which instruments trade after end; past it, one the answer does not show may.
TRADES_PEEK = 1000
#: Appended to the bar tools' descriptions.
LAST_BAR_DOC = (
    " Bars hold no trades outside the window: Alpaca leaves out the bar that starts before start, and a bar "
    "whose period runs past end is kept whole only if no trade falls after end; else a 1w or Nmo bar is "
    "rebuilt from the 1d bars of its whole days to end (vwap null) and a shorter one is left out (an end "
    "given as a date takes in that whole day). Notes name each, as they do the bar still open now."
)


def _size(timeframe: str) -> tuple[int, str]:
    return int(timeframe.rstrip("minhdwo")), timeframe.lstrip("0123456789")


def bar_end(t: datetime, timeframe: str, zone: tzinfo = UTC) -> datetime:
    """When a bar of ``timeframe`` starting at ``t`` closes: N minutes or hours
    later, or N days, weeks or months later on the calendar of ``zone``, where
    Alpaca starts the bars of a day or longer (midnight New York for stocks and
    options, so a week across a clock change is an hour longer or shorter in
    UTC; midnight UTC for crypto)."""
    n, unit = _size(timeframe)
    if unit == "min":
        return t + timedelta(minutes=n)
    if unit == "h":
        return t + timedelta(hours=n)
    local = t.astimezone(zone)
    if unit == "mo":
        months = local.month - 1 + n
        local = local.replace(year=local.year + months // 12, month=months % 12 + 1)
    else:
        local += timedelta(days=n * 7 if unit == "w" else n)
    return local.astimezone(UTC)


def bar_start(t: datetime, timeframe: str, zone: tzinfo = UTC) -> datetime:
    """When the bar of ``timeframe`` that holds ``t`` starts: N minutes or hours
    from ZERO_TIME, or at midnight in ``zone`` of the day, the Monday or the
    first month of N counted from January."""
    n, unit = _size(timeframe)
    if unit in ("min", "h"):
        return t - (t - ZERO_TIME) % timedelta(**{"minutes" if unit == "min" else "hours": n})
    day = t.astimezone(zone).date()
    if unit == "w":
        day -= timedelta(days=day.weekday())
    elif unit == "mo":
        day = day.replace(month=(day.month - 1) // n * n + 1, day=1)
    return datetime.combine(day, time(0), zone).astimezone(UTC)


def first_bar_note(win: Window, timeframe: str, zone: tzinfo = UTC) -> list[str]:
    """Alpaca leaves out the bar that starts before the window's start: name
    the stretch from start that no bar covers. A start at 00:00 UTC (a date,
    or a lookback for bars of a day or longer) means that day in ``zone``, and
    the arbitrary start a lookback gives shorter bars is not named."""
    intraday = timeframe.endswith(("min", "h"))
    if intraday and win.lookback is not None:
        return []
    start = win.start.astimezone(UTC)
    if not intraday and start.time() == time(0):
        start = datetime.combine(start.date(), time(0), zone).astimezone(UTC)
    first = bar_start(start, timeframe, zone)
    upto = min(bar_end(first, timeframe, zone), win.as_of)
    if first == start or upto <= win.start:
        return []
    return [
        f"Alpaca leaves out the {timeframe} bar from {iso_z(first)}, which starts before start "
        f"({iso_z(win.start)}): no bar covers start to {iso_z(upto)}. A start of {iso_z(first)} takes in that "
        "whole bar."
    ]


async def _trading_after(
    api: AlpacaClient,
    path: str,
    params: Mapping[str, Any],
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> tuple[set[str], bool]:
    """Which of ``symbols`` (Alpaca's) may trade from ``start`` through ``end``,
    and whether each of those was seen to: one request for their trades, on
    the bars' feed. A symbol it does not settle (the request failed, or its
    answer runs past TRADES_PEEK trades) may trade."""
    query = {"symbols": symbols, "start": iso_z(start), "end": iso_z(end), "limit": TRADES_PEEK}
    if "feed" in params:
        query["feed"] = params["feed"]
    try:
        data = await api.get("data", path.removesuffix("/bars") + "/trades", query)
    except ToolError:
        return set(symbols), False
    seen = {s for s, trades in (data.get("trades") or {}).items() if trades}
    if data.get("next_page_token"):
        return set(symbols), seen >= set(symbols)
    return seen, True


async def cap_last_bars(
    ctx: ToolContext,
    api: AlpacaClient,
    win: Window,
    rows: list[Any],
    timeframe: str,
    *,
    path: str,
    params: Mapping[str, Any],
    key: str,
    zone: tzinfo = UTC,
    symbol: Callable[[str], str] = str,
    unit: str = "",
) -> tuple[list[Any], list[str]]:
    """The rows without trades after the window's end, and notes naming the
    bars whose period runs past it or past the newest data. ``path`` and
    ``params`` are the tool's bars request (its trades are asked whether
    anything trades after end, and it is sent again for the 1Day bars that
    rebuild a 1w or Nmo bar), ``key`` is the rows' instrument column,
    ``symbol`` its Alpaca symbol and ``unit`` what a note calls the
    instruments it does not name."""
    after = (win.end or win.latest) + TICK
    over = [r for r in rows if bar_end(r.t, timeframe, zone) > after]
    if not over:
        return rows, []
    start = min(r.t for r in over)
    close = bar_end(start, timeframe, zone)

    def current(bars: list[Any], held: str = "") -> list[str]:
        """The bars whose period runs past the newest data: later trades would change them."""
        if not bars:
            return []
        now = ctx.now()
        which, reach, so_far = (
            ("current one", f"now ({iso_z(now)})", "so far")
            if close > now
            else ("newest one served", f"the newest data the feed serves ({iso_z(win.latest)})", "to then")
        )
        return [
            f"The {timeframe} bar from {iso_z(start)} ({named(sorted({getattr(r, key) for r in bars}), unit)}) is "
            f"the {which}: its period runs to {iso_z(close)}, after {reach}, so it holds the trades {so_far}{held}, "
            "and any later trade in its period would change it."
        ]

    if win.end is None or win.end >= win.latest:
        return rows, current(over)
    # a bar with no trade between end and its close (a week whose end is a Friday) is whole as it is, and still
    # the current one when its period runs past the newest data (a month whose end is last Friday)
    trading, seen = await _trading_after(
        api, path, params, sorted({symbol(getattr(r, key)) for r in over}), win.end, min(close, win.latest)
    )
    forming = current(
        [r for r in over if symbol(getattr(r, key)) not in trading and close > win.latest],
        " (none after end)",
    )
    over = [r for r in over if symbol(getattr(r, key)) in trading]
    if not over:
        return rows, forming

    def said(bars: list[Any], what: str) -> str:
        return (
            f"The {timeframe} bar from {iso_z(start)} ({named(sorted(getattr(r, key) for r in bars), unit)}) runs "
            f"to {iso_z(close)}, past end ({iso_z(win.end)}), and trades after end "
            f"{'fall' if seen else 'may fall'} in it: {what}"
        )

    day = datetime.combine(after.astimezone(zone).date(), time(0), zone).astimezone(UTC)
    if not timeframe.endswith(("w", "mo")) or day <= start:
        return _without(rows, over), [
            said(over, "it is left out; bars of a shorter timeframe cover its start to end."),
            *forming,
        ]
    try:
        days = await fetch_grouped(
            ctx,
            api,
            path=path,
            params={
                **params,
                "symbols": sorted({symbol(getattr(r, key)) for r in over}),
                "timeframe": "1Day",
                "start": iso_z(start),
                "end": iso_z(day - TICK),
            },
            key="bars",
            mapper=lambda s, records: [(s, d) for d in records],
        )
    except ToolError as e:
        return _without(rows, over), [said(over, f"it is left out (its 1d bars failed: {e.code})."), *forming]
    if not days.complete or days.row_cap_hit or days.page_cap_hit:
        return _without(rows, over), [
            said(over, "it is left out (its 1d bars stopped at the fetch limits; narrow the request)."),
            *forming,
        ]
    by_symbol: dict[str, list[Any]] = {}
    for s, d in days.rows:
        by_symbol.setdefault(s, []).append(d)
    rebuilt = {}
    for r in over:
        try:
            rebuilt[id(r)] = _from_days(r, by_symbol[symbol(getattr(r, key))])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    first, last = start.astimezone(zone).date(), (day - TICK).astimezone(zone).date()
    dates = f"{first} through {last} ({'UTC' if zone == UTC else 'New York'} dates)"
    notes = []
    if rebuilt:
        notes.append(
            said(
                [r for r in over if id(r) in rebuilt],
                f"it is rebuilt from the 1d bars of {dates}, so it covers those days only; its vwap is null "
                "(insufficient_data), which the 1d bars do not give exactly.",
            )
        )
    left = [r for r in over if id(r) not in rebuilt]
    if left:
        notes.append(said(left, f"it is left out, as no 1d bar of it falls on {dates}."))
    return _without([rebuilt.get(id(r), r) for r in rows], left), [*notes, *forming]


def _without(rows: list[Any], drop: list[Any]) -> list[Any]:
    gone = {id(r) for r in drop}
    return [r for r in rows if id(r) not in gone]


def _from_days(row: Any, days: list[Mapping[str, Any]]) -> Any:
    """``row`` with the open, high, low, close, volume and trade count of the
    daily bars ``days``, and no vwap."""
    days = sorted(days, key=lambda d: parse_ts(d["t"]))
    counts = [integer(d.get("n")) for d in days]
    values = {
        "open": float(days[0]["o"]),
        "high": max(float(d["h"]) for d in days),
        "low": min(float(d["l"]) for d in days),
        "close": float(days[-1]["c"]),
        "volume": math.fsum(float(d["v"]) for d in days),
        "trade_count": None if None in counts else sum(counts),
        "vwap": None,
    }
    absent = {k: v for k, v in (row.absent or {}).items() if k not in values}
    absent["vwap"] = AbsenceCode.INSUFFICIENT_DATA
    if values["trade_count"] is None:
        absent["trade_count"] = AbsenceCode.NOT_PROVIDED_BY_SOURCE
    return row.model_copy(update={**values, "absent": absent})
