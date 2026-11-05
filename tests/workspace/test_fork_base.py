"""The workspace side of `!fork` (issue #202): a fork base with the parent's ancestry, and
`prepare(base_commit=...)` branching a new worktree from it."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager

from .conftest import run_git


def tree(repo: Path, sha: str) -> set[str]:
    return set(run_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())


def name() -> str:
    return f"{uuid.uuid4()}/1"


def state(repo: Path) -> tuple[str, str, str]:
    return (
        run_git(repo, "rev-parse", "HEAD"),
        run_git(repo, "ls-files", "--stage"),
        run_git(repo, "status", "--porcelain", "--untracked-files=all"),
    )


async def test_the_fork_base_is_the_working_tree_on_top_of_head(repo: Path):
    (repo / ".env").write_text("TOKEN=decoy\n")  # tracked by mistake
    (repo / "gone.txt").write_text("bye\n")
    run_git(repo, "add", ".env", "gone.txt")
    run_git(repo, "commit", "-q", "-m", "more")
    head = run_git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("edited, not committed\n")
    (repo / "gone.txt").unlink()
    (repo / "new.lean").write_text("theorem t : True := trivial\n")
    (repo / ".env").write_text("TOKEN=changed\n")  # a denylisted change stays out
    (repo / "id_ed25519").write_text("decoy key\n")  # a new denylisted file stays out
    before = state(repo)
    mgr = LocalWorkspaceManager()
    sha = await mgr.fork_base(LocalWorkspace(str(repo)), n := name())
    assert sha and run_git(repo, "rev-parse", f"{sha}^") == head  # the parent's ancestry
    assert tree(repo, sha) == {"README.md", "new.lean", ".env"}
    assert run_git(repo, "show", f"{sha}:README.md") == "edited, not committed"
    # HEAD's version of the tracked secret: a fork merged back must not delete or change it
    assert run_git(repo, "show", f"{sha}:.env") == "TOKEN=decoy"
    assert run_git(repo, "rev-parse", f"refs/zab/forks/{n}") == sha
    assert state(repo) == before  # branch, index and working tree untouched
    assert await mgr.fork_base(LocalWorkspace(str(repo)), n) == sha  # once per name


async def test_a_second_hard_link_keeps_heads_version(repo: Path):
    (repo / "notes.txt").write_text("v1\n")
    run_git(repo, "add", "notes.txt")
    run_git(repo, "commit", "-q", "-m", "notes")
    (repo / "notes.txt").write_text("v2\n")
    os.link(repo / "notes.txt", repo / "other")  # notes.txt now has two links
    sha = await LocalWorkspaceManager().fork_base(LocalWorkspace(str(repo)), name())
    assert sha and run_git(repo, "show", f"{sha}:notes.txt") == "v1"
    assert "other" not in tree(repo, sha)


async def test_no_fork_base_without_a_head_or_with_a_bad_name(tmp_path: Path, repo: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q")
    (empty / "a.txt").write_text("a\n")
    mgr = LocalWorkspaceManager()
    assert await mgr.fork_base(LocalWorkspace(str(empty)), name()) is None
    assert await mgr.fork_base(LocalWorkspace(str(repo)), "../x") is None
    assert await mgr.fork_base(LocalWorkspace(str(tmp_path)), name()) is None  # not a repo


async def test_prepare_branches_a_fork_worktree_from_the_base(repo: Path):
    (repo / "wip.py").write_text("print('half done')\n")
    mgr = LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    sha = await mgr.fork_base(project, name())
    prepared = await mgr.prepare(project, "worktree", "side task", base_commit=sha)
    assert prepared.base == f"fork@{sha[:7]}"
    assert prepared.branch and prepared.branch.startswith("zab/side-task-")
    assert (Path(prepared.repo_root) / "wip.py").read_text() == "print('half done')\n"
    assert run_git(Path(prepared.repo_root), "rev-parse", "HEAD") == sha
    # merging the fork back is an ordinary merge: the parent's HEAD is its ancestor
    run_git(repo, "merge-base", "--is-ancestor", "HEAD", prepared.branch)
    assert run_git(repo, "status", "--porcelain") == "?? wip.py"  # parent untouched
    await mgr.discard(prepared)
    assert not await asyncio.to_thread(os.path.exists, prepared.repo_root)


async def test_prepare_refuses_a_bad_base_commit(repo: Path):
    mgr = LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    head = run_git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="worktree mode"):
        await mgr.prepare(project, "shared", "x", base_commit=head)
    with pytest.raises(ValueError, match="full commit id"):
        await mgr.prepare(project, "worktree", "x", base_commit=head[:7])
    with pytest.raises(ValueError, match="not a commit"):
        await mgr.prepare(project, "worktree", "x", base_commit="0" * 40)
    blob = run_git(repo, "rev-parse", "HEAD:README.md")
    with pytest.raises(ValueError, match="not a commit"):
        await mgr.prepare(project, "worktree", "x", base_commit=blob)
    assert not (repo / ".zab" / "worktrees").exists() or not any(
        (repo / ".zab" / "worktrees").iterdir()
    )


async def test_a_fork_of_a_worktree_may_start_in_a_directory_only_the_parent_has(repo: Path):
    """Codex on #226: the parent topic created `pkg/` in its own worktree; the main checkout
    doesn't have it, the fork base does."""
    mgr = LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    parent = await mgr.prepare(project, "worktree", "parent")
    pkg = Path(parent.repo_root) / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n")
    sha = await mgr.fork_base(LocalWorkspace(parent.repo_root), name())
    child = await mgr.prepare(project, "worktree", "child", subdir="pkg", base_commit=sha)
    assert child.cwd == str(Path(child.repo_root) / "pkg")
    # a directory the base lacks is still refused, and the half-made worktree removed
    with pytest.raises(ValueError):
        await mgr.prepare(project, "worktree", "other", subdir="nope", base_commit=sha)
    left = run_git(repo, "worktree", "list", "--porcelain")
    assert "other-" not in left
