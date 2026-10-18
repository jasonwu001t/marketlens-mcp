"""Fetching the allow-listed feeds. It can only GET.

* Only the URLs of ``feeds.FEEDS``, at most CONCURRENCY at a time, each after
  the process-wide limiter of its host (PER_MINUTE requests a minute; SEC's
  fair-access ceiling is 10 a second).
* User-Agent: ``marketlens-mcp/<version> (+<repository>)``; for SEC and BLS,
  ``marketlens-mcp/<version> <SEC_CONTACT_EMAIL>`` (SEC's fair-access form).
  Without a contact those feeds are never requested (``tool.py`` skips them).
* Timeout ``providers.official.timeout_seconds`` (default 20). A timeout, a
  connection failure or a 5xx is retried once after 1 s; any other answer but
  200 fails the feed.
* httpx follows no redirect. One to https on the same publisher's hosts is
  followed by hand (at most MAX_REDIRECTS); any other fails the feed.
* At most MAX_BYTES are read: a larger Content-Length is refused before the
  body, and a body stops at the cap. The request asks for
  ``Accept-Encoding: identity`` and a compressed answer is refused, so the cap
  counts the bytes that are parsed.
* A feed's items are kept in this process for ``cache_seconds`` (default
  300) per URL, so a question repeated within five minutes asks nobody.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.metadata
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from marketlens_mcp import __version__
from marketlens_mcp.plugin_api import ToolContext

from .feeds import HOSTS, Feed
from .parse import FeedRefused, RawItem, read_items

TIMEOUT_SECONDS = 20
CACHE_SECONDS = 300
MAX_BYTES = 5 * 1024 * 1024
CONCURRENCY = 4
PER_MINUTE = 60
MAX_REDIRECTS = 3
RETRY_AFTER = 1.0
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"

#: Test seam: an httpx transport used instead of the network (httpx.MockTransport).
_transport: ContextVar[httpx.AsyncBaseTransport | None] = ContextVar(
    "marketlens_official_transport", default=None
)
_sleep = asyncio.sleep
#: URL -> (when it was fetched, its items). Shared by every session of the process.
_CACHE: dict[str, tuple[datetime, tuple[RawItem, ...]]] = {}


@contextlib.contextmanager
def use_transport(transport: httpx.AsyncBaseTransport) -> Iterator[None]:
    """Route every feed request made inside the block through ``transport``."""
    token = _transport.set(transport)
    try:
        yield
    finally:
        _transport.reset(token)


def clear_cache() -> None:
    _CACHE.clear()


class FeedFailed(Exception):
    """One feed could not be read; the message says why."""


@dataclass(frozen=True)
class Settings:
    """``providers.official`` in the marketlens config, with its defaults."""

    cache_seconds: int = CACHE_SECONDS
    timeout_seconds: int = TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> Settings:
        m = m or {}
        return cls(**{k: m[k] for k in cls.__dataclass_fields__ if m.get(k) is not None})


@dataclass(frozen=True)
class Fetched:
    feed: Feed
    items: tuple[RawItem, ...] = ()
    error: str | None = None
    cached: bool = False


@functools.cache
def _repository() -> str | None:
    try:
        urls = importlib.metadata.metadata("marketlens-mcp").get_all("Project-URL") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in urls:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "repository":
            return url.strip()
    return None


def user_agent(contact: str | None = None) -> str:
    if contact:
        return f"marketlens-mcp/{__version__} {contact}"
    repository = _repository()
    return f"marketlens-mcp/{__version__}" + (f" (+{repository})" if repository else "")


# --- the byte cap ----------------------------------------------------------------------------


def _too_large() -> FeedFailed:
    return FeedFailed(f"is larger than {MAX_BYTES // 2**20} MB, the most this server reads")


class _CappedStream(httpx.AsyncByteStream):
    def __init__(self, inner: Any, limit: int):
        self._inner = inner
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._limit:
                raise _too_large()
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Capped(httpx.AsyncBaseTransport):
    """Wraps the real transport: refuses a compressed or oversized answer and
    stops reading a body at ``limit`` bytes."""

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int):
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        encoding = response.headers.get("content-encoding", "").strip().lower()
        length = response.headers.get("content-length", "").strip()
        problem = None
        if encoding not in ("", "identity"):
            problem = FeedFailed("answered compressed although this server asked for no compression")
        elif length.isdigit() and int(length) > self._limit:
            problem = _too_large()
        if problem is not None:
            await response.aclose()
            raise problem
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_CappedStream(response.stream, self._limit),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- one feed --------------------------------------------------------------------------------


def _redirect(url: str, response: httpx.Response, feed: Feed) -> str:
    """The next URL, only when it is https on the publisher's own hosts."""
    location = response.headers.get("location") or ""
    try:
        target = urlsplit(urljoin(url, location))
        host, port = target.hostname, target.port
    except ValueError:
        target, host, port = None, None, None
    if (
        not location
        or target is None
        or target.scheme != "https"
        or host not in HOSTS[feed.publisher]
        or port not in (None, 443)
        or target.username is not None
    ):
        shown = (host or "an unreadable address")[:80]
        raise FeedFailed(f"redirected to {shown}, which is not on the allow-list: not followed")
    return target.geturl()


async def _answer(
    ctx: ToolContext, http: httpx.AsyncClient, url: str, agent: str, timeout: int
) -> httpx.Response:
    """One GET, retried once after a timeout, a connection failure or a 5xx."""
    limiter = ctx.limiter(f"official:{urlsplit(url).hostname}", PER_MINUTE)
    for attempt in (1, 2):
        await limiter.acquire()
        try:
            response = await http.get(url, headers={"User-Agent": agent})
        except httpx.TimeoutException:
            reason = f"did not answer within {timeout} seconds"
        except httpx.TransportError as e:
            reason = f"could not be reached ({type(e).__name__})"
        else:
            if response.status_code < 500:
                return response
            reason = f"answered HTTP {response.status_code}"
        if attempt == 2:
            raise FeedFailed(f"{reason}, twice")
        await _sleep(RETRY_AFTER)
    raise AssertionError("unreachable")  # pragma: no cover


async def _read(ctx: ToolContext, http: httpx.AsyncClient, feed: Feed, agent: str, timeout: int) -> bytes:
    url = feed.url
    for _ in range(MAX_REDIRECTS + 1):
        response = await _answer(ctx, http, url, agent, timeout)
        status = response.status_code
        if 300 <= status < 400:
            url = _redirect(url, response, feed)
            continue
        if status == 429:
            raise FeedFailed("asked this server to slow down (HTTP 429)")
        if status != 200:
            raise FeedFailed(f"answered HTTP {status}")
        return response.content
    raise FeedFailed(f"redirected more than {MAX_REDIRECTS} times")


async def fetch_all(ctx: ToolContext, feeds: Sequence[Feed], contacts: Mapping[str, str]) -> list[Fetched]:
    """Every feed's items, in the order given. ``contacts`` maps a publisher
    that needs a declared contact to the address to send."""
    settings = Settings.from_mapping(ctx.settings)
    now = ctx.now()
    gate = asyncio.Semaphore(CONCURRENCY)
    inner = _transport.get() or httpx.AsyncHTTPTransport()
    headers = {"Accept": ACCEPT, "Accept-Encoding": "identity"}

    async with httpx.AsyncClient(
        transport=_Capped(inner, MAX_BYTES),
        timeout=settings.timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        headers=headers,
    ) as http:

        async def one(feed: Feed) -> Fetched:
            hit = _CACHE.get(feed.url)
            if hit is not None and 0 <= (now - hit[0]).total_seconds() < settings.cache_seconds:
                return Fetched(feed, hit[1], cached=True)
            agent = user_agent(contacts.get(feed.publisher))
            async with gate:
                try:
                    raw = await _read(ctx, http, feed, agent, settings.timeout_seconds)
                    items = tuple(read_items(raw))
                except (FeedFailed, FeedRefused, ET.ParseError) as e:
                    return Fetched(feed, error=str(e) or type(e).__name__)
            if settings.cache_seconds:
                _CACHE[feed.url] = (now, items)
            return Fetched(feed, items)

        return list(await asyncio.gather(*(one(f) for f in feeds)))
