"""LocalWorkspaceManager: prepare, snapshot, diff, attachments and upload checks (SPEC §10).

This is the implementation of the ``WorkspaceManager`` protocol in
``zab.core.types`` (DECISIONS D17). The core calls it for everything in SPEC §10 and for
the upload denylist of §11.4; ``render/links.py`` reuses :func:`upload_denied_reason`.

Interpretations recorded here:

- ``check_upload`` has no size argument, so the effective limit, the smaller of
  ``render.max_upload_mb`` and the server's ``max_file_upload_size_mib`` (§11.4), is a
  constructor parameter that the core sets once it has read the ``register`` response.
- ``PreparedWorkspace.base`` (Appendix B.1, ``main@4e1d2aa``) is filled in worktree mode
  only, since "from" describes the commit a worktree started at; shared mode returns
  ``None``.
- Relative upload paths resolve against ``cwd`` first, then ``root`` (the first that
  exists), mirroring the link resolution rule of §11.3.
- ``FileStat`` carries no file type beyond ``is_dir``, so ``check_upload`` confirms that
  the resolved path is a regular file with ``os.stat`` (FIFOs, sockets and devices are
  refused: reading them would block or dump a device). Phase 1 workspaces are local; a
  remote ``Workspace`` (§12.3) would need a mode field in the contract.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import posixpath
import re
import secrets
import stat as statmod
from typing import Literal

from zab.core.types import (
    Attachment,
    DiffResult,
    PreparedWorkspace,
    Snapshot,
    SnapshotRef,
    Upload,
    UploadRefused,
    Workspace,
)
from zab.workspace import git as g
from zab.workspace.denylist import UPLOAD_DENY_DIRS, UPLOAD_DENY_NAMES, upload_denied_reason
from zab.workspace.local import LocalWorkspace

__all__ = [
    "DEFAULT_AUTO_UPLOAD_EXT",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "UPLOAD_DENY_DIRS",
    "UPLOAD_DENY_NAMES",
    "LocalWorkspaceManager",
    "WorkspaceError",
    "sanitize_filename",
    "slugify",
    "upload_denied_reason",
]

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # render.max_upload_mb default (§5.2)
DEFAULT_AUTO_UPLOAD_EXT = frozenset({"png", "jpg", "jpeg", "gif", "svg", "pdf"})  # §5.2

_SLUG_MAX = 40
_NAME_MAX = 100


log = logging.getLogger("zab.workspace")
#: The longest a worktree post-create hook may take (issue #205).
POST_CREATE_TIMEOUT_S = 60


class WorkspaceError(Exception):
    """A workspace operation failed or was refused (§7.2: the core replies with ⚠️)."""


def _inside(path: str, root: str) -> bool:
    """True when the resolved ``path`` is ``root`` or below it (string comparison)."""
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def slugify(name: str) -> str:
    """Topic name -> worktree slug: lower case, ``[a-z0-9-]``, at most 40 chars (§10.1)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    slug = slug[:_SLUG_MAX].strip("-")
    return slug or "session"


def _with_suffix(stem: str, ext: str, suffix: str) -> str:
    """``stem + suffix + ext``, shortening ``stem`` so the name stays within 100 chars."""
    ext = ext[:16]
    return stem[: max(1, _NAME_MAX - len(ext) - len(suffix))] + suffix + ext


def sanitize_filename(filename: str) -> str:
    """Attachment name -> ``[A-Za-z0-9._-]``, at most 100 chars, extension kept (§10.3)."""
    base = posixpath.basename(filename.replace(os.sep, "/"))
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    if not name or set(name) <= {"."}:
        name = "file"
    if len(name) > _NAME_MAX:
        stem, ext = os.path.splitext(name)
        name = _with_suffix(stem, ext, "")
    return name


class LocalWorkspaceManager:
    """The ``WorkspaceManager`` protocol for local checkouts and worktrees (§10)."""

    def __init__(
        self,
        *,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        auto_upload_ext: frozenset[str] | set[str] = DEFAULT_AUTO_UPLOAD_EXT,
        state_dir: str | None = None,
        post_create: list[str] | tuple[str, ...] = (),
    ) -> None:
        """``state_dir`` (the bridge's, ``bridge.state_dir``) persists the workspace pins in
        ``<state_dir>/workspace-pins.json``, so that a restart cannot re-bind a workspace
        to a ``.git`` the agent planted (git.py hardening); ``None`` keeps them in memory.
        ``post_create`` (``bridge.worktree_post_create``, issue #205) runs in each new
        worktree; see ``_post_create``."""
        self.post_create = tuple(post_create)
        self.max_upload_bytes = max_upload_bytes
        self.auto_upload_ext = frozenset(e.lower().lstrip(".") for e in auto_upload_ext)
        self.pins_file = os.path.join(state_dir, "workspace-pins.json") if state_dir else None
        g.configure_pins(self.pins_file)

    # ------------------------------------------------------------------ prepare (§10.1, §10.2)

    async def prepare(
        self,
        project: Workspace,
        mode: Literal["shared", "worktree"],
        slug: str,
        subdir: str = "",
        *,
        base_commit: str | None = None,
    ) -> PreparedWorkspace:
        """Create the worktree if needed and ensure ``.zab/`` is excluded (§10.1, §10.2).

        ``shared``: the workspace is the project path plus ``subdir``. ``worktree``:
        ``git worktree add -b zab/<slug>-<6 hex> <project>/.zab/worktrees/<slug>-<6 hex>
        HEAD``, or the full commit id ``base_commit`` instead of HEAD (a fork, #202: the commit
        :meth:`fork_base` made); the card's base then reads ``fork@<short sha>``. Session
        worktrees are never deleted here (§10.1); only a worktree whose creation failed
        half-way (``subdir`` missing from HEAD) is removed again, as §7.2 requires. Raises
        ``ValueError`` for a ``subdir`` outside the repository or not a directory, and for a
        ``base_commit`` in shared mode, malformed, or not a commit of this repository; and
        :class:`WorkspaceError` when git fails.
        """
        if mode not in ("shared", "worktree"):
            raise ValueError(f"unknown workspace mode {mode!r}")
        if base_commit is not None:
            if mode != "worktree":
                raise ValueError("a base commit needs worktree mode")
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit):
                raise ValueError("a base commit must be a full commit id")
            found = await g.git(project, "cat-file", "-t", base_commit)
            if found.exit_code != 0 or found.stdout.strip() != "commit":
                raise ValueError(f"{base_commit} is not a commit of this repository")
        subdir = _check_subdir(subdir)
        # cheap early rejection before any worktree is created; not for a fork, whose base may
        # hold a directory the parent created in its own worktree and the checkout lacks (codex
        # on #226): the check after creation below still applies, with its clean-up
        if subdir and base_commit is None:
            await _check_cwd(project, project.root, subdir, "the project")
        await g.ensure_zab_excluded(project)
        base: str | None = None
        if mode == "worktree":
            name = f"{slugify(slug)}-{secrets.token_hex(3)}"
            branch = f"zab/{name}"
            repo_root = os.path.join(project.root, ".zab", "worktrees", name)
            start = base_commit or "HEAD"
            head = await g.git(project, "rev-parse", "--short", start)
            if head.exit_code != 0:
                raise WorkspaceError(f"cannot create a worktree: {head.stderr.strip()}")
            if base_commit:
                base = f"fork@{head.stdout.strip()}"
            else:
                src = await g.git(project, "symbolic-ref", "--short", "-q", "HEAD")
                base = f"{src.stdout.strip() or 'HEAD'}@{head.stdout.strip()}"
            res = await g.git(
                project, "worktree", "add", "-b", branch, "--", repo_root, start, timeout_s=60
            )
            if res.exit_code != 0:
                raise WorkspaceError(f"git worktree add failed: {res.stderr.strip()}")
        else:
            repo_root = project.root
        try:
            cwd = await _check_cwd(project, repo_root, subdir, "the workspace")
        except ValueError:
            if mode == "worktree":  # untracked in HEAD, or a tracked symlink leading out
                await g.git(project, "worktree", "remove", "--force", "--", repo_root)
                await g.git(project, "branch", "-D", "--", f"zab/{name}")
            raise
        # bind the new workspace to its repository now, so later git calls for it cannot be
        # redirected by a nested .git the agent creates (git.py hardening, issue #36)
        await g.pin_workspace(project, root=repo_root)
        if mode == "worktree":
            await self._post_create(repo_root)
        # query the branch through a workspace rooted at repo_root: git() selects the pin by
        # ws.root, and a pinned *project* would answer for its own HEAD, not the worktree's
        ref = await g.git(LocalWorkspace(repo_root), "symbolic-ref", "--short", "-q", "HEAD")
        current = ref.stdout.strip() if ref.exit_code == 0 else ""
        return PreparedWorkspace(repo_root=repo_root, cwd=cwd, branch=current or None, base=base)

    async def _post_create(self, repo_root: str) -> None:
        """``bridge.worktree_post_create`` in a new worktree (issue #205): an argument list,
        never a shell string (§10.6), run there through ``LocalWorkspace.run``: no stdin, its
        own process group, killed with all its descendants after ``POST_CREATE_TIMEOUT_S``
        (codex round 1 on #260). Whatever happens is logged, never raised: the session starts
        either way."""
        if not self.post_create:
            return
        try:
            res = await LocalWorkspace(repo_root).run(
                list(self.post_create), timeout_s=POST_CREATE_TIMEOUT_S
            )
        except TimeoutError:
            log.warning("worktree post-create hook timed out after %d s", POST_CREATE_TIMEOUT_S)
            return
        except OSError as exc:
            log.warning("worktree post-create hook did not start (%s)", type(exc).__name__)
            return
        lines = (res.stdout + "\n" + res.stderr).strip().splitlines()
        if res.exit_code:
            log.warning(
                "worktree post-create hook exited %d: %s",
                res.exit_code,
                lines[-1][:300] if lines else "",
            )
        elif lines:
            log.info("worktree post-create hook: %s", " / ".join(line[:200] for line in lines[:5]))

    async def discard(self, prepared: PreparedWorkspace) -> None:
        """Remove a worktree that :meth:`prepare` created, after session creation failed (§7.2).

        A shared preparation is a no-op whatever its path looks like: :meth:`prepare` fills
        ``base`` only for worktrees it created, so ``base is None`` identifies shared mode
        even when the project itself lives at ``<x>/.zab/worktrees/<name>`` (a user may
        configure an existing worktree as a project). For a worktree preparation, only a
        directory of the form ``<project>/.zab/worktrees/<name>`` is touched, together with
        its branch ``zab/<name>`` when that is the branch :meth:`prepare` reported; any
        other path is left alone, so session worktrees are never deleted by accident
        (§10.1). A worktree whose directory is already gone is pruned from git's list.
        Errors from git are raised as :class:`WorkspaceError`.
        """
        if prepared.base is None:
            return  # shared mode (see above): never remove the project checkout
        root = prepared.repo_root.rstrip("/")
        parent, name = os.path.split(root)
        zab_dir, worktrees = os.path.split(parent)
        project_root, zab = os.path.split(zab_dir)
        if not (name and worktrees == "worktrees" and zab == ".zab" and project_root):
            return
        project = LocalWorkspace(project_root)
        if await project.stat(root) is None:
            await g.git(project, "worktree", "prune")
        else:
            res = await g.git(project, "worktree", "remove", "--force", "--", root, timeout_s=60)
            if res.exit_code != 0:
                raise WorkspaceError(f"git worktree remove failed: {res.stderr.strip()}")
        if prepared.branch == f"zab/{name}":
            await g.git(project, "branch", "-D", "--", prepared.branch)

    # ---------------------------------------------------------------- snapshots and diffs (§10.5)

    async def fork_base(self, ws: Workspace, name: str) -> str | None:
        """See :func:`zab.workspace.git.fork_base_commit` (#202); pins the workspace first."""
        await _pin_quietly(ws)
        return await g.fork_base_commit(ws, name)

    async def snapshot(self, ws: Workspace) -> Snapshot | None:
        """See :func:`zab.workspace.git.snapshot` (§10.5); pins the workspace first."""
        await _pin_quietly(ws)  # a no-op once prepare() pinned it; covers restarts
        return await g.snapshot(ws)

    async def diff(self, ws: Workspace, since: Snapshot) -> DiffResult | None:
        """See :func:`zab.workspace.git.diff` (§10.5); pins the workspace first."""
        await _pin_quietly(ws)
        return await g.diff(ws, since)

    # ------------------------------------------------------------------------ inbox (§10.3)

    async def save_attachment(
        self, ws: Workspace, message_id: int, filename: str, data: bytes, mime_type: str
    ) -> Attachment:
        """Write an owner attachment to ``<root>/.zab/inbox/<message id>-<name>`` (§10.3).

        The inbox directory and the destination are resolved with ``ws.realpath`` first:
        both must be plain paths inside the workspace (no symlink in ``.zab``, ``inbox`` or
        the file itself), or :class:`WorkspaceError` is raised and nothing is written. As
        with the upload checks (§11.4), this is a safety net against a redirected inbox,
        not a race-free guarantee: the bridge and the agent are the same user (§12.2).

        A message can carry several uploads whose sanitized names coincide (``plot (1).png``
        and ``plot [1].png``); an existing file is never overwritten. Later ones get
        ``-2``, ``-3``, ... before the extension, within the 100-character cap.
        """
        name = sanitize_filename(filename)
        root = (await ws.realpath(ws.root)).rstrip("/")
        inbox = os.path.join(root, ".zab", "inbox")
        if await ws.realpath(inbox) != inbox:
            raise WorkspaceError("refusing to write attachments: .zab/inbox is a symlink")
        stem, ext = os.path.splitext(name)
        for n in itertools.count(1):
            unique = name if n == 1 else _with_suffix(stem, ext, f"-{n}")
            path = os.path.join(inbox, f"{int(message_id)}-{unique}")
            if await ws.realpath(path) != path:
                raise WorkspaceError(f"refusing to overwrite {unique}: it is a symlink")
            if await ws.stat(path) is None:
                break
        await ws.write_bytes(path, data)
        return Attachment(path=path, mime_type=mime_type, size=len(data))

    # ---------------------------------------------------------------- upload checks (§11.4)

    async def publish_snapshot(
        self, ws: Workspace, name: str, *, github_owner: str
    ) -> SnapshotRef | None:
        """See :func:`zab.workspace.git.publish_snapshot` (§10.7); pins the workspace first."""
        await _pin_quietly(ws)
        return await g.publish_snapshot(ws, name, github_owner=github_owner)

    async def check_upload(self, ws: Workspace, path: str, *, explicit: bool) -> Upload:
        """Root check, denylist, existence, extension and size limit (§11.4).

        Raises :class:`UploadRefused` with a one-line reason naming the rule. With
        ``explicit=False`` (automatic uploads, §11.3) the extension must be in
        ``auto_upload_ext``; ``explicit=True`` (``!file``) allows any extension but is
        subject to the same denylist and size limit. Only regular files with a single hard
        link are approved: a second link gives a credential file an innocent name.
        """
        candidate = await _locate(ws, path)
        real = await ws.realpath(candidate)
        root = (await ws.realpath(ws.root)).rstrip("/")
        if real != root and not real.startswith(root + "/"):
            raise UploadRefused(f"{path}: outside the workspace")
        rel = real[len(root) :].lstrip("/")
        if not rel:
            raise UploadRefused(f"{path}: is the workspace root, not a file")
        # the denylist applies after symlink resolution (§11.4); the literal path is
        # checked too, so a symlink named like a secret cannot masquerade as a plain file
        literal = candidate[len(ws.root) :] if candidate.startswith(ws.root + "/") else rel
        reason = upload_denied_reason(rel) or upload_denied_reason(literal)
        if reason:
            raise UploadRefused(f"{path}: {reason}")
        st = await ws.stat(real)
        if st is None:
            raise UploadRefused(f"{path}: does not exist")
        if st.is_dir:
            raise UploadRefused(f"{path}: is a directory")
        regular = await _regular_file_stat(real)
        if regular is None:
            raise UploadRefused(f"{path}: not a regular file")
        if regular.st_nlink > 1:
            # a hard link named plot.png to a credential file has the credential's contents
            # and an innocent name; the denylist cannot see through it (issue #36)
            raise UploadRefused(f"{path}: has more than one hard link")
        ext = os.path.splitext(real)[1].lstrip(".").lower()
        if not explicit and ext not in self.auto_upload_ext:
            raise UploadRefused(f"{path}: extension .{ext or ''} is not uploaded automatically")
        if st.size > self.max_upload_bytes:
            limit_mb = self.max_upload_bytes / (1024 * 1024)
            raise UploadRefused(f"{path}: {st.size} bytes exceeds the {limit_mb:g} MiB limit")
        return Upload(path=real, name=os.path.basename(real))


async def _pin_quietly(ws: Workspace) -> None:
    """Pin like :func:`git.pin_workspace`, but degrade (log by type) instead of raising when
    git cannot run, so ``snapshot``/``diff`` keep their never-raise contract after a restart
    (issue #36; PM review on PR #50)."""
    try:
        await g.pin_workspace(ws)
    except (TimeoutError, OSError) as exc:
        g.logger.warning("pin_workspace failed in %s: %s", ws.root, type(exc).__name__)


def _check_subdir(subdir: str) -> str:
    """Normalize a project-relative ``subdir``; reject absolute paths and ``..`` escapes."""
    subdir = (subdir or "").strip()
    if not subdir or subdir == ".":
        return ""
    if os.path.isabs(subdir) or subdir.startswith("~"):
        raise ValueError(f"subdir must be relative to the project: {subdir!r}")
    norm = posixpath.normpath(subdir.replace(os.sep, "/"))
    if norm == "." or norm == ".." or norm.startswith("../"):
        raise ValueError(f"subdir escapes the project: {subdir!r}")
    return norm


async def _regular_file_stat(path: str) -> os.stat_result | None:
    """``os.stat`` of the (already symlink-resolved, local) ``path`` if it is a regular file."""
    try:
        st = await asyncio.to_thread(os.stat, path)
    except OSError:
        return None
    return st if statmod.S_ISREG(st.st_mode) else None


async def subdir_cwd(ws: Workspace, repo_root: str, subdir: str) -> str:
    """The session cwd for `subdir` in an existing checkout at `repo_root`, checked as
    `prepare` checks it (§10.1): for a session that joins another bot's topic worktree with
    its own subdir (codex on #168). Raises ``ValueError``."""
    return await _check_cwd(ws, repo_root, _check_subdir(subdir), "the workspace")


async def _check_cwd(ws: Workspace, repo_root: str, subdir: str, where: str) -> str:
    """``repo_root/subdir`` as the session cwd, after checking it is a directory that
    stays inside ``repo_root`` once symlinks are resolved (§10.1). Raises ``ValueError``."""
    cwd = os.path.join(repo_root, subdir) if subdir else repo_root
    st = await ws.stat(cwd)
    if st is None or not st.is_dir:
        raise ValueError(f"subdir {subdir!r} is not a directory in {where}")
    if not _inside(await ws.realpath(cwd), await ws.realpath(repo_root)):
        raise ValueError(f"subdir {subdir!r} leads outside {where} (symlink)")
    return cwd


async def _locate(ws: Workspace, path: str) -> str:
    """Absolute candidate for ``path``: as is, else under ``cwd``, else under ``root``."""
    if os.path.isabs(path):
        return path
    in_cwd = os.path.join(ws.cwd, path)
    if ws.cwd == ws.root or await ws.stat(in_cwd) is not None:
        return in_cwd
    in_root = os.path.join(ws.root, path)
    if await ws.stat(in_root) is not None:
        return in_root
    return in_cwd
