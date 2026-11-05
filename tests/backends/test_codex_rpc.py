import asyncio
import json

import pytest
from websockets.asyncio.server import unix_serve

from zab.backends.codex_rpc import CodexRpc
from zab.core.types import BackendUnavailable

pytestmark = pytest.mark.contract


async def test_rpc_demultiplexes_requests_and_notifications_without_waiting(tmp_path):
    received = []

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            received.append(message)
            if message.get("method") == "initialize":
                await ws.send(
                    json.dumps({"id": message["id"], "result": {"codexHome": str(tmp_path)}})
                )
            elif message.get("method") == "account/read":
                await ws.send(
                    json.dumps(
                        {
                            "id": "approval",
                            "method": "item/fileChange/requestApproval",
                            "params": {},
                        }
                    )
                )
                await ws.send(json.dumps({"method": "turn/started", "params": {}}))
                await ws.send(json.dumps({"id": message["id"], "result": {"account": None}}))

    path = str(tmp_path / "rpc.sock")
    async with unix_serve(handler, path):
        rpc = CodexRpc()
        await rpc.connect(home=str(tmp_path), socket=path)
        assert await rpc.call("account/read", {}) == {"account": None}
        assert (await rpc.incoming.get())["id"] == "approval"
        assert (await rpc.incoming.get())["method"] == "turn/started"
        assert received[0]["params"]["capabilities"]["experimentalApi"]
        assert received[1] == {"method": "initialized"}
        for method in ["command/exec", "thread/shellCommand", "process/spawn"]:
            with pytest.raises(ValueError, match="unsupported"):
                await rpc.call(method, {})
        await rpc.close()


async def test_disconnect_fails_pending_requests(tmp_path):
    async def handler(ws):
        message = json.loads(await ws.recv())
        await ws.send(json.dumps({"id": message["id"], "result": {"codexHome": str(tmp_path)}}))
        await ws.recv()
        await ws.recv()
        await ws.close()

    path = str(tmp_path / "rpc.sock")
    async with unix_serve(handler, path):
        rpc = CodexRpc()
        await rpc.connect(home=str(tmp_path), socket=path)
        with pytest.raises(BackendUnavailable):
            await asyncio.wait_for(rpc.call("account/read", {}), 1)
        await rpc.close()


async def test_close_clears_socket_for_stdio_reconnect():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    rpc = CodexRpc()
    socket = AsyncMock()
    rpc.socket = socket
    await rpc.close()
    socket.close.assert_awaited_once()
    stdin = SimpleNamespace(write=Mock(), drain=AsyncMock())
    rpc.process = SimpleNamespace(stdin=stdin)
    rpc.up = True
    await rpc.send({"method": "initialized"})
    assert json.loads(stdin.write.call_args.args[0]) == {"method": "initialized"}
    socket.send.assert_not_awaited()
