import json
from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_login import dm

from zab.core.commands import DM_HELP
from zab.core.store import Store
from zab.zulip.client import ZulipError

pytestmark = pytest.mark.integration


def owner(b):
    b.users[20] = dict(
        user_id=20, full_name="Participant Owner", is_bot=False, delivery_email="owner@example.org"
    )
    b.bot.allow_emails = ["OWNER@example.org"]
    b.users[22] = dict(user_id=22, full_name="Other Person", is_bot=False)


async def test_wrong_bot_reply_once_per_hour_and_across_store_reopen(bridge, monkeypatch, caplog):
    b, fake, backend = bridge
    owner(b)
    now = 100.0
    monkeypatch.setattr("zab.core.bridge.time.time", lambda: now)
    await dm(b, "SECRET-INBOUND !login", uid=22)
    assert len(fake.messages) == 1
    answer = next(iter(fake.messages.values()))["content"]
    assert "belongs to Participant Owner" in answer
    assert "Claude ·" in answer and "Codex ·" in answer and "Start here" in answer
    await dm(b, "again", uid=22)
    assert len(fake.messages) == 1
    original = b.store
    database = original.db.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path

    b.store = Store(Path(database).parent)
    try:
        await dm(b, "after restart", uid=22)
        assert len(fake.messages) == 1
        now += 3600
        await dm(b, "new window", uid=22)
        assert len(fake.messages) == 2
        stored = json.dumps({table: b.store.rows(table) for table in ("kv", "inbound", "held")})
        assert "SECRET-INBOUND" not in stored + caplog.text + answer
    finally:
        b.store.close()
        b.store = original
    backend.create_session.assert_not_awaited()
    backend.start_turn.assert_not_awaited()
    backend.login.assert_not_awaited()
    assert not b.actors


async def test_wrong_bot_reply_ignores_channels_bots_groups_and_unknown_users(bridge):
    b, fake, backend = bridge
    owner(b)
    b.users[30] = dict(user_id=30, full_name="Other Bot", is_bot=True)
    await dm(b, "!login", uid=30)
    await dm(b, "hi", uid=22, recipients=[22, 1, 20])
    before = len(fake.messages)
    await user_message(b, fake, "@**Test Bot** !new", sender=22)
    assert len(fake.messages) == before + 1  # inbound test helper only
    request = b.client.request

    async def unavailable(method, endpoint, **kwargs):
        if endpoint == "users/100006":
            raise ZulipError("BAD_REQUEST", 400)
        return await request(method, endpoint, **kwargs)

    b.client.request = AsyncMock(side_effect=unavailable)
    await dm(b, "system notification", uid=100006)
    assert len(fake.messages) == before + 1
    backend.login.assert_not_awaited()
    backend.create_session.assert_not_awaited()
    assert not [row for row in b.store.rows("kv") if row["key"].startswith("wrong_bot_reply:")]


async def test_owner_dm_unaffected_and_rate_limits_are_per_bot(bridge):
    b, fake, _ = bridge
    owner(b)
    await dm(b, "hi", uid=22)
    await dm(b, "!help")
    assert list(fake.messages.values())[-1]["content"] == DM_HELP
    b.bot.name = "second-bot"
    await dm(b, "hi", uid=22)
    assert sum("belongs to Participant Owner" in m["content"] for m in fake.messages.values()) == 2
