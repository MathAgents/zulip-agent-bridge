"""What the bridge logs about `claude auth status` and the CLI's refresh lock (#483).

After a boot on the test VM a new topic's first Claude answer lost the OAuth refresh race twice
(71.7 s), and the journal couldn't say why: it had no line for `auth status`'s start, end or
duration, nor for whose lock the turn met. Now each `auth status` logs what asked for it, its
pid, how it ended and after how long, and the refresh lock it leaves; a turn logs its wait for
it; and a lost race logs the lock's age and the pid its owner record names, alive or gone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest

from backends.claude_backend_helpers import drain, make_backend
from zab.backends import claude as cb
from zab.core.commands import status_login_hint
from zab.core.reason import asked_by, doctor_reason
from zab.core.types import SessionSpec, TurnCompleted, TurnStatus, UserInput
from zab.core.vm import sign_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="reads /proc"),
]


def lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "zab.backends.claude"]


def lock(config_dir, *, age: float = 0.0, owner: int | None = None, taken: float = 0.0) -> None:
    """The CLI's refresh lock, as proper-lockfile makes it (a directory), touched `age` s ago,
    with the owner record 2.1.283 writes, naming `owner` and a lock taken `taken` s ago, unless
    `owner` is None."""
    path = config_dir / cb.CLI_REFRESH_LOCK
    path.mkdir(exist_ok=True)
    os.utime(path, (time.time() - age, time.time() - age))
    if owner is not None:
        record = {"pid": owner, "procStart": "1", "pidDomain": "linux:x:y", "pidSpace": "h#y",
                  "lockBirthtimeMs": (time.time() - taken) * 1000}  # fmt: skip
        (config_dir / cb.CLI_REFRESH_LOCK_OWNER).write_text(json.dumps(record))


def holder(tmp_path, *args: str) -> subprocess.Popen:
    """A process that /proc shows as `claude <args>` (a script named `claude`). The script is
    written once: /proc shows the command line as soon as the interpreter starts, before it has
    read the script, so rewriting it for the next holder could hand a running one an empty file,
    and it would exit at once (a zombie where the test expects it alive)."""
    script = tmp_path / "claude"
    if not script.exists():
        script.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, str(script), *args])  # noqa: S603
    deadline = time.monotonic() + 10
    while "claude" not in cb.process_state(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.01)  # until the interpreter has started
    return proc


def ends_aged(line: str, template: str, since: float) -> None:
    """Assert that `line` ends with `template`, in which each `<n>` is an age of n s that the
    line may show grown by up to the time since `since` (`time.monotonic()`, taken before the
    lock or process it describes was made): the line comes that much later, more than half a
    second on a slow runner (#590: CI read `started 1 s ago` for 0; a loaded run here shifts
    every age by 1)."""
    ages = [int(n) for n in re.findall(r"<(\d+)>", template)]
    pattern = r"(\d+)".join(map(re.escape, re.split(r"<\d+>", template)))
    match = re.search(pattern + "$", line)
    assert match, (line, template)
    late = time.monotonic() - since + 0.51  # each age shown rounded; /proc's in hundredths
    shown = [int(n) for n in match.groups()]
    assert all(a <= s <= a + late for a, s in zip(ages, shown, strict=True)), (line, late)


def gone_pid() -> int:
    proc = subprocess.Popen(["true"])  # noqa: S607
    proc.wait()
    return proc.pid


async def up(home) -> tuple[cb.ClaudeBackend, str]:
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()  # BackendStatus
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    return backend, sid


def last_pid(backend: cb.ClaudeBackend) -> int | None:
    state = backend._auth.get(backend.config.config_dir("test"))
    return state.ran.pid if state and state.ran else None


async def test_auth_status_logs_why_its_pid_how_it_ended_and_the_lock(home, caplog, monkeypatch):
    caplog.set_level(logging.INFO, "zab.backends.claude")
    backend = make_backend(home)
    with asked_by("!vm"):
        assert (await backend.doctor())[-1].ok
    pid = last_pid(backend)
    start, end = [line for line in lines(caplog) if "`auth status` for account test" in line]
    assert start == "claude: `auth status` for account test starts, for !vm"
    assert end.startswith(f"claude: `auth status` for account test ended: pid {pid}, exit 0, ")
    assert end.endswith(" s; no refresh lock")
    # one left behind by the last `auth status`, gone now: the next one's line says so
    since = time.monotonic()
    lock(home["account"], age=2, owner=pid, taken=3)
    caplog.clear()
    await backend.doctor()
    assert lines(caplog)[0].endswith(" starts, for a doctor check")  # nothing named it
    ends_aged(
        lines(caplog)[1],
        f"; the refresh lock was touched <2> s ago, by pid {pid} (gone), in a record for a lock "
        "taken <3> s ago",
        since,
    )
    # and one that this `auth status` leaves is named as its own
    real = cb._run_capture

    async def leaves_the_lock(argv, *, ran=None, **kwargs):
        out = await real(argv, ran=ran, **kwargs)
        if ran is not None:  # `auth status`, not `--version`
            lock(home["account"], age=1, owner=ran.pid, taken=1)
        return out

    monkeypatch.setattr(cb, "_run_capture", leaves_the_lock)
    caplog.clear()
    since = time.monotonic()
    await backend.doctor()
    ends_aged(
        lines(caplog)[1],
        f"; the refresh lock was touched <1> s ago, by pid {last_pid(backend)} "
        "(gone, this `auth status`), in a record for a lock taken <1> s ago",
        since,
    )


async def test_the_core_names_its_doctor_calls(home, caplog):
    caplog.set_level(logging.INFO, "zab.backends.claude")
    backend = make_backend(home)
    await status_login_hint(backend)
    assert await sign_in(backend) == "signed in"
    starts = [line for line in lines(caplog) if " starts, for " in line]
    assert [s.rsplit(", for ", 1)[1] for s in starts] == ["!status", "!vm"]
    assert doctor_reason.get() == "a doctor check"  # reset after each


async def test_a_turns_wait_for_auth_status_is_logged_at_both_ends(home, caplog, monkeypatch):
    caplog.set_level(logging.INFO, "zab.backends.claude")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_AUTH_DELAY", "1.0")
    backend, sid = await up(home)
    doctor = asyncio.create_task(backend.doctor())
    async with asyncio.timeout(10):
        while True:
            if last_pid(backend) is not None:
                break
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    done = (await drain(backend))[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    waits = [line for line in lines(caplog) if "for the account's `auth status`" in line]
    assert len(waits) == 1 and f"(pid {last_pid(backend)}, " in waits[0]
    assert waits[0].endswith(" s in)")
    waited = [line for line in lines(caplog) if " for `auth status`, which ended" in line]
    assert len(waited) == 1
    took = float(waited[0].split(" waited ", 1)[1].split(" s ", 1)[0])
    assert 0.3 < took < 2.0
    await doctor
    await backend.close()


async def test_a_turn_starting_with_the_probe_names_the_new_pid(home, caplog, monkeypatch):
    """Review round 1 (nit 7): a turn that starts between `doctor()` making the probe and the
    probe's first step names that `auth status`, never the previous one."""
    caplog.set_level(logging.INFO, "zab.backends.claude")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_AUTH_DELAY", "0.5")
    backend, sid = await up(home)
    await backend.doctor()
    old = backend._auth[backend.config.config_dir("test")].ran
    doctor = asyncio.create_task(backend.doctor())
    async with asyncio.timeout(10):
        while True:
            state = backend._auth[backend.config.config_dir("test")]
            if state.probe is not None and not state.probe.done():
                break
            await asyncio.sleep(0)
    assert state.ran is not old  # already this probe's, before its process exists
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend)
    waits = [line for line in lines(caplog) if "for the account's `auth status`" in line]
    assert waits and f"(pid {old.pid}, " not in waits[0]
    await doctor
    await backend.close()


async def test_a_lost_race_says_whose_lock_it_met(home, caplog, monkeypatch, tmp_path):
    caplog.set_level(logging.INFO, "zab.backends.claude")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_REFRESH_RACE", "1")
    monkeypatch.setattr(cb, "refresh_retry_delay", lambda attempt, since: 0.0)
    since = time.monotonic()
    other = holder(tmp_path, "auth", "status")  # alive, and not this bridge's
    try:
        lock(home["account"], age=9, owner=other.pid, taken=20)
        backend, sid = await up(home)
        await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
        done = (await drain(backend))[-1]
        assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    finally:
        other.kill()
        other.wait()
    lost = [line for line in lines(caplog) if "lost the OAuth refresh race" in line]
    assert len(lost) == 1
    ends_aged(
        lost[0],
        "(try 1 of 3); again in 0 s; the refresh lock was touched <9> s ago, by pid "
        f"{other.pid} (alive: claude auth status, started <0> s ago), in a record for a lock "
        "taken <20> s ago",
        since,
    )
    await backend.close()


@pytest.mark.parametrize("how", ["interrupt", "close"])
async def test_a_stop_while_the_lock_is_read_ends_the_retry_wait(home, monkeypatch, how):
    """Review round 1: the lost race's lock read is an await; a stop or shutdown during it
    still wakes the retry wait at once, not after the delay (46 s in the issue's run, more than
    systemd's `TimeoutStopSec=30`)."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_REFRESH_RACE", "1")
    monkeypatch.setattr(cb, "refresh_retry_delay", lambda attempt, since: 30.0)
    reading, slow = threading.Event(), cb.refresh_lock_state

    def read_slowly(config_dir, own=None):
        reading.set()
        time.sleep(0.3)
        return slow(config_dir, own)

    monkeypatch.setattr(cb, "refresh_lock_state", read_slowly)
    backend, sid = await up(home)
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    async with asyncio.timeout(10):
        while True:
            if reading.is_set():
                break
            await asyncio.sleep(0.01)
    started = time.monotonic()
    if how == "interrupt":
        await backend.interrupt(sid)
        done = (await asyncio.wait_for(drain(backend), 10))[-1]
        assert isinstance(done, TurnCompleted) and done.status is TurnStatus.INTERRUPTED
    else:
        await asyncio.wait_for(backend.close(), 10)
    assert time.monotonic() - started < 3.0  # not the 30 s delay
    if how == "interrupt":
        await backend.close()


def test_the_lock_state_reads_only_the_locks_and_the_owner_record(tmp_path):
    config = tmp_path / "account"
    config.mkdir()
    assert cb.refresh_lock_state(str(config)) == "no refresh lock"
    lock(config, age=12)
    assert cb.refresh_lock_state(str(config)) == (
        "the refresh lock was touched 12 s ago, without an owner record"
    )
    gone = gone_pid()
    lock(config, age=12, owner=gone, taken=300)  # an earlier holder's record: far older
    assert cb.refresh_lock_state(str(config), {gone: "this `auth status`"}) == (
        f"the refresh lock was touched 12 s ago, by pid {gone} (gone, this `auth status`), in "
        "a record for a lock taken 300 s ago"
    )
    lock(config, age=12, owner=os.getpid())
    assert cb.refresh_lock_state(str(config)).startswith(
        f"the refresh lock was touched 12 s ago, by pid {os.getpid()} (alive: "
    )
    owner = config / cb.CLI_REFRESH_LOCK_OWNER
    deep = "[" * 2048 + "]" * 2048  # 4 KiB: RecursionError on Python 3.11 (review round 1)
    for junk in ("[1, 2]", '{"pid": true}', '{"pid": "12"}', '{"pid": 1}', "{", "", deep):
        owner.write_text(junk)
        assert cb.refresh_lock_state(str(config)).endswith(", without an owner record")
    owner.write_text(json.dumps({"pid": os.getpid(), "lockBirthtimeMs": "soon"}))
    assert "in a record" not in cb.refresh_lock_state(str(config))  # no age from junk
    owner.unlink()
    os.mkfifo(owner)  # never waited on
    assert cb.refresh_lock_state(str(config)).endswith(", without an owner record")
    # the legacy lock, `<real path of the config directory>.lock`, the CLI takes after it
    legacy = tmp_path / "account.lock"
    legacy.mkdir()
    os.utime(legacy, (time.time() - 5, time.time() - 5))
    assert cb.refresh_lock_state(str(config)).endswith(
        ", without an owner record; the legacy lock was touched 5 s ago"
    )
    shutil.rmtree(config / cb.CLI_REFRESH_LOCK)
    assert cb.refresh_lock_state(str(config)) == (
        "no refresh lock; the legacy lock was touched 5 s ago"
    )
    # a link in the lock's place is reported by its own times, never its target's
    (config / cb.CLI_REFRESH_LOCK).symlink_to(tmp_path)
    os.utime(
        config / cb.CLI_REFRESH_LOCK, (time.time() - 40, time.time() - 40), follow_symlinks=False
    )
    assert cb.refresh_lock_state(str(config)).startswith("the refresh lock was touched 40 s ago")


def test_the_lock_state_never_raises(tmp_path, monkeypatch):
    lock(tmp_path, age=1, owner=os.getpid())

    def boom(pid):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cb, "process_state", boom)
    assert cb.refresh_lock_state(str(tmp_path)) == (
        "the refresh lock's state is unknown (RecursionError)"
    )


def test_a_process_is_named_by_its_command_never_its_arguments(tmp_path):
    turn = holder(tmp_path, "-p", "--append-system-prompt", "SECRET")
    status = holder(tmp_path, "auth", "status")
    other = holder(tmp_path, "mcp", "list")
    # a program whose name only starts like an interpreter's is named for itself (nit 6)
    # Python under that name, not coreutils' sleep: a multi-call coreutils (Ubuntu 26.04's
    # Rust one) refuses to run under a name that isn't one of its utilities'
    shsleep = tmp_path / "shsleep"
    shsleep.symlink_to(sys.executable)
    lookalike = subprocess.Popen([str(shsleep), "-c", "import time; time.sleep(60)"])  # noqa: S603
    deadline = time.monotonic() + 10  # Popen returns mid-exec, with /proc's cmdline still empty
    while "shsleep" not in cb.process_state(lookalike.pid) and time.monotonic() < deadline:
        time.sleep(0.01)  # until its exec has set it; a misnamed one still fails below
    zombie = subprocess.Popen(["true"])  # noqa: S607
    try:
        deadline = time.monotonic() + 10
        while cb.process_state(zombie.pid) != "a zombie" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cb.process_state(zombie.pid) == "a zombie"  # exited, not reaped yet
        named = (
            (turn, "claude -p"),
            (status, "claude auth status"),
            (other, "claude"),
            (lookalike, "shsleep"),
        )
        for proc, command in named:
            described = cb.process_state(proc.pid)
            assert described.startswith(f"alive: {command}, started ") and "SECRET" not in described
    finally:
        for proc in (turn, status, other, lookalike, zombie):
            proc.kill()
            proc.wait()
    assert cb.process_state(turn.pid) == "gone"


async def test_a_stopped_capture_says_by_which_signal(tmp_path):
    polite = tmp_path / "polite"
    polite.write_text("#!/bin/sh\ntrap 'exit 0' TERM\nsleep 30 &\nwait\n")
    stubborn = tmp_path / "stubborn"
    stubborn.write_text("#!/bin/sh\ntrap '' TERM\nsleep 30\n")
    for script, signal_name in ((polite, "SIGTERM"), (stubborn, "SIGKILL")):
        script.chmod(0o755)
        ran = cb._Ran()
        with pytest.raises(TimeoutError):
            await cb._run_capture([str(script)], timeout_s=0.3, kill_grace_s=0.5, ran=ran)
        assert ran.pid is not None and ran.stopped == signal_name
        assert ran.summary().startswith(f"pid {ran.pid}, stopped by {signal_name}, after ")
    ran = cb._Ran()
    assert await cb._run_capture(["sh", "-c", "exit 3"], timeout_s=5, check=False, ran=ran) == ""
    assert ran.summary().startswith(f"pid {ran.pid}, exit 3, after ")
    assert cb._Ran().summary().startswith("pid None, not started, after ")


async def test_logins_auth_status_is_logged_and_counted_as_the_bridges(home, caplog):
    caplog.set_level(logging.INFO, "zab.backends.claude")
    backend = make_backend(home)
    state = backend._auth.setdefault(backend.config.config_dir("test"), cb._AuthState())
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home["account"])}
    out = await cb._capture_auth_status(
        [backend.config.binary, "auth", "status"], timeout_s=30, env=env, account="test",
        state=state,
    )  # fmt: skip
    assert json.loads(out)["loggedIn"]
    start, end = [line for line in lines(caplog) if "!login" in line]
    assert start == "claude: `auth status` for account test starts, for !login"
    assert end.startswith("claude: `auth status` for account test, for !login, ended: pid ")
    assert ", exit 0, " in end
    assert state.login_ran is not None and state.login_ran.pid is not None
    own = backend._own_processes(state)
    assert own[state.login_ran.pid] == "this bridge's `auth status` for !login"
