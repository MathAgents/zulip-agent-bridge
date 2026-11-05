"""Codex rc2 live drivers for SPEC §17 AT-15/16/19; ZAB_LIVE=codex only.

Reuse B's acceptance payloads, driven through the isolated Codex fixture.
"""

import json
import os
import struct
import zlib
from pathlib import Path

import pytest
from test_codex_m1 import live as live
from test_codex_m1 import until

from zab.workspace.local import LocalWorkspace

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "codex", reason="Codex opt-in required"),
]
BOT = "@**codex dev-gpt**"


@pytest.fixture
async def live_worktree(live):
    live.runtime.config.bridge.default_workspace = "worktree"
    return live


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
    rendered = await live.tester.request("GET", f"messages/{ids2[0]}", apply_markdown=True)
    assert "user_uploads" in rendered["message"]["content"]
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
        f"{BOT} Two files are attached. Look at the attached image and say "
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
    assert "red" in answer.lower() and "square" in answer.lower() and "6" in answer, answer
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
            f"{key}{key}{key}, then use your shell tool to run `git branch --show-current`, `pwd` "
            "and `python3 -c 'import time; time.sleep(10)'` in the foreground (never in the "
            "background). Reply with the branch "
            f"name, the directory and the word {key}_DONE.",
            topic=topic,
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
