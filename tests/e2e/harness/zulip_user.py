"""A test user on Zulip (`zab-tester`, `zab-tester2`; SPEC §15.3), as the scenarios drive it.

It posts and reacts like a person, and waits for the bots by polling `GET /messages` with raw
Markdown (`apply_markdown=false`), so patterns match what the bridge sent. It posts only in the
rig's channels and in DMs to the rig's bots (the dev org's `zab-dev*` channels and the test
participant's only): `restrict()` names them, and
anything else, or anything before it, raises `PostingRefused`, as `TestEc2` refuses untagged
instances. A transient Zulip or network error during a wait is retried with backoff until the
deadline, each retry logged. A wait that times out says for how long the rig's calls failed, and
one that failed on the rig's side, not the bridge's, is a `RigTimeout` (`harness.rig_failure`,
#877). The zuliprc is read by `zab.zulip.client`; its key is never printed.
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

from e2e.harness.refusal import Refused, refusal
from e2e.harness.rig_failure import Outage, RigFailure, RigTimeout, earlier, shown, stamp, utc
from e2e.harness.secret_files import label_of, read_secret
from zab.zulip.client import ZulipClient, ZulipError

T = TypeVar("T")
Narrow = list[dict[str, Any]]
LOG = logging.getLogger("zab.e2e")
#: How long a wait that timed out waits for the connection to come back, to read what it missed
BACK_S = 120.0
#: The key under which a message read from Zulip (`after`, `message`) carries the rig's time
#: (epoch seconds) just before the request that read it: what `settled` sets an edit beside
ASKED = "_asked"


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
        wall: Callable[[], float] = time.time,  # beside Zulip's timestamps
    ) -> None:
        self.client, self.every, self.sleep, self.clock = client, every, sleep, clock
        self.wall = wall
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
        """The message as Zulip serves it now, stamped with `ASKED`."""
        asked = self.wall()
        data = await self.client.request("GET", f"messages/{message_id}", apply_markdown=False)
        return {**data["message"], ASKED: asked}

    async def history(self, message_id: int) -> list[dict[str, Any]]:
        """The message's versions, oldest first, each with its raw `content` and `timestamp`."""
        data = await self.client.request("GET", f"messages/{message_id}/history")
        return list(data["message_history"])

    async def settled(self, msg: dict[str, Any], pattern: str) -> None:
        """Message `msg`'s final state matches `pattern` (at its start), or an AssertionError.
        When the version Zulip served doesn't, the last entry of its edit history is read. A
        last entry that differs and matches, from an edit made before Zulip served `msg` (its
        `ASKED`, by `rig_failure.earlier`), means Zulip served an older version: a
        `RigFailure` quoting both (#877). An edit made after the read, or too close to it to
        tell, leaves the read as it was: the bridge's failure."""
        served = str(msg["content"])
        if re.match(pattern, served):
            return
        failed = f"message {msg['id']} reads {served[:200]!r}, not {pattern!r}"
        try:
            until = self.clock() + BACK_S
            last = (await self.retrying(lambda: self.history(msg["id"]), until=until,
                                        what="the edit history"))[-1]  # fmt: skip
        except Exception as exc:
            raise AssertionError(f"{failed} (its edit history unread: {exc!r})"[:400]) from exc
        content, edited = str(last.get("content", "")), stamp(last)
        if content != served and re.match(pattern, content):
            asked = msg.get(ASKED)
            if asked is not None and earlier(edited, asked):
                raise RigFailure(
                    f"Zulip served message {msg['id']} as {served[:120]!r} at {utc(asked)}, but "
                    f"its edit history ends with {content[:120]!r} from {utc(edited)}"
                )
            read = f"the read at {utc(asked)}" if asked is not None else "the read"
            failed += f" (its edit history ends with a match from {utc(edited)}, not before {read})"
        raise AssertionError(failed)

    async def after(self, narrow: Narrow, after_id: int, page: int = 200) -> list[dict[str, Any]]:
        """Every message of `narrow` newer than `after_id`, oldest first, as they are now, each
        stamped with `ASKED`: page after page until Zulip says it reached the newest (codex on
        #277: a reply past the first page, or a sender there, must count)."""
        out: list[dict[str, Any]] = []
        anchor = after_id
        while True:
            asked = self.wall()
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
            batch = [{**m, ASKED: asked} for m in data["messages"] if m["id"] > anchor]
            out.extend(batch)
            newest = data.get("found_newest")
            if newest or not batch or (newest is None and len(batch) < page):
                return out
            anchor = batch[-1]["id"]

    async def wait_for(
        self,
        probe: Callable[[], Awaitable[T | None]],
        *,
        within: float,
        what: str,
        missed: Callable[[float, float], Awaitable[str | None]] | None = None,
    ) -> T:
        """Poll `probe` every `every` seconds until it returns something; after `within` seconds,
        a TimeoutError naming `what`, and for how long the rig's calls failed (`Outage`, #877).
        A transient error is retried (`retrying`). The timeout is a `RigTimeout` when `missed`,
        asked once the connection is back, names something that matched between the wait's
        start and its deadline (epoch seconds) and was never seen; or when the calls still
        failed at the deadline and Zulip couldn't be reached after it: the rig couldn't look."""
        outage, since = Outage(self.clock(), within), self.wall()
        while True:
            try:
                found = await self.retrying(probe, until=outage.end, what=what, outage=outage)
            except Exception as exc:
                if not transient(exc):
                    raise
                break  # `retrying` gives up on a transient error only at the deadline
            if found is not None:
                return found
            if self.clock() >= outage.end:
                break
            await self.sleep(self.every)
        lost, down, saw = outage.text(self.clock()), outage.down, f"no {what} within {within:.0f} s"
        if missed is not None:
            try:
                seen = await self.retrying(
                    lambda: missed(since, since + within), until=self.clock() + BACK_S,
                    what="the edit history",
                )  # fmt: skip
                down = False  # the rig looked, after the deadline
            except Exception as exc:  # unread: the timeout as it was
                seen, down = None, down and transient(exc)  # an answer: Zulip was reached
                unread = f"the edit history unread ({type(exc).__name__})"
                lost = f"{lost}, {unread}" if lost else unread
            if seen:
                how = lost or "Zulip didn't serve it, with no connection errors"
                raise RigTimeout(f"{seen}, but the rig never saw it: {how}; {saw}")
        if down:
            raise RigTimeout(f"{lost}: the rig couldn't look; {saw}")
        raise TimeoutError(f"{lost}; {saw}" if lost else saw)

    async def retrying(
        self,
        call: Callable[[], Awaitable[T]],
        *,
        until: float,
        what: str,
        outage: Outage | None = None,
    ) -> T:
        """`call()`, again after a transient error (`transient`), with a growing pause (the poll
        interval doubled each time, at most 60 s), logged each time, until `until`; then, or
        on any other error, the error is raised. Each transient error, and the next answer,
        goes in `outage`."""
        failures = 0
        while True:
            began = self.clock()
            try:
                found = await call()
            except Exception as exc:
                if not transient(exc):
                    raise
                error = str(exc) if isinstance(exc, ZulipError) else type(exc).__name__
                if outage is not None:
                    outage.failed(began, error)
                if self.clock() >= until:
                    raise
                failures += 1
                pause = min(self.every * 2**failures, 60.0, max(0.0, until - self.clock()))
                LOG.warning(
                    "e2e: %s while waiting for %s; retry %d in %.0f s", error, what, failures, pause
                )
                await self.sleep(pause)
            else:
                if outage is not None:
                    outage.back(began)
                return found

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
        matches `pattern` (a regular expression; edits count, as the bridge edits its status).
        One from them saying that OpenAI refused a Codex request with a 429 raises `Refused` at
        once, whatever `pattern` is: that turn will never answer (#842). At the deadline, their
        messages' edit histories are read: a version that matched before it, which the rig
        never saw (a status shown only while the connection failed, or one Zulip didn't
        serve), makes the timeout a `RigTimeout` (#877)."""
        wanted, regex = set(senders), re.compile(pattern, re.S)

        async def probe() -> dict[str, Any] | None:
            for msg in await self.after(narrow, after_id):
                if msg["sender_id"] not in wanted:
                    continue
                if line := refusal(msg["content"]):
                    raise Refused(line)
                if regex.search(msg["content"]):
                    return msg
            return None

        async def missed(since: float, before: float) -> str | None:
            for msg in await self.after(narrow, after_id):
                if msg["sender_id"] in wanted:
                    history = await self.history(msg["id"])
                    if seen := shown(history, regex, since=since, before=before, every=self.every):
                        return f"message {msg['id']} {seen}"
            return None

        what = f"reply matching {pattern!r}"
        return await self.wait_for(probe, within=within, what=what, missed=missed)

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
