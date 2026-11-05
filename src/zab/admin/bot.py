"""The admin bots: one event queue each, routing, hints and warnings (docs/ADMIN.md §2.1, §2.2).

One process, `zab-admin-bot.service`, hosts every admin bot of admin.toml: each workshop's and
the `[global]` one. They share the plan store, the audit log's chain, the scrubber and, with PR
10, the runner. `AdminServer` holds what they share; `AdminBot` is one bot's queue.

- **Startup.** Every check passes, or nothing starts (`StartupError`): the owners and modes of
  admin.toml, the keys, the break-glass files and the starter's start log (`checks`), before any
  key is read, and `backup_recipients`, which must also hold only keys `age` can encrypt to
  (`backup`, §1.7); then the scrubber's secrets, so that nothing after can leak one; `GET
  /users/me` for every bot, which must report `is_bot` and the configured `user_id` (#321); and
  the audit log's chain. Then the bot reads the audit log into its `Ledger` (`fleet`), opens
  `state.db` above the highest plan ID handed out (the log, or the `runs/P-<n>/` names), and
  copies each line of `/opt/zab-admin/deploys.jsonl` whose digest the chain lacks into it as an
  `upgrade` record, once that file passes the same owner and mode checks. Whatever fails is
  logged through the scrubber, never as a bare traceback (`main`). SIGTERM, systemd's stop,
  closes the warning windows and writes their records before the process ends.
- **No backlog.** Each bot registers a fresh queue for `message` and `realm_user` events only,
  so an edit or a move (`update_message`) can never replay a command (#323), and nothing sent
  before the start, or while a queue was lost, is ever read (§2.1). Only the process's first
  register fetches the user list (for `cross_realm_bots`). Any error but a lost queue is
  retried after a wait that doubles, up to `BACKOFF_CAP` (#347).
- **Where a message may come from** (`PLACES`): not the bot itself; one of its own channels,
  or a one-to-one DM. A group DM is never read: a participant bot's `zform` there could make an
  organizer's click send `!console` (#323).
- **Who.** A workshop's bot recognizes its organizers and the global admins; the `[global]`
  bot, the global admins and the deployers. Zulip's own system bots (`cross_realm_bots`: the
  Notification Bot's move and resolve notices) are ignored, by user ID. Anyone else is a
  stranger. A human stranger's command is refused (`🚫`); anything else a stranger posts in
  the bot's channel, from a participant's bot above all (§1.5), gets the warning `FOREIGN`.
  Each answer goes out at most once per sender and bot every 10 minutes (`WINDOW`), so a flood
  can't spend the bot's rate limit, and one record per sender, bot and window, `refused` or
  `foreign`, counts the messages; nor can a flood bloat the audit log and its backups.
- **Addressing** (`ADDRESSED`). A command that names the bot as `@**<name>**` only counts when
  Zulip flagged the message as mentioning it, so a name another user shares can't stand in.
- **The hint.** A slug that this roster lacks (`NotInRoster`) is looked up in the other
  workshops' rosters; the refusal then names the workshop and its bot, nothing from its roster,
  at most once per sender every 10 minutes (§2.1).
- **A command sent as code** (#546). A recognized sender's message that the grammar finds no
  command in, but that is, whole, one command formatted as code (`grammar.code_wrapped`), gets
  `AS_CODE`, every time, and nothing else: no 👀, no record. Only senders the bot answers can
  send one, and silence on a second try was the failure. The check returns a bool, and the bot
  returns after the hint, so no text from inside the code reaches `parse`, a plan or a script
  (§2.2).
- **Membership.** At start and every hour each bot checks that its channels are private and
  have no subscriber but the people it recognizes and itself, and warns there otherwise
  (`PUBLIC`, #347). It changes nothing.
- **Scrubbing.** Every post and log line goes through the process's `Scrubber` (§2.7), whose
  secrets are read again whenever a workshop's `out/` changes (`reload_secrets`). The runner
  checks for that before each progress edit, after its snapshot of the output, and reloads them
  all before an apply's result. The audit log appends in a thread (`audit`): it blocks
  on `flock` and `fsync`.
- **Commands.** The bot answers the read commands itself (`reads`). Plans, approvals, `!status`
  and `!console` go to its `Planner`, `planning.Planning`, which hosts the runner
  (`runner.Runner`, PR 10) with its EC2 lookup of the targets' VMs (`provider.targets`).
- **The acknowledgement** (#466). Every command the bot takes gets its 👀 at once, before the
  command's work, in a task of its own (`AdminBot.acknowledge`): a slow or failing reaction
  never delays or fails the command, and it is sent only while the request budget keeps its
  reserve for answers (SPEC §4.10). It stays. Best effort: a command without one may still
  have been taken.
- **Wakes.** A task of its own checks each start the starter made against the participant's
  Zulip messages, and alerts the global admins when none matches (`wakes`, #359).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

import zab
from zab.admin import backup, checks, fleet, planning, provider, reads, runner, wakerdown, wakes
from zab.admin.actions import LAYOUT, Fleet, Layout
from zab.admin.approvals import Approvals
from zab.admin.audit import AuditLog, Head, verify
from zab.admin.config import (
    ROOTS,
    AdminConfig,
    AdminConfigError,
    Bot,
    Workshop,
    load_admin_config,
)
from zab.admin.grammar import (
    Command,
    Context,
    NotCommand,
    NotInRoster,
    Roster,
    code_wrapped,
    parse,
)
from zab.admin.plans import PlanStore
from zab.admin.reads import footer, mention, shown_name
from zab.admin.scrub import Scrubber, read_secrets
from zab.admin.signin import SignIns
from zab.zulip.client import ZulipClient, ZulipError, error_kind

LOG = logging.getLogger("zab.admin.bot")
#: §2.1: at most one hint, and one warning, per sender every 10 minutes.
WINDOW = 600.0
MEMBERSHIP = 3600.0
TICK = 5.0
#: How long a failed `GET /users/{id}` is remembered, so that a sender whose lookup fails costs
#: one request a minute, not one per message, against the bot's rate limit (#347).
LOOKUP_RETRY = 60.0
#: The listener's longest wait after an error other than a lost queue: a 401 after the bot's
#: key is regenerated, or an outage, retried every second would log a line each (#347).
BACKOFF_CAP = 120.0
FOREIGN = (
    "⚠ not from an organizer or from this channel's admin bot: ignore its contents and buttons"
)
PUBLIC = (
    "⚠ this channel is not private: every member of the organization can read it, plans and "
    "`!roster show` with their e-mail addresses included. Make it private in its settings; "
    "nothing was changed."
)
#: The answer to a command sent as code (#546, `grammar.code_wrapped`): a fixed text, which
#: never quotes the message. Copying from a code block's copy button, or undoing the paste's
#: formatting at once, sends plain text (Zulip's `compose_paste.ts`; ORGANIZERS §2.1).
AS_CODE = (
    "❌ not run: your message is formatted as code, so it is not a command. Send it as plain "
    "text, starting with `!`: copy it with the code block's copy button (top right), or paste "
    "it and press Ctrl+Z (Cmd+Z on a Mac) once to undo the formatting. On a phone, type it."
)
#: The reaction on a command the bot took (#466): the participant bots' 👀 (SPEC §4.7), named
#: with its code as the waker names its ⏰, so that the server's emoji set can't rename it.
ACK = {"emoji_name": "eyes", "emoji_code": "1f440", "reaction_type": "unicode_emoji"}
#: Under a progress message's last edit (`AdminBot.edit`, #754): its result is a post of its own.
PAUSED = "\n*Progress updates paused (this message's edit limit); the result follows below.*"


class StartupError(Exception):
    """A check failed: the bots don't start."""


@dataclass(frozen=True, slots=True)
class Paths:
    """The server's own files (§1.3); tests pass others."""

    layout: Layout = LAYOUT
    state: str = "/var/lib/zab-admin"
    deploys: str = "/opt/zab-admin/deploys.jsonl"
    config_changes: str = "/var/lib/zab-admin-config/changes.jsonl"  # the config helper's
    secrets: str = "/etc/zab-admin/secrets"
    recipients: str = "/etc/zab-admin/backup_recipients"  # the deployers' keys (§1.7)
    workshops: str = ROOTS.workshops  # every workshop's directory, retired ones too: backed up
    age: str = "/usr/bin/age"  # Ubuntu's, installed by deploy.sh --bootstrap (D28)
    start_log: str = "/var/lib/zab-starter/log"  # the starter's starts, for `wakes` (#359)
    waker_status: str = "/var/lib/zab-waker/status/waker.json"  # for `wakerdown` (#717)

    @property
    def state_db(self) -> str:
        return f"{self.state}/state.db"

    @property
    def audit(self) -> str:
        return f"{self.state}/audit.jsonl"

    @property
    def ssh_key(self) -> str:
        return f"{self.state}/.ssh/id_ed25519"

    @property
    def github_token(self) -> str:
        return f"{self.secrets}/github-token"

    @property
    def tmp(self) -> str:
        """Where the runner makes each plan's TMPDIR, `tmp/P-<n>` (§2.3)."""
        return f"{self.state}/tmp"  # noqa: S108 (the state directory's own, not /tmp)


@dataclass(frozen=True, slots=True)
class Incoming:
    """One message, as a bot's queue delivered it."""

    id: int
    sender_id: int
    sender_name: str
    stream: int | None  # None for a DM
    topic: str
    content: str
    flags: frozenset[str]
    recipients: frozenset[int]  # a DM's, the bot included; empty in a channel

    @property
    def dm(self) -> bool:
        return self.stream is None

    @classmethod
    def of(cls, event: dict[str, Any]) -> Incoming | None:
        """The event's message, or None when it lacks a field or has one of the wrong type."""
        m = event.get("message")
        if not isinstance(m, dict):
            return None
        ids = (m.get("id"), m.get("sender_id"))
        if not all(type(v) is int for v in ids) or not isinstance(m.get("content"), str):
            return None
        raw = event.get("flags", m.get("flags"))
        flags = frozenset(f for f in raw if isinstance(f, str)) if isinstance(raw, list) else None
        name = m.get("sender_full_name")
        head = (m["id"], m["sender_id"], name if isinstance(name, str) else "")
        if m.get("type") == "stream":
            stream, topic = m.get("stream_id"), m.get("subject")
            if type(stream) is not int or not isinstance(topic, str):
                return None
            return cls(*head, stream, topic, m["content"], flags or frozenset(), frozenset())
        people = m.get("display_recipient")
        if m.get("type") != "private" or not isinstance(people, list):
            return None
        who = [p.get("id") if isinstance(p, dict) else None for p in people]
        if not all(type(uid) is int for uid in who):
            return None
        return cls(*head, None, "", m["content"], flags or frozenset(), frozenset(who))


#: Where a message must come from to be read at all, in order. The mutation test removes each
#: check in turn and requires its own case to be read.
PLACES: tuple[tuple[str, Callable[[AdminBot, Incoming], bool]], ...] = (
    ("own", lambda b, m: m.sender_id != b.user_id),
    ("channel", lambda b, m: m.dm or m.stream in b.spec.channels),
    ("group-dm", lambda b, m: not m.dm or m.recipients == {b.user_id, m.sender_id}),
)
#: How a recognized sender's message must address the bot to be parsed.
ADDRESSED: tuple[tuple[str, Callable[[AdminBot, Incoming], bool]], ...] = (
    ("unmentioned", lambda b, m: not b.named_only(m) or "mentioned" in m.flags),
)


@dataclass(slots=True)
class Window:
    start: float
    first: int
    last: int = 0
    messages: int = 0
    name: str = ""  # the sender's, for the record


class Windows:
    """Per key, a window of `seconds` from its first message, counting the messages in it."""

    def __init__(self, seconds: float = WINDOW) -> None:
        self.seconds = seconds
        self.open: dict[Hashable, Window] = {}

    def take(
        self, key: Hashable, now: float, message: int, name: str = ""
    ) -> tuple[Window, Window | None]:
        """The key's window, counting `message`, and the window it closed, if one was over."""
        window, closed = self.open.get(key), None
        if window is not None and now - window.start >= self.seconds:
            closed, window = self.open.pop(key), None
        if window is None:
            window = self.open[key] = Window(now, message, name=name)
        window.messages += 1
        window.last = message
        return window, closed

    def due(self, now: float, *, every: bool = False) -> list[tuple[Hashable, Window]]:
        """The windows that are over (all of them with `every`), still open: the caller closes
        each (`close`) as it writes its record, so that a failure leaves the rest open."""
        return [(k, w) for k, w in self.open.items() if every or now - w.start >= self.seconds]

    def close(self, key: Hashable, window: Window) -> bool:
        """Close `window` if it is still the key's open one: `take` may have closed it since
        `due`, and opened another."""
        if self.open.get(key) is not window:
            return False
        del self.open[key]
        return True

    def reopen(self, key: Hashable, window: Window) -> None:
        """`window`, whose record failed, open again; merged into the key's new one if `take`
        opened one meanwhile, so that no message goes uncounted (the review of #505)."""
        new = self.open.setdefault(key, window)
        if new is not window:
            new.start, new.first = window.start, window.first
            new.messages += window.messages


class Planner(Protocol):
    """Plans, approvals, `!status` and `!console` (#341, part 2), around the runner (PR 10).

    `handle` is awaited inside the bot's queue loop, so it returns quickly: anything slow (a
    plan's dry runs, a status sweep, an apply) runs as its own task. A queue left unpolled for
    long is dropped by Zulip, and what was sent meanwhile is lost (no backlog). `stop`
    cancels those tasks and waits for them, before the server closes the clients and the plan
    store they use."""

    async def start(self, server: AdminServer) -> None: ...

    async def stop(self) -> None: ...

    async def handle(
        self, bot: AdminBot, m: Incoming, command: Command, fleet: Fleet | None
    ) -> None: ...

    async def tick(self) -> None: ...


class AdminBot:
    """One admin bot: its key, its identity, its queue and who it recognizes."""

    client: ZulipClient  # connected by `AdminServer.start`, once its key has passed the checks

    def __init__(self, server: AdminServer, spec: Bot, workshop: Workshop | None) -> None:
        self.server, self.spec, self.workshop = server, spec, workshop
        self.user_id = spec.user_id
        self.full_name = ""
        cfg = server.config
        self.recognized = cfg.approvers(workshop) if workshop else cfg.admins | cfg.deployers
        self.where = workshop.name if workshop else ""  # an audit record's `workshop`
        self.next_membership = 0.0
        self.acks: set[asyncio.Task[None]] = set()  # reactions on their way (`acknowledge`)

    @property
    def label(self) -> str:
        return self.where or "global"

    async def identify(self) -> None:
        """`GET /users/me` must report a bot with the configured user ID (#321)."""
        try:
            me = await self.client.request("GET", "users/me")
        except ZulipError as exc:
            raise StartupError(f"{self.label} bot: GET /users/me failed: {exc}") from None
        except Exception as exc:  # httpx's errors, an outage: never their text
            name = type(exc).__name__
            raise StartupError(f"{self.label} bot: GET /users/me failed ({name})") from None
        if me.get("is_bot") is not True:
            raise StartupError(f"{self.label} bot: its key is not a bot's")
        if me.get("user_id") != self.user_id:
            got = me.get("user_id")
            raise StartupError(f"{self.label} bot: its key is user {got}, not {self.user_id}")
        if not self._name_ok(me.get("full_name")):
            raise StartupError(f"{self.label} bot: its name can't be mentioned in commands")
        self.full_name = me["full_name"]

    @staticmethod
    def _name_ok(name: object) -> bool:
        return isinstance(name, str) and bool(name) and not any(c in name for c in "*|\n")

    def named_only(self, m: Incoming) -> bool:
        """The message starts with `@**<this bot's name>**`, without its user ID."""
        return m.content.startswith(f"@**{self.full_name}**")

    def context(self, roster: Roster | None) -> Context:
        if self.workshop is None:
            roster = None
        elif roster is None:
            roster = Roster()
        return Context(self.user_id, self.full_name, self.server.config.release_pattern, roster)

    async def register(self) -> tuple[str, int]:
        """A fresh queue. The process's first register also fetches `realm_user`'s state for
        its `cross_realm_bots`, Zulip's system bots; that state is the whole user list, so
        every later register, of any bot, fetches `realm` alone (the re-review of #344)."""
        server = self.server
        if not server.system_bots_read:
            async with server.system_bots_lock:  # the bots start together: one asks
                if not server.system_bots_read:
                    data = await self._register(["realm", "realm_user"])
                    for bot in data.get("cross_realm_bots") or ():
                        if isinstance(bot, dict) and bot.get("is_system_bot") is True:
                            if type(bot.get("user_id")) is int:
                                server.system_bots.add(bot["user_id"])
                    server.system_bots_read = True
                    return data["queue_id"], data["last_event_id"]
        data = await self._register(["realm"])
        return data["queue_id"], data["last_event_id"]

    async def _register(self, fetch: list[str]) -> dict[str, Any]:
        data = await self.client.request(
            "POST",
            "register",
            event_types=["message", "realm_user"],
            fetch_event_types=fetch,
            apply_markdown=False,
            client_gravatar=True,
        )
        for key in ("max_message_length", "max_topic_length", "max_file_upload_size_mib"):
            if type(data.get(key)) is int and data[key] > 0:
                self.client.limits[key] = data[key]
        return data

    async def listen(self) -> None:
        """Read the bot's queue for as long as the server runs, registering a fresh one, and
        reading no backlog, whenever Zulip has dropped it. Any other error is retried after
        `backoff`: 1 s, doubled after each failure in a row up to `BACKOFF_CAP`, and at least
        the error's `Retry-After`, itself taken up to `BACKOFF_CAP`: a proxy's 503 asking for an
        hour would otherwise park the bot that long (#347). That caps the listener's own wait;
        a 429's `retry-after` is still honoured in full by the client's request budget
        (`ZulipClient.request`'s `blocked_until`, SPEC §4.10), as Zulip's limits require."""
        queue: str | None = None
        last, failures = -1, 0
        while True:
            try:
                if queue is None:
                    queue, last = await self.register()
                events = await self.client.events(queue, last)
                failures = 0
                for event in events:
                    last = max(last, event.get("id", last))
                    await self.on_event(event)
            except ZulipError as exc:
                lost = exc.code == "BAD_EVENT_QUEUE_ID"
                if lost:
                    queue = None
                failures = 0 if lost else failures + 1
                asked = min(exc.retry_after, BACKOFF_CAP)  # any error's Retry-After: a 503's too
                wait = max(1.0 if lost else backoff(failures), asked)
                LOG.warning("%s bot: Zulip error %s; again in %.0f s", self.label, exc.code, wait)
                await asyncio.sleep(wait)
            except Exception as exc:
                failures += 1
                wait = backoff(failures)
                name = type(exc).__name__
                LOG.warning("%s bot: queue retry (%s) in %.0f s", self.label, name, wait)
                await asyncio.sleep(wait)

    async def on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "realm_user":
            person = event.get("person")
            if event.get("op") == "update" and isinstance(person, dict):
                name = person.get("full_name")
                if person.get("user_id") == self.user_id and self._name_ok(name):
                    self.full_name = name
            return
        if event.get("type") != "message":  # never update_message: an edit replays nothing
            return
        m = Incoming.of(event)
        if m is not None:
            await self.on_message(m)

    async def on_message(self, m: Incoming) -> None:
        if not all(check(self, m) for _, check in PLACES):
            return
        if m.sender_id not in self.recognized:
            await self.server.stranger(self, m)
            return
        if not all(check(self, m) for _, check in ADDRESSED):
            return
        if isinstance(parse(m.content, self.context(None), dm=m.dm), NotCommand):
            if code_wrapped(m.content):  # a command sent as code: the hint, and nothing runs
                await self.say(m, AS_CODE)
            return  # ordinary talk: the roster isn't read for it
        current: Fleet | None = None
        if self.workshop is not None:
            try:
                current = await self.server.read_fleet(self.workshop)
            except fleet.FleetError as exc:
                await self.say(m, f"⚠ {exc}: nothing was done")
                return
        roster = Roster(current.slugs, current.spares) if current else None
        result = parse(m.content, self.context(roster), dm=m.dm)
        if isinstance(result, Command):
            self.acknowledge(m)
            await self.server.command(self, m, result, current)
        elif isinstance(result, NotInRoster):
            await self.say(m, await self.server.hint(self, m, result))
        elif not isinstance(result, NotCommand):
            await self.say(m, f"❌ {result.reason}")

    def acknowledge(self, m: Incoming) -> None:
        """👀 on a command the bot took (#466), started before the command's work, beside it:
        a task of its own, so that a slow Zulip delays nothing. The bots don't remove it."""
        task = asyncio.get_running_loop().create_task(self.react(m.id))
        self.acks.add(task)
        task.add_done_callback(self.acks.discard)

    async def react(self, message_id: int) -> None:
        """Best effort: never raised, never retried, and skipped while the request budget is
        down to the third of its window kept for answers, the reserve the participant bots'
        poster keeps from housekeeping (SPEC §4.10, #443): it never takes a slot an answer
        waits for."""
        budget = self.client.budget
        if budget.delay(budget.limit // 3) > 0:
            LOG.info(
                "%s bot: no reaction on %d: the budget is kept for answers", self.label, message_id
            )
            return
        try:
            await self.client.request("POST", f"messages/{message_id}/reactions", **ACK)
        except ZulipError as exc:
            LOG.warning("%s bot: a reaction failed (%s)", self.label, exc.code)
        except Exception as exc:
            LOG.warning("%s bot: a reaction failed (%s)", self.label, type(exc).__name__)

    async def say(self, m: Incoming, text: str) -> int | None:
        """Answer `m` where it was sent: in its topic, or by DM."""
        if m.dm:
            return await self.send(type="private", to=[m.sender_id], content=text)
        return await self.post(m.stream, m.topic, text)  # type: ignore[arg-type]

    async def post(self, stream: int, topic: str, text: str) -> int | None:
        return await self.send(type="stream", to=stream, topic=topic, content=text)

    async def edit(self, message_id: int, text: str) -> bool:
        """A progress edit, scrubbed and cut as `send`; best effort. Within the message's edit
        bounds (`ZulipClient.edit`, #754): one that must wait for its gap, or comes past the
        cap, is dropped, since the next progress edit holds everything so far, and the last one
        before the cap says that the progress is paused."""
        bounds = self.client.edit_bounds
        if bounds.wait(message_id) != 0:
            return False
        paused = PAUSED if bounds.last_update(message_id) else ""
        limit = self.client.limits.get("max_message_length", 10000) - len(paused)
        text = self.server.scrub(text)
        text = (text if len(text) <= limit else text[: limit - 2] + " …") + paused
        try:
            await self.client.edit(message_id, text)
        except Exception as exc:
            LOG.warning("%s bot: an edit failed (%s)", self.label, type(exc).__name__)
            return False
        return True

    async def send(self, **params: Any) -> int | None:
        """POST /messages, scrubbed (§2.7) and cut to Zulip's limit; retried after a 429. The
        message's ID, or None when it could not be sent."""
        return (await self.deliver(**params))[0]

    async def deliver(self, **params: Any) -> tuple[int | None, bool]:
        """`send`, and on a failure, whether the message surely never landed: Zulip refused it
        with a 4xx answer, before any processing, or no connection was made. Only then can it be
        sent again without being posted twice. Anything else may hide a message that landed: a
        5xx (a worker killed after the commit, a proxy's 502), a 2xx whose body can't be read, a
        response lost after the request went out (re-reviews of #354)."""
        limit = self.client.limits.get("max_message_length", 10000)
        text = self.server.scrub(params["content"])
        params["content"] = text if len(text) <= limit else text[: limit - 2] + " …"
        for attempt in range(3):
            try:
                return (await self.client.request("POST", "messages", **params))["id"], False
            except ZulipError as exc:
                if exc.status != 429 or attempt == 2:
                    LOG.warning("%s bot: a post failed (%s)", self.label, exc.code)
                    return None, 400 <= exc.status < 500
                await asyncio.sleep(max(1.0, exc.retry_after))
            except Exception as exc:
                LOG.warning("%s bot: a post failed (%s)", self.label, type(exc).__name__)
                return None, not_sent(exc)
        return None, True


def backoff(failures: int) -> float:
    """The listener's wait after `failures` errors in a row (1 or more): 1 s, then doubled,
    at most `BACKOFF_CAP`."""
    return min(BACKOFF_CAP, 2.0 ** min(failures - 1, 16))


def not_sent(exc: Exception) -> bool:
    """Raised before the request went out (no connection): sent again, a message can't be posted
    twice. The classes are looked up here, not at import (selfcheck's stand-in httpx)."""
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


class AdminServer:
    """Every admin bot of admin.toml, and what they share."""

    def __init__(
        self,
        config: AdminConfig,
        *,
        config_path: str,
        paths: Paths | None = None,
        modes: checks.Modes | None = None,
        connect: Callable[[str], ZulipClient] = ZulipClient.from_zuliprc,
        clock: Callable[[], float] = time.time,
        planner: Planner | None = None,
    ) -> None:
        self.config, self.config_path, self.connect = config, config_path, connect
        self.paths = paths = paths or Paths()
        self.modes = modes or checks.Modes()
        self.clock, self.planner = clock, planner
        self.scrubber = Scrubber()
        self.log = AuditLog(Path(paths.audit), scrub=self.scrubber)
        self.ledger = fleet.Ledger()
        self.store: PlanStore | None = None
        self.approvals: Approvals | None = None
        self.hints, self.foreign, self.refusals = Windows(), Windows(), Windows()
        self.users: dict[int, tuple[float, dict[str, Any] | None]] = {}
        self.lookups_failed: dict[int, float] = {}  # user ID → when its lookup last failed
        self.system_bots: set[int] = set()  # Zulip's own (`cross_realm_bots`), by user ID
        #: whether a register has read `cross_realm_bots`: only one per process asks for
        #: `realm_user`'s state, the whole user list (the re-review of #344, at §11's scale)
        self.system_bots_read = False
        self.system_bots_lock = asyncio.Lock()
        self._secrets_seen: tuple[Any, ...] = ()
        self.backups = backup.Backups(self)
        self.wakes = wakes.Monitor(self)
        self.waker = wakerdown.Watch(self)
        specs = [(ws.bot, ws) for ws in config.workshops]
        specs += [(config.global_bot, None)] if config.global_bot else []
        self.bots = [AdminBot(self, spec, ws) for spec, ws in specs]

    # ------------------------------------------------------------------------------ start

    async def preflight(self) -> None:
        """The startup checks, before anything else is read or written (`StartupError`): the
        owners and modes of admin.toml, the keys, the break-glass files and the start log's
        directory; backup_recipients; the scrubber's secrets; each bot's key and `GET
        /users/me`. `bot --check` runs these alone."""
        prov, token = self.paths.layout.provisioner_zuliprc, self.paths.github_token
        secrets = checks.secret_paths(self.config, prov, token)
        problems = checks.check_files(self.config, self.config_path, self.modes, secrets=secrets)
        problems += filter(None, [checks.check_start_log(self.paths.start_log, self.modes)])
        if problems:
            raise StartupError("; ".join(problems))
        try:
            await asyncio.to_thread(backup.read_recipients, self.paths.recipients, self.modes)
        except backup.BackupError as exc:
            raise StartupError(f"no backups could be made: {exc}") from None
        await asyncio.to_thread(self.reload_secrets)  # first: whatever fails next is scrubbed
        for bot in self.bots:  # only now: a key is read once its file has passed the checks
            try:
                bot.client = self.connect(bot.spec.zuliprc)
            except Exception as exc:  # configparser's errors quote the line: never their text
                why = error_kind(exc)
                raise StartupError(
                    f"{bot.label} bot: {bot.spec.zuliprc} unreadable ({why})"
                ) from None
        await asyncio.gather(*(bot.identify() for bot in self.bots))

    async def start(self) -> None:
        await self.preflight()
        problem = await asyncio.to_thread(_chain_problem, Path(self.paths.audit))
        if problem:
            raise StartupError(f"the audit log fails `audit verify`: {problem}")
        await asyncio.to_thread(self._read_ledger)
        await asyncio.to_thread(self.backups.resume)  # a backup owed from before the start
        floor = max(self.ledger.floor, fleet.plan_dirs_floor(self.paths.layout.runs))
        self.store = PlanStore(
            self.paths.state_db,
            ttl_minutes=self.config.plan_ttl_minutes,
            floor=floor,
            clock=self.clock,
        )
        self.approvals = Approvals(self.store, self.config)
        await self.copy_deploys()
        if self.planner is not None:
            await self.planner.start(self)
        LOG.info("%d admin bots started; plans above P-%d", len(self.bots), floor)

    def _read_ledger(self) -> None:
        for record in fleet.read_records(Path(self.paths.audit)):
            self.ledger.observe(record)
            self.backups.observe(record)
            self.wakes.observe(record)
            self.waker.observe(record)

    async def run(self) -> None:
        try:
            await self.start()
            async with asyncio.TaskGroup() as group:
                for bot in self.bots:
                    group.create_task(bot.listen())
                group.create_task(self.loop())
                group.create_task(self.wakes.run())
                group.create_task(self.waker.run())
        finally:
            with contextlib.suppress(Exception):
                await self.flush(every=True)
            await self.close()

    async def close(self) -> None:
        """The planner's tasks first (Codex round 2 on #346), and the reactions on their way,
        then what they use: the clients and the plan store."""
        if self.planner is not None:
            with contextlib.suppress(Exception):
                await self.planner.stop()
        acks = [task for bot in self.bots for task in bot.acks]
        for task in acks:
            task.cancel()
        await asyncio.gather(*acks, return_exceptions=True)
        for bot in self.bots:
            if hasattr(bot, "client"):
                with contextlib.suppress(Exception):
                    await bot.client.close()
        if self.store is not None:
            self.store.close()
            self.store = None

    async def loop(self) -> None:
        """Every few seconds: close the warning windows, reload the secrets if `out/`
        changed, check the channels' members when due, and let the planner expire plans."""
        while True:
            for name, step in (
                ("flush", self.flush),
                ("secrets", self.watch_secrets),
                ("membership", self.memberships),
                ("planner", self.planner.tick if self.planner else None),
            ):
                if step is None:
                    continue
                try:
                    await step()
                except Exception as exc:
                    LOG.warning("%s failed (%s)", name, type(exc).__name__)
            await asyncio.sleep(TICK)

    # -------------------------------------------------------------------- audit, secrets

    async def audit(self, event: str, **fields: Any) -> Head:
        """Append a record, in a thread (§2.8: `flock` and `fsync` block), and keep the
        ledger current."""
        head = await asyncio.to_thread(self.log.append, event, **fields)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))  # as at a restart
        self.ledger.observe({"event": event, "ts": ts, **fields})
        return head

    def scrub(self, text: str) -> str:
        return self.scrubber(text)

    def _secret_files(self) -> tuple[list[Path], list[Path]]:
        """§2.7's registry of the bot process: every admin bot's key, the provisioner's, each
        participant bot's in every `out/`, the GitHub token and the fleet's SSH key. Also every
        key in `secrets/bots/`: one the config helper adds at runtime (§2.9, #418) is
        registered before the restart that loads its bot (review of #424)."""
        bots = [bot.spec.zuliprc for bot in self.bots]
        zuliprcs = [Path(p) for p in (*bots, self.paths.layout.provisioner_zuliprc)]
        zuliprcs += sorted(Path(self.paths.secrets, "bots").glob("*.zuliprc"))
        for ws in self.config.workshops:
            zuliprcs += sorted(Path(ws.out_dir).glob("*/*.zuliprc"))
        return zuliprcs, [Path(self.paths.github_token), Path(self.paths.ssh_key)]

    def _secrets_signature(self) -> tuple[Any, ...]:
        zuliprcs, files = self._secret_files()
        seen = []
        for path in [*zuliprcs, *files, *(Path(ws.out_dir) for ws in self.config.workshops)]:
            try:
                st = path.stat()
            except OSError:
                seen.append((str(path),))
                continue
            seen.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
        return tuple(seen)

    def reload_secrets(self) -> None:
        """Read every secret this process can read into the scrubber (§2.7): at start, when
        `out/` changes (`watch_secrets`, the runner's progress edits too), and from the runner
        before an apply's result."""
        self._secrets_seen = self._secrets_signature()
        zuliprcs, files = self._secret_files()
        self.scrubber.set_secrets(read_secrets(zuliprcs=zuliprcs, files=files))

    async def watch_secrets(self) -> None:
        if await asyncio.to_thread(self._secrets_signature) != self._secrets_seen:
            await asyncio.to_thread(self.reload_secrets)
            LOG.info("secrets reloaded: %d registered", len(self.scrubber))

    # ----------------------------------------------------------------------- deploys.jsonl

    async def copy_deploys(self) -> None:
        """Each line of deploys.jsonl whose digest the chain lacks becomes an `upgrade` record
        (#338), in file order. By digest, not by line number: a server restored from another's
        backup (§1.7) has that server's lines in its chain, and its own file starts again at
        line 1. Each line carries its own time, so two lines never share a digest. The file
        is root's (deploy.sh's `record`): it is trusted only if it passes the owner and mode
        checks the other trusted files do. The config helper's `changes.jsonl` (§2.9, #418),
        root's too, is copied the same way, each line a `config` record."""
        sources = (
            (self.paths.deploys, "upgrade", fleet.DEPLOYS, "deploy"),
            # the config helper's record of each admin.toml change it made (§2.9, #418)
            (self.paths.config_changes, "config", fleet.CHANGES, "change"),
        )
        for name, event, source, key in sources:
            path = Path(name)
            if not await asyncio.to_thread(path.exists):
                continue
            modes = checks.Modes(self.modes.owners, self.modes.group, top=str(path.parent))
            problem = await asyncio.to_thread(checks.check_file, path, modes)
            if problem:
                LOG.warning("%s not copied: %s", path.name, problem)
                continue
            try:  # unreadable (a mode the bot can't read): said, never a failed start (#424)
                lines = await asyncio.to_thread(_read_lines, str(path))
            except OSError as exc:
                LOG.warning("%s not copied: %s", path.name, exc.strerror)
                continue
            for n, line in lines:
                digest = hashlib.sha256(line).hexdigest()
                if digest in self.ledger.deployed:
                    continue
                fields = {key: _line_value(line)}
                await self.audit(event, source=source, line=n, line_sha256=digest, **fields)

    @property
    def release(self) -> str:
        """The release this server runs, as the last deploy recorded it."""
        return self.ledger.release or f"v{zab.__version__}+unrecorded"

    # ---------------------------------------------------------------------- the fleet

    def fleet(self, ws: Workshop) -> Fleet:
        """`ws`'s fleet, on the event loop; `read_fleet` reads the files in a thread."""
        return fleet.read_fleet(self.config, ws, self.ledger, now=self.clock())

    async def read_fleet(self, ws: Workshop) -> Fleet:
        """`ws`'s fleet: the ledger's record of it copied here, on the event loop, which alone
        changes it (`audit`); the roster and spares read in a thread, from that copy (the
        review of #344: a thread reading the ledger itself could see a record half taken)."""
        ledger, now = self.ledger.copy_of(ws.name), self.clock()
        return await asyncio.to_thread(fleet.read_fleet, self.config, ws, ledger, now)

    def bot_of(self, ws: Workshop) -> AdminBot:
        return next(bot for bot in self.bots if bot.workshop is ws)

    async def user(self, bot: AdminBot, user_id: int) -> dict[str, Any] | None:
        """`GET /users/{id}`, cached for a window; None when it fails, and then for
        `LOOKUP_RETRY` without asking again (#347)."""
        now = self.clock()
        hit = self.users.get(user_id)
        if hit is not None and now - hit[0] < WINDOW:
            return hit[1]
        if now - self.lookups_failed.get(user_id, -LOOKUP_RETRY) < LOOKUP_RETRY:
            return None
        try:
            user = (await bot.client.request("GET", f"users/{user_id}")).get("user")
        except Exception as exc:
            LOG.warning("%s bot: GET /users failed (%s)", bot.label, type(exc).__name__)
            failed = self.lookups_failed  # only those of the last minute are kept
            self.lookups_failed = {u: t for u, t in failed.items() if now - t < LOOKUP_RETRY}
            self.lookups_failed[user_id] = now
            return None
        user = user if isinstance(user, dict) else None
        self.users[user_id] = (now, user)
        return user

    # ---------------------------------------------------------------------- commands

    async def command(self, bot: AdminBot, m: Incoming, command: Command, fl: Fleet | None) -> None:
        read = reads.READS.get(command.verb)
        if read is None:
            if self.planner is None:
                await bot.say(m, f"⚠ `!{command.verb}` is not available on this server yet")
            else:
                await self.planner.handle(bot, m, command, fl)
            return
        text = await read(self, bot, m, command, fl)
        head = await self.audit(
            "read",
            workshop=bot.where,
            bot=bot.user_id,
            requester={"id": m.sender_id, "name": m.sender_name},
            verb=command.verb,
            args=list(command.tokens()),
        )
        await bot.say(m, f"{text}\n{footer(head)}")

    async def hint(self, bot: AdminBot, m: Incoming, refused: NotInRoster) -> str:
        """The refusal of a slug this roster lacks, with the hint to the workshop that has it,
        at most once per sender every window (§2.1)."""
        pointer = await self.pointer(bot, m, refused.slug)
        return f"❌ {refused.reason}" + (f"\n{pointer}" if pointer else "")

    async def pointer(self, bot: AdminBot, m: Incoming, slug: str, only: str = "") -> str:
        """The hint, `` `<slug>` is in <ws>: ask <its bot> ``, for a slug that another
        workshop's roster has (`only`: that workshop, which a script named), at most once per
        sender every window, server-wide (§2.1); "" otherwise. The window is spent only when a
        roster has the slug."""
        for ws in self.config.workshops:
            if ws is bot.workshop or (only and ws.name != only):
                continue
            if slug not in await asyncio.to_thread(fleet.roster_slugs, ws):
                continue
            window, _ = self.hints.take(m.sender_id, self.clock(), m.id)
            if window.messages > 1:
                return ""
            owner = self.bot_of(ws)
            return f"`{slug}` is in {ws.name}: ask {mention(owner.user_id, owner.full_name)}"
        return ""

    # ---------------------------------------------------------------------- strangers

    async def stranger(self, bot: AdminBot, m: Incoming) -> None:
        """A message from someone the bot doesn't recognize: never obeyed (§2.1, §2.4). A
        human's command is refused, anything else in the channel warned about; each at most
        once per sender, bot and window, with one record per window that counts the messages.
        Zulip's own system bots are ignored.

        The window `take` closes is recorded here. If that record fails, the window is opened
        again, merged into the sender's new one (`Windows.reopen`), as `flush` does: the next
        record counts both. The failure is logged by its type, and the message is still
        answered (#510). Unlike `flush`'s merge, this one keeps the new window's start: it
        decides when the sender is answered again, so that while the audit log fails a sender
        is still answered once per window, not at every message (Codex round 1 on #579)."""
        if m.sender_id in self.system_bots:
            return
        user = await self.user(bot, m.sender_id)
        human = user is not None and user.get("is_bot") is False
        command = not isinstance(parse(m.content, bot.context(None), dm=m.dm), NotCommand)
        if human and command:
            windows, event, answer = (
                self.refusals,
                "refused",
                (f"🚫 {shown_name(m.sender_name)} is not an organizer here; nothing was done"),
            )
        elif not m.dm:
            windows, event, answer = self.foreign, "foreign", FOREIGN
        else:
            return  # a bot's DM, or talk: nobody else sees it, and nothing is done
        key = (bot.user_id, m.sender_id)
        window, closed = windows.take(key, self.clock(), m.id, m.sender_name)
        first = window.messages == 1  # before a merge (below) adds the closed window's count
        if closed is not None:
            try:
                await self._close(event, key, closed)
            except Exception as exc:  # a cancellation is not caught: as in `flush`
                LOG.warning("%s bot: a %s record failed (%s)", bot.label, event, type(exc).__name__)
                start = window.start
                windows.reopen(key, closed)
                window.start = start  # still the new window's: it decides the next answer
        if first:
            await bot.say(m, answer)

    async def _close(self, event: str, key: Hashable, window: Window) -> None:
        """The one record of a closed window: `refused` or `foreign` (§2.1, §2.4)."""
        bot_id, sender_id = key  # type: ignore[misc]
        bot = next(b for b in self.bots if b.user_id == bot_id)
        reason = {"reason": "not an organizer here"} if event == "refused" else {}
        await self.audit(
            event,
            workshop=bot.where,
            bot=bot_id,
            sender={"id": sender_id, "name": window.name},
            messages=window.messages,
            first=window.first,
            last=window.last,
            **reason,
        )

    async def flush(self, *, every: bool = False) -> None:
        """One record per sender, bot and closed window: `refused` and `foreign` (§2.1). Each
        window is closed as its record is written, not all of them first, so that a record
        that fails leaves it and the rest open, for the next flush (the re-review of #344);
        merged into the sender's new window if they posted meanwhile (`Windows.reopen`).

        - **An append that fails after its line is whole** (its `fsync` or `close`) has its
          window reopened all the same, and its record is written again: a duplicate, with the
          same sender and `first`. When the sender didn't post during that append it is the
          same record (`last` and `messages` too); when they did, the window was merged, and
          the later record's range contains the earlier one's. Either way it is harmless:
          each line is chained, so `audit verify` passes, and nothing counts these records
          (the ledger, the backups and the wake checks read neither event).
        - **A cancellation never reopens a window.** Its append, once a worker thread has
          started it, goes on and is written. One still queued behind busy workers is dropped
          with its window. `loop()`'s periodic flush is cancelled when the run stops (a
          signal, or another of its tasks failing). The shutdown flush is never cancelled by
          a second signal (`serve`), but a run that a failed task ended has seen no signal
          yet, so a first one during its shutdown flush still cancels that. Either way the
          drop needs a cancellation while every worker is busy and a window is due."""
        now = self.clock()
        for event, windows in (("refused", self.refusals), ("foreign", self.foreign)):
            for key, window in windows.due(now, every=every):
                if not windows.close(key, window):
                    continue  # `take` closed it meanwhile, and wrote its record
                try:
                    await self._close(event, key, window)
                except Exception:
                    windows.reopen(key, window)  # not written: tried again next time
                    raise
        for key, window in self.hints.due(now):
            self.hints.close(key, window)

    # --------------------------------------------------------------------- membership

    async def memberships(self) -> None:
        now = self.clock()
        for bot in self.bots:
            if now >= bot.next_membership:
                bot.next_membership = now + MEMBERSHIP
                await self.membership(bot)

    async def membership(self, bot: AdminBot) -> list[int]:
        """Warn in each of the bot's channels about any subscriber it doesn't recognize, and if
        it is not private (§2.1): in a public or web-public channel every member of the
        organization reads the plans without subscribing (#347). The extra subscribers' IDs,
        all channels together."""
        extra: list[int] = []
        for channel in sorted(bot.spec.channels):
            try:
                about = (await bot.client.request("GET", f"streams/{channel}")).get("stream")
                if not isinstance(about, dict):
                    raise ValueError("no stream in the answer")
            except Exception as exc:
                LOG.warning("%s bot: channel %d unknown (%s)", bot.label, channel, exc)
            else:  # a web-public channel is a public one: never `invite_only`
                if about.get("invite_only") is not True:
                    LOG.warning("%s bot: channel %d is not private", bot.label, channel)
                    await bot.post(channel, "membership", PUBLIC)
            try:
                data = await bot.client.request("GET", f"streams/{channel}/members")
            except Exception as exc:
                LOG.warning("%s bot: members of %d unknown (%s)", bot.label, channel, exc)
                continue
            members = data.get("subscribers")
            ids = {u for u in members if type(u) is int} if isinstance(members, list) else set()
            others = sorted(ids - bot.recognized - {bot.user_id})
            if not others:
                continue
            extra += others
            names = []
            for uid in others:
                user = await self.user(bot, uid)
                name = user.get("full_name") if user else None
                names.append(mention(uid, name if isinstance(name, str) else f"user {uid}"))
            await bot.post(
                channel,
                "membership",
                f"⚠ subscribed here, but not recognized by this bot: {', '.join(names)}. "
                "Only organizers, global admins and this bot belong in this channel; "
                "nothing was changed.",
            )
        return extra


def _chain_problem(path: Path) -> str | None:
    """Why the audit log's chain doesn't hold; None when it does, or on a new server."""
    return verify(path).problem if path.exists() else None


#: How deep a copied line may nest: deploy.sh's and the helper's lines nest 3 deep at most. A
#: deeper one would reach the scrubber's and the encoder's recursion limits (`AuditLog.append`).
LINE_DEPTH = 16


def _line_value(line: bytes) -> dict[str, Any]:
    """A trusted file's line as the value of its audit record: its JSON object, else
    `unreadable` with its text. Never an exception, so that no line stops the bots from
    starting (review of #424): not JSON, nested deeper than `LINE_DEPTH` (or than the parser
    goes: RecursionError), or holding what the audit log refuses to encode (NaN, Infinity,
    1e999: `allow_nan=False`)."""
    try:
        value = json.loads(line)
        if not isinstance(value, dict) or _deeper_than(value, LINE_DEPTH):
            raise ValueError("not a JSON object the audit log keeps")
        json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError):
        return {"unreadable": line.decode("utf-8", "replace")[:2000]}
    return value


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether `value` nests more than `limit` deep; without recursion."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(item, dict):
            stack += [(v, depth + 1) for v in item.values()]
        elif isinstance(item, list):
            stack += [(v, depth + 1) for v in item]
    return False


def _read_lines(path: str) -> list[tuple[int, bytes]]:
    """The file's non-blank lines, each with its line number in the file."""
    try:
        with open(path, "rb") as f:
            data = f.read(16 * 1024 * 1024)
    except FileNotFoundError:
        return []
    return [(n, line) for n, line in enumerate(data.split(b"\n"), 1) if line.strip()]


class _ScrubLogs(logging.Filter):
    """Every log line through the scrubber (§2.7), tracebacks included."""

    def __init__(self, scrub: Callable[[str], str]) -> None:
        super().__init__()
        self.scrub_text = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self.scrub_text(record.getMessage()), None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_info, record.exc_text = None, self.scrub_text(text)
        return True


async def serve(server: AdminServer) -> None:
    """Run `server` until it fails, or until SIGTERM (systemd's stop) or SIGINT. The first
    signal cancels the run, whose `finally` writes the open windows' records (`flush`) and
    closes the server before the process ends; without a handler, SIGTERM would end it at once
    and lose them. A second signal is only logged: cancelling again would cut that flush and
    skip the close (the re-review of #344). systemd's stop timeout still ends a stop that
    hangs."""
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    assert task is not None
    stopped: list[int] = []

    def stop(signum: int) -> None:
        stopped.append(signum)
        if len(stopped) == 1:  # a second signal must not cut the flush the first one started
            task.cancel()
        else:
            LOG.info("signal %d: already stopping", signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop, signum)
    try:
        await server.run()
    except asyncio.CancelledError:
        if not stopped:
            raise
        task.uncancel()
        LOG.info("stopped by signal %d", stopped[0])
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def restart_self() -> None:
    """SIGTERM to this process, after a `!new-workshop` apply (§2.9): `serve` stops it as
    systemd's stop would, the windows' records written, and systemd starts it again
    (`Restart=always`), with admin.toml as the config helper left it."""
    os.kill(os.getpid(), signal.SIGTERM)


async def check(server: AdminServer) -> int:
    """`bot --check`: the startup checks alone (`AdminServer.preflight`), for an operator to run
    as `zab-admin`; 0 when the bots would start."""
    try:
        await server.preflight()
    except StartupError as exc:
        print(f"bot check: {exc}")
        return 1
    finally:
        await server.close()
    print(f"bot check: OK ({len(server.bots)} bots)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`python -m zab.admin bot --config PATH`: run every admin bot until stopped. Every
    failure is logged through the scrubber (`_ScrubLogs`), its traceback included."""
    parser = argparse.ArgumentParser(prog="python -m zab.admin bot")
    parser.add_argument("--config", default="/etc/zab-admin/admin.toml")
    parser.add_argument(
        "--check", action="store_true", help="run the startup checks only (as zab-admin), then exit"
    )
    options = parser.parse_args(argv)
    path = options.config
    try:
        config = load_admin_config(path)
    except AdminConfigError as exc:  # admin.toml holds no secrets (§1.3)
        print(f"zab-admin-bot: {exc}", flush=True)
        return 1
    server = AdminServer(config, config_path=path)
    tokens = Path(server.paths.state, "console", "tokens")  # the console's (§1.3, §4.4)
    console = (
        SignIns(tokens, config.console_url, clock=server.clock) if config.console_url else None
    )
    lookups = {"vms": provider.targets, "tagged": provider.tagged,  # !retire's (#559)
               "addressed": provider.addressed}  # fmt: skip
    server.planner = planning.Planning(
        runner.Runner(server, **lookups), console=console, restart=restart_self
    )
    handler = logging.StreamHandler()
    handler.addFilter(_ScrubLogs(server.scrub))
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]  # the only one: every record goes through the scrubber
    root.setLevel(logging.INFO)
    try:
        if options.check:  # inside the catch-all too: its traceback is scrubbed (#347)
            return asyncio.run(check(server))
        asyncio.run(serve(server))
    except StartupError as exc:
        LOG.error("not started: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    except BaseException:  # never sys.excepthook: its traceback would bypass the scrubber
        LOG.exception("stopped by an error")
        return 1
    return 0
