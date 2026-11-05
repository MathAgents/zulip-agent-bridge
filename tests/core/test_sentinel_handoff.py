"""The bridge leaves DM `!vm` and `!restart` to a listening `zab sentinel` (#204; SPEC §4.6).

It stays silent on them in a one-to-one DM that the sentinel received (its mark: this boot, a
live process, a fresh poll, a queue registered before the message). Otherwise it answers `!vm`
itself and says that nothing was restarted for `!restart`: a double reply over a lost one.
"""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_vm import stat

from zab.core import types as t
from zab.core import vm
from zab.core.commands import UNKNOWN
from zab.tools import sentinel

pytestmark = pytest.mark.integration
OWNER, BOT = 20, 1
NOW = time.time()


def mark(b, *, registered=NOW - 600, boot=None, pid=None, written=None, bot_id=None) -> None:
    """The sentinel's mark for the fixture's bot, as `Sentinel.mark` writes it."""
    path = sentinel.mark_path(b.store.state_dir, b.bot.name)
    body = {
        "pid": os.getpid() if pid is None else pid,
        "boot": sentinel.boot_id() if boot is None else boot,
        "bot_id": BOT if bot_id is None else bot_id,
        "registered": registered,
        "allowed": {str(OWNER): registered},
    }
    path.write_text(json.dumps(body))
    if written is not None:
        os.utime(path, (written, written))


async def dm(b, text, *, sent=NOW, recipients=(OWNER, BOT), mid=None):
    mid = mid or getattr(b, "test_dm_id", 1000) + 1
    b.test_dm_id = mid
    message = {
        "id": mid,
        "type": "private",
        "sender_id": OWNER,
        "content": text,
        "timestamp": int(sent),
        "display_recipient": [{"id": r} for r in recipients],
    }
    await b.handle({"type": "message", "message": message})
    return mid


def replies(fake) -> list[str]:
    return [m["content"] for m in fake.messages.values()]


@pytest.fixture
def report(monkeypatch, bridge):
    _, _, backend = bridge
    backend.kind = "codex"
    backend.doctor.return_value = [t.Check(name="codex:login", ok=True)]
    monkeypatch.setattr(vm, "collect", AsyncMock(return_value=stat()))


@pytest.fixture
def units(tmp_path, monkeypatch):
    """The user unit directory `restart_unanswered` looks in."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    directory = tmp_path / "xdg" / "systemd" / "user"
    directory.mkdir(parents=True)
    return directory


async def test_silent_on_the_sentinels_commands_while_it_listens(bridge, report):
    b, fake, _ = bridge
    mark(b)
    for text in ("!vm", "!restart", " !restart \n codex", "!restart  codex"):
        await dm(b, text)
    assert replies(fake) == []  # no report, no "Unknown command": the sentinel answers
    await dm(b, "!status")
    await dm(b, "!restart now")  # not a sentinel command: the bridge's as before
    assert replies(fake)[0].startswith("Bridge running") and replies(fake)[1] == UNKNOWN
    assert len(b.store.rows("inbound", bot="bot")) == 6  # claimed: never replayed


async def test_typed_forms_are_left_to_the_sentinel_too(bridge, report):
    """#709: the bridge reads `!VM`, "`!restart`" and a leading mention of its bot as the
    sentinel does, so it leaves them to it, and answers neither twice nor "Unknown command"."""
    b, fake, _ = bridge
    b.router.kind = "claude"  # a typed mention of the bot's kind first (#713), then as typed
    mark(b)
    for text in (
        "!VM", "`!restart`", "@**Test Bot** !Restart codex", "@**Test Bot** `!vm`",
        "@claude !VM", "@Claude `!restart`", "!Restart Codex",
    ):  # fmt: skip
        await dm(b, text)
    assert replies(fake) == []


@pytest.mark.parametrize(
    "why",
    [
        "no mark",
        "other boot",
        "dead process",
        "stale",
        "sent before it registered",
        "garbage",
        "another bot",  # the zuliprc changed, the sentinel wasn't restarted (review on #573)
    ],
)
async def test_answers_vm_itself_when_the_sentinel_did_not_receive_it(bridge, report, why):
    b, fake, _ = bridge
    if why == "other boot":
        mark(b, boot="another-boot")
    elif why == "dead process":
        mark(b, pid=2**22 + 1)  # above pid_max's default: no such process
    elif why == "stale":
        mark(b, written=NOW - sentinel.LISTENING_FRESH_S - 5)
    elif why == "sent before it registered":  # a DM replayed after the VM woke (catch-up)
        mark(b, registered=NOW - sentinel.CLOCK_MARGIN_S + 1)
    elif why == "another bot":
        mark(b, bot_id=BOT + 1000)
    elif why == "garbage":
        sentinel.mark_path(b.store.state_dir, "bot").write_text("[1, 2]")
    await dm(b, "!vm")
    (reply,) = replies(fake)
    assert reply.startswith("**ws-alice**") and "Codex signed in" in reply


@pytest.mark.parametrize("installed", [False, True])
async def test_restart_without_a_listening_sentinel_says_nothing_was_restarted(
    bridge, units, installed
):
    b, fake, _ = bridge
    if installed:
        (units / "zab-sentinel.service").write_text("# Managed by zab service install\n")
        mark(b, registered=NOW + 30)  # registered after the message: it never saw it
    await dm(b, "!restart codex")
    await dm(b, "!Restart Codex")  # its word in any case too: never "Unknown command" (#725)
    for reply in replies(fake):
        assert reply.startswith("Nothing was restarted: ")
        assert ("didn't receive this message" in reply) is installed
        assert ("isn't available on this VM yet" in reply) is not installed
        assert reply.endswith(
            ("send `!restart` again if you still need one.", "(it sent this reply).")
        )
    assert len(replies(fake)) == 2


async def test_group_dms_are_the_bridges_as_before(bridge, report):
    """The sentinel answers one-to-one DMs only: in a group, others would see its reply."""
    b, fake, _ = bridge
    mark(b)
    await dm(b, "!restart", recipients=(OWNER, BOT, 30))
    await dm(b, "!vm", recipients=(OWNER, BOT, 30))
    assert replies(fake)[0] == UNKNOWN and replies(fake)[1].startswith("**ws-alice**")


async def test_a_login_in_progress_leaves_them_to_the_sentinel_too(bridge):
    b, fake, backend = bridge
    waiting = asyncio.Event()

    async def login(account, send, receive):
        waiting.set()
        await receive(5)
        return False

    backend.login.side_effect = login
    await dm(b, "!login")
    await asyncio.wait_for(waiting.wait(), 1)
    mark(b)
    await dm(b, "!vm")
    await dm(b, "!restart")
    assert replies(fake) == []  # neither the report nor "A login is in progress"
    sentinel.mark_path(b.store.state_dir, "bot").unlink()
    await dm(b, "!vm")
    assert replies(fake) == ["A login is in progress: paste the code here, or send !login cancel."]
    b.login.task.cancel()
    await asyncio.gather(b.login.task, return_exceptions=True)


@pytest.mark.parametrize("listening", [True, False])
async def test_catch_up_after_the_bridge_was_down_adds_nothing_the_sentinel_answered(
    bridge, report, listening
):
    """The bridge down for hours while the sentinel answered: at its start, the catch-up reads
    those DMs, recent and older than its two hours, and posts neither a report nor the notice
    for old messages. Without a listening sentinel it answers as before."""
    from test_catchup import missed, restart

    from zab.core.catchup import EXPIRED

    b, fake, _ = bridge
    fake.members.append({"user_id": OWNER, "is_bot": False, "full_name": "Owner"})
    await b.refresh_users()
    if listening:
        mark(b, registered=NOW - 3 * 3600)
    for text, age in (("!vm", 7300), ("!restart", 7250), ("!vm", 60)):
        await missed(b, fake, text, dm=True, age=age)
    before = len(fake.messages)
    await restart(b)
    posted = [m["content"] for m in list(fake.messages.values())[before:]]
    if listening:
        assert posted == []
    else:
        assert posted == [b.note(EXPIRED), posted[1]] and posted[1].startswith("**ws-alice**")
