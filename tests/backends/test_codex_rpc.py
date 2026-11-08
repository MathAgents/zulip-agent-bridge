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


async def test_close_passes_on_a_cancellation_of_its_caller():
    """Review of #965: `close()` waits for the reader it cancelled. A cancel of its caller
    meanwhile (`CodexBackend.close()` cancelling `_run`, which closes the connection on its
    way out of a lost one) is no reader's end: swallowed, `_run` went on to reconnect, and
    `CodexBackend.close()` waited for it forever."""
    rpc = CodexRpc()
    unwinding = asyncio.Event()

    async def reader():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            unwinding.set()
            await asyncio.sleep(0.2)  # a reader slow to unwind
            raise

    rpc.reader = asyncio.create_task(reader())
    await asyncio.sleep(0)
    closing = asyncio.create_task(rpc.close())
    await asyncio.wait_for(unwinding.wait(), 5)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 5)
    await asyncio.wait_for(asyncio.gather(rpc.reader, return_exceptions=True), 5)
    other = CodexRpc()  # the reader's own cancellation still ends a close() normally
    other.reader = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.wait_for(other.close(), 5)
    assert other.reader is None
