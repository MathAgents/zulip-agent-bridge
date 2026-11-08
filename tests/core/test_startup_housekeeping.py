"""Start-up answers first, housekeeping after (issue #443; SPEC §7.1, §4.4, §7.8.2).

On the participant test VM (Oct 18) the claude bot had 86 sessions: start-up spent two
requests per session confirming cards and one more reconciling each topic, about 260
requests against the bot's 150 per minute, before it read the message that woke the VM.
"""

import asyncio
import contextlib
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_catchup import missed, restart, settle

from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core import types as t
from zab.core.bridge import BotBridge
from zab.core.catchup import Catchup
from zab.core.session import Reopen
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_clock import FrozenClock
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient, ZulipError

pytestmark = pytest.mark.integration


async def until(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def completed(turn="turn1"):
    return t.TurnCompleted(
        session_id="native", turn_id=turn, status=t.TurnStatus.COMPLETED, final_text="done"
    )


def dm_replies(fake):
    return [m for m in fake.messages.values() if m["type"] == "private" and m["sender_id"] == 1]


@contextlib.asynccontextmanager
async def many_sessions(tmp_path, bridge, count=300):
    """A bridge of its own on the fixture's mocks, its budget on a still clock, every request
    logged as (time, method, path, query), with `count` recent idle sessions from earlier
    runs, each with its card. Yields the bridge, its fake Zulip, the clock, the log and the
    cards (message id -> topic)."""
    _, _, backend = bridge
    clock, log = FrozenClock(), []
    fake = FakeZulip()

    async def recorded(request):
        query = parse_qs(request.url.query.decode()) if request.method == "GET" else {}
        log.append((clock.now, request.method, request.url.path.removeprefix("/api/v1/"), query))
        return await fake.handle(request)

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
    client = ZulipClient(
        "https://zulip.example", "bot", "decoy", transport=httpx.MockTransport(recorded)
    )
    client.budget.clock = clock
    store = Store(tmp_path / f"state{count}")
    b = BotBridge(
        config,
        config.bots[0],
        store,
        backend,
        bridge[0].manager,
        MarkdownRenderer(),
        asyncio.Semaphore(2),
        client,
    )
    try:
        await b.initialize()
        cards = {}
        for i in range(count):
            fake._message_id += 1
            cards[fake._message_id] = f"topic {i}"
            fake.messages[fake._message_id] = {
                "id": fake._message_id,
                "type": "stream",
                "stream_id": 1,
                "subject": f"topic {i}",
                "content": "**zab session** · 🤖 Session started.",
                "sender_id": 1,
                "timestamp": time.time() - 3 * 86400,
                "flags": [],
                "reactions": [],
            }
            store.insert(
                "sessions",
                id=f"s{i}",
                bot="bot",
                backend="codex",
                native_id=f"n{i}",
                card_message_id=fake._message_id,
                stream_id=1,
                topic=f"topic {i}",
                workspace=str(tmp_path),
                workspace_mode="shared",
                repo_root=str(tmp_path),
                settings_json=json.dumps({}),
                state="idle",
                created_at=time.time() - 3 * 86400,
                updated_at=time.time() - 86400,
            )
        store.set("catchup_cursor:bot", fake._message_id)
        yield b, fake, clock, log, cards
    finally:
        clock.advance(3600)  # release whatever still waits on the budget, then close
        client.budget.clock = None
        await b.close()
        store.close()


async def housekeeping_done(b, clock):
    """Run the housekeeping to its end, one budget window at a time."""
    for _ in range(30):
        if b.housekeeping.done():
            break
        clock.advance(61)
        await until(lambda: b.housekeeping.done() or b.client.budget.free() <= b.poster.reserve)
    await b.housekeeping


def card_ids(log, since=0):
    """The messages fetched one by one (`GET /messages/{id}`: card checks) from `since` on."""
    return {path[9:] for _, m, path, _ in log[since:] if m == "GET" and path[9:].isdigit()}


def reconciled(log, since=0):
    """The topics fetched by channel and topic (reconciliation, §7.8.2) from `since` on."""
    topics = set()
    for _, method, path, query in log[since:]:
        if method == "GET" and path == "messages" and "narrow" in query:
            narrow = json.loads(query["narrow"][0])
            if {term["operator"] for term in narrow} == {"channel", "topic"}:
                topics.add(next(term["operand"] for term in narrow if term["operator"] == "topic"))
    return topics


async def test_waking_messages_are_answered_before_the_housekeeping_of_300_sessions(
    tmp_path, bridge
):
    """Criterion 3: the DM that woke the VM and a mention in a new topic are answered
    before any housekeeping request, without a wait on the budget; a DM during the
    housekeeping is answered at once too; the housekeeping then confirms every card and
    reconciles every topic, within the budget minus its reserve."""
    _, _, backend = bridge
    async with many_sessions(tmp_path, bridge) as (b, fake, clock, log, cards):
        store = b.store
        fake.post_as(20, "!status")  # the DM that woke the VM
        fake.post_as(20, "@**Test Bot** NEW-TOPIC-question", topic="new topic")
        await asyncio.wait_for(b.start(), 10)
        b.listener.cancel()  # this test drives the live message itself
        assert clock.now == 1000.0, "start-up waited on the budget"
        assert len(dm_replies(fake)) == 1
        await settle(b)
        backend.start_turn.assert_awaited_once()
        assert backend.start_turn.call_args.args[1].text.endswith("NEW-TOPIC-question")
        card_fetches = [
            i for i, (_, m, path, _) in enumerate(log) if m == "GET" and path[9:].isdigit()
        ]
        replies = [i for i, (_, m, path, _) in enumerate(log) if m == "POST" and path == "messages"]
        assert card_fetches and replies and max(replies[:2]) < min(card_fetches)

        # The housekeeping runs until only the reserve is left in the window, then waits.
        limit, reserve = b.client.budget.limit, b.poster.reserve
        await until(lambda: b.client.budget.free() <= reserve)
        await asyncio.sleep(0.05)
        assert not b.housekeeping.done() and 0 < b.client.budget.free() <= reserve
        live = fake.post_as(20, "!help")
        await asyncio.wait_for(b.handle({"type": "message", "message": live}), 5)
        assert len(dm_replies(fake)) == 2 and clock.now == 1000.0

        await housekeeping_done(b, clock)
        assert not b.unverified
        assert card_ids(log) >= {str(card) for card in cards}
        assert reconciled(log) >= set(cards.values())
        assert store.get("catchup_reconcile:bot") is None
        # Never more than the budget in a window, and housekeeping never past its share.
        times = [when for when, *_ in log]
        house = [
            when
            for when, m, path, query in log
            if m == "GET"
            and (path[9:].isdigit() or ("narrow" in query and "sender" not in query["narrow"][0]))
        ]
        for start in times:
            assert sum(start <= x < start + 60 for x in times) <= limit
        for start in house:
            assert sum(start <= x < start + 60 for x in house) <= limit - reserve


async def test_a_lost_event_queue_answers_before_the_housekeeping_of_300_sessions(tmp_path, bridge):
    """E2e scenario 4 at v0.2.19's candidate (#964): Zulip blocked for 12 minutes, the turn's
    answer refused meanwhile and the event queue lost. The recovery confirmed every card and
    reconciled every recent topic inline, about two requests per session against the bot's
    150 a minute, before it retried the answer or read the gap's new topic; on the test VM's
    hundreds of sessions that passed the scenario's ten minutes. Both now come first, without
    a wait on the budget, and the housekeeping follows in the background, as at start-up
    (SPEC §7.8.2, §7.1, §4.4)."""
    from unittest.mock import AsyncMock

    _, _, backend = bridge
    async with many_sessions(tmp_path, bridge) as (b, fake, clock, log, cards):
        await asyncio.wait_for(b.start(), 10)
        b.listener.cancel()  # this test drives the listener itself
        with contextlib.suppress(asyncio.CancelledError):
            await b.listener
        await housekeeping_done(b, clock)
        clock.advance(61)
        await user_message(b, fake, "@**Test Bot** sleep, then answer", topic="busy")
        actor = b.actors["native"]
        assert actor.row["state"] == "running"
        fake.offline = True  # Zulip goes away; the turn then finishes
        actor.put(
            t.TurnCompleted(
                session_id="native",
                turn_id="turn1",
                status=t.TurnStatus.COMPLETED,
                final_text="OUTAGE-token",
            )
        )
        await actor.inbox.join()
        assert actor.completion and actor.row["state"] == "error"
        backend.create_session.return_value = replace(
            backend.create_session.return_value, native_id="native2"
        )
        fake.post_as(20, "@**Test Bot** DURING-token", topic="during")  # Zulip has it
        fake.lose_queues()  # nobody polled it for ten minutes
        fake.offline = False
        clock.advance(61)  # a window of its own for the recovery
        back, since = clock.now, len(log)
        b.client.events = AsyncMock(
            side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), [], asyncio.CancelledError()]
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(b.listen(), 10)

        def answered(text):
            return any(m["sender_id"] == 1 and text in m["content"] for m in fake.messages.values())

        await until(lambda: answered("OUTAGE-token") and backend.start_turn.await_count == 2)
        await settle(b)
        assert backend.start_turn.call_args.args[1].text.endswith("DURING-token")
        assert actor.completion is None and actor.row["state"] == "idle"
        assert clock.now == back, "the answers waited on the budget"
        assert not b.housekeeping.done()  # the 300 sessions are still to come
        await housekeeping_done(b, clock)
        assert not b.unverified
        assert card_ids(log, since) >= {str(card) for card in cards}
        assert reconciled(log, since) >= set(cards.values())
        assert b.store.get("catchup_reconcile:bot") is None


@pytest.mark.parametrize("age_days", [0, 30])
async def test_follow_up_in_a_topic_resolved_while_down_reaches_its_session(bridge, age_days):
    """The card moved with its topic while no event could tell the bridge: routing
    confirms it first, for a recent session and for one the housekeeping skips."""
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(completed())
    old = time.time() - age_days * 86400
    b.store.update("sessions", {"id": actor.row["id"]}, updated_at=old)
    actor.row["updated_at"] = old
    for message in fake.messages.values():  # Zulip moves every message of the topic
        if message["subject"] == "topic":
            message["subject"] = "✔ topic"
    follow = await missed(b, fake, "and one more thing", topic="✔ topic")
    follow["flags"] = []  # no mention: the topic's session is addressed
    fake.requests.clear()
    await restart(b)
    backend.create_session.assert_awaited_once()
    assert backend.start_turn.await_count == 2
    assert backend.start_turn.call_args.args[1].text.endswith("and one more thing")
    assert b.store.rows("sessions", id=actor.row["id"])[0]["topic"] == "✔ topic"
    searches = [
        p
        for m, path, p in fake.requests
        if m == "GET" and path == "messages" and "sender" in p.get("narrow", "")
    ]
    assert len(searches) == 1


async def test_card_deleted_while_down_detaches_its_session(bridge):
    b, fake, _backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(completed())
    del fake.messages[actor.row["card_message_id"]]
    await restart(b)
    assert b.store.rows("sessions", id=actor.row["id"])[0]["state"] == "detached"
    assert not b.unverified


async def test_channel_made_public_while_down_is_checked_before_routing(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(completed())
    fake.streams[1]["invite_only"] = False
    await missed(b, fake, "@**Test Bot** run this", topic="topic")
    await restart(b)
    assert backend.start_turn.await_count == 1
    assert b.store.rows("sessions", id=actor.row["id"])[0]["state"] == "readonly"


async def test_a_live_move_during_the_card_fetch_is_not_undone(bridge):
    b, fake, _backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    card = actor.row["card_message_id"]
    b.unverify()
    request = b.client.request
    fetches = []

    async def racing(method, endpoint, **params):
        result = await request(method, endpoint, **params)
        if endpoint == f"messages/{card}":
            fetches.append(result["message"]["subject"])
            if len(fetches) == 1:  # the topic is renamed while the stale answer travels
                fake.messages[card]["subject"] = "live name"
                await b.handle(
                    {"type": "update_message", "message_ids": [card], "subject": "live name"}
                )
        return result

    b.client.request = racing
    await b.verify_card(actor.row["id"])
    assert fetches == ["topic", "live name"]
    assert b.store.rows("sessions", id=actor.row["id"])[0]["topic"] == "live name"
    assert actor.row["id"] not in b.unverified


async def test_reconciliation_after_the_replay_uses_the_boundary_from_before_it(bridge):
    """A message the last run never handled, older than the bot's newest handled one: topic
    reconciliation finds it, although the replay moved the cursor past it first."""
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    await b.actors["native"].event(completed())
    await missed(b, fake, "GAP-message", topic="topic")  # never handled
    handled = await missed(b, fake, "!help", dm=True)
    await b.handle({"type": "message", "message": handled})
    later = await missed(b, fake, "!help", dm=True)  # replayed: the cursor passes the gap
    await restart(b)
    assert b.store.get("catchup_cursor:bot") >= later["id"]
    assert backend.start_turn.await_count == 2
    assert backend.start_turn.call_args.args[1].text.endswith("GAP-message")


async def test_restart_between_replay_and_reconciliation_reconciles_from_the_old_boundary(
    bridge,
):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    await b.actors["native"].event(completed())
    await missed(b, fake, "GAP-message", topic="topic")
    handled = await missed(b, fake, "!help", dm=True)
    await b.handle({"type": "message", "message": handled})
    interrupted = Catchup(b)
    await missed(b, fake, "NEWER-message", topic="topic")
    await interrupted.replay()  # claims the newer message in the same topic
    await settle(b)
    assert backend.start_turn.call_args.args[1].text.endswith("NEWER-message")
    # The process exits here, before its housekeeping reconciled the topic.
    await restart(b)
    backend.steer.assert_awaited_once()
    assert backend.steer.call_args.args[2].text.endswith("GAP-message")
    assert b.store.get(interrupted.reconcile_key) is None


async def test_reopening_an_unchanged_session_keeps_its_activity_time(bridge):
    """Every start-up made every session recent: reopening rewrote its settings and state
    with the same values, and that moved `updated_at` (seen on the test VM, Oct 18)."""
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(completed())
    old = time.time() - 30 * 86400
    b.store.update("sessions", {"id": actor.row["id"]}, updated_at=old)
    actor.row["updated_at"] = old
    backend.open_session.return_value = backend.create_session.return_value
    actor.put(Reopen())
    await actor.inbox.join()
    assert actor.ready and b.store.rows("sessions", id=actor.row["id"])[0]["updated_at"] == old
    assert b.recent_sessions() == []
    actor.state(state="error")
    assert b.store.rows("sessions", id=actor.row["id"])[0]["updated_at"] > old


async def test_channel_privacy_is_cached_until_a_stream_event(bridge):
    b, fake, backend = bridge

    def lookups():
        return [path for _, path, _ in fake.requests if path == "streams/1"]

    # The event that forgets a channel's privacy reaches the bridge at all (#447 review).
    registers = [p for _, path, p in fake.requests if path == "register"]
    assert registers and all("stream" in json.loads(p["event_types"]) for p in registers)
    await user_message(b, fake, "@**Test Bot** begin")
    before = len(lookups())
    assert before >= 1
    for text in ("one", "two"):
        await user_message(b, fake, text)
    assert len(lookups()) == before
    fake.streams[1]["invite_only"] = False
    await b.handle(
        {
            "type": "stream",
            "op": "update",
            "stream_id": 1,
            "property": "invite_only",
            "value": False,
        }
    )
    await user_message(b, fake, "@**Test Bot** three")
    assert len(lookups()) == before + 1
    assert any(
        m["content"] == b.note("sessions are disabled in public channels")
        for m in fake.messages.values()
    )
    assert backend.steer.await_count == 2


async def test_queue_loss_stops_the_startup_housekeeping_and_runs_it_after_the_replay(
    bridge, monkeypatch
):
    """The recovery confirms only the cards of sessions with work, replays the gap, then
    starts the housekeeping again in the background (SPEC §7.8.2), as a start-up does: once
    the replay has ended, so that reconciliation claims nothing past what it read (§7.8.1)."""
    from unittest.mock import AsyncMock

    b, _fake, _backend = bridge
    b.housekeeping = asyncio.create_task(asyncio.Event().wait())
    running = b.housekeeping
    b.client.events = AsyncMock(
        side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), asyncio.CancelledError()]
    )
    order = []
    b.refresh_cards = AsyncMock()
    b.reconcile_messages = AsyncMock(
        side_effect=lambda **kw: order.append(("reconcile", kw, kw["catchup"].end))
    )
    replay = Catchup.replay

    async def replayed(catchup):
        await asyncio.sleep(0.05)  # time for a housekeeping started too soon to reconcile
        count = await replay(catchup)
        order.append(("replayed", catchup))
        return count

    monkeypatch.setattr(Catchup, "replay", replayed)
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert running.cancelled() and b.housekeeping is not running
    await b.housekeeping
    b.refresh_cards.assert_not_awaited()
    assert [step[0] for step in order] == ["replayed", "reconcile"]
    (_, catchup), (_, kw, end) = order
    assert kw == {"catchup": catchup, "background": True} and end is not None


async def test_a_queue_loss_confirms_the_cards_of_sessions_with_work_first(bridge, monkeypatch):
    """Review of #965: the gap's card moves were lost with the queue. Before the replay, the
    recovery has confirmed the card of a session with work in flight (its turn), which moved
    with its topic, and forgotten that of an idle one, moved too, which the housekeeping
    confirms after the replay (SPEC §7.8.2 steps 1 and 5, §4.4)."""
    from unittest.mock import AsyncMock

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** idle", topic="idle")
    idle = b.actors["native"]
    await idle.event(completed())
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    await user_message(b, fake, "@**Test Bot** busy", topic="busy")
    busy = b.actors["native2"]
    assert busy.turn and not idle.turn
    for message in fake.messages.values():  # both topics move while the queue is gone
        message["subject"] = {"idle": "idle moved", "busy": "busy moved"}.get(
            message["subject"], message["subject"]
        )
    seen = []
    replay = Catchup.replay

    async def recorded(catchup):
        for actor in (busy, idle):
            row = b.store.rows("sessions", id=actor.row["id"])[0]
            seen.append((row["topic"], row["id"] in b.unverified))
        return await replay(catchup)

    monkeypatch.setattr(Catchup, "replay", recorded)
    b.client.events = AsyncMock(
        side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), asyncio.CancelledError()]
    )
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert seen == [("busy moved", False), ("idle", True)]
    await b.housekeeping
    assert b.store.rows("sessions", id=idle.row["id"])[0]["topic"] == "idle moved"
    assert not b.unverified


def slow_housekeeping(b):
    """A housekeeping whose cancellation takes 0.2 s to unwind, as a session creation's
    workspace clean-up inside a reconciliation can: an event set once it unwinds."""
    unwinding = asyncio.Event()

    async def housekeep():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            unwinding.set()
            await asyncio.sleep(0.2)
            raise

    b.housekeeping = asyncio.create_task(housekeep())
    return unwinding


def polls(b, *, lose_first=False):
    """The listener's polls, counted: each answers nothing after 10 ms; the first, with
    `lose_first`, finds the queue gone."""
    count = [0]

    async def events(queue_id, last, dont_block=False):
        count[0] += 1
        if lose_first and count[0] == 1:
            raise ZulipError("BAD_EVENT_QUEUE_ID", 400)
        await asyncio.sleep(0.01)
        return []

    b.client.events = events
    return count


async def test_close_during_a_queue_loss_stops_the_listener(bridge):
    """Review of #965 (N1): `close()` cancels the listener while the listener, at a queue
    loss, waits for the housekeeping it cancelled. The listener's cancellation must not be
    taken for the housekeeping's: it was swallowed, the listener re-registered and went on
    polling, and `close()` never returned."""
    b, _fake, _backend = bridge
    unwinding = slow_housekeeping(b)
    count = polls(b, lose_first=True)
    b.listener = asyncio.create_task(b.listen())
    await asyncio.wait_for(unwinding.wait(), 5)  # the listener waits for it now
    closer = asyncio.create_task(b.close())
    try:
        await asyncio.wait_for(asyncio.shield(closer), 5)
    finally:
        b.listener.cancel()  # a hung close() returns once the listener really ends
        await asyncio.wait_for(closer, 5)
    assert count[0] == 1  # the poll that found the queue gone, and none after
    assert b.housekeeping is None


async def test_close_stops_the_listener_before_the_housekeeping(bridge):
    """Review of #965: nothing the listener reads after `close()` began is handled, and no
    recovery of its starts a housekeeping behind `close()`'s back: the listener goes first,
    then the housekeeping, however long that takes to unwind."""
    b, _fake, _backend = bridge
    unwinding = slow_housekeeping(b)
    count = polls(b)
    b.listener = asyncio.create_task(b.listen())
    await until(lambda: count[0] >= 2)
    closer = asyncio.create_task(b.close())
    await asyncio.wait_for(unwinding.wait(), 5)
    assert b.listener.done()
    before = count[0]
    await asyncio.wait_for(closer, 5)
    assert count[0] == before and b.housekeeping is None


async def test_a_second_queue_loss_during_the_recoverys_housekeeping(tmp_path, bridge):
    """Review of #965: a queue lost again while the first recovery's housekeeping confirms the
    cards. The second recovery's housekeeping reconciles from the older boundary: a message
    whose event was lost, older than the newest the listener handled, so no catch-up reads
    it, still reaches its session's topic, and the boundary is cleared once it is done."""
    _, _, backend = bridge
    async with many_sessions(tmp_path, bridge) as (b, fake, clock, _log, _cards):
        await asyncio.wait_for(b.start(), 10)
        b.listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await b.listener
        await housekeeping_done(b, clock)
        clock.advance(61)
        lost = fake.post_as(20, "@**Test Bot** LOST-token", topic="topic 7")  # its event lost
        seen = fake.post_as(20, "@**Test Bot** SEEN-token", topic="seen topic")
        backend.create_session.return_value = replace(
            backend.create_session.return_value, native_id="native2"
        )
        first: list[asyncio.Task] = []
        count = [0]

        async def events(queue_id, last, dont_block=False):
            count[0] += 1
            if count[0] == 1:  # the listener handles SEEN: the catch-ups start after it
                message = {"type": "message", "message": dict(seen), "flags": ["mentioned"]}
                return [message | {"id": 10**6}]
            if count[0] in (2, 4):  # 4: once the first housekeeping has spent its share
                if first:
                    await until(lambda: b.client.budget.free() <= b.poster.reserve)
                    assert not first[0].done()
                raise ZulipError("BAD_EVENT_QUEUE_ID", 400)
            if count[0] == 3:  # the first recovery's first poll
                first.append(b.housekeeping)
                return []
            if count[0] == 5:
                return []
            raise asyncio.CancelledError

        b.client.events = events
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(b.listen(), 10)
        assert first[0].cancelled() and b.housekeeping is not first[0]
        assert not b.store.rows("inbound", bot="bot", message_id=lost["id"])
        clock.advance(61)
        await housekeeping_done(b, clock)
        rows = b.store.rows("inbound", bot="bot", message_id=lost["id"])
        assert rows and rows[0]["session_id"] == "s7"
        assert b.store.get("catchup_reconcile:bot") is None


async def test_card_search_without_the_sender_narrow_still_finds_the_card(bridge):
    """A server refusing the `sender` operand (400): the bridge filters the topic itself."""
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(completed())
    for message in fake.messages.values():
        if message["subject"] == "topic":
            message["subject"] = "renamed"
    await missed(b, fake, "@**Test Bot** still here?", topic="renamed")
    request = b.client.request

    async def no_sender(method, endpoint, **params):
        if "sender" in json.dumps(params.get("narrow", [])):
            raise ZulipError("BAD_REQUEST", 400)
        return await request(method, endpoint, **params)

    b.client.request = no_sender
    await restart(b)
    assert not b.sender_narrow
    backend.create_session.assert_awaited_once()
    assert backend.start_turn.call_args.args[1].text.endswith("still here?")
    assert b.store.rows("sessions", id=actor.row["id"])[0]["topic"] == "renamed"


async def test_a_turn_adopted_at_reopen_posts_at_the_confirmed_location(bridge):
    """Codex review on #447: an idle session that nothing reaches at start-up, whose topic
    moved while the bridge was down, and whose backend reports a turn begun meanwhile (the
    TUI): the status of that turn goes to the card's location, confirmed first."""
    from dataclasses import replace

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(completed())
    old = time.time() - 30 * 86400  # housekeeping skips it
    b.store.update("sessions", {"id": actor.row["id"]}, updated_at=old)
    actor.row["updated_at"] = old
    for message in fake.messages.values():
        if message["subject"] == "topic":
            message["subject"] = "moved while down"
    b.unverify()  # as at start-up
    backend.open_session.return_value = replace(
        backend.create_session.return_value, active_turn_id="from-the-tui"
    )
    actor.put(Reopen())
    await actor.inbox.join()
    working = [m for m in fake.messages.values() if "via terminal" in m["content"]]
    assert [m["subject"] for m in working] == ["moved while down"]
    assert actor.row["topic"] == "moved while down" and actor.row["id"] not in b.unverified


@pytest.mark.parametrize("work", ["turn", "held"])
@pytest.mark.parametrize("change", ["moved", "public"])
async def test_sessions_with_work_are_confirmed_before_the_replay(
    bridge, monkeypatch, work, change
):
    """#447 review: the previous run's messages of a session with work in flight are edited
    by id (its status message, a waiting notice, an approval), and its held input starts a
    turn: its card is confirmed, read-only in a public channel, before the replay begins."""
    b, fake, _backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    if work == "held":
        await actor.event(completed())
        b.store.hold(actor.row["id"], 10**6, "Owner", "held while the bridge was down")
    if change == "moved":
        for message in fake.messages.values():
            if message["subject"] == "topic":
                message["subject"] = "moved while down"
    else:
        fake.streams[1]["invite_only"] = False
    seen = []
    replay = Catchup.replay

    async def recorded(self):
        row = b.store.rows("sessions", id=actor.row["id"])[0]
        seen.append((row["topic"], row["state"], row["id"] in b.unverified))
        return await replay(self)

    monkeypatch.setattr(Catchup, "replay", recorded)
    await restart(b)
    topic, state, unconfirmed = seen[0]
    assert not unconfirmed
    if change == "moved":
        assert topic == "moved while down"
    else:
        assert state == "readonly"


async def test_a_400_turns_off_the_sender_narrow_only_if_the_retry_succeeds(bridge):
    b, fake, _backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    request = b.client.request

    async def refuse(method, endpoint, **params):
        if endpoint == "messages" and "narrow" in params:
            raise ZulipError("BAD_REQUEST", 400)
        return await request(method, endpoint, **params)

    b.client.request = refuse
    with pytest.raises(ZulipError):
        await b.own_messages(1, "elsewhere")
    assert b.sender_narrow  # the 400 was not the sender operand's
    b.client.request = request
    assert await b.own_messages(1, "elsewhere") == []
    assert b.sender_narrow


async def test_a_channel_lookup_in_flight_when_its_channel_is_forgotten_is_not_cached(bridge):
    b, _fake, _backend = bridge
    request = b.client.request

    async def racing(method, endpoint, **params):
        result = await request(method, endpoint, **params)
        if endpoint == "streams/1":
            b.forget_channels({"type": "stream", "op": "update", "stream_id": 1})
        return result

    b.client.request = racing
    await b.channel(1)
    assert 1 not in b.channels
    b.client.request = request
    await b.channel(1)
    assert 1 in b.channels


async def test_a_live_move_of_an_unconfirmed_card_searches_topics_again(bridge):
    b, fake, _backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    card = b.actors["native"].row["card_message_id"]
    b.unverify()
    b.unverified.discard(b.actors["native"].row["id"])
    b.unverified.add("someone else")  # an unconfirmed card elsewhere keeps searches on
    await b.ensure_location(1, "elsewhere")
    assert (1, "elsewhere") in b.searched
    b.unverified.add(b.actors["native"].row["id"])
    await b.handle({"type": "update_message", "message_ids": [card], "subject": "renamed"})
    assert not b.searched
