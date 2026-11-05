"""Reopening inside the actor (SPEC §7.8.1): the snapshot and its recovery are one step."""

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.commands.run import Runtime
from zab.core import types as t
from zab.core.session import Delivery, Reopen, SessionActor
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient
from zab.zulip.poster import Poster

pytestmark = pytest.mark.integration


@pytest.fixture
async def world(tmp_path):
    """Actors of one bot that share a backend, a store and one turn slot."""
    store = Store(tmp_path)
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    backend = AsyncMock()
    backend.kind = "codex"
    backend.capabilities = t.Capabilities(
        steer=False, approvals=False, live_attach=False, rename=False
    )
    backend.start_turn.side_effect = ["turn1", "turn2", "turn3"]
    manager = AsyncMock()
    manager.snapshot.return_value = None
    poster = Poster(client)
    slots = asyncio.Semaphore(1)
    context = t.RenderContext(
        workspace=LocalWorkspace(str(tmp_path)),
        max_message_length=10000,
        max_upload_bytes=100000,
        upload=None,
    )
    actors = []

    async def make(sid, state="idle"):
        card = await client.send(1, sid, "**zab session**")
        store.insert(
            "sessions",
            id=sid,
            bot="b",
            backend="c",
            native_id=sid,
            card_message_id=card,
            stream_id=1,
            topic=sid,
            workspace=str(tmp_path),
            workspace_mode="shared",
            repo_root=str(tmp_path),
            settings_json="{}",
            state=state,
            created_at=0,
            updated_at=0,
        )
        actor = SessionActor(
            store.rows("sessions", id=sid)[0],
            store,
            backend,
            poster,
            MarkdownRenderer(),
            manager,
            context,
            slots,
            machine="test",
            resume="codex resume",
        )
        actors.append(actor)
        return actor

    yield SimpleNamespace(make=make, fake=fake, backend=backend, slots=slots, path=str(tmp_path))
    for actor in actors:
        await actor.close()
    await poster.close()
    await client.close()
    store.close()


def info(world, native, **values):
    return t.SessionInfo(
        native_id=native,
        cwd=world.path,
        settings=t.SessionSettings(),
        resume_hint="resume",
        **values,
    )


async def settle(*actors):
    for actor in actors:
        await actor.inbox.join()
        await asyncio.sleep(0)
        await actor.inbox.join()


async def deliver(actor, world, text):
    mid = await actor.poster.client.send(1, actor.row["topic"], text)
    actor.put(Delivery(mid, 20, "Tester", text))
    await settle(actor)


def warnings_posted(world):
    return [m["content"] for m in world.fake.messages.values() if m["content"].startswith("⚠️")]


async def test_failed_reopen_leaves_only_that_session_in_error(world, caplog):
    bad, good = await world.make("bad"), await world.make("good")

    async def open_session(native, spec):
        if native == "bad":
            raise RuntimeError("missing thread")
        return info(world, native)

    world.backend.open_session.side_effect = open_session
    request = Reopen()
    with caplog.at_level(logging.WARNING, logger="zab.runtime"):
        bad.put(request)
        good.put(request)
        await settle(bad, good)
    assert bad.row["state"] == "error" and not bad.ready
    assert good.row["state"] == "idle" and good.ready
    assert [r.getMessage() for r in caplog.records] == ["Session recovery failed: RuntimeError"]
    assert caplog.records[0].session == "bad"
    assert not warnings_posted(world)  # as before: a log line, no "use !status" post


REFUSAL = "Codex thread is already loaded with an unverified git identity; restart, then retry"


def last_post(world):
    return list(world.fake.messages.values())[-1]["content"]


async def test_a_refused_session_alone_goes_to_error_and_status_retries_it(world):
    """#306: one thread's refusal is not the backend being down. Only that session goes to
    `error`, the round reopens the others, and `!status` shows the reason and retries."""
    actors = [await world.make(sid) for sid in ("before", "refused", "after")]
    refused = actors[1]
    refusing = True

    async def open_session(native, spec):
        if native == "refused" and refusing:
            raise t.SessionRefused(REFUSAL)
        return info(world, native)

    world.backend.open_session.side_effect = open_session
    request = Reopen()
    for actor in actors:
        actor.ready = False  # restored at startup
        actor.put(request)
    await settle(*actors)
    assert not request.stopped and world.backend.open_session.await_count == 3
    assert actors[0].ready and actors[2].ready and actors[2].row["state"] == "idle"
    assert refused.row["state"] == "error" and not refused.ready

    # Its user is told, and the message is held for the retry (§7.3).
    await deliver(refused, world, "work")
    assert last_post(world) == (
        "⚠️ **Bridge** (not Codex): This session needs recovery; use !status to retry."
    )
    assert not refused.turn and not world.backend.start_turn.await_count

    refused.put(("!status",))
    await settle(refused)
    assert last_post(world).startswith(f"⚠️ Error: {REFUSAL} · recovery attempted")
    assert refused.row["state"] == "error" and not refused.turn

    refusing = False  # its app-server was restarted
    refused.put(("!status",))
    await settle(refused)
    assert refused.ready and refused.turn["native_turn_id"] == "turn1"


async def test_status_reopens_a_backend_down_session_once_its_backend_answers(world):
    """Left backend-down by a round that stopped, a session waits for the next
    BackendStatus(up=True); `!status` reopens it sooner if the backend answers (#306)."""
    actor = await world.make("a")
    world.backend.open_session.side_effect = t.BackendUnavailable("down")
    actor.put(Reopen())
    await settle(actor)
    await deliver(actor, world, "work")
    assert last_post(world) == "⏳ Waiting for a free slot or backend."

    actor.put(("!status",))
    await settle(actor)
    assert "· recovery attempted: backend unavailable" in last_post(world)
    assert actor.row["state"] == "idle" and not actor.ready  # still backend-down, not error

    world.backend.open_session.side_effect = lambda native, spec: info(world, native)
    actor.put(("!status",))
    await settle(actor)
    assert actor.ready and actor.turn["native_turn_id"] == "turn1"


async def test_one_round_for_every_bot_of_a_backend(monkeypatch):
    """Bots that share a backend reopen in one round, so their backend calls take turns as
    the pump's own loop made them (#306); each reconnect is a new round."""
    received = []

    def bridge(backend):
        actor = SimpleNamespace(row={"state": "idle"}, put=received.append)
        metrics = SimpleNamespace(connection=lambda up: None)
        return SimpleNamespace(
            bot=SimpleNamespace(backend=backend),
            client=SimpleNamespace(metrics=metrics),
            actors={"s": actor},
        )

    async def events():
        for _ in range(2):
            yield t.BackendStatus(session_id="", up=True)

    runtime = Runtime.__new__(Runtime)
    runtime.bridges, runtime.recoveries = [bridge("codex"), bridge("claude"), bridge("codex")], {}
    monkeypatch.setattr(runtime, "finish_recovery", AsyncMock())
    await runtime.pump("codex", SimpleNamespace(events=events))
    assert len(received) == 4 and all(isinstance(r, Reopen) for r in received)
    assert received[0] is received[1] and received[2] is received[3]
    assert received[0] is not received[2]


async def test_backend_unavailable_stops_the_round_as_backend_down(world):
    first, rest = await world.make("first"), await world.make("rest")
    await deliver(first, world, "work")
    status = first.turn["status_message_id"]
    world.backend.open_session.side_effect = t.BackendUnavailable("down")
    request = Reopen()
    first.put(request)
    rest.put(request)
    await settle(first, rest)
    world.backend.open_session.assert_awaited_once()  # the rest of the round never asked
    assert request.stopped
    assert not first.ready and not rest.ready
    assert first.row["state"] == "running" and rest.row["state"] == "idle"  # never "error"
    assert "Backend reconnecting" in world.fake.messages[status]["content"]
    assert not warnings_posted(world)

    # The next BackendStatus(up=True) is a new round and reopens everything.
    world.backend.open_session.side_effect = lambda native, spec: info(
        world, native, active_turn_id="turn1" if native == "first" else None
    )
    request = Reopen()
    first.put(request)
    rest.put(request)
    await settle(first, rest)
    assert first.ready and rest.ready
    assert first.row["state"] == "running" and first.turn["native_turn_id"] == "turn1"


async def test_a_round_reopens_one_session_at_a_time(world):
    """As the pump's own loop did: one backend call at a time, never a burst."""
    actors = [await world.make(f"s{i}") for i in range(3)]
    flight = peak = 0

    async def open_session(native, spec):
        nonlocal flight, peak
        flight += 1
        peak = max(peak, flight)
        await asyncio.sleep(0.01)
        flight -= 1
        return info(world, native)

    world.backend.open_session.side_effect = open_session
    request = Reopen()
    for actor in actors:
        actor.put(request)
    await settle(*actors)
    assert world.backend.open_session.await_count == 3 and peak == 1
    assert all(actor.ready for actor in actors)


async def test_reopen_skips_readonly_and_detached_sessions(world):
    readonly, detached = await world.make("ro", "readonly"), await world.make("gone", "detached")
    request = Reopen()
    readonly.put(request)
    detached.put(request)
    await settle(readonly, detached)
    world.backend.open_session.assert_not_awaited()


async def test_reopen_between_slot_wait_and_begin_keeps_the_slot(world):
    """A reopen can land after the waiter took the slot and queued "begin": the slot is that
    begin's, so recovery of an idle session must not release it under the new turn."""
    actor = await world.make("a")

    async def open_session(native, spec):
        for _ in range(3):
            await asyncio.sleep(0)  # the waiter takes the free slot meanwhile
        return info(world, native)

    world.backend.open_session.side_effect = open_session
    mid = await actor.poster.client.send(1, "a", "hello")
    actor.put(Delivery(mid, 20, "Tester", "hello"))
    actor.put(Reopen())
    await settle(actor)
    assert actor.turn and actor.turn["native_turn_id"] == "turn1"
    assert actor.slot_owned and world.slots.locked()


async def test_reopen_adopts_a_codex_turn_still_running(world):
    actor = await world.make("a")
    await deliver(actor, world, "work")
    actor.ready = False  # restored at startup: held until the snapshot is applied
    world.backend.open_session.return_value = info(world, "a", active_turn_id="turn1")
    actor.put(Reopen())
    await settle(actor)
    assert actor.ready and actor.row["state"] == "running"
    assert actor.turn["native_turn_id"] == "turn1" and actor.slot_owned


async def test_finish_recovery_waits_for_every_in_actor_snapshot(bridge, monkeypatch):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    gate = asyncio.Event()

    async def open_session(native, spec):
        await gate.wait()
        return replace(backend.create_session.return_value, active_turn_id="turn1")

    backend.open_session.side_effect = open_session
    order = []
    monkeypatch.setattr(
        "zab.commands.run.detached.recover", AsyncMock(side_effect=lambda _: order.append(1))
    )

    async def events():
        yield t.BackendStatus(session_id="", up=True)

    runtime = Runtime.__new__(Runtime)
    runtime.bridges, runtime.recoveries = [b], {}
    await runtime.pump("codex", SimpleNamespace(events=events))
    recovery = runtime.recoveries[id(b)]
    await asyncio.sleep(0.05)
    assert not recovery.done() and not order  # detached.recover and the rest wait
    gate.set()
    await asyncio.wait_for(recovery, 5)
    assert order == [1]
    assert b.actors["native"].row["state"] == "running"
