"""Test releases and deploy.sh (docs/ADMIN.md §1.6, §7; issue #356).

The test server runs signed release candidates of the commit under test (`release = "rc"`).
deploy.sh checks that a tag's name equals the version its `pyproject.toml` declares, so each test
release is a commit on top of the commit under test that changes only the version, in
`pyproject.toml` and `zab.__version__` (the VMs report the latter), tagged and signed with the
rig's **test-only** key. The tags live in the rig's own release repository, a `--shared` clone
of this checkout, so no test tag ever reaches the zab repository, and they are made fresh each
run (the running release's tag aside: deploy.sh diffs from it).

That repository plays the private one. The public one, `release_repo` (#958), holds exported
commits (`tools/publish`): `public.git`, a bare repository beside it, gets a test release as
`export` makes it, the private tree less what the export drops, one commit on the public
repository's last, its tag signed there with the same test-only key. A10 reviews a release in
the private repository and deploys its public commit from a clone of the public one into which
only the running release's private tag is fetched (`deploy_clone`), as BOOTSTRAP §13.4's last
hand deploy does; A10b's tags are public ones.

The keys are made once, with `ssh-keygen`, under the rig's `keys/`: `signing` (the only key in
the test server's `allowed_signers`), `unknown` (a signer it doesn't know, for A10) and `backup`
(the deployer's backup key, in `backup_recipients`: §1.7). deploy.sh reaches the server through
the rig's ssh config (`ZAB_DEPLOY_SSH`, a wrapper for `ssh -F`), pinned to its host key.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from e2e.admin.harness.gate import Gate
from e2e.admin.harness.rig import HOST_ALIAS, AdminRig
from e2e.harness.config import CHECKOUT
from e2e.harness.vm import Result, connect_line

KEYS = ("signing", "unknown", "backup")
_VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$")
_DIFF_SHA = re.compile(r"SHA-256 ([0-9a-f]{64}); release tree")
AUTHOR, EMAIL = "zab admin e2e", "zab-admin-e2e@example.org"
IDENTITY = ("-c", f"user.name={AUTHOR}", "-c", f"user.email={EMAIL}")
#: The export's own list of the paths it leaves out of the public tree (its `DROP`), read from
#: the commit exported; a commit without the tool, a public one, drops nothing.
EXPORT_TOOL = "tools/publish/export.py"
#: A local command: argument list, standard input, environment, timeout, a file for its output.
Local = Callable[..., Result]
#: A command that is typed an answer at its prompt (`typed`).
Typed = Callable[..., Result]


def local(
    argv: list[str],
    *,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600,
    log: Path | None = None,
) -> Result:
    """Run `argv` (never a shell string); with `log`, its output also goes there, whole."""
    try:
        done = subprocess.run(  # noqa: S603 - an argument list
            argv, input=input, env=dict(env) if env else None, capture_output=True,
            text=True, timeout=timeout, check=False,
        )  # fmt: skip
        result = Result(done.returncode, done.stdout, done.stderr)
    except subprocess.TimeoutExpired as exc:
        result = Result(124, str(exc.stdout or ""), f"{exc.stderr or ''}\ntimed out")
    _log(log, argv, result)
    return result


def _log(log: Path | None, argv: list[str], result: Result) -> None:
    if log is not None:
        with log.open("a", encoding="utf-8") as f:
            f.write(f"$ {' '.join(argv)}\n{result.stdout}{result.stderr}\n")
            f.write(f"[exit {result.returncode}]\n")


def typed(
    argv: list[str],
    *,
    answer: str,
    prompt: str,
    env: Mapping[str, str],
    timeout: float = 3600,
    log: Path | None = None,
) -> Result:
    """`argv` with an open but empty standard input until `prompt` shows on its standard error;
    then `answer` is typed and the input closed. deploy.sh's `ssh` calls before its prompt read
    their standard input, which is ours: an answer written up front would be theirs, and the
    prompt would read EOF (Codex round 1 on #363). After it, they read EOF.

    The command runs in a session of its own: on the timeout, or anything that ends the wait (an
    interrupt: SIGTERM and SIGHUP raise in `run.py`), its whole process group, deploy.sh and its
    `ssh` children, is stopped and reaped before the error goes on, so no deploy outlives an
    aborted run (Codex round 4 on #363)."""
    proc = subprocess.Popen(  # noqa: S603 - an argument list
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(env), start_new_session=True,
    )  # fmt: skip
    out, err = bytearray(), bytearray()
    assert proc.stdin and proc.stdout and proc.stderr

    def drain(stream: Any, buf: bytearray, watch: bool) -> None:
        while chunk := stream.read1(65536):
            buf.extend(chunk)
            if watch and not proc.stdin.closed and prompt.encode() in err:
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.write(answer.encode())
                    proc.stdin.close()

    threads = [threading.Thread(target=drain, args=(proc.stdout, out, False), daemon=True)]
    threads.append(threading.Thread(target=drain, args=(proc.stderr, err, True), daemon=True))
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(proc)
        code, timed_out = proc.wait(), True
    except BaseException:
        stop_group(proc)
        raise
    for thread in threads:
        thread.join(timeout=10)
    with contextlib.suppress(BrokenPipeError):
        proc.stdin.close()
    tail = "\ntimed out" if timed_out else ""
    result = Result(124 if timed_out else code, out.decode(errors="replace"),
                    err.decode(errors="replace") + tail)  # fmt: skip
    _log(log, argv, result)
    return result


def stop_group(proc: subprocess.Popen, grace: float = 10) -> None:
    """SIGTERM to `proc`'s process group, SIGKILL after `grace` seconds, then reaped."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 30)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=wait)
            if sig == signal.SIGKILL or not _group_alive(proc.pid):
                return


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# ---------------------------------------------------------------------------- versions


def key(version: str) -> tuple[int, int, int, int, int]:
    """deploy.sh's order (`vkey`): X.Y.ZrcN before X.Y.Z."""
    found = _VERSION.match(version)
    if not found:
        raise ValueError(f"{version}: not a release name")
    x, y, z, rc = found.groups()
    return int(x), int(y), int(z), 0 if rc is not None else 1, int(rc or 0)


def name(x: int, y: int, z: int, rc: int | None) -> str:
    return f"v{x}.{y}.{z}" + (f"rc{rc}" if rc is not None else "")


def next_version(base: str, running: str | None) -> str:
    """The next test release: an rc after the commit's own version `base` (`0.2.3` → `v0.2.4rc1`,
    `0.3.0rc2` → `v0.3.0rc3`), and after the release the server runs."""
    x, y, z, final, rc = key("v" + base.removeprefix("v"))
    candidate = name(x, y, z + 1, 1) if final else name(x, y, z, rc + 1)
    if running and key(running) >= key(candidate):
        x, y, z, final, rc = key(running)
        candidate = name(x, y, z + 1, 1) if final else name(x, y, z, rc + 1)
    return candidate


def older_version(running: str) -> str:
    """A release older than `running`, for A10's refused downgrade."""
    x, y, z, final, rc = key(running)
    if final:
        return name(x, y, z, 0)
    if rc > 0:
        return name(x, y, z, rc - 1)
    if z > 0:
        return name(x, y, z - 1, None)
    raise ValueError(f"no older release than {running} to test with")


# -------------------------------------------------------------------------------- keys


def dropped(source: str) -> tuple[str, ...]:
    """The paths of `DROP` in the export tool's source, read without running it; () for none."""
    for node in ast.parse(source).body:
        names = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
        if isinstance(node, ast.Assign) and names == ["DROP"]:
            return tuple(str(path) for path in ast.literal_eval(node.value))
    return ()


def under(path: str, drops: tuple[str, ...]) -> bool:
    """`path` is one of `drops` or lies under one (a directory's, with or without its `/`)."""
    return any(path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/") for d in drops)


def ensure_keys(gate: Gate, rig: AdminRig, run: Local = local) -> None:
    for which in KEYS:
        path = rig.keys / which
        if path.exists() or not gate.write(f"local ssh-keygen -t ed25519 -f {path}"):
            continue
        rig.keys.mkdir(mode=0o700, parents=True, exist_ok=True)
        made = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"zab-admin-e2e-{which}",
                    "-f", str(path)])  # fmt: skip
        if not made.ok:
            raise RuntimeError(f"ssh-keygen {path}: {made.stderr.strip()[:200]}")


def write_ssh(rig: AdminRig, ip: str) -> None:
    """The rig's ssh config for the server's current address, and deploy.sh's wrapper."""
    for path in (rig.ssh_key, rig.known_hosts, rig.ssh_config):
        if any(c in str(path) for c in "'\"\n"):
            raise ValueError(f"{path}: quotes and newlines are not allowed in the rig's paths")
    rig.ssh_config.write_text(
        f'Host {HOST_ALIAS}\n  HostName {ip}\n  User ubuntu\n  IdentityFile "{rig.ssh_key}"\n'
        f'  IdentitiesOnly yes\n  UserKnownHostsFile "{rig.known_hosts}"\n'
        "  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n  BatchMode yes\n"
        "  ForwardAgent no\n  ForwardX11 no\n  ClearAllForwardings yes\n  ConnectTimeout 15\n"
        "  ServerAliveInterval 30\n",
        encoding="utf-8",
    )
    rig.ssh_wrapper.write_text(f"#!/bin/sh\nexec ssh -F '{rig.ssh_config}' \"$@\"\n", "utf-8")
    rig.ssh_wrapper.chmod(0o700)


# ------------------------------------------------------------------------ the releases


@dataclass(frozen=True)
class Deployed:
    result: Result
    diff_sha256: str  # the reviewed diff's, as deploy.sh printed it; "" when it stopped before

    @property
    def ok(self) -> bool:
        return self.result.ok

    def why(self) -> str:
        """deploy.sh's own last words: `deploy.sh: <reason>` on a refusal."""
        lines = [ln for ln in self.result.stderr.splitlines() if ln.startswith("deploy.sh: ")]
        return lines[-1] if lines else (self.result.stderr.strip().splitlines() or [""])[-1]


#: deploy.sh's `die` when a `remote` call to its `--host` failed: the check of what the server
#: runs, and `run_server`'s temporary directory and copy (`deploy.sh: <host>: <reason>`)
CONNECT_DEATHS = ("unreachable", "no temporary directory", "could not copy the release")
#: deploy.sh's confirmation (`confirm`), with no newline: what follows is on its line
_PROMPT = re.compile(r"^Type v\S+ to [^:]*: ")


def connect_failed(result: Result, ip: str, host: str = HOST_ALIAS) -> str:
    """ssh's line when deploy.sh failed only because a connection to the server at `ip` was
    never made (`vm.connect_line`: timed out, refused, no route, network unreachable), so that
    no step of its server half ran and it may simply run again (#804); else "". deploy.sh's
    `remote()` never retries a connect. Its stderr then ends, read without the prompt:
    - with `deploy.sh: <host>: <reason>` for a reason of CONNECT_DEATHS, right after ssh's line;
    - or, exit 255, with ssh's line right after the prompt: the `ssh … sudo bash deploy.sh
      --server …` that would run the server's half never connected;
    either perhaps followed by ssh's lines for its exit trap's `rm`, which never connected
    either. Anything else ends the run: another `deploy.sh:` line, a line between ssh's and the
    `die`, a session cut short, an authentication refused, another host's connect."""
    if result.ok:
        return ""
    items: list[str | None] = []  # None: the prompt
    for line in result.stderr.splitlines():
        if prompt := _PROMPT.match(line):
            items.append(None)
            line = line[prompt.end() :]
        if line.strip():
            items.append(line.strip())

    def ssh_line(item: str | None) -> bool:
        return item is not None and connect_line(item, ip)

    after: list[str | None] = []  # the exit trap's
    while items and ssh_line(items[-1]):
        after.insert(0, items.pop())
    if items and items[-1] in {f"deploy.sh: {host}: {reason}" for reason in CONNECT_DEATHS}:
        cause = items[-2] if len(items) > 1 else None
        return cause if cause is not None and ssh_line(cause) else ""
    if result.returncode == 255 and after and items and items[-1] is None:
        return str(after[0])
    return ""


class Releases:
    def __init__(
        self, rig: AdminRig, run: Local = local, checkout: Path = CHECKOUT, type_: Typed = typed
    ) -> None:
        self.rig, self.run, self.checkout, self.type = rig, run, checkout, type_

    def git(
        self, *args: str, env: Mapping[str, str] | None = None, input: str | None = None
    ) -> str:
        done = self.run(["git", "-C", str(self.rig.repo), *args], env=env, input=input)
        if not done.ok:
            raise RuntimeError(f"git {args[0]}: {done.stderr.strip()[:300]}")
        return done.stdout.strip()

    def ensure_repo(self) -> None:
        if (self.rig.repo / "HEAD").exists() or (self.rig.repo / ".git").exists():
            return
        common = self.run(["git", "-C", str(self.checkout), "rev-parse", "--path-format=absolute",
                           "--git-common-dir"])  # fmt: skip
        made = self.run(["git", "clone", "-q", "--shared", "--no-checkout",
                         common.stdout.strip(), str(self.rig.repo)])  # fmt: skip
        if not (common.ok and made.ok):
            raise RuntimeError(f"the release repository: {(common.stderr + made.stderr)[:300]}")

    def source(self, *args: str) -> str:
        """git in this checkout, which has the commit under test (the release repository
        may not exist yet: a dry run makes none)."""
        done = self.run(["git", "-C", str(self.checkout), *args])
        if not done.ok:
            raise RuntimeError(f"git {args[0]}: {done.stderr.strip()[:300]}")
        return done.stdout.strip()

    def commit_of(self, rev: str) -> str:
        return self.source("rev-parse", "--verify", f"{rev}^{{commit}}")

    def base_version(self, commit: str) -> str:
        text = self.source("show", f"{commit}:pyproject.toml")
        found = re.search(r'(?m)^version = "([^"]+)"$', text)
        if not found:
            raise RuntimeError(f"{commit[:12]}: no version in pyproject.toml")
        return found.group(1)

    def version_commit(self, base: str, version: str) -> str:
        """`base` with its version set to `version` (no `v`), as a new commit."""
        bare = version.removeprefix("v")
        edits = {
            "pyproject.toml": (r'(?m)^version = "[^"]*"$', f'version = "{bare}"'),
            "src/zab/__init__.py": (r'(?m)^__version__ = "[^"]*"$', f'__version__ = "{bare}"'),
        }
        with tempfile.TemporaryDirectory(prefix="ae2e-index-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            env |= {"GIT_AUTHOR_NAME": AUTHOR, "GIT_COMMITTER_NAME": AUTHOR}
            env |= {"GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_EMAIL": EMAIL}
            self.git("read-tree", base, env=env)
            for path, (pattern, line) in edits.items():
                old = self.git("show", f"{base}:{path}") + "\n"
                text, count = re.subn(pattern, line, old, count=1)
                if count != 1:
                    raise RuntimeError(f"{base[:12]}:{path}: no version line to set")
                blob = self.git("hash-object", "-w", "--stdin", input=text)
                self.git("update-index", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = self.git("write-tree", env=env)
            message = f"zab admin e2e: {version} of {base[:12]} (test only)"
            return self.git("commit-tree", tree, "-p", base, "-m", message, env=env)

    def tag(self, version: str, commit: str, signer: str | None = "signing") -> str:
        """`refs/tags/<version>` made afresh: annotated, and signed with the rig's `signer` key
        (None: unsigned). The tag object's ID."""
        self.run(["git", "-C", str(self.rig.repo), "tag", "-d", version])  # an earlier run's
        if signer is None:
            self.git(*IDENTITY, "tag", "-a", "--no-sign", "-m", version, version, commit)
        else:
            signing = ("-c", "gpg.format=ssh", "-c", f"user.signingkey={self.rig.keys / signer}")
            self.git(*IDENTITY, *signing, "tag", "-s", "-m", version, version, commit)
        return self.git("rev-parse", f"refs/tags/{version}")

    def alias(self, ref_name: str, tag_object: str) -> None:
        """`refs/tags/<ref_name>` pointing at another tag's object (A10's renamed ref)."""
        self.git("update-ref", f"refs/tags/{ref_name}", tag_object)

    def has_tag(self, version: str) -> bool:
        return self.run(["git", "-C", str(self.rig.repo), "rev-parse", "--verify", "--quiet",
                         f"refs/tags/{version}"]).ok  # fmt: skip

    def release(self, base: str, version: str, signer: str | None = "signing") -> str:
        """A test release of `base`: its version commit, tagged. The commit."""
        commit = self.version_commit(base, version)
        self.tag(version, commit, signer)
        return commit

    @property
    def public(self) -> Path:
        """The rig's public repository (#958): exported commits only, bare."""
        return self.rig.repo.parent / "public.git"

    def public_git(self, *args: str) -> str:
        done = self.run(["git", "-C", str(self.public), *args])
        if not done.ok:
            raise RuntimeError(f"git {args[0]} (public): {done.stderr.strip()[:300]}")
        return done.stdout.strip()

    def has_public_tag(self, version: str) -> bool:
        return (self.public / "HEAD").exists() and self.run(
            ["git", "-C", str(self.public), "rev-parse", "--verify", "--quiet",
             f"refs/tags/{version}"]
        ).ok  # fmt: skip

    def export(self, version: str, commit: str, signer: str | None = "signing") -> str:
        """The public release of the test release `commit`, as `tools/publish` makes one: its
        tree less the export's `DROP`, one commit on the public repository's `main` with an
        `Exported-from:` trailer, pushed there (only it and the public history travel), then
        tagged there afresh, signed with the rig's `signer` key (None: unsigned). The public
        commit."""
        if not (self.public / "HEAD").exists():
            made = self.run(["git", "init", "-q", "--bare", "-b", "main", str(self.public)])
            if not made.ok:
                raise RuntimeError(f"the public repository: {made.stderr.strip()[:300]}")
        tool = self.run(["git", "-C", str(self.rig.repo), "show", f"{commit}:{EXPORT_TOOL}"])
        drops = dropped(tool.stdout) if tool.ok else ()
        parent: list[str] = []
        if self.run(["git", "-C", str(self.public), "rev-parse", "--verify", "--quiet",
                     "refs/heads/main"]).ok:  # fmt: skip
            # the public main, here too: the rig's repository may be newer than the public one
            self.git(
                "fetch", "-q", "--no-tags", str(self.public), "+refs/heads/main:refs/public/main"
            )
            parent = ["-p", "refs/public/main"]
        with tempfile.TemporaryDirectory(prefix="ae2e-export-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            env |= {"GIT_AUTHOR_NAME": AUTHOR, "GIT_COMMITTER_NAME": AUTHOR}
            env |= {"GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_EMAIL": EMAIL}
            self.git("read-tree", commit, env=env)
            names = self.git("ls-files", "-z", env=env).split("\0")
            gone = [name for name in names if name and under(name, drops)]
            if gone:
                self.git("update-index", "--force-remove", "-z", "--stdin", env=env,
                         input="".join(f"{name}\0" for name in gone))  # fmt: skip
            tree = self.git("write-tree", env=env)
            message = f"zab {version}\n\nExported-from: {commit}\n"
            public = self.git("commit-tree", tree, *parent, "-m", message, env=env)
        self.git("push", "-q", str(self.public), f"{public}:refs/heads/main")
        self.run(["git", "-C", str(self.public), "tag", "-d", version])  # an earlier run's
        if signer is None:
            self.public_git(*IDENTITY, "tag", "-a", "--no-sign", "-m", version, version, public)
        else:
            signing = ("-c", "gpg.format=ssh", "-c", f"user.signingkey={self.rig.keys / signer}")
            self.public_git(*IDENTITY, *signing, "tag", "-s", "-m", version, version, public)
        return public

    def deploy_clone(self, base: str, to: Path) -> Path:
        """The deployer's clone of the public repository (BOOTSTRAP §13.4), at `to`, new: its
        remote removed, so that nothing is ever pushed from it, and the private tag `base`, the
        release the server runs and the review's base, fetched in alone, over a public tag of
        that name if an earlier run's export left one."""
        made = self.run(["git", "clone", "-q", str(self.public), str(to)])
        if not made.ok:
            raise RuntimeError(f"the deploy clone: {made.stderr.strip()[:300]}")
        for args in (["remote", "remove", "origin"],
                     ["fetch", "-q", "--no-tags", str(self.rig.repo),
                      f"+refs/tags/{base}:refs/tags/{base}"]):  # fmt: skip
            done = self.run(["git", "-C", str(to), *args])
            if not done.ok:
                raise RuntimeError(f"the deploy clone, git {args[0]}: {done.stderr.strip()[:300]}")
        return to

    def extract(self, commit: str, path: str, to: Path) -> Path:
        """A file of `commit` (deploy.sh, render_policy.py), as the commit has it."""
        to.write_text(self.source("show", f"{commit}:{path}") + "\n", encoding="utf-8")
        return to

    def deploy(
        self,
        script: Path,
        version: str,
        *flags: str,
        out: Path,
        log: Path | None = None,
        confirm: str | None = None,
        repo: Path | None = None,
    ) -> Deployed:
        """deploy.sh as a deployer runs it from the development machine (§7), typing the version
        when asked: `--bootstrap` (with the rig's secrets), `--wheel`, `--downgrade`, or an
        upgrade; from the rig's repository, or `repo` (a deploy clone, the public one)."""
        rig = self.rig
        argv = ["bash", str(script), "--repo", str(repo or rig.repo), "--etc", str(rig.etc)]
        argv += ["--out", str(out), "--host", HOST_ALIAS, *flags]
        if "--bootstrap" in flags:
            argv += ["--secrets", str(rig.secrets)]
        env = {**os.environ, "ZAB_DEPLOY_SSH": str(rig.ssh_wrapper)}
        done = self.type([*argv, version], answer=f"{confirm or version}\n",
                         prompt=f"Type {version} to ", env=env, timeout=3600, log=log)  # fmt: skip
        found = _DIFF_SHA.search(done.stderr)
        return Deployed(done, found.group(1) if found else "")
