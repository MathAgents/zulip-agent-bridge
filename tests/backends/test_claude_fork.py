"""The Claude fork (issue #202): a cut copy of the parent's transcript, the child's first turn
with `--fork-session --session-id`, and nothing changed for the parent."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from backends.claude_backend_helpers import FIXTURES, calls, drain, make_backend
from zab.backends import claude as cb
from zab.core.types import SessionSpec, TurnStarted, UserInput


def worktrees(home):
    base = home["work"] / ".zab" / "worktrees"
    parent, child = base / "topic-aaaaaa", base / "fork-bbbbbb"
    parent.mkdir(parents=True)
    child.mkdir(parents=True)
    return str(parent), str(child)


def lines(path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def read(path) -> bytes:  # sync helpers: the async tests call them (ruff ASYNC230/240)
    with open(path, "rb") as f:
        return f.read()


def append(path, text: str) -> None:
    with open(path, "a") as f:
        f.write(text)


async def started(home):
    backend = make_backend(home)
    await backend.start()
    await backend._events.get()
    return backend


async def parent_with_one_turn(backend, cwd):
    info = await backend.create_session(SessionSpec(cwd=cwd, name="topic"))
    await backend.start_turn(info.native_id, UserInput(text=f"read {cwd}/notes.txt", client_id="1"))
    await drain(backend)
    return info.native_id


async def test_a_fork_of_an_idle_parent_starts_from_its_history(home):
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    account = str(home["account"])
    parent_file = cb.find_transcript(account, parent, parent_cwd)
    before = read(parent_file)

    child = await backend.fork_session(parent, SessionSpec(cwd=child_cwd, name="topic ⑂ 1"))
    assert child.native_id != parent and child.cwd == child_cwd
    copy = cb.fork_copy_path(account, child.native_id)
    copied = lines(copy)
    assert copied[0]["message"]["content"] == f"read {child_cwd}/notes.txt"  # paths rewritten
    assert copied[0]["cwd"] == child_cwd
    assert read(parent_file) == before  # the parent's transcript is untouched

    await backend.start_turn(child.native_id, UserInput(text="side task", client_id="2"))
    await drain(backend)
    argv = calls(home)[-1]["argv"]
    i = argv.index("--fork-session")
    assert argv[i - 2 : i + 3] == [
        "--resume",
        cb.fork_copy_id(child.native_id),
        "--fork-session",
        "--session-id",
        child.native_id,
    ]
    assert calls(home)[-1]["cwd"] == child_cwd
    mine = cb.find_transcript(account, child.native_id, child_cwd)
    assert [r["message"]["content"] for r in lines(mine) if r["type"] == "user"] == [
        f"read {child_cwd}/notes.txt",
        "side task",
    ]
    assert not await asyncio.to_thread(os.path.exists, copy)  # the child has its own now
    await backend.start_turn(child.native_id, UserInput(text="more", client_id="3"))
    await drain(backend)
    argv = calls(home)[-1]["argv"]
    assert "--fork-session" not in argv and argv[argv.index("--resume") + 1] == child.native_id
    assert read(parent_file) == before


async def test_a_fork_during_a_running_turn_stops_at_the_last_completed_one(home, monkeypatch):
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    parent_file = cb.find_transcript(str(home["account"]), parent, parent_cwd)
    completed = read(parent_file).count(b"\n")
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "tool_use.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    await backend.start_turn(parent, UserInput(text="long job", client_id="4"))
    await drain(backend, until=TurnStarted)
    # what the running turn has written so far, the last line still being written
    started_line = json.dumps({"type": "user", "message": {"content": "long job"}}) + "\n"
    append(parent_file, started_line + '{"type": "assistant", "message": {"content": "half a li')

    child = await backend.fork_session(parent, SessionSpec(cwd=child_cwd, name="f"))
    copy = cb.fork_copy_path(str(home["account"]), child.native_id)
    assert len(lines(copy)) == completed  # exactly the completed turn (paths rewritten)
    assert all(r["message"]["content"] != "long job" for r in lines(copy))
    await backend.interrupt(parent)
    await drain(backend)


async def test_a_fork_needs_an_open_session_with_a_completed_turn(home):
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    with pytest.raises(ValueError, match="isn't open"):
        await backend.fork_session(cb.new_session_id(), SessionSpec(cwd=child_cwd, name="f"))
    fresh = await backend.create_session(SessionSpec(cwd=parent_cwd, name="t"))
    with pytest.raises(ValueError, match="no completed turn"):
        await backend.fork_session(fresh.native_id, SessionSpec(cwd=child_cwd, name="f"))
    backend._sessions[fresh.native_id].busy = True  # still nothing completed
    with pytest.raises(ValueError, match="no completed turn"):
        await backend.fork_session(fresh.native_id, SessionSpec(cwd=child_cwd, name="f"))


async def test_a_turn_starting_during_a_fork_stays_out_of_it(home, monkeypatch):
    """Codex round 3 on #227: the boundary is taken with no await before the size, so a turn
    that starts while the transcript is being looked up can't slip into the copy."""
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    parent_file = cb.find_transcript(str(home["account"]), parent, parent_cwd)
    completed = read(parent_file).count(b"\n")
    real_find = cb.find_transcript

    def find_then_a_turn_starts(*args):
        found = real_find(*args)
        backend._sessions[parent].busy = True  # start_turn ran meanwhile...
        backend._sessions[parent].turn_start_size = len(read(found))  # ...recorded its start
        append(found, json.dumps({"type": "user", "message": {"content": "new turn"}}) + "\n")
        return found

    monkeypatch.setattr(cb, "find_transcript", find_then_a_turn_starts)
    child = await backend.fork_session(parent, SessionSpec(cwd=child_cwd, name="f"))
    copy = cb.fork_copy_path(str(home["account"]), child.native_id)
    assert len(lines(copy)) == completed
    assert all(r["message"]["content"] != "new turn" for r in lines(copy))


async def test_a_fork_survives_a_restart_before_its_first_turn(home):
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    child = await backend.fork_session(parent, SessionSpec(cwd=child_cwd, name="f"))
    await backend.close()

    again = await started(home)
    await again.open_session(child.native_id, SessionSpec(cwd=child_cwd, name="f"))
    await again.start_turn(child.native_id, UserInput(text="side task", client_id="5"))
    await drain(again)
    argv = calls(home)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == cb.fork_copy_id(child.native_id)
    assert "--fork-session" in argv


def test_write_fork_copy_rewrites_whole_path_components_only(tmp_path):
    src = tmp_path / "t.jsonl"
    old, new = "/w/.zab/worktrees/a-111111", "/w/.zab/worktrees/b-222222"
    src.write_text(
        json.dumps({"a": f"{old}/x.py", "b": old, "c": f"{old}1/y", "d": f"cd {old} && ls"})
        + "\n"
        + '{"partial": "li'
    )
    dest = tmp_path / "out" / "c.jsonl"
    assert cb.write_fork_copy(str(src), None, old, new, str(dest))
    (record,) = lines(dest)  # the partial line is cut
    assert record == {"a": f"{new}/x.py", "b": new, "c": f"{old}1/y", "d": f"cd {new} && ls"}
    assert oct(os.stat(dest).st_mode & 0o777) == "0o600"
    assert not cb.write_fork_copy(str(src), 3, old, new, str(tmp_path / "empty.jsonl"))


@pytest.mark.parametrize("dirname", ["research+notes", "proof (draft)", "théorème"])
def test_paths_with_plus_parentheses_or_accents_are_rewritten(tmp_path, dirname):
    """Codex on #227: skipping these left the fork reading and editing the parent's files."""
    old = f"/work/{dirname}/.zab/worktrees/parent-111111"
    new = f"/work/{dirname}/.zab/worktrees/fork-222222"
    src = tmp_path / "t.jsonl"
    record = {"cwd": old, "input": {"file_path": f"{old}/A.lean"}, "other": f"{old}é/x"}
    src.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    dest = tmp_path / "c.jsonl"
    assert cb.write_fork_copy(str(src), None, old, new, str(dest))
    (got,) = lines(dest)
    assert got == {"cwd": new, "input": {"file_path": f"{new}/A.lean"}, "other": f"{old}é/x"}


def test_only_the_whole_root_is_rewritten(tmp_path):
    """Codex round 2 on #227: a sibling `<root>+backup` and a longer `/mnt<root>` stay."""
    old, new = "/w/parent-111111", "/w/fork-222222"
    record = {
        "exact": old,
        "file": f"{old}/A.lean",
        "cmd": f"cd {old} && lake build",
        "prose": f"(see {old}) and `{old}/B.lean`, then\n{old}/C.lean:3",
        "sibling": f"{old}+backup/A.lean",
        "longer": f"/mnt{old}/A.lean",
        "suffix": f"{old}x/A.lean",
    }
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps(record) + "\n")
    dest = tmp_path / "c.jsonl"
    assert cb.write_fork_copy(str(src), None, old, new, str(dest))
    (got,) = lines(dest)
    assert got == {
        "exact": new,
        "file": f"{new}/A.lean",
        "cmd": f"cd {new} && lake build",
        "prose": f"(see {new}) and `{new}/B.lean`, then\n{new}/C.lean:3",
        "sibling": f"{old}+backup/A.lean",
        "longer": f"/mnt{old}/A.lean",
        "suffix": f"{old}x/A.lean",
    }


def test_a_sibling_path_field_is_left_alone_and_any_character_is_fine(tmp_path):
    """Codex round 3 on #227: `<root> backup/A.lean` as a field is a sibling, not the root. And
    since each line is decoded first, a quote or a backslash in the root needs no care."""
    old, new = '/w/say "hi"\\x/parent-111111', '/w/say "hi"\\x/fork-222222'
    record = {
        "file_path": f"{old}/A.lean",
        "sibling": f"{old} backup/A.lean",
        "cmd": f"cd {old} && ls {old}/B.lean",
        "begins": f"{old}/run.sh {old}/data",
    }
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps(record) + "\n")
    dest = tmp_path / "c.jsonl"
    assert cb.write_fork_copy(str(src), None, old, new, str(dest))
    (got,) = lines(dest)
    assert got == {
        "file_path": f"{new}/A.lean",
        "sibling": f"{old} backup/A.lean",
        "cmd": f"cd {new} && ls {new}/B.lean",
        "begins": f"{new}/run.sh {new}/data",
    }


def test_the_copy_lives_in_one_short_directory(tmp_path):
    """Codex round 3 on #227: a long cwd's own directory name can pass 255 bytes."""
    child = cb.new_session_id()
    path = cb.fork_copy_path("/acct", child)
    assert path == f"/acct/projects/{cb.FORK_COPY_DIR}/{cb.fork_copy_id(child)}.jsonl"


def test_fork_roots_drop_a_common_subdir():
    assert cb.fork_roots("/w/a-1/pkg/sub", "/w/b-2/pkg/sub") == ("/w/a-1", "/w/b-2")
    assert cb.fork_roots("/w/a-1", "/w/b-2") == ("/w/a-1", "/w/b-2")


def test_build_argv_for_a_forks_first_turn():
    argv = cb.build_argv(
        cb.ClaudeConfig(), cb.SessionSettings(), session_id="X", resume=False, fork_from="Q"
    )
    assert argv[-5:] == ["--resume", "Q", "--fork-session", "--session-id", "X"]
    assert "--fork-session" not in cb.build_argv(
        cb.ClaudeConfig(), cb.SessionSettings(), session_id="X", resume=True
    )


def test_fork_copy_ids_are_stable_and_distinct():
    a, b = cb.new_session_id(), cb.new_session_id()
    assert cb.fork_copy_id(a) == cb.fork_copy_id(a) != cb.fork_copy_id(b) != a


async def test_fork_copies_are_neither_listed_nor_attachable(home):
    """Codex round 4 on #227: a copy is a fork's frozen source, not a session; attaching one
    would let its history change, and the copy is deleted later anyway."""
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    child = await backend.fork_session(parent, SessionSpec(cwd=child_cwd, name="f"))
    account = str(home["account"])
    copy_id = cb.fork_copy_id(child.native_id)
    assert cb.find_transcript(account, copy_id) is None
    listed = {s.native_id for s in await backend.list_sessions(None, 50)}
    assert parent in listed and copy_id not in listed


async def test_a_fork_keeps_the_parents_cleared_settings(home):
    """Codex round 5 on #227: backend defaults must not refill what the parent cleared (an
    effort dropped on a switch to Haiku, say)."""
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    backend.config.defaults = cb.SessionSettings(model="opus", effort="max", approval="plan")
    inherited = cb.SessionSettings(model="haiku", effort=None, approval="bypassPermissions")
    child = await backend.fork_session(
        parent, SessionSpec(cwd=child_cwd, name="f", settings=inherited)
    )
    settings = backend._sessions[child.native_id].settings
    assert (settings.model, settings.effort, settings.approval) == (
        "haiku",
        None,
        "bypassPermissions",
    )
    assert settings.account == "test"  # the default account when the spec names none


async def test_a_fork_given_only_overrides_keeps_the_parents_defaults(home):
    """The live check on #227: the core persists explicit overrides only (types.py), so a
    fork's spec may name no approval at all. The child must still run as the parent does,
    `bypassPermissions` included, or its edits are refused; an explicit value still wins."""
    backend = await started(home)  # defaults: haiku, bypassPermissions
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    child = await backend.fork_session(parent, SessionSpec(cwd=child_cwd, name="f"))
    await backend.start_turn(child.native_id, UserInput(text="side task", client_id="7"))
    await drain(backend)
    argv = calls(home)[-1]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--model") + 1] == "haiku"
    other = await backend.fork_session(
        parent,
        SessionSpec(cwd=child_cwd, name="g", settings=cb.SessionSettings(model="sonnet")),
    )
    settings = backend._sessions[other.native_id].settings
    assert (settings.model, settings.approval) == ("sonnet", "bypassPermissions")


async def test_a_fork_commits_as_the_agent(home):
    """Review on #227: the child session takes the spec's git identity (#214), so a fork's
    turns never fall back to the participant's git config."""
    from zab.core.types import GitIdentity

    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    who = GitIdentity("Claude · alice", "claude-alice-bot@example.zulipchat.com")
    child = await backend.fork_session(
        parent, SessionSpec(cwd=child_cwd, name="f", git_identity=who)
    )
    await backend.start_turn(child.native_id, UserInput(text="side task", client_id="6"))
    await drain(backend)
    env = calls(home)[-1]["env"]
    assert (env["GIT_AUTHOR_NAME"], env["GIT_COMMITTER_EMAIL"]) == (
        "Claude · alice",
        "claude-alice-bot@example.zulipchat.com",
    )


async def test_a_fork_names_its_bot(home):
    """The child session takes the spec's bot (#248), so `zab history` in a fork's turns reads
    as this bot, like every created or opened session's."""
    backend = await started(home)
    parent_cwd, child_cwd = worktrees(home)
    parent = await parent_with_one_turn(backend, parent_cwd)
    child = await backend.fork_session(
        parent, SessionSpec(cwd=child_cwd, name="f", bot="claude-alice")
    )
    await backend.start_turn(child.native_id, UserInput(text="side task", client_id="7"))
    await drain(backend)
    assert calls(home)[-1]["env"]["ZAB_BOT"] == "claude-alice"
