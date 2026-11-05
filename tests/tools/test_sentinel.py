"""`zab sentinel` (issue #204): DM-only `!vm` and `!restart`, with fakes for Zulip and systemd."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from zab import __version__ as VERSION
from zab import cli
from zab.config import BotConfig, BridgeConfig, Config, ProjectConfig, RenderConfig
from zab.tools import sentinel as sn
from zab.tools import vmstat
from zab.zulip.client import ZulipError

BOT, OWNER, OTHER = 100, 7, 8
NOW = 1_800_000_000.0


class FakeClient:
    """`request` for the few endpoints the sentinel uses; `events` is a list of batches."""

    def __init__(self, events=None, *, users=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.batches = list(events or [])
        self.users = users or []
        self.post_errors: list[Exception] = []  # what the next posts raise, in turn

    async def request(self, method, endpoint, **params):
        if endpoint == "messages" and self.post_errors:
            raise self.post_errors.pop(0)  # `calls` and `sent` hold the posts Zulip took
        self.calls.append((method, endpoint, params))
        if endpoint == "users/me":
            return {"user_id": BOT, "is_bot": True, "bot_owner_id": OWNER, "full_name": "Claude"}
        if endpoint == "users":
            return {"members": self.users}
        if endpoint == "register":
            return {"queue_id": "q1", "last_event_id": -1}
        if endpoint == "events":
            if not self.batches:
                raise asyncio.CancelledError  # ends serve() in the test
            batch = self.batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return {"events": batch}
        if endpoint == "messages":
            return {"id": 1}
        raise AssertionError(endpoint)

    def sent(self) -> list[str]:
        return [p["content"] for m, e, p in self.calls if e == "messages"]

    def count(self, endpoint: str) -> int:
        return [e for _, e, _ in self.calls].count(endpoint)


class Systemd:
    """`systemctl --user` as a fake runner: records argv; answers `is-active`, `show`'s
    MainPID (`pids` in turn, the last one repeating) and `restart`."""

    def __init__(self, *, active="active", pids=(500,), restart_code=0, on_restart=None):
        self.calls: list[list[str]] = []
        self.active = active
        self.pids = list(pids)
        self.restart_code = restart_code
        self.on_restart = on_restart

    async def __call__(self, argv, seconds):
        self.calls.append(list(argv))
        assert argv[:2] == ["systemctl", "--user"]
        verb = argv[2]
        if verb == "is-active":
            return (0 if self.active == "active" else 3), self.active
        if verb == "show":
            assert argv[3:] == ["--property=MainPID", "--value", "zab.service"]
            pid = self.pids.pop(0) if len(self.pids) > 1 else self.pids[0]
            return (0, str(pid)) if pid is not None else (1, "")
        if verb == "reset-failed":
            return 0, ""
        if verb == "restart":
            if self.on_restart:
                self.on_restart()
            return self.restart_code, ""
        raise AssertionError(argv)

    def actions(self) -> list[list[str]]:
        """The calls that change something: every argv but the reads."""
        return [c for c in self.calls if c[2] not in ("is-active", "show")]


class Clock:
    def __init__(self):
        self.t = NOW
        self.slept: list[float] = []
        self.on_sleep = None

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep()


def stat() -> vmstat.VmStat:
    return vmstat.VmStat(
        host="ws-alice",
        cpus=8,
        load=(0.4, 0.6, 0.7),
        mem_total=30 * 1024**3,
        mem_available=27 * 1024**3,
        uptime=7200.0,
        disks=(vmstat.Disk("/", 100 * 1024**3, 41 * 1024**3, 54 * 1024**3),),
        heaviest=(vmstat.Proc(1, "sage", 1024**3, 50.0, 60.0),),
        unavailable=(),
        cpu=0.05,
    )


@pytest.fixture
def make(tmp_path):
    def build(*, events=None, systemd=None, units=("codex-app-server.service",), bot=None):
        config = Config(
            path=tmp_path / "config.toml",
            bridge=BridgeConfig(state_dir=str(tmp_path / "state"), default_project="/work"),
            bots=[bot or BotConfig("claude", "unused", "claude")],
            backends={},
            projects=[ProjectConfig(channel="a", path="/work"), ProjectConfig("b", "/data/b")],
            render=RenderConfig(),
        )
        (tmp_path / "state").mkdir(exist_ok=True)
        clock = Clock()
        client = FakeClient(events)
        collected: list[list[str]] = []

        def collect(paths):
            collected.append(list(paths))
            return stat()

        s = sn.Sentinel(
            config,
            config.bots[0],
            client,
            sn.Shared(clock=clock.now),
            run=systemd or Systemd(),
            collect=collect,
            now=clock.now,
            sleep=clock.sleep,
            units=lambda: list(units),
            hold=str(tmp_path / "hold"),  # none: the tests that want one write it
        )
        s.clock, s.collected = clock, collected
        s.registered = NOW - 3600  # as `serve` registers its queue
        s.heartbeat_file = tmp_path / "state" / "heartbeat-claude"
        return s

    return build


def dm(text, sender=OWNER, recipients=(OWNER, BOT), mid=1, sent=NOW + 60):
    return {
        "id": mid,
        "type": "private",
        "sender_id": sender,
        "content": text,
        "timestamp": int(sent),
        "display_recipient": [{"id": r} for r in recipients],
    }


def touch(path: Path, mtime: float) -> None:
    path.touch()
    os.utime(path, (mtime, mtime))


async def ready(s):
    await s.refresh_users()
    return s


def status_line(reply: str) -> str:
    return reply.split("\n\n", 1)[1].splitlines()[0]


# ------------------------------------------------------------------------------- who and where


async def test_only_the_owners_one_to_one_dm_with_exactly_a_command_counts(make):
    s = await ready(make())
    assert s.allowed == {OWNER}
    assert s.command(dm("!vm")) == "!vm"
    assert s.command(dm("  !restart \n codex ")) == "!restart codex"
    assert s.command(dm("!restart")) == "!restart"
    for message in (
        dm("!vm", sender=OTHER, recipients=(OTHER, BOT)),  # not the owner
        dm("!vm", recipients=(OWNER, BOT, OTHER)),  # a group DM: others would see the reply
        dm("!vm extra"),
        dm("!restart now"),
        dm("!status"),  # the bridge's
        dm("!vm", sender=BOT),
        {**dm("!vm"), "type": "stream"},
    ):
        assert s.command(message) is None


async def test_commands_as_people_type_them(make):
    """#709: a verb in any case, the whole message one code span, a leading mention of this bot
    (its name from `users/me`, as the bridge's router has it); another bot's mention is none."""
    s = await ready(make())
    for typed, command in (
        ("!VM", "!vm"),
        ("`!vm`", "!vm"),
        ("@**Claude** !Restart", "!restart"),
        (f"@**Claude|{BOT}** `!restart codex`", "!restart codex"),
    ):
        assert s.command(dm(typed)) == command, typed
    # `!restart`'s one word in any case too (the review of #725): never "Unknown command"
    for typed in ("!restart Codex", "!Restart CODEX", "`!Restart Codex`"):
        assert s.command(dm(typed)) == "!restart codex", typed
    assert s.command(dm("!restart Now")) is None  # no other word is a sentinel command
    assert s.command(dm("!vm Codex")) is None
    assert s.command(dm("@**Codex** !vm")) is None
    assert sn.command_text("@**Claude** !vm") is None  # without the mention, no address
    # after a typed mention of the bot's kind (#713), then as typed (#709): the fixture's config
    # has no backend, so its kind is set as the bot's backend would give it
    s.kind = "claude"
    for typed, command in (
        ("@claude !VM", "!vm"),
        ("@Claude `!restart`", "!restart"),
        ("@claude: @**Claude** !Restart codex", "!restart codex"),
    ):
        assert s.command(dm(typed)) == command, typed
    assert s.command(dm("@codex !VM")) is None  # another kind's typed mention: no address
    assert sn.command_text("@claude !VM", "claude", s.mention) == "!vm"


async def test_allow_lists_replace_the_owner_as_in_the_bridge(make):
    s = make(bot=BotConfig("claude", "unused", "claude", allow_emails=["Ann@Example.org"]))
    s.client.users = [
        {"user_id": 9, "email": "ann@example.org", "is_bot": False, "is_active": True},
        {"user_id": OWNER, "email": "owner@example.org", "is_bot": False, "is_active": True},
    ]
    await s.refresh_users()
    assert s.allowed == {9}


async def test_an_allowed_email_that_joined_since_the_last_refresh(make, tmp_path):
    """Codex on #573: the bridge learns of a newly allowed sender from Zulip's user events; the
    sentinel only when it reads the users again. The mark records since when the sentinel
    allows each sender, and the bridge leaves it only their messages sent after that, as the
    sentinel answers only those: the first command, which makes the sentinel refresh (at most
    once a minute), is the bridge's, and the ones after it the sentinel's. Never neither."""
    s = await ready(make(bot=BotConfig("claude", "unused", "claude", allow_emails=["a@x.org"])))
    state = tmp_path / "state"
    s.clock.t += 30
    s.client.users = [{"user_id": 9, "email": "a@x.org", "is_bot": False, "is_active": True}]
    first = dm("!vm", sender=9, recipients=(9, BOT), sent=s.clock.t)
    await s.handle(first)  # within the cooldown: no refresh
    assert s.client.count("users") == 1 and not s.client.sent()
    assert not sn.listening(state, "claude", BOT, 9, first["timestamp"])  # so the bridge answers
    s.clock.t += sn.USERS_RETRY_S
    second = dm("!vm", sender=9, recipients=(9, BOT), mid=2, sent=s.clock.t)
    await s.handle(second)  # refreshes: sent before the sentinel allowed 9, so the bridge's
    assert s.client.count("users") == 2 and not s.client.sent()
    assert not sn.listening(state, "claude", BOT, 9, second["timestamp"])
    third = dm("!vm", sender=9, recipients=(9, BOT), mid=3, sent=s.clock.t + sn.CLOCK_MARGIN_S)
    assert sn.listening(state, "claude", BOT, 9, third["timestamp"])  # the mark was rewritten
    await s.handle(third)
    assert len(s.client.sent()) == 1
    await s.handle(dm("!vm", sender=OTHER, recipients=(OTHER, BOT), mid=4))
    await s.handle(dm("hello", sender=OTHER, recipients=(OTHER, BOT), mid=5))
    assert len(s.client.sent()) == 1 and s.client.count("users") == 2  # within the minute


async def test_the_queue_is_dm_only_and_its_own(make):
    s = make()
    assert await s.register() == ("q1", -1)
    _, endpoint, params = s.client.calls[-1]
    assert endpoint == "register" and params["event_types"] == ["message"]
    assert params["narrow"] == [["is", "dm"]]


# ----------------------------------------------------------------------------------- !vm


async def test_vm_reports_the_vm_and_the_bridge(make):
    s = await ready(make())
    touch(s.heartbeat_file, NOW - 40)
    await s.handle(dm("!vm"))
    (reply,) = s.client.sent()
    lines = reply.splitlines()
    assert lines[:2] == ["**ws-alice**", "🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0.4 of 8 CPUs"]
    # the bridge's state first, as the bridge's own `!vm` starts with its running turns
    assert lines[5] == (
        f"bridge running, last Zulip event 40 seconds ago · up 2 hours · zab {VERSION}"
    )
    assert lines[6] == "Using the most: `sage` 1.0 GB memory, 0.5 CPUs"
    assert s.collected == [["/", "/work", "/data/b"]]  # the root and the projects, as the bridge
    _, _, params = s.client.calls[-1]
    assert params["type"] == "private" and params["to"] == [OWNER]


@pytest.mark.parametrize(
    "state,age,line",
    [
        ("failed", 420, "bridge not running (failed): send !restart"),
        ("inactive", None, "bridge not running (inactive): send !restart"),
        (
            "active",
            420,
            "bridge running, but no Zulip event processed for 7 minutes: send !restart",
        ),
        ("active", None, "bridge running, no Zulip event processed yet"),
        ("activating", 30, "bridge starting"),
        ("deactivating", 30, "bridge stopping"),
        ("unknown", 30, "bridge state unknown (unknown)"),
    ],
)
async def test_vm_says_when_the_bridge_is_down_or_deaf(make, state, age, line):
    s = await ready(make(systemd=Systemd(active=state)))
    if age is not None:
        touch(s.heartbeat_file, NOW - age)
    await s.handle(dm("!vm"))
    assert status_line(s.client.sent()[0]) == f"{line} · up 2 hours · zab {VERSION}"


# ------------------------------------------------------------------------------ !restart


async def test_restart_runs_fixed_commands_and_confirms_on_the_new_bridges_heartbeat(make):
    # MainPID: 500 before the restart; at the polls 500 (still stopping), 0, then 600
    s = await ready(make(systemd=Systemd(pids=(500, 500, 0, 600))))
    touch(s.heartbeat_file, NOW - 600)
    ticks = []

    def polls():
        ticks.append(1)
        if len(ticks) == 1:  # the old bridge's last event, as it stops: no confirmation
            touch(s.heartbeat_file, s.clock.now())
        if len(ticks) == 4:  # the new bridge's listener advances
            touch(s.heartbeat_file, s.clock.now())

    s.clock.on_sleep = polls
    s.registered = NOW - 600  # serve() registered its queue then
    mark = sn.mark_path(s.state_dir, "claude")
    await s.handle(dm("!restart"))
    assert s.run.actions() == [
        ["systemctl", "--user", "reset-failed", "zab.service"],
        ["systemctl", "--user", "restart", "--no-block", "zab.service"],
    ]
    first, last = s.client.sent()
    assert first == "🔄 Restarting your bridge: running turns will be interrupted."
    assert last == (
        "✅ Your bridge is back: it is processing Zulip events again (8 seconds after the restart)."
    )
    # the wait kept the mark fresh: its queue holds the DMs that came meanwhile
    assert json.loads(mark.read_text())["registered"] == NOW - 600


async def test_a_bridge_that_was_down_is_confirmed_at_its_first_heartbeat(make):
    s = await ready(make(systemd=Systemd(pids=(0, 600), active="failed")))
    s.clock.on_sleep = lambda: touch(s.heartbeat_file, s.clock.now())
    await s.handle(dm("!restart"))
    assert s.client.sent()[-1].endswith("(2 seconds after the restart).")


async def test_a_restart_that_never_comes_back_says_so(make):
    # the old process keeps advancing and never goes: that is no confirmation
    s = await ready(make(systemd=Systemd(active="activating", pids=(500, None, 500))))
    s.clock.on_sleep = lambda: touch(s.heartbeat_file, s.clock.now())
    await s.handle(dm("!restart"))
    assert sum(s.clock.slept) >= sn.RESTART_WAIT_S
    assert s.client.sent()[-1] == (
        "⚠️ The bridge has not processed a Zulip event within 2 minutes of the restart "
        "(bridge: activating). Ask in your workshop's channel."
    )


async def test_a_failed_restart_command_is_reported(make):
    s = await ready(make(systemd=Systemd(restart_code=1, active="failed")))
    await s.handle(dm("!restart"))
    assert s.client.sent()[-1] == "⚠️ The restart failed (exit 1); bridge: failed."


async def test_one_restart_a_minute_across_bots(make):
    s = await ready(make(systemd=Systemd(pids=(0, 600))))
    s.clock.on_sleep = lambda: touch(s.heartbeat_file, s.clock.now())
    await s.handle(dm("!restart"))
    other = await ready(make())
    other.shared, other.clock = s.shared, s.clock  # the second bot's sentinel, same process
    other.now = s.now
    await other.handle(dm("!restart codex", mid=2))
    assert other.client.sent() == [
        "⏳ A restart ran less than a minute ago; try again in 58 s."  # 2 s of polling
    ]
    assert not other.run.calls
    s.clock.t += 61
    await other.handle(dm("!restart codex", mid=3))
    assert other.run.actions() == [
        ["systemctl", "--user", "reset-failed", "codex-app-server.service"],
        ["systemctl", "--user", "restart", "codex-app-server.service"],
    ]
    assert other.client.sent()[-1] == "✅ Codex is running again."


async def test_restart_codex_without_a_daemon(make):
    s = await ready(make(units=()))
    await s.handle(dm("!restart codex"))
    assert s.client.sent() == ["This VM has no Codex daemon to restart."] and not s.run.calls


async def test_a_codex_daemon_that_stays_down_is_reported(make):
    s = await ready(make(systemd=Systemd(active="failed")))
    await s.handle(dm("!restart codex"))
    assert s.client.sent()[-1] == (
        "⚠️ Codex did not come back (exit 0; failed). Ask in your workshop's channel."
    )


# --------------------------------------------------------------------------- the event loop


async def test_a_reply_zulip_does_not_take_is_tried_again(make):
    s = await ready(make())
    s.client.post_errors = [ZulipError("RATE_LIMIT_HIT", 429, retry_after=7), ZulipError("X", 503)]
    await s.handle(dm("!vm"))
    assert len(s.client.sent()) == 1 and s.clock.slept == [7.0, 4.0]  # as Zulip asked, doubling
    s.client.post_errors = [ZulipError("X", 502)] * sn.SEND_TRIES
    with pytest.raises(ZulipError):
        await s.handle(dm("!vm", mid=2))  # given up: `serve` handles the event again later
    s.client.post_errors = [ZulipError("BAD_REQUEST", 400)]
    await s.handle(dm("!vm", mid=3))  # refused: not tried again, and a short reply instead
    assert len(s.clock.slept) == 2 + sn.SEND_TRIES - 1
    assert s.client.sent()[1:] == [  # the post was refused, not the report (#588)
        "⚠️ Zulip refused the VM report (BAD_REQUEST). If your bots don't answer, send !restart."
    ]


async def test_serve_answers_a_vm_whose_reply_failed_at_a_later_poll(make):
    """Codex on #573: the bridge left this DM to the sentinel, so a reply Zulip didn't take
    must not be dropped with the event: the cursor stays before it and the next poll brings
    it again. A reply refused otherwise is not tried again: a short failure reply instead."""
    vm = [{"id": 1, "type": "message", "message": dm("!vm")}]
    refused = [{"id": 2, "type": "message", "message": dm("!vm", mid=2)}]
    s = make(events=[vm, vm, refused, [{"id": 3, "type": "heartbeat"}]])
    s.client.post_errors = [ZulipError("X", 503)] * sn.SEND_TRIES
    real = s.client.request

    async def request(method, endpoint, **params):
        if endpoint == "messages" and params["content"].startswith("**") and len(s.client.sent()):
            s.client.post_errors = [ZulipError("BAD_REQUEST", 400)]
        return await real(method, endpoint, **params)

    s.client.request = request
    touch(s.heartbeat_file, NOW)
    with pytest.raises(asyncio.CancelledError):
        await s.serve()
    polls = [p["last_event_id"] for _, e, p in s.client.calls if e == "events"]
    assert polls == [-1, -1, 1, 2, 3]  # the failed reply's event again; the refused one once
    assert len(s.client.sent()) == 2 and s.client.count("register") == 1
    assert s.client.sent()[1].startswith("⚠️ Zulip refused the VM report (BAD_REQUEST).")


async def test_a_restart_runs_once_whatever_zulip_does_with_its_messages(make):
    s = await ready(make(systemd=Systemd(pids=(0, 600), active="failed")))
    s.clock.on_sleep = lambda: touch(s.heartbeat_file, s.clock.now())
    s.client.post_errors = [ZulipError("X", 503)] * 100
    await s.handle(dm("!restart"))  # raises nothing: `serve` won't run it again
    assert [c for c in s.run.actions() if "restart" in c] == [
        ["systemctl", "--user", "restart", "--no-block", "zab.service"]
    ]
    assert not s.client.sent()


async def test_serve_retries_its_own_queue_and_registers_again_only_when_it_expired(make):
    batches = [
        [{"id": 0, "type": "heartbeat"}, {"id": 1, "type": "message", "message": dm("!vm")}],
        ZulipError("BAD_GATEWAY", 502),  # transient: the same queue, its DMs kept
        [{"id": 2, "type": "message", "message": dm("!vm", mid=2)}],
        ZulipError("RATE_LIMIT_HIT", 429, retry_after=30),
        ZulipError("BAD_EVENT_QUEUE_ID", 400),
        [{"id": 0, "type": "message", "message": dm("!vm", mid=3)}],
    ]
    s = make(events=batches)
    touch(s.heartbeat_file, NOW)
    with pytest.raises(asyncio.CancelledError):
        await s.serve()
    assert len(s.client.sent()) == 3
    assert s.client.count("register") == 2
    assert s.clock.slept == [5.0, 30.0, 10.0]  # backed off, as Zulip asked, then doubled
    polls = [p["last_event_id"] for _, e, p in s.client.calls if e == "events"]
    assert polls == [-1, 1, 1, 2, 2, -1, 0]  # past each answered message, -1 on a new queue


async def test_the_mark_tells_the_bridge_what_the_sentinel_received(make, tmp_path):
    s = make(events=[[{"id": 0, "type": "heartbeat"}]])
    with pytest.raises(asyncio.CancelledError):
        await s.serve()
    path = tmp_path / "state" / "sentinel-claude"
    body = json.loads(path.read_text())
    assert body == {
        "pid": os.getpid(),
        "boot": sn.boot_id(),
        "bot_id": BOT,
        "registered": NOW,
        "allowed": {str(OWNER): NOW},
    }
    state = tmp_path / "state"
    after = NOW + sn.CLOCK_MARGIN_S
    now = path.stat().st_mtime
    assert sn.listening(state, "claude", BOT, OWNER, after, now=now)
    assert not sn.listening(state, "claude", BOT, OWNER, after - 1, now=now)  # before it registered
    assert not sn.listening(state, "claude", BOT, OTHER, after, now=now)  # not a sender it allows
    assert not sn.listening(state, "claude", BOT, OWNER, after, now=now + sn.LISTENING_FRESH_S + 1)
    assert not sn.listening(state, "codex", BOT, OWNER, after, now=now)  # another bot's
    for changed in (
        {"boot": "earlier-boot"},
        {"pid": 2**22 + 1},
        {"pid": 0},
        {"pid": "x"},
        {"registered": None},
        {"allowed": {str(OWNER): NOW + 60}},  # allowed after the message was sent
        {"allowed": []},
        {"bot_id": 999},  # listening as another bot: its zuliprc changed (review on #573)
        {"bot_id": None},
    ):
        path.write_text(json.dumps(body | changed))
        assert not sn.listening(state, "claude", BOT, OWNER, after, now=now), changed
    path.write_text("not json")
    assert not sn.listening(state, "claude", BOT, OWNER, after, now=now)


async def test_an_expired_queue_hands_nothing_to_the_sentinel_until_the_next(make, tmp_path):
    """Zulip dropped the queue (a server restart, or ten minutes without a poll): its DMs are
    gone, so the mark goes until a new queue is registered; the bridge answers meanwhile."""
    path = tmp_path / "state" / "sentinel-claude"
    seen = []
    s = make(events=[[{"id": 0, "type": "heartbeat"}], ZulipError("BAD_EVENT_QUEUE_ID", 400)])

    async def sleep(seconds):
        seen.append(path.exists())
        s.clock.t += seconds

    s.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await s.serve()
    assert seen == [False]  # during the backoff, before the new registration
    assert json.loads(path.read_text())["registered"] == NOW + 5  # the new queue's


async def test_no_mark_before_the_state_directory_exists(make, tmp_path, caplog):
    s = make(events=[[{"id": 0, "type": "heartbeat"}]])
    (tmp_path / "state").rmdir()
    with pytest.raises(asyncio.CancelledError):
        await s.serve()
    assert "sentinel claude: no mark written (FileNotFoundError)" in caplog.text


def test_the_bridge_leaves_only_one_to_one_dm_commands(tmp_path):
    message = {**dm("!restart codex"), "timestamp": NOW}
    mark = {"pid": os.getpid(), "boot": sn.boot_id(), "bot_id": BOT, "registered": NOW - 60}
    sn.mark_path(tmp_path, "b").write_text(json.dumps(mark | {"allowed": {str(OWNER): 0}}))
    assert sn.answers(message, BOT, tmp_path, "b")
    assert not sn.answers({**message, "content": "!status"}, BOT, tmp_path, "b")
    assert not sn.answers({**dm("!vm", recipients=(OWNER, BOT, 9)), "timestamp": NOW}, BOT,
                          tmp_path, "b")  # fmt: skip
    assert not sn.answers({**message, "timestamp": None}, BOT, tmp_path, "b")
    # after a typed mention of the bot's own kind (#713), for the bridge and the sentinel alike
    typed = {**message, "content": "@Claude !restart codex"}
    assert sn.answers(typed, BOT, tmp_path, "b", "claude")
    assert not sn.answers(typed, BOT, tmp_path, "b", "codex")
    assert not sn.answers(typed, BOT, tmp_path, "b")
    assert sn.command_text("@claude: !vm", "claude") == "!vm"
    assert sn.command_text("@codex !vm", "claude") is None


async def test_health_reads_the_unit_then_the_mark_as_the_bridge_does(make, tmp_path):
    """#588, `zab doctor`'s `<bot>:sentinel`: the mark the sentinel writes is `ok`; each mark
    that `listening` wouldn't trust, the sender's rule apart, is `stale`; none is `no mark`."""
    s = make(events=[[{"id": 0, "type": "heartbeat"}]])
    with pytest.raises(asyncio.CancelledError):
        await s.serve()
    state, path = tmp_path / "state", tmp_path / "state" / "sentinel-claude"
    body = json.loads(path.read_text())
    now = path.stat().st_mtime + 30
    ok = ("ok", "its mark is from this boot, 30 seconds old")
    assert sn.health(state, "claude", BOT, "active", now=now) == ok
    assert sn.health(state, "claude", None, "active", now=now) == ok  # the bot's id unknown
    assert sn.health(state, "claude", BOT, None, now=now)[0] == "not installed"
    assert sn.health(state, "claude", BOT, "failed") == ("down", "zab-sentinel.service is failed")
    assert sn.health(state, "codex", BOT, "active", now=now)[0] == "no mark"
    late = now + sn.LISTENING_FRESH_S
    assert sn.health(state, "claude", BOT, "active", now=late) == (
        "stale",
        "its mark is 3 minutes old",
    )
    for changed, why in (
        ({"boot": "earlier-boot"}, "its mark is from another boot"),
        ({"pid": 2**22 + 1}, f"its mark's process {2**22 + 1} has ended"),
        ({"bot_id": 999}, "its mark is another bot's"),
        ({"pid": "x"}, "its mark can't be read (ValueError)"),
    ):
        path.write_text(json.dumps(body | changed))
        now = path.stat().st_mtime
        assert sn.health(state, "claude", BOT, "active", now=now) == ("stale", why), changed
    path.write_text("[]")
    assert sn.mark_health(state, "claude", BOT) == ("stale", "its mark can't be read (TypeError)")
    unknown = sn.health(state, "claude", BOT, "")  # systemctl answered nothing: no user bus
    assert unknown == ("unknown", "can't reach the user's systemd: `systemctl --user is-active` "
                       "gave no answer (is XDG_RUNTIME_DIR set?)")  # fmt: skip
    seen = {sn.health(state, "x", BOT, unit)[0] for unit in (None, "", "inactive", "active")}
    assert seen | {"stale"} == set(sn.UNHEALTHY)  # status.py's states (`SENTINEL_STATES`)
    assert set(sn.UNHEALTHY) - set(sn.DOWN) == {"unknown"}  # the one that doesn't say it's down


async def test_a_systemctl_that_cannot_start_is_exit_126():
    """A VM in trouble may refuse to fork (the user slice's TasksMax, no memory): reported as an
    exit code, never raised past the command (review on #573)."""
    assert await sn.run_command(["/nonexistent/systemctl", "--user"], 5) == (126, "")


async def test_every_command_the_bridge_left_gets_an_answer(make, tmp_path):
    """Review on #573: the bridge stays silent on what it leaves to the sentinel, so a command
    the sentinel can't carry out still gets a reply, and `handle` raises only what `serve`
    retries (a reply Zulip didn't take)."""

    async def no_fork(argv, seconds):
        return 126, ""  # as `run_command` reports a systemctl it can't start

    s = await ready(make(systemd=no_fork))
    await s.handle(dm("!restart"))
    assert s.client.sent() == [
        "🔄 Restarting your bridge: running turns will be interrupted.",
        "⚠️ The restart failed (exit 126); bridge: unknown.",
    ]
    assert "bridge state unknown (unknown)" in (await s.vm_report())

    def broken(paths):
        raise OSError("no /proc")

    s = await ready(make())
    s.collect = broken
    await s.handle(dm("!vm"))
    assert s.client.sent() == [
        "⚠️ The VM report failed (OSError). If your bots don't answer, send !restart."
    ]

    async def unexpected(argv, seconds):
        raise RuntimeError("anything else")

    s = await ready(make(systemd=unexpected))
    await s.handle(dm("!restart"))
    assert (
        s.client.sent()[-1]
        == "⚠️ The restart failed (RuntimeError). Ask in your workshop's channel."
    )


@pytest.mark.parametrize("hold", ["future", "garbage"])
async def test_the_organizers_hold_refuses_restarts(make, tmp_path, hold):
    """Review on #573: a rollout's `--lean-reclaim` or `--restart-codex` stops the bridge on
    purpose, under idle stop's hold; `!restart` must not start it again meanwhile. An unreadable
    hold counts for an hour from its mtime, as for idle stop."""
    s = await ready(make(systemd=Systemd(active="inactive")))
    path = tmp_path / "hold"
    path.write_text(str(int(NOW + 600)) if hold == "future" else "?")
    os.utime(path, (NOW, NOW))
    for command in ("!restart", "!restart codex"):
        await s.handle(dm(command))
    assert s.client.sent() == [sn.HELD, sn.HELD_CODEX] and not s.run.actions()
    assert "Codex" in sn.HELD_CODEX and "bridge" not in sn.HELD_CODEX  # #588
    assert s.shared.last_restart is None  # refused before the minute's restart is counted
    report = await s.vm_report()
    assert "bridge not running (inactive); your organizers are working on this VM" in report
    s.clock.t += 3601  # expired, or an hour past an unreadable one's mtime
    assert "bridge not running (inactive): send !restart" in await s.vm_report()
    s.run.active = "active"
    await s.handle(dm("!restart codex", mid=2))
    assert s.client.sent()[-1] == "✅ Codex is running again."


def test_the_hold_is_idle_stops():
    idle_stop = Path(__file__).resolve().parents[2] / "workshop" / "admin" / "idle_stop.py"
    assert f'HOLD = "{sn.HOLD}"' in idle_stop.read_text()


async def test_a_new_bridge_slow_to_its_first_event_is_starting_not_failed(make):
    """Review on #573: at the two minutes, a new main process that hasn't advanced its cursor
    yet (Zulip's first heartbeat comes about a minute after it polls) is starting."""
    s = await ready(make(systemd=Systemd(active="active", pids=(500, 600))))
    await s.handle(dm("!restart"))
    assert s.client.sent()[-1] == (
        "⏳ Your bridge is starting again but hasn't processed a Zulip event yet: send !vm in a "
        "minute."
    )


async def test_serve_removes_a_previous_processs_mark_before_reaching_zulip(make, tmp_path):
    s = make()
    path = tmp_path / "state" / "sentinel-claude"
    path.write_text(json.dumps({"pid": os.getpid(), "boot": sn.boot_id(), "bot_id": BOT}))

    async def unreachable():
        assert not path.exists()  # before any request to Zulip
        raise asyncio.CancelledError

    s.refresh_users = unreachable
    with pytest.raises(asyncio.CancelledError):
        await s.serve()


def test_codex_units_are_the_installed_managed_ones(tmp_path):
    from zab.commands.service import MARKER

    for name, text in (
        ("zab.service", MARKER),
        ("zab-sentinel.service", MARKER),
        ("codex-app-server.service", MARKER),
        ("codex-app-server-1.service", MARKER),
        ("codex-app-server-2.service", "[Unit]\n"),  # not written by zab
    ):
        (tmp_path / name).write_text(text)
    assert sn.codex_units(tmp_path) == ["codex-app-server-1.service", "codex-app-server.service"]


def test_main_refuses_a_bad_config(tmp_path, capsys):
    bad = tmp_path / "config.toml"
    bad.write_text("[bridge]\nstate_dir = 3\n")
    assert sn.main(["--config", str(bad)]) == 2
    assert "zab sentinel:" in capsys.readouterr().err


def test_zab_sentinel_is_a_command():
    assert cli.COMMANDS["sentinel"] == "zab.tools.sentinel"
    assert sn.SENTINEL_UNIT == "zab-sentinel.service"
