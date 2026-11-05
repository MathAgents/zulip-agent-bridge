import json
from unittest.mock import AsyncMock, Mock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_session import actor as actor
from test_session import send

from zab.core import types as t

pytestmark = pytest.mark.integration


async def test_anchor_is_never_edited_or_replaced_across_lifecycle(actor):
    a, fake, backend = actor
    mid = a.row["card_message_id"]
    original = fake.messages[mid]["content"]
    a.poster.edit = AsyncMock(wraps=a.poster.edit)
    fake.refused_edits.add(mid)
    await send(a, fake, "start")
    backend.update_settings.side_effect = lambda sid, s: s
    await a.command("!model next")
    await a.command("!status")
    await a.complete(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    await a.command("!detach")
    assert all(c.args[0] != mid for c in a.poster.edit.call_args_list)
    assert a.row["card_message_id"] == mid and fake.messages[mid]["content"] == original


@pytest.mark.parametrize("tokens,display", [(123, "123"), (1_200_000, "1.2M")])
async def test_friendly_card_and_current_details(bridge, tokens, display):
    b, fake, backend = bridge
    backend.effective_settings = Mock(
        return_value=t.SessionSettings(model="resolved", effort="high")
    )
    await user_message(b, fake, "@**Test Bot** start")
    a = b.actors["native"]
    card = fake.messages[a.row["card_message_id"]]["content"]
    assert card.startswith("**zab session**") and "\n" not in card
    assert "!session" in card and a.row["workspace"] not in card
    await a.event(t.UsageUpdated(session_id="native", usage=t.TokenUsage(total_tokens=tokens)))
    await user_message(b, fake, "!session")
    reply = list(fake.messages.values())[-1]["content"]
    assert "resolved (default)" in reply and "high (default)" in reply
    assert f"{display} tokens" in reply and "1 turns" in reply
    assert "approval:" in reply and "sandbox:" in reply
    assert "For SSH users" in reply.splitlines()[-1]
    assert reply.startswith("**Session details**")
    assert json.loads(a.row["settings_json"]).get("model") is None
    await user_message(b, fake, "!help")
    assert "!session" in list(fake.messages.values())[-1]["content"]


async def test_claude_tokens_survive_reset_cumulative_counter_and_completion_replay(actor):
    a, fake, backend = actor
    backend.kind = "claude"
    for total, turn_id in ((1000, "turn1"), (100, "turn2")):
        await send(a, fake, "start")
        event = t.TurnCompleted(
            session_id="native",
            turn_id=turn_id,
            status=t.TurnStatus.COMPLETED,
            usage=t.TokenUsage(total_tokens=total),
        )
        await a.complete(event)
        await a.complete(event)  # Replayed terminal event must not add the turn twice.
        await a.event(t.UsageUpdated(session_id="native", usage=t.TokenUsage(total_tokens=total)))
    await a.command("!session")
    assert "1.1k tokens" in list(fake.messages.values())[-1]["content"]
    assert a.store.get("turn_tokens:" + str(a.store.rows("turns")[0]["id"])) == 1000
