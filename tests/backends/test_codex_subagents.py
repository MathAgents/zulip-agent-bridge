"""Codex sub-agents across a turn's end (#660), against the scripted app-server `FakeAppServer`
(SPEC §15.2), whose wire is checked against the pinned 0.157.1 schemas; Codex itself never runs
on the development machine. Case 2: a sub-agent still running when its parent's turn ends is
reported in that turn's answer, not lost without a word (DECISIONS, 2026-10-25, B2)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from zab.backends import codex as cx
from zab.backends.codex import CodexBackend, remember_identity
from zab.backends.codex_rpc import RpcError
from zab.config import BackendConfig
from zab.core import types as t
from zab.testing.fake_codex import FakeAppServer

pytestmark = pytest.mark.contract
SCHEMAS = Path(__file__).resolve().parents[2] / "docs/protocol/codex-0.157.1"


@pytest.fixture
async def codex(tmp_path):
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True  # each turn runs until the test finishes it
    path = str(tmp_path / "s.sock")
    async with fake.serve(path):
        backend = CodexBackend(
            "c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path)
        )
        await backend.start()
        await asyncio.wait_for(backend.ready.wait(), 5)
        try:
            yield fake, backend
        finally:
            await backend.close()


async def until(backend, cls, sid):
    async with asyncio.timeout(5):
        while True:
            event = await backend.queue.get()
            if isinstance(event, cls) and event.session_id == sid:
                return event


async def started(fake, backend, tmp_path):
    sid = (await backend.create_session(t.SessionSpec(cwd=str(tmp_path), name="n"))).native_id
    tid = await backend.start_turn(sid, t.UserInput(text="check it", client_id="c1"))
    await until(backend, t.TurnStarted, sid)
    return sid, tid


def valid_wire(fake):
    notifications = Draft7Validator(json.loads((SCHEMAS / "ServerNotification.json").read_text()))
    for direction, message in fake.wire:
        if direction == "receive" and "method" in message and "id" not in message:
            notifications.validate(message)


async def test_a_subagent_left_running_is_reported_in_the_answer(codex, tmp_path):
    """The parent spawns a sub-agent and ends its turn while it runs: Codex starts no turn when
    the sub-agent finishes, so its answer used to be lost without a word. The turn's answer now
    says that a sub-agent is still running and how to reach it."""
    fake, backend = codex
    sid, tid = await started(fake, backend, tmp_path)
    await fake.spawn(sid)
    kinds = []
    while t.ItemKind.SUBAGENT not in kinds:  # the prompt's userMessage, then the spawn
        kinds.append((await until(backend, t.ItemCompleted, sid)).item.kind)
    await fake.finish(sid, "I started a sub-agent for the check.")
    done = await until(backend, t.TurnCompleted, sid)
    assert done.turn_id == tid and done.status is t.TurnStatus.COMPLETED
    assert done.final_text == (
        "I started a sub-agent for the check.\n\n⚠️ **Bridge** (not Codex): A sub-agent was still "
        "running when this turn ended. Its answer won't be posted here: ask Codex about it in a "
        "new message, or send !stop to stop it."
    )
    valid_wire(fake)


async def test_no_note_once_the_subagent_is_done(codex, tmp_path):
    """A sub-agent that finished in the turn (the parent's `subAgentActivity` `completed`, and
    its own `turn/completed`) ends the turn without the note; so does a turn that started none.
    The spawn shows in the status as a sub-agent step."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    spawned = await until(backend, t.ItemStarted, sid)
    assert spawned.item.kind is t.ItemKind.SUBAGENT
    assert spawned.item.title == f"sub-agent /root/{child} started"
    await fake.finish(child, "rank 1")
    await fake.finish(sid, "It found rank 1.")
    assert (await until(backend, t.TurnCompleted, sid)).final_text == "It found rank 1."
    await backend.start_turn(sid, t.UserInput(text="thanks", client_id="c2"))
    await fake.finish(sid, "You're welcome.")
    assert (await until(backend, t.TurnCompleted, sid)).final_text == "You're welcome."
    valid_wire(fake)


async def test_a_subagent_that_ends_after_its_parent_is_not_noted_again(codex, tmp_path):
    """The case B2 saw: the parent's turn ends first, with the note; the sub-agent's completion
    comes later, under the spawn's turn id, and the parent's next turn has no note."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await fake.finish(sid, "Started it.")
    assert (await until(backend, t.TurnCompleted, sid)).final_text.startswith("Started it.\n\n⚠️")
    await fake.finish(child, "rank 1")
    await backend.start_turn(sid, t.UserInput(text="and now?", client_id="c2"))
    await fake.finish(sid, "It found rank 1.")
    assert (await until(backend, t.TurnCompleted, sid)).final_text == "It found rank 1."
    valid_wire(fake)


async def test_a_message_to_a_finished_subagent_is_no_running_one(codex, tmp_path):
    """Codex round 1 on #822: a `send_message` to a sub-agent that finished queues the message
    and starts nothing; its `interacted` item used to mark it running, and every later turn got
    the note. A `followup_task` starts it again, with a turn of its own: noted while it runs."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await fake.finish(child, "rank 1")
    await fake.message(child)
    await fake.finish(sid, "It found rank 1; I sent it a thank-you.")
    assert (await until(backend, t.TurnCompleted, sid)).final_text.endswith("thank-you.")
    await backend.start_turn(sid, t.UserInput(text="and the torsion?", client_id="c2"))
    await fake.finish(sid, "Trivial.")
    assert (await until(backend, t.TurnCompleted, sid)).final_text == "Trivial."
    await backend.start_turn(sid, t.UserInput(text="ask it to check again", client_id="c3"))
    await fake.message(child, follow_up=True)
    await fake.finish(sid, "Asked it again.")
    done = await until(backend, t.TurnCompleted, sid)
    assert done.final_text.startswith("Asked it again.\n\n⚠️ **Bridge** (not Codex): A sub-agent")
    valid_wire(fake)


async def test_a_daemon_restart_ends_the_note(tmp_path):
    """Review of #822, B2: the daemon dies while a sub-agent runs, so its end never comes. With
    the old state kept across the reconnection, every later turn of the thread said a
    sub-agent was still running. The state is now forgotten at a new connection."""
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True
    path = str(tmp_path / "s.sock")
    backend = CodexBackend("c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path))
    spec = t.SessionSpec(cwd=str(tmp_path), name="n")
    try:
        async with fake.serve(path):
            await backend.start()
            await asyncio.wait_for(backend.ready.wait(), 5)
            sid, _tid = await started(fake, backend, tmp_path)
            child = await fake.spawn(sid)
            await fake.finish(sid, "Started it.")
            assert "⚠️" in (await until(backend, t.TurnCompleted, sid)).final_text
        fake.restart()  # nothing loaded, and the child's turn died with the daemon
        assert fake.threads[child]["turns"][-1]["status"] == "interrupted"
        async with fake.serve(path):
            while not (await until(backend, t.BackendStatus, "")).up:  # down, then up again
                pass
            await backend.open_session(sid, spec)
            assert sid in backend.mapper.closed  # reopened unloaded: the next turn loads it
            for n in (2, 3):
                await backend.start_turn(sid, t.UserInput(text=f"q{n}", client_id=f"c{n}"))
                await fake.finish(sid, f"answer {n}")
                assert (await until(backend, t.TurnCompleted, sid)).final_text == f"answer {n}"
    finally:
        await backend.close()


# ------------------------------------------------------------- case 3: `!stop` and sub-agents


def stops(fake) -> list[str]:
    return [p["threadId"] for p in fake.requests("turn/interrupt")]


def status(fake, thread_id) -> str:
    return fake.threads[thread_id]["turns"][-1]["status"]


async def seen_running(backend, sid, child):
    """Once the backend has the child's turn: its events come to every connection."""
    async with asyncio.timeout(5):
        while True:
            if child in backend.mapper.running_subagents(sid) and child in backend.mapper.active:
                return
            await asyncio.sleep(0.01)


async def test_a_stop_interrupts_the_running_subagents_first(codex, tmp_path):
    """#660 case 3: `turn/interrupt` stopped the thread's turn but not its sub-agent, which ran
    on (DECISIONS, 2026-10-26). `!stop` now interrupts the sub-agents' turns too, at any depth,
    before the thread's own, whose answer then has no note that one still runs."""
    fake, backend = codex
    sid, tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    grandchild = await fake.spawn(child)
    await seen_running(backend, child, grandchild)
    await seen_running(backend, sid, child)
    await backend.interrupt(sid)
    done = await until(backend, t.TurnCompleted, sid)
    assert done.turn_id == tid and done.status is t.TurnStatus.INTERRUPTED
    assert done.final_text is None  # no note: nothing left running
    assert stops(fake) == [child, grandchild, sid]
    assert [status(fake, x) for x in (child, grandchild)] == ["interrupted", "interrupted"]
    assert "thread/loaded/list" in [c["method"] for c in fake.calls]  # then, untracked ones
    valid_wire(fake)


async def test_a_stop_finds_the_subagents_a_restart_left_untracked(codex, tmp_path):
    """A bridge restart loses what the events said, and the new connection hears nothing of a
    running sub-agent: the daemon's loaded threads hold it, its read (`thread/read`) gives its
    `parentThreadId` and `status`, and its newest turn in progress (`thread/turns/list`) is
    interrupted, after the thread's own (#858)."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await seen_running(backend, sid, child)
    forget(backend, child)
    await backend.interrupt(sid)
    assert (await until(backend, t.TurnCompleted, sid)).status is t.TurnStatus.INTERRUPTED
    assert stops(fake) == [sid, child] and status(fake, child) == "interrupted"
    assert fake.requests("thread/loaded/list") and not fake.requests("thread/list")
    assert child in [p["threadId"] for p in fake.requests("thread/read")]
    assert fake.requests("thread/turns/list")[-1]["threadId"] == child


@pytest.mark.parametrize("restarted", [False, True])
async def test_a_stop_reaches_a_running_grandchild_under_an_idle_child(codex, tmp_path, restarted):
    """Codex on #825: root → child → grandchild, the child done while the grandchild runs.
    Both ways of finding sub-agents skipped the idle child, so the stop never reached the
    grandchild. The stop now follows every edge, and checks activity only to interrupt: from
    what the events told (the grandchild first), or after a restart from the loaded threads, where
    the child is listed `idle` (after the thread's own stop)."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    grandchild = await fake.spawn(child)
    await seen_running(backend, child, grandchild)
    await fake.finish(child, "my part is done")
    async with asyncio.timeout(5):
        while True:  # until its turn/completed is in
            if child not in backend.mapper.active:
                break
            await asyncio.sleep(0.01)
    if restarted:
        forget(backend, grandchild)
    await backend.interrupt(sid)
    assert (await until(backend, t.TurnCompleted, sid)).status is t.TurnStatus.INTERRUPTED
    assert stops(fake) == ([sid, grandchild] if restarted else [grandchild, sid])
    assert status(fake, grandchild) == "interrupted" and status(fake, child) == "completed"
    valid_wire(fake)


async def test_a_stop_follows_edges_without_looping_or_going_too_deep(codex, tmp_path):
    """The edges come from what the daemon said: a cycle among them is walked once, and the
    walk stops `SUBAGENT_MAX_DEPTH` (8) levels down. Only a sub-agent with a turn running is
    interrupted."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    chain = [sid, *(f"c{i}" for i in range(1, 11))]
    edges = {p: {c: "completed"} for p, c in itertools.pairwise(chain)}
    edges |= {"c2": {"c3": "completed", "a": "completed"}, "a": {"b": "completed"}}
    edges |= {"b": {"a": "completed", sid: "completed"}}
    backend.mapper.subagents.update(edges)
    backend.mapper.active.update({"b": "turn-b", "c8": "turn-c8", "c9": "turn-c9"})
    await backend.interrupt(sid)
    assert (await until(backend, t.TurnCompleted, sid)).status is t.TurnStatus.INTERRUPTED
    assert stops(fake) == ["b", "c8", sid]  # breadth first; c9 is 9 levels down


def forget(backend, *threads):
    """What a bridge restart loses: the events' sub-agents and turns in progress."""
    backend.mapper.subagents.clear()
    for thread in threads:
        backend.mapper.active.pop(thread, None)


@pytest.mark.parametrize("parent", ["ended", "refused"])
async def test_the_listing_runs_when_the_threads_own_turn_is_over(codex, tmp_path, parent):
    """Codex round 2 on #825: the daemon's pass ran only when the thread still had a turn
    once the first pass was done, and not after a refused interrupt; so a sub-agent a restart
    left untracked ran on if the thread's turn ended meanwhile, or the stop came just as it
    ended. The pass now always runs, and a refusal is raised after it."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await seen_running(backend, sid, child)
    forget(backend, child)
    if parent == "ended":
        await fake.finish(sid, "Started it.")
        await until(backend, t.TurnCompleted, sid)
        await backend.interrupt(sid)
        assert stops(fake) == [child]
    else:
        fake.refuse_interrupt.add(sid)
        with pytest.raises(RpcError, match="no active turn"):
            await backend.interrupt(sid)
        assert stops(fake) == [sid, child]
    assert status(fake, child) == "interrupted"


async def test_the_loaded_threads_are_read_every_page(codex, tmp_path, monkeypatch):
    """Codex round 2 on #825, for the loaded threads (#858): one page could leave out an
    untracked sub-agent of this thread behind the loaded threads of others. Every page is
    read (`nextCursor`) before the walk."""
    monkeypatch.setattr(cx, "SUBAGENT_LIST_LIMIT", 2)
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    other, _ = await started(fake, backend, tmp_path)
    for _ in range(3):  # sub-agent threads of another thread: loaded before this one's
        await fake.finish(await fake.spawn(other), "done")
    child = await fake.spawn(sid)
    forget(backend, child)
    await backend.interrupt(sid)
    assert status(fake, child) == "interrupted"
    pages = fake.requests("thread/loaded/list")
    assert [p.get("cursor") for p in pages] == [None, "2", "4"]
    assert all(p["limit"] == 2 for p in pages)


async def test_a_stop_spares_another_topics_session_on_a_subagent_thread(codex, tmp_path):
    """Review of #825, B1: `!attach` lists sub-agent threads, so another topic can run its own
    turns on one. A `!stop` here used to interrupt that topic's turn as a sub-agent's. A
    sub-agent thread that is an open session of this backend is now left to its own topic,
    and so is what it spawned."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    grandchild = await fake.spawn(child)
    await seen_running(backend, child, grandchild)
    await fake.finish(child, "done")
    await until(backend, t.TurnCompleted, child)
    # the other topic's session, reopened: what the bridge's load applied in this run (#830)
    other = remember_identity(t.SessionSpec(cwd=str(tmp_path), name="b"), backend.instance)
    await backend.open_session(child, other)
    theirs = await backend.start_turn(child, t.UserInput(text="hi", client_id="b1"))
    await until(backend, t.TurnStarted, child)
    assert (child, theirs) in backend.own_turns
    await backend.interrupt(sid)
    assert (await until(backend, t.TurnCompleted, sid)).status is t.TurnStatus.INTERRUPTED
    assert stops(fake) == [sid]
    assert status(fake, child) == "inProgress" and status(fake, grandchild) == "inProgress"


async def test_a_stop_spares_a_subagent_the_thread_only_interrupted(codex, tmp_path):
    """Codex round 3: thread B interrupted A's sub-agent C (`interrupt_agent` can name any
    thread), and A resumed C. B's `!stop` then interrupted C as one of B's sub-agents. Only a
    spawn makes a sub-agent of a thread now (#822's mapper), so B's stop leaves C to A."""
    fake, backend = codex
    a, _tid = await started(fake, backend, tmp_path)
    c = await fake.spawn(a)
    await seen_running(backend, a, c)
    b, _tid = await started(fake, backend, tmp_path)
    b_turn = next(x for x in fake.threads[b]["turns"] if x["status"] == "inProgress")
    await fake._activity(fake.threads[b], b_turn["id"], c, "interrupted")  # B's interrupt_agent
    for note in fake._completion(
        fake.threads[c], fake.threads[c]["turns"][-1], "interrupted", None
    ):
        await fake._send(note)
    await fake.message(c, follow_up=True)  # A resumes C: a turn of its own
    await seen_running(backend, a, c)
    await backend.interrupt(b)
    assert (await until(backend, t.TurnCompleted, b)).status is t.TurnStatus.INTERRUPTED
    assert stops(fake) == [b] and status(fake, c) == "inProgress"


async def test_a_subagent_resumed_during_the_stop_is_stopped_again(codex, tmp_path):
    """Codex round 4, on #825: the thread runs on during the first pass, and can resume the
    sub-agent it has just stopped, with a new turn. The second pass skipped any thread it had
    interrupted, so that turn survived the `!stop`. It now skips only the very turn it
    interrupted."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await seen_running(backend, sid, child)
    first = fake.threads[child]["turns"][-1]["id"]
    interrupt = fake._turn_interrupt

    async def resumed(params):  # the parent's `followup_task`, as the child stops
        result = await interrupt(params)
        if params["threadId"] == child and len(fake.threads[child]["turns"]) == 1:
            again = {"id": "turn-again", "status": "inProgress", "items": [], "started": 0}
            fake.threads[child]["turns"].append(again)
            note = fake._turn_json(again, "notLoaded")
            result[1].append(fake._note("turn/started", threadId=child, turn=note))
        return result

    fake._turn_interrupt = resumed
    await backend.interrupt(sid)
    assert (await until(backend, t.TurnCompleted, sid)).status is t.TurnStatus.INTERRUPTED
    stopped = [(q["threadId"], q["turnId"]) for q in fake.requests("turn/interrupt")]
    assert stopped[0] == (child, first) and (child, "turn-again") in stopped[2:]
    assert [x["status"] for x in fake.threads[child]["turns"]] == ["interrupted"] * 2


async def test_a_refused_subagent_stop_never_holds_the_thread_back(codex, tmp_path):
    """A sub-agent whose turn just ended refuses the interrupt; a failed listing is logged.
    Neither keeps the thread's own stop from going out."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await seen_running(backend, sid, child)
    fake.refuse_interrupt.add(child)
    fake._thread_list = None  # the method is gone: `unsupported method`
    await backend.interrupt(sid)
    assert (await until(backend, t.TurnCompleted, sid)).status is t.TurnStatus.INTERRUPTED
    assert stops(fake) == [child, sid]


async def test_a_subagent_stop_refused_while_it_runs_is_logged_and_noted(codex, tmp_path, caplog):
    """Codex round 1 on #851, on `interrupt`'s path: an interrupt refused while the sub-agent's
    turn runs isn't a turn that just ended. The thread's own stop still goes out; the failure
    is logged, and the turn's answer notes the sub-agent still running, with `!stop`."""
    fake, backend = codex
    sid, _tid = await started(fake, backend, tmp_path)
    child = await fake.spawn(sid)
    await seen_running(backend, sid, child)
    fake.refuse_interrupt.add(child)
    with caplog.at_level(logging.WARNING, logger="zab.backends.codex"):
        await backend.interrupt(sid)
    done = await until(backend, t.TurnCompleted, sid)
    assert done.status is t.TurnStatus.INTERRUPTED and "send !stop" in done.final_text
    assert stops(fake) == [child, sid] and status(fake, child) == "inProgress"
    logged = [r.getMessage() for r in caplog.records]
    assert f"Sub-agent {child} of {sid} not stopped: RpcError" in logged, logged


async def test_a_subagent_no_event_told_of_is_found_among_the_loaded_threads(codex, tmp_path):
    """Review round 3 of #851, B2, and #858: a sub-agent this connection never heard of, such
    as one another client's turn spawned on a thread zab started and the daemon had closed.
    `thread/list` lists none live; its thread is loaded while its turn runs, and its read
    names its parent, so the stop finds it."""
    fake, backend = codex
    fake.lists_subagents = False  # what 0.157.1 answered on the test VM
    sid, _tid = await started(fake, backend, tmp_path)
    await fake.finish(sid, "done")
    await until(backend, t.TurnCompleted, sid)
    assert await backend.stop_subagents(sid) == t.SubagentStop(0)
    source = {"subAgent": {"thread_spawn": {"parent_thread_id": sid, "depth": 1}}}
    child = fake._new_thread({}, str(tmp_path), parent=sid, source=source, session=sid)
    child["turns"].append(
        {"id": "turn-elsewhere", "status": "inProgress", "items": [], "started": 0}
    )
    assert await backend.stop_subagents(sid) == t.SubagentStop(1)
    assert stops(fake) == [child["id"]] and status(fake, child["id"]) == "interrupted"


async def test_a_reconnection_on_one_backend_still_finds_the_running_subagent(tmp_path):
    """Review of #854, N7, with #858: the socket drops and the same backend object reconnects,
    the daemon still up and the sub-agent still running. The new connection forgets the
    sub-agents' states (`reconnected`), and no bookkeeping of the old one says none runs: the
    loaded threads give it, and `!stop` with no turn stops it."""
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True
    path = str(tmp_path / "s.sock")
    backend = CodexBackend("c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path))
    try:
        async with fake.serve(path):
            await backend.start()
            await asyncio.wait_for(backend.ready.wait(), 5)
            sid, _tid = await started(fake, backend, tmp_path)
            child = await fake.spawn(sid)
            await seen_running(backend, sid, child)
            await fake.finish(sid, "Started it.")
            await until(backend, t.TurnCompleted, sid)
        async with fake.serve(path):  # the same daemon: nothing unloaded, the child runs on
            while not (await until(backend, t.BackendStatus, "")).up:  # down, then up again
                pass
            # (the core's reopen of this thread, loaded since, is #854's identity check's)
            assert child not in backend.mapper.running_subagents(sid)  # states forgotten
            assert child not in backend.mapper.active
            assert await backend.stop_subagents(sid) == t.SubagentStop(1)
            assert status(fake, child) == "interrupted"
    finally:
        await backend.close()
