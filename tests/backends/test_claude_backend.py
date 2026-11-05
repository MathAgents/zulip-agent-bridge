"""`ClaudeBackend` against `testing/fake_claude.py` (SPEC §9, §7.1, §7.8.1, §9.6, §9.8)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from backends.claude_backend_helpers import FAKE, FIXTURES, calls, drain, make_backend
from zab.backends import claude as cb
from zab.core.types import (
    BackendStatus,
    BackendUnavailable,
    ItemCompleted,
    ItemStarted,
    Notice,
    SessionChanged,
    SessionSettings,
    SessionSpec,
    SteerRejected,
    TokenUsage,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
    UsageUpdated,
    UserInput,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang"),
]


async def test_start_reports_status(home):
    backend = make_backend(home)
    await backend.start()
    ev = await asyncio.wait_for(backend._events.get(), 5)
    assert isinstance(ev, BackendStatus) and ev.up
    missing = cb.ClaudeBackend("claude", cb.ClaudeConfig(binary="/nonexistent/claude"))
    await missing.start()
    ev = await asyncio.wait_for(missing._events.get(), 5)
    assert isinstance(ev, BackendStatus) and not ev.up and "not found" in ev.message
    with pytest.raises(BackendUnavailable):
        await missing.create_session(SessionSpec(cwd=str(home["work"]), name="t"))


async def test_capabilities_and_identity(home):
    backend = make_backend(home)
    assert backend.name == "claude" and backend.kind == "claude"
    caps = backend.capabilities
    assert not caps.steer and not caps.approvals and not caps.live_attach and not caps.rename


async def test_two_turns_session_id_then_resume(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()  # BackendStatus
    spec = SessionSpec(cwd=str(home["work"]), name="topic", context_prompt="CTX")
    info = await backend.create_session(spec)
    sid = info.native_id
    assert info.cwd == str(home["work"]) and info.settings.model == "haiku"
    assert (
        info.resume_hint
        == f"cd {home['work']} && CLAUDE_CONFIG_DIR={home['account']} claude --resume {sid}"
    )

    turn_id = await backend.start_turn(sid, UserInput(text="Reply with pong.", client_id="c1"))
    assert turn_id.startswith(sid + ":")
    events = await drain(backend, extra=1)
    kinds = [type(e).__name__ for e in events]
    # TurnStarted at spawn, again with the model from system/init (SPEC B.2 amendment)
    assert kinds == ["TurnStarted", "TurnStarted", "ItemCompleted", "TurnCompleted", "UsageUpdated"]
    assert isinstance(events[0], TurnStarted) and events[0].pgid and events[0].turn_id == turn_id
    assert events[1].model == "claude-haiku-4-5-20251001" and events[1].pgid == events[0].pgid
    done = events[3]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    assert done.final_text == "pong" and done.session_id == sid
    first_call = calls(home)[0]
    assert "--session-id" in first_call["argv"] and sid in first_call["argv"]
    assert first_call["argv"][-2:] == ["--append-system-prompt", "CTX"]
    assert first_call["stdin"] == "Reply with pong."
    assert first_call["env"]["CLAUDE_CONFIG_DIR"] == str(home["account"])
    assert "ANTHROPIC_API_KEY" not in first_call["env"]["names"]  # scrubbed (§9.1)
    assert "CLAUDE_CONFIG_DIR" in first_call["env"]["names"]
    assert first_call["cwd"] == str(home["work"])
    transcript = cb.transcript_path(str(home["account"]), str(home["work"]), sid)
    assert await asyncio.to_thread(os.path.exists, transcript)

    await backend.start_turn(sid, UserInput(text="Again.", client_id="c2"))
    events = await drain(backend, extra=1)
    second_call = calls(home)[1]
    assert "--resume" in second_call["argv"] and "--session-id" not in second_call["argv"]
    usage = events[-1]
    assert isinstance(usage, UsageUpdated) and usage.usage.total_tokens is not None
    single = done.usage.total_tokens if done.usage else 0
    assert usage.usage.total_tokens == 2 * single  # running total over both turns (§9.4)
    assert usage.usage.cost_usd is not None and usage.usage.cost_usd > 0


async def test_second_turn_while_running_is_refused(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    await drain(backend, until=TurnStarted)
    with pytest.raises(RuntimeError):
        await backend.start_turn(info.native_id, UserInput(text="again", client_id="c2"))
    await backend.interrupt(info.native_id)
    events = await drain(backend)
    assert isinstance(events[-1], TurnCompleted) and events[-1].status is TurnStatus.INTERRUPTED


async def test_interrupt_gives_interrupted_status(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    sid = info.native_id
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    started = await drain(backend, until=TurnStarted)
    assert isinstance(started[-1], TurnStarted)
    # Stop once a tool call has started, not after a fixed sleep: on a loaded runner the fake
    # may not have written anything yet 0.2 s after its spawn (#548).
    tool = (await drain(backend, until=ItemStarted))[-1].item.item_id
    await backend.interrupt(sid)
    events = await drain(backend, extra=1)
    done = events[-2]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.INTERRUPTED
    assert done.error is None
    # the first tool call ends: with its result, or, had the stop come first, as left without
    # one (that path: test_process_death_without_result_fails_the_turn)
    assert any(isinstance(e, ItemCompleted) and e.item.item_id == tool for e in events)
    assert isinstance(events[-1], UsageUpdated)
    # the session is idle again: a new turn can start
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_HANG")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    await backend.start_turn(sid, UserInput(text="go", client_id="c2"))
    events = await drain(backend)
    assert events[-1].status is TurnStatus.COMPLETED


async def test_graceful_interrupt_like_the_real_cli(home, monkeypatch):
    """The fake's graceful mode reproduces the CLI's SIGINT output (DECISIONS §9.6): the turn
    still ends as INTERRUPTED, with no final text and no error."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "graceful")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    # Stop once a tool call has started: the fake sets its SIGINT handler before its first line,
    # and a signal before that kills it with no result (#548).
    await drain(backend, until=ItemStarted)
    await backend.interrupt(sid)
    events = await drain(backend, extra=1)
    done = events[-2]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.INTERRUPTED
    assert done.error is None  # the commentary before the stop is kept as the last text (§7.4)
    assert done.usage is not None and done.usage.total_tokens == 0  # the CLI's abort result


async def test_failed_turn_and_error_exit(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "error.jsonl"))
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    events = await drain(backend)
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.error is not None and "isn't available" in done.error.message
    assert "`!model`" in done.error.message  # what to do about it (§7.9)


async def test_open_session_reads_transcript_and_resumes(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t")
    sid = (await backend.create_session(spec)).native_id
    await backend.start_turn(sid, UserInput(text="Reply with pong.", client_id="c1"))
    await drain(backend, extra=1)

    fresh = make_backend(home)  # a restarted bridge
    await fresh.start()
    await fresh._events.get()
    info = await fresh.open_session(sid, spec)
    assert info.native_id == sid and info.active_turn_id is None
    assert info.preview == "Reply with pong."
    assert info.last_turn is not None and info.last_turn.final_text == "pong"
    assert info.updated_at is not None
    await fresh.start_turn(sid, UserInput(text="More.", client_id="c2"))
    events = await drain(fresh)
    assert events[-1].status is TurnStatus.COMPLETED
    assert "--resume" in calls(home)[-1]["argv"]

    # open_session stays lenient (recovery of a persisted session without a transcript);
    # the definitive "does this session exist" question is lookup_session's
    unknown = await fresh.open_session("00000000-0000-4000-8000-000000000000", spec)
    assert unknown.preview is None and unknown.last_turn is None


async def test_list_sessions_newest_first(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t")
    ids = []
    for i in range(2):
        sid = (await backend.create_session(spec)).native_id
        ids.append(sid)
        await backend.start_turn(sid, UserInput(text=f"Prompt number {i}.", client_id="c"))
        await drain(backend, extra=1)
        newest = cb.transcript_path(str(home["account"]), str(home["work"]), sid)
        os.utime(newest, (1_800_000_000 + i, 1_800_000_000 + i))
    listed = await backend.list_sessions(str(home["work"]))
    assert [s.native_id for s in listed] == [ids[1], ids[0]]
    assert listed[0].preview == "Prompt number 1." and listed[0].settings.account == "test"
    assert listed[0].resume_hint.endswith(f"claude --resume {ids[1]}")
    assert await backend.list_sessions(str(home["work"]), limit=1) == listed[:1]
    assert await backend.list_sessions("/elsewhere") == []
    everything = await backend.list_sessions(None)
    assert {s.native_id for s in everything} == set(ids)
    assert all(s.cwd == str(home["work"]) for s in everything)  # from the records, not the key
    assert all(s.resume_hint.startswith(f"cd {home['work']} &&") for s in everything)
    # a transcript without a cwd record is skipped in an all-projects listing
    orphan_dir = home["account"] / "projects" / "-orphan-project"
    orphan_dir.mkdir()
    (orphan_dir / "11111111-1111-4111-8111-111111111111.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    )
    assert {s.native_id for s in await backend.list_sessions(None)} == set(ids)
    # a transcript recorded under another cwd with the same encoding (e.g. `a.b` vs `a-b`)
    # is excluded from a filtered listing and listed under its own cwd otherwise
    same_key_dir = home["account"] / "projects" / cb.encode_project_dir(str(home["work"]))
    twin_cwd = str(home["work"]).replace("work", "w.rk") if "work" in str(home["work"]) else "/x"
    (same_key_dir / "22222222-2222-4222-8222-222222222222.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": "/somewhere/else",
                "message": {"role": "user", "content": "twin"},
            }
        )
        + "\n"
    )
    assert twin_cwd  # (cwd collisions are simulated with an explicit record, not by name)
    assert {s.native_id for s in await backend.list_sessions(str(home["work"]))} == set(ids)
    twin = [s for s in await backend.list_sessions(None) if s.preview == "twin"]
    assert len(twin) == 1 and twin[0].cwd == "/somewhere/else"


async def test_doctor(home, monkeypatch):
    backend = make_backend(home)
    checks = {c.name: c for c in await backend.doctor()}
    assert checks["claude version"].ok and not checks["claude version"].warning
    assert checks["claude account test"].ok and "claude.ai" in checks["claude account test"].detail
    detail = checks["claude account test"].detail
    # never the account's identity: the fake's auth status carries decoy e-mail, org id and
    # org name, none of which may reach the doctor line (PM review on PR #51)
    assert "@" not in detail and "decoy" not in detail.lower() and "orgId" not in detail
    assert "Decoy Org" not in detail and "00000000-0000-4000" not in detail

    monkeypatch.setenv("ZAB_FAKE_CLAUDE_VERSION", "9.9.9")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGGED_IN", "0")
    checks = {c.name: c for c in await backend.doctor()}
    assert checks["claude version"].ok and checks["claude version"].warning
    # `auth status` exits 1 when signed out (the real CLI does too): still read as JSON, not
    # reported as a failed command (Drew's rc3 rollout)
    assert not checks["claude account test"].ok
    assert checks["claude account test"].detail == cb.NOT_SIGNED_IN

    other = make_backend(home)
    other.config.accounts["ghost"] = str(home["account"] / "missing")
    checks = {c.name: c for c in await other.doctor()}
    # an operational failure has its own name, never the sign-in check's (codex on #152)
    assert not checks["claude config ghost"].ok and "claude account ghost" not in checks

    async def hangs(*args, **kwargs):
        raise TimeoutError

    real_run = cb._run_capture

    async def run(argv, **kwargs):
        if argv[1:3] == ["auth", "status"]:
            return await hangs()
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(cb, "_run_capture", run)
    checks = {c.name: c for c in await backend.doctor()}
    assert not checks["claude auth test"].ok and "claude account test" not in checks
    assert "TimeoutError" in checks["claude auth test"].detail

    broken = cb.ClaudeBackend("claude", cb.ClaudeConfig(binary="/nonexistent/claude"))
    checks = await broken.doctor()
    assert len(checks) == 1 and not checks[0].ok


async def test_steer_respond_settings_and_close(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    with pytest.raises(SteerRejected):
        await backend.steer(info.native_id, "turn", UserInput(text="x", client_id="c"))
    with pytest.raises(RuntimeError):
        await backend.respond("r", cb.ApprovalDecision.ACCEPT)
    # the core sends the session's whole state back (core/session.py `!model`/`!effort`):
    # `replace(stored, model=value)`, with None for `!model ""` (SessionSettings: None = default)
    stored = SessionSettings(model="haiku", approval="bypassPermissions", account="test")
    updated = await backend.update_settings(info.native_id, replace(stored, model="opus"))
    assert updated.model == "opus" and updated.approval == "bypassPermissions"
    assert updated.account == "test"
    reset = await backend.update_settings(info.native_id, replace(updated, model=None))
    assert reset.model is None  # the agent's default again (PM review on PR #49)
    assert reset.approval == "bypassPermissions" and reset.effort is None
    # the default is the config's haiku, which takes no effort (test_claude_settings.py)
    reset = await backend.update_settings(info.native_id, replace(reset, model="sonnet"))
    reset = await backend.update_settings(info.native_id, replace(reset, effort="high"))
    assert reset.effort == "high" and reset.model == "sonnet"
    reset = await backend.update_settings(info.native_id, replace(reset, effort=None, model=None))
    assert reset.effort is None and reset.approval == "bypassPermissions"
    also = await backend.update_settings(info.native_id, SessionSettings(model="", effort=""))
    assert also.model is None and also.effort is None  # "" is harmless and resets too
    assert also.account == "test"  # the account never changes in Phase 1 (§9.2)
    await backend.rename(info.native_id, "new name")  # no-op
    await backend.close()
    assert [e async for e in backend.events()] == []


async def test_close_terminates_running_turns(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    started = await drain(backend, until=TurnStarted)
    pgid = started[-1].pgid
    await asyncio.sleep(0.2)
    await backend.close()
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    events = [e async for e in backend.events()]
    assert any(isinstance(e, TurnCompleted) and e.status is TurnStatus.INTERRUPTED for e in events)


async def test_plan_is_carried_across_turns(home, monkeypatch):
    """issue #36: plan state belongs to the session, not the turn."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    await backend.start_turn(sid, UserInput(text="one", client_id="c1"))
    events = await drain(backend, extra=1)
    plans = [e for e in events if type(e).__name__ == "PlanUpdated"]
    assert plans and len(plans[-1].steps) == 3
    assert len(backend._sessions[sid].plan) == 3
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    await backend.start_turn(sid, UserInput(text="two", client_id="c2"))
    await drain(backend, extra=1)
    assert len(backend._sessions[sid].plan) == 3  # a turn without plan tools keeps it


async def test_a_resumed_session_with_a_stopped_background_task_delivers_its_answer(
    home, monkeypatch
):
    """issue #36 (Wed Oct 7): the turn's one `TurnCompleted` carries the answer, not the empty
    result Claude Code reports first for the previous process's background task."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "background_resume.jsonl"))
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    await backend.start_turn(sid, UserInput(text="Summarise your last turn.", client_id="c1"))
    events = await drain(backend, extra=1)
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.COMPLETED
    assert done[0].final_text and done[0].final_text.startswith("I started a background shell")
    assert backend._sessions[sid].last_completed.final_text == done[0].final_text
    await backend.close()


async def test_lingering_grandchild_does_not_hold_the_turn(home, tmp_path):
    """issue #36: after the CLI exits, waiting for stdout EOF is bounded (§9.6)."""
    script = tmp_path / "linger"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "sys.stdin.read()\n"
        # a grandchild that inherits stdout and outlives the parent
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        'print(\'{"type":"system","subtype":"init","session_id":"s"}\', flush=True)\n'
        'print(\'{"type":"result","subtype":"success","is_error":false,"result":"ok",'
        '"session_id":"s"}\', flush=True)\n'
    )
    script.chmod(0o755)
    config = cb.ClaudeConfig(
        binary=str(script), accounts={"test": str(home["account"])}, default_account="test"
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    t0 = asyncio.get_running_loop().time()
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    events = await asyncio.wait_for(drain(backend, extra=1), 30)
    assert asyncio.get_running_loop().time() - t0 < 20  # not the grandchild's 60 s
    done = events[-2]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    assert done.final_text == "ok"


async def test_lingering_grandchild_on_stderr_does_not_hold_the_turn(home, tmp_path):
    """codex round 1 on PR #49: a holder of stderr only is killed after the grace too."""
    script = tmp_path / "linger_err"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "sys.stdin.read()\n"
        # grandchild keeps stderr (not stdout) open and outlives the parent
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdout=subprocess.DEVNULL)\n"
        'print(\'{"type":"system","subtype":"init","session_id":"s"}\', flush=True)\n'
        'print(\'{"type":"result","subtype":"success","is_error":false,"result":"ok",'
        '"session_id":"s"}\', flush=True)\n'
        "sys.stdout.close()\n"
        "os.close(1)\n"
    )
    script.chmod(0o755)
    config = cb.ClaudeConfig(
        binary=str(script), accounts={"test": str(home["account"])}, default_account="test"
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    t0 = asyncio.get_running_loop().time()
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    events = await asyncio.wait_for(drain(backend, extra=1), 30)
    assert asyncio.get_running_loop().time() - t0 < 20
    assert isinstance(events[-2], TurnCompleted) and events[-2].final_text == "ok"


async def test_chatty_grandchild_cannot_extend_the_grace(tmp_path):
    """codex round 2 on PR #49: one deadline after exit, however often the descendant writes."""
    child = tmp_path / "noisy_child.py"
    child.write_text(
        "import json, sys, time\n"
        "for _ in range(600):\n"
        "    print(json.dumps({'type': 'noise'}), flush=True)\n"
        "    time.sleep(0.05)\n"
    )
    script = tmp_path / "chatty.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
    )
    turn = cb.ClaudeTurn(
        [sys.executable, str(script)], env=os.environ, cwd=str(tmp_path), prompt="x"
    )
    await turn.start()
    t0 = asyncio.get_running_loop().time()
    seen = 0
    async for _obj in turn.objects(eof_grace_s=0.5):
        seen += 1
    elapsed = asyncio.get_running_loop().time() - t0
    assert seen >= 1
    assert elapsed < 3.0, f"the drain ran {elapsed:.1f} s: the grace was being extended"
    assert await asyncio.wait_for(turn.wait(eof_grace_s=0.5), 5) == 0
    for _ in range(40):  # the killed grandchild is reaped by init shortly after
        if not turn.group_alive():
            break
        await asyncio.sleep(0.05)
    assert not turn.group_alive()


async def test_doctor_detail_has_no_raw_stderr(home, monkeypatch, tmp_path):
    script = tmp_path / "noisy"
    script.write_text(
        "#!/bin/sh\necho 'secret-looking stderr sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx' >&2\nexit 7\n"
    )
    script.chmod(0o755)
    backend = cb.ClaudeBackend("claude", cb.ClaudeConfig(binary=str(script)))
    checks = await backend.doctor()
    assert not checks[0].ok and "sk-ant" not in checks[0].detail and "OSError" in checks[0].detail


async def test_close_right_after_start_turn_does_not_hang(home, monkeypatch):
    """Shutdown while the turn's process is still starting (codex round 1 on PR #24)."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    await asyncio.wait_for(backend.close(), 15)  # no await in between: the slot is not set yet
    events = [e async for e in backend.events()]
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.INTERRUPTED


async def test_close_during_transcript_check_never_launches(home, monkeypatch):
    """codex round 2 on PR #24: shutdown while start_turn's task is still checking the
    transcript must not launch the process afterwards."""
    loop = asyncio.get_running_loop()
    gate = asyncio.Event()
    real_find = cb.find_transcript

    def gated_find(config_dir: str, session_id: str, cwd: str | None = None) -> str | None:
        # runs in a worker thread: block until the test has called close()
        asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(timeout=10)
        return real_find(config_dir, session_id, cwd)

    monkeypatch.setattr(cb, "find_transcript", gated_find)
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    turn_id = await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    await asyncio.sleep(0.1)  # the task is inside the gated transcript check
    closing = asyncio.create_task(backend.close())
    await asyncio.sleep(0.1)
    gate.set()
    await asyncio.wait_for(closing, 15)
    events = [e async for e in backend.events()]
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.INTERRUPTED
    assert done[0].turn_id == turn_id
    assert not home["log"].exists(), "the fake must never have been launched"
    with pytest.raises(BackendUnavailable):
        await backend.start_turn(info.native_id, UserInput(text="again", client_id="c2"))


async def test_close_while_prompt_write_is_blocked(home, tmp_path):
    """codex round 4 on PR #24: a child that does not read stdin yet, plus a prompt larger than
    the pipe buffer, keeps start() in drain(); a shutdown must still signal the process."""
    script = tmp_path / "slowreader"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys, time\ntime.sleep(30)\nsys.stdin.read()\n"
    )
    script.chmod(0o755)
    config = cb.ClaudeConfig(
        binary=str(script), accounts={"test": str(home["account"])}, default_account="test"
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="x" * (4 * 1024 * 1024), client_id="c"))
    session = backend._sessions[info.native_id]
    for _ in range(100):  # the process exists while the prompt write is still blocked
        if session.turn is not None:
            break
        await asyncio.sleep(0.05)
    assert session.turn is not None and session.turn.pgid is not None
    pgid = session.turn.pgid
    # TurnStarted, with the pgid, is already out although the child printed nothing (§7.8.1)
    first = await asyncio.wait_for(backend._events.get(), 5)
    assert isinstance(first, TurnStarted) and first.pgid == pgid
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(backend.close(), 15)
    assert asyncio.get_running_loop().time() - t0 < 10  # not the child's 30 s sleep
    events = [e async for e in backend.events()]
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.INTERRUPTED
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


async def test_interrupt_right_after_start_turn(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    await backend.interrupt(info.native_id)  # before the process exists
    events = await asyncio.wait_for(drain(backend, extra=1), 15)
    assert isinstance(events[-2], TurnCompleted) and events[-2].status is TurnStatus.INTERRUPTED
    assert isinstance(events[-1], UsageUpdated)


async def test_follow_up_turn_can_start_on_completion(home, monkeypatch):
    """TurnCompleted arrives only once the session can take the next turn (§7.4 step 6)."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LINGER", "1.0")  # stdout stays open after the result
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    sid = info.native_id
    await backend.start_turn(sid, UserInput(text="one", client_id="c1"))
    events = await drain(backend)
    assert isinstance(events[-1], TurnCompleted)
    second = await backend.start_turn(sid, UserInput(text="two", client_id="c2"))  # must not raise
    events = await drain(backend, extra=1)
    assert isinstance(events[-2], TurnCompleted) and events[-2].turn_id == second
    assert [type(e).__name__ for e in events][:1] == ["UsageUpdated"]  # the first turn's total


async def test_start_retries_discovery(home, tmp_path):
    binary = tmp_path / "bin" / "claude"
    config = cb.ClaudeConfig(
        binary=str(binary), accounts={"test": str(home["account"])}, default_account="test"
    )
    backend = cb.ClaudeBackend("claude", config, discovery_backoff=(0.05, 0.2))
    await backend.start()
    first = await asyncio.wait_for(backend._events.get(), 5)
    assert isinstance(first, BackendStatus) and not first.up
    with pytest.raises(BackendUnavailable):
        await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    binary.parent.mkdir()
    binary.write_bytes(FAKE.read_bytes())
    binary.chmod(0o755)
    second = await asyncio.wait_for(backend._events.get(), 5)
    assert isinstance(second, BackendStatus) and second.up
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    assert info.native_id
    await backend.close()


async def test_close_cancels_pending_discovery(home):
    backend = cb.ClaudeBackend(
        "claude", cb.ClaudeConfig(binary="/nonexistent/claude"), discovery_backoff=(30.0, 60.0)
    )
    await backend.start()
    await backend._events.get()
    await asyncio.wait_for(backend.close(), 5)
    assert backend._discovery is not None and backend._discovery.done()


def test_transcript_summary_reads_past_large_records(tmp_path):
    """codex round 5 on PR #24: the newest exchange must be found however much precedes it."""
    path = tmp_path / "big.jsonl"
    filler = json.dumps(
        {"type": "attachment", "attachment": {"type": "blob", "data": "x" * (3 << 20)}}
    )
    with path.open("w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "old"}}) + "\n")
        f.write(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "A0"}]}}
            )
            + "\n"
        )
        for _ in range(4):
            f.write(filler + "\n")
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "new"}}) + "\n")
        f.write(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "A1"}]}}
            )
            + "\n"
        )
    summary = cb.read_transcript_summary(str(path))
    assert (summary["first_user"], summary["last_user"], summary["last_assistant"]) == (
        "old",
        "new",
        "A1",
    )


def test_resume_hint_is_shell_quoted():
    hint = cb.ClaudeBackend.resume_hint("/home/u/.claude two", "/work/my project", "abc-123")
    assert hint == (
        "cd '/work/my project' && CLAUDE_CONFIG_DIR='/home/u/.claude two' claude --resume abc-123"
    )
    plain = cb.ClaudeBackend.resume_hint("/home/u/.claude", "/work/proj", "abc")
    assert plain == "cd /work/proj && CLAUDE_CONFIG_DIR=/home/u/.claude claude --resume abc"
    nasty = cb.ClaudeBackend.resume_hint("/h", "/work/x; rm -rf ~", "abc")
    assert "; rm" not in nasty.replace("'/work/x; rm -rf ~'", "")


async def test_workspace_with_underscore_and_space_resumes(tmp_path, monkeypatch):
    """issue #36 item 4: `_` and spaces encode to `-`; the second turn must resume."""
    account = tmp_path / "acct"
    account.mkdir()
    work = tmp_path / "my_repo with space"
    work.mkdir()
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_HANG", raising=False)
    config = cb.ClaudeConfig(binary=str(FAKE), accounts={"t": str(account)}, default_account="t")
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(work), name="t")
    sid = (await backend.create_session(spec)).native_id
    await backend.start_turn(sid, UserInput(text="one", client_id="c1"))
    await drain(backend, extra=1)
    written = cb.find_transcript(str(account), sid, str(work))
    assert written is not None and "-my-repo-with-space/" in written.replace("\\", "/")
    await backend.start_turn(sid, UserInput(text="two", client_id="c2"))
    await drain(backend, extra=1)
    calls_ = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert "--session-id" in calls_[0]["argv"] and "--resume" in calls_[1]["argv"]
    listed = await backend.list_sessions(str(work))
    assert [s.native_id for s in listed] == [sid] and listed[0].cwd == str(work)
    # A transcript stored under a name the encoder cannot predict (long paths get a hash
    # suffix) is still found by session id, for open_session, resume and listing. The listing
    # is asserted BEFORE any further turn: the fake writes its own copy of the transcript under
    # the encoded directory on the next turn, which would make `sid2` show up for the wrong
    # reason (PM review on PR #44); only a scan of every project directory finds it here.
    other = account / "projects" / "-some-truncated-name-abc123"
    other.mkdir()
    sid2 = "22222222-2222-4222-8222-222222222222"
    (other / f"{sid2}.jsonl").write_text(
        json.dumps(
            {"type": "user", "cwd": str(work), "message": {"role": "user", "content": "elsewhere"}}
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "cwd": str(work),
                "message": {"content": [{"type": "text", "text": "A"}]},
            }
        )
        + "\n"
    )
    info = await backend.open_session(sid2, spec)
    assert info.preview == "elsewhere" and info.last_turn is not None
    assert {s.native_id for s in await backend.list_sessions(str(work))} == {sid, sid2}
    await backend.start_turn(sid2, UserInput(text="three", client_id="c3"))
    await drain(backend, extra=1)
    calls_ = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert "--resume" in calls_[-1]["argv"] and sid2 in calls_[-1]["argv"]
    assert {s.native_id for s in await backend.list_sessions(str(work))} == {sid, sid2}
    assert cb.find_transcript(str(account), "../../etc/passwd") is None  # id must be a UUID


async def test_open_session_keeps_a_running_turn(home, monkeypatch):
    """issue #36 item 5: `!status` during a turn must not forget the turn or clear context."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t", context_prompt="CTX")
    sid = (await backend.create_session(spec)).native_id
    turn_id = await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend, until=TurnStarted)
    info = await backend.open_session(sid, SessionSpec(cwd=str(home["work"]), name="t"))
    assert info.active_turn_id == turn_id
    assert backend._sessions[sid].context_prompt == "CTX"  # empty spec prompt keeps the old one
    with pytest.raises(RuntimeError):  # still the same busy session: no second process
        await backend.start_turn(sid, UserInput(text="again", client_id="c2"))
    await backend.interrupt(sid)
    events = await drain(backend)
    assert events[-1].status is TurnStatus.INTERRUPTED and events[-1].turn_id == turn_id
    info = await backend.open_session(
        sid, SessionSpec(cwd=str(home["work"]), name="t", context_prompt="NEW")
    )
    assert info.active_turn_id is None and backend._sessions[sid].context_prompt == "NEW"


def test_add_usage_and_transcript_helpers(tmp_path):
    a = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.5)
    b = TokenUsage(
        input_tokens=1, output_tokens=1, total_tokens=2, context_window=200000, cost_usd=0.25
    )
    total = cb._add_usage(cb._add_usage(TokenUsage(), a), b)
    assert (total.input_tokens, total.output_tokens, total.total_tokens) == (11, 6, 17)
    assert total.context_window == 200000 and total.cost_usd == 0.75
    assert cb._add_usage(a, None) == a

    assert cb.transcript_path("/home/u/.claude", "/home/u/18.705", "sid") == (
        "/home/u/.claude/projects/-home-u-18-705/sid.jsonl"
    )
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "x"}),
                json.dumps(
                    {"type": "user", "message": {"role": "user", "content": "First prompt"}}
                ),
                json.dumps({"type": "attachment", "attachment": {"type": "environment"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "thinking", "thinking": ""}]},
                    }
                ),
                json.dumps(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "A1"}]}}
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]
                        },
                    }
                ),
                json.dumps(
                    {"type": "user", "message": {"role": "user", "content": "Second prompt"}}
                ),
                "not json",
                json.dumps(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "A2"}]}}
                ),
                json.dumps({"type": "cost-state", "totalCostUSD": 0.1}),
            ]
        )
    )
    assert cb.read_transcript_summary(str(path)) == {
        "first_user": "First prompt",
        "last_user": "Second prompt",
        "last_assistant": "A2",
        "cwd": None,
    }


@pytest.mark.skipif(
    importlib.util.find_spec("zab.workspace") is None, reason="workspace package not on this branch"
)
def test_workspace_is_local(home):
    backend = make_backend(home)
    ws = backend.workspace(str(home["work"]), str(home["work"]))
    assert ws.root == str(home["work"]) and ws.cwd == str(home["work"])


async def test_api_key_notice_reaches_the_topic(home, tmp_path, monkeypatch):
    """PM review on PR #49: the API-key guard stays a warning but must be visible in the topic,
    i.e. arrive as a Notice through `events()`, not only as a log line. The six fixtures carry
    `apiKeySource: "none"`, so a copy of `simple` with a key source is replayed."""
    lines = (FIXTURES / "simple.jsonl").read_text().splitlines()
    keyed = []
    for line in lines:
        rec = json.loads(line)
        data = rec.get("data")
        if rec.get("kind") == "stdout" and isinstance(data, dict) and data.get("subtype") == "init":
            data["apiKeySource"] = "ANTHROPIC_API_KEY"
        keyed.append(json.dumps(rec))
    fixture = tmp_path / "keyed.jsonl"
    fixture.write_text("\n".join(keyed) + "\n")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(fixture))
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    events = await drain(backend)
    notices = [e for e in events if isinstance(e, Notice) and "API key" in e.message]
    assert len(notices) == 1 and notices[0].level == "warning"
    assert "ANTHROPIC_API_KEY" in notices[0].message and events[-1].status is TurnStatus.COMPLETED
    # the unmodified fixture (apiKeySource "none") produces no such notice
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    await backend.start_turn(sid, UserInput(text="again", client_id="c2"))
    events = await drain(backend)
    assert not any(isinstance(e, Notice) and "API key" in e.message for e in events)
    await backend.close()


async def test_open_session_reports_the_last_completed_turn_with_its_id(home, monkeypatch):
    """PM follow-up from the #39 re-review: a `!status` landing just as a turn finishes must find
    that turn as `last_turn` with its real id, so the core completes it instead of marking it
    "interrupted by bridge restart" and dropping the answer (§7.8.1)."""
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t")
    sid = (await backend.create_session(spec)).native_id
    assert (await backend.open_session(sid, spec)).last_turn is None  # nothing ran yet
    turn_id = await backend.start_turn(sid, UserInput(text="one", client_id="c1"))
    done = next(e for e in await drain(backend, extra=1) if isinstance(e, TurnCompleted))
    info = await backend.open_session(sid, spec)
    assert info.active_turn_id is None and info.last_turn is not None
    assert info.last_turn.turn_id == turn_id and info.last_turn.status is TurnStatus.COMPLETED
    assert info.last_turn.final_text == done.final_text and info.last_turn.usage == done.usage
    # while the next turn runs, the previous one is still the last completed; then the new one
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    second = await backend.start_turn(sid, UserInput(text="two", client_id="c2"))
    await drain(backend, until=TurnStarted)
    info = await backend.open_session(sid, spec)
    assert info.active_turn_id == second and info.last_turn.turn_id == turn_id
    await backend.interrupt(sid)
    await drain(backend, extra=1)
    info = await backend.open_session(sid, spec)
    assert info.active_turn_id is None and info.last_turn.turn_id == second
    assert info.last_turn.status is TurnStatus.INTERRUPTED
    # a failed turn is reported as failed, with its error
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_HANG")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "error.jsonl"))
    third = await backend.start_turn(sid, UserInput(text="three", client_id="c3"))
    await drain(backend, extra=1)
    info = await backend.open_session(sid, spec)
    assert info.last_turn.turn_id == third and info.last_turn.status is TurnStatus.FAILED
    assert info.last_turn.error is not None
    # a backend that never ran the session (after a restart) falls back to the transcript
    fresh = make_backend(home)
    await fresh.start()
    await fresh._events.get()
    info = await fresh.open_session(sid, spec)
    assert info.last_turn is not None and info.last_turn.turn_id == ""


def test_transcript_summary_skips_valid_but_unexpected_lines(tmp_path):
    """PM follow-up on #44: `[1, 2]`, `"str"`, a string `message`, a non-dict block and a bare
    number are valid JSON lines the reader must skip, not crash on."""
    path = tmp_path / "odd.jsonl"
    lines = [
        json.dumps([1, 2]),
        json.dumps("str"),
        json.dumps(42),
        "null",
        json.dumps({"type": "user", "message": "a string message", "cwd": "/w"}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "first prompt"}}),
        json.dumps({"type": "assistant", "message": {"content": "not a list"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [1, "x", None, {"type": "text", "text": "A1"}]},
            }
        ),
        json.dumps({"type": "user", "message": {"content": ["not", "a", "string"]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "last prompt"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text"}, {"type": "text", "text": "A2"}]},
            }
        ),
        json.dumps({"type": "assistant", "message": {"content": [{"no": "type"}]}}),
    ]
    path.write_text("\n".join(lines) + "\n")
    summary = cb.read_transcript_summary(str(path))
    assert summary == {
        "first_user": "first prompt",
        "last_user": "last prompt",
        "last_assistant": "A2",
        "cwd": "/w",
    }
    assert cb.read_transcript_head(str(path)) == {"cwd": "/w", "first_user": "first prompt"}


async def test_open_session_survives_an_odd_transcript(home):
    """The session must not stick in `error` because its transcript has an unexpected line."""
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    sid = "33333333-3333-4333-8333-333333333333"
    directory = home["account"] / "projects" / cb.encode_project_dir(str(home["work"]))
    directory.mkdir(parents=True)
    (directory / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(home["work"]), "message": "oops"})
        + "\n[1, 2]\n"
        + json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}})
        + "\n"
        + json.dumps(
            {"type": "assistant", "message": {"content": [7, {"type": "text", "text": "hi"}]}}
        )
        + "\n"
    )
    info = await backend.open_session(sid, SessionSpec(cwd=str(home["work"]), name="t"))
    assert info.preview == "hello" and info.last_turn is not None
    assert info.last_turn.final_text == "hi"
    listed = await backend.list_sessions(str(home["work"]))
    assert [s.native_id for s in listed] == [sid]


async def test_symlinks_under_projects_are_not_followed_outside(home, tmp_path):
    """PM follow-up on #44: a symlinked project directory or transcript that resolves outside
    CLAUDE_CONFIG_DIR is refused by lookup and listing alike."""
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    work = str(home["work"])
    projects = home["account"] / "projects"
    projects.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sid_dir = "44444444-4444-4444-8444-444444444444"
    sid_file = "55555555-5555-4555-8555-555555555555"
    sid_real = "66666666-6666-4666-8666-666666666666"
    record = (
        json.dumps({"type": "user", "cwd": work, "message": {"role": "user", "content": "p"}})
        + "\n"
    )
    (outside / f"{sid_dir}.jsonl").write_text(record)
    (outside / f"{sid_file}.jsonl").write_text(record)
    (projects / "-evil").symlink_to(outside)  # a directory symlink
    encoded = projects / cb.encode_project_dir(work)
    encoded.mkdir()
    (encoded / f"{sid_file}.jsonl").symlink_to(outside / f"{sid_file}.jsonl")  # a file symlink
    (encoded / f"{sid_real}.jsonl").write_text(record)
    assert cb.find_transcript(str(home["account"]), sid_dir, work) is None
    assert cb.find_transcript(str(home["account"]), sid_file, work) is None
    assert cb.find_transcript(str(home["account"]), sid_real, work) == str(
        encoded / f"{sid_real}.jsonl"
    )
    listed = {s.native_id for s in await backend.list_sessions(work)}
    assert listed == {sid_real}
    # a symlink that stays inside projects/ is harmless and still followed
    (projects / "-alias").symlink_to(encoded)
    assert cb.find_transcript(str(home["account"]), sid_real) is not None


async def test_open_session_after_session_change_aliases_the_live_session(home, monkeypatch):
    """PM follow-up on #44: after `SessionChanged`, opening the old id must reach the live
    session (its new id), never create a second idle one that a restart could reap (§9.3)."""
    new_id = "77777777-7777-4777-8777-777777777777"
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_REPORT_SESSION", new_id)
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t", context_prompt="CTX")
    old_id = (await backend.create_session(spec)).native_id
    turn_id = await backend.start_turn(old_id, UserInput(text="one", client_id="c1"))
    events = await drain(backend, extra=1)
    changed = [e for e in events if isinstance(e, SessionChanged)]
    assert changed and changed[0].session_id == old_id and changed[0].new_session_id == new_id
    assert events[-2].turn_id == turn_id and events[-2].session_id == new_id
    info = await backend.open_session(old_id, SessionSpec(cwd=str(home["work"]), name="t"))
    assert info.native_id == new_id and len(backend._sessions) == 1
    assert backend._sessions[new_id].context_prompt == "CTX"
    assert info.last_turn is not None and info.last_turn.turn_id == turn_id
    # the old id still drives the same session: the next turn resumes under the new id
    await backend.start_turn(old_id, UserInput(text="two", client_id="c2"))
    await drain(backend, extra=1)
    argv = calls(home)[-1]["argv"]
    assert "--resume" in argv and new_id in argv and old_id not in argv
    assert len(backend._sessions) == 1
    await backend.update_settings(old_id, SessionSettings(model="opus"))
    assert backend._sessions[new_id].settings.model == "opus"


async def test_open_session_binds_the_account_and_cwd_of_the_transcript(home, tmp_path):
    """The attach request (issue #36): an id unknown to this process is looked up in
    every configured account's transcripts; the session takes that account and the recorded
    cwd, not the topic's cwd and the default account."""
    alt = tmp_path / "alt-account"
    other_work = tmp_path / "other_work"
    other_work.mkdir()
    sid = "33333333-3333-4333-8333-333333333333"
    directory = alt / "projects" / cb.encode_project_dir(str(other_work))
    directory.mkdir(parents=True)
    (directory / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": str(other_work),
                "message": {"role": "user", "content": "hi there"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "cwd": str(other_work),
                "message": {"content": [{"type": "text", "text": "A"}]},
            }
        )
        + "\n"
    )
    config = cb.ClaudeConfig(
        binary=str(FAKE),
        accounts={"test": str(home["account"]), "alt": str(alt)},
        default_account="test",
        defaults=SessionSettings(model="haiku", approval="bypassPermissions"),
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    info = await backend.open_session(sid, SessionSpec(cwd=str(home["work"]), name="t"))
    assert info.native_id == sid
    assert info.cwd == str(other_work) and info.settings.account == "alt"
    assert info.preview == "hi there" and info.last_turn is not None
    assert backend._sessions[sid].config_dir == str(alt)
    # the next turn runs in the transcript's cwd and account, and resumes
    await backend.start_turn(sid, UserInput(text="go on", client_id="c1"))
    await drain(backend, extra=1)
    call = calls(home)[-1]
    assert "--resume" in call["argv"] and call["env"].get("CLAUDE_CONFIG_DIR") == str(alt)
    assert call["cwd"] == str(other_work)


async def test_lookup_session_finds_transcripts_and_refuses_unknown_ids(home, tmp_path):
    """`!attach ID` asks lookup_session first: a transcript in any account is reported with its
    real account and cwd; an unknown or malformed id is None and nothing is created (§9.8)."""
    alt = tmp_path / "alt-account"
    other_work = tmp_path / "other_work"
    other_work.mkdir()
    sid = "55555555-5555-4555-8555-555555555555"
    directory = alt / "projects" / cb.encode_project_dir(str(other_work))
    directory.mkdir(parents=True)
    (directory / f"{sid}.jsonl").write_text(
        json.dumps(
            {"type": "user", "cwd": str(other_work), "message": {"role": "user", "content": "q"}}
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "cwd": str(other_work),
                "message": {"content": [{"type": "text", "text": "a"}]},
            }
        )
        + "\n"
    )
    config = cb.ClaudeConfig(
        binary=str(FAKE),
        accounts={"test": str(home["account"]), "alt": str(alt)},
        default_account="test",
        defaults=SessionSettings(model="haiku", approval="bypassPermissions"),
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()
    found = await backend.lookup_session(sid)
    assert found is not None and found.cwd == str(other_work) and found.settings.account == "alt"
    assert found.preview == "q" and found.last_turn is not None and found.updated_at is not None
    assert sid not in backend._sessions  # looked up, not opened
    assert await backend.lookup_session("44444444-4444-4444-8444-444444444444") is None
    assert await backend.lookup_session("../../etc/passwd") is None  # never a path component
    # a session this process holds is reported from memory, busy state included
    mine = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    live = await backend.lookup_session(mine)
    assert live is not None and live.native_id == mine and live.active_turn_id is None


async def test_recovery_reconstructs_a_persisted_session_without_a_transcript(home):
    """Codex round 1 on PR #68: create_session persists a row before any transcript exists; a
    fresh backend (restart) must still open it and run its first turn."""
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    spec = SessionSpec(cwd=str(home["work"]), name="t", context_prompt="CTX")
    sid = (await backend.create_session(spec)).native_id
    await backend.close()
    fresh = make_backend(home)  # the bridge restarted before the first turn
    await fresh.start()
    await fresh._events.get()
    info = await fresh.open_session(sid, spec)
    assert info.native_id == sid and info.active_turn_id is None and info.last_turn is None
    await fresh.start_turn(sid, UserInput(text="first", client_id="c1"))
    events = await drain(fresh, extra=1)
    assert events[-2].status is TurnStatus.COMPLETED
    assert "--session-id" in calls(home)[-1]["argv"]  # a first turn, not a resume


async def test_cancelled_doctor_capture_reaps_process(monkeypatch):
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
    task = asyncio.create_task(cb._run_capture(["fake-claude", "auth", "status"], timeout_s=30))
    await communicating.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(proc.pid, cb.signal.SIGKILL)]
    proc.wait.assert_awaited_once()


async def test_turns_and_model_checks_run_without_claudeai_connectors(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    await backend.update_settings(
        info.native_id, SessionSettings(model="opus", approval="bypassPermissions")
    )  # a model check
    await backend.start_turn(info.native_id, UserInput(text="go", client_id="c1"))
    await drain(backend)
    values = [c["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] for c in calls(home)]
    assert len(values) == 2 and set(values) == {"false"}
    backend.config.claudeai_connectors = True
    await backend.start_turn(info.native_id, UserInput(text="again", client_id="c2"))
    await drain(backend)
    assert calls(home)[-1]["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] is None


# ------------------------------------------------- the agent's commit identity (issue #214)


def _git(cwd, *args: str) -> str:
    res = subprocess.run(  # noqa: S603 - a fixed argument list, test helper
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


async def test_a_turn_commits_as_the_agent_and_off_leaves_the_users_git_config(home, monkeypatch):
    from zab.core.types import GitIdentity

    work = home["work"]
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "Alice Participant")
    _git(work, "config", "user.email", "alice@example.org")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_GIT_COMMIT", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Inherited")  # the bridge's own, never the turn's
    who = GitIdentity("Claude · alice", "claude-alice-bot@example.zulipchat.com")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    last = ("log", "-1", "--format=%an <%ae> | %cn <%ce>")

    info = await backend.create_session(SessionSpec(cwd=str(work), name="t", git_identity=who))
    await backend.start_turn(info.native_id, UserInput(text="commit", client_id="c1"))
    await drain(backend)
    agent = "Claude · alice <claude-alice-bot@example.zulipchat.com>"
    assert _git(work, *last) == f"{agent} | {agent}"
    assert calls(home)[-1]["env"]["GIT_COMMITTER_NAME"] == "Claude · alice"
    assert calls(home)[-1]["argv"][-2:] == ["--settings", cb.NO_COMMIT_ATTRIBUTION]  # one author
    # reopened after a restart without an identity ("off"): the participant's config again
    await backend.open_session(info.native_id, SessionSpec(cwd=str(work), name="t"))
    await backend.start_turn(info.native_id, UserInput(text="again", client_id="c2"))
    await drain(backend)
    alice = "Alice Participant <alice@example.org>"
    assert _git(work, *last) == f"{alice} | {alice}"
    assert calls(home)[-1]["env"]["GIT_AUTHOR_NAME"] is None
    assert "--settings" not in calls(home)[-1]["argv"]  # "off": the CLI's own default


async def test_a_turn_carries_zab_bot_from_the_spec(home, monkeypatch):
    """Issue #248: every turn names its bot in ZAB_BOT, the one the spec gives at create and
    open; the bridge's own ZAB_BOT never leaks into a turn."""
    monkeypatch.setenv("ZAB_BOT", "the-bridge-itself")
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    work = str(home["work"])
    info = await backend.create_session(SessionSpec(cwd=work, name="t", bot="claude-alice"))
    await backend.start_turn(info.native_id, UserInput(text="hi", client_id="z1"))
    await drain(backend)
    assert calls(home)[-1]["env"]["ZAB_BOT"] == "claude-alice"
    await backend.open_session(info.native_id, SessionSpec(cwd=work, name="t"))
    await backend.start_turn(info.native_id, UserInput(text="again", client_id="z2"))
    await drain(backend)
    assert calls(home)[-1]["env"]["ZAB_BOT"] is None


# ----------------------------------- the opt-in raw-stream log (PM on issue #36, Wed 14:45)


async def run_logged_turn(home, monkeypatch, tmp_path, mode: str | None):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    if mode is None:
        monkeypatch.delenv(cb.RAW_LOG_ENV, raising=False)
    else:
        monkeypatch.setenv(cb.RAW_LOG_ENV, mode)
    monkeypatch.setattr(cb, "RAW_LOG_DIR", str(tmp_path / "raw"))
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    turn_id = await backend.start_turn(sid, UserInput(text="go", client_id="c1"))
    await drain(backend, extra=1)
    await backend.close()
    return turn_id, tmp_path / "raw"


async def test_the_raw_log_records_shapes_not_content(home, monkeypatch, tmp_path):
    turn_id, raw = await run_logged_turn(home, monkeypatch, tmp_path, "1")
    files = list(raw.iterdir())
    assert [f.name for f in files] == [turn_id.replace(":", "_") + ".jsonl"]
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert oct(raw.stat().st_mode & 0o777) == "0o700"
    records = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert records[0]["turn"] == turn_id
    types = [r.get("type") for r in records if r.get("stream") == "stdout"]
    assert "assistant" in types and types[-1] == "result"
    result = next(r for r in records if r.get("type") == "result")
    assert result["result_chars"] > 0 and result["num_turns"] >= 1
    assistant = [r for r in records if r.get("type") == "assistant"]
    assert any(b[0] == "tool_use" and b[1] > 0 for r in assistant for b in r["blocks"])
    assert records[-1] == {"completed": "completed", "final_text_chars": result["result_chars"]}
    text = files[0].read_text()
    assert "hello.txt" not in text and '"line"' not in text  # no content in meta mode


async def test_the_raw_log_with_content_and_off(home, monkeypatch, tmp_path):
    _turn, raw = await run_logged_turn(home, monkeypatch, tmp_path / "a", "content")
    text = next(raw.iterdir()).read_text()
    assert '"line"' in text and "hello.txt" in text
    _turn, raw = await run_logged_turn(home, monkeypatch, tmp_path / "b", None)
    assert not raw.exists()  # off by default
    assert cb.raw_log_mode({}) is None and cb.raw_log_mode({cb.RAW_LOG_ENV: "0"}) is None
    assert cb.raw_log_mode({cb.RAW_LOG_ENV: "yes"}) == "meta"


def test_a_raw_log_that_cannot_be_written_never_fails_the_turn(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    log = cb.RawLog("s:t", "meta", str(blocker / "raw"))
    log("stdout", '{"type": "result", "result": "x"}', 0.1)
    log("stdout", '{"type": "result", "result": "x"}', 0.2)  # off after the first failure
    assert caplog.text.count("claude raw log") == 1
    assert cb.describe_raw_line("stdout", "not json") == {"stream": "stdout", "bytes": 8}


@pytest.mark.parametrize(
    ("stderr", "hidden"),
    [
        ("e" * 190 + "\nkey=" + "Q7xK" + "a1B2" * 7 + "\n", "Q7xK"),  # crosses 200
        ("e" * 190 + " ghp_" + "Z9y8X7w6" * 4 + "V5u4\n", "ghp_"),
    ],
)
async def test_a_failed_commands_stderr_is_redacted_before_it_is_cut(stderr, hidden):
    """#661: `_run_capture`'s error keeps 200 characters of stderr; a secret crossing the cut
    would keep too little for its rule (SPEC §11.6), so it is redacted first."""
    script = "import os, sys; sys.stderr.write(os.environ['DECOY']); sys.exit(3)"  # argv is shown
    with pytest.raises(OSError, match="exited 3") as caught:
        await cb._run_capture(
            [sys.executable, "-c", script], timeout_s=30, env={**os.environ, "DECOY": stderr}
        )
    assert hidden not in str(caught.value) and "[redacted" in str(caught.value)
