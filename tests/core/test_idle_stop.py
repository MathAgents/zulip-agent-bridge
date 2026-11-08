"""`!stop` with no turn running (#847, SPEC §7.7), end to end offline: `BotBridge` on the fake
Zulip, Codex on `FakeAppServer` and Claude on `fake_claude.py`, with `test_fork.py`'s rigs.

A Codex turn can end while a sub-agent it spawned runs on (#660). `!stop` reached the backend
only while a turn ran: named, it answered "Unknown command", and unmentioned, nothing. Now
the backend stops those sub-agents, without loading the thread, and the bot says how many."""

from __future__ import annotations

import asyncio

import pytest
from test_fork import OWNER, make_rig, parent, restarts  # noqa: F401 - the rigs are fixtures

from backends.test_claude_steer import after_first_result, subagent_fixture
from zab.core.commands import NO_TURN, NO_TURN_NOR_SUBAGENT, UNKNOWN, stopped_subagents

pytestmark = pytest.mark.integration
STOP = "@**Test Bot** !stop"


def status(server, thread_id) -> str:
    return server.threads[thread_id]["turns"][-1]["status"]


async def left_running(server, native, until_ended) -> str:
    """A held turn of `native` spawns a sub-agent and ends while it runs: the sub-agent."""
    async with asyncio.timeout(30):
        while not any(t["status"] == "inProgress" for t in server.threads[native]["turns"]):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    child = await server.spawn(native)
    await server.finish(native, "Started a sub-agent for the check.")
    await until_ended()
    return child


async def test_a_codex_stop_with_no_turn_stops_the_subagent_left_running(make_rig):  # noqa: F811
    rig = await make_rig("codex")
    native = (await parent(rig))["native_id"]
    rig.server.hold = True
    await rig.say("@**Test Bot** check it in a sub-agent")
    child = await left_running(rig.server, native, lambda: rig.turns(2))
    assert "send !stop to stop it" in rig.posts("topic")[-1]  # the note (#660) says how
    await rig.say(STOP)
    await rig.until(lambda: stopped_subagents(1) in rig.posts("topic"), "the stop's report")
    assert status(rig.server, child) == "interrupted"
    assert [p["threadId"] for p in rig.server.requests("turn/interrupt")] == [child]
    await rig.say("!stop")  # unmentioned, in a VM of one bot: the same answer
    await rig.until(lambda: rig.posts("topic")[-1] == NO_TURN_NOR_SUBAGENT, "nothing left")
    assert UNKNOWN not in rig.posts("topic") and len(rig.ended()) == 2


@pytest.mark.parametrize("listed", [True, False])
async def test_a_codex_stop_reaches_a_thread_reopened_unloaded(restarts, listed):  # noqa: F811
    """The bridge restarts while the sub-agent runs, and the daemon unloads the thread, which
    no connection held (`FakeAppServer.unload`): the session reopens without loading it
    (#632). Whether `thread/list` lists the sub-agent or, as live, none (#858), the stop
    finds it among the loaded threads, and the thread stays unloaded."""
    rig = await restarts("codex")
    runtime = await rig.start()
    rig.server.hold = True
    rig.fake.post_as(OWNER, "@**Test Bot** check it in a sub-agent", "topic")
    async with asyncio.timeout(30):
        while not rig.server.threads:  # noqa: ASYNC110 - the runtime's own listener
            await asyncio.sleep(0.05)
    native = next(iter(rig.server.threads))
    child = await left_running(rig.server, native, lambda: rig.turns(1))
    await rig.stop()
    rig.server.unload(native)
    runtime = await rig.start()
    backend = runtime.bridges[0].backend
    async with asyncio.timeout(30):
        while native not in backend.mapper.closed:  # noqa: ASYNC110 - reopened by the runtime
            await asyncio.sleep(0.05)
    loads = list(rig.server.loads)
    rig.server.lists_subagents = listed
    rig.fake.post_as(OWNER, STOP, "topic")
    answer = stopped_subagents(1)
    async with asyncio.timeout(30):
        while answer not in rig.posts("topic"):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    assert status(rig.server, child) == "interrupted"
    assert native not in rig.server.loaded and rig.server.loads == loads


async def test_a_claude_stop_with_no_turn_says_so(make_rig):  # noqa: F811
    """Claude has no sub-agents to stop once its turn has ended: they belong to the turn's
    process (§9.7). `!stop` answers "No turn is running", named or not."""
    rig = await make_rig("claude")
    await parent(rig)
    for n, text in enumerate((STOP, "!stop"), 1):
        await rig.say(text)
        await rig.until(lambda n=n: rig.posts("topic").count(NO_TURN) == n, text)
    assert UNKNOWN not in rig.posts("topic") and len(rig.ended()) == 1


async def test_a_claude_turn_kept_open_for_a_subagent_is_stopped_as_a_turn(
    make_rig,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """After #814 a Claude turn whose background sub-agent outlives its `result` keeps
    running (stdin open, §9.7): `!stop` there interrupts the turn, its process group holding
    the sub-agent, and never takes the path of a stop with no turn."""
    rig = await make_rig("claude")
    native = (await parent(rig))["native_id"]
    fixture = subagent_fixture(tmp_path / "subagent.jsonl", steer=True, never_ends=True)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(fixture))
    await rig.say("@**Test Bot** start a background sub-agent")
    await after_first_result(rig.backend, native)
    assert rig.row()["state"] == "running"
    await rig.say(STOP)
    await rig.until(lambda: len(rig.ended()) == 2, "the stopped turn")
    assert rig.ended()[-1]["state"] == "interrupted"
    assert NO_TURN not in rig.posts("topic") and UNKNOWN not in rig.posts("topic")
