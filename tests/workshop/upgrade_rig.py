"""A deployer's machine for `workshop/adminserver/upgrade.py`'s offline tests (#772).

`origin` is a repository of the release paths, this tree's own (the driver under test among
them), whose `main` gets a commit per release. The machine has its HOME in the test's directory:
its clone of `origin` (`~/zab`), its admin directory with `allowed_signers` (`~/zab-admin`), the
dedicated key without a passphrase (`~/.ssh/zab-deployer`), and the driver's configuration. The
environment holds no agent: each test starts one if it needs it. No network or account.

Neither repository runs the maintenance that git starts after a fetch, a commit or a push: from
git 2.54 on, it prunes worktrees in the background, beside the test's next command, and can
remove the one that a `git worktree add` is making (#920). The driver's own fetch doesn't start
it either, which a test checks without the clone's setting."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVER = "workshop/adminserver/upgrade.py"
RELEASE_PATHS = [
    line
    for line in (ROOT / "workshop/adminserver/release-paths").read_text().splitlines()
    if line and not line.startswith("#")
]
ENV_GIT = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_DATE": "2026-11-06T12:00:00Z"}


def tool(name: str) -> str:
    found = shutil.which(name)
    assert found, name
    return found


def missing_tools() -> str:
    names = ("git", "bash", "perl", "ssh-keygen", "ssh-add", "ssh-agent")
    return " ".join(name for name in names if not shutil.which(name))


def listening(path: Path) -> bool:
    """ssh-agent binds its socket, which makes the file, before it listens (#920)."""
    with socket.socket(socket.AF_UNIX) as sock:
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


class Machine:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.home = tmp, tmp / "home"
        self.origin, self.clone = tmp / "origin", self.home / "zab"
        self.admin = self.home / "zab-admin"
        self.etc = self.admin / "etc"
        for path in (self.home / ".ssh", self.etc, tmp / "t"):
            path.mkdir(parents=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TMPDIR": str(tmp / "t"),
            "LANG": "C.UTF-8",
            **ENV_GIT,
        }
        self.agents: list[subprocess.Popen] = []
        self.key = self.keygen(self.home / ".ssh" / "zab-deployer")
        self.other = self.keygen(tmp / "other")
        (self.etc / "allowed_signers").write_text(self.signers_line(self.key))
        shutil.copy(ROOT / "workshop/adminserver/admin.example.toml", self.etc / "admin.toml")
        (self.home / ".gitconfig").write_text("[user]\n\tname = D\n\temail = d@example.org\n")
        self.conf = self.home / ".config" / "zab" / "upgrade.conf"
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(f"key={self.key}\nclone={self.clone}\nadmin={self.admin}\n")
        self.init_origin()

    # --- helpers

    def sh(self, *argv, cwd=None, input=None, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - the test's own argument lists
            [tool(str(argv[0])), *map(str, argv[1:])], cwd=cwd, input=input, text=True,
            capture_output=True, env={**self.env, **(env or {})}, check=False,
        )  # fmt: skip

    def git(self, *args, where: Path | None = None) -> str:
        done = self.sh("git", "-C", where or self.origin, *args)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def keygen(self, path: Path, passphrase: str = "") -> Path:
        done = self.sh("ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-C", path.name,
                       "-f", path)  # fmt: skip
        assert done.returncode == 0, done.stderr
        return path

    def signers_line(self, key: Path) -> str:
        return f'd@example.org namespaces="git" {Path(f"{key}.pub").read_text().strip()}\n'

    def fingerprint(self, key: Path) -> str:
        return self.sh("ssh-keygen", "-l", "-f", f"{key}.pub").stdout.split()[1]

    # --- the releases

    def init_origin(self) -> None:
        for rel in RELEASE_PATHS:
            source, target = ROOT / rel, self.origin / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(source, target)
        self.git("init", "-q", "-b", "main")
        self.git("config", "maintenance.auto", "false")
        self.git("config", "receive.autogc", "false")  # the driver's push
        self.release("v0.3.0", signer=self.key)
        done = self.sh("git", "clone", "-q", "-c", "maintenance.auto=false", self.origin,
                       self.clone)  # fmt: skip
        assert done.returncode == 0, done.stderr

    def release(self, version: str, signer: Path | None = None, *, parent: str = "") -> str:
        """A commit of `version` on main (or after `parent`, detached), signed with `signer`."""
        if parent:
            self.git("checkout", "-q", "--detach", parent)
        path = self.origin / "pyproject.toml"
        text = re.sub(r'(?m)^version = ".*"$', f'version = "{version[1:]}"', path.read_text())
        path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", f"release {version}")
        commit = self.git("rev-parse", "HEAD")
        if parent:
            self.git("checkout", "-q", "main")
        if signer:
            self.sign(version, commit, signer)
        return commit

    def sign(self, version: str, commit: str, key: Path, where: Path | None = None) -> None:
        self.git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s",
                 "-m", f"zab {version}", version, commit, where=where)  # fmt: skip

    def review_sha256(self, commit: str, since: str = "v0.3.0") -> str:
        """The SHA-256 that `deploy.sh --review` prints for `commit`: the release message's."""
        out = self.tmp / "review"
        out.mkdir(exist_ok=True)
        deploy = ROOT / "workshop/adminserver/deploy.sh"
        done = self.sh("bash", deploy, "--repo", self.origin, "--etc", self.etc,
                       "--since", since, "--review", commit, "--out", out)  # fmt: skip
        assert done.returncode == 0, done.stderr
        return re.search(r"SHA-256 ([0-9a-f]{64}); release tree", done.stderr)[1]

    def copy(self, version: str) -> Path:
        """The worktree of `version` in the clone, as BOOTSTRAP §9 makes it."""
        self.git("fetch", "-q", "origin", "--tags", where=self.clone)
        path = self.home / f"zab-{version}"
        self.git("worktree", "add", "-q", "--detach", path, version, where=self.clone)
        return path

    # --- running the driver

    def agent(self, *keys: Path) -> dict[str, str]:
        """A terminal's agent of the test's own, holding `keys`: its SSH_AUTH_SOCK."""
        sock = self.tmp / f"agent{len(self.agents)}"
        self.agents.append(subprocess.Popen(  # noqa: S603
            [tool("ssh-agent"), "-D", "-a", str(sock)], stdout=subprocess.DEVNULL,
        ))  # fmt: skip
        for _ in range(200):
            if listening(sock):
                break
            time.sleep(0.05)
        for key in keys:
            done = self.sh("ssh-add", "-q", key, env={"SSH_AUTH_SOCK": str(sock)})
            assert done.returncode == 0, done.stderr
        return {"SSH_AUTH_SOCK": str(sock)}

    def run(self, *args, copy: Path | None = None, input="", env=None):
        """The driver of `copy` (the clone's own checkout by default), as a deployer runs it."""
        script = (copy or self.clone) / DRIVER
        return self.sh(sys.executable, "-I", script, *args, input=input, env=env)

    def close(self) -> None:
        for proc in self.agents:
            proc.terminate()
            proc.wait()
