#!/usr/bin/env python3
"""Central, read-only turn snapshots and share links (SPEC §10.7, §11.4; PM #35, Mon 07:10;
#710).

Git tree parsing and MIME handling adapted from refserve (MIT; notice in
workshop/files/LICENSE), via #135. HTTPS belongs to Caddy.
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import html
import http.client
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from zab.workspace.denylist import upload_denied_reason

SLUG = re.compile(r"(?!\.{1,2}$)[a-zA-Z0-9_.-]{1,100}")
SHA = re.compile(r"[0-9a-fA-F]{40}")
REFSPEC = "+refs/zab/turns/*:refs/zab/turns/*"
#: Share links (#710): `zab share` pushes `refs/zab/shares/<name>`; a mirror fetches them with
#: the turns, and the share store keeps each share apart (`Store.adopt`).
SHARE_REFSPEC = "+refs/zab/shares/*:refs/zab/shares/*"
SHARE_REFS = "refs/zab/shares/"
SHARE_REF = re.compile(r"refs/zab/shares/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
#: A store's files beside its refs, which name each share by its commit (Codex round 1 on
#: #740: a repository made again under the name may reuse a share's name): each share's
#: repository ID when it was taken, its names on GitHub, and the shares revoked, which are
#: never taken again.
SHARE_IDS, SHARE_NAMES, REVOKED = "zab-share-ids", "zab-share-names", "zab-revoked"
#: A store's lock between processes (`Store.store_lock`): the service's adoption and a
#: deployer's `server.py unshare`. Not `*.lock`, which the stale-lock sweep removes.
STORE_LOCK = "zab-store-flock"
#: `POST /_zab/recheck/<repo>/<sha>` looks a share up on GitHub at once (#710), so that
#: `zab share --revoke` takes effect now: each share at most once every `RECHECK_EVERY` seconds,
#: and at most `RECHECKS_PER_HOUR` in all, so that a link's holder can't spend the token's quota.
RECHECK_PREFIX = "/_zab/recheck/"
RECHECK_EVERY = 10.0
RECHECKS_PER_HOUR = 120
MAX_BLOB = 32 * 1024 * 1024
#: The GitHub organization whose repositories are mirrored (#693): `--org`, by default
#: `$ZAB_FILES_ORG`, which the unit reads from `ORG_FILE` (README §3). There is no built-in one,
#: and the service doesn't start without it. Letters, digits and inner hyphens, as GitHub's.
ORG_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", re.ASCII)
ORG_ENV = "ZAB_FILES_ORG"
ORG_FILE = "/etc/zab-files/zab-files.env"
#: The service's exit status without an organization, or with a name GitHub can't have: sysexits'
#: EX_CONFIG, which the unit's `RestartPreventExitStatus` names, so that systemd doesn't restart
#: it in a loop; not 2, which Python and argparse use for errors of their own (review of #770).
EX_CONFIG = 78
API = "https://api.github.com"
#: Every mirror's repository is looked up on GitHub this often, the first time `CHECK_DELAY`
#: after a start (#714): a request for a cached snapshot never fetches, so without the check a
#: deleted or transferred repository's mirror would be served for good.
CHECK_INTERVAL = 3600.0
CHECK_DELAY = 300.0
#: One check removes at most this many mirrors as gone, or a quarter of them if that is more:
#: more at once looks like a token that lost access to them, and none is removed (#714).
GONE_AT_ONCE = 3
#: The organization's listing is read up to this many pages of 100 repositories (#714).
LISTING_PAGES = 50
#: The repository's GitHub ID, written into its mirror by the first check (#714): another ID
#: under the same name is a new repository, so the one mirrored is gone.
ID_FILE = "zab-github-id"
EXISTS, MISSING, UNKNOWN = "exists", "missing", "unknown"


def allowed(path: str) -> bool:
    """Check components and apply filename rules only to the full path."""
    parts = path.split("/") if path else []
    return (
        all(
            part not in ("", ".", "..") and not re.search(r"[\x00-\x1f\x7f\\]", part)
            for part in parts
        )
        and upload_denied_reason(path) is None
    )


@dataclass(frozen=True)
class Entry:
    mode: str
    kind: str
    sha: str
    size: int
    name: str


def entries(data: bytes) -> list[Entry]:
    result = []
    for record in data.split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, kind, sha, size = meta.decode("ascii").split()
        # Undecodable names cannot be represented faithfully in a UTF-8 URL.
        try:
            decoded = name.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result.append(Entry(mode, kind, sha, 0 if size == "-" else int(size), decoded))
    return result


MIME_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".svg": "image/svg+xml",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".wasm": "application/wasm",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mov": "video/quicktime",
    ".bmp": "image/bmp",
    ".webm": "video/webm",
    ".ico": "image/x-icon",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}
SANDBOX = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads"
)


#: Rendered pages' assets (render.py): the second segment is never a commit SHA, so this can't
#: shadow a snapshot URL, whatever the repository is called.
STATIC_PREFIX = "/_zab/static/"
#: A browser navigating to the file gets the rendered page; scripts, stylesheets, `fetch()`,
#: curl and downloads by script get the bytes (issue #256).
PAGE_DESTINATIONS = ("document", "iframe")
#: Renders at once. One can take 1-2 s and 100 MB, beside the eight blob slots under the
#: unit's MemoryMax=1G, and two only share the GIL (review on #266). A navigation waits up to
#: RENDER_WAIT seconds for the slot, holding its blob slot, then gets the bytes, uncached, so
#: that a reload can still get the page. Most renders take milliseconds.
RENDER_SLOTS = 1
RENDER_WAIT = 0.5


def _load_render():
    """render.py beside this file. Without it (or its libraries) files stay plain text.
    Loading it warms it up, so the first burst after a restart can't race Pygments' lazy
    imports (review on #265)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "zab_files_render", Path(__file__).resolve().parent / "render.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        logging.getLogger("zab.files").warning("rendering off: %s", type(exc).__name__)
        return None


RENDER = _load_render()


def content_type(name: str, data: bytes) -> str:
    kind = MIME_TYPES.get(Path(name).suffix.lower())
    if not kind:
        try:
            # Incremental validation handles multibyte boundaries without ever
            # allocating a decoded copy of the full blob.
            decoder = codecs.getincrementaldecoder("utf-8")()
            for offset in range(0, len(data), 8192):
                decoder.decode(data[offset : offset + 8192])
            decoder.decode(b"", final=True)
            kind = "text/plain"
        except UnicodeDecodeError:
            kind = "application/octet-stream"
    return kind + ("; charset=utf-8" if kind.startswith("text/") else "")


class GitError(OSError):
    def __init__(self, code: int, stderr: bytes) -> None:
        super().__init__("git operation failed")
        self.code = code
        first = stderr.decode("utf-8", "replace").splitlines()
        text = first[0] if first else "no stderr"
        text = re.sub(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[redacted]", text)
        text = re.sub(r"https?://[^/\s]*@", "https://[redacted]@", text)
        text = re.sub(r"[a-fA-F0-9]{40,64}", "[sha]", text)
        self.detail = "".join(c if c.isprintable() else " " for c in text)[:500]


class FetchBusy(OSError):
    """Repository admission exhausted; ask clients to retry instead of claiming absence."""


@dataclass
class PendingTip:
    done: threading.Event = field(default_factory=threading.Event)
    result: Path | None = None  # where the tip is served from: a mirror, or the share store
    busy: bool = False


@dataclass
class FetchGate:
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict[str, PendingTip] = field(default_factory=dict)
    started: float = 0.0
    #: When the last fetch that succeeded began: a share is taken from the mirror only after one
    #: that began after its request (Codex round 2 on #740), never from a mirror a failed or
    #: deferred fetch left as it was.
    succeeded: float = 0.0


def read_token(directory: str | None = None) -> str:
    """The service's GitHub token, checked as askpass.py checks it; "" when there is none."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "") if directory is None else directory
    if not directory:
        return ""
    path = Path(directory) / "github-token"
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o007:
            return ""
        token = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return "" if not token or any(char.isspace() for char in token) else token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # `GitHubAPI.get` follows one itself, and only within the API


class GitHubAPI:
    """Read-only REST calls with the service's token, for the check of #714. A redirect, which
    GitHub answers for a renamed or transferred repository, is followed once, and only to
    `<API>/repositories/<id>`. The token goes in a header to the API alone; nothing logs it."""

    def __init__(self, token: str, *, base: str = API, timeout: float = 20.0) -> None:
        self._token, self.base, self.timeout = token, base, timeout
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._moved = re.compile(re.escape(base) + r"/repositories/[0-9]{1,20}(?:/[^\s#]*)?")

    def get(self, path: str) -> tuple[int, Any]:
        """(HTTP status, the JSON body or None). Raises OSError (a timeout included),
        `http.client.HTTPException` or ValueError (a body that isn't JSON) for no answer."""
        status, body, location = self._open(self.base + path)
        if status in (301, 302, 307, 308) and self._moved.fullmatch(location):
            status, body, _ = self._open(location)  # a second redirect stays a redirect
        return status, body

    def _open(self, url: str) -> tuple[int, Any, str]:
        request = urllib.request.Request(  # noqa: S310 - the API's https base, or a test's
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "zab-files",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read(8 << 20) or b"null"), ""
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, None, exc.headers.get("Location", "")


class Store:
    """Disposable bare mirrors of the turn and share refs, never checked out; and the share
    store (#710), bare repositories that keep each share apart from its mirror, so that a
    mirror removed (#714) or rebuilt never takes a share with it."""

    #: Seconds `store_lock` waits for another process (a deployer's `unshare`) to finish.
    store_wait = 35.0

    def __init__(
        self,
        cache: Path,
        *,
        shares: Path | None = None,
        org: str = "",
        test_remotes: Path | None = None,
    ) -> None:
        """`org`: the GitHub organization whose repositories are mirrored (#693); "" for a
        deployer's command (`purge`, `shares`, `unshare`), which never asks GitHub."""
        if org and not ORG_NAME.fullmatch(org):
            raise ValueError(f"not a GitHub organization: {org!r}")
        self.org = org
        # Absolute: git runs with `-C <repository>`, and the share store fetches from a mirror
        # by its path, so a relative `--cache` would be read from inside the store (Codex on
        # #740).
        self.cache = cache = Path(cache).absolute()
        cache.mkdir(parents=True, exist_ok=True)
        shares = Path(shares).absolute() if shares is not None else None
        self.shares = shares if shares is not None else cache.with_name(cache.name + "-shares")
        for trash in cache.glob("*.git.gone"):  # a removal cut short (`remove`)
            shutil.rmtree(trash, ignore_errors=True)
        # Test injection is deliberately absent from the production CLI.
        self.test_remotes = test_remotes
        self.gates: dict[str, FetchGate] = {}
        self.gates_lock = threading.Lock()
        self.pending_tips = 0
        self.last_fetch: dict[str, float] = {}
        self.failed_fetch: set[str] = set()
        self.fetch_capacity = threading.BoundedSemaphore(2)
        self.fetch_interval = 5.0
        self.negative_interval = 60.0
        self.fetch_window = 60.0
        self.fetch_rate_limit = 60  # New mirrors only; room for the initial workshop burst.
        self.fetch_times: deque[float] = deque()
        self.history_lock = threading.Lock()
        self.max_fetch_history = 256
        self.rechecked: dict[tuple[str, str], float] = {}
        self.recheck_times: deque[float] = deque()
        self.recheck_lock = threading.Lock()
        self.meta_lock = threading.Lock()  # each store's ID and name files: read, then written
        #: The API client of the share checks: one with the token read afresh, or None
        #: without a token. Tests replace it.
        self.api: Any = lambda: GitHubAPI(token) if (token := read_token()) else None
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": str(Path(__file__).with_name("askpass.py")),
            "CREDENTIALS_DIRECTORY": os.environ.get("CREDENTIALS_DIRECTORY", ""),
        }

    def git(self, repo: Path, *args: str, local: bool = False) -> bytes:
        """`local`: the file protocol is allowed, for a share's copy from its own mirror."""
        proc = subprocess.Popen(  # noqa: S603 — argument lists, validated repo/path
            [
                "/usr/bin/git",
                "--literal-pathspecs",
                "-C",
                str(repo),
                "-c",
                "credential.helper=",
                "-c",
                "gc.autoDetach=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "http.followRedirects=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                f"protocol.file.allow={'always' if self.test_remotes or local else 'never'}",
                *args,
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            # Git installs SIGTERM handlers that clean its lock files. Signal
            # the whole process group so HTTPS/GC children cannot outlive it.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise
        if proc.returncode:
            raise GitError(proc.returncode, stderr)
        return stdout

    def fetch(self, slug: str, repo: Path, gate: FetchGate | None = None) -> None:
        # Caller holds the normalized repository's own gate. Waiting for cooldown
        # or the fetch pool never consumes a response/body slot.
        now = time.monotonic()
        created = not repo.exists()
        with self.history_lock:
            for name, expiry in list(self.last_fetch.items()):
                if now >= expiry:
                    del self.last_fetch[name]
                    self.failed_fetch.discard(name)
            while self.fetch_times and now - self.fetch_times[0] >= self.fetch_window:
                self.fetch_times.popleft()
            reason = None
            if slug in self.failed_fetch:
                reason = "negative cache"
            elif created and len(self.last_fetch) >= self.max_fetch_history:
                reason = "new-mirror history full"
            elif created and len(self.fetch_times) >= self.fetch_rate_limit:
                reason = "new-mirror budget exhausted"
            if reason:
                logging.getLogger("zab.files").warning("fetch deferred repo=%s: %s", slug, reason)
                return
            delay = max(0.0, self.last_fetch.get(slug, now) - now)
            if created:
                self.fetch_times.append(now)
                # Reserve history before dropping its lock, including requests
                # queued for the fetch pool. Other new slugs cannot overfill it.
                self.last_fetch[slug] = now + self.negative_interval
                self.failed_fetch.add(slug)
        if delay:
            time.sleep(delay)
        if not self.fetch_capacity.acquire(timeout=35):
            if created:
                with self.history_lock:
                    self.last_fetch.pop(slug, None)
                    self.failed_fetch.discard(slug)
                    with suppress(ValueError):
                        self.fetch_times.remove(now)
            logging.getLogger("zab.files").warning("fetch deferred repo=%s: fetch pool busy", slug)
            raise FetchBusy("fetch pool busy")
        if gate is not None:
            gate.started = time.monotonic()
        try:
            self.fetch_now(slug, repo)
            if gate is not None:
                gate.succeeded = gate.started
        except (OSError, subprocess.TimeoutExpired):
            if created:
                shutil.rmtree(repo, ignore_errors=True)
            raise
        finally:
            self.fetch_capacity.release()

    def fetch_now(self, slug: str, repo: Path, source: str = "") -> None:
        """The atomic, pruning fetch of `slug`'s turn refs into `repo`, from `source`, the
        repository's name on GitHub now when it was renamed (by default `slug`), and its outcome
        in the cooldown and the negative cache. The caller holds the repository's gate and a
        fetch slot."""
        source = source or slug
        try:
            repo.mkdir(exist_ok=True)
            if not (repo / "HEAD").exists():
                self.git(repo, "init", "--bare", "--template=")
            # Recover locks left by a previous killed service/fetch. The fetch
            # lock is held, and auto-GC stays attached to its parent process.
            for lock in repo.rglob("*.lock"):
                with suppress(FileNotFoundError):
                    if time.time() - lock.stat().st_mtime > 35:
                        lock.unlink(missing_ok=True)
            self.git(
                repo,
                "fetch",
                "--atomic",
                "--prune",
                "--no-tags",
                "--no-recurse-submodules",
                self.remote(source),
                REFSPEC,
                SHARE_REFSPEC,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            with self.history_lock:
                self.last_fetch[slug] = time.monotonic() + self.negative_interval
                self.failed_fetch.add(slug)
            logging.getLogger("zab.files").warning(
                "fetch failed repo=%s exit=%s: %s",
                slug,
                exc.code if isinstance(exc, GitError) else "timeout/error",
                exc.detail if isinstance(exc, GitError) else type(exc).__name__,
            )
            raise
        with self.history_lock:
            self.last_fetch[slug] = time.monotonic() + self.fetch_interval
            self.failed_fetch.discard(slug)

    def remote(self, name: str) -> str:
        """`org/name` on GitHub, which git fetches from; in the tests, the bare repository
        `test_remotes/org/name.git`, keyed by the organization as GitHub is, so that a server
        of another one finds nothing there either (review of #770). `GitError` without an
        organization: nothing is fetched then."""
        if not self.org:
            raise GitError(2, b"no GitHub organization to fetch from")
        if self.test_remotes:
            return (self.test_remotes / self.org / f"{name}.git").as_uri()
        return f"https://github.com/{self.org}/{name}.git"

    def known_tip(self, repo: Path, sha: str) -> bool:
        if not (repo / "HEAD").exists():
            return False
        tips = self.git(repo, "for-each-ref", "--format=%(objectname)", "refs/zab/turns/")
        return sha.encode() in tips.splitlines()

    def lookup(self, repo: Path, sha: str, path: str) -> Entry | None:
        if self.git(repo, "cat-file", "-t", sha).strip() != b"commit":
            return None
        if not path:
            tree = self.git(repo, "rev-parse", f"{sha}^{{tree}}").decode().strip()
            return Entry("040000", "tree", tree, 0, "")
        found = entries(self.git(repo, "ls-tree", "-l", "-z", sha, "--", path))
        return next((entry for entry in found if entry.name == path), None)

    def share_refs(self, repo: Path) -> dict[str, list[str]]:
        """The share commits at tips of `repo`'s `refs/zab/shares/*`, each with its refs."""
        if not (repo / "HEAD").exists():
            return {}
        listed = self.git(repo, "for-each-ref", "--format=%(objectname) %(refname)", SHARE_REFS)
        out: dict[str, list[str]] = {}
        for line in listed.decode("ascii", "replace").splitlines():
            sha, _, ref = line.partition(" ")
            if SHA.fullmatch(sha) and SHARE_REF.fullmatch(ref):
                out.setdefault(sha, []).append(ref)
        return out

    def prepare(self, slug: str, sha: str, path: str) -> Path | None:
        """Validate a URL and refresh unknown tips before taking a body slot; where the tip is
        served from: its mirror for a turn, the share store for a share (#710), or None."""
        if (
            not SLUG.fullmatch(slug)
            or not SHA.fullmatch(sha)
            or not allowed(path + "/x" if path else "")
        ):
            return None
        arrived = time.monotonic()
        slug, sha = slug.lower(), sha.lower()
        repo = self.cache / f"{slug}.git"
        share = self.shares / f"{slug}.git"
        try:
            if sha in self.share_refs(share) and sha not in self.revoked(share):
                return share
            if self.known_tip(repo, sha):
                return repo
        except GitError:
            pass  # A concurrent git init may have written HEAD but not the full mirror.
        with self.gates_lock:
            gate = self.gates.setdefault(slug, FetchGate())
            pending = gate.pending.get(sha)
            leader = pending is None
            if leader:
                if len(gate.pending) >= 8 or self.pending_tips >= 32:
                    if not gate.pending and not repo.exists():
                        self.gates.pop(slug, None)
                    raise FetchBusy("unknown snapshot waiter limit")
                pending = gate.pending[sha] = PendingTip()
                self.pending_tips += 1
        if not leader:
            if not pending.done.wait(35) or pending.busy:
                raise FetchBusy("snapshot wait timeout")
            return pending.result
        try:
            if not gate.lock.acquire(timeout=35):
                raise FetchBusy("repository wait timeout")
            try:
                if not self.known_tip(repo, sha) and gate.started <= arrived:
                    self.fetch(slug, repo, gate)
                fresh = gate.succeeded > arrived  # this request's own fetch, or a later one's
                pending.result = self.found(slug, repo, share, sha, fresh=fresh)
                return pending.result
            finally:
                gate.lock.release()
        except FetchBusy:
            pending.busy = True
            raise
        finally:
            with self.gates_lock:
                pending.done.set()
                del gate.pending[sha]
                self.pending_tips -= 1
                if not gate.pending and not repo.exists():
                    self.gates.pop(slug, None)

    def found(self, slug: str, repo: Path, share: Path, sha: str, *, fresh: bool) -> Path | None:
        """Where `sha` is served from, under its gate: the mirror for a turn; the share store
        for a share, which takes it from the mirror first, but only when `fresh`, after a fetch
        that began after the request and succeeded, so that the share is on GitHub now."""
        if self.known_tip(repo, sha):
            return repo
        if sha in self.revoked(share):
            return None
        if sha in self.share_refs(share) or (fresh and self.adopt(slug, repo, share, sha)):
            return share
        return None

    def adopt(self, slug: str, repo: Path, share: Path, sha: str) -> bool:
        """The share `sha`, a tip of the mirror's `refs/zab/shares/*`, copied into the share
        store (#710), under `refs/zab/shares/<sha>`: so a share of a repository made again under
        the name, under the same name, takes nothing from one kept (Codex round 1 on #740); its
        names on GitHub are kept beside it. The caller holds the repository's gate, after a
        fetch that began after its request, so the share is on GitHub now. A share is taken only
        with its repository's GitHub ID, looked up first and recorded, with its names, before
        its ref is installed, so that every share kept can be revoked (`check_shares`; the
        reviewer on #740): without one (no token, no answer, a rate limit) it is `FetchBusy`, a
        503, which `zab share`'s first opening retries. A share revoked is never taken again."""
        refs = self.share_refs(repo).get(sha)
        if not refs:
            return False
        api = self.api()
        state, ident, _ = self.lookup_repo(api, slug) if api else (UNKNOWN, None, "")
        if state == MISSING:
            return False  # gone since its fetch
        if state != EXISTS or ident is None:
            raise FetchBusy("the repository's GitHub ID can't be read now")
        share.mkdir(parents=True, exist_ok=True)
        with self.store_lock(share):  # a deployer's `unshare` may run its gc meanwhile
            if sha in self.revoked(share):
                return False
            if not (share / "HEAD").exists():
                self.git(share, "init", "--bare", "--template=")
            for lock in share.rglob("*.lock"):  # left by a killed service, as a mirror's
                with suppress(FileNotFoundError):
                    if time.time() - lock.stat().st_mtime > 35:
                        lock.unlink(missing_ok=True)
            names = " ".join(r.removeprefix(SHARE_REFS) for r in refs)
            self.record(share, SHARE_NAMES, {sha: names})  # raises, and nothing is taken
            self.record_ids(share, {sha: ident})
            mapped = f"+{refs[0]}:{SHARE_REFS}{sha}"
            self.git(
                share, "fetch", "--no-tags", "--no-write-fetch-head", str(repo), mapped, local=True
            )
        return sha in self.share_refs(share)

    def read(
        self, slug: str, sha: str, path: str, repo: Path | None = None
    ) -> tuple[bytes, str, bool] | None:
        """Read a tip admitted by prepare(), from where it said, while the caller holds a body
        slot."""
        repo = repo if repo is not None else self.cache / f"{slug.lower()}.git"
        sha = sha.lower()
        entry = self.lookup(repo, sha, path)
        if entry is None:
            return None
        if entry.kind == "tree":
            rows = []
            base = f"/{slug}/{sha}/"
            if path:
                parent = path.rpartition("/")[0]
                parent_url = base + (quote(parent) + "/" if parent else "")
                rows.append(f'<li><a href="{parent_url}">../</a></li>')
            for child in entries(self.git(repo, "ls-tree", "-l", "-z", entry.sha)):
                child_path = f"{path}/{child.name}" if path else child.name
                if not allowed(
                    child_path + "/x" if child.kind == "tree" else child_path
                ) or child.mode not in ("040000", "100644", "100755"):
                    continue
                suffix = "/" if child.kind == "tree" else ""
                url = base + quote(child_path, safe="/") + suffix
                rows.append(f'<li><a href="{url}">{html.escape(child.name)}{suffix}</a></li>')
            title = html.escape(path or slug)
            page = (
                f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
                f"<ul>{''.join(rows)}</ul>"
            )
            return page.encode(), "text/html; charset=utf-8", True
        if not allowed(path) or entry.mode not in ("100644", "100755") or entry.size > MAX_BLOB:
            return None
        data = self.git(repo, "cat-file", "blob", entry.sha)
        return data, content_type(path, data), False

    # ------------------------------------------------- deleted and transferred repositories (#714)

    def mirrors(self) -> list[str]:
        """The repositories mirrored here, by their lowercase names."""
        names = (path.name.removesuffix(".git") for path in self.cache.glob("*.git"))
        return sorted(name for name in names if SLUG.fullmatch(name) and name == name.lower())

    def lookup_repo(self, api: GitHubAPI, name: str) -> tuple[str, int | None, str]:
        """`EXISTS` with its ID and its name now (another one when it was renamed within
        `org`) when the token reads `org/name`; `MISSING` for a 404, or a redirect to a
        repository outside `org` (moved out of reach, or readable at its new owner's);
        `UNKNOWN` for anything else: no answer, a 401 or 403 (a bad token), a 5xx, a rate
        limit, a 200 without its owner's login, or no organization to look in."""
        if not self.org:
            return UNKNOWN, None, ""
        try:
            status, body = api.get(f"/repos/{self.org}/{quote(name, safe='')}")
        except (OSError, http.client.HTTPException, ValueError):
            return UNKNOWN, None, ""
        if status == 404:
            return MISSING, None, ""
        if status != 200 or not isinstance(body, dict) or type(body.get("id")) is not int:
            return UNKNOWN, None, ""
        owner = body.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        if not isinstance(login, str) or not login:
            return UNKNOWN, None, ""
        if login.casefold() != self.org.casefold():
            return MISSING, None, ""
        return EXISTS, body["id"], str(body.get("name") or name)

    def reads_another(self, api: GitHubAPI, missing: set[str]) -> set[str] | None:
        """The private repositories of `org` that the token lists, when it also reads one of
        them, not in `missing`, afresh: so a 404 isn't a token that reads nothing private
        (an expired approval, a token of another owner). None otherwise, and without an
        organization. Every page of the listing is read, up to `LISTING_PAGES` of 100: a page
        that fails, or more, is None."""
        if not self.org:
            return None
        listed: set[str] = set()
        for page in range(1, LISTING_PAGES + 1):
            try:
                path = f"/orgs/{self.org}/repos?type=private&per_page=100&page={page}"
                status, body = api.get(path)
            except (OSError, http.client.HTTPException, ValueError):
                return None
            if status != 200 or not isinstance(body, list):
                return None
            listed |= {
                str(repo.get("name", "")).lower()
                for repo in body
                if isinstance(repo, dict) and repo.get("private") is True
            }
            if len(body) < 100:
                break
        else:
            return None
        other = sorted(listed - missing)
        if not other or self.lookup_repo(api, other[0])[0] != EXISTS:
            return None
        return listed

    def replaced(self, name: str, ident: int, now: str = "") -> bool:
        """Whether the repository called `name` has another ID than the one its mirror
        recorded: a new repository took the name of the one mirrored. A mirror without an ID
        yet (made before this check, or by a request since the last) may hold a repository
        deleted since, whose name a new one took (Codex round 1 on #733): it is fetched first,
        which prunes its turn refs to the repository found now, and its ID recorded only once
        that fetch succeeded. The fetch is from `now`, the repository's name on GitHub, which
        GitHub's redirect from `name` gave, since the server's git doesn't follow one: so a
        repository made again and then renamed is pruned to as well (Codex round 2)."""
        path = self.cache / f"{name}.git" / ID_FILE
        try:
            known = path.read_text().strip()
        except FileNotFoundError:
            if self.refresh(name, now):
                with suppress(OSError):
                    temporary = path.with_name(ID_FILE + ".new")
                    temporary.write_text(f"{ident}\n")
                    temporary.replace(path)
            return False
        except (OSError, UnicodeDecodeError):
            return False
        return known != str(ident)

    def refresh(self, name: str, source: str = "") -> bool:
        """`name`'s mirror fetched now, from `source` on GitHub (`fetch_now`), under its gate
        and a fetch slot, as a request's fetch is: True when it was. Requests waiting for that
        gate take this fetch for theirs."""
        if source and not SLUG.fullmatch(source):
            return False
        with self.gates_lock:
            gate = self.gates.setdefault(name, FetchGate())
        repo = self.cache / f"{name}.git"
        if not gate.lock.acquire(timeout=35):
            return False
        try:
            if not (repo / "HEAD").exists() or not self.fetch_capacity.acquire(timeout=35):
                return False
            try:
                gate.started = time.monotonic()
                self.fetch_now(name, repo, source)
                gate.succeeded = gate.started
                return True
            except (OSError, subprocess.TimeoutExpired):
                return False
            finally:
                self.fetch_capacity.release()
        finally:
            gate.lock.release()
            with self.gates_lock:
                if not gate.pending and self.gates.get(name) is gate and not repo.exists():
                    self.gates.pop(name, None)

    def check(self, api: GitHubAPI) -> dict[str, list[str]]:
        """Each mirror's repository looked up on GitHub (#714), and the mirrors of those gone
        removed. Gone means: not found (`MISSING`); then the token lists the organization's
        private repositories and reads another of them; then not found again. A repository
        that its listing shows is not gone, and neither is any repository while the token reads
        nothing, so no failure, outage or bad token removes a mirror: those answer `UNKNOWN`,
        or leave the token reading nothing. More than `GONE_AT_ONCE` at once, or a quarter of
        the mirrors, and none is removed. A repository found under another ID is gone too: a
        new one has its name. Returns what it found, by state, and the mirrors removed."""
        found: dict[str, list[str]] = {EXISTS: [], MISSING: [], UNKNOWN: [], "removed": []}
        names = self.mirrors()
        for name in names:
            state, ident, now = self.lookup_repo(api, name)
            found[state].append(name)
            if ident is not None and self.replaced(name, ident, now):
                if self.remove(name, "another repository has its name"):
                    found["removed"].append(name)
        missing, gone, note = found[MISSING], [], ""
        if missing:
            listed = self.reads_another(api, set(missing))
            if listed is None:
                note = (
                    "; the token reads no other private repository: none removed, kept "
                    + ", ".join(missing)
                )
            else:
                gone = [n for n in missing if n not in listed]
                gone = [n for n in gone if self.lookup_repo(api, n)[0] == MISSING]
                if len(gone) > max(GONE_AT_ONCE, len(names) // 4):
                    # named, so that a deployer can check each on GitHub and purge it (#714)
                    note = (
                        f"; {len(gone)} gone at once: none removed, kept {', '.join(gone)} "
                        "(check the token, then purge by hand those deleted)"
                    )
                    gone = []
        for name in gone:
            if self.remove(name, "deleted or transferred"):
                found["removed"].append(name)
        logging.getLogger("zab.files").warning(
            "gone check: %d mirrors, %d exist, %d not found, %d unknown; removed %s%s",
            len(names),
            len(found[EXISTS]),
            len(missing),
            len(found[UNKNOWN]),
            ", ".join(found["removed"]) or "none",
            note,
        )
        return found

    def remove(self, name: str, why: str) -> bool:
        """Remove `name`'s mirror, under its fetch lock: a later request for it fetches again,
        and gets a 404 unless GitHub has the repository. False when there was none."""
        name = name.lower()
        if not SLUG.fullmatch(name):
            return False
        with self.gates_lock:
            gate = self.gates.setdefault(name, FetchGate())
        if not gate.lock.acquire(timeout=35):
            logging.getLogger("zab.files").warning("mirror kept repo=%s: its fetch is busy", name)
            return False
        repo = self.cache / f"{name}.git"
        try:
            if not repo.exists():
                return False
            trash = repo.with_name(repo.name + ".gone")
            shutil.rmtree(trash, ignore_errors=True)
            try:
                repo.rename(trash)  # readers lose it at once; its contents go after
            except OSError:
                trash = repo
            shutil.rmtree(trash, ignore_errors=True)
            with self.history_lock:
                self.last_fetch.pop(name, None)
                self.failed_fetch.discard(name)
        finally:
            gate.lock.release()
            with self.gates_lock:
                if not gate.pending and self.gates.get(name) is gate and not repo.exists():
                    self.gates.pop(name, None)
        logging.getLogger("zab.files").warning("mirror removed repo=%s: %s", name, why)
        return True

    # ------------------------------------------------------------------- share links (#710)

    def stores(self) -> list[str]:
        """The repositories with a share store here, by their lowercase names."""
        names = (path.name.removesuffix(".git") for path in self.shares.glob("*.git"))
        return sorted(name for name in names if SLUG.fullmatch(name) and name == name.lower())

    @contextmanager
    def store_lock(self, share: Path) -> Iterator[None]:
        """The store's lock between processes (`flock` on `STORE_LOCK`), around what writes its
        objects or refs: an adoption's fetch, and a revocation with its gc, which could otherwise
        prune the objects of a fetch that hasn't installed its ref yet (Codex round 3 on #740).
        The repository's gate already orders them within the service. `FetchBusy` after
        `store_wait` seconds."""
        fd = os.open(share / STORE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + self.store_wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise FetchBusy("share store busy") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # which releases the lock

    def recorded(self, share: Path, kind: str) -> dict[str, str]:
        """A store's `SHARE_IDS` or `SHARE_NAMES`: commit → its value."""
        try:
            text = (share / kind).read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return {}
        pairs = (line.split(" ", 1) for line in text.splitlines())
        return {p[0]: p[1] for p in pairs if len(p) == 2 and SHA.fullmatch(p[0])}

    def record(self, share: Path, kind: str, values: dict[str, str]) -> None:
        """`values` merged into a store's `kind` file, under `meta_lock` (Codex round 1 on
        #740: the adoption and the check may record at once), through a temporary file of its
        own."""
        with self.meta_lock:
            merged = {**self.recorded(share, kind), **values}
            handle, temporary = tempfile.mkstemp(dir=share, prefix=kind + ".")
            with os.fdopen(handle, "w", encoding="ascii") as out:
                out.write("".join(f"{sha} {v}\n" for sha, v in sorted(merged.items())))
            os.replace(temporary, share / kind)

    def share_ids(self, share: Path) -> dict[str, int]:
        """Each share's repository ID, as recorded when it was taken."""
        return {s: int(v) for s, v in self.recorded(share, SHARE_IDS).items() if v.isdigit()}

    def record_ids(self, share: Path, ids: dict[str, int]) -> None:
        self.record(share, SHARE_IDS, {sha: str(ident) for sha, ident in ids.items()})

    def revoked(self, share: Path) -> set[str]:
        try:
            return set((share / REVOKED).read_text(encoding="ascii").split())
        except (OSError, UnicodeDecodeError):
            return set()

    def remote_shares(self, name: str) -> set[tuple[str, str]]:
        """`org/name`'s `refs/zab/shares/*` on GitHub, as (ref, sha); raises on any failure.
        git lists every ref, which the REST API may page."""
        listed = self.git(self.cache, "ls-remote", "--refs", self.remote(name), SHARE_REFS + "*")
        pairs = (line.split("\t") for line in listed.decode("ascii", "replace").splitlines())
        # the pattern matches a ref's tail: only `refs/zab/shares/…` itself is a share
        return {
            (ref, sha)
            for sha, ref in (p for p in pairs if len(p) == 2)
            if ref.startswith(SHARE_REFS)
        }

    def lookup_id(self, api: GitHubAPI, ident: int) -> tuple[str, str]:
        """The repository with GitHub ID `ident` (`GET /repositories/<id>`, which follows a
        rename and never names a new repository that took the name): `EXISTS` with its name
        now when it is `org`'s; `MISSING` for a 404 or another owner's; else `UNKNOWN`, as
        `lookup_repo`."""
        if not self.org:
            return UNKNOWN, ""
        try:
            status, body = api.get(f"/repositories/{int(ident)}")
        except (OSError, http.client.HTTPException, ValueError):
            return UNKNOWN, ""
        if status == 404:
            return MISSING, ""
        if status != 200 or not isinstance(body, dict) or body.get("id") != ident:
            return UNKNOWN, ""
        owner = body.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        if not isinstance(login, str) or not login:
            return UNKNOWN, ""
        if login.casefold() != self.org.casefold():
            return MISSING, ""
        now = body.get("name")
        return (EXISTS, now) if isinstance(now, str) and SLUG.fullmatch(now) else (UNKNOWN, "")

    def check_shares(
        self, api: GitHubAPI, name: str, *, only: str = "", strict: bool = False
    ) -> list[str]:
        """The shares of `name` (or `only` that one) whose ref is gone from GitHub, revoked
        (#710): each looked up by the GitHub ID recorded when it was taken (`lookup_id`, the
        reviewer on #740), so a renamed repository is followed and a new one under the name is
        another. No share ref on GitHub at its commit, in a repository that exists, revokes it.
        Its repository gone revokes nothing; nor does a failure, or with `strict` (the recheck),
        a failure that leaves the share undecided is `FetchBusy`. Returns the shares revoked."""
        share = self.shares / f"{name}.git"
        held = [sha for sha in self.share_refs(share) if not only or sha == only]
        ids = self.share_ids(share)
        by_repo: dict[int, list[str]] = {}
        for sha in held:  # a share without an ID is never taken (`adopt`)
            if sha in ids:
                by_repo.setdefault(ids[sha], []).append(sha)
        gone = []
        for ident, shas in sorted(by_repo.items()):
            state, now = self.lookup_id(api, ident)
            if state == MISSING:
                continue  # its repository is gone: a gone repository revokes nothing
            try:
                if state != EXISTS:
                    raise FetchBusy("GitHub didn't say whether the repository exists")
                listed = {sha for _, sha in self.remote_shares(now.lower())}
            except (FetchBusy, OSError, subprocess.TimeoutExpired):
                if strict:
                    raise FetchBusy("the share can't be checked with GitHub now") from None
                continue
            gone += [sha for sha in shas if sha not in listed]  # under any name
        revoked = []
        for sha in gone:
            try:
                if self.unshare(name, sha, "its ref is gone from GitHub"):
                    revoked.append(sha)
            except FetchBusy:  # the hourly check tries again; the recheck answers 503
                if strict:
                    raise
        return revoked

    def check_all_shares(self, api: GitHubAPI) -> dict[str, list[str]]:
        """`check_shares` for every store: within an hour of a ref's deletion, its share goes."""
        out = {}
        for name in self.stores():
            if revoked := self.check_shares(api, name):
                out[name] = revoked
        logging.getLogger("zab.files").warning(
            "share check: %d repositories with shares; revoked %d",
            len(self.stores()),
            sum(len(v) for v in out.values()),
        )
        return out

    def unshare(self, name: str, sha: str, why: str) -> bool:
        """Revoke one share at once: its refs deleted from the store, its objects pruned, and
        its commit recorded as revoked, so that no later fetch takes it again. False when the
        store held no such share; `FetchBusy` when its repository's gate or the store stays busy
        for `store_wait` seconds (Codex on #740: never a quiet False, which a recheck would
        report as kept). Never logs the commit, which is the link's capability."""
        name, sha = name.lower(), sha.lower()
        if not SLUG.fullmatch(name) or not SHA.fullmatch(sha):
            return False
        share = self.shares / f"{name}.git"
        with self.gates_lock:
            gate = self.gates.setdefault(name, FetchGate())
        if not gate.lock.acquire(timeout=self.store_wait):
            raise FetchBusy("the repository's gate is busy")
        try:
            if not (share / "HEAD").exists():
                return False
            with self.store_lock(share):  # Codex round 3 on #740: the service's adoption
                refs = self.share_refs(share).get(sha)
                if not refs:
                    return False
                with (share / REVOKED).open("a", encoding="ascii") as out:
                    out.write(sha + "\n")
                for ref in refs:
                    self.git(share, "update-ref", "-d", ref)
                # its objects, no longer reachable, go; a failed gc leaves them, never served
                with suppress(OSError, subprocess.TimeoutExpired):
                    self.git(share, "gc", "--prune=now", "--quiet")
        finally:
            gate.lock.release()
        logging.getLogger("zab.files").warning("share revoked repo=%s: %s", name, why)
        return True

    def recheck(self, name: str, sha: str) -> str | None:
        """`POST /_zab/recheck/<repo>/<sha>` (#710): the share looked up on GitHub again at
        once, as the hourly check does: "revoked" or "kept"; None when it is no share here.
        `FetchBusy` (a 503) when its limit is reached, there is no token, or GitHub doesn't say
        (the reviewer on #740: never "kept" for a share left undecided): try later."""
        if not SLUG.fullmatch(name) or not SHA.fullmatch(sha):
            return None
        name, sha = name.lower(), sha.lower()
        if sha not in self.share_refs(self.shares / f"{name}.git"):
            return None
        now = time.monotonic()
        with self.recheck_lock:
            while self.recheck_times and now - self.recheck_times[0] >= 3600:
                self.recheck_times.popleft()
            for key in [k for k, at in self.rechecked.items() if now - at >= RECHECK_EVERY]:
                del self.rechecked[key]
            if len(self.recheck_times) >= RECHECKS_PER_HOUR or (name, sha) in self.rechecked:
                raise FetchBusy("recheck limit")
            self.recheck_times.append(now)
            self.rechecked[(name, sha)] = now
        api = self.api()
        if api is None:
            raise FetchBusy("no token for the recheck")
        return "revoked" if self.check_shares(api, name, only=sha, strict=True) else "kept"

    def list_shares(self, name: str) -> list[tuple[str, str, str, list[str], list[str]]]:
        """A store's shares, for a deployer: (sha, date, link path, its names on GitHub, the
        paths shared)."""
        share = self.shares / f"{name.lower()}.git"
        names, out = self.recorded(share, SHARE_NAMES), []
        for sha in sorted(self.share_refs(share)):
            date = self.git(share, "log", "-1", "--format=%cI", sha).decode().strip()
            message = self.git(share, "log", "-1", "--format=%B", sha).decode("utf-8", "replace")
            paths = [line[6:] for line in message.splitlines() if line.startswith("Path: ")]
            out.append((sha, date, f"/{name.lower()}/{sha}/", names.get(sha, "?").split(), paths))
        return out


class Checker(threading.Thread):
    """`Store.check`, every `CHECK_INTERVAL` seconds from `CHECK_DELAY` after the start, with the
    token read afresh each time, so a rotated token (README §3) needs no restart."""

    def __init__(
        self, store: Store, *, delay: float = CHECK_DELAY, interval: float = CHECK_INTERVAL
    ) -> None:
        super().__init__(name="zab-files-check", daemon=True)
        self.store, self.delay, self.interval = store, delay, interval
        self.stopped = threading.Event()

    def run(self) -> None:
        wait = self.delay
        while not self.stopped.wait(wait):
            wait = self.interval
            token = read_token()
            if not token:
                logging.getLogger("zab.files").warning("gone check skipped: no token")
                continue
            for check in (self.store.check, self.store.check_all_shares):  # #714, #710
                try:
                    check(GitHubAPI(token))
                except Exception as exc:  # the next check tries again; serving goes on
                    logging.getLogger("zab.files").warning(
                        "%s failed: %s", check.__name__, type(exc).__name__
                    )


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], store: Store) -> None:
        self.store = store
        self.capacity = threading.BoundedSemaphore(8)
        self.render_capacity = threading.BoundedSemaphore(RENDER_SLOTS)
        self.admission = threading.BoundedSemaphore(32)
        self.admission_timeout = 35
        super().__init__(address, Handler)

    def process_request(self, request, client_address) -> None:
        # Wait before starting another thread; the listen backlog absorbs bursts.
        # Bound handlers to 32 and blob responses to eight under TasksMax=128.
        request.settimeout(40)
        if not self.admission.acquire(timeout=self.admission_timeout):
            logging.getLogger("zab.files").warning(
                "load shed status=503: handler admission timeout"
            )
            try:
                request.settimeout(1)
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.admission.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.admission.release()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args: object) -> None:
        pass  # SHA URLs are capabilities. Do not put request paths in access logs.

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        """Only `POST /_zab/recheck/<repo>/<sha>` (#710): never a request's body."""
        self.response_started = False
        try:
            target = urlsplit(self.path)
            parts = unquote(target.path, errors="strict").split("/")
            if not target.netloc and target.path.startswith(RECHECK_PREFIX) and len(parts) == 5:
                said = self.server.store.recheck(parts[3], parts[4])
                if said is not None:
                    self.reply(
                        200, f"{said}\n".encode(), "text/plain; charset=utf-8", transient=True
                    )
                    return
            self.reply(404, b"Not found\n", "text/plain; charset=utf-8")
        except FetchBusy as exc:
            logging.getLogger("zab.files").warning("recheck refused status=503: %s", exc)
            with suppress(OSError):
                self.reply(503, b"Busy; retry shortly\n", "text/plain; charset=utf-8")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if not self.response_started:
                with suppress(OSError):
                    self.reply(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        self.response_started = False
        try:
            self.serve_get()
        except OSError:
            pass  # A disconnected client must not produce a server traceback.

    def serve_get(self) -> None:
        try:
            target = urlsplit(self.path)
            if target.path.startswith(STATIC_PREFIX) and not target.netloc:
                self.serve_static(unquote(target.path[len(STATIC_PREFIX) :], errors="strict"))
                return
            parts = unquote(target.path, errors="strict").split("/", 3)
            if not target.netloc and len(parts) >= 3 and parts[0] == "":
                slug, sha = parts[1:3]
                path = parts[3].removesuffix("/") if len(parts) == 4 else ""
                if where := self.server.store.prepare(slug, sha, path):
                    slug, sha = slug.lower(), sha.lower()
                    if not self.server.capacity.acquire(timeout=35):
                        logging.getLogger("zab.files").warning(
                            "load shed status=503: response pool busy"
                        )
                        self.reply(503, b"Busy; retry shortly\n", "text/plain; charset=utf-8")
                        return
                    try:
                        result = self.server.store.read(slug, sha, path, where)
                        if result:
                            data, kind, directory = result
                            # Relative HTML assets need a canonical directory base.
                            if directory and not target.path.endswith("/"):
                                self.response_started = True
                                self.send_response(308)
                                self.send_header("Location", target.path + "/")
                                self.headers_common(False)
                                self.send_header("Content-Length", "0")
                                self.end_headers()
                            elif directory:
                                # a listing, even of `pkg.py/`: never a rendered view (codex
                                # round 1 on #266)
                                self.reply(200, data, kind)
                            else:
                                self.serve_file(slug, sha, path, data, kind, target.query)
                            return
                    finally:
                        self.server.capacity.release()
        except FetchBusy as exc:
            logging.getLogger("zab.files").warning("load shed status=503: %s", exc)
            self.reply(503, b"Busy; retry shortly\n", "text/plain; charset=utf-8")
            return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if self.response_started:
                return  # A partial response cannot be replaced by a second HTTP response.
        self.reply(404, b"Not found\n", "text/plain; charset=utf-8")

    def serve_static(self, name: str) -> None:
        data = RENDER.static_file(name) if RENDER else None
        if data is None:
            self.reply(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        self.reply(200, data, content_type(name, data), static=True)

    def serve_file(
        self, slug: str, sha: str, path: str, data: bytes, kind: str, query: str
    ) -> None:
        """The rendered page when a browser navigates to a Markdown or source file (#256),
        else the exact bytes, as before; `?raw=1` always gets the bytes. When the page is
        wanted but not rendered for a passing reason (a renderer error, no free render slot,
        or a HEAD request, which never renders), the bytes are sent with `no-store`; a
        file over the caps stays plain text for good, and is cached as such."""
        renderable = RENDER is not None and RENDER.language(path) is not None
        if not (
            renderable
            and parse_qs(query).get("raw") != ["1"]
            and self.headers.get("Sec-Fetch-Dest", "") in PAGE_DESTINATIONS
        ):
            self.reply(200, data, kind, vary=renderable)
            return
        page, transient = None, True
        if self.command == "GET" and self.server.render_capacity.acquire(timeout=RENDER_WAIT):
            try:
                page = RENDER.render(path, data, base=f"/{quote(slug)}/{sha}")
                transient = False
            except Exception as exc:  # a renderer bug must never cost the file itself
                logging.getLogger("zab.files").warning("render failed: %s", type(exc).__name__)
            finally:
                self.server.render_capacity.release()
        elif self.command == "GET":
            logging.getLogger("zab.files").warning("render slot busy: sent the bytes")
        if page is not None:
            self.reply(200, page, "text/html; charset=utf-8", vary=True)
        else:
            self.reply(200, data, kind, vary=True, transient=transient)

    def reply(
        self,
        status: int,
        data: bytes,
        kind: str,
        *,
        static: bool = False,
        vary: bool = False,
        transient: bool = False,
    ) -> None:
        """`transient`: this URL may answer differently later, so the reply is `no-store`."""
        self.response_started = True
        self.send_response(status)
        self.headers_common(status == 200 and not transient, kind, static=static)
        if vary:
            # one URL, two responses: the page for navigation, the bytes for everything else
            self.send_header("Vary", "Sec-Fetch-Dest")
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            body = memoryview(data)
            started = time.monotonic()
            for offset in range(0, len(body), 256 * 1024):
                # Bound elapsed time by bytes already sent, not the advertised size.
                remaining = started + max(40, offset / (64 * 1024)) - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("response deadline")
                self.connection.settimeout(min(40, remaining))
                self.wfile.write(body[offset : offset + 256 * 1024])

    def headers_common(self, immutable: bool, kind: str = "", *, static: bool = False) -> None:
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Preserve interactive HTML, but give each document an opaque origin so
        # another participant's page cannot read its opener's SHA URL or DOM.
        mime = kind.split(";", 1)[0]
        if mime == "text/html" or mime in ("text/xml", "application/xml") or mime.endswith("+xml"):
            self.send_header("Content-Security-Policy", SANDBOX)
        if immutable:
            self.send_header(
                "Access-Control-Allow-Origin", "*"
            )  # also lets sandboxed pages load fonts
        if static:
            # pages link files.css, math.js and KaTeX by content hash; the fonts KaTeX's CSS
            # names have no hash, so a day, not a year
            self.send_header("Cache-Control", "public, max-age=86400")
            return
        self.send_header(
            "Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-store"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("/var/cache/zab-files"))
    parser.add_argument("--shares", type=Path, default=Path("/var/lib/zab-files/shares"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--check-delay", type=float, default=CHECK_DELAY, help="seconds before the first check"
    )
    parser.add_argument(
        "--org",
        default=os.environ.get(ORG_ENV, "").strip(),
        help=f"the GitHub organization whose repositories are served (default: ${ORG_ENV}, "
        f"which the unit reads from {ORG_FILE}); the service needs it, a command doesn't",
    )
    commands = parser.add_subparsers(dest="command", metavar="purge|shares|unshare")
    purge = commands.add_parser("purge", help="remove one repository's mirror at once (#714)")
    purge.add_argument("repo", help="the repository's name, as in its links")
    shares = commands.add_parser("shares", help="list the share links kept here (#710)")
    shares.add_argument("repo", nargs="?", help="one repository's (default: every one)")
    unshare = commands.add_parser("unshare", help="revoke share links at once (#710)")
    unshare.add_argument("repo", help="the repository's name, as in its links")
    unshare.add_argument("share", help="a share's link, commit or name, or `all`")
    args = parser.parse_args(argv)
    for name in filter(None, (getattr(args, "repo", None),)):
        if not SLUG.fullmatch(name):
            print(f"not a repository name: {name!r}", file=sys.stderr)
            return 2
    if args.command == "purge":
        removed = Store(args.cache, shares=args.shares).remove(args.repo, "purged by hand")
        said = f"{'removed' if removed else 'no'} mirror of {args.repo.lower()}"
        journal(f"purge: {said}")
        print(said)
        return 0
    if args.command == "shares":
        store = Store(args.cache, shares=args.shares)
        for name in [args.repo.lower()] if args.repo else store.stores():
            for _sha, date, link, names, paths in store.list_shares(name):
                print(f"{name}\t{', '.join(names)}\t{date}\t{link}\t{' '.join(paths)}")
        return 0
    if args.command == "unshare":
        return unshare_by_hand(Store(args.cache, shares=args.shares), args.repo, args.share)
    if not ORG_NAME.fullmatch(args.org):
        said = f"not a GitHub organization: {args.org!r}" if args.org else "no GitHub organization"
        print(
            f"{said}: give --org, or {ORG_ENV}=<organization> in {ORG_FILE} "
            "(workshop/files/README.md §3)",
            file=sys.stderr,
        )
        return EX_CONFIG
    store = Store(args.cache, shares=args.shares, org=args.org)
    logging.getLogger("zab.files").warning("serving the repositories of %s", args.org)
    Checker(store, delay=args.check_delay).start()
    with Server(("127.0.0.1", args.port), store) as server:
        server.serve_forever()
    return 0


def journal(message: str) -> None:
    """A deployer's command's outcome in the system log, which journald keeps, under the
    service's name (`journalctl -t zab-files`); sudo's own log records the command."""
    with suppress(OSError, ImportError):
        import syslog

        syslog.openlog("zab-files")
        syslog.syslog(syslog.LOG_WARNING, message)


def unshare_by_hand(store: Store, repo: str, share: str) -> int:
    """`server.py unshare REPO SHARE`: one share by its link, commit or name, or `all` of the
    repository's; each revoked at once, as the checks revoke one (`Store.unshare`)."""
    held = {sha: names for sha, _, _, names, _ in store.list_shares(repo)}
    match = re.search(r"/([0-9a-fA-F]{40})(?:/|$)", share)
    wanted = match.group(1).lower() if match else share.lower()
    shas = [sha for sha, names in held.items() if share == "all" or sha == wanted or share in names]
    if not shas:
        print(f"no such share of {repo.lower()}", file=sys.stderr)
        return 1
    failed = 0
    for sha in sorted(shas):
        names = ", ".join(held[sha])
        try:
            done = store.unshare(repo, sha, "revoked by hand")
            why = "it was revoked meanwhile"
        except FetchBusy:
            done, why = False, "the store is busy: try again"
        if done:
            print(f"revoked {names} ({sha[:12]}) of {repo.lower()}")
            journal(f"unshare: revoked {names} of {repo.lower()}")  # never the commit
        else:
            failed += 1
            print(f"not revoked: {names} ({sha[:12]}) of {repo.lower()}: {why}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
