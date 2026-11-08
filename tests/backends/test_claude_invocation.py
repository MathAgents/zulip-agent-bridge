"""How a Claude turn is invoked: argv, environment, ids (SPEC §9.1 to §9.3, §9.8)."""

import json
import re
import uuid

import pytest

from zab.backends.claude import (
    NESTING_ENV,
    SCRUBBED_ENV,
    ClaudeConfig,
    build_argv,
    effective_settings,
    encode_project_dir,
    new_session_id,
    new_turn_id,
    turn_env,
)
from zab.core.types import SessionSettings

SID = "11111111-2222-4333-8444-555555555555"


def test_argv_first_turn_has_documented_flags_and_only_non_empty_settings():
    config = ClaudeConfig(binary="/opt/bin/claude")
    argv = build_argv(config, SessionSettings(), session_id=SID, resume=False)
    assert argv[:2] == ["/opt/bin/claude", "-p"]
    assert argv[2:6] == ["--output-format", "stream-json", "--verbose", "--permission-prompts"]
    assert argv[6] == "none"
    assert argv[7:9] == ["--session-id", SID]
    assert len(argv) == 9  # nothing else: empty settings mean the agent's defaults (§5.2)


def test_argv_resume_and_all_settings():
    config = ClaudeConfig(allowed_tools=["Bash(git *)", "Edit"], disallowed_tools=["WebFetch"])
    settings = SessionSettings(model="opus", effort="high", approval="acceptEdits")
    argv = build_argv(config, settings, session_id=SID, resume=True, context_prompt="CTX")
    assert "--resume" in argv and SID in argv and "--session-id" not in argv
    assert "--fork-session" not in argv  # §9.3: never
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    i = argv.index("--allowedTools")
    assert argv[i + 1 : i + 3] == ["Bash(git *)", "Edit"]
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch"
    assert argv[-2:] == ["--append-system-prompt", "CTX"]


def test_argv_is_a_list_of_plain_strings_never_a_shell_string():
    argv = build_argv(ClaudeConfig(), SessionSettings(model="haiku"), session_id=SID, resume=False)
    assert all(isinstance(a, str) for a in argv)
    assert not any(" && " in a or ";" in a for a in argv)


def test_turn_env_scrubs_api_keys_and_sets_config_dir():
    base = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "HOME": "/home/x",
    }
    base.update(
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "tok2",
            "ANTHROPIC_BASE_URL": "https://proxy",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_BEARER_TOKEN_BEDROCK": "b",
        }
    )
    env = turn_env("/home/x/.claude2", base)
    assert env["CLAUDE_CONFIG_DIR"] == "/home/x/.claude2"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x"
    for name in SCRUBBED_ENV:
        assert name not in env
    for name in NESTING_ENV:  # always dropped (issue #36)
        assert name not in env
    assert "CLAUDECODE" not in turn_env("/x", base, nested_ok=True)
    assert base["ANTHROPIC_API_KEY"] == "sk-ant-secret"  # the input is not mutated
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_EFFORT_LEVEL",  # would override --effort (DECISIONS)
    ):
        assert name in SCRUBBED_ENV


def test_effective_settings_overlays_backend_defaults():
    config = ClaudeConfig(
        defaults=SessionSettings(model="sonnet", approval="bypassPermissions"),
        default_account="main",
    )
    s = effective_settings(config, SessionSettings(model="haiku"))
    assert (s.model, s.approval, s.account, s.effort) == (
        "haiku",
        "bypassPermissions",
        "main",
        None,
    )
    s2 = effective_settings(config, SessionSettings(account="alt", approval="acceptEdits"))
    assert (s2.model, s2.approval, s2.account) == ("sonnet", "acceptEdits", "alt")


def test_config_dir_lookup_and_from_mapping():
    config = ClaudeConfig.from_mapping(
        {
            "binary": "",
            "accounts": {"main": "~/.claude", "alt": "/srv/claude-alt"},
            "default_account": "alt",
            "defaults": {"model": "", "approval": "bypassPermissions"},
            "allowed_tools": [],
        }
    )
    assert config.binary == "claude"
    assert config.config_dir(None) == "/srv/claude-alt"
    assert config.config_dir("main").endswith("/.claude") and "~" not in config.config_dir("main")
    assert config.defaults.model is None and config.defaults.approval == "bypassPermissions"
    with pytest.raises(KeyError):
        config.config_dir("nope")


def test_ids():
    sid = new_session_id()
    assert uuid.UUID(sid).version == 4
    tid = new_turn_id(sid)
    assert tid.startswith(sid + ":") and re.fullmatch(r"[0-9a-f]{32}", tid.split(":")[1])
    assert new_turn_id(sid) != tid


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/home/claude/18.705", "-home-claude-18-705"),
        ("/var/x/-home-y", "-var-x--home-y"),
        ("/work/scratch", "-work-scratch"),
        ("/var/zab-record-crs5_n89", "-var-zab-record-crs5-n89"),  # as in the interrupt fixture
        ("/work/my_repo", "-work-my-repo"),
        ("/work/with space", "-work-with-space"),
        ("/work/café", "-work-caf-"),
    ],
)
def test_encode_project_dir(cwd, expected):
    assert encode_project_dir(cwd) == expected  # verified on 2.1.283 (DECISIONS §9.8)


def test_find_transcript(tmp_path):
    from zab.backends.claude import find_transcript

    sid = "33333333-3333-4333-8333-333333333333"
    projects = tmp_path / "projects"
    (projects / "-work-my-repo").mkdir(parents=True)
    (projects / "-work-my-repo" / f"{sid}.jsonl").write_text("{}\n")
    assert find_transcript(str(tmp_path), sid, "/work/my_repo") == str(
        projects / "-work-my-repo" / f"{sid}.jsonl"
    )
    assert find_transcript(str(tmp_path), sid) == str(projects / "-work-my-repo" / f"{sid}.jsonl")
    assert find_transcript(str(tmp_path), sid, "/elsewhere") == str(
        projects / "-work-my-repo" / f"{sid}.jsonl"
    )
    assert find_transcript(str(tmp_path), "44444444-4444-4444-8444-444444444444") is None
    assert find_transcript(str(tmp_path), "../-work-my-repo/" + sid) is None
    assert find_transcript(str(tmp_path / "nowhere"), sid) is None


def test_turn_env_leaves_the_default_account_to_the_cli(tmp_path, monkeypatch):
    """PM review on PR #53: with CLAUDE_CONFIG_DIR=~/.claude the CLI reads ~/.claude/.claude.json,
    not ~/.claude.json, so bridge sessions would miss the MCP servers, settings and onboarding
    state of interactive `claude`. For the default directory the variable stays unset."""
    monkeypatch.setenv("HOME", str(tmp_path))
    default = tmp_path / ".claude"
    default.mkdir()
    (tmp_path / "link").symlink_to(default)
    base = {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/inherited/elsewhere"}
    for spelling in (
        str(default),
        "~/.claude",
        str(tmp_path / "link"),
        str(tmp_path / "x/../.claude"),
    ):
        env = turn_env(spelling, base)
        assert "CLAUDE_CONFIG_DIR" not in env, spelling  # the inherited value is dropped too
        assert env["PATH"] == "/usr/bin"
    other = tmp_path / ".claude2"
    other.mkdir()
    assert turn_env(str(other), base)["CLAUDE_CONFIG_DIR"] == str(other)
    assert turn_env("/srv/claude-alt", base)["CLAUDE_CONFIG_DIR"] == "/srv/claude-alt"
    from zab.backends.claude import ClaudeBackend, is_default_config_dir

    assert is_default_config_dir("~/.claude") and not is_default_config_dir(str(other))
    # the card's resume command carries the variable only when it is needed
    assert ClaudeBackend.resume_hint(str(default), "/w", SID) == f"cd /w && claude --resume {SID}"
    assert ClaudeBackend.resume_hint(str(other), "/w", SID) == (
        f"cd /w && CLAUDE_CONFIG_DIR={other} claude --resume {SID}"
    )


def test_turn_env_turns_off_claudeai_connectors_unless_asked(monkeypatch):
    """PM on issue #36 (Sun 20:42): the account's claude.ai connectors stay out of bridge turns."""
    assert turn_env("/x", {"PATH": "/bin"})["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert "ENABLE_CLAUDEAI_MCP_SERVERS" not in turn_env("/x", {"PATH": "/bin"}, connectors=True)
    assert ClaudeConfig.from_mapping({}).claudeai_connectors is False
    assert ClaudeConfig.from_mapping({"claudeai_connectors": True}).claudeai_connectors is True


def test_the_table_steers_unless_steer_is_false():
    """Drew, Oct 15 (issue #310): an absent `steer` key means on; a `ClaudeConfig` built
    directly (the dev tools) keeps the plain invocation."""
    assert ClaudeConfig.from_mapping({}).steer is True
    assert ClaudeConfig.from_mapping({"steer": False}).steer is False
    assert ClaudeConfig().steer is False
    steered = build_argv(
        ClaudeConfig.from_mapping({}), SessionSettings(), session_id=SID, resume=False
    )
    assert "--input-format" in steered and "--replay-user-messages" in steered
    plain = build_argv(
        ClaudeConfig.from_mapping({"steer": False}), SessionSettings(), session_id=SID, resume=False
    )
    assert "--input-format" not in plain and "--replay-user-messages" not in plain


# ------------------------------------------------- the agent's commit identity (issue #214)


def test_turn_env_sets_the_agents_git_identity_or_removes_inherited_ones():
    from zab.backends.claude import GIT_IDENTITY_ENV
    from zab.core.types import GitIdentity

    who = GitIdentity("Claude · alice", "claude-alice-bot@example.zulipchat.com")
    base = {"PATH": "/bin", "GIT_AUTHOR_NAME": "Stale", "GIT_COMMITTER_EMAIL": "stale@x"}
    env = turn_env("/x", base, git_identity=who)
    assert [env[k] for k in GIT_IDENTITY_ENV] == [
        "Claude · alice",
        "claude-alice-bot@example.zulipchat.com",
        "Claude · alice",
        "claude-alice-bot@example.zulipchat.com",
    ]
    # "off": the user's own git config applies, whatever the bridge inherited
    assert not set(GIT_IDENTITY_ENV) & set(turn_env("/x", base))


def test_argv_turns_commit_attribution_off_only_when_asked():
    """Issue #214: with the agent's git identity, Claude Code's Co-Authored-By trailer would
    show a second author on GitHub; without one, the argv is exactly as before."""
    from zab.backends.claude import NO_COMMIT_ATTRIBUTION

    config, settings = ClaudeConfig(), SessionSettings(model="haiku")
    plain = build_argv(config, settings, session_id=SID, resume=False)
    assert "--settings" not in plain
    assert (
        build_argv(config, settings, session_id=SID, resume=False, commit_attribution=True) == plain
    )
    off = build_argv(config, settings, session_id=SID, resume=False, commit_attribution=False)
    assert off == [*plain, "--settings", NO_COMMIT_ATTRIBUTION]
    assert json.loads(NO_COMMIT_ATTRIBUTION) == {"attribution": {"commit": ""}}


def test_turn_env_names_the_bot_or_removes_an_inherited_one():
    """Issue #248: ZAB_BOT lets `zab history` in the turn read as this session's bot."""
    base = {"PATH": "/bin", "ZAB_BOT": "stale-bot"}
    assert turn_env("/x", base, bot="claude-alice")["ZAB_BOT"] == "claude-alice"
    assert "ZAB_BOT" not in turn_env("/x", base)


def test_turn_env_names_the_session_or_removes_an_inherited_one():
    """Issue #655: ZAB_SESSION tells `zab job` in the turn which session started a job."""
    base = {"PATH": "/bin", "ZAB_SESSION": "the-bridges-own"}
    env = turn_env("/x", base, bot="claude-alice", session="s-655")
    assert env["ZAB_SESSION"] == "s-655" and env["ZAB_BOT"] == "claude-alice"
    assert "ZAB_SESSION" not in turn_env("/x", base, bot="claude-alice")
