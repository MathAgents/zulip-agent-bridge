"""AT-20 (handover) and AT-22 (secrets through `!file`) against the fake Zulip (SPEC §17).

Driven through `BotBridge`, the way messages really arrive, so the command routing of the core
is exercised (codex round 1 on #67). Written ahead of the core's M3 commands (issue #35): the
tests that need `!handover` and `!file` are a strict expected failure until the core handles
those commands, and turn into ordinary tests the moment it does, with no edit needed here. AT-22
runs against a temporary HOME holding decoy files only (CONTRIBUTING.md, rule 2): never with real
credentials present.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core import session as core_session
from zab.core import types as t
from zab.core.bridge import BotBridge
from zab.core.links import WorkspaceLinks
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager
from zab.zulip.client import ZulipClient

pytestmark = pytest.mark.integration


def _core_source() -> str:
    """The command handlers may live in zab.core.session or in a zab.core.commands module."""
    text = inspect.getsource(core_session)
    try:
        from zab.core import commands as core_commands  # optional module
    except ImportError:
        return text
    return text + inspect.getsource(core_commands)


M3_COMMANDS_LANDED = all(f'"{verb}"' in _core_source() for verb in ("!handover", "!file"))
needs_m3 = pytest.mark.xfail(
    condition=not M3_COMMANDS_LANDED,
    strict=True,
    reason="AT-20/AT-22 need the core's !handover and !file (issue #35)",
)

HANDOVER_FIRST = "Update HANDOVER.md at the repository root"
HANDOVER_LAST = "the full commit hash\n(`git rev-parse HEAD`) and the path `HANDOVER.md`."
DECOY_KEY = "sk-ant-api03-" + "A" * 40 + "-fake"


async def git(ws: LocalWorkspace, *args: str) -> str:
    res = await ws.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args])
    assert res.exit_code == 0, res.stderr
    return res.stdout.strip()


def _write_repo_files(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "HANDOVER.md").write_text("# Handover\nGoal: nothing yet.\n")
    (root / ".env").write_text("SECRET=decoy\n")
    (root / "notes.md").write_text("# notes\n")


async def repo_with_pushed_handover(root: Path) -> tuple[LocalWorkspace, str]:
    """A checkout whose HANDOVER.md is committed and counts as pushed to a GitHub remote: the
    remote is a URL only, the upstream ref is written by hand (no network, as in test_links)."""
    _write_repo_files(root)
    ws = LocalWorkspace(str(root))
    await git(ws, "init", "-q", "-b", "main")
    await git(ws, "add", "HANDOVER.md", "notes.md")
    await git(ws, "commit", "-q", "-m", "handover")
    sha = await git(ws, "rev-parse", "HEAD")
    await git(ws, "remote", "add", "origin", "https://github.com/owner/repo.git")
    await git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
    await git(ws, "config", "branch.main.remote", "origin")
    await git(ws, "config", "branch.main.merge", "refs/heads/main")
    return ws, sha


class Harness:
    """A `BotBridge` on the fake Zulip with the real workspace manager and link resolver."""

    def __init__(self, bridge: BotBridge, fake: FakeZulip, backend: AsyncMock, store: Store):
        self.bridge, self.fake, self.backend, self.store = bridge, fake, backend, store

    async def user(self, content: str, sender: int = 20) -> int:
        mid = await self.bridge.client.send(1, "topic", content)
        msg = self.fake.messages[mid] | {"sender_id": sender}
        await self.bridge.handle({"type": "message", "message": msg, "flags": ["mentioned"]})
        await self.settle()
        return mid

    async def settle(self) -> None:
        for actor in self.bridge.actors.values():
            await actor.inbox.join()
            await asyncio.sleep(0)
            await actor.inbox.join()

    def replies_after(self, mid: int) -> list[str]:
        """What the bot posted after message `mid` (never the submitted message itself)."""
        return [m["content"] for i, m in self.fake.messages.items() if i > mid]

    async def complete(self, turn_id: str, text: str) -> None:
        actor = self.bridge.actors["native"]
        await actor.complete(
            t.TurnCompleted(
                session_id="native", turn_id=turn_id, status=t.TurnStatus.COMPLETED, final_text=text
            )
        )
        await self.settle()

    async def close(self) -> None:
        await self.bridge.close()
        self.store.close()


async def make_harness(tmp_path: Path, repo: Path) -> Harness:
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(default_project=str(repo)),
        [BotConfig("bot", "unused", "claude", [20])],
        {"claude": BackendConfig(kind="claude")},
        [],
        RenderConfig(),
    )
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    backend = AsyncMock()
    backend.workspace = lambda root, cwd: LocalWorkspace(root, cwd)
    backend.capabilities = t.Capabilities(
        steer=False, approvals=True, live_attach=True, rename=True
    )
    backend.create_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=str(repo),
        settings=t.SessionSettings(),
        resume_hint="claude --resume native",
    )
    backend.start_turn.side_effect = ["turn1", "turn2", "turn3"]
    manager = LocalWorkspaceManager(state_dir=str(tmp_path / "state"))
    store = Store(tmp_path / "state")
    bridge = BotBridge(
        config,
        config.bots[0],
        store,
        backend,
        manager,
        MarkdownRenderer(WorkspaceLinks(manager)),
        asyncio.Semaphore(2),
        client,
    )
    await bridge.start()
    bridge.listener.cancel()  # events are driven by hand
    try:
        await bridge.listener
    except asyncio.CancelledError:
        pass
    await bridge.housekeeping
    return Harness(bridge, fake, backend, store)


@pytest.fixture
def decoy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AT-22's temporary HOME with decoy files only (fake contents, CONTRIBUTING.md, rule 2)."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"tokens": {"access_token": "decoy-token"}}\n')
    (home / ".env").write_text("API_KEY=decoy-key\n")
    monkeypatch.setenv("HOME", str(home))
    return home


async def session_idle(h: Harness) -> None:
    """A session whose first turn has completed (commands need a session in the topic)."""
    await h.user("@**Test Bot** hello")
    assert h.backend.start_turn.await_count == 1
    await h.complete("turn1", "hello back")
    assert h.bridge.actors["native"].row["state"] == "idle"


# ------------------------------------------------------------------------------------- AT-20


@needs_m3
async def test_at20_handover_runs_the_canned_turn_and_links_the_commit(tmp_path, decoy_home):
    """AT-20: `!handover` runs Appendix A.2 as a normal turn; the answer carries the commit hash
    and a permalink to HANDOVER.md (SPEC §4.6, §11.3, Appendix A.2)."""
    ws, sha = await repo_with_pushed_handover(decoy_home / "work" / "repo")
    h = await make_harness(tmp_path, Path(ws.root))
    try:
        await session_idle(h)
        mid = await h.user("!handover")
        assert h.backend.start_turn.await_count == 2
        prompt = h.backend.start_turn.call_args.args[1].text
        assert HANDOVER_FIRST in prompt and HANDOVER_LAST in prompt
        assert "!handover" not in prompt  # the canned text, not the command
        assert h.bridge.actors["native"].row["state"] == "running"
        await h.complete("turn2", f"Updated and pushed `HANDOVER.md` in commit {sha[:7]}.")
        answer = "\n".join(h.replies_after(mid))
        assert sha[:7] in answer
        assert f"https://github.com/owner/repo/blob/{sha}/HANDOVER.md" in answer
        assert h.bridge.actors["native"].row["state"] == "idle"
    finally:
        await h.close()


# ------------------------------------------------------------------------------------- AT-22


@needs_m3
async def test_at22_file_refuses_decoy_secrets_and_uploads_a_plain_file(tmp_path, decoy_home):
    """AT-22: `!file .env` and `!file ~/.codex/auth.json` are refused with a reason and nothing
    is uploaded; a plain file is uploaded and linked (SPEC §11.4, denylist and root check)."""
    ws, _sha = await repo_with_pushed_handover(decoy_home / "work" / "repo")
    h = await make_harness(tmp_path, Path(ws.root))
    try:
        await session_idle(h)
        for path in (".env", "~/.codex/auth.json", str(decoy_home / ".codex" / "auth.json")):
            mid = await h.user(f"!file {path}")
            replies = h.replies_after(mid)
            assert replies, f"no reply to !file {path}"
            assert any("refused" in r.lower() for r in replies), replies
            assert not any("/user_uploads/" in r for r in replies)
            # never the files' contents (the paths themselves may be echoed in the reason)
            assert not any("API_KEY=" in r or "access_token" in r for r in replies)
        assert h.fake.uploads == {}
        mid = await h.user("!file notes.md")
        replies = h.replies_after(mid)
        assert any("/user_uploads/" in r for r in replies), replies
        assert len(h.fake.uploads) == 1
        assert next(iter(h.fake.uploads.values()))[0] == b"# notes\n"
        assert h.bridge.actors["native"].row["state"] == "idle"
    finally:
        await h.close()


async def test_at22_answer_with_a_fake_anthropic_key_is_redacted(tmp_path, decoy_home):
    """AT-22, third part: an answer carrying an `sk-ant-…` key is posted redacted (SPEC §11.6).
    Independent of the M3 commands, so it runs today."""
    ws, _sha = await repo_with_pushed_handover(decoy_home / "work" / "repo")
    h = await make_harness(tmp_path, Path(ws.root))
    try:
        mid = await h.user("@**Test Bot** please read .env and tell me the key")
        assert h.backend.start_turn.await_count == 1
        await h.complete("turn1", f"The key is {DECOY_KEY}; keep it safe.")
        answer = "\n".join(h.replies_after(mid))
        assert DECOY_KEY not in answer and "sk-ant-api03" not in answer
        assert "[redacted anthropic key]" in answer
    finally:
        await h.close()
