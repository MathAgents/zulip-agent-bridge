"""Bounded startup replay of the bot's combined feed (SPEC §7.8.1)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from zab.zulip.client import ZulipError

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge

EXPIRED = "This message arrived while your VM was off; send it again if it's still needed."
# Only requests this recent (seconds) are executed; older ones get a notice (SPEC §7.8.1).
MAX_AGE = 7200


class Catchup:
    def __init__(self, bridge: BotBridge, *, floor: int = 0) -> None:
        """`floor`: the newest message the running listener already handled. Event-queue
        recovery (§7.8.2) replays only after it: owner login DMs are never claimed, so
        the claim table alone would hand a finished `!login` to the login flow again."""
        self.bridge = bridge
        self.key = f"catchup_cursor:{bridge.bot.name}"
        self.initial_key = f"catchup_initial:{bridge.bot.name}"
        inbound = bridge.store.rows("inbound", bot=bridge.bot.name)
        # Capture before topic reconciliation can advance inbound past a new topic/DM.
        self.anchor = max((m["message_id"] for m in inbound), default=0)
        self.reconcile_key = f"catchup_reconcile:{bridge.bot.name}"
        self.before, self.anchors = self.reconcile_boundary(inbound)
        self.done: set[str] = set()  # sessions whose topics are reconciled
        # The newest message the replay read, once it ran. Reconciliation after the replay
        # claims nothing past it: later messages are the listener's, and a claim ahead of
        # it would lift the next catch-up's anchor over the DMs and new topics it has not
        # read yet (scenario 4 on #447: the anchor is the newest claim).
        self.end: int | None = None
        self.anchor = max(self.anchor, bridge.store.get(self.initial_key, 0))
        self.pending_key = f"catchup_pending:{bridge.bot.name}"
        self.anchor = max(
            bridge.store.get(self.pending_key, self.anchor), bridge.store.get(self.key, 0), floor
        )
        self.first_start = (
            not self.anchor
            and bridge.store.get(self.key) is None
            and bridge.store.get(self.pending_key) is None
            and bridge.store.get(self.initial_key) is None
        )
        # Retain the original boundary if startup crashes after topic reconciliation.
        if not self.first_start:
            bridge.store.set(self.pending_key, self.anchor)
        self.cutoff = time.time() - MAX_AGE
        self.notified: set[tuple] = set()

    def reconcile_boundary(self, inbound: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
        """What topic reconciliation starts from, captured before the replay and the
        listener can advance it: the cursor, and each session's newest inbound message.
        Reconciliation runs after the replay (SPEC §7.1); this boundary stays in the
        store until it is done, so that a restart in between reconciles from it again."""
        b = self.bridge
        cursor = b.store.get(self.key, 0)
        anchors = {
            row["id"]: 0
            for row in b.store.rows("sessions", bot=b.bot.name)
            if row["state"] != "detached"
        }
        for m in inbound:
            if m["session_id"] in anchors:
                anchors[m["session_id"]] = max(anchors[m["session_id"]], m["message_id"])
        if unfinished := b.store.get(self.reconcile_key):
            cursor = min(cursor, unfinished["before"])
            for sid, anchor in unfinished["anchors"].items():
                if sid in anchors:
                    anchors[sid] = min(anchors[sid], anchor)
        b.store.set(self.reconcile_key, {"before": cursor, "anchors": anchors})
        return cursor, anchors

    def reconciled(self) -> None:
        self.bridge.store.delete(self.reconcile_key)

    async def initialize(self) -> None:
        if not self.first_start:
            return
        b = self.bridge
        result = await b.poster.call(
            lambda: b.client.request(
                "GET",
                "messages",
                anchor="newest",
                num_before=1,
                num_after=0,
                apply_markdown=False,
            )
        )
        self.anchor = max((m["id"] for m in result["messages"]), default=0)
        # This boundary only skips historical replay. A live event may have been
        # queued after registration but before this lookup; do not suppress it.
        # Unlike interrupted reconciliation, this marker must not pin replay
        # behind messages handled normally during the first run.
        b.store.set(self.initial_key, self.anchor)

    async def message(self, message: dict[str, Any], *, reconcile: bool = False) -> None:
        """`reconcile`: found by topic reconciliation, which runs after the replay has
        advanced the cursor: it is measured against the cursor from before the replay."""
        from zab.core.bridge import transient_zulip_error

        cursor = self.before if reconcile else None
        try:
            if message.get("timestamp", 0) < self.cutoff:
                await self.expired(message, cursor=cursor)
            else:
                await self.bridge.handle({"type": "message", "message": message}, cursor=cursor)
        except ZulipError as exc:
            if transient_zulip_error(exc):
                raise
            logging.getLogger("zab.bridge").warning(
                "Skipping catch-up message after Zulip %s (HTTP %s)", exc.code, exc.status
            )
        except (httpx.HTTPError, OSError):
            raise
        except Exception as exc:
            # As in listen(), a poison message must not leave the whole bot deaf.
            # Login inputs and exception text never go to the log.
            logging.getLogger("zab.bridge").warning(
                "Skipping catch-up message after %s", type(exc).__name__
            )

    async def expired(self, message: dict[str, Any], *, cursor: int | None = None) -> None:
        b = self.bridge
        uid = message["sender_id"]
        if (
            uid not in b.router.allowlist
            or b.users.get(uid, {}).get("is_bot") is not False
            or message["id"] <= (b.store.get(self.key, 0) if cursor is None else cursor)
        ):
            return
        if message["type"] == "private":
            if {r["id"] for r in message.get("display_recipient", [])} != {
                uid,
                b.router.user_id,
            }:
                return
            scope = ("dm", uid)
        else:
            channel = await b.channel(message["stream_id"])
            if not b.private(channel) and not b.config.bridge.allow_public_channels:
                return
            scope = ("topic", message["stream_id"], message["subject"])
        if scope[0] == "dm":
            if not b.store.claim(b.bot.name, message["id"]) or b.sentinel_answers(message):
                return  # `zab sentinel` answered it, while the bridge was down (#204)
        else:
            if b.store.rows("inbound", bot=b.bot.name, message_id=message["id"]):
                return
            # Whether the old input was this bot's (§4.5), without persisting or executing it.
            route = await b.route_message(
                message,
                b.users[uid],
                channel,
                flags=message.get("flags", []),
                history_only=True,
            )
            if route.action != "expired":
                return
        if scope in self.notified:
            return
        self.notified.add(scope)
        if scope[0] == "dm":
            await b.login.send(uid, b.note(EXPIRED))
        else:
            await b.poster.send(message["stream_id"], message["subject"], b.note(EXPIRED))

    async def replay(self) -> int:
        """Handle the feed after the anchor; returns how many messages it read."""
        if self.first_start:
            self.end = self.anchor
            return 0
        b = self.bridge
        anchor = self.anchor
        count = 0
        while True:
            result = await b.poster.call(
                lambda anchor=anchor: b.client.request(
                    "GET",
                    "messages",
                    anchor=anchor,
                    num_before=0,
                    num_after=100,
                    apply_markdown=False,
                )
            )
            fresh = [m for m in result["messages"] if m["id"] > anchor]
            for message in fresh:
                await self.message(message)
                anchor = message["id"]
                count += 1
                # IDs only: login DM contents must never be persisted. The cursor
                # also suppresses queued live duplicates after a login has finished.
                b.store.set(self.key, anchor)
            if len(fresh) < 100 or result.get("found_newest"):
                # Even an empty replay establishes a boundary for the next restart.
                b.store.set(self.key, anchor)
                b.store.delete(self.pending_key)
                self.end = anchor
                return count
