import asyncio
import json
import logging

import httpx
import pytest

from zab.core.logging import JsonFormatter
from zab.testing.fake_clock import FrozenClock
from zab.testing.fake_zulip import FakeZulip
from zab.zulip.budget import RequestBudget
from zab.zulip.client import ZulipClient, ZulipError
from zab.zulip.poster import Poster

pytestmark = pytest.mark.integration


async def test_polls_posts_and_uploads_share_budget_and_count_actual_attempts(monkeypatch):
    clock, delays = [1000.0], []

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr("zab.zulip.budget.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("zab.zulip.budget.time.time", lambda: clock[0])
    monkeypatch.setattr("zab.zulip.budget.asyncio.sleep", sleep)
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "DECOY_KEY", transport=fake.transport)
    await client.register()
    poster = Poster(client, per_minute=2)
    try:
        await client.events("1", 0)  # spends the second slot after registration
        await poster.send(1, "topic", "private")  # must wait for the poll's shared budget
        await client.upload("test.txt", b"private")
        assert [delay for delay in delays if delay] == [60]
        assert client.metrics.counts["zulip_requests"] == len(fake.requests) == 4
    finally:
        await poster.close()
        await client.close()


async def test_inflight_long_poll_does_not_hold_outbound_worker():
    entered, release = asyncio.Event(), asyncio.Event()
    fake = FakeZulip()

    async def transport(request):
        if request.url.path.endswith("/events"):
            entered.set()
            await release.wait()
        return await fake.handle(request)

    client = ZulipClient(
        "https://zulip.example", "bot", "decoy", transport=httpx.MockTransport(transport)
    )
    await client.register()
    poster = Poster(client)
    poll = asyncio.create_task(client.events("1", 0))
    try:
        await entered.wait()
        await asyncio.wait_for(poster.send(1, "topic", "reply"), 1)
        assert not poll.done()
    finally:
        release.set()
        await poll
        await poster.close()
        await client.close()


async def test_poll_429_logs_retry_after_without_body_and_recovers(caplog):
    caplog.set_level(logging.WARNING, logger="zab.zulip")
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "DECOY_KEY", transport=fake.transport)
    try:
        await client.register()
        fake.failures.append(
            (429, {"code": "RATE_LIMIT_HIT", "retry-after": 0.01, "msg": "PRIVATE_BODY"})
        )
        with pytest.raises(ZulipError):
            await client.events("1", 0)
        await client.events("1", 0)
        record = json.loads(JsonFormatter().format(caplog.records[-1]))
        assert record["event"] == "zulip_429" and record["retry_after"] == 0.01
        assert "PRIVATE_BODY" not in caplog.text and "DECOY_KEY" not in caplog.text
        assert client.metrics.counts["zulip_429"] == 1
        assert client.metrics.counts["zulip_requests"] == 3
    finally:
        await client.close()


async def test_concurrent_budget_acquires_do_not_overspend(monkeypatch):
    clock = [1000.0]
    original_sleep = asyncio.sleep

    async def sleep(delay):
        clock[0] += delay
        await original_sleep(0)

    monkeypatch.setattr("zab.zulip.budget.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("zab.zulip.budget.asyncio.sleep", sleep)
    budget = RequestBudget(2)
    await asyncio.gather(*(budget.acquire() for _ in range(5)))
    assert len(budget.sent) <= 2
    assert clock[0] >= 1120


async def test_housekeeping_lane_leaves_its_reserve_to_replies():
    """#443: housekeeping sends only while a third of the window is free; replies then go
    at once, and housekeeping resumes when the window moves."""

    clock = FrozenClock()
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "DECOY_KEY", transport=fake.transport)
    client.budget.clock = clock
    poster = Poster(client, per_minute=6)  # a reserve of 2
    try:
        await client.register()  # one of the six
        done = []

        async def housekeeping():
            for _ in range(6):
                await poster.background(lambda: client.request("GET", "users/me"))
                done.append(clock.now)

        task = asyncio.create_task(housekeeping())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(done) == 3:
                break
        await asyncio.sleep(0.05)
        assert len(done) == 3 and len(client.budget.sent) == 4  # 6 - 2 reserved
        await asyncio.wait_for(poster.send(1, "topic", "a reply"), 1)
        await asyncio.wait_for(poster.send(1, "topic", "another"), 1)
        assert len(client.budget.sent) == 6 and clock.now == 1000.0
        clock.advance(61)
        await asyncio.wait_for(task, 5)
        assert done == [1000.0] * 3 + [1061.0] * 3
    finally:
        clock.advance(3600)
        await poster.close()
        await client.close()


async def test_housekeeping_queued_behind_replies_still_keeps_the_reserve():
    """Codex review on #447: the reserve is checked when the job would be sent, after the
    replies queued in front of it, not when it was queued."""
    clock = FrozenClock()
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "DECOY_KEY", transport=fake.transport)
    client.budget.clock = clock
    poster = Poster(client, per_minute=6)  # a reserve of 2
    try:
        await client.register()  # one of the six
        housekeeping = asyncio.create_task(
            poster.background(lambda: client.request("GET", "users/me"))
        )
        replies = [asyncio.create_task(poster.send(1, "topic", f"reply {i}")) for i in range(3)]
        await asyncio.wait_for(asyncio.gather(*replies), 1)
        await asyncio.sleep(0.05)
        assert not housekeeping.done() and len(client.budget.sent) == 4
        for text in ("fifth", "sixth"):  # the reserve: neither waits for the window
            await asyncio.wait_for(poster.send(1, "topic", text), 1)
        assert not housekeeping.done() and clock.now == 1000.0
        clock.advance(61)
        await asyncio.wait_for(housekeeping, 5)
        assert [p for _, p, _ in fake.requests].count("users/me") == 1
    finally:
        clock.advance(3600)
        await poster.close()
        await client.close()


async def test_closing_drops_a_deferred_housekeeping_job():
    clock = FrozenClock()
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "DECOY_KEY", transport=fake.transport)
    client.budget.clock = clock
    poster = Poster(client, per_minute=3)  # a reserve of 1
    await client.register()
    await poster.send(1, "topic", "reply")  # two of three: housekeeping must wait
    housekeeping = asyncio.create_task(poster.background(lambda: client.request("GET", "users")))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(poster.close(), 1)
    with pytest.raises(RuntimeError):
        await housekeeping
    assert "users" not in [p for _, p, _ in fake.requests]
    await client.close()
