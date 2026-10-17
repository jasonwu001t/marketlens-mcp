"""The last bar of a window (market_bars, options_bars, crypto_bars).

Alpaca returns every bar that starts by ``end``, whole: the 1w bar of a week
whose Thursday is ``end`` holds Friday's trades too. A bar whose period runs
past an ``end`` before the newest data is therefore rebuilt from the daily bars
of its whole days up to ``end`` (1w and Nmo bars, whose open, high, low, close,
volume and trade count are their days') or left out (1d and shorter bars, which
only trades would rebuild exactly). When the window reaches the newest data,
the bar still forming is kept and named."""

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
#: Appended to the bar tools' descriptions.
LAST_BAR_DOC = (
    " No bar holds trades after end: a 1w or Nmo bar whose period runs past it is rebuilt from the 1d bars of "
    "its whole days to end (vwap null), a 1d or shorter one is left out, and the bar still forming now is "
    "kept; notes name each."
)


def bar_end(t: datetime, timeframe: str, zone: tzinfo = UTC) -> datetime:
    """When a bar of ``timeframe`` starting at ``t`` closes: N minutes or hours
    later, or N days, weeks or months later on the calendar of ``zone``, where
    Alpaca starts the bars of a day or longer (midnight New York for stocks and
    options, so a week across a clock change is an hour longer or shorter in
    UTC; midnight UTC for crypto)."""
    n, unit = int(timeframe.rstrip("minhdwo")), timeframe.lstrip("0123456789")
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
) -> tuple[list[Any], list[str]]:
    """The rows without trades after the window's end, and notes naming the
    bars whose period runs past it. ``path`` and ``params`` are the tool's
    bars request (sent again for the 1Day bars that rebuild a 1w or Nmo bar),
    ``key`` is the rows' instrument column and ``symbol`` its Alpaca symbol."""
    after = (win.end or win.latest) + TICK
    over = [r for r in rows if bar_end(r.t, timeframe, zone) > after]
    if not over:
        return rows, []
    start = min(r.t for r in over)
    if win.end is None or win.end >= win.latest:
        return rows, [
            f"The {timeframe} bar from {iso_z(start)} ({named(sorted({getattr(r, key) for r in over}))}) is still "
            f"forming: it holds trades up to now ({iso_z(ctx.now())}) only, not a whole {timeframe}."
        ]

    def said(bars: list[Any], what: str) -> str:
        return (
            f"The {timeframe} bar from {iso_z(start)} ({named(sorted(getattr(r, key) for r in bars))}) runs to "
            f"{iso_z(bar_end(start, timeframe, zone))}, past end ({iso_z(win.end)}), and Alpaca's bar holds the "
            f"trades after end: {what}"
        )

    day = datetime.combine(after.astimezone(zone).date(), time(0), zone).astimezone(UTC)
    if not timeframe.endswith(("w", "mo")) or day <= start:
        return _without(rows, over), [
            said(over, "it is left out; bars of a shorter timeframe cover its start to end.")
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
        return _without(rows, over), [said(over, f"it is left out (its 1d bars failed: {e.code}).")]
    if not days.complete or days.row_cap_hit or days.page_cap_hit:
        return _without(rows, over), [
            said(over, "it is left out (its 1d bars stopped at the fetch limits; narrow the request).")
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
    return _without([rebuilt.get(id(r), r) for r in rows], left), notes


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
