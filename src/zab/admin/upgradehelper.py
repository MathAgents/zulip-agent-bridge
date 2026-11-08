"""The root helpers of `!upgrade-server` (docs/ADMIN.md §1.6; #569).

The bot runs as `zab-admin`, which can neither read the release repository's deploy key nor
install a release. So `!upgrade-server`'s dry run and its apply each write a request, and a path
unit starts the helper, as root, from the running release: `/usr/bin/python3 -I -B -S`, the
standard library and `current`'s `src/` alone, as the config helper runs (ADMIN §1.6, #437).
Keep its imports to `confighelper`, `config`, `checks` and the standard library
(`tests/admin/test_admin_imports.py`, `UPGRADE_IMPORTS`).

**The plan** (`zab-admin-upgrade-plan.service`, for `requests/plan.json`):
1. the request: `{"version": 1, "plan": "P-n", "nonce": <32 hex>, "release": "vX.Y.Z",
   "sha256": <64 hex>}`, taken as the config helper takes its own (`confighelper.take`).
   Nothing in it is trusted but a release name of deploy.sh's pattern and a SHA-256, which the
   review must match; the plan and the nonce only name the outcome. It never asks for a
   downgrade: the in-band upgrade only goes forward, and a deployer goes back with
   `deploy.sh --downgrade` (BOOTSTRAP §13.4);
2. `admin.toml`, through the strict loader: its `release` pattern and its `release_repo`;
3. the fetch: exactly two tags, the release asked for and the running one, which is the review's
   base, from `release_repo`, with the deploy key and GitHub's pinned host keys
   (`github-known-hosts`), into the helper's own bare repository (`repo.git`, root 0700), under
   `refs/fetched/`;
4. the checks of deploy.sh's `verify_tag`, made here again: an annotated tag of a commit, signed
   by a key of `allowed_signers`, whose own `tag` header is the name asked for, a name the
   `release` pattern allows, and a `pyproject.toml` of that version; that the release is newer
   than the running one, never the same nor older; and that the running one's tag names the
   commit deploy.sh's record (`deploys.jsonl`) has for it, as deploy.sh's `base_is_running` does.
5. the review, by the **running** release's deploy.sh (`--review C --since <running>`), the
   script and base the deployer reviews with (BOOTSTRAP §13.4: their running release's own copy
   of deploy.sh, since the server can't run the new one before it trusts it). Its SHA-256 must
   be the request's, the one the deployer saw (`!upgrade-server … sha256=`): so a deployer's
   request installs only the diff they reviewed, from the base they reviewed it from. It is no
   secret (a refused plan's message shows the right one), so it binds an honest request; a
   compromised bot is bounded by the signature and the pattern alone. Each tag enters
   the repository's `refs/tags/`, which never moves one to another commit (`adopt`: a tag whose
   commit changed in release_repo since is refused): the base's before the review, which reads
   it as `--since` (its commit is `deploys.jsonl`'s), the release's only once its SHA-256
   matched;
6. the outcome, `results/P-n.plan.json` (0644), with the nonce: the plan's text (`plan_text`:
   both tags, by object, commit, signer and key, the SHA-256 and the release tree, the
   privileged files, `render_policy.py`'s line, the review up to its unprivileged rest, the
   CHANGELOG section, sanitized), and the facts the apply checks against.

The plan helper runs in a sandbox (`zab-admin-upgrade-plan.service`): it writes only its own
state and holds one capability, to take the request from `zab-admin`'s directory. Of
`/etc/zab-admin/secrets` it sees the deploy key alone; `/var/lib/zab-admin`, the console's
state, the SSH host keys and Caddy's are hidden from it; and it can't reach IMDS.

**The apply** (`zab-admin-upgrade.service`, for `requests/apply.json`), once the plan's dry run
has run again for its hash (the runner's recheck), with the same request and the plan's commit:
1. the plan helper's record of that plan, root's (`results/P-n.plan.json`): it passed, for this
   release, commit and SHA-256, from the release that runs now;
2. the checks again, on the server's own copies of the tags (`refs/tags/`, which the plan took):
   the signatures, the release's commit, the base's against the plan and `deploys.jsonl`. It
   fetches nothing;
3. the release's own deploy.sh, from its commit, with a bundle of its tag, twice. First with
   `--build-wheel`: the release's fleet wheel, built as `zab-build` with the pinned backend and
   placed for `!rollout`, unless one is there (a failed build changes nothing, and a wheel for a
   release not yet running is harmless). Then `--server … --in-band P-n`, as a deployer's laptop
   has root run it over SSH. It checks everything once more, builds
   at the final path as `zab-build`, switches `current`, records the plan in `deploys.jsonl`,
   installs the units and restarts them, all but the bots': the bot hosts this apply, and
   restarts itself once it has posted the result (ADMIN §2.9's rule, for `!new-workshop`);
4. the outcome, `results/P-n.apply.json`: whether `current` moved, and deploy.sh's last lines.

The apply's unit has root's system, as `sudo` over SSH has: deploy.sh installs users, units,
tmpfiles entries, Caddy's configuration and the release under `/opt`. Its guard is what it runs,
a signed tag's deploy.sh after these checks, never a sandbox.

**What it can't check.** The approvals and the apply locks live in the bot's `state.db`, which
root never parses: a forged one could say anything. So a compromised `zab-admin` can ask for an
upgrade nobody approved, but only to a release signed by a key of `allowed_signers`, of the
`release` pattern, and newer than the running one.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from zab.admin.config import AdminConfig, AdminConfigError, parse_admin_config
from zab.admin.confighelper import Refused, take

USER = "zab-admin"  # who writes the requests
PLAN, APPLY = "plan", "apply"
#: deploy.sh's VERSION_RE: a release name, rc or not; admin.toml's `release` narrows it.
VERSION_RE = re.compile(
    r"v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(?:rc(0|[1-9][0-9]{0,5}))?",
    re.ASCII,
)
_PLAN = re.compile(r"P-[1-9][0-9]{0,8}", re.ASCII)
_NONCE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", re.ASCII)
#: What a path in the ssh command may be: the command is a string git hands to a shell.
_PLAIN_PATH = re.compile(r"(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)++", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
#: ssh-keygen's word for a good signature: the principal, then the key's type and fingerprint.
_GOOD = re.compile(r'Good "git" signature for (\S+) with (\S+) key (SHA256:[A-Za-z0-9+/]+)')
_REVIEWED = re.compile(r"SHA-256 ([0-9a-f]{64}); release tree ([0-9a-f]{64}) \(([0-9]+) paths")
SIGNATURE = "\n-----BEGIN SSH SIGNATURE-----\n"
#: The review's last section, the unprivileged files' diff: left out of the plan, whose SHA-256
#: covers it (the `.diff` holds it, and the deployer read it before signing).
REST = "\n==== The rest ("
POLICY = "workshop/adminserver/render_policy.py"
KNOWN_HOSTS = "workshop/adminserver/github-known-hosts"
FETCHED = "refs/fetched"  # the remote's tags as last fetched; refs/tags/, the verified ones
DEPLOYS = "deploys.jsonl"  # under /opt/zab-admin: deploy.sh's record of each install
GIT_SECONDS, REVIEW_SECONDS = 300, 600
CHANGELOG_LINES = 200
#: The apply: deploy.sh's two runs at most, the fleet wheel's build and the upgrade, each with a
#: log of its own, within the unit's TimeoutStartSec=1500; how many lines of a log are kept.
WHEEL_SECONDS, DEPLOY_SECONDS, TAIL = 300, 1080, 60
WHEEL_LOG, DEPLOY_LOG = "wheel.log", "deploy.log"
#: deploy.sh's note of the fleet wheel's SHA-256, placed or there already (#901's review, N4).
_WHEEL_SHA = re.compile(r"\bzulip_agent_bridge-\S+\.whl\b.*: SHA-256 ([0-9a-f]{64})\.$")
KEEP = 50  # results and work directories kept, the newest
#: `git archive`'s attributes, all off, as deploy.sh's server repository has them.
ATTRIBUTES = "* -export-subst -export-ignore -ident -text -eol -filter -working-tree-encoding\n"
#: The tools root runs, by their full paths (Ubuntu 24.04's), never through a PATH lookup.
GIT, BASH = "/usr/bin/git", "/usr/bin/bash"


@dataclass(frozen=True, slots=True)
class Paths:
    """Where the helpers read and write (§1.3); tests pass others."""

    state: str = "/var/lib/zab-admin-upgrade"  # root 0755 (tmpfiles.conf)
    etc: str = "/etc/zab-admin"  # admin.toml and allowed_signers: the review's --etc too
    key: str = "/etc/zab-admin/secrets/release-key"  # the deploy key, root 0600
    opt: str = "/opt/zab-admin"  # current, releases/
    #: Tests only: a local repository fetched in place of `release_repo`, over `file`; and more
    #: of deploy.sh's environment (its ZAB_DEPLOY_PREFIX, which it refuses as root).
    fetch_url: str = ""
    deploy_env: tuple[tuple[str, str], ...] = ()

    @property
    def requests(self) -> str:  # zab-admin's 0700, in root's state: the bot can't swap it
        return f"{self.state}/requests"

    @property
    def results(self) -> str:  # root 0755: the outcomes, which the bot reads
        return f"{self.state}/results"

    @property
    def repo(self) -> str:  # root 0700: the fetched tags
        return f"{self.state}/repo.git"

    @property
    def work(self) -> str:  # root 0700: each run's review, and git's HOME
        return f"{self.state}/work"


#: The in-band upgrade only goes forward: a rollback is a deployer's `deploy.sh --downgrade`.
NO_DOWNGRADE = (
    "an in-band upgrade only goes forward: go back with deploy.sh --downgrade from a "
    "deployer's machine (BOOTSTRAP §13.4)"
)


@dataclass(frozen=True, slots=True)
class Request:
    action: str
    plan: str
    nonce: str
    release: str
    sha256: str  # the reviewed diff's, as the deployer's own review printed it
    commit: str = ""  # the apply's: the commit its plan showed


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    object: str  # the tag object's ID
    commit: str
    signer: str  # the principal of allowed_signers its signature verified for
    key: str  # that key's type and fingerprint, as ssh-keygen prints them

    def line(self, what: str) -> str:
        return (
            f"{what} {self.name}: tag {self.object}, commit {self.commit}, signed by "
            f"{self.signer} with {self.key}"
        )


@dataclass(frozen=True, slots=True)
class Run:
    """One helper run: its paths, the running release, its own directory and git's HOME."""

    paths: Paths
    running: str  # vX.Y.Z, `current`'s
    current: str  # the running release's directory, `current` resolved once
    work: str
    env: dict[str, str] = field(default_factory=dict)


def _json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def identity(data: bytes) -> tuple[str, str] | None:
    """The plan and the nonce a request names, each by its pattern: whom its outcome is for,
    whatever else it holds; None when either is missing or malformed."""
    found = _json(data)
    if not isinstance(found, dict):
        return None
    plan, nonce = found.get("plan"), found.get("nonce")
    if isinstance(plan, str) and isinstance(nonce, str):
        if _PLAN.fullmatch(plan) and _NONCE.fullmatch(nonce):
            return plan, nonce
    return None


def parse(data: bytes, action: str) -> Request:
    """The request, checked for shape: every value by its pattern, nothing else read."""
    found = _json(data)
    if found is None:
        raise Refused("the request is not JSON")
    if isinstance(found, dict) and "downgrade" in found:
        raise Refused(NO_DOWNGRADE)
    keys = {"version", "plan", "nonce", "release", "sha256"}
    keys |= {"commit"} if action == APPLY else set()
    if not isinstance(found, dict) or set(found) != keys:
        raise Refused(f"the request's keys are not {', '.join(sorted(keys))}")
    if type(found["version"]) is not int or found["version"] != 1:  # not `true`, not 1.0
        raise Refused("the request is not version 1")
    patterns = [("plan", _PLAN), ("nonce", _NONCE), ("release", VERSION_RE), ("sha256", _SHA256)]
    patterns += [("commit", _COMMIT)] if action == APPLY else []
    for name, pattern in patterns:
        if not isinstance(found[name], str) or not pattern.fullmatch(found[name]):
            raise Refused(f"the request's {name} is not one")
    values = (found["plan"], found["nonce"], found["release"], found["sha256"])
    return Request(action, *values, found.get("commit", ""))


def vkey(name: str) -> tuple[int, int, int, int, int]:
    """deploy.sh's `vkey`: vX.Y.ZrcN comes before vX.Y.Z."""
    match = VERSION_RE.fullmatch(name)
    if match is None:
        raise Refused(f"{name}: not a release name")
    x, y, z, rc = match.groups()
    return int(x), int(y), int(z), 0 if rc is not None else 1, int(rc or 0)


def running(paths: Paths) -> tuple[str, str]:
    """The running release, as `current` names it, and its directory."""
    link = f"{paths.opt}/current"
    if not os.path.islink(link):
        raise Refused(f"{link} is not a link: no release is running")
    target = os.path.realpath(link)
    name = os.path.basename(target)
    if os.path.dirname(target) != os.path.realpath(f"{paths.opt}/releases"):
        raise Refused(f"{link} points outside {paths.opt}/releases")
    if not VERSION_RE.fullmatch(name):
        raise Refused(f"{link} names {name!r}, not a release")
    return name, target


def config(paths: Paths) -> AdminConfig:
    try:
        with open(f"{paths.etc}/admin.toml", encoding="utf-8") as f:
            text = f.read()
        return parse_admin_config(text, f"{paths.etc}/admin.toml")
    except OSError as exc:
        raise Refused(f"{paths.etc}/admin.toml: {exc.strerror}") from None
    except AdminConfigError as exc:
        raise Refused(f"admin.toml is refused: {exc}") from None


def git(job: Run, *args: str, timeout: float = GIT_SECONDS) -> str:
    """git in the helper's repository, with neither the system's nor root's configuration, no
    replace refs and literal pathspecs (deploy.sh's `g`); its stdout, or Refused with its own
    words for why (`said`), quoted."""
    argv = [GIT, "-C", job.paths.repo, *args]
    try:
        done = subprocess.run(  # noqa: S603 - an argument list
            argv, env=job.env, capture_output=True, text=True, errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        raise Refused(f"git {_verb(args)} took over {timeout:.0f} s") from None
    if done.returncode != 0:
        raise Refused(f"git {_verb(args)} said {said(done.stderr) or f'exit {done.returncode}'}")
    return done.stdout


def git_bytes(job: Run, *args: str) -> bytes:
    """`git`, its stdout as bytes, never decoded (review of #894, B1): what is written, run,
    hashed or compared as deploy.sh reads it (a blob to install, a tag object's header, a
    `pyproject.toml`), with no newline or byte rewritten by a text mode."""
    argv = [GIT, "-C", job.paths.repo, *args]
    try:
        done = subprocess.run(  # noqa: S603 - an argument list
            argv, env=job.env, capture_output=True, timeout=GIT_SECONDS,
            stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        raise Refused(f"git {_verb(args)} took over {GIT_SECONDS} s") from None
    if done.returncode != 0:
        why = said(done.stderr.decode("utf-8", "replace")) or f"exit {done.returncode}"
        raise Refused(f"git {_verb(args)} said {why}")
    return done.stdout


def _verb(args: tuple[str, ...]) -> str:
    return next((a for a in args if not a.startswith("-") and "=" not in a), "?")


def said(stderr: str) -> str:
    """What a git or ssh error says, as one quoted string (JSON's quoting: no text from the
    remote makes a line of its own, or reads as anything but quoted; review of #889, N5). The
    cause stands before git's first `fatal:` line, which it keeps: over SSH, git ends every
    failure with the same advice, `Please make sure you have the correct access rights / and
    the repository exists.`, after a blank line, whatever the cause (review of #889, B1). The
    last two lines up to that `fatal:`, or of the whole when there is none."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    fatal = next((i for i, line in enumerate(lines) if line.startswith("fatal:")), None)
    kept = lines[: fatal + 1] if fatal is not None else lines
    return json.dumps(" ".join(kept[-2:])[:400], ensure_ascii=True) if kept else ""


def ensure_repo(job: Run) -> None:
    """The helper's bare repository, root's alone, with `git archive`'s attributes all off."""
    if not os.path.isdir(job.paths.repo):
        done = subprocess.run(  # noqa: S603 - an argument list
            [GIT, "init", "-q", "--bare", "--template=", job.paths.repo], env=job.env,
            capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise Refused(f"the release repository: {said(done.stderr)}")
    os.chmod(job.paths.repo, 0o700)
    os.makedirs(f"{job.paths.repo}/info", mode=0o700, exist_ok=True)
    with open(f"{job.paths.repo}/info/attributes", "w", encoding="ascii") as f:
        f.write(ATTRIBUTES)


def ssh_command(job: Run) -> str:
    """The ssh git runs: the deploy key alone, no agent, no configuration, GitHub's pinned host
    keys, no prompt. A string that git hands to a shell, so it is built from fixed paths only."""
    hosts = f"{job.current}/{KNOWN_HOSTS}"
    for path in (job.paths.key, hosts):
        if not _PLAIN_PATH.fullmatch(path):
            raise Refused(f"{path}: not a plain path")
    if not os.path.isfile(job.paths.key):
        raise Refused(
            f"no deploy key at {job.paths.key}: a deployer makes it, and adds its .pub to "
            "release_repo's deploy keys, read-only (BOOTSTRAP §13.4)"
        )
    if not os.path.isfile(hosts):
        raise Refused(f"no {KNOWN_HOSTS} in the running release")
    options = (
        "IdentitiesOnly=yes", "IdentityAgent=none", f"UserKnownHostsFile={hosts}",
        "GlobalKnownHostsFile=/dev/null", "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
        "BatchMode=yes", "ConnectTimeout=30", "ServerAliveInterval=15", "ServerAliveCountMax=4",
        "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
    )  # fmt: skip
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-i", job.paths.key]
    for option in options:
        argv += ["-o", option]
    return shlex.join(argv)


def fetch(job: Run, url: str, names: tuple[str, ...]) -> None:
    """Exactly `names`, tags, from `url`, into `refs/fetched/`: each forced there, so that it
    holds the remote's tags as they are now; the server's own `refs/tags/` takes a tag only once
    it has verified, and never moves it (`adopt`)."""
    protocol = "file" if job.paths.fetch_url else "ssh"
    setting = ["-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true"]
    setting += ["-c", "protocol.allow=never", "-c", f"protocol.{protocol}.allow=always"]
    if protocol == "ssh":
        setting += ["-c", f"core.sshCommand={ssh_command(job)}"]
    specs = [f"+refs/tags/{name}:{FETCHED}/{name}" for name in names]
    flags = ["--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules"]
    try:
        git(job, *setting, "fetch", *flags, url, *specs)
    except Refused as exc:
        raise Refused(f"the fetch of {', '.join(names)} from {url} failed: {exc}") from None


def verify(
    job: Run, name: str, signers: str, cfg: AdminConfig | None = None, where: str = FETCHED
) -> Tag:
    """deploy.sh's `verify_tag`, made again (module doc, step 4): with `cfg`, the release asked
    for, which its `release` pattern must allow; without, a base, as deploy.sh checks one. Of
    the tag `where` holds: release_repo's as just fetched, or for the apply, the server's own
    copy (`refs/tags`), which the plan took."""
    if not VERSION_RE.fullmatch(name):
        raise Refused(f"{name}: not a release name (vX.Y.Z or vX.Y.ZrcN)")
    if cfg is not None and not cfg.release_pattern.fullmatch(name):
        raise Refused(f'{name}: a release candidate, and admin.toml has release = "{cfg.release}"')
    if not os.path.isfile(signers) or not os.path.getsize(signers):
        raise Refused(f"{signers}: no allowed signers")
    try:
        obj = git(job, "rev-parse", "--verify", "--quiet", f"{where}/{name}").strip()
    except Refused:
        held = "release_repo" if where == FETCHED else "the server's copy (no plan took it)"
        raise Refused(f"{name}: no such tag in {held}") from None
    if git(job, "cat-file", "-t", obj).strip() != "tag":
        raise Refused(f"{name}: a lightweight tag, which has no signature")
    text = git_bytes(job, "cat-file", "tag", obj)  # bytes, as deploy.sh reads it (#894, B1)
    header = text.split(b"\n\n", 1)[0].split(b"\n")

    def one(field_name: str) -> str:
        start = field_name.encode("ascii") + b" "
        found = [line[len(start) :] for line in header if line.startswith(start)]
        return found[0].decode("ascii", "replace") if len(found) == 1 else ""

    if one("tag") != name:
        raise Refused(
            f"refs/tags/{name} holds a tag object named otherwise: a renamed ref is refused"
        )
    if one("type") != "commit":
        raise Refused(f"{name}: not a tag of a commit")
    commit = one("object")
    if not _COMMIT.fullmatch(commit):
        raise Refused(f"{name}: no commit")
    if SIGNATURE.encode("ascii") not in text:
        raise Refused(f"{name}: not signed with an SSH key")
    signed = [
        "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}",
        "-c", "gpg.ssh.program=/usr/bin/ssh-keygen", "-c", "gpg.program=false",
        "-c", "gpg.x509.program=false",
    ]  # fmt: skip
    done = subprocess.run(  # noqa: S603 - an argument list
        [GIT, "-C", job.paths.repo, *signed, "verify-tag", obj], env=job.env,
        capture_output=True, text=True, errors="replace", timeout=GIT_SECONDS,
        stdin=subprocess.DEVNULL, check=False,
    )  # fmt: skip
    out = done.stdout + done.stderr
    good = _GOOD.search(out)
    if done.returncode != 0 or good is None:
        raise Refused(f"{name}: not signed by a key in {signers}: {said(out)}")
    try:
        project = git_bytes(job, "cat-file", "blob", f"{commit}:pyproject.toml")
    except Refused:
        raise Refused(f"{name}: no pyproject.toml") from None
    if f'\nversion = "{name[1:]}"\n'.encode("ascii") not in b"\n" + project + b"\n":
        raise Refused(f"{name}: pyproject.toml has another version")
    return Tag(name, obj, commit, good.group(1), f"{good.group(2)} key {good.group(3)}")


def deployed(paths: Paths, version: str) -> str:
    """The commit deploy.sh's record names for `version`: its last bootstrap or upgrade to it,
    as deploy.sh's `base_is_running` reads it (#587). "" when none does."""
    commit = ""
    try:
        with open(f"{paths.opt}/{DEPLOYS}", encoding="utf-8") as f:
            for line in f:
                found = _json(line.encode("utf-8"))
                if not isinstance(found, dict) or found.get("version") != version:
                    continue
                if found.get("event") in ("bootstrap", "upgrade"):
                    value = found.get("commit")
                    commit = value if isinstance(value, str) and _COMMIT.fullmatch(value) else ""
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise Refused(f"{paths.opt}/{DEPLOYS}: {exc.strerror}") from None
    return commit


def adopt(job: Run, tag: Tag) -> None:
    """`refs/tags/<name>`, the server's own copy of a verified tag: made the first time, and
    never moved to another commit (review of #889). A released tag never moves: one whose commit
    changed in release_repo since the server took it is refused, every time, with both commits.
    One signed again for the same commit, by a key of `allowed_signers` (it has just verified),
    replaces the server's copy: a deployer's own signature of a release the maintainer signed,
    after `release_repo` moved to the deployer's mirror (BOOTSTRAP §9), changes no code."""
    ref = f"refs/tags/{tag.name}"
    try:
        was = git(job, "rev-parse", "--verify", "--quiet", ref).strip()
    except Refused:
        git(job, "update-ref", ref, tag.object, "")  # "": none there
        return
    if was == tag.object:
        return
    took = git(job, "rev-parse", "--verify", "--quiet", f"{was}^{{commit}}").strip()
    if took != tag.commit:
        raise Refused(
            f"{tag.name} moved in release_repo: the server took it at commit {took}, and "
            f"release_repo now has it at {tag.commit}; a released tag never moves to another "
            "commit, so ask its signer why (BOOTSTRAP §13.4)"
        )
    git(job, "update-ref", ref, tag.object, was)  # only over the copy it read


@dataclass(frozen=True, slots=True)
class Review:
    diff_sha256: str
    tree_sha256: str
    paths: int  # release paths
    privileged: tuple[str, ...]  # `M workshop/adminserver/deploy.sh`, as deploy.sh notes them
    text: str  # the review as deploy.sh wrote it, sanitized


def review(job: Run, base: str, commit: str) -> Review:
    """The running release's `deploy.sh --review` of `commit` since `base` (module doc, step
    5), written into the run's directory."""
    out = f"{job.work}/review"
    os.mkdir(out, 0o700)
    script = f"{job.current}/workshop/adminserver/deploy.sh"
    argv = [BASH, script, "--review", commit, "--since", base, "--etc", job.paths.etc]
    argv += ["--repo", job.paths.repo, "--out", out]
    try:
        done = subprocess.run(  # noqa: S603 - an argument list
            argv, env=job.env, capture_output=True, text=True, errors="replace",
            timeout=REVIEW_SECONDS, stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        raise Refused(f"the review took over {REVIEW_SECONDS} s") from None
    died = [line for line in done.stderr.splitlines() if line.startswith("deploy.sh: ")]
    if done.returncode != 0:
        raise Refused(f"the review failed: {said(died[-1] if died else done.stderr)}")
    found = _REVIEWED.search(done.stderr)
    diffs = [name for name in sorted(os.listdir(out)) if name.endswith(".diff")]
    if found is None or len(diffs) != 1:
        raise Refused("the review printed no SHA-256, or wrote no single diff")
    with open(f"{out}/{diffs[0]}", "rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != found.group(1):
            raise Refused("the review's SHA-256 is not its diff's")
    name = f"{out}/{diffs[0].removesuffix('.diff')}.review"
    with open(name, "rb") as f:  # deploy.sh sanitized it; read as it reads (review of #893, N-a)
        text = "".join(line + "\n" for line in sanitized(f.read()))
    noted = done.stderr.splitlines()
    privileged = tuple(line.removeprefix("    P ") for line in noted if line.startswith("    P "))
    return Review(found.group(1), found.group(2), int(found.group(3)), privileged, text)


#: Hangul fillers, which render as nothing (deploy.sh's `sanitize` names them too).
_FILLERS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0)))
#: The default-ignorable code points (Unicode's DerivedCoreProperties) that no category above
#: names: combining marks, the variation selectors, and the unassigned ones reserved as
#: ignorable, which perl's `\p{Default_Ignorable_Code_Point}` in `sanitize` shows too.
_IGNORABLE = ((0x034F, 0x034F), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x2060, 0x206F),
              (0xFE00, 0xFE0F), (0xFFF0, 0xFFF8), (0xE0000, 0xE0FFF))  # fmt: skip
#: An ASCII line's controls, but the tab, as `^X`.
_CONTROLS = {c: "^" + chr(c ^ 0x40) for c in (*range(0x20), 0x7F) if c != 0x09}


def shown(line: str) -> str:
    """`line` with what a reader wouldn't see made visible, as deploy.sh's `sanitize` shows the
    review (review of #893, N2): a control but the tab as `^X`; a format character (bidi
    overrides, zero widths), private use, a surrogate, a line or paragraph separator, a space
    but the ASCII one, a Hangul filler or a variation selector as `<U+XXXX>`."""
    if line.isascii():
        return line.translate(_CONTROLS)
    out = []
    for c in line:
        code, kind = ord(c), unicodedata.category(c)
        hidden = kind in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") or (kind == "Zs" and c != " ")
        hidden |= c in _FILLERS or any(low <= code <= high for low, high in _IGNORABLE)
        if c == "\t" or not hidden:
            out.append(c)
        elif code < 0x20 or code == 0x7F:
            out.append("^" + chr(code ^ 0x40))
        else:
            out.append(f"<U+{code:04X}>")
    return "".join(out)


def sanitized(data: bytes) -> list[str]:
    """`data`'s lines as deploy.sh's `sanitize` shows them (review of #893, N-a): split on `\n`
    alone, as `perl -n` splits, so that CR, VT, FF, NEL and U+2028/U+2029 stay in their line,
    shown, and never start one (a `## ` after a U+2028 is no heading); a line that is not UTF-8
    with each byte above 0x7F as `<XX>`; then `shown`."""
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()  # a final newline ends the last line; it doesn't start another
    out = []
    for raw in lines:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            line = "".join(f"<{b:02X}>" if b > 0x7F else chr(b) for b in raw)
        out.append(shown(line))
    return out


def changelog(job: Run, commit: str, name: str) -> list[str]:
    """The release's section of `CHANGELOG.md`: `## X.Y.Z …`, or for a release candidate with
    none yet, `## Unreleased`; at most `CHANGELOG_LINES` of it."""
    try:  # the blob's bytes, shown line by line (review of #893, N-a)
        lines = sanitized(git_bytes(job, "show", f"{commit}:CHANGELOG.md"))
    except Refused:
        return ["(the release has no CHANGELOG.md)"]
    final = re.sub(r"rc[0-9]+$", "", name[1:])
    heads = [f"## {final}", *(["## Unreleased"] if final != name[1:] else [])]
    for head in heads:
        starts = [i for i, line in enumerate(lines) if line == head or line.startswith(head + " ")]
        if starts:
            section = [lines[starts[0]]]
            for line in lines[starts[0] + 1 :]:
                if line.startswith("## "):
                    break
                section.append(line)
            if len(section) > CHANGELOG_LINES:
                more = len(section) - CHANGELOG_LINES
                section = [*section[:CHANGELOG_LINES], f"… {more} more lines in CHANGELOG.md"]
            return section
    return [f"(CHANGELOG.md has no section {heads[0]})"]


def policy_line(job: Run, base: Tag, release: Tag) -> str:
    """Whether `render_policy.py` changed: the server never renders the role's policy for the
    account owner (§2.9), so a change is the deployer's to render on their own machine."""

    def blob(commit: str) -> str:
        try:
            return git(job, "rev-parse", "--verify", "--quiet", f"{commit}:{POLICY}").strip()
        except Refused:
            return ""

    if blob(base.commit) == blob(release.commit):
        return f"render_policy.py: unchanged since {base.name}"
    return (
        f"render_policy.py: CHANGED since {base.name}: render the role's policy from this release "
        "on your own machine (BOOTSTRAP §13.4, each release's step 3) and compare it with the "
        "role's before you approve"
    )


def plan_text(base: Tag, release: Tag, seen: Review, lines: list[str], policy: str) -> str:
    """The plan's dry run: what to compare and read first (the bot shows 40 lines), then the
    review without its unprivileged rest, then the CHANGELOG section."""
    c12 = release.commit[:12]  # always newer than the base: check() refused an older one
    out = [
        f"!upgrade-server {release.name}, from {base.name}: newer than {base.name}",
        release.line("release"),
        base.line("base, running"),
        f"the reviewed diff, {base.name}..{c12}, SHA-256 {seen.diff_sha256}; release tree "
        f"{seen.tree_sha256} ({seen.paths} paths)",
        f"  the sha256= you gave: the same (your deploy.sh --review {c12} --since {base.name})",
        f"privileged files changed: {len(seen.privileged)}",
        *(f"  P {line}" for line in seen.privileged),
        policy,
        "",
    ]
    cut = seen.text.split(REST, 1)
    rest = "" if len(cut) == 1 else " (its unprivileged rest is left out: the SHA-256 covers it)"
    out += [f"==== The review{rest} ====", cut[0].rstrip("\n"), ""]
    out += [f"==== CHANGELOG.md, {release.name[1:]} ====", *lines]
    return "\n".join(out) + "\n"


def check(job: Run, request: Request) -> dict[str, Any]:
    """Every check of a plan (module doc, steps 2 to 5): its outcome's fields."""
    cfg = config(job.paths)
    url = job.paths.fetch_url or cfg.release_repo
    if not url:
        raise Refused(
            "admin.toml has no release_repo: the in-band upgrade is off on this server, so "
            "deploy with deploy.sh (BOOTSTRAP §13)"
        )
    if request.release == job.running:
        raise Refused(f"{request.release} is running already")
    if vkey(request.release) < vkey(job.running):
        raise Refused(f"{request.release} is older than {job.running}, which runs: {NO_DOWNGRADE}")
    ensure_repo(job)
    fetch(job, url, (request.release, job.running))
    signers = f"{job.paths.etc}/allowed_signers"
    release = verify(job, request.release, signers, cfg)
    base = verify(job, job.running, signers)
    recorded = deployed(job.paths, base.name)
    if recorded != base.commit:  # the review's base is the commit running: never another's
        raise Refused(
            f"{base.name} is {base.commit} in release_repo, but "
            f"{recorded or 'no commit'} in {job.paths.opt}/{DEPLOYS}, which runs: "
            "not the release the server runs"
        )
    adopt(job, base)  # the review's --since: its commit is the one running, whatever the plan
    seen = review(job, base.name, release.commit)
    if seen.diff_sha256 != request.sha256:
        raise Refused(
            f"the reviewed diff {base.name}..{release.commit[:12]} has SHA-256 "
            f"{seen.diff_sha256}, not the sha256= you gave, {request.sha256}: review it with "
            f"{base.name}'s own deploy.sh, which runs here, as `deploy.sh --review "
            f"{release.commit} --since {base.name}` (BOOTSTRAP §13.4), and give its SHA-256; "
            "nothing was planned"
        )
    adopt(job, release)  # only once its review is the deployer's (review of #889, N3)
    lines = changelog(job, release.commit, release.name)
    text = plan_text(base, release, seen, lines, policy_line(job, base, release))
    return {
        "release": release.name, "commit": release.commit, "signer": release.signer,
        "running": base.name, "base_commit": base.commit,
        "diff_sha256": seen.diff_sha256, "tree_sha256": seen.tree_sha256, "text": text,
    }  # fmt: skip


def recorded_plan(paths: Paths, request: Request) -> dict[str, Any]:
    """The plan helper's outcome for the request's plan, root's own record (module doc, the
    apply): it passed, for this release, commit and SHA-256."""
    path = f"{paths.results}/{request.plan}.{PLAN}.json"
    try:
        with open(path, encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError):
        raise Refused(f"{request.plan} has no check on record ({path}): request it again") from None
    if not isinstance(found, dict) or found.get("ok") is not True:
        raise Refused(f"{request.plan}'s check did not pass: there is nothing to apply")
    asked = {"release": request.release, "commit": request.commit, "diff_sha256": request.sha256}
    for name, value in asked.items():
        if found.get(name) != value:
            raise Refused(f"the request's {name} is not the one {request.plan}'s check recorded")
    return found


def install(job: Run, request: Request) -> dict[str, Any]:
    """The apply (module doc): its plan's record and every check again, on the server's own
    copies of the tags, then the release's own deploy.sh `--server`, with `--in-band`."""
    cfg = config(job.paths)
    if not (job.paths.fetch_url or cfg.release_repo):  # the off switch, here too (#894, N4)
        raise Refused("admin.toml has no release_repo: the in-band upgrade is off on this server")
    planned = recorded_plan(job.paths, request)
    if planned.get("running") != job.running:
        raise Refused(
            f"{request.plan} was planned from {planned.get('running')}, and the server runs "
            f"{job.running} now: request it again"
        )
    signers = f"{job.paths.etc}/allowed_signers"
    release = verify(job, request.release, signers, cfg, where="refs/tags")
    base = verify(job, job.running, signers, where="refs/tags")
    if release.commit != request.commit:
        raise Refused(f"{release.name} is {release.commit} here, not {request.commit}")
    if base.commit != planned.get("base_commit") or deployed(job.paths, base.name) != base.commit:
        raise Refused(f"{base.name} is not the commit its plan and deploys.jsonl name")
    payload = f"{job.work}/payload"
    os.mkdir(payload, 0o700)
    blob = f"{release.commit}:workshop/adminserver/deploy.sh"
    with open(f"{payload}/deploy.sh", "xb") as f:  # the blob's bytes, never text (#894, B1)
        f.write(git_bytes(job, "cat-file", "blob", blob))
    written = git(job, "hash-object", "--no-filters", "-t", "blob", f"{payload}/deploy.sh")
    if written.strip() != git(job, "rev-parse", "--verify", blob).strip():
        raise Refused(f"the payload's deploy.sh is not {release.name}'s")
    git(job, "bundle", "create", "-q", f"{payload}/release.bundle", f"refs/tags/{release.name}")
    argv = [BASH, f"{payload}/deploy.sh", "--server", payload, "--version", release.name]
    argv += ["--commit", release.commit, "--manifest", str(planned.get("tree_sha256"))]
    # first the release's fleet wheel, built here as zab-build unless one is there (#569): a
    # failed build changes nothing, and a wheel for a release not yet running is harmless
    code = deploy(job, [*argv, "--build-wheel"], WHEEL_SECONDS, WHEEL_LOG)
    now = os.path.basename(os.path.realpath(f"{job.paths.opt}/current"))
    if code != 0:
        tail = logged(job, WHEEL_LOG)
        how = f"exit {code}" if code is not None else f"stopped after {WHEEL_SECONDS} s"
        return {"switched": False, "current": now, "log": tail, "release": release.name,
                "ok": False, "message": f"the fleet wheel was not built ({how}), so nothing "
                f"was installed: {now} runs, as before: {said(chr(10).join(tail))}"}  # fmt: skip
    # the placed wheel's SHA-256, for the deployer to compare with their own build (#901's
    # review, N4): deploy.sh's last note says it, placed or there already
    noted = [found[1] for line in logged(job, WHEEL_LOG) if (found := _WHEEL_SHA.search(line))]
    wheel_sha = noted[-1] if noted else ""
    argv += ["--base", base.name, "--diff-sha256", request.sha256, "--in-band", request.plan]
    code = deploy(job, argv, DEPLOY_SECONDS, DEPLOY_LOG)
    tail = logged(job, DEPLOY_LOG)
    now = os.path.basename(os.path.realpath(f"{job.paths.opt}/current"))
    facts = {"switched": now == release.name, "current": now, "log": tail, "release": release.name}
    facts["wheel_sha256"] = wheel_sha
    if code == 0:
        sha = f" (SHA-256 {wheel_sha})" if wheel_sha else ""
        return {**facts, "message": (
            f"{release.name} is installed and current, with its fleet wheel{sha}, and its units "
            "restarted, but the bots', which restart themselves into it"
        )}  # fmt: skip
    how = f"exit {code}" if code is not None else f"stopped after {DEPLOY_SECONDS} s"
    if now == release.name:
        why = f"{release.name} is current, but its deploy.sh failed after the switch ({how})"
    else:
        why = f"deploy.sh failed ({how}) before the switch: {now} runs, as before"
    return {**facts, "ok": False, "message": f"{why}: {said(chr(10).join(tail))}"}


def logged(job: Run, name: str) -> list[str]:
    """A deploy.sh run's last `TAIL` lines, to show, never to run (`shown`)."""
    with open(f"{job.work}/{name}", "rb") as f:  # as the CHANGELOG (review of #893, N-a)
        return sanitized(f.read())[-TAIL:]


def deploy(job: Run, argv: list[str], seconds: float, log: str) -> int | None:
    """`argv` (deploy.sh's server half) in a session of its own, its output in the run's `log`;
    its exit code, or None when it outlived `seconds` and was stopped: its process group
    SIGTERMed, then SIGKILLed. deploy.sh's `as_user` steps run in sessions of their own: its
    EXIT trap ends them (`end_build_processes`), or at the latest systemd, which ends what is
    left of the unit once this oneshot has written its outcome (#894, N3)."""
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": job.env["HOME"]}
    env.update(job.paths.deploy_env)
    with open(f"{job.work}/{log}", "wb") as out:
        proc = subprocess.Popen(  # noqa: S603 - an argument list
            argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        try:
            return proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 30)):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, sig)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=grace)
                    break
            return None


def _write(path: str, data: dict[str, Any]) -> None:
    """`data` as JSON at `path`, 0644, by a rename: the bot never reads half of it."""
    new = f"{path}.new"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(new)
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o644)  # the units' UMask=0077 makes it 0600; the bot reads it
        json.dump(data, f, ensure_ascii=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(new, path)


def _prune(directory: str, keep: int = KEEP) -> None:
    """The newest `keep` entries of `directory` stay; the others go. Best effort."""
    with contextlib.suppress(OSError):
        names = [n for n in os.listdir(directory) if not n.endswith(".new")]
        names.sort(key=lambda n: os.lstat(f"{directory}/{n}").st_mtime_ns, reverse=True)
        for name in names[keep:]:
            path = f"{directory}/{name}"
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(path)


def start(paths: Paths, name: str) -> Run:
    """The run's own directory under `work/` (git's HOME, the review), fresh; and the running
    release, resolved once."""
    version, current = running(paths)
    os.makedirs(paths.work, mode=0o700, exist_ok=True)
    work = f"{paths.work}/{name}"
    shutil.rmtree(work, ignore_errors=True)
    os.mkdir(work, 0o700)
    os.mkdir(f"{work}/home", 0o700)
    os.mkdir(f"{work}/temp", 0o700)
    env = {
        "PATH": "/usr/bin:/bin", "HOME": f"{work}/home", "LANG": "C.UTF-8", "LC_ALL": "C",
        "TMPDIR": f"{work}/temp", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LITERAL_PATHSPECS": "1", "GIT_TERMINAL_PROMPT": "0",
    }  # fmt: skip
    return Run(paths, version, current, work, env)


def run(action: str, paths: Paths, owner: int) -> int:
    """One request of `action`: 0 when there was none or it passed, 1 when it was refused. The
    outcome goes to `results/<plan>.<action>.json`, with the request's nonce, or, for a request
    whose plan and nonce can't be read (`identity`), to `results/unread.<action>.json`; and to the
    journal."""
    named: tuple[str, str] | None = None
    try:
        data = take(paths, owner, f"{action}.json")
        if data is None:
            return 0
        named = identity(data)
        request = parse(data, action)
        job = start(paths, f"{request.plan}.{action}")
        done = check(job, request) if action == PLAN else install(job, request)
        outcome = {"ok": True, "message": "the checks passed", **done}
    except Refused as exc:
        outcome = {"ok": False, "message": str(exc)}
    except OSError as exc:
        outcome = {"ok": False, "message": f"{exc.filename or 'a file'}: {exc.strerror}"}
    except Exception as exc:  # never without an outcome (the config helper's rule)
        outcome = {"ok": False, "message": f"internal error: {type(exc).__name__}"}
    plan, nonce = named or (None, None)
    where = f"{plan or 'unread'}.{action}"
    said = {"version": 1, "action": action, "plan": plan, "nonce": nonce, **outcome}
    print(f"zab-admin-upgrade {action}: {where}: {said['message']}", file=sys.stderr)
    _write(f"{paths.results}/{where}.json", said)
    _prune(paths.results)
    _prune(paths.work)
    return 0 if said["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    """The helpers' units, as root (module doc): each ExecStart imports this and calls it."""
    parser = argparse.ArgumentParser(prog="zab-admin-upgrade")
    parser.add_argument("action", choices=[PLAN, APPLY])
    options = parser.parse_args(argv)
    return run(options.action, Paths(), pwd.getpwnam(USER).pw_uid)
