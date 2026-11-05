"""Share links on the file server (#710): served from a store of their own, which outlives the
repository's mirror (#714), and revoked only when the repository they came from says so. Real git
and HTTP to local bare repositories, GitHub's API faked; offline, no credentials."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import threading
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[3] / "workshop/files"
spec = importlib.util.spec_from_file_location("snapshot_server_shares", DIRECTORY / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
pytestmark = pytest.mark.integration

#: Any organization (#693): none is built in, and the example deployment's is not this one.
ORG = "Example-Org"
LISTING = f"/orgs/{ORG}/repos?type=private&per_page=100&page=1"


def git(root, *args):
    return subprocess.check_output(  # noqa: S603
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.invalid",
            *args,
        ],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def repo(name: str, ident: int, owner: str = ORG) -> tuple[int, dict]:
    return 200, {"id": ident, "name": name, "owner": {"login": owner}, "private": True}


class FakeAPI:
    def __init__(self, answers):
        self.answers, self.calls = dict(answers), []

    def get(self, path):
        self.calls.append(path)
        answer = self.answers.get(path, (404, None))
        if isinstance(answer, BaseException):
            raise answer
        return answer


def path_of(name: str) -> str:
    return f"/repos/{ORG}/{name}"


class Site:
    def __init__(self, tmp_path, *, relative: bool = False):
        # keyed by organization, as on GitHub (review of #770)
        self.remotes, self.tmp = tmp_path / "remotes" / ORG, tmp_path
        work = tmp_path / "work"
        work.mkdir()
        git(work, "init", "-q", "--initial-branch=main")
        self.turns = {}
        for name in ("alice", "bob"):
            remote = self.remotes / f"{name}.git"
            remote.mkdir(parents=True)
            git(remote, "init", "-q", "--bare")
            git(remote, "config", "gc.auto", "0")
            (work / "note.txt").write_text(f"{name}'s whole repository\n")
            (work / "secret-plans.txt").write_text("not shared\n")
            git(work, "add", ".")
            git(work, "commit", "-q", "-m", name)
            self.turns[name] = git(work, "rev-parse", "HEAD")
            git(work, "push", "-q", str(remote), "HEAD:refs/zab/turns/s/1")
        # by name, as the adoption looks a repository up, and by ID, as the checks do
        self.api = FakeAPI(
            {
                path_of("alice"): repo("alice", 1),
                path_of("bob"): repo("bob", 2),
                "/repositories/1": repo("alice", 1),
                "/repositories/2": repo("bob", 2),
            }
        )
        base = Path() if relative else tmp_path  # relative: from the test's working directory
        self.store = server.Store(
            base / "cache",
            shares=base / "state" / "shares",
            org=ORG,
            test_remotes=self.remotes.parent,
        )
        self.store.api = lambda: self.api
        self.store.fetch_interval = 0.01  # a new share in a mirror fetches at once (#473)
        self.http = server.Server(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def share(self, name: str, files: dict[str, str], ref: str = "s1") -> str:
        """A commit holding only `files`, as `zab share` makes one, pushed to its ref."""
        work = self.tmp / f"share-{name}-{ref}"
        work.mkdir()
        git(work, "init", "-q")
        for path, text in files.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_text(text)
        git(work, "add", "-f", ".")
        paths = "".join(f"Path: {p}\n" for p in files)
        git(work, "commit", "-q", "-m", f"zab share\n\n{paths}Nonce: {'ab' * 32}")
        git(work, "push", "-q", str(self.remotes / f"{name}.git"), f"HEAD:refs/zab/shares/{ref}")
        return git(work, "rev-parse", "HEAD")

    def request(self, path, method="GET"):
        with closing(HTTPConnection(*self.http.server_address, timeout=10)) as conn:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, response.read()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


@pytest.fixture
def site(tmp_path):
    site = Site(tmp_path)
    yield site
    site.close()


def test_a_share_is_served_from_its_store_and_lists_only_what_was_shared(site):
    sha = site.share("alice", {"paper/main.pdf": "%PDF-1.7\n", "paper/fig/a.png": "png"})
    assert site.request(f"/alice/{sha}/paper/main.pdf") == (200, b"%PDF-1.7\n")
    status, root = site.request(f"/alice/{sha}/")
    assert status == 200 and b"paper/" in root and b"note.txt" not in root
    assert b"secret-plans" not in root
    status, folder = site.request(f"/alice/{sha}/paper/")
    assert b"main.pdf" in folder and b"fig/" in folder
    store = site.store.shares / "alice.git"
    # kept under its commit, its name on GitHub beside it (Codex round 1 on #740)
    assert git(store, "for-each-ref", "--format=%(refname)") == f"refs/zab/shares/{sha}"
    assert site.store.recorded(store, server.SHARE_NAMES) == {sha: "s1"}
    assert site.store.share_ids(store) == {sha: 1}  # its repository, when it was taken
    # the mirror fetches the shares with the turns, and serves the turns alone
    mirror = site.store.cache / "alice.git"
    assert "refs/zab/shares/s1" in git(mirror, "for-each-ref", "--format=%(refname)")
    assert site.store.prepare("alice", sha, "") == store
    assert site.store.prepare("alice", site.turns["alice"], "") == mirror


def test_a_store_given_relative_paths_still_takes_a_share(tmp_path, monkeypatch):
    """Codex on #740 (at 2e136c86): with a relative `--cache`, as in a hand run, the mirror was
    a relative fetch source for the share store's `git -C <store>`, so no share was ever taken
    (a 404) while the turns were served; the store makes its paths absolute."""
    monkeypatch.chdir(tmp_path)
    site = Site(tmp_path, relative=True)
    try:
        sha = site.share("alice", {"a.txt": "shared\n"})
        assert site.request(f"/alice/{sha}/a.txt") == (200, b"shared\n")
        assert site.request(f"/alice/{site.turns['alice']}/note.txt")[0] == 200
        assert site.store.cache == tmp_path / "cache"
        assert site.store.shares == tmp_path / "state" / "shares"
    finally:
        site.close()


def test_the_denylist_applies_to_a_share_too(site):
    sha = site.share("alice", {"auth.json": "DECOY", "keep.txt": "ok", ".aws/config": "DECOY"})
    assert site.request(f"/alice/{sha}/keep.txt") == (200, b"ok")
    for path in ("auth.json", ".aws/config"):
        assert site.request(f"/alice/{sha}/{path}")[0] == 404
    root = site.request(f"/alice/{sha}/")[1]
    assert b"auth.json" not in root and b".aws" not in root


def test_a_share_outlives_its_repository_and_a_gone_repository_revokes_nothing(site):
    sha = site.share("alice", {"page.html": "<h1>hi</h1>"})
    assert site.request(f"/alice/{sha}/page.html")[0] == 200
    shutil.rmtree(site.remotes / "alice.git")  # deleted on "GitHub"
    site.api.answers[path_of("alice")] = (404, None)
    site.api.answers["/repositories/1"] = (404, None)
    site.api.answers[LISTING] = (200, [{"name": "bob", "private": True}])
    assert site.store.check(site.api)["removed"] == ["alice"]  # #714: the mirror goes
    assert not (site.store.cache / "alice.git").exists()
    assert site.store.check_all_shares(site.api) == {}
    assert site.request(f"/alice/{sha}/page.html") == (200, b"<h1>hi</h1>")
    assert site.request(f"/alice/{site.turns['alice']}/note.txt")[0] == 404  # the turns are gone


def test_a_revoked_share_stops_and_is_never_taken_again(site, caplog):
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    git(site.remotes / "alice.git", "update-ref", "-d", "refs/zab/shares/s1")  # `--revoke`
    assert site.store.check_all_shares(site.api) == {"alice": [sha]}
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404
    store = site.store.shares / "alice.git"
    assert site.store.revoked(store) == {sha}
    assert sha not in git(store, "rev-list", "--all", "--objects")  # pruned
    assert "share revoked repo=alice: its ref is gone from GitHub" in caplog.text
    assert sha not in caplog.text  # a share's commit is its link's capability
    # its ref back on GitHub (a stale copy pushed again): still revoked
    work = next(site.tmp.glob("share-alice-s1"))
    git(work, "push", "-q", str(site.remotes / "alice.git"), "HEAD:refs/zab/shares/s1")
    site.store.last_fetch.clear()
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404


@pytest.mark.parametrize(
    "answer",
    [
        (404, None),  # its repository gone, whatever now has the name
        repo("alice", 1, owner="someone"),  # transferred: another owner's
        (401, None),  # a bad token
        (503, None),  # an outage
        TimeoutError(),
        (200, {"id": 1, "name": "alice"}),  # no owner: no answer
        repo("alice", 9),  # another ID: no answer about this one
    ],
)
def test_only_the_repository_it_came_from_revokes_a_share(site, answer):
    """The reviewer on #740: a share's repository is looked up by the ID recorded when it was
    taken (`/repositories/<id>`), never by the name, which a new repository may have taken."""
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    git(site.remotes / "alice.git", "update-ref", "-d", "refs/zab/shares/s1")
    site.api.answers["/repositories/1"] = answer
    site.api.answers[path_of("alice")] = repo("alice", 7)  # a new repository with the name
    site.api.calls.clear()
    assert site.store.check_all_shares(site.api) == {}
    assert site.api.calls == ["/repositories/1"]  # by its ID, never by the name
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200


def test_a_renamed_repository_revokes_by_its_name_now(site):
    """Looked up by its ID, the repository is followed through its rename."""
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    shutil.move(site.remotes / "alice.git", site.remotes / "alice-renamed.git")
    site.api.answers["/repositories/1"] = repo("alice-renamed", 1)
    assert site.store.check_all_shares(site.api) == {}  # its ref is still there
    git(site.remotes / "alice-renamed.git", "update-ref", "-d", "refs/zab/shares/s1")
    assert site.store.check_all_shares(site.api) == {"alice": [sha]}


def test_a_listing_that_fails_revokes_nothing(site, monkeypatch):
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    git(site.remotes / "alice.git", "update-ref", "-d", "refs/zab/shares/s1")

    def unreachable(name):
        raise server.GitError(128, b"fatal: unable to access")

    monkeypatch.setattr(site.store, "remote_shares", unreachable)
    assert site.store.check_all_shares(site.api) == {}
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200


@pytest.mark.parametrize(
    "lookup", [None, (503, None), (429, None), TimeoutError(), (200, {"id": 1, "name": "alice"})]
)
def test_a_share_is_not_taken_without_its_repositorys_id(site, lookup):
    """The reviewer on #740: a share taken without the ID could never be revoked. Without a
    token, or an answer, its request is a 503, which `zab share`'s first opening retries, and
    nothing is kept; once GitHub answers, it is taken with its ID and names."""
    if lookup is None:
        site.store.api = lambda: None  # no token
    else:
        site.api.answers[path_of("alice")] = lookup
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 503
    store = site.store.shares / "alice.git"
    assert sha not in site.store.share_refs(store) and site.store.share_ids(store) == {}
    site.store.api = lambda: site.api
    site.api.answers[path_of("alice")] = repo("alice", 1)
    site.store.last_fetch.clear()
    assert site.request(f"/alice/{sha}/a.txt") == (200, b"shared")
    assert site.store.share_ids(store) == {sha: 1}
    assert site.store.recorded(store, server.SHARE_NAMES) == {sha: "s1"}


def test_the_recheck_revokes_at_once_and_is_limited(site, monkeypatch):
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    url = f"/_zab/recheck/alice/{sha}"
    assert site.request(url, "POST") == (200, b"kept\n")
    assert site.request(url, "POST")[0] == 503  # once every RECHECK_EVERY seconds each
    site.store.rechecked.clear()
    git(site.remotes / "alice.git", "update-ref", "-d", "refs/zab/shares/s1")
    assert site.request(url, "POST") == (200, b"revoked\n")
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404
    site.store.rechecked.clear()
    calls = len(site.api.calls)
    assert site.request(url, "POST")[0] == 404  # no share now: no GitHub call
    assert site.request(f"/_zab/recheck/alice/{'a' * 40}", "POST")[0] == 404
    assert site.request(f"/_zab/recheck/alice/{site.turns['alice']}", "POST")[0] == 404
    assert len(site.api.calls) == calls
    assert site.request(url)[0] == 404  # GET: no such route
    assert site.request(f"/alice/{sha}/x", "POST")[0] == 404


def test_the_recheck_has_an_hourly_budget_and_needs_a_token(site, monkeypatch):
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    url = f"/_zab/recheck/alice/{sha}"
    monkeypatch.setattr(server, "RECHECKS_PER_HOUR", 1)
    assert site.request(url, "POST")[0] == 200
    site.store.rechecked.clear()
    assert site.request(url, "POST")[0] == 503
    site.store.recheck_times.clear()
    site.store.rechecked.clear()
    site.store.api = lambda: None
    assert site.request(url, "POST")[0] == 503


@pytest.mark.parametrize("answer", [(503, None), (401, None), TimeoutError()])
def test_the_recheck_never_says_kept_when_github_doesnt_say(site, answer, monkeypatch):
    """The reviewer on #740: a recheck that can't decide is a 503 (try later), never "kept",
    and so is one whose listing of the refs fails."""
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    url = f"/_zab/recheck/alice/{sha}"
    site.api.answers["/repositories/1"] = answer
    assert site.request(url, "POST")[0] == 503
    site.api.answers["/repositories/1"] = repo("alice", 1)
    site.store.rechecked.clear()

    def unreachable(name):
        raise server.GitError(128, b"fatal: unable to access")

    monkeypatch.setattr(site.store, "remote_shares", unreachable)
    assert site.request(url, "POST")[0] == 503
    monkeypatch.undo()
    site.store.rechecked.clear()
    site.api.answers["/repositories/1"] = (404, None)  # its repository gone: kept, for sure
    assert site.request(url, "POST") == (200, b"kept\n")


def test_a_recheck_whose_revocation_waits_too_long_is_a_503(site):
    """Codex on cf974162: an adoption may hold the repository's gate through a fetch and a
    lookup; a revocation that can't get it raises, so the recheck answers 503 (retry), never
    `kept`, and the hourly check leaves it for the next."""
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    git(site.remotes / "alice.git", "update-ref", "-d", "refs/zab/shares/s1")
    site.store.store_wait = 0.2
    gate = site.store.gates.setdefault("alice", server.FetchGate())
    assert gate.lock.acquire(timeout=1)
    try:
        assert site.request(f"/_zab/recheck/alice/{sha}", "POST")[0] == 503
        assert site.store.check_all_shares(site.api) == {}
    finally:
        gate.lock.release()
    site.store.rechecked.clear()
    assert site.request(f"/_zab/recheck/alice/{sha}", "POST") == (200, b"revoked\n")


def test_a_revoked_share_is_never_served_even_with_a_ref_left(site):
    """The reviewer on #740: `prepare` and `found` read the revoked list too, so a share
    whose ref a cut-short revocation left behind is not served."""
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    store = site.store.shares / "alice.git"
    with (store / server.REVOKED).open("a") as out:
        out.write(sha + "\n")
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404


def test_a_ref_that_only_ends_like_a_share_is_none(site):
    """The reviewer on #740: `ls-remote`'s pattern matches a ref's tail."""
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    remote = site.remotes / "alice.git"
    git(remote, "update-ref", "refs/heads/x/refs/zab/shares/s1", sha)
    git(remote, "update-ref", "-d", "refs/zab/shares/s1")
    assert ("refs/heads/x/refs/zab/shares/s1", sha) not in site.store.remote_shares("alice")
    assert site.store.check_all_shares(site.api) == {"alice": [sha]}


def test_the_deployers_list_and_unshare(site, capsys, monkeypatch):
    logged = []
    monkeypatch.setattr(server, "journal", logged.append)
    one = site.share("alice", {"paper/main.pdf": "%PDF"}, ref="one")
    two = site.share("alice", {"site/index.html": "<p>"}, ref="two")
    three = site.share("alice", {"x.txt": "x"}, ref="three")
    for sha in (one, two, three):
        assert site.request(f"/alice/{sha}/")[0] == 200
    args = ["--cache", str(site.store.cache), "--shares", str(site.store.shares)]
    assert server.main([*args, "shares", "alice"]) == 0
    lines = sorted(capsys.readouterr().out.splitlines())
    assert len(lines) == 3
    fields = {line.split("\t")[1]: line.split("\t") for line in lines}
    assert fields["one"][0] == "alice" and fields["one"][3] == f"/alice/{one}/"
    assert fields["one"][4] == "paper/main.pdf" and fields["one"][2].startswith("20")
    assert server.main([*args, "shares"]) == 0 and len(capsys.readouterr().out.splitlines()) == 3
    assert server.main([*args, "unshare", "alice", "one"]) == 0
    assert capsys.readouterr().out == f"revoked one ({one[:12]}) of alice\n"
    assert logged == ["unshare: revoked one of alice"]  # the journal, never the commit
    assert server.main([*args, "unshare", "alice", f"https://files.example/alice/{two}/x"]) == 0
    assert server.main([*args, "unshare", "alice", "nothing"]) == 1
    capsys.readouterr()
    assert site.request(f"/alice/{one}/")[0] == 404 and site.request(f"/alice/{two}/")[0] == 404
    assert site.request(f"/alice/{three}/")[0] == 200
    assert server.main([*args, "unshare", "alice", "all"]) == 0
    assert site.request(f"/alice/{three}/")[0] == 404
    assert server.main([*args, "unshare", "../x", "all"]) == 2


def test_a_new_repository_reusing_a_share_name_takes_nothing_from_one_kept(site):
    """Codex round 1 on #740: the store keeps each share under its commit, so a repository
    made again under the name, whose share has the same name, adds a share beside the kept
    one; and the new repository, without that commit, revokes nothing of the old one's."""
    old = site.share("alice", {"a.txt": "the deleted repository's"})
    assert site.request(f"/alice/{old}/a.txt")[0] == 200
    shutil.rmtree(site.remotes / "alice.git")
    git(site.remotes, "init", "-q", "--bare", "alice.git")
    site.api.answers[path_of("alice")] = repo("alice", 7)
    site.api.answers["/repositories/1"] = (404, None)  # the deleted one
    site.api.answers["/repositories/7"] = repo("alice", 7)
    new = site.share("alice", {"b.txt": "the new repository's"}, ref="s1-again")
    work = site.tmp / "share-alice-s1-again"
    git(work, "push", "-q", "-f", str(site.remotes / "alice.git"), "HEAD:refs/zab/shares/s1")
    site.store.last_fetch.clear()
    assert site.request(f"/alice/{new}/b.txt") == (200, b"the new repository's")
    assert site.request(f"/alice/{old}/a.txt") == (200, b"the deleted repository's")
    store = site.store.shares / "alice.git"
    assert site.store.share_ids(store) == {old: 1, new: 7}
    assert site.store.check_all_shares(site.api) == {}  # the old one's repository is gone
    assert site.request(f"/alice/{old}/a.txt")[0] == 200


def test_ids_recorded_at_once_are_all_kept(site, monkeypatch):
    """Codex round 1 on #740: the adoption and the check may record at once. Each merges under
    one lock, so neither loses the other's."""
    store = site.store.shares / "alice.git"
    store.mkdir(parents=True)
    original = site.store.recorded

    def slow(share, kind):
        found = original(share, kind)
        threading.Event().wait(0.01)  # a read, then the other thread's turn
        return found

    monkeypatch.setattr(site.store, "recorded", slow)
    shas = [f"{n:040x}" for n in range(16)]
    threads = [threading.Thread(target=site.store.record_ids, args=(store, {s: 1})) for s in shas]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert site.store.share_ids(store) == dict.fromkeys(shas, 1)
    assert sorted(p.name for p in store.iterdir()) == [server.SHARE_IDS]  # no temporary left


def test_a_share_is_taken_only_after_a_fetch_that_succeeded(site):
    """Codex round 2 on #740: a share the mirror fetched with a turn, then revoked on GitHub
    before its first request, isn't taken from that stale mirror when the request's fetch
    fails, or is deferred by the negative cache."""
    sha = site.share("alice", {"a.txt": "shared"})
    work = site.tmp / "work"
    (work / "note.txt").write_text("a new turn\n")
    git(work, "commit", "-q", "-am", "turn 2")
    turn = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", str(site.remotes / "alice.git"), "HEAD:refs/zab/turns/s/2")
    assert site.request(f"/alice/{turn}/note.txt")[0] == 200  # the fetch takes the share too
    mirror = site.store.cache / "alice.git"
    assert "refs/zab/shares/s1" in git(mirror, "for-each-ref", "--format=%(refname)")
    git(site.remotes / "alice.git", "update-ref", "-d", "refs/zab/shares/s1")  # revoked
    shutil.move(site.remotes / "alice.git", site.remotes / "away.git")  # GitHub fails
    site.store.last_fetch.clear()
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404  # the fetch failed: nothing taken
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404  # negatively cached: nothing taken
    assert not (site.store.shares / "alice.git").exists()
    shutil.move(site.remotes / "away.git", site.remotes / "alice.git")
    site.store.last_fetch.clear()
    site.store.failed_fetch.clear()
    assert site.request(f"/alice/{sha}/a.txt")[0] == 404  # fetched: the ref is gone
    assert "refs/zab/shares/s1" not in git(mirror, "for-each-ref", "--format=%(refname)")


def test_adoption_and_revocation_wait_for_the_store_lock_of_another_process(
    site, capsys, monkeypatch
):
    """Codex round 3 on #740: a deployer's `unshare` runs in a process of its own, whose gc
    could prune what an adoption fetches meanwhile. Both hold the store's `flock`: an adoption
    that can't get it answers 503 (retry), and takes nothing; once it's free, the share is
    taken. An `unshare` waits for it the same way."""
    import fcntl
    import os

    first = site.share("alice", {"a.txt": "one"})
    assert site.request(f"/alice/{first}/a.txt")[0] == 200
    second = site.share("alice", {"b.txt": "two"}, ref="s2")
    store = site.store.shares / "alice.git"
    site.store.store_wait = 0.2
    held = os.open(store / server.STORE_LOCK, os.O_RDWR)  # another process's, as `unshare`'s
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        site.store.last_fetch.clear()
        assert site.request(f"/alice/{second}/b.txt")[0] == 503
        assert second not in site.store.share_refs(store)
        with pytest.raises(server.FetchBusy):
            site.store.unshare("alice", first, "revoked by hand")
        assert first in site.store.share_refs(store)
        # the deployer's command says what happened (the reviewer on #740), and exits 1
        args = ["--cache", str(site.store.cache), "--shares", str(site.store.shares)]
        monkeypatch.setattr(server.Store, "store_wait", 0.2)
        assert server.main([*args, "unshare", "alice", first]) == 1
        assert "not revoked: s1 (" in capsys.readouterr().err
        assert first in site.store.share_refs(store)
    finally:
        os.close(held)
    site.store.last_fetch.clear()
    assert site.request(f"/alice/{second}/b.txt") == (200, b"two")
    assert site.store.unshare("alice", first, "revoked by hand")


def test_purge_leaves_the_shares(site, capsys):
    sha = site.share("alice", {"a.txt": "shared"})
    assert site.request(f"/alice/{sha}/a.txt")[0] == 200
    args = ["--cache", str(site.store.cache), "--shares", str(site.store.shares)]
    assert server.main([*args, "purge", "alice"]) == 0
    assert site.request(f"/alice/{sha}/a.txt") == (200, b"shared")
