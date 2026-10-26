"""Economic series: FRED/ALFRED with vintages, BLS, BEA NIPA tables, BLS
release schedules and the FRED series catalogue; US trade by commodity and
partner (Census), the Bank of Japan's and the Bank of England's series, the
Richmond and Kansas City Fed business surveys and FINRA's TRACE market
aggregates (capability ``macro``)."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import (
    CentralBankObservation,
    EconomicObservation,
    EconomicSeriesInfo,
    RegionalSurveyIndex,
    ReleaseScheduleEntry,
    TraceAggregate,
    TradeFlow,
)

from .. import frames as F
from .. import runtime
from . import common
from .common import AsOf, Inputs

GOLDEN = "tests/data/test_macro.py"
_FRED_ID = re.compile(r"^[A-Z0-9_.]{1,40}$")
_BLS_ID = re.compile(r"^[A-Z0-9]{5,30}$")
_BEA_TABLE = re.compile(r"^T\d{5}[A-Z]?$")


def _ids(pattern: re.Pattern[str], example: str):
    def check(values: list[str]) -> list[str]:
        out = []
        for v in values:
            s = v.strip().upper()
            if not pattern.match(s):
                raise ValueError(f"{v!r} is not a series id (e.g. {example})")
            out.append(s)
        return list(dict.fromkeys(out))

    return check


def _table(value: str) -> str:
    s = value.strip().upper()
    if not _BEA_TABLE.match(s):
        raise ValueError(f"{value!r} is not a NIPA table name (e.g. T10101)")
    return s


class SeriesInputs(Inputs):
    series_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=20,
            description="FRED series ids (1-20), e.g. CPIAUCSL, CPILFESL, UNRATE, PAYEMS, ICSA, FEDFUNDS, "
            "DGS10, T10Y2Y, GDPC1, PCEPI.",
        ),
        AfterValidator(_ids(_FRED_ID, "CPIAUCSL")),
    ]
    start: date | None = Field(default=None, description="First observation date (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last observation date (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()
    include_vintages: bool = Field(
        default=False,
        description="Every stored vintage (one row per revision, knowable at as_of) instead of the latest.",
    )


class CatalogInputs(Inputs):
    q: str | None = Field(default=None, max_length=80, description="Substring of the series id or name.")
    category: str | None = Field(
        default=None, max_length=40, description="Category, e.g. Inflation, Employment, Interest Rates."
    )


class BlsInputs(Inputs):
    series_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=20,
            description="BLS series ids (1-20), e.g. CUUR0000SA0 (CPI-U), CUUR0000SA0L1E (core CPI), "
            "LNS14000000 (unemployment rate), CES0000000001 (payrolls), JTS000000000000000JOL (job openings).",
        ),
        AfterValidator(_ids(_BLS_ID, "CUUR0000SA0")),
    ]
    start: date | None = Field(
        default=None,
        description="First month (inclusive; a quarter is kept when any of its months is in the window). "
        "Default: 10 years back.",
    )
    end: date | None = Field(default=None, description="Last month (inclusive). Default: today.")
    as_of: AsOf | None = common.as_of_field()


class BeaInputs(Inputs):
    table_name: Annotated[str, Field(description="NIPA table, e.g. T10101 (GDP)."), AfterValidator(_table)]
    frequency: Literal["A", "Q", "M"] = Field(default="Q", description="A annual, Q quarterly, M monthly.")
    line_numbers: list[int] | None = Field(
        default=None, max_length=100, description="Only these table lines (default: every line)."
    )
    start: date | None = Field(default=None, description="First period start (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last period start (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()


class ScheduleInputs(Inputs):
    releases: list[Literal["cpi", "employment_situation"]] = Field(
        default_factory=lambda: ["cpi", "employment_situation"],
        min_length=1,
        description="cpi and/or employment_situation (the jobs report).",
    )
    start: date | None = Field(default=None, description="First release date. Default: 30 days ago.")
    end: date | None = Field(default=None, description="Last release date. Default: a year ahead.")
    as_of: AsOf | None = common.as_of_field()


_HS = re.compile(r"^\d{2,10}$")
_CTY = re.compile(r"^(TOTAL|\d{4}|\dXXX)$")
_BOJ_DB = re.compile(r"^[A-Z]{2}\d{0,2}$")
_BOJ_CODE = re.compile(r"^[A-Z0-9@_.]{2,40}$")
_BOE_CODE = re.compile(r"^[A-Z][A-Z0-9]{4,11}$")
TRACE_REPORTS = (
    "corporateMarketBreadth",
    "corporateMarketSentiment",
    "corporate144AMarketBreadth",
    "corporate144AMarketSentiment",
    "agencyMarketBreadth",
    "agencyMarketSentiment",
    "treasuryDailyAggregates",
    "treasuryMonthlyAggregates",
)


def _codes(pattern: re.Pattern[str], example: str, *, upper: bool = True):
    def check(values: list[str]) -> list[str]:
        out = []
        for v in values:
            c = v.strip().upper() if upper else v.strip()
            if not pattern.match(c):
                raise ValueError(f"{v!r} is not a valid code (e.g. {example})")
            out.append(c)
        return list(dict.fromkeys(out))

    return check


class TradeInputs(Inputs):
    hs_codes: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=6,
            description="HS commodity codes (1-6), 2 to 10 digits, e.g. 8542 (integrated circuits), 8541, "
            "8471 (computers), 8473, 8486 (semiconductor equipment), 8517.",
        ),
        AfterValidator(_codes(_HS, "8542")),
    ]
    flow: Literal["exports", "imports", "both"] = Field(
        default="both", description="US exports, imports or both."
    )
    country_codes: Annotated[
        list[str] | None,
        Field(
            default=None,
            max_length=25,
            description="Census country codes to keep (default: every stored partner), e.g. 5700 China, "
            "5830 Taiwan, 5800 Korea, 5880 Japan, 2010 Mexico, 0003 EU, 0027 ASEAN, TOTAL all countries.",
        ),
        AfterValidator(lambda v: None if v is None else _codes(_CTY, "5700")(v)),
    ] = None
    start: date | None = Field(default=None, description="First month (inclusive). Default: all (2010 on).")
    end: date | None = Field(default=None, description="Last month (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()


class BojInputs(Inputs):
    database: Annotated[
        str,
        Field(
            description="The BoJ database: FM01 (call rate), FM08 (FX), FM09 (effective FX), MD01 (monetary "
            "base), BS01 (the Bank's accounts), CO (Tankan), ..."
        ),
        AfterValidator(lambda v: _codes(_BOJ_DB, "FM01")([v])[0]),
    ]
    series_codes: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=20,
            description="Series codes of that database (1-20), e.g. STRDCLUCON (call rate), FXERD01 "
            "(USD/JPY 9:00), MABS1AN11 (monetary base), MABJMTA (total assets), TK99F1000601GCQ01000 (Tankan "
            "large manufacturers DI).",
        ),
        AfterValidator(_codes(_BOJ_CODE, "STRDCLUCON")),
    ]
    start: date | None = Field(default=None, description="First period start (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last period start (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()


class BoeInputs(Inputs):
    series_codes: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=20,
            description="Bank of England database series codes (1-20), e.g. IUDBEDR (Bank Rate), IUDSOIA "
            "(SONIA), IUDMNPY (10-year gilt par yield), LPMAUYN (M4), XUDLUSS (USD per GBP).",
        ),
        AfterValidator(_codes(_BOE_CODE, "IUDBEDR")),
    ]
    start: date | None = Field(default=None, description="First period start (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last period start (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()


class RegionalSurveyInputs(Inputs):
    bank: Literal["richmond", "kansascity", "both"] = Field(default="both", description="Which Reserve Bank.")
    survey: Literal["manufacturing", "services", "both"] = Field(default="both", description="Which survey.")
    series: list[str] | None = Field(
        default=None,
        max_length=40,
        description="Series to keep (default: all), e.g. sa_mfg_composite (Richmond), composite_index_month_sa "
        "(Kansas City).",
    )
    start: date | None = Field(default=None, description="First month (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last month (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()


class TraceInputs(Inputs):
    report: Literal[TRACE_REPORTS] = Field(description="FINRA's TRACE aggregate report.")  # type: ignore[valid-type]
    start: date | None = Field(default=None, description="First date (inclusive). Default: 30 days ago.")
    end: date | None = Field(default=None, description="Last date (inclusive). Default: today.")
    product_category: str | None = Field(default=None, max_length=80, description="Keep one productCategory.")
    metrics: list[str] | None = Field(
        default=None, max_length=20, description="Keep these metrics (FINRA's field names), e.g. advances."
    )
    as_of: AsOf | None = common.as_of_field()


NPS = AbsenceCode.NOT_PROVIDED_BY_SOURCE
NA = AbsenceCode.NOT_APPLICABLE
ROUTES = {
    "fred": "omni fred.series <- api.stlouisfed.org/fred/series/observations, series/vintagedates (ALFRED vintages)",
    "catalog": "omni catalog.series_meta (local, no fetch)",
    "bls": "omni bls.timeseries <- api.bls.gov/publicAPI/v2/timeseries/data",
    "bea": "omni bea.nipa <- apps.bea.gov/api/data (NIPA GetData)",
    "schedule": "omni bls.cpi_schedule, bls.empsit_schedule <- www.bls.gov/schedule/news_release, "
    "schedule/{year}/{MM}_sched_list.htm (this and next month's releases)",
}
ROUTES.update(
    {
        "trade": "omni census.trade <- api.census.gov/data/timeseries/intltrade/{exports,imports}/hs",
        "boj": "omni boj.series <- www.stat-search.boj.or.jp/api/v1/getDataCode",
        "boe": "omni boe.series <- www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp",
        "regional": "omni regionalfed.surveys <- the Richmond and Kansas City Feds' survey workbooks and "
        "release schedules (linked from their survey pages)",
        "trace": "omni finra.trace_aggregates <- api.finra.org/data/group/fixedIncomeMarket "
        "(OAuth token from ews.fintech.finra.org)",
    }
)
SCHEDULES = {"cpi": "bls.cpi_schedule", "employment_situation": "bls.empsit_schedule"}
#: marketlens-data keeps BEA's CL_UNIT (`unit`), METRIC_NAME (`metric_name`) and UNIT_MULT
#: (`unit_mult`), but rows it stored before it kept the last two have neither: their scale is lost.
BEA_LEVEL_NOTE = (
    "A value whose units is just 'Level' was stored without BEA's multiplier (UNIT_MULT; rows stored before "
    "marketlens-data kept it have none), so its scale is not known: NIPA dollar levels are usually in millions of "
    "dollars. Check the table's header on bea.gov before reading a magnitude."
)


def _window_check(start: date | None, end: date | None) -> None:
    if start and end:
        common.window("", start, end, default_start=start, default_end=end)


async def macro_series(ctx: ToolContext, args: SeriesInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    _window_check(args.start, args.end)

    def work(session: common.Session):
        catalog = session.module("omni.catalog").series_meta()
        authority = session.module("omni.sources.base").authority_of
        out = []
        for sid in args.series_ids:
            session.refresh("fred.series", {"series_id": sid})
            df = session.read("fred.series", as_of=as_of, vintages=args.include_vintages, series_id=sid)
            out.append((sid, F.records(df), catalog.get(sid), authority("fred.series", series_id=sid)))
        return out

    found, session = await common.run(ctx, s, work, source="fred")
    rows = []
    for order, (sid, records, meta, _) in enumerate(found):
        meta = meta or {}
        for r in records:
            d = F.day(r["date"])
            if not F.within(d, args.start, args.end):
                continue
            row = common.row(
                EconomicObservation,
                {
                    "source": "fred",
                    "series_id": sid,
                    "title": F.text(meta.get("name")),
                    "date": d,
                    "period": None,
                    "frequency": None,
                    "value": F.number(r["value"]),
                    "units": F.text(meta.get("unit")),
                    "table_name": None,
                    "line_number": None,
                    "knowledge_time": F.instant(r["knowledge_time"]),
                    "pit": "vintage",
                },
                codes={"title": NPS, "units": NPS},
                explained=("period", "frequency", "table_name", "line_number"),
            )
            rows.append((order, row))
    ordered = [r for _, r in sorted(rows, key=lambda x: (x[0], x[1].date, x[1].knowledge_time))]
    absent = common.reasons(
        period=(NA, "FRED states no period code"),
        frequency=(NPS, "FRED's observations do not state the frequency"),
        table_name=(NA, "BEA tables only"),
        line_number=(NA, "BEA tables only"),
    )
    prov = common.provenance(
        ctx,
        provider="fred",
        route=ROUTES["fred"],
        datasets=("fred.series",),
        request={
            "series_ids": args.series_ids,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
            "include_vintages": args.include_vintages,
        },
        as_of=as_of,
        rows=ordered,
        session=session,
        authority=[a for *_, a in found],
    )
    return common.output(ctx, s, EconomicObservation, ordered, prov, absent=absent)


async def macro_series_catalog(ctx: ToolContext, args: CatalogInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)

    def work(session: common.Session):
        return dict(session.module("omni.catalog").series_meta())

    catalog, _ = await common.run(ctx, s, work, source="fred")
    q = (args.q or "").strip().lower()
    category = (args.category or "").strip().lower()
    rows = []
    for sid in sorted(catalog):
        meta = catalog[sid] or {}
        name = F.text(meta.get("name"))
        if q and q not in sid.lower() and q not in (name or "").lower():
            continue
        if category and category != (F.text(meta.get("category")) or "").lower():
            continue
        rows.append(
            common.row(
                EconomicSeriesInfo,
                {
                    "series_id": sid,
                    "name": name,
                    "category": F.text(meta.get("category")),
                    "units": F.text(meta.get("unit")),
                    "chart_type": F.text(meta.get("chart_type")),
                    "polarity": F.text(meta.get("polarity")),
                },
                codes=dict.fromkeys(("name", "category", "units", "chart_type", "polarity"), NPS),
            )
        )
    prov = common.provenance(
        ctx,
        provider="local",
        route=ROUTES["catalog"],
        datasets=(),
        request={"q": args.q, "category": args.category},
        as_of=None,
        rows=rows,
        session=None,
    )
    prov = prov.model_copy(update={"cached": True})
    return common.output(ctx, s, EconomicSeriesInfo, rows, prov)


def decade_blocks(start: date, end: date, this_year: int) -> list[tuple[int, int]]:
    """Fixed decade blocks (2010-2019, 2020-2029, ...) covering start..end, the
    last one ending at this year: BLS serves at most 10 years per request
    without a key, and fixed blocks keep each block's freshness stable."""
    last = min(end.year, this_year)
    return [(d, min(d + 9, this_year)) for d in range(start.year // 10 * 10, last // 10 * 10 + 1, 10)]


async def macro_bls_series(ctx: ToolContext, args: BlsInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 10), default_end=today
    )
    start = start.replace(day=1)  # observations are dated the first of their month (or quarter)
    blocks = decade_blocks(start, end, today.year)

    def work(session: common.Session):
        out = []
        for sid in args.series_ids:
            for first, last in blocks:
                session.refresh("bls.timeseries", {"series_id": sid, "start_year": first, "end_year": last})
            out.append((sid, F.records(session.read("bls.timeseries", as_of=as_of, series_id=sid))))
        return out

    found, session = await common.run(ctx, s, work, source="bls")
    rows = []
    for order, (sid, records) in enumerate(found):
        for r in records:
            d = F.day(r["date"])
            period = F.text(r["period"])
            quarterly = period is not None and period.startswith("Q")  # Q01-Q04; else M01-M12
            # a quarter is in the window when any of its months is: its last on or after start
            if d is None or d > end or (d.replace(month=d.month + 2) if quarterly else d) < start:
                continue
            rows.append(
                (
                    order,
                    common.row(
                        EconomicObservation,
                        {
                            "source": "bls",
                            "series_id": sid,
                            "title": None,
                            "date": d,
                            "period": period,
                            "frequency": "Q" if quarterly else "M",
                            "value": F.number(r["value"]),
                            "units": None,
                            "table_name": None,
                            "line_number": None,
                            "knowledge_time": F.instant(r["knowledge_time"]),
                            "pit": "lagged",
                        },
                        explained=("title", "units", "table_name", "line_number"),
                    ),
                )
            )
    ordered = [r for _, r in sorted(rows, key=lambda x: (x[0], x[1].date))]
    absent = common.reasons(
        title=(NPS, "BLS's data API returns no series title"),
        units=(NPS, "BLS's data API returns no unit"),
        table_name=(NA, "BEA tables only"),
        line_number=(NA, "BEA tables only"),
    )
    prov = common.provenance(
        ctx,
        provider="bls",
        route=ROUTES["bls"],
        datasets=("bls.timeseries",),
        request={"series_ids": args.series_ids, "start": start, "end": end, "as_of": args.as_of},
        as_of=as_of,
        rows=ordered,
        session=session,
        authority=["official"],
    )
    return common.output(ctx, s, EconomicObservation, ordered, prov, absent=absent)


def _bea_units(unit: str | None, measure: str | None, power: int | None) -> str | None:
    """A 'Level' row's units with BEA's METRIC_NAME and UNIT_MULT ('Level, millions of Current
    Dollars'); any other unit, or a level stored without UNIT_MULT, as BEA's CL_UNIT."""
    if power is None or (unit or "").strip().lower() != "level":
        return unit
    scale = {0: "", 3: "thousands of ", 6: "millions of ", 9: "billions of "}.get(power, f"10^{power} ")
    return f"{unit}, {scale}{measure or 'units'}"


def _period(d: date, frequency: str) -> str:
    if frequency == "A":
        return "A"
    if frequency == "Q":
        return f"Q{(d.month - 1) // 3 + 1}"
    return f"M{d.month:02d}"


async def macro_bea_table(ctx: ToolContext, args: BeaInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    _window_check(args.start, args.end)
    identity = {"table_name": args.table_name, "frequency": args.frequency}

    def work(session: common.Session):
        session.refresh("bea.nipa", identity)
        return F.records(session.read("bea.nipa", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="bea")
    lines = set(args.line_numbers or [])
    rows = []
    for r in records:
        d = F.day(r["date"])
        line = F.integer(r["line_number"])
        if not F.within(d, args.start, args.end) or (lines and line not in lines):
            continue
        rows.append(
            common.row(
                EconomicObservation,
                {
                    "source": "bea",
                    "series_id": F.text(r["series_code"]) or "",
                    "title": F.text(r["line_description"]),
                    "date": d,
                    "period": _period(d, args.frequency),
                    "frequency": args.frequency,
                    "value": F.number(r["value"]),
                    "units": _bea_units(
                        F.text(r["unit"]), F.text(r.get("metric_name")), F.integer(r.get("unit_mult"))
                    ),
                    "table_name": args.table_name,
                    "line_number": line,
                    "knowledge_time": F.instant(r["knowledge_time"]),
                    "pit": "lagged",
                },
                codes={"title": NPS, "units": NPS},
            )
        )
    rows.sort(key=lambda r: (r.line_number or 0, r.series_id, r.date))
    # A level with its multiplier reads "Level, millions of ..."; one without it is just "Level".
    unscaled = any((r.units or "").strip().lower() == "level" for r in rows)
    prov = common.provenance(
        ctx,
        provider="bea",
        route=ROUTES["bea"],
        datasets=("bea.nipa",),
        request={
            "table_name": args.table_name,
            "frequency": args.frequency,
            "line_numbers": args.line_numbers,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
        },
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )
    notes = [BEA_LEVEL_NOTE] if unscaled else []
    return common.output(ctx, s, EconomicObservation, rows, prov, notes=notes)


async def macro_release_schedule(ctx: ToolContext, args: ScheduleInputs) -> ToolOutput:
    from datetime import timedelta

    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=today - timedelta(days=30),
        default_end=today + timedelta(days=365),
    )
    releases = list(dict.fromkeys(args.releases))

    def work(session: common.Session):
        out = []
        for release in releases:
            session.refresh(SCHEDULES[release], {})
            out.append((release, F.records(session.read(SCHEDULES[release], as_of=as_of))))
        return out

    found, session = await common.run(ctx, s, work, source="bls")
    rows = []
    for release, records in found:
        for r in records:
            released = F.day(r["start_date"])
            if not F.within(released, start, end):
                continue
            rows.append(
                common.row(
                    ReleaseScheduleEntry,
                    {
                        "release": release,
                        "reference_month": F.day(r["reference_month"]),
                        "start_date": released,
                        "end_date": F.day(r["end_date"]),
                        "scheduled_at": F.instant(r["scheduled_at"]),
                        "knowledge_time": F.instant(r["knowledge_time"]),
                    },
                )
            )
    rows.sort(key=lambda r: (r.scheduled_at, r.release))
    prov = common.provenance(
        ctx,
        provider="bls",
        route=ROUTES["schedule"],
        datasets=tuple(SCHEDULES[r] for r in releases),
        request={"releases": releases, "start": start, "end": end, "as_of": args.as_of},
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )
    return common.output(ctx, s, ReleaseScheduleEntry, rows, prov)


async def macro_census_trade(ctx: ToolContext, args: TradeInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    _window_check(args.start, args.end)
    flows = ["exports", "imports"] if args.flow == "both" else [args.flow]
    keep = set(args.country_codes or [])

    def work(session: common.Session):
        out = []
        for flow in flows:
            for hs in args.hs_codes:
                session.refresh("census.trade", {"flow": flow, "hs": hs})
                out.extend(F.records(session.read("census.trade", as_of=as_of, flow=flow, hs_code=hs)))
        return out

    records, session = await common.run(ctx, s, work, source="census")
    rows = []
    for r in records:
        month = F.day(r["month"])
        code = F.text(r["country_code"]) or ""
        if not F.within(month, args.start, args.end) or (keep and code not in keep):
            continue
        rows.append(
            common.row(
                TradeFlow,
                {
                    "flow": F.text(r["flow"]),
                    "hs_code": F.text(r["hs_code"]),
                    "hs_level": F.text(r["hs_level"]),
                    "description": F.text(r["description"]),
                    "country_code": code,
                    "country_name": F.text(r["country_name"]),
                    "summary_level": F.text(r["summary_level"]),
                    "month": month,
                    "value_usd": F.number(r["value"]),
                    "value_field": F.text(r["value_field"]),
                    "quantity": F.number(r["quantity"]),
                    "quantity_unit": F.text(r["quantity_unit"]),
                    "stamp_estimated": bool(F.flag(r["stamp_estimated"])),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"quantity": NA, "quantity_unit": NA},
            )
        )
    rows.sort(key=lambda r: (r.flow, r.hs_code, r.country_code, r.month))
    prov = common.provenance(
        ctx,
        provider="census",
        route=ROUTES["trade"],
        datasets=("census.trade",),
        request={
            "hs_codes": args.hs_codes,
            "flow": args.flow,
            "country_codes": args.country_codes,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
        },
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )
    return common.output(ctx, s, TradeFlow, rows, prov)


async def _central_bank(ctx: ToolContext, bank: str, database: str, codes: list[str], args) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    _window_check(args.start, args.end)
    dataset = f"{bank}.series"

    def work(session: common.Session):
        out = []
        for code in codes:
            session.refresh(dataset, {"flow": database, "key": code})
            out.append(F.records(session.read(dataset, as_of=as_of, flow=database, series_key=code)))
        return out

    found, session = await common.run(ctx, s, work, source=bank)
    rows = []
    for order, records in enumerate(found):
        for r in records:
            d = F.day(r["date"])
            if not F.within(d, args.start, args.end):
                continue
            row = common.row(
                CentralBankObservation,
                {
                    "bank": bank,
                    "database": database,
                    "series_code": F.text(r["series_key"]),
                    "period": F.text(r["period"]),
                    "date": d,
                    "value": F.number(r["value"]),
                    "units": F.text(r["unit"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"units": NPS},
            )
            rows.append((order, row))
    ordered = [r for _, r in sorted(rows, key=lambda x: (x[0], x[1].date))]
    prov = common.provenance(
        ctx,
        provider=bank,
        route=ROUTES[bank],
        datasets=(dataset,),
        request={
            "database": database,
            "series_codes": codes,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
        },
        as_of=as_of,
        rows=ordered,
        session=session,
        authority=["official"],
    )
    notes = (
        [
            "The Bank of England's CSV states no unit: each series' unit is in its description on the Bank's "
            "database (rates and yields in percent per annum, M4 and lending in sterling millions)."
        ]
        if bank == "boe" and ordered
        else []
    )
    return common.output(ctx, s, CentralBankObservation, ordered, prov, notes=notes)


async def macro_boj_series(ctx: ToolContext, args: BojInputs) -> ToolOutput:
    return await _central_bank(ctx, "boj", args.database, args.series_codes, args)


async def macro_boe_series(ctx: ToolContext, args: BoeInputs) -> ToolOutput:
    return await _central_bank(ctx, "boe", "IADB", args.series_codes, args)


async def macro_regional_fed_surveys(ctx: ToolContext, args: RegionalSurveyInputs) -> ToolOutput:
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    _window_check(args.start, args.end)
    banks = ["richmond", "kansascity"] if args.bank == "both" else [args.bank]
    surveys = ["manufacturing", "services"] if args.survey == "both" else [args.survey]
    keep = {x.strip() for x in args.series or []}

    def work(session: common.Session):
        out = []
        for bank in banks:
            for survey in surveys:
                session.refresh("regionalfed.surveys", {"bank": bank, "survey": survey})
                out.extend(
                    F.records(session.read("regionalfed.surveys", as_of=as_of, bank=bank, survey=survey))
                )
        return out

    records, session = await common.run(ctx, s, work, source="regionalfed")
    rows = []
    for r in records:
        month = F.day(r["month"])
        series = F.text(r["series"]) or ""
        if not F.within(month, args.start, args.end) or (keep and series not in keep):
            continue
        rows.append(
            common.row(
                RegionalSurveyIndex,
                {
                    "bank": F.text(r["bank"]),
                    "survey": F.text(r["survey"]),
                    "series": series,
                    "label": F.text(r["label"]) or series,
                    "seasonally_adjusted": bool(F.flag(r["seasonally_adjusted"])),
                    "month": month,
                    "value": F.number(r["value"]),
                    "unit": F.text(r["unit"]) or "diffusion_index",
                    "release": F.instant(r["release"]),
                    "release_estimated": bool(F.flag(r["release_estimated"])),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
        )
    rows.sort(key=lambda r: (r.bank, r.survey, r.series, r.month))
    prov = common.provenance(
        ctx,
        provider="regionalfed",
        route=ROUTES["regional"],
        datasets=("regionalfed.surveys",),
        request={
            "bank": args.bank,
            "survey": args.survey,
            "series": args.series,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
        },
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )
    return common.output(ctx, s, RegionalSurveyIndex, rows, prov)


async def credit_trace_aggregates(ctx: ToolContext, args: TraceInputs) -> ToolOutput:
    from datetime import timedelta

    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=today - timedelta(days=30),
        default_end=today,
        max_days=3660,
    )
    metrics = set(args.metrics or [])

    def work(session: common.Session):
        session.refresh("finra.trace_aggregates", {"report": args.report, "since": start.isoformat()})
        return F.records(session.read("finra.trace_aggregates", as_of=as_of, report=args.report))

    records, session = await common.run(ctx, s, work, source="finra")
    rows = []
    for r in records:
        d = F.day(r["date"])
        category = F.text(r["product_category"]) or ""
        metric = F.text(r["metric"]) or ""
        if not F.within(d, start, end) or (metrics and metric not in metrics):
            continue
        if args.product_category and category != args.product_category:
            continue
        rows.append(
            common.row(
                TraceAggregate,
                {
                    "report": args.report,
                    "date": d,
                    "product_category": category,
                    "breakdown": F.text(r["breakdown"]) or "",
                    "metric": metric,
                    "value": F.number(r["value"]),
                    "stamp_estimated": bool(F.flag(r["stamp_estimated"])),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
        )
    rows.sort(key=lambda r: (r.date, r.product_category, r.breakdown, r.metric))
    prov = common.provenance(
        ctx,
        provider="finra",
        route=ROUTES["trace"],
        datasets=("finra.trace_aggregates",),
        request={
            "report": args.report,
            "start": start,
            "end": end,
            "product_category": args.product_category,
            "metrics": args.metrics,
            "as_of": args.as_of,
        },
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )
    return common.output(ctx, s, TraceAggregate, rows, prov)


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="macro_series",
        capability="macro",
        title="Economic series (FRED/ALFRED)",
        description=common.describe(
            "FRED economic series observations with true ALFRED vintages: CPI (CPIAUCSL), core CPI (CPILFESL), "
            "the unemployment rate (UNRATE), payrolls (PAYEMS), claims (ICSA), fed funds (FEDFUNDS), Treasury "
            "yields (DGS10), spreads (T10Y2Y), real GDP (GDPC1), PCE prices (PCEPI) and any other FRED id. "
            "value is in the publisher's unit (units; rates stay in percent as FRED publishes them); a '.' "
            "is null with reason no_data. knowledge_time is 17:00 St. Louis time (22:00 UTC, 23:00 in winter) "
            "of the day FRED posted the release a value first appeared in (a revision: the release that revised "
            "it): its ALFRED date, or later for a series FRED posts late (e.g. ICE BofA spreads, VIXCLS: a "
            "business day; UMCSENT: weeks), so an as_of on that day sees it only from then. "
            "include_vintages=true returns every stored revision. "
            "Needs FRED_API_KEY (free).",
            "vintage",
        ),
        readme="FRED/ALFRED series with vintages and as-of reads",
        input_model=SeriesInputs,
        output_model=EconomicObservation,
        provider="fred",
        route="omni fred.series <- api.stlouisfed.org/fred/series/observations, series/vintagedates (ALFRED vintages)",
        handler=macro_series,
        golden_test=GOLDEN,
        env=("FRED_API_KEY",),
    ),
    ToolSpec(
        name="macro_series_catalog",
        capability="macro",
        title="FRED series catalogue",
        description=(
            "The FRED series marketlens-data describes (name, category, unit label, chart type, polarity), "
            "searchable by a substring of the id or name and by category. Local: no fetch, no key. Use it to "
            "find a series id for macro_series."
        ),
        readme="Names, categories and units of the catalogued FRED series",
        input_model=CatalogInputs,
        output_model=EconomicSeriesInfo,
        provider="local",
        route="omni catalog.series_meta (local, no fetch)",
        handler=macro_series_catalog,
        golden_test=GOLDEN,
    ),
    ToolSpec(
        name="macro_bls_series",
        capability="macro",
        title="BLS series",
        description=common.describe(
            "BLS monthly and quarterly series (CPI-U CUUR0000SA0, core CPI CUUR0000SA0L1E, unemployment "
            "LNS14000000, payrolls CES0000000001, JOLTS openings JTS000000000000000JOL, ECI CIS1010000000000I). "
            "BLS serves only the latest revision and no release date, so "
            "knowledge_time is the latest release in BLS's archive at the value's first fetch (08:30 ET; "
            "JOLTS 10:00 ET; real earnings: CPI or jobs report), else that fetch (the archive lagging or "
            "unread, a state's JOLTS, SA CPI from December's to January's CPI): history older than the first sync "
            "takes that sync's latest release, so an earlier as_of returns none (first prints: "
            "macro_series). A later revision is a new vintage at its release, else at its fetch. BLS_API_KEY is "
            "optional.",
            "lagged",
        ),
        readme="BLS monthly and quarterly series (CPI, unemployment, payrolls, JOLTS, ECI)",
        input_model=BlsInputs,
        output_model=EconomicObservation,
        provider="bls",
        route="omni bls.timeseries <- api.bls.gov/publicAPI/v2/timeseries/data",
        handler=macro_bls_series,
        golden_test=GOLDEN,
        env=("BLS_API_KEY",),
    ),
    ToolSpec(
        name="macro_bea_table",
        capability="macro",
        title="BEA NIPA table",
        description=common.describe(
            "Every line of a BEA NIPA table (GDP T10101, PCE, personal income, corporate profits) at annual, "
            "quarterly or monthly frequency; series_id is the line's series code, title its description, "
            "units the publisher's unit, for a level with BEA's scale and measure ('Level, millions of Current "
            "Dollars'); just 'Level' was stored without the scale, which is then not known (check the table's "
            "header). BEA serves the latest revision only, so knowledge_time is the table's last "
            "revision date, never before the period's first release; history older than the first sync "
            "takes that sync's last revision date, so an earlier as_of returns none of it (GDP vintages: "
            "macro_series). A revision seen later is a new vintage. Needs BEA_API_KEY (free).",
            "lagged",
        ),
        readme="BEA NIPA tables (GDP, PCE, income, profits)",
        input_model=BeaInputs,
        output_model=EconomicObservation,
        provider="bea",
        route="omni bea.nipa <- apps.bea.gov/api/data (NIPA GetData)",
        handler=macro_bea_table,
        golden_test=GOLDEN,
        env=("BEA_API_KEY",),
    ),
    ToolSpec(
        name="macro_release_schedule",
        capability="macro",
        title="BLS release schedule",
        description=common.describe(
            "When BLS publishes the CPI and the Employment Situation (jobs report): reference month, release "
            "date and the official release instant in UTC, from BLS's own schedule pages. Keyless.",
            "forward_known",
            stored=False,
        ),
        readme="CPI and jobs-report release dates and instants",
        input_model=ScheduleInputs,
        output_model=ReleaseScheduleEntry,
        provider="bls",
        route=ROUTES["schedule"],
        handler=macro_release_schedule,
        golden_test=GOLDEN,
    ),
)

SPECS = (
    *SPECS,
    ToolSpec(
        name="macro_census_trade",
        capability="macro",
        title="US trade by commodity and partner (Census)",
        description=common.describe(
            "US goods exports (total value) and general imports by Harmonized System code and partner "
            "country, monthly from 2010, in US dollars, not seasonally adjusted, from the Census Bureau's "
            "international trade API: e.g. HS 8542 integrated circuits to China (5700), Taiwan (5830), the EU "
            "(0003) or all countries (TOTAL); the quantity where the code is HS10. knowledge_time is the "
            "FT-900 release of the month (08:30 ET, Census's calendar), or for a year Census has revised "
            "(the annual revision with April's statistics in June) that revision; stamp_estimated marks a "
            "month the calendar does not date. Needs CENSUS_API_KEY (free).",
            "lagged",
        ),
        readme="US exports and imports by HS commodity and partner country (semis and hardware codes)",
        input_model=TradeInputs,
        output_model=TradeFlow,
        provider="census",
        route=ROUTES["trade"],
        handler=macro_census_trade,
        golden_test=GOLDEN,
        env=("CENSUS_API_KEY",),
    ),
    ToolSpec(
        name="macro_boj_series",
        capability="macro",
        title="Bank of Japan series",
        description=common.describe(
            "Bank of Japan Time-Series Data Search series by database and code: the uncollateralized "
            "overnight call rate, the 9:00 dollar/yen rate, effective exchange rates, the monetary base, the "
            "Bank's balance sheet (total assets, JGB and ETF holdings) and the Tankan business conditions DIs "
            "(actual and next-quarter forecast). value is in the Bank's own unit (units: percent per annum, "
            "100 million yen, % points). JGB yields are not in this database. knowledge_time is the earliest "
            "of period end + 35 days (a daily value + 7), the end of the series' last-update date (JST) and "
            "the fetch; a revision from the update that states it. Keyless.",
            "lagged",
        ),
        readme="Bank of Japan series: call rate, yen, monetary base, balance sheet, Tankan",
        input_model=BojInputs,
        output_model=CentralBankObservation,
        provider="boj",
        route=ROUTES["boj"],
        handler=macro_boj_series,
        golden_test=GOLDEN,
    ),
    ToolSpec(
        name="macro_boe_series",
        capability="macro",
        title="Bank of England series",
        description=common.describe(
            "Bank of England database (IADB) series by code: Bank Rate (IUDBEDR), SONIA (IUDSOIA), the 5-, "
            "10- and 20-year nominal par gilt yields (IUDSNPY, IUDMNPY, IUDLNPY), M4 (LPMAUYN) and lending, "
            "the spot dollar and euro rates and the sterling ERI. The CSV states no unit (units is null: "
            "rates and yields are percent per annum, M4 and lending sterling millions). knowledge_time is "
            "the earlier of period end + 40 days (a daily value + 7) and the fetch that saw it; a revision "
            "from the fetch. Keyless.",
            "lagged",
        ),
        readme="Bank of England series: Bank Rate, SONIA, gilt yields, M4, sterling FX",
        input_model=BoeInputs,
        output_model=CentralBankObservation,
        provider="boe",
        route=ROUTES["boe"],
        handler=macro_boe_series,
        golden_test=GOLDEN,
    ),
    ToolSpec(
        name="macro_regional_fed_surveys",
        capability="macro",
        title="Regional Fed business surveys",
        description=common.describe(
            "The Richmond Fed's manufacturing and non-manufacturing surveys and the Kansas City Fed's "
            "manufacturing and services surveys, monthly diffusion indexes (composite, shipments or "
            "production, new orders, employment, prices; Kansas City versus a month ago, a year ago and "
            "expected in six months), official stand-ins for the licensed PMIs (Empire State, Dallas: "
            "macro_series). knowledge_time is the release on the bank's schedule "
            "(Richmond 10:10 ET, Kansas City the end of the listed date), history the schedules no longer "
            "list a week after the month (release_estimated), and a seasonally adjusted month of an earlier "
            "year than the newest at the newest year's January release (new seasonal factors). Keyless.",
            "lagged",
        ),
        readme="Richmond and Kansas City Fed manufacturing and services surveys",
        input_model=RegionalSurveyInputs,
        output_model=RegionalSurveyIndex,
        provider="regionalfed",
        route=ROUTES["regional"],
        handler=macro_regional_fed_surveys,
        golden_test=GOLDEN,
    ),
    ToolSpec(
        name="credit_trace_aggregates",
        capability="macro",
        title="FINRA TRACE market aggregates",
        description=common.describe(
            "FINRA's TRACE bond market statistics, one row per metric: corporate, 144A and agency market "
            "breadth (issues traded, advances, declines, unchanged, 52-week highs and lows, volume) and "
            "sentiment (by trade type), Treasury daily and monthly aggregates (securities traded and dollar "
            "volume, ATS/inter-dealer and dealer-to-customer, by years to maturity and benchmark). "
            "knowledge_time is a conservative rule (stamp_estimated): 23:59 ET of the next business day "
            "(Treasury daily: the second; monthly: the end of the next month), never after the fetch. Needs "
            "FINRA_API_CLIENT_ID and FINRA_API_CLIENT_SECRET (a free FINRA developer account's API client).",
            "lagged",
        ),
        readme="FINRA TRACE bond market breadth, sentiment and Treasury aggregates",
        input_model=TraceInputs,
        output_model=TraceAggregate,
        provider="finra",
        route=ROUTES["trace"],
        handler=credit_trace_aggregates,
        golden_test=GOLDEN,
        env=("FINRA_API_CLIENT_ID", "FINRA_API_CLIENT_SECRET"),
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "macro_census_trade": ("census.trade",),
    "macro_boj_series": ("boj.series",),
    "macro_boe_series": ("boe.series",),
    "macro_regional_fed_surveys": ("regionalfed.surveys",),
    "credit_trace_aggregates": ("finra.trace_aggregates",),
    "macro_series": ("fred.series",),
    "macro_series_catalog": (),
    "macro_bls_series": ("bls.timeseries",),
    "macro_bea_table": ("bea.nipa",),
    "macro_release_schedule": ("bls.cpi_schedule", "bls.empsit_schedule"),
}
