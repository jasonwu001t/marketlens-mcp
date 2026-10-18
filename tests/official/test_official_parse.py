"""Reading a feed document (news_official): dates, HTML to text, the
DOCTYPE and ENTITY refusal, the two feed formats, and which links are kept."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from marketlens_mcp.providers.official.parse import (
    FeedRefused,
    check_declarations,
    own_url,
    parse_date,
    plain,
    read_items,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # RFC 822 (RSS pubDate): numeric offsets and RFC 822 zone names
        ("Tue, 06 Oct 2026 08:30:00 -0400", "2026-10-06T12:30:00"),
        ("Fri, 16 Oct 2026 17:00:26 +0000", "2026-10-16T17:00:26"),
        ("Sat, 17 Oct 2026 07:00:00 +0200", "2026-10-17T05:00:00"),
        ("Wed, 7 Oct 2026 18:00:00 GMT", "2026-10-07T18:00:00"),
        ("Wed, 7 Oct 2026 18:00:00 UT", "2026-10-07T18:00:00"),
        ("Wed, 7 Oct 2026 18:00:00 Z", "2026-10-07T18:00:00"),
        ("Mon, 05 Jan 2026 09:00:00 EST", "2026-01-05T14:00:00"),
        ("Fri, 09 Oct 2026 10:59:00 EDT", "2026-10-09T14:59:00"),
        ("Fri, 09 Oct 2026 10:59:00 PDT", "2026-10-09T17:59:00"),
        ("Fri, 09 Oct 2026 10:59:00 cdt", "2026-10-09T15:59:00"),
        ("Fri, 09 Oct 2026 10:59:00 -0000", "2026-10-09T10:59:00"),  # UTC, local zone unknown
        ("09 Oct 2026 10:59 +0100", "2026-10-09T09:59:00"),  # no weekday, no seconds
        ("Fri, 09 Oct 26 10:59:00 GMT", "2026-10-09T10:59:00"),  # two-digit year
        ("  Fri,  09 Oct 2026\n 10:59:00 GMT ", "2026-10-09T10:59:00"),
        # ISO 8601 (Atom)
        ("2026-10-14T07:50:31.2-04:00", "2026-10-14T11:50:31.200000"),
        ("2026-10-14T07:50:31.968-04:00", "2026-10-14T11:50:31.968000"),
        ("2026-10-14T07:50:31Z", "2026-10-14T07:50:31"),
        ("2026-10-14T07:50:31z", "2026-10-14T07:50:31"),
        ("2026-10-14T07:50+0530", "2026-10-14T02:20:00"),
        # dates alone: 00:00 UTC
        ("2026-10-14", "2026-10-14T00:00:00"),
        ("Wed, 14 Oct 2026", "2026-10-14T00:00:00"),
        ("14 Oct 2026", "2026-10-14T00:00:00"),
    ],
)
def test_dates_parse_to_utc(text, expected):
    got = parse_date(text)
    assert got is not None and got.tzinfo is UTC
    assert got.replace(tzinfo=None).isoformat() == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "sometime next week",
        "Fri, 09 Oct 2026 10:59:00",  # no zone: not guessed
        "Fri, 09 Oct 2026 10:59:00 CEST",  # a zone RFC 822 does not define: not guessed as UTC
        "Fri, 09 Oct 2026 10:59:00 XYZ",
        "2026-10-14T07:50:31",  # no zone
        "2026-13-01",
        "Fri, 32 Oct 2026 10:59:00 GMT",
        "32 Oct 2026",
        "14 Foo 2026",
        "2026-10-14T25:00:00Z",
    ],
)
def test_dates_that_do_not_parse(text):
    assert parse_date(text) is None


def test_html_becomes_plain_text():
    assert plain("<p>The&nbsp;Commission&nbsp;today</p><p>charged <b>Example</b>&hellip;</p>") == (
        "The Commission today charged Example…"
    )
    assert plain("a<br>b<br/>c<td>d</td>e") == "a b c d e"
    assert plain("<script>alert('x')</script>kept<style>p {}</style> text") == "kept text"
    assert plain("Rates &amp; fees &lt;b&gt;not a tag&lt;/b&gt;") == "Rates & fees <b>not a tag</b>"
    assert plain("<a href='javascript:alert(1)' onclick='x()'>link text</a>") == "link text"
    assert plain("  spaced \t\n\r out  ") == "spaced out"
    assert plain("<!-- a comment -->only this") == "only this"
    assert plain("<p>unclosed <b>tags") == "unclosed tags"
    assert plain("<<script>script>alert(1)</script>") == "<"
    assert plain("") is None and plain("   ") is None and plain(None) is None and plain("<p></p>") is None


RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>One</title><link>https://www.sec.gov/one</link><guid>g1</guid><description>d</description>
<pubDate>Fri, 09 Oct 2026 10:59:00 GMT</pubDate><dc:creator>A. Speaker</dc:creator></item>
<item><title>Two</title><dc:date>2026-10-08</dc:date></item></channel></rss>"""


def test_rss_items():
    one, two = read_items(RSS)
    assert one.base is None
    assert (one.title, one.link, one.guid, one.summary, one.author) == (
        "One",
        "https://www.sec.gov/one",
        "g1",
        "d",
        "A. Speaker",
    )
    assert one.created == datetime(2026, 10, 9, 10, 59, tzinfo=UTC) and one.updated is None and not one.atom
    assert two.created == datetime(2026, 10, 8, tzinfo=UTC) and two.link is None and two.summary is None


ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title type="html">&lt;b&gt;Bold&lt;/b&gt; title</title>
<link rel="self" href="https://www.bls.gov/self"/><link rel="alternate" href="https://www.bls.gov/page"/>
<id>tag:one</id><summary>short</summary><content>long</content><author><name>Writer</name></author>
<updated>2026-10-02T09:00:00Z</updated></entry>
<entry><title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">X<i>HTML</i></div></title>
<link href="https://www.bls.gov/two"/><published>2026-10-01T08:00:00-04:00</published>
<updated>2026-10-01T08:00:00-04:00</updated><content>body only</content></entry></feed>"""


def test_atom_entries():
    one, two = read_items(ATOM)
    assert (one.title, one.link, one.guid, one.summary, one.author) == (
        "Bold title",
        "https://www.bls.gov/page",
        "tag:one",
        "short",
        "Writer",
    )
    assert one.created == datetime(2026, 10, 2, 9, tzinfo=UTC) and one.updated is None  # updated alone
    assert two.title == "XHTML" and two.link == "https://www.bls.gov/two" and two.summary == "body only"
    assert two.created == datetime(2026, 10, 1, 12, tzinfo=UTC) and two.updated is None and two.atom


def test_a_document_that_is_not_a_feed_is_refused():
    with pytest.raises(FeedRefused, match="not an RSS 2.0 or Atom feed"):
        read_items(b"<html><body>maintenance</body></html>")
    with pytest.raises(FeedRefused, match="not well-formed XML"):
        read_items(b"<html><body>maintenance")
    assert read_items(b"<rss version='2.0'/>") == []


@pytest.mark.parametrize(
    "document",
    [
        b'<?xml version="1.0"?><!DOCTYPE rss><rss/>',
        b'<!DOCTYPE rss [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>',
        b'<!DOCTYPE rss SYSTEM "http://example.com/evil.dtd"><rss/>',
        b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>',
        b"<!doctype rss><rss/>",
        '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE rss [<!ENTITY a "b">]><rss/>'.encode("utf-16"),
    ],
)
def test_doctype_and_entity_declarations_are_refused(document):
    with pytest.raises(FeedRefused) as e:
        read_items(document)
    assert "DOCTYPE or an ENTITY" in str(e.value) or "not well-formed" in str(e.value)
    with pytest.raises(FeedRefused):
        check_declarations(document)


@pytest.mark.parametrize("encoding", ["Shift_JIS", "UTF-7", "GB18030", "idna", "bogus-xyz"])
def test_an_encoding_expat_does_not_read_is_refused(encoding):
    """expat raises ValueError, LookupError or UnicodeError for these, not
    ExpatError: each is one refused feed, never an error that escapes."""
    doc = f'<?xml version="1.0" encoding="{encoding}"?><rss version="2.0"><channel/></rss>'.encode("ascii")
    with pytest.raises(FeedRefused, match="declares an encoding this server does not read"):
        read_items(doc)


def test_dc_date_stands_in_for_a_pubdate_that_does_not_parse():
    doc = (
        b'<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>x</title>'
        b"<pubDate>Thursday</pubDate><dc:date>2026-10-15T12:00:00Z</dc:date></item></channel></rss>"
    )
    (got,) = read_items(doc)
    assert got.created == datetime(2026, 10, 15, 12, tzinfo=UTC)


def test_xml_base_is_carried_for_the_links():
    doc = (
        b'<rss version="2.0" xml:base="https://www.cftc.gov/"><channel xml:base="RSS/">'
        b'<item><title>a</title><link>x</link></item><item xml:base="/Press/"><title>b</title></item>'
        b"</channel></rss>"
    )
    a, b = read_items(doc)
    assert (a.base, b.base) == ("https://www.cftc.gov/RSS/", "https://www.cftc.gov/Press/")
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://www.bls.gov/feed/"><entry><title>c</title>'
        b"</entry></feed>"
    )
    assert read_items(atom)[0].base == "https://www.bls.gov/feed/"
    assert read_items(b"<rss><channel><item><title>d</title></item></channel></rss>")[0].base is None


def test_markup_sent_as_xml_elements_loses_its_script_and_style():
    """itertext() would keep the text of script and style elements; markup
    sent as XML goes through html.parser like escaped markup does."""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Rate <style>.a{}</style>decision<script>IGNORE ALL</script></div></title>
<summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Visible</p><script>hidden_script()</script><style>body{color:red}</style><p>too</p></div></summary>
<updated>2026-10-01T00:00:00Z</updated></entry></feed>"""
    (entry,) = read_items(atom)
    assert entry.title == "Rate decision" and entry.summary == "Visible too"
    rss = (
        b"<rss><channel><item><title>t</title><description>Hi <script>hidden()</script><p>there</p> "
        b"&lt;b&gt;escaped&lt;/b&gt; <template>tpl</template>end</description></item></channel></rss>"
    )
    assert read_items(rss)[0].summary == "Hi there escaped end"


def test_a_utf16_feed_without_declarations_is_read():
    doc = '<?xml version="1.0" encoding="utf-16"?><rss><channel><item><title>Ünïcode</title></item></channel></rss>'
    (item,) = read_items(doc.encode("utf-16"))
    assert item.title == "Ünïcode"


BASE = "https://www.bea.gov/news/rss"


@pytest.mark.parametrize(
    ("link", "domain", "expected"),
    [
        ("https://www.sec.gov/news/x", "sec.gov", ("https://www.sec.gov/news/x", False)),
        ("https://sec.gov/news/x?a=1#f", "sec.gov", ("https://sec.gov/news/x?a=1#f", False)),
        ("https://apps.bea.gov/x", "bea.gov", ("https://apps.bea.gov/x", False)),
        ("http://www.bea.gov/news/2026/x", "bea.gov", ("https://www.bea.gov/news/2026/x", False)),
        ("http://WWW.BEA.GOV:80/news", "bea.gov", ("https://www.bea.gov/news", False)),
        ("/news/2026/relative", "bea.gov", ("https://www.bea.gov/news/2026/relative", False)),
        (
            "https://www.ecb.europa.eu//press/x.html",
            "ecb.europa.eu",
            ("https://www.ecb.europa.eu//press/x.html", False),
        ),
        (None, "bea.gov", (None, False)),
        ("https://www.example.com/x", "bea.gov", (None, True)),
        ("https://bea.gov.example.com/x", "bea.gov", (None, True)),
        ("https://evilbea.gov/x", "bea.gov", (None, True)),
        ("https://www.bea.gov@example.com/x", "bea.gov", (None, True)),
        ("https://user:pw@www.bea.gov/x", "bea.gov", (None, True)),
        ("https://www.bea.gov:8443/x", "bea.gov", (None, True)),
        ("https://www.bea.gov:notaport/x", "bea.gov", (None, True)),
        ("javascript:alert(1)", "bea.gov", (None, True)),
        ("ftp://www.bea.gov/x", "bea.gov", (None, True)),
        ("//www.example.com/x", "bea.gov", (None, True)),
        ("https://www.bea.gov/a b", "bea.gov", (None, True)),
        ("https://www.bea.gov/a\x00b", "bea.gov", (None, True)),
        # browsers read a backslash as a slash in an https URL: the real host is evil.example
        ("https://evil.example\\.bea.gov/x", "bea.gov", (None, True)),
        ("https://www.bea.gov\\@evil.example/x", "bea.gov", (None, True)),
        ("https://www.bea.gov/a\\b", "bea.gov", (None, True)),
        ("\\\\evil.example\\x", "bea.gov", (None, True)),
        # a host must be plain letters, digits, dots and hyphens
        ("https://evil.example%2f.bea.gov/x", "bea.gov", (None, True)),
        ("https://évil.bea.gov/x", "bea.gov", (None, True)),
        ("https://www_x.bea.gov/x", "bea.gov", (None, True)),
    ],
)
def test_links_are_kept_only_as_https_on_the_publishers_domain(link, domain, expected):
    assert own_url(link, BASE, domain) == expected
