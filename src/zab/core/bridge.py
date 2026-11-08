"""Per-bot Zulip listener and shared-protocol wiring (SPEC §7.1)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import shlex
import time
from asyncio import sleep
from asyncio import sleep as metrics_sleep
from pathlib import Path
from typing import Any

import httpx

from zab.config import BotConfig, Config, allowed_users
from zab.core import btw, unmentioned
from zab.core import presentation as view
from zab.core import types as t
from zab.core.attach import attach
from zab.core.catchup import MAX_AGE, Catchup
from zab.core.commands import (
    BTW_DM,
    DM_HELP,
    DM_HINT,
    FORK_DM,
    HELP,
    UNKNOWN,
    status_login_hint,
)
from zab.core.creation import create_session
from zab.core.fork import (
    defer_fork,
    drop_pending_fork,
    fork,
    is_fork,
    parent_of,
    pending_forks,
    waits,
)
from zab.core.git_identity import display as identity_display
from zab.core.login import Login
from zab.core.metrics import Metrics
from zab.core.router import Route, Router
from zab.core.session import Delivery, Reopen, SessionActor
from zab.core.store import Store
from zab.core.watchdog import Heartbeat, waiting, waiting_lock
from zab.zulip.client import ZulipClient, ZulipError
from zab.zulip.poster import Poster

# Reconciliation covers sessions with activity this recent (§2 "active session").
RECENT = 7 * 86400
# A cached channel lookup is forgotten on a stream or subscription event, a new event
# queue, or after this long, whichever comes first.
CHANNEL_TTL = 120  # until the stream-event VERIFY (SPEC §4.10) is resolved on the rig
# A session creation Zulip keeps interrupting while it answers polls: the listener retries it
# after 1 s, doubling up to this (issue #534).
CREATION_BACKOFF_CAP = 60
# A first prompt still pending past the catch-up's limit, MAX_AGE (SPEC §7.2, #534).
CREATION_EXPIRED = (
    "[This message]({url}) could not start a session within {hours} hours and has "
    "expired; send it again if it's still needed."
)


def transient_zulip_error(exc: ZulipError) -> bool:
    """Whether retrying the request can succeed: a rate limit, a server error (5xx), a
    response that is not JSON whatever its status (a proxy page, a truncated 200: codex
    review on #199), or an expired event queue. Any other error is Zulip refusing this
    request, and it refuses it again on every retry."""
    return (
        exc.status == 429
        or exc.status >= 500
        or exc.code in ("INVALID_RESPONSE", "BAD_EVENT_QUEUE_ID")
    )


def zulip_unreachable(exc: BaseException) -> bool:
    """Whether the request failed because Zulip did not answer, so that it can succeed
    once Zulip answers again: a transport error (refused, reset, timed out) or a transient
    Zulip error. A local failure (`OSError` from the workspace) is not one."""
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, ZulipError) and transient_zulip_error(exc)
    )


class BotBridge:
    def __init__(
        self,
        config: Config,
        bot: BotConfig,
        store: Store,
        backend: t.Backend,
        manager: t.WorkspaceManager,
        renderer: t.Renderer,
        slots: asyncio.Semaphore,
        client: ZulipClient | None = None,
    ) -> None:
        self.config, self.bot, self.store, self.backend = config, bot, store, backend
        self.manager, self.renderer, self.slots = manager, renderer, slots
        self.client = client or ZulipClient.from_zuliprc(bot.zuliprc)
        self.client.metrics = Metrics(bot.name, bot.backend)
        # Each message's edits, kept across restarts (#757): in whichever store the bridge has
        self.client.edit_bounds.keep(lambda: self.store, bot.name)
        self.poster = Poster(self.client)
        self.actors: dict[str, SessionActor] = {}
        self.attaching: dict[str, list[t.BackendEvent]] = {}
        self.peers: list[BotBridge] = [self]
        self.all_bridges: list[BotBridge] = [self]
        self.users: dict[int, dict[str, Any]] = {}
        self.owner_id: int | None = None
        self.router: Router | None = None
        self.registration: dict[str, Any] = {}
        self.listener: asyncio.Task | None = None
        self.creation_lock = asyncio.Lock()
        self.login = Login(self)
        self.heartbeat = Heartbeat(store.state_dir, bot.name)
        self.metric_task: asyncio.Task | None = None
        self.subscribers: dict[int, tuple[float, set[int] | None]] = {}
        # Channel lookups: (expiry, stream). `channel_epoch` keeps a lookup that was in
        # flight when a stream event forgot the channel from being cached after it.
        self.channels: dict[int, tuple[float, dict[str, Any]]] = {}
        self.channel_epoch = 0
        # Sessions whose card location this process has not confirmed (SPEC §4.4), the
        # topics searched for their cards, and the live moves of each card (verify_card).
        self.unverified: set[str] = set()
        self.searched: set[tuple[int, str]] = set()
        self.search_epoch = 0
        self.verify_epoch = 0  # moves on every unverify(): a fetch from before is stale
        self.sender_narrow = True
        self.card_moves: dict[int, int] = {}
        self.housekeeping: asyncio.Task | None = None
        # A session creation Zulip interrupted (issue #532): the listener resumes it, from
        # its pending payload, after its next successful poll (`recover_pending_creations`).
        self.creations_interrupted = False
        # The last refresh of the users, and of the allowlist resolved from them, failed:
        # both fail closed until one succeeds. Pending creations kept meanwhile, because
        # their sender can't be checked, are logged once (issue #534).
        self.users_failed = False
        self.unknown_senders: set[int] = set()
        # `!fork`s that waited for their session (#202): one run of them at a time, and the
        # runs a reopen starts (`pending_forks_soon`)
        self.pending_forks_lock = asyncio.Lock()
        # pending forks a reopen started, and the `!btw` answers (#716)
        self.background: set[asyncio.Task] = set()
        # `!btw`: one side question per session at a time; the lock and how many hold or
        # wait for it, by session id (`btw._one_at_a_time`)
        self.side_questions: dict[str, list] = {}
        # The start of the window in which `siblings` waits longer for this bot's own agent
        # (a Codex daemon connecting after a wake); None once a wait ended unknown (#687).
        self.own_wait_from: float | None = time.monotonic()
        # Each bridge's last sign-in answer, by `id`, with when it came (`siblings`).
        self.sign_ins: dict[int, tuple[float, bool | None]] = {}

    async def initialize(self) -> None:
        """Resolve identity and register events before any bridge starts replaying."""
        me = await self.client.request("GET", "users/me")
        if not me.get("is_bot"):
            raise ValueError("bridge credentials must belong to a bot")
        from zab.core.git_identity import resolve

        self.git_identity = resolve(
            self.config.backends[self.bot.backend].git_identity,
            me["full_name"],
            me.get("email", ""),
        )
        self.users[me["user_id"]] = me
        self.store.set(f"site:{self.bot.name}", self.client.site)
        self.store.set(f"bot_name:{self.bot.name}", me["full_name"])
        self.owner_id = me.get("bot_owner_id")
        self.router = Router(
            self.store,
            self.bot.name,
            me["user_id"],
            me["full_name"],
            allowed_users(self.bot, me.get("bot_owner_id")),
            self.config.bridge.allow_public_channels,
            kind=self.backend.kind,
        )
        self.registration = await self.poster.call(self.client.register)
        await self.refresh_users()
        if url := self.registration.get("server_emoji_data_url"):
            async with httpx.AsyncClient() as http:
                response = await http.get(url)
                response.raise_for_status()
                codes = response.json()["code_to_names"]
            self.poster.emoji_names = {
                name: codes.get(code, [name])[0]
                for name, code in {
                    "eyes": "1f440",
                    "arrow_right_hook": "21aa",
                    "hourglass": "23f3",
                    "check": "2705",  # ✅ a steer the agent has taken in (#654)
                }.items()
            }

    async def start(self) -> None:
        """Answer first, housekeeping after (SPEC §7.1, issue #443).

        The catch-up replay and the listener come before the per-session housekeeping.
        Confirming every card's location (§4.4) and reconciling every recent topic
        (§7.8.2) cost a request or two per session; through the bot's budget they delayed
        the answer to the message that woke the VM by a minute and more. Routing confirms
        the cards a message depends on first (`ensure_location`). Sessions that can post
        before any message reaches them (a turn, held or pending input, an approval) are
        confirmed here, before the replay, as all sessions were before."""
        if self.router is None:
            await self.initialize()
        await self.stop_housekeeping()
        began = time.monotonic()
        catchup = Catchup(self)
        await catchup.initialize()
        await self.confirm_working_cards()
        self.prepare_actors()
        for message_id, content in self.client.edit_bounds.held_finals():
            self.poster.resume(message_id, content)  # a summary an earlier run held back (#806)
        # Zulip failing here fails the start, as in the replay: the replay must not create
        # a topic's session from a later message (codex review on #533).
        await self.recover_pending_creations(interrupt=True)
        count = await catchup.replay()
        self.listener = asyncio.create_task(self.listen())
        logging.getLogger("zab.bridge").info(
            "Caught up %d messages in %.1f s; housekeeping follows",
            count,
            time.monotonic() - began,
            extra={"bot": self.bot.name},
        )
        self.housekeeping = asyncio.create_task(self.housekeep(catchup))
        if self.metric_task is None:
            self.metric_task = asyncio.create_task(self.report_metrics())

    async def confirm_working_cards(self) -> None:
        """No card location is confirmed any more (`unverify`); those of the sessions that can
        post before a message reaches them (a turn, held or pending input, an approval) are
        confirmed now (§4.4): at start-up and after an event-queue loss (§7.1, §7.8.2). Every
        other card is confirmed when a message reaches its topic (`ensure_location`) or its
        session posts (`verify_card`), and the recent ones by the housekeeping after the
        replay: their cost grows with the sessions, and no answer waits for it."""
        self.forget_channels()
        self.unverify()
        for row in self.store.rows("sessions", bot=self.bot.name):
            if row["state"] != "detached" and self.has_work(row):
                await self.verify_card(row["id"])

    async def housekeep(self, catchup: Catchup) -> None:
        """After the replay of a start-up or of an event-queue loss's recovery, in the
        background and within the budget's reserve (`Poster.background`): confirm the cards of
        recent sessions, then reconcile their topics (§7.8.2) from the boundary captured
        before the replay. Older sessions are confirmed when a message reaches their topic
        (`ensure_location`)."""
        log = logging.getLogger("zab.bridge")
        began, delay = time.monotonic(), 1
        while True:
            try:
                for row in self.recent_sessions():
                    await self.verify_card(row["id"], background=True)
                await self.reconcile_messages(catchup=catchup, background=True)
                break
            except Exception as exc:
                log.warning(
                    "Housekeeping failed (%s); retrying in %d s",
                    type(exc).__name__,
                    delay,
                    extra={"bot": self.bot.name},
                )
                await sleep(delay)
                delay = min(delay * 2, 60)
        catchup.reconciled()
        log.info(
            "Housekeeping done in %.0f s; %d older sessions are checked on their next message",
            time.monotonic() - began,
            len(self.unverified),
            extra={"bot": self.bot.name},
        )

    async def stop_housekeeping(self) -> None:
        """Cancel the housekeeping and wait for its end. A cancel of the caller propagates:
        under `suppress(CancelledError)` an `await` of the task swallowed it, so `close()`,
        cancelling a listener that waited here at a queue loss, waited forever for a listener
        that went on polling (review of #965). The reference stays until the task has ended,
        so that a later call, `close()`'s own, waits for it too."""
        task = self.housekeeping
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        if self.housekeeping is task:
            self.housekeeping = None

    def recent_sessions(self) -> list[dict[str, Any]]:
        """The sessions reconciliation covers (§2 "active session"): a turn in flight, or
        activity in the last seven days. Newest first."""
        cutoff = time.time() - RECENT
        rows = [
            row
            for row in self.store.rows("sessions", bot=self.bot.name)
            if row["state"] != "detached"
            and (row["updated_at"] >= cutoff or self.has_turn(row["id"]))
        ]
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)

    def has_turn(self, session_id: str) -> bool:
        return bool(
            self.store.rows("turns", session_id=session_id, state="running")
            or self.store.rows("turns", session_id=session_id, state="delivery_pending")
        )

    def has_work(self, row: dict[str, Any]) -> bool:
        """Whether the session can post before a message reaches it: recovering a turn,
        delivering held or pending input, or a pending approval."""
        sid = row["id"]
        return bool(
            self.has_turn(sid)
            or self.store.held(sid)
            or self.store.rows("approvals", session_id=sid, state="pending")
            or self.store.get(f"waiting:{sid}")
            or any(p["session_id"] == sid for p in self.store.pending_deliveries(self.bot.name))
        )

    async def report_metrics(self) -> None:
        while True:
            await metrics_sleep(600)
            self.client.metrics.emit()

    async def channel(self, stream_id: int, *, background: bool = False) -> dict[str, Any]:
        """The channel (`streams/{id}`: its privacy), cached; see CHANNEL_TTL."""
        cached = self.channels.get(stream_id)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        epoch = self.channel_epoch
        call = self.poster.background if background else self.poster.call
        result = await call(lambda: self.client.request("GET", f"streams/{stream_id}"))
        if epoch == self.channel_epoch:
            self.channels[stream_id] = (time.monotonic() + CHANNEL_TTL, result["stream"])
        return result["stream"]

    def forget_channels(self, event: dict[str, Any] | None = None) -> None:
        """Forget the channels a stream or subscription event names; all, without one."""
        self.channel_epoch += 1
        event = event or {}
        ids = set(event.get("stream_ids", []))
        if "stream_id" in event:
            ids.add(event["stream_id"])
        for item in [*event.get("streams", []), *event.get("subscriptions", [])]:
            if isinstance(item, dict) and "stream_id" in item:
                ids.add(item["stream_id"])
        if not ids:
            self.channels.clear()
        for stream_id in ids:
            self.channels.pop(stream_id, None)

    async def channel_members(self, stream_id: int) -> set[int] | None:
        """The channel's subscribers, people and bots, read again at most every three minutes;
        None when Zulip can't say, asked again after ten seconds."""
        expiry, members = self.subscribers.get(stream_id, (0, set()))
        if time.monotonic() >= expiry:
            try:
                result = await self.poster.call(
                    lambda: self.client.request("GET", f"streams/{stream_id}/members")
                )
                members = set(result["subscribers"])
                if members - self.users.keys():
                    await self.refresh_users(update_allowlist=False)
                self.subscribers[stream_id] = (time.monotonic() + 180, members)
            except Exception:
                logging.getLogger("zab.bridge").warning("Channel membership unavailable")
                self.subscribers[stream_id] = (time.monotonic() + 10, None)
                return None
        return members

    async def multi_party(self, stream_id: int) -> bool:
        """Count active human subscribers (`channel_members`)."""
        if not self.config.bridge.multi_party:
            return False
        members = await self.channel_members(stream_id)
        if members is None:
            return True  # Unknown membership must not turn casual chat into a prompt.
        if any(type(self.users.get(uid, {}).get("is_bot")) is not bool for uid in members):
            return True  # Never mistake incomplete identities for a single-human channel.
        return (
            sum(
                self.users.get(uid, {}).get("is_bot") is False
                and self.users[uid].get("is_active", True)
                for uid in members
            )
            > 1
        )

    def foreign_workshop_mentions(self) -> dict[tuple[str, int], str | None]:
        """Map foreign workshop identities to a unique local sibling, never a foreign peer."""
        if (
            not self.config.bridge.multi_party
            or len(self.all_bridges) != len(self.config.bots)
            or not all(p.router for p in self.all_bridges)
        ):
            return {}
        local_ids = self.bridge_bot_ids()
        result = {}
        for uid, user in self.users.items():
            if not user.get("is_bot"):
                continue
            name = user["full_name"]
            kind, _, suffix = name.partition(" · ")
            kind = kind.casefold()
            if uid in local_ids or kind not in {"codex", "claude"} or not suffix:
                continue
            siblings = {
                self.users[p.router.user_id]["full_name"]
                for p in self.all_bridges
                if p.backend.kind == kind and p.router.user_id in self.users
            }
            result[name, uid] = (
                next(iter(siblings)) if kind != self.backend.kind and len(siblings) == 1 else None
            )
        return result

    def first_here(self, message: dict[str, Any], peers: tuple[str, ...]) -> bool:
        """Whether this bot comes first among the bots an unmentioned command reached (`peers`
        and this one) that have a session in its topic: the one that answers for them where
        none acts (SPEC §4.6, `Delivery.first`, #847). A bot whose backend stops sub-agents
        left running (`stop_subagents`) comes before one whose doesn't, then by full name:
        its answer to `!stop` with no turn says what it found, which only it knows, and the
        topic gets that one line (review of #851)."""

        def rank(bridge: BotBridge, name: str | None) -> tuple[bool, str]:
            return not bridge.backend.capabilities.stop_subagents, name or ""

        rows = self.store.rows("sessions", stream_id=message["stream_id"], topic=message["subject"])
        here = {row["bot"] for row in rows if row["state"] != "detached"}
        others = [
            rank(p, name)
            for p in self.all_bridges
            if p is not self and p.router and p.bot.name in here
            if (name := self.users.get(p.router.user_id, {}).get("full_name")) in peers
        ]
        mine = rank(self, self.router.full_name)
        return all(mine < other for other in others)

    async def siblings(
        self,
        message: dict[str, Any],
        multi_party: bool,
        addressees: tuple[str, ...] | None = None,
    ) -> tuple[str, ...] | None:
        """The router's `siblings` (SPEC §4.5 rule 6.2, #713), for a channel message that names
        no bot (`Router.unmentioned`): the VM's other bots (this process's other bridges) that
        allow its sender, which it goes to as well as this one; of them, under the rule `last`,
        those the topic's record names (`addressees`, #878). None where it doesn't go to this
        bot: a multi-party channel or one of unknown membership, a sender this bot doesn't
        allow, a topic where this bot's session was detached, which waits for a mention (§4.6),
        a record that doesn't name it.
        A bot that isn't subscribed to the channel (`channel_members`) never gets the message:
        it is no sibling, and so no peer, nor the bot that answers for the VM.
        With `bridge.multi_party` off, it goes to the bots with a session in the topic and
        starts none: None for this bot without one there.

        A bot without a session in the topic whose agent is known to be signed out (`vm.signed_in`)
        stays out, silently, unless none of those bots is signed in: None for this bot, and left
        out of the others'. Unknown counts as signed in: an answer beats silence. The others'
        agents get `SIGNED_IN_WAIT_S`, and so does its own, except within `SIGNED_IN_OWN_WAIT_S`
        of the bridge's start (a Codex daemon connecting after a wake or a restart), until one
        of its answers is unknown: the startup, whose catch-up holds up the next bridge's, waits
        that long at most once (review round 2 of #688). A bot with a session there is asked
        nothing: its agent answers, with its sign-in hint if it needs one."""
        sender = message["sender_id"]
        if message["type"] != "stream" or multi_party or sender not in self.router.allowlist:
            return None
        rows = self.store.rows("sessions", stream_id=message["stream_id"], topic=message["subject"])
        live = {row["bot"] for row in rows if row["state"] != "detached"}
        detached = {row["bot"] for row in rows} - live  # waits for a mention (§4.6, `!detach`)
        if self.bot.name in detached:
            return None
        bridges = [
            p
            for p in self.all_bridges
            if p.router and sender in p.router.allowlist and p.bot.name not in detached
        ]
        if not self.config.bridge.multi_party and self.bot.name not in live:
            return None
        if addressees is not None:  # the sign-in rule below holds among them (#878)
            if self.router.full_name not in addressees:
                return None
            bridges = [p for p in bridges if p is self or self.full_name_of(p) in addressees]
        # Only the bots in the channel get it (Codex rounds 1 and 2 on #730), the kill switch's
        # included: `multi_party` reads the membership only while the switch is on.
        members = await self.channel_members(message["stream_id"])
        if members is not None:
            bridges = [p for p in bridges if p is self or p.router.user_id in members]
        out: set[int] = set()
        if not self.config.bridge.multi_party:
            out = {id(p) for p in bridges if p.bot.name not in live}
        elif starting := [p for p in bridges if p.bot.name not in live]:
            from zab.core import vm

            own = vm.SIGNED_IN_WAIT_S
            if self.own_wait_from is not None:
                own = max(own, self.own_wait_from + vm.SIGNED_IN_OWN_WAIT_S - time.monotonic())
            now = time.monotonic()
            known = {
                id(p): self.sign_ins[id(p)][1]
                for p in starting
                if now - self.sign_ins.get(id(p), (-math.inf, None))[0] < vm.SIGNED_IN_CACHE_S
            }
            ask = [p for p in starting if id(p) not in known]
            answers = await asyncio.gather(
                *(vm.signed_in(p.backend, own if p is self else None) for p in ask)
            )
            for p, state in zip(ask, answers, strict=True):
                self.sign_ins[id(p)] = (time.monotonic(), state)
                known[id(p)] = state
                if p is self and state is None:
                    self.own_wait_from = None  # unknown: never waited for long again
            out = {id(p) for p in starting if known[id(p)] is False}
            if len(out) == len(bridges):
                out = set()  # none signed in: each answers with its sign-in hint
        if id(self) in out:
            return None
        names = (self.users.get(p.router.user_id, {}).get("full_name") for p in bridges)
        return tuple(
            sorted(
                name
                for p, name in zip(bridges, names, strict=True)
                if name and p is not self and id(p) not in out
            )
        )

    async def answerers(
        self, message: dict[str, Any], recorded: tuple[str, ...] | None = None
    ) -> tuple[str, ...] | None:
        """For a message without a mention in a single-human channel: the full names of the VM's
        bots (this process's bridges, this one's included) that it reaches by their allowlists
        and the channel's members, whatever their sessions, a `!detach` or their agents'
        sign-ins; under the rule `last`, those of them that the topic's record names before it
        (`recorded`, `unmentioned.addressees`, not `addressees`, which reads a card found gone
        as it is now), or all where it names none of them. The first by name answers a command
        whose answer is the VM's (`!help`, `!vm`, `!tools`, a topic's `!unmentioned`) for all
        of them (SPEC §4.6, #899): each bot finds the same one, in whichever order the bots read
        the command and a `!detach`, and one that waits for a mention after `!detach` still
        answers. None with `bridge.multi_party` off, whose rule reaches the bots with a session
        in the topic (`siblings`, which then decides), and for a sender this bot doesn't
        allow."""
        sender = message["sender_id"]
        if not self.config.bridge.multi_party or sender not in self.router.allowlist:
            return None
        members = await self.channel_members(message["stream_id"])
        names = {
            self.router.full_name if p is self else self.full_name_of(p)
            for p in self.all_bridges
            if p.router
            and sender in p.router.allowlist
            and (members is None or p.router.user_id in members)
        }
        names.discard(None)
        return tuple(sorted(names & set(recorded or ()) or names))

    def full_name_of(self, bridge: BotBridge) -> str | None:
        return self.users.get(bridge.router.user_id, {}).get("full_name")

    def last(self) -> bool:
        """Whether the rule `last` routes a message without a mention (#878)."""
        config = self.config.bridge
        return config.multi_party and unmentioned.rule(self.store, config.unmentioned) == "last"

    async def confirm_others(self, stream_id: int, topic: str) -> bool:
        """Before a message without a mention is routed under `last`, and before a `!detach`
        under either rule (#891): the VM's other bots' cards moved into the topic while the
        bridge was down, found (`ensure_location`), bring their record (`Router.move_card`), so
        that this bot reads the record they will (#878). False, logged, when Zulip refuses
        another bot's request: a message without a mention is then routed as without a record,
        to all, and a `!detach` as it is, amending from the entry it finds, rather than either
        being dropped (a refused request skips the event). A transient failure is raised, so
        the message is retried, as for this bot's own cards, and so is any other error, a bug's
        included."""
        for bridge in self.all_bridges:
            if bridge is not self and bridge.router:
                try:
                    await bridge.ensure_location(stream_id, topic)
                except (ZulipError, httpx.HTTPError) as exc:
                    if zulip_unreachable(exc):
                        raise
                    logging.getLogger("zab.bridge").warning(
                        "Another bot's cards unconfirmed (%s)",
                        type(exc).__name__,
                        extra={"bot": self.bot.name},
                    )
                    return False
        return True

    async def addressees(
        self, stream_id: int, topic: str, before: int | None, sender: int | None = None
    ) -> tuple[str, ...] | None:
        """Under the rule `last` (#878), the VM's bots that the topic's newest prompt with a
        mention before message `before` named (`unmentioned.addressees`), of those that can take
        a message there: a message without a mention goes to them. A bot can't when the VM no
        longer has it by that name (renamed), or it is detached in the topic, isn't in the
        channel or doesn't allow `sender`, and when a card's check found its card in the topic
        gone (`unmentioned.card_gone`), which detaches it without a message. A `!detach` takes
        its bots out of the record by its position instead (`Router._route`). None under
        `all`, where no bot that can is named, and with `bridge.multi_party` off, whose rule
        stands under both (SPEC §4.5)."""
        if not self.last():
            return None
        recorded = unmentioned.addressees(self.store, stream_id, topic, before)
        if not recorded:
            return None
        # Only a detach that no message records: a `!detach` amends the record by its position
        # instead (`Router._route`), so that it changes no lookup for a message before it.
        rows = self.store.rows("sessions", stream_id=stream_id, topic=topic)
        gone = {row["bot"] for row in rows if self.store.get(unmentioned.card_gone(row["id"]))}
        detached = gone - {row["bot"] for row in rows if row["state"] != "detached"}
        members = await self.channel_members(stream_id)
        able = {
            self.full_name_of(p)
            for p in self.all_bridges
            if p.router
            and p.bot.name not in detached
            and (members is None or p.router.user_id in members)
            and (sender is None or sender in p.router.allowlist)
        }
        return tuple(name for name in recorded if name in able) or None

    async def unmentioned_here(self, stream_id: int, topic: str) -> str:
        """Where a message without a mention goes in the topic, for its `!session` (#878)."""
        rule = unmentioned.rule(self.store, self.config.bridge.unmentioned)
        if await self.multi_party(stream_id):
            where = "to no agent: more than one person is in this channel, so name one"
        elif not self.config.bridge.multi_party:
            where = "to the agents with a session here"
        elif rule == "last" and (names := await self.addressees(stream_id, topic, None)):
            where = "to " + ", ".join(view.code(name) for name in names)
            where += ", as the topic's last mention named"
        else:
            where = "to all of your agents"
            where += ", until a mention here names some" if rule == "last" else ""
        return f"Without a mention: {where} (`!unmentioned {rule}`, set in a direct message)"

    def forget_sign_in(self) -> None:
        """Drop every bridge's cached answer for this bot's agent (`siblings`): after a `!login`,
        its new sign-in counts at once, not `SIGNED_IN_CACHE_S` later."""
        for p in self.all_bridges:
            cache = getattr(p, "sign_ins", None)
            if isinstance(cache, dict):
                cache.pop(id(self), None)

    def sibling_kinds(self, sender: int) -> dict[str, str]:
        """The router's `sibling_kinds` (#713): the VM's other bots (this process's other
        bridges) that allow `sender`, by full name, with their backend kinds. A typed `@claude`
        or `@codex` from `sender` names those of its kind."""
        return {
            self.users[p.router.user_id]["full_name"]: p.backend.kind
            for p in self.all_bridges
            if p is not self
            and p.router
            and sender in p.router.allowlist
            and p.router.user_id in self.users
        }

    def bridge_bot_ids(self) -> set[int]:
        return {bridge.router.user_id for bridge in self.all_bridges if bridge.router}

    async def refresh_users(self, *, update_allowlist: bool = True) -> None:
        try:
            result = await self.poster.call(lambda: self.client.request("GET", "users"))
            self.users = {u["user_id"]: u for u in result["members"]}
        except Exception:
            if update_allowlist:
                self.users = {}  # Authorization refreshes still fail closed.
                self.users_failed = True
            raise
        else:
            if update_allowlist:
                self.users_failed = False
        finally:
            if self.router and update_allowlist:
                self.router.allowlist = allowed_users(self.bot, self.owner_id, self.users.values())
                for actor in self.actors.values():
                    actor.allowed_senders = self.router.allowlist
                if self.login.owner is not None and self.login.owner not in self.router.allowlist:
                    await self.login.close()

    def sentinel_answers(self, message: dict[str, Any]) -> bool:
        """`zab sentinel` answers this one-to-one DM `!vm` or `!restart`: it was listening when
        the message was sent (#204; SPEC §4.6)."""
        from zab.tools import sentinel

        router, state = self.router, self.store.state_dir
        return sentinel.answers(
            message, router.user_id, state, self.bot.name, router.kind, router.mention
        )

    async def sentinel_command(self, message: dict[str, Any]) -> bool:
        """True when the bridge leaves this DM to `zab sentinel`, or has answered its `!restart`
        itself, which it can't run: nothing was restarted (#204). `!vm` the sentinel didn't
        receive is the bridge's own, as in a topic."""
        from zab.tools import sentinel

        router = self.router
        verb = sentinel.command_text(message.get("content", ""), router.kind, router.mention)
        if verb is None or not sentinel.one_to_one(message, self.router.user_id):
            return False
        if self.sentinel_answers(message):
            return True
        if verb == "!vm":
            return False
        await self.login.send(message["sender_id"], sentinel.restart_unanswered())
        return True

    @staticmethod
    def private(channel: dict[str, Any]) -> bool:
        return channel.get("invite_only") is True and not channel.get("is_web_public", False)

    async def refresh_cards(self) -> None:
        """Confirm every session's card location, and its channel's privacy, now (§4.4). No
        recovery does this any more: a request per session, before anything else, held up
        the answers after an event-queue loss (`confirm_working_cards`)."""
        assert self.router
        self.forget_channels()
        self.unverify()
        for row in self.store.rows("sessions", bot=self.bot.name):
            if row["state"] != "detached":
                await self.verify_card(row["id"])

    def unverify(self) -> None:
        """No card location is confirmed: at startup, or once the event queue, and with it
        the card moves of the gap, was lost."""
        self.unverified = {
            row["id"]
            for row in self.store.rows("sessions", bot=self.bot.name)
            if row["state"] != "detached"
        }
        self.searched.clear()
        self.search_epoch += 1
        self.verify_epoch += 1

    def prepare_actors(self) -> None:
        """An actor for every session, without a request: deliveries reach them, and the
        backend's first `BackendStatus(up)` reopens them (§7.8.1)."""
        for row in self.store.rows("sessions", bot=self.bot.name):
            if row["state"] != "detached" and row["native_id"] not in self.actors:
                self.make_actor(row, self.resume_hint(row)).ready = False

    async def verify_card(self, session_id: str, *, background: bool = False) -> None:
        """Confirm one session's card location (§4.4): the session moves with its card, is
        detached if the card is gone, read-only in a public channel, reopened in a private
        one again. A live `update_message` for the card handled while it was fetched is
        newer than the fetch: the result is dropped and the card fetched again."""
        assert self.router
        call = self.poster.background if background else self.poster.call
        for _ in range(3):
            if session_id not in self.unverified:
                return
            rows = self.store.rows("sessions", id=session_id)
            if not rows or rows[0]["state"] == "detached":
                self.unverified.discard(session_id)
                return
            card_id = rows[0]["card_message_id"]
            moves, epoch = self.card_moves.get(card_id, 0), self.verify_epoch
            card: dict[str, Any] | None = None
            channel: dict[str, Any] = {}
            try:
                result = await call(
                    lambda card_id=card_id: self.client.request(
                        "GET", f"messages/{card_id}", apply_markdown=False
                    )
                )
                card = result["message"]
                channel = await self.channel(card["stream_id"], background=background)
            except ZulipError as exc:
                if exc.status not in (400, 403, 404):
                    raise
                card = None
            if session_id not in self.unverified:
                return  # another caller confirmed it while this fetch was in flight
            if self.card_moves.get(card_id, 0) != moves or self.verify_epoch != epoch:
                continue
            self.unverified.discard(session_id)
            row = self.store.rows("sessions", id=session_id)[0]
            if row["state"] == "detached":
                return
            if card is None:
                with self.store.transaction():  # a detach no message records (#878)
                    self.store.update("sessions", {"id": row["id"]}, state="detached")
                    self.store.set(unmentioned.card_gone(row["id"]), True)
                if actor := self.actors.get(row["native_id"]):
                    actor.row["state"] = "detached"
                    actor.ready = False
                logging.getLogger("zab.bridge").warning(
                    "Session card inaccessible", extra={"session": row["id"]}
                )
                return
            was_readonly = row["state"] == "readonly"
            self.router.move_card(
                {
                    "message_ids": [card_id],
                    "new_stream_id": card["stream_id"],
                    "subject": card["subject"],
                },
                private=self.private(channel),
            )
            row = self.store.rows("sessions", id=row["id"])[0]
            restored = row["native_id"] not in self.actors
            actor = self.make_actor(row, self.resume_hint(row))
            if restored:
                actor.ready = False
            if was_readonly and row["state"] not in ("readonly", "detached"):
                await self.reopen(actor)
            return

    async def ensure_location(self, stream_id: int, topic: str) -> None:
        """Before a message in this topic is routed: confirm the cards of the sessions
        stored here; if none is left here, search the topic, once, for the card of an
        unconfirmed session moved here while the bridge was down (§4.4)."""
        if not self.unverified or self.router is None:
            return

        def here() -> list[dict[str, Any]]:
            rows = self.store.rows("sessions", bot=self.bot.name, stream_id=stream_id, topic=topic)
            return [row for row in rows if row["state"] != "detached"]

        for row in here():
            await self.verify_card(row["id"])
        if here() or not self.unverified or (stream_id, topic) in self.searched:
            return
        epoch = self.search_epoch
        try:
            found = await self.own_messages(stream_id, topic)
        except ZulipError as exc:
            if transient_zulip_error(exc):
                raise
            # Zulip refuses the search: confirm every card instead, once (§4.4).
            logging.getLogger("zab.bridge").warning(
                "Card search refused (Zulip %s, HTTP %s); confirming every card",
                exc.code,
                exc.status,
                extra={"bot": self.bot.name},
            )
            for sid in list(self.unverified):
                await self.verify_card(sid)
            return
        for card in found:
            for row in self.store.rows("sessions", bot=self.bot.name, card_message_id=card):
                await self.verify_card(row["id"])
        if epoch == self.search_epoch:
            self.searched.add((stream_id, topic))

    async def own_messages(
        self, stream_id: int, topic: str, *, by_sender: bool | None = None
    ) -> list[int]:
        """The ids of this bot's messages in a topic, oldest first."""
        assert self.router
        by_sender = self.sender_narrow if by_sender is None else by_sender
        narrow: list[dict[str, Any]] = [
            {"operator": "channel", "operand": stream_id},
            {"operator": "topic", "operand": topic},
        ]
        if by_sender:
            narrow.append({"operator": "sender", "operand": self.router.user_id})
        ids: list[int] = []
        anchor: int | str = "oldest"
        while True:
            try:
                result = await self.poster.call(
                    lambda anchor=anchor: self.client.request(
                        "GET",
                        "messages",
                        anchor=anchor,
                        num_before=0,
                        num_after=100,
                        apply_markdown=False,
                        narrow=narrow,
                    )
                )
            except ZulipError as exc:
                if exc.status != 400 or not by_sender:
                    raise
                # Perhaps a server that refuses the `sender` operand: without it, filtered
                # here. Only a search that then succeeds turns it off for good.
                ids = await self.own_messages(stream_id, topic, by_sender=False)
                self.sender_narrow = False
                return ids
            page = [m for m in result["messages"] if anchor == "oldest" or m["id"] > anchor]
            ids += [m["id"] for m in page if m.get("sender_id") == self.router.user_id]
            if len(page) < 100 or result.get("found_newest"):
                return ids
            anchor = max(m["id"] for m in page)

    def resume_hint(self, row: dict[str, Any]) -> str:
        config = self.config.backends[self.bot.backend]
        native = shlex.quote(row["native_id"])
        prefix = f"cd {shlex.quote(row['workspace'])} && "
        if config.kind == "claude":
            account = json.loads(row["settings_json"]).get("account") or config.default_account
            home = config.accounts.get(account, "~/.claude")
            directory = shlex.quote(str(Path(home).expanduser()))
            return prefix + f"CLAUDE_CONFIG_DIR={directory} claude --resume {native}"
        home = str(Path(config.codex_home or "~/.codex").expanduser())
        return prefix + f"CODEX_HOME={shlex.quote(home)} codex resume {native}"

    async def reopen(self, actor: SessionActor) -> None:
        actor.ready = False
        actor.put(Reopen())  # the actor takes the snapshot itself (§7.8.1)

    async def rename_session(self, actor: SessionActor) -> None:
        key = f"rename_pending:{actor.row['id']}"
        self.store.set(key, True)
        try:
            await self.backend.rename(actor.row["native_id"], actor.row["topic"])
        except Exception:
            logging.getLogger("zab.bridge").warning(
                "Backend rename deferred", extra={"session": actor.row["id"]}
            )
        else:
            self.store.set(key, None)

    async def retry_renames(self) -> None:
        for actor in list(self.actors.values()):
            if self.store.get(f"rename_pending:{actor.row['id']}", False):
                await self.rename_session(actor)

    def make_actor(self, row: dict[str, Any], resume: str) -> SessionActor:
        if row["native_id"] in self.actors:
            actor = self.actors[row["native_id"]]
            actor.row.update(row)
            return actor
        workspace = self.backend.workspace(row["repo_root"], row["workspace"])

        async def upload(item: t.Upload) -> str:
            checked = await self.manager.check_upload(workspace, item.path, explicit=True)
            # A caller that validated the contents (text uploads) passes them: those exact
            # bytes are sent, never a second read the file may have changed in between.
            data = (
                item.data
                if item.data is not None
                else await workspace.read_bytes(
                    checked.path, self.config.render.max_upload_mb * 1024**2 + 1
                )
            )
            limit = (
                min(
                    self.config.render.max_upload_mb, self.client.limits["max_file_upload_size_mib"]
                )
                * 1024**2
            )
            if len(data) > limit:
                raise t.UploadRefused("file exceeds upload limit")
            digest = hashlib.sha256(data).hexdigest()
            # the name is part of the key: `!file x.lean` and the `x.lean.txt` copy differ
            key = f"upload:{row['id']}:{checked.path}:{item.name}:{digest}"
            if cached := self.store.get(key):
                return cached

            url = await self.poster.call(
                lambda: self.client.upload(item.name, data), retry_server_errors=True
            )
            self.store.set(key, url)
            return url

        context = t.RenderContext(
            workspace=workspace,
            max_message_length=self.client.limits["max_message_length"],
            max_upload_bytes=min(
                self.config.render.max_upload_mb, self.client.limits["max_file_upload_size_mib"]
            )
            * 1024**2,
            upload=upload,
            auto_upload_ext=frozenset(self.config.render.auto_upload_ext),
            text_upload_ext=frozenset(
                e.lower().lstrip(".") for e in self.config.render.auto_upload_text_ext
            ),
            max_text_upload_bytes=self.config.render.max_text_upload_kb * 1024,
            max_text_uploads=self.config.render.max_text_uploads,
        )
        actor = SessionActor(
            row,
            self.store,
            self.backend,
            self.poster,
            self.renderer,
            self.manager,
            context,
            self.slots,
            machine=self.config.bridge.machine,
            resume=resume,
            edit_interval=self.config.bridge.status_edit_interval_s,
            mention_after=self.config.bridge.mention_after_s,
            allowed_senders=self.router.allowlist,
            refserve_url=self.config.render.refserve_url,
            refserve_owner=self.config.render.refserve_owner,
            multi_party_enabled=self.config.bridge.multi_party,
            topic_multi_party=self.multi_party,
            bridge_bot_ids=self.bridge_bot_ids,
            bot_user_id=self.router.user_id,
            git_identity=self.git_identity,
            confirm_location=self.verify_card,
            reopened=self.pending_forks_soon,
            unmentioned_here=self.unmentioned_here,
        )
        self.actors[row["native_id"]] = actor
        for pending in self.store.pending_deliveries(self.bot.name):
            if pending["session_id"] == row["id"]:
                if pending["sender_id"] in self.router.allowlist:
                    actor.put(
                        Delivery(
                            pending["message_id"],
                            pending["sender_id"],
                            pending["sender"],
                            pending["text"],
                        )
                    )
                else:
                    self.store.delete(f"pending_delivery:{self.bot.name}:{pending['message_id']}")
        return actor

    def retry_deliveries(self) -> int:
        """Zulip answers again: retry the answers it refused while it was unreachable."""
        actors = [
            actor
            for actor in self.actors.values()
            if actor.completion and actor.row["state"] not in ("readonly", "detached")
        ]
        for actor in actors:
            actor.put("redeliver")
        return len(actors)

    async def listen(self) -> None:
        self.heartbeat.bind()
        log = logging.getLogger("zab.bridge")
        extra = {"bot": self.bot.name}
        queue = self.registration
        last = queue["last_event_id"]
        recovering = False
        catchup: Catchup | None = None
        seen = 0  # the newest message this listener handled: the catch-up floor
        down_since: float | None = None  # the first failed poll of an outage
        probe = False  # poll without blocking once, then retry undelivered answers
        backoff = 0  # seconds: a creation Zulip interrupted again while it answered polls
        resume = False  # the queue-loss recovery resumes at the creations, after the backoff

        async def recover_creations() -> bool:
            """The pending creations (§7.2), or False after waiting out the backoff: Zulip
            answers polls and refuses the creation with a 5xx, or drops its request. That is
            no outage. The probe is not re-armed, so the answers are not posted again, and
            the poll's events wait (codex review on #533). Retried at 1 s, doubling to
            CREATION_BACKOFF_CAP, until it succeeds (issue #534)."""
            nonlocal backoff
            self.creations_interrupted = False  # a failure sets it again
            try:
                await self.recover_pending_creations(interrupt=True)
            except (ZulipError, httpx.TransportError):
                backoff = min(2 * backoff or 1, CREATION_BACKOFF_CAP)
                log.warning("Retrying the pending creations in %d s", backoff, extra=extra)
                await waiting(sleep(backoff))
                return False
            if backoff and (retried := self.retry_deliveries()):
                # Zulip takes posts again: the answers it refused meanwhile, once.
                log.info("Retrying %d undelivered answers", retried, extra=extra)
            backoff = 0
            return True

        while True:
            self.heartbeat.pulse()
            try:
                if recovering:
                    if queue is None:
                        queue = await self.poster.call(self.client.register)
                        last = queue["last_event_id"]
                        self.forget_channels()  # their events of the gap are lost
                        self.client.metrics.counts["reconnects"] += 1
                        log.info("Registered a new Zulip event queue", extra=extra)
                    if not resume:  # a creation's retry costs no request per card
                        await self.refresh_users()
                        # The card moves of the gap went with the queue: as at start-up, the
                        # cards of sessions with work now, any other when something needs it.
                        await self.confirm_working_cards()
                    resume = not await recover_creations()
                    if resume:
                        continue
                    catchup = catchup or Catchup(self, floor=seen)
                    # New topics and DMs too, as at startup (SPEC §7.8.2; e2e scenario 4).
                    count = await catchup.replay()
                    await self.reconcile_reactions()
                    log.info(
                        "Event-queue recovery caught up %d messages; housekeeping follows",
                        count,
                        extra=extra,
                    )
                    # The recent cards and topics in the background, as after a start-up's
                    # replay: inline, a request or two per session held up the answers Zulip
                    # had refused and the gap's messages, past ten minutes on the test VM's
                    # hundreds of sessions (e2e scenario 4 at v0.2.19's candidate, #964).
                    self.housekeeping = asyncio.create_task(self.housekeep(catchup))
                    recovering, catchup = False, None
                # Answer at once: Zulip is back, or a creation's retry is due, whether or not
                # events are waiting.
                if probe or self.creations_interrupted:
                    events = await self.client.events(queue["queue_id"], last, dont_block=True)
                else:
                    events = await self.client.events(queue["queue_id"], last)
                if down_since is not None:
                    elapsed = time.monotonic() - down_since
                    log.info("Zulip reachable again after %.0f s", elapsed, extra=extra)
                    down_since = None
                if probe:
                    probe = False
                    if retried := self.retry_deliveries():
                        log.info("Retrying %d undelivered answers", retried, extra=extra)
                    self.creations_interrupted = True
                if self.creations_interrupted:
                    # The sessions whose creation Zulip interrupted (issue #532), before this
                    # poll's events: a later message in the topic must not create the session
                    # ahead of the first prompt. While Zulip interrupts one again, these events
                    # wait, and the poll after the backoff retries both.
                    if not await recover_creations():
                        continue
                received_at = time.monotonic()
                for event in events:
                    if event["type"] == "message" and event["message"]["type"] == "stream":
                        message = event["message"]
                        self.client.metrics.arrived(
                            message["id"], received_at, message.get("timestamp")
                        )
                for event in events:
                    self.heartbeat.pulse()
                    try:
                        await self.handle(event)
                    except ZulipError as exc:
                        if transient_zulip_error(exc):
                            raise  # Retry this event: rate limits, server errors, the queue.
                        # A refusal (4xx) repeats on every retry, and retrying this event
                        # forever left the bot deaf to everything after it, its owner's
                        # DMs included (Fri Oct 9: a message in a new channel).
                        logging.getLogger("zab.bridge").warning(
                            "Skipping event after Zulip %s (HTTP %s)",
                            exc.code,
                            exc.status,
                            extra={"bot": self.bot.name, "event": event.get("type")},
                        )
                    except (httpx.HTTPError, OSError):
                        raise  # Retain the existing transport retry and queue-recovery paths.
                    except Exception as exc:
                        logging.getLogger("zab.bridge").exception(
                            "Skipping event after %s",
                            type(exc).__name__,
                            exc_info=False,
                            extra={"bot": self.bot.name, "event": event.get("type")},
                        )
                    if event["type"] == "message":
                        seen = max(seen, event["message"]["id"])
                    last = event["id"]
                    self.store.set(f"last_event_id:{self.bot.name}", last)
                    await self.heartbeat.advance()
            except ZulipError as exc:
                if exc.code == "BAD_EVENT_QUEUE_ID":
                    # Zulip drops a queue nobody polled for about ten minutes, events and all.
                    log.info("Zulip event queue expired; registering a new one", extra=extra)
                    queue, recovering, probe = None, True, True
                    # Its subscription events are lost with it: read memberships again.
                    self.subscribers.clear()
                    # Recovery runs it again after its replay, from the boundary a start-up's
                    # or an earlier recovery's housekeeping left, if it had not finished.
                    await self.stop_housekeeping()
                    # Before anything claims messages newer than a new topic or DM.
                    catchup = catchup or Catchup(self, floor=seen)
                else:
                    if exc.status != 429:  # the client logs rate limits itself
                        log.warning(
                            "Zulip %s (HTTP %s); retrying", exc.code, exc.status, extra=extra
                        )
                        if down_since is None:
                            down_since, probe = time.monotonic(), True
                    await waiting(sleep(max(1, exc.retry_after)))
            except (httpx.HTTPError, OSError) as exc:
                if down_since is None:  # once per outage; no request contents
                    down_since, probe = time.monotonic(), True
                    log.warning(
                        "Zulip unreachable (%s); retrying every second",
                        type(exc).__name__,
                        extra=extra,
                    )
                await waiting(sleep(1))

    async def wrong_bot_reply(self, message: dict[str, Any]) -> None:
        """Help a human find their own bots without authorizing or retaining input."""
        assert self.router
        uid = message["sender_id"]
        if message["type"] != "private" or {
            r["id"] for r in message.get("display_recipient", [])
        } != {uid, self.router.user_id}:
            return
        key = f"wrong_bot_reply:{self.bot.name}:{uid}"
        now = time.time()
        if now < self.store.get(key, 0):
            return
        user = self.users.get(uid)
        if user is None:
            try:
                response = await self.poster.call(
                    lambda: self.client.request("GET", f"users/{uid}")
                )
                user = response["user"]
            except (ZulipError, httpx.HTTPError, KeyError):
                return  # Cross-realm bots and unknown identities fail closed.
        if user.get("is_bot") is not False or not user.get("is_active", True):
            return
        names = []
        for owner in sorted(self.router.allowlist):
            info = self.users.get(owner)
            if info is None:
                try:
                    info = (
                        await self.poster.call(
                            lambda owner=owner: self.client.request("GET", f"users/{owner}")
                        )
                    )["user"]
                except (ZulipError, httpx.HTTPError, KeyError):
                    continue
            if info.get("is_bot") is False and info.get("full_name"):
                # Zulip names are untrusted text, never mentions or markdown.
                name = " ".join(info["full_name"].split()).replace("@", "@\u200b")
                for char in "\\`*_[]<>~":
                    name = name.replace(char, "\\" + char)
                names.append(name)
        owner_text = (
            "This bot belongs to " + ", ".join(names) + ". "
            if names
            else "This is another participant's bot. "
        )
        # Persist before the await: concurrent deliveries and restarts cannot
        # reset the one-hour window. A failed send also consumes the window.
        with self.store.transaction():
            if now < self.store.get(key, 0):
                return
            self.store.set(key, now + 3600)
        await self.poster.call(
            lambda: self.client.request(
                "POST",
                "messages",
                type="private",
                to=[uid],
                content=owner_text + "Yours are the two bots with your name: "
                "Claude · your name and Codex · your name. "
                '"Start here" in your sandbox channel has links to both.',
            )
        )

    async def route_message(
        self,
        message: dict,
        user: dict,
        channel: dict,
        *,
        flags: list[str],
        history_only: bool = False,
    ) -> Route:
        if message["type"] == "stream":
            # The topic's session, from a confirmed card location (§4.4).
            await self.ensure_location(message["stream_id"], message["subject"])
        foreign = self.foreign_workshop_mentions()
        multi_party = (
            await self.multi_party(message["stream_id"]) if message["type"] == "stream" else False
        )
        others = {
            u["full_name"]
            for u in self.users.values()
            if u.get("is_bot") and u["user_id"] != self.router.user_id
        }
        unnamed = message["type"] == "stream" and self.router.unmentioned(
            message["content"], others
        )
        addressees = recorded = None
        if unnamed and self.last():
            stream, topic = message["stream_id"], message["subject"]
            if await self.confirm_others(stream, topic):
                sender = message["sender_id"]
                addressees = await self.addressees(stream, topic, message["id"], sender)
                recorded = unmentioned.addressees(self.store, stream, topic, message["id"])
        elif message["type"] == "stream" and self.router.detaching(message["content"], others):
            # its amendment of the record reads the entry before it, under either rule (#891)
            await self.confirm_others(message["stream_id"], message["subject"])
        return self.router.route(
            message,
            foreign_kind_mentions={
                key for key in foreign if key[0].partition(" · ")[0].casefold() == self.backend.kind
            },
            foreign_peer_replacements=foreign,
            flags=flags,
            history_only=history_only,
            sender_name=user["full_name"],
            sender_is_bot=user.get("is_bot") is not False,
            private=self.private(channel),
            multi_party=multi_party,
            siblings=await self.siblings(message, multi_party, addressees) if unnamed else None,
            sibling_kinds=self.sibling_kinds(message["sender_id"]),
            other_bot_names=others,
            addressees=addressees,
            answerers=await self.answerers(message, recorded)
            if unnamed and not multi_party
            else None,
        )

    async def handle(self, event: dict[str, Any], *, cursor: int | None = None) -> None:
        """`cursor`: the catch-up cursor to skip messages up to, instead of the stored one
        (topic reconciliation after the replay, `Catchup.message`)."""
        assert self.router
        if event["type"] == "subscription":
            for stream_id in event.get("stream_ids", []):
                self.subscribers.pop(stream_id, None)
            self.forget_channels(event)
        elif event["type"] == "stream":
            self.forget_channels(event)  # a channel made public, private, renamed, deleted
        elif event["type"] == "realm_user":
            await self.refresh_users()
        elif event["type"] == "message":
            message = event["message"]
            if cursor is None:
                cursor = self.store.get(f"catchup_cursor:{self.bot.name}", 0)
            if message["id"] <= cursor:
                return
            uid = message["sender_id"]
            if uid == self.login.owner:
                if uid not in self.router.allowlist or await self.login.handle(message):
                    return
            if uid not in self.router.allowlist:
                if message["type"] == "private":
                    if self.store.claim(self.bot.name, message["id"], None):
                        await self.wrong_bot_reply(message)
                    return
                if not message["content"].startswith("**zab session**"):
                    # Keep cross-realm channel notifications away from user lookup.
                    self.store.claim(self.bot.name, message["id"], None)
                    return
            if uid not in self.users:
                response = await self.poster.call(
                    lambda: self.client.request("GET", f"users/{uid}")
                )
                self.users[uid] = response["user"]
            user = self.users[uid]
            if (
                uid in self.router.allowlist
                and user.get("is_bot") is False
                and await self.login.handle(message)
            ):
                return
            channel = (
                await self.channel(message["stream_id"]) if message["type"] == "stream" else {}
            )
            route = await self.route_message(
                message, user, channel, flags=event.get("flags", message.get("flags", []))
            )
            if route.action == "ignore":
                return
            if route.action == "dm" and await self.sentinel_command(message):
                return
            if route.text.strip() in ("!vm", "!tools") and route.action in ("dm", "command"):
                if route.text.strip() == "!vm":
                    from zab.core.vm import report

                    text = await report(self, dm=route.action == "dm")
                else:  # the VM's software list (#475), whatever the session's state
                    from zab.core.software import reply

                    limit = self.client.limits.get("max_message_length", 10000) - 200
                    text = await reply(limit)
                if route.action == "dm":
                    await self.login.send(uid, text)
                else:
                    await self.poster.send(message["stream_id"], message["subject"], text)
                return
            default, active = self.config.bridge.unmentioned, self.config.bridge.multi_party
            if route.action == "dm" and (
                answer := unmentioned.command(self.store, default, route.text.strip(), active)
            ):
                await self.login.send(uid, answer)  # the VM's setting, for each bot (#878)
                return
            if route.action == "dm":
                status = (
                    "Bridge running"
                    + identity_display(self.git_identity)
                    + await status_login_hint(self.backend)
                    + f" · without a mention: `{unmentioned.rule(self.store, default)}`"
                    if route.text.strip() == "!status"
                    else ""
                )
                await self.poster.call(
                    lambda: self.client.request(
                        "POST",
                        "messages",
                        type="private",
                        to=[uid],
                        content=DM_HELP
                        if route.text.strip() == "!help"
                        else status
                        if route.text.strip() == "!status"
                        else FORK_DM
                        if is_fork(route.text.strip())
                        else BTW_DM
                        if btw.is_btw(route.text.strip())
                        else UNKNOWN
                        if route.text.lstrip().startswith("!")
                        else DM_HINT,
                    )
                )
            elif route.action in ("reply", "note"):  # a note answers no command (#677)
                text = self.note(route.text) if route.action == "note" else route.text
                await self.poster.send(message["stream_id"], message["subject"], text)
            elif route.action == "new":
                try:
                    await self.create_pending(message, route.text, channel)
                except Exception as exc:
                    if zulip_unreachable(exc):
                        # Zulip went away while the session was created, at its card (e2e
                        # scenario 4, issue #532). The message is claimed: neither the retried
                        # poll nor the catch-up reads it again. Its pending payload stays, and
                        # the listener creates the session after its next successful poll.
                        self.creations_interrupted = True
                        raise
                    self.store.delete(f"pending_delivery:{self.bot.name}:{message['id']}")
                    await self.poster.send(
                        message["stream_id"],
                        message["subject"],
                        self.note(self.renderer.render_inline(str(exc))[:800]),
                    )
            elif (
                route.action == "command"
                and not route.session
                and (route.text == "!attach" or route.text.startswith("!attach "))
            ):
                try:
                    await attach(self, message, route.text, channel)
                except Exception as exc:
                    logging.getLogger("zab.bridge").warning("Attach failed: %s", type(exc).__name__)
                    safe = isinstance(exc, ValueError | t.SessionRefused)
                    detail = str(exc) if safe else type(exc).__name__
                    await self.poster.send(
                        message["stream_id"],
                        message["subject"],
                        "Attach failed: " + self.renderer.render_inline(detail),
                    )
                finally:
                    for sid, events in self.attaching.items():
                        if actor := self.actors.get(sid):
                            for event in events:
                                actor.put(event)
                    self.attaching.clear()
            elif route.action == "command" and route.session and is_fork(route.text):
                await self.fork(message, route.text, user, channel)
            elif route.action == "command" and route.session and btw.is_btw(route.text):
                btw.start(self, message, route.text, user)  # beside the actor (§7.13)
            elif route.session:
                actor = self.actors.get(route.session["native_id"])
                if actor:
                    if route.action == "command":
                        actor.put(
                            (
                                route.text,
                                None,
                                None,
                                Delivery(
                                    message["id"],
                                    uid,
                                    user["full_name"],
                                    route.text,
                                    route.quiet,
                                    not route.quiet or self.first_here(message, route.peers),
                                ),
                            )
                        )
                    else:
                        actor.put(Delivery(message["id"], uid, user["full_name"], route.text))
            else:
                await self.poster.send(
                    message["stream_id"],
                    message["subject"],
                    HELP if route.text == "!help" else UNKNOWN,
                )
        elif event["type"] == "update_message":
            ids = set(event.get("message_ids", [event.get("message_id")]))
            rows = [
                row
                for row in self.store.rows("sessions", bot=self.bot.name)
                if row["card_message_id"] in ids
            ]
            if "subject" in event or "new_stream_id" in event:
                self.router.follow_cards(event)  # any bot's card: the record goes with it (#878)
                for row in rows:
                    # A card fetch in flight is older than this move (verify_card).
                    card = row["card_message_id"]
                    self.card_moves[card] = self.card_moves.get(card, 0) + 1
                    if row["id"] in self.unverified:
                        # Applied to an unconfirmed location, the move may leave the card
                        # somewhere a searched topic would not show it: search again.
                        self.searched.clear()
                        self.search_epoch += 1
            for row in rows:
                channel = await self.channel(event.get("new_stream_id", row["stream_id"]))
                for updated in self.router.move_card(event, private=self.private(channel)):
                    actor = self.make_actor(updated, self.resume_hint(updated))
                    if row["state"] == "readonly" and updated["state"] not in (
                        "readonly",
                        "detached",
                    ):
                        await self.reopen(actor)
                    if "subject" in event and self.backend.capabilities.rename:
                        await self.rename_session(actor)
        elif event["type"] == "reaction" and event.get("op") == "add":
            if (
                event.get("user_id") not in self.router.allowlist
                or event.get("reaction_type") != "unicode_emoji"
            ):
                return
            for actor in self.actors.values():
                if actor.row["state"] == "readonly":
                    continue
                if (
                    actor.turn
                    and actor.turn["status_message_id"] == event["message_id"]
                    and event["emoji_code"] == "1f6d1"
                ):
                    actor.put(("!stop",))
                for approval in self.store.rows(
                    "approvals", session_id=actor.row["id"], state="pending"
                ):
                    if approval["message_id"] == event["message_id"]:
                        decision = {
                            "1f44d": t.ApprovalDecision.ACCEPT,
                            "1f501": t.ApprovalDecision.ACCEPT_FOR_SESSION,
                            "1f44e": t.ApprovalDecision.DECLINE,
                        }.get(event["emoji_code"])
                        if decision:
                            actor.put(("", decision, approval["request_id"]))

    def note(self, text: str) -> str:
        """`text` as the bridge's own warning in a topic (SPEC §4.9, issue #677)."""
        return view.bridge_note(self.backend.kind, text)

    async def fork(
        self, message: dict, text: str, user: dict, channel: dict, *, deferred: bool = False
    ) -> None:
        """`!fork` in a topic with a session (§7.11): a failure is one ⚠️ line in that topic,
        with the reason when it is one for the user, and only its type otherwise. While the
        session reopens (the `!fork` that woke the VM, replayed before its backend is up), the
        fork waits with ⏳ and runs from `run_pending_forks` (review of #572)."""
        if not deferred and waits(self, message):
            defer_fork(self.store, self.bot.name, message, text, user["full_name"])
            _, actor = parent_of(self, message)
            try:
                await actor.react(message["id"], "hourglass")
            except Exception as exc:  # the fork waits either way
                logging.getLogger("zab.bridge").warning("Fork ⏳ failed: %s", type(exc).__name__)
            return
        try:
            await fork(self, message, text, user["full_name"], channel)
        except Exception as exc:
            if deferred:
                drop_pending_fork(self.store, self.bot.name, message["id"])  # run once
            logging.getLogger("zab.bridge").warning(
                "Fork failed: %s", type(exc).__name__, extra={"bot": self.bot.name}
            )
            safe = isinstance(exc, ValueError | t.SessionRefused | t.BackendUnavailable)
            detail = str(exc) if safe else f"Fork failed ({type(exc).__name__}); try again."
            await self.poster.send(
                message["stream_id"],
                message["subject"],
                self.note(self.renderer.render_inline(detail)[:800]),
            )

    async def run_pending_forks(self) -> None:
        """The `!fork`s that waited for their session to reopen (§7.11), oldest first, each run
        once: its row's transaction, or its failure, drops it. One run at a time, each reading
        the list afresh, so two runs never take the same entry. One whose session still isn't
        open waits for its reopen; so does one whose sender can't be checked (a failed user
        refresh, #534). One whose sender a refresh doesn't allow is dropped, and so is one whose
        channel Zulip refuses (review round 2 of #572)."""
        assert self.router
        async with waiting_lock(self.pending_forks_lock):
            for entry in pending_forks(self.store, self.bot.name):
                await self.run_pending_fork(entry)

    async def run_pending_fork(self, entry: dict) -> None:
        assert self.router
        log = logging.getLogger("zab.bridge")
        message = dict(
            id=entry["message_id"],
            type="stream",
            stream_id=entry["stream_id"],
            subject=entry["subject"],
            sender_id=entry["sender_id"],
        )
        if self.users_failed or waits(self, message):
            return  # kept: for the session's reopen, or until the sender can be checked
        if entry["sender_id"] not in self.router.allowlist:
            drop_pending_fork(self.store, self.bot.name, entry["message_id"])
            return
        try:
            channel = await self.channel(entry["stream_id"])
        except ZulipError as exc:
            if transient_zulip_error(exc):
                log.warning("Waiting fork kept: Zulip %s", exc.code, extra={"bot": self.bot.name})
                return
            drop_pending_fork(self.store, self.bot.name, entry["message_id"])
            log.warning("Waiting fork dropped: Zulip %s", exc.code, extra={"bot": self.bot.name})
            with contextlib.suppress(Exception):  # if the topic can still be reached
                await self.poster.send(
                    entry["stream_id"],
                    entry["subject"],
                    self.note("This !fork couldn't run; send it again."),
                )
            return
        except Exception as exc:  # unreachable: kept for the next reopen
            log.warning("Waiting fork kept: %s", type(exc).__name__, extra={"bot": self.bot.name})
            return
        try:
            await self.fork(
                message, entry["text"], {"full_name": entry["sender"]}, channel, deferred=True
            )
        except Exception as exc:  # its ⚠️ couldn't be posted; the fork's failure dropped it
            log.warning("Waiting fork: %s", type(exc).__name__, extra={"bot": self.bot.name})

    def pending_forks_soon(self) -> None:
        """A session reopened outside a backend recovery (a card back in a private channel, an
        actor restored, `!status`): run the forks that waited for it (review round 2 of #572)."""
        if not pending_forks(self.store, self.bot.name) or self.router is None:
            return
        task = asyncio.create_task(self.run_pending_forks())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def create_pending(self, message: dict, text: str, channel: dict) -> None:
        from zab.core.creation import new_options

        async with waiting_lock(self.creation_lock):
            key = f"pending_delivery:{self.bot.name}:{message['id']}"
            pending = self.store.get(key)
            if pending is None:
                return  # Another creator already handed this prompt to its actor.
            rows = (
                self.store.rows("sessions", id=pending["session_id"])
                if pending["session_id"]
                else self.store.rows(
                    "sessions",
                    bot=self.bot.name,
                    stream_id=message["stream_id"],
                    topic=message["subject"],
                )
            )
            existing = next((r for r in rows if r["state"] != "detached"), None)
            if existing:
                _, prompt = new_options(text)
                with self.store.transaction():
                    self.store.set(key, pending | {"session_id": existing["id"], "text": prompt})
                    self.store.update(
                        "inbound",
                        {"bot": self.bot.name, "message_id": message["id"]},
                        session_id=existing["id"],
                        disposition="turn",
                    )
                actor = self.make_actor(existing, self.resume_hint(existing))
                actor.put(Delivery(message["id"], pending["sender_id"], pending["sender"], prompt))
            else:
                await create_session(self, message, text, channel)

    async def recover_pending_creations(self, *, interrupt: bool = False) -> None:
        """Resume claimed first prompts whose session was never bound: a crash before the
        binding was committed, or Zulip unreachable at the card (issue #532).

        `interrupt`: re-raise a failure because Zulip did not answer, its payload kept, so
        that the caller (startup, the listener) handles no later message first: one in the
        same topic would create the session in the first prompt's place, without its
        options (codex review on #533).

        Issue #534: a payload whose sender can't be checked is kept, never dropped. A
        failed `refresh_users()` empties the directory, and the allowlist resolved from it, to
        fail closed, and a backend reconnect runs this recovery during a Zulip outage. One
        whose sender a successful refresh doesn't list is dropped, as main did. A payload
        older than the catch-up's limit (`MAX_AGE`) is not created: `expire_creation`."""
        log = logging.getLogger("zab.bridge")
        unbound = [
            p for p in self.store.pending_deliveries(self.bot.name) if p["session_id"] is None
        ]

        def failed(exc: Exception) -> bool:
            """Log a failure. Its payloads are kept: for the next backend recovery or
            startup, and, when Zulip did not answer, for the listener's next successful
            poll. Whether to re-raise it."""
            unreachable = zulip_unreachable(exc)
            if unreachable:
                self.creations_interrupted = True
            log.warning("Pending creation recovery: %s", type(exc).__name__)
            return unreachable and interrupt

        if unbound and (
            self.users_failed or any(p["sender_id"] not in self.users for p in unbound)
        ):
            try:
                await self.refresh_users()  # the senders, before they are judged
            except Exception as exc:
                if failed(exc):
                    raise
        for pending in unbound:
            mid = pending["message_id"]
            key = f"pending_delivery:{self.bot.name}:{mid}"
            pending = self.store.get(key)
            if pending is None or pending["session_id"] is not None:
                continue  # created, bound or dropped since the list was read
            message = pending["message"]
            if self.users_failed:
                # A directory refilled without the allowlist (`multi_party`) still fails
                # closed: kept until the sender can be checked.
                if mid not in self.unknown_senders:
                    self.unknown_senders.add(mid)
                    log.warning(
                        "Pending creation kept until its sender can be checked",
                        extra={"bot": self.bot.name},
                    )
                continue
            self.unknown_senders.discard(mid)
            user = self.users.get(pending["sender_id"])
            if user is None:  # not listed by the refresh that just succeeded
                log.warning(
                    "Pending creation dropped: its sender is not listed",
                    extra={"bot": self.bot.name},
                )
                self.store.delete(key)
                continue
            # Judged now, from the directory just checked: not after an await, when a
            # concurrent refresh may have failed and emptied it.
            allowed = pending["sender_id"] in self.router.allowlist and user.get("is_bot") is False
            try:
                channel = await self.channel(message["stream_id"])
                if not allowed or (
                    not self.private(channel) and not self.config.bridge.allow_public_channels
                ):
                    self.store.delete(key)
                    continue
                if message.get("timestamp", time.time()) < time.time() - MAX_AGE:
                    await self.expire_creation(key, message)
                    continue
                # Whether the topic has a session depends on confirmed card locations.
                await self.ensure_location(message["stream_id"], message["subject"])
                await self.create_pending(message, pending["text"], channel)
            except Exception as exc:
                if failed(exc):
                    raise

    async def expire_creation(self, key: str, message: dict[str, Any]) -> None:
        """A first prompt still pending past the catch-up's limit, its creation failing for
        a reason of its own, or never retried: it is not executed, as the catch-up executes
        no older request (§7.8.1). One ⚠️ reply names it, then its payload is dropped
        (issue #534). Under the creation lock, as a creation, and only while the payload is
        still unbound: a concurrent recovery that created or bound it, from a list read
        before it crossed the limit, wins, and no ⚠️ is posted for a prompt that runs (review
        of #537)."""
        async with waiting_lock(self.creation_lock):
            pending = self.store.get(key)
            if pending is None or pending["session_id"] is not None:
                return
            stream_id, mid = message["stream_id"], message["id"]
            url = f"{self.client.site}/#narrow/channel/{stream_id}/near/{mid}"
            text = self.note(CREATION_EXPIRED.format(url=url, hours=MAX_AGE // 3600))
            try:
                await self.poster.send(stream_id, message["subject"], text)
            except ZulipError as exc:
                if transient_zulip_error(exc):
                    raise  # kept: the reply is retried with the payload
                # A refusal (4xx) repeats on every retry.
                logging.getLogger("zab.bridge").warning(
                    "Expired creation's reply refused (Zulip %s, HTTP %s)",
                    exc.code,
                    exc.status,
                    extra={"bot": self.bot.name},
                )
            self.store.delete(key)

    async def reconcile_messages(self, *, catchup: Catchup, background: bool = False) -> None:
        """Topics of active sessions, through the startup or queue-loss catch-up: each from
        the session's newest inbound message when the catch-up began (`Catchup.anchors`),
        at its confirmed location. `background`: housekeeping's lane (`Poster.background`)."""
        call = self.poster.background if background else self.poster.call

        def skip(actor: SessionActor) -> bool:
            row = actor.row
            return row["state"] in ("detached", "readonly") or (
                not actor.turn and row["updated_at"] < time.time() - RECENT
            )

        for actor in sorted(self.actors.values(), key=lambda a: a.row["updated_at"], reverse=True):
            if skip(actor) or actor.row["id"] in catchup.done:
                continue
            await self.verify_card(actor.row["id"], background=background)
            if skip(actor):
                continue
            row = actor.row
            anchor = catchup.anchors.get(row["id"])
            if anchor is None:  # a session that the catch-up itself created
                anchor = max(
                    (m["message_id"] for m in self.store.rows("inbound", session_id=row["id"])),
                    default=0,
                )
            while True:
                try:
                    result = await call(
                        lambda anchor=anchor, row=row: self.client.request(
                            "GET",
                            "messages",
                            anchor=anchor,
                            num_before=0,
                            num_after=100,
                            apply_markdown=False,
                            narrow=[
                                {"operator": "channel", "operand": row["stream_id"]},
                                {"operator": "topic", "operand": row["topic"]},
                            ],
                        )
                    )
                except ZulipError as exc:
                    if transient_zulip_error(exc):
                        raise
                    # A refusal repeats on every retry: it must not hold up the other topics.
                    logging.getLogger("zab.bridge").warning(
                        "Topic reconciliation refused (Zulip %s, HTTP %s)",
                        exc.code,
                        exc.status,
                        extra={"bot": self.bot.name, "session": row["id"]},
                    )
                    break
                fresh = [m for m in result["messages"] if m["id"] > anchor]
                if not fresh:
                    break
                # After the replay, nothing past what it read (`Catchup.end`).
                ours = [m for m in fresh if catchup.end is None or m["id"] <= catchup.end]
                for message in ours:
                    await catchup.message(message, reconcile=True)
                anchor = max(m["id"] for m in fresh)
                if len(fresh) < 100 or len(ours) < len(fresh):
                    break
            catchup.done.add(row["id"])

    async def reconcile_reactions(self) -> None:
        for actor in list(self.actors.values()):
            if actor.row["state"] in ("readonly", "detached") or not actor.ready:
                continue
            approvals = self.store.rows("approvals", session_id=actor.row["id"], state="pending")
            mids = {a["message_id"] for a in approvals if a["message_id"]}
            if actor.turn and actor.turn["status_message_id"]:
                mids.add(actor.turn["status_message_id"])
            for mid in mids:
                try:
                    result = await self.poster.call(
                        lambda mid=mid: self.client.request(
                            "GET", f"messages/{mid}", apply_markdown=False
                        )
                    )
                except ZulipError as exc:
                    if exc.status in (400, 403, 404):
                        continue
                    raise
                for reaction in result["message"].get("reactions", []):
                    key = (
                        f"reaction:{self.bot.name}:{mid}:"
                        f"{reaction.get('user_id')}:{reaction.get('emoji_code')}"
                    )
                    if self.store.get(key):
                        continue
                    await self.handle(dict(reaction, type="reaction", op="add", message_id=mid))
                    await waiting(actor.inbox.join())
                    if actor.row["state"] != "error":
                        self.store.set(key, True)

    async def close(self) -> None:
        if self.listener:  # first: its recovery starts a housekeeping (`listen`)
            self.listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.listener
        await self.stop_housekeeping()
        await self.login.close()
        for task in list(self.background):  # pending forks a reopen started, `!btw` answers
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for actor in self.actors.values():
            await actor.close()
        await self.poster.close()
        await self.client.close()
        if self.metric_task:
            self.metric_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.metric_task
        self.client.metrics.emit()
