"""ctx.paginate (normative algorithm in plugin_api) and ctx.limiter."""

from __future__ import annotations

import pyarrow as pa
import pytest
from coresupport import provenance, run

from marketlens_mcp import fetch, ratelimit
from marketlens_mcp.offload import finalize
from marketlens_mcp.plugin_api import FetchLimits, RateLimiter, ToolOutput
from marketlens_mcp.results.store import StoreRoot
from marketlens_mcp.testing import make_context
from marketlens_schema import QUERY_ROW_SCHEMA_NAME


def pages_of(sizes, last_token=None):
    calls = []

    async def fetch_page(token, page_limit):
        i = 0 if token is None else int(token)
        calls.append((token, page_limit))
        n = min(sizes[i], page_limit) if i < len(sizes) else 0
        nxt = str(i + 1) if i + 1 < len(sizes) else last_token
        return list(range(n)), nxt

    return fetch_page, calls


def test_until_exhausted():
    fp, calls = pages_of([3, 3, 2])
    res = run(fetch.paginate(fp, FetchLimits(max_rows=100, max_pages=20)))
    assert len(res.rows) == 8 and res.pages_fetched == 3 and res.complete
    assert calls[0] == (None, 100) and calls[1] == ("1", 97)
    st = res.state()
    assert (st.complete, st.rows_fetched, st.row_cap_hit, st.page_cap_hit) == (True, 8, False, False)


def test_row_cap():
    fp, _ = pages_of([5, 5, 5, 5])
    res = run(fetch.paginate(fp, FetchLimits(max_rows=10, max_pages=20)))
    assert len(res.rows) == 10 and res.row_cap_hit and not res.complete
    assert res.next_page_token == "2"


def test_page_cap():
    fp, _ = pages_of([1] * 30)
    res = run(fetch.paginate(fp, FetchLimits(max_rows=1000, max_pages=20)))
    assert res.pages_fetched == 20 and res.page_cap_hit and res.next_page_token == "20"


def test_upstream_ignoring_page_limit_is_cut():
    async def greedy(token, page_limit):
        return list(range(50)), "more"

    res = run(fetch.paginate(greedy, FetchLimits(max_rows=10, max_pages=5)))
    assert len(res.rows) == 10 and res.row_cap_hit


def test_start_token():
    fp, calls = pages_of([2, 2, 2])
    res = run(fetch.paginate(fp, FetchLimits(), start_token="1"))
    assert calls[0][0] == "1" and len(res.rows) == 4


def test_context_paginate_uses_its_limits():
    ctx = make_context(limits=FetchLimits(max_rows=4, max_pages=20))
    fp, _ = pages_of([3, 3, 3])
    res = run(ctx.paginate(fp))
    assert len(res.rows) == 4 and res.row_cap_hit


def test_token_bucket_spaces_requests():
    clock = {"t": 0.0}
    slept = []

    async def sleep(s):
        slept.append(s)
        clock["t"] += s

    bucket = ratelimit.TokenBucket(60, clock=lambda: clock["t"], sleep=sleep)

    async def go():
        for _ in range(61):
            await bucket.acquire()

    run(go())
    assert slept and sum(slept) == pytest.approx(1.0, rel=1e-6)  # 60/min refills one per second


def test_limiter_is_process_wide_per_key():
    a = ratelimit.get_limiter("test:key", 120)
    assert ratelimit.get_limiter("test:key", 120) is a
    assert ratelimit.get_limiter("test:other", 120) is not a
    assert isinstance(a, RateLimiter)
    assert make_context().limiter("test:key", 120) is a


def test_rows_cut_from_a_last_page_leave_the_fetch_incomplete_and_flagged(tmp_path):
    """An upstream that ignored page_limit on a page with no next token was
    cut at fetch.max_rows but reported complete, so the middleware's
    truncation note never fired and provenance.truncated stayed false."""

    async def single(token, page_limit):
        return list(range(10)), None

    limits = FetchLimits(max_rows=5, max_pages=20)
    ctx = make_context(limits=limits)
    page = run(ctx.paginate(single))
    state = page.state()
    assert (state.complete, state.rows_fetched, state.row_cap_hit, state.next_page_token) == (
        False,
        5,
        True,
        None,
    )
    out = finalize(
        ToolOutput(
            model=QUERY_ROW_SCHEMA_NAME,
            provenance=provenance(),
            table=pa.table({"n": page.rows}),
            pagination=state,
        ),
        tool="plugin_tool",
        default_risk="api_structured",
        store=StoreRoot(tmp_path / "cache").session("s"),
        limits=limits,
    )
    assert out.provenance.truncated is True
    assert out.notes == [
        "Stopped after 1 pages and 5 rows (limits fetch.max_pages=20, fetch.max_rows=5). The data is incomplete; "
        "narrow the request."
    ]
