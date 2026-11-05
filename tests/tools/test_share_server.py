"""`zab share` (#744) against the real file server (#740), offline: each PR's own tests fake the
other side (the reviewer on #710). Local bare remotes through the snapshot tests' fake ssh, the
server's `test_remotes` on the same bare repositories, GitHub's API faked."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import httpx
import pytest

from workspace.conftest import FAKE_SSH, run_git
from zab.tools import share as s
from zab.workspace import git as g

pytestmark = pytest.mark.integration
#: Not the example deployment's organization (#693): the server's and the config's, as rolled out.
ORG = "Example-Org"

FILES = Path(__file__).resolve().parents[2] / "workshop/files"
spec = importlib.util.spec_from_file_location("share_combined_server", FILES / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)

CONFIG = """
[backends.claude]
kind = "claude"
accounts = { main = "account" }

[[bots]]
name = "claude-alice"
zuliprc = "missing.zuliprc"
backend = "claude"

[render]
refserve_url = "BASE/"
refserve_owner = "Example-Org"
"""


class FakeAPI:
    def __init__(self, answers):
        self.answers = answers

    def get(self, path):
        return self.answers.get(path, (404, None))


def found(name, ident):
    return 200, {"id": ident, "name": name, "owner": {"login": ORG}, "private": True}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "github"
    script = tmp_path / "fake-ssh"
    script.write_text(FAKE_SSH)
    script.chmod(0o755)
    monkeypatch.setitem(g._PUSH_ENV, "GIT_SSH_COMMAND", f"{script} {root}")
    bare = root / ORG / "alice.git"
    bare.parent.mkdir(parents=True)
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = tmp_path / "alice"
    repo.mkdir()
    run_git(repo, "init", "-q")
    (repo / "README.md").write_text("the whole repository\n")
    (repo / "secret.md").write_text("PRIVATE\n")
    (repo / "paper").mkdir()
    (repo / "paper" / "main.pdf").write_bytes(b"%PDF-1.7\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "work")
    run_git(repo, "remote", "add", "origin", f"git@github.com-alice:{ORG}/alice.git")
    store = server.Store(
        tmp_path / "cache", shares=tmp_path / "state" / "shares", org=ORG, test_remotes=root
    )
    api = FakeAPI({f"/repos/{ORG}/alice": found("alice", 1), "/repositories/1": found("alice", 1)})
    store.api = lambda: api
    store.fetch_interval = 0.01
    http = server.Server(("127.0.0.1", 0), store)
    thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    base = "http://{}:{}".format(*http.server_address)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.replace("BASE", base))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(s, "WAIT_S", 10.0)

    def zab_share(*argv):
        return s.main([*argv, "--config", str(config)])

    yield repo, store, base, zab_share
    http.shutdown()
    http.server_close()
    thread.join()


def get(url):
    response = httpx.get(url, follow_redirects=False, timeout=10)
    return response.status_code, response.content


def test_a_share_opens_lists_and_is_revoked_end_to_end(rig, capsys):
    _repo, store, base, zab_share = rig
    assert zab_share("paper/main.pdf") == 0, capsys.readouterr().err
    out = capsys.readouterr().out.splitlines()
    link = out[0]
    assert link.startswith(base + "/alice/") and link.endswith("/paper/main.pdf")
    assert get(link) == (200, b"%PDF-1.7\n")
    root = link[: -len("paper/main.pdf")]
    status, body = get(root)
    assert status == 200 and b"paper/" in body and b"README" not in body and b"secret" not in body
    sha = root.rstrip("/").rsplit("/", 1)[1]
    shares = store.shares / "alice.git"
    assert sha in store.share_refs(shares) and store.share_ids(shares) == {sha: 1}
    assert zab_share("--list") == 0 and link in capsys.readouterr().out
    name = out[-1].split("--revoke ")[1].split("`")[0]
    assert zab_share("--revoke", name) == 0
    assert "the link no longer opens" in capsys.readouterr().out
    assert get(link)[0] == 404 and get(root)[0] == 404
    assert sha in store.revoked(shares)


def test_a_folder_shares_an_html_page_with_its_images(rig, capsys):
    repo, _, _, zab_share = rig
    (repo / "site").mkdir()
    (repo / "site" / "index.html").write_text("<img src=a.png>")
    (repo / "site" / "a.png").write_bytes(b"png")
    assert zab_share("site") == 0, capsys.readouterr().err
    link = capsys.readouterr().out.splitlines()[0]
    assert link.endswith("/site/")
    status, body = get(link)
    assert status == 200 and b"index.html" in body and b"a.png" in body
    assert get(link + "a.png") == (200, b"png")
    assert get(link + "../README.md")[0] == 404


def test_the_checkout_is_untouched_and_the_share_holds_the_working_tree(rig, capsys):
    repo, _, _, zab_share = rig
    (repo / "README.md").write_text("staged change\n")
    run_git(repo, "add", "README.md")
    (repo / "README.md").write_text("staged change\nplus unstaged\n")
    (repo / "new.txt").write_text("untracked\n")
    index = (repo / ".git" / "index").read_bytes()
    before = [run_git(repo, *a) for a in (("status", "--porcelain"), ("diff", "--cached"))]
    assert zab_share("README.md", "new.txt") == 0, capsys.readouterr().err
    link = capsys.readouterr().out.splitlines()[0]
    assert [run_git(repo, *a) for a in (("status", "--porcelain"), ("diff", "--cached"))] == before
    assert (repo / ".git" / "index").read_bytes() == index
    assert get(link + "README.md") == (200, b"staged change\nplus unstaged\n")


def test_without_its_repositorys_id_no_link_is_made(rig, capsys, monkeypatch):
    """#740's review: the server takes no share without its ID; `zab share`'s opening retries,
    then withdraws the share and makes no link."""
    repo, store, base, zab_share = rig
    store.api = lambda: None
    monkeypatch.setattr(s, "WAIT_S", 2.0)
    assert zab_share("README.md") == 1
    captured = capsys.readouterr()
    assert base not in captured.out and "didn't serve the new share (HTTP 503)" in captured.err
    assert run_git(repo, "for-each-ref", "refs/zab/shares/") == ""
