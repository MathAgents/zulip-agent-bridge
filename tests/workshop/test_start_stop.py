"""A stopped VM started for its row, and stopped again (#995): `power.StartStop`, the VM's read of
its use (`idle_stop.py --in-use`, `admin_common.vm_in_use`), `push_config.py --start-stopped` and
`trust_fix.py`'s, against the fake EC2 of `test_power.py` and fake runners."""

from __future__ import annotations

import json
import pathlib
import subprocess
import threading
import time
from types import SimpleNamespace

import admin_common as ac
import idle_stop as idle
import power
import push_config as pc
import pytest
import trust_fix

from .test_admin_idle_stop import MONO, NOW, FakeRun, proc
from .test_admin_push import ALICE, KH, FakeRunner, _release_is_this_checkout, out_dir, roster
from .test_power import FakeEc2, vm
from .test_trust_fix import Runner as TrustRunner

__all__ = ["_release_is_this_checkout", "out_dir", "proc", "roster"]  # the fixtures, by name

ROWS = [
    {"slug": "alice", "ip": "203.0.113.1", "host": "ws-alice"},
    {"slug": "bob", "ip": "203.0.113.2", "host": "ws-bob"},
    {"slug": "carol", "ip": "", "host": ""},
]
SCOPE = {"workshop": "nt-agents-2026"}


class Ec2(FakeEc2):
    """`test_power`'s fake, thread-safe, whose start answers each VM's `PreviousState`, as EC2's
    `StartingInstances` does; `first` gives one started by someone else just before; `mute`,
    a start that answers nothing; `error`, one that raises it, after acting with `acts` (an
    answer lost, or the waker's start just before a refused one), before it otherwise; `fail`,
    the numbers of the lookups that fail (the first is 1)."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.first: dict[str, str] = {}
        self.mute = self.acts = False
        self.error: Exception | None = None
        self.fail: set[int] = set()

    def find_instances(self, tags, states, *, deadline=None, clock=None):
        with self.lock:
            if len(self.lookups()) + 1 in self.fail:
                self.calls.append(("find_instances", dict(tags), states))
                raise RuntimeError("RequestLimitExceeded: slow down")
            return super().find_instances(tags, states, deadline=deadline, clock=clock)

    def start(self, ids):
        with self.lock:
            before = {i: self.first.get(i) or self.state(i) for i in ids}
            if self.error is not None and not self.acts:
                self.calls.append(("start", list(ids)))
                raise self.error
            super().start(ids)
        if self.error is not None:
            raise self.error
        return [] if self.mute else [
            {"InstanceId": i, "PreviousState": {"Name": s}} for i, s in before.items()
        ]  # fmt: skip

    def stop(self, ids):
        with self.lock:
            super().stop(ids)


class ClientError(Exception):
    """botocore's: EC2 answered the request with this error (its `response`), and HTTP `status`."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(f"An error occurred ({code}) when calling the StartInstances operation")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def fleet(bob: str = "stopped") -> Ec2:
    ec2 = Ec2()
    ec2.add("i-alice", "203.0.113.1", "running", vm("alice"))
    ec2.add("i-bob", "203.0.113.2", bob, vm("bob"))
    return ec2


def window(
    ec2: Ec2, *, used=(), at_once: int = 4, answers=lambda ip: True, log=None, ready=None,
    rows=ROWS, halted_in="",
):  # fmt: skip
    seen: list[str] = []
    made = power.StartStop(
        ec2, SCOPE, rows, at_once=at_once, in_use=lambda row: list(used),
        unread_reasons=idle.UNREAD, halted_in=halted_in,
        log=(log if log is not None else seen.append), answers=answers, ready=ready,
        sleep=ec2.time.sleep, clock=ec2.time.monotonic,
    )  # fmt: skip
    return made, seen


def go(made: power.StartStop, slug: str, ran: list[str] | None = None) -> str:
    row = next(r for r in ROWS if r["slug"] == slug)
    done = ran if ran is not None else []
    return made.around(row, lambda: done.append(slug) or "done", lambda why: f"refused: {why}")


def test_a_running_vm_is_worked_on_and_left_running():
    ec2 = fleet()
    made, seen = window(ec2)
    assert go(made, "alice") == "done"
    assert made.cells == {"alice": power.RUNNING} and not ec2.changes() and not seen


def test_a_stopped_vm_is_started_worked_on_and_stopped_again():
    ec2 = fleet()
    made, seen = window(ec2)
    order: list[str] = []
    row = ROWS[1]
    result = made.around(row, lambda: order.append(ec2.state("i-bob")) or "done", lambda why: why)
    assert result == "done" and order == ["running"]  # up before its work
    assert ec2.changes() == [("start", ["i-bob"]), ("stop", ["i-bob"])]
    assert made.cells["bob"] == power.STOPPED_AGAIN
    assert seen == ["power: bob i-bob: started for its row", "power: bob i-bob: stopped again"]
    assert made.summary() == "stopped VMs: 1 started, 1 stopped again"


def test_a_vm_used_since_its_start_is_left_running():
    ec2 = fleet()
    made, _ = window(ec2, used=["zab active since its start"])
    assert go(made, "bob") == "done"
    assert ec2.changes() == [("start", ["i-bob"])]
    assert made.cells["bob"] == "started, left running: zab active since its start"
    assert made.summary() == "stopped VMs: 1 started, 0 stopped again; left running: bob"


def test_a_check_that_fails_leaves_it_running_and_a_failed_work_still_stops_it():
    ec2 = fleet()
    made = power.StartStop(ec2, SCOPE, ROWS, at_once=1, in_use=lambda row: 1 / 0, log=print,
                           unread_reasons=idle.UNREAD, answers=lambda ip: True,
                           sleep=ec2.time.sleep)  # fmt: skip
    assert go(made, "bob") == "done"
    assert made.cells["bob"] == "started, left running: its use can't be read: ZeroDivisionError"
    ec2 = fleet()
    made, _ = window(ec2)

    def broken() -> str:
        raise RuntimeError("the rollout broke")

    with pytest.raises(RuntimeError):
        made.around(ROWS[1], broken, lambda why: why)
    assert ec2.changes() == [("start", ["i-bob"]), ("stop", ["i-bob"])]


def test_a_vm_the_waker_started_first_is_left_running():
    ec2 = fleet()
    ec2.first["i-bob"] = "pending"  # its participant's message, a moment before our start
    made, seen = window(ec2)
    assert go(made, "bob") == "done"
    assert ec2.changes() == [("start", ["i-bob"])] and made.cells["bob"] == power.RUNNING
    assert seen == ["power: bob i-bob: pending already, started by someone else: left running"]


def test_a_vm_still_stopping_is_waited_for_then_started():
    ec2 = fleet(bob="stopping")  # the fake stops it after one lookup
    made, _ = window(ec2)
    assert go(made, "bob") == "done" and made.cells["bob"] == power.STOPPED_AGAIN
    ec2 = fleet(bob="stopping")
    ec2.slow.add("i-bob")  # never stops
    made, _ = window(ec2)
    assert go(made, "bob") == "refused: not started: it is stopping after 5 minutes"
    assert not ec2.changes()


def test_a_failed_start_is_refused_and_one_that_never_answers_stopped_again():
    ec2 = fleet()
    ec2.broken.add("i-bob")
    made, _ = window(ec2)
    ran: list[str] = []
    said = go(made, "bob", ran)
    assert said.startswith("refused: not started: RuntimeError: IncorrectInstanceState") and not ran
    ec2 = fleet()
    made, _ = window(ec2, answers=lambda ip: False)
    said = go(made, "bob", ran)
    assert said == (
        "refused: started, but its SSH (203.0.113.2:22) doesn't answer after 5 minutes: "
        "stopped again"
    )
    assert not ran and ec2.changes() == [("start", ["i-bob"]), ("stop", ["i-bob"])]
    assert made.summary() == "stopped VMs: 1 started, 1 stopped again; failed: bob"


def test_a_stop_that_fails_names_the_command():
    ec2 = fleet()
    made, _ = window(ec2)
    real = ec2.stop

    def refuse(ids):
        real(ids)
        raise RuntimeError("UnauthorizedOperation: no")

    ec2.stop = refuse  # type: ignore[method-assign]
    go(made, "bob")
    assert made.cells["bob"] == (
        "started, NOT stopped again (RuntimeError: UnauthorizedOperation: no): !stop bob"
    )


def test_rows_without_a_vm_and_a_failed_lookup_run_as_before():
    ec2 = fleet()
    made, _ = window(ec2)
    assert go(made, "carol") == "done" and "carol" not in made.cells
    ec2.down = True
    made, _ = window(ec2)
    assert made.off.startswith("the EC2 lookup failed: RuntimeError: ExpiredToken")
    assert go(made, "bob") == "done" and not ec2.changes() and not made.cells
    other = fleet()
    other.instances[1]["Tags"] = [{"Key": "workshop", "Value": "nt-agents-2026"},
                                  {"Key": "participant", "Value": "dave"}]  # fmt: skip
    made, _ = window(other)
    assert go(made, "bob") == "done" and not other.changes()  # not bob's VM: push_config refuses


def test_at_most_n_started_vms_are_worked_on_at_once():
    """Criterion 2: the slots bound the VMs this run started and hasn't decided on yet, here 3;
    the first two to get in wait for each other, so at least two did at once (review of #1026,
    N5: never an exact count that a loaded runner's timing decides)."""
    ec2 = Ec2()
    rows = [{"slug": f"p{n}", "ip": f"203.0.113.{n}", "host": ""} for n in range(1, 9)]
    for n in range(1, 9):
        ec2.add(f"i-{n}", f"203.0.113.{n}", "stopped", vm(f"p{n}"))
    made = power.StartStop(ec2, SCOPE, rows, at_once=3, in_use=lambda row: [], log=print,
                           unread_reasons=idle.UNREAD, answers=lambda ip: True,
                           sleep=lambda s: None)  # fmt: skip
    lock, up, most, entered = threading.Lock(), [0], [0], [0]
    first_two = threading.Barrier(2, timeout=10)

    def work() -> str:
        with lock:
            up[0] += 1
            entered[0] += 1
            most[0], early = max(most[0], up[0]), entered[0] <= 2
        if early:
            first_two.wait()
        time.sleep(0.01)
        with lock:
            up[0] -= 1
        return "done"

    results = ac.run_parallel(rows, lambda row: made.around(row, work, str), parallel=8,
                              on_error=lambda row, exc: repr(exc))  # fmt: skip
    assert results == ["done"] * 8 and 2 <= most[0] <= 3
    assert {c for c in made.cells.values()} == {power.STOPPED_AGAIN}


def test_a_start_ec2_doesnt_say_was_ours_is_someone_elses():
    """Review of #1026, N1: no `PreviousState` is no proof that this run started it."""
    ec2 = fleet()
    ec2.mute = True
    made, seen = window(ec2)
    assert go(made, "bob") == "done" and made.cells["bob"] == power.RUNNING
    assert ec2.changes() == [("start", ["i-bob"])]
    assert seen == ["power: bob i-bob: EC2 didn't say it was stopped before this start: left "
                    "running"]  # fmt: skip


#: The cell of a VM up after this run's start failed: left running, never stopped.
def up_after(error: str) -> str:
    return f"start failed ({error}), VM up: left running"


UNAUTHORIZED = "ClientError: An error occurred (UnauthorizedOperation) when calling the "
UNAUTHORIZED += "StartInstances operation"
TIMEOUT = "RuntimeError: ReadTimeoutError: read timed out"


def test_the_wakers_start_then_a_refused_one_is_never_claimed():
    """Round 2 of #1026 (Codex's P2, the review's B1): the waker started the VM between this
    run's read and its request, which EC2 refused: up, it is left running and said so, never
    stopped; one read, since EC2's answer says this start didn't act."""
    ec2 = fleet()
    ec2.error, ec2.acts = ClientError("UnauthorizedOperation"), True
    made, seen = window(ec2)
    assert go(made, "bob") == "done" and made.cells["bob"] == up_after(UNAUTHORIZED)
    assert ec2.changes() == [("start", ["i-bob"])]
    assert seen == [f"power: bob i-bob: {up_after(UNAUTHORIZED)}"]
    assert made.summary() == (
        "stopped VMs: 0 started, 0 stopped again; up after a failed start, left running: bob"
    )


def test_a_start_whose_answer_is_lost_is_never_claimed():
    """The same when the start's answer was lost and EC2 then shows the VM up: this run's
    start or another's, it is left running and said so, never stopped."""
    ec2 = fleet()
    ec2.error, ec2.acts = RuntimeError("ReadTimeoutError: read timed out"), True
    made, _ = window(ec2)
    assert go(made, "bob") == "done" and made.cells["bob"] == up_after(TIMEOUT)
    assert ec2.changes() == [("start", ["i-bob"])]


def test_a_lost_answer_with_ec2_still_reading_stopped_says_to_look():
    """EC2's reads lag its writes: after 30 s of `stopped`, a start may still land. The row is
    refused, and says so, never a bare `not started`."""
    ec2 = fleet()
    ec2.error, ec2.acts = RuntimeError("ReadTimeoutError: read timed out"), True
    ec2.after["i-bob"] = "stopped"  # what the reads show, whatever the start did
    made, _ = window(ec2)
    ran: list[str] = []
    said = go(made, "bob", ran)
    assert said == f"refused: not started: {TIMEOUT}; a start may still reach it: look at it"
    assert not ran and ec2.time.now >= power.LOST_START_S


def test_an_error_ec2_answered_on_a_stopped_vm_is_a_plain_refusal():
    """EC2 answered (`ClientError`): this start didn't act, so one read, no 30 s of them; also
    with a server status, since its code says why it couldn't (#1031: `power.answered`)."""
    ec2 = fleet()
    ec2.error = ClientError("InsufficientInstanceCapacity", 500)
    made, _ = window(ec2)
    said = go(made, "bob")
    error = "An error occurred (InsufficientInstanceCapacity) when calling the StartInstances"
    assert said == f"refused: not started: ClientError: {error} operation"
    assert len(ec2.lookups()) == 3 and ec2.time.now == 0  # the map, the row's, the one read


SERVER_ERROR = "ClientError: An error occurred (InternalError) when calling the StartInstances "
SERVER_ERROR += "operation"


def test_a_server_error_on_a_stopped_vm_says_a_start_may_still_reach_it():
    """#1031 (review of #1026, N1): a 5xx `ClientError` is EC2's answer, but no proof that the
    start didn't act: 30 s of reads, as after a timeout, then `a start may still reach it`; and
    a VM that comes up meanwhile is left running, never stopped."""
    ec2 = fleet()
    ec2.error, ec2.acts = ClientError("InternalError", 500), True
    ec2.after["i-bob"] = "stopped"  # EC2's reads lag its writes
    made, _ = window(ec2)
    said = go(made, "bob")
    assert said == f"refused: not started: {SERVER_ERROR}; a start may still reach it: look at it"
    assert ec2.time.now >= power.LOST_START_S
    ec2 = fleet()
    ec2.error, ec2.acts = ClientError("InternalError", 500), True
    made, _ = window(ec2)
    assert go(made, "bob") == "done" and made.cells["bob"] == up_after(SERVER_ERROR)
    assert ec2.changes() == [("start", ["i-bob"])]


@pytest.mark.parametrize(
    ("code", "status", "answered"),
    [("UnauthorizedOperation", 403, True), ("IncorrectInstanceState", 400, True),
     ("InternalError", 500, False), ("ServiceUnavailable", 503, False), ("Unavailable", 503, False),
     ("InsufficientInstanceCapacity", 500, True), ("RequestLimitExceeded", 503, True),
     ("EC2ThrottledException", 503, True),
     ("InsufficientInstanceCapacity", 400, True)],
)  # fmt: skip
def test_which_errors_say_a_start_did_nothing(code, status, answered):
    """#1031: a 4xx, and a 5xx that says why EC2 couldn't (no capacity, throttled), did nothing;
    any other 5xx may hide a start that acted; no `response` (a timeout) always may."""
    assert power.answered(ClientError(code, status)) is answered
    assert not power.answered(RuntimeError("ReadTimeoutError: read timed out"))


def test_a_failed_lookup_while_it_comes_up_is_read_again():
    """Review of #1026, N3: a throttled lookup ends no wait; only the deadline does."""
    ec2 = fleet()
    ec2.fail = {3, 4}  # the first two reads of the wait, after the map and the row's own
    made, _ = window(ec2)
    assert go(made, "bob") == "done" and made.cells["bob"] == power.STOPPED_AGAIN
    ec2 = fleet()
    ec2.fail = set(range(3, 1000))
    made, _ = window(ec2)
    said = go(made, "bob")
    head = "refused: started, but the EC2 lookup failed: RuntimeError: RequestLimitExceeded: "
    assert said.startswith(f"{head}slow down, still after 5 minutes: "), said


def test_port_22_answering_is_not_enough_a_login_must_get_in():
    """Review of #1026, N4: sshd answers before a boot lets logins in; `ready` is tried until it
    does, and a VM that never lets one in is stopped again."""
    ec2 = fleet()
    tries = iter([False, False, True])
    made, _ = window(ec2, ready=lambda row: next(tries))
    assert go(made, "bob") == "done" and made.cells["bob"] == power.STOPPED_AGAIN
    ec2 = fleet()
    made, _ = window(ec2, ready=lambda row: False)
    said = go(made, "bob")
    assert said == ("refused: started, but its SSH (203.0.113.2:22) lets no login in after 5 "
                    "minutes: stopped again")  # fmt: skip


def test_after_three_unread_checks_in_a_row_no_more_vms_are_started():
    """Review of #1026, B2: a check broken on every VM would start the whole fleet and leave it
    up; after `UNREAD_LIMIT` unread checks in a row, the run starts no more, and says so."""
    ec2 = Ec2()
    rows = [{"slug": f"p{n}", "ip": f"203.0.113.{n}", "host": ""} for n in range(1, 6)]
    for n in range(1, 6):
        ec2.add(f"i-{n}", f"203.0.113.{n}", "stopped", vm(f"p{n}"))
    made, seen = window(ec2, used=["turns unknown"], rows=rows)
    said = [made.around(row, lambda: "done", lambda why: why) for row in rows]
    halted = "the use of 3 started VMs in a row couldn't be read, so no more are started"
    assert said == ["done"] * 3 + [f"not started: {halted}"] * 2 and made.tripped
    assert ec2.changes() == [("start", [f"i-{n}"]) for n in (1, 2, 3)]
    assert f"power: {halted}" in seen and made.summary().endswith(f"; {halted}")
    ec2 = fleet()  # one that reads, between unread ones, starts the count again
    made, _ = window(ec2, used=["2 turn(s) running"])
    made.unread = 2
    go(made, "bob")
    assert made.unread == 0 and not made.halted


def test_a_run_told_another_workshops_halted_starts_none():
    """#1031: `halted_in`, the workshop whose run of the same plan halted its starts: from the
    first row, a stopped VM is refused and never started, a running one is worked on; its own
    breaker never trips (`tripped`), so its exit says nothing new."""
    ec2 = fleet()
    made, seen = window(ec2, halted_in="harvard-2026")
    halt = "the use of 3 started VMs in a row couldn't be read in harvard-2026, so no more are "
    halt += "started"
    assert made.halted == halt and not made.tripped
    assert go(made, "alice") == "done" and made.cells["alice"] == power.RUNNING
    assert go(made, "bob") == f"refused: not started: {halt}"
    assert ec2.changes() == [] and not seen
    assert made.summary() == f"stopped VMs: 0 started, 0 stopped again; failed: bob; {halt}"


def test_unread_is_exactly_what_in_use_gives_for_what_it_cant_read():
    """#1031 (review of #1026, N3): one constant, `idle_stop.UNREAD`, from which `in_use` words
    its unread reasons and which `StartStop` is given: with no answer, and with every fact
    missing, `in_use` gives each of them once, and nothing else; a fact read as unknown that
    isn't in it, a seventh say, fails here. Its own prefix reads as `vm_in_use`'s."""
    missing = idle.in_use({})
    assert set(missing) | set(idle.in_use(None)) == idle.UNREAD
    assert len(missing) == len(set(missing)) == len(idle.UNREAD) - 1
    assert power.UNREAD_PREFIX == f"{idle.USE_UNREAD}: "


@pytest.mark.parametrize("fact", [{"turns": None}, {"jobs": "x"}, {"activity": "x"},
                                  {"logins": None}, {"uptime": None}])  # fmt: skip
def test_every_unknown_fact_is_worded_as_unread(fact):
    """`idle_stop.UNREAD` is how StartStop tells an unread check from a VM in use: `in_use`'s own
    reasons, exactly; a login's name that holds `unknown` is a use (round 2 of #1026, N1)."""
    (reason,) = idle.in_use(facts(**fact))
    assert reason in idle.UNREAD, reason
    ec2 = fleet()
    made, _ = window(ec2, used=["logged in: unknown@pts/0"])
    made.unread = 2
    go(made, "bob")
    assert made.unread == 0 and made.cells["bob"].endswith("logged in: unknown@pts/0")


# ------------------------------------------------------------------- the VM's read of its use


def facts(**changes) -> dict:
    out = {"now": NOW, "uptime": 300.0, "turns": 0, "activity": NOW - 3600, "jobs": 0,
           "logins": []}  # fmt: skip
    return {**out, **changes}


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({}, []),
        ({"activity": None}, []),
        ({"activity": NOW - 60}, ["zab active since its start"]),
        ({"turns": 2}, ["2 turn(s) running"]),
        ({"jobs": 1}, ["1 background job(s) running"]),
        ({"logins": ["ubuntu@pts/0"]}, ["logged in: ubuntu@pts/0"]),
        ({"logins": ["\x1b[2J@x"]}, ["logged in: 1 session(s)"]),
        ({"turns": None, "jobs": "3"}, ["turns unknown", "background jobs unknown"]),
        ({"uptime": None}, ["its uptime unknown"]),
        ({"logins": None, "activity": "x"}, ["zab's activity unknown", "logins unknown"]),
        ({"activity": idle.NO_ACTIVITY_KEY}, ["zab's activity unknown"]),  # zab up to 0.2.3
    ],
)
def test_in_use_reads_the_vms_answer(changes, reasons):
    assert idle.in_use(facts(**changes)) == reasons


def test_in_use_of_no_answer():
    assert idle.in_use(None) == idle.in_use([1]) == ["its use can't be read"]
    gone = facts()
    del gone["activity"]  # a fact that isn't there is unknown (review of #1026, B1)
    assert idle.in_use(gone) == ["zab's activity unknown"]


@pytest.mark.parametrize(
    "status", ['{"running_turns": []}', '{"running_turns": [], "last_activity": "2026"}']
)
def test_activity_zab_doesnt_give_as_a_number_is_unknown(proc, tmp_path, status):
    """Review of #1026, B1: zab up to 0.2.3 has no `last_activity`, and a VM's owner controls
    what it prints: either is unknown, never "no activity"; null alone is none."""
    config = tmp_path / "config.toml"
    config.write_text("")  # a zab set up: its status is read
    got = idle.use_facts(StatusRun(status, who="", jobs=""), str(proc), now=NOW, pid=70,
                         monotonic=MONO, config=str(config))  # fmt: skip
    assert got["turns"] == 0 and idle.in_use(got) == ["zab's activity unknown"]


class StatusRun(FakeRun):
    """`FakeRun`, and `zab status --json` as agent."""

    def __init__(self, status: str, code: int = 0, **kw):
        super().__init__(**kw)
        self.status, self.code = status, code

    def __call__(self, argv, **kwargs):
        if argv == idle.ZAB_STATUS:
            self.calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, self.code, self.status, "")
        return super().__call__(argv, **kwargs)


def test_use_facts_reads_zab_as_agent_and_leaves_out_its_own_session(proc, tmp_path):
    """The check's own SSH session is an ancestor of its process; the status reads are no one's
    login; another session is. A VM whose zab has no config yet runs no bridge: no turn."""
    config = tmp_path / "config.toml"
    config.write_text("")
    for pid, ppid, title in ((50, 1, "sshd: ubuntu@notty"), (60, 50, "bash"),
                             (70, 60, "python3"), (80, 1, "sshd: zab-monitor@notty"),
                             (90, 1, "sshd-session: ubuntu@pts/1")):  # fmt: skip
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "cmdline").write_bytes(title.encode() + b"\0")
        (proc / str(pid) / "stat").write_text(f"{pid} ({title.split()[0]}) S {ppid} 1 1 0\n")
    status = json.dumps({"sessions": [], "running_turns": [{"session_id": 1}],
                         "last_activity": NOW - 30})  # fmt: skip
    run = StatusRun(status, who="", jobs="")
    got = idle.use_facts(run, str(proc), now=NOW, pid=70, monotonic=MONO, config=str(config))
    assert got == {"now": NOW, "uptime": 7200.0, "turns": 1, "activity": NOW - 30, "jobs": 0,
                   "logins": ["ubuntu@pts/1"]}  # fmt: skip
    (_, kwargs), *_ = [c for c in run.calls if c[0] == idle.ZAB_STATUS]
    assert kwargs["user"] == kwargs["group"] == "agent" and kwargs["env"]["HOME"] == "/home/agent"
    assert idle.in_use(got) == [
        "1 turn(s) running",
        "zab active since its start",
        "logged in: ubuntu@pts/1",
    ]
    run = StatusRun('{"error": "OperationalError"}', code=1, who="", jobs=None)
    got = idle.use_facts(run, str(proc), now=NOW, pid=70, monotonic=MONO, config=str(config))
    assert got["turns"] is None and got["jobs"] is None
    assert idle.in_use(got)[:2] == ["turns unknown", "background jobs unknown"]
    config.unlink()  # never rolled out: zab isn't asked
    run = StatusRun('{"error": "FileNotFoundError"}', code=1, who="", jobs="")
    got = idle.use_facts(run, str(proc), now=NOW, pid=70, monotonic=MONO, config=str(config))
    assert got["turns"] == 0 and idle.ZAB_STATUS not in [c[0] for c in run.calls]


def test_main_in_use_prints_the_facts_and_writes_nothing(tmp_path, proc, capsys):
    run = StatusRun('{"running_turns": [], "last_activity": null}', who="", jobs="")
    config = tmp_path / "config.toml"
    config.write_text("")
    assert idle.main(["--in-use"], run=run, proc=str(proc), now=NOW, monotonic=MONO,
                     config=str(config)) == 0  # fmt: skip
    printed = json.loads(capsys.readouterr().out)
    assert printed["turns"] == 0 and idle.in_use(printed) == []
    assert [c[0][0] for c in run.calls] == [idle.ZAB_STATUS[0], "systemctl", "who"]


def test_vm_in_use_sends_idle_stop_on_stdin_and_reads_its_last_line():
    runner = FakeRunner(answers={ac.IN_USE_CMD: "noise\n" + json.dumps(facts(turns=1)) + "\n"})
    assert ac.vm_in_use(runner, ALICE, KH) == ["1 turn(s) running"]
    assert runner.calls[-1][-1] == ac.IN_USE_CMD and runner.calls[-1][-3] == f"ubuntu@{ALICE}"
    assert runner.stdins[-1] == ac.IDLE_STOP_SCRIPT.read_text()
    runner = FakeRunner(fail={ac.IN_USE_CMD: "sudo: a password is required"})
    assert ac.vm_in_use(runner, ALICE, KH) == [
        "its use can't be read: sudo: a password is required"
    ]
    runner = FakeRunner(answers={ac.IN_USE_CMD: "not json\n"})
    assert ac.vm_in_use(runner, ALICE, KH) == ["its use can't be read"]


# ------------------------------------------------------------------- push_config.py's flag


def push(roster, out_dir, *extra: str) -> list[str]:
    return [*("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
            *("--known-hosts", KH), *extra]  # fmt: skip


START = ("--start-stopped", "8", "--region", "us-west-2", "--scope-tag", "workshop=w")


def test_push_config_dry_run_says_so_and_reads_no_ec2(roster, out_dir, monkeypatch, capsys):
    """The dry run, which the bot hashes, holds one fixed line and no VM's state."""
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    monkeypatch.setattr(pc, "start_stop", lambda *a, **k: pytest.fail("EC2 in a dry run"))
    assert pc.main(push(roster, out_dir, *START, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert ac.start_stopped_line(8) in out.splitlines()
    table = [line for line in out.splitlines() if line.startswith("participant")]
    assert table and "power" not in table[0]


def test_push_config_wraps_each_row_and_adds_the_column(roster, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    calls: list[str] = []

    class Window:
        off = ""
        halted, tripped = "", False

        def __init__(self) -> None:
            self.cells = {"alice": power.RUNNING, "bob": "not started: it is shutting-down"}

        def around(self, row, work, refused):
            calls.append(ac.slug_of(row))
            return work() if ac.slug_of(row) == "alice" else refused(self.cells["bob"])

        def summary(self) -> str:
            return "stopped VMs: 0 started, 0 stopped again; failed: bob"

    monkeypatch.setattr(pc, "start_stop", lambda *a, **k: Window())
    assert pc.main(push(roster, out_dir, *START)) == 1
    out = capsys.readouterr().out
    assert sorted(calls) == ["alice", "bob"]
    lines = out.splitlines()
    head = next(line for line in lines if line.startswith("participant"))
    assert head.split() == ["participant", "host", "install", "doctor", "power", "failures"]
    bob = next(line for line in lines if line.startswith("bob"))
    assert "FAIL" in bob and bob.count("not started: it is shutting-down") == 2
    assert lines[-1] == Window().summary()


def test_push_config_exits_halted_when_its_own_breaker_tripped(roster, out_dir, monkeypatch,
                                                                capsys):  # fmt: skip
    """#1031: its exit says it halted its starts, so that a plan of every workshop halts them
    in its later parts: `HALTED_EXIT` with every row done all the same, `HALTED_FAILED_EXIT`
    with a row refused (review round 1, N2: the ledger tells them apart). A run told so
    (`--halted-in`) says it first, and exits as usual."""
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())

    class Window:
        off, cells = "", {}

        def __init__(self, halted: str = "", trips: bool = False, refuses: str = "") -> None:
            self.halted, self.tripped, self.trips, self.refuses = halted, False, trips, refuses

        def around(self, row, work, refused):
            if self.trips:  # its own breaker, at a row (`StartStop._count`)
                self.halted, self.tripped = power.halted(), True
            if ac.slug_of(row) == self.refuses:
                return refused(f"not started: {self.halted}")
            return work()

        def summary(self) -> str:
            return ""

    monkeypatch.setattr(pc, "start_stop", lambda *a, **k: Window(trips=True))
    assert pc.main(push(roster, out_dir, *START)) == ac.HALTED_EXIT == 3
    assert "start/stop halted" not in capsys.readouterr().out  # said by StartStop as it trips
    monkeypatch.setattr(pc, "start_stop", lambda *a, **k: Window(trips=True, refuses="bob"))
    assert pc.main(push(roster, out_dir, *START)) == ac.HALTED_FAILED_EXIT == 4
    halt = power.halted("harvard-2026")
    monkeypatch.setattr(pc, "start_stop", lambda *a, **k: Window(halt))
    assert pc.main(push(roster, out_dir, *START, "--halted-in", "harvard-2026")) == 0
    assert f"start/stop halted: {halt}; stopped VMs are not started" in capsys.readouterr().out


def test_the_bot_reads_the_halted_exit_of_these_two_scripts():
    """#1031: the runner's `STARTS_HALTED…` and `HALTING` are the scripts' own."""
    from zab.admin import actions

    assert actions.STARTS_HALTED == ac.HALTED_EXIT
    assert actions.STARTS_HALTED_FAILED == ac.HALTED_FAILED_EXIT
    assert set(actions.HALTING) == {
        f"admin/{pathlib.Path(m.__file__).name}" for m in (pc, trust_fix)
    }


def test_halted_in_is_given_to_start_stop_with_the_unread_reasons(monkeypatch):
    """#1031: `admin_common.start_stop` gives `StartStop` idle_stop's `UNREAD` (review of #1026,
    N3: one constant) and `--halted-in`, a workshop's name or refused."""
    import aws_common

    monkeypatch.setattr(aws_common, "Ec2", lambda region, config: fleet())
    opts = SimpleNamespace(start_stopped=8, region="us-west-2", scope_tag=["workshop=w"],
                           known_hosts=KH, halted_in="harvard-2026")  # fmt: skip
    made = ac.start_stop(opts, ROWS, FakeRunner(), log=print)
    assert made.unread_reasons == idle.UNREAD and made.halted == power.halted("harvard-2026")
    assert not made.tripped
    with pytest.raises(ValueError, match="--halted-in takes a workshop's name"):
        ac.start_stop(SimpleNamespace(**{**vars(opts), "halted_in": "a b"}), ROWS, FakeRunner(),
                      log=print)  # fmt: skip


def test_push_config_refuses_the_flag_without_its_scope(roster, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(pc, "subprocess_runner", runner := FakeRunner())
    assert pc.main(push(roster, out_dir, "--start-stopped", "8", "--region", "us-west-2")) == 2
    assert "--start-stopped takes N ≥ 1, with --region and --scope-tag" in capsys.readouterr().err
    assert not runner.calls


# ---------------------------------------------------------------------- trust_fix.py's flag


def test_a_work_that_may_still_run_keeps_its_vm_up():
    """`keep`: trust_fix.py's fix cut short may still run there; a stop would cut apt off."""
    ec2 = fleet()
    made, seen = window(ec2)
    made.around(ROWS[1], lambda: "cut", str, keep=lambda r: "still running" * (r == "cut"))
    assert ec2.changes() == [("start", ["i-bob"])]
    assert made.cells["bob"] == "started, left running: still running"
    assert seen[-1] == "power: bob i-bob: left running: still running"


def trust(tmp_path, monkeypatch, made, *extra: str, report=None, bot: bool = True) -> int:
    """trust_fix.py with `--start-stopped`, `made` its `start_stop`, alice's check failing."""
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email,ip\nalice,a@example.org,198.51.100.7\n")
    report = report or {"apply": True, "steps": {}, "check": [], "ok": False}
    monkeypatch.setattr(trust_fix, "subprocess_runner", TrustRunner(report))
    monkeypatch.setattr(trust_fix, "start_stop", made)
    monkeypatch.setattr(trust_fix.sys, "argv", ["/srv/trust_fix.py"])
    monkeypatch.delenv("SUDO_USER", raising=False)
    flags = ("--start-stopped", "6", "--region", "us-west-2", "--scope-tag", "workshop=w")
    args = ["--roster", str(roster), "--known-hosts", "kh", *["--admin-bot"] * bot, *flags]
    return trust_fix.main([*args, *extra])


@pytest.mark.parametrize("bot", [False, True], ids=["by-hand", "admin-bot"])
def test_the_shell_commands_it_prints_keep_the_start_and_stop(tmp_path, monkeypatch, capsys, bot):
    """Codex round 1 on #1028: a VM this run started is stopped again once its fix is done, so
    every shell command the run prints, a hand run's rerun and the pips' command (a bot run's
    too), carries `--start-stopped`, the region and the scope: without them, it would find
    that VM stopped, `not reached`. The bot's own rerun, `!trust-fix alice`, passes them."""
    left = [{"kind": "pip", "env": "/home/agent/venv", "file": "/home/agent/venv/pip/x.pem"}]
    report = {"apply": True, "steps": {}, "check": [], "ok": False, "left": left, "uv_ok": True}

    class Window:
        off, cells = "", {"alice": "started, stopped again"}
        halted, tripped = "", False

        def around(self, row, work, refused, keep=None):
            return work()

        def summary(self) -> str:
            return ""

    trust(tmp_path, monkeypatch, lambda *a, **k: Window(), "--apply", report=report, bot=bot)
    out = capsys.readouterr().out
    kept = (
        "--known-hosts kh --start-stopped 6 --region us-west-2 --scope-tag workshop=w --only alice"
    )
    assert f"{kept} --apply --upgrade-left-pips" in out, out  # the pips' command
    rerun = out.split("Then rerun them:\n  ", 1)[1].rstrip("\n")
    assert rerun == "!trust-fix alice" if bot else rerun.endswith(f"{kept} --apply"), rerun


def test_trust_fix_dry_run_starts_nothing(tmp_path, monkeypatch, capsys):
    trust(tmp_path, monkeypatch, lambda *a, **k: pytest.fail("EC2 in a dry run"))
    out = capsys.readouterr().out
    assert ac.start_stopped_line(6) in out.splitlines()
    assert "power" not in next(line for line in out.splitlines() if line.startswith("partic"))


def test_trust_fix_apply_wraps_each_vm_and_says_what_became_of_it(tmp_path, monkeypatch, capsys):
    kept: list[object] = []

    class Window:
        off = ""
        halted, tripped = "", False

        def __init__(self) -> None:
            self.cells = {"alice": "started, stopped again"}

        def around(self, row, work, refused, keep=None):
            result = work()
            kept.append(keep(result))
            return result

        def summary(self) -> str:
            return "stopped VMs: 1 started, 1 stopped again"

    assert trust(tmp_path, monkeypatch, lambda *a, **k: Window(), "--apply") == 1
    out = capsys.readouterr().out
    head = next(line for line in out.splitlines() if line.startswith("participant"))
    assert head.split()[-2:] == ["check", "power"] and kept == [""]
    assert "\nstopped VMs: 1 started, 1 stopped again\n" in out
    assert "\nNot done on alice: wait out apt's lock" in out  # it started them: no `!start`


def test_trust_fix_exits_halted_when_its_own_breaker_tripped(tmp_path, monkeypatch, capsys):
    """#1031: as push_config.py's at its apply: `HALTED_EXIT` with every VM passing,
    `HALTED_FAILED_EXIT` with one that didn't."""

    class Window:
        off, cells, halted, tripped = "", {}, power.halted(), True

        def around(self, row, work, refused, keep=None):
            return work()

        def summary(self) -> str:
            return ""

    ok = {"apply": True, "steps": {}, "check": [], "ok": True}
    made = lambda *a, **k: Window()  # noqa: E731
    assert trust(tmp_path, monkeypatch, made, "--apply", report=ok) == ac.HALTED_EXIT
    assert trust(tmp_path, monkeypatch, made, "--apply") == ac.HALTED_FAILED_EXIT


def test_trust_fix_keeps_the_start_hint_when_ec2_couldnt_be_read(tmp_path, monkeypatch, capsys):
    """Review of #1028, N2: a run whose EC2 lookup failed (`off`) started nothing, so a stopped VM
    is `not reached` as before #995, and its rerun line says to `!start` it first."""

    class Off:
        off, cells = "the EC2 lookup failed: RuntimeError: ExpiredToken", {}
        halted, tripped = "", False

        def around(self, row, work, refused, keep=None):
            return work()

        def summary(self) -> str:
            return ""

    trust(tmp_path, monkeypatch, lambda *a, **k: Off(), "--apply")
    out = capsys.readouterr().out
    assert "start/stop off: the EC2 lookup failed: RuntimeError: ExpiredToken" in out
    assert "\nNot done on alice: `!start` a VM not reached; wait out apt's lock" in out


#: What the remote half reports for an apt-get install its timeout killed (`trust_fix_remote`).
APT_KILLED = (
    "FAILED: apt-get install killed after 900 s, maybe mid-install: run sudo dpkg --configure -a"
)


@pytest.mark.parametrize(
    ("steps", "cut", "held", "looked"),
    [
        ({"uv": "0.13.2 -> 0.13.7"}, False, "", False),
        ({}, True, trust_fix.STILL_RUNNING, True),
        ({"ca-certificates": APT_KILLED}, False, trust_fix.STILL_RUNNING, True),
        ({"conda": "FAILED: base: solver"}, False, "", True),
        ({"claude store": f"FAILED: {trust_fix.MANAGED} left as it is: not an object"}, False,
         "", True),
        ({"claude store": "absent -> system"}, False, "", True),
        ({"conda": "FAILED: sage: solver"}, True, trust_fix.STILL_RUNNING, True),
    ],
)  # fmt: skip
def test_only_a_fix_that_may_still_run_keeps_its_vm_up(steps, cut, held, looked):
    """#1031 (review round 3 of #1028: the `absent` caveat, and N1): `held` keeps up only a VM
    whose fix may still be running there (review round 3's Codex P2: apt-get killed
    mid-install). `recovery` prints commands exactly when `looked_into`, the same tests; for a
    VM this run stopped again, which `held` never is, they begin with `!start alice`, and for
    one left up they never do."""
    result = trust_fix.FixResult("alice", "198.51.100.7", report={"steps": steps}, cut=cut)
    assert result.held() == held and result.looked_into() is looked
    args = SimpleNamespace(known_hosts="kh", admin_bot=True)
    up = trust_fix.recovery(result, args)
    assert bool(up) is looked and "!start" not in "\n".join(up)
    first = ["\nalice: stopped after its fix (its `power` cell); start it first:",
             "  !start alice"]  # fmt: skip
    assert trust_fix.recovery(result, args, stopped=True) == ([*first, *up] if looked else [])
    assert (trust_fix.AGAIN, trust_fix.NOT_AGAIN) == (power.STOPPED_AGAIN, power.NOT_STOPPED_AGAIN)


def test_a_vm_whose_conda_failed_is_stopped_and_its_commands_start_it_first(
    tmp_path, monkeypatch, capsys
):
    """#1031: through `trust_fix.main --apply` with a real `power.StartStop`, a stopped VM whose
    conda install failed is stopped again after its fix, as usual, not kept up for the
    commands under the table: they begin with `!start alice`. A conda-forge outage then
    leaves no VM up."""
    ec2 = Ec2()
    ec2.add("i-alice", "198.51.100.7", "stopped", vm("alice"))

    def made(opts, rows, runner, *, log):
        return power.StartStop(ec2, SCOPE, rows, at_once=6, in_use=lambda row: [], log=log,
                               unread_reasons=idle.UNREAD, answers=lambda ip: True,
                               sleep=ec2.time.sleep, clock=ec2.time.monotonic)  # fmt: skip

    steps = {"conda": "FAILED: base: solver", "claude store": "absent -> system"}
    report = {"apply": True, "steps": steps, "check": [], "ok": False}
    trust(tmp_path, monkeypatch, made, "--apply", report=report)
    out = capsys.readouterr().out
    assert ec2.changes() == [("start", ["i-alice"]), ("stop", ["i-alice"])]
    row = next(line for line in out.splitlines() if line.startswith("alice "))
    assert row.rstrip().endswith(power.STOPPED_AGAIN), row
    start = out.index("\nalice: stopped after its fix (its `power` cell); start it first:"
                      "\n  !start alice\n")  # fmt: skip
    assert start < out.index("conda list --revisions") and start < out.index(trust_fix.MANAGED)
    assert out.count("!start alice") == 1


def test_a_vm_whose_stop_failed_gets_the_start_line_too(tmp_path, monkeypatch, capsys):
    """Review round 1, N3: a VM whose stop failed has a cell that says `!stop alice`: the
    commands under the table begin with `!start alice` for it too, harmless on a VM running,
    so that a `!stop` sent for the cell first leaves them a VM to reach."""
    ec2 = Ec2()
    ec2.add("i-alice", "198.51.100.7", "stopped", vm("alice"))

    def stop(ids):
        raise RuntimeError("UnauthorizedOperation: not allowed")

    ec2.stop = stop

    def made(opts, rows, runner, *, log):
        return power.StartStop(ec2, SCOPE, rows, at_once=6, in_use=lambda row: [], log=log,
                               unread_reasons=idle.UNREAD, answers=lambda ip: True,
                               sleep=ec2.time.sleep, clock=ec2.time.monotonic)  # fmt: skip

    report = {"apply": True, "steps": {"conda": "FAILED: base: solver"}, "check": [], "ok": False}
    trust(tmp_path, monkeypatch, made, "--apply", report=report)
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("alice "))
    assert f"{power.NOT_STOPPED_AGAIN} (" in row and row.rstrip().endswith("!stop alice"), row
    assert out.index("\n  !start alice\n") < out.index("conda list --revisions")


def test_a_vm_left_running_gets_no_start_line(tmp_path, monkeypatch, capsys):
    """Only a VM this run stopped again gets `!start`: one used since its start is up."""
    ec2 = Ec2()
    ec2.add("i-alice", "198.51.100.7", "stopped", vm("alice"))

    def made(opts, rows, runner, *, log):
        return power.StartStop(ec2, SCOPE, rows, at_once=6, log=log,
                               in_use=lambda row: ["1 turn(s) running"],
                               unread_reasons=idle.UNREAD, answers=lambda ip: True,
                               sleep=ec2.time.sleep, clock=ec2.time.monotonic)  # fmt: skip

    report = {"apply": True, "steps": {"conda": "FAILED: base: solver"}, "check": [], "ok": False}
    trust(tmp_path, monkeypatch, made, "--apply", report=report)
    out = capsys.readouterr().out
    assert ec2.changes() == [("start", ["i-alice"])]
    assert "conda list --revisions" in out and "!start alice" not in out


def test_a_vm_whose_apt_was_killed_mid_install_is_left_up(tmp_path, monkeypatch, capsys):
    """Through `trust_fix.main --apply` with a real `power.StartStop`: the stopped VM is started
    for its fix, apt-get is killed mid-install, and the VM is left up for `recovery`'s commands,
    its cell and the line under the table saying why."""
    ec2 = Ec2()
    ec2.add("i-alice", "198.51.100.7", "stopped", vm("alice"))

    def made(opts, rows, runner, *, log):
        return power.StartStop(ec2, SCOPE, rows, at_once=6, in_use=lambda row: [], log=log,
                               unread_reasons=idle.UNREAD, answers=lambda ip: True,
                               sleep=ec2.time.sleep, clock=ec2.time.monotonic)  # fmt: skip

    report = {"apply": True, "steps": {"ca-certificates": APT_KILLED}, "check": [], "ok": False}
    trust(tmp_path, monkeypatch, made, "--apply", report=report)
    out = capsys.readouterr().out
    assert ec2.changes() == [("start", ["i-alice"])]  # never stopped
    left = f"started, left running: {trust_fix.STILL_RUNNING}"
    row = next(line for line in out.splitlines() if line.startswith("alice "))
    assert row.rstrip().endswith(left), row
    assert "stopped VMs: 1 started, 0 stopped again; left running: alice" in out
    assert "sudo dpkg --configure -a" in out  # the command it is kept up for
