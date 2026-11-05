"""The console (docs/ADMIN.md §4.3, §4.4; `zab.admin.console`, `zab.admin.signin`): the routes and
methods, the headers, the one-time sign-in and the session, and the access rules: an organizer,
a global admin, a stranger, a removed organizer; an expired, used or forged token; a forged or
tampered cookie."""

import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from zab.admin import console
from zab.admin.console import CSP, Console, read_secret
from zab.admin.signin import SignIns

from .botkit import ALICE, DEE, DREW, OLGA, PAT, TESS, make_site

GOLDEN = Path(__file__).parent / "golden" / "collector-snapshot.json"
STATIC = Path(console.__file__).parent / "static"
SECRET = b"s" * 64
ROUTES = ("/", "/w/harvard-2026/", "/w/harvard-2026/vm/alice", "/w/harvard-2026/actions")


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0 + 60

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def site(tmp_path):
    site = make_site(tmp_path)
    state = Path(site.paths.state) / "console"
    (state / "tokens").mkdir(parents=True)
    shutil.copy(GOLDEN, state / "snapshot.json")
    record = {"seq": 1, "ts": "2027-01-15T07:00:00Z", "event": "apply_end", "plan": "P-1",
              "workshop": "zab-test", "verb": "stop", "slugs": ["erin"], "people": []}  # fmt: skip
    (state / "audit-view.jsonl").write_text(json.dumps(record) + "\n")
    return site


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(site, clock):
    state = Path(site.paths.state) / "console"
    return Console(
        site.config_path, state=state, secret=SECRET, static=STATIC, clock=clock, roots=site.roots
    )


def tokens(site) -> Path:
    return Path(site.paths.state) / "console" / "tokens"


def issue(site, clock, user_id, name="Someone"):
    """A link as the bot makes it (`SignIns.issue`), and its token."""
    signins = SignIns(tokens(site), "https://admin.example.org", clock=clock)
    link = signins.issue(user_id, name, "harvard-2026", 901)
    return link.rsplit("#", 1)[1]


def post_login(app, token):
    return app.respond("POST", "/login", {}, f"token={token}".encode())


def sign_in(app, site, clock, user_id, name="Someone") -> str:
    """The Cookie header a browser would send after signing in."""
    response = post_login(app, issue(site, clock, user_id, name))
    assert response.status == 303 and response.headers["Location"] == "/", response.body
    return response.headers["Set-Cookie"].split(";", 1)[0]


def get(app, path, cookie=""):
    return app.respond("GET", path, {"Cookie": cookie} if cookie else {}, b"")


# ------------------------------------------------------------------------- routes, headers


@pytest.mark.parametrize("path", ROUTES)
def test_no_cookie_goes_to_the_sign_in_page(app, path):
    response = get(app, path)
    assert (response.status, response.headers["Location"]) == (303, "/login")


def test_only_the_listed_routes_and_methods(app, site, clock):
    cookie = sign_in(app, site, clock, DREW)
    for method, path, status in [
        ("POST", "/", 405), ("PUT", "/login", 405), ("DELETE", "/w/harvard-2026/", 405),
        ("HEAD", "/", 405), ("GET", "/logout", 405), ("POST", "/static/login.js", 405),
        ("GET", "/nope", 404), ("GET", "/w/harvard-2026", 404), ("GET", "/static/x.js", 404),
        ("GET", "/w/harvard-2026/vm/../actions", 404), ("GET", "/w/Harvard-2026/", 404),
        ("GET", "/login/", 404), ("GET", "//", 404), ("GET", "/w/harvard-2026/vm/a%2Fb", 404),
    ]:  # fmt: skip
        response = app.respond(method, path, {"Cookie": cookie}, b"")
        assert response.status == status, (method, path, response.status)
    assert app.respond("POST", "/", {}, b"").headers["Allow"] == "GET"
    assert app.respond("PUT", "/login", {}, b"").headers["Allow"] == "GET, POST"


def test_every_response_carries_the_policy(app, site, clock):
    cookie = sign_in(app, site, clock, DREW)
    answers = [get(app, path, cookie) for path in ROUTES]
    answers += [get(app, path) for path in (*ROUTES, "/login", "/nope", *console.STATIC)]
    answers += [app.respond("POST", "/", {}, b""), post_login(app, "0" * 64)]
    answers += [app.respond("POST", "/logout", {}, b"")]
    for response in answers:
        assert response.headers["Content-Security-Policy"] == CSP
        assert response.headers["Referrer-Policy"] == "same-origin"  # R2-3
        assert response.headers["Cache-Control"] == "no-store"
    assert CSP == (
        "default-src 'none'; style-src 'self'; img-src 'self'; script-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )


def test_the_static_files(app):
    css, js = (get(app, path) for path in ("/static/console.css", "/static/login.js"))
    assert css.headers["Content-Type"] == "text/css; charset=utf-8" and b".bar" in css.body
    assert js.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert b"history.replaceState" in js.body and b"fetch" not in js.body


# ---------------------------------------------------------------------------------- sign-in


def test_a_token_file_is_0640_under_umask_0077_and_holds_only_its_hash(site, clock):
    old = os.umask(0o077)
    try:
        token = issue(site, clock, OLGA, "Olga Organizer")
    finally:
        os.umask(old)
    files = list(tokens(site).iterdir())
    assert [f.name for f in files] == [hashlib.sha256(token.encode()).hexdigest()]
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o640
    assert json.loads(files[0].read_text()) == {
        "user_id": OLGA, "name": "Olga Organizer", "expires": clock.now + 300,
    }  # fmt: skip
    assert token not in files[0].read_text() and len(token) == 64


def test_sign_in_once(app, site, clock):
    token = issue(site, clock, OLGA, "Olga Organizer")
    first = post_login(app, token)
    assert first.status == 303 and first.headers["Location"] == "/"
    cookie = first.headers["Set-Cookie"]
    assert cookie.startswith("__Host-zab_console=")
    assert "; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=43200" in cookie
    assert list(tokens(site).iterdir()) == []  # used: deleted
    again = post_login(app, token)
    assert again.status == 403 and b"used already" in again.body
    assert "Set-Cookie" not in again.headers


def test_an_expired_unknown_or_malformed_token_is_refused(app, site, clock):
    token = issue(site, clock, OLGA)
    clock.now += 301
    expired = post_login(app, token)
    assert expired.status == 403 and b"expired" in expired.body
    assert len(list(tokens(site).iterdir())) == 1  # the bot deletes it, not the console
    for bad in ("0" * 64, "zz", "", token.upper(), token + "0", f"{token}&token=x"):
        response = post_login(app, bad)
        assert response.status == 403 and "Set-Cookie" not in response.headers, bad
    assert app.respond("POST", "/login", {}, b"\xff\xfe").status == 403
    assert app.respond("POST", "/login", {}, b"token=" + b"a" * 5000).status == 413


def test_a_forged_token_file_is_refused(app, site, clock):
    token = "1" * 64
    path = tokens(site) / hashlib.sha256(token.encode()).hexdigest()
    for text in ("not json", json.dumps({"user_id": "101", "name": "x", "expires": 2e9}),
                 json.dumps({"user_id": DREW, "name": "x"})):  # fmt: skip
        path.write_text(text)
        assert post_login(app, token).status == 403
    path.unlink()
    os.symlink(tokens(site).parent / "snapshot.json", path)
    assert post_login(app, token).status == 403  # never followed


def test_a_stranger_or_a_deployer_is_not_signed_in(app, site, clock):
    for user in (PAT, DEE):  # DEE deploys, and organizes nothing (botkit)
        token = issue(site, clock, user)
        response = post_login(app, token)
        assert response.status == 403 and b"organizes no workshop" in response.body
        assert "Set-Cookie" not in response.headers


# ------------------------------------------------------------------------- what each sees


def test_an_organizer_sees_only_their_workshop(app, site, clock):
    cookie = sign_in(app, site, clock, OLGA, "Olga Organizer")
    index = get(app, "/", cookie)
    assert index.status == 200 and b"/w/harvard-2026/" in index.body
    assert b"zab-test" not in index.body and b"Olga Organizer" in index.body
    for path in ROUTES[1:]:
        assert get(app, path, cookie).status == 200, path
    for path in ("/w/zab-test/", "/w/zab-test/vm/erin", "/w/zab-test/actions", "/w/nowhere/"):
        assert get(app, path, cookie).status == 404, path  # as for a missing workshop
    assert get(app, "/w/harvard-2026/vm/nobody", cookie).status == 404
    tess = sign_in(app, site, clock, TESS)
    assert get(app, "/w/zab-test/vm/erin", tess).status == 200
    assert get(app, "/w/harvard-2026/", tess).status == 404


def test_a_global_admin_sees_every_workshop(app, site, clock):
    cookie = sign_in(app, site, clock, ALICE, "Alice Admin")
    index = get(app, "/", cookie)
    assert b"/w/harvard-2026/" in index.body and b"/w/zab-test/" in index.body
    actions = get(app, "/w/zab-test/actions", cookie)
    assert actions.status == 200 and b"<td>P-1</td>" in actions.body
    page = get(app, "/w/harvard-2026/", cookie)
    assert b"alice" in page.body and b'http-equiv="refresh"' in page.body


def test_a_removed_organizer_loses_the_session_at_once(app, site, clock):
    cookie = sign_in(app, site, clock, OLGA)
    assert get(app, "/w/harvard-2026/", cookie).status == 200
    text = site.config_path.read_text()
    assert "organizers = [103, 104]" in text
    site.config_path.write_text(text.replace("organizers = [103, 104]", "organizers = [104]"))
    for path in ROUTES:
        response = get(app, path, cookie)
        assert response.status == 403 and b"organizes no workshop" in response.body
        assert response.headers["Set-Cookie"].startswith("__Host-zab_console=; ")
        assert "Max-Age=0" in response.headers["Set-Cookie"]


def test_an_organizer_moved_to_another_workshop_sees_that_one(app, site, clock):
    cookie = sign_in(app, site, clock, OLGA)
    text = site.config_path.read_text().replace("organizers = [103, 104]", "organizers = [104]")
    site.config_path.write_text(
        text.replace(f"organizers = [{TESS}]", f"organizers = [{TESS}, 103]")
    )
    assert get(app, "/w/harvard-2026/", cookie).status == 404
    assert get(app, "/w/zab-test/", cookie).status == 200


def test_a_refused_admin_toml_closes_the_console(app, site, clock):
    cookie = sign_in(app, site, clock, DREW)
    site.config_path.write_text("provider = 'gcp'\n")
    assert get(app, "/", cookie).status == 503
    assert post_login(app, issue(site, clock, DREW)).status == 503


# ---------------------------------------------------------------------------------- cookies


def forge(payload: dict, secret: bytes = SECRET) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    mac = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    return f"__Host-zab_console={body}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"


def test_forged_and_tampered_cookies_are_refused(app, site, clock):
    cookie = sign_in(app, site, clock, OLGA)
    assert get(app, "/", cookie).status == 200
    body, mac = cookie.split("=", 1)[1].split(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert data["u"] == OLGA and data["e"] == clock.now + 12 * 3600
    as_drew = base64.urlsafe_b64encode(json.dumps({**data, "u": DREW}).encode()).decode()
    for bad in (
        f"__Host-zab_console={as_drew.rstrip('=')}.{mac}",  # another user, the same HMAC tag
        forge({**data, "u": DREW}, secret=b"t" * 64),  # signed with another secret
        f"__Host-zab_console={body}.{mac[:-2]}AA",
        f"__Host-zab_console={body}", "__Host-zab_console=", "zab_console=" + cookie[19:],
        forge({"u": "101", "e": data["e"]}), forge({"u": DREW}), forge([DREW]),
        "__Host-zab_console=%%%.%%%",
    ):  # fmt: skip
        assert get(app, "/", bad).status == 303, bad
    assert get(app, "/", forge({**data, "u": DREW})).status == 200  # the secret is the key
    clock.now += 12 * 3600
    assert get(app, "/", cookie).status == 303  # expired


def test_sign_out(app, site, clock):
    response = app.respond("POST", "/logout", {}, b"")
    assert (response.status, response.headers["Location"]) == (303, "/login")
    assert response.headers["Set-Cookie"].startswith("__Host-zab_console=; Path=/; Secure")


def test_the_secret_is_made_once_and_kept_private(tmp_path):
    path = tmp_path / "secret"
    first = read_secret(path)
    assert len(first) == 64 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_secret(path) == first
    path.chmod(0o640)
    with pytest.raises(ValueError, match="0600"):
        read_secret(path)
    path.chmod(0o600)
    path.write_text("short\n")
    with pytest.raises(ValueError, match="short"):
        read_secret(path)
    os.symlink(path, tmp_path / "link")
    with pytest.raises(OSError):
        read_secret(tmp_path / "link")


# ----------------------------------------------------------------------- the real server


def test_the_http_server(app, site, clock):
    server = console.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def ask(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        answer = connection.getresponse()
        return answer.status, dict(answer.getheaders()), answer.read()

    try:
        token = issue(site, clock, OLGA)
        status, headers, _ = ask("POST", "/login", f"token={token}",
                                 {"Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
        assert status == 303 and headers["Content-Security-Policy"] == CSP
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, body = ask("GET", "/w/harvard-2026/", headers={"Cookie": cookie})
        assert status == 200 and b"harvard-2026" in body and headers["Server"] == "zab-console"
        assert ask("POST", "/")[0] == 405
        assert ask("BREW", "/")[0] == 501  # the library's refusal, with our headers
        status, headers, _ = ask("BREW", "/")
        assert headers["Content-Security-Policy"] == CSP
        too_big = ask("POST", "/login", "x", {"Content-Length": "999999"})
        assert too_big[0] == 413
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(
    os.environ.get("ZAB_FIREFOX") != "1" or shutil.which("firefox") is None,
    reason="headless Firefox: ZAB_FIREFOX=1 where Firefox is installed",
)
def test_the_sign_in_completes_in_headless_firefox_under_the_policy(app, site, clock):
    """§4.4 step 4, in a browser that enforces §4.3's policy: login.js, the page's only script,
    posts the link's fragment, and the console signs the user in (it deletes the token)."""
    clock.now = time.time()
    server = console.serve(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = issue(site, clock, OLGA)
    url = f"http://127.0.0.1:{server.server_address[1]}/login#{token}"
    with tempfile.TemporaryDirectory() as profile:
        firefox = subprocess.Popen(  # noqa: S603 (a test's own browser)
            [shutil.which("firefox") or "firefox", "--headless", "--no-remote", "--profile",
             profile, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        try:
            for _ in range(600):
                if not any(tokens(site).iterdir()):
                    break
                time.sleep(0.1)
            assert not any(tokens(site).iterdir()), "the browser never posted the token"
        finally:
            firefox.terminate()
            firefox.wait(timeout=30)
            server.shutdown()
            server.server_close()


# ------------------------------------------------------------------ the review of #496


def running(app, **limits):
    server = console.serve(app, "127.0.0.1", 0, **limits)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def ask_raw(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    answer = connection.getresponse()
    return answer.status, dict(answer.getheaders()), answer.read()


def trickle(port):
    """A client that sends a request's head and one byte of its 4096-byte body, then waits."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(b"POST /login HTTP/1.0\r\nContent-Length: 4096\r\n\r\nt")
    return sock


def cut(sock) -> bool:
    """Whether the server ended the connection: end of file, or a reset, within 5 s; any data
    left is its answer to a truncated request, read through to the end."""
    sock.settimeout(5)
    try:
        while sock.recv(65536):
            pass
    except TimeoutError:
        return False
    except OSError:
        return True
    return True


def test_a_slow_client_is_cut_at_the_deadline_and_others_still_answer(app):
    """(1) Each connection has DEADLINE to be read and answered, whatever its pace; when every
    slot is taken, one more is answered 503 at once; fresh requests answer meanwhile.

    A slot comes back when its thread ends, a moment after its client has seen the connection
    end, so the test asks until a request answers 200, for up to 10 s, and asks nothing after
    it (#643). It used to stop at the first 200 and ask once more at once: that request could
    find the slot of the one before still held, by a thread that had sent its answer but not
    yet ended, while the other cut client's thread had not ended either, and read 503."""
    server, port = running(app, requests=2, deadline=1.0)
    try:
        slow = [trickle(port), trickle(port)]
        time.sleep(0.2)
        status, headers, _ = ask_raw(port, "GET", "/login")  # both slots taken
        assert status == 503 and headers["Content-Security-Policy"] == CSP
        started = time.monotonic()
        for sock in slow:
            assert cut(sock)  # the server ended it at its deadline
        assert time.monotonic() - started < 3
        # The slots come back once the threads end: well within 10 s, a third of the handler's
        # idle timeout, so a slot held until then still fails.
        statuses = [ask_raw(port, "GET", "/login")[0]]
        until = time.monotonic() + 10
        while statuses[-1] != 200 and time.monotonic() < until:
            time.sleep(0.05)
            statuses.append(ask_raw(port, "GET", "/login")[0])
        assert statuses[-1] == 200, statuses
        for sock in slow:
            sock.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_trickling_client_holds_no_slot_past_the_deadline(app):
    server, port = running(app, requests=1, deadline=0.5)
    try:
        sock = trickle(port)
        for _ in range(10):  # a byte every 0.2 s: never idle, past the deadline anyway
            time.sleep(0.2)
            try:
                sock.sendall(b"x")
            except OSError:
                break
        sock.close()
        time.sleep(0.3)
        assert ask_raw(port, "GET", "/login")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def with_console_url(site, url="https://admin.example.org"):
    text = site.config_path.read_text()
    site.config_path.write_text(
        text.replace(
            "plan_ttl_minutes = 10\n", f'plan_ttl_minutes = 10\nconsole_url = "{url}"\n', 1
        )
    )


@pytest.mark.parametrize("headers, ok", [
    ({}, True),  # a non-browser client
    ({"Sec-Fetch-Site": "same-origin"}, True),
    ({"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:8090"}, True),
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "same-site", "Origin": "https://admin.example.org"}, False),
    ({"Origin": "https://admin.example.org"}, True),
    ({"Origin": "https://evil.example"}, False),
    ({"Origin": "null"}, False),
])  # fmt: skip
def test_a_post_must_be_same_origin(app, site, clock, headers, ok):
    """(2) Login and logout CSRF: a cross-site POST is refused, whatever it carries."""
    with_console_url(site)
    response = app.respond("POST", "/login", headers, f"token={issue(site, clock, OLGA)}".encode())
    assert (response.status == 303) == ok, response.body[:200]
    if not ok:
        assert b"another site" in response.body and "Set-Cookie" not in response.headers
    logout = app.respond("POST", "/logout", headers, b"")
    assert (logout.status == 303) == ok


def test_without_a_console_url_an_origin_is_refused(app, site, clock):
    response = app.respond("POST", "/login", {"Origin": "https://admin.example.org"},
                           f"token={issue(site, clock, OLGA)}".encode())  # fmt: skip
    assert response.status == 403


def test_an_error_answers_500_with_the_headers_and_one_line(app, monkeypatch, caplog):
    """(3) Anything unexpected answers 500 with HEADERS; the log has one line, its type."""

    def broken(*args):
        raise ValueError("a secret-looking detail")

    server, port = running(app)
    try:
        monkeypatch.setattr(app, "respond", broken)
        with caplog.at_level(logging.WARNING, logger="zab.admin.console"):
            status, headers, _ = ask_raw(port, "GET", "/")
        assert status == 500 and headers["Content-Security-Policy"] == CSP
        assert headers["Cache-Control"] == "no-store"
        assert [r.getMessage() for r in caplog.records] == ["a request failed (ValueError)"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_client_gone_logs_nothing(app, caplog):
    server = console.serve(app, "127.0.0.1", 0)
    try:
        with caplog.at_level(logging.WARNING, logger="zab.admin.console"):
            for error in (BrokenPipeError(), ConnectionResetError(), OSError(5, "x")):
                try:
                    raise error
                except OSError:
                    server.handle_error(None, None)
        assert [r.getMessage() for r in caplog.records] == ["a connection failed (OSError)"]
    finally:
        server.server_close()


def test_a_token_that_cant_be_deleted_is_refused(app, site, clock):
    """(3) `consume` on a directory it can't write: a refusal, never an exception."""
    token = issue(site, clock, OLGA)
    tokens(site).chmod(0o500)
    try:
        if os.access(tokens(site), os.W_OK):  # root ignores the mode
            pytest.skip("running as root")
        response = post_login(app, token)
        assert response.status == 403 and b"failed on the server" in response.body
    finally:
        tokens(site).chmod(0o700)


def test_cookies_case_insensitive_and_joined(app, site, clock):
    """(4) A lowercase `cookie` header, and the session cookie in a second Cookie header."""
    cookie = sign_in(app, site, clock, OLGA)
    server, port = running(app)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest("GET", "/w/harvard-2026/")
        connection.putheader("cookie", "other=1")
        connection.putheader("Cookie", cookie)
        connection.endheaders()
        assert connection.getresponse().status == 200
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("listen", ["::1:8090", "localhost:8090", "0.0.0.0:8090", "8090"])
def test_listen_is_ipv4_loopback_only(tmp_path, listen):
    """(6) ThreadingHTTPServer is IPv4: only 127.0.0.1 is taken."""
    assert console.main(["--listen", listen, "--secret", str(tmp_path / "s")]) == 2


def test_a_cut_request_is_never_processed(app, site, clock):
    """The review of #496, R2-2: a body cut short at its deadline never reaches `respond`, so
    no token is spent on an answer nobody gets; the link still signs in after."""
    clock.now = time.time()
    token = issue(site, clock, OLGA)
    server, port = running(app, deadline=0.5)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(b"POST /login HTTP/1.0\r\nContent-Length: 4096\r\n\r\ntoken=" +
                     token.encode())  # fmt: skip
        assert cut(sock)
        sock.close()
        assert len(list(tokens(site).iterdir())) == 1  # not spent
        status, headers, _ = ask_raw(port, "POST", "/login", body=f"token={token}")
        assert status == 303 and headers["Set-Cookie"].startswith("__Host-zab_console=")
    finally:
        server.shutdown()
        server.server_close()


def test_the_link_carries_the_token_in_its_fragment(site, clock):
    """R2-3 rests on it: no Referer carries a fragment."""
    signins = SignIns(tokens(site), "https://admin.example.org/x", clock=clock)
    link = signins.issue(OLGA, "Olga", "harvard-2026", 901)
    assert link.startswith("https://admin.example.org/login#") and "?" not in link
