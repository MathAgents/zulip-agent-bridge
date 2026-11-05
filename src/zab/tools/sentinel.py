"""`zab sentinel`: `!vm` and `!restart` in a DM, even while the bridge is down or deaf (#204).

    zab sentinel [--config PATH]

A separate small process, with no bot of its own: for every `[[bots]]` entry it reads the same
zuliprc the bridge uses and registers its **own** event queue, limited to direct messages
(`narrow=[["is", "dm"]]`; Zulip allows several queues per bot). It answers only the bot's
owner, by the bridge's rule (`config.allowed_users`), and only in a one-to-one DM:

- `!vm`: the report of `vmstat.format_report`, with a status line that starts with what only
  an outside process can see: whether `zab.service` runs, and how long ago this bot's listener
  last made progress with Zulip (the bridge touches `<state_dir>/heartbeat-<bot>` whenever its
  event cursor advances, Zulip's own heartbeat events included, so a healthy bridge does so
  about once a minute). It has no sign-in or turns: a topic's `!vm` has them.
- `!restart`: `systemctl --user restart --no-block zab.service`, a fixed argument list. It says
  so first ("running turns will be interrupted"), then confirms once the heartbeat moves after
  systemd shows a new main process (the old one gone), or says that it didn't within two
  minutes.
- `!restart codex`: the same for the installed `codex-app-server*.service` units, confirmed
  by `systemctl --user is-active`.

Anything else gets no reply: the bridge answers everything else. **The bridge leaves these two
to the sentinel** in a one-to-one DM that the sentinel answers (`listening`): its mark
`<state_dir>/sentinel-<bot>` names its process and this boot, was rewritten by a poll within
`LISTENING_FRESH_S`, and records since when its queue existed and since when it allows each
sender. Both sides decide by one rule on the message's Zulip timestamp (`answerable`): the
sentinel answers only a message sent `CLOCK_MARGIN_S` after both, and the bridge leaves it only
those. Otherwise (no sentinel installed, one down or deaf, a DM sent while the VM was off and
replayed by the bridge's catch-up, a sender allowed since the sentinel last read the users) the
bridge answers `!vm` itself, and says that nothing was restarted for `!restart`. A double reply
is possible (a mark the bridge read before the sentinel rewrote it). A command the sentinel
can't carry out gets a short failure reply, never silence; a DM goes unanswered only if the
sentinel hangs or dies with it while its mark is still fresh, or Zulip drops its queue before
it fetches the DM. While the organizers' hold is on (a rollout or a sync, which may stop the
bridge on purpose: `HOLD`), it refuses `!restart` and says why.

At most one restart a minute, across all bots. Each command is logged to the journal with the
sender's id, never the message text. No shell strings (CONTRIBUTING.md, rule 3) and no credential
file other than the zuliprc files the bridge already reads. It writes nothing but its marks, and
reads no state of the bridge but the heartbeat files, so idle stop (which reads zab's database,
the logins and the load) never counts it as activity. Installed as its own user unit,
`zab-sentinel.service`, by `zab service install` (SPEC §13.3), with `Restart=always`,
`WantedBy=default.target` (a woken VM starts it) and no tie to `zab.service`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from zab import __version__
from zab.commands.service import SENTINEL as SENTINEL_UNIT
from zab.config import BotConfig, Config, ConfigError, allowed_users, load_config
from zab.core.addressing import mention, without_typed_address
from zab.core.command_text import as_command
from zab.tools import vmstat
from zab.zulip.client import ZulipClient, ZulipError

log = logging.getLogger("zab.sentinel")

ZAB_UNIT = "zab.service"
#: A healthy listener advances its heartbeat about once a minute (Zulip heartbeat events).
STALE_AFTER_S = 180.0
RESTART_WAIT_S = 120.0
RESTART_EVERY_S = 60.0
POLL_S = 2.0
USERS_EVERY_S = 600.0  # refresh the allowlist's e-mail matches
#: ... and at most this often when a command comes from someone not (yet) allowed.
USERS_RETRY_S = 60.0
#: How long the sentinel's mark counts without a poll: a queue's long poll returns at least
#: about once a minute, with Zulip's heartbeat events.
LISTENING_FRESH_S = 180.0
#: The bridge leaves a DM to the sentinel only when it was sent this long after the sentinel's
#: queue was registered: the VM's clock against Zulip's timestamps (`zab doctor` checks < 5 s).
CLOCK_MARGIN_S = 5.0
COMMANDS = ("!vm", "!restart", "!restart codex")
#: A reply Zulip doesn't take for a transient reason is tried again: this many times in all,
#: 2 s apart, doubling, or after Zulip's `retry_after` (about a minute in all).
SEND_TRIES = 6
BOOT_ID = "/proc/sys/kernel/random/boot_id"
#: The organizers' hold, idle stop's (`workshop/admin/idle_stop.py`): `push_config.py` and
#: `final_sync.py` write its expiry (epoch seconds, `admin_common.IDLE_HOLD_ON_CMD`, mode 0644)
#: around their work, which may stop the bridge on purpose (`--lean-reclaim`, `--restart-codex`).
HOLD = "/run/zab-idle-stop.hold"
HELD = (
    "⏸ Nothing was restarted: your organizers are working on this VM (a rollout or a sync), "
    "and may have stopped your bridge on purpose. It comes back when they finish."
)
#: ... for `!restart codex`, whose daemon a rollout's `--restart-codex` restarts itself (#588).
HELD_CODEX = (
    "⏸ Nothing was restarted: your organizers are working on this VM (a rollout or a sync), "
    "and may be restarting Codex themselves. Send !restart codex again when they finish."
)

#: `(argv, timeout) -> (exit code, stdout)`, a fixed argument list, never a shell string.
Runner = Callable[[Sequence[str], float], Awaitable[tuple[int, str]]]


async def run_command(argv: Sequence[str], seconds: float) -> tuple[int, str]:
    """`(exit code, stdout)`: 124 on a timeout, 126 when the process can't be started (a VM in
    trouble: no fork at the user slice's TasksMax, or no memory)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 126, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode or 0, out.decode(errors="replace").strip()


def command_text(
    content: object, kind: str = "", mention: re.Pattern[str] | None = None
) -> str | None:
    """The command, when the text is exactly one of `COMMANDS` (spacing aside), as people
    type them: after a leading typed mention of the bot's `kind` (`@claude !vm`, #713), then
    as `as_command` reads it (#709: `!VM`, "`!restart`", after a leading completed
    `mention` of the bot). `!restart`'s one word after it is read in any case too, so that
    `!Restart Codex` is `!restart codex`. The bridge reads it with its router's kind and
    mention, so both sides agree on a message."""
    words = as_command(without_typed_address(str(content), kind), mention).split()
    if len(words) == 2 and words[0] == "!restart":
        words[1] = words[1].casefold()
    text = " ".join(words)
    return text if text in COMMANDS else None


def transient(exc: BaseException) -> bool:
    """Zulip didn't answer, or asked for a retry: a transport error, a rate limit, a server error
    or a response that isn't JSON (`zab.core.bridge.zulip_unreachable`'s rule)."""
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, ZulipError)
        and (exc.status == 429 or exc.status >= 500 or exc.code == "INVALID_RESPONSE")
    )


def held(now: float, path: str = HOLD) -> bool:
    """Whether the organizers' hold is on: its expiry is to come. An unreadable one counts for an
    hour from its mtime, as for idle stop (`idle_stop.read_hold`)."""
    hold = Path(path)
    try:
        text = hold.read_text()
    except FileNotFoundError:
        return False
    except OSError:
        text = ""
    try:
        until = float(text.split()[0])
    except (ValueError, IndexError):
        try:
            until = hold.stat().st_mtime + 3600
        except OSError:
            return False
    return now < until


def heartbeat_path(state_dir: str | Path, bot: str) -> Path:
    return Path(state_dir).expanduser() / f"heartbeat-{bot}"


def mark_path(state_dir: str | Path, bot: str) -> Path:
    return Path(state_dir).expanduser() / f"sentinel-{bot}"


def boot_id(path: str = BOOT_ID) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def alive(pid: int) -> bool:
    if pid <= 1:  # 0 and -1 would signal a process group, or everything
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def one_to_one(message: dict[str, Any], bot_id: int | None) -> bool:
    """A DM between its sender and the bot alone."""
    recipients = {r.get("id") for r in message.get("display_recipient") or []}
    return message.get("type") == "private" and recipients == {message.get("sender_id"), bot_id}


def answerable(
    registered: float | None, allowed: dict[str, float], sender: object, sent_at: float
) -> bool:
    """The rule both sides apply to a message (module doc): the sentinel's queue existed, and it
    allowed the sender, `CLOCK_MARGIN_S` before Zulip's timestamp `sent_at`. `allowed` maps a
    sender's id, as a string (JSON's keys), to since when the sentinel allows them."""
    since = allowed.get(str(sender))
    if registered is None or since is None:
        return False
    return max(float(registered), float(since)) + CLOCK_MARGIN_S <= sent_at


def listening(
    state_dir: str | Path,
    bot: str,
    bot_id: int | None,
    sender: object,
    sent_at: float,
    *,
    now: float | None = None,
) -> bool:
    """Whether this bot's sentinel answers `sender`'s DM that Zulip timestamped `sent_at`
    (module doc): its mark is from this boot, by a live process listening as `bot_id` (a bot
    whose zuliprc changed is another bot), rewritten by a poll within `LISTENING_FRESH_S`, and
    `answerable` says so. Anything unreadable is False, so that the bridge answers."""
    path = mark_path(state_dir, bot)
    try:
        written = path.stat().st_mtime
        mark = json.loads(path.read_text())
        pid, boot = int(mark["pid"]), mark.get("boot")
        if mark.get("bot_id") != bot_id or bot_id is None:
            return False
        if not answerable(mark["registered"], mark["allowed"], sender, sent_at):
            return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    now = time.time() if now is None else now
    return boot == boot_id() and alive(pid) and now - written <= LISTENING_FRESH_S


def answers(
    message: dict[str, Any],
    bot_id: int | None,
    state_dir: str | Path,
    bot: str,
    kind: str = "",
    mention: re.Pattern[str] | None = None,
) -> bool:
    """The bridge's test: the sentinel answers this one-to-one DM command (module doc); `kind`
    and `mention` are the bot's, as its router has them, for `command_text`."""
    return (
        command_text(message.get("content", ""), kind, mention) is not None
        and one_to_one(message, bot_id)
        and listening(
            state_dir,
            bot,
            bot_id,
            message.get("sender_id"),
            float(message.get("timestamp") or 0),
        )
    )


#: `health`'s states in which no sentinel answers: DM `!restart` restarts nothing, and the
#: bridge answers DM `!vm` itself.
DOWN = ("not installed", "down", "no mark", "stale")
#: ... and all its states other than `ok`, `unknown` when the doctor couldn't ask systemd: the
#: first words of a warning `<bot>:sentinel` check of `zab doctor` (#588), which
#: `workshop/admin/status.py` shows as the VM's `sentinel` (`SENTINEL_STATES`, a copy; the admin
#: side's in `zab.admin.pages`, which doesn't import this module: tests keep all three equal).
UNHEALTHY = (*DOWN, "unknown")


def mark_health(
    state_dir: str | Path, bot: str, bot_id: int | None, *, now: float | None = None
) -> tuple[str, str]:
    """`("ok", why)` when this bot's mark is what `listening` needs, its sender's rule apart:
    from this boot, by a live process, as `bot_id` (unless that is unknown), rewritten within
    `LISTENING_FRESH_S`; else `("no mark", why)` or `("stale", why)`. Reads the mark alone."""
    path = mark_path(state_dir, bot)
    try:
        written = path.stat().st_mtime
        mark = json.loads(path.read_text())
        pid, boot, marked = int(mark["pid"]), mark.get("boot"), mark.get("bot_id")
    except FileNotFoundError:
        return "no mark", "no Zulip queue of the sentinel for this bot"
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return "stale", f"its mark can't be read ({type(exc).__name__})"
    age = (time.time() if now is None else now) - written
    if boot != boot_id():
        return "stale", "its mark is from another boot"
    if not alive(pid):
        return "stale", f"its mark's process {pid} has ended"
    if bot_id is not None and marked != bot_id:
        return "stale", "its mark is another bot's"
    if age > LISTENING_FRESH_S:
        return "stale", f"its mark is {age_text(age)} old"
    return "ok", f"its mark is from this boot, {age_text(age)} old"


def health(
    state_dir: str | Path,
    bot: str,
    bot_id: int | None,
    unit: str | None,
    *,
    now: float | None = None,
) -> tuple[str, str]:
    """`(state, why)` of this bot's sentinel (#588): `unit` is `systemctl --user is-active`'s
    answer for `zab-sentinel.service`, None when its unit file isn't installed, "" when there
    was none (the user's systemd out of reach: no `XDG_RUNTIME_DIR`, as under a bare `sudo -u`);
    then the mark (`mark_health`). A state is `ok` or one of `UNHEALTHY`."""
    if unit is None:
        return "not installed", f"no {SENTINEL_UNIT}, which `zab service install` writes"
    if not unit:
        why = "`systemctl --user is-active` gave no answer (is XDG_RUNTIME_DIR set?)"
        return "unknown", f"can't reach the user's systemd: {why}"
    if unit != "active":
        return "down", f"{SENTINEL_UNIT} is {unit}"
    return mark_health(state_dir, bot, bot_id, now=now)


def restart_unanswered() -> str:
    """The bridge's reply to a one-to-one DM `!restart` that the sentinel didn't receive."""
    from zab.commands.service import unit_directory

    running = "Your bridge is running (it sent this reply)"
    if (unit_directory() / SENTINEL_UNIT).is_file():
        return (
            "Nothing was restarted: the restart service didn't receive this message, perhaps "
            f"because your VM was starting. {running}; send `!restart` again if you still "
            "need one."
        )
    return f"Nothing was restarted: `!restart` isn't available on this VM yet. {running}."


def codex_units(unit_dir: Path | None = None) -> list[str]:
    """The installed `codex-app-server*.service` units, as `zab service install` wrote them."""
    from zab.commands.service import managed_units, unit_directory

    try:
        found = managed_units(unit_dir or unit_directory())
    except OSError:
        return []
    return [p.name for p in found if p.name.startswith("codex-app-server")]


def age_text(seconds: float) -> str:
    """`40 seconds`, `7 minutes`: in words, as `!vm` gives the uptime (PM spec on #204)."""
    return vmstat.words(max(0.0, seconds))


class Shared:
    """State all bots' sentinels share: one restart a minute in all."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.last_restart: float | None = None

    def may_restart(self) -> float:
        """0 when a restart may run now (and records it), else the seconds to wait."""
        now = self.clock()
        if self.last_restart is not None and now - self.last_restart < RESTART_EVERY_S:
            return RESTART_EVERY_S - (now - self.last_restart)
        self.last_restart = now
        return 0.0


class Sentinel:
    """One bot's DM queue and commands."""

    def __init__(
        self,
        config: Config,
        bot: BotConfig,
        client: Any,
        shared: Shared,
        *,
        run: Runner = run_command,
        collect: Callable[..., vmstat.VmStat] = vmstat.collect,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        units: Callable[[], list[str]] = codex_units,
        hold: str = HOLD,
    ) -> None:
        self.config = config
        self.bot = bot
        # the bot's backend kind, whose typed mention may start a command (`command_text`)
        self.kind = getattr(config.backends.get(bot.backend), "kind", "")
        self.client = client
        self.shared = shared
        self.run = run
        self.collect = collect
        self.now = now
        self.sleep = sleep
        self.units = units
        self.hold = hold
        self.user_id: int | None = None
        self.owner_id: int | None = None
        self.mention: re.Pattern[str] | None = None  # the bot's, as its bridge's router has it
        self.allowed: frozenset[int] = frozenset()
        self.allowed_since: dict[str, float] = {}  # sender id -> since when (`answerable`)
        self.users_at = 0.0
        self.registered: float | None = None  # when this queue was registered (`listening`)
        self.boot = boot_id()
        self.state_dir = Path(config.bridge.state_dir).expanduser()
        # the root and the projects' file systems, as the bridge's `!vm` (`collect` drops a
        # duplicate device)
        self.disk_paths = ["/", *(p.path for p in config.projects)]

    # ------------------------------------------------------------------------------ Zulip

    async def refresh_users(self) -> None:
        if self.user_id is None:
            me = await self.client.request("GET", "users/me")
            if not me.get("is_bot"):
                raise ValueError("sentinel credentials must belong to a bot")
            self.user_id, self.owner_id = me["user_id"], me.get("bot_owner_id")
            self.mention = mention(me["full_name"], me["user_id"])
        users: list[dict[str, Any]] = []
        if self.bot.allow_emails:  # e-mails need the user list; ids and the owner don't
            users = (await self.client.request("GET", "users"))["members"]
        self.allowed = allowed_users(self.bot, self.owner_id, users)
        self.users_at = self.now()
        since = self.allowed_since
        self.allowed_since = {str(u): since.get(str(u), self.users_at) for u in self.allowed}
        self.mark()  # the bridge leaves a newly allowed sender to the sentinel from now on

    async def register(self) -> tuple[str, int]:
        data = await self.client.request(
            "POST",
            "register",
            event_types=["message"],
            narrow=[["is", "dm"]],
            apply_markdown=False,
        )
        return data["queue_id"], data["last_event_id"]

    async def send(self, user_id: int, text: str) -> None:
        """Post a reply. A transient failure is tried again (`SEND_TRIES`), then raised: the
        command's event is then handled again at a later poll (`serve`)."""
        delay = 2.0
        for attempt in range(1, SEND_TRIES + 1):
            try:
                await self.client.request(
                    "POST", "messages", type="private", to=[user_id], content=text
                )
                return
            except Exception as exc:
                if attempt == SEND_TRIES or not transient(exc):
                    raise
                wait = max(delay, exc.retry_after if isinstance(exc, ZulipError) else 0.0)
                kind = exc.code if isinstance(exc, ZulipError) else type(exc).__name__
                log.warning("sentinel %s: reply not taken (%s); again in %.0f s",
                            self.bot.name, kind, wait)  # fmt: skip
                await self.sleep(wait)
                delay *= 2

    async def tell(self, user_id: int, text: str) -> None:
        """`send` for a restart's messages, which never fail the restart: it runs once, whether
        or not Zulip takes them."""
        try:
            await self.send(user_id, text)
        except Exception as exc:
            kind = exc.code if isinstance(exc, ZulipError) else type(exc).__name__
            log.warning("sentinel %s: a restart's message was lost (%s)", self.bot.name, kind)

    def mark(self) -> None:
        """Write the mark the bridge reads (`listening`), after a registration, a refresh of the
        users and each poll; without a queue, remove it, so that the bridge answers. Before the
        bridge first made the state directory, there is none to write to: the next poll tries
        again."""
        path = mark_path(self.state_dir, self.bot.name)
        if self.registered is None:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return
        body = {
            "pid": os.getpid(),
            "boot": self.boot,
            "bot_id": self.user_id,
            "registered": self.registered,
            "allowed": self.allowed_since,
        }
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(body))
            temporary.replace(path)
        except OSError as exc:
            log.warning("sentinel %s: no mark written (%s)", self.bot.name, type(exc).__name__)

    async def serve(self) -> None:
        """Register, poll and answer, forever. A failed poll is retried on the same queue, which
        keeps the DMs the bridge left to the sentinel; only an expired queue (Zulip drops one
        after about ten minutes without a poll) is registered again, which moves `registered`
        on."""
        delay = 5.0
        queue: str | None = None
        last = -1
        self.registered = None
        self.mark()  # a previous process's mark goes at once, Zulip reachable or not
        while True:
            try:
                if queue is None:
                    await self.refresh_users()
                    queue, last = await self.register()
                    self.registered = self.now()
                    self.mark()
                elif self.now() - self.users_at > USERS_EVERY_S:
                    await self.refresh_users()
                events = await self._events(queue, last)
                self.mark()
                delay = 5.0
                for event in events:
                    if event.get("type") == "message":
                        try:
                            await self.handle(event["message"])
                        except Exception as exc:
                            if transient(exc):  # a reply Zulip didn't take: this event again,
                                raise  # at the next poll (a restart's messages don't raise)
                            # anything else would fail again: skipped, never retried forever
                            log.warning("sentinel %s: message skipped after %s",
                                        self.bot.name, type(exc).__name__)  # fmt: skip
                    last = max(last, event["id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # ZulipError, transport errors: log the kind and retry
                code = exc.code if isinstance(exc, ZulipError) else type(exc).__name__
                if code == "BAD_EVENT_QUEUE_ID":  # its DMs are gone: none is the sentinel's
                    queue, self.registered = None, None  # until it registers again
                    self.mark()  # removed: the bridge answers meanwhile
                wait = max(delay, exc.retry_after if isinstance(exc, ZulipError) else 0)
                log.warning("sentinel %s: %s; retrying in %.0f s", self.bot.name, code, wait)
                await self.sleep(wait)
                delay = min(delay * 2, 60.0)

    async def _events(self, queue: str, last: int) -> list[dict[str, Any]]:
        data = await self.client.request("GET", "events", queue_id=queue, last_event_id=last)
        return data["events"]

    # --------------------------------------------------------------------------- commands

    def command(self, message: dict[str, Any]) -> str | None:
        """The command, when this is the owner's one-to-one DM holding exactly one, and the
        bridge leaves it to the sentinel (`answerable`)."""
        uid = message.get("sender_id")
        if message.get("type") != "private" or uid == self.user_id or uid not in self.allowed:
            return None
        sent_at = float(message.get("timestamp") or 0)
        if not answerable(self.registered, self.allowed_since, uid, sent_at):
            return None  # sent before the queue, or before the sentinel allowed its sender
        if not one_to_one(message, self.user_id):  # a group DM: other people would see the reply
            return None
        return command_text(message.get("content", ""), self.kind, self.mention)

    async def handle(self, message: dict[str, Any]) -> None:
        command = self.command(message)
        if (
            command is None
            and self.bot.allow_emails
            and message.get("sender_id") not in self.allowed
            and command_text(message.get("content", ""), self.kind, self.mention)
            and self.now() - self.users_at > USERS_RETRY_S
        ):
            # someone the bridge may allow already (an allowed e-mail that joined since the
            # last refresh): the bridge answers this message, the sentinel the next ones
            await self.refresh_users()
        if command is None:
            return
        uid = message["sender_id"]
        log.info("sentinel %s: %s from user %s", self.bot.name, command, uid)
        try:
            if command == "!vm":
                await self.send_report(uid, await self.vm_report())
            elif command == "!restart":
                await self.restart_bridge(uid)
            else:
                await self.restart_codex(uid)
        except Exception as exc:
            if transient(exc):
                raise  # a reply Zulip didn't take: this event again at the next poll (`serve`)
            # the bridge left this command to the sentinel: an answer, whatever failed
            kind = type(exc).__name__
            log.warning("sentinel %s: %s failed (%s)", self.bot.name, command, kind)
            if command == "!vm":
                text = f"⚠️ The VM report failed ({kind}). If your bots don't answer, send !restart."
            else:
                text = f"⚠️ The restart failed ({kind}). Ask in your workshop's channel."
            await self.tell(uid, text)

    async def send_report(self, uid: int, report: str) -> None:
        """`!vm`'s report, made: a post Zulip refuses for good (not `transient`, which `serve`
        tries again) gets a short reply that says so, not that the report failed (#588)."""
        try:
            await self.send(uid, report)
        except ZulipError as exc:
            if transient(exc):
                raise
            log.warning("sentinel %s: !vm's report was refused (%s)", self.bot.name, exc.code)
            await self.tell(
                uid,
                f"⚠️ Zulip refused the VM report ({exc.code}). If your bots don't answer, "
                "send !restart.",
            )

    def heartbeat(self) -> float | None:
        """When this bot's listener last advanced its event cursor (its file's mtime)."""
        try:
            return heartbeat_path(self.state_dir, self.bot.name).stat().st_mtime
        except OSError:
            return None

    def heartbeat_age(self) -> float | None:
        beat = self.heartbeat()
        return None if beat is None else self.now() - beat

    async def unit_state(self, unit: str) -> str:
        _, out = await self.run(["systemctl", "--user", "is-active", unit], 30)
        return out or "unknown"

    async def main_pid(self) -> int | None:
        """`zab.service`'s main process, 0 when it has none; None when systemctl can't say."""
        argv = ["systemctl", "--user", "show", "--property=MainPID", "--value", ZAB_UNIT]
        code, out = await self.run(argv, 30)
        try:
            return int(out) if code == 0 else None
        except ValueError:
            return None

    async def bridge_line(self) -> str:
        state = await self.unit_state(ZAB_UNIT)
        advice = (
            "; your organizers are working on this VM"
            if held(self.now(), self.hold)
            else ": send !restart"
        )
        if state in ("activating", "reloading"):
            return "bridge starting"
        if state == "deactivating":
            return "bridge stopping"
        if state in ("inactive", "failed"):
            return f"bridge not running ({state}){advice}"
        if state != "active":
            return f"bridge state unknown ({state})"
        age = self.heartbeat_age()
        if age is None:
            return "bridge running, no Zulip event processed yet"
        if age > STALE_AFTER_S:
            quiet = age_text(age)
            return f"bridge running, but no Zulip event processed for {quiet}{advice}"
        return f"bridge running, last Zulip event {age_text(age)} ago"

    async def vm_report(self) -> str:
        stat = await asyncio.to_thread(self.collect, self.disk_paths)
        status = [await self.bridge_line(), vmstat.uptime_text(stat), f"zab {__version__}"]
        return vmstat.format_report(stat, status=[part for part in status if part])

    async def restart(self, units: Sequence[str], *, block: bool) -> int:
        """`reset-failed` (a unit that hit its start limit restarts too), then `restart`."""
        await self.run(["systemctl", "--user", "reset-failed", *units], 30)
        argv = ["systemctl", "--user", "restart", *([] if block else ["--no-block"]), *units]
        code, _ = await self.run(argv, 60)
        return code

    async def restart_bridge(self, uid: int) -> None:
        if held(self.now(), self.hold):
            await self.tell(uid, HELD)
            return
        wait = self.shared.may_restart()
        if wait:
            await self.tell(
                uid, f"⏳ A restart ran less than a minute ago; try again in {wait:.0f} s."
            )
            return
        await self.tell(uid, "🔄 Restarting your bridge: running turns will be interrupted.")
        old = await self.main_pid()
        started = self.now()
        code = await self.restart([ZAB_UNIT], block=False)
        if code != 0:
            state = await self.unit_state(ZAB_UNIT)
            await self.tell(uid, f"⚠️ The restart failed (exit {code}); bridge: {state}.")
            return
        # Once systemd shows another main process, the old one has exited: a heartbeat after
        # that moment is the new bridge's (its old process may advance once more as it stops).
        gone = started if old == 0 else None
        pid: int | None = None
        while self.now() - started < RESTART_WAIT_S:
            await self.sleep(POLL_S)
            if self.registered is not None:
                self.mark()  # the queue keeps the DMs that come meanwhile: still listening
            pid = await self.main_pid()
            if pid is None:
                continue
            if gone is None and pid != old:
                gone = self.now()
            beat = self.heartbeat()
            if gone is not None and pid not in (0, old) and beat is not None and beat > gone:
                await self.tell(
                    uid,
                    "✅ Your bridge is back: it is processing Zulip events again "
                    f"({age_text(self.now() - started)} after the restart).",
                )
                return
        state = await self.unit_state(ZAB_UNIT)
        if pid not in (None, 0, old) and state in ("activating", "active"):
            # a new process, slow to its first event (Zulip's heartbeat, about a minute)
            await self.tell(
                uid,
                "⏳ Your bridge is starting again but hasn't processed a Zulip event yet: send "
                "!vm in a minute.",
            )
            return
        await self.tell(
            uid,
            f"⚠️ The bridge has not processed a Zulip event within {age_text(RESTART_WAIT_S)} of "
            f"the restart (bridge: {state}). Ask in your workshop's channel.",
        )

    async def restart_codex(self, uid: int) -> None:
        units = self.units()
        if not units:
            await self.tell(uid, "This VM has no Codex daemon to restart.")
            return
        if held(self.now(), self.hold):
            await self.tell(uid, HELD_CODEX)
            return
        wait = self.shared.may_restart()
        if wait:
            await self.tell(
                uid, f"⏳ A restart ran less than a minute ago; try again in {wait:.0f} s."
            )
            return
        await self.tell(uid, "🔄 Restarting Codex: its running turns will be interrupted.")
        code = await self.restart(units, block=True)
        states = [await self.unit_state(u) for u in units]
        if code == 0 and all(s == "active" for s in states):
            await self.tell(uid, "✅ Codex is running again.")
        else:
            await self.tell(
                uid,
                f"⚠️ Codex did not come back (exit {code}; {', '.join(states)}). "
                "Ask in your workshop's channel.",
            )


async def serve_all(
    config: Config, make_client: Callable[[str], Any] = ZulipClient.from_zuliprc
) -> None:
    shared = Shared()
    sentinels = [Sentinel(config, bot, make_client(bot.zuliprc), shared) for bot in config.bots]
    await asyncio.gather(*(s.serve() for s in sentinels))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="zab sentinel", description="!vm and !restart in DMs (#204)"
    )
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"zab sentinel: {exc}", file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_all(config))
    return 0
