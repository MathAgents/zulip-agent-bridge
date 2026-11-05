from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_login import dm

from zab.core.commands import DM_HELP
from zab.zulip.client import ZulipError

pytestmark = pytest.mark.integration


def member(**changes):
    return (
        dict(
            user_id=20,
            full_name="Owner",
            is_bot=False,
            is_active=True,
            email="user20@zulip.test",
            delivery_email="late@example.org",
        )
        | changes
    )


async def test_startup_resolves_after_queue_registration(bridge):
    b, fake, _ = bridge
    b.bot.allow_user_ids = []
    b.bot.allow_emails = ["LATE@example.org"]
    fake.members.append(member())
    await b.start()
    b.listener.cancel()
    assert b.router.allowlist == {20}
    paths = [path for _, path, _ in fake.requests]
    assert paths.index("register") < paths.index("users")
    await user_message(b, fake, "@**Test Bot** hello")
    assert b.actors["native"].allowed_senders == {20}
    await dm(b, "!help")
    assert list(fake.messages.values())[-1]["content"] == DM_HELP


async def test_late_join_updates_topics_dms_and_revokes_pending_login(bridge):
    b, fake, backend = bridge
    b.bot.allow_user_ids = []
    b.bot.allow_emails = ["late@example.org"]
    await b.refresh_users()
    assert not b.router.allowlist  # does not fall back to bot owner 2
    await dm(b, "!login", uid=2)
    await dm(b, "!login")
    backend.login.assert_not_awaited()
    fake.members.append(member())
    await b.handle({"type": "realm_user", "op": "add", "person": member()})
    await user_message(b, fake, "@**Test Bot** hello")
    actor = b.actors["native"]
    assert actor.allowed_senders == {20}

    async def login(account, send, receive):
        await receive(60)

    backend.login.side_effect = login
    await dm(b, "!login")
    assert b.login.owner == 20
    fake.members[-1] = member(delivery_email=None)
    await b.handle({"type": "realm_user", "op": "update", "person": {"user_id": 20}})
    assert not b.router.allowlist and not actor.allowed_senders
    assert b.login.task is None
    backend.steer.reset_mock()
    await user_message(b, fake, "revoked input")
    backend.steer.assert_not_awaited()
    await dm(b, "!login")
    backend.login.assert_awaited_once()


@pytest.mark.parametrize("change", ["deactivate", "remove", "failure"])
async def test_refresh_removes_stale_email_access(bridge, change):
    b, fake, _ = bridge
    b.bot.allow_user_ids = [22]
    b.bot.allow_emails = ["late@example.org"]
    fake.members.append(member())
    await b.refresh_users()
    assert b.router.allowlist == {20, 22}
    if change == "failure":
        b.client.request = AsyncMock(side_effect=ZulipError("FAILED", 500))
        with pytest.raises(ZulipError):
            await b.refresh_users()
    else:
        if change == "remove":
            fake.members.pop()
        else:
            fake.members[-1]["is_active"] = False
        await b.handle({"type": "realm_user", "op": "remove", "person": {"user_id": 20}})
    assert b.router.allowlist == {22}
