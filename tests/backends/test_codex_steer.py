"""A Codex steer through the backend (issue #654), against the scripted app-server
`FakeAppServer` (SPEC §15.2): the steer's `userMessage` item comes out as the steer's
`ItemCompleted(USER_MESSAGE)`, with the core's client_id, which marks its message read."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from zab.backends.codex import CodexBackend
from zab.config import BackendConfig
from zab.core import types as t
from zab.testing.fake_codex import FakeAppServer

pytestmark = pytest.mark.contract
SCHEMAS = Path(__file__).resolve().parents[2] / "docs/protocol/codex-0.157.1"


async def test_a_steer_is_reported_when_codex_takes_it_in(tmp_path):
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True  # the turn runs until it is steered and finished
    path = str(tmp_path / "s.sock")
    async with fake.serve(path):
        backend = CodexBackend(
            "c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path)
        )
        await backend.start()
        try:
            await asyncio.wait_for(backend.ready.wait(), 5)
            sid = (
                await backend.create_session(t.SessionSpec(cwd=str(tmp_path), name="n"))
            ).native_id
            tid = await backend.start_turn(sid, t.UserInput(text="go", client_id="c1"))
            await backend.steer(sid, tid, t.UserInput(text="also this", client_id="c2"))
            await fake.finish(sid, "done")
            events = []
            async with asyncio.timeout(5):
                while not isinstance(event := await backend.queue.get(), t.TurnCompleted):
                    events.append(event)
        finally:
            await backend.close()
    users = [
        e
        for e in events
        if isinstance(e, t.ItemCompleted) and e.item.kind == t.ItemKind.USER_MESSAGE
    ]
    assert [(e.turn_id, e.item.client_id, e.item.text) for e in users] == [
        (tid, "c1", "go"),
        (tid, "c2", "also this"),
    ]
    notifications = Draft7Validator(json.loads((SCHEMAS / "ServerNotification.json").read_text()))
    steered = [m for d, m in fake.wire if d == "receive" and m.get("params", {}).get("item", {})
               .get("clientId") == "c2"]  # fmt: skip
    assert [m["method"] for m in steered] == ["item/started", "item/completed"]
    for message in steered:
        notifications.validate(message)
