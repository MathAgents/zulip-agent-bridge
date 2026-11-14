"""A wait or a check that failed on the rig's side, not the bridge's (#877).

Two causes were seen. The rig's own connection to Zulip failed during a wait: its calls timed
out or could not connect (`TestUser.retrying`) while the bots on the VM posted and edited
throughout. And Zulip Cloud served an edited message's older version from `GET /messages`,
while the last entry of `GET /messages/{id}/history` had the edit.

So a wait that times out says for how long the rig's calls failed (`Outage`). A `wait_reply`
that times out reads, once the connection is back, the edit history of the messages it waited
on: a version that matched before the deadline, and still shows or showed for longer than the
rig's polls are apart (`shown`), means the bridge did its part and the rig never saw it, a
`RigTimeout`. So is a wait whose calls still failed at its deadline, when Zulip couldn't be
reached after it: the rig couldn't look. A check of a message's final state
(`TestUser.settled`) that fails on what Zulip served reads the last entry of the message's edit
history, and a disagreement in which that entry passes, from an edit made before the read, is
a `RigFailure`. Zulip's timestamps are set beside the rig's clock with `SKEW_S` to spare, and
one missing is from an unknown time (`stamp`), so that a doubt stays the bridge's failure.

A part that fails so runs once more, with a fresh nonce, after `WAIT_S` (`refusal.retry`, from
`conftest.pytest_runtest_call`), and its row names the cause (`ScenarioResult.external`). A
second rig failure is no result for the part (a skip, so the run's verdict is NO-RESULT), never
a failure: the bridge was not seen to fail.

A wait that succeeds after its calls failed for a while says so too (`Waited`, #943): a timing
that ends at its result includes the run of failed calls just before it, in which the rig
couldn't look (`Waited.blind`), and `Timing.outage` keeps those seconds beside the bounds.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any

#: How long before a part that failed on the rig's side runs again: the connection came back
#: within seconds on Nov 6 (#877)
WAIT_S = 30.0
WHY = "a rig failure, not the bridge's (#877)"
#: Zulip's timestamps are whole seconds on Zulip's clock, set beside the rig's: one counts as
#: before a time of the rig's only when it is this much earlier, so that a doubt stays the
#: bridge's failure. A second for the whole seconds, and one for the clocks, both kept by NTP
SKEW_S = 2.0


class RigFailure(Exception):
    """A part failed on the rig's side: `line` says how."""

    label = "a rig failure"  # `refusal.retry`'s word for it

    def __init__(self, line: str) -> None:
        super().__init__(line)
        self.line, self.why = line, ""  # `refusal.retry`: why the part wasn't run again

    @property
    def wait_s(self) -> float:
        return WAIT_S  # the module's, read late: a test sets it

    def __str__(self) -> str:
        return f"{WHY}{', ' + self.why if self.why else ''}: {self.line}"


class RigTimeout(RigFailure, TimeoutError):
    """A wait that timed out on the rig's side: still a TimeoutError, for callers that ask again
    after one."""


def utc(epoch: float) -> str:
    return (
        time.strftime("%H:%M:%SZ", time.gmtime(epoch))
        if math.isfinite(epoch)
        else "an unknown time"
    )


def earlier(zulip: float, rig: float) -> bool:
    """Zulip's timestamp `zulip` was surely before the rig's time `rig` (epoch seconds both)."""
    return zulip + SKEW_S <= rig


def stamp(version: dict[str, Any]) -> float:
    """A version's `timestamp`; one without is from an unknown time, never `earlier`."""
    value = version.get("timestamp")
    return float(value) if isinstance(value, int | float) else math.inf


@dataclass
class Outage:
    """How long the rig's calls to Zulip failed during a wait from `start` for `within` seconds
    (`TestUser.retrying`). A run of failures lasts from the start of its first failed call to the
    start of the next call that is answered, counted up to the wait's end."""

    start: float
    within: float
    lost: float = 0.0
    errors: int = 0
    last: str = ""  # the last error
    since: float | None = None  # when the run of failures still going began
    #: When the last answered call began, and when the run of failures it ended began (None
    #: when the call before it was answered too)
    answered: float | None = None
    ended: float | None = None

    @property
    def end(self) -> float:
        return self.start + self.within

    @property
    def down(self) -> bool:
        """The last call failed."""
        return self.since is not None

    def failed(self, began: float, error: str) -> None:
        self.errors, self.last = self.errors + 1, error
        if self.since is None:
            self.since = began

    def back(self, began: float) -> None:
        self.answered, self.ended = began, self.since
        if self.since is not None:
            self.lost += max(0.0, min(began, self.end) - self.since)
            self.since = None

    def text(self, now: float) -> str:
        """For example `the rig's Zulip connection failed for 125 s of the 240 s wait (5 errors,
        the last ConnectError)`, then `, still at its end` when the last call failed; "" when no
        call failed."""
        if not self.errors:
            return ""
        lost = self.lost + (max(0.0, min(now, self.end) - self.since) if self.down else 0.0)
        return (
            f"the rig's Zulip connection failed for {lost:.0f} s of the {self.within:.0f} s wait "
            f"({self.errors} error{'s' if self.errors > 1 else ''}, the last {self.last})"
            + (", still at its end" if self.down else "")
        )


@dataclass(frozen=True)
class Waited:
    """A wait that found what it waited for (`TestUser.wait_for`, #943), on the rig's clock:
    from `start` to `found`, when the call that found it began; `retried`, the seconds its calls
    to Zulip failed and were retried (`Outage`: connection errors, timeouts, 429s, 5xx), in
    `errors` errors; and `blind_from`, when the run of failed calls that the finding call ended
    began, or None when the call before it was answered."""

    start: float
    found: float
    retried: float = 0.0
    errors: int = 0
    blind_from: float | None = None

    @classmethod
    def of(cls, outage: Outage) -> Waited:
        found = outage.answered if outage.answered is not None else outage.start
        return cls(outage.start, found, outage.lost, outage.errors, outage.ended)

    def blind(self, since: float) -> float:
        """The seconds between `since` (the start of a timing, on the rig's clock) and the
        result in which the rig couldn't look: the run of failed calls just before the call that
        found it. What the timing measured may have come that much earlier, and no earlier: an
        answered poll before the run didn't find it. A run of failures that an answered poll
        followed delayed nothing, and counts for none."""
        if self.blind_from is None:
            return 0.0
        return max(0.0, self.found - max(self.blind_from, since))

    def text(self) -> str:
        """For example `found after 95 s, 40 s of them in retries (3 errors), 30 s of those
        just before it`."""
        text = f"found after {self.found - self.start:.0f} s"
        if not self.errors:
            return text
        plural = "s" if self.errors > 1 else ""
        blind = self.blind(self.start)
        return f"{text}, {self.retried:.0f} s of them in retries ({self.errors} error{plural})" + (
            f", {blind:.0f} s of those just before it" if blind else ""
        )


def shown(
    history: list[dict[str, Any]],
    regex: re.Pattern[str],
    *,
    since: float,
    before: float,
    every: float,
) -> str | None:
    """When a version in a message's edit history (`history`, oldest first, as `GET
    /messages/{id}/history` gives it) matched `regex`, began to show before `before`, the rig's
    deadline (`earlier`), and either still shows or showed after `since`, the wait's start, for
    `every` (the rig's poll interval) and `SKEW_S` at each end: so long that a rig whose calls
    were answered and served it would have seen it. "matched from 08:40:22Z to 08:41:57Z by its
    edit history", or None. A version shorter than that, between two polls, is no sign that
    the rig missed it (#877)."""
    for i, version in enumerate(history):
        began = stamp(version)
        if not earlier(began, before):
            return None
        if not regex.search(str(version.get("content", ""))):
            continue
        if i + 1 == len(history):
            return f"matched from {utc(began)} on by its edit history"
        ended = stamp(history[i + 1])
        if math.isfinite(ended) and ended - max(began, since) >= every + 2 * SKEW_S:
            return f"matched from {utc(began)} to {utc(ended)} by its edit history"
    return None
