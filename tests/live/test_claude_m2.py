"""Claude M2 live acceptance tests on the development machine: AT-4, AT-11, AT-15, AT-16, AT-19
(SPEC §17).

ZAB_LIVE=claude only (§15.3). The Claude account is ~/.claude2, never the session's own ~/.claude
(the development machine's rule); posts go to #zab-dev as the dev bot, driven by zab-tester; the
workspace is a scratch git repository whose path contains `_` (issue #36 item 9).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import struct
import sys
import time
import zlib
from pathlib import Path

import pytest

from zab.commands.run import Runtime
from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "claude", reason="Claude opt-in required"),
]
CREDENTIALS = Path.home() / ".config/zab"
ACCOUNT = Path.home() / ".claude2"
BOT = "@**claude dev-claude**"
BOT_ID = 1207971
TESTER_ID = 1207964
STREAM = 639106  # zab-dev (private)


async def until(check, seconds=120):
    async with asyncio.timeout(seconds):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.3)


async def scratch_repo(root: Path) -> Path:
    repo = root / "my_repo"  # an underscore in the path, as item 9 asks
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# Scratch\nA scratch repository for Claude M2 acceptance.\n")
    ws = LocalWorkspace(str(repo))
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "README.md"],
        [
            "git",
            "-c",
            "user.name=Zab Test",
            "-c",
            "user.email=zab@example.invalid",
            "commit",
            "-qm",
            "init",
        ],
    ):
        assert (await ws.run(args)).exit_code == 0
    return repo


def config_for(
    tmp_path: Path, repo: Path, workspace: str = "shared", *, steer: bool = True
) -> Config:
    """The live drivers' config: the default, steering on (#310), unless `steer=False`."""
    assert ACCOUNT.is_dir() and ACCOUNT != Path.home() / ".claude"
    return Config(
        tmp_path / "config.toml",
        BridgeConfig(
            default_project=str(repo),
            state_dir=str(tmp_path / "state"),
            status_edit_interval_s=60,
            default_workspace=workspace,
            max_concurrent_turns=3,
        ),
        [BotConfig("dev-claude", str(CREDENTIALS / "dev-bot.zuliprc"), "claude", [TESTER_ID])],
        {
            "claude": BackendConfig(
                kind="claude",
                binary="claude",
                accounts={"test": str(ACCOUNT)},
                default_account="test",
                defaults={"model": "haiku", "approval": "bypassPermissions"},
                steer=steer,
            )
        },
        [],
        RenderConfig(),
    )


class Live:
    def __init__(self, runtime: Runtime, tester: ZulipClient, repo: Path) -> None:
        self.runtime, self.tester, self.repo = runtime, tester, repo
        self.bridge = runtime.bridges[0]
        self.topic = "zab-m2-" + str(time.time_ns())

    async def send(self, text: str, topic: str | None = None) -> int:
        data = await self.tester.request(
            "POST", "messages", type="stream", to=STREAM, topic=topic or self.topic, content=text
        )
        return data["id"]

    async def message(self, mid: int, markdown: bool = False) -> dict:
        return (await self.tester.request("GET", f"messages/{mid}", apply_markdown=markdown))[
            "message"
        ]

    async def actor(self, topic: str | None = None):
        topic = topic or self.topic
        return next((a for a in self.bridge.actors.values() if a.row["topic"] == topic), None)

    async def finished(self, count: int = 1, topic: str | None = None):
        """The `count`-th turn of the topic's session once it is no longer running.

        The requested turn, not the newest: held messages start the next turn on their own,
        and between two polls the newest row can already be that one (codex round 1 on #62).
        """
        actor = await self.actor(topic)
        if not actor:
            return None
        rows = sorted(
            self.runtime.store.rows("turns", session_id=actor.row["id"]), key=lambda r: r["id"]
        )
        if len(rows) >= count and rows[count - 1]["state"] != "running":
            return rows[count - 1]
        if actor.row["state"] == "error":
            raise AssertionError(f"session in error: {actor.row}")
        return None

    async def answer(self, turn: dict) -> str:
        ids = json.loads(turn["final_message_ids"] or "[]")
        return "\n".join([(await self.message(mid))["content"] for mid in ids])


@pytest.fixture
async def live(tmp_path, request):
    """`config_for`'s keywords come from an indirect parameter (AT-4's `steer=False`)."""
    repo = await scratch_repo(tmp_path)
    runtime = Runtime(config_for(tmp_path, repo, **getattr(request, "param", {})))
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == TESTER_ID
        await runtime.start()
        await until(lambda: asyncio.sleep(0, result=runtime.backends["claude"]._up), 30)
        channel = await runtime.bridges[0].channel(STREAM)
        assert channel["name"] == "zab-dev" and runtime.bridges[0].private(channel)
        yield Live(runtime, tester, repo)
    finally:
        await runtime.close()
        await tester.close()


@pytest.fixture
async def live_worktree(tmp_path):
    repo = await scratch_repo(tmp_path)
    runtime = Runtime(config_for(tmp_path, repo, workspace="worktree"))
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == TESTER_ID
        await runtime.start()
        await until(lambda: asyncio.sleep(0, result=runtime.backends["claude"]._up), 30)
        yield Live(runtime, tester, repo)
    finally:
        await runtime.close()
        await tester.close()


def transcript_of(native_id: str) -> Path | None:
    hits = list((ACCOUNT / "projects").glob(f"*/{native_id}.jsonl"))
    return hits[0] if hits else None


# ------------------------------------------------------------------------------------- AT-4


@pytest.mark.parametrize("live", [{"steer": False}], ids=["steer-off"], indirect=True)
async def test_at4_hold(live):
    """Two messages during a Claude turn: both get ⏳ and arrive as one coalesced turn (§7.5).
    Holding needs `steer = false` (SPEC §9.7, AT-4); the other drivers run the default."""
    m1 = await live.send(
        f"{BOT} Use the Bash tool to run exactly "
        "`python3 -c 'import time; time.sleep(30); print(1)'` in the foreground (never in the "
        "background) and wait for its output; only after the command has returned, reply exactly "
        "FIRST_DONE."
    )

    async def running():
        actor = await live.actor()
        return actor and actor.turn and actor.turn.get("status_message_id")

    await until(running, 60)
    m2 = await live.send("Second message: the first codeword is HELD_A.")
    m3 = await live.send(
        "Third message: without tools, reply with both codewords HELD_A and HELD_B."
    )

    async def hourglasses():
        codes = []
        for mid in (m2, m3):
            codes.append({r["emoji_code"] for r in (await live.message(mid)).get("reactions", [])})
        return all("23f3" in c for c in codes)

    await until(hourglasses, 30)
    first = await until(live.finished, 180)
    assert first["state"] == "completed" and "FIRST_DONE" in await live.answer(first)
    second = await until(lambda: live.finished(2), 240)
    assert json.loads(second["trigger_msg_ids"]) == [m2, m3]
    answer = await live.answer(second)
    assert "HELD_B" in answer
    actor = await live.actor()
    rows = live.runtime.store.rows("turns", session_id=actor.row["id"])
    assert len(rows) == 2
    transcript = transcript_of(actor.row["native_id"])
    assert transcript is not None
    text = transcript.read_text()
    assert "2 messages arrived while you were working; the newest takes precedence" in text
    assert "[end of held messages]" in text
    print(
        "AT4",
        live.topic,
        "m1/m2/m3",
        m1,
        m2,
        m3,
        "turns",
        [r["id"] for r in rows],
        "final",
        second["final_message_ids"],
        "session",
        actor.row["native_id"],
    )


# ------------------------------------------------------------------------------------ AT-15


async def test_at15_long_answers(live):
    """15 000 chars with a fence across the split → (1/2)+(2/2); 45 000 chars → part + .md."""
    mid = await live.send(
        f"{BOT} Without using any tools, output exactly 270 numbered lines and nothing else. "
        "Line k (k = 1 … 270) is: "
        "`Line k of the long answer: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` "
        "with k as a plain number and forty letters a. Lines 1-120 and 201-270 are plain text, "
        "each followed by one blank line. Lines 121-200 are inside a single fenced code block "
        "that opens "
        "with ```text on its own line and closes with ``` on its own line, with no blank lines "
        "inside the block. No heading, no commentary."
    )
    first = await until(live.finished, 600)
    assert first["state"] == "completed", first
    ids = json.loads(first["final_message_ids"])
    parts = [(await live.message(i))["content"] for i in ids]
    total = sum(len(p) for p in parts)
    print("AT15a", live.topic, mid, "parts", len(ids), "chars", [len(p) for p in parts], total)
    assert len(ids) == 2, f"expected two parts, got {len(ids)} ({total} chars)"
    assert "(1/2)" in parts[0] and "(2/2)" in parts[1]
    head = parts[0].rstrip().rsplit("(1/2)", 1)[0].rstrip()
    assert head.endswith("```"), "the fence must be closed at the end of part 1"
    assert parts[1].lstrip().startswith("```text"), "and reopened with its info string in part 2"
    await live.send(
        "Now, without tools, output exactly 900 lines and nothing else: line k (k = 1 … 900) is "
        "`Row k: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb` (forty-two letters b), each followed "
        "by one blank line. No code blocks, no heading, no commentary."
    )
    second = await until(lambda: live.finished(2), 900)
    assert second["state"] == "completed", second
    ids2 = json.loads(second["final_message_ids"])
    posted = [(await live.message(i))["content"] for i in ids2]
    print("AT15b", live.topic, "parts", len(ids2), "chars", [len(p) for p in posted])
    assert len(ids2) == 1, f"expected one part plus an upload, got {len(ids2)} parts"
    assert "/user_uploads/" in posted[0] and "answer-turn" in posted[0]
    rendered = await live.message(ids2[0], markdown=True)
    assert "user_uploads" in rendered["content"]
    print("AT15", live.topic, first["final_message_ids"], second["final_message_ids"])


# ------------------------------------------------------------------------------------ AT-16


def png_red_square(size: int = 64) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inside = size // 4 <= x < 3 * size // 4 and size // 4 <= y < 3 * size // 4
            row += bytes((220, 20, 20)) if inside else bytes((255, 255, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def test_at16_attachments(live):
    """A PNG and a CSV on the message land in .zab/inbox/, and the agent reads both (§10.3)."""
    png = png_red_square()
    csv = b"name,value\nalpha,1\nbeta,2\ngamma,3\n"
    uris = []
    for name, data, mime in (("red_square.png", png, "image/png"), ("data.csv", csv, "text/csv")):
        response = await live.tester.http.post("user_uploads", files={"file": (name, data, mime)})
        response.raise_for_status()
        body = response.json()
        uris.append(body.get("url") or body.get("uri"))
    mid = await live.send(
        f"{BOT} Two files are attached. Look at the image (use the Read tool on the PNG) and say "
        "in one sentence what shape and colour it shows; then read the CSV and give the sum of its "
        "value column. Reply with the colour, the shape and the sum.\n"
        f"[red_square.png]({uris[0]})\n[data.csv]({uris[1]})"
    )
    turn = await until(live.finished, 300)
    assert turn["state"] == "completed", turn
    inbox = live.repo / ".zab" / "inbox"
    files = sorted(p.name for p in inbox.iterdir())
    assert files == [f"{mid}-data.csv", f"{mid}-red_square.png"], files
    assert (inbox / f"{mid}-red_square.png").read_bytes() == png
    assert (inbox / f"{mid}-data.csv").read_bytes() == csv
    answer = await live.answer(turn)
    assert "red" in answer.lower() and "6" in answer, answer
    print("AT16", live.topic, mid, files, turn["final_message_ids"])


# ------------------------------------------------------------------------------------ AT-19


async def test_at19_worktrees(live_worktree):
    """Two concurrent worktree sessions in one project: separate branches and directories."""
    live = live_worktree
    topics = {"A": live.topic + "-a", "B": live.topic + "-b"}
    mids = {}
    for key, topic in topics.items():
        mids[key] = await live.send(
            f"{BOT} Create a file named marker_{key.lower()}.txt containing the word "
            f"{key}{key}{key}, then use the Bash tool to run `git branch --show-current`, `pwd` "
            "and `python3 -c 'import time; time.sleep(10)'` in the foreground (never in the "
            "background). Reply with the branch "
            f"name, the directory and the word {key}_DONE.",
            topic,
        )
    results = {}
    for key, topic in topics.items():
        results[key] = await until(lambda t=topic: live.finished(1, t), 300)
        assert results[key]["state"] == "completed", results[key]
    rows = {r["topic"]: r for r in live.runtime.store.rows("sessions")}
    a, b = rows[topics["A"]], rows[topics["B"]]
    assert a["workspace_mode"] == b["workspace_mode"] == "worktree"
    assert a["repo_root"] != b["repo_root"] and a["branch"] != b["branch"]
    worktrees = live.repo / ".zab" / "worktrees"
    for row in (a, b):
        assert Path(row["repo_root"]).parent == worktrees, row["repo_root"]
        assert row["branch"].startswith("zab/"), row["branch"]
    assert (Path(a["repo_root"]) / "marker_a.txt").read_text().strip() == "AAA"
    assert (Path(b["repo_root"]) / "marker_b.txt").read_text().strip() == "BBB"
    assert not (Path(a["repo_root"]) / "marker_b.txt").exists()
    assert not (Path(b["repo_root"]) / "marker_a.txt").exists()
    assert not (live.repo / "marker_a.txt").exists() and not (live.repo / "marker_b.txt").exists()
    ws = LocalWorkspace(str(live.repo))
    listing = (await ws.run(["git", "worktree", "list"])).stdout
    assert a["repo_root"] in listing and b["repo_root"] in listing
    branches = (await ws.run(["git", "branch", "--list", "zab/*"])).stdout
    assert a["branch"] in branches and b["branch"] in branches
    for key, row in (("A", a), ("B", b)):
        answer = await live.answer(results[key])
        assert row["branch"] in answer and f"{key}_DONE" in answer, answer
    # both turns overlapped (started before the other finished)
    turns = {
        k: live.runtime.store.rows("turns", session_id=r["id"])[0] for k, r in (("A", a), ("B", b))
    }
    assert (
        turns["A"]["started_at"] < turns["B"]["ended_at"]
        and turns["B"]["started_at"] < turns["A"]["ended_at"]
    )
    print("AT19", topics, {k: (r["repo_root"], r["branch"]) for k, r in (("A", a), ("B", b))})


# ------------------------------------------------------------------------------------ AT-11


async def test_at11_restart(tmp_path):
    """Kill `zab run` during a Claude turn, restart: the orphaned claude group is reaped, the
    status reads "interrupted by bridge restart", the next message resumes the same session
    (§7.8.1)."""
    repo = await scratch_repo(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[bridge]
default_project={json.dumps(str(repo))}
state_dir={json.dumps(str(tmp_path / "state"))}
status_edit_interval_s=60
[[bots]]
name="dev-claude"
zuliprc={json.dumps(str(CREDENTIALS / "dev-bot.zuliprc"))}
backend="claude"
allow_user_ids=[{TESTER_ID}]
[backends.claude]
kind="claude"
binary="claude"
accounts={{test={json.dumps(str(ACCOUNT))}}}
default_account="test"
defaults={{model="haiku", approval="bypassPermissions"}}
"""
    )
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    topic = "zab-m2-restart-" + str(time.time_ns())
    process = None
    db = None

    async def launch():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zab",
            "run",
            "--config",
            str(config),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(60):
            while True:
                line = await child.stderr.readline()
                assert line, "bridge exited before readiness"
                if b"zab ready" in line:
                    return child

    def group_alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True

    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == TESTER_ID
        process = await launch()
        m1 = await tester.request(
            "POST",
            "messages",
            type="stream",
            to=STREAM,
            topic=topic,
            content=(
                f"{BOT} The codeword is PELICAN. Use the Bash tool to run exactly "
                "`python3 -c 'import time; time.sleep(45); print(1)'` in the foreground (never in "
                "the background) and wait for "
                "its output; only after the command has returned, reply exactly RESTART_FINAL."
            ),
        )
        db = sqlite3.connect(tmp_path / "state/zab.db")
        db.row_factory = sqlite3.Row

        async def turn():
            row = db.execute("SELECT * FROM turns ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row and row["status_message_id"] and row["pgid"] else None

        running = await until(turn, 90)
        assert running["state"] == "running", running
        pgid = running["pgid"]
        assert group_alive(pgid), "the claude process group should be alive mid-turn"
        # TurnStarted comes right after the spawn, before the prompt is even written: wait until
        # the transcript shows the sleeping command's tool_use with no tool_result yet, so the
        # kill lands mid-tool and the resume has a transcript to resume (codex round 1 on #62).
        # The CLI runs tool commands in a process group of their own, so the claude group's
        # members are no evidence (checked on 2.1.287).
        session = db.execute(
            "SELECT * FROM sessions WHERE id=?", (running["session_id"],)
        ).fetchone()
        native_id = session["native_id"]

        async def tool_running():
            path = transcript_of(native_id)
            if path is None:
                return False
            text = path.read_text()
            return "time.sleep(45)" in text and '"tool_use"' in text and '"tool_result"' not in text

        await until(tool_running, 120)
        process.kill()
        await process.wait()
        await asyncio.sleep(1)
        assert group_alive(pgid), "SIGKILL on the bridge must orphan the turn's group, not end it"
        process = await launch()

        async def interrupted():
            row = db.execute("SELECT * FROM turns WHERE id=?", (running["id"],)).fetchone()
            return dict(row) if row and row["state"] != "running" else None

        final = await until(interrupted, 60)
        assert final["state"] == "interrupted", final
        await until(lambda: asyncio.sleep(0, result=not group_alive(pgid)), 20)
        status = (
            await tester.request(
                "GET", f"messages/{running['status_message_id']}", apply_markdown=False
            )
        )["message"]
        assert "interrupted by bridge restart" in status["content"], status["content"]
        session = dict(db.execute("SELECT * FROM sessions").fetchone())
        m2 = await tester.request(
            "POST",
            "messages",
            type="stream",
            to=STREAM,
            topic=topic,
            content=(
                "Without tools: reply exactly RESUMED_OK and then the codeword from my previous "
                "message."
            ),
        )

        async def second():
            row = db.execute("SELECT * FROM turns ORDER BY id DESC LIMIT 1").fetchone()
            return (
                dict(row)
                if row and row["id"] != running["id"] and row["state"] != "running"
                else None
            )

        done = await until(second, 180)
        assert done["state"] == "completed", done
        parts = []
        for mid in json.loads(done["final_message_ids"]):
            message = await tester.request("GET", f"messages/{mid}", apply_markdown=False)
            parts.append(message["message"]["content"])
        answer = "\n".join(parts)
        assert "RESUMED_OK" in answer, answer
        sessions = [dict(r) for r in db.execute("SELECT * FROM sessions").fetchall()]
        assert len(sessions) == 1 and sessions[0]["native_id"] == session["native_id"]
        transcript = transcript_of(session["native_id"])
        assert transcript is not None
        users = [
            json.loads(line)
            for line in transcript.read_text().splitlines()
            if '"type":"user"' in line or '"type": "user"' in line
        ]
        assert any("RESUMED_OK" in json.dumps(u) for u in users), (
            "the second turn resumed the transcript"
        )
        print(
            "AT11",
            topic,
            "m1",
            m1["id"],
            "m2",
            m2["id"],
            "session",
            session["native_id"],
            "pgid",
            pgid,
            "status",
            running["status_message_id"],
            "final",
            done["final_message_ids"],
            "recalled PELICAN:",
            "PELICAN" in answer,
        )
    finally:
        if process and process.returncode is None:
            process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), 20)
        if db:
            db.close()
        await tester.close()
