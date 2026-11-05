"""Zulip outages without a restart (SPEC §7.8.2; end-to-end scenario 4).

Found live on Oct 14: Zulip blocked for 12 minutes during a Claude turn. The listener got a
new event queue afterwards, but the answer that could not be posted stayed undelivered, and
a message sent to a new topic during the block was never read, until the bridge restarted.
"""

import asyncio
import logging
import math
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core import types as t
from zab.core.session import StopTimeout
from zab.zulip.client import ZulipError

pytestmark = pytest.mark.integration


async def until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.fixture
async def live(bridge, monkeypatch, caplog):
    """The bridge with its listener running on a fresh queue; Zulip long-polls."""
    b, fake, backend = bridge
    caplog.set_level(logging.INFO, logger="zab.bridge")
    real_sleep = asyncio.sleep
    monkeypatch.setattr("zab.core.bridge.sleep", lambda s: real_sleep(min(s, 0.01)))
    fake.poll_timeout = 0.05
    await user_message(b, fake, "@**Test Bot** sleep, then answer")
    b.registration = await b.client.register()
    listener = asyncio.create_task(b.listen())
    yield b, fake, backend
    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener


def answered(fake, text):
    return any(text in m["content"] and m["sender_id"] == 1 for m in fake.messages.values())


def logged(caplog, text):
    return [r for r in caplog.records if text in r.getMessage()]


async def outage_during_answer(b, fake, caplog, token):
    """Zulip goes away; the turn then finishes, and its answer cannot be posted."""
    actor = b.actors["native"]
    fake.offline = True
    await asyncio.sleep(0.2)  # the long poll fails; the listener retries
    actor.put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text=token
        )
    )
    await actor.inbox.join()
    assert actor.completion and actor.row["state"] == "error"
    assert not answered(fake, token)
    return actor


async def test_outage_longer_than_the_queue_delivers_answer_and_gap_message(live, caplog):
    b, fake, backend = live
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    queue = fake._queue_id
    actor = await outage_during_answer(b, fake, caplog, "OUTAGE-token")
    # Someone writes in a new topic; then Zulip drops the queue nobody polled.
    fake.post_as(20, "@**Test Bot** DURING-token", topic="during")
    fake.lose_queues()
    await asyncio.sleep(0.1)
    fake.offline = False
    await until(lambda: answered(fake, "OUTAGE-token"))
    await until(lambda: backend.start_turn.await_count == 2)
    await actor.inbox.join()  # the answer is posted before complete() settles the turn
    assert backend.start_turn.call_args.args[1].text.endswith("DURING-token")
    assert fake._queue_id == queue + 1
    assert actor.completion is None and actor.row["state"] == "idle"
    for text in ("Zulip event queue expired", "Registered a new", "caught up", "reachable again"):
        assert logged(caplog, text), text
    assert len(logged(caplog, "Zulip unreachable")) == 1  # once per outage, not per retry


def bot_posts(fake, text, topic=None):
    return [
        m
        for m in fake.messages.values()
        if m["sender_id"] == 1 and text in m["content"] and topic in (None, m["subject"])
    ]


def finish(actor, turn_id, text):
    actor.put(
        t.TurnCompleted(
            session_id=actor.row["native_id"],
            turn_id=turn_id,
            status=t.TurnStatus.COMPLETED,
            final_text=text,
        )
    )


@pytest.mark.parametrize("queue_lost", [True, False], ids=["queue-lost", "queue-kept"])
@pytest.mark.parametrize("busy", [True, False], ids=["turn-running", "idle"])
@pytest.mark.parametrize("target", ["new-topic", "session"])
@pytest.mark.parametrize("arrives", ["as-zulip-goes", "during-outage"])
async def test_a_message_received_as_zulip_goes_away_is_answered_once(
    live, arrives, target, busy, queue_lost
):
    """Scenario 4 on c114d64f (issue #532): the DURING message reached the bridge 65 ms
    into the block, in a new topic, and creating its session failed at the card. The
    message stayed claimed and its pending payload was dropped, so neither the retried
    poll nor the catch-up ever answered it.

    `target`: a new topic, or a follow-up in a topic whose session is idle. `busy`: the
    outage turn (`sleep 120`) in another topic is still running meanwhile. `queue_lost`:
    the outage outlasts the event queue. `during-outage`: the message arrives after the
    block took effect, the case that already passed: still one answer."""
    b, fake, backend = live
    fake.members.append({"user_id": 20, "is_bot": False, "full_name": "Owner"})  # GET /users
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    backend.start_turn.side_effect = ["turn2", "turn3"]  # the outage turn is turn1
    first = b.actors["native"]
    if not busy:
        finish(first, "turn1", "x")
        await first.inbox.join()
        assert first.turn is None
    turn = "turn2"
    if target == "session":
        fake.post_as(20, "@**Test Bot** earlier", topic="during")
        await until(lambda: "native2" in b.actors and b.actors["native2"].turn)
        finish(b.actors["native2"], "turn2", "earlier answer")
        await until(lambda: bot_posts(fake, "earlier answer") and not b.actors["native2"].turn)
        turn = "turn3"
    starts = backend.start_turn.await_count
    if arrives == "as-zulip-goes":
        fake.cut_after = "DURING-token"  # the long poll returns it as the connections drop
        fake.post_as(20, "@**Test Bot** DURING-token", topic="during")
        await until(lambda: fake.offline)
    else:
        fake.offline = True
        await asyncio.sleep(0.1)
        fake.post_as(20, "@**Test Bot** DURING-token", topic="during")
    await asyncio.sleep(0.2)  # Zulip stays down; the listener retries
    if queue_lost:
        fake.lose_queues()  # nobody polled it for ten minutes
    fake.offline = False
    await until(lambda: backend.start_turn.await_count == starts + 1)
    assert backend.start_turn.call_args.args[1].text.endswith("DURING-token")
    second = b.actors["native2"]
    await until(lambda: second.turn is not None and second.turn["native_turn_id"] == turn)
    finish(second, turn, "DURING-answer")
    await until(lambda: bot_posts(fake, "DURING-answer"))
    await asyncio.sleep(0.2)  # time for a second answer, if anything would post one
    for actor in list(b.actors.values()):
        await actor.inbox.join()
    assert len(bot_posts(fake, "DURING-answer")) == 1
    assert backend.start_turn.await_count == starts + 1
    assert len(bot_posts(fake, "**zab session**", topic="during")) == 1
    assert len(b.store.rows("sessions", topic="during")) == 1
    assert second.row["state"] == "idle" and second.turn is None
    assert not b.store.pending_deliveries(b.bot.name)
    if busy:  # the outage turn still runs, and its session is untouched
        assert first.turn and first.row["state"] == "running"


async def test_the_interrupted_first_prompt_still_creates_the_session(live):
    """Zulip back on the same queue: the retried poll brings a later message in the topic
    too. The session is the first prompt's, with its options, not the later message's."""
    b, fake, backend = live
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    fake.cut_after = "DURING-token"
    fake.post_as(20, "@**Test Bot** !new --model m1 DURING-token", topic="during")
    await until(lambda: fake.offline)
    await asyncio.sleep(0.1)
    fake.post_as(20, "@**Test Bot** DURING-later", topic="during")
    fake.offline = False
    await until(lambda: "native2" in b.actors and b.actors["native2"].turn)
    await asyncio.sleep(0.1)
    await b.actors["native2"].inbox.join()
    assert backend.create_session.await_args.args[0].settings.model == "m1"
    texts = [c.args[1].text for c in backend.start_turn.await_args_list[1:]]
    texts += [c.args[2].text for c in backend.steer.await_args_list]
    assert sum("DURING-token" in text for text in texts) == 1
    assert sum("DURING-later" in text for text in texts) == 1
    assert len(b.store.rows("sessions", topic="during")) == 1


@pytest.mark.parametrize("later", [False, True], ids=["alone", "later-message"])
async def test_a_creation_zulip_interrupts_again_is_retried_after_the_next_poll(
    live, monkeypatch, caplog, later
):
    """Zulip answers the poll that ends the outage, then fails the card once more: the
    creation waits for the next poll, not for the next outage or restart. `later`: that
    poll also brings a later message in the topic. It is not handled before the first
    prompt's session exists, or it would create the session itself, without the first
    prompt's options (codex review on #533)."""
    import zab.core.bridge as module

    b, fake, backend = live
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    original, calls = module.create_session, []

    async def create(*args):
        calls.append(args)
        if len(calls) == 2:  # the first retry, as the outage ends
            raise httpx.ConnectError("flap")
        return await original(*args)

    monkeypatch.setattr(module, "create_session", create)
    fake.cut_after = "DURING-token"
    fake.post_as(20, "@**Test Bot** !new --model m1 DURING-token", topic="during")
    await until(lambda: fake.offline)
    await asyncio.sleep(0.1)
    if later:
        fake.post_as(20, "@**Test Bot** DURING-later", topic="during")
    fake.offline = False
    await until(lambda: backend.start_turn.await_count == 2)
    await asyncio.sleep(0.1)
    await b.actors["native2"].inbox.join()
    assert len(calls) == 3
    assert [c[1]["id"] for c in calls] == [calls[0][1]["id"]] * 3
    assert backend.create_session.await_args.args[0].settings.model == "m1"
    texts = [c.args[1].text for c in backend.start_turn.await_args_list[1:]]
    texts += [c.args[2].text for c in backend.steer.await_args_list]
    assert sum("DURING-token" in text for text in texts) == 1
    assert sum("DURING-later" in text for text in texts) == later
    assert len(b.store.rows("sessions", topic="during")) == 1
    assert logged(caplog, "Pending creation recovery: ConnectError")
    assert not b.store.pending_deliveries(b.bot.name) and not b.creations_interrupted


async def test_startup_stops_before_the_replay_when_zulip_interrupts_a_creation(
    bridge, monkeypatch
):
    """A first prompt left pending (a crash, or Zulip, issue #532) whose creation Zulip
    interrupts again at startup: the start fails, as when the replay fails, rather than
    replay a later message that would create the topic's session in its place."""
    import zab.core.bridge as module

    b, fake, _backend = bridge
    monkeypatch.setattr(module, "create_session", AsyncMock(side_effect=httpx.ConnectError("x")))
    first = await b.client.send(1, "fresh", "@**Test Bot** !new --model m1 first")
    with pytest.raises(httpx.ConnectError):
        message = fake.messages[first] | {"sender_id": 20}
        await b.handle({"type": "message", "message": message, "flags": ["mentioned"]})
    replay = AsyncMock(return_value=0)
    monkeypatch.setattr(module.Catchup, "replay", replay)
    with pytest.raises(httpx.ConnectError):
        await b.start()
    replay.assert_not_awaited()
    assert [p["message_id"] for p in b.store.pending_deliveries(b.bot.name)] == [first]


@pytest.mark.parametrize("queue_lost", [False, True], ids=["queue-kept", "queue-lost"])
async def test_a_creation_zulip_keeps_refusing_is_retried_with_backoff(
    live, monkeypatch, caplog, queue_lost
):
    """Issue #534, from the review of #533: Zulip answers polls, and fails every post with
    a 502. Each failed retry of the pending creation re-armed the outage probe, so every
    second the bridge created a worktree and a backend session and discarded them, posted
    every undelivered answer again and wrote 3 log lines: about 60 creations a minute. After
    a lost queue, each retry also refreshed the users and every card. The retry backs off
    now, 1 s doubling to 60 s, without the probe or the refreshes. A later message in the
    topic still waits for the first prompt's session (codex review on #533)."""
    b, fake, backend = live
    fake.members.append({"user_id": 20, "is_bot": False, "full_name": "Owner"})  # GET /users
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    clock, start, minute = [0.0], [math.inf], asyncio.Event()
    real_sleep = asyncio.sleep

    def elapsed():  # simulated seconds since Zulip began to fail the posts
        return clock[0] - start[0]

    async def simulated(seconds):  # the listener's waits, on a simulated clock
        clock[0] += seconds
        if elapsed() >= 60:
            await minute.wait()  # the minute is over: the listener waits here
        await real_sleep(0.001)

    monkeypatch.setattr("zab.core.bridge.sleep", simulated)
    fake.post_failure = 502
    first = b.actors["native"]
    finish(first, "turn1", "OUTAGE-answer")  # its answer cannot be posted either
    await first.inbox.join()
    assert first.completion and first.row["state"] == "error"
    before, requests = backend.create_session.await_count, len(fake.requests)

    def attempts():
        return backend.create_session.await_count - before

    if queue_lost:  # sent while Zulip was away for good: the catch-up reads it
        fake.offline = True
        await asyncio.sleep(0.1)
        fake.post_as(20, "@**Test Bot** !new --model m1 FIRST-token", topic="during")
        fake.lose_queues()
        start[0] = clock[0]
        fake.offline = False
    else:
        start[0] = clock[0]
        fake.post_as(20, "@**Test Bot** !new --model m1 FIRST-token", topic="during")
    await until(lambda: attempts() > 0)
    fake.post_as(20, "@**Test Bot** LATER-token", topic="during")
    await until(lambda: elapsed() >= 60 or attempts() > 7)
    # At 0, 1, 2, 4, 8, 16 and 32 s: the first attempt, then 6 retries; then 60 s apart.
    assert attempts() == 7, f"{attempts()} attempts in {elapsed():.0f} s"
    assert len(logged(caplog, "Retrying 1 undelivered answers")) <= 1
    assert len(logged(caplog, "reachable again")) <= 1  # one outage, not one a second
    assert len(logged(caplog, "Zulip SERVER_ERROR (HTTP 502)")) == 1
    # The queue-loss recovery's refreshes: before the catch-up's attempt, and after it.
    users = [r for r in fake.requests[requests:] if r[:2] == ("GET", "users")]
    assert len(users) == (2 if queue_lost else 0)
    fake.post_failure = None  # Zulip takes posts again
    minute.set()
    await until(lambda: "native2" in b.actors and b.actors["native2"].turn)
    await until(lambda: answered(fake, "OUTAGE-answer"))  # retried once more, as it ends
    await asyncio.sleep(0.1)
    for actor in list(b.actors.values()):
        await actor.inbox.join()
    assert backend.create_session.await_args.args[0].settings.model == "m1"
    texts = [c.args[1].text for c in backend.start_turn.await_args_list[1:]]
    texts += [c.args[2].text for c in backend.steer.await_args_list]
    assert sum("FIRST-token" in text for text in texts) == 1
    assert sum("LATER-token" in text for text in texts) == 1
    assert len(b.store.rows("sessions", topic="during")) == 1
    assert len(bot_posts(fake, "**zab session**", topic="during")) == 1
    assert len(bot_posts(fake, "OUTAGE-answer")) == 1
    assert not b.store.pending_deliveries(b.bot.name) and not b.creations_interrupted


async def interrupted_creation(b, fake, monkeypatch, topic, text):
    """A first prompt whose creation Zulip interrupted at the card (issue #532): pending."""
    import zab.core.bridge as module

    original = module.create_session
    monkeypatch.setattr(module, "create_session", AsyncMock(side_effect=httpx.ConnectError("x")))
    mid = await b.client.send(1, topic, f"@**Test Bot** {text}")
    with pytest.raises(httpx.ConnectError):
        message = fake.messages[mid] | {"sender_id": 20}
        await b.handle({"type": "message", "message": message, "flags": ["mentioned"]})
    assert mid in [p["message_id"] for p in b.store.pending_deliveries(b.bot.name)]
    monkeypatch.setattr(module, "create_session", original)
    return mid


@pytest.mark.parametrize("directory", ["emptied", "refilled"])
async def test_a_failed_user_refresh_keeps_a_pending_creation(
    bridge, monkeypatch, caplog, directory
):
    """Issue #534: a failed `refresh_users()` empties the directory, to fail closed, and a
    backend reconnect (`Runtime.finish_recovery`) runs the recovery during the outage. The
    payload, whose sender was then unknown, was deleted: the first prompt was lost, and a
    later message in the topic created the session without its `!new` options.

    `refilled`: a channel-membership lookup (`multi_party`) refilled the directory without
    the allowlist. An allowlist of e-mails, resolved from the directory, stays empty."""
    b, fake, backend = bridge
    caplog.set_level(logging.INFO, logger="zab.bridge")
    mid = await interrupted_creation(b, fake, monkeypatch, "fresh", "!new --model m1 first")
    fake.offline = True
    with pytest.raises(httpx.ConnectError):
        await b.refresh_users()
    assert not b.users
    if directory == "refilled":
        b.users[20] = {"user_id": 20, "is_bot": False, "full_name": "Owner"}
        b.router.allowlist = frozenset()  # as an allowlist of e-mails is left
    b.creations_interrupted = False
    for _ in range(2):  # backend reconnects, Zulip still away
        await b.recover_pending_creations()
    assert [p["message_id"] for p in b.store.pending_deliveries(b.bot.name)] == [mid]
    assert len(logged(caplog, "until its sender can be checked")) == 1  # logged once
    assert b.creations_interrupted  # the listener retries after its next successful poll
    backend.create_session.assert_not_awaited()
    fake.offline = False
    fake.members.append({"user_id": 20, "is_bot": False, "full_name": "Owner"})
    await b.recover_pending_creations()  # it refreshes the users first
    await until(lambda: "native" in b.actors and b.actors["native"].turn)
    assert backend.create_session.await_args.args[0].settings.model == "m1"
    assert not b.store.pending_deliveries(b.bot.name)


async def test_a_stale_pending_creation_expires_with_one_warning(bridge, monkeypatch):
    """Issue #534: a first prompt that Zulip interrupted, whose retry then fails for a
    reason of its own, stayed pending with no ⚠️, and could land hours later in a session a
    newer message created. Past the catch-up's two hours it gets one ⚠️ that names it, and is
    dropped. A fresh one is kept."""
    import zab.core.bridge as module
    from zab.core.catchup import MAX_AGE

    b, fake, backend = bridge
    stale = await interrupted_creation(b, fake, monkeypatch, "stale", "stale prompt")
    fresh = await interrupted_creation(b, fake, monkeypatch, "fresh", "fresh prompt")
    retry = AsyncMock(side_effect=t.BackendUnavailable("down"))
    monkeypatch.setattr(module, "create_session", retry)
    await b.recover_pending_creations()
    assert retry.await_count == 2
    assert len(b.store.pending_deliveries(b.bot.name)) == 2
    assert not bot_posts(fake, "⚠️")
    key = f"pending_delivery:{b.bot.name}:{stale}"
    pending = b.store.get(key)
    pending["message"]["timestamp"] = time.time() - MAX_AGE - 60
    b.store.set(key, pending)
    for _ in range(2):
        await b.recover_pending_creations()
    warnings = bot_posts(fake, "⚠️")
    assert len(warnings) == 1 and warnings[0]["subject"] == "stale"
    assert f"/near/{stale})" in warnings[0]["content"]
    assert "within 2 hours and has expired" in warnings[0]["content"]  # MAX_AGE
    assert warnings[0]["content"].startswith(b.note("[This message]("))  # #677
    assert [p["message_id"] for p in b.store.pending_deliveries(b.bot.name)] == [fresh]
    assert retry.await_count == 4  # the fresh one, twice; the stale one is not retried
    backend.start_turn.assert_not_awaited()


def age(b, mid, seconds):
    """The pending first prompt `mid`, as if it had been sent `seconds` ago."""
    key = f"pending_delivery:{b.bot.name}:{mid}"
    pending = b.store.get(key)
    pending["message"]["timestamp"] = time.time() - seconds
    b.store.set(key, pending)
    return key


async def test_no_expiry_warning_for_a_prompt_another_recovery_binds(bridge, monkeypatch):
    """Review of #537: two recoveries at once, the listener's and a backend reconnect's.
    R1 reads the prompt as fresh and binds it to the topic's session, which a later message
    created, and whose actor is busy: a follow-up's steer is in flight. R2's list, read as
    the prompt crosses the limit, still shows it unbound. R2 posted the ⚠️ after R1 released
    the creation lock, and the prompt ran all the same."""
    from test_bridge import user_message

    from zab.core.catchup import MAX_AGE

    b, fake, backend = bridge
    mid = await interrupted_creation(b, fake, monkeypatch, "edge", "EDGE-prompt")
    await user_message(b, fake, "@**Test Bot** later prompt", topic="edge")  # the session
    steering = asyncio.Event()

    async def steer(*args):
        await steering.wait()

    backend.steer.side_effect = steer
    follow_up = await b.client.send(1, "edge", "a follow-up")
    await b.handle({"type": "message", "message": fake.messages[follow_up] | {"sender_id": 20}})
    await until(lambda: backend.steer.await_count == 1)  # the actor is busy
    await b.creation_lock.acquire()  # a creation in progress meanwhile
    first = asyncio.create_task(b.recover_pending_creations())
    await asyncio.sleep(0.05)  # fresh: it waits for the lock to bind the prompt
    age(b, mid, MAX_AGE + 1)
    second = asyncio.create_task(b.recover_pending_creations())
    await asyncio.sleep(0.05)  # expired: it waits for the lock behind the first
    b.creation_lock.release()
    await asyncio.gather(first, second)
    steering.set()
    for actor in list(b.actors.values()):
        await actor.inbox.join()
    assert not bot_posts(fake, "has expired")
    texts = [c.args[1].text for c in backend.start_turn.await_args_list]
    texts += [c.args[2].text for c in backend.steer.await_args_list]
    assert sum("EDGE-prompt" in text for text in texts) == 1
    assert not b.store.pending_deliveries(b.bot.name)


async def test_a_pending_creation_whose_sender_is_not_listed_is_dropped(
    bridge, monkeypatch, caplog
):
    """Review of #537: a sender that a successful refresh doesn't list, gone from the
    organization, can never be checked. The payload is dropped, as before #534, rather
    than kept, and refreshed for, at every recovery."""
    b, fake, backend = bridge
    caplog.set_level(logging.INFO, logger="zab.bridge")
    await interrupted_creation(b, fake, monkeypatch, "fresh", "first")
    await b.refresh_users()  # succeeds: GET /users doesn't list user 20
    assert 20 not in b.users and not b.users_failed
    await b.recover_pending_creations()
    assert not b.store.pending_deliveries(b.bot.name)
    assert len(logged(caplog, "its sender is not listed")) == 1
    assert not bot_posts(fake, "⚠️")
    backend.create_session.assert_not_awaited()


@pytest.mark.parametrize("status", [502, 400])
async def test_an_expiry_reply_zulip_fails_is_retried_and_one_it_refuses_is_not(
    bridge, monkeypatch, caplog, status
):
    """Review of #537: the expired prompt's ⚠️ fails. A 5xx keeps the payload, so that the
    reply is retried with it: a backend reconnect's recovery flags it for the listener,
    whose own re-raises. A 4xx would be refused again on every retry: the payload is
    dropped without its reply."""
    from zab.core.catchup import MAX_AGE

    b, fake, backend = bridge
    caplog.set_level(logging.INFO, logger="zab.bridge")
    mid = await interrupted_creation(b, fake, monkeypatch, "old", "old prompt")
    key = age(b, mid, MAX_AGE + 60)
    fake.post_failure = status
    b.creations_interrupted = False
    if status >= 500:
        await b.recover_pending_creations()  # a backend reconnect's
        assert b.store.get(key) and b.creations_interrupted
        with pytest.raises(ZulipError):
            await b.recover_pending_creations(interrupt=True)  # the listener's
        assert b.store.get(key)
        fake.post_failure = None
        await b.recover_pending_creations(interrupt=True)
        assert len(bot_posts(fake, "has expired", topic="old")) == 1
    else:
        await b.recover_pending_creations(interrupt=True)
        assert logged(caplog, "reply refused (Zulip BAD_REQUEST, HTTP 400)")
        assert not bot_posts(fake, "has expired") and not b.creations_interrupted
    assert b.store.get(key) is None
    backend.create_session.assert_not_awaited()


@pytest.mark.parametrize(
    "error, unreachable",
    [
        (lambda: httpx.ConnectError("offline"), True),
        (lambda: httpx.ReadTimeout("slow"), True),
        (lambda: ZulipError("BAD_GATEWAY", 502), True),
        (lambda: ZulipError("BAD_REQUEST", 400), False),  # Zulip refuses: no retry helps
        (lambda: OSError("disk full"), False),  # the workspace's, not Zulip's
        (lambda: ValueError("add this channel to [[projects]]"), False),
    ],
)
async def test_only_zulip_unreachable_keeps_a_failed_creation_pending(
    bridge, monkeypatch, error, unreachable
):
    """SPEC §7.2: a creation that fails for a reason of its own is answered with ⚠️ and
    dropped; one that Zulip interrupted stays pending, without a reply, for the listener."""
    import zab.core.bridge as module

    b, fake, _backend = bridge
    exc = error()
    monkeypatch.setattr(module, "create_session", AsyncMock(side_effect=exc))
    mid = await b.client.send(1, "fresh", "@**Test Bot** hello")
    event = {"type": "message", "message": fake.messages[mid] | {"sender_id": 20}}
    if unreachable:
        with pytest.raises(type(exc)):
            await b.handle(event | {"flags": ["mentioned"]})
        assert [p["message_id"] for p in b.store.pending_deliveries(b.bot.name)] == [mid]
        assert b.creations_interrupted
    else:
        await b.handle(event | {"flags": ["mentioned"]})
        assert not b.store.pending_deliveries(b.bot.name)
        assert not b.creations_interrupted
        posts = bot_posts(fake, "⚠️", topic="fresh")  # the failed creation's, marked (#677)
        assert posts and all(p["content"].startswith(b.note("")) for p in posts)


async def test_short_outage_retries_the_answer_on_the_same_queue(live, caplog):
    b, fake, _backend = live
    queue = fake._queue_id
    actor = await outage_during_answer(b, fake, caplog, "SHORT-token")
    fake.offline = False
    await until(lambda: answered(fake, "SHORT-token"))
    await actor.inbox.join()
    assert fake._queue_id == queue  # the queue survived: no re-registration
    assert actor.completion is None and actor.row["state"] == "idle"
    assert logged(caplog, "Retrying 1 undelivered answers")


async def test_catch_up_after_queue_loss_skips_what_the_listener_handled(live, caplog):
    """Login DMs are never claimed: a replay from the claim table alone would hand a finished
    `!login` back to the login flow."""
    b, fake, _backend = live
    calls = []
    real = b.login.handle

    async def handle(message):
        calls.append(message["id"])
        return message["content"] == "!login" or await real(message)

    b.login.handle = handle
    login = fake.post_as(20, "!login")
    await until(lambda: login["id"] in calls)
    fake.offline = True
    fake.lose_queues()
    await asyncio.sleep(0.2)  # the long poll fails; the listener retries
    gap = fake.post_as(20, "!help")
    fake.offline = False
    await until(lambda: gap["id"] in calls)  # the catch-up reads the gap after the login DM
    assert calls.count(login["id"]) == 1
    assert calls.count(gap["id"]) == 1


async def test_a_failed_status_refresh_is_no_session_error(live, caplog):
    b, fake, _backend = live
    actor = b.actors["native"]
    actor.last_status = ""  # force an edit
    fake.offline = True
    actor.put("tick")
    await actor.inbox.join()
    assert actor.row["state"] == "running"
    assert logged(caplog, "Status refresh failed: ConnectError")


async def test_an_unconfirmed_stop_still_waits_for_status(live, caplog):
    """A stop timeout is not proof the turn ended: its slot waits for `!status` (SPEC §7.7)."""
    b, fake, _backend = live
    actor = b.actors["native"]
    fake.offline = True
    await asyncio.sleep(0.2)  # the long poll fails; the listener retries
    actor.put(StopTimeout("turn1"))
    await actor.inbox.join()
    assert actor.completion and actor.unconfirmed_turn
    fake.offline = False
    await until(lambda: logged(caplog, "Retrying 1 undelivered answers"))
    await actor.inbox.join()
    assert actor.completion and actor.unconfirmed_turn and actor.row["state"] == "error"


async def test_a_pending_answer_status_shows_how_the_turn_ended(bridge):
    """Scenario 4 on 0bca627: right after the reconnect, a tick rendered the finished turn under
    the session's state, a raw "error · 12m 20s" heading, before the answer's retry posted it."""
    b, fake, _backend = bridge  # no listener: nothing retries the answer here
    await user_message(b, fake, "@**Test Bot** sleep, then answer")
    actor = b.actors["native"]
    fake.offline = True
    actor.put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="X"
        )
    )
    await actor.inbox.join()
    assert actor.completion and actor.row["state"] == "error"
    fake.offline = False
    actor.last_status = ""
    actor.retried = None  # a look a gap after the failed try (review round 2 of #755)
    actor.put("tick")
    await actor.inbox.join()
    assert fake.messages[actor.turn["status_message_id"]]["content"].startswith("✅ **Done** · ")


def test_status_headings_never_show_a_raw_state_word():
    from zab.core import presentation as view
    from zab.render.markdown import MarkdownRenderer

    def heading(state):
        return view.status(state, time.time(), [], MarkdownRenderer(), 10000).split(" · ")[0]

    assert heading("error") == "⚠️ **Error**"
    assert heading("backend reconnecting") == "**Backend reconnecting**"  # §7.8.3
    assert heading("running") == "⏳ **Working**"


async def test_queue_loss_after_startup_reconciliation_still_replays_the_gap(bridge):
    """Scenario 4 on #447 (d141d4d): the event queue expired while the start-up housekeeping
    was still running. Back online, housekeeping reconciled a session topic before the
    listener saw the queue loss, and claimed a message there newer than a new-topic mention
    and a DM sent during the outage; the recovery's catch-up started after that claim and
    never read them. Reconciliation claims nothing past what the replay before it read."""
    from unittest.mock import AsyncMock

    from test_catchup import missed

    from zab.core.catchup import Catchup
    from zab.zulip.client import ZulipError

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    await b.actors["native"].event(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="x"
        )
    )
    backend.create_session.return_value = replace(
        backend.create_session.return_value, native_id="native2"
    )
    startup = Catchup(b)  # a start-up whose replay ran; its housekeeping is still to come
    await startup.replay()
    # Zulip unreachable: what is sent meanwhile reaches the server, not the bridge.
    during = await missed(b, fake, "@**Test Bot** DURING-token", topic="during")
    dm = await missed(b, fake, "!help", dm=True)
    newer = await missed(b, fake, "⚠️ Answer delivery is pending", topic="topic", sender=1)
    assert newer["id"] > dm["id"] > during["id"]
    # Back online, the housekeeping reconciles the session's topic first.
    await b.reconcile_messages(catchup=startup, background=True)
    assert not b.store.rows("inbound", bot=b.bot.name, message_id=newer["id"])
    # Then the listener finds its queue gone and recovers.
    b.client.events = AsyncMock(
        side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), asyncio.CancelledError()]
    )
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    for actor in list(b.actors.values()):
        await actor.inbox.join()
    assert backend.start_turn.await_count == 2
    assert backend.start_turn.call_args.args[1].text.endswith("DURING-token")
    replies = [m for m in fake.messages.values() if m["type"] == "private" and m["sender_id"] == 1]
    assert len(replies) == 1
