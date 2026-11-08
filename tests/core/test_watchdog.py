import asyncio
import contextlib
import os
import socket
import sys
import time

import pytest
from test_bridge import bridge as bridge

from zab.core import bridge as bridge_module
from zab.core import watchdog

pytestmark = pytest.mark.integration


async def renewed(*heartbeats, times=3, within=10.0):
    """Wait until each heartbeat has been renewed `times` times from now, as a listener
    waiting in `watchdog.waiting` renews it every `WAIT_INTERVAL`. This counts renewals rather
    than bounding the newest one's age. `fresh(..., 0.04)` with renewals every 5 ms failed once
    under load, at #865's head: the test process was descheduled longer than that between a
    renewal and the check."""
    first = [heartbeat.last_seen for heartbeat in heartbeats]
    seen: list[set[float]] = [set() for _ in heartbeats]
    deadline = time.monotonic() + within
    while any(len(values) < times for values in seen):
        assert time.monotonic() < deadline, "a waiting listener stopped renewing its lease"
        await asyncio.sleep(0.001)
        for values, heartbeat, start in zip(seen, heartbeats, first, strict=True):
            if heartbeat.last_seen != start:
                values.add(heartbeat.last_seen)


async def test_heartbeat_event_advances_cursor_and_file(bridge):
    b, fake, _ = bridge
    fake.emit({"type": "heartbeat"})
    advanced = asyncio.Event()
    original = b.heartbeat.advance

    async def advance():
        await original()
        advanced.set()

    b.heartbeat.advance = advance
    task = asyncio.create_task(b.listen())
    try:
        await asyncio.wait_for(advanced.wait(), 1)
        assert b.store.get("last_event_id:bot") == fake._event_id
        assert b.heartbeat.path == b.store.state_dir / "heartbeat-bot"
        assert b.heartbeat.path.stat().st_mtime > time.time() - 5
        assert watchdog.fresh([b.heartbeat], 300)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("stalled", [False, True])
async def test_one_stalled_bot_suppresses_pings_and_old_files_are_not_progress(
    tmp_path, monkeypatch, stalled
):
    first = watchdog.Heartbeat(tmp_path, "a")
    second = watchdog.Heartbeat(tmp_path, "b")
    await first.advance()
    await second.advance()
    first.pulse()
    second.pulse()
    assert watchdog.fresh([first, second], 300)
    if stalled:
        second.last_seen -= 151
    assert watchdog.fresh([first, second], 300) != stalled
    assert not watchdog.fresh([watchdog.Heartbeat(tmp_path, "a")], 300)
    sent = []
    checked = asyncio.Event()
    monkeypatch.setattr(watchdog, "notify", sent.append)

    async def sleep(_):
        checked.set()
        await asyncio.Future()

    monkeypatch.setattr(watchdog, "sleep", sleep)
    task = asyncio.create_task(watchdog.watch([first, second], 300))
    await checked.wait()
    assert sent == ([] if stalled else ["WATCHDOG=1"])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("abstract", [False, True])
def test_notify_sends_real_unix_datagrams(tmp_path, monkeypatch, abstract):
    address = str(tmp_path / "notify")
    if abstract and sys.platform != "linux":
        pytest.skip("abstract Unix sockets are Linux-only")
    if abstract:
        address = "\0zab-watchdog-test-" + str(os.getpid())
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(address)
        server.settimeout(1)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + address[1:] if abstract else address)
        watchdog.notify("READY=1")
        watchdog.notify("WATCHDOG=1")
        assert server.recv(1024) == b"READY=1"
        assert server.recv(1024) == b"WATCHDOG=1"


def test_watchdog_respects_pid_and_missing_environment(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert watchdog.interval() == 0
    monkeypatch.setenv("WATCHDOG_USEC", "300000000")
    assert watchdog.interval() == 300
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert watchdog.interval() == 0


@pytest.mark.parametrize("uptime", [0.0, 1000.0, 1_000_000.0])
@pytest.mark.parametrize("kind", ["transport", "server", "revoked", "rate_limit"])
async def test_outage_passes_remain_live_without_event_progress(bridge, monkeypatch, kind, uptime):
    import httpx

    from zab.zulip.client import ZulipError

    b, _, _ = bridge
    real_monotonic, real_sleep = time.monotonic, asyncio.sleep
    scheduled = asyncio.Event()
    asyncio.get_running_loop().call_later(0, scheduled.set)
    clock = [uptime]
    monkeypatch.setattr(watchdog, "monotonic", lambda: clock[0])
    seen = []

    async def events(*args, **kwargs):  # after the first failure, probes with dont_block
        seen.append(watchdog.fresh([b.heartbeat], 300))
        clock[0] += 90
        if len(seen) == 8:
            raise asyncio.CancelledError
        if kind == "transport":
            raise httpx.ConnectError("offline")
        raise ZulipError("TEST", {"server": 503, "revoked": 401, "rate_limit": 429}[kind], 1)

    async def sleep(delay):
        clock[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(b.client, "events", events)
    monkeypatch.setattr(bridge_module, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert all(seen) and clock[0] - uptime > 600
    assert scheduled.is_set()  # unrelated event-loop callbacks still run
    assert time.monotonic is real_monotonic and asyncio.sleep is real_sleep
    assert not b.metric_task.done()  # keep the real metrics task alive throughout
    assert not b.heartbeat.path.exists()  # no event progress was fabricated


async def test_known_wait_keeps_lease_but_child_cannot_mask_hung_handler(tmp_path, monkeypatch):
    h = watchdog.Heartbeat(tmp_path, "bot")
    clock = [1000.0]
    monkeypatch.setattr(watchdog, "monotonic", lambda: clock[0])
    h.bind()
    future = asyncio.get_running_loop().create_future()

    async def wait(fs, **kwargs):
        assert watchdog.fresh([h], 300)
        clock[0] += 100
        await asyncio.sleep(0)
        if clock[0] == 1700:
            future.set_result("done")

    monkeypatch.setattr(watchdog, "wait", wait)
    assert await watchdog.waiting(future) == "done"
    assert h.last_seen == 1600
    # A child inherits CURRENT but cannot renew the listener's lease.
    clock[0] += 300
    child_result = asyncio.get_running_loop().create_future()
    child_result.set_result(None)
    await asyncio.create_task(watchdog.waiting(child_result))
    assert not watchdog.fresh([h], 300)


async def test_hung_handler_stops_liveness(bridge, monkeypatch):
    b, fake, _ = bridge
    entered = asyncio.Event()

    async def handle(event):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(b, "handle", handle)
    fake.emit({"type": "heartbeat"})
    task = asyncio.create_task(b.listen())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        b.heartbeat.last_seen -= 451
        assert not watchdog.fresh([b.heartbeat], 300)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_two_listeners_waiting_on_topic_creation_stay_live(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "WAIT_INTERVAL", 0.005)
    lock = asyncio.Lock()
    owner = watchdog.Heartbeat(tmp_path, "owner")
    peer = watchdog.Heartbeat(tmp_path, "peer")
    entered, release = asyncio.Event(), asyncio.Event()

    async def create():
        owner.bind()
        async with watchdog.waiting_lock(lock):
            entered.set()
            await watchdog.waiting(release.wait())  # card POST rate-limit wait

    async def sibling():
        peer.bind()
        async with watchdog.waiting_lock(lock):
            pass

    first = asyncio.create_task(create())
    await entered.wait()
    second = asyncio.create_task(sibling())
    try:
        await renewed(owner, peer)  # both renew their leases while they wait
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        assert lock.locked()  # cancelling the waiter must not release the owner's lock
    finally:
        release.set()
        await first
    assert not lock.locked()


async def test_listener_waiting_for_recovery_creation_lock_stays_live(bridge, monkeypatch):
    b, _, _ = bridge
    monkeypatch.setattr(watchdog, "WAIT_INTERVAL", 0.005)
    await b.creation_lock.acquire()

    async def listener():
        b.heartbeat.bind()
        await b.create_pending({"id": 999}, "unused", {})

    task = asyncio.create_task(listener())
    try:
        await renewed(b.heartbeat)
        assert not task.done()  # still waiting for the lock
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert b.creation_lock.locked()
    b.creation_lock.release()


async def test_reaction_recovery_actor_queue_wait_stays_live(bridge, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    b, _, _ = bridge
    monkeypatch.setattr(watchdog, "WAIT_INTERVAL", 0.005)
    queue = asyncio.Queue()
    queue.put_nowait("actor awaiting rate-limited poster")
    actor = Mock(
        ready=True,
        inbox=queue,
        row={"id": "missing", "state": "running"},
        turn={"status_message_id": 123},
    )
    b.actors["probe"] = actor
    monkeypatch.setattr(
        b.poster,
        "call",
        AsyncMock(
            return_value={"message": {"reactions": [{"user_id": 20, "emoji_code": "1f44d"}]}}
        ),
    )
    monkeypatch.setattr(b, "handle", AsyncMock())

    async def listener():
        b.heartbeat.bind()
        await b.reconcile_reactions()

    task = asyncio.create_task(listener())
    try:
        await renewed(b.heartbeat)
        assert not task.done()
        queue.task_done()
        await asyncio.wait_for(task, 1)
    finally:
        b.actors.pop("probe")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_listener_queue_joins_and_locks_use_liveness_wrappers():
    """Keep the audited listener modules from adding another unrenewed queue wait."""
    import ast
    from pathlib import Path

    core = Path(__file__).resolve().parents[2] / "src/zab/core"
    for name in ("bridge", "attach", "creation", "login"):
        tree = ast.parse((core / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                fn = node.value.func
                assert not (isinstance(fn, ast.Attribute) and fn.attr in {"join", "acquire"}), (
                    name,
                    node.lineno,
                    "wrap queue/lock waits with waiting()",
                )
            if isinstance(node, ast.AsyncWith):
                for item in node.items:
                    ctx = item.context_expr
                    assert not (isinstance(ctx, ast.Attribute) and ctx.attr.endswith("lock")), (
                        name,
                        node.lineno,
                        "use waiting_lock()",
                    )


async def test_stall_diagnostics_report_each_bot_once_and_again_after_recovery(
    tmp_path, monkeypatch, caplog
):

    from zab.core.logging import JsonFormatter

    clock = [0.0]
    monkeypatch.setattr(watchdog, "monotonic", lambda: clock[0])
    heartbeats = [watchdog.Heartbeat(tmp_path, name) for name in ("codex", "claude")]
    entered = [asyncio.Event(), asyncio.Event()]

    async def hung_listener(index):
        private_input = "PRIVATE_LOGIN_CODE"
        heartbeats[index].bind()
        entered[index].set()
        await asyncio.Future()
        return private_input

    tasks = [asyncio.create_task(hung_listener(i)) for i in range(2)]
    await asyncio.gather(*(event.wait() for event in entered))
    sent = []
    monkeypatch.setattr(watchdog, "notify", sent.append)
    ticks = 0

    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            clock[0] = 151
        elif ticks == 2:
            clock[0] = 225  # still stale: do not repeat either diagnostic
        elif ticks == 3:
            for heartbeat in heartbeats:
                heartbeat.pulse()
        elif ticks == 4:
            clock[0] = 400
        else:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    monkeypatch.setattr(watchdog, "sleep", sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await watchdog.watch(heartbeats, 300)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    records = [r for r in caplog.records if r.name == "zab.watchdog"]
    assert [r.bot for r in records] == ["codex", "claude", "codex", "claude"]
    assert all(r.event == "watchdog_stale" for r in records)
    assert "last_seen_age=151.0s" in records[0].getMessage()
    assert "last_seen_age=175.0s" in records[-1].getMessage()
    assert all(
        "test_watchdog.py:" in r.getMessage() and "hung_listener" in r.getMessage() for r in records
    )
    assert "PRIVATE_LOGIN_CODE" not in "".join(JsonFormatter().format(r) for r in records)
    assert sent == ["WATCHDOG=1", "WATCHDOG=1"]


async def test_unbound_listener_diagnostic(tmp_path, monkeypatch, caplog):
    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(watchdog, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await watchdog.watch([watchdog.Heartbeat(tmp_path, "unbound")], 300)
    assert "bot=unbound last_seen_age=never" in caplog.text
    assert "task absent or finished" in caplog.text
