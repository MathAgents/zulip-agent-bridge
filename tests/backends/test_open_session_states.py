"""Regression tests for PR #44 (from the PM review): `open_session` while a turn is starting,
while it is being interrupted, and when the session is idle (SPEC §7.1, §9.6). They pass on the
PR and fail on the `main` before it: the F1 and F2 bugs lived in exactly these states."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from backends.claude_backend_helpers import drain, make_backend
from zab.backends import claude as cb
from zab.core.types import SessionSpec, TurnStarted, TurnStatus, UserInput

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang"),
]


async def test_open_session_while_starting(home, monkeypatch):
    """Between start_turn and the spawn: reported, not replaced, no second process."""
    loop = asyncio.get_running_loop()
    gate = asyncio.Event()
    real_find = cb.find_transcript
    first = []

    def gated_find(config_dir, session_id, cwd=None):
        if not first:  # only the turn's own check blocks; open_session's lookup goes through
            first.append(1)
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(timeout=10)
        return real_find(config_dir, session_id, cwd)

    monkeypatch.setattr(cb, "find_transcript", gated_find)
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t", context_prompt="CTX")
    sid = (await backend.create_session(spec)).native_id
    session = backend._sessions[sid]
    turn_id = await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await asyncio.sleep(0.05)
    assert session.turn is None and session.busy  # starting
    info = await backend.open_session(sid, SessionSpec(cwd=str(home["work"]), name="t"))
    assert info.active_turn_id == turn_id
    assert backend._sessions[sid] is session and session.context_prompt == "CTX"
    with pytest.raises(RuntimeError):
        await backend.start_turn(sid, UserInput(text="again", client_id="c2"))
    gate.set()
    events = await drain(backend)
    assert events[-1].status is TurnStatus.COMPLETED and events[-1].turn_id == turn_id
    assert len([json.loads(x) for x in home["log"].read_text().splitlines()]) == 1


async def test_open_session_while_interrupting(home, tmp_path, monkeypatch):
    """SIGINT sent, process still alive (ignores SIGINT): still reported as active."""
    script = tmp_path / "stubborn.py"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, signal, sys, time\n"
        "sys.stdin.read()\n"
        "sid = sys.argv[sys.argv.index('--session-id') + 1]\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(cb.ClaudeTurn.interrupt, "__kwdefaults__", {"grace_s": 1.0})
    config = cb.ClaudeConfig(
        binary=str(script), accounts={"t": str(home["account"])}, default_account="t"
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t")
    sid = (await backend.create_session(spec)).native_id
    turn_id = await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend, until=TurnStarted)  # at spawn
    # The repeat comes from the stub's `init` line, printed once it ignores SIGINT: a stop
    # before that kills it, however long it takes to start (#548).
    await drain(backend, until=TurnStarted)
    stopping = asyncio.create_task(backend.interrupt(sid))
    await asyncio.sleep(0.3)
    assert not stopping.done() and backend._sessions[sid].interrupting
    info = await backend.open_session(sid, spec)
    assert info.active_turn_id == turn_id
    await stopping
    events = await drain(backend)
    assert events[-1].status is TurnStatus.INTERRUPTED
    info = await backend.open_session(sid, spec)
    assert info.active_turn_id is None


# --- F2: the idle case, where the bug was ---------------------------------------------------


async def test_open_session_idle_keeps_context(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t", context_prompt="CTX")
    sid = (await backend.create_session(spec)).native_id
    await backend.start_turn(sid, UserInput(text="one", client_id="c1"))
    await drain(backend, extra=1)
    await backend.open_session(sid, SessionSpec(cwd=str(home["work"]), name="t"))
    assert backend._sessions[sid].context_prompt == "CTX"
    await backend.start_turn(sid, UserInput(text="two", client_id="c2"))
    await drain(backend, extra=1)
    argv = [json.loads(x) for x in home["log"].read_text().splitlines()][-1]["argv"]
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "CTX"
