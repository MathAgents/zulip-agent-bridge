from unittest.mock import AsyncMock

import pytest

from zab.backends.codex import CodexBackend, identity_config
from zab.backends.codex_rpc import RpcError
from zab.config import BackendConfig
from zab.core import types as t

pytestmark = pytest.mark.contract


def backend():
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    b.epoch = "epoch"
    # Environment-policy RPCs are exercised separately in test_codex_identity.
    b._identity_config = AsyncMock(side_effect=identity_config)
    return b


async def test_create_and_steer_use_verified_fields():
    b = backend()
    b.rpc.call.side_effect = [
        {"thread": {"id": "s", "cwd": "/repo"}, "model": "m"},
        {},
        {"turn": {"id": "t"}},
        {},
    ]
    info = await b.create_session(
        t.SessionSpec(
            cwd="/repo",
            name="topic",
            context_prompt="context",
            settings=t.SessionSettings(approval="never"),
        )
    )
    assert info.native_id == "s"
    assert b.rpc.call.call_args_list[0].args[1]["developerInstructions"] == "context"
    assert await b.start_turn("s", t.UserInput(text="hello", client_id="one")) == "t"
    await b.steer("s", "t", t.UserInput(text="update", client_id="two"))
    assert b.rpc.call.call_args.args[1]["expectedTurnId"] == "t"
    b.rpc.call.side_effect = RpcError({"code": -1, "message": "cannot steer"})
    with pytest.raises(t.SteerRejected):
        await b.steer("s", "t", t.UserInput(text="late", client_id="three"))


async def test_only_owned_approvals_are_answered_by_default():
    b = backend()
    message = {
        "id": 7,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "s", "turnId": "t", "command": "true"},
    }
    await b._approval(message)
    assert b.queue.empty()
    b.rpc.send.assert_not_called()
    b.own_turns.add(("s", "t"))
    await b._approval(message)
    event = b.queue.get_nowait()
    assert event.request_id == "c:epoch:7"
    await b.respond(event.request_id, t.ApprovalDecision.DECLINE)
    b.rpc.send.assert_awaited_once_with({"id": 7, "result": {"decision": "decline"}})


@pytest.mark.parametrize("context", ["", "Bridge owner boundary"])
async def test_resume_gets_summary_and_active_turn(context):
    b = backend()
    b.rpc.call.side_effect = [
        {"data": []},  # the thread is cold; its off identity can be applied
        {"thread": {"id": "s", "cwd": "/repo"}},
        {
            "data": [
                {
                    "id": "running",
                    "status": "inProgress",
                    "items": [
                        {
                            "id": "u",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "latest question"}],
                        }
                    ],
                },
                {
                    "id": "done",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "answer"}],
                },
            ]
        },
    ]
    info = await b.open_session(
        "s", t.SessionSpec(cwd="/repo", name="topic", context_prompt=context)
    )
    resume_params = b.rpc.call.call_args_list[1].args[1]
    if context:
        assert resume_params["developerInstructions"] == context
    else:
        assert "developerInstructions" not in resume_params
    assert info.active_turn_id == "running"
    assert info.preview == "latest question"
    assert info.last_turn.final_text == "answer"
    assert b.rpc.call.call_args_list[1].args[1]["excludeTurns"] is True


async def test_unavailable_backend_start_does_not_raise(tmp_path):
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home=str(tmp_path)))
    await b.start()
    try:
        with pytest.raises(t.BackendUnavailable):
            await b.list_sessions(None)
    finally:
        await b.close()


@pytest.mark.parametrize("steer", [False, True])
async def test_attach_context_reaches_first_accepted_input_only(steer):
    b = backend()
    b.rpc.call.side_effect = [
        {"data": []},  # the thread is cold; its off identity can be applied
        {"thread": {"id": "s", "cwd": "/repo"}},
        {"data": []},
    ]
    await b.open_session(
        "s", t.SessionSpec(cwd="/repo", name="topic", context_prompt="Bridge context")
    )
    turn_input = t.UserInput(text="continue", client_id="one")

    async def submit():
        if steer:
            return await b.steer("s", "t", turn_input)
        return await b.start_turn("s", turn_input)

    b.rpc.call.side_effect = RpcError({"code": -1, "message": "not accepted"})
    with pytest.raises(t.SteerRejected if steer else RpcError):
        await submit()
    b.rpc.call.side_effect = None
    b.rpc.call.return_value = {"turn": {"id": "t"}}
    await submit()
    assert b.rpc.call.call_args.args[1]["input"][0]["text"] == (
        "<zab-context>\nBridge context\n</zab-context>\n\ncontinue"
    )
    assert turn_input.text == "continue"
    await submit()
    assert b.rpc.call.call_args.args[1]["input"][0]["text"] == "continue"


async def test_unsupported_external_request_is_left_for_owning_client():
    b = backend()
    await b._approval(
        {
            "id": 7,
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "s", "turnId": "external"},
        }
    )
    b.rpc.send.assert_not_awaited()


async def test_an_unsupported_request_of_an_own_turn_is_declined_with_a_plain_notice():
    """§8.5: refused with "not supported by zab", and a Notice that says who is speaking. Nothing
    failed for the user, so it is information (🔧, issue #677)."""
    b = backend()
    b.own_turns.add(("s", "t"))
    method = "item/tool/requestUserInput"
    await b._approval({"id": 7, "method": method, "params": {"threadId": "s", "turnId": "t"}})
    assert b.rpc.send.call_args.args[0]["error"]["message"] == "not supported by zab"
    notice = b.queue.get_nowait()
    assert isinstance(notice, t.Notice) and notice.level == "info"
    assert notice.message == (
        "Codex asked for something the bridge can't do from Zulip "
        "(` item/tool/requestUserInput `), so the bridge declined it. Codex was told and can "
        "carry on."
    )


async def test_updated_settings_survive_closed_thread_reopen():
    b = backend()
    b.specs["s"] = t.SessionSpec(cwd="/repo", name="topic", settings=t.SessionSettings(model="old"))
    await b.update_settings("s", t.SessionSettings(model="new"))
    b.mapper.closed.add("s")
    b.rpc.call.side_effect = [
        {"thread": {"id": "s", "cwd": "/repo"}},
        {"data": []},
        {"turn": {"id": "t"}},
    ]
    await b.start_turn("s", t.UserInput(text="continue", client_id="one"))
    assert b.rpc.call.call_args.args[1]["model"] == "new"


@pytest.mark.parametrize("signed_in", [True, False])
async def test_doctor_warns_on_versions_and_duplicate_processes(monkeypatch, signed_in):
    from types import SimpleNamespace

    monkeypatch.setenv("OPENAI_API_KEY", "decoy")
    backend = CodexBackend("test", BackendConfig(kind="codex", codex_home="/isolated"))
    backend._call = AsyncMock(return_value={"account": {"type": "chatgpt"} if signed_in else None})
    outputs = iter([b"codex-cli 0.0.0\n", b"2\n"])

    async def spawn(*argv, **kwargs):
        assert kwargs["env"]["CODEX_HOME"] == "/isolated"
        assert "OPENAI_API_KEY" not in kwargs["env"]
        return SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(next(outputs), b""))
        )

    monkeypatch.setattr("zab.backends.codex.asyncio.create_subprocess_exec", spawn)
    checks = {c.name: c for c in await backend.doctor()}
    assert checks["codex:login"].ok is signed_in
    assert checks["codex:login"].detail == (
        "signed in" if signed_in else "not signed in: send !login to this bot in a direct message"
    )
    assert checks["codex:version"].ok and checks["codex:version"].warning
    assert checks["codex:daemons"].warning


@pytest.mark.parametrize(
    "mode,kind",
    [
        ("read-only", "readOnly"),
        ("workspace-write", "workspaceWrite"),
        ("danger-full-access", "dangerFullAccess"),
    ],
)
async def test_next_turn_applies_updated_sandbox(mode, kind):
    b = backend()
    await b.update_settings("s", t.SessionSettings(sandbox=mode))
    b.rpc.call.return_value = {"turn": {"id": "t"}}
    await b.start_turn("s", t.UserInput(text="hello", client_id="one"))
    policy = b.rpc.call.call_args.args[1]["sandboxPolicy"]
    assert policy == {"type": kind}


async def test_resume_restores_only_persisted_owned_turns():
    b = backend()
    b.rpc.call.side_effect = [
        {"data": []},  # the thread is cold; its off identity can be applied
        {"thread": {"id": "s", "cwd": "/repo"}},
        {"data": [{"id": "external", "status": "inProgress"}]},
    ]
    await b.open_session(
        "s",
        t.SessionSpec(
            cwd="/repo",
            name="topic",
            settings=t.SessionSettings(extra={"owned_turn_ids": ["owned"]}),
        ),
    )
    assert ("s", "owned") in b.own_turns
    assert ("s", "external") not in b.own_turns


@pytest.mark.parametrize("succeeds", [True, False])
async def test_pending_start_only_releases_approvals_for_confirmed_turn(succeeds):
    b = backend()
    b.settings["s"] = t.SessionSettings()

    async def call(*args):
        for tid in ("external", "owned"):
            await b._approval(
                {
                    "id": tid,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "s", "turnId": tid, "command": "true"},
                }
            )
        assert b.queue.empty()
        b.rpc.send.assert_not_awaited()
        if not succeeds:
            raise RpcError({"code": -1, "message": "start failed"})
        return {"turn": {"id": "owned"}}

    b.rpc.call.side_effect = call
    if succeeds:
        await b.start_turn("s", t.UserInput(text="hi", client_id="one"))
        assert b.queue.get_nowait().turn_id == "owned"
    else:
        with pytest.raises(RpcError):
            await b.start_turn("s", t.UserInput(text="hi", client_id="one"))
    assert b.queue.empty()
    assert not b.pending_requests
    b.rpc.send.assert_not_awaited()


@pytest.mark.parametrize("notification", [None, "turn/started", "turn/completed"])
async def test_resume_reconciles_stale_active_without_overwriting_new_notifications(notification):
    b = backend()
    b.mapper.active["s"] = "old"

    async def call(method, params):
        if method == "thread/resume":
            return {"thread": {"id": "s", "cwd": "/repo"}}
        if notification:
            b.mapper.notification(
                notification,
                {
                    "threadId": "s",
                    "turn": {"id": "new", "status": "completed"},
                },
            )
        # The list snapshot predates a notification delivered during this lookup.
        return {"data": [{"id": "old", "status": "inProgress"}] if notification else []}

    b.rpc.call.side_effect = call
    info = await b.open_session("s", t.SessionSpec(cwd="/repo", name="topic"))
    expected = "new" if notification == "turn/started" else None
    assert info.active_turn_id == b.mapper.active.get("s") == expected
    if expected is None:
        b.rpc.call.reset_mock()
        await b.interrupt("s")
        b.rpc.call.assert_not_awaited()


def notify(b, *messages):
    for method, params in messages:
        b.mapper.notification(method, {"threadId": "s", **params})


ANSWER = (
    "item/completed",
    {"turnId": "t1", "item": {"id": "m", "type": "agentMessage", "text": "A"}},
)
DONE = ("turn/completed", {"turn": {"id": "t1", "status": "completed"}})


async def test_resume_reports_a_turn_that_completed_after_the_list_was_read():
    """#306: the server read the list while t1 ran, and its turn/completed was handled before
    open_session resumed. The list shows t1 running; the stream saw it complete. Report t1
    with its answer, or recovery ends it as "no longer active" and drops the answer."""
    b = backend()
    notify(b, ("turn/started", {"turn": {"id": "t1"}}))

    async def call(method, params):
        if method == "thread/loaded/list":
            return {"data": []}
        if method == "thread/resume":
            return {"thread": {"id": "s", "cwd": "/repo"}}
        notify(b, ANSWER, DONE)
        return {
            "data": [
                {"id": "t1", "status": "inProgress", "items": []},
                {"id": "t0", "status": "completed", "items": []},
            ]
        }

    b.rpc.call.side_effect = call
    info = await b.open_session("s", t.SessionSpec(cwd="/repo", name="topic"))
    assert info.active_turn_id is None
    assert info.last_turn == t.TurnSummary(
        turn_id="t1", status=t.TurnStatus.COMPLETED, final_text="A"
    )


async def test_resume_keeps_the_lists_last_turn_over_an_older_completion():
    """A completion the stream saw before the call is older than the list: t2 and t3 ran
    while zab was away, and t3 starts during the call. The list's t2 stands, not t1."""
    b = backend()
    notify(b, ("turn/started", {"turn": {"id": "t1"}}), ANSWER, DONE)

    async def call(method, params):
        if method == "thread/loaded/list":
            return {"data": []}
        if method == "thread/resume":
            return {"thread": {"id": "s", "cwd": "/repo"}}
        notify(b, ("turn/started", {"turn": {"id": "t3"}}))
        items = [{"type": "agentMessage", "text": "B"}]
        return {"data": [{"id": "t2", "status": "completed", "items": items}]}

    b.rpc.call.side_effect = call
    info = await b.open_session("s", t.SessionSpec(cwd="/repo", name="topic"))
    assert info.active_turn_id == "t3"
    assert (info.last_turn.turn_id, info.last_turn.final_text) == ("t2", "B")


async def test_lookup_reads_metadata_without_resuming_and_refuses_unknown_ids():
    b = backend()
    b.rpc.call.return_value = {"thread": {"id": "s", "cwd": "/native"}}
    info = await b.lookup_session("s")
    assert info.native_id == "s" and info.cwd == "/native"
    b.rpc.call.assert_awaited_once_with("thread/read", {"threadId": "s", "includeTurns": False})
    for message in ("thread not loaded: missing", "invalid thread id: malformed"):
        b.rpc.call.side_effect = RpcError({"code": -32600, "message": message})
        assert await b.lookup_session("missing") is None
    b.rpc.call.side_effect = RpcError({"code": -32603, "message": "backend failure"})
    with pytest.raises(RpcError):
        await b.lookup_session("s")
