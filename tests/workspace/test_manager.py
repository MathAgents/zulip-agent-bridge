"""LocalWorkspaceManager: prepare, attachments and the upload denylist (SPEC §10, §11.4).

The denylist tests use decoy files created under tmp_path only (CONTRIBUTING.md, rule 2; AT-22).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from zab.core.types import Snapshot, UploadRefused, WorkspaceManager
from zab.workspace import git as g
from zab.workspace import manager as m
from zab.workspace.local import LocalWorkspace

from .conftest import real, run_git


def test_is_a_workspace_manager():
    assert isinstance(m.LocalWorkspaceManager(), WorkspaceManager)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Fix Parser!", "fix-parser"),
        ("fix-parser", "fix-parser"),
        ("  Spaces   and__underscores  ", "spaces-and-underscores"),
        ("a" * 60, "a" * 40),
        ("x" * 39 + "-yz", "x" * 39),  # the cut must not leave a trailing hyphen
        ("数学 セッション", "session"),
        ("---", "session"),
        ("", "session"),
        ("v1.2.3 / tests", "v1-2-3-tests"),
    ],
)
def test_slugify(name: str, slug: str):
    assert m.slugify(name) == slug
    assert len(slug) <= 40


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("plot.png", "plot.png"),
        ("my data (final).csv", "my_data_final_.csv"),
        ("../../etc/passwd", "passwd"),
        ("dir/sub/file.txt", "file.txt"),
        ("", "file"),
        ("...", "file"),
        ("ééé", "file"),
        ("a" * 150 + ".pdf", "a" * 96 + ".pdf"),
        ("a" * 150, "a" * 100),
        (".hidden", ".hidden"),
    ],
)
def test_sanitize_filename(filename: str, expected: str):
    out = m.sanitize_filename(filename)
    assert out == expected
    assert len(out) <= 100


# ------------------------------------------------------------------------------- prepare


async def test_prepare_shared_with_and_without_subdir(repo: Path):
    mgr = m.LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    (repo / "pkg").mkdir()
    pw = await mgr.prepare(project, "shared", "Fix Parser!")
    assert (pw.repo_root, pw.cwd, pw.branch, pw.base) == (str(repo), str(repo), "main", None)
    pw = await mgr.prepare(project, "shared", "x", subdir="pkg")
    assert (pw.repo_root, pw.cwd) == (str(repo), str(repo / "pkg"))
    assert ".zab/" in (repo / ".git" / "info" / "exclude").read_text()


@pytest.mark.parametrize("subdir", ["..", "../x", "pkg/../..", "/etc", "~/x"])
async def test_prepare_rejects_subdir_escapes(repo: Path, subdir: str):
    mgr = m.LocalWorkspaceManager()
    with pytest.raises(ValueError):
        await mgr.prepare(LocalWorkspace(str(repo)), "shared", "x", subdir=subdir)
    with pytest.raises(ValueError):
        await mgr.prepare(LocalWorkspace(str(repo)), "shared", "x", subdir="missing")
    assert not list((repo / ".zab").glob("worktrees/*")) if (repo / ".zab").exists() else True


async def test_prepare_two_worktrees_are_independent(repo: Path):
    """AT-19 logic: two concurrent worktree sessions in one project."""
    mgr = m.LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    head = run_git(repo, "rev-parse", "--short", "HEAD")
    a = await mgr.prepare(project, "worktree", "Fix Parser!")
    b = await mgr.prepare(project, "worktree", "Fix Parser!")
    assert a.repo_root != b.repo_root and a.branch != b.branch
    for pw in (a, b):
        assert pw.branch is not None and pw.branch.startswith("zab/fix-parser-")
        assert len(pw.branch) == len("zab/fix-parser-") + 6
        assert pw.repo_root == str(repo / ".zab" / "worktrees" / pw.branch[len("zab/") :])
        assert pw.cwd == pw.repo_root
        assert pw.base == f"main@{head}"
        assert (Path(pw.repo_root) / "README.md").read_text() == "hello\n"
        assert run_git(Path(pw.repo_root), "symbolic-ref", "--short", "HEAD") == pw.branch
    # each worktree changes independently, and the project's status stays clean (§10.2)
    (Path(a.repo_root) / "README.md").write_text("a\n")
    (Path(b.repo_root) / "new.txt").write_text("b\n")
    assert run_git(repo, "status", "--porcelain") == ""
    assert run_git(Path(a.repo_root), "status", "--porcelain") == "M README.md"  # stripped
    assert run_git(Path(b.repo_root), "status", "--porcelain") == "?? new.txt"
    assert "README.md" not in run_git(Path(b.repo_root), "status", "--porcelain")
    listed = run_git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {real(a.repo_root)}" in listed and f"worktree {real(b.repo_root)}" in listed


async def test_a_worktree_turn_diff_holds_only_its_own_topic(repo: Path):
    """Issue #36 (Drew's rehearsal, Mon 18:15): in the shared checkout a Claude topic listed
    Codex's files as new. With a worktree per topic, a turn's diff, and its new files, hold only
    what that topic's worktree changed: not another worktree's, not the main checkout's."""
    mgr = m.LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    a = await mgr.prepare(project, "worktree", "claude topic")
    b = await mgr.prepare(project, "worktree", "codex topic")
    wa = LocalWorkspace(a.repo_root, a.cwd)
    start = await mgr.snapshot(wa)
    assert start is not None
    (Path(b.repo_root) / "genus2_benchmark.py").write_text("print(2)\n")  # the other topic
    (repo / "scratch.txt").write_text("main checkout\n")  # the shared checkout
    (Path(a.repo_root) / "claude.md").write_text("this topic\n")
    (Path(a.repo_root) / "README.md").write_text("changed here\n")
    diff = await mgr.diff(wa, start)
    assert diff is not None
    assert diff.new_untracked == ("claude.md",)
    assert diff.files_changed == 1 and "README.md" in diff.stat
    assert "genus2_benchmark" not in diff.patch + diff.stat
    assert "scratch.txt" not in diff.patch + diff.stat


async def test_prepare_pins_the_workspace_to_its_repository(repo: Path):
    """Issue #36: later git calls for a prepared root pass --git-dir/--work-tree."""
    from zab.workspace import git as g

    mgr = m.LocalWorkspaceManager()
    shared = await mgr.prepare(LocalWorkspace(str(repo)), "shared", "s")
    pin = g.pinned(shared.repo_root)
    assert pin is not None
    assert pin.git_dir == run_git(repo, "rev-parse", "--absolute-git-dir")
    assert pin.work_tree == run_git(repo, "rev-parse", "--show-toplevel")
    wt = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "w")
    # Codex #50 round 1: with the project pinned, the worktree's branch must still be its own
    assert wt.branch == run_git(Path(wt.repo_root), "symbolic-ref", "--short", "HEAD")
    assert wt.branch is not None and wt.branch.startswith("zab/w-")
    assert run_git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    pin = g.pinned(wt.repo_root)
    assert pin is not None
    assert pin.git_dir == run_git(Path(wt.repo_root), "rev-parse", "--absolute-git-dir")
    assert pin.git_dir.startswith(str(repo / ".git" / "worktrees"))
    assert pin.work_tree == run_git(Path(wt.repo_root), "rev-parse", "--show-toplevel")
    # a nested repository created by the agent inside the worktree does not redirect
    nested = Path(wt.repo_root) / "vendor"
    nested.mkdir()
    run_git(nested, "init", "-q")
    ws = LocalWorkspace(wt.repo_root, str(nested))
    snap = await mgr.snapshot(ws)
    assert snap is not None and snap.ref == run_git(Path(wt.repo_root), "rev-parse", "HEAD")
    (Path(wt.repo_root) / "README.md").write_text("edited in the worktree\n")
    d = await mgr.diff(ws, snap)
    assert d is not None and d.files_changed == 1 and "README.md" in d.stat


async def test_manager_persists_pins_in_the_state_dir(repo: Path, tmp_path: Path):
    """PM review on PR #50 (B1): the pin written by prepare() survives a new manager (a
    restarted bridge), through <state_dir>/workspace-pins.json."""
    g.reset_pins()
    state = tmp_path / "state"
    mgr = m.LocalWorkspaceManager(state_dir=str(state))
    assert mgr.pins_file == str(state / "workspace-pins.json")
    wt = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "s")
    data = json.loads((state / "workspace-pins.json").read_text())
    assert wt.repo_root in data and data[wt.repo_root]["work_tree"] == real(wt.repo_root)
    g.reset_pins()  # a fresh process
    again = m.LocalWorkspaceManager(state_dir=str(state))
    assert again.pins_file == mgr.pins_file
    assert g.pinned(wt.repo_root) == g.GitPin(**data[wt.repo_root])
    snap = await again.snapshot(LocalWorkspace(wt.repo_root))
    assert snap is not None
    g.reset_pins()


async def test_snapshot_and_diff_degrade_when_pinning_cannot_run(repo: Path, caplog):
    """PM review on PR #50: pin_workspace() ran outside the degrade guards, so after a restart
    snapshot/diff could raise instead of returning None."""
    g.reset_pins()

    class Broken(LocalWorkspace):
        async def run(self, argv, *, cwd=None, timeout_s=10.0):
            raise TimeoutError("git hung")

    mgr = m.LocalWorkspaceManager()
    ws = Broken(str(repo))
    with caplog.at_level("WARNING", logger="zab.workspace.git"):
        assert await mgr.snapshot(ws) is None
        assert await mgr.diff(ws, Snapshot(ref="0" * 40, untracked=())) is None
    assert "pin_workspace failed" in caplog.text
    g.reset_pins()


async def test_prepare_worktree_with_subdir_and_detached_project(repo: Path):
    mgr = m.LocalWorkspaceManager()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "keep").write_text("")
    run_git(repo, "add", "pkg/keep")
    run_git(repo, "commit", "-q", "-m", "pkg")
    run_git(repo, "checkout", "-q", "--detach")
    head = run_git(repo, "rev-parse", "--short", "HEAD")
    pw = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "Sub", subdir="pkg")
    assert pw.cwd == os.path.join(pw.repo_root, "pkg")
    assert pw.base == f"HEAD@{head}"
    assert pw.branch is not None and pw.branch.startswith("zab/sub-")


async def test_prepare_worktree_rolls_back_when_subdir_is_untracked(repo: Path):
    """§7.2: a worktree whose creation failed half-way is removed again."""
    (repo / "scratch").mkdir()  # exists in the project, but not in HEAD
    with pytest.raises(ValueError, match="scratch"):
        await m.LocalWorkspaceManager().prepare(
            LocalWorkspace(str(repo)), "worktree", "x", subdir="scratch"
        )
    assert not list((repo / ".zab" / "worktrees").iterdir())
    assert "zab/" not in run_git(repo, "branch", "--list", "zab/*")
    assert run_git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_prepare_rejects_symlinked_subdir_leading_outside(repo: Path, tmp_path: Path):
    """Codex PR #18 round 1: a subdir symlink must not start the agent outside the root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / "pkg")
    run_git(repo, "add", "pkg")  # a tracked symlink, so it is checked out into worktrees
    run_git(repo, "commit", "-q", "-m", "symlink")
    mgr = m.LocalWorkspaceManager()
    for mode in ("shared", "worktree"):
        with pytest.raises(ValueError, match="outside"):
            await mgr.prepare(LocalWorkspace(str(repo)), mode, "x", subdir="pkg")
    assert not (repo / ".zab" / "worktrees").exists()  # rejected before any worktree
    assert run_git(repo, "branch", "--list", "zab/*") == ""


async def test_prepare_worktree_rolls_back_symlink_that_leaves_the_worktree(repo: Path):
    """An absolute symlink inside the project points outside a fresh worktree."""
    (repo / "src").mkdir()
    (repo / "src" / "keep").write_text("")
    os.symlink(repo / "src", repo / "pkg")  # fine in the project, escapes the worktree
    run_git(repo, "add", "src/keep", "pkg")
    run_git(repo, "commit", "-q", "-m", "abs symlink")
    mgr = m.LocalWorkspaceManager()
    pw = await mgr.prepare(LocalWorkspace(str(repo)), "shared", "x", subdir="pkg")
    assert pw.cwd == str(repo / "pkg")
    with pytest.raises(ValueError, match="outside the workspace"):
        await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "x", subdir="pkg")
    assert not list((repo / ".zab" / "worktrees").iterdir())  # rolled back
    assert run_git(repo, "branch", "--list", "zab/*") == ""
    assert run_git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_prepare_accepts_symlinked_subdir_inside_root(repo: Path):
    (repo / "src" / "pkg").mkdir(parents=True)
    os.symlink("src/pkg", repo / "pkg")
    pw = await m.LocalWorkspaceManager().prepare(LocalWorkspace(str(repo)), "shared", "x", "pkg")
    assert pw.cwd == str(repo / "pkg")


async def test_prepare_worktree_fails_cleanly_without_commits(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q")
    with pytest.raises(m.WorkspaceError):
        await m.LocalWorkspaceManager().prepare(LocalWorkspace(str(empty)), "worktree", "x")


# ------------------------------------------------------------------------ save_attachment


async def test_save_attachment(repo: Path):
    mgr = m.LocalWorkspaceManager()
    sub = repo / "sub"
    sub.mkdir()
    ws = LocalWorkspace(str(repo), str(sub))
    att = await mgr.save_attachment(ws, 4242, "my plot (v2).png", b"\x89PNG", "image/png")
    assert att.path == str(repo / ".zab" / "inbox" / "4242-my_plot_v2_.png")
    assert att.mime_type == "image/png" and att.size == 4
    assert await ws.read_bytes(att.path, 100) == b"\x89PNG"
    att = await mgr.save_attachment(ws, 1, "../../../etc/passwd", b"x", "text/plain")
    assert att.path == str(repo / ".zab" / "inbox" / "1-passwd")
    assert await ws.stat(att.path) is not None
    assert await ws.stat(str(repo / "etc" / "passwd")) is None


async def test_save_attachment_never_overwrites_on_name_collision(repo: Path):
    """Codex PR #18 round 2: several uploads on one message may sanitize alike."""
    mgr = m.LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo))
    inbox = repo / ".zab" / "inbox"
    a = await mgr.save_attachment(ws, 42, "plot (1).png", b"first", "image/png")
    b = await mgr.save_attachment(ws, 42, "plot [1].png", b"second", "image/png")
    c = await mgr.save_attachment(ws, 42, "plot_1_.png", b"third", "image/png")
    assert [os.path.basename(x.path) for x in (a, b, c)] == [
        "42-plot_1_.png",
        "42-plot_1_-2.png",
        "42-plot_1_-3.png",
    ]
    assert await ws.read_bytes(a.path, 10) == b"first"
    assert await ws.read_bytes(b.path, 10) == b"second"
    assert await ws.read_bytes(c.path, 10) == b"third"
    # another message with the same name gets its own prefix, so no suffix
    d = await mgr.save_attachment(ws, 43, "plot (1).png", b"fourth", "image/png")
    assert os.path.basename(d.path) == "43-plot_1_.png"
    # the 100-character cap holds for suffixed names, and the extension survives
    long = "a" * 150 + ".pdf"
    e = await mgr.save_attachment(ws, 44, long, b"1", "application/pdf")
    f = await mgr.save_attachment(ws, 44, long, b"2", "application/pdf")
    e_name, f_name = os.path.basename(e.path)[3:], os.path.basename(f.path)[3:]
    assert e_name == "a" * 96 + ".pdf"
    assert f_name == "a" * 94 + "-2.pdf" and len(f_name) == 100
    assert sorted(p.name for p in inbox.iterdir()) == sorted(
        os.path.basename(x.path) for x in (a, b, c, d, e, f)
    )


async def test_save_attachment_refuses_symlink_destinations(tmp_path: Path):
    """Codex PR #18 round 1 (P1): writes must not follow symlinks out of the inbox."""
    mgr = m.LocalWorkspaceManager()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("precious\n")
    # (a) a symlinked file inside a real inbox, pointing outside the root
    root = tmp_path / "ws"
    (root / ".zab" / "inbox").mkdir(parents=True)
    os.symlink(victim, root / ".zab" / "inbox" / "42-report.txt")
    ws = LocalWorkspace(str(root))
    with pytest.raises(m.WorkspaceError, match="symlink"):
        await mgr.save_attachment(ws, 42, "report.txt", b"pwned\n", "text/plain")
    assert victim.read_text() == "precious\n"
    # (b) a symlinked file pointing inside the root is refused too
    (root / "README.md").write_text("keep\n")
    os.symlink(root / "README.md", root / ".zab" / "inbox" / "43-readme.md")
    with pytest.raises(m.WorkspaceError, match="symlink"):
        await mgr.save_attachment(ws, 43, "readme.md", b"pwned\n", "text/plain")
    assert (root / "README.md").read_text() == "keep\n"
    # (c) the inbox directory itself is a symlink
    root2 = tmp_path / "ws2"
    (root2 / ".zab").mkdir(parents=True)
    os.symlink(outside, root2 / ".zab" / "inbox")
    with pytest.raises(m.WorkspaceError, match="inbox"):
        await mgr.save_attachment(LocalWorkspace(str(root2)), 7, "x.txt", b"esc\n", "text/plain")
    assert not (outside / "7-x.txt").exists()
    # (d) .zab itself is a symlink
    root3 = tmp_path / "ws3"
    root3.mkdir()
    os.symlink(outside, root3 / ".zab")
    with pytest.raises(m.WorkspaceError, match="inbox"):
        await mgr.save_attachment(LocalWorkspace(str(root3)), 8, "y.txt", b"esc\n", "text/plain")
    assert not (outside / "inbox").exists()
    # (e) a plain workspace without an inbox yet still works: the directory is created
    root4 = tmp_path / "ws4"
    root4.mkdir()
    att = await mgr.save_attachment(LocalWorkspace(str(root4)), 9, "z.txt", b"ok\n", "text/plain")
    assert att.path == str(root4 / ".zab" / "inbox" / "9-z.txt")
    assert (root4 / ".zab" / "inbox" / "9-z.txt").read_bytes() == b"ok\n"


# --------------------------------------------------------------------------- check_upload


@pytest.fixture
def decoys(tmp_path: Path) -> Path:
    """A workspace with decoy secrets only (AT-22); nothing here is a real credential."""
    root = tmp_path / "ws"
    root.mkdir()
    files = {
        ".env": "FAKE=1\n",
        ".env.local": "FAKE=1\n",
        "secrets.pem": "-----BEGIN FAKE-----\n",
        "id_ed25519": "fake\n",
        "server.key": "fake\n",
        ".netrc": "fake\n",
        "bot.zuliprc": "[api]\nkey=fake\n",
        ".codex/auth.json": "{}\n",
        ".claude/.credentials.json": "{}\n",
        ".config/zab/bot.zuliprc": "fake\n",
        ".ssh/config": "fake\n",
        "nested/.ssh/id_rsa": "fake\n",
        "auth.json": "{}\n",
        ".git/config": "[core]\n",
        ".zab/worktrees/x/plot.png": "png",
        ".zab/inbox/12-in.png": "png",
        "plot.png": "png",
        "PLOT2.PNG": "png",
        "data.csv": "a,b\n",
        "sub/inner.png": "png",
        "big.png": "x" * 2000,
        "notes.md": "# hi\n",
        "id_generator/module.py": "x\n",
        "src/keys.py": "x\n",
        # issue #36 additions: case variants and new names
        "SECRETS.PEM": "fake\n",
        ".ENV": "FAKE=1\n",
        "Auth.JSON": "{}\n",
        "zuliprc": "[api]\nkey=fake\n",
        ".git-credentials": "https://fake:fake@example.com\n",
        ".pypirc": "fake\n",
        ".npmrc": "fake\n",
        ".aws/credentials": "fake\n",
        "certs/client.p12": "fake",
        "certs/client.PFX": "fake",
        "tools/.Codex/auth.json": "{}\n",
        "environment.txt": "fine\n",
        "pfx-notes.md": "fine\n",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (root / "dir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n")
    os.symlink(outside / "secret.txt", root / "escape.png")
    os.symlink(root / ".codex", root / "linked_codex")
    return root


REFUSED = [
    (".env", "never uploaded"),
    (".env.local", "never uploaded"),
    ("secrets.pem", "never uploaded"),
    ("id_ed25519", "never uploaded"),
    ("server.key", "never uploaded"),
    (".netrc", "never uploaded"),
    ("bot.zuliprc", "never uploaded"),
    (".codex/auth.json", "never uploaded"),
    (".claude/.credentials.json", "never uploaded"),
    (".config/zab/bot.zuliprc", "never uploaded"),
    (".ssh/config", "never uploaded"),
    ("nested/.ssh/id_rsa", "never uploaded"),
    ("auth.json", "never uploaded"),
    (".git/config", "never uploaded"),
    (".zab/worktrees/x/plot.png", "never uploaded"),
    ("linked_codex/auth.json", "never uploaded"),  # symlink into a denied dir
    ("escape.png", "outside the workspace"),  # symlink out of the root
    ("../outside/secret.txt", "outside the workspace"),
    ("dir", "is a directory"),
    ("missing.png", "does not exist"),
    ("big.png", "exceeds"),
    (".", "not a file"),
    ("SECRETS.PEM", "never uploaded"),
    (".ENV", "never uploaded"),
    ("Auth.JSON", "never uploaded"),
    ("zuliprc", "never uploaded"),
    (".git-credentials", "never uploaded"),
    (".pypirc", "never uploaded"),
    (".npmrc", "never uploaded"),
    (".aws/credentials", "never uploaded"),
    ("certs/client.p12", "never uploaded"),
    ("certs/client.PFX", "never uploaded"),
    ("tools/.Codex/auth.json", "never uploaded"),
]


@pytest.mark.parametrize(("path", "reason"), REFUSED)
async def test_check_upload_refuses(decoys: Path, path: str, reason: str):
    mgr = m.LocalWorkspaceManager(max_upload_bytes=1000)
    ws = LocalWorkspace(str(decoys))
    for explicit in (False, True):  # !file bypasses the extension rule only
        with pytest.raises(UploadRefused) as exc:
            await mgr.check_upload(ws, path, explicit=explicit)
        assert reason in str(exc.value)
        assert "FAKE" not in str(exc.value) and "fake" not in str(exc.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
async def test_check_upload_refuses_non_regular_files(tmp_path: Path):
    """Codex PR #18 round 3: a FIFO or socket named like an image must not be approved."""
    root = tmp_path / "ws"
    root.mkdir()
    os.mkfifo(root / "pipe.png")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(root / "sock.png"))
    try:
        mgr = m.LocalWorkspaceManager()
        ws = LocalWorkspace(str(root))
        for name in ("pipe.png", "sock.png"):
            for explicit in (False, True):
                with pytest.raises(UploadRefused, match="not a regular file"):
                    await mgr.check_upload(ws, name, explicit=explicit)
    finally:
        sock.close()


async def test_check_upload_absolute_path_outside_root(decoys: Path, tmp_path: Path):
    mgr = m.LocalWorkspaceManager()
    with pytest.raises(UploadRefused, match="outside the workspace"):
        await mgr.check_upload(
            LocalWorkspace(str(decoys)), str(tmp_path / "outside" / "secret.txt"), explicit=True
        )


async def test_check_upload_accepts(decoys: Path):
    mgr = m.LocalWorkspaceManager(max_upload_bytes=1000)
    ws = LocalWorkspace(str(decoys))
    up = await mgr.check_upload(ws, "plot.png", explicit=False)
    assert up == m.Upload(path=real(decoys / "plot.png"), name="plot.png")
    up = await mgr.check_upload(ws, "PLOT2.PNG", explicit=False)  # extension case-insensitive
    assert up.name == "PLOT2.PNG"
    up = await mgr.check_upload(ws, str(decoys / "sub" / "inner.png"), explicit=False)
    assert up.path == real(decoys / "sub" / "inner.png")
    with pytest.raises(UploadRefused, match="not uploaded automatically"):
        await mgr.check_upload(ws, "data.csv", explicit=False)
    up = await mgr.check_upload(ws, "data.csv", explicit=True)
    assert up.name == "data.csv"
    up = await mgr.check_upload(ws, ".zab/inbox/12-in.png", explicit=False)  # inbox is fine
    assert up.name == "12-in.png"
    fine = ("id_generator/module.py", "src/keys.py", "notes.md", "environment.txt", "pfx-notes.md")
    for ok in fine:
        assert (await mgr.check_upload(ws, ok, explicit=True)).name == os.path.basename(ok)


async def test_check_upload_refuses_hard_links(decoys: Path):
    """Issue #36: a hard link named plot.png to a decoy credential has an innocent name."""
    mgr = m.LocalWorkspaceManager()
    ws = LocalWorkspace(str(decoys))
    os.link(decoys / ".codex" / "auth.json", decoys / "linked.png")
    for explicit in (False, True):
        with pytest.raises(UploadRefused, match="hard link"):
            await mgr.check_upload(ws, "linked.png", explicit=explicit)
    os.unlink(decoys / "linked.png")  # back to one link: the original rule applies again
    with pytest.raises(UploadRefused, match="never uploaded"):
        await mgr.check_upload(ws, ".codex/auth.json", explicit=True)
    assert (await mgr.check_upload(ws, "plot.png", explicit=False)).name == "plot.png"


async def test_check_upload_resolves_cwd_then_root(decoys: Path):
    mgr = m.LocalWorkspaceManager()
    ws = LocalWorkspace(str(decoys), str(decoys / "sub"))
    assert (await mgr.check_upload(ws, "inner.png", explicit=False)).path == real(
        decoys / "sub" / "inner.png"
    )
    assert (await mgr.check_upload(ws, "plot.png", explicit=False)).path == real(
        decoys / "plot.png"
    )
    with pytest.raises(UploadRefused, match="outside the workspace"):
        await mgr.check_upload(ws, "../../outside/secret.txt", explicit=True)


async def test_discard_removes_a_prepared_worktree_and_its_branch(repo: Path):
    """contract: WorkspaceManager.discard (§7.2 clean-up after a failed session creation)."""
    mgr = m.LocalWorkspaceManager()
    project = LocalWorkspace(str(repo))
    keep = await mgr.prepare(project, "worktree", "keep me")
    gone = await mgr.prepare(project, "worktree", "drop me")
    await mgr.discard(gone)
    assert await project.stat(gone.repo_root) is None
    kept = await project.stat(keep.repo_root)
    assert kept is not None and kept.is_dir
    branches = run_git(repo, "branch", "--list", "zab/*")
    assert gone.branch not in branches and keep.branch in branches
    assert run_git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    await mgr.discard(gone)  # idempotent: already gone, nothing to prune, no error


async def test_discard_never_removes_a_shared_project_at_a_worktree_path(repo: Path):
    """Codex #43 round 1: a project configured at an existing zab worktree stays intact."""
    mgr = m.LocalWorkspaceManager()
    wt = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "example")
    (Path(wt.repo_root) / "uncommitted.txt").write_text("precious\n")
    shared = await mgr.prepare(LocalWorkspace(wt.repo_root), "shared", "s")
    assert shared.repo_root == wt.repo_root and shared.base is None
    await mgr.discard(shared)
    assert (Path(wt.repo_root) / "uncommitted.txt").read_text() == "precious\n"
    assert run_git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    assert wt.branch in run_git(repo, "branch", "--list", "zab/*")


async def test_discard_prunes_a_worktree_whose_directory_vanished(repo: Path):
    mgr = m.LocalWorkspaceManager()
    pw = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "vanish")
    shutil.rmtree(pw.repo_root)
    assert "prunable" in run_git(repo, "worktree", "list", "--porcelain")
    await mgr.discard(pw)
    assert run_git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert run_git(repo, "branch", "--list", "zab/*") == ""


async def test_discard_leaves_shared_and_foreign_paths_alone(repo: Path, tmp_path: Path):
    mgr = m.LocalWorkspaceManager()
    shared = await mgr.prepare(LocalWorkspace(str(repo)), "shared", "x")
    await mgr.discard(shared)
    assert (repo / "README.md").exists() and (repo / ".git").is_dir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "file").write_text("x")
    await mgr.discard(
        m.PreparedWorkspace(repo_root=str(other), cwd=str(other), branch="main", base=None)
    )
    assert (other / "file").exists()
    # a zab-looking path whose branch is not the one prepare() reported keeps its branch
    pw = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "keepbranch")
    await mgr.discard(
        m.PreparedWorkspace(repo_root=pw.repo_root, cwd=pw.cwd, branch="main", base=pw.base)
    )
    assert await LocalWorkspace(str(repo)).stat(pw.repo_root) is None
    assert pw.branch in run_git(repo, "branch", "--list", "zab/*")
    assert run_git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_upload_denied_reason_table():
    assert m.upload_denied_reason("a/b/plot.png") is None
    assert m.upload_denied_reason(".zab/inbox/1-x.png") is None
    assert m.upload_denied_reason("x/.git/HEAD") is not None
    assert m.upload_denied_reason(".zab/worktrees/a/f") is not None
    assert m.upload_denied_reason("home/.config/zab/c.zuliprc") is not None
    assert m.upload_denied_reason(".config/other/x") is None
    assert m.upload_denied_reason("id_rsa.pub") is not None
    assert m.upload_denied_reason("x/y/.env.production") is not None
    assert m.upload_denied_reason("environment.txt") is None
    assert m.upload_denied_reason("ID_RSA") is not None  # case-insensitive names
    assert m.upload_denied_reason("a/.SSH/known_hosts") is not None  # and directories
    assert m.upload_denied_reason(".aws/config") is not None
    assert m.upload_denied_reason("zuliprc") is not None
    assert m.upload_denied_reason("my.zuliprc") is not None
    assert m.upload_denied_reason("x/.git-credentials") is not None
    assert m.upload_denied_reason("client.p12") is not None
    assert m.upload_denied_reason("zuliprc.md") is None
    from zab.workspace import denylist

    assert m.upload_denied_reason is denylist.upload_denied_reason  # one table, re-exported


# ------------------------------------------- bridge.worktree_post_create (issue #205)


def hook(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "hook.py"
    script.write_text(body)
    return [sys.executable, str(script)]


async def test_the_post_create_hook_runs_in_each_new_worktree(repo: Path, tmp_path: Path):
    """Issue #205: `lean-project --repair-all` links a committed Lean project in every new
    topic worktree; here a hook records where it ran."""
    argv = hook(tmp_path, "import os\nopen('hooked', 'w').write(os.getcwd())\n")
    mgr = m.LocalWorkspaceManager(post_create=argv)
    project = LocalWorkspace(str(repo))
    pw = await mgr.prepare(project, "worktree", "lean topic")
    assert (Path(pw.repo_root) / "hooked").read_text() == real(pw.repo_root)
    shared = await mgr.prepare(project, "shared", "no worktree")
    assert not (Path(shared.repo_root) / "hooked").exists()  # only new worktrees


@pytest.mark.parametrize(
    ("body", "logged"),
    [
        (
            "import sys\nprint('lean-project: refused')\nsys.exit(3)\n",
            "exited 3: lean-project: refused",
        ),
        ("import time\ntime.sleep(30)\n", "timed out after 1 s"),
    ],
)
async def test_a_failing_post_create_hook_is_logged_never_fatal(
    repo: Path, tmp_path: Path, caplog, monkeypatch, body: str, logged: str
):
    monkeypatch.setattr(m, "POST_CREATE_TIMEOUT_S", 1)
    mgr = m.LocalWorkspaceManager(post_create=hook(tmp_path, body))
    pw = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "topic")
    assert await asyncio.to_thread(os.path.isdir, pw.repo_root) and pw.branch
    assert logged in caplog.text


async def test_a_missing_post_create_hook_is_logged(repo: Path, tmp_path: Path, caplog):
    mgr = m.LocalWorkspaceManager(post_create=[str(tmp_path / "no-such-tool"), "--repair-all"])
    pw = await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "topic")
    assert await asyncio.to_thread(os.path.isdir, pw.repo_root)
    assert "worktree post-create hook did not start (FileNotFoundError)" in caplog.text


async def test_a_timed_out_hook_is_killed_with_its_children(
    repo: Path, tmp_path: Path, caplog, monkeypatch
):
    """Codex round 1 on #260: a child that inherits the output pipe must not hold the session
    start past the limit."""
    monkeypatch.setattr(m, "POST_CREATE_TIMEOUT_S", 1)
    body = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    mgr = m.LocalWorkspaceManager(post_create=hook(tmp_path, body))
    start = asyncio.get_running_loop().time()
    await mgr.prepare(LocalWorkspace(str(repo)), "worktree", "topic")
    assert asyncio.get_running_loop().time() - start < 10
    assert "timed out after 1 s" in caplog.text
