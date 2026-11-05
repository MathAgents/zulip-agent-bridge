"""One rolling request budget shared by event polls and outbound API calls."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable
from typing import Any

from zab.core.watchdog import waiting


class RequestBudget:
    def __init__(self, per_minute: int = 150) -> None:
        self.limit = per_minute
        self.sent: deque[float] = deque()
        self.blocked_until = 0.0
        # A test's fake clock: `monotonic()`, `time()` and `async sleep(delay)`.
        self.clock: Any = None

    def _monotonic(self) -> float:
        return self.clock.monotonic() if self.clock else time.monotonic()

    def wall(self) -> float:
        """The wall clock that `blocked_until` is measured on (a test clock, if set)."""
        return self.clock.time() if self.clock else time.time()

    def sleep(self, delay: float) -> Awaitable[None]:
        """`delay` from now: a fake clock fixes the deadline here, not when awaited."""
        return self.clock.sleep(delay) if self.clock else asyncio.sleep(delay)

    def free(self) -> int:
        """Requests the window still allows now."""
        now = self._monotonic()
        while self.sent and self.sent[0] <= now - 60:
            self.sent.popleft()
        return self.limit - len(self.sent)

    def delay(self, reserve: int = 0) -> float:
        """Seconds until a request fits in the window, 0 if it fits now. `reserve`: leave
        that many of the window's requests to everyone else, as housekeeping does for
        replies (SPEC §4.10)."""
        now = self._monotonic()
        while self.sent and self.sent[0] <= now - 60:
            self.sent.popleft()
        delay = max(0, self.blocked_until - self.wall())
        allowed = max(1, self.limit - max(0, reserve))
        if len(self.sent) >= allowed:
            # The request `allowed` places from the newest must leave the window first.
            delay = max(delay, self.sent[len(self.sent) - allowed] + 60 - now)
        return delay

    async def wait(self, reserve: int = 0) -> None:
        """Until a request fits in the window, keeping `reserve` (see `delay`)."""
        while (delay := self.delay(reserve)) > 0:
            await waiting(self.sleep(delay))

    async def acquire(self) -> None:
        await self.wait()
        # No await between availability check and debit: concurrent polls/posts
        # share the limit without holding a lock across the long-poll request.
        self.sent.append(self._monotonic())
