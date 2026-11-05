"""Developer instructions survive a fresh-client resume (SPEC §8.4, §7.8)."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from zab.backends.codex import CodexBackend
from zab.config import BackendConfig
from zab.core import types as t
from zab.core.creation import context_prompt

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "codex", reason="Codex opt-in required"),
]


async def test_developer_instructions_survive_resume(tmp_path):
    home = str(Path.home() / ".codex-zabtest")
    config = BackendConfig(
        kind="codex", codex_home=home, socket=os.getenv("ZAB_CODEX_TEST_SOCKET", "")
    )
    marker = "ZAB_CONTEXT_" + uuid4().hex
    spec = t.SessionSpec(
        cwd=str(tmp_path),
        name="context resume verification",
        settings=t.SessionSettings(approval="never", sandbox="read-only"),
        context_prompt=context_prompt(True)
        + f"\nFor this verification use no tools. If asked CONTEXT_PROBE, reply exactly {marker}. "
        "Otherwise reply exactly READY.",
    )

    async def answer(backend, sid, text):
        tid = await backend.start_turn(sid, t.UserInput(text=text, client_id=str(uuid4())))
        async with asyncio.timeout(120):
            async for event in backend.events():
                if isinstance(event, t.TurnCompleted) and event.turn_id == tid:
                    assert event.status == t.TurnStatus.COMPLETED
                    return event.final_text.strip()

    backend = CodexBackend("codex", config)
    try:
        await backend.start()
        await asyncio.wait_for(backend.ready.wait(), 20)
        info = await backend.create_session(spec)
        first = await answer(backend, info.native_id, "Reply READY.")
        assert first == "READY" and marker not in first
    finally:
        await backend.close()
    resumed = CodexBackend("codex", config)
    resumes = []
    try:
        await resumed.start()
        await asyncio.wait_for(resumed.ready.wait(), 20)
        resumed.rpc.trace = lambda direction, msg: (
            resumes.append(msg["params"])
            if direction == "send" and msg.get("method") == "thread/resume"
            else None
        )
        # Deliberately provide no context on resume: zab never re-sends it to Codex.
        await resumed.open_session(
            info.native_id, t.SessionSpec(cwd=str(tmp_path), name=spec.name, settings=spec.settings)
        )
        assert resumes and all("developerInstructions" not in params for params in resumes)
        assert await answer(resumed, info.native_id, "CONTEXT_PROBE") == marker
    finally:
        await resumed.close()
