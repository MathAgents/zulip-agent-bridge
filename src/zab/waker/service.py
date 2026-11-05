"""Owner-authorized VM starts with per-instance cooldowns (SPEC §13; issue #207)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from zab.config import BotConfig, allowed_users
from zab.core.addressing import KINDS, typed_kinds, visible_text
from zab.core.store import Store
from zab.waker import queues
from zab.zulip.client import ZulipClient, error_kind

LOG = logging.getLogger("zab.waker")
#: The fleet's `workshop` tag value; the config's `workshop` key overrides it (the test waker's
#: VM is `workshop=zab-test`, issue #263). Only instances carrying the tag are ever started.
WORKSHOP = "nt-agents-2026"
TAG = {"Key": "workshop", "Value": WORKSHOP}
#: Messages kept per VM while they may wait for its wake.
HOLD = 100
#: Messages already acknowledged, remembered so that a late copy, from any bot, adds no ⏰.
DONE = 1024
#: Seconds the acknowledgments wait, after the start request, until all the VM's bots' queues
#: have delivered each channel message (their `mentioned` flags pick the bots).
GRACE = 5.0
STARTING = "⏳ Starting your VM (about 30 seconds)…"
#: Per bot, in the store: its mark, the message up to which it has handled every one (#403).
MARK = "waker_mark:"
#: Starter mode's states snapshots, one per workshop: `STATES` and its name.
STATES = "waker_states:"
#: Starter mode: when each workshop's waker last served, `READY` and its name (#717).
READY = "waker_ready:"
#: The states in which a VM's bridge takes its messages in, or will once it is up (#403).
UP = ("pending", "running")
#: Seconds of Zulip's clock (whole seconds) against this host's, as the wake monitor allows
#: (`wakes.SKEW`): for the catch-up's start without a mark, and the stale-message rule.
SKEW = 30.0


@dataclass
class Target:
    bot: BotConfig
    instance: str
    client: ZulipClient


@dataclass
class Waiting:
    """A message that may wait for its VM's wake (SPEC §13.4): its IDs and type, never its
    text, and which of the VM's bots saw it, each with whether it names that bot (`named`).
    Both bots of a VM see a channel message: the copies merge here."""

    id: int
    sender_id: int
    type: str
    bots: dict[str, tuple[Target, bool]] = field(default_factory=dict)

    @classmethod
    def of(cls, message: dict) -> Waiting:
        return cls(message["id"], message["sender_id"], message["type"])


def kind_of(name: str) -> str | None:
    """A bot's kind, from its configured name's prefix: `claude-<slug>`, `codex-<slug>`, as
    `push_waker` names them; None for another name, which no typed mention names (#713)."""
    kind = name.partition("-")[0].casefold()
    return kind if kind in KINDS else None


def named(target: Target, message: dict, flags: Iterable[str]) -> bool:
    """Whether `message` names `target`'s bot, as its bridge reads it (SPEC §4.5): Zulip's
    `mentioned` flag, for a completed mention outside quotes (Zulip silences those in a quote),
    or a typed `@claude` or `@codex` of its kind, which Zulip doesn't flag (#713). The text is
    read here and not kept; a caught-up message has none, but the kinds it named (`typed`,
    `queues.kept`)."""
    if "mentioned" in flags:
        return True
    kind, typed = kind_of(target.bot.name), message.get("typed")
    if isinstance(typed, list):  # caught up (#727): no text
        return kind in typed
    text = message.get("content")
    return bool(kind and isinstance(text, str) and kind in typed_kinds(visible_text(text)))


def error_code(exc: Exception) -> str:
    """botocore's error code, which the starter's client raises too, else the exception's class
    (`error_kind`: a malformed zuliprc's parse error, #527), whatever an odd exception's
    `response` holds: the supervisor and `wake` must never fail on it (#348)."""
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    code = error.get("Code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else error_kind(exc)


class Waker:
    def __init__(
        self,
        targets: list[Target],
        ec2: Any,
        store: Store,
        organizers: int,
        workshop: str = WORKSHOP,
        states_key: str = "waker_states",
        opted_in: float | None = None,
    ) -> None:
        """`ec2` is boto3's (direct mode) or the starter's client (`zab.waker.starter_client`):
        both have `describe_instances` and `start_instances`. One waker serves one workshop;
        on the admin server, one per workshop share a process and a store (`states_key`).
        `opted_in` is the config's mtime (`since`)."""
        self.targets, self.ec2, self.store, self.organizers = targets, ec2, store, organizers
        self.tag = {"Key": "workshop", "Value": workshop}
        self.states_key = states_key
        self.source = queues.BotQueues(targets, self.since, self.settled)
        self.states: dict[str, str] = {}
        self.locks = {t.instance: asyncio.Lock() for t in targets}
        self.refresh_lock = asyncio.Lock()
        self.refreshed = 0.0
        self.missing: set[str] = set()
        self.rank = {t.bot.name: i for i, t in enumerate(targets)}
        self.bots: dict[str, set[str]] = {}
        for t in targets:
            self.bots.setdefault(t.instance, set()).add(t.bot.name)
        self.tasks: dict[str, asyncio.Task] = {}
        #: Per VM, by message ID: what was seen while the VM was or may have been stopped,
        #: until its wake is issued (each gets its ⏰) or a poll shows the VM up (dropped).
        self.waiting: dict[str, dict[int, Waiting]] = {}
        #: Per VM: the messages being acknowledged, which late copies still join, and the
        #: event that tells `acknowledge` one did.
        self.acking: dict[str, dict[int, Waiting]] = {}
        self.copied: dict[str, asyncio.Event] = {}
        self.done: dict[tuple[str, int], None] = {}
        #: Catch-up (#403). `opted_in`: when the config that lists these bots was written; per
        #: bot, the newest message it delivered (`seen`, at first its stored mark) and the mark
        #: last stored; per VM, the last time it was seen pending or running, or asked to start.
        self.opted_in = opted_in
        self.instance_of = {t.bot.name: t.instance for t in targets}
        self.seen = {n: m for n in self.instance_of if (m := self.stored_mark(n)) is not None}
        self.saved = dict(self.seen)
        self.up = observed(self.stored(states_key))
        #: Per bot, while its catch-up has not settled: the mark it started from, which the
        #: stored mark doesn't pass, so that a next run fetches what this one couldn't.
        self.held: dict[str, int | None] = {}

    def stored(self, key: str) -> Any:
        try:
            return self.store.get(key)
        except ValueError:  # not JSON: damaged
            return None

    def stored_mark(self, name: str) -> int | None:
        key = f"{MARK}{name}"
        if not self.store.rows("kv", key=key):
            return None
        if type(value := self.stored(key)) is int and value >= 0:
            return value
        LOG.warning("Ignoring a damaged catch-up mark for %s", name)
        return None

    def mark(self, name: str) -> int | None:
        """The newest message up to which every one `name` delivered was handled: before the
        oldest that still waits for its VM's wake, or for the next poll; and no further than
        where a catch-up that hasn't settled started (none, if it started without a mark)."""
        mark = self.seen.get(name)
        waiting = self.waiting.get(self.instance_of[name], {})
        if mark is not None and (held := [m for m, e in waiting.items() if name in e.bots]):
            mark = min(held) - 1
        if name in self.held:
            start = self.held[name]
            mark = start if start is None or mark is None else min(mark, start)
        return mark

    def checkpoint(self) -> None:
        """Store each bot's mark that moved. A lost write only makes the next catch-up longer."""
        try:
            for name in self.instance_of:
                if (mark := self.mark(name)) is not None and mark != self.saved.get(name):
                    self.store.set(f"{MARK}{name}", mark)
                    self.saved[name] = mark
        except Exception as exc:
            LOG.warning("Catch-up mark not stored (%s)", type(exc).__name__)

    def since(self, target: Target) -> tuple[int | None, float | None]:
        """Where `target`'s catch-up starts (`queues.catch_up`), at a registration: after its
        mark; else from the config's write, less `SKEW`: for a bot without a mark (newly opted
        in, as `forget` leaves a bot opted in again, or its mark damaged), or whose mark is past
        every message it can see. The stored mark is held there until `settled`."""
        name = target.bot.name
        self.held[name] = mark = self.mark(name)
        return mark, None if self.opted_in is None else self.opted_in - SKEW

    def settled(self, target: Target) -> None:
        """`target`'s catch-up is done, or given up: its mark may move on."""
        self.held.pop(target.bot.name, None)
        self.checkpoint()

    def cooling(self, instance: str) -> bool:
        """Whether a start was asked for less than a minute ago. A time in the future (a clock
        set back since) doesn't count, nor does a damaged one: neither may block every wake."""
        last = self.stored(f"wake:{instance}")
        return type(last) in (int, float) and 0 <= time.time() - last < 60

    async def refresh(self, *, force: bool = False) -> None:
        async with self.refresh_lock:
            if not force and time.monotonic() - self.refreshed < 30:
                return
            response = await asyncio.to_thread(
                self.ec2.describe_instances,
                Filters=[
                    {"Name": "tag:workshop", "Values": [self.tag["Value"]]},
                    {"Name": "instance-id", "Values": sorted(self.locks)},
                ],
            )
            self.states = {
                i["InstanceId"]: i["State"]["Name"]
                for r in response["Reservations"]
                for i in r["Instances"]
                if self.tag in i.get("Tags", [])
            }
            missing = self.locks.keys() - self.states.keys()
            for instance in sorted(missing - self.missing):
                LOG.warning("Configured VM missing or untagged: %s", instance)
            self.missing = missing
            self.refreshed = time.monotonic()
            now = time.time()
            self.up.update((i, now) for i, state in self.states.items() if state in UP)
            up = {i: t for i, t in self.up.items() if i in self.locks}
            self.store.set(self.states_key, {"at": now, "states": self.states, "up": up})

    async def owner(self, target: Target, uid: int) -> bool:
        # A messages-only queue cannot invalidate an email lookup on realm_user:
        # resolve again before every proposed wake, including retries. Fail closed.
        me = await target.client.request("GET", "users/me")
        if me.get("is_bot") is not True:
            return False
        users = (await target.client.request("GET", "users"))["members"]
        user = next((u for u in users if u["user_id"] == uid), {})
        return (
            user.get("is_bot") is False
            and user.get("is_active", True)
            and uid in allowed_users(target.bot, me.get("bot_owner_id"), users)
        )

    async def message(self, target: Target, message: dict, flags: Iterable[str] = ()) -> None:
        """One bot's copy of a message, live or caught up (#403); `flags` are its queue's,
        `mentioned` for that bot. The bot's mark is stored after it, whatever happened."""
        name = target.bot.name
        self.seen[name] = max(self.seen.get(name, message["id"]), message["id"])
        try:
            await self.consider(target, message, flags)
        finally:
            self.checkpoint()

    async def consider(self, target: Target, message: dict, flags: Iterable[str]) -> None:
        instance, mid = target.instance, message["id"]
        if (instance, mid) in self.done:
            if late := self.acking.get(instance, {}).get(mid):  # in time to pick its bot
                late.bots.setdefault(target.bot.name, (target, named(target, message, flags)))
                if copied := self.copied.get(instance):
                    copied.set()
            return
        sent = message.get("timestamp")
        if type(sent) in (int, float) and sent + 1 + SKEW < self.up.get(instance, -math.inf):
            # Stale (#403): the VM was seen up after it was sent, so its bridge has it. The
            # margin is Zulip's whole second and `SKEW`: a live message never counts as stale.
            return
        waiting = self.waiting.setdefault(instance, {})
        entry = waiting.get(mid)
        first = entry is None
        if entry is None:
            entry = waiting[mid] = Waiting.of(message)
            if len(waiting) > HOLD:
                # Drop the oldest message of the sender with the most, so that another
                # sender's flood never pushes out the owner's.
                counts = Counter(e.sender_id for e in waiting.values())
                noisy = max(counts, key=counts.__getitem__)
                waiting.pop(next(m for m, e in waiting.items() if e.sender_id == noisy))
        entry.bots[target.bot.name] = (target, named(target, message, flags))
        if not first or instance in self.tasks:
            return  # the other bot's copy, or a message that joins the wake in flight
        if self.states.get(instance) not in ("stopped", "stopping"):
            # A shutdown can begin between the cached observation and this event: the
            # message waits for the next poll (`reconsider`).
            return
        try:
            if not await self.trigger(instance, entry):
                waiting.pop(mid, None)
        except Exception as exc:
            LOG.warning("Deferred wake authorization retry (%s)", type(exc).__name__)

    async def trigger(
        self, instance: str, entry: Waiting, denied: set[tuple[str, int]] | None = None
    ) -> bool:
        """Start the VM's wake for `entry` if a bot that saw it allows its sender, the bots
        that answer for it first; False when none does, or not yet. `denied` remembers failed
        owner checks across calls. A copy that merges during a check is checked too."""
        if self.cooling(instance):
            return False
        denied = set() if denied is None else denied
        while True:
            names = [t.bot.name for t in self.candidates(entry)] + self.ranked(entry)
            name = next((n for n in names if (n, entry.sender_id) not in denied), None)
            if name is None:
                return False
            target = entry.bots[name][0]
            if await self.owner(target, entry.sender_id):
                break
            denied.add((name, entry.sender_id))
        # No awaits between this check and the assignment: both bots share one VM.
        if instance not in self.tasks:
            self.tasks[instance] = asyncio.create_task(self.wake(target, entry))
        return True

    def ranked(self, entry: Waiting) -> list[str]:
        """The bots that saw `entry`, in the configuration's order."""
        return sorted(entry.bots, key=lambda name: (self.rank.get(name, len(self.rank)), name))

    def addressees(self, entry: Waiting) -> list[str]:
        """The bots that saw `entry` and answer for it as the bridge routes it, in the
        configuration's order: the bots it names, by a completed mention or a typed one; else,
        in a channel, each of them, as a message that names no bot goes to each of the VM's
        bots (SPEC §4.5 rule 6.2, #713). A DM without a mention: none."""
        names = self.ranked(entry)
        if mentioned := [name for name in names if entry.bots[name][1]]:
            return mentioned
        return names if entry.type == "stream" else []

    def candidates(self, entry: Waiting) -> list[Target]:
        """Its `addressees`, else every bot that saw it, in the configuration's order. A channel
        message gets a ⏰ from each of its addressees that allows the sender (`acknowledge`); a
        DM, one notice, from the first candidate that does."""
        names = self.addressees(entry) or self.ranked(entry)
        return [entry.bots[name][0] for name in names]

    async def notify(self, target: Target, endpoint: str = "messages", **params: Any) -> None:
        from zab.zulip.poster import Poster

        poster = Poster(target.client)
        try:
            await poster.call(lambda: target.client.request("POST", endpoint, **params))
        except Exception as exc:
            LOG.warning("Wake notice failed (%s)", type(exc).__name__)
        finally:
            await poster.close()

    async def wake(self, target: Target, trigger: Waiting) -> None:
        issued = False
        try:
            async with self.locks[target.instance]:
                for attempt in range(3):
                    if attempt:
                        await asyncio.sleep(60 * 2 ** (attempt - 1))
                    await self.refresh(force=True)
                    # Keep the triggering message while an idle shutdown finishes.
                    deadline = time.monotonic() + 300
                    while self.states.get(target.instance) == "stopping":
                        if time.monotonic() >= deadline:
                            LOG.warning("VM still stopping after five minutes: %s", target.instance)
                            return
                        await asyncio.sleep(10)
                        await self.refresh(force=True)
                    if self.states.get(target.instance) != "stopped":
                        return
                    if not await self.owner(target, trigger.sender_id):
                        return
                    key = f"wake:{target.instance}"
                    if self.cooling(target.instance):
                        return
                    # Save before the AWS call, including failures, across restarts.
                    self.store.set(key, time.time())
                    try:
                        await asyncio.to_thread(
                            self.ec2.start_instances,
                            InstanceIds=[target.instance],
                        )
                    except Exception as exc:
                        if error_code(exc) != "InsufficientInstanceCapacity":
                            raise
                        if attempt < 2:
                            continue
                        text = (
                            "Could not start your VM after three capacity failures; "
                            "please contact the organizers."
                        )
                        await self.notify(
                            target,
                            type="private",
                            to=[trigger.sender_id],
                            content=text,
                        )
                        await self.notify(
                            target,
                            type="stream",
                            to=self.organizers,
                            topic="VM wake failures",
                            content=(
                                f"VM {target.instance}: "
                                "three InsufficientInstanceCapacity failures."
                            ),
                        )
                        return
                    self.states[target.instance] = "pending"
                    self.up[target.instance] = time.time()
                    # No awaits from here to `acking`: a message first seen after this point
                    # waits as for a running VM, and the bridge's 👀 answers it.
                    issued = True
                    batch = self.waiting.pop(target.instance, {})
                    batch.setdefault(trigger.id, trigger)
                    for mid in batch:
                        self.done[(target.instance, mid)] = None
                    while len(self.done) > DONE:
                        self.done.pop(next(iter(self.done)))
                    self.acking[target.instance] = batch
                    await self.acknowledge(target, trigger, batch)
                    return
        except Exception as exc:
            LOG.warning("Wake failed (%s)", type(exc).__name__)  # Never log Zulip contents.
        finally:
            self.tasks.pop(target.instance, None)
            self.acking.pop(target.instance, None)
            self.copied.pop(target.instance, None)
            if not issued:  # no wake: the messages that waited for it are dropped
                self.waiting.pop(target.instance, None)

    async def acknowledge(
        self, target: Target, trigger: Waiting, batch: dict[int, Waiting]
    ) -> None:
        """After the start request: on each channel message that waited for it, a ⏰ from each
        of its `addressees` that allows the sender, as each bot's bridge adds its 👀 (the bots
        it names, else each of them); and one "Starting" DM for the DMs (the trigger's sender
        was checked just before the start). The ⏰s wait up to `GRACE` seconds for every bot's
        copy of the channel messages, whatever order the queues delivered them in; the start
        doesn't wait. Each message is acknowledged once, by distinct bots: no bot adds two ⏰s
        to one message, and a copy that arrives after its ⏰s adds none (`done`)."""
        bots = self.bots.get(target.instance, set())
        copied = self.copied[target.instance] = asyncio.Event()
        deadline = time.monotonic() + GRACE
        while any(not bots <= e.bots.keys() for e in batch.values() if e.type == "stream"):
            if (left := deadline - time.monotonic()) <= 0:
                break
            copied.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(copied.wait(), left)
        allowed = {(target.bot.name, trigger.sender_id): True}
        noticed = False
        for entry in sorted(batch.values(), key=lambda e: e.id):
            # A channel message's `candidates` are its addressees: each answers. A DM gets one
            # notice.
            each = entry.type == "stream"
            chosen: list[Target] = []
            for candidate in self.candidates(entry):
                key = (candidate.bot.name, entry.sender_id)
                if key not in allowed:
                    try:
                        allowed[key] = await self.owner(candidate, entry.sender_id)
                    except Exception as exc:
                        LOG.warning("Wake notice authorization failed (%s)", type(exc).__name__)
                        allowed[key] = False
                if allowed[key]:
                    chosen.append(candidate)
                    if not each:
                        break
            if not chosen:
                continue
            if entry.type == "private":
                if not noticed:
                    noticed = True
                    await self.notify(
                        chosen[0], type="private", to=[entry.sender_id], content=STARTING
                    )
                continue
            for bot in chosen:
                await self.notify(
                    bot,
                    f"messages/{entry.id}/reactions",
                    emoji_name="alarm_clock",
                    emoji_code="23f0",
                    reaction_type="unicode_emoji",
                )

    async def listen(self, target: Target) -> None:
        await self.source.listen(target, self.message)

    async def reconsider(self) -> None:
        """After each poll: a VM that shows stopped or stopping now wakes for the messages that
        waited while the cache said otherwise; for a VM that is up they are dropped. Only the
        messages seen before this pass count: one that arrives while it awaits owner checks
        waits for the next poll, which has seen its VM since."""
        seen = {instance: list(waiting) for instance, waiting in self.waiting.items()}
        for instance, mids in seen.items():
            waiting = self.waiting.get(instance)  # a wake may have taken them meanwhile
            if waiting is None or instance in self.tasks:
                continue  # they join the wake in flight
            if self.states.get(instance) not in ("stopped", "stopping"):
                for mid in mids:
                    waiting.pop(mid, None)
                if not waiting:
                    del self.waiting[instance]
                continue
            denied: set[tuple[str, int]] = set()
            for mid in mids:  # oldest first
                if instance in self.tasks or self.waiting.get(instance) is not waiting:
                    break
                if (entry := waiting.get(mid)) is None:
                    continue
                try:
                    if not await self.trigger(instance, entry, denied):
                        waiting.pop(mid, None)
                except Exception as exc:
                    LOG.warning("Deferred wake authorization retry (%s)", type(exc).__name__)

    async def poll_states(self) -> None:
        while True:
            try:
                await self.refresh(force=True)
                await self.reconsider()
                self.checkpoint()  # the marks the pass moved
            except Exception as exc:
                LOG.warning("Waker state refresh failed (%s)", type(exc).__name__)
            await asyncio.sleep(30)

    async def run(self) -> None:
        try:
            await self.refresh(force=True)
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll_states())
                group.create_task(self.source.run(self.message))
        finally:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for target in self.targets:
                await target.client.close()


def observed(snapshot: Any) -> dict[str, float]:
    """Per VM, the last time it was seen pending or running, from the states snapshot that
    `Waker.refresh` stores: its `up`, and its `states` at its `at` (a snapshot stored before
    #403 has only those). Anything damaged counts as not seen, and so does a time in the future
    (a clock set back since): as now, it would make every message stale, a VM long stopped
    included, until the VM is seen up again."""
    if not isinstance(snapshot, dict):
        return {}
    now = time.time()

    def when(t: Any) -> float | None:
        return float(t) if type(t) in (int, float) and math.isfinite(t) and t <= now else None

    up = snapshot.get("up")
    seen = (
        {i: t for i, v in up.items() if (t := when(v)) is not None} if isinstance(up, dict) else {}
    )
    states, at = snapshot.get("states"), when(snapshot.get("at"))
    if isinstance(states, dict) and at is not None:
        for instance, state in states.items():
            if state in UP:
                seen[instance] = max(seen.get(instance, at), at)
    return seen


def forget(store: Store, bots: set[str], snapshots: set[str] | None = None) -> None:
    """At the waker's start (`waker run`): drop the catch-up marks of bots its config doesn't
    list, so that a bot opted in again catches up from the config's write, never from its mark
    of before the opt-out, and a bot name used again starts afresh (#403). In starter mode,
    `snapshots` are the states and ready keys of the workshops it lists: the others' go too."""
    keys = [r[0] for r in store.db.execute("SELECT key FROM kv WHERE substr(key, 1, 6) = 'waker_'")]
    with store.transaction():
        for key in keys:
            if key.startswith(MARK) and key.removeprefix(MARK) not in bots:
                store.delete(key)
            elif snapshots is not None and key.startswith((STATES, READY)) and key not in snapshots:
                store.delete(key)
