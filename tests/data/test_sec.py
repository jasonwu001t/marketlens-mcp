"""sec_filings, sec_xbrl_facts, sec_fundamentals, sec_earnings_releases,
sec_earnings_figures, sec_insider_trades, sec_13f_holdings, sec_fund_nport and
sec_fund_holdings against a synthetic EDGAR: the fictional company TESTCO
(CIK 9999901), manager CIK 9999950 and fund series S000099991 (TFUND)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from data_harness import KEYS, call, check_golden, context, fixture_text, refused

ARCH = r"www\.sec\.gov/Archives/edgar/data"


def sec_routes(upstream):
    j, t = upstream.json_file, upstream.text_file
    j(r"www\.sec\.gov/files/company_tickers\.json", "sec.company_tickers.json")
    j(r"www\.sec\.gov/files/company_tickers_mf\.json", "sec.company_tickers_mf.json")
    j(r"data\.sec\.gov/submissions/CIK0009999901\.json", "sec.submissions__TESTCO.json")
    j(r"data\.sec\.gov/submissions/CIK0009999950\.json", "sec.submissions__13F.json")
    j(r"data\.sec\.gov/submissions/CIK0009999970\.json", "sec.submissions__FUND.json")
    j(r"data\.sec\.gov/api/xbrl/companyfacts/CIK0009999901\.json", "sec.company_facts__TESTCO.json")
    # the earnings press release of 0009999901-26-000009; the other release's index is gone
    t(rf"{ARCH}/9999901/000999990126000009/0009999901-26-000009-index\.htm", "sec.8k_index__26-000009.htm")
    t(rf"{ARCH}/9999901/000999990126000009/tc-ex991\.htm", "sec.ex991__26-000009.htm")
    upstream.add(rf"{ARCH}/9999901/000999990126000001/.*", httpx.Response(404))
    # insider documents (the XSL rendering directory stripped)
    t(rf"{ARCH}/9999901/000999990126000005/form4\.xml", "sec.form4__26-000005.xml")
    t(rf"{ARCH}/9999901/000999990126000004/primary_doc\.xml", "sec.form144__26-000004.xml")
    # 13F: directory index, information table, cover page
    for acc in ("26-000003", "26-000004", "26-000002"):
        folder = f"{ARCH}/9999950/0009999950{acc.replace('-', '')}"
        j(rf"{folder}/index\.json", f"sec.13f_index__{acc}.json")
        t(rf"{folder}/infotable\.xml", f"sec.13f_table__{acc}.xml")
        t(rf"{folder}/primary_doc\.xml", f"sec.13f_cover__{acc}.xml")
    # N-PORT
    t(r"www\.sec\.gov/cgi-bin/browse-edgar", "sec.nport_feed__S000099991.xml")
    for acc in ("26-000005", "26-000002"):
        t(rf"{ARCH}/9999970/0009999970{acc.replace('-', '')}/primary_doc\.xml", f"sec.nport_doc__{acc}.xml")


@pytest.fixture
def edgar(upstream, keys):
    sec_routes(upstream)
    return upstream


# --- filings ----------------------------------------------------------------------------------


def test_sec_filings_golden(tmp_path, edgar):
    out = call("sec_filings", context(tmp_path), ticker="testco")
    doc = check_golden(out, "sec_filings")
    assert [r["form"] for r in doc["rows"]] == ["10-Q", "8-K", "4", "144", "10-K", "8-K", "4", "8-K"]
    assert doc["rows"][1]["items"] == ["2.02", "9.01"]
    assert doc["rows"][0]["accepted_at"] == "2026-08-01T20:15:00Z"
    # no acceptance time in the feed: the end of the filing day in New York, never the start
    assert doc["rows"][-1]["accepted_at"] == "2025-11-06T04:59:59.999999Z"
    assert doc["provenance"]["authority"] == ["official"]
    agents = {r.headers["User-Agent"] for r in edgar.requests}
    assert agents and all(KEYS["SEC_CONTACT_EMAIL"] in a for a in agents)
    eight_k = call("sec_filings", context(tmp_path), ticker="TESTCO", forms=["8-k"], start="2026-01-01")
    assert [r.accession_number for r in eight_k.rows] == ["0009999901-26-000009", "0009999901-26-000001"]


def test_sec_tools_need_a_contact_email(tmp_path, upstream):
    sec_routes(upstream)
    err = refused("sec_filings", context(tmp_path), ticker="TESTCO")
    assert err.code == "sec_contact_missing"
    assert err.message == (
        "SEC EDGAR asks every client to name a contact email in its User-Agent: set SEC_CONTACT_EMAIL "
        "(for example you@example.com) in this server's environment."
    )
    assert upstream.requests == []
    # reading only what is stored needs no contact
    out = call("sec_filings", context(tmp_path, settings={"mode": "local"}), ticker="TESTCO")
    assert out.rows == [] and out.notes == [
        "mode is local and nothing is stored for this request; set providers.data.mode: auto to fetch it"
    ]


def test_sec_unknown_ticker_is_not_found(tmp_path, edgar):
    err = refused("sec_filings", context(tmp_path), ticker="ZZZZ")
    assert (err.code, err.message) == ("data_not_found", "SEC EDGAR has no filer for ZZZZ.")


# --- XBRL facts and fundamentals ------------------------------------------------------------------


def test_sec_xbrl_facts_golden_and_restatement(tmp_path, edgar):
    out = call("sec_xbrl_facts", context(tmp_path), ticker="TESTCO", metrics=["Revenues"])
    doc = check_golden(out, "sec_xbrl_facts")
    assert [(r["period_end"], r["value"], r["accession_number"]) for r in doc["rows"]] == [
        ("2024-12-31", 1010000000.0, "0009999901-26-000002"),
        ("2025-12-31", 1200000000.0, "0009999901-26-000002"),
    ]
    assert doc["rows"][1]["knowledge_time"] == "2026-02-10T21:00:00Z"  # the 10-K's acceptance
    before = call(
        "sec_xbrl_facts", context(tmp_path), ticker="TESTCO", metrics=["Revenues"], as_of="2025-06-01"
    )
    assert [(r.period_end.isoformat(), r.value) for r in before.rows] == [("2024-12-31", 1000000000.0)]
    every = call(
        "sec_xbrl_facts", context(tmp_path), ticker="TESTCO", metrics=["Revenues"], include_vintages=True
    )
    assert [r.value for r in every.rows] == [1000000000.0, 1010000000.0, 1200000000.0]
    # every vintage, but only those knowable at as_of: the 2026-02-10 restatement stays hidden
    then = call(
        "sec_xbrl_facts",
        context(tmp_path),
        ticker="TESTCO",
        metrics=["Revenues"],
        include_vintages=True,
        as_of="2025-06-01",
    )
    assert [(r.period_end.isoformat(), r.value) for r in then.rows] == [("2024-12-31", 1000000000.0)]
    assert all(r.knowledge_time <= datetime(2025, 6, 1, tzinfo=UTC) for r in then.rows)
    assert 1010000000.0 not in [r.value for r in then.rows]
    dei = call("sec_xbrl_facts", context(tmp_path), ticker="TESTCO", taxonomy="dei")
    assert [(r.metric, r.unit, r.period_start) for r in dei.rows] == [
        ("EntityCommonStockSharesOutstanding", "shares", None)
    ]
    assert dei.rows[0].absent == {"period_start": "not_applicable"}


def test_sec_fundamentals_golden_two_step(tmp_path, edgar):
    out = call("sec_fundamentals", context(tmp_path), ticker="TESTCO")
    doc = check_golden(out, "sec_fundamentals")
    by = {(r["metric"], r["period_end"]): r for r in doc["rows"]}
    assert by[("revenue", "2025-12-31")]["value"] == 1200000000.0
    margin = by[("gross_margin", "2025-12-31")]
    assert (margin["value"], margin["unit"]) == (0.4, "fraction")  # stored as 40.0 percent
    assert by[("net_margin", "2025-12-31")]["value"] == 0.125
    assert doc["provenance"]["dataset"] == "sec.company_facts,sec.fundamentals"
    missing = [r for r in doc["rows"] if r["value"] is None]
    assert missing and all(r["reason"] and r["absent"]["value"] for r in missing)
    hosts = {(r.url.host, r.url.path) for r in edgar.requests}
    assert ("data.sec.gov", "/api/xbrl/companyfacts/CIK0009999901.json") in hosts
    # point in time: before the FY2025 10-K only FY2024's revenue was computable
    before = call(
        "sec_fundamentals", context(tmp_path), ticker="TESTCO", metrics=["revenue"], as_of="2025-06-01"
    )
    assert [(r.period_end.isoformat(), r.value) for r in before.rows] == [("2024-12-31", 1000000000.0)]


# --- earnings ---------------------------------------------------------------------------------------


def test_sec_earnings_releases_golden(tmp_path, edgar):
    out = call("sec_earnings_releases", context(tmp_path), ticker="TESTCO")
    doc = check_golden(out, "sec_earnings_releases")
    assert [(r["accession_number"], r["session"]) for r in doc["rows"]] == [
        ("0009999901-26-000009", "after_close"),
        ("0009999901-26-000001", "pre_open"),
    ]


def test_sec_earnings_figures_golden(tmp_path, edgar):
    out = call("sec_earnings_figures", context(tmp_path), ticker="TESTCO")
    doc = check_golden(out, "sec_earnings_figures")
    assert [(r["metric"], r["value"], r["fiscal_period_label"]) for r in doc["rows"]] == [
        ("eps_adjusted", 0.51, "Three Months Ended June 30, 2026"),
        ("eps_diluted_gaap", 0.42, "Three Months Ended June 30, 2026"),
    ]
    assert doc["rows"][1]["source_text"] == "Diluted EPS | $ | 0.42"
    gaap = call("sec_earnings_figures", context(tmp_path), ticker="TESTCO", metrics=["eps_diluted_gaap"])
    assert [r.metric for r in gaap.rows] == ["eps_diluted_gaap"]


# --- insiders ---------------------------------------------------------------------------------------


def test_sec_insider_trades_golden(tmp_path, edgar):
    out = call("sec_insider_trades", context(tmp_path), ticker="TESTCO")
    doc = check_golden(out, "sec_insider_trades")
    assert [(r["form"], r["line"], r["transaction_code"], r["value_usd"]) for r in doc["rows"]] == [
        ("4", 1, "S", 50250.0),
        ("4", 2, "M", None),
        ("144", 1, "144", 101000.0),
    ]
    exercise = doc["rows"][1]
    assert exercise["absent"] == {"price_per_share": "no_data", "value_usd": "no_data"}
    assert exercise["is_derivative"] is True and exercise["has_10b5_1"] is True
    notice = doc["rows"][2]
    assert notice["absent"] == {
        "insider_cik": "not_provided_by_source",
        "acquired_disposed": "not_applicable",
        "shares_owned_after": "not_applicable",
    }
    # the 2025 Form 4 is before marketlens-data's insider history floor: never fetched
    assert not any("000999990125000030" in str(r.url) for r in edgar.requests)
    only4 = call("sec_insider_trades", context(tmp_path), ticker="TESTCO", forms=["4"])
    assert {r.form for r in only4.rows} == {"4"}


# --- 13F ----------------------------------------------------------------------------------------------


def test_sec_13f_holdings_golden(tmp_path, edgar):
    out = call("sec_13f_holdings", context(tmp_path), filer_cik="9999950")
    doc = check_golden(out, "sec_13f_holdings")
    assert {r["period_end"] for r in doc["rows"]} == {"2026-06-30"}
    assert [(r["cusip"], r["value_usd"], r["put_call"]) for r in doc["rows"]] == [
        ("99999T101", 5025000.0, None),
        ("99999O102", 1200000.0, "Put"),
    ]
    # the 2017 filing predates marketlens-data's 13F floor
    assert not any("000999995017000001" in str(r.url) for r in edgar.requests)


def test_sec_13f_restatement_replaces_the_original(tmp_path, edgar):
    out = call("sec_13f_holdings", context(tmp_path), filer_cik="0009999950", period_end="2026-03-31")
    assert [(r.accession_number, r.cusip, r.value_usd) for r in out.rows] == [
        ("0009999950-26-000004", "99999T101", 4500000.0)
    ]
    assert out.notes == [
        "A restatement (0009999950-26-000004) replaces the earlier 13F-HR filings for 2026-03-31."
    ]
    before = call(
        "sec_13f_holdings",
        context(tmp_path),
        filer_cik="9999950",
        period_end="2026-03-31",
        as_of="2026-05-20",
    )
    assert [r.accession_number for r in before.rows] == ["0009999950-26-000002"] * 2
    one = call("sec_13f_holdings", context(tmp_path), filer_cik="9999950", cusips=["99999o102"])
    assert [r.cusip for r in one.rows] == ["99999O102"]


# --- N-PORT -------------------------------------------------------------------------------------------


def test_sec_fund_nport_golden(tmp_path, edgar):
    out = call("sec_fund_nport", context(tmp_path), ticker="tfund")
    doc = check_golden(out, "sec_fund_nport")
    assert [(r["report_date"], r["net_assets"]) for r in doc["rows"]] == [
        ("2026-06-30", 398000000.0),
        ("2026-03-31", 380000000.0),
    ]
    assert doc["rows"][0]["class_ids"] == ["C000099991", "C000099992"]
    assert doc["rows"][0]["ticker"] == "TFUND"


def test_sec_fund_holdings_golden(tmp_path, edgar):
    out = call("sec_fund_holdings", context(tmp_path), ticker="TFUND")
    doc = check_golden(out, "sec_fund_holdings")
    assert [(r["line"], r["cusip"], r["pct_value"]) for r in doc["rows"]] == [
        (1, "99999T101", 0.012625),
        (2, None, 0.005),
    ]
    assert {r["accession_number"] for r in doc["rows"]} == {"0009999970-26-000005"}
    older = call(
        "sec_fund_holdings", context(tmp_path), ticker="TFUND", accession_number="0009999970-26-000002"
    )
    assert {r.report_date.isoformat() for r in older.rows} == {"2026-03-31"}


def test_sec_fund_tools_refuse_a_company(tmp_path, edgar):
    call("sec_filings", context(tmp_path), ticker="TESTCO")  # caches SEC's company list
    err = refused("sec_fund_nport", context(tmp_path), ticker="TESTCO")
    assert (err.code, err.message) == ("data_not_found", "SEC EDGAR has no fund for TESTCO.")


def test_fixture_documents_are_synthetic():
    assert "synthetic" in fixture_text("sec.form4__26-000005.xml")
