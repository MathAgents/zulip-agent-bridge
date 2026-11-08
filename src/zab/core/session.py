"""Serialized per-session turn lifecycle (SPEC §7)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from zab.core import presentation as view
from zab.core import types as t
from zab.core.attachments import load_attachments
from zab.core.commands import (
    DETACH,
    NO_TURN,
    NO_TURN_NOR_SUBAGENT,
    SUBAGENTS_UNCHECKED,
    TURN_ELSEWHERE,
    UNKNOWN,
    login_required,
    run_command,
    status_login_hint,
    stopped_subagents,
)
from zab.core.peer_context import consume_fork_answers, fork_context, peer_context
from zab.core.reason import verified_only
from zab.core.settings import display_setting, effective_settings
from zab.core.store import Store
from zab.render.redact import redact_secrets
from zab.zulip.client import EditRefused, ZulipError
from zab.zulip.poster import ACK, APPROVAL, FINAL, Poster

# The notice under messages held before a turn can start (§7.3).
WAITING = "⏳ Waiting for a free slot or backend."
#: Marked as the bridge's (`SessionActor.note`, #677).
NEEDS_RECOVERY = "This session needs recovery; use !status to retry."
#: Under a status message's last edit before its summary (`ZulipClient.edit`, #754).
PAUSED = (
    "\n\n⏸ *Updates to this message are paused (its edit limit). `!status` shows the turn's "
    "progress; the summary comes here when the turn ends.*"
)
#: A running turn's periodic refresh (SPEC §4.9, #754): from `FIRST_REFRESH` after it starts,
#: one edit gap (`zab.zulip.client.GAP`), it looks every `LOOK` seconds, and the first look that
#: finds its actions, plan or notes changed edits it once, the early view: most turns end within
#: `edit_interval`, and one early view of what the agent does costs one edit. After it, a
#: refresh at most every `edit_interval`, at the first look that finds a change.
FIRST_REFRESH, LOOK = 60.0, 30.0


@dataclass
class Delivery:
    """`quiet`: a command that reached this bot without naming it, as it reached the VM's other
    bots (SPEC §4.5 rule 6.2, `Route.quiet`): it says nothing where it doesn't act (§4.6).
    `first`: of those with a session in the topic, this bot comes first by full name, and so
    answers for them where none acts (`BotBridge.first_here`, #847); true unless `quiet`."""

    message_id: int
    sender_id: int
    sender: str
    text: str
    quiet: bool = False
    first: bool = True


@dataclass(frozen=True)
class StopTimeout:
    turn_id: str


@dataclass(eq=False)
class Reopen:
    """Reopen the session from inside its actor: the backend snapshot and recovery are one
    step of the serialized loop, so no turn can start between them (SPEC §7.8.1). The pump
    shares one request per reconnect among the actors of every bot of the backend. Their
    backend calls take turns, as in the pump's own loop before, and the first
    `BackendUnavailable` stops the reopening: the rest are marked backend-down as well.
    Any other failure, such as `SessionRefused`, puts only its own session in `error`."""

    stopped: bool = False
    turns: asyncio.Lock = field(default_factory=asyncio.Lock)
    info: t.SessionInfo | None = None  # the last snapshot taken; `!attach` reads its own


class ReadOnlySession(Exception):
    """A card moved to a channel where the bridge must not post."""


class SessionActor:
    def __init__(
        self,
        row: dict[str, Any],
        store: Store,
        backend: t.Backend,
        poster: Poster,
        renderer: t.Renderer,
        manager: t.WorkspaceManager,
        context: t.RenderContext,
        slots: asyncio.Semaphore,
        *,
        machine: str,
        resume: str,
        edit_interval: float = 300,
        refserve_url: str = "",
        refserve_owner: str = "MathAgents",
        mention_after: float = 120,
        allowed_senders: frozenset[int] = frozenset(),
        multi_party_enabled: bool = False,
        topic_multi_party: Callable[[int], Awaitable[bool]] | None = None,
        bridge_bot_ids: Callable[[], set[int]] | None = None,
        bot_user_id: int | None = None,
        git_identity: t.GitIdentity | None = None,
        confirm_location: Callable[[str], Awaitable[None]] | None = None,
        reopened: Callable[[], None] | None = None,
        unmentioned_here: Callable[[int, str], Awaitable[str]] | None = None,
    ) -> None:
        self.row, self.store, self.backend, self.poster = row, store, backend, poster
        self.unmentioned_here = unmentioned_here  # BotBridge.unmentioned_here, for !session
        self.confirm_location = confirm_location  # BotBridge.verify_card
        self.reopened = reopened  # BotBridge.pending_forks_soon: forks waiting for it (#202)
        self.renderer, self.manager, self.context, self.slots = renderer, manager, context, slots
        self.machine, self.resume = machine, resume
        self.allowed_senders = allowed_senders
        self.multi_party_enabled = multi_party_enabled
        self.topic_multi_party = topic_multi_party
        self.bridge_bot_ids = bridge_bot_ids
        self.bot_user_id = bot_user_id
        self.git_identity = git_identity
        self.peer_boundary: int | None = None
        self.fork_answers: list[str] = []  # the forks whose answers this input carries (#202)
        self.edit_interval, self.mention_after = edit_interval, mention_after
        # The running turn's id, and when its next periodic refresh may come, once its early
        # view was shown (`tick`, #754)
        self.viewed: tuple[Any, float] | None = None
        # The running turn's id, and when its summary was last tried, or a look last tried its
        # status unasked (`tick`)
        self.retried: tuple[Any, float] | None = None
        # rc6: the central file server and the GitHub owner it serves; "" = off (SPEC §10.7)
        self.refserve_url, self.refserve_owner = refserve_url, refserve_owner
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.actions: dict[str, t.ItemInfo] = {}
        self.plan: tuple = ()
        self.notes: dict[str, str] = {}  # the agent's interim texts by item id, oldest first
        # The client_id of each steer of the running turn not yet taken in -> its message (#654)
        self.steered: dict[str, int] = {}
        self.turn: dict[str, Any] | None = next(
            iter(
                store.rows("turns", session_id=row["id"], state="delivery_pending")
                + store.rows("turns", session_id=row["id"], state="running")
            ),
            None,
        )
        self.pending: dict[str, t.ApprovalRequested] = {}
        self.slot_owned = False
        self.unconfirmed_turn: dict[str, Any] | None = None
        self.slot_reservation: asyncio.Task | None = None
        self.waiter: asyncio.Task | None = None
        self.stop_timer: asyncio.Task | None = None
        self.last_status = ""
        payload = store.get(f"completion:{self.turn['id']}", None) if self.turn else None
        self.completion: t.TurnCompleted | None = None
        if payload:
            payload["status"] = t.TurnStatus(payload["status"])
            if payload.get("error"):
                payload["error"]["kind"] = t.ErrorKind(payload["error"]["kind"])
                payload["error"] = t.ErrorInfo(**payload["error"])
            if payload.get("usage"):
                payload["usage"] = t.TokenUsage(**payload["usage"])
            self.completion = t.TurnCompleted(**payload)
        self.metric_completion_id = self.turn["id"] if self.turn and self.completion else None
        self.ready = True
        self.waiting_message_id = self.store.get(f"waiting:{row['id']}")
        self.runner = asyncio.create_task(self._run())
        self.timer = asyncio.create_task(self._ticks())

    def put(self, event: Any) -> None:
        self.inbox.put_nowait(event)

    def state(self, **values: Any) -> None:
        if self.row["state"] in ("readonly", "detached"):
            values["state"] = self.row["state"]
        # `updated_at` is the session's last activity (§2 "active session"): a write that
        # changes nothing, as every reopen at startup made, must not make it recent.
        if any(self.row.get(name) != value for name, value in values.items()):
            values["updated_at"] = time.time()
        if not values:
            return
        self.row.update(values)
        self.store.update("sessions", {"id": self.row["id"]}, **values)

    async def post(self, content: str, priority: int = FINAL) -> int:
        if self.confirm_location:
            # Never post at a card location this process has not confirmed (SPEC §4.4):
            # a turn the backend reports for a session nothing has reached since start-up.
            await self.confirm_location(self.row["id"])
        if self.row["state"] in ("readonly", "detached"):
            raise ReadOnlySession
        return await self.poster.send(self.row["stream_id"], self.row["topic"], content, priority)

    async def _run(self) -> None:
        while True:
            event = await self.inbox.get()
            try:
                if isinstance(event, Delivery):
                    await self.deliver(event)
                elif isinstance(event, Reopen):
                    await self.reopen(event)
                elif isinstance(event, t.SessionInfo):
                    from zab.core.recovery import recover

                    await recover(self, event)
                elif isinstance(event, t.BackendEvent):
                    await self.event(event)
                elif event == "begin":
                    await self.begin()
                elif event == "tick":
                    await self.tick()
                elif event == "redeliver":
                    # Zulip answers again (BotBridge.listen): the automatic `!status` retry.
                    # A turn whose stop was never confirmed keeps waiting for `!status`.
                    if self.completion and not self.unconfirmed_turn:
                        await self.complete(self.completion)
                elif (
                    isinstance(event, StopTimeout)
                    and self.turn
                    and event.turn_id == self.turn["native_turn_id"]
                ):
                    self.unconfirmed_turn = self.turn
                    await self.complete(
                        t.TurnCompleted(
                            session_id=self.row["native_id"],
                            turn_id=self.turn["native_turn_id"],
                            status=t.TurnStatus.INTERRUPTED,
                            final_text=self.note("No stop confirmation after 30 seconds."),
                        ),
                        drain=False,
                        release_slot=False,
                    )
                    self.state(state="error")
                elif isinstance(event, tuple):
                    await self.command(*event)
            except ReadOnlySession:
                if self.completion:
                    await self.complete(self.completion)
            except Exception as exc:
                # A failed operation cannot kill the actor or expose request contents.
                self.log_error("Session operation", exc)
                if self.row["state"] not in ("readonly", "detached"):
                    self.state(state="error")
                with contextlib.suppress(Exception):
                    await self.post(self.note(f"{type(exc).__name__}; use !status to retry."))
            finally:
                self.inbox.task_done()

    async def refresh(self) -> None:
        try:
            await self.status()
        except ReadOnlySession:
            raise
        except Exception as exc:
            # A status refresh is cosmetic, and the next tick retries it: Zulip
            # being unreachable is no reason to put the session in error.
            self.log_error("Status refresh", exc)

    def view_changed(self) -> bool:
        """Whether the actions, plan or notes differ from those last shown: the status below
        its first line, whose model, effort and state have edits of their own (#754)."""
        return self.status_text().partition("\n")[2] != self.last_status.partition("\n")[2]

    async def tick(self) -> None:
        """A look at the running turn (`_ticks`; SPEC §4.9, #754): before its early view, a
        refresh at the first look that finds its actions, plan or notes changed; after it, at
        the first such look at least `edit_interval` after the last. A status not yet shown
        (`last_status` empty) or a summary still to deliver is tried again at a look, but at
        most once an edit gap: each try that may have landed counts towards the message's cap,
        and an outage would otherwise spend it at one try a look (review round 2 of #755)."""
        if not self.turn:
            return
        now = self.poster.client.budget.wall()
        key = self.turn["id"]
        viewed = self.viewed if self.viewed and self.viewed[0] == key else None
        changed = self.view_changed()
        if self.last_status and not self.completion:
            if not changed or (viewed and now < viewed[1] - LOOK / 2):
                return
        else:
            retried = self.retried if self.retried and self.retried[0] == key else None
            if retried and now < retried[1] + FIRST_REFRESH:
                return
            self.retried = (key, now)
        await self.refresh()
        if changed:
            self.viewed = (self.turn["id"], now + self.edit_interval)

    def next_refresh(self) -> float | None:
        """When the running turn's next look is due, on the poster's clock (a test's, if set):
        `FIRST_REFRESH` after it started, then every `LOOK` seconds."""
        if not self.turn:
            return None
        first = self.turn["started_at"] + FIRST_REFRESH
        late = self.poster.client.budget.wall() - first
        return first if late < 0 else first + (late // LOOK + 1) * LOOK

    async def _ticks(self) -> None:
        budget, fired = self.poster.client.budget, None
        while True:
            turn, due = self.turn, self.next_refresh()
            if due is None or due == fired or due - budget.wall() > LOOK:
                await budget.sleep(LOOK)
                continue
            await budget.sleep(max(0.0, due - budget.wall()))
            if self.turn is turn:
                self.put("tick")
            fired = due

    async def react(self, message_id: int, name: str) -> None:
        try:
            await self.poster.call(
                lambda: self.poster.client.request(
                    "POST",
                    f"messages/{message_id}/reactions",
                    emoji_name=getattr(self.poster, "emoji_names", {}).get(name, name),
                ),
                ACK,
            )
        except ZulipError as exc:
            # A restart replays held messages that may already carry this reaction.
            if exc.code != "REACTION_ALREADY_EXISTS":
                raise
        else:
            if name == "eyes":
                self.poster.client.metrics.acknowledged(message_id)

    async def mark_read(self, message_id: int) -> None:
        """✅ on a steered message once the agent has taken it in (#654). Cosmetic, like a
        status refresh: a failure is logged and never puts the session in error."""
        try:
            await self.react(message_id, "check")
        except Exception as exc:
            self.log_error("Read mark", exc)

    def new_turn_view(self) -> None:
        """What the status message shows starts afresh with each turn."""
        self.actions.clear()
        self.plan = ()
        self.notes.clear()
        self.steered.clear()

    async def deliver(self, delivery: Delivery) -> None:
        if self.row["state"] in ("detached", "readonly"):
            return
        with self.store.transaction():
            receipt = self.store.rows(
                "inbound", bot=self.row["bot"], message_id=delivery.message_id
            )
            if receipt and receipt[0]["disposition"] == "delivered":
                return  # A restored and an already queued copy may meet after recovery.
            self.store.claim(self.row["bot"], delivery.message_id, self.row["id"])
            self.store.hold(self.row["id"], delivery.message_id, delivery.sender, delivery.text)
            self.state(last_sender_id=delivery.sender_id)
            self.store.set(f"sender:{self.row['id']}", delivery.sender)
            self.store.update(
                "inbound",
                {"bot": self.row["bot"], "message_id": delivery.message_id},
                disposition="delivered",
                session_id=self.row["id"],
            )
            self.store.delete(f"pending_delivery:{self.row['bot']}:{delivery.message_id}")
        if (
            self.ready
            and self.turn
            and self.row["state"] == "running"
            and self.backend.capabilities.steer
        ):
            try:
                steer = await self.prepare_input(
                    delivery.text, delivery.sender, [delivery.message_id]
                )
                await self.backend.steer(self.row["native_id"], self.turn["native_turn_id"], steer)
                self.accept_input([delivery.message_id])
                # ✅ once the agent takes it in: its ItemCompleted(USER_MESSAGE) (#654)
                self.steered[steer.client_id] = delivery.message_id
                await self.react(delivery.message_id, "arrow_right_hook")
                return
            except t.SteerRejected:
                pass
            except t.BackendUnavailable:
                self.ready = False
                raise
        error = self.row["state"] == "error"  # held until `!status` recovers it (§7.3)
        if self.turn and not error:
            await self.react(delivery.message_id, "hourglass")
        else:
            # in error with its turn's row still running too, as a reopen refused while the
            # turn ran (review of #854, B1): the note says what unblocks it
            if error or self.slots.locked() or not self.ready:
                await self.react(delivery.message_id, "hourglass")
                if not self.waiting_message_id:
                    self.waiting_message_id = await self.post(
                        self.note(NEEDS_RECOVERY) if error else WAITING, ACK
                    )
                    self.store.set(f"waiting:{self.row['id']}", self.waiting_message_id)
            self.schedule()

    def user_input(self, text: str, sender: str) -> t.UserInput:
        client_id = str(uuid4())
        key = f"client_ids:{self.row['id']}"
        self.store.set(key, [*self.store.get(key, []), client_id][-10000:])
        return t.UserInput(text=text, client_id=client_id, sender=sender)

    def accept_input(self, message_ids: list[int]) -> None:
        with self.store.transaction():
            self.store.clear_held(self.row["id"], message_ids)
            if self.peer_boundary is not None:
                key = f"peer_cursor:{self.row['id']}"
                cursor = max(self.store.get(key, 0), self.peer_boundary)
                self.store.set(key, cursor)
                self.store.prune_audience(
                    self.row["bot"], self.row["stream_id"], self.row["topic"], cursor
                )
            if self.fork_answers:
                consume_fork_answers(self.store, self.row["id"], self.fork_answers)
                self.fork_answers = []

    async def prepare_input(self, text: str, sender: str, message_ids: list[int]) -> t.UserInput:
        peers = sorted(
            {
                name
                for mid in message_ids
                for name in self.store.get(f"addressed_peers:{self.row['bot']}:{mid}", [])
            }
        )
        if peers:
            text = (
                "[Addressed to you and to "
                + ", ".join(peers)
                + " (other AI agents in this topic); answer it yourself.]\n\n"
                + text
            )
        files = await load_attachments(self, message_ids)
        if files:
            text += "\n\nAttachments saved locally:\n" + "\n".join(a.path for a in files)
        user_input = self.user_input(text, sender)
        multi_party = bool(
            self.topic_multi_party and await self.topic_multi_party(self.row["stream_id"])
        )
        preamble, self.peer_boundary = await peer_context(
            self,
            message_ids,
            multi_party=multi_party,
            sender=sender,
            context_id=user_input.client_id,
        )
        forks, self.fork_answers = fork_context(self)
        return replace(user_input, text=preamble + forks + text, attachments=files)

    def schedule(self) -> None:
        if (
            not self.ready
            or self.row["state"] == "error"
            or self.waiter
            or self.turn
            or not self.store.held(self.row["id"])
        ):
            return

        async def wait() -> None:
            await self.slots.acquire()
            self.slot_owned = True
            self.put("begin")

        self.waiter = asyncio.create_task(wait())

    async def begin(self) -> None:
        self.waiter = None
        if self.turn:
            self.release()
            return
        held = self.store.held(self.row["id"])
        if not self.ready or not held or self.row["state"] in ("readonly", "detached", "error"):
            self.release()
            return
        text = (
            held[0]["content"]
            if len(held) == 1
            else (
                f"[{len(held)} messages arrived while you were working; "
                "the newest takes precedence]\n"
                + "\n".join(
                    f"{time.strftime('%H:%M', time.localtime(m['received_at']))} "
                    f"{m['sender']}: {m['content']}"
                    for m in held
                )
                + "\n[end of held messages]"
            )
        )
        try:
            snapshot_started = time.time()
            snapshot = await self.manager.snapshot(self.context.workspace)
            tid = await self.backend.start_turn(
                self.row["native_id"],
                await self.prepare_input(text, held[-1]["sender"], [m["message_id"] for m in held]),
            )
            self.store.insert(
                "turns",
                session_id=self.row["id"],
                native_turn_id=tid,
                trigger_msg_ids=json.dumps([m["message_id"] for m in held]),
                state="running",
                status_message_id=None,
                start_snapshot=json.dumps(asdict(snapshot)) if snapshot else None,
                sender_id=self.row.get("last_sender_id"),
                started_at=snapshot_started,
            )
            self.turn = self.store.rows("turns", session_id=self.row["id"], native_turn_id=tid)[0]
            self.poster.client.metrics.counts["turns_started"] += 1
            self.accept_input([m["message_id"] for m in held])
            self.new_turn_view()
            self.state(state="running")
            mid, content = self.waiting_message_id, self.status_text()
            if mid:
                await self.poster.edit(mid, content, ACK)
                self.waiting_message_id = None
                self.store.set(f"waiting:{self.row['id']}", None)
            else:
                mid = await self.post(content, ACK)
            self.last_status = content  # a refresh with nothing new edits nothing (#754)
            self.turn["status_message_id"] = mid
            self.store.update("turns", {"id": self.turn["id"]}, status_message_id=mid)
            for msg in held:
                await self.react(msg["message_id"], "eyes")
            await self.card()
        except BaseException:
            if not self.turn:
                self.release()
            raise

    async def reserve_slot(self) -> None:
        if self.slot_owned or self.slot_reservation:
            return

        async def acquire() -> None:
            await self.slots.acquire()
            self.slot_owned = True

        if not self.slots.locked():
            await acquire()
        else:
            # Do not block the actor from receiving the completion that releases it.
            self.slot_reservation = asyncio.create_task(acquire())

    def release(self) -> None:
        if self.slot_reservation:
            self.slot_reservation.cancel()
            self.slot_reservation = None
        if self.slot_owned:
            self.slot_owned = False
            self.slots.release()

    async def event(self, event: t.BackendEvent) -> None:
        if isinstance(event, t.SessionChanged):
            if event.new_session_id:
                previous = shlex.quote(self.row["native_id"])
                if self.resume.endswith(previous):
                    self.resume = self.resume[: -len(previous)] + shlex.quote(event.new_session_id)
                self.state(native_id=event.new_session_id)
            if event.settings:
                self.state(settings_json=json.dumps(asdict(event.settings)))
            await self.card()
            return
        if (
            isinstance(event, t.TurnStarted)
            and self.turn
            and event.turn_id == self.turn["native_turn_id"]
        ):
            if event.pgid is not None:
                self.turn["pgid"] = event.pgid
                self.store.update("turns", {"id": self.turn["id"]}, pgid=event.pgid)
        if self.row["state"] in ("readonly", "detached"):
            if isinstance(event, t.TurnCompleted):
                await self.complete(event)
            elif isinstance(event, t.ApprovalResolved):
                self.pending.pop(event.request_id, None)
                self.store.update(
                    "approvals",
                    {"request_id": event.request_id, "session_id": self.row["id"]},
                    state="resolved_elsewhere",
                )
            return
        turn_id = getattr(event, "turn_id", None)
        if (
            not self.turn
            and turn_id
            and not isinstance(event, t.TurnCompleted)  # an unconfirmed stop still settles
            and self.store.rows("turns", session_id=self.row["id"], native_turn_id=turn_id)
        ):
            # A late event of a settled turn. The runtime's pump reopens sessions first when
            # the backend comes up, so the summary can complete a turn whose own queued
            # events follow (§7.8.1; e2e scenario 2). It is no new terminal turn.
            return
        if (
            self.turn
            and getattr(event, "turn_id", self.turn["native_turn_id"])
            != self.turn["native_turn_id"]
        ):
            return
        if isinstance(event, t.TurnStarted) and not self.turn:
            snapshot = await self.manager.snapshot(self.context.workspace)
            self.new_turn_view()
            self.last_status = ""
            mid = await self.post("⏳ **Working** · via terminal", ACK)
            self.store.insert(
                "turns",
                session_id=self.row["id"],
                native_turn_id=event.turn_id,
                pgid=event.pgid,
                trigger_msg_ids="[]",
                state="running",
                status_message_id=mid,
                start_snapshot=json.dumps(asdict(snapshot)) if snapshot else None,
                started_at=time.time(),
            )
            self.turn = self.store.rows(
                "turns", session_id=self.row["id"], native_turn_id=event.turn_id
            )[0]
            self.poster.client.metrics.counts["turns_started"] += 1
            self.state(state="running")
        elif isinstance(event, (t.ItemStarted, t.ItemCompleted)):
            item = event.item
            if item.kind == t.ItemKind.USER_MESSAGE and isinstance(event, t.ItemCompleted):
                if item.client_id is None:
                    for part in await self.renderer.render_agent_text(
                        item.text or "", self.context
                    ):
                        await self.post("💻 *via terminal:*\n" + part)
                elif (mid := self.steered.pop(item.client_id, None)) is not None:
                    await self.mark_read(mid)
            elif item.kind == t.ItemKind.USER_MESSAGE:
                return
            elif item.kind == t.ItemKind.AGENT_MESSAGE:
                # Codex starts each message empty: only a finished, non-empty one is a note.
                if isinstance(event, t.ItemCompleted) and (item.text or "").strip():
                    self.notes.pop(item.item_id, None)
                    self.notes[item.item_id] = item.text or ""
                    for old in list(self.notes)[: -view.NOTES]:
                        del self.notes[old]
            else:
                self.actions[item.item_id] = item
        elif isinstance(event, t.PlanUpdated):
            self.plan = event.steps
        elif isinstance(event, t.ApprovalRequested):
            self.pending[event.request_id] = event
            summary = redact_secrets(event.summary)[:500]  # redacted before the cut (§11.6)
            prefix = self.mention() + " approval needed: " + view.code(summary)
            controls = "\n" + " · ".join(
                label
                for decision, label in (
                    (t.ApprovalDecision.ACCEPT, "👍 approve"),
                    (t.ApprovalDecision.ACCEPT_FOR_SESSION, "🔁 session"),
                    (t.ApprovalDecision.DECLINE, "👎 deny"),
                )
                if decision in event.decisions
            )
            detail = redact_secrets(event.detail)  # before any cut (§11.6)
            while (
                len(prefix + "\n" + view.block(detail) + controls)
                > self.context.max_message_length - 200
            ):
                detail = detail[: len(detail) // 2]
            mid = await self.post(prefix + "\n" + view.block(detail) + controls, APPROVAL)
            values = dict(
                session_id=self.row["id"],
                message_id=mid,
                kind=event.kind,
                state="pending",
                created_at=time.time(),
            )
            if self.store.rows("approvals", request_id=event.request_id):
                self.store.update("approvals", {"request_id": event.request_id}, **values)
            else:
                self.store.insert("approvals", request_id=event.request_id, **values)
            self.state(state="awaiting_approval")
            await self.refresh()
        elif isinstance(event, t.ApprovalResolved):
            await self.resolve(event.request_id, "resolved_elsewhere")
        elif isinstance(event, t.UsageUpdated):
            if self.backend.kind != "claude" and event.usage.total_tokens is not None:
                self.store.set(f"session_tokens:{self.row['id']}", event.usage.total_tokens)
        elif isinstance(event, t.TurnCompleted):
            await self.complete(event)
        elif isinstance(event, t.Notice):  # the bridge's, or the agent's program's (#677)
            # redacted, NULs dropped first, before the marker, which would defeat the
            # line-anchored rules (§11.6; reviews of #678)
            note = self.note(redact_secrets(event.message.replace("\x00", "")), event.level)
            for part in await self.renderer.render_agent_text(note, self.context):
                await self.post(part)

        if isinstance(event, t.TurnStarted) and self.turn:
            if any(
                value is not None for value in (event.model, event.effort, event.effort_supported)
            ):
                self.store.set(
                    f"turn_settings:{self.turn['id']}",
                    {
                        "model": event.model,
                        "effort": event.effort,
                        "effort_supported": event.effort_supported,
                    },
                )
                await self.status()

    def note(self, text: str, level: str = "warning") -> str:
        """`text` as the bridge's own message in this conversation (SPEC §4.9, issue #677)."""
        return view.bridge_note(self.backend.kind, text, level)

    def mention(self) -> str:
        name = self.store.get(f"sender:{self.row['id']}", "user")
        uid = self.row.get("last_sender_id")
        return f"@**{name}|{uid}**" if uid else ""

    async def resolve(self, request_id: str, state: str) -> None:
        rows = self.store.rows("approvals", request_id=request_id, state="pending")
        if not rows:
            return
        self.pending.pop(request_id, None)
        self.store.update("approvals", {"request_id": request_id}, state=state)
        if rows[0]["message_id"]:
            await self.poster.edit(  # the request's one edit: final (#754)
                rows[0]["message_id"], f"Approval: {state.replace('_', ' ')}", APPROVAL, final=True
            )
        if not self.pending and self.turn:
            self.state(state="running")
            await self.refresh()

    async def complete(
        self, event: t.TurnCompleted, drain: bool = True, *, release_slot: bool = True
    ) -> None:
        if (
            not self.turn
            and self.unconfirmed_turn
            and self.unconfirmed_turn["native_turn_id"] == event.turn_id
        ):
            self.turn = self.unconfirmed_turn
            self.turn["final_message_ids"] = self.turn["ended_at"] = None
            self.store.update("turns", {"id": self.turn["id"]}, final_message_ids=None)
        if not self.turn or self.turn["native_turn_id"] != event.turn_id:
            return
        if release_slot:
            self.unconfirmed_turn = None
            if self.metric_completion_id != self.turn["id"]:
                self.poster.client.metrics.counts["turns_" + event.status.value] += 1
                self.metric_completion_id = self.turn["id"]
        self.completion = event
        # Its duration as the summary shows it, unchanged by a retry (#754)
        self.turn["ended_at"] = self.turn.get("ended_at") or time.time()
        failed = False
        cancelled = False
        if self.stop_timer:
            self.stop_timer.cancel()
            self.stop_timer = None
        try:
            if event.usage and event.usage.total_tokens is not None:
                # Native turn IDs deduplicate replay; Claude's running total resets on reopen.
                self.store.set(f"turn_tokens:{self.turn['id']}", event.usage.total_tokens)
            if release_slot:
                self.store.set(f"completion:{self.turn['id']}", asdict(event))
            if self.row["state"] not in ("readonly", "detached"):
                await self.publish_completion(event)
                if release_slot and self.row.get("parent_session_id"):
                    from zab.core.fork import report_fork

                    await report_fork(self, event)  # a fork's first answer, to its parent (#202)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ReadOnlySession:
            pass
        except Exception as exc:
            failed = True
            self.log_completion_error(exc)
            try:
                # Never pass raw agent output through a failed renderer.
                key = f"completion_warning:{self.turn['id']}"
                if not self.store.get(key, False):
                    await self.post(self.note("Answer delivery is pending; use !status to retry."))
                    self.store.set(key, True)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except ReadOnlySession:
                pass
            except Exception as fallback_error:
                self.log_completion_error(fallback_error)
        finally:
            try:
                if not cancelled:
                    self.store.update(
                        "turns",
                        {"id": self.turn["id"]},
                        state="delivery_pending" if failed else event.status.value,
                        ended_at=time.time(),
                    )
                    self.state(state="error" if failed else "idle")
                    if not failed:
                        self.store.set(f"completion:{self.turn['id']}", None)
                        self.store.delete(f"restart_notice:{self.turn['id']}")
                        self.store.delete(f"diff_overlap:{self.turn['id']}")
                        # Its status message is edited no more: the store forgets its edits (#757).
                        # Not after a stop that timed out: the backend may still run the turn,
                        # which recovery then takes up again with that message (codex round 1)
                        if release_slot:
                            self.poster.client.edit_bounds.forget(self.turn["status_message_id"])
            finally:
                if not failed and not cancelled:
                    self.turn = None
                    self.completion = None
                if release_slot:
                    self.release()
        try:
            await self.card()
        except Exception as exc:
            self.log_completion_error(exc)
        if drain and not failed:
            self.schedule()

    def log_completion_error(self, exc: Exception) -> None:
        logging.getLogger("zab.session").exception(
            "Turn presentation failed: %s",
            type(exc).__name__,
            exc_info=False,
            extra={"bot": self.row["bot"], "session": self.row["id"]},
        )

    async def publish_completion(self, event: t.TurnCompleted) -> None:
        assert self.turn
        restart_notice = self.store.get(f"restart_notice:{self.turn['id']}", None)
        await self.status(event.status.value, notice=restart_notice)
        if restart_notice:
            return  # The edited status is the restart notice (SPEC §7.8.1).
        self.context.turn_number = len(self.store.rows("turns", session_id=self.row["id"]))
        auth_failure = (
            event.status == t.TurnStatus.FAILED
            and event.error
            and event.error.kind == t.ErrorKind.AUTH
        )
        # A backend that is briefly unavailable says so in one plain sentence for the user, such
        # as Claude's lost OAuth refresh race (claude_stream.REFRESH_RACE_MESSAGE, #307 item 2):
        # no "(no final message)" and no raw kind word. The error is the bridge's (#677).
        error = event.error
        transient = error is not None and error.kind == t.ErrorKind.BACKEND_UNAVAILABLE
        if auth_failure:
            text = self.note(login_required(self.backend.kind))
        else:
            parts = [event.final_text] if event.final_text else []
            if not parts and not transient:
                parts = ["(no final message)"]
            if error is not None:
                # before the cut and the marker, NULs dropped first (§11.6; reviews of #678)
                message = redact_secrets(error.message.replace("\x00", ""))[:1000]
                parts.append(self.note(message if transient else f"{error.kind.value}: {message}"))
            text = "\n\n".join(parts)
        ids = json.loads(self.turn["final_message_ids"] or "[]")
        # The turn's diff comes first: the answer's file references prefer the files this
        # turn touched (SPEC §11.3, issue #36), and it is posted after the answer.
        diff: t.DiffResult | None = None
        diff_error: Exception | None = None
        if self.turn["start_snapshot"] and not auth_failure:
            try:
                snap = t.Snapshot(**json.loads(self.turn["start_snapshot"]))
                diff = await self.manager.diff(self.context.workspace, snap)
            except ReadOnlySession:
                raise
            except Exception as exc:
                diff_error = exc
        context = replace(self.context, final_answer=True)  # text uploads: answers only
        if diff is not None:
            context = replace(context, turn_files=frozenset(diff.changed + diff.new_untracked))
        if self.refserve_url and not auth_failure:
            context = replace(context, snapshot=self.snapshot_provider())
        try:
            parts = await self.renderer.render_agent_text(text, context)
        except Exception as exc:
            if ids:
                # A fallback has its own cursor; keep partially delivered answers retryable.
                raise
            self.log_completion_error(exc)
            parts = [self.note("Unable to format this answer. View it in the terminal.")]
        for part in parts[len(ids) :]:
            ids.append(await self.post(part))
            self.turn["final_message_ids"] = json.dumps(ids)
            self.store.update("turns", {"id": self.turn["id"]}, final_message_ids=json.dumps(ids))
        if auth_failure:
            return  # One helpful line, without raw auth errors, diffs or another failure ping.
        if self.turn["start_snapshot"] and len(ids) == len(parts):
            try:
                if diff_error is not None:
                    raise diff_error
                if diff and not diff.empty:
                    key = f"diff_overlap:{self.turn['id']}"
                    names = self.store.get(key)
                    if names is None:
                        names = [
                            self.store.get(f"bot_name:{bot}", bot)
                            for bot in self.store.overlapping_bots(
                                self.row["id"], self.turn["started_at"], time.time()
                            )
                        ]
                        self.store.set(key, names)
                    note = (
                        (
                            "\n\nChanges may also include work from "
                            + ", ".join(view.code(name) for name in names)
                            + ", active in the same folder during this turn."
                        )
                        if names
                        else ""
                    )
                    diff_context = replace(
                        context, max_message_length=context.max_message_length - len(note)
                    )
                    content = await self.renderer.render_diff(diff, diff_context) + note
                    ids.append(await self.post(content))
                    self.turn["final_message_ids"] = json.dumps(ids)
                    self.store.update(
                        "turns", {"id": self.turn["id"]}, final_message_ids=json.dumps(ids)
                    )
            except ReadOnlySession:
                raise
            except Exception as exc:
                self.log_completion_error(exc)
                await self.post(self.note("No diff available for this turn."))
        if (
            event.status == t.TurnStatus.FAILED
            or time.time() - self.turn["started_at"] >= self.mention_after
        ):
            if self.row.get("last_sender_id"):
                await self.post(self.mention() + " turn " + event.status.value)

    def status_text(self, state: str | None = None) -> str:
        assert self.turn
        if state is None and self.completion:
            # The turn ended and only its answer waits for Zulip (§7.8.2): show how it ended,
            # not the session's `error` (scenario 4 on 0bca627: an "error · 12m 20s" heading).
            state = self.completion.status.value
        return view.status(
            state or self.row["state"],
            self.turn["started_at"],
            list(self.actions.values()),
            self.renderer,
            self.context.max_message_length,
            self.plan,
            list(self.notes.values()),
            ended=self.turn.get("ended_at"),
            **self.store.get(f"turn_settings:{self.turn['id']}", {}),
        )

    def snapshot_provider(self) -> Callable[[], Awaitable[t.PublishedSnapshot | None]]:
        """The turn's snapshot, published on first use and then shared (SPEC §10.7): a turn
        whose answer and diff link no file publishes and pushes nothing, and the answer and its
        diff link one commit, pushed once. Never raises."""
        memo: list[t.PublishedSnapshot | None] = []
        lock = asyncio.Lock()

        async def provide() -> t.PublishedSnapshot | None:
            async with lock:
                if not memo:
                    memo.append(await self.publish_snapshot())
                return memo[0]

        return provide

    async def publish_snapshot(self) -> t.PublishedSnapshot | None:
        """The turn's snapshot on the central file server (SPEC §10.7), or None: rendering then
        goes on exactly as without it. Never raises."""
        from urllib.parse import quote

        try:
            ref = await self.manager.publish_snapshot(
                self.context.workspace,
                f"{self.row['id']}/{self.turn['id']}",
                github_owner=self.refserve_owner,
            )
        except Exception as exc:
            self.log_completion_error(exc)
            return None
        if ref is None:
            return None
        base = self.refserve_url.rstrip("/")
        return t.PublishedSnapshot(sha=ref.sha, url=f"{base}/{quote(ref.repo)}/{ref.sha}")

    async def status(self, state: str | None = None, *, notice: str | None = None) -> None:
        if not self.turn or self.row["state"] in ("readonly", "detached"):
            return
        if not self.turn["status_message_id"]:
            self.last_status = ""
            self.turn["status_message_id"] = await self.post("⏳ **Working**", ACK)
            self.store.update(
                "turns", {"id": self.turn["id"]}, status_message_id=self.turn["status_message_id"]
            )
        content = notice or self.status_text(state)
        if content == self.last_status:
            return
        # Final, its last edit: the turn's summary, or the restart notice in its place; not a
        # "stopping" or "backend reconnecting" while an answer waits (review of #755)
        if state is None and self.completion:
            state = self.completion.status.value  # what `status_text` showed
        final = notice is not None or state in view.FINISHED
        if final:  # a look tries it again a gap later at the earliest (`tick`)
            self.retried = (self.turn["id"], self.poster.client.budget.wall())

        def failed() -> None:  # a held edit (codex round 1): the next refresh tries it again
            if self.last_status == content:
                self.last_status = ""

        mid = self.turn["status_message_id"]
        try:
            shown = await self.poster.edit(mid, content, final=final, paused=PAUSED, failed=failed)
            if final and not shown and mid not in self.poster.held:
                # Its edits are spent, the last by a final edit that failed (codex round 2): the
                # summary goes in a new message, as when the old one can't be edited at all. One
                # held is a repeat, which goes out when its gap has passed (#806).
                raise EditRefused(mid)
        except (ZulipError, EditRefused):
            mid = await self.post(content, ACK)
            if final:  # the summary went out with it: a repeat waits for its gap (#806)
                self.poster.client.edit_bounds.sent(mid)
            self.last_status = ""
            self.turn["status_message_id"] = mid
            self.store.update("turns", {"id": self.turn["id"]}, status_message_id=mid)
        self.last_status = content

    def log_error(self, operation: str, exc: Exception) -> None:
        """Log diagnostic metadata only, never exception text or request contents (§14)."""
        detail = f" code={exc.code} status={exc.status}" if isinstance(exc, ZulipError) else ""
        logging.getLogger("zab.session").error(
            "%s failed: %s%s",
            operation,
            type(exc).__name__,
            detail,
            exc_info=False,
            extra={
                "bot": self.row["bot"],
                "session": self.row["id"],
                "turn": self.turn["id"] if self.turn else None,
            },
        )

    async def card(self) -> None:
        """The anchor is immutable; current details are available through !session."""

    async def command(
        self,
        command: str,
        decision: t.ApprovalDecision | None = None,
        request_id: str | None = None,
        delivery: Delivery | None = None,
    ) -> None:
        if self.row["state"] in ("readonly", "detached"):
            return
        if await run_command(self, command, delivery):
            return
        quiet = delivery is not None and delivery.quiet

        if command == DETACH:
            note = ""
            if self.turn and self.backend.capabilities.approvals:
                note = (
                    " Pending approvals can keep the turn and its slot blocked until reattached; "
                    "new approval requests are not mirrored while detached."
                )
            shared_worktree = any(
                row["id"] != self.row["id"] and row["state"] != "detached"
                for row in self.store.rows("sessions", repo_root=self.row["repo_root"])
            )
            if self.row["workspace_mode"] == "worktree" and not shared_worktree:
                note += "\nCleanup when finished: " + view.code(
                    "git worktree remove -- " + shlex.quote(self.row["repo_root"])
                )
            await self.post(
                "Detached. The native session and workspace are preserved; "
                "held messages will not run. Mention me to start a new session in this topic."
                + note
            )
            self.state(state="detached")
            self.ready = False
            if self.waiter:
                self.waiter.cancel()
                self.waiter = None
            if not self.turn:
                self.release()
        elif command == "!attach" or command.startswith("!attach "):
            await self.post("This topic already has a session; use !detach first.")
        elif command == "!stop" and self.turn:
            for rid in list(self.pending):
                await self.backend.respond(rid, t.ApprovalDecision.CANCEL)
                await self.resolve(rid, "cancelled")
            await self.backend.interrupt(self.row["native_id"])
            await self.status("stopping")

            if self.stop_timer:
                self.stop_timer.cancel()
            tid = self.turn["native_turn_id"]

            async def timeout() -> None:
                await asyncio.sleep(30)
                self.put(StopTimeout(tid))

            self.stop_timer = asyncio.create_task(timeout())
        elif command == "!stop":  # no turn running
            await self.stop_idle(delivery)
        elif quiet and command.split(maxsplit=1)[:1] == ["!new"]:
            return  # a session here already: the VM's others act on it
        elif command in ("!approve", "!approve-session", "!deny") or decision:
            if not self.pending:
                return
            rid = request_id or next(iter(self.pending))
            if rid not in self.pending:
                return
            event = self.pending[rid]
            decision = (
                decision
                or {
                    "!approve": t.ApprovalDecision.ACCEPT,
                    "!approve-session": t.ApprovalDecision.ACCEPT_FOR_SESSION,
                    "!deny": t.ApprovalDecision.DECLINE,
                }[command]
            )
            if decision not in event.decisions:
                await self.post("That decision is not available for this request.")
                return
            await self.backend.respond(rid, decision)
            await self.resolve(
                rid,
                {
                    t.ApprovalDecision.ACCEPT: "accepted",
                    t.ApprovalDecision.ACCEPT_FOR_SESSION: "accepted_for_session",
                    t.ApprovalDecision.DECLINE: "declined",
                    t.ApprovalDecision.CANCEL: "cancelled",
                }[decision],
            )
        elif command == "!queue clear":
            self.store.clear_held(
                self.row["id"], [m["message_id"] for m in self.store.held(self.row["id"])]
            )
            await self.post("Queue cleared.")
        elif command == "!queue":
            await self.post(f"{len(self.store.held(self.row['id']))} messages held.")
        elif command == "!backend-down":
            self.ready = False
            await self.status("backend reconnecting")
        elif command.split(maxsplit=1)[0:1] in (["!model"], ["!effort"]):
            parts = command.split(maxsplit=1)
            field = parts[0][1:]
            previous = t.SessionSettings(**json.loads(self.row["settings_json"]))
            settings = previous
            note = ""
            if len(parts) > 1:
                value = parts[1].strip()
                candidate = replace(previous, **{field: None if value == '""' else value})
                try:
                    # unmentioned, a value is this agent's only if its backend can verify it
                    with verified_only(quiet):
                        settings = await self.backend.update_settings(
                            self.row["native_id"], candidate
                        )
                except t.InvalidSetting as exc:
                    if not quiet:  # a value for another of the VM's agents (§4.6)
                        await self.post(view.invalid_setting(exc))
                    return
                self.state(settings_json=json.dumps(asdict(settings)))
                if field == "model" and previous.effort is not None and settings.effort is None:
                    note = (
                        " · effort reset to the new model's default (previous effort unsupported)"
                    )
                await self.card()
            effective = effective_settings(self.backend, self.row["native_id"])
            await self.post(
                f"{field}: " + view.code(display_setting(field, settings, effective)) + note
            )
        elif command == "!status":
            await self.report_status()
        else:
            await self.post(UNKNOWN)

    async def stop_idle(self, delivery: Delivery | None) -> None:
        """`!stop` with no turn of this session running (SPEC §7.7, #847): the backend stops the
        sub-agents its ended turns left running, where it has any (Codex), and the bot says how
        many it stopped, or, unmentioned too, that one may still run: no other bot would say
        so (Codex round 2 on #851), or that a turn started outside Zulip runs there, whose
        sub-agents it leaves alone. Else "No turn is running": from this bot when named, and
        unmentioned only from the bot that answers for the topic (`Delivery.first`), and only
        where no turn of the topic runs (§4.6), which its bot stops."""
        has_subagents = self.backend.capabilities.stop_subagents
        stopped: t.SubagentStop | None = t.SubagentStop(0)
        if has_subagents:
            try:
                stopped = await self.backend.stop_subagents(self.row["native_id"])
            except t.TurnElsewhere:
                await self.post(TURN_ELSEWHERE)
                return
            except Exception as exc:
                self.log_error("Stopping sub-agents", exc)
                stopped = None  # unknown: never "none ran"
        if stopped is None:
            await self.post(SUBAGENTS_UNCHECKED)
        elif stopped.stopped:  # only this bot knows it
            await self.post(stopped_subagents(stopped.stopped, unseen=stopped.unseen))
        elif delivery is None or not delivery.quiet or (delivery.first and not self.turn_here()):
            # a zero, with what it can't check (Codex round 3), gated as any zero (review
            # round 3 of #851): beside another bot's running turn, it says nothing
            if stopped.unseen:
                await self.post(stopped_subagents(0, unseen=True))
            else:
                await self.post(NO_TURN_NOR_SUBAGENT if has_subagents else NO_TURN)

    def turn_here(self) -> bool:
        """Whether a turn of another bot's session in this topic runs."""
        rows = self.store.rows("sessions", stream_id=self.row["stream_id"], topic=self.row["topic"])
        return any(
            self.store.rows("turns", session_id=row["id"], state="running")
            for row in rows
            if row["id"] != self.row["id"] and row["state"] != "detached"
        )

    async def reopen(self, request: Reopen) -> None:
        """Take the backend's snapshot now and apply it at once (SPEC §7.8.1). A snapshot
        taken outside this loop could predate a turn that `begin()` starts before it is
        applied, and recovery would then interrupt that live turn."""
        from zab.core.recovery import recover, session_spec

        if self.row["state"] in ("readonly", "detached"):
            return
        try:
            async with request.turns:
                if request.stopped:
                    raise t.BackendUnavailable("reopening stopped")
                info = await self.backend.open_session(self.row["native_id"], session_spec(self))
        except t.BackendUnavailable:
            request.stopped = True
            self.ready = False
            await self.status("backend reconnecting")
            return
        except Exception as exc:
            self.ready = False
            self.state(state="error")
            logging.getLogger("zab.runtime").warning(
                "Session recovery failed: %s", type(exc).__name__, extra={"session": self.row["id"]}
            )
            if self.waiting_message_id and self.store.held(self.row["id"]):
                # A message held before this round, such as one caught up at startup, waits
                # under "⏳ Waiting…", which no longer ends: say what unblocks it (§7.3).
                try:
                    await self.poster.edit(self.waiting_message_id, self.note(NEEDS_RECOVERY), ACK)
                except Exception as edit:
                    self.log_error("Waiting notice", edit)
            return
        request.info = info
        await recover(self, info)

    async def report_status(self) -> None:
        """Refresh/recover, then acknowledge !status with one line (SPEC §4.6)."""
        note = await status_login_hint(self.backend)
        reason = "session needs recovery"
        try:
            if self.completion:
                note += " · pending answer delivery retried"
                await self.complete(self.completion)
            elif self.row["state"] == "error" or not self.ready:
                # Also a session left backend-down by a round that stopped: reopen it now if
                # the backend answers, as its next BackendStatus(up=True) would (§7.8.1).
                from zab.core.recovery import recover, session_spec

                note += " · recovery attempted"
                spec = session_spec(self)
                try:
                    info = await self.backend.open_session(self.row["native_id"], spec)
                except t.BackendUnavailable:
                    if self.row["state"] == "error":
                        raise
                    note += ": backend unavailable"
                else:
                    await recover(self, info)
            await self.card()
            await self.status()
            self.schedule()
        except ReadOnlySession:
            raise
        except Exception as exc:
            self.log_error("Status refresh", exc)
            self.state(state="error")
            # Exception text may contain private request contents; a refusal's reason never does.
            reason = (
                self.renderer.render_inline(str(exc))
                if isinstance(exc, t.SessionRefused)
                else type(exc).__name__
            )

        state = self.row["state"]
        if state == "error":
            if self.completion:
                reason = "answer delivery pending"
            line = f"⚠️ Error: {reason}"
        elif state == "awaiting_approval" or self.pending:
            line = "⏸ Waiting for approval"
        elif self.turn:
            elapsed = max(0, int(time.time() - self.turn["started_at"]))
            held = len(self.store.held(self.row["id"]))
            line = (
                f"⏳ Running · {elapsed // 60}m {elapsed % 60:02d}s"
                f" · {len(self.actions)} actions · {held} messages held"
            )
            if mid := self.turn["status_message_id"]:
                url = (
                    f"{self.poster.client.site}/#narrow/channel/{self.row['stream_id']}/near/{mid}"
                )
                line += f" · [status message]({url})"
        else:
            turns = self.store.rows("turns", session_id=self.row["id"])
            line = f"💤 Idle · {len(turns)} turn" + ("" if len(turns) == 1 else "s")
            ended = [turn["ended_at"] for turn in turns if turn["ended_at"] is not None]
            if ended:
                line += (
                    " · last turn <time:"
                    + datetime.fromtimestamp(max(ended), UTC).isoformat()
                    + ">"
                )
        from zab.core.git_identity import display

        await self.post(line + note + display(self.git_identity))

    async def close(self) -> None:
        for task in (self.runner, self.timer, self.waiter, self.stop_timer):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.release()
