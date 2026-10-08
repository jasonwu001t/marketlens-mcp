"""Economic series: FRED/ALFRED with vintages, BLS, BEA NIPA tables, BLS
release schedules and the FRED series catalogue (capability ``macro``)."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import EconomicObservation, EconomicSeriesInfo, ReleaseScheduleEntry

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
    start: date | None = Field(default=None, description="First month (inclusive). Default: 10 years back.")
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


NPS = AbsenceCode.NOT_PROVIDED_BY_SOURCE
NA = AbsenceCode.NOT_APPLICABLE
ROUTES = {
    "fred": "omni fred.series <- api.stlouisfed.org/fred/series/observations, series/vintagedates (ALFRED vintages)",
    "catalog": "omni catalog.series_meta (local, no fetch)",
    "bls": "omni bls.timeseries <- api.bls.gov/publicAPI/v2/timeseries/data",
    "bea": "omni bea.nipa <- apps.bea.gov/api/data (NIPA GetData)",
    "schedule": "omni bls.cpi_schedule, bls.empsit_schedule <- www.bls.gov/schedule/news_release",
}
SCHEDULES = {"cpi": "bls.cpi_schedule", "employment_situation": "bls.empsit_schedule"}
#: marketlens-data keeps BEA's CL_UNIT but not its UNIT_MULT, so a level's scale is lost.
BEA_LEVEL_NOTE = (
    "BEA states each value's multiplier (UNIT_MULT) separately and it is not stored, so a value whose units "
    "is 'Level' is not in ones: NIPA dollar levels are usually in millions of dollars. Check the table's "
    "header on bea.gov before reading a magnitude."
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
    start = start.replace(day=1)  # monthly observations are dated the first of their month
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
            if not F.within(d, start, end):
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
                            "period": F.text(r["period"]),
                            "frequency": "M",
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
                    "units": F.text(r["unit"]),
                    "table_name": args.table_name,
                    "line_number": line,
                    "knowledge_time": F.instant(r["knowledge_time"]),
                    "pit": "lagged",
                },
                codes={"title": NPS, "units": NPS},
            )
        )
    rows.sort(key=lambda r: (r.line_number or 0, r.series_id, r.date))
    levels = any((r.units or "").strip().lower() == "level" for r in rows)
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
    notes = [BEA_LEVEL_NOTE] if levels else []
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
            "is null with reason no_data. knowledge_time is 22:00 UTC of the day FRED posted the release a value "
            "first appeared in (a revision: the release that revised it): its ALFRED date, or later for a series "
            "FRED posts late (e.g. ICE BofA spreads, VIXCLS: a business day; UMCSENT: 43 days), so an "
            "as_of on that day sees it only from then. include_vintages=true returns every stored revision. "
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
            "Bureau of Labor Statistics monthly series (CPI-U CUUR0000SA0, core CPI CUUR0000SA0L1E, the "
            "unemployment rate LNS14000000, payrolls CES0000000001, JOLTS openings JTS000000000000000JOL), "
            "fetched in decade blocks. value is as BLS publishes it; BLS serves only the latest revision and "
            "no release date, so knowledge_time is an approximation (period + release lag). BLS_API_KEY is "
            "optional (raises BLS's daily limit).",
            "lagged",
        ),
        readme="BLS monthly series (CPI, unemployment, payrolls, JOLTS)",
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
            "units the publisher's unit. BEA states a value's multiplier separately and it is not stored: a "
            "units of 'Level' is not in ones (NIPA dollar levels are usually millions of dollars; check the "
            "table's header). BEA serves the latest revision only, so knowledge_time is period end plus a "
            "release lag. Needs BEA_API_KEY (free).",
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
        route="omni bls.cpi_schedule, bls.empsit_schedule <- www.bls.gov/schedule/news_release",
        handler=macro_release_schedule,
        golden_test=GOLDEN,
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "macro_series": ("fred.series",),
    "macro_series_catalog": (),
    "macro_bls_series": ("bls.timeseries",),
    "macro_bea_table": ("bea.nipa",),
    "macro_release_schedule": ("bls.cpi_schedule", "bls.empsit_schedule"),
}
