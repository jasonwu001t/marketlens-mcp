"""The Alpaca HTTP client. It can only GET.

* Keys come from the server's environment only: ``ALPACA_API_KEY`` and
  ``ALPACA_SECRET_KEY`` (the names alpaca-mcp-server uses), or Alpaca's SDK
  names ``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY``. They are sent as the
  ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers and never appear in a
  response, an error or a log line.
* Base URLs: market data ``https://data.alpaca.markets``; trading (account,
  orders, positions, and the reference endpoints that live on the trading API)
  ``https://paper-api.alpaca.markets`` unless ``portfolio.environment`` is
  ``live`` (``https://api.alpaca.markets``). ``providers.alpaca.trading_url`` /
  ``data_url`` override them.
* Every request first waits for the process-wide limiter
  ``alpaca:<sha256(key id)[:8]>`` at ``rate_limit_per_minute`` (default 190,
  under Alpaca's 200 per minute).
* 429: honour ``Retry-After`` in seconds or as an HTTP-date (missing,
  unreadable or non-finite: 3 s), at most 2 retries; a wait over 60 s is
  ``alpaca_rate_limited`` at once. 5xx: one retry after 1 s. Timeout 30 s.
  Redirects are never followed (the key headers would go with them); a 3xx
  is ``alpaca_unavailable``. Failures become canonical ``ToolError`` codes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from marketlens_mcp import __version__
from marketlens_mcp.plugin_api import ToolContext, ToolError
from marketlens_schema.base import Environment

DATA_URL = "https://data.alpaca.markets"
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"
KEY_NAMES = (("ALPACA_API_KEY", "ALPACA_SECRET_KEY"), ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"))
TIMEOUT_SECONDS = 30.0
MAX_429_RETRIES = 2
DEFAULT_RETRY_AFTER = 3.0
#: The longest Retry-After this server waits; a longer one fails at once (retryable).
MAX_RETRY_AFTER = 60.0
RETRY_5XX_AFTER = 1.0
MESSAGE_MAX = 300
#: Alpaca allows 200 requests per minute per key; stay under it whatever the config says.
MAX_PER_MINUTE = 199

R11 = "ALPACA_API_KEY and ALPACA_SECRET_KEY are not set in this server's environment, so Alpaca tools cannot run."
R11_HINT = "Add both to the env block of this server in your MCP client's configuration."
#: The 403 hint by data family (the request path's prefix); anything else has no feed to change.
NOT_ENTITLED_HINTS = (
    (
        "/v2/stocks",
        "Your Alpaca plan may not include this stock feed: set providers.alpaca.stock_feed: iex (or "
        "delayed_sip) in the marketlens config, or ask for older data.",
    ),
    (
        "/v1beta1/options",
        "Your Alpaca plan may not include this options feed: set providers.alpaca.options_feed: indicative "
        "in the marketlens config.",
    ),
)
NOT_ENTITLED_OTHER = "Your Alpaca plan does not include this data set; no marketlens setting will enable it."

Api = Literal["data", "trading"]

#: Test seam: an httpx transport used instead of the network (httpx.MockTransport).
_transport: ContextVar[httpx.AsyncBaseTransport | None] = ContextVar(
    "marketlens_alpaca_transport", default=None
)
_sleep = asyncio.sleep


@contextlib.contextmanager
def use_transport(transport: httpx.AsyncBaseTransport) -> Iterator[None]:
    """Route every AlpacaClient created inside the block through ``transport``."""
    token = _transport.set(transport)
    try:
        yield
    finally:
        _transport.reset(token)


@dataclass(frozen=True)
class AlpacaSettings:
    """``providers.alpaca`` in the marketlens config, with its defaults."""

    stock_feed: str = "iex"
    options_feed: str = "indicative"
    crypto_location: str = "us"
    rate_limit_per_minute: int = 190
    trading_url: str | None = None
    data_url: str | None = None

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> AlpacaSettings:
        m = m or {}
        known = {k: m[k] for k in cls.__dataclass_fields__ if m.get(k) is not None}
        return cls(**known)


def _env(ctx: ToolContext, name: str) -> str | None:
    try:
        value = ctx.env(name)
    except KeyError:  # not declared for this tool: treated as unset
        return None
    return value or None


def _keys(ctx: ToolContext) -> tuple[str, str]:
    for key_name, secret_name in KEY_NAMES:
        key, secret = _env(ctx, key_name), _env(ctx, secret_name)
        if key and secret:
            return key, secret
    raise ToolError("alpaca_keys_missing", R11, hint=R11_HINT)


class AlpacaClient:
    """``async with AlpacaClient(ctx) as api: await api.get("data", path, params)``.

    The only request method is ``get``."""

    def __init__(self, ctx: ToolContext):
        self._ctx = ctx
        self.settings = AlpacaSettings.from_mapping(ctx.settings)
        self._key, self._secret = _keys(ctx)
        key_id = hashlib.sha256(self._key.encode()).hexdigest()[:8]
        per_minute = max(1, min(int(self.settings.rate_limit_per_minute), MAX_PER_MINUTE))
        self._limiter = ctx.limiter(f"alpaca:{key_id}", per_minute)
        self.environment = ctx.portfolio_environment
        live = self.environment == Environment.LIVE
        self._bases = {
            "data": (self.settings.data_url or DATA_URL).rstrip("/"),
            "trading": (
                self.settings.trading_url or (LIVE_TRADING_URL if live else PAPER_TRADING_URL)
            ).rstrip("/"),
        }
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> AlpacaClient:
        self._http = httpx.AsyncClient(
            transport=_transport.get(),
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            headers={
                "APCA-API-KEY-ID": self._key,
                "APCA-API-SECRET-KEY": self._secret,
                "User-Agent": f"marketlens-mcp/{__version__}",
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get(
        self, api: Api, path: str, params: Mapping[str, Any] | None = None, *, decimals: bool = False
    ) -> Any:
        """GET ``path`` on the data or trading API and return the parsed JSON.
        ``decimals=True`` reads JSON numbers as Decimal (exact money)."""
        assert self._http is not None, "use AlpacaClient as an async context manager"
        url = self._bases[api] + path
        query = _query(params or {})
        retries_429 = retries_5xx = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._http.get(url, params=query)
            except httpx.TimeoutException as e:
                raise ToolError(
                    "alpaca_timeout",
                    f"Alpaca did not answer within {TIMEOUT_SECONDS:.0f} seconds.",
                    hint="Retry, or narrow the request (fewer tickers, a shorter window).",
                    retryable=True,
                ) from e
            except httpx.TransportError as e:
                raise ToolError(
                    "alpaca_unavailable",
                    f"Alpaca could not be reached ({type(e).__name__}).",
                    hint="Check the network connection and retry.",
                    retryable=True,
                ) from e
            status = resp.status_code
            if status == 429 and retries_429 < MAX_429_RETRIES:
                wait = _retry_after(resp, self._ctx.now())
                if wait > MAX_RETRY_AFTER:
                    raise ToolError(
                        "alpaca_rate_limited",
                        "Alpaca is rate-limiting these API keys (HTTP 429) and asked to wait longer than "
                        f"{MAX_RETRY_AFTER:.0f} seconds.",
                        hint="Retry later, or lower providers.alpaca.rate_limit_per_minute.",
                        retryable=True,
                    )
                retries_429 += 1
                await _sleep(wait)
                continue
            if status >= 500 and retries_5xx < 1:
                retries_5xx += 1
                await _sleep(RETRY_5XX_AFTER)
                continue
            if 300 <= status < 400:
                raise ToolError(
                    "alpaca_unavailable",
                    f"Alpaca answered with a redirect (HTTP {status}), which this server does not follow.",
                    hint="Check providers.alpaca.trading_url and data_url in the marketlens config.",
                )
            if status >= 400:
                raise self._error(resp, path)
            try:
                return json.loads(resp.content, parse_float=Decimal) if decimals else resp.json()
            except ValueError as e:
                raise ToolError(
                    "alpaca_unavailable",
                    f"Alpaca returned a response that is not JSON (HTTP {status}).",
                    hint="Retry later.",
                    retryable=True,
                ) from e

    def _error(self, resp: httpx.Response, path: str) -> ToolError:
        status = resp.status_code
        message = self._scrub(_upstream_message(resp))
        if status == 429:
            return ToolError(
                "alpaca_rate_limited",
                "Alpaca is rate-limiting these API keys (HTTP 429) after retries.",
                hint="Wait a minute, or lower providers.alpaca.rate_limit_per_minute.",
                retryable=True,
            )
        if status >= 500:
            return ToolError(
                "alpaca_unavailable",
                f"Alpaca returned HTTP {status} twice: {message}",
                hint="Retry later.",
                retryable=True,
            )
        lowered = message.lower()
        if status in (401, 403) and any(w in lowered for w in ("subscription", "permit", "entitle", "feed")):
            return ToolError(
                "alpaca_not_entitled",
                f"Alpaca refused the data (HTTP {status}): {message}",
                hint=not_entitled_hint(path),
            )
        if status in (401, 403):
            return ToolError(
                "alpaca_auth",
                f"Alpaca refused the API keys (HTTP {status}). Check ALPACA_API_KEY and ALPACA_SECRET_KEY, "
                f"and that they belong to the {self.environment.value} account.",
            )
        return ToolError("alpaca_rejected", f"Alpaca rejected the request (HTTP {status}): {message}")

    def _scrub(self, message: str) -> str:
        for secret in (self._key, self._secret):
            message = message.replace(secret, "<redacted>")
        return message[:MESSAGE_MAX]


def not_entitled_hint(path: str) -> str:
    return next((hint for prefix, hint in NOT_ENTITLED_HINTS if path.startswith(prefix)), NOT_ENTITLED_OTHER)


def _query(params: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, list | tuple):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def _retry_after(resp: httpx.Response, now: datetime) -> float:
    """Seconds to wait before retrying a 429: ``Retry-After`` as seconds or as
    an HTTP-date (counted from ``now``); missing, unreadable or non-finite means
    the default; never negative."""
    value = resp.headers.get("Retry-After")
    if value is None:
        return DEFAULT_RETRY_AFTER
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    if not math.isfinite(seconds):
        return DEFAULT_RETRY_AFTER
    return max(0.0, seconds)


def _upstream_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"]).splitlines()[0]
    text = resp.text.strip().splitlines()
    return text[0] if text else f"HTTP {resp.status_code}"
