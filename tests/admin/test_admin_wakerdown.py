"""The waker down (#717; ADMIN §1.2, §1.8): its status file as the waker writes it, its config as
the bots write it, and the workshop's bot's alert in its fleet-alerts channel."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from zab.admin import pages, wakerdown
from zab.admin.wakerdown import DOWN, DOWN_AFTER, OFF, UP

from .botkit import HARVARD, make_site

pytestmark = pytest.mark.integration
ALERTS = 44
OPT_IN = """starter = "/run/zab-admin-starter/start.sock"
[[workshop]]
name = "harvard-2026"
organizers_channel = 44
[[workshop.bots]]
name = "codex-bob"
instance_id = "i-0aaaaaaaaaaaaaaa1"
"""


@pytest.fixture
def site(tmp_path):
    site = make_site(tmp_path)
    text = site.config_path.read_text()
    site.config_path.write_text(
        text.replace(
            'name = "harvard-2026"\n', f'name = "harvard-2026"\nalerts_channel = {ALERTS}\n', 1
        )
    )
    opt_in(site, OPT_IN, site.clock.now - 3600)
    return site


@pytest.fixture
async def server(site):
    srv = site.server()
    await srv.start()
    yield srv
    await srv.close()


def opt_in(site, text, mtime):
    path = Path(site.paths.layout.waker) / "config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def status(site, ready, written=None, **entry):
    """The status file, as `zab.waker.__main__.report` writes it; `ready` None leaves the key
    out, as a waker of an earlier release does."""
    shown = {"name": "harvard-2026", "checked_at": written, "bots": []} | entry
    if ready is not None:
        shown["ready_at"] = ready
    path = Path(site.paths.waker_status)
    path.parent.mkdir(exist_ok=True)
    written = site.clock.now if written is None else written
    path.write_text(json.dumps({"written_at": written, "workshops": [shown]}))


def state(site, workshop="harvard-2026"):
    config = f"{site.paths.layout.waker}/config.toml"
    return wakerdown.state(site.paths.waker_status, config, workshop, site.clock.now)


def alerts(site):
    return [p for p in site.realm.posts(HARVARD) if p.get("topic") == wakerdown.TOPIC]


def records(site):
    text = site.audit.read_text() if site.audit.exists() else ""
    rows = [json.loads(line) for line in text.splitlines()]
    return [r for r in rows if r["event"] == "waker"]


def test_down_means_no_ready_at_for_a_few_minutes(site):
    """`ready_at` moves only while a workshop's waker has run a minute: a waker killed at every
    start (Nov 1) rewrites the file, `written_at` fresh, but not that."""
    now = site.clock.now
    status(site, now - 60)
    assert state(site) == (UP, None)
    status(site, now - DOWN_AFTER - 1)  # written now, by a waker that never stays up
    assert state(site) == (DOWN, now - DOWN_AFTER - 1)
    status(site, now - DOWN_AFTER - 1, written=now - DOWN_AFTER - 1)  # no waker at all
    assert state(site) == (DOWN, now - DOWN_AFTER - 1)
    assert state(site, "zab-test") == (OFF, None)  # not opted in: nothing to say
    status(site, None, written=now - 30)  # an earlier release's file: its write stands in
    assert state(site) == (UP, None)
    status(site, None, written=now - DOWN_AFTER - 30)
    assert state(site) == (DOWN, now - DOWN_AFTER - 30)
    status(site, "damaged")  # its ready_at unusable: the config's write, an hour ago
    assert state(site) == (DOWN, now - 3600)
    Path(site.paths.waker_status).unlink()  # no file: the config's write
    assert state(site) == (DOWN, now - 3600)
    opt_in(site, OPT_IN, now - 60)  # just opted in: not yet
    assert state(site) == (UP, None)
    Path(site.paths.layout.waker, "config.toml").write_text("not = [toml")
    assert state(site) == ("", None)  # the config can't be read: nothing can be said


def test_status_and_the_console_say_since_when(site):
    now = site.clock.now
    config = f"{site.paths.layout.waker}/config.toml"
    status(site, now - 60)
    assert wakerdown.status_line(site.paths.waker_status, config, "harvard-2026", now) is None
    status(site, 1_799_990_000.0)
    line = wakerdown.status_line(site.paths.waker_status, config, "harvard-2026", now)
    assert line.startswith("⚠ waker down since 2027-01-15 05:13 UTC: ") and "`!start" in line
    ws = {"waker_down": "2027-01-15T05:13:20Z", "vms": []}
    shown = pages.workshop("harvard-2026", ws, [], now, user="olga")
    assert "waker down since 2027-01-15 05:13 UTC" in shown
    index = pages.index({"at": now, "workshops": {"harvard-2026": ws}}, ["harvard-2026"], now,
                        user="olga")  # fmt: skip
    assert "harvard-2026: waker down since 2027-01-15 05:13 UTC" in index
    assert "waker down" not in pages.workshop("harvard-2026", {"vms": []}, [], now, user="olga")


async def test_the_alert_goes_out_once_and_again_when_the_waker_is_back(site, server):
    """Once in the workshop's alerts channel, by its bot, and its record; nothing more while it
    stays down, a restart of the bots included; then "back", once."""
    since = site.clock.now - DOWN_AFTER - 60
    status(site, since)
    await server.waker.check()
    (alert,) = alerts(site)
    assert alert["to"] == str(ALERTS)
    assert "**waker down since 2027-01-15 07:54 UTC:**" in alert["content"]
    assert "harvard-2026" in alert["content"] and "ADMIN §1.8" in alert["content"]
    (record,) = records(site)
    assert (record["workshop"], record["state"], record["since"]) == ("harvard-2026", DOWN, since)
    assert type(record["alert"]) is int
    await server.waker.check()
    await server.close()
    restarted = site.server()
    await restarted.start()
    try:
        await restarted.waker.check()
        assert len(alerts(site)) == 1 and len(records(site)) == 1
        status(site, site.clock.now)
        await restarted.waker.check()
        back = alerts(site)[-1]
        assert back["content"].startswith("✅ **waker back** at 2027-01-15 08:00 UTC, down since ")
        assert [r["state"] for r in records(site)] == [DOWN, UP]
        await restarted.waker.check()
        assert len(alerts(site)) == 2
    finally:
        await restarted.close()


async def test_a_refused_alert_is_posted_again_and_an_opt_out_ends_it(site, server, monkeypatch):
    """A post Zulip surely never took (a 4xx) is made again on the next pass; then the waker,
    opted out while down, gets no "back", and a record ends the alert."""
    status(site, site.clock.now - DOWN_AFTER - 60)
    bot = next(b for b in server.bots if b.workshop and b.workshop.name == "harvard-2026")
    deliver, refused = bot.deliver, []

    async def refusing(**params):
        if not refused:
            refused.append(params)
            return None, True  # a 4xx: it never landed
        return await deliver(**params)

    monkeypatch.setattr(bot, "deliver", refusing)
    await server.waker.check()
    assert refused and alerts(site) == [] and records(site) == []
    await server.waker.check()
    assert len(alerts(site)) == 1
    opt_in(site, OPT_IN.replace("harvard-2026", "zab-test"), site.clock.now)  # opted out
    await server.waker.check()
    assert len(alerts(site)) == 1 and [r["state"] for r in records(site)] == [DOWN, OFF]


async def test_an_alert_that_may_have_landed_is_not_posted_twice(site, server):
    """A 5xx may hide a post that landed (`AdminBot.deliver`): the alert counts as made, its
    record has no message ID, and the next pass posts nothing more."""
    status(site, site.clock.now - DOWN_AFTER - 60)
    site.realm.broken.add(HARVARD)
    await server.waker.check()
    (record,) = records(site)
    assert (record["state"], record["alert"]) == (DOWN, None)
    site.realm.broken.clear()
    await server.waker.check()
    assert alerts(site) == [] and len(records(site)) == 1


async def test_an_alert_cut_short_is_recorded_and_never_posted_again(site, server, monkeypatch):
    """Codex round 3 on #732: a post cancelled before Zulip's answer (the bots' stop) may have
    landed. Its record goes first, with no message ID, and the next pass posts nothing; the
    pass itself has no timeout to cut a slow post short."""
    status(site, site.clock.now - DOWN_AFTER - 60)
    bot = next(b for b in server.bots if b.workshop and b.workshop.name == "harvard-2026")
    started = asyncio.Event()

    async def slow(**params):
        started.set()
        await asyncio.Event().wait()  # Zulip took it; its answer never comes

    monkeypatch.setattr(bot, "deliver", slow)
    task = asyncio.create_task(server.waker.check())
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    (record,) = records(site)
    assert (record["state"], record["alert"]) == (DOWN, None)
    monkeypatch.undo()
    await server.waker.check()
    assert alerts(site) == [] and len(records(site)) == 1


async def test_a_workshop_without_an_alerts_channel_gets_no_alert(site, server):
    opt_in(site, OPT_IN.replace("harvard-2026", "zab-test"), site.clock.now - 3600)
    await server.waker.check()  # zab-test's waker is down, but it has no alerts channel
    assert site.realm.posts(HARVARD) == [] and records(site) == []
