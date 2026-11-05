import argparse
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zab.dev import record_codex

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("version", ["0.159.0", None])
async def test_recorder_rejects_version_mismatch_before_scenario(tmp_path, monkeypatch, version):
    rpc = SimpleNamespace(
        identity={"userAgent": f"zab/{version} (Linux)"} if version else {},
        connect=AsyncMock(),
        close=AsyncMock(),
        call=AsyncMock(),
    )
    monkeypatch.setattr(record_codex, "CodexRpc", lambda trace: rpc)
    output = tmp_path / "capture.jsonl"
    args = argparse.Namespace(codex_home="/isolated", scenario="simple", output=str(output))
    with pytest.raises(ValueError, match="fixture pin"):
        await record_codex.record(args)
    rpc.call.assert_not_awaited()
    rpc.close.assert_awaited_once()
    assert json.loads(output.read_text().splitlines()[0])["version"] == version
