"""Raw Alpaca values -> canonical values, and user input -> Alpaca parameters.

Pure functions, no I/O. The rules (schema conventions in marketlens_schema.base):
timestamps are UTC with microsecond precision (Alpaca sends up to nine
fractional digits; the extra digits are truncated), equity tickers use the SEC
share-class hyphen (Alpaca's ``BRK.B`` is ``BRK-B``), crypto pairs are
``BASE/QUOTE``, exact money is Decimal built from text, and a value Alpaca does
not give is None with a reason in the row's ``absent`` map.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from marketlens_schema.base import (
    CRYPTO_QUOTE_CURRENCIES,
    OCC_RE,
    TICKER_RE,
    TIMEFRAME_RE,
    AbsenceCode,
    CanonicalModel,
    normalize_ticker,
)

NEW_YORK = ZoneInfo("America/New_York")

_TS_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(?:\.(?P<frac>\d+))?(?P<tz>Z|z|[+-]\d{2}:?\d{2})$"
)


# --- time ------------------------------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    """An Alpaca timestamp (RFC 3339 with up to 9 fractional digits, or UNIX
    seconds) as an aware UTC datetime truncated to microseconds. Empty -> None;
    a timestamp without a zone is refused."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(int(value), tz=UTC)
    m = _TS_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"not an RFC 3339 timestamp with a time zone: {value!r}")
    frac = (m["frac"] or "")[:6].ljust(6, "0")
    tz = "+00:00" if m["tz"] in ("Z", "z") else m["tz"]
    base = m["base"].replace(" ", "T")
    if len(base) == 16:
        base += ":00"
    return datetime.fromisoformat(f"{base}.{frac}{tz}").astimezone(UTC)


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value)[:10])


def parse_instant(value: str) -> datetime:
    """User input: an ISO date (00:00 UTC) or a datetime with a zone."""
    s = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.combine(date.fromisoformat(s), time(0), UTC)
    if _TS_RE.match(s):
        return parse_ts(s)  # type: ignore[return-value]
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s):
        raise ValueError(f"{value!r} has no time zone; add Z or an offset such as -05:00")
    raise ValueError(f"{value!r} is not an ISO date (2026-01-02) or datetime with a time zone")


def iso_z(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


_DURATION_RE = re.compile(
    r"^P(?!$)(?:(?P<y>\d+)Y)?(?:(?P<mo>\d+)M)?(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<h>\d+)H)?(?:(?P<mi>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def parse_duration(value: str) -> timedelta:
    """An ISO-8601 duration of whole units. A year counts as 365 days and a month
    as 30 days (lookbacks are approximate windows, not calendar arithmetic)."""
    m = _DURATION_RE.match(value.strip().upper()) if value else None
    if not m or not any(m.groupdict().values()):
        raise ValueError(f"{value!r} is not an ISO-8601 duration such as P5D, P1Y or PT20M")
    g = {k: int(v or 0) for k, v in m.groupdict().items()}
    try:
        return timedelta(
            days=g["y"] * 365 + g["mo"] * 30 + g["w"] * 7 + g["d"],
            hours=g["h"],
            minutes=g["mi"],
            seconds=g["s"],
        )
    except OverflowError:
        raise ValueError(f"{value!r} is too long a duration") from None


def et_to_utc(day: date, hhmm: str | None) -> datetime | None:
    """A New York wall-clock time (``09:30`` or ``0930``) on ``day`` -> UTC."""
    if not hhmm:
        return None
    digits = hhmm.replace(":", "")
    local = datetime.combine(day, time(int(digits[:2]), int(digits[2:4])), NEW_YORK)
    return local.astimezone(UTC)


_TF_ALPACA = {"min": "Min", "h": "Hour", "d": "Day", "w": "Week", "mo": "Month"}
_TF_FROM = re.compile(r"^(\d+)(Min|T|Hour|H|Day|D|Week|W|Month|M)$")
_TF_UNIT = {
    "Min": "min",
    "T": "min",
    "Hour": "h",
    "H": "h",
    "Day": "d",
    "D": "d",
    "Week": "w",
    "W": "w",
    "Month": "mo",
    "M": "mo",
}


def timeframe_to_alpaca(tf: str) -> str:
    if not TIMEFRAME_RE.match(tf):
        raise ValueError(f"{tf!r} is not a timeframe (Nmin, Nh, 1d, 1w, Nmo)")
    m = re.match(r"^(\d+)(min|h|d|w|mo)$", tf)
    assert m
    return f"{m[1]}{_TF_ALPACA[m[2]]}"


def timeframe_from_alpaca(tf: str) -> str:
    m = _TF_FROM.match(tf)
    if not m:
        raise ValueError(f"unknown Alpaca timeframe {tf!r}")
    out = f"{int(m[1])}{_TF_UNIT[m[2]]}"
    if not TIMEFRAME_RE.match(out):
        raise ValueError(f"Alpaca timeframe {tf!r} has no canonical form")
    return out


# --- instruments -----------------------------------------------------------------------------


def to_ticker(symbol: str, asset_class: str | None) -> str:
    """Alpaca symbol -> canonical ticker: equities swap "." for "-", crypto
    symbols become BASE/QUOTE (positions spell them BTCUSD)."""
    s = symbol.strip().upper()
    if asset_class == "crypto":
        if "/" in s:
            return s
        for quote in sorted(CRYPTO_QUOTE_CURRENCIES, key=len, reverse=True):
            if s.endswith(quote) and len(s) - len(quote) >= 2:
                return f"{s[: -len(quote)]}/{quote}"
        return s
    return s.replace(".", "-")


def alpaca_symbol(ticker: str) -> str:
    """Canonical ticker -> Alpaca query symbol (BRK-B -> BRK.B; pairs unchanged)."""
    return ticker if "/" in ticker else ticker.replace("-", ".")


def path_symbol(ticker: str) -> str:
    """Canonical ticker -> Alpaca path segment (a pair loses its slash: BTCUSD)."""
    return ticker.replace("/", "") if "/" in ticker else ticker.replace("-", ".")


def equity_tickers(raw: Iterable[str]) -> list[str]:
    out = []
    for item in raw:
        t = normalize_ticker(item)
        if "/" in t:
            raise ValueError(f"'{item.strip()}' is a crypto pair; use the crypto_* tools for pairs")
        if not TICKER_RE.match(t):
            raise ValueError(f"'{item}' is not a ticker (expected e.g. AAPL or BRK-B)")
        out.append(t)
    return out


def crypto_pairs(raw: Iterable[str]) -> list[str]:
    out = []
    for item in raw:
        t = normalize_ticker(item)
        if "/" not in t or not TICKER_RE.match(t):
            raise ValueError(f"'{item.strip()}' is not a crypto pair (expected BASE/QUOTE, e.g. BTC/USD)")
        out.append(t)
    return out


def occ_symbols(raw: Iterable[str]) -> list[str]:
    out = []
    for item in raw:
        s = item.strip().upper()
        if not OCC_RE.match(s):
            raise ValueError(f"'{item}' is not an OCC option symbol (e.g. AAPL250117C00150000)")
        out.append(s)
    return out


@dataclass(frozen=True)
class OccParts:
    root: str
    expiration: date
    option_type: str
    strike: float


def parse_occ(symbol: str) -> OccParts:
    m = OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    y = m["yymmdd"]
    return OccParts(
        root=m["root"],
        expiration=date(2000 + int(y[:2]), int(y[2:4]), int(y[4:6])),
        option_type="call" if m["cp"] == "C" else "put",
        strike=int(m["strike"]) / 1000,
    )


def is_occ(symbol: str | None) -> bool:
    return bool(symbol) and bool(OCC_RE.match(symbol or ""))


# --- numbers ---------------------------------------------------------------------------------


def dec(value: Any) -> Decimal | None:
    """Exact decimal from Alpaca's strings (or Decimal/int); a float is read
    through its repr so 0.1 stays 0.1. Empty -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)
    try:
        return Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError(f"not a decimal number: {value!r}") from e


def num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(Decimal(str(value)))


def percent(value: Any) -> float | None:
    """A percent (145.56) as a fraction (1.4556)."""
    v = num(value)
    return None if v is None else v / 100


def text(value: Any) -> str | None:
    """A string field; Alpaca's empty string means no value."""
    if value is None:
        return None
    s = str(value)
    return s if s.strip() else None


# --- rows ------------------------------------------------------------------------------------


def build_row(
    model: type[CanonicalModel],
    data: Mapping[str, Any],
    codes: Mapping[str, AbsenceCode] | None = None,
    default: AbsenceCode = AbsenceCode.NOT_PROVIDED_BY_SOURCE,
    skip: Iterable[str] = (),
) -> Any:
    """Build ``model`` from ``data``; every field left None gets a reason in the
    row's ``absent`` map (``codes`` per field, else ``default``), except the
    fields in ``skip``, which the response-level ``absent`` map explains."""
    codes = codes or {}
    skipped = set(skip)
    values = {k: v for k, v in data.items() if k in model.model_fields and v is not None}
    row = model(**values)
    absent = {
        name: AbsenceCode(codes.get(name, default))
        for name in model.model_fields
        if name != "absent" and name not in skipped and getattr(row, name) is None
    }
    return row.model_copy(update={"absent": absent}) if absent else row
