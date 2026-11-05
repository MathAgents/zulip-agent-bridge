"""A still clock for the request budget in offline tests (`RequestBudget.clock`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


class FrozenClock:
    """Time moves only when the test advances it, so a wait on the budget shows."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self._tick = asyncio.Event()

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, delay: float) -> Awaitable[None]:
        """The deadline is `delay` from the call, however late the sleep is awaited."""
        deadline = self.now + delay

        async def until_deadline() -> None:
            while self.now < deadline:
                await self._tick.wait()

        return until_deadline()

    def advance(self, seconds: float) -> None:
        self.now += seconds
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()
