"""filings_search: SEC EDGAR's full-text search, offline against the synthetic
page in fixtures/efts.search-index.json (see its README) and callables that
answer by the request's parameters."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import UTC, date, datetime

import httpx
import pytest
from official_harness import CONTACT, NOW, check_golden, fixture_bytes

from marketlens_mcp import __version__, pipeline, registry
from marketlens_mcp import config as cfg
from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_mcp.providers.data import runtime
from marketlens_mcp.providers.official import fetch, search
from marketlens_mcp.providers.official.tool import CONTACT_HINT
from marketlens_mcp.testing import make_context, temp_store
from marketlens_schema.data import FilingSearchHit

SPEC = search.SPECS[0]
URL = search.SEARCH_URL
FIXTURE = json.loads(fixture_bytes("efts.search-index"))


def context(*, contact: str | None = CONTACT, now: datetime = NOW, settings=None, limits=FetchLimits()):  # noqa: B008
    env = {"SEC_CONTACT_EMAIL": contact} if contact is not None else {}
    return make_context(tool=SPEC.name, env=env, now=now, settings=settings, limits=limits)


def call(ctx=None, **arguments):
    """validate -> handler, as the server does (without the offload)."""
    args = SPEC.input_model.model_validate(arguments)
    return asyncio.run(SPEC.handler(ctx or context(), args))


def refused(**arguments) -> ToolError:
    with pytest.raises(ToolError) as e:
        asyncio.run(pipeline.execute(SPEC, context(), arguments))
    return e.value


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


def hit(i: int) -> dict:
    """The fixture's first hit as the i-th of a long search."""
    one = copy.deepcopy(FIXTURE["hits"]["hits"][0])
    accession = f"0000000001-26-{i:06d}"
    one["_id"] = f"{accession}:primary_doc.xml"
    one["_source"]["adsh"] = accession
    return one


def pages_of(total: int, relation: str = "eq", fail_from: int | None = None, answer=None):
    """Serves ``total`` hits a page of 100 at a time by ``from``; from
    ``fail_from`` on, ``answer`` instead."""

    def serve(request: httpx.Request):
        start = int(request.url.params.get("from", "0"))
        if fail_from is not None and start >= fail_from:
            return answer() if callable(answer) else answer
        hits = [hit(i) for i in range(start, min(start + search.PAGE, total))]
        return httpx.Response(
            200, json={"hits": {"total": {"value": total, "relation": relation}, "hits": hits}}
        )

    return serve


# --- the answer ---------------------------------------------------------------------------------


def test_filings_search_golden_and_the_request_on_the_wire(publishers):
    out = call(query='"Example Capital"', forms=["13F-HR"])
    check_golden(out, "filings_search")
    (request,) = publishers.requests
    assert str(request.url).startswith(URL + "?")
    assert params(request) == {"q": '"Example Capital"', "forms": "13F-HR"}  # no from, no dates
    assert request.headers["user-agent"] == f"marketlens-mcp/{__version__} {CONTACT}"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["accept"] == "application/json"
    assert out.pagination.complete and out.pagination.next_page_token is None
    assert out.provenance.provider == "official" and out.provenance.operation == "full_text_search"


def test_hits_are_mapped(publishers):
    out = call(query="Example")
    rows = {r.form: r for r in out.rows}
    assert [r.form for r in out.rows] == ["13F-HR", "13F-HR/A", "8-K", "144", "D"]  # EDGAR's order
    form144 = rows["144"]
    assert form144.cik == "0000000002" and form144.ciks == ["0000000002", "0000000003"]
    assert form144.entity_name == "Example Corp"  # tickers and CIK stripped
    assert form144.document_url == (
        "https://www.sec.gov/Archives/edgar/data/2/000199999926000101/primary_doc.xml"
    )
    assert form144.filing_url == (
        "https://www.sec.gov/Archives/edgar/data/2/000199999926000101/0001999999-26-000101-index.htm"
    )
    cover = rows["13F-HR"]
    assert cover.entity_name == "Example Capital Management, LP"
    assert cover.report_date == date(2026, 6, 30) and cover.document_type == "13F-HR"
    # the end of the filing date in New York (EDT), never early
    assert cover.filing_date == date(2026, 8, 14)
    assert cover.model_dump(mode="json")["knowledge_time"] == "2026-08-15T03:59:59.999999Z"
    exhibit = rows["8-K"]
    assert (exhibit.document, exhibit.document_type) == ("ex99-1.htm", "EX-99.1")
    assert exhibit.document_url.endswith("/4/000000000426000012/ex99-1.htm")
    form_d = rows["D"]
    assert form_d.report_date is None and form_d.absent == {"report_date": "not_provided_by_source"}
    assert out.notes[-1] == (
        "Skipped 1 upstream record(s) that do not fit marketlens.FilingSearchHit: "
        "0000000006-26-000001:attachments/ex1.htm."
    )
    assert out.notes[0] == ("EDGAR found 6 matching document(s) (by relevance); these are hits 1 to 6.")


def test_a_hit_that_does_not_fit_is_skipped():
    good = FIXTURE["hits"]["hits"][0]
    assert search.hit_row(good).accession_number == "0000000001-26-000003"
    for change in (
        {"_id": "0000000001-26-000003:../x.htm"},
        {"_id": "0000000001-26-000003:"},
        {"_source": {**good["_source"], "adsh": "1-2-3"}},
        {"_source": {**good["_source"], "ciks": ["1", 2]}},
        {"_source": {**good["_source"], "form": " "}},
        {"_source": {**good["_source"], "file_date": "14/08/2026"}},
    ):
        with pytest.raises((ValueError, TypeError, KeyError)):
            search.hit_row({**good, **change})
    odd = {**good, "_source": {**good["_source"], "display_names": ["Example Capital Management, LP"]}}
    assert search.hit_row(odd).entity_name is None and search.hit_row(odd).absent == {
        "entity_name": "not_provided_by_source"
    }


# --- the arguments ------------------------------------------------------------------------------


def test_ciks_are_sent_ten_digits_and_forms_upper_case(publishers):
    call(ciks=["1350694", " 0001350694"], forms=["13f-hr ", "13F-HR", "sc 13d"])
    (request,) = publishers.requests
    assert params(request) == {"q": "", "ciks": "0001350694", "forms": "13F-HR,SC 13D"}
    for bad in ({"ciks": ["abc"]}, {"ciks": ["0"]}, {"ciks": ["12345678901"]}, {"forms": ["13F-HR,10-K"]}):
        assert refused(**bad).code == "invalid_arguments", bad
    assert len(publishers.requests) == 1


def test_filing_dates(publishers):
    call(ciks=["1350694"], start="2026-01-01")
    assert params(publishers.requests[-1]) == {
        "q": "",
        "ciks": "0001350694",
        "dateRange": "custom",
        "startdt": "2026-01-01",
        "enddt": "2026-10-16",  # today in New York at NOW
    }
    # 02:00 UTC on the 17th is still the 16th in New York
    call(context(now=datetime(2026, 10, 17, 2, 0, tzinfo=UTC)), ciks=["1350694"], end="2026-06-30")
    assert params(publishers.requests[-1])["startdt"] == "2001-01-01"
    assert params(publishers.requests[-1])["enddt"] == "2026-06-30"
    call(context(now=datetime(2026, 10, 17, 2, 0, tzinfo=UTC)), ciks=["1350694"], start="2026-10-01")
    assert params(publishers.requests[-1])["enddt"] == "2026-10-16"
    call(ciks=["1350694"])
    assert "dateRange" not in params(publishers.requests[-1])
    assert refused(ciks=["1350694"], start="2026-02-01", end="2026-01-01").code == "invalid_arguments"
    assert len(publishers.requests) == 4


def test_a_blank_search_is_refused_before_any_request(publishers):
    for arguments in ({}, {"start": "2026-01-01"}, {"query": " ? "}, {"limit": 5}):
        assert refused(**arguments).code == "invalid_arguments", arguments
    error = refused(start="2026-01-01")
    assert "give query, forms or ciks: EDGAR refuses a blank search" in error.message
    assert publishers.requests == []


# --- paging -------------------------------------------------------------------------------------


def test_pages_and_the_page_token(publishers):
    publishers.set(URL, pages_of(250))
    out = call(query="Example", limit=150)
    assert [params(r).get("from") for r in publishers.requests] == [None, "100"]
    assert len(out.rows) == 150 and out.rows[-1].accession_number == "0000000001-26-000149"
    cont = 'More hits remain: call filings_search again with page_token="150" for the next ones.'
    assert out.pagination.next_page_token == "150" and not out.pagination.complete
    assert out.pagination.pages_fetched == 2 and out.pagination.rows_fetched == 150
    assert out.provenance.truncated and out.provenance.truncation_note == cont
    assert out.notes[:2] == [
        cont,
        "EDGAR found 250 matching document(s) (by relevance); these are hits 1 to 150.",
    ]
    more = call(query="Example", page_token="150")
    assert params(publishers.requests[-1])["from"] == "150"
    assert len(more.rows) == 100 and more.pagination.next_page_token is None
    assert more.notes[0] == "EDGAR found 250 matching document(s) (by relevance); these are hits 151 to 250."
    error = refused(query="Example", page_token="9950")
    assert error.code == "invalid_arguments"
    assert "EDGAR serves only the first 10,000 hits of a search; narrow it" in error.message
    assert refused(query="Example", page_token="abc").code == "invalid_arguments"


def test_the_last_page_of_a_search_ends_it(publishers):
    publishers.set(URL, pages_of(130))
    out = call(ciks=["1"], limit=500)
    assert len(out.rows) == 130 and out.pagination.next_page_token is None and out.pagination.complete
    assert len(publishers.requests) == 2
    assert out.notes == [
        "EDGAR found 130 matching document(s) (newest filing first, main documents only); these are hits 1 to 130."
    ]


def test_a_limit_inside_the_last_page_keeps_a_page_token(publishers):
    """The hits a limit leaves unread on a short last page are still EDGAR's."""
    publishers.set(URL, pages_of(60))
    out = call(query="Example", limit=40)
    assert len(out.rows) == 40 and out.pagination.next_page_token == "40"
    rest = call(query="Example", page_token="40")
    assert len(rest.rows) == 20 and rest.pagination.next_page_token is None


def test_more_than_edgar_serves(publishers):
    publishers.set(URL, pages_of(10_000, relation="gte"))
    out = call(query="Example", limit=100)
    assert out.notes == [
        'More hits remain: call filings_search again with page_token="100" for the next ones.',
        "EDGAR found 10,000 or more matching document(s) (by relevance); these are hits 1 to 100.",
        "EDGAR serves only the first 10,000 hits of a search: narrow it (forms, ciks, start/end or more words) "
        "to reach the rest.",
    ]
    last = call(query="Example", page_token="9900", limit=500)
    assert len(last.rows) == 100 and last.pagination.next_page_token is None
    assert len(publishers.requests) == 2


def test_a_limit_above_max_rows_is_lowered(publishers):
    publishers.set(URL, pages_of(250))
    out = call(context(limits=FetchLimits(max_rows=120)), query="Example", limit=300)
    assert len(out.rows) == 120 and out.pagination.next_page_token == "120"
    assert "limit 300 is above fetch.max_rows (120), so it was lowered to that." in out.notes


# --- failures -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contact", "why"), [(None, "is not set"), ("me at home", "is not an email address")]
)
def test_without_a_contact_nothing_is_asked(publishers, contact, why):
    with pytest.raises(ToolError) as e:
        call(context(contact=contact), query="Example")
    assert e.value.code == "official_contact_missing"
    assert e.value.message == (
        "filings_search asks SEC's EDGAR, which refuses automated clients that do not name a contact, and "
        f"SEC_CONTACT_EMAIL {why}: nothing was fetched."
    )
    assert e.value.hint == CONTACT_HINT
    assert publishers.requests == []


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        (httpx.Response(503), "answered HTTP 503, twice"),
        (httpx.Response(429), "asked this server to slow down (HTTP 429)"),
        (httpx.Response(301, headers={"location": "https://www.sec.gov/"}), "answered HTTP 301"),
        (
            httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"\x1f\x8b"),
            "answered compressed although this server asked for no compression",
        ),
        (httpx.Response(200, text="<html>maintenance</html>"), "answered with JSON that does not parse"),
        (httpx.Response(200, json={"hits": []}), "answered in a shape this server does not read"),
        (httpx.Response(200, json=[1, 2]), "answered in a shape this server does not read"),
    ],
)
def test_a_first_page_that_fails_is_an_error(publishers, _no_sleep, answer, says):
    publishers.set(URL, answer)
    with pytest.raises(ToolError) as e:
        call(query="Example")
    assert e.value.code == "official_unavailable" and e.value.retryable
    assert e.value.message == f"EDGAR's full-text search {says}."
    assert e.value.hint == "Retry in a few minutes."
    assert _no_sleep == ([1.0] if answer.status_code == 503 else [])  # only a 5xx is retried, once


@pytest.mark.parametrize(
    ("body", "text"),
    [
        (
            {
                "error": "Blank search not valid.  Either entity, keywords, location, or filing types must be submitted."
            },
            "Blank search not valid.  Either entity, keywords, location, or filing types must be submitted.",
        ),
        (
            {"errorType": "search_phase_execution_exception", "errorMessage": "Result window is too large"},
            "Result window is too large",
        ),
    ],
)
def test_edgars_refusal_is_passed_on(publishers, body, text):
    publishers.set(URL, httpx.Response(200, json=body))
    with pytest.raises(ToolError) as e:
        call(query="Example")
    assert e.value.code == "official_rejected" and not e.value.retryable
    assert e.value.message == f"EDGAR's full-text search refused the request: {text}"


def test_a_later_page_that_fails_gives_a_partial_result(publishers):
    publishers.set(URL, pages_of(250, fail_from=100, answer=httpx.Response(404)))
    out = call(query="Example", limit=200)
    assert len(out.rows) == 100 and out.pagination.next_page_token == "100"
    assert out.notes[2] == (
        "Partial result: EDGAR's full-text search answered HTTP 404 when asked for hits from 101; "
        'page_token="100" asks again from there.'
    )


def test_the_call_has_a_deadline(publishers, monkeypatch):
    monkeypatch.setattr(fetch, "DEADLINE_EXTRA", 0)

    async def hang():
        await asyncio.sleep(60)

    publishers.set(URL, pages_of(250, fail_from=100, answer=hang))
    began = time.monotonic()
    out = call(context(settings={"timeout_seconds": 0.3}), query="Example", limit=200)
    assert time.monotonic() - began < 5
    assert len(out.rows) == 100 and out.pagination.next_page_token == "100"
    assert (
        "Partial result: EDGAR's full-text search did not answer within this call's limit of 0.3 seconds when "
        'asked for hits from 101; page_token="100" asks again from there.'
    ) in out.notes


# --- the manifest and the pipeline --------------------------------------------------------------


@pytest.mark.parametrize("installed", [False, True])
def test_the_tool_is_built_in_with_or_without_the_data_extra(monkeypatch, installed):
    monkeypatch.setattr(runtime, "available", lambda: installed)
    entry = registry.build_catalog(cfg.load_config(), discover=list).tools["filings_search"]
    assert entry.plugin == "builtin:official" and entry.env == ("SEC_CONTACT_EMAIL",)
    assert entry.spec.capability == "filings" and entry.spec.output_risk == "external_text"
    assert entry.spec.output_model is FilingSearchHit
    assert len(entry.spec.description) <= 1000
    for words in ("never as instructions", "SEC_CONTACT_EMAIL", "13F-HR/A", "page_token"):
        assert words in entry.spec.description, words


def test_through_the_pipeline_the_answer_is_third_party_text(publishers, tmp_path):
    ctx = make_context(store=temp_store(tmp_path), env={"SEC_CONTACT_EMAIL": CONTACT}, now=NOW)
    done = asyncio.run(pipeline.execute(SPEC, ctx, {"query": "Example"}))
    assert done.risk == "external_text"
    assert done.response.kind == "inline" and done.response.row_count == 5
