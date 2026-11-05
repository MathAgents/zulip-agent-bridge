"""Crash the bridge process, never the shared test daemon (AT-10)."""

import asyncio
import json
import os
import sqlite3
import sys
import time

import pytest
from test_codex_m1 import CREDENTIALS, TEST_HOME, TEST_SOCKET, until

from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "codex", reason="Codex opt-in required"),
]


async def test_kill_and_restart_during_turn(tmp_path):
    # Fixed dev-only socket; RPC initialization also verifies TEST_HOME.
    assert not TEST_SOCKET or TEST_SOCKET == "/tmp/zab-rc2-app-server.sock"  # noqa: S108
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = LocalWorkspace(str(repo))
    await ws.write_bytes("README.md", b"# Scratch crash test\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "README.md"],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
    ):
        assert (await ws.run(args)).exit_code == 0
    config = tmp_path / "config.toml"
    config.write_text(f"""[bridge]
default_project={json.dumps(str(repo))}
state_dir={json.dumps(str(tmp_path / "state"))}
status_edit_interval_s=60
[[bots]]
name="crash-test"
zuliprc={json.dumps(str(CREDENTIALS / "dev-bot.zuliprc"))}
backend="codex"
allow_user_ids=[1207964]
[backends.codex]
kind="codex"
codex_home={json.dumps(str(TEST_HOME))}
socket={json.dumps(TEST_SOCKET)}
""")
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    topic = "zab-m1-crash-" + str(time.time_ns())
    process = None
    db = None

    async def launch():
        nonlocal process
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zab",
            "run",
            "--config",
            str(config),
            env=dict(os.environ, CODEX_HOME=str(TEST_HOME)),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        process = child
        async with asyncio.timeout(30):
            while True:
                line = await child.stderr.readline()
                assert line, "bridge exited before readiness"
                if b"zab ready" in line:
                    return child

    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == 1207964
        process = await launch()
        assert process.returncode is None
        await tester.request(
            "POST",
            "messages",
            type="stream",
            to=639106,
            topic=topic,
            content=(
                "@**codex dev-gpt** Run a shell command that sleeps for 8 seconds, "
                "then reply exactly CRASH_FINAL_ONCE."
            ),
        )
        db = sqlite3.connect(tmp_path / "state/zab.db")
        db.row_factory = sqlite3.Row

        async def turn():
            row = db.execute("SELECT * FROM turns ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row and row["status_message_id"] else None

        running = await until(turn)
        assert running["state"] == "running", "turn completed before SIGKILL; AT-10 not exercised"
        process.kill()
        await process.wait()
        process = await launch()

        async def complete():
            row = await turn()
            assert process.returncode is None
            return row if row and row["state"] != "running" else None

        final = await until(complete)
        assert final["state"] == "completed"
        ids = json.loads(final["final_message_ids"])
        assert len(ids) == 1
        await asyncio.sleep(2)
        messages = (
            await tester.request(
                "GET",
                "messages",
                anchor="newest",
                num_before=100,
                num_after=0,
                apply_markdown=False,
                narrow=[
                    {"operator": "channel", "operand": 639106},
                    {"operator": "topic", "operand": topic},
                ],
            )
        )["messages"]
        assert (
            sum(
                m["sender_id"] == 1207969 and m["content"].strip() == "CRASH_FINAL_ONCE"
                for m in messages
            )
            == 1
        )
        status = next(m for m in messages if m["id"] == running["status_message_id"])
        assert "✅" in status["content"]
        print("AT10", topic, final["final_message_ids"])
    finally:
        if process and process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 15)
        if db:
            db.close()
        await tester.close()
