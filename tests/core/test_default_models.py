"""A configured default model the agent can't use never blocks `!new` (#1074, SPEC §7.9).

The workshops' profile names a default model for each agent (SPEC §5.3). A new session whose
agent can't use it starts on the agent's own default, with a warning, as an unsupported
configured effort already did: Codex, when the model is missing from the connection's catalog;
Claude, when the account can't use it, which a check of one short turn, made once per account
and model in the process, says. An explicit `!new --model` is still refused. A session keeps its
model across restarts, and one created before the default was configured keeps the agent's own.

Each real backend on its fake, through `BotBridge` or the real `Runtime` (`make_rig` and
`restarts`, from the `!fork` tests): Codex on `FakeAppServer`, whose catalog lists `gpt-fake`,
its default, and `gpt-other`, each with the efforts low, medium and high; Claude on
`fake_claude.py`, which answers a model in `ZAB_FAKE_CLAUDE_MISSING_MODELS` as the CLI answers
a model the account can't use, and one in `ZAB_FAKE_CLAUDE_CREDIT_MODELS` as it answers a model
that needs usage credits (#1077)."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from test_fork import OWNER, calls, parent
from test_fork import make_rig as make_rig  # the fixtures
from test_fork import restarts as restarts

pytestmark = pytest.mark.integration
ASTRA, FABLE = "gpt-6.2-astra", "claude-fable-5-2"
FELL_BACK = "Configured default model {} unavailable; using the agent's default"
EFFORT_FELL_BACK = "Configured default effort unsupported; using the model default"


def settings(row) -> dict:
    return json.loads(row["settings_json"])


def checks(tmp) -> list[list[str]]:
    """The fake Claude's model checks, by their argv."""
    return [c["argv"] for c in calls(tmp) if "--no-session-persistence" in c["argv"]]


def turns(tmp) -> list[list[str]]:
    """The fake Claude's turns, by their argv."""
    return [
        c["argv"]
        for c in calls(tmp)
        if "-p" in c["argv"] and "--no-session-persistence" not in c["argv"]
    ]


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def warnings(rig, topic: str) -> list[str]:
    """The bridge's ⚠️ notes in `topic` (SPEC §4.9): a refused creation's."""
    return [p for p in rig.posts(topic) if p.startswith("⚠️")]


# ------------------------------------------------------------------------------------- Codex


async def test_a_codex_default_model_missing_from_the_catalog_falls_back(make_rig, caplog):
    """A Codex whose catalog lacks the model (0.157.1 and `gpt-6.2-astra`): the session starts
    on Codex's own default at the configured effort; `!new --model` naming it is refused."""
    caplog.set_level(logging.WARNING, "zab.creation")
    rig = await make_rig("codex")
    rig.bridge.config.backends["codex"].defaults = {"model": ASTRA, "effort": "high"}
    row = await parent(rig)
    assert (settings(row)["model"], settings(row)["effort"]) == (None, "high")
    [start] = rig.server.requests("thread/start")
    assert "model" not in start
    turn = rig.server.requests("turn/start")[-1]
    assert (turn["model"], turn["effort"]) == ("gpt-fake", "high")  # Codex's own default
    assert FELL_BACK.format(ASTRA) in caplog.text and EFFORT_FELL_BACK not in caplog.text

    await rig.say(f"@**Test Bot** !new --model {ASTRA} hello", "explicit")
    [refusal] = await rig.until(lambda: warnings(rig, "explicit"), "the refusal")
    assert f"Unknown model ` {ASTRA} `" in refusal
    assert rig.row("explicit") is None and len(rig.server.requests("thread/start")) == 1


async def test_a_codex_default_model_in_the_catalog_is_used(make_rig, caplog):
    caplog.set_level(logging.WARNING, "zab.creation")
    rig = await make_rig("codex")
    rig.bridge.config.backends["codex"].defaults = {"model": "gpt-other", "effort": "high"}
    row = await parent(rig)
    assert (settings(row)["model"], settings(row)["effort"]) == ("gpt-other", "high")
    assert rig.server.requests("thread/start")[0]["model"] == "gpt-other"
    assert rig.server.requests("turn/start")[-1]["model"] == "gpt-other"
    assert "Configured default" not in caplog.text


@pytest.mark.parametrize("explicit", [False, True])
async def test_an_effort_the_fallback_model_refuses_falls_back_too(make_rig, caplog, explicit):
    """The model falls back, and then the configured effort, which Codex's own default doesn't
    take: three tries, one thread. An explicit `--effort` is refused instead."""
    caplog.set_level(logging.WARNING, "zab.creation")
    rig = await make_rig("codex")
    rig.bridge.config.backends["codex"].defaults = {"model": ASTRA, "effort": "max"}
    command = "!new --effort max " if explicit else ""
    await rig.say(f"@**Test Bot** {command}remember orchid")
    if explicit:
        [refusal] = await rig.until(lambda: warnings(rig, "topic"), "the refusal")
        assert "Unknown effort ` max `" in refusal
        assert rig.row() is None and not rig.server.requests("thread/start")
        assert EFFORT_FELL_BACK not in caplog.text
    else:
        await rig.turns(1)
        assert (settings(rig.row())["model"], settings(rig.row())["effort"]) == (None, None)
        assert len(rig.server.requests("thread/start")) == 1
        assert EFFORT_FELL_BACK in caplog.text
    assert FELL_BACK.format(ASTRA) in caplog.text


# ------------------------------------------------------------------------------------ Claude


@pytest.mark.parametrize("answer", ["missing", "credits", "available"])
async def test_a_claude_default_model_is_checked_once_and_kept(
    restarts, monkeypatch, caplog, answer
):
    """Its first new session tries the model with one check; the next session takes the answer
    kept. Unavailable, both sessions run on Claude's own default (no `--model`), the effort
    still configured, and stay so after a restart; `!new --model` naming it is refused. The
    account can't use a model the CLI calls missing, or one that needs usage credits (#1077),
    whose refusal gives the CLI's sentence."""
    caplog.set_level(logging.WARNING, "zab.creation")
    available = answer == "available"
    if answer == "missing":
        monkeypatch.setenv("ZAB_FAKE_CLAUDE_MISSING_MODELS", FABLE)
    elif answer == "credits":
        monkeypatch.setenv("ZAB_FAKE_CLAUDE_CREDIT_MODELS", FABLE)
    rig = await restarts("claude")
    rig.config.backends["claude"].defaults = {"model": FABLE, "effort": "high"}
    await rig.start()
    for topic in ("one", "two"):
        rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", topic)
        await rig.turns(1, topic)
    [check] = checks(rig.tmp)
    assert flag(check, "--model") == FABLE
    model = FABLE if available else None
    for topic, argv in zip(("one", "two"), turns(rig.tmp), strict=True):
        row = rig.store().rows("sessions", topic=topic)[0]
        assert (settings(row)["model"], settings(row)["effort"]) == (model, "high")
        assert (flag(argv, "--model"), flag(argv, "--effort")) == (model, "high")
    assert (FELL_BACK.format(FABLE) in caplog.text) is not available

    await rig.stop()
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** what did I ask?", "one")
    await rig.turns(2, "one")
    assert flag(turns(rig.tmp)[-1], "--model") == model
    if available:
        return
    rig.fake.post_as(OWNER, f"@**Test Bot** !new --model {FABLE} hello", "explicit")
    async with asyncio.timeout(30):
        while not warnings(rig, "explicit"):  # noqa: ASYNC110 - the bridge's own task
            await asyncio.sleep(0.05)
    [refusal] = warnings(rig, "explicit")
    assert f"Model {FABLE} isn't available on this Claude account." in refusal
    assert ("Claude Code said: Fable requires usage credits." in refusal) is (answer == "credits")
    assert not rig.store().rows("sessions", topic="explicit")
    assert len(checks(rig.tmp)) == 2  # a new process checks again


async def test_a_claude_session_from_before_the_default_keeps_its_model(restarts):
    """SPEC §7.2 step 2: the defaults are a new session's. One created before the default model
    was configured keeps Claude's own default after the restart that brings it in."""
    rig = await restarts("claude")
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", "old")
    await rig.turns(1, "old")
    await rig.stop()
    rig.config.backends["claude"].defaults = {"model": FABLE, "effort": "high"}
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** what did I ask?", "old")
    await rig.turns(2, "old")
    rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", "new")
    await rig.turns(1, "new")
    *_, old, new = turns(rig.tmp)
    assert flag(old, "--model") is None and flag(new, "--model") == FABLE


# ------------------------------------------ an effort the created session's model doesn't take


async def test_new_with_a_model_that_takes_no_effort_refuses_an_explicit_effort(make_rig):
    """Codex round 1 on #1075 (SPEC §7.9): the check at creation finds that haiku takes no
    effort; an explicit `!new --effort` is refused, never cleared, and no session starts."""
    rig = await make_rig("claude")
    await rig.say("@**Test Bot** !new --model haiku --effort high hello")
    [refusal] = await rig.until(lambda: warnings(rig, "topic"), "the refusal")
    assert "Unknown effort ` high `" in refusal and "takes no effort level" in refusal
    assert rig.row() is None and not turns(rig.tmp)
    assert [flag(c, "--model") for c in checks(rig.tmp)] == ["haiku"]


async def test_a_configured_effort_the_new_model_doesnt_take_falls_back(make_rig, caplog):
    """The configured effort is cleared with the warning, from the one kept check, and the
    session starts at the model's default. Later, `!model haiku` on a live session still clears
    the inherited effort, and the bot notes the reset."""
    caplog.set_level(logging.WARNING, "zab.creation")
    rig = await make_rig("claude")
    rig.bridge.config.backends["claude"].defaults = {"effort": "high"}
    row = await parent(rig, "@**Test Bot** !new --model haiku remember orchid")
    assert (settings(row)["model"], settings(row)["effort"]) == ("haiku", None)
    assert EFFORT_FELL_BACK in caplog.text and len(checks(rig.tmp)) == 1
    argv = turns(rig.tmp)[-1]
    assert (flag(argv, "--model"), flag(argv, "--effort")) == ("haiku", None)

    await rig.say("@**Test Bot** remember orchid", "second")
    await rig.turns(1, "second")
    assert (settings(rig.row("second"))["model"], settings(rig.row("second"))["effort"]) == (
        None,
        "high",
    )
    await rig.say("!model haiku", "second")
    await rig.until(lambda: any("effort reset" in p for p in rig.posts("second")), "the reset")
    assert (settings(rig.row("second"))["model"], settings(rig.row("second"))["effort"]) == (
        "haiku",
        None,
    )
