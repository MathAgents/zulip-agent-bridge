"""A scenario's own clean-up, offline (#517): retried, then a Leftover naming what was left."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from e2e.harness import cleanup
from e2e.harness.cleanup import IDLE_STOP_LEFT, PAUSE_S, Leftover, undo, vm_up
from e2e.harness.startup import PROBE_PAUSE_S, SSH_WAIT_S, STEADY_GAP_S, Timeline
from e2e.harness.vm import Result


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def instant(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(cleanup.asyncio, "sleep", instant)
    return slept


class Flaky(list):
    """A step that raises (an SSH connection refused) on its first `fails` calls."""

    def __init__(self, fails: int) -> None:
        super().__init__()
        self.fails = fails

    def step(self) -> None:
        self.append(1)
        if len(self) <= self.fails:
            raise RuntimeError(f"ssh: connection refused ({len(self)})")


async def test_a_step_is_tried_three_times_then_is_a_leftover_that_names_it(pauses):
    calls = Flaky(2)
    await undo(IDLE_STOP_LEFT, calls.step)
    assert len(calls) == 3 and pauses == [PAUSE_S] * 2
    calls, pauses[:] = Flaky(99), []
    with pytest.raises(Leftover) as caught:
        await undo(IDLE_STOP_LEFT, calls.step)
    assert len(calls) == 3 and len(pauses) == 2  # no pause after the last try
    assert caught.value.what == IDLE_STOP_LEFT and caught.value.stops
    assert str(caught.value) == f"{IDLE_STOP_LEFT} (RuntimeError: ssh: connection refused (3))"
    assert caught.value.after == ""  # nothing had failed before the clean-up
    with pytest.raises(Leftover) as caught:
        await undo(cleanup.WARM_DISABLED, Flaky(1).step, stops=False, attempts=1)
    assert not caught.value.stops and len(pauses) == 2


async def test_a_leftover_keeps_the_failure_its_clean_up_ran_after(pauses):
    """Reviewer on #518 (N1): raised in a `finally`, the Leftover replaces the scenario's own
    exception; it keeps that one's first line for the report."""
    with pytest.raises(Leftover) as caught:
        try:
            await asyncio.sleep(0)
            raise AssertionError("the DM wake was not answered\nassert False")
        finally:
            await undo(IDLE_STOP_LEFT, Flaky(9).step, attempts=2)
    assert caught.value.after == "AssertionError: the DM wake was not answered"
    assert isinstance(caught.value.__cause__.__context__, AssertionError)


async def test_a_cancelled_body_is_named_without_a_trailing_colon(pauses):
    """Reviewer on #518: `after` was `CancelledError:`; a cause without a message too (#519)."""
    with pytest.raises(Leftover) as caught:
        try:
            raise asyncio.CancelledError
        finally:
            await undo(IDLE_STOP_LEFT, Flaky(9).step, attempts=1)
    assert caught.value.after == "CancelledError"
    assert str(Leftover(IDLE_STOP_LEFT, TimeoutError())) == f"{IDLE_STOP_LEFT} (TimeoutError)"
    assert cleanup.failed_with(None) == ""


async def test_an_async_step_is_awaited(pauses):
    calls = []

    async def step() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("still stopping")

    await undo(cleanup.VM_STOPPED, step)
    assert calls == [1, 1] and pauses == [PAUSE_S]


def ec2(state: str, after: str = "stopped") -> SimpleNamespace:
    """A VM in `state`; one `stopping` reads `after` next."""
    states, calls = [state], []

    def wait_state(iid, target, timeout):
        calls.append(target)
        states[0] = target

    def wait_while(iid, state, timeout):
        calls.append(f"not {state}")
        states[0] = after
        return after

    def start(iid):
        calls.append("start")
        states[0] = "pending"

    return SimpleNamespace(state=lambda iid: states[0], wait_state=wait_state, start=start,
                           wait_while=wait_while, calls=calls)  # fmt: skip


def vm(answers_after: int, clock: list[float] | None = None) -> SimpleNamespace:
    """SSH that answers from probe `answers_after + 1` on, each probe taking 1 s on `clock`;
    cloud-init done."""
    tries: list[int] = []
    clock = [0.0] if clock is None else clock

    def probe():
        tries.append(1)
        clock[0] += 1.0
        return Result(0, "", "") if len(tries) > answers_after else Result(255, "", REFUSED)

    def run(argv, timeout=None, check=True):
        assert argv == ["cloud-init", "status", "--wait"] and not check
        return Result(0, "status: done\n", "")

    return SimpleNamespace(host="198.51.100.4", tries=tries, probe=probe, run=run, clock=clock)


REFUSED = "ssh: connect to host 198.51.100.4 port 22: Connection refused\n"


def quiet(ssh) -> dict:
    """vm_up's timeline and pauses on the fake VM's clock, printing nothing."""
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        ssh.clock[0] += seconds

    timeline = Timeline(lambda: ssh.clock[0], lambda: 0.0, lambda line: None)
    return {"timeline": timeline, "sleep": sleep, "slept": slept}


async def test_vm_up_waits_for_steady_ssh_also_on_a_running_vm():
    """SSH is checked on a running VM too, a precaution: not #517's cause, as there SSH had
    answered push_config's owner check just before. #804: three answers in a row, as the
    runner's start (`startup.steady_ssh`). A stopping VM is started again."""
    aws, ssh = ec2("running"), vm(answers_after=2)
    up = quiet(ssh)
    await vm_up(aws, "i-test", ssh, timeline=up["timeline"], sleep=up["sleep"])
    assert aws.calls == [] and len(ssh.tries) == 5
    assert up["slept"] == [PROBE_PAUSE_S] * 2 + [STEADY_GAP_S] * 2
    assert [line.split(": ", 1)[1] for line in up["timeline"].lines][2:4] == [
        "SSH answered in 1.0 s (1 of 3)",
        "cloud-init status: done (exit 0), after 0.0 s",
    ]
    aws, ssh = ec2("stopping"), vm(answers_after=10**6)
    up = quiet(ssh)
    with pytest.raises(TimeoutError, match=r"198\.51\.100\.4: SSH not steady 600 s after"):
        await vm_up(aws, "i-test", ssh, timeline=up["timeline"], sleep=up["sleep"])
    assert aws.calls == ["not stopping", "start", "running"]
    assert ssh.clock[0] <= SSH_WAIT_S + 1  # one probe past


async def test_vm_up_takes_a_vm_the_waker_started_again_as_it_is():
    """#305: a VM stopping may read `pending` next, its `stopped` missed by the polls, when the
    test waker starts it again. It is waited for, never started a second time."""
    aws, ssh = ec2("stopping", after="pending"), vm(answers_after=0)
    up = quiet(ssh)
    await vm_up(aws, "i-test", ssh, timeline=up["timeline"], sleep=up["sleep"])
    assert aws.calls == ["not stopping", "running"] and len(ssh.tries) == 3
    aws, ssh = ec2("stopping", after="running"), vm(answers_after=0)
    up = quiet(ssh)
    await vm_up(aws, "i-test", ssh, timeline=up["timeline"], sleep=up["sleep"])
    assert aws.calls == ["not stopping"]
