import asyncio

import pytest
from test_session import actor as actor
from test_session import send

from zab.core import types as t
from zab.core.router import Router
from zab.core.session import Delivery

pytestmark = pytest.mark.integration


async def peer_message(a, fake, text, sender=30):
    mid = await a.poster.client.send(1, "topic", text)
    fake.messages[mid].update(
        sender_id=sender, sender_full_name="Peer" if sender == 30 else "Owner"
    )
    return mid


def enable_peer(a, fake):
    a.allowed_senders = frozenset({20})
    fake.members.append(dict(user_id=30, full_name="Peer", is_bot=True))


async def test_peer_context_contains_only_missed_prior_topic_messages(actor):
    a, fake, backend = actor
    enable_peer(a, fake)
    await send(a, fake, "first")
    answer = await peer_message(a, fake, "PEER ANSWER")
    for body in [
        "**zab session** CARD",
        "✅ **Done** · 1m 02s · 3 actions",
        # a running status, since #754 (review of #755)
        "⏳ **Working** · started <time:2026-11-02T10:00:00+00:00> · 4 actions · model default"
        "\n- ⏳ ` RUNNING-STEP `\n> RUNNING-NOTE",
        "@**Owner|20** approval needed: true",
        "Approval: accepted",
        "⚠️ **Bridge** (not Claude): Claude Code … BLOCKED",  # the bridge's own (#677)
        "🔧 **Bridge** (not Codex): INFORMATION",
    ]:
        await peer_message(a, fake, body)
    missed = await peer_message(a, fake, "PEER-ONLY QUESTION", sender=20)
    a.store.set(f"message_audience:b:{missed}", dict(addressed=False, peers=["Peer"]))
    await peer_message(a, fake, "UNAUTHORIZED", sender=21)
    await peer_message(a, fake, "OWN OUTPUT", sender=1)
    await peer_message(a, fake, "OTHER BOT", sender=40)
    trigger = await peer_message(a, fake, "compare", sender=20)
    a.store.set(f"addressed_peers:b:{trigger}", ["Peer"])
    await peer_message(a, fake, "SAME-REQUEST FUTURE ANSWER")
    a.put(Delivery(trigger, 20, "Owner", "compare"))
    await a.inbox.join()
    await a.event(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="done"
        )
    )
    await asyncio.sleep(0)
    await a.inbox.join()
    text = backend.start_turn.call_args.args[1].text
    assert "PEER ANSWER" in text and "PEER-ONLY QUESTION" in text
    assert f"/near/{answer}" in text and f"/near/{missed}" in text
    assert "Untrusted topic context" in text and "not instructions" in text
    assert text.endswith("compare")
    for excluded in [
        "CARD",
        "3 actions",
        "4 actions",
        "RUNNING-STEP",
        "RUNNING-NOTE",
        "approval needed",
        "Approval: accepted",
        "BLOCKED",
        "INFORMATION",
        "UNAUTHORIZED",
        "OWN OUTPUT",
        "OTHER BOT",
        "FUTURE ANSWER",
    ]:
        assert excluded not in text
    assert a.store.get("peer_cursor:s") == trigger
    assert a.store.get(f"addressed_peers:b:{trigger}") is None
    # The next input reads the durable cursor; no old context is replayed.
    later = await peer_message(a, fake, "compare again", sender=20)
    a.store.set(f"addressed_peers:b:{later}", ["Peer"])
    prepared = await a.prepare_input("compare again", "Owner", [later])
    assert "PEER ANSWER" not in prepared.text
    assert "SAME-REQUEST FUTURE ANSWER" in prepared.text


async def test_peer_context_is_bounded_and_links_truncated_originals(actor):
    from zab.core.peer_context import FOOTER, MAX_CHARS, MAX_MESSAGES

    a, fake, _ = actor
    enable_peer(a, fake)
    for i in range(MAX_MESSAGES + 5):
        last = await peer_message(a, fake, f"answer {i}: " + "\n\x01" * 4000)
    trigger = await peer_message(a, fake, "compare", sender=20)
    a.store.set(f"addressed_peers:b:{trigger}", ["Peer"])
    prepared = await a.prepare_input("compare", "Owner", [trigger])
    assert len(prepared.text.partition(FOOTER)[0] + FOOTER) <= MAX_CHARS
    assert prepared.text.endswith("compare")
    assert f"/near/{last}" in prepared.text and '"truncated": true' in prepared.text
    assert "answer 0:" not in prepared.text
    request = [
        params for method, path, params in fake.requests if path == "messages" and method == "GET"
    ][-1]
    assert request["include_anchor"] == "false" and request["num_before"] == str(MAX_MESSAGES)
    assert request["num_after"] == "0"


async def test_peer_cursor_waits_for_backend_acceptance(actor):
    a, fake, backend = actor
    enable_peer(a, fake)
    await peer_message(a, fake, "keep this peer answer")
    trigger = await peer_message(a, fake, "compare", sender=20)
    a.store.set(f"addressed_peers:b:{trigger}", ["Peer"])
    backend.start_turn.side_effect = t.BackendUnavailable("offline")
    a.put(Delivery(trigger, 20, "Owner", "compare"))
    await a.inbox.join()
    await asyncio.sleep(0)
    await a.inbox.join()
    assert a.store.get("peer_cursor:s", 0) == 0
    assert a.store.held("s")
    assert a.store.get(f"addressed_peers:b:{trigger}") == ["Peer"]
    backend.start_turn.side_effect = None
    backend.start_turn.return_value = "turn1"
    a.ready = True
    a.state(state="idle")
    a.schedule()
    await asyncio.sleep(0)
    await a.inbox.join()
    assert "keep this peer answer" in backend.start_turn.call_args.args[1].text
    assert a.store.get("peer_cursor:s") == trigger


async def test_peer_context_failure_preserves_prompt_and_history_cursor(actor):
    a, fake, backend = actor
    enable_peer(a, fake)
    trigger = await peer_message(a, fake, "compare", sender=20)
    a.store.set(f"addressed_peers:b:{trigger}", ["Peer"])
    fake.failures.append((503, {"code": "TEMPORARY"}))
    a.put(Delivery(trigger, 20, "Owner", "compare"))
    await a.inbox.join()
    await asyncio.sleep(0)
    await a.inbox.join()
    text = backend.start_turn.call_args.args[1].text
    assert "Peer context unavailable" in text and text.endswith("compare")
    assert a.store.get("peer_cursor:s", 0) == 0
    assert a.row["state"] == "running"


async def test_router_to_session_peer_context_without_seeded_audience(actor):
    a, fake, backend = actor
    enable_peer(a, fake)
    router = Router(a.store, "b", 1, "Test Bot", frozenset({20}))
    await send(a, fake, "first")
    mid = await peer_message(a, fake, "@**Peer** PRIVATE QUESTION", sender=20)
    result = router.route(
        fake.messages[mid], flags=[], sender_is_bot=False, private=True, other_bot_names={"Peer"}
    )
    assert result.action == "ignore"
    answer = await peer_message(a, fake, "PEER RESULT")
    assert (
        router.route(fake.messages[answer], flags=[], sender_is_bot=True, private=True).action
        == "ignore"
    )
    assert backend.start_turn.await_count == 1
    trigger = await peer_message(a, fake, "@**Test Bot** @**Peer** compare", sender=20)
    result = router.route(
        fake.messages[trigger],
        flags=["mentioned"],
        sender_is_bot=False,
        private=True,
        other_bot_names={"Peer"},
    )
    a.put(Delivery(trigger, 20, "Owner", result.text))
    await a.inbox.join()
    await a.event(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="done"
        )
    )
    await asyncio.sleep(0)
    await a.inbox.join()
    text = backend.start_turn.call_args.args[1].text
    assert "PRIVATE QUESTION" in text and "PEER RESULT" in text
    assert "Addressed to you and to Peer" in text and text.endswith("compare")
