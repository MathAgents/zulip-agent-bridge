"""Codex 0.157.1 JSON-RPC transport, verified against generated schemas (§8.2)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import unix_connect

from zab import __version__
from zab.core.types import BackendUnavailable
from zab.core.watchdog import waiting

METHODS = frozenset(
    {
        "initialize",
        "account/read",
        "account/rateLimits/read",
        "model/list",
        "config/read",
        "account/login/start",
        "account/login/cancel",
        "thread/start",
        "thread/resume",
        "thread/fork",
        "thread/read",
        "thread/list",
        "thread/loaded/list",
        "thread/turns/list",
        "thread/items/list",  # a side question: the running turn so far (#716)
        "thread/unsubscribe",  # a side question: its ephemeral fork, once answered (#716)
        "mcpServerStatus/list",  # a side question: its fork's MCP servers are off (#716)
        "thread/name/set",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
    }
)
#: How long a request waits for its answer (`CodexRpc.call`), unless it is kept.
CALL_TIMEOUT_S = 120.0
OPT_OUT = [
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
    "item/commandExecution/outputDelta",
    "item/plan/delta",
    "item/fileChange/patchUpdated",
    "command/exec/outputDelta",
]


class RpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "Codex RPC error"))
        self.code = error.get("code")
        self.data = error.get("data")


class CodexRpc:
    def __init__(self, trace: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.trace = trace
        self.pending: dict[int, asyncio.Future] = {}
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.ids = itertools.count(1)
        self.reader: asyncio.Task | None = None
        self.socket = None
        self.process: asyncio.subprocess.Process | None = None
        self.send_lock = asyncio.Lock()
        self.identity: dict[str, Any] = {}
        self.up = False

    async def connect(
        self, *, home: str, socket: str = "", binary: str = "codex", spawn: bool = False
    ) -> None:
        if self.reader:
            raise RuntimeError("connection already opened")
        if spawn:
            env = dict(os.environ, CODEX_HOME=home)
            for key in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"):
                env.pop(key, None)
            self.process = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=128 * 1024 * 1024,
            )
        else:
            path = socket or str(Path(home) / "app-server-control/app-server-control.sock")
            self.socket = await unix_connect(
                path, "ws://localhost/", max_size=None, ping_interval=20
            )
        self.up = True
        self.reader = asyncio.create_task(self._read())
        try:
            self.identity = await self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "zab",
                        "title": "Zulip Agent Bridge",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True, "optOutNotificationMethods": OPT_OUT},
                },
            )
            actual = await asyncio.to_thread(Path(self.identity["codexHome"]).resolve)
            expected = await asyncio.to_thread(Path(home).resolve)
            if actual != expected:
                raise ValueError("app-server CODEX_HOME differs from configured home")
            await self.send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def send(self, message: dict[str, Any]) -> None:
        if not self.up:
            raise BackendUnavailable("Codex disconnected")
        payload = json.dumps(message)
        if self.trace:
            self.trace("send", message)
        async with self.send_lock:
            if self.socket:
                await self.socket.send(payload)
            elif self.process and self.process.stdin:
                self.process.stdin.write((payload + "\n").encode())
                await self.process.stdin.drain()

    async def call(
        self, method: str, params: dict[str, Any], *, keep: bool = False
    ) -> dict[str, Any]:
        """The result of one request, within `CALL_TIMEOUT_S`. `keep`: however long it takes,
        until the answer or the disconnect, which fails every waiter (`_read`): for a request
        whose answer must not be lost (a side question's fork, codex round 4 on #739)."""
        if method not in METHODS:
            raise ValueError(f"unsupported app-server method: {method}")
        for attempt in range(5):
            rid = next(self.ids)
            waiter = asyncio.get_running_loop().create_future()
            self.pending[rid] = waiter
            try:
                await self.send({"id": rid, "method": method, "params": params})
                deadline = None if keep else CALL_TIMEOUT_S
                return await waiting(asyncio.wait_for(waiter, deadline))
            except RpcError as exc:
                if exc.code != -32001 or attempt == 4:
                    raise
                await waiting(asyncio.sleep((2**attempt) * random.uniform(0.8, 1.2)))  # noqa: S311
            finally:
                self.pending.pop(rid, None)
        raise AssertionError("unreachable")

    async def _read(self) -> None:
        try:
            while True:
                if self.socket:
                    raw = await self.socket.recv()
                else:
                    assert self.process and self.process.stdout
                    raw = await self.process.stdout.readline()
                    if not raw:
                        break
                message = json.loads(raw)
                if self.trace:
                    self.trace("receive", message)
                if "method" in message:
                    self.incoming.put_nowait(message)
                elif (waiter := self.pending.get(message.get("id"))) and not waiter.done():
                    if "error" in message:
                        waiter.set_exception(RpcError(message["error"]))
                    else:
                        waiter.set_result(message.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.incoming.put_nowait({"disconnected": type(exc).__name__})
        finally:
            self.up = False
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(BackendUnavailable("Codex disconnected"))
            self.incoming.put_nowait({"disconnected": "connection closed"})

    async def close(self) -> None:
        self.up = False
        if self.socket:
            await self.socket.close()
            self.socket = None
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
