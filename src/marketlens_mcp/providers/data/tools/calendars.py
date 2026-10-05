"""Market calendars (capability ``calendars``, off by default): Nasdaq's
economic and earnings calendars and earnings history, an unofficial endpoint
that may change or refuse without notice, and the US market holidays NYSE,
SIFMA and OPM publish."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import EarningsCalendarEntry, EarningsSurprise, EconomicEvent, MarketHoliday

from .. import frames as F
from .. import runtime, sources
from . import common
from .common import AsOf, Inputs, Ticker, Tickers

GOLDEN = "tests/data/test_calendars.py"
MAX_DAYS = 31
UNOFFICIAL = "Nasdaq's endpoint is unofficial: it may change or refuse without notice."


class EconomicInputs(Inputs):
    start: date | None = Field(default=None, description="First event date (Eastern). Default: today.")
    end: date | None = Field(
        default=None, description="Last event date. Default: today + 7. At most 31 days."
    )
    q: str | None = Field(
        default=None, max_length=80, description='Substring of the event name, e.g. "PMI", "CPI", "Nonfarm".'
    )
    as_of: AsOf | None = common.as_of_field()


class EarningsInputs(Inputs):
    start: date | None = Field(default=None, description="First report date. Default: today.")
    end: date | None = Field(
        default=None, description="Last report date. Default: today + 7. At most 31 days."
    )
    tickers: Annotated[list[Ticker], Field(min_length=1, max_length=200)] | None = Field(
        default=None, description="Only these companies."
    )
    as_of: AsOf | None = common.as_of_field()


class HistoryInputs(Inputs):
    tickers: Tickers = Field(description="Companies (1-20), SEC style.")
    as_of: AsOf | None = common.as_of_field()


class HolidaysInputs(Inputs):
    markets: list[Literal["stocks", "bonds", "federal"]] = Field(
        default_factory=lambda: ["stocks", "bonds", "federal"],
        min_length=1,
        description="stocks (NYSE), bonds (SIFMA), federal (OPM).",
    )
    start: date | None = Field(default=None, description="First date. Default: 1 January this year.")
    end: date | None = Field(default=None, description="Last date. Default: 31 December next year.")
    as_of: AsOf | None = common.as_of_field()


NA = AbsenceCode.NOT_APPLICABLE
ROUTES = {
    "calendar_economic": "omni nasdaq.economic_events <- api.nasdaq.com/api/calendar/economicevents (unofficial)",
    "calendar_earnings": "omni nasdaq.earnings <- api.nasdaq.com/api/calendar/earnings (unofficial)",
    "calendar_earnings_history": (
        "omni nasdaq.earnings_surprise <- api.nasdaq.com/api/company/{symbol}/earnings-surprise (unofficial)"
    ),
    "calendar_us_holidays": (
        "omni nyse.holidays, sifma.holidays, opm.federal_holidays <- nyse.com, sifma.org, opm.gov"
    ),
}
#: market -> (marketlens-data source, dataset), in the order rows are listed.
PUBLISHERS = {
    "stocks": ("nyse", "nyse.holidays"),
    "bonds": ("sifma", "sifma.holidays"),
    "federal": ("opm", "opm.federal_holidays"),
}


def _days(start, end) -> list:
    from datetime import timedelta

    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _captured(session: common.Session, outcomes: list) -> list[str]:
    """Notes for failed captures; D6 when every capture failed."""
    failed = [o for o in outcomes if o.status == "failed"]
    for o in outcomes:
        if o.status == "captured":
            session.fetched += 1
            session.fresh = False
    if outcomes and len(failed) == len(outcomes):
        raise ToolError(
            "data_unavailable",
            f"Nasdaq did not answer ({runtime.redact(failed[0].error or 'no reason given')}); try again later.",
            retryable=True,
        )
    return [f"The capture of {o.key} failed: {runtime.redact(o.error or 'no reason given')}" for o in failed]


def _removed_note(n: int) -> list[str]:
    if not n:
        return []
    noun = "event" if n == 1 else "events"
    return [
        f"{n} {noun} listed earlier {'was' if n == 1 else 'were'} removed from Nasdaq's calendar since (not shown)."
    ]


async def _capture_days(ctx: ToolContext, kind: str, dataset_id: str, column: str, start, end, as_of):
    s = runtime.settings_of(ctx.settings)
    days = _days(start, end)

    def work(session: common.Session):
        notes: list[str] = []
        if s.mode == "auto":
            calendars = session.module("omni.calendars")
            outcomes = [
                calendars.capture_day(getattr(calendars, kind), d, ttl_seconds=s.calendar_ttl_seconds)
                for d in days
            ]
            notes = _captured(session, outcomes)
        records = []
        for d in days:
            records.extend(F.records(session.read(dataset_id, as_of=as_of, **{column: d})))
        return notes, records

    (notes, records), session = await common.run(ctx, s, work, source="nasdaq")
    return s, session, notes, records


async def calendar_economic(ctx: ToolContext, args: EconomicInputs) -> ToolOutput:
    from datetime import timedelta

    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=today,
        default_end=today + timedelta(days=7),
        max_days=MAX_DAYS,
    )
    as_of = common.as_of_of(args.as_of)
    s, session, notes, records = await _capture_days(
        ctx, "ECONOMIC", "nasdaq.economic_events", "event_date", start, end, as_of
    )
    q = (args.q or "").strip().lower()
    rows, removed = [], 0
    for r in records:
        kind = F.text(r["record_type"])
        if kind == "removed":
            removed += 1
        if kind != "event" or (q and q not in (F.text(r["event_name"]) or "").lower()):
            continue
        rows.append(
            common.row(
                EconomicEvent,
                {
                    "event_date": F.day(r["event_date"]),
                    "release_at": F.instant(r["release_at"]),
                    "time_et": F.text(r["time_et"]),
                    "all_day": bool(F.flag(r["all_day"])),
                    "country": F.text(r["country"]),
                    "event_name": F.text(r["event_name"]) or "",
                    "actual": F.number(r["actual"]),
                    "consensus": F.number(r["consensus"]),
                    "previous": F.number(r["previous"]),
                    "actual_text": F.text(r["actual_text"]),
                    "consensus_text": F.text(r["consensus_text"]),
                    "previous_text": F.text(r["previous_text"]),
                    "value_kind": F.text(r["value_kind"]) or "none",
                    "description": F.text(r["description"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"release_at": NA, "time_et": NA},
            )
        )
    far = F.instant("9999-12-31T00:00:00Z")
    rows.sort(key=lambda r: (r.event_date, r.release_at or far, r.event_name))
    prov = common.provenance(
        ctx,
        provider="nasdaq",
        route=ROUTES[ctx.tool],
        datasets=("nasdaq.economic_events",),
        request={"start": start, "end": end, "q": args.q, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["third-party"],
    )
    return common.output(ctx, s, EconomicEvent, rows, prov, notes=notes + _removed_note(removed))


async def calendar_earnings(ctx: ToolContext, args: EarningsInputs) -> ToolOutput:
    from datetime import timedelta

    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=today,
        default_end=today + timedelta(days=7),
        max_days=MAX_DAYS,
    )
    as_of = common.as_of_of(args.as_of)
    s, session, notes, records = await _capture_days(
        ctx, "EARNINGS", "nasdaq.earnings", "report_date", start, end, as_of
    )
    tickers = set(args.tickers or [])
    rows, removed = [], 0
    for r in records:
        kind = F.text(r["record_type"])
        if kind == "removed":
            removed += 1
        if kind != "event" or (tickers and F.text(r["ticker"]) not in tickers):
            continue
        reported = F.text(r["status"]) == "reported"
        rows.append(
            common.row(
                EarningsCalendarEntry,
                {
                    "report_date": F.day(r["report_date"]),
                    "ticker": F.text(r["ticker"]) or "",
                    "name": F.text(r["name"]),
                    "time_of_day": F.text(r["time_of_day"]),
                    "market_cap": F.number(r["market_cap"]),
                    "fiscal_quarter": F.text(r["fiscal_quarter"]),
                    "eps_forecast": F.number(r["eps_forecast"]),
                    "eps_actual": F.number(r["eps_actual"]),
                    "last_year_eps": F.number(r["last_year_eps"]),
                    "estimates": F.integer(r["estimates"]),
                    "surprise_pct": F.fraction(r["surprise_pct"]),
                    "last_year_report_date": F.day(r["last_year_report_date"]),
                    "status": F.text(r["status"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "eps_actual": AbsenceCode.NO_DATA if reported else NA,
                    "surprise_pct": AbsenceCode.NO_DATA if reported else NA,
                    "last_year_eps": NA if reported else AbsenceCode.NO_DATA,
                    "last_year_report_date": NA if reported else AbsenceCode.NO_DATA,
                },
            )
        )
    rows.sort(key=lambda r: (r.report_date, r.ticker))
    prov = common.provenance(
        ctx,
        provider="nasdaq",
        route=ROUTES[ctx.tool],
        datasets=("nasdaq.earnings",),
        request={"start": start, "end": end, "tickers": args.tickers, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["third-party"],
    )
    return common.output(ctx, s, EarningsCalendarEntry, rows, prov, notes=notes + _removed_note(removed))


async def calendar_earnings_history(ctx: ToolContext, args: HistoryInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)

    def work(session: common.Session):
        notes: list[str] = []
        if s.mode == "auto":
            calendars = session.module("omni.calendars")
            outcomes = [
                calendars.capture_ticker_history(t, ttl_seconds=s.calendar_ttl_seconds) for t in args.tickers
            ]
            unknown = [
                o for o in outcomes if o.status == "failed" and (o.error or "").startswith("Nasdaq has no ")
            ]
            if outcomes and len(unknown) == len(outcomes):
                raise ToolError("data_not_found", f"{unknown[0].error}.")
            others = [o for o in outcomes if o not in unknown]
            notes = [runtime.redact(o.error or "") for o in unknown] + _captured(session, others)
        records = []
        for t in args.tickers:
            records.append((t, F.records(session.read("nasdaq.earnings_surprise", as_of=as_of, ticker=t))))
        return notes, records

    (notes, found), session = await common.run(ctx, s, work, source="nasdaq")
    rows = []
    for ticker, records in found:
        part = [
            common.row(
                EarningsSurprise,
                {
                    "ticker": ticker,
                    "fiscal_quarter": F.text(r["fiscal_quarter"]),
                    "report_date": F.day(r["report_date"]),
                    "eps_actual": F.number(r["eps_actual"]),
                    "eps_forecast": F.number(r["eps_forecast"]),
                    "surprise_pct": F.fraction(r["surprise_pct"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
            for r in records
            if F.text(r["record_type"]) == "event"
        ]
        part.sort(key=lambda r: r.fiscal_quarter, reverse=True)
        rows.extend(part)
    prov = common.provenance(
        ctx,
        provider="nasdaq",
        route=ROUTES[ctx.tool],
        datasets=("nasdaq.earnings_surprise",),
        request={"tickers": args.tickers, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["third-party"],
    )
    return common.output(ctx, s, EarningsSurprise, rows, prov, notes=notes)


async def calendar_us_holidays(ctx: ToolContext, args: HolidaysInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=date(today.year, 1, 1),
        default_end=date(today.year + 1, 12, 31),
    )
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    markets = [m for m in PUBLISHERS if m in set(args.markets)]

    def work(session: common.Session):
        authority = session.module("omni.sources.base").authority_of
        notes, failures = [], []
        for market in markets:
            source, dataset_id = PUBLISHERS[market]
            try:
                session.refresh(dataset_id, {})
            except Exception as exc:  # one publisher's page must not fail the others
                err = runtime.map_error(exc, source=source, tool=ctx.tool)
                failures.append(err)
                notes.append(
                    f"{sources.label(source)} could not be read ({err.message.rstrip('.')}); its stored rows, "
                    "if any, are shown."
                )
        if markets and len(failures) == len(markets):
            raise failures[0]
        found = [(market, F.records(session.read(PUBLISHERS[market][1], as_of=as_of))) for market in markets]
        return notes, found, [authority(PUBLISHERS[m][1]) for m in markets]

    (notes, found, labels), session = await common.run(ctx, s, work, source=PUBLISHERS[markets[0]][0])
    order = {m: i for i, m in enumerate(PUBLISHERS)}
    rows = []
    for market, records in found:
        for r in records:
            when = F.day(r["holiday_date"])
            if not F.within(when, start, end):
                continue
            early = F.text(r["status"]) == "early_close"
            rows.append(
                common.row(
                    MarketHoliday,
                    {
                        "publisher": F.text(r["source"]) or PUBLISHERS[market][0],
                        "market": market,
                        "holiday_date": when,
                        "name": F.text(r["name"]) or "",
                        "status": F.text(r["status"]),
                        "close_time_et": F.text(r["close_time_et"]),
                        "close_at": F.instant(r["close_at"]),
                        "note": F.text(r["note"]),
                        "crosscheck": F.text(r["crosscheck"]),
                        "crosscheck_note": F.text(r["crosscheck_note"]),
                        "knowledge_time": F.instant(r["knowledge_time"]),
                    },
                    codes={
                        "close_time_et": AbsenceCode.NO_DATA if early else NA,
                        "close_at": AbsenceCode.NO_DATA if early else NA,
                        "note": NA,
                        "crosscheck": NA,
                        "crosscheck_note": NA,
                    },
                )
            )
    rows.sort(key=lambda r: (r.holiday_date, order[r.market], r.name))
    prov = common.provenance(
        ctx,
        provider=",".join(PUBLISHERS[m][0] for m in markets),
        route=ROUTES[ctx.tool],
        datasets=tuple(PUBLISHERS[m][1] for m in markets),
        request={"markets": markets, "start": start, "end": end, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=labels,
    )
    return common.output(ctx, s, MarketHoliday, rows, prov, notes=notes)


HANDLERS = {
    "calendar_economic": calendar_economic,
    "calendar_earnings": calendar_earnings,
    "calendar_earnings_history": calendar_earnings_history,
    "calendar_us_holidays": calendar_us_holidays,
}


def _spec(name, title, description, readme, inputs, model, provider, route, *, env=(), risk="api_structured"):
    return ToolSpec(
        name=name,
        capability="calendars",
        title=title,
        description=description,
        readme=readme,
        input_model=inputs,
        output_model=model,
        provider=provider,
        route=route,
        handler=HANDLERS[name],
        golden_test=GOLDEN,
        env=env,
        output_risk=risk,
    )


SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "calendar_economic",
        "Economic calendar",
        common.describe(
            "Nasdaq's US economic calendar: release time, actual, consensus and previous as displayed (text "
            "and number; percent stays percent, see value_kind) and a description (vendor text: untrusted). "
            "PMI actual and consensus figures appear here; there is no free official PMI source. Filter with "
            f"q. At most 31 days per call. {UNOFFICIAL} Keyless.",
            "snapshot",
            stored=False,
        ),
        "Economic calendar with consensus (PMI, CPI, payrolls, ...)",
        EconomicInputs,
        EconomicEvent,
        "nasdaq",
        "omni nasdaq.economic_events <- api.nasdaq.com/api/calendar/economicevents (unofficial)",
        risk="external_text",
    ),
    _spec(
        "calendar_earnings",
        "Earnings calendar",
        common.describe(
            "Nasdaq's earnings calendar per report date: time of day, market cap in USD, consensus EPS and "
            "number of estimates; EPS and surprise (fraction) once reported. At most 31 days per call. "
            f"{UNOFFICIAL} Keyless.",
            "snapshot",
        ),
        "Earnings dates with consensus EPS",
        EarningsInputs,
        EarningsCalendarEntry,
        "nasdaq",
        "omni nasdaq.earnings <- api.nasdaq.com/api/calendar/earnings (unofficial)",
    ),
    _spec(
        "calendar_earnings_history",
        "Earnings history",
        common.describe(
            "A company's recent quarters from Nasdaq: reported EPS against consensus and the surprise "
            f"(fraction). {UNOFFICIAL} Keyless.",
            "snapshot",
            stored=False,
        ),
        "Recent EPS against consensus per company",
        HistoryInputs,
        EarningsSurprise,
        "nasdaq",
        "omni nasdaq.earnings_surprise <- api.nasdaq.com/api/company/{symbol}/earnings-surprise (unofficial)",
    ),
    _spec(
        "calendar_us_holidays",
        "US market holidays",
        common.describe(
            "US market closures and early closes as their publishers state them: stocks from NYSE (each row "
            "cross-checked against Alpaca's trading calendar when ALPACA_API_KEY and ALPACA_SECRET_KEY are "
            "set), bonds from SIFMA's recommendations, federal holidays from OPM. Early closes carry the "
            "Eastern time and the UTC instant.",
            "forward_known",
            stored=False,
        ),
        "NYSE, SIFMA and OPM holidays and early closes",
        HolidaysInputs,
        MarketHoliday,
        "nyse,sifma,opm",
        "omni nyse.holidays, sifma.holidays, opm.federal_holidays <- nyse.com, sifma.org, opm.gov",
        env=("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "calendar_economic": ("nasdaq.economic_events",),
    "calendar_earnings": ("nasdaq.earnings",),
    "calendar_earnings_history": ("nasdaq.earnings_surprise",),
    "calendar_us_holidays": ("nyse.holidays", "sifma.holidays", "opm.federal_holidays"),
}
