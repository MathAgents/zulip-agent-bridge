"""`protect.py` and `teardown.py` against an EC2 that shows and enforces a change of termination
protection late, as measured on Oct 15 (#378): reads lagged 157 s, and a `TerminateInstances`
sent right after the protection was lifted was refused (`OperationNotPermitted`)."""

from __future__ import annotations

import aws_common as ac
import protect
import pytest
import teardown

from zab.admin import runner

from .test_aws import TAG, FakeEc2, populated


class Clock:
    """The scripts' `clock` and `sleep`: time passes only when they sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.slept.append(seconds)
        self.now += seconds


class ClientError(Exception):
    """botocore's, as far as the scripts read it: the code in `response`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"An error occurred ({code}) when calling the operation: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class LaggingEc2(FakeEc2):
    """A change of termination protection shows in reads `read_lag` s after it was made, and
    in `TerminateInstances` `enforce_lag` s after it. One VM still enforced as protected refuses
    the whole call, as EC2 does."""

    def __init__(self, clock: Clock, *, read_lag: float = 0.0, enforce_lag: float = 0.0):
        super().__init__()
        self.clock, self.read_lag, self.enforce_lag = clock, read_lag, enforce_lag
        self.history: dict[str, list[tuple[float, bool]]] = {}  # instance → [(when, on)]
        self.refuse: dict[str, str] = {}  # instance → another error code, at every terminate

    def set_termination_protection(self, iid, on):
        before = self.attributes.setdefault(iid, [False, "stop"])[0]
        self.history.setdefault(iid, [(float("-inf"), before)]).append((self.clock(), on))
        super().set_termination_protection(iid, on)

    def seen(self, iid: str, lag: float) -> bool:
        if iid not in self.history:
            return self.attributes.setdefault(iid, [False, "stop"])[0]
        return [on for when, on in self.history[iid] if when + lag <= self.clock()][-1]

    def protection(self, iid):
        _, behavior = super().protection(iid)
        return self.seen(iid, self.read_lag), behavior

    def termination_protection(self, iid):
        self.calls.append(("termination_protection", iid))
        if iid in self.broken:
            raise RuntimeError("UnauthorizedOperation")
        return self.seen(iid, self.read_lag)

    def terminate(self, ids):
        self.calls.append(("terminate", list(ids)))
        for iid in ids:
            if iid in self.refuse:
                raise ClientError(self.refuse[iid], f"not {iid}")
            if self.seen(iid, self.enforce_lag):
                raise ClientError(
                    "OperationNotPermitted",
                    f"The instance '{iid}' may not be terminated. Modify its "
                    "'disableApiTermination' instance attribute and try again.",
                )
        for iid in ids:
            self.instances[iid]["State"]["Name"] = "shutting-down"


def lagging(**lags: float) -> tuple[LaggingEc2, Clock]:
    """`populated()`'s two VMs (alice i-0001 with eipalloc-0002, bob-2 i-0003 with
    eipalloc-0004) in a lagging EC2."""
    clock = Clock()
    ec2 = LaggingEc2(clock, **lags)
    source = populated()
    ec2.instances, ec2.addresses, ec2.attributes = source.instances, source.addresses, {}
    for iid in ec2.instances:
        ec2.attributes[iid] = [False, "stop"]
    return ec2, clock


def run_protect(ec2, clock, **kw) -> tuple[int, list[str]]:
    log: list[str] = []
    rc = protect.protect(ec2, TAG, log=log.append, sleep=clock.sleep, clock=clock, **kw)
    return rc, log


def run_teardown(ec2, clock, **kw) -> tuple[int, list[str]]:
    log: list[str] = []
    rc = teardown.teardown(ec2, TAG, yes=True, log=log.append, sleep=clock.sleep, clock=clock, **kw)
    return rc, log


# ----------------------------------------------------------------------------- protect.py


def test_protect_reads_it_back_until_ec2_shows_it():
    ec2, clock = lagging(read_lag=157)
    rc, log = run_protect(ec2, clock)
    assert rc == 0
    assert log[2:] == [
        "reading it back until EC2 shows it, for 300 s at most",
        "alice i-0001: termination protection on: set; EC2 shows it now",
        "bob-2 i-0003: termination protection on: set; EC2 shows it now",
    ]
    assert 157 <= clock.now < 170 and set(clock.slept) == {protect.POLL_SECONDS}
    reads = [c for c in ec2.calls if c[0] == "termination_protection"]
    assert len(reads) == 2 * (len(clock.slept) + 1)  # each VM, once per round


def test_protect_says_ec2_does_not_show_it_yet_and_still_succeeds():
    """Never a failure because the read lags: the change was made (#378)."""
    ec2, clock = lagging(read_lag=400)
    rc, log = run_protect(ec2, clock, only=["alice"], off=False)
    assert rc == 0
    assert log[-1] == (
        "alice i-0001: termination protection on: set; EC2 doesn't show it yet "
        "(it can take a few minutes)"
    )
    assert clock.now == protect.SETTLE_SECONDS == 300  # bounded, all VMs together
    assert ec2.attributes["i-0001"][0] is True


def test_protect_off_is_read_back_too_and_only_the_vms_it_changed():
    ec2, clock = lagging(read_lag=60)
    run_protect(ec2, clock)  # both on, then 157 s … and alice off
    ec2.calls.clear()
    rc, log = run_protect(ec2, clock, only=["alice"], off=True)
    assert rc == 0 and log[-1] == "alice i-0001: termination protection off: set; EC2 shows it now"
    assert {c[1] for c in ec2.calls if c[0] == "termination_protection"} == {"i-0001"}


def test_a_failing_read_back_is_said_and_is_no_failure():
    ec2, clock = lagging()
    original = ec2.termination_protection

    def flaky(iid):
        if iid == "i-0001":
            raise RuntimeError("RequestLimitExceeded")
        return original(iid)

    ec2.termination_protection = flaky
    rc, log = run_protect(ec2, clock)
    assert rc == 0 and clock.now == protect.SETTLE_SECONDS
    assert log[-2] == (
        "alice i-0001: termination protection on: set; EC2 doesn't show it yet (it can take a "
        "few minutes); its last read failed: RuntimeError: RequestLimitExceeded"
    )
    assert log[-1] == "bob-2 i-0003: termination protection on: set; EC2 shows it now"


def test_no_wait_and_dry_run_read_nothing_back(monkeypatch):
    ec2, clock = lagging(read_lag=157)
    rc, log = run_protect(ec2, clock, wait=False)
    assert rc == 0 and clock.slept == []
    assert not any(c[0] == "termination_protection" for c in ec2.calls)
    assert log[-1] == (
        "bob-2 i-0003: termination protection on: set; not read back (--no-wait): EC2 can take "
        "a few minutes to show it"
    )
    ec2, clock = lagging(read_lag=157)
    rc, log = run_protect(ec2, clock, dry_run=True)
    assert rc == 0 and clock.slept == [] and log[-1] == "dry run: nothing changed"
    seen: dict = {}
    monkeypatch.setattr(protect, "Ec2", lambda region: object())
    monkeypatch.setattr(protect, "protect", lambda ec2, tag, **kw: seen.update(kw) or 0)
    assert protect.main(["--no-wait"]) == 0 and seen["wait"] is False
    assert protect.main([]) == 0 and seen["wait"] is True


# ---------------------------------------------------------------------------- teardown.py


def test_teardown_retries_a_vm_whose_lifted_protection_ec2_still_enforces():
    """The admin e2e's run at 14:01Z: reads show it lifted, EC2 still refuses."""
    ec2, clock = lagging(enforce_lag=157)
    ec2.set_termination_protection("i-0001", True)
    clock.now = 1000.0
    ec2.set_termination_protection("i-0001", False)  # protect.py --off, just now
    rc, log = run_teardown(ec2, clock)
    assert rc == 0
    assert "EC2 refused the call (ClientError: An error occurred (OperationNotPermitted)" in (
        "\n".join(log)
    )
    assert "EC2 still refuses i-0001 as protected: trying again in 5 s" in log
    assert clock.slept[:4] == [5, 10, 20, 30] and max(clock.slept) == teardown.RETRY_MAX
    assert 157 <= clock.now - 1000 <= teardown.RETRY_SECONDS
    assert all(i["State"]["Name"] == "shutting-down" for i in ec2.instances.values())
    assert not ec2.addresses  # both released


def test_teardown_reports_a_vm_still_refused_and_tears_down_the_others():
    ec2, clock = lagging(enforce_lag=10_000)
    clock.now = -20_000.0
    ec2.set_termination_protection("i-0001", True)  # kept, long ago
    clock.now = 0.0
    ec2.set_termination_protection("i-0001", False)  # and lifted, but EC2 enforces it on
    rc, log = run_teardown(ec2, clock)
    assert rc == 1
    assert clock.now == teardown.RETRY_SECONDS  # bounded: about 5 minutes
    assert (
        "refused i-0001 ws-alice (participant alice): EC2 still refuses it as protected after "
        "300 s: its termination protection is on, or was lifted moments ago; check, then run "
        "teardown.py again"
    ) in log
    assert ec2.instances["i-0001"]["State"]["Name"] == "running"
    assert ec2.instances["i-0003"]["State"]["Name"] == "shutting-down"
    assert ("wait_terminated", ["i-0003"]) in ec2.calls
    assert list(ec2.addresses) == ["eipalloc-0002"]  # alice's stays with her VM
    assert (
        "keep eipalloc-0002 203.0.113.2: its instance i-0001 is not terminated; run "
        "teardown.py again"
    ) in log


def test_teardown_retries_only_a_refusal_as_protected():
    ec2, clock = lagging()
    ec2.refuse["i-0001"] = "InvalidInstanceID.Malformed"
    rc, log = run_teardown(ec2, clock)
    assert rc == 1 and clock.slept == []
    assert [c[1] for c in ec2.calls if c[0] == "terminate"] == [
        ["i-0001", "i-0003"], ["i-0001"], ["i-0003"]
    ]  # fmt: skip
    assert any(
        line.startswith("refused i-0001 ws-alice (participant alice): EC2 refused it: ClientError")
        for line in log
    )
    assert list(ec2.addresses) == ["eipalloc-0002"]


def test_a_failed_wait_or_release_keeps_the_rest_going():
    ec2, clock = lagging()

    def wait_fails(ids):
        raise RuntimeError("Waiter InstanceTerminated failed: Max attempts exceeded")

    ec2.wait_terminated = wait_fails
    rc, log = run_teardown(ec2, clock)
    assert rc == 1 and ec2.addresses.keys() == {"eipalloc-0002", "eipalloc-0004"}
    assert log[-3].startswith("FAILED: waiting for i-0001, i-0003 to terminate: RuntimeError")
    ec2, clock = lagging()
    release = ec2.release_address

    def release_fails(aid):
        if aid == "eipalloc-0002":
            raise RuntimeError("InvalidIPAddress.InUse")
        release(aid)

    ec2.release_address = release_fails
    rc, log = run_teardown(ec2, clock)
    assert rc == 1 and list(ec2.addresses) == ["eipalloc-0002"]
    assert log[-2:] == [
        "FAILED: release eipalloc-0002: RuntimeError: InvalidIPAddress.InUse",
        "released eipalloc-0004",
    ]


@pytest.mark.parametrize("read_lag", [0.0, 157.0])
def test_teardown_keeps_a_vm_protected_long_ago(read_lag):
    """The safe direction: a VM whose protection EC2 already shows is `keep`, never tried."""
    ec2, clock = lagging(read_lag=read_lag, enforce_lag=read_lag)
    ec2.set_termination_protection("i-0001", True)
    clock.now = 1000.0
    rc, log = run_teardown(ec2, clock)
    assert rc == 0 and log[0].startswith("keep i-0001 ws-alice: termination protection is on")
    assert [c[1] for c in ec2.calls if c[0] == "terminate"] == [["i-0003"]]


# ------------------------------------------------------------------- the admin bot's timeouts

#: Besides the waits: the lookups, one call per VM, and the releases, at most this long.
CALLS = 300


def test_the_bots_timeouts_cover_the_scripts_worst_case():
    """§2.4: the runner kills a step at its verb's timeout (`runner.TIMEOUTS`). A teardown
    retries for `RETRY_SECONDS` (its last pause is cut to the deadline), then waits for EC2 at
    most `TERMINATED_WAITER`'s 40 times 15 s; a protect reads back for `SETTLE_SECONDS`."""
    waiter = ac.TERMINATED_WAITER["Delay"] * ac.TERMINATED_WAITER["MaxAttempts"]
    assert waiter == 600
    assert runner.timeout("teardown") >= teardown.RETRY_SECONDS + waiter + CALLS
    assert runner.timeout("protect") >= protect.SETTLE_SECONDS + protect.POLL_SECONDS + CALLS


def test_teardown_waits_with_the_pinned_waiter():
    seen: dict = {}

    class Waiter:
        def wait(self, **kw):
            seen.update(kw)

    class Client:
        def get_waiter(self, name):
            seen["name"] = name
            return Waiter()

    ec2 = object.__new__(ac.Ec2)  # no boto3: only the client's waiter is used
    ec2._ec2 = Client()
    ec2.wait_terminated(["i-1"])
    assert seen == {
        "name": "instance_terminated",
        "InstanceIds": ["i-1"],
        "WaiterConfig": {"Delay": 15, "MaxAttempts": 40},
    }


def test_a_teardown_that_retries_to_the_end_stays_within_its_bound():
    ec2, clock = lagging(enforce_lag=10_000)
    clock.now = -20_000.0
    ec2.set_termination_protection("i-0001", True)
    clock.now = 0.0
    ec2.set_termination_protection("i-0001", False)
    run_teardown(ec2, clock)
    assert sum(clock.slept) == clock.now == teardown.RETRY_SECONDS  # never past it
