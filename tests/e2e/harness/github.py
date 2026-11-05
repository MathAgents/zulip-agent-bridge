"""Commit authors through the GitHub API (scenario 7, issue #214), with the `gh` login that is
already configured on the development machine: no token is read or passed here."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_TRAILER_RE = re.compile(r"^co-authored-by:\s*(.+)$", re.I | re.M)

Gh = Callable[[list[str]], str]


def gh(args: list[str]) -> str:
    """`gh <args>`'s stdout; an argument list, never a shell string."""
    program = shutil.which("gh")
    if program is None:
        raise RuntimeError("gh is not installed")
    done = subprocess.run(  # noqa: S603 - fixed program, argument list
        [program, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"gh {args[:2]}: exit {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout


@dataclass(frozen=True)
class Authorship:
    sha: str
    author: str
    author_email: str
    committer: str
    co_authors: tuple[str, ...]
    subject: str = ""  # the message's first line

    def problems(self, expected_author: str) -> list[str]:
        """What scenario 7 refuses: another author, or any `Co-Authored-By` trailer."""
        out = []
        if self.author != expected_author:
            out.append(f"author {self.author!r}, expected {expected_author!r}")
        if self.co_authors:
            out.append("Co-Authored-By: " + ", ".join(self.co_authors))
        return out


def parse_commit(sha: str, data: dict) -> Authorship:
    commit = data["commit"]
    return Authorship(
        sha=sha,
        author=commit["author"]["name"],
        author_email=commit["author"]["email"],
        committer=commit["committer"]["name"],
        co_authors=tuple(m.strip() for m in _TRAILER_RE.findall(commit.get("message", ""))),
        subject=(commit.get("message", "").splitlines() or [""])[0].strip(),
    )


def commit_authorship(repo: str, sha: str, *, run: Gh = gh) -> Authorship:
    """The commit as GitHub has it (it must have been pushed)."""
    if not _REPO_RE.match(repo) or not _SHA_RE.match(sha):
        raise ValueError(f"bad repository or sha: {repo!r} {sha!r}")
    return parse_commit(sha, json.loads(run(["api", f"repos/{repo}/commits/{sha}"])))


#: `git show -s` on the VM, for a commit that was not pushed: author, e-mail, committer, body.
GIT_SHOW_FORMAT = "%an%x00%ae%x00%cn%x00%B"


def parse_git_show(sha: str, text: str) -> Authorship:
    author, email, committer, body = text.split("\x00", 3)
    return Authorship(
        sha=sha,
        author=author,
        author_email=email,
        committer=committer,
        co_authors=tuple(m.strip() for m in _TRAILER_RE.findall(body)),
        subject=(body.strip().splitlines() or [""])[0].strip(),
    )
