import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zab.commands import run

pytestmark = pytest.mark.integration


async def test_serve_emits_readiness_after_start(tmp_path, monkeypatch, capsys):
    runtime = SimpleNamespace(bridges=[], pumps=[], close=AsyncMock())

    async def start():
        assert "zab ready" not in capsys.readouterr().err
        runtime.pumps.append(asyncio.create_task(asyncio.sleep(0)))

    runtime.start = start
    monkeypatch.setattr(run, "Runtime", lambda config: runtime)
    await run.serve(SimpleNamespace(bridge=SimpleNamespace(state_dir=str(tmp_path))))
    assert "zab ready" in capsys.readouterr().err
    runtime.close.assert_awaited_once()
