"""Live check of the Claude fork (issue #202, SPEC §8.8): the cut copy with the parent worktree's
paths rewritten to the child's, resumed with `--fork-session --session-id`.

    ZAB_LIVE=claude uv run pytest -q -s tests/live/test_claude_fork_live.py
    ZAB_LIVE=claude ZAB_CLAUDE_BINARY=/path/to/claude-2.1.283 uv run pytest ...   # the AMI pin

No Zulip: the backend is driven directly. The Claude account is ~/.claude2, never the session's own
~/.claude (the development machine's rule). Both worktrees are scratch directories under pytest's
tmp_path.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

import pytest

from zab.backends import claude as cb
from zab.core.types import SessionSettings, SessionSpec, TurnCompleted, TurnStatus, UserInput

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "claude", reason="Claude opt-in required"),
]
ACCOUNT = Path.home() / ".claude2"


async def turn(backend: cb.ClaudeBackend, session: str, text: str) -> TurnCompleted:
    await backend.start_turn(session, UserInput(text=text, client_id=secrets.token_hex(4)))
    async with asyncio.timeout(300):
        async for ev in backend.events():
            if isinstance(ev, TurnCompleted) and ev.session_id == session:
                return ev
    raise AssertionError("event stream ended")


def read(path: Path | str) -> str:  # sync helper (ruff ASYNC230)
    with open(path) as f:
        return f.read()


async def test_fork_works_in_its_own_worktree_and_keeps_the_history(tmp_path):
    assert ACCOUNT.is_dir() and ACCOUNT != Path.home() / ".claude"
    base = tmp_path / "proj" / ".zab" / "worktrees"
    parent_cwd, child_cwd = base / "topic-aaaaaa", base / "fork-bbbbbb"
    for cwd in (parent_cwd, child_cwd):  # the child starts from the parent's tree (#226)
        cwd.mkdir(parents=True)
        (cwd / "notes.txt").write_text("alpha line\nbeta line\n")
    codeword = "HELIOTROPE-" + secrets.token_hex(3).upper()
    later = "LATER-" + secrets.token_hex(3).upper()

    backend = cb.ClaudeBackend(
        "claude",
        cb.ClaudeConfig(
            binary=os.getenv("ZAB_CLAUDE_BINARY", "claude"),
            accounts={"test": str(ACCOUNT)},
            default_account="test",
            defaults=SessionSettings(model="haiku", approval="bypassPermissions"),
        ),
    )
    await backend.start()
    try:
        parent = await backend.create_session(SessionSpec(cwd=str(parent_cwd), name="fork-live"))
        done = await turn(
            backend,
            parent.native_id,
            f"Remember the codeword {codeword}. Use the Read tool on notes.txt in the current "
            "directory, then reply with its first line only.",
        )
        assert done.status == TurnStatus.COMPLETED, done
        parent_file = cb.find_transcript(str(ACCOUNT), parent.native_id, str(parent_cwd))
        before = read(parent_file)

        child = await backend.fork_session(
            parent.native_id, SessionSpec(cwd=str(child_cwd), name="fork-live ⑂ 1")
        )
        copy = cb.fork_copy_path(str(ACCOUNT), child.native_id)
        assert str(parent_cwd) not in read(copy)  # every path now points at the child
        # A parent edit after the fork must not reach the child (edited_text_file, DECISIONS).
        (parent_cwd / "notes.txt").write_text(f"alpha line\nbeta line\n{later}\n")

        done = await turn(
            backend,
            child.native_id,
            "Use the Edit tool to change 'alpha line' to 'child edit' in the notes.txt you read "
            "earlier. Then reply with the codeword I gave you earlier, and nothing else.",
        )
        assert done.status == TurnStatus.COMPLETED, done
        assert codeword in (done.final_text or "")  # the child has the parent's history
        assert read(child_cwd / "notes.txt").startswith("child edit\n")  # its own worktree
        assert read(parent_cwd / "notes.txt") == f"alpha line\nbeta line\n{later}\n"
        mine = cb.find_transcript(str(ACCOUNT), child.native_id, str(child_cwd))
        assert mine and later not in read(mine)  # the parent's later edit was not attached
        assert read(parent_file) == before  # the parent's transcript is untouched
        assert not await asyncio.to_thread(os.path.exists, copy)  # the child has its own now
        print(f"fork live OK on {os.getenv('ZAB_CLAUDE_BINARY', 'claude')}: {child.native_id}")
    finally:
        await backend.close()
