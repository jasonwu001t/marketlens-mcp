"""SEC EDGAR: filings, XBRL facts, point-in-time fundamentals, earnings
releases and press-release EPS, insider trades, 13F holdings and fund
N-PORT reports (capability ``filings``). Every fetch names the operator's
contact in its User-Agent (SEC_CONTACT_EMAIL), as EDGAR's fair-access rule
asks."""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import (
    EarningsFigure,
    EarningsRelease,
    Filing,
    FundamentalValue,
    FundHolding,
    FundReport,
    InsiderTransaction,
    InstitutionalHolding,
    XbrlFact,
)

from .. import frames as F
from .. import runtime
from . import common
from .common import AsOf, Inputs, Ticker

GOLDEN = "tests/data/test_sec.py"
ENV = ("SEC_CONTACT_EMAIL",)
CONTACT = "Fetching needs SEC_CONTACT_EMAIL (SEC fair access)."

FUNDAMENTAL_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "d_and_a",
    "ebit",
    "ebitda",
    "net_income",
    "eps_diluted",
    "shares_diluted",
    "shares_outstanding",
    "operating_cash_flow",
    "capex",
    "free_cash_flow",
    "dividends_per_share",
    "total_equity",
    "total_debt",
    "cash",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "gross_margin",
    "operating_margin",
    "net_margin",
)
FundamentalMetric = Literal[
    "revenue",
    "gross_profit",
    "operating_income",
    "d_and_a",
    "ebit",
    "ebitda",
    "net_income",
    "eps_diluted",
    "shares_diluted",
    "shares_outstanding",
    "operating_cash_flow",
    "capex",
    "free_cash_flow",
    "dividends_per_share",
    "total_equity",
    "total_debt",
    "cash",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "gross_margin",
    "operating_margin",
    "net_margin",
]

_CONCEPT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,200}$")
_FORM = re.compile(r"^[A-Z0-9][A-Z0-9 /-]{0,19}$")
_CUSIP = re.compile(r"^[0-9A-Z]{9}$")
_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def _concepts(values: list[str]) -> list[str]:
    for v in values:
        if not _CONCEPT.match(v):
            raise ValueError(f"{v!r} is not an XBRL concept name (e.g. Revenues, NetIncomeLoss)")
    return list(dict.fromkeys(values))


def _forms(values: list[str]) -> list[str]:
    out = []
    for v in values:
        s = v.strip().upper()
        if not _FORM.match(s):
            raise ValueError(f"{v!r} is not an SEC form type (e.g. 10-K, 10-Q, 8-K, 4)")
        out.append(s)
    return list(dict.fromkeys(out))


def _cusips(values: list[str]) -> list[str]:
    out = []
    for v in values:
        s = v.strip().upper()
        if not _CUSIP.match(s):
            raise ValueError(f"{v!r} is not a CUSIP (9 letters and digits)")
        out.append(s)
    return list(dict.fromkeys(out))


def _cik(value: str) -> str:
    s = value.strip()
    if not s.isdigit() or not 1 <= len(s) <= 10 or int(s) == 0:
        raise ValueError(f"{value!r} is not a CIK (1-10 digits)")
    return f"{int(s):010d}"


def _accession(value: str) -> str:
    s = value.strip()
    if not _ACCESSION.match(s):
        raise ValueError(f"{value!r} is not an accession number (0000000000-00-000000)")
    return s


Forms = Annotated[list[str], Field(min_length=1, max_length=20), AfterValidator(_forms)]
TICKER_DOC = "Company ticker, SEC style (brk.b becomes BRK-B)."


class FilingsInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    forms: Forms | None = Field(default=None, description="Only these forms, e.g. [10-K, 10-Q, 8-K, 4].")
    start: date | None = Field(default=None, description="First filing date. Default: 2 years ago.")
    end: date | None = Field(default=None, description="Last filing date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class FactsInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    metrics: Annotated[list[str], Field(max_length=50), AfterValidator(_concepts)] | None = Field(
        default=None,
        description="XBRL concepts (0-50), e.g. Revenues, NetIncomeLoss, EarningsPerShareDiluted. Default: all.",
    )
    taxonomy: Literal["us-gaap", "dei", "ifrs-full", "srt"] | None = Field(
        default=None, description="Only this taxonomy. Default: any."
    )
    forms: Forms | None = Field(default=None, description="Only facts filed on these forms, e.g. [10-K].")
    start: date | None = Field(default=None, description="First period end (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last period end (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()
    include_vintages: bool = Field(
        default=False, description="Every filed version of each fact, not the latest."
    )


class FundamentalsInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    metrics: list[FundamentalMetric] | None = Field(
        default=None, max_length=23, description="Only these of the 23 metrics. Default: all."
    )
    start: date | None = Field(default=None, description="First period end (inclusive). Default: all.")
    end: date | None = Field(default=None, description="Last period end (inclusive). Default: all.")
    as_of: AsOf | None = common.as_of_field()


class ReleasesInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    start: date | None = Field(default=None, description="First filing date. Default: 3 years ago.")
    end: date | None = Field(default=None, description="Last filing date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class FiguresInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    metrics: list[Literal["eps_diluted_gaap", "eps_adjusted"]] = Field(
        default_factory=lambda: ["eps_diluted_gaap", "eps_adjusted"], min_length=1
    )
    start: date | None = Field(default=None, description="First acceptance date. Default: 3 years ago.")
    end: date | None = Field(default=None, description="Last acceptance date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class InsiderInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    forms: list[Literal["4", "144"]] = Field(
        default_factory=lambda: ["4", "144"],
        min_length=1,
        description="4 (executed trades) and/or 144 (notices of intended sales); amendments included.",
    )
    start: date | None = Field(default=None, description="First transaction date. Default: a year ago.")
    end: date | None = Field(default=None, description="Last transaction date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class ThirteenFInputs(Inputs):
    filer_cik: Annotated[str, AfterValidator(_cik)] = Field(
        description="The manager's CIK (1-10 digits), e.g. 1067983."
    )
    period_end: date | None = Field(
        default=None, description="Quarter end. Default: the latest quarter stored at as_of."
    )
    cusips: Annotated[list[str], Field(min_length=1, max_length=100), AfterValidator(_cusips)] | None = Field(
        default=None, description="Only these CUSIPs."
    )
    as_of: AsOf | None = common.as_of_field()


class NportInputs(Inputs):
    ticker: Ticker = Field(description="Fund or ETF symbol, e.g. VOO.")
    as_of: AsOf | None = common.as_of_field()


class FundHoldingsInputs(Inputs):
    ticker: Ticker = Field(description="Fund or ETF symbol, e.g. VOO.")
    accession_number: Annotated[str, AfterValidator(_accession)] | None = Field(
        default=None, description="One N-PORT filing. Default: the latest report at as_of."
    )
    as_of: AsOf | None = common.as_of_field()


NPS = AbsenceCode.NOT_PROVIDED_BY_SOURCE
NA = AbsenceCode.NOT_APPLICABLE
INSUFFICIENT = AbsenceCode.INSUFFICIENT_DATA
ROUTES = {
    "sec_filings": "omni sec.submissions <- data.sec.gov/submissions",
    "sec_xbrl_facts": "omni sec.company_facts <- data.sec.gov/api/xbrl/companyfacts",
    "sec_fundamentals": "omni sec.fundamentals <- sec.company_facts (computed locally)",
    "sec_earnings_releases": "omni sec.earnings_releases <- data.sec.gov/submissions (8-K Item 2.02)",
    "sec_earnings_figures": "omni sec.earnings_press_release_figures <- www.sec.gov/Archives (EX-99.1)",
    "sec_insider_trades": "omni sec_insider.transactions <- www.sec.gov/Archives (Forms 4 and 144)",
    "sec_13f_holdings": "omni sec13f.holdings <- www.sec.gov/Archives (13F-HR)",
    "sec_fund_nport": "omni sec.fund_nport <- www.sec.gov/Archives (N-PORT)",
    "sec_fund_holdings": "omni sec.fund_nport_holdings <- www.sec.gov/Archives (N-PORT)",
}
#: marketlens-data's reason for a null fundamental -> why the value is None.
REASON_CODES = {
    "no_us_gaap": NPS,
    "concept_not_filed": NPS,
    "non_usd": NA,
    "nonpositive_denominator": NA,
    "insufficient_periods": INSUFFICIENT,
    "missing_input": INSUFFICIENT,
    "period_mismatch": INSUFFICIENT,
}
FORM_GROUPS = {"4": {"4", "4/A"}, "144": {"144", "144/A"}}


def _settings(ctx: ToolContext) -> runtime.Settings:
    s = runtime.settings_of(ctx.settings)
    common.check_sec_contact(s, os.environ)
    return s


def _today(ctx: ToolContext):
    return ctx.now().date()


def _prov(ctx, tool, *, datasets, request, as_of, rows, session, provider="sec"):
    return common.provenance(
        ctx,
        provider=provider,
        route=ROUTES[tool],
        datasets=datasets,
        request=request,
        as_of=as_of,
        rows=rows,
        session=session,
        authority=["official"],
    )


async def sec_filings(ctx: ToolContext, args: FilingsInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    today = _today(ctx)
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 2), default_end=today
    )
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.submissions", identity)
        return F.records(session.read("sec.submissions", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    forms = set(args.forms or [])
    rows = []
    for r in records:
        filed = F.day(r["filing_date"])
        if not F.within(filed, start, end) or (forms and F.text(r["form"]) not in forms):
            continue
        rows.append(
            common.row(
                Filing,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "cik": F.text(r["cik"]),
                    "company_name": F.text(r["company_name"]),
                    "form": F.text(r["form"]),
                    "filing_date": filed,
                    "accepted_at": F.instant(r["event_time"]),
                    "accession_number": F.text(r["accession_number"]),
                    "report_date": F.day(r["report_date"]),
                    "items": F.split(r["items"]),
                    "primary_doc_url": F.text(r["primary_doc_url"]),
                    "sic": F.text(r["sic"]),
                    "sic_description": F.text(r["sic_description"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"company_name": NPS, "sic": NPS, "sic_description": NPS},
            )
        )
    rows.sort(key=lambda r: (r.accepted_at, r.accession_number), reverse=True)
    prov = _prov(
        ctx,
        "sec_filings",
        datasets=("sec.submissions",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "forms": args.forms, "start": start, "end": end, "as_of": args.as_of},
    )
    return common.output(ctx, s, Filing, rows, prov)


async def sec_xbrl_facts(ctx: ToolContext, args: FactsInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    if args.start and args.end:
        common.window(ctx.tool, args.start, args.end, default_start=args.start, default_end=args.end)
    identity = {"ticker": args.ticker}
    filters = {**identity, **({"taxonomy": args.taxonomy} if args.taxonomy else {})}

    def work(session):
        session.refresh("sec.company_facts", identity)
        return F.records(
            session.read("sec.company_facts", as_of=as_of, vintages=args.include_vintages, **filters)
        )

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    metrics, forms = set(args.metrics or []), set(args.forms or [])
    rows = []
    for r in records:
        end = F.day(r["period_end"])
        if metrics and F.text(r["metric"]) not in metrics:
            continue
        if forms and F.text(r["form"]) not in forms:
            continue
        if not F.within(end, args.start, args.end):
            continue
        rows.append(
            common.row(
                XbrlFact,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "cik": f"{int(F.text(r['cik']) or 0):010d}",
                    "taxonomy": F.text(r["taxonomy"]),
                    "metric": F.text(r["metric"]),
                    "unit": F.text(r["unit"]),
                    "period_start": F.day(r["period_start"]),
                    "period_end": end,
                    "value": F.number(r["value"]),
                    "accession_number": F.text(r["accn"]) or "",
                    "form": F.text(r["form"]),
                    "fiscal_year": F.integer(r["fiscal_year"]),
                    "fiscal_period": F.text(r["fiscal_period"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"period_start": NA, "form": NPS, "fiscal_year": NPS, "fiscal_period": NPS},
            )
        )
    rows.sort(
        key=lambda r: (
            r.taxonomy,
            r.metric,
            r.unit,
            r.period_end,
            r.period_start or r.period_end,
            r.knowledge_time,
        )
    )
    prov = _prov(
        ctx,
        "sec_xbrl_facts",
        datasets=("sec.company_facts",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "ticker": args.ticker,
            "metrics": args.metrics,
            "taxonomy": args.taxonomy,
            "forms": args.forms,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
            "include_vintages": args.include_vintages,
        },
    )
    return common.output(ctx, s, XbrlFact, rows, prov)


async def sec_fundamentals(ctx: ToolContext, args: FundamentalsInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    if args.start and args.end:
        common.window(ctx.tool, args.start, args.end, default_start=args.start, default_end=args.end)
    identity = {"ticker": args.ticker}

    def work(session):
        facts_fresh = session.refresh("sec.company_facts", identity)
        # built locally from the stored facts: rebuilt whenever they were just fetched
        session.refresh("sec.fundamentals", identity, force=not facts_fresh)
        return F.records(session.read("sec.fundamentals", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    wanted = set(args.metrics or FUNDAMENTAL_METRICS)
    order = {m: i for i, m in enumerate(FUNDAMENTAL_METRICS)}
    rows = []
    for r in records:
        metric, end = F.text(r["metric"]), F.day(r["period_end"])
        if metric not in wanted or not F.within(end, args.start, args.end):
            continue
        unit, value = F.text(r["unit"]), F.number(r["value"])
        if unit == "pct":
            unit, value = "fraction", F.fraction(value)
        reason = F.text(r["reason"])
        rows.append(
            common.row(
                FundamentalValue,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "cik": F.text(r["cik"]),
                    "metric": metric,
                    "unit": unit,
                    "basis": F.text(r["basis"]),
                    "period_start": F.day(r["period_start"]),
                    "period_end": end,
                    "fiscal_label": F.text(r["fiscal_label"]),
                    "value": value,
                    "derived": bool(F.flag(r["derived"])),
                    "reason": reason,
                    "note": F.text(r["note"]),
                    "concepts": F.split(r["concepts"]),
                    "accession_numbers": F.split(r["accns"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "value": REASON_CODES.get(reason or "", AbsenceCode.NO_DATA),
                    "period_start": NA,
                    "fiscal_label": NA,
                    "reason": NA,
                    "note": NPS,
                },
            )
        )
    rows.sort(key=lambda r: (order.get(r.metric, 99), r.period_end, r.knowledge_time))
    prov = _prov(
        ctx,
        "sec_fundamentals",
        datasets=("sec.company_facts", "sec.fundamentals"),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "ticker": args.ticker,
            "metrics": args.metrics,
            "start": args.start,
            "end": args.end,
            "as_of": args.as_of,
        },
    )
    return common.output(ctx, s, FundamentalValue, rows, prov)


async def sec_earnings_releases(ctx: ToolContext, args: ReleasesInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    today = _today(ctx)
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 3), default_end=today
    )
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.earnings_releases", identity)
        return F.records(session.read("sec.earnings_releases", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    rows = []
    for r in records:
        filed = F.day(r["filing_date"])
        if not F.within(filed, start, end):
            continue
        rows.append(
            common.row(
                EarningsRelease,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "cik": F.text(r["cik"]),
                    "company_name": F.text(r["company_name"]),
                    "form": F.text(r["form"]),
                    "accession_number": F.text(r["accession_number"]),
                    "filing_date": filed,
                    "report_date": F.day(r["report_date"]),
                    "accepted_at": F.instant(r["acceptance_datetime"]),
                    "session": F.text(r["session"]),
                    "items": F.split(r["items"]),
                    "primary_doc_url": F.text(r["primary_doc_url"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"company_name": NPS},
            )
        )
    rows.sort(key=lambda r: (r.accepted_at, r.accession_number), reverse=True)
    prov = _prov(
        ctx,
        "sec_earnings_releases",
        datasets=("sec.earnings_releases",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "start": start, "end": end, "as_of": args.as_of},
    )
    return common.output(ctx, s, EarningsRelease, rows, prov)


async def sec_earnings_figures(ctx: ToolContext, args: FiguresInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    today = _today(ctx)
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 3), default_end=today
    )
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.earnings_press_release_figures", identity)
        return F.records(session.read("sec.earnings_press_release_figures", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    metrics = set(args.metrics)
    rows = []
    for r in records:
        accepted = F.instant(r["event_time"])
        if F.text(r["metric"]) not in metrics or not F.within(accepted.date(), start, end):
            continue
        rows.append(
            common.row(
                EarningsFigure,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "cik": F.text(r["cik"]),
                    "accession_number": F.text(r["accession_number"]),
                    "accepted_at": accepted,
                    "exhibit": F.text(r["exhibit"]),
                    "exhibit_url": F.text(r["exhibit_url"]),
                    "metric": F.text(r["metric"]),
                    "fiscal_period_label": F.text(r["fiscal_period_label"]),
                    "value": F.number(r["value"]),
                    "unit": F.text(r["unit"]),
                    "basis": F.text(r["basis"]),
                    "source_text": F.text(r["source_text"]) or "",
                    "parse_rule": F.text(r["parse_rule"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"fiscal_period_label": NPS},
            )
        )
    rows.sort(key=lambda r: (-r.accepted_at.timestamp(), r.accession_number, r.metric))
    prov = _prov(
        ctx,
        "sec_earnings_figures",
        datasets=("sec.earnings_press_release_figures",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "ticker": args.ticker,
            "metrics": args.metrics,
            "start": start,
            "end": end,
            "as_of": args.as_of,
        },
    )
    return common.output(ctx, s, EarningsFigure, rows, prov)


async def sec_insider_trades(ctx: ToolContext, args: InsiderInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    today = _today(ctx)
    start, end = common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, 1), default_end=today
    )
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec_insider.transactions", identity)
        return F.records(session.read("sec_insider.transactions", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="sec_insider", subject=("filer", args.ticker))
    forms = set().union(*(FORM_GROUPS[f] for f in args.forms))
    rows = []
    for r in records:
        form, when = F.text(r["form"]), F.day(r["transaction_date"])
        if form not in forms or not F.within(when, start, end):
            continue
        notice = form.startswith("144")
        rows.append(
            common.row(
                InsiderTransaction,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "cik": F.text(r["cik"]),
                    "accession_number": F.text(r["accession_number"]),
                    "line": (F.integer(r["line"]) or 0) + 1,
                    "form": form,
                    "filing_date": F.day(r["filing_date"]),
                    "insider_name": F.text(r["insider_name"]) or "(unnamed)",
                    "insider_cik": F.text(r["insider_cik"]),
                    "officer_title": F.text(r["officer_title"]),
                    "is_director": bool(F.flag(r["is_director"])),
                    "is_officer": bool(F.flag(r["is_officer"])),
                    "is_ten_percent_owner": bool(F.flag(r["is_ten_percent_owner"])),
                    "has_10b5_1": bool(F.flag(r["has_10b5_1"])),
                    "is_derivative": bool(F.flag(r["is_derivative"])),
                    "security_title": F.text(r["security_title"]),
                    "transaction_date": when,
                    "transaction_code": F.text(r["transaction_code"]) or "",
                    "acquired_disposed": F.text(r["acquired_disposed"]),
                    "shares": F.number(r["shares"]),
                    "price_per_share": F.number(r["price_per_share"]),
                    "value_usd": F.number(r["value_usd"]),
                    "shares_owned_after": F.number(r["shares_owned_after"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "insider_cik": NPS,
                    "officer_title": NA,
                    "acquired_disposed": NA if notice else AbsenceCode.NO_DATA,
                    "shares_owned_after": NA if notice else AbsenceCode.NO_DATA,
                },
            )
        )
    rows.sort(key=lambda r: (r.transaction_date, r.accession_number, -r.line), reverse=True)
    prov = _prov(
        ctx,
        "sec_insider_trades",
        datasets=("sec_insider.transactions",),
        as_of=as_of,
        rows=rows,
        session=session,
        provider="sec_insider",
        request={"ticker": args.ticker, "forms": args.forms, "start": start, "end": end, "as_of": args.as_of},
    )
    return common.output(ctx, s, InsiderTransaction, rows, prov)


def _restated(records: list[dict], period) -> tuple[list[dict], list[str]]:
    """One portfolio per period: a restatement replaces everything filed before it
    for the period (the latest restatement wins); new-holdings amendments add."""
    rows = [r for r in records if F.day(r["period_end"]) == period]
    restatements = sorted(
        {
            (F.day(r["filing_date"]), F.instant(r["knowledge_time"]), F.text(r["accession_number"]))
            for r in rows
            if (F.text(r["amendment_type"]) or "").upper() == "RESTATEMENT"
        }
    )
    if restatements:
        winner = restatements[-1][2]
        note = f"A restatement ({winner}) replaces the earlier 13F-HR filings for {period.isoformat()}."
        return [r for r in rows if F.text(r["accession_number"]) == winner], [note]
    added = sorted(
        {
            F.text(r["accession_number"])
            for r in rows
            if (F.text(r["amendment_type"]) or "").upper() == "NEW HOLDINGS"
        }
    )
    notes = [f"New-holdings amendments ({', '.join(added)}) add to the original filing."] if added else []
    return rows, notes


async def sec_13f_holdings(ctx: ToolContext, args: ThirteenFInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    identity = {"filer_cik": args.filer_cik}

    def work(session):
        session.refresh("sec13f.holdings", identity)
        return F.records(session.read("sec13f.holdings", as_of=as_of, **identity))

    records, session = await common.run(ctx, s, work, source="sec13f", subject=("13F filer", args.filer_cik))
    periods = sorted({F.day(r["period_end"]) for r in records})
    period = args.period_end or (periods[-1] if periods else None)
    chosen, notes = _restated(records, period) if period else ([], [])
    cusips = set(args.cusips or [])
    rows = []
    for r in chosen:
        cusip = F.text(r["cusip"]) or ""
        if cusips and cusip not in cusips:
            continue
        rows.append(
            common.row(
                InstitutionalHolding,
                {
                    "filer_cik": F.text(r["filer_cik"]),
                    "filer_name": F.text(r["filer_name"]),
                    "accession_number": F.text(r["accession_number"]),
                    "line": (F.integer(r["line"]) or 0) + 1,
                    "form": F.text(r["form"]),
                    "period_end": F.day(r["period_end"]),
                    "filing_date": F.day(r["filing_date"]),
                    "is_amendment": bool(F.flag(r["is_amendment"])),
                    "amendment_type": F.text(r["amendment_type"]),
                    "issuer_name": F.text(r["issuer_name"]) or "",
                    "cusip": cusip,
                    "title_of_class": F.text(r["title_of_class"]),
                    "value_usd": F.number(r["value_usd"]),
                    "shares": F.number(r["shares"]),
                    "share_type": F.text(r["share_type"]) or "SH",
                    "put_call": F.text(r["put_call"]),
                    "investment_discretion": F.text(r["investment_discretion"]),
                    "sole_voting": F.number(r["sole_voting"]),
                    "shared_voting": F.number(r["shared_voting"]),
                    "no_voting": F.number(r["no_voting"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"filer_name": NPS, "amendment_type": NA, "put_call": NA},
            )
        )
    rows.sort(key=lambda r: (r.accession_number, r.line))
    prov = _prov(
        ctx,
        "sec_13f_holdings",
        datasets=("sec13f.holdings",),
        as_of=as_of,
        rows=rows,
        session=session,
        provider="sec13f",
        request={
            "filer_cik": args.filer_cik,
            "period_end": period,
            "cusips": args.cusips,
            "as_of": args.as_of,
        },
    )
    return common.output(ctx, s, InstitutionalHolding, rows, prov, notes=notes)


def _series_of(session, ticker: str) -> str | None:
    """The fund series of a symbol, from SEC's stored fund ticker list."""
    df = session.read("sec.fund_tickers", as_of=None, symbol=ticker)
    return F.first(x for x in (F.text(r["series_id"]) for r in F.records(df)) if x)


def _fund_refresh(session, dataset_id: str, ticker: str) -> str | None:
    session.refresh("sec.fund_tickers", {})
    session.refresh(dataset_id, {"ticker": ticker})
    return _series_of(session, ticker)


async def sec_fund_nport(ctx: ToolContext, args: NportInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)

    def work(session):
        series = _fund_refresh(session, "sec.fund_nport", args.ticker)
        if series is None:
            return []
        return F.records(session.read("sec.fund_nport", as_of=as_of, series_id=series))

    records, session = await common.run(ctx, s, work, source="sec", subject=("fund", args.ticker))
    rows = [
        common.row(
            FundReport,
            {
                "ticker": args.ticker,
                "cik": F.text(r["cik"]),
                "series_id": F.text(r["series_id"]),
                "series_name": F.text(r["series_name"]),
                "class_ids": F.split(r["class_ids"]),
                "accession_number": F.text(r["accession_number"]),
                "form": F.text(r["form"]),
                "report_date": F.day(r["report_date"]),
                "report_period_end": F.day(r["report_period_end"]),
                "filing_date": F.day(r["filing_date"]),
                "total_assets": F.number(r["total_assets"]),
                "total_liabilities": F.number(r["total_liabilities"]),
                "net_assets": F.number(r["net_assets"]),
                "holdings_count": F.integer(r["holdings_count"]) or 0,
                "is_final": F.flag(r["is_final"]),
                "primary_doc_url": F.text(r["primary_doc_url"]),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
            codes={"series_name": NPS},
        )
        for r in records
    ]
    rows.sort(key=lambda r: (r.report_date, r.knowledge_time), reverse=True)
    prov = _prov(
        ctx,
        "sec_fund_nport",
        datasets=("sec.fund_nport",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "as_of": args.as_of},
    )
    return common.output(ctx, s, FundReport, rows, prov)


async def sec_fund_holdings(ctx: ToolContext, args: FundHoldingsInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)

    def work(session):
        series = _fund_refresh(session, "sec.fund_nport_holdings", args.ticker)
        if series is None:
            return []
        return F.records(session.read("sec.fund_nport_holdings", as_of=as_of, series_id=series))

    records, session = await common.run(ctx, s, work, source="sec", subject=("fund", args.ticker))
    accession = args.accession_number
    if accession is None and records:
        latest = max(records, key=lambda r: (F.day(r["report_date"]), F.instant(r["knowledge_time"])))
        accession = F.text(latest["accession_number"])
    rows = [
        common.row(
            FundHolding,
            {
                "cik": F.text(r["cik"]),
                "series_id": F.text(r["series_id"]),
                "accession_number": F.text(r["accession_number"]),
                "report_date": F.day(r["report_date"]),
                "line": F.integer(r["line"]),
                "name": F.text(r["name"]),
                "lei": F.text(r["lei"]),
                "title": F.text(r["title"]),
                "cusip": F.text(r["cusip"]),
                "isin": F.text(r["isin"]),
                "ticker": F.text(r["ticker"]),
                "balance": F.number(r["balance"]),
                "units": F.text(r["units"]),
                "currency": F.text(r["currency"]),
                "value_usd": F.number(r["value_usd"]),
                "pct_value": F.fraction(r["pct_value"]),
                "payoff_profile": F.text(r["payoff_profile"]),
                "asset_category": F.text(r["asset_cat"]),
                "issuer_category": F.text(r["issuer_cat"]),
                "country": F.text(r["country"]),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
        )
        for r in records
        if F.text(r["accession_number"]) == accession
    ]
    rows.sort(key=lambda r: r.line)
    prov = _prov(
        ctx,
        "sec_fund_holdings",
        datasets=("sec.fund_nport_holdings",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "accession_number": accession, "as_of": args.as_of},
    )
    return common.output(ctx, s, FundHolding, rows, prov)


def _spec(
    name: str,
    title: str,
    description: str,
    readme: str,
    inputs,
    model,
    provider: str,
    route: str,
    *,
    risk: str = "api_structured",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        capability="filings",
        title=title,
        description=description,
        readme=readme,
        input_model=inputs,
        output_model=model,
        provider=provider,
        route=route,
        handler=HANDLERS[name],
        golden_test=GOLDEN,
        env=ENV,
        output_risk=risk,  # type: ignore[arg-type]
    )


HANDLERS = {
    "sec_filings": sec_filings,
    "sec_xbrl_facts": sec_xbrl_facts,
    "sec_fundamentals": sec_fundamentals,
    "sec_earnings_releases": sec_earnings_releases,
    "sec_earnings_figures": sec_earnings_figures,
    "sec_insider_trades": sec_insider_trades,
    "sec_13f_holdings": sec_13f_holdings,
    "sec_fund_nport": sec_fund_nport,
    "sec_fund_holdings": sec_fund_holdings,
}

SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "sec_filings",
        "SEC filings",
        common.describe(
            "A company's EDGAR filings index: form, filing date, EDGAR acceptance instant (UTC), accession "
            f"number, report date, 8-K items, primary document URL. Filter by forms. {CONTACT}",
            "at_event",
        ),
        "EDGAR filings index of a company",
        FilingsInputs,
        Filing,
        "sec",
        "omni sec.submissions <- data.sec.gov/submissions",
    ),
    _spec(
        "sec_xbrl_facts",
        "SEC XBRL facts",
        common.describe(
            "XBRL facts a company filed (us-gaap, dei, ...): concept, unit, period, value as filed, "
            "accession, form and fiscal period, stamped at the filing's EDGAR acceptance. Restatements are "
            f"kept: as_of shows what was filed by then; include_vintages=true returns every version. {CONTACT}",
            "vintage",
        ),
        "XBRL facts as filed, with restatement vintages",
        FactsInputs,
        XbrlFact,
        "sec",
        "omni sec.company_facts <- data.sec.gov/api/xbrl/companyfacts",
    ),
    _spec(
        "sec_fundamentals",
        "SEC fundamentals",
        common.describe(
            "23 point-in-time fundamentals computed from the filed XBRL facts: TTM revenue, gross profit, "
            "operating income, EBIT, EBITDA, net income, diluted EPS, cash flow, capex, free cash flow, "
            "dividends per share, balance-sheet items and margins (fractions: 0.25 is 25 %). A missing value "
            f"is null with its reason and note, never 0. {CONTACT}",
            "vintage",
        ),
        "TTM and balance-sheet fundamentals from XBRL, point in time",
        FundamentalsInputs,
        FundamentalValue,
        "sec",
        "omni sec.fundamentals <- sec.company_facts (computed locally)",
    ),
    _spec(
        "sec_earnings_releases",
        "SEC earnings releases",
        common.describe(
            "A company's 8-K and 8-K/A filings with Item 2.02 (results of operations): acceptance instant, "
            f"session (pre_open, intraday, after_close in New York), period of report, document URL. {CONTACT}",
            "at_event",
        ),
        "8-K Item 2.02 earnings releases, stamped at acceptance",
        ReleasesInputs,
        EarningsRelease,
        "sec",
        "omni sec.earnings_releases <- data.sec.gov/submissions (8-K Item 2.02)",
    ),
    _spec(
        "sec_earnings_figures",
        "SEC press-release EPS",
        common.describe(
            "Diluted GAAP EPS and adjusted EPS read from the tables of each earnings press release (EX-99.1), "
            "in USD per share, with the table row they came from (source_text, company text: untrusted). "
            f"A figure that did not parse has no row; revenue is never read from a release. {CONTACT}",
            "at_event",
        ),
        "EPS from earnings press releases (provisional)",
        FiguresInputs,
        EarningsFigure,
        "sec",
        "omni sec.earnings_press_release_figures <- www.sec.gov/Archives (EX-99.1)",
        risk="external_text",
    ),
    _spec(
        "sec_insider_trades",
        "SEC insider trades",
        common.describe(
            "Insider transactions from Forms 4 (executed: code P buy, S sell, A grant, M exercise, F tax) and "
            "144 (announced sales): insider, role, 10b5-1 flag, shares, price, value in USD, holdings after. "
            "knowledge_time is EDGAR acceptance, after the transaction date. marketlens-data's insider history "
            f"starts at filings of 2026-01-01 on a first fetch. {CONTACT}",
            "lagged",
        ),
        "Form 4 and Form 144 insider transactions",
        InsiderInputs,
        InsiderTransaction,
        "sec_insider",
        "omni sec_insider.transactions <- www.sec.gov/Archives (Forms 4 and 144)",
    ),
    _spec(
        "sec_13f_holdings",
        "SEC 13F holdings",
        common.describe(
            "A manager's 13F-HR holdings for one quarter (default: the latest stored): issuer, CUSIP, value in "
            "USD, shares or principal, put/call, voting authority. A restatement replaces the original; "
            "new-holdings amendments are added. Knowable at EDGAR acceptance, up to 45 days after the quarter. "
            f"{CONTACT}",
            "lagged",
        ),
        "13F-HR institutional holdings of a manager",
        ThirteenFInputs,
        InstitutionalHolding,
        "sec13f",
        "omni sec13f.holdings <- www.sec.gov/Archives (13F-HR)",
    ),
    _spec(
        "sec_fund_nport",
        "SEC fund N-PORT reports",
        common.describe(
            "A fund series' latest Form N-PORT headers (e.g. VOO): report date, net assets, total assets and "
            "liabilities in USD, share classes, holdings count. Series level: one report covers every share "
            f"class. {CONTACT}",
            "vintage",
            stored=False,
        ),
        "Fund N-PORT headers: net assets per report date",
        NportInputs,
        FundReport,
        "sec",
        "omni sec.fund_nport <- www.sec.gov/Archives (N-PORT)",
    ),
    _spec(
        "sec_fund_holdings",
        "SEC fund holdings",
        common.describe(
            "Every holding line of one N-PORT filing of a fund series (default: the latest report at as_of): "
            "name, CUSIP, ISIN, balance, value in USD, share of net assets (fraction), asset and issuer "
            f"category. {CONTACT}",
            "vintage",
        ),
        "Fund N-PORT holdings of one report",
        FundHoldingsInputs,
        FundHolding,
        "sec",
        "omni sec.fund_nport_holdings <- www.sec.gov/Archives (N-PORT)",
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "sec_filings": ("sec.submissions",),
    "sec_xbrl_facts": ("sec.company_facts",),
    "sec_fundamentals": ("sec.company_facts", "sec.fundamentals"),
    "sec_earnings_releases": ("sec.earnings_releases",),
    "sec_earnings_figures": ("sec.earnings_press_release_figures",),
    "sec_insider_trades": ("sec_insider.transactions",),
    "sec_13f_holdings": ("sec13f.holdings",),
    "sec_fund_nport": ("sec.fund_nport",),
    "sec_fund_holdings": ("sec.fund_nport_holdings",),
}
