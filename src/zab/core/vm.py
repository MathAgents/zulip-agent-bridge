"""`!vm` and the `!usage` VM line, for topic and DM commands (SPEC §4.6; issue #204).

`!vm` is `vmstat.format_report`: the host, a line per resource, then one status line in this order:
turns running, uptime, agent sign-in, version (PM spec on #204, Oct 13), e.g.
`1 turn running · up 2 hours · Codex and Claude signed in · zab 0.2.1`. The processes using the
most are shown only in a DM.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from typing import TYPE_CHECKING

from zab import __version__
from zab.core.reason import asked_by
from zab.tools import vmstat

if TYPE_CHECKING:
    from zab.core import types as t
    from zab.core.bridge import BotBridge

#: Seconds of `/proc/stat` that `!vm` samples for the CPU share, and that `!usage` samples
#: while the backend reports its usage: a quarter second still resolves 0.5% of 8 CPUs, and
#: keeps `!usage` quick when the backend answers at once (Claude reads a local file). The test
#: conftest sets both to 0, so tests that reach the real collector take the load average.
SAMPLE_S = 1.0
USAGE_SAMPLE_S = 0.25
#: `signed_in`: the longest the routing of a message without a mention waits for the sign-in
#: of an agent it would start a session of (#687, #713), and the window after the bridge's
#: start in which it waits longer for its own: a Codex daemon still connecting after a wake
#: (its reconnects back off 1, 2, 4, 8 s) answers once connected, so that a Codex never
#: signed in stays silent (`BotBridge.siblings`).
SIGNED_IN_WAIT_S, SIGNED_IN_OWN_WAIT_S = 1.0, 15.0
#: How long `BotBridge.siblings` reuses an agent's sign-in answer, unknown included: a catch-up's
#: messages without a mention, while a Codex daemon reconnects, wait for it once (#713).
SIGNED_IN_CACHE_S = 5.0


async def collect(paths: Sequence[str], *, top: int, sample: float | None = None) -> vmstat.VmStat:
    """`vmstat.collect` off the event loop: it reads `/proc` and sleeps for its sample
    (`SAMPLE_S` unless given)."""
    sample = SAMPLE_S if sample is None else sample
    return await asyncio.to_thread(vmstat.collect, paths, top=top, sample=sample)


async def summary(paths: Sequence[str]) -> str:
    """The `!usage` line: `VM: 0.4 of 8 CPUs · memory 10% · disk 41%`, from the short sample."""
    return vmstat.summary_line(await collect(paths, top=0, sample=USAGE_SAMPLE_S))


def turns_text(running: int) -> str:
    if running == 0:
        return "no turns running"
    return f"{running} turn{'' if running == 1 else 's'} running"


def sign_in_text(states: Sequence[tuple[str, str]]) -> str:
    """`Codex and Claude signed in` when the agents agree, else
    `Codex signed in · Claude not signed in`."""
    if len({state for _, state in states}) == 1:
        names = list(dict.fromkeys(name for name, _ in states))
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"{joined} {states[0][1]}"
    return " · ".join(f"{name} {state}" for name, state in states)


async def sign_in(backend: t.Backend) -> str:
    """`signed in`, `not signed in`, or `sign-in status unavailable` (a failed or slow check
    is not evidence of being signed out)."""
    try:
        async with asyncio.timeout(2):
            with asked_by("!vm"):
                checks = await backend.doctor()
    except Exception:
        return "sign-in status unavailable"
    accounts = [
        c for c in checks if c.name == "codex:login" or c.name.startswith("claude account ")
    ]
    if not accounts:
        return "sign-in status unavailable"
    return "signed in" if any(c.ok for c in accounts) else "not signed in"


async def signed_in(backend: t.Backend, wait: float | None = None) -> bool | None:
    """The agent's sign-in as its backend knows it (its optional `signed_in`, #687): None when
    unknown, when the backend has no such hook, or after `wait` (`SIGNED_IN_WAIT_S`)."""
    hook = getattr(backend, "signed_in", None)
    if hook is None or not inspect.iscoroutinefunction(hook):
        return None
    try:
        async with asyncio.timeout(SIGNED_IN_WAIT_S if wait is None else wait):
            state = await hook()
    except Exception:
        return None
    return state if isinstance(state, bool) else None


async def report(bridge: BotBridge, *, dm: bool) -> str:
    paths = ("/", *(p.path for p in bridge.config.projects))
    bridges = bridge.all_bridges or [bridge]
    backends = list({id(b.backend): b.backend for b in bridges}.values())

    async def stat() -> vmstat.VmStat | None:
        try:
            return await collect(paths, top=3 if dm else 0)
        except Exception:
            return None

    found, states = await asyncio.gather(
        stat(), asyncio.gather(*(sign_in(backend) for backend in backends))
    )
    running = sum(bool(a.turn) for b in bridges for a in b.actors.values())
    status = [turns_text(running)]
    if found is not None and (up := vmstat.uptime_text(found)):
        status.append(up)
    names = [b.kind.capitalize() for b in backends]
    status.append(sign_in_text(list(zip(names, states, strict=True))))
    status.append(f"zab {__version__}")
    if found is None:
        return "VM statistics unavailable; try !vm again.\n" + " · ".join(status)
    return vmstat.format_report(found, status=status, totals_only=not dm)
