"""A record of the identity the bridge applied vouches only for a load in the same run of the
app-server (issue #830, SPEC §5.5, §8.6, §8.7).

A loaded Codex thread keeps the environment its first loader gave it, so the bridge trusts a
thread the app-server holds only after its own load: this connection's, or one a record left
in this run of the app-server (`applied_instance`: the boot's id and the process's id and start
time, `codex_rpc.app_server_instance`). After a boot, or a restart of the daemon alone, every
earlier load is gone, and a terminal's client may load a thread before the bridge's reopen
reaches it. Against `FakeAppServer`, each of whose `restart()`s is a new run (`instance_of`)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backends.test_codex_fork import WHO, spec, turn, until
from backends.test_codex_lazy_open import daemon as daemon  # the fixture
from backends.test_codex_lazy_open import persisted, sessions
from zab.backends import codex_rpc
from zab.backends.codex import identity_config
from zab.backends.codex_rpc import CodexRpc, RpcError, app_server_instance
from zab.core import types as t
from zab.testing.fake_codex import FakeAppServer, RpcFailure

pytestmark = pytest.mark.contract
LINUX = Path(codex_rpc.BOOT_ID).exists()


def since(fake: FakeAppServer, start: int) -> list[str]:
    return [call["method"] for call in fake.calls[start:]]


async def test_after_a_boot_a_thread_a_terminal_loaded_first_is_refused(daemon):
    """The boot race: the app-server restarts (a boot, or the daemon alone), and a terminal's
    client loads a thread before the bridge reopens it. The record its load left in the
    earlier run vouches for nothing there: the thread is checked as an unknown identity and
    refused (§5.5), the config read before the check, so that only the resume's send is left
    between them (review of #818, N1). The other session reopens unloaded. Once the terminal
    quits and the thread unloads, `!status` reopens it, and the bridge's own load runs its
    turn, recorded in this run."""
    fake, connect = daemon
    made = await sessions(await connect(), 2)
    before = made[0].settings.extra["applied_instance"]
    assert before == fake.instance
    fake.restart()
    held, idle = made[0].native_id, made[1].native_id
    fake.loaded.add(held)  # the terminal's `codex resume`, first
    backend = await connect()
    start = len(fake.calls)
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await backend.open_session(held, persisted(made[0]))
    assert since(fake, start) == ["thread/read", "config/read", "thread/loaded/list"]
    await backend.open_session(idle, persisted(made[1]))
    assert idle in backend.mapper.closed and not fake.requests("thread/resume")

    fake.unload(held)  # the terminal quit; the app-server unloads it a minute later
    await backend.open_session(held, persisted(made[0]))  # `!status`
    await turn(backend, held, "mine now", wait=False)
    changed = await until(backend, t.SessionChanged, held)
    assert changed.settings.extra["applied_instance"] == fake.instance != before
    await until(backend, t.TurnCompleted, held)
    assert [r["threadId"] for r in fake.requests("thread/resume")] == [held]


async def test_a_record_from_before_830_vouches_for_nothing(daemon):
    """A record without its run, as every one written before #830, could be from any boot: a
    thread loaded at the reopen is checked, and refused until it unloads (the app-server
    unloads it a minute after the old bridge leaves), then reopened unloaded."""
    fake, connect = daemon
    (made,) = await sessions(await connect(), 1)
    extra = {k: v for k, v in made.settings.extra.items() if k != "applied_instance"}
    old = replace(made, settings=replace(made.settings, extra=extra))
    backend = await connect()  # the bridge alone restarts: the thread is still loaded
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await backend.open_session(made.native_id, persisted(old))
    fake.unload(made.native_id)
    await backend.open_session(made.native_id, persisted(old))
    assert made.native_id in backend.mapper.closed
    await turn(backend, made.native_id, "again")


async def test_without_the_run_no_record_vouches(daemon, monkeypatch):
    """Where the run can't be read (no `/proc`, no peer pid), a record vouches for no load
    after its connection: a restart of the bridge alone checks a thread it holds, refused."""
    _fake, connect = daemon
    monkeypatch.setattr(codex_rpc, "app_server_instance", lambda pid: None)
    (made,) = await sessions(await connect(), 1)
    assert made.settings.extra["applied_instance"] is None
    backend = await connect()
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await backend.open_session(made.native_id, persisted(made))


async def test_a_thread_this_connection_loaded_is_vouched_for_its_identity_only(daemon):
    """`!attach` passes no record (§8.7). A thread this connection started is vouched for by
    that load, for the identity it applied: attached again with it, it is resumed, and its
    record is this run's. For a bot with another identity (one backend can serve several), it
    is checked and refused: its shells run as the first one's."""
    fake, connect = daemon
    backend = await connect()
    (made,) = await sessions(backend, 1)
    bare = spec("/w/x", "topic")  # no record
    info = await backend.open_session(made.native_id, bare)
    assert info.settings.extra["applied_instance"] == fake.instance
    other = replace(bare, git_identity=t.GitIdentity("Codex · other", "other@example.test"))
    with pytest.raises(t.SessionRefused, match="unverified git identity"):
        await backend.open_session(made.native_id, other)


async def test_a_resume_that_fails_keeps_the_connections_own_load(daemon, monkeypatch):
    """Review of #854, N2: `!attach` (no record) of a thread this connection started, whose
    resume fails once. The connection still holds its own load: the retry resumes it, without
    a check, rather than refusing the bridge's own thread until a restart."""
    fake, connect = daemon
    backend = await connect()
    (made,) = await sessions(backend, 1)
    resume, failed = fake._thread_resume, []

    async def fail_once(params):
        if not failed:
            failed.append(params)
            raise RpcFailure(-32603, "resume failed")
        return await resume(params)

    monkeypatch.setattr(fake, "_thread_resume", fail_once)
    bare = spec("/w/x", "topic")
    with pytest.raises(RpcError):
        await backend.open_session(made.native_id, bare)
    assert made.native_id in backend.resumed  # restored: still this connection's own load
    info = await backend.open_session(made.native_id, bare)
    assert info.settings.extra["applied_instance"] == fake.instance
    assert not fake.requests("thread/loaded/list")
    assert made.native_id in backend.resumed


async def test_a_started_thread_closed_then_reloaded_is_checked_and_its_subagents_found(
    daemon, monkeypatch
):
    """With #851 and #858: `resumed` (what this connection loaded, with the identity applied)
    drops a thread at its `thread/closed`. A thread this connection started, then closed: a
    resume that fails leaves it out. The bridge's next turn then loads it again, checked as an
    unknown identity, and applies the identity anew, recorded in this run. A sub-agent spawned
    meanwhile, which no event told of, is found among the daemon's loaded threads: `!stop`
    stops it, and never says `unseen` (#858)."""
    fake, connect = daemon
    backend = await connect()
    (made,) = await sessions(backend, 1)
    sid = made.native_id
    assert sid in backend.resumed
    assert await backend.stop_subagents(sid) == t.SubagentStop(0)
    fake.loaded.discard(sid)  # the app-server unloaded it, and said so
    await fake._send(fake._note("thread/closed", threadId=sid))
    async with asyncio.timeout(5):
        while sid not in backend.mapper.closed:  # noqa: ASYNC110 - the backend's reader
            await asyncio.sleep(0.01)
    assert sid not in backend.resumed

    resume, failed = fake._thread_resume, []

    async def fail_once(params):
        if not failed:
            failed.append(params)
            raise RpcFailure(-32603, "resume failed")
        return await resume(params)

    monkeypatch.setattr(fake, "_thread_resume", fail_once)
    with pytest.raises(RpcError):
        await backend.start_turn(sid, t.UserInput(text="first", client_id="c-first"))
    assert sid not in backend.resumed
    checks = len(fake.requests("thread/loaded/list"))
    await turn(backend, sid, "again", wait=False)
    changed = await until(backend, t.SessionChanged, sid)
    await until(backend, t.TurnCompleted, sid)
    assert len(fake.requests("thread/loaded/list")) == checks + 1  # checked as unknown
    assert fake.requests("thread/resume")[-1]["config"] == identity_config(spec("/w/x", "x"))
    marker = {"name": WHO.name, "email": WHO.email}
    assert changed.settings.extra["applied_git_identity"] == marker
    assert changed.settings.extra["applied_instance"] == fake.instance
    assert backend.resumed[sid] == marker
    source = {"subAgent": {"thread_spawn": {"parent_thread_id": sid, "depth": 1}}}
    child = fake._new_thread({}, "/w/x", parent=sid, source=source, session=sid)
    child["turns"].append({"id": "turn-unseen", "status": "inProgress", "items": [], "started": 0})
    assert await backend.stop_subagents(sid) == t.SubagentStop(1)
    assert child["turns"][-1]["status"] == "interrupted"


@pytest.mark.skipif(not LINUX, reason="the run is read from Linux's /proc")
def test_a_run_is_the_boot_and_the_process_id_and_start_time(tmp_path):
    """`app_server_instance`: this boot's id, the pid and its start time (`/proc/<pid>/stat`,
    field 22, after the process name, which may hold spaces, parentheses and bytes that aren't
    UTF-8: review of #854, N1); None for a process that is gone, or no pid."""
    boot = Path(codex_rpc.BOOT_ID).read_text().strip()
    started = Path("/proc/self/stat").read_text().rpartition(")")[2].split()[19]
    assert app_server_instance(os.getpid()) == f"{boot}/{os.getpid()}/{started}"
    odd = tmp_path / os.fsdecode(b"a) (b\xff\xfe c")
    shutil.copy(shutil.which("sleep") or "/bin/sleep", odd)
    child = subprocess.Popen([odd, "30"])  # noqa: S603 - a fixed test argv
    try:
        theirs = app_server_instance(child.pid)
        stat = Path(f"/proc/{child.pid}/stat").read_bytes()
        assert b"(a) (b\xff\xfe c)" in stat
        assert theirs == f"{boot}/{child.pid}/{stat.rpartition(b')')[2].split()[19].decode()}"
    finally:
        child.kill()
        child.wait()
    assert app_server_instance(child.pid) is None
    assert app_server_instance(None) is None
    assert app_server_instance(0) is None


def test_a_run_not_as_expected_is_none(tmp_path, monkeypatch):
    """A boot id or a stat that can't be read as expected gives no run, so no earlier record
    vouches for a load, and the connection goes on (review of #854, N1)."""
    proc, boot = tmp_path / "proc", tmp_path / "boot_id"
    (proc / "7").mkdir(parents=True)
    monkeypatch.setattr(codex_rpc, "PROC", str(proc))
    monkeypatch.setattr(codex_rpc, "BOOT_ID", str(boot))
    stat = proc / "7" / "stat"
    good = b"7 (x) " + b" ".join([b"S", *[b"0"] * 18, b"4242", *[b"0"] * 5])
    boot.write_bytes(b"b0\n")
    stat.write_bytes(good)
    assert app_server_instance(7) == "b0/7/4242"
    assert app_server_instance(8) is None  # no such process
    for bad in (b"7 (x) S 0 0", good.replace(b"4242", b"42x2")):
        stat.write_bytes(bad)
        assert app_server_instance(7) is None
    stat.write_bytes(good)
    boot.write_bytes(b"\xff\xfe")
    assert app_server_instance(7) is None


async def test_a_connection_reads_the_run_of_the_process_on_the_socket(tmp_path):
    """The run is read from the process listening on the daemon's socket (`SO_PEERCRED`):
    here the fake, in this very process. A spawned app-server is the child."""
    fake = FakeAppServer(str(tmp_path))
    path = str(tmp_path / "s.sock")
    async with fake.serve(path):
        rpc = CodexRpc()
        await rpc.connect(home=str(tmp_path), socket=path)
        try:
            assert rpc._peer() == (os.getpid() if LINUX else None)
            assert rpc.instance == app_server_instance(os.getpid())
            assert (rpc.instance is not None) == LINUX
        finally:
            await rpc.close()
    spawned = CodexRpc()
    spawned.process = SimpleNamespace(pid=4321)
    assert spawned._peer() == 4321
    assert CodexRpc()._peer() is None  # not connected


async def test_a_fork_child_is_this_connections_own_load(daemon):
    """Review of #854, N6: a fork child, which `thread/fork` loaded on this connection, has
    its `resumed` entry with the identity the fork applied, so its next use resumes it as the
    bridge's own instead of checking it (which would only fail closed)."""
    fake, connect = daemon
    backend = await connect()
    (made,) = await sessions(backend, 1)
    child = await backend.fork_session(made.native_id, spec("/w/fork", "fork"))
    marker = {"name": WHO.name, "email": WHO.email}
    assert backend.resumed[child.native_id] == marker
    checks = len(fake.requests("thread/loaded/list"))
    await turn(backend, child.native_id, "in the fork")
    assert len(fake.requests("thread/loaded/list")) == checks  # no check: its own load
