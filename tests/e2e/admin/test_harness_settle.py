"""Reading AWS until a bot's change shows (`harness/settle.py`), offline (issue #356): EC2 as
the first live run's A4 met it, answering from before the change for a few reads."""

from __future__ import annotations

import pytest

from e2e.admin.fakes import FakeEc2, FakeError, FakeIam
from e2e.admin.harness import rig as rigs
from e2e.admin.harness import settle
from e2e.admin.harness.aws_rig import AwsRig
from e2e.admin.harness.gate import Aws, ReadOnly
from e2e.admin.harness.settle import EVERY_S, PROTECTION_S, WITHIN_S, attribute, until
from e2e.admin.test_scenarios_2_bot import vm_state, wait_state

A, B = "zab-test-a", "zab-test-b"
SCOPE = {"workshop": rigs.E2E.name, "zab-role": "test"}


class FakeTime:
    """`settle`'s clock and sleep: time moves on only by sleeping; a wait without a bound
    fails, never hangs."""

    def __init__(self) -> None:
        self.now, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.now > 3600:
            raise RuntimeError("a wait without a bound")


@pytest.fixture
def fake_time(monkeypatch) -> FakeTime:
    clock = FakeTime()
    monkeypatch.setattr(settle, "clock", clock)
    monkeypatch.setattr(settle, "sleep", clock.sleep)
    return clock


class Lagging(FakeEc2):
    """After a change: the first `stale` reads of termination protection give the value from
    before it; the first reads of the workshop's instances show `views`, one each, as states by
    participant (a participant left out: not shown yet)."""

    def __init__(self, stale: int = 2, views: list[dict[str, str]] | None = None) -> None:
        super().__init__()
        self.stale, self.views = stale, list(views or [])
        self.reads = {"attribute": 0, "instances": 0}

    def describe_instance_attribute(self, **p):
        self.reads["attribute"] += 1
        found = super().describe_instance_attribute(**p)
        if self.reads["attribute"] <= self.stale:
            found["DisableApiTermination"]["Value"] = not found["DisableApiTermination"]["Value"]
        return found

    def describe_instances(self, **p):
        found = super().describe_instances(**p)
        if {"Name": "tag:workshop", "Values": [rigs.E2E.name]} not in p.get("Filters", []):
            return found
        self.reads["instances"] += 1
        if not self.views:
            return found
        view, shown = self.views.pop(0), []
        for inst in found["Reservations"][0]["Instances"]:
            slug = {t["Key"]: t["Value"] for t in inst["Tags"]}.get("participant")
            if slug in view:
                shown.append({**inst, "State": {"Name": view[slug]}})
        return {"Reservations": [{"Instances": shown}]}


def scenarios_view(ec2: FakeEc2, tmp_path) -> AwsRig:
    """The scenarios' `aws` (conftest): the rig's AWS view through `ReadOnly`."""
    return AwsRig(Aws(ReadOnly(), ec2, FakeIam()), rigs.load({"HOME": str(tmp_path)}))


def protection(aws: AwsRig, iid: str):
    return lambda: attribute(aws, iid, "disableApiTermination")


async def test_a_change_shown_late_is_read_until_it_shows(tmp_path, fake_time):
    ec2 = Lagging(stale=2)
    ec2.add_instance("i-a", {**SCOPE, "participant": A})
    ec2.protected = {"i-a"}  # `!protect on zab-test-a`: done, but read False twice
    aws = scenarios_view(ec2, tmp_path)
    found = await until(protection(aws, "i-a"), lambda value: value is True, what="A's")
    assert found is True and ec2.reads["attribute"] == 3
    assert fake_time.slept == [EVERY_S, EVERY_S]


async def test_a_real_mismatch_still_fails_after_the_bound(tmp_path, fake_time):
    ec2 = Lagging(stale=0)
    ec2.add_instance("i-a", {**SCOPE, "participant": A})  # never protected
    aws = scenarios_view(ec2, tmp_path)
    bound = f"A's protection, True: still False after {WITHIN_S:.0f} s"
    with pytest.raises(AssertionError, match=bound):
        await until(protection(aws, "i-a"), lambda value: value is True,
                    what="A's protection, True")  # fmt: skip
    assert fake_time.now == WITHIN_S and ec2.reads["attribute"] == WITHIN_S / EVERY_S + 1


async def test_a_read_that_fails_is_not_retried(fake_time):
    reads = []

    def refused():
        reads.append(1)
        raise FakeError("UnauthorizedOperation")

    with pytest.raises(FakeError, match="UnauthorizedOperation"):
        await until(refused, lambda value: True, what="a refused read")
    assert reads == [1] and fake_time.slept == []


async def test_a_stop_is_waited_for_though_ec2_shows_it_late(tmp_path, fake_time):
    """`wait_state`, A7's and A8's: a read that doesn't list the instance yet, then its state
    from before the stop, then `stopped`; a state that never comes fails after the bound."""
    ec2 = Lagging(views=[{}, {B: "running"}, {B: "running"}])
    ec2.add_instance("i-a", {**SCOPE, "participant": A}, state="stopped")
    ec2.add_instance("i-b", {**SCOPE, "participant": B}, state="stopped")
    aws = scenarios_view(ec2, tmp_path)
    await wait_state(aws, B, "stopped")
    assert ec2.reads["instances"] == 4 and fake_time.slept == [10, 10, 10]
    assert vm_state(aws, A) == "stopped"
    with pytest.raises(AssertionError, match=r"zab-test-a running: still 'stopped' after 30 s"):
        await wait_state(aws, A, "running", within=30)
    assert ec2.reads["instances"] == 9  # read at 0, 10, 20 and 30 s


def test_the_bounds_cover_the_lag_measured_live():
    """Oct 15, the #263 test VM: `disableApiTermination` read False for 157 s after a
    successful `--disable-api-termination`; a terminate right after a lift was refused."""
    import inspect

    from e2e.admin.harness.aws_rig import TERMINATE_RETRIES_S, VOLUMES_S

    assert PROTECTION_S >= 300 and WITHIN_S >= 180 and VOLUMES_S >= WITHIN_S
    assert sum(TERMINATE_RETRIES_S) >= 300
    assert inspect.signature(wait_state).parameters["within"].default >= WITHIN_S
