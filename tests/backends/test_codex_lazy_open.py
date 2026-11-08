"""Codex sessions reopen without loading their threads (issue #632, SPEC §7.8.1, §8.4).

Each thread the app-server holds loaded runs its own stdio MCP servers (on the VMs, a
`lean-lsp-mcp` of about 54 MB), and the app-server holds none after a boot or wake. So a thread
it doesn't hold is only read (`thread/read`, `thread/turns/list`) when its session reopens, and
loaded at its next use; a thread it holds is resumed, as a turn still running needs. Against the
scripted app-server `FakeAppServer` (SPEC §15.2), which models what 0.157.1's source does
(DECISIONS, 2026-11-05) and whose wire is checked against the pinned schemas."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from backends.test_codex_fork import check_wire, spec, turn, until
from backends.test_codex_subagents import seen_running, status, stops
from zab.backends import codex_rpc
from zab.backends.codex import CodexBackend, identity_config
from zab.backends.codex_rpc import RpcError
from zab.config import BackendConfig
from zab.core import types as t
from zab.core.reason import verified_only
from zab.testing.fake_codex import FakeAppServer, RpcFailure

pytestmark = pytest.mark.contract


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    """A `FakeAppServer` on its socket, and `connect()`, which starts a backend on it as a
    bridge starting does (the previous one closed first: the fake serves one connection). Each
    `restart()` is a new run of the app-server for the bridge (`instance_of`, #830)."""
    fake = FakeAppServer(str(tmp_path))
    monkeypatch.setattr(codex_rpc, "app_server_instance", fake.instance_of)
    path = str(tmp_path / "s.sock")
    backends: list[CodexBackend] = []

    async def connect() -> CodexBackend:
        if backends:
            await backends[-1].close()
        backend = CodexBackend(
            "c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path)
        )
        await backend.start()
        await asyncio.wait_for(backend.ready.wait(), 5)
        backends.append(backend)
        return backend

    async with fake.serve(path):
        try:
            yield fake, connect
        finally:
            if backends:
                await backends[-1].close()


async def sessions(backend, n: int) -> list[t.SessionInfo]:
    """n sessions with one answered turn each, as a VM's store holds them: their specs carry
    the settings recovery persists (the applied identity included)."""
    made = []
    for i in range(n):
        info = await backend.create_session(spec(f"/w/{i}", f"topic {i}"))
        await turn(backend, info.native_id, f"remember {i}")
        made.append(info)
    return made


def persisted(info: t.SessionInfo, name: str = "topic") -> t.SessionSpec:
    return replace(spec("/w/x", name), settings=info.settings)


async def test_a_boot_reopens_every_session_without_loading_one(daemon):
    """After the app-server's restart (a VM's boot or wake), no session's reopen loads its
    thread; the snapshot is the one a resume gave: preview, last turn and its answer. The
    first turn of one session then loads that thread alone, cold, with the session's identity,
    and its first input carries the bridge context (§8.4)."""
    fake, connect = daemon
    made = await sessions(await connect(), 5)
    fake.restart()
    backend = await connect()
    loads = len(fake.loads)
    opened = [await backend.open_session(i.native_id, persisted(i)) for i in made]
    assert fake.loaded == set() and len(fake.loads) == loads
    assert not fake.requests("thread/resume")
    for i, info in enumerate(opened):
        assert info.native_id == made[i].native_id and info.active_turn_id is None
        assert info.preview == f"remember {i}"
        assert info.last_turn.status == t.TurnStatus.COMPLETED
        assert info.last_turn.final_text == "FAKE_OK 1"
        assert "applied_git_identity" not in info.settings.extra  # not loaded by the bridge
    assert set(backend.mapper.closed) == {i.native_id for i in made}
    # nothing a resume resolves is settled from the reads (codex round 1 on #818)
    resolved = set(backend.defaults) | set(backend.policies) | set(backend.settings_pending)
    assert not resolved & set(backend.mapper.closed)

    sid = made[2].native_id
    await turn(backend, sid, "what did I ask you to remember?", wait=False)
    # the identity the load applied, for the core to persist (review of #818)
    changed = await until(backend, t.SessionChanged, sid)
    assert (
        changed.settings.extra["applied_git_identity"]
        == (made[2].settings.extra["applied_git_identity"])
    )
    await until(backend, t.TurnCompleted, sid)
    assert fake.loaded == {sid} and fake.loads[loads:] == [sid]
    (resume,) = fake.requests("thread/resume")
    assert resume["threadId"] == sid and resume["config"] == identity_config(spec("/w/x", "x"))
    first = fake.requests("turn/start")[-1]["input"][0]["text"]
    assert first.startswith("<zab-context>\nbridge context\n</zab-context>")
    assert sid not in backend.mapper.closed
    await turn(backend, sid, "and now?")
    assert fake.requests("turn/start")[-1]["input"][0]["text"] == "and now?"
    assert len(fake.requests("thread/resume")) == 1


async def test_a_fork_or_a_side_question_loads_only_the_threads_it_needs(daemon):
    """`!fork` and `!btw` of an unloaded session resume the parent first, as its next turn
    would (§8.8, §8.10); the other sessions stay unloaded."""
    fake, connect = daemon
    made = await sessions(await connect(), 3)
    fake.restart()
    backend = await connect()
    for info in made:
        await backend.open_session(info.native_id, persisted(info))
    parent = made[0].native_id
    child = await backend.fork_session(parent, spec("/w/fork", "fork"))
    assert fake.loaded == {parent, child.native_id}
    assert await backend.side_question(made[1].native_id, "what?", timeout_s=10) == "FAKE_OK 2"
    assert fake.loaded == {parent, child.native_id, made[1].native_id}  # the side fork is gone


async def test_a_thread_still_loaded_is_resumed_with_its_running_turn(daemon):
    """A bridge restart while the app-server runs (D4): the thread it holds, with a turn
    running, is resumed, so its events reach the new connection (§7.8.1)."""
    fake, connect = daemon
    backend = await connect()
    (info,) = await sessions(backend, 1)
    fake.hold = True
    running = await turn(backend, info.native_id, "long job", wait=False)
    backend = await connect()
    opened = await backend.open_session(info.native_id, persisted(info))
    assert opened.active_turn_id == running
    assert [r["threadId"] for r in fake.requests("thread/resume")] == [info.native_id]
    assert info.native_id not in backend.mapper.closed
    assert not fake.requests("thread/loaded/list")  # its identity is the persisted one
    await fake.finish(info.native_id, "LONG_DONE")
    done = await until(backend, t.TurnCompleted, info.native_id)
    assert (done.turn_id, done.final_text) == (running, "LONG_DONE")


async def test_a_turn_the_restart_ended_is_read_without_loading_the_thread(daemon):
    """A turn running when the app-server stopped (a crash: an idle stop waits for idle) is
    listed `interrupted` from the rollout; recovery settles it from that, unloaded."""
    fake, connect = daemon
    backend = await connect()
    (info,) = await sessions(backend, 1)
    fake.hold = True
    running = await turn(backend, info.native_id, "long job", wait=False)
    fake.restart()
    backend = await connect()
    opened = await backend.open_session(info.native_id, persisted(info))
    assert opened.active_turn_id is None
    assert (opened.last_turn.turn_id, opened.last_turn.status) == (
        running,
        t.TurnStatus.INTERRUPTED,
    )
    assert fake.loaded == set()


async def test_an_unloaded_thread_another_client_loads_first_is_refused(daemon):
    """Opened unloaded, then loaded by another client (a terminal's `codex resume`) in its own
    environment: its persisted identity vouches only for the bridge's own loads, so its next
    turn is refused, as an unknown identity on a loaded thread is (§5.5), not run there. The
    reopen `!status` makes refuses it too."""
    fake, connect = daemon
    (info,) = await sessions(await connect(), 1)
    fake.restart()
    backend = await connect()
    await backend.open_session(info.native_id, persisted(info))
    fake.loaded.add(info.native_id)  # the terminal's client loads it
    starts = len(fake.requests("turn/start"))
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await backend.start_turn(info.native_id, t.UserInput(text="hi", client_id="c"))
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await backend.open_session(info.native_id, persisted(info))
    assert not fake.requests("thread/resume")
    assert len(fake.requests("turn/start")) == starts


async def test_an_unloaded_thread_still_unloaded_at_its_next_use_is_loaded(daemon):
    """The check before that load finds no other client: the thread is resumed cold."""
    fake, connect = daemon
    (info,) = await sessions(await connect(), 1)
    fake.restart()
    backend = await connect()
    await backend.open_session(info.native_id, persisted(info))
    await turn(backend, info.native_id, "next")
    assert fake.requests("thread/loaded/list") and fake.loaded == {info.native_id}


@pytest.mark.parametrize("failure", ["read", "running"])
async def test_a_thread_read_that_fails_or_finds_a_turn_running_resumes_as_before(
    daemon, monkeypatch, failure
):
    """A `thread/read` the app-server refuses (a thread with no rollout yet, …) resumes it, as
    every reopen did before; so does a turn in progress in the list after the read said
    `notLoaded` (another client loaded it and started one in between)."""
    fake, connect = daemon
    backend = await connect()
    (info,) = await sessions(backend, 1)
    running = None
    if failure == "running":
        fake.hold = True
        running = await turn(backend, info.native_id, "long job", wait=False)
    read = fake._thread_read

    async def thread_read(params):
        if failure == "read":
            raise RpcFailure(-32600, f"thread not loaded: {params['threadId']}")
        result, after = await read(params)
        result["thread"]["status"] = {"type": "notLoaded"}  # read just before the load
        return result, after

    monkeypatch.setattr(fake, "_thread_read", thread_read)
    backend = await connect()
    opened = await backend.open_session(info.native_id, persisted(info))
    assert [r["threadId"] for r in fake.requests("thread/resume")] == [info.native_id]
    assert opened.active_turn_id == running
    assert info.native_id not in backend.mapper.closed


async def test_the_unloaded_open_speaks_the_pinned_schemas(daemon):
    """The reads of an unloaded open and the load at its next turn, both ways, against the
    generated schemas, the requests' params closed (as in test_codex_fork)."""
    fake, connect = daemon
    (info,) = await sessions(await connect(), 1)
    fake.restart()
    backend = await connect()
    await backend.open_session(info.native_id, persisted(info))
    await turn(backend, info.native_id, "next")
    checked = check_wire(fake)
    assert {"thread/read", "thread/turns/list", "thread/loaded/list", "thread/resume"} <= checked


async def test_a_side_question_during_the_first_load_waits_for_it(daemon, monkeypatch):
    """A `!btw` while the first turn after a wake loads the thread waits for that load: it
    would otherwise find the thread loaded, by the bridge itself, and refuse it as another
    client's. One load, both answered."""
    fake, connect = daemon
    (info,) = await sessions(await connect(), 1)
    fake.restart()
    backend = await connect()
    await backend.open_session(info.native_id, persisted(info))
    resume, side = fake._thread_resume, []

    async def slow_resume(params):
        if not side:
            question = backend.side_question(info.native_id, "what?", timeout_s=10)
            side.append(asyncio.ensure_future(question))
            await asyncio.sleep(0.1)  # its requests queue behind this one
        return await resume(params)

    monkeypatch.setattr(fake, "_thread_resume", slow_resume)
    await turn(backend, info.native_id, "next")
    assert (await side[0]).startswith("FAKE_OK")
    assert len(fake.requests("thread/resume")) == 1 and fake.loaded == {info.native_id}


@pytest.mark.parametrize("catalog", [True, False])
@pytest.mark.parametrize("between", [False, True])
async def test_a_change_then_a_reset_after_a_wake_returns_to_the_sessions_model(
    daemon, monkeypatch, catalog, between
):
    """Codex round 1 on #818: an unloaded open settles none of the session's defaults from
    `thread/read`, whose `model` and `reasoningEffort` are the persisted ones, or null ("when
    unavailable"), where a resume reports the resolved ones; `_remember_defaults` sets them
    once. After a wake, `!model` to another model and then `!model ""` go back to the
    session's own model, as before #632: with or without a turn between, with or without the
    catalog (else the catalog's default, or no model at all, is sent)."""
    fake, connect = daemon
    fake.defaults["model"] = "gpt-other"  # the session's model: its config's default then
    (info,) = await sessions(await connect(), 1)
    fake.defaults["model"] = "gpt-fake"  # the config's default since, the catalog's
    fake.restart()
    read = fake._thread_read

    async def unavailable(params):
        result, after = await read(params)
        result["thread"] |= {"model": None, "reasoningEffort": None}
        return result, after

    async def no_catalog(params):
        raise RpcFailure(-32603, "model/list unavailable")

    monkeypatch.setattr(fake, "_thread_read", unavailable)
    if not catalog:
        monkeypatch.setattr(fake, "_model_list", no_catalog)
    backend = await connect()
    sid = info.native_id
    opened = await backend.open_session(sid, persisted(info))
    changed = await backend.update_settings(sid, replace(opened.settings, model="gpt-fake"))
    if between:
        await turn(backend, sid, "with the other model")
        assert fake.requests("turn/start")[-1]["model"] == "gpt-fake"
    await backend.update_settings(sid, replace(changed, model=None))
    await turn(backend, sid, "back to mine")
    assert fake.requests("turn/start")[-1].get("model") == "gpt-other"
    assert fake.threads[sid]["model"] == "gpt-other"


async def woken(fake, connect) -> tuple[CodexBackend, str, t.SessionInfo]:
    """One session with a turn, the app-server restarted (a wake), the bridge's new backend,
    and the session opened unloaded."""
    (info,) = await sessions(await connect(), 1)
    fake.restart()
    backend = await connect()
    return backend, info.native_id, await backend.open_session(info.native_id, persisted(info))


@pytest.mark.parametrize("first", ["bridge", "terminal"])
async def test_a_bridge_restart_trusts_only_a_load_the_bridge_made(daemon, first):
    """Review of #818 (B1, §5.5): after a wake, the thread's first loader sets its
    environment. A restart of the bridge alone then trusts the thread the bridge loaded, whose
    identity `_load` reported for the core to persist, and refuses one a terminal's client
    loaded first: the unloaded open recorded no identity, so nothing vouches for it."""
    fake, connect = daemon
    backend, sid, opened = await woken(fake, connect)
    settings = opened.settings  # what recovery persists
    if first == "bridge":
        await turn(backend, sid, "mine", wait=False)
        settings = (await until(backend, t.SessionChanged, sid)).settings  # what it persists then
        await until(backend, t.TurnCompleted, sid)
    else:
        fake.loaded.add(sid)  # a terminal's `codex resume`, in its own environment
    backend = await connect()  # the bridge alone restarts: the app-server still holds it
    checks, resumes = len(fake.requests("thread/loaded/list")), len(fake.requests("thread/resume"))
    reopen = backend.open_session(sid, replace(spec("/w/x", "topic"), settings=settings))
    if first == "bridge":
        await reopen
        assert len(fake.requests("thread/loaded/list")) == checks  # its record is trusted
        await turn(backend, sid, "again")
    else:
        with pytest.raises(t.SessionRefused, match="quit that client if any, wait a minute"):
            await reopen
        assert len(fake.requests("thread/resume")) == resumes


async def test_a_settings_change_after_a_load_is_announced_after_it(daemon):
    """The announcement of a load can still be queued when `!model` changes the settings: the
    change is announced again, after it, so the core never ends with the older settings."""
    fake, connect = daemon
    backend, sid, _ = await woken(fake, connect)
    await turn(backend, sid, "mine", wait=False)
    first = await until(backend, t.SessionChanged, sid)
    await until(backend, t.TurnCompleted, sid)
    changed = await backend.update_settings(sid, replace(first.settings, model="gpt-other"))
    last = await until(backend, t.SessionChanged, sid)
    assert first.settings.model is None and last.settings == changed
    assert (
        last.settings.extra["applied_git_identity"] == first.settings.extra["applied_git_identity"]
    )


async def test_a_load_that_fails_after_its_resume_is_retried_not_refused(daemon, monkeypatch):
    """Review of #818 (N1): the resume went out and loaded the thread, then the turn list
    failed. The thread, loaded by the bridge's own connection, is resumed again at the next
    turn, not refused as another client's."""
    fake, connect = daemon
    backend, sid, _ = await woken(fake, connect)
    listed, failed = fake._thread_turns_list, []

    async def fail_once(params):
        if sid in fake.loaded and not failed:  # the list after the resume
            failed.append(params)
            raise RpcFailure(-32603, "turns unavailable")
        return await listed(params)

    monkeypatch.setattr(fake, "_thread_turns_list", fail_once)
    with pytest.raises(RpcError):
        await backend.start_turn(sid, t.UserInput(text="hi", client_id="c-hi"))
    assert sid in fake.loaded and sid in backend.mapper.closed
    await turn(backend, sid, "again")
    assert len(fake.requests("thread/resume")) == 2


async def test_a_side_question_that_times_out_during_the_load_leaves_it_retryable(
    daemon, monkeypatch
):
    """Review of #818 (N1): `!btw`'s deadline passes while its parent loads, after the
    resume went out. The next turn loads the thread again and runs."""
    fake, connect = daemon
    backend, sid, _ = await woken(fake, connect)
    listed, slowed = fake._thread_turns_list, []

    async def slow_once(params):
        if sid in fake.loaded and not slowed:
            slowed.append(params)
            await asyncio.sleep(1)
        return await listed(params)

    monkeypatch.setattr(fake, "_thread_turns_list", slow_once)
    with pytest.raises(TimeoutError):
        await backend.side_question(sid, "what?", timeout_s=0.5)
    await turn(backend, sid, "again")
    assert not fake.requests("thread/fork")


async def test_an_unmentioned_setting_codex_lacks_is_refused_before_any_load(daemon):
    """Review of #818 (N2): `!model` for the other agent's model, unmentioned, after a wake:
    refused against the catalog first, quietly (`InvalidSetting`), the thread left unloaded."""
    fake, connect = daemon
    backend, sid, opened = await woken(fake, connect)
    loads = len(fake.loads)
    for field, value in (("model", "claude-opus"), ("effort", "extreme")):
        with verified_only(), pytest.raises(t.InvalidSetting) as refused:
            await backend.update_settings(sid, replace(opened.settings, **{field: value}))
        assert (refused.value.field, refused.value.value) == (field, value)
    assert len(fake.loads) == loads and sid in backend.mapper.closed


@pytest.mark.parametrize("mentioned", [False, True])
async def test_a_setting_for_a_thread_a_terminal_holds_is_refused(daemon, mentioned):
    """Review of #818 (N2): a valid `!model` for a thread a terminal's client loaded first.
    Unmentioned, it is refused as quietly as a wrong value (§4.5), not as a session refusal
    that would put the session in error; mentioned, it is refused as the topic's next turn
    would be, `!status` saying why. Neither resumes the thread."""
    fake, connect = daemon
    backend, sid, opened = await woken(fake, connect)
    fake.loaded.add(sid)
    candidate = replace(opened.settings, model="gpt-other")
    if mentioned:
        with pytest.raises(t.SessionRefused, match="quit that client"):
            await backend.update_settings(sid, candidate)
    else:
        with verified_only(), pytest.raises(t.InvalidSetting, match="quit that client"):
            await backend.update_settings(sid, candidate)
    assert not fake.requests("thread/resume")


def queued(backend, sid) -> list[t.SessionChanged]:
    """The `SessionChanged` events queued for a session so far, the queue drained."""
    found = []
    while not backend.queue.empty():
        event = backend.queue.get_nowait()
        if isinstance(event, t.SessionChanged) and event.session_id == sid:
            found.append(event)
    return found


async def test_a_load_for_a_setting_refused_after_it_still_reports_its_identity(
    daemon, monkeypatch
):
    """Codex round 2 on #818: `!effort` with an effort another model of the catalog takes
    passes the check before the load, loads the thread, then is refused for the session's own
    model. The load is still reported with its identity, so a restart of the bridge alone
    trusts the thread the bridge loaded; its later turns load nothing, and nothing else would
    report it."""
    fake, connect = daemon
    models = fake._model_list

    async def model_list(params):
        result, after = await models(params)
        for model in result["data"]:
            if model["model"] == "gpt-other":  # an effort the session's model lacks
                xhigh = {"reasoningEffort": "xhigh", "description": "xhigh"}
                model["supportedReasoningEfforts"] = [*model["supportedReasoningEfforts"], xhigh]
        return result, after

    monkeypatch.setattr(fake, "_model_list", model_list)
    backend, sid, opened = await woken(fake, connect)
    with pytest.raises(t.InvalidSetting) as refused:
        await backend.update_settings(sid, replace(opened.settings, effort="xhigh"))
    assert refused.value.field == "effort" and sid in fake.loaded
    (reported,) = queued(backend, sid)
    assert "applied_git_identity" in reported.settings.extra
    backend = await connect()  # the bridge alone restarts
    checks = len(fake.requests("thread/loaded/list"))
    await backend.open_session(sid, replace(spec("/w/x", "topic"), settings=reported.settings))
    assert len(fake.requests("thread/loaded/list")) == checks
    await turn(backend, sid, "again")


async def test_an_accepted_setting_that_loaded_the_thread_is_reported_after_the_load(daemon):
    """Codex round 2 on #818: the load a `!model` makes is reported at once, and the accepted
    change after it, so that the core ends with the change, the identity included."""
    fake, connect = daemon
    backend, sid, opened = await woken(fake, connect)
    accepted = await backend.update_settings(sid, replace(opened.settings, model="gpt-other"))
    load, change = queued(backend, sid)
    assert load.settings.model is None and change.settings == accepted
    assert "applied_git_identity" in load.settings.extra
    assert accepted.extra["applied_git_identity"] == load.settings.extra["applied_git_identity"]


async def test_the_turn_that_loads_a_thread_notes_a_subagent_left_running(daemon):
    """#660 case 2 on a thread its session reopened unloaded: the turn that loads it
    (`_load`) gets its events from the start, so the sub-agent it spawns is tracked, and the
    turn's answer says that it still runs."""
    fake, connect = daemon
    (made,) = await sessions(await connect(), 1)
    fake.restart()
    backend = await connect()
    sid = made.native_id
    await backend.open_session(sid, persisted(made))
    assert sid in backend.mapper.closed and sid not in fake.loaded
    fake.hold = True
    await turn(backend, sid, "check it in a sub-agent", wait=False)
    await until(backend, t.TurnStarted, sid)
    assert sid in fake.loaded and sid not in backend.mapper.closed
    await fake.spawn(sid)
    await fake.finish(sid, "I started a sub-agent for the check.")
    done = await until(backend, t.TurnCompleted, sid)
    assert done.final_text == (
        "I started a sub-agent for the check.\n\n⚠️ **Bridge** (not Codex): A sub-agent was still "
        "running when this turn ended. Its answer won't be posted here: ask Codex about it in a "
        "new message, or send !stop to stop it."
    )
    check_wire(fake)


async def subagent_across_a_restart(fake, connect) -> tuple[CodexBackend, t.SessionInfo, str]:
    """A sub-agent still running across a bridge restart, the daemon up (#660): the session's
    turn spawned it and ended; the bridge stopped, and the daemon unloaded the thread, which no
    connection held any more, while the sub-agent ran on (`FakeAppServer.unload`). The new
    bridge reopened the session unloaded. The new backend, the session and the sub-agent."""
    first = await connect()
    (made,) = await sessions(first, 1)
    sid = made.native_id
    fake.hold = True
    await turn(first, sid, "check it in a sub-agent", wait=False)
    await until(first, t.TurnStarted, sid)
    child = await fake.spawn(sid)
    await fake.finish(sid, "Started it.")
    await until(first, t.TurnCompleted, sid)
    await first.close()
    fake.unload(sid)
    backend = await connect()
    await backend.open_session(sid, persisted(made))
    assert sid in backend.mapper.closed and sid not in fake.loaded and child in fake.loaded
    return backend, made, child


async def test_a_stop_of_an_unloaded_thread_stops_its_subagent_without_loading_it(daemon):
    """#660 case 3 on a thread reopened unloaded. The core passes `!stop` on only while a turn
    runs, and the thread's next turn loads it (next test); the backend's stop needs no loaded
    thread anyway: the loaded threads give the sub-agent, whose turn is interrupted, and the thread
    stays unloaded (no `thread/resume`, so no `lean-lsp-mcp` started for it)."""
    fake, connect = daemon
    backend, made, child = await subagent_across_a_restart(fake, connect)
    loads = list(fake.loads)
    await backend.interrupt(made.native_id)
    assert stops(fake) == [child] and status(fake, child) == "interrupted"
    assert made.native_id not in fake.loaded and fake.loads == loads
    assert not fake.requests("thread/resume")
    check_wire(fake)


async def test_a_stop_after_the_lazy_load_stops_old_and_new_subagents(daemon):
    """The next turn loads that thread (`_load`) and spawns another sub-agent. `!stop` then
    interrupts the new one, which this run's events tracked, then the thread's turn, then the
    old one, which only the loaded threads give (#858)."""
    fake, connect = daemon
    backend, made, old = await subagent_across_a_restart(fake, connect)
    sid = made.native_id
    tid = await turn(backend, sid, "check it again", wait=False)
    await until(backend, t.TurnStarted, sid)
    assert sid in fake.loaded and sid not in backend.mapper.closed
    new = await fake.spawn(sid)
    await seen_running(backend, sid, new)
    await backend.interrupt(sid)
    done = await until(backend, t.TurnCompleted, sid)
    assert done.turn_id == tid and done.status is t.TurnStatus.INTERRUPTED
    assert done.final_text is None  # no note: nothing it tracked is left running
    assert stops(fake) == [new, sid, old]
    assert [status(fake, x) for x in (new, old)] == ["interrupted", "interrupted"]
    check_wire(fake)


async def test_stop_subagents_on_an_unloaded_thread_counts_what_it_stopped(daemon):
    """#847, `!stop` with no turn running: `stop_subagents` is `interrupt`'s listing pass
    alone. It stops the sub-agent left running under a thread reopened unloaded, counts it,
    loads nothing and sends the thread no `turn/interrupt`; then there is none left."""
    fake, connect = daemon
    backend, made, child = await subagent_across_a_restart(fake, connect)
    loads = list(fake.loads)
    assert backend.capabilities.stop_subagents
    # reopened after a restart: the loaded threads give the sub-agent no event told of
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(1)
    assert stops(fake) == [child] and status(fake, child) == "interrupted"
    assert made.native_id not in fake.loaded and fake.loads == loads
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(0)
    check_wire(fake)


async def test_a_stop_that_could_not_read_the_loaded_threads_never_says_none_ran(
    daemon, monkeypatch
):
    """`thread/loaded/list` fails, or a loaded thread's read, so a sub-agent no event told of
    can't be found: `BackendUnavailable`, never 0, which the core would report as none
    running. `thread/list`, which lists none live (#858), isn't asked."""
    fake, connect = daemon
    fake.lists_subagents = False  # what 0.157.1 answered on the test VM
    backend, made, child = await subagent_across_a_restart(fake, connect)

    async def refused(params):
        raise RpcFailure(-32603, "the thread store is unavailable")

    monkeypatch.setattr(fake, "_thread_loaded_list", refused)
    with pytest.raises(t.BackendUnavailable):
        await backend.stop_subagents(made.native_id)
    assert status(fake, child) == "inProgress" and not stops(fake)
    monkeypatch.undo()
    read = fake._thread_read

    async def unreadable(params):
        if params["threadId"] == child:
            raise RpcFailure(-32603, "the rollout is unreadable")
        return await read(params)

    monkeypatch.setattr(fake, "_thread_read", unreadable)
    with pytest.raises(t.BackendUnavailable):
        await backend.stop_subagents(made.native_id)
    assert status(fake, child) == "inProgress" and not stops(fake)
    monkeypatch.undo()
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(1)
    assert status(fake, child) == "interrupted" and not fake.requests("thread/list")


async def test_a_stop_whose_lookup_or_interrupt_fails_never_says_none_ran(daemon, monkeypatch):
    """Codex round 1 on #851: the sub-agent's read says it is active, but the lookup of its
    turn (`thread/turns/list`) fails, or its `turn/interrupt` is refused while that turn still
    runs. Either was swallowed, and `stop_subagents` said 0: "no sub-agent was", the sub-agent
    running on. Now `BackendUnavailable`; the next stop, with the failure gone, stops it."""
    fake, connect = daemon
    backend, made, child = await subagent_across_a_restart(fake, connect)
    listed = fake._thread_turns_list

    async def lookup_fails(params):
        if params["threadId"] == child:
            raise RpcFailure(-32603, "the rollout is unreadable")
        return await listed(params)

    monkeypatch.setattr(fake, "_thread_turns_list", lookup_fails)
    with pytest.raises(t.BackendUnavailable):
        await backend.stop_subagents(made.native_id)
    assert status(fake, child) == "inProgress" and not stops(fake)
    monkeypatch.setattr(fake, "_thread_turns_list", listed)
    fake.refuse_interrupt.add(child)  # refused, though its turn runs on
    with pytest.raises(t.BackendUnavailable):
        await backend.stop_subagents(made.native_id)
    assert status(fake, child) == "inProgress" and stops(fake) == [child]
    fake.refuse_interrupt.clear()
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(1)
    assert status(fake, child) == "interrupted"


async def test_a_refused_interrupt_of_a_turn_that_just_ended_is_none_running(daemon, monkeypatch):
    """The refusal a turn gets when it ended between the lookup and the interrupt: its newest
    turn, looked up again, isn't in progress, so nothing runs and none was stopped."""
    fake, connect = daemon
    backend, made, child = await subagent_across_a_restart(fake, connect)
    interrupt = fake._turn_interrupt

    async def ended_meanwhile(params):
        if params["threadId"] == child:
            fake.threads[child]["turns"][-1]["status"] = "completed"
            raise RpcFailure(-32600, "no active turn to interrupt")
        return await interrupt(params)

    monkeypatch.setattr(fake, "_turn_interrupt", ended_meanwhile)
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(0)


async def test_a_stop_leaves_the_subagents_of_a_turn_started_elsewhere(daemon):
    """Review of #851, B2: the session reopened unloaded, then a terminal client loaded the
    thread and ran a turn on it, which spawned a sub-agent. The core has no turn there
    (§8.6), so `!stop` reached `stop_subagents`, which interrupted that turn's sub-agent while
    the turn ran on. Now the thread's status (`thread/read`, which loads nothing) says a turn
    runs: `TurnElsewhere`, and nothing is stopped. Once that turn ends, its sub-agent left
    running is stopped as any other."""
    fake, connect = daemon
    first = await connect()
    (made,) = await sessions(first, 1)
    sid = made.native_id
    await first.close()
    fake.unload(sid)
    backend = await connect()
    await backend.open_session(sid, persisted(made))
    fake._load(fake.threads[sid])  # the terminal client loads the thread and runs a turn
    terminal = {"id": "turn-terminal", "status": "inProgress", "items": [], "started": 0}
    fake.threads[sid]["turns"].append(terminal | {"started": int(time.time())})
    child = await fake.spawn(sid)
    loads = list(fake.loads)
    with pytest.raises(t.TurnElsewhere):
        await backend.stop_subagents(sid)
    assert not stops(fake) and fake.loads == loads
    assert status(fake, child) == status(fake, sid) == "inProgress"
    (read,) = fake.requests("thread/read")[-1:]
    assert read == {"threadId": sid, "includeTurns": False}
    fake.threads[sid]["turns"][-1]["status"] = "completed"  # it ends; its sub-agent runs on
    assert await backend.stop_subagents(sid) == t.SubagentStop(1)
    assert stops(fake) == [child] and status(fake, child) == "interrupted"
    check_wire(fake)


async def test_a_hung_thread_read_before_the_stop_is_bounded(daemon, monkeypatch):
    """Review round 2 of #851, N1: the stop's `thread/read` had no bound but the call's own
    (120 s), so a hung daemon held the session that long. It now has `SUBAGENT_STOP_S`, as the
    walk does, and then the stop couldn't check: `BackendUnavailable`, nothing stopped."""
    from zab.backends import codex as codex_backend

    fake, connect = daemon
    backend, made, child = await subagent_across_a_restart(fake, connect)
    monkeypatch.setattr(codex_backend, "SUBAGENT_STOP_S", 0.2)
    released = asyncio.Event()
    read = fake._thread_read

    async def hung(params):
        await released.wait()
        return await read(params)

    monkeypatch.setattr(fake, "_thread_read", hung)
    started = time.monotonic()
    try:
        with pytest.raises(t.BackendUnavailable):
            await backend.stop_subagents(made.native_id)
        assert time.monotonic() - started < 2
        assert not stops(fake) and status(fake, child) == "inProgress"
    finally:
        released.set()


async def test_after_a_restart_the_loaded_threads_give_the_subagent(daemon):
    """#858: live, `thread/list` lists no sub-agent thread. After a bridge restart the events
    have told of none, so the walk found nothing (Codex round 3 on #851). A running
    sub-agent's turn keeps its thread loaded, and its read names its parent: the stop finds
    it among the loaded threads and stops it."""
    fake, connect = daemon
    fake.lists_subagents = False  # what 0.157.1 answered on the test VM
    backend, made, child = await subagent_across_a_restart(fake, connect)
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(1)
    assert stops(fake) == [child] and status(fake, child) == "interrupted"
    fresh = await backend.create_session(spec("/w/new", "new"))
    assert await backend.stop_subagents(fresh.native_id) == t.SubagentStop(0)


async def test_a_thread_loaded_by_a_turn_after_a_restart_still_finds_the_old_subagent(daemon):
    """Review round 3 of #851, N2: after a restart, a turn loads the thread (`_load`'s
    `thread/resume`) and ends, while a sub-agent of an earlier turn runs on, of which the
    events never told. With the listing empty, as live, the loaded threads give it (#858)."""
    fake, connect = daemon
    backend, made, old = await subagent_across_a_restart(fake, connect)
    sid = made.native_id
    await turn(backend, sid, "something else", wait=False)
    await until(backend, t.TurnStarted, sid)
    await fake.finish(sid, "done")
    await until(backend, t.TurnCompleted, sid)
    assert sid in fake.loaded
    fake.lists_subagents = False  # what 0.157.1 answered on the test VM
    assert await backend.stop_subagents(sid) == t.SubagentStop(1)
    assert stops(fake) == [old] and status(fake, old) == "interrupted"


async def test_a_running_grandchild_under_an_unloaded_idle_child_is_found(daemon):
    """#858: after a restart, the loaded threads hold the running grandchild but not its
    parent, an idle sub-agent the daemon unloaded. The grandchild's read names that parent,
    whose own read (unloaded: it loads nothing) gives the edge up to the topic's thread, so
    the walk reaches the grandchild and stops it."""
    fake, connect = daemon
    fake.lists_subagents = False  # what 0.157.1 answered on the test VM
    first = await connect()
    (made,) = await sessions(first, 1)
    sid = made.native_id
    fake.hold = True
    await turn(first, sid, "check it in a sub-agent", wait=False)
    await until(first, t.TurnStarted, sid)
    child = await fake.spawn(sid)
    grandchild = await fake.spawn(child)
    await fake.finish(child, "Started mine.")
    await fake.finish(sid, "Started it.")
    await until(first, t.TurnCompleted, sid)
    await first.close()
    fake.unload(sid)
    fake.unload(child)  # idle: unloaded 60 s after its last client left
    backend = await connect()
    await backend.open_session(sid, persisted(made))
    assert grandchild in fake.loaded and child not in fake.loaded
    loads = list(fake.loads)
    assert await backend.stop_subagents(sid) == t.SubagentStop(1)
    assert stops(fake) == [grandchild] and status(fake, grandchild) == "interrupted"
    assert fake.loads == loads  # nothing loaded: the reads only


def terminal_turn(fake, thread_id: str) -> None:
    """Another client (a terminal) holds the thread loaded and runs a turn on it."""
    fake._load(fake.threads[thread_id])
    fake.threads[thread_id]["turns"].append(
        {"id": f"turn-term-{thread_id}", "status": "inProgress", "items": [], "started": 0}
    )


async def test_an_attached_subagents_stop_stops_only_its_own_descendants(daemon):
    """Codex round 1 on #861 (P1): a terminal's thread P runs a turn that spawned S1 and S2,
    and S1, which spawned G, is attached in a topic. S1's `sessionId` is P's, so the
    session-tree fallback made P a child of S1, and `!stop` in S1's topic interrupted P and
    S2, the terminal's work. Only `parentThreadId` makes an edge now: G is stopped, and
    nothing of P's."""
    fake, connect = daemon
    backend = await connect()
    parent = fake._new_thread({}, "/w/term")  # not a session of the bridge
    terminal_turn(fake, parent["id"])
    s1 = await fake.spawn(parent["id"])
    s2 = await fake.spawn(parent["id"])
    grandchild = await fake.spawn(s1)
    await fake.finish(s1, "done")  # the attached child is idle; its own sub-agent runs on
    fake.unload(s1)  # idle, no subscriber left: the daemon unloaded it; then `!attach`
    await backend.open_session(s1, spec("/w/term", "attached"))
    assert s1 in backend.specs and parent["id"] not in backend.specs
    assert await backend.stop_subagents(s1) == t.SubagentStop(1)
    assert stops(fake) == [grandchild] and status(fake, grandchild) == "interrupted"
    assert status(fake, parent["id"]) == status(fake, s2) == "inProgress"


async def test_a_running_thread_of_the_tree_without_a_parent_is_left_alone(daemon):
    """Review of #861: a loaded thread of the topic's session tree with no `parentThreadId`
    (a fork, if a fork shares its source's tree), running another client's turn. It is no
    sub-agent: `!stop` with no turn leaves it alone."""
    fake, connect = daemon
    backend = await connect()
    (made,) = await sessions(backend, 1)
    other = fake._new_thread({}, "/w/0", session=made.native_id)  # same tree, no parent
    terminal_turn(fake, other["id"])
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(0)
    assert not stops(fake) and status(fake, other["id"]) == "inProgress"


async def test_another_topics_subagents_are_spared(daemon):
    """Review of #861: topics A and B; B's turn ended with a sub-agent running. `!stop` in A,
    with no turn, doesn't touch B's sub-agent."""
    fake, connect = daemon
    backend = await connect()
    a, b = await sessions(backend, 2)
    terminal_turn(fake, b.native_id)  # a turn on B spawns, then ends
    child = await fake.spawn(b.native_id)
    fake.threads[b.native_id]["turns"][-1]["status"] = "completed"
    assert await backend.stop_subagents(a.native_id) == t.SubagentStop(0)
    assert not stops(fake) and status(fake, child) == "inProgress"


async def test_a_chain_deeper_than_the_cap_is_never_complete(daemon):
    """Review of #861: a running sub-agent 10 levels below the topic's thread, its idle
    ancestors unloaded, and no event told of any. The walk up stops at the cap, and the
    stop said none ran, complete. Past the cap the walk is incomplete: `BackendUnavailable`."""
    fake, connect = daemon
    backend = await connect()
    (made,) = await sessions(backend, 1)
    source = {"subAgent": {"thread_spawn": {"parent_thread_id": "", "depth": 1}}}
    chain = [made.native_id]
    for _ in range(10):
        thread = fake._new_thread({}, "/w/0", parent=chain[-1], source=source)
        fake.loaded.discard(thread["id"])  # idle, and unloaded
        chain.append(thread["id"])
    fake.loaded.add(chain[-1])  # the leaf runs
    fake.threads[chain[-1]]["turns"].append(
        {"id": "turn-deep", "status": "inProgress", "items": [], "started": 0}
    )
    with pytest.raises(t.BackendUnavailable):
        await backend.stop_subagents(made.native_id)
    assert not stops(fake)


async def test_a_slow_unrelated_read_keeps_the_subagents_already_found(daemon, monkeypatch):
    """Codex round 2 on #861: `asyncio.gather` gave no read until all were done, and the edges
    were made only after the walk up. So one slow read of an unrelated loaded thread, past
    the pass's budget, discarded the sub-agent already read, and after a restart `!stop` left
    it running. Each edge is kept as its read completes: the stop interrupts it, and says
    that not every thread could be checked (`unseen`)."""
    from zab.backends import codex as codex_backend

    fake, connect = daemon
    backend, made, child = await subagent_across_a_restart(fake, connect)
    other = fake._new_thread({}, "/w/other")  # loaded, after the child: read after it
    monkeypatch.setattr(codex_backend, "SUBAGENT_STOP_S", 1.0)  # the pass gets 0.5 s
    read = fake._thread_read

    async def slow(params):
        if params["threadId"] == other["id"]:
            await asyncio.sleep(0.7)  # past the pass's budget, within the stop's
        return await read(params)

    monkeypatch.setattr(fake, "_thread_read", slow)
    assert await backend.stop_subagents(made.native_id) == t.SubagentStop(1, unseen=True)
    assert stops(fake) == [child] and status(fake, child) == "interrupted"
