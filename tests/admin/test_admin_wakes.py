"""The wake monitor (#359; docs/ADMIN.md §1.2, §1.5): a fake start log, as the starter writes it,
and a fake Zulip realm in which the participant's bots received, or didn't, a message from the
participant before each start. The bots' Zulip owner is someone else (OLGA), as the
provisioner owns them since #364: the rule is the roster's `zulip_user_id`, never
`bot_owner_id`."""

import asyncio
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from zab.admin import checks, starter, wakes
from zab.admin.bot import StartupError
from zab.admin.config import parse_admin_config
from zab.waker import queues

from .botkit import ADMINS_CH, CODEX_BOB, GLOBAL, OLGA, PAT, make_site
from .test_admin_starter import CONFIG

pytestmark = pytest.mark.integration

CLAUDE_BOB = 503
SANDBOX = 77  # bob's sandbox channel, where his Claude bot is subscribed
I1, I2, I3 = "i-0aaaaaaaaaaaaaaa1", "i-0bbbbbbbbbbbbbbb2", "i-0ccccccccccccccc3"
ROSTER = (
    "slug,first_name,last_name,email,github,ip,region,zulip_user_id\n"
    "alice,Alice,A,alice@example.com,alice-gh,198.51.100.1,us-west-2,\n"
    f"bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2,{PAT}\n"
)
#: The waker's config (`push_waker.py --config-dir`'s shape): I1 and I2 are opted in, I3 isn't.
OPT_INS = f"""starter = "/run/zab-admin-starter/start.sock"
[[workshop]]
name = "harvard-2026"
organizers_channel = 44
[[workshop.bots]]
name = "codex-bob"
instance_id = "{I1}"
[[workshop.bots]]
name = "claude-bob"
instance_id = "{I2}"
"""
WORDS = "PRIVATE-WORDS-OF-THE-PARTICIPANT"
#: A malformed key line, `key <32 alphanumerics>`, that configparser's error would quote (#527).
MALFORMED_KEY = "Decoy527" * 4


@pytest.fixture
def site(tmp_path):
    site = make_site(tmp_path)
    site.realm.person(CLAUDE_BOB, "Claude · bob", bot=True)
    for bot in (CODEX_BOB, CLAUDE_BOB):
        site.realm.users[bot]["bot_owner_id"] = OLGA  # not the participant (#364)
    site.realm.channel(SANDBOX, PAT, CLAUDE_BOB, OLGA)
    ws = tmp_path / "ws" / "harvard-2026"
    (ws / "roster.csv").write_text(ROSTER)
    (ws / "out" / "bob").mkdir()
    for kind, uid in (("codex", CODEX_BOB), ("claude", CLAUDE_BOB)):
        (ws / "out" / "bob" / f"{kind}.zuliprc").write_text(site.realm.zuliprc(uid))
    opt_ins(site, OPT_INS, mtime=1.0)
    return site


@pytest.fixture
async def server(site):
    srv = site.server()
    await srv.start()
    yield srv
    await srv.close()


def opt_ins(site, text, mtime):
    path = Path(site.paths.layout.waker) / "config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def log_start(site, t, instance=I1, participant="bob", peer="zab-waker"):
    line = {"t": t, "workshop": "harvard-2026", "participant": participant,
            "instance": instance, "peer": peer}  # fmt: skip
    append(site, json.dumps(line) + "\n")


def append(site, text):
    path = Path(site.paths.start_log) / starter.LOG_NAME
    with open(path, "a") as f:
        f.write(text)
    path.chmod(0o640)  # as the starter sets it


def chmod(path, mode):
    Path(path).chmod(mode)


def records(site):
    text = site.audit.read_text() if site.audit.exists() else ""
    rows = [json.loads(line) for line in text.splitlines()]
    return [r for r in rows if r["event"] == "wake_check"]


def alerts(site):
    return [p for p in site.realm.posts() if p.get("topic") == wakes.TOPIC]


def gets(site):
    return [r for r in site.realm.requests if r[1:] == ("GET", "messages")]


def at(site, offset):
    """The realm's clock `offset` seconds from the bots' now: the time of the next messages."""
    site.realm.now = int(site.clock.now + offset)


async def restart(site, server):
    await server.close()
    restarted = site.server()
    await restarted.start()
    return restarted


async def test_a_start_after_the_owners_dm_raises_no_alert(site, server):
    at(site, -200)
    sent = site.realm.dm(PAT, WORDS, CODEX_BOB)
    log_start(site, site.clock.now - 100)
    await server.wakes.check_pass()
    assert alerts(site) == []
    (record,) = records(site)
    assert (record["outcome"], record["message"], record["via"]) == ("matched", sent, "codex")
    assert (record["instance"], record["participant"], record["owner"]) == (I1, "bob", PAT)
    assert WORDS not in site.audit.read_text()  # the matched message's ID and time, no text
    await server.wakes.check_pass()
    assert len(records(site)) == 1  # checked once


async def test_a_channel_message_its_bot_received_counts_too(site, server):
    """The waker wakes a VM for any message its bots receive from the owner (SPEC §13.4;
    decided on #391)."""
    at(site, -30)
    site.realm.say(PAT, SANDBOX, "an unmentioned message in the sandbox")
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    assert alerts(site) == [] and records(site)[0]["via"] == "claude"


@pytest.mark.parametrize(
    ("offset", "outcome"),
    [(20, "matched"), (wakes.SKEW + 10, "unmatched"), (-wakes.WINDOW + 10, "matched"),
     (-wakes.WINDOW - 10, "unmatched")],
    ids=["just after", "too late", "just in", "too early"],
)  # fmt: skip
async def test_the_window(site, server, offset, outcome):
    at(site, offset)
    site.realm.dm(PAT, "wake up", CODEX_BOB)
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    assert records(site)[0]["outcome"] == outcome


@pytest.mark.parametrize("retries", [0, 60 + 120], ids=["first try", "third try"])
async def test_a_start_for_a_message_the_waker_caught_up_on_is_matched(site, server, retries):
    """#403: after a restart the waker wakes a VM for a message up to `CATCH_UP_AGE` old. Its
    start, even its third try after two capacity failures, finds that message in the window,
    unless the wake first waited for a VM still stopping (up to 300 s more): that start can be
    unmatched, a false alarm (reviewer on #405)."""
    at(site, -(queues.CATCH_UP_AGE + retries))
    sent = site.realm.dm(PAT, "!status", CODEX_BOB)
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    assert (records(site)[0]["outcome"], records(site)[0]["message"]) == ("matched", sent)


async def test_a_start_with_no_message_alerts_once(site, server):
    at(site, -wakes.WINDOW - 60)
    site.realm.dm(PAT, WORDS, CODEX_BOB)  # too early
    at(site, wakes.SKEW + 5)
    site.realm.dm(PAT, WORDS, CODEX_BOB)  # after the start: it can't have asked for it
    log_start(site, site.clock.now, peer="root")
    await server.wakes.check_pass()
    (alert,) = alerts(site)
    assert alert["to"] == str(ADMINS_CH)  # the global admin bot's channel
    for part in (I1, "harvard-2026", "`bob`", "asked by root", f"user {PAT}", " UTC,"):
        assert part in alert["content"], part
    (record,) = records(site)
    assert record["outcome"] == "unmatched" and type(record["alert"]) is int
    await server.wakes.check_pass()
    assert len(alerts(site)) == 1 and len(records(site)) == 1
    assert WORDS not in site.audit.read_text()  # IDs and times only, never the text
    assert all(WORDS not in p["content"] for p in site.realm.posts())


async def test_only_the_participant_counts(site, server):
    """The bots' Zulip owner (OLGA, as the provisioner owns them since #364) and anyone else are
    not the participant: their messages to the bots explain nothing."""
    at(site, -60)
    site.realm.dm(OLGA, "!status", CODEX_BOB)
    site.realm.dm(OLGA, "!status", CLAUDE_BOB)
    site.realm.say(OLGA, SANDBOX, "@**Claude · bob** hello")
    site.realm.dm(PAT, "to someone else", OLGA)  # the participant, but not to their bots
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    assert len(alerts(site)) == 1 and records(site)[0]["outcome"] == "unmatched"


async def test_an_alert_zulip_refused_is_posted_again(site, server):
    """Codex round 1: until the alert goes out, the start is not done, across passes and a
    restart; past HORIZON it is recorded as never delivered."""
    site.realm.broken.add(GLOBAL)
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    await server.wakes.check_pass()
    assert alerts(site) == [] and records(site) == []
    restarted = await restart(site, server)  # nothing recorded it: checked again
    try:
        await restarted.wakes.check_pass()
        assert records(site) == [] and len(restarted.wakes.undelivered) == 1
        site.realm.broken.clear()
        await restarted.wakes.check_pass()
        assert len(alerts(site)) == 1
        (record,) = records(site)
        assert record["outcome"] == "unmatched" and type(record["alert"]) is int
        assert restarted.wakes.undelivered == {}
        site.realm.broken.add(GLOBAL)
        site.clock.now += wakes.ALERT_EVERY + 1
        log_start(site, site.clock.now, instance=I2)
        await restarted.wakes.check_pass()
        site.clock.now += wakes.HORIZON + 1
        await restarted.wakes.check_pass()
        assert records(site)[-1]["instance"] == I2 and records(site)[-1]["alert"] is None
        assert restarted.wakes.undelivered == {} and len(alerts(site)) == 1
    finally:
        await restarted.close()


async def test_alerts_are_rate_limited_per_vm_across_restarts(site, server):
    log_start(site, site.clock.now - 10)
    await server.wakes.check_pass()
    site.clock.now += 600
    held = site.clock.now - 10
    log_start(site, held)
    log_start(site, site.clock.now - 5, instance=I2)  # another VM alerts on its own
    await server.wakes.check_pass()
    assert [I1 in a["content"] for a in alerts(site)] == [True, False]
    assert [r.get("held") for r in records(site)] == [None, True, None]
    restarted = await restart(site, server)  # the records are the monitor's memory
    try:
        site.clock.now += 600
        log_start(site, site.clock.now - 10)
        await restarted.wakes.check_pass()
        assert len(alerts(site)) == 2 and records(site)[-1]["held"] is True
        site.clock.now += wakes.ALERT_EVERY
        log_start(site, site.clock.now - 10)
        await restarted.wakes.check_pass()
        assert len(alerts(site)) == 3 and records(site)[-1]["held_before"] == 2
        text = alerts(site)[-1]["content"]
        assert "(2 more of this VM's starts were held back since the last alert" in text
        assert f"made at {wakes.utc(held)}, " in text  # each listed by its time
    finally:
        await restarted.close()


def _roster(site, text):
    (site.tmp / "ws/harvard-2026/roster.csv").write_text(text)


def _malformed(site):
    (site.tmp / "ws/harvard-2026/out/bob/codex.zuliprc").write_text(f"[api]\nkey {MALFORMED_KEY}\n")


@pytest.mark.parametrize(
    ("change", "reason", "tries"),
    [
        (lambda s: (s.tmp / "ws/harvard-2026/out/bob/codex.zuliprc").unlink(),
         "no codex.zuliprc in its out/", 1),
        (lambda s: s.realm.broken.add(CODEX_BOB), "codex bot: Zulip INTERNAL_SERVER_ERROR",
         wakes.ATTEMPTS),
        (lambda s: _roster(s, ROSTER.replace(f",{PAT}\n", ",\n")),
         "its roster row has no zulip_user_id", 1),
        (lambda s: _roster(s, ROSTER.replace("bob,", "robert,")), "no roster row bob", 1),
        (_malformed, "codex bot: ParsingError", wakes.ATTEMPTS),
    ],
    ids=["no key", "zulip", "no user id", "no row", "malformed key"],
)  # fmt: skip
async def test_a_failed_check_alerts_with_its_reason(site, server, change, reason, tries):
    """At once when another try can't help; a Zulip error is tried on the next passes first. A
    malformed zuliprc is said by its parse error's type, never its line (#527)."""
    change(site)
    log_start(site, site.clock.now)
    for _ in range(tries - 1):
        await server.wakes.check_pass()
        assert alerts(site) == [] and records(site) == []
    await server.wakes.check_pass()
    (alert,) = alerts(site)
    assert "could not be checked" in alert["content"] and reason in alert["content"]
    (record,) = records(site)
    assert record["outcome"] == "failed" and reason in record["reason"]
    assert MALFORMED_KEY not in alert["content"] + json.dumps(record)


async def test_too_many_messages_since_the_start_is_a_failed_check(site, server, monkeypatch):
    """A busy participant can't make a start look unexplained: the check fails, and says why."""
    monkeypatch.setattr(wakes, "PAGE", 2)
    at(site, wakes.SKEW + 60)
    for _ in range(8):
        site.realm.dm(PAT, "later", CODEX_BOB)
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    (record,) = records(site)
    assert record["outcome"] == "failed"
    assert "more than 4 of the participant's messages since the start" in record["reason"]


async def test_one_bot_that_received_the_message_is_enough(site, server):
    site.realm.broken.add(CLAUDE_BOB)  # read first, and failing
    at(site, -60)
    site.realm.dm(PAT, "wake up", CODEX_BOB)
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    assert alerts(site) == [] and records(site)[0]["via"] == "codex"


# ----------------------------------------------------------------------------- opted in?


async def test_a_waker_start_of_a_vm_not_opted_in_alerts_at_once(site, server):
    """The hardening of #391: the waker's config, which it can't write, says
    which VMs it may start; a message from the participant explains nothing then."""
    at(site, -60)
    site.realm.dm(PAT, "wake up", CODEX_BOB)
    log_start(site, site.clock.now, instance=I3)
    await server.wakes.check_pass()
    (alert,) = alerts(site)
    assert I3 in alert["content"] and "doesn't opt it in" in alert["content"]
    assert records(site)[0]["outcome"] == "not_opted_in" and gets(site) == []
    log_start(site, site.clock.now + 1, instance=I3, peer="root")  # root's: checked as any
    await server.wakes.check_pass()
    assert records(site)[-1]["outcome"] == "matched" and len(alerts(site)) == 1
    log_start(site, site.clock.now + 2, instance=I3)  # held back: its own limit
    await server.wakes.check_pass()
    assert records(site)[-1]["held"] is True and len(alerts(site)) == 1


async def test_a_config_changed_after_the_start_or_unreadable_judges_nothing(site, server):
    opt_ins(site, OPT_INS, mtime=site.clock.now + 5)  # `!waker off` after the start, say
    at(site, -60)
    site.realm.dm(PAT, "wake up", CODEX_BOB)
    log_start(site, site.clock.now, instance=I3)
    await server.wakes.check_pass()
    assert records(site)[0]["outcome"] == "matched"
    opt_ins(site, "not = [toml", mtime=1.0)
    log_start(site, site.clock.now + 1, instance=I3)
    await server.wakes.check_pass()
    assert records(site)[-1]["outcome"] == "matched"
    (Path(site.paths.layout.waker) / "config.toml").unlink()  # none: the waker starts nothing
    log_start(site, site.clock.now + 2, instance=I1)
    await server.wakes.check_pass()
    assert records(site)[-1]["outcome"] == "not_opted_in"


# --------------------------------------------------------------- nothing ages out silently


async def test_a_check_failing_at_the_horizon_is_recorded_and_alerted(site, server):
    site.realm.broken.add(CODEX_BOB)  # a Zulip error: tried again on the next pass
    log_start(site, site.clock.now - wakes.HORIZON + 60)
    await server.wakes.check_pass()
    assert records(site) == []
    site.clock.now += wakes.PERIOD  # past HORIZON before its next try
    await server.wakes.check_pass()
    (record,) = records(site)
    assert record["outcome"] == "expired" and [s["instance"] for s in record["starts"]] == [I1]
    assert "Zulip INTERNAL_SERVER_ERROR" in record["starts"][0]["reason"]
    (alert,) = alerts(site)
    assert "1 starts were not checked within 6 hours" in alert["content"]
    assert I1 in alert["content"]


async def test_a_backlog_that_ages_out_is_recorded_and_alerted_once(site, server, monkeypatch):
    monkeypatch.setattr(wakes, "CALLS", 2)  # one start a pass
    for n, instance in enumerate((I1, I2, I3)):
        log_start(site, site.clock.now - wakes.HORIZON + 30 + n, instance=instance, peer="root")
    await server.wakes.check_pass()
    assert [r["outcome"] for r in records(site)] == ["unmatched"]
    site.clock.now += wakes.PERIOD
    await server.wakes.check_pass()
    record = records(site)[-1]
    assert [s["instance"] for s in record["starts"]] == [I2, I3]
    assert record["starts"][0]["reason"] == "not checked in time"
    assert "2 starts were not checked" in alerts(site)[-1]["content"]


async def test_starts_older_than_the_horizon_when_first_seen_alert_once(site, server):
    """After the bots were down: one summary, never repeated after a restart; another batch
    within the hour is held back, then counted in the next summary."""
    for n in range(3):
        log_start(site, site.clock.now - wakes.HORIZON - 100 + n, instance=f"i-{n:017x}")
    await server.wakes.check_pass()
    (alert,) = alerts(site)
    assert "3 starts were not checked within 6 hours" in alert["content"]
    restarted = await restart(site, server)
    try:
        await restarted.wakes.check_pass()
        assert len(alerts(site)) == 1 and len(records(site)) == 1
        log_start(site, site.clock.now - wakes.HORIZON - 50, instance=I1)
        await restarted.wakes.check_pass()
        assert records(site)[-1]["held"] is True and len(alerts(site)) == 1
        site.clock.now += wakes.ALERT_EVERY
        log_start(site, site.clock.now - wakes.HORIZON - 50, instance=I2)
        await restarted.wakes.check_pass()
        assert len(alerts(site)) == 2
        assert "(1 more such starts were held back" in alerts(site)[-1]["content"]
    finally:
        await restarted.close()


async def test_a_summary_zulip_refused_is_posted_again(site, server):
    """Codex round 4: the aged-out starts stay unrecorded until their summary goes out, across
    passes and a restart; twice HORIZON on, they are recorded as never delivered."""
    site.realm.broken.add(GLOBAL)
    log_start(site, site.clock.now - wakes.HORIZON - 100, instance=I1)
    await server.wakes.check_pass()
    await server.wakes.check_pass()
    assert alerts(site) == [] and records(site) == []
    restarted = await restart(site, server)
    try:
        await restarted.wakes.check_pass()
        assert records(site) == []
        site.realm.broken.clear()
        await restarted.wakes.check_pass()
        (alert,) = alerts(site)
        assert "1 starts were not checked" in alert["content"] and I1 in alert["content"]
        (record,) = records(site)
        assert record["outcome"] == "expired" and record["alert"] is not None
        site.realm.broken.add(GLOBAL)
        site.clock.now += wakes.ALERT_EVERY
        log_start(site, site.clock.now - wakes.HORIZON - 100, instance=I2)
        await restarted.wakes.check_pass()
        assert len(records(site)) == 1
        site.clock.now += wakes.HORIZON
        await restarted.wakes.check_pass()
        assert (
            records(site)[-1]["alert"] is None and records(site)[-1]["starts"][0]["instance"] == I2
        )
        await restarted.wakes.check_pass()
        assert len(records(site)) == 2 and len(alerts(site)) == 1
    finally:
        await restarted.close()


async def test_a_record_the_audit_log_refused_is_written_on_the_next_pass(
    site, server, monkeypatch
):
    """Codex round 5: a start is done only once its record is written. A refused record is
    written first on the next pass, as decided: its start is neither checked nor alerted
    again; an aged-out batch, the same."""
    log_start(site, site.clock.now, peer="root")
    log_start(site, site.clock.now - wakes.HORIZON - 100, instance=I2, peer="root")
    real = server.audit

    async def full(event, **fields):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(server, "audit", full)
    with pytest.raises(OSError):
        await server.wakes.check_pass()  # the batch's record fails first, and ends the pass
    with pytest.raises(OSError):
        await server.wakes.check_pass()
    monkeypatch.setattr(server, "audit", real)
    await server.wakes.check_pass()
    assert [r["outcome"] for r in records(site)] == ["expired", "unmatched"]
    assert [type(r["alert"]) for r in records(site)] == [int, int] and len(alerts(site)) == 2
    asked = len(gets(site))
    await server.wakes.check_pass()
    assert len(records(site)) == 2 and len(alerts(site)) == 2 and len(gets(site)) == asked
    log_start(site, site.clock.now + 1, instance=I3, peer="root")
    monkeypatch.setattr(server, "audit", full)
    with pytest.raises(OSError):
        await server.wakes.check_pass()  # checked, alerted, and its record refused
    asked = len(gets(site))
    monkeypatch.setattr(server, "audit", real)
    await server.wakes.check_pass()
    record = records(site)[-1]
    assert (record["instance"], record["outcome"]) == (I3, "unmatched")
    assert type(record["alert"]) is int and "held" not in record
    assert len(alerts(site)) == 3 and len(gets(site)) == asked  # not checked, not alerted again


async def test_not_opted_in_has_a_limit_of_its_own_across_restarts(site, server):
    """A VM's "no message" alert of the hour never holds back its "not opted in" one, nor the
    reverse; after a restart, each limit holds still (review of #391)."""
    log_start(site, site.clock.now, instance=I3, peer="root")
    await server.wakes.check_pass()
    log_start(site, site.clock.now + 1, instance=I3)  # the waker's, and I3 isn't opted in
    await server.wakes.check_pass()
    assert [r["outcome"] for r in records(site)] == ["unmatched", "not_opted_in"]
    assert len(alerts(site)) == 2 and "doesn't opt it in" in alerts(site)[-1]["content"]
    restarted = await restart(site, server)
    try:
        log_start(site, site.clock.now + 2, instance=I3, peer="root")
        log_start(site, site.clock.now + 3, instance=I3)
        await restarted.wakes.check_pass()
        assert [r.get("held") for r in records(site)[-2:]] == [True, True]
        assert len(alerts(site)) == 2
    finally:
        await restarted.close()


async def test_nothing_is_forgotten_while_the_log_cant_be_read(site, server):
    log_start(site, site.clock.now, peer="root")
    await server.wakes.check_pass()
    asked = len(gets(site))
    chmod(site.paths.start_log, 0o770)
    await server.wakes.check_pass()  # the log's problem alerts; what was checked stays so
    chmod(site.paths.start_log, 0o750)
    await server.wakes.check_pass()
    assert [r["outcome"] for r in records(site)] == ["unmatched", "failed"]
    assert len(gets(site)) == asked and len(alerts(site)) == 2


async def test_a_check_that_times_out_is_tried_again_first(site, server, monkeypatch):
    async def slow(start, calls):
        await asyncio.sleep(5)

    monkeypatch.setattr(wakes, "CHECK_S", 0.05)
    monkeypatch.setattr(server.wakes, "check", slow)
    log_start(site, site.clock.now)
    for _ in range(wakes.ATTEMPTS - 1):
        await server.wakes.check_pass()
        assert records(site) == [] and alerts(site) == []
    await server.wakes.check_pass()
    (record,) = records(site)
    assert record["outcome"] == "failed" and "no answer within" in record["reason"]


async def test_one_record_lists_at_most_expire_per_pass_starts(site, server, monkeypatch):
    monkeypatch.setattr(wakes, "EXPIRE_PER_PASS", 2)
    for n in range(5):
        log_start(site, site.clock.now - wakes.HORIZON - 100 + n, instance=f"i-{n:017x}")
    for _ in range(4):
        await server.wakes.check_pass()
    assert [len(r["starts"]) for r in records(site)] == [2, 2, 1]
    assert [r.get("held") for r in records(site)] == [None, True, True]
    assert len(alerts(site)) == 1  # one summary an hour; the next counts the 3 held back


# -------------------------------------------------------------------------- the log itself


async def test_a_start_log_others_could_write_is_not_read(site, server, monkeypatch):
    log = Path(site.paths.start_log)
    log_start(site, site.clock.now)
    chmod(log, 0o770)
    await server.wakes.check_pass()
    (alert,) = alerts(site)
    assert "start log" in alert["content"] and "writable by group" in alert["content"]
    (record,) = records(site)
    assert record["outcome"] == "failed" and "instance" not in record
    await server.wakes.check_pass()  # the same problem: no new alert, no new record
    assert len(alerts(site)) == 1 and len(records(site)) == 1
    with pytest.raises(StartupError, match="writable by group"):
        await site.server().preflight()  # nor do the bots start on it
    chmod(log, 0o750)
    chmod(log / starter.LOG_NAME, 0o666)
    assert "not a file of the starter's alone" in wakes.read_log(str(log), site.modes)[1]
    chmod(log / starter.LOG_NAME, 0o640)
    real = os.fstat

    def someone_elses(fd):
        st = real(fd)
        if not stat.S_ISREG(st.st_mode):
            return st
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1,
                               st.st_gid, st.st_size, 0, 0, 0))  # fmt: skip

    with monkeypatch.context() as m:
        m.setattr(wakes.os, "fstat", someone_elses)  # in a directory of the starter's
        problem = wakes.read_log(str(log), site.modes)[1]
    assert "not a file of the starter's alone" in problem


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda log, m: log.rmdir(), "is missing"),
        (lambda log, m: log.chmod(0o755), "open to others"),
        (lambda log, m: log.parent.chmod(0o731), "writable by group or others"),
        (lambda log, m: (log.rmdir(), log.symlink_to(log.parent)), "symbolic link"),
        (lambda log, m: (log.rmdir(), log.write_text("")), "not a directory"),
    ],
    ids=["missing", "others", "parent", "link", "file"],
)
def test_the_start_log_directory_is_the_starters_alone(tmp_path, change, problem):
    log = tmp_path / "zab-starter" / "log"
    log.mkdir(parents=True, mode=0o750)
    log.parent.chmod(0o711)
    modes = checks.Modes(group=log.stat().st_gid, starter=os.getuid())
    assert checks.check_start_log(log, modes) == ""
    assert "not zab-starter" in checks.check_start_log(log, checks.Modes(starter=-1))
    other_group = checks.Modes(group=-1, starter=os.getuid())
    assert "not the bot's" in checks.check_start_log(log, other_group)
    change(log, modes)
    assert problem in checks.check_start_log(log, modes)


async def test_bounds_per_pass(site, server, monkeypatch):
    """At most PER_PASS starts and CALLS GET /messages per pass; the rest wait, oldest first."""
    monkeypatch.setattr(wakes, "CALLS", 6)
    for n in range(wakes.PER_PASS + 5):
        log_start(site, site.clock.now - 100 + n, instance=f"i-{n:017x}", peer="root")
    await server.wakes.check_pass()
    assert len(gets(site)) == 6 and len(records(site)) == 3  # one call per bot, none matched
    monkeypatch.setattr(wakes, "CALLS", 100)
    await server.wakes.check_pass()
    assert len(records(site)) == 3 + wakes.PER_PASS
    await server.wakes.check_pass()
    assert [r["instance"] for r in records(site)] == [
        f"i-{n:017x}" for n in range(wakes.PER_PASS + 5)
    ]


async def test_bad_lines_alert_and_a_line_being_written_waits(site, server):
    append(site, '{"t": 1, "workshop": "harvard-2026", "instance": "i-../../x"}\n')
    append(site, '{"t": 1800000000, "workshop": "harv')  # a write in progress
    await server.wakes.check_pass()
    (alert,) = alerts(site)
    assert "1 of its lines are not starts" in alert["content"]  # the last one waits
    assert [r.get("instance") for r in records(site)] == [None]


def test_a_participant_tag_that_is_no_slug_is_kept_for_its_check_to_fail():
    start = wakes.Start.of({"t": 5, "workshop": "ws", "participant": "../x", "instance": I1})
    assert start is not None and start.participant is None and start.peer == "?"


async def test_the_pages_reach_back_to_the_window(site, server, monkeypatch):
    monkeypatch.setattr(wakes, "PAGE", 2)
    at(site, -60)
    sent = site.realm.dm(PAT, "wake up", CODEX_BOB)
    at(site, wakes.SKEW + 60)
    for _ in range(3):  # newer ones, after the start
        site.realm.dm(PAT, "later", CODEX_BOB)
    log_start(site, site.clock.now)
    await server.wakes.check_pass()
    assert records(site)[0]["message"] == sent


# ------------------------------------------------------------------ the starter's start log


@pytest.fixture
def started(tmp_path):
    """The start log as on the server: the state 0711, `log/` setgid 2750, UMask=0077."""
    state = tmp_path / "zab-starter"
    state.mkdir(mode=0o711)
    (state / starter.LOG_DIR).mkdir()
    (state / starter.LOG_DIR).chmod(0o2750)
    service = starter.Starter(parse_admin_config(CONFIG), {}, state, {0}, lambda: 1000.0)
    old = os.umask(0o077)
    try:
        log_vm(service, I1, 1000.0, os.getuid())
    finally:
        os.umask(old)
    return service


def log_vm(service, instance, t, peer=None, slug="alice"):
    vm = {"InstanceId": instance, "Tags": [{"Key": "participant", "Value": slug}]}
    service.log_start(service.config.workshops[0], vm, peer, t)


def logged(service):
    log = service.state / starter.LOG_DIR
    modes = checks.Modes(group=log.stat().st_gid, starter=os.getuid())
    return wakes.read_log(str(log), modes)


def test_the_starter_logs_each_start_for_the_bots_group_alone(started):
    """0640 whatever the umask, in the directory's group: the bots' group reads it; the waker's
    user, neither its owner nor in that group, can't write it, nor the directory (2750)."""
    log = started.state / starter.LOG_DIR
    st, dst = (log / starter.LOG_NAME).stat(), log.stat()
    assert stat.S_IMODE(st.st_mode) == 0o640 and st.st_gid == dst.st_gid
    assert stat.S_IMODE(dst.st_mode) == 0o2750
    assert not (st.st_mode | dst.st_mode) & 0o027  # nobody but the owner writes; others nothing
    (line,) = (log / starter.LOG_NAME).read_text().splitlines()
    assert json.loads(line) == {"t": 1000.0, "workshop": "alpha", "participant": "alice",
                                "instance": I1, "peer": starter.peer_name(os.getuid())}  # fmt: skip


def test_the_start_log_rotates_and_both_files_are_read(started, monkeypatch):
    monkeypatch.setattr(starter, "LOG_MAX", 10)
    log_vm(started, I2, 2000.0, slug="bob")
    assert (started.state / starter.LOG_DIR / (starter.LOG_NAME + ".1")).exists()
    starts, problem = logged(started)
    assert problem == "" and [(s.instance, s.t) for s in starts] == [(I1, 1000.0), (I2, 2000.0)]
    assert starts[1].peer == "?"  # not known


def test_a_write_that_fails_part_way_leaves_no_fragment(started, monkeypatch):
    """Review of #391: the start is refused, and the next start's line reads whole."""
    path = started.state / starter.LOG_DIR / starter.LOG_NAME
    before, real = path.read_bytes(), os.write

    def part_then_full(fd, data):
        real(fd, bytes(data[:10]))
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(starter.os, "write", part_then_full)
        with pytest.raises(OSError):
            log_vm(started, I2, 2000.0)
    assert path.read_bytes() == before
    log_vm(started, I3, 3000.0)
    assert logged(started) == ([wakes.Start.of(json.loads(line)) for line in
                                path.read_bytes().splitlines()], "")  # fmt: skip
    assert [s.t for s in logged(started)[0]] == [1000.0, 3000.0]


def test_a_fragment_left_behind_never_joins_the_next_line(started):
    path = started.state / starter.LOG_DIR / starter.LOG_NAME
    with open(path, "ab") as f:
        f.write(b'{"t": 1500, "work')  # as if a cut had failed too
    log_vm(started, I3, 3000.0)
    starts, problem = logged(started)
    assert [s.t for s in starts] == [1000.0, 3000.0] and problem == "1 of its lines are not starts"
