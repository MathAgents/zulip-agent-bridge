import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from zab.commands.run import Runtime
from zab.core.session import Reopen
from zab.core.types import BackendStatus

pytestmark = pytest.mark.integration
PROC = os.path.isdir("/proc")  # the Lean reaper runs only with it (SPEC §7.12)


@pytest.fixture(autouse=True)
def detached_storage_is_separately_tested(monkeypatch):
    # These dispatch probes use minimal bridge doubles. Real detached storage and
    # finish_recovery integration are covered in tests/core/test_detached.py.
    monkeypatch.setattr("zab.commands.run.detached.recover", AsyncMock())
    monkeypatch.setattr("zab.commands.run.detached.completed", Mock())


async def test_each_reconnect_hands_every_live_actor_one_reopen_request():
    """The pump takes no snapshot itself: each actor takes its own in its loop (§7.8.1)."""

    def actor(state):
        return SimpleNamespace(
            row={"state": state}, put=Mock(), inbox=SimpleNamespace(join=AsyncMock())
        )

    idle, running, readonly, detached = (
        actor(state) for state in ("idle", "running", "readonly", "detached")
    )
    bridge = SimpleNamespace(
        client=SimpleNamespace(metrics=Mock()),
        attaching={},
        bot=SimpleNamespace(backend="codex"),
        actors={"a": idle, "b": running, "c": readonly, "d": detached},
        reconcile_reactions=AsyncMock(),
        retry_renames=AsyncMock(),
        recover_pending_creations=AsyncMock(),
        run_pending_forks=AsyncMock(),
    )

    async def events():
        yield BackendStatus(session_id="", up=True)
        yield BackendStatus(session_id="", up=False)
        yield BackendStatus(session_id="", up=True)  # a down -> up cycle reopens everything

    backend = SimpleNamespace(events=events, open_session=AsyncMock())
    runtime = Runtime.__new__(Runtime)
    runtime.bridges = [bridge]
    runtime.recoveries = {}
    await runtime.pump("codex", backend)
    backend.open_session.assert_not_awaited()
    first, down, second = (call.args[0] for call in idle.put.call_args_list)
    assert isinstance(first, Reopen) and isinstance(second, Reopen) and first is not second
    assert down == ("!backend-down",)
    # One request per round, shared: a BackendUnavailable stops the rest of that round.
    assert [call.args[0] for call in running.put.call_args_list] == [first, down, second]
    readonly.put.assert_not_called()
    detached.put.assert_not_called()
    await runtime.recoveries[id(bridge)]
    bridge.reconcile_reactions.assert_awaited_once()


@pytest.mark.parametrize("kind", ["codex", "claude"])
async def test_runtime_caps_manager_at_registered_server_limit(tmp_path, monkeypatch, kind):
    from zab.commands import run
    from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig

    async def events():
        if False:
            yield

    backend = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), events=events)
    monkeypatch.setattr(run, "make_backend", lambda *args: backend)

    class Bridge:
        def __init__(self, config, bot, store, backend, manager, renderer, slots):
            self.bot, self.manager = bot, manager
            self.client = SimpleNamespace(limits={"max_file_upload_size_mib": 10})
            self.actors = {}
            self.initialized = self.unverified = False
            self.close = AsyncMock()

        async def initialize(self):
            self.initialized = True

        def unverify(self):
            self.unverified = True

        async def start(self):
            assert len(self.all_bridges) == 3
            assert all(b.initialized for b in self.all_bridges)
            assert all(b.unverified for b in self.all_bridges)  # before any replay (#878)

    monkeypatch.setattr(run, "BotBridge", Bridge)
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(state_dir=str(tmp_path)),
        [
            BotConfig("bot", "unused", "codex"),
            BotConfig("peer", "unused", "codex"),
            BotConfig("other", "unused", "other"),
        ],
        {"codex": BackendConfig(kind=kind), "other": BackendConfig(kind="claude")},
        [],
        RenderConfig(max_upload_mb=20),
    )
    runtime = Runtime(config)
    try:
        await runtime.start()
        assert runtime.bridges[0].manager.max_upload_bytes == 10 * 1024**2
        assert all(b.all_bridges is runtime.bridges for b in runtime.bridges)
        assert runtime.bridges[0].peers == runtime.bridges[:2]
        assert runtime.bridges[2].peers == [runtime.bridges[2]]
        # one Lean reaper for the machine, only with a Codex backend (SPEC §7.12, #249)
        assert (runtime.lean_reaper is not None) == (kind == "codex" and PROC)
    finally:
        await runtime.close()
    assert runtime.lean_reaper is None or runtime.lean_reaper.cancelled()


async def test_recovery_wait_does_not_block_other_backend_events():
    import asyncio

    from zab.core.types import TurnCompleted, TurnStatus

    gate = asyncio.Event()
    actor = SimpleNamespace(
        row={
            "state": "idle",
            "native_id": "s",
            "id": "s",
            "settings_json": "{}",
            "workspace": "/work",
            "topic": "topic",
            "bot": "bot",
        },
        backend=SimpleNamespace(kind="codex"),
        turn=None,
        store=SimpleNamespace(get=lambda key, default: default),
        put=Mock(),
        inbox=SimpleNamespace(join=gate.wait),
    )
    bridge = SimpleNamespace(
        client=SimpleNamespace(metrics=Mock()),
        attaching={},
        bot=SimpleNamespace(backend="codex"),
        actors={"s": actor},
        reconcile_reactions=AsyncMock(),
        retry_renames=AsyncMock(),
        recover_pending_creations=AsyncMock(),
        run_pending_forks=AsyncMock(),
    )
    done = TurnCompleted(session_id="s", turn_id="t", status=TurnStatus.COMPLETED)

    async def events():
        yield BackendStatus(session_id="", up=True)
        await asyncio.sleep(0)
        yield done

    runtime = Runtime.__new__(Runtime)
    runtime.bridges, runtime.recoveries = [bridge], {}
    backend = SimpleNamespace(events=events, open_session=AsyncMock(return_value="info"))
    await asyncio.wait_for(runtime.pump("codex", backend), 1)
    actor.put.assert_any_call(done)
    bridge.reconcile_reactions.assert_not_awaited()
    gate.set()
    await runtime.recoveries[id(bridge)]
    bridge.reconcile_reactions.assert_awaited_once()


async def test_reaction_recovery_retries_transient_failures():
    import asyncio

    import httpx

    bridge = SimpleNamespace(
        client=SimpleNamespace(metrics=Mock()),
        attaching={},
        retry_renames=AsyncMock(),
        recover_pending_creations=AsyncMock(),
        run_pending_forks=AsyncMock(),
        reconcile_reactions=AsyncMock(side_effect=[httpx.ConnectError("temporary"), None]),
    )
    runtime = Runtime.__new__(Runtime)
    await asyncio.wait_for(runtime.finish_recovery(bridge, []), 2)
    assert bridge.reconcile_reactions.await_count == 2


async def test_changed_session_rekeys_event_dispatch():
    from zab.core.types import SessionChanged, TurnCompleted, TurnStatus

    actor = SimpleNamespace(row={"state": "running"}, put=Mock())
    bridge = SimpleNamespace(
        bot=SimpleNamespace(backend="claude"), actors={"old": actor}, attaching={}
    )
    changed = SessionChanged(session_id="old", new_session_id="new")
    completed = TurnCompleted(session_id="new", turn_id="turn", status=TurnStatus.COMPLETED)

    async def events():
        yield changed
        yield completed

    runtime = Runtime.__new__(Runtime)
    runtime.bridges = [bridge]
    await runtime.pump("claude", SimpleNamespace(events=events))
    assert bridge.actors == {"new": actor}
    assert [call.args[0] for call in actor.put.call_args_list] == [changed, completed]
