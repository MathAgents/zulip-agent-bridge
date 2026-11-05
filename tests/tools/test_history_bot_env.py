"""Issue #248 end to end, offline: `zab history`, run in the environment each backend builds for
a session, reads as that session's bot, with no `--bot` and no zuliprc in the agent's hands.

The spec's bot comes from the core (tests/core/test_session_bot.py); here it goes through
Claude's `turn_env` and Codex's `shell_environment_policy` overrides into a real `zab history`
process. Its zuliprc files don't exist, so it stops before any network call, naming the bot it
picked."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

import pytest

from zab.backends.claude import turn_env
from zab.backends.codex import identity_config
from zab.core import types as t

CONFIG = """
[backends.claude]
kind = "claude"
accounts = { main = "account" }

[backends.codex]
kind = "codex"

[[bots]]
name = "claude-alice"
zuliprc = "missing-claude.zuliprc"
backend = "claude"

[[bots]]
name = "codex-alice"
zuliprc = "missing-codex.zuliprc"
backend = "codex"
"""


def claude_env(spec: t.SessionSpec, base: dict[str, str]) -> dict[str, str]:
    return turn_env("/claude-account", base, git_identity=spec.git_identity, bot=spec.bot)


def codex_env(spec: t.SessionSpec, base: dict[str, str]) -> dict[str, str]:
    """What a Codex shell gets: the app-server's environment, minus the exclusions, plus `set`
    (applied after exclusions; DECISIONS, 2026-10-11)."""
    policy = identity_config(spec, {"exclude": None, "set": None})
    env = {k: v for k, v in base.items() if k not in policy["shell_environment_policy.exclude"]}
    return env | policy["shell_environment_policy.set"]


BACKENDS: dict[str, Callable[[t.SessionSpec, dict[str, str]], dict[str, str]]] = {
    "claude": claude_env,
    "codex": codex_env,
}


def zab_history(tmp_path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "zab", "history", "--list-topics", "--config", str(config)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_zab_history_reads_as_the_bot_whose_turn_it_is(tmp_path, kind):
    bridge = dict(os.environ) | {"ZAB_BOT": "the-bridge-itself"}  # never reaches a turn
    bot = f"{kind}-alice"
    spec = t.SessionSpec(cwd=str(tmp_path), name="Line 9", bot=bot)
    result = zab_history(tmp_path, BACKENDS[kind](spec, bridge))
    assert result.returncode == 2, result.stderr
    assert result.stderr.strip() == f"zab history: cannot read the zuliprc of bot {bot!r}"


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_without_a_bot_zab_history_asks_instead_of_using_the_bridges(tmp_path, kind):
    bridge = dict(os.environ) | {"ZAB_BOT": "claude-alice"}
    spec = t.SessionSpec(cwd=str(tmp_path), name="Line 9")
    result = zab_history(tmp_path, BACKENDS[kind](spec, bridge))
    assert result.returncode == 2
    assert "Which bot? Set ZAB_BOT or pass --bot" in result.stderr
    assert "claude-alice (claude), codex-alice (codex)" in result.stderr
