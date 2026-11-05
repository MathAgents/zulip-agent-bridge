"""The Codex side question (`!btw`, issue #716, SPEC §8.10): an ephemeral `thread/fork` through
the last ended turn and one read-only turn with approval `never`, against the scripted
app-server `FakeAppServer` (SPEC §15.2), whose wire is checked against the pinned schemas. The
parent's thread, its running turn and the backend's state are untouched, an approval is
declined, a tool stops it, and no event reaches the core. Codex never runs here."""

from __future__ import annotations

import asyncio
import json

import pytest
from jsonschema import Draft7Validator

from backends.test_codex_fork import SCHEMAS, spec, turn, until
from backends.test_codex_fork import codex as codex
from zab.backends import codex as cx
from zab.backends import codex_rpc
from zab.core import types as t
from zab.testing.fake_codex import RpcFailure

pytestmark = pytest.mark.contract
QUESTION = "what was the codeword?"


def side_thread(fake) -> dict | None:
    return next((x for x in fake.threads.values() if x.get("ephemeral")), None)


def state_of(backend) -> dict:
    """The backend's per-thread bookkeeping, which a side question must leave as it was."""
    return {
        name: dict(getattr(backend, name))
        for name in ("settings", "specs", "observed", "policies", "defaults", "usage_observations")
    } | {"active": dict(backend.mapper.active), "closed": set(backend.mapper.closed)}


async def parent_with_a_turn(fake, backend):
    sid = (await backend.create_session(spec("/w/parent", "topic", model="gpt-other"))).native_id
    first = await turn(backend, sid, "remember orchid")
    await backend.update_settings(sid, t.SessionSettings(model="gpt-other", effort="high"))
    return sid, first


async def test_an_ephemeral_fork_answers_read_only_and_leaves_nothing(codex):
    fake, backend = codex
    sid, first = await parent_with_a_turn(fake, backend)
    before, state = json.dumps(fake.threads[sid], sort_keys=True), state_of(backend)
    calls = len(fake.calls)

    assert await backend.side_question(sid, QUESTION, timeout_s=10) == "FAKE_OK 2"
    (fork,) = fake.requests("thread/fork")
    assert fork["threadId"] == sid and fork["lastTurnId"] == first
    assert fork["ephemeral"] is True and fork["excludeTurns"] is True
    assert (fork["approvalPolicy"], fork["sandbox"], fork["cwd"]) == (
        "never",
        "read-only",
        "/w/parent",
    )
    assert fork["model"] == "gpt-other"
    assert fork["developerInstructions"].startswith("bridge context\n\n" + cx.SIDE_QUESTION_FRAME)
    started = fake.requests("turn/start")[-1]
    child = started["threadId"]
    assert child != sid and fake.requests("thread/unsubscribe") == [{"threadId": child}]
    assert started["approvalPolicy"] == "never"
    assert started["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert (started["model"], started["effort"]) == ("gpt-other", "high")
    text = started["input"][0]["text"]
    assert text.startswith("<zab-context>\n" + cx.SIDE_QUESTION_FRAME) and text.endswith(QUESTION)
    assert child not in fake.threads  # an ephemeral thread is gone once unsubscribed
    # the parent: only read (its turn list), never resumed, set, renamed or started
    methods = [c["method"] for c in fake.calls[calls:] if c["params"].get("threadId") == sid]
    assert methods == ["thread/turns/list", "thread/fork"]
    assert json.dumps(fake.threads[sid], sort_keys=True) == before
    assert state_of(backend) == state and not backend.side
    await asyncio.sleep(0.05)  # the fork's `thread/closed` arrives after the unsubscribe
    assert backend.queue.empty() and child not in backend.mapper.closed


#: The VMs' Codex config (workshop/ami/build.sh): two MCP servers and the lean4-skills plugin.
VM_CONFIG = {
    "mcp_servers": {
        "lmfdb": {"url": "https://mcp.lmfdb.org/mcp"},
        "lean-lsp": {"command": "/home/agent/.local/bin/lean-lsp-mcp", "tool_timeout_sec": 900},
    },
    "plugins": {"lean4@lean4-skills": {"enabled": True}},
    "apps": {"_default": {"enabled": True}, "gmail": {"enabled": True}},  # N11
    "web_search": "live",
}


async def test_the_fork_runs_without_mcp_servers_plugins_apps_or_web_search(codex):
    """Review of #739 (B1): MCP tools run outside the exec sandbox, so the fork's `config`
    turns every MCP server and plugin of the cwd's effective config off, by name."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.effective_config = VM_CONFIG
    await backend.side_question(sid, QUESTION, timeout_s=10)
    assert fake.requests("config/read")[-1] == {"cwd": "/w/parent", "includeLayers": False}
    assert fake.requests("thread/fork")[-1]["config"] == {
        "apps._default.enabled": False,
        "features.apps": False,  # no built-in `codex_apps` server
        "web_search": "disabled",
        "mcp_servers.lmfdb.enabled": False,
        "mcp_servers.lean-lsp.enabled": False,
        "plugins.lean4@lean4-skills.enabled": False,
        "apps.gmail.enabled": False,  # an app entry may turn one on over `_default`
    }
    # the parent and its next turns keep theirs
    assert fake.threads[sid]["config"]["shell_environment_policy.set"]["ZAB_BOT"] == "codex"
    # and the fork's servers were checked off for its thread, before its turn
    child = fake.requests("turn/start")[-1]["threadId"]
    (status,) = fake.requests("mcpServerStatus/list")
    assert status == {"threadId": child, "detail": "toolsAndAuthOnly", "cursor": None}
    order = [c["method"] for c in fake.calls if c["params"].get("threadId") == child]
    assert order[:2] == ["mcpServerStatus/list", "turn/start"]


@pytest.mark.parametrize(
    ("states", "reported"),
    [
        ({"lean-lsp": "connected"}, False),  # `config/read` left the server out: nothing sent
        ({"lean-lsp": "starting"}, True),  # the override didn't take
        ({"lean-lsp": None}, True),  # unknown
        ({"lean-lsp": "notStarted"}, True),  # may start for the turn
    ],
)
async def test_a_server_not_off_for_the_fork_refuses_it(codex, states, reported):
    """Fail closed (review of #739): any MCP server not `disabled` for the fork's thread, also
    one that `config/read` didn't report, refuses the side question before its turn."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.effective_config = VM_CONFIG if reported else {}
    fake.mcp_states = dict(states)
    with pytest.raises(ValueError, match=r"MCP servers aren't off for the side question: ` lean"):
        await backend.side_question(sid, QUESTION, timeout_s=10)
    child = fake.requests("thread/unsubscribe")[-1]["threadId"]  # the fork, gone
    assert not [r for r in fake.requests("turn/start") if r["threadId"] == child]
    assert not backend.side and child not in fake.threads


@pytest.mark.parametrize(
    ("pages", "last", "answered"),
    [
        (3, "disabled", True),  # read to its end, all off
        (3, "connected", False),  # the last page has one on
        (cx.SIDE_MCP_PAGES + 1, "connected", False),  # a page left unread: incomplete
        (cx.SIDE_MCP_PAGES + 1, "disabled", False),
    ],
)
async def test_the_listing_is_read_to_its_end(codex, monkeypatch, pages, last, answered):
    """Codex on #739: the pages of `mcpServerStatus/list` are read to the last; one left
    after `SIDE_MCP_PAGES` refuses, since what it holds is unknown."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)

    async def paged(params):
        n = int(params.get("cursor") or 0)
        state = last if n == pages - 1 else "disabled"
        server = {"name": f"s{n}", "authStatus": "unsupported", "resources": [], "tools": {}}
        server |= {"resourceTemplates": [], "runtimeStatus": state}
        return {"data": [server], "nextCursor": str(n + 1) if n + 1 < pages else None}, []

    monkeypatch.setattr(fake, "_mcpServerStatus_list", paged)
    if answered:
        assert await backend.side_question(sid, QUESTION, timeout_s=10) == "FAKE_OK 2"
        assert len(fake.requests("mcpServerStatus/list")) == pages
        return
    with pytest.raises(ValueError, match=r"aren't off|couldn't check"):
        await backend.side_question(sid, QUESTION, timeout_s=10)
    assert len(fake.requests("mcpServerStatus/list")) == min(pages, cx.SIDE_MCP_PAGES)
    assert not backend.side


async def test_codex_apps_goes_with_the_apps_feature(codex, monkeypatch):
    """The review of #739 (R2): 0.157.1 registers its built-in `codex_apps` MCP server while
    `features.apps` is on, whatever `apps._default` says; without `features.apps = false` the
    fork would list it `notStarted`, and every side question would be refused."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    with monkeypatch.context() as m:  # not `undo()`: that undoes the conftest's patches too
        m.setattr(cx, "SIDE_CONFIG", {"apps._default.enabled": False})
        with pytest.raises(ValueError, match=r"aren't off for the side question: ` codex_apps `"):
            await backend.side_question(sid, QUESTION, timeout_s=10)
    assert await backend.side_question(sid, QUESTION, timeout_s=10) == "FAKE_OK 2"
    assert fake.requests("thread/fork")[-1]["config"]["features.apps"] is False


async def test_a_listing_that_fails_refuses_too(codex, monkeypatch):
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)

    async def refused(params):
        raise RpcFailure(-32601, "unsupported method")

    monkeypatch.setattr(fake, "_mcpServerStatus_list", refused)
    with pytest.raises(ValueError, match=r"couldn't check that this agent's MCP servers are off"):
        await backend.side_question(sid, QUESTION, timeout_s=10)
    assert fake.requests("thread/unsubscribe") and not backend.side


async def test_a_name_no_dotted_key_can_address_refuses(codex):
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.effective_config = {"mcp_servers": {"lean.lsp": {"command": "x"}}}
    with pytest.raises(ValueError, match=r"can't turn off this agent's mcp_servers entry"):
        await backend.side_question(sid, QUESTION, timeout_s=10)
    assert not fake.requests("thread/fork")


async def test_a_hook_prompt_doesnt_stop_it(codex):
    """Review of #739 (N2): a hook's prompt is text its hook's own run added, not a tool."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.side_items = [{"type": "hookPrompt", "fragments": []}]
    assert await backend.side_question(sid, QUESTION, timeout_s=10) == "FAKE_OK 2"


async def test_a_side_question_while_the_parent_runs(codex):
    fake, backend = codex
    sid, first = await parent_with_a_turn(fake, backend)
    fake.hold = True  # the parent's next turn runs on; `hold_side` stays off
    running = await turn(backend, sid, "count the primes below 10^9", wait=False)

    assert await backend.side_question(sid, QUESTION, timeout_s=10) == "FAKE_OK 2"
    assert fake.requests("thread/fork")[-1]["lastTurnId"] == first  # the running turn stays out
    (items,) = fake.requests("thread/items/list")
    assert items == {
        "threadId": sid,
        "turnId": running,
        "limit": cx.PROGRESS_ITEMS,
        "sortDirection": "desc",
    }
    text = fake.requests("turn/start")[-1]["input"][0]["text"]
    assert "The turn still running" in text and "- your user: count the primes" in text
    assert fake.threads[sid]["turns"][-1]["status"] == "inProgress"  # untouched
    assert backend.mapper.active[sid] == running
    await fake.finish(sid, "PARENT_DONE")
    done = await until(backend, t.TurnCompleted, sid)
    assert done.turn_id == running and done.final_text == "PARENT_DONE"


@pytest.mark.parametrize(
    ("request_method", "answer"),
    [
        ("item/commandExecution/requestApproval", {"result": {"decision": "decline"}}),
        ("item/permissions/requestApproval", {"error": {"code": -32601}}),
    ],
)
async def test_an_approval_is_declined_and_never_reaches_the_core(codex, request_method, answer):
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.side_request = request_method
    assert await backend.side_question(sid, QUESTION, timeout_s=10) == "FAKE_OK 2"
    async with asyncio.timeout(5):
        while not fake.responses:  # noqa: ASYNC110 - the decline crosses the socket
            await asyncio.sleep(0.01)
    (response,) = fake.responses.values()
    assert {k: v for k, v in response.items() if k in answer} | (
        {"error": {"code": response["error"]["code"]}} if "error" in response else {}
    ) == answer
    assert backend.queue.empty() and not backend.requests  # no ApprovalRequested


async def test_an_approval_after_the_answer_is_declined_too(codex):
    """Review of #739 (N1): a request for a fork whose side question has ended is answered,
    not left to hang a thread nobody watches."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    await backend.side_question(sid, QUESTION, timeout_s=10)
    child = fake.requests("turn/start")[-1]["threadId"]
    params = {"threadId": child, "turnId": "turn-x", "itemId": "item-x", "startedAtMs": 0}
    late = {"id": "srv-late", "method": "item/commandExecution/requestApproval", "params": params}
    await fake._send(late)
    async with asyncio.timeout(5):
        while "srv-late" not in fake.responses:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    assert fake.responses["srv-late"]["result"] == {"decision": "decline"}
    assert backend.queue.empty() and not backend.requests and not backend.side


async def test_a_tool_stops_the_side_question(codex):
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.side_items = [
        {"type": "commandExecution", "command": "ls", "cwd": "/w", "status": "inProgress"}
    ]
    with pytest.raises(ValueError, match=r"reached for a tool \(` commandExecution `\)"):
        await backend.side_question(sid, QUESTION, timeout_s=10)
    child = fake.requests("turn/start")[-1]["threadId"]
    assert fake.requests("turn/interrupt")[-1]["threadId"] == child
    assert fake.requests("thread/unsubscribe") == [{"threadId": child}] and not backend.side


async def test_no_completed_turn_is_refused(codex):
    fake, backend = codex
    sid = (await backend.create_session(spec("/w/parent", "topic"))).native_id
    with pytest.raises(ValueError, match="no completed turn"):
        await backend.side_question(sid, QUESTION, timeout_s=10)
    fake.hold = True
    await turn(backend, sid, "first", wait=False)
    with pytest.raises(ValueError, match="no completed turn"):  # its first turn runs
        await backend.side_question(sid, QUESTION, timeout_s=10)
    assert not fake.requests("thread/fork")


async def test_a_timeout_stops_the_turn_and_forgets_the_fork(codex):
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.hold_side = True
    with pytest.raises(TimeoutError):
        await backend.side_question(sid, QUESTION, timeout_s=0.5)
    child = fake.requests("turn/start")[-1]["threadId"]
    assert fake.requests("turn/interrupt")[-1]["threadId"] == child
    assert child not in fake.threads and not backend.side


async def test_an_interrupt_that_fails_still_unsubscribes(codex, monkeypatch):
    """Codex round 1 on #739: the turn may have ended, and its interrupt be refused."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.hold_side = True

    async def refused(params):
        raise RpcFailure(-32600, "no active turn to interrupt")

    monkeypatch.setattr(fake, "_turn_interrupt", refused)
    with pytest.raises(TimeoutError):
        await backend.side_question(sid, QUESTION, timeout_s=0.5)
    child = fake.requests("turn/start")[-1]["threadId"]
    assert fake.requests("turn/interrupt")[-1]["threadId"] == child
    assert fake.requests("thread/unsubscribe") == [{"threadId": child}]
    assert child not in fake.threads and not backend.side


@pytest.mark.parametrize("slow", ["thread/fork", "turn/start"])
async def test_a_fork_or_turn_made_after_the_timeout_is_cleaned_up(codex, monkeypatch, slow):
    """Codex rounds 2 and 4 on #739: the side question gave up while the server was still
    making its fork, or starting its turn, and the answer came after the RPC's own deadline
    too; it is still stopped and unsubscribed."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.hold_side = True
    name = "_" + slow.replace("/", "_")
    original = getattr(fake, name)

    async def late(params):
        await asyncio.sleep(0.6)
        return await original(params)

    monkeypatch.setattr(fake, name, late)
    monkeypatch.setattr(codex_rpc, "CALL_TIMEOUT_S", 0.45)  # past it, an answer is dropped
    with pytest.raises(TimeoutError):
        await backend.side_question(sid, QUESTION, timeout_s=0.3)
    # a late fork: its cleanup's unsubscribe; a late turn: the side question's own, then its
    # cleanup's, with the interrupt
    expected = 1 if slow == "thread/fork" else 2
    async with asyncio.timeout(5):
        while len(fake.requests("thread/unsubscribe")) < expected or backend.side_cleanups:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    (made,) = [c["params"] for c in fake.calls if c["method"] == "thread/fork"]
    assert made["ephemeral"] and side_thread(fake) is None  # gone at its unsubscribe
    (unsubscribed,) = {r["threadId"] for r in fake.requests("thread/unsubscribe")}
    if slow == "turn/start":  # its turn was interrupted too, once known
        assert fake.requests("turn/interrupt")[-1]["threadId"] == unsubscribed
    assert not backend.side and not backend.side_cleanups and backend.queue.empty()


async def test_a_result_its_cancelled_caller_missed_is_cleaned_up(codex):
    """Codex round 3 on #739: the request answered, but the cancel woke the caller first."""
    _, backend = codex
    seen: list[dict] = []

    async def late(result):
        seen.append(result)

    call = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(backend._kept(call, late))
    await asyncio.sleep(0)  # it awaits the request
    call.set_result({"thread": {"id": "fork-1"}})
    waiter.cancel()  # before it wakes with the result
    with pytest.raises(asyncio.CancelledError):
        await waiter
    async with asyncio.timeout(5):
        while backend.side_cleanups or not seen:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    assert seen == [{"thread": {"id": "fork-1"}}]
    failed = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(backend._kept(failed, late))
    await asyncio.sleep(0)
    failed.set_exception(RuntimeError("refused"))
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0.05)
    assert seen == [{"thread": {"id": "fork-1"}}] and not backend.side_cleanups  # nothing made


async def test_a_failed_turn_is_one_line(codex):
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.hold_side = True
    task = asyncio.create_task(backend.side_question(sid, QUESTION, timeout_s=10))
    async with asyncio.timeout(5):
        while (thread := side_thread(fake)) is None or not thread["turns"][-1:]:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    while thread["turns"][-1]["status"] != "inProgress":  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    failed = fake._turn_json(thread["turns"][-1]) | {
        "status": "failed",
        "error": {
            "message": "You've hit your usage limit.",
            "codexErrorInfo": "usageLimitExceeded",
        },
    }
    await fake._send(fake._note("turn/completed", threadId=thread["id"], turn=failed))
    with pytest.raises(ValueError, match=r"^Codex couldn't answer \(usage_limit\): You've hit"):
        await task


async def test_the_side_question_speaks_the_pinned_schemas(codex):
    """Every message of a side question while the parent runs, an approval included, both
    ways, against the generated schemas, the requests' params closed (as for the fork)."""
    fake, backend = codex
    sid, _ = await parent_with_a_turn(fake, backend)
    fake.hold = True
    await turn(backend, sid, "long", wait=False)
    fake.side_request = "item/commandExecution/requestApproval"
    fake.effective_config = VM_CONFIG  # servers to list, and overrides to send
    start = len(fake.wire)
    await backend.side_question(sid, QUESTION, timeout_s=10)
    async with asyncio.timeout(5):
        while not fake.responses:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    def validator(name, closed=False):
        schema = json.loads(next(SCHEMAS.rglob(f"{name}.json")).read_text())
        if closed:
            for key, definition in schema["definitions"].items():
                if key.endswith("Params") and definition.get("type") == "object":
                    definition["additionalProperties"] = False
        return Draft7Validator(schema)

    sides = {s: validator(s) for s in ("ServerNotification", "ServerRequest")}
    sides["ClientRequest"] = validator("ClientRequest", closed=True)
    methods, checked = {}, set()
    for direction, message in fake.wire[start:]:
        if "method" in message:
            side = "Client" if direction == "send" else "Server"
            kind = "Request" if "id" in message else "Notification"
            sides[side + kind].validate(message)
            methods[message.get("id")] = message["method"]
        elif "result" in message:
            method = methods[message["id"]]
            names = {
                "item/commandExecution/requestApproval": "CommandExecutionRequestApproval",
                "mcpServerStatus/list": "ListMcpServerStatus",
            }
            name = names.get(method, "".join(p.title() for p in method.split("/")))
            validator(f"{name}Response").validate(message["result"])
        checked.add(message.get("method") or methods[message["id"]])
    assert {
        "thread/turns/list",
        "thread/items/list",
        "thread/fork",
        "turn/start",
        "item/commandExecution/requestApproval",
        "turn/completed",
        "thread/unsubscribe",
        "mcpServerStatus/list",
    } <= checked
