"""Scenario 17, part 7 (#668): Claude's notes while it waits on a background sub-agent.

Three topics at once, each a turn in which `zab-tester` asks Claude to give a thinking task (a
check of an elliptic curve, a proof, a class number count) to a sub-agent in the background and
to wait for its findings. The sub-agent's task also runs a marked `python3` sleep of 100 s
twice, a stand-in for a long computation, so that it runs for several minutes, and its log
stays the same for minutes at a time, as #668's 171 s gap had it ("three log checks with no
note between"). The prompts say nothing of notes: the installed instructions are under test.

When each turn has answered, the part reads its session's transcript and measures, from the
sub-agent's launch to its notification (the one naming the launch's call, taken in after the
check it arrived during: `transcript`), the gaps between Claude's notes (its texts, the first
gap from the launch and the last to the notification), and counts the checks (model calls with
tool calls) made right after another with no note between. Every gap must be at most 65 s
(#668's criterion), and each topic's gaps go into the report. In a release candidate's run
(`run.py --require rc`, which sets `ZAB_E2E_RC=1`) the bound is a target, shown but never a
failure: the model's cadence varies, and the instructions' words are pinned offline. A topic
with no background sub-agent, no notification or too short a wait fails in any run.

The status message can't measure this: since #754 the bridge edits it at most once a minute and
refreshes its notes every `status_edit_interval_s` (300 s on a workshop's VMs), so the part only
records when each version of it was made (`GET /messages/{id}/history`), as what the
participant saw.

The part skips when the installed instructions lack #668's text. Its clean-up, per topic, is
part 1's: `!stop` if the turn may still run, then what is left of the command is ended.

About 8 minutes, the three topics side by side.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
from dataclasses import dataclass
from typing import Any

import pytest

from e2e.harness import installed
from e2e.harness.cleanup import Leftover
from e2e.harness.flows import Chat, answer, topic_name
from e2e.harness.rig_failure import RigFailure
from e2e.test_scenarios_subagents import Entry, claude_entries, clean_up

pytestmark = pytest.mark.e2e
#: #668's bound on the gap between two notes
GAP_S = 65.0
#: Set by `run.py --require rc`: the bound is then a target
RC_ENV = "ZAB_E2E_RC"
#: The shortest wait that measures anything: several checks at 30 to 50 s
WAIT_S = 150.0
#: The sub-agent's stand-in for a long computation, run twice: each run within the Bash
#: tool's default timeout of 2 minutes
SLEEP_S = 100
#: How long a turn may take: the sub-agent's work and its two sleeps, then the answer
TURN_S = 900.0
#: The topics' thinking tasks, as #659's sanity check had them (DECISIONS, 2026-10-26)
TASKS = (
    (
        "curve",
        "Independently check the rank and the torsion subgroup of the elliptic curve "
        "y^2 + y = x^3 - x^2 - 10x - 20 (Cremona's 11a1), and say how certain each part of "
        "the check is.",
    ),
    (
        "squares",
        "Write out an elementary proof that every prime p ≡ 1 (mod 4) is a sum of two squares, "
        "and check the statement for every such prime below 1000.",
    ),
    (
        "class-number",
        "Independently check that the imaginary quadratic fields Q(√-163) and Q(√-47) have "
        "class numbers 1 and 5, with a count of reduced binary quadratic forms for each.",
    ),
)


@dataclass(frozen=True)
class Wait:
    """What Claude did while its background sub-agent ran, from the launch to the
    notification of its end."""

    seconds: float
    gaps: tuple[float, ...]  # the launch, each note, then the notification: the times between
    notes: int
    checks: int  # model calls with tool calls while waiting (parallel calls count once)
    silent: int  # of those, the ones right after another, with no note between

    @property
    def longest(self) -> float:
        return max(self.gaps)

    def text(self) -> str:
        return (
            f"waited {self.seconds:.0f} s, {self.notes} notes, {self.checks} checks "
            f"({self.silent} right after another with no note between); gaps between notes "
            + ", ".join(f"{g:.0f}" for g in self.gaps)
            + " s"
        )


def _ends(entry: Entry, call: str) -> bool:
    """Whether `entry` notifies the end of the task that call `call` started: a notification
    naming that call, or one of them that names none."""
    return entry.notified is not None and (not call or not entry.notified or entry.notified == call)


def waiting(entries: list[Entry]) -> Wait | str:
    """Claude's wait on its first background sub-agent, from its transcript's main thread
    (`transcript`), or why it can't be measured: from the call that started it to the
    notification of its end, taken in, which names that call (a background command's
    notification, from a wait of Claude's own, doesn't end the window)."""
    launched = next((i for i, e in enumerate(entries) if e.background_agent), None)
    if launched is None:
        return "Claude started no background sub-agent"
    call = entries[launched].agent_id
    notified = next((i for i in range(launched + 1, len(entries)) if _ends(entries[i], call)), None)
    if notified is None:
        return "the transcript has no notification of the sub-agent's end"
    start, end = entries[launched].at, entries[notified].at
    marks, notes, checks, silent, calls = [start], 0, 0, 0, 0
    last: str | None = None  # the response of the last call since the last note
    for entry in entries[launched + 1 : notified]:
        if entry.kind != "assistant":
            continue
        if entry.text.strip():  # a record's text comes before its tool call
            notes, calls, last = notes + 1, 0, None
            marks.append(entry.at)
        if entry.tool_use and not (entry.response and entry.response == last):
            checks, calls, last = checks + 1, calls + 1, entry.response
            silent += calls > 1
    marks.append(end)
    gaps = tuple(b - a for a, b in itertools.pairwise(marks))
    return Wait(end - start, gaps, notes, checks, silent)


def edit_times(history: list[dict[str, Any]]) -> list[float]:
    """Seconds from a message's post to each later version, from its edit history (oldest
    first, as `TestUser.history` gives it); a version without a time is left out, and with no
    time for the post there are none."""
    stamps = [v.get("timestamp") for v in history]
    if not stamps or not isinstance(stamps[0], (int, float)):
        return []
    return [float(t) - stamps[0] for t in stamps[1:] if isinstance(t, (int, float))]


def notes_task(marker: str, task: str, done: str) -> str:
    """A Claude prompt: `task` for a background sub-agent, whose findings Claude waits for."""
    command = f"`python3 -c 'import time; time.sleep({SLEEP_S})' {marker}`"
    return (
        f"This is a test of the bridge (`{marker}`). {task} Give this to a sub-agent in the "
        "background: it takes a few minutes. As a stand-in for a long computation, its task "
        f"must include running the shell command {command} with the Bash tool in the "
        "foreground twice: once before its first step and once before it reports. Wait for it "
        "in this turn, and when it has finished, give me its findings, ending your reply with "
        f"exactly `{done}`."
    )


async def one_topic(
    chat: Chat, vm, nonce: str, index: int, label: str, task: str
) -> tuple[Wait | str, list[float] | str]:
    """One topic's turn, waited for, then its wait as the transcript shows it and the times of
    its status message's versions, or why those couldn't be read: they are only shown, so a
    failed read fails nothing (review of #922, N3)."""
    marker, done = f"S17N{index}-{nonce}", f"DONE-{nonce}-{index}"
    topic = topic_name(17, nonce, f"notes-{label}")
    turn = await chat.say("claude", topic, notes_task(marker, task, done))
    over = False
    try:
        await chat.reply(turn, answer(re.escape(done)), within=TURN_S)
        over = True
        measured = waiting(await asyncio.to_thread(claude_entries, vm, marker))
        edits: list[float] | str
        try:
            statuses = await chat.statuses(turn)
            edits = edit_times(await chat.user.history(statuses[0]["id"])) if statuses else []
        except Exception as exc:  # shown, never a failure
            edits = f"unread ({type(exc).__name__}: {exc})"[:160]
        return measured, edits
    finally:
        await clean_up(chat, vm, topic, marker, running=not over)


@pytest.mark.scenario(17)
async def test_claude_notes_while_a_subagent_works(chat: Chat, vm, scenario, nonce):
    """Part 7 (#668): in each of three topics, Claude's notes while its background sub-agent
    works come at most 65 s apart, from the launch to the notification."""
    if not installed.NOTES_WHILE_WAITING.search(installed.instructions(vm)):
        pytest.skip("the installed instructions predate #668's notes while waiting")
    results = await asyncio.gather(
        *(one_topic(chat, vm, nonce, i, label, task) for i, (label, task) in enumerate(TASKS)),
        return_exceptions=True,
    )
    target = os.environ.get(RC_ENV) == "1"
    failed: list[str] = []
    left: list[Leftover] = []
    rigs: list[str] = []
    kept = len(scenario.timings)
    for (label, _), result in zip(TASKS, results, strict=True):
        who = f"claude {label}"
        if isinstance(result, Leftover):
            left.append(result)
            failed += [f"{who}: {result.after}"] if result.after else []
            continue
        if isinstance(result, RigFailure):
            rigs.append(f"{who}: {result}"[:300])
            continue
        if isinstance(result, BaseException):
            failed.append(f"{who}: {type(result).__name__}: {result}"[:300])
            continue
        measured, edits = result
        shown = edits if isinstance(edits, str) else ", ".join(f"+{t:.0f}" for t in edits)
        shown = (
            f"status edits {shown or 'none'}"
            if isinstance(edits, str)
            else (f"status edits at {shown or 'none'} s")
        )
        if isinstance(measured, str):
            failed.append(f"{who}: {measured}")
            scenario.notes.append(f"{who}: {measured}; {shown}")
            continue
        scenario.timing(
            f"{who}: longest gap between notes", measured.longest, high=GAP_S, target=target
        )
        scenario.notes.append(f"{who}: {measured.text()}; {shown}")
        if measured.seconds < WAIT_S:
            failed.append(f"{who}: the sub-agent ran {measured.seconds:.0f} s, too short to tell")
    if left:  # a clean-up failed: a run problem, after the part's own failures
        first = left[0]
        others = [f"also left: {other.what}" for other in left[1:]]  # review of #922, N4
        first.after = "; ".join([*failed, *rigs, *others])
        raise first
    missed = [t for t in scenario.timings[kept:] if not (t.ok or t.target)]
    if rigs and not failed and not missed:  # the part runs again (`conftest`, #877)
        raise next(r for r in results if isinstance(r, RigFailure))
    # a failure seen stays one: a rig failure beside it never turns it into a rerun (B2)
    if failed or rigs:
        pytest.fail("; ".join(failed + rigs), pytrace=False)
