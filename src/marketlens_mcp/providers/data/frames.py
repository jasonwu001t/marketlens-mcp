"""marketlens-data's DataFrames to canonical values.

Stored rows come back from DuckDB as pandas values: NaN/NaT/NA for nulls,
numpy scalars, ``Timestamp`` (UTC-aware for instants, naive for dates), 0/1
integers for some flags. These helpers turn each into the plain Python value
a canonical model takes, never inventing one: a null stays None (the tool
explains it), a percent becomes a fraction only where the tool says so.
Imports pandas lazily (it comes with marketlens-data)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any


def missing(value: Any) -> bool:
    """None, NaN, NaT or pandas NA (a list or other container is never missing)."""
    if value is None:
        return True
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return False
    import pandas as pd

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def plain(value: Any) -> Any:
    """None for a missing value; numpy scalars as Python scalars."""
    if missing(value):
        return None
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (str, bytes, datetime, date)):
        try:
            return item()
        except (TypeError, ValueError):
            return value
    return value


def text(value: Any) -> str | None:
    v = plain(value)
    if v is None:
        return None
    s = str(v)
    return s if s.strip() else None


def number(value: Any) -> float | None:
    v = plain(value)
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def integer(value: Any) -> int | None:
    f = number(value)
    return None if f is None else int(f)


def flag(value: Any) -> bool | None:
    v = plain(value)
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "y", "yes"}
    return bool(v)


def day(value: Any) -> date | None:
    v = plain(value)
    if v is None:
        return None
    if hasattr(v, "to_pydatetime"):
        v = v.to_pydatetime()
    if isinstance(v, datetime):
        return v.astimezone(UTC).date() if v.tzinfo else v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def instant(value: Any) -> datetime | None:
    """A UTC instant; a naive value is read as UTC (marketlens-data's store session is UTC)."""
    v = plain(value)
    if v is None:
        return None
    if hasattr(v, "to_pydatetime"):
        v = v.to_pydatetime()
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if not isinstance(v, datetime):
        return None
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


def scaled(value: Any, factor: float, digits: int = 6) -> float | None:
    """value x factor (millions or billions to USD), rounded to drop float noise."""
    f = number(value)
    return None if f is None else round(f * factor, digits)


def fraction(value: Any) -> float | None:
    """A percent as a fraction: 4.33 -> 0.0433."""
    f = number(value)
    return None if f is None else round(f / 100.0, 12)


def split(value: Any, sep: str = ",") -> list[str]:
    s = text(value)
    return [p.strip() for p in s.split(sep) if p.strip()] if s else []


def records(df: Any) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def within(value: date | None, start: date | None, end: date | None) -> bool:
    if value is None:
        return start is None and end is None
    return (start is None or value >= start) and (end is None or value <= end)


def first(values: Iterable[Any]) -> Any:
    return next(iter(values), None)
