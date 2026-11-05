"""The waker's catch-up (#403): what is sent while it restarts (a config change, a deploy, a
reboot) or while its queue is gone wakes the VM after it, through the live filter, and only
when the VM wasn't seen up after the message was sent. A fake Zulip, a fake EC2 (the starter's
answers have the same shape) and the waker's own store."""

import asyncio
import json
import logging
import time

import pytest

from zab.config import BotConfig
from zab.core.store import Store
from zab.testing.fake_zulip import FakeZulip
from zab.waker import queues, service
from zab.waker.service import Target, Waker
from zab.zulip.client import ZulipClient

from .test_service import EC2, INSTANCE

pytestmark = pytest.mark.integration
OWNER, STRANGER = 20, 99
MARK = f"{service.MARK}bot"


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr(service, "GRACE", 0.2)


@pytest.fixture
def fake():
    fake = FakeZulip()
    fake.poll_timeout = 0.05  # a long poll, so that idle listeners don't spin
    fake.members += [
        {"user_id": OWNER, "full_name": "Owner", "email": "o@example.test", "is_bot": False}
    ]
    return fake


class Run:
    """One waker process: its store, its Zulip client, its bot's listener."""

    def __init__(self, path, fake, ec2, opted_in=None):
        self.fake, self.store = fake, Store(path)
        self.client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
        bot = BotConfig(name="bot", allow_user_ids=[OWNER])
        self.target = Target(bot, INSTANCE, self.client)
        self.waker = Waker([self.target], ec2, self.store, 1, opted_in=opted_in)
        self.task = None

    async def start(self):
        """The first poll, then the listener, once its queue is registered."""
        await self.waker.refresh(force=True)
        queues = len(self.fake.queues)
        self.task = asyncio.create_task(self.waker.listen(self.target))
        await until(lambda: len(self.fake.queues) > queues)

    async def exit(self):
        """As the process ends: the listener, then the wakes in flight (`Waker.run`)."""
        tasks = [t for t in (self.task, *self.waker.tasks.values()) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()
        self.store.close()


async def until(predicate, within=3.0):
    deadline = time.monotonic() + within
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def starting(fake):
    return [m for m in fake.messages.values() if m["content"] == service.STARTING]


@pytest.mark.parametrize("before", ["marked", "newly opted in"])
async def test_a_message_sent_while_the_waker_restarts_wakes_the_vm_after_it(
    tmp_path, fake, before
):
    """A8 on the rig (#403): the owner's DM comes between the old waker's exit and the new
    one's registration. With a mark from the run before, or none: the bot was just opted in,
    and the config's write, before the DM, is where its catch-up starts."""
    ec2 = EC2()
    written = time.time()
    if before == "marked":
        first = Run(tmp_path, fake, ec2)
        await first.start()
        hello = fake.post_as(STRANGER, "a stranger's message", "wake")  # handled live
        await until(lambda: first.waker.saved.get("bot") == hello["id"])
        await first.exit()
    dm = fake.post_as(OWNER, "!status")  # in the gap: no queue sees it
    second = Run(tmp_path, fake, ec2, opted_in=written)
    assert second.waker.since(second.target)[0] == (hello["id"] if before == "marked" else None)
    await second.start()
    await until(lambda: starting(fake))
    await asyncio.gather(*second.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    await until(lambda: second.waker.saved.get("bot", 0) >= dm["id"])  # past it, once handled
    await second.exit()


@pytest.mark.parametrize("up", service.UP)
async def test_a_message_sent_before_the_vm_stopped_wakes_nothing(tmp_path, fake, up):
    """The owner's message reached the VM, running or starting; the waker saw it so after that,
    then stopped. The next run catches up on the message (its mark was before it): stale, no
    start. The next message, sent while the VM is stopped, wakes it."""
    assert set(service.UP) == {"pending", "running"}
    ec2 = EC2()
    ec2.state = up
    answered = fake.post_as(OWNER, "!status")
    answered["timestamp"] = int(time.time()) - 60  # a minute ago, in Zulip's whole seconds
    first = Run(tmp_path, fake, ec2)
    first.store.set(MARK, answered["id"] - 1)  # the old waker had not handled it yet
    await first.waker.refresh(force=True)  # a poll: running, after the message
    ec2.state = "stopped"
    await first.waker.refresh(force=True)  # the last poll: stopped
    await first.exit()
    second = Run(tmp_path, fake, ec2)
    assert second.waker.since(second.target) == (answered["id"] - 1, None)
    await second.start()
    await until(lambda: second.waker.saved.get("bot") == answered["id"])
    assert not ec2.starts and not starting(fake)
    fresh = fake.post_as(OWNER, "!status")
    await until(lambda: starting(fake))
    await asyncio.gather(*second.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    await until(lambda: second.waker.saved.get("bot", 0) >= fresh["id"])
    await second.exit()


async def test_a_queue_zulip_dropped_is_caught_up_on_registered_again(tmp_path, fake, monkeypatch):
    """#295's gap, in one run: what was sent while the queue was gone comes after the new
    registration, through the same rules: the message the VM answered before it stopped is
    stale, the one sent after the stop wakes it."""
    sleep = asyncio.sleep
    monkeypatch.setattr(queues.asyncio, "sleep", lambda delay: sleep(min(delay, 0.01)))
    ec2 = EC2()
    ec2.state = "running"
    run = Run(tmp_path, fake, ec2)
    await run.start()
    hello = fake.post_as(STRANGER, "hello", "wake")
    await until(lambda: run.waker.seen.get("bot") == hello["id"])
    gate, request = asyncio.Event(), run.client.request

    async def held(method, endpoint, **params):
        if endpoint == "register":
            await gate.wait()  # registered again only when the test says
        return await request(method, endpoint, **params)

    run.client.request = held
    fake.lose_queues()
    answered = fake.post_as(OWNER, "@**bot** answered before the stop", "wake")
    answered["timestamp"] = int(time.time()) - 60
    await run.waker.refresh(force=True)  # running, after it
    ec2.state = "stopped"
    await run.waker.refresh(force=True)
    await run.waker.reconsider()
    fresh = fake.post_as(OWNER, "@**bot** sent after the stop", "wake")
    gate.set()
    await until(lambda: fake.messages[fresh["id"]]["reactions"])
    await asyncio.gather(*run.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    assert not fake.messages[answered["id"]]["reactions"]  # stale: no ⏰
    await run.exit()


@pytest.mark.parametrize("behind", [0, 5, int(service.SKEW)])
async def test_a_message_sent_as_the_vm_stops_waits_for_the_next_poll(tmp_path, fake, behind):
    """Codex round 1: the VM is seen running at a fractional time, then stops, and the owner
    writes in the same second. Zulip's timestamp is that whole second, or less when its clock is
    `behind`, up to `SKEW`: the message isn't stale, it waits, and the next poll wakes the VM. A
    message sent more than `SKEW` and a second before the sighting is stale."""
    ec2 = EC2()
    ec2.state = "running"
    run = Run(tmp_path, fake, ec2)
    await run.waker.refresh(force=True)
    seen = run.waker.up[INSTANCE]
    message = fake.post_as(OWNER, "@**bot** sent as it stops", "wake")
    message["timestamp"] = int(seen) - behind
    old = fake.post_as(OWNER, "@**bot** answered long before", "wake")
    old["timestamp"] = int(seen - service.SKEW) - 2
    for m in (message, old):
        await run.waker.message(run.target, m)
    assert list(run.waker.waiting[INSTANCE]) == [message["id"]]
    ec2.state = "stopped"
    await run.waker.refresh(force=True)
    await run.waker.reconsider()
    await asyncio.gather(*run.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    assert fake.messages[message["id"]]["reactions"] and not old["reactions"]
    await run.exit()


async def test_a_wake_cut_short_by_the_exit_is_caught_up_after_it(tmp_path, fake):
    """The DM came while the VM was stopping, and its wake waited for the stop when the waker
    exited: the mark stayed before it, so the next run wakes the VM for it."""
    ec2 = EC2()
    ec2.state = "stopping"
    first = Run(tmp_path, fake, ec2)
    await first.waker.refresh(force=True)
    dm = fake.post_as(OWNER, "!status")
    await first.waker.message(first.target, dm)
    assert INSTANCE in first.waker.tasks  # its wake waits for the stop
    assert first.store.get(MARK) == dm["id"] - 1
    await first.exit()
    ec2.state = "stopped"
    second = Run(tmp_path, fake, ec2)
    await second.start()
    await until(lambda: starting(fake))
    await asyncio.gather(*second.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    await second.exit()


async def test_the_mark_waits_for_a_message_until_it_is_handled(tmp_path, fake, monkeypatch):
    """A message that waits for the next poll (the cache said running) holds the mark back
    until the poll drops it, and the poll stores it; the mark stored survives the restart."""
    ec2 = EC2()
    ec2.state = "running"
    run = Run(tmp_path, fake, ec2)
    await run.waker.refresh(force=True)
    message = fake.post_as(OWNER, "!status")
    await run.waker.message(run.target, message)
    assert run.store.get(MARK) == message["id"] - 1
    polled = asyncio.Event()

    async def after_one_pass(delay):  # `poll_states`' wait after its pass
        polled.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.asyncio, "sleep", after_one_pass)
    poll = asyncio.create_task(run.waker.poll_states())
    await polled.wait()
    poll.cancel()
    await asyncio.gather(poll, return_exceptions=True)
    monkeypatch.undo()
    assert run.store.get(MARK) == message["id"]
    await run.exit()
    again = Run(tmp_path, fake, ec2)
    assert again.waker.since(again.target) == (message["id"], None)
    await again.exit()


async def test_a_bot_opted_in_again_catches_up_from_the_config_write(tmp_path, fake):
    """Reviewer on #405: the bot is opted out, and the waker's next start, which doesn't list
    it, forgets its mark. Its owner writes, then the bot is opted in again: that message wakes
    nothing, while one sent after the new config's write, before the registration, does."""
    ec2 = EC2()
    first = Run(tmp_path, fake, ec2)
    await first.start()
    hello = fake.post_as(STRANGER, "hello", "wake")
    await until(lambda: first.waker.saved.get("bot") == hello["id"])
    await first.exit()
    store = Store(tmp_path)
    service.forget(store, {"another-bot"})  # `waker run` without it
    assert store.get(MARK) is None
    store.close()
    written = time.time()  # `!waker on`, after the owner wrote
    out = fake.post_as(OWNER, "@**bot** sent while opted out", "wake")
    out["timestamp"] = int(written) - 60
    gap = fake.post_as(OWNER, "@**bot** sent after the opt-in", "wake")
    again = Run(tmp_path, fake, ec2, opted_in=written)
    await again.start()
    await until(lambda: fake.messages[gap["id"]]["reactions"])
    await asyncio.gather(*again.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}] and not out["reactions"]
    await again.exit()


async def test_a_bot_name_used_again_starts_afresh(tmp_path, fake):
    """A mark past every message the bot can see (left by another realm's bot of that name, or
    damaged) counts as none: the catch-up starts from the config's write. `forget` keeps the
    marks of the bots the config lists, and only those."""
    store = Store(tmp_path)
    store.set(MARK, 10**9)
    store.set(f"{service.MARK}gone", 5)
    service.forget(store, {"bot"})
    assert store.get(MARK) == 10**9 and store.get(f"{service.MARK}gone") is None
    store.close()
    ec2 = EC2()
    written = time.time()
    gap = fake.post_as(OWNER, "@**bot** hello", "wake")
    run = Run(tmp_path, fake, ec2, opted_in=written)
    await run.start()
    await until(lambda: fake.messages[gap["id"]]["reactions"])
    await asyncio.gather(*run.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    await run.exit()


async def test_a_failed_catch_up_is_tried_again_and_holds_the_mark(tmp_path, fake, monkeypatch):
    """The first fetch fails; a live message comes meanwhile; the stored mark stays where the
    catch-up started, so that a restart would fetch again. The next try delivers the missed DM,
    which wakes the VM, but not the live message again; then the mark moves on."""
    monkeypatch.setattr(queues, "CATCH_UP_RETRY", 0.01)
    ec2 = EC2()
    hello = fake.post_as(STRANGER, "hello", "wake")
    store = Store(tmp_path)
    store.set(MARK, hello["id"])
    store.close()
    missed = fake.post_as(OWNER, "!status")  # in the gap
    run = Run(tmp_path, fake, ec2)
    gate, calls, request = asyncio.Event(), [], run.client.request

    async def flaky(method, endpoint, **params):
        if (method, endpoint) == ("GET", "messages"):
            calls.append(endpoint)
            if len(calls) == 1:
                raise OSError("timeout")
            await gate.wait()
        return await request(method, endpoint, **params)

    run.client.request = flaky
    delivered, message = [], run.waker.message

    async def counting(target, m, flags=()):
        delivered.append(m["id"])
        await message(target, m, flags)

    run.waker.message = counting
    await run.start()
    live = fake.post_as(STRANGER, "live", "wake")
    await until(lambda: live["id"] in delivered and len(calls) == 2)
    assert run.store.get(MARK) == hello["id"]  # held while the catch-up is due
    gate.set()
    await until(lambda: starting(fake))
    await asyncio.gather(*run.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    assert delivered.count(missed["id"]) == 1 and delivered.count(live["id"]) == 1
    await until(lambda: run.waker.saved.get("bot", 0) >= live["id"])
    await run.exit()


async def test_a_catch_up_that_keeps_failing_is_given_up(tmp_path, fake, monkeypatch, caplog):
    monkeypatch.setattr(queues, "CATCH_UP_RETRY", 0.01)
    ec2 = EC2()
    store = Store(tmp_path)
    store.set(MARK, 0)
    store.close()
    fake.post_as(STRANGER, "after the mark", "wake")  # something to fetch (#717)
    run = Run(tmp_path, fake, ec2)
    request = run.client.request

    async def failing(method, endpoint, **params):
        if (method, endpoint) == ("GET", "messages"):
            raise OSError("timeout")
        return await request(method, endpoint, **params)

    run.client.request = failing
    await run.start()
    await until(lambda: f"given up after {queues.CATCH_UP_TRIES} tries" in caplog.text)
    assert caplog.text.count("Waker catch-up failed") == queues.CATCH_UP_TRIES
    live = fake.post_as(STRANGER, "live", "wake")
    await until(lambda: run.waker.saved.get("bot") == live["id"])  # the mark moves on
    await run.exit()


async def test_the_start_request_counts_as_a_sighting(tmp_path, fake):
    """Once the waker asks for the VM's start, a message sent well before (as a catch-up could
    bring again) is stale, before any poll sees the VM up: a start that failed is left at that,
    until a message sent since."""
    ec2 = EC2()
    run = Run(tmp_path, fake, ec2)
    await run.waker.refresh(force=True)
    before = int(time.time())
    await run.waker.message(run.target, fake.post_as(OWNER, "@**bot** wake up", "wake"))
    await asyncio.gather(*run.waker.tasks.values())
    ec2.state = run.waker.states[INSTANCE] = "stopped"  # the start failed; no poll since
    run.store.set(f"wake:{INSTANCE}", 0)  # and the minute passed
    older = fake.post_as(OWNER, "@**bot** sent before the start", "wake")
    older["timestamp"] = before - int(service.SKEW) - 2
    await run.waker.message(run.target, older)
    await asyncio.gather(*run.waker.tasks.values())
    assert len(ec2.starts) == 1 and not older["reactions"]
    await run.exit()


async def test_times_in_the_future_never_block_a_wake(tmp_path, fake):
    """A clock set back since the store was written: a sighting or a start request in the
    future neither makes every message stale nor keeps the VM cooling down."""
    store = Store(tmp_path)
    later = time.time() + 3600
    store.set("waker_states", {"at": 1.0, "states": {}, "up": {INSTANCE: later}})
    store.set(f"wake:{INSTANCE}", later)
    store.close()
    ec2 = EC2()
    run = Run(tmp_path, fake, ec2)
    assert run.waker.up == {}
    await run.waker.refresh(force=True)
    await run.waker.message(run.target, fake.post_as(OWNER, "!status"))
    await asyncio.gather(*run.waker.tasks.values())
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    await run.exit()


async def test_no_mark_and_no_config_time_no_catch_up(tmp_path, fake):
    """A waker built without its config's mtime and with no mark catches up on nothing."""
    ec2 = EC2()
    fake.post_as(OWNER, "!status")
    run = Run(tmp_path, fake, ec2)
    await run.start()
    await until(lambda: any(path == "events" for _, path, _ in fake.requests))
    assert not any(path == "messages" and m == "GET" for m, path, _ in fake.requests)
    assert not ec2.starts
    await run.exit()


@pytest.mark.parametrize("value", ['"seven"', "-1", "true", "1.5", "{not json"])
async def test_a_damaged_mark_counts_as_none(tmp_path, fake, caplog, value):
    ec2 = EC2()
    store = Store(tmp_path)
    store.db.execute("INSERT INTO kv VALUES (?, ?)", (MARK, value))
    store.db.commit()
    store.close()
    run = Run(tmp_path, fake, ec2, opted_in=1000.0)
    assert run.waker.since(run.target) == (None, 1000.0 - service.SKEW)
    run.waker.settled(run.target)  # as the queue says once it has caught up
    assert "Ignoring a damaged catch-up mark for bot" in caplog.text
    await run.waker.refresh(force=True)
    message = fake.post_as(STRANGER, "hello", "wake")
    await run.waker.message(run.target, message)
    assert run.store.get(MARK) == message["id"]  # replaced by the next one
    await run.exit()


@pytest.mark.parametrize(
    ("snapshot", "up"),
    [
        ({"at": 5.0, "states": {INSTANCE: "running"}}, {INSTANCE: 5.0}),  # stored before #403
        ({"at": 9, "states": {INSTANCE: "stopped"}, "up": {INSTANCE: 7}}, {INSTANCE: 7.0}),
        ({"at": "x", "states": {INSTANCE: "running"}, "up": {INSTANCE: float("nan")}}, {}),
        ({"up": [1], "states": 3}, {}),
        ([1], {}),
        ("{not json", {}),
    ],
    ids=["old format", "up", "bad times", "bad shapes", "not a dict", "not JSON"],
)
async def test_the_states_snapshot_is_read_safely(tmp_path, fake, snapshot, up):
    """Per VM, when it was last seen up: from the snapshot `refresh` stores, or nothing."""
    text = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    store = Store(tmp_path)
    store.db.execute("INSERT INTO kv VALUES ('waker_states', ?)", (text,))
    store.db.commit()
    store.close()
    run = Run(tmp_path, fake, EC2())
    assert run.waker.up == up
    await run.exit()


# ----------------------------------------------------------------------- the queue's part


def recorder():
    got = []

    async def deliver(target, message, flags):
        got.append(message["id"])

    return got, deliver


def fetches(fake):
    return [p for m, path, p in fake.requests if (m, path) == ("GET", "messages")]


async def test_the_catch_up_walks_back_a_page_at_a_time(fake):
    """#717: from the newest message back, `PAGE` at a time, to the mark, or to the first
    message older than the floor (`CATCH_UP_AGE`, or the time without a usable mark: none, or
    one past the newest), the newest `CATCH_UP` at most, delivered oldest first; no request when
    the registration's newest message is the mark, nor with no mark and no time."""
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    now = time.time()
    old = [fake.post_as(OWNER, "old", "t") for _ in range(3)]
    ids = [fake.post_as(OWNER, f"m{i}", "t")["id"] for i in range(queues.CATCH_UP + 20)]
    for m in old:
        fake.messages[m["id"]]["timestamp"] = now - queues.CATCH_UP_AGE - 5
    for i, mid in enumerate(ids):
        fake.messages[mid]["timestamp"] = now - 100 + i / 10
    newest, got, deliver = ids[-1], *recorder()

    def asked():
        return [(p["anchor"], p.get("include_anchor"), p["num_before"], p["num_after"])
                for p in fetches(fake)]  # fmt: skip

    await queues.catch_up(target, deliver, (ids[-3], None), set(), newest)
    assert got == ids[-2:] and asked() == [("newest", None, str(queues.PAGE), "0")]
    fake.requests.clear()
    got.clear()  # nothing since the mark: no request
    await queues.catch_up(target, deliver, (newest, None), set(), newest)
    assert got == [] and not fetches(fake)
    caught: set[int] = set()  # far behind: the newest CATCH_UP, a page per request
    await queues.catch_up(target, deliver, (0, None), caught, newest)
    assert got == ids[-queues.CATCH_UP :] and caught == set(got)
    size, pages = str(queues.PAGE), queues.CATCH_UP // queues.PAGE
    anchors = [ids[-queues.PAGE * n] for n in range(1, pages)]
    assert asked() == [("newest", None, size, "0")] + [
        (str(a), "false", size, "0") for a in anchors
    ]
    got.clear()  # no mark: from the time, back to it
    await queues.catch_up(target, deliver, (None, now - 100 + 115 / 10), set(), newest)
    assert got == ids[115:]
    got.clear()  # a mark past the newest message (another realm's): from the time, if any
    await queues.catch_up(target, deliver, (10**9, None), set(), newest)
    await queues.catch_up(target, deliver, (10**9, now - 100 + 115 / 10), set(), newest)
    assert got == ids[115:]
    got.clear()  # what `caught` holds already is skipped
    await queues.catch_up(target, deliver, (ids[-4], None), set(ids[-2:]), newest)
    assert got == ids[-3:-2]
    fake.requests.clear()
    got.clear()  # Zulip didn't say its newest: one page, back to the mark
    await queues.catch_up(target, deliver, (newest, None), set())
    assert got == [] and len(fetches(fake)) == 1
    for mid in ids:
        fake.messages[mid]["timestamp"] = now - queues.CATCH_UP_AGE - 1
    fake.requests.clear()  # all older than the floor: the first page says so
    await queues.catch_up(target, deliver, (0, None), set(), newest)
    assert got == [] and len(fetches(fake)) == 1
    fake.requests.clear()
    await queues.catch_up(target, deliver, (None, None), set(), newest)
    assert got == [] and not fake.requests
    assert queues.PAGE <= queues.CATCH_UP <= service.HOLD  # what a VM holds
    await client.close()


async def test_a_long_outage_misses_nothing_recent(fake):
    """#717, the review's case: 130 messages since the mark, all within `CATCH_UP_AGE`, the
    owner's the 105th: it is delivered, with the rest of the newest `CATCH_UP`, oldest first,
    each with only what the waker reads (`KEPT`): no text."""
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    mark = fake.post_as(STRANGER, "before the outage", "t")["id"]
    ids = [fake.post_as(OWNER if i == 104 else STRANGER, f"m{i}", "t")["id"] for i in range(130)]
    seen = []

    async def deliver(target, message, flags):
        seen.append(message)

    await queues.catch_up(target, deliver, (mark, None), set(), ids[-1])
    assert [m["id"] for m in seen] == ids[-queues.CATCH_UP :] and ids[104] in ids[-100:]
    assert {key for m in seen for key in m} <= {*queues.KEPT, "typed"} and "content" not in seen[0]
    await client.close()


async def test_a_refused_narrow_is_filtered_here(fake, caplog):
    """#717: a server that refuses the negated `sender` narrow (`BAD_NARROW`) gets the request
    again without it, and the VM's bots' messages are left out here, by their address; the
    next pages, and the next catch-ups of that bot, ask without it."""
    fake.refuse_negated = True
    fake.members.append({"user_id": 30, "email": "twin@example.test", "is_bot": True})
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    mark = fake.post_as(OWNER, "before", "t")["id"]
    fake.post_as(30, "the other bot's", "t")
    person = fake.post_as(OWNER, "!status", "t")
    got, deliver = recorder()
    senders = queues.Senders(["bot", "twin@example.test"])
    await queues.catch_up(target, deliver, (mark, None), set(), person["id"], senders)
    assert got == [person["id"]] and senders.narrow is False
    assert caplog.text.count("narrow refused") == 1
    first = len(fetches(fake))
    await queues.catch_up(target, deliver, (mark, None), set(), person["id"], senders)
    assert len(fetches(fake)) == first + 1 and "narrow" not in fetches(fake)[-1]
    await client.close()


async def test_a_turn_that_hangs_gives_its_turn_up(fake, monkeypatch, caplog):
    """#717: a registration or a catch-up that takes more than `TURN` seconds ends, so that a
    hung request can't hold a turn: the registration is tried again, the catch-up counts as a
    failed try. Each turn's time, and the start's once every bot has settled, are logged."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(queues, "TURN", 0.1)
    monkeypatch.setattr(queues, "CATCH_UP_RETRY", 0.01)
    sleep = asyncio.sleep

    async def soon(seconds):
        await sleep(min(seconds, 0.01))

    monkeypatch.setattr(queues.asyncio, "sleep", soon)
    fake.poll_timeout = 0.05
    fake.post_as(OWNER, "!status")
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    calls, request = [], client.request

    async def hanging(method, endpoint, **params):
        calls.append(endpoint)
        if calls.count(endpoint) == 1 and endpoint in ("register", "messages"):
            await asyncio.Event().wait()  # the first of each hangs
        return await request(method, endpoint, **params)

    client.request = hanging
    got, deliver = recorder()
    source = queues.BotQueues([target], lambda t: (0, None))
    task = asyncio.create_task(source.run(deliver))
    await until(lambda: "Waker start: 1 bots caught up in" in caplog.text)
    assert "Waker listener retry (TimeoutError)" in caplog.text
    assert "Waker catch-up failed (TimeoutError): trying again" in caplog.text
    assert calls.count("register") == 2 and calls.count("messages") == 2 and len(got) == 1
    assert caplog.text.count("Waker turn of bot: ") == 2
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.close()


async def test_the_catch_up_leaves_out_the_vms_own_bots(fake):
    """#717: the messages of the VM's bots (the bridge's status messages, with every edit) wake
    nothing and are not fetched; a person's are. `BotQueues` names them by their sign-in
    addresses, the bot's own included, and no other VM's."""
    fake.members.append({"user_id": 30, "email": "twin@example.test", "is_bot": True})
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    mark = fake.post_as(OWNER, "before", "t")["id"]
    fake.post_as(fake.user["user_id"], "its own status message", "t")
    fake.post_as(30, "the other bot's", "t")
    person = fake.post_as(OWNER, "!status", "t")
    got, deliver = recorder()
    senders = ["bot", "twin@example.test"]
    await queues.catch_up(target, deliver, (mark, None), set(), person["id"], senders)
    assert got == [person["id"]]
    assert json.loads(fetches(fake)[-1]["narrow"]) == [
        {"operator": "sender", "operand": email, "negated": True} for email in senders
    ]
    twin = ZulipClient(
        "https://zulip.example", "twin@example.test", "decoy", transport=fake.transport
    )
    far = ZulipClient(
        "https://zulip.example", "far@example.test", "decoy", transport=fake.transport
    )
    vm = [target, Target(BotConfig(name="twin"), INSTANCE, twin)]
    source = queues.BotQueues([*vm, Target(BotConfig(name="far"), "i-0000000000000002", far)])
    assert source.senders(target) == senders == source.senders(vm[1])
    for c in (client, twin, far):
        await c.close()


async def test_registrations_and_catch_ups_take_turns(fake, monkeypatch):
    """#717: at most `AT_ONCE` bots of the process register and catch up at once, those of
    every workshop (two `BotQueues` in one event loop share the bound), each catch-up's
    deliveries included; every bot gets its turn, and its queue then polls outside it. The
    registration asks Zulip for the bot's newest message."""
    monkeypatch.setattr(queues, "AT_ONCE", 2)
    fake.poll_timeout = 0.05
    fake.post_as(OWNER, "!status")
    inside, peak, done = set(), [0], set()

    def since(target):
        return (0, None)

    def settled(target):
        inside.discard(target.bot.name)
        done.add(target.bot.name)

    async def deliver(target, message, flags):
        assert target.bot.name in inside  # its catch-up delivers in its turn
        await asyncio.sleep(0.02)

    def bot(n):
        client = ZulipClient("https://zulip.example", f"b{n}", "decoy", transport=fake.transport)
        request = client.request

        async def turn(method, endpoint, **params):
            if endpoint == "register":
                inside.add(f"b{n}")
                peak[0] = max(peak[0], len(inside))
                await asyncio.sleep(0.02)
            return await request(method, endpoint, **params)

        client.request = turn
        return Target(BotConfig(name=f"b{n}"), f"i-{n:016x}", client)

    workshops = [[bot(n) for n in range(k, k + 3)] for k in (0, 3)]
    runs = [queues.BotQueues(ts, since, settled).run(deliver) for ts in workshops]
    tasks = [asyncio.create_task(r) for r in runs]
    await until(lambda: len(done) == 6)
    assert peak[0] == 2 and queues.gate() is queues.gate()
    await until(lambda: sum(path == "events" for _, path, _ in fake.requests) >= 6)
    registrations = [p for _, path, p in fake.requests if path == "register"]
    assert {p["fetch_event_types"] for p in registrations} == {'["realm", "message"]'}
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for targets in workshops:
        for target in targets:
            await target.client.close()


async def test_a_turn_has_one_deadline_for_its_registration_and_catch_up(fake, monkeypatch):
    """The review of #727: a slow registration leaves its catch-up only what is left of the
    turn's `TURN` seconds, so that one turn never holds its slot for two of them."""
    monkeypatch.setattr(queues, "TURN", 0.2)
    monkeypatch.setattr(queues, "CATCH_UP_RETRY", 0.01)
    fake.poll_timeout = 0.05
    fake.post_as(OWNER, "!status")
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    request, slow = client.request, {"register": 0.15, "messages": 0.1}

    async def slowly(method, endpoint, **params):
        if (delay := slow.pop(endpoint, 0)) and endpoint in ("register", "messages"):
            await asyncio.sleep(delay)  # each alone within TURN, together past it
        return await request(method, endpoint, **params)

    client.request = slowly
    failed, resume = [], queues.resume

    async def counting(*args, **kwargs):
        feed = args[2]
        tries = feed.tries
        await resume(*args, **kwargs)
        failed.append(feed.tries > tries)

    monkeypatch.setattr(queues, "resume", counting)
    got, deliver = recorder()
    task = asyncio.create_task(queues.listen(target, deliver, lambda t: (0, None)))
    await until(lambda: len(got) == 1)
    assert failed[0] is True and failed[-1] is False  # the first turn's catch-up ran out
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.close()


async def test_a_message_both_caught_up_and_queued_is_delivered_once(fake):
    """The owner's DM comes after the registration, before the catch-up's fetch: both have it,
    and it is delivered once; the queue's later messages still come."""
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    posted = []
    request = client.request

    async def racing(method, endpoint, **params):
        if (method, endpoint) == ("GET", "messages") and not posted:
            posted.append(fake.post_as(OWNER, "!status"))
        return await request(method, endpoint, **params)

    client.request = racing
    got, deliver = recorder()
    fake.post_as(STRANGER, "after the mark", "t")  # something to fetch (#717)
    task = asyncio.create_task(queues.listen(target, deliver, lambda t: (0, None)))
    await until(lambda: posted and posted[0]["id"] in got)
    later = fake.post_as(OWNER, "!status")
    await until(lambda: later["id"] in got)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert got.count(posted[0]["id"]) == 1 and got.count(later["id"]) == 1
    await client.close()


async def test_a_failed_catch_up_leaves_the_queue_running(fake, caplog):
    caplog.set_level(logging.WARNING)
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot"), INSTANCE, client)
    fake.post_as(OWNER, "!status")
    request = client.request

    async def failing(method, endpoint, **params):
        if (method, endpoint) == ("GET", "messages"):
            raise OSError("boom")
        return await request(method, endpoint, **params)

    client.request = failing
    got, deliver = recorder()
    task = asyncio.create_task(queues.listen(target, deliver, lambda t: (0, None)))
    await until(lambda: "Waker catch-up failed (OSError)" in caplog.text)
    live = fake.post_as(OWNER, "!status")
    await until(lambda: got == [live["id"]])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.close()
