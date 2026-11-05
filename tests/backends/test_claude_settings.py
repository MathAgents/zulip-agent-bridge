"""`!model` and `!effort` on Claude (SPEC §7.9, B.2 amendment; PM on issue #36, Sun 15:30):
validation, the model check, the resolved values for display, and each turn's own settings."""

from __future__ import annotations

import json
import sys
from dataclasses import replace

import pytest

from backends.claude_backend_helpers import FAKE, FIXTURES, drain
from zab.backends import claude as cb
from zab.backends import claude_stream as cs
from zab.core.types import (
    InvalidSetting,
    SessionSettings,
    SessionSpec,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
    UserInput,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang"),
]

# The core's stored settings, sent back whole on every command (core/session.py); with backend
# defaults, the core folded them in at creation (§7.2), so a test passes them along too.
STORED = SessionSettings(approval="bypassPermissions", account="test")


def calls(home) -> list[dict]:
    """The fake's call log (none yet: empty)."""
    log = home["log"]
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


async def session(home, defaults: SessionSettings | None = None):
    """A backend on the fake whose config defaults are `defaults` (haiku when not given)."""
    config = cb.ClaudeConfig(
        binary=str(FAKE),
        accounts={"test": str(home["account"])},
        default_account="test",
        defaults=defaults or SessionSettings(model="haiku", approval="bypassPermissions"),
    )
    backend = cb.ClaudeBackend("claude", config)
    await backend.start()
    await backend._events.get()  # BackendStatus
    info = await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))
    return backend, info.native_id


def checks(home) -> list[dict]:
    """The fake's model-check invocations."""
    return [c for c in calls(home) if "--no-session-persistence" in c["argv"]]


async def turn(backend, sid) -> list:
    await backend.start_turn(sid, UserInput(text="Reply with pong.", client_id="c"))
    return await drain(backend)


@pytest.mark.parametrize("effort", ["extra", "10", "banana", "very high", ""])
async def test_an_unknown_effort_is_refused_and_nothing_changes(home, effort):
    backend, sid = await session(home)
    if not effort:  # "" means the default (§5.2), not an error
        assert (await backend.update_settings(sid, replace(STORED, effort=""))).effort is None
        return
    with pytest.raises(InvalidSetting) as exc:
        await backend.update_settings(sid, replace(STORED, effort=effort))
    assert exc.value.field == "effort" and exc.value.value == effort
    assert exc.value.valid == ["low", "medium", "high", "xhigh", "max"]
    assert exc.value.message == ""  # the core's "Unknown effort … Valid: …" says it all
    assert calls(home) == []  # refused before any check or turn
    events = await turn(backend, sid)
    assert "--effort" not in calls(home)[-1]["argv"]  # nothing was stored
    assert events[-1].status is TurnStatus.COMPLETED


async def test_effort_levels_ignore_case(home):
    backend, sid = await session(home, defaults=SessionSettings(model="opus"))
    stored = replace(STORED, model="opus")
    assert (await backend.update_settings(sid, replace(stored, effort="XHigh"))).effort == "xhigh"


@pytest.mark.parametrize("model", ["banana", "gpt-5", "claude", "opus 4", "claude-"])
async def test_a_name_that_is_neither_alias_nor_full_name_is_refused_without_a_check(home, model):
    backend, sid = await session(home)
    with pytest.raises(InvalidSetting) as exc:
        await backend.update_settings(sid, replace(STORED, model=model))
    assert exc.value.field == "model" and exc.value.message == ""
    assert exc.value.valid[:2] == ["default", "opus"]
    assert exc.value.valid[-1] == cb.FULL_NAME_CHOICE
    assert checks(home) == []


@pytest.mark.parametrize(
    ("model", "resolved"),
    [
        ("opus", "claude-opus-5-5"),
        ("Opus", "claude-opus-5-5"),
        ("opus[1m]", "claude-opus-5-5[1m]"),
        ("best", "claude-fable-5-1"),
        ("default", "claude-opus-5-5"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
    ],
)
async def test_a_new_model_is_checked_once_and_reports_its_resolved_name(home, model, resolved):
    backend, sid = await session(home)
    assert backend.effective_settings(sid) is None  # nothing known before a turn or a check
    accepted = await backend.update_settings(sid, replace(STORED, model=model))
    assert accepted.model == model  # the override as given; display uses the resolved name
    [check] = checks(home)
    assert check["argv"][-2:] == ["--model", model] and ["--tools", ""] == check["argv"][-4:-2]
    assert check["cwd"] == cb.MODEL_CHECK_DIR != str(home["work"])  # one fixed empty directory
    assert check["env"]["CLAUDE_CONFIG_DIR"] == str(home["account"])  # the session's account
    assert "ANTHROPIC_API_KEY" not in check["env"]["names"]  # the turn environment
    assert backend.effective_settings(sid).model == resolved
    # the same model again, or an effort change, needs no new check
    await backend.update_settings(sid, replace(STORED, model=model))
    await backend.update_settings(sid, replace(STORED, model=model, effort="high"))
    assert len(checks(home)) == 1


async def test_a_missing_model_is_refused_after_its_check(home, monkeypatch):
    """The PM's case: `!model claude-x` that does not exist must not break every later turn."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_MISSING_MODELS", "claude-gone-1")
    backend, sid = await session(home)
    with pytest.raises(InvalidSetting) as exc:
        await backend.update_settings(sid, replace(STORED, model="claude-gone-1"))
    assert exc.value.field == "model" and exc.value.value == "claude-gone-1"
    assert exc.value.message == "Model claude-gone-1 isn't available on this Claude account."
    assert "`" not in exc.value.message  # plain text: the core quotes it
    events = await turn(backend, sid)
    assert calls(home)[-1]["argv"][calls(home)[-1]["argv"].index("--model") + 1] == "haiku"
    assert events[-1].status is TurnStatus.COMPLETED


async def test_a_model_without_effort_clears_the_inherited_effort_and_refuses_a_new_one(home):
    backend, sid = await session(home, defaults=SessionSettings(model="opus"))
    accepted = await backend.update_settings(sid, replace(STORED, model="opus", effort="max"))
    assert accepted.effort == "max"
    accepted = await backend.update_settings(sid, replace(accepted, model="haiku"))
    assert accepted.model == "haiku" and accepted.effort is None  # cleared; the core notes it
    shown = backend.effective_settings(sid)
    assert shown.model == "claude-haiku-4-5-20251001" and shown.effort is None
    with pytest.raises(InvalidSetting) as exc:
        await backend.update_settings(sid, replace(accepted, effort="high"))
    assert exc.value.field == "effort" and exc.value.message.startswith(
        "claude-haiku-4-5-20251001 takes no effort level."
    )
    accepted = await backend.update_settings(sid, replace(accepted, model="sonnet"))
    assert (await backend.update_settings(sid, replace(accepted, effort="high"))).effort == "high"


async def test_a_check_that_cannot_tell_accepts_the_model(home, monkeypatch):
    monkeypatch.setattr(cb, "MODEL_CHECK_TIMEOUT_S", 0.5)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")  # no result: like a stalled network
    backend, sid = await session(home)
    accepted = await backend.update_settings(sid, replace(STORED, model="claude-new-9"))
    assert accepted.model == "claude-new-9" and backend.effective_settings(sid) is None
    assert len(checks(home)) == 1


async def test_each_turn_reports_the_model_and_effort_it_ran_with(home):
    backend, sid = await session(home)
    accepted = await backend.update_settings(sid, replace(STORED, model="opus"))
    await backend.update_settings(sid, replace(accepted, effort="xhigh"))
    events = await turn(backend, sid)
    first, second = (e for e in events if isinstance(e, TurnStarted))
    assert first.pgid and first.model is None and first.effort == "xhigh"  # at spawn
    assert second.pgid == first.pgid and second.turn_id == first.turn_id
    assert second.model == "claude-opus-5-5" and second.effort == "xhigh"  # after system/init
    assert second.effort_supported is True
    argv = calls(home)[-1]["argv"]
    assert argv[argv.index("--effort") + 1] == "xhigh"


async def test_a_turn_without_overrides_shows_the_default_model_after_it_ran(home):
    backend, sid = await session(home, defaults=SessionSettings())
    assert backend.effective_settings(sid) is None
    events = await turn(backend, sid)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert started[-1].model == "claude-haiku-4-5-20251001" and started[-1].effort is None
    assert backend.effective_settings(sid).model == "claude-haiku-4-5-20251001"
    assert checks(home) == []  # a turn never needs a check


async def test_a_turn_that_fails_on_its_model_says_what_to_do(home, monkeypatch):
    """A model no check has seen (a config default, `!new --model`) fails on the turn instead."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_MISSING_MODELS", "claude-gone-1")
    backend, sid = await session(home, defaults=SessionSettings(model="claude-gone-1"))
    events = await turn(backend, sid)
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.error.message.startswith("Model `claude-gone-1` isn't available")
    assert '`!model ""`' in done.error.message
    assert backend.effective_settings(sid) is None  # the failed name is never shown as resolved


def test_the_recorded_missing_model_result_is_classified():
    records = [r for r in cs_records("error") if r.get("type") == "result"]
    assert cs.is_model_unavailable(records[0])
    assert not any(cs.is_model_unavailable(r) for r in cs_records("simple"))
    assert not cs.is_model_unavailable({"is_error": True, "api_error_status": 429, "result": "x"})


def cs_records(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text().splitlines()
    return [r["data"] for r in map(json.loads, lines) if r.get("kind") == "stdout"]


def test_names():
    for good in ("opus", "OPUS", "sonnet[1m]", "claude-opus-5-5", "claude-haiku-4-5-20251001"):
        cb.check_model_name(good)
    assert cb.check_effort("MAX") == "max"


async def test_clearing_a_setting_omits_its_flag_even_with_backend_defaults(home):
    """Codex round 2 on #113: `None` is the agent's own default (PM review on PR #49). The
    backend's defaults reach the core's stored settings at creation (§7.2), never again here."""
    backend, sid = await session(home, defaults=SessionSettings(model="opus", effort="max"))
    cleared = await backend.update_settings(sid, replace(STORED, model=None, effort=None))
    assert cleared.model is None and cleared.effort is None
    await turn(backend, sid)
    argv = calls(home)[-1]["argv"]
    assert "--model" not in argv and "--effort" not in argv


async def test_a_setting_changed_while_the_turn_starts_applies_to_the_next_turn(home, monkeypatch):
    """Codex round 3 on #113: the argv and the reported effort come from one snapshot."""
    import asyncio
    import threading

    backend, sid = await session(home, defaults=SessionSettings(model="opus", effort="low"))
    gate = threading.Event()
    real = cb.find_transcript

    def slow(*args):
        gate.wait(10)
        return real(*args)

    monkeypatch.setattr(cb, "find_transcript", slow)
    await backend.start_turn(sid, UserInput(text="Reply with pong.", client_id="c"))
    await asyncio.sleep(0.1)  # the turn is in its transcript lookup
    await backend.update_settings(sid, replace(STORED, model="opus", effort="max"))
    gate.set()
    events = await drain(backend)
    started = [e for e in events if isinstance(e, TurnStarted)]
    argv = calls(home)[-1]["argv"]
    assert argv[argv.index("--effort") + 1] == "low"
    assert {e.effort for e in started} == {"low"}
    await turn(backend, sid)
    argv = calls(home)[-1]["argv"]
    assert argv[argv.index("--effort") + 1] == "max"  # the next turn
