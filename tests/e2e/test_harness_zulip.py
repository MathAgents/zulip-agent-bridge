"""The e2e harness's Zulip test user, offline (issue #263)."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from e2e.harness.rig_failure import RigTimeout
from e2e.harness.zulip_user import (
    PostingRefused,
    TestUser,
    dm_narrow,
    mention,
    topic_narrow,
    transient,
)
from zab.zulip.client import ZulipClient, ZulipError

TESTER, BOT = 10, 77


class Zulip:
    """Just enough of Zulip for the test user: messages with senders, two narrows, reactions."""

    def __init__(self) -> None:
        self.messages: dict[int, dict] = {}
        self.sent: list[dict] = []
        self.users = [{"user_id": BOT, "full_name": "Claude · zab-test", "is_bot": True},
                      {"user_id": TESTER, "full_name": "zab-tester", "is_bot": False}]  # fmt: skip
        self.subscribers = [BOT, TESTER]
        self.failures: list[int] = []  # HTTP statuses the next GET requests answer with
        self.posts = 0

    def add(self, sender: int, content: str, *, stream: int | None = None, topic: str = "") -> int:
        mid = 100 + len(self.messages)
        self.messages[mid] = {"id": mid, "sender_id": sender, "content": content,
                              "stream_id": stream, "subject": topic, "reactions": []}  # fmt: skip
        if stream is None:
            self.messages[mid]["display_recipient"] = [{"id": TESTER}, {"id": BOT}]
        return mid

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        raw = request.url.query if request.method == "GET" else request.content
        params = dict(parse_qsl(raw.decode()))
        body: dict = {}
        if request.method == "GET" and self.failures:
            status = self.failures.pop(0)
            return httpx.Response(status, json={"result": "error", "code": f"E{status}"})
        if request.method != "GET":
            self.posts += 1
        if path == "users/me":
            body = {"user_id": TESTER}
        elif path == "users":
            body = {"members": self.users}
        elif path == "get_stream_id":
            body = {"stream_id": 5}
        elif path == "streams/5/members":
            body = {"subscribers": self.subscribers}
        elif path == "messages" and request.method == "POST":
            self.sent.append(params)
            to = json.loads(params["to"])
            stream = to if params["type"] == "stream" else None
            body = {"id": self.add(TESTER, params["content"], stream=stream,
                                   topic=params.get("topic", ""))}  # fmt: skip
        elif path == "messages":
            assert params["apply_markdown"] == "false"
            found = list(self.messages.values())
            for term in json.loads(params["narrow"]):
                if term["operator"] == "dm":
                    found = [m for m in found if m["stream_id"] is None]
                elif term["operator"] == "channel":
                    found = [m for m in found if m["stream_id"] == term["operand"]]
                else:
                    found = [m for m in found if m["subject"] == term["operand"]]
            newer = [m for m in found if m["id"] > int(params["anchor"])]
            count = int(params["num_after"])
            body = {"messages": newer[:count], "found_newest": len(newer) <= count}
        elif path.endswith("/history"):  # one version, from the epoch
            content = self.messages[int(path.split("/")[1])]["content"]
            body = {"message_history": [{"content": content, "timestamp": 0}]}
        elif path.startswith("messages/"):
            body = {"message": self.messages[int(path.split("/")[1])]}
        return httpx.Response(200, json={"result": "success", **body})


def user(fake: Zulip) -> TestUser:
    client = ZulipClient(
        "https://zulip.example", "t@x", "decoy", transport=httpx.MockTransport(fake.handle)
    )

    async def no_sleep(seconds):
        fake.slept = getattr(fake, "slept", 0) + seconds

    ticks = iter(range(0, 10_000))
    tester = TestUser(client, sleep=no_sleep, clock=lambda: next(ticks))
    tester.restrict(channels=[5], bots=[BOT])
    return tester


async def test_send_and_wait_for_a_bot_reply_in_a_topic():
    fake = Zulip()
    tester = user(fake)
    bot = await tester.user_id("Claude · zab-test")
    stream = await tester.stream_id("zab-test-sandbox")
    mid = await tester.send(stream, "e2e-1", mention("Claude · zab-test", bot) + " hi")
    assert fake.sent[0]["content"] == "@**Claude · zab-test|77** hi"
    fake.add(BOT, "⏳ **Working**", stream=5, topic="e2e-1")
    fake.add(BOT, "PONG-123", stream=5, topic="other")  # another topic: not a reply here
    narrow = topic_narrow(stream, "e2e-1")
    with pytest.raises(TimeoutError, match="PONG"):
        await tester.wait_reply(narrow, after_id=mid, senders=[bot], pattern=r"PONG-123",
                                within=5)  # fmt: skip
    fake.add(BOT, "the answer is PONG-123", stream=5, topic="e2e-1")
    got = await tester.wait_reply(
        narrow, after_id=mid, senders=[bot], pattern=r"PONG-\d+", within=5
    )
    assert got["content"] == "the answer is PONG-123"
    await tester.close()


async def test_replies_and_senders_past_the_first_page_count():
    """Codex on #277: a match at position 201, or a sender there, is seen."""
    fake = Zulip()
    tester = user(fake)
    mid = await tester.send_dm(BOT, "go")
    for i in range(250):
        fake.add(TESTER, f"chatter {i}")
    fake.add(BOT, "late PONG-9")
    got = await tester.wait_reply(
        dm_narrow(BOT), after_id=mid, senders=[BOT], pattern="PONG-9", within=5
    )
    assert got["content"] == "late PONG-9"
    assert len(await tester.silent(dm_narrow(BOT), after_id=mid, senders=[BOT], seconds=1)) == 1
    assert len(await tester.after(dm_narrow(BOT), mid, page=100)) == 251
    await tester.close()


async def test_humans_of_a_channel():
    """One human: a follow-up needs no mention there; a second makes it multi-party."""
    fake = Zulip()
    tester = user(fake)
    assert await tester.humans(5) == {TESTER}
    fake.users.append({"user_id": 11, "full_name": "zab-tester2", "is_bot": False})
    fake.users.append({"user_id": 12, "full_name": "gone", "is_bot": False, "is_active": False})
    fake.subscribers += [11, 12]
    tester._users = None  # a fresh look-up
    assert await tester.humans(5) == {TESTER, 11}
    await tester.close()


async def test_dm_silence_and_reactions():
    fake = Zulip()
    tester = user(fake)
    mid = await tester.send_dm(BOT, "!help")
    assert fake.sent[0]["type"] == "private" and json.loads(fake.sent[0]["to"]) == [BOT]
    assert await tester.silent(dm_narrow(BOT), after_id=mid, senders=[BOT], seconds=60) == []
    fake.messages[mid]["reactions"].append({"emoji_code": "21aa", "user_id": BOT})
    got = await tester.wait_reaction(mid, emoji_code="21aa", user_id=BOT, within=5)
    assert got["user_id"] == BOT
    # silent() reports a bot's reaction on the message too (reviewer on #284)
    fake.messages[mid]["reactions"].append({"emoji_name": "eyes", "user_id": BOT})
    seen = await tester.silent(dm_narrow(BOT), after_id=mid, senders=[BOT], seconds=0)
    assert [s["content"] for s in seen] == ["reaction :?:", "reaction :eyes:"]
    with pytest.raises(LookupError):
        await tester.user_id("Codex · zab-test")
    await tester.close()


async def test_posting_only_in_the_rigs_channels_and_bot_dms():
    """The rig posts only in its own channels and its bots' DMs, as TestEc2 refuses untagged
    instances (reviewer on #286)."""
    fake = Zulip()
    tester = user(fake)
    other = fake.add(42, "someone else's", stream=9, topic="t")
    for refused in (
        tester.send(9, "t", "x"),  # not one of the rig's channels
        tester.send_dm(42, "x"),  # not one of the rig's bots
        tester.edit(other, "x"),  # not posted by this run
        tester.react(other, "eyes"),  # outside the rig's channels
    ):
        with pytest.raises(PostingRefused):
            await refused
    assert fake.posts == 0
    mid = await tester.send(5, "t", "x")
    await tester.edit(mid, "y")
    await tester.react(mid, "thumbs_up")
    assert fake.posts == 3
    closed = user(fake)
    closed.restrict(channels=[], bots=[])  # before restrict(), or with nothing: nothing
    with pytest.raises(PostingRefused):
        await closed.send(5, "t", "x")
    await tester.close()
    await closed.close()


async def test_transient_errors_are_retried_until_the_deadline(caplog):
    """A 502 or a 429 during a wait is retried with backoff and logged; a 400 is not."""
    fake = Zulip()
    tester = user(fake)
    mid = await tester.send_dm(BOT, "go")
    fake.add(BOT, "PONG-1")
    fake.failures = [502, 429, 503]
    caplog.set_level("WARNING", logger="zab.e2e")
    got = await tester.wait_reply(dm_narrow(BOT), after_id=mid, senders=[BOT], pattern="PONG",
                                  within=600)  # fmt: skip
    retries = [r for r in caplog.records if r.name == "zab.e2e"]
    assert got["content"] == "PONG-1" and len(retries) == 3
    assert "retry 3" in retries[-1].getMessage()
    fake.failures = [400]
    with pytest.raises(ZulipError):
        await tester.wait_reply(dm_narrow(BOT), after_id=mid, senders=[BOT], pattern="PONG",
                                within=600)  # fmt: skip
    fake.failures = [502] * 200
    # the deadline passes while Zulip fails, and it fails on: the rig couldn't look (#877)
    with pytest.raises(
        RigTimeout,
        match=r"\(HTTP 502\)\), still at its end, the edit history "
        r"unread \(ZulipError\): the rig couldn't look; no reply matching 'PONG'",
    ):
        await tester.wait_reply(dm_narrow(BOT), after_id=mid, senders=[BOT], pattern="PONG",
                                within=20)  # fmt: skip
    await tester.close()


def test_what_is_transient():
    """Reviewer on #289: an answer that isn't JSON is transient only on a 429 or a 5xx, and a
    URL scheme httpx can't speak fails at once."""
    assert transient(ZulipError("INVALID_RESPONSE", 502)) and transient(ZulipError("X", 429))
    assert not transient(ZulipError("INVALID_RESPONSE", 200))
    assert not transient(ZulipError("INVALID_RESPONSE", 404))
    assert not transient(httpx.UnsupportedProtocol("ftp"))
    assert transient(httpx.ConnectError("refused")) and transient(httpx.ReadTimeout("slow"))


async def test_a_person_is_never_taken_for_a_bot():
    """Reviewer on #289: ZAB_E2E_CLAUDE_BOT naming a person would have let the posting guard
    DM them; the bot look-up refuses anyone without `is_bot`."""
    fake = Zulip()
    tester = user(fake)
    assert await tester.bot_id("Claude · zab-test") == BOT
    with pytest.raises(PostingRefused, match="not a bot"):
        await tester.bot_id("zab-tester")
    await tester.close()
