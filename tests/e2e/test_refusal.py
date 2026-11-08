"""A Codex request OpenAI refused with a 429, offline (#842): named, run again once, and a
second refusal fails the part."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from e2e.harness import refusal, report
from e2e.harness.recorder import Recorder
from e2e.harness.refusal import Refused
from e2e.harness.zulip_user import topic_narrow
from e2e.test_harness_zulip import BOT, TESTER, Zulip, user
from e2e.test_recorder import inner_pytest

LIMIT = "exceeded retry limit, last status: 429 Too Many Requests"
#: The bridge's two forms: a turn's answer (`publish_completion`) and a side question's
TURN = f"(no final message)\n\n⚠️ **Bridge** (not Codex): network: {LIMIT}"
SIDE = f"⚠️ **Bridge** (not Codex): Codex couldn't answer (network): {LIMIT}"


def test_the_bridges_refusal_lines_and_nothing_else():
    assert refusal.refusal(TURN) == f"⚠️ **Bridge** (not Codex): network: {LIMIT}"
    assert refusal.refusal(SIDE) == SIDE
    with_id = f"⚠️ **Bridge** (not Codex): network: {LIMIT}, request id: x"
    assert refusal.refusal(with_id) == with_id
    assert refusal.refusal(f"⚠️ **Bridge** (not Claude): {LIMIT}") is None  # Codex's only
    assert refusal.refusal(f"The log said `{LIMIT}`.") is None  # an agent's words
    assert refusal.refusal("⚠️ **Bridge** (not Codex): network: stream disconnected") is None
    # the note in mid-line, as an agent's sentence quoting it has it: no refusal (review N3)
    assert refusal.refusal(f"The bridge said ⚠️ **Bridge** (not Codex): network: {LIMIT}") is None


async def test_a_wait_raises_the_refusal_at_once_and_only_its_bots():
    fake = Zulip()
    tester = user(fake)
    mid = await tester.send(5, "e2e-1", "@codex hi")
    fake.add(TESTER, SIDE, stream=5, topic="e2e-1")  # not from the bot waited for
    narrow = topic_narrow(5, "e2e-1")
    with pytest.raises(TimeoutError):
        await tester.wait_reply(narrow, after_id=mid, senders=[BOT], pattern="PONG", within=3)
    fake.add(BOT, "❌ **Failed** · 0m 01s · 0 actions · model default", stream=5, topic="e2e-1")
    fake.add(BOT, TURN, stream=5, topic="e2e-1")
    fake.slept = 0
    with pytest.raises(Refused) as caught:  # whatever the pattern: `|^⚠️` takes it too
        await tester.wait_reply(narrow, after_id=mid, senders=[BOT], pattern="^⚠️", within=900)
    assert caught.value.line.endswith(LIMIT) and str(caught.value).startswith(refusal.WHY)
    assert fake.slept == 0  # raised at its first look, not at the deadline
    await tester.close()


def row_and_runs(
    outcomes: list[Exception | None],
) -> tuple[report.ScenarioResult, list[str], Callable[[], None]]:
    """Scenario 1's row, the calls made, and a part that records a timing and then fails with
    the next of `outcomes` (None: passes)."""
    row = Recorder().row(1)
    calls: list[str] = []

    def run() -> None:
        calls.append("run")
        row.timing("codex: the answer", 6.2)
        if (failure := outcomes.pop(0)) is not None:
            raise failure

    return row, calls, run


def test_refused_once_then_passed():
    row, calls, run = row_and_runs([None])
    row.timing("claude: an earlier part's", 3.1)
    row.notes.append("an earlier part's note")
    row.timing("codex: the refused run's", 9.3)
    row.notes.append("codex btw (command): answered without a tool, 6 s")
    slept: list[float] = []
    budget = refusal.Budget(2)
    refusal.retry("test_typed_mention", Refused(SIDE), row, kept=1, noted=1, budget=budget,
                  run=run, renew=lambda: calls.append("renew"), sleep=slept.append)  # fmt: skip
    assert slept == [refusal.WAIT_S] and calls == ["renew", "run"] and budget.left == 1
    # the refused run's timing went; the second run's, and an earlier part's, stay
    assert [t.name for t in row.timings] == ["claude: an earlier part's", "codex: the answer"]
    # the refused run's note stays, marked as the first run's (review N4)
    first = "first run: codex btw (command): answered without a tool, 6 s"
    assert row.notes == ["an earlier part's note", first]
    assert row.external == [f"test_typed_mention: {SIDE}; the part ran again after 120 s"]
    assert f"external cause: test_typed_mention: {SIDE}" in row.explanation()


def test_refused_twice_fails():
    row, calls, run = row_and_runs([Refused(SIDE)])
    with pytest.raises(Refused, match="in the part's second run too") as caught:
        refusal.retry("test_btw_codex_limits", Refused(SIDE), row, kept=0, noted=0,
                      budget=refusal.Budget(), run=run, sleep=lambda s: None)  # fmt: skip
    assert caught.value.twice and calls == ["run"]
    assert row.external[1] == f"test_btw_codex_limits: refused again in its second run: {SIDE}"
    row_, _, run_ = row_and_runs([AssertionError("no side answer")])
    with pytest.raises(AssertionError):  # another failure of the second run is its own
        refusal.retry("p", Refused(SIDE), row_, kept=0, noted=0, budget=refusal.Budget(),
                      run=run_, sleep=lambda s: None)  # fmt: skip


def test_a_refusal_after_the_runs_retries_are_spent_fails_at_once():
    """Review N2: a broad throttle costs a run at most `RETRIES` waits."""
    row, calls, run = row_and_runs([None] * refusal.RETRIES)
    budget, slept = refusal.Budget(), []
    for _ in range(refusal.RETRIES):
        refusal.retry("p", Refused(SIDE), row, kept=0, noted=0, budget=budget, run=run,
                      sleep=slept.append)  # fmt: skip
    assert budget.left == 0 and len(slept) == len(calls) == refusal.RETRIES
    with pytest.raises(Refused, match="retries are spent") as caught:
        refusal.retry("q", Refused(SIDE), row, kept=0, noted=0, budget=budget, run=run,
                      sleep=slept.append)  # fmt: skip
    assert not caught.value.twice
    assert len(slept) == len(calls) == refusal.RETRIES  # no wait, no run
    assert row.external[-1] == f"q: {SIDE}; not run again: the run's 3 retries are spent"


def test_an_incomplete_rows_refusal_is_named_too():
    """Review B1: a row with a part not run (scenario 8's cold import, in a fast run) is listed
    under "Not completely run", not in Details: the cause is named there."""
    proof = "test_lean_proof[codex]"
    row = report.ScenarioResult(
        8, "Lean (#205)", parts=[proof, "test_lean_cold_import"], passed_parts=[proof]
    )
    row.external.append(f"test_lean_proof[codex]: {SIDE}; the part ran again after 120 s")
    summary = report.Report(results=[row]).markdown()
    assert ("- **8. Lean (#205)** (not run: test_lean_cold_import): no reason given; external "
            f"cause: test_lean_proof[codex]: {SIDE}") in summary  # fmt: skip


INNER = f"""
import pytest
from e2e.harness import refusal

pytestmark = pytest.mark.e2e
refusal.WAIT_S = 0.0
LINE = {SIDE!r}


@pytest.mark.scenario(1)
def test_typed_mention(scenario, nonce):
    with open("nonces-1", "a") as f:  # in the run's directory
        f.write(nonce + " ")
    scenario.timing("codex: a typed @codex, its answer", 6.2)
    if len(open("nonces-1").read().split()) == 1:
        raise refusal.Refused(LINE)


@pytest.mark.scenario(12)
def test_btw_codex_limits(scenario):
    scenario.notes.append("codex btw (command): answered without a tool, 6 s")
    raise refusal.Refused(LINE)
"""


def test_the_suite_runs_a_refused_part_again_and_reports_it(tmp_path):
    """Through the e2e conftest's hook, in a pytest of its own: 1's part passes on its second
    run, with another nonce; 12's is refused twice and fails. Both rows name the cause."""
    done = inner_pytest(tmp_path, INNER)
    assert done.returncode == 1, done.stdout[-2000:] + done.stderr[-2000:]
    nonces = (tmp_path / "nonces-1").read_text().split()
    assert len(nonces) == 2 and nonces[0] != nonces[1]
    ran = report.Report.read(tmp_path / "r.json")
    rows = {r.number: r for r in ran.results}
    assert rows[1].effective == "passed" and len(rows[1].timings) == 1
    assert rows[1].external == [f"test_typed_mention: {SIDE}; the part ran again after 0 s"]
    assert rows[12].effective == "failed" and len(rows[12].external) == 2
    assert "in the part's second run too" in rows[12].reason
    note = "codex btw (command): answered without a tool, 6 s"
    assert rows[12].notes == [f"first run: {note}", note]  # each run's notes, told apart
    summary = ran.markdown()
    assert "| 1 | Basics, both agents | ✅ pass · ⚠️ external |" in summary
    assert f"**1. Basics, both agents** (passed): external cause: test_typed_mention: {SIDE}" in (
        summary
    )
