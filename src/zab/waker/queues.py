"""Where the waker's messages come from (docs/ADMIN.md §11.4).

Today one Zulip event queue per opted-in bot, all long-polled by one process: 36 queues for the
18 kept VMs, and a risk at about 2,000 bots (§11.4, V14). Whatever replaces it (outgoing
webhooks, one queue per workshop) needs to change only this module: the waker takes every
message through `deliver(target, message, flags)`, each bot's copy with that bot's queue flags
(`mentioned` picks the bot that answers, SPEC §13.4).

**Catch-up** (#403). A queue sees only what is sent after its registration, so a waker that
restarts (each config change, a deploy, a reboot) or registers again (Zulip dropped the queue)
would never see what was sent in between. At each registration the waker says (`since`) where
the bot's catch-up starts: after a message ID, or from a time. Then the bot's messages since
then, none sent more than `CATCH_UP_AGE` seconds ago, go through `deliver`, oldest first, as live
ones do. Registration comes first, so nothing falls between the two; a message both fetched and
queued is delivered once. A fetch that fails is tried again between the queue's polls,
`CATCH_UP_TRIES` times in all, `CATCH_UP_RETRY` seconds apart and doubling; then the waker is
told (`settled`), and until then it holds the bot's mark.

**A page at a time** (#717). Fetching each bot's newest `CATCH_UP` messages, all bots at once,
took the waker past 1 GB at a start after two workshop days (Nov 1): Zulip returns each message
with its whole edit history, every earlier version and its HTML, and the bridge edited a turn's
status message every 10 seconds before #754. So the catch-up (`recent`):
- **asks nothing** when the registration's `max_message_id`, the newest message the bot has
  received, is its mark;
- **walks back** from the newest message, `PAGE` at a time, to its mark, or to the first message
  sent before its floor (`CATCH_UP_AGE` ago, or the time it starts from, without a usable mark:
  none, or one past `max_message_id`), the newest `CATCH_UP` at most;
- **keeps of each page** only what it may deliver, and of each such message only what the waker
  reads (`kept`: `KEPT`, no text, no edit history, and the kinds its typed mentions name,
  `typed`, read from the text before it goes), and lets the page go before it asks for the
  next; then delivers them, oldest first;
- **never fetches the VM's own bots' messages** (`Senders`): the owner check refuses a bot, so
  they wake nothing, and they are the heavy ones. A negated `sender` narrow leaves them out; if
  Zulip refuses it (`BAD_NARROW`), the catch-up asks without it and leaves them out itself.

**A few at a time** (#717). The registrations and catch-ups of every bot of the process, every
workshop's, take turns: at most `AT_ONCE` run at once (`gate`), each catch-up's deliveries
included, each turn, registration and catch-up together, bounded by `TURN` seconds, so that a
request that hangs holds a turn no longer.
Each turn's time is logged, and each workshop's whole start's.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from zab.core.addressing import typed_kinds, visible_text
from zab.zulip.client import ZulipError

if TYPE_CHECKING:
    from zab.waker.service import Target

LOG = logging.getLogger("zab.waker")
Deliver = Callable[["Target", dict, list[str]], Awaitable[None]]
#: Where a bot's catch-up starts: after a message ID, else from a time; (None, None): none.
Where = tuple[int | None, float | None]
Since = Callable[["Target"], Where]
Settled = Callable[["Target"], None]
#: The catch-up's bounds: messages per bot (no more than the waker holds per VM,
#: `service.HOLD`), and seconds back. The wake monitor matches a start to a message sent up to
#: 10 minutes before it (`zab.admin.wakes.WINDOW`): a start for a message 5 minutes old is
#: matched, after the waker's two capacity retries too, unless the wake first waited for a VM
#: still stopping (up to 5 more minutes).
CATCH_UP, CATCH_UP_AGE = 100, 300.0
CATCH_UP_TRIES, CATCH_UP_RETRY = 4, 5.0
#: Messages per request of a catch-up (#717): what one bot holds of Zulip's answer at a time.
PAGE = 10
#: What a caught-up message keeps: what the waker reads of it (`service.Waker.consider`), and
#: `typed` (`kept`).
KEPT = ("id", "sender_id", "type", "stream_id", "subject", "timestamp", "flags")
#: Registrations and catch-ups at once, in the whole process, and seconds each may take (#717).
AT_ONCE, TURN = 8, 30.0
_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def kept(message: dict) -> dict:
    """What a catch-up keeps of `message`: its `KEPT` fields, and `typed`, the kinds that its
    typed mentions name (`@claude`, `@codex`, which Zulip doesn't flag: `service.named`, #713),
    sorted, read from its text, which goes. At most one short string per kind (`KINDS`, two) a
    message, whatever its text (Codex round 6 on #727, after #713)."""
    out = {key: message[key] for key in KEPT if key in message}
    text = message.get("content")
    named = isinstance(text, str) and "@" in text  # no `@`, no typed mention: no scan
    out["typed"] = sorted(typed_kinds(visible_text(text))) if named else []
    return out


def gate() -> asyncio.Semaphore:
    """The process's turns (module doc): one semaphore per event loop, shared by every
    workshop's bots."""
    loop = asyncio.get_running_loop()
    if (turns := _GATES.get(loop)) is None:
        turns = _GATES[loop] = asyncio.Semaphore(AT_ONCE)
    return turns


class Senders:
    """Whose messages a catch-up leaves out (module doc), by address: Zulip, through a negated
    `sender` narrow, until it refuses one (`narrow` false from then on); else the catch-up."""

    def __init__(self, emails: Iterable[str] = ()) -> None:
        self.emails = sorted(set(emails))
        self.narrow = bool(self.emails)

    def keeps(self, message: dict) -> bool:
        return message.get("sender_email") not in self.emails


class BotQueues:
    """One event queue per bot, registered again when Zulip drops it (#295)."""

    def __init__(
        self, targets: list[Target], since: Since | None = None, settled: Settled | None = None
    ) -> None:
        self.targets, self.since, self.settled = targets, since, settled
        self.began, self.starting = 0.0, set()  # since `run`: the bots not settled yet

    def senders(self, target: Target) -> list[str]:
        """Whose messages `target`'s catch-up leaves out: its VM's bots, itself included."""
        vm = [t for t in self.targets if t.instance == target.instance]
        return sorted({e for t in vm if isinstance(e := getattr(t.client, "email", None), str)})

    def done(self, target: Target) -> None:
        """`settled`, and once every bot has settled since `run`, the start's time."""
        if self.settled is not None:
            self.settled(target)
        if target.bot.name in self.starting:
            self.starting.discard(target.bot.name)
            if not self.starting:
                took = time.monotonic() - self.began
                LOG.info("Waker start: %d bots caught up in %.1f s", len(self.targets), took)

    async def listen(self, target: Target, deliver: Deliver) -> None:
        await listen(target, deliver, self.since, self.done, Senders(self.senders(target)))

    async def run(self, deliver: Deliver) -> None:
        self.began, self.starting = time.monotonic(), {t.bot.name for t in self.targets}
        async with asyncio.TaskGroup() as group:
            for target in self.targets:
                group.create_task(self.listen(target, deliver))


class Feed:
    """One registration: its queue and the newest message the bot had then (`newest`, Zulip's
    `max_message_id`, None if it didn't say); its catch-up while due (`where`), with its tries;
    and the IDs the catch-up delivered, with, until it settles, those the queue delivered."""

    def __init__(self, queue: dict, where: Where) -> None:
        self.id, self.last = queue["queue_id"], queue["last_event_id"]
        newest = queue.get("max_message_id")
        self.newest: int | None = newest if type(newest) is int else None
        self.where: Where | None = None if where == (None, None) else where
        self.caught: set[int] = set()
        self.tries, self.due = 0, 0.0


async def fetch(target: Target, senders: Senders, **params: Any) -> list[dict]:
    """`GET /messages` for `target`, oldest first, without `senders`' messages while Zulip takes
    the narrow; once it refuses it, the whole page."""
    if senders.narrow:
        narrow = [{"operator": "sender", "operand": e, "negated": True} for e in senders.emails]
        try:
            result = await target.client.request(
                "GET", "messages", apply_markdown=False, narrow=narrow, **params
            )
            return sorted(result["messages"], key=lambda m: m["id"])
        except ZulipError as exc:
            if exc.code != "BAD_NARROW":
                raise
            LOG.warning("Waker catch-up narrow refused for %s: filtered here", target.bot.name)
            senders.narrow = False
    result = await target.client.request("GET", "messages", apply_markdown=False, **params)
    return sorted(result["messages"], key=lambda m: m["id"])


async def recent(target: Target, senders: Senders, after: int | None, floor: float) -> list[dict]:
    """What a catch-up delivers (module doc): `target`'s messages after `after` (a mark, or
    None) and sent from `floor` on, not `senders`', the newest `CATCH_UP` at most, each as
    `kept`, oldest first; fetched from the newest back, `PAGE` at a time."""
    found: list[dict] = []
    anchor: int | str = "newest"
    fetched, more = 0, True
    while more and fetched < CATCH_UP:
        size = min(PAGE, CATCH_UP - fetched)
        params: dict[str, Any] = {"anchor": anchor, "num_before": size, "num_after": 0}
        if anchor != "newest":
            params["include_anchor"] = False
        page = await fetch(target, senders, **params)
        fetched += len(page)
        more = len(page) >= size
        for m in reversed(page):
            sent = m.get("timestamp")
            if (after is not None and m["id"] <= after) or (
                type(sent) in (int, float) and sent < floor
            ):
                more = False  # the mark, or older than the floor: the rest are older still
                break
            if type(sent) in (int, float) and senders.keeps(m):
                found.append(kept(m))
        if page:
            anchor = page[0]["id"]
        del page  # before the next page: one bot holds one page at a time
    return found[::-1]


async def catch_up(
    target: Target,
    deliver: Deliver,
    where: Where,
    caught: set[int],
    newest: int | None = None,
    senders: Senders | Iterable[str] = (),
) -> None:
    """`target`'s messages since `where`, within the bounds, not `senders`' and not in `caught`,
    through `deliver`; their IDs go into `caught`. `newest` is the newest message the bot has
    received (the registration's), if known: a mark past it (another realm's, or damaged)
    counts as none, and the catch-up starts from the time."""
    skip = senders if isinstance(senders, Senders) else Senders(senders)
    after, start = where
    if after is not None and newest is not None and after > newest:
        LOG.warning("Catch-up mark of %s is past its newest message: ignored", target.bot.name)
        after = None
    floor = time.time() - CATCH_UP_AGE
    if after is None:
        if start is None:
            return
        floor = max(floor, start)
    elif after == newest:
        return  # nothing since the mark: no request
    missed = [m for m in await recent(target, skip, after, floor) if m["id"] not in caught]
    for message in missed:
        caught.add(message["id"])
        await deliver(target, message, message.get("flags", []))
    if missed:
        LOG.info("Caught up on %d message(s) for %s", len(missed), target.bot.name)


async def resume(
    target: Target,
    deliver: Deliver,
    feed: Feed,
    settled: Settled | None,
    senders: Senders | Iterable[str] = (),
    deadline: float | None = None,
) -> None:
    """`feed`'s catch-up, once more, by the event loop's `deadline` (the turn's: `TURN` seconds
    after it began, its registration included), else in `TURN` seconds; settled when it
    succeeds or has had all its tries."""
    try:
        where = feed.where or (None, None)
        end = asyncio.get_running_loop().time() + TURN if deadline is None else deadline
        async with asyncio.timeout_at(end):
            await catch_up(target, deliver, where, feed.caught, feed.newest, senders)
    except Exception as exc:
        feed.tries += 1
        code = exc.code if isinstance(exc, ZulipError) else type(exc).__name__
        if feed.tries < CATCH_UP_TRIES:
            LOG.warning("Waker catch-up failed (%s): trying again", code)
            feed.due = time.monotonic() + CATCH_UP_RETRY * 2 ** (feed.tries - 1)
            return
        LOG.warning("Waker catch-up failed (%s): given up after %d tries", code, feed.tries)
    feed.where = None
    if settled is not None:
        settled(target)


async def listen(
    target: Target,
    deliver: Deliver,
    since: Since | None = None,
    settled: Settled | None = None,
    senders: Senders | Iterable[str] = (),
) -> None:
    skip = senders if isinstance(senders, Senders) else Senders(senders)
    feed: Feed | None = None
    while True:
        try:
            if feed is None or (feed.where is not None and time.monotonic() >= feed.due):
                async with gate():  # its turn (#717), of at most TURN seconds in all
                    began = time.monotonic()
                    deadline = asyncio.get_running_loop().time() + TURN
                    if feed is None:
                        async with asyncio.timeout_at(deadline):
                            queue = await target.client.request(
                                "POST",
                                "register",
                                event_types=["message"],
                                fetch_event_types=["realm", "message"],
                                apply_markdown=False,
                            )
                        feed = Feed(queue, since(target) if since is not None else (None, None))
                        if feed.where is None and settled is not None:
                            settled(target)  # nothing to catch up on
                    if feed.where is not None and time.monotonic() >= feed.due:
                        await resume(target, deliver, feed, settled, skip, deadline)
                    took = time.monotonic() - began
                    LOG.info("Waker turn of %s: %.1f s", target.bot.name, took)
            for event in await target.client.events(feed.id, feed.last):
                if event["type"] == "message":
                    message = event["message"]
                    if message["id"] not in feed.caught:
                        if feed.where is not None:  # a catch-up to try again skips it
                            feed.caught.add(message["id"])
                        flags = event.get("flags", message.get("flags", []))
                        await deliver(target, message, flags)
                feed.last = event["id"]
        except ZulipError as exc:
            if exc.code == "BAD_EVENT_QUEUE_ID":
                feed = None
            LOG.warning("Waker Zulip error (%s)", exc.code)
            await asyncio.sleep(max(1, exc.retry_after))
        except Exception as exc:
            LOG.warning("Waker listener retry (%s)", type(exc).__name__)
            await asyncio.sleep(5)
