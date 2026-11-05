"""The acknowledgement (#466; docs/ADMIN.md §2.1, docs/ORGANIZERS.md §2.1): every command a bot
takes gets its 👀 at once, before the command's work, through the real bot, planner and runner
(scripts faked as `test_admin_runner` fakes them) against the fake realm; nothing else gets one;
and a reaction that fails, hangs or finds the request budget short changes no command's outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sys
import time
from dataclasses import replace

import httpx
import pytest

from zab.admin.bot import ACK, AS_CODE, FOREIGN
from zab.admin.planning import Planning
from zab.admin.plans import PENDING
from zab.admin.runner import Runner

from .botkit import (
    CODEX_BOB,
    DREW,
    HARVARD,
    HARVARD_CH,
    OLGA,
    OSCAR,
    PAT,
    TEST_CH,
    incoming,
    make_site,
    until,
)
from .test_admin_runner import fake

EYES = ACK["emoji_code"]
#: A fake script waits while this file exists, under the site's tree.
HOLD = "while os.path.exists({!r}): time.sleep(0.02)\n"
STATUS = {"schema": 1, "vms": [], "summary": {}, "ec2_error": None}
VMS = {"alice": "i-1", "bob": "i-2"}


def made(tmp):
    site = make_site(tmp)
    layout = replace(site.paths.layout, release=str(tmp / "release"), python=sys.executable)
    site.paths = replace(site.paths, layout=layout)
    hold = HOLD.format(str(tmp / "hold"))
    fake(site, "admin/status.py", hold + f"print(json.dumps({STATUS!r}))")
    fake(site, "aws/power.py", hold + 'print("alice: would stop i-1")')
    return site


@pytest.fixture
def site(tmp_path):
    return made(tmp_path)


@contextlib.asynccontextmanager
async def running(site):
    """The whole server, its queues on the fake realm, with the real planner and runner."""
    srv = site.server()
    srv.planner = Planning(Runner(srv, vms=lambda fleet, names: {n: VMS[n] for n in names}))
    task = asyncio.create_task(srv.run())
    try:
        await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
        yield srv
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def acked(site, bot=HARVARD):
    """The messages `bot` reacted to, with the emoji, in order."""
    return [(mid, p["emoji_code"]) for who, mid, p in site.realm.reactions if who == bot]


def timeline(site, bot=HARVARD):
    """`bot`'s reactions and posts, in the order the realm got them."""
    sent = iter(params for who, params in site.realm.sent if who == bot)
    out = []
    for who, method, path in site.realm.requests:
        if who == bot and method == "POST" and path == "messages":
            out.append(("post", next(sent).get("topic")))
        elif who == bot and method == "POST" and path.endswith("/reactions"):
            out.append(("ack", int(path.split("/")[1])))
    return out


def posts(site, bot=HARVARD):
    return [p["content"] for p in site.realm.posts(bot)]


# --------------------------------------------------------------------------------- at once


async def test_a_command_is_acknowledged_at_once_while_its_work_waits(site):
    """AC1: `!status all` (its read) and `!stop alice` (its dry run and status read), both held
    by their scripts: each command carries the bot's 👀 within a second, with nothing posted
    yet. Then the answers come, and the reactions stay."""
    hold = site.tmp / "hold"
    hold.touch()
    async with running(site) as srv:
        status = site.realm.say(OLGA, HARVARD_CH, "!status all", topic="status")
        await until(lambda: acked(site) == [(status, EYES)], 1.0)
        stop = site.realm.say(OLGA, HARVARD_CH, "!stop alice", topic="stop")
        await until(lambda: acked(site) == [(status, EYES), (stop, EYES)], 1.0)
        await until(lambda: "harvard-2026" in srv.planner.reading)  # the read is under way
        assert posts(site) == []  # nothing answered: neither the read nor the plan is done
        hold.unlink()
        await until(lambda: len(posts(site)) == 3)  # the status, the plan, its approval message
        assert srv.store.get(1).state == PENDING
    said = timeline(site)
    assert said[:2] == [("ack", status), ("ack", stop)]
    assert sorted(said[2:]) == [("post", "status"), ("post", "stop"), ("post", "stop")]
    assert site.realm.reactions[0] == (HARVARD, status, ACK)  # its name, code and type


async def test_a_read_is_acknowledged_before_its_answer(site):
    """A read the bot answers itself (`reads`), and a DM: the 👀 goes out before the answer."""
    async with running(site):
        roster = site.realm.say(OLGA, HARVARD_CH, "!roster show", topic="roster")
        await until(lambda: len(posts(site)) == 1)
        dm = site.realm.dm(DREW, "!help", HARVARD)
        await until(lambda: len(posts(site)) == 2)
        await until(lambda: len(acked(site)) == 2)
    assert timeline(site)[:2] == [("ack", roster), ("post", "roster")]
    assert acked(site) == [(roster, EYES), (dm, EYES)]


# ------------------------------------------------------------------------ only what it takes


async def test_only_a_command_the_bot_takes_gets_one(site):
    """No reaction for whom the bot may not obey, where it doesn't read, or what it answers at
    once as not a command it takes (ADMIN §2.1)."""
    srv = site.server()
    await srv.start()
    harvard = next(b for b in srv.bots if b.user_id == HARVARD)
    nothing = [
        incoming(PAT, "!status all", mid=1),  # a person who may not use the bot: 🚫
        incoming(CODEX_BOB, "!stop all", mid=2),  # a participant's bot: ⚠
        incoming(OLGA, "!help", stream=TEST_CH, mid=3),  # another bot's channel
        incoming(OLGA, "!help", stream=None, to=(HARVARD, OSCAR), mid=4),  # a group DM
        incoming(HARVARD, "!help", mid=5),  # its own
        incoming(OLGA, "@**Workshop admin · harvard-2026** !help", mid=6),  # not mentioned
        incoming(OLGA, "@**Codex · bob** !approve P-1 abcdef", mid=7),  # a click elsewhere
        incoming(OLGA, "hello", mid=8),  # talk
        incoming(OLGA, "!stop alice --yes", mid=9),  # the grammar's ❌, at once
        incoming(OLGA, "!status erin", mid=10),  # another workshop's slug: the hint, at once
        incoming(OLGA, "`!stop alice`", mid=15),  # a command sent as code: its hint (#546)
    ]
    taken = [
        incoming(OLGA, "!help", mid=11),
        incoming(OLGA, "!help", stream=None, to=(HARVARD,), mid=12),
        incoming(DREW, "!roster show", mid=13),  # a global admin
        incoming(OLGA, "!stop alice", mid=14),  # taken, though this server has no planner
    ]
    try:
        for m in [*nothing, *taken]:
            await harvard.on_message(m)
        await asyncio.gather(*harvard.acks)
    finally:
        await srv.close()
    assert acked(site) == [(m.id, EYES) for m in taken]
    said = posts(site)
    assert said[0].startswith("🚫 Pat Participant") and said[1] == FOREIGN
    assert AS_CODE in said
    assert said[-1] == "⚠ `!stop` is not available on this server yet"


# -------------------------------------------------------------------------- never in the way


def _answer(status, code):
    async def respond():
        return httpx.Response(status, json={"result": "error", "code": code, "msg": code})

    return respond


async def _no_connection():
    raise httpx.ConnectError("connection refused")


#: How a reaction fails: the realm's answer to it, or what it raises (`FakeRealm.reacting`).
FAILURES = {
    "refused": _answer(400, "BAD_REQUEST"),  # as for an emoji name the server lacks
    "server error": _answer(500, "INTERNAL_SERVER_ERROR"),
    "rate limited": _answer(429, "RATE_LIMIT_HIT"),
    "no connection": _no_connection,
    "hangs": None,  # never answers (`converse`)
}
COMMANDS = ("!roster show", "!status all", "!stop alice", "!help")


async def converse(site, failure):
    """Each of `COMMANDS`, answered before the next, with the reactions failing as `failure`
    says: what the bot posted, its plan's state, the audit's events, and the reactions still
    on their way when the last answer came."""
    gate = asyncio.Event()
    if failure == "hangs":

        async def hang():
            await gate.wait()

        site.realm.reacting = hang
    elif failure is not None:
        site.realm.reacting = FAILURES[failure]
    async with running(site) as srv:
        harvard = next(b for b in srv.bots if b.user_id == HARVARD)
        for n, text in enumerate(COMMANDS):
            site.realm.say(OLGA, HARVARD_CH, text, topic=text)
            await until(lambda n=n: len(posts(site)) == n + 1 + (n >= 2))  # a plan: 2 posts
        pending = len(harvard.acks)
        state = srv.store.get(1).state
    gate.set()
    events = [(r["event"], r.get("verb")) for r in records(site)]
    return [normal(site, text) for text in posts(site)], state, events, pending


def records(site):
    return [json.loads(line) for line in site.audit.read_text().splitlines()]


def normal(site, text):
    """A post without what differs between two sites: their tree, the chain, the plan's hash."""
    text = text.replace(str(site.tmp), "<tmp>")
    text = re.sub(r"`audit #\d+ · chain [0-9a-f]{12}`", "<footer>", text)
    return re.sub(r"(?<![0-9a-f])([0-9a-f]{12}|[0-9a-f]{6})(?![0-9a-f])", "<hash>", text)


async def test_a_failed_reaction_changes_no_outcome(tmp_path_factory, caplog):
    """AC2: whether a reaction is refused, fails, is rate limited, can't connect or never
    answers, every command's answers, its plan and its records are those of a run whose
    reactions all succeed; a failure is logged, never raised, and nobody waits for a hung
    one."""
    caplog.set_level(logging.INFO, "zab.admin.bot")
    ok = made(tmp_path_factory.mktemp("ok"))
    expected = await converse(ok, None)
    assert expected[3] == 0 and len(acked(ok)) == len(COMMANDS)
    for failure in FAILURES:
        caplog.clear()
        site = made(tmp_path_factory.mktemp(failure.replace(" ", "-")))
        said, state, events, pending = await converse(site, failure)
        assert acked(site) == [], failure
        assert (said, state, events) == expected[:3], failure
        failed = [r.getMessage() for r in caplog.records if "a reaction failed" in r.getMessage()]
        if failure == "hangs":
            assert pending == len(COMMANDS) and failed == []  # all on their way: none waited
        else:
            assert pending == 0 and len(failed) == len(COMMANDS), (failure, failed)


async def test_no_reaction_from_the_budget_answers_need(site, caplog):
    """The reaction is sent only while the budget keeps its reserve (SPEC §4.10): with 99 of
    150 requests in the window, it goes; at 100, the last third is the answers', and it is
    skipped; the answer goes at once either way."""
    caplog.set_level(logging.INFO, "zab.admin.bot")
    srv = site.server()
    await srv.start()
    harvard = next(b for b in srv.bots if b.user_id == HARVARD)
    budget = harvard.client.budget
    try:
        for n, used in enumerate((99, 100)):
            budget.sent.clear()
            budget.sent.extend([time.monotonic()] * used)
            started = time.monotonic()
            await harvard.on_message(incoming(OLGA, "!help", mid=20 + n))
            await asyncio.gather(*harvard.acks)
            assert time.monotonic() - started < 2 and len(posts(site)) == n + 1
    finally:
        await srv.close()
    assert acked(site) == [(20, EYES)]
    assert any("no reaction on 21" in r.getMessage() for r in caplog.records)
