"""Actual Claude adapter + fake CLI + fake Zulip: core M2 wiring (SPEC §9)."""

import asyncio
import json
from pathlib import Path

import pytest

from zab.commands.run import Runtime
from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core.session import Delivery
from zab.testing.fake_zulip import FakeZulip
from zab.zulip.client import ZulipClient

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


async def test_real_claude_adapter_posts_and_drains_held_inputs(tmp_path, monkeypatch):
    root = ROOT
    fake_binary = root / "src/zab/testing/fake_claude.py"
    fixture = root / "tests/fixtures/claude/2.1.283/simple.jsonl"
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(fixture))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LINGER", "0.2")
    log = tmp_path / "claude-log.jsonl"
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOG", str(log))
    fake = FakeZulip()
    monkeypatch.setattr(
        ZulipClient,
        "from_zuliprc",
        lambda _: ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport),
    )
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(state_dir=str(tmp_path / "state"), default_project=str(tmp_path)),
        [BotConfig("bot", "decoy", "claude", [20])],
        {
            "claude": BackendConfig(
                kind="claude", binary=str(fake_binary), accounts={"main": str(tmp_path / "account")}
            )
        },
        [],
        RenderConfig(),
    )
    runtime = Runtime(config)
    try:
        await runtime.start()
        bridge = runtime.bridges[0]
        bridge.listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bridge.listener
        ws = runtime.backends["claude"].workspace(str(tmp_path), str(tmp_path))
        await ws.run(["git", "init", "-q"])
        await ws.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "initial",
            ]
        )
        mid = await bridge.client.send(1, "topic", "@**Test Bot** hello")
        await bridge.handle(
            {
                "type": "message",
                "message": fake.messages[mid] | {"sender_id": 20},
                "flags": ["mentioned"],
            }
        )
        actor = next(iter(bridge.actors.values()))
        await actor.inbox.join()
        await asyncio.sleep(0)
        await actor.inbox.join()
        finished = asyncio.Event()
        complete = actor.complete

        async def observe_completion(*args, **kwargs):
            await complete(*args, **kwargs)
            if len(runtime.store.rows("turns", session_id=actor.row["id"], state="completed")) == 2:
                finished.set()

        actor.complete = observe_completion
        mids = []
        for text in ("second", "third"):
            mid = await bridge.client.send(1, "topic", text)
            mids.append(mid)
            actor.put(Delivery(mid, 20, "Tester", text))
        await asyncio.wait_for(finished.wait(), 10)
        turns = sorted(
            runtime.store.rows("turns", session_id=actor.row["id"]), key=lambda t: t["id"]
        )
        assert len(turns) == 2 and json.loads(turns[1]["trigger_msg_ids"]) == mids
        assert all(turn["pgid"] for turn in turns)
        assert all(turn["final_message_ids"] for turn in turns)
        assert not any("⚠️" in m["content"] for m in fake.messages.values())
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        for call in calls:
            argv = call["argv"]
            assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
            start = argv.index("--allowedTools") + 1
            assert argv[start : start + 5] == ["Read", "Glob", "Grep", "Edit", "Write"]
        assert "--resume" in calls[1]["argv"]
        assert "--append-system-prompt" in calls[1]["argv"]
    finally:
        await runtime.close()
