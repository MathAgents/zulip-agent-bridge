"""In spawn mode (`connect = "spawn"`, SPEC §8.1) the bridge owns its `codex app-server` child.
Every way the backend closes signals it (SIGTERM, #967), and every close that is not itself
cancelled waits for its exit (SIGKILL after 5 s). So a child that obeys SIGTERM, as the
stand-in below does, doesn't outlive the connection, nor run beside the next one for the same
`CODEX_HOME` (§8.1, one app-server per home). A cancelled close sends no SIGKILL.

Against a stdio stand-in for the app-server (§8.2, spawn mode), written into each test's own
directory: it answers `initialize` with its `CODEX_HOME`, unless that home holds a file
`silent`, answers every other request with `{}`, and runs until its stdin closes or a signal
ends it."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from zab.backends import codex
from zab.backends.codex import CodexBackend
from zab.backends.codex_rpc import CodexRpc
from zab.config import BackendConfig

pytestmark = pytest.mark.contract

STAND_IN = """
import json, os, sys

home = os.environ["CODEX_HOME"]
silent = os.path.exists(os.path.join(home, "silent"))
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    result = {}
    if message.get("method") == "initialize":
        if silent:
            continue
        result = {"codexHome": home, "userAgent": "stand-in/0"}
    sys.stdout.write(json.dumps({"id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def rpcs(monkeypatch) -> list[CodexRpc]:
    """Every connection the backend makes, in order."""
    made: list[CodexRpc] = []

    class Recorded(CodexRpc):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(codex, "CodexRpc", Recorded)
    return made


@pytest.fixture
async def spawned(tmp_path, rpcs):
    """A spawn-mode backend on the stand-in. Afterwards it is closed, a reader left behind is
    cancelled, and a child still running, as one a failed test leaves, is killed."""
    binary = tmp_path / "codex"
    binary.write_text("#!" + sys.executable + "\n" + STAND_IN)
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    config = BackendConfig(kind="codex", codex_home=str(home), connect="spawn", binary=str(binary))
    backend = CodexBackend("codex", config)
    yield backend
    try:
        async with asyncio.timeout(10):
            await backend.close()
    finally:  # also when that close fails or runs past its bound
        async with asyncio.timeout(10):
            for rpc in rpcs:
                if rpc.reader:
                    rpc.reader.cancel()
                    await asyncio.gather(rpc.reader, return_exceptions=True)
                if rpc.process and rpc.process.returncode is None:
                    rpc.process.kill()
                    await rpc.process.wait()


async def until(condition: Callable[[], object], seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition()


def silence(backend: CodexBackend) -> None:
    """The stand-in leaves `initialize` unanswered (a sync helper: ruff ASYNC240)."""
    Path(backend.home, "silent").touch()


async def test_close_terminates_the_app_server(spawned, rpcs):
    await spawned.start()
    async with asyncio.timeout(10):
        await spawned.ready.wait()
    process = rpcs[-1].process
    assert process and process.returncode is None
    async with asyncio.timeout(10):
        await spawned.close()
    assert spawned.task is None and rpcs[-1].reader is None
    assert process.returncode is not None  # waited for, not only signalled
    assert len(rpcs) == 1


async def test_close_during_the_handshake_terminates_the_app_server(spawned, rpcs):
    """`initialize` is never answered: `_run` waits in `connect()`, whose cleanup closes."""
    silence(spawned)
    await spawned.start()
    await until(lambda: rpcs and rpcs[-1].pending)  # `initialize` sent
    process = rpcs[-1].process
    async with asyncio.timeout(10):
        await spawned.close()
    assert spawned.task is None and not spawned.ready.is_set()
    assert process and process.returncode is not None
    assert len(rpcs) == 1


async def test_close_while_a_failed_connection_waits_for_its_reader(spawned, rpcs, monkeypatch):
    """#967: `_run` leaves its connection on an unexpected error (here the event mapper's), and
    its `finally` closes it, waiting for a reader slow to unwind. `CodexBackend.close()` cancels
    `_run` in that wait, and the cancel passes on (#965), so `CodexRpc.close()` goes no
    further. The child was signalled before that wait, so the stand-in, which obeys SIGTERM,
    still ends, and `_run` spawns no other. Before #967 the child kept running."""
    real = CodexRpc._read
    unwinding, release = asyncio.Event(), asyncio.Event()

    async def slow_read(self: CodexRpc) -> None:
        try:
            await real(self)
        except asyncio.CancelledError:
            unwinding.set()
            await release.wait()
            raise

    def failing(method: str, params: dict) -> list:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(CodexRpc, "_read", slow_read)
    await spawned.start()
    async with asyncio.timeout(10):
        await spawned.ready.wait()
    rpc = rpcs[-1]
    process = rpc.process
    assert process and process.returncode is None
    monkeypatch.setattr(spawned.mapper, "notification", failing)
    rpc.incoming.put_nowait({"method": "turn/started", "params": {}})
    async with asyncio.timeout(5):
        await unwinding.wait()  # `_run`'s `finally` waits in `rpc.close()`
    async with asyncio.timeout(10):
        await spawned.close()
    assert spawned.task is None and rpc.reader and not rpc.reader.done()
    async with asyncio.timeout(5):
        await process.wait()  # with the reader still unwinding
    release.set()
    async with asyncio.timeout(5):
        await asyncio.gather(rpc.reader, return_exceptions=True)
    assert len(rpcs) == 1
