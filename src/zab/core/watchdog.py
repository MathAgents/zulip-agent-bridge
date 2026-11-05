"""Listener liveness and progress and standard-library systemd notifications (SPEC §13.3)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from asyncio import sleep, wait
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
from typing import TypeVar

WAIT_INTERVAL = 30
T = TypeVar("T")
CURRENT: ContextVar[Heartbeat | None] = ContextVar("listener_heartbeat", default=None)


class Heartbeat:
    def __init__(self, state_dir: str | Path, bot: str) -> None:
        self.bot = bot
        self.path = Path(state_dir) / f"heartbeat-{bot}"
        self.task: asyncio.Task | None = None
        self.last_seen: float | None = None  # Never trust a file left by the previous process.

    def bind(self) -> None:
        self.task = asyncio.current_task()
        CURRENT.set(self)
        self.pulse()

    def pulse(self) -> None:
        self.last_seen = monotonic()

    async def advance(self) -> None:
        # The sentinel still reads event progress, independently of liveness.
        await asyncio.to_thread(self.path.touch)


async def waiting(operation: Awaitable[T]) -> T:
    """Known queue/rate-limit waits are healthy; arbitrary handler awaits are not.

    Only the listener task can renew its lease. Child tasks inherit context but
    cannot mask a hung listener. Cancellation propagates to the awaited operation.
    """
    heartbeat = CURRENT.get()
    if heartbeat is None or asyncio.current_task() is not heartbeat.task:
        return await operation
    future = asyncio.ensure_future(operation)
    try:
        while not future.done():
            heartbeat.pulse()
            await wait({future}, timeout=WAIT_INTERVAL)
        return future.result()
    finally:
        if not future.done():
            future.cancel()
            await asyncio.gather(future, return_exceptions=True)


@asynccontextmanager
async def waiting_lock(lock: asyncio.Lock) -> AsyncIterator[None]:
    acquired = asyncio.create_task(lock.acquire())
    try:
        await waiting(acquired)
        yield
    finally:
        # Cancellation can arrive just as acquire succeeds; release our acquisition
        # in that case too, without releasing a different task's lock.
        if acquired.done() and not acquired.cancelled() and acquired.exception() is None:
            if acquired.result():
                lock.release()


def notify(text: str) -> None:
    """Send from the main process; support filesystem and Linux abstract sockets."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.sendto(text.encode(), address)
    except OSError as exc:
        logging.getLogger("zab.watchdog").warning("sd_notify failed (%s)", type(exc).__name__)


def interval() -> float:
    """Honor systemd's watchdog environment only for its designated process."""
    try:
        pid = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
        return (
            max(0, int(os.environ.get("WATCHDOG_USEC", "0")) / 1_000_000)
            if pid == os.getpid()
            else 0
        )
    except ValueError:
        return 0


def stale(heartbeats: list[Heartbeat], period: float, now: float) -> list[Heartbeat]:
    return [h for h in heartbeats if h.last_seen is None or now - h.last_seen > period / 2]


def fresh(heartbeats: list[Heartbeat], period: float) -> bool:
    return bool(heartbeats) and not stale(heartbeats, period, monotonic())


async def watch(heartbeats: list[Heartbeat], period: float) -> None:
    reported: set[Heartbeat] = set()
    while True:
        now = monotonic()
        expired = set(stale(heartbeats, period, now))
        if heartbeats and not expired:
            notify("WATCHDOG=1")
        for heartbeat in heartbeats:
            if heartbeat not in expired or heartbeat in reported:
                continue
            age = (
                f"{now - heartbeat.last_seen:.1f}s" if heartbeat.last_seen is not None else "never"
            )
            # File/function/line only: never inspect locals or render source text,
            # which could include message content or login input.
            stack = (
                [
                    f"{f.f_code.co_filename}:{f.f_lineno} in {f.f_code.co_name}"
                    for f in heartbeat.task.get_stack()
                ]
                if heartbeat.task
                else []
            )
            logging.getLogger("zab.watchdog").error(
                "Watchdog listener stalled: bot=%s last_seen_age=%s stack=%s",
                heartbeat.bot,
                age,
                " <- ".join(stack) or "unavailable (task absent or finished)",
                extra={"bot": heartbeat.bot, "event": "watchdog_stale"},
            )
        reported = expired  # log once per stale episode, again after recovery
        await sleep(period / 4)
