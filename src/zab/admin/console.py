"""zab-console: the read-only web console (docs/ADMIN.md §4.3, §4.4).

`python -m zab.admin console` is zab-console.service: a stdlib `ThreadingHTTPServer` on
127.0.0.1:8090, behind Caddy, as its own user `zab-console`, which holds no key and reaches no
network beyond localhost (its unit's `IPAddressDeny=any`, `IPAddressAllow=localhost`; nftables
keeps it from IMDS). It reads only:
- admin.toml, for the allowlists, again whenever the file changed (checked on every request);
  a file the strict loader refuses closes the console (503) until it is fixed;
- `console/snapshot.json` and `console/audit-view.jsonl`, the collector's, scrubbed already;
- `console/tokens/<hash>`, the bot's sign-in tokens, which it deletes on use (`signin`);
- its cookie secret, `/var/lib/zab-console/secret`, which it makes at its first start;
- its two static files.

**Routes** (§4.3): `GET` `/`, `/w/<ws>/`, `/w/<ws>/vm/<slug>`, `/w/<ws>/actions`; `GET` and
`POST` `/login`; `POST` `/logout`; `GET` `/static/console.css` and `/static/login.js`. Another
method on a route answers 405, any other path 404. Every response carries `HEADERS`: the CSP,
no referrer, no store.

**Sign-in** (§4.4): `POST /login` with the link's token (`login.js` posts the fragment). The
token must be one the bot issued, unexpired, of a user the allowlists let see a workshop now;
then it is deleted, and only the request that deleted it signs in. The session is the cookie
`__Host-zab_console`: the user's ID and name and an expiry 12 hours ahead, HMAC-SHA256-signed
with the secret; HttpOnly, Secure, SameSite=Strict.

A request whose body is cut short (its deadline, or a client gone) is never processed.

**On every request** the user's ID is checked against admin.toml as it is now: `admins` see every
workshop, a workshop's `organizers` that workshop. A page of another workshop answers 404, as a
missing one does; a user who sees none any more is signed out. Signing out deletes the cookie in
the browser; a copy of it stays valid until it expires, or until the secret is replaced.

**Limits** (the review of #496): each connection has `DEADLINE` seconds, from its accept, to be
read and answered, whatever its pace; a reaper thread shuts down any that outlives it, so a
client trickling its request can't hold a thread. At most `REQUESTS` connections are served at
once, below the unit's `TasksMax=64`; one more is answered 503 at once, from the accepting
thread. A `POST` (`/login`, `/logout`) is refused unless it is same-origin: `Sec-Fetch-Site:
same-origin` when the browser sends it, else an `Origin` that is `console_url`'s, or neither
header (a non-browser client). An error answers 500, with the headers above.

It writes no log of requests: nothing a browser sends reaches the journal; a failed request is
one line, with the error's type, and a client gone mid-answer nothing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from zab.admin import pages, signin
from zab.admin.config import ROOTS, AdminConfig, AdminConfigError, Roots, load_admin_config

LOG = logging.getLogger("zab.admin.console")
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; script-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    # same-origin, not no-referrer: under no-referrer a browser's form POST carries `Origin:
    # null`, which `same_origin` refuses. The token is in the link's fragment, which no
    # Referer carries, and the pages load nothing cross-origin (the review of #496, R2-3).
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
COOKIE = "__Host-zab_console"
SESSION = 12 * 3600.0
MAX_BODY = 4096
DEADLINE = 10.0  # seconds a connection has to be read and answered (module doc)
REQUESTS = 32  # connections served at once; the unit's TasksMax is 64
#: A client gone mid-answer: nothing to log.
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
STATIC = {
    "/static/console.css": ("console.css", "text/css; charset=utf-8"),
    "/static/login.js": ("login.js", "text/javascript; charset=utf-8"),
}
_WS = r"(?P<ws>[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)"
ROUTES: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (re.compile(r"/"), "index", ("GET",)),
    (re.compile(rf"/w/{_WS}/"), "workshop", ("GET",)),
    (re.compile(rf"/w/{_WS}/vm/(?P<slug>[a-z0-9](?:[a-z0-9-]{{0,62}}[a-z0-9])?)"), "vm", ("GET",)),
    (re.compile(rf"/w/{_WS}/actions"), "actions", ("GET",)),
    (re.compile(r"/login"), "login", ("GET", "POST")),
    (re.compile(r"/logout"), "logout", ("POST",)),
    *((re.compile(re.escape(path)), "static", ("GET",)) for path in STATIC),
)


@dataclass(slots=True)
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def html(status: int, text: str, **headers: str) -> Response:
    return Response(status, text.encode(), {"Content-Type": "text/html; charset=utf-8", **headers})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_secret(path: Path) -> bytes:
    """The cookie secret: made at the first start (32 random bytes, 0600, never over a file);
    refused when it isn't a regular file of this user's that no one else can read."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32) + "\n")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError(f"{path}: not a 0600 file of this user's")
        secret = f.read(1024).strip()
    if len(secret) < 32:
        raise ValueError(f"{path}: too short")
    return secret


class _Cached:
    """A file read again only when its inode, mtime or size changed."""

    def __init__(self, path: Path, parse: Callable[[Path], Any]) -> None:
        self.path, self.parse, self.seen, self.value = path, parse, None, None
        self.lock = threading.Lock()

    def get(self) -> Any:
        try:
            st = self.path.stat()
            seen = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            seen = None
        with self.lock:
            if seen != self.seen:
                self.value, self.seen = (self.parse(self.path) if seen else None), seen
            return self.value


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _records(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _config(path: Path, roots: Roots) -> AdminConfig | None:
    try:
        return load_admin_config(path, roots=roots)
    except AdminConfigError as exc:
        LOG.warning("admin.toml refused, the console is closed: %s", exc)
        return None


class Console:
    """The console's answers, apart from the HTTP server (module doc)."""

    def __init__(
        self,
        config: Path,
        *,
        state: Path,
        secret: bytes,
        static: Path,
        clock: Callable[[], float] = time.time,
        roots: Roots = ROOTS,
    ) -> None:
        self.config = _Cached(config, lambda path: _config(path, roots))
        self.snapshot = _Cached(state / "snapshot.json", _json)
        self.records = _Cached(state / "audit-view.jsonl", _records)
        self.tokens, self.secret, self.clock = state / "tokens", secret, clock
        self.static = {route: (static / name).read_bytes() for route, (name, _) in STATIC.items()}

    # ---------------------------------------------------------------------------- session

    def cookie(self, user_id: int, name: str) -> str:
        payload = _b64(json.dumps({"u": user_id, "n": name, "e": self.clock() + SESSION}).encode())
        mac = _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())
        attributes = f"Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={int(SESSION)}"
        return f"{COOKIE}={payload}.{mac}; {attributes}"

    def session(self, header: str) -> tuple[int, str] | None:
        """The signed-in user from the Cookie header, or None for no valid session."""
        value = next(
            (part.split("=", 1)[1] for part in header.split(";")
             if part.strip().startswith(COOKIE + "=")), None
        )  # fmt: skip
        if value is None or value.count(".") != 1:
            return None
        payload, mac = value.strip().split(".")
        want = hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(mac), want):
                return None
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or type(data.get("u")) is not int:
            return None
        if not isinstance(data.get("e"), int | float) or self.clock() >= data["e"]:
            return None
        return data["u"], str(data.get("n", ""))[:60]

    @staticmethod
    def visible(config: AdminConfig, user_id: int) -> list[str]:
        """The workshops `user_id` may see now: every one for a global admin, their own for an
        organizer; none for anyone else, a deployer who is neither included."""
        if user_id in config.admins:
            return [ws.name for ws in config.workshops]
        return [ws.name for ws in config.workshops if user_id in ws.organizers]

    # ---------------------------------------------------------------------------- answers

    def respond(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> Response:
        response = self._respond(method, path.split("?", 1)[0], headers, body)
        response.headers = {**HEADERS, **response.headers}
        return response

    def _respond(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> Response:
        for pattern, name, methods in ROUTES:
            found = pattern.fullmatch(path)
            if found is None:
                continue
            if method not in methods:
                return Response(405, b"", {"Allow": ", ".join(methods)})
            if name == "static":
                return Response(200, self.static[path], {"Content-Type": STATIC[path][1]})
            if method == "POST" and not self.same_origin(headers):
                return html(403, pages.login("That request came from another site."))
            if name == "login":
                return self.login(body) if method == "POST" else html(200, pages.login())
            if name == "logout":
                return self.signed_out()
            return self.page(name, found.groupdict(), headers.get("Cookie", ""))
        return html(404, pages.message("Not found", "There is no such page."))

    def same_origin(self, headers: Mapping[str, str]) -> bool:
        """A POST from the console's own pages (module doc; the review of #496: login and
        logout CSRF). `Sec-Fetch-Site` decides when the browser sends it; else `Origin`, which
        must be `console_url`'s; a request with neither is a non-browser client's."""
        site, origin = headers.get("Sec-Fetch-Site"), headers.get("Origin")
        if site is not None:
            return site == "same-origin"
        if origin is None:
            return True
        config = self.config.get()
        url = urlsplit(config.console_url) if config and config.console_url else None
        return url is not None and origin == f"{url.scheme}://{url.netloc}"

    def signed_out(self, text: str | None = None) -> Response:
        gone = f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
        if text is not None:
            return html(403, pages.login(text), **{"Set-Cookie": gone})
        return Response(303, b"", {"Location": "/login", "Set-Cookie": gone})

    def login(self, body: bytes) -> Response:
        config = self.config.get()
        if config is None:
            return html(
                503, pages.message("Closed", "The console is closed: admin.toml is refused.")
            )
        if len(body) > MAX_BODY:
            return html(413, pages.login("That request is too large."))
        try:
            form = parse_qs(body.decode("ascii"), max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            form = {}
        token = (form.get("token") or [""])[0]
        try:
            path, user_id, name = signin.claim(self.tokens, token, self.clock())
        except signin.Refused as exc:
            return html(403, pages.login(str(exc)))
        if not self.visible(config, user_id):
            return html(403, pages.login("Your account organizes no workshop on this server."))
        try:
            if not signin.consume(path):
                return html(403, pages.login("That link was used already. Send !console again."))
        except signin.Refused as exc:
            return html(403, pages.login(str(exc)))
        return Response(303, b"", {"Location": "/", "Set-Cookie": self.cookie(user_id, name)})

    def page(self, name: str, parts: dict[str, str], cookie: str) -> Response:
        user = self.session(cookie)
        if user is None:
            return Response(303, b"", {"Location": "/login"})
        config = self.config.get()
        if config is None:
            return html(
                503, pages.message("Closed", "The console is closed: admin.toml is refused.")
            )
        names = self.visible(config, user[0])
        if not names:  # removed from every allowlist: signed out at once (§4.4 step 6)
            return self.signed_out("Your account organizes no workshop on this server any more.")
        who, now = user[1] or f"user {user[0]}", self.clock()
        snapshot = self.snapshot.get()
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if name == "index":
            return html(200, pages.index(snapshot, names, now, user=who))
        ws = parts["ws"]
        if ws not in names:  # another workshop's pages are not there for this user
            return html(404, pages.message("Not found", "There is no such page.", user=who))
        section = (snapshot.get("workshops") or {}).get(ws)
        section = section if isinstance(section, dict) else {}
        records = self.records.get() or []
        if name == "workshop":
            return html(200, pages.workshop(ws, section, records, now, user=who))
        if name == "actions":
            return html(200, pages.actions(ws, records, now, user=who))
        shown = pages.vm(ws, section, parts["slug"], now, user=who)
        if shown is None:
            return html(404, pages.message("Not found", "There is no such VM.", user=who))
        return html(200, shown)


class Handler(BaseHTTPRequestHandler):
    """The HTTP side: every method goes to `Console.respond`; nothing is logged."""

    console: Console
    timeout = 30  # seconds a connection may stay idle

    def version_string(self) -> str:
        return "zab-console"  # never Python's version

    def _answer(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            response = html(413, pages.message("Too large", "That request is too large."))
            response.headers = {**HEADERS, **response.headers}
            self.close_connection = True
        else:
            body = self.rfile.read(length) if length else b""
            if len(body) != length:  # cut at its deadline, or abandoned: never processed, so
                self.close_connection = True  # no token is spent on an answer nobody gets
                return  # (the review of #496, R2-2)
            # case-insensitive, and every Cookie header joined (the review of #496)
            headers = {"Cookie": "; ".join(self.headers.get_all("Cookie") or [])}
            for name in ("Origin", "Sec-Fetch-Site"):
                if (value := self.headers.get(name)) is not None:
                    headers[name] = value
            try:
                response = self.console.respond(self.command, self.path, headers, body)
            except Exception as exc:  # its type only (module doc)
                LOG.warning("a request failed (%s)", type(exc).__name__)
                response = html(500, pages.message("Error", "That page failed. Try again."))
                response.headers = {**HEADERS, **response.headers}
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _answer

    def send_error(self, code: int, message: str | None = None, explain: str | None = None):
        """A request the server itself refuses (malformed, an unknown method): our page and
        headers, never the library's."""
        body = pages.message(str(code), "That request can't be answered.").encode()
        self.send_response(code)
        for key, value in {**HEADERS, "Content-Type": "text/html; charset=utf-8"}.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and code >= 200 and code not in (204, 304):
            self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        """No access log (module doc)."""


BUSY = (
    b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\n"
    + b"".join(f"{k}: {v}\r\n".encode() for k, v in HEADERS.items())
    + b"Content-Length: 22\r\nConnection: close\r\n\r\nThe console is busy.\r\n"
)


class Server(ThreadingHTTPServer):
    """The limits of the module doc: `REQUESTS` connections at once, each with `DEADLINE`."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: Any, *, requests: int = REQUESTS,
                 deadline: float = DEADLINE) -> None:  # fmt: skip
        super().__init__(address, handler)
        self.slots, self.deadline = threading.BoundedSemaphore(requests), deadline
        self.open: dict[socket.socket, float] = {}
        self.open_lock, self.stopping = threading.Lock(), threading.Event()
        threading.Thread(target=self._reap, name="zab-console-reaper", daemon=True).start()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):  # full: 503 at once, no thread
            with suppress(OSError):
                request.settimeout(1.0)
                request.sendall(BUSY)
            self.shutdown_request(request)
            return
        with self.open_lock:
            self.open[request] = time.monotonic() + self.deadline
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._done(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._done(request)

    def _done(self, request: Any) -> None:
        with self.open_lock:
            if self.open.pop(request, None) is not None:
                self.slots.release()

    def shutdown_request(self, request: Any) -> None:
        """Shut down and close a connection, and free its slot, under `open_lock`: the reaper
        shuts down under the same lock, so it never reaches a descriptor already closed and
        perhaps reused (the review of #496, R2-4)."""
        with self.open_lock:
            if self.open.pop(request, None) is not None:
                self.slots.release()
            super().shutdown_request(request)

    def _reap(self) -> None:
        """Shut down each connection past its deadline: its thread's next read or write
        fails, and the thread ends."""
        while not self.stopping.wait(0.25):
            now = time.monotonic()
            with self.open_lock:
                for request, deadline in self.open.items():
                    if now > deadline:
                        with suppress(OSError):
                            request.shutdown(socket.SHUT_RDWR)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """One line with the error's type, never a traceback; nothing for a client gone."""
        error = sys.exc_info()[1]
        if not isinstance(error, GONE):
            LOG.warning("a connection failed (%s)", type(error).__name__)

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


def serve(console: Console, host: str, port: int, **limits: Any) -> Server:
    handler = type("ConsoleHandler", (Handler,), {"console": console})
    return Server((host, port), handler, **limits)


def main(argv: list[str] | None = None) -> int:
    """`python -m zab.admin console --config PATH`: serve until stopped."""
    parser = argparse.ArgumentParser(prog="python -m zab.admin console")
    parser.add_argument("--config", default="/etc/zab-admin/admin.toml")
    parser.add_argument("--listen", default="127.0.0.1:8090", help="HOST:PORT, on loopback")
    parser.add_argument("--state", default="/var/lib/zab-admin/console")
    default_secret = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/zab-console"), "secret")
    parser.add_argument("--secret", type=Path, default=default_secret)
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    host, _, port = options.listen.rpartition(":")
    if host != "127.0.0.1" or not port.isdigit():  # IPv4: ThreadingHTTPServer's family
        print("zab-console: --listen is 127.0.0.1:PORT", file=sys.stderr)
        return 2
    try:
        secret = read_secret(options.secret)
    except (OSError, ValueError) as exc:
        print(f"zab-console: no cookie secret ({exc})", file=sys.stderr)
        return 1
    static = Path(__file__).parent / "static"
    console = Console(Path(options.config), state=Path(options.state), secret=secret, static=static)
    server = serve(console, host, int(port))
    LOG.info("listening on %s", options.listen)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0
