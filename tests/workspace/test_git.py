"""GitContext, snapshots, diffs and the .zab/ exclusion (SPEC §10.2, §10.4-§10.6)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from zab.workspace import git as g
from zab.workspace.local import LocalWorkspace

from .conftest import make_repo, real, run_git


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/o/r", "https://github.com/o/r"),
        ("https://github.com/o/r.git", "https://github.com/o/r"),
        ("https://github.com/o/r/", "https://github.com/o/r"),
        ("https://github.com/o/r.git/", "https://github.com/o/r"),
        ("http://github.com/o/r", "https://github.com/o/r"),
        ("https://user@github.com/o/r.git", "https://github.com/o/r"),
        ("git@github.com:o/r", "https://github.com/o/r"),
        ("git@github.com:o/r.git", "https://github.com/o/r"),
        ("git@github.com-myrepo:o/r.git", "https://github.com/o/r"),  # §13.1 alias
        ("ssh://git@github.com/o/r.git", "https://github.com/o/r"),
        ("ssh://git@github.com:22/o/r.git", "https://github.com/o/r"),
        ("ssh://git@github.com-myrepo/o/r", "https://github.com/o/r"),
        ("  https://github.com/o/r.git\n", "https://github.com/o/r"),
        ("https://gitlab.com/o/r.git", None),
        ("git@gitlab.com:o/r.git", None),
        ("git@github.example.com:o/r.git", None),
        ("https://github.com/o", None),
        ("https://github.com/o/r/extra", None),
        ("/home/me/src/r", None),
        ("../r.git", None),
        ("", None),
    ],
)
def test_parse_github_url(url: str, expected: str | None):
    assert g.parse_github_url(url) == expected


async def test_context_fresh_repo(repo: Path):
    ws = LocalWorkspace(str(repo))
    ctx = await g.git_context(ws)
    assert ctx is not None
    assert ctx.repo_root == real(repo)
    assert ctx.head_sha == run_git(repo, "rev-parse", "HEAD")
    assert ctx.branch == "main"
    assert ctx.upstream is None
    assert ctx.pushed is False
    assert ctx.web_url is None


async def test_context_pushed_and_web_url(repo_with_origin: Path):
    repo = repo_with_origin
    run_git(repo, "remote", "set-url", "origin", "git@github.com:octo/demo.git")
    ws = LocalWorkspace(str(repo))
    ctx = await g.git_context(ws)
    assert ctx is not None
    assert ctx.upstream == "origin/main"
    assert ctx.pushed is True
    assert ctx.web_url == "https://github.com/octo/demo"
    (repo / "README.md").write_text("changed\n")
    run_git(repo, "commit", "-q", "-am", "local only")
    ctx = await g.git_context(ws)
    assert ctx is not None and ctx.pushed is False
    assert ctx.head_sha == run_git(repo, "rev-parse", "HEAD")


async def test_context_detached_head_and_subdir(repo: Path):
    sub = repo / "pkg"
    sub.mkdir()
    run_git(repo, "checkout", "-q", "--detach")
    ws = LocalWorkspace(str(repo), str(sub))
    ctx = await g.git_context(ws)
    assert ctx is not None
    assert ctx.branch is None
    assert ctx.repo_root == real(repo)


async def test_context_outside_repo_and_empty_repo(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await g.is_git_repo(LocalWorkspace(str(plain))) is False
    assert await g.git_context(LocalWorkspace(str(plain))) is None
    assert await g.snapshot(LocalWorkspace(str(plain))) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q")
    assert await g.git_context(LocalWorkspace(str(empty))) is None
    assert await g.snapshot(LocalWorkspace(str(empty))) is None


async def test_snapshot_clean_tree_is_head(repo: Path):
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    assert snap.ref == run_git(repo, "rev-parse", "HEAD")
    assert snap.untracked == ()
    d = await g.diff(ws, snap)
    assert d is not None and d.empty


async def test_snapshot_dirty_tree_is_stash_commit(repo: Path):
    ws = LocalWorkspace(str(repo))
    (repo / "README.md").write_text("hello\nworld\n")
    head = run_git(repo, "rev-parse", "HEAD")
    snap = await g.snapshot(ws)
    assert snap is not None
    assert snap.ref != head
    assert run_git(repo, "cat-file", "-t", snap.ref) == "commit"
    # stash create touches nothing: tree still dirty, stash list empty, HEAD unchanged
    assert (repo / "README.md").read_text() == "hello\nworld\n"
    assert run_git(repo, "stash", "list") == ""
    assert run_git(repo, "rev-parse", "HEAD") == head
    d = await g.diff(ws, snap)
    assert d is not None and d.empty
    (repo / "README.md").write_text("hello\nworld\n!\n")
    d = await g.diff(ws, snap)
    assert d is not None and d.files_changed == 1 and d.insertions == 1 and d.deletions == 0


async def test_stash_create_refreshes_stat_data_but_not_staged_content(repo: Path):
    """Issue #36 "small things": what `git stash create` does to the index, measured."""
    import hashlib

    (repo / "b.txt").write_text("b\n")
    run_git(repo, "add", "b.txt")
    run_git(repo, "commit", "-q", "-m", "two files")
    index = repo / ".git" / "index"
    past = index.stat().st_mtime - 60
    os.utime(repo / "README.md", (past, past))  # stale stat data, same content
    (repo / "b.txt").write_text("b2\n")  # a real change, so a stash commit is created
    staged_before = run_git(repo, "ls-files", "--stage")
    bytes_before = hashlib.sha256(index.read_bytes()).hexdigest()
    snap = await g.snapshot(LocalWorkspace(str(repo)))
    assert snap is not None and snap.ref != run_git(repo, "rev-parse", "HEAD")
    assert run_git(repo, "ls-files", "--stage") == staged_before  # staged content unchanged
    assert run_git(repo, "stash", "list") == ""
    assert (repo / "b.txt").read_text() == "b2\n"
    rewritten = hashlib.sha256(index.read_bytes()).hexdigest() != bytes_before
    # the file itself may be rewritten with refreshed stat data; a second call then is not
    snap2 = await g.snapshot(LocalWorkspace(str(repo)))
    assert snap2 is not None
    bytes_after = hashlib.sha256(index.read_bytes()).hexdigest()
    assert run_git(repo, "ls-files", "--stage") == staged_before
    assert rewritten or bytes_after == bytes_before  # documented, not required, behaviour


async def test_snapshot_uses_the_identity_from_the_environment(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Codex #50 round 3: GIT_AUTHOR_*/GIT_COMMITTER_* survive the scrub and reach the
    stash commit; with no configured identity at all, snapshot() still works (git 2.43)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # no ~/.gitconfig identity
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    run_git(repo, "config", "user.useConfigOnly", "true")
    (repo / "README.md").write_text("dirty\n")  # stash create must write a commit
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    snap = await g.snapshot(LocalWorkspace(str(repo)))
    assert snap is not None and snap.ref != run_git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Op")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "op@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Op")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "op@example.com")
    snap = await g.snapshot(LocalWorkspace(str(repo)))
    assert snap is not None
    commit = run_git(repo, "cat-file", "-p", snap.ref)
    assert "author Op <op@example.com>" in commit
    assert "committer Op <op@example.com>" in commit


async def test_diff_counts_patch_and_stat(repo: Path):
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "README.md").write_text("goodbye\nand more\n")
    d = await g.diff(ws, snap)
    assert d is not None
    assert (d.files_changed, d.insertions, d.deletions) == (1, 2, 1)
    assert "-hello" in d.patch and "+goodbye" in d.patch and "+and more" in d.patch
    assert d.patch.startswith("diff --git a/README.md b/README.md")
    assert "README.md" in d.stat and "1 file changed" in d.stat
    assert d.new_untracked == ()
    assert not d.empty


async def test_diff_new_untracked_files(repo: Path):
    ws = LocalWorkspace(str(repo))
    (repo / "old.txt").write_text("existed before the snapshot\n")
    snap = await g.snapshot(ws)
    assert snap is not None and snap.untracked == ("old.txt",)
    (repo / "new.txt").write_text("appeared after\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "deep.txt").write_text("also new\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("never listed\n")
    d = await g.diff(ws, snap)
    assert d is not None
    assert d.new_untracked == (".gitignore", "dir/deep.txt", "new.txt")
    assert d.files_changed == 0 and d.patch == ""
    assert not d.empty  # new files alone make the diff worth posting


async def test_untracked_seen_from_subdir_cwd(repo: Path):
    """Codex round 1: ls-files only searches below its cwd; run it from the root."""
    sub = repo / "sub"
    sub.mkdir()
    ws = LocalWorkspace(str(repo), str(sub))
    (repo / "before.txt").write_text("untracked at the root before the snapshot\n")
    snap = await g.snapshot(ws)
    assert snap is not None and snap.untracked == ("before.txt",)
    (repo / "root_new.txt").write_text("new at the root\n")
    (sub / "sub_new.txt").write_text("new in the subdir\n")
    (repo / "README.md").write_text("hello\nchanged\n")
    d = await g.diff(ws, snap)
    assert d is not None
    assert d.new_untracked == ("root_new.txt", "sub/sub_new.txt")
    assert d.files_changed == 1 and "README.md" in d.stat
    assert d.patch.startswith("diff --git a/README.md b/README.md")


async def test_snapshot_fails_loudly_on_conflicted_index(repo: Path, caplog):
    """Codex round 2: a failed `git stash create` must not become a HEAD baseline."""
    run_git(repo, "checkout", "-q", "-b", "other")
    (repo / "README.md").write_text("other\n")
    run_git(repo, "commit", "-q", "-am", "other")
    run_git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main\n")
    run_git(repo, "commit", "-q", "-am", "main")
    with pytest.raises(subprocess.CalledProcessError):
        run_git(repo, "merge", "other")  # leaves README.md unmerged
    ws = LocalWorkspace(str(repo))
    with caplog.at_level(logging.WARNING, logger="zab.workspace.git"):
        assert await g.snapshot(ws) is None
    assert "git stash create failed" in caplog.text
    assert await g.git_context(ws) is not None  # the repository itself is still usable


async def test_diff_is_repository_wide_despite_diff_relative(repo: Path):
    """Codex round 2: diff.relative=true must not hide changes outside the subdir."""
    sub = repo / "sub"
    sub.mkdir()
    (sub / "s.txt").write_text("s\n")
    run_git(repo, "add", "sub/s.txt")
    run_git(repo, "commit", "-q", "-m", "sub")
    run_git(repo, "config", "diff.relative", "true")
    ws = LocalWorkspace(str(repo), str(sub))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "README.md").write_text("hello\nroot change\n")
    (sub / "s.txt").write_text("s\nsub change\n")
    d = await g.diff(ws, snap)
    assert d is not None
    assert (d.files_changed, d.insertions, d.deletions) == (2, 2, 0)
    assert "README.md" in d.stat and "sub/s.txt" in d.stat
    assert "diff --git a/README.md b/README.md" in d.patch
    assert "diff --git a/sub/s.txt b/sub/s.txt" in d.patch


async def test_diff_hides_denylisted_paths(repo: Path):
    """Issue #36: an edit to .env or a key file never reaches the patch, only its name."""
    sub = repo / "sub"
    sub.mkdir()
    (repo / ".env").write_text("SECRET=old\n")
    (repo / "we[i]rd.pem").write_text("-----BEGIN FAKE-----\nold\n")
    (sub / "id_ed25519").write_text("fake-old\n")
    (sub / "s.txt").write_text("s\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "decoys")
    ws = LocalWorkspace(str(repo), str(sub))  # a subdir cwd, as with a configured subdir
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / ".env").write_text("SECRET=new-value-must-not-leak\n")
    (repo / "we[i]rd.pem").write_text("-----BEGIN FAKE-----\nnew-must-not-leak\n")
    (sub / "id_ed25519").write_text("fake-new-must-not-leak\n")
    (repo / "README.md").write_text("hello\nvisible change\n")
    (sub / "s.txt").write_text("s\nalso visible\n")
    (repo / "new.env").write_text("NEW=1\n")  # untracked: listed by name only
    d = await g.diff(ws, snap)
    assert d is not None
    assert "must-not-leak" not in d.patch and "must-not-leak" not in d.stat
    assert "+visible change" in d.patch and "+also visible" in d.patch
    assert (d.files_changed, d.insertions, d.deletions) == (5, 2, 0)
    assert "README.md" in d.stat and "sub/s.txt" in d.stat and "2 files changed" in d.stat
    assert " .env | changed (contents hidden)" in d.stat
    assert " we[i]rd.pem | changed (contents hidden)" in d.stat
    assert " sub/id_ed25519 | changed (contents hidden)" in d.stat
    assert d.new_untracked == ("new.env",)
    # only hidden changes: no patch, but the diff is not empty and the names are listed
    (repo / "README.md").write_text("hello\n")
    (sub / "s.txt").write_text("s\n")
    (repo / "new.env").unlink()
    d = await g.diff(ws, snap)
    assert d is not None and not d.empty
    assert d.patch == "" and (d.files_changed, d.insertions, d.deletions) == (3, 0, 0)
    assert d.stat.count("changed (contents hidden)") == 3 and "must-not-leak" not in d.stat


def _write_raw(path: bytes, data: bytes) -> None:
    """Write to a file whose name is raw bytes (sync helper: ruff ASYNC230)."""
    with open(path, "wb") as f:
        f.write(data)


async def test_diff_hides_denylisted_names_with_invalid_utf8(repo: Path):
    """Codex #50 round 4: a name git spells in bytes we cannot decode is still excluded."""
    rb = os.fsencode(str(repo))
    secret = os.path.join(rb, b".env\xff")
    glob_name = os.path.join(rb, b"se[c]ret\xff.pem")  # glob characters must be escaped
    plain = os.path.join(rb, b"data\xff.txt")  # not denylisted: its patch is shown
    for p, text in ((secret, b"SECRET=old\n"), (glob_name, b"old\n"), (plain, b"old\n")):
        _write_raw(p, text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "invalid utf-8 names")
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    for p, text in (
        (secret, b"SECRET=new-must-not-leak\n"),
        (glob_name, b"new-must-not-leak\n"),
        (plain, b"new visible\n"),
    ):
        _write_raw(p, text)
    (repo / "README.md").write_text("hello\nvisible\n")
    d = await g.diff(ws, snap)
    assert d is not None
    assert "must-not-leak" not in d.patch and "must-not-leak" not in d.stat
    assert "+new visible" in d.patch and "+visible" in d.patch
    # README.md +1; data\xff.txt +1 -1; the two hidden files count in files_changed only
    assert (d.files_changed, d.insertions, d.deletions) == (4, 2, 1)
    assert " .env� | changed (contents hidden)" in d.stat
    assert " se[c]ret�.pem | changed (contents hidden)" in d.stat
    assert "data�.txt" in d.stat and "README.md" in d.stat
    assert g._exclusion_pathspec("se[c]ret�.pem") == ":(exclude,glob)se\\[c\\]ret*.pem"
    assert g._exclusion_pathspec("plain.pem") == ":(exclude,literal)plain.pem"


async def test_diff_fails_closed_when_an_exclusion_does_not_match(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """If a denylisted name still shows after exclusion, nothing of the patch is posted."""
    (repo / ".env").write_text("SECRET=old\n")
    run_git(repo, "add", ".env")
    run_git(repo, "commit", "-q", "-m", "secret")
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / ".env").write_text("SECRET=new-must-not-leak\n")
    (repo / "README.md").write_text("hello\nvisible\n")
    monkeypatch.setattr(g, "_exclusion_pathspec", lambda path: ":(exclude,literal)nonexistent")
    d = await g.diff(ws, snap)
    assert d is not None and not d.empty
    assert d.patch == "" and "must-not-leak" not in d.stat
    assert (d.files_changed, d.insertions, d.deletions) == (2, 0, 0)
    assert d.stat.count("changed (contents hidden)") == 2
    assert " .env | changed (contents hidden)" in d.stat
    assert " README.md | changed (contents hidden)" in d.stat


async def test_diff_binary_file_counts_zero_lines(repo: Path):
    ws = LocalWorkspace(str(repo))
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    run_git(repo, "add", "blob.bin")
    run_git(repo, "commit", "-q", "-m", "binary")
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    d = await g.diff(ws, snap)
    assert d is not None
    assert (d.files_changed, d.insertions, d.deletions) == (1, 0, 0)


async def test_ensure_zab_excluded_idempotent_and_shared_by_worktrees(repo: Path, tmp_path: Path):
    ws = LocalWorkspace(str(repo))
    await g.ensure_zab_excluded(ws)
    await g.ensure_zab_excluded(ws)
    exclude = repo / ".git" / "info" / "exclude"
    assert exclude.read_text().count(".zab/") == 1
    (repo / ".zab").mkdir()
    (repo / ".zab" / "inbox.txt").write_text("x")
    assert run_git(repo, "status", "--porcelain") == ""
    wt = tmp_path / "wt"
    run_git(repo, "worktree", "add", "-q", "-b", "zab/test-abc123", str(wt), "HEAD")
    exclude.write_text("")  # remove the line, then re-add it from inside the worktree
    await g.ensure_zab_excluded(LocalWorkspace(str(wt)))
    assert exclude.read_text().count(".zab/") == 1
    assert not (wt / ".git" / "info").exists()  # .git is a file in a worktree
    (wt / ".zab").mkdir()
    (wt / ".zab" / "x").write_text("x")
    assert run_git(wt, "status", "--porcelain") == ""


async def test_ensure_zab_excluded_from_subdir(repo: Path):
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    await g.ensure_zab_excluded(LocalWorkspace(str(repo), str(sub)))
    assert (repo / ".git" / "info" / "exclude").read_text().count(".zab/") == 1
    assert not (sub / ".git").exists()


async def test_repository_hooks_do_not_run(repo: Path, tmp_path: Path):
    """Issue #36: a post-checkout hook in the checkout must not run as the bridge."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = tmp_path / "hook-fired"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho fired > {marker}\n")
    hook.chmod(0o755)
    ws = LocalWorkspace(str(repo))
    res = await g.git(ws, "worktree", "add", "-q", "-b", "zab/t-aaaaaa", "--", str(tmp_path / "a"))
    assert res.exit_code == 0, res.stderr
    assert not marker.exists()
    run_git(repo, "worktree", "add", "-q", "-b", "zab/t-bbbbbb", str(tmp_path / "b"))
    assert marker.exists()  # plain git does run it: the hook and the test are live


async def test_diff_ignores_textconv_from_the_checkout(repo: Path):
    """Issue #36: a textconv command configured in the repository must not run."""
    (repo / ".gitattributes").write_text("*.txt diff=secret\n")
    (repo / "notes.txt").write_text("v1\n")
    run_git(repo, "add", ".gitattributes", "notes.txt")
    run_git(repo, "commit", "-q", "-m", "attrs")
    run_git(repo, "config", "diff.secret.textconv", "sed s/^/PWNED:/")
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "notes.txt").write_text("v2\n")
    assert "+PWNED:v2" in run_git(repo, "diff", "HEAD")  # plain git applies it
    d = await g.diff(ws, snap)
    assert d is not None
    assert "PWNED" not in d.patch and "+v2" in d.patch and d.files_changed == 1


async def test_clean_and_smudge_filters_never_run(repo: Path, tmp_path: Path):
    """PM review on PR #50 (B1): a filter named by the checkout's .gitattributes must not run
    as the bridge in diff, stash create, the classification diff or worktree add. Plain git
    runs it in every one of those (measured on 2.43), which keeps the test live."""
    marker = tmp_path / "filter-fired"
    script = tmp_path / "filter.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(0o755)
    (repo / ".gitattributes").write_text("* filter=secret\n")
    run_git(repo, "add", ".gitattributes")
    run_git(repo, "commit", "-q", "-m", "attrs")
    run_git(repo, "config", "filter.secret.clean", str(script))
    run_git(repo, "config", "filter.secret.smudge", str(script))
    ws = LocalWorkspace(str(repo))
    g.forget_pin(str(repo))
    snap = await g.snapshot(ws)  # stash create (a clean tree: HEAD)
    assert snap is not None and not marker.exists()
    (repo / "README.md").write_text("changed\n")
    assert (await g.snapshot(ws)) is not None and not marker.exists()  # stash create, dirty
    d = await g.diff(ws, snap)
    assert d is not None and d.files_changed == 1 and "+changed" in d.patch
    assert not marker.exists()
    res = await g.git(ws, "diff", "--name-only", "-z", "HEAD", "--", "README.md", cwd=ws.root)
    assert res.exit_code == 0 and "README.md" in res.stdout and not marker.exists()
    res = await g.git(ws, "worktree", "add", "-q", "-b", "zab/f-aaaaaa", "--", str(tmp_path / "wt"))
    assert res.exit_code == 0, res.stderr
    assert (tmp_path / "wt" / "README.md").read_text() == "hello\n" and not marker.exists()
    # the filter is real: plain git runs it on the same commands
    run_git(repo, "diff", "HEAD", "--")
    assert marker.exists()
    marker.unlink()
    run_git(repo, "stash", "create")
    assert marker.exists()


async def test_attr_source_matches_the_repository_object_format(tmp_path: Path):
    """Codex round 5 on PR #50: a SHA-256 repository has another empty tree; with the SHA-1 id
    git rejects the option ("bad --attr-source") and diff and stash create fail."""
    repo = tmp_path / "r256"
    repo.mkdir()
    run_git(repo, "init", "-q", "--object-format=sha256")
    (repo / "README.md").write_text("hello\n")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-q", "-m", "initial")
    assert run_git(repo, "rev-parse", "--show-object-format") == "sha256"
    assert run_git(repo, "hash-object", "-t", "tree", os.devnull) == g.EMPTY_TREES["sha256"]
    assert g.EMPTY_TREES["sha1"] == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    ws = LocalWorkspace(str(repo))
    g.reset_pins()
    # git reads attributes, and so validates --attr-source, only when it compares a file's
    # content. On a clean tree it does that only for a "racily clean" index entry (the file's
    # mtime in the same second as the index's), so the reproduction used to pass or fail with
    # the clock. A modified file makes git read attributes every time.
    (repo / "README.md").write_text("modified\n")
    bad = await ws.run(["git", f"--attr-source={g.EMPTY_TREES['sha1']}", "diff", "HEAD", "--"])
    assert bad.exit_code != 0 and "attr-source" in bad.stderr  # the reproduction
    snap = await g.snapshot(ws)  # unpinned: the format is queried once and cached
    assert snap is not None and g._FORMATS[str(repo)] == "sha256"
    (repo / "README.md").write_text("changed\n")
    d = await g.diff(ws, snap)
    assert d is not None and d.files_changed == 1 and "+changed" in d.patch
    g.reset_pins()
    pin = await g.pin_workspace(ws)  # pinned: the format travels with the pin
    assert pin is not None and pin.object_format == "sha256"
    assert (await g.diff(ws, snap)) is not None
    g.reset_pins()


def test_parse_git_version():
    assert g.parse_git_version("git version 2.43.0\n") == (2, 43, 0)
    assert g.parse_git_version("git version 2.39.5 (Apple Git-154)") == (2, 39, 5)
    assert g.parse_git_version("git version 2.41") == (2, 41, 0)
    assert g.parse_git_version("nonsense") is None
    assert g.MIN_GIT_VERSION == (2, 41)  # --attr-source: git 2.41 release notes


async def test_git_older_than_the_minimum_is_refused_with_a_clear_message(
    repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    """Codex round 5 on PR #50: on a git without --attr-source every hardened call would fail
    with "unknown option"; the bridge says what is wrong instead, once, and degrades."""
    monkeypatch.setattr(g, "_GIT_PROBED", True)
    monkeypatch.setattr(g, "_GIT_VERSION", (2, 34, 1))
    ws = LocalWorkspace(str(repo))
    g.forget_pin(str(repo))
    res = await g.git(ws, "rev-parse", "HEAD")
    assert res.exit_code != 0 and "2.34.1" in res.stderr and "2.41" in res.stderr
    assert await g.snapshot(ws) is None and await g.git_context(ws) is None
    # the installed git passes the gate, and doctor's check says so
    monkeypatch.setattr(g, "_GIT_PROBED", False)
    monkeypatch.setattr(g, "_GIT_VERSION", None)
    with caplog.at_level("ERROR", logger="zab.workspace.git"):
        assert (await g.git(ws, "rev-parse", "HEAD")).exit_code == 0
    assert "older than" not in caplog.text
    ok, detail = await g.git_version_check()
    assert ok and detail.startswith("2.") and "2.41" in detail
    ok, detail = await g.git_version_check(os.devnull)
    assert not ok and "failed" in detail


async def test_diff_does_not_descend_into_a_submodule(repo: Path, tmp_path: Path):
    """PM review on PR #50 (B1): a submodule has its own repository and configuration; the
    bridge's diff must not run git inside it (plain git does, to report "-dirty")."""
    sub = make_repo(tmp_path / "sub")
    run_git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    run_git(repo, "commit", "-q", "-m", "add submodule")
    marker = tmp_path / "submodule-fsmonitor-fired"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nprintf '/\\0'\n")
    script.chmod(0o755)
    run_git(repo / "vendor", "config", "core.fsmonitor", str(script))
    ws = LocalWorkspace(str(repo))
    g.forget_pin(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "vendor" / "README.md").write_text("dirty inside the submodule\n")
    (repo / "README.md").write_text("changed\n")
    d = await g.diff(ws, snap)
    assert d is not None and d.files_changed == 1 and "vendor" not in d.stat
    assert "vendor" not in d.patch and not marker.exists()
    run_git(repo, "diff", "HEAD", "--")  # plain git examines the submodule and runs its monitor
    assert marker.exists()
    assert "diff.ignoreSubmodules=dirty" in g.HARDENING


async def test_pins_persist_across_a_restart_and_defeat_a_planted_git(
    repo: Path, tmp_path: Path, caplog
):
    """PM review on PR #50 (B1): after a restart the in-memory pins are empty and the old code
    re-derived them from the tree, which the agent controls. With the pins file the stored
    pin wins, a disagreement is logged, and a replaced worktree .git file redirects nothing."""
    pins = tmp_path / "state" / "workspace-pins.json"
    g.reset_pins()
    g.configure_pins(str(pins))
    wt = tmp_path / "wt"
    run_git(repo, "worktree", "add", "-q", "-b", "zab/p-aaaaaa", str(wt))
    ws = LocalWorkspace(str(wt))
    pin = await g.pin_workspace(ws)
    assert pin is not None and pin.work_tree == real(wt)
    assert pin.git_dir.startswith(real(repo / ".git"))
    saved = json.loads(pins.read_text())
    assert saved == {
        str(wt): {"git_dir": pin.git_dir, "work_tree": pin.work_tree, "object_format": "sha1"}
    }
    assert not pins.with_suffix(".json.tmp").exists()
    # the agent points the worktree's .git file at a repository of its own
    other = make_repo(tmp_path / "other")
    (other / "more.txt").write_text("more\n")
    run_git(other, "add", "more.txt")
    run_git(other, "commit", "-q", "-m", "a different HEAD")
    assert run_git(other, "rev-parse", "HEAD") != run_git(repo, "rev-parse", "HEAD")
    (wt / ".git").write_text(f"gitdir: {other / '.git'}\n")
    # restart: a fresh process loads the file; the stored pin wins and the swap is logged
    g.reset_pins()
    g.configure_pins(str(pins))
    assert g.pinned(str(wt)) == pin
    with caplog.at_level("WARNING", logger="zab.workspace.git"):
        assert await g.pin_workspace(ws) == pin
    assert "keeping the pin" in caplog.text
    ctx = await g.git_context(ws)
    assert ctx is not None and ctx.head_sha == run_git(repo, "rev-parse", "HEAD")
    assert ctx.head_sha != run_git(other, "rev-parse", "HEAD")
    # without the stored pin, the same process would have followed the planted file
    g.reset_pins()
    ctx = await g.git_context(ws)
    assert ctx is not None and ctx.head_sha == run_git(other, "rev-parse", "HEAD")
    g.reset_pins()


def test_configure_pins_tolerates_a_missing_or_corrupt_file(tmp_path: Path, caplog):
    g.reset_pins()
    g.configure_pins(str(tmp_path / "missing.json"))
    assert g.pinned("/x") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with caplog.at_level("WARNING", logger="zab.workspace.git"):
        g.configure_pins(str(bad))
    assert "not loaded" in caplog.text
    bad.write_text(
        json.dumps({"/x": {"git_dir": "/x/.git"}, "/y": {"git_dir": "/y/.git", "work_tree": "/y"}})
    )
    g.configure_pins(str(bad))
    assert g.pinned("/x") is None and g.pinned("/y") == g.GitPin(git_dir="/y/.git", work_tree="/y")
    assert g.pinned("/y").object_format == "sha1"  # an entry written before the field existed
    g.forget_pin("/y")
    assert json.loads(bad.read_text()) == {}
    g.reset_pins()


async def test_pin_defeats_nested_repository_and_inherited_git_dir(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Issue #36: a nested .git under cwd, or GIT_DIR in the environment, must not redirect."""
    nested = repo / "vendor"
    nested.mkdir()
    run_git(nested, "init", "-q")
    (nested / "x").write_text("x\n")
    run_git(nested, "add", "x")
    run_git(nested, "commit", "-q", "-m", "nested")
    ws = LocalWorkspace(str(repo), str(nested))
    g.forget_pin(str(repo))
    ctx = await g.git_context(ws)
    assert ctx is not None and ctx.repo_root == real(nested)  # unpinned: redirected
    pin = await g.pin_workspace(ws)
    assert pin == g.GitPin(
        git_dir=run_git(repo, "rev-parse", "--absolute-git-dir"),
        work_tree=run_git(repo, "rev-parse", "--show-toplevel"),
    )
    assert await g.pin_workspace(ws) is pin  # idempotent
    ctx = await g.git_context(ws)
    assert ctx is not None and ctx.repo_root == real(repo)
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "README.md").write_text("changed\n")
    d = await g.diff(ws, snap)
    assert d is not None and d.files_changed == 1 and "README.md" in d.stat
    # an inherited GIT_DIR is dropped from the environment even without a pin
    g.forget_pin(str(repo))
    other = make_repo(tmp_path / "other")
    (other / "more.txt").write_text("more\n")
    run_git(other, "add", "more.txt")
    run_git(other, "commit", "-q", "-m", "a different HEAD")
    repo_head = run_git(repo, "rev-parse", "HEAD")  # before GIT_DIR redirects run_git too
    other_head = run_git(other, "rev-parse", "HEAD")
    assert repo_head != other_head
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    ctx = await g.git_context(LocalWorkspace(str(repo)))
    assert ctx is not None and ctx.repo_root == real(repo)
    assert ctx.head_sha == repo_head


async def test_pin_workspace_outside_repo_records_nothing(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await g.pin_workspace(LocalWorkspace(str(plain))) is None
    assert g.pinned(str(plain)) is None


class _FailingRun:
    """A Workspace whose git calls fail the way a hung or broken machine fails."""

    def __init__(self, inner: LocalWorkspace, exc: BaseException) -> None:
        self._inner, self._exc = inner, exc
        self.root, self.cwd = inner.root, inner.cwd

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def run(self, argv, *, cwd=None, timeout_s=10.0):
        raise self._exc


@pytest.mark.parametrize("exc", [TimeoutError("git hung"), OSError(2, "git missing")])
async def test_queries_return_none_when_git_cannot_run(repo: Path, exc: BaseException, caplog):
    """Issue #36: snapshot, diff and git_context degrade instead of raising."""
    ws = _FailingRun(LocalWorkspace(str(repo)), exc)
    with caplog.at_level(logging.WARNING, logger="zab.workspace.git"):
        assert await g.snapshot(ws) is None
        assert await g.diff(ws, g.Snapshot(ref="HEAD")) is None
        assert await g.git_context(ws) is None
    assert caplog.text.count(type(exc).__name__) == 3
    assert "git hung" not in caplog.text and "git missing" not in caplog.text


async def test_ensure_zab_excluded_outside_repo_is_noop(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    await g.ensure_zab_excluded(LocalWorkspace(str(plain)))
    assert list(plain.iterdir()) == []


async def test_diff_files_list_every_touched_file_committed_or_not(repo: Path):
    """rc6 (#36, Mon 18:25): a turn whose agent commits its work showed every new file only
    in the stat; `files` lists them all with status and counts, then the untracked ones."""
    (repo / "gone.txt").write_text("a\nb\nc\n")
    (repo / "blob.bin").write_bytes(b"\x00\x01")
    run_git(repo, "add", "gone.txt", "blob.bin")
    run_git(repo, "commit", "-q", "-m", "before")
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "curves.py").write_text("x = 1\ny = 2\n")  # new, committed during the turn
    run_git(repo, "add", "curves.py")
    run_git(repo, "commit", "-q", "-m", "turn work")
    (repo / "README.md").write_text("goodbye\nand more\n")  # modified, not committed
    (repo / "gone.txt").unlink()  # deleted
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")  # binary
    (repo / ".env").write_text("TOKEN=decoy\n")  # untracked and denylisted: never read
    (repo / "notes.md").write_text("one\ntwo\nthree")  # untracked, no final newline
    os.mkfifo(repo / "pipe")  # a FIFO: git does not list it, and it is never opened
    d = await g.diff(ws, snap)
    assert d is not None
    by = {f.path: f for f in d.files}
    assert [f.path for f in d.files] == [
        "README.md",
        "blob.bin",
        "curves.py",
        "gone.txt",
        ".env",
        "notes.md",
    ]
    assert (by["curves.py"].status, by["curves.py"].insertions, by["curves.py"].deletions) == (
        "new",
        2,
        0,
    )
    assert (by["README.md"].status, by["README.md"].insertions, by["README.md"].deletions) == (
        "modified",
        2,
        1,
    )
    assert (by["gone.txt"].status, by["gone.txt"].deletions) == ("deleted", 3)
    assert by["blob.bin"].binary and by["blob.bin"].insertions is None
    assert by[".env"].hidden and by[".env"].status == "new" and by[".env"].insertions is None
    assert (by["notes.md"].status, by["notes.md"].insertions) == ("new", 3)
    # counting opens regular files only: a FIFO (it would block), a symlink, a missing path
    os.symlink("notes.md", repo / "link.md")
    counts = g._count_untracked(str(repo), ["pipe", "link.md", "missing", "notes.md"])
    assert counts == {
        "pipe": (None, False),
        "link.md": (None, False),
        "missing": (None, False),
        "notes.md": (3, False),
    }
    # codex on #164: a hard link named innocently to a credential is never read, as for
    # snapshots (it is listed, without counts)
    secret = repo.parent / "credential"
    secret.write_text("TOKEN=decoy\nmore\n")
    os.link(secret, repo / "notes2.txt")
    assert g._count_untracked(str(repo), ["notes2.txt"]) == {"notes2.txt": (None, False)}


async def test_diff_files_hide_tracked_denylisted_changes_and_skip_big_untracked(
    repo: Path, monkeypatch
):
    (repo / "id_rsa").write_text("decoy\n")
    run_git(repo, "add", "id_rsa")
    run_git(repo, "commit", "-q", "-m", "a key by mistake")
    ws = LocalWorkspace(str(repo))
    snap = await g.snapshot(ws)
    assert snap is not None
    (repo / "id_rsa").write_text("changed decoy\n")
    (repo / "big.csv").write_text("row\n" * 100)
    monkeypatch.setattr(g, "COUNT_FILE_CAP", 64)  # big.csv is 400 bytes
    d = await g.diff(ws, snap)
    assert d is not None
    by = {f.path: f for f in d.files}
    assert by["id_rsa"].hidden and by["id_rsa"].insertions is None
    assert by["big.csv"].status == "new" and by["big.csv"].insertions is None
