"""Who gets a channel message that mentions no bot (SPEC §4.5, #878): the VM's rule, and each
topic's addressee record, which the rule `last` reads.

The store is the bridge process's, so the rule and each record are the VM's, one for all its
bots: each bot finds the same addressees for a message. A record keeps, for each of a topic's
prompts that named any of the VM's bots, which ones it named (a command names none: whom the
participant talks to, not whom they send a command); a message without a mention goes, under
`last`, to those of the newest such prompt before it. Before it, not the newest of all:
the VM's bots read their queues each at its own pace (a catch-up replays one bot's after the
other's), and a bot that is behind still finds the record that the message followed. So an entry
stays while a bot may still read a message it precedes (`RETAIN_S`)."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from zab.core.store import Store

#: `all`: a message without a mention goes to each of the VM's bots (#713). `last`: to the bots
#: the topic's newest message with a mention named, and to all where none has been named yet.
RULES = ("all", "last")
#: The rule `!unmentioned` set, for the VM; without one, `bridge.unmentioned` applies.
RULE_KEY = "unmentioned"
#: An entry stays until the entry after it was first recorded this long ago: the messages
#: between the two, which it decides, come before the second in every bot's queue, and the bot
#: that reads them last reads them within a catch-up's replay of the others', minutes after the
#: first. Not a count: with the newest 100 kept, a hundred mentions in that replay dropped the
#: entry that the last bot's next message needed.
RETAIN_S = 86400.0
USAGE = "Usage: !unmentioned, !unmentioned all or !unmentioned last."
EXPLAIN = {
    "all": (
        "A message in a topic that mentions no agent goes to all of your agents (`all`); "
        "`!unmentioned last` sends it to the ones the topic's last mention named."
    ),
    "last": (
        "A message in a topic that mentions no agent goes to the ones the topic's last mention "
        "named, or to all of your agents where none has been named (`last`); `!unmentioned all` "
        "sends it to all."
    ),
}


def rule(store: Store, default: str) -> str:
    """The rule in force: the one `!unmentioned` set, or `default` (`bridge.unmentioned`)."""
    value = store.get(RULE_KEY)
    return value if value in RULES else default


#: Either rule's place with `bridge.multi_party` off, whose own rule then applies (§4.5).
INACTIVE = (
    "{}, but it has no effect while `bridge.multi_party` is off: a message in a topic that "
    "mentions no agent goes to the ones with a session there."
)


def command(store: Store, default: str, text: str, active: bool = True) -> str | None:
    """The answer to a direct message `text` (a command as people type it) that is
    `!unmentioned`, after setting the rule it names; None for any other message. `active`:
    `bridge.multi_party`, without which neither rule applies, as the answer says."""
    words = text.split()
    if not words or words[0] != "!unmentioned":
        return None
    if len(words) > 2 or (len(words) == 2 and words[1].casefold() not in RULES):
        return USAGE
    if len(words) == 2:
        store.set(RULE_KEY, words[1].casefold())
    value = rule(store, default)
    if not active:
        return INACTIVE.format(f"Set to `{value}`" if len(words) == 2 else f"`{value}` is set")
    return ("Done. " if len(words) == 2 else "") + EXPLAIN[value]


def card_gone(session_id: str) -> str:
    """The store key marking a session that a check of its card detached, the card gone: a
    detach that no message records, unlike a `!detach`, which amends the record by its position
    (§4.5). `BotBridge.addressees` reads it as the session's state now, not as of a message."""
    return f"card_gone:{session_id}"


def _key(stream: Any, topic: str) -> str:
    return f"topic_addressees:{stream}:{topic}"


def record(
    store: Store,
    stream: Any,
    topic: str,
    message_id: int,
    names: Iterable[str],
    *,
    now: float | None = None,
) -> None:
    """Record that message `message_id` named the VM's bots `names` in the topic, at `now` (the
    wall clock's). Another record of the same message adds its names and keeps the first time:
    each bot that reads the message records it, and each bot that forked on one `!fork` message
    records itself in the fork's topic (§7.11). Entries no bot needs any more go (`RETAIN_S`)."""
    now = time.time() if now is None else now
    key = _key(stream, topic)
    with store.transaction():
        entries = {mid: (set(named), at) for mid, named, at in store.get(key, [])}
        named, at = entries.get(message_id, (set(), now))
        entries[message_id] = (named | set(names), at)
        ordered = sorted(entries.items())
        kept = [
            [mid, sorted(named), at]
            for (mid, (named, at)), after in zip(ordered, [*ordered[1:], None], strict=True)
            if after is None or after[1][1] > now - RETAIN_S
        ]
        store.set(key, kept)


def addressees(store: Store, stream: Any, topic: str, before: int | None = None) -> tuple[str, ...]:
    """The bots the topic's newest prompt with a mention named, before message `before` (any
    message when None): none where no prompt has named any."""
    found = [
        named
        for mid, named, _ in store.get(_key(stream, topic), [])
        if before is None or mid < before
    ]
    return tuple(found[-1]) if found else ()


def follow(store: Store, source: tuple[Any, str], destination: tuple[Any, str]) -> None:
    """A session card moved from `source` to `destination`: the source's newest entry joins the
    destination's record, where the newer message wins (`addressees`), as before #713."""
    entries = store.get(_key(*source), [])
    if entries and source != destination:  # recorded there now: its own entries stay a day
        record(store, *destination, *entries[-1][:2])
