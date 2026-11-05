"""A fake Zulip realm for the admin bots' tests: several bots in one process, each identified by
its key, with channels, subscribers, DMs and one event queue per registration
(docs/ADMIN.md §2.1). Offline and deterministic (SPEC §15.2)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

SITE = "https://zulip.example.com"


@dataclass
class Queue:
    owner: int
    event_types: list[str]
    events: list[dict[str, Any]] = field(default_factory=list)


class FakeRealm:
    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {}
        self.keys: dict[str, int] = {}  # email → user ID, for the bots' basic auth
        self.subscribers: dict[int, set[int]] = {}
        #: the channels that aren't private: `public` or `web-public` (`GET /streams/{id}`)
        self.privacy: dict[int, str] = {}
        self.queues: dict[str, Queue] = {}
        self.registers: list[tuple[int, list[str]]] = []  # (bot, its `fetch_event_types`)
        self.messages: dict[int, dict[str, Any]] = {}
        self.sent: list[tuple[int, dict[str, Any]]] = []  # (sender, POST /messages params)
        self.edits: list[tuple[int, int, str]] = []  # (editor, message ID, new content)
        self.requests: list[tuple[int, str, str]] = []
        self.uploads: dict[str, bytes] = {}
        self.forbidden: set[str] = set()  # uploads whose signed link answers 403
        self.me_override: dict[int, dict[str, Any]] = {}  # users/me answers, for the checks
        self._ids = {"message": 1000, "event": 0, "queue": 0}
        self.now = 1_800_000_000  # each message's `timestamp`, as a test sets it
        self.received: dict[int, list[int]] = {}  # user ID → the messages delivered to them
        self.broken: set[int] = set()  # users whose GET /messages and POST /messages answer 500
        #: POST /messages/{id}/reactions, as (user, message ID, params), the IDs unchecked: tests
        #: hand the bots messages the realm never saw (#466)
        self.reactions: list[tuple[int, int, dict[str, str]]] = []
        #: A test's reaction failure, awaited first: what it returns or raises, instead of success
        self.reacting: Callable[[], Awaitable[httpx.Response | None]] | None = None
        self._wake = asyncio.Event()
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------------- the realm

    def person(
        self, user_id: int, name: str, *, bot: bool = False, active: bool = True,
        system: bool = False,
    ) -> None:  # fmt: skip
        email = f"user{user_id}@example.com"
        self.users[user_id] = {
            "user_id": user_id,
            "full_name": name,
            "email": email,
            "is_bot": bot or system,
            "is_active": active,
        }
        if system:  # a cross-realm bot: not in GET /users/{id}
            self.users[user_id]["system"] = True
        if bot:
            self.keys[email] = user_id

    def zuliprc(self, user_id: int) -> str:
        email = self.users[user_id]["email"]
        return f"[api]\nsite={SITE}\nemail={email}\nkey=k{user_id:031d}\n"

    def channel(self, stream_id: int, *members: int) -> None:
        self.subscribers[stream_id] = set(members)

    def _next(self, kind: str) -> int:
        self._ids[kind] += 1
        return self._ids[kind]

    def emit(self, event: dict[str, Any], to: set[int] | None = None) -> None:
        """`event` into every queue (of the users in `to`) registered for its type."""
        for queue in self.queues.values():
            if (to is None or queue.owner in to) and event["type"] in queue.event_types:
                queue.events.append(event | {"id": self._next("event")})
        self._wake.set()
        self._wake = asyncio.Event()

    def _deliver(self, message: dict[str, Any], to: set[int]) -> None:
        message.setdefault("timestamp", int(self.now))
        self.messages[message["id"]] = message
        for uid in to:
            self.received.setdefault(uid, []).append(message["id"])
            mentioned = f"@**{self.users[uid]['full_name']}" in message["content"]
            flags = ["mentioned"] if mentioned else []
            self.emit({"type": "message", "message": dict(message), "flags": flags}, {uid})

    def say(self, sender: int, stream: int, content: str, topic: str = "ops") -> int:
        """A message from `sender` in a channel, delivered to its subscribers' queues."""
        mid = self._next("message")
        message = {
            "id": mid,
            "type": "stream",
            "stream_id": stream,
            "subject": topic,
            "content": content,
            "sender_id": sender,
            "sender_full_name": self.users[sender]["full_name"],
        }
        self._deliver(message, self.subscribers.get(stream, set()))
        return mid

    def dm(self, sender: int, content: str, *to: int) -> int:
        """A DM from `sender` to the users `to`."""
        mid = self._next("message")
        people = sorted({sender, *to})
        message = {
            "id": mid,
            "type": "private",
            "display_recipient": [{"id": uid} for uid in people],
            "content": content,
            "sender_id": sender,
            "sender_full_name": self.users[sender]["full_name"],
        }
        self._deliver(message, set(people))
        return mid

    def click(self, user: int, message_id: int, choice: str) -> int:
        """`user` clicks a widget's button, as Zulip's web client sends it
        (`transmit.reply_message`): the choice's reply, after a mention of the widget message's
        sender (`people.get_mention_syntax`), in its topic."""
        message = self.messages[message_id]
        reply = next(c["reply"] for c in message["widget"]["extra_data"]["choices"]
                     if c["short_name"] == choice)  # fmt: skip
        mention = f"@**{message['sender_full_name']}**"
        return self.say(user, message["stream_id"], f"{mention} {reply}", message["subject"])

    def posts(self, by: int | None = None) -> list[dict[str, Any]]:
        """What the bots posted (POST /messages params), oldest first."""
        return [params for uid, params in self.sent if by is None or uid == by]

    # ------------------------------------------------------------------------ transport

    def _who(self, request: httpx.Request) -> int:
        auth = request.headers.get("Authorization", "")
        email = base64.b64decode(auth.removeprefix("Basic ")).decode().split(":", 1)[0]
        return self.keys[email]

    @staticmethod
    def _ok(**body: Any) -> httpx.Response:
        return httpx.Response(200, json={"result": "success", **body})

    @staticmethod
    def _error(code: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"result": "error", "code": code, "msg": code})

    def upload(self, name: str, data: bytes) -> str:
        """A file someone attached: its `/user_uploads/…` path."""
        key = f"7/ab/{len(self.uploads) + 1}/{name}"
        self.uploads[key] = data
        return f"/user_uploads/{key}"

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/user_uploads/temporary/"):  # signed: no credentials
            assert "Authorization" not in request.headers
            key = request.url.path.removeprefix("/user_uploads/temporary/")
            if key in self.forbidden:
                return httpx.Response(403, content=b"expired link")
            data = self.uploads[key]
            return httpx.Response(200, content=data, headers={"Content-Type": "text/csv"})
        who = self._who(request)
        path = request.url.path.removeprefix("/api/v1/")
        if path == "user_uploads" and request.method == "POST":  # an attachment from a bot
            body = request.content
            start = body.index(b"\r\n\r\n") + 4
            data = body[start : body.rindex(b"\r\n--")]
            name = request.content.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            return self._ok(url=self.upload(name, data))
        raw = request.url.query if request.method == "GET" else request.content
        params = {k: v[-1] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items()}
        self.requests.append((who, request.method, path))
        if path == "users/me":
            return self._ok(**(self.me_override.get(who) or self.users[who]))
        if path.startswith("user_uploads/") and request.method == "GET":
            key = path.removeprefix("user_uploads/")
            if key not in self.uploads:
                return self._error("NOT_FOUND", 404)
            return self._ok(url=f"/user_uploads/temporary/{key}")
        if path.startswith("users/") and request.method == "GET":
            user = self.users.get(int(path.split("/")[1]))
            if user is None or user.get("system") or user.get("lookup_fails"):
                return self._error("BAD_REQUEST")
            return self._ok(user=user)
        if path.startswith("streams/") and path.endswith("/members"):
            stream = int(path.split("/")[1])
            if stream not in self.subscribers:
                return self._error("BAD_REQUEST")
            return self._ok(subscribers=sorted(self.subscribers[stream]))
        if path.startswith("streams/") and request.method == "GET":
            stream = int(path.split("/")[1])
            if stream not in self.subscribers:
                return self._error("BAD_REQUEST")
            kind = self.privacy.get(stream, "private")
            about = {"invite_only": kind == "private", "is_web_public": kind == "web-public"}
            return self._ok(stream={"stream_id": stream, **about})
        if path == "register":
            qid = str(self._next("queue"))
            self.queues[qid] = Queue(who, json.loads(params["event_types"]))
            state: dict[str, Any] = {}
            self.registers.append((who, json.loads(params.get("fetch_event_types", "[]"))))
            if "realm_user" in self.registers[-1][1]:
                state["cross_realm_bots"] = [
                    {"user_id": uid, "full_name": u["full_name"], "is_bot": True,
                     "is_system_bot": True}
                    for uid, u in self.users.items() if u.get("system")
                ]  # fmt: skip
            return self._ok(
                queue_id=qid,
                last_event_id=self._ids["event"],
                max_message_length=10000,
                max_topic_length=60,
                **state,
            )
        if path == "events":
            queue = self.queues.get(params["queue_id"])
            if queue is None or queue.owner != who:
                return self._error("BAD_EVENT_QUEUE_ID")
            last = int(params["last_event_id"])
            if not any(e["id"] > last for e in queue.events):
                # not `asyncio.wait_for`, which on 3.11 loses a cancel that comes as the
                # event is set (gh-86296): the bot's queue loop then never ended (#491)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(0.5):
                        await self._wake.wait()
            return self._ok(events=[e for e in queue.events if e["id"] > last])
        if path.startswith("messages/") and path.endswith("/reactions"):
            if self.reacting is not None and (answer := await self.reacting()) is not None:
                return answer
            self.reactions.append((who, int(path.split("/")[1]), params))
            return self._ok()
        if path.startswith("messages/") and request.method == "PATCH":  # an edit, by its sender
            mid = int(path.split("/")[1])
            if self.messages.get(mid, {}).get("sender_id") != who:
                return self._error("BAD_REQUEST")
            self.edits.append((who, mid, params["content"]))
            self.messages[mid]["content"] = params["content"]
            return self._ok()
        if path == "messages" and request.method == "GET":
            return self._history(who, params)
        if path == "messages" and request.method == "POST":
            if who in self.broken:
                return self._error("INTERNAL_SERVER_ERROR", 500)
            self.sent.append((who, params))
            mid = self._next("message")
            message = {
                "id": mid,
                "content": params["content"],
                "sender_id": who,
                "sender_full_name": self.users[who]["full_name"],
                "widget": json.loads(params["widget_content"])
                if "widget_content" in params
                else None,
            }
            if params["type"] == "stream":
                stream = int(params["to"])
                message |= {"type": "stream", "stream_id": stream, "subject": params["topic"]}
                self._deliver(message, self.subscribers.get(stream, set()))
            else:
                people = sorted({who, *json.loads(params["to"])})
                message |= {"type": "private", "display_recipient": [{"id": u} for u in people]}
                self._deliver(message, set(people))
            return self._ok(id=mid)
        return self._error("NOT_FOUND", 404)

    def _history(self, who: int, params: dict[str, str]) -> httpx.Response:
        """GET /messages as Zulip reads it without a channel narrow: only what `who` received,
        here narrowed by `sender` (a user ID); `num_before` up to the anchor, which counts when
        it is one of them; oldest first."""
        if who in self.broken:
            return self._error("INTERNAL_SERVER_ERROR", 500)
        narrow = {t["operator"]: t["operand"] for t in json.loads(params.get("narrow", "[]"))}
        assert set(narrow) <= {"sender"}, narrow
        ids = sorted(
            mid
            for mid in self.received.get(who, [])
            if "sender" not in narrow or self.messages[mid]["sender_id"] == narrow["sender"]
        )
        anchor = params["anchor"]
        top = ids[-1] if anchor == "newest" and ids else int(anchor) if anchor != "newest" else 0
        upto = [mid for mid in ids if mid <= top]
        assert int(params.get("num_after", "0")) == 0
        before = int(params["num_before"]) + (1 if top in upto else 0)
        page = upto[-before:] if before else []
        found_oldest = not ids or (bool(page) and page[0] == ids[0]) or not upto
        messages = [dict(self.messages[mid]) for mid in page]
        return self._ok(messages=messages, found_oldest=found_oldest, found_newest=True)
