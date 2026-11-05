"""Test releases, offline with real git and ssh-keygen (issue #356): version commits, signed
and unsigned tags, a renamed ref, the version order, and how deploy.sh is called."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from e2e.admin.harness import rig as rigs
from e2e.admin.harness.gate import Gate
from e2e.admin.harness.release import (
    Releases,
    ensure_keys,
    key,
    local,
    next_version,
    older_version,
    typed,
    write_ssh,
)
from e2e.harness.vm import Result

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("ssh-keygen")), reason="needs git and ssh-keygen"
)


def git(repo, *args, check=True):
    argv = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args]
    return subprocess.run(argv, capture_output=True, text=True, check=check)  # noqa: S603


@pytest.fixture
def made(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "src" / "zab").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "zab"\nversion = "0.2.3"\n')
    (checkout / "src" / "zab" / "__init__.py").write_text('"""zab."""\n\n__version__ = "0.2.3"\n')
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # noqa: S603, S607
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "base")
    rig = rigs.load({"HOME": str(tmp_path)})
    ensure_keys(Gate(dry_run=False, out=lambda line: None), rig)
    rel = Releases(rig, checkout=checkout)
    rel.ensure_repo()
    signers = tmp_path / "allowed_signers"
    pub = (rig.keys / "signing.pub").read_text().strip()
    signers.write_text(f'e2e@example.org namespaces="git" {pub}\n')
    return rel, rel.commit_of("HEAD"), signers


def verify(rel, signers, version):
    return git(rel.rig.repo, "-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-tag",
               version, check=False)  # fmt: skip


def test_a_signed_release_of_the_commit_under_test(made):
    rel, base, signers = made
    assert rel.base_version(base) == "0.2.3"
    version = next_version("0.2.3", None)
    assert version == "v0.2.4rc1"
    commit = rel.release(base, version)
    assert rel.git("rev-parse", f"{commit}^") == base
    assert 'version = "0.2.4rc1"' in rel.git("show", f"{commit}:pyproject.toml")
    assert '__version__ = "0.2.4rc1"' in rel.git("show", f"{commit}:src/zab/__init__.py")
    changed = rel.git("diff", "--name-only", base, commit).split()
    assert changed == ["pyproject.toml", "src/zab/__init__.py"]  # the version only
    assert verify(rel, signers, version).returncode == 0
    assert rel.has_tag(version) and not rel.has_tag("v9.9.9")
    rel.release(base, version)  # an earlier run's tag of that name is replaced
    assert verify(rel, signers, version).returncode == 0
    assert git(rel.checkout, "tag").stdout == ""  # no test tag in the zab repository


def test_the_bad_tags_of_a10(made):
    rel, base, signers = made
    rel.release(base, "v0.2.4rc1", signer=None)
    assert "BEGIN SSH SIGNATURE" not in rel.git("cat-file", "tag", "v0.2.4rc1")
    rel.release(base, "v0.2.4rc1", signer="unknown")
    assert "BEGIN SSH SIGNATURE" in rel.git("cat-file", "tag", "v0.2.4rc1")
    assert verify(rel, signers, "v0.2.4rc1").returncode != 0
    rel.release(base, "v0.2.4rc1")
    obj = rel.git("rev-parse", "refs/tags/v0.2.4rc1")
    rel.alias("v0.2.4rc2", obj)
    assert rel.git("rev-parse", "refs/tags/v0.2.4rc2") == obj
    assert "\ntag v0.2.4rc1\n" in "\n" + rel.git("cat-file", "tag", "v0.2.4rc2")


def test_versions_follow_deploy_sh_order():
    assert key("v0.2.4rc9") < key("v0.2.4") < key("v0.2.5rc1")
    assert next_version("0.2.3", "v0.2.4rc1") == "v0.2.4rc2"
    assert next_version("0.2.3", "v0.2.4") == "v0.2.5rc1"
    assert next_version("0.3.0rc2", None) == "v0.3.0rc3"
    assert next_version("0.3.0", "v0.2.4rc7") == "v0.3.1rc1"  # a newer commit: its own
    assert older_version("v0.2.4rc2") == "v0.2.4rc1"
    assert older_version("v0.2.4") == "v0.2.4rc0"
    assert older_version("v0.2.4rc0") == "v0.2.3"
    with pytest.raises(ValueError):
        older_version("v0.2.0rc0")
    with pytest.raises(ValueError):
        key("main")


def test_deploy_sh_is_called_as_a_deployer_would(tmp_path):
    rig = rigs.load({"HOME": str(tmp_path)})
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        err = "  SHA-256 " + "a" * 64 + "; release tree " + "b" * 64 + "\n"
        return Result(1, "", err + "deploy.sh: v0.2.4rc1: not signed with an SSH key\n")

    rel = Releases(rig, type_=run)
    done = rel.deploy(tmp_path / "deploy.sh", "v0.2.4rc1", "--bootstrap", out=tmp_path)
    argv, kw = calls[0]
    assert argv[:2] == ["bash", str(tmp_path / "deploy.sh")] and argv[-1] == "v0.2.4rc1"
    assert argv[argv.index("--host") + 1] == "zab-test-admin"
    assert argv[argv.index("--secrets") + 1] == str(rig.secrets)
    assert kw["answer"] == "v0.2.4rc1\n"  # typed at deploy.sh's confirmation, when it asks
    assert kw["prompt"] == "Type v0.2.4rc1 to "
    assert kw["env"]["ZAB_DEPLOY_SSH"] == str(rig.ssh_wrapper)
    assert done.diff_sha256 == "a" * 64 and not done.ok
    assert done.why() == "deploy.sh: v0.2.4rc1: not signed with an SSH key"


def test_the_ssh_config_pins_the_server(tmp_path):
    rig = rigs.load({"HOME": str(tmp_path)})
    rig.dir.mkdir(parents=True)
    write_ssh(rig, "198.51.100.5")
    config = rig.ssh_config.read_text()
    assert "HostName 198.51.100.5" in config and "StrictHostKeyChecking yes" in config
    assert f'UserKnownHostsFile "{rig.known_hosts}"' in config and "ForwardAgent no" in config
    assert rig.ssh_wrapper.read_text() == f"#!/bin/sh\nexec ssh -F '{rig.ssh_config}' \"$@\"\n"
    bad = rigs.load({"HOME": str(tmp_path / "it's")})
    with pytest.raises(ValueError, match="quotes"):
        write_ssh(bad, "198.51.100.5")


#: deploy.sh's shape (Codex round 1 on #363): an `ssh` (here `cat`, which reads its standard
#: input as ssh does) before the prompt, the prompt on stderr without a newline, `read`, then
#: another reader after it.
CONFIRM = r"""
timeout 2 cat > "$1/before" || true
printf 'Type %s to deploy it: ' "$2" >&2
read -r answer || { echo "deploy.sh: not confirmed" >&2; exit 1; }
[ "$answer" = "$2" ] || { echo "deploy.sh: not confirmed" >&2; exit 1; }
cat > "$1/after"
seq 1 50000
echo "confirmed $answer"
"""


def test_the_confirmation_is_typed_only_at_the_prompt(tmp_path):
    argv = ["bash", "-c", CONFIRM, "bash", str(tmp_path), "v0.2.4rc1"]
    done = typed(
        argv, answer="v0.2.4rc1\n", prompt="Type v0.2.4rc1 to ", env={"PATH": "/usr/bin:/bin"}
    )
    assert done.ok, done.stderr
    assert done.stdout.endswith("50000\nconfirmed v0.2.4rc1\n")  # a large stdout drained too
    assert (tmp_path / "before").read_text() == ""  # the earlier reader took nothing
    assert (tmp_path / "after").read_text() == ""  # and the later one read EOF, not a hang
    never = typed(["bash", "-c", "echo deploy.sh: refused >&2; exit 1"], answer="v\n",
                  prompt="Type v to ", env={"PATH": "/usr/bin:/bin"})  # fmt: skip
    assert never.returncode == 1 and "refused" in never.stderr


def test_local_logs_the_whole_output(tmp_path):
    log = tmp_path / "deploy.log"
    done = local(["printf", "%s", "hello"], log=log)
    assert done.ok and done.stdout == "hello"
    assert log.read_text() == "$ printf %s hello\nhello\n[exit 0]\n"


#: A deploy.sh that never ends, with a child of its own (as its `ssh`), whose PID it records.
HANGS = r"""
sleep 300 &
echo $! > "$1/child"
sleep 300
"""


def _gone(pid: int, within: float = 10) -> bool:
    import os
    import time

    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _child(tmp_path) -> int:
    import time

    for _ in range(100):
        if (tmp_path / "child").exists() and (tmp_path / "child").read_text().strip():
            return int((tmp_path / "child").read_text())
        time.sleep(0.05)
    raise AssertionError("the script never started its child")


def test_a_timeout_stops_the_whole_deploy(tmp_path):
    """Codex round 4 on #363: deploy.sh's children don't outlive its timeout."""
    argv = ["bash", "-c", HANGS, "bash", str(tmp_path)]
    done = typed(argv, answer="v\n", prompt="Type v to ", env={"PATH": "/usr/bin:/bin"}, timeout=1)
    assert done.returncode == 124 and "timed out" in done.stderr
    assert _gone(_child(tmp_path)), "deploy.sh's child survived the timeout"


def test_an_interrupt_stops_the_whole_deploy_before_it_goes_on(tmp_path):
    """Codex round 4 on #363: SIGTERM or SIGHUP raise in run.py; the deploy must not go on."""
    import signal

    class Interrupted(Exception):
        pass

    def interrupt(signum, frame):
        raise Interrupted()

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(Interrupted):
            typed(["bash", "-c", HANGS, "bash", str(tmp_path)], answer="v\n", prompt="Type v to ",
                  env={"PATH": "/usr/bin:/bin"}, timeout=60)  # fmt: skip
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert _gone(_child(tmp_path)), "deploy.sh's child survived the interrupt"
