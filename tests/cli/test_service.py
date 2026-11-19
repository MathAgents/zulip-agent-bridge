import asyncio
import itertools
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from shutil import which as real_which
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from zab.commands import doctor, service
from zab.config import BackendConfig, BridgeConfig, Config, RenderConfig
from zab.core.types import Check

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/true")
    config = Config(tmp_path / "config $%.toml", BridgeConfig(), [], {}, [], RenderConfig())
    monkeypatch.setattr(service, "load_config", lambda path: config)
    control = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(service, "control", control)
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
    monkeypatch.setattr(service, "run", run)
    return config, control, run


def test_install_claude_only_omits_codex_and_quotes_config(setup, capsys):
    config, control, run = setup
    config.backends["claude"] = BackendConfig(kind="claude")
    assert service.main(["install"]) == 0
    directory = service.unit_directory()
    assert sorted(p.name for p in directory.glob("*.service")) == [
        "zab-sentinel.service",
        "zab.service",
    ]
    body = (directory / "zab.service").read_text()
    assert 'config $$%%.toml"' in body
    assert "UnsetEnvironment=OPENAI_API_KEY" in body
    assert "codex-app-server" not in body
    control.assert_any_call("enable", "--now", "--no-block", "zab.service", "zab-sentinel.service")
    assert run.call_args.args[0][1] == "enable-linger"
    output = capsys.readouterr().out
    assert "sudo loginctl enable-linger" in output
    assert "startup/restart queued and may still be pending" in output


def test_codex_daemons_are_deduplicated_and_explicitly_isolated(setup):
    config, _, _ = setup
    config.backends = {
        name: BackendConfig(kind="codex", codex_home="/isolated", binary="codex")
        for name in ("one", "two")
    }
    rendered = service.units(config)
    others = {"zab.service", "zab-sentinel.service", "zab-jobs.slice"}  # the slice: #655
    assert set(rendered) == {"codex-app-server.service", *others}
    assert 'Environment="CODEX_HOME=/isolated"' in rendered["codex-app-server.service"]
    config.backends["two"].codex_home = "/other"
    assert len(service.units(config)) == 2 + len(others)
    config.backends["two"].connect = "spawn"
    assert len(service.units(config)) == 1 + len(others)


def test_uninstall_preserves_unmanaged_units(setup):
    _, control, _ = setup
    assert service.main(["install"]) == 0
    unmanaged = service.unit_directory() / "codex-app-server.service"
    unmanaged.write_text("# belongs to someone else\n")
    assert service.main(["uninstall"]) == 0
    assert unmanaged.exists()
    assert not (service.unit_directory() / "zab.service").exists()
    # the sentinel too: left running, it would go on answering the participant's DMs (#204)
    assert not (service.unit_directory() / "zab-sentinel.service").exists()
    control.assert_any_call("disable", "--now", "zab.service")
    control.assert_any_call("disable", "--now", "zab-sentinel.service")
    assert not any("codex-app-server.service" in call.args for call in control.call_args_list)


def test_install_refuses_unmanaged_destination_before_service_changes(setup):
    _, control, _ = setup
    directory = service.unit_directory()
    directory.mkdir(parents=True)
    target = directory / "zab.service"
    target.write_text("# manual\n")
    assert service.main(["install"]) == 1
    control.assert_not_called()
    assert target.read_text() == "# manual\n"


def test_service_path_contains_discovered_tool_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/opt/{name}/bin/{name}")
    path = service.service_path().split(":")
    assert path[:5] == [f"/opt/{name}/bin" for name in ("codex", "claude", "uv", "git", "gp")]
    assert path[-3:] == ["/usr/local/bin", "/usr/bin", "/bin"]


def test_doctor_uses_and_restores_service_environment(setup, monkeypatch):
    config, _, _ = setup
    assert service.main(["install"]) == 0
    before = os.environ["PATH"]
    monkeypatch.setenv("OPENAI_API_KEY", "decoy")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "decoy")
    monkeypatch.setattr(doctor, "load_config", lambda _: config)

    async def checks(config):
        assert os.environ["PATH"] == service.service_path()
        assert "OPENAI_API_KEY" not in os.environ
        assert "CODEX_ACCESS_TOKEN" not in os.environ
        return [Check(name="service", ok=True)]

    monkeypatch.setattr(doctor, "checks", checks)
    assert doctor.main(["--service-env", "--json"]) == 0
    assert os.environ["PATH"] == before and os.environ["OPENAI_API_KEY"] == "decoy"
    assert os.environ["CODEX_ACCESS_TOKEN"] == "decoy"  # noqa: S105 - synthetic fixture


def test_unit_arguments_reject_control_characters():
    with pytest.raises(ValueError):
        service.quote("path\nExecStart=bad")


def test_exported_codex_home_is_pinned_in_both_units(setup, monkeypatch):
    config, _, _ = setup
    config.backends["codex"] = BackendConfig(kind="codex")
    monkeypatch.setenv("CODEX_HOME", "/isolated/exported-home")
    rendered = service.units(config)
    assert all(
        'Environment="CODEX_HOME=/isolated/exported-home"' in body and "CODEX_ACCESS_TOKEN" in body
        for name, body in rendered.items()
        if name not in ("zab-sentinel.service", "zab-jobs.slice")  # they run no agent
    )
    assert "CODEX_ACCESS_TOKEN" in rendered["zab-sentinel.service"]
    assert service.main(["install"]) == 0
    monkeypatch.setenv("CODEX_HOME", "/different")
    with service.service_environment():
        assert os.environ["CODEX_HOME"] == "/isolated/exported-home"
    assert os.environ["CODEX_HOME"] == "/different"


def test_reinstall_restarts_only_changed_active_units(setup):
    config, control, run = setup
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/isolated")
    assert service.main(["install"]) == 0
    control.reset_mock()
    run.return_value.returncode = 0
    config.path = config.path.with_name("different.toml")
    assert service.main(["install"]) == 0
    control.assert_any_call("try-restart", "--no-block", "zab.service", "zab-sentinel.service")
    assert not any(
        call.args[0] == "try-restart" and "codex-app-server.service" in call.args
        for call in control.call_args_list
    )
    control.reset_mock()
    assert service.main(["install"]) == 0
    # only the sentinel, on every install: it reads its code and the configuration at start
    restarts = [call.args for call in control.call_args_list if call.args[0] == "try-restart"]
    assert restarts == [("try-restart", "--no-block", "zab-sentinel.service")]


def test_same_config_path_with_changed_home_restarts_bridge_too(setup):
    config, control, run = setup
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/one")
    assert service.main(["install"]) == 0
    control.reset_mock()
    run.return_value.returncode = 0
    config.backends["codex"].codex_home = "/two"
    assert service.main(["install"]) == 0
    restarted = ("codex-app-server.service", "zab.service", "zab-sentinel.service")
    control.assert_any_call("try-restart", "--no-block", *restarted)


@pytest.mark.parametrize("returncode", [0, 1])
def test_workshop_venv_is_used_by_units_and_doctor(setup, monkeypatch, capsys, returncode):
    from pathlib import Path

    config, _, _ = setup
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/isolated")
    config.bridge.service_path_prepend = ["~/venv/bin"]
    config.bridge.service_env = {"VIRTUAL_ENV": "~/venv", "ZAB_TEST_ENV": "quoted space % value"}
    venv = Path.home() / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin/python3"
    python.write_text(
        "#!/bin/sh\n"
        'test "$VIRTUAL_ENV" = "' + str(venv) + '" || exit 9\n'
        'test "$2" = "import numpy, cypari2" || exit 8\n'
        f"exit {returncode}\n"
    )
    python.chmod(0o755)
    assert service.main(["install"]) == 0
    for name, body in service.units(config).items():
        if name == "zab-jobs.slice":
            continue  # no process of its own: each job's environment is its caller's (#655)
        assert f'Environment="PATH={venv}/bin:' in body
        assert f'Environment="VIRTUAL_ENV={venv}"' in body
    monkeypatch.setenv("VIRTUAL_ENV", "/interactive")
    before = os.environ["PATH"]
    monkeypatch.setattr(doctor, "load_config", lambda _: config)

    async def checks(config):
        assert os.environ["ZAB_TEST_ENV"] == "quoted space % value"
        return []

    monkeypatch.setattr(doctor, "checks", checks)
    # Let the diagnostic discover the actual decoy interpreter through unit PATH.
    monkeypatch.setattr(doctor.shutil, "which", real_which)
    assert doctor.main(["--service-env", "--json"]) == returncode
    output = capsys.readouterr().out
    assert "service:python" in output
    assert os.environ["VIRTUAL_ENV"] == "/interactive" and os.environ["PATH"] == before
    assert "ZAB_TEST_ENV" not in os.environ


def test_standard_tool_directories_keep_conventional_order(monkeypatch):
    from pathlib import Path

    tools = {"git": "/usr/bin/git", "gp": "/usr/local/bin/gp", "codex": "/opt/codex/bin/codex"}
    monkeypatch.setattr(service.shutil, "which", tools.get)
    path = service.service_path(["~/venv/bin", "~/.elan/bin"]).split(":")
    assert path == [
        str(Path.home() / "venv/bin"),
        str(Path.home() / ".elan/bin"),
        "/opt/codex/bin",
        str(Path.home() / ".local/bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]


@pytest.mark.parametrize("gp", ["/usr/local/bin/gp", None])
def test_doctor_service_gp_is_informational(setup, monkeypatch, capsys, gp):
    config, _, _ = setup
    assert service.main(["install"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(doctor, "load_config", lambda _: config)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: gp)
    monkeypatch.setattr(
        doctor,
        "service_restart_check",
        AsyncMock(return_value=Check(name="service:restarts", ok=True)),
    )

    async def checks(config):
        return []

    monkeypatch.setattr(doctor, "checks", checks)
    assert doctor.main(["--service-env", "--json"]) == 0
    import json

    result = json.loads(capsys.readouterr().out)
    assert [row for row in result if row["name"] == "service:gp"] == [
        {
            "name": "service:gp",
            "ok": True,
            "detail": gp or "not found in service PATH (informational)",
            "warning": False,
        }
    ]


def test_bridge_unit_watchdog_does_not_change_codex_unit(tmp_path, monkeypatch):
    from zab.commands import service
    from zab.config import BackendConfig, BridgeConfig, Config, RenderConfig

    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(),
        [],
        {"codex": BackendConfig(kind="codex", socket=str(tmp_path / "decoy.sock"))},
        [],
        RenderConfig(),
    )
    rendered = service.units(config)
    assert (
        "Type=notify\nNotifyAccess=main\nTimeoutStartSec=1800\nWatchdogSec=300\n"
        in rendered["zab.service"]
    )
    assert "WatchdogSec" not in rendered["codex-app-server.service"]


def test_sentinel_unit_runs_on_its_own_and_always_restarts(tmp_path, monkeypatch):
    """`zab-sentinel.service` (#204; SPEC §13.3): the sentinel with the bridge's config and
    PATH, restarted whatever ends it, started at boot (a woken VM included), and with no tie to
    `zab.service`, so that it answers while the bridge is down."""
    monkeypatch.setattr(service, "binary", lambda name: "/home/a/.local/bin/" + name)
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    config = Config(tmp_path / "config.toml", BridgeConfig(), [], {}, [], RenderConfig())
    body = service.units(config)["zab-sentinel.service"]
    path = service.service_path()
    assert body == (
        "# Managed by zab service install\n"
        "[Unit]\nDescription=zab sentinel: !vm and !restart in direct messages\n"
        "[Service]\n"
        f'ExecStart="/home/a/.local/bin/zab" "sentinel" "--config" "{tmp_path}/config.toml"\n'
        f'Environment="PATH={path}"\n'
        f"UnsetEnvironment={' '.join(service.SCRUB)}\n"
        "Restart=always\nRestartSec=10\n"
        "[Install]\nWantedBy=default.target\n"
    )


def test_reinstall_over_an_unmanaged_sentinel_unit_is_refused(setup):
    _, control, _ = setup
    directory = service.unit_directory()
    directory.mkdir(parents=True)
    (directory / "zab-sentinel.service").write_text("# mine\n")
    assert service.main(["install"]) == 1
    control.assert_not_called()
    assert service.managed_units(directory) == []


# ------------------------------------------------------------- the Codex daemon's open files


def test_the_codex_unit_raises_its_open_file_limit_alone(tmp_path, monkeypatch):
    """#664: the Codex app-server's own limit, which its `lean-lsp-mcp` children's pipes reach
    (#632). The bridge's unit and the sentinel's keep the default: the bridge holds a few files
    a turn, and Claude's processes, its children, end with their turn."""
    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(),
        [],
        {"codex": BackendConfig(kind="codex", socket=str(tmp_path / "decoy.sock"))},
        [],
        RenderConfig(),
    )
    rendered = service.units(config)
    codex = rendered["codex-app-server.service"]
    assert "\nLimitNOFILE=65536\nRestart=on-failure\n" in codex
    assert codex.count("LimitNOFILE=") == 1
    assert "LimitNOFILE=4096\n" in service.units(config, nofile=4096)["codex-app-server.service"]
    assert all("LimitNOFILE" not in rendered[n] for n in ("zab.service", "zab-sentinel.service"))


@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        ("1048576\n", 65536),  # the AMI
        ("524288\n", 65536),  # older systemd
        ("4096\n", 4096),  # a user manager that allows less
        ("infinity\n", 65536),
    ],
)
def test_the_limit_stays_within_the_user_managers(monkeypatch, manager, expected):
    """The user manager's hard limit caps it: a daemon started without the line has that hard
    limit, which `prlimit` can't raise. The installing process's own limit is not read (review
    of #665): a session with `ulimit -Hn 512` would otherwise `prlimit` the daemon down."""
    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=manager))
    monkeypatch.setattr(service, "run", run)
    assert service.nofile_limit() == expected
    argv = run.call_args.args[0]
    assert argv[1:] == ["--user", "show", "-p", "DefaultLimitNOFILE", "--value"]
    run.return_value = SimpleNamespace(returncode=1, stdout="")  # no user manager to ask
    assert service.nofile_limit() == 65536
    run.side_effect = OSError("no systemctl")
    assert service.nofile_limit() == 65536
    assert not hasattr(service, "resource")


def _systemd(
    prlimit_ok: bool = True, main_pid: str = "937\n", show_rc: int = 0, active: bool = True
) -> tuple[Mock, list[list[str]]]:
    """`run` as systemctl and prlimit answer it: by default every unit active, the daemon's PID
    937, and `prlimit` succeeding."""
    seen: list[list[str]] = []

    def answer(argv, check=True):
        seen.append(argv)
        if "DefaultLimitNOFILE" in argv:
            return SimpleNamespace(returncode=0, stdout="1048576\n")
        if "MainPID" in argv:
            return SimpleNamespace(returncode=show_rc, stdout=main_pid)
        if any(a.startswith("--pid=") for a in argv):
            return SimpleNamespace(returncode=0 if prlimit_ok else 1, stdout="")
        if "is-active" in argv:
            return SimpleNamespace(returncode=0 if active else 3, stdout="")
        return SimpleNamespace(returncode=0, stdout="")  # enable-linger

    return Mock(side_effect=answer), seen


@pytest.mark.parametrize("prlimit_ok", [True, False])
def test_a_running_daemon_gets_the_new_limit_without_a_restart(
    setup, monkeypatch, capsys, prlimit_ok
):
    """#664: a rollout over a release whose Codex unit had no limit gives the running daemon the
    new one with `prlimit`, and doesn't restart it, which would end its running Codex turns. If
    `prlimit` fails, the limit waits for the daemon's next start, and the install says so."""
    config, control, _ = setup
    monkeypatch.setattr(service, "nofile_limit", lambda: 65536)
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/isolated")
    assert service.main(["install"]) == 0
    unit = service.unit_directory() / "codex-app-server.service"
    unit.write_text(unit.read_text().replace("LimitNOFILE=65536\n", ""))  # as v0.2.10 wrote it
    run, seen = _systemd(prlimit_ok)
    monkeypatch.setattr(service, "run", run)
    control.reset_mock()
    capsys.readouterr()
    assert service.main(["install"]) == 0
    assert "LimitNOFILE=65536\n" in unit.read_text()
    restarts = [call.args for call in control.call_args_list if call.args[0] == "try-restart"]
    assert restarts == [("try-restart", "--no-block", "zab-sentinel.service")]
    assert ["/usr/bin/true", "--pid=937", "--nofile=65536:65536"] in seen
    note = "codex-app-server.service: its open-file limit (65536) applies at its next start"
    assert (note in capsys.readouterr().out) is not prlimit_ok


def test_a_daemon_whose_unit_changed_otherwise_too_is_restarted(setup, monkeypatch):
    """The limit and another setting: a restart, as for any other change, and no `prlimit`."""
    config, control, _ = setup
    monkeypatch.setattr(service, "nofile_limit", lambda: 65536)
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/one")
    assert service.main(["install"]) == 0
    unit = service.unit_directory() / "codex-app-server.service"
    unit.write_text(unit.read_text().replace("LimitNOFILE=65536\n", ""))
    config.backends["codex"].codex_home = "/two"
    run, seen = _systemd()
    monkeypatch.setattr(service, "run", run)
    control.reset_mock()
    assert service.main(["install"]) == 0
    restarted = ("codex-app-server.service", "zab.service", "zab-sentinel.service")
    control.assert_any_call("try-restart", "--no-block", *restarted)
    assert not any(a.startswith("--pid=") for argv in seen for a in argv)


def test_a_codex_version_change_rewrites_the_unit_and_restarts_the_daemon(
    setup, monkeypatch, tmp_path
):
    """SPEC §8.3: the configuration resolves the Codex binary, so the daemon's unit names the
    package, `…/codex-<version>/bin/codex`. Once the rollout's step has relinked
    ~/.local/bin/codex, the install after it writes the new path and restarts the running
    daemon onto it; a plain restart before it would run the old one."""
    from zab.config import load_config

    _, control, _ = setup
    which = lambda name: real_which(name) if "/" in name else "/usr/bin/" + name  # noqa: E731
    monkeypatch.setattr(service.shutil, "which", which)  # the Codex binary as the VM's
    link = tmp_path / "bin" / "codex"
    link.parent.mkdir()
    for version in ("0.157.1", "0.169.1"):
        program = tmp_path / f"codex-{version}" / "bin" / "codex"
        program.parent.mkdir(parents=True)
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
    link.symlink_to(tmp_path / "codex-0.157.1" / "bin" / "codex")
    toml = tmp_path / "zab.toml"
    toml.write_text(f'[backends.codex]\nkind = "codex"\nbinary = "{link}"\n')
    monkeypatch.setattr(service, "load_config", lambda path: load_config(toml))
    assert service.main(["install"]) == 0
    unit = service.unit_directory() / "codex-app-server.service"
    old = (tmp_path / "codex-0.157.1" / "bin" / "codex").resolve()
    assert f'ExecStart="{old}" ' in unit.read_text()
    link.unlink()
    link.symlink_to(tmp_path / "codex-0.169.1" / "bin" / "codex")
    run, _ = _systemd()
    monkeypatch.setattr(service, "run", run)
    control.reset_mock()
    assert service.main(["install"]) == 0
    new = (tmp_path / "codex-0.169.1" / "bin" / "codex").resolve()
    assert f'ExecStart="{new}" ' in unit.read_text() and str(old) not in unit.read_text()
    restarts = [c.args for c in control.call_args_list if c.args[0] == "try-restart"]
    assert restarts and "codex-app-server.service" in restarts[0]


def test_reload_only_reads_the_limit_and_oom_policy_lines_alone():
    old = "[Service]\nExecStart=x\nRestart=on-failure\n"
    new = "[Service]\nExecStart=x\nOOMPolicy=continue\nLimitNOFILE=65536\nRestart=on-failure\n"
    assert service.reload_only(old, new) and service.reload_only(new, new.replace("65536", "4096"))
    assert service.reload_only(new, new.replace("OOMPolicy=continue\n", ""))
    assert not service.reload_only(new, new)  # unchanged: nothing to do
    assert not service.reload_only(old, new.replace("ExecStart=x", "ExecStart=y"))
    assert service.limit_changed(old, new)
    assert service.limit_changed(new, new.replace("65536", "4096"))
    assert not service.limit_changed(new, new.replace("OOMPolicy=continue\n", ""))


@pytest.mark.parametrize(
    ("main_pid", "show_rc"),
    [("0\n", 0), ("", 0), ("n/a\n", 0), ("937\n", 1)],
    ids=["no main process", "empty", "not a number", "show failed"],
)
def test_raise_limit_without_a_pid_runs_no_prlimit(monkeypatch, main_pid, show_rc):
    """Review of #665: `systemctl show` gives MainPID 0 for an inactive or unknown unit, with
    exit 0. No PID, or no answer: no `prlimit`, and False, so that install prints the note."""
    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    run, seen = _systemd(main_pid=main_pid, show_rc=show_rc)
    monkeypatch.setattr(service, "run", run)
    assert service.raise_limit("codex-app-server.service", 65536) is False
    assert not any(a.startswith("--pid=") for argv in seen for a in argv)
    run.side_effect = OSError("no systemctl")
    assert service.raise_limit("codex-app-server.service", 65536) is False


def test_an_inactive_daemon_starts_from_the_new_unit(setup, monkeypatch, capsys):
    """Review of #665: a daemon that isn't running, whose unit changed in its limit alone, gets
    no `prlimit` and no restart: `enable --now` starts it from the new unit, with the limit."""
    config, control, _ = setup
    monkeypatch.setattr(service, "nofile_limit", lambda: 65536)
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/isolated")
    assert service.main(["install"]) == 0
    unit = service.unit_directory() / "codex-app-server.service"
    unit.write_text(unit.read_text().replace("LimitNOFILE=65536\n", ""))
    run, seen = _systemd(active=False)
    monkeypatch.setattr(service, "run", run)
    control.reset_mock()
    capsys.readouterr()
    assert service.main(["install"]) == 0
    assert "LimitNOFILE=65536\n" in unit.read_text()
    assert not any(a.startswith("--pid=") or a == "MainPID" for argv in seen for a in argv)
    restarts = [call.args for call in control.call_args_list if call.args[0] == "try-restart"]
    assert restarts == [("try-restart", "--no-block", "zab-sentinel.service")]
    enabled = [call.args for call in control.call_args_list if call.args[0] == "enable"]
    assert enabled and "codex-app-server.service" in enabled[0]
    assert "open-file limit" not in capsys.readouterr().out


# ------------------------------------------------------------- a job killed for memory (#997)


def test_the_agents_units_outlive_a_job_killed_for_memory(tmp_path, monkeypatch):
    """#997: an agent's commands run in its backend's unit, a Claude turn's under the bridge's
    and a Codex turn's under the daemon's. Both units keep running when the kernel's OOM killer
    kills one of their processes (`OOMPolicy=continue`), instead of systemd's default, which
    stops the unit. The sentinel runs no agent and keeps the default."""
    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(),
        [],
        {"codex": BackendConfig(kind="codex", socket=str(tmp_path / "decoy.sock"))},
        [],
        RenderConfig(),
    )
    rendered = service.units(config)
    for name in ("zab.service", "codex-app-server.service"):
        body = rendered[name]
        assert body.count("OOMPolicy=") == 1 and "\nOOMPolicy=continue\n" in body, name
        assert body.index("[Service]") < body.index("OOMPolicy=") < body.index("[Install]")
    assert "OOMPolicy" not in rendered[service.SENTINEL]


@pytest.mark.parametrize("limit_too", [False, True], ids=["policy alone", "limit too"])
def test_a_running_daemon_gets_its_oom_policy_without_a_restart(
    setup, monkeypatch, capsys, limit_too
):
    """#997: a rollout over a release whose units had no `OOMPolicy=` restarts no running Codex
    daemon, which would end its Codex turns: the manager's reload hands it the policy, and
    `prlimit` runs only where the limit is new too (#664). The bridge's unit changed, so it is
    restarted, as every rollout restarts it anyway."""
    config, control, _ = setup
    monkeypatch.setattr(service, "nofile_limit", lambda: 65536)
    config.backends["codex"] = BackendConfig(kind="codex", codex_home="/isolated")
    assert service.main(["install"]) == 0
    directory = service.unit_directory()
    for name in ("codex-app-server.service", "zab.service"):  # as v0.2.20 wrote them
        unit = directory / name
        unit.write_text(unit.read_text().replace(service.OOM_POLICY, ""))
    if limit_too:  # as v0.2.10 wrote the daemon's
        unit = directory / "codex-app-server.service"
        unit.write_text(unit.read_text().replace("LimitNOFILE=65536\n", ""))
    run, seen = _systemd()
    monkeypatch.setattr(service, "run", run)
    control.reset_mock()
    capsys.readouterr()
    assert service.main(["install"]) == 0
    for name in ("codex-app-server.service", "zab.service"):
        assert service.OOM_POLICY in (directory / name).read_text(), name
    calls = [call.args for call in control.call_args_list]
    restarts = [args for args in calls if args[0] == "try-restart"]
    assert restarts == [("try-restart", "--no-block", "zab.service", "zab-sentinel.service")]
    assert calls.index(("daemon-reload",)) < calls.index(restarts[0])
    prlimit = ["/usr/bin/true", "--pid=937", "--nofile=65536:65536"]
    assert (prlimit in seen) is limit_too
    assert "open-file limit" not in capsys.readouterr().out


def test_the_bridge_unit_backs_off_its_restarts(tmp_path, monkeypatch):
    """#806: the bridge's automatic restarts back off from `RestartSec` (5 s) to 120 s in 5
    steps (`service.RESTART_BACKOFF`), with no start limit, which would leave it `failed` until
    `!restart` (review round 1 of #813); the sentinel's and Codex's units keep theirs."""
    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(),
        [],
        {"codex": BackendConfig(kind="codex", socket=str(tmp_path / "decoy.sock"))},
        [],
        RenderConfig(),
    )
    rendered = service.units(config)
    bridge = rendered["zab.service"]
    assert "Restart=on-failure\nRestartSec=5\nRestartSteps=5\nRestartMaxDelaySec=120\n" in bridge
    assert "StartLimit" not in bridge
    for other in (rendered[service.SENTINEL], rendered["codex-app-server.service"]):
        assert "RestartSteps" not in other and "RestartMaxDelaySec" not in other


def _user_manager() -> bool:
    tool = real_which("systemctl")
    if not (tool and os.environ.get("XDG_RUNTIME_DIR")):
        return False
    return subprocess.run([tool, "--user", "show-environment"], capture_output=True).returncode == 0  # noqa: S603


# 15 s. On a pull request, the unit's text (above) and the sentinel's commands
# (tests/tools/test_sentinel.py) are checked without systemd.
@pytest.mark.slow
@pytest.mark.skipif(not _user_manager(), reason="no user systemd manager")
async def test_a_bridge_failing_at_start_backs_off_and_recovers_on_its_own(tmp_path, monkeypatch):
    """#806 on a real user manager: a unit with the bridge's backoff, its times a fifth of the
    shipped ones (`RestartSec` 1 s, growing to 24 s in the same 5 steps), whose process fails
    at start while its cause lasts (a file: Zulip down, say). Its restarts come further and
    further apart, each delay about 1.9 times the last, and it never ends `failed`. Once the
    cause clears, the next restart starts it, with no `!restart`. The sentinel's `!restart`
    (`reset-failed`, then `restart --no-block`) starts it at once, as before."""
    from zab.tools import sentinel as sn

    monkeypatch.setattr(service, "binary", lambda name: "/usr/bin/" + name)
    config = Config(tmp_path / "config.toml", BridgeConfig(), [], {}, [], RenderConfig())
    rendered = service.units(config)["zab.service"]
    shipped = dict(
        line.split("=", 1) for line in rendered.splitlines() if line.startswith("Restart")
    )
    scaled = [  # a fifth of the times, the same steps
        "Restart=on-failure",
        f"RestartSec={int(shipped['RestartSec']) / 5}",
        f"RestartSteps={shipped['RestartSteps']}",
        f"RestartMaxDelaySec={int(shipped['RestartMaxDelaySec']) / 5}",
    ]
    cause, starts = tmp_path / "zulip-down", tmp_path / "starts"
    cause.touch()
    # A start is recorded once it has seen the cause, so that the test, seeing it, knows how it
    # ends; `%%`, as systemd expands `%` in a command
    record = f"date +%%s.%%N >> {starts}"
    run = f"if [ -e {cause} ]; then {record}; exit 1; fi; {record}; exec sleep 300"
    name = f"zab-test-{uuid.uuid4().hex[:12]}-backoff.service"
    units = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    (units / name).write_text(
        "\n".join(["[Service]", f"ExecStart=/bin/sh -c '{run}'", *scaled, ""])
    )
    me = SimpleNamespace(run=sn.run_command)  # the sentinel's own way to run systemctl

    async def systemctl(*args: str) -> int:
        return (await sn.run_command(["systemctl", "--user", *args], 30))[0]

    def times() -> list[float]:
        return [float(t) for t in starts.read_text().split()] if starts.exists() else []

    async def until(condition, seconds: float) -> bool:
        for _ in range(int(seconds / 0.05)):
            if await condition():
                return True
            await asyncio.sleep(0.05)
        return False

    async def started(count: int) -> bool:
        return len(times()) >= count

    async def active() -> bool:
        return await sn.Sentinel.unit_state(me, name) == "active"

    try:
        assert await systemctl("daemon-reload") == 0
        assert await systemctl("start", "--no-block", name) == 0
        assert await until(lambda: started(4), 60)
        # The delays, 1, 1.89 and 3.57 s (24 ** (n / 5)), are each gap's least: a loaded
        # machine only adds to them, so the gaps are checked from below (review round 2)
        gaps = [b - a for a, b in itertools.pairwise(times())]
        assert [g >= 0.95 * 24 ** (n / 5) for n, g in enumerate(gaps)] == [True] * 3, gaps
        cause.unlink()  # Zulip answers again: the next restart, about 6.8 s later, starts it
        # `Type=simple`: active once the process is forked, maybe before it records its start
        assert await until(lambda: started(5), 60) and await until(active, 15)
        assert len(times()) == 5
        await asyncio.sleep(1)
        assert await active()
        cause.touch()  # down again: the sentinel's `!restart` starts it at once all the same
        assert await sn.Sentinel.restart(me, [name], block=False) == 0
        assert await until(lambda: started(6), 15)
    finally:
        await systemctl("stop", name)
        await systemctl("reset-failed", name)
        (units / name).unlink(missing_ok=True)
        await systemctl("daemon-reload")


# Under a second. Without a user manager, the units' text and the install's restarts (above) are
# checked without systemd.
@pytest.mark.skipif(not _user_manager(), reason="no user systemd manager")
def test_a_unit_outlives_its_child_killed_for_memory(tmp_path):
    """#997 on a real user manager: two units, each a shell whose child outgrows the unit's
    64 MB `MemoryMax`, as an agent's job can outgrow the VM, and is OOM-killed. systemd's default
    stops the first, `oom-kill`. The second gets the units' `OOMPolicy=` in its file while it
    runs, then a reload, as a Codex daemon does from the install that adds the line: its shell
    runs on, the same process."""
    tool = real_which("systemctl")
    units = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    go = tmp_path / "go"
    tag = uuid.uuid4().hex[:12]
    names = [f"zab-test-{tag}-oom-{policy}.service" for policy in ("default", "continue")]

    def systemctl(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([tool, "--user", *args], capture_output=True, text=True, timeout=30)  # noqa: S603

    def show(name: str) -> dict[str, str]:
        out = systemctl("show", "-p", "ActiveState", "-p", "Result", "-p", "MainPID", name).stdout
        return dict(line.split("=", 1) for line in out.splitlines())

    for name in names:
        job = tmp_path / f"{name}.sh"
        job.write_text(
            f"while [ ! -e '{go}' ]; do sleep 0.1; done\n"
            f"'{sys.executable}' -c 'x = b\"x\" * (300 * 2**20)'\n"
            f"echo ran >> '{tmp_path / name}'\nexec sleep 300\n"
        )
        start = " ".join(service.quote(arg, command=True) for arg in ["/bin/sh", str(job)])
        (units / name).write_text(f"[Service]\nExecStart={start}\nMemoryMax=64M\nMemorySwapMax=0\n")
    try:
        assert systemctl("daemon-reload").returncode == 0
        assert systemctl("start", *names).returncode == 0
        before = show(names[1])["MainPID"]
        (units / names[1]).write_text((units / names[1]).read_text() + service.OOM_POLICY)
        assert systemctl("daemon-reload").returncode == 0
        go.touch()
        for _ in range(200):
            if show(names[0])["ActiveState"] != "active" and (tmp_path / names[1]).exists():
                break
            time.sleep(0.1)
        first, second = show(names[0]), show(names[1])
        if first["ActiveState"] == "active" and (tmp_path / names[0]).exists():
            pytest.skip("the child outgrew MemoryMax unkilled: no memory controller here")
        assert first["Result"] == "oom-kill", first
        assert second == {"ActiveState": "active", "Result": "success", "MainPID": before}
        assert (tmp_path / names[1]).read_text() == "ran\n"
    finally:
        systemctl("stop", *names)
        systemctl("reset-failed", *names)
        for name in names:
            (units / name).unlink(missing_ok=True)
        systemctl("daemon-reload")
