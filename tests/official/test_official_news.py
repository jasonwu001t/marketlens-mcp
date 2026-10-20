"""news_official end to end against the fake publishers: the golden outputs,
the SEC_CONTACT_EMAIL rule, dedupe, window, query and sort, partial and total
failure, the cache, redirects, the byte cap, and GET only."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from official_harness import CONTACT, NOW, SPEC, call, check_golden, context, fixture_bytes

from marketlens_mcp import __version__, builtins, pipeline, registry
from marketlens_mcp import config as cfg
from marketlens_mcp.plugin_api import FetchLimits, ToolError
from marketlens_mcp.providers.official import feeds, fetch, parse
from marketlens_mcp.providers.official.tool import doctor_line
from marketlens_mcp.testing import make_context, temp_store

SEC_BLS = [f for f in feeds.FEEDS if f.publisher in ("sec", "bls")]
URL = {f.id: f.url for f in feeds.FEEDS}


def row_by_headline(out, text):
    return next(r for r in out.rows if text in r.headline)


# --- golden ---------------------------------------------------------------------------------------


def test_news_official_golden_every_feed(publishers):
    out = call()
    doc = check_golden(out, "news_official")
    assert sorted(publishers.urls()) == sorted(f.url for f in feeds.FEEDS)  # each feed once
    agents = {str(r.url): r.headers["user-agent"] for r in publishers.requests}
    for feed in feeds.FEEDS:
        if feed.publisher in ("sec", "bls"):
            assert agents[feed.url] == f"marketlens-mcp/{__version__} {CONTACT}", feed.id
        else:
            assert agents[feed.url].startswith("marketlens-mcp/") and CONTACT not in agents[feed.url], feed.id
    assert {r.headers["accept-encoding"] for r in publishers.requests} == {"identity"}
    prov = doc["provenance"]
    assert prov["provider"] == "official" and prov["operation"] == "feeds" and prov["route"] == "GET RSS/Atom"
    assert prov["dataset"] == ",".join(feeds.FEED_IDS) and prov["cached"] is False
    assert prov["as_of"] == "2026-10-16T20:00:00Z" and prov["request"]["start"] == "2026-10-09T20:00:00Z"
    assert doc["absent"]["content"]["code"] == "not_provided_by_source"
    times = [r["created_at"] for r in doc["rows"]]
    assert times == sorted(times, reverse=True)  # newest first by default
    assert all(r["tickers"] == [] and r["content"] is None for r in doc["rows"])


def test_news_official_golden_without_a_contact(publishers):
    out = call(context(contact=None))
    check_golden(out, "news_official__no_contact")
    assert not any(h in u for u in publishers.urls() for h in ("sec.gov", "bls.gov"))
    assert out.notes[0].startswith(
        "SEC_CONTACT_EMAIL is not set, so the SEC and BLS feeds were skipped (sec.press,"
    )
    assert {r.publisher for r in out.rows} == {"federal_reserve", "bea", "cftc", "ecb"}


# --- rows --------------------------------------------------------------------------------------


def test_rows_are_mapped_from_each_feed_shape(publishers):
    out = call(lookback="P30D")
    sec = row_by_headline(out, "Cherry-Picking")
    assert sec.headline == "SEC Charges Example Advisers in a Synthetic Cherry-Picking Scheme"
    assert sec.summary == (
        "The Securities and Exchange Commission today charged Example Advisers LLC in a synthetic case. "
        "Rates & fees…"
    )
    assert sec.news_id.startswith("sec.press:") and re.fullmatch(r"sec\.press:[0-9a-f]{16}", sec.news_id)
    assert sec.created_at.isoformat() == "2026-10-14T14:30:47+00:00"
    assert sec.author is None and sec.absent["author"] == "not_provided_by_source"
    edt = row_by_headline(out, "Synthetic Agenda")
    assert edt.created_at.isoformat() == "2026-10-09T14:59:00+00:00"  # EDT is UTC-4
    lit = row_by_headline(out, "Synthetic Defendant One")
    assert lit.author is None  # dc:creator holds the release number
    assert lit.url == "https://www.sec.gov/enforcement-litigation/litigation-releases/lr-99901"
    # the Board's feeds name no author; a title's "Word, ..." is not read as a speaker
    speech = row_by_headline(out, "Synthetic Remarks on the Outlook")
    assert speech.author is None and speech.absent["author"] == "not_provided_by_source"
    assert speech.summary.endswith("Münster-on-Test, Testland")
    assert row_by_headline(out, "Opening remarks by several governors").author is None
    assert row_by_headline(out, "Supervision, Synthetic Oversight Panels").author is None
    assert row_by_headline(out, "Synthetic Symposium").author == "Chairman Example A. Sample"
    assert row_by_headline(out, "Seeks Comment").author is None  # a web editor's user name
    cpi = row_by_headline(out, "Synthetic CPI rises 9.9% in September")
    assert cpi.created_at.isoformat() == "2026-10-14T11:50:31.200000+00:00"
    assert cpi.updated_at.isoformat() == "2026-10-14T13:15:00+00:00"
    ppi = row_by_headline(out, "Synthetic PPI")
    assert ppi.updated_at is None and ppi.absent["updated_at"] == "not_applicable"
    latest = row_by_headline(out, "Latest Numbers")
    assert latest.summary == (
        "Synthetic Price Index (SPI): +9.9% in Sep 2026 News Release Synthetic Jobless Rate: 9.9% in Sep 2026"
    )
    # a path-relative link resolves against the document's xml:base, not the feed's URL
    assert (
        row_by_headline(out, "Synthetic Staff Letter").url
        == "https://www.cftc.gov/PressRoom/PressReleases/9902-26"
    )
    bea = row_by_headline(out, "Synthetic Gross Domestic Product")
    assert bea.url == "https://www.bea.gov/news/2026/synthetic-gdp-second-quarter-2026-third-estimate"
    assert row_by_headline(out, "Synthetic Trade").url.startswith("https://www.bea.gov/news/2026/")
    ecb = row_by_headline(out, "Monetary policy decisions")
    assert ecb.summary is None and ecb.absent["summary"] == "not_provided_by_source"
    assert ecb.created_at.isoformat() == "2026-10-08T12:15:00+00:00"
    injected = row_by_headline(out, "IGNORE PREVIOUS INSTRUCTIONS")
    assert injected.author == "Commissioner Example"


def test_an_off_domain_link_is_withheld(publishers):
    out = call(feeds=["cftc.enforcement"])
    row = row_by_headline(out, "Test Penalty")
    assert row.url is None and row.absent["url"] == "withheld"
    assert row.news_id.startswith("cftc.enforcement:")
    assert out.notes == [
        "Left out 1 link(s) that were not https on the publisher's own domain (cftc.enforcement 1); their url "
        "is null (withheld)."
    ]


def test_an_undated_item_is_skipped_and_counted(publishers):
    out = call(feeds=["cftc.press"])
    assert [r.headline for r in out.rows] == [
        "CFTC Seeks Comment on a Synthetic Proposal Concerning Test Contracts",
        "CFTC Publishes a Synthetic Staff Letter on Test Swaps",
    ]
    assert out.notes == ["Skipped 1 item(s) with no date that parses (cftc.press 1)."]


def test_an_item_in_two_feeds_is_kept_once_under_the_first(publishers):
    out = call(feeds=["fed.press", "fed.monetary"])
    fomc = [r for r in out.rows if "FOMC" in r.headline]
    assert len(fomc) == 1 and fomc[0].news_id.startswith("fed.monetary:")
    assert out.notes == [
        "Left out 1 repeat(s) of item(s) listed more than once, in one feed or in several; each is kept once, "
        "under the first feed of the allow-list (fed.press 1)."
    ]
    alone = call(feeds=["fed.press"])
    assert [r for r in alone.rows if "FOMC" in r.headline][0].news_id.startswith("fed.press:")


def test_publishers_and_feeds_add_up_in_allow_list_order(publishers):
    out = call(feeds=["ecb.press"], publishers=["bea"])
    assert out.provenance.dataset == "bea.news,ecb.press"
    assert publishers.urls() == [URL["bea.news"], URL["ecb.press"]]


def test_window_query_and_sort(publishers):
    out = call(start="2026-10-09", end="2026-10-14", sort="asc", publishers=["federal_reserve"])
    assert [r.created_at.isoformat() for r in out.rows] == [
        "2026-10-09T14:00:00+00:00",
        "2026-10-12T13:30:00+00:00",
        "2026-10-13T14:00:00+00:00",
        "2026-10-14T18:00:00+00:00",
    ]
    assert out.notes[0].startswith("end 2026-10-14 is a date: the window includes that whole day")
    found = call(query="Interest  RATE", publishers=["federal_reserve", "ecb"], lookback="P30D")
    assert [r.headline for r in found.rows] == [
        "Agencies request comment on a synthetic interest rate risk proposal"
    ]
    assert (
        "query 'Interest RATE': 1 of the 9 item(s) in the window had every word in their headline or summary."
        in found.notes
    )
    assert found.provenance.request["query"] == "Interest RATE"


def test_start_and_lookback_together_are_refused(publishers):
    with pytest.raises(ToolError, match="Pass start or lookback, not both"):
        call(start="2026-10-01", lookback="P1D")
    with pytest.raises(ToolError) as e:
        call(start="2026-10-17")
    assert e.value.message == (
        "start (2026-10-17T00:00:00Z) is not before the newest data each feed serves (2026-10-16T20:00:00Z); "
        "pass an earlier start."
    )
    assert publishers.requests == []


# --- SEC_CONTACT_EMAIL -------------------------------------------------------------------------------


def test_a_contact_that_is_not_an_email_is_never_sent(publishers):
    out = call(context(contact="me at home"))
    assert not any(h in u for u in publishers.urls() for h in ("sec.gov", "bls.gov"))
    assert out.notes[0].startswith(
        "SEC_CONTACT_EMAIL is not an email address, so the SEC and BLS feeds were skipped"
    )
    assert all("me at home" not in r.headers["user-agent"] for r in publishers.requests)


def test_only_sec_and_bls_feeds_without_a_contact_is_an_error(publishers):
    with pytest.raises(ToolError) as e:
        call(context(contact=None), publishers=["sec", "bls"])
    assert e.value.code == "official_contact_missing" and "SEC_CONTACT_EMAIL is not set" in e.value.message
    assert "env block" in e.value.hint
    assert publishers.requests == []


def test_doctor_line():
    assert doctor_line({}) == (
        "official feeds: 9 of 18 ready; SEC_CONTACT_EMAIL is not set, so the SEC and BLS feeds are skipped "
        "(set it to your email address: SEC and BLS ask automated clients to name a contact)"
    )
    assert "is not an email address" in doctor_line({"SEC_CONTACT_EMAIL": "nope"})
    assert doctor_line({"SEC_CONTACT_EMAIL": CONTACT}).startswith("official feeds: all 18 ready")


# --- failures -----------------------------------------------------------------------------------


def test_a_failed_feed_gives_a_partial_result(publishers, _no_sleep):
    publishers.set(URL["fed.press"], httpx.Response(503))
    publishers.set(URL["ecb.press"], httpx.Response(404))
    out = call(publishers=["federal_reserve", "ecb"])
    assert out.notes[-1] == (
        "Partial result: fed.press answered HTTP 503, twice; ecb.press answered HTTP 404. The other feeds answered."
    )
    assert _no_sleep == [1.0]  # the 5xx was retried once, the 404 not at all
    assert out.provenance.dataset == "fed.monetary,fed.speeches,fed.testimony"
    assert out.rows


def test_every_feed_failing_is_an_error(publishers):
    publishers.set(URL["ecb.press"], httpx.Response(200, text="<html><body>maintenance</body></html>"))
    publishers.set(URL["bea.news"], httpx.Response(429))
    with pytest.raises(ToolError) as e:
        call(publishers=["ecb", "bea"])
    assert e.value.code == "official_unavailable" and e.value.retryable
    assert e.value.message == (
        "Every feed asked for failed: bea.news asked this server to slow down (HTTP 429); ecb.press is not an "
        "RSS 2.0 or Atom feed."
    )


def test_a_timeout_or_a_lost_connection_fails_the_feed_at_once(publishers, _no_sleep):
    """As the Alpaca client: no retry on a timeout or a connection failure (only on a 5xx)."""

    def timeout(request):
        raise httpx.ConnectTimeout("synthetic", request=request)

    def refused(request):
        raise httpx.ConnectError("synthetic", request=request)

    publishers.set(URL["ecb.press"], timeout, httpx.Response(200, content=fixture_bytes("ecb.press")))
    publishers.set(URL["bea.news"], refused, httpx.Response(200, content=fixture_bytes("bea.news")))
    out = call(feeds=["ecb.press", "bea.news", "fed.press"])
    assert out.notes[-1] == (
        "Partial result: bea.news could not be reached (ConnectError); ecb.press did not answer within 20 "
        "seconds. The other feeds answered."
    )
    assert _no_sleep == [] and publishers.urls().count(URL["ecb.press"]) == 1


def test_a_declaration_in_a_feed_is_refused(publishers):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    publishers.set(URL["fed.press"], httpx.Response(200, content=bomb))
    out = call(feeds=["fed.press", "fed.speeches"])
    assert out.notes == [
        "Partial result: fed.press declares a DOCTYPE or an ENTITY, which this server refuses. The other feeds "
        "answered."
    ]


# --- transport rules ----------------------------------------------------------------------------


def test_a_redirect_on_the_publishers_host_is_followed(publishers):
    moved = "https://www.federalreserve.gov/feeds/press_all_moved.xml"
    publishers.set(URL["fed.press"], httpx.Response(301, headers={"Location": "/feeds/press_all_moved.xml"}))
    publishers.set(moved, httpx.Response(200, content=fixture_bytes("fed.press")))
    out = call(feeds=["fed.press"])
    assert publishers.urls() == [URL["fed.press"], moved] and len(out.rows) == 2


@pytest.mark.parametrize(
    ("location", "shown"),
    [
        ("https://evil.example.net/feed.xml", "evil.example.net"),
        ("http://www.federalreserve.gov/feeds/press_all.xml", "www.federalreserve.gov"),  # not https
        ("https://www.sec.gov/news/pressreleases.rss", "www.sec.gov"),  # another publisher's host
        ("https://www.federalreserve.gov:8443/x", "www.federalreserve.gov"),
    ],
)
def test_no_redirect_off_the_allow_list(publishers, location, shown):
    publishers.set(URL["fed.press"], httpx.Response(302, headers={"Location": location}))
    out = call(feeds=["fed.press", "fed.speeches"])
    assert publishers.urls() == [URL["fed.press"], URL["fed.speeches"]]  # the redirect was not followed
    assert out.notes == [
        f"Partial result: fed.press redirected to {shown}, which is not on the allow-list: not followed. The "
        "other feeds answered."
    ]


def test_the_byte_cap(publishers):
    big = b"<rss><channel>" + b" " * fetch.MAX_BYTES + b"</channel></rss>"
    publishers.set(URL["fed.press"], httpx.Response(200, content=big))

    async def chunks():
        for _ in range(6):
            yield b" " * (1024 * 1024)

    publishers.set(URL["fed.speeches"], lambda request: httpx.Response(200, content=chunks()))
    out = call(publishers=["federal_reserve"])
    assert out.notes == [
        "Partial result: fed.press is larger than 5 MB, the most this server reads; fed.speeches is larger than "
        "5 MB, the most this server reads. The other feeds answered."
    ]


def test_a_compressed_answer_is_refused(publishers):
    gzip = httpx.Response(200, stream=httpx.ByteStream(b"\x1f\x8b..."), headers={"Content-Encoding": "gzip"})
    publishers.set(URL["ecb.press"], gzip)
    with pytest.raises(
        ToolError, match="ecb.press answered compressed although this server asked for no compression"
    ):
        call(feeds=["ecb.press"])


def test_the_cache_serves_a_repeat_within_its_ttl(publishers):
    first = call(feeds=["ecb.press"])
    again = call(context(now=NOW + timedelta(seconds=299)), feeds=["ecb.press"])
    assert len(publishers.requests) == 1
    assert again.provenance.cached is True and again.provenance.pages_fetched == 0
    assert first.provenance.cached is False and first.provenance.pages_fetched == 1
    assert [r.news_id for r in again.rows] == [r.news_id for r in first.rows]
    # a cached answer is as old as the read that filled the cache, not the call
    assert first.provenance.fetched_at == NOW and again.provenance.fetched_at == NOW
    assert again.notes == [
        "1 of 1 feed(s) came from this server's cache, fetched within the last few minutes: ecb.press."
    ]
    call(context(now=NOW + timedelta(seconds=300)), feeds=["ecb.press"])
    assert len(publishers.requests) == 2  # expired
    call(context(settings={"cache_seconds": 0}), feeds=["fed.press"])
    call(context(settings={"cache_seconds": 0}), feeds=["fed.press"])
    assert len(publishers.requests) == 4  # cache off


def test_the_ecbs_crawl_delay_holds_with_the_cache_off(publishers, monkeypatch):
    """robots.txt asks for 5 s between requests: a repeat within 5 s is answered
    from the cache whatever cache_seconds says, and the host allows 12 a minute."""
    rates = {}
    real = fetch._answer

    async def spy(ctx, http, url, agent, timeout, rate):
        rates[url] = rate
        return await real(ctx, http, url, agent, timeout, rate)

    monkeypatch.setattr(fetch, "_answer", spy)
    off = {"cache_seconds": 0}
    call(context(settings=off), feeds=["ecb.press", "fed.press"])
    again = call(context(now=NOW + timedelta(seconds=4), settings=off), feeds=["ecb.press"])
    assert again.provenance.cached is True
    call(context(now=NOW + timedelta(seconds=5), settings=off), feeds=["ecb.press"])
    assert sorted(publishers.urls()) == [URL["ecb.press"], URL["ecb.press"], URL["fed.press"]]
    assert rates == {URL["ecb.press"]: 12, URL["fed.press"]: fetch.PER_MINUTE}


def test_a_failed_feed_is_not_cached(publishers):
    publishers.set(
        URL["ecb.press"], httpx.Response(404), httpx.Response(200, content=fixture_bytes("ecb.press"))
    )
    with pytest.raises(ToolError):
        call(feeds=["ecb.press"])
    assert len(call(feeds=["ecb.press"]).rows) == 1


def test_the_provider_sends_only_get_requests():
    root = Path(fetch.__file__).parent
    calls = re.compile(
        r"\.(post|put|patch|delete|request|stream)\(|\b(send|build_request)\(|httpx\.Request\("
    )
    methods = re.compile(r"""["'](POST|PUT|PATCH|DELETE)["']""")
    offenders = [
        f"{p.name}:{i}: {line.strip()}"
        for p in sorted(root.rglob("*.py"))
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if calls.search(line) or methods.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_fake_publishers_answer_get_only(method):
    from official_harness import FakePublishers

    fake = FakePublishers()

    async def send():
        async with httpx.AsyncClient(transport=fake.transport) as http:
            return await http.request(method, URL["ecb.press"])

    assert asyncio.run(send()).status_code == 405
    assert fake.non_get() == [f"{method} {URL['ecb.press']}"]


# --- the manifest and the pipeline ----------------------------------------------------------------


def test_unknown_feed_or_publisher_is_refused(publishers):
    for arguments in ({"feeds": ["fed.nope"]}, {"publishers": ["treasury"]}, {"feeds": []}, {"url": "x"}):
        with pytest.raises(ToolError) as e:
            asyncio.run(pipeline.execute(SPEC, context(), arguments))
        assert e.value.code == "invalid_arguments", arguments
    assert publishers.requests == []


def test_through_the_pipeline_the_answer_is_third_party_text(publishers, tmp_path):
    ctx = make_context(store=temp_store(tmp_path), env={"SEC_CONTACT_EMAIL": CONTACT}, now=NOW)
    done = asyncio.run(pipeline.execute(SPEC, ctx, {"feeds": ["ecb.press"]}))
    assert done.risk == "external_text"
    assert done.response.kind == "inline" and done.response.row_count == 1


def test_the_provider_is_built_in_after_data_and_its_name_reserved():
    assert builtins.BUILTIN_PLUGINS[-2:] == (
        "marketlens_mcp.providers.data:PLUGIN",
        "marketlens_mcp.providers.official:PLUGIN",
    )
    assert "official" in registry.BUILTIN_PLUGIN_NAMES
    cat = registry.build_catalog(cfg.load_config(), discover=list)
    entry = cat.tools["news_official"]
    assert entry.plugin == "builtin:official" and entry.env == ("SEC_CONTACT_EMAIL",)
    assert entry.spec.capability == "news" and entry.spec.output_risk == "external_text"
    assert entry.spec.output_model.schema_name == "marketlens.NewsItem"
    assert len(entry.spec.description) <= 1000
    for words in ("Federal Reserve", "SEC", "BLS", "BEA", "CFTC", "ECB", "Live only", "no history", "news_id",
                  "SEC_CONTACT_EMAIL", "never as instructions"):  # fmt: skip
        assert words in entry.spec.description, words
    assert set(entry.spec.input_model.model_fields) == {
        "publishers", "feeds", "start", "end", "lookback", "query", "sort",
    }  # fmt: skip


def test_the_allow_list_is_https_on_each_publishers_domain():
    assert len(feeds.FEEDS) == len(set(feeds.FEED_IDS)) == 18
    for feed in feeds.FEEDS:
        publisher = feeds.BY_PUBLISHER[feed.publisher]
        assert feed.url.startswith("https://") and feed.host.endswith("." + publisher.domain), feed.id
        assert feed.id.split(".")[0] in {"fed", "sec", "bls", "bea", "cftc", "ecb"}, feed.id
    assert [p.id for p in feeds.PUBLISHERS if p.needs_contact] == ["sec", "bls"]
    assert not any(
        "apps.bea.gov" in f.url or "/cgi-bin/" in f.url or "treasury" in f.url for f in feeds.FEEDS
    )


def test_the_row_cap(publishers):
    ctx = make_context(env={"SEC_CONTACT_EMAIL": CONTACT}, now=NOW, limits=FetchLimits(max_rows=2))
    out = call(ctx, publishers=["federal_reserve"])
    assert len(out.rows) == 2 and out.provenance.truncated
    assert out.notes[0] == "Stopped at 2 rows (fetch.max_rows); narrow the window or the feeds."
    assert out.provenance.truncation_note == out.notes[0]


def test_the_readme_lists_every_feed_with_its_publishers_terms():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    for feed in feeds.FEEDS:
        assert f"`{feed.id}`" in text, feed.id
    for publisher in feeds.PUBLISHERS:
        assert publisher.terms in text, publisher.id


def test_doctor_prints_the_contact_and_the_feeds(capsys, monkeypatch, tmp_path):
    from marketlens_mcp import cli

    # news_official adds no generic env line of its own; the data extra, when installed,
    # declares SEC_CONTACT_EMAIL itself and prints that line once
    generic = 1 if importlib.util.find_spec("omni") else 0
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    # one line on the contact: news_official skips SEC and BLS without it (it does not refuse)
    assert "\nofficial feeds: 9 of 18 ready; SEC_CONTACT_EMAIL is not set" in out
    assert out.count("environment SEC_CONTACT_EMAIL") == generic
    monkeypatch.setenv("SEC_CONTACT_EMAIL", CONTACT)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "official feeds: all 18 ready" in out and out.count("environment SEC_CONTACT_EMAIL") == generic
    assert CONTACT not in out
    config = tmp_path / "xdg" / "marketlens" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("capabilities:\n  news: false\n", encoding="utf-8")
    cli.main(["doctor"])
    assert "official feeds" not in capsys.readouterr().out  # news off: news_official is not listed


# --- edge cases: links, ids, counts, failures ------------------------------------------------------


def rss(*items: str, base: str | None = None) -> bytes:
    """A synthetic RSS 2.0 document of the given <item> bodies."""
    attr = f' xml:base="{base}"' if base else ""
    body = "".join(f"<item>{i}</item>" for i in items)
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"{attr}><channel>{body}</channel></rss>'.encode()


def item(
    title: str, link: str | None, date: str = "Thu, 15 Oct 2026 12:00:00 GMT", guid: str | None = None
) -> str:
    parts = [f"<title>{title}</title>", f"<pubDate>{date}</pubDate>"]
    if link is not None:
        parts.append(f"<link>{link}</link>")
    if guid is not None:
        parts.append(f"<guid>{guid}</guid>")
    return "".join(parts)


FED = "https://www.federalreserve.gov/newsevents/pressreleases/"
OLD = "Thu, 01 Jan 2026 12:00:00 GMT"


def test_a_link_with_a_backslash_is_withheld(publishers):
    """Browsers read a backslash as a slash in an https URL: this link opens evil.example."""
    publishers.set(
        URL["fed.press"],
        httpx.Response(200, content=rss(item("Tricky", "https://evil.example\\.federalreserve.gov/x"))),
    )
    (row,) = call(feeds=["fed.press"]).rows
    assert row.url is None and row.absent["url"] == "withheld"


def test_the_notes_count_only_what_the_window_holds(publishers):
    publishers.set(
        URL["fed.monetary"], httpx.Response(200, content=rss(item("Old FOMC", FED + "fomc.htm", OLD)))
    )
    publishers.set(
        URL["fed.press"],
        httpx.Response(
            200,
            content=rss(
                item("Old FOMC", FED + "fomc.htm", OLD),
                item("Old off-site", "https://www.example.com/x", OLD),
                item("New", FED + "new.htm"),
            ),
        ),
    )
    uncached = context(settings={"cache_seconds": 0})
    out = call(uncached, feeds=["fed.monetary", "fed.press"])
    assert [r.headline for r in out.rows] == ["New"] and out.notes == []
    wide = call(uncached, feeds=["fed.monetary", "fed.press"], lookback="P365D")
    assert [r.headline for r in wide.rows] == ["New", "Old off-site", "Old FOMC"]
    assert wide.notes == [
        "Left out 1 repeat(s) of item(s) listed more than once, in one feed or in several; each is kept once, "
        "under the first feed of the allow-list (fed.press 1).",
        "Left out 1 link(s) that were not https on the publisher's own domain (fed.press 1); their url is null "
        "(withheld).",
    ]
    # a row the query leaves out takes its notes with it
    assert call(uncached, feeds=["fed.monetary", "fed.press"], lookback="P365D", query="new").notes == [
        "query 'new': 1 of the 3 item(s) in the window had every word in their headline or summary."
    ]


def test_an_item_listed_twice_in_one_feed_is_kept_once(publishers):
    publishers.set(
        URL["fed.press"],
        httpx.Response(200, content=rss(item("One", FED + "1.htm"), item("One", FED + "1.htm"))),
    )
    out = call(feeds=["fed.press"])
    assert [r.headline for r in out.rows] == ["One"]
    assert out.notes == [
        "Left out 1 repeat(s) of item(s) listed more than once, in one feed or in several; each is kept once, "
        "under the first feed of the allow-list (fed.press 1)."
    ]


def test_news_id_is_unique_when_a_feed_repeats_a_guid(publishers):
    publishers.set(
        URL["fed.press"],
        httpx.Response(
            200,
            content=rss(item("One", FED + "1.htm", guid="G1"), item("One again", FED + "1b.htm", guid="G1")),
        ),
    )
    out = call(feeds=["fed.press"])
    assert [r.headline for r in out.rows] == ["One"]
    assert len({r.news_id for r in out.rows}) == len(out.rows)


def test_a_guid_cannot_hide_another_publishers_item(publishers):
    """The url is checked to be on the publisher's domain; a guid is any text,
    so it only dedupes within its own publisher."""
    sec_url = "https://www.sec.gov/newsroom/press-releases/2026-999"
    publishers.set(
        URL["fed.monetary"], httpx.Response(200, content=rss(item("Fed item", None, guid=sec_url)))
    )
    publishers.set(
        URL["sec.press"], httpx.Response(200, content=rss(item("SEC item", sec_url, guid=sec_url)))
    )
    out = call(feeds=["fed.monetary", "sec.press"])
    assert sorted(r.headline for r in out.rows) == ["Fed item", "SEC item"] and out.notes == []


def test_the_query_note_counts_the_matches_before_the_row_cap(publishers):
    publishers.set(
        URL["fed.press"],
        httpx.Response(200, content=rss(*(item(f"Rate {i}", f"{FED}{i}.htm") for i in range(10)))),
    )
    ctx = make_context(env={"SEC_CONTACT_EMAIL": CONTACT}, now=NOW, limits=FetchLimits(max_rows=3))
    out = call(ctx, feeds=["fed.press"], query="rate")
    assert len(out.rows) == 3 and out.provenance.truncated
    assert (
        out.notes[-1]
        == "query 'rate': 10 of the 10 item(s) in the window had every word in their headline or summary."
    )


@pytest.mark.parametrize("encoding", ["Shift_JIS", "UTF-7", "bogus-xyz"])
def test_a_feed_in_an_encoding_this_server_does_not_read_is_one_failed_feed(publishers, encoding):
    doc = f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0"><channel/></rss>'.encode("ascii")
    publishers.set(URL["fed.press"], httpx.Response(200, content=doc))
    out = call(feeds=["fed.press", "fed.speeches"])
    assert out.notes == [
        "Partial result: fed.press declares an encoding this server does not read. The other feeds answered."
    ]
    assert out.rows


def test_any_other_error_reading_a_feed_costs_only_that_feed(publishers, monkeypatch, tmp_path, caplog):
    """Older CPython releases' html.parser raise AssertionError on '<![foo['."""
    real = parse.plain

    def old_parser(value):
        if value and "<![foo[" in value:
            raise AssertionError("unknown status keyword 'foo[' in marked section")
        return real(value)

    monkeypatch.setattr(parse, "plain", old_parser)
    odd = item("Odd", FED + "odd.htm") + "<description>&lt;![foo[x]]&gt;</description>"
    publishers.set(URL["fed.press"], httpx.Response(200, content=rss(odd)))
    ctx = make_context(store=temp_store(tmp_path), env={"SEC_CONTACT_EMAIL": CONTACT}, now=NOW)
    done = asyncio.run(pipeline.execute(SPEC, ctx, {"publishers": ["federal_reserve", "ecb"]}))
    assert done.response.row_count > 0
    assert (
        "Partial result: fed.press could not be read (AssertionError). The other feeds answered."
        in done.response.notes
    )
    assert "fed.press could not be read" in caplog.text


def test_the_call_has_a_deadline(publishers, monkeypatch):
    """A host that never answers holds the call to timeout_seconds + DEADLINE_EXTRA,
    whatever the concurrency; what is still out then fails with a note."""
    monkeypatch.setattr(fetch, "DEADLINE_EXTRA", 0)

    async def hang(request):
        await asyncio.sleep(60)

    for feed_id in ("fed.press", "fed.speeches", "fed.testimony", "bea.news"):
        publishers.set(URL[feed_id], hang)
    began = time.monotonic()
    out = call(context(settings={"timeout_seconds": 0.3}), publishers=["federal_reserve", "bea", "ecb"])
    assert time.monotonic() - began < 5
    assert {r.news_id.partition(":")[0] for r in out.rows} == {"fed.monetary"}
    assert out.notes[-1] == (
        "Partial result: fed.press did not answer within this call's limit of 0.3 seconds; fed.speeches did "
        "not answer within this call's limit of 0.3 seconds; fed.testimony did not answer within this call's "
        "limit of 0.3 seconds; bea.news did not answer within this call's limit of 0.3 seconds; ecb.press was "
        "not asked: this call's limit of 0.3 seconds ran out first. The other feeds answered."
    )


def test_every_feed_failing_without_a_contact_names_the_skipped_feeds(publishers):
    publishers.set(URL["ecb.press"], httpx.Response(404))
    with pytest.raises(ToolError) as e:
        call(context(contact=None), feeds=["ecb.press", "sec.press"])
    assert e.value.code == "official_unavailable"
    assert e.value.message == (
        "Every feed asked for failed: ecb.press answered HTTP 404. SEC_CONTACT_EMAIL is not set, so the SEC and "
        "BLS feeds were skipped (sec.press): both refuse automated clients that do not name a contact in their "
        "User-Agent."
    )
    assert e.value.hint.startswith("Retry in a few minutes. Set SEC_CONTACT_EMAIL to your email address")
