"""A test user on Zulip (`zab-tester`, `zab-tester2`; SPEC §15.3), as the scenarios drive it.

It posts and reacts like a person, and waits for the bots by polling `GET /messages` with raw
Markdown (`apply_markdown=false`), so patterns match what the bridge sent. It posts only in the
rig's channels and in DMs to the rig's bots (the dev org's `zab-dev*` channels and the test
participant's only): `restrict()` names them, and
anything else, or anything before it, raises `PostingRefused`, as `TestEc2` refuses untagged
instances. A transient Zulip or network error during a wait is retried with backoff until the
deadline, each retry logged. The zuliprc is read by `zab.zulip.client`; its key is never
printed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

import httpx

from e2e.harness.secret_files import label_of, read_secret
from zab.zulip.client import ZulipClient, ZulipError

T = TypeVar("T")
Narrow = list[dict[str, Any]]
LOG = logging.getLogger("zab.e2e")


class PostingRefused(RuntimeError):
    """A post the rig may not make: outside its channels and its bots' DMs."""


def transient(exc: BaseException) -> bool:
    """A failure worth retrying: rate limited, a server error, a broken or timed-out
    connection. A 4xx answer (a bad narrow, a missing message) is not, nor is an answer that
    isn't JSON on any other status, nor a URL scheme httpx can't speak (reviewer on #289)."""
    if isinstance(exc, ZulipError):  # INVALID_RESPONSE too: it carries the HTTP status
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, httpx.UnsupportedProtocol):
        return False
    return isinstance(exc, httpx.TransportError | TimeoutError | ConnectionError)


def zulip_client(path: Path) -> ZulipClient:
    """A client from the zuliprc at `path`. One that can't be read or parsed is a
    SecretFileError naming the file, never quoting it: configparser quotes the line (#519)."""
    return read_secret(label_of(path), ZulipClient.from_zuliprc, path)


def dm_narrow(user_id: int) -> Narrow:
    """The one-to-one DM conversation with `user_id`."""
    return [{"operator": "dm", "operand": [user_id]}]


def topic_narrow(stream_id: int, topic: str) -> Narrow:
    return [
        {"operator": "channel", "operand": stream_id},
        {"operator": "topic", "operand": topic},
    ]


def mention(full_name: str, user_id: int) -> str:
    """`@**Name|id**`: unambiguous even when two users share a name."""
    return f"@**{full_name}|{user_id}**"


class TestUser:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        client: ZulipClient,
        *,
        every: float = 3.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client, self.every, self.sleep, self.clock = client, every, sleep, clock
        self._me: dict[str, Any] | None = None
        self._users: list[dict[str, Any]] | None = None
        # nothing may be posted until restrict() names the rig's channels and bots
        self.channels: frozenset[int] = frozenset()
        self.bots: frozenset[int] = frozenset()
        self._sent: set[int] = set()

    def restrict(self, *, channels: Iterable[int], bots: Iterable[int]) -> None:
        """The only places this user may post: these channels, and DMs to these bots."""
        self.channels, self.bots = frozenset(channels), frozenset(bots)

    @classmethod
    def from_zuliprc(cls, path: Path, **kwargs: Any) -> TestUser:
        return cls(zulip_client(path), **kwargs)

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------------ lookups

    async def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = await self.client.request("GET", "users/me")
        return self._me

    async def user_id(self, full_name: str) -> int:
        """The one active user (bots included) with this full name."""
        if self._users is None:
            self._users = (await self.client.request("GET", "users"))["members"]
        found = [
            u["user_id"]
            for u in self._users
            if u.get("full_name") == full_name and u.get("is_active", True)
        ]
        if len(found) != 1:
            raise LookupError(f"expected one user named {full_name!r}, found {len(found)}")
        return int(found[0])

    async def bot_id(self, full_name: str) -> int:
        """The one active bot with this full name: a person's name is refused, so that the
        posting guard never allows DMs to a person (reviewer on #289)."""
        uid = await self.user_id(full_name)
        user = next(u for u in self._users or [] if u["user_id"] == uid)
        if user.get("is_bot") is not True:
            raise PostingRefused(f"{full_name!r} is not a bot: the rig DMs its bots only")
        return uid

    async def stream_id(self, name: str) -> int:
        return int((await self.client.request("GET", "get_stream_id", stream=name))["stream_id"])

    async def humans(self, stream_id: int) -> set[int]:
        """The channel's active human members: more than one makes it multi-party for the
        bridge (#35), where only a mention addresses a bot."""
        members = (await self.client.request("GET", f"streams/{stream_id}/members"))["subscribers"]
        if self._users is None:
            self._users = (await self.client.request("GET", "users"))["members"]
        people = {u["user_id"]: u for u in self._users}
        return {
            uid
            for uid in members
            if people.get(uid, {}).get("is_bot") is False and people[uid].get("is_active", True)
        }

    # -------------------------------------------------------------------------- posting

    async def send_dm(self, user_id: int, content: str) -> int:
        if user_id not in self.bots:
            raise PostingRefused(f"a DM to {user_id}, not one of the rig's bots")
        data = await self.client.request(
            "POST", "messages", type="private", to=[user_id], content=content
        )
        self._sent.add(int(data["id"]))
        return int(data["id"])

    async def send(
        self, stream_id: int, topic: str, content: str, *, widget: dict[str, Any] | None = None
    ) -> int:
        """A post in one of the rig's channels; with `widget`, as its `widget_content` (the
        admin suite's fake `zform`, #356)."""
        if stream_id not in self.channels:
            raise PostingRefused(f"channel {stream_id} is not one of the rig's")
        extra = {"widget_content": widget} if widget is not None else {}
        data = await self.client.request(
            "POST", "messages", type="stream", to=stream_id, topic=topic, content=content, **extra
        )
        self._sent.add(int(data["id"]))
        return int(data["id"])

    async def edit(self, message_id: int, content: str) -> None:
        if message_id not in self._sent:  # sent by this user, in the rig's places
            raise PostingRefused(f"message {message_id} was not posted by this run")
        await self.client.request("PATCH", f"messages/{message_id}", content=content)

    async def react(self, message_id: int, emoji_name: str) -> None:
        msg = await self.message(message_id)
        if msg.get("stream_id") is not None:
            allowed = msg["stream_id"] in self.channels
        else:  # a DM: between this user and one of the rig's bots only
            me = (await self.me())["user_id"]
            people = {r.get("id") for r in msg.get("display_recipient") or []}
            allowed = bool(people & self.bots) and people <= self.bots | {me}
        if not allowed:
            raise PostingRefused(f"message {message_id} is outside the rig's channels and DMs")
        await self.client.request("POST", f"messages/{message_id}/reactions", emoji_name=emoji_name)

    # -------------------------------------------------------------------------- reading

    async def message(self, message_id: int) -> dict[str, Any]:
        data = await self.client.request("GET", f"messages/{message_id}", apply_markdown=False)
        return dict(data["message"])

    async def after(self, narrow: Narrow, after_id: int, page: int = 200) -> list[dict[str, Any]]:
        """Every message of `narrow` newer than `after_id`, oldest first, as they are now: page
        after page until Zulip says it reached the newest (codex on #277: a reply past the
        first page, or a sender there, must count)."""
        out: list[dict[str, Any]] = []
        anchor = after_id
        while True:
            data = await self.client.request(
                "GET",
                "messages",
                anchor=str(anchor),
                include_anchor=False,
                num_before=0,
                num_after=page,
                narrow=json.dumps(narrow),
                apply_markdown=False,
            )
            batch = [m for m in data["messages"] if m["id"] > anchor]
            out.extend(batch)
            newest = data.get("found_newest")
            if newest or not batch or (newest is None and len(batch) < page):
                return out
            anchor = batch[-1]["id"]

    async def wait_for(
        self, probe: Callable[[], Awaitable[T | None]], *, within: float, what: str
    ) -> T:
        """Poll `probe` every `every` seconds until it returns something; TimeoutError after
        `within` seconds, naming `what`. A transient error is retried (`retrying`)."""
        deadline = self.clock() + within
        while True:
            found = await self.retrying(probe, until=deadline, what=what)
            if found is not None:
                return found
            if self.clock() >= deadline:
                raise TimeoutError(f"no {what} within {within:.0f} s")
            await self.sleep(self.every)

    async def retrying(self, call: Callable[[], Awaitable[T]], *, until: float, what: str) -> T:
        """`call()`, again after a transient error (`transient`), with a growing pause (the poll
        interval doubled each time, at most 60 s), logged each time, until `until`; then, or
        on any other error, the error is raised."""
        failures = 0
        while True:
            try:
                return await call()
            except Exception as exc:
                if not transient(exc) or self.clock() >= until:
                    raise
                failures += 1
                pause = min(self.every * 2**failures, 60.0, max(0.0, until - self.clock()))
                LOG.warning(
                    "e2e: %s while waiting for %s; retry %d in %.0f s",
                    exc if isinstance(exc, ZulipError) else type(exc).__name__,
                    what,
                    failures,
                    pause,
                )
                await self.sleep(pause)

    async def wait_reply(
        self,
        narrow: Narrow,
        *,
        after_id: int,
        senders: Iterable[int],
        pattern: str,
        within: float,
    ) -> dict[str, Any]:
        """The first message newer than `after_id` from one of `senders` whose raw content
        matches `pattern` (a regular expression; edits count, as the bridge edits its status)."""
        wanted, regex = set(senders), re.compile(pattern, re.S)

        async def probe() -> dict[str, Any] | None:
            for msg in await self.after(narrow, after_id):
                if msg["sender_id"] in wanted and regex.search(msg["content"]):
                    return msg
            return None

        return await self.wait_for(probe, within=within, what=f"reply matching {pattern!r}")

    async def wait_reaction(
        self, message_id: int, *, emoji_code: str, user_id: int, within: float
    ) -> dict[str, Any]:
        """The reaction `emoji_code` (`21aa` is ↪) by `user_id` on the message."""

        async def probe() -> dict[str, Any] | None:
            for r in (await self.message(message_id)).get("reactions", []):
                if r.get("emoji_code") == emoji_code and r.get("user_id") == user_id:
                    return dict(r)
            return None

        return await self.wait_for(probe, within=within, what=f"reaction {emoji_code}")

    async def silent(
        self, narrow: Narrow, *, after_id: int, senders: Iterable[int], seconds: float
    ) -> list[dict[str, Any]]:
        """Wait `seconds`, then everything `senders` did about message `after_id`: their
        messages newer than it, and their reactions on it, each reaction as `{"sender_id",
        "content": "reaction :name:", "reaction"}` (reviewer on #284). None is the pass of a
        negative test."""
        await self.sleep(seconds)
        wanted, until = set(senders), self.clock() + 120
        messages = await self.retrying(
            lambda: self.after(narrow, after_id), until=until, what="the messages since"
        )
        target = await self.retrying(
            lambda: self.message(after_id), until=until, what="the message's reactions"
        )
        reactions = [
            {"sender_id": r["user_id"], "content": f"reaction :{r.get('emoji_name', '?')}:",
             "reaction": r}
            for r in target.get("reactions", [])
            if r.get("user_id") in wanted
        ]  # fmt: skip
        return [m for m in messages if m["sender_id"] in wanted] + reactions
