"""The wake monitor (#359; docs/ADMIN.md §1.2, §1.5): each VM start that the starter made,
checked against the participant's own Zulip messages, independently of the waker.

The waker reads every participant bot's messages, so it is the most exposed service, and it can
only ask `zab-admin-starter` for starts. A compromised waker could keep starting stopped VMs:
idle stop bounds the cost, but nobody would notice. So the bots' process, as `zab-admin`, reads
the starter's start log (`zab.admin.starter`), which nothing of the waker's can write
(`checks.check_start_log`), and checks each start:

- **Opted in?** A start the waker asked for, of a VM that the waker's config
  (`/var/lib/zab-admin/waker/config.toml`, which the bots write and the waker only reads) doesn't
  list, alerts at once: the waker has no business starting it. When the config changed after
  the start, this can't be told, and the start is checked as any other.
- **The owner** is the participant: the `zulip_user_id` of the roster row named by the VM's
  `participant` tag, the sender the waker obeys; never Zulip's `bot_owner_id` (the participant
  bots are the provisioner's, #364). A row without one is a failed check, which alerts.
- **A match** is a message from that user, sent from `WINDOW` before the start to `SKEW` after
  it, that one of the VM's two bots received. `GET /messages` with that bot's own key from the
  workshop's `out/`, narrowed to the sender, reads only what the bot received: a DM, or a
  message in a channel the bot is subscribed to, mentioned or not, as the waker's queues
  deliver them (SPEC §13.4).
- **No match:** an alert to the global admins, in the global admin bot's channel (in the
  workshop bot's when admin.toml has no `[global]`), at most one per VM every `ALERT_EVERY`
  seconds, by the starts' times. The next one counts and lists the starts held back meanwhile.
  An alert Zulip doesn't take is posted again on each pass (`conclude`).
- **A check that fails** alerts with its reason: at once when another try can't help (no key,
  no roster row, no `zulip_user_id`), else (a Zulip error, a timeout) after `ATTEMPTS` passes.
  A start log that can't be trusted or read alerts too, at most once every `ALERT_EVERY`.
- **Nothing ages out silently.** A start still unchecked `HORIZON` after it was made (a check
  that kept failing, a backlog, the bots down for longer) is recorded and counted in one summary
  alert for the pass's batch (`expire`), at most one every `ALERT_EVERY`; the next counts those
  held back. Only a start that left the log's two files before the bots read it is lost.
- **Either way,** `wake_check` audit records: the start, the outcome, the matching message's ID
  and time, or the reason, and the alert's message ID. These records are the monitor's memory
  across restarts: the starts it checked, and the last alerts.

A message's text never reaches an alert, a record or a log line: only IDs and times.

**Bounds,** per pass, every `PERIOD` seconds: the start log's two files, each read up to
`MAX_READ` bytes; starts of the last `HORIZON` seconds only, at most `PER_PASS` of them, oldest
first; at most `CALLS` `GET /messages` in all, `PAGES` of `PAGE` messages per bot and start;
`CHECK_S` seconds per check, `PASS_S` per pass; `EXPIRE_PER_PASS` aged-out starts in one
record. What is left waits for the next pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import stat
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zab.admin import starter
from zab.admin.actions import BOT_KINDS
from zab.admin.checks import Modes, check_start_log
from zab.admin.fleet import FleetError, read_roster
from zab.admin.grammar import SLUG_RE, WS_NAME_RE
from zab.zulip.client import ZulipError, error_kind

if TYPE_CHECKING:
    from zab.admin.bot import AdminBot, AdminServer

LOG = logging.getLogger("zab.admin.wakes")
PERIOD = 120.0
WINDOW = 600.0  # "about 10 minutes" before the start (#359): the waker's longest path is ~9
SKEW = 30.0  # seconds after the start: Zulip's clock and the server's
HORIZON = 6 * 3600.0
PER_PASS = 20
CALLS = 40
PAGE, PAGES = 50, 2
CHECK_S, PASS_S = 20.0, 90.0
ALERT_EVERY = 3600.0
ATTEMPTS = 3
EXPIRE_PER_PASS = 200
MAX_READ = 2 * starter.LOG_MAX
MAX_CONFIG = 1024 * 1024  # bytes of the waker's config
LISTED = 10  # starts named in one alert
TOPIC = "wake monitor"
WAKER = "zab-waker"  # the start log's `peer` for the waker's starts
LOG_KEY = "start log"  # the alert limiter's keys: a problem with the log itself,
EXPIRED_KEY = "expired"  # starts that aged out unchecked,
OPT_IN = " not opted in"  # and, after an instance, a start of a VM not opted in
_INSTANCE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", re.ASCII)
_PEER = re.compile(r"[a-z_][a-z0-9_-]{0,31}|[0-9]{1,10}", re.ASCII)


@dataclass(frozen=True, slots=True)
class Start:
    """One line of the start log. `participant` is None when the tag is no slug."""

    t: float
    workshop: str
    participant: str | None
    instance: str
    peer: str

    @property
    def key(self) -> tuple[str, float]:
        return self.instance, self.t

    @classmethod
    def of(cls, data: Any) -> Start | None:
        """The line's start, or None when it is not one the starter writes."""
        if not isinstance(data, dict):
            return None
        t, ws, instance = data.get("t"), data.get("workshop"), data.get("instance")
        if type(t) not in (int, float) or not math.isfinite(t):
            return None
        if not (isinstance(ws, str) and WS_NAME_RE.fullmatch(ws)):
            return None
        if not (isinstance(instance, str) and _INSTANCE.fullmatch(instance)):
            return None
        slug, peer = data.get("participant"), data.get("peer")
        slug = slug if isinstance(slug, str) and SLUG_RE.fullmatch(slug) else None
        peer = peer if isinstance(peer, str) and _PEER.fullmatch(peer) else "?"
        return cls(float(t), ws, slug, instance, peer)

    def fields(self) -> dict[str, Any]:
        return {"workshop": self.workshop, "instance": self.instance,
                "participant": self.participant, "peer": self.peer, "started": iso(self.t),
                "start_t": self.t}  # fmt: skip

    def named(self) -> str:
        return f"`{self.instance}` ({self.workshop}, `{self.participant or '?'}`)"


def read_log(directory: str, modes: Modes) -> tuple[list[Start], str]:
    """The starts in the log's two files, older first, and a problem with the log, or "". A
    last line without its newline is a write in progress, left for the next pass."""
    if problem := check_start_log(directory, modes):
        return [], problem
    starts: list[Start] = []
    bad = 0
    for name in (starter.LOG_NAME, starter.LOG_NAME + ".1"):  # current first: a rotation between
        path = Path(directory) / name  # the two reads repeats lines, and never loses one
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return [], f"{path}: {exc.strerror}"
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != modes.starter or st.st_mode & 0o022:
                return [], f"{path} is not a file of the starter's alone"
            data = f.read(MAX_READ)
        for line in data[: data.rfind(b"\n") + 1].splitlines():
            try:
                start = Start.of(json.loads(line))
            except ValueError:
                start = None
            if start is None:
                bad += 1
            else:
                starts.append(start)
    unique = {s.key: s for s in starts}
    found = sorted(unique.values(), key=lambda s: s.t)
    return found, f"{bad} of its lines are not starts" if bad else ""


def read_opt_ins(path: str) -> tuple[frozenset[tuple[str, str]] | None, float]:
    """The waker's opt-ins, (workshop, instance), and its config's mtime; no config, none.
    None when it can't be read: then no start is judged by it."""
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            data = tomllib.loads(f.read(MAX_CONFIG + 1).decode())
    except FileNotFoundError:
        return frozenset(), 0.0  # the waker idles without one: it starts nothing
    except (OSError, ValueError):
        return None, 0.0
    pairs = set()
    for ws in data.get("workshop") or ():
        bots = ws.get("bots") if isinstance(ws, dict) else None
        for bot in bots if isinstance(bots, list) else ():
            if isinstance(bot, dict) and isinstance(bot.get("instance_id"), str):
                pairs.add((str(ws.get("name")), bot["instance_id"]))
    return frozenset(pairs), st.st_mtime


@dataclass(frozen=True, slots=True)
class Outcome:
    """`matched` (`bot`, `message`, `sent`), `unmatched`, `not_opted_in`, or `failed`
    (`reason`; `transient` when another try may pass: a Zulip error or a timeout)."""

    kind: str
    owner: int | None = None
    bot: str = ""
    message: int | None = None
    sent: float | None = None
    reason: str = ""
    transient: bool = False


class Spent(Exception):
    """The pass's Zulip calls are spent: the start waits for the next pass."""


class Calls:
    def __init__(self, left: int) -> None:
        self.left = left

    def take(self) -> None:
        if self.left <= 0:
            raise Spent
        self.left -= 1


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def limiter_key(start: Start, outcome: Outcome) -> str:
    return start.instance + (OPT_IN if outcome.kind == "not_opted_in" else "")


class Monitor:
    """The bots' process's wake monitor (module doc). `observe` takes the audit log at start."""

    def __init__(self, server: AdminServer) -> None:
        self.server = server
        self.checked: dict[tuple[str, float], float] = {}  # start → its time
        self.alerted: dict[str, float] = {}  # limiter key → its last alert
        self.held: dict[str, list[float]] = {}  # limiter key → its starts held back since
        self.failures: dict[tuple[str, float], tuple[int, str]] = {}  # start → tries, reason
        #: Alerts Zulip didn't take: posted again each pass, before any new check (Codex round 1)
        self.undelivered: dict[tuple[str, float], tuple[Start, Outcome]] = {}
        #: Records the audit log refused, as decided: written again first (Codex round 5)
        self.unrecorded: dict[tuple[str, float], tuple[list[Start], dict[str, Any]]] = {}

    @property
    def clock(self) -> Callable[[], float]:
        return self.server.clock

    def observe(self, record: dict[str, Any]) -> None:
        """One audit record: a `wake_check`'s starts are checked, and its alert counts. (A
        problem of the log's alerts again after a restart.)"""
        if record.get("event") != "wake_check":
            return
        if record.get("outcome") == "expired":
            starts = record.get("starts") if isinstance(record.get("starts"), list) else []
            times = [s.get("start_t") for s in starts if isinstance(s, dict)]
            for s in starts:
                if isinstance(s, dict) and isinstance(s.get("instance"), str):
                    if type(s.get("start_t")) in (int, float):
                        self.checked[(s["instance"], float(s["start_t"]))] = float(s["start_t"])
            self._limited(EXPIRED_KEY, record, record.get("at"), times)
            return
        instance, t = record.get("instance"), record.get("start_t")
        if not isinstance(instance, str) or type(t) not in (int, float):
            return
        self.checked[(instance, float(t))] = float(t)
        key = instance + (OPT_IN if record.get("outcome") == "not_opted_in" else "")
        self._limited(key, record, t, [t])

    def _limited(self, key: str, record: dict[str, Any], stamp: Any, times: list[Any]) -> None:
        if type(record.get("alert")) is int and type(stamp) in (int, float):
            self.alerted[key] = max(self.alerted.get(key, -math.inf), float(stamp))
            self.held[key] = []
        elif record.get("held") is True:
            self.held.setdefault(key, []).extend(float(t) for t in times if type(t) in (int, float))

    async def run(self) -> None:
        """A pass every `PERIOD` seconds, for as long as the bots run; a pass that fails is
        logged, and the next one comes. Its bounds are `asyncio.timeout`, never
        `asyncio.wait_for`: on Python 3.11 that loses a stop that comes as a pass or a check
        ends (gh-86296), and this loop, so the server, never ended (#491)."""
        while True:
            try:
                async with asyncio.timeout(PASS_S + CHECK_S + 30):
                    await self.check_pass()
            except Exception as exc:
                LOG.warning("a wake check pass failed (%s)", type(exc).__name__)
            await asyncio.sleep(PERIOD)

    async def check_pass(self) -> None:
        now = self.clock()
        paths, modes = self.server.paths, self.server.modes
        starts, problem = await asyncio.to_thread(read_log, paths.start_log, modes)
        for decided, record in list(self.unrecorded.values()):
            await self.write(decided, record)  # a failure again ends the pass, as the first did
        if problem:
            await self.conclude(None, Outcome("failed", reason=problem))
        for start, outcome in sorted(self.undelivered.values(), key=lambda p: p[0].t)[:PER_PASS]:
            await self.conclude(start, outcome)  # posted, held, or past HORIZON: recorded
        if starts or not problem:  # what left the log is forgotten; a log not read, nothing
            present = {s.key for s in starts}
            for seen in (self.checked, self.failures):
                for key in [k for k in seen if k not in present]:
                    del seen[key]
        done = self.checked.keys() | self.undelivered.keys()
        done |= {s.key for decided, _ in self.unrecorded.values() for s in decided}
        open_ = [s for s in starts if s.key not in done]
        if expired := [s for s in open_ if s.t < now - HORIZON]:
            await self.expire(expired[:EXPIRE_PER_PASS], now)
        due = [s for s in open_ if s.t >= now - HORIZON]
        config = f"{paths.layout.waker}/config.toml"
        opt_ins, changed = await asyncio.to_thread(read_opt_ins, config)
        calls, deadline = Calls(CALLS), time.monotonic() + PASS_S
        for start in due[:PER_PASS]:
            if time.monotonic() >= deadline:
                break
            if start.peer == WAKER and opt_ins is not None and changed <= start.t:
                if (start.workshop, start.instance) not in opt_ins:
                    await self.conclude(start, Outcome("not_opted_in"))
                    continue
            try:
                async with asyncio.timeout(CHECK_S):
                    outcome = await self.check(start, calls)
            except Spent:
                break
            except TimeoutError:
                reason = f"no answer within {CHECK_S:.0f} s"
                outcome = Outcome("failed", reason=reason, transient=True)
            if outcome.transient:
                tries = self.failures.get(start.key, (0, ""))[0] + 1
                self.failures[start.key] = (tries, outcome.reason)
                if tries < ATTEMPTS:
                    continue
            await self.conclude(start, outcome)

    async def check(self, start: Start, calls: Calls) -> Outcome:
        ws = self.server.config.workshop(start.workshop)
        if ws is None:
            return Outcome("failed", reason=f"{start.workshop} is not in admin.toml")
        if start.participant is None:
            return Outcome("failed", reason="its participant tag is no roster slug")
        try:
            rows = await asyncio.to_thread(read_roster, ws.roster)
        except FleetError as exc:
            return Outcome("failed", reason=f"the roster: {exc}")
        except Exception as exc:  # csv's errors quote the line: never their text
            return Outcome("failed", reason=f"the roster: {type(exc).__name__}")
        row = next((r for r in rows if r["slug"] == start.participant), None)
        if row is None:
            return Outcome("failed", reason=f"no roster row {start.participant}")
        uid = row.get("zulip_user_id", "")
        if not (uid.isdecimal() and int(uid) > 0):
            return Outcome("failed", reason="its roster row has no zulip_user_id")
        owner, reasons, transient = int(uid), [], False
        for kind in BOT_KINDS:
            path = f"{ws.out_dir}/{start.participant}/{kind}.zuliprc"
            try:
                found = await self.search(path, owner, start, calls)
            except Spent:
                raise
            except FileNotFoundError:
                reasons.append(f"no {kind}.zuliprc in its out/")
                continue
            except ZulipError as exc:
                reasons.append(f"{kind} bot: Zulip {exc.code}")
                transient = True
                continue
            except Exception as exc:  # an outage, an unreadable key: never their text
                reasons.append(f"{kind} bot: {error_kind(exc)}")
                transient = True
                continue
            if isinstance(found, str):
                reasons.append(f"{kind} bot: {found}")
            elif found is not None:
                return Outcome("matched", owner, kind, found["id"], found["timestamp"])
        if reasons:
            return Outcome("failed", owner, reason="; ".join(reasons), transient=transient)
        return Outcome("unmatched", owner)

    async def search(
        self, path: str, owner: int, start: Start, calls: Calls
    ) -> dict[str, Any] | str | None:
        """The owner's first message in the window that the bot of `path` received; None when
        there is none; a reason when it can't tell."""
        st = await asyncio.to_thread(os.lstat, path)  # FileNotFoundError: the reason
        if not stat.S_ISREG(st.st_mode):
            return f"{Path(path).name} is not a file"
        client = self.server.connect(path)
        try:
            anchor: int | str = "newest"
            for _ in range(PAGES):
                calls.take()
                data = await client.request(
                    "GET",
                    "messages",
                    anchor=str(anchor),
                    num_before=PAGE,
                    num_after=0,
                    narrow=json.dumps([{"operator": "sender", "operand": owner}]),
                    apply_markdown=False,
                    client_gravatar=True,
                )
                messages = [m for m in data.get("messages") or () if isinstance(m, dict)]
                for m in messages:
                    sent = m.get("timestamp")
                    if (
                        m.get("sender_id") == owner
                        and type(sent) is int
                        and type(m.get("id")) is int
                    ):
                        if start.t - WINDOW <= sent <= start.t + SKEW:
                            return {"id": m["id"], "timestamp": sent}
                times = [m["timestamp"] for m in messages if type(m.get("timestamp")) is int]
                ids = [m["id"] for m in messages if type(m.get("id")) is int]
                if data.get("found_oldest") or not ids or min(times, default=0) < start.t - WINDOW:
                    return None
                anchor = min(ids) - 1
            return f"more than {PAGE * PAGES} of the participant's messages since the start"
        finally:
            await client.close()

    def channel_bot(self, workshop: str | None) -> AdminBot | None:
        """The global admin bot; without one, the workshop's bot, else the first bot."""
        bots = self.server.bots
        named = [b for b in bots if b.workshop is None]
        if not named and workshop is not None:
            named = [b for b in bots if b.workshop and b.workshop.name == workshop]
        return (named or bots or [None])[0]

    async def post(self, workshop: str | None, text: str) -> int | None:
        bot = self.channel_bot(workshop)
        if bot is None or not bot.spec.channels:
            return None
        return await bot.post(sorted(bot.spec.channels)[0], TOPIC, text)

    async def conclude(self, start: Start | None, outcome: Outcome) -> None:
        """The alert, when one is due, and the record. A VM's alerts are limited by the times
        of its starts; the log's, by the pass's. An alert Zulip doesn't take is kept, with no
        record, and posted again on each pass until it goes out, or until `HORIZON` after its
        start, when its record says `alert: null` (Codex round 1). A restart checks such a
        start again, since nothing recorded it. A problem of the log's is recorded only with
        its alert: it comes back every pass."""
        fields: dict[str, Any] = {"workshop": "", "outcome": outcome.kind}
        key, stamp = LOG_KEY, self.clock()
        if start is not None:
            key, stamp = limiter_key(start, outcome), start.t
            fields |= start.fields()
        if outcome.owner is not None:
            fields["owner"] = outcome.owner
        if outcome.kind == "matched":
            sent = iso(outcome.sent or 0)
            await self.record(start, fields, via=outcome.bot, message=outcome.message, sent=sent)
            return
        if outcome.kind != "not_opted_in":
            fields["reason"] = outcome.reason or "no message from the participant"
        if stamp - self.alerted.get(key, -math.inf) < ALERT_EVERY:
            if start is not None:
                self.held.setdefault(key, []).append(start.t)
                await self.record(start, fields, held=True)
            return
        held = self.held.get(key, [])
        posted = await self.post(start.workshop if start else None,
                                 alert_text(start, outcome, held))  # fmt: skip
        if posted is not None:
            self.alerted[key], self.held[key] = stamp, []
        else:
            LOG.warning("a wake alert was not posted (%s)", key)
            if start is None:
                return
            if start.t >= self.clock() - HORIZON:
                self.undelivered[start.key] = (start, outcome)
                return
        await self.record(start, fields, alert=posted, held_before=len(held))

    async def record(self, start: Start | None, fields: dict[str, Any], **more: Any) -> None:
        await self.write([start] if start else [], fields | more)

    async def expire(self, starts: list[Start], now: float) -> None:
        """Starts still unchecked `HORIZON` after they were made: one record listing them, and
        one summary alert, at most one every `ALERT_EVERY`; the next counts those held back.
        A summary Zulip doesn't take leaves its starts unrecorded, so the next pass, or a
        restart, finds them again and posts it again, until it goes out or its oldest start is
        twice `HORIZON` old: then they are recorded with `alert: null` (Codex round 4)."""
        listed = [s.fields() | {"reason": self.failures.get(s.key, (0, ""))[1]
                                or "not checked in time"} for s in starts]  # fmt: skip
        fields = {"workshop": "", "outcome": "expired", "starts": listed, "at": now}
        if now - self.alerted.get(EXPIRED_KEY, -math.inf) < ALERT_EVERY:
            self.held.setdefault(EXPIRED_KEY, []).extend(s.t for s in starts)
            await self.expired(starts, fields, held=True)
            return
        held = self.held.get(EXPIRED_KEY, [])
        posted = await self.post(None, expired_text(starts, held))
        if posted is not None:
            self.alerted[EXPIRED_KEY], self.held[EXPIRED_KEY] = now, []
        else:
            LOG.warning("a wake alert was not posted (%s)", EXPIRED_KEY)
            if starts[0].t >= now - 2 * HORIZON:
                return
        await self.expired(starts, fields, alert=posted, held_before=len(held))

    async def expired(self, starts: list[Start], fields: dict[str, Any], **more: Any) -> None:
        await self.write(starts, fields | more)

    async def write(self, starts: list[Start], record: dict[str, Any]) -> None:
        """A `wake_check` record; only once it is written are its starts done. A record the
        audit log refuses (a full disk) is kept, as decided, and written again first on the
        next pass: its starts are neither checked nor alerted again (Codex round 5)."""
        try:
            await self.server.audit("wake_check", **record)
        except Exception:
            if starts:
                self.unrecorded[starts[0].key] = (starts, record)
            raise
        if starts:
            self.unrecorded.pop(starts[0].key, None)
        for s in starts:
            self.undelivered.pop(s.key, None)
            self.failures.pop(s.key, None)
            self.checked[s.key] = s.t


def held_text(held: list[float], what: str) -> str:
    if not held:
        return ""
    times = ", ".join(utc(t) for t in sorted(held)[:LISTED])
    more = f" and {len(held) - LISTED} more" if len(held) > LISTED else ""
    return f" ({len(held)} more {what} held back since the last alert, made at {times}{more}.)"


def alert_text(start: Start | None, outcome: Outcome, held: list[float]) -> str:
    """The alert: IDs, names and times, never a message's text."""
    if start is None:
        return f"⚠ **Wake check failed:** the starter's start log: {outcome.reason}."
    when, vm, who = utc(start.t), start.named(), start.peer
    if outcome.kind == "not_opted_in":
        text = (
            f"⚠ **Wake check:** {vm} was started at {when}, asked by {who}, but the waker's "
            "config doesn't opt it in: the waker had no business starting it. Look at the waker "
            "(ADMIN §1.5)."
        )
    elif outcome.kind == "unmatched":
        text = (
            f"⚠ **Wake check:** {vm} was started at {when}, asked by {who}, and no "
            f"message from its participant (user {outcome.owner}) reached its bots in the "
            f"{WINDOW / 60:.0f} minutes before. If nobody expected this start, look at the "
            "waker (ADMIN §1.5)."
        )
    else:
        text = (
            f"⚠ **Wake check failed:** {vm}, started at {when}, asked by {who}, could "
            f"not be checked: {outcome.reason}. Look at it as at a start with no message "
            "(ADMIN §1.5)."
        )
    return text + held_text(held, "of this VM's starts were")


def expired_text(starts: list[Start], held: list[float]) -> str:
    named = ", ".join(s.named() for s in starts[:LISTED])
    more = f" and {len(starts) - LISTED} more" if len(starts) > LISTED else ""
    return (
        f"⚠ **Wake check failed:** {len(starts)} starts were not checked within "
        f"{HORIZON / 3600:.0f} hours of being made (the bots were down, a backlog, or a check "
        f"that kept failing), from {utc(starts[0].t)} to {utc(starts[-1].t)}: {named}{more}. "
        "The audit record lists each (ADMIN §1.5)." + held_text(held, "such starts were")
    )
