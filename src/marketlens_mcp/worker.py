"""Blocking work off the event loop.

The result store's work is synchronous: DuckDB queries, Parquet reads and
writes, the store lock (which waits with ``time.sleep``) and eviction. Run on
the event loop, one slow query froze every other tool call, the MCP
session's messages and the upstream fetches in flight until it finished or
timed out. Handlers of that kind are written as plain functions and wrapped
here into the async handlers the manifest needs:

* ``query_handler``: DuckDB work over stored results (``results_query``,
  ``results_sample``, the analytics tools) runs in one query thread, one call
  at a time, so concurrent queries never multiply
  ``results.query_memory_limit``. Storing an answer is not queued there: its
  summary, a DuckDB connection under the same limit, runs in the thread that
  stores it (``pipeline.execute`` stores a tool's answer in the default pool),
  beside a query and other stores.
* ``thread_handler``: other file work (list, describe, drop, export) runs in
  the loop's default thread pool, so it is not queued behind a slow query.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

_QUERIES = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marketlens-query")


async def run_query(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` in the query thread, with this task's context."""
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_QUERIES, call)


def query_handler(fn: Callable[[Any, Any], T]) -> Callable[[Any, Any], Awaitable[T]]:
    """A tool handler ``fn(ctx, args)`` run in the query thread."""

    @functools.wraps(fn)
    async def handler(ctx: Any, args: Any) -> T:
        return await run_query(fn, ctx, args)

    return handler


def thread_handler(fn: Callable[[Any, Any], T]) -> Callable[[Any, Any], Awaitable[T]]:
    """A tool handler ``fn(ctx, args)`` run in the default thread pool."""

    @functools.wraps(fn)
    async def handler(ctx: Any, args: Any) -> T:
        return await asyncio.to_thread(fn, ctx, args)

    return handler
