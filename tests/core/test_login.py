import asyncio
from unittest.mock import Mock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_session import actor as actor
from test_session import status_replies

from zab.core import types as t

pytestmark = pytest.mark.integration


async def dm(b, text, uid=20, recipients=None, kind="private", mid=None):
    # Inbound DMs are delivered directly, not posted back by the fake bot.
    mid = mid or getattr(b, "test_dm_id", 1000) + 1
    b.test_dm_id = mid
    await b.handle(
        {
            "type": "message",
            "message": {
                "id": mid,
                "type": kind,
                "sender_id": uid,
                "content": text,
                "display_recipient": [{"id": r} for r in (recipients or [uid, 1])],
                "stream_id": 1,
                "subject": "topic",
            },
        }
    )
    return mid


@pytest.mark.parametrize("result", [True, False, RuntimeError("DECOY-CODE")])
async def test_owner_replies_only_reach_receive_and_refresh_status(bridge, caplog, result):
    b, fake, backend = bridge
    received = []
    waiting = asyncio.Event()

    async def login(account, send, receive):
        assert account == "work"
        await send("Open the CLI verification page.")
        assert await receive(0.001) is None
        waiting.set()
        received.extend([await receive(1), await receive(1)])
        if isinstance(result, Exception):
            raise result
        return result

    backend.login.side_effect = login
    await dm(b, "!login work")
    task = b.login.task
    await asyncio.wait_for(waiting.wait(), 1)
    route = b.router.route = Mock(wraps=b.router.route)
    for command in ("!login", "!status", "  !help"):
        await dm(b, command)
        assert list(fake.messages.values())[-1]["content"] == (
            "A login is in progress: paste the code here, or send !login cancel."
        )
    await dm(b, "a non-command reply")
    mid = await dm(b, "DECOY-CODE")
    await dm(b, "DECOY-CODE", mid=mid)  # duplicate delivery is not delivered twice
    await asyncio.wait_for(task, 1)
    assert received == ["a non-command reply", "DECOY-CODE"]
    route.assert_not_called()
    assert "DECOY-CODE" not in str(fake.messages) + caplog.text
    assert "DECOY-CODE" not in "\n".join(b.store.db.iterdump())
    assert not b.store.rows("inbound")
    assert list(fake.messages.values())[-1]["content"].startswith("✅" if result is True else "❌")
    backend.doctor.assert_awaited_once()
    assert b.login.owner is None and b.login.inbox.empty()
    if result is True:
        await user_message(b, fake, "@**Test Bot** hello after login")
        backend.start_turn.assert_awaited_once()


@pytest.mark.parametrize("ending", ["cancel", "timeout", "shutdown"])
async def test_login_cleanup_allows_another_attempt(bridge, monkeypatch, ending):
    b, fake, backend = bridge
    cleaned = asyncio.Event()

    async def login(account, send, receive):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    backend.login.side_effect = login
    monkeypatch.setattr("zab.core.login.TIMEOUT", 0.02 if ending == "timeout" else 600)
    await dm(b, "!login")
    task = b.login.task
    if ending == "cancel":
        await dm(b, "!login cancel")
    elif ending == "shutdown":
        await b.login.close()
    await asyncio.wait_for(task, 1)
    assert cleaned.is_set()
    assert b.login.task is None
    assert list(fake.messages.values())[-1]["content"].startswith("❌")
    backend.login.side_effect = None
    backend.login.return_value = True
    await dm(b, "!login")
    await asyncio.wait_for(b.login.task, 1)
    assert backend.login.await_count == 2


@pytest.mark.parametrize(
    "start, cancel",
    [
        ("!Login", "!Login cancel"),  # a phone keyboard's capital
        ("`!login`", "`!login cancel`"),  # Zulip's paste of the rendered "Start here"
        ("@**Test Bot** !login", "@**Test Bot** !login cancel"),
        ("@**Test Bot|1** `!Login`", "@**Test Bot|1**: `!LOGIN cancel`"),
        ("!login\n", "  !login cancel\n"),
        ("!login", "!login  cancel"),  # its spacing and case, too (review of #725)
        ("!login", "!login Cancel"),
        ("!login", "!login\tCANCEL"),
        # after a typed mention of the bot's kind (#713), then as typed (#709)
        ("@claude !Login", "@Claude `!login cancel`"),
        ("@claude: @**Test Bot** `!login`", "@claude !login  Cancel"),
    ],
)
async def test_dm_login_as_people_type_it(bridge, start, cancel):
    """#709: `!login` starts the sign-in however it is typed, and `!login cancel` ends it so."""
    b, fake, backend = bridge
    b.router.kind = "claude"  # the bot's backend kind, whose typed mention may lead a DM (#713)
    accounts = []

    async def login(account, send, receive):
        accounts.append(account)
        await asyncio.Event().wait()

    backend.login.side_effect = login
    await dm(b, start)
    task = b.login.task
    assert task is not None and b.login.owner == 20
    await asyncio.sleep(0)
    assert accounts == [None]  # no account: the mention and the backticks are not one
    await dm(b, cancel)
    await asyncio.wait_for(task, 1)
    assert list(fake.messages.values())[-1]["content"] == "❌ Login failed or cancelled."


@pytest.mark.parametrize("typed", ["!login cancel", "!login Cancel", "`!Login  cancel`"])
async def test_a_cancel_with_no_login_starts_none(bridge, typed):
    """`!login Cancel` is a cancel, never a sign-in for an account named `Cancel`."""
    b, fake, backend = bridge
    await dm(b, typed)
    assert b.login.task is None and not backend.login.await_count
    assert list(fake.messages.values())[-1]["content"] == (
        "❌ No login started. Use !login [account]."
    )


async def test_dm_commands_as_people_type_them(bridge):
    """#709: the bridge's other DM commands, too: `!Help`, "`!status`", after a mention."""
    b, fake, backend = bridge
    backend.doctor.return_value = []
    for typed, starts in (
        ("!Help", "In a direct message, only `!login`"),
        ("`!help`", "In a direct message, only `!login`"),
        ("@**Test Bot** !STATUS", "Bridge running"),
        ("@**Test Bot** hello", "DMs only take commands."),
    ):
        await dm(b, typed)
        assert list(fake.messages.values())[-1]["content"].startswith(starts), typed


async def test_authorization_dm_only_and_one_login_per_bot(bridge):
    b, fake, backend = bridge
    backend.login.side_effect = lambda *args: None
    b.router.allowlist = frozenset({20, 22, 30})
    b.users[30] = {"user_id": 30, "full_name": "Other Bot", "is_bot": True}
    await dm(b, "!login", uid=21)
    await dm(b, "!login", uid=30)
    await dm(b, "!login", recipients=[20, 1, 22])
    await dm(b, "!login", kind="stream")
    backend.login.assert_not_awaited()

    async def login(*args):
        await asyncio.Event().wait()

    backend.login.side_effect = login
    await dm(b, "!login")
    task = b.login.task
    await dm(b, "!login cancel", uid=22)
    assert list(fake.messages.values())[-1]["content"] == "❌ A login is already in progress."
    assert not task.cancelling()
    backend.login.assert_awaited_once()
    await dm(b, "!login cancel")
    await asyncio.wait_for(task, 1)


async def test_post_and_refresh_failures_release_login_without_logging(bridge, caplog):
    b, fake, backend = bridge
    backend.login.return_value = True
    backend.doctor.side_effect = RuntimeError("DECOY-PRIVATE")
    fake.failures.append((400, {"msg": "DECOY-PRIVATE"}))
    # Cache the sender so the injected failure applies to the completion post.
    b.users[20] = {"user_id": 20, "full_name": "Owner", "is_bot": False}
    await dm(b, "!login")
    await asyncio.wait_for(b.login.task, 1)
    assert b.login.owner is None
    backend.doctor.assert_awaited_once()
    assert "DECOY-PRIVATE" not in caplog.text


@pytest.mark.parametrize("existing", [False, True])
async def test_topic_login_redirects_to_dm_without_starting_login(bridge, existing):
    b, fake, backend = bridge
    if existing:
        await user_message(b, fake, "@**Test Bot** start")
    before = backend.start_turn.await_count
    await user_message(b, fake, "@**Test Bot** !login")
    assert list(fake.messages.values())[-1]["content"] == "Send !login to me in a direct message."
    backend.login.assert_not_awaited()
    assert backend.start_turn.await_count == before


async def test_dm_help_and_status_mention_login(bridge):
    b, fake, backend = bridge
    backend.doctor.return_value = [t.Check(name="codex:login", ok=False)]
    for command in ("!help", "!status"):
        await dm(b, command)
        assert "!login" in list(fake.messages.values())[-1]["content"]


@pytest.mark.parametrize("in_dm", [False, True], ids=["topic", "dm"])
@pytest.mark.parametrize(
    "checks,hint",
    [
        ([t.Check(name="codex:login", ok=False)], True),
        ([t.Check(name="codex:login", ok=True)], False),
        ([t.Check(name="codex:connection", ok=False)], False),
        ([t.Check(name="claude account a", ok=False, detail="not signed in: use !login")], True),
        ([t.Check(name="claude account a", ok=True)], False),
        ([t.Check(name="claude account a", ok=False, detail="status check failed")], False),
        (
            [
                t.Check(name="claude account a", ok=False, detail="not signed in: use !login"),
                t.Check(name="claude account b", ok=True),
            ],
            False,
        ),
        (
            [
                t.Check(name="claude account a", ok=False, detail="not signed in: use !login"),
                t.Check(name="claude account b", ok=False, detail="missing directory"),
            ],
            False,
        ),
        ([], False),
        (RuntimeError("PRIVATE_DIAGNOSTIC"), False),
    ],
)
async def test_status_hint_requires_explicit_signed_out_report(
    bridge, actor, in_dm, checks, hint, caplog
):
    b, dm_fake, dm_backend = bridge
    a, topic_fake, topic_backend = actor
    backend = dm_backend if in_dm else topic_backend
    if isinstance(checks, Exception):
        backend.doctor.side_effect = checks
    else:
        backend.doctor.return_value = checks
    if in_dm:
        await dm(b, "!status")
        reply = list(dm_fake.messages.values())[-1]["content"]
    else:
        [reply] = await status_replies(a, topic_fake)
    assert ("sign in via DM: !login" in reply) is hint
    assert "\n" not in reply
    assert "PRIVATE_DIAGNOSTIC" not in reply + caplog.text


async def test_slow_status_check_is_cancelled_without_losing_reply(bridge, monkeypatch):
    from zab.core import commands

    b, fake, backend = bridge
    timeout = asyncio.timeout
    monkeypatch.setattr(commands.asyncio, "timeout", lambda _: timeout(0.01))
    cancelled = asyncio.Event()

    async def doctor():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    backend.doctor.side_effect = doctor
    await dm(b, "!status")
    assert cancelled.is_set()
    assert (
        list(fake.messages.values())[-1]["content"]
        == "Bridge running · git: Test Bot <test-bot@example.zulipchat.com>"
        " · without a mention: `all`"  # the VM's rule (#878)
    )


# ------------------------------------------- a backend that is not connected yet (after a deploy)


def replies(fake) -> list[str]:
    return [m["content"] for m in fake.messages.values()]


async def until(condition) -> None:
    for _ in range(400):  # about 2 s
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def test_login_waits_for_a_codex_daemon_that_is_still_connecting(bridge, caplog, monkeypatch):
    """Seen live on the test VM: !login right after push_config restarted the bridge, while the
    Codex backend was still connecting, got "Login failed or cancelled". Now the login waits
    for the connection, saying so, and then proceeds."""
    from unittest.mock import AsyncMock

    from zab.backends.codex import CodexBackend
    from zab.config import BackendConfig

    b, fake, _ = bridge
    monkeypatch.setattr("zab.core.login.READY_POLL", 0.01)
    codex = CodexBackend("codex", BackendConfig(kind="codex", codex_home="/isolated"))
    codex.doctor = AsyncMock(return_value=[])  # the status refresh; never runs a `codex`
    b.backend = codex  # no rpc yet: its connection is pending
    await dm(b, "!login")
    task = b.login.task
    await until(lambda: any("isn't connected yet" in r for r in replies(fake)))
    assert replies(fake)[-1] == (
        "⏳ Codex isn't connected yet; it's probably still starting. "
        "The sign-in begins as soon as it is, within a minute."
    )
    await dm(b, "typed while waiting")  # can't be a code for a sign-in that hasn't begun
    rpc = AsyncMock()
    rpc.up = True
    started = {"loginId": "L1", "verificationUrl": "https://auth.example/device", "userCode": "C"}
    rpc.call.side_effect = lambda method, params: (
        started if method == "account/login/start" else {"account": {"type": "chatgpt"}}
    )
    codex.rpc = rpc  # connected
    await until(lambda: codex.login_events is not None and "auth.example" in replies(fake)[-1])
    assert b.login.inbox.empty()
    codex.login_events.put_nowait({"loginId": "L1", "success": True})
    await asyncio.wait_for(task, 1)
    assert replies(fake)[-1] == "✅ connected"
    assert not any("Login failed" in r for r in replies(fake))
    assert "Codex daemon unavailable" not in caplog.text + str(fake.messages)


async def test_a_backend_that_never_connects_gets_a_clear_reply(bridge, caplog, monkeypatch):
    b, fake, backend = bridge
    monkeypatch.setattr("zab.core.login.READY_WAIT", 0.05)
    monkeypatch.setattr("zab.core.login.READY_POLL", 0.01)
    backend.login.side_effect = t.BackendUnavailable("DECOY-PRIVATE")
    await dm(b, "!login")
    await asyncio.wait_for(b.login.task, 1)
    assert backend.login.await_count > 1
    assert sum("isn't connected yet" in r for r in replies(fake)) == 1  # once, not per retry
    assert replies(fake)[-1] == (
        "⏳ Codex still isn't connected, so the sign-in didn't start. "
        "Send !login again in a minute."
    )
    assert not any("Login failed" in r for r in replies(fake))
    assert "DECOY-PRIVATE" not in caplog.text + str(fake.messages)
    assert b.login.task is None
    backend.doctor.assert_awaited_once()


async def test_a_backend_returning_none_after_a_code_is_not_retried(bridge, monkeypatch):
    """Review of #288: None means "not connected" inside the flow, so a backend's own falsy
    result (the contract says bool) is a failure, never a retry that relays the code again."""
    b, fake, backend = bridge
    monkeypatch.setattr("zab.core.login.READY_POLL", 0.01)

    async def login(account, send, receive):
        await send("DEVICE CODE")

    backend.login.side_effect = login
    await dm(b, "!login")
    await asyncio.wait_for(b.login.task, 1)
    backend.login.assert_awaited_once()
    assert replies(fake)[-2:] == ["DEVICE CODE", "❌ Login failed or cancelled."]


async def test_a_backend_lost_during_the_sign_in_is_a_failure_not_a_retry(bridge, monkeypatch):
    b, fake, backend = bridge
    monkeypatch.setattr("zab.core.login.READY_POLL", 0.01)

    async def login(account, send, receive):
        await send("Open the verification page.")
        raise t.BackendUnavailable("DECOY-PRIVATE")

    backend.login.side_effect = login
    await dm(b, "!login")
    await asyncio.wait_for(b.login.task, 1)
    backend.login.assert_awaited_once()
    assert replies(fake)[-1] == "❌ Login failed or cancelled."
