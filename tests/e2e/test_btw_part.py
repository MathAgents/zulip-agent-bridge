"""Scenario 12's `!btw` helpers (#716), offline: the `lean-lsp-mcp` count and its sampling
while a reply is awaited, and the bridge log's side-question lines, against a fake VM, so that
their code is exercised before a live run."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from e2e import test_scenarios_features as features


class Vm:
    """`ps` answers each listing in turn (the last one again), `tail` the given log."""

    def __init__(self, listings: list[list[str]], log: str = "") -> None:
        self.listings, self.log, self.calls = listings, log, 0

    def run(self, argv, check=True, timeout=120):
        if argv[:1] == ["ps"]:
            listing = self.listings[min(self.calls, len(self.listings) - 1)]
            self.calls += 1
            return SimpleNamespace(ok=True, stdout="\n".join(listing))
        assert "tail" in argv and features.BRIDGE_LOG in argv, argv
        return SimpleNamespace(ok=True, stdout=self.log)


SERVER = "/home/agent/.local/share/uv/tools/lean-lsp-mcp/bin/python /usr/bin/lean-lsp-mcp"


def test_lean_servers_counts_the_servers_not_ps_itself():
    vm = Vm([[SERVER, SERVER, "ps -eo args=", "bash -c lean-lsp-mcp-free", "codex app-server"]])
    assert features.lean_servers(vm) == 2


async def test_the_peak_is_sampled_while_the_reply_is_awaited(monkeypatch):
    vm = Vm([[SERVER], [SERVER, SERVER, SERVER], [SERVER]])

    async def reply():
        await asyncio.sleep(0.1)
        return "answered"

    real_wait_for = asyncio.wait_for

    async def quick(awaitable, seconds):  # sample every 20 ms instead of 2 s
        return await real_wait_for(awaitable, min(seconds, 0.02))

    monkeypatch.setattr(features.asyncio, "wait_for", quick)
    waiting = asyncio.ensure_future(reply())
    assert await features.peak_lean_servers(vm, waiting) == 3
    assert await waiting == "answered" and vm.calls >= 3


def test_side_log_keeps_the_newest_line_of_each_event():
    lines = [
        {"event": "side_question_config", "msg": "old"},
        {"event": "zab.bridge", "msg": "unrelated"},
        {"event": "side_question_config", "msg": "config/read reported mcp_servers; off: …"},
        {"event": "side_question_mcp", "msg": "lean-lsp=disabled, lmfdb=disabled"},
    ]
    log = "\n".join([*(json.dumps(x) for x in lines), "not json", "[1, 2]"])
    assert features.side_log(Vm([[]], log)) == {
        "side_question_config": "config/read reported mcp_servers; off: …",
        "side_question_mcp": "lean-lsp=disabled, lmfdb=disabled",
    }
