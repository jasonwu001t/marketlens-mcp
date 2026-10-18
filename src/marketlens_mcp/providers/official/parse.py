"""Reading a feed document: untrusted input, read with the standard library
only and never executed or evaluated.

* A document that declares a DOCTYPE or an ENTITY is refused before it is
  parsed into a tree (an expat pass whose declaration handlers raise, so the
  rule holds in any encoding): no entity expansion, no external entities. A
  document in an encoding expat does not read (Shift_JIS, UTF-7, an unknown
  name) is refused too.
* HTML in titles and descriptions is reduced to text with ``html.parser``
  (entities decoded, script and style dropped, whitespace collapsed), never
  with a regular expression. Markup sent as XML child elements (Atom's
  ``type="xhtml"``, unescaped tags in an RSS description) goes through the same
  parser, rebuilt from the tree by local name, and a script, style or template
  element is dropped with its text.
* Links resolve against the ``xml:base`` in effect (document, channel, item),
  which the tool joins onto the feed's URL.
* Dates: RFC 822 (``pubDate``) with a numeric offset or a zone name of RFC 822
  (GMT, UT, Z, EST, EDT, CST, CDT, MST, MDT, PST, PDT, AST, ADT), ISO 8601
  (Atom) with Z or an offset, and dates alone (00:00 UTC). A time without a
  zone, or an unknown zone name, is not guessed: the item has no date.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from email.utils import parsedate_tz
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.parsers import expat

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"


class FeedRefused(ValueError):
    """The document is not one this server reads (the reason is the message)."""


# --- the document ----------------------------------------------------------------------------


def _refuse_declaration(*_args: object) -> None:
    raise FeedRefused("declares a DOCTYPE or an ENTITY, which this server refuses")


def check_declarations(raw: bytes) -> None:
    """Refuse a DOCTYPE or ENTITY declaration, and anything that is not
    well-formed XML."""
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _refuse_declaration
    parser.EntityDeclHandler = _refuse_declaration
    parser.UnparsedEntityDeclHandler = _refuse_declaration
    parser.ExternalEntityRefHandler = _refuse_declaration
    try:
        parser.Parse(raw, True)
    except FeedRefused:
        raise
    except expat.ExpatError as e:
        raise FeedRefused(f"is not well-formed XML ({expat.ErrorString(e.code)})") from None
    except (ValueError, LookupError):  # a multi-byte, unknown or unusable encoding
        raise FeedRefused("declares an encoding this server does not read") from None


@dataclass(frozen=True)
class RawItem:
    """One item as the feed gives it (text already plain)."""

    title: str | None
    link: str | None
    guid: str | None
    summary: str | None
    author: str | None
    #: The publication time, None when the feed gives none that parses.
    created: datetime | None
    #: Atom's updated, when it differs from the publication time.
    updated: datetime | None
    #: True for Atom (updated_at exists there, else the format has none).
    atom: bool
    #: The xml:base in effect for the item (may be relative), else None.
    base: str | None = None


def read_items(raw: bytes) -> list[RawItem]:
    """The items of an RSS 2.0 or Atom document."""
    check_declarations(raw)
    root = ET.fromstring(raw)  # noqa: S314 - declarations were refused above
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return []
        base = _base(None, root, channel)
        return [_rss_item(el, _base(base, el)) for el in channel.findall("item")]
    if root.tag == f"{ATOM}feed":
        base = _base(None, root)
        return [_atom_entry(el, _base(base, el)) for el in root.findall(f"{ATOM}entry")]
    raise FeedRefused("is not an RSS 2.0 or Atom feed")


def _base(base: str | None, *elements: ET.Element) -> str | None:
    """``base`` with each element's xml:base joined on, outermost first."""
    for el in elements:
        value = _bare(el.get(XML_BASE))
        if value is not None:
            base = urljoin(base, value) if base else value
    return base


def _local(tag: object) -> str:
    return tag.rpartition("}")[2].lower() if isinstance(tag, str) else ""


def _markup(el: ET.Element) -> str:
    """The element's content as HTML source: its text as given, its child
    elements as tags by local name (no attributes); a script, style or
    template element is dropped with its text, so ``plain`` never sees it."""
    parts = [el.text or ""]
    for child in el:
        name = _local(child.tag)
        if name and name not in _SKIPPED:
            parts.append(f"<{name}>{_markup(child)}</{name}>")
        parts.append(child.tail or "")
    return "".join(parts)


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    return _markup(el) if len(el) else (el.text or "")


def _rss_item(el: ET.Element, base: str | None) -> RawItem:
    created = parse_date(_text(el.find("pubDate"))) or parse_date(_text(el.find(f"{DC}date")))
    return RawItem(
        title=plain(_text(el.find("title"))),
        link=_bare(_text(el.find("link"))),
        guid=_bare(_text(el.find("guid"))),
        summary=plain(_text(el.find("description"))),
        author=plain(_text(el.find(f"{DC}creator"))),
        created=created,
        updated=None,
        atom=False,
        base=base,
    )


def _atom_link(el: ET.Element) -> str | None:
    links = el.findall(f"{ATOM}link")
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return _bare(link.get("href"))
    return None


def _atom_entry(el: ET.Element, base: str | None) -> RawItem:
    published = parse_date(_text(el.find(f"{ATOM}published")))
    updated = parse_date(_text(el.find(f"{ATOM}updated")))
    created = published or updated
    summary = el.find(f"{ATOM}summary")
    return RawItem(
        title=plain(_text(el.find(f"{ATOM}title"))),
        link=_atom_link(el),
        guid=_bare(_text(el.find(f"{ATOM}id"))),
        summary=plain(_text(summary if summary is not None else el.find(f"{ATOM}content"))),
        author=plain(_text(el.find(f"{ATOM}author/{ATOM}name"))),
        created=created,
        updated=updated if updated is not None and created is not None and updated != created else None,
        atom=True,
        base=base,
    )


def _bare(value: str | None) -> str | None:
    s = (value or "").strip()
    return s or None


# --- text ------------------------------------------------------------------------------------

#: Tags whose start or end separates words.
_BREAKS = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    ]
)
_SKIPPED = frozenset({"script", "style", "template"})


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIPPED:
            self._skipping += 1
        elif tag in _BREAKS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _BREAKS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BREAKS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def plain(value: str | None) -> str | None:
    """HTML (or text with entities) -> plain text with whitespace collapsed; None when empty."""
    if value is None:
        return None
    parser = _Text()
    parser.feed(value)
    parser.close()
    text = " ".join("".join(parser.parts).split())
    return text or None


# --- dates -----------------------------------------------------------------------------------

#: The zone names RFC 822 defines (hours from UTC).
RFC822_ZONES = {
    "UT": 0,
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "AST": -4,
    "ADT": -3,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
}
_NUMERIC_ZONE = re.compile(r"^[+-]\d{4}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2}))?$")
_RFC822_DAY = re.compile(r"^(?:[A-Za-z]{3},\s*)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def parse_date(value: str | None) -> datetime | None:
    """A feed's date as a UTC instant, or None when it does not parse."""
    s = " ".join((value or "").split())
    if not s:
        return None
    try:
        if _ISO.match(s):
            dt = datetime.fromisoformat(s.replace("z", "Z"))
            if len(s) == 10:  # a date alone
                return datetime.combine(dt.date(), time(0), UTC)
            return dt.astimezone(UTC)
        day = _RFC822_DAY.match(s)
        if day:
            month = _MONTHS.get(day[2].lower())
            if month is None:
                return None
            return datetime.combine(date(int(day[3]), month, int(day[1])), time(0), UTC)
        return _rfc822(s)
    except (ValueError, OverflowError):
        return None


def _rfc822(s: str) -> datetime | None:
    zone = s.rsplit(" ", 1)[-1]
    if not _NUMERIC_ZONE.match(zone) and zone.upper() not in RFC822_ZONES:
        return None  # no zone, or one RFC 822 does not define: not guessed
    parts = parsedate_tz(s)
    if parts is None:
        return None
    year, month, day, hour, minute, second = parts[:6]
    offset = timezone(timedelta(seconds=parts[9] or 0))
    return datetime(year, month, day, hour, minute, second, tzinfo=offset).astimezone(UTC)


# --- links -----------------------------------------------------------------------------------

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def own_url(link: str | None, base: str, domain: str) -> tuple[str | None, bool]:
    """``(url, refused)``: the item's link as an absolute https URL on
    ``domain`` (or a subdomain), else None. ``refused`` is True when there was
    a link but it was left out. Publishers' own http links (BEA's) are served
    as https on the same host. A backslash anywhere is refused: browsers read
    it as a slash in an https URL, so ``https://evil.example\\.sec.gov/``
    opens evil.example although urlsplit sees a subdomain of sec.gov; and the
    host must be plain letters, digits, dots and hyphens."""
    if not link:
        return None, False
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in link):
        return None, True
    try:
        parts = urlsplit(urljoin(base, link))
        port = parts.port
    except ValueError:
        return None, True
    host = (parts.hostname or "").lower()
    if (
        parts.scheme not in ("http", "https")
        or not _HOST.fullmatch(host)
        or not (host == domain or host.endswith("." + domain))
        or parts.username is not None
        or port not in (None, 443 if parts.scheme == "https" else 80)
    ):
        return None, True
    return urlunsplit(("https", host, parts.path or "/", parts.query, parts.fragment)), False
