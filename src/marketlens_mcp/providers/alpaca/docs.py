"""Client for Alpaca's public documentation MCP server (capability
provider.docs, off by default). An outbound call to a third party; every
result is third-party text. No API keys are sent.

The fastmcp client is created only when a docs tool is called (never at import
or registration). Its result shape is not part of any spec we pin, so records
are read defensively: a list of objects under a common key, or one object.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

from marketlens_mcp.plugin_api import ToolContext, ToolError

DOCS_URL = "https://docs.alpaca.markets/mcp?project=us"
RATE_LIMIT_KEY = "alpaca-docs"
RATE_PER_MINUTE = 60
FALLBACK_HINT = (
    "Read https://docs.alpaca.markets/llms.txt or https://docs.alpaca.markets/us/reference instead."
)
#: The spec titles the endpoint tools may read (the Broker API is excluded).
API_TITLES = {
    "trading": "Trading API",
    "market_data": "Market Data API",
    "authentication": "Authentication API",
}

_factory: ContextVar[Callable[[], Any] | None] = ContextVar("marketlens_alpaca_docs_factory", default=None)


@contextlib.contextmanager
def use_client_factory(factory: Callable[[], Any]) -> Iterator[None]:
    """Test seam: build docs clients with ``factory`` inside the block."""
    token = _factory.set(factory)
    try:
        yield
    finally:
        _factory.reset(token)


def _default_client() -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    return Client(StreamableHttpTransport(url=DOCS_URL))


async def call(ctx: ToolContext, tool: str, arguments: Mapping[str, Any]) -> Any:
    """Call one tool of the docs server and return its parsed payload."""
    await ctx.limiter(RATE_LIMIT_KEY, RATE_PER_MINUTE).acquire()
    factory = _factory.get() or _default_client
    try:
        async with factory() as client:
            result = await client.call_tool(tool, dict(arguments), raise_on_error=False)
    except Exception as e:  # any transport or protocol failure; the message may name local paths
        raise ToolError(
            "docs_unavailable",
            f"Alpaca's documentation service could not be reached ({type(e).__name__}).",
            hint=FALLBACK_HINT,
            retryable=True,
        ) from e
    payload = _payload(result)
    if getattr(result, "is_error", False):
        first = str(payload if not isinstance(payload, dict) else payload.get("text", payload)).splitlines()
        raise ToolError(
            "docs_error",
            f"Alpaca's documentation service returned an error: {(first or [''])[0][:300]}",
            hint=FALLBACK_HINT,
        )
    return payload


def _payload(result: Any) -> Any:
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = [getattr(block, "text", None) for block in getattr(result, "content", None) or []]
    texts = [t for t in texts if t is not None]
    if not texts:
        return None
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return {"text": joined}


_LIST_KEYS = ("results", "hits", "pages", "items", "endpoints", "result", "data", "specs")


def records(payload: Any) -> list[dict[str, Any]]:
    """The list of objects in a docs payload (or the payload as one object)."""
    if isinstance(payload, list):
        return [p if isinstance(p, dict) else {"text": str(p)} for p in payload]
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            if isinstance(payload.get(key), list):
                return records(payload[key])
        return [payload]
    if payload is None:
        return []
    return [{"text": str(payload)}]


def pick(record: Mapping[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = record.get(k)
        if v not in (None, ""):
            return str(v)
    return None
