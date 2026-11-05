"""Git helpers: GitContext, snapshots and turn diffs (SPEC §10.2, §10.4-§10.6).

Every git invocation goes through :func:`git`, which runs the argument vector through the
``Workspace`` (never a shell). Callers put ``--`` before any path arguments (SPEC §10.6).
The bridge never fetches, pulls, pushes or commits: nothing in this module writes to the
repository except :func:`ensure_zab_excluded`, which touches only ``info/exclude``.

Hardening (issue #36, SPEC §10.6). The bridge's own ``stash create``, ``diff`` and
``worktree add`` run in a repository the agent controls, so :func:`git` passes, on every
call: ``-c core.fsmonitor=false`` and ``-c core.hooksPath=/dev/null`` (no fsmonitor daemon,
no hooks); ``--attr-source=<the empty tree>`` (git 2.41 or newer; the id depends on the
repository's object format), so no ``.gitattributes`` of the checkout can name a clean or
smudge filter or a diff driver (measured on git 2.43: without it ``diff``, ``stash create``,
``diff --name-only`` and ``worktree add`` all ran a configured filter); and
``-c diff.ignoreSubmodules=dirty``, so a diff never descends into a submodule's own
repository. :func:`diff` adds ``--no-textconv``. ``LocalWorkspace.run`` drops inherited
``GIT_*`` variables. A workspace can be *pinned*: :func:`pin_workspace` records
``--git-dir`` and ``--work-tree`` for a root once, every later call for that root passes
them, and the pins are persisted (:func:`configure_pins`) so that after a bridge restart the
stored pin wins over whatever ``.git`` the agent has planted in the meantime. What remains:
``$GIT_DIR/info/attributes`` is read whatever ``--attr-source`` says, so an agent that writes
inside ``.git`` can still name a filter there. On the development machine and the workshop VMs the
agent is unsandboxed and all of this is defence in depth; on the Codex development machine, where
Codex runs in the ``workspace-write`` sandbox, a repository-controlled command run by the
unsandboxed bridge would be a sandbox escape, which is why the flags are unconditional.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat as statmod
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from zab.core.types import (
    CommandResult,
    DiffFile,
    DiffResult,
    Snapshot,
    SnapshotRef,
    Workspace,
)
from zab.workspace.denylist import upload_denied_reason

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")

#: The tree with no entries (``git hash-object -t tree /dev/null``), which exists in every
#: repository: reading attributes from it means no ``.gitattributes`` applies. Its id is the
#: hash of ``tree 0\0`` under the repository's object format, so a SHA-256 repository has
#: another one (codex round 5 on PR #50: the SHA-1 id makes ``diff`` and ``stash create``
#: fail there with "bad --attr-source").
EMPTY_TREES: dict[str, str] = {
    fmt: hashlib.new(fmt, b"tree 0\0").hexdigest() for fmt in ("sha1", "sha256")
}

#: ``--attr-source`` arrived in git 2.41 (its release notes); an older git rejects every
#: call that carries it, so :func:`git` refuses to run on one, with a clear message.
MIN_GIT_VERSION: tuple[int, int] = (2, 41)

#: Options forced on every git call (see the module docstring); ``--attr-source`` is added
#: by :func:`git` with the id for the repository's object format.
HARDENING: tuple[str, ...] = (
    "-c",
    "core.quotepath=off",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "diff.ignoreSubmodules=dirty",
)


@dataclass(frozen=True, slots=True)
class GitPin:
    """The repository a workspace root was bound to when it was prepared or first used."""

    git_dir: str  # absolute, e.g. /p/.git or /p/.git/worktrees/<name>
    work_tree: str  # absolute, the repository or worktree root
    object_format: str = "sha1"  # the repository's hash: selects the empty tree id


_PINS: dict[str, GitPin] = {}
_PINS_FILE: str | None = None
_VERIFIED: set[str] = set()  # roots whose stored pin was checked against the tree once
_FORMATS: dict[str, str] = {}  # object format of unpinned roots, once queried successfully
_GIT_PROBED = False
_GIT_VERSION: tuple[int, int, int] | None = None


def parse_git_version(text: str) -> tuple[int, int, int] | None:
    """``git version 2.43.0`` (or ``2.39.5 (Apple Git-154)``) → ``(2, 43, 0)``; else None."""
    m = re.search(r"git version (\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _too_old(version: tuple[int, int, int] | None) -> str | None:
    """The refusal message for a git older than ``MIN_GIT_VERSION``, else None."""
    if version is None or version[:2] >= MIN_GIT_VERSION:
        return None
    have = ".".join(str(n) for n in version)
    need = ".".join(str(n) for n in MIN_GIT_VERSION)
    return f"git {have} is older than {need}, the oldest the bridge supports (--attr-source)"


async def git_version_check(git_path: str = "git") -> tuple[bool, str]:
    """For ``zab doctor``: whether the installed git is new enough, and a one-line detail."""
    try:
        proc = await asyncio.create_subprocess_exec(
            git_path,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, TimeoutError) as exc:
        return False, f"git --version failed: {type(exc).__name__}"
    version = parse_git_version(out.decode("utf-8", "replace"))
    if version is None:
        return False, "git --version printed no version"
    have = ".".join(str(n) for n in version)
    why = _too_old(version)
    return (
        (False, why)
        if why
        else (True, f"{have} (needs {MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]} or newer)")
    )


async def _version_gate(ws: Workspace) -> CommandResult | None:
    """Once per process, learn the git version; a too-old git gives a failing result."""
    global _GIT_PROBED, _GIT_VERSION
    if not _GIT_PROBED:
        try:
            res = await ws.run(["git", "--version"])
            _GIT_VERSION = parse_git_version(res.stdout) if res.exit_code == 0 else None
        except (TimeoutError, OSError):
            return None  # the real call will fail the same way and be degraded there
        _GIT_PROBED = True
        why = _too_old(_GIT_VERSION)
        if why:
            logger.error("%s; every workspace git call is refused", why)
    why = _too_old(_GIT_VERSION)
    if why is None:
        return None
    return CommandResult(exit_code=1, stdout="", stderr=why)


async def _object_format(ws: Workspace, cwd: str | None) -> str:
    """The object format of the repository a call will run in: the pin's, else queried once
    per root with ``rev-parse --show-object-format`` (not cached on failure)."""
    pin = _PINS.get(ws.root)
    if pin is not None:
        return pin.object_format
    fmt = _FORMATS.get(ws.root)
    if fmt is not None:
        return fmt
    res = await ws.run(["git", *HARDENING, "rev-parse", "--show-object-format"], cwd=cwd)
    fmt = res.stdout.strip()
    if res.exit_code != 0 or fmt not in EMPTY_TREES:
        return "sha1"  # not a repository (the call fails anyway) or an unknown format
    _FORMATS[ws.root] = fmt
    return fmt


def configure_pins(path: str | None) -> None:
    """Persist pins in the JSON file ``path`` (``None``: in memory only, as in tests).

    Called by the workspace manager with ``<state_dir>/workspace-pins.json``. Entries already
    in the file are loaded now and take precedence over anything pinned in memory, so after
    a bridge restart a workspace is bound to the repository it was prepared with, not to what
    its directory shows at that moment (an agent can replace a worktree's ``.git`` file).
    A missing or unreadable file is an empty one; a corrupt file is logged and ignored.
    """
    global _PINS_FILE
    _PINS_FILE = path
    if path is None:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        logger.warning("workspace pins at %s not loaded: %s", path, type(exc).__name__)
        return
    if not isinstance(data, dict):
        logger.warning("workspace pins at %s not loaded: not an object", path)
        return
    for root, pin in data.items():
        try:
            fmt = str(pin.get("object_format", "sha1"))
            _PINS[root] = GitPin(
                git_dir=str(pin["git_dir"]),
                work_tree=str(pin["work_tree"]),
                object_format=fmt if fmt in EMPTY_TREES else "sha1",
            )
        except (KeyError, TypeError, AttributeError):
            logger.warning("workspace pin for %s ignored: malformed", root)


def _save_pins() -> None:
    """Write every pin to the configured file atomically (temp file + ``os.replace``)."""
    if _PINS_FILE is None:
        return
    tmp = _PINS_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(_PINS_FILE) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({root: asdict(pin) for root, pin in _PINS.items()}, f, indent=1)
            f.write("\n")
        os.replace(tmp, _PINS_FILE)
    except OSError as exc:
        logger.warning("workspace pins not saved to %s: %s", _PINS_FILE, type(exc).__name__)


def pinned(root: str) -> GitPin | None:
    """The :class:`GitPin` recorded for a workspace root, if any."""
    return _PINS.get(root)


def forget_pin(root: str) -> None:
    """Drop the pin of ``root`` (after a worktree is discarded, or in tests)."""
    if _PINS.pop(root, None) is not None:
        _save_pins()
    _VERIFIED.discard(root)


def reset_pins() -> None:
    """Forget every pin, the file and the format cache (tests only: a fresh bridge process)."""
    global _PINS_FILE
    _PINS.clear()
    _VERIFIED.clear()
    _FORMATS.clear()
    _PINS_FILE = None


async def _derive_pin(ws: Workspace, root: str) -> GitPin | None:
    gd = await ws.run(["git", *HARDENING, "rev-parse", "--absolute-git-dir"], cwd=root)
    tl = await ws.run(["git", *HARDENING, "rev-parse", "--show-toplevel"], cwd=root)
    if gd.exit_code != 0 or tl.exit_code != 0 or not gd.stdout.strip() or not tl.stdout.strip():
        return None
    of = await ws.run(["git", *HARDENING, "rev-parse", "--show-object-format"], cwd=root)
    fmt = of.stdout.strip() if of.exit_code == 0 else ""
    return GitPin(
        git_dir=gd.stdout.strip(),
        work_tree=tl.stdout.strip(),
        object_format=fmt if fmt in EMPTY_TREES else "sha1",
    )


async def pin_workspace(ws: Workspace, root: str | None = None) -> GitPin | None:
    """Record ``--git-dir`` and ``--work-tree`` for ``root`` (default ``ws.root``).

    Both are read with ``git rev-parse`` run *in the root itself*, not in a subdirectory, so
    a nested repository under ``cwd`` is never picked up. Later :func:`git` calls for a
    workspace with that root pass them explicitly. Returns ``None``, recording nothing, when
    the root is not inside a git work tree. A root already pinned, in memory or in the pins
    file, keeps its pin: the first time such a stored pin is used in this process the tree is
    checked against it once, and a disagreement (a ``.git`` planted since) is logged and the
    stored pin kept.
    """
    root = root or ws.root
    stored = _PINS.get(root)
    if stored is not None:
        if root not in _VERIFIED:
            _VERIFIED.add(root)
            now = await _derive_pin(ws, root)
            if now != stored:
                logger.warning(
                    "workspace %s now resolves to another repository than its pin; "
                    "keeping the pin (git dir %s)",
                    root,
                    stored.git_dir,
                )
        return stored
    pin = await _derive_pin(ws, root)
    if pin is None:
        return None
    _PINS[root] = pin
    _VERIFIED.add(root)
    _save_pins()
    return pin


_GITHUB_URL_RES = (
    # https://github.com/o/r(.git)(/)
    re.compile(r"^https?://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?$"),
    # git@github.com:o/r(.git), including deploy-key aliases git@github.com-<alias>:o/r
    re.compile(r"^[\w.-]+@github\.com(?:-[\w.-]+)?:(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?$"),
    # ssh://git@github.com/o/r(.git) and ssh://git@github.com-<alias>/o/r(.git)
    re.compile(
        r"^ssh://(?:[\w.-]+@)?github\.com(?:-[\w.-]+)?(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?$"
    ),
)


async def git(
    ws: Workspace,
    *args: str,
    cwd: str | None = None,
    timeout_s: float = 10.0,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run ``git [--git-dir … --work-tree …] <HARDENING> <args>`` in the workspace (§10.6).

    The workspace supplies the argument-list execution, the 10 s default timeout and the
    scrubbed ``GIT_*`` environment; the pin of ``ws.root``, if one was recorded with
    :func:`pin_workspace`, fixes the repository the command operates on. ``HARDENING`` plus
    ``--attr-source`` (the empty tree of the repository's object format) neutralise hooks,
    fsmonitor, the checkout's ``.gitattributes`` and submodule descent (module docstring).
    On a git older than ``MIN_GIT_VERSION`` nothing runs: the result says why. ``env`` sets
    variables for this call only (``GIT_INDEX_FILE`` for a turn snapshot), through ``env(1)``
    in the argument vector, after ``Workspace.run`` has dropped the inherited ``GIT_*`` ones.
    """
    gate = await _version_gate(ws)
    if gate is not None:
        return gate
    prefix = ["env", *(f"{k}={v}" for k, v in env.items())] if env else []
    argv = [*prefix, "git", f"--attr-source={EMPTY_TREES[await _object_format(ws, cwd)]}"]
    pin = _PINS.get(ws.root)
    if pin is not None:
        argv += ["--git-dir", pin.git_dir, "--work-tree", pin.work_tree]
    argv += [*HARDENING, *args]
    return await ws.run(argv, cwd=cwd, timeout_s=timeout_s)


def parse_github_url(url: str) -> str | None:
    """Normalize a GitHub remote URL to ``https://github.com/o/r`` (SPEC §10.4).

    Accepts ``https://github.com/o/r(.git)``, ``git@github.com:o/r(.git)``,
    ``ssh://git@github.com/o/r(.git)`` and the SSH host aliases ``github.com-<alias>``
    that ``zab setup-repo`` creates (SPEC §13.1). Any other host yields ``None``.
    """
    url = url.strip()
    for rx in _GITHUB_URL_RES:
        m = rx.match(url)
        if m:
            owner, repo = m.group("owner"), m.group("repo")
            if repo.endswith(".git"):
                repo = repo[:-4]
            if not owner or not repo:
                return None
            return f"https://github.com/{owner}/{repo}"
    return None


@dataclass(frozen=True, slots=True)
class GitContext:
    """What the renderer needs to know about the repository at a turn (SPEC §10.4)."""

    repo_root: str
    head_sha: str
    branch: str | None  # None when HEAD is detached
    upstream: str | None  # e.g. "origin/main"; None when the branch has no upstream
    pushed: bool  # HEAD is an ancestor of the upstream
    web_url: str | None  # https://github.com/o/r, GitHub only


async def is_git_repo(ws: Workspace) -> bool:
    """True when ``ws.cwd`` is inside a git work tree."""
    res = await git(ws, "rev-parse", "--is-inside-work-tree")
    return res.exit_code == 0 and res.stdout.strip() == "true"


def _degrade(what: str, ws: Workspace, exc: BaseException) -> None:
    """Log a failed git query by exception type only (no paths from agent text, no output)."""
    logger.warning("%s failed in %s: %s", what, ws.root, type(exc).__name__)


async def git_context(ws: Workspace) -> GitContext | None:
    """Compute the :class:`GitContext` for ``ws`` (SPEC §10.4).

    Returns ``None`` when ``ws.cwd`` is not inside a git work tree, for a repository
    without any commit yet (there is no HEAD to describe), and when git cannot be run at
    all (``TimeoutError`` or ``OSError``), which is logged by exception type (issue #36).
    """
    try:
        return await _git_context(ws)
    except (TimeoutError, OSError) as exc:
        _degrade("git_context", ws, exc)
        return None


async def _git_context(ws: Workspace) -> GitContext | None:
    if not await is_git_repo(ws):
        return None
    root = await git(ws, "rev-parse", "--show-toplevel")
    head = await git(ws, "rev-parse", "HEAD")
    if root.exit_code != 0 or head.exit_code != 0:
        return None
    branch_res = await git(ws, "symbolic-ref", "--short", "-q", "HEAD")
    branch = branch_res.stdout.strip() if branch_res.exit_code == 0 else None
    upstream_res = await git(ws, "rev-parse", "--abbrev-ref", "@{u}")
    upstream = upstream_res.stdout.strip() if upstream_res.exit_code == 0 else None
    pushed = False
    if upstream:
        anc = await git(ws, "merge-base", "--is-ancestor", "HEAD", "@{u}")
        pushed = anc.exit_code == 0
    remote = await git(ws, "remote", "get-url", "origin")
    web_url = parse_github_url(remote.stdout) if remote.exit_code == 0 else None
    return GitContext(
        repo_root=root.stdout.strip(),
        head_sha=head.stdout.strip(),
        branch=branch or None,
        upstream=upstream or None,
        pushed=pushed,
        web_url=web_url,
    )


async def _untracked(ws: Workspace) -> tuple[str, ...]:
    """Untracked, non-ignored paths relative to the repository root, sorted.

    Runs from ``ws.root``: ``git ls-files`` only searches below its working directory, so
    running it in a ``subdir`` cwd would hide new files elsewhere in the repository.
    """
    res = await git(
        ws, "ls-files", "--others", "--exclude-standard", "-z", "--full-name", cwd=ws.root
    )
    if res.exit_code != 0:
        return ()
    return tuple(sorted(p for p in res.stdout.split("\0") if p))


async def snapshot(ws: Workspace) -> Snapshot | None:
    """Capture the workspace state at session or turn start (SPEC §10.5).

    ``ref`` is the commit printed by ``git stash create``, which records the uncommitted
    tracked changes without touching the working tree, the stash list or the staged
    content; when it succeeds but prints nothing (a clean tree) ``ref`` is the HEAD sha.
    ``untracked`` lists the untracked files at that moment. Like ``git diff``, the command
    may rewrite the index *file* to refresh its cached stat data (measured on git 2.43:
    ``git ls-files --stage`` is identical before and after), which ``GIT_OPTIONAL_LOCKS=0``
    does not suppress; nothing staged changes.

    Returns ``None`` outside a git repository, in a repository with no commits yet (neither
    a stash nor HEAD exists), and when ``git stash create`` fails, for example on an index
    with unresolved merge conflicts. A failure is logged and never replaced by a HEAD
    baseline, which would attribute pre-existing changes to the next turn; the core then
    reports "no diff available" for that turn. ``TimeoutError`` and ``OSError`` from git
    itself are logged by type and also give ``None`` (issue #36).
    """
    try:
        return await _snapshot(ws)
    except (TimeoutError, OSError) as exc:
        _degrade("snapshot", ws, exc)
        return None


async def _snapshot(ws: Workspace) -> Snapshot | None:
    if not await is_git_repo(ws):
        return None
    head = await git(ws, "rev-parse", "HEAD")
    if head.exit_code != 0:
        return None
    stash = await git(ws, "stash", "create")
    if stash.exit_code != 0:
        logger.warning(
            "git stash create failed in %s (exit %d): %s",
            ws.root,
            stash.exit_code,
            (stash.stderr.strip() or stash.stdout.strip()).splitlines()[-1:],
        )
        return None
    ref = stash.stdout.strip() or head.stdout.strip()
    if not _SHA_RE.match(ref):
        logger.warning("git stash create printed no commit id in %s: %r", ws.root, ref)
        return None
    return Snapshot(ref=ref, untracked=await _untracked(ws))


def _parse_numstat(text: str) -> list[tuple[int, int, str]]:
    """``(insertions, deletions, path)`` per record of ``git diff --numstat -z``.

    Binary files (``-`` counts) contribute 0 lines. Records are NUL-terminated, so file
    names with tabs, newlines or quoting characters arrive intact (``--no-renames`` keeps
    every record single-path).
    """
    out: list[tuple[int, int, str]] = []
    for record in text.split("\0"):
        parts = record.split("\t", 2)
        if len(parts) < 3 or not parts[2]:
            continue
        ins = int(parts[0]) if parts[0].isdigit() else 0
        dels = int(parts[1]) if parts[1].isdigit() else 0
        out.append((ins, dels, parts[2]))
    return out


def _parse_binary(text: str) -> set[str]:
    """The paths `git diff --numstat -z` reports as binary (``-`` counts)."""
    out = set()
    for record in text.split("\0"):
        parts = record.split("\t", 2)
        if len(parts) == 3 and parts[2] and parts[0] == "-" and parts[1] == "-":
            out.add(parts[2])
    return out


def _parse_name_status(text: str) -> dict[str, str]:
    """``path -> "new" | "deleted" | "modified"`` from ``git diff --name-status -z
    --no-renames`` (records ``<letter>\0<path>\0``)."""
    fields = text.split("\0")
    out: dict[str, str] = {}
    for letter, path in zip(fields[0::2], fields[1::2], strict=False):
        if path:
            out[path] = {"A": "new", "D": "deleted"}.get(letter[:1], "modified")
    return out


#: New untracked files are counted by reading them (§11.8): each up to this size, and in all
#: up to the total; beyond, a file is listed without counts.
COUNT_FILE_CAP = 8 * 1024 * 1024
COUNT_TOTAL_CAP = 64 * 1024 * 1024


def _count_untracked(root: str, paths: list[str]) -> dict[str, tuple[int | None, bool]]:
    """``path -> (lines or None, binary)`` for new untracked files: only regular files with a
    single link (no symlink, FIFO or device is ever opened, and a hard link named
    ``notes.txt`` to a credential file is never read, as for snapshots; codex on #164), each
    under COUNT_FILE_CAP and in all under COUNT_TOTAL_CAP; the opened descriptor must still be
    that file (`fstat`). A NUL byte in the first 8000 bytes means binary, as for git."""
    out: dict[str, tuple[int | None, bool]] = {}
    budget = COUNT_TOTAL_CAP
    for rel in paths:
        full = os.path.join(root, rel)
        try:
            st = os.lstat(full)
        except (OSError, ValueError):
            out[rel] = (None, False)
            continue
        if (
            not statmod.S_ISREG(st.st_mode)
            or st.st_nlink > 1
            or st.st_size > min(COUNT_FILE_CAP, budget)
        ):
            out[rel] = (None, False)
            continue
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                now = os.fstat(f.fileno())
                if (
                    (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino)
                    or not statmod.S_ISREG(now.st_mode)
                    or now.st_nlink > 1
                ):
                    out[rel] = (None, False)  # replaced or linked since the lstat
                    continue
                data = f.read(st.st_size + 1)
        except (OSError, ValueError):
            out[rel] = (None, False)
            continue
        budget -= len(data)
        if b"\0" in data[:8000]:
            out[rel] = (None, True)
            continue
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        out[rel] = (lines, False)
    return out


async def diff(ws: Workspace, since: Snapshot) -> DiffResult | None:
    """The working tree against a snapshot, plus new untracked files (SPEC §10.5).

    Returns ``None`` outside a git repository, and when git cannot be run (``TimeoutError``
    or ``OSError``, logged by type; issue #36). An empty patch with no new untracked files
    gives a result whose ``empty`` property is true, so the core posts no diff message.
    The diff always covers the whole repository with repository-relative paths, whatever
    ``ws.cwd`` and the user's ``diff.relative`` setting are.

    Changed files that the upload denylist refuses (§11.4: ``.env``, ``*.pem``, ``.ssh/``,
    ...) are excluded from the patch and the counts with ``:(exclude,literal)`` pathspecs,
    and listed in ``stat`` as ``<path> | changed (contents hidden)``; they still count in
    ``files_changed``. An edit to a secret file never reaches Zulip, only its name does
    (issue #36).

    ``files`` lists every file the turn touched for the diff message (§11.8): the tracked
    changes with their status (``--name-status``: new, deleted or modified, committed or
    not) and line counts (``--numstat``; binary files without), then the new untracked files
    as ``new``, counted by reading them (:func:`_count_untracked`). Hidden files carry no
    counts, and a denylisted untracked file is never read.
    """
    try:
        return await _diff(ws, since)
    except (TimeoutError, OSError) as exc:
        _degrade("diff", ws, exc)
        return None


_GLOB_SPECIALS = set("*?[]\\")


def _exclusion_pathspec(path: str) -> str:
    """The pathspec that hides ``path`` from a diff, byte-safe (§11.4, issue #36).

    ``Workspace.run`` decodes git's output with ``errors="replace"``, so a name holding
    bytes that are not UTF-8 reaches us with U+FFFD in their place and a literal pathspec
    built from it would not match the real bytes. Such a name gets a ``:(exclude,glob)``
    pattern with ``*`` for each replacement character and every glob character of the rest
    escaped, which can only match more than the one file, never less; whatever it hides is
    reported by name, and :func:`_diff` fails closed if a denylisted name still shows.
    """
    if "�" not in path:
        return f":(exclude,literal){path}"
    pattern = "".join(
        "*" if ch == "�" else (f"\\{ch}" if ch in _GLOB_SPECIALS else ch) for ch in path
    )
    return f":(exclude,glob){pattern}"


async def _diff(ws: Workspace, since: Snapshot) -> DiffResult | None:
    if not await is_git_repo(ws):
        return None
    common = (
        "-c",
        "diff.noprefix=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-relative",
    )
    # all commands run from ws.root, so "." and the exclude pathspecs are repository-wide
    listing = await git(ws, *common, "--numstat", "-z", since.ref, "--", cwd=ws.root)
    if listing.exit_code != 0:
        return None
    changed = _parse_numstat(listing.stdout)
    binary = _parse_binary(listing.stdout)
    statuses_res = await git(ws, *common, "--name-status", "-z", since.ref, "--", cwd=ws.root)
    statuses = _parse_name_status(statuses_res.stdout) if statuses_res.exit_code == 0 else {}
    denied = [path for _, _, path in changed if upload_denied_reason(path)]
    spec = ["--", ".", *(_exclusion_pathspec(path) for path in denied)] if denied else ["--"]
    patch = await git(ws, *common, since.ref, *spec, cwd=ws.root)
    if patch.exit_code != 0:
        return None
    stat = await git(ws, *common, "--stat", since.ref, *spec, cwd=ws.root)
    if denied:
        # what git actually kept after the exclusions: the hidden set is the complement, so
        # an exclusion that matched more than one name still reports every name it hid
        shown = await git(ws, *common, "--numstat", "-z", since.ref, *spec, cwd=ws.root)
        if shown.exit_code != 0:
            return None
        visible = _parse_numstat(shown.stdout)
    else:
        visible = changed
    visible_names = {path for _, _, path in visible}
    hidden = [path for _, _, path in changed if path not in visible_names]
    if any(upload_denied_reason(path) for path in visible_names):
        # an exclusion did not take (git spells the name differently from our decoding):
        # fail closed and show nothing of the patch, only the names (§11.4, §11.8)
        logger.warning("diff in %s: a denylisted path escaped exclusion; hiding the patch", ws.root)
        visible, hidden = [], [path for _, _, path in changed]
        patch = CommandResult(exit_code=0, stdout="", stderr="")
        stat = patch
    files = len(visible) if patch.stdout.strip() else 0
    ins = sum(i for i, _, _ in visible) if files else 0
    dels = sum(d for _, d, _ in visible) if files else 0
    stat_text = stat.stdout if files else ""
    stat_text += "".join(f" {path} | changed (contents hidden)\n" for path in hidden)
    before = set(since.untracked)
    new_untracked = tuple(p for p in await _untracked(ws) if p not in before)
    hidden_set = set(hidden)
    files_list = [
        DiffFile(path=path, status=statuses.get(path, "modified"), hidden=True)
        if path in hidden_set
        else DiffFile(
            path=path,
            status=statuses.get(path, "modified"),
            insertions=None if path in binary else ins_,
            deletions=None if path in binary else dels_,
            binary=path in binary,
        )
        for ins_, dels_, path in changed
    ]
    readable = [p for p in new_untracked if not upload_denied_reason(p)]
    counts: dict[str, tuple[int | None, bool]] = {}
    if readable:  # the paths are relative to the top level (--full-name), not ws.root
        top = await git(ws, "rev-parse", "--show-toplevel", cwd=ws.root)
        if top.exit_code == 0 and top.stdout.strip():
            counts = await asyncio.to_thread(_count_untracked, top.stdout.strip(), readable)
    for path in new_untracked:
        if upload_denied_reason(path):
            files_list.append(DiffFile(path=path, status="new", hidden=True))
            continue
        lines, is_binary = counts.get(path, (None, False))
        files_list.append(
            DiffFile(
                path=path,
                status="new",
                insertions=lines,
                deletions=0 if lines is not None else None,
                binary=is_binary,
            )
        )
    return DiffResult(
        files_changed=files + len(hidden),
        insertions=ins,
        deletions=dels,
        stat=stat_text,
        patch=patch.stdout if files else "",
        new_untracked=new_untracked,
        changed=tuple(path for _, _, path in changed),
        files=tuple(files_list),
    )


#: A turn snapshot leaves out untracked files above this size, and any beyond the total budget
#: (§10.7): a stray dataset or build product must not make every turn write gigabytes.
#: The total is what one push may have to carry within `PUSH_TIMEOUT_S`: 50 MiB in 120 s needs
#: under 0.5 MiB/s, which a busy VM still manages (Drew's test, issue #36, Wed Oct 7: 200 MiB of
#: video and frames did not go in 20 s). Files beyond it are simply not linked.
SNAPSHOT_FILE_CAP = 20 * 1024 * 1024
SNAPSHOT_TOTAL_CAP = 50 * 1024 * 1024
#: `<session id>/<turn row>`: sessions are `str(uuid4())` (core/creation.py), turns integers
#: (PM's blocker on #133). Both parts are valid ref components and never start with `-`.
_SNAPSHOT_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{0,63}/[0-9]+")
#: The push of a turn's snapshot ref (§10.7): bounded, and never waiting for a passphrase or a
#: host-key prompt (the deploy key alias of `zab setup-repo` needs neither). The answer waits
#: for it, so it is bounded, but generously: media-heavy turns are common (PM on issue #36, Wed
#: 17:25: 20 s timed out on a VM busy with ffmpeg, and the links fell back to GitHub).
PUSH_TIMEOUT_S = 120.0
_PUSH_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
}
_SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "zab",
    "GIT_AUTHOR_EMAIL": "zab@localhost",
    "GIT_COMMITTER_NAME": "zab",
    "GIT_COMMITTER_EMAIL": "zab@localhost",
}


async def publish_snapshot(
    ws: Workspace,
    name: str,
    *,
    github_owner: str,
    file_cap: int = SNAPSHOT_FILE_CAP,
    total_cap: int = SNAPSHOT_TOTAL_CAP,
) -> SnapshotRef | None:
    """Commit the workspace (:func:`snapshot_commit`) and push the ref to GitHub (SPEC §10.7).

    Only when ``origin`` has exactly one push destination (its push URLs, else its URLs, as
    configured: ``git push origin`` would go to every one of them; codex on #133) and that is a
    GitHub repository of ``github_owner``, compared without case: the file server serves that
    owner's repositories alone, so a snapshot pushed anywhere else could never be linked.
    A ``url.*.insteadOf`` or ``pushInsteadOf`` rule that rewrites that destination means no
    snapshot either (the push would go where the rule says). Nothing is committed for any
    other origin. The push goes to the remote, ``git push origin
    refs/zab/turns/<name>:refs/zab/turns/<name>``, so that git resolves it exactly as ``git
    remote get-url --push --all origin`` did (a literal URL would undergo ``pushInsteadOf``
    even where an explicit ``pushurl`` does not; codex on #133); from the checkout, with the
    hardening of :func:`git`, no hooks, no prompts and a ``PUSH_TIMEOUT_S`` bound. The URL is
    the deploy key alias of ``zab setup-repo``. No branch is pushed. Returns the commit and
    the repository's name; None on any failure, a failed push included (logged): rendering
    then goes on without snapshot links. Never raises.
    """
    if not _SNAPSHOT_NAME.fullmatch(name):
        return None
    try:
        origin = await _github_origin(ws)
        if origin is None or origin[0].casefold() != github_owner.casefold():
            return None
        repo = origin[1]
        sha = await snapshot_commit(ws, name, file_cap=file_cap, total_cap=total_cap)
        if sha is None:
            return None
        ref = f"refs/zab/turns/{name}"
        top = await git(ws, "rev-parse", "--show-toplevel")
        started = time.monotonic()
        try:
            push = await git(
                ws,
                "push",
                "--quiet",
                "--no-verify",
                "origin",
                f"{ref}:{ref}",
                cwd=top.stdout.strip() or None,
                timeout_s=PUSH_TIMEOUT_S,
                env=_PUSH_ENV,
            )
        except TimeoutError:
            logger.warning(
                "snapshot push of %s timed out after %.0f s", ref, time.monotonic() - started
            )
            return None
        # how close the fleet runs to the bound (PM on issue #36, Wed 17:25)
        logger.info("snapshot push of %s: %.1f s", ref, time.monotonic() - started)
    except (TimeoutError, OSError) as exc:
        _degrade("publish_snapshot", ws, exc)
        return None
    if push.exit_code != 0:
        why = push.stderr.strip().splitlines()[-1][:200] if push.stderr.strip() else ""
        logger.warning("snapshot push of %s failed (exit %s): %s", ref, push.exit_code, why)
        return None
    return SnapshotRef(sha=sha, repo=repo)


async def _github_origin(ws: Workspace) -> tuple[str, str, str] | None:
    """(owner, repository, URL) of ``origin``'s one push destination when it is on GitHub
    (:func:`parse_github_url`) and no URL rewrite changes it; None otherwise."""
    for key in ("remote.origin.pushurl", "remote.origin.url"):
        res = await git(ws, "config", "--get-all", key)
        urls = (
            [u.strip() for u in res.stdout.splitlines() if u.strip()] if res.exit_code == 0 else []
        )
        if not urls:
            continue
        web = parse_github_url(urls[0]) if len(urls) == 1 else None
        if web is None:
            return None
        # `url.*.insteadOf` and `pushInsteadOf` apply to the push too, so a rule could send it
        # to another owner (codex on #133): only an unrewritten destination is used, and the
        # push to `origin` resolves to exactly this one
        eff = await git(ws, "remote", "get-url", "--push", "--all", "origin")
        lines = eff.stdout.splitlines() if eff.exit_code == 0 else []
        if [u.strip() for u in lines if u.strip()] != [urls[0]]:
            return None
        owner, repo = web.rsplit("/", 2)[-2:]
        return owner, repo, urls[0]
    return None


async def snapshot_commit(
    ws: Workspace,
    name: str,
    *,
    file_cap: int = SNAPSHOT_FILE_CAP,
    total_cap: int = SNAPSHOT_TOTAL_CAP,
) -> str | None:
    """Commit the workspace as it stands under ``refs/zab/turns/<name>`` (SPEC §10.7).

    Tracked changes plus untracked files that ``.gitignore`` does not exclude, minus every
    denylisted path even when tracked (§11.4), minus untracked files over ``file_cap`` or past
    ``total_cap``, and minus changed or untracked files with more than one hard link (a link
    named ``notes.txt`` to a credential file has the credential's contents; the denylist sees
    only names). Files staged in the real index count as tracked, ignored or large ones too.
    Built in a temporary index (``GIT_INDEX_FILE``, outside the repository) that
    starts from HEAD's tree; ``write-tree`` and ``commit-tree`` **without a parent**, so that a
    push carries no unpublished history (whose commits may hold denylisted files); the ref,
    created only if absent, keeps it alive. A name that already has a ref returns that commit
    unchanged, so a retried completion links the same snapshot. The branch, the real index,
    the stash and the working tree are untouched, and every call carries the hardening of
    :func:`git`. ``name`` is ``<session id>/<turn row>``. Returns the commit sha; None outside
    a git repository, for a malformed name, and on any failure (logged by type): rendering
    then goes on without snapshot links.
    """
    if not _SNAPSHOT_NAME.fullmatch(name):
        return None
    tmp = await asyncio.to_thread(tempfile.mkdtemp, prefix="zab-snapshot-")
    try:
        return await _snapshot_commit(ws, name, tmp, file_cap, total_cap)
    except (TimeoutError, OSError) as exc:
        _degrade("snapshot_commit", ws, exc)
        return None
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)


async def fork_base_commit(
    ws: Workspace,
    name: str,
    *,
    file_cap: int = SNAPSHOT_FILE_CAP,
    total_cap: int = SNAPSHOT_TOTAL_CAP,
) -> str | None:
    """A local commit to fork a topic from (issue #202): the workspace as it stands, **with HEAD
    as its parent**, under ``refs/zab/forks/<name>``.

    The same selection as :func:`snapshot_commit` (tracked changes, files staged in the real
    index, untracked files within the caps), with one difference that the parent makes
    necessary: a tracked file the snapshot would leave out (denylisted, or a second hard link)
    keeps HEAD's version instead of disappearing, because a deletion in this commit would
    delete the file in the parent's branch when the fork is merged back. New denylisted or
    oversized files stay out as in a snapshot. Never pushed by zab: a fork branch built on it
    carries the parent's history, which is the participant's own. Created only if absent; the
    branch, the real index and the working tree are untouched. ``name`` is ``<session id>/<n>``.
    Returns the sha; None outside a repository, without a HEAD, for a malformed name, or on
    any failure (logged by type).
    """
    if not _SNAPSHOT_NAME.fullmatch(name):
        return None
    tmp = await asyncio.to_thread(tempfile.mkdtemp, prefix="zab-fork-")
    try:
        return await _snapshot_commit(ws, name, tmp, file_cap, total_cap, fork=True)
    except (TimeoutError, OSError) as exc:
        _degrade("fork_base_commit", ws, exc)
        return None
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)


def _nul_list(text: str) -> list[str]:
    return [p for p in text.split("\0") if p]


def _snapshot_candidate(top: str, rel: str) -> tuple[bool, int]:
    """(include?, size) for an untracked or changed path: symlinks and single-link regular
    files are kept (git stores a symlink's target, not what it points to); missing paths are
    deletions, which are kept too."""
    try:
        st = os.lstat(os.path.join(top, rel))
    except FileNotFoundError:
        return True, 0
    except OSError:
        return False, 0
    if statmod.S_ISLNK(st.st_mode):
        return True, 0
    if not statmod.S_ISREG(st.st_mode) or st.st_nlink > 1:
        return False, 0
    return True, st.st_size


async def _snapshot_commit(
    ws: Workspace, name: str, tmp: str, file_cap: int, total_cap: int, *, fork: bool = False
) -> str | None:
    """A turn snapshot (§10.7), or with `fork` a fork base (:func:`fork_base_commit`)."""
    if not await is_git_repo(ws):
        return None
    res = await git(ws, "rev-parse", "--show-toplevel")
    if res.exit_code != 0 or not res.stdout.strip():
        return None
    top = res.stdout.strip()
    ref_name = f"refs/zab/{'forks' if fork else 'turns'}/{name}"
    # Once per name: a completion retried after some answer parts were posted (or after a
    # restart) gets the same commit, so links already in Zulip stay a ref tip that the server
    # serves (codex reviews on #133 and #134). A new commit would differ by its timestamp alone.
    if (existing := await _commit_at(ws, ref_name, top)) is not None:
        return existing
    head = await git(ws, "rev-parse", "--verify", "-q", "HEAD^{commit}", cwd=top)
    base = head.stdout.strip() if head.exit_code == 0 else ""
    if base and not _SHA_RE.match(base):
        return None
    if fork and not base:  # nothing to keep the ancestry of
        return None
    env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
    lit = "--literal-pathspecs"

    async def run(*args: str, timeout_s: float = 60.0) -> CommandResult | None:
        r = await git(ws, lit, *args, cwd=top, timeout_s=timeout_s, env=env)
        return r if r.exit_code == 0 else None

    async def with_paths(cmd: list[str], paths: list[str]) -> bool:
        if not paths:
            return True
        spec = os.path.join(tmp, "paths")
        data = "\0".join(paths) + "\0"
        await asyncio.to_thread(_write, spec, data)
        return await run(*cmd, f"--pathspec-from-file={spec}", "--pathspec-file-nul") is not None

    if await run("read-tree", base or "--empty") is None:
        return None
    # tracked files changed in the working tree (modified or deleted), against HEAD's tree
    changed = await run("diff-files", "--name-only", "-z")
    if changed is None:
        return None
    keep, refused = [], []
    for p in _nul_list(changed.stdout):
        if upload_denied_reason(p):
            continue  # removed with every other denylisted path below (a fork keeps HEAD's)
        ok = (await asyncio.to_thread(_snapshot_candidate, top, p))[0]
        (keep if ok else refused).append(p)
    if not await with_paths(["add", "-u"], keep):
        return None
    # a changed tracked file the snapshot refuses (a second hard link, no longer a regular file)
    # leaves the snapshot, rather than showing HEAD's stale version under its name (codex #133);
    # a fork base keeps HEAD's version, since leaving it out would delete it on merge-back
    if not fork and not await with_paths(["rm", "--cached", "-q"], refused):
        return None
    # files staged in the real index but not in HEAD are tracked too (codex on #133): their
    # working-tree contents go in even when .gitignore matches them (`git add -f`) or they are
    # over the caps for untracked files; the denylist and the hard-link rule still apply
    in_head = await run("ls-files", "-z")  # the temporary index: HEAD's tree plus `add -u`
    real = await git(ws, "--literal-pathspecs", "ls-files", "-z", cwd=top)  # the real index
    if in_head is None or real.exit_code != 0:
        return None
    known = set(_nul_list(in_head.stdout))
    staged = []
    for p in _nul_list(real.stdout):
        if p in known or upload_denied_reason(p):
            continue
        if await asyncio.to_thread(os.path.lexists, os.path.join(top, p)):
            if (await asyncio.to_thread(_snapshot_candidate, top, p))[0]:
                staged.append(p)
    if not await with_paths(["add", "-f"], staged):
        return None
    # untracked files .gitignore does not exclude, within the caps
    others = await run("ls-files", "--others", "--exclude-standard", "-z")
    if others is None:
        return None
    total, picked = 0, []
    for p in _nul_list(others.stdout):
        if upload_denied_reason(p):
            continue
        ok, size = await asyncio.to_thread(_snapshot_candidate, top, p)
        if not ok or size > file_cap or total + size > total_cap:
            continue
        total += size
        picked.append(p)
    if not await with_paths(["add"], picked):
        return None
    # every denylisted path goes, tracked ones included (§11.4); a fork base keeps HEAD's own
    listed = await run("ls-files", "-z")
    if listed is None:
        return None
    denied = [
        p for p in _nul_list(listed.stdout) if upload_denied_reason(p) and not (fork and p in known)
    ]
    if not await with_paths(["rm", "--cached", "-q"], denied):
        return None
    # Fail closed (codex on #133, after merge): names reach Python decoded with replacement
    # characters, so `\xff/.env` and a real `\ufffd/.env` look alike and a pathspec removes
    # only the latter. Whatever the index still holds is listed again, and one denylisted
    # name left (one HEAD didn't already have, for a fork) means no commit at all.
    left = await run("ls-files", "-z")
    if left is None:
        return None
    if any(upload_denied_reason(p) and not (fork and p in known) for p in _nul_list(left.stdout)):
        logger.warning("snapshot refused: a denylisted path could not be removed")
        return None
    tree = await run("write-tree")
    if tree is None or not _SHA_RE.match(tree.stdout.strip()):
        return None
    if fork:
        # HEAD as the parent: the fork's branch merges back like any other branch (#202)
        message = f"zab fork base {name}\n\nWorking tree on {base}"
        parent = ["-p", base]
    else:
        # No parent: pushing the snapshot must not upload the checkout's unpublished history,
        # whose commits may hold the very files the denylist leaves out of the tree (codex #133).
        message = f"zab turn snapshot {name}" + (f"\n\nWorking tree on {base}" if base else "")
        parent = []
    commit = await git(
        ws,
        "commit-tree",
        tree.stdout.strip(),
        *parent,
        "-m",
        message,
        cwd=top,
        env=_SNAPSHOT_IDENTITY,
    )
    sha = commit.stdout.strip()
    if commit.exit_code != 0 or not _SHA_RE.match(sha):
        return None
    # Create-only (an empty old value): a concurrent publication of the same name keeps its
    # commit, and this one adopts it.
    ref = await git(ws, "update-ref", ref_name, sha, "", cwd=top)
    if ref.exit_code == 0:
        return sha
    return await _commit_at(ws, ref_name, top)


async def _snapshot_ref(ws: Workspace, name: str, top: str) -> str | None:
    """The commit `refs/zab/turns/<name>` points to, or None."""
    return await _commit_at(ws, f"refs/zab/turns/{name}", top)


async def _commit_at(ws: Workspace, ref: str, top: str) -> str | None:
    """The commit `ref` points to, or None."""
    res = await git(ws, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", cwd=top)
    sha = res.stdout.strip()
    return sha if res.exit_code == 0 and _SHA_RE.match(sha) else None


def _write(path: str, data: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)


async def ensure_zab_excluded(ws: Workspace) -> None:
    """Make sure ``.zab/`` is listed in ``<git dir>/info/exclude`` (SPEC §10.2).

    The git directory is ``git rev-parse --git-common-dir``, so worktrees created with
    ``git worktree add`` share the exclusion with the main checkout. Nothing committed
    changes. Does nothing outside a git repository.
    """
    res = await git(ws, "rev-parse", "--git-common-dir")
    if res.exit_code != 0:
        return
    common_dir = res.stdout.strip()
    if not os.path.isabs(common_dir):
        # git prints the path relative to the directory it ran in, which is ws.cwd
        common_dir = os.path.join(ws.cwd, common_dir)
    exclude = os.path.join(common_dir, "info", "exclude")
    existing = b""
    if await ws.stat(exclude) is not None:
        existing = await ws.read_bytes(exclude, 1 << 20)
    lines = existing.decode("utf-8", errors="replace").splitlines()
    if any(line.strip() in {".zab/", ".zab", "/.zab/", "/.zab"} for line in lines):
        return
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    await ws.write_bytes(exclude, existing + b".zab/\n")
