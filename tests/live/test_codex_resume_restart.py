"""Resume persisted instructions or install attach context across fresh servers (§8.4)."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from zab.backends.codex import CodexBackend
from zab.config import BackendConfig
from zab.core import types as t

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ZAB_LIVE") != "codex", reason="Codex opt-in required"),
]


@pytest.mark.parametrize("attach_context", [False, True])
async def test_resume_context_across_app_server_restart(tmp_path, attach_context):
    config = BackendConfig(
        kind="codex",
        connect="spawn",
        codex_home=str(Path.home() / ".codex-zabtest"),
        binary=os.getenv("ZAB_CODEX_TEST_BINARY", "codex"),
    )
    marker = "ZAB_PERSISTED_" + uuid4().hex
    instructions = f"Use no tools. For CONTEXT_PROBE reply exactly {marker}; otherwise reply READY."
    spec = t.SessionSpec(
        cwd=str(tmp_path),
        name="resume restart check",
        settings=t.SessionSettings(approval="never", sandbox="read-only"),
        context_prompt="" if attach_context else instructions,
    )

    async def answer(backend, sid, text):
        tid = await backend.start_turn(sid, t.UserInput(text=text, client_id=str(uuid4())))
        async with asyncio.timeout(120):
            async for event in backend.events():
                if isinstance(event, t.TurnCompleted) and event.turn_id == tid:
                    assert event.status == t.TurnStatus.COMPLETED
                    return event.final_text.strip()

    first = CodexBackend("codex", config)
    try:
        await first.start()
        await asyncio.wait_for(first.ready.wait(), 20)
        process = first.rpc.process
        info = await first.create_session(spec)
        assert await answer(first, info.native_id, "Reply READY.") == "READY"
    finally:
        await first.close()
    assert process.returncode is not None
    second = CodexBackend("codex", config)
    resumes = []
    try:
        await second.start()
        await asyncio.wait_for(second.ready.wait(), 20)
        assert second.rpc.process.pid != process.pid
        second.rpc.trace = lambda direction, msg: (
            resumes.append(msg["params"])
            if direction == "send" and msg.get("method") == "thread/resume"
            else None
        )
        await second.open_session(
            info.native_id,
            t.SessionSpec(
                cwd=str(tmp_path),
                name=spec.name,
                settings=spec.settings,
                context_prompt=instructions if attach_context else "",
            ),
        )
        assert ("developerInstructions" in resumes[0]) is attach_context
        assert await answer(second, info.native_id, "CONTEXT_PROBE") == marker
    finally:
        await second.close()
