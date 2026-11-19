"""The agent CLIs at the release's pins (SPEC §8.3): `workshop/admin/agent_clis.py` on a fake VM's
home, its step in `push_config.py`, and the pins in one place (`src/zab/pins.py`)."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import math
import os
import pathlib
import re
import shutil
import tarfile
import zipfile

import agent_clis as clis
import build_ami as ba
import push_config as pc
import pytest

from zab import pins as zab_pins
from zab.backends import claude as claude_backend
from zab.backends import codex as codex_backend

from .test_admin_push import (
    ALICE,
    ALICE_ROW,
    KH,
    STAGING,
    FakeRunner,
    StatusRunner,
    _release_is_this_checkout,
    out_dir,
    roster,
    zab_status,
)

__all__ = ["_release_is_this_checkout", "out_dir", "roster"]  # the fixtures, used by name

ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_RELEASE_PINS = pc.release_pins  # before the autouse fixture stubs it
REAL_FETCH = clis.fetch  # before Vm replaces it
OLD_CODEX, NEW_CODEX, OLD_CLAUDE, NEW_CLAUDE = "0.157.1", "0.169.1", "2.1.283", "2.1.311"


def program(text: str) -> bytes:
    return f"#!/bin/sh\necho '{text}'\n".encode()


def package(version: str, *, executor: bool = True, executable: bool = True) -> bytes:
    """A Codex release package as 0.169.1's is laid out (its `codex-package.json` lists the
    executor, `execServer`), whose programs print their version."""
    meta = {"layoutVersion": 1, "version": version, "entrypoint": "bin/codex"}
    members = {
        "bin/codex": (program(f"codex-cli {version}"), 0o755),
        "bin/codex-code-mode-host": (program("host"), 0o755),
        "codex-path/rg": (program("rg"), 0o755),
        "codex-resources/bwrap": (program("bwrap"), 0o755),
    }
    if executor:
        meta["execServer"] = "bin/codex-exec-server"
        members["bin/codex-exec-server"] = (program("exec"), 0o755 if executable else 0o644)
    members["codex-package.json"] = (json.dumps(meta).encode(), 0o644)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, (data, mode) in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Vm:
    """agent's home as an image built with the old pins leaves it (build.sh §5, §6): the Codex
    package in ~/.local/lib/codex-<version>, the native installer's ~/.local/share/claude/versions/
    <version>, absolute links in ~/.local/bin; and the release files the step downloads."""

    def __init__(self, tmp: pathlib.Path, monkeypatch):
        self.layout = clis.Layout(str(tmp / "home"))
        self.lib, self.bin = pathlib.Path(self.layout.lib), pathlib.Path(self.layout.bin)
        self.versions = pathlib.Path(self.layout.versions)
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.codex_package, self.claude_binary = package(NEW_CODEX), program(f"{NEW_CLAUDE} (x)")
        self.pins = clis.Pins(
            NEW_CODEX, sha(self.codex_package), NEW_CLAUDE, sha(self.claude_binary)
        )
        self.fetched: list[str] = []
        monkeypatch.setattr(clis, "fetch", self.fetch)

    def fetch(self, url: str, dest: str) -> None:
        self.fetched.append(url)
        if url == clis.CODEX_URL.format(version=NEW_CODEX):
            pathlib.Path(dest).write_bytes(self.codex_package)
        elif url == clis.CLAUDE_URL.format(version=NEW_CLAUDE):
            pathlib.Path(dest).write_bytes(self.claude_binary)
        else:
            raise clis.Failed(f"download {url}: 404")

    def codex(self, version: str, *, linked: bool = True) -> Vm:
        bin_ = self.lib / f"codex-{version}" / "bin"
        bin_.mkdir(parents=True)
        for name, text in (("codex", f"codex-cli {version}"), ("codex-code-mode-host", "host")):
            (bin_ / name).write_bytes(program(text))
            (bin_ / name).chmod(0o755)
        if linked:
            self.bin.mkdir(parents=True, exist_ok=True)
            (self.bin / "codex").unlink(missing_ok=True)
            (self.bin / "codex").symlink_to(bin_ / "codex")
        return self

    def claude(self, version: str, *, linked: bool = True) -> Vm:
        self.versions.mkdir(parents=True, exist_ok=True)
        (self.versions / version).write_bytes(program(f"{version} (Claude Code)"))
        (self.versions / version).chmod(0o755)
        if linked:
            self.bin.mkdir(parents=True, exist_ok=True)
            (self.bin / "claude").unlink(missing_ok=True)
            (self.bin / "claude").symlink_to(self.versions / version)
        return self

    def daemon(self, pid: int, exe: str, *, unit: bool = True) -> None:
        """A Codex app-server of this user's in the fake /proc, run from `exe`: zab's daemon, as
        its unit starts it (`service.py`), or another client's, such as an editor's."""
        (self.proc / str(pid)).mkdir()
        argv = [str(self.bin / "codex").encode(), b"app-server"]
        argv += [b"--listen", b"unix:///home/agent/.codex/app.sock"] if unit else []
        (self.proc / str(pid) / "cmdline").write_bytes(b"\0".join([*argv, b""]))
        (self.proc / str(pid) / "exe").symlink_to(exe)  # canonical, as the kernel gives it

    def install(self) -> list[str]:
        return clis.install(self.pins, self.layout, str(self.proc))

    def state(self) -> dict[str, object]:
        """Every path under the home, with a link's target: what a step may have changed."""
        out: dict[str, object] = {}
        for path in sorted(pathlib.Path(self.layout.home).rglob("*")):
            out[str(path)] = os.readlink(path) if path.is_symlink() else path.is_dir()
        return out


@pytest.fixture
def vm(tmp_path, monkeypatch) -> Vm:
    return Vm(tmp_path, monkeypatch)


# --------------------------------------------------------------------- on the VM (agent_clis.py)


def test_a_vm_behind_gets_both_clis_and_keeps_the_previous_versions(vm):
    vm.codex("0.150.0", linked=False).codex(OLD_CODEX).claude("2.1.200", linked=False)
    vm.claude(OLD_CLAUDE)
    assert vm.install() == [
        f"agent CLIs: codex {OLD_CODEX} → {NEW_CODEX}, claude {OLD_CLAUDE} → {NEW_CLAUDE}"
    ]
    new = vm.lib / f"codex-{NEW_CODEX}"
    assert os.readlink(vm.bin / "codex") == str(new / "bin" / "codex")  # absolute, as build.sh's
    assert os.readlink(vm.bin / "claude") == str(vm.versions / NEW_CLAUDE)  # as the installer's
    assert clis.version_of(str(vm.bin / "codex")) == NEW_CODEX
    assert clis.version_of(str(vm.bin / "claude")) == NEW_CLAUDE
    # build.sh's members, and the executor its codex-package.json lists, executable
    for member in ("bin/codex-code-mode-host", "bin/codex-exec-server", "codex-path/rg"):
        assert os.access(new / member, os.X_OK), member
    assert (new / "codex-package.json").is_file() and (new / "codex-resources" / "bwrap").is_file()
    # the hash it was checked against, as build.sh records it (build_ami.py --smoke reads it)
    assert (new / "codex-package.sha256").read_text() == vm.pins.codex_sha256 + "\n"
    # the version linked before stays, for a rollback and a daemon still running it; older go
    assert sorted(p.name for p in vm.lib.iterdir()) == [f"codex-{OLD_CODEX}", f"codex-{NEW_CODEX}"]
    assert sorted(p.name for p in vm.versions.iterdir()) == [OLD_CLAUDE, NEW_CLAUDE]
    assert not list(vm.bin.glob(".*"))  # no temporary link left
    # the second run finds both at the pins and downloads nothing
    assert vm.install() == [clis.CURRENT] and len(vm.fetched) == 2


def test_current_at_the_pins_changes_nothing(vm):
    vm.codex(NEW_CODEX).claude(NEW_CLAUDE)
    before = vm.state()
    assert vm.install() == ["agent CLIs: current"]
    assert vm.state() == before and not vm.fetched


def test_one_behind_is_the_only_one_replaced(vm):
    vm.codex(NEW_CODEX).claude(OLD_CLAUDE)
    assert vm.install() == [f"agent CLIs: codex current, claude {OLD_CLAUDE} → {NEW_CLAUDE}"]
    assert vm.fetched == [clis.CLAUDE_URL.format(version=NEW_CLAUDE)]


@pytest.mark.parametrize("which", ["codex", "claude"])
def test_a_checksum_mismatch_changes_nothing(vm, which):
    """Either download wrong: neither CLI is touched, and nothing is left of the staging."""
    vm.codex(OLD_CODEX).claude(OLD_CLAUDE)
    if which == "codex":
        vm.codex_package = package(NEW_CODEX, executor=False)
    else:
        vm.claude_binary = program("tampered")
    before = vm.state()
    with pytest.raises(
        clis.Failed, match=r"SHA-256 [0-9a-f]{64}, not the pin's .*; nothing changed"
    ):
        vm.install()
    assert vm.state() == before
    assert clis.version_of(str(vm.bin / "codex")) == OLD_CODEX


def test_a_failed_download_or_a_listed_program_missing_changes_nothing(vm, monkeypatch):
    vm.codex(OLD_CODEX).claude(OLD_CLAUDE)
    before = vm.state()
    vm.pins = clis.Pins("0.170.0", vm.pins.codex_sha256, NEW_CLAUDE, vm.pins.claude_sha256)
    with pytest.raises(clis.Failed, match=r"download .*rust-v0\.170\.0.*: 404; nothing changed"):
        vm.install()
    assert vm.state() == before
    # the executor that codex-package.json lists isn't executable: refused before any change
    vm.codex_package = package(NEW_CODEX, executable=False)
    vm.pins = clis.Pins(NEW_CODEX, sha(vm.codex_package), NEW_CLAUDE, vm.pins.claude_sha256)
    with pytest.raises(clis.Failed, match="bin/codex-exec-server isn't an executable file"):
        vm.install()
    assert vm.state() == before


def test_the_links_are_swapped_by_one_rename(vm, monkeypatch):
    """`ln -sfn` on a temporary name, then `mv -T`: the link is never missing, and a link is
    what is replaced, never what it points to."""
    vm.codex(OLD_CODEX).claude(OLD_CLAUDE)
    renames = []
    real = os.replace

    def replace(src, dst):
        if os.path.basename(dst) in ("codex", "claude"):
            renames.append((os.path.basename(src), os.readlink(src), os.path.islink(dst)))
        return real(src, dst)

    monkeypatch.setattr(clis.os, "replace", replace)
    vm.install()
    pid = os.getpid()
    assert renames == [
        (f".codex.zab-{pid}", str(vm.lib / f"codex-{NEW_CODEX}" / "bin" / "codex"), True),
        (f".claude.zab-{pid}", str(vm.versions / NEW_CLAUDE), True),
    ]
    assert (vm.lib / f"codex-{OLD_CODEX}" / "bin" / "codex").is_file()  # not written through


def test_a_daemon_on_another_binary_is_reported(vm):
    vm.codex(OLD_CODEX).claude(NEW_CLAUDE)
    vm.daemon(4242, os.path.realpath(vm.lib / f"codex-{OLD_CODEX}" / "bin" / "codex"))
    vm.daemon(4343, "/usr/local/bin/codex (deleted)")
    vm.daemon(4444, "/opt/editor/codex", unit=False)  # an editor's app-server isn't zab's
    lines = vm.install()
    assert lines[1:] == [f"{clis.DAEMON_MARK}{OLD_CODEX}", f"{clis.DAEMON_MARK}another binary"]
    # one on the installed binary is not; nor is another program
    shutil.rmtree(vm.proc)
    vm.proc.mkdir()
    vm.daemon(4242, os.path.realpath(vm.lib / f"codex-{NEW_CODEX}" / "bin" / "codex"))
    (vm.proc / "77").mkdir()
    (vm.proc / "77" / "cmdline").write_bytes(b"/usr/bin/python3\0app-server\0")
    (vm.proc / "77" / "exe").symlink_to("/usr/bin/python3")
    assert vm.install() == [clis.CURRENT]


def test_an_editors_app_server_is_not_the_daemon(vm):
    """Review round 2, R2: Codex replaced, and only another client's app-server runs on the
    old package: no mark, so the row says nothing of a daemon and --restart-codex runs."""
    vm.codex(OLD_CODEX).claude(NEW_CLAUDE)
    vm.daemon(4444, os.path.realpath(vm.lib / f"codex-{OLD_CODEX}" / "bin" / "codex"), unit=False)
    assert vm.install() == [f"agent CLIs: codex {OLD_CODEX} → {NEW_CODEX}, claude current"]


def test_a_failure_after_the_codex_switch_says_so(vm, monkeypatch):
    """Review round 2, R1: Claude's placement failing after Codex's switch says that Codex was
    installed, never that nothing changed; and the bound is reset when `install` returns (R3)."""
    vm.codex(OLD_CODEX).claude(OLD_CLAUDE)

    def full(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(clis, "place_claude", full)
    with pytest.raises(
        clis.Failed, match=r"^claude: .*No space left.*; codex 0\.169\.1 installed$"
    ):
        vm.install()
    assert clis.version_of(str(vm.bin / "codex")) == NEW_CODEX
    assert clis.version_of(str(vm.bin / "claude")) == OLD_CLAUDE
    assert clis._deadline == math.inf


def test_a_current_vm_reports_no_daemon(vm):
    """Review B1: Codex not replaced, so its unit is as it was and nothing restarts: no mark,
    whatever app-servers run, zab's on an older package or an editor's."""
    vm.codex(NEW_CODEX).claude(OLD_CLAUDE)
    vm.daemon(4242, os.path.realpath(vm.lib / f"codex-{NEW_CODEX}" / "bin" / "codex"))
    vm.daemon(4343, "/opt/editor/codex", unit=False)
    vm.daemon(4444, "/usr/local/bin/codex")
    assert vm.install() == [f"agent CLIs: codex current, claude {OLD_CLAUDE} → {NEW_CLAUDE}"]
    assert vm.install() == [clis.CURRENT]


def test_nothing_is_switched_past_the_bound(vm, monkeypatch):
    """Review N3: past `--deadline-s` the step switches nothing, so a run that push_config
    stopped waiting for (it waits longer) never switches the links later; and each download
    is cut off when it stalls, within the bound."""
    vm.codex(OLD_CODEX).claude(OLD_CLAUDE)
    before, clock = vm.state(), [1000.0]
    monkeypatch.setattr(clis, "_now", lambda: clock[0])
    fetch = vm.fetch

    def slow(url: str, dest: str) -> None:
        fetch(url, dest)
        if "claude" in url:
            clock[0] += clis.DEADLINE_S  # the last download ends at the bound

    monkeypatch.setattr(clis, "fetch", slow)
    with pytest.raises(clis.Failed, match="the switch: past the step's 720 s; nothing changed"):
        vm.install()
    assert vm.state() == before
    calls = []
    monkeypatch.setattr(clis, "_run", lambda argv, what, timeout: calls.append((argv, timeout)))
    monkeypatch.setattr(clis, "_deadline", clock[0] + 300)
    REAL_FETCH("https://example.org/x", "/tmp/x")  # noqa: S108 - not written: _run is a stub
    argv, timeout = calls[0]
    for flag, value in (("--speed-limit", "102400"), ("--speed-time", "60"),
                        ("--max-time", "300"), ("--retry-max-time", "300"),
                        ("--connect-timeout", "30")):  # fmt: skip
        assert argv[argv.index(flag) + 1] == value, flag
    assert timeout == 300
    assert pc.AGENT_CLIS_DEADLINE_S == clis.DEADLINE_S < pc.AGENT_CLIS_TIMEOUT_S < 1800


def test_main_says_a_failure_in_one_line_and_exits_1(vm, capsys):
    vm.codex(OLD_CODEX).claude(OLD_CLAUDE)
    argv = ["install", "--codex", NEW_CODEX, "0" * 64, "--claude", NEW_CLAUDE, "1" * 64]
    assert clis.main([*argv, "--home", vm.layout.home]) == 1
    out, err = capsys.readouterr()
    assert not out and err.startswith("agent CLIs: failed: codex 0.169.1: SHA-256 ")
    assert err.strip().endswith("; nothing changed") and len(err.strip().splitlines()) == 1
    argv[2] = "latest"
    assert clis.main([*argv, "--home", vm.layout.home]) == 1
    assert capsys.readouterr().err == "agent CLIs: failed: not a pinned version: 'latest'\n"


def test_read_pins_takes_the_four_strings_and_nothing_else():
    source = (ROOT / "src/zab/pins.py").read_text(encoding="utf-8")
    assert clis.read_pins(source) == clis.Pins(
        zab_pins.CODEX_VERSION, zab_pins.CODEX_PACKAGE_SHA256,
        zab_pins.CLAUDE_VERSION, zab_pins.CLAUDE_SHA256,
    )  # fmt: skip
    for bad in (
        source.replace('CLAUDE_SHA256 = "', 'CLAUDE_SHA = "'),
        source.replace(f'"{zab_pins.CODEX_VERSION}"', "'latest'"),
        source.replace(f'"{zab_pins.CODEX_VERSION}"', "VERSION"),  # not a literal
        "CODEX_VERSION = (",
    ):
        with pytest.raises(ValueError):
            clis.read_pins(bad)


# ------------------------------------------------------------------- the pins in one place


def test_the_pins_agree_everywhere():
    """src/zab/pins.py is the release's; the doctor's pins, build_ami.py's defaults and build.sh's
    (which runs on the builder without this checkout's Python) are the same."""
    pins = clis.read_pins((ROOT / "src/zab/pins.py").read_text(encoding="utf-8"))
    assert (codex_backend.PIN, claude_backend.PINNED_VERSION) == (pins.codex, pins.claude)
    assert ba.PINS == pins
    source = pathlib.Path(ba.__file__).read_text(encoding="utf-8")
    assert "CODEX_VERSION = PINS.codex\n" in source  # --codex-version's default, #1064's
    assert 'parser.add_argument("--codex-version", default=CODEX_VERSION)' in source
    assert 'parser.add_argument("--claude-version", default=PINS.claude)' in source
    text = ba.BUILD_SH.read_text()
    assert f'CODEX_VERSION="${{CODEX_VERSION:-{pins.codex}}}"' in text
    assert f'CLAUDE_VERSION="${{CLAUDE_VERSION:-{pins.claude}}}"' in text
    assert f'CODEX_PKG_SHA256="${{CODEX_PKG_SHA256:-{pins.codex_sha256}}}"' in text
    assert f"(default {pins.codex}," in text and f"(default {pins.claude}," in text
    # the same download as build.sh's, from the same release
    url = re.search(r'^CODEX_URL="(.+)"$', text, re.MULTILINE)
    assert url and url.group(1).replace("${CODEX_VERSION}", "{version}") == clis.CODEX_URL
    # build.sh's members, the core ones among them (#1064 adds the listed bin/ programs there)
    assert all(member in text for member in clis.CODEX_MEMBERS)
    decisions = (ROOT / "docs/DECISIONS.md").read_text(encoding="utf-8")
    assert f"| Codex CLI | {pins.codex}, " in decisions
    assert f"| Claude Code | {pins.claude}, " in decisions


# --------------------------------------------------------------------- its step in push_config


PINS = clis.Pins(NEW_CODEX, "a" * 64, NEW_CLAUDE, "b" * 64)
STEP = "agent_clis.py install "
MOVED = f"agent CLIs: codex {OLD_CODEX} → {NEW_CODEX}, claude {OLD_CLAUDE} → {NEW_CLAUDE}\n"


def test_the_step_runs_after_the_release_and_before_the_services(out_dir):
    runner = FakeRunner(answers={STEP: MOVED})
    opts = pc.Options(tag="v0.2.22", out_dir=out_dir, known_hosts=KH, agent_pins=PINS)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok and result.notes == [MOVED.strip()]
    remotes = runner.remotes(ALICE)
    step = next(i for i, r in enumerate(remotes) if STEP in r)
    # after the release and its config.toml (#996), which don't depend on the CLIs: a failure
    # here, minutes of downloads, never leaves the new release with the old config
    assert " tool install --force " in remotes[step - 2]
    assert remotes[step - 1] == pc.install_files_cmd(STAGING, ("config.toml",))
    assert remotes[step + 1] == pc.SERVICE_INSTALL_CMD  # before the services and the doctor
    assert remotes[step] == (
        f"sudo chmod 0644 {STAGING}/agent_clis.py && sudo -u agent -H bash -lc "
        f"'cd / && /usr/bin/python3 {STAGING}/agent_clis.py install --codex {NEW_CODEX} "
        f"{'a' * 64} --claude {NEW_CLAUDE} {'b' * 64} --deadline-s 720'"
    )
    scps = [c[-1] for c in runner.calls if c[0] == "scp"]
    assert f"ubuntu@{ALICE}:{STAGING}/agent_clis.py" in scps
    # current: nothing to say
    runner = FakeRunner(answers={STEP: "agent CLIs: current\n"})
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok and not result.notes


def test_a_release_without_pins_leaves_the_clis_alone(out_dir):
    runner = FakeRunner()
    result = pc.rollout_vm(ALICE_ROW, pc.Options(tag="v0.2.21", out_dir=out_dir), runner)
    assert result.ok and not any(STEP in r for r in runner.remotes(ALICE))
    assert not any(c[-1].endswith("/agent_clis.py") for c in runner.calls if c[0] == "scp")


def test_a_failed_step_fails_the_row_and_the_rollout_goes_on(out_dir):
    why = "agent CLIs: failed: codex 0.169.1: SHA-256 0123, not the pin's aaaa; nothing changed"
    runner = FakeRunner(fail={STEP: f"{why}\n"})
    opts = pc.Options(tag="v0.2.22", out_dir=out_dir, known_hosts=KH, agent_pins=PINS)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert not result.ok and result.failures == [why] and result.installed
    assert result.notes == [pc.AGENT_CLIS_KEPT]  # the release and its config in, the CLIs kept


def test_a_step_that_failed_partway_claims_nothing(out_dir):
    """Review round 2, R1: a failure after Codex's switch, or an ssh cut while the VM's run
    goes on, may have replaced some: the notes say so, and treat Codex as (maybe) replaced, so
    that --restart-codex's own restart, which would run the old package, is left out."""
    opts = pc.Options(tag="v0.2.22", out_dir=out_dir, known_hosts=KH, agent_pins=PINS)
    restart = dataclasses.replace(opts, restart_codex=True)
    tail = (
        " at the service install, which rewrites its unit to the new package (a Codex turn "
        "running then ends); so --restart-codex's own restart isn't run"
    )
    for stderr, head in (
        (
            f"agent CLIs: failed: claude: [Errno 28] No space left; codex {NEW_CODEX} installed\n",
            f"codex {NEW_CODEX} installed: the daemon, on its previous version, restarts onto it",
        ),
        (
            "timed out after 900 s: ssh\n",
            f"codex {NEW_CODEX} may be installed: if so, the daemon restarts onto it",
        ),
    ):
        runner = StatusRunner(zab_status(("s1", "codex")))
        runner.fail[STEP] = stderr
        result = pc.rollout_vm(ALICE_ROW, restart, runner)
        assert not result.ok and len(result.failures) == 1
        assert result.notes == [pc.AGENT_CLIS_PARTLY, head + tail]
        sent = runner.remotes(ALICE)
        assert pc.SERVICE_INSTALL_CMD in sent and pc.CODEX_RESTART_CMD not in sent
    assert pc.SERVICE_INSTALL_CMD in runner.remotes(ALICE)  # the release's bridge all the same


def test_the_daemon_note_says_the_service_install_restarts_it(out_dir):
    """The daemon's unit names the package by its resolved path, so the service install after
    the step rewrites it and restarts a daemon still on the old one (tests/cli/test_service.py),
    a running Codex turn or not; the row says so. --restart-codex's own restart, which would
    run the old package and whose idle check can't hold, is left out then (review N1)."""
    moved = MOVED + f"{clis.DAEMON_MARK}{OLD_CODEX}\n"
    note = (
        f"codex {NEW_CODEX} installed: the daemon, on {OLD_CODEX}, restarts onto it at the service "
        "install, which rewrites its unit to the new package (a Codex turn running then ends)"
    )
    opts = pc.Options(tag="v0.2.22", out_dir=out_dir, known_hosts=KH, agent_pins=PINS)
    runner = FakeRunner(answers={STEP: moved})
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.notes == [MOVED.strip(), note]
    remotes = runner.remotes(ALICE)
    assert remotes.index(pc.SERVICE_INSTALL_CMD) == 1 + next(
        i for i, r in enumerate(remotes) if STEP in r
    )
    restart = dataclasses.replace(opts, restart_codex=True)
    for status in (zab_status(("s1", "codex")), zab_status()):  # busy, idle
        runner = StatusRunner(status)
        runner.answers[STEP] = moved
        result = pc.rollout_vm(ALICE_ROW, restart, runner)
        assert result.notes == [
            MOVED.strip(),
            note + "; so --restart-codex's own restart isn't run",
        ]
        sent = runner.remotes(ALICE)
        assert not {pc.BRIDGE_STOP_CMD, pc.CODEX_STATUS_CMD, pc.CODEX_RESTART_CMD} & set(sent)
    # no daemon on another binary: nothing to say, and --restart-codex as before
    result = pc.rollout_vm(ALICE_ROW, opts, FakeRunner(answers={STEP: MOVED}))
    assert result.notes == [MOVED.strip()]
    idle = StatusRunner(zab_status())
    idle.answers[STEP] = MOVED
    result = pc.rollout_vm(ALICE_ROW, restart, idle)
    assert result.notes == [MOVED.strip(), pc.CODEX_RESTARTED]
    # review B1: Codex not replaced, a mark or not: no note, and nothing restarts for it
    stray = f"agent CLIs: current\n{clis.DAEMON_MARK}another binary\n"
    result = pc.rollout_vm(ALICE_ROW, opts, FakeRunner(answers={STEP: stray}))
    assert result.ok and result.notes == []


def test_the_dry_run_names_the_step_and_the_pins(roster, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(pc, "release_pins", lambda wheel, tag, runner=None: PINS)
    args = ["--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.2.22", "--dry-run"]
    assert pc.main(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert pc.agent_clis_line(PINS) in out
    assert pc.agent_clis_line(PINS).startswith(
        f"agent CLIs: codex {NEW_CODEX}, claude {NEW_CLAUDE}, the release's pins"
    )
    dry = next(line for line in out if line.startswith("dry-run alice"))
    assert (
        "; zab installed; config installed; scp agent_clis.py; agent CLIs; service install" in dry
    )
    assert not any("note: agent CLIs" in line for line in out)  # no VM was looked at
    monkeypatch.setattr(pc, "release_pins", lambda wheel, tag, runner=None: None)
    assert pc.main(args) == 0
    out = capsys.readouterr().out
    assert pc.agent_clis_line(None) in out.splitlines() and "agent CLIs;" not in out


def test_the_pins_are_the_release_s(tmp_path, monkeypatch, capsys):
    """Read from the wheel or the tag being installed, never this checkout: none in an older
    release, and a broken file stops the rollout."""
    monkeypatch.setattr(pc, "release_pins", REAL_RELEASE_PINS)  # not the autouse stub
    source = (ROOT / "src/zab/pins.py").read_text(encoding="utf-8")
    wheel = tmp_path / "zulip_agent_bridge-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as whl:
        whl.writestr("zab/pins.py", source)
    assert pc.release_pins(str(wheel), "") == clis.read_pins(source)
    older = tmp_path / "older.whl"
    with zipfile.ZipFile(older, "w") as whl:
        whl.writestr("zab/config.py", "")
    assert pc.release_pins(str(older), "") is None
    tag = lambda argv, **kw: pc.RunResult(0, source, "")  # noqa: E731
    assert pc.release_pins(None, "v0.2.22", tag) == clis.read_pins(source)
    gone = lambda argv, **kw: pc.RunResult(128, "", "fatal: path does not exist")  # noqa: E731
    assert pc.release_pins(None, "v0.2.21", gone) is None
    broken = tmp_path / "broken.whl"
    with zipfile.ZipFile(broken, "w") as whl:
        whl.writestr("zab/pins.py", source.replace("CLAUDE_SHA256", "CLAUDE_SUM"))
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    assert pc.main(["--roster", "r.csv", "--wheel", str(broken), "--dry-run"]) == 2
    assert "the release's zab/pins.py: has no string CLAUDE_SHA256" in capsys.readouterr().err
