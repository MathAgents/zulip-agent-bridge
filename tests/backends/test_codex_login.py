import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import unix_serve

from zab.backends.codex import CodexBackend
from zab.config import BackendConfig
from zab.core import types as t
from zab.testing.fake_codex import FakeCodex

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "cancel", "timeout", "disconnect", "cancel_during_start"]
)
async def test_device_login_protocol(tmp_path, outcome):
    calls = []
    sent = asyncio.Event()
    starting, release = asyncio.Event(), asyncio.Event()
    fixture = tmp_path / "login.jsonl"
    notifications = [
        {
            "method": "account/login/completed",
            "params": {
                "loginId": "unrelated",
                "success": True,
            },
        }
    ]
    if outcome in ("success", "failure"):
        notifications.append(
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "decoy-login",
                    "success": outcome == "success",
                    "error": None,
                },
            }
        )
    fixture.write_text(
        "\n".join(json.dumps({"direction": "receive", "message": m}) for m in notifications)
    )
    fake = FakeCodex(fixture)

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if "id" not in msg:
                continue
            calls.append(msg)
            if msg["method"] == "initialize":
                result = {"codexHome": str(tmp_path)}
            elif msg["method"] == "account/login/start":
                assert msg["params"] == {"type": "chatgptDeviceCode"}
                if outcome == "cancel_during_start":
                    starting.set()
                    await release.wait()
                # Exercise completion arriving BEFORE the start response.
                async for notification in fake.messages():
                    await ws.send(json.dumps(notification))
                result = {
                    "type": "chatgptDeviceCode",
                    "loginId": "decoy-login",
                    "verificationUrl": "https://example.test/device",
                    "userCode": "DECOY",
                }
            else:
                assert msg["method"] == "account/login/cancel"
                assert msg["params"] == {"loginId": "decoy-login"}
                result = {"status": "canceled"}
            await ws.send(json.dumps({"id": msg["id"], "result": result}))
            if outcome == "disconnect" and msg["method"] == "account/login/start":
                await sent.wait()
                await ws.close()

    async def send(text):
        # the setting as ChatGPT names it (Nov 1), and nothing to paste back (#709)
        assert text == (
            "First, in ChatGPT's settings, under **Security and login**, turn on device code "
            "sign-in for Codex (*Enable device code sign-in for Codex*). Then open "
            "https://example.test/device and enter DECOY. Nothing to send back here: I'll "
            "answer ✅ when you're done."
        )
        sent.set()

    path = str(tmp_path / "login.sock")
    async with unix_serve(handler, path):
        backend = CodexBackend(
            "c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path)
        )
        await backend.start()
        try:
            await asyncio.wait_for(backend.ready.wait(), 1)
            receive = AsyncMock()
            task = asyncio.create_task(backend.login(None, send, receive))
            if outcome == "cancel_during_start":
                await asyncio.wait_for(starting.wait(), 1)
                task.cancel()
                release.set()
            else:
                await asyncio.wait_for(sent.wait(), 1)
            if outcome in ("cancel", "timeout", "cancel_during_start"):
                # Another bot sharing this backend cannot supersede the active login.
                assert await backend.login(None, send, receive) is False
                if outcome != "timeout":
                    if outcome == "cancel":
                        task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(task, 0.01)
                assert calls[-1]["method"] == "account/login/cancel"
            else:
                assert await asyncio.wait_for(task, 1) is (outcome == "success")
            receive.assert_not_awaited()
            assert backend.login_events is None
        finally:
            await backend.close()


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({"account": {"type": "chatgpt"}}, True),
        ({"account": None}, False),
        (t.BackendUnavailable("down"), None),
    ],
)
async def test_signed_in_is_the_doctors_account_read(tmp_path, answer, expected):
    """#687: `account/read` without a token refresh, as the doctor's `codex:login`."""
    backend = CodexBackend("c", BackendConfig(kind="codex", codex_home=str(tmp_path)))
    call = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock()
    call.return_value = answer
    backend._call = call
    asking = asyncio.create_task(backend.signed_in())
    await asyncio.sleep(0.05)
    assert not asking.done()  # the daemon is still connecting (a VM just woken): it waits
    call.assert_not_awaited()
    backend.ready.set()
    assert await asking is expected
    call.assert_awaited_once_with("account/read", refreshToken=False)
