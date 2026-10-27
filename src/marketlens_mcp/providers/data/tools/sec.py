"""SEC EDGAR: filings, XBRL facts, XBRL frames (one concept across filers),
point-in-time fundamentals, earnings releases and press-release EPS, material
filing events, pay versus performance, insider trades, Form 144 notices, the
IPO pipeline, EDGAR's daily form index, 13F holdings and fund
N-PORT reports (capability ``filings``). Every fetch names the operator's
contact in its User-Agent (SEC_CONTACT_EMAIL), as EDGAR's fair-access rule
asks; unset, marketlens-data sends its placeholder contact and every answer
says so."""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import (
    DailyIndexEntry,
    EarningsFigure,
    EarningsRelease,
    Filing,
    FilingEvent,
    Form144Notice,
    FundamentalValue,
    FundHolding,
    FundReport,
    InsiderTransaction,
    InstitutionalHolding,
    IpoFiling,
    PayVsPerformance,
    XbrlFact,
    XbrlFrameValue,
)

from .. import frames as F
from .. import runtime
from . import common
from .common import AsOf, Inputs, Ticker

GOLDEN = "tests/data/test_sec.py"
ENV = ("SEC_CONTACT_EMAIL",)
CONTACT = "Without SEC_CONTACT_EMAIL a placeholder contact is sent (SEC fair access) and the answer says so."

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
_ITEM_CODE = re.compile(r"^(?:\d{1,2}\.\d{2}|6-K)$")
#: The 8-K items marketlens-data keeps as events (sec.filing_events' allowlist), and 6-K.
EVENT_ITEMS = ("1.01", "1.02", "2.01", "2.03", "2.05", "2.06", "3.02", "5.02", "7.01", "8.01", "6-K")


def _item_codes(values: list[str]) -> list[str]:
    out = []
    for v in values:
        code = v.strip().upper()
        if not _ITEM_CODE.match(code):
            raise ValueError(f"{v!r} is not an 8-K item code (1.01, 3.02, ...) or 6-K")
        out.append(code)
    return list(dict.fromkeys(out))


ItemCodes = Annotated[list[str], Field(min_length=1, max_length=20), AfterValidator(_item_codes)]
TICKER_DOC = "Company ticker, SEC style (brk.b becomes BRK-B)."


class FilingsInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    forms: Forms | None = Field(default=None, description="Only these forms, e.g. [10-K, 10-Q, 8-K, 4].")
    start: date | None = Field(default=None, description="First filing date. Default: 2 years ago.")
    end: date | None = Field(default=None, description="Last filing date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class EventsInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    items: ItemCodes | None = Field(
        default=None,
        description="Only these item codes: 1.01, 1.02, 2.01, 2.03, 2.05, 2.06, 3.02, 5.02, 7.01, 8.01 or 6-K. "
        "Default: every item on the allowlist.",
    )
    since: date | None = Field(
        default=None, description="First filing date (SEC's filing date, inclusive). Default: 2 years ago."
    )
    limit: int = Field(default=100, ge=1, le=500, description="At most this many events, newest first.")
    as_of: AsOf | None = common.as_of_field()


class DailyIndexInputs(Inputs):
    day: date | None = Field(
        default=None,
        description="The index day (EDGAR posts a business day's index at about 22:01 ET). "
        "Default: the last weekday before today.",
    )
    forms: Forms | None = Field(default=None, description="Only these forms, e.g. [144, S-1, 424B4].")
    cik: Annotated[str, AfterValidator(_cik)] | None = Field(
        default=None, description="Only the lines of this company (CIK, 1-10 digits)."
    )
    as_of: AsOf | None = common.as_of_field()


class Form144Inputs(Inputs):
    ticker: Ticker = Field(description="The issuer's ticker, SEC style (brk.b becomes BRK-B).")
    since: date | None = Field(
        default=None, description="First filing date (SEC's filing date, inclusive). Default: a year ago."
    )
    limit: int = Field(default=100, ge=1, le=500, description="At most this many notices, newest first.")
    as_of: AsOf | None = common.as_of_field()


IPO_STAGES = ("filed", "amended", "effective", "priced", "withdrawn")
IPO_MAX_DAYS = 31


class IpoInputs(Inputs):
    start: date | None = Field(default=None, description="First filing date. Default: 14 days ago.")
    end: date | None = Field(
        default=None,
        description=f"Last filing date. Default: today. At most {IPO_MAX_DAYS} days after start.",
    )
    stages: list[Literal["filed", "amended", "effective", "priced", "withdrawn"]] | None = Field(
        default=None, min_length=1, description="Only these stages. Default: all."
    )
    categories: list[Literal["operating", "spac", "trust"]] | None = Field(
        default=None,
        min_length=1,
        description="operating companies, SPACs (SIC 6770), trusts (SIC 6221 ETPs).",
    )
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


_FRAME_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,149}$")
_FRAME_PERIOD = re.compile(r"^CY\d{4}(?:Q[1-4]I?)?$")
_FRAME_UNIT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")


def _frame_tag(value: str) -> str:
    if not _FRAME_TAG.match(value):
        raise ValueError(f"{value!r} is not an XBRL concept name (e.g. Revenues, Assets)")
    return value


def _frame_period(value: str) -> str:
    s = value.strip().upper()
    if not _FRAME_PERIOD.match(s):
        raise ValueError(
            f"{value!r} is not a frames period: CY2025 (a year), CY2025Q2 (a quarter), CY2025Q2I (an instant)"
        )
    return s


def _frame_unit(value: str) -> str:
    if not _FRAME_UNIT.match(value):
        raise ValueError(f"{value!r} is not a frames unit (USD, USD-per-shares, shares, pure)")
    return value


class FramesInputs(Inputs):
    tag: Annotated[str, AfterValidator(_frame_tag)] = Field(
        description="XBRL concept, e.g. Revenues, NetIncomeLoss, Assets, EarningsPerShareDiluted; or the alias "
        "revenue: per filer revenue from contracts, else Revenues (a larger Revenues leads)."
    )
    period: Annotated[str, AfterValidator(_frame_period)] = Field(
        description="Calendar period: CY2025 (a year), CY2025Q2 (a quarter) or CY2025Q2I (an instant, for a "
        "balance such as Assets). A fiscal period is placed in the calendar period it most nearly fits."
    )
    taxonomy: Literal["us-gaap", "dei", "ifrs-full", "srt"] = Field(
        default="us-gaap", description="The concept's taxonomy (dei for EntityCommonStockSharesOutstanding)."
    )
    unit: Annotated[str, AfterValidator(_frame_unit)] | None = Field(
        default=None,
        description="As the frames API spells it: USD, USD-per-shares, shares, pure. Default: the unit "
        "marketlens-data configures for the concept, else USD.",
    )
    tickers: Annotated[list[Ticker], Field(min_length=1, max_length=200)] | None = Field(
        default=None, description="Only these companies (matched by the SEC's CIK). Default: every filer."
    )
    sort: Literal["desc", "asc"] = Field(default="desc", description="By value.")
    limit: int = Field(default=100, ge=1, le=10000, description="At most this many rows, after sorting.")
    as_of: AsOf | None = common.as_of_field()


PVP_MEASURES = (
    "peo_total_comp",
    "peo_comp_actually_paid",
    "non_peo_neo_avg_total_comp",
    "non_peo_neo_avg_comp_actually_paid",
    "company_tsr",
    "peer_group_tsr",
    "net_income",
    "company_selected_measure",
)


class PayVsPerformanceInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    measures: (
        Annotated[
            list[
                Literal[
                    "peo_total_comp",
                    "peo_comp_actually_paid",
                    "non_peo_neo_avg_total_comp",
                    "non_peo_neo_avg_comp_actually_paid",
                    "company_tsr",
                    "peer_group_tsr",
                    "net_income",
                    "company_selected_measure",
                ]
            ],
            Field(min_length=1, max_length=8),
        ]
        | None
    ) = Field(default=None, description="Only these measures. Default: all eight.")
    as_of: AsOf | None = common.as_of_field()


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
    "sec_xbrl_frames": "omni sec.xbrl_frames <- data.sec.gov/api/xbrl/frames + www.sec.gov/Archives/edgar/full-index (XBRL index)",
    "sec_pay_vs_performance": "omni sec.pay_vs_performance <- sec.submissions + www.sec.gov/Archives (proxy XBRL)",
    "filings_events": "omni sec.filing_events <- sec.submissions + www.sec.gov/Archives (filing index, primary document)",
    "sec_fundamentals": "omni sec.fundamentals <- sec.company_facts (computed locally)",
    "sec_earnings_releases": "omni sec.earnings_releases <- data.sec.gov/submissions (8-K Item 2.02)",
    "sec_earnings_figures": "omni sec.earnings_press_release_figures <- www.sec.gov/Archives (EX-99.1)",
    "sec_insider_trades": "omni sec_insider.transactions <- www.sec.gov/Archives (Forms 4 and 144)",
    "sec_daily_index": "omni sec.daily_index <- www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{q}/form.{YYYYMMDD}.idx",
    "sec_form144": "omni sec.form144 <- data.sec.gov/submissions + www.sec.gov/Archives (Form 144 XML)",
    "sec_ipo_pipeline": "omni sec.ipo_filings <- sec.daily_index + data.sec.gov/submissions + www.sec.gov/Archives "
    "(S-1, F-1, EFFECT, 424B4, RW)",
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


def _output(ctx: ToolContext, s: runtime.Settings, model, rows, prov, *, notes=()) -> ToolOutput:
    """common.output, with the note that SEC_CONTACT_EMAIL is not set when it is not."""
    return common.output(ctx, s, model, rows, prov, notes=[*notes, *common.sec_contact_notes(s, os.environ)])


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
        return F.records(session.read("sec.submissions", as_of=as_of, **session.company(args.ticker)))

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
                    "ticker": args.ticker,
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
    return _output(ctx, s, Filing, rows, prov)


def _exhibits(raw: str | None) -> dict[str, str]:
    """The stored JSON map of exhibit type to URL, decoded; {} for anything else."""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in decoded.items()} if isinstance(decoded, dict) else {}


def _index_url(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


async def filings_events(ctx: ToolContext, args: EventsInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    today = _today(ctx)
    since = args.since or common.years_ago(today, 2)
    identity = {"ticker": args.ticker}

    def work(session):
        index_fresh = session.refresh("sec.submissions", identity)
        # derived from the stored filings index: read again whenever the index was just fetched
        session.refresh("sec.filing_events", identity, force=not index_fresh)
        return F.records(session.read("sec.filing_events", as_of=as_of, **session.company(args.ticker)))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    wanted = set(args.items or [])
    rows = []
    for r in records:
        filed, item = F.day(r["filing_date"]), F.text(r["item"])
        if filed is None or filed < since or (wanted and item not in wanted):
            continue
        cik, accession = F.text(r["cik"]), F.text(r["accession_number"])
        rows.append(
            common.row(
                FilingEvent,
                {
                    "ticker": args.ticker,
                    "cik": cik,
                    "form": F.text(r["form"]),
                    "filing_date": filed,
                    "accepted_at": F.instant(r["event_time"]),
                    "accession_number": accession,
                    "item": item,
                    "item_title": F.text(r["item_title"]),
                    "headline": F.text(r["headline"]),
                    "primary_doc_url": F.text(r["primary_doc_url"]),
                    "exhibits": _exhibits(F.text(r["exhibits"])),
                    "index_url": _index_url(cik, accession),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"primary_doc_url": NPS},
            )
        )
    rows.sort(key=lambda r: r.item)  # within a filing, its items in code order
    rows.sort(key=lambda r: (r.accepted_at, r.accession_number), reverse=True)
    rows = rows[: args.limit]
    prov = _prov(
        ctx,
        "filings_events",
        datasets=("sec.submissions", "sec.filing_events"),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "ticker": args.ticker,
            "items": args.items,
            "since": since,
            "limit": args.limit,
            "as_of": args.as_of,
        },
    )
    return _output(
        ctx, s, FilingEvent, rows, prov
    )  # the spec's output risk: external_text (the headline is the filer's prose)


def _frame_unit_default(tag: str, taxonomy: str) -> str:
    """The unit marketlens-data configures for a concept it collects, else USD."""
    try:
        from omni.sources import sec_xbrl_frames

        spec = sec_xbrl_frames.concepts().get(tag)
    except Exception:  # no configuration readable: the caller's unit or USD
        spec = None
    return spec["unit"] if spec and spec["taxonomy"] == taxonomy else "USD"


#: The concept alias `revenue`: a revenue fallback chain in its order (revenue
#: from contracts, then Revenues), the two concepts marketlens-data collects
#: frames of, with a lead rule on one period: per filer the first concept it
#: states; revenue from contracts, a part of the revenue where a filer also
#: states a total, yields to Revenues when that is larger by more than 0.5%.
REVENUE_ALIAS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues")
_LARGER = 0.005


def _revenue_lead(records: list[dict]) -> list[dict]:
    """One record per filer: the alias's concept for it (REVENUE_ALIAS)."""
    by_cik: dict[str, dict[str, dict]] = {}
    for r in records:
        by_cik.setdefault(F.text(r["cik"]) or "", {})[F.text(r["tag"]) or ""] = r
    out = []
    for stated in by_cik.values():
        part, total = (stated.get(t) for t in REVENUE_ALIAS)
        lead = part or total
        larger = (
            part is not None
            and total is not None
            and ((F.number(total["value"]) or 0) > (F.number(part["value"]) or 0) * (1 + _LARGER))
        )
        if larger:
            lead = total
        out.append(lead)
    return out


async def sec_xbrl_frames(ctx: ToolContext, args: FramesInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    alias = args.tag == "revenue"
    unit = args.unit or ("USD" if alias else _frame_unit_default(args.tag, args.taxonomy))
    tags = REVENUE_ALIAS if alias else (args.tag,)
    identities = [{"taxonomy": args.taxonomy, "tag": t, "unit": unit, "period": args.period} for t in tags]
    asked = list(dict.fromkeys(args.tickers or []))

    def work(session):
        records = []
        for identity in identities:
            session.refresh("sec.xbrl_frames", identity)
            records += F.records(session.read("sec.xbrl_frames", as_of=as_of, **identity))
        if alias:
            records = _revenue_lead(records)
        ciks = {}
        if asked:
            sec_source = session.module("omni.sources.sec")
            errors = session.module("omni.errors")
            for t in asked:
                try:
                    ciks[t] = f"{sec_source.cik_for(t):010d}"
                except errors.PermanentError:  # not an SEC filer: named in the note as missing
                    ciks[t] = None
        return records, ciks

    (records, ciks), session = await common.run(
        ctx, s, work, source="sec", subject=("frame", f"{args.tag} {args.period}")
    )
    by_cik = {c: t for t, c in ciks.items() if c is not None}
    rows = []
    for r in records:
        cik = F.text(r["cik"])
        if asked and cik not in by_cik:
            continue
        rows.append(
            common.row(
                XbrlFrameValue,
                {
                    "taxonomy": F.text(r["taxonomy"]),
                    "tag": F.text(r["tag"]),
                    "unit": F.text(r["unit"]),
                    "period": F.text(r["period"]),
                    "cik": cik,
                    "ticker": by_cik.get(cik) or F.text(r["ticker"]),
                    "entity_name": F.text(r["entity_name"]),
                    "period_start": F.day(r["period_start"]),
                    "period_end": F.day(r["period_end"]),
                    "value": F.number(r["value"]),
                    "accession_number": F.text(r["accession_number"]),
                    "filing_date": F.day(r["filing_date"]),
                    "stamp": F.text(r["stamp"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"ticker": NPS, "period_start": NA, "filing_date": NA},
            )
        )
    rows.sort(key=lambda r: r.cik)
    rows.sort(key=lambda r: r.value, reverse=args.sort == "desc")
    rows = rows[: args.limit]
    found = {r.cik for r in rows}
    missing = [t for t in asked if ciks.get(t) not in found]
    notes = (
        [f"No {args.tag} value for {args.period} in the frame for: {', '.join(missing)}."] if missing else []
    )
    if alias:
        notes.append(
            "tag revenue: per filer RevenueFromContractWithCustomerExcludingAssessedTax, or Revenues where the "
            "filer states only that or a Revenues larger by more than 0.5%; each row's tag names its concept."
        )
    prov = _prov(
        ctx,
        "sec_xbrl_frames",
        datasets=("sec.xbrl_frames",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "tag": args.tag,
            "period": args.period,
            "taxonomy": args.taxonomy,
            "unit": unit,
            "tickers": args.tickers,
            "sort": args.sort,
            "limit": args.limit,
            "as_of": args.as_of,
        },
    )
    return _output(ctx, s, XbrlFrameValue, rows, prov, notes=notes)


async def sec_pay_vs_performance(ctx: ToolContext, args: PayVsPerformanceInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    identity = {"ticker": args.ticker}

    def work(session):
        index_fresh = session.refresh("sec.submissions", identity)
        # derived from the stored filings index: read again whenever the index was just fetched
        session.refresh("sec.pay_vs_performance", identity, force=not index_fresh)
        return F.records(session.read("sec.pay_vs_performance", as_of=as_of, **session.company(args.ticker)))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    wanted = set(args.measures or PVP_MEASURES)
    rows = [
        common.row(
            PayVsPerformance,
            {
                "ticker": args.ticker,
                "cik": F.text(r["cik"]),
                "fiscal_year": F.integer(r["fiscal_year"]),
                "measure": F.text(r["measure"]),
                "executive": F.text(r["executive"]),
                "executive_name": F.text(r["executive_name"]),
                "concept": F.text(r["concept"]),
                "value": F.number(r["value"]),
                "unit": F.text(r["unit"]),
                "measure_name": F.text(r["measure_name"]),
                "period_start": F.day(r["period_start"]),
                "period_end": F.day(r["period_end"]),
                "accession_number": F.text(r["accession_number"]),
                "form": F.text(r["form"]),
                "filing_date": F.day(r["filing_date"]),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
            codes={"executive": NA, "executive_name": NPS, "measure_name": NA},
        )
        for r in records
        if F.text(r["measure"]) in wanted
    ]
    rows.sort(key=lambda r: (PVP_MEASURES.index(r.measure), r.executive or ""))
    rows.sort(key=lambda r: r.fiscal_year, reverse=True)
    prov = _prov(
        ctx,
        "sec_pay_vs_performance",
        datasets=("sec.submissions", "sec.pay_vs_performance"),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "measures": args.measures, "as_of": args.as_of},
    )
    return _output(ctx, s, PayVsPerformance, rows, prov)


async def sec_xbrl_facts(ctx: ToolContext, args: FactsInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    if args.start and args.end:
        common.window(ctx.tool, args.start, args.end, default_start=args.start, default_end=args.end)
    identity = {"ticker": args.ticker}
    taxonomy = {"taxonomy": args.taxonomy} if args.taxonomy else {}

    def work(session):
        session.refresh("sec.company_facts", identity)
        filters = {**session.company(args.ticker, unpadded=True), **taxonomy}  # its CIK unpadded
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
                    "ticker": args.ticker,
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
    return _output(ctx, s, XbrlFact, rows, prov)


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
        return F.records(session.read("sec.fundamentals", as_of=as_of, **session.company(args.ticker)))

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
                    "ticker": args.ticker,
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
    return _output(ctx, s, FundamentalValue, rows, prov)


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
        return F.records(session.read("sec.earnings_releases", as_of=as_of, **session.company(args.ticker)))

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
                    "ticker": args.ticker,
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
    return _output(ctx, s, EarningsRelease, rows, prov)


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
        company = session.company(args.ticker)
        return F.records(session.read("sec.earnings_press_release_figures", as_of=as_of, **company))

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
                    "ticker": args.ticker,
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
    return _output(ctx, s, EarningsFigure, rows, prov)


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
        company = session.company(args.ticker)
        return F.records(session.read("sec_insider.transactions", as_of=as_of, **company))

    records, session = await common.run(ctx, s, work, source="sec_insider", subject=("filer", args.ticker))
    forms = set().union(*(FORM_GROUPS[f] for f in args.forms))
    rows = []
    for r in records:
        form, when = F.text(r["form"]), F.day(r["transaction_date"])
        # A Form 4's holdings listed without a transaction (a trust's shares, an
        # option) are stored beside its transactions as row_kind "holding": not
        # trades. A store written before row_kind has no column and no such rows.
        if form not in forms or r.get("row_kind") == "holding" or not F.within(when, start, end):
            continue
        notice = form.startswith("144")
        rows.append(
            common.row(
                InsiderTransaction,
                {
                    "ticker": args.ticker,
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
                    # A Form 144 states shares and an aggregate market value, no
                    # price: the stored price of a notice is the value over the
                    # shares, no price anyone traded at.
                    "price_per_share": None if notice else F.number(r["price_per_share"]),
                    "value_usd": F.number(r["value_usd"]),
                    "shares_owned_after": F.number(r["shares_owned_after"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "insider_cik": NPS,
                    "officer_title": NA,
                    "price_per_share": NA if notice else AbsenceCode.NO_DATA,
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
    return _output(ctx, s, InsiderTransaction, rows, prov)


def _last_weekday_before(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


async def sec_daily_index(ctx: ToolContext, args: DailyIndexInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    day = args.day or _last_weekday_before(_today(ctx))
    identity = {"index_date": day.isoformat()}

    def work(session):
        session.refresh("sec.daily_index", identity)
        filters = {"index_date": day}
        if args.forms:
            filters["form"] = list(args.forms)
        if args.cik:
            filters["cik"] = args.cik
        return F.records(session.read("sec.daily_index", as_of=as_of, **filters))

    records, session = await common.run(ctx, s, work, source="sec")
    rows = [
        common.row(
            DailyIndexEntry,
            {
                "index_date": F.day(r["index_date"]),
                "form": F.text(r["form"]),
                "company": F.text(r["company"]),
                "cik": F.text(r["cik"]),
                "date_filed": F.day(r["date_filed"]),
                "accession_number": F.text(r["accession_number"]),
                "file_url": f"https://www.sec.gov/Archives/{F.text(r['file_name'])}",
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
        )
        for r in records
    ]
    rows.sort(key=lambda r: (r.form, r.company, r.accession_number, r.cik))
    notes = []
    if not rows:
        notes.append(
            f"EDGAR lists no form index for {day.isoformat()} (a weekend, a federal holiday, or a day "
            "not posted yet: EDGAR posts a business day's index at about 22:01 ET)."
        )
    prov = _prov(
        ctx,
        "sec_daily_index",
        datasets=("sec.daily_index",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"day": day, "forms": args.forms, "cik": args.cik, "as_of": args.as_of},
    )
    return _output(ctx, s, DailyIndexEntry, rows, prov, notes=notes)


def _json_list(raw: str | None) -> list[dict]:
    """A stored JSON list of objects, decoded; [] for anything else."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return []
    return [d for d in decoded if isinstance(d, dict)] if isinstance(decoded, list) else []


async def sec_form144(ctx: ToolContext, args: Form144Inputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    since = args.since or common.years_ago(_today(ctx), 1)
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.form144", identity)
        company = session.company(args.ticker, column="issuer_cik")  # the notice's issuer
        return F.records(session.read("sec.form144", as_of=as_of, **company))

    records, session = await common.run(ctx, s, work, source="sec", subject=("issuer", args.ticker))
    rows = []
    for r in records:
        filed = F.day(r["filing_date"])
        if filed is None or filed < since:
            continue
        cik, accession = F.text(r["issuer_cik"]), F.text(r["accession_number"])
        sold = F.flag(r["nothing_sold_past_3_months"])
        rows.append(
            common.row(
                Form144Notice,
                {
                    "ticker": args.ticker,
                    "issuer_cik": cik,
                    "issuer_name": F.text(r["issuer_name"]),
                    "accession_number": accession,
                    "line": (F.integer(r["line"]) or 0) + 1,
                    "form": F.text(r["form"]),
                    "filing_date": filed,
                    "accepted_at": F.instant(r["event_time"]),
                    "filer_cik": F.text(r["filer_cik"]),
                    "seller_name": F.text(r["seller_name"]) or "(unnamed)",
                    "relationship": F.text(r["relationship"]),
                    "security_class": F.text(r["security_class"]),
                    "broker_name": F.text(r["broker_name"]),
                    "shares_to_sell": F.number(r["shares_to_sell"]),
                    "aggregate_market_value": F.number(r["aggregate_market_value"]),
                    "shares_outstanding": F.number(r["shares_outstanding"]),
                    "approx_sale_date": F.day(r["approx_sale_date"]),
                    "exchange": F.text(r["exchange"]),
                    "has_10b5_1": bool(F.flag(r["has_10b5_1"])),
                    "plan_adoption_date": F.day(r["plan_adoption_date"]),
                    "notice_date": F.day(r["notice_date"]),
                    "acquisitions": _json_list(F.text(r["acquisitions"])),
                    "sales_past_3_months": _json_list(F.text(r["sales_past_3_months"])),
                    "nothing_sold_past_3_months": sold,
                    "remarks": F.text(r["remarks"]),
                    "index_url": _index_url(cik, accession),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"plan_adoption_date": NA, "filer_cik": NPS},
            )
        )
    rows.sort(key=lambda r: r.line)
    rows.sort(key=lambda r: (r.accepted_at, r.accession_number), reverse=True)
    kept = list(dict.fromkeys(r.accession_number for r in rows))[: args.limit]
    rows = [r for r in rows if r.accession_number in set(kept)]
    prov = _prov(
        ctx,
        "sec_form144",
        datasets=("sec.form144",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "since": since, "limit": args.limit, "as_of": args.as_of},
    )
    notes = [
        "A Form 144 announces a proposed sale under Rule 144; it is not a trade (sec_insider_trades has Form 4s)."
    ]
    return _output(ctx, s, Form144Notice, rows, prov, notes=notes)  # external_text: the filer's words


async def sec_ipo_pipeline(ctx: ToolContext, args: IpoInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    today = _today(ctx)
    start, end = common.window(
        ctx.tool,
        args.start,
        args.end,
        default_start=today - timedelta(days=14),
        default_end=today,
        max_days=IPO_MAX_DAYS,
    )
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def work(session):
        # the pipeline is read from the stored daily index: each weekday's file first
        fetched = False
        for d in days:
            if d.weekday() < 5 and d <= today:
                fetched |= not session.refresh("sec.daily_index", {"index_date": d.isoformat()})
        session.refresh("sec.ipo_filings", {"since": start.isoformat()}, force=fetched)
        return F.records(session.read("sec.ipo_filings", as_of=as_of))

    records, session = await common.run(ctx, s, work, source="sec")
    stages, categories = (
        set(args.stages or IPO_STAGES),
        set(args.categories or ("operating", "spac", "trust")),
    )
    rows = []
    for r in records:
        filed, stage, category = F.day(r["filing_date"]), F.text(r["stage"]), F.text(r["category"])
        if not F.within(filed, start, end) or stage not in stages or category not in categories:
            continue
        form = F.text(r["form"])
        notice = form in ("EFFECT", "RW")
        rows.append(
            common.row(
                IpoFiling,
                {
                    "cik": F.text(r["cik"]),
                    "company": F.text(r["company"]),
                    "form": form,
                    "stage": stage,
                    "filing_date": filed,
                    "accepted_at": F.instant(r["event_time"]),
                    "accession_number": F.text(r["accession_number"]),
                    "file_number": F.text(r["file_number"]),
                    "registration_form": F.text(r["registration_form"]),
                    "ticker": F.text(r["ticker"]),
                    "exchange": F.text(r["exchange"]),
                    "price_low": F.number(r["price_low"]),
                    "price_high": F.number(r["price_high"]),
                    "offer_price": F.number(r["offer_price"]),
                    "shares_offered": F.number(r["shares_offered"]),
                    "offered_unit": F.text(r["offered_unit"]),
                    "effective_at": F.instant(r["effective_at"]),
                    "category": category,
                    "sic": F.text(r["sic"]),
                    "primary_doc_url": F.text(r["primary_doc_url"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    **dict.fromkeys(
                        ("ticker", "exchange", "price_low", "price_high", "shares_offered", "offered_unit"),
                        NA if notice else AbsenceCode.NO_DATA,
                    ),
                    "offer_price": AbsenceCode.NO_DATA if form == "424B4" else NA,
                    "effective_at": AbsenceCode.NO_DATA if form == "EFFECT" else NA,
                    "sic": NPS,
                },
            )
        )
    rows.sort(key=lambda r: (r.accepted_at, r.accession_number, r.cik), reverse=True)
    prov = _prov(
        ctx,
        "sec_ipo_pipeline",
        datasets=("sec.daily_index", "sec.ipo_filings"),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "start": start,
            "end": end,
            "stages": args.stages,
            "categories": args.categories,
            "as_of": args.as_of,
        },
    )
    notes = [
        "New issuers only: a company with a periodic report, a 424B4 or an exchange registration (8-A) more "
        "than 30 days before the filing is left out, as is a prospectus that does not state an IPO. Ticker, "
        "exchange, price range, offer price and shares come from the prospectus cover, best effort: null "
        "when the cover does not state them."
    ]
    return _output(ctx, s, IpoFiling, rows, prov, notes=notes)  # external_text: company names, covers


def _filings(rows: list[dict], amendment_type: str) -> list[tuple]:
    """(filing_date, knowledge_time, accession) of each filing of that amendment type, in filing order."""
    return sorted(
        {
            (F.day(r["filing_date"]), F.instant(r["knowledge_time"]), F.text(r["accession_number"]))
            for r in rows
            if (F.text(r["amendment_type"]) or "").upper() == amendment_type
        }
    )


def _restated(records: list[dict], period) -> tuple[list[dict], list[str]]:
    """One portfolio per period: a restatement replaces everything filed before it
    for the period (the latest restatement wins); new-holdings amendments add to
    what stands when they are filed, so those filed after the restatement add to it."""
    rows = [r for r in records if F.day(r["period_end"]) == period]
    restatements = _filings(rows, "RESTATEMENT")
    if restatements:
        *filed, winner = restatements[-1]
        later = list(dict.fromkeys(acc for *when, acc in _filings(rows, "NEW HOLDINGS") if when > filed))
        note = f"A restatement ({winner}) replaces the earlier 13F-HR filings for {period.isoformat()}"
        if later:
            one = len(later) == 1
            note += (
                f"; the new-holdings amendment{'' if one else 's'} filed after it ({', '.join(later)}) "
                f"add{'s' if one else ''} to it"
            )
        keep = {winner, *later}
        return [r for r in rows if F.text(r["accession_number"]) in keep], [note + "."]
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
    return _output(ctx, s, InstitutionalHolding, rows, prov, notes=notes)


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
    return _output(ctx, s, FundReport, rows, prov)


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
    return _output(ctx, s, FundHolding, rows, prov)


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
    "sec_xbrl_frames": sec_xbrl_frames,
    "filings_events": filings_events,
    "sec_pay_vs_performance": sec_pay_vs_performance,
    "sec_fundamentals": sec_fundamentals,
    "sec_earnings_releases": sec_earnings_releases,
    "sec_earnings_figures": sec_earnings_figures,
    "sec_insider_trades": sec_insider_trades,
    "sec_form144": sec_form144,
    "sec_ipo_pipeline": sec_ipo_pipeline,
    "sec_daily_index": sec_daily_index,
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
        "filings_events",
        "SEC material filing events",
        common.describe(
            "A company's material filing events: one row per 8-K / 8-K/A item on the allowlist (1.01 material "
            "agreement, 1.02 termination, 2.01 acquisition or disposition, 2.03 direct financial obligation, "
            "2.05 exit costs, 2.06 impairment, 3.02 unregistered equity sale, 5.02 officer or director change, "
            "7.01 Reg FD, 8.01 other events) and per Form 6-K, newest first, with SEC's title for the item, "
            "the filing's exhibit URLs (EX-4.1, EX-10.1, EX-99.1, ...) and the first 300 characters of the "
            "item's section (null when none was found). An 8-K carrying only 2.02 and 9.01 is an earnings "
            f"release (sec_earnings_releases), not an event. Filter by items and since. {CONTACT}",
            "at_event",
        ),
        "Material 8-K items and 6-Ks of a company with their exhibit URLs",
        EventsInputs,
        FilingEvent,
        "sec",
        ROUTES["filings_events"],
        risk="external_text",
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
        "sec_xbrl_frames",
        "SEC XBRL frames",
        common.describe(
            "One XBRL concept for every filer in one calendar period, from the SEC's frames API (a peer "
            "comparison): per filer the value of the fact the SEC finds last filed for the period, the filer's "
            "name and ticker, the fact's own period and the reporting filing, sorted by value. Period CY2025, "
            "CY2025Q2 or CY2025Q2I (an instant, for balances). knowledge_time is the reporting filing's EDGAR "
            "acceptance, or the end of its filing date from EDGAR's quarterly XBRL index (stamp says which; the "
            "first read of a filing year downloads that index, about 0.5 MB a quarter); a restated value is "
            f"served from the restating filing. {CONTACT}",
            "vintage",
        ),
        "One XBRL concept across filers in a calendar period",
        FramesInputs,
        XbrlFrameValue,
        "sec",
        ROUTES["sec_xbrl_frames"],
    ),
    _spec(
        "sec_pay_vs_performance",
        "SEC pay versus performance",
        common.describe(
            "A company's pay-versus-performance table from its proxies' XBRL (Reg. S-K Item 402(v), DEF 14A "
            "and DEFR14A since 2023): per fiscal year the PEO's Summary Compensation Table total and "
            "compensation actually paid (one row per PEO when the year had two), the other named executives' "
            "averages, company and peer-group TSR (value of $100 invested at the start of the proxy's first "
            "year), net income and the company-selected measure with its name (filer text: untrusted). Every "
            f"proxy restates the earlier years; as_of reads the proxy known then. {CONTACT} "
            + common.PIT["at_event"],
            "vintage",
        ),
        "Pay versus performance from proxy statements",
        PayVsPerformanceInputs,
        PayVsPerformance,
        "sec",
        ROUTES["sec_pay_vs_performance"],
        risk="external_text",
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
        "sec_form144",
        "SEC Form 144 notices",
        common.describe(
            "A company's Form 144 notices of proposed sale (144 and 144/A), every field the notice states, one "
            "row per class and broker it lists: seller and relationship to the issuer as filed, class, broker, "
            "shares to be sold, aggregate market value (USD), shares outstanding, approximate sale date, exchange, "
            "Rule 10b5-1 plan adoption date, notice date, remarks, how the securities were acquired and what the "
            "seller sold in the past three months. A notice announces a sale; it is not one. accepted_at is "
            f"EDGAR's acceptance. History from filings of 2026-01-01 on a first fetch. {CONTACT}",
            "at_event",
        ),
        "Form 144 notices of proposed sale of a company's stock",
        Form144Inputs,
        Form144Notice,
        "sec",
        ROUTES["sec_form144"],
        risk="external_text",
    ),
    _spec(
        "sec_ipo_pipeline",
        "SEC IPO pipeline",
        common.describe(
            "The US IPO pipeline from EDGAR, by filing date (at most 31 days a call): new issuers' S-1 and F-1 "
            "(stage filed), their amendments (amended), the SEC's EFFECT notice (effective), the 424B4 final "
            "prospectus (priced) and RW withdrawals (withdrawn), with the cover's proposed ticker, exchange, "
            "price range, offer price and shares offered (best effort; null when not stated), the "
            "registration's file number and the category (operating, spac, trust). Read from EDGAR's daily "
            f"form index; a company that already files periodic reports is left out. {CONTACT}",
            "at_event",
        ),
        "IPO filings of new issuers: filed, amended, effective, priced, withdrawn",
        IpoInputs,
        IpoFiling,
        "sec",
        ROUTES["sec_ipo_pipeline"],
        risk="external_text",
    ),
    _spec(
        "sec_daily_index",
        "EDGAR daily form index",
        common.describe(
            "Every filing EDGAR disseminated on one business day, from its daily form index: form, company, "
            "CIK, date filed, accession number and the filing's text file URL, one line per filing and company "
            "it is listed under. Filter by forms and cik. knowledge_time is when EDGAR posted the index (about "
            f"22:01 ET), after each filing's own acceptance. {CONTACT}",
            "lagged",
        ),
        "Every filing of a business day from EDGAR's daily form index",
        DailyIndexInputs,
        DailyIndexEntry,
        "sec",
        ROUTES["sec_daily_index"],
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
    "sec_xbrl_frames": ("sec.xbrl_frames",),
    "filings_events": ("sec.submissions", "sec.filing_events"),
    "sec_pay_vs_performance": ("sec.submissions", "sec.pay_vs_performance"),
    "sec_fundamentals": ("sec.company_facts", "sec.fundamentals"),
    "sec_earnings_releases": ("sec.earnings_releases",),
    "sec_earnings_figures": ("sec.earnings_press_release_figures",),
    "sec_insider_trades": ("sec_insider.transactions",),
    "sec_form144": ("sec.form144",),
    # read from the stored daily index, which it refreshes first; it reads only the pipeline
    "sec_ipo_pipeline": ("sec.ipo_filings",),
    "sec_daily_index": ("sec.daily_index",),
    "sec_13f_holdings": ("sec13f.holdings",),
    "sec_fund_nport": ("sec.fund_nport",),
    "sec_fund_holdings": ("sec.fund_nport_holdings",),
}
