"""sec_filings, sec_xbrl_facts, sec_xbrl_frames, sec_fundamentals,
sec_earnings_releases, sec_earnings_figures, filings_events,
sec_pay_vs_performance, sec_insider_trades, sec_13f_holdings, sec_fund_nport
and sec_fund_holdings against a synthetic EDGAR: the fictional companies
TESTCO (CIK 9999901), EVNTCO (CIK 9999903, the material 8-Ks) and PVPCO (CIK
9999904, two proxies with the pay-versus-performance table), a synthetic
Revenues frame for CY2026Q2 with EDGAR's XBRL index of 2026 Q3, manager CIK
9999950 and fund series S000099991 (TFUND)."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from data_harness import (
    CAPABILITIES,
    KEYS,
    NOW,
    SPECS,
    call,
    check_golden,
    context,
    fixture_json,
    fixture_text,
    refused,
)

ARCH = r"www\.sec\.gov/Archives/edgar/data"
#: The submissions feed of each synthetic EDGAR company, by CIK.
FEEDS = {
    9999901: "sec.submissions__TESTCO.json",
    9999903: "sec.submissions__EVNTCO.json",
    9999904: "sec.submissions__PVPCO.json",
    9999950: "sec.submissions__13F.json",
    9999951: "sec.submissions__13F_amends.json",
    9999970: "sec.submissions__FUND.json",
}


#: The fund series each registrant's N-PORT filings report for (SERIES-ID in their index headers).
SERIES = {9999970: "S000099991"}
HEADERS = rf"{ARCH}/\d+/\d{{18}}/\d{{10}}-\d\d-\d{{6}}-index(?:-headers)?\.html"


def index_headers(request: httpx.Request):
    """EDGAR's index headers of one filing (``-index-headers.html``, the SGML header
    in EDGAR's layout), which marketlens-data reads for the acceptance times of the
    filings EDGAR added since it last rewrote the submissions feed, and for the series
    a fund's N-PORT reports for. Built from the company's synthetic submissions feed,
    which is rewritten (true UTC): ACCEPTANCE-DATETIME is the feed's acceptance as New
    York wall time, so each filing is stamped as the feed reads it. A filing the feed
    gives no acceptance time has no headers yet, nor a filing index (404)."""
    cik, _, name = request.url.path.split("/")[-3:]
    accession = name.split("-index")[0]
    recent = fixture_json(FEEDS[int(cik)])["filings"]["recent"]
    rows = zip(
        recent["accessionNumber"],
        recent["acceptanceDateTime"],
        recent["form"],
        recent["filingDate"],
        strict=True,
    )
    at, form, filed = next((at, form, filed) for accn, at, form, filed in rows if accn == accession)
    if not at or name.endswith("-index.html"):
        return httpx.Response(404)
    accepted = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
    day = filed.replace("-", "")
    series = (
        "<SERIES-AND-CLASSES-CONTRACTS-DATA>\n<EXISTING-SERIES-AND-CLASSES-CONTRACTS>\n<SERIES>\n"
        f"<OWNER-CIK>{int(cik):010d}\n<SERIES-ID>{SERIES[int(cik)]}\n<SERIES-NAME>Synthetic Index Fund\n"
        "</SERIES>\n</EXISTING-SERIES-AND-CLASSES-CONTRACTS>\n</SERIES-AND-CLASSES-CONTRACTS-DATA>\n"
        if form.startswith("NPORT") and int(cik) in SERIES
        else ""
    )
    return (
        f"<HTML><HEAD><TITLE>SEC EDGAR Submission {accession}</TITLE>\n<!-- synthetic -->\n<!--\n"
        f"<SEC-HEADER>{accession}.hdr.sgml : {day}\n<ACCEPTANCE-DATETIME>{accepted:%Y%m%d%H%M%S}\n"
        f"<ACCESSION-NUMBER>{accession}\n<TYPE>{form}\n<PUBLIC-DOCUMENT-COUNT>1\n<FILING-DATE>{day}\n"
        f"{series}</SEC-HEADER>\n-->\n</HEAD><BODY>\n<PRE>&lt;SEC-HEADER&gt;{accession}.hdr.sgml : {day}\n"
        f"ACCESSION NUMBER:\t\t{accession}\n</PRE></BODY></HTML>\n"
    )


def sec_routes(upstream):
    j, t = upstream.json_file, upstream.text_file
    j(r"www\.sec\.gov/files/company_tickers\.json", "sec.company_tickers.json")
    j(r"www\.sec\.gov/files/company_tickers_mf\.json", "sec.company_tickers_mf.json")
    j(r"data\.sec\.gov/submissions/CIK0009999901\.json", "sec.submissions__TESTCO.json")
    j(r"data\.sec\.gov/submissions/CIK0009999950\.json", "sec.submissions__13F.json")
    j(r"data\.sec\.gov/submissions/CIK0009999970\.json", "sec.submissions__FUND.json")
    j(r"data\.sec\.gov/api/xbrl/companyfacts/CIK0009999901\.json", "sec.company_facts__TESTCO.json")
    # EVNTCO: the material 8-Ks (filings_events), their index pages and primary documents
    j(r"data\.sec\.gov/submissions/CIK0009999903\.json", "sec.submissions__EVNTCO.json")
    for acc, doc in (("26-000011", "evntco-20260106"), ("25-000040", "evntco-20251103")):
        folder = f"{ARCH}/9999903/0009999903{acc.replace('-', '')}"
        t(rf"{folder}/0009999903-{acc}-index\.htm", f"sec.8k_index__EVNTCO_{acc}.htm")
        t(rf"{folder}/{doc}\.htm", f"sec.8k_primary__EVNTCO_{acc}.htm")
    # PVPCO: its two proxies (folder listing, XBRL instance)
    j(r"data\.sec\.gov/submissions/CIK0009999904\.json", "sec.submissions__PVPCO.json")
    for acc, doc in (("26-000007", "pvpco-def14a-2026"), ("25-000006", "pvpco-def14a-2025")):
        folder = f"{ARCH}/9999904/0009999904{acc.replace('-', '')}"
        j(rf"{folder}/index\.json", f"sec.pvp_index__{acc}.json")
        t(rf"{folder}/{doc}_htm\.xml", f"sec.pvp_instance__{acc}.xml")
    # the frames API (one synthetic frame) and EDGAR's quarterly XBRL index: 2026 Q3 only
    upstream.add(r"data\.sec\.gov/api/xbrl/frames/.*", httpx.Response(404))  # the latest route wins
    j(
        r"data\.sec\.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2026Q2\.json",
        "sec.frames__Revenues_USD_CY2026Q2.json",
    )
    j(
        r"data\.sec\.gov/api/xbrl/frames/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax/USD/CY2026Q2\.json",
        "sec.frames__RevenueFromContractWithCustomerExcludingAssessedTax_USD_CY2026Q2.json",
    )
    upstream.add(r"www\.sec\.gov/Archives/edgar/full-index/\d{4}/QTR\d/index\.json", httpx.Response(404))
    j(r"www\.sec\.gov/Archives/edgar/full-index/2026/QTR3/index\.json", "sec.full_index__2026_QTR3.json")
    upstream.add(
        r"www\.sec\.gov/Archives/edgar/full-index/2026/QTR3/xbrl\.gz",
        lambda _r: gzip.compress(fixture_text("sec.xbrl_index__2026_QTR3.idx").encode("latin-1"), mtime=0),
    )
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
    # every filing's index headers (after the gone release's folder, whose headers stay)
    upstream.add(HEADERS, index_headers)
    # N-PORT documents
    for acc in ("26-000005", "26-000002"):
        t(rf"{ARCH}/9999970/0009999970{acc.replace('-', '')}/primary_doc\.xml", f"sec.nport_doc__{acc}.xml")


@pytest.fixture(autouse=True)
def _fresh_xbrl_index(monkeypatch, tmp_path):
    """marketlens-data keeps each quarter's XBRL index read in its process (and a
    closed quarter on disk): each test's EDGAR answers its own."""
    from omni.sources import sec_xbrl_frames

    monkeypatch.setattr(sec_xbrl_frames, "_index_memo", {})
    monkeypatch.setattr(
        sec_xbrl_frames, "_index_file", lambda y, q: tmp_path / "xbrl_index" / f"{y}Q{q}.json"
    )


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


def test_unreadable_edgar_index_headers_fail_the_answer_rather_than_stamp_early(tmp_path, edgar):
    """Without the filings' index headers the filings EDGAR just added would read four
    or five hours early (omni review of 2026-10-09): the refresh fails as retryable,
    and nothing is stored to answer from."""
    edgar.add(HEADERS, httpx.Response(503, text="busy"))
    err = refused("sec_filings", context(tmp_path), ticker="TESTCO")
    assert err.code == "data_unavailable" and err.retryable
    assert "sec_headers_unreadable" in err.message


#: What every SEC answer in mode auto says while SEC_CONTACT_EMAIL is not set (never a value).
NO_CONTACT = (
    "SEC_CONTACT_EMAIL is not set, so SEC EDGAR requests were sent with a placeholder contact in the "
    "User-Agent; SEC may throttle or block them. Set SEC_CONTACT_EMAIL to your email address in the env "
    "block of this server in your MCP client's configuration."
)
SEC_ARGS = {
    "sec_filings": {"ticker": "TESTCO"},
    "sec_xbrl_facts": {"ticker": "TESTCO"},
    "sec_fundamentals": {"ticker": "TESTCO"},
    "sec_earnings_releases": {"ticker": "TESTCO"},
    "sec_earnings_figures": {"ticker": "TESTCO"},
    "filings_events": {"ticker": "EVNTCO"},
    "sec_xbrl_frames": {"tag": "Revenues", "period": "CY2026Q2"},
    "sec_pay_vs_performance": {"ticker": "PVPCO"},
    "sec_insider_trades": {"ticker": "TESTCO"},
    "sec_13f_holdings": {"filer_cik": "9999950"},
    "sec_fund_nport": {"ticker": "TFUND"},
    "sec_fund_holdings": {"ticker": "TFUND"},
}


def test_sec_tools_run_without_a_contact_email(tmp_path, upstream):
    # as marketlens-data does on its own: its placeholder contact is sent, and the answer says so
    from omni.sources import sec as omni_sec

    sec_routes(upstream)
    # reading only what is stored sends nothing, so it needs no contact and carries no such note
    out = call("sec_filings", context(tmp_path, settings={"mode": "local"}), ticker="TESTCO")
    assert out.rows == [] and out.notes == [
        "mode is local and nothing is stored for this request; set providers.data.mode: auto to fetch it"
    ]
    assert upstream.requests == []
    out = call("sec_filings", context(tmp_path), ticker="TESTCO")
    assert len(out.rows) == 8 and out.notes == [NO_CONTACT]
    agents = {r.headers["User-Agent"] for r in upstream.requests}
    assert agents and all(omni_sec._PLACEHOLDER_CONTACT in a for a in agents)
    # within the TTL nothing is fetched again; the answer still says the contact is not set
    again = call("sec_filings", context(tmp_path), ticker="TESTCO")
    assert again.provenance.cached and again.notes == [NO_CONTACT]


def test_every_sec_answer_notes_the_missing_contact(tmp_path, upstream):
    sec_routes(upstream)
    for name, args in SEC_ARGS.items():
        out = call(name, context(tmp_path), **args)
        assert out.rows and out.notes[-1] == NO_CONTACT, name
        assert out.notes.count(NO_CONTACT) == 1, name


def test_sec_tools_refuse_a_contact_that_is_not_an_email(tmp_path, upstream, monkeypatch):
    # unchanged: a value that is set is sent as the contact, so it must be an email address
    sec_routes(upstream)
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "not-an-email")
    err = refused("sec_filings", context(tmp_path), ticker="TESTCO")
    assert err.code == "sec_contact_missing"
    assert err.message == (
        "SEC EDGAR asks every client to name a contact email in its User-Agent: set SEC_CONTACT_EMAIL "
        "(for example you@example.com) in this server's environment."
    )
    assert upstream.requests == []


def test_sec_unknown_ticker_is_not_found(tmp_path, edgar):
    err = refused("sec_filings", context(tmp_path), ticker="ZZZZ")
    assert (err.code, err.message) == ("data_not_found", "SEC EDGAR has no filer for ZZZZ.")


# --- material filing events ---------------------------------------------------------------------

EVNTCO_FOLDER = "https://www.sec.gov/Archives/edgar/data/9999903/000999990326000011"


def test_filings_events_golden(tmp_path, edgar):
    out = call("filings_events", context(tmp_path), ticker="evntco")
    doc = check_golden(out, "filings_events")
    # newest first; the earnings release (2.02, 9.01) and the 10-Q give no row
    assert [(r["accession_number"], r["item"]) for r in doc["rows"]] == [
        ("0009999903-26-000011", "1.01"),
        ("0009999903-26-000011", "3.02"),
        ("0009999903-25-000040", "5.02"),
    ]
    first = doc["rows"][0]
    assert first["item_title"] == "Entry into a Material Definitive Agreement"
    assert first["headline"].startswith("On January 6, 2026, Eventco Synthetic Inc")
    assert first["accepted_at"] == "2026-01-06T21:40:00Z" and first["filing_date"] == "2026-01-07"
    assert first["exhibits"] == {
        "EX-4.1": f"{EVNTCO_FOLDER}/evntco-ex41.htm",
        "EX-10.1": f"{EVNTCO_FOLDER}/evntco-ex101.htm",
        "EX-99.1": f"{EVNTCO_FOLDER}/evntco-ex991.htm",
    }
    assert first["index_url"] == f"{EVNTCO_FOLDER}/0009999903-26-000011-index.htm"
    officer = doc["rows"][2]
    assert officer["exhibits"] == {} and officer["headline"].startswith("On November 3, 2025")
    assert doc["provenance"]["dataset"] == "sec.submissions,sec.filing_events"
    assert doc["provenance"]["authority"] == ["official"]
    assert SPECS["filings_events"].output_risk == "external_text"  # the headline is the filer's prose
    # the index pages and primary documents were read once, with the contact in the User-Agent
    archive = [
        str(r.url) for r in edgar.requests if "/Archives/" in str(r.url) and "index-headers" not in str(r.url)
    ]
    assert sorted(archive) == sorted(
        [
            f"{EVNTCO_FOLDER}/0009999903-26-000011-index.htm",
            f"{EVNTCO_FOLDER}/evntco-20260106.htm",
            f"{EVNTCO_FOLDER.replace('000999990326000011', '000999990325000040')}/0009999903-25-000040-index.htm",
            f"{EVNTCO_FOLDER.replace('000999990326000011', '000999990325000040')}/evntco-20251103.htm",
        ]
    )
    assert all(KEYS["SEC_CONTACT_EMAIL"] in r.headers["User-Agent"] for r in edgar.requests)


def test_filings_events_filters(tmp_path, edgar):
    ctx = context(tmp_path)
    warrant = call("filings_events", ctx, ticker="EVNTCO", items=["3.02", "1.02"])
    assert [(r.accession_number, r.item) for r in warrant.rows] == [("0009999903-26-000011", "3.02")]
    assert warrant.provenance.request["items"] == ["3.02", "1.02"]
    since = call("filings_events", ctx, ticker="EVNTCO", since="2026-01-07")
    assert {r.accession_number for r in since.rows} == {"0009999903-26-000011"}
    assert since.provenance.request["since"] == "2026-01-07"
    assert call("filings_events", ctx, ticker="EVNTCO", since="2026-01-08").rows == []
    one = call("filings_events", ctx, ticker="EVNTCO", limit=1)
    assert [(r.item) for r in one.rows] == ["1.01"] and one.pagination is None
    before = call("filings_events", ctx, ticker="EVNTCO", as_of="2026-01-06T21:39:00Z")
    assert [r.item for r in before.rows] == ["5.02"]
    # through the pipeline, as the server runs a call: an argument that does not validate is invalid_arguments
    import asyncio

    from marketlens_mcp import pipeline
    from marketlens_mcp.plugin_api import ToolError

    for bad in ({"items": ["earnings"]}, {"items": []}, {"limit": 0}, {"limit": 501}, {"since": "yesterday"}):
        with pytest.raises(ToolError) as info:
            asyncio.run(pipeline.execute(SPECS["filings_events"], ctx, {"ticker": "EVNTCO", **bad}))
        assert info.value.code == "invalid_arguments", bad


def test_a_filing_whose_documents_edgar_no_longer_serves_gives_no_event(tmp_path, edgar):
    """TESTCO's one material 8-K (5.02, no acceptance time in the feed) has no index
    page on EDGAR any more (404): marketlens-data records it and stores no row, so
    the answer is empty rather than invented."""
    edgar.add(rf"{ARCH}/9999901/000999990125000020/.*", httpx.Response(404))
    out = call("filings_events", context(tmp_path), ticker="TESTCO")
    assert out.rows == [] and out.notes == []
    assert out.provenance.dataset == "sec.submissions,sec.filing_events"


def test_a_large_events_answer_is_stored(tmp_path, edgar):
    import asyncio

    from marketlens_mcp.results.store import StoreLimits, StoreRoot
    from marketlens_mcp.testing import call_tool, make_context

    store = StoreRoot(tmp_path / "results", StoreLimits(inline_max_rows=2)).session("s")
    ctx = make_context(tool="filings_events", store=store, now=NOW, capabilities=CAPABILITIES)
    marker = asyncio.run(call_tool(SPECS["filings_events"], ctx, ticker="EVNTCO"))
    assert marker.kind == "stored" and marker.model == "marketlens.FilingEvent"
    assert marker.row_count == 3 and marker.preview.time_column == "accepted_at"
    assert marker.provenance.dataset == "sec.submissions,sec.filing_events"


# --- XBRL frames ---------------------------------------------------------------------------------


def test_sec_xbrl_frames_golden(tmp_path, edgar):
    out = call("sec_xbrl_frames", context(tmp_path), tag="Revenues", period="cy2026q2")
    doc = check_golden(out, "sec_xbrl_frames")
    # by value, largest first; LATE's filing is in no XBRL index yet: held, no row
    assert [(r["cik"], r["ticker"], r["value"]) for r in doc["rows"]] == [
        ("0009999990", None, 800000000.0),
        ("0009999901", "TESTCO", 500000000.0),
        ("0009999903", "EVNTCO", 300000000.0),
    ]
    testco = doc["rows"][1]
    assert testco["stamp"] == "filing_date" and testco["filing_date"] == "2026-08-05"
    # the end of the filing date in New York (EDT), never its start
    assert testco["knowledge_time"] == "2026-08-06T03:59:59.999999Z"
    assert doc["rows"][2]["period_end"] == "2026-06-27"  # the filer's own quarter, as the SEC placed it
    assert doc["provenance"]["dataset"] == "sec.xbrl_frames"
    assert doc["provenance"]["authority"] == ["official"]
    assert doc["provenance"]["request"]["unit"] == "USD"
    index = [str(r.url) for r in edgar.requests if "full-index" in str(r.url)]
    assert "https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/xbrl.gz" in index
    assert all(KEYS["SEC_CONTACT_EMAIL"] in r.headers["User-Agent"] for r in edgar.requests)


def test_sec_xbrl_frames_filters_and_refusals(tmp_path, edgar):
    ctx = context(tmp_path)
    peers = call(
        "sec_xbrl_frames",
        ctx,
        tag="Revenues",
        period="CY2026Q2",
        tickers=["evntco", "testco", "othrco"],
        sort="asc",
    )
    assert [(r.ticker, r.value) for r in peers.rows] == [("EVNTCO", 300000000.0), ("TESTCO", 500000000.0)]
    assert peers.notes == ["No Revenues value for CY2026Q2 in the frame for: OTHRCO."]
    top = call("sec_xbrl_frames", ctx, tag="Revenues", period="CY2026Q2", limit=1)
    assert [r.cik for r in top.rows] == ["0009999990"]
    before = call("sec_xbrl_frames", ctx, tag="Revenues", period="CY2026Q2", as_of="2026-08-06T00:00:00Z")
    assert before.rows == []  # every filing of the frame was dated later
    # a frame the SEC does not have: no row
    assert call("sec_xbrl_frames", ctx, tag="Revenues", period="CY2026Q3").rows == []
    import asyncio

    from marketlens_mcp import pipeline
    from marketlens_mcp.plugin_api import ToolError

    for bad in (
        {"period": "2026Q2"},
        {"period": "CY2026Q5"},
        {"tag": "Bad/Tag"},
        {"limit": 0},
        {"unit": "US D"},
        {"taxonomy": "gaap"},
    ):
        with pytest.raises(ToolError) as info:
            asyncio.run(
                pipeline.execute(
                    SPECS["sec_xbrl_frames"], ctx, {"tag": "Revenues", "period": "CY2026Q2", **bad}
                )
            )
        assert info.value.code == "invalid_arguments", bad


def test_sec_xbrl_frames_revenue_alias(tmp_path, edgar):
    """Per filer revenue from contracts, else Revenues; a Revenues larger by more
    than 0.5% leads (TESTCO: 500m beside 480m); within it the part keeps the lead
    (EVNTCO: 300m beside 299m)."""
    out = call("sec_xbrl_frames", context(tmp_path), tag="revenue", period="CY2026Q2")
    by = {r.cik: (r.tag, r.value) for r in out.rows}
    rfc = "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert by == {
        "0009999990": ("Revenues", 800000000.0),
        "0009999992": (rfc, 650000000.0),
        "0009999901": ("Revenues", 500000000.0),
        "0009999903": (rfc, 299000000.0),
    }
    assert [r.value for r in out.rows] == sorted((r.value for r in out.rows), reverse=True)
    assert out.notes[-1].startswith(
        "tag revenue: per filer RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    assert out.provenance.request["unit"] == "USD"
    peers = call("sec_xbrl_frames", context(tmp_path), tag="revenue", period="CY2026Q2", tickers=["EVNTCO"])
    assert [(r.ticker, r.tag) for r in peers.rows] == [("EVNTCO", rfc)]


# --- pay versus performance -----------------------------------------------------------------------


def test_sec_pay_vs_performance_golden(tmp_path, edgar):
    out = call("sec_pay_vs_performance", context(tmp_path), ticker="pvpco")
    doc = check_golden(out, "sec_pay_vs_performance")
    rows = {(r["fiscal_year"], r["measure"], r["executive"]): r for r in doc["rows"]}
    # FY2025 had two PEOs, tagged per person; the filer named one of them
    assert rows[(2025, "peo_total_comp", "AlphaMember")]["value"] == 4500000.0
    assert rows[(2025, "peo_total_comp", "AlphaMember")]["executive_name"] == "Ms. Alpha"
    assert rows[(2025, "peo_comp_actually_paid", "BetaMember")]["value"] == -250000.0
    assert rows[(2025, "peo_comp_actually_paid", "BetaMember")]["executive_name"] is None
    # FY2024 as the 2026 proxy restates it (the TSR on its own base), one PEO, untagged
    assert rows[(2024, "company_tsr", None)]["value"] == 110.2
    assert rows[(2024, "company_tsr", None)]["accession_number"] == "0009999904-26-000007"
    assert rows[(2024, "peo_total_comp", None)]["executive_name"] == "Ms. Alpha"
    assert rows[(2025, "company_selected_measure", None)]["measure_name"] == "Revenue"
    assert rows[(2025, "net_income", None)]["concept"] == "us-gaap:NetIncomeLoss"
    assert {r["measure"] for r in doc["rows"]} == {
        "peo_total_comp",
        "peo_comp_actually_paid",
        "non_peo_neo_avg_total_comp",
        "non_peo_neo_avg_comp_actually_paid",
        "company_tsr",
        "peer_group_tsr",
        "net_income",
        "company_selected_measure",
    }
    assert [r["fiscal_year"] for r in doc["rows"]] == sorted(
        (r["fiscal_year"] for r in doc["rows"]), reverse=True
    )
    assert rows[(2025, "peo_total_comp", "AlphaMember")]["knowledge_time"] == "2026-03-20T20:00:00Z"
    assert doc["provenance"]["dataset"] == "sec.submissions,sec.pay_vs_performance"
    assert SPECS["sec_pay_vs_performance"].output_risk == "external_text"  # names are the filer's text


def test_sec_pay_vs_performance_as_of_and_measures(tmp_path, edgar):
    ctx = context(tmp_path)
    tsr = call("sec_pay_vs_performance", ctx, ticker="PVPCO", measures=["company_tsr"])
    assert [(r.fiscal_year, r.value) for r in tsr.rows] == [(2025, 131.4), (2024, 110.2), (2023, 100.0)]
    before = call(
        "sec_pay_vs_performance", ctx, ticker="PVPCO", measures=["company_tsr"], as_of="2026-01-01T00:00:00Z"
    )
    assert [(r.fiscal_year, r.value) for r in before.rows] == [(2024, 121.0), (2023, 100.0)]
    # the DEFA14A (additional material) and the 10-K are no proxy: only the two folders were read
    folders = {
        str(r.url).rsplit("/", 1)[0]
        for r in edgar.requests
        if "/9999904/" in str(r.url) and str(r.url).endswith(("/index.json", "_htm.xml"))
    }
    assert folders == {
        f"https://www.sec.gov/Archives/edgar/data/9999904/0009999904{a}" for a in ("26000007", "25000006")
    }


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
    # a Form 144 states 2,000 shares worth 101,000 and no price: the stored 50.50 is
    # the value over the shares, no price anyone traded at
    assert (notice["shares"], notice["price_per_share"]) == (2000.0, None)
    assert notice["absent"] == {
        "insider_cik": "not_provided_by_source",
        "acquired_disposed": "not_applicable",
        "price_per_share": "not_applicable",
        "shares_owned_after": "not_applicable",
    }
    # the 2025 Form 4 is before marketlens-data's insider history floor: never fetched
    assert not any("000999990125000030" in str(r.url) for r in edgar.requests)
    only4 = call("sec_insider_trades", context(tmp_path), ticker="TESTCO", forms=["4"])
    assert {r.form for r in only4.rows} == {"4"}


def test_sec_insider_trades_leaves_out_holding_lines(tmp_path, edgar):
    # marketlens-data stores the holdings a Form 4 lists without a transaction (a
    # trust's shares, an option) beside its transactions, as row_kind "holding"
    # rows with no code, shares or price: no trade, so no row here
    import omni

    out = call("sec_insider_trades", context(tmp_path), ticker="TESTCO", forms=["4"])
    stored = omni.query("sec_insider.transactions", mode="local", ticker="TESTCO")
    form4 = stored[stored["form"] == "4"]
    assert list(form4["row_kind"]) == ["transaction", "transaction", "holding", "holding"]
    assert [(r.line, r.transaction_code) for r in out.rows] == [(1, "S"), (2, "M")]


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


def amending_manager_routes(upstream):
    """Manager 9999951's report for 2026-03-31: the original (May 10), a new-holdings
    amendment (May 15), a restatement (May 20), then a new-holdings amendment (Jun 1)."""
    upstream.json_file(r"data\.sec\.gov/submissions/CIK0009999951\.json", "sec.submissions__13F_amends.json")
    # The filings' index headers (marketlens-data reads them for the filings added
    # since EDGAR last rewrote a feed; a fetch without them fails).
    upstream.add(HEADERS, index_headers)
    documents = {
        "26-000001": ("sec.13f_cover__26-000002.xml", "sec.13f_table__26-000002.xml"),
        "26-000002": ("sec.13f_cover__new_holdings.xml", "sec.13f_table__9999951-26-000002.xml"),
        "26-000003": ("sec.13f_cover__26-000004.xml", "sec.13f_table__26-000004.xml"),
        "26-000004": ("sec.13f_cover__new_holdings.xml", "sec.13f_table__9999951-26-000004.xml"),
    }
    for acc, (cover, table) in documents.items():
        folder = f"{ARCH}/9999951/0009999951{acc.replace('-', '')}"
        upstream.json_file(rf"{folder}/index\.json", "sec.13f_index__9999951.json")
        upstream.text_file(rf"{folder}/infotable\.xml", table)
        upstream.text_file(rf"{folder}/primary_doc\.xml", cover)


def test_sec_13f_new_holdings_filed_after_a_restatement_add_to_it(tmp_path, upstream, keys):
    amending_manager_routes(upstream)
    out = call("sec_13f_holdings", context(tmp_path), filer_cik="9999951")
    # the original and the new-holdings amendment filed before the restatement are replaced
    assert [(r.accession_number, r.amendment_type, r.cusip, r.value_usd) for r in out.rows] == [
        ("0009999951-26-000003", "RESTATEMENT", "99999T101", 4500000.0),
        ("0009999951-26-000004", "NEW HOLDINGS", "99999X202", 250000.0),
    ]
    assert out.notes == [
        "A restatement (0009999951-26-000003) replaces the earlier 13F-HR filings for 2026-03-31; the "
        "new-holdings amendment filed after it (0009999951-26-000004) adds to it."
    ]
    # before the later amendment was filed, the restatement stands alone
    before = call("sec_13f_holdings", context(tmp_path), filer_cik="9999951", as_of="2026-05-25")
    assert [(r.accession_number, r.cusip) for r in before.rows] == [("0009999951-26-000003", "99999T101")]
    assert before.notes == [
        "A restatement (0009999951-26-000003) replaces the earlier 13F-HR filings for 2026-03-31."
    ]


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
