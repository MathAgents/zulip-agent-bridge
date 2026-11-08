"""One prioritized, rate-limited outbound worker per bot (SPEC §4.10)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zab.core.watchdog import waiting
from zab.zulip.client import GAP, EditRefused, ZulipClient, ZulipError

FINAL, APPROVAL, ACK, STATUS, HOUSEKEEPING = range(5)

#: An edit held back: its content, priority, paused note, failure callback, and whether final
_Held = tuple[str, int, str, Callable[[], None] | None, bool]


@dataclass
class _Job:
    operation: Callable[[], Awaitable[Any]]
    key: int | None = None
    priority: int = STATUS
    sequence: int = 0
    waiters: list[asyncio.Future] = field(default_factory=list)
    retry_server_errors: bool = False
    retries: int = 0
    final: bool = False  # an edit's: a later edit that isn't final never replaces it (#754)


class Poster:
    def __init__(self, client: ZulipClient, per_minute: int = 150) -> None:
        if per_minute <= 0:
            raise ValueError("rate must be positive")
        self.client = client
        self.client.budget.limit = per_minute
        # Housekeeping leaves this much of the window to replies and polls (SPEC §4.10).
        self.reserve = per_minute // 3
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.edits: dict[int, _Job] = {}
        # At most one edit held back per message, until its gap has passed (#754): its content,
        # priority, paused note, failure callback, and whether it is final (a repeat, #806)
        self.held: dict[int, _Held] = {}
        self.timers: dict[int, asyncio.Future] = {}
        self.sequence = itertools.count()
        self.closed = False
        # Jobs enqueued or promoted so far: a deferred housekeeping job yields to them.
        self.arrivals = 0
        self.arrived = asyncio.Event()
        self.worker = asyncio.create_task(self._run())

    async def call(
        self,
        operation: Callable[[], Awaitable[Any]],
        priority: int = ACK,
        *,
        retry_server_errors: bool = False,
    ) -> Any:
        return await self._enqueue(
            _Job(operation, retry_server_errors=retry_server_errors), priority
        )

    async def background(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Housekeeping (SPEC §7.1, §4.10): the lowest priority, and sent only while the
        window keeps `reserve` requests free, so that it never makes a reply wait."""
        return await self.call(operation, HOUSEKEEPING)

    async def send(self, stream_id: int, topic: str, content: str, priority: int = FINAL) -> int:
        return await self.call(lambda: self.client.send(stream_id, topic, content), priority)

    async def edit(
        self,
        message_id: int,
        content: str,
        priority: int = STATUS,
        *,
        final: bool = False,
        paused: str = "",
        failed: Callable[[], None] | None = None,
    ) -> bool:
        """Edit within the message's bounds (`ZulipClient.edit`, #754): True once sent. An edit
        that must wait for its gap is held back, without waiting here, and sent then with the
        newest content held for that message, unless a final edit comes first; one past the
        cap is dropped. A final edit held (a repeat, #806) is never replaced by one that isn't
        (`held` shows it). `paused` is added to the last edit before only a final one is left.
        Nothing waits for a held edit: `failed` is called if it fails (codex round 1)."""
        waiter = self._edit(message_id, content, priority, final, paused, failed)
        if waiter is None:
            return False
        try:
            await waiting(waiter)
        except EditRefused:
            return False
        return True

    def _edit(
        self,
        message_id: int,
        content: str,
        priority: int,
        final: bool,
        paused: str,
        failed: Callable[[], None] | None,
    ) -> asyncio.Future | None:
        """Hold the edit back, drop it, or queue it, merged with one queued for the same
        message: the queued job's waiter, or None."""
        if self.closed:
            raise RuntimeError("poster closed")
        bounds = self.client.edit_bounds
        wait = bounds.wait(message_id, final=final)
        if not final and self.held.get(message_id, (0,) * 5)[4]:
            return None  # a held final edit is never replaced by one that isn't (#806)
        if final and wait is not None and (wait > 0 or message_id in bounds.pending):
            # The one place that keeps what is owed (#806): a repeat held back, or a final edit
            # of a message that owes one, which takes its place, held, queued or sent at once
            bounds.hold_final(message_id, content)
        if wait is None or wait > 0:
            if wait is None:
                self._drop_held(message_id)
                if final:  # past the cap: it can never be sent
                    bounds.release_final(message_id)
            else:
                self._hold_back(message_id, (content, priority, paused, failed, final), wait)
            return None
        self._drop_held(message_id)
        if not final and bounds.last_update(message_id):
            content += paused
        operation = lambda: self.client.edit(message_id, content, final=final)  # noqa: E731
        if message_id in self.edits:
            job = self.edits[message_id]
            if job.final and not final:
                return None
            job.operation, job.final = operation, final
            self._promote(job, priority)
            waiter = asyncio.get_running_loop().create_future()
            job.waiters.append(waiter)
            return waiter
        job = _Job(operation, key=message_id, final=final)
        self.edits[message_id] = job
        return self._submit(job, priority)

    def _hold_back(self, message_id: int, held: _Held, wait: float) -> None:
        """Hold `held` back for `message_id` until `wait` from now. A final one, a repeat, is
        kept in the store until it is sent (`EditBounds.hold_final`, by `_edit`)."""
        self.held[message_id] = held
        if message_id not in self.timers:
            due = self.client.budget._monotonic() + wait
            self.timers[message_id] = asyncio.ensure_future(self._hold(message_id, due))

    async def _hold(self, message_id: int, due: float) -> None:
        """Queue the edit held for `message_id` once its gap has passed, at `due`. Nothing
        waits for it: a failure is logged, as a status refresh's is (the next one follows)."""
        budget = self.client.budget
        await budget.sleep(max(0.0, due - budget._monotonic()))
        del self.timers[message_id]
        held = self.held.pop(message_id)
        if not self.closed:
            self._resubmit(message_id, held)

    def resume(self, message_id: int, content: str) -> None:
        """Send a repeat of a final edit that an earlier run held back and never sent (#806),
        as if held now: at once if its gap has passed, else when it has."""
        self._resubmit(message_id, (content, STATUS, "", None, True))

    def _resubmit(self, message_id: int, held: _Held) -> None:
        content, priority, paused, failed, final = held
        # Held again if an edit went out since (a 429's retry): it keeps its callback (review)
        if waiter := self._edit(message_id, content, priority, final, paused, failed):
            waiter.add_done_callback(functools.partial(self._held_done, message_id, held))

    def _held_done(self, message_id: int, held: _Held, waiter: asyncio.Future) -> None:
        """A held edit's outcome. A final one is released once sent, or once it can never be:
        Zulip refused it (a 4xx, so it never landed), or the cap. After any other failure it
        may have landed, and was counted: what the message owes is tried again a gap later,
        within the cap, unless a newer edit of the message is held or queued, or nothing is
        owed any more. Closing keeps it in the store."""
        if waiter.cancelled():
            return
        exc, final, bounds = waiter.exception(), held[4], self.client.edit_bounds
        if exc is None:
            return  # sent: `ZulipClient.edit` released it, if it is the one held
        if isinstance(exc, EditRefused):
            if final:  # past the cap: it can never be sent
                bounds.release_final(message_id)
            return
        logging.getLogger("zab.zulip").warning("Held edit failed: %s", type(exc).__name__)
        if held[3]:
            held[3]()
        if not final:
            return
        if isinstance(exc, ZulipError) and 400 <= exc.status < 500:
            bounds.release_final(message_id)
        elif (
            not self.closed
            and message_id not in self.held
            and message_id not in self.edits
            and (owed := bounds.pending.get(message_id)) is not None
        ):  # what is owed now: a newer final edit may have taken its place (codex round 3)
            self._hold_back(message_id, (owed, *held[1:]), GAP)

    def _drop_held(self, message_id: int) -> None:
        self.held.pop(message_id, None)
        if timer := self.timers.pop(message_id, None):
            timer.cancel()

    async def _enqueue(self, job: _Job, priority: int) -> Any:
        return await waiting(self._submit(job, priority))

    def _submit(self, job: _Job, priority: int) -> asyncio.Future:
        if self.closed:
            raise RuntimeError("poster closed")
        waiter = asyncio.get_running_loop().create_future()
        job.waiters.append(waiter)
        job.priority, job.sequence = priority, next(self.sequence)
        self.queue.put_nowait((priority, job.sequence, job))
        self._arrive()
        return waiter

    def _promote(self, job: _Job, priority: int) -> None:
        if priority < job.priority:
            job.priority = priority
            self.queue.put_nowait((priority, job.sequence, job))
            self._arrive()

    def _arrive(self) -> None:
        self.arrivals += 1
        self.arrived.set()

    async def _throttle(self) -> None:
        await self.client.budget.wait()

    async def _idle(self, delay: float) -> None:
        """Until a job arrives or `delay` passes on the budget's clock."""
        self.arrived.clear()
        sleeper = asyncio.ensure_future(self.client.budget.sleep(delay))
        arrival = asyncio.ensure_future(self.arrived.wait())
        try:
            await asyncio.wait({sleeper, arrival}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, arrival):
                task.cancel()

    async def _run(self) -> None:
        current: _Job | None = None
        try:
            while True:
                # Rate-limit before choosing a job so new high-priority messages
                # can overtake queued status edits while we wait.
                await self._throttle()
                priority, sequence, current = await self.queue.get()
                seen = self.arrivals
                if priority != current.priority:
                    self.queue.task_done()
                    current = None
                    continue
                await self._throttle()
                if priority != current.priority:
                    self.queue.task_done()
                    current = None
                    continue
                if priority == HOUSEKEEPING:
                    # Checked when it would be sent, after whatever went first (codex review
                    # on #447): housekeeping waits for the window to keep its reserve, and
                    # never in front of another job.
                    delay = self.client.budget.delay(self.reserve)
                    if delay > 0 or self.arrivals != seen:
                        self.queue.task_done()
                        job, current = current, None
                        if self.closed or all(w.done() for w in job.waiters):
                            self._finish(job, error=RuntimeError("poster closed"))
                        else:
                            self.queue.put_nowait((priority, sequence, job))
                            if self.arrivals == seen:
                                await self._idle(delay)
                        continue
                if current.key is not None:
                    self.edits.pop(current.key, None)
                try:
                    result = await current.operation()
                except ZulipError as exc:
                    retry_server = (
                        current.retry_server_errors
                        and 500 <= exc.status < 600
                        and current.retries < 3
                    )
                    if exc.status == 429 or retry_server:
                        delay = max(0.01, exc.retry_after)
                        if retry_server:
                            delay = max(delay, 2**current.retries)
                            current.retries += 1
                        self.client.budget.blocked_until = max(
                            self.client.budget.blocked_until,
                            self.client.budget.wall() + delay,
                            self.client.reset_at,
                        )
                        if current.key is not None and current.key in self.edits:
                            queued = self.edits[current.key]
                            queued.waiters.extend(current.waiters)
                            self._promote(queued, priority)
                        else:
                            if current.key is not None:
                                self.edits[current.key] = current
                            self.queue.put_nowait((priority, sequence, current))
                    else:
                        self._finish(current, error=exc)
                except Exception as exc:
                    self._finish(current, error=exc)
                else:
                    self._finish(current, result=result)
                finally:
                    self.queue.task_done()
                current = None
        finally:
            if current:
                self._finish(current, error=RuntimeError("poster stopped"))
            while not self.queue.empty():
                _, _, job = self.queue.get_nowait()
                self._finish(job, error=RuntimeError("poster stopped"))
                self.queue.task_done()

    @staticmethod
    def _finish(job: _Job, result: Any = None, error: Exception | None = None) -> None:
        for waiter in job.waiters:
            if not waiter.done():
                if error:
                    waiter.set_exception(error)
                else:
                    waiter.set_result(result)

    async def close(self, *, drain: bool = True) -> None:
        self.closed = True
        for message_id in list(self.timers):
            # a refresh: the next run makes its own; a final one stays in the store (#806)
            self._drop_held(message_id)
        self.arrived.set()  # a deferred housekeeping job is dropped, not waited for
        if drain:
            await self.queue.join()
        self.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.worker
