"""Banks and money market funds, the plumbing beside the Fed's: the FDIC's
Call Report financials of the banks listed companies own and its annual
industry aggregates, and the SEC's monthly money market fund statistics
(capability ``fed_treasury``). The FDIC is keyless; the SEC wants a contact in
SEC_CONTACT_EMAIL, as every sec_ tool does."""

from __future__ import annotations

import os
from datetime import date

from pydantic import Field

from marketlens_mcp.plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import BankFinancials, BankIndustrySummary, MoneyMarketFundStatistic

from .. import frames as F
from .. import runtime
from . import common
from .common import AsOf, Inputs, Tickers

GOLDEN = "tests/data/test_banks.py"
NATION = "All States and Territories"
NA = AbsenceCode.NOT_APPLICABLE
ROUTES = {
    "bank_fdic_financials": (
        "omni fdic.financials <- api.fdic.gov/banks/financials (the CERTs of /banks/institutions' "
        "regulatory top holders)"
    ),
    "bank_fdic_industry": "omni fdic.industry_summary <- api.fdic.gov/banks/summary",
    "funds_mmf_statistics": (
        "omni sec.mmf_statistics <- www.sec.gov (Money Market Fund Statistics supporting-data workbooks)"
    ),
}
#: fdic.financials' dollar columns (stored in USD) and its percent ratios (as fractions here).
USD_COLUMNS = (
    "total_assets",
    "total_deposits",
    "domestic_deposits",
    "insured_deposits_est",
    "uninsured_deposits_est",
    "gross_loans",
    "net_loans",
    "total_equity",
    "net_income_ytd",
    "net_interest_income_ytd",
    "net_charge_offs_ytd",
    "noncurrent_loans",
    "htm_securities_amortized_cost",
    "htm_securities_fair_value",
    "afs_securities_amortized_cost",
    "afs_securities_fair_value",
    "htm_unrealized_gain",
    "afs_unrealized_gain",
)
PERCENT_COLUMNS = (
    "net_interest_margin",
    "roa",
    "roe",
    "leverage_ratio",
    "tier1_capital_ratio",
    "cet1_ratio",
    "total_capital_ratio",
    "noncurrent_loans_ratio",
    "net_charge_off_ratio",
)
SUMMARY_USD = (
    "total_assets",
    "total_deposits",
    "gross_loans",
    "net_loans",
    "total_equity",
    "net_income",
    "net_interest_income",
    "noncurrent_loans",
    "net_charge_offs",
)
#: sec.mmf_statistics' units -> (this tool's unit, the factor to it).
MMF_UNITS = {
    "usd_billions": ("USD", 1e9),
    "percent": ("fraction", 0.01),
    "fraction": ("fraction", 1.0),
    "days": ("days", 1.0),
}
MMF_TABLES = (
    "2.1",
    "2.6",
    "3.1",
    "3.5",
    "3.9",
    "4.3",
    "5.1",
    "5.3",
    "6.1",
    "6.3",
    "7.1",
    "7.4",
    "7.7",
    "8.2",
)


class FinancialsInputs(Inputs):
    tickers: Tickers = Field(
        description="Listed companies (JPM, BAC, SCHW, ...); each maps to the FDIC-insured banks its "
        "holding company owns. A company that owns none answers no row."
    )
    start: date | None = Field(default=None, description="First quarter end. Default: 3 years ago.")
    end: date | None = Field(default=None, description="Last quarter end. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class IndustryInputs(Inputs):
    start_year: int | None = Field(default=None, ge=1934, le=2100, description="Default: 10 years ago.")
    end_year: int | None = Field(default=None, ge=1934, le=2100, description="Default: this year.")
    location: str = Field(
        default=NATION, max_length=60, description=f"A state or territory's name, or '{NATION}' (the nation)."
    )
    as_of: AsOf | None = common.as_of_field()


class MmfInputs(Inputs):
    tables: list[str] = Field(
        default_factory=lambda: ["2.1", "7.1", "8.2"],
        min_length=1,
        description="The workbook's tables: 2.1 net assets, 2.6 total assets, 3.1 / 3.5 / 3.9 gross "
        "subscriptions, redemptions and net flows ($), 4.3 seven-day net yield, 5.1 WAM, 5.3 WAL, "
        "6.1 / 6.3 daily and weekly liquid assets, 7.1 / 7.4 / 7.7 investments of all, government and prime "
        "MMFs by category (Treasury, agency, repo, CDs, commercial paper, ...), 8.2 repos with the Federal "
        "Reserve (its ON RRP). Default: 2.1, 7.1, 8.2.",
    )
    categories: list[str] | None = Field(default=None, description="Only these categories. Default: all.")
    start: date | None = Field(default=None, description="First month. Default: 2 years ago.")
    end: date | None = Field(default=None, description="Last month. Default: today.")
    as_of: AsOf | None = common.as_of_field()


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


async def bank_fdic_financials(ctx: ToolContext, args: FinancialsInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 3), default_end=today
    )
    s = runtime.settings_of(ctx.settings)
    common.check_sec_contact(s, os.environ)
    as_of = common.as_of_of(args.as_of)
    tickers = sorted(args.tickers)

    def work(session: common.Session):
        session.refresh("fdic.financials", {"ticker": ",".join(tickers)})
        return F.records(session.read("fdic.financials", as_of=as_of))

    records, session = await common.run(ctx, s, work, source="fdic")
    wanted = set(tickers)
    rows = []
    for r in records:
        if F.text(r["ticker"]) not in wanted or not F.within(F.day(r["report_date"]), start, end):
            continue
        values = {
            "cert": F.integer(r["cert"]),
            "report_date": F.day(r["report_date"]),
            "ticker": F.text(r["ticker"]),
            "name": F.text(r["name"]) or "",
            "holding_company": F.text(r["holding_company"]),
            **{c: F.number(r[c]) for c in USD_COLUMNS},
            **{c: F.fraction(r[c]) for c in PERCENT_COLUMNS},
            "knowledge_time": F.instant(r["knowledge_time"]),
        }
        rows.append(common.row(BankFinancials, values, codes={"holding_company": NA}))
    rows.sort(key=lambda r: (r.ticker or "", r.cert, r.report_date))
    prov = _prov(
        ctx,
        "bank_fdic_financials",
        "fdic",
        "fdic.financials",
        {"tickers": tickers, "start": start, "end": end, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    notes = []
    missing = sorted(wanted - {r.ticker for r in rows})
    if missing:
        notes.append(
            f"No FDIC-insured bank found for {', '.join(missing)} in the window: the company's SEC name "
            "matches no bank's regulatory top holder (most listed companies own no bank)."
        )
    return common.output(
        ctx, s, BankFinancials, rows, prov, notes=[*notes, *common.sec_contact_notes(s, os.environ)]
    )


async def bank_fdic_industry(ctx: ToolContext, args: IndustryInputs) -> ToolOutput:
    today = ctx.now().date()
    first = args.start_year or today.year - 10
    last = args.end_year or today.year
    if first > last:
        raise ToolError("invalid_arguments", f"start_year ({first}) must not be after end_year ({last}).")
    s = runtime.settings_of(ctx.settings)
    as_of = common.as_of_of(args.as_of)

    def work(session: common.Session):
        session.refresh("fdic.industry_summary", {})
        return F.records(session.read("fdic.industry_summary", as_of=as_of))

    records, session = await common.run(ctx, s, work, source="fdic")
    location = args.location.strip().lower()
    rows = []
    for r in records:
        year = F.integer(r["year"])
        if year is None or not first <= year <= last or (F.text(r["location"]) or "").lower() != location:
            continue
        rows.append(
            common.row(
                BankIndustrySummary,
                {
                    "year_end": date(year, 12, 31),
                    "location": F.text(r["location"]),
                    "charter_class": F.text(r["charter_class"]),
                    "institutions": F.integer(r["institutions"]),
                    "offices": F.integer(r["offices"]),
                    "branches": F.integer(r["branches"]),
                    **{c: F.number(r[c]) for c in SUMMARY_USD},
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
        )
    rows.sort(key=lambda r: (r.year_end, r.charter_class))
    prov = _prov(
        ctx,
        "bank_fdic_industry",
        "fdic",
        "fdic.industry_summary",
        {"start_year": first, "end_year": last, "location": args.location, "as_of": args.as_of},
        as_of,
        rows,
        session,
    )
    return common.output(ctx, s, BankIndustrySummary, rows, prov)


async def funds_mmf_statistics(ctx: ToolContext, args: MmfInputs) -> ToolOutput:
    today = ctx.now().date()
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 2), default_end=today
    )
    unknown = sorted(set(args.tables) - set(MMF_TABLES))
    if unknown:
        raise ToolError(
            "invalid_arguments",
            f"unknown table(s) {', '.join(unknown)}; choose from {', '.join(MMF_TABLES)}.",
        )
    s = runtime.settings_of(ctx.settings)
    common.check_sec_contact(s, os.environ)
    as_of = common.as_of_of(args.as_of)

    def work(session: common.Session):
        session.refresh("sec.mmf_statistics", {})
        return F.records(session.read("sec.mmf_statistics", as_of=as_of))

    records, session = await common.run(ctx, s, work, source="sec")
    tables = set(args.tables)
    categories = {c.lower() for c in args.categories or ()}
    rows = []
    for r in records:
        month = F.day(r["month"])
        if F.text(r["table"]) not in tables or not F.within(month, date(start.year, start.month, 1), end):
            continue
        if categories and (F.text(r["category"]) or "").lower() not in categories:
            continue
        unit, factor = MMF_UNITS[F.text(r["unit"]) or ""]
        value = F.number(r["value"])
        rows.append(
            common.row(
                MoneyMarketFundStatistic,
                {
                    "table": F.text(r["table"]),
                    "table_title": F.text(r["table_title"]) or "",
                    "category": F.text(r["category"]) or "",
                    "item": F.text(r["item"]) or "",
                    "month": month,
                    "value": None if value is None else round(value * factor, 6),
                    "unit": unit,
                    "file": F.text(r["file"]) or "",
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
            )
        )
    order = {t: i for i, t in enumerate(MMF_TABLES)}
    rows.sort(key=lambda r: (order[r.table], r.month, r.category, r.item))
    prov = _prov(
        ctx,
        "funds_mmf_statistics",
        "sec",
        "sec.mmf_statistics",
        {
            "tables": args.tables,
            "categories": args.categories,
            "start": start,
            "end": end,
            "as_of": args.as_of,
        },
        as_of,
        rows,
        session,
    )
    return common.output(
        ctx, s, MoneyMarketFundStatistic, rows, prov, notes=common.sec_contact_notes(s, os.environ)
    )


HANDLERS = {
    "bank_fdic_financials": bank_fdic_financials,
    "bank_fdic_industry": bank_fdic_industry,
    "funds_mmf_statistics": funds_mmf_statistics,
}


def _spec(name, title, description, readme, inputs, model, provider, *, env=()):
    return ToolSpec(
        name=name,
        capability="fed_treasury",
        title=title,
        description=description,
        readme=readme,
        input_model=inputs,
        output_model=model,
        provider=provider,
        route=ROUTES[name],
        handler=HANDLERS[name],
        golden_test=GOLDEN,
        env=env,
        output_risk="api_structured",
    )


SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "bank_fdic_financials",
        "FDIC bank financials",
        common.describe(
            "Quarterly Call Report financials (FDIC BankFind) of the insured banks a listed company owns, "
            "its SEC name matched to the FDIC's top holder: assets, deposits (insured and uninsured), loans, "
            "equity, YTD income and charge-offs, noncurrent loans, HTM and AFS securities at cost and fair "
            "value with the unrealized gain (< 0 a loss), in USD; NIM, ROA, ROE and capital ratios as "
            "fractions. A quarter is knowable from the first day of the third month after it (after the "
            "Quarterly Banking Profile). Keyless; names come from SEC's ticker list. Without "
            "SEC_CONTACT_EMAIL a placeholder contact is sent (SEC fair access) and the answer says so.",
            "lagged",
        ),
        "Call Report financials of the banks a company owns",
        FinancialsInputs,
        BankFinancials,
        "fdic",
        env=("SEC_CONTACT_EMAIL",),
    ),
    _spec(
        "bank_fdic_industry",
        "FDIC industry aggregates",
        common.describe(
            "The FDIC's annual aggregates of insured institutions (1934 on) for a year-end, a location (a "
            "state, or All States and Territories) and a charter class (CB commercial banks, SI savings "
            "institutions): institutions, offices, branches, the year-end balance sheet and the year's "
            "income in USD. Keyless.",
            "lagged",
        ),
        "Annual FDIC industry aggregates",
        IndustryInputs,
        BankIndustrySummary,
        "fdic",
    ),
    _spec(
        "funds_mmf_statistics",
        "Money market fund statistics",
        common.describe(
            "The SEC's monthly Money Market Fund Statistics (Form N-MFP aggregates, 2010-12 on): assets, "
            "flows and yields by fund type, WAM and WAL, liquid assets, the investments by category "
            "(Treasury and agency debt, repo, CDs, commercial paper) and the repos with the Federal Reserve "
            "(its overnight reverse repo facility). Dollars in USD, yields and shares as fractions, WAM and "
            "WAL in days. Each value knowable from the day the SEC posted the workbook that first stated it. "
            "Without SEC_CONTACT_EMAIL a placeholder contact is sent (SEC fair access) and the answer says so.",
            "vintage",
        ),
        "SEC money market fund statistics (N-MFP)",
        MmfInputs,
        MoneyMarketFundStatistic,
        "sec",
        env=("SEC_CONTACT_EMAIL",),
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "bank_fdic_financials": ("fdic.financials",),
    "bank_fdic_industry": ("fdic.industry_summary",),
    "funds_mmf_statistics": ("sec.mmf_statistics",),
}
