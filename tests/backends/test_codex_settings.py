import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from backends.test_codex_backend import backend
from zab.backends.codex_rpc import RpcError
from zab.core import types as t

pytestmark = pytest.mark.contract


def model(name, efforts, default=False, hidden=False):
    return dict(
        id=name + "-id",
        model=name,
        isDefault=default,
        hidden=hidden,
        defaultReasoningEffort=efforts[0],
        supportedReasoningEfforts=[dict(reasoningEffort=e, description=e) for e in efforts],
    )


MODELS = [model("main", ["medium", "high"], True), model("hidden", ["low"], hidden=True)]


@pytest.mark.parametrize(
    "field,value,valid",
    [
        ("model", "banana", ["hidden", "main"]),
        ("effort", "banana", ["medium", "high"]),
        ("effort", "10", ["medium", "high"]),
    ],
)
async def test_invalid_update_is_atomic(field, value, valid):
    b = backend()
    old = t.SessionSettings(model="main", effort="high")
    b.settings["s"] = old
    b.specs["s"] = t.SessionSpec(cwd="/repo", name="topic", settings=old)
    b.rpc.call.return_value = dict(data=MODELS)
    candidate = replace(old, **{field: value})
    with pytest.raises(t.InvalidSetting) as caught:
        await b.update_settings("s", candidate)
    assert caught.value.field == field and caught.value.valid == valid
    assert b.settings["s"] is old and b.specs["s"].settings is old
    assert getattr(candidate, field) == value


async def test_all_pages_hidden_ids_cache_and_model_change_effort_reset():
    b = backend()
    b.settings["s"] = t.SessionSettings(model="main", effort="high")
    b.rpc.call.side_effect = [dict(data=MODELS[:1], nextCursor="next"), dict(data=MODELS[1:])]
    result = await b.update_settings("s", t.SessionSettings(model="hidden-id", effort="high"))
    assert result.effort is None
    assert b.effective_settings("s").model == "hidden"
    assert b.effective_settings("s").effort == "low"
    assert [c.args[1] for c in b.rpc.call.call_args_list] == [
        dict(includeHidden=True, cursor=None),
        dict(includeHidden=True, cursor="next"),
    ]
    await b.update_settings("s", t.SessionSettings(model="hidden", effort="low"))
    assert b.rpc.call.await_count == 2
    b.rpc = AsyncMock(up=True)
    b.rpc.call.return_value = dict(data=[model("replacement", ["max"], True)])
    with pytest.raises(t.InvalidSetting):
        await b.update_settings("s", t.SessionSettings(model="main"))
    b.rpc.call.assert_awaited_once()


@pytest.mark.parametrize(
    "response", [RpcError(dict(code=-1, message="PRIVATE")), dict(data=[]), dict(data=[{}])]
)
async def test_failed_catalog_accepts_with_metadata_only_warning(response, caplog):
    b = backend()
    if isinstance(response, Exception):
        b.rpc.call.side_effect = response
    else:
        b.rpc.call.return_value = response
    result = await b.update_settings("s", t.SessionSettings(model="banana"))
    assert result.model == "banana"
    assert "accepting settings without validation" in caplog.text
    assert "PRIVATE" not in caplog.text


async def test_slow_catalog_does_not_block_command(monkeypatch):
    b = backend()
    timeout = asyncio.timeout
    monkeypatch.setattr("zab.backends.codex_settings.asyncio.timeout", lambda _: timeout(0.01))

    async def slow(*args):
        await asyncio.Event().wait()

    b.rpc.call.side_effect = slow
    assert (await b.update_settings("s", t.SessionSettings(effort="custom"))).effort == "custom"


async def test_default_display_is_separate_and_resets_are_sent_to_native_turn():
    b = backend()
    b.rpc.call.side_effect = [
        dict(thread=dict(id="s", cwd="/repo"), model="main", reasoningEffort="medium"),
        {},
    ]
    info = await b.create_session(t.SessionSpec(cwd="/repo", name="topic"))
    assert info.settings.model is None and info.settings.effort is None
    assert b.effective_settings("s").model == "main"
    assert b.effective_settings("s").effort == "medium"
    b.rpc.call.side_effect = None
    b.rpc.call.return_value = dict(data=MODELS)
    await b.update_settings("s", t.SessionSettings(model="hidden", effort="low"))
    await b.update_settings("s", t.SessionSettings())
    b.rpc.call.return_value = dict(turn=dict(id="t"))
    await b.start_turn("s", t.UserInput(text="hello", client_id="c"))
    params = b.rpc.call.call_args.args[1]
    assert params["model"] == "main" and params["effort"] == "medium"
    assert b.settings["s"].model is None and b.settings["s"].effort is None


async def test_native_notifications_supply_actual_turn_settings(monkeypatch):
    from zab.backends import codex

    b = backend()
    rpc = b.rpc
    rpc.identity = {}
    rpc.incoming = asyncio.Queue()
    monkeypatch.setattr(codex, "CodexRpc", lambda: rpc)
    b.settings["s"] = t.SessionSettings()
    try:
        await b.start()
        await asyncio.wait_for(b.ready.wait(), 1)
        await b.queue.get()  # BackendStatus(up=True)
        rpc.incoming.put_nowait(
            dict(
                method="thread/settings/updated",
                params=dict(
                    threadId="s", threadSettings=dict(model="terminal-model", effort="high")
                ),
            )
        )
        rpc.incoming.put_nowait(
            dict(method="turn/started", params=dict(threadId="s", turn=dict(id="t")))
        )
        event = await asyncio.wait_for(b.queue.get(), 1)
        assert isinstance(event, t.TurnStarted)
        assert event.model == "terminal-model" and event.effort == "high"
        assert b.effective_settings("s").model == "terminal-model"
        rpc.call.return_value = dict(data=MODELS)
        await b.update_settings("s", t.SessionSettings(model="hidden"))
        assert b.effective_settings("s").model == "hidden"  # Next-turn choice, not active settings.
        assert event.model == "terminal-model"  # The emitted turn snapshot is immutable.
    finally:
        await b.close()


async def test_invalid_new_options_never_create_a_native_thread():
    b = backend()
    b.rpc.call.return_value = dict(data=MODELS)
    with pytest.raises(t.InvalidSetting):
        await b.create_session(
            t.SessionSpec(cwd="/repo", name="topic", settings=t.SessionSettings(model="banana"))
        )
    assert [call.args[0] for call in b.rpc.call.call_args_list] == ["model/list"]


@pytest.mark.parametrize("explicit", [False, True])
async def test_effort_update_preserves_terminal_selected_model(explicit):
    b = backend()
    b.settings["s"] = t.SessionSettings(model="main" if explicit else None)
    b.defaults["s"] = t.SessionSettings(model="main", effort="medium")
    b.observed["s"] = t.SessionSettings(model="hidden", effort="low")
    b.rpc.call.return_value = dict(data=MODELS)
    accepted = await b.update_settings("s", replace(b.settings["s"], effort="low"))
    assert accepted.model == ("hidden" if explicit else None)
    assert b.effective_settings("s").model == "hidden"
    # A second effort command also keeps the pending model.
    await b.update_settings("s", replace(accepted, effort=None))
    assert b.effective_settings("s").model == "hidden"
    assert b.effective_settings("s").effort == "low"
    b.rpc.call.return_value = dict(turn=dict(id="t"))
    await b.start_turn("s", t.UserInput(text="hi", client_id="c"))
    assert b.rpc.call.call_args.args[1]["model"] == "hidden"
    assert b.rpc.call.call_args.args[1]["effort"] == "low"


async def test_new_model_catalog_id_is_sent_as_native_model_name():
    b = backend()
    b.rpc.call.side_effect = [
        dict(data=MODELS),
        dict(thread=dict(id="s", cwd="/repo"), model="hidden", reasoningEffort="low"),
        {},
    ]
    info = await b.create_session(
        t.SessionSpec(cwd="/repo", name="topic", settings=t.SessionSettings(model="hidden-id"))
    )
    assert b.rpc.call.call_args_list[1].args[1]["model"] == "hidden"
    assert info.settings.model == "hidden-id"


async def test_null_native_effort_stays_unknown_without_runtime_evidence(monkeypatch):
    from zab.backends import codex

    b = backend()
    rpc = b.rpc
    rpc.identity = {}
    rpc.incoming = asyncio.Queue()
    monkeypatch.setattr(codex, "CodexRpc", lambda: rpc)
    await b.start()
    try:
        await asyncio.wait_for(b.ready.wait(), 1)
        await b.queue.get()
        rpc.call.side_effect = [
            dict(thread=dict(id="native-id", cwd="/repo"), model="main", reasoningEffort=None),
            {},
        ]
        info = await b.create_session(t.SessionSpec(cwd="/repo", name="topic"))
        b.catalog.models = MODELS
        assert b.effective_settings(info.native_id).effort == "medium"
        rpc.incoming.put_nowait(
            dict(method="turn/started", params=dict(threadId=info.native_id, turn=dict(id="t")))
        )
        event = await asyncio.wait_for(b.queue.get(), 1)
        assert isinstance(event, t.TurnStarted) and event.session_id == info.native_id
        assert event.model == "main" and event.effort is None
    finally:
        await b.close()


@pytest.mark.parametrize("field,value", [("model", "opus"), ("effort", "max")])
async def test_an_unverifiable_value_is_refused_when_it_must_be_verified(field, value):
    """Review of #730, R9: with Codex's model list unavailable, a value it can't check is
    accepted (fail open), except inside `verified_only` (an unmentioned `!model` or `!effort`,
    which may be Claude's value): refused, and nothing changes."""
    from zab.core.reason import verified_only

    b = backend()
    old = t.SessionSettings(model="main", effort="high")
    b.settings["s"] = old
    b.rpc.call.side_effect = RpcError(dict(code=-1, message="unavailable"))
    with verified_only(), pytest.raises(t.InvalidSetting) as caught:
        await b.update_settings("s", replace(old, **{field: value}))
    assert (caught.value.field, caught.value.value) == (field, value)
    assert b.settings["s"] is old
    accepted = await b.update_settings("s", replace(old, **{field: value}))
    assert getattr(accepted, field) == value
