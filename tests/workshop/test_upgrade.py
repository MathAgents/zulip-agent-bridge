"""`workshop/adminserver/upgrade.py` (#772), offline: its configuration, the check of its own
copy, the preflight, the agent it signs with, and the phase runner. Real git and a real
ssh-agent, with throwaway keys, on the machine of `upgrade_rig`."""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from .upgrade_rig import DRIVER, ROOT, Machine, missing_tools

pytestmark = pytest.mark.skipif(bool(missing_tools()), reason=f"needs {missing_tools()}")
SHA = "ab" * 32


@pytest.fixture
def machine(tmp_path):
    m = Machine(tmp_path)
    yield m
    m.close()


def said(done: subprocess.CompletedProcess) -> str:
    return done.stdout + done.stderr


def upgrade(machine: Machine, version="v0.3.1", *, sha=True, input=None, **kw):
    """A release `version` on main, then the driver of v0.3.0's signed copy towards it, with
    the review's SHA-256 (or with `sha` False, one that a run stopped earlier never compares),
    typing the version to sign."""
    commit = machine.release(version)
    review = machine.review_sha256(commit) if sha else SHA
    typed = f"{version}\n" if input is None else input
    done = machine.run(version, "--commit", commit, "--review-sha256", review, input=typed, **kw)
    return done, commit


# --------------------------------------------------------------------------- configuration


def test_setup_asks_once_and_keeps_the_values(machine):
    machine.conf.unlink()
    rule = machine.conf.with_name("ssh-rule.conf")
    rule.write_text(f"profile=p\nregion=us-east-1\nkey={machine.key}\n")
    done = machine.run("--setup", input="\n\n\n")  # each default: ssh-rule.sh's key, ~/zab …
    assert done.returncode == 0, said(done)
    assert machine.conf.read_text() == (
        f"key={machine.key}\nclone={machine.clone}\nadmin={machine.admin}\n"
    )
    assert stat.S_IMODE(machine.conf.stat().st_mode) == 0o600
    assert "the dedicated key's file, with its .pub beside it (BOOTSTRAP §2.1) [" in done.stdout
    done = machine.run("--setup", input=f"{machine.other}\n\n\n")  # a new answer wins
    assert done.returncode == 0, said(done)
    assert machine.conf.read_text().startswith(f"key={machine.other}\n")


@pytest.mark.parametrize(
    ("answers", "why"),
    [("/nowhere\n\n\n", "no /nowhere.pub"), ("\n/nowhere\n\n", "clone: no directory /nowhere"),
     ("\nrelative/zab\n\n", "clone: an absolute path without blanks")],
)  # fmt: skip
def test_setup_refuses_what_is_not_there(machine, answers, why):
    before = machine.conf.read_text()
    done = machine.run("--setup", input=answers)
    assert done.returncode == 1 and f"stopped: {why}" in done.stderr, said(done)
    assert machine.conf.read_text() == before


@pytest.mark.parametrize("text", [None, "key=/k\n", "key=/k\nclone=/c\nadmin=/a\nhost=h\n"])
def test_a_missing_or_partial_config_says_to_run_setup(machine, text):
    machine.conf.unlink() if text is None else machine.conf.write_text(text)
    done, _ = upgrade(machine, sha=False)
    assert done.returncode == 1, said(done)
    assert "--setup" in done.stderr and done.stderr.startswith("stopped: ")


def test_setup_rewrites_a_file_with_a_line_it_doesnt_take(machine):
    """An unknown line stops a run, and says so; --setup offers the rest, and drops it."""
    machine.conf.write_text(machine.conf.read_text() + "host=h\n")
    done = machine.run("--setup", input="\n\n\n")
    assert done.returncode == 0, said(done)
    assert machine.conf.read_text() == (
        f"key={machine.key}\nclone={machine.clone}\nadmin={machine.admin}\n"
    )


# ------------------------------------------------------------------------- the preflight


def test_the_preflight_passes_from_the_signed_copy(machine):
    copy = machine.copy("v0.3.0")
    done, commit = upgrade(machine, copy=copy)
    assert done.returncode == 0, said(done)
    out = done.stdout
    assert f"this copy: v0.3.0, signed, at {copy}\n" in out
    assert f"v0.3.1: {commit}, on origin/main, after v0.3.0\n" in out
    assert "this terminal's agent doesn't hold the key alone: the driver uses its own\n" in out
    assert f"key: {machine.fingerprint(machine.key)}, alone in the agent\n" in out
    transcript = (machine.admin / "upgrades/v0.3.1/transcript.txt").read_text()
    heads = ("this copy: ", "v0.3.1: ", "key:")
    ours = [line for line in out.splitlines() if line.startswith(heads)]
    assert len(ours) == 3 and all(f"{line}\n" in transcript for line in ours)
    assert "\n$ git -C " in transcript  # and the commands, which the output doesn't show
    sock = re.search(r"^\$ ssh-agent -D -a (\S+)$", transcript, re.M)[1]
    assert not Path(sock).parent.exists()  # its agent stopped, its directory removed


def test_the_terminals_agent_is_used_when_it_holds_the_key_alone(machine):
    copy = machine.copy("v0.3.0")
    done, _ = upgrade(machine, copy=copy, env=machine.agent(machine.key))
    assert done.returncode == 0, said(done)
    assert "uses its own" not in done.stdout and "ssh-add" not in done.stdout
    commit = machine.git("rev-parse", "HEAD")
    done = machine.run("v0.3.1", "--commit", commit, "--review-sha256",
                       machine.review_sha256(commit), copy=copy,
                       env=machine.agent(machine.key, machine.other))  # fmt: skip  # all done
    assert done.returncode == 0 and "the driver uses its own" in done.stdout, said(done)


def test_a_protected_key_is_added_with_its_passphrase(machine):
    """ssh-add asks for it, here through an askpass stand-in, as the terminal would."""
    machine.keygen(machine.tmp / "locked", passphrase="correct horse")  # noqa: S106
    machine.conf.write_text(f"key={machine.tmp / 'locked'}\nclone={machine.clone}\n"
                            f"admin={machine.admin}\n")  # fmt: skip
    (machine.etc / "allowed_signers").write_text(
        machine.signers_line(machine.key) + machine.signers_line(machine.tmp / "locked")
    )
    askpass = machine.tmp / "askpass"
    askpass.write_text(f"#!/bin/sh\necho asked >> {machine.tmp / 'asked'}\necho correct horse\n")
    askpass.chmod(0o755)
    env = {"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"}
    done, _ = upgrade(machine, copy=machine.copy("v0.3.0"), env=env)
    assert done.returncode == 0, said(done)
    assert (machine.tmp / "asked").read_text() == "asked\n"


def copy_with(machine: Machine, case: str):
    """A copy of v0.3.0 that the driver must refuse, as `case` says."""
    if case == "the clone's checkout":  # at main, after v0.3.0
        machine.release("v0.3.1rc1")
        machine.git("fetch", "-q", "origin", where=machine.clone)
        machine.git("checkout", "-q", "--detach", "origin/main", where=machine.clone)
        return None
    if case != "a change":
        machine.git("tag", "-d", "v0.3.0")
        commit = machine.git("rev-parse", "HEAD")
        if case == "another key":
            machine.sign("v0.3.0", commit, machine.other)
        else:
            machine.git("tag", *(["-a", "-m", "m"] if case == "unsigned" else []), "v0.3.0")
        machine.git("tag", "-d", "v0.3.0", where=machine.clone)
    copy = machine.copy("v0.3.0")
    if case == "a change":
        (copy / "README.md").write_text("changed\n")
    return copy


@pytest.mark.parametrize(
    ("case", "why"),
    [("the clone's checkout", "no release tag names"), ("a change", "it has changes"),
     ("another key", "v0.3.0: not signed by a key of"),
     ("unsigned", "v0.3.0: not signed with an SSH key"),
     ("lightweight", "v0.3.0: a lightweight tag, which has no signature")],
)  # fmt: skip
def test_any_other_copy_is_refused_with_the_lines_that_make_the_right_one(machine, case, why):
    copy = copy_with(machine, case)
    done, commit = upgrade(machine, copy=copy, sha=case == "the clone's checkout")
    assert done.returncode == 1, said(done)
    assert done.stderr.startswith(f"stopped: {copy or machine.clone}: {why}"), said(done)
    assert "Run the copy of the release the server runs, which its signed tag makes:\n" in (
        done.stderr
    )
    assert "  V=<the release the server runs, as !version shows it>\n" in done.stderr
    if case != "the clone's checkout":
        return
    lines = done.stderr.split("as !version shows it>\n", 1)[1].splitlines()
    script = "V=v0.3.0\n" + "\n".join(line.strip() for line in lines)
    pasted = machine.sh("bash", "-c", script, env=machine.agent(machine.key), input="v0.3.1\n")
    assert pasted.returncode == 0, said(pasted)
    assert f"v0.3.1: {commit}, on origin/main, after v0.3.0\n" in pasted.stdout


def test_a_tag_of_ones_own_beside_origins_is_kept_and_new_tags_come(machine):
    """BOOTSTRAP §9: from the public repository, the clone's v0.3.0 is the deployer's own
    signature, and origin's the maintainer's, on the same commit. `git fetch --tags` would
    refuse to clobber it and exit 1; the driver fetches origin's tags aside, keeps the clone's,
    and takes the tags it lacks."""
    commit = machine.git("rev-parse", "v0.3.0^{commit}")
    machine.git("tag", "-d", "v0.3.0")
    machine.sign("v0.3.0", commit, machine.other)  # origin's: the maintainer's key
    mine = machine.git("rev-parse", "v0.3.0", where=machine.clone)
    machine.git("tag", "v0.3.1rc1", commit)  # a tag the clone lacks
    copy = machine.home / "zab-v0.3.0"  # the clone's own v0.3.0, no fetch
    machine.git("worktree", "add", "-q", "--detach", copy, "v0.3.0", where=machine.clone)
    assert machine.sh("git", "-C", machine.clone, "fetch", "origin", "--tags").returncode == 1
    done, _ = upgrade(machine, copy=copy, sha=False)  # the review stops at the SHA-256 (part 3)
    assert "stopped: this review's diff has SHA-256 " in done.stderr, said(done)
    assert f"this copy: v0.3.0, signed, at {copy}\n" in done.stdout
    assert machine.git("rev-parse", "v0.3.0", where=machine.clone) == mine  # kept
    assert machine.git("rev-parse", "v0.3.1rc1^{commit}", where=machine.clone) == commit
    machine.origin.rename(machine.tmp / "gone")  # a real failure still stops
    done = machine.run("v0.3.1", "--commit", commit, "--review-sha256", SHA, copy=copy)
    assert done.returncode == 1 and "stopped: git fetch origin: " in done.stderr, said(done)


def test_a_tag_whose_pyproject_says_another_version_doesnt_verify(machine):
    """deploy.sh's verify_tag checks it, and so does the driver: a v0.2.9 signed on v0.3.0's
    commit is not a second release tag of the copy's commit."""
    machine.sign("v0.2.9", machine.git("rev-parse", "v0.3.0^{commit}"), machine.key)
    copy = machine.copy("v0.3.0")
    done, commit = upgrade(machine, copy=copy)
    assert done.returncode == 0 and f"this copy: v0.3.0, signed, at {copy}\n" in done.stdout, said(
        done
    )
    machine.git("tag", "-d", "v0.3.0", where=machine.clone)  # v0.2.9 alone names the commit
    done = machine.run("v0.3.1", "--commit", commit, "--review-sha256", SHA, copy=copy)
    assert done.returncode == 1, said(done)
    assert f"stopped: {copy}: v0.2.9: its pyproject.toml says version 0.3.0.\n" in done.stderr


@pytest.mark.parametrize("case", ["off main", "other version", "not newer", "before vP"])
def test_the_commit_must_be_vn_on_main_after_this_copy(machine, case):
    copy = machine.copy("v0.3.0")
    first = machine.git("rev-list", "--max-parents=0", "HEAD")
    version, commit = "v0.3.1", ""
    if case == "off main":
        commit = machine.release("v0.3.1", parent="main")
    elif case == "other version":
        commit = machine.release("v0.3.2")
    elif case == "not newer":
        version, commit = "v0.3.0rc9", machine.release("v0.3.0rc9")
    else:  # main's first commit, under v0.3.0's tag, rewritten as if it said 0.3.1
        commit = machine.git("commit-tree", "-m", "early", f"{first}^{{tree}}")
        machine.git("update-ref", "refs/heads/main", machine.git(
            "commit-tree", "-p", commit, "-m", "v0.3.0 again", "v0.3.0^{tree}"))  # fmt: skip
    machine.git("tag", "-f", "z", commit)  # so that the clone's fetch brings it
    done = machine.run(version, "--commit", commit, "--review-sha256", SHA, copy=copy)
    assert done.returncode == 1, said(done)
    why = {"off main": "is not on origin/main", "other version": "pyproject.toml says version",
           "not newer": "is not newer than v0.3.0",
           "before vP": "v0.3.0 is not an ancestor of"}[case]  # fmt: skip
    assert why in done.stderr, said(done)


# ----------------------------------------------------------------------- agent and runner


def load(monkeypatch):
    spec = importlib.util.spec_from_file_location("upgrade_under_test", ROOT / DRIVER)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # for its dataclass, until the test ends
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver(machine, monkeypatch):
    """The driver's module, in this process, on the machine's HOME, without its preflight."""
    for name, value in machine.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    module = load(monkeypatch)
    monkeypatch.setattr(module.Upgrade, "preflight", lambda self: None)
    return module


def test_the_agent_must_list_the_fingerprint_itself(driver, machine, monkeypatch):
    """The listing's second field, not a substring of the line: a comment that names the
    fingerprint isn't the key."""
    up = driver.Upgrade("v0.3.1", "a" * 40, SHA)
    up.agent.fingerprint = machine.fingerprint(machine.key)
    for line, holds in ((f"256 {up.agent.fingerprint} me (ED25519)", True),
                        (f"256 SHA256:other {up.agent.fingerprint} (ED25519)", False)):  # fmt: skip
        listing = subprocess.CompletedProcess([], 0, line + "\n", "")
        monkeypatch.setattr(up, "call", lambda argv, listing=listing, **kw: listing)
        assert up.agent.holds_key_alone() is holds


def test_a_key_whose_lifetime_lapsed_is_added_again(driver, machine, capsys):
    up = driver.Upgrade("v0.3.1", "a" * 40, SHA)
    try:
        up.agent.ensure()
        sock = up.agent.env()["SSH_AUTH_SOCK"]
        machine.sh("ssh-add", "-D", env={"SSH_AUTH_SOCK": sock})  # as a lapsed lifetime would
        up.agent.ensure()
        assert up.agent.holds_key_alone()
    finally:
        up.finish()
    assert capsys.readouterr().out.count(f"ssh-add {machine.key}, for 4h,") == 2


#: an ssh-agent whose socket's file exists half a second before it listens, as a loaded
#: machine can delay the real one's listen(2) after its bind(2)
SLOW_AGENT = """
import socket, sys, time
sock = socket.socket(socket.AF_UNIX)
sock.bind(sys.argv[sys.argv.index("-a") + 1])
time.sleep(0.5)
sock.listen()
time.sleep(60)
"""


def test_the_drivers_agent_is_used_once_it_listens(driver, machine, monkeypatch):
    """Not as soon as its socket's file exists: ssh-add is refused until it listens (#920)."""
    bindir = machine.tmp / "bin"
    bindir.mkdir()
    (bindir / "ssh-agent").write_text(f"#!{sys.executable}\n{SLOW_AGENT}")
    (bindir / "ssh-agent").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    up = driver.Upgrade("v0.3.1", "a" * 40, SHA)
    try:
        up.agent.start()
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(up.agent.env()["SSH_AUTH_SOCK"])  # refused if it isn't listening
    finally:
        up.finish()


@pytest.mark.parametrize("ignores", [False, True])
def test_a_signal_during_a_command_reaches_it_and_the_run_ends(driver, machine, monkeypatch,
                                                               ignores):  # fmt: skip
    """SIGTERM sent to the driver alone, as `kill` does (a terminal's Ctrl-C reaches the command
    too): passed on to the command, which has END_WAIT seconds to end, then is killed."""
    monkeypatch.setattr(driver, "END_WAIT", 0.5)
    up = driver.Upgrade("v0.3.1", "a" * 40, SHA)
    mark = machine.tmp / "got"
    trap = "trap '' TERM" if ignores else f"trap 'echo TERM > {mark}; kill $!; exit 3' TERM"
    held = signal.signal(signal.SIGTERM, up.interrupted)
    threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            up.call(["sh", "-c", f"{trap}; sleep 5 & wait"])
    finally:
        signal.signal(signal.SIGTERM, held)
    took = time.monotonic() - start
    assert up.signal == signal.SIGTERM and took < 4, took  # not the command's 5 s
    if ignores:
        assert took >= 0.8 and not mark.exists()  # 0.3, then END_WAIT, then killed
    else:
        assert mark.read_text() == "TERM\n"


def phases(driver, log: list[str], fail_at: str = ""):
    """Three phases: the second done once the first ran; `fail_at`'s run stops."""

    def step(name):
        def run():
            log.append(name)
            if name == fail_at:
                raise driver.Stop(f"{name} failed")

        return run

    return lambda self: [
        driver.Phase("one", step("one"), lambda: "already" if "one" in log else ""),
        driver.Phase("two", step("two"), lambda: ""),
        driver.Phase("checks", step("checks")),
    ]


ARGV = ["v0.3.1", "--commit", "a" * 40, "--review-sha256", SHA]


@pytest.mark.parametrize("fail_at", ["", "two"])
def test_the_runner_skips_what_is_done_and_a_rerun_goes_on(driver, machine, monkeypatch, capsys,
                                                           fail_at):  # fmt: skip
    log: list[str] = []
    monkeypatch.setattr(driver.Upgrade, "make_phases", phases(driver, log, fail_at))
    code = driver.main(ARGV)
    out = capsys.readouterr()
    assert out.out.startswith("plan:\n  1. one: to do\n  2. two: to do\n  3. checks: every run\n")
    if not fail_at:
        assert code == 0 and log == ["one", "two", "checks"]
        assert "[3/3] checks\ndone: v0.3.1 is signed and verified on aaaaaaaaaaaa" in out.out
        return
    assert code == 1 and log == ["one", "two"] and out.err == "stopped: two failed\n"
    assert driver.main(ARGV) == 1 and log == ["one", "two", "two"]  # one is done: skipped
    assert "  1. one: done (already)\n" in capsys.readouterr().out
    transcript = (machine.admin / "upgrades/v0.3.1/transcript.txt").read_text()
    assert transcript.count("stopped: two failed\n") == 2 and "[1/3] one: done (already)\n" in (
        transcript
    )


@pytest.mark.parametrize("how", ["stop", "interrupt", "terminate"])
def test_the_clean_ups_run_on_every_exit(driver, monkeypatch, capsys, how):
    """A Stop, a Ctrl-C, or SIGTERM as a closed terminal's SIGHUP would be: each clean-up runs,
    last first, and the handlers of the signals are put back."""
    log: list[str] = []
    before = [signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)]

    def fail():
        if how == "stop":
            raise driver.Stop("no")
        if how == "interrupt":
            raise KeyboardInterrupt
        os.kill(os.getpid(), signal.SIGTERM)

    def cleanup_failing():
        log.append("failing")
        raise OSError("gone")

    def make(self):
        self.cleanups += [lambda: log.append("first"), cleanup_failing]
        return [driver.Phase("one", fail)]

    monkeypatch.setattr(driver.Upgrade, "make_phases", make)
    monkeypatch.setattr(driver.Agent, "close", lambda self: log.append("agent"))
    assert driver.main(ARGV) == (1 if how == "stop" else 130)
    assert log == ["failing", "first", "agent"]
    assert "clean-up: gone\n" in capsys.readouterr().err
    assert [signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)] == before
