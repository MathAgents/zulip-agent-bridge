"""`ClaudeTurn` and `run_turn` against small stand-in processes (SPEC §9.1, §9.6)."""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from zab.backends.claude import ClaudeTurn, run_turn

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")

INIT = '{"type":"system","subtype":"init","session_id":"s"}'

# A parent that exits on SIGINT, with a child that ignores SIGINT and SIGTERM and keeps the
# inherited stdout open: the shape of a tool process that must not outlive a stop (§9.6). The
# child writes the first line once it ignores both, so a stop after that line finds the group
# ready, however slowly the child started (#548).
STUBBORN_TOOL = (
    "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
    f"signal.signal(signal.SIGTERM, signal.SIG_IGN); print({INIT!r}, flush=True); time.sleep(60)"
)
STUBBORN_CHILD = f"""
import signal, subprocess, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
child = subprocess.Popen([sys.executable, "-c", {STUBBORN_TOOL!r}])
time.sleep(60)
"""

SLEEPER = f"""
import sys, time
sys.stdin.read()
print('{INIT}', flush=True)
time.sleep(60)
"""


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


async def first_object(turn: ClaudeTurn) -> dict:
    async for obj in turn.objects():
        return obj
    raise AssertionError("no stdout object")


async def test_interrupt_escalates_until_the_group_is_gone(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text(STUBBORN_CHILD)
    turn = ClaudeTurn([sys.executable, str(script)], env=os.environ, cwd=str(tmp_path), prompt="")
    await turn.start()
    assert await first_object(turn) == {"type": "system", "subtype": "init", "session_id": "s"}
    assert turn.pgid is not None and turn.pgid != os.getpgid(0)
    t0 = time.monotonic()
    await turn.interrupt(grace_s=0.3)
    elapsed = time.monotonic() - t0
    assert not group_alive(turn.pgid), "a tool process survived the stop"
    assert elapsed >= 0.6, "SIGINT killed the parent only; escalation must continue to SIGKILL"
    assert await asyncio.wait_for(turn.wait(), 5) == 0


async def test_run_turn_cancellation_kills_the_group(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    holder: list[ClaudeTurn] = []
    seen = asyncio.Event()

    def on_object(_obj: dict) -> None:
        seen.set()

    task = asyncio.create_task(
        run_turn(
            [sys.executable, str(script)],
            env=os.environ,
            cwd=str(tmp_path),
            prompt="x",
            on_object=on_object,
            turn_holder=holder,
        )
    )
    await asyncio.wait_for(seen.wait(), 10)
    pgid = holder[0].pgid
    assert pgid is not None and group_alive(pgid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not group_alive(pgid), "cancelling run_turn must not leave the agent running"
    assert holder[0].proc is not None and holder[0].proc.returncode is not None


async def test_cancel_during_stdin_write_kills_the_group(tmp_path):
    """A prompt larger than the pipe buffer blocks `start()` in `drain()` when the child does
    not read stdin; cancelling there must still kill the process (codex round 2 on PR #15).
    The child records its pid in a file, so the check is portable (no /proc)."""
    script = tmp_path / "deaf.py"
    pid_file = tmp_path / "deaf.pid"
    script.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    holder: list[ClaudeTurn] = []
    task = asyncio.create_task(
        run_turn(
            [sys.executable, str(script)],
            env=os.environ,
            cwd=str(tmp_path),
            prompt="x" * (4 * 1024 * 1024),
            turn_holder=holder,
        )
    )
    for _ in range(100):  # wait for the child to be up while start() is blocked in drain()
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    # start() is still blocked in the stdin write, but the turn is already registered (the
    # spawn-time callback of codex round 4 on PR #24), so a stop could signal it meanwhile.
    assert not task.done() and holder and holder[0].pgid is not None
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):  # the loop's child watcher reaps it; then the pid is gone
        await asyncio.sleep(0.05)
        if not _alive(pid):
            break
    assert not _alive(pid), "child survived cancellation"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_run_turn_callback_exception_kills_the_group(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    holder: list[ClaudeTurn] = []

    def boom(_obj: dict) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError):
        await run_turn(
            [sys.executable, str(script)],
            env=os.environ,
            cwd=str(tmp_path),
            prompt="x",
            on_object=boom,
            turn_holder=holder,
        )
    assert holder[0].pgid is not None and not group_alive(holder[0].pgid)


async def test_non_json_stdout_is_not_logged_verbatim(tmp_path, caplog):
    """Agent text stays out of normal logs (SPEC §14; codex round 6 on PR #15)."""
    script = tmp_path / "leak.py"
    decoy = "sk-ant-" + "x" * 32
    script.write_text(f"import sys\nsys.stdin.read()\nprint('diagnostic {decoy}')\n")
    with caplog.at_level("DEBUG", logger="zab.backends.claude"):
        code, _tail = await run_turn(
            [sys.executable, str(script)], env=os.environ, cwd=str(tmp_path), prompt="x"
        )
    assert code == 0
    assert decoy not in caplog.text and "diagnostic" not in caplog.text
    assert "non-JSON stdout line" in caplog.text


async def test_run_turn_returns_exit_code_and_stderr_tail(tmp_path):
    script = tmp_path / "exit3.py"
    script.write_text(
        "import sys\nsys.stdin.read()\nprint('not json')\nprint('{\"type\":\"result\"}')\n"
        "print('warn 1', file=sys.stderr)\nprint('warn 2', file=sys.stderr)\nsys.exit(3)\n"
    )
    objects: list[dict] = []
    raw: list[tuple[str, str]] = []
    code, tail = await run_turn(
        [sys.executable, str(script)],
        env=os.environ,
        cwd=str(tmp_path),
        prompt="hello",
        on_object=objects.append,
        on_raw=lambda stream, line, _t: raw.append((stream, line)),
    )
    assert code == 3 and objects == [{"type": "result"}]
    assert tail == "warn 1\nwarn 2"
    assert ("stdout-text", "not json") in raw and ("stderr", "warn 2") in raw
