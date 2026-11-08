#!/usr/bin/env python3
"""Prepare an upgrade of the admin server: review, sign, verify and push a release (#772).

    python3 -I ~/zab-vP/workshop/adminserver/upgrade.py --setup     once on each machine
    python3 -I ~/zab-vP/workshop/adminserver/upgrade.py vN --commit C --review-sha256 SHA

It reaches no server. The server is upgraded to the release it signs from the copy ~/zab-vN it
makes, by the adminserver README's deploy, and by `!upgrade-server` once #569 adds that.

vP is the release the server runs. Run the copy of its signed tag: a clean worktree of your zab
clone, at a commit on which exactly one release tag verifies against your allowed_signers. The
driver refuses any other copy, and prints the lines that make the right one. vN is the new
release, C its commit on main, and SHA the SHA-256 of its review, both from the release
message. Run it on your own machine, in BOOTSTRAP §2.1's terminal, never on a machine where
coding agents run (BOOTSTRAP §13.2).

`--setup` asks for the three values below once, and keeps them in ~/.config/zab/upgrade.conf:
the dedicated key, the clone and the admin directory. Nothing in it is secret.

The phases, after a preflight that changes nothing but the clone's fetch:
- the review of C since vP, which this copy's deploy.sh pages, as BOOTSTRAP §9 runs it; its
  diff must have the release message's SHA-256, then you type vN to sign it. Every run compares
  that SHA-256; once the tag verifies, without paging the review again;
- the tag: signed on C through the agent, checked as `tag -v` checks it and never by git's exit
  status (git 2.43 exits 0 after a failed signature), pushed to origin, and its copy ~/zab-vN
  made from it. From the public repository, origin has its maintainer's vN of C, which stays
  as it is: your own is kept in your clone, never pushed (BOOTSTRAP §9).

Each phase checks first whether it is done, from what it reads (the tags, the clone), never
from a state file: after any stop, run the same command again. The driver pauses only for what
a person does: the key's passphrase, reading the review, typing the version to sign. What it
prints goes to ~/zab-admin/upgrades/vN/transcript.txt too, with every command it runs.

Standard library only, run with `python3 -I`, as render_policy.py is. Every command is an
argument list.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: deploy.sh's patterns: a release name, a full commit, a SHA-256
VERSION = re.compile(
    r"v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(?:rc(0|[1-9][0-9]{0,5}))?",
    re.ASCII,
)
COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
#: what the driver runs, and deploy.sh's review with it (BOOTSTRAP §1)
TOOLS = ("git", "bash", "perl", "ssh-keygen", "ssh-add", "ssh-agent")
#: upgrade.conf's values, in the order --setup asks for them
SETTINGS = {
    "key": "the dedicated key's file, with its .pub beside it (BOOTSTRAP §2.1)",
    "clone": "your zab clone (BOOTSTRAP §1, step 2)",
    "admin": "the admin directory, which holds etc/ (BOOTSTRAP §1, step 3)",
}
DEFAULTS = {"clone": "~/zab", "admin": "~/zab-admin"}
KEY_LIFE = "4h"  # how long the driver's own agent keeps the key
END_WAIT = 60  # seconds an interrupted command has to run its own clean-up, before it is killed
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)  # each ends the run, clean-ups done


class Stop(Exception):
    """A refusal or a failure: its text is the `stopped: …` line, and the run exits 1."""


def stop(text: str) -> None:
    raise Stop(text)


def conf_dir() -> Path:
    return Path.home() / ".config" / "zab"


def vkey(version: str) -> tuple[int, ...]:
    """A release's place in the order of deploy.sh's `older`: vX.Y.ZrcN comes before vX.Y.Z."""
    m = VERSION.fullmatch(version)
    if not m:
        stop(f"{version}: not a release name (vX.Y.Z or vX.Y.ZrcN)")
    major, minor, patch, rc = m.groups()
    return (int(major), int(minor), int(patch), 0 if rc else 1, int(rc or 0))


def read_conf(path: Path, names, *, others_too: bool = False) -> dict[str, str]:
    """`name=value` lines, read and never run; blank lines and `#` lines are skipped. Any other
    line stops the run, unless `others_too`: --setup offers what it can read, and rewrites the
    file without the rest."""
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if sep and name in names:
            found[name] = value
        elif not others_too:
            stop(f"{path}: not a line it takes: {line[:80]!r}. Remove it, or run --setup,"
                 " which writes the file again")  # fmt: skip
    return found


def checked(name: str, value: str) -> str:
    """A value of upgrade.conf as an absolute path, which must exist: the key with its .pub."""
    path = Path(value).expanduser()
    if not value or not path.is_absolute() or any(c.isspace() for c in str(path)):
        stop(f"{name}: an absolute path without blanks, please: {value!r}")
    if name == "key" and not Path(f"{path}.pub").is_file():
        stop(f"no {path}.pub: {SETTINGS['key']}")
    if name != "key" and not path.is_dir():
        stop(f"{name}: no directory {path}: {SETTINGS[name]}")
    return str(path)


def load_config() -> dict[str, str]:
    path = conf_dir() / "upgrade.conf"
    if not path.exists():
        stop(f"no {path}: run this once with --setup")
    values = read_conf(path, SETTINGS)
    missing = [name for name in SETTINGS if name not in values]
    if missing:
        stop(f"{path} has no {', '.join(missing)}: run this again with --setup")
    return {name: checked(name, values[name]) for name in SETTINGS}


def setup() -> int:
    """Ask for each value once, offering the saved one, or ssh-rule.sh's key, or the default."""
    path = conf_dir() / "upgrade.conf"
    saved = read_conf(path, SETTINGS, others_too=True)
    rule = read_conf(conf_dir() / "ssh-rule.conf", ("key",), others_too=True)
    values = {}
    for name, what in SETTINGS.items():
        default = saved.get(name) or (rule.get("key") if name == "key" else DEFAULTS.get(name))
        try:
            answer = input(f"{what}{f' [{default}]' if default else ''}: ").strip()
        except EOFError:
            answer = ""
        values[name] = checked(name, answer or default or "")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    new = path.with_name("upgrade.conf.new")
    new.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    new.chmod(0o600)
    new.replace(path)
    print(f"saved, in {path}: " + ", ".join(f"{name} {value}" for name, value in values.items()))
    return 0


@dataclass
class Phase:
    """One step of the run. `done` says why it is done already, from what it reads, or "" when
    it is not; `run` does it. A phase whose `done` is None runs every time."""

    title: str
    run: Callable[[], None]
    done: Callable[[], str] | None = None


def listening(path: str) -> bool:
    """Something accepts connections on the socket at `path`. ssh-agent binds its socket, which
    makes the file, before it listens: an ssh-add in between is refused (#920)."""
    with socket.socket(socket.AF_UNIX) as sock:
        try:
            sock.connect(path)
        except OSError:
            return False
    return True


class Agent:
    """The dedicated key in an ssh agent, and no other key (BOOTSTRAP §2.1). This terminal's
    agent when it holds that key alone; otherwise one of the driver's own, for the commands it
    runs only, which ssh-add asks the passphrase for, and which stops at the driver's exit.
    Checked before each use: a key whose lifetime lapsed is added again (#772). Commands that
    reach origin never get the driver's own agent, so that git authenticates to it as it would
    without the driver (`Upgrade.git`)."""

    def __init__(self, up: Upgrade, key: Path) -> None:
        self.up, self.key = up, key
        self.proc: subprocess.Popen | None = None
        self.dir = ""
        self.fingerprint = ""

    def env(self) -> dict[str, str]:
        """What the commands the driver runs get: the driver's own agent, once it has one."""
        return {"SSH_AUTH_SOCK": f"{self.dir}/agent"} if self.proc else {}

    def holds_key_alone(self) -> bool:
        """`ssh-add -l` lists one key, whose fingerprint, its second field, is the key's."""
        listed = [line.split() for line in self.up.call(["ssh-add", "-l"]).stdout.splitlines()]
        return len(listed) == 1 and len(listed[0]) > 1 and listed[0][1] == self.fingerprint

    def ensure(self) -> None:
        if not self.fingerprint:
            words = self.up.call(["ssh-keygen", "-l", "-f", f"{self.key}.pub"]).stdout.split()
            if len(words) < 2 or not words[1].startswith("SHA256:"):
                stop(f"ssh-keygen read no fingerprint from {self.key}.pub")
            self.fingerprint = words[1]
        if self.holds_key_alone():
            return
        if not self.proc:
            self.start()
        self.up.say(f"ssh-add {self.key}, for {KEY_LIFE}, into the driver's own agent:")
        self.up.call(["ssh-add", "-t", KEY_LIFE, str(self.key)], tty=True)
        if not self.holds_key_alone():
            stop(f"the agent doesn't hold {self.key} ({self.fingerprint}): run this again")

    def start(self) -> None:
        # Under /tmp, as ssh-agent's own: a socket's path has at most about 100 bytes.
        self.dir = tempfile.mkdtemp(prefix="zab-agent.", dir="/tmp")  # mode 0700
        self.up.note(f"$ ssh-agent -D -a {self.dir}/agent")
        self.proc = subprocess.Popen(  # noqa: S603 - an argument list; -D: it stays our child
            ["ssh-agent", "-D", "-a", f"{self.dir}/agent"],  # noqa: S607 - from PATH, as ssh is
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        )  # fmt: skip
        for _ in range(200):
            if listening(f"{self.dir}/agent") or self.proc.poll() is not None:
                break
            time.sleep(0.05)
        if not listening(f"{self.dir}/agent"):
            stop("ssh-agent started no agent")
        self.up.say("this terminal's agent doesn't hold the key alone: the driver uses its own")

    def close(self) -> None:
        """The driver's own agent ended, and its directory removed; or, if either is left,
        a line that says which."""
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            self.proc = None
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
            if Path(self.dir).exists():
                self.up.say(f"clean-up: the driver's agent left {self.dir}: remove it", err=True)


class Upgrade:
    """One run of the driver: its values, what it found, its phases, and its clean-ups."""

    def __init__(self, version: str, commit: str, review_sha256: str) -> None:
        self.v, self.commit, self.sha = version, commit, review_sha256
        conf = load_config()
        self.clone, self.admin = Path(conf["clone"]), Path(conf["admin"])
        self.etc, self.out = self.admin / "etc", self.admin / "upgrades" / version
        self.here = Path(__file__).resolve().parents[2]  # this copy's worktree
        self.prev = ""  # vP, which this copy is
        self.compared = ""  # the review's SHA-256, once this run has compared it (`review`)
        self.log = None
        self.signal = signal.SIGINT  # the signal that interrupted the run (`interrupted`)
        self.agent = Agent(self, Path(conf["key"]))
        self.cleanups: list[Callable[[], None]] = [self.agent.close]
        self.phases = self.make_phases()

    def make_phases(self) -> list[Phase]:
        return [
            Phase(f"the review of {self.commit[:12]}, this copy's deploy.sh", self.review),
            Phase(f"the tag {self.v}: signed, verified, pushed; its copy", self.tag, self.tagged),
        ]

    # --- output and commands

    def say(self, text: str, err: bool = False) -> None:
        print(text, file=sys.stderr if err else sys.stdout, flush=True)
        self.note(text)

    def note(self, text: str) -> None:
        """The transcript alone."""
        if self.log:
            self.log.write(text + "\n")
            self.log.flush()

    def call(
        self, argv, *, tty=False, env=None, cwd=None, own_agent=True
    ) -> subprocess.CompletedProcess:
        """`argv`, an argument list, with the driver's agent (unless not `own_agent`: this
        terminal's). Its output is captured, or with `tty` it uses the terminal itself (a pager,
        a prompt, a passphrase). Interrupted, the run stops once the command has ended (`end`)."""
        argv = [str(a) for a in argv]
        self.note(f"$ {shlex.join(argv)}")
        full = {**os.environ, **(self.agent.env() if own_agent else {}), **(env or {})}
        pipe = None if tty else subprocess.PIPE
        interrupted = False
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(  # noqa: S603 - an argument list
                argv, env=full, cwd=cwd, stdin=None if tty else subprocess.DEVNULL,
                stdout=out if pipe else None, stderr=err if pipe else None,
            )  # fmt: skip
            try:
                code = proc.wait()
            except KeyboardInterrupt:
                interrupted = True
                code = self.end(proc)
            out.seek(0)
            err.seek(0)
            done = subprocess.CompletedProcess(
                argv, code, out.read().decode(errors="replace"), err.read().decode(errors="replace")
            )
        self.note(f"  exit {code}")
        if interrupted:
            raise KeyboardInterrupt
        return done

    def end(self, proc: subprocess.Popen) -> int:
        """The command a signal interrupted, ended. A Ctrl-C at the terminal reaches it too;
        SIGTERM or SIGHUP sent to the driver alone is passed on to it, the direct child only:
        the commands share the terminal's process group, which a pager and a prompt need, and
        a signal to that whole group could reach the shell that started the driver. It then has
        END_WAIT seconds for its own clean-up (deploy.sh's traps), and is killed after that."""
        if self.signal != signal.SIGINT:
            proc.send_signal(self.signal)
        deadline = time.monotonic() + END_WAIT
        while True:
            try:
                return proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                return proc.wait()
            except KeyboardInterrupt:  # another signal: the same deadline
                pass

    def interrupted(self, signum: int, _frame) -> None:
        """Ctrl-C, SIGTERM or SIGHUP: the run stops, and its clean-ups run."""
        self.signal = signum
        raise KeyboardInterrupt

    def git(
        self, *args, user=False, origin=False, where: Path | None = None
    ) -> subprocess.CompletedProcess:
        """git in the clone (or `where`), with neither the user's nor the system's configuration
        and no replace refs, as deploy.sh's `g`; with `user`, the user's own, which signing
        needs (the tagger's name), and with `origin` too, for a command that reaches origin
        (its credentials), which then gets this terminal's agent, never the driver's own: an
        SSH origin would otherwise be offered the dedicated key alone."""
        env = {"GIT_NO_REPLACE_OBJECTS": "1"}
        if not (user or origin):
            env |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        argv = ["git", "-C", where or self.clone, *args]
        return self.call(argv, env=env, own_agent=not origin)

    def read(self, *args, where: Path | None = None) -> str:
        """A git command's output, or a stop with its error."""
        done = self.git(*args, where=where)
        if done.returncode:
            stop(f"git {' '.join(args)}: {done.stderr.strip()[:300]}")
        return done.stdout.strip()

    # --- the tags

    def verify(self, version: str, signers: Path, fingerprint: str = "") -> tuple[str, str]:
        """(the commit, "") when refs/tags/VERSION is an annotated tag of a commit, named
        VERSION in the tag object itself, signed with an SSH key of SIGNERS (with FINGERPRINT,
        by that key), whose pyproject.toml says that version: deploy.sh's `verify_tag`'s checks,
        less its release pattern, which admin.toml sets on the server. `verify-tag` exits
        non-zero for a missing or bad signature, unlike git 2.43's `tag -s` for a failed one.
        Otherwise ("", why)."""
        obj = self.git("rev-parse", "--verify", "--quiet", f"refs/tags/{version}").stdout.strip()
        text = self.git("cat-file", "tag", obj).stdout if obj else ""
        header = text.split("\n\n")[0].splitlines()
        fields = dict(line.split(" ", 1) for line in header if " " in line)
        out = ""
        if obj and text and "\n-----BEGIN SSH SIGNATURE-----\n" in text:
            done = self.git(
                "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}",
                "-c", "gpg.ssh.program=ssh-keygen", "-c", "gpg.program=false",
                "-c", "gpg.x509.program=false", "verify-tag", obj,
            )  # fmt: skip
            out = (done.stdout + done.stderr).strip() if done.returncode == 0 else ""
        said = ""  # the version of the commit's pyproject.toml, once the signature is good
        if 'Good "git" signature for ' in out and COMMIT.fullmatch(fields.get("object", "")):
            meta = self.git("show", f"{fields['object']}:pyproject.toml").stdout
            try:
                said = tomllib.loads(meta).get("project", {}).get("version", "")
            except tomllib.TOMLDecodeError:
                said = ""
        for bad, why in (
            (not obj, "no such tag"),
            (not text, "a lightweight tag, which has no signature"),
            (fields.get("tag") != version, "its tag object has another name"),
            (fields.get("type") != "commit" or not COMMIT.fullmatch(fields.get("object", "")),
             "not a tag of a commit"),
            ("\n-----BEGIN SSH SIGNATURE-----\n" not in text, "not signed with an SSH key"),
            ('Good "git" signature for ' not in out, f"not signed by a key of {signers}"),
            (fingerprint and f" with ED25519 key {fingerprint}" not in out,
             f"not signed by {fingerprint}"),
            (said != version[1:], f"its pyproject.toml says version {said or '(none)'}"),
        ):  # fmt: skip
            if bad:
                return "", f"{version}: {why}"
        return fields["object"], ""

    def own_copy(self) -> None:
        """This copy is a release's signed tag: a clean worktree of the clone, at a commit on
        which exactly one release tag verifies against etc/allowed_signers. That release is vP.
        As deploy.sh's own check, it guards against running the wrong copy, not against a
        compromised account."""
        signers, here = self.etc / "allowed_signers", self.here
        top = self.git("rev-parse", "--show-toplevel", where=here).stdout.strip()
        ours = self.git("rev-parse", "--path-format=absolute", "--git-common-dir", where=here)
        theirs = self.read("rev-parse", "--path-format=absolute", "--git-common-dir")
        names: list[str] = []
        if top != str(here) or ours.stdout.strip() != theirs:
            why = f"not a checkout of {self.clone}"
        elif self.read("status", "--porcelain", "--untracked-files=all", where=here):
            why = "it has changes (git status)"
        else:
            head = self.read("rev-parse", "HEAD", where=here)
            tags = [
                t for t in self.read("tag", "--points-at", head).split() if VERSION.fullmatch(t)
            ]
            found = {t: self.verify(t, signers) for t in tags}
            names = [t for t, (commit, _) in found.items() if commit == head]
            refused = "; ".join(why for _, why in found.values() if why)
            if names:
                why = f"{len(names)} release tags verify on it"
            else:
                why = refused or f"no release tag names {head}"
        if len(names) == 1:
            self.prev = names[0]
            return
        q, wt = shlex.quote, shlex.quote(str(Path.home() / "zab-"))
        clone = q(str(self.clone))
        stop(
            f"{here}: {why}.\n"
            "Run the copy of the release the server runs, which its signed tag makes:\n"
            "  V=<the release the server runs, as !version shows it>\n"
            f"  git -C {clone} fetch origin --tags\n"
            f'  git -C {clone} -c gpg.ssh.allowedSignersFile={q(str(signers))} tag -v "$V" &&'
            f' git -C {clone} worktree add --detach {wt}"$V" "$V"\n'
            f'  python3 -I {wt}"$V"/workshop/adminserver/upgrade.py {self.v}'
            f" --commit {self.commit} --review-sha256 {self.sha}"
        )

    def fetch(self) -> None:
        """origin's branches, and its tags into refs/remote-tags/origin/, so that a fetch never
        touches this clone's tags. A tag the clone lacks is then made from origin's, and one it
        has is kept: from the public repository, a deployer's tag is their own signature where
        origin's is the maintainer's (BOOTSTRAP §9), which `git fetch --tags` refuses to
        clobber, exiting 1. Never --force over refs/tags. Without the maintenance that git
        starts after a fetch: from git 2.54 on, it prunes worktrees in the background, and can
        remove the one that `git worktree add` is making for vN's copy (#920)."""
        done = self.git(
            "-c", "maintenance.auto=false",
            "fetch", "--no-tags", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*",
            "+refs/tags/*:refs/remote-tags/origin/*", origin=True,
        )  # fmt: skip
        if done.returncode:
            stop(f"git fetch origin: {done.stderr.strip()[:400]}")
        ours = set(self.read("for-each-ref", "--format=%(refname)", "refs/tags/").split())
        theirs = self.read(
            "for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", "refs/remote-tags/origin/"
        )
        for line in theirs.splitlines():
            name, obj = line.split(" ")
            if f"refs/tags/{name}" not in ours:  # "" as the old value: made, never moved
                self.read("update-ref", f"refs/tags/{name}", obj, "")

    # --- the review and the tag (BOOTSTRAP §9)

    def signed(self) -> str:
        """The review's done-test: vN's tag verifies on C. Whoever signed it read the review."""
        commit, _ = self.verify(self.v, self.etc / "allowed_signers")
        return "its tag verifies" if commit == self.commit else ""

    def local_tag(self) -> str:
        """The tag object that this clone's refs/tags/vN names, or ""."""
        return self.git("rev-parse", "--verify", "--quiet", f"refs/tags/{self.v}").stdout.strip()

    def theirs(self) -> str:
        """origin's vN: "" when it has none; "ours" when it is this clone's tag, which verifies;
        "other" when it is another tag of C, which stays as it is (a pushed tag is never moved),
        while this clone signs and keeps its own, never pushed: the public repository's, signed
        by its maintainer, or another signer's (BOOTSTRAP §9). A vN of another commit, origin's
        or one of this clone's that verifies, stops the run before the review: the release
        message gives another commit than the tag."""
        remote, local = self.remote_tag(), self.local_tag()
        commit, _ = self.verify(self.v, self.etc / "allowed_signers")
        if remote and remote == local and commit == self.commit:
            return "ours"
        if remote:
            named = self.git("rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}")
            if named.stdout.strip() != self.commit:
                stop(f"origin's {self.v} names {named.stdout.strip() or 'an object not fetched'},"
                     f" not {self.commit}: check the release message")  # fmt: skip
        if commit and commit != self.commit:
            clone = shlex.quote(str(self.clone))
            stop(f"this clone's {self.v} verifies on {commit}, not on {self.commit}, the release"
                 f" message's: check which is right. If {self.commit[:12]} is, delete the tag,"
                 f" never pushed: git -C {clone} tag -d {self.v}")  # fmt: skip
        return "other" if remote else ""

    def review(self) -> None:
        """This copy's deploy.sh reviews C since vP, as BOOTSTRAP §9 runs it, on every run: its
        diff must have the release message's SHA-256, the one the run ends with. It pages the
        review, then you type vN to sign it. Once vN's tag verifies on C, whoever signed it read
        the review: a rerun compares the SHA-256 again, its review written but not paged."""
        self.theirs()
        signed = self.signed()
        self.out.mkdir(parents=True, exist_ok=True)
        done = self.call(
            ["bash", self.here / "workshop/adminserver/deploy.sh", "--repo", self.clone,
             "--etc", self.etc, "--since", self.prev, "--review", self.commit, "--out", self.out],
            tty=not signed,
        )  # fmt: skip
        diff = self.out / f"zab-admin-{self.prev}..{self.commit[:12]}.diff"
        if done.returncode or not diff.is_file():
            said = done.stderr.strip()[-300:]
            stop(f"the review stopped: {said}" if signed else "the review stopped (above):"
                 " nothing was signed")  # fmt: skip
        sha = hashlib.sha256(diff.read_bytes()).hexdigest()
        if sha != self.sha:
            stop(f"this review's diff has SHA-256 {sha}, not {self.sha}, the release message's:"
                 + (" check the release message" if signed else " don't sign it. Nothing was"
                    " signed"))  # fmt: skip
        self.compared = sha
        if signed:
            self.say(f"the review's SHA-256 is the release message's, {sha}: compared again,"
                     f" its review not paged, since {self.v}'s tag verifies on it")  # fmt: skip
            return
        self.say(f"the review's SHA-256 is the release message's, {sha}.")
        self.say(
            "The driver signs, verifies and pushes the tag itself: don't paste the lines above."
        )
        self.confirm(f"sign it on {self.commit[:12]}")

    def confirm(self, what: str) -> None:
        """You type vN, or the run stops. The transcript says only whether it matched: a
        passphrase typed here by mistake goes nowhere."""
        try:
            answer = input(f"Type {self.v} to {what}: ").strip()
        except EOFError:
            answer = ""
        self.note("(typed: the version)" if answer == self.v else "(typed: something else)")
        if answer != self.v:
            stop("not confirmed")

    def signer_of(self, obj: str) -> str:
        """Who signed tag object OBJ, as BOOTSTRAP §9 has it checked: its key's fingerprint,
        whatever allowed_signers says of it, or that it has no SSH signature."""
        if "\n-----BEGIN SSH SIGNATURE-----\n" not in self.git("cat-file", "tag", obj).stdout:
            return "with no SSH signature"
        done = self.git(
            "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={self.etc}/allowed_signers",
            "-c", "gpg.ssh.program=ssh-keygen", "verify-tag", obj,
        )  # fmt: skip
        found = re.search(r" key (SHA256:[A-Za-z0-9+/]+)", done.stdout + done.stderr)
        return f"signed by {found[1]}" if found else "with a signature that doesn't check"

    def remote_tag(self) -> str:
        """The tag object that origin's refs/tags/vN names, or ""."""
        done = self.git("ls-remote", "origin", f"refs/tags/{self.v}", origin=True)
        if done.returncode:
            stop(f"git ls-remote origin: {done.stderr.strip()[:300]}")
        found = [line.split("\t") for line in done.stdout.splitlines()]
        return next((obj for obj, ref in found if ref == f"refs/tags/{self.v}"), "")

    def copy_of(self, version: str) -> Path:
        return Path.home() / f"zab-{version}"

    def tagged(self) -> str:
        """The tag's done-test: it verifies on C with a key of allowed_signers, origin has it
        (or another tag of C, `theirs`), and its copy is clean, at C."""
        if not self.signed() or not self.theirs() or self.copy_state() != "ok":
            return ""
        return f"{self.copy_of(self.v)} is its copy"

    def copy_state(self) -> str:
        """vN's worktree: "ok" when it is a clean checkout of the clone at C, "none" when there
        is nothing at its path, or what else is there."""
        path = self.copy_of(self.v)
        if not path.exists():
            return "none"
        top = self.git("rev-parse", "--show-toplevel", where=path).stdout.strip()
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir", where=path)
        theirs = self.read("rev-parse", "--path-format=absolute", "--git-common-dir")
        if top != str(path.resolve()) or common.stdout.strip() != theirs:  # git's are resolved
            return f"{path} is not a checkout of {self.clone}"
        if self.git("rev-parse", "HEAD", where=path).stdout.strip() != self.commit:
            return f"{path} is not at {self.commit}"
        if self.git("status", "--porcelain", "--untracked-files=all", where=path).stdout:
            return f"{path} has changes (git status)"
        return "ok"

    def tag(self) -> None:
        """Sign vN on C unless it verifies there already, check the signature with `tag -v`'s
        test, never git's exit status (git 2.43 exits 0 after a failed one, #772), push it
        unless origin has another tag of C, then make its copy from the tag."""
        signers, v = self.etc / "allowed_signers", self.v
        theirs = self.theirs()  # stops on a vN of another commit, origin's or this clone's
        obj = self.local_tag()
        commit, why = self.verify(v, signers)
        if obj and not commit:  # never pushed: a failed signature, or a copy of origin's
            kept = "origin's stays as it is" if theirs == "other" else "it was never pushed"
            self.say(f"{why}: deleting this clone's {v} ({kept})")
            self.read("tag", "-d", v)
            obj = ""
        if not obj:
            self.agent.ensure()
            sign = self.git(
                "-c", "gpg.format=ssh", "-c", f"user.signingkey={self.agent.key}.pub",
                "-c", "gpg.ssh.program=ssh-keygen", "tag", "-s", v, "-m", f"zab {v}", self.commit,
                user=True,
            )  # fmt: skip  # its exit status says nothing of the signature: checked below
            commit, why = self.verify(v, signers, self.agent.fingerprint)
            if commit != self.commit:
                made = self.local_tag()
                said = f" (git tag -s: {sign.stderr.strip()[:300]})" if sign.stderr.strip() else ""
                what = f"{why}: the tag was deleted, never pushed" if made else f"{v}: no tag made"
                what += said
                if made:
                    done = self.git("tag", "-d", v)
                    left = self.local_tag()  # what is left, whatever git's exit status
                    if left:
                        error = done.stderr.strip()[:300] or f"exit {done.returncode}"
                        clone = shlex.quote(str(self.clone))
                        what = (f"{why}{said}, and its delete failed (git tag -d: {error}): this"
                                f" clone's {v} is still {left}, never pushed. Delete it"
                                f" (git -C {clone} tag -d {v})")  # fmt: skip
                held = "holds" if self.agent.holds_key_alone() else "doesn't hold"
                stop(f"{what}. The agent {held} the key alone; check it (ssh-add -l),"
                     " then run this again")  # fmt: skip
            obj = self.local_tag()
            self.say(f"signed: {v}, {self.agent.fingerprint}, verified on {self.commit}")
        if theirs == "other":
            signer = self.signer_of(self.remote_tag())
            if signer.startswith("signed by "):
                check = (
                    "Check that fingerprint against the one SECURITY.md publishes (BOOTSTRAP §9)"
                )
            else:  # no fingerprint to check
                how = (
                    "has no SSH signature"
                    if signer == "with no SSH signature"
                    else "has a signature that doesn't check"
                )
                check = (f"origin's tag {how}, so there is no fingerprint to check. The public"
                         " repository's tags are signed, and in a repository you tag yourself"
                         " such a tag is an anomaly worth investigating")  # fmt: skip
            self.say(f"origin has another {v} of {self.commit[:12]}, {signer}, which stays as it"
                     f" is: this clone keeps its own, never pushed. {check}. A deploy that fetches"
                     " the tag from origin won't see this clone's")  # fmt: skip
        elif not theirs:
            push = self.git("push", "origin", f"refs/tags/{v}", origin=True)
            if push.returncode:
                stop(f"git push origin {v}: {push.stderr.strip()[:300]}. It is signed here: run"
                     " this again to push it")  # fmt: skip
            if self.remote_tag() != obj:
                stop(f"origin's {v} is not this clone's {obj}, after the push")
            self.say(f"pushed: origin's {v} is {obj}")
        state = self.copy_state()
        if state == "none":
            self.read("worktree", "add", "--detach", self.copy_of(v), f"refs/tags/{v}")
        elif state != "ok":
            stop(f"{state}: move it aside, then run this again")
        self.say(f"copy: {self.copy_of(v)}, clean, at {v}")

    # --- the run

    def preflight(self) -> None:
        """What every run checks first; nothing here changes anything but the clone's fetch."""
        missing = [tool for tool in TOOLS if not shutil.which(tool)]
        if sys.version_info < (3, 12) or missing:
            stop(f"python3 3.12 or newer, and on PATH: {' '.join(TOOLS)} (BOOTSTRAP §1)")
        for name in ("allowed_signers", "admin.toml"):  # deploy.sh's review reads both
            if not (self.etc / name).is_file():
                stop(f"no {self.etc}/{name}: the server's, as BOOTSTRAP §15's step 6 fetches it")
        self.own_copy()
        self.say(f"this copy: {self.prev}, signed, at {self.here}")
        if vkey(self.v) <= vkey(self.prev):
            stop(f"{self.v} is not newer than {self.prev}, this copy's release")
        self.fetch()
        c = self.commit
        if self.git("cat-file", "-e", f"{c}^{{commit}}").returncode:
            stop(f"{c}: no such commit in {self.clone}, after the fetch")
        if self.git("merge-base", "--is-ancestor", c, "refs/remotes/origin/main").returncode:
            stop(f"{c} is not on origin/main")
        if self.git("merge-base", "--is-ancestor", self.prev, c).returncode:
            stop(f"{self.prev} is not an ancestor of {c}: not an upgrade from this copy")
        try:
            meta = tomllib.loads(self.read("show", f"{c}:pyproject.toml"))
        except tomllib.TOMLDecodeError as exc:
            stop(f"{c}: pyproject.toml doesn't parse: {exc}")
        if meta.get("project", {}).get("version") != self.v[1:]:
            stop(f"{c}'s pyproject.toml says version {meta.get('project', {}).get('version')}")
        self.say(f"{self.v}: {c}, on origin/main, after {self.prev}")
        self.agent.ensure()
        self.say(f"key: {self.agent.fingerprint}, alone in the agent")

    def plan(self) -> None:
        self.say("plan:")
        for n, phase in enumerate(self.phases, 1):
            if phase.done is None:
                state = "every run"
            elif why := phase.done():
                state = f"done ({why})"
            else:
                state = "to do"
            self.say(f"  {n}. {phase.title}: {state}")

    def run(self) -> None:
        self.preflight()
        self.plan()
        for n, phase in enumerate(self.phases, 1):
            why = phase.done() if phase.done else None
            if why:
                self.say(f"[{n}/{len(self.phases)}] {phase.title}: done ({why})")
                continue
            self.say(f"[{n}/{len(self.phases)}] {phase.title}")
            phase.run()
        self.say(f"done: {self.v} is signed and verified on {self.commit[:12]}, and its copy is")
        self.say(f"{self.copy_of(self.v)}. Upgrade the server to it from that copy (the")
        if self.compared:  # the review phase compares it on every run
            self.say(f"adminserver README's deploy), with the review's SHA-256, {self.compared},")
            self.say("which this run compared with the release message's.")
        else:
            self.say("adminserver README's deploy).")

    def finish(self) -> None:
        """The clean-ups, on every exit, last first, with the signals held off until they end."""
        held = [signal.signal(sig, signal.SIG_IGN) for sig in SIGNALS]
        try:
            for cleanup in reversed(self.cleanups):
                try:
                    cleanup()
                except Exception as exc:  # each clean-up runs, whatever one before did
                    self.say(f"clean-up: {exc}", err=True)
        finally:
            for sig, handler in zip(SIGNALS, held, strict=True):
                signal.signal(sig, handler)
        if self.log:
            self.log.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="upgrade.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--setup", action="store_true", help="ask for the values, and keep them")
    parser.add_argument("version", nargs="?", help="vN, the release to upgrade to")
    parser.add_argument("--commit", default="", help="C, vN's full commit on main")
    parser.add_argument("--review-sha256", default="", help="the review's SHA-256")
    args = parser.parse_args(argv)
    try:
        if args.setup:
            if args.version or args.commit or args.review_sha256:
                parser.error("--setup takes nothing else")
            return setup()
        if not (args.version and VERSION.fullmatch(args.version)):
            parser.error("name the release: vN --commit C --review-sha256 SHA")
        if not COMMIT.fullmatch(args.commit) or not SHA256.fullmatch(args.review_sha256):
            parser.error("--commit takes a full commit, --review-sha256 64 hex digits")
        up = Upgrade(args.version, args.commit, args.review_sha256)
    except Stop as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    before = [signal.signal(sig, up.interrupted) for sig in SIGNALS]
    code = 1
    try:
        up.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        up.log = (up.out / "transcript.txt").open("a", encoding="utf-8")
        up.note(f"== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: {shlex.join(sys.argv)}")
        up.run()
        code = 0
    except Stop as exc:
        up.say(f"stopped: {exc}", err=True)
    except KeyboardInterrupt:
        up.say("stopped: interrupted; run the same command again to go on", err=True)
        code = 130
    finally:
        up.finish()
        for sig, handler in zip(SIGNALS, before, strict=True):
            signal.signal(sig, handler)
    return code


if __name__ == "__main__":
    sys.exit(main())
