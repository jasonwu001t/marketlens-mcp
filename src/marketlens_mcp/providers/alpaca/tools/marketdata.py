"""Fetch helpers shared by the stock, crypto and option tools (market data API)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from marketlens_mcp.plugin_api import PageResult, ToolContext

from ..client import AlpacaClient

#: Upstream page size ceiling of the historical bars, quotes and trades endpoints.
HISTORY_PAGE_MAX = 10_000

GroupMapper = Callable[[str, list[Any]], list[Any]]


async def fetch_grouped(
    ctx: ToolContext,
    api: AlpacaClient,
    *,
    path: str,
    params: Mapping[str, Any],
    key: str,
    mapper: GroupMapper,
    page_max: int = HISTORY_PAGE_MAX,
    start_token: str | None = None,
) -> PageResult:
    """Page through ``{key: {symbol: [records]}, next_page_token}`` responses.
    The upstream page size is what the caller may still take, capped at
    ``page_max``, so no page is ever cut in half."""

    async def fetch_page(token: str | None, limit: int) -> tuple[list[Any], str | None]:
        data = await api.get("data", path, {**params, "limit": min(limit, page_max), "page_token": token})
        rows: list[Any] = []
        for symbol, records in (data.get(key) or {}).items():
            rows.extend(mapper(symbol, list(records or [])))
        return rows, data.get("next_page_token") or None

    return await ctx.paginate(fetch_page, start_token=start_token)


async def fetch_once(ctx: ToolContext, fetch: Callable[[], Any]) -> PageResult:
    """A single-response endpoint, still passed through ``ctx.paginate`` so the
    row cap applies (rows beyond fetch.max_rows are cut and reported)."""

    async def fetch_page(token: str | None, limit: int) -> tuple[list[Any], str | None]:
        return await fetch(), None

    return await ctx.paginate(fetch_page)
