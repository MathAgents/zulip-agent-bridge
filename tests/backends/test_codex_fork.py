"""The Codex fork (issue #202): `thread/fork` through the parent's last ended turn, with the
parent's settings passed explicitly, and nothing changed for the parent. Run against the
scripted app-server `FakeAppServer` (SPEC §15.2), whose wire is checked against the pinned
schemas; Codex itself never runs on the development machine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from zab.backends.codex import CodexBackend
from zab.backends.codex_rpc import RpcError
from zab.config import BackendConfig
from zab.core import types as t
from zab.testing.fake_codex import FakeAppServer

pytestmark = pytest.mark.contract
SCHEMAS = Path(__file__).resolve().parents[2] / "docs/protocol/codex-0.157.1"
WHO = t.GitIdentity("Codex · test", "codex-bot@example.zulipchat.com")


@pytest.fixture
async def codex(tmp_path):
    fake = FakeAppServer(str(tmp_path))
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


async def turn(backend, sid, text, *, wait=True):
    tid = await backend.start_turn(sid, t.UserInput(text=text, client_id=f"client-{text}"))
    if wait:
        done = await until(backend, t.TurnCompleted, sid)
        assert done.turn_id == tid
    return tid


def spec(cwd, name, **settings):
    return t.SessionSpec(
        cwd=cwd,
        name=name,
        settings=t.SessionSettings(**settings),
        context_prompt="bridge context",
        git_identity=WHO,
        bot="codex",
    )


def user_texts(thread):
    return [
        item["content"][0]["text"]
        for turn in thread["turns"]
        for item in turn["items"]
        if item["type"] == "userMessage"
    ]


def parent_calls(fake, parent, since):
    return [c["method"] for c in fake.calls[since:] if c["params"].get("threadId") == parent]


async def test_codex_forks_are_enabled(codex):
    _, backend = codex
    assert backend.capabilities.fork


async def test_a_fork_of_an_idle_parent_copies_its_turns_with_explicit_settings(codex):
    fake, backend = codex
    parent = await backend.create_session(
        spec("/w/parent", "topic", approval="never", sandbox="read-only", model="gpt-other")
    )
    await backend.update_settings(
        parent.native_id, t.SessionSettings(approval="never", sandbox="read-only", effort="high")
    )
    first = await turn(backend, parent.native_id, "remember orchid")
    before = json.dumps(fake.threads[parent.native_id], sort_keys=True)
    calls = len(fake.calls)

    child = await backend.fork_session(
        parent.native_id,
        spec("/w/fork", "topic ⑂ 1", approval="never", sandbox="read-only"),
    )
    (request,) = fake.requests("thread/fork")
    assert request["threadId"] == parent.native_id and request["lastTurnId"] == first
    assert request["cwd"] == "/w/fork" and request["excludeTurns"] is True
    assert (request["model"], request["approvalPolicy"], request["sandbox"]) == (
        "gpt-other",
        "never",
        "read-only",
    )
    assert request["developerInstructions"] == "bridge context"
    identity = request["config"]["shell_environment_policy.set"]
    assert identity["GIT_AUTHOR_EMAIL"] == WHO.email and identity["ZAB_BOT"] == "codex"
    assert child.native_id != parent.native_id and child.cwd == "/w/fork"
    assert (child.settings.model, child.settings.effort) == ("gpt-other", "high")
    assert child.settings.extra["applied_git_identity"] == {"name": WHO.name, "email": WHO.email}
    forked = fake.threads[child.native_id]
    assert forked["forked_from"] == parent.native_id and forked["name"] == "topic ⑂ 1"
    assert user_texts(forked) == ["remember orchid"]

    assert child.native_id not in backend.mapper.totals  # no zero baseline (review of #568)
    await turn(backend, child.native_id, "side task")
    await turn(backend, child.native_id, "more")
    starts = [r for r in fake.requests("turn/start") if r["threadId"] == child.native_id]
    for start in starts:  # each of the child's turns, as the parent's own turns
        assert (start["model"], start["effort"], start["approvalPolicy"]) == (
            "gpt-other",
            "high",
            "never",
        )
        assert start["sandboxPolicy"] == {"type": "readOnly"}
    assert starts[0]["input"][0]["text"].startswith("<zab-context>\nbridge context\n")  # §8.4
    assert not starts[1]["input"][0]["text"].startswith("<zab-context>")
    # the parent was read (its turn list, the fork) and nothing else
    assert parent_calls(fake, parent.native_id, calls) == ["thread/turns/list", "thread/fork"]
    assert json.dumps(fake.threads[parent.native_id], sort_keys=True) == before
    await turn(backend, parent.native_id, "next")  # and goes on as before
    assert user_texts(fake.threads[parent.native_id]) == ["remember orchid", "next"]
    assert user_texts(fake.threads[child.native_id])[-2].endswith("side task")


async def test_a_fork_while_the_parent_runs_stops_at_its_last_ended_turn(codex):
    fake, backend = codex
    parent = (await backend.create_session(spec("/w/parent", "topic", approval="never"))).native_id
    first = await turn(backend, parent, "first")
    fake.hold = True
    running = await turn(backend, parent, "long job", wait=False)
    await until(backend, t.TurnStarted, parent)

    child = await backend.fork_session(parent, spec("/w/fork", "topic ⑂ 1"))
    assert fake.requests("thread/fork")[0]["lastTurnId"] == first
    assert user_texts(fake.threads[child.native_id]) == ["first"]
    fake.hold = False
    await turn(backend, child.native_id, "side task")  # runs beside the parent's turn

    await fake.finish(parent, "LONG_DONE")
    done = await until(backend, t.TurnCompleted, parent)
    assert (done.turn_id, done.status, done.final_text) == (
        running,
        t.TurnStatus.COMPLETED,
        "LONG_DONE",
    )
    assert user_texts(fake.threads[parent]) == ["first", "long job"]
    assert "long job" not in " ".join(user_texts(fake.threads[child.native_id]))


async def test_a_stopped_turn_has_ended_and_is_forked(codex):
    """DECISIONS, "The fork boundary": a turn `!stop` ended is part of the history."""
    fake, backend = codex
    parent = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    fake.hold = True
    stopped = await turn(backend, parent, "stopped job", wait=False)
    await until(backend, t.TurnStarted, parent)
    await backend.interrupt(parent)
    assert (await until(backend, t.TurnCompleted, parent)).status == t.TurnStatus.INTERRUPTED
    await backend.fork_session(parent, spec("/w/fork", "f"))
    assert fake.requests("thread/fork")[0]["lastTurnId"] == stopped


async def test_a_fork_needs_an_open_parent_with_an_ended_turn(codex):
    fake, backend = codex
    with pytest.raises(ValueError, match="isn't open"):
        await backend.fork_session("unknown", spec("/w/fork", "f"))
    parent = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    with pytest.raises(ValueError, match="no completed turn"):
        await backend.fork_session(parent, spec("/w/fork", "f"))
    fake.hold = True
    running = await turn(backend, parent, "only a running turn", wait=False)
    with pytest.raises(ValueError, match="no completed turn"):
        await backend.fork_session(parent, spec("/w/fork", "f"))
    assert not fake.requests("thread/fork")
    # what the VERIFY found, and the fake models: a turn in progress is no fork boundary
    with pytest.raises(RpcError) as refused:
        await backend._call("thread/fork", threadId=parent, lastTurnId=running)
    assert refused.value.code == -32600


async def test_the_parents_resolved_policy_is_passed_when_it_set_none(codex):
    """A native fork takes the server's defaults, not the parent's (DECISIONS 2026-10-10). What
    the parent runs with without having set it goes on `thread/fork` only: the server then
    builds the whole policy as for `thread/start`, and the child's turns, like the parent's,
    send none, which keeps config.toml's network access and writable roots (review of #568)."""
    fake, backend = codex
    fake.defaults.update(approval="never", sandbox="read-only")  # what the parent started with
    parent = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    await turn(backend, parent, "first")
    fake.defaults.update(approval="on-request", sandbox="workspace-write")
    child = await backend.fork_session(parent, spec("/w/fork", "f"))
    request = fake.requests("thread/fork")[0]
    assert (request["approvalPolicy"], request["sandbox"]) == ("never", "read-only")
    assert (child.settings.approval, child.settings.sandbox) == (None, None)
    assert fake.threads[child.native_id]["sandbox"] == "read-only"
    await turn(backend, child.native_id, "side")
    start = fake.requests("turn/start")[-1]
    assert "sandboxPolicy" not in start and "approvalPolicy" not in start
    assert not [r for r in fake.requests("turn/start") if "sandboxPolicy" in r]  # nor the parent's


async def test_a_settings_update_from_another_client_is_what_a_fork_gets(codex):
    """Review of #568: `thread/settings/updated` (another client changed the thread's approval or
    sandbox) also refreshes what a fork falls back to."""
    fake, backend = codex
    parent = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    await turn(backend, parent, "first")
    settings = {
        "approvalPolicy": "untrusted",
        "approvalsReviewer": "user",
        "collaborationMode": {"mode": "default", "settings": {"model": "gpt-fake"}},
        "cwd": "/w/parent",
        "model": "gpt-fake",
        "modelProvider": "openai",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    }
    await fake._send(
        {
            "method": "thread/settings/updated",
            "params": {"threadId": parent, "threadSettings": settings},
        }
    )
    async with asyncio.timeout(5):
        while backend.policies[parent].sandbox != "danger-full-access":  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await backend.fork_session(parent, spec("/w/fork", "f"))
    request = fake.requests("thread/fork")[0]
    assert (request["approvalPolicy"], request["sandbox"]) == ("untrusted", "danger-full-access")


async def test_the_specs_own_values_win_over_the_parents(codex):
    """Review of #568: an explicit spec value that differs from the parent's is the child's."""
    fake, backend = codex
    parent = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    await turn(backend, parent, "first")
    child = await backend.fork_session(
        parent, spec("/w/fork", "f", model="gpt-other", effort="low", sandbox="read-only")
    )
    request = fake.requests("thread/fork")[0]
    assert (request["model"], request["sandbox"]) == ("gpt-other", "read-only")
    assert (child.settings.model, child.settings.effort) == ("gpt-other", "low")
    await turn(backend, child.native_id, "side")
    start = fake.requests("turn/start")[-1]
    assert (start["model"], start["effort"], start["sandboxPolicy"]) == (
        "gpt-other",
        "low",
        {"type": "readOnly"},
    )
    assert backend.effective_settings(parent).model == "gpt-fake"  # the parent's own


async def test_a_closed_parent_is_reopened_as_its_next_turn_would(codex):
    fake, backend = codex
    parent = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    first = await turn(backend, parent, "first")
    fake.loaded.discard(parent)  # the app-server unloaded it, and said so (thread/closed)
    backend.mapper.closed.add(parent)
    await backend.fork_session(parent, spec("/w/fork", "f"))
    assert "thread/resume" in [c["method"] for c in fake.calls]
    assert fake.requests("thread/fork")[0]["lastTurnId"] == first


async def test_the_fake_speaks_the_pinned_schemas(codex):
    """Every message of a fork while the parent runs, both ways, against the generated schemas,
    with the requests' params closed: zab sends no field 0.157.1 doesn't define, and the fake
    answers in the shapes it defines (types, required fields)."""
    fake, backend = codex
    parent = (await backend.create_session(spec("/w/parent", "topic", approval="never"))).native_id
    await turn(backend, parent, "first")
    fake.hold = True
    await turn(backend, parent, "long", wait=False)
    child = await backend.fork_session(parent, spec("/w/fork", "f", sandbox="workspace-write"))
    fake.hold = False
    await turn(backend, child.native_id, "side")
    await backend.interrupt(parent)
    await until(backend, t.TurnCompleted, parent)

    stray = {"id": 1, "method": "thread/fork", "params": {"threadId": "t", "effort": "low"}}
    closed = validator("ClientRequest", closed=True)
    assert not closed.is_valid(stray)  # a field 0.157.1 doesn't define fails
    checked = check_wire(fake)
    assert {"thread/fork", "thread/turns/list", "turn/start", "turn/completed"} <= checked


def validator(name, closed=False):
    schema = json.loads(next(SCHEMAS.rglob(f"{name}.json")).read_text())
    if closed:  # the pinned *Params leave additional properties open (review of #568)
        for key, definition in schema["definitions"].items():
            if key.endswith("Params") and definition.get("type") == "object":
                definition["additionalProperties"] = False
    return Draft7Validator(schema)


def check_wire(fake) -> set[str]:
    """Validate every message of `fake.wire`, both ways, against the pinned schemas, the
    requests' params closed; the methods seen."""
    sides = {s: validator(s) for s in ("ClientNotification", "ServerNotification")}
    sides["ClientRequest"] = validator("ClientRequest", closed=True)
    methods, checked = {}, set()
    for direction, message in fake.wire:
        if "method" in message:
            side = "Client" if direction == "send" else "Server"
            kind = "Request" if "id" in message else "Notification"
            sides[side + kind].validate(message)
            methods[message.get("id")] = message["method"]
        elif "result" in message:
            name = "".join(p.title() for p in methods[message["id"]].split("/"))
            name = {"ThreadNameSet": "ThreadSetName"}.get(name, name)
            validator(f"{name}Response").validate(message["result"])
        checked.add(message.get("method") or methods[message["id"]])
    return checked
