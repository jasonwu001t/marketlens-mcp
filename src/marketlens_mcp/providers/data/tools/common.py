"""Shared pieces of the data tools: input types, date windows, the
fetch-then-read helpers, provenance and the output builder.

The fetch rule (mode ``auto``): a slice of a dataset is named by its
*identity* (the fetch params a tool passes, e.g. ``{"series_id": "CPIAUCSL"}``).
When the store's last sync of that identity is older than the TTL it is
fetched and stored (``omni.sync``), exactly what marketlens-data's own ``auto``
mode does; every answer is then read from the store, point in time, with
``as_of`` passed through. Mode ``local`` reads only what is stored.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput
from marketlens_schema import (
    AbsenceCode,
    AbsenceReason,
    CanonicalModel,
    Delay,
    PaginationState,
    Provenance,
    normalize_ticker,
)

from .. import runtime, sources

STORED_NOTE = "Large results are stored, not shown: you get a result_id to query with results_query."
AS_OF_DOC = (
    "Point in time: answer with what was knowable then. An ISO date means 00:00 UTC that day; give a "
    "datetime with a zone for intraday precision, e.g. 2024-03-01T13:30:00Z. Default: now."
)
LOCAL_EMPTY = (
    "mode is local and nothing is stored for this request; set providers.data.mode: auto to fetch it"
)
ROW_CAP = "Stopped at {n} rows (fetch.max_rows); narrow the request (fewer series, a shorter window, or metrics=...)."

#: The PIT line each tool's description carries, by marketlens-data policy.
PIT = {
    "vintage": "Point in time: exact; as_of returns the values as published at that time.",
    "lagged": (
        "Point in time: rows not yet knowable at as_of are hidden (release lag or acceptance); a later "
        "revision shows the newest the store held at as_of, tracked only from the first fetch."
    ),
    "at_event": "Point in time: exact EDGAR acceptance instants.",
    "forward_known": (
        "Point in time: known from the capture; an as_of before the first capture returns nothing."
    ),
    "snapshot": "Point in time: the captured state at as_of; history exists only from the first capture.",
}


class Inputs(BaseModel):
    """Base of every data tool's input model: unknown arguments are refused."""

    model_config = ConfigDict(extra="forbid")


# --- input types -------------------------------------------------------------------------------

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEC_TICKER = re.compile(r"^[A-Z][A-Z0-9-]{0,9}$")


def parse_instant(value: str) -> datetime:
    """A date means 00:00 UTC that day; a datetime must carry a zone."""
    s = value.strip()
    if _DATE_ONLY.match(s):
        return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO date or datetime") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("a datetime needs a zone, e.g. 2024-03-01T13:30:00Z")
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _check_as_of(value: str) -> str:
    return iso_z(parse_instant(value))


def _sec_ticker(value: str) -> str:
    t = normalize_ticker(value).replace("/", "-")
    if not _SEC_TICKER.match(t):
        raise ValueError(f"{value!r} is not a stock or fund ticker (SEC style, e.g. AAPL or BRK-B)")
    return t


def _sec_tickers(values: list[str]) -> list[str]:
    return list(dict.fromkeys(_sec_ticker(v) for v in values))


AsOf = Annotated[str, AfterValidator(_check_as_of)]
Ticker = Annotated[str, Field(max_length=20), AfterValidator(_sec_ticker)]
Tickers = Annotated[list[str], Field(min_length=1, max_length=20), AfterValidator(_sec_tickers)]


def as_of_field() -> Any:
    return Field(default=None, description=AS_OF_DOC)


def as_of_of(value: str | None) -> datetime | None:
    return parse_instant(value) if value else None


# --- windows -----------------------------------------------------------------------------------


def window(
    tool: str,
    start: date | None,
    end: date | None,
    *,
    default_start: date,
    default_end: date,
    max_days: int | None = None,
) -> tuple[date, date]:
    """Inclusive [start, end]; refuses start > end, and D9 past ``max_days``."""
    s, e = start or default_start, end or default_end
    if s > e:
        raise ToolError(
            "invalid_arguments", f"start ({s.isoformat()}) must not be after end ({e.isoformat()})."
        )
    if max_days is not None and (e - s).days > max_days:
        raise ToolError(
            "data_window",
            f"{tool} accepts at most {max_days} days between start and end (got {(e - s).days}).",
        )
    return s, e


def years_ago(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def check_sec_contact(settings: runtime.Settings, env: Mapping[str, str]) -> None:
    """D3: SEC EDGAR's fair-access rule asks every client to name a contact."""
    if settings.mode == "auto" and not sources.is_email(env.get("SEC_CONTACT_EMAIL")):
        raise ToolError(
            "sec_contact_missing",
            "SEC EDGAR asks every client to name a contact email in its User-Agent: set SEC_CONTACT_EMAIL "
            "(for example you@example.com) in this server's environment.",
        )


# --- fetch, then read --------------------------------------------------------------------------


class Session:
    """What one tool call did against the store (on the worker thread)."""

    def __init__(self, omni: Any, settings: runtime.Settings):
        self.omni = omni
        self.settings = settings
        self.fetched = 0
        self.fresh = True

    @property
    def store(self) -> Any:
        return self.module("omni.store").get_store()

    @staticmethod
    def module(name: str) -> Any:
        """A marketlens-data module (``omni.query`` names a function, so import by name)."""
        import importlib

        return importlib.import_module(name)

    def refresh(
        self, dataset_id: str, params: Mapping[str, Any], ttl: int | None = None, *, force: bool = False
    ) -> bool:
        """Fetch the slice when its last sync is older than the TTL (mode auto),
        or always with ``force`` (a dataset built from another one just fetched).
        Returns True when nothing had to be fetched."""
        if self.settings.mode == "local":
            return True
        if not force and self.store.is_fresh(dataset_id, dict(params), ttl or self.settings.ttl_seconds):
            return True
        self.omni.sync(dataset_id, **params)
        self.fetched += 1
        self.fresh = False
        return False

    def read(self, dataset_id: str, *, as_of: datetime | None, vintages: bool = False, **filters: Any):
        """The stored rows, point in time (latest vintage per key at as_of), or
        every stored vintage knowable at as_of when ``vintages``."""
        if vintages:
            return self.omni.read_raw(dataset_id, as_of=as_of, **filters)
        return self.omni.query(dataset_id, as_of=as_of, mode="local", **filters)


# --- rows, provenance, output ------------------------------------------------------------------


def row(
    model: type[CanonicalModel],
    values: Mapping[str, Any],
    *,
    codes: Mapping[str, AbsenceCode | str] | None = None,
    explained: Collection[str] = (),
) -> CanonicalModel:
    """A canonical row whose every None is explained: fields in ``explained``
    by the response-level map, the rest per row (``codes``, else no_data)."""
    absent = {
        name: AbsenceCode((codes or {}).get(name, AbsenceCode.NO_DATA))
        for name, value in values.items()
        if value is None and name not in explained
    }
    return model(**values, absent=absent or None)


def reasons(**codes: tuple[AbsenceCode | str, str | None]) -> dict[str, AbsenceReason]:
    return {
        name: AbsenceReason(code=AbsenceCode(code), detail=detail) for name, (code, detail) in codes.items()
    }


def request_doc(values: Mapping[str, Any]) -> dict[str, Any]:
    """Normalised inputs as Provenance.request accepts them (dates ISO, lists of strings)."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, (date, datetime)):
            out[key] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            out[key] = [str(v) for v in value]
        else:
            out[key] = value
    return out


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    return None


def provenance(
    ctx: ToolContext,
    *,
    provider: str,
    route: str,
    datasets: Sequence[str],
    request: Mapping[str, Any],
    as_of: datetime | None,
    rows: Sequence[CanonicalModel],
    session: Session | None,
    authority: Iterable[str | None] = (),
) -> Provenance:
    model = type(rows[0]) if rows else None
    knowledge = max((r.knowledge_time for r in rows if getattr(r, "knowledge_time", None)), default=None)
    newest = None
    if model is not None and model.time_column:
        days = [d for d in (_as_date(getattr(r, model.time_column)) for r in rows) if d is not None]
        newest = max(days, default=None)
    reference = (as_of or ctx.now()).astimezone(UTC).date()
    labels = list(dict.fromkeys(a for a in authority if a))
    return Provenance(
        provider=provider,
        route=route,
        dataset=",".join(datasets) or None,
        fetched_at=ctx.now(),
        as_of=as_of,
        knowledge_time=knowledge,
        delay=Delay.UNKNOWN,
        request=request_doc(request),
        pages_fetched=session.fetched if session else 0,
        cached=session.fresh if session else False,
        authority=labels or None,
        lag_days=float((reference - newest).days) if newest is not None else None,
    )


def output(
    ctx: ToolContext,
    settings: runtime.Settings,
    model: type[CanonicalModel],
    rows: list[CanonicalModel],
    prov: Provenance,
    *,
    absent: Mapping[str, AbsenceReason] | None = None,
    notes: Iterable[str] = (),
    risk: Any = None,
) -> ToolOutput:
    """Rows in the tool's order, cut at fetch.max_rows with the row-cap note."""
    notes = list(notes)
    pagination = None
    cap = ctx.limits.max_rows
    if len(rows) > cap:
        rows = rows[:cap]
        note = ROW_CAP.format(n=cap)
        notes.append(note)
        pagination = PaginationState(
            complete=False,
            pages_fetched=prov.pages_fetched,
            rows_fetched=cap,
            next_page_token=None,
            row_cap_hit=True,
        )
        prov = prov.model_copy(update={"truncated": True, "truncation_note": note})
    if not rows and settings.mode == "local":
        notes.append(LOCAL_EMPTY)
    return ToolOutput(
        model=model,
        provenance=prov,
        rows=rows,
        absent=dict(absent or {}),
        pagination=pagination,
        notes=notes,
        risk=risk,
    )


def describe(text: str, pit: str, *, stored: bool = True) -> str:
    return " ".join(p for p in (text, PIT[pit], STORED_NOTE if stored else "") if p)


Work = Callable[[Session], Any]


async def run(
    ctx: ToolContext,
    settings: runtime.Settings,
    work: Work,
    *,
    source: str,
    subject: tuple[str, str] | None = None,
) -> Any:
    """``work(session)`` on the worker thread; returns (result, session)."""

    def job(omni: Any) -> Any:
        session = Session(omni, settings)
        return work(session), session

    return await runtime.call(settings, job, source=source, tool=ctx.tool, subject=subject)
