"""Live check of the agent's commits (issue #214): one author, no Claude Code co-author trailer.

    ZAB_LIVE=claude uv run pytest -q -s tests/live/test_claude_commit_identity_live.py
    ZAB_LIVE=claude ZAB_CLAUDE_BINARY=/path/to/claude-2.1.283 uv run pytest ...   # the AMI pin

No Zulip: the backend is driven directly, in a scratch repository under pytest's tmp_path. The
Claude account is ~/.claude2, never the session's own ~/.claude (the development machine's
rule for live tests).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from zab.backends import claude as cb
from zab.core.types import (
    GitIdentity,
    SessionSettings,
    SessionSpec,
    TurnCompleted,
    TurnStatus,
    UserInput,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "claude", reason="Claude opt-in required"),
]
ACCOUNT = Path.home() / ".claude2"
PROMPT = (
    "Create a file notes.txt containing the word hello, stage it, and commit it, following your "
    "usual commit conventions."
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - a fixed argument list, test helper
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout


async def test_a_commit_has_the_agent_as_its_only_author(tmp_path):
    assert ACCOUNT.is_dir() and ACCOUNT != Path.home() / ".claude"
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=Owner", "-c", "user.email=o@example.org", "commit", "-q",
        "--allow-empty", "-m", "init")  # fmt: skip
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
        who = GitIdentity("Claude · test", "claude-test-bot@example.zulipchat.com")
        info = await backend.create_session(
            SessionSpec(cwd=str(repo), name="identity", git_identity=who)
        )
        await backend.start_turn(info.native_id, UserInput(text=PROMPT, client_id="i1"))
        async with asyncio.timeout(300):
            async for ev in backend.events():
                if isinstance(ev, TurnCompleted) and ev.session_id == info.native_id:
                    break
        assert ev.status == TurnStatus.COMPLETED, ev
        assert git(repo, "rev-list", "--count", "HEAD").strip() == "2", "the agent didn't commit"
        author = git(repo, "log", "-1", "--format=%an <%ae> | %cn <%ce>").strip()
        assert author == f"{who.name} <{who.email}> | {who.name} <{who.email}>"
        message = git(repo, "log", "-1", "--format=%B")
        assert "Co-Authored-By" not in message and "noreply@anthropic.com" not in message
        assert "Generated with" not in message
        print(f"commit identity live OK on {os.getenv('ZAB_CLAUDE_BINARY', 'claude')}: {author}")
    finally:
        await backend.close()
