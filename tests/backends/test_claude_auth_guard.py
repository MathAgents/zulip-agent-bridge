"""The bridge's own `claude auth status` and a starting turn never refresh at once (#307, #450).

On the test VM a new topic's first Claude answer took 74 s: `!status` ran `claude auth status`,
its 2 s budget ran out while that process was refreshing the OAuth token, the SIGKILL left the
CLI's refresh lock behind, and the turn lost the refresh race until the lock went stale a
minute later. Now `auth status` always runs to its end, the doctor reports the last answer
while a turn of the account is starting, and a turn waits for a running `auth status`.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time

import pytest

from backends.claude_backend_helpers import FIXTURES, calls, drain, make_backend
from zab.backends import claude as cb
from zab.core.types import (
    ItemStarted,
    SessionSpec,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
    UserInput,
)
from zab.core.vm import sign_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a shebang"),
]


@pytest.fixture
def auth_log(tmp_path, monkeypatch):
    """`events()`: the fake `auth status` runs' `start` and `end` lines, in order."""
    path = tmp_path / "auth.jsonl"
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_AUTH_LOG", str(path))

    def events() -> list[str]:
        if not path.exists():
            return []
        return [json.loads(line)["auth_status"] for line in path.read_text().splitlines()]

    return events


async def until(predicate) -> None:
    async with asyncio.timeout(10):
        while True:
            if predicate():
                return
            await asyncio.sleep(0.01)


def hanging_fixture(tmp_path, monkeypatch, name: str, *, upto: str | None) -> None:
    """Replay `name` up to (not including) its `result`, then wait for a signal; with `upto`,
    keep only the records up to the first `system` object of that subtype (and the result)."""
    records = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    if upto is not None:
        cut = next(
            i
            for i, r in enumerate(records)
            if r["kind"] == "stdout" and (r["data"] or {}).get("subtype") == upto
        )
        result = [r for r in records if r["kind"] == "stdout" and r["data"]["type"] == "result"]
        records = [*records[: cut + 1], *result]
    path = tmp_path / f"{name}-hang.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(path))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")


async def up(home) -> tuple[cb.ClaudeBackend, str]:
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()  # BackendStatus
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    return backend, sid


def turn_calls(home) -> list[dict]:
    """The fake's `claude -p` runs (only they write the call log)."""
    return calls(home) if home["log"].exists() else []


def state(backend: cb.ClaudeBackend) -> cb._AuthState:
    return backend._auth[backend.config.config_dir("test")]


async def test_a_caller_that_stops_waiting_never_kills_auth_status(home, auth_log, monkeypatch):
    """`!status` and `!vm` give the doctor 2 s; `auth status` runs to its end anyway."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_AUTH_DELAY", "1.0")
    backend = make_backend(home)
    doctor = asyncio.create_task(backend.doctor())
    await until(lambda: auth_log() == ["start"])
    doctor.cancel()  # what `asyncio.timeout(2)` in core/commands.py and core/vm.py does
    with pytest.raises(asyncio.CancelledError):
        await doctor
    probe = state(backend).probe
    assert probe is not None and not probe.done()
    check = await asyncio.wait_for(asyncio.shield(probe), 10)
    assert auth_log() == ["start", "end"]  # not killed mid-refresh
    assert check.name == "claude account test" and check.ok
    assert state(backend).check == check  # the last answer, for a doctor during a turn's start


async def test_a_turn_waits_for_a_running_auth_status(home, auth_log, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_AUTH_DELAY", "1.0")
    backend, sid = await up(home)
    doctor = asyncio.create_task(backend.doctor())
    await until(lambda: auth_log() == ["start"])
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    done = (await drain(backend))[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    turn = turn_calls(home)
    assert len(turn) == 1 and turn[0]["auth_running"] == 0  # started after `auth status` ended
    assert (await doctor)[-1].ok
    await backend.close()


async def test_a_stop_while_waiting_for_auth_status_never_starts_the_process(
    home, auth_log, monkeypatch
):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_AUTH_DELAY", "5.0")
    monkeypatch.setattr(cb, "AUTH_STATUS_CLOSE_WAIT_S", 0.2)
    backend, sid = await up(home)
    doctor = asyncio.create_task(backend.doctor())
    await until(lambda: auth_log() == ["start"])
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await until(lambda: backend._sessions[sid].retry_wake is not None)
    await backend.interrupt(sid)
    async with asyncio.timeout(3):  # well before `auth status` ends
        done = (await drain(backend))[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.INTERRUPTED
    assert turn_calls(home) == []
    assert state(backend).starting == 0
    doctor.cancel()
    started = time.monotonic()
    await backend.close()  # ends the running `auth status`: SIGTERM after the short wait
    assert time.monotonic() - started < 3.0  # not the 5 s `auth status` would take
    assert state(backend).probe is not None and state(backend).probe.done()
    assert auth_log() == ["start"]  # stopped, not run to its end
    after = {c.name: c for c in await backend.doctor()}  # closed: no new `auth status`
    assert auth_log() == ["start"] and "claude auth test" in after


async def test_close_ends_every_accounts_auth_status_at_once(home, monkeypatch):
    """Codex round 3 on #451: three accounts' `auth status`, each needing its grace, end
    together, not one after another (3 x 10 s would pass systemd's `TimeoutStopSec=30`)."""
    monkeypatch.setattr(cb, "AUTH_STATUS_CLOSE_WAIT_S", 0.05)
    backend = make_backend(home)

    async def probe() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # SIGTERM's grace: the CLI's own shutdown
            raise

    for n in range(3):
        backend._auth[f"/accounts/{n}"] = cb._AuthState(probe=asyncio.create_task(probe()))
    await asyncio.sleep(0)
    started = time.monotonic()
    await backend._end_auth_status()
    assert time.monotonic() - started < 1.2  # one grace, not three (1.5 s)
    assert all(a.probe is not None and a.probe.done() for a in backend._auth.values())


async def test_the_doctor_reports_the_last_answer_while_a_turn_starts(
    home, auth_log, monkeypatch, tmp_path
):
    backend, sid = await up(home)
    first = {c.name: c for c in await backend.doctor()}
    assert first["claude account test"].ok and auth_log() == ["start", "end"]
    hanging_fixture(tmp_path, monkeypatch, "simple", upto="init")  # no model answer yet
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend, until=TurnStarted)
    assert state(backend).starting == 1
    during = {c.name: c for c in await backend.doctor()}
    assert auth_log() == ["start", "end"]  # no second `auth status`
    check = during["claude account test"]
    assert check.ok and check.detail.startswith(first["claude account test"].detail)
    assert "(checked " in check.detail and check.detail.endswith(" ago)")
    assert await sign_in(backend) == "signed in"  # `!vm` keeps its answer
    await backend.interrupt(sid)
    await drain(backend)
    assert state(backend).starting == 0
    await backend.doctor()
    assert auth_log() == ["start", "end", "start", "end"]  # checked again once it is over
    await backend.close()


async def test_with_no_last_answer_the_doctor_says_it_did_not_check(
    home, auth_log, monkeypatch, tmp_path
):
    backend, sid = await up(home)
    hanging_fixture(tmp_path, monkeypatch, "simple", upto="init")
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend, until=TurnStarted)
    checks = {c.name: c for c in await backend.doctor()}
    assert auth_log() == []
    assert "claude account test" not in checks  # no sign-in answer, so no "not signed in"
    assert checks["claude auth test"].ok and checks["claude auth test"].warning
    assert "not checked" in checks["claude auth test"].detail
    assert await sign_in(backend) == "sign-in status unavailable"
    await backend.interrupt(sid)
    await drain(backend)
    await backend.close()


async def test_a_turn_stops_counting_as_starting_once_the_model_answers(
    home, auth_log, monkeypatch, tmp_path
):
    backend, sid = await up(home)
    hanging_fixture(tmp_path, monkeypatch, "tool_use", upto=None)  # everything but the result
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend, until=ItemStarted)
    assert state(backend).starting == 0
    await backend.doctor()
    assert auth_log() == ["start", "end"]  # the turn is running, past its sign-in
    await backend.interrupt(sid)
    await drain(backend)
    await backend.close()


async def test_a_lost_race_keeps_the_turn_starting_until_its_retry(home, auth_log, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_REFRESH_RACE", "1")
    monkeypatch.setattr(cb, "refresh_retry_delay", lambda attempt, since: 60.0)
    backend, sid = await up(home)
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await until(lambda: backend._sessions[sid].retry_wake is not None)
    assert state(backend).starting == 1  # the API-error message is no model answer
    checks = {c.name: c for c in await backend.doctor()}
    assert auth_log() == [] and "not checked" in checks["claude auth test"].detail
    await backend.interrupt(sid)
    await drain(backend)
    assert state(backend).starting == 0
    await backend.close()


async def test_auth_status_on_its_time_limit_gets_sigterm_first(tmp_path):
    """SIGTERM lets the CLI remove its refresh lock; SIGKILL follows only after the grace."""
    marker = tmp_path / "term"
    script = tmp_path / "slow"
    script.write_text(f"#!/bin/sh\ntrap 'echo term > {marker}; exit 0' TERM\nsleep 30 &\nwait\n")
    script.chmod(0o755)
    with pytest.raises(TimeoutError):
        await cb._run_capture([str(script)], timeout_s=0.5, kill_grace_s=5.0)
    assert marker.read_text().strip() == "term"


async def test_a_cancel_during_the_grace_does_not_cut_it_short(tmp_path):
    """Codex on #451: the 30 s limit has sent SIGTERM and `close()` then cancels the capture.
    The process still gets its grace to exit by itself (here 0.5 s of shutdown work), and the
    cancel is passed on afterwards."""
    term, done = tmp_path / "term", tmp_path / "done"
    script = tmp_path / "slow"
    script.write_text(
        f"#!/bin/sh\ntrap 'echo term > {term}; sleep 0.5; echo done > {done}; exit 0' TERM\n"
        "sleep 30 &\nwait\n"
    )
    script.chmod(0o755)
    task = asyncio.create_task(cb._run_capture([str(script)], timeout_s=0.3, kill_grace_s=5.0))
    await until(term.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.read_text().strip() == "done"  # its shutdown ran to the end, no SIGKILL


CANCEL_ALL = """
import asyncio, pathlib, sys
from zab.backends import claude as cb

async def main(script, term):
    task = asyncio.create_task(cb._run_capture([script], timeout_s=0.3, kill_grace_s=5.0))
    while not pathlib.Path(term).exists():
        await asyncio.sleep(0.01)
    for other in asyncio.all_tasks():  # as a loop's teardown does: the stop task too
        if other is not asyncio.current_task():
            other.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("cancelled")

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def test_cancelling_the_stop_itself_ends_the_capture(tmp_path):
    """Codex round 2 on #451: when the stop task is cancelled too, the capture passes the
    cancellation on instead of spinning on it (in a child process, as a spin never yields)."""
    term = tmp_path / "term"
    script = tmp_path / "slow"
    script.write_text(
        f"#!/bin/sh\ntrap 'echo term > {term}; sleep 2; exit 0' TERM\nsleep 30 &\nwait\n"
    )
    script.chmod(0o755)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CANCEL_ALL, str(script), str(term), stdout=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        pytest.fail("the capture spun on the cancelled stop")
    assert proc.returncode == 0 and out.decode().strip() == "cancelled"


async def test_a_cancelled_capture_with_a_grace_sends_sigterm_then_sigkill(monkeypatch):
    from unittest.mock import AsyncMock

    proc = AsyncMock(pid=12345)
    communicating = asyncio.Event()

    async def communicate():
        communicating.set()
        await asyncio.Event().wait()

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    killed = []
    monkeypatch.setattr(cb.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    task = asyncio.create_task(
        cb._run_capture(["claude", "auth", "status"], timeout_s=30, kill_grace_s=1.0)
    )
    await communicating.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]


async def test_the_doctor_gives_auth_status_its_sigterm_grace(home, monkeypatch):
    seen: list[dict] = []
    real = cb._run_capture

    async def run(argv, **kwargs):
        if argv[1:3] == ["auth", "status"]:
            seen.append(kwargs)
        return await real(argv, **kwargs)

    monkeypatch.setattr(cb, "_run_capture", run)
    await make_backend(home).doctor()
    assert len(seen) == 1
    assert seen[0]["timeout_s"] == cb.AUTH_STATUS_TIMEOUT_S
    assert seen[0]["kill_grace_s"] == cb.AUTH_STATUS_KILL_GRACE_S > 0


def test_the_grace_outlasts_the_clis_own_shutdown():
    """On SIGTERM the CLI exits within its failsafe: at least 5 s, 1.5 s of session-end hooks
    plus 5 s by default, and 2 s more while it holds a refresh (2.1.283, DECISIONS). The stop
    stays well inside systemd's `TimeoutStopSec=30`."""
    assert cb.AUTH_STATUS_KILL_GRACE_S >= max(5.0, 1.5 + 5.0) + 2.0
    assert cb.AUTH_STATUS_CLOSE_WAIT_S + cb.AUTH_STATUS_KILL_GRACE_S <= 15.0


async def test_logins_auth_status_gets_the_grace_too(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGGED_IN", "0")
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_LOGIN", raising=False)
    seen: list[dict] = []
    real = cb._run_capture

    async def run(argv, **kwargs):
        if argv[1:3] == ["auth", "status"]:
            seen.append(kwargs)
        return await real(argv, **kwargs)

    monkeypatch.setattr(cb, "_run_capture", run)
    replies = ["fake-code#fake-state"]

    async def send(_text: str) -> None:
        return None

    async def receive(_timeout_s: float) -> str | None:
        return replies.pop(0) if replies else None

    backend = make_backend(home)
    assert await asyncio.wait_for(backend.login(None, send, receive), 30) is True
    assert seen and all(k.get("kill_grace_s") == cb.AUTH_STATUS_KILL_GRACE_S for k in seen)


async def test_signed_in_reads_the_last_answer_and_never_asks(home, auth_log, monkeypatch):
    """#687: a new topic's routing asks `signed_in`, which reads the last `auth status` answer
    (the doctor's) and never runs one. A "not signed in" is believed for
    `SIGNED_OUT_TRUSTED_S`; older, a failed check, or none since the start: unknown."""
    from zab.core.types import Check

    backend = make_backend(home)
    assert await backend.signed_in() is None
    assert auth_log() == []  # nothing ran
    s = backend._auth.setdefault(backend.config.config_dir("test"), cb._AuthState())
    s.check, s.checked_at = Check(name="claude account test", ok=True, detail=""), time.monotonic()
    assert await backend.signed_in() is True
    s.check = Check(name="claude account test", ok=False, detail=cb.NOT_SIGNED_IN)
    assert await backend.signed_in() is False
    s.checked_at -= cb.SIGNED_OUT_TRUSTED_S + 1
    assert await backend.signed_in() is None
    s.check, s.checked_at = (
        Check(name="claude auth test", ok=False, detail="failed"),
        time.monotonic(),
    )
    assert await backend.signed_in() is None
    assert auth_log() == []
