"""The Claude side question (`!btw`, issue #716, SPEC §9.10): one `claude -p` process on a
copy of the conversation that is never saved, with no tools, MCP servers or hooks; the session's
transcript, settings and running turn are untouched, and no event reaches the core."""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from backends.claude_backend_helpers import FIXTURES, calls, drain, make_backend
from zab.backends import claude as cb
from zab.core.types import SessionSpec, TurnCompleted, UserInput

pytestmark = pytest.mark.contract
QUESTION = "what was the codeword?"


def read(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def started(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    return backend


async def session_with_a_turn(backend, home) -> str:
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="topic"))
    await backend.start_turn(info.native_id, UserInput(text="remember orchid", client_id="1"))
    await drain(backend, extra=1)  # and its UsageUpdated
    return info.native_id


def side_calls(home) -> list[dict]:
    if not home["log"].exists():
        return []
    return [c for c in calls(home) if "--no-session-persistence" in c.get("argv", [])]


def test_every_flag_of_the_side_question_is_in_the_pinned_help():
    """`claude --help` of the pinned 2.1.283, recorded (`fixtures/claude/2.1.283/help.txt`)."""
    help_text = " ".join((FIXTURES / "help.txt").read_text().split())  # unwrapped
    offered = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", help_text))
    argv = cb.build_side_argv(
        cb.ClaudeConfig(), cb.SessionSettings(model="opus", effort="high"), "s-id"
    )
    flags = {a for a in argv if a.startswith("--")}
    assert flags <= offered, flags - offered
    assert argv[argv.index("--tools") + 1] == ""  # `""` disables every built-in tool (help)
    assert '"" to disable all tools' in help_text
    assert "Only use MCP servers from --mcp-config" in help_text
    assert "--mcp-config" not in argv  # so no MCP server at all
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    for flag in ("--permission-mode", "--session-id", "--input-format", "--append-system-prompt"):
        assert flag not in argv


async def test_a_side_question_answers_from_the_session_and_saves_nothing(home):
    backend = await started(home)
    sid = await session_with_a_turn(backend, home)
    transcript = cb.find_transcript(str(home["account"]), sid, str(home["work"]))
    before = read(transcript)
    projects = sorted(os.listdir(home["account"] / "projects"))
    settings = backend._sessions[sid].settings

    answer = await backend.side_question(sid, QUESTION, timeout_s=30)
    assert answer == "pong"  # the fixture's result
    (call,) = side_calls(home)
    argv = call["argv"]
    assert argv[argv.index("--resume") + 1] == sid  # the session itself: nothing copied
    assert call["stdin"] == cb.SIDE_QUESTION_FRAME + QUESTION
    assert call["cwd"] == str(home["work"])
    assert call["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"  # connectors off
    assert call["env"]["ZAB_BOT"] is None and call["env"]["GIT_AUTHOR_NAME"] is None
    assert argv[argv.index("--model") + 1] == "haiku"  # the session's model
    assert read(transcript) == before  # never saved
    assert sorted(os.listdir(home["account"] / "projects")) == projects
    assert backend._sessions[sid].settings == settings
    assert backend._events.empty()  # no event for the core
    assert not backend._side_questions


async def test_a_side_question_while_a_turn_runs_leaves_it_running(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    backend = await started(home)
    sid = await session_with_a_turn(backend, home)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")  # the next turn runs until stopped
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_SIDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    await backend.start_turn(sid, UserInput(text="long work", client_id="2"))
    session = backend._sessions[sid]
    async with asyncio.timeout(10):
        while session.turn is None:  # noqa: ASYNC110 - the turn's process starts
            await asyncio.sleep(0.02)
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_HANG")  # read by the side question's process

    assert await backend.side_question(sid, QUESTION, timeout_s=30) == "pong"
    argv = side_calls(home)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == sid  # the live session, its progress included
    assert session.busy and session.turn is not None and session.turn.group_alive()
    assert not any(isinstance(e, TurnCompleted) for e in list(backend._events._queue))
    await backend.interrupt(sid)
    await drain(backend)


async def test_a_session_with_no_completed_turn_is_refused(home, monkeypatch):
    backend = await started(home)
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="topic"))
    with pytest.raises(ValueError, match="no completed turn"):
        await backend.side_question(info.native_id, QUESTION, timeout_s=30)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    await backend.start_turn(info.native_id, UserInput(text="first", client_id="1"))
    session = backend._sessions[info.native_id]
    async with asyncio.timeout(10):
        while session.turn is None:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    with pytest.raises(ValueError, match="no completed turn"):  # its first turn still runs
        await backend.side_question(info.native_id, QUESTION, timeout_s=30)
    assert not side_calls(home)
    await backend.interrupt(info.native_id)
    await drain(backend)


async def test_a_side_question_that_times_out_is_stopped(home, monkeypatch):
    backend = await started(home)
    sid = await session_with_a_turn(backend, home)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    with pytest.raises(TimeoutError):
        await backend.side_question(sid, QUESTION, timeout_s=1)
    assert not backend._side_questions  # the process group is gone, the task reaped
    assert not backend._sessions[sid].busy


async def test_the_last_non_empty_result_is_the_answer(home, monkeypatch):
    """Resuming a session whose turn ran a background command: the CLI's task notification
    and an empty `result` come first, then the answer in a second one (recorded on 2.1.283)."""
    backend = await started(home)
    sid = await session_with_a_turn(backend, home)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_SIDE_FIXTURE", str(FIXTURES / "background_resume.jsonl"))
    answer = await backend.side_question(sid, QUESTION, timeout_s=30)
    assert answer.startswith("I started a background shell command that sleeps for 4 minutes")


async def test_a_failed_result_is_one_line(home, monkeypatch):
    backend = await started(home)
    sid = await session_with_a_turn(backend, home)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_SIDE_FIXTURE", str(FIXTURES / "error.jsonl"))
    with pytest.raises(ValueError, match=r"^Claude couldn't answer \(other\): There's an issue"):
        await backend.side_question(sid, QUESTION, timeout_s=30)


async def test_a_fork_whose_first_turn_never_ran_is_asked_from_its_copy(home):
    backend = await started(home)
    sid = await session_with_a_turn(backend, home)
    child_cwd = home["work"] / "child"
    child_cwd.mkdir()
    child = await backend.fork_session(sid, SessionSpec(cwd=str(child_cwd), name="topic ⑂ 1"))
    assert await backend.side_question(child.native_id, QUESTION, timeout_s=30) == "pong"
    argv = side_calls(home)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == cb.fork_copy_id(child.native_id)
    copy = cb.fork_copy_path(str(home["account"]), child.native_id)
    assert await asyncio.to_thread(os.path.exists, copy)  # kept for the fork's first turn
