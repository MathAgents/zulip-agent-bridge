"""Ordered, per-bot Zulip routing with durable deduplication (SPEC §4.5)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from zab.core.addressing import (
    KINDS,
    TYPED_ADDRESSES,
    mention,
    typed_kinds,
    unaddressed,
    visible_text,
    without_mention,
    without_typed_address,
)
from zab.core.command_text import as_command
from zab.core.commands import BTW_NO_SESSION, FORK_NO_SESSION, HANDOVER
from zab.core.store import Store

#: Rule 7's reply to a command other than `!new` in a topic without a session of this bot.
NO_SESSION = "No session here: write a prompt to start one; !help lists the commands."
#: The commands whose answer is the VM's, the same from each bot: one bot gives it (§4.6).
VM_WIDE = ("!help", "!vm", "!tools")
#: The answer to an unmentioned `!handover` where both agents have a session (§4.6).
HANDOVER_NAMED = (
    "Both agents have a session here, and would write HANDOVER.md in the same worktree: name "
    "one, `@claude !handover` or `@codex !handover`."
)


@dataclass(frozen=True)
class Route:
    """`quiet`: the message reached this bot without naming it, and other bots of the VM too
    (SPEC §4.5 rule 6.2, #713): a command answers only where it acts (§4.6)."""

    action: str
    text: str = ""
    session: dict[str, Any] | None = None
    peers: tuple[str, ...] = ()
    quiet: bool = False


class Router:
    def __init__(
        self,
        store: Store,
        bot: str,
        user_id: int,
        full_name: str,
        allowlist: frozenset[int],
        allow_public: bool = False,
        kind: str = "",
    ) -> None:
        """`kind`: the bot's backend kind, which a typed mention names (`@claude`, `@codex`,
        #713); none for any other."""
        self.store = store
        self.bot = bot
        self.user_id = user_id
        self.allowlist = allowlist
        self.allow_public = allow_public
        self.full_name = full_name
        self.mention = mention(full_name, user_id)
        self.kind = kind if kind in KINDS else ""

    def unmentioned(self, text: str, other_bot_names: Iterable[str] = ()) -> bool:
        """Whether a channel message `text` names no bot, completed or typed, and is no aside:
        one that rule 6.2 sends to all of the VM's bots (SPEC §4.5, #713). Only for such a
        message does the bridge work out the router's `siblings`, asking for sign-ins."""
        visible = visible_text(text)
        return not (
            typed_kinds(visible)
            or self.mention.search(visible)
            or any(mention(name).search(visible) for name in other_bot_names)
            or text.lstrip().startswith("//")
        )

    def route(
        self,
        message: dict[str, Any],
        *,
        foreign_kind_mentions: set[tuple[str, int]] | None = None,
        foreign_peer_replacements: dict[tuple[str, int], str | None] | None = None,
        flags: list[str],
        sender_is_bot: bool,
        private: bool,
        other_bot_names: set[str] | None = None,
        multi_party: bool = False,
        sender_name: str = "user",
        history_only: bool = False,
        siblings: tuple[str, ...] | None = None,
        sibling_kinds: dict[str, str] | None = None,
    ) -> Route:
        """`siblings`: the full names of the VM's other bots, which a message without a mention
        also goes to (rule 6.2); None where it doesn't go to this bot (`BotBridge.siblings`: a
        multi-party channel, `multi_party` off without a session here, its agent signed out).
        `sibling_kinds`: the VM's other bots that allow the sender, by full name, with their
        kinds, which a typed mention names (#713)."""
        with self.store.transaction():
            route = self._route(
                message,
                foreign_kind_mentions=foreign_kind_mentions,
                foreign_peer_replacements=foreign_peer_replacements,
                flags=flags,
                sender_is_bot=sender_is_bot,
                private=private,
                other_bot_names=other_bot_names,
                multi_party=multi_party,
                history_only=history_only,
                siblings=siblings,
                sibling_kinds=sibling_kinds,
            )
            if route.action in ("deliver", "new") or (
                route.action == "command" and route.session and route.text == "!handover"
            ):
                if route.peers:
                    self.store.set(f"addressed_peers:{self.bot}:{message['id']}", route.peers)
                self.store.set(
                    f"pending_delivery:{self.bot}:{message['id']}",
                    dict(
                        message_id=message["id"],
                        sender_id=message["sender_id"],
                        sender=sender_name,
                        text=HANDOVER if route.text == "!handover" else route.text,
                        session_id=route.session["id"] if route.session else None,
                        message=message if route.action == "new" else None,
                    ),
                )
            return route

    def _route(
        self,
        message: dict[str, Any],
        *,
        foreign_kind_mentions: set[tuple[str, int]] | None = None,
        foreign_peer_replacements: dict[tuple[str, int], str | None] | None = None,
        flags: list[str],
        sender_is_bot: bool,
        private: bool,
        other_bot_names: set[str] | None,
        multi_party: bool,
        history_only: bool = False,
        siblings: tuple[str, ...] | None = None,
        sibling_kinds: dict[str, str] | None = None,
    ) -> Route:
        stream = message.get("stream_id")
        topic = message.get("subject", "")
        sessions = self.store.rows("sessions", bot=self.bot, stream_id=stream, topic=topic)
        session = next((s for s in sessions if s["state"] != "detached"), None)
        if not self.store.claim(self.bot, message["id"], session["id"] if session else None):
            return Route("ignore")
        text = message["content"]
        sender = message["sender_id"]
        if (
            sender_is_bot
            and sender != self.user_id
            and session
            and text.startswith("**zab session**")
        ):
            self.store.update("sessions", {"id": session["id"]}, shared=1)
        if sender == self.user_id or sender_is_bot or sender not in self.allowlist:
            return Route("ignore")
        if message["type"] != "stream":
            # `@claude !help` (#713), then `!Help`, "`!help`", `@**Bot** !help` (#709)
            dm = as_command(without_typed_address(text, self.kind), self.mention)
            return Route("dm", dm)
        visible = visible_text(text)
        # A typed `@claude` or `@codex` names the sender's bot of that kind, without Zulip's
        # `mentioned` flag, which it doesn't set for one (#713): this bot, or the VM's others.
        kinds = typed_kinds(visible)
        mentioned = self.kind in kinds or (
            "mentioned" in flags and self.mention.search(visible) is not None
        )
        other_mentions = {name: mention(name) for name in (other_bot_names or set())}
        others = tuple(
            sorted(
                {name for name, pattern in other_mentions.items() if pattern.search(visible)}
                | {name for name, kind in (sibling_kinds or {}).items() if kind in kinds}
            )
        )
        explicitly_addressed = bool(mentioned or others or kinds - {self.kind})
        normalized, addresses = unaddressed(
            text, [self.mention, *other_mentions.values(), *TYPED_ADDRESSES]
        )
        normalized = as_command(normalized)  # `!Stop`, "`!stop`" (#709); a prompt as it was
        foreign_mentions = [
            ((name, user_id), mention(name, user_id))
            for name, user_id in set(foreign_kind_mentions or ())
            | set(foreign_peer_replacements or ())
        ]
        corrected = {
            identity
            for address in addresses
            for identity, pattern in foreign_mentions
            if pattern.match(address)
        }
        if not self.mention.search(visible) and corrected.intersection(foreign_kind_mentions or ()):
            mentioned = True
        corrected_names = {name for name, _ in corrected}
        replacements = foreign_peer_replacements or {}
        others = tuple(
            sorted(
                (set(others) - corrected_names)
                | {replacements[key] for key in corrected if replacements.get(key)}
            )
        )
        if not private and not self.allow_public:
            return (
                self._once(stream, topic, "public", "sessions are disabled in public channels")
                if mentioned
                else Route("ignore")
            )
        if normalized.startswith("//"):
            return Route("ignore")
        quiet = False
        if (
            siblings is not None
            and (session or not sessions)  # after `!detach`, a mention is needed (§4.6)
            and not (multi_party or explicitly_addressed or self.mention.search(visible))
        ):
            # A message that names no bot goes to each of the VM's bots, whatever earlier
            # messages in the topic addressed, as if it mentioned them all (SPEC §4.5, #713).
            mentioned, others, quiet = True, tuple(sorted(siblings)), bool(siblings)
        peers = others if mentioned or not multi_party else ()
        self.store.set(
            f"message_audience:{self.bot}:{message['id']}",
            dict(addressed=mentioned, peers=peers, stream_id=stream, topic=topic),
        )
        if not mentioned:
            return Route("ignore")
        if history_only:
            deliverable = not session or session["state"] != "readonly"
            return Route("expired" if deliverable else "ignore")
        command = normalized.startswith("!")
        verb = normalized.split(maxsplit=1)[0] if command else ""
        lead = not quiet or self.full_name < min(peers)
        if verb in VM_WIDE and not lead:
            return Route("ignore")  # the first of the VM's bots answers it, by name
        if verb == "!login":
            return Route("reply", "Send !login to me in a direct message.")
        if session:
            if session["state"] in ("readonly", "detached"):
                return Route("ignore")
            if quiet and verb == "!handover" and self._others_here(stream, topic):
                return Route("reply", HANDOVER_NAMED) if lead else Route("ignore")
            action = "command" if command else "deliver"
            text = without_mention(normalized, self.mention).strip()
            return Route(action, text, session, peers, quiet)
        if not topic:
            return self._once(stream, topic, "empty", "Please use a named topic for a session.")
        if verb in ("!help", "!attach", "!vm", "!tools"):
            return Route("command", normalized, quiet=quiet)
        if command and verb != "!new":
            # Unmentioned, it is the VM's: another bot's session here answers it, and else
            # one bot says that none has one (§4.6).
            if quiet and (not lead or self._others_here(stream, topic)):
                return Route("ignore")
            replies = {"!fork": FORK_NO_SESSION, "!btw": BTW_NO_SESSION}  # §7.11, §7.13
            return Route("reply", replies.get(verb, NO_SESSION))
        return Route("new", without_mention(normalized, self.mention).strip(), peers=peers)

    def _others_here(self, stream: int | None, topic: str) -> bool:
        """Whether another bot of the VM (the store's) has a session in the topic."""
        return any(
            row["bot"] != self.bot and row["state"] != "detached"
            for row in self.store.rows("sessions", stream_id=stream, topic=topic)
        )

    def _once(self, stream: int, topic: str, reason: str, text: str) -> Route:
        key = f"refusal:{self.bot}:{stream}:{topic}:{reason}"
        if self.store.get(key):
            return Route("ignore")
        self.store.set(key, True)
        return Route("note", text)  # a reply to a plain mention: posted marked (#677)

    def move_card(self, event: dict[str, Any], *, private: bool) -> list[dict[str, Any]]:
        """Card location is authoritative, including partial topic moves (§4.4)."""
        ids = set(event.get("message_ids", []))
        if "message_id" in event:
            ids.add(event["message_id"])
        changed = []
        for session in self.store.rows("sessions", bot=self.bot):
            if session["card_message_id"] not in ids or session["state"] == "detached":
                continue
            values = {}
            if "new_stream_id" in event:
                values["stream_id"] = event["new_stream_id"]
            if "subject" in event:
                values["topic"] = event["subject"]
            if not private and not self.allow_public:
                values["state"] = "readonly"
            elif (private or self.allow_public) and session["state"] == "readonly":
                running = self.store.rows("turns", session_id=session["id"], state="running")
                pending = self.store.rows("approvals", session_id=session["id"], state="pending")
                values["state"] = (
                    "awaiting_approval" if pending else "running" if running else "idle"
                )
            if values:
                destination = session | values
                occupied = any(
                    row["id"] != session["id"] and row["state"] != "detached"
                    for row in self.store.rows(
                        "sessions",
                        bot=self.bot,
                        stream_id=destination["stream_id"],
                        topic=destination["topic"],
                    )
                )
                if occupied:
                    # The existing binding wins; never keep routing via a stale location.
                    values["state"] = "detached"
                self.store.update("sessions", {"id": session["id"]}, **values)
                changed.append(session | values)
        return changed
