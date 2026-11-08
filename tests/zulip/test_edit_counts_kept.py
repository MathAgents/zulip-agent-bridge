"""Each message's edits, kept in the store across restarts (#757; SPEC §4.9, §6.2; D41)."""

import asyncio
import sqlite3

import pytest

from zab.core.store import Store
from zab.testing.fake_clock import FrozenClock
from zab.testing.fake_zulip import FakeZulip
from zab.zulip import client as module
from zab.zulip.client import GAP, MAX_EDITS, EditRefused, ZulipClient, ZulipError
from zab.zulip.poster import Poster

pytestmark = pytest.mark.integration


async def started(fake, store, clock):
    """A bridge process's client: its clock, read as the budget's, its edits kept in `store`."""
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    client.budget.clock = clock
    client.edit_bounds.keep(lambda: store, "bot")
    await client.register()
    return client


def kept(store):
    return {r["message_id"]: (r["edits"], r["edited_at"]) for r in store.rows("message_edits")}


async def test_a_restart_after_a_reboot_keeps_each_messages_count_and_gap(tmp_path):
    fake, store = FakeZulip(), Store(tmp_path)
    clock = FrozenClock(1000.0, boot=900.0)
    first = await started(fake, store, clock)
    mid = await first.send(1, "topic", "initial")
    for n in range(MAX_EDITS - 1):
        if n:
            clock.advance(GAP)
        await first.edit(mid, f"edit {n}")
    last = clock.now
    assert kept(store) == {mid: (MAX_EDITS - 1, last)}
    await first.close()
    store.close()

    # 30 s later, after a reboot: the monotonic clock began again, near 0
    store, clock = Store(tmp_path), FrozenClock(last + 30, boot=last + 25)
    second = await started(fake, store, clock)
    bounds = second.edit_bounds
    assert bounds.edits(mid) == MAX_EDITS - 1
    assert bounds.wait(mid, final=True) == 0 and bounds.wait(mid) is None  # the cap holds
    other = await second.send(1, "topic", "another")
    await second.edit(other, "edited")
    assert bounds.wait(other) == GAP
    await second.close()
    store.close()

    # and the gap: a message edited once, 30 s before the restart, waits 30 s more
    store, clock = Store(tmp_path), FrozenClock(clock.now + 30, boot=clock.now + 29)
    third = await started(fake, store, clock)
    assert third.edit_bounds.edits(other) == 1 and third.edit_bounds.wait(other) == GAP - 30
    with pytest.raises(EditRefused):
        await third.edit(other, "too soon")
    clock.advance(GAP - 30)
    await third.edit(other, "now")
    assert fake.messages[other]["content"] == "now" and kept(store)[other] == (2, clock.now)
    await third.close()
    store.close()


async def test_a_last_edit_later_than_now_counts_as_now(tmp_path):
    """A wall clock set back between two runs: the gap counts from the start, never longer."""
    fake, store = FakeZulip(), Store(tmp_path)
    first = await started(fake, store, FrozenClock(5000.0))
    mid = await first.send(1, "topic", "initial")
    await first.edit(mid, "one")
    await first.close()
    second = await started(fake, store, FrozenClock(4000.0, boot=3990.0))
    assert second.edit_bounds.wait(mid) == GAP
    await second.close()
    store.close()


async def test_one_write_per_edit_sent_and_none_per_edit_refused(tmp_path):
    fake, store = FakeZulip(), Store(tmp_path)
    clock = FrozenClock()
    client = await started(fake, store, clock)
    poster = Poster(client)
    mid = await client.send(1, "topic", "initial")
    writes = store.db.total_changes
    assert await poster.edit(mid, "one")
    assert store.db.total_changes == writes + 1
    writes = store.db.total_changes
    for n in range(50):  # held back, merged, refused: none kept
        assert not await poster.edit(mid, f"too soon {n}")
        with pytest.raises(EditRefused):
            await client.edit(mid, "direct")
    assert store.db.total_changes == writes and kept(store)[mid][0] == 1
    client.edit_bounds.messages[mid] = (MAX_EDITS, 0.0)  # its cap reached: dropped
    assert not await poster.edit(mid, "past the cap") and not poster.held
    assert store.db.total_changes == writes
    await poster.close()
    await client.close()
    store.close()


async def test_an_edit_zulip_refused_is_not_kept(tmp_path):
    fake, store = FakeZulip(), Store(tmp_path)
    clock = FrozenClock()
    client = await started(fake, store, clock)
    mid = await client.send(1, "topic", "initial")
    fake.refused_edits.add(mid)
    with pytest.raises(ZulipError):
        await client.edit(mid, "refused")
    assert kept(store) == {}
    fake.refused_edits.clear()
    await client.edit(mid, "one")
    clock.advance(GAP + 5)
    fake.refused_edits.add(mid)
    with pytest.raises(ZulipError):
        await client.edit(mid, "refused again")
    assert kept(store) == {mid: (1, 1000.0)} and client.edit_bounds.wait(mid) == 0
    await client.close()
    store.close()


async def test_the_store_keeps_what_memory_keeps(tmp_path, monkeypatch):
    """The `KEPT` messages edited last, for each bot: a message edited after them forgets the
    least recently edited one, in memory and in the store, and a restart takes up no more."""
    monkeypatch.setattr(module, "KEPT", 3)
    fake, store = FakeZulip(), Store(tmp_path)
    clock = FrozenClock()
    client = await started(fake, store, clock)
    mids = [await client.send(1, "topic", f"m{n}") for n in range(4)]
    for mid in mids:
        await client.edit(mid, "edited")
        clock.advance(1)
    assert list(kept(store)) == list(client.edit_bounds.messages) == mids[1:]
    store.keep_edit_count("other-bot", mids[0], 1, clock.now)  # another bot's: never touched
    await client.close()
    monkeypatch.setattr(module, "KEPT", 2)
    again = await started(fake, store, clock)
    assert list(again.edit_bounds.messages) == mids[2:]
    assert sorted(kept(store)) == sorted([mids[0], *mids[2:]])
    assert store.edit_counts("other-bot") == [(mids[0], 1, clock.now, False)]
    await again.close()
    store.close()


async def test_a_message_forgotten_leaves_the_store_but_not_memory(tmp_path):
    fake, store = FakeZulip(), Store(tmp_path)
    client = await started(fake, store, FrozenClock())
    mid, never = await client.send(1, "topic", "edited"), await client.send(1, "topic", "not")
    await client.edit(mid, "once")
    client.edit_bounds.forget(mid)
    assert kept(store) == {} and client.edit_bounds.edits(mid) == 1
    writes = store.db.total_changes
    client.edit_bounds.forget(never)  # never edited: no write
    client.edit_bounds.forget(None)
    assert store.db.total_changes == writes
    await client.close()
    store.close()


async def test_an_edit_the_store_cannot_keep_is_not_sent(tmp_path):
    fake, store = FakeZulip(), Store(tmp_path)
    client = await started(fake, store, FrozenClock())
    mid = await client.send(1, "topic", "initial")
    store.db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        await client.edit(mid, "unkept")
    await asyncio.sleep(0)
    assert fake.messages[mid]["content"] == "initial"
    assert client.edit_bounds.edits(mid) == 1  # counted all the same: on the safe side
    await client.close()


async def test_a_repeated_final_edit_waits_for_its_gap_across_a_restart(tmp_path):
    """#806: the first final edit goes at once; once it went out, a repeat (the summary sent
    again at the next start of a bridge that dies delivering its answer) waits for the gap like
    any other edit. The store keeps that it went out."""
    fake, store = FakeZulip(), Store(tmp_path)
    clock = FrozenClock(1000.0)
    first = await started(fake, store, clock)
    mid = await first.send(1, "topic", "initial")
    await first.edit(mid, "working")
    await first.edit(mid, "done", final=True)  # at once, 0 s after the last
    assert store.edit_counts("bot") == [(mid, 2, 1000.0, True)]
    with pytest.raises(EditRefused):
        await first.edit(mid, "done", final=True)  # a repeat in the same process
    await first.close()
    clock = FrozenClock(1010.0, boot=1005.0)  # restarted 10 s later, after a reboot
    second = await started(fake, store, clock)
    poster = Poster(second)
    assert second.edit_bounds.wait(mid, final=True) == GAP - 10
    assert not await poster.edit(mid, "done again", final=True)
    assert not await poster.edit(mid, "a refresh")  # never replaces the held final edit
    assert poster.held[mid][0] == "done again" and fake.messages[mid]["content"] == "done"
    clock.advance(GAP - 10)
    for _ in range(20):
        await asyncio.sleep(0)
    assert fake.messages[mid]["content"] == "done again" and not poster.held
    assert store.edit_counts("bot") == [(mid, 3, 1060.0, True)]
    await poster.close()
    await second.close()
    store.close()


async def test_a_final_edit_zulip_refused_leaves_none_sent(tmp_path):
    fake, store = FakeZulip(), Store(tmp_path)
    client = await started(fake, store, FrozenClock())
    mid = await client.send(1, "topic", "initial")
    await client.edit(mid, "working")
    fake.refused_edits.add(mid)
    with pytest.raises(ZulipError):
        await client.edit(mid, "done", final=True)
    assert mid not in client.edit_bounds.finals and not store.edit_counts("bot")[0][3]
    fake.refused_edits.clear()
    await client.edit(mid, "done", final=True)  # still the first: at once
    assert mid in client.edit_bounds.finals
    await client.close()
    store.close()


async def test_a_summary_posted_anew_counts_as_its_final(tmp_path):
    """A summary posted as a new status message (its old one's edits spent, SPEC §4.9) went
    out with the post: the new message's repeat waits for its gap, and counts no edit."""
    fake, store = FakeZulip(), Store(tmp_path)
    clock = FrozenClock()
    client = await started(fake, store, clock)
    mid = await client.send(1, "topic", "✅ Done")
    client.edit_bounds.sent(mid)
    assert store.edit_counts("bot") == [(mid, 0, 1000.0, True)]
    assert client.edit_bounds.wait(mid, final=True) == GAP and client.edit_bounds.edits(mid) == 0
    await client.close()
    store.close()


async def a_final_sent_then_one_held(tmp_path, clock):
    """A message whose final edit went out (counted, whether it landed or not), then a repeat
    of it held back for its gap, 10 s later: the fake, store, client, poster and message."""
    fake, store = FakeZulip(), Store(tmp_path)
    client = await started(fake, store, clock)
    poster = Poster(client)
    mid = await client.send(1, "topic", "⏳ Working")
    assert await poster.edit(mid, "✅ Done", final=True)
    clock.advance(10)
    assert not await poster.edit(mid, "✅ Done again", final=True)
    assert store.held_finals("bot") == [(mid, "✅ Done again")]
    return fake, store, client, poster, mid


async def settled(clock, seconds):
    clock.advance(seconds)
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_held_final_edit_that_fails_is_tried_again_a_gap_later(tmp_path):
    """Codex round 2 on #813: a repeat held back may be the only one that lands, as when the
    first was counted but never reached Zulip. One that fails, and may have landed, is tried
    again a gap later, and kept in the store until it is sent."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    fake.offline = True
    await settled(clock, GAP - 10)  # it goes, and fails
    assert client.edit_bounds.edits(mid) == 2 and mid in poster.held
    assert store.held_finals("bot") == [(mid, "✅ Done again")]
    fake.offline = False
    await settled(clock, GAP - 1)
    assert fake.messages[mid]["content"] == "✅ Done"  # not before its gap
    await settled(clock, 1)
    assert fake.messages[mid]["content"] == "✅ Done again" and not poster.held
    assert client.edit_bounds.edits(mid) == 3 and store.held_finals("bot") == []
    await poster.close()
    await client.close()
    store.close()


async def test_a_held_final_edit_survives_a_shutdown(tmp_path):
    """Codex round 2 on #813: closing drops the held edits, but a final one stays in the store,
    and the next start sends it once its gap has passed (`Poster.resume`)."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    await poster.close()
    await client.close()
    assert store.held_finals("bot") == [(mid, "✅ Done again")]
    clock = FrozenClock(clock.now + 20, boot=clock.now + 15)  # a restart 20 s later
    again = await started(fake, store, clock)
    poster = Poster(again)
    for message_id, content in again.edit_bounds.held_finals():
        poster.resume(message_id, content)
    await settled(clock, GAP - 31)
    assert fake.messages[mid]["content"] == "✅ Done" and mid in poster.held
    await settled(clock, 1)
    assert fake.messages[mid]["content"] == "✅ Done again" and store.held_finals("bot") == []
    await poster.close()
    await again.close()
    store.close()


async def test_a_held_final_edit_zulip_refuses_is_dropped(tmp_path):
    """A 4xx: it never landed and never will (a message deleted, say). It is released, not
    tried again."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    fake.refused_edits.add(mid)
    await settled(clock, GAP - 10)
    assert not poster.held and store.held_finals("bot") == []
    fake.refused_edits.clear()
    await settled(clock, 2 * GAP)
    assert fake.messages[mid]["content"] == "✅ Done"
    await poster.close()
    await client.close()
    store.close()


async def test_a_message_forgotten_while_its_final_is_held_goes_once_it_is_sent(tmp_path):
    """The turn ends, its answer delivered, while a repeat of its summary is held: the counts
    stay until that is sent, so that a restart still finds it, then go."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    client.edit_bounds.forget(mid)
    assert store.edit_counts("bot")[0][0] == mid and store.held_finals("bot")
    await settled(clock, GAP - 10)
    assert fake.messages[mid]["content"] == "✅ Done again"
    assert store.edit_counts("bot") == [] and store.rows("message_finals") == []
    await poster.close()
    await client.close()
    store.close()


async def test_a_final_edit_that_takes_a_held_ones_place_releases_it(tmp_path):
    """Review round 1 of #813, B2 (a): a final edit that comes once the gap has passed, before
    the held one's timer runs, goes at once in its place. Once it lands, nothing is owed: the
    next start sends nothing, and the message keeps the newer text."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    clock.advance(GAP - 10)  # the gap has passed; the timer hasn't run yet
    assert await poster.edit(mid, "✅ Done, newer", final=True)
    await settled(clock, 0)
    assert fake.messages[mid]["content"] == "✅ Done, newer" and not poster.held
    assert store.held_finals("bot") == [] and not client.edit_bounds.pending
    await poster.close()
    await client.close()
    clock = FrozenClock(clock.now + 3600, boot=clock.now + 3500)  # the next start, an hour on
    again = await started(fake, store, clock)
    assert again.edit_bounds.held_finals() == []
    await again.close()
    store.close()


async def test_a_retry_after_a_slow_failure_releases_the_held_final_edit(tmp_path):
    """B2 (a), its likely path: the held final edit's PATCH fails after a 30 s timeout, so it is
    held again a gap after the failure, while its own gap counts from the try. A retry of the
    summary in between (a look, `!status`) goes at once, and landing releases it."""
    import httpx

    fake, store, clock = FakeZulip(), Store(tmp_path), FrozenClock()
    slow = {"armed": False}

    async def handle(request):
        if request.method == "PATCH" and slow["armed"]:
            slow["armed"] = False
            await clock.sleep(30)
            raise httpx.ReadTimeout("slow", request=request)
        return await fake.handle(request)

    client = ZulipClient(
        "https://zulip.example", "bot", "decoy", transport=httpx.MockTransport(handle)
    )
    client.budget.clock = clock
    client.edit_bounds.keep(lambda: store, "bot")
    await client.register()
    poster = Poster(client)
    mid = await client.send(1, "topic", "⏳ Working")
    assert await poster.edit(mid, "✅ Done", final=True)
    clock.advance(10)
    assert not await poster.edit(mid, "✅ Done again", final=True)
    slow["armed"] = True
    await settled(clock, GAP - 10)  # it goes at 60 s, and hangs
    await settled(clock, 30)  # it fails at 90 s: held again, due at 150 s
    assert mid in poster.held
    await settled(clock, 35)  # 125 s: the gap from the try has passed
    assert await poster.edit(mid, "✅ Done again", final=True)
    await settled(clock, 2 * GAP)
    assert fake.messages[mid]["content"] == "✅ Done again"
    assert store.held_finals("bot") == [] and not client.edit_bounds.pending
    await poster.close()
    await client.close()
    store.close()


async def test_a_refresh_never_takes_a_held_final_edits_place(tmp_path):
    """B2 (b): a refresh that isn't final, once the gap has passed and before the held final
    edit's timer runs, is dropped: the held summary goes, and is released."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    clock.advance(GAP - 10)
    assert not await poster.edit(mid, "⏳ reconnecting")
    await settled(clock, 2 * GAP)
    assert fake.messages[mid]["content"] == "✅ Done again"
    assert store.held_finals("bot") == [] and not client.edit_bounds.pending
    await poster.close()
    await client.close()
    store.close()


async def test_a_final_edit_that_landed_releases_only_the_one_held(tmp_path):
    """One that landed releases the repeat held only if it is that repeat, not a newer one
    held back since: that one is still owed."""
    store = Store(tmp_path)
    client = await started(FakeZulip(), store, FrozenClock())
    bounds = client.edit_bounds
    bounds.hold_final(10, "newer")
    bounds.release_final(10, "older")
    assert bounds.pending == {10: "newer"} and store.held_finals("bot") == [(10, "newer")]
    bounds.release_final(10, "newer")
    assert not bounds.pending and store.held_finals("bot") == []
    await client.close()
    store.close()


async def test_a_final_edit_that_replaces_a_queued_repeat_is_the_one_owed(tmp_path):
    """Codex round 3 on #813: the held repeat's timer queues it, and a newer final edit replaces
    the queued job before it is sent. The store must owe the newer one: once it lands nothing
    is owed, and the next start replays nothing older over it."""
    clock = FrozenClock()
    fake, store, client, poster, mid = await a_final_sent_then_one_held(tmp_path, clock)
    gate, busy = asyncio.Event(), asyncio.Event()

    async def block():
        busy.set()
        await gate.wait()

    blocker = asyncio.create_task(poster.call(block))  # the worker is busy meanwhile
    await busy.wait()
    await settled(clock, GAP - 10)  # the timer queues the held repeat
    assert mid in poster.edits and not poster.held
    newer = asyncio.create_task(poster.edit(mid, "✅ Done, newer", final=True))
    await settled(clock, 0)
    assert client.edit_bounds.pending == {mid: "✅ Done, newer"}
    gate.set()
    await blocker
    assert await newer
    assert fake.messages[mid]["content"] == "✅ Done, newer"
    assert not client.edit_bounds.pending and store.held_finals("bot") == []
    await poster.close()
    await client.close()
    store.close()
