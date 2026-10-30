"""Canonical models of the official-data tools (schema 1.1).

Built by the ``data`` provider (the ``marketlens-mcp[data]`` extra, which reads
the publishers through marketlens-data); the models themselves are built in, so
their JSON Schemas exist whether or not the extra is installed.

Conventions of base.py hold, plus:

* ``knowledge_time`` (every model) is when the value became knowable under its
  dataset's point-in-time policy: a vintage or acceptance instant where the
  publisher states one, an approximation that is never early otherwise. Its
  JSON Schema says ``x-role: available`` (the stored column's role), so
  analytics_align matches a row from the later of its time and its
  knowledge_time: never at a time before it was known, nor before its own (a
  schedule known in advance counts from each date).
* Rates and percentages are fractions (a 4.33 % yield is 0.0433), money is in
  US dollars (millions and billions multiplied out). A value published in the
  publisher's own unit is ``as_published`` and the row names that unit.
* ``None`` is always explained: ``no_data`` for a publisher's missing value,
  ``not_applicable`` for a field another source or another kind of row fills,
  ``not_provided_by_source`` for a field this source never gives.
* Text written by companies, the Fed or a calendar vendor (press-release
  lines, statements, event descriptions) is UNTRUSTED: the tools that return
  it mark their responses ``external_text``.

FilingSearchHit is built by the built-in official provider (filings_search),
not the extra; FilingEvent by the extra's filings_events, XbrlFrameValue by
sec_xbrl_frames, PayVsPerformance by sec_pay_vs_performance, EarningsGuidance,
NonGaapFigure, SegmentFact, ForeignQuarterlyFigure, IrDocument and IrTarget by
the tools of the same datasets; TaiwanMonthlyRevenue by twse_monthly_revenue.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any, ClassVar, Literal

from pydantic import Field

from .base import CanonicalModel, UtcDatetime
from .base import unit as _unit

KNOWN = "When the value became knowable under the dataset's point-in-time policy"


def _known() -> Any:
    return _unit("UTC", description=KNOWN, json_schema_extra={"x-role": "available"})


# --- macro --------------------------------------------------------------------------------


class EconomicObservation(CanonicalModel):
    """One observation of an economic series (FRED/ALFRED, BLS or a BEA NIPA line)."""

    schema_name: ClassVar[str] = "marketlens.EconomicObservation"
    time_column: ClassVar[str | None] = "date"
    group_column: ClassVar[str | None] = "series_id"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    source: Literal["fred", "bls", "bea"]
    series_id: str = Field(description="FRED or BLS series id; a BEA line's series code")
    title: str | None = Field(
        default=None, description="Series name (FRED catalogue) or BEA line description"
    )
    date: Date = Field(description="First day of the period the value describes")
    period: str | None = Field(
        default=None,
        description="Publisher's period code: M01..M12, Q1..Q4 (BEA), Q01..Q04 (BLS) or A (None for FRED)",
    )
    frequency: str | None = Field(default=None, description="A, Q or M where the source states it")
    value: float | None = _unit("as_published", default=None, description="In the row's `units`")
    units: str | None = Field(default=None, description="The publisher's unit for `value`")
    table_name: str | None = Field(default=None, description="BEA NIPA table, e.g. T10101")
    line_number: int | None = _unit("ordinal", default=None, description="BEA table line")
    knowledge_time: UtcDatetime = _known()
    pit: Literal["vintage", "lagged"] = Field(
        description="vintage: the value as published at knowledge_time; lagged: release-lag approximation"
    )


class TradeFlow(CanonicalModel):
    """US goods trade in one HS commodity with one partner for one month (Census)."""

    schema_name: ClassVar[str] = "marketlens.TradeFlow"
    time_column: ClassVar[str | None] = "month"
    group_column: ClassVar[str | None] = "country_code"
    value_columns: ClassVar[tuple[str, ...]] = ("value_usd",)

    flow: Literal["exports", "imports"] = Field(description="Exports (total value) or general imports")
    hs_code: str = Field(description="Harmonized System code, e.g. 8542 (integrated circuits)")
    hs_level: str | None = Field(default=None, description="HS2, HS4, HS6 or HS10")
    description: str | None = Field(default=None, description="Census's short description of the code")
    country_code: str = Field(description="Census country code (5700 China); TOTAL for all countries")
    country_name: str | None = None
    summary_level: str | None = Field(default=None, description="DET a country, CGP a country group")
    month: Date = Field(description="First day of the statistical month")
    value_usd: float | None = _unit("USD", default=None, description="Not seasonally adjusted")
    value_field: str = Field(description="Census's variable: ALL_VAL_MO (exports), GEN_VAL_MO (imports)")
    quantity: float | None = _unit(
        "as_published", default=None, description="First quantity, in `quantity_unit` (HS10 codes only)"
    )
    quantity_unit: str | None = Field(default=None, description="Census's unit code: NO, KG, ...")
    stamp_estimated: bool = Field(description="knowledge_time is an estimate, not the calendar's release")
    knowledge_time: UtcDatetime = _known()


class CentralBankObservation(CanonicalModel):
    """One observation of a Bank of Japan or Bank of England statistical series."""

    schema_name: ClassVar[str] = "marketlens.CentralBankObservation"
    time_column: ClassVar[str | None] = "date"
    group_column: ClassVar[str | None] = "series_code"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    bank: Literal["boj", "boe"]
    database: str = Field(description="The BoJ database (FM01, MD01, CO, ...) or IADB for the BoE")
    series_code: str = Field(description="The bank's series code, e.g. STRDCLUCON or IUDBEDR")
    period: str = Field(description="2026-10-22 (daily), 2026-09 (monthly), 2026-Q3 (quarterly)")
    date: Date = Field(description="First day of the period")
    value: float | None = _unit("as_published", default=None, description="In the row's `units`")
    units: str | None = Field(default=None, description="The bank's unit text (BoJ); the BoE states none")
    knowledge_time: UtcDatetime = _known()


class RegionalSurveyIndex(CanonicalModel):
    """One Richmond or Kansas City Fed business survey index for one month."""

    schema_name: ClassVar[str] = "marketlens.RegionalSurveyIndex"
    time_column: ClassVar[str | None] = "month"
    group_column: ClassVar[str | None] = "series"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    bank: Literal["richmond", "kansascity"]
    survey: Literal["manufacturing", "services"]
    series: str = Field(
        description="Richmond's column name (sa_mfg_composite) or index_heading (Kansas City)"
    )
    label: str = Field(description="The index as the bank's workbook words it")
    seasonally_adjusted: bool
    month: Date = Field(description="First day of the survey month")
    value: float | None = _unit("as_published", default=None, description="In the row's `unit`")
    unit: str = Field(description="diffusion_index (percent up less percent down) or percent")
    release: UtcDatetime = _unit("UTC", description="The month's release on the bank's schedule")
    release_estimated: bool = Field(description="No schedule lists the month: `release` is an estimate")
    knowledge_time: UtcDatetime = _known()


class TraceAggregate(CanonicalModel):
    """One metric of a FINRA TRACE market aggregate row."""

    schema_name: ClassVar[str] = "marketlens.TraceAggregate"
    time_column: ClassVar[str | None] = "date"
    group_column: ClassVar[str | None] = "metric"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    report: str = Field(description="FINRA's dataset: corporateMarketBreadth, treasuryDailyAggregates, ...")
    date: Date = Field(description="Trade (report) date; a monthly report's first day")
    product_category: str = Field(description="FINRA's productCategory")
    breakdown: str = Field(description="Trade type, or 'years to maturity | benchmark', or empty")
    metric: str = Field(description="FINRA's field: advances, totalVolume, dealerCustomerVolume, ...")
    value: float | None = _unit("as_published", default=None, description="A count, volume or price")
    stamp_estimated: bool = Field(description="knowledge_time is marketlens-data's conservative rule")
    knowledge_time: UtcDatetime = _known()


class EconomicSeriesInfo(CanonicalModel):
    """One series of the FRED catalogue marketlens-data ships."""

    schema_name: ClassVar[str] = "marketlens.EconomicSeriesInfo"
    group_column: ClassVar[str | None] = "series_id"

    series_id: str
    name: str | None = None
    category: str | None = None
    units: str | None = Field(default=None, description="Unit label, e.g. % or Index 1982-84=100")
    chart_type: str | None = Field(default=None, description="line (a level) or bar (a flow or change)")
    polarity: str | None = Field(
        default=None, description="up_is_risk_on, up_is_risk_off or none (no view on the sign)"
    )


class ReleaseScheduleEntry(CanonicalModel):
    """One scheduled BLS release (CPI or the Employment Situation)."""

    schema_name: ClassVar[str] = "marketlens.ReleaseScheduleEntry"
    time_column: ClassVar[str | None] = "scheduled_at"
    group_column: ClassVar[str | None] = "release"

    release: Literal["cpi", "employment_situation"]
    reference_month: Date = Field(description="First day of the month the release describes")
    start_date: Date
    end_date: Date
    scheduled_at: UtcDatetime = _unit("UTC", description="The official release instant")
    knowledge_time: UtcDatetime = _known()


# --- SEC EDGAR ------------------------------------------------------------------------------


class Filing(CanonicalModel):
    """One EDGAR filing (the filings index, not its content)."""

    schema_name: ClassVar[str] = "marketlens.Filing"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "ticker"

    ticker: str = Field(description="SEC style, e.g. BRK-B")
    cik: str = Field(description="10-digit, zero-padded")
    company_name: str | None = None
    form: str = Field(description="10-K, 10-Q, 8-K, 4, DEF 14A, ...")
    filing_date: Date
    accepted_at: UtcDatetime = _unit(
        "UTC", description="EDGAR acceptance; the end of the filing day in New York when EDGAR states none"
    )
    accession_number: str
    report_date: Date | None = None
    items: list[str] = Field(default_factory=list, description="8-K item codes, e.g. 2.02")
    primary_doc_url: str | None = None
    sic: str | None = None
    sic_description: str | None = None
    knowledge_time: UtcDatetime = _known()


class FilingSearchHit(CanonicalModel):
    """One document of an EDGAR filing that SEC's full-text search matched: the
    filing's index entry and the document's address, not its text.
    entity_name and document_type are written by the filers: UNTRUSTED."""

    schema_name: ClassVar[str] = "marketlens.FilingSearchHit"
    time_column: ClassVar[str | None] = "filing_date"
    group_column: ClassVar[str | None] = "cik"

    accession_number: str = Field(description="0000000000-00-000000")
    cik: str = Field(description="10-digit, zero-padded: the first entity EDGAR lists for the filing")
    entity_name: str | None = Field(
        default=None,
        description="That entity's name as EDGAR shows it (usually the filer; for a Form 144, the issuer)",
    )
    ciks: list[str] = Field(
        default_factory=list, description="Every entity EDGAR lists for the filing, 10-digit, in its order"
    )
    form: str = Field(description="As filed: 13F-HR, 13F-HR/A, 8-K, SC 13D, ...")
    filing_date: Date
    report_date: Date | None = Field(default=None, description="The period the filing reports on")
    document: str = Field(description="The matched file's name in the filing (primary_doc.xml, ex99-1.htm)")
    document_type: str | None = Field(
        default=None,
        description="EDGAR's type of that file: the form for the main document, EX-99.1 for an exhibit",
    )
    document_url: str
    filing_url: str = Field(description="The filing's index page on www.sec.gov")
    knowledge_time: UtcDatetime = _known()


class FilingEvent(CanonicalModel):
    """One material event a company filed: an 8-K item on marketlens-data's
    allowlist (1.01, 1.02, 2.01, 2.03, 2.05, 2.06, 3.02, 5.02, 7.01, 8.01) or a
    Form 6-K (item "6-K"), with the filing's exhibit URLs and the first 300
    characters of the item's own section. headline is the filer's prose and
    item_title SEC's title for the code; the headline is UNTRUSTED text."""

    schema_name: ClassVar[str] = "marketlens.FilingEvent"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "ticker"

    ticker: str = Field(description="SEC style, e.g. BRK-B")
    cik: str = Field(description="10-digit, zero-padded")
    form: str = Field(description="8-K, 8-K/A or 6-K")
    filing_date: Date = Field(
        description="SEC's filing date; an acceptance after 17:30 ET is dated the next business day"
    )
    accepted_at: UtcDatetime = _unit(
        "UTC", description="EDGAR acceptance; the end of the filing day in New York when EDGAR states none"
    )
    accession_number: str = Field(description="0000000000-00-000000")
    item: str = Field(description="The 8-K item code (1.01, 3.02, ...) or 6-K")
    item_title: str = Field(description="SEC's title for the item code")
    headline: str | None = Field(
        default=None,
        description="The first 300 characters of the item's section in the primary document; None when no "
        "section was found (never invented)",
    )
    primary_doc_url: str | None = None
    exhibits: dict[str, str] = Field(
        default_factory=dict,
        description="Exhibit type -> URL on www.sec.gov (EX-4.1, EX-10.1, EX-99.1, ...); the bodies are not stored",
    )
    index_url: str = Field(description="The filing's index page on www.sec.gov")
    knowledge_time: UtcDatetime = _known()


class DailyIndexEntry(CanonicalModel):
    """One line of EDGAR's daily form index: a filing EDGAR disseminated that
    business day, listed under one of its companies (a filing made by several,
    a Form 144's issuer and seller or co-registrants, has a line for each)."""

    schema_name: ClassVar[str] = "marketlens.DailyIndexEntry"
    time_column: ClassVar[str | None] = "date_filed"
    group_column: ClassVar[str | None] = "form"

    index_date: Date = Field(description="The day of the index file (form.YYYYMMDD.idx)")
    form: str = Field(description="Form type as the index states it (cut at 16 characters)")
    company: str = Field(description="The company the line lists the filing under")
    cik: str = Field(description="10-digit, zero-padded")
    date_filed: Date = Field(
        description="As the line states it (an EFFECT notice is in the next day's index)"
    )
    accession_number: str = Field(description="0000000000-00-000000")
    file_url: str = Field(description="The filing's complete submission text file on www.sec.gov")
    knowledge_time: UtcDatetime = _known()


class Form144Notice(CanonicalModel):
    """One securitiesInformation block of a Form 144 notice of proposed sale (a class
    and a broker), with the notice's seller, relationship, plan date and its
    acquisitions and past-three-month sales. A notice announces a sale; it is not one.
    seller_name, relationship, remarks and the lists are the filer's text: UNTRUSTED."""

    schema_name: ClassVar[str] = "marketlens.Form144Notice"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("aggregate_market_value",)

    ticker: str | None = Field(default=None, description="The issuer's ticker, SEC style")
    issuer_cik: str = Field(description="10-digit, zero-padded")
    issuer_name: str | None = None
    accession_number: str
    line: int = _unit("ordinal", description="Position of the block in the notice (1 = first)")
    form: str = Field(description="144 or 144/A")
    filing_date: Date
    accepted_at: UtcDatetime = _unit("UTC", description="EDGAR acceptance of the notice")
    filer_cik: str | None = None
    seller_name: str = Field(description="The person for whose account the securities are to be sold")
    relationship: str | None = Field(default=None, description="Relationship to the issuer as filed")
    security_class: str | None = None
    broker_name: str | None = None
    shares_to_sell: float | None = _unit("shares", default=None)
    aggregate_market_value: float | None = _unit("USD", default=None, description="As filed")
    shares_outstanding: float | None = _unit("shares", default=None)
    approx_sale_date: Date | None = Field(default=None, description="The intended sale date, as filed")
    exchange: str | None = None
    has_10b5_1: bool = Field(description="The notice gives a Rule 10b5-1 plan adoption date")
    plan_adoption_date: Date | None = None
    notice_date: Date | None = None
    acquisitions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="How the securities were acquired: class, acquired_date, nature_of_acquisition, "
        "acquired_from, is_gift, amount, payment_date, nature_of_payment",
    )
    sales_past_3_months: list[dict[str, Any]] = Field(
        default_factory=list, description="seller, security_class, sale_date, amount, gross_proceeds (USD)"
    )
    nothing_sold_past_3_months: bool | None = None
    remarks: str | None = None
    index_url: str = Field(description="The filing's index page on www.sec.gov")
    knowledge_time: UtcDatetime = _known()


class IpoFiling(CanonicalModel):
    """One IPO filing of a new issuer: an S-1 / F-1 (filed), an amendment (amended),
    the SEC's EFFECT notice (effective), the 424B4 final prospectus (priced) or an RW
    withdrawal (withdrawn), with what the prospectus cover states (best effort)."""

    schema_name: ClassVar[str] = "marketlens.IpoFiling"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "cik"
    value_columns: ClassVar[tuple[str, ...]] = ("offer_price",)

    cik: str = Field(description="10-digit, zero-padded")
    company: str
    form: str = Field(description="S-1, S-1/A, F-1, F-1/A, EFFECT, 424B4 or RW")
    stage: Literal["filed", "amended", "effective", "priced", "withdrawn"]
    filing_date: Date
    accepted_at: UtcDatetime = _unit("UTC", description="EDGAR acceptance")
    accession_number: str
    file_number: str | None = Field(default=None, description="The registration's 333- number")
    registration_form: str | None = Field(default=None, description="EFFECT / RW: the form it concerns")
    ticker: str | None = Field(default=None, description="Proposed, from the prospectus cover")
    exchange: str | None = Field(default=None, description="As the cover names it")
    price_low: float | None = _unit("USD_per_share", default=None, description="Per share, ADS or unit")
    price_high: float | None = _unit("USD_per_share", default=None, description="Per share, ADS or unit")
    offer_price: float | None = _unit("USD_per_share", default=None, description="424B4: the priced offering")
    shares_offered: float | None = Field(default=None, description="In offered_unit")
    offered_unit: Literal["shares", "ADSs", "units"] | None = None
    effective_at: UtcDatetime | None = _unit(
        "UTC", default=None, description="EFFECT: when it became effective"
    )
    category: Literal["operating", "spac", "trust"]
    sic: str | None = None
    primary_doc_url: str | None = None
    knowledge_time: UtcDatetime = _known()


class TaiwanMonthlyRevenue(CanonicalModel):
    """One month's revenue of a company listed on TWSE (sii) or TPEx (otc), as
    it filed it by the 10th of the next month (MOPS, the market's filing
    system; TWSE's OpenAPI): the month, the month before, the same month a year
    before and the year to date, New Taiwan dollars in thousands as published.
    note is the company's own explanation: UNTRUSTED text."""

    schema_name: ClassVar[str] = "marketlens.TaiwanMonthlyRevenue"
    time_column: ClassVar[str | None] = "month"
    group_column: ClassVar[str | None] = "company_code"
    value_columns: ClassVar[tuple[str, ...]] = ("revenue",)

    company_code: str = Field(description="TWSE / TPEx code, e.g. 2330")
    adr: str | None = Field(
        default=None, description="The US ADR ticker marketlens-data maps to the code (TSM)"
    )
    company_name: str = Field(description="As the filing writes it, e.g. 台積電")
    market: Literal["sii", "otc"] = Field(description="sii: listed on TWSE; otc: on TPEx")
    industry: str | None = Field(default=None, description="The exchange's industry (產業別)")
    month: Date = Field(description="First day of the month the revenue is for")
    unit: str = Field(description="TWD_thousands: thousands of New Taiwan dollars, as published")
    revenue: float | None = _unit("as_published", default=None, description="The month's revenue, in `unit`")
    revenue_prev_month: float | None = _unit("as_published", default=None, description="The month before")
    revenue_year_ago: float | None = _unit(
        "as_published", default=None, description="The month a year before"
    )
    revenue_ytd: float | None = _unit("as_published", default=None, description="January to the month")
    revenue_ytd_year_ago: float | None = _unit(
        "as_published", default=None, description="January to the month, a year before"
    )
    mom_change: float | None = _unit("fraction", default=None, description="As the filing states it")
    yoy_change: float | None = _unit("fraction", default=None, description="As the filing states it")
    ytd_yoy_change: float | None = _unit("fraction", default=None, description="As the filing states it")
    note: str | None = Field(default=None, description="The company's note (備註); untrusted text")
    publication: Literal["mops_summary", "twse_openapi"]
    stamp_estimated: bool = Field(
        description="knowledge_time is the statutory deadline (the first TWSE trading day from the 10th of the "
        "next month, 23:59:59 Taipei), not a date read"
    )
    knowledge_time: UtcDatetime = _known()


class XbrlFact(CanonicalModel):
    """One XBRL fact from EDGAR company facts, as filed."""

    schema_name: ClassVar[str] = "marketlens.XbrlFact"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "metric"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    taxonomy: str = Field(description="us-gaap, dei, ifrs-full, srt, ...")
    metric: str = Field(description="The XBRL concept, verbatim (Revenues, NetIncomeLoss, ...)")
    unit: str = Field(description="The fact's unit: USD, USD/shares, shares, pure, ...")
    period_start: Date | None = None
    period_end: Date
    value: float | None = _unit("as_published", default=None, description="In the row's `unit`")
    accession_number: str
    form: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = Field(default=None, description="Q1..Q4 or FY")
    knowledge_time: UtcDatetime = _known()


class XbrlFrameValue(CanonicalModel):
    """One filer's value of one XBRL concept in one calendar period, from the
    SEC's frames API: the fact the SEC finds last filed for the period, so a
    restated value is the restating filing's."""

    schema_name: ClassVar[str] = "marketlens.XbrlFrameValue"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "cik"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    taxonomy: str = Field(description="us-gaap, dei, ifrs-full, srt")
    tag: str = Field(description="The XBRL concept, verbatim (Revenues, Assets, ...)")
    unit: str = Field(description="As the frames API spells it: USD, USD-per-shares, shares, pure")
    period: str = Field(description="CY2025 (a year), CY2025Q2 (a quarter), CY2025Q2I (an instant)")
    cik: str = Field(description="10-digit, zero-padded")
    ticker: str | None = Field(default=None, description="The SEC company list's first ticker for the CIK")
    entity_name: str
    period_start: Date | None = Field(default=None, description="The fact's own start; None for an instant")
    period_end: Date = Field(
        description="The fact's own end (a fiscal quarter is placed in the nearest calendar one)"
    )
    value: float = _unit("as_published", description="In the row's `unit`")
    accession_number: str = Field(description="The filing that reported the value")
    filing_date: Date | None = Field(
        default=None, description="From EDGAR's quarterly XBRL index, when knowledge_time came from it"
    )
    stamp: Literal["acceptance", "filing_date"] = Field(
        description="How knowledge_time was set: EDGAR's acceptance instant, or the end of the filing date in New York"
    )
    knowledge_time: UtcDatetime = _known()


class PayVsPerformance(CanonicalModel):
    """One measure of one fiscal year of a company's pay-versus-performance
    table (Reg. S-K Item 402(v)), as one proxy tagged it in XBRL (ecd). Every
    proxy restates the earlier years; the read is the latest proxy at as_of.
    executive_name and measure_name are the filer's text: UNTRUSTED."""

    schema_name: ClassVar[str] = "marketlens.PayVsPerformance"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "measure"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    fiscal_year: int = Field(
        description="The calendar year the fiscal year ends in (a year closing in the first week of January is the year before)"
    )
    measure: Literal[
        "peo_total_comp",
        "peo_comp_actually_paid",
        "non_peo_neo_avg_total_comp",
        "non_peo_neo_avg_comp_actually_paid",
        "company_tsr",
        "peer_group_tsr",
        "net_income",
        "company_selected_measure",
    ]
    executive: str | None = Field(
        default=None,
        description="The filer's member when a PEO figure is tagged per person (two PEOs in a year)",
    )
    executive_name: str | None = Field(default=None, description="The filer's own text for the PEO")
    concept: str = Field(description="ecd:PeoTotalCompAmt, us-gaap:NetIncomeLoss, ...")
    value: float = _unit(
        "as_published", description="In the row's `unit`: USD in full; a TSR is the value of $100 invested"
    )
    unit: str = Field(description="USD, USD/shares, pure, ... as tagged")
    measure_name: str | None = Field(default=None, description="The company-selected measure's name")
    period_start: Date | None = None
    period_end: Date
    accession_number: str = Field(description="The proxy (DEF 14A or DEFR14A)")
    form: str
    filing_date: Date
    knowledge_time: UtcDatetime = _known()


class FundamentalValue(CanonicalModel):
    """One point-in-time fundamentals figure computed from the stored XBRL facts."""

    schema_name: ClassVar[str] = "marketlens.FundamentalValue"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "metric"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    metric: str = Field(description="revenue, net_income, eps_diluted, gross_margin, ... (23 metrics)")
    unit: str = Field(description="USD, USD/shares, shares or fraction (margins)")
    basis: Literal["ttm", "instant", "period"]
    period_start: Date | None = None
    period_end: Date
    fiscal_label: str | None = Field(default=None, description="e.g. 2026-Q3 or FY2025")
    value: float | None = _unit("as_published", default=None, description="In the row's `unit`")
    derived: bool = Field(description="Arithmetic on filed facts (TTM roll-forward, margins, EBITDA)")
    reason: str | None = Field(default=None, description="Why value is None (marketlens-data's code)")
    note: str | None = Field(default=None, description="How the value was computed, or why it is absent")
    concepts: list[str] = Field(default_factory=list, description="XBRL concepts used")
    accession_numbers: list[str] = Field(default_factory=list, description="Filings the inputs came from")
    knowledge_time: UtcDatetime = _known()


class EarningsRelease(CanonicalModel):
    """One 8-K or 8-K/A carrying Item 2.02 (results of operations)."""

    schema_name: ClassVar[str] = "marketlens.EarningsRelease"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "ticker"

    ticker: str
    cik: str
    company_name: str | None = None
    form: str
    accession_number: str
    filing_date: Date
    report_date: Date | None = None
    accepted_at: UtcDatetime = _unit("UTC", description="EDGAR acceptance instant")
    session: Literal["pre_open", "intraday", "after_close"] = Field(
        description="New York session of the acceptance: before 09:30, to 16:00, from 16:00"
    )
    items: list[str] = Field(default_factory=list)
    primary_doc_url: str | None = None
    knowledge_time: UtcDatetime = _known()


class EarningsFigure(CanonicalModel):
    """One EPS figure read from a table of an earnings press release (EX-99.1).
    ``source_text`` is UNTRUSTED company text."""

    schema_name: ClassVar[str] = "marketlens.EarningsFigure"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    accession_number: str
    accepted_at: UtcDatetime = _unit("UTC", description="The 8-K's EDGAR acceptance")
    exhibit: str = Field(description="EX-99.1, EX-99, ...")
    exhibit_url: str
    metric: Literal["eps_diluted_gaap", "eps_adjusted"]
    fiscal_period_label: str | None = Field(default=None, description="The table's column header, as stated")
    value: float = _unit("USD_per_share")
    unit: str = Field(description="As the parser states it: USD/share")
    basis: str = Field(description="gaap or non_gaap")
    source_text: str = Field(description="The table row the figure came from (untrusted text)")
    parse_rule: str
    knowledge_time: UtcDatetime = _known()


class EarningsGuidance(CanonicalModel):
    """One guidance statement of an earnings press release (EX-99.1): the
    company's own outlook for one metric over one period, from its prose or its
    guidance table by a named rule; ``source_text`` is UNTRUSTED company text.
    A margin, a tax rate or a growth rate is a fraction (56 % is 0.56; a
    growth rate's decline is negative)."""

    schema_name: ClassVar[str] = "marketlens.EarningsGuidance"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "metric"
    value_columns: ClassVar[tuple[str, ...]] = ("low", "high", "mid")

    ticker: str
    cik: str
    accession_number: str
    accepted_at: UtcDatetime = _unit("UTC", description="The 8-K's (or 6-K's) EDGAR acceptance")
    exhibit: str = Field(description="EX-99.1, EX-99, ...")
    exhibit_url: str
    metric: str = Field(
        description="revenue, gross_margin, operating_margin, operating_income, opex, tax_rate, capex, eps, "
        "revenue_growth, organic_revenue_growth, eps_growth"
    )
    period_kind: str = Field(description="next_quarter or fiscal_year")
    period_label: str = Field(description="As stated: third quarter of 2026, full year 2026")
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    low: float = _unit("as_published", description="In the row's `unit`")
    high: float = _unit("as_published", description="In the row's `unit`")
    mid: float = _unit("as_published", description="(low + high) / 2; a point estimate has low = high = mid")
    unit: str = Field(description="USD (full dollars), fraction (a margin, a rate or a growth rate) or USD/share")
    basis: str = Field(description="gaap or non_gaap, the sentence's own word")
    source_text: str = Field(description="The sentence or table row (untrusted text)")
    parse_rule: str
    knowledge_time: UtcDatetime = _known()


class NonGaapFigure(CanonicalModel):
    """One line of an earnings press release's GAAP to non-GAAP reconciliation
    table: a GAAP or non-GAAP figure, or a bridge line between them, in the
    column of the release's quarter. ``source_text`` is UNTRUSTED company text."""

    schema_name: ClassVar[str] = "marketlens.NonGaapFigure"
    time_column: ClassVar[str | None] = "accepted_at"
    group_column: ClassVar[str | None] = "metric"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    accession_number: str
    accepted_at: UtcDatetime = _unit("UTC", description="The 8-K's EDGAR acceptance")
    exhibit: str
    exhibit_url: str
    metric: str = Field(
        description="gross_profit, gross_margin, operating_expenses, operating_income, operating_margin, "
        "net_income, eps_diluted, stock_based_compensation, amortization_acquisition_intangibles, restructuring"
    )
    basis: str = Field(description="gaap, non_gaap or adjustment (a bridge line)")
    reconciles: str = Field(description="The GAAP line this row belongs to (a bridge line's target)")
    fiscal_period_label: str | None = Field(default=None, description="The table column's header, as stated")
    value: float = _unit("as_published", description="In the row's `unit`")
    unit: str = Field(description="USD (full dollars), fraction (a margin) or USD/share")
    source_text: str = Field(description="The table row (untrusted text)")
    parse_rule: str
    knowledge_time: UtcDatetime = _known()


class SegmentFact(CanonicalModel):
    """One dimensional XBRL fact of a 10-K or 10-Q: a concept's value for one
    business segment, product, region or revenue-disaggregation line, as the
    filing tags it (the companyfacts API drops every dimension)."""

    schema_name: ClassVar[str] = "marketlens.SegmentFact"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "member_key"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    accession_number: str
    form: str
    filed: Date
    fiscal_year: int | None = Field(default=None, description="The filing's fiscal year focus")
    fiscal_period: str | None = Field(
        default=None, description="The filing's fiscal period focus: Q1..Q3 or FY"
    )
    concept: str = Field(description="The XBRL concept, verbatim")
    axis: str | None = Field(default=None, description="The segmenting axes, ';'-joined")
    axis_kind: str | None = Field(default=None, description="segment, geography, disaggregation or other")
    member: str | None = Field(default=None, description="The members' labels as filed")
    member_key: str | None = Field(default=None, description="The label reduced to letters and digits")
    member_label: str | None = Field(default=None, description="The filer's own name for the member")
    consolidation: str | None = Field(
        default=None, description="Operating Segments, Intersegment Eliminations, ..."
    )
    period_start: Date | None = Field(default=None, description="None for an instant")
    period_end: Date = Field(description="The period end, rounded to the nearest month end")
    quarters: int = Field(description="The duration in whole quarters; 0 for an instant")
    value: float | None = _unit("as_published", default=None, description="In the row's `unit`")
    unit: str = Field(description="USD, shares, ...")
    source: str = Field(description="The read it came from: a data set (2026_08_notes) or edgar:<accession>")
    knowledge_time: UtcDatetime = _known()


class ForeignQuarterlyFigure(CanonicalModel):
    """One figure of a foreign private issuer's results 6-K exhibits (press
    release table, slides, management report, interim statements' revenue
    note), in the issuer's unit at its scale as stated."""

    schema_name: ClassVar[str] = "marketlens.ForeignQuarterlyFigure"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "line"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    ticker: str
    cik: str
    form: str
    accession_number: str
    exhibit: str
    exhibit_url: str
    statement: str = Field(description="income, balance, cash_flow, segments or kpi")
    line: str = Field(description="The KPI id (revenue, operating_income, ...); revenue_share for a segment")
    member: str | None = Field(default=None, description="The segment or node as stated (HPC, 3nm)")
    axis: str | None = Field(default=None, description="platform, technology, ... for a segment")
    value: float = _unit("as_published", description="In `unit` at 10**scale; a share is a fraction")
    unit: str = Field(description="TWD, USD, TWD/share, fraction, wafers_12in_equiv")
    scale: int = Field(description="The power of ten the value is scaled by: 6 millions, 9 billions")
    period_label: str = Field(description="The column's label as stated: 3Q26, 2025")
    period_start: Date | None = None
    period_end: Date
    fiscal_year: int
    fiscal_period: str = Field(description="Q1..Q4 or FY")
    source_text: str = Field(description="The row's label as stated (untrusted text)")
    parse_rule: str
    knowledge_time: UtcDatetime = _known()


class IrDocument(CanonicalModel):
    """One company-published document the operator lists for a ticker (an
    investor-day release, an outlook, a letter): its text (UNTRUSTED) and sha,
    served from EDGAR (authority official) when the company also filed it."""

    schema_name: ClassVar[str] = "marketlens.IrDocument"
    time_column: ClassVar[str | None] = "published"
    group_column: ClassVar[str | None] = "ticker"

    ticker: str
    cik: str | None = None
    document_url: str
    published: Date
    kind: str = Field(description="investor_day, outlook, letter or presentation")
    title: str
    authority: Literal["official", "company"] = Field(
        description="official when the EDGAR copy is served, else company (the company's own site)"
    )
    served_url: str
    edgar_accession: str | None = None
    edgar_form: str | None = None
    content_type: str = Field(description="html, pdf or text, of the served copy")
    sha256: str
    text: str = Field(description="The served copy's text, capped (untrusted text)")
    text_chars: int = _unit("count", description="The text's length before the cap")
    truncated: bool
    knowledge_time: UtcDatetime = _known()


class IrTarget(CanonicalModel):
    """One multi-year target a company document states (a revenue CAGR, a
    margin, EPS, capex, free cash flow), by a named sentence rule, never from a
    table. A margin or CAGR is a fraction (35 % is 0.35); ``source_text`` is
    UNTRUSTED company text."""

    schema_name: ClassVar[str] = "marketlens.IrTarget"
    time_column: ClassVar[str | None] = "published"
    group_column: ClassVar[str | None] = "metric"
    value_columns: ClassVar[tuple[str, ...]] = ("low", "high", "mid")

    ticker: str
    document_url: str
    published: Date
    title: str
    kind: str
    authority: Literal["official", "company"]
    served_url: str
    edgar_accession: str | None = None
    metric: str = Field(
        description="revenue_cagr, gross_margin, operating_margin, eps, capex, free_cash_flow"
    )
    horizon: str = Field(description="As stated: the next three to five years, 2024-2029, long-term")
    horizon_start: int | None = None
    horizon_end: int | None = None
    low: float | None = _unit("as_published", default=None, description="A floor has a low and no high")
    high: float | None = _unit("as_published", default=None, description="A ceiling has a high and no low")
    mid: float | None = _unit(
        "as_published", default=None, description="(low + high) / 2 when both are stated"
    )
    unit: str = Field(description="fraction, USD (full dollars) or USD/share")
    basis: str = Field(description="gaap, non_gaap or unstated, the sentence's own word")
    source_text: str = Field(description="The sentence (untrusted text)")
    parse_rule: str
    knowledge_time: UtcDatetime = _known()


class InsiderTransaction(CanonicalModel):
    """One transaction of a Form 4, or one line of a Form 144 notice of an intended sale: a
    line per securitiesInformation block the notice lists, each a class of security and a
    broker (a broker can recur)."""

    schema_name: ClassVar[str] = "marketlens.InsiderTransaction"
    time_column: ClassVar[str | None] = "transaction_date"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("value_usd",)

    ticker: str
    cik: str = Field(description="Issuer CIK, 10-digit")
    accession_number: str
    line: int = _unit("ordinal", description="Position of the transaction in its filing (1 = first)")
    form: str = Field(description="4, 4/A, 144 or 144/A")
    filing_date: Date
    insider_name: str
    insider_cik: str | None = None
    officer_title: str | None = None
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    has_10b5_1: bool = Field(
        description="Under a pre-arranged 10b5-1 plan (a Form 144: it gives the plan's adoption date)"
    )
    is_derivative: bool = Field(description="Options/RSU table (True) or common stock table (False)")
    security_title: str | None = None
    transaction_date: Date = Field(description="Executed (Form 4) or intended sale date (Form 144)")
    transaction_code: str = Field(
        description="P buy, S sell, A grant, M exercise, F tax, G gift, 144 announced sale"
    )
    acquired_disposed: str | None = Field(default=None, description="A or D")
    shares: float | None = _unit("shares", default=None)
    price_per_share: float | None = _unit(
        "price", default=None, description="USD; none on a Form 144, which states no price"
    )
    value_usd: float | None = _unit(
        "USD", default=None, description="shares x price, or a Form 144 line's aggregate market value"
    )
    shares_owned_after: float | None = _unit("shares", default=None)
    knowledge_time: UtcDatetime = _known()


class InstitutionalHolding(CanonicalModel):
    """One position of a 13F-HR information table."""

    schema_name: ClassVar[str] = "marketlens.InstitutionalHolding"
    time_column: ClassVar[str | None] = "period_end"
    group_column: ClassVar[str | None] = "cusip"
    value_columns: ClassVar[tuple[str, ...]] = ("value_usd",)

    filer_cik: str
    filer_name: str | None = None
    accession_number: str
    line: int = _unit("ordinal", description="Position in the information table (1 = first)")
    form: str = Field(description="13F-HR or 13F-HR/A")
    period_end: Date
    filing_date: Date
    is_amendment: bool
    amendment_type: str | None = Field(default=None, description="RESTATEMENT or NEW HOLDINGS")
    issuer_name: str
    cusip: str
    title_of_class: str | None = None
    value_usd: float = _unit("USD")
    shares: float | None = _unit(
        "as_published", default=None, description="Shares, or principal in USD when share_type is PRN"
    )
    share_type: str = Field(description="SH (shares) or PRN (principal)")
    put_call: str | None = None
    investment_discretion: str | None = None
    sole_voting: float | None = _unit("shares", default=None)
    shared_voting: float | None = _unit("shares", default=None)
    no_voting: float | None = _unit("shares", default=None)
    knowledge_time: UtcDatetime = _known()


class FundReport(CanonicalModel):
    """One Form N-PORT header of a fund series (net assets span every share class)."""

    schema_name: ClassVar[str] = "marketlens.FundReport"
    time_column: ClassVar[str | None] = "report_date"
    group_column: ClassVar[str | None] = "series_id"
    value_columns: ClassVar[tuple[str, ...]] = ("net_assets",)

    ticker: str = Field(description="The fund or ETF symbol requested")
    cik: str
    series_id: str
    series_name: str | None = None
    class_ids: list[str] = Field(default_factory=list)
    accession_number: str
    form: str
    report_date: Date
    report_period_end: Date | None = Field(default=None, description="The fiscal year end")
    filing_date: Date
    total_assets: float | None = _unit("USD", default=None)
    total_liabilities: float | None = _unit("USD", default=None)
    net_assets: float | None = _unit("USD", default=None)
    holdings_count: int = _unit("count")
    is_final: bool | None = None
    primary_doc_url: str | None = None
    knowledge_time: UtcDatetime = _known()


class FundHolding(CanonicalModel):
    """One holding line of a Form N-PORT filing."""

    schema_name: ClassVar[str] = "marketlens.FundHolding"
    time_column: ClassVar[str | None] = "report_date"
    group_column: ClassVar[str | None] = "series_id"
    value_columns: ClassVar[tuple[str, ...]] = ("value_usd",)

    cik: str
    series_id: str
    accession_number: str
    report_date: Date
    line: int = _unit("ordinal")
    name: str | None = None
    lei: str | None = None
    title: str | None = None
    cusip: str | None = None
    isin: str | None = None
    ticker: str | None = Field(default=None, description="Identifier as filed (can be a futures code)")
    balance: float | None = _unit("as_published", default=None, description="In the row's `units`")
    units: str | None = Field(default=None, description="NS shares, NC contracts, PA principal, ...")
    currency: str | None = None
    value_usd: float | None = _unit("USD", default=None)
    pct_value: float | None = _unit("fraction", default=None, description="Share of the series' net assets")
    payoff_profile: str | None = None
    asset_category: str | None = None
    issuer_category: str | None = None
    country: str | None = None
    knowledge_time: UtcDatetime = _known()


# --- the Federal Reserve and the Treasury --------------------------------------------------


class FomcMeeting(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.FomcMeeting"
    time_column: ClassVar[str | None] = "start_date"

    start_date: Date
    end_date: Date
    year: int
    has_projection: bool = Field(description="A Summary of Economic Projections meeting")
    knowledge_time: UtcDatetime = _known()


class FomcStatement(CanonicalModel):
    """A post-meeting FOMC statement. ``text`` is UNTRUSTED third-party text."""

    schema_name: ClassVar[str] = "marketlens.FomcStatement"
    time_column: ClassVar[str | None] = "meeting_date"

    meeting_date: Date
    url: str
    text: str | None = None
    n_paragraphs: int = _unit("count")
    knowledge_time: UtcDatetime = _known()


class ReferenceRate(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.ReferenceRate"
    time_column: ClassVar[str | None] = "date"
    group_column: ClassVar[str | None] = "rate_type"
    value_columns: ClassVar[tuple[str, ...]] = ("rate",)

    rate_type: Literal["SOFR", "EFFR", "OBFR", "TGCR", "BGCR"]
    date: Date = Field(description="Effective date")
    rate: float | None = _unit("fraction_per_year", default=None)
    volume_usd: float | None = _unit("USD", default=None)
    target_from: float | None = _unit("fraction_per_year", default=None, description="FOMC target range")
    target_to: float | None = _unit("fraction_per_year", default=None)
    knowledge_time: UtcDatetime = _known()


class YieldCurvePoint(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.YieldCurvePoint"
    time_column: ClassVar[str | None] = "date"
    group_column: ClassVar[str | None] = "tenor"
    value_columns: ClassVar[tuple[str, ...]] = ("rate",)

    date: Date
    tenor: str = Field(description="1M ... 30Y, and 1.5M")
    tenor_label: str | None = Field(default=None, description="1 Mo ... 30 Yr")
    rate: float | None = _unit("fraction_per_year", default=None, description="Par yield")
    knowledge_time: UtcDatetime = _known()


class TreasuryAuction(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.TreasuryAuction"
    time_column: ClassVar[str | None] = "auction_date"
    group_column: ClassVar[str | None] = "security_term"
    value_columns: ClassVar[tuple[str, ...]] = ("high_yield",)

    cusip: str
    auction_date: Date
    security_type: str
    security_term: str
    issue_date: Date | None = None
    maturity_date: Date | None = None
    offering_amount: float | None = _unit("USD", default=None)
    total_accepted: float | None = _unit("USD", default=None)
    bid_to_cover: float | None = _unit("times", default=None)
    high_yield: float | None = _unit("fraction_per_year", default=None)
    high_investment_rate: float | None = _unit("fraction_per_year", default=None)
    high_discount_rate: float | None = _unit("fraction_per_year", default=None)
    interest_rate: float | None = _unit("fraction_per_year", default=None)
    price_per_100: float | None = _unit("percent_of_par", default=None)
    knowledge_time: UtcDatetime = _known()


class TreasuryDebt(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.TreasuryDebt"
    time_column: ClassVar[str | None] = "date"
    value_columns: ClassVar[tuple[str, ...]] = ("total_debt",)

    date: Date
    total_debt: float | None = _unit("USD", default=None)
    debt_held_public: float | None = _unit("USD", default=None)
    intragovernmental: float | None = _unit("USD", default=None)
    knowledge_time: UtcDatetime = _known()


class TreasuryCashBalance(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.TreasuryCashBalance"
    time_column: ClassVar[str | None] = "date"
    group_column: ClassVar[str | None] = "account_type"
    value_columns: ClassVar[tuple[str, ...]] = ("closing_balance",)

    date: Date
    account_type: str
    closing_balance: float | None = _unit("USD", default=None)
    knowledge_time: UtcDatetime = _known()


class FomcProjection(CanonicalModel):
    """One cell of the FOMC's Summary of Economic Projections. ``kind`` summary:
    Table 1 (median, central tendency and range of the participants'
    projections; ``midpoint`` and ``participants`` not applicable). ``kind``
    dots: Figure 2, the dot plot (how many participants put the appropriate
    federal funds rate for ``horizon`` at ``midpoint``; the summary fields not
    applicable)."""

    schema_name: ClassVar[str] = "marketlens.FomcProjection"
    time_column: ClassVar[str | None] = "meeting_date"
    group_column: ClassVar[str | None] = "variable"
    value_columns: ClassVar[tuple[str, ...]] = ("median",)

    meeting_date: Date = Field(description="The meeting's last day")
    release_date: Date
    kind: Literal["summary", "dots"]
    variable: Literal[
        "gdp_growth", "unemployment_rate", "pce_inflation", "core_pce_inflation", "fed_funds_rate"
    ]
    horizon: str = Field(description="A year (2026) or longer_run")
    horizon_year: int | None = Field(default=None, description="None for longer_run")
    median: float | None = _unit("fraction", default=None)
    central_tendency_low: float | None = _unit("fraction", default=None)
    central_tendency_high: float | None = _unit("fraction", default=None)
    range_low: float | None = _unit("fraction", default=None)
    range_high: float | None = _unit("fraction", default=None)
    midpoint: float | None = _unit(
        "fraction_per_year", default=None, description="A dot's federal funds rate"
    )
    participants: int | None = _unit("count", default=None)
    url: str
    knowledge_time: UtcDatetime = _known()


class FomcMinutes(CanonicalModel):
    """The minutes of an FOMC meeting. ``text`` is UNTRUSTED third-party text."""

    schema_name: ClassVar[str] = "marketlens.FomcMinutes"
    time_column: ClassVar[str | None] = "meeting_date"

    meeting_date: Date = Field(description="The meeting's last day")
    release_date: Date = Field(description="The release date the Fed prints beside the link")
    url: str
    text: str | None = None
    n_paragraphs: int = _unit("count")
    knowledge_time: UtcDatetime = _known()


class BeigeBookSection(CanonicalModel):
    """One district's report (or the national summary) of a Beige Book edition.
    ``text`` is UNTRUSTED third-party text."""

    schema_name: ClassVar[str] = "marketlens.BeigeBookSection"
    time_column: ClassVar[str | None] = "release_date"
    group_column: ClassVar[str | None] = "district"

    release_date: Date
    edition: str = Field(description="The page's own stamp (202610); not the release month")
    district: str = Field(description="national_summary or a Reserve Bank (boston ... san_francisco)")
    url: str
    text: str | None = None
    n_paragraphs: int = _unit("count")
    knowledge_time: UtcDatetime = _known()


# --- banks and money market funds -----------------------------------------------------------


class BankFinancials(CanonicalModel):
    """One insured bank's Call Report quarter, from the FDIC's BankFind."""

    schema_name: ClassVar[str] = "marketlens.BankFinancials"
    time_column: ClassVar[str | None] = "report_date"
    group_column: ClassVar[str | None] = "name"
    value_columns: ClassVar[tuple[str, ...]] = ("total_assets", "total_deposits", "net_income_ytd")

    cert: int = Field(description="FDIC certificate number")
    report_date: Date = Field(description="The quarter's end")
    ticker: str | None = Field(default=None, description="The listed holder the bank was mapped from")
    name: str
    holding_company: str | None = Field(default=None, description="The FDIC's regulatory top holder")
    total_assets: float | None = _unit("USD", default=None)
    total_deposits: float | None = _unit("USD", default=None)
    domestic_deposits: float | None = _unit("USD", default=None)
    insured_deposits_est: float | None = _unit("USD", default=None)
    uninsured_deposits_est: float | None = _unit("USD", default=None)
    gross_loans: float | None = _unit("USD", default=None)
    net_loans: float | None = _unit("USD", default=None)
    total_equity: float | None = _unit("USD", default=None)
    net_income_ytd: float | None = _unit("USD", default=None, description="Year to date")
    net_interest_income_ytd: float | None = _unit("USD", default=None)
    net_charge_offs_ytd: float | None = _unit("USD", default=None)
    noncurrent_loans: float | None = _unit("USD", default=None)
    htm_securities_amortized_cost: float | None = _unit("USD", default=None)
    htm_securities_fair_value: float | None = _unit("USD", default=None)
    afs_securities_amortized_cost: float | None = _unit("USD", default=None)
    afs_securities_fair_value: float | None = _unit("USD", default=None)
    htm_unrealized_gain: float | None = _unit(
        "USD", default=None, description="Fair value less cost; < 0 a loss"
    )
    afs_unrealized_gain: float | None = _unit(
        "USD", default=None, description="Fair value less cost; < 0 a loss"
    )
    net_interest_margin: float | None = _unit("fraction_per_year", default=None)
    roa: float | None = _unit("fraction_per_year", default=None)
    roe: float | None = _unit("fraction_per_year", default=None)
    leverage_ratio: float | None = _unit("fraction", default=None)
    tier1_capital_ratio: float | None = _unit("fraction", default=None)
    cet1_ratio: float | None = _unit("fraction", default=None)
    total_capital_ratio: float | None = _unit("fraction", default=None)
    noncurrent_loans_ratio: float | None = _unit("fraction", default=None)
    net_charge_off_ratio: float | None = _unit("fraction_per_year", default=None)
    knowledge_time: UtcDatetime = _known()


class BankIndustrySummary(CanonicalModel):
    """The FDIC's annual aggregates of insured institutions for one year-end,
    location and charter class."""

    schema_name: ClassVar[str] = "marketlens.BankIndustrySummary"
    time_column: ClassVar[str | None] = "year_end"
    group_column: ClassVar[str | None] = "charter_class"
    value_columns: ClassVar[tuple[str, ...]] = ("total_assets", "net_income")

    year_end: Date
    location: str = Field(description="A state or territory, or All States and Territories")
    charter_class: Literal["CB", "SI"] = Field(description="Commercial banks or savings institutions")
    institutions: int | None = _unit("count", default=None)
    offices: int | None = _unit("count", default=None)
    branches: int | None = _unit("count", default=None)
    total_assets: float | None = _unit("USD", default=None)
    total_deposits: float | None = _unit("USD", default=None)
    gross_loans: float | None = _unit("USD", default=None)
    net_loans: float | None = _unit("USD", default=None)
    total_equity: float | None = _unit("USD", default=None)
    net_income: float | None = _unit("USD", default=None)
    net_interest_income: float | None = _unit("USD", default=None)
    noncurrent_loans: float | None = _unit("USD", default=None)
    net_charge_offs: float | None = _unit("USD", default=None)
    knowledge_time: UtcDatetime = _known()


class MoneyMarketFundStatistic(CanonicalModel):
    """One value of the SEC's monthly Money Market Fund Statistics (Form N-MFP
    aggregates)."""

    schema_name: ClassVar[str] = "marketlens.MoneyMarketFundStatistic"
    time_column: ClassVar[str | None] = "month"
    group_column: ClassVar[str | None] = "item"
    value_columns: ClassVar[tuple[str, ...]] = ("value",)

    table: str = Field(description="The workbook's table: 2.1 net assets, 7.1 holdings, 8.2 Fed repo, ...")
    table_title: str
    category: str = Field(description="Government, Prime, Tax Exempt, Total; or an investment group")
    item: str = Field(description="A fund type or investment category; Subtotal, Total")
    month: Date = Field(description="The reporting month (its first day)")
    value: float | None = _unit("as_published", default=None, description="In the row's `unit`")
    unit: Literal["USD", "fraction", "days"]
    file: str = Field(description="The workbook that first stated the value")
    knowledge_time: UtcDatetime = _known()


# --- calendars ------------------------------------------------------------------------------


class EconomicEvent(CanonicalModel):
    """One row of the Nasdaq economic calendar (vendor data; unofficial endpoint).
    ``description`` is UNTRUSTED third-party text."""

    schema_name: ClassVar[str] = "marketlens.EconomicEvent"
    time_column: ClassVar[str | None] = "release_at"
    group_column: ClassVar[str | None] = "event_name"
    value_columns: ClassVar[tuple[str, ...]] = ("actual",)

    event_date: Date = Field(description="Eastern date of the event")
    release_at: UtcDatetime | None = _unit("UTC", default=None)
    time_et: str | None = Field(default=None, description="HH:MM Eastern")
    all_day: bool
    country: str | None = None
    event_name: str
    actual: float | None = _unit(
        "as_published", default=None, description="Percent stays percent (value_kind)"
    )
    consensus: float | None = _unit("as_published", default=None)
    previous: float | None = _unit("as_published", default=None)
    actual_text: str | None = None
    consensus_text: str | None = None
    previous_text: str | None = None
    value_kind: Literal["pct", "num", "none"]
    description: str | None = None
    knowledge_time: UtcDatetime = _known()


class EarningsCalendarEntry(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.EarningsCalendarEntry"
    time_column: ClassVar[str | None] = "report_date"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("eps_actual",)

    report_date: Date
    ticker: str
    name: str | None = None
    time_of_day: str | None = Field(default=None, description="pre_market, after_hours or not_supplied")
    market_cap: float | None = _unit("USD", default=None)
    fiscal_quarter: str | None = Field(default=None, description="YYYY-MM of the quarter's end")
    eps_forecast: float | None = _unit("USD_per_share", default=None)
    eps_actual: float | None = _unit("USD_per_share", default=None)
    last_year_eps: float | None = _unit("USD_per_share", default=None)
    estimates: int | None = _unit("count", default=None)
    surprise_pct: float | None = _unit("fraction", default=None)
    last_year_report_date: Date | None = None
    status: str | None = Field(default=None, description="reported or upcoming")
    knowledge_time: UtcDatetime = _known()


class EarningsSurprise(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.EarningsSurprise"
    time_column: ClassVar[str | None] = "report_date"
    group_column: ClassVar[str | None] = "ticker"
    value_columns: ClassVar[tuple[str, ...]] = ("eps_actual",)

    ticker: str
    fiscal_quarter: str = Field(description="YYYY-MM of the quarter's end")
    report_date: Date | None = None
    eps_actual: float | None = _unit("USD_per_share", default=None)
    eps_forecast: float | None = _unit("USD_per_share", default=None)
    surprise_pct: float | None = _unit("fraction", default=None)
    knowledge_time: UtcDatetime = _known()


class MarketHoliday(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.MarketHoliday"
    time_column: ClassVar[str | None] = "holiday_date"
    group_column: ClassVar[str | None] = "market"

    publisher: Literal["nyse", "sifma", "opm"]
    market: Literal["stocks", "bonds", "federal"]
    holiday_date: Date
    name: str
    status: Literal["closed", "early_close"]
    close_time_et: str | None = Field(default=None, description="HH:MM Eastern, early closes only")
    close_at: UtcDatetime | None = _unit("UTC", default=None)
    note: str | None = None
    crosscheck: str | None = Field(
        default=None,
        description="NYSE rows: agrees, disagrees, alpaca_only, not_covered or not_checked (Alpaca's calendar)",
    )
    crosscheck_note: str | None = None
    knowledge_time: UtcDatetime = _known()


DATA_MODELS: tuple[type[CanonicalModel], ...] = (
    EconomicObservation,
    EconomicSeriesInfo,
    ReleaseScheduleEntry,
    TradeFlow,
    CentralBankObservation,
    RegionalSurveyIndex,
    TraceAggregate,
    Filing,
    FilingSearchHit,
    FilingEvent,
    TaiwanMonthlyRevenue,
    XbrlFact,
    XbrlFrameValue,
    PayVsPerformance,
    FundamentalValue,
    EarningsRelease,
    EarningsFigure,
    EarningsGuidance,
    NonGaapFigure,
    SegmentFact,
    ForeignQuarterlyFigure,
    IrDocument,
    IrTarget,
    InsiderTransaction,
    DailyIndexEntry,
    Form144Notice,
    IpoFiling,
    InstitutionalHolding,
    FundReport,
    FundHolding,
    FomcMeeting,
    FomcStatement,
    FomcProjection,
    FomcMinutes,
    BeigeBookSection,
    ReferenceRate,
    YieldCurvePoint,
    TreasuryAuction,
    TreasuryDebt,
    TreasuryCashBalance,
    BankFinancials,
    BankIndustrySummary,
    MoneyMarketFundStatistic,
    EconomicEvent,
    EarningsCalendarEntry,
    EarningsSurprise,
    MarketHoliday,
)
