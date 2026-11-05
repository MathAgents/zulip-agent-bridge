import asyncio
import time
from dataclasses import replace
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest

from zab.core import types as t
from zab.core.session import PAUSED, Delivery, SessionActor
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_clock import FrozenClock
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import GAP, MAX_EDITS, ZulipClient, ZulipError
from zab.zulip.poster import Poster

pytestmark = pytest.mark.integration


@pytest.fixture
async def actor(tmp_path):
    store = Store(tmp_path)
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    card = await client.send(1, "topic", "**zab session**")
    store.insert(
        "sessions",
        id="s",
        bot="b",
        backend="c",
        native_id="native",
        card_message_id=card,
        stream_id=1,
        topic="topic",
        workspace=str(tmp_path),
        workspace_mode="shared",
        repo_root=str(tmp_path),
        settings_json="{}",
        state="idle",
        created_at=0,
        updated_at=0,
    )
    backend = AsyncMock()
    backend.capabilities = t.Capabilities(
        steer=False, approvals=True, live_attach=True, rename=True
    )
    backend.start_turn.side_effect = ["turn1", "turn2", "turn3"]
    manager = AsyncMock()
    manager.snapshot.return_value = None
    poster = Poster(client)
    ctx = t.RenderContext(
        workspace=LocalWorkspace(str(tmp_path)),
        max_message_length=10000,
        max_upload_bytes=100000,
        upload=None,
    )
    instance = SessionActor(
        store.rows("sessions")[0],
        store,
        backend,
        poster,
        MarkdownRenderer(),
        manager,
        ctx,
        asyncio.Semaphore(1),
        machine="test",
        resume="codex resume",
    )
    yield instance, fake, backend
    await instance.close()
    await poster.close()
    await client.close()
    store.close()


async def send(actor, fake, text):
    mid = await actor.poster.client.send(1, "topic", text)
    actor.put(Delivery(mid, 20, "Tester", text))
    # Synchronize through queue barriers, including the slot-acquisition task.
    await actor.inbox.join()
    await asyncio.sleep(0)
    await actor.inbox.join()
    return mid


async def until(condition, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition()


async def test_hold_complete_and_resume_one_coalesced_turn(actor):
    a, fake, backend = actor
    await send(a, fake, "first")
    assert a.row["state"] == "running"
    await send(a, fake, "second")
    await send(a, fake, "third")
    assert len(a.store.held("s")) == 2
    a.put(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.COMPLETED,
            final_text="answer @**all**",
        )
    )
    await a.inbox.join()
    await asyncio.sleep(0)
    await a.inbox.join()
    assert backend.start_turn.await_count == 2
    assert "second" in backend.start_turn.call_args.args[1].text
    assert "third" in backend.start_turn.call_args.args[1].text
    assert a.turn["native_turn_id"] == "turn2"
    assert any("`@\u200b**all**`" in m["content"] for m in fake.messages.values())
    assert a.store.rows("turns", native_turn_id="turn1")[0]["final_message_ids"]


async def test_replayed_message_with_existing_reaction_still_starts(actor):
    # A restart replays a held message that already got its 👀 before the bridge died.
    a, fake, backend = actor
    mid = await a.poster.client.send(1, "topic", "replayed")
    await a.react(mid, "eyes")
    a.put(Delivery(mid, 20, "Tester", "replayed"))
    await a.inbox.join()
    await asyncio.sleep(0)
    await a.inbox.join()
    assert a.row["state"] == "running"
    assert backend.start_turn.await_count == 1
    assert not any(m["content"].startswith("⚠️") for m in fake.messages.values())
    assert len(fake.messages[mid]["reactions"]) == 1


async def test_other_reaction_errors_still_raise(actor):
    a, _, _ = actor
    with pytest.raises(ZulipError):
        await a.react(999, "eyes")


async def test_approval_stop_keeps_held_and_deduplicates_completion(actor):
    a, fake, backend = actor
    await send(a, fake, "first")
    a.put(
        t.ApprovalRequested(
            session_id="native",
            turn_id="turn1",
            request_id="r",
            kind="command",
            summary="echo hello",
        )
    )
    await a.inbox.join()
    assert a.row["state"] == "awaiting_approval"
    await send(a, fake, "later")
    a.put(("!stop",))
    await a.inbox.join()
    backend.respond.assert_awaited_with("r", t.ApprovalDecision.CANCEL)
    backend.interrupt.assert_awaited_once_with("native")
    assert len(a.store.held("s")) == 1
    event = t.TurnCompleted(
        session_id="native", turn_id="turn1", status=t.TurnStatus.INTERRUPTED, final_text="stopped"
    )
    await a.complete(event, drain=False)
    await a.complete(event, drain=False)
    assert sum(m["content"] == "stopped" for m in fake.messages.values()) == 1


async def test_failed_start_keeps_held(actor):
    a, fake, backend = actor
    backend.start_turn.side_effect = t.BackendUnavailable("offline")
    await send(a, fake, "retry me")
    assert len(a.store.held("s")) == 1
    assert a.turn is None
    assert not a.slot_owned


async def test_recovery_finishes_unposted_turn_once(actor):
    from zab.core.recovery import recover

    a, fake, _backend = actor
    await send(a, fake, "first")
    info = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        last_turn=t.TurnSummary(
            turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="recovered answer"
        ),
    )
    await recover(a, info)
    await recover(a, info)
    assert sum(m["content"] == "recovered answer" for m in fake.messages.values()) == 1
    assert a.row["state"] == "idle"
    assert not a.store.rows("turns", state="running")


@pytest.mark.parametrize(
    "failure", ["render", "status", "post", "all_posts", "diff", "render_diff", "card"]
)
async def test_completion_failures_finalize_and_release_slot(actor, failure, caplog):
    a, fake, _backend = actor
    await send(a, fake, "first")
    error = ValueError("private output must not be logged or posted")
    if failure == "render":
        a.renderer.render_agent_text = AsyncMock(side_effect=error)
    elif failure in ("status", "card"):
        setattr(a, failure, AsyncMock(side_effect=error))
    elif failure in ("post", "all_posts"):
        original = a.post
        attempts = 0

        async def post(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1 or failure == "all_posts":
                raise error
            return await original(*args, **kwargs)

        a.post = post
    else:
        a.turn["start_snapshot"] = '{"ref":"before"}'
        if failure == "diff":
            a.manager.diff.side_effect = error
        else:
            a.manager.diff.return_value = t.DiffResult(
                files_changed=1, insertions=1, deletions=0, patch="+x", stat="1 file"
            )
            a.renderer.render_diff = AsyncMock(side_effect=error)
    event = t.TurnCompleted(
        session_id="native",
        turn_id="turn1",
        status=t.TurnStatus.COMPLETED,
        final_text="final answer",
    )
    await a.complete(event, drain=False)
    await a.complete(event, drain=False)
    pending = failure in ("status", "all_posts")
    assert bool(a.turn) is pending and bool(a.completion) is pending
    assert not a.slot_owned and not a.slots.locked()
    expected = "delivery_pending" if pending else "completed"
    assert a.store.rows("turns", native_turn_id="turn1")[0]["state"] == expected
    transcript = "\n".join(m["content"] for m in fake.messages.values())
    assert "private output" not in transcript + caplog.text
    if failure in ("diff", "render_diff"):  # each marked as the bridge's (#677)
        assert a.note("No diff available for this turn.") in transcript
        assert "final answer" in transcript
    elif failure == "render":
        assert a.note("Unable to format this answer. View it in the terminal.") in transcript
    elif failure in ("status", "post"):
        assert a.note("Answer delivery is pending; use !status to retry.") in transcript
    assert "ValueError" in caplog.text


async def test_cancelled_completion_releases_slot(actor):
    a, fake, _ = actor
    await send(a, fake, "first")
    started = asyncio.Event()

    async def render(*args):
        started.set()
        await asyncio.Event().wait()

    a.renderer.render_agent_text = render
    task = asyncio.create_task(
        a.complete(
            t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert a.turn is not None and not a.slot_owned
    assert not a.slots.locked()
    assert a.store.rows("turns", native_turn_id="turn1")[0]["state"] == "running"


async def test_stale_stop_timeout_cannot_finish_new_turn(actor):
    from zab.core.session import StopTimeout

    a, fake, _backend = actor
    await send(a, fake, "first")
    await a.command("!stop")
    old_timer = a.stop_timer
    await a.command("!stop")
    await asyncio.sleep(0)
    assert old_timer.cancelled()
    a.put(StopTimeout("old-turn"))
    await a.inbox.join()
    assert a.turn["native_turn_id"] == "turn1"


async def test_slot_acquisition_does_not_replace_external_turn(actor):
    a, fake, backend = actor
    await a.slots.acquire()
    await send(a, fake, "held while waiting")
    assert a.turn is None
    await a.event(t.TurnStarted(session_id="native", turn_id="external"))
    a.slots.release()
    await asyncio.sleep(0)
    await a.inbox.join()
    backend.start_turn.assert_not_awaited()
    assert a.turn["native_turn_id"] == "external"
    assert len(a.store.held("s")) == 1


async def test_large_approval_preserves_controls_within_limit(actor):
    a, fake, _backend = actor
    await send(a, fake, "first")
    await a.event(
        t.ApprovalRequested(
            session_id="native",
            turn_id="turn1",
            request_id="huge",
            kind="file_change",
            summary="edit files",
            detail="x" * 50000,
        )
    )
    row = a.store.rows("approvals", request_id="huge")[0]
    text = fake.messages[row["message_id"]]["content"]
    assert len(text) <= 9800
    assert "👍 approve" in text


async def test_recovery_adopts_newer_terminal_turn_before_draining(actor):
    from zab.core.recovery import recover

    a, fake, backend = actor
    await send(a, fake, "first")
    await send(a, fake, "held")
    info = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        active_turn_id="terminal-new",
        last_turn=t.TurnSummary(
            turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="old final"
        ),
    )
    await recover(a, info)
    await asyncio.sleep(0)
    await a.inbox.join()
    assert a.turn["native_turn_id"] == "terminal-new"
    assert len(a.store.held("s")) == 1
    assert backend.start_turn.await_count == 1


async def test_restored_actor_holds_input_until_backend_summary(actor):
    from zab.core.recovery import recover

    a, fake, backend = actor
    a.ready = False
    await send(a, fake, "during reconnect")
    backend.start_turn.assert_not_awaited()
    assert len(a.store.held("s")) == 1
    await recover(
        a,
        t.SessionInfo(
            native_id="native",
            cwd=a.row["workspace"],
            settings=t.SessionSettings(),
            resume_hint="resume",
        ),
    )
    await asyncio.sleep(0)
    await a.inbox.join()
    backend.start_turn.assert_awaited_once()


async def test_public_move_completion_releases_slot_without_posting(actor):
    a, fake, _backend = actor
    await send(a, fake, "first")
    a.state(state="readonly", stream_id=2)
    before = len(fake.requests)
    await a.status()
    await a.command("!stop")
    await a.event(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.COMPLETED,
            final_text="private answer",
        )
    )
    assert a.row["state"] == "readonly"
    assert a.turn is None and not a.slot_owned
    assert len(fake.requests) == before


async def test_terminal_turn_resets_actions_and_settings_persist(actor):
    import json

    a, _fake, backend = actor
    a.actions["old"] = t.ItemInfo(
        item_id="old", kind=t.ItemKind.COMMAND, title="old command", status=t.ItemStatus.COMPLETED
    )
    await a.event(t.TurnStarted(session_id="native", turn_id="terminal"))
    assert not a.actions
    await a.command("!model")
    backend.update_settings.assert_not_awaited()
    backend.update_settings.side_effect = lambda sid, settings: settings
    await a.command("!model chosen-model")
    assert json.loads(a.row["settings_json"])["model"] == "chosen-model"
    assert (
        json.loads(a.store.rows("sessions", id="s")[0]["settings_json"])["model"] == "chosen-model"
    )


async def test_public_move_during_start_cannot_reactivate_session(actor):
    a, fake, backend = actor

    async def start(*args):
        a.state(state="readonly", stream_id=2)
        return "raced"

    backend.start_turn.side_effect = start
    await send(a, fake, "first")
    assert a.row["state"] == "readonly"
    assert all(m["stream_id"] == 1 for m in fake.messages.values())
    await a.event(
        t.TurnCompleted(session_id="native", turn_id="raced", status=t.TurnStatus.COMPLETED)
    )
    assert not a.slot_owned


async def test_disconnected_steer_keeps_input_durably(actor):
    a, _, backend = actor
    a.react = AsyncMock()
    await a.deliver(Delivery(10, 20, "User", "first"))
    await asyncio.sleep(0)
    await a.inbox.join()
    backend.capabilities = t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True)
    backend.steer.side_effect = t.BackendUnavailable("offline")
    with pytest.raises(t.BackendUnavailable):
        await a.deliver(Delivery(11, 20, "User", "keep this"))
    assert [m["content"] for m in a.store.held("s")] == ["keep this"]
    assert not a.ready


async def test_identical_completion_edits_each_new_status(actor, monkeypatch):
    a, fake, _ = actor
    a.react = AsyncMock()
    monkeypatch.setattr("zab.core.session.view.status", lambda *args, **kwargs: "✅ Completed")
    for mid, tid in [(10, "turn1"), (11, "turn2")]:
        await a.deliver(Delivery(mid, 20, "User", "hello"))
        await asyncio.sleep(0)
        await a.inbox.join()
        status_id = a.turn["status_message_id"]
        await a.complete(
            t.TurnCompleted(
                session_id="native", turn_id=tid, status=t.TurnStatus.COMPLETED, final_text="done"
            )
        )
        assert fake.messages[status_id]["content"] == "✅ Completed"


async def test_stop_timeout_probes_active_turn_without_starting_held_input(actor):
    from zab.core.session import StopTimeout

    a, fake, backend = actor
    await send(a, fake, "first")
    await send(a, fake, "held")
    a.put(StopTimeout("turn1"))
    await a.inbox.join()
    assert a.row["state"] == "error" and not a.turn
    assert a.slot_owned and a.slots.locked()
    a.schedule()
    assert a.waiter is None
    backend.open_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        active_turn_id="turn1",
    )
    await a.command("!status")
    assert a.turn["native_turn_id"] == "turn1"
    assert len(a.store.held("s")) == 1
    backend.start_turn.assert_awaited_once()

    await a.complete(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.COMPLETED,
            final_text="AFTER_TIMEOUT",
        ),
        drain=False,
    )
    assert sum(m["content"] == "AFTER_TIMEOUT" for m in fake.messages.values()) == 1


async def test_terminal_turn_records_snapshot_for_final_diff(actor):
    import json

    a, _, _ = actor
    a.manager.snapshot.return_value = t.Snapshot(ref="before-terminal")
    a.manager.diff.return_value = t.DiffResult(
        files_changed=0, insertions=0, deletions=0, patch="", stat=""
    )
    await a.event(t.TurnStarted(session_id="native", turn_id="terminal"))
    assert json.loads(a.turn["start_snapshot"])["ref"] == "before-terminal"
    await a.complete(
        t.TurnCompleted(session_id="native", turn_id="terminal", status=t.TurnStatus.COMPLETED)
    )
    assert a.manager.diff.call_args.args[1].ref == "before-terminal"


async def test_the_final_answer_is_rendered_with_the_files_the_turn_touched(actor):
    """Issue #36 (Drew, Sun 16:30): the diff is computed before the answer is rendered, so its
    file references can prefer the files this turn touched (SPEC §11.3); posted after it."""
    a, fake, _ = actor
    await send(a, fake, "first")
    a.turn["start_snapshot"] = '{"ref":"before"}'
    a.manager.diff.return_value = t.DiffResult(
        files_changed=1,
        insertions=1,
        deletions=0,
        patch="+x",
        stat="1 file",
        changed=("README.md",),
        new_untracked=("fermat/README.md",),
    )
    seen = []
    render = a.renderer.render_agent_text

    async def spy(text, ctx):
        seen.append(ctx.turn_files)
        return await render(text, ctx)

    a.renderer.render_agent_text = spy
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="ok"
        ),
        drain=False,
    )
    assert seen == [frozenset({"README.md", "fermat/README.md"})]
    assert a.manager.diff.await_count == 1  # once, for the links and the diff message
    assert a.context.turn_files is None  # only this answer, never later renders


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_answer_and_diff_share_one_lazy_snapshot(actor, enabled):
    """rc6 (SPEC §10.7, Drew on #36 Mon 04:01): with refserve_url set, the answer and its diff
    get one provider; the snapshot is published on first use only, once, under
    "<session id>/<turn row>" for the server's owner, and linked at {url}/{repo}/{sha}."""
    a, fake, _ = actor
    await send(a, fake, "first")
    a.refserve_url = "https://files.mathagents.org/" if enabled else ""
    a.turn["start_snapshot"] = '{"ref":"before"}'
    a.manager.diff.return_value = t.DiffResult(
        files_changed=1, insertions=1, deletions=0, patch="+x", stat="1 file"
    )
    a.manager.publish_snapshot.return_value = t.SnapshotRef(sha="d" * 40, repo="alice")
    seen, got = [], []
    render_text, render_diff = a.renderer.render_agent_text, a.renderer.render_diff

    async def spy_text(text, ctx):
        seen.append(("answer", ctx.snapshot))
        if ctx.snapshot is not None:
            assert a.manager.publish_snapshot.await_count == 0  # nothing before first use
            got.extend([await ctx.snapshot(), await ctx.snapshot()])
        return await render_text(text, ctx)

    async def spy_diff(diff, ctx):
        seen.append(("diff", ctx.snapshot))
        if ctx.snapshot is not None:
            got.append(await ctx.snapshot())
        return await render_diff(diff, ctx)

    a.renderer.render_agent_text, a.renderer.render_diff = spy_text, spy_diff
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="ok"
        ),
        drain=False,
    )
    if enabled:
        expected = t.PublishedSnapshot(
            sha="d" * 40, url="https://files.mathagents.org/alice/" + "d" * 40
        )
        assert got == [expected] * 3 and seen[0][1] is seen[1][1]  # one provider per turn
        assert a.manager.publish_snapshot.await_count == 1  # one snapshot, one push
        call = a.manager.publish_snapshot.call_args
        assert call.args[1] == f"{a.row['id']}/{a.store.rows('turns')[0]['id']}"
        assert call.kwargs == {"github_owner": "MathAgents"}
    else:
        assert seen == [("answer", None), ("diff", None)]
        a.manager.publish_snapshot.assert_not_awaited()
    assert a.context.snapshot is None  # only this answer


async def test_a_turn_that_links_nothing_publishes_nothing(actor):
    """ "what is 2+2" → "4" leaves no trace on GitHub (Drew): the provider is never awaited."""
    a, fake, _ = actor
    await send(a, fake, "first")
    a.refserve_url = "https://files.mathagents.org"
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="4"
        ),
        drain=False,
    )
    a.manager.publish_snapshot.assert_not_awaited()


async def test_a_failing_or_absent_snapshot_renders_as_without_one(actor):
    a, fake, _ = actor
    await send(a, fake, "first")
    a.refserve_url = "https://files.mathagents.org"
    a.manager.publish_snapshot.side_effect = [RuntimeError("boom"), None]
    first, second = a.snapshot_provider(), a.snapshot_provider()
    assert await first() is None and await first() is None  # memoized, even a failure
    assert await second() is None  # None from the manager: no snapshot either
    assert a.manager.publish_snapshot.await_count == 2


async def test_recovered_turn_reserves_busy_slot_without_blocking_completion(actor):
    from zab.core.recovery import recover
    from zab.core.session import StopTimeout

    a, fake, _ = actor
    await send(a, fake, "first")
    a.put(StopTimeout("turn1"))
    await a.inbox.join()
    # Reproduce an older persisted timeout while another session has the slot.
    a.release()
    await a.slots.acquire()
    info = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        active_turn_id="turn1",
    )
    await asyncio.wait_for(recover(a, info), 1)
    assert a.slot_reservation and not a.slot_owned
    a.slots.release()
    await asyncio.sleep(0)
    assert a.slot_owned and a.slots.locked()
    await a.complete(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED),
        drain=False,
    )
    assert not a.slot_owned and not a.slots.locked()


@pytest.mark.parametrize("state", ["error", "readonly", "detached"])
async def test_late_stop_completion_releases_retained_slot_without_probe(actor, state):
    from zab.core.session import StopTimeout

    a, fake, _ = actor
    await send(a, fake, "first")
    a.put(StopTimeout("turn1"))
    await a.inbox.join()
    assert a.slot_owned and not a.turn
    a.state(state=state)
    before = len(fake.messages)
    a.put(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.COMPLETED,
            final_text="LATE_DONE",
        )
    )
    await a.inbox.join()
    assert not a.slot_owned and not a.slots.locked()
    assert a.unconfirmed_turn is None and a.turn is None
    if state == "error":
        assert sum(m["content"] == "LATE_DONE" for m in fake.messages.values()) == 1
    else:
        assert len(fake.messages) == before


async def test_claude_pgid_and_session_id_survive_core_events(actor):
    from zab.core.creation import CONTEXT
    from zab.core.recovery import session_spec

    a, fake, _ = actor
    await send(a, fake, "first")
    a.resume = "claude --resume native"
    await a.event(t.TurnStarted(session_id="native", turn_id="turn1", pgid=12345))
    assert a.store.rows("turns", native_turn_id="turn1")[0]["pgid"] == 12345
    await a.event(t.SessionChanged(session_id="native", new_session_id="changed"))
    assert a.store.rows("sessions", id="s")[0]["native_id"] == "changed"
    assert a.resume == "claude --resume changed"
    assert session_spec(a).context_prompt == CONTEXT


async def test_claude_restart_edits_original_status_instead_of_posting_notice(actor, monkeypatch):
    from zab.core import processes
    from zab.core.recovery import recover

    a, fake, backend = actor
    backend.kind = "claude"
    backend.config.binary = "/isolated/claude"
    await send(a, fake, "first")
    a.turn["pgid"] = 12345
    status_id = a.turn["status_message_id"]
    messages_before = set(fake.messages)
    reaper = AsyncMock()
    monkeypatch.setattr(processes, "reap_claude_turn", reaper)
    await recover(
        a,
        t.SessionInfo(
            native_id="native",
            cwd=a.row["workspace"],
            settings=t.SessionSettings(),
            resume_hint="claude --resume native",
        ),
    )
    reaper.assert_awaited_once_with(12345, "native", "/isolated/claude")
    assert a.turn is None and not a.slot_owned
    assert fake.messages[status_id]["content"] == (
        "⏹ interrupted by bridge restart; send a message to continue"
    )
    assert set(fake.messages) == messages_before
    assert a.store.rows("turns", native_turn_id="turn1")[0]["state"] == "interrupted"


async def test_attachments_are_saved_and_passed_on_start_and_steer(actor):
    from pathlib import Path

    from zab.workspace.manager import LocalWorkspaceManager

    a, fake, backend = actor
    a.manager = LocalWorkspaceManager()
    a.manager.snapshot = AsyncMock(return_value=None)
    fake.uploads["1/ab/plot.png"] = (b"png decoy", "image/png")
    fake.uploads["1/ab/report:2026.csv"] = (b"x,y\n1,2\n", "text/csv")
    await send(
        a,
        fake,
        "read [plot](/user_uploads/1/ab/plot.png) and [data](/user_uploads/1/ab/report%3A2026.csv)",
    )
    supplied = backend.start_turn.call_args.args[1]
    assert [file.mime_type for file in supplied.attachments] == ["image/png", "text/csv"]
    assert all(
        "/.zab/inbox/" in file.path and file.path in supplied.text for file in supplied.attachments
    )
    assert await asyncio.to_thread(Path(supplied.attachments[1].path).read_bytes) == b"x,y\n1,2\n"
    from zab.backends.codex import CodexBackend

    assert CodexBackend._input(supplied)[1] == {
        "type": "localImage",
        "path": supplied.attachments[0].path,
    }
    backend.capabilities = t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True)
    await send(a, fake, "also [csv](/user_uploads/1/ab/report%3A2026.csv)")
    assert backend.steer.call_args.args[2].attachments[0].mime_type == "text/csv"


async def test_held_attachment_cache_survives_retry_without_redownload(actor):
    from zab.core.attachments import load_attachments
    from zab.workspace.manager import LocalWorkspaceManager

    a, fake, _ = actor
    a.manager = LocalWorkspaceManager()
    fake.uploads["1/a/x.csv"] = (b"x\n", "text/csv")
    a.store.hold("s", 100, "Tester", "[csv](/user_uploads/1/a/x.csv)")
    first = await load_attachments(a, [100])
    fake.uploads.clear()
    second = await load_attachments(a, [100])
    assert first == second
    assert sum(r[1] == "user_uploads/1/a/x.csv" for r in fake.requests) == 1


@pytest.mark.parametrize("failure", ["deleted", "oversized"])
async def test_rejected_attachment_does_not_block_later_input(actor, failure):
    from dataclasses import replace

    a, fake, backend = actor
    if failure == "oversized":
        fake.uploads["1/a/file.csv"] = (b"too large", "text/csv")
        a.context = replace(a.context, max_upload_bytes=2)
    await send(a, fake, "read [file](/user_uploads/1/a/file.csv)")
    assert backend.start_turn.await_count == 1
    assert not backend.start_turn.call_args.args[1].attachments
    assert not a.store.held("s")
    rejected = a.note("An attachment was rejected (")  # marked as the bridge's (#677)
    assert any(m["content"].startswith(rejected) for m in fake.messages.values())
    await send(a, fake, "next prompt")
    a.put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="done"
        )
    )
    await a.inbox.join()
    await asyncio.sleep(0)
    await a.inbox.join()
    assert backend.start_turn.await_count == 2
    assert backend.start_turn.call_args.args[1].text == "next prompt"


async def test_rejection_notice_retries_without_fetching_rejected_file(actor):
    from zab.core.attachments import load_attachments

    a, fake, _ = actor
    a.store.hold("s", 100, "Tester", "[file](/user_uploads/1/a/deleted.csv)")
    post = a.post
    a.post = AsyncMock(side_effect=RuntimeError("temporary posting failure"))
    with pytest.raises(RuntimeError):
        await load_attachments(a, [100])
    a.post = post
    assert await load_attachments(a, [100]) == []
    assert await load_attachments(a, [100]) == []
    assert sum(r[1] == "user_uploads/1/a/deleted.csv" for r in fake.requests) == 1
    assert sum("attachment was rejected" in m["content"] for m in fake.messages.values()) == 1


async def test_transient_download_error_remains_retryable(actor):
    import httpx

    from zab.core.attachments import load_attachments
    from zab.workspace.manager import LocalWorkspaceManager

    a, fake, _ = actor
    a.manager = LocalWorkspaceManager()
    fake.uploads["1/a/file.csv"] = (b"data", "text/csv")
    a.store.hold("s", 100, "Tester", "[file](/user_uploads/1/a/file.csv)")
    download = a.poster.client.download
    a.poster.client.download = AsyncMock(side_effect=httpx.ReadTimeout("transient"))
    with pytest.raises(httpx.ReadTimeout):
        await load_attachments(a, [100])
    a.poster.client.download = download
    assert len(await load_attachments(a, [100])) == 1
    assert not any("attachment was rejected" in m["content"] for m in fake.messages.values())


@pytest.mark.parametrize("render_fails_on_retry", [False, True])
async def test_partial_delivery_survives_restart_without_duplicate_parts(
    actor, render_fails_on_retry
):
    from zab.core.recovery import recover
    from zab.zulip.client import ZulipError

    a, fake, backend = actor
    await send(a, fake, "first")
    a.renderer.render_agent_text = AsyncMock(return_value=["part one", "part two"])
    original = a.post

    async def fail_second(content, *args):
        if content == "part two":
            raise ZulipError("SERVER_ERROR", 500)
        return await original(content, *args)

    a.post = fail_second
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="answer"
        )
    )
    assert a.store.rows("turns", native_turn_id="turn1")[0]["state"] == "delivery_pending"
    assert not a.slot_owned
    await a.close()
    restored = SessionActor(
        a.row.copy(),
        a.store,
        backend,
        a.poster,
        a.renderer,
        a.manager,
        a.context,
        a.slots,
        machine="test",
        resume="resume",
    )
    try:
        if render_fails_on_retry:
            restored.renderer.render_agent_text.side_effect = ValueError("render failure")
            await restored.complete(restored.completion)
            turn = a.store.rows("turns", native_turn_id="turn1")[0]
            assert turn["state"] == "delivery_pending"
            assert a.store.get(f"completion:{turn['id']}")["final_text"] == "answer"
            assert not restored.slot_owned
            assert (
                sum("Answer delivery is pending" in m["content"] for m in fake.messages.values())
                == 1
            )
            restored.renderer.render_agent_text.side_effect = None
        await recover(
            restored,
            t.SessionInfo(
                native_id="native",
                cwd=a.row["workspace"],
                settings=t.SessionSettings(),
                resume_hint="resume",
            ),
        )
        assert restored.turn is None and restored.completion is None
        assert sum(m["content"] == "part one" for m in fake.messages.values()) == 1
        assert sum(m["content"] == "part two" for m in fake.messages.values()) == 1
    finally:
        await restored.close()


async def test_cancellation_during_fallback_preserves_recovery(actor):
    a, fake, _ = actor
    await send(a, fake, "first")
    fallback = asyncio.Event()

    async def post(content, *args):
        if content == "answer":
            raise OSError("offline")
        fallback.set()
        await asyncio.Event().wait()

    a.post = post
    task = asyncio.create_task(
        a.complete(
            t.TurnCompleted(
                session_id="native",
                turn_id="turn1",
                status=t.TurnStatus.COMPLETED,
                final_text="answer",
            )
        )
    )
    await fallback.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not a.slot_owned and not a.slots.locked()
    turn = a.store.rows("turns", native_turn_id="turn1")[0]
    assert turn["state"] == "running"
    assert a.store.get(f"completion:{turn['id']}")["final_text"] == "answer"


async def test_status_recovers_own_live_claude_child_without_reaping(actor, monkeypatch):
    from zab.core import processes

    a, fake, backend = actor
    backend.kind = "claude"
    await send(a, fake, "first")
    a.turn["pgid"] = 12345
    a.state(state="error")
    backend.open_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
    )
    monkeypatch.setattr(processes, "is_bridge_child", lambda pgid: pgid == 12345)
    reaper = AsyncMock()
    monkeypatch.setattr(processes, "reap_claude_turn", reaper)
    a.put(("!status",))
    await a.inbox.join()
    reaper.assert_not_called()
    assert a.turn and a.turn["native_turn_id"] == "turn1"
    assert a.row["state"] == "running" and a.slot_owned
    assert not any("interrupted by bridge restart" in m["content"] for m in fake.messages.values())


async def test_restart_reaps_changed_session_using_launch_time_id(actor, monkeypatch):
    from zab.core import processes
    from zab.core.recovery import recover

    a, fake, backend = actor
    backend.kind = "claude"
    backend.config.binary = "/isolated/claude"
    backend.start_turn.side_effect = ["native:turn1"]
    await send(a, fake, "first")
    await a.event(t.TurnStarted(session_id="native", turn_id="native:turn1", pgid=12345))
    await a.event(t.SessionChanged(session_id="native", new_session_id="changed"))
    await a.close()
    restored = SessionActor(
        a.store.rows("sessions", id="s")[0],
        a.store,
        backend,
        a.poster,
        a.renderer,
        a.manager,
        a.context,
        a.slots,
        machine="test",
        resume="resume",
    )
    reaper = AsyncMock()
    monkeypatch.setattr(processes, "is_bridge_child", lambda _: False)
    monkeypatch.setattr(processes, "reap_claude_turn", reaper)
    try:
        await recover(
            restored,
            t.SessionInfo(
                native_id="changed",
                cwd=a.row["workspace"],
                settings=t.SessionSettings(),
                resume_hint="claude --resume changed",
            ),
        )
        reaper.assert_awaited_once_with(12345, "native", "/isolated/claude")
        assert restored.turn is None
    finally:
        await restored.close()


async def test_recovery_uses_matching_last_turn_before_orphan_cleanup(actor, monkeypatch):
    from zab.core import processes
    from zab.core.recovery import recover

    a, fake, backend = actor
    backend.kind = "claude"
    await send(a, fake, "first")
    a.turn["pgid"] = 12345
    reaper = AsyncMock()
    monkeypatch.setattr(processes, "reap_claude_turn", reaper)
    await recover(
        a,
        t.SessionInfo(
            native_id="native",
            cwd=a.row["workspace"],
            settings=t.SessionSettings(),
            resume_hint="resume",
            last_turn=t.TurnSummary(
                turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="finished answer"
            ),
        ),
    )
    reaper.assert_not_called()
    assert a.turn is None
    assert a.store.rows("turns", native_turn_id="turn1")[0]["state"] == "completed"
    assert sum(m["content"] == "finished answer" for m in fake.messages.values()) == 1
    assert not any("interrupted by bridge restart" in m["content"] for m in fake.messages.values())


@pytest.mark.parametrize("kind", ["codex", "claude"])
async def test_recovery_metadata_stays_codex_only(actor, kind):
    import json

    from zab.core.recovery import session_spec

    a, fake, backend = actor
    backend.kind = kind
    await send(a, fake, "first")
    a.row["settings_json"] = json.dumps(
        {
            "extra": {
                "owned_turn_ids": ["stale"],
                "client_ids": ["old"],
                "keep": "value",
            }
        }
    )
    a.store.set(f"client_ids:{a.row['id']}", ["new"])
    extra = session_spec(a).settings.extra
    if kind == "claude":
        stored = a.store.rows("sessions", id=a.row["id"])[0]
        assert json.loads(stored["settings_json"])["extra"] == {"keep": "value"}
    assert extra == (
        {"owned_turn_ids": ["turn1"], "client_ids": ["new"], "keep": "value"}
        if kind == "codex"
        else {"keep": "value"}
    )


async def test_restart_notice_retained_on_delivery_failure_then_deleted(actor):
    from zab.core.recovery import recover

    a, fake, backend = actor
    backend.kind = "claude"
    await send(a, fake, "first")
    key = f"restart_notice:{a.turn['id']}"
    status_id = a.turn["status_message_id"]
    edit = a.poster.edit

    async def fail_status(mid, *args, **kwargs):
        if mid == status_id:
            raise RuntimeError("offline")
        return await edit(mid, *args, **kwargs)

    a.poster.edit = AsyncMock(side_effect=fail_status)
    info = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
    )
    await recover(a, info)
    notice = "⏹ interrupted by bridge restart; send a message to continue"
    assert a.store.get(key) == notice
    assert a.turn and not a.slot_owned
    a.poster.edit = edit
    await recover(a, info)
    assert fake.messages[status_id]["content"] == notice
    assert not a.store.rows("kv", key=key)
    assert a.turn is None


@pytest.mark.parametrize("zulip", [False, True])
async def test_actor_logs_safe_error_metadata_before_warning(actor, caplog, zulip):
    from zab.zulip.client import ZulipError

    a, fake, _ = actor
    error = ZulipError("BAD_REQUEST", 403) if zulip else ValueError("PRIVATE_REQUEST_CONTENT")
    error.args = ("PRIVATE_REQUEST_CONTENT",)
    a.manager.snapshot.side_effect = error
    post = a.post

    async def check_log_before_warning(content, *args, **kwargs):
        if content.startswith("⚠️"):
            assert "Session operation failed:" in caplog.text
        return await post(content, *args, **kwargs)

    a.post = check_log_before_warning
    await send(a, fake, "first")
    expected = "ZulipError code=BAD_REQUEST status=403" if zulip else "ValueError"
    assert "Session operation failed: " + expected in caplog.text
    assert "PRIVATE_REQUEST_CONTENT" not in caplog.text
    assert not any(r.exc_info for r in caplog.records)
    assert a.row["state"] == "error"
    assert any("⚠️" in m["content"] for m in fake.messages.values())


async def test_status_recovers_session_left_in_error_by_old_card_failure(actor):
    a, fake, backend = actor
    await send(a, fake, "first")
    original = a.row["card_message_id"]
    fake.refused_edits.add(original)
    a.state(state="error")
    backend.open_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        active_turn_id="turn1",
    )
    a.put(("!status",))
    await a.inbox.join()
    backend.open_session.assert_awaited_once()
    assert a.row["state"] == "running" and a.turn["native_turn_id"] == "turn1"
    assert a.row["card_message_id"] == original
    assert not any("⚠️" in m["content"] for m in fake.messages.values())


async def status_replies(a, fake):
    before = set(fake.messages)
    a.put(("!status",))
    await a.inbox.join()
    return [m["content"] for mid, m in fake.messages.items() if mid not in before]


async def test_status_replies_once_while_running(actor, monkeypatch):
    a, fake, _ = actor
    await send(a, fake, "first")
    await send(a, fake, "held one")
    await send(a, fake, "held two")
    a.actions = {
        str(i): t.ItemInfo(
            item_id=str(i), kind=t.ItemKind.COMMAND, status="completed", title="an action"
        )
        for i in range(7)
    }
    monkeypatch.setattr("zab.core.session.time.time", lambda: a.turn["started_at"] + 252)
    mid = a.turn["status_message_id"]
    assert await status_replies(a, fake) == [
        "⏳ Running · 4m 12s · 7 actions · 2 messages held · "
        f"[status message](https://zulip.example/#narrow/channel/1/near/{mid})"
        " · git: off (user git config)"
    ]
    assert a.row["state"] == "running"


@pytest.mark.parametrize("completed", [False, True])
async def test_status_replies_once_while_idle(actor, completed):

    a, fake, _ = actor
    expected = "💤 Idle · 0 turns"
    if completed:
        await send(a, fake, "first")
        await a.complete(
            t.TurnCompleted(
                session_id="native",
                turn_id="turn1",
                status=t.TurnStatus.COMPLETED,
                final_text="done",
            )
        )
        ended = a.store.rows("turns", session_id="s")[0]["ended_at"]
        from datetime import UTC, datetime

        expected = (
            "💤 Idle · 1 turn · last turn <time:"
            + datetime.fromtimestamp(ended, UTC).isoformat()
            + ">"
        )
    assert await status_replies(a, fake) == [expected + " · git: off (user git config)"]
    assert a.row["state"] == "idle"


async def test_status_replies_once_while_waiting_for_approval(actor):
    a, fake, _ = actor
    await send(a, fake, "first")
    await a.event(
        t.ApprovalRequested(
            session_id="native",
            turn_id="turn1",
            request_id="r",
            kind="command",
            summary="run tests",
        )
    )
    assert await status_replies(a, fake) == ["⏸ Waiting for approval · git: off (user git config)"]
    assert a.row["state"] == "awaiting_approval"


async def test_status_replies_once_when_recovery_fails_without_private_details(actor):
    a, fake, backend = actor
    a.state(state="error")
    backend.open_session.side_effect = t.BackendUnavailable("PRIVATE_REQUEST @**all**\nsecret")
    assert await status_replies(a, fake) == [
        "⚠️ Error: BackendUnavailable · recovery attempted · git: off (user git config)"
    ]
    assert a.row["state"] == "error"


async def test_status_reply_reports_recovered_state(actor):
    a, fake, backend = actor
    await send(a, fake, "first")
    a.state(state="error")
    backend.open_session.return_value = t.SessionInfo(
        native_id="native",
        cwd=a.row["workspace"],
        settings=t.SessionSettings(),
        resume_hint="resume",
        active_turn_id="turn1",
    )
    replies = await status_replies(a, fake)
    assert len(replies) == 1 and "\n" not in replies[0]
    assert replies[0].startswith("⏳ Running") and replies[0].endswith(
        " · recovery attempted · git: off (user git config)"
    )
    assert a.row["state"] == "running"


async def test_status_reply_reports_pending_answer_delivery(actor):
    a, fake, _ = actor
    await send(a, fake, "first")
    a.completion = t.TurnCompleted(
        session_id="native",
        turn_id="turn1",
        status=t.TurnStatus.COMPLETED,
        final_text="pending answer",
    )
    a.state(state="error")
    messages = await status_replies(a, fake)
    assert messages[0] == "pending answer"
    assert len(messages) == 2 and "\n" not in messages[1]
    assert messages[1].startswith("💤 Idle · 1 turn · last turn ")
    assert messages[1].endswith(" · pending answer delivery retried · git: off (user git config)")
    assert a.row["state"] == "idle" and a.completion is None


async def test_detached_resolution_cannot_consume_replacement_binding_approval(actor):
    a, fake, _ = actor
    await send(a, fake, "first")
    request = t.ApprovalRequested(
        session_id="native",
        turn_id="turn1",
        request_id="shared-request",
        kind="command",
        summary="review",
        detail="true",
        decisions=(t.ApprovalDecision.ACCEPT,),
    )
    await a.event(request)
    mid = a.store.rows("approvals", request_id=request.request_id)[0]["message_id"]
    old_card = await a.poster.client.send(1, "old-topic", "**zab session**")
    old_row = a.row | {
        "id": "old",
        "bot": "other-bot",
        "state": "detached",
        "card_message_id": old_card,
    }
    a.store.insert("sessions", **old_row)
    old = SessionActor(
        old_row,
        a.store,
        a.backend,
        a.poster,
        a.renderer,
        a.manager,
        a.context,
        a.slots,
        machine="test",
        resume="resume",
    )
    old.pending[request.request_id] = request
    resolved = t.ApprovalResolved(session_id="native", request_id=request.request_id)
    try:
        # Runtime fans out a shared backend's event; the detached bot may receive it first.
        await old.event(resolved)
        assert not old.pending
        assert a.store.rows("approvals", request_id=request.request_id)[0]["state"] == "pending"
        await a.event(resolved)
        assert not a.pending
        assert a.row["state"] == "running"
        assert fake.messages[mid]["content"] == "Approval: resolved elsewhere"
    finally:
        await old.close()


@pytest.mark.parametrize("peer", ["Codex · X", "Claude · X"])
async def test_coaddressed_input_explicitly_addresses_this_agent(actor, peer):
    a, _, backend = actor
    mid = await a.poster.client.send(1, "topic", "compare")
    a.store.set(f"addressed_peers:{a.row['bot']}:{mid}", [peer])
    a.put(Delivery(mid, 20, "Tester", "compare"))
    await a.inbox.join()
    await asyncio.sleep(0)
    await a.inbox.join()
    text = backend.start_turn.call_args.args[1].text
    assert text.startswith(f"[Addressed to you and to {peer}")
    assert "answer it yourself.]" in text and text.endswith("compare")


@pytest.mark.parametrize("kind,service", [("claude", "Claude"), ("codex", "ChatGPT")])
async def test_auth_failure_posts_only_subscription_guidance(actor, kind, service):
    a, fake, backend = actor
    backend.kind = kind
    await send(a, fake, "first")
    before = set(fake.messages)
    event = t.TurnCompleted(
        session_id="native",
        turn_id="turn1",
        status=t.TurnStatus.FAILED,
        final_text="PRIVATE_AUTH_OUTPUT",
        error=t.ErrorInfo(kind=t.ErrorKind.AUTH, message="PRIVATE_AUTH_ERROR"),
    )
    await a.complete(event)
    assert [m["content"] for mid, m in fake.messages.items() if mid not in before] == [
        f"⚠️ **Bridge** (not {kind.capitalize()}): This agent isn't connected to your {service} "
        "subscription yet: send me !login in a direct message."
    ]
    assert a.turn is None and not a.slot_owned
    a.backend.doctor.return_value = [t.Check(name="codex:login", ok=False)]
    assert "!login" in (await status_replies(a, fake))[0]


async def test_a_lost_claude_refresh_race_is_reported_as_transient_not_login(actor):
    """The Claude backend's error after its retries (claude_stream.REFRESH_RACE_MESSAGE): one
    plain sentence, without "(no final message)" or the raw kind word (#307 item 2)."""
    from zab.backends.claude_stream import REFRESH_RACE_MESSAGE

    a, fake, backend = actor
    backend.kind = "claude"
    await send(a, fake, "first")
    before = set(fake.messages)
    error = t.ErrorInfo(
        kind=t.ErrorKind.BACKEND_UNAVAILABLE, message=REFRESH_RACE_MESSAGE, retry_after_s=60.0
    )
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.FAILED, error=error
        )
    )
    posted = [m["content"] for mid, m in fake.messages.items() if mid not in before]
    assert posted[0] == a.note(REFRESH_RACE_MESSAGE)  # the answer; then the failure ping
    assert all("backend_unavailable" not in p and "(no final message)" not in p for p in posted)
    sentence = REFRESH_RACE_MESSAGE
    assert sentence.startswith("Claude could not refresh its sign-in")
    assert sentence.endswith("send your message again in a minute.")
    assert sentence.count(".") == 1 and "\n" not in sentence  # one sentence
    assert "!login" not in sentence and "backend_unavailable" not in sentence


async def test_a_transient_error_after_some_text_keeps_the_text(actor):
    """Text from before the failure stays; the plain sentence follows, still without the kind."""
    a, fake, backend = actor
    backend.kind = "claude"
    await send(a, fake, "first")
    before = set(fake.messages)
    error = t.ErrorInfo(kind=t.ErrorKind.BACKEND_UNAVAILABLE, message="Try again in a minute.")
    await a.complete(
        t.TurnCompleted(
            session_id="native",
            turn_id="turn1",
            status=t.TurnStatus.FAILED,
            final_text="Half an answer",
            error=error,
        )
    )
    posted = "\n".join(m["content"] for mid, m in fake.messages.items() if mid not in before)
    assert "Half an answer\n\n" + a.note("Try again in a minute.") in posted
    assert "backend_unavailable" not in posted and "(no final message)" not in posted


async def test_other_failures_still_name_their_kind(actor):
    """A failed turn's error keeps its kind, marked as the bridge's words (#677)."""
    a, fake, backend = actor
    backend.kind = "codex"
    await send(a, fake, "first")
    before = set(fake.messages)
    error = t.ErrorInfo(kind=t.ErrorKind.NETWORK, message="ECONNRESET")
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.FAILED, error=error
        )
    )
    posted = "\n".join(m["content"] for mid, m in fake.messages.items() if mid not in before)
    assert "(no final message)\n\n⚠️ **Bridge** (not Codex): network: ECONNRESET" in posted


@pytest.mark.parametrize(
    "peer_start,peer_end,same_folder,expected",
    [
        (101, None, True, True),  # peer still running
        (95, 105, True, True),  # peer already finished
        (90, 100, True, False),  # adjacent, no overlap
        (120, 130, True, False),  # starts after this diff
        (101, None, False, False),  # another worktree
    ],
)
async def test_diff_warns_only_for_overlapping_workspace_turns(
    actor, monkeypatch, peer_start, peer_end, same_folder, expected
):
    a, fake, _ = actor
    await send(a, fake, "go")
    a.turn["started_at"] = 100
    a.turn["start_snapshot"] = '{"ref":"before"}'
    peer = a.row | {
        "id": "peer",
        "bot": "claude",
        "native_id": "peer-native",
        "card_message_id": 999,
        "topic": "another topic",
    }
    if not same_folder:
        peer["workspace"] += "/another-worktree"
        peer["repo_root"] += "/another-worktree"
    a.store.insert("sessions", **peer)
    a.store.insert(
        "turns",
        session_id="peer",
        native_turn_id="peer-turn",
        trigger_msg_ids="[]",
        state="running" if peer_end is None else "completed",
        started_at=peer_start,
        ended_at=peer_end,
    )
    a.store.set("bot_name:claude", "Claude · Owner")
    a.manager.diff.return_value = t.DiffResult(
        files_changed=1,
        insertions=1,
        deletions=0,
        stat="x.py | 1 +",
        changed=("x.py",),
        new_untracked=(),
        patch="diff --git a/x.py b/x.py\n+change",
    )
    monkeypatch.setattr("zab.core.session.time.time", lambda: 110)
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="answer"
        ),
        drain=False,
    )
    notes = [
        m["content"] for m in fake.messages.values() if "active in the same folder" in m["content"]
    ]
    assert bool(notes) is expected
    if expected:
        assert len(notes) == 1 and "Claude · Owner" in notes[0]
        assert "Changes" in notes[0]


STEER = t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True)


def taken_in(client_id, turn="turn1"):
    """What a backend reports when the agent takes an input in (#654)."""
    item = t.ItemInfo(
        item_id=f"user-{client_id}",
        kind=t.ItemKind.USER_MESSAGE,
        status=t.ItemStatus.COMPLETED,
        title="also this",
        text="also this",
        client_id=client_id,
    )
    return t.ItemCompleted(session_id="native", turn_id=turn, item=item)


def reactions(fake, mid):
    return [r["emoji_name"] for r in fake.messages[mid]["reactions"]]


async def test_a_steer_is_marked_read_when_the_agent_takes_it_in(actor):
    """#654: ↪️ when the message is written into the turn, ✅ beside it when the backend
    reports that the agent took it in. The prompt's report, another turn's, an unknown id and
    a repeat mark nothing more."""
    a, fake, backend = actor
    first = await send(a, fake, "first")
    prompt = backend.start_turn.call_args.args[1]
    backend.capabilities = STEER
    mid = await send(a, fake, "also this")
    steer = backend.steer.call_args.args[2]
    assert reactions(fake, mid) == ["arrow_right_hook"]
    for event in (taken_in(prompt.client_id), taken_in("other"), taken_in(steer.client_id, "t9")):
        a.put(event)
    await a.inbox.join()
    assert reactions(fake, mid) == ["arrow_right_hook"] and reactions(fake, first) == ["eyes"]
    a.put(taken_in(steer.client_id))
    a.put(taken_in(steer.client_id))
    await a.inbox.join()
    assert reactions(fake, mid) == ["arrow_right_hook", "check"]
    assert a.row["state"] == "running" and not a.steered


async def test_a_failed_read_mark_is_only_logged(actor, caplog):
    a, fake, backend = actor
    await send(a, fake, "first")
    backend.capabilities = STEER
    await send(a, fake, "also this")
    steer = backend.steer.call_args.args[2]
    a.react = AsyncMock(side_effect=ZulipError("BAD_REQUEST", 400))
    a.put(taken_in(steer.client_id))
    await a.inbox.join()
    assert a.react.await_args.args[1] == "check"
    assert a.row["state"] == "running"
    assert not any(m["content"].startswith("⚠️") for m in fake.messages.values())
    assert "Read mark failed" in caplog.text


async def test_the_status_shows_the_last_notes_and_no_running_time(actor):
    """#654: the last three notes, newest last, kept while the agent starts its next message;
    all of it afresh for the next turn. #754: a running step shows no time of its own, which
    would change the message only because time passed."""
    a, fake, _ = actor
    await send(a, fake, "first")

    def message(n, text, cls=t.ItemCompleted):
        item = t.ItemInfo(
            item_id=f"m{n}", kind=t.ItemKind.AGENT_MESSAGE, status=t.ItemStatus.COMPLETED,
            title=text, text=text,
        )  # fmt: skip
        return cls(session_id="native", turn_id="turn1", item=item)

    def command(status, cls):
        item = t.ItemInfo(item_id="c", kind=t.ItemKind.COMMAND, status=status, title="sleep 300")
        return cls(session_id="native", turn_id="turn1", item=item)

    for n, text in enumerate(["one", "two", "three", "four"]):
        a.put(message(n, text))
    a.put(message(9, "", t.ItemStarted))  # Codex starts each message empty
    a.put(command(t.ItemStatus.IN_PROGRESS, t.ItemStarted))
    await a.inbox.join()
    before = a.status_text()
    a.turn["started_at"] -= 200  # as if it started 200 s ago: nothing in the message changes
    a.put(command(t.ItemStatus.IN_PROGRESS, t.ItemStarted))
    await a.inbox.join()
    assert before.splitlines()[1:] == a.status_text().splitlines()[1:]
    assert a.status_text().splitlines()[1:] == [
        "- ⏳ ` sleep 300 `",
        "> two",
        ">",
        "> three",
        ">",
        "> four",
    ]
    a.put(command(t.ItemStatus.COMPLETED, t.ItemCompleted))
    await a.inbox.join()
    assert a.status_text().splitlines()[1] == "- ✅ ` sleep 300 `"
    a.put(t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED))
    await a.inbox.join()
    await send(a, fake, "second")
    assert a.turn["native_turn_id"] == "turn2"
    assert not (a.notes or a.actions or a.steered)


DECOY = "Q7xK" + "a1B2" * 7  # 32 alphanumerics: a fake zuliprc key


@pytest.mark.parametrize("field", ["summary", "detail"])
async def test_an_approval_is_redacted_before_it_is_cut(actor, field):
    """Review of #658, the defect class of its finding 1: a cut through `key=<32>` leaves a
    line the zuliprc rule (SPEC §11.6) no longer matches, so the text is redacted first."""
    a, fake, _backend = actor
    await send(a, fake, "first")
    a.context.max_message_length = 1000  # the detail is halved down to 510 characters
    secret = "a" * 490 + "\nkey=" + DECOY + "\n"  # the key's line ends at 527
    secret += "z" * (510 * 2**7 - len(secret) if field == "detail" else 9)
    texts = {"summary": "edit files", "detail": "diff"} | {field: secret}
    await a.event(
        t.ApprovalRequested(
            session_id="native", turn_id="turn1", request_id="r", kind="command", **texts
        )
    )
    text = fake.messages[a.store.rows("approvals", request_id="r")[0]["message_id"]]["content"]
    assert DECOY[:4] not in text and "👍 approve" in text


async def test_a_failed_turns_error_is_redacted_before_it_is_cut(actor):
    a, fake, _backend = actor
    await send(a, fake, "first")
    before = set(fake.messages)
    error = t.ErrorInfo(kind=t.ErrorKind.OTHER, message="x" * 990 + "\nkey=" + DECOY + "\nend")
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.FAILED, error=error
        )
    )
    posted = "\n".join(m["content"] for mid, m in fake.messages.items() if mid not in before)
    assert "other: xxx" in posted and DECOY[:4] not in posted


async def on_a_still_clock(a):
    """The actor's Zulip clock, still from now on (`FrozenClock`), its refresh timer on it, and
    the times and contents of the PATCHes sent, by message."""
    a.poster.client.budget.clock = clock = FrozenClock(time.time())
    a.timer.cancel()
    a.timer = asyncio.create_task(a._ticks())
    sent: dict[int, list[tuple[float, str]]] = {}
    request = a.poster.client.request

    async def timed(method, endpoint, **params):
        if method == "PATCH":
            sent.setdefault(int(endpoint.split("/")[1]), []).append((clock.now, params["content"]))
        return await request(method, endpoint, **params)

    a.poster.client.request = timed
    return clock, sent


async def later(a, clock, seconds):
    """`seconds` pass; the refresh timer and the held edits act on it, then the actor."""
    clock.advance(seconds)
    for _ in range(10):
        await asyncio.sleep(0)
    await a.inbox.join()


async def test_a_three_hour_busy_turn_edits_its_status_within_the_bounds(actor):
    """#754: a turn that runs three hours, with a new action every 20 s, a note every minute,
    a plan update every five, an approval, the periodic refresh (a minute in, then every
    `edit_interval`) and the model's own edit, edits its status message at most `MAX_EDITS`
    times, never less than `GAP` apart but for its summary; the last refresh says that updates
    are paused, and the summary still comes. The approval request gets its one edit."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "work for three hours")
    mid = a.turn["status_message_id"]
    clock.advance(a.turn["started_at"] - clock.now)  # the turn's start, on the still clock
    assert a.edit_interval == 300
    a.put(t.TurnStarted(session_id="native", turn_id="turn1", model="m", effort="high"))
    for second in range(0, 3 * 3600, 20):
        n = second // 20
        item = t.ItemInfo(
            item_id=f"c{n}", kind=t.ItemKind.COMMAND, status=t.ItemStatus.COMPLETED, title=f"s{n}"
        )
        a.put(t.ItemCompleted(session_id="native", turn_id="turn1", item=item))
        if second % 60 == 0:
            note = replace(item, item_id=f"m{n}", kind=t.ItemKind.AGENT_MESSAGE, text=f"note {n}")
            a.put(t.ItemCompleted(session_id="native", turn_id="turn1", item=note))
        if second % 300 == 0:
            plan = ((f"part {n}", "in_progress"), ("the rest", "pending"))
            a.put(t.PlanUpdated(session_id="native", turn_id="turn1", steps=plan))
        if second == 3600:
            a.put(
                t.ApprovalRequested(
                    session_id="native", turn_id="turn1", request_id="r", kind="command",
                    summary="rm -r build",
                )
            )  # fmt: skip
        if second == 3620:
            a.put(("!approve",))
        await a.inbox.join()
        assert set(a.poster.held) <= {mid}  # one held edit at most, never a queue
        await later(a, clock, 20)
    a.turn["started_at"] -= 3 * 3600  # the wall clock's three hours
    a.put(t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED))
    await a.inbox.join()
    times = [when for when, _ in sent[mid]]
    contents = [content for _, content in sent[mid]]
    assert len(times) == MAX_EDITS
    start = times[0]  # the model's edit, as the turn starts
    assert [round(x - start) for x in times[1:4]] == [60, 360, 660]  # a minute in, then 300 s
    assert all(b - a >= GAP for a, b in pairwise(times[:-1]))
    assert contents[-2].endswith(PAUSED) and not any(PAUSED in c for c in contents[:-2])
    assert contents[-1] == fake.messages[mid]["content"]
    assert contents[-1].startswith("✅ **Done** · 180m 00s · ")
    assert times[-1] == clock.now  # the summary at once, whatever the gap
    approval = next(m for m, c in sent.items() if c[0][1] == "Approval: accepted")
    assert len(sent[approval]) == 1 and set(sent) == {mid, approval}


async def test_a_held_status_edit_that_failed_is_tried_again(actor):
    """Codex round 1 on #755: a refresh within the edit gap is held and sent later, with nothing
    waiting for it. If that send fails, the next refresh must not take the content as shown."""
    a, fake, _backend = actor
    a.poster.client.budget.clock = clock = FrozenClock()  # the refresh timer not on it
    await send(a, fake, "start")
    mid = a.turn["status_message_id"]
    a.put(t.TurnStarted(session_id="native", turn_id="turn1", model="m"))
    await a.inbox.join()  # the model's edit, at once
    item = t.ItemInfo(item_id="c", kind=t.ItemKind.COMMAND, status=t.ItemStatus.IN_PROGRESS,
                      title="make all")  # fmt: skip
    a.put(t.ItemStarted(session_id="native", turn_id="turn1", item=item))
    a.put("tick")
    await a.inbox.join()
    assert mid in a.poster.held and "make all" not in fake.messages[mid]["content"]
    fake.offline = True
    clock.advance(GAP)
    await until(lambda: not a.poster.held and not a.poster.edits)  # sent, and failed
    fake.offline = False
    assert "make all" not in fake.messages[mid]["content"] and a.last_status == ""
    a.put("tick")
    await a.inbox.join()
    clock.advance(GAP)  # the failed send counted: a gap after it
    await until(lambda: "make all" in fake.messages[mid]["content"])


async def test_a_short_turn_shows_what_it_does_a_minute_in(actor):
    """#754: a 90 s turn's status message gets its periodic refresh one edit gap (60 s) after
    the turn started, showing its running step, and then only its summary."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "a short one")
    mid, start = a.turn["status_message_id"], a.turn["started_at"]
    clock.advance(start - clock.now)  # the turn's start, on the still clock
    item = t.ItemInfo(item_id="c", kind=t.ItemKind.COMMAND, status=t.ItemStatus.IN_PROGRESS,
                      title="sleep 80")  # fmt: skip
    await later(a, clock, 10)
    a.put(t.ItemStarted(session_id="native", turn_id="turn1", item=item))
    await a.inbox.join()
    for _ in range(4):
        await later(a, clock, 10)
    assert mid not in sent  # 50 s in
    await later(a, clock, 10)
    assert [(when - start, "sleep 80" in body) for when, body in sent[mid]] == [(60, True)]
    await later(a, clock, 30)
    a.put(t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED))
    await a.inbox.join()
    assert [when - start for when, _ in sent[mid]] == [60, 90]
    assert sent[mid][-1][1].startswith("✅ **Done** · ")


async def test_a_refresh_with_nothing_new_edits_nothing(actor):
    """#754: a turn with nothing new a minute in gets no edit at its refresh's look: `begin`
    records what it posted as shown. Its clock starts at the turn's own start (review of
    #755: a clock moved back made an earlier form of this test pass without that record)."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "nothing to show yet")
    mid, start = a.turn["status_message_id"], a.turn["started_at"]
    assert start >= clock.now
    clock.advance(start - clock.now)  # forward, to the turn's start
    looked = []
    tick = a.tick

    async def counted():
        looked.append(clock.now - start)
        await tick()

    a.tick = counted
    for _ in range(7):
        await later(a, clock, 10)
    assert looked == [60]  # the refresh's first look, a minute in
    assert mid not in sent  # edited nothing


async def test_a_summary_whose_last_edit_was_spent_is_posted_anew(actor):
    """Codex round 2 on #755: after 19 refreshes, the summary's edit is the message's last. If
    it fails in a way that may have landed, it counts, so its retry can't edit the message: the
    summary is posted as a new status message instead, never taken as shown."""
    a, fake, _backend = actor
    await send(a, fake, "start")
    mid = a.turn["status_message_id"]
    bounds = a.poster.client.edit_bounds
    bounds.messages[mid] = (MAX_EDITS - 1, 0.0)  # 19 edits made: only the final one is left
    fake.offline = True
    a.put(t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED))
    await a.inbox.join()
    assert a.completion and bounds.edits(mid) == MAX_EDITS  # the failed final edit counted
    fake.offline = False
    await a.complete(a.completion)  # the retry, as `!status` or the reconnect makes it
    assert not a.completion and not fake.messages[mid]["content"].startswith("✅")
    (turn,) = a.store.rows("turns", native_turn_id="turn1")
    assert turn["status_message_id"] != mid
    assert fake.messages[turn["status_message_id"]]["content"].startswith("✅ **Done** · ")


async def test_only_the_summary_skips_the_gap_while_an_answer_waits(actor):
    """Review of #755: while a turn's answer waits for delivery, a `stopping` or `backend
    reconnecting` status is no final edit: it waits for its gap like any other. The summary
    itself still goes at once."""
    a, fake, _backend = actor
    a.poster.client.budget.clock = clock = FrozenClock()
    await send(a, fake, "start")
    mid = a.turn["status_message_id"]
    a.put(t.TurnStarted(session_id="native", turn_id="turn1", model="m"))
    await a.inbox.join()  # an edit now: the next non-final one waits a minute
    a.completion = t.TurnCompleted(
        session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED
    )  # its answer waits for delivery (§7.8.2)
    await a.status("backend reconnecting")
    assert mid in a.poster.held and "Backend reconnecting" not in fake.messages[mid]["content"]
    await a.status()  # the summary, as a retry renders it: at once, in place of the held edit
    assert not a.poster.held and fake.messages[mid]["content"].startswith("✅ **Done** · ")
    clock.advance(GAP)
    await asyncio.sleep(0)
    assert fake.messages[mid]["content"].startswith("✅ **Done** · ")


def step(n: int) -> t.ItemStarted:
    item = t.ItemInfo(item_id=f"c{n}", kind=t.ItemKind.COMMAND, status=t.ItemStatus.IN_PROGRESS,
                      title=f"step {n}")  # fmt: skip
    return t.ItemStarted(session_id="native", turn_id="turn1", item=item)


async def test_a_slow_first_action_gets_its_early_view_at_the_next_look(actor):
    """#755's e2e (scenarios 3 and 4): a CLI slow to start reports its model at once, its effort
    after 30 s and its first action only at 100 s. The model's edits don't count as the early
    view: the first look that finds an action, at 120 s, shows it, and the next periodic
    refresh comes about `edit_interval` later, not six minutes into the turn."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "think, then sleep 300")
    mid, start = a.turn["status_message_id"], a.turn["started_at"]
    clock.advance(start - clock.now)
    await later(a, clock, 1)
    a.put(t.TurnStarted(session_id="native", turn_id="turn1", model="m"))
    await a.inbox.join()
    await later(a, clock, 29)
    a.put(t.TurnStarted(session_id="native", turn_id="turn1", model="m", effort="high"))
    await a.inbox.join()
    for _ in range(7):
        await later(a, clock, 10)  # to 100 s
    # The model at once, then the effort, held to its gap (61 s; the still clock moves 10 s at a
    # time, so it goes out at 70 s)
    assert [round(w - start) for w, _ in sent[mid]] == [1, 70]
    assert all("0 actions" in body for _, body in sent[mid])
    a.put(step(1))
    await a.inbox.join()
    for n in range(2, 33):
        await later(a, clock, 10)  # a new action every 10 s, to 410 s
        a.put(step(n))
        await a.inbox.join()
    await later(a, clock, 10)  # 420 s
    times = [round(w - start) for w, _ in sent[mid]]
    assert times == [1, 70, 130, 420]  # the early view (its look at 120 s, held to its gap)
    assert "step 1" in sent[mid][2][1] and "step 32" in sent[mid][3][1]


async def test_a_turn_with_no_action_gets_no_periodic_edit(actor):
    """#754: a turn whose agent does nothing visible for four minutes gets no periodic edit,
    however many looks pass; its first action is shown at the next look."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "think for a long time")
    mid, start = a.turn["status_message_id"], a.turn["started_at"]
    clock.advance(start - clock.now)
    for _ in range(24):
        await later(a, clock, 10)  # 240 s: looks at 60, 90, … 240
    assert mid not in sent
    await later(a, clock, 5)
    a.put(step(1))
    await a.inbox.join()
    await later(a, clock, 25)  # the look at 270 s
    assert [round(w - start) for w, _ in sent[mid]] == [270] and "step 1" in sent[mid][0][1]


async def test_an_outage_retries_a_pending_summary_at_most_once_a_gap(actor):
    """Review round 2 of #755: Zulip goes away as the turn completes, for ten minutes. Each try
    of the summary may have landed, so each counts towards the cap: the looks try it again a
    gap apart at the earliest, well under the cap, not at every look (30 s)."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "one step, then done")
    mid, start = a.turn["status_message_id"], a.turn["started_at"]
    clock.advance(start - clock.now)
    a.put(step(1))
    await a.inbox.join()
    for _ in range(10):
        await later(a, clock, 10)  # 100 s: the early view at 60 s
    shown = len(sent[mid])
    fake.offline = True
    a.put(t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED))
    await a.inbox.join()
    assert a.completion  # its answer waits for Zulip
    for _ in range(60):
        await later(a, clock, 10)  # ten minutes of looks
    tries = [w - start for w, _ in sent[mid][shown:]]
    assert tries[0] == 100 and all(b - a >= GAP for a, b in pairwise(tries)), tries
    assert len(tries) <= 11 and a.poster.client.edit_bounds.edits(mid) < MAX_EDITS - 1


async def test_a_change_to_the_first_line_alone_makes_no_edit_at_a_look(actor):
    """#754: a look refreshes for the actions, plan or notes only. The state, model and effort
    on the first line have edits of their own; a state set without one (as `_run` sets `error`)
    makes no edit at a look."""
    a, fake, _backend = actor
    clock, sent = await on_a_still_clock(a)
    await send(a, fake, "start")
    mid, start = a.turn["status_message_id"], a.turn["started_at"]
    clock.advance(start - clock.now)
    a.state(state="error")
    for _ in range(12):
        await later(a, clock, 10)  # looks at 60, 90 and 120 s
    assert "Error" in a.status_text().splitlines()[0] and mid not in sent
