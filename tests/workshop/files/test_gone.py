"""A deleted or transferred repository's mirror goes, and only then (#714): real git and HTTP to
local bare repositories, with GitHub's API faked; offline, no credentials."""

from __future__ import annotations

import http.client
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[3] / "workshop/files"
spec = importlib.util.spec_from_file_location("snapshot_server_gone", DIRECTORY / "server.py")
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


def listing(*names: str) -> tuple[int, list]:
    return 200, [{"name": n, "private": True} for n in names]


class FakeAPI:
    """GitHub's answers by path: a (status, body) pair, an exception to raise, or a list of
    those, one per call; anything not given answers 404."""

    def __init__(self, answers):
        self.answers, self.calls = dict(answers), []

    def get(self, path):
        self.calls.append(path)
        answer = self.answers.get(path, (404, None))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def path_of(name: str) -> str:
    return f"/repos/{ORG}/{name}"


@pytest.fixture
def site(tmp_path):
    """Mirrors of `alice` and `bob`, each fetched for its turn snapshot."""
    # keyed by organization, as on GitHub: a server of another one finds nothing (review of #770)
    remotes, work, shas = tmp_path / "remotes" / ORG, tmp_path / "work", {}
    work.mkdir()
    git(work, "init", "-q", "--initial-branch=main")
    for name in ("alice", "bob"):
        remote = remotes / f"{name}.git"
        remote.mkdir(parents=True)
        git(remote, "init", "-q", "--bare")
        for key, value in {"receive.autogc": "false", "gc.auto": "0"}.items():
            git(remote, "config", key, value)
        (work / "note.txt").write_text(f"{name}\n")
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", name)
        shas[name] = git(work, "rev-parse", "HEAD")
        git(work, "push", "-q", str(remote), f"HEAD:refs/zab/turns/s/{len(shas)}")
    store = server.Store(tmp_path / "cache", org=ORG, test_remotes=remotes.parent)
    http = server.Server(("127.0.0.1", 0), store)
    thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
    thread.start()

    def get(path):
        with closing(HTTPConnection(*http.server_address, timeout=10)) as conn:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read()

    for name, sha in shas.items():
        assert get(f"/{name}/{sha}/note.txt") == (200, f"{name}\n".encode())
    yield store, get, shas, remotes
    http.shutdown()
    http.server_close()
    thread.join()


def test_a_gone_repository_s_mirror_goes_and_its_links_answer_404(site, caplog):
    store, get, shas, remotes = site
    shutil.rmtree(remotes / "alice.git")  # deleted on "GitHub": a fetch fails from now on
    api = FakeAPI({path_of("bob"): repo("bob", 2), LISTING: listing("bob")})
    found = store.check(api)
    assert found["removed"] == ["alice"] and found[server.MISSING] == ["alice"]
    assert store.mirrors() == ["bob"]
    assert get(f"/alice/{shas['alice']}/note.txt")[0] == 404
    assert get(f"/bob/{shas['bob']}/note.txt")[0] == 200
    # not found, then the token reads another repository, then not found again
    assert api.calls == [
        path_of("alice"),
        path_of("bob"),
        LISTING,
        path_of("bob"),
        path_of("alice"),
    ]
    assert "gone check: 2 mirrors, 1 exist, 1 not found, 0 unknown; removed alice" in caplog.text
    assert "mirror removed repo=alice: deleted or transferred" in caplog.text
    assert shas["alice"] not in caplog.text


NO_ANSWER = [
    TimeoutError("timed out"),
    ConnectionResetError(),
    http.client.IncompleteRead(b""),
    ValueError("not JSON"),
]


@pytest.mark.parametrize(
    "answers",
    [
        # transient failures: the repository itself gives no answer, or a server error
        *({path_of("alice"): exc} for exc in NO_ANSWER),
        *({path_of("alice"): (status, None)} for status in (500, 502, 503, 429)),
        # a GitHub outage: nothing answers, or every lookup fails
        {path_of("alice"): (503, None), path_of("bob"): (503, None), LISTING: (503, None)},
        {path_of("alice"): TimeoutError(), path_of("bob"): TimeoutError(), LISTING: OSError()},
        # an expired or revoked token (401), or one GitHub refuses (403)
        {path_of("alice"): (401, None), path_of("bob"): (401, None), LISTING: (401, None)},
        {path_of("alice"): (403, None), path_of("bob"): (403, None), LISTING: (403, None)},
        # a 404, but the token then reads nothing private: an approval lapsed, another owner's
        {path_of("bob"): (404, None), LISTING: listing()},
        {path_of("bob"): repo("bob", 2), LISTING: (401, None)},
        {path_of("bob"): repo("bob", 2), LISTING: TimeoutError()},
        {path_of("bob"): [repo("bob", 2), (503, None)], LISTING: listing("bob")},
        # a 404 that the second look doesn't confirm, and a repository its listing shows
        {
            path_of("alice"): [(404, None), repo("alice", 1)],
            path_of("bob"): repo("bob", 2),
            LISTING: listing("bob"),
        },
        {
            path_of("alice"): (404, None),
            path_of("bob"): repo("bob", 2),
            LISTING: listing("alice", "bob"),
        },
        # moved within the organization: renamed, so not gone
        {path_of("alice"): repo("alice-renamed", 1), path_of("bob"): repo("bob", 2)},
    ],
)
def test_failures_outages_and_bad_tokens_never_remove_a_mirror(site, answers):
    store, get, shas, _ = site
    found = store.check(FakeAPI(answers))
    assert found["removed"] == [] and store.mirrors() == ["alice", "bob"]
    assert get(f"/alice/{shas['alice']}/note.txt")[0] == 200


@pytest.mark.parametrize(
    "answer",
    [
        repo("alice", 1, owner="alice-the-participant"),  # transferred, readable at its new home
        (404, None),  # transferred out of the token's reach (GitHubAPI followed its redirect)
    ],
)
def test_a_transferred_repository_is_gone(site, answer):
    store, *_ = site
    api = FakeAPI(
        {path_of("alice"): answer, path_of("bob"): repo("bob", 2), LISTING: listing("bob")}
    )
    assert store.check(api)["removed"] == ["alice"]


def test_a_new_repository_under_the_name_is_another_repository(site, caplog):
    """Deleted, then made again under its name (a `!provision repos` after the deletion): the
    snapshots mirrored are the deleted repository's."""
    store, *_ = site
    api = FakeAPI({path_of("alice"): repo("alice", 1), path_of("bob"): repo("bob", 2)})
    assert store.check(api)["removed"] == []
    assert (store.cache / "alice.git" / server.ID_FILE).read_text() == "1\n"
    api.answers[path_of("alice")] = repo("alice", 7)
    assert store.check(api)["removed"] == ["alice"]
    assert store.mirrors() == ["bob"]
    assert "mirror removed repo=alice: another repository has its name" in caplog.text


def replace_remote(remotes, work, name):
    """`name` deleted on "GitHub" and made again: the new repository has a turn of its own."""
    shutil.rmtree(remotes / f"{name}.git")
    git(remotes, "init", "-q", "--bare", f"{name}.git")
    (work / "note.txt").write_text("the new repository\n")
    git(work, "commit", "-q", "-am", "new")
    git(work, "push", "-q", str(remotes / f"{name}.git"), "HEAD:refs/zab/turns/n/1")
    return git(work, "rev-parse", "HEAD")


def test_a_repository_replaced_before_its_mirrors_first_check_keeps_no_old_snapshot(site):
    """Codex round 1 on #733: deleted and made again before the first check, which would have
    recorded the new one's ID against the old one's snapshots. The first check fetches the
    mirror first, which prunes it to the new repository's turns, and only then records."""
    store, get, shas, remotes = site
    new = replace_remote(remotes, remotes.parents[1] / "work", "alice")
    store.last_fetch.clear()
    api = FakeAPI({path_of("alice"): repo("alice", 7), path_of("bob"): repo("bob", 2)})
    assert store.check(api)["removed"] == []
    assert (store.cache / "alice.git" / server.ID_FILE).read_text() == "7\n"
    assert get(f"/alice/{shas['alice']}/note.txt")[0] == 404  # the deleted one's: gone
    assert get(f"/alice/{new}/note.txt") == (200, b"the new repository\n")
    assert get(f"/bob/{shas['bob']}/note.txt")[0] == 200


def test_an_id_is_recorded_only_after_a_fetch_that_succeeds(site, monkeypatch):
    store, get, shas, remotes = site
    shutil.move(remotes / "alice.git", remotes / "away.git")  # "GitHub" can't serve it now
    api = FakeAPI({path_of("alice"): repo("alice", 1), path_of("bob"): repo("bob", 2)})
    assert store.check(api)["removed"] == []
    assert not (store.cache / "alice.git" / server.ID_FILE).exists()
    assert get(f"/alice/{shas['alice']}/note.txt")[0] == 200  # kept, as any failed fetch keeps it
    shutil.move(remotes / "away.git", remotes / "alice.git")
    store.last_fetch.clear()
    store.failed_fetch.clear()
    store.check(api)
    assert (store.cache / "alice.git" / server.ID_FILE).read_text() == "1\n"
    fetches = []
    original = store.git
    monkeypatch.setattr(store, "git", lambda r, *a: fetches.append(a[0]) or original(r, *a))
    store.check(api)  # recorded: no fetch any more
    assert "fetch" not in fetches


def test_a_renamed_repository_is_fetched_under_its_name_now(site):
    """Its old name redirects, which the server's git doesn't follow: the check fetches from
    the name GitHub's API gave, and the old links keep opening from the mirror."""
    store, get, shas, remotes = site
    shutil.move(remotes / "alice.git", remotes / "alice-renamed.git")
    api = FakeAPI({path_of("alice"): repo("alice-renamed", 1), path_of("bob"): repo("bob", 2)})
    assert store.check(api)["removed"] == []
    assert (store.cache / "alice.git" / server.ID_FILE).read_text() == "1\n"
    assert get(f"/alice/{shas['alice']}/note.txt")[0] == 200


def test_a_repository_replaced_then_renamed_keeps_no_old_snapshot(site):
    """Codex round 2 on #733: deleted, made again under the name, then renamed, all before the
    mirror's first check. The old name redirects to the new repository, which the check fetches
    from, so the old one's snapshots are pruned before the ID is recorded."""
    store, get, shas, remotes = site
    new = replace_remote(remotes, remotes.parents[1] / "work", "alice")
    shutil.move(remotes / "alice.git", remotes / "alice-next.git")
    store.last_fetch.clear()
    api = FakeAPI({path_of("alice"): repo("alice-next", 9), path_of("bob"): repo("bob", 2)})
    assert store.check(api)["removed"] == []
    assert (store.cache / "alice.git" / server.ID_FILE).read_text() == "9\n"
    assert get(f"/alice/{shas['alice']}/note.txt")[0] == 404
    assert get(f"/alice/{new}/note.txt") == (200, b"the new repository\n")


def test_too_many_at_once_removes_none(site, caplog):
    """A token narrowed to some repositories would make the others look deleted."""
    store, *_ = site
    for i in range(3):
        shutil.copytree(store.cache / "alice.git", store.cache / f"carol{i}.git")
    api = FakeAPI({path_of("bob"): repo("bob", 2), LISTING: listing("bob")})
    found = store.check(api)
    assert len(found[server.MISSING]) == 4 and found["removed"] == []
    assert len(store.mirrors()) == 5
    # each named, so that a deployer can check it and purge it (reviewer on #733)
    assert (
        "4 gone at once: none removed, kept alice, carol0, carol1, carol2 (check the token, "
        "then purge by hand those deleted)"
    ) in caplog.text


def test_a_token_that_reads_nothing_names_what_it_keeps(site, caplog):
    store, *_ = site
    store.check(FakeAPI({LISTING: listing()}))
    assert (
        "the token reads no other private repository: none removed, kept alice, bob"
    ) in caplog.text


@pytest.mark.parametrize("owner", [None, {}, {"login": ""}, {"login": 7}, ORG])
def test_a_200_without_its_owner_is_no_answer(site, owner):
    """Reviewer on #733: a body that doesn't say whose repository it is proves nothing."""
    store, *_ = site
    body = {"id": 1, "name": "alice", "owner": owner}
    if owner is None:
        del body["owner"]
    api = FakeAPI({path_of("alice"): (200, body), path_of("bob"): repo("bob", 2)})
    found = store.check(api)
    assert found[server.UNKNOWN] == ["alice"] and found["removed"] == []


def test_the_listing_is_read_page_by_page(site):
    """Reviewer on #733: after a few workshops the organization has over 100 private
    repositories; the one gone is on the second page's listing or not, never guessed."""
    store, *_ = site
    full = [{"name": f"r{i:03d}", "private": True} for i in range(100)]
    page2 = f"/orgs/{ORG}/repos?type=private&per_page=100&page=2"
    answers = {
        path_of("bob"): repo("bob", 2),
        LISTING: (200, full),
        page2: (200, [{"name": "alice", "private": True}]),  # alice is listed: it exists
        path_of("r000"): repo("r000", 3),
    }
    api = FakeAPI(answers)
    assert store.check(api)["removed"] == [] and page2 in api.calls
    api.answers[page2] = (200, [{"name": "bob", "private": True}])
    assert store.check(api)["removed"] == ["alice"]
    # a page that fails: nothing is known. alice's mirror again first, so that the check has
    # a repository to keep (the reviewer's re-check of #733)
    shutil.copytree(store.cache / "bob.git", store.cache / "alice.git")
    (store.cache / "alice.git" / server.ID_FILE).unlink()
    api.answers[page2] = (502, None)
    api.calls.clear()
    assert store.check(api)["removed"] == [] and store.mirrors() == ["alice", "bob"]
    assert page2 in api.calls


def test_the_listing_stops_at_its_page_limit(site, monkeypatch):
    store, *_ = site
    monkeypatch.setattr(server, "LISTING_PAGES", 2)
    full = [{"name": f"r{i:03d}", "private": True} for i in range(100)]
    answers = {path_of("bob"): repo("bob", 2), LISTING: (200, full), path_of("r000"): repo("r", 3)}
    answers[f"/orgs/{ORG}/repos?type=private&per_page=100&page=2"] = (200, full)
    assert store.check(FakeAPI(answers))["removed"] == []  # more than it reads: unknown


def test_the_purge_command_removes_one_mirror(site, capsys, monkeypatch):
    store, get, shas, _ = site
    argv = ["--cache", str(store.cache), "purge"]
    logged = []
    monkeypatch.setattr(server, "journal", logged.append)
    assert server.main([*argv, "Alice"]) == 0
    assert capsys.readouterr().out == "removed mirror of alice\n"
    assert logged == ["purge: removed mirror of alice"]  # in the journal too
    assert server.Store(store.cache).mirrors() == ["bob"]
    assert server.main([*argv, "alice"]) == 0
    assert capsys.readouterr().out == "no mirror of alice\n"
    assert server.main([*argv, "../x"]) == 2
    assert get(f"/bob/{shas['bob']}/note.txt")[0] == 200


def test_a_removal_cut_short_is_finished_at_the_next_start(tmp_path):
    (tmp_path / "alice.git.gone" / "objects").mkdir(parents=True)
    store = server.Store(tmp_path)
    assert not (tmp_path / "alice.git.gone").exists() and store.mirrors() == []


# ------------------------------------------------------------------- GitHubAPI and the token


class FakeGitHub(BaseHTTPRequestHandler):
    """`renamed` moves to repository 12, which moves again; `moved` to 13, in the organization;
    `transferred` to 14, another owner's; `elsewhere` off the API."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        moves = {
            f"/repos/{ORG}/renamed": f"{base}/repositories/12",
            "/repositories/12": f"{base}/repositories/13",
            f"/repos/{ORG}/moved": f"{base}/repositories/13",
            f"/repos/{ORG}/transferred": f"{base}/repositories/14",
            f"/repos/{ORG}/elsewhere": "https://example.invalid/repositories/13",
        }
        if self.path in moves:
            self.send_response(301)
            self.send_header("Location", moves[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        owners = {"/repositories/13": ORG, "/repositories/14": "someone"}
        body = b""
        if self.path in owners:
            ident, owner = int(self.path.rsplit("/", 1)[1]), owners[self.path]
            body = json.dumps({"id": ident, "owner": {"login": owner}}).encode()
        self.send_response(200 if body else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def github():
    http = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    http.seen = []
    thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield (
        http,
        server.GitHubAPI("synthetic-token", base=f"http://127.0.0.1:{http.server_address[1]}"),
    )
    http.shutdown()
    http.server_close()
    thread.join()


def test_the_api_follows_one_redirect_and_only_within_itself(github):
    http, api = github
    assert api.get(f"/repos/{ORG}/moved")[0] == 200
    assert api.get(f"/repos/{ORG}/renamed") == (301, None)  # moved twice: no answer
    assert api.get(f"/repos/{ORG}/elsewhere") == (301, None)  # never off the API
    assert api.get(f"/repos/{ORG}/absent") == (404, None)
    assert [path for path, _ in http.seen] == [
        f"/repos/{ORG}/moved", "/repositories/13", f"/repos/{ORG}/renamed", "/repositories/12",
        f"/repos/{ORG}/elsewhere", f"/repos/{ORG}/absent",
    ]  # fmt: skip
    assert {auth for _, auth in http.seen} == {"Bearer synthetic-token"}


def test_a_redirect_tells_renamed_from_transferred(github, tmp_path):
    _, api = github
    lookup = server.Store(tmp_path, org=ORG).lookup_repo
    assert lookup(api, "moved") == (server.EXISTS, 13, "moved")
    assert lookup(api, "transferred") == (server.MISSING, None, "")
    assert lookup(api, "absent") == (server.MISSING, None, "")
    assert lookup(api, "renamed") == (server.UNKNOWN, None, "")
    assert lookup(api, "elsewhere") == (server.UNKNOWN, None, "")


def test_the_organization_is_the_configured_one_and_none_is_built_in(github, tmp_path):
    """#693: the lookups name the store's organization, and an owner's login is compared with
    it: a repository of another organization, even the example deployment's, is not found.
    Without an organization, nothing is asked and nothing is ever gone."""
    http, api = github
    other = server.Store(tmp_path / "other", org="MathAgents")
    assert other.lookup_repo(api, "moved") == (server.MISSING, None, "")  # 404: not its own
    assert http.seen[-1][0] == "/repos/MathAgents/moved"
    assert other.lookup_id(api, 13) == (server.MISSING, "")  # Example-Org's repository
    assert not hasattr(server, "ORG")
    unset, asked = server.Store(tmp_path / "unset"), len(http.seen)
    assert unset.lookup_repo(api, "moved") == (server.UNKNOWN, None, "")
    assert unset.lookup_id(api, 13) == (server.UNKNOWN, "")
    assert unset.reads_another(api, {"moved"}) is None
    assert len(http.seen) == asked
    with pytest.raises(server.GitError):
        unset.remote("alice")
    with pytest.raises(server.GitError):  # the tests' remotes too: no organization, no fetch
        server.Store(tmp_path / "t", test_remotes=tmp_path / "remotes").remote("alice")
    assert server.Store(tmp_path / "s", org=ORG).remote("alice") == (
        f"https://github.com/{ORG}/alice.git"
    )
    keyed = server.Store(tmp_path / "k", org=ORG, test_remotes=tmp_path / "remotes")
    assert keyed.remote("alice") == (tmp_path / "remotes" / ORG / "alice.git").as_uri()
    for bad in ("-x", "x-", "a/b", "a b", "x" * 40, "a.b"):
        with pytest.raises(ValueError, match="not a GitHub organization"):
            server.Store(tmp_path / "bad", org=bad)


def test_the_token_is_read_as_askpass_reads_it(tmp_path):
    token = tmp_path / "github-token"
    assert server.read_token(str(tmp_path)) == ""
    token.write_text("synthetic-token\n")
    token.chmod(0o600)
    assert server.read_token(str(tmp_path)) == "synthetic-token"
    token.chmod(0o644)
    assert server.read_token(str(tmp_path)) == ""
    token.chmod(0o600)
    token.write_text("two words\n")
    assert server.read_token(str(tmp_path)) == ""
    assert server.read_token("") == ""


def test_the_checker_runs_the_check_with_the_token_and_skips_without(tmp_path, monkeypatch, caplog):
    store = server.Store(tmp_path / "cache")
    ran = threading.Event()
    monkeypatch.setattr(store, "check", lambda api: ran.set())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    checker = server.Checker(store, delay=0.01, interval=0.01)
    checker.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.3)
        assert not ran.is_set() and "gone check skipped: no token" in caplog.text
        (tmp_path / "github-token").write_text("synthetic-token\n")
        (tmp_path / "github-token").chmod(0o600)
        assert ran.wait(5)
    finally:
        checker.stopped.set()
        checker.join(5)
