"""sec_earnings_guidance, sec_earnings_non_gaap, sec_segment_facts,
sec_foreign_quarterly, company_ir_documents and company_ir_targets against a
synthetic EDGAR: GUIDCO (CIK 9999905, an earnings 8-K whose release states an
outlook and a GAAP to non-GAAP reconciliation), TESTCO's 10-Q 0009999901-26-000010
(its own XBRL with segment facts) and its investor-day page on a synthetic
company host, and FPICO (CIK 9999906, a foreign issuer's results 6-K with a
management report)."""

from __future__ import annotations

import httpx
import pytest
import test_sec
from data_harness import KEYS, SPECS, call, check_golden, context, fixture_text
from test_sec import ARCH, sec_routes

IR_URL = "https://ir.testco.example.com/news/investor-day-2026"
IR_LIST = {
    "TESTCO": [
        {
            "url": IR_URL,
            "kind": "investor_day",
            "published": "2026-05-12",
            "title": "Testco Synthetic Corp Investor Day 2026",
        }
    ]
}


def research_routes(upstream):
    sec_routes(upstream)
    j, t = upstream.json_file, upstream.text_file
    test_sec.FEEDS.setdefault(9999905, "sec.submissions__GUIDCO.json")
    test_sec.FEEDS.setdefault(9999906, "sec.submissions__FPICO.json")
    j(r"data\.sec\.gov/submissions/CIK0009999905\.json", "sec.submissions__GUIDCO.json")
    j(r"data\.sec\.gov/submissions/CIK0009999906\.json", "sec.submissions__FPICO.json")
    gc = f"{ARCH}/9999905/000999990526000031"
    t(rf"{gc}/0009999905-26-000031-index\.htm", "sec.8k_index__GUIDCO_26-000031.htm")
    t(rf"{gc}/gc-ex991\.htm", "sec.ex991__GUIDCO_26-000031.htm")
    fp = f"{ARCH}/9999906/000999990626000044"
    t(rf"{fp}/0009999906-26-000044-index\.htm", "sec.6k_index__FPICO_26-000044.htm")
    t(rf"{fp}/fp-ex991\.htm", "sec.ex991__FPICO_26-000044.htm")
    t(rf"{fp}/fp-ex993\.htm", "sec.ex993__FPICO_26-000044.htm")
    tc = f"{ARCH}/9999901"
    j(rf"{tc}/000999990126000010/index\.json", "sec.10q_index__TESTCO_26-000010.json")
    t(rf"{tc}/000999990126000010/testco-20260630_htm\.xml", "sec.10q_instance__TESTCO_26-000010.xml")
    j(rf"{tc}/000999990126000002/index\.json", "sec.10k_index__TESTCO_26-000002.json")
    t(
        r"www\.sec\.gov/data-research/sec-markets-data/financial-statement-notes-data-sets",
        "sec.fsn_page.html",
    )
    upstream.add(
        r"ir\.testco\.example\.com/news/investor-day-2026",
        lambda _r: httpx.Response(
            200,
            text=fixture_text("company.ir_page__TESTCO_investor_day.html"),
            headers={"content-type": "text/html; charset=utf-8"},
        ),
    )


@pytest.fixture
def edgar(upstream, keys, monkeypatch):
    from omni import config

    monkeypatch.setattr(config, "ir_documents_config", lambda: IR_LIST)
    research_routes(upstream)
    return upstream


# --- guidance and the non-GAAP reconciliation ---------------------------------------------------


def test_sec_earnings_guidance_golden(tmp_path, edgar):
    out = call("sec_earnings_guidance", context(tmp_path), ticker="guidco")
    doc = check_golden(out, "sec_earnings_guidance")
    by = {r["metric"]: r for r in doc["rows"]}
    assert set(by) == {"revenue", "gross_margin"}
    rev = by["revenue"]
    assert (rev["low"], rev["mid"], rev["high"], rev["unit"]) == (12.7e9, 13e9, 13.3e9, "USD")
    assert (rev["period_kind"], rev["fiscal_year"], rev["fiscal_quarter"]) == ("next_quarter", 2026, 3)
    margin = by["gross_margin"]
    assert (margin["mid"], margin["unit"], margin["basis"]) == (0.56, "fraction", "non_gaap")
    assert rev["accepted_at"] == "2026-08-04T20:05:00Z" and rev["exhibit"] == "EX-99.1"
    assert doc["provenance"]["dataset"] == "sec.earnings_guidance"
    assert doc["provenance"]["authority"] == ["official"]
    assert SPECS["sec_earnings_guidance"].output_risk == "external_text"
    assert all(KEYS["SEC_CONTACT_EMAIL"] in r.headers["User-Agent"] for r in edgar.requests)
    before = call("sec_earnings_guidance", context(tmp_path), ticker="GUIDCO", as_of="2026-08-04T20:00:00Z")
    assert before.rows == []


def test_sec_earnings_non_gaap_golden(tmp_path, edgar):
    out = call("sec_earnings_non_gaap", context(tmp_path), ticker="GUIDCO")
    doc = check_golden(out, "sec_earnings_non_gaap")
    rows = {(r["reconciles"], r["basis"], r["metric"]): r for r in doc["rows"]}
    assert rows[("gross_profit", "gaap", "gross_profit")]["value"] == 6.203e9
    assert rows[("gross_profit", "non_gaap", "gross_profit")]["value"] == 6.488e9
    assert rows[("gross_profit", "adjustment", "stock_based_compensation")]["value"] == 8e6
    assert rows[("gross_margin", "non_gaap", "gross_margin")]["value"] == 0.56
    assert rows[("eps_diluted", "non_gaap", "eps_diluted")]["value"] == 1.66
    assert {r["fiscal_period_label"] for r in doc["rows"]} == {"Three Months Ended June 27, 2026"}
    only = call("sec_earnings_non_gaap", context(tmp_path), ticker="GUIDCO", basis=["adjustment"])
    assert {r.basis for r in only.rows} == {"adjustment"} and len(only.rows) < len(doc["rows"])


# --- segment facts ---------------------------------------------------------------------------------


def test_sec_segment_facts_golden(tmp_path, edgar):
    out = call("sec_segment_facts", context(tmp_path), ticker="TESTCO")
    doc = check_golden(out, "sec_segment_facts")
    rows = {(r["concept"], r["axis"], r["member"], r["quarters"]): r for r in doc["rows"]}
    rev = "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert rows[(rev, "StatementBusinessSegmentsAxis", "Cloud", 1)]["value"] == 320e6
    assert rows[(rev, "StatementBusinessSegmentsAxis", "Cloud", 2)]["value"] == 610e6
    assert rows[("OperatingIncomeLoss", "StatementBusinessSegmentsAxis", "Devices", 1)]["value"] == -12e6
    assert rows[(rev, "StatementGeographicalAxis", "US", 1)]["axis_kind"] == "geography"
    assert rows[(rev, "ProductOrServiceAxis", "Subscription", 1)]["axis_kind"] == "disaggregation"
    cloud = rows[(rev, "StatementBusinessSegmentsAxis", "Cloud", 1)]
    assert (cloud["accession_number"], cloud["form"], cloud["source"]) == (
        "0009999901-26-000010",
        "10-Q",
        "edgar:0009999901-26-000010",
    )
    assert cloud["knowledge_time"] == "2026-08-01T20:15:00Z"  # the 10-Q's acceptance
    # the 10-K's folder holds no XBRL instance: read, no row; the undimensioned total is no segment fact
    assert {r["accession_number"] for r in doc["rows"]} == {"0009999901-26-000010"}
    geo = call("sec_segment_facts", context(tmp_path), ticker="TESTCO", axis_kind=["geography"])
    assert [r.member for r in geo.rows] == ["US"]


# --- a foreign issuer's quarters ----------------------------------------------------------------


def test_sec_foreign_quarterly_golden(tmp_path, edgar):
    out = call("sec_foreign_quarterly", context(tmp_path), ticker="FPICO", statement=["segments"])
    doc = check_golden(out, "sec_foreign_quarterly")
    rows = {(r["axis"], r["member"], r["period_label"]): r for r in doc["rows"]}
    hpc = rows[("platform", "HPC", "3Q26")]
    assert (hpc["value"], hpc["unit"], hpc["parse_rule"], hpc["exhibit"]) == (
        0.66,
        "fraction",
        "report.share_table",
        "EX-99.3",
    )
    assert (hpc["period_end"], hpc["fiscal_year"], hpc["fiscal_period"]) == ("2026-09-30", 2026, "Q3")
    assert hpc["knowledge_time"] == "2026-10-16T09:30:00Z"  # the 6-K's acceptance
    shares = [r["value"] for r in doc["rows"] if r["axis"] == "platform" and r["period_label"] == "3Q26"]
    assert round(sum(shares), 6) == 1.0
    assert (
        doc["provenance"]["dataset"]
        == "sec.submissions,sec.earnings_releases,sec.foreign_quarterly_statements"
    )


# --- company documents and targets ------------------------------------------------------------------


def test_company_ir_documents_golden(tmp_path, edgar, frozen_clock):
    out = call("company_ir_documents", context(tmp_path), ticker="testco")
    doc = check_golden(out, "company_ir_documents")
    (row,) = doc["rows"]
    assert (row["authority"], row["served_url"], row["content_type"], row["edgar_accession"]) == (
        "company",
        IR_URL,
        "html",
        None,
    )
    assert "greater than 20% CAGR" in row["text"]
    assert doc["provenance"]["authority"] == ["company"]
    assert doc["provenance"]["provider"] == "company"
    assert SPECS["company_ir_documents"].output_risk == "external_text"
    unlisted = call("company_ir_documents", context(tmp_path), ticker="EVNTCO")
    assert unlisted.rows == [] and "config/ir_documents.yaml" in unlisted.notes[0]


def test_company_ir_targets_golden(tmp_path, edgar, frozen_clock):
    out = call("company_ir_targets", context(tmp_path), ticker="TESTCO")
    doc = check_golden(out, "company_ir_targets")
    by = {r["metric"]: r for r in doc["rows"]}
    cagr, margin = by["revenue_cagr"], by["operating_margin"]
    assert (cagr["low"], cagr["high"], cagr["unit"]) == (0.2, None, "fraction")
    assert cagr["absent"]["high"] == "not_applicable"
    assert (cagr["horizon_start"], cagr["horizon_end"]) == (2026, 2031)
    assert (margin["mid"], margin["basis"], margin["horizon_end"]) == (0.3, "non_gaap", 2029)
    assert {r["authority"] for r in doc["rows"]} == {"company"}
    assert doc["provenance"]["dataset"] == "sec.submissions,company.ir_documents,company.ir_targets"


def test_research_tool_refusals(tmp_path, edgar):
    import asyncio

    from marketlens_mcp import pipeline
    from marketlens_mcp.plugin_api import ToolError

    ctx = context(tmp_path)
    for name, bad in (
        ("sec_segment_facts", {"ticker": "TESTCO", "filings": 0}),
        ("sec_segment_facts", {"ticker": "TESTCO", "axis_kind": ["region"]}),
        ("sec_earnings_non_gaap", {"ticker": "GUIDCO", "basis": ["adjusted"]}),
        ("sec_foreign_quarterly", {"ticker": "FPICO", "statement": ["notes"]}),
        ("company_ir_targets", {"ticker": "NOT A TICKER!"}),
    ):
        with pytest.raises(ToolError) as info:
            asyncio.run(pipeline.execute(SPECS[name], ctx, bad))
        assert info.value.code == "invalid_arguments", (name, bad)


def test_fixture_documents_are_synthetic():
    for name in (
        "sec.ex991__GUIDCO_26-000031.htm",
        "sec.ex993__FPICO_26-000044.htm",
        "sec.10q_instance__TESTCO_26-000010.xml",
        "company.ir_page__TESTCO_investor_day.html",
    ):
        assert "synthetic" in fixture_text(name), name


# --- two listed classes of one company ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "company", "extra"),
    (
        ("sec_earnings_guidance", "GUIDCO", {}),
        ("sec_earnings_non_gaap", "GUIDCO", {}),
        ("sec_segment_facts", "TESTCO", {}),
        ("sec_foreign_quarterly", "FPICO", {"statement": ["segments"]}),
    ),
)
def test_a_second_listed_class_reads_its_company_rows(tmp_path, edgar, monkeypatch, tool, company, extra):
    """One payload written under two tickers sharing a CIK (test_sec.CLASS_B): the
    second class reads the company's rows (bug audit 2026-10-26 #2, B144)."""
    test_sec.list_class_b(edgar, monkeypatch, tmp_path)
    second = test_sec.CLASS_B[company][0]
    first = call(tool, context(tmp_path), ticker=company, **extra)
    test_sec.same_rows(first, call(tool, context(tmp_path), ticker=second, **extra), second)
