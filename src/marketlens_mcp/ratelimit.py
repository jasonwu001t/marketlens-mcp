"""Process-wide token buckets (``ToolContext.limiter``).

A bucket for ``per_minute`` requests holds ``per_minute`` tokens and refills
``per_minute / 60`` tokens per second. One bucket per key (per upstream API
key or host), created on first use and shared by every session.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self.per_minute = per_minute
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self) -> None:
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            await self._sleep(wait)


_LIMITERS: dict[str, TokenBucket] = {}
_LOCK = threading.Lock()


def get_limiter(key: str, per_minute: int) -> TokenBucket:
    """The bucket for ``key``, created with ``per_minute`` on first use."""
    with _LOCK:
        bucket = _LIMITERS.get(key)
        if bucket is None:
            bucket = _LIMITERS[key] = TokenBucket(per_minute)
        return bucket
