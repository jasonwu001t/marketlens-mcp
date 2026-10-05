"""marketlens canonical schema: the conventions every model follows.

This module depends on pydantic only (no marketlens_mcp import, no I/O), so a
data pipeline can adopt the same models without the server.

CONVENTIONS (schema 1.x)
------------------------
* Field names are snake_case and vendor-neutral. Instruments:
    - ``ticker``     equities and ETFs in SEC style, upper case, share class
                     after a hyphen: ``BRK-B`` (Alpaca spells it ``BRK.B``; the
                     Alpaca adapter translates both ways). Crypto pairs are
                     tickers too, spelled ``BASE/QUOTE``: ``BTC/USD``.
    - ``occ_symbol`` options, compact OCC form: root, YYMMDD, C or P, strike
                     x 1000 in eight digits: ``AAPL250117C00150000``.
    - ``isin``       fixed income.
* Timestamps are timezone-aware and normalised to UTC; JSON spells them
  ISO-8601 with a ``Z`` suffix. A naive datetime is refused, never assumed.
  Calendar dates that are not instants (a trading day, an expiration date, an
  ex-date) are ``date`` and carry no zone.
* Money that must be exact (account balances, orders, fills, P&L, cost basis)
  is ``DecimalStr``: a Decimal, serialised as a string, never built from a
  float. Market-data prices and analytics series are ``float``.
* Every numeric field states its unit in the JSON Schema as ``x-unit`` (see
  ``UNITS``). Percentages are fractions: 0.0125 means 1.25 %.
* ABSENCE: a value the source does not give is ``None``, never 0, and every
  ``None`` is explained, either per row in the row's ``absent`` map
  (field -> AbsenceCode) or for the whole response in the response-level
  ``absent`` map (field -> AbsenceReason). A real zero is 0.
* PROVENANCE: every response carries one ``Provenance`` block.

VERSIONING
----------
``SCHEMA_VERSION`` is semver. Adding an optional field or a new model is a
minor bump; removing or renaming a field, changing its type or its unit, or
tightening a pattern is a major bump. Every Provenance block names the schema
version the response was built with.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
)

SCHEMA_VERSION = "1.0.0"
#: Prefix of every built-in model's schema name ("marketlens.Bar"). A plugin's
#: models use the plugin name instead ("myplugin.Thing").
SCHEMA_NAMESPACE = "marketlens"

#: The unit vocabulary used in ``x-unit``. A field whose unit depends on the
#: row (volume of a stock vs a crypto pair) names the rule in its description
#: and uses the unit ``"by_asset_class"``.
UNITS: Mapping[str, str] = {
    "price": "price in the row's quote currency (the row's `currency`, USD unless stated)",
    "USD": "US dollars",
    "shares": "shares of stock",
    "contracts": "option contracts",
    "base_units": "units of the crypto pair's base asset",
    "by_asset_class": "shares (us_equity), contracts (us_option), base_units (crypto)",
    "fraction": "a ratio; 0.0125 means 1.25 %",
    "fraction_per_year": "an annualised ratio; 0.20 means 20 % per year",
    "percent_of_par": "bond price as a percent of par (101.5 means 101.5 % of par)",
    "count": "a count of events",
    "multiplier": "shares delivered per contract",
    "days": "calendar days",
    "UTC": "an instant in UTC",
    "per_share_greek": "option greek per share, provider convention (see field description)",
}


# --- scalar types --------------------------------------------------------------------------


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must carry a time zone; naive datetimes are refused")
    return value.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: An instant, normalised to UTC; JSON ``"2026-10-02T13:30:00Z"``.
UtcDatetime = Annotated[
    datetime,
    AfterValidator(_to_utc),
    PlainSerializer(_iso_z, return_type=str, when_used="json"),
]


def _no_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("exact money is never built from a float; pass a string or Decimal")
    return value


#: Exact decimal, serialised as a plain string ("1234.5600"), never via float.
DecimalStr = Annotated[
    Decimal,
    BeforeValidator(_no_float),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d+)?$"}),
]

EQUITY_TICKER_PATTERN = r"[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,5})?"
CRYPTO_PAIR_PATTERN = r"[A-Z0-9]{2,10}/[A-Z0-9]{2,10}"
TICKER_RE = re.compile(rf"^(?:{EQUITY_TICKER_PATTERN}|{CRYPTO_PAIR_PATTERN})$")
OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yymmdd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
#: Canonical bar size: minutes 1-59 ("5min"), hours 1-23 ("1h"), "1d", "1w",
#: months 1, 2, 3, 4, 6 or 12 ("3mo"). Adapters translate to the vendor's names.
TIMEFRAME_RE = re.compile(r"^(?:(?:[1-9]|[1-5][0-9])min|(?:[1-9]|1[0-9]|2[0-3])h|1d|1w|(?:1|2|3|4|6|12)mo)$")

Ticker = Annotated[str, StringConstraints(pattern=TICKER_RE.pattern)]
OccSymbol = Annotated[str, StringConstraints(pattern=OCC_RE.pattern)]
Isin = Annotated[str, StringConstraints(pattern=ISIN_RE.pattern)]
Timeframe = Annotated[str, StringConstraints(pattern=TIMEFRAME_RE.pattern)]


def normalize_ticker(raw: str) -> str:
    """User input -> canonical ticker: trimmed, upper case, and for an equity
    the share-class separator "." or "/" turned into "-" ("brk.b" -> "BRK-B").
    A crypto pair keeps its "/" ("btc/usd" -> "BTC/USD"); a string that is a
    pair by shape (two 2-10 character legs around "/", where the quote leg is
    a known quote currency) is treated as a pair. Does not validate: callers
    validate the result against ``TICKER_RE`` and refuse a mismatch by name.
    """
    s = raw.strip().upper()
    if "/" in s:
        base, _, quote = s.partition("/")
        if quote in CRYPTO_QUOTE_CURRENCIES and 2 <= len(base) <= 10:
            return f"{base}/{quote}"
        return s.replace("/", "-")
    return s.replace(".", "-")


#: Quote legs that make "X/Y" a crypto pair rather than a share class.
CRYPTO_QUOTE_CURRENCIES = frozenset({"USD", "USDT", "USDC", "BTC", "ETH", "EUR"})


# --- enums ---------------------------------------------------------------------------------


class AssetClass(StrEnum):
    US_EQUITY = "us_equity"
    US_OPTION = "us_option"
    CRYPTO = "crypto"
    FIXED_INCOME = "fixed_income"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionStyle(StrEnum):
    AMERICAN = "american"
    EUROPEAN = "european"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"
    CLS = "cls"
    IOC = "ioc"
    FOK = "fok"


class OrderClass(StrEnum):
    SIMPLE = "simple"
    BRACKET = "bracket"
    OCO = "oco"
    OTO = "oto"
    MLEG = "mleg"


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    DONE_FOR_DAY = "done_for_day"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    PENDING_NEW = "pending_new"
    ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
    STOPPED = "stopped"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    CALCULATED = "calculated"
    HELD = "held"


class Delay(StrEnum):
    """How current the data is, as the provider and feed define it."""

    REALTIME = "realtime"
    DELAYED = "delayed"
    END_OF_DAY = "end_of_day"
    UNKNOWN = "unknown"


class Environment(StrEnum):
    """Which brokerage account a portfolio read came from."""

    PAPER = "paper"
    LIVE = "live"


class AbsenceCode(StrEnum):
    """Why a value is None. Never served as 0."""

    NOT_PROVIDED_BY_SOURCE = "not_provided_by_source"  # the provider's payload has no value
    NOT_APPLICABLE = "not_applicable"  # meaningless for this row (strike of a stock)
    NOT_ENTITLED = "not_entitled"  # plan, feed or account not entitled
    NO_DATA = "no_data"  # no observation (no trade in window; a 0 price that means "no quote")
    INSUFFICIENT_DATA = "insufficient_data"  # too few observations to compute
    NOT_RECOVERED = "not_recovered"  # a drawdown not recovered by the last observation
    NO_MATCH = "no_match"  # an as-of join found nothing within tolerance
    WITHHELD = "withheld"  # deliberately not served (masked id, capability off)
    SOURCE_ERROR = "source_error"  # the provider failed for this part of the request
    UNPARSEABLE = "unparseable"  # the provider's value did not parse into the field type


# --- blocks every response carries ----------------------------------------------------------


class AbsenceReason(BaseModel):
    """The response-level explanation of a None: a code and, optionally, words."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: AbsenceCode
    detail: str | None = Field(default=None, max_length=300)


RequestValue = str | int | float | bool | list[str] | None


class Provenance(BaseModel):
    """Where a response came from and how current it is. One per response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(
        description='"alpaca", "alpaca-docs", "marketlens" (derived locally), or a plugin\'s provider name'
    )
    route: str = Field(
        description='Upstream route without host, e.g. "GET /v2/stocks/bars", or "duckdb:analytics_returns" for local work'
    )
    operation: str | None = Field(
        default=None, description="Upstream operation id where one exists (Alpaca operationId)"
    )
    dataset: str | None = Field(
        default=None, description="Upstream dataset or store name(s), comma-separated"
    )
    fetched_at: UtcDatetime = Field(description="When this server produced the response")
    as_of: UtcDatetime | None = Field(
        default=None,
        description="The point in time the data reflects: the requested end or cutoff, or the snapshot time",
    )
    knowledge_time: UtcDatetime | None = Field(
        default=None,
        description="When the source learned the newest value it served, where the source records it",
    )
    feed: str | None = Field(
        default=None,
        description='Provider feed, e.g. "iex", "sip", "delayed_sip", "opra", "indicative", crypto location "us"',
    )
    delay: Delay = Delay.UNKNOWN
    environment: Environment | None = Field(
        default=None, description="paper or live, on brokerage reads only"
    )
    request: dict[str, RequestValue] = Field(
        default_factory=dict, description="The normalised request parameters; never a secret"
    )
    pages_fetched: int = 0
    truncated: bool = False
    truncation_note: str | None = None
    stale: bool | None = None
    cached: bool = False
    authority: list[str] | None = None
    lag_days: float | None = None
    upstream_request_id: str | None = None
    derived_from: list[str] = Field(
        default_factory=list, description="Result ids this response was computed from"
    )
    schema_version: str = SCHEMA_VERSION


class PaginationState(BaseModel):
    """Where an upstream fetch stopped, and how to continue it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: bool
    pages_fetched: int
    rows_fetched: int
    next_page_token: str | None = None
    row_cap_hit: bool = False
    page_cap_hit: bool = False


# --- the base of every row model ----------------------------------------------------------------


class CanonicalModel(BaseModel):
    """Base of every row model. Subclasses set the ClassVars below.

    ``absent`` is the per-row explanation of None fields (field -> code). It is
    None when the row has no None field, or when every None in the row is
    explained by the response-level ``absent`` map.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: "marketlens.Bar". Unique across built-ins and plugins.
    schema_name: ClassVar[str] = ""
    #: The column analytics treat as time (an UtcDatetime field), if any.
    time_column: ClassVar[str | None] = None
    #: The column that identifies a series within a result (ticker, occ_symbol), if any.
    group_column: ClassVar[str | None] = None
    #: Numeric columns an analytics tool may default to, first is the default.
    value_columns: ClassVar[tuple[str, ...]] = ()

    absent: dict[str, AbsenceCode] | None = Field(
        default=None, description="Per-row reasons for None fields: field name -> AbsenceCode"
    )


def unit(u: str, **kwargs: Any) -> Any:
    """``Field(...)`` with ``x-unit`` in the JSON Schema. ``u`` must be a key of UNITS."""
    if u not in UNITS:
        raise ValueError(f"unknown unit {u!r}")
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra["x-unit"] = u
    return Field(json_schema_extra=extra, **kwargs)


def unexplained_absences(
    rows: Sequence[CanonicalModel], response_absent: Mapping[str, AbsenceReason]
) -> list[tuple[int, str]]:
    """Every (row index, field) whose value is None with no reason in the row's
    ``absent`` map nor in ``response_absent``. The absence rule, executable:
    every lane's tests assert this returns [] for every golden output."""
    out: list[tuple[int, str]] = []
    for i, row in enumerate(rows):
        own = row.absent or {}
        for name in type(row).model_fields:
            if name == "absent":
                continue
            if getattr(row, name) is None and name not in own and name not in response_absent:
                out.append((i, name))
    return out


def schema_names(models: Iterable[type[CanonicalModel]]) -> list[str]:
    """The schema names of ``models``, in order (helper for registries and docs)."""
    return [m.schema_name for m in models]
