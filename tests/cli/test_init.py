import stat
from pathlib import Path

import pytest

from zab.commands import init
from zab.config import load_config

pytestmark = pytest.mark.integration


def test_idempotent_init_preserves_existing_and_sets_permissions(tmp_path, monkeypatch):
    source = tmp_path / "source.zuliprc"
    source.write_text("decoy credentials")
    target = tmp_path / "config.toml"

    async def identity(path):
        return {"user_id": 1, "bot_owner_id": 2, "is_bot": True}

    monkeypatch.setattr(init, "_identity", identity)
    args = [
        "--config",
        str(target),
        "--bot",
        "dev",
        "--zuliprc",
        str(source),
        "--backend",
        "codex",
        "--allow-user-id",
        "3",
        "--codex-home",
        "isolated",
    ]
    assert init.main(args) == 0
    assert init.main(args) == 0
    config = load_config(target)
    assert len(config.bots) == 1
    assert config.bots[0].allow_user_ids == [3]
    assert config.backends["codex"].codex_home == str(tmp_path / "isolated")
    assert stat.S_IMODE((tmp_path / "dev.zuliprc").stat().st_mode) == 0o600
    assert (tmp_path / "dev.zuliprc").read_text() == "decoy credentials"


def test_failed_auth_does_not_write(tmp_path, monkeypatch):
    async def identity(path):
        raise ValueError("not a bot")

    monkeypatch.setattr(init, "_identity", identity)
    target = tmp_path / "config.toml"
    assert (
        init.main(
            ["--config", str(target), "--bot", "dev", "--zuliprc", "missing", "--backend", "codex"]
        )
        == 1
    )
    assert not target.exists()


def test_workshop_requires_explicit_owner_and_project():
    with pytest.raises(SystemExit) as exc:
        init.main(
            ["--profile", "workshop", "--bot", "dev", "--zuliprc", "missing", "--backend", "codex"]
        )
    assert exc.value.code == 2


def test_extend_partial_unicode_config(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    target = tmp_path / "config.toml"
    target.write_text('[bridge]\ndefault_project = "repos/🚀"\n')
    source = tmp_path / "source.zuliprc"
    source.write_text("decoy")
    monkeypatch.setattr(init, "_identity", AsyncMock(return_value={"user_id": 1, "is_bot": True}))
    assert (
        init.main(
            [
                "--config",
                str(target),
                "--bot",
                "dev",
                "--zuliprc",
                str(source),
                "--backend",
                "codex",
            ]
        )
        == 0
    )
    assert load_config(target).bridge.default_project == str(tmp_path / "repos/🚀")
    import tomllib

    assert tomllib.loads(init._toml({"🚀": "🧮"})) == {"🚀": "🧮"}


@pytest.mark.parametrize("reference", ["new.zuliprc", "alias.zuliprc"])
def test_init_rejects_another_bots_credential_destination(tmp_path, monkeypatch, reference):
    from unittest.mock import AsyncMock

    target = tmp_path / "config.toml"
    target.write_text(
        '[[bots]]\nname="existing"\nbackend="codex"\nzuliprc="' + reference + '"\n'
        '[backends.codex]\nkind="codex"\n'
    )
    credential = tmp_path / "new.zuliprc"
    credential.write_text("existing decoy")
    (tmp_path / "alias.zuliprc").symlink_to(credential)
    source = tmp_path / "source.zuliprc"
    source.write_text("new decoy")
    original = target.read_bytes()
    monkeypatch.setattr(init, "_identity", AsyncMock(return_value={"user_id": 1, "is_bot": True}))
    assert (
        init.main(
            [
                "--config",
                str(target),
                "--bot",
                "new",
                "--zuliprc",
                str(source),
                "--backend",
                "codex",
            ]
        )
        == 1
    )
    assert credential.read_text() == "existing decoy"
    assert target.read_bytes() == original


def _personal(tmp_path, monkeypatch, profile: list[str]) -> dict[str, bytes]:
    """Run `zab init` with `profile`'s arguments in a directory of its own; return its files."""
    from unittest.mock import AsyncMock

    home = tmp_path / (profile[-1] if profile else "default")
    home.mkdir()
    config = home / "config.toml"
    config.write_text('[bridge]\nmachine="laptop"\n')  # an existing config, extended
    args = ["--config", str(config), *profile]
    for name in ("codex", "claude"):
        source = home / f"source-{name}.zuliprc"
        source.write_text(f"decoy {name}")
        args.extend(["--bot", name, "--zuliprc", str(source), "--backend", name])
    monkeypatch.setattr(init, "_identity", AsyncMock(return_value={"user_id": 1, "is_bot": True}))
    monkeypatch.setattr(init.shutil, "which", lambda name: f"/opt/{name}/bin/{name}")
    assert init.main(args) == 0
    files = {p.name: p.read_bytes() for p in sorted(home.iterdir()) if p.is_file()}
    # the paths written into the config name the directory: compare the files without it
    return {name: data.replace(str(home).encode(), b"HOME") for name, data in files.items()}


def test_personal_is_the_default_and_drew_its_alias(tmp_path, monkeypatch):
    """#593: `--profile personal` is the default, and `drew`, its earlier name, means it."""
    personal = _personal(tmp_path, monkeypatch, ["--profile", "personal"])
    assert _personal(tmp_path, monkeypatch, []) == personal
    assert _personal(tmp_path, monkeypatch, ["--profile", "drew"]) == personal
    assert personal["codex.zuliprc"] == b"decoy codex"
    # what the profile writes (SPEC §5.3); the profile's name is not among it
    text = personal["config.toml"].decode()
    assert "personal" not in text and "drew" not in text and "profile" not in text
    loaded = load_config(tmp_path / "default" / "config.toml")
    assert loaded.bridge.default_workspace == "shared" and loaded.bridge.machine == "laptop"
    assert loaded.backends["codex"].defaults == {"approval": "never", "sandbox": "workspace-write"}
    assert loaded.backends["codex"].connect == "daemon"
    assert loaded.backends["claude"].defaults == {"approval": "bypassPermissions"}
    # one account, the CLI's default directory, asked of no one (SPEC §5.3, §9.2; #621)
    assert loaded.backends["claude"].accounts == {"main": str(Path.home() / ".claude")}
    assert loaded.backends["claude"].default_account == "main"
    assert all(bot.allow_user_ids == [] for bot in loaded.bots)


EXISTING_CLAUDE = """\
[bridge]
default_workspace = "shared"

[backends.claude]
kind = "claude"
binary = "claude"
accounts = {{ main = "~/.claude", alt = "{alt}" }}
default_account = "alt"
defaults = {{ approval = "default" }}
"""


def test_personal_writes_claude_only_for_a_claude_bot_and_keeps_its_table(tmp_path, monkeypatch):
    """#621, SPEC §5.3: `zab init` writes `[backends.claude]` only when it adds a Claude bot to a
    config without that table, and a re-run over one, with the personal profile, leaves it whole."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(init, "_identity", AsyncMock(return_value={"user_id": 1, "is_bot": True}))
    source = tmp_path / "source.zuliprc"
    source.write_text("decoy")

    def run(config, backend):
        args = ["--config", str(config), "--bot", backend, "--zuliprc", str(source)]
        assert init.main([*args, "--backend", backend]) == 0
        return load_config(config).backends

    (tmp_path / "fresh").mkdir()
    assert "claude" not in run(tmp_path / "fresh" / "config.toml", "codex")
    (tmp_path / "kept").mkdir()
    config, alt = tmp_path / "kept" / "config.toml", str(tmp_path / "alt")
    config.write_text(EXISTING_CLAUDE.format(alt=alt))
    claude = run(config, "claude")["claude"]
    assert claude.accounts == {"main": str(Path.home() / ".claude"), "alt": alt}
    assert claude.default_account == "alt" and claude.defaults == {"approval": "default"}
    assert claude.binary == "claude"


def test_profile_help_names_personal_and_workshop_only(capsys):
    with pytest.raises(SystemExit) as exc:
        init.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--profile {personal,workshop}" in out and "drew" not in out
    with pytest.raises(SystemExit) as exc:
        init.main(["--profile", "other", "--bot", "dev", "--zuliprc", "x", "--backend", "codex"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'other'" in err and "drew" not in err


def test_workshop_profile_updates_both_backends_and_preserves_model(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    config = tmp_path / "config.toml"
    config.write_text(
        '[bridge]\ndefault_workspace="worktree"\nallow_public_channels=true\n'
        '[backends.codex]\nkind="codex"\nconnect="spawn"\n'
        '[backends.codex.defaults]\nmodel="selected"\nsandbox="workspace-write"\n'
    )
    sources = []
    for name in ("codex", "claude"):
        path = tmp_path / f"source-{name}.zuliprc"
        path.write_text(f"decoy {name}")
        sources.append(path)
    monkeypatch.setattr(init, "_identity", AsyncMock(return_value={"user_id": 1, "is_bot": True}))
    monkeypatch.setattr(init.shutil, "which", lambda name: f"/opt/{name}/bin/{name}")
    args = [
        "--config",
        str(config),
        "--profile",
        "workshop",
        "--project",
        str(tmp_path / "repo"),
        "--allow-user-id",
        "20",
    ]
    for name, source in zip(("codex", "claude"), sources, strict=True):
        args.extend(["--bot", name, "--zuliprc", str(source), "--backend", name])
    assert init.main(args) == 0
    assert init.main(args) == 0
    loaded = load_config(config)
    assert loaded.bridge.default_workspace == "shared"
    assert loaded.bridge.max_concurrent_turns == 2 and not loaded.bridge.allow_public_channels
    assert loaded.bridge.service_path_prepend == [str(Path.home() / "venv/bin")]
    assert loaded.bridge.service_env == {"VIRTUAL_ENV": str(Path.home() / "venv")}
    assert all(bot.allow_user_ids == [20] for bot in loaded.bots)
    assert loaded.backends["codex"].defaults == {
        "model": "selected",
        "approval": "never",
        "sandbox": "danger-full-access",
    }
    assert loaded.backends["codex"].connect == "daemon"
    assert loaded.backends["claude"].defaults["approval"] == "bypassPermissions"
    assert all(backend.binary.startswith("/opt/") for backend in loaded.backends.values())
    assert all(
        stat.S_IMODE((tmp_path / f"{name}.zuliprc").stat().st_mode) == 0o600
        for name in ("codex", "claude")
    )
