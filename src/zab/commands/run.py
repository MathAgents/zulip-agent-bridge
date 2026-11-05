"""Foreground bridge and graceful shutdown (SPEC §7.1, §13.1)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from zab.config import Config, load_config
from zab.core import detached, lean_reaper, watchdog
from zab.core import logging as log_setup
from zab.core import types as t
from zab.core.backends import make_backend
from zab.core.bridge import BotBridge
from zab.core.links import WorkspaceLinks
from zab.core.session import Reopen, SessionActor
from zab.core.store import BotLock, Store
from zab.render.markdown import MarkdownRenderer
from zab.zulip.client import error_kind


class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config.bridge.state_dir)
        self.locks: list[BotLock] = []
        self.backends: dict[str, t.Backend] = {}
        self.bridges: list[BotBridge] = []
        self.pumps: list[asyncio.Task] = []
        self.recoveries: dict[int, asyncio.Task] = {}
        self.watchdog: asyncio.Task | None = None
        self.lean_reaper: asyncio.Task | None = None

    async def start(self) -> None:
        from zab.workspace.manager import LocalWorkspaceManager

        for bot in self.config.bots:
            self.locks.append(BotLock(self.config.bridge.state_dir, bot.name))
        for name in {bot.backend for bot in self.config.bots}:
            config = self.config.backends[name]
            backend = make_backend(name, config)
            self.backends[name] = backend
            await backend.start()
        slots = {
            name: asyncio.Semaphore(self.config.bridge.max_concurrent_turns)
            for name in self.backends
        }
        for bot in self.config.bots:
            manager = LocalWorkspaceManager(
                max_upload_bytes=self.config.render.max_upload_mb * 1024**2,
                auto_upload_ext=frozenset(self.config.render.auto_upload_ext),
                state_dir=self.config.bridge.state_dir,  # persists the workspace pins (§10.6)
                post_create=self.config.bridge.worktree_post_create,  # issue #205
            )
            bridge = BotBridge(
                self.config,
                bot,
                self.store,
                self.backends[bot.backend],
                manager,
                MarkdownRenderer(
                    WorkspaceLinks(manager),
                    diff_inline_max_lines=self.config.render.diff_inline_max_lines,
                ),
                slots[bot.backend],
            )
            self.bridges.append(bridge)
            bridge.all_bridges = self.bridges  # Shared live list across every configured backend.
            peers = [peer for peer in self.bridges if peer.bot.backend == bot.backend]
            for peer in peers:
                peer.peers = peers
        for bridge in self.bridges:
            await bridge.initialize()
            bridge.manager.max_upload_bytes = min(
                bridge.manager.max_upload_bytes,
                bridge.client.limits["max_file_upload_size_mib"] * 1024**2,
            )
        for bridge in self.bridges:
            await bridge.start()
        for name, backend in self.backends.items():
            self.pumps.append(asyncio.create_task(self.pump(name, backend)))

        watchdog.notify("READY=1")
        if timeout := watchdog.interval():
            self.watchdog = asyncio.create_task(
                watchdog.watch([b.heartbeat for b in self.bridges], timeout)
            )
        # Codex threads' idle Lean servers (SPEC §7.12, #249): one reaper for the machine
        if reaper := lean_reaper.for_config(self.config):
            self.lean_reaper = asyncio.create_task(reaper.run())

    async def pump(self, name: str, backend: t.Backend) -> None:
        async for event in backend.events():
            # Each actor takes its own snapshot in its loop (§7.8.1): one taken here could
            # predate a turn the actor starts before it reaches the snapshot. One request for
            # every bot of this backend, so that their backend calls take turns.
            request = Reopen() if isinstance(event, t.BackendStatus) else None
            for bridge in self.bridges:
                if bridge.bot.backend != name:
                    continue
                if isinstance(event, t.BackendStatus):
                    bridge.client.metrics.connection(event.up)
                    if previous := self.recoveries.pop(id(bridge), None):
                        previous.cancel()
                    recovered = []
                    for actor in list(bridge.actors.values()):
                        if actor.row["state"] in ("readonly", "detached"):
                            continue
                        if event.up:
                            actor.put(request)
                            recovered.append(actor)
                        else:
                            actor.put(("!backend-down",))
                    if event.up:
                        self.recoveries[id(bridge)] = asyncio.create_task(
                            self.finish_recovery(bridge, recovered)
                        )
                elif event.session_id in bridge.attaching:
                    bridge.attaching[event.session_id].append(event)
                elif isinstance(event, t.TurnCompleted):
                    detached.completed(bridge, event)
                    if actor := bridge.actors.get(event.session_id):
                        actor.put(event)
                elif actor := bridge.actors.get(event.session_id):
                    if isinstance(event, t.SessionChanged) and event.new_session_id:
                        bridge.actors[event.new_session_id] = bridge.actors.pop(event.session_id)
                    if actor.row["state"] != "detached" or isinstance(
                        event, (t.TurnCompleted, t.ApprovalResolved)
                    ):
                        actor.put(event)

    async def finish_recovery(self, bridge: BotBridge, actors: list[SessionActor]) -> None:
        for actor in actors:
            await actor.inbox.join()
        await detached.recover(bridge)
        await bridge.retry_renames()
        await bridge.recover_pending_creations()
        await bridge.run_pending_forks()  # `!fork`s that waited for this reopen (#202)
        delay = 1
        while True:
            try:
                await bridge.reconcile_reactions()
                return
            except Exception:
                logging.getLogger("zab.runtime").exception("Reaction recovery failed; retrying")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def close(self) -> None:
        tasks = [*self.pumps, *self.recoveries.values()]
        if self.watchdog:
            tasks.append(self.watchdog)
        if self.lean_reaper:
            tasks.append(self.lean_reaper)
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for bridge in self.bridges:
            await bridge.close()
        for backend in self.backends.values():
            await backend.close()
        for lock in self.locks:
            lock.close()
        self.store.close()


async def serve(config: Config) -> None:
    runtime = Runtime(config)
    handlers = await asyncio.to_thread(log_setup.configure, config.bridge.state_dir)
    logger = logging.getLogger("zab.runtime")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stop_task = asyncio.create_task(stop.wait())
    try:
        await runtime.start()
        print("zab ready", file=sys.stderr, flush=True)
        logger.info("Bridge started with %d bots", len(runtime.bridges))
        monitored = [
            stop_task,
            *runtime.pumps,
            *(b.listener for b in runtime.bridges if b.listener),
        ]
        done, _ = await asyncio.wait(monitored, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop_task.cancel()
        await runtime.close()
        logger.info("Bridge stopped")
        log_setup.close(handlers)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="zab run")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(load_config(args.config)))
    except Exception as exc:
        print(f"zab run failed ({error_kind(exc)})")
        return 1
    return 0
