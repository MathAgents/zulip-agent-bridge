"""rc6 file server on a real repository (SPEC §10.7): the lazy turn snapshot, pushed as
refs/zab/turns/<session>/<turn> through origin, its membership query through WorkspaceLinks, and
the links of the answer and the diff's new files to {refserve_url}/{repo}/{sha}/{path}."""

from __future__ import annotations

import uuid
from pathlib import Path

from zab.core.links import WorkspaceLinks
from zab.core.types import PublishedSnapshot, RenderContext
from zab.render.links import rewrite_links
from zab.render.markdown import MarkdownRenderer, protect_code, restore_code
from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager

from .conftest import make_repo, run_git

ORIGIN = "git@github.com-demo:MathAgents/demo.git"  # the `zab setup-repo` alias form
BASE = "https://files.mathagents.org"


def with_origin(repo: Path, fake_github) -> Path:
    """origin is the alias URL, served by `fake_github` (MathAgents/demo.git) over a fake ssh;
    no URL rewriting, so `remote get-url origin` shows GitHub (permalinks)."""
    bare = fake_github("MathAgents/demo.git")
    run_git(repo, "remote", "add", "origin", ORIGIN)
    return bare


def lazy(manager: LocalWorkspaceManager, ws: LocalWorkspace, name: str):
    """What the core hands the renderer: published on first use, then shared."""
    memo: list[PublishedSnapshot | None] = []

    async def provide() -> PublishedSnapshot | None:
        if not memo:
            ref = await manager.publish_snapshot(ws, name, github_owner="MathAgents")
            memo.append(
                PublishedSnapshot(sha=ref.sha, url=f"{BASE}/{ref.repo}/{ref.sha}") if ref else None
            )
        return memo[0]

    return provide, memo


def context(ws, provide) -> RenderContext:
    return RenderContext(
        workspace=ws,
        max_message_length=10_000,
        max_upload_bytes=1 << 20,
        upload=None,
        snapshot=provide,
        final_answer=True,
    )


async def test_links_and_new_files_point_into_the_pushed_snapshot(tmp_path: Path, fake_github):
    repo = make_repo(tmp_path / "demo")
    bare = with_origin(repo, fake_github)
    run_git(repo, "push", "-q", str(bare), "main")  # README.md is pushed and unchanged
    run_git(repo, "update-ref", "refs/remotes/origin/main", "main")
    run_git(repo, "branch", "-q", "--set-upstream-to=origin/main")
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo))
    start = await manager.snapshot(ws)
    (repo / "report").mkdir()
    (repo / "report" / "index.html").write_text("<h1>result</h1>\n")
    (repo / ".env").write_text("TOKEN=decoy\n")
    name = f"{uuid.uuid4()}/7"
    provide, memo = lazy(manager, ws, name)
    ctx = context(ws, provide)
    text = "See [the report](report/index.html), `README.md:1` and `.env`."
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, ctx, WorkspaceLinks(manager))
    out = restore_code(out, table)
    snap = memo[0]
    assert snap is not None and snap.url == f"{BASE}/demo/{snap.sha}"
    assert f"[the report]({snap.url}/report/index.html)" in out
    # pushed and unchanged, but the snapshot comes first (PM on #36, Mon 09:30)
    assert f"[README.md:1]({snap.url}/README.md)" in out and "github.com" not in out
    assert "`.env` *(local)*" in out  # never in a snapshot
    diff = await manager.diff(ws, start)
    message = await MarkdownRenderer(WorkspaceLinks(manager)).render_diff(diff, ctx)
    assert f"- [`report/index.html`]({snap.url}/report/index.html)" in message
    assert "- `.env`" in message  # listed, not linked
    # pushed once, under the turn's name; no branch beyond main
    assert run_git(bare, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/main",
        f"refs/zab/turns/{name}",
    ]


async def test_an_answer_without_files_pushes_nothing(tmp_path: Path, fake_github):
    repo = make_repo(tmp_path / "demo")
    bare = with_origin(repo, fake_github)
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo))
    provide, memo = lazy(manager, ws, f"{uuid.uuid4()}/1")
    protected, table = protect_code("4")
    links = WorkspaceLinks(manager)
    out, table = await rewrite_links(protected, table, context(ws, provide), links)
    assert restore_code(out, table) == "4" and memo == []
    assert run_git(repo, "for-each-ref", "refs/zab") == ""
    assert run_git(bare, "for-each-ref", "refs/zab") == ""


async def test_a_worktree_pushes_through_its_main_repositorys_origin(tmp_path: Path, fake_github):
    repo = make_repo(tmp_path / "demo")
    bare = with_origin(repo, fake_github)
    run_git(repo, "worktree", "add", "-q", "-b", "topic", str(tmp_path / "wt"))
    (tmp_path / "wt" / "draft.md").write_text("a draft\n")
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(tmp_path / "wt"))
    ref = await manager.publish_snapshot(ws, "s-1/2", github_owner="MathAgents")
    assert ref and ref.repo == "demo"
    assert run_git(bare, "rev-parse", "refs/zab/turns/s-1/2") == ref.sha
    assert "draft.md" in run_git(repo, "ls-tree", "--name-only", ref.sha)


async def test_a_repository_without_commits_links_its_first_snapshot(tmp_path: Path, fake_github):
    """Codex review on #134: no HEAD yet (repo_info is None), but the snapshot links."""
    repo = tmp_path / "demo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    with_origin(repo, fake_github)
    (repo / "report").mkdir()
    (repo / "report" / "index.html").write_text("<h1>first</h1>\n")
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo))
    provide, memo = lazy(manager, ws, "s/1")
    protected, table = protect_code("See `report/index.html`.")
    links = WorkspaceLinks(manager)
    out, table = await rewrite_links(protected, table, context(ws, provide), links)
    snap = memo[0]
    assert snap is not None
    assert f"[report/index.html]({snap.url}/report/index.html)" in restore_code(out, table)
