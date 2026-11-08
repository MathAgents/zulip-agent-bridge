"""A10b's rig: `!upgrade-server` in-band (docs/ADMIN.md §1.6, §7; #569).

The test server fetches its releases from a **test release repository** on GitHub, never the zab
repository: `ZAB_ADMIN_E2E_RELEASE_REPO`, `OWNER/NAME`, which plays the public one (#958). The rig
pushes the test tags there from its own public repository (`Releases.public`: exported commits,
their tags signed with the rig's test-only key), forced, and deletes one, and adds the server's
deploy key, with a token for that repository alone: the file `ZAB_ADMIN_E2E_RELEASE_TOKEN_FILE`
names, the user's own, mode 0600 (`release_token`). The development machine's own `gh` sign-in may
be scoped to another account. The token goes to those `gh` and `git` calls alone, as `GH_TOKEN` in
their environment, never in an argument; no frame of the harness's own shows it, and the runner's
sweep of pytest.log redacts what the harness can't wrap. The server reads the repository with a
deploy key of its own, made on the server (root 0600) and added read-only, as BOOTSTRAP §13.4 has a
deployer do.

**Only a test participant's repository, and only a private one** (#902's Codex round 1): those
credentials reach the zab repository too. So the name must be `MathAgents/zab-test*`, compared as
GitHub compares names, without regard to case, when it is read (`release_repo`) and again before
each change on GitHub (`guard`), which also asks GitHub that the repository is private and is
the one named, not a redirect from an old name to another repository.

Either unset, A10b skips, naming what it needs.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from e2e.admin.harness.release import Local, local

ENV = "ZAB_ADMIN_E2E_RELEASE_REPO"
TOKEN_ENV = "ZAB_ADMIN_E2E_RELEASE_TOKEN_FILE"  # noqa: S105 - a variable's name, not a token
NO_REPO = (
    f"A10b needs {ENV}=OWNER/NAME, a private test release repository (MathAgents/zab-test*), "
    f"and {TOKEN_ENV}, a file (yours, mode 0600) holding a token that may push to it and add "
    "deploy keys to it"
)
_REPO = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}"
)
KEY = "/etc/zab-admin/secrets/release-key"
#: As root on the server: the deploy key, made once (BOOTSTRAP §13.4), then its public half.
MAKE_KEY = r"""set -eu
[ -e "$1" ] || ssh-keygen -q -t ed25519 -N '' -C zab-admin-e2e-release -f "$1"
chmod 0600 "$1"
cat -- "$1.pub"
"""
#: git's credential helper for an HTTPS push: the development machine's gh sign-in, as
#: `gh auth setup-git` configures it (a fixed command, never one built from anything here).
CREDENTIAL = "!gh auth git-credential"


#: The test participant's repositories: `MathAgents/zab-test*`, the only ones besides zab's own
#: that a test pushes to. Lower case: GitHub's names are compared without regard to case.
OWNER, PREFIX = "mathagents", "zab-test"


def allowed(repo: str) -> bool:
    """Whether `repo` is one of the test participant's repositories, `MathAgents/zab-test*`."""
    owner, _, name = repo.partition("/")
    return (
        bool(_REPO.fullmatch(repo)) and owner.lower() == OWNER and name.lower().startswith(PREFIX)
    )


def release_repo(env: Mapping[str, str] | None = None) -> str:
    """`OWNER/NAME`, one of `MathAgents/zab-test*`, or "" when the rig has none (A10b skips)."""
    value = (os.environ if env is None else env).get(ENV, "").strip()
    if value and not _REPO.fullmatch(value):
        raise ValueError(f"{ENV}: not OWNER/NAME: {value!r}")
    if value and not allowed(value):
        raise ValueError(
            f"{ENV}: {value!r} is not one of MathAgents/zab-test*: A10b changes no other"
        )
    return value


class Token:
    """The test release repository's token. Its repr and str are redacted, so that no frame of
    the harness's own shows it: pytest prints a failing frame's arguments, and its locals under
    `-l` (#902's re-review, B1). `_token_env` alone unwraps it, into a call's `GH_TOKEN`. What
    the harness can't wrap, a standard-library frame under `-l` say, the runner's sweep of
    pytest.log redacts (`run.py`, `swept_values`), reading `value`."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "Token(<redacted>)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    @property
    def value(self) -> str:
        return self._value


class _Env(dict):
    """A call's environment, holding the token as `GH_TOKEN`: its repr is redacted, as the
    token's, for the frames that take it as an argument."""

    def __repr__(self) -> str:
        return "<the environment, with GH_TOKEN, redacted>"


#: What a refusal may quote of `TOKEN_ENV`'s value: an absolute path of path-safe characters.
#: Anything else, a token put in the variable itself say, is never quoted (#902's review, B1).
_QUOTABLE = re.compile(r"/[A-Za-z0-9._@+/-]{1,4095}")
#: Variables that would have `gh` or git trace a call, its headers among them: never passed.
_TRACING = ("GH_DEBUG", "GIT_TRACE", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE")


def _named(path: str) -> str:
    """How a refusal names the token file: its path, if that is one to quote."""
    return f"{TOKEN_ENV}: {path}" if _QUOTABLE.fullmatch(path) else f"{TOKEN_ENV} (not a path)"


def _open_regular(path: str) -> int | None:
    """A read-only descriptor of `path`, never through a link, when it is a regular file; None
    otherwise. The caller closes it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:
        os.close(fd)
        return None
    return fd


def release_token(env: Mapping[str, str] | None = None) -> Token:
    """The token for the test release repository, from the file `TOKEN_ENV` names; an empty one
    when it is unset (A10b skips). The file must be named by an absolute path, and be the user's
    own regular file, mode 0600, holding one token, opened once, never through a link, and
    checked as opened (`fstat`, before anything reads it). Anything else is a ValueError naming
    the file, or saying it isn't a path, never quoting the variable otherwise nor the file's
    content."""
    path = (os.environ if env is None else env).get(TOKEN_ENV, "").strip()
    if not path:
        return Token("")
    if not _QUOTABLE.fullmatch(path):
        raise ValueError(f"{_named(path)}: name the token's file by its absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        why = "is a link" if exc.errno == errno.ELOOP else exc.strerror
        raise ValueError(f"{_named(path)}: {why}: your own regular file, mode 0600") from None
    try:
        found = os.fstat(fd)
        mine = stat.S_ISREG(found.st_mode) and found.st_uid == os.getuid()
        if not mine or stat.S_IMODE(found.st_mode) != 0o600:
            raise ValueError(f"{_named(path)} must be your own regular file, mode 0600")
        with os.fdopen(fd, "rb") as f:
            fd = -1  # the file object closes it now
            # wrapped as it is read: no plain local holds the file's text, which a refused
            # one's traceback would print with `-l` (#902's Codex round 2)
            read = Token(f.read(4096).decode("utf-8", "replace").strip())
    except OSError as exc:
        raise ValueError(f"{_named(path)}: {exc.strerror}") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if not read or any(c.isspace() or not c.isprintable() for c in read.value):
        raise ValueError(f"{_named(path)} holds no single token")
    return read


def raw_words(env: Mapping[str, str] | None = None) -> list[Token]:
    """For the runner's sweep when `release_token` refuses the file: every word of it, split at
    whitespace, of 20 characters or more (a token's length), each wrapped; read apart from the
    checks, through no link, a regular file alone, at most 64 KiB; none when it can't be read.
    The variable's own value is the runner's to sweep (`run.py`)."""
    path = (os.environ if env is None else env).get(TOKEN_ENV, "").strip()
    fd = _open_regular(path) if path else None
    if fd is None:
        return []
    try:
        with os.fdopen(fd, "rb") as f:
            read = Token(f.read(65536).decode("utf-8", "replace"))
    except OSError:
        return []
    return [Token(word) for word in read.value.split() if len(word) >= 20]


def _token_env(token: Token) -> _Env:
    """The environment of a call to the test release repository: the user's own, but for the
    variables that would trace the call, with its token as `GH_TOKEN`, which `gh` (and git
    through `gh auth git-credential`) take first."""
    if not isinstance(token, Token) or not token:
        raise ValueError(f"no token for the test release repository ({TOKEN_ENV})")
    kept = {name: value for name, value in os.environ.items() if name not in _TRACING}
    return _Env({**kept, "GH_TOKEN": token.value})


def _tools(run: Local) -> None:
    """With the real runner, `gh` and `git` must be there before a call: one that can't be run
    raises from inside subprocess, whose frames hold the environment, under `-l` printed
    (#902's review, N1)."""
    if run is local:
        missing = [tool for tool in ("gh", "git") if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"A10b needs {' and '.join(missing)} on PATH")


def guard(repo: str, token: Token, run: Local = local) -> None:
    """Before any change on GitHub: `repo` is a test participant's, by its name and by GitHub's
    own (a renamed repository's old name redirects to it), and private. Raises otherwise."""
    if not allowed(repo):
        raise ValueError(f"{repo!r} is not one of MathAgents/zab-test*: A10b changes no other")
    _tools(run)
    ask = ["gh", "api", f"repos/{repo}", "--jq", "[.full_name, .private] | @tsv"]
    seen = run(ask, env=_token_env(token), timeout=60)
    if not seen.ok:
        raise RuntimeError(f"gh api repos/{repo}: {seen.stderr.strip()[-300:]}")
    full, _, private = seen.stdout.strip().partition("\t")
    if full.lower() != repo.lower() or not allowed(full):
        raise ValueError(
            f"{repo} is {full or 'nothing'} on GitHub: A10b changes only the one named"
        )
    if private != "true":
        raise ValueError(f"{repo} is not private: A10b pushes its test tags only to a private one")


def ssh_url(repo: str) -> str:
    """What `release_repo` in the server's admin.toml says."""
    return f"git@github.com:{repo}.git"


def push(repo_dir: Path, repo: str, token: Token, *refs: str, run: Local = local) -> None:
    """`refs` (`refs/tags/vX`, or `:refs/tags/vX` to delete one) pushed to the test release
    repository, forced: each case of A10b signs one release name again."""
    guard(repo, token, run)
    argv = ["git", "-C", str(repo_dir), "-c", "credential.helper=", "-c",
            f"credential.helper={CREDENTIAL}", "push", "-q", "--force",
            f"https://github.com/{repo}.git", *refs]  # fmt: skip
    done = run(argv, env=_token_env(token), timeout=600)
    if not done.ok:
        raise RuntimeError(f"git push to {repo}: {done.stderr.strip()[-300:]}")


def deploy_key(server, repo: str, token: Token, run: Local = local) -> str:
    """The server's deploy key, made there if it has none, and added to the test release
    repository read-only if GitHub doesn't have it yet. Its public half."""
    guard(repo, token, run)
    public = server.script(MAKE_KEY, KEY, sudo=True).stdout.strip()
    if not public.startswith("ssh-ed25519 "):
        raise RuntimeError("the server's deploy key has no ssh-ed25519 public half")
    ask = ["gh", "api", f"repos/{repo}/keys", "--jq", ".[].key"]
    listed = run(ask, env=_token_env(token), timeout=60)  # never a local: built for each call
    if not listed.ok:
        raise RuntimeError(f"gh api repos/{repo}/keys: {listed.stderr.strip()[-300:]}")
    body = " ".join(public.split()[:2])
    if body not in {" ".join(line.split()[:2]) for line in listed.stdout.splitlines()}:
        added = run(["gh", "api", "-X", "POST", f"repos/{repo}/keys", "-f",
                     "title=zab-admin-e2e (A10b)", "-f", f"key={body}", "-F", "read_only=true"],
                    env=_token_env(token), timeout=60)  # fmt: skip
        if not added.ok:
            raise RuntimeError(f"adding the deploy key: {added.stderr.strip()[-300:]}")
    return body


def with_release_repo(toml: str, repo: str) -> str:
    """The rig's admin.toml with `release_repo` for the test release repository, once."""
    kept = [line for line in toml.splitlines() if not line.startswith("release_repo ")]
    head = next(i for i, line in enumerate(kept) if line.startswith("["))
    kept.insert(head, f'release_repo = "{ssh_url(repo)}"')
    return "\n".join(kept) + "\n"


_SHA = re.compile(r"SHA-256 ([0-9a-f]{64}); release tree")


def reviewed_sha256(script: Path, repo_dir: Path, etc: Path, commit: str, base: str, out: Path,
                    run: Local = local) -> str:  # fmt: skip
    """What a deployer's own review prints for `commit` since `base`, with `base`'s deploy.sh
    (`script`), as the plan helper reviews it (BOOTSTRAP §13.4)."""
    argv = ["bash", str(script), "--review", commit, "--since", base, "--etc", str(etc)]
    done = run([*argv, "--repo", str(repo_dir), "--out", str(out)], timeout=600)
    found = _SHA.search(done.stderr) or _SHA.search(done.stdout)
    if not done.ok or found is None:
        raise RuntimeError(f"the review of {commit[:12]}: {done.stderr.strip()[-300:]}")
    return found.group(1)
