"""A Codex turn that OpenAI refused with 429 Too Many Requests: an external cause (#842).

Codex 0.157.1 retries no 429 whose body isn't a usage limit: it ends the turn at once with
"exceeded retry limit, last status: 429 Too Many Requests", before any output. The bridge then
says so in its own words, in the turn's answer (`⚠️ **Bridge** (not Codex): network: …`) or
in a side question's (`… Codex couldn't answer (network): …`). A wait that sees that line from
the bot it waits for raises `Refused` at once (`TestUser.wait_reply`), not at its deadline.

The part (test function) it happened in runs once more, after `WAIT_S`, with a fresh nonce
(`retry`, from `conftest.pytest_runtest_call`). The scenario's row names the cause either way
(`ScenarioResult.external`), so a part that passed on its second run is no silent pass, and a
part refused twice fails. So does a refused part once the run has spent its `RETRIES` (a
`Budget`): a broad throttle costs a run at most that many waits.

A refused run stops where it was refused: its later checks (and its clean-up after them) don't
run, and the second run checks its own, under its new nonce. Its notes stay in the row, marked
`first run:`; its timings go, as the second run measures them again.

A part that failed on the rig's side (`rig_failure.RigFailure`, #877) runs once more the same
way, after that module's own `WAIT_S`, on a budget of its own (`conftest`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from e2e.harness.report import ScenarioResult
from e2e.harness.rig_failure import RigFailure

#: The bridge's line for a Codex request that OpenAI refused with a 429 (`publish_completion`,
#: `CodexBackend._side_answer`): its note, on a line of its own.
REFUSED_RE = re.compile(
    r"^⚠️ \*\*Bridge\*\* \(not Codex\): [^\n]*\b429 Too Many Requests[^\n]*", re.M
)
#: How long before the part runs again. On Nov 2 and 5 every refused request was a Codex
#: thread's first, at the suite's peak of six Codex turns and side questions begun within 60 s;
#: none begun with five or fewer was refused (#842). Two minutes take the earlier parts' out.
WAIT_S = 120.0
#: How many refused parts a run runs again; after that, a refusal fails its part at once.
RETRIES = 3
WHY = "OpenAI refused a Codex request with 429 Too Many Requests, an external cause (#842)"


class Budget:
    """The refused parts a run may still run again (`RETRIES`): one per run, in `conftest`."""

    def __init__(self, left: int = RETRIES) -> None:
        self.left = left


class Refused(Exception):
    """The bot's message refusing a Codex request with a 429: `line` is the bridge's."""

    label = "refused"  # `retry`'s word for it

    def __init__(self, line: str) -> None:
        super().__init__(line)
        self.line, self.why = line, ""  # `retry`: why the part wasn't (or isn't) run again

    @property
    def wait_s(self) -> float:
        return WAIT_S  # the module's, read late: a test sets it

    @property
    def twice(self) -> bool:
        return self.why == AGAIN

    def __str__(self) -> str:
        return f"{WHY}{', ' + self.why if self.why else ''}: {self.line}"


AGAIN = "in the part's second run too"
SECOND = "in the part's second run"  # after the other cause in its first
SPENT = f"not run again: the run's {RETRIES} retries are spent"


def refusal(content: str) -> str | None:
    """The bridge's line in `content` saying a Codex request was refused with a 429, or None."""
    found = REFUSED_RE.search(content)
    return found[0] if found else None


def retry(
    name: str,
    first: Refused | RigFailure,
    row: ScenarioResult,
    *,
    kept: int,
    noted: int,
    budget: Budget,
    run: Callable[[], object],
    renew: Callable[[], None] = lambda: None,
    sleep: Callable[[float], object] = time.sleep,
) -> None:
    """Part `name` was refused, or failed on the rig's side (`first`): name the cause in `row`.
    With the `budget` spent, raise `first` at once. Else mark the notes the first run wrote
    (those after the first `noted`) as the first run's, drop its timings (after the first
    `kept`), wait `first.wait_s`, `renew` its inputs (a fresh nonce: new topics) and `run` it
    once more. A second refusal or rig failure is raised, marked `AGAIN` after the same cause
    (`twice`); any other failure of the second run is its own."""
    if budget.left <= 0:
        first.why = SPENT
        row.external.append(f"{name}: {first.line}; {SPENT}")
        raise first
    budget.left -= 1
    del row.timings[kept:]
    row.notes[noted:] = [f"first run: {note}" for note in row.notes[noted:]]
    row.external.append(f"{name}: {first.line}; the part ran again after {first.wait_s:.0f} s")
    sleep(first.wait_s)
    renew()
    try:
        run()
    except (Refused, RigFailure) as second:
        same = isinstance(second, Refused) == isinstance(first, Refused)
        second.why = AGAIN if same else SECOND
        again = " again" if same else ""
        row.external.append(f"{name}: {second.label}{again} in its second run: {second.line}")
        raise
