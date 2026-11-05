"""Scenario 8's cold import and #461's warm-up (offline): the unit's phases, and what the part
records when the warm-up is still running, done, failed, absent or disabled."""

from __future__ import annotations

import pytest

from e2e import test_scenarios_features as features
from e2e.harness import report
from e2e.harness.vm import Result

LOADED = {"LoadState": "loaded", "UnitFileState": "enabled"}
NOW = {"ExecMainStartTimestampMonotonic": "31000000"}
RUNNING = {**LOADED, **NOW, "ActiveState": "activating", "Result": "success"}
DONE = {
    **LOADED,
    **NOW,
    "ActiveState": "inactive",
    "Result": "success",
    "ExecMainExitTimestampMonotonic": "131500000",
}
LINE = "zab-warm-cache: read 7.94 GB in 96189 files in 99.0 s (80 MB/s): Mathlib v4.34.1 …"
#: `journalctl -u` of a boot: systemd's own lines about the unit come around the script's
JOURNAL = [
    "Starting zab-warm-cache.service - Read the shared Mathlib into the page cache…",
    LINE,
    "zab-warm-cache.service: Deactivated successfully.",
    "Finished zab-warm-cache.service - Read the shared Mathlib into the page cache….",
    "zab-warm-cache.service: Consumed 4.212s CPU time.",
]


def show(state: dict[str, str]) -> Result:
    return Result(0, "".join(f"{k}={v}\n" for k, v in state.items()), "")


class Vm:
    """Answers `systemctl show` with each state in turn, and `journalctl` with `journal`."""

    def __init__(self, *states: dict[str, str], journal: list[str] = JOURNAL) -> None:
        self.states, self.calls, self.journal = list(states), [], journal

    def run(self, argv, check=True, timeout=0):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "show"]:
            return show(self.states.pop(0))
        if "journalctl" in argv:
            return Result(0, "".join(f"{line}\n" for line in self.journal), "")
        raise AssertionError(argv)


@pytest.mark.parametrize(
    ("state", "phase"),
    [
        ({"LoadState": "not-found", "ActiveState": "inactive"}, "absent"),
        ({}, "absent"),
        (RUNNING, "running"),
        (DONE, "done"),
        ({**DONE, "Result": "exit-code"}, "failed"),
        ({**LOADED, "ActiveState": "failed", "Result": "timeout"}, "failed"),
        ({**LOADED, "ActiveState": "inactive", "ExecMainExitTimestampMonotonic": "0"}, "pending"),
        ({**LOADED, "UnitFileState": "disabled", "ActiveState": "inactive"}, "disabled"),
    ],
)
def test_warm_phase(state, phase):
    assert features.warm_phase(state) == phase


def test_warm_state_asks_systemd_for_the_unit():
    vm = Vm(DONE)
    assert features.warm_state(vm) == DONE
    assert vm.calls[0][:3] == ["systemctl", "show", "zab-warm-cache.service"]
    assert "--property=ExecMainExitTimestampMonotonic" in vm.calls[0]


async def test_a_running_warm_up_is_waited_for_then_recorded(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(features.asyncio, "sleep", instant)
    scenario = report.ScenarioResult(8, "Lean (#205)")
    vm = Vm(RUNNING, DONE)
    await features.record_warm_up(vm, scenario, RUNNING, 95.4)
    assert scenario.notes == [
        "warm-up (#461): running when the import ended, 95 s after boot",
        LINE,
    ]
    assert [(t.name, t.seconds) for t in scenario.timings] == [("warm-up done, after boot", 131.5)]


async def test_warm_finished_polls_until_the_unit_ends_or_the_wait_does(monkeypatch):
    """`ZAB_E2E_WARM=wait` starts the import after it (review round 1 on #464)."""

    async def instant(_):
        return None

    monkeypatch.setattr(features.asyncio, "sleep", instant)
    pending = {**LOADED, "ActiveState": "inactive"}
    vm = Vm(RUNNING, DONE)
    assert await features.warm_finished(vm, pending) == (DONE, "done")
    assert len(vm.calls) == 2
    monkeypatch.setattr(features, "WARM_WAIT_S", 0)
    assert await features.warm_finished(Vm(), RUNNING) == (RUNNING, "running")


async def test_a_warm_up_done_before_the_import_ended_is_recorded_at_once():
    scenario = report.ScenarioResult(8, "Lean (#205)")
    await features.record_warm_up(Vm(), scenario, DONE, 140.0)
    assert scenario.notes[0] == "warm-up (#461): done when the import ended, 140 s after boot"
    assert scenario.timings[0].seconds == 131.5


DISABLED = {**LOADED, "UnitFileState": "disabled", "ActiveState": "inactive"}


@pytest.mark.parametrize(
    ("state", "off", "why"),
    [
        ({"LoadState": "not-found"}, False, "no unit on the VM"),
        ({"LoadState": "not-found"}, True, "no unit on the VM"),
        (DISABLED, True, "disabled (ZAB_E2E_WARM=off)"),
    ],
)
async def test_without_the_warm_up_the_time_is_marked_as_such(state, off, why):
    scenario = report.ScenarioResult(8, "Lean (#205)")
    vm = Vm()
    await features.record_warm_up(vm, scenario, state, 120.0, off=off)
    assert scenario.notes == [f"warm-up (#461): {why}; the time above is without it"]
    assert not scenario.timings and not vm.calls


async def test_a_unit_disabled_but_not_by_the_switch_fails_the_part():
    scenario = report.ScenarioResult(8, "Lean (#205)")
    with pytest.raises(AssertionError, match="installed but not enabled"):
        await features.record_warm_up(Vm(), scenario, DISABLED, 120.0)


async def test_a_failed_warm_up_fails_the_part_with_its_journal_line():
    scenario = report.ScenarioResult(8, "Lean (#205)")
    with pytest.raises(AssertionError, match="the warm-up is failed: zab-warm"):
        await features.record_warm_up(Vm(), scenario, {**DONE, "Result": "exit-code"}, 120.0)
    # one that died before its summary shows the journal's last lines instead
    crashed = [JOURNAL[0], "Traceback (most recent call last):", "OSError: boom"]
    with pytest.raises(AssertionError, match=r"is failed: Starting .* / Traceback .* / OSError"):
        await features.record_warm_up(
            Vm(journal=crashed), scenario, {**DONE, "Result": "exit-code"}, 120.0
        )


async def test_the_summary_is_the_scripts_line_not_systemds_last():
    """Codex round 1: `journalctl -u` ends with systemd's lines about the unit."""
    scenario = report.ScenarioResult(8, "Lean (#205)")
    await features.record_warm_up(Vm(), scenario, DONE, 140.0)
    assert scenario.notes[-1] == LINE
    # a run that ended well without a summary fails the part: nothing to evaluate it by
    with pytest.raises(AssertionError, match="finished without its summary in the journal"):
        await features.record_warm_up(Vm(journal=[JOURNAL[0], *JOURNAL[2:]]), scenario, DONE, 1.0)
