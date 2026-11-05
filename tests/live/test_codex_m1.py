"""Real development-channel acceptance tests; ZAB_LIVE=codex only (§15.3)."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from zab.commands.run import Runtime
from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "codex", reason="Codex opt-in required"),
]
TEST_HOME = Path.home() / ".codex-zabtest"
TEST_SOCKET = os.getenv("ZAB_CODEX_TEST_SOCKET", "")
CREDENTIALS = Path.home() / ".config/zab"
CHECKOUT = Path(__file__).resolve().parents[2]


async def until(check, seconds=90):
    async with asyncio.timeout(seconds):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.2)


class Live:
    def __init__(self, runtime, tester):
        self.runtime, self.tester = runtime, tester
        self.repo = Path(runtime.config.bridge.default_project)
        self.bridge = runtime.bridges[0]
        self.topic = "zab-m1-" + str(time.time_ns())
        self.stream = 639106

    async def send(self, text, client=None, *, topic=None):
        return (
            await (client or self.tester).request(
                "POST",
                "messages",
                type="stream",
                to=self.stream,
                topic=topic or self.topic,
                content=text,
            )
        )["id"]

    async def message(self, mid):
        return (await self.tester.request("GET", f"messages/{mid}", apply_markdown=False))[
            "message"
        ]

    async def messages(self):
        return (
            await self.tester.request(
                "GET",
                "messages",
                anchor="newest",
                num_before=100,
                num_after=0,
                apply_markdown=False,
                narrow=[
                    {"operator": "channel", "operand": self.stream},
                    {"operator": "topic", "operand": self.topic},
                ],
            )
        )["messages"]

    async def actor(self, topic=None):
        return next(
            (a for a in self.bridge.actors.values() if a.row["topic"] == (topic or self.topic)),
            None,
        )

    async def finished(self, count=1, topic=None):
        actor = await self.actor(topic)
        if not actor:
            return None
        rows = sorted(
            self.runtime.store.rows("turns", session_id=actor.row["id"]), key=lambda r: r["id"]
        )
        if len(rows) >= count and rows[count - 1]["state"] != "running":
            return rows[count - 1]
        if actor.row["state"] == "error":
            raise AssertionError([m["content"] for m in await self.messages()])
        return None

    async def answer(self, turn):
        return "\n".join(
            [(await self.message(mid))["content"] for mid in json.loads(turn["final_message_ids"])]
        )


@pytest.fixture
async def live(tmp_path):
    assert TEST_HOME == Path("/home/gpt/.codex-zabtest")
    # Fixed dev-only socket; RPC initialization also verifies TEST_HOME.
    assert not TEST_SOCKET or TEST_SOCKET == "/tmp/zab-rc2-app-server.sock"  # noqa: S108
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text(
        "# Zab test\nA scratch repository for bridge acceptance tests.\n"
    )
    ws = LocalWorkspace(str(repo))
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "README.md"],
        [
            "git",
            "-c",
            "user.name=Zab Test",
            "-c",
            "user.email=zab@example.invalid",
            "commit",
            "-qm",
            "Initialize scratch repo",
        ],
    ):
        assert (await ws.run(args)).exit_code == 0
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(
            default_project=str(repo), state_dir=str(tmp_path / "state"), status_edit_interval_s=60
        ),
        [BotConfig("dev-codex", str(CREDENTIALS / "dev-bot.zuliprc"), "codex", [1207964])],
        {"codex": BackendConfig(kind="codex", codex_home=str(TEST_HOME), socket=TEST_SOCKET)},
        [],
        RenderConfig(),
    )
    runtime = Runtime(config)
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == 1207964
        await runtime.start()
        await asyncio.wait_for(runtime.backends["codex"].ready.wait(), 20)
        identity = runtime.backends["codex"].rpc.identity
        assert identity["userAgent"].startswith("zab/0.157.1 "), identity["userAgent"]
        for sid, name, private in [
            (639106, "zab-dev", True),
            (639107, "zab-dev-2", True),
            (639108, "zab-dev-public", False),
        ]:
            channel = await runtime.bridges[0].channel(sid)
            assert channel["name"] == name and runtime.bridges[0].private(channel) == private
        yield Live(runtime, tester)
    finally:
        await runtime.close()
        await tester.close()


async def test_new_followup_and_ignored_senders(live):
    started = time.monotonic()
    mid = await live.send(
        "@**codex dev-gpt** List the files and summarize README.md. Finish with M1_FIRST."
    )

    async def acknowledged():
        actor = await live.actor()
        message = await live.message(mid)
        return actor and actor.turn and actor.turn["status_message_id"] and message.get("reactions")

    await until(acknowledged, 5)
    print("AT1 acknowledgement seconds", time.monotonic() - started, "topic", live.topic)
    first = await until(live.finished)
    assert "M1_FIRST" in await live.answer(first)
    actor = await live.actor()
    sid = actor.row["native_id"]
    for name, uid in [("tester2", 1207965), ("dev-spare", 1207970)]:
        client = ZulipClient.from_zuliprc(CREDENTIALS / f"{name}.zuliprc")
        try:
            assert (await client.request("GET", "users/me"))["user_id"] == uid
            ignored = await live.send("@**codex dev-gpt** Reply UNAUTHORIZED", client)
            await asyncio.sleep(1)
            assert not (await live.message(ignored)).get("reactions")
            assert len(live.runtime.store.rows("turns", session_id=actor.row["id"])) == 1
        finally:
            await client.close()
    await live.send("Without using tools, respond M1_FOLLOWUP and name the file you summarized.")
    second = await until(lambda: live.finished(2))
    assert "M1_FOLLOWUP" in await live.answer(second)
    assert (await live.actor()).row["native_id"] == sid
    print("AT1/2/7", sid, second["final_message_ids"])


async def test_steer_and_stop(live):
    await live.send(
        "@**codex dev-gpt** Run a shell command that sleeps for 10 seconds, "
        "then reply DONE. This is a timing test."
    )

    async def running_command():
        actor = await live.actor()
        return actor and any(i.kind.value == "command" for i in actor.actions.values())

    await until(running_command)
    actor = await live.actor()
    tid = actor.turn["native_turn_id"]
    mid = await live.send("Steering update: finish with STEER_RECEIVED instead.")

    async def steered():
        return any(
            r["emoji_code"] == "21aa" for r in (await live.message(mid)).get("reactions", [])
        )

    await until(steered, 10)
    first = await until(live.finished)
    assert "STEER_RECEIVED" in await live.answer(first)
    turns = await live.runtime.backends["codex"]._call(
        "thread/turns/list", threadId=actor.row["native_id"], limit=1, itemsView="full"
    )
    assert turns["data"][0]["id"] == tid
    assert any(
        i["type"] == "userMessage" and "STEER_RECEIVED" in json.dumps(i)
        for i in turns["data"][0]["items"]
    )
    await live.send("Run a shell command that sleeps for 20 seconds, then reply SECOND_DONE.")

    async def second_running():
        return (
            actor.turn
            and actor.turn["native_turn_id"] != tid
            and any(i.kind.value == "command" for i in actor.actions.values())
        )

    await until(second_running)
    start = time.monotonic()
    await live.send("!stop")
    result = await until(lambda: live.finished(2), 15)
    assert result["state"] == "interrupted"
    assert "⏹" in (await live.message(result["status_message_id"]))["content"]
    print("AT3/5 stop seconds", time.monotonic() - start, "topic", live.topic)


async def test_public_refusal(live):
    live.stream = 639108
    await live.send("@**codex dev-gpt** Test the public-channel refusal.")
    await live.send("@**codex dev-gpt** Repeat the public-channel refusal.")

    async def refusal():
        return [m for m in await live.messages() if m["sender_id"] == 1207969]

    replies = await until(refusal, 10)
    await asyncio.sleep(1)
    assert len(await refusal()) == len(replies) == 1
    assert await live.actor() is None
    print("AT8", live.topic)


async def test_rename_resolve_move(live):
    await live.send("@**codex dev-gpt** Reply MOVE_START without tools.")
    await until(live.finished)
    actor = await live.actor()
    sid = actor.row["native_id"]
    for topic, stream in [
        (live.topic + "-renamed", 639106),
        ("✔ " + live.topic + "-renamed", 639106),
        ("✔ " + live.topic + "-renamed", 639107),
    ]:
        response = await live.tester.http.patch(
            f"messages/{actor.row['card_message_id']}",
            data={
                "topic": topic,
                "stream_id": str(stream),
                "propagate_mode": "change_all",
                "send_notification_to_old_thread": "false",
                "send_notification_to_new_thread": "false",
            },
        )
        response.raise_for_status()
        live.topic, live.stream = topic, stream

        async def moved(topic=topic, stream=stream):
            return actor.row["topic"] == topic and actor.row["stream_id"] == stream

        await until(moved, 10)
    await live.send("Reply MOVE_FINISHED without tools.")
    result = await until(lambda: live.finished(2))
    assert "MOVE_FINISHED" in await live.answer(result)
    info = await live.runtime.backends["codex"]._call(
        "thread/resume", threadId=sid, excludeTurns=True
    )
    assert info["thread"]["name"] == live.topic.removeprefix("✔ ")
    print("AT9", sid, live.topic)


@pytest.mark.parametrize("emoji, expected", [("+1", "accepted"), ("-1", "declined")])
async def test_approvals(live, emoji, expected):
    live.runtime.config.backends["codex"].defaults["sandbox"] = "workspace-write"
    await live.send(
        "@**codex dev-gpt** !new --approval on-request "
        "This is an approval UI test. Request escalated permission explicitly by running "
        "the harmless shell command `printf ZAB_APPROVAL_OK` with sandbox_permissions set to "
        "require_escalated and justification 'May I print the approval test marker?'. "
        "Do not run it without escalation, and if denied do not retry. Then say APPROVAL_TEST_DONE."
    )

    async def pending():
        actor = await live.actor()
        if actor:
            rows = live.runtime.store.rows("approvals", session_id=actor.row["id"], state="pending")
            if rows:
                return rows[0]
            if await live.finished():
                raise AssertionError("No approval: " + await live.answer(await live.finished()))
        return None

    approval = await until(pending)
    message = await live.message(approval["message_id"])
    assert "1207964" in message["content"]
    await live.tester.request(
        "POST", f"messages/{approval['message_id']}/reactions", emoji_name=emoji
    )
    result = await until(live.finished)
    state = live.runtime.store.rows("approvals", request_id=approval["request_id"])[0]["state"]
    assert state == expected
    assert "APPROVAL_TEST_DONE" in await live.answer(result)
    print("AT6", expected, live.topic)


async def test_math_and_mentions(live):
    payload = r"""Inline: $x^2$ and \(a\).

$$
x+y
$$

\[z^2\]

Currency: $100 and $200.

```sh
echo $HOME
```

@**all**"""
    await live.send(
        "@**codex dev-gpt** Without using tools, output precisely the following text, "
        "omit the outer four-backtick fence:\n\n````\n" + payload + "\n````"
    )
    result = await until(live.finished)
    answer = await live.answer(result)
    assert "$$x^2$$" in answer and "$$a$$" in answer
    assert "```math" in answer and "x+y" in answer and "z^2" in answer
    assert "$100 and $200" in answer and "echo $HOME" in answer
    assert "`@\u200b**all**`" in answer
    for mid in json.loads(result["final_message_ids"]):
        assert "wildcard_mentioned" not in (await live.message(mid)).get("flags", [])
    print("AT13/14", live.topic, result["final_message_ids"])


async def test_links_and_image(live, tmp_path):
    import base64

    repo = tmp_path / "pushed"
    ws = LocalWorkspace(str(tmp_path))
    assert (
        await ws.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                str(CHECKOUT),
                str(repo),
            ]
        )
    ).exit_code == 0
    ws = LocalWorkspace(str(repo))
    # Published M0 fixture commit; independent of local topic branch names.
    fixture_sha = "07fe40bd64572a1a46fd6252146899bc3817d6c6"
    assert (
        await ws.run(["git", "checkout", "-qb", "zab-live-fixture", fixture_sha])
    ).exit_code == 0
    assert (
        await ws.run(["git", "update-ref", "refs/remotes/origin/main", fixture_sha])
    ).exit_code == 0
    assert (await ws.run(["git", "branch", "--set-upstream-to=origin/main"])).exit_code == 0
    assert (
        await ws.run(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                "https://github.com/MathAgents/zulip-agent-bridge.git",
            ]
        )
    ).exit_code == 0
    await ws.write_bytes("local-note.txt", b"untracked local note\n")
    await ws.write_bytes(
        "plot.png",
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
        ),
    )
    live.runtime.config.bridge.default_project = str(repo)
    await live.send(
        "@**codex dev-gpt** Do not use tools. Output exactly these references: "
        "README.md:1, `local-note.txt`, and `plot.png`."
    )
    result = await until(live.finished)
    answer = await live.answer(result)
    sha = (await ws.run(["git", "rev-parse", "HEAD"])).stdout.strip()
    assert f"/blob/{sha}/README.md#L1" in answer
    assert "`local-note.txt` *(local)*" in answer
    assert "/user_uploads/" in answer and "plot.png" in answer
    rendered = await live.tester.request(
        "GET", f"messages/{json.loads(result['final_message_ids'])[0]}", apply_markdown=True
    )
    assert 'class="message_inline_image"' in rendered["message"]["content"]
    print("AT12", live.topic, result["final_message_ids"])
