"""Adapt the workspace helpers to the renderer's link protocol (SPEC §11.3)."""

import posixpath
from collections.abc import Sequence
from pathlib import PurePosixPath

from zab.core.types import Upload, Workspace, WorkspaceManager
from zab.render.links import LinkTarget, RepoInfo
from zab.workspace.git import git, git_context


class WorkspaceLinks:
    def __init__(self, manager: WorkspaceManager) -> None:
        self.manager = manager

    async def repo_info(self, ws: Workspace) -> RepoInfo | None:
        context = await git_context(ws)
        if context is None:
            return None
        return RepoInfo(
            head_sha=context.head_sha,
            pushed=context.pushed,
            web_url=context.web_url,
            root=context.repo_root,
        )

    async def classify(self, ws: Workspace, paths: Sequence[str]) -> dict[str, LinkTarget]:
        if not paths:
            return {}
        tracked = await git(ws, "ls-files", "--cached", "-z", "--", *paths, cwd=ws.root)
        changed = await git(ws, "diff", "--name-only", "-z", "HEAD", "--", *paths, cwd=ws.root)
        files = (
            {str(PurePosixPath(ws.root) / p) for p in tracked.stdout.split("\0") if p}
            if tracked.exit_code == 0
            else set()
        )
        dirty = {str(PurePosixPath(ws.root) / p) for p in changed.stdout.split("\0") if p}
        result = {}
        for path in paths:
            stat = await ws.stat(path)
            prefix = path.rstrip("/") + "/"
            result[path] = LinkTarget(
                exists=stat is not None,
                is_dir=bool(stat and stat.is_dir),
                tracked=path in files or any(p.startswith(prefix) for p in files),
                modified=changed.exit_code != 0
                or path in dirty
                or any(p.startswith(prefix) for p in dirty),
            )
        return result

    async def snapshot_paths(self, ws: Workspace, sha: str, paths: Sequence[str]) -> dict[str, str]:
        """The canonical `paths` that exist, as files or directories, in commit `sha` (§10.7),
        each with its path in the snapshot. The root comes from `--show-toplevel`, which needs
        no HEAD: a repository's first snapshot links too (codex review on #134)."""
        if not paths:
            return {}
        top = await git(ws, "rev-parse", "--show-toplevel")
        root = await ws.realpath(top.stdout.strip()) if top.exit_code == 0 else ""
        if not root:
            return {}
        rels = {
            p: posixpath.relpath(p, root) for p in paths if p.startswith(root.rstrip("/") + "/")
        }
        if not rels:
            return {}
        listing = await git(
            ws,
            "--literal-pathspecs",
            "ls-tree",
            "-r",
            "-t",
            "-z",
            "--name-only",
            sha,
            "--",
            *rels.values(),
            cwd=root,
        )
        found = set(listing.stdout.split("\0")) if listing.exit_code == 0 else set()
        return {p: rel for p, rel in rels.items() if rel in found}

    async def check_upload(self, ws: Workspace, path: str, *, explicit: bool) -> Upload:
        return await self.manager.check_upload(ws, path, explicit=explicit)
