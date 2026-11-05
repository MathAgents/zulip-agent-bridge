from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.commands.run import Runtime
from zab.core import detached
from zab.core import types as t

pytestmark = pytest.mark.integration


async def detach(bridge, fake, *, forget=True):
    await user_message(bridge, fake, "@**Test Bot** begin")
    actor = bridge.actors["native"]
    turn = dict(actor.turn)
    await user_message(bridge, fake, "!detach")
    if forget:
        await bridge.actors.pop("native").close()
    return actor, turn


@pytest.mark.parametrize("state", ["completed", "interrupted", "running", "unavailable"])
async def test_recovery_settles_detached_rows_without_posting(bridge, state):
    b, fake, backend = bridge
    actor, turn = await detach(b, fake)
    info = replace(backend.create_session.return_value)
    if state == "running":
        info.active_turn_id = "turn1"
    elif state == "completed":
        info.last_turn = t.TurnSummary(turn_id="turn1", status=t.TurnStatus.COMPLETED)
    if state == "unavailable":
        backend.open_session.side_effect = t.BackendUnavailable("offline")
    else:
        backend.open_session.return_value = info
    messages = dict(fake.messages)
    runtime = Runtime.__new__(Runtime)
    await runtime.finish_recovery(b, [])
    row = b.store.rows("turns", id=turn["id"])[0]
    assert row["state"] == ("running" if state == "unavailable" else state)
    assert (row["ended_at"] is None) == (state in ("running", "unavailable"))
    assert b.store.rows("sessions", id=actor.row["id"])[0]["state"] == "detached"
    assert fake.messages == messages
    backend.interrupt.assert_not_awaited()
    if state == "running":

        async def events():
            yield t.TurnCompleted(
                session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED
            )

        backend.events = events
        runtime.bridges = [b]
        await runtime.pump("codex", backend)
        assert b.store.rows("turns", id=turn["id"])[0]["state"] == "completed"
        assert fake.messages == messages


async def test_attach_atomically_retires_the_superseded_turn(bridge):
    b, fake, backend = bridge
    _, turn = await detach(b, fake, forget=False)
    backend.open_session.return_value = replace(
        backend.create_session.return_value, active_turn_id="turn1"
    )
    await user_message(b, fake, "@**Test Bot** !attach native", topic="new")
    old = b.store.rows("turns", id=turn["id"])[0]
    current = b.actors["native"].turn
    assert old["state"] == "superseded" and old["ended_at"] is not None
    assert current["state"] == "running" and current["trigger_msg_ids"] == old["trigger_msg_ids"]
    assert len(b.store.rows("turns", state="running")) == 1
    # Repair duplicate running rows left by releases before this fix too.
    b.store.update("turns", {"id": old["id"]}, state="running", ended_at=None)
    backend.open_session.reset_mock()
    backend.open_session.side_effect = t.BackendUnavailable("offline")
    await detached.recover(b)
    backend.open_session.assert_not_awaited()
    assert b.store.rows("turns", id=old["id"])[0]["state"] == "superseded"
    assert len(b.store.rows("turns", state="running")) == 1


async def test_detached_actor_recovery_does_not_adopt_a_later_terminal_turn(bridge):
    b, fake, backend = bridge
    actor, turn = await detach(b, fake, forget=False)
    backend.open_session.return_value = replace(
        backend.create_session.return_value, active_turn_id="external"
    )
    messages = dict(fake.messages)
    await detached.recover(b)
    assert b.store.rows("turns", id=turn["id"])[0]["state"] == "interrupted"
    assert actor.turn is None and not actor.slot_owned
    assert fake.messages == messages


@pytest.mark.parametrize("live_child", [False, True])
async def test_detached_claude_recovery_preserves_child_and_reaps_verified_orphan(
    bridge, monkeypatch, live_child
):
    b, fake, backend = bridge
    _, turn = await detach(b, fake)
    b.config.backends["codex"].kind = "claude"
    b.config.backends["codex"].binary = "claude"
    b.store.update("turns", {"id": turn["id"]}, pgid=1234)
    backend.open_session.return_value = backend.create_session.return_value
    monkeypatch.setattr(detached, "is_bridge_child", lambda _: live_child)
    reap = AsyncMock()
    monkeypatch.setattr(detached, "reap_claude_turn", reap)
    await detached.recover(b)
    assert b.store.rows("turns", id=turn["id"])[0]["state"] == (
        "running" if live_child else "interrupted"
    )
    assert reap.await_count == (0 if live_child else 1)


async def test_recovery_uses_the_current_peer_owner_settings(bridge):
    import json
    from types import SimpleNamespace

    b, fake, backend = bridge
    actor, turn = await detach(b, fake, forget=False)
    # Another bot owns the resumed native session with newer settings.
    # The old detached row still records the original settings.
    b.actors.pop("native")
    actor.row = actor.row | {"settings_json": json.dumps({"model": "current-owner-model"})}
    b.peers.append(SimpleNamespace(bot=b.bot, actors={"native": actor}))
    backend.open_session.return_value = replace(
        backend.create_session.return_value,
        last_turn=t.TurnSummary(turn_id="turn1", status=t.TurnStatus.COMPLETED),
    )
    try:
        await detached.recover(b)
        assert backend.open_session.call_args.args[1].settings.model == "current-owner-model"
        assert b.store.rows("turns", id=turn["id"])[0]["state"] == "completed"
    finally:
        await actor.close()
