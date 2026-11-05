"""Async Zulip REST transport (SPEC §4.10); no automatic POST retries."""

from __future__ import annotations

import configparser
import json
import logging
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from zab import __version__
from zab.core.metrics import Metrics
from zab.zulip.budget import RequestBudget


class ZulipError(Exception):
    def __init__(self, code: str, status: int, retry_after: float = 0) -> None:
        super().__init__(f"Zulip {code} (HTTP {status})")
        self.code = code
        self.status = status
        self.retry_after = retry_after


class AttachmentRejected(ValueError):
    """A permanent download refusal that should not block session input."""


class ZuliprcError(ValueError):
    """A zuliprc that doesn't parse, isn't UTF-8 or lacks `[api]`'s site, email or key: its
    `path` and `why`, in words of `from_zuliprc`'s own. A parse error is said by its type, never
    its message: configparser's quote the offending line, which may be `key=…` (#527)."""

    def __init__(self, path: str, why: str) -> None:
        super().__init__(f"{path}: {why}")
        self.path, self.why = path, why


#: Bounds on the edits of each message (`EditBounds`; SPEC §4.9, §4.10; issue #754)
GAP = 60.0  # seconds between two edits of one message, a final edit excepted
MAX_EDITS = 20  # edits of one message, a final edit included
KEPT = 4096  # messages remembered, the most recently edited; one forgotten starts again


class EditRefused(Exception):
    """An edit a bound forbids; never sent. Not a `ZulipError`: nothing posts it anew instead."""


class EditBounds:
    """Zulip keeps every earlier version of an edited message, and Zulip Cloud's guidance is to
    edit a message only for a change that matters, and to send programmatic updates about once
    a minute at most (#754). Every edit this process makes goes through `ZulipClient.edit`,
    within these bounds:

    - two edits of one message are at least `GAP` seconds apart. A *final* edit may go sooner:
      a turn's summary, or the answer on an approval request. It is that message's last edit, so
      it adds one at most, and holding it back would show a finished turn as running;
    - a message gets at most `MAX_EDITS` edits, a final one included. The others stop one
      short, so that the final edit always has its slot.

    `Poster.edit` holds an edit back until its gap has passed, merged with any later one of the
    same message, and drops one past the cap; `ZulipClient.edit` refuses what still breaks a
    bound. Kept in memory: a restart starts every message's count again."""

    def __init__(self, budget: RequestBudget) -> None:
        self.budget = budget  # its clock: a test's, if set
        self.messages: dict[int, tuple[int, float]] = {}  # id -> edits, last; oldest first

    def edits(self, message_id: int) -> int:
        return self.messages.get(message_id, (0, 0.0))[0]

    def wait(self, message_id: int, *, final: bool = False) -> float | None:
        """Seconds until an edit of `message_id` may be sent: 0 now, None never (the cap)."""
        if message_id not in self.messages:
            return 0.0
        edits, last = self.messages[message_id]
        if edits >= MAX_EDITS or (not final and edits >= MAX_EDITS - 1):
            return None
        return 0.0 if final else max(0.0, last + GAP - self.budget._monotonic())

    def last_update(self, message_id: int) -> bool:
        """Whether the next edit is the last one before only a final edit is left."""
        return self.edits(message_id) == MAX_EDITS - 2

    def record(self, message_id: int) -> tuple[int, float] | None:
        """Count an edit about to be sent; what `undo` needs if it never lands."""
        before = self.messages.pop(message_id, None)
        self.messages[message_id] = ((before or (0, 0.0))[0] + 1, self.budget._monotonic())
        while len(self.messages) > KEPT:
            del self.messages[next(iter(self.messages))]
        return before

    def undo(self, message_id: int, before: tuple[int, float] | None) -> None:
        self.messages.pop(message_id, None)
        if before is not None:
            self.messages[message_id] = before


def error_kind(exc: BaseException) -> str:
    """What a caller may say of `exc` without its message, which may quote a secrets file: a
    ZuliprcError's `why` (a parse error's type, as before #527), else the exception's type."""
    return exc.why if isinstance(exc, ZuliprcError) else type(exc).__name__


_TLS: ssl.SSLContext | None = None


def tls() -> ssl.SSLContext:
    """The process's one TLS context, httpx's default, for every client (#717): httpx would
    build one per client, about 0.7 MB each with its root certificates loaded. Two per bot made
    170 MB of the waker's 118 bots."""
    global _TLS
    if _TLS is None:
        _TLS = httpx.create_ssl_context()
    return _TLS


class ZulipClient:
    def __init__(
        self, site: str, email: str, key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not site.startswith("https://"):
            raise ValueError("Zulip site must use HTTPS")
        self.site = site.rstrip("/")
        self.email = email  # the bot's address, as narrows name a sender; never its key
        verify = tls() if transport is None else True  # a test's transport makes no TLS
        self.http = httpx.AsyncClient(
            base_url=self.site + "/api/v1/",
            auth=(email, key),
            headers={"User-Agent": f"zab/{__version__}"},
            timeout=httpx.Timeout(90, connect=10),
            transport=transport,
            verify=verify,
            follow_redirects=False,
        )
        self.metrics = Metrics()
        self.budget = RequestBudget()
        self.edit_bounds = EditBounds(self.budget)
        self.remaining: int | None = None
        # Signed download URLs must never receive the bot's API credentials.
        self.download_http = httpx.AsyncClient(transport=transport, timeout=60, verify=verify)
        self.reset_at = 0.0
        self.limits: dict[str, int] = {}

    @classmethod
    def from_zuliprc(cls, path: str | Path, **kwargs: Any) -> ZulipClient:
        """A client from the zuliprc at `path`. One that doesn't parse, isn't UTF-8 or lacks
        `[api]`'s site, email or key is a ZuliprcError, raised outside the handler and `from
        None`, so that no traceback chains configparser's error, which quotes the line (#527).
        A file that can't be opened is the OSError, which names only the path."""
        config = configparser.ConfigParser(interpolation=None)
        try:
            with Path(path).expanduser().open(encoding="utf-8") as stream:
                config.read_file(stream)
        except (configparser.Error, UnicodeDecodeError) as exc:
            why = type(exc).__name__
        else:
            missing = [n for n in ("site", "email", "key") if not config.has_option("api", n)]
            if not missing:
                api = config["api"]  # no local holds the key itself
                return cls(api["site"], api["email"], api["key"], **kwargs)
            why = f"no {missing[0]} in [api]" if config.has_section("api") else "no [api] section"
        raise ZuliprcError(str(path), why) from None

    async def close(self) -> None:
        await self.http.aclose()
        await self.download_http.aclose()

    async def download(self, temporary_url: str, limit: int) -> tuple[bytes, str]:
        url = urljoin(self.site + "/", temporary_url)
        for _ in range(6):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username:
                raise AttachmentRejected("attachment download must use HTTPS without credentials")
            async with self.download_http.stream("GET", url) as response:
                if response.has_redirect_location:
                    url = urljoin(url, response.headers["Location"])
                    continue
                response.raise_for_status()
                if int(response.headers.get("Content-Length", "0")) > limit:
                    raise AttachmentRejected("attachment exceeds the configured size limit")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise AttachmentRejected("attachment exceeds the configured size limit")
                    data.extend(chunk)
                return bytes(data), response.headers.get("Content-Type", "").split(";", 1)[0]
        raise AttachmentRejected("too many attachment redirects")

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        _sending: Any = None,  # Callable[[], None] | None
        **params: Any,
    ) -> dict[str, Any]:
        """`_sending`, called once the budget lets the request go, just before it is sent (an
        edit's bounds, `edit`), may raise to stop it."""
        # Endpoints are API-relative, never URLs received from agent output.
        decoded = unquote(endpoint)
        if (
            decoded.startswith("/")
            or ":" in decoded.split("/", 1)[0]
            or any(part in (".", "..") for part in decoded.split("/"))
        ):
            raise ValueError("expected a relative Zulip API endpoint")
        encoded = {
            key: json.dumps(value) if not isinstance(value, str) else value
            for key, value in params.items()
        }
        kwargs = {"params": encoded} if method == "GET" else {"data": encoded}
        await self.budget.acquire()
        if _sending is not None:
            _sending()
        self.metrics.counts["zulip_requests"] += 1
        response = await self.http.request(method, endpoint, **kwargs)
        return self._response(response)

    async def upload(self, name: str, data: bytes) -> str:
        """Upload bytes using the same response/rate-limit handling as other API calls."""
        await self.budget.acquire()
        self.metrics.counts["zulip_requests"] += 1
        response = await self.http.post("user_uploads", files={"file": (name, data)})
        result = self._response(response)
        return result.get("url") or result["uri"]

    def _response(self, response: httpx.Response) -> dict[str, Any]:
        if "X-RateLimit-Remaining" in response.headers:
            self.remaining = int(response.headers["X-RateLimit-Remaining"])
        if "X-RateLimit-Reset" in response.headers:
            self.reset_at = float(response.headers["X-RateLimit-Reset"])
        if self.remaining == 0:
            self.budget.blocked_until = max(self.budget.blocked_until, self.reset_at)
        retry_after = float(response.headers.get("Retry-After", 0))
        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry-after", retry_after))
            except (ValueError, TypeError, AttributeError):
                pass  # Fall back to the response header, without logging its body.
            self.metrics.counts["zulip_429"] += 1
            self.budget.blocked_until = max(
                self.budget.blocked_until, self.budget.wall() + max(0.01, retry_after)
            )
            logging.getLogger("zab.zulip").warning(
                "Zulip rate limit",
                extra={
                    "event": "zulip_429",
                    "bot": self.metrics.bot,
                    "backend": self.metrics.backend,
                    "retry_after": retry_after,
                },
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ZulipError(
                "INVALID_RESPONSE",
                response.status_code,
                float(response.headers.get("Retry-After", 0)),
            ) from exc
        if response.is_error or data.get("result") == "error":
            raise ZulipError(
                data.get("code", "HTTP_ERROR"),
                response.status_code,
                float(data.get("retry-after", response.headers.get("Retry-After", 0))),
            )
        return data

    async def register(self) -> dict[str, Any]:
        data = await self.request(
            "POST",
            "register",
            # "stream": a channel made public or private forgets its cached privacy.
            event_types=[
                "message",
                "reaction",
                "update_message",
                "subscription",
                "stream",
                "realm_user",
            ],
            fetch_event_types=["realm", "subscription"],
            apply_markdown=False,
            client_gravatar=True,
            include_subscribers=True,
        )
        for key in ("max_message_length", "max_topic_length", "max_file_upload_size_mib"):
            if type(data.get(key)) is not int or data[key] <= 0:
                raise ZulipError(f"MISSING_{key.upper()}", 200)
            self.limits[key] = data[key]
        return data

    async def events(
        self, queue_id: str, last_event_id: int, *, dont_block: bool = False
    ) -> list[dict[str, Any]]:
        """Long-poll the queue; `dont_block` answers at once, to probe it after an outage."""
        params: dict[str, Any] = {"queue_id": queue_id, "last_event_id": last_event_id}
        if dont_block:
            params["dont_block"] = True
        data = await self.request("GET", "events", **params)
        return data["events"]

    async def send(self, stream_id: int, topic: str, content: str) -> int:
        if len(content) > self.limits["max_message_length"]:
            raise ValueError("message exceeds the server limit")
        data = await self.request(
            "POST",
            "messages",
            type="stream",
            to=stream_id,
            topic=topic[: self.limits["max_topic_length"]],
            content=content,
        )
        return data["id"]

    async def edit(self, message_id: int, content: str, *, final: bool = False) -> None:
        """PATCH /messages/{id}: every edit of a message, the bridge's and the admin bot's, goes
        through here, within its bounds (`EditBounds`, #754). They are checked again, and the edit
        counted, once the request budget lets it go, just before it is sent: a wait for the budget
        never shortens the gap (codex round 3). One that Zulip refused with a 4xx never landed and
        doesn't count, while any other failure, a lost answer included, may have landed and does."""
        if len(content) > self.limits.get("max_message_length", 10000):
            raise ValueError("message exceeds the server limit")
        if self.edit_bounds.wait(message_id, final=final) != 0:
            raise EditRefused(message_id)
        counted: list[tuple[int, float] | None] = []

        def sending() -> None:
            if self.edit_bounds.wait(message_id, final=final) != 0:
                raise EditRefused(message_id)
            counted.append(self.edit_bounds.record(message_id))

        try:
            await self.request("PATCH", f"messages/{message_id}", _sending=sending, content=content)
        except ZulipError as exc:
            if counted and 400 <= exc.status < 500:
                self.edit_bounds.undo(message_id, counted[0])
            raise
