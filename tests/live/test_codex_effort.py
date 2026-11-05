"""First-turn workshop effort through the real CLI and fake Zulip (SPEC §8.4, B.2)."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zab.backends.codex import CodexBackend
from zab.config import BackendConfig
from zab.core import types as t
from zab.core.session import Delivery, SessionActor
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient
from zab.zulip.poster import Poster

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "codex", reason="Codex opt-in required"),
]


async def test_fresh_workshop_turn_sends_and_displays_high(tmp_path):
    home = str(Path.home() / ".codex-zabtest")
    backend = CodexBackend(
        "codex",
        BackendConfig(
            kind="codex",
            connect="spawn",
            codex_home=home,
            binary=os.getenv("ZAB_CODEX_TEST_BINARY", "codex"),
        ),
    )
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    poster, store, actor = Poster(client), Store(tmp_path / "state"), None
    requests = []
    try:
        await backend.start()
        await asyncio.wait_for(backend.ready.wait(), 20)
        backend.rpc.trace = lambda direction, msg: (
            requests.append(msg["params"]["effort"])
            if (direction == "send" and msg.get("method") == "turn/start")
            else None
        )
        settings = t.SessionSettings(effort="high", approval="never", sandbox="danger-full-access")
        info = await backend.create_session(
            t.SessionSpec(cwd=str(tmp_path), name="effort check", settings=settings)
        )
        await client.register()
        card = await client.send(1, "topic", "**zab session**")
        store.insert(
            "sessions",
            id="bridge-id",
            bot="bot",
            backend="codex",
            native_id=info.native_id,
            card_message_id=card,
            stream_id=1,
            topic="topic",
            workspace=str(tmp_path),
            workspace_mode="shared",
            repo_root=str(tmp_path),
            state="idle",
            settings_json=json.dumps({"effort": "high"}),
            created_at=0,
            updated_at=0,
        )
        manager = AsyncMock()
        manager.snapshot.return_value = None
        context = t.RenderContext(
            workspace=LocalWorkspace(str(tmp_path)),
            max_message_length=10000,
            max_upload_bytes=100000,
            upload=None,
        )
        actor = SessionActor(
            store.rows("sessions")[0],
            store,
            backend,
            poster,
            MarkdownRenderer(),
            manager,
            context,
            asyncio.Semaphore(1),
            machine="test",
            resume="codex",
        )
        mid = await client.send(1, "topic", "Reply exactly OK. Do not use tools.")
        actor.put(Delivery(mid, 20, "Tester", "Reply exactly OK. Do not use tools."))
        started_header = None
        async with asyncio.timeout(90):
            async for event in backend.events():
                if event.session_id != info.native_id:
                    continue
                actor.put(event)
                await actor.inbox.join()
                if isinstance(event, t.TurnStarted):
                    started_header = fake.messages[actor.turn["status_message_id"]][
                        "content"
                    ].splitlines()[0]
                    assert event.effort == "high" and "effort ` high `" in started_header
                if isinstance(event, t.TurnCompleted):
                    assert event.status == t.TurnStatus.COMPLETED
                    break
        assert requests == ["high"] and started_header is not None
        final = store.rows("turns")[0]
        assert (
            "effort ` high `"
            in fake.messages[final["status_message_id"]]["content"].splitlines()[0]
        )
    finally:
        if actor:
            await actor.close()
        await poster.close()
        await client.close()
        store.close()
        await backend.close()
