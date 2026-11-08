"""A10b's harness (`harness/upgrade.py`, #569), offline: the test release repository's name, the
admin.toml line, the push and the deploy key, against stand-ins for git, gh and the server."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from e2e.admin.harness import upgrade
from e2e.harness.vm import Result
from zab.admin.config import RELEASE_REPO

PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
TEST_REPO = "MathAgents/zab-test-release"
#: The test release repository's own token, for these tests alone: a made-up one.
SECRET = "github_pat_" + "x" * 30  # made up
TOKEN = upgrade.Token(SECRET)
#: Names a misconfigured run must never change (#902's Codex round 1): zab's public repository and a
#: copy under another owner, a `zab-test` name under another owner, and near misses, in any case.
REFUSED = (
    "MathAgents/zulip-agent-bridge",
    "Maintainer/zulip-agent-bridge",
    "mathagents/ZULIP-AGENT-BRIDGE",
    "Maintainer/zab-test-release",
    "Other/zab-test",
    "MathAgentsX/zab-test-release",
    "Math-Agents/zab-test-release",
    "MathAgents/zab-tes",
    "MathAgents/xzab-test",
    "MathAgents/zab_test-release",
    "MathAgents/zab-test-release/../zulip-agent-bridge",
    "MathAgents/zab-test-release;x",
    "MathAgents/zab-test-r\u0435lease",  # a Cyrillic e
    "M\u0430thAgents/zab-test-release",  # a Cyrillic a
)
#: GitHub compares names without regard to case: these are the test repository itself.
SAME = ("MathAgents/zab-test-release", "mathagents/zab-test-release", "MATHAGENTS/ZAB-TEST-Release")


def test_the_release_repository_is_a_test_participants_or_none():
    assert upgrade.release_repo({}) == ""
    assert upgrade.release_repo({upgrade.ENV: f" {TEST_REPO} "}) == TEST_REPO
    for bad in ("MathAgents", "a/b/c", "https://github.com/a/b", "a/b; rm", *REFUSED):
        with pytest.raises(ValueError, match=upgrade.ENV):
            upgrade.release_repo({upgrade.ENV: bad})
    assert all(upgrade.allowed(name) for name in SAME)
    assert not any(upgrade.allowed(name) for name in REFUSED)
    assert RELEASE_REPO.fullmatch(upgrade.ssh_url(TEST_REPO))


def test_admin_toml_gets_one_release_repo_line_before_its_tables():
    toml = 'provider = "aws"\nrelease_repo = "git@github.com:Old/one.git"\n\n[global]\nx = 1\n'
    text = upgrade.with_release_repo(toml, "MathAgents/zab-test-release")
    assert text.count("release_repo") == 1
    head, tables = text.split("[global]", 1)
    assert 'release_repo = "git@github.com:MathAgents/zab-test-release.git"' in head
    assert "x = 1" in tables


class Runs:
    """A stand-in for `release.local`: each argument list, answered from `answers` by its
    first words; `tokens` keeps the `GH_TOKEN` each call's environment had."""

    def __init__(self, answers: dict[tuple[str, ...], Result]) -> None:
        self.answers, self.seen, self.tokens = answers, [], []

    def __call__(self, argv, env=None, **_):
        self.seen.append(argv)
        self.tokens.append((env or {}).get("GH_TOKEN"))
        for head, result in self.answers.items():
            if tuple(argv[: len(head)]) == head:
                return result
        return Result(0, "", "")


def github(full: str = TEST_REPO, private: str = "true") -> dict[tuple[str, ...], Result]:
    """`gh api repos/<TEST_REPO>`'s answer to `guard`: its full name and whether it is private."""
    return {("gh", "api", f"repos/{TEST_REPO}", "--jq"): Result(0, f"{full}\t{private}\n", "")}


def test_the_push_is_forced_over_https_with_the_gh_sign_in(tmp_path):
    runs = Runs(github())
    upgrade.push(tmp_path, TEST_REPO, TOKEN, "refs/tags/v0.3.1", run=runs)
    asked, argv = runs.seen
    assert asked[:3] == ["gh", "api", f"repos/{TEST_REPO}"]  # guard, first
    assert argv[:3] == ["git", "-C", str(tmp_path)]
    assert "credential.helper=!gh auth git-credential" in argv and "--force" in argv
    assert argv[-2:] == [f"https://github.com/{TEST_REPO}.git", "refs/tags/v0.3.1"]
    failing = Runs({**github(), ("git",): Result(1, "", "denied")})
    with pytest.raises(RuntimeError, match="denied"):
        upgrade.push(tmp_path, TEST_REPO, TOKEN, "refs/tags/v0.3.1", run=failing)


@pytest.mark.parametrize("name", REFUSED)
def test_nothing_on_github_changes_but_a_test_participants_repository(tmp_path, name):
    """#902's Codex round 1: a misconfigured name is refused before any call at all."""
    runs, server = Runs({}), Server()
    with pytest.raises(ValueError, match="not one of MathAgents/zab-test"):
        upgrade.push(tmp_path, name, TOKEN, ":refs/tags/v0.3.1", run=runs)
    with pytest.raises(ValueError, match="not one of MathAgents/zab-test"):
        upgrade.deploy_key(server, name, TOKEN, run=runs)
    assert runs.seen == [] and server.scripts == []


@pytest.mark.parametrize(
    ("full", "private", "why"),
    [
        (TEST_REPO, "false", "is not private"),
        ("MathAgents/zulip-agent-bridge", "true", "is MathAgents/zulip-agent-bridge on GitHub"),
        ("MathAgents/zab-test-other", "true", "is MathAgents/zab-test-other on GitHub"),
        ("", "", "is nothing on GitHub"),
    ],
)
def test_github_must_say_the_repository_is_the_one_named_and_private(tmp_path, full, private, why):
    """A public repository is refused, and so is a renamed one's old name, which GitHub
    redirects to whatever it is called now: `guard` asks GitHub before each change."""
    runs, server = Runs(github(full, private)), Server()
    with pytest.raises(ValueError, match=why):
        upgrade.push(tmp_path, TEST_REPO, TOKEN, "refs/tags/v0.3.1", run=runs)
    with pytest.raises(ValueError, match=why):
        upgrade.deploy_key(server, TEST_REPO, TOKEN, run=runs)
    assert [argv[:2] for argv in runs.seen] == [["gh", "api"]] * 2 and server.scripts == []
    failed = Runs({("gh", "api", f"repos/{TEST_REPO}", "--jq"): Result(1, "", "Not Found")})
    with pytest.raises(RuntimeError, match="Not Found"):
        upgrade.push(tmp_path, TEST_REPO, TOKEN, "refs/tags/v0.3.1", run=failed)


class Server:
    def __init__(self) -> None:
        self.scripts = []

    def script(self, literal, *args, sudo=False, **_):
        self.scripts.append((literal, args, sudo))
        return Result(0, PUBLIC + " zab-admin-e2e-release\n", "")


@pytest.mark.parametrize("listed", ["", f"{PUBLIC}\n"])
def test_the_deploy_key_is_made_on_the_server_and_added_read_only_once(listed):
    server = Server()
    runs = Runs({**github(), ("gh", "api", f"repos/{TEST_REPO}/keys"): Result(0, listed, "")})
    assert upgrade.deploy_key(server, TEST_REPO, TOKEN, run=runs) == PUBLIC
    ((literal, args, sudo),) = server.scripts
    assert sudo and args == (upgrade.KEY,) and "ssh-keygen -q -t ed25519 -N ''" in literal
    posts = [argv for argv in runs.seen if "-X" in argv]
    if listed:
        assert posts == []  # GitHub has it already
    else:
        (post,) = posts
        assert ["-f", f"key={PUBLIC}"] == post[post.index("-f") + 2 : post.index("-f") + 4]
        assert post[-2:] == ["-F", "read_only=true"]


def test_the_token_goes_to_each_call_to_the_repository_alone(tmp_path, monkeypatch):
    """The test release repository's token reaches every `gh` and `git` call to it, as
    `GH_TOKEN` in that call's environment alone: never in an argument, never in this process's
    own environment."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    runs = Runs({**github(), ("gh", "api", f"repos/{TEST_REPO}/keys"): Result(0, "", "")})
    upgrade.push(tmp_path, TEST_REPO, TOKEN, "refs/tags/v0.3.1", run=runs)
    upgrade.deploy_key(Server(), TEST_REPO, TOKEN, run=runs)
    assert len(runs.seen) == 5 and runs.tokens == [SECRET] * 5  # guard, push; guard, list, add
    assert not any(SECRET in word for argv in runs.seen for word in argv)
    assert "GH_TOKEN" not in os.environ
    with pytest.raises(ValueError, match=upgrade.TOKEN_ENV):
        upgrade.push(tmp_path, TEST_REPO, "", "refs/tags/v0.3.1", run=Runs(github()))


def test_the_variable_is_quoted_only_as_a_path_and_a_directory_refused(tmp_path):
    """#902's review, B1 and B2: a refusal quotes the variable only when it is an absolute path
    of path-safe characters, never a token put there instead, nor a relative name; a directory
    is no regular file, refused, and has no words to sweep."""
    for given in ("github_pat_" + "y" * 30, "release-token", "/home/a token"):
        with pytest.raises(ValueError, match=r"\(not a path\): name the token's file") as caught:
            upgrade.release_token({upgrade.TOKEN_ENV: given})
        assert given not in str(caught.value)
    folder = tmp_path / "token-dir"
    folder.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="must be your own regular file, mode 0600"):
        upgrade.release_token({upgrade.TOKEN_ENV: str(folder)})
    assert upgrade.raw_words({upgrade.TOKEN_ENV: str(folder)}) == []
    assert upgrade.raw_words({upgrade.TOKEN_ENV: str(tmp_path / "missing")}) == []


def test_no_call_without_gh_and_git_and_no_tracing(tmp_path, monkeypatch):
    """#902's review, N1 and N4: with the real runner, a missing `gh` or `git` is said before
    any call (one that can't run raises from subprocess, whose frames hold the environment);
    and the calls' environment drops what would have `gh` or git trace them."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="A10b needs gh and git on PATH"):
        upgrade.guard(TEST_REPO, TOKEN)
    monkeypatch.setenv("GIT_TRACE_CURL", "1")
    monkeypatch.setenv("GH_DEBUG", "api")
    env = upgrade._token_env(TOKEN)
    assert "GIT_TRACE_CURL" not in env and "GH_DEBUG" not in env and env["GH_TOKEN"] == SECRET
    assert SECRET not in repr(env)


def test_the_token_file_is_yours_alone(tmp_path):
    """`ZAB_ADMIN_E2E_RELEASE_TOKEN_FILE`: unset, A10b skips; otherwise your own regular file,
    mode 0600, holding one token, or it is refused, naming the file and never its content."""
    good = tmp_path / "token"
    good.write_text(f"{SECRET}\n")
    good.chmod(0o600)
    found = upgrade.release_token({upgrade.TOKEN_ENV: str(good)})
    assert found.value == SECRET and SECRET not in f"{found!r} {found!s} {found}"
    assert not upgrade.release_token({}) and repr(upgrade.release_token({})) == "Token(<redacted>)"
    loose = tmp_path / "loose"
    loose.write_text(SECRET)
    loose.chmod(0o644)
    link = tmp_path / "link"
    link.symlink_to(good)
    empty = tmp_path / "empty"
    empty.write_text("\n")
    empty.chmod(0o600)
    two = tmp_path / "two"
    two.write_text(f"{SECRET} {SECRET}\n")
    two.chmod(0o600)
    missing = tmp_path / "missing"
    cases = [(loose, "mode 0600"), (link, "mode 0600"), (empty, "no single token")]
    for path, why in [*cases, (two, "no single token"), (missing, "No such file")]:
        with pytest.raises(ValueError, match=why) as caught:
            upgrade.release_token({upgrade.TOKEN_ENV: str(path)})
        assert SECRET not in str(caught.value)


#: A test file run in a pytest of its own: failing calls that hold the token, as A10b's would.
FAILING = """
import pytest
from e2e.admin.harness import upgrade
from e2e.harness.vm import Result

FAKE = upgrade.Token("FAKE-TOKEN-902")
REPO = "MathAgents/zab-test-release"


def refused(argv, env=None, **_):  # a GitHub call that fails, its environment in the frame
    return Result(1, "", "HTTP 403")


def broken(argv, env=None, **_):  # one that raises, as a missing gh would
    raise OSError(2, "No such file or directory: gh")


def test_push(tmp_path):
    upgrade.push(tmp_path, REPO, FAKE, "refs/tags/v0.3.1", run=refused)


def test_deploy_key():
    upgrade.deploy_key(None, REPO, FAKE, run=refused)


def test_raised(tmp_path):
    upgrade.push(tmp_path, REPO, FAKE, "refs/tags/v0.3.1", run=broken)


def cases(where):  # A10b's `_cases`, which takes `(repo, token)`
    repo, token = where
    assert repo == "another", "an assertion of the scenario's"


def test_cases():
    cases((REPO, FAKE))


def test_malformed():  # a token file of two words, refused while it is read
    upgrade.release_token()
"""


@pytest.mark.parametrize("flags", [[], ["-l"], ["--tb=long", "-l"]])
def test_no_failing_frame_shows_the_token(tmp_path, flags):
    """#902's re-review, B1: pytest prints a failing frame's arguments, and with `-l` its
    locals. The token is a `Token` everywhere but in a call's environment, whose dict is
    redacted too: four failures, the token in none of them."""
    (tmp_path / "test_failing.py").write_text(FAILING)
    tests = str(Path(__file__).resolve().parents[2])
    malformed = tmp_path / "release-token"  # #902's Codex round 2: two words, refused
    malformed.write_text("FAKE-WORD-ONE-902-0123 FAKE-WORD-TWO-902-4567\n")
    malformed.chmod(0o600)
    env = {**os.environ, "PYTHONPATH": tests, upgrade.TOKEN_ENV: str(malformed)}
    env.pop("PYTEST_ADDOPTS", None)
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-q"]
    done = subprocess.run(  # noqa: S603 - an argument list
        [*argv, "-rA", *flags, str(tmp_path / "test_failing.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=100, check=False,
    )  # fmt: skip
    output = done.stdout + done.stderr
    assert "5 failed" in output and "holds no single token" in output, output[-3000:]
    assert "FAKE-WORD" not in output, output[-3000:]
    assert "Token(<redacted>)" in output and "FAKE-TOKEN-902" not in output, output[-3000:]
