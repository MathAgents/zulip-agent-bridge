from pathlib import Path

import pytest

from zab.config import BotConfig, ConfigError, allowed_users, load_config, merge_settings

pytestmark = pytest.mark.contract


def test_reference_config_loads():
    config = load_config(Path(__file__).parents[2] / "config/zab.example.toml")
    assert config.backends["codex"].connect == "daemon"
    assert config.bots[0].name == "codex"


def test_paths_are_relative_to_config_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[bridge]\nstate_dir="state"\ndefault_project="repo"\n')
    monkeypatch.setenv("ZAB_CONFIG", str(path))
    config = load_config()
    assert config.bridge.state_dir == str(tmp_path / "state")
    assert config.bridge.default_project == str(tmp_path / "repo")


@pytest.mark.parametrize(
    "content,key",
    [
        ("[bridge]\nsecret_typo=true", "bridge.secret_typo"),
        ('[bridge]\nallow_public_channels="false"', "bridge.allow_public_channels"),
        ("[bridge]\nmax_concurrent_turns=0", "bridge.max_concurrent_turns"),
        ("[bridge]\nmax_concurrent_turns=true", "bridge.max_concurrent_turns"),
        ('[[bots]]\nname="../lock"', "bots.name"),
        ('[[bots]]\nname="bot"\nzuliprc="bot.rc"\nbackend="missing"', "bots.backend"),
        ('[backends.c]\nkind="codex"\ndefaults={typo="x"}', "defaults.typo"),
        ('[[projects]]\nchannel="x"\npath="."\nsubdir="../outside"', "projects.subdir"),
        ("[render]\nauto_upload_ext=[1]", "render.auto_upload_ext"),
        ("[render]\nmax_text_upload_kb=0", "render.max_text_upload_kb"),
        ("[render]\nmax_text_uploads=-1", "render.max_text_uploads"),
        ('[render]\nauto_upload_text_ext="lean"', "render.auto_upload_text_ext"),
        ("bots={}", "bots"),
        ('[bridge]\nservice_path_prepend=["a:b"]', "service_path_prepend"),
        ('[bridge]\nservice_env={PATH="/bin"}', "service_env"),
        ("[bridge]\nservice_env={VIRTUAL_ENV=3}", "service_env"),
    ],
)
def test_invalid_configs_name_key_and_location(tmp_path, content, key):
    path = tmp_path / "bad.toml"
    path.write_text(content)
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert key in str(exc.value)
    assert f"{path}:" in str(exc.value)


@pytest.mark.parametrize("value", [0, 1, 10, 59])
def test_a_status_edit_interval_below_a_minute_is_refused(tmp_path, value):
    """#754: Zulip Cloud's guidance, an edit about once a minute at most (SPEC §4.9)."""
    path = tmp_path / "config.toml"
    path.write_text(f"[bridge]\nstatus_edit_interval_s={value}\n")
    with pytest.raises(ConfigError, match=r"bridge\.status_edit_interval_s: must be"):
        load_config(path)


def test_the_status_edit_interval_is_five_minutes_by_default(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[bridge]\n")
    assert load_config(path).bridge.status_edit_interval_s == 300
    path.write_text("[bridge]\nstatus_edit_interval_s=60\n")
    assert load_config(path).bridge.status_edit_interval_s == 60


def test_allowlist_replaces_owner():
    assert allowed_users(BotConfig(allow_user_ids=[10]), 20) == {10}
    assert allowed_users(BotConfig(), 20) == {20}
    assert not allowed_users(BotConfig(), None)


def test_settings_precedence_and_empty_reset():
    settings = merge_settings({"model": "old", "effort": "high"}, {"model": ""})
    assert settings.model is None
    assert settings.effort == "high"


def test_service_paths_resolve_relative_to_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[bridge]\nservice_path_prepend=["venv/bin"]\nservice_env={VIRTUAL_ENV="venv"}\n'
    )
    config = load_config(path)
    assert config.bridge.service_path_prepend == [str(tmp_path / "venv/bin")]
    assert config.bridge.service_env == {"VIRTUAL_ENV": str(tmp_path / "venv")}


@pytest.mark.parametrize("tools", [None, [], ["Read", "Bash(ls *)"]])
def test_claude_allow_rules_default_only_when_omitted(tmp_path, tools):
    import json

    from zab.core.backends import make_backend

    path = tmp_path / "config.toml"
    text = '[backends.claude]\nkind="claude"\naccounts={main="account"}\n'
    if tools is not None:
        text += "allowed_tools=" + json.dumps(tools) + "\n"
    path.write_text(text)
    backend = make_backend("claude", load_config(path).backends["claude"])
    assert backend.config.allowed_tools == (
        ["Read", "Glob", "Grep", "Edit", "Write"] if tools is None else tools
    )


@pytest.mark.parametrize("emails", ["[1]", '[""]', '["no-at"]', '["a@b c"]'])
def test_email_allowlist_validation(tmp_path, emails):
    path = tmp_path / "config.toml"
    path.write_text(
        '[backends.c]\nkind="codex"\n[[bots]]\nname="b"\nbackend="c"\n'
        'zuliprc="b.rc"\nallow_emails=' + emails
    )
    with pytest.raises(ConfigError, match=r"bots\.allow_emails"):
        load_config(path)


def test_email_resolution_prefers_delivery_and_combines_explicit_ids():
    bot = BotConfig(allow_user_ids=[99], allow_emails=["OWNER@example.org"])
    users = [
        dict(user_id=20, is_bot=False, email="masked", delivery_email="owner@EXAMPLE.org"),
        dict(user_id=21, is_bot=False, email="owner@example.org", delivery_email=None),
        dict(user_id=22, is_bot=False, email="owner@example.org", delivery_email="different@x"),
        dict(user_id=23, is_bot=False, email="owner@example.org", is_active=False),
        dict(user_id=24, is_bot=True, email="owner@example.org"),
    ]
    assert allowed_users(bot, 2, users) == {20, 21, 99}
    assert allowed_users(BotConfig(allow_emails=["not-joined@x"]), 2, users) == set()


def test_text_uploads_default_on_and_configurable(tmp_path):
    """Issue #36 (Drew, Sun 18:40): referenced text files are uploaded by default."""
    config = load_config(Path(__file__).parents[2] / "config/zab.example.toml")
    assert "lean" in config.render.auto_upload_text_ext and "html" not in (
        config.render.auto_upload_text_ext
    )
    assert (config.render.max_text_upload_kb, config.render.max_text_uploads) == (512, 10)
    path = tmp_path / "c.toml"
    path.write_text('[render]\nauto_upload_text_ext=["lean"]\nmax_text_uploads=0\n')
    off = load_config(path)
    assert off.render.auto_upload_text_ext == ["lean"] and off.render.max_text_uploads == 0


def test_claudeai_connectors_option(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[backends.claude]\nkind="claude"\naccounts={main="~/.claude"}\nclaudeai_connectors=true\n'
    )
    assert load_config(path).backends["claude"].claudeai_connectors is True
    path.write_text(
        '[backends.claude]\nkind="claude"\naccounts={main="~/.claude"}\nclaudeai_connectors="yes"\n'
    )
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "line,steer", [("", True), ("steer = true\n", True), ("steer = false\n", False)]
)
def test_claude_steering_defaults_on_and_false_is_honoured(tmp_path, line, steer):
    """Drew, Oct 15 (issue #310): `[backends.claude] steer` is on when the key is absent, and
    `steer = false` still turns it off, all the way to the backend's capabilities (SPEC §9.7)."""
    from zab.core.backends import make_backend

    path = tmp_path / "config.toml"
    head = '[backends.claude]\nkind = "claude"\naccounts = { main = "~/.claude" }\n'
    path.write_text(head + line)
    backend = load_config(path).backends["claude"]
    assert backend.steer is steer
    claude = make_backend("claude", backend)
    assert claude.config.steer is steer and claude.capabilities.steer is steer
    path.write_text(head + 'steer = "no"\n')
    with pytest.raises(ConfigError, match=r"backends\.claude\.steer: invalid value type"):
        load_config(path)


def test_multi_party_defaults_on_and_can_be_disabled(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    assert load_config(path).bridge.multi_party is True
    path.write_text("[bridge]\nmulti_party = false\n")
    assert load_config(path).bridge.multi_party is False


@pytest.mark.parametrize("value", ["auto", "off", "Codex · alice <bot@example.test>"])
def test_git_identity_config(tmp_path, value):
    import json

    path = tmp_path / "identity.toml"
    path.write_text('[backends.codex]\nkind="codex"\ngit_identity=' + json.dumps(value))
    assert load_config(path).backends["codex"].git_identity == value


@pytest.mark.parametrize("value", ['"bad"', "true", '"Name <missing-at>"'])
def test_invalid_git_identity_config(tmp_path, value):
    path = tmp_path / "identity.toml"
    path.write_text('[backends.codex]\nkind="codex"\ngit_identity=' + value)
    with pytest.raises(ConfigError, match="git_identity"):
        load_config(path)


def test_worktree_post_create_is_an_argument_list(tmp_path):
    """Issue #205: off by default; an argument list of strings, never a shell string."""
    path = tmp_path / "config.toml"
    path.write_text("")
    assert load_config(path).bridge.worktree_post_create == []
    path.write_text(
        '[bridge]\nworktree_post_create = ["/usr/local/bin/lean-project", "--repair-all"]\n'
    )
    assert load_config(path).bridge.worktree_post_create == [
        "/usr/local/bin/lean-project",
        "--repair-all",
    ]
    for bad in ('"lean-project --repair-all"', "[1, 2]"):
        path.write_text(f"[bridge]\nworktree_post_create = {bad}\n")
        with pytest.raises(ConfigError, match="worktree_post_create"):
            load_config(path)
