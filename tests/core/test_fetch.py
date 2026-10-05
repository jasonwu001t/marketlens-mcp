"""ctx.paginate (normative algorithm in plugin_api) and ctx.limiter."""

from __future__ import annotations

import pytest
from coresupport import run

from marketlens_mcp import fetch, ratelimit
from marketlens_mcp.plugin_api import FetchLimits, RateLimiter
from marketlens_mcp.testing import make_context


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
