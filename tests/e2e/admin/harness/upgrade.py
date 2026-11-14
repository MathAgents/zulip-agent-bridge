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

**The HTTPS form** (#959): with `ZAB_ADMIN_E2E_PUBLIC_RELEASE_REPO`, a **public** test repository,
`MathAgents/zab-test-release-*` alone (`allowed_public`), which the same token may push to, the
good rc is planned and applied from there, `release_repo` in its anonymous form (`https_url`),
and the server's deploy key set aside meanwhile (`KEY_ASIDE`, `KEY_BACK`), as a server that
reads a public repository has none. Unset, A10b runs over SSH alone, and says so. What is pushed
there is public at once, so `publish` pushes only exports of the commit under test, this run's
(`Releases.exports_of`), each tree first checked as a real publication checks it, with
`tools/publish/export.py`'s own functions (`publication_problems`: the dropped-path links and
the rest of `check_tree`, the pinned gitleaks with both configurations and its canaries); then
`guard` asks GitHub that the repository is public (#977's review, B1). So the HTTPS case runs
from the private tree alone: a public export's tree has no `tools/publish/` (`export_tool`).

**The review's text** (#963): the good rc carries `HIDDEN`, non-ASCII text and control
characters, in a privileged file and as its CHANGELOG section. The plan's review, as the server
showed it (its deploy.sh's `sanitize`, then the helper's `shown()`), must be the deployer's own
`.review` byte for byte (`planned_review`, `reviewed`); its CHANGELOG section, which `shown()`
alone shows, the server's perl's `sanitize` of the same bytes (`sanitized_on`); both, `SHOWN`.
"""

from __future__ import annotations

import base64
import errno
import importlib.util
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

from e2e.admin.harness.release import Local, local
from zab.admin.upgradehelper import REST

ENV = "ZAB_ADMIN_E2E_RELEASE_REPO"
PUBLIC_ENV = "ZAB_ADMIN_E2E_PUBLIC_RELEASE_REPO"  # #959: the HTTPS form's, public
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
#: As root on the server: the deploy key back, if a run that set it aside (`KEY_ASIDE`) ended
#: before it could put it back; then made once (BOOTSTRAP §13.4), then its public half.
MAKE_KEY = r"""set -eu
[ -e "$1" ] || [ ! -e "$1.aside" ] || mv -T -- "$1.aside" "$1"
[ -e "$1" ] || ssh-keygen -q -t ed25519 -N '' -C zab-admin-e2e-release -f "$1"
chmod 0600 "$1"
cat -- "$1.pub"
"""
#: As root on the server (#959): the deploy key set aside, so that a fetch over SSH would fail,
#: never over a key set aside before (#977's review, N3), and put back, never over a key there.
KEY_ASIDE = (
    'set -eu\n[ ! -e "$1.aside" ] || { echo "$1.aside is there already" >&2; exit 1; }\n'
    'mv -T -- "$1" "$1.aside"\n'
)
KEY_BACK = 'set -eu\n[ -e "$1" ] || [ ! -e "$1.aside" ] || mv -T -- "$1.aside" "$1"\n'
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


#: The HTTPS form's public test repository (#959): `MathAgents/zab-test-release-public`, or
#: another `MathAgents/zab-test-release-*`, never a participant's `zab-test*` (#977's review, N2).
PUBLIC_PREFIX = "zab-test-release-"


def allowed_public(repo: str) -> bool:
    """Whether `repo` may be the HTTPS form's public test repository: `allowed`, and named
    `MathAgents/zab-test-release-*`."""
    return allowed(repo) and repo.partition("/")[2].lower().startswith(PUBLIC_PREFIX)


def release_repo(env: Mapping[str, str] | None = None, name: str = ENV) -> str:
    """`OWNER/NAME`, one of `MathAgents/zab-test*`, or "" when the rig has none (A10b skips);
    with `name`, `PUBLIC_ENV`'s (#959; unset, the HTTPS form isn't covered), which must be
    `allowed_public` and another repository than `ENV`'s."""
    given = os.environ if env is None else env
    value = given.get(name, "").strip()
    if value and not _REPO.fullmatch(value):
        raise ValueError(f"{name}: not OWNER/NAME: {value!r}")
    if value and not allowed(value):
        raise ValueError(
            f"{name}: {value!r} is not one of MathAgents/zab-test*: A10b changes no other"
        )
    if value and name != ENV and not allowed_public(value):
        raise ValueError(f"{name}: {value!r} is not one of MathAgents/{PUBLIC_PREFIX}*")
    if value and name != ENV and value.lower() == given.get(ENV, "").strip().lower():
        raise ValueError(f"{name}: {value!r} is {ENV}'s too: the HTTPS form takes a public one")
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


def guard(repo: str, token: Token, run: Local = local, *, private: bool = True) -> None:
    """Before any change on GitHub: `repo` is a test participant's, by its name and by GitHub's
    own (a renamed repository's old name redirects to it), and private; or, `private` False,
    public and `allowed_public` (#959's, `PUBLIC_ENV`). Raises otherwise."""
    fits = allowed if private else allowed_public
    if not fits(repo):
        names = "MathAgents/zab-test*" if private else f"MathAgents/{PUBLIC_PREFIX}*"
        raise ValueError(f"{repo!r} is not one of {names}: A10b changes no other")
    _tools(run)
    ask = ["gh", "api", f"repos/{repo}", "--jq", "[.full_name, .private] | @tsv"]
    seen = run(ask, env=_token_env(token), timeout=60)
    if not seen.ok:
        raise RuntimeError(f"gh api repos/{repo}: {seen.stderr.strip()[-300:]}")
    full, _, said = seen.stdout.strip().partition("\t")
    if full.lower() != repo.lower() or not fits(full):
        raise ValueError(
            f"{repo} is {full or 'nothing'} on GitHub: A10b changes only the one named"
        )
    if private and said != "true":
        raise ValueError(f"{repo} is not private: A10b pushes its test tags only to a private one")
    if not private and said != "false":
        raise ValueError(f"{repo} is not public: the HTTPS form ({PUBLIC_ENV}) reads a public one")


def ssh_url(repo: str) -> str:
    """What `release_repo` in the server's admin.toml says, over SSH."""
    return f"git@github.com:{repo}.git"


def https_url(repo: str) -> str:
    """What it says in its anonymous form, for a public repository (#959)."""
    return f"https://github.com/{repo}.git"


def push(repo_dir: Path, repo: str, token: Token, *refs: str, run: Local = local) -> None:
    """`refs` (`refs/tags/vX`, or `:refs/tags/vX` to delete one) pushed to the private test
    release repository, forced: each case of A10b signs one release name again."""
    guard(repo, token, run)
    _send(repo_dir, repo, token, refs, run)


def _send(repo_dir: Path, repo: str, token: Token, refs: tuple[str, ...], run: Local) -> None:
    """The push itself, once its caller's checks passed."""
    argv = ["git", "-C", str(repo_dir), "-c", "credential.helper=", "-c",
            f"credential.helper={CREDENTIAL}", "push", "-q", "--force",
            https_url(repo), *refs]  # fmt: skip
    done = run(argv, env=_token_env(token), timeout=600)
    if not done.ok:
        raise RuntimeError(f"git push to {repo}: {done.stderr.strip()[-300:]}")


#: The publication tool of this checkout: its own checks run on what A10b makes public.
EXPORT_TOOL = Path(__file__).resolve().parents[4] / "tools" / "publish" / "export.py"
#: Where it keeps the pinned gitleaks archive, as its own `--cache` default.
GITLEAKS_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "zab-publish"


def export_tool(path: Path = EXPORT_TOOL) -> ModuleType:
    """`tools/publish/export.py`, loaded by its path (it is no package). A public export's tree
    has no `tools/publish/` (`DROP`), so the HTTPS case's publication needs the private tree,
    and says so (#977's Codex round 2)."""
    if not path.is_file():
        raise RuntimeError(
            f"{path}: no publication tool in this tree (a public export drops tools/publish/): "
            "A10b's HTTPS case publishes only from the private tree"
        )
    spec = importlib.util.spec_from_file_location("zab_publish_export", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path}: not loadable")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


#: A scan of one tree, into a directory of its own: its findings, as problems.
Scan = Callable[[Path, Path], list[str]]


def gitleaks_scan(tool: ModuleType, work: Path) -> Scan:
    """The publication's scan: the pinned gitleaks (its archive checked against its pinned
    SHA-256), its canaries first, then each tree with both configurations; a tree with a
    `.gitleaksignore` at its root is refused, as the tool refuses one. `work` is made first:
    the tool unpacks the binary into it, and its canaries go there (#977's Codex round 1)."""
    work.mkdir(parents=True, exist_ok=True)
    binary, _ = tool.gitleaks(GITLEAKS_CACHE, work)
    configs = [tool.HERE / name for name in tool.CONFIGS]
    tool.canary_check(binary, configs, work)

    def scan(tree: Path, where: Path) -> list[str]:
        if (tree / ".gitleaksignore").exists():
            return ["the tree has a .gitleaksignore at its root, which gitleaks honours"]
        found = tool.scan_both(binary, configs, tree, where, "gitleaks")
        return [f"scan: {f['RuleID']} {f['File']}:{f['StartLine']}" for f in found]

    return scan


def publication_problems(
    rel, exports: list[tuple[str, str]], work: Path, tool: ModuleType | None = None,
    scan: Scan | None = None,
) -> list[str]:  # fmt: skip
    """What a real publication (`tools/publish/export.py`) would refuse in the trees of
    `exports`, `(public, private)` pairs, each tree once, with the tool's own functions: the
    public tree must be the private commit's less `DROP` (`expected_tree_id`); `check_tree`
    (the required files, no Markdown link into a dropped path, nothing the tree's .gitignore
    keeps out); the tool must be the commit's own (`tool_problems`); and the scan (`scan`, the
    pinned gitleaks by default). Not its run of the offline suite, which CI ran on the commit."""
    tool = tool or export_tool()
    if scan is None:
        scan = gitleaks_scan(tool, work)
    problems: list[str] = []
    seen: set[str] = set()
    for public, private in exports:
        tree_id = rel.public_git("rev-parse", f"{public}^{{tree}}")
        if tree_id in seen:
            continue
        seen.add(tree_id)
        where = work / tree_id[:12]
        (where / "tree").mkdir(parents=True)
        _, gone = tool.build_tree(rel.rig.repo, private, where / "tree")
        said = []
        if tool.expected_tree_id(rel.rig.repo, private) != tree_id:
            said.append(f"its tree is not the export of {private[:12]}")
        said += tool.check_tree(where / "tree", gone)
        said += tool.tool_problems(rel.rig.repo, private, [])
        said += scan(where / "tree", where)
        problems += [f"{public[:12]}: {line}" for line in said]
    return problems


def publish(rel, repo: str, token: Token, base: str, refs: tuple[str, ...], work: Path,
            run: Local = local, tool: ModuleType | None = None,
            scan: Scan | None = None) -> list[tuple[str, str]]:  # fmt: skip
    """`refs` pushed to the HTTPS form's public test repository (#959), forced, and public at
    once (#977's review, B1): only when every commit they reach is an export of a test release
    of `base`, the commit under test (`Releases.exports_of`), and each tree has passed a real
    publication's checks (`publication_problems`); then `guard`, which asks GitHub that the
    repository is public. ValueError otherwise, with nothing pushed. The exports pushed."""
    if not allowed_public(repo):
        raise ValueError(f"{repo!r} is not one of MathAgents/{PUBLIC_PREFIX}*: nothing pushed")
    exports = rel.exports_of(base, *refs)
    problems = publication_problems(rel, exports, work, tool, scan)
    if problems:
        raise ValueError(f"not published, nothing pushed: {'; '.join(problems)[:2000]}")
    guard(repo, token, run, private=False)
    _send(rel.public, repo, token, refs, run)
    return exports


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


def with_release_repo(toml: str, url: str) -> str:
    """The rig's admin.toml with `release_repo = "<url>"`, once (`ssh_url`, `https_url`)."""
    kept = [line for line in toml.splitlines() if not line.startswith("release_repo ")]
    head = next(i for i, line in enumerate(kept) if line.startswith("["))
    kept.insert(head, f'release_repo = "{url}"')
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


# ------------------------------------------------------------------ the review's text (#963)

#: What the good rc's review holds (#963): non-ASCII text that `sanitize` and `shown()` both keep
#: (`·`, `—`), and characters of the classes they both make visible, each class here:
#: C0 controls, on an ASCII line and on a non-ASCII one, C1 controls, format characters, line
#: and paragraph separators, spaces other than ASCII's, default-ignorable code points, private
#: use, encoded surrogates, which perl's lax `utf8::decode` takes and so does the helper
#: (#963's review, B1), and a line that isn't UTF-8. A tab stays. No NUL: deploy.sh would take
#: the file for a binary one. Not here: a code point above U+10FFFF, which perl decodes too and
#: `sanitize` writes back raw, where the helper shows its bytes as `<XX>`: both visible, apart.
HIDDEN = (
    "Non-ASCII text, kept: a middle dot \u00b7 and an em dash \u2014; "
    "on their line, CR \r and ESC \x1b[1m.\n"
    "C0 controls: ESC \x1b[0m, BEL \x07, VT \x0b, FF \x0c, CR \r, DEL \x7f; a tab\tkept.\n"
    "C1 controls: NEL \x85, CSI \x9b.\n"
    "Format: ZWSP \u200b, RLO \u202e, LRI \u2066, BOM \ufeff, soft hyphen \xad, tag \U000e0001.\n"
    "Separators: line \u2028## no heading, paragraph \u2029.\n"
    "Spaces: no-break \xa0, ideographic \u3000.\n"
    "Ignorable: CGJ \u034f, fillers \u115f \u3164, selectors \ufe0f \U000e0100.\n"
    "Private use: \ue000.\n"
).encode() + (
    b"Surrogates, encoded: \xed\xa0\x80 and \xed\xbf\xbf, beside a \xc2\xb7.\n"
    b"Not UTF-8: \xff, and a cut sequence \xe2\x80.\n"
)
#: HIDDEN's lines as both show them: deploy.sh's `sanitize` and the plan helper's `sanitized()`.
SHOWN = (
    "Non-ASCII text, kept: a middle dot \u00b7 and an em dash \u2014; "
    "on their line, CR ^M and ESC ^[[1m.",
    "C0 controls: ESC ^[[0m, BEL ^G, VT ^K, FF ^L, CR ^M, DEL ^?; a tab\tkept.",
    "C1 controls: NEL <U+0085>, CSI <U+009B>.",
    "Format: ZWSP <U+200B>, RLO <U+202E>, LRI <U+2066>, BOM <U+FEFF>, soft hyphen <U+00AD>, "
    "tag <U+E0001>.",
    "Separators: line <U+2028>## no heading, paragraph <U+2029>.",
    "Spaces: no-break <U+00A0>, ideographic <U+3000>.",
    "Ignorable: CGJ <U+034F>, fillers <U+115F> <U+3164>, selectors <U+FE0F> <U+E0100>.",
    "Private use: <U+E000>.",
    "Surrogates, encoded: <U+D800> and <U+DFFF>, beside a \u00b7.",
    "Not UTF-8: <FF>, and a cut sequence <E2><80>.",
)
#: As any user on the server: deploy.sh's `sanitize`, its perl program `$1`, of the bytes on
#: standard input, in base64; perl with a clean environment, as the plan helper runs deploy.sh
#: (its PATH, LANG, and the LC_ALL deploy.sh sets), never the login's: a PERL5OPT or a
#: PERL_UNICODE there would change what perl shows (#963's review, N2).
SANITIZE = (
    'set -euo pipefail\nbase64 -d | env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C perl -ne "$1"\n'
)
#: Each side's perl, and the python3 the plan helper runs, with their Unicode versions.
PERL_UNICODE = ["perl", "-MUnicode::UCD", "-e",
                'print "perl $^V, Unicode ", Unicode::UCD::UnicodeVersion()']  # fmt: skip
PYTHON_UNICODE = ["/usr/bin/python3", "-I", "-c", "import sys, unicodedata; print(f'python3 "
                  "{sys.version.split()[0]}, Unicode {unicodedata.unidata_version}')"]  # fmt: skip
_REVIEW_HEAD = re.compile(r"^==== The review(?: \([^\n]*\))? ====\n", re.M)


def section(version: str) -> bytes:
    """The CHANGELOG section a plan of `version` shows (the helper's `changelog`): `## X.Y.Z`,
    the release's own for an rc too, then HIDDEN."""
    return _heading(version).encode() + HIDDEN


def shown_section(version: str) -> str:
    """`section` as the plan must show it, HIDDEN as SHOWN."""
    return _heading(version) + "\n".join(SHOWN) + "\n"


def _heading(version: str) -> str:
    final = re.sub(r"rc[0-9]+$", "", version.removeprefix("v"))
    return f"## {final} \u2014 A10b's review (#963)\n\n"


def review_file(version: str) -> str:
    """Where the good rc `version` carries HIDDEN in its review: a privileged file (the plan
    leaves out the review's unprivileged rest), which nothing on the server reads; named for
    the release, so that every good rc adds one, `P A` with HIDDEN's lines as `+`, even after
    an earlier A10b's good rc, which held its own, now `P D` (#963's review, B2)."""
    return f"workshop/adminserver/a10b-review-{version.removeprefix('v')}.md"


def hidden_files(version: str) -> dict[str, bytes]:
    """The good rc's files besides its version (`Releases.release`): HIDDEN in `review_file`,
    and a CHANGELOG.md of one section, `section`. Neither reaches the server's release but as a
    file nothing reads: CHANGELOG.md is no release path."""
    return {review_file(version): HIDDEN, "CHANGELOG.md": b"# Changelog\n\n" + section(version)}


def planned_review(text: str) -> str:
    """The review in a plan's text (the helper's `plan_text`): what follows its `==== The review`
    line, up to the blank line before the CHANGELOG's section."""
    head = _REVIEW_HEAD.search(text)
    end = text.find("\n\n==== CHANGELOG.md, ", head.end()) if head else -1
    if head is None or end < 0:
        raise ValueError("the plan's text has no review followed by a CHANGELOG section")
    return text[head.end() : end]


def planned_changelog(text: str, version: str) -> str:
    """The CHANGELOG section in a plan's text of `version`: all that follows its heading."""
    head = f"\n==== CHANGELOG.md, {version.removeprefix('v')} ====\n"
    if head not in text:
        raise ValueError(f"the plan's text has no CHANGELOG section for {version}")
    return text.split(head, 1)[1]


def reviewed(out: Path, base: str, commit: str) -> str:
    """The review a deployer's `deploy.sh --review <commit> --since <base> --out <out>` wrote,
    `zab-admin-<base>..<12 hex>.review`, as a plan shows it: up to its unprivileged rest, the
    newlines before that left out (`plan_text`). Its bytes, decoded strictly."""
    data = (out / f"zab-admin-{base}..{commit[:12]}.review").read_bytes()
    return data.decode("utf-8").split(REST, 1)[0].rstrip("\n")


def sanitize_program(deploy_sh: str) -> str:
    """The perl program of deploy.sh's `sanitize`, from the script's text."""
    _, head, rest = deploy_sh.partition("\nsanitize() {\n")
    found = re.search(r"\bperl -ne '([^']+)'", rest.split("\n}\n", 1)[0]) if head else None
    if found is None:
        raise ValueError("deploy.sh has no sanitize() with a perl program")
    return found.group(1)


def sanitized_on(server, program: str, data: bytes) -> str:
    """`data` as deploy.sh's `sanitize` (`program`) shows it with the server's perl."""
    sent = base64.b64encode(data).decode("ascii")
    return server.script(SANITIZE, program, input=sent).stdout


def first_difference(ours: str, theirs: str, names: tuple[str, str]) -> str:
    """Where two texts first differ, by line, each side's line quoted in ASCII."""
    a, b = ours.split("\n"), theirs.split("\n")
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1

    def quoted(lines: list[str]) -> str:
        return ascii(lines[n]) if n < len(lines) else "(none: the text ends)"

    return f"line {n + 1}: {names[0]} {quoted(a)}, {names[1]} {quoted(b)}"


def unicode_versions(server, run: Local = local) -> str:
    """What decides how each side shows text: the server's perl (deploy.sh's `sanitize`) and
    the python3 its plan helper runs (`shown()`), and the development machine's perl (the
    deployer's review), each with its Unicode version; `?` for one that didn't answer."""

    def said(done) -> str:
        return done.stdout.strip() if done.ok and done.stdout.strip() else "?"

    there = [said(server.run(argv, check=False)) for argv in (PERL_UNICODE, PYTHON_UNICODE)]
    return f"the server: {there[0]}, {there[1]}; the development machine: {said(run(PERL_UNICODE))}"
