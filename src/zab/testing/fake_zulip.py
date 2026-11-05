"""Deterministic in-memory Zulip transport for offline tests (SPEC §15.2)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.parse import parse_qs

import httpx

#: What a `with` narrow keeps the same as its message's: Zulip's conversation, in a channel.
CONVERSATION = ("type", "stream_id", "subject")


class FakeZulip:
    def __init__(self) -> None:
        self.messages: dict[int, dict[str, Any]] = {}
        self.uploads: dict[str, tuple[bytes, str]] = {}
        self.queues: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.failures: list[tuple[int, dict[str, Any]]] = []
        self.refused_edits: set[int] = set()
        self.user = {
            "user_id": 1,
            "full_name": "Test Bot",
            "email": "test-bot@example.zulipchat.com",
            "is_bot": True,
            "bot_owner_id": 2,
        }
        self.members = [self.user]
        self.subscribers: dict[int, list[int]] = {1: [1, 20]}
        self.streams = {
            1: {"stream_id": 1, "name": "zab-dev", "invite_only": True, "is_web_public": False}
        }
        self._queue_id = 0
        self._message_id = 0
        self._event_id = 0
        # An outage: every request fails as when the network refuses connections.
        self.offline = False
        # A long poll: GET /events waits up to this long for an event (0: answers at once).
        self.poll_timeout = 0.0
        # An outage that begins as the poll carrying a message with this text returns: the
        # bridge has the message, and its next request fails (scenario 4, issue #532).
        self.cut_after: str | None = None
        # Zulip answers everything else, polls included, and fails each `POST /messages` with
        # this status: a 5xx, or a 4xx refusal (issue #534).
        self.post_failure: int | None = None
        # A server that refuses a negated narrow (`BAD_NARROW`), as one might (#717).
        self.refuse_negated = False
        self._wake = asyncio.Event()
        self.transport = httpx.MockTransport(self.handle)

    def emit(self, event: dict[str, Any]) -> None:
        self._event_id += 1
        for queue in self.queues.values():
            queue.append(event | {"id": self._event_id})
        self._wake.set()
        self._wake = asyncio.Event()

    def lose_queues(self) -> None:
        self.queues.clear()

    def post_as(self, sender_id: int, content: str, topic: str | None = None) -> dict[str, Any]:
        """Another user's message in channel 1, or a DM to the bot when `topic` is None;
        it reaches the server even while the bridge is `offline`."""
        self._message_id += 1
        message = {
            "id": self._message_id,
            "type": "private" if topic is None else "stream",
            "stream_id": None if topic is None else 1,
            "subject": topic or "",
            "content": content,
            "sender_id": sender_id,
            "sender_email": next(
                (
                    m["email"]
                    for m in self.members
                    if m.get("user_id") == sender_id and "email" in m
                ),
                f"user{sender_id}@example.test",
            ),
            "timestamp": time.time(),
            "flags": ["mentioned"],
            "display_recipient": "zab-dev"
            if topic is not None
            else [{"id": self.user["user_id"]}, {"id": sender_id}],
            "reactions": [],
        }
        self.messages[message["id"]] = message
        self.emit({"type": "message", "message": dict(message), "flags": ["mentioned"]})
        return message

    @staticmethod
    def email(request: httpx.Request) -> str | None:
        """The e-mail the request signs in with."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return base64.b64decode(token).decode().partition(":")[0] if scheme == "Basic" else None

    def me(self, request: httpx.Request) -> dict[str, Any]:
        """The member whose e-mail the request signs in with (two bots on one fake, #683),
        who sends its messages and reactions (#712); else `user`, the one bot of most tests."""
        email = self.email(request)
        return next((m for m in self.members if m.get("email") == email), self.user)

    def rate_limit_once(self, retry_after: float = 0) -> None:
        self.failures.append((429, {"code": "RATE_LIMIT_HIT", "retry-after": retry_after}))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        path = request.url.path.removeprefix("/api/v1/")
        if path == "user_uploads" and request.method == "POST":
            self.requests.append((request.method, path, {}))
            if self.failures:
                status, body = self.failures.pop(0)
                return httpx.Response(status, json={"result": "error", **body})
            part = (
                BytesParser(policy=policy.default)
                .parsebytes(
                    b"Content-Type: "
                    + request.headers["content-type"].encode()
                    + b"\r\n\r\n"
                    + request.content
                )
                .get_payload(0)
            )
            key = f"1/{len(self.uploads) + 1}/{part.get_filename()}"
            self.uploads[key] = (part.get_payload(decode=True), part.get_content_type())
            return httpx.Response(200, json={"result": "success", "url": "/user_uploads/" + key})
        raw = request.url.query if request.method == "GET" else request.content
        params = {k: v[-1] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items()}
        self.requests.append((request.method, path, params))
        if self.failures:
            status, body = self.failures.pop(0)
            return httpx.Response(status, json={"result": "error", **body})
        result: dict[str, Any] = {}
        if path.startswith("/user_uploads/temporary/"):
            assert "Authorization" not in request.headers
            upload = path.removeprefix("/user_uploads/temporary/")
            data, mime = self.uploads[upload]
            return httpx.Response(200, content=data, headers={"Content-Type": mime})
        if path.startswith("user_uploads/") and request.method == "GET":
            upload = path.removeprefix("user_uploads/")
            if upload not in self.uploads:
                return httpx.Response(404, json={"result": "error", "code": "NOT_FOUND"})
            result = {"url": "/user_uploads/temporary/" + upload}
        elif path == "users":
            result = {"members": self.members}
        elif path == "users/me":
            result = self.me(request)
        elif path.startswith("users/"):
            result = {
                "user": self.user
                | {
                    "user_id": int(path.split("/")[1]),
                    "is_bot": int(path.split("/")[1]) == self.user["user_id"],
                }
            }
        elif path.startswith("streams/") and path.endswith("/members"):
            result = {"subscribers": self.subscribers.get(int(path.split("/")[1]), [])}
        elif path.startswith("streams/"):
            result = {"stream": self.streams[int(path.split("/")[1])]}
        elif path == "register":
            assert "realm" in json.loads(params["fetch_event_types"])
            self._queue_id += 1
            qid = str(self._queue_id)
            self.queues[qid] = []
            result = {
                "queue_id": qid,
                "last_event_id": self._event_id,
                "max_message_length": 10000,
                "max_topic_length": 60,
                "max_file_upload_size_mib": 25,
                "zulip_feature_level": 1,
            }
            if "message" in json.loads(params["fetch_event_types"]):
                result["max_message_id"] = max(self.messages, default=-1)
        elif path == "events":
            await asyncio.sleep(0)
            qid, last = params["queue_id"], int(params["last_event_id"])
            if qid in self.queues and self.poll_timeout and params.get("dont_block") != "true":
                wake = self._wake
                if not any(e["id"] > last for e in self.queues[qid]):
                    # not `asyncio.wait_for`: on 3.11 it loses a cancel that comes as `wake`
                    # is set (gh-86296), and the listener polls on (#491)
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(self.poll_timeout):
                            await wake.wait()
                if self.offline:  # the outage began during the long poll
                    raise httpx.ReadError("connection reset", request=request)
            if qid not in self.queues:
                return httpx.Response(400, json={"result": "error", "code": "BAD_EVENT_QUEUE_ID"})
            result = {"events": [e for e in self.queues[qid] if e["id"] > last]}
            if self.cut_after and any(
                self.cut_after in e.get("message", {}).get("content", "") for e in result["events"]
            ):
                self.offline, self.cut_after = True, None
        elif path == "messages" and request.method == "POST":
            if self.post_failure:
                code = "SERVER_ERROR" if self.post_failure >= 500 else "BAD_REQUEST"
                return httpx.Response(self.post_failure, json={"result": "error", "code": code})
            self._message_id += 1
            mid = self._message_id
            self.messages[mid] = {
                "id": mid,
                "stream_id": int(params["to"]) if params["type"] == "stream" else None,
                "subject": params.get("topic", ""),
                "content": params["content"],
                "sender_id": self.me(request)["user_id"],
                "sender_email": self.me(request).get(
                    "email", f"user{self.me(request)['user_id']}@example.test"
                ),
                "type": params["type"],
                "timestamp": time.time(),
                "reactions": [],
            }
            self.emit({"type": "message", "message": dict(self.messages[mid]), "flags": []})
            result = {"id": mid}
        elif path == "messages":
            narrow = json.loads(params.get("narrow", "[]"))
            messages = list(self.messages.values())
            for term in narrow:
                if term["operator"] == "with":  # the conversation the message is in
                    if (anchor := self.messages.get(int(term["operand"]))) is not None:
                        messages = [
                            m
                            for m in messages
                            if all(m.get(k) == anchor.get(k) for k in CONVERSATION)
                        ]
                    continue
                key = {"stream": "stream_id", "channel": "stream_id", "sender": "sender_id"}.get(
                    term["operator"], "subject"
                )
                operand = term["operand"]
                if key == "sender_id" and not str(operand).isdecimal():  # an address, as Zulip
                    member = self.me(request) if operand == self.email(request) else next(
                        (m for m in self.members if m.get("email") == operand), None
                    )  # fmt: skip
                    if member is None:
                        return httpx.Response(400, json={"result": "error", "code": "BAD_NARROW"})
                    operand = member["user_id"]
                negated = term.get("negated", False)
                if negated and self.refuse_negated:
                    return httpx.Response(400, json={"result": "error", "code": "BAD_NARROW"})
                messages = [m for m in messages if (str(m[key]) == str(operand)) != negated]
            anchor = (
                max((m["id"] for m in messages), default=0) + 1
                if params.get("anchor") == "newest"
                else 0
                if params.get("anchor") == "oldest"
                else int(params.get("anchor", 0))
            )
            before = sorted([m for m in messages if m["id"] < anchor], key=lambda m: m["id"])
            count = int(params.get("num_before", 0))
            after = sorted([m for m in messages if m["id"] > anchor], key=lambda m: m["id"])
            result = {
                "messages": (before[-count:] if count else [])
                + after[: int(params.get("num_after", 100))]
            }
        elif path.startswith("messages/"):
            mid = int(path.split("/")[1])
            if mid not in self.messages and request.method == "GET":
                # A deleted message, or one the bot can no longer access.
                return httpx.Response(400, json={"result": "error", "code": "BAD_REQUEST"})
            if path.endswith("/reactions"):
                reaction = {"user_id": self.me(request)["user_id"], **params}
                if mid not in self.messages:
                    return httpx.Response(400, json={"result": "error", "code": "BAD_REQUEST"})
                if reaction in self.messages[mid]["reactions"]:
                    return httpx.Response(
                        400, json={"result": "error", "code": "REACTION_ALREADY_EXISTS"}
                    )
                self.messages[mid]["reactions"].append(reaction)
                self.emit({"type": "reaction", "op": "add", "message_id": mid, **reaction})
            elif request.method == "PATCH":
                if mid in self.refused_edits:
                    return httpx.Response(400, json={"result": "error", "code": "BAD_REQUEST"})
                if "topic" in params:
                    params["subject"] = params.pop("topic")
                if "stream_id" in params:
                    params["stream_id"] = int(params["stream_id"])
                self.messages[mid].update(params)
                event = {"type": "update_message", "message_id": mid, "message_ids": [mid]}
                event.update(params)
                if "stream_id" in params:
                    event["new_stream_id"] = params["stream_id"]
                self.emit(event)
            result = {"message": self.messages[mid]}
        elif path != "typing":
            return httpx.Response(404, json={"result": "error", "code": "NOT_FOUND"})
        return httpx.Response(200, json={"result": "success", **result})
