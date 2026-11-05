import os
import subprocess
import sys
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from zab.backends.codex import BOT_KEY, GIT_KEYS, CodexBackend, identity_config
from zab.config import BackendConfig
from zab.core import types as t

pytestmark = pytest.mark.contract


def spec(identity=True):
    return t.SessionSpec(
        cwd="/repo",
        name="test",
        git_identity=t.GitIdentity("Codex · test", "test-bot@example.zulipchat.com")
        if identity
        else None,
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_fake_tool_commit_author_and_committer(tmp_path, enabled):
    identity = identity_config(spec(enabled))
    env = dict(os.environ)
    for key in identity["shell_environment_policy.exclude"]:
        env.pop(key, None)
    env.update(identity["shell_environment_policy.set"])

    def git(*args):
        return subprocess.check_output(  # noqa: S603 - fixed test argv
            ["git", "-C", str(tmp_path), *args],  # noqa: S607
            env=env,
            text=True,
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Participant")
    git("config", "user.email", "participant@example.test")
    git("commit", "--allow-empty", "-qm", "fake tool commit")
    assert git("log", "-1", "--format=%an|%cn") == (
        "Codex · test|Codex · test" if enabled else "Participant|Participant"
    )
    assert git("log", "-1", "--format=%ae|%ce") == (
        "test-bot@example.zulipchat.com|test-bot@example.zulipchat.com"
        if enabled
        else "participant@example.test|participant@example.test"
    )
    if not enabled:
        assert all(k not in env for k in GIT_KEYS)


async def test_start_and_cold_resume_send_verified_config():
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    b.rpc.call.side_effect = [{"config": {}}, {"thread": {"id": "s", "cwd": "/repo"}}, {}]
    await b.create_session(spec())
    assert b.rpc.call.call_args_list[1].args[1]["config"] == identity_config(spec())
    b.specs.clear()
    b.rpc.call.reset_mock()
    b.rpc.call.side_effect = [
        {"data": ["other"], "nextCursor": "next"},
        {"data": []},
        {"config": {}},
        {"thread": {"id": "s", "cwd": "/repo"}},
        {"data": []},
    ]
    await b.open_session("s", spec())
    assert b.rpc.call.call_args_list[3].args[1]["config"] == identity_config(spec())


@pytest.mark.parametrize("enabled", [True, False])
async def test_loaded_unknown_identity_is_refused_without_interrupting(enabled):
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    b.rpc.call.return_value = {"data": ["s"]}
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await b.open_session("s", spec(enabled))
    assert "s" not in b.specs
    assert [c.args[0] for c in b.rpc.call.call_args_list] == ["thread/loaded/list"]


@pytest.mark.parametrize("enabled", [True, False])
async def test_applied_identity_survives_backend_restart_and_settings_change(enabled):
    import json
    from dataclasses import asdict, replace

    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    b.rpc.call.side_effect = [{"config": {}}, {"thread": {"id": "s", "cwd": "/repo"}}, {}]
    info = await b.create_session(spec(enabled))
    b.catalog.load = AsyncMock(return_value=None)
    # Creation's configured defaults arrive without backend metadata.
    accepted = await b.update_settings("s", t.SessionSettings(effort="medium"))
    assert accepted.extra["applied_git_identity"] == info.settings.extra["applied_git_identity"]
    persisted = json.dumps(asdict(accepted))
    fresh = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    fresh.rpc = AsyncMock()
    fresh.rpc.up = True
    fresh.rpc.call.side_effect = [
        {"config": {}},
        {"thread": {"id": "s", "cwd": "/repo"}},
        {"data": []},
    ]
    reopened = await fresh.open_session(
        "s", replace(spec(enabled), settings=t.SessionSettings(**json.loads(persisted)))
    )
    assert reopened.settings.extra == accepted.extra
    assert [c.args[0] for c in fresh.rpc.call.call_args_list] == [
        "config/read",
        "thread/resume",
        "thread/turns/list",
    ]


async def test_changed_persisted_identity_still_refuses_loaded_thread():
    from dataclasses import replace

    candidate = replace(spec(), settings=t.SessionSettings(extra={"applied_git_identity": "off"}))
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    b.rpc.call.return_value = {"data": ["s"]}
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await b.open_session("s", candidate)
    assert [c.args[0] for c in b.rpc.call.call_args_list] == ["thread/loaded/list"]


async def test_auto_fallback_records_off_only_after_cold_resume():
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    b.rpc.call.side_effect = [
        {"data": []},
        {"config": {}},
        {"thread": {"id": "s", "cwd": "/repo"}},
        {"data": []},
    ]
    info = await b.open_session("s", spec(False))
    assert info.settings.extra["applied_git_identity"] == "off"
    assert b.rpc.call.call_args_list[2].args[1]["config"] == identity_config(spec(False))


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("resume", [True, False])
async def test_identity_preserves_effective_environment_policy(enabled, resume):
    from copy import deepcopy

    policy = {
        "exclude": ["DATABASE_URL", "PRIVATE_*"],
        "set": {"KEEP_ME": "configured", "GIT_AUTHOR_NAME": "old override"},
        "inherit": "core",
        "ignore_default_excludes": False,
    }
    before = deepcopy(policy)
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True

    async def call(method, params):
        if method == "config/read":
            assert params == {"cwd": "/repo", "includeLayers": False}
            return {"config": {"shell_environment_policy": policy}}
        if method in ("thread/start", "thread/resume"):
            overrides = params["config"]
            assert overrides["shell_environment_policy.exclude"] == [
                "DATABASE_URL",
                "PRIVATE_*",
                *GIT_KEYS,
                BOT_KEY,
            ]
            values = overrides["shell_environment_policy.set"]
            assert values["KEEP_ME"] == "configured"
            assert values.get("GIT_AUTHOR_NAME") == ("Codex · test" if enabled else "old override")
            assert set(overrides) == {
                "shell_environment_policy.exclude",
                "shell_environment_policy.set",
            }
            return {"thread": {"id": "s", "cwd": "/repo"}}
        return {"data": []}

    b.rpc.call.side_effect = call
    if resume:
        await b.open_session("s", spec(enabled))
    else:
        await b.create_session(spec(enabled))
    assert policy == before


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("resume", [True, False])
async def test_identity_accepts_null_policy_fields(enabled, resume):
    # Live 0.157.1 config/read with no configured policy returns explicit nulls.
    policy = {
        "exclude": None,
        "set": None,
        "inherit": None,
        "ignore_default_excludes": None,
        "include_only": None,
    }
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    sent = {}

    async def call(method, params):
        if method == "config/read":
            return {"config": {"shell_environment_policy": policy}}
        if method in ("thread/start", "thread/resume"):
            sent.update(params["config"])
            return {"thread": {"id": "s", "cwd": "/repo"}}
        return {"data": []}

    b.rpc.call.side_effect = call
    if resume:
        await b.open_session("s", spec(enabled))
    else:
        await b.create_session(spec(enabled))
    assert sent["shell_environment_policy.exclude"] == [*GIT_KEYS, BOT_KEY]
    who = ("Codex · test", "test-bot@example.zulipchat.com")
    expected = dict(zip(GIT_KEYS, who * 2, strict=True))
    assert sent["shell_environment_policy.set"] == (expected if enabled else {})


@pytest.mark.parametrize("resume", [True, False])
async def test_unknown_policy_never_starts_or_resumes_a_thread(resume):
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True

    async def call(method, params):
        if method == "thread/loaded/list":
            return {"data": []}
        assert method == "config/read"
        raise t.BackendUnavailable("config unavailable")

    b.rpc.call.side_effect = call
    with pytest.raises(t.BackendUnavailable, match="config unavailable"):
        if resume:
            await b.open_session("s", spec())
        else:
            await b.create_session(spec())
    assert not b.specs
    assert not {"thread/start", "thread/resume"} & {c.args[0] for c in b.rpc.call.call_args_list}


def apply_policy(config: dict, base: dict[str, str]) -> dict[str, str]:
    """What a Codex shell gets from these overrides: exclusions first, then `set`."""
    env = {k: v for k, v in base.items() if k not in config["shell_environment_policy.exclude"]}
    return env | config["shell_environment_policy.set"]


@pytest.mark.parametrize("bot", ["codex-alice", ""])
def test_tool_processes_name_the_bot_or_lose_an_inherited_one(bot):
    """Issue #248: a tool process carries ZAB_BOT from the spec, so `zab history` reads as
    the session's bot; without a bot, the app-server's own ZAB_BOT never reaches it."""
    session = replace(spec(), bot=bot)
    env = apply_policy(identity_config(session), dict(os.environ) | {BOT_KEY: "the-daemon"})
    seen = subprocess.check_output(
        [sys.executable, "-c", "import os; print(os.environ.get('ZAB_BOT', '-'))"],
        env=env,
        text=True,
    ).strip()
    assert seen == (bot or "-")
    # The spec's bot overrides a configured one; without a bot, the user's own `set` stays,
    # as it does for the git identity in "off" mode.
    configured = {"exclude": None, "set": {BOT_KEY: "configured"}}
    who = ("Codex · test", "test-bot@example.zulipchat.com")
    assert identity_config(session, configured)["shell_environment_policy.set"] == {
        **dict(zip(GIT_KEYS, who * 2, strict=True)),
        BOT_KEY: bot or "configured",
    }


async def test_start_and_resume_carry_the_specs_bot():
    """Issue #248: thread/start and a cold thread/resume both send the spec's ZAB_BOT next to
    the git identity, the path verified to reach shell processes (DECISIONS, 2026-10-11)."""
    session = replace(spec(), bot="codex-alice")
    sent: list[tuple[str, dict]] = []
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True

    async def call(method, params):
        if method == "config/read":
            return {"config": {"shell_environment_policy": {"exclude": None, "set": None}}}
        if method in ("thread/start", "thread/resume"):
            sent.append((method, params["config"]["shell_environment_policy.set"]))
            return {"thread": {"id": "s", "cwd": "/repo"}}
        return {"data": []}

    b.rpc.call.side_effect = call
    info = await b.create_session(session)
    fresh = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    fresh.rpc = b.rpc
    await fresh.open_session("s", replace(session, settings=info.settings))
    assert [(method, values[BOT_KEY]) for method, values in sent] == [
        ("thread/start", "codex-alice"),
        ("thread/resume", "codex-alice"),
    ]


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_loaded_thread_with_a_known_identity_is_resumed_whatever_its_bot(enabled):
    """DECISIONS, 2026-10-13: ZAB_BOT is not part of the loaded-thread check. A thread the
    app-server loaded under v0.2.1, whose persisted marker records only the git identity, is
    resumed with a bot, not refused; so is one reopened for another bot. Until the app-server
    restarts its shells keep the old ZAB_BOT, which only sets `zab history`'s default."""
    marker = (
        {"name": "Codex · test", "email": "test-bot@example.zulipchat.com"} if enabled else "off"
    )
    v021 = replace(
        spec(enabled),
        bot="codex-alice",
        settings=t.SessionSettings(extra={"applied_git_identity": marker}),
    )
    b = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    b.rpc = AsyncMock()
    b.rpc.up = True
    sent: list[str] = []

    async def call(method, params):
        if method == "thread/loaded/list":
            return {"data": ["s"]}  # loaded: a refusal would follow this call
        if method == "config/read":
            return {"config": {"shell_environment_policy": {"exclude": None, "set": None}}}
        if method == "thread/resume":
            sent.append(params["config"]["shell_environment_policy.set"][BOT_KEY])
            return {"thread": {"id": "s", "cwd": "/repo"}}
        return {"data": []}

    b.rpc.call.side_effect = call
    info = await b.open_session("s", v021)
    await b.open_session("s", replace(v021, bot="codex-bob", settings=info.settings))
    assert sent == ["codex-alice", "codex-bob"]
    assert "thread/loaded/list" not in [c.args[0] for c in b.rpc.call.call_args_list]
