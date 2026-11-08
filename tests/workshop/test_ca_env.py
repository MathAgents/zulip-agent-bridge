"""No variable that adds a trust store to the system's is set for the agent (#882, X1):
`workshop/ami/files/bin/ca_env.py`, and the image's smoke tests that run it. Offline: the
script's commands go through a fake `run`; its shell probe runs in a real bash."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

AMI = Path(__file__).resolve().parents[2] / "workshop" / "ami"
SCRIPT = AMI / "files" / "bin" / "ca_env.py"


def load():
    loader = SourceFileLoader("ca_env", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ca_env", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ca_env = load()


class FakeHost:
    """Answers the script's three reads as a VM would; records each argument list."""

    def __init__(self):
        self.calls = []
        self.shell = "ca-env done\n"
        self.manager = CompletedProcess([], 0, "HOME=/home/agent\nLANG=C.UTF-8\n", "")
        self.unit = CompletedProcess([], 0, "", "")  # no zab.service: an empty Environment=

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "bash" in argv:
            return None if self.shell is None else CompletedProcess(argv, 0, self.shell, "")
        return self.manager if "show-environment" in argv else self.unit


@pytest.fixture
def host(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(ca_env, "run", fake)
    monkeypatch.setattr(
        ca_env, "pwd", SimpleNamespace(getpwnam=lambda u: SimpleNamespace(pw_uid=1001))
    )
    return fake


def test_nothing_set_anywhere_passes(host, capsys):
    assert ca_env.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "login shell: ok",
        "user manager: ok",
        "zab.service: ok",
        "ok: neither SSL_CERT_FILE nor CODEX_CA_CERTIFICATE is set for agent in the places read",
    ]
    login, manager, unit = host.calls
    assert login == ["sudo", "-u", "agent", "-H", "bash", "-lic", ca_env.SHELL_PROBE]
    bus = [
        "XDG_RUNTIME_DIR=/run/user/1001",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
    ]
    assert manager == [
        "sudo",
        "-u",
        "agent",
        "env",
        *bus,
        "systemctl",
        "--user",
        "show-environment",
    ]
    show = ["systemctl", "--user", "show", "--property=Environment", "--value", "zab.service"]
    assert unit == ["sudo", "-u", "agent", "env", *bus, *show]


def test_each_variable_set_is_named_where_it_is_set_and_no_value_shown(host, capsys):
    """#882, criterion 6: the smoke test fails with a message naming the variable."""
    host.shell = "Welcome!\nca-env set: SSL_CERT_FILE\nca-env done\n"  # a profile's own output
    host.manager.stdout += "CODEX_CA_CERTIFICATE=/etc/secret-ca.pem\nSSL_CERT_DIR=/x\n"
    host.unit.stdout = 'PATH=/usr/bin "VIRTUAL_ENV=/a b" "SSL_CERT_FILE=/opt/ca file.pem"\n'
    assert ca_env.main(["agent"]) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "login shell: SET SSL_CERT_FILE",
        "user manager: SET CODEX_CA_CERTIFICATE",
        "zab.service: SET SSL_CERT_FILE",
        "FAILED: the SET or FAILED lines above (either variable adds roots to the system's)",
    ]
    assert "/etc/secret-ca.pem" not in out and "ca file" not in out


def test_a_place_that_cant_be_read_fails(host, capsys):
    """A check that read nothing proves nothing: a shell that ended before its last line (an
    `exit` in a profile, a timeout), or a user manager systemctl can't reach."""
    host.shell = "ca-env set: CODEX_CA_CERTIFICATE\n"  # no "done"
    host.manager = CompletedProcess([], 1, "", "Failed to connect to bus")
    host.unit = None
    assert ca_env.main([]) == 1
    assert capsys.readouterr().out.splitlines()[:3] == [
        "login shell: FAILED, not read",
        "user manager: FAILED, not read",
        "zab.service: FAILED, not read",
    ]
    host.shell = None  # bash didn't start, or ran past TIMEOUT_S
    host.manager = CompletedProcess([], 0, "", "")
    host.unit = CompletedProcess([], 0, "", "")
    assert ca_env.main([]) == 1


def test_usage_and_an_unknown_user(monkeypatch, capsys):
    assert ca_env.main(["-h"]) == 2 and ca_env.main(["a", "b"]) == 2
    assert "ca_env.py [USER]" in capsys.readouterr().err

    def missing(user):
        raise KeyError(user)

    monkeypatch.setattr(ca_env, "pwd", SimpleNamespace(getpwnam=missing))
    assert ca_env.main(["nobody-here"]) == 2


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({}, []),
        ({"SSL_CERT_FILE": "/x.pem"}, ["SSL_CERT_FILE"]),
        ({"CODEX_CA_CERTIFICATE": ""}, ["CODEX_CA_CERTIFICATE"]),  # set, though empty
        (
            {"SSL_CERT_FILE": "a", "CODEX_CA_CERTIFICATE": "b", "SSL_CERT_DIR": "c"},
            list(ca_env.NAMES),
        ),
    ],
)
def test_the_shell_probe_names_what_is_set(env, named):
    """The login shell's probe, in a real bash: a variable set, even to nothing, is named."""
    argv = ["bash", "-c", ca_env.SHELL_PROBE]
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    out = subprocess.run(  # noqa: S603 - the script's own probe
        argv, env={**base, **env}, capture_output=True, text=True, check=True
    )
    assert out.stdout.splitlines() == [*(f"ca-env set: {n}" for n in named), "ca-env done"]
    assert sorted(ca_env.NAMES) == sorted(("SSL_CERT_FILE", "CODEX_CA_CERTIFICATE"))
    assert all(n in ca_env.SHELL_PROBE for n in ca_env.NAMES)


def test_both_smoke_tests_run_it_and_fail_on_it():
    """build.sh's smoke test at the end of the build, and build_ami.py's on a fresh instance."""
    build = (AMI / "build.sh").read_text()
    smoke = build[build.index("# --- 13. smoke test") : build.index("# --- 14. housekeeping")]
    run = 'if ca_env=$(sudo /usr/bin/python3 -I "$FILES/bin/ca_env.py" "$AGENT" 2>&1); then'
    after = smoke[smoke.index(run) :]
    assert "printf 'FAIL agent environment:" in after.split("\nfi\n", 1)[0]
    assert "fail=1" in after.split("\nfi\n", 1)[0]
    assert smoke.index(run) < smoke.index('if [ "$fail" -ne 0 ]')
    import build_ami

    instance = build_ami.SMOKE_SCRIPT
    run = "if ca_env=$(sudo /usr/bin/python3 -I /usr/local/share/workshop/bin/ca_env.py agent 2>&1)"
    block = instance[instance.index(run) :].split("\nfi\n", 1)[0]
    assert "FAIL agent environment:" in block and "fail=1" in block
    assert instance.index(run) < instance.index('echo "SMOKE-RESULT $fail"')
    assert os.access(SCRIPT, os.X_OK)
