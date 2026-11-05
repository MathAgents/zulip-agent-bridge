import asyncio
import time

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core import types as t
from zab.core.catchup import EXPIRED, Catchup

pytestmark = pytest.mark.integration


async def missed(b, fake, text, *, dm=False, topic="new topic", age=0, sender=20):
    result = await b.client.request(
        "POST",
        "messages",
        type="private" if dm else "stream",
        to=[1, sender] if dm else 1,
        topic=topic,
        content=text,
    )
    message = fake.messages[result["id"]]
    message.update(
        sender_id=sender,
        timestamp=time.time() - age,
        flags=["mentioned"],
        display_recipient=[{"id": 1}, {"id": sender}],
    )
    return message


async def settle(b):
    for actor in b.actors.values():
        await actor.inbox.join()
        await asyncio.sleep(0)
        await actor.inbox.join()


async def restart(b, *, first_start=False):
    if not first_start and b.store.get(f"catchup_cursor:{b.bot.name}") is None:
        # These restart probes model a previously established replay boundary.
        b.store.set(f"catchup_cursor:{b.bot.name}", 0)
    # start() is the production path, including pre-existing topic reconciliation.
    await b.start()
    b.listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b.listener
    await b.housekeeping  # topic reconciliation follows the replay (#443)
    await settle(b)


@pytest.mark.parametrize("command", ["!help", "!status"])
async def test_restart_new_topic_and_dm_with_live_overlap(bridge, command):
    b, fake, backend = bridge
    topic = await missed(b, fake, "@**Test Bot** start here")
    dm = await missed(b, fake, command, dm=True)
    await restart(b)
    backend.start_turn.assert_awaited_once()
    assert backend.start_turn.call_args.args[1].text.endswith("start here")
    replies = [m for m in fake.messages.values() if m["type"] == "private" and m["sender_id"] == 1]
    assert len(replies) == 1
    for message in (topic, dm):
        await b.handle({"type": "message", "message": message})
    await settle(b)
    assert backend.start_turn.await_count == 1
    assert [
        m for m in fake.messages.values() if m["type"] == "private" and m["sender_id"] == 1
    ] == replies


async def test_topic_reconciliation_cannot_skip_earlier_dm(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    await missed(b, fake, "!help", dm=True)
    await missed(b, fake, "follow up", topic="topic")
    await restart(b)
    assert backend.steer.await_count == 1
    assert (
        len([m for m in fake.messages.values() if m["type"] == "private" and m["sender_id"] == 1])
        == 1
    )


@pytest.mark.parametrize("dm", [False, True])
async def test_old_messages_only_get_one_notice_per_conversation(bridge, dm):
    b, fake, backend = bridge
    fake.members.append({"user_id": 20, "is_bot": False, "full_name": "Owner"})
    await b.refresh_users()
    old = [await missed(b, fake, "@**Test Bot** old", dm=dm, age=7201) for _ in range(2)]
    await missed(b, fake, "@**Test Bot** unauthorized", dm=dm, age=7201, sender=99)
    await restart(b)
    backend.start_turn.assert_not_awaited()
    assert len([m for m in fake.messages.values() if m["content"] == b.note(EXPIRED)]) == 1
    await restart(b)
    for message in old:
        await b.handle({"type": "message", "message": message})
    assert len([m for m in fake.messages.values() if m["content"] == b.note(EXPIRED)]) == 1


async def test_old_followup_is_not_executed_by_topic_reconciliation(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    await missed(b, fake, "stale follow up", topic="topic", age=7201)
    await restart(b)
    backend.steer.assert_not_awaited()
    assert any(m["content"] == b.note(EXPIRED) for m in fake.messages.values())


async def test_idle_topic_older_than_seven_days_is_recovered(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="done"
        )
    )
    b.store.update("sessions", {"id": actor.row["id"]}, updated_at=time.time() - 8 * 86400)
    await missed(b, fake, "resume this", topic="topic")
    await restart(b)
    assert backend.start_turn.await_count == 2


async def test_paging_keeps_flags_and_does_not_repeat_claimed_message(bridge):
    b, fake, backend = bridge
    # Other senders fill the first page without generating 100 replies.
    for _ in range(101):
        await missed(b, fake, "unrelated", sender=99)
    message = await missed(b, fake, "@**Test Bot** after the page boundary")
    # missed() posts through the bot's own budget: a new process starts with it empty, and
    # housekeeping would otherwise wait a real minute for its reserve (#447 review).
    b.client.budget.sent.clear()
    await restart(b)
    assert backend.start_turn.await_count == 1
    pages = [
        p
        for method, path, p in fake.requests
        if method == "GET" and path == "messages" and "narrow" not in p
    ]
    assert any(p["anchor"] == "100" for p in pages)
    await Catchup(b).replay()
    await b.handle({"type": "message", "message": message})
    assert backend.start_turn.await_count == 1


async def test_login_replay_uses_only_cursor_and_never_persists_code(bridge, caplog):
    b, fake, backend = bridge
    codes = []

    async def login(account, send, receive):
        codes.append(await receive(1))
        return True

    backend.login.side_effect = login
    login_message = await missed(b, fake, "!login", dm=True)
    code = await missed(b, fake, "DECOY-private-code", dm=True)
    await restart(b)
    if b.login.task:
        await b.login.task
    await b.handle({"type": "message", "message": login_message})
    await b.handle({"type": "message", "message": code})
    assert backend.login.await_count == 1
    assert codes == ["DECOY-private-code"]
    dump = "\n".join(b.store.db.iterdump())
    assert "DECOY-private-code" not in dump + caplog.text
    assert b.store.rows("inbound", bot=b.bot.name) == []
    assert b.store.get(f"catchup_cursor:{b.bot.name}") == code["id"]


async def test_restart_after_crash_during_topic_reconciliation_keeps_boundary(bridge):
    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    dm = await missed(b, fake, "!help", dm=True)
    await missed(b, fake, "follow up", topic="topic")
    interrupted = Catchup(b)
    await b.reconcile_messages(catchup=interrupted)
    assert max(m["message_id"] for m in b.store.rows("inbound")) > dm["id"]
    # The process exits here before combined-feed replay. A new Catchup must
    # recover the persisted boundary, not the newly advanced inbound maximum.
    await Catchup(b).replay()
    assert any(m["type"] == "private" and m["sender_id"] == 1 for m in fake.messages.values())
    assert b.store.get(interrupted.pending_key) is None


async def test_an_expired_mention_of_another_bot_leaves_the_follow_up_to_all(bridge):
    """An expired mention of another bot decides nothing for the recent follow-up without a
    mention, which goes to all of the VM's bots, this one included (#713; before, the expired
    mention made it the other bot's)."""
    b, fake, backend = bridge
    fake.members += [
        {"user_id": 20, "full_name": "Owner", "is_bot": False},
        {"user_id": 4, "full_name": "Claude", "is_bot": True},
    ]
    await b.refresh_users()
    await user_message(b, fake, "@**Test Bot** begin")
    old = await missed(b, fake, "@**Claude** old request", topic="topic", age=7201)
    old["flags"] = []
    follow = await missed(b, fake, "follow up for both", topic="topic")
    await restart(b)
    assert not b.store.get(f"message_audience:bot:{old['id']}")["addressed"]
    texts = [c.args[2].text for c in backend.steer.await_args_list]
    texts += [c.args[1].text for c in backend.start_turn.await_args_list]
    assert any(text.endswith("follow up for both") for text in texts), follow["id"]


@pytest.mark.parametrize(
    "text,topic,guest",
    [
        ("@**Claude** old request", "other topic", False),
        ("just chatting with a colleague", "chat", True),  # multi-party: a mention is needed
        ("// aside", "chat", False),
    ],
)
async def test_expiry_notice_only_for_this_bots_input(bridge, text, topic, guest):
    b, fake, backend = bridge
    fake.members += [
        {"user_id": 20, "full_name": "Owner", "is_bot": False},
        {"user_id": 4, "full_name": "Claude", "is_bot": True},
        {"user_id": 21, "full_name": "Guest", "is_bot": False},
    ]
    if guest:
        fake.subscribers[1].append(21)
    await b.refresh_users()
    old = await missed(b, fake, text, topic=topic, age=7201)
    old["flags"] = []
    await restart(b)
    assert not any(m["content"] == b.note(EXPIRED) for m in fake.messages.values())
    backend.start_turn.assert_not_awaited()
    assert not b.store.pending_deliveries("bot")


async def test_expired_new_topic_without_a_mention_is_both_bots_input(bridge):
    """A new topic's first message without a mention addresses all of the VM's bots in a
    single-human channel (#687, #713): expired, it gets the notice, as a mention would."""
    b, fake, backend = bridge
    fake.members += [
        {"user_id": 20, "full_name": "Owner", "is_bot": False},
        {"user_id": 4, "full_name": "Claude", "is_bot": True},
    ]
    await b.refresh_users()
    old = await missed(b, fake, "just a question", topic="chat", age=7201)
    old["flags"] = []
    await restart(b)
    assert [m["content"] for m in fake.messages.values()].count(b.note(EXPIRED)) == 1
    backend.start_turn.assert_not_awaited()
    assert not b.store.pending_deliveries("bot")


@pytest.mark.parametrize("dm", [False, True])
async def test_first_start_preserves_events_queued_before_newest_lookup(bridge, dm):
    b, fake, backend = bridge
    b.store.delete(f"catchup_cursor:{b.bot.name}")
    b.store.delete(f"catchup_pending:{b.bot.name}")
    b.store.delete(f"catchup_initial:{b.bot.name}")
    await missed(b, fake, "@**Test Bot** old", age=7201)
    await missed(b, fake, "@**Test Bot** historical recent")
    # Register after history, then deliver a message during first-start setup.
    await b.initialize()
    queued = await missed(b, fake, "!help" if dm else "@**Test Bot** live", dm=dm)
    fake.requests.clear()
    await restart(b, first_start=True)
    assert b.store.get(f"catchup_cursor:{b.bot.name}") is None
    pages = [p for method, path, p in fake.requests if method == "GET" and path == "messages"]
    assert len(pages) == 1 and pages[0]["anchor"] == "newest"
    assert not any(m["content"] == b.note(EXPIRED) for m in fake.messages.values())
    backend.start_turn.assert_not_awaited()
    events = await b.client.events(b.registration["queue_id"], b.registration["last_event_id"])
    assert [e["message"]["id"] for e in events if e["type"] == "message"] == [queued["id"]]
    for event in events:
        # missed() rewrites the fake bot POST into human input after event enqueue.
        if event["type"] == "message":
            event["message"] = fake.messages[event["message"]["id"]]
            event["flags"] = event["message"]["flags"]
        await b.handle(event)
    await settle(b)
    if dm:
        assert any(m["type"] == "private" and m["sender_id"] == 1 for m in fake.messages.values())
    else:
        backend.start_turn.assert_awaited_once()


@pytest.mark.parametrize("dm", [False, True])
@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("quiet_restart", [False, True])
async def test_first_run_boundary_survives_without_handling_messages(
    bridge, dm, history, quiet_restart
):
    b, fake, backend = bridge
    b.store.delete(f"catchup_cursor:{b.bot.name}")
    b.store.delete(f"catchup_pending:{b.bot.name}")
    b.store.delete(f"catchup_initial:{b.bot.name}")
    if history:
        await missed(b, fake, "@**Test Bot** historical")
    first = Catchup(b)
    assert first.first_start
    await first.initialize()
    await first.replay()
    assert b.store.get(first.key) is None
    assert b.store.get(first.initial_key) == first.anchor
    assert b.store.get(first.pending_key) is None
    assert not b.store.rows("inbound")
    if quiet_restart:
        # A second boot with nothing to replay must not forget the first boot.
        quiet = Catchup(b)
        assert not quiet.first_start
        await quiet.initialize()
        await quiet.replay()
    message = await missed(b, fake, "!help" if dm else "@**Test Bot** wake up", dm=dm)
    wake = Catchup(b)
    assert not wake.first_start
    await wake.initialize()
    await wake.replay()
    await settle(b)
    assert b.store.get(wake.key) >= message["id"]
    assert b.store.get(wake.pending_key) is None
    if dm:
        replies = [
            m for m in fake.messages.values() if m["type"] == "private" and m["sender_id"] == 1
        ]
        assert len(replies) == 1
    else:
        backend.start_turn.assert_awaited_once()
        assert backend.start_turn.call_args.args[1].text.endswith("wake up")


async def test_completed_live_login_is_not_replayed_after_newer_handled_input(bridge, caplog):
    b, fake, backend = bridge
    for key in ("catchup_cursor", "catchup_pending", "catchup_initial"):
        b.store.delete(f"{key}:{b.bot.name}")
    await missed(b, fake, "@**Test Bot** historical")
    first = Catchup(b)
    await first.initialize()
    codes = []

    async def login(account, send, receive):
        codes.append(await receive(1))
        return True

    backend.login.side_effect = login
    for text in ("!login", "DECOY-live-login-code"):
        message = await missed(b, fake, text, dm=True)
        await b.handle({"type": "message", "message": message})
    if b.login.task:
        await b.login.task
    assert codes == ["DECOY-live-login-code"]
    await user_message(b, fake, "@**Test Bot** later topic input")
    restarted = Catchup(b)
    await restarted.initialize()
    await restarted.replay()
    if b.login.task:
        await b.login.task
    backend.login.assert_awaited_once()
    assert codes == ["DECOY-live-login-code"]
    assert "DECOY-live-login-code" not in "\n".join(b.store.db.iterdump()) + caplog.text


async def test_turn_finished_before_the_backend_pump_runs_is_settled_once(bridge):
    """End-to-end scenario 2 (Oct 14): the startup catch-up starts a Codex turn, which ends
    before the runtime's pump runs. The pump first handles the backend's `BackendStatus(up)`:
    the reopened session's summary completes the turn (§7.8.1). Then the turn's own queued
    events arrive. Its late `TurnStarted` posted "⏳ Working · via terminal" and failed with
    IntegrityError: one turn shown as two."""
    from dataclasses import replace

    b, fake, backend = bridge
    await missed(b, fake, "@**Test Bot** CATCHUP")
    await restart(b)
    actor = b.actors["native"]
    assert actor.turn and actor.turn["native_turn_id"] == "turn1"
    summary = t.TurnSummary(turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="TOKEN")
    actor.put(replace(backend.create_session.return_value, last_turn=summary))
    actor.put(t.TurnStarted(session_id="native", turn_id="turn1", model="gpt-x", effort="high"))
    actor.put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="TOKEN"
        )
    )
    await settle(b)
    contents = [m["content"] for m in fake.messages.values() if m["sender_id"] == 1]
    assert contents.count("TOKEN") == 1
    assert not [c for c in contents if "via terminal" in c or c.startswith("⚠️")]
    assert len(b.store.rows("turns", session_id=actor.row["id"])) == 1
    assert actor.turn is None and actor.row["state"] == "idle"
