"""Shared pieces of the `ClaudeBackend` tests (SPEC §15.2): the fake binary, the recorded
fixtures, a backend factory and an event drain. The `home` fixture lives in `conftest.py`."""

from __future__ import annotations

import asyncio
import json
import pathlib

from zab.backends import claude as cb
from zab.core.types import SessionSettings, TurnCompleted

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "claude" / cb.PINNED_VERSION
FAKE = pathlib.Path(__file__).resolve().parents[2] / "src" / "zab" / "testing" / "fake_claude.py"


def make_backend(home, **kw) -> cb.ClaudeBackend:
    config = cb.ClaudeConfig(
        binary=str(FAKE),
        accounts={"test": str(home["account"])},
        default_account="test",
        defaults=SessionSettings(model="haiku", approval="bypassPermissions"),
        **kw,
    )
    return cb.ClaudeBackend("claude", config)


def calls(home) -> list[dict]:
    """The fake's call log: one record per `claude` invocation."""
    return [json.loads(line) for line in home["log"].read_text().splitlines()]


async def drain(backend: cb.ClaudeBackend, until=TurnCompleted, *, extra: int = 0) -> list:
    """Collect events up to and including the first `until`, plus `extra` more."""
    events = []
    async for ev in backend.events():
        events.append(ev)
        if isinstance(ev, until):
            for _ in range(extra):
                events.append(await asyncio.wait_for(backend._events.get(), 5))
            break
    return events
