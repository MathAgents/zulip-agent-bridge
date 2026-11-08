"""Ordered, per-bot Zulip routing with durable deduplication (SPEC §4.5)."""

from __future__ import annotations

import re
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
from zab.core.commands import BTW_NO_SESSION, DETACH, FORK_NO_SESSION, HANDOVER
from zab.core.store import Store
from zab.core.unmentioned import addressees as recorded
from zab.core.unmentioned import follow, record

#: Rule 7's reply to a command other than `!new` in a topic without a session of this bot.
NO_SESSION = "No session here: write a prompt to start one; !help lists the commands."
#: The commands whose answer is the VM's, the same from each bot: one bot gives it (§4.6).
VM_WIDE = ("!help", "!vm", "!tools")
#: A topic's `!unmentioned`, whose pointer to a DM no session changes either (#878).
UNMENTIONED = "!unmentioned"
#: The answer to an unmentioned `!handover` where both agents have a session (§4.6).
HANDOVER_NAMED = (
    "Both agents have a session here, and would write HANDOVER.md in the same worktree: name "
    "one, `@claude !handover` or `@codex !handover`."
)
#: A topic's `!unmentioned`: the VM's setting, which a direct message changes (§4.6, #878).
UNMENTIONED_DM = "Send !unmentioned to me in a direct message: it is for all of your agents."


def actor_text(normalized: str, pattern: re.Pattern[str]) -> str:
    """A topic message's text as the actor of the bot whose mention is `pattern` reads it
    (`Route.text`): `normalized` without that bot's mentions."""
    return without_mention(normalized, pattern).strip()


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
        one that rule 6.2 sends to all of the VM's bots (SPEC §4.5, #713), or under `last` to
        the recorded ones (#878). Only for such a message does the bridge work out the router's
        `addressees` and `siblings`, asking for sign-ins."""
        visible = visible_text(text)
        return not (
            typed_kinds(visible)
            or self.mention.search(visible)
            or any(mention(name).search(visible) for name in other_bot_names)
            or text.lstrip().startswith("//")
        )

    def detaching(self, text: str, other_bot_names: Iterable[str] = ()) -> bool:
        """Whether a channel message `text` is a `!detach` by its verb, which amends the topic's
        record from the entry before it (`_record`): the bridge first has the other bots confirm
        their cards in the topic, as before a message without a mention under `last` (#891)."""
        patterns = [self.mention, *(mention(name) for name in other_bot_names), *TYPED_ADDRESSES]
        normalized, _ = unaddressed(text, patterns)
        return as_command(normalized).split(maxsplit=1)[:1] == [DETACH]

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
        addressees: tuple[str, ...] | None = None,
        answerers: tuple[str, ...] | None = None,
    ) -> Route:
        """`siblings`: the full names of the VM's other bots, which a message without a mention
        also goes to (rule 6.2); None where it doesn't go to this bot (`BotBridge.siblings`: a
        multi-party channel, `multi_party` off without a session here, its agent signed out, a
        bot the topic's addressee record leaves out). `sibling_kinds`: the VM's other bots that
        allow the sender, by full name, with their kinds, which a typed mention names (#713).
        `addressees`: under the rule `last`, the VM's bots that the record names, which a
        message without a mention goes to (`BotBridge.addressees`, #878); None under `all` or
        without a record. `answerers`: for a message without a mention, the VM's bots, by full
        name, that it reaches by their allowlists and the channel, whatever their sessions
        (`BotBridge.answerers`, #899): the first by name answers a command whose answer is the
        VM's; None with `bridge.multi_party` off, where `siblings` decides who does."""
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
                addressees=addressees,
                answerers=answerers,
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
        addressees: tuple[str, ...] | None = None,
        answerers: tuple[str, ...] | None = None,
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
        unnamed = not (multi_party or explicitly_addressed or self.mention.search(visible))
        self._record(message["id"], stream, topic, normalized, mentioned, others, unnamed,
                     sibling_kinds, addressees)  # fmt: skip
        if not private and not self.allow_public:
            return (
                self._once(stream, topic, "public", "sessions are disabled in public channels")
                if mentioned
                else Route("ignore")
            )
        if normalized.startswith("//"):
            return Route("ignore")
        quiet = False
        if unnamed and addressees is not None and self.full_name not in addressees:
            # Under `last`, it is the recorded bots', as a mention of them (#878).
            others = tuple(sorted(addressees))
        elif siblings is not None and (session or not sessions) and unnamed:
            # A message that names no bot goes to each of the VM's bots it reaches (#713), or
            # under `last` to those the topic's record names (#878), as if it mentioned them
            # all (SPEC §4.5): `siblings`, from the bridge, says which. After `!detach`, a
            # mention is needed (§4.6).
            mentioned, others, quiet = True, tuple(sorted(siblings)), bool(siblings)
        peers = others if mentioned or not multi_party else ()
        self.store.set(
            f"message_audience:{self.bot}:{message['id']}",
            dict(addressed=mentioned, peers=peers, stream_id=stream, topic=topic),
        )
        command = normalized.startswith("!")
        verb = normalized.split(maxsplit=1)[0] if command else ""
        if (
            unnamed
            and answerers is not None
            and verb in (*VM_WIDE, UNMENTIONED)
            and topic
            and not history_only
        ):
            # The VM's answer, from the first by name of the bots that the message reaches by
            # their allowlists and the channel (`answerers`), whatever their sessions, so that each
            # bot finds the same one in whichever order they read it and a `!detach` (#899). A
            # topic without a name, where no session is, and an expired message, as before.
            if self.full_name != min(answerers, default=None):
                return Route("ignore")
            return (
                Route("reply", UNMENTIONED_DM)
                if verb == UNMENTIONED
                else Route("command", normalized)
            )
        if not mentioned:
            return Route("ignore")
        if history_only:
            deliverable = not session or session["state"] != "readonly"
            return Route("expired" if deliverable else "ignore")
        lead = not quiet or self.full_name < min(peers)
        if verb in VM_WIDE and not lead:  # without `answerers`: `bridge.multi_party` off
            return Route("ignore")  # the first of the VM's bots answers it, by name
        if verb == UNMENTIONED:
            return Route("reply", UNMENTIONED_DM) if lead else Route("ignore")
        if verb == "!login":
            return Route("reply", "Send !login to me in a direct message.")
        if session:
            if session["state"] in ("readonly", "detached"):
                return Route("ignore")
            if quiet and verb == "!handover" and self._others_here(stream, topic):
                return Route("reply", HANDOVER_NAMED) if lead else Route("ignore")
            action = "command" if command else "deliver"
            text = actor_text(normalized, self.mention)
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
        return Route("new", actor_text(normalized, self.mention), peers=peers)

    def _record(
        self,
        message_id: int,
        stream: Any,
        topic: str,
        normalized: str,
        mentioned: bool,
        others: tuple[str, ...],
        unnamed: bool,
        sibling_kinds: dict[str, str] | None,
        addressees: tuple[str, ...] | None,
    ) -> None:
        """The topic's addressee record, for the rule `last` (#878), from what each of the VM's
        bots reads the same in the message, whichever reads it first: its text and position.
        Before the public-channel rule, whose channel state may change between two bots' reads
        (a public topic's routing reads no record), and an expired message's too.

        A prompt (neither a command nor an aside; `!new` included) records the VM's bots it
        names: whom the participant talks to, not whom they send a command. A `!detach` is the
        one command that amends it, by removal: under its own id, the newest entry before it
        without the bots it detaches, so that every bot reads the same after it, in any order.
        Those are the bots it reaches (named, in a multi-party channel too; without a mention, in
        a single-human one, the recorded ones or else all) for which it is exactly `!detach`
        without their completed mentions (`actor_text`), as their actors read it, or would, a bot
        without a session included: `@claude !detach now` detaches nothing, and amends nothing.
        A mention of the bot's name with another user's ID counts as the bot's here, though not
        for its actor (Zulip writes the ID only for two users of one name)."""
        verb = normalized.split(maxsplit=1)[0] if normalized.startswith("!") else ""
        named = ({self.full_name} if mentioned else set()) | (
            set(others) & set(sibling_kinds or ())
        )
        if normalized.startswith("//"):
            return  # an aside
        if named and verb in ("", "!new"):
            record(self.store, stream, topic, message_id, named)
        elif verb == DETACH:
            everyone = (self.full_name, *(sibling_kinds or ()))
            reached = named or (set(addressees or everyone) if unnamed else set())
            reached = {name for name in reached if actor_text(normalized, mention(name)) == DETACH}
            if reached and (before := recorded(self.store, stream, topic, message_id)):
                record(self.store, stream, topic, message_id, set(before) - reached)

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

    def follow_cards(self, event: dict[str, Any]) -> None:
        """A move of messages that takes a session card of any of the VM's bots takes the
        card's topic's addressee record with it (`unmentioned.follow`, SPEC §4.5, #878). Each
        bot does so on the move's event, from the move's source (Zulip's `stream_id` and
        `orig_subject`, as the card's row may have moved already), so that the record is there
        for whichever bot reads the destination's next message first."""
        ids = set(event.get("message_ids", [])) | {event.get("message_id")}
        for row in self.store.rows("sessions"):
            if row["card_message_id"] in ids and row["state"] != "detached":
                source = (
                    event.get("stream_id", row["stream_id"]),
                    event.get("orig_subject", row["topic"]),
                )
                destination = (
                    event.get("new_stream_id", source[0]),
                    event.get("subject", source[1]),
                )
                follow(self.store, source, destination)

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
                # The record goes with the card, a move found by a card's check included (#878)
                source = (session["stream_id"], session["topic"])
                follow(self.store, source, (destination["stream_id"], destination["topic"]))
                self.store.update("sessions", {"id": session["id"]}, **values)
                changed.append(session | values)
        return changed
