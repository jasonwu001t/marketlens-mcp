"""Shared pieces of the Alpaca tools: input field types, time windows, feeds,
provenance and output building."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from marketlens_mcp.plugin_api import Handler, PageResult, ToolContext, ToolError, ToolOutput, ToolSpec
from marketlens_schema.base import (
    ISIN_RE,
    TIMEFRAME_RE,
    AbsenceReason,
    CanonicalModel,
    Delay,
    Environment,
    Provenance,
)

from .. import convert

ALPACA_ENV = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
SKIP_SAMPLE = 5


class Inputs(BaseModel):
    """Base of every Alpaca tool's input model: unknown arguments are refused."""

    model_config = ConfigDict(extra="forbid")


def _check_instant(v: str) -> str:
    convert.parse_instant(v)
    return v.strip()


def _check_duration(v: str) -> str:
    convert.parse_duration(v)
    return v.strip().upper()


def _check_isins(v: list[str]) -> list[str]:
    out = []
    for item in v:
        s = item.strip().upper()
        if not ISIN_RE.match(s):
            raise ValueError(f"'{item}' is not an ISIN (12 characters, e.g. US912797SX61)")
        out.append(s)
    return out


def _one_equity(v: str) -> str:
    return convert.equity_tickers([v])[0]


def _one_ticker_or_pair(v: str) -> str:
    return convert.crypto_pairs([v])[0] if "/" in v else convert.equity_tickers([v])[0]


def _one_occ(v: str) -> str:
    return convert.occ_symbols([v])[0]


#: One stock ticker, normalised (brk.b -> BRK-B).
EquityTicker = Annotated[str, Field(max_length=20), AfterValidator(_one_equity)]
#: One stock ticker or crypto pair.
TickerOrPair = Annotated[str, Field(max_length=20), AfterValidator(_one_ticker_or_pair)]
#: One OCC option symbol.
OccSymbol = Annotated[str, Field(max_length=21), AfterValidator(_one_occ)]

EquityTickers = Annotated[
    list[str],
    Field(min_length=1, max_length=200, description='Stock tickers, e.g. ["AAPL", "BRK-B"] (1-200).'),
    AfterValidator(convert.equity_tickers),
]
CryptoPairs = Annotated[
    list[str],
    Field(min_length=1, max_length=200, description='Crypto pairs as BASE/QUOTE, e.g. ["BTC/USD"] (1-200).'),
    AfterValidator(convert.crypto_pairs),
]
OccSymbols = Annotated[
    list[str],
    Field(
        min_length=1, max_length=100, description='OCC option symbols, e.g. ["AAPL250117C00150000"] (1-100).'
    ),
    AfterValidator(convert.occ_symbols),
]
Isins = Annotated[
    list[str], Field(min_length=1, max_length=100, description="ISINs (1-100)."), AfterValidator(_check_isins)
]
Instant = Annotated[str, AfterValidator(_check_instant)]
SORT_DOC = "Time order of the rows: asc (oldest first, default) or desc (newest first)."
Duration = Annotated[str, AfterValidator(_check_duration)]
TimeframeIn = Annotated[str, Field(pattern=TIMEFRAME_RE.pattern)]

START_DOC = "Window start: ISO date (00:00 UTC) or datetime with a zone, e.g. 2026-01-02T14:30:00Z."
END_DOC = "Window end (same format). Default: now (Alpaca's latest available)."
LOOKBACK_DOC = (
    "Window length back from end as an ISO-8601 duration (P5D, P1Y, PT20M); only when start is omitted. "
    "Bars of 1d or longer start at 00:00 UTC of the first day."
)
CONTINUE_DOC = "Continue a truncated fetch: the page_token from the previous response's pagination."
TIMEFRAME_DOC = "Bar size: Nmin (1-59), Nh (1-23), 1d, 1w or Nmo (1,2,3,4,6,12)."
STORED_NOTE = "Large results are stored, not shown: you get a result_id to query with results_query."

# --- time windows ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime | None
    lookback: str | None

    def fields(self) -> dict[str, Any]:
        return {"start": convert.iso_z(self.start), "end": convert.iso_z(self.end), "lookback": self.lookback}


def window(
    ctx: ToolContext,
    start: str | None,
    end: str | None,
    lookback: str | None,
    default: str,
    *,
    timeframe: str | None = None,
) -> Window:
    """With a timeframe of a day or longer, a start from lookback is floored to
    00:00 UTC so the first day's bar (stamped at midnight New York, 04:00 or
    05:00 UTC) is in the window."""
    end_dt = convert.parse_instant(end) if end else None
    if start and lookback:
        raise ToolError("invalid_arguments", "Pass start or lookback, not both.")
    if start:
        start_dt, lb = convert.parse_instant(start), None
    else:
        lb = lookback or default
        try:
            start_dt = (end_dt or ctx.now()) - convert.parse_duration(lb)
        except OverflowError:
            raise ToolError(
                "invalid_arguments", f"lookback {lb} reaches back before the year 1; pass a shorter lookback."
            ) from None
        if timeframe is not None and timeframe.endswith(("d", "w", "mo")):
            start_dt = start_dt.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if end_dt is not None and start_dt >= end_dt:
        raise ToolError("invalid_arguments", f"start ({convert.iso_z(start_dt)}) must be before end ({end}).")
    return Window(start_dt, end_dt, lb)


# --- feeds -----------------------------------------------------------------------------------

_DELAY = {
    "sip": Delay.REALTIME,
    "iex": Delay.REALTIME,
    "boats": Delay.REALTIME,
    "overnight": Delay.REALTIME,
    "otc": Delay.REALTIME,
    "opra": Delay.REALTIME,
    "delayed_sip": Delay.DELAYED,
    "indicative": Delay.DELAYED,
}
#: Historical stock endpoints accept iex, otc, sip and boats only.
_HISTORICAL_STOCK_FEED = {"delayed_sip": "sip", "overnight": "boats"}


def stock_feed(configured: str, *, historical: bool) -> tuple[str, Delay]:
    sent = _HISTORICAL_STOCK_FEED.get(configured, configured) if historical else configured
    return sent, _DELAY.get(configured, Delay.UNKNOWN)


def option_feed(configured: str) -> tuple[str, Delay]:
    return configured, _DELAY.get(configured, Delay.UNKNOWN)


# --- rows, provenance, output ----------------------------------------------------------------


@dataclass
class Skips:
    """Upstream records that did not fit the canonical model, reported in notes."""

    model: str
    labels: list[str] = field(default_factory=list)

    def note(self) -> list[str]:
        if not self.labels:
            return []
        sample = ", ".join(self.labels[:SKIP_SAMPLE])
        return [f"Skipped {len(self.labels)} upstream record(s) that do not fit {self.model}: {sample}."]


def collect(
    skips: Skips, items: Iterable[Any], mapper: Callable[[Any], Any], label: Callable[[Any], str]
) -> list:
    """Map raw items to rows; a mapper may return None (dropped on purpose), a
    row, or a list of rows. Records that fail validation are skipped and noted."""
    rows: list = []
    for item in items:
        try:
            out = mapper(item)
        except (ValidationError, ValueError, TypeError, KeyError, ArithmeticError):
            skips.labels.append(label(item))
            continue
        if out is None:
            continue
        rows.extend(out if isinstance(out, list) else [out])
    return rows


def request_of(args: BaseModel, **extra: Any) -> dict[str, Any]:
    req = {k: v for k, v in args.model_dump(mode="json").items() if v is not None}
    req.update({k: v for k, v in extra.items() if v is not None})
    return req


def truncation_note(ctx: ToolContext, page: PageResult) -> str | None:
    if not (page.row_cap_hit or page.page_cap_hit):
        return None
    lim = ctx.limits
    if page.next_page_token is None:
        return (
            f"Stopped after {len(page.rows)} rows (limit fetch.max_rows={lim.max_rows}). The data is incomplete "
            f"and this endpoint cannot be continued; narrow the request to see the rest."
        )
    return (
        f"Stopped after {page.pages_fetched} pages and {len(page.rows)} rows (limits fetch.max_pages="
        f"{lim.max_pages}, fetch.max_rows={lim.max_rows}). The data is incomplete; call {ctx.tool} again with "
        f'page_token="{page.next_page_token}" to continue.'
    )


def output(
    ctx: ToolContext,
    model: type[CanonicalModel],
    rows: list,
    *,
    route: str,
    operation: str | None,
    request: Mapping[str, Any],
    provider: str = "alpaca",
    feed: str | None = None,
    delay: Delay = Delay.UNKNOWN,
    environment: Environment | None = None,
    as_of: datetime | None = None,
    page: PageResult | None = None,
    absent: dict[str, AbsenceReason] | None = None,
    notes: list[str] | None = None,
    skips: Skips | None = None,
) -> ToolOutput:
    notes = list(notes or [])
    note = truncation_note(ctx, page) if page is not None else None
    if note:
        notes.insert(0, note)
    if skips is not None:
        notes.extend(skips.note())
    prov = Provenance(
        provider=provider,
        route=route,
        operation=operation,
        fetched_at=ctx.now(),
        as_of=as_of,
        feed=feed,
        delay=delay,
        environment=environment,
        request=dict(request),
        pages_fetched=page.pages_fetched if page is not None else 1,
        truncated=bool(note),
        truncation_note=note,
    )
    return ToolOutput(
        model=model,
        provenance=prov,
        rows=rows,
        absent=dict(absent or {}),
        pagination=page.state() if page is not None else None,
        notes=notes,
    )


def latest_t(rows: Iterable[Any], attr: str = "t") -> datetime | None:
    times = [getattr(r, attr) for r in rows if getattr(r, attr, None) is not None]
    return max(times) if times else None


def absence(code: str, detail: str) -> AbsenceReason:
    return AbsenceReason(code=code, detail=detail)


def missing_note(requested: Iterable[str], rows: Iterable[Any], attr: str = "ticker") -> list[str]:
    """A note naming requested instruments that produced no row."""
    got = {getattr(r, attr) for r in rows}
    missing = [x for x in requested if x not in got]
    return [f"No data from Alpaca for: {', '.join(missing)}."] if missing else []


def spec(
    *,
    name: str,
    capability: str,
    title: str,
    description: str,
    readme: str,
    input_model: type[BaseModel],
    output_model: type[CanonicalModel],
    route: str,
    handler: Handler,
    golden_test: str,
    operations: tuple[str, ...],
    parity: tuple[str, ...],
    provider: str = "alpaca",
    risk: Literal["api_structured", "external_text"] = "api_structured",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        capability=capability,
        title=title,
        description=description,
        readme=readme,
        input_model=input_model,
        output_model=output_model,
        provider=provider,
        route=route,
        handler=handler,
        golden_test=golden_test,
        upstream_operations=operations,
        parity_names=parity,
        env=ALPACA_ENV if provider == "alpaca" else (),
        output_risk=risk,
    )
