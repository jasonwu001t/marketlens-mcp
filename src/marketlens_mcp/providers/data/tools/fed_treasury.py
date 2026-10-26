"""The Federal Reserve and the Treasury: FOMC meetings, statements, the
Summary of Economic Projections (with the dot plot), minutes and the Beige
Book, NY Fed reference rates, the Treasury par yield curve, auctions, debt and
the cash balance (capability ``fed_treasury``). Every source here is keyless.

The FOMC documents are fetched for the window asked for: a store holding no
document near the window's start is synced from that start (marketlens-data
reads only the pages its index links after it), then incrementally from the
newest stored one, so a first call does not read every page since 2012."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import (
    BeigeBookSection,
    FomcMeeting,
    FomcMinutes,
    FomcProjection,
    FomcStatement,
    ReferenceRate,
    TreasuryAuction,
    TreasuryCashBalance,
    TreasuryDebt,
    YieldCurvePoint,
)

from .. import frames as F
from .. import runtime
from . import common
from .common import AsOf, Inputs

GOLDEN = "tests/data/test_fed_treasury.py"
CURVE_MAX_DAYS = 3660
RateType = Literal["SOFR", "EFFR", "OBFR", "TGCR", "BGCR"]
Tenor = Literal["1M", "1.5M", "2M", "3M", "4M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
SecurityType = Literal["Bill", "Note", "Bond", "TIPS", "FRN", "CMB"]


class MeetingsInputs(Inputs):
    start: date | None = Field(default=None, description="First meeting start. Default: 1 January this year.")
    end: date | None = Field(default=None, description="Last meeting start. Default: 31 December next year.")
    as_of: AsOf | None = common.as_of_field()


class StatementsInputs(Inputs):
    start: date | None = Field(default=None, description="First meeting date. Default: 2 years ago.")
    end: date | None = Field(default=None, description="Last meeting date. Default: today.")
    include_text: bool = Field(default=True, description="false: dates and links only.")
    as_of: AsOf | None = common.as_of_field()


SepVariable = Literal[
    "gdp_growth", "unemployment_rate", "pce_inflation", "core_pce_inflation", "fed_funds_rate"
]
District = Literal[
    "national_summary",
    "boston",
    "new_york",
    "philadelphia",
    "cleveland",
    "richmond",
    "atlanta",
    "chicago",
    "st_louis",
    "minneapolis",
    "kansas_city",
    "dallas",
    "san_francisco",
]


class ProjectionsInputs(Inputs):
    start: date | None = Field(default=None, description="First meeting date. Default: 1 January last year.")
    end: date | None = Field(default=None, description="Last meeting date. Default: today.")
    kind: Literal["summary", "dots", "all"] = Field(
        default="all", description="summary: Table 1 (median, central tendency, range); dots: the dot plot."
    )
    variables: list[SepVariable] | None = Field(default=None, description="Only these. Default: all five.")
    as_of: AsOf | None = common.as_of_field()


class MinutesInputs(Inputs):
    start: date | None = Field(default=None, description="First meeting date. Default: 1 January last year.")
    end: date | None = Field(default=None, description="Last meeting date. Default: today.")
    include_text: bool = Field(default=True, description="false: dates and links only.")
    as_of: AsOf | None = common.as_of_field()


class BeigeBookInputs(Inputs):
    start: date | None = Field(default=None, description="First release date. Default: 60 days ago.")
    end: date | None = Field(default=None, description="Last release date. Default: today.")
    districts: list[District] | None = Field(
        default=None, description="Only these (national_summary and the twelve Reserve Banks). Default: all."
    )
    include_text: bool = Field(default=True, description="false: dates and links only.")
    as_of: AsOf | None = common.as_of_field()


class RatesInputs(Inputs):
    rate_types: list[RateType] = Field(
        default_factory=lambda: ["SOFR", "EFFR", "OBFR", "TGCR", "BGCR"], min_length=1
    )
    start: date | None = Field(default=None, description="First effective date. Default: a year ago.")
    end: date | None = Field(default=None, description="Last effective date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class CurveInputs(Inputs):
    start: date | None = Field(default=None, description="First date. Default: 30 days ago.")
    end: date | None = Field(default=None, description="Last date. Default: today.")
    tenors: list[Tenor] | None = Field(default=None, description="Only these tenors. Default: all.")
    as_of: AsOf | None = common.as_of_field()


class AuctionsInputs(Inputs):
    start: date | None = Field(default=None, description="First auction date. Default: a year ago.")
    end: date | None = Field(default=None, description="Last auction date. Default: today.")
    security_types: list[SecurityType] | None = Field(default=None, description="Only these. Default: all.")
    as_of: AsOf | None = common.as_of_field()


class DailyInputs(Inputs):
    start: date | None = Field(default=None, description="First date. Default: a year ago.")
    end: date | None = Field(default=None, description="Last date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


NA = AbsenceCode.NOT_APPLICABLE
WITHHELD = AbsenceCode.WITHHELD
RATE_ORDER = ("SOFR", "EFFR", "OBFR", "TGCR", "BGCR")
TENOR_ORDER = ("1M", "1.5M", "2M", "3M", "4M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y")
ROUTES = {
    "fed_fomc_meetings": "omni fomc.meetings <- www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "fed_fomc_statements": (
        "omni fomc.statement <- www.federalreserve.gov (statements linked from the FOMC calendar)"
    ),
    "fed_fomc_projections": (
        "omni fomc.projections <- www.federalreserve.gov (SEP pages linked from the FOMC calendar)"
    ),
    "fed_fomc_minutes": (
        "omni fomc.minutes <- www.federalreserve.gov (minutes linked from the FOMC calendar and year pages)"
    ),
    "fed_beige_book": (
        "omni fomc.beige_book <- www.federalreserve.gov (editions linked from the Beige Book pages)"
    ),
    "fed_reference_rates": "omni nyfed.reference_rates <- markets.newyorkfed.org/api/rates/all/search.json",
    "treasury_yield_curve": "omni treasury.yield_curve <- home.treasury.gov daily-treasury-rates.csv",
    "treasury_auctions": "omni fiscaldata.auctions <- api.fiscaldata.treasury.gov/v1/accounting/od/auctions_query",
    "treasury_debt": (
        "omni fiscaldata.debt_to_penny <- api.fiscaldata.treasury.gov/v2/accounting/od/debt_to_penny"
    ),
    "treasury_tga": (
        "omni fiscaldata.tga_balance <- api.fiscaldata.treasury.gov/v1/accounting/dts/operating_cash_balance"
    ),
}


async def _simple(ctx: ToolContext, dataset_id: str, source: str, as_of_text, identities=({},)):
    """Refresh the identities, then read the whole dataset (point in time)."""
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(as_of_text)

    def work(session: common.Session):
        for identity in identities:
            session.refresh(dataset_id, identity)
        return F.records(session.read(dataset_id, as_of=as_of))

    records, session = await common.run(ctx, s, work, source=source)
    return s, as_of, records, session


def _prov(ctx, tool, provider, dataset_id, request, as_of, rows, session):
    return common.provenance(
        ctx,
        provider=provider,
        route=ROUTES[tool],
        datasets=(dataset_id,),
        request=request,
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )


async def fed_fomc_meetings(ctx: ToolContext, args: MeetingsInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=date(today.year, 1, 1),
        default_end=date(today.year + 1, 12, 31),
    )
    s, as_of, records, session = await _simple(ctx, "fomc.meetings", "fomc", args.as_of)
    rows = [
        common.row(
            FomcMeeting,
            {
                "start_date": F.day(r["start_date"]),
                "end_date": F.day(r["end_date"]),
                "year": F.integer(r["year"]),
                "has_projection": bool(F.flag(r["has_projection"])),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
        )
        for r in records
        if F.within(F.day(r["start_date"]), start, end)
    ]
    rows.sort(key=lambda r: r.start_date)
    prov = _prov(
        ctx,
        "fed_fomc_meetings",
        "fomc",
        "fomc.meetings",
        {"start": start, "end": end, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, FomcMeeting, rows, prov)


async def fed_fomc_statements(ctx: ToolContext, args: StatementsInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 2), default_end=today
    )
    s, as_of, records, session = await _simple(ctx, "fomc.statement", "fomc", args.as_of)
    rows = [
        common.row(
            FomcStatement,
            {
                "meeting_date": F.day(r["meeting_date"]),
                "url": F.text(r["url"]),
                "text": F.text(r["text"]) if args.include_text else None,
                "n_paragraphs": F.integer(r["n_paragraphs"]) or 0,
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
            codes={"text": AbsenceCode.NO_DATA if args.include_text else WITHHELD},
        )
        for r in records
        if F.within(F.day(r["meeting_date"]), start, end)
    ]
    rows.sort(key=lambda r: r.meeting_date, reverse=True)
    prov = _prov(
        ctx,
        "fed_fomc_statements",
        "fomc",
        "fomc.statement",
        {"start": start, "end": end, "include_text": args.include_text, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, FomcStatement, rows, prov)


#: Per FOMC document dataset: the first date marketlens-data reads, and the
#: longest gap between two documents (a store whose earliest document is within
#: it of a window's start holds the window: each sync reads from its start to now).
DOCUMENT_HISTORY = {
    "fomc.projections": (date(2021, 3, 17), 110),
    "fomc.minutes": (date(2012, 1, 25), 70),
    "fomc.beige_book": (date(2017, 1, 18), 60),
}


async def _documents(ctx: ToolContext, dataset_id: str, start: date, as_of_text):
    """Refresh the FOMC document dataset for a window starting at ``start``
    (module docstring), then read it whole, point in time."""
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(as_of_text)
    floor, gap = DOCUMENT_HISTORY[dataset_id]

    def work(session: common.Session):
        # what the store holds at all (not a point-in-time read: whether to fetch)
        earliest = F.day((session.store.coverage(dataset_id) or {}).get("event_min"))
        if earliest is None or (earliest > max(start, floor) + timedelta(days=gap)):
            session.refresh(dataset_id, {"start": (start - timedelta(days=1)).isoformat()})
        session.refresh(dataset_id, {})
        return F.records(session.read(dataset_id, as_of=as_of))

    records, session = await common.run(ctx, s, work, source="fomc")
    return s, as_of, records, session


def _horizon_order(horizon: str) -> tuple[bool, str]:
    return (horizon == "longer_run", horizon)


async def fed_fomc_projections(ctx: ToolContext, args: ProjectionsInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=date(today.year - 1, 1, 1), default_end=today
    )
    s, as_of, records, session = await _documents(ctx, "fomc.projections", start, args.as_of)
    variables = set(args.variables or ())
    rows = []
    for r in records:
        kind = F.text(r["kind"])
        if not F.within(F.day(r["meeting_date"]), start, end) or args.kind not in ("all", kind):
            continue
        if variables and F.text(r["variable"]) not in variables:
            continue
        summary = kind == "summary"
        rows.append(
            common.row(
                FomcProjection,
                {
                    "meeting_date": F.day(r["meeting_date"]),
                    "release_date": F.day(r["release_date"]),
                    "kind": kind,
                    "variable": F.text(r["variable"]),
                    "horizon": F.text(r["horizon"]),
                    "horizon_year": F.integer(r["horizon_year"]),
                    "median": F.fraction(r["median"]),
                    "central_tendency_low": F.fraction(r["central_tendency_low"]),
                    "central_tendency_high": F.fraction(r["central_tendency_high"]),
                    "range_low": F.fraction(r["range_low"]),
                    "range_high": F.fraction(r["range_high"]),
                    "midpoint": F.fraction(r["midpoint"]),
                    "participants": F.integer(r["participants"]),
                    "url": F.text(r["url"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "horizon_year": NA,
                    **{
                        c: (NA if not summary else AbsenceCode.NO_DATA)
                        for c in (
                            "median",
                            "central_tendency_low",
                            "central_tendency_high",
                            "range_low",
                            "range_high",
                        )
                    },
                    "midpoint": NA if summary else AbsenceCode.NO_DATA,
                    "participants": NA if summary else AbsenceCode.NO_DATA,
                },
            )
        )
    rows.sort(
        key=lambda r: (
            -r.meeting_date.toordinal(),
            r.kind != "summary",
            r.variable,
            _horizon_order(r.horizon),
            -(r.midpoint or 0),
        )
    )
    prov = _prov(
        ctx,
        "fed_fomc_projections",
        "fomc",
        "fomc.projections",
        {
            "start": start,
            "end": end,
            "kind": args.kind,
            "variables": args.variables,
            "as_of": args.as_of,
        },
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, FomcProjection, rows, prov)


async def fed_fomc_minutes(ctx: ToolContext, args: MinutesInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=date(today.year - 1, 1, 1), default_end=today
    )
    s, as_of, records, session = await _documents(ctx, "fomc.minutes", start, args.as_of)
    rows = [
        common.row(
            FomcMinutes,
            {
                "meeting_date": F.day(r["meeting_date"]),
                "release_date": F.day(r["release_date"]),
                "url": F.text(r["url"]),
                "text": F.text(r["text"]) if args.include_text else None,
                "n_paragraphs": F.integer(r["n_paragraphs"]) or 0,
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
            codes={"text": AbsenceCode.NO_DATA if args.include_text else WITHHELD},
        )
        for r in records
        if F.within(F.day(r["meeting_date"]), start, end)
    ]
    rows.sort(key=lambda r: r.meeting_date, reverse=True)
    prov = _prov(
        ctx,
        "fed_fomc_minutes",
        "fomc",
        "fomc.minutes",
        {"start": start, "end": end, "include_text": args.include_text, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, FomcMinutes, rows, prov)


DISTRICT_ORDER = tuple(District.__args__)  # type: ignore[attr-defined]


async def fed_beige_book(ctx: ToolContext, args: BeigeBookInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=today - timedelta(days=60), default_end=today
    )
    s, as_of, records, session = await _documents(ctx, "fomc.beige_book", start, args.as_of)
    districts = set(args.districts or DISTRICT_ORDER)
    rows = [
        common.row(
            BeigeBookSection,
            {
                "release_date": F.day(r["release_date"]),
                "edition": F.text(r["edition"]) or "",
                "district": F.text(r["district"]) or "",
                "url": F.text(r["url"]),
                "text": F.text(r["text"]) if args.include_text else None,
                "n_paragraphs": F.integer(r["n_paragraphs"]) or 0,
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
            codes={"text": AbsenceCode.NO_DATA if args.include_text else WITHHELD},
        )
        for r in records
        if F.within(F.day(r["release_date"]), start, end) and F.text(r["district"]) in districts
    ]
    order = {d: i for i, d in enumerate(DISTRICT_ORDER)}
    rows.sort(key=lambda r: (-r.release_date.toordinal(), order.get(r.district, 99)))
    prov = _prov(
        ctx,
        "fed_beige_book",
        "fomc",
        "fomc.beige_book",
        {
            "start": start,
            "end": end,
            "districts": args.districts,
            "include_text": args.include_text,
            "as_of": args.as_of,
        },
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, BeigeBookSection, rows, prov)


async def fed_reference_rates(ctx: ToolContext, args: RatesInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 1), default_end=today
    )
    s, as_of, records, session = await _simple(ctx, "nyfed.reference_rates", "nyfed", args.as_of)
    wanted = set(args.rate_types)
    rows = []
    for r in records:
        kind = F.text(r["rate_type"])
        if kind not in wanted or not F.within(F.day(r["date"]), start, end):
            continue
        target = AbsenceCode.NO_DATA if kind == "EFFR" else NA
        rows.append(
            common.row(
                ReferenceRate,
                {
                    "rate_type": kind,
                    "date": F.day(r["date"]),
                    "rate": F.fraction(r["rate"]),
                    "volume_usd": F.scaled(r["volume_billions"], 1e9, 2),
                    "target_from": F.fraction(r["target_from"]),
                    "target_to": F.fraction(r["target_to"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"target_from": target, "target_to": target},
            )
        )
    rows.sort(key=lambda r: (r.date, RATE_ORDER.index(r.rate_type)))
    prov = _prov(
        ctx,
        "fed_reference_rates",
        "nyfed",
        "nyfed.reference_rates",
        {"rate_types": args.rate_types, "start": start, "end": end, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, ReferenceRate, rows, prov)


async def treasury_yield_curve(ctx: ToolContext, args: CurveInputs) -> ToolOutput:
    from datetime import timedelta

    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=today - timedelta(days=30),
        default_end=today,
        max_days=CURVE_MAX_DAYS,
    )
    years = [{"year": y} for y in range(start.year, min(end.year, today.year) + 1)]
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)

    def work(session: common.Session):
        for identity in years:
            session.refresh("treasury.yield_curve", identity)
        labels = dict(session.module("omni.catalog").TENOR_LABELS)
        return labels, F.records(session.read("treasury.yield_curve", as_of=as_of))

    (labels, records), session = await common.run(ctx, s, work, source="treasury")
    tenors = set(args.tenors or TENOR_ORDER)
    rows = []
    for r in records:
        tenor = F.text(r["tenor"])
        if tenor not in tenors or not F.within(F.day(r["date"]), start, end):
            continue
        rows.append(
            common.row(
                YieldCurvePoint,
                {
                    "date": F.day(r["date"]),
                    "tenor": tenor,
                    "tenor_label": labels.get(tenor),
                    "rate": F.fraction(r["rate"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"tenor_label": NA},
            )
        )
    order = {t: i for i, t in enumerate(TENOR_ORDER)}
    rows.sort(key=lambda r: (r.date, order.get(r.tenor, 99)))
    prov = _prov(
        ctx,
        "treasury_yield_curve",
        "treasury",
        "treasury.yield_curve",
        {"start": start, "end": end, "tenors": args.tenors, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, YieldCurvePoint, rows, prov)


def _security_matches(row: dict, wanted: set[str]) -> bool:
    kind = (F.text(row["security_type"]) or "").lower()
    term = (F.text(row["security_term"]) or "").upper()
    return any(w.lower() == kind or (w in ("TIPS", "FRN") and w in term.split()) for w in wanted)


async def treasury_auctions(ctx: ToolContext, args: AuctionsInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 1), default_end=today
    )
    s, as_of, records, session = await _simple(ctx, "fiscaldata.auctions", "fiscaldata", args.as_of)
    wanted = set(args.security_types or [])
    rows = []
    for r in records:
        if not F.within(F.day(r["auction_date"]), start, end) or (
            wanted and not _security_matches(r, wanted)
        ):
            continue
        rows.append(
            common.row(
                TreasuryAuction,
                {
                    "cusip": F.text(r["cusip"]),
                    "auction_date": F.day(r["auction_date"]),
                    "security_type": F.text(r["security_type"]) or "",
                    "security_term": F.text(r["security_term"]) or "",
                    "issue_date": F.day(r["issue_date"]),
                    "maturity_date": F.day(r["maturity_date"]),
                    "offering_amount": F.number(r["offering_amt"]),
                    "total_accepted": F.number(r["total_accepted"]),
                    "bid_to_cover": F.number(r["bid_to_cover"]),
                    "high_yield": F.fraction(r["high_yield"]),
                    "high_investment_rate": F.fraction(r["high_investment_rate"]),
                    "high_discount_rate": F.fraction(r["high_discnt_rate"]),
                    "interest_rate": F.fraction(r["int_rate"]),
                    "price_per_100": F.number(r["price_per100"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
        )
    rows.sort(key=lambda r: (r.auction_date, r.cusip), reverse=True)
    prov = _prov(
        ctx,
        "treasury_auctions",
        "fiscaldata",
        "fiscaldata.auctions",
        {"start": start, "end": end, "security_types": args.security_types, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, TreasuryAuction, rows, prov)


async def treasury_debt(ctx: ToolContext, args: DailyInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 1), default_end=today
    )
    s, as_of, records, session = await _simple(ctx, "fiscaldata.debt_to_penny", "fiscaldata", args.as_of)
    rows = [
        common.row(
            TreasuryDebt,
            {
                "date": F.day(r["date"]),
                "total_debt": F.number(r["total_debt"]),
                "debt_held_public": F.number(r["debt_held_public"]),
                "intragovernmental": F.number(r["intragov"]),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
        )
        for r in records
        if F.within(F.day(r["date"]), start, end)
    ]
    rows.sort(key=lambda r: r.date)
    prov = _prov(
        ctx,
        "treasury_debt",
        "fiscaldata",
        "fiscaldata.debt_to_penny",
        {"start": start, "end": end, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, TreasuryDebt, rows, prov)


async def treasury_tga(ctx: ToolContext, args: DailyInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 1), default_end=today
    )
    s, as_of, records, session = await _simple(ctx, "fiscaldata.tga_balance", "fiscaldata", args.as_of)
    rows = [
        common.row(
            TreasuryCashBalance,
            {
                "date": F.day(r["date"]),
                "account_type": F.text(r["account_type"]) or "",
                "closing_balance": F.scaled(r["closing_balance_mm"], 1e6, 2),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
        )
        for r in records
        if F.within(F.day(r["date"]), start, end)
    ]
    rows.sort(key=lambda r: r.date)
    prov = _prov(
        ctx,
        "treasury_tga",
        "fiscaldata",
        "fiscaldata.tga_balance",
        {"start": start, "end": end, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, TreasuryCashBalance, rows, prov)


HANDLERS = {
    "fed_fomc_meetings": fed_fomc_meetings,
    "fed_fomc_statements": fed_fomc_statements,
    "fed_fomc_projections": fed_fomc_projections,
    "fed_fomc_minutes": fed_fomc_minutes,
    "fed_beige_book": fed_beige_book,
    "fed_reference_rates": fed_reference_rates,
    "treasury_yield_curve": treasury_yield_curve,
    "treasury_auctions": treasury_auctions,
    "treasury_debt": treasury_debt,
    "treasury_tga": treasury_tga,
}


def _spec(name, title, description, readme, inputs, model, provider, route, *, risk="api_structured"):
    return ToolSpec(
        name=name,
        capability="fed_treasury",
        title=title,
        description=description,
        readme=readme,
        input_model=inputs,
        output_model=model,
        provider=provider,
        route=route,
        handler=HANDLERS[name],
        golden_test=GOLDEN,
        output_risk=risk,
    )


SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "fed_fomc_meetings",
        "FOMC meetings",
        common.describe(
            "The FOMC meeting calendar from the Federal Reserve: start and end dates, and whether the "
            "meeting has a Summary of Economic Projections. Keyless.",
            "forward_known",
            stored=False,
        ),
        "FOMC meeting calendar",
        MeetingsInputs,
        FomcMeeting,
        "fomc",
        "omni fomc.meetings <- www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    ),
    _spec(
        "fed_fomc_statements",
        "FOMC statements",
        common.describe(
            "Post-meeting FOMC policy statements (text and link; the Fed's own words, marked untrusted), "
            "knowable at 2 p.m. Washington time on the meeting's last day. include_text=false lists dates "
            "and links only. Keyless.",
            "lagged",
        ),
        "FOMC statement text",
        StatementsInputs,
        FomcStatement,
        "fomc",
        "omni fomc.statement <- www.federalreserve.gov (statements linked from the FOMC calendar)",
        risk="external_text",
    ),
    _spec(
        "fed_fomc_projections",
        "FOMC economic projections and dot plot",
        common.describe(
            "The FOMC's Summary of Economic Projections (2021-03 on), from the pages the Fed's calendar links: "
            "kind summary is Table 1, the median, central tendency and range of the participants' "
            "projections of real GDP growth, unemployment, PCE and core PCE inflation and the federal funds "
            "rate per year and the longer run; kind dots is the dot plot, how many participants put the "
            "appropriate federal funds rate at each midpoint. Values are fractions (0.041 is 4.1 %). "
            "Knowable at 2 p.m. Washington time on the meeting's last day. Keyless.",
            "lagged",
        ),
        "FOMC Summary of Economic Projections with the dot plot",
        ProjectionsInputs,
        FomcProjection,
        "fomc",
        "omni fomc.projections <- www.federalreserve.gov (SEP pages linked from the FOMC calendar)",
    ),
    _spec(
        "fed_fomc_minutes",
        "FOMC minutes",
        common.describe(
            "The minutes of FOMC meetings (2012 on; text and link; the Fed's own words, marked untrusted), "
            "knowable at 2 p.m. Washington time on the release date the Fed prints beside each link, about "
            "three weeks after the meeting. include_text=false lists dates and links only. Keyless.",
            "lagged",
        ),
        "FOMC minutes text",
        MinutesInputs,
        FomcMinutes,
        "fomc",
        "omni fomc.minutes <- www.federalreserve.gov (minutes linked from the FOMC calendar and year pages)",
        risk="external_text",
    ),
    _spec(
        "fed_beige_book",
        "Beige Book",
        common.describe(
            "The Federal Reserve's Beige Book (2017 on): the national summary and each Reserve Bank's report "
            "of an edition (text and link; marked untrusted), knowable at 2 p.m. Washington time on its "
            "release date. Default window: the last 60 days. include_text=false lists dates and links only. "
            "Keyless.",
            "lagged",
        ),
        "Beige Book text per district",
        BeigeBookInputs,
        BeigeBookSection,
        "fomc",
        "omni fomc.beige_book <- www.federalreserve.gov (editions linked from the Beige Book pages)",
        risk="external_text",
    ),
    _spec(
        "fed_reference_rates",
        "NY Fed reference rates",
        common.describe(
            "The New York Fed's official reference rates: SOFR, EFFR, OBFR, TGCR, BGCR. rate and the FOMC "
            "target range are fractions per year (0.0533 is 5.33 %); volume in USD. Keyless.",
            "lagged",
        ),
        "SOFR, EFFR, OBFR, TGCR, BGCR",
        RatesInputs,
        ReferenceRate,
        "nyfed",
        "omni nyfed.reference_rates <- markets.newyorkfed.org/api/rates/all/search.json",
    ),
    _spec(
        "treasury_yield_curve",
        "Treasury yield curve",
        common.describe(
            "The US Treasury's daily par yield curve, one row per date and tenor (1M to 30Y), rates as "
            "fractions per year. At most 3,660 days per call. Keyless.",
            "lagged",
        ),
        "Daily Treasury par yield curve",
        CurveInputs,
        YieldCurvePoint,
        "treasury",
        "omni treasury.yield_curve <- home.treasury.gov daily-treasury-rates.csv",
    ),
    _spec(
        "treasury_auctions",
        "Treasury auctions",
        common.describe(
            "Treasury auction results from FiscalData: security type and term, offering and accepted amounts "
            "in USD, bid-to-cover (times), high yield, investment and discount rates and coupon (fractions per "
            "year), price per 100. Keyless.",
            "lagged",
        ),
        "Treasury auction results",
        AuctionsInputs,
        TreasuryAuction,
        "fiscaldata",
        "omni fiscaldata.auctions <- api.fiscaldata.treasury.gov/v1/accounting/od/auctions_query",
    ),
    _spec(
        "treasury_debt",
        "Treasury debt to the penny",
        common.describe(
            "Total US public debt outstanding per day, split into debt held by the public and "
            "intragovernmental holdings, in USD (Debt to the Penny). Keyless.",
            "lagged",
        ),
        "Total public debt per day",
        DailyInputs,
        TreasuryDebt,
        "fiscaldata",
        "omni fiscaldata.debt_to_penny <- api.fiscaldata.treasury.gov/v2/accounting/od/debt_to_penny",
    ),
    _spec(
        "treasury_tga",
        "Treasury General Account",
        common.describe(
            "The Treasury General Account's closing balance per day, in USD (the Daily Treasury Statement). "
            "Keyless.",
            "lagged",
        ),
        "Treasury cash balance (TGA) per day",
        DailyInputs,
        TreasuryCashBalance,
        "fiscaldata",
        "omni fiscaldata.tga_balance <- api.fiscaldata.treasury.gov/v1/accounting/dts/operating_cash_balance",
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "fed_fomc_meetings": ("fomc.meetings",),
    "fed_fomc_statements": ("fomc.statement",),
    "fed_fomc_projections": ("fomc.projections",),
    "fed_fomc_minutes": ("fomc.minutes",),
    "fed_beige_book": ("fomc.beige_book",),
    "fed_reference_rates": ("nyfed.reference_rates",),
    "treasury_yield_curve": ("treasury.yield_curve",),
    "treasury_auctions": ("fiscaldata.auctions",),
    "treasury_debt": ("fiscaldata.debt_to_penny",),
    "treasury_tga": ("fiscaldata.tga_balance",),
}
