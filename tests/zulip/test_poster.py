import asyncio

import pytest

from zab.testing.fake_clock import FrozenClock
from zab.testing.fake_zulip import FakeZulip
from zab.zulip.client import GAP, MAX_EDITS, EditRefused, ZulipClient, ZulipError
from zab.zulip.poster import FINAL, STATUS, Poster

pytestmark = pytest.mark.integration


async def test_429_recovers_and_edits_coalesce():
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    mid = await client.send(1, "topic", "initial")
    poster = Poster(client)
    fake.rate_limit_once(0.01)
    edits = [asyncio.create_task(poster.edit(mid, str(n))) for n in range(10)]
    final = asyncio.create_task(poster.send(1, "topic", "final"))
    await asyncio.gather(*edits, final)
    assert fake.messages[mid]["content"] == "9"
    assert [m["content"] for m in fake.messages.values()].count("final") == 1
    assert len([r for r in fake.requests if r[0] == "PATCH"]) == 1
    await poster.close()
    await client.close()


async def test_failure_does_not_kill_worker_and_priority_is_respected():
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    poster = Poster(client)
    low = asyncio.create_task(poster.send(1, "topic", "status", STATUS))
    high = asyncio.create_task(poster.send(1, "topic", "final", FINAL))
    await asyncio.gather(low, high)
    assert fake.messages[1]["content"] == "final"
    fake.failures.append((400, {"code": "BAD_REQUEST"}))
    with pytest.raises(ZulipError):
        await poster.send(1, "topic", "bad")
    await poster.send(1, "topic", "still works")
    await poster.close()
    await client.close()


async def test_close_fails_pending_jobs_without_hanging():
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=FakeZulip().transport)
    poster = Poster(client)
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(poster.call(slow))
    await started.wait()
    await poster.close(drain=False)
    with pytest.raises(RuntimeError, match="stopped"):
        await task
    await client.close()


async def test_retry_preserves_same_priority_message_order():
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    poster = Poster(client)
    fake.rate_limit_once(0.01)
    await asyncio.gather(poster.send(1, "topic", "part 1"), poster.send(1, "topic", "part 2"))
    assert [m["content"] for m in fake.messages.values()] == ["part 1", "part 2"]
    await poster.close()
    await client.close()


async def test_coalesced_edit_promotes_priority_once():
    from zab.zulip.poster import ACK, APPROVAL

    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    mid = await client.send(1, "topic", "initial")
    poster = Poster(client)
    gate, started = asyncio.Event(), asyncio.Event()

    async def block():
        started.set()
        await gate.wait()

    blocker = asyncio.create_task(poster.call(block))
    await started.wait()
    low = asyncio.create_task(poster.edit(mid, "status", STATUS))
    ack = asyncio.create_task(poster.send(1, "topic", "ack", ACK))
    high = asyncio.create_task(poster.edit(mid, "approval", APPROVAL))
    await asyncio.sleep(0)
    fake.requests.clear()
    gate.set()
    await asyncio.gather(blocker, low, ack, high)
    await poster.close()
    writes = [(method, params.get("content")) for method, _, params in fake.requests]
    assert writes == [("PATCH", "approval"), ("POST", "ack")]
    await client.close()


async def test_promotion_during_second_throttle_never_writes_twice():
    from zab.zulip.poster import APPROVAL

    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    mid = await client.send(1, "topic", "initial")
    poster = Poster(client)
    blocked, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def throttle():
        nonlocal calls
        calls += 1
        if calls == 2:
            blocked.set()
            await release.wait()

    poster._throttle = throttle
    low = asyncio.create_task(poster.edit(mid, "status", STATUS))
    await blocked.wait()
    high = asyncio.create_task(poster.edit(mid, "approval", APPROVAL))
    await asyncio.sleep(0)
    fake.requests.clear()
    release.set()
    await asyncio.gather(low, high)
    await poster.close()
    assert [(m, p["content"]) for m, _, p in fake.requests] == [("PATCH", "approval")]
    await client.close()


async def test_upload_server_retries_are_bounded():
    from unittest.mock import AsyncMock

    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=FakeZulip().transport)
    poster = Poster(client)
    # No wall-clock waiting: still assert the actual requeue and terminal-failure behavior.
    poster._throttle = AsyncMock()
    operation = AsyncMock(side_effect=ZulipError("SERVER_ERROR", 503))
    with pytest.raises(ZulipError):
        await poster.call(operation, retry_server_errors=True)
    assert operation.await_count == 4
    await poster.close()
    await client.close()


# ---------------------------------------------- the edits of each message, bounded (#754)


async def bounded():
    """A poster on a still clock, and the time and content of each PATCH as it goes out."""
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    await client.register()
    mid = await client.send(1, "topic", "initial")
    client.budget.clock = clock = FrozenClock()
    sent: list[tuple[float, str]] = []
    request = client.http.request

    async def timed(method, url, **kwargs):
        if method == "PATCH":
            sent.append((clock.now, kwargs["data"]["content"]))
        return await request(method, url, **kwargs)

    client.http.request = timed
    return fake, client, Poster(client), mid, clock, sent


async def later(clock, seconds):
    """Let `seconds` pass, then the held edits that came due go out."""
    clock.advance(seconds)
    for _ in range(20):
        await asyncio.sleep(0)


async def test_edits_of_a_message_are_a_minute_apart_and_merged():
    fake, client, poster, mid, clock, sent = await bounded()
    assert await poster.edit(mid, "one")
    assert not await poster.edit(mid, "two")  # held back, without waiting here
    assert not await poster.edit(mid, "three")  # merged: the newest content wins
    assert len(poster.held) == 1 and fake.messages[mid]["content"] == "one"
    await later(clock, GAP - 1)
    assert fake.messages[mid]["content"] == "one"
    await later(clock, 1)
    assert fake.messages[mid]["content"] == "three"
    assert [t for t, _ in sent] == [1000.0, 1000.0 + GAP] and not poster.held
    await poster.close()
    await client.close()


async def test_a_final_edit_goes_at_once_in_place_of_the_held_one():
    fake, client, poster, mid, clock, sent = await bounded()
    await poster.edit(mid, "running")
    await poster.edit(mid, "a refresh")
    assert await poster.edit(mid, "done", FINAL, final=True)
    assert fake.messages[mid]["content"] == "done" and not poster.held and not poster.timers
    await later(clock, GAP)
    assert [c for _, c in sent] == ["running", "done"]
    await poster.close()
    await client.close()


async def test_a_message_gets_at_most_max_edits_the_final_one_included():
    fake, client, poster, mid, clock, sent = await bounded()
    for n in range(2 * MAX_EDITS):
        await poster.edit(mid, f"refresh {n}", paused=" (paused)")
        await later(clock, GAP)
    assert len(sent) == MAX_EDITS - 1  # one slot is kept for the final edit
    assert sent[-1][1] == f"refresh {MAX_EDITS - 2} (paused)"
    assert not any("paused" in c for _, c in sent[:-1]) and not poster.held
    assert await poster.edit(mid, "done", final=True)
    assert not await poster.edit(mid, "done again", final=True)
    assert [c for _, c in sent][-1] == fake.messages[mid]["content"] == "done"
    assert len(sent) == MAX_EDITS
    with pytest.raises(EditRefused):  # the client refuses whatever comes past the poster
        await client.edit(mid, "direct", final=True)
    await poster.close()
    await client.close()


async def test_an_edit_zulip_refused_does_not_count():
    fake, client, poster, mid, _clock, _sent = await bounded()
    fake.refused_edits.add(mid)
    with pytest.raises(ZulipError):
        await poster.edit(mid, "refused")
    assert client.edit_bounds.edits(mid) == 0
    fake.refused_edits.clear()
    assert await poster.edit(mid, "now") and await poster.edit(mid, "too soon") is False
    assert client.edit_bounds.edits(mid) == 1
    await poster.close()
    await client.close()


async def test_close_drops_a_held_edit():
    _fake, client, poster, mid, clock, sent = await bounded()
    await poster.edit(mid, "one")
    await poster.edit(mid, "two")
    await poster.close()
    await later(clock, GAP)
    assert [c for _, c in sent] == ["one"] and not poster.timers
    await client.close()


async def test_a_queued_final_edit_is_never_replaced_by_a_refresh():
    _fake, client, poster, mid, _clock, sent = await bounded()
    gate, started = asyncio.Event(), asyncio.Event()

    async def block():
        started.set()
        await gate.wait()

    blocker = asyncio.create_task(poster.call(block))
    await started.wait()
    final = asyncio.create_task(poster.edit(mid, "done", FINAL, final=True))
    await asyncio.sleep(0)
    assert not await poster.edit(mid, "a late refresh")
    gate.set()
    assert (await asyncio.gather(blocker, final))[1]
    assert [c for _, c in sent] == ["done"]
    await poster.close()
    await client.close()


async def test_a_held_edit_held_again_keeps_its_failure_callback():
    """Review of #755: a held edit that comes due while an edit sent since still has its gap to
    run (a 429's retry, sent after the hold began) is held again, with its `failed` callback,
    which a failure then calls."""
    fake, client, poster, mid, clock, sent = await bounded()
    calls = []
    await poster.edit(mid, "one")
    await poster.edit(mid, "two", failed=lambda: calls.append("failed"))  # held to 1060
    await later(clock, 30)
    client.edit_bounds.record(mid)  # an edit that went out at 1030, as a 429's retry may
    await later(clock, 30)
    assert mid in poster.held and [c for _, c in sent] == ["one"]  # held again, to 1090
    fake.offline = True
    await later(clock, 30)
    assert calls == ["failed"] and not poster.held
    await poster.close()
    await client.close()


async def test_the_gap_counts_from_the_send_after_a_budget_wait():
    """Codex round 3 (#756's review): an edit that waits 90 s for the request budget counts
    from when it is sent, not from when it was asked for, so the next ordinary edit still waits
    a whole gap after it."""
    _fake, client, poster, mid, clock, sent = await bounded()
    client.budget.blocked_until = clock.now + 90  # as a 429's Retry-After leaves it
    first = asyncio.create_task(client.edit(mid, "one"))
    await later(clock, 0)
    assert not sent  # waiting for the budget
    await later(clock, 90)
    await first
    assert [t for t, _ in sent] == [1090.0] and client.edit_bounds.wait(mid) == GAP
    with pytest.raises(EditRefused):
        await client.edit(mid, "two")
    assert not await poster.edit(mid, "two") and mid in poster.held  # held, a gap after it
    await poster.close()
    await client.close()
