"""Real git and HTTP, entirely offline: no GitHub/Zulip credentials."""

import importlib.util
import io
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

DIRECTORY = Path(__file__).resolve().parents[3] / "workshop/files"
spec = importlib.util.spec_from_file_location("snapshot_server", DIRECTORY / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
pytestmark = pytest.mark.integration
#: The organization the server serves, and its host (#693): any deployment's, none built in.
ORG, HOST = "Example-Org", "https://files.example.org"


def git(root, *args):
    return (
        subprocess.check_output(  # noqa: S603
            [
                "/usr/bin/git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


@pytest.fixture
def site(tmp_path):
    remote = tmp_path / "remotes" / ORG / "alice.git"  # keyed by organization, as on GitHub
    remote.mkdir(parents=True)
    git(remote, "init", "--bare")
    # No automatic gc or maintenance after a push: a detached `git maintenance` creates and
    # deletes objects/maintenance.lock while tests copytree() this repository (issue #305).
    quiet = {"receive.autogc": "false", "gc.auto": "0", "maintenance.auto": "false"}
    for key, value in quiet.items():
        git(remote, "config", key, value)
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "--initial-branch=main")
    (work / "note.txt").write_text("branch\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "branch")
    branch = git(work, "rev-parse", "HEAD")
    git(work, "push", str(remote), "HEAD:refs/heads/main")
    files = {
        "note.txt": "héllo\n",
        "page.html": "<!doctype html><h1>Hello</h1>",
        "plot.svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        "folder/a b<.txt": "child",
        "folder/auth.json": "DECOY",
        "auth.json": "DECOY",
        ".ENV": "DECOY",
        ".aws/config": "DECOY",
        "Auth.JSON/nested.txt": "ordinary file",
        "id_theory/notes.txt": "allowed notes",
        "id_theory/id_secret": "DECOY",
        ":(glob)*": "literal",
        # rendered views (issue #256)
        "docs/README.md": "# Title\n\n$x^2$, [proof](Proof.lean#L2), [root](/note.txt)\n\n"
        "<script>alert(1)</script>\n",
        "docs/Proof.lean": "import Mathlib.Tactic\n\ntheorem t : True := trivial\n",
        "app.js": "console.log(1)\n",
        "lib.py/inner.txt": "a directory named like a source file\n",
    }
    for name, text in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (work / "linked").symlink_to("note.txt")
    git(work, "add", ".")
    git(work, "commit", "-m", "snapshot")
    sha = git(work, "rev-parse", "HEAD")
    git(work, "push", str(remote), "HEAD:refs/zab/turns/session/1")
    store = server.Store(tmp_path / "cache", org=ORG, test_remotes=remote.parent.parent)
    http = server.Server(("127.0.0.1", 0), store)
    # shutdown() waits for the loop's next poll: 0.5 s by default, in every test (#473)
    thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
    thread.start()

    def get(path, method="GET", headers=None):
        with closing(HTTPConnection(*http.server_address, timeout=10)) as conn:
            conn.request(method, path, headers=headers or {})
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()

    yield store, get, sha, branch, work, remote
    http.shutdown()
    http.server_close()
    thread.join()


def test_content_listings_headers_and_head(site):
    store, get, sha, _, _, _ = site
    status, headers, body = get(f"/alice/{sha}/note.txt")
    assert (status, body) == (200, "héllo\n".encode())
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in headers
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "immutable" in headers["Cache-Control"]
    assert get(f"/alice/{sha}/page.html")[1]["Content-Type"] == "text/html; charset=utf-8"
    for path in ("page.html", "plot.svg", "folder/"):
        assert get(f"/alice/{sha}/{path}")[1]["Content-Security-Policy"] == server.SANDBOX
    assert get(f"/alice/{sha}/note.txt", "HEAD")[2] == b""
    assert get(f"/alice/{sha.upper()}/note.txt")[0] == 200
    assert get(f"/alice/{sha}/%3A%28glob%29%2A")[2] == b"literal"
    status, headers, _ = get(f"/alice/{sha}/folder")
    assert status == 308 and headers["Location"] == f"/alice/{sha}/folder/"
    status, _, body = get(f"/alice/{sha}/folder/")
    assert status == 200 and b"a%20b%3C.txt" in body and b"a b&lt;.txt" in body
    assert b"auth.json" not in body
    assert f'href="/alice/{sha}/"'.encode() in body
    root = get(f"/alice/{sha}/")[2]
    assert all(name not in root for name in (b"auth.json", b".aws", b".ENV", b"linked"))
    assert git(store.cache / "alice.git", "for-each-ref", "--format=%(refname)") == (
        "refs/zab/turns/session/1"
    )


@pytest.mark.parametrize(
    "path",
    [
        "auth.json",
        "folder/auth.json",
        ".ENV",
        ".aws/config",
        "%2e%2e/note.txt",
        "folder/../note.txt",
        "folder//a",
        "%00",
        "%ff",
        "a%5cb",
        "linked",
        ".git/config",
        "absent",
    ],
)
def test_forbidden_paths(site, path):
    _, get, sha, *_ = site
    status, headers, body = get(f"/alice/{sha}/{path}")
    assert status == 404 and body == b"Not found\n"
    assert headers["Cache-Control"] == "no-store"


def test_only_full_tip_commit_shas(site):
    store, get, sha, branch, work, remote = site
    store.fetch_interval = 0.1  # the branch's miss fetches again after it, not 5 s (#473)
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    for ref in (branch, sha[:12], "main", sha + "0", sha + "^", "a" * 64):
        assert get(f"/alice/{ref}/note.txt")[0] == 404
    blob = git(work, "rev-parse", "HEAD:note.txt")
    git(work, "push", str(remote), f"{blob}:refs/zab/turns/not-a-commit")
    store.last_fetch.clear()
    assert get(f"/alice/{blob}/")[0] == 404


@pytest.mark.parametrize(
    "slug", [".", "..", "a" * 101, "a%2fb", "a%5cb", "%00", "a:b", "%E2%84%AA"]
)
def test_invalid_slug_never_fetches(site, slug):
    store, get, sha, *_ = site
    assert get(f"/{slug}/{sha}/note.txt")[0] == 404
    assert not store.last_fetch
    assert not list(store.cache.iterdir())


def test_concurrent_misses_fetch_once_and_new_tips_refresh(site, monkeypatch):
    store, get, sha, _, work, remote = site
    fetches = []
    store.fetch_interval = 0.1
    original = store.git

    def counted(repo, *args):
        if args[0] == "fetch":
            fetches.append(args)
        return original(repo, *args)

    monkeypatch.setattr(store, "git", counted)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: get(f"/alice/{sha}/note.txt")[0], range(6)))
    assert results == [200] * 6 and len(fetches) == 1
    (work / "note.txt").write_text("new")
    git(work, "commit", "-am", "new snapshot")
    new = git(work, "rev-parse", "HEAD")
    git(work, "push", str(remote), "HEAD:refs/zab/turns/session/2")
    # Force an unexpired success window regardless of machine/test speed.
    store.last_fetch["alice"] = server.time.monotonic() + 0.1
    before = server.time.monotonic()
    assert get(f"/Alice/{new}/note.txt")[2] == b"new"
    assert server.time.monotonic() - before >= 0.1
    assert len(fetches) == 2
    git(remote, "update-ref", "-d", "refs/zab/turns/session/1")
    store.last_fetch.clear()
    get("/alice/" + "a" * 40 + "/missing")  # unknown tip refreshes and prunes
    assert get(f"/alice/{sha}/note.txt")[0] == 404


def test_failed_fetch_throttled_and_logged(site, caplog):
    store, get, sha, *_ = site
    for _ in range(2):
        assert get(f"/missing/{sha}/note.txt")[0] == 404
    assert len(store.last_fetch) == 1
    assert not list(store.cache.iterdir())
    assert not store.gates
    assert "fetch failed repo=missing exit=128" in caplog.text
    assert sha not in caplog.text
    assert caplog.text.count("fetch failed") == 1


def test_unknown_repositories_have_bounded_expiring_history(site, monkeypatch):
    store, get, sha, *_ = site
    store.max_fetch_history = 2
    now = 100.0
    monkeypatch.setattr(server.time, "monotonic", lambda: now)
    for slug in ("missing-a", "missing-b", "missing-c", "missing-a"):
        assert get(f"/{slug}/{sha}/note.txt")[0] == 404
    assert set(store.last_fetch) == {"missing-a", "missing-b"}
    assert not list(store.cache.iterdir())
    # Do not evict an unexpired failure and accidentally bypass its throttle.
    assert get(f"/alice/{sha}/note.txt")[0] == 404
    now += store.negative_interval
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    assert set(store.last_fetch) == {"alice"}
    assert [path.name for path in store.cache.iterdir()] == ["alice.git"]


def test_blob_limit_and_failed_git_do_not_end_server(site, monkeypatch):
    store, get, sha, *_ = site
    monkeypatch.setattr(server, "MAX_BLOB", 1)
    assert get(f"/alice/{sha}/note.txt")[0] == 404
    monkeypatch.setattr(server, "MAX_BLOB", 32 * 1024 * 1024)
    original = store.git

    def timeout(*args):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(store, "git", timeout)
    assert get(f"/alice/{sha}/note.txt")[0] == 404
    monkeypatch.setattr(store, "git", original)
    assert get(f"/alice/{sha}/note.txt")[0] == 200


def test_askpass_decoy_private_file_and_host_scope(tmp_path):
    token = tmp_path / "github-token"
    token.write_text("synthetic-test-token\n")
    token.chmod(0o600)

    def ask(prompt):
        return subprocess.run(  # noqa: S603
            [sys.executable, str(DIRECTORY / "askpass.py"), prompt],
            env={"CREDENTIALS_DIRECTORY": str(tmp_path)},
            capture_output=True,
        )

    assert ask("Username for 'https://github.com': ").stdout == b"x-access-token\n"
    assert ask("Password for 'https://x-access-token@github.com': ").stdout == (
        b"synthetic-test-token\n"
    )
    assert ask("Password for 'https://evil.example': ").returncode == 1
    token.chmod(0o440)
    assert ask("Password for 'https://github.com': ").returncode == 0
    token.chmod(0o644)
    result = ask("Password for 'https://github.com': ")
    assert result.returncode == 1 and not result.stdout and not result.stderr


@pytest.mark.parametrize("ssh", [False, True])
@pytest.mark.parametrize("slug", ["Alice", "a.b", "a_b"])
def test_deployment_selftest_against_local_remote(site, monkeypatch, capsys, ssh, slug):
    import shutil

    store, get, _, branch, _, remote = site
    store.fetch_interval = 0.01
    if slug.lower() != "alice":
        renamed = remote.with_name(slug.lower() + ".git")
        shutil.copytree(remote, renamed)
        remote = renamed
    spec = importlib.util.spec_from_file_location("files_selftest", DIRECTORY / "selftest.py")
    selftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selftest)
    original = subprocess.check_output

    pushed = []

    def local_git(argv, **kwargs):
        assert argv[0] != "gh"  # Only configured git credentials are required.
        github = (f"https://github.com/{ORG}/{slug}.git", f"git@github.com:{ORG}/{slug}.git")
        pushed.extend(arg for arg in argv if arg in github)
        argv = [remote.as_uri() if arg in github else arg for arg in argv]
        return original(argv, **kwargs)

    def local_urlopen(request, timeout):
        url = getattr(request, "full_url", request)  # a URL, or a Request with headers (#256)
        headers = dict(request.header_items()) if hasattr(request, "header_items") else {}
        if url.startswith("https://api.github.com/repos/"):
            assert url == f"https://api.github.com/repos/{ORG}/{slug}"  # --org's (#693)
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())
        assert url.startswith((f"{HOST}/{slug.lower()}/", f"{HOST}/_zab/"))
        parts = urlsplit(url)
        status, _, body = get(
            parts.path + (f"?{parts.query}" if parts.query else ""), headers=headers
        )
        response = io.BytesIO(body)
        response.status = status
        return response

    monkeypatch.setattr(selftest.subprocess, "check_output", local_git)
    monkeypatch.setattr(selftest, "urlopen", local_urlopen)
    monkeypatch.setattr(
        sys,
        "argv",
        ["selftest.py", slug, "--org", ORG, "--url", HOST]
        + (["--push-url", f"git@github.com:{ORG}/{slug}.git"] if ssh else []),
    )
    selftest.main()
    out = capsys.readouterr().out
    assert "PASS: snapshot file served" in out
    assert "PASS: 9 files rendered (Markdown with math, 8 languages); ?raw=1 exact" in out
    assert f" in {ORG}/{slug.lower()}\n" in out
    assert len(pushed) == 1  # the one push, to --org's repository
    assert git(remote, "rev-parse", "refs/heads/main") == branch


@pytest.mark.parametrize(
    "name,body,kind",
    [
        ("plot.pdf", b"%PDF-1.7", "application/pdf"),
        ("module.wasm", b"\x00asm", "application/wasm"),
        ("movie.mp4", b"movie", "video/mp4"),
        ("sound.mp3", b"audio", "audio/mpeg"),
        ("sound.wav", b"audio", "audio/wav"),
        ("sound.ogg", b"audio", "audio/ogg"),
        ("sound.m4a", b"audio", "audio/mp4"),
        ("movie.mov", b"movie", "video/quicktime"),
        ("image.bmp", b"image", "image/bmp"),
        ("movie.webm", b"movie", "video/webm"),
        ("picture.avif", b"picture", "image/avif"),
        ("code.m", b"print 1;", "text/plain; charset=utf-8"),
        ("data.csv", b"a,b\n1,2", "text/plain; charset=utf-8"),
        ("module.mjs", b"export default 1", "text/javascript; charset=utf-8"),
        ("unknown", b"a" * 8191 + "é".encode(), "text/plain; charset=utf-8"),
        ("unknown", b"a" * 8192 + b"\xff", "application/octet-stream"),
    ],
)
def test_mime_is_explicit_and_sniffing_is_bounded(name, body, kind):
    assert server.content_type(name, body) == kind


def test_pdf_not_sandboxed_and_forty_eight_requests_wait(site):
    _, get, _, _, work, remote = site
    (work / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "PDF")
    sha = git(work, "rev-parse", "HEAD")
    git(work, "push", str(remote), "HEAD:refs/zab/turns/pdf")
    burst = threading.Barrier(48)

    def request(_):
        burst.wait(timeout=5)
        return get(f"/alice/{sha}/paper.pdf")

    with ThreadPoolExecutor(max_workers=48) as pool:
        results = list(pool.map(request, range(48)))
    assert all(status == 200 for status, _, _ in results)
    assert all("Content-Security-Policy" not in headers for _, headers, _ in results)
    assert all(headers["Access-Control-Allow-Origin"] == "*" for _, headers, _ in results)


def test_missing_path_does_not_fetch_and_hit_does_not_wait_for_fetch(site, monkeypatch):
    store, get, sha, *_ = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    store.last_fetch.clear()
    started, release = threading.Event(), threading.Event()
    original = store.fetch

    def slow_fetch(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(store, "fetch", slow_fetch)
    assert get(f"/alice/{sha}/missing")[0] == 404
    assert not started.is_set()
    # Eight misses must not occupy all body slots.
    arrivals = threading.Barrier(9)
    original_tip = store.known_tip

    def tip(repo, value):
        if repo.name.startswith("missing"):
            arrivals.wait(timeout=5)
        return original_tip(repo, value)

    # Only synchronize each request's first check (before its stripe wait).
    seen = set()

    def once(repo, value):
        if threading.get_ident() not in seen and repo.name.startswith("missing"):
            seen.add(threading.get_ident())
            return tip(repo, value)
        return original_tip(repo, value)

    monkeypatch.setattr(store, "known_tip", once)
    with ThreadPoolExecutor(max_workers=9) as pool:
        misses = [pool.submit(get, f"/missing{i}/{'a' * 40}/note.txt") for i in range(8)]
        try:
            arrivals.wait(timeout=5)
            assert started.wait(2)
            hit = pool.submit(get, f"/alice/{sha}/note.txt")
            assert hit.result(timeout=2)[0] == 200
        finally:
            release.set()
        assert all(miss.result()[0] == 404 for miss in misses)


def test_new_repo_budget_logs_without_starving_existing_mirrors(site, monkeypatch, caplog):
    store, get, sha, *_ = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    store.last_fetch.clear()
    store.fetch_times.clear()
    now = server.time.monotonic()
    monkeypatch.setattr(server.time, "monotonic", lambda: now)
    store.fetch_rate_limit = 2
    assert get(f"/missing/{sha}/x")[0] == 404
    now += 6
    assert get(f"/missing/{sha}/x")[0] == 404
    assert len(store.fetch_times) == 1
    assert get(f"/other/{sha}/x")[0] == 404
    assert get(f"/third/{sha}/note.txt")[0] == 404
    assert "repo=third: new-mirror budget exhausted" in caplog.text
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    # Even a new turn in an existing mirror can fetch after the small budget fills.
    _, _, _, _, work, remote = site
    (work / "note.txt").write_text("fresh")
    git(work, "commit", "-am", "new turn")
    fresh = git(work, "rev-parse", "HEAD")
    git(work, "push", str(remote), "HEAD:refs/zab/turns/fresh")
    assert get(f"/alice/{fresh}/note.txt")[2] == b"fresh"
    assert len(store.fetch_times) == 2
    now += 61
    assert get(f"/alice/{sha}/note.txt")[0] == 200


def test_stale_git_lock_removed_before_fetch(site):
    import os
    import time

    store, get, sha, *_ = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    lock = store.cache / "alice.git/packed-refs.lock"
    lock.write_text("stale")
    os.utime(lock, (time.time() - 60, time.time() - 60))
    store.last_fetch.clear()
    get("/alice/" + "a" * 40 + "/note.txt")
    assert not lock.exists()


def test_git_error_log_detail_scrubbed():
    detail = b"fatal: ghp_DEC0Y https://secret@github.com/ " + b"a" * 40 + b"\nsecond line"
    error = server.GitError(128, detail)
    assert error.code == 128
    assert all(text not in error.detail for text in ("ghp_DEC0Y", "secret", "a" * 40, "second"))


@pytest.mark.parametrize("status", [200, 403, 500])
def test_selftest_refuses_public_or_unverifiable_repo_before_push(monkeypatch, status):
    from unittest.mock import Mock

    spec = importlib.util.spec_from_file_location("files_selftest", DIRECTORY / "selftest.py")
    selftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selftest)
    output = Mock()

    asked = []

    def anonymous(url, timeout):
        asked.append(url)
        if status != 200:
            raise HTTPError(url, status, "unavailable", {}, io.BytesIO())
        return io.BytesIO()

    monkeypatch.setattr(selftest, "urlopen", anonymous)
    monkeypatch.setattr(selftest.subprocess, "check_output", output)
    monkeypatch.setattr(sys, "argv", ["selftest.py", "alice", "--org", ORG, "--url", HOST])
    with pytest.raises(SystemExit, match="2"):
        selftest.main()
    output.assert_not_called()
    assert asked == [f"https://api.github.com/repos/{ORG}/alice"]  # refused there, not before


@pytest.mark.parametrize(
    "argv",
    [
        ["alice", "--url", HOST],  # no --org: there is no default organization (#693)
        ["alice", "--org", ORG],  # no --url: no default host either
        ["alice", "--org", "-bad", "--url", HOST],
        ["alice", "--org", "a/b", "--url", HOST],
        ["alice", "--org", ORG, "--url", HOST, "--push-url", "git@github.com:MathAgents/alice.git"],
        ["alice", "--org", ORG, "--url", HOST, "--push-url", f"git@github.com:{ORG}/bob.git"],
    ],
)
def test_selftest_needs_its_organization_and_host_and_pushes_only_there(monkeypatch, argv):
    """#693: the selftest pushes to `--org`'s repository, the organization the server serves,
    and reads it back from `--url`; a push URL elsewhere is refused before anything is asked."""
    from unittest.mock import Mock

    spec = importlib.util.spec_from_file_location("files_selftest", DIRECTORY / "selftest.py")
    selftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selftest)
    output, anonymous = Mock(), Mock()
    monkeypatch.setattr(selftest, "urlopen", anonymous)
    monkeypatch.setattr(selftest.subprocess, "check_output", output)
    monkeypatch.setattr(sys, "argv", ["selftest.py", *argv])
    with pytest.raises(SystemExit, match="2"):
        selftest.main()
    anonymous.assert_not_called()
    output.assert_not_called()


def test_selftest_through_a_tunnel_to_a_staged_server(site, monkeypatch, capsys):
    """`--url http://127.0.0.1:PORT`: the update's check of the server staged beside the running
    one, before the switch, through an SSH tunnel (README; #442). Real HTTP to a second local
    server on the same cache layout; only GitHub is faked."""
    store, _, _, branch, _, remote = site
    store.fetch_interval = 0.01
    staged = server.Server(("127.0.0.1", 0), store)
    thread = threading.Thread(target=staged.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{staged.server_address[1]}"
    spec = importlib.util.spec_from_file_location("files_selftest", DIRECTORY / "selftest.py")
    selftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selftest)
    original, real_urlopen = subprocess.check_output, selftest.urlopen

    def local_git(argv, **kwargs):
        github = f"https://github.com/{ORG}/alice.git"
        return original([remote.as_uri() if arg == github else arg for arg in argv], **kwargs)

    def urlopen(request, timeout):
        url = getattr(request, "full_url", request)
        if url == f"https://api.github.com/repos/{ORG}/alice":
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())
        assert url.startswith(f"{base}/"), url  # nothing goes to the running host
        return real_urlopen(request, timeout=timeout)

    monkeypatch.setattr(selftest.subprocess, "check_output", local_git)
    monkeypatch.setattr(selftest, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["selftest.py", "alice", "--org", ORG, "--url", base])
    try:
        selftest.main()
    finally:
        staged.shutdown()
        staged.server_close()
        thread.join()
    out = capsys.readouterr().out
    assert "PASS: snapshot file served" in out
    assert "PASS: 9 files rendered (Markdown with math, 8 languages); ?raw=1 exact" in out
    assert git(remote, "rev-parse", "refs/heads/main") == branch


@pytest.mark.parametrize(
    "url",
    [
        "http://files.mathagents.org",
        "http://127.0.0.1.example.com:8081",
        "http://localhost.example.com:8081",
        "http://127.0.0.1:8081@example.com",
        "http://127.0.0.1",
        "ftp://127.0.0.1:8081",
    ],
)
def test_selftest_refuses_plain_http_off_this_machine(monkeypatch, url):
    """Plain HTTP only to a loopback port (a tunnel); never over the network (#442)."""
    from unittest.mock import Mock

    spec = importlib.util.spec_from_file_location("files_selftest", DIRECTORY / "selftest.py")
    selftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selftest)
    output, anonymous = Mock(), Mock()
    monkeypatch.setattr(selftest, "urlopen", anonymous)
    monkeypatch.setattr(selftest.subprocess, "check_output", output)
    monkeypatch.setattr(sys, "argv", ["selftest.py", "alice", "--org", ORG, "--url", url])
    with pytest.raises(SystemExit, match="2"):
        selftest.main()
    anonymous.assert_not_called()
    output.assert_not_called()


def test_the_server_serves_the_configured_organization_and_needs_one(
    tmp_path, monkeypatch, capsys, caplog
):
    """#693: `--org`, by default `$ZAB_FILES_ORG` (the unit's EnvironmentFile), is the one
    organization fetched from; without one, or with a name GitHub can't have, the service
    exits 78 (EX_CONFIG, which the unit doesn't restart) before it serves anything. A
    deployer's command needs none."""
    from unittest.mock import MagicMock, Mock

    served = Mock(side_effect=SystemExit("served"))
    monkeypatch.setattr(server.Server, "serve_forever", served)
    monkeypatch.setattr(server.Checker, "start", Mock())
    monkeypatch.setattr(server, "journal", Mock())  # purge's line, never the test's syslog
    argv = ["--cache", str(tmp_path / "cache"), "--shares", str(tmp_path / "shares"), "--port", "0"]
    monkeypatch.delenv(server.ORG_ENV, raising=False)
    assert server.EX_CONFIG == 78
    assert server.main(argv) == 78
    assert "no GitHub organization: give --org, or ZAB_FILES_ORG=<organization> in " in (
        capsys.readouterr().err
    )
    for bad in ("-x", "a/b", "x" * 40, "a.b"):
        assert server.main([*argv, f"--org={bad}"]) == 78
        assert f"not a GitHub organization: {bad!r}" in capsys.readouterr().err
    monkeypatch.setenv(server.ORG_ENV, "-bad")
    assert server.main(argv) == 78
    served.assert_not_called()
    with pytest.raises(SystemExit) as usage:  # argparse's own error stays 2: restarted, as before
        server.main([*argv, "--port", "x"])
    assert usage.value.code == 2
    assert server.main([*argv, "purge", "alice"]) == 0  # a command: no organization needed
    stores = []
    monkeypatch.setattr(server, "Server", lambda _, store: stores.append(store) or MagicMock())
    monkeypatch.setenv(server.ORG_ENV, f" {ORG}\n")  # as an EnvironmentFile may leave it
    assert server.main(argv) == 0
    monkeypatch.setenv(server.ORG_ENV, "MathAgents")
    assert server.main([*argv, "--org", ORG]) == 0  # --org wins
    assert [store.org for store in stores] == [ORG, ORG]
    assert caplog.text.count("serving the repositories of Example-Org") == 2


def test_a_fetch_goes_to_the_configured_organization_alone(tmp_path, monkeypatch):
    """#693: the fetch and the share listing name `org`'s repository on GitHub, never a built-in
    organization's; the remote is git's argument, not a shell's."""
    from unittest.mock import Mock

    store = server.Store(tmp_path / "cache", org=ORG)
    git = Mock(return_value=b"")
    monkeypatch.setattr(store, "git", git)
    store.fetch_now("alice", tmp_path / "cache" / "alice.git", source="Alice")
    store.remote_shares("alice")
    remotes = [a for call in git.call_args_list for a in call.args if "github.com" in str(a)]
    assert remotes == [f"https://github.com/{ORG}/Alice.git", f"https://github.com/{ORG}/alice.git"]


def test_a_server_of_another_organization_serves_none_of_its_snapshots(site, tmp_path):
    """Review of #770, N6: the test remotes are keyed by organization, as GitHub is, so a
    server configured with another organization than the snapshot's fetches nothing and
    answers 404, where the right one serves it (`site`)."""
    store, get, sha, _, _, remote = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    assert store.remote("alice") == remote.as_uri()
    other = server.Store(tmp_path / "other", org="MathAgents", test_remotes=store.test_remotes)
    assert other.remote("alice") != remote.as_uri()
    with server.Server(("127.0.0.1", 0), other) as http:
        thread = threading.Thread(target=http.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            with closing(HTTPConnection(*http.server_address, timeout=10)) as conn:
                conn.request("GET", f"/alice/{sha}/note.txt")
                assert conn.getresponse().status == 404
        finally:
            http.shutdown()
            thread.join()
    assert other.mirrors() == []  # its failed first fetch left no mirror


def test_the_selftest_fails_against_a_server_of_another_organization(site, monkeypatch):
    """Review of #770, N6: the selftest pushes to `--org`'s repository; a server that serves
    another organization can't read it back, and the selftest says so, naming `--org`."""
    store, get, _, _, _, remote = site
    store.org = "MathAgents"  # the server of another organization than the selftest's
    spec = importlib.util.spec_from_file_location("files_selftest", DIRECTORY / "selftest.py")
    selftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selftest)
    original = subprocess.check_output

    def local_git(argv, **kwargs):
        github = f"https://github.com/{ORG}/alice.git"
        return original([remote.as_uri() if arg == github else arg for arg in argv], **kwargs)

    def local_urlopen(request, timeout):
        url = getattr(request, "full_url", request)
        if url.startswith("https://api.github.com/"):
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO())
        parts = urlsplit(url)
        status, _, body = get(parts.path + (f"?{parts.query}" if parts.query else ""))
        response = io.BytesIO(body)
        response.status = status
        return response

    monkeypatch.setattr(selftest.subprocess, "check_output", local_git)
    monkeypatch.setattr(selftest, "urlopen", local_urlopen)
    no_wait = SimpleNamespace(sleep=lambda seconds: None, time_ns=time.time_ns)
    monkeypatch.setattr(selftest, "time", no_wait)  # its 12 tries at once, not over 72 s
    monkeypatch.setattr(sys, "argv", ["selftest.py", "alice", "--org", ORG, "--url", HOST])
    with pytest.raises(SystemExit, match=f"does the server serve {ORG}"):
        selftest.main()


@pytest.mark.parametrize("exited", [False, True])
def test_git_timeout_terminates_process_group_before_kill(tmp_path, monkeypatch, exited):
    from types import SimpleNamespace
    from unittest.mock import Mock

    proc = SimpleNamespace(
        pid=12345,
        communicate=Mock(
            side_effect=[
                subprocess.TimeoutExpired("git", 30),
                (b"", b""),
            ]
        ),
    )
    popen = Mock(return_value=proc)
    kill = Mock(side_effect=ProcessLookupError if exited else None)
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    monkeypatch.setattr(server.os, "killpg", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        server.Store(tmp_path).git(tmp_path, "fetch")
    kill.assert_called_once_with(12345, server.signal.SIGTERM)
    assert proc.communicate.call_count == 2
    assert "gc.autoDetach=false" in popen.call_args.args[0]
    assert popen.call_args.kwargs["start_new_session"] is True


def test_admission_is_bounded_before_thread_creation(tmp_path, monkeypatch, caplog):
    from unittest.mock import Mock

    started = Mock()
    monkeypatch.setattr(server.ThreadingHTTPServer, "process_request", started)
    with server.Server(("127.0.0.1", 0), server.Store(tmp_path)) as http:
        http.admission_timeout = 0.001
        requests = [Mock() for _ in range(40)]
        for request in requests:
            http.process_request(request, ("127.0.0.1", 1))
        assert started.call_count == 32
        assert "load shed status=503: handler admission timeout" in caplog.text
        assert all(not request.sendall.called for request in requests[:32])
        assert all(b"503" in request.sendall.call_args.args[0] for request in requests[32:])


@pytest.mark.parametrize("slug", ["Alice", "a.b", "a_b", "-alice", "alice-", "a" * 100])
def test_github_repository_names_share_normalized_mirrors(site, slug):
    import shutil

    store, get, sha, _, _, remote = site
    if slug.lower() != "alice":
        shutil.copytree(remote, remote.with_name(slug.lower() + ".git"))
    assert get(f"/{slug}/{sha}/note.txt")[0] == 200
    assert get(f"/{slug.upper()}/{sha}/note.txt")[0] == 200
    assert set(store.last_fetch) == {slug.lower()}
    assert [p.name for p in store.cache.iterdir()] == [slug.lower() + ".git"]


def test_directory_names_do_not_match_filename_deny_rules(site):
    _, get, sha, *_ = site
    root = get(f"/alice/{sha}/")[2]
    assert b"id_theory/" in root and b"Auth.JSON/" in root
    assert get(f"/alice/{sha}/id_theory/notes.txt")[2] == b"allowed notes"
    assert get(f"/alice/{sha}/Auth.JSON/nested.txt")[2] == b"ordinary file"
    listing = get(f"/alice/{sha}/id_theory/")[2]
    assert b"notes.txt" in listing and b"id_secret" not in listing
    assert get(f"/alice/{sha}/id_theory/id_secret")[0] == 404


def test_cached_hit_checks_tip_only_once(site, monkeypatch):
    from unittest.mock import Mock

    store, get, sha, *_ = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    check = Mock(wraps=store.known_tip)
    monkeypatch.setattr(store, "known_tip", check)
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    assert check.call_count == 1


def test_stale_lock_disappears_during_sweep(site, monkeypatch):
    store, get, sha, *_ = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    store.last_fetch.clear()
    original = server.Path.rglob

    def vanished(path, pattern):
        yield from original(path, pattern)
        yield path / "already-removed.lock"

    monkeypatch.setattr(server.Path, "rglob", vanished)
    assert get(f"/alice/{'a' * 40}/note.txt")[0] == 404
    assert "alice" not in store.failed_fetch


def test_admission_waits_for_a_thread_instead_of_rejecting_a_burst(tmp_path, monkeypatch):
    from unittest.mock import Mock

    started = threading.Event()
    monkeypatch.setattr(server.ThreadingHTTPServer, "process_request", lambda *_: started.set())
    with server.Server(("127.0.0.1", 0), server.Store(tmp_path)) as http:
        for _ in range(32):
            assert http.admission.acquire(blocking=False)
        request = Mock()
        worker = threading.Thread(target=http.process_request, args=(request, ("127.0.0.1", 1)))
        worker.start()
        try:
            assert not started.wait(0.05)
            assert not request.sendall.called
        finally:
            http.admission.release()
            worker.join(timeout=2)
        assert not worker.is_alive() and started.is_set()
        assert not request.sendall.called


def test_bogus_sha_burst_is_coalesced_and_does_not_delay_cached_hits(site, monkeypatch):
    import time

    store, get, sha, *_ = site
    assert get(f"/alice/{sha}/note.txt")[0] == 200
    store.last_fetch.clear()
    cooling, release = threading.Event(), threading.Event()
    original, fetches = store.git, []
    original_fetch = store.fetch

    def git_counted(repo, *args):
        if args[0] == "fetch":
            fetches.append(args)
        return original(repo, *args)

    def before_fetch(*args):
        cooling.set()
        assert release.wait(10)
        return original_fetch(*args)

    monkeypatch.setattr(store, "git", git_counted)
    # Do not patch global time.sleep: subprocess timeout polling uses it too.
    monkeypatch.setattr(store, "fetch", before_fetch)
    with ThreadPoolExecutor(max_workers=41) as pool:
        misses = [pool.submit(get, f"/alice/{i:040x}/x") for i in range(40)]
        try:
            assert cooling.wait(2)
            # A same-repo cached hit bypasses both the per-repo waiter cap and lock.
            assert pool.submit(get, f"/alice/{sha}/note.txt").result(timeout=2)[0] == 200
            assert len(store.gates["alice"].pending) <= 8
            # Finish a full wave of requests before releasing the coalesced fetch.
            deadline = time.monotonic() + 2
            while sum(m.done() for m in misses) < 32 and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert sum(m.done() for m in misses) >= 32
        finally:
            release.set()
        statuses = [m.result()[0] for m in misses]
        assert all(status in (404, 503) for status in statuses)
        assert statuses.count(503) >= 32
    assert len(fetches) == 1


def test_fetch_pool_timeout_rolls_back_new_mirror_reservations(site, monkeypatch, caplog):
    from unittest.mock import Mock

    store, get, sha, *_ = site
    monkeypatch.setattr(store, "fetch_capacity", Mock(acquire=Mock(return_value=False)))
    assert get(f"/missing/{sha}/x")[0] == 503
    assert not store.last_fetch and not store.failed_fetch and not store.fetch_times
    assert not store.gates and "fetch pool busy" in caplog.text


@pytest.mark.parametrize("fail", [False, True])
def test_large_response_chunks_and_never_sends_second_status(fail):
    from types import SimpleNamespace
    from unittest.mock import Mock

    payload = b"x" * (600 * 1024)
    handler = object.__new__(server.Handler)
    handler.path, handler.command = "/alice/" + "a" * 40 + "/x", "GET"
    handler.server = SimpleNamespace(
        store=Mock(
            prepare=Mock(return_value=True), read=Mock(return_value=(payload, "text/plain", False))
        ),
        capacity=threading.BoundedSemaphore(8),
    )
    for name in ("send_response", "headers_common", "send_header", "end_headers"):
        setattr(handler, name, Mock())
    handler.connection = Mock()
    handler.wfile = SimpleNamespace(
        write=Mock(side_effect=[None, TimeoutError()] if fail else None)
    )
    handler.do_GET()
    handler.send_response.assert_called_once_with(200)
    chunks = [bytes(c.args[0]) for c in handler.wfile.write.call_args_list]
    assert all(len(c) <= 256 * 1024 for c in chunks)
    assert len(chunks) == (2 if fail else 3)
    if not fail:
        assert b"".join(chunks) == payload
    assert all(handler.server.capacity.acquire(blocking=False) for _ in range(8))


def test_concurrent_valid_snapshots_wait_through_slow_fetch(site, monkeypatch):
    store, get, sha, *_ = site
    started, release = threading.Event(), threading.Event()
    original = store.git

    def slow_fetch(repo, *args):
        if args[0] == "fetch":
            started.set()
            assert release.wait(5)
        return original(repo, *args)

    monkeypatch.setattr(store, "git", slow_fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get, f"/alice/{sha}/note.txt")
        try:
            assert started.wait(2)
            second = pool.submit(get, f"/alice/{sha}/note.txt")
            assert not threading.Event().wait(1.3)
            assert not second.done()
        finally:
            release.set()
        assert first.result()[0] == second.result()[0] == 200


def test_twenty_requests_share_one_slow_new_snapshot_fetch(site, monkeypatch):
    store, get, sha, *_ = site
    original = store.git
    fetched = []

    def slow(repo, *args):
        if args[0] == "fetch":
            fetched.append(True)
            threading.Event().wait(1.5)
        return original(repo, *args)

    monkeypatch.setattr(store, "git", slow)
    barrier = threading.Barrier(20)

    def request(_):
        barrier.wait(5)
        return get(f"/alice/{sha}/note.txt")[0]

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert list(pool.map(request, range(20))) == [200] * 20
    assert len(fetched) == 1
    assert store.pending_tips == 0


def test_initial_tip_probe_git_error_falls_through_to_fetch_gate(site, monkeypatch):
    store, get, sha, *_ = site
    original = store.known_tip
    calls = 0

    def initializing(repo, tip):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise server.GitError(128, b"not a git repository yet")
        return original(repo, tip)

    monkeypatch.setattr(store, "known_tip", initializing)
    assert get(f"/alice/{sha}/note.txt")[0] == 200


def test_global_distinct_tip_cap_does_not_leave_empty_gate(tmp_path):
    store = server.Store(tmp_path)
    store.pending_tips = 32
    with pytest.raises(server.FetchBusy):
        store.prepare("new", "a" * 40, "x")
    assert not store.gates


@pytest.mark.parametrize("status", [404, 503])
def test_disconnected_error_response_does_not_escape(status):
    from types import SimpleNamespace
    from unittest.mock import Mock

    handler = object.__new__(server.Handler)
    handler.path = "/alice/" + "a" * 40 + "/x"
    handler.server = SimpleNamespace(store=Mock())
    handler.server.store.prepare.side_effect = server.FetchBusy() if status == 503 else OSError()
    handler.reply = Mock(side_effect=BrokenPipeError())
    handler.do_GET()
    assert handler.reply.call_args.args[0] == status


def test_body_deadline_caps_slow_progress(monkeypatch):
    from unittest.mock import Mock

    handler = object.__new__(server.Handler)
    handler.command = "GET"
    handler.connection, handler.wfile = Mock(), Mock()
    handler.send_response = handler.send_header = handler.end_headers = handler.headers_common = (
        Mock()
    )
    clock = iter([0, 0, 41])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        handler.reply(200, b"x" * (8 * 1024 * 1024), "application/octet-stream")
    assert handler.wfile.write.call_count == 1


def test_thirty_five_participants_can_open_distinct_cold_snapshots(site, monkeypatch):
    import shutil

    store, get, sha, _, _, remote = site
    for i in range(35):
        shutil.copytree(remote, remote.with_name(f"guest{i}.git"))
    original = store.git

    def slow_fetch(repo, *args):
        if args[0] == "fetch":
            threading.Event().wait(0.05)
        return original(repo, *args)

    monkeypatch.setattr(store, "git", slow_fetch)
    burst = threading.Barrier(35)

    def request(i):
        burst.wait(5)
        return get(f"/guest{i}/{sha}/note.txt")[0]

    with ThreadPoolExecutor(max_workers=35) as pool:
        assert list(pool.map(request, range(35))) == [200] * 35
    assert store.pending_tips == 0


# --------------------------------------------- rendered views (issue #256)

NAVIGATE = {"Sec-Fetch-Dest": "document"}


def test_a_browser_navigating_to_markdown_gets_the_sandboxed_page(site):
    _, get, sha, *_ = site
    status, headers, body = get(f"/alice/{sha}/docs/README.md", headers=NAVIGATE)
    page = body.decode()
    assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"] == server.SANDBOX  # as agent HTML
    assert headers["Vary"] == "Sec-Fetch-Dest" and "immutable" in headers["Cache-Control"]
    assert '<h1 id="title">Title</h1>' in page and '<span class="math inline">x^2</span>' in page
    assert '<a href="Proof.lean#L2">' in page  # resolves inside the snapshot
    assert f'<a href="/alice/{sha}/note.txt">' in page  # `/x` is the snapshot's root
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page
    assert "/_zab/static/katex/katex.min.js?v=" in page
    assert '<a class="raw" href="?raw=1">Raw</a>' in page


def test_everything_but_navigation_gets_the_exact_bytes(site):
    _, get, sha, _, work, _ = site
    raw = (work / "docs" / "README.md").read_bytes()
    for headers, query in (({}, ""), (NAVIGATE, "?raw=1"), ({"Sec-Fetch-Dest": "empty"}, "")):
        status, got, body = get(f"/alice/{sha}/docs/README.md{query}", headers=headers)
        assert (status, body) == (200, raw)
        assert (
            got["Content-Type"] == "text/plain; charset=utf-8" and got["Vary"] == "Sec-Fetch-Dest"
        )
        assert "Content-Security-Policy" not in got
    # an agent's page loading its own script still gets JavaScript; navigating to it, a view
    status, got, body = get(f"/alice/{sha}/app.js", headers={"Sec-Fetch-Dest": "script"})
    assert (body, got["Content-Type"]) == (b"console.log(1)\n", "text/javascript; charset=utf-8")
    assert get(f"/alice/{sha}/app.js", headers=NAVIGATE)[1]["Content-Type"].startswith("text/html")
    # a file we don't render is unchanged, and doesn't vary
    status, got, body = get(f"/alice/{sha}/note.txt", headers=NAVIGATE)
    assert body == "héllo\n".encode() and "Vary" not in got


def test_a_source_file_gets_line_anchors(site):
    _, get, sha, *_ = site
    page = get(f"/alice/{sha}/docs/Proof.lean", headers=NAVIGATE)[2].decode()
    assert '<tr id="L3"><td class="ln"><a href="#L3">3</a></td>' in page
    assert "katex" not in page


def test_a_renderer_failure_still_serves_the_file(site, monkeypatch):
    _, get, sha, _, work, _ = site

    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(server.RENDER, "render", broken)
    status, headers, body = get(f"/alice/{sha}/docs/README.md", headers=NAVIGATE)
    assert (status, body) == (200, (work / "docs" / "README.md").read_bytes())
    # not cached: the next visit gets the page once the bug is gone (review on #266)
    assert headers["Cache-Control"] == "no-store" and headers["Vary"] == "Sec-Fetch-Dest"
    monkeypatch.setattr(server, "RENDER", None)
    assert get(f"/alice/{sha}/docs/README.md", headers=NAVIGATE)[2].startswith(b"# Title")


def test_the_static_route_serves_only_the_vendored_assets(site):
    _, get, *_ = site
    status, headers, body = get("/_zab/static/files.css")
    assert status == 200 and headers["Content-Type"] == "text/css; charset=utf-8"
    assert headers["Access-Control-Allow-Origin"] == "*"  # fonts load in sandboxed pages
    assert headers["Cache-Control"] == "public, max-age=86400" and b".hl .k" in body
    assert get("/_zab/static/katex/katex.min.js")[1]["Content-Type"].startswith("text/javascript")
    font = get("/_zab/static/katex/fonts/KaTeX_Main-Regular.woff2")
    assert font[0] == 200 and font[1]["Content-Type"] == "font/woff2"
    for path in (
        "/_zab/static/../server.py",
        "/_zab/static/%2E%2E/render.py",
        "/_zab/static/katex/../../server.py",
        "/_zab/static/missing.js",
        "/_zab/static/",
    ):
        assert get(path)[0] == 404, path


def test_a_directory_named_like_a_source_file_is_still_a_listing(site):
    """Codex round 1 on #266: `lib.py/` is a directory, so its listing, with links."""
    _, get, sha, *_ = site
    status, headers, body = get(f"/alice/{sha}/lib.py/", headers=NAVIGATE)
    assert status == 200 and b'lib.py/inner.txt">inner.txt</a>' in body
    assert b'<tr id="L1">' not in body
    assert "Vary" not in headers


def test_renders_are_bounded_and_a_busy_renderer_sends_uncached_bytes(site, monkeypatch):
    """Four big renders at once reached 1.13 GB against the unit's MemoryMax=1G (review on
    #266): at most RENDER_SLOTS run, and a navigation that finds none free gets the bytes."""
    _, get, sha, _, work, _ = site
    url, raw = f"/alice/{sha}/docs/README.md", (work / "docs" / "README.md").read_bytes()
    monkeypatch.setattr(server, "RENDER_WAIT", 0.1)
    lock, release, original = threading.Lock(), threading.Event(), server.RENDER.render
    running, peak = [0], [0]

    def slow(*args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(10)
        with lock:
            running[0] -= 1
        return original(*args, **kwargs)

    monkeypatch.setattr(server.RENDER, "render", slow)
    with ThreadPoolExecutor(server.RENDER_SLOTS) as pool:
        held = [pool.submit(get, url, headers=NAVIGATE) for _ in range(server.RENDER_SLOTS)]
        deadline = time.monotonic() + 10
        while running[0] < server.RENDER_SLOTS and time.monotonic() < deadline:
            time.sleep(0.01)
        status, headers, body = get(url, headers=NAVIGATE)
        assert (status, body) == (200, raw)
        assert headers["Cache-Control"] == "no-store" and headers["Vary"] == "Sec-Fetch-Dest"
        release.set()
        for future in held:
            _, headers, body = future.result()
            assert headers["Content-Type"] == "text/html; charset=utf-8"
            assert "immutable" in headers["Cache-Control"] and b"<h1" in body
    assert peak[0] == server.RENDER_SLOTS
    assert get(url, headers=NAVIGATE)[1]["Content-Type"] == "text/html; charset=utf-8"


def test_head_never_renders(site, monkeypatch):
    """HEAD rendered the whole page only to drop it (review on #266)."""
    _, get, sha, _, work, _ = site
    calls = []
    monkeypatch.setattr(server.RENDER, "render", lambda *args, **kwargs: calls.append(args))
    status, headers, body = get(f"/alice/{sha}/docs/README.md", "HEAD", headers=NAVIGATE)
    assert (status, body, calls) == (200, b"", [])
    assert headers["Cache-Control"] == "no-store" and headers["Vary"] == "Sec-Fetch-Dest"
    size = len((work / "docs" / "README.md").read_bytes())
    assert headers["Content-Length"] == str(size)


def test_a_file_over_the_caps_is_plain_text_for_good(site, monkeypatch):
    """Unlike a passing failure, the caps are the file's own: its bytes are cached."""
    _, get, sha, _, work, _ = site
    monkeypatch.setattr(server.RENDER, "MAX_MARKDOWN_BYTES", 10)
    status, headers, body = get(f"/alice/{sha}/docs/README.md", headers=NAVIGATE)
    assert (status, body) == (200, (work / "docs" / "README.md").read_bytes())
    assert "immutable" in headers["Cache-Control"] and headers["Vary"] == "Sec-Fetch-Dest"
