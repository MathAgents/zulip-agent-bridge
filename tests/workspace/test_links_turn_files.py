"""Issue #36 (Drew, Sun 16:30): on a real pushed repository, a bare `README.md:1` in a turn's
answer names the README the turn created, not the tracked one at the root (SPEC §11.3)."""

from __future__ import annotations

from pathlib import Path

from zab.core.links import WorkspaceLinks
from zab.core.types import RenderContext
from zab.render.links import rewrite_links
from zab.render.markdown import protect_code, restore_code
from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager

from .conftest import run_git


async def test_a_bare_name_resolves_to_the_file_the_turn_created(repo_with_origin: Path):
    repo = repo_with_origin
    run_git(repo, "remote", "set-url", "origin", "git@github.com:octo/demo.git")
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo))
    snap = await manager.snapshot(ws)
    (repo / "fermat").mkdir()
    (repo / "fermat" / "README.md").write_text("# Fermat\n")
    (repo / "fermat" / "Solution.lean").write_text("theorem t : True := trivial\n")
    diff = await manager.diff(ws, snap)
    assert diff is not None and set(diff.new_untracked) == {
        "fermat/README.md",
        "fermat/Solution.lean",
    }

    async def render(turn_files):
        ctx = RenderContext(
            workspace=ws,
            max_message_length=10_000,
            max_upload_bytes=1 << 20,
            upload=None,
            turn_files=turn_files,
        )
        text = "Created in `fermat`: `Solution.lean:7`, `README.md:1`"
        protected, table = protect_code(text)
        out, table = await rewrite_links(protected, table, ctx, WorkspaceLinks(manager))
        return restore_code(out, table)

    before = await render(None)
    assert "github.com/octo/demo/blob/" in before and "/README.md#L1" in before  # the bug
    after = await render(frozenset(diff.changed + diff.new_untracked))
    assert "github.com" not in after
    assert "`fermat/README.md:1` *(local)*" in after
    assert "`fermat/Solution.lean:7` *(local)*" in after


async def test_the_diff_lists_changed_tracked_files(repo_with_origin: Path):
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo_with_origin))
    snap = await manager.snapshot(ws)
    (repo_with_origin / "README.md").write_text("changed\n")
    diff = await manager.diff(ws, snap)
    assert diff is not None and diff.changed == ("README.md",) and diff.new_untracked == ()
