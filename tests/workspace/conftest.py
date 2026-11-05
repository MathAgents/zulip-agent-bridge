"""Shared helpers for workspace tests: real git repositories under tmp_path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,  # never read the machine's ~/.gitconfig
    "GIT_CONFIG_NOSYSTEM": "1",
}


def real(path: str | Path) -> str:
    """os.path.realpath, as a sync helper the async tests can call (ruff ASYNC240)."""
    return os.path.realpath(path)


def run_git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity and no user config; return stripped stdout."""
    env = dict(os.environ)
    env.update(GIT_ENV)
    res = subprocess.run(  # noqa: S603  # argument list, test helper
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def make_repo(path: Path) -> Path:
    """A repository with one commit containing ``README.md``."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q")
    (path / "README.md").write_text("hello\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> Path:
    """A repository whose ``main`` tracks a bare ``origin`` and is fully pushed."""
    path = make_repo(tmp_path / "repo")
    bare = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    run_git(path, "remote", "add", "origin", str(bare))
    run_git(path, "push", "-q", "-u", "origin", "main")
    return path


FAKE_SSH = """#!/bin/sh
# A stand-in for ssh: `<script> <root> [options] <host> "<git command> '<owner>/<repo>.git'"`
# runs the git command on <root>/<owner>/<repo>.git, so a push to any GitHub alias URL lands in
# a local bare repository over git's real ssh transport.
root=$1; shift
for last; do :; done
verb=${last%% *}; path=${last#* }; path=${path#\\'}; path=${path%\\'}
exec $verb "$root/$path"
"""


@pytest.fixture
def fake_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`git@github.com-<alias>:<owner>/<repo>.git` served from local bare repositories through a
    fake ssh in the snapshot push's GIT_SSH_COMMAND (no network, no URL rewriting). HOME is an
    empty directory, so the machine's own git config cannot rewrite URLs either. Returns
    `bare(owner_repo)`, which creates the bare repository `<owner>/<repo>.git`."""
    from zab.workspace import git as g

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "github"
    root.mkdir()
    script = tmp_path / "fake-ssh"
    script.write_text(FAKE_SSH)
    script.chmod(0o755)
    monkeypatch.setitem(g._PUSH_ENV, "GIT_SSH_COMMAND", f"{script} {root}")

    def bare(owner_repo: str) -> Path:
        path = root / owner_repo
        path.parent.mkdir(parents=True, exist_ok=True)
        run_git(tmp_path, "init", "-q", "--bare", str(path))
        return path

    return bare
