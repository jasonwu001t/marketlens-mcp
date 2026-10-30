"""What marketlens-data reads out of the filings, beyond the facts (capability
``filings``): an earnings release's guidance and its GAAP to non-GAAP
reconciliation, a 10-K or 10-Q's segment facts from its own XBRL, a foreign
private issuer's quarters from its results 6-K exhibits, and the company
documents (investor-day releases, outlooks, letters) the operator lists, with
the multi-year targets they state. Every SEC request names the operator's
contact (SEC_CONTACT_EMAIL)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from marketlens_mcp.plugin_api import ToolContext, ToolOutput, ToolSpec
from marketlens_schema import AbsenceCode
from marketlens_schema.data import (
    EarningsGuidance,
    ForeignQuarterlyFigure,
    IrDocument,
    IrTarget,
    NonGaapFigure,
    SegmentFact,
)

from .. import frames as F
from . import common
from .common import AsOf, Inputs, Ticker
from .sec import CONTACT, ENV, TICKER_DOC, _output, _settings, _today

GOLDEN = "tests/data/test_sec_research.py"
NPS = AbsenceCode.NOT_PROVIDED_BY_SOURCE
NA = AbsenceCode.NOT_APPLICABLE
ROUTES = {
    "sec_earnings_guidance": "omni sec.earnings_guidance <- www.sec.gov/Archives (EX-99.1 prose and guidance tables)",
    "sec_earnings_non_gaap": "omni sec.earnings_non_gaap <- www.sec.gov/Archives (EX-99.1 reconciliation tables)",
    "sec_segment_facts": "omni sec.segment_facts <- www.sec.gov/Archives (a 10-K or 10-Q's own XBRL) + "
    "SEC Financial Statement and Notes data sets",
    "sec_foreign_quarterly": "omni sec.foreign_quarterly_statements <- www.sec.gov/Archives (results 6-K exhibits)",
    "company_ir_documents": "omni company.ir_documents <- the company's site (operator's list) + www.sec.gov/Archives",
    "company_ir_targets": "omni company.ir_targets <- company.ir_documents (sentence rules, computed locally)",
}
#: A percentage marketlens-data states in points is served as a fraction.
_PERCENT = "percent"


def _value(value, unit: str | None) -> float | None:
    return F.fraction(value) if unit == _PERCENT else F.number(value)


def _unit(unit: str | None) -> str | None:
    return "fraction" if unit == _PERCENT else unit


def _prov(ctx, tool, *, datasets, request, as_of, rows, session, authority=("official",), provider="sec"):
    return common.provenance(
        ctx,
        provider=provider,
        route=ROUTES[tool],
        datasets=datasets,
        request=request,
        as_of=as_of,
        rows=rows,
        session=session,
        authority=list(authority),
    )


def _window(ctx, args, years: int) -> tuple[date, date]:
    today = _today(ctx)
    return common.window(
        ctx.tool, args.start, args.end, default_start=common.years_ago(today, years), default_end=today
    )


# --- earnings guidance and non-GAAP reconciliation ---------------------------------------------


class GuidanceInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    start: date | None = Field(default=None, description="First acceptance date. Default: 3 years ago.")
    end: date | None = Field(default=None, description="Last acceptance date. Default: today.")
    as_of: AsOf | None = common.as_of_field()


class NonGaapInputs(GuidanceInputs):
    basis: list[Literal["gaap", "non_gaap", "adjustment"]] | None = Field(
        default=None, min_length=1, description="Only these bases. Default: all."
    )


async def sec_earnings_guidance(ctx: ToolContext, args: GuidanceInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    start, end = _window(ctx, args, 3)
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.earnings_guidance", identity)
        return F.records(session.read("sec.earnings_guidance", as_of=as_of, **session.company(args.ticker)))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    rows = []
    for r in records:
        accepted = F.instant(r["event_time"])
        if not F.within(accepted.date(), start, end):
            continue
        unit = F.text(r["unit"])
        rows.append(
            common.row(
                EarningsGuidance,
                {
                    "ticker": args.ticker,
                    "cik": F.text(r["cik"]),
                    "accession_number": F.text(r["accession_number"]),
                    "accepted_at": accepted,
                    "exhibit": F.text(r["exhibit"]),
                    "exhibit_url": F.text(r["exhibit_url"]),
                    "metric": F.text(r["metric"]),
                    "period_kind": F.text(r["period_kind"]),
                    "period_label": F.text(r["period_label"]),
                    "fiscal_year": F.integer(r["fiscal_year"]),
                    "fiscal_quarter": F.integer(r["fiscal_quarter"]),
                    "low": _value(r["low"], unit),
                    "high": _value(r["high"], unit),
                    "mid": _value(r["mid"], unit),
                    "unit": _unit(unit),
                    "basis": F.text(r["basis"]),
                    "source_text": F.text(r["source_text"]) or "",
                    "parse_rule": F.text(r["parse_rule"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"fiscal_year": NPS, "fiscal_quarter": NA},
            )
        )
    rows.sort(key=lambda r: (-r.accepted_at.timestamp(), r.accession_number, r.metric, r.period_label))
    prov = _prov(
        ctx,
        "sec_earnings_guidance",
        datasets=("sec.earnings_guidance",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "start": start, "end": end, "as_of": args.as_of},
    )
    return _output(ctx, s, EarningsGuidance, rows, prov)


async def sec_earnings_non_gaap(ctx: ToolContext, args: NonGaapInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    start, end = _window(ctx, args, 3)
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.earnings_non_gaap", identity)
        return F.records(session.read("sec.earnings_non_gaap", as_of=as_of, **session.company(args.ticker)))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    bases = set(args.basis or ("gaap", "non_gaap", "adjustment"))
    rows = []
    for r in records:
        accepted = F.instant(r["event_time"])
        if not F.within(accepted.date(), start, end) or F.text(r["basis"]) not in bases:
            continue
        unit = F.text(r["unit"])
        rows.append(
            common.row(
                NonGaapFigure,
                {
                    "ticker": args.ticker,
                    "cik": F.text(r["cik"]),
                    "accession_number": F.text(r["accession_number"]),
                    "accepted_at": accepted,
                    "exhibit": F.text(r["exhibit"]),
                    "exhibit_url": F.text(r["exhibit_url"]),
                    "metric": F.text(r["metric"]),
                    "basis": F.text(r["basis"]),
                    "reconciles": F.text(r["reconciles"]),
                    "fiscal_period_label": F.text(r["fiscal_period_label"]),
                    "value": _value(r["value"], unit),
                    "unit": _unit(unit),
                    "source_text": F.text(r["source_text"]) or "",
                    "parse_rule": F.text(r["parse_rule"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"fiscal_period_label": NPS},
            )
        )
    rows.sort(key=lambda r: (-r.accepted_at.timestamp(), r.accession_number, r.reconciles, r.basis, r.metric))
    prov = _prov(
        ctx,
        "sec_earnings_non_gaap",
        datasets=("sec.earnings_non_gaap",),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "basis": args.basis, "start": start, "end": end, "as_of": args.as_of},
    )
    return _output(ctx, s, NonGaapFigure, rows, prov)


# --- segment facts ------------------------------------------------------------------------------

#: The forms whose XBRL holds segment facts.
_PERIODIC = ("10-K", "10-K/A", "10-KT", "10-Q", "10-Q/A", "10-QT")


class SegmentInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    filings: int = Field(
        default=4, ge=1, le=12, description="Read the newest this many 10-K and 10-Q filings' own XBRL."
    )
    axis_kind: list[Literal["segment", "geography", "disaggregation", "other"]] | None = Field(
        default=None, min_length=1, description="Only these kinds of axis. Default: all."
    )
    as_of: AsOf | None = common.as_of_field()


async def sec_segment_facts(ctx: ToolContext, args: SegmentInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    identity = {"ticker": args.ticker}

    def work(session):
        session.refresh("sec.submissions", identity)
        index = F.records(session.read("sec.submissions", as_of=as_of, **session.company(args.ticker)))
        periodic = sorted(
            (r for r in index if F.text(r["form"]) in _PERIODIC),
            key=lambda r: F.instant(r["knowledge_time"]),
            reverse=True,
        )[: args.filings]
        accessions = sorted(F.text(r["accession_number"]) for r in periodic)
        cik = F.text(periodic[0]["cik"]) if periodic else None
        if accessions:
            # each filing read from its own XBRL once (marketlens-data skips none it was asked for)
            session.refresh("sec.segment_facts", {"accession": ",".join(accessions)})
        if cik is None:
            return []
        records = F.records(session.read("sec.segment_facts", as_of=as_of, cik=cik))
        return [r for r in records if F.text(r["accn"]) in set(accessions)]

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    kinds = set(args.axis_kind or ())
    rows = []
    for r in records:
        kind = F.text(r["axis_kind"])
        if kinds and not (kind and set(kind.split(";")) & kinds):
            continue
        rows.append(
            common.row(
                SegmentFact,
                {
                    "ticker": args.ticker,
                    "cik": F.text(r["cik"]),
                    "accession_number": F.text(r["accn"]),
                    "form": F.text(r["form"]),
                    "filed": F.day(r["filed"]),
                    "fiscal_year": F.integer(r["fiscal_year"]),
                    "fiscal_period": F.text(r["fiscal_period"]),
                    "concept": F.text(r["concept"]),
                    "axis": F.text(r["axis"]),
                    "axis_kind": kind,
                    "member": F.text(r["member"]),
                    "member_key": F.text(r["member_key"]),
                    "member_label": F.text(r["member_label"]),
                    "consolidation": F.text(r["consolidation"]),
                    "period_start": F.day(r["period_start"]),
                    "period_end": F.day(r["period_end"]),
                    "quarters": F.integer(r["qtrs"]),
                    "value": F.number(r["value"]),
                    "unit": F.text(r["unit"]),
                    "source": F.text(r["source_file"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "axis": NA,
                    "axis_kind": NA,
                    "member": NA,
                    "member_key": NA,
                    "member_label": NPS,
                    "consolidation": NA,
                    "period_start": NA,
                    "fiscal_year": NPS,
                    "fiscal_period": NPS,
                },
            )
        )
    rows.sort(
        key=lambda r: (r.period_end, r.quarters, r.concept, r.axis or "", r.member_key or ""), reverse=True
    )
    prov = _prov(
        ctx,
        "sec_segment_facts",
        datasets=("sec.submissions", "sec.segment_facts"),
        as_of=as_of,
        rows=rows,
        session=session,
        request={
            "ticker": args.ticker,
            "filings": args.filings,
            "axis_kind": args.axis_kind,
            "as_of": args.as_of,
        },
    )
    return _output(ctx, s, SegmentFact, rows, prov)


# --- a foreign private issuer's quarters ---------------------------------------------------------


class ForeignInputs(Inputs):
    ticker: Ticker = Field(description="A foreign private issuer's ticker (its ADR), e.g. TSM.")
    statement: list[Literal["income", "balance", "cash_flow", "segments", "kpi"]] | None = Field(
        default=None, min_length=1, description="Only these statements. Default: all."
    )
    as_of: AsOf | None = common.as_of_field()


async def sec_foreign_quarterly(ctx: ToolContext, args: ForeignInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)
    identity = {"ticker": args.ticker}

    def work(session):
        index_fresh = session.refresh("sec.submissions", identity)
        releases_fresh = session.refresh("sec.earnings_releases", identity)
        # derived from the stored releases and filings index: read again whenever either was fetched
        session.refresh(
            "sec.foreign_quarterly_statements", identity, force=not (index_fresh and releases_fresh)
        )
        company = session.company(args.ticker)
        return F.records(session.read("sec.foreign_quarterly_statements", as_of=as_of, **company))

    records, session = await common.run(ctx, s, work, source="sec", subject=("filer", args.ticker))
    wanted = set(args.statement or ())
    rows = []
    for r in records:
        if wanted and F.text(r["statement"]) not in wanted:
            continue
        rows.append(
            common.row(
                ForeignQuarterlyFigure,
                {
                    "ticker": args.ticker,
                    "cik": F.text(r["cik"]),
                    "form": F.text(r["form"]),
                    "accession_number": F.text(r["accession_number"]),
                    "exhibit": F.text(r["exhibit"]),
                    "exhibit_url": F.text(r["exhibit_url"]),
                    "statement": F.text(r["statement"]),
                    "line": F.text(r["line"]),
                    "member": F.text(r["member"]),
                    "axis": F.text(r["axis"]),
                    "value": F.number(r["value"]),
                    "unit": F.text(r["unit"]),
                    "scale": F.integer(r["scale"]),
                    "period_label": F.text(r["period_label"]),
                    "period_start": F.day(r["period_start"]),
                    "period_end": F.day(r["period_end"]),
                    "fiscal_year": F.integer(r["fiscal_year"]),
                    "fiscal_period": F.text(r["fiscal_period"]),
                    "source_text": F.text(r["source_text"]) or "",
                    "parse_rule": F.text(r["parse_rule"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={"member": NA, "axis": NA, "period_start": NA},
            )
        )
    rows.sort(key=lambda r: (r.statement, r.line, r.axis or "", r.member or ""))
    rows.sort(key=lambda r: r.period_end, reverse=True)
    prov = _prov(
        ctx,
        "sec_foreign_quarterly",
        datasets=("sec.submissions", "sec.earnings_releases", "sec.foreign_quarterly_statements"),
        as_of=as_of,
        rows=rows,
        session=session,
        request={"ticker": args.ticker, "statement": args.statement, "as_of": args.as_of},
    )
    return _output(ctx, s, ForeignQuarterlyFigure, rows, prov)


# --- company documents and targets ---------------------------------------------------------------


class IrInputs(Inputs):
    ticker: Ticker = Field(description=TICKER_DOC)
    as_of: AsOf | None = common.as_of_field()


def _ir_refresh(session, ticker: str, *, targets: bool) -> None:
    identity = {"ticker": ticker}
    session.refresh("sec.submissions", identity)  # the filed copy is matched against the filings index
    documents_fresh = session.refresh("company.ir_documents", identity)
    if targets:
        # derived from the stored documents: read again whenever they were just fetched
        session.refresh("company.ir_targets", identity, force=not documents_fresh)


def _authorities(rows) -> list[str]:
    return sorted({r.authority for r in rows}) or ["company"]


async def company_ir_documents(ctx: ToolContext, args: IrInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)

    def work(session):
        _ir_refresh(session, args.ticker, targets=False)
        return F.records(session.read("company.ir_documents", as_of=as_of, ticker=args.ticker))

    records, session = await common.run(ctx, s, work, source="company", subject=("company", args.ticker))
    rows = [
        common.row(
            IrDocument,
            {
                "ticker": F.text(r["ticker"]) or args.ticker,
                "cik": F.text(r["cik"]),
                "document_url": F.text(r["document_url"]),
                "published": F.day(r["published"]),
                "kind": F.text(r["kind"]),
                "title": F.text(r["title"]),
                "authority": F.text(r["authority"]),
                "served_url": F.text(r["served_url"]),
                "edgar_accession": F.text(r["edgar_accession"]),
                "edgar_form": F.text(r["edgar_form"]),
                "content_type": F.text(r["content_type"]),
                "sha256": F.text(r["sha256"]),
                "text": F.text(r["text"]) or "",
                "text_chars": F.integer(r["text_chars"]),
                "truncated": bool(F.flag(r["truncated"])),
                "knowledge_time": F.instant(r["knowledge_time"]),
            },
            codes={"cik": NPS, "edgar_accession": NA, "edgar_form": NA},
        )
        for r in records
    ]
    rows.sort(key=lambda r: (r.published, r.document_url), reverse=True)
    prov = _prov(
        ctx,
        "company_ir_documents",
        datasets=("sec.submissions", "company.ir_documents"),
        as_of=as_of,
        rows=rows,
        session=session,
        authority=_authorities(rows),
        provider="company",
        request={"ticker": args.ticker, "as_of": args.as_of},
    )
    notes = (
        []
        if rows
        else [f"No document of {args.ticker} is listed in marketlens-data's config/ir_documents.yaml."]
    )
    return _output(ctx, s, IrDocument, rows, prov, notes=notes)


async def company_ir_targets(ctx: ToolContext, args: IrInputs) -> ToolOutput:
    s = _settings(ctx)
    as_of = common.as_of_of(args.as_of)

    def work(session):
        _ir_refresh(session, args.ticker, targets=True)
        return F.records(session.read("company.ir_targets", as_of=as_of, ticker=args.ticker))

    records, session = await common.run(ctx, s, work, source="company", subject=("company", args.ticker))
    rows = []
    for r in records:
        unit = F.text(r["unit"])
        rows.append(
            common.row(
                IrTarget,
                {
                    "ticker": F.text(r["ticker"]) or args.ticker,
                    "document_url": F.text(r["document_url"]),
                    "published": F.day(r["published"]),
                    "title": F.text(r["title"]),
                    "kind": F.text(r["kind"]),
                    "authority": F.text(r["authority"]),
                    "served_url": F.text(r["served_url"]),
                    "edgar_accession": F.text(r["edgar_accession"]),
                    "metric": F.text(r["metric"]),
                    "horizon": F.text(r["horizon"]),
                    "horizon_start": F.integer(r["horizon_start"]),
                    "horizon_end": F.integer(r["horizon_end"]),
                    "low": _value(r["low"], unit),
                    "high": _value(r["high"], unit),
                    "mid": _value(r["mid"], unit),
                    "unit": _unit(unit),
                    "basis": F.text(r["basis"]),
                    "source_text": F.text(r["source_text"]) or "",
                    "parse_rule": F.text(r["parse_rule"]),
                    "knowledge_time": F.instant(r["knowledge_time"]),
                },
                codes={
                    "edgar_accession": NA,
                    "horizon_start": NPS,
                    "horizon_end": NPS,
                    "low": NA,
                    "high": NA,
                    "mid": NA,
                },
            )
        )
    rows.sort(key=lambda r: (r.metric, r.horizon))
    rows.sort(key=lambda r: r.published, reverse=True)
    prov = _prov(
        ctx,
        "company_ir_targets",
        datasets=("sec.submissions", "company.ir_documents", "company.ir_targets"),
        as_of=as_of,
        rows=rows,
        session=session,
        authority=_authorities(rows),
        provider="company",
        request={"ticker": args.ticker, "as_of": args.as_of},
    )
    return _output(ctx, s, IrTarget, rows, prov)


# --- specs --------------------------------------------------------------------------------------


def _spec(name, title, description, readme, inputs, model, provider, *, risk="external_text") -> ToolSpec:
    return ToolSpec(
        name=name,
        capability="filings",
        title=title,
        description=description,
        readme=readme,
        input_model=inputs,
        output_model=model,
        provider=provider,
        route=ROUTES[name],
        handler=HANDLERS[name],
        golden_test=GOLDEN,
        env=ENV,
        output_risk=risk,  # type: ignore[arg-type]
    )


HANDLERS = {
    "sec_earnings_guidance": sec_earnings_guidance,
    "sec_earnings_non_gaap": sec_earnings_non_gaap,
    "sec_segment_facts": sec_segment_facts,
    "sec_foreign_quarterly": sec_foreign_quarterly,
    "company_ir_documents": company_ir_documents,
    "company_ir_targets": company_ir_targets,
}

SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "sec_earnings_guidance",
        "SEC earnings guidance",
        common.describe(
            "A company's guidance from the press release of each earnings 8-K (EX-99.1; a foreign issuer's "
            "results 6-K): revenue, gross and operating margin, operating income, operating expenses, tax "
            "rate, capex and EPS, and the growth rates of revenue (reported or constant currency), organic "
            "revenue and EPS, for the next quarter or the fiscal year, as low, high and mid, read from "
            "outlook sentences and guidance tables by named rules; never from a segment or a region, never "
            "a guess. Margins, rates and growth rates as fractions (a decline negative). source_text is "
            "company text (untrusted). "
            f"{CONTACT}",
            "at_event",
        ),
        "Company guidance from earnings press releases",
        GuidanceInputs,
        EarningsGuidance,
        "sec",
    ),
    _spec(
        "sec_earnings_non_gaap",
        "SEC non-GAAP reconciliation",
        common.describe(
            "The GAAP to non-GAAP reconciliation of each earnings press release (EX-99.1): gross profit and "
            "margin, operating expenses, operating income and margin, net income and diluted EPS on both "
            "bases, and the bridge lines (stock-based compensation, amortization of acquired intangibles, "
            "restructuring) with the GAAP line each reconciles, in the column of the release's quarter, "
            f"dollars in full. Margins as fractions. source_text is company text (untrusted). {CONTACT}",
            "at_event",
        ),
        "GAAP to non-GAAP reconciliation lines of earnings releases",
        NonGaapInputs,
        NonGaapFigure,
        "sec",
    ),
    _spec(
        "sec_segment_facts",
        "SEC segment facts",
        common.describe(
            "A company's dimensional XBRL facts, which the companyfacts API drops: each concept per business "
            "segment, product, region or revenue-disaggregation line (axis, axis_kind, the member as filed, "
            "member_key across spellings, the filer's own member name), with the consolidation qualifier, "
            "read from the newest 10-K and 10-Q filings' own XBRL (filings, default 4) and served with any "
            "the SEC's Financial Statement and Notes data sets already gave. Period ends rounded to the "
            f"month end. Member names are filer text (untrusted). {CONTACT} " + common.PIT["at_event"],
            "vintage",
        ),
        "Segment, product, region and disaggregation facts from XBRL",
        SegmentInputs,
        SegmentFact,
        "sec",
    ),
    _spec(
        "sec_foreign_quarterly",
        "SEC foreign issuer quarters",
        common.describe(
            "A foreign private issuer's quarters from the exhibits of its results 6-Ks (TSM): the press "
            "release's income statement, the slides' balance sheet, cash flow, shipments and US$ revenue, "
            "the revenue split by platform and technology (a management report's shares, a slide's chart "
            "labels, the interim statements' revenue note), each in the issuer's unit at its stated scale, "
            "by named rules over allowlisted row labels; a figure two exhibits state differently has no "
            f"row. Labels are company text (untrusted). {CONTACT}",
            "at_event",
        ),
        "A foreign issuer's quarterly figures from its 6-K exhibits",
        ForeignInputs,
        ForeignQuarterlyFigure,
        "sec",
    ),
    _spec(
        "company_ir_documents",
        "Company investor documents",
        common.describe(
            "The investor documents marketlens-data's operator lists for a company (config/ir_documents.yaml: "
            "investor-day releases, outlooks, annual-report letters, presentations): each document's text "
            "(capped), sha256 and kind, served from EDGAR with authority official when the company also "
            "filed it (an 8-K or 6-K EX-99 exhibit within a week, an ARS within 400 days), else from the "
            "company's site with authority company. Company text (untrusted). A ticker not listed has no "
            f"document. {CONTACT} " + common.PIT["at_event"],
            "snapshot",
        ),
        "A company's own investor documents, filed copy preferred",
        IrInputs,
        IrDocument,
        "company",
    ),
    _spec(
        "company_ir_targets",
        "Company multi-year targets",
        common.describe(
            "The multi-year targets a company's listed investor documents state (revenue CAGR, gross and "
            "operating margin, EPS, capex, free cash flow): low, high and mid (a floor has only a low, a "
            "ceiling only a high), the horizon as stated with its years, the basis, the sentence and the "
            "rule; never from a table, never a guess. Margins and CAGRs as fractions. Each row carries its "
            f"document's authority (official for a filed copy, else company). {CONTACT} "
            + common.PIT["at_event"],
            "snapshot",
        ),
        "A company's published multi-year targets",
        IrInputs,
        IrTarget,
        "company",
    ),
)

DATASETS: dict[str, tuple[str, ...]] = {
    "sec_earnings_guidance": ("sec.earnings_guidance",),
    "sec_earnings_non_gaap": ("sec.earnings_non_gaap",),
    "sec_segment_facts": ("sec.submissions", "sec.segment_facts"),
    "sec_foreign_quarterly": ("sec.submissions", "sec.earnings_releases", "sec.foreign_quarterly_statements"),
    "company_ir_documents": ("sec.submissions", "company.ir_documents"),
    "company_ir_targets": ("sec.submissions", "company.ir_documents", "company.ir_targets"),
}
