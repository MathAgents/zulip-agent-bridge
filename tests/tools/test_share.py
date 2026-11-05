"""`zab share` (issue #710): a commit of the paths alone, with a nonce, pushed to
`refs/zab/shares/<name>` through the deploy-key alias (a fake ssh into local bare repositories,
as the snapshot tests do), its link opened once; refusals that push nothing; list and revoke.
Offline: the file server is a fake `http`."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from workspace.conftest import FAKE_SSH, run_git
from zab import cli
from zab.tools import share as s
from zab.workspace import git as g

BASE = "https://files.example"
ORIGIN = "git@github.com-alice:MathAgents/alice.git"
CONFIG = f"""
[backends.claude]
kind = "claude"
accounts = {{ main = "account" }}

[[bots]]
name = "claude-alice"
zuliprc = "missing.zuliprc"
backend = "claude"

[render]
refserve_url = "{BASE}/"
"""


class Server:
    """The file server's answers: `answer(method, url)` gives each status (default 200)."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.answer = lambda method, url: 200

    def __call__(self, method, url, timeout):
        self.calls.append((method, url))
        return self.answer(method, url)


@pytest.fixture
def vm(tmp_path, monkeypatch):
    """A checkout of MathAgents/alice whose origin is the deploy-key alias, a bare repository
    behind a fake ssh; zab's config with the file server; the checkout as the cwd."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "github"
    script = tmp_path / "fake-ssh"
    script.write_text(FAKE_SSH)
    script.chmod(0o755)
    monkeypatch.setitem(g._PUSH_ENV, "GIT_SSH_COMMAND", f"{script} {root}")
    monkeypatch.setattr(s, "WAIT_S", 1.0)
    monkeypatch.setattr(s.time, "sleep", lambda _: None)
    bare = root / "MathAgents" / "alice.git"
    bare.parent.mkdir(parents=True)
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = tmp_path / "alice"
    repo.mkdir()
    run_git(repo, "init", "-q")
    (repo / "README.md").write_text("the whole repository\n")
    (repo / "paper").mkdir()
    (repo / "paper" / "main.pdf").write_bytes(b"%PDF-1.7 committed\n")
    (repo / ".gitignore").write_text("*.aux\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "work")
    run_git(repo, "remote", "add", "origin", ORIGIN)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    monkeypatch.chdir(repo)
    server = Server()

    def zab_share(*argv):
        return s.main([*argv, "--config", str(config)], http=server)

    return repo, bare, server, zab_share


def shares(bare: Path) -> dict[str, str]:
    out = run_git(bare, "for-each-ref", "--format=%(refname) %(objectname)", "refs/zab/shares/")
    return dict(line.split() for line in out.splitlines())


def files(repo: Path, sha: str) -> set[str]:
    return set(run_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())


def test_one_file_its_link_and_nothing_else_of_the_repository(vm, capsys):
    repo, bare, server, zab_share = vm
    (repo / "paper" / "main.pdf").write_bytes(b"%PDF-1.7 not committed yet\n")
    before = (run_git(repo, "rev-parse", "HEAD"), run_git(repo, "status", "--porcelain"))
    assert zab_share("paper/main.pdf") == 0
    out = capsys.readouterr().out.splitlines()
    [(ref, sha)] = shares(bare).items()
    name = ref.removeprefix("refs/zab/shares/")
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", name)
    assert out[0] == f"{BASE}/alice/{sha}/paper/main.pdf"
    assert "It opens only paper/main.pdf" in out[1]
    assert f"`zab share --revoke {name}`" in out[-1]
    assert files(bare, sha) == {"paper/main.pdf"}
    assert run_git(bare, "show", f"{sha}:paper/main.pdf") == "%PDF-1.7 not committed yet"
    assert run_git(bare, "rev-list", "--parents", "-n", "1", sha) == sha  # no parent
    message = run_git(bare, "log", "-1", "--format=%B", sha)
    assert re.search(r"^Nonce: [0-9a-f]{64}$", message, re.M) and "Path: paper/main.pdf" in message
    assert run_git(repo, "rev-parse", ref) == sha  # the local ref, for --list
    assert (run_git(repo, "rev-parse", "HEAD"), run_git(repo, "status", "--porcelain")) == before
    assert run_git(bare, "for-each-ref", "refs/heads") == ""  # no branch pushed
    assert server.calls == [("HEAD", out[0])]  # opened once, so the server copies it


def test_the_nonce_makes_the_same_files_another_link(vm, capsys):
    _repo, bare, _, zab_share = vm
    assert zab_share("README.md") == 0 and zab_share("README.md") == 0
    first, second = shares(bare).values()
    assert first != second and files(bare, first) == files(bare, second) == {"README.md"}


def test_a_folder_holds_what_git_sees_less_what_a_snapshot_leaves_out(vm, capsys, monkeypatch):
    repo, bare, _, zab_share = vm
    monkeypatch.setattr(g, "SNAPSHOT_FILE_CAP", 1024)
    report = repo / "report"
    report.mkdir()
    (report / "index.html").write_text("<img src=fig.png>\n")
    (report / "fig.png").write_bytes(b"png")
    (report / "notes.aux").write_text("ignored\n")
    (report / ".env").write_text("TOKEN=decoy\n")
    (report / "huge.bin").write_bytes(b"x" * 2048)
    os.symlink("index.html", report / "alias.html")
    (report / "source.txt").write_text("decoy\n")
    os.link(report / "source.txt", report / "linked.txt")
    (report / "nested").mkdir()
    run_git(report / "nested", "init", "-q")
    assert zab_share("report") == 0
    out = capsys.readouterr().out
    [sha] = shares(bare).values()
    assert out.splitlines()[0] == f"{BASE}/alice/{sha}/report/"
    assert files(bare, sha) == {"report/index.html", "report/fig.png"}
    for path, why in (
        ("report/.env", "the denylist"),
        ("report/huge.bin", "over 0 MB"),
        ("report/alias.html", "a symbolic link"),
        ("report/linked.txt", "more than one hard link"),
        ("report/nested/", "a git repository of its own"),
    ):
        assert f"{path} ({why})" in out, path
    assert "notes.aux" not in out  # ignored, as in a snapshot: not listed either


def test_several_paths_link_the_share_root(vm, capsys):
    repo, bare, _, zab_share = vm
    (repo / "data.csv").write_text("a,b\n")
    assert zab_share("README.md", "data.csv", "paper/") == 0
    [sha] = shares(bare).values()
    assert capsys.readouterr().out.splitlines()[0] == f"{BASE}/alice/{sha}/"
    assert files(bare, sha) == {"README.md", "data.csv", "paper/main.pdf"}


def test_a_path_given_from_a_subfolder_and_an_ignored_file_named(vm, capsys, monkeypatch):
    repo, bare, _, zab_share = vm
    (repo / "paper" / "main.aux").write_text("named, so shared\n")
    monkeypatch.chdir(repo / "paper")
    assert zab_share("main.aux") == 0
    [sha] = shares(bare).values()
    assert files(bare, sha) == {"paper/main.aux"}


@pytest.mark.parametrize(
    "setup,path,why",
    [
        (None, "../outside.txt", "is outside the repository"),
        (None, ".", "is the whole repository"),
        (None, "absent.pdf", "doesn't exist"),
        (None, ".git/config", "is on the denylist"),
        ("env", ".env", "is on the denylist"),
        ("key", "keys/id_ed25519", "is on the denylist"),
        ("link", "alias.pdf", "is a symbolic link"),
        ("linkdir", "via/main.pdf", "is a symbolic link, or inside one"),
        # Codex round 2 on #744: `..` after a link means beside its target, not beside it
        ("dotdot", "link/../README.md", "is a symbolic link, or inside one"),
        ("hard", "copy.txt", "more than one hard link"),
        ("big", "big.bin", "over 0 MB"),
        ("submodule", "sub", "is a git repository of its own"),
        ("ignored", "build", "has nothing that can be shared"),
        (None, "two\nlines", "is not a path"),
    ],
)
def test_a_path_that_cant_be_shared_is_refused_and_nothing_is_pushed(
    vm, capsys, monkeypatch, setup, path, why
):
    repo, bare, server, zab_share = vm
    monkeypatch.setattr(g, "SNAPSHOT_FILE_CAP", 1024)
    (repo.parent / "outside.txt").write_text("not the repository's\n")
    if setup == "env":
        (repo / ".env").write_text("TOKEN=decoy\n")
    elif setup == "key":
        (repo / "keys").mkdir()
        (repo / "keys" / "id_ed25519").write_text("decoy\n")
    elif setup == "link":
        os.symlink("paper/main.pdf", repo / "alias.pdf")
    elif setup == "linkdir":
        os.symlink("paper", repo / "via")
    elif setup == "dotdot":  # link -> public/subdir: link/../README.md is public/README.md
        (repo / "public" / "subdir").mkdir(parents=True)
        (repo / "public" / "README.md").write_text("public\n")
        os.symlink("public/subdir", repo / "link")
    elif setup == "hard":
        os.link(repo / "README.md", repo / "copy.txt")
    elif setup == "big":
        (repo / "big.bin").write_bytes(b"x" * 2048)
    elif setup == "submodule":
        (repo / "sub").mkdir()
        run_git(repo / "sub", "init", "-q")
    elif setup == "ignored":
        (repo / "build").mkdir()
        (repo / "build" / "out.aux").write_text("ignored\n")
    assert zab_share("README.md", path) == 2
    err = capsys.readouterr().err
    assert why in err and "nothing was shared" in err
    assert shares(bare) == {} and not server.calls
    assert run_git(repo, "for-each-ref", "refs/zab/shares/") == ""


def test_too_much_in_all_is_refused(vm, capsys, monkeypatch):
    _repo, bare, _, zab_share = vm
    monkeypatch.setattr(g, "SNAPSHOT_TOTAL_CAP", 30)
    assert zab_share("README.md", "paper/main.pdf") == 2
    assert "a share can hold" in capsys.readouterr().err and shares(bare) == {}


def test_a_share_the_server_doesnt_serve_is_withdrawn_and_no_link_printed(vm, capsys):
    repo, bare, server, zab_share = vm
    server.answer = lambda method, url: 404
    assert zab_share("README.md") == 1
    captured = capsys.readouterr()
    assert BASE not in captured.out and "didn't serve the new share (HTTP 404)" in captured.err
    assert shares(bare) == {} and run_git(repo, "for-each-ref", "refs/zab/shares/") == ""
    assert {m for m, _ in server.calls} == {"HEAD"} and len(server.calls) >= 1


def test_a_failed_push_leaves_no_local_share(vm, capsys):
    repo, _bare, server, zab_share = vm
    run_git(repo, "remote", "set-url", "origin", "git@github.com-alice:MathAgents/absent.git")
    assert zab_share("README.md") == 1
    assert "the push to GitHub failed" in capsys.readouterr().err
    assert run_git(repo, "for-each-ref", "refs/zab/shares/") == "" and not server.calls


def test_list_and_revoke_by_name_and_by_link(vm, capsys):
    repo, bare, server, zab_share = vm
    assert zab_share("README.md") == 0 and zab_share("paper") == 0
    links = [line for line in capsys.readouterr().out.splitlines() if line.startswith(BASE)]
    assert zab_share("--list") == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 2
    by_name = {line.split(" · ")[0]: line for line in listed}
    names = sorted(by_name)
    assert {line.rsplit(" · ", 1)[1] for line in listed} == set(links)  # the links made
    assert " · README.md · " in by_name[names[0]] or " · README.md · " in by_name[names[1]]
    server.calls.clear()
    gone = set()
    server.answer = lambda method, url: 404 if any(sha in url for sha in gone) else 200
    first = shares(bare)[f"refs/zab/shares/{names[0]}"]
    gone.add(first)
    assert zab_share("--revoke", names[0]) == 0
    assert "the link no longer opens" in capsys.readouterr().out
    assert [m for m, _ in server.calls] == ["POST", "HEAD"]
    assert server.calls[0][1] == f"{BASE}/_zab/recheck/alice/{first}"
    second = shares(bare)[f"refs/zab/shares/{names[1]}"]
    assert zab_share("--revoke", f"{BASE}/alice/{second}/paper/") == 0
    capsys.readouterr()
    assert shares(bare) == {} and run_git(repo, "for-each-ref", "refs/zab/shares/") == ""
    assert zab_share("--list") == 0 and "no share links" in capsys.readouterr().out


def test_list_and_revoke_read_the_push_destination(vm, capsys, tmp_path):
    """Codex round 1 on #744: with `origin.url` another repository's (an upstream) and
    `origin.pushurl` the participant's, the shares are pushed to the latter, and `--list` and
    `--revoke` read it, not the fetch URL."""
    repo, bare, _server, zab_share = vm
    upstream = tmp_path / "github" / "upstream" / "alice.git"
    upstream.parent.mkdir(parents=True)
    run_git(tmp_path, "init", "-q", "--bare", str(upstream))
    run_git(repo, "remote", "set-url", "origin", "git@github.com-up:upstream/alice.git")
    run_git(repo, "remote", "set-url", "--push", "origin", ORIGIN)
    assert zab_share("README.md") == 0
    [(_ref, sha)] = shares(bare).items()
    assert shares(upstream) == {}
    capsys.readouterr()
    assert zab_share("--list") == 0
    assert sha in capsys.readouterr().out
    assert zab_share("--revoke", f"{BASE}/alice/{sha}/README.md") == 0
    assert shares(bare) == {}


def test_an_answers_link_or_an_unknown_name_revokes_nothing(vm, capsys):
    repo, bare, _, zab_share = vm
    assert zab_share("README.md") == 0
    capsys.readouterr()
    head = run_git(repo, "rev-parse", "HEAD")
    for what in (f"{BASE}/alice/{head}/README.md", "nothing-here"):
        assert zab_share("--revoke", what) == 2
        assert "an answer's own links aren't share links" in capsys.readouterr().err
    assert len(shares(bare)) == 1


def test_a_server_that_still_serves_after_the_revoke_says_so(vm, capsys):
    _repo, bare, server, zab_share = vm
    assert zab_share("README.md") == 0
    capsys.readouterr()
    [name] = [r.removeprefix("refs/zab/shares/") for r in shares(bare)]
    assert zab_share("--revoke", name) == 0
    said = capsys.readouterr().out
    assert "still served it just now: it stops at the server's next check that reaches" in said
    assert "don't delete the repository before the link answers Not found" in said
    assert "ask the organizers" in said and "65 minutes" not in said  # no deadline (Codex)
    assert shares(bare) == {}
    # a recheck that GitHub couldn't answer (503 to the end): said so (the reviewer on #744)
    assert zab_share("README.md") == 0
    capsys.readouterr()
    [name] = [r.removeprefix("refs/zab/shares/") for r in shares(bare)]
    server.answer = lambda method, url: 503 if method == "POST" else 200
    assert zab_share("--revoke", name) == 0
    assert "couldn't check it with GitHub just now" in capsys.readouterr().out


def test_a_path_inside_a_symbolic_link_is_left_out_whatever_git_lists(vm, tmp_path):
    """The reviewer on #744: every component of a folder's file is checked itself."""
    repo, *_ = vm
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.txt").write_text("not the repository's\n")
    (repo / "report").mkdir()
    os.symlink(tmp_path / "elsewhere", repo / "report" / "sub")
    assert s.candidate(str(repo), "report/sub/x.txt") == ("inside a symbolic link", 0)
    assert s.candidate(str(repo), "README.md")[0] is None


def test_a_name_the_file_server_never_serves_is_refused_or_left_out(vm, capsys):
    """The reviewer on #744: the server's `allowed` refuses a control character or a backslash
    in a component; named, such a file is refused; in a folder, left out and listed. Both
    branches: a real tab, and one backslash."""
    repo, bare, _, zab_share = vm
    backslash, tab = "a\\b.txt", "tab\there.md"
    assert len(backslash) == 7 and "\t" in tab and "\\" not in tab  # one backslash; a tab
    (repo / backslash).write_text("odd\n")
    assert zab_share(backslash) == 2
    assert "a name the file server never serves" in capsys.readouterr().err
    (repo / tab).write_text("odd\n")
    assert zab_share(tab) == 2
    assert "a name the file server never serves" in capsys.readouterr().err
    (repo / "notes").mkdir()
    (repo / "notes" / "ok.md").write_text("fine\n")
    (repo / "notes" / tab).write_text("odd\n")
    (repo / "notes" / backslash).write_text("odd\n")
    assert zab_share("notes") == 0
    out = capsys.readouterr().out
    [sha] = shares(bare).values()
    assert files(bare, sha) == {"notes/ok.md"}
    assert out.count("a name the file server never serves") == 2


@pytest.mark.parametrize("withdrawn", [True, False])
@pytest.mark.parametrize(
    "cut", [TimeoutError(), s.ShareError("the push to GitHub failed: fatal: the remote hung up")]
)
def test_a_push_that_fails_after_landing_is_withdrawn_and_leaves_no_local_share(
    vm, capsys, monkeypatch, withdrawn, cut
):
    """The reviewer and Codex on #744: a push that timed out, or that git reports failed, may
    have landed: the local ref goes, GitHub is asked, and a share there is deleted again, or the
    message says to check and revoke it."""
    repo, bare, server, zab_share = vm
    real, calls = s.push, []

    async def push(ws, top, refspec):
        calls.append(refspec)
        if len(calls) == 1:
            await real(ws, top, refspec)  # it landed, and then the connection was cut
            raise cut
        if not withdrawn:
            raise s.ShareError("the push to GitHub failed: ERROR: unreachable")
        await real(ws, top, refspec)

    monkeypatch.setattr(s, "push", push)
    assert zab_share("README.md") == 1
    err = capsys.readouterr().err
    assert ("didn't finish (TimeoutError)" if isinstance(cut, TimeoutError) else "hung up") in err
    assert run_git(repo, "for-each-ref", "refs/zab/shares/") == "" and not server.calls
    if withdrawn:
        assert "it had reached GitHub, and was withdrawn again" in err and shares(bare) == {}
    else:
        assert "couldn't be withdrawn" in err and "zab share --revoke" in err
        assert len(shares(bare)) == 1


def test_a_push_that_never_landed_says_so_alone(vm, capsys, monkeypatch):
    """GitHub has no such share: nothing to withdraw, and the message is the push's own."""
    repo, bare, _server, zab_share = vm

    async def push(ws, top, refspec):
        raise s.ShareError("the push to GitHub failed: ERROR: Permission denied")

    monkeypatch.setattr(s, "push", push)
    assert zab_share("README.md") == 1
    err = capsys.readouterr().err
    assert err.strip() == "zab share: the push to GitHub failed: ERROR: Permission denied"
    assert shares(bare) == {} and run_git(repo, "for-each-ref", "refs/zab/shares/") == ""


def test_an_origin_of_another_owner_or_no_file_server_shares_nothing(vm, capsys, tmp_path):
    repo, bare, server, zab_share = vm
    run_git(repo, "remote", "set-url", "origin", "git@github.com-x:someone/alice.git")
    assert zab_share("README.md") == 2
    assert "isn't a GitHub repository of MathAgents" in capsys.readouterr().err
    config = tmp_path / "off.toml"
    config.write_text(CONFIG.replace(f'refserve_url = "{BASE}/"', 'refserve_url = ""'))
    assert s.main(["README.md", "--config", str(config)], http=server) == 2
    assert "share links are off" in capsys.readouterr().err
    assert shares(bare) == {} and not server.calls


def test_the_command_line(vm, capsys):
    *_, zab_share = vm
    with pytest.raises(SystemExit) as done:
        cli.main(["share", "--help"])
    assert done.value.code == 0 and "Never share an answer's own link" in capsys.readouterr().out
    for argv in ((), ("--list", "README.md"), ("--revoke", "bad/../name")):
        with pytest.raises(SystemExit) as done:
            zab_share(*argv)
        assert done.value.code == 2
