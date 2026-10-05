"""Upstream pagination with the fetch ceilings (``ToolContext.paginate``).

The algorithm is normative (see ``ToolContext.paginate`` in plugin_api): stop
when the upstream has no next token, or at ``max_rows`` rows, or at
``max_pages`` pages, whichever comes first. A stopped fetch keeps its token so
the model can continue it; the middleware adds the R16 note.
"""

from __future__ import annotations

from typing import TypeVar

from .plugin_api import FetchLimits, FetchPage, PageResult

T = TypeVar("T")


async def paginate(
    fetch_page: FetchPage[T], limits: FetchLimits, *, start_token: str | None = None
) -> PageResult[T]:
    rows: list[T] = []
    token = start_token
    pages = 0
    row_cap_hit = page_cap_hit = False
    while True:
        remaining = limits.max_rows - len(rows)
        page, token = await fetch_page(token, remaining)
        pages += 1
        rows.extend(page)
        if len(rows) > limits.max_rows:
            del rows[limits.max_rows :]
            row_cap_hit = True
        if token is None:
            break
        if len(rows) >= limits.max_rows:
            row_cap_hit = True
            break
        if pages >= limits.max_pages:
            page_cap_hit = True
            break
    return PageResult(
        rows=rows,
        pages_fetched=pages,
        next_page_token=token,
        row_cap_hit=row_cap_hit,
        page_cap_hit=page_cap_hit,
    )
