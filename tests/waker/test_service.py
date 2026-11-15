import asyncio
import logging
import re
import threading
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest

from zab.config import BotConfig
from zab.core.store import Store
from zab.testing.fake_zulip import FakeZulip
from zab.waker import queues, service
from zab.waker.service import TAG, Target, Waker
from zab.zulip.client import ZulipClient, ZulipError

pytestmark = pytest.mark.integration
INSTANCE = "i-0123456789abcdef0"


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    """A message only one bot sees keeps its ⏰ waiting `GRACE` seconds for the other copy."""
    monkeypatch.setattr(service, "GRACE", 0.2)


class CapacityError(Exception):
    response: ClassVar = {"Error": {"Code": "InsufficientInstanceCapacity"}}


class EC2:
    def __init__(self):
        self.state, self.tags = "stopped", [TAG]
        self.starts = []
        self.failures = 0
        self.describes = 0

    def describe_instances(self, **kwargs):
        assert kwargs == {
            "Filters": [
                {"Name": "tag:workshop", "Values": [TAG["Value"]]},
                {"Name": "instance-id", "Values": [INSTANCE]},
            ]
        }
        self.describes += 1
        return {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": INSTANCE, "State": {"Name": self.state}, "Tags": self.tags}
                    ]
                }
            ]
        }

    def start_instances(self, **kwargs):
        self.starts.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise CapacityError()
        self.state = "pending"
        return {}


@pytest.fixture
async def setup(tmp_path):
    fake = FakeZulip()
    fake.members += [
        {"user_id": 20, "full_name": "Owner", "email": "OWNER@example.test", "is_bot": False}
    ]
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    target = Target(BotConfig(name="bot", allow_user_ids=[20]), INSTANCE, client)
    store = Store(tmp_path)
    ec2 = EC2()
    waker = Waker([target], ec2, store, 1)
    await waker.refresh(force=True)
    ec2.describes = 0
    yield waker, target, fake, ec2
    await asyncio.gather(*waker.tasks.values())
    await client.close()
    store.close()


async def send(waker, target, fake, *, dm=False, uid=20):
    result = await target.client.request(
        "POST",
        "messages",
        type="private" if dm else "stream",
        to=[20] if dm else 1,
        topic="wake",
        content="unmentioned follow-up",
    )
    message = fake.messages[result["id"]] | {"sender_id": uid}
    await waker.message(target, message)
    await asyncio.gather(*list(waker.tasks.values()))
    return result["id"]


@pytest.mark.parametrize("dm", [False, True])
async def test_owner_wakes_stopped_vm_and_gets_ack(setup, dm):
    waker, target, fake, ec2 = setup
    mid = await send(waker, target, fake, dm=dm)
    assert ec2.starts == [{"InstanceIds": [INSTANCE]}]
    if dm:
        assert any("Starting your VM" in m["content"] for m in fake.messages.values())
    else:
        assert fake.messages[mid]["reactions"][0]["emoji_code"] == "23f0"
    assert ec2.describes == 1  # fresh immediately before start
    assert waker.store.get(f"wake:{INSTANCE}")


@pytest.mark.parametrize("state", ["running", "pending", "terminated"])
async def test_nonstopped_instance_is_left_alone(setup, state):
    waker, target, fake, ec2 = setup
    ec2.state = state
    waker.states[INSTANCE] = state
    await send(waker, target, fake)
    assert not ec2.starts
    assert len(fake.messages) == 1


async def test_not_owner_bot_inactive_and_wrong_tag_fail_closed(setup):
    waker, target, fake, ec2 = setup
    for uid in (99, 1):
        await send(waker, target, fake, uid=uid)
    fake.members[-1]["is_active"] = False
    await send(waker, target, fake)
    fake.members[-1]["is_active"] = True
    ec2.tags = []
    await send(waker, target, fake)
    assert not ec2.starts


async def test_email_resolution_replaces_owner_and_refreshes(setup):
    waker, target, fake, ec2 = setup
    target.bot.allow_user_ids = []
    target.bot.allow_emails = ["owner@example.test"]
    fake.members[-1]["email"] = "hidden@example.test"
    await send(waker, target, fake)
    assert not ec2.starts
    fake.members[-1]["email"] = "Owner@Example.Test"
    await send(waker, target, fake)
    assert len(ec2.starts) == 1


async def test_cooldown_survives_restart_and_is_shared_between_bots(setup):
    waker, target, fake, ec2 = setup
    await send(waker, target, fake)
    ec2.state = "stopped"
    sibling = Target(BotConfig(name="other", allow_user_ids=[20]), INSTANCE, target.client)
    restarted = Waker([target, sibling], ec2, waker.store, 1)
    await send(restarted, sibling, fake)
    assert len(ec2.starts) == 1


async def test_cached_running_avoids_user_and_ec2_queries(setup):
    waker, target, fake, ec2 = setup
    ec2.state = "running"
    await waker.refresh(force=True)
    ec2.state = "stopped"
    fake.requests.clear()
    await send(waker, target, fake)
    assert not ec2.starts
    assert ec2.describes == 1
    assert not any(path.startswith("users") for _, path, _ in fake.requests)
    assert "unmentioned follow-up" not in repr(waker.waiting)
    await waker.refresh(force=True)
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1


@pytest.mark.parametrize("failures", [2, 3])
async def test_capacity_retries_respect_minute_budget_and_notify(setup, monkeypatch, failures):
    waker, target, fake, ec2 = setup
    ec2.failures = failures
    clock = [1000]
    delays = []
    monkeypatch.setattr(
        service, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    await send(waker, target, fake, dm=True)
    assert len(ec2.starts) == 3
    assert delays == [60, 120]
    contents = [m["content"] for m in fake.messages.values()]
    if failures == 3:
        assert any("Could not start" in text for text in contents)
        assert any("InsufficientInstanceCapacity" in text for text in contents)
    else:
        assert any("Starting your VM" in text for text in contents)


async def test_an_odd_start_error_is_logged_by_its_class(setup, monkeypatch, caplog):
    """#348: an exception whose `response` isn't botocore's shape fails the wake, logged by its
    class, not by the AttributeError reading it would raise."""
    waker, target, fake, ec2 = setup

    class Odd(Exception):
        response = "odd"

    def start_instances(**kwargs):
        raise Odd()

    monkeypatch.setattr(ec2, "start_instances", start_instances)
    await send(waker, target, fake)
    assert "Wake failed (Odd)" in caplog.text and "AttributeError" not in caplog.text


async def test_two_simultaneous_bot_messages_only_start_once(setup):
    waker, target, fake, ec2 = setup
    other = Target(BotConfig(name="other", allow_user_ids=[20]), INSTANCE, target.client)
    await asyncio.gather(send(waker, target, fake), send(waker, other, fake))
    assert len(ec2.starts) == 1
    assert not waker.tasks


async def test_failed_owner_notice_does_not_suppress_organizer_notice(setup, monkeypatch):
    waker, target, fake, _ = setup
    from zab.zulip.client import ZulipError

    original = target.client.request

    async def reject_dm(method, endpoint, **params):
        if params.get("type") == "private":
            raise ZulipError("FORBIDDEN", 403)
        return await original(method, endpoint, **params)

    monkeypatch.setattr(target.client, "request", reject_dm)
    await waker.notify(target, type="private", to=[20], content="decoy")
    await waker.notify(target, type="stream", to=1, topic="failures", content="notice")
    assert any(m["content"] == "notice" for m in fake.messages.values())


async def test_listener_registers_message_only_queue_and_wakes(setup, monkeypatch):
    waker, target, fake, ec2 = setup
    done = asyncio.Event()
    blocked = asyncio.Event()
    calls = 0
    original = waker.notify

    async def events(queue_id, last):
        nonlocal calls
        calls += 1
        if calls > 1:
            await blocked.wait()
        return [
            {"id": 1, "type": "message", "message": {"id": 1, "sender_id": 20, "type": "stream"}}
        ]

    async def notify(*args, **kwargs):
        await original(*args, **kwargs)
        done.set()

    monkeypatch.setattr(target.client, "events", events)
    monkeypatch.setattr(waker, "notify", notify)
    fake.messages[1] = {"id": 1, "content": "DECOY no persistence", "reactions": []}
    task = asyncio.create_task(waker.listen(target))
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert len(ec2.starts) == 1
        registration = next(p for _, endpoint, p in fake.requests if endpoint == "register")
        assert registration["event_types"] == '["message"]'
        assert "DECOY" not in "\n".join(waker.store.db.iterdump())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("finishes", [True, False])
async def test_stopping_waits_with_a_bound(setup, monkeypatch, finishes):
    waker, target, fake, ec2 = setup
    waker.states[INSTANCE] = ec2.state = "stopping"
    clock = [1000]
    monkeypatch.setattr(
        service, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )

    async def sleep(delay):
        clock[0] += delay
        if finishes:
            ec2.state = "stopped"

    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    await send(waker, target, fake)
    assert len(ec2.starts) == int(finishes)
    assert clock[0] == (1010 if finishes else 1300)
    assert not waker.tasks


async def test_missing_id_does_not_block_other_vm_and_logs_once(setup, monkeypatch, caplog):
    waker, target, fake, ec2 = setup
    missing = "i-00000000000000000"
    waker.locks[missing] = asyncio.Lock()
    original = ec2.describe_instances

    def describe(**kwargs):
        assert kwargs["Filters"][1]["Values"] == [missing, INSTANCE]
        kwargs["Filters"][1]["Values"] = [INSTANCE]
        return original(**kwargs)

    monkeypatch.setattr(ec2, "describe_instances", describe)
    await waker.refresh(force=True)
    await send(waker, target, fake)
    assert len(ec2.starts) == 1
    assert caplog.text.count(missing) == 1


async def test_the_workshop_tag_comes_from_the_config(tmp_path):
    """The test waker (issue #263) serves `workshop=zab-test`: it filters on that value and
    ignores an instance that carries only the fleet's tag."""
    filters = []

    class TestEc2:
        def describe_instances(self, **kwargs):
            filters.append(kwargs["Filters"])
            tags = {"i-fleet": [TAG], "i-test": [{"Key": "workshop", "Value": "zab-test"}]}
            instances = [
                {"InstanceId": i, "State": {"Name": "stopped"}, "Tags": t} for i, t in tags.items()
            ]
            return {"Reservations": [{"Instances": instances}]}

    store = Store(tmp_path)
    bot = BotConfig(name="bot", allow_user_ids=[20])
    targets = [Target(bot, i, SimpleNamespace()) for i in ("i-fleet", "i-test")]
    waker = Waker(targets, TestEc2(), store, 1, "zab-test")
    await waker.refresh(force=True)
    assert filters[0][0] == {"Name": "tag:workshop", "Values": ["zab-test"]}
    assert waker.states == {"i-test": "stopped"} and waker.missing == {"i-fleet"}
    assert Waker([], None, store, 1).tag == TAG  # the fleet's tag by default
    store.close()


# -------------------------------------------- the right bot's ⏰ on every waiting message


#: The pair's bots: (config name, Zulip user ID, full name).
BOTS = (("codex", 31, "Codex"), ("claude", 32, "Claude"))


@pytest.fixture
async def pair(setup, monkeypatch):
    """The VM's two bots, Codex configured first as `push_waker` writes them, each with its own
    queue and its own client, signed in as itself (`users/me`); every notice recorded as (bot,
    endpoint, content), and every request of each bot's client as (bot, endpoint, params)."""
    first, _, fake, ec2 = setup
    fake.members += [
        {"user_id": uid, "full_name": name, "email": f"{bot}@example.test", "is_bot": True}
        for bot, uid, name in BOTS
    ]
    calls = []
    clients = []
    for bot, _, _ in BOTS:
        client = ZulipClient("https://zulip.example", f"{bot}@example.test", "decoy",
                             transport=fake.transport)  # fmt: skip
        request = client.request

        async def recorded(method, endpoint, _bot=bot, _request=request, **params):
            calls.append((_bot, endpoint, params))
            return await _request(method, endpoint, **params)

        monkeypatch.setattr(client, "request", recorded)
        clients.append(client)
    codex, claude = (
        Target(BotConfig(name=bot, allow_user_ids=[20]), INSTANCE, client)
        for (bot, _, _), client in zip(BOTS, clients, strict=True)
    )
    waker = Waker([codex, claude], ec2, first.store, 1)
    await waker.refresh(force=True)
    waker.calls = calls
    notices = []
    original = waker.notify

    async def notify(bot, endpoint="messages", **params):
        notices.append((bot.bot.name, endpoint, params.get("content")))
        await original(bot, endpoint, **params)

    monkeypatch.setattr(waker, "notify", notify)
    yield waker, codex, claude, fake, ec2, notices
    await asyncio.gather(*waker.tasks.values())
    for client in clients:
        await client.close()


def alarms(notices):
    """(bot, message ID) of every ⏰ the waker posted."""
    return [(bot, int(endpoint.split("/")[1])) for bot, endpoint, _ in notices if "/" in endpoint]


async def deliver(waker, message, *copies):
    """Each bot's queue's copy of `message`, in order: (target, mentioned)."""
    for target, mentioned in copies:
        await waker.message(target, message, ["mentioned"] if mentioned else [])


@pytest.mark.parametrize("mentioned", [("codex",), ("claude",), ("codex", "claude")])
@pytest.mark.parametrize("first", ["codex", "claude"])
async def test_a_mention_gets_an_alarm_clock_from_each_mentioned_bot(pair, mentioned, first):
    """One ⏰ from each bot the message mentions, as each bridge adds its 👀, in the configured
    order (Codex first), whichever queue delivers first; none from a bot it doesn't mention
    (Drew on #679: the ⏰ came from Codex alone for a message to both)."""
    waker, codex, claude, fake, ec2, notices = pair
    bots = {"codex": codex, "claude": claude}
    other = {"codex": "claude", "claude": "codex"}[first]
    message = fake.post_as(20, "DECOY @**bot** text", "wake")
    await deliver(waker, message, *[(bots[name], name in mentioned) for name in (first, other)])
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [(name, message["id"]) for name in mentioned]
    assert "DECOY" not in repr(waker.waiting) + repr(waker.done)


@pytest.mark.parametrize(
    ("text", "flagged", "expected"),
    [
        ("@claude DECOY", (), ["claude"]),
        ("@Codex: DECOY", (), ["codex"]),
        ("ask @CLAUDE and @codex DECOY", (), ["codex", "claude"]),
        ("@claude and @**Codex** DECOY", ("codex",), ["codex", "claude"]),
        ("`@claude` x@codex.ai \\@claude @claudette DECOY", (), ["codex", "claude"]),
        ("```quote\n@claude DECOY\n```\n@codex", (), ["codex"]),
        ("https://medium.com/@claude/post DECOY @codex", (), ["codex"]),  # a link's `@`
    ],
)
@pytest.mark.parametrize("first", ["codex", "claude"])
async def test_a_typed_mention_gets_an_alarm_clock_from_that_bot(
    pair, text, flagged, expected, first
):
    """A typed `@claude` or `@codex`, which Zulip doesn't flag, names the bot of that kind
    (#713): its ⏰, as a completed mention's; one in code, escaped, part of a word or quoted
    names none, and the message gets each bot's ⏰ as one without a mention. The waker reads the
    text as it arrives and keeps none of it."""
    waker, codex, claude, fake, ec2, notices = pair
    bots = {"codex": codex, "claude": claude}
    other = {"codex": "claude", "claude": "codex"}[first]
    message = fake.post_as(20, text, "typed " + first)
    await deliver(waker, message, *[(bots[name], name in flagged) for name in (first, other)])
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [(name, message["id"]) for name in expected]
    assert "DECOY" not in repr(waker.waiting) + repr(waker.done)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("@claude DECOY", ["claude"]), ("@Codex: DECOY", ["codex"]), ("DECOY", ["codex", "claude"])],
)
async def test_a_typed_mention_caught_up_gets_that_bots_alarm_clock(pair, text, expected):
    """Codex round 6 on #727 (after #713): a caught-up message keeps no text (`queues.kept`), but
    the kinds its typed mentions name (`typed`), read before the text goes. So after a restart a
    typed `@claude`, which Zulip doesn't flag, still gets Claude's ⏰ alone, as live."""
    waker, codex, claude, fake, ec2, notices = pair
    mark = fake.post_as(20, "handled before the restart", "earlier")["id"]
    message = fake.post_as(20, text, "typed, caught up")
    fake.messages[message["id"]]["flags"] = []  # typed: Zulip flags no mention
    for target in (codex, claude):  # each bot's catch-up, after its mark
        await queues.catch_up(target, waker.message, (mark, None), set(), message["id"])
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [(name, message["id"]) for name in expected]
    assert "DECOY" not in repr(waker.waiting) + repr(waker.done)


def test_a_bots_kind_is_its_names_prefix():
    assert [service.kind_of(n) for n in ("codex-alice", "Claude-bob", "claude", "bot", "x-y")] == [
        "codex", "claude", "claude", None, None
    ]  # fmt: skip


@pytest.mark.parametrize("refuses", ["codex", "claude"])
async def test_a_mentioned_bot_that_refuses_the_sender_adds_none(pair, monkeypatch, refuses):
    """Both bots mentioned, their allowlists differ: the bot that allows the sender wakes the VM
    and adds its ⏰; the other's copy, merged before the ⏰s, gets that bot's owner check, once,
    as its own bridge would check it, and adds none (#679)."""
    waker, codex, claude, fake, ec2, notices = pair
    bots = {"codex": codex, "claude": claude}
    allows = {"codex": "claude", "claude": "codex"}[refuses]
    bots[refuses].bot.allow_user_ids = [99]
    checked = []
    owner = waker.owner

    async def counted(target, uid):
        checked.append(target.bot.name)
        return await owner(target, uid)

    monkeypatch.setattr(waker, "owner", counted)
    message = fake.post_as(20, "@**Codex** @**Claude** hi", "wake")
    await deliver(waker, message, (bots[allows], True), (bots[refuses], True))
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [(allows, message["id"])]
    assert checked.count(refuses) == 1  # once, in the acknowledgment


async def test_a_mention_of_both_bots_then_a_follow_up_in_its_topic(pair):
    """A mention of both bots gets both ⏰s, and so does an unmentioned follow-up in its topic,
    which the bridge sends to both (#683; one, from Codex, before); an unmentioned message in
    another topic, one from the first configured bot that saw it."""
    waker, codex, claude, fake, ec2, notices = pair
    waker.states[INSTANCE] = "running"  # a stale cache: all three wait for the next poll
    both = fake.post_as(20, "@**Codex** @**Claude** hi", "wake")
    follow = fake.post_as(20, "and again", "wake")
    elsewhere = fake.post_as(20, "elsewhere", "another topic")
    await deliver(waker, both, (claude, True), (codex, True))
    await deliver(waker, follow, (claude, False), (codex, False))
    await deliver(waker, elsewhere, (claude, False))
    await waker.refresh(force=True)
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [
        ("codex", both["id"]),
        ("claude", both["id"]),
        ("codex", follow["id"]),
        ("claude", follow["id"]),
        ("claude", elsewhere["id"]),
    ]


async def test_two_quick_messages_both_get_one_alarm_clock_in_flight(pair, monkeypatch):
    """A mention, then an unmentioned follow-up and a non-owner's message in the same topic that
    arrive while `start_instances` is running: the follow-up joins the wake and gets a ⏰ from
    each bot, as a message without a mention goes to each (#713); the non-owner's gets none."""
    waker, codex, claude, fake, ec2, notices = pair
    started, release = threading.Event(), threading.Event()
    start = ec2.start_instances

    def blocking_start(**kwargs):
        started.set()
        assert release.wait(5)
        return start(**kwargs)

    monkeypatch.setattr(ec2, "start_instances", blocking_start)
    first = fake.post_as(20, "@**Claude** !vm", "wake")
    await deliver(waker, first, (codex, False), (claude, True))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    second = fake.post_as(20, "what is 1+1", "wake")
    stranger = fake.post_as(99, "hello", "wake")
    await deliver(waker, second, (claude, False), (codex, False))
    await deliver(waker, stranger, (codex, False), (claude, False))
    release.set()
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [
        ("claude", first["id"]),
        ("codex", second["id"]),
        ("claude", second["id"]),
    ]
    assert not waker.waiting.get(INSTANCE)


async def test_two_quick_messages_both_get_one_alarm_clock_after_a_stale_cache(pair):
    """The cache still says running: both messages wait, without owner or EC2 queries, and
    `reconsider` wakes the VM for both: the mention's ⏰ from its bot, the follow-up's from each
    (Drew's `!vm`, then "what is 1+1", on 2026-10-14; #713)."""
    waker, codex, claude, fake, ec2, notices = pair
    waker.states[INSTANCE] = "running"
    fake.requests.clear()
    first = fake.post_as(20, "@**Claude** !vm", "wake")
    second = fake.post_as(20, "what is 1+1", "wake")
    await deliver(waker, first, (codex, False), (claude, True))
    await deliver(waker, second, (claude, False), (codex, False))
    assert not ec2.starts and not fake.requests
    assert sorted(waker.waiting[INSTANCE]) == [first["id"], second["id"]]
    await waker.refresh(force=True)
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [
        ("claude", first["id"]),
        ("codex", second["id"]),
        ("claude", second["id"]),
    ]


async def test_one_alarm_clock_per_message_across_the_two_queues(pair, monkeypatch):
    """A copy that arrives after the ⏰ was posted adds none, not even at a later wake, nor
    does a message sent after the start (the bridge's 👀 answers it): what waits for a VM that
    is up is dropped at the next poll. Memory stays bounded (`HOLD` waiting per VM, `DONE`)."""
    waker, codex, claude, fake, ec2, notices = pair
    monkeypatch.setattr(service, "DONE", 2)
    message = fake.post_as(20, "@**Claude** hi", "wake")
    await deliver(waker, message, (codex, False))  # Claude's queue lags past the grace
    await asyncio.gather(*waker.tasks.values())
    after = fake.post_as(20, "@**Claude** again", "wake")
    await deliver(waker, message, (claude, True))  # too late to change the bot
    await deliver(waker, after, (claude, True), (codex, False))
    await waker.refresh(force=True)  # pending
    await waker.reconsider()
    assert alarms(notices) == [("codex", message["id"])]
    assert not waker.waiting and len(ec2.starts) == 1
    # the start failed after all and the cooldown passed: the next wake is for new messages
    ec2.state = waker.states[INSTANCE] = "stopped"
    waker.store.set(f"wake:{INSTANCE}", 0)
    await deliver(waker, message, (claude, True))
    third = fake.post_as(20, "@**Claude** third", "wake")
    await deliver(waker, third, (claude, True))
    await asyncio.gather(*waker.tasks.values())
    assert alarms(notices) == [("codex", message["id"]), ("claude", third["id"])]
    assert len(ec2.starts) == 2 and len(waker.done) == 2
    monkeypatch.setattr(service, "HOLD", 3)
    for _ in range(5):
        await deliver(waker, fake.post_as(20, "busy", "wake"), (codex, False))
    assert len(waker.waiting[INSTANCE]) == 3


async def test_a_late_copy_adds_no_alarm_clock_from_either_bot(pair):
    """Both bots mentioned. Codex's copy comes twice within the grace (as a catch-up's and its
    queue's may), Claude's only after it: Codex's ⏰, once. Claude's copy adds none, then or at
    a later wake. The memory is per message: no bot adds a second ⏰ to it (#679)."""
    waker, codex, claude, fake, ec2, notices = pair
    message = fake.post_as(20, "@**Codex** @**Claude** hi", "wake")
    await deliver(waker, message, (codex, True))
    for _ in range(500):
        if waker.acking.get(INSTANCE):
            break
        await asyncio.sleep(0.01)
    await deliver(waker, message, (codex, True))  # the same bot's copy again, in the grace
    assert waker.acking.get(INSTANCE) and not notices
    await asyncio.gather(*waker.tasks.values())  # the grace ends without Claude's copy
    await deliver(waker, message, (claude, True), (codex, True))
    assert alarms(notices) == [("codex", message["id"])]
    ec2.state = waker.states[INSTANCE] = "stopped"  # the start failed after all
    waker.store.set(f"wake:{INSTANCE}", 0)  # and the cooldown passed
    await deliver(waker, message, (claude, True), (codex, True))
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())
    assert alarms(notices) == [("codex", message["id"])]
    assert len(ec2.starts) == 1 and not waker.waiting.get(INSTANCE)


async def test_one_noisy_sender_does_not_push_out_the_owners_message(pair, monkeypatch):
    """Past `HOLD`, the waker drops the oldest message of the sender with the most."""
    waker, codex, claude, fake, _, _ = pair
    monkeypatch.setattr(service, "HOLD", 3)
    waker.states[INSTANCE] = "running"
    owner = fake.post_as(20, "@**Claude** hi", "wake")
    await deliver(waker, owner, (claude, True))
    for _ in range(5):
        await deliver(waker, fake.post_as(99, "chatter", "wake"), (codex, False))
    assert len(waker.waiting[INSTANCE]) == 3 and owner["id"] in waker.waiting[INSTANCE]


class TwoVms(EC2):
    """Two opted-in VMs with their own states."""

    def __init__(self, states):
        super().__init__()
        self.states = states

    def describe_instances(self, **kwargs):
        instances = [
            {"InstanceId": i, "State": {"Name": s}, "Tags": [TAG]} for i, s in self.states.items()
        ]
        return {"Reservations": [{"Instances": instances}]}

    def start_instances(self, **kwargs):
        self.starts.append(kwargs)
        self.states[kwargs["InstanceIds"][0]] = "pending"
        return {}


async def test_a_message_during_a_poll_pass_waits_for_the_next_poll(setup, monkeypatch):
    """VM A's owner checks are slow; VM B, which the poll saw running, starts stopping and its
    owner writes meanwhile: the message waits for the next poll, which wakes B (reviewer on
    #301: the pass used to drop B's messages against the state seen before it arrived)."""
    first, target, fake, _ = setup
    vm_a, vm_b = INSTANCE, "i-0fedcba9876543210"
    ec2 = TwoVms({vm_a: "running", vm_b: "running"})
    a, b = (
        Target(BotConfig(name=name, allow_user_ids=[20]), vm, target.client)
        for name, vm in (("a", vm_a), ("b", vm_b))
    )
    waker = Waker([a, b], ec2, first.store, 1)
    await waker.refresh(force=True)
    await waker.message(a, fake.post_as(20, "for A", "a"))
    await waker.message(b, fake.post_as(20, "earlier, for B", "b"))
    ec2.states[vm_a] = "stopped"
    await waker.refresh(force=True)
    gate, checking = asyncio.Event(), asyncio.Event()
    owner = waker.owner

    async def slow_owner(bot, uid):
        if bot is a:
            checking.set()
            await gate.wait()
        return await owner(bot, uid)

    monkeypatch.setattr(waker, "owner", slow_owner)
    poll = asyncio.create_task(waker.reconsider())
    await checking.wait()
    ec2.states[vm_b] = "stopping"
    await waker.message(b, fake.post_as(20, "for B", "b"))
    gate.set()
    await poll
    ec2.states[vm_b] = "stopped"
    await waker.refresh(force=True)
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())
    assert sorted(s["InstanceIds"][0] for s in ec2.starts) == sorted([vm_a, vm_b])


async def test_a_copy_within_the_grace_still_picks_the_mentioned_bot(pair, monkeypatch):
    """Codex's copy alone starts the VM; Claude's copy, with the mention, arrives after the
    start request: the ⏰ waited for it and comes from Claude (codex on #301)."""
    waker, codex, claude, fake, ec2, notices = pair
    monkeypatch.setattr(service, "GRACE", 5.0)
    message = fake.post_as(20, "@**Claude** hi", "wake")
    await deliver(waker, message, (codex, False))
    for _ in range(500):
        if waker.acking.get(INSTANCE):
            break
        await asyncio.sleep(0.01)
    assert len(ec2.starts) == 1 and not notices  # started; the ⏰ waits for Claude's copy
    await deliver(waker, message, (claude, True))
    await asyncio.gather(*waker.tasks.values())
    assert alarms(notices) == [("claude", message["id"])]


async def test_a_bot_that_allows_the_sender_wakes_the_vm_when_the_other_denies(pair, monkeypatch):
    """The bots' allowlists differ: Codex's copy arrives first and Codex refuses the sender;
    Claude's copy, merged during that check, is checked too, and wakes the VM (codex on #301)."""
    waker, codex, claude, fake, ec2, notices = pair
    codex.bot.allow_user_ids = [99]
    gate = asyncio.Event()
    owner = waker.owner

    async def slow_owner(target, uid):
        if target is codex:
            await gate.wait()
        return await owner(target, uid)

    monkeypatch.setattr(waker, "owner", slow_owner)
    message = fake.post_as(20, "an unmentioned message", "wake")
    first = asyncio.create_task(deliver(waker, message, (codex, False)))
    await asyncio.sleep(0)
    await deliver(waker, message, (claude, False))
    gate.set()
    await first
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [("claude", message["id"])]


@pytest.mark.parametrize("seen_by", [("codex", "claude"), ("claude",)])
async def test_an_unmentioned_message_still_wakes_the_vm(pair, seen_by):
    """No mention, here or earlier in its topic: each bot that saw it answers for it, in the
    configuration's order, as each of the VM's bots answers a new topic (#687)."""
    waker, codex, claude, fake, ec2, notices = pair
    bots = {"codex": codex, "claude": claude}
    message = fake.post_as(20, "an unmentioned follow-up", "existing session")
    await deliver(waker, message, *[(bots[name], False) for name in seen_by])
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [(name, message["id"]) for name in seen_by]


@pytest.mark.parametrize("mentioned", [False, True])
async def test_two_quick_dms_to_both_bots_get_one_starting_notice(pair, mentioned):
    """One notice per wake, from the bot of the first DM, also when the DMs mention the bots and
    the second (a group DM with both) reaches both (#679: a DM never gets one per bot)."""
    waker, codex, claude, fake, ec2, notices = pair
    first, second = fake.post_as(20, "!status"), fake.post_as(20, "!status")
    await deliver(waker, first, (claude, mentioned))
    await deliver(waker, second, (codex, mentioned), (claude, mentioned))
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert notices == [("claude", "messages", service.STARTING)]


# ------------------------------- a message without a mention: each bot's ⏰ (#713, no lookup)


async def batched(waker, *copies):
    """The VM stopped while the cache said running: each (message, *(target, mentioned)) waits,
    then the next poll wakes the VM for all of them at once."""
    waker.states[INSTANCE] = "running"
    for message, *bots in copies:
        await deliver(waker, message, *bots)
    await waker.refresh(force=True)
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())


@pytest.mark.parametrize(
    "before",
    ["@**Claude** DECOY", "@claude DECOY", "@**Codex** @**Claude** DECOY", "DECOY", None],
)
@pytest.mark.parametrize("seen_by", [("codex", "claude"), ("claude", "codex"), ("claude",)])
async def test_a_message_without_a_mention_gets_each_bots_clock(pair, before, seen_by):
    """#713: a follow-up without a mention after the stop goes to each of the VM's bots,
    whatever the topic's earlier messages addressed (SPEC §4.5 rule 6.2), and gets a ⏰ from
    each bot that saw it, in configured order; the waker reads no history (#683's lookup
    went): it makes no request but the owner checks."""
    waker, codex, claude, fake, ec2, notices = pair
    bots = {"codex": codex, "claude": claude}
    if before:
        fake.post_as(20, before, "wake")
    follow = fake.post_as(20, "and again", "wake")
    await deliver(waker, follow, *[(bots[name], False) for name in seen_by])
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [(name, follow["id"]) for name in sorted(seen_by, key=waker.rank.get)]
    assert not [c for c in waker.calls if c[1] == "messages" and "narrow" in c[2]]


async def test_a_wake_of_mentions_and_follow_ups(pair):
    """In one wake: an unmentioned message, a mention of both, a follow-up, a mention of Codex
    and one after it, in two topics, and a DM. Each mention gets its bots' ⏰s, each message
    without one both bots' ⏰s, whatever came before it; the DM gets the one notice."""
    waker, codex, claude, fake, ec2, notices = pair
    first = fake.post_as(20, "a follow-up", "wake")
    both = fake.post_as(20, "@**Codex** @**Claude** to both", "wake")
    second = fake.post_as(20, "another follow-up", "wake")
    mention = fake.post_as(20, "@codex now Codex", "other")
    after = fake.post_as(20, "and on", "other")
    dm = fake.post_as(20, "!status")
    unmentioned = [(codex, False), (claude, False)]
    await batched(
        waker,
        (first, *unmentioned),
        (both, (claude, True), (codex, True)),
        (second, *unmentioned),
        (mention, *unmentioned),  # typed: no flag
        (after, *unmentioned),
        (dm, (claude, False)),
    )
    assert len(ec2.starts) == 1
    assert alarms(notices) == [
        ("codex", first["id"]),
        ("claude", first["id"]),
        ("codex", both["id"]),
        ("claude", both["id"]),
        ("codex", second["id"]),
        ("claude", second["id"]),
        ("codex", mention["id"]),
        ("codex", after["id"]),
        ("claude", after["id"]),
    ]
    assert [n for n in notices if n[1] == "messages"] == [("claude", "messages", service.STARTING)]


async def test_a_message_without_a_mention_gets_a_clock_from_each_bot_that_allows_the_sender(
    pair, monkeypatch
):
    """Each of the VM's bots that allows its sender: here Claude's alone."""
    waker, codex, claude, fake, ec2, notices = pair

    async def owner(target, uid):
        return target.bot.name == "claude"

    monkeypatch.setattr(waker, "owner", owner)
    message = fake.post_as(20, "a question", "new topic")
    await deliver(waker, message, (codex, False), (claude, False))
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    assert alarms(notices) == [("claude", message["id"])]


@pytest.mark.parametrize("refuses", ["codex", "claude"])
async def test_each_clock_of_a_follow_up_needs_its_bot_to_allow_the_sender(pair, refuses):
    """A follow-up's ⏰ comes from each bot that allows the sender, the other bot adding none,
    as its bridge answers nothing."""
    waker, codex, claude, fake, ec2, notices = pair
    {"codex": codex, "claude": claude}[refuses].bot.allow_user_ids = [99]
    follow = fake.post_as(20, "and again", "wake")
    await deliver(waker, follow, (codex, False), (claude, False))
    await asyncio.gather(*waker.tasks.values())
    assert len(ec2.starts) == 1
    allows = {"codex": "claude", "claude": "codex"}[refuses]
    assert alarms(notices) == [(allows, follow["id"])]


@pytest.fixture
def http_loggers():
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    yield loggers
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def test_the_waker_logs_httpx_warnings_only(http_loggers, monkeypatch):
    """httpx logs every Zulip long poll at INFO; the waker's own INFO messages stay."""
    from zab.waker import __main__ as cli

    calls = []
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: calls.append(kwargs))
    for logger in http_loggers:
        logger.setLevel(logging.NOTSET)
    cli.setup_logging()
    assert calls == [{"level": logging.INFO}]
    assert [logger.level for logger in http_loggers] == [logging.WARNING] * 2
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


# ---------------------------------------------------------------------- the wake log (#1017)

#: A wake's log line (`Waker.fields`): every field, the age a whole number of seconds.
WAKE_LINE = (
    r"Wake (?P<what>start|delayed|not started|failed \(\w+\)): workshop=nt-agents-2026 "
    rf"vm={INSTANCE} bot=(?P<bot>[\w-]+) message=(?P<message>\d+) sender=(?P<sender>\d+) "
    r"why=(?P<why>dm|mention|channel) seen=(?P<seen>\w+) via=(?P<via>live|catch-up) "
    r"age=\d+s(?P<rest>.*)"
)


def wake_lines(caplog) -> list[dict[str, str]]:
    """The waker's wake lines, each parsed by `WAKE_LINE` (any other fails the test)."""
    found = []
    for record in caplog.records:
        text = record.getMessage()
        if record.name == "zab.waker" and text.startswith(
            ("Wake start", "Wake delayed", "Wake not", "Wake fail")
        ):
            match = re.fullmatch(WAKE_LINE, text)
            assert match, text
            found.append(match.groupdict() | {"level": record.levelname})
    return found


@pytest.mark.parametrize("dm", [False, True])
async def test_a_start_is_logged_with_its_message_and_why(setup, caplog, dm):
    """One INFO line per start request, after it: the workshop, the VM, the bot, the message's
    and the sender's IDs, why the message counts, and the result; never the message's text."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    mid = await send(waker, target, fake, dm=dm)
    assert len(ec2.starts) == 1
    assert wake_lines(caplog) == [{
        "what": "start", "bot": "bot", "message": str(mid), "sender": "20",
        "why": "dm" if dm else "channel", "seen": "stopped", "via": "live", "rest": " result=ok",
        "level": "INFO",
    }]  # fmt: skip
    assert "unmentioned follow-up" not in caplog.text


async def test_a_message_held_for_the_poll_is_logged_as_seen_running(pair, caplog):
    """#1017's case: a mention seen while the VM was up, then a poll that shows it stopped (a
    stop just after the message): `seen=running` says that it waited for that poll."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, codex, claude, fake, ec2, _ = pair
    waker.states[INSTANCE] = "running"
    message = fake.post_as(20, "@**Claude** DECOY", "wake")
    await deliver(waker, message, (codex, False), (claude, True))
    assert not ec2.starts and not wake_lines(caplog)  # nothing logged while it waits
    await waker.refresh(force=True)
    await waker.reconsider()
    await asyncio.gather(*waker.tasks.values())
    (line,) = wake_lines(caplog)
    assert (line["bot"], line["message"], line["why"], line["seen"], line["via"]) == (
        "claude", str(message["id"]), "mention", "running", "live"
    )  # fmt: skip
    assert "DECOY" not in caplog.text


async def test_a_caught_up_message_is_logged_as_such(pair, caplog):
    """A message that the catch-up fetched (#403): `via=catch-up`, its typed mention read
    before its text went."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, codex, claude, fake, _, _ = pair
    mark = fake.post_as(20, "handled before the restart", "earlier")["id"]
    message = fake.post_as(20, "@codex DECOY", "caught up")
    fake.messages[message["id"]]["flags"] = []  # typed: Zulip flags no mention
    for target in (codex, claude):
        await queues.catch_up(target, waker.message, (mark, None), set(), message["id"])
    await asyncio.gather(*waker.tasks.values())
    (line,) = wake_lines(caplog)
    assert (line["bot"], line["why"], line["via"], line["rest"]) == (
        "codex", "mention", "catch-up", " result=ok"
    )  # fmt: skip
    assert "DECOY" not in caplog.text


class Refused(Exception):
    response: ClassVar = {"Error": {"Code": "UnauthorizedOperation"}}


async def test_a_refused_start_is_logged_with_its_code(setup, monkeypatch, caplog):
    """EC2 (or the starter) refuses the start: its line has the error code, then the wake's
    failure, with the same fields."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup

    def start_instances(**kwargs):
        raise Refused()

    monkeypatch.setattr(ec2, "start_instances", start_instances)
    mid = await send(waker, target, fake)
    start, failed = wake_lines(caplog)
    assert (start["what"], start["message"], start["rest"], start["level"]) == (
        "start", str(mid), " result=UnauthorizedOperation", "INFO"
    )  # fmt: skip
    assert (failed["what"], failed["message"], failed["rest"]) == ("failed (Refused)", str(mid), "")


async def test_each_capacity_failure_is_logged(setup, monkeypatch, caplog):
    """InsufficientInstanceCapacity, then a start: one line for each request."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    ec2.failures = 2
    clock = [1000]
    monkeypatch.setattr(
        service, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    await send(waker, target, fake, dm=True)
    assert [line["rest"] for line in wake_lines(caplog)] == [
        " result=InsufficientInstanceCapacity", " result=InsufficientInstanceCapacity", " result=ok"
    ]  # fmt: skip


@pytest.mark.parametrize("why", ["up", "owner", "cooldown"])
async def test_a_wake_given_up_before_its_start_is_logged(setup, monkeypatch, caplog, why):
    """No start request: the VM up at the wake's own refresh, the owner check refused at the
    wake's own check, or a start asked for since the message's owner check (another wake's).
    Each line says why, at INFO, once."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    if why == "up":
        ec2.state = "running"  # the cache still says stopped
    elif why == "owner":
        answers = iter([True, False])
        monkeypatch.setattr(waker, "owner", lambda target, uid: asyncio.sleep(0, next(answers)))
    else:

        async def owner(target, uid):  # a start asked for meanwhile
            waker.store.set(f"wake:{INSTANCE}", service.time.time())
            return True

        monkeypatch.setattr(waker, "owner", owner)
    mid = await send(waker, target, fake)
    assert not ec2.starts
    (line,) = wake_lines(caplog)
    assert (line["what"], line["message"], line["sender"]) == ("not started", str(mid), "20")
    reason = {"up": " (VM running)", "owner": " (owner check refused)",
              "cooldown": " (a start under a minute ago)"}[why]  # fmt: skip
    assert (line["bot"], line["rest"], line["level"]) == ("bot", reason, "INFO")


async def test_a_message_the_cooldown_holds_back_gets_no_line(pair, caplog):
    """The cooldown is checked first, without an owner check: each bot's copy of a message
    then is dropped unlogged, as one whose sender no bot allows (the start that began the
    cooldown has its line)."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, codex, claude, fake, ec2, _ = pair
    waker.store.set(f"wake:{INSTANCE}", service.time.time())
    waker.calls.clear()
    message = fake.post_as(20, "@**Claude** DECOY", "wake")
    await deliver(waker, message, (codex, False), (claude, True))
    await asyncio.gather(*waker.tasks.values())
    assert not ec2.starts and not wake_lines(caplog)
    assert not [endpoint for _, endpoint, _ in waker.calls if endpoint.startswith("users")]


# ------------------------------------------------- Zulip unavailable in the wake's check (#1022)


@pytest.fixture
def paused(monkeypatch):
    """The waker's clock (`clock`, which a client's request budget may take too), which only
    its sleeps move, at once: each sleep's delay (`delays`), then each of `during` (coroutine
    functions) runs, as in that pause."""
    now = [1000.0]
    state = SimpleNamespace(delays=[], during=[])

    async def sleep(delay):
        state.delays.append(delay)
        now[0] += delay
        for during in state.during:
            await during()

    state.clock = SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0], sleep=sleep)
    monkeypatch.setattr(service, "time", state.clock)
    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    return state


def users_fail(monkeypatch, target, failure):
    """`GET users` raises `failure(n)` at its n-th call, unless None: the first is the
    message's owner check (`Waker.trigger`), the wake's own follow."""
    original, calls = target.client.request, []

    async def request(method, endpoint, **params):
        if endpoint == "users":
            calls.append(endpoint)
            if (exc := failure(len(calls))) is not None:
                raise exc
        return await original(method, endpoint, **params)

    monkeypatch.setattr(target.client, "request", request)


#: Each transient error, and how the wake log names it.
TRANSIENT = {
    "502": (ZulipError("INVALID_RESPONSE", 502), "ZulipError HTTP 502"),  # a proxy's page, Nov 9
    "429": (ZulipError("RATE_LIMIT_HIT", 429), "ZulipError HTTP 429"),
    "refused": (httpx.ConnectError("refused"), "ConnectError"),
    "timeout": (httpx.ReadTimeout("slow"), "ReadTimeout"),
}


@pytest.mark.parametrize(("error", "kind"), TRANSIENT.values(), ids=TRANSIENT.keys())
async def test_a_zulip_error_in_the_wakes_check_is_tried_again(
    setup, monkeypatch, caplog, paused, error, kind
):
    """Zulip passes the message's owner check, then fails the wake's: one line says the wake
    waits, the check is tried again 5 s later from a fresh refresh, and the VM starts once. A
    message that came meanwhile joined the wake: its ⏰ too."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    users_fail(monkeypatch, target, lambda n: error if n == 2 else None)
    later = []

    async def message():
        later.append(fake.post_as(20, "DECOY meanwhile", "wake"))
        await waker.message(target, later[-1])

    paused.during.append(message)
    mid = await send(waker, target, fake)
    assert len(ec2.starts) == 1 and paused.delays == [5.0] and ec2.describes == 2
    for m in (mid, later[0]["id"]):
        assert [r["emoji_code"] for r in fake.messages[m]["reactions"]] == ["23f0"]
    assert [(line["what"], line["rest"], line["level"]) for line in wake_lines(caplog)] == [
        ("delayed", f" (Zulip unavailable: {kind})", "INFO"), ("start", " result=ok", "INFO")
    ]  # fmt: skip
    assert "DECOY" not in caplog.text and not waker.waiting and not waker.tasks


async def test_a_zulip_outage_past_the_bound_drops_the_wake_with_a_line(
    setup, monkeypatch, caplog, paused
):
    """Every try fails: the waits double from 5 s up to a minute, the last cut to leave its try
    `ROOM` (5 s) before the bound, five minutes after the first failure; each wait has its try
    (review round 1). Then `Wake not started:` with the error's kind and status, and the
    messages are dropped, as before (#1022)."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    down, checks = ZulipError("INVALID_RESPONSE", 502), []
    users_fail(monkeypatch, target, lambda n: checks.append(n) or (down if n > 1 else None))
    mid = await send(waker, target, fake)
    assert not ec2.starts
    assert paused.delays == [5, 10, 20, 40, 60, 60, 60, 40]
    assert sum(paused.delays) == service.OUTAGE - service.ROOM
    assert len(checks) == 1 + 9  # the message's own check, then the wake's tries
    reason = " (Zulip unavailable: ZulipError HTTP 502)"
    assert [(line["what"], line["message"], line["rest"], line["level"])
            for line in wake_lines(caplog)] == [
        ("delayed", str(mid), reason, "INFO"), ("not started", str(mid), reason, "WARNING")
    ]  # fmt: skip
    assert not waker.waiting and not waker.tasks


async def test_zulip_back_before_the_bound_gets_its_last_try(setup, monkeypatch, caplog, paused):
    """Review round 1: Zulip back 260 s after the first failure, just after the try at 255 s.
    The last wait, cut to 40 s, still leads to a try, at 295 s, which starts the VM."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    down, first = ZulipError("INVALID_RESPONSE", 502), paused.clock.monotonic()
    back = first + 260
    users_fail(
        monkeypatch, target, lambda n: down if n > 1 and paused.clock.time() < back else None
    )
    await send(waker, target, fake)
    assert len(ec2.starts) == 1 and paused.delays == [5, 10, 20, 40, 60, 60, 60, 40]
    assert [line["what"] + line["rest"] for line in wake_lines(caplog)] == [
        "delayed (Zulip unavailable: ZulipError HTTP 502)", "start result=ok"
    ]  # fmt: skip


async def test_a_refusal_in_the_wakes_check_ends_it_at_once(setup, monkeypatch, caplog, paused):
    """A 401 (a deactivated bot's key) is Zulip refusing: no retry and no start, the wake failed
    as before #1022."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    refused = ZulipError("UNAUTHORIZED", 401)
    users_fail(monkeypatch, target, lambda n: refused if n > 1 else None)
    await send(waker, target, fake)
    assert not ec2.starts and not paused.delays
    (line,) = wake_lines(caplog)
    assert (line["what"], line["level"]) == ("failed (ZulipError)", "WARNING")
    assert not waker.waiting and not waker.tasks


async def test_a_vm_up_after_the_outage_is_left_alone(setup, monkeypatch, caplog, paused):
    """The retry begins at the refresh: a VM started meanwhile is no longer stopped, so no
    start request, and the messages go to its bridge."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    down = ZulipError("INVALID_RESPONSE", 503)
    users_fail(monkeypatch, target, lambda n: down if n == 2 else None)

    async def started():
        ec2.state = "running"

    paused.during.append(started)
    await send(waker, target, fake)
    assert not ec2.starts and paused.delays == [5.0]
    assert [line["what"] + line["rest"] for line in wake_lines(caplog)] == [
        "delayed (Zulip unavailable: ZulipError HTTP 503)", "not started (VM running)"
    ]  # fmt: skip


async def test_a_rate_limit_past_the_bound_ends_the_wake_without_waiting(
    setup, monkeypatch, caplog, paused
):
    """Codex round 1 on #1023: a 429 whose `Retry-After`, an hour, outlasts the bound, through
    the client. Its request budget would hold the next try for the hour, and the VM would start
    then; the wake gives up after its first wait instead, the 429 its reason."""
    caplog.set_level(logging.INFO, "zab.waker")
    waker, target, fake, ec2 = setup
    target.client.budget.clock = paused.clock
    original, calls = target.client.request, []

    async def request(method, endpoint, **params):
        if endpoint == "users/me":
            calls.append(endpoint)
            if len(calls) == 2:  # the wake's own check: Zulip answers it with the 429
                fake.failures.append((429, {"code": "RATE_LIMIT_HIT", "retry-after": 3600}))
        return await original(method, endpoint, **params)

    monkeypatch.setattr(target.client, "request", request)
    mid = await send(waker, target, fake)
    assert not ec2.starts and paused.delays == [5.0] and len(calls) == 2
    reason = " (Zulip unavailable: ZulipError HTTP 429)"
    assert [(line["what"], line["message"], line["rest"], line["level"])
            for line in wake_lines(caplog)] == [
        ("delayed", str(mid), reason, "INFO"), ("not started", str(mid), reason, "WARNING")
    ]  # fmt: skip
    assert not waker.waiting and not waker.tasks


async def test_a_try_zulip_never_answers_ends_at_the_bound(setup, monkeypatch, caplog):
    """Codex round 1 on #1023: a try begun before the bound, which Zulip never answers, is cut
    off there, and nothing starts. On the real clock, the bound, the first wait and the room
    for a try shortened."""
    caplog.set_level(logging.INFO, "zab.waker")
    monkeypatch.setattr(service, "OUTAGE", 0.3)
    monkeypatch.setattr(service, "BACKOFF", 0.01)
    monkeypatch.setattr(service, "ROOM", 0.05)
    waker, target, fake, ec2 = setup
    original, calls = target.client.request, []

    async def request(method, endpoint, **params):
        if endpoint == "users":
            calls.append(endpoint)
            if len(calls) == 2:
                raise ZulipError("INVALID_RESPONSE", 502)
            if len(calls) > 2:
                await asyncio.Event().wait()  # no answer
        return await original(method, endpoint, **params)

    monkeypatch.setattr(target.client, "request", request)
    await asyncio.wait_for(send(waker, target, fake), 5)
    assert not ec2.starts and len(calls) == 3
    assert [line["what"] + line["rest"] for line in wake_lines(caplog)] == [
        "delayed (Zulip unavailable: ZulipError HTTP 502)",
        "not started (Zulip unavailable: ZulipError HTTP 502)",
    ]
    assert not waker.waiting and not waker.tasks
