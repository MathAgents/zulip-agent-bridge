"""Commands through the actor and fake Zulip, for either backend contract (§7.9)."""

import asyncio
import json
from dataclasses import asdict, replace
from unittest.mock import Mock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_session import actor as actor
from test_session import send

from zab.core import types as t
from zab.testing.fake_clock import FrozenClock

pytestmark = pytest.mark.integration


async def until(condition, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition()


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("field", ["model", "effort"])
async def test_invalid_setting_keeps_state_and_next_turn_usable(actor, kind, field):
    a, fake, backend = actor
    backend.kind = kind
    a.state(settings_json=json.dumps(asdict(t.SessionSettings(model="good", effort="high"))))
    before = a.row["settings_json"]
    backend.update_settings.side_effect = t.InvalidSetting(field, "banana", ["good", "high"])
    a.put((f"!{field} banana",))
    await a.inbox.join()
    assert a.row["state"] == "idle"
    assert a.row["settings_json"] == before
    assert a.store.rows("sessions", id="s")[0]["settings_json"] == before
    reply = list(fake.messages.values())[-1]["content"]
    assert f"Unknown {field}" in reply and "banana" in reply and "Valid:" in reply
    await send(a, fake, "still works")
    assert backend.start_turn.await_count == 1 and a.row["state"] == "running"


@pytest.mark.parametrize("explicit", [False, True])
async def test_commands_show_resolved_values_without_persisting_defaults(actor, explicit):
    a, fake, backend = actor
    resolved = t.SessionSettings(model="resolved-model", effort="high")
    backend.effective_settings = Mock(return_value=resolved)
    if explicit:
        a.state(settings_json=json.dumps(asdict(resolved)))
    before = a.row["settings_json"]
    for field, value in (("model", "resolved-model"), ("effort", "high")):
        await a.command("!" + field)
        reply = list(fake.messages.values())[-1]["content"]
        assert value in reply and ("(default)" in reply) is not explicit
    assert a.row["settings_json"] == before
    backend.update_settings.assert_not_awaited()


async def test_model_reset_note_and_tab_separated_command(actor):
    a, fake, backend = actor
    a.state(settings_json=json.dumps(asdict(t.SessionSettings(model="old", effort="high"))))
    backend.update_settings.side_effect = lambda sid, s: replace(s, effort=None)
    await a.command("!model\tnew")
    assert json.loads(a.row["settings_json"])["model"] == "new"
    assert json.loads(a.row["settings_json"])["effort"] is None
    assert "effort reset" in list(fake.messages.values())[-1]["content"]
    await a.command('!model ""')
    assert json.loads(a.row["settings_json"])["model"] is None


@pytest.mark.parametrize("supported,effort", [(True, "high"), (None, None), (False, None)])
async def test_turn_status_keeps_actual_settings_across_updates_and_reload(
    actor, supported, effort
):
    a, fake, backend = actor
    backend.effective_settings = Mock(return_value=t.SessionSettings(model="initial", effort="low"))
    await send(a, fake, "start")
    mid = a.turn["status_message_id"]
    await a.event(
        t.TurnStarted(
            session_id="native",
            turn_id="turn1",
            model="actual",
            effort=effort,
            effort_supported=supported,
        )
    )
    header = fake.messages[mid]["content"].splitlines()[0]
    assert "actual" in header
    assert ("effort" in header) is (supported is not False)
    if supported is not False:
        assert (effort or "default") in header
    backend.update_settings.side_effect = lambda sid, s: s
    backend.effective_settings.return_value = t.SessionSettings(model="next", effort="low")
    await a.command("!model next")
    # Reload persisted turn data, as actor reconstruction does after a restart.
    a.turn = a.store.rows("turns", id=a.turn["id"])[0]
    await a.status()
    assert fake.messages[mid]["content"].splitlines()[0] == header
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="done"
        )
    )
    header = fake.messages[mid]["content"].splitlines()[0]
    assert "actual" in header and "next" not in header
    assert ("effort" in header) is (supported is not False)
    await send(a, fake, "next turn")
    await a.event(t.TurnStarted(session_id="native", turn_id="turn2", model="next", effort="low"))
    await a.status()
    assert "next" in fake.messages[a.turn["status_message_id"]]["content"].splitlines()[0]


async def test_unknown_display_hook_and_terminal_turn_are_not_guessed(actor):
    a, fake, backend = actor
    backend.effective_settings = None
    await a.command("!effort")
    assert "default" in list(fake.messages.values())[-1]["content"]
    await a.event(t.TurnStarted(session_id="native", turn_id="terminal", model="terminal-model"))
    await a.status()
    header = fake.messages[a.turn["status_message_id"]]["content"].splitlines()[0]
    assert "terminal-model" in header and "effort default" in header


@pytest.mark.parametrize("field", ["model", "effort"])
async def test_new_options_validate_before_binding_or_turn(bridge, field):
    b, fake, backend = bridge
    backend.update_settings.side_effect = t.InvalidSetting(field, "banana", ["valid"])
    await user_message(b, fake, f"@**Test Bot** !new --{field} banana hello")
    assert not b.store.rows("sessions")
    backend.start_turn.assert_not_awaited()
    assert any(
        f"Unknown {field}" in m["content"] and "Valid:" in m["content"]
        for m in fake.messages.values()
    )


async def test_repeated_start_enriches_without_losing_pgid_or_restarting_status(actor):
    a, fake, _ = actor
    a.poster.client.budget.clock = clock = FrozenClock()
    await send(a, fake, "start")
    mid, started = a.turn["status_message_id"], a.turn["started_at"]
    await a.event(t.TurnStarted(session_id="native", turn_id="turn1", pgid=123, effort="high"))
    before = set(fake.messages)
    await a.event(
        t.TurnStarted(
            session_id="native",
            turn_id="turn1",
            model="resolved",
            effort="high",
            effort_supported=True,
        )
    )
    assert a.turn["pgid"] == 123 and a.turn["started_at"] == started
    assert a.turn["status_message_id"] == mid and set(fake.messages) == before
    # The model came within a minute of the effort's edit: its edit waits for the gap (#754)
    assert "resolved" not in fake.messages[mid]["content"] and mid in a.poster.held
    clock.advance(60)
    await until(lambda: "resolved" in fake.messages[mid]["content"].splitlines()[0])


@pytest.mark.parametrize("explicit", [False, True])
async def test_unsupported_config_effort_falls_back_but_explicit_is_refused(
    bridge, caplog, explicit
):
    from unittest.mock import AsyncMock

    from zab.backends.codex import CodexBackend

    b, fake, backend = bridge
    b.config.backends["codex"].defaults = {"effort": "high"}
    native = CodexBackend("codex", b.config.backends["codex"])
    native.rpc = AsyncMock(up=True)
    catalog = dict(
        id="main",
        model="main",
        isDefault=True,
        defaultReasoningEffort="medium",
        supportedReasoningEfforts=[dict(reasoningEffort="medium")],
    )

    async def call(method, params):
        if method == "config/read":
            return {"config": {}}
        if method == "model/list":
            return dict(data=[catalog])
        if method == "thread/start":
            return dict(
                thread=dict(id="native", cwd=b.config.bridge.default_project),
                model="main",
                reasoningEffort=None,
            )
        if method == "turn/start":
            assert params["effort"] == "medium"
            return dict(turn=dict(id="turn1"))
        return {}

    native.rpc.call.side_effect = call
    backend.create_session.side_effect = native.create_session
    backend.update_settings.side_effect = native.update_settings
    backend.start_turn.side_effect = native.start_turn
    await user_message(
        b, fake, "@**Test Bot** " + ("!new --effort high " if explicit else "") + "hello"
    )
    if explicit:
        assert not b.store.rows("sessions")
        backend.start_turn.assert_not_awaited()
        assert any("Unknown effort" in m["content"] for m in fake.messages.values())
        assert "Configured default effort unsupported" not in caplog.text
    else:
        row = b.store.rows("sessions")[0]
        assert json.loads(row["settings_json"])["effort"] is None
        backend.start_turn.assert_awaited_once()
        assert "Configured default effort unsupported" in caplog.text
        assert b.config.backends["codex"].defaults["effort"] == "high"
        assert [c.args[0] for c in native.rpc.call.call_args_list].count("thread/start") == 1


async def test_fallback_after_update_reuses_the_native_session(bridge):
    b, fake, backend = bridge
    b.config.backends["codex"].defaults = {"effort": "high"}
    backend.update_settings.side_effect = [
        t.InvalidSetting("effort", "high", ["medium"]),
        t.SessionSettings(),
    ]
    await user_message(b, fake, "@**Test Bot** hello")
    backend.create_session.assert_awaited_once()
    assert backend.update_settings.await_count == 2
    assert backend.update_settings.call_args.args[1].effort is None
    assert json.loads(b.store.rows("sessions")[0]["settings_json"])["effort"] is None
