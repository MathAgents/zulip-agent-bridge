"""#858's probe of `thread/list` (`probes/codex_thread_list.py`), offline against the scripted
app-server `FakeAppServer`, so that its code runs before the test VM's time is spent on it.
The fake plays the model: it spawns the sub-agent once the probe's turn runs, then ends that
turn; the sub-agent's command is a stub. What the probe prints is only ids, kinds, statuses,
counts and seconds."""

from __future__ import annotations

import asyncio
import json

from e2e.probes import codex_thread_list as cl
from zab.testing.fake_codex import FakeAppServer

PHASES = [
    "1 the sub-agent's command running",
    "2 parent's turn ended",
    "3 new connection",
    "4 after the unload delay",
    "5 another new connection",
]


def made(tmp_path, socket):
    argv = ["--home", str(tmp_path), "--socket", socket, "--wait", "10", "--unload", "0"]
    return cl.probe(argv, workdir=tmp_path)


async def test_the_probe_lists_each_variant_in_each_phase_then_cleans_up(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(cl, "COMMAND_POLL_S", 0.05)
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True
    socket = str(tmp_path / "s.sock")
    probe = made(tmp_path, socket)
    probe.command_running = lambda: any(t.get("parent") for t in fake.threads.values())
    async with fake.serve(socket):
        running = asyncio.create_task(cl.main(probe))
        async with asyncio.timeout(10):
            while not any(  # noqa: ASYNC110 - the probe's own connection
                t["status"] == "inProgress" for th in fake.threads.values() for t in th["turns"]
            ):
                await asyncio.sleep(0.01)
        (parent,) = fake.threads
        child = await fake.spawn(parent)
        await asyncio.sleep(0.3)  # the probe's first snapshot, its parent's turn running
        await fake.finish(parent, "STARTED")
        assert await asyncio.wait_for(running, 30) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted({line["phase"] for line in lines if "phase" in line}) == PHASES
    states = [line["state"] for line in lines if "state" in line]
    assert [s["command_running"] for s in states] == [True] * 5
    assert states[1]["parent_turn_ended"] is True
    queries = [line for line in lines if line.get("query") and line["phase"].startswith("2 ")]
    assert len(queries) == len(cl.variants(probe.cwd)) == 2 + len(cl.KINDS) + 5
    bridge = next(q for q in queries if q["query"] == "the bridge's query")
    assert bridge["listed"] == 0 and bridge["probe_threads"] == []  # as 0.157.1 answered
    reads = {line["read"]: line for line in lines if line.get("read") and line["phase"][0] == "2"}
    assert reads[parent]["role"] == "parent" and reads[child]["parent"] == parent
    assert reads[child]["cwd_is_probe"] is True and reads[child]["loaded"] is True
    loaded = [line for line in lines if line.get("read") == "loaded threads but the parent"]
    assert len(loaded) == 5 and all(line["reach_the_parent"] == [child] for line in loaded)
    assert {"event": "clean-up", "interrupted": child} in lines
    assert fake.threads[child]["turns"][-1]["status"] == "interrupted"
    text = "\n".join(json.dumps(line) for line in lines)
    assert "spawn_agent" not in text and "STARTED" not in text  # no message text
    (start,) = fake.requests("thread/start")
    assert start["sandbox"] == "danger-full-access" and start["approvalPolicy"] == "never"


async def test_the_probe_aborts_when_the_subagents_command_never_runs(
    tmp_path, capsys, monkeypatch
):
    """The probe's first live run got no running sub-agent, so it tested nothing it was for:
    it now waits for the command, and says so and exits 3 without it."""
    monkeypatch.setattr(cl, "COMMAND_S", 0.2)
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True
    socket = str(tmp_path / "s.sock")
    probe = made(tmp_path, socket)
    probe.command_running = lambda: False
    async with fake.serve(socket):
        assert await asyncio.wait_for(cl.main(probe), 30) == 3
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"event": "aborted", "reason": "the sub-agent's command didn't run within 0 s"} in lines
    assert not any("phase" in line for line in lines)
