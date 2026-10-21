"""The fixed allow-list of official feeds. Nothing else is ever fetched:
there is no setting or argument that adds a URL (no SSRF), and a redirect is
followed only to an https address on the same publisher's hosts.

Each feed was checked once, live, on 2026-10-18 with a declared User-Agent
(marketlens-mcp/0.2.0 and a contact), and the publisher's robots.txt was read
the same day. Why each is in, and why the candidates left out are out:

In (every one answered 200 with a current RSS 2.0 or Atom document, and
robots.txt allows its path for every agent):

* Federal Reserve Board, www.federalreserve.gov/feeds/ (robots.txt: none,
  404). press_monetary.xml (FOMC statements, minutes, monetary-policy
  releases), press_all.xml (every press release, monetary ones included),
  speeches.xml, testimony.xml (the newest testimony was three months old:
  testimony is seasonal, not a stale feed). press_monetary comes before
  press_all so that an FOMC release, which is in both, keeps the
  ``fed.monetary`` id. The Board's feeds name no author, so a speech's
  speaker is not given: a title's words before its first comma are not
  reliably a name (the testimony feed had "Regulation, Global Governance
  Bodies").
* SEC, www.sec.gov (robots.txt allows /news and /enforcement-litigation;
  only /cgi-bin and EDGAR internals are disallowed). Press releases,
  speeches and statements, litigation releases. The litigation feed answers
  403 to an undeclared agent: SEC's fair-access policy asks every automated
  client to name a contact, so every SEC feed needs SEC_CONTACT_EMAIL.
* BLS, www.bls.gov/feed/ (robots.txt allows /feed). bls_latest (the latest
  numbers) and the release feeds of the CPI, the Employment Situation, the
  PPI, JOLTS and the ECI. BLS answers 403 ("Access Denied", its bot policy)
  to an undeclared agent, so BLS needs SEC_CONTACT_EMAIL as well.
* BEA, www.bea.gov/news/rss (robots.txt allows it). Its items link with
  http://www.bea.gov; parse.own_url serves them as https on the same host.
* CFTC, www.cftc.gov/RSS/ (robots.txt allows every agent but named AI
  training crawlers, and states "ai-train=no"; this server reads headlines
  live for its user and trains nothing). Press releases, enforcement
  releases, speeches and testimony.
* ECB, www.ecb.europa.eu/rss/press.html (robots.txt allows it and asks for a
  Crawl-delay of 5 s: ``Publisher.crawl_delay``, so a repeat within 5 s is
  answered from the cache even with cache_seconds 0, and the host's limiter
  allows 12 requests a minute). Press releases.

Out:

* apps.bea.gov/rss/rss.xml (BEA's richer release feed): robots.txt
  disallows every path of apps.bea.gov for every agent.
* EDGAR's company and current-filings Atom feeds (www.sec.gov/cgi-bin/
  browse-edgar): robots.txt disallows /cgi-bin. Filings belong to the data
  extra's sec_ tools. SEC's full-text search, a JSON API on efts.sec.gov, is
  filings_search (search.py).
* The Treasury: home.treasury.gov has no press-release feed (its /rss.xml is
  a general site feed, mostly FAQ entries months old), and TreasuryDirect's
  robots.txt disallows every agent but the government's search crawler
  (usasearch).
* The Fed's H.15 feed (h15.xml): stale, its newest entry is from July 2026
  although H.15 is published every business day.
* The IMF: no feed URL answered (403 to a generic agent, 404 at /en/News/RSS).
* Publisher and vendor feeds (WSJ, MarketWatch, Barron's, CNBC, FT,
  Bloomberg, Reuters, AP, Yahoo Finance, Nasdaq.com, Seeking Alpha,
  Investing.com): their terms limit feeds to personal, non-commercial use or
  forbid automated or AI use, so the owner excluded them. Licensed headlines
  come from news_search (Alpaca).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

#: Where a feed's author comes from: its dc:creator or Atom author name; or
#: nowhere, because the feed's dc:creator is not an author (a web editor's
#: user name, a release number, or the agency itself).
AuthorFrom = Literal["creator", "none"]


@dataclass(frozen=True)
class Publisher:
    id: str
    name: str
    #: Links are kept only on this domain or its subdomains.
    domain: str
    #: SEC and BLS refuse undeclared agents: their feeds need SEC_CONTACT_EMAIL.
    needs_contact: bool
    #: Reuse and attribution terms, one line (README).
    terms: str
    #: robots.txt's Crawl-delay in seconds (0: none): the feeds' cache never
    #: expires sooner, and the host's limiter allows 60 / crawl_delay a minute.
    crawl_delay: int = 0


@dataclass(frozen=True)
class Feed:
    id: str
    publisher: str
    url: str
    title: str
    author_from: AuthorFrom = "creator"

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


PUBLISHERS: tuple[Publisher, ...] = (
    Publisher(
        "federal_reserve",
        "Federal Reserve Board",
        "federalreserve.gov",
        False,
        "US federal work, public domain; the Board asks to be cited as the source.",
    ),
    Publisher(
        "sec",
        "U.S. Securities and Exchange Commission",
        "sec.gov",
        True,
        "US federal work, public domain; fair access: a declared User-Agent with a contact, at most 10 "
        "requests a second.",
    ),
    Publisher(
        "bls",
        "U.S. Bureau of Labor Statistics",
        "bls.gov",
        True,
        "US federal work, public domain; BLS asks to be cited as the source.",
    ),
    Publisher(
        "bea",
        "U.S. Bureau of Economic Analysis",
        "bea.gov",
        False,
        "US federal work, public domain.",
    ),
    Publisher(
        "cftc",
        "U.S. Commodity Futures Trading Commission",
        "cftc.gov",
        False,
        "US federal work, public domain; the CFTC asks to be acknowledged (its robots.txt: no AI training).",
    ),
    Publisher(
        "ecb",
        "European Central Bank",
        "ecb.europa.eu",
        False,
        "Free to reuse if reproduced accurately with the ECB cited as the source; whoever sells it must say "
        "it is available free.",
        crawl_delay=5,
    ),
)

FEEDS: tuple[Feed, ...] = (
    Feed("fed.monetary", "federal_reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml",
         "Monetary policy press releases"),
    Feed("fed.press", "federal_reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
         "All press releases"),
    Feed("fed.speeches", "federal_reserve", "https://www.federalreserve.gov/feeds/speeches.xml", "Speeches"),
    Feed("fed.testimony", "federal_reserve", "https://www.federalreserve.gov/feeds/testimony.xml", "Testimony"),
    Feed("sec.press", "sec", "https://www.sec.gov/news/pressreleases.rss", "Press releases"),
    # dc:creator is read if present, but the live feed leaves it blank (the speaker is
    # named only in the description), so rows usually carry no author
    Feed("sec.speeches", "sec", "https://www.sec.gov/news/speeches-statements.rss", "Speeches and statements"),
    # dc:creator holds the release number (LR-NNNNN), not an author
    Feed("sec.litigation", "sec", "https://www.sec.gov/enforcement-litigation/litigation-releases/rss",
         "Litigation releases", "none"),
    # dc:creator is the agency itself
    Feed("bls.latest", "bls", "https://www.bls.gov/feed/bls_latest.rss", "Latest numbers", "none"),
    Feed("bls.cpi", "bls", "https://www.bls.gov/feed/cpi.rss", "Consumer Price Index releases"),
    Feed("bls.empsit", "bls", "https://www.bls.gov/feed/empsit.rss", "Employment Situation releases"),
    Feed("bls.ppi", "bls", "https://www.bls.gov/feed/ppi.rss", "Producer Price Index releases"),
    Feed("bls.jolts", "bls", "https://www.bls.gov/feed/jolts.rss", "Job Openings and Labor Turnover releases"),
    Feed("bls.eci", "bls", "https://www.bls.gov/feed/eci.rss", "Employment Cost Index releases"),
    # dc:creator is the agency itself
    Feed("bea.news", "bea", "https://www.bea.gov/news/rss", "News releases", "none"),
    # dc:creator is the web editor's user name, not an author
    Feed("cftc.press", "cftc", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", "Press releases", "none"),
    Feed("cftc.enforcement", "cftc", "https://www.cftc.gov/RSS/RSSENF/rssenf.xml", "Enforcement releases",
         "none"),
    # dc:creator names the speaker
    Feed("cftc.speeches", "cftc", "https://www.cftc.gov/RSS/RSSST/rssst.xml", "Speeches and testimony"),
    Feed("ecb.press", "ecb", "https://www.ecb.europa.eu/rss/press.html", "Press releases"),
)  # fmt: skip

PUBLISHER_IDS: tuple[str, ...] = tuple(p.id for p in PUBLISHERS)
FEED_IDS: tuple[str, ...] = tuple(f.id for f in FEEDS)
BY_PUBLISHER: dict[str, Publisher] = {p.id: p for p in PUBLISHERS}
BY_FEED: dict[str, Feed] = {f.id: f for f in FEEDS}
#: The hosts a publisher's feeds live on: the only hosts a redirect may lead to.
HOSTS: dict[str, frozenset[str]] = {
    p.id: frozenset(f.host for f in FEEDS if f.publisher == p.id) for p in PUBLISHERS
}


def select(publishers: list[str] | None, feeds: list[str] | None) -> list[Feed]:
    """The feeds asked for, in allow-list order: every feed of ``publishers``
    plus ``feeds``; all of them when neither is given."""
    if not publishers and not feeds:
        return list(FEEDS)
    wanted = set(feeds or ())
    chosen = set(publishers or ())
    return [f for f in FEEDS if f.id in wanted or f.publisher in chosen]
