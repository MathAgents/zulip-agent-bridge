"""Claude acceptance run on the development machine: every AT of SPEC §17 that applies here, one
command.

    ZAB_LIVE=claude uv run pytest -q -s tests/live/test_claude_at.py

Each test prints one `ATn …` evidence line. The M2 drivers (AT-4, AT-11, AT-15, AT-16, AT-19)
are imported from test_claude_m2 and collected here as well, so running this file alone covers
the whole Claude set; do not run both files in one session (the M2 tests would run twice).
Not run here, by design (see `test_not_applicable_here`): AT-3, AT-6 and AT-10 are Codex-only
(tests/live/test_codex_*.py); AT-17 and AT-18 are fake-Zulip tests (offline suites); AT-21 is
Drew's on the staging VM; AT-22 runs only against the fake Zulip
(tests/core/test_m3_acceptance.py); AT-23 is the soak test, run separately. AT-20 needs the
core's `!handover` and reports BLOCKED without it. AT-8 skips: its public channel is archived
(`AT8_SKIP`, SPEC §15.3).

Rules as in test_claude_m2: ZAB_LIVE=claude only (§15.3), the Claude account is ~/.claude2 (never
the session's ~/.claude), posts go to the zab-dev channels only, as the dev bots and test users
whose zuliprc files sit in ~/.config/zab/ (paths only, never their contents).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest
from at8 import AT8_SKIP, PUBLIC
from test_claude_m2 import (
    BOT,
    BOT_ID,
    CREDENTIALS,
    STREAM,
    TESTER_ID,
    Live,
    config_for,
    live,  # noqa: F401 - fixtures used by the imported M2 tests
    live_worktree,  # noqa: F401
    png_red_square,
    scratch_repo,
    test_at4_hold,  # noqa: F401 - collected here too
    test_at11_restart,  # noqa: F401
    test_at15_long_answers,  # noqa: F401
    test_at16_attachments,  # noqa: F401
    test_at19_worktrees,  # noqa: F401
    transcript_of,
    until,
)

from zab.commands.run import Runtime
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        __import__("os").getenv("ZAB_LIVE") != "claude", reason="Claude opt-in required"
    ),
]

STREAM2 = 639107  # zab-dev-2 (private; AT-9's move target)
TESTER2_ID = 1207965  # zabtester2: not in any allowlist (AT-7)
SPARE_BOT_ID = 1207972  # spare dev-claude: "another bot" (AT-7)
GITHUB_URL = "https://github.com/owner/repo.git"
HANDOVER_AVAILABLE = importlib.util.find_spec("zab.core.commands") is not None


async def git(ws: LocalWorkspace, *args: str) -> str:
    res = await ws.run(["git", *args])
    assert res.exit_code == 0, f"git {' '.join(args)}: {res.stderr}"
    return res.stdout.strip()


async def mark_pushed_to_github(repo: Path) -> str:
    """Make the checkout count as pushed to a GitHub remote without any network: `origin` is a
    URL only, the upstream ref is written by hand (as tests/core/test_links.py does). Returns
    the head sha."""
    ws = LocalWorkspace(str(repo))
    await git(ws, "config", "user.name", "Zab Test")
    await git(ws, "config", "user.email", "zab@example.invalid")
    await git(ws, "remote", "add", "origin", GITHUB_URL)
    await git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
    await git(ws, "config", "branch.main.remote", "origin")
    await git(ws, "config", "branch.main.merge", "refs/heads/main")
    return await git(ws, "rev-parse", "HEAD")


class LiveAT(Live):
    """The M2 harness plus a channel of choice and a topic listing."""

    def __init__(self, runtime: Runtime, tester: ZulipClient, repo: Path) -> None:
        super().__init__(runtime, tester, repo)
        self.topic = "zab-at-" + str(time.time_ns())
        self.stream = STREAM

    async def send(
        self, text: str, topic: str | None = None, client: ZulipClient | None = None
    ) -> int:
        data = await (client or self.tester).request(
            "POST",
            "messages",
            type="stream",
            to=self.stream,
            topic=topic or self.topic,
            content=text,
        )
        return data["id"]

    async def messages(self, topic: str | None = None, stream: int | None = None) -> list[dict]:
        narrow = [
            {"operator": "stream", "operand": stream or self.stream},
            {"operator": "topic", "operand": topic or self.topic},
        ]
        data = await self.tester.request(
            "GET",
            "messages",
            anchor="newest",
            num_before=100,
            num_after=0,
            narrow=json.dumps(narrow),
        )
        return data["messages"]

    async def reactions(self, mid: int) -> set[str]:
        return {r["emoji_code"] for r in (await self.message(mid)).get("reactions", [])}

    async def session_row(self):
        actor = await self.actor()
        return actor.row if actor else None


@pytest.fixture
async def at(tmp_path):
    repo = await scratch_repo(tmp_path)
    runtime = Runtime(config_for(tmp_path, repo))
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == TESTER_ID
        await runtime.start()
        await until(lambda: asyncio.sleep(0, result=runtime.backends["claude"]._up), 30)
        for sid, name, private in ((STREAM, "zab-dev", True), (STREAM2, "zab-dev-2", True)):
            channel = await runtime.bridges[0].channel(sid)
            assert channel["name"] == name and runtime.bridges[0].private(channel) == private
        yield LiveAT(runtime, tester, repo)
    finally:
        await runtime.close()
        await tester.close()


# ------------------------------------------------------------------------- AT-1, AT-2, AT-7


async def test_at1_at2_at7_new_followup_ignored_senders(at):
    """AT-1: within 5 s a 👀 reaction, the card and the status message; the final answer and the
    updated card. AT-2: a follow-up without a mention continues the same native session. AT-7:
    another human and another bot in the topic are ignored (no reaction, no delivery)."""
    started = time.monotonic()
    mid = await at.send(f"{BOT} List the files and summarize README.md. Finish with AT1_FIRST.")

    async def acknowledged():
        actor = await at.actor()
        return (
            actor
            and actor.turn
            and actor.turn.get("status_message_id")
            and actor.row.get("card_message_id")
            and "1f440" in await at.reactions(mid)
        )

    await until(acknowledged, 5)
    ack_s = time.monotonic() - started
    actor = await at.actor()
    card_before = (await at.message(actor.row["card_message_id"]))["content"]
    status = (await at.message(actor.turn["status_message_id"]))["content"]
    assert "zab session" in card_before and "Working" in status
    first = await until(at.finished, 300)
    assert first["state"] == "completed", first
    answer = await at.answer(first)
    assert "AT1_FIRST" in answer and "README" in answer
    sid = actor.row["native_id"]
    card_after = (await at.message(actor.row["card_message_id"]))["content"]
    assert card_after != card_before and sid in card_after  # the card carries the resume command
    transcript = transcript_of(sid)
    assert transcript is not None and "AT1_FIRST" in transcript.read_text()
    # AT-7: zabtester2 (not in the allowlist) and the spare dev bot ("another bot")
    ignored = []
    for name, uid in (("tester2", TESTER2_ID), ("dev-spare", SPARE_BOT_ID)):
        client = ZulipClient.from_zuliprc(CREDENTIALS / f"{name}.zuliprc")
        try:
            assert (await client.request("GET", "users/me"))["user_id"] == uid
            ignored.append(
                await at.send(f"{BOT} Reply UNAUTHORIZED_{name.upper()}.", client=client)
            )
        finally:
            await client.close()
    await asyncio.sleep(4)
    for mid2 in ignored:
        assert not (await at.message(mid2)).get("reactions"), mid2
    assert len(at.runtime.store.rows("turns", session_id=actor.row["id"])) == 1
    assert at.runtime.store.held(actor.row["id"]) == []
    # AT-2: a follow-up without a mention
    await at.send("Without tools, reply AT2_FOLLOWUP and name the file you summarized earlier.")
    second = await until(lambda: at.finished(2), 300)
    assert second["state"] == "completed" and "AT2_FOLLOWUP" in await at.answer(second)
    assert (await at.actor()).row["native_id"] == sid
    assert "AT2_FOLLOWUP" in transcript_of(sid).read_text()  # same transcript, resumed
    print(
        "AT1",
        at.topic,
        "ack_s",
        round(ack_s, 2),
        "m1",
        mid,
        "card",
        actor.row["card_message_id"],
        "status",
        actor.turn and actor.turn.get("status_message_id"),
        "final",
        first["final_message_ids"],
        "session",
        sid,
        "resume",
        actor.row.get("resume_hint", ""),
    )
    print("AT2", at.topic, "turn2 final", second["final_message_ids"], "same session", sid)
    print("AT7", at.topic, "ignored", ignored, "turns", 1, "held", 0)


# --------------------------------------------------------------------------------------- AT-5


async def test_at5_stop(at):
    """`!stop` during a long tool call: the turn ends within 15 s, the status message shows ⏹,
    and the next message still resumes the session (§7.7)."""
    mid = await at.send(
        f"{BOT} The codeword is HERON. Use the Bash tool to run exactly "
        "`python3 -c 'import time; time.sleep(120); print(1)'` in the foreground (never in the "
        "background) and wait for it; only after it has returned, reply exactly "
        "STOP_SHOULD_NOT_FINISH."
    )

    async def tool_running():
        actor = await at.actor()
        if not actor or not actor.turn:
            return False
        path = transcript_of(actor.row["native_id"])
        if path is None:
            return False
        text = path.read_text()
        return "time.sleep(120)" in text and '"tool_use"' in text and '"tool_result"' not in text

    await until(tool_running, 120)
    actor = await at.actor()
    status_id = actor.turn["status_message_id"]
    sent = time.monotonic()
    await at.send("!stop")
    turn = await until(at.finished, 30)
    stop_s = time.monotonic() - sent
    assert turn["state"] == "interrupted", turn
    assert stop_s <= 15, f"stop took {stop_s:.1f} s"
    status = (await at.message(status_id))["content"]
    assert "⏹" in status, status
    await at.send("Without tools, reply exactly AFTER_STOP and then the codeword I gave you.")
    second = await until(lambda: at.finished(2), 300)
    answer = await at.answer(second)
    assert second["state"] == "completed" and "AFTER_STOP" in answer
    print(
        "AT5",
        at.topic,
        "m1",
        mid,
        "stop_s",
        round(stop_s, 1),
        "status",
        status_id,
        "recalled HERON:",
        "HERON" in answer,
    )


# --------------------------------------------------------------------------------------- AT-8


@pytest.mark.skip(reason=AT8_SKIP)
async def test_at8_public_refusal(at):
    """A mention in the public channel gets one refusal and creates no session (§4.5).
    Skipped (`AT8_SKIP`): it would need a public channel, and the dev organization's is
    archived."""
    at.stream = PUBLIC
    first = await at.send(f"{BOT} Test the public-channel refusal.")
    await at.send(f"{BOT} Repeat the public-channel refusal.")

    async def refusals():
        return [m for m in await at.messages() if m["sender_id"] == BOT_ID]

    replies = await until(refusals, 15)
    await asyncio.sleep(3)
    assert len(await refusals()) == len(replies) == 1, replies
    assert await at.actor() is None
    assert not [r for r in at.runtime.store.rows("sessions") if r["topic"] == at.topic]
    print("AT8", "stream", PUBLIC, at.topic, "mention", first, "refusal", replies[0]["id"])


# --------------------------------------------------------------------------------------- AT-9


async def test_at9_rename_resolve_move(at):
    """Rename, resolve, move to the second private channel, then post there: the session
    continues (§4.4, §7.1). Claude has no thread name, so only the continuation is checked."""
    await at.send(f"{BOT} Reply MOVE_START without tools.")
    first = await until(at.finished, 300)
    assert first["state"] == "completed"
    actor = await at.actor()
    sid = actor.row["native_id"]
    base = at.topic
    for topic, stream in (
        (base + "-renamed", STREAM),
        ("✔ " + base + "-renamed", STREAM),
        ("✔ " + base + "-renamed", STREAM2),
    ):
        response = await at.tester.http.patch(
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
        at.topic, at.stream = topic, stream

        async def moved(topic=topic, stream=stream):
            return actor.row["topic"] == topic and actor.row["stream_id"] == stream

        await until(moved, 15)
    await at.send("Reply MOVE_FINISHED without tools.")
    second = await until(lambda: at.finished(2), 300)
    assert second["state"] == "completed" and "MOVE_FINISHED" in await at.answer(second)
    assert actor.row["native_id"] == sid
    print(
        "AT9",
        base,
        "->",
        at.topic,
        "stream",
        at.stream,
        "session",
        sid,
        "final",
        second["final_message_ids"],
    )


# -------------------------------------------------------------------------------------- AT-12


async def test_at12_links_and_image(at):
    """A pushed tracked file with a line → GitHub permalink at the sha with an anchor; an
    untracked file → code span *(local)*; a PNG → uploaded and previewed inline (§11.3)."""
    sha = await mark_pushed_to_github(at.repo)
    (at.repo / "local-note.txt").write_text("untracked local note\n")
    (at.repo / "plot.png").write_bytes(png_red_square(32))
    mid = await at.send(
        f"{BOT} Do not use tools. Output exactly these three references, one per line, and nothing "
        "else: README.md:1, `local-note.txt`, and `plot.png`."
    )
    turn = await until(at.finished, 300)
    assert turn["state"] == "completed", turn
    answer = await at.answer(turn)
    assert f"https://github.com/owner/repo/blob/{sha}/README.md#L1" in answer, answer
    assert "`local-note.txt` *(local)*" in answer, answer
    assert "/user_uploads/" in answer and "plot.png" in answer, answer
    rendered = await at.message(json.loads(turn["final_message_ids"])[0], markdown=True)
    assert 'class="message_inline_image"' in rendered["content"]
    print("AT12", at.topic, mid, "sha", sha[:7], "final", turn["final_message_ids"])


# ------------------------------------------------------------------------------- AT-13, AT-14


MATH_PAYLOAD = r"""Inline: $x^2$ and \(a\).

$$
x+y
$$

\[z^2\]

Currency: $100 and $200.

```sh
echo $HOME
```

@**all**"""


async def test_at13_at14_math_and_mentions(at):
    """Math in Zulip syntax, prose dollars and code untouched (§11.2); `@**all**` inert (§11.5)."""
    mid = await at.send(
        f"{BOT} Without using tools, output precisely the following text, omitting the outer "
        "four-backtick fence:\n\n````\n" + MATH_PAYLOAD + "\n````"
    )
    turn = await until(at.finished, 300)
    assert turn["state"] == "completed", turn
    answer = await at.answer(turn)
    assert "$$x^2$$" in answer and "$$a$$" in answer, answer
    assert "```math" in answer and "x+y" in answer and "z^2" in answer, answer
    assert "$100 and $200" in answer and "echo $HOME" in answer, answer
    assert "`@\u200b**all**`" in answer, answer
    for fid in json.loads(turn["final_message_ids"]):
        assert "wildcard_mentioned" not in (await at.message(fid)).get("flags", [])
    print("AT13/14", at.topic, mid, "final", turn["final_message_ids"])


# -------------------------------------------------------------------------------------- AT-20


@pytest.mark.skipif(
    not HANDOVER_AVAILABLE, reason="AT-20 BLOCKED: the core has no !handover yet (PR #66)"
)
async def test_at20_handover(at, tmp_path):
    """`!handover`: HANDOVER.md written, committed and pushed by the agent; the answer carries the
    commit hash and a permalink to the file (Appendix A.2). `origin` fetches from a GitHub URL
    (for the permalink) and pushes to a local bare repository, so whichever push the agent picks
    (`git push`, `git push origin main`, `-u origin main`) really lands and updates
    `refs/remotes/origin/main`. A separate upstream remote failed once: haiku ran
    `git push -u origin main` against the unreachable GitHub URL."""
    ws = LocalWorkspace(str(at.repo))
    bare = tmp_path / "upstream.git"
    assert (
        await LocalWorkspace(str(tmp_path)).run(["git", "init", "-q", "--bare", str(bare)])
    ).exit_code == 0
    await git(ws, "config", "user.name", "Zab Test")
    await git(ws, "config", "user.email", "zab@example.invalid")
    await git(ws, "remote", "add", "origin", GITHUB_URL)
    await git(ws, "remote", "set-url", "--push", "origin", str(bare))
    await git(ws, "push", "-q", "-u", "origin", "main")
    before = await git(ws, "rev-parse", "HEAD")
    await at.send(f"{BOT} Reply HANDOVER_READY without tools.")
    first = await until(at.finished, 300)
    assert first["state"] == "completed"
    mid = await at.send("!handover")
    turn = await until(lambda: at.finished(2), 900)
    assert turn["state"] == "completed", turn
    head = await git(ws, "rev-parse", "HEAD")
    assert head != before, "the agent should have committed"
    touched = await git(ws, "show", "--name-only", "--format=", "HEAD")
    assert "HANDOVER.md" in touched and (at.repo / "HANDOVER.md").is_file()
    pushed = (await LocalWorkspace(str(bare)).run(["git", "rev-parse", "main"])).stdout.strip()
    assert pushed == head, "the agent should have pushed to the upstream"
    answer = await at.answer(turn)
    assert head[:7] in answer, answer
    assert f"https://github.com/owner/repo/blob/{head}/HANDOVER.md" in answer, answer
    print(
        "AT20",
        at.topic,
        mid,
        "commit",
        head[:7],
        "pushed",
        pushed == head,
        "final",
        turn["final_message_ids"],
    )


# ----------------------------------------------------------------------- not applicable here


def test_not_applicable_here():
    """The ATs this run does not perform, with the reason, so the summary is complete."""
    for line in (
        "AT3 N/A here: AT-3 is Codex's steer. Claude steers by default (#310), covered by e2e "
        "scenario 5; AT-4 runs Claude with steer=false (hold).",
        "AT6 N/A here: approvals are Codex-only (Claude runs bypassPermissions/dontAsk, "
        "approvals=False); tests/live/test_codex_m1.py runs it.",
        f"AT8 skipped (test_at8_public_refusal): {AT8_SKIP}.",
        "AT10 N/A here: restart (Codex) is tests/live/test_codex_restart.py; Claude's is AT-11.",
        "AT17 N/A live: 429 injection needs the fake Zulip; offline "
        "tests/zulip/test_poster.py::test_429_recovers_and_edits_coalesce.",
        "AT18 N/A live: BAD_EVENT_QUEUE_ID injection needs the fake Zulip; offline "
        "tests/core/test_bridge.py (queue gap and reconcile tests).",
        "AT21 N/A here: fresh install on the staging VM, run by Drew or a helper.",
        "AT22 N/A live: fake Zulip and decoy HOME only; offline "
        "tests/core/test_m3_acceptance.py (PR #67).",
        "AT23 separate: 5 bridges x 3 sessions x 30 min with the fake claude, "
        "`ZAB_LIVE=claude uv run python tests/live/at23_soak.py`.",
    ):
        print(line)
