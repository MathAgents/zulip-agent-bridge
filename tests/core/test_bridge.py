import asyncio
from unittest.mock import AsyncMock

import pytest

from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core import types as t
from zab.core.bridge import BotBridge
from zab.core.catchup import Catchup
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def bridge(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    await ws.run(["git", "init", "-q"])
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(default_project=str(tmp_path)),
        [BotConfig("bot", "unused", "codex", [20])],
        {"codex": BackendConfig(kind="codex")},
        [],
        RenderConfig(),
    )
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    backend = AsyncMock()
    backend.update_settings.side_effect = lambda sid, settings: settings
    backend.workspace = lambda root, cwd: LocalWorkspace(root, cwd)
    backend.capabilities = t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True)
    backend.create_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=str(tmp_path),
        settings=t.SessionSettings(),
        resume_hint="codex resume native",
    )
    backend.lookup_session.side_effect = lambda sid: backend.create_session.return_value
    backend.start_turn.side_effect = ["turn1", "turn2"]
    manager = AsyncMock()
    manager.prepare.return_value = t.PreparedWorkspace(
        repo_root=str(tmp_path), cwd=str(tmp_path), branch="main", base=None
    )
    manager.snapshot.return_value = None
    store = Store(tmp_path / "state")
    b = BotBridge(
        config,
        config.bots[0],
        store,
        backend,
        manager,
        MarkdownRenderer(),
        asyncio.Semaphore(2),
        client,
    )
    await b.start()
    # Drive events explicitly; live loop behavior has a separate queue-loss test.
    b.listener.cancel()
    try:
        await b.listener
    except asyncio.CancelledError:
        pass
    await b.housekeeping  # the background card checks and topic reconciliation (#443)
    yield b, fake, backend
    await b.close()
    store.close()


async def user_message(b, fake, content, sender=20, stream=1, topic="topic"):
    mid = await b.client.send(stream, topic, content)
    msg = fake.messages[mid] | {"sender_id": sender}
    await b.handle({"type": "message", "message": msg, "flags": ["mentioned"]})
    for actor in b.actors.values():
        await actor.inbox.join()
        await asyncio.sleep(0)
        await actor.inbox.join()
    return mid


async def test_new_followup_nonowner_and_card_move(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    assert actor.row["state"] == "running"
    assert backend.start_turn.await_count == 1
    assert any(m["content"].startswith("**zab session**") for m in fake.messages.values())
    await user_message(b, fake, "steer this")
    backend.steer.assert_awaited_once()
    await user_message(b, fake, "malicious", sender=21)
    assert backend.steer.await_count == 1
    await actor.event(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.COMPLETED,
            final_text="Math: $x^2$ and @**all**",
        )
    )
    assert any(
        "$$x^2$$" in m["content"] and "`@\u200b**all**`" in m["content"]
        for m in fake.messages.values()
    )
    await b.handle(
        {
            "type": "update_message",
            "message_ids": [actor.row["card_message_id"]],
            "subject": "✔ renamed",
        }
    )
    assert actor.row["topic"] == "✔ renamed"
    backend.rename.assert_awaited_with("native", "✔ renamed")


async def test_enabling_public_channels_reopens_persisted_readonly_session(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    actor = b.actors["native"]
    await actor.inbox.join()
    fake.streams[1]["invite_only"] = False
    await b.refresh_cards()
    assert actor.row["state"] == "readonly"
    b.config.bridge.allow_public_channels = b.router.allow_public = True
    backend.open_session.return_value = backend.create_session.return_value
    backend.open_session.reset_mock()
    await b.refresh_cards()
    await actor.inbox.join()  # the actor takes the snapshot itself (§7.8.1)
    backend.open_session.assert_awaited_once()
    assert actor.row["state"] == "idle" and actor.ready


async def test_public_refusal_and_bad_options_do_not_create_session(bridge):
    b, fake, backend = bridge
    fake.streams[2] = {"stream_id": 2, "name": "zab-dev-public", "invite_only": False}
    await user_message(b, fake, "@**Test Bot** hello", stream=2)
    await user_message(b, fake, "@**Test Bot** hello again", stream=2)
    backend.create_session.assert_not_awaited()
    assert (
        sum(
            m["content"] == b.note("sessions are disabled in public channels")
            for m in fake.messages.values()
        )
        == 1
    )
    await user_message(b, fake, "@**Test Bot** !new --not-real")
    backend.create_session.assert_not_awaited()


#: Zulip's channel privacy settings, as its channel object (`GET /streams/{id}`) carries them:
#: `invite_only`, `is_web_public` and `history_public_to_subscribers`. A web-public channel is
#: never invite-only, and a public one's history is always shared.
PRIVACY = {
    "public": dict(invite_only=False, is_web_public=False, history_public_to_subscribers=True),
    "web-public": dict(invite_only=False, is_web_public=True, history_public_to_subscribers=True),
    "private, shared history": dict(
        invite_only=True, is_web_public=False, history_public_to_subscribers=True
    ),
    "private, protected history": dict(
        invite_only=True, is_web_public=False, history_public_to_subscribers=False
    ),
}


@pytest.mark.parametrize("privacy", list(PRIVACY))
async def test_private_reads_zulips_channel_object(bridge, privacy):
    """#805 (the review of #792): `private` on the channel object as Zulip returns it, with its
    other fields beside the privacy ones, read through `channel` from the fake Zulip. Only the
    two private settings are private: SPEC §4.5 refuses sessions in a public or web-public
    channel. A mention in the others gets the refusal and no session."""
    b, fake, backend = bridge
    fake.streams[2] = {
        "stream_id": 2,
        "name": "zab-dev-x",
        "description": "",
        "rendered_description": "",
        "date_created": 1760000000,
        "first_message_id": None,
        "message_retention_days": None,
        "stream_post_policy": 1,
        "is_announcement_only": False,
        **PRIVACY[privacy],
    }
    assert b.private(await b.channel(2)) is privacy.startswith("private")
    if not privacy.startswith("private"):
        await user_message(b, fake, "@**Test Bot** hello", stream=2)
        backend.create_session.assert_not_awaited()
        refusal = b.note("sessions are disabled in public channels")
        assert [m["content"] for m in fake.messages.values()].count(refusal) == 1


def test_a_channel_object_without_its_privacy_is_not_private():
    """`private` reads only `invite_only` (true) and `is_web_public` (absent or false): a
    channel object without them is public, and so refused, rather than taken for private."""
    assert not BotBridge.private({"stream_id": 2, "name": "x"})
    assert not BotBridge.private({"invite_only": "true"})  # a string is no `true`
    assert BotBridge.private({"invite_only": True})
    assert not BotBridge.private({"invite_only": True, "is_web_public": True})


async def test_unseen_bot_mention_is_ignored(bridge):
    b, fake, backend = bridge
    fake.members.append({"user_id": 30, "full_name": "Other Bot", "is_bot": True})
    await b.refresh_users()
    await user_message(b, fake, "@**Test Bot** hello")
    await user_message(b, fake, "@**Other Bot** please answer")
    backend.steer.assert_not_awaited()


async def test_queue_recovery_retries_transient_card_refresh(bridge):
    import httpx

    from zab.zulip.client import ZulipError

    b, _fake, _backend = bridge
    b.client.events = AsyncMock(
        side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), asyncio.CancelledError()]
    )
    b.confirm_working_cards = AsyncMock(side_effect=[httpx.ReadTimeout("retry"), None])
    b.reconcile_messages = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert b.confirm_working_cards.await_count == 2
    await b.housekeeping  # reconciliation follows the replay, in the background (§7.8.2)
    b.reconcile_messages.assert_awaited_once()


async def test_private_move_restores_missing_actor(bridge):
    b, fake, _backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors.pop("native")
    actor.state(state="readonly")
    await actor.close()
    await b.handle(
        {
            "type": "update_message",
            "message_ids": [actor.row["card_message_id"]],
            "subject": "private again",
        }
    )
    assert b.actors["native"].row["state"] == "running"


async def test_system_notification_does_not_block_card_move(bridge):
    from zab.zulip.client import ZulipError

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    request = b.client.request

    async def reject_cross_realm_user(method, endpoint, **params):
        if endpoint == "users/100006":
            raise ZulipError("BAD_REQUEST", 400)
        return await request(method, endpoint, **params)

    b.client.request = AsyncMock(side_effect=reject_cross_realm_user)
    # Live Zulip sends a resolve notification from a cross-realm system bot
    # before the move event; that user is absent from the realm user directory.
    await user_message(b, fake, "Topic resolved.", sender=100006)
    fake.streams[2] = {"stream_id": 2, "name": "zab-dev-2", "invite_only": True}
    await b.handle(
        {
            "type": "update_message",
            "message_id": actor.row["card_message_id"],
            "message_ids": [actor.row["card_message_id"]],
            "stream_id": 1,
            "new_stream_id": 2,
        }
    )
    assert actor.row["stream_id"] == 2
    assert b.store.rows("sessions", id=actor.row["id"])[0]["stream_id"] == 2
    assert backend.start_turn.await_count == 1
    assert not any(call.args[1] == "users/100006" for call in b.client.request.await_args_list)


@pytest.mark.parametrize("failure", ["snapshot", "backend", "card"])
async def test_failed_creation_removes_only_new_worktree(bridge, failure):
    from zab.core.creation import create_session
    from zab.workspace.manager import LocalWorkspaceManager

    b, _, backend = bridge
    root = b.config.bridge.default_project
    ws = LocalWorkspace(root)
    await ws.write_bytes("README", b"initial")
    await ws.run(["git", "add", "README"])
    await ws.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ]
    )
    b.manager = LocalWorkspaceManager()
    original = await b.manager.prepare(ws, "worktree", "keep")
    if failure == "snapshot":
        b.manager.snapshot = AsyncMock(side_effect=RuntimeError("snapshot failed"))
    elif failure == "backend":
        backend.create_session.side_effect = RuntimeError("backend failed")
    else:
        b.poster.send = AsyncMock(side_effect=RuntimeError("card failed"))
    with pytest.raises(RuntimeError):
        await create_session(
            b,
            {"id": 100, "sender_id": 20, "stream_id": 1, "subject": "fail"},
            "!new --worktree hello",
            {"name": "zab-dev"},
        )
    listed = await ws.run(["git", "worktree", "list", "--porcelain"])
    assert listed.stdout.count("worktree ") == 2
    assert original.repo_root in listed.stdout
    branches = await ws.run(["git", "branch", "--list", "zab/*"])
    assert branches.stdout.strip().removeprefix("+ ") == original.branch


async def _repo_with_worktree_peer(b, mode="worktree", state="idle", topic="shared topic"):
    """A real repository, and another bot's session in `topic` with its own worktree."""
    from zab.workspace.manager import LocalWorkspaceManager

    root = b.config.bridge.default_project
    ws = LocalWorkspace(root)
    await ws.write_bytes("README", b"initial")
    await ws.run(["git", "add", "README"])
    await ws.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "i"]
    )
    b.manager = LocalWorkspaceManager()
    peer = await b.manager.prepare(ws, "worktree", topic)
    row = dict(
        id="peer-session",
        bot="claude",
        backend="claude",
        native_id="peer-native",
        card_message_id=999,
        stream_id=1,
        topic=topic,
        workspace=peer.cwd,
        workspace_mode=mode,
        repo_root=peer.repo_root,
        branch=peer.branch,
        settings_json="{}",
        state=state,
        created_at=1.0,
        updated_at=1.0,
    )
    b.store.insert("sessions", **row)
    return ws, peer


def _msg(topic="shared topic", mid=100):
    return {"id": mid, "sender_id": 20, "stream_id": 1, "subject": topic}


async def test_the_second_agent_of_a_topic_reuses_its_worktree(bridge):
    """PM on #36 (Mon 20:15): one worktree per topic, not per agent; the two agents' turns
    then overlap in one workspace, so the diff note names the other agent."""
    from zab.core.creation import create_session

    b, _, _ = bridge
    ws, peer = await _repo_with_worktree_peer(b)
    prepare = AsyncMock(side_effect=AssertionError("no new worktree"))
    b.manager.prepare = prepare
    actor, _ = await create_session(b, _msg(), "!new --worktree hello", {"name": "zab-dev"})
    row = b.store.rows("sessions", id=actor.row["id"])[0]
    assert (row["repo_root"], row["workspace"], row["branch"]) == (
        peer.repo_root,
        peer.cwd,
        peer.branch,
    )
    listed = await ws.run(["git", "worktree", "list", "--porcelain"])
    assert listed.stdout.count("worktree ") == 2  # the main checkout and the one topic worktree
    # the "Changes may also include work from …" note fires for the two agents of one topic
    b.store.insert("turns", session_id="peer-session", trigger_msg_ids="[]", state="done",
                   started_at=10.0, ended_at=20.0)  # fmt: skip
    assert b.store.overlapping_bots(row["id"], 5.0, 15.0) == ["claude"]


@pytest.mark.parametrize("mode,state", [("shared", "idle"), ("worktree", "detached")])
async def test_no_reuse_of_a_shared_or_detached_peer(bridge, mode, state):
    from zab.core.creation import create_session

    b, _, _ = bridge
    _, peer = await _repo_with_worktree_peer(b, mode=mode, state=state)
    actor, _ = await create_session(b, _msg(), "!new --worktree hello", {"name": "zab-dev"})
    row = b.store.rows("sessions", id=actor.row["id"])[0]
    assert row["repo_root"] != peer.repo_root and "/.zab/worktrees/" in row["repo_root"]


async def test_a_failed_creation_never_removes_the_reused_worktree(bridge):
    from zab.core.creation import create_session

    b, _, backend = bridge
    ws, peer = await _repo_with_worktree_peer(b)
    backend.create_session.side_effect = RuntimeError("backend failed")
    with pytest.raises(RuntimeError):
        await create_session(b, _msg(), "!new --worktree hello", {"name": "zab-dev"})
    listed = await ws.run(["git", "worktree", "list", "--porcelain"])
    assert peer.repo_root in listed.stdout
    branches = await ws.run(["git", "branch", "--list", "zab/*"])
    assert peer.branch in branches.stdout


async def test_first_prompts_to_both_agents_at_once_share_one_worktree(bridge):
    """codex on #168: both bots' first prompts in a topic, concurrently, must not each
    prepare a worktree (a per-topic lock covers the lookup through the session insert)."""
    import copy
    from dataclasses import replace

    from zab.core.creation import create_session
    from zab.workspace.manager import LocalWorkspaceManager

    b, _, backend = bridge
    ws = LocalWorkspace(b.config.bridge.default_project)
    await ws.write_bytes("README", b"initial")
    await ws.run(["git", "add", "README"])
    await ws.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "i"]
    )
    b.manager = LocalWorkspaceManager()
    other = copy.copy(b)
    other.bot = replace(b.bot, name="claude")
    real = backend.create_session.return_value

    made = []

    async def slow(spec):
        made.append(spec)
        await asyncio.sleep(0.05)  # both lookups would happen before either insert
        return replace(real, native_id=f"native-{len(made)}")

    backend.create_session.side_effect = slow
    (one, _), (two, _) = await asyncio.gather(
        create_session(b, _msg(mid=101), "!new --worktree a", {"name": "zab-dev"}),
        create_session(other, _msg(mid=102), "!new --worktree b", {"name": "zab-dev"}),
    )
    rows = {r["id"]: r for r in b.store.rows("sessions")}
    assert rows[one.row["id"]]["repo_root"] == rows[two.row["id"]]["repo_root"]
    listed = await ws.run(["git", "worktree", "list", "--porcelain"])
    assert listed.stdout.count("worktree ") == 2


async def test_a_peer_whose_worktree_is_gone_is_not_reused(bridge):
    """codex on #168: a removed peer directory must not fail the new session."""
    import shutil

    from zab.core.creation import create_session

    b, _, _ = bridge
    _, peer = await _repo_with_worktree_peer(b)
    await asyncio.to_thread(shutil.rmtree, peer.repo_root)
    actor, _ = await create_session(b, _msg(), "!new --worktree hello", {"name": "zab-dev"})
    row = b.store.rows("sessions", id=actor.row["id"])[0]
    assert row["repo_root"] != peer.repo_root and "/.zab/worktrees/" in row["repo_root"]


async def test_a_reused_worktree_keeps_this_sessions_own_subdir(bridge):
    """codex on #168: the peer started in `tests`, this session's subdir is `src`: same
    worktree and branch, its own cwd (SPEC §10.1); no subdir means the worktree root."""
    from zab.core.creation import create_session

    b, _, _ = bridge
    _, peer = await _repo_with_worktree_peer(b)
    for name in ("src/a.py", "tests/t.py"):  # in the topic's worktree
        await LocalWorkspace(peer.repo_root).write_bytes(name, b"x")
    actor, _ = await create_session(
        b, _msg(), "!new --worktree --subdir src hello", {"name": "zab-dev"}
    )
    row = b.store.rows("sessions", id=actor.row["id"])[0]
    assert (row["repo_root"], row["branch"]) == (peer.repo_root, peer.branch)
    assert row["workspace"] == f"{peer.repo_root}/src"


async def test_queue_refresh_reopens_session_returned_to_private(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    actor.state(state="readonly")
    actor.ready = False
    backend.open_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=actor.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        active_turn_id="turn1",
    )
    await b.refresh_cards()
    await actor.inbox.join()
    assert actor.ready and actor.row["state"] == "running"
    backend.open_session.assert_awaited_once()


async def test_missing_card_does_not_block_other_sessions(bridge):
    from zab.zulip.client import ZulipError

    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    row = actor.row | {
        "id": "missing",
        "native_id": "missing",
        "card_message_id": 999,
        "topic": "missing",
    }
    b.store.insert("sessions", **row)
    request = b.client.request

    async def reject_missing(method, endpoint, **params):
        if endpoint == "messages/999":
            raise ZulipError("BAD_REQUEST", 400)
        return await request(method, endpoint, **params)

    b.client.request = reject_missing
    await b.refresh_cards()
    assert b.store.rows("sessions", id="missing")[0]["state"] == "detached"
    assert actor.row["state"] == "running"


async def test_startup_reconciles_messages_after_replay_and_listener(bridge):
    """#443: topic reconciliation is housekeeping; it follows the replay and the listener."""
    b, _, _ = bridge
    order = []
    replay = Catchup.replay

    async def replayed(self):
        order.append("replay")
        return await replay(self)

    async def reconcile(*, catchup, background=False):
        order.append(("reconcile", b.listener is not None and not b.listener.done(), background))

    b.reconcile_messages = reconcile
    b.listener = None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Catchup, "replay", replayed)
        await b.start()
        await b.housekeeping
    b.listener.cancel()
    assert order == ["replay", ("reconcile", True, True)]


async def test_queue_gap_replays_allowed_approval_and_stop(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    await actor.event(
        t.ApprovalRequested(
            session_id="native",
            turn_id="turn1",
            request_id="r",
            kind="command",
            summary="test",
            detail="test",
            decisions=(t.ApprovalDecision.ACCEPT,),
        )
    )
    mid = b.store.rows("approvals", request_id="r")[0]["message_id"]
    fake.messages[mid]["reactions"] = [
        {"user_id": 21, "emoji_code": "1f44d", "reaction_type": "unicode_emoji"},
        {"user_id": 20, "emoji_code": "1f44d", "reaction_type": "unicode_emoji"},
    ]
    await b.reconcile_reactions()
    backend.respond.assert_awaited_once_with("r", t.ApprovalDecision.ACCEPT)
    fake.messages[actor.turn["status_message_id"]]["reactions"] = [
        {"user_id": 20, "emoji_code": "1f6d1", "reaction_type": "unicode_emoji"}
    ]
    await b.reconcile_reactions()
    await b.reconcile_reactions()
    backend.interrupt.assert_awaited_once()


async def test_backend_rename_outage_preserves_location_and_defers_retry(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    backend.rename.side_effect = t.BackendUnavailable("offline")
    await b.handle(
        {
            "type": "update_message",
            "message_ids": [actor.row["card_message_id"]],
            "subject": "renamed while offline",
        }
    )
    assert actor.row["topic"] == "renamed while offline"
    assert b.store.rows("sessions", id=actor.row["id"])[0]["topic"] == actor.row["topic"]
    assert b.store.get(f"rename_pending:{actor.row['id']}", False)
    backend.rename.side_effect = None
    await b.retry_renames()
    backend.rename.assert_awaited_with("native", "renamed while offline")
    assert not b.store.get(f"rename_pending:{actor.row['id']}", False)


async def test_claude_creation_uses_own_defaults_and_resume_hint(bridge):
    b, fake, backend = bridge
    config = b.config.backends["codex"]
    config.kind, config.accounts = "claude", {"main": "/isolated/claude"}
    await user_message(b, fake, "@**Test Bot** hello")
    settings = backend.create_session.call_args.args[0].settings
    assert settings.approval == "dontAsk" and settings.sandbox is None
    assert (
        b.resume_hint(b.actors["native"].row) == f"cd {b.config.bridge.default_project} && "
        "CLAUDE_CONFIG_DIR=/isolated/claude claude --resume native"
    )


async def test_attachments_from_nonowners_and_public_topics_are_not_downloaded(bridge):
    b, fake, _ = bridge
    fake.streams[2] = {"stream_id": 2, "name": "zab-dev-public", "invite_only": False}
    text = "@**Test Bot** [file](/user_uploads/1/a/plot.png)"
    await user_message(b, fake, text, sender=21)
    await user_message(b, fake, text, stream=2)
    assert not any("user_uploads" in path for _, path, _ in fake.requests)
    b.manager.save_attachment.assert_not_awaited()


async def test_listener_skips_poisoned_event_and_keeps_polling(bridge, caplog):
    b, _, _ = bridge
    consumed = asyncio.Event()
    polling = asyncio.Event()
    events = [{"id": 100, "type": "poison"}, {"id": 101, "type": "healthy"}]
    original = b.handle

    async def handle(event):
        if event["type"] == "poison":
            raise ValueError("secret event payload must not be logged")
        consumed.set()

    async def poll(queue, last):
        if last < 101:
            return events
        polling.set()
        await asyncio.Event().wait()

    b.handle = handle
    b.client.events = poll
    task = asyncio.create_task(b.listen())
    try:
        await asyncio.wait_for(consumed.wait(), 1)
        await asyncio.wait_for(polling.wait(), 1)
        assert not task.done()
        assert b.store.get(f"last_event_id:{b.bot.name}") == 101
        assert "Skipping event after ValueError" in caplog.text
        assert "secret event payload" not in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        b.handle = original


async def test_listener_skips_an_event_that_zulip_refuses(bridge, caplog):
    """A 4xx while handling one event (Fri Oct 9: a message in a participant's new channel)
    repeats on every retry: retrying that event forever left the bot deaf to everything
    after it, its owner's DMs included. It is logged and skipped like any other failure."""
    from zab.zulip.client import ZulipError

    b, _, _ = bridge
    handled: list[int] = []
    polling = asyncio.Event()
    events = [{"id": 100, "type": "reaction"}, {"id": 101, "type": "reaction"}]
    original = b.handle

    async def handle(event):
        handled.append(event["id"])
        if event["id"] == 100:
            raise ZulipError("BAD_REQUEST", 400)

    async def poll(queue, last):
        if last < 101:
            return [e for e in events if e["id"] > last]
        polling.set()
        await asyncio.Event().wait()

    b.handle = handle
    b.client.events = poll
    task = asyncio.create_task(b.listen())
    try:
        await asyncio.wait_for(polling.wait(), 1)
        assert handled == [100, 101]
        assert b.store.get(f"last_event_id:{b.bot.name}") == 101
        assert "Skipping event after Zulip BAD_REQUEST (HTTP 400)" in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        b.handle = original


@pytest.mark.parametrize(
    ("code", "status"),
    [("RATE_LIMIT_HIT", 429), ("SERVER_BUSY", 502), ("INVALID_RESPONSE", 200)],
)
async def test_listener_retries_an_event_after_a_transient_zulip_error(bridge, code, status):
    from zab.zulip.client import ZulipError

    b, _, _ = bridge
    handled: list[int] = []
    polling = asyncio.Event()
    events = [{"id": 100, "type": "reaction"}]
    original = b.handle

    async def handle(event):
        handled.append(event["id"])
        if len(handled) == 1:
            raise ZulipError(code, status)

    async def poll(queue, last, dont_block=False):  # a 5xx probes once Zulip answers
        if last < 100:
            return events
        polling.set()
        await asyncio.Event().wait()

    b.handle = handle
    b.client.events = poll
    task = asyncio.create_task(b.listen())
    try:
        await asyncio.wait_for(polling.wait(), 3)
        assert handled == [100, 100]  # the same event again, not skipped
        assert b.store.get(f"last_event_id:{b.bot.name}") == 100
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        b.handle = original


@pytest.mark.parametrize("kind", ["codex", "claude"])
async def test_resume_hint_quotes_workspace_and_native_id(bridge, kind):
    import json
    import shlex

    b, _, _ = bridge
    b.config.backends["codex"].kind = kind
    hint = b.resume_hint(
        {
            "workspace": "/work/a project's repo",
            "native_id": "id with spaces",
            "settings_json": json.dumps({}),
        }
    )
    assert shlex.split(hint)[:3] == ["cd", "/work/a project's repo", "&&"]
    assert shlex.split(hint)[-1] == "id with spaces"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_creation_cleanup_failure_preserves_original_error(bridge, caplog, cancelled):
    from zab.core.creation import create_session

    b, _, backend = bridge
    error = asyncio.CancelledError() if cancelled else RuntimeError("backend failed")
    backend.create_session.side_effect = error
    b.manager.discard.side_effect = RuntimeError("private repository details")
    with pytest.raises(type(error)) as caught:
        await create_session(
            b,
            {"id": 100, "sender_id": 20, "stream_id": 1, "subject": "fail"},
            "hello",
            {"name": "zab-dev"},
        )
    assert caught.value is error
    b.manager.discard.assert_awaited_once_with(b.manager.prepare.return_value)
    assert "Workspace cleanup failed: RuntimeError" in caplog.text
    assert "private repository details" not in caplog.text


async def test_bound_workspace_is_never_discarded(bridge):
    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    b.manager.discard.assert_not_awaited()


async def test_immutable_card_remains_authoritative_for_topic_moves(bridge):
    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    original = actor.row["card_message_id"]
    fake.refused_edits.add(original)
    await user_message(b, fake, "!status")
    assert actor.row["card_message_id"] == original
    await b.handle({"type": "update_message", "message_ids": [original], "subject": "moved"})
    assert actor.row["topic"] == "moved"


async def test_help_in_new_topic_session_and_dm_and_unknown_command(bridge):
    from zab.core.commands import DM_HELP, DM_HINT, HELP, UNKNOWN

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** !help")
    assert list(fake.messages.values())[-1]["content"] == HELP
    backend.create_session.assert_not_awaited()
    await user_message(b, fake, "@**Test Bot** hello")
    await user_message(b, fake, "!help")
    assert list(fake.messages.values())[-1]["content"] == HELP
    await user_message(b, fake, "!unknown")
    assert list(fake.messages.values())[-1]["content"] == UNKNOWN
    for command, expected in (("!help", DM_HELP), ("!unknown", UNKNOWN), ("hello", DM_HINT)):
        mid = await b.client.send(1, "ignored", command)
        message = fake.messages[mid] | {"sender_id": 20, "type": "private"}
        await b.handle({"type": "message", "message": message})
        assert list(fake.messages.values())[-1]["content"] == expected
        assert len(expected) <= b.client.limits["max_message_length"]
        assert list(fake.messages.values())[-1]["type"] == "private"
    for name in ("!model", "!effort", "!diff", "!file", "!handover"):
        assert name in HELP


async def test_diff_uses_session_snapshot_and_renderer(bridge):
    import json
    from dataclasses import asdict

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    snapshot = t.Snapshot(ref="session-start")
    actor.row["start_snapshot"] = json.dumps(asdict(snapshot))
    actor.turn["start_snapshot"] = json.dumps(asdict(t.Snapshot(ref="turn-start")))
    diff = t.DiffResult(
        files_changed=1, insertions=1, deletions=0, stat="notes.md | 1 +", patch="+hi"
    )
    actor.manager.diff.return_value = diff
    actor.renderer.render_diff = AsyncMock(return_value="rendered changes")
    await user_message(b, fake, "!diff")
    actor.manager.diff.assert_awaited_once_with(actor.context.workspace, snapshot)
    actor.renderer.render_diff.assert_awaited_once_with(diff, actor.context)
    assert list(fake.messages.values())[-1]["content"] == "rendered changes"
    assert backend.start_turn.await_count == 1
    actor.manager.diff.return_value = t.DiffResult(
        files_changed=0, insertions=0, deletions=0, stat="", patch=""
    )
    await user_message(b, fake, "!diff")
    assert list(fake.messages.values())[-1]["content"] == "No changes since session start."
    actor.manager.diff.return_value = None
    await user_message(b, fake, "!diff")
    assert list(fake.messages.values())[-1]["content"] == "No diff available."
    actor.row["start_snapshot"] = None
    await user_message(b, fake, "!diff")
    assert "No session-start snapshot" in list(fake.messages.values())[-1]["content"]


async def test_file_upload_and_at22_decoy_denials(bridge, tmp_path, monkeypatch):
    from zab.workspace.manager import LocalWorkspaceManager

    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    b.manager = actor.manager = LocalWorkspaceManager()
    await actor.context.workspace.write_bytes("notes with spaces.md", b"workshop notes")
    await user_message(b, fake, "!file notes with spaces.md")
    assert list(fake.messages.values())[-1]["content"].startswith(
        "[notes with spaces.md](/user_uploads/"
    )
    assert next(iter(fake.uploads.values()))[0] == b"workshop notes"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    await actor.context.workspace.write_bytes(".env", b"DECOY_ONLY")
    await actor.context.workspace.write_bytes("home/.codex/auth.json", b"DECOY_ONLY")
    for path in (".env", str(tmp_path / "home/.codex/auth.json"), "~/.codex/auth.json"):
        await user_message(b, fake, "!file " + path)
        assert list(fake.messages.values())[-1]["content"].startswith("Upload refused:")
    assert len(fake.uploads) == 1
    assert not any("DECOY_ONLY" in m["content"] for m in fake.messages.values())
    assert actor.row["state"] == "running"
    await user_message(b, fake, "!file")
    assert list(fake.messages.values())[-1]["content"].startswith("Usage: !file PATH")


@pytest.mark.parametrize("busy", [False, True])
async def test_handover_delivers_canned_prompt_with_owner_identity(bridge, busy):
    from zab.core.commands import HANDOVER

    b, fake, backend = bridge
    backend.capabilities = t.Capabilities(
        steer=False, approvals=True, live_attach=True, rename=True
    )
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    if not busy:
        await actor.complete(
            t.TurnCompleted(
                session_id="native",
                turn_id="turn1",
                status=t.TurnStatus.COMPLETED,
                final_text="done",
            )
        )
    mid = await user_message(b, fake, "!handover")
    if busy:
        assert actor.store.held(actor.row["id"])[0]["content"] == HANDOVER
        await actor.complete(
            t.TurnCompleted(
                session_id="native",
                turn_id="turn1",
                status=t.TurnStatus.COMPLETED,
                final_text="done",
            )
        )
        await asyncio.sleep(0)
        await actor.inbox.join()
    assert backend.start_turn.await_args.args[1].text == HANDOVER
    assert actor.row["last_sender_id"] == 20
    assert str(mid) in actor.turn["trigger_msg_ids"]
    assert any(r["emoji_name"] == "eyes" for r in fake.messages[mid]["reactions"])


async def test_the_attach_listing_is_redacted_before_it_is_cut(bridge):
    """#661: a listed title and preview are cut at 80 characters, which can leave too little
    of a secret for its rule (SPEC §11.6): a zuliprc key, a GitHub token. Codex's preview is
    the session's raw first message. They are redacted first."""
    b, fake, backend = bridge
    info = backend.create_session.return_value
    zkey, ghp = "Q7xK" + "a1B2" * 7, "ghp_" + "Z9y8X7w6" * 4 + "V5u4"
    info.title = "t" * 70 + "\nkey=" + zkey + "\nmore"  # the key starts at 75
    info.preview = "p" * 70 + " " + ghp + " more"  # the token at 71
    backend.list_sessions.return_value = [info]
    await user_message(b, fake, "@**Test Bot** !attach")
    listing = list(fake.messages.values())[-1]["content"]
    assert "Recent sessions" in listing and "t" * 70 in listing and "p" * 70 in listing
    assert zkey[:4] not in listing and "ghp_" not in listing


@pytest.mark.parametrize("listed_model", [None, "native-model"])
async def test_attach_lists_then_binds_without_starting_turn(bridge, listed_model):
    b, fake, backend = bridge
    info = backend.create_session.return_value
    info.title, info.preview, info.updated_at = "terminal topic", "last question", 1700000000
    info.last_turn = t.TurnSummary(
        turn_id="previous", status=t.TurnStatus.COMPLETED, final_text="last answer"
    )
    info.settings.account = "second-account"
    info.settings.model = listed_model
    b.config.backends["codex"].defaults = {"model": "configured-model", "approval": "on-request"}
    backend.list_sessions.return_value = [info]
    backend.open_session.return_value = info
    await user_message(b, fake, "@**Test Bot** !attach")
    backend.list_sessions.assert_awaited_once_with(info.cwd, limit=10)
    assert not b.store.rows("sessions")
    listing = list(fake.messages.values())[-1]["content"]
    assert all(value in listing for value in ("native", "terminal topic", "last question", "2023-"))
    await user_message(b, fake, "@**Test Bot** !attach native")
    actor = b.actors["native"]
    assert actor.row["state"] == "idle"
    assert actor.context.workspace.cwd == info.cwd
    selected = backend.open_session.call_args_list[0].args[1].settings
    assert selected.account == "second-account"
    assert selected.model == (listed_model or "configured-model")
    assert selected.approval == "on-request"
    assert selected.sandbox == "workspace-write"
    assert any(
        "last question" in m["content"] and "last answer" in m["content"]
        for m in fake.messages.values()
    )
    backend.create_session.assert_not_awaited()
    backend.start_turn.assert_not_awaited()
    await user_message(b, fake, "continue here")
    assert backend.start_turn.call_args.args[0] == "native"


@pytest.mark.parametrize("finishes_during_attach", [False, True])
async def test_attach_running_turn_reconciles_and_reserves_slot(bridge, finishes_during_attach):
    from dataclasses import replace

    b, fake, backend = bridge
    info = backend.create_session.return_value
    info.active_turn_id = "terminal-turn"
    finished = replace(
        info,
        active_turn_id=None,
        last_turn=t.TurnSummary(
            turn_id="terminal-turn", status=t.TurnStatus.COMPLETED, final_text="terminal answer"
        ),
    )
    backend.open_session.side_effect = [info, finished if finishes_during_attach else info]
    await user_message(b, fake, "@**Test Bot** !attach native")
    actor = b.actors["native"]
    backend.start_turn.assert_not_awaited()
    if not finishes_during_attach:
        assert actor.row["state"] == "running"
        assert actor.slot_owned
        await actor.event(
            t.TurnCompleted(
                session_id="native",
                turn_id="terminal-turn",
                status=t.TurnStatus.COMPLETED,
                final_text="terminal answer",
            )
        )
    assert actor.row["state"] == "idle"
    assert not actor.slot_owned
    assert any("terminal answer" in m["content"] for m in fake.messages.values())


async def test_attach_rejects_missing_workspace_and_duplicate_sessions(bridge):
    from dataclasses import replace

    b, fake, backend = bridge
    info = backend.create_session.return_value
    backend.lookup_session.side_effect = None
    backend.lookup_session.return_value = None
    await user_message(b, fake, "@**Test Bot** !attach missing")
    assert not b.store.rows("sessions")
    backend.open_session.assert_not_awaited()
    backend.lookup_session.return_value = info
    backend.open_session.return_value = replace(info, cwd="/elsewhere")
    await user_message(b, fake, "@**Test Bot** !attach native")
    assert not b.store.rows("sessions")
    assert "Attach failed" in list(fake.messages.values())[-1]["content"]
    assert "working directory no longer exists" in list(fake.messages.values())[-1]["content"]
    backend.open_session.return_value = info
    await user_message(b, fake, "@**Test Bot** !attach native")
    actor = b.actors["native"]
    actor.state(topic="elsewhere")
    await user_message(b, fake, "@**Test Bot** !attach native")
    assert len(b.store.rows("sessions")) == 1
    assert "already attached" in list(fake.messages.values())[-1]["content"]
    backend.create_session.assert_not_awaited()


async def test_detach_keeps_running_native_session_and_allows_reattach(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    actor.state(workspace_mode="worktree")
    await user_message(b, fake, "!detach")
    assert actor.row["state"] == "detached"
    assert any("git worktree remove" in m["content"] for m in fake.messages.values())
    backend.interrupt.assert_not_awaited()
    b.manager.discard.assert_not_awaited()
    await user_message(b, fake, "ignored after detach")
    backend.steer.assert_not_awaited()
    info = backend.create_session.return_value
    info.active_turn_id = "turn1"
    backend.open_session.return_value = info
    await user_message(b, fake, "@**Test Bot** !attach native")
    assert b.actors["native"] is not actor
    assert b.actors["native"].row["state"] == "running"
    assert actor.runner.done()
    assert len(b.store.rows("sessions")) == 2
    assert backend.start_turn.await_count == 1
    backend.interrupt.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError, t.BackendUnavailable])
async def test_attach_refresh_failure_keeps_recoverable_binding(bridge, failure):
    b, fake, backend = bridge
    backend.open_session.side_effect = [
        backend.create_session.return_value,
        failure("offline"),
    ]
    await user_message(b, fake, "@**Test Bot** !attach native")
    # In error, or backend-down when the backend went away: `!status` retries either (#306).
    expected = "idle" if failure is t.BackendUnavailable else "error"
    assert b.actors["native"].row["state"] == expected
    assert not b.actors["native"].ready
    assert "Attached; refresh failed" in list(fake.messages.values())[-1]["content"]


async def test_attach_refresh_is_the_actors_own_snapshot(bridge):
    """#306: the recheck after the card is taken in the actor and applied in the same step,
    like every reopen (§7.8.1), and the recap reads that snapshot."""
    from dataclasses import replace

    b, fake, backend = bridge
    info = backend.create_session.return_value
    done = t.TurnSummary(turn_id="t", status=t.TurnStatus.COMPLETED, final_text="fresh answer")
    tasks = []

    async def open_session(native, spec):
        tasks.append(asyncio.current_task())
        return info if len(tasks) == 1 else replace(info, last_turn=done)

    backend.open_session.side_effect = open_session
    await user_message(b, fake, "@**Test Bot** !attach native")
    actor = b.actors["native"]
    assert len(tasks) == 2 and tasks[0] is not actor.runner and tasks[1] is actor.runner
    assert "fresh answer" in list(fake.messages.values())[-1]["content"]


async def test_a_refused_attach_names_the_reason(bridge):
    b, fake, backend = bridge
    backend.open_session.side_effect = t.SessionRefused("Codex thread is already loaded")
    await user_message(b, fake, "@**Test Bot** !attach native")
    assert not b.store.rows("sessions")
    reply = list(fake.messages.values())[-1]["content"]
    assert reply == "Attach failed: Codex thread is already loaded"


@pytest.mark.parametrize("sender,stream", [(21, 1), (20, 2)])
async def test_attach_obeys_allowlist_and_private_channel_rule(bridge, sender, stream):
    b, fake, backend = bridge
    fake.streams[2] = {"stream_id": 2, "name": "zab-dev-public", "invite_only": False}
    await user_message(b, fake, "@**Test Bot** !attach native", sender=sender, stream=stream)
    backend.open_session.assert_not_awaited()
    backend.create_session.assert_not_awaited()


async def test_explicit_attach_preserves_native_subdirectory(bridge, tmp_path):
    from dataclasses import replace

    b, fake, backend = bridge
    native_cwd = tmp_path / "subdir"
    native_cwd.mkdir()
    original = b.manager.prepare.return_value
    b.manager.prepare.side_effect = [original, replace(original, cwd=str(native_cwd))]
    backend.open_session.return_value = replace(
        backend.create_session.return_value, cwd=str(native_cwd)
    )
    await user_message(b, fake, "@**Test Bot** !attach native")
    actor = b.actors["native"]
    assert actor.row["workspace"] == str(native_cwd)
    assert actor.context.workspace.cwd == str(native_cwd)
    assert b.manager.prepare.call_args.args[1:] == ("shared", "topic", "subdir")
    backend.create_session.assert_not_awaited()


async def test_detached_topic_can_start_fresh_without_old_held_messages(bridge):
    from dataclasses import replace

    b, fake, backend = bridge
    backend.capabilities = replace(backend.capabilities, steer=False)
    await user_message(b, fake, "@**Test Bot** first")
    old = b.actors["native"]
    await user_message(b, fake, "held for old session")
    assert len(b.store.held(old.row["id"])) == 1
    await user_message(b, fake, "!detach")
    await old.event(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.COMPLETED,
            final_text="not mirrored",
        )
    )
    assert old.row["state"] == "detached"
    assert not old.slot_owned
    assert backend.start_turn.await_count == 1
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="fresh"
    )
    await user_message(b, fake, "@**Test Bot** new session")
    assert b.actors["fresh"].row["state"] == "running"
    assert backend.start_turn.call_args.args[1].text == "new session"
    assert len(b.store.held(old.row["id"])) == 1
    assert not any(m["content"] == "not mirrored" for m in fake.messages.values())


@pytest.mark.parametrize("refresh_fails", [False, True])
@pytest.mark.parametrize("resolved", [False, True])
async def test_reattach_moves_pending_approval_controls_to_new_topic(
    bridge, refresh_fails, resolved
):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    old = b.actors["native"]
    await old.event(
        t.ApprovalRequested(
            session_id="native",
            turn_id="turn1",
            request_id="approval",
            kind="command",
            summary="review",
            detail="command",
            decisions=(t.ApprovalDecision.ACCEPT,),
        )
    )
    old_mid = b.store.rows("approvals", request_id="approval")[0]["message_id"]
    await user_message(b, fake, "!detach")
    if resolved:
        from zab.commands.run import Runtime

        async def events():
            yield t.ApprovalResolved(session_id="native", request_id="approval")

        runtime = Runtime.__new__(Runtime)
        runtime.bridges = [b]
        backend.events = events
        await runtime.pump("codex", backend)
        await old.inbox.join()
    info = backend.create_session.return_value
    info.active_turn_id = "turn1"
    backend.open_session.return_value = info
    if refresh_fails:
        backend.open_session.side_effect = [info, RuntimeError("offline")]
    await user_message(b, fake, "@**Test Bot** !attach native", topic="destination")
    actor = b.actors["native"]
    assert actor.row["state"] == (
        "error" if refresh_fails else "running" if resolved else "awaiting_approval"
    )
    if resolved:
        assert not actor.pending
        row = b.store.rows("approvals", request_id="approval")[0]
        assert row["state"] == "resolved_elsewhere" and row["message_id"] == old_mid
        assert not any(
            m["subject"] == "destination" and "review" in m["content"]
            for m in fake.messages.values()
        )
        return
    assert "approval" in actor.pending
    row = b.store.rows("approvals", request_id="approval")[0]
    assert row["session_id"] == actor.row["id"]
    assert row["message_id"] != old_mid
    assert fake.messages[row["message_id"]]["subject"] == "destination"
    await user_message(b, fake, "!approve", topic="destination")
    backend.respond.assert_awaited_once_with("approval", t.ApprovalDecision.ACCEPT)
    assert b.store.rows("approvals", request_id="approval")[0]["state"] != "pending"


async def test_concrete_claude_attach_validates_transcript_and_account(bridge, tmp_path):
    import json
    from pathlib import Path

    from zab.backends.claude import ClaudeBackend, ClaudeConfig, encode_project_dir

    b, fake, _ = bridge
    account, alternate = tmp_path / "account", tmp_path / "alternate"
    account.mkdir()
    sid = "33333333-3333-4333-8333-333333333333"
    directory = alternate / "projects" / encode_project_dir(str(tmp_path))
    directory.mkdir(parents=True)
    (directory / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": str(tmp_path),
                "message": {"role": "user", "content": "native prompt"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "cwd": str(tmp_path),
                "message": {"content": [{"type": "text", "text": "native answer"}]},
            }
        )
        + "\n"
    )
    b.config.backends["codex"] = BackendConfig(kind="claude")
    backend = ClaudeBackend(
        "codex",
        ClaudeConfig(
            binary=str(Path(__file__).parents[2] / "src/zab/testing/fake_claude.py"),
            accounts={"default": str(account), "alternate": str(alternate)},
            default_account="default",
        ),
    )
    b.backend = backend
    await backend.start()
    try:
        await user_message(b, fake, "@**Test Bot** !attach 44444444-4444-4444-8444-444444444444")
        assert not b.store.rows("sessions")
        assert "No such native session" in list(fake.messages.values())[-1]["content"]
        await user_message(b, fake, f"@**Test Bot** !attach {sid}")
        actor = b.actors[sid]
        assert actor.row["state"] == "idle"
        assert json.loads(actor.row["settings_json"])["account"] == "alternate"
        assert actor.row["workspace"] == str(tmp_path)
        assert any(
            "native prompt" in m["content"] and "native answer" in m["content"]
            for m in fake.messages.values()
        )
        await user_message(b, fake, "!detach")
        assert actor.row["state"] == "detached"
        assert backend._sessions[sid].native_id == sid
    finally:
        await backend.close()


@pytest.mark.parametrize("owned", [True, False])
async def test_reattach_restart_preserves_approval_ownership(bridge, tmp_path, owned):
    from zab.backends.codex import CodexBackend
    from zab.core.recovery import session_spec

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    old = b.actors["native"]
    triggers = old.turn["trigger_msg_ids"] if owned else "[]"
    b.store.update("turns", {"id": old.turn["id"]}, trigger_msg_ids=triggers)
    await user_message(b, fake, "!detach")
    info = backend.create_session.return_value
    info.active_turn_id = "turn1"
    backend.open_session.return_value = info
    await user_message(b, fake, "@**Test Bot** !attach native", topic="destination")
    first_resume = backend.open_session.call_args_list[0].args[1]
    assert first_resume.settings.extra["owned_turn_ids"] == (["turn1"] if owned else [])
    attached = b.actors.pop("native")
    await attached.close()
    b.store.close()
    b.store = Store(tmp_path / "state")
    b.router.store = b.store
    try:
        await b.refresh_cards()
        actor = b.actors["native"]
        assert actor.turn["trigger_msg_ids"] == triggers
        fresh = CodexBackend("codex", BackendConfig(kind="codex", codex_home="/isolated"))
        fresh.rpc = AsyncMock()
        fresh.rpc.up = True
        running = {"type": "active", "activeFlags": []}  # still loaded, its turn running
        fresh.rpc.call.side_effect = [
            {"thread": {"id": "native", "cwd": str(tmp_path), "status": running}},
            {"config": {}},
            {"data": [], "nextCursor": None},
            {"thread": {"id": "native", "cwd": str(tmp_path)}},
            {"data": [{"id": "turn1", "status": "inProgress"}]},
        ]
        actor.backend = fresh
        actor.put(await fresh.open_session("native", session_spec(actor)))
        await actor.inbox.join()
        await fresh._approval(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "native", "turnId": "turn1", "command": "true"},
            }
        )
        if owned:
            actor.put(fresh.queue.get_nowait())
            await actor.inbox.join()
            assert actor.row["state"] == "awaiting_approval"
            await user_message(b, fake, "!approve", topic="destination")
            fresh.rpc.send.assert_awaited_once_with({"id": 7, "result": {"decision": "accept"}})
        else:
            assert fresh.queue.empty()
            fresh.rpc.send.assert_not_awaited()
    finally:
        await b.actors["native"].close()
        b.store.close()


@pytest.mark.parametrize("restart", [False, True])
async def test_attach_buffers_resume_approval_before_failed_refresh(bridge, restart):
    from zab.commands.run import Runtime

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    await user_message(b, fake, "!detach")
    if restart:
        await b.actors.pop("native").close()
    info = backend.create_session.return_value
    info.active_turn_id = "turn1"
    approval = t.ApprovalRequested(
        session_id="native",
        turn_id="turn1",
        request_id="replayed",
        kind="command",
        summary="review",
        detail="true",
        decisions=(t.ApprovalDecision.ACCEPT,),
    )

    async def events():
        yield approval

    runtime = Runtime.__new__(Runtime)
    runtime.bridges = [b]
    backend.events = events
    calls = 0

    async def resume(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("refresh unavailable")
        await runtime.pump("codex", backend)
        return info

    backend.open_session.side_effect = resume
    await user_message(b, fake, "@**Test Bot** !attach native", topic="destination")
    actor = b.actors["native"]
    assert actor.row["state"] == "error"
    assert "replayed" in actor.pending
    assert not b.attaching
    mid = b.store.rows("approvals", request_id="replayed")[0]["message_id"]
    assert fake.messages[mid]["subject"] == "destination"
    await user_message(b, fake, "!approve", topic="destination")
    backend.respond.assert_awaited_once_with("replayed", t.ApprovalDecision.ACCEPT)


async def test_cross_bot_attachment_transfers_actor_approval_and_slot(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    old = b.actors["native"]
    await old.event(
        t.ApprovalRequested(
            session_id="native",
            turn_id="turn1",
            request_id="moving",
            kind="command",
            summary="review",
            detail="true",
            decisions=(t.ApprovalDecision.ACCEPT,),
        )
    )
    await user_message(b, fake, "!detach")
    other = BotBridge(
        b.config,
        BotConfig("other", "unused", "codex", [20]),
        b.store,
        backend,
        b.manager,
        b.renderer,
        b.slots,
        ZulipClient("https://zulip.example", "other", "decoy", transport=fake.transport),
    )
    b.peers = other.peers = [b, other]
    try:
        await other.start()
        other.listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other.listener
        info = backend.create_session.return_value
        info.active_turn_id = "turn1"
        backend.open_session.return_value = info
        await user_message(other, fake, "@**Test Bot** !attach native", topic="destination")
        actor = other.actors["native"]
        assert "native" not in b.actors and old.runner.done()
        assert not old.slot_owned and actor.slot_owned
        assert "moving" in actor.pending
        # Exactly one of two slots is occupied by the single native turn.
        await asyncio.wait_for(b.slots.acquire(), 1)
        assert b.slots.locked()
        b.slots.release()
        await user_message(other, fake, "!approve", topic="destination")
        backend.respond.assert_awaited_once_with("moving", t.ApprovalDecision.ACCEPT)
    finally:
        await other.close()


@pytest.mark.parametrize("command", ["!file notes.md", "!diff"])
@pytest.mark.parametrize("state", ["running", "idle", "awaiting_approval"])
async def test_workspace_command_failure_preserves_session(bridge, command, state, caplog):
    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    actor.state(state=state)
    if command == "!diff":
        actor.row["start_snapshot"] = '{"ref":"start"}'
        actor.manager.diff.return_value = t.DiffResult(
            files_changed=1, insertions=1, deletions=0, stat="file", patch="+x"
        )
        actor.renderer.render_diff = AsyncMock(side_effect=RuntimeError("PRIVATE DETAIL"))
    else:
        actor.manager.check_upload.return_value = t.Upload(path="notes.md", name="notes.md")
        actor.context.upload = AsyncMock(side_effect=RuntimeError("PRIVATE DETAIL"))
    mid = await user_message(b, fake, command)
    replies = [m["content"] for i, m in fake.messages.items() if i > mid]
    assert len(replies) == 1 and "try !" in replies[0]
    assert actor.row["state"] == state
    assert "Workspace command" in caplog.text and "PRIVATE DETAIL" not in caplog.text


@pytest.mark.parametrize("status", [400, 429, 503])
async def test_file_upload_http_failure_and_retry(bridge, status, tmp_path, monkeypatch):
    import time

    import httpx

    from zab.workspace.manager import LocalWorkspaceManager

    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    b.manager = actor.manager = LocalWorkspaceManager()
    await actor.context.workspace.write_bytes("notes.md", b"notes")
    monkeypatch.setenv("HOME", str(tmp_path))
    original = fake.transport.handler
    attempts = []
    reset = time.time() + 0.05

    async def respond(request):
        if request.url.path.endswith("/user_uploads"):
            attempts.append(bytes(request.content))
            if len(attempts) == 1:
                return httpx.Response(
                    status,
                    text="temporary failure",
                    headers={
                        "Retry-After": ".01",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset),
                    },
                )
            assert b.client.remaining == 0 and b.client.reset_at == reset
        return await original(request)

    fake.transport.handler = respond
    mid = await user_message(b, fake, "!file `~/notes.md`")
    replies = [m["content"] for i, m in fake.messages.items() if i > mid]
    assert actor.row["state"] == "running"
    assert len(replies) == 1
    if status == 400:
        assert "File upload failed" in replies[0] and not fake.uploads
        assert len(attempts) == 1
    else:
        assert replies[0].startswith("[notes.md](/user_uploads/")
        assert len(attempts) == 2 and len(fake.uploads) == 1
        assert b"notes" in attempts[0] and b"notes" in attempts[1]


async def test_file_label_cannot_notify_a_wildcard(bridge):
    from zab.workspace.manager import LocalWorkspaceManager

    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    b.manager = actor.manager = LocalWorkspaceManager()
    await actor.context.workspace.write_bytes("@**all**.txt", b"notes")
    mid = await user_message(b, fake, "!file `@**all**.txt`")
    reply = [m["content"] for i, m in fake.messages.items() if i > mid]
    assert len(reply) == 1 and "/user_uploads/" in reply[0]
    assert "@**all**" not in reply[0] and "@\u200b**all**" in reply[0]


async def restart_store_and_actors(b, tmp_path):
    for actor in b.actors.values():
        await actor.close()
    b.actors.clear()
    b.store.close()
    b.store = Store(tmp_path / "state")
    b.router.store = b.store
    catchup = Catchup(b)  # as start() does: before topic reconciliation
    await b.refresh_cards()
    await b.recover_pending_creations()
    await b.reconcile_messages(catchup=catchup)
    for actor in b.actors.values():
        await actor.inbox.join()


@pytest.mark.parametrize("boundary", ["before_actor", "during_promotion", "after_promotion"])
@pytest.mark.parametrize("prompt", ["durable followup", "!handover"])
async def test_claimed_followup_survives_crash_boundary(
    bridge, tmp_path, monkeypatch, boundary, prompt
):
    from dataclasses import replace

    b, fake, backend = bridge
    backend.capabilities = replace(backend.capabilities, steer=False)
    await user_message(b, fake, "@**Test Bot** first")
    old = b.actors["native"]
    original_delete = b.store.delete
    if boundary == "before_actor":
        old.runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old.runner
    elif boundary == "during_promotion":

        def crash_delete(key):
            original_delete(key)
            if key.startswith("pending_delivery:"):
                raise asyncio.CancelledError

        monkeypatch.setattr(b.store, "delete", crash_delete)
    else:
        old.react = AsyncMock(side_effect=asyncio.CancelledError)
    mid = await b.client.send(1, "topic", prompt)
    event = {"type": "message", "message": fake.messages[mid] | {"sender_id": 20}}
    await b.handle(event)
    if boundary != "before_actor":
        await old.inbox.join()
    assert b.store.rows("inbound", bot="bot", message_id=mid)
    assert bool(b.store.pending_deliveries("bot")) == (boundary != "after_promotion")
    monkeypatch.setattr(b.store, "delete", original_delete)
    await restart_store_and_actors(b, tmp_path)
    actor = b.actors["native"]
    assert [r["message_id"] for r in b.store.held(actor.row["id"])] == [mid]
    assert not b.store.pending_deliveries("bot")
    actor.put(
        t.SessionInfo(
            native_id="native",
            cwd=str(tmp_path),
            settings=t.SessionSettings(),
            resume_hint="resume",
            last_turn=t.TurnSummary(
                turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="first done"
            ),
        )
    )
    await actor.inbox.join()
    await asyncio.sleep(0)
    await actor.inbox.join()
    assert backend.start_turn.await_count == 2
    from zab.core.commands import HANDOVER

    assert backend.start_turn.call_args.args[1].text == (
        HANDOVER if prompt == "!handover" else prompt
    )
    await b.handle(event)  # Duplicate replay cannot start or steer a second copy.
    await actor.inbox.join()
    assert backend.start_turn.await_count == 2
    assert not b.store.held(actor.row["id"])
    b.store.close()


@pytest.mark.parametrize("boundary", ["before_creation", "after_binding"])
async def test_first_prompt_survives_crash_before_actor(bridge, tmp_path, monkeypatch, boundary):
    import zab.core.bridge as module

    b, fake, backend = bridge
    if boundary == "before_creation":
        target, name = module, "create_session"
        replacement = AsyncMock(side_effect=asyncio.CancelledError)
    else:
        target, name = b, "make_actor"

        def replacement(*args):
            raise asyncio.CancelledError

    original = getattr(target, name)
    monkeypatch.setattr(target, name, replacement)
    mid = await b.client.send(1, "topic", "@**Test Bot** !new --model test-model durable first")
    with pytest.raises(asyncio.CancelledError):
        await b.handle(
            {
                "type": "message",
                "message": fake.messages[mid] | {"sender_id": 20},
                "flags": ["mentioned"],
            }
        )
    assert b.store.pending_deliveries("bot")[0]["message_id"] == mid
    monkeypatch.setattr(target, name, original)
    await restart_store_and_actors(b, tmp_path)
    actor = b.actors["native"]
    actor.put(backend.create_session.return_value)
    await actor.inbox.join()
    await asyncio.sleep(0)
    await actor.inbox.join()
    assert backend.create_session.await_count == 1
    assert backend.start_turn.await_count == 1
    assert backend.start_turn.call_args.args[1].text == "durable first"
    assert not b.store.pending_deliveries("bot")
    b.store.close()


async def test_claim_and_pending_payload_roll_back_together(bridge, monkeypatch):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** first")
    original = b.store.set

    def crash(key, value):
        original(key, value)
        if key.startswith("pending_delivery:"):
            raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(b.store, "set", crash)
    mid = await b.client.send(1, "topic", "never committed")
    event = {"type": "message", "message": fake.messages[mid] | {"sender_id": 20}}
    with pytest.raises(RuntimeError, match="simulated crash"):
        await b.handle(event)
    assert not b.store.rows("inbound", bot="bot", message_id=mid)
    assert not b.store.pending_deliveries("bot")
    monkeypatch.setattr(b.store, "set", original)
    await b.handle(event)
    await b.actors["native"].inbox.join()
    backend.steer.assert_awaited_once()


async def test_recovery_drops_staged_prompt_from_revoked_sender(bridge, tmp_path):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** first")
    actor = b.actors["native"]
    actor.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor.runner
    mid = await b.client.send(1, "topic", "no longer authorized")
    await b.handle({"type": "message", "message": fake.messages[mid] | {"sender_id": 20}})
    b.router.allowlist = frozenset({999})
    await restart_store_and_actors(b, tmp_path)
    assert not b.store.pending_deliveries("bot")
    assert not b.store.held(actor.row["id"])
    assert backend.start_turn.await_count == 1
    b.store.close()


async def test_pending_creation_survives_temporary_zulip_failure(bridge, monkeypatch):
    import httpx

    import zab.core.bridge as module

    b, fake, backend = bridge
    original = module.create_session
    monkeypatch.setattr(module, "create_session", AsyncMock(side_effect=asyncio.CancelledError))
    mid = await b.client.send(1, "topic", "@**Test Bot** durable first")
    with pytest.raises(asyncio.CancelledError):
        await b.handle(
            {
                "type": "message",
                "message": fake.messages[mid] | {"sender_id": 20},
                "flags": ["mentioned"],
            }
        )
    monkeypatch.setattr(module, "create_session", original)
    channel = b.channel
    monkeypatch.setattr(b, "channel", AsyncMock(side_effect=httpx.ConnectError("offline")))
    await b.recover_pending_creations()
    assert b.store.pending_deliveries("bot")[0]["message_id"] == mid
    backend.create_session.assert_not_awaited()
    monkeypatch.setattr(b, "channel", channel)
    await b.recover_pending_creations()
    await b.actors["native"].inbox.join()
    await asyncio.sleep(0)
    await b.actors["native"].inbox.join()
    backend.start_turn.assert_awaited_once()
    assert not b.store.pending_deliveries("bot")


@pytest.mark.parametrize("recovery_first", [False, True])
async def test_creation_recovery_serializes_with_live_prompt(bridge, recovery_first):
    b, fake, backend = bridge
    entered, release = asyncio.Event(), asyncio.Event()
    prepared = b.manager.prepare.return_value

    async def prepare(*args):
        entered.set()
        await release.wait()
        return prepared

    b.manager.prepare.side_effect = prepare
    mid = await b.client.send(1, "topic", "@**Test Bot** first")
    message = fake.messages[mid] | {"sender_id": 20}
    event = {"type": "message", "message": message, "flags": ["mentioned"]}
    if recovery_first:
        b.users[20] = {"is_bot": False, "full_name": "Owner"}
        b.router.route(message, flags=["mentioned"], sender_is_bot=False, private=True)
        first = asyncio.create_task(b.recover_pending_creations())
    else:
        first = asyncio.create_task(b.handle(event))
    await asyncio.wait_for(entered.wait(), 1)
    if recovery_first:
        other = await b.client.send(1, "topic", "@**Test Bot** second")
        event["message"] = fake.messages[other] | {"sender_id": 20}
        second = asyncio.create_task(b.handle(event))
    else:
        second = asyncio.create_task(b.recover_pending_creations())
    await asyncio.sleep(0)
    assert b.manager.prepare.await_count == 1
    release.set()
    await asyncio.gather(first, second)
    await b.actors["native"].inbox.join()
    backend.create_session.assert_awaited_once()
    assert len(b.store.rows("sessions")) == 1
    assert (
        len([m for m in fake.messages.values() if m["content"].startswith("**zab session**")]) == 1
    )
    assert not b.store.pending_deliveries("bot")


async def test_at18_queue_loss_replays_two_gap_messages_once(bridge):
    from zab.zulip.client import ZulipError

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** initial")
    actor = b.actors["native"]
    mids = []
    for text in ("gap one", "gap two"):
        mid = await b.client.send(1, "topic", text)
        fake.messages[mid]["sender_id"] = 20
        mids.append(mid)
    old_queue = fake._queue_id
    b.client.events = AsyncMock(
        side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), asyncio.CancelledError()]
    )
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    await actor.inbox.join()
    assert fake._queue_id == old_queue + 1
    assert [c.args[2].text for c in backend.steer.await_args_list] == ["gap one", "gap two"]
    # The same messages arriving on the new event queue must not deliver again.
    for mid in mids:
        await b.handle({"type": "message", "message": fake.messages[mid]})
    await actor.inbox.join()
    assert backend.steer.await_count == 2
    assert len([r for r in b.store.rows("inbound") if r["message_id"] in mids]) == 2


async def test_text_upload_copies_are_cached_by_name_and_send_the_checked_bytes(bridge):
    """Codex review on #124: `!file x.lean` and the answer's `x.lean.txt` copy are distinct
    uploads, and a text upload sends the bytes the renderer validated, not a second read."""
    import posixpath

    from zab.workspace.manager import LocalWorkspaceManager

    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    b.manager = actor.manager = LocalWorkspaceManager()
    ws = actor.context.workspace
    checked = b"theorem t : True := trivial\n"
    await ws.write_bytes("Solution.lean", checked)
    path = await ws.realpath(posixpath.join(ws.cwd, "Solution.lean"))
    plain = await actor.context.upload(t.Upload(path=path, name="Solution.lean"))
    copy = await actor.context.upload(t.Upload(path=path, name="Solution.lean.txt", data=checked))
    assert plain != copy and copy.endswith("/Solution.lean.txt") and len(fake.uploads) == 2
    await ws.write_bytes("Solution.lean", b"\x00" * 2048)  # changed after the check
    again = await actor.context.upload(t.Upload(path=path, name="Solution.lean.txt", data=checked))
    assert again == copy and len(fake.uploads) == 2  # the checked bytes, cached by name
    assert [data for data, _ in fake.uploads.values()] == [checked, checked]
    assert [mime for _, mime in fake.uploads.values()][1] == "text/plain"


@pytest.mark.parametrize("peer_live", [False, True])
async def test_detach_cleanup_hint_respects_other_live_worktree_users(bridge, peer_live):
    from zab.core.creation import create_session

    b, fake, _ = bridge
    await _repo_with_worktree_peer(b)
    actor, _ = await create_session(b, _msg(), "!new --worktree hello", {"name": "zab-dev"})
    if not peer_live:
        b.store.update("sessions", {"id": "peer-session"}, state="detached")
    await actor.command("!detach")
    text = list(fake.messages.values())[-1]["content"]
    assert ("git worktree remove" in text) is not peer_live


async def test_overlap_note_includes_different_subdirectories_of_one_worktree(bridge):
    from zab.core.creation import create_session

    b, _, _ = bridge
    _, peer = await _repo_with_worktree_peer(b)
    actor, _ = await create_session(b, _msg(), "!new --worktree hello", {"name": "zab-dev"})
    b.store.update("sessions", {"id": "peer-session"}, workspace=peer.cwd + "/different-subdir")
    b.store.insert(
        "turns", session_id="peer-session", trigger_msg_ids="[]", state="running", started_at=10.0
    )
    assert b.store.overlapping_bots(actor.row["id"], 5, 15) == ["claude"]


@pytest.mark.parametrize("symlink", [False, True])
async def test_do_not_reuse_peer_worktree_outside_selected_project(bridge, tmp_path, symlink):
    from zab.core.creation import topic_worktree

    b, _, _ = bridge
    _, peer = await _repo_with_worktree_peer(b)
    other = tmp_path / "other-project"
    await asyncio.to_thread(other.mkdir)
    if symlink:
        await asyncio.to_thread((other / ".zab" / "worktrees").mkdir, parents=True)
        link = other / ".zab" / "worktrees" / "escaped"
        await asyncio.to_thread(link.symlink_to, peer.repo_root, target_is_directory=True)
        b.store.update("sessions", {"id": "peer-session"}, repo_root=str(link))
    assert await topic_worktree(b, _msg(), str(other)) is None
