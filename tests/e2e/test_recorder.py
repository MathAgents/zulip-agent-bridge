"""The e2e suite's scenario selection and recorder, offline (issue #263)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e import conftest
from e2e.harness import report
from e2e.harness.recorder import Recorder


def test_recorder_folds_parts_into_one_row():
    rec = Recorder()
    rec.row(12).notes.append("history: read 3 messages")
    rec.finish(12, "test_history", "passed", 30.0, "")
    rec.finish(12, "test_fork", "skipped", 0.5, "!fork is not on the VM (#202)")
    rec.finish(1, "test_basics[claude]", "passed", 40, "")
    rec.finish(1, "test_basics[codex]", "failed", 50, "no reply within 120 s")
    rows = {r.number: r for r in rec.report().results}
    assert rows[12].outcome == "passed" and rows[12].seconds == 30.5
    assert rows[1].outcome == "failed"
    assert rows[1].reason == "test_basics[codex]: no reply within 120 s"
    rec.finish(2, "test_catchup", "skipped", 0, "bridge unit missing")
    assert rec.rows[2].outcome == "skipped"
    with pytest.raises(ValueError):
        rec.row(17)


def test_scenarios_are_selected_by_number_or_group(monkeypatch):
    def item(number, slow=False):
        marks = {"scenario": SimpleNamespace(args=(number,)) if number else None, "slow": slow}
        name = f"test_{number}{'_slow' if slow else ''}"
        return SimpleNamespace(
            n=(number, slow), name=name, get_closest_marker=lambda name: marks.get(name)
        )

    def select(spec):
        every = [item(1), item(3), item(5), item(8), item(8, slow=True), item(14), item(None)]
        dropped = []
        hook = SimpleNamespace(pytest_deselected=lambda items: dropped.extend(items))
        monkeypatch.setenv("ZAB_E2E_SCENARIOS", spec)
        conftest.pytest_collection_modifyitems(SimpleNamespace(hook=hook), every)
        return [i.n for i in every], [i.n for i in dropped]

    # None: a helper's own test, always kept; (8, True): scenario 8's part that stops the VM,
    # which runs with the slow group only (codex on #286)
    assert select("slow,5") == ([(3, False), (5, False), (8, True), (None, False)],
                                [(1, False), (8, False), (14, False)])  # fmt: skip
    kept, _ = select("fast")
    assert (8, False) in kept and (8, True) not in kept
    # scenario 14 stops only the bridge, for seconds: in every fast run (#305)
    assert (14, False) in kept and (14, False) in select("slow")[1]
    assert (8, True) not in select("8")[0] and (8, True) in select("all")[0]
    monkeypatch.delenv("ZAB_E2E_SCENARIOS")
    items = [item(1)]
    conftest.pytest_collection_modifyitems(None, items)  # unset: everything stays
    assert len(items) == 1


def phase(when: str, outcome: str, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        when=when, outcome=outcome, passed=outcome == "passed", failed=outcome == "failed",
        skipped=outcome == "skipped", duration=1.0, longrepr=None, longreprtext=text,
    )  # fmt: skip


def test_a_failed_teardown_fails_the_scenario():
    """Codex on #282: setup passed, call passed, teardown raised: the row failed, with why."""
    rec = Recorder()
    for rep in (
        phase("setup", "passed"),
        phase("call", "passed"),
        phase("teardown", "failed", "Traceback …\nRuntimeError: bridge left stopped"),
    ):
        conftest.record(rec, 2, "test_catch_up", rep)
    row = rec.rows[2]
    assert row.outcome == "failed" and row.effective == "failed"
    assert row.reason == "test_catch_up: teardown: RuntimeError: bridge left stopped"
    assert not rec.report().passed
    clean = Recorder()
    for rep in (phase("setup", "passed"), phase("call", "passed"), phase("teardown", "passed")):
        conftest.record(clean, 1, "test_basics", rep)
    assert clean.rows[1].outcome == "passed" and clean.rows[1].seconds == 1.0


def test_a_part_left_out_of_the_selection_keeps_a_required_scenario_incomplete(monkeypatch):
    """Codex on #286: `--scenarios 1-13 --require rc` left scenario 8's slow cold import out
    and still said PASS. Every collected part is expected; a required scenario passes only
    when all of them did, here or in a merged report of the same build."""
    from e2e.harness import report

    def item(name, slow=False):
        marks = {"scenario": SimpleNamespace(args=(8,)), "slow": slow}
        return SimpleNamespace(name=name, get_closest_marker=lambda m: marks.get(m))

    fast_rec, slow_rec = Recorder(), Recorder()
    hook = SimpleNamespace(pytest_deselected=lambda items: None)
    for rec, spec in ((fast_rec, "1-13"), (slow_rec, "slow")):
        monkeypatch.setattr(conftest, "_RECORDER", rec)
        monkeypatch.setenv("ZAB_E2E_SCENARIOS", spec)
        items = [item("test_lean_proof[claude]"), item("test_lean_cold_import", slow=True)]
        conftest.pytest_collection_modifyitems(SimpleNamespace(hook=hook), items)
        for kept in items:
            rec.finish(8, kept.name, "passed", 10, "")
    fast = report.Report(commit="c", wheel="w", results=fast_rec.report().results, required=[8])
    assert fast.results[0].unrun() == ["test_lean_cold_import"]
    assert fast.verdict == "FAIL" and fast.missed() == [8] and "not run:" in fast.markdown()
    fast.required = []
    assert fast.verdict == "PASS"  # a slow part left out of a fast run alone is no skip
    fast.required = [8]
    fast.merge(report.Report(commit="c", wheel="w", results=slow_rec.report().results))
    assert fast.results[0].complete and fast.verdict == "PASS"


IDLE = "idle stop is still installed on the VM"
STOPPED = f"the run stopped after scenario 11: {IDLE}"


def test_a_failed_part_is_reported_with_its_message_not_only_its_place():
    """#517: the row said `tests/e2e/harness/admin.py:58: RuntimeError`; the message was lost."""
    rec = Recorder()
    rep = phase(
        "call", "failed", "…\nE   RuntimeError: exit 1: ssh: no\n\nadmin.py:58: RuntimeError"
    )
    crash = SimpleNamespace(message="RuntimeError: exit 1: ssh: no\nsecond line")
    rep.longrepr = SimpleNamespace(reprcrash=crash)
    conftest.record(rec, 11, "test_idle_stop", rep)
    assert rec.rows[11].reason == "test_idle_stop: RuntimeError: exit 1: ssh: no"


INNER = """
import asyncio

import pytest
from e2e.harness.cleanup import Leftover, undo

pytestmark = pytest.mark.e2e


@pytest.mark.scenario(8)
def test_cold_import(scenario):
    raise Leftover("the warm-up unit is still disabled", OSError("x"), stops=False)


def refused():
    raise RuntimeError("exit 1: ssh: no")


@pytest.mark.scenario(11)
def test_idle_stop(scenario):
    async def scenario_11():
        try:
            assert False, "the wake failed"
        finally:
            await undo("idle stop is still installed on the VM", refused, attempts=1)

    asyncio.run(scenario_11())


@pytest.mark.scenario(4)
def test_zulip_outage(scenario):
    open("ran-4", "w").close()
"""


def inner_pytest(tmp_path: Path, source: str, **env: str) -> subprocess.CompletedProcess:
    """`source` as a test module, in a pytest of its own with the e2e conftest as its plugin;
    its report goes to `r.json`."""
    (tmp_path / "test_inner.py").write_text(source)
    markers = ("e2e", "scenario(n)", "slow", "admin_e2e", "admin_scenario(label)")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n" + "".join(f"  {m}\n" for m in markers)
    )
    tests = str(Path(conftest.__file__).parents[1])
    env = {**os.environ, "PYTHONPATH": tests, "ZAB_E2E_REPORT_JSON": str(tmp_path / "r.json"),
           **env}  # fmt: skip
    env.pop("ZAB_E2E_SCENARIOS", None)
    argv = [sys.executable, "-m", "pytest", "-p", "e2e.conftest", "-p", "no:cacheprovider"]
    return subprocess.run(  # noqa: S603 - an argument list
        [*argv, "-p", "no:xdist", "-q", str(tmp_path)], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=100, check=False,
    )  # fmt: skip


def test_pytest_stops_after_the_scenario_and_the_report_says_why(tmp_path):
    """#517, in a pytest of its own with the e2e conftest as its plugin: each failed clean-up
    is a run problem; after 11's, which leaves the VM unfit for the later scenarios, the run
    stops (pytest's `shouldstop`, exit 2): 4 never runs, and its row says why."""
    done = inner_pytest(tmp_path, INNER)
    assert done.returncode == 2, done.stdout[-2000:] + done.stderr[-2000:]
    assert f"Interrupted: scenario 11's clean-up failed: {IDLE}" in done.stdout
    assert not (tmp_path / "ran-4").exists()
    ran = report.Report.read(tmp_path / "r.json")
    assert ran.problems == [
        "scenario 8's clean-up failed: the warm-up unit is still disabled (OSError: x)",
        f"{STOPPED} (RuntimeError: exit 1: ssh: no)",
    ]
    rows = {r.number: r for r in ran.results}
    assert rows[11].outcome == "failed" and f"Leftover: {IDLE}" in rows[11].reason
    # the scenario's own failure stays in its row (reviewer on #518, N1)
    assert rows[11].reason.endswith("; after the scenario failed: AssertionError: the wake failed")
    assert rows[4].outcome == "skipped" and rows[4].unrun() == ["test_zulip_outage"]
    assert rows[4].reason == STOPPED


DECOY = "DECOY-SECRET-519"  # a decoy, never a real key
MALFORMED = """
from pathlib import Path

import pytest
from e2e.harness.zulip_user import TestUser

pytestmark = pytest.mark.e2e


@pytest.fixture
def tester():
    return TestUser.from_zuliprc(Path.home() / ".config" / "zab" / "tester.zuliprc")


@pytest.mark.scenario(1)
def test_basics(scenario, tester):
    pass
"""


def test_a_malformed_zuliprc_is_named_in_the_row_never_quoted(tmp_path):
    """#519: configparser quotes the offending line; the row names the file and the type, and
    pytest's whole output (`pytest.log`) shows neither the message nor the file's text."""
    rc = tmp_path / ".config" / "zab" / "tester.zuliprc"
    rc.parent.mkdir(parents=True)
    rc.write_text(f"key={DECOY}\n")  # no [api] header: a decoy, in the test's HOME
    done = inner_pytest(tmp_path, MALFORMED, HOME=str(tmp_path))
    assert done.returncode == 1, done.stdout[-2000:] + done.stderr[-2000:]
    assert "SecretFileError: tester.zuliprc: MissingSectionHeaderError" in done.stdout
    assert DECOY not in done.stdout + done.stderr
    ran = report.Report.read(tmp_path / "r.json")
    (row,) = ran.results
    assert row.outcome == "failed"
    assert row.reason.endswith("SecretFileError: tester.zuliprc: MissingSectionHeaderError")
    assert DECOY not in (tmp_path / "r.json").read_text() and DECOY not in ran.markdown()
