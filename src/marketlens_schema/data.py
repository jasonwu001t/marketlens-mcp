"""Canonical models of the official-data tools (schema 1.1).

Built by the ``data`` provider (the ``marketlens-mcp[data]`` extra, which reads
the publishers through marketlens-data); the models themselves are built in, so
their JSON Schemas exist whether or not the extra is installed.

Conventions of base.py hold, plus:

* ``knowledge_time`` (every model) is when the value became knowable under its
  dataset's point-in-time policy: a vintage or acceptance instant where the
  publisher states one, an approximation that is never early otherwise.
* Rates and percentages are fractions (a 4.33 % yield is 0.0433), money is in
  US dollars (millions and billions multiplied out). A value published in the
  publisher's own unit is ``as_published`` and the row names that unit.
* ``None`` is always explained: ``no_data`` for a publisher's missing value,
  ``not_applicable`` for a field another source or another kind of row fills,
  ``not_provided_by_source`` for a field this source never gives.
* Text written by companies, the Fed or a calendar vendor (press-release
  lines, statements, event descriptions) is UNTRUSTED: the tools that return
  it mark their responses ``external_text``.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any, ClassVar, Literal

from pydantic import Field

from .base import CanonicalModel, UtcDatetime
from .base import unit as _unit

KNOWN = "When the value became knowable under the dataset's point-in-time policy"


def _known() -> Any:
    return _unit("UTC", description=KNOWN)


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
        default=None, description="Publisher's period code: M01..M12, Q1..Q4 or A (None for FRED)"
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


class InsiderTransaction(CanonicalModel):
    """One transaction of a Form 4, or one Form 144 notice of an intended sale."""

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
    has_10b5_1: bool = Field(description="Under a pre-arranged 10b5-1 plan")
    is_derivative: bool = Field(description="Options/RSU table (True) or common stock table (False)")
    security_title: str | None = None
    transaction_date: Date = Field(description="Executed (Form 4) or intended sale date (Form 144)")
    transaction_code: str = Field(
        description="P buy, S sell, A grant, M exercise, F tax, G gift, 144 announced sale"
    )
    acquired_disposed: str | None = Field(default=None, description="A or D")
    shares: float | None = _unit("shares", default=None)
    price_per_share: float | None = _unit("price", default=None, description="USD")
    value_usd: float | None = _unit("USD", default=None, description="shares x price, or the 144 value")
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
    Filing,
    XbrlFact,
    FundamentalValue,
    EarningsRelease,
    EarningsFigure,
    InsiderTransaction,
    InstitutionalHolding,
    FundReport,
    FundHolding,
    FomcMeeting,
    FomcStatement,
    ReferenceRate,
    YieldCurvePoint,
    TreasuryAuction,
    TreasuryDebt,
    TreasuryCashBalance,
    EconomicEvent,
    EarningsCalendarEntry,
    EarningsSurprise,
    MarketHoliday,
)
