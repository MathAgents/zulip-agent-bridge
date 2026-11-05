"""Whether a workshop's waker is down, and the alert when it is (#717; ADMIN §1.2, §1.8).

On Nov 1 the waker was OOM-killed at every start for 90 minutes, and nothing said so. It writes
its status file (`/var/lib/zab-waker/status/waker.json`) every 30 s, at each start too, with
each workshop's `ready_at`: when that workshop's waker last served, once it had run a minute
(`zab.waker.__main__.ready`), kept across its restarts. A waker killed at every start rewrites
the file each time but never moves `ready_at`, and a waker that doesn't run rewrites nothing.

- **Down:** a workshop the waker's config opts in (the bots write it, `!waker on|off`) whose
  `ready_at` is more than `DOWN_AFTER` old: down since then. A status file of an earlier
  release has no `ready_at`: its `written_at` stands in for it. With no status file, or no
  entry for the workshop, the config's write does.
- **`!status` and the console** say `waker down since …` (`status_line`, the collector).
- **The alert** (`Watch`): every `PERIOD`, the workshop's bot posts once in its
  `alerts_channel`, topic `TOPIC`, when its waker goes down, and once when it is back. A
  workshop opted out meanwhile gets no "back". A post Zulip surely never took (`deliver`) is
  tried again on the next pass; one that may have landed is not, so that none comes twice. Each
  post's audit record (`waker`) is the watch's memory across the bots' restarts: a record the
  audit log refuses leaves the post to be made again after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from zab.admin.wakes import read_opt_ins

if TYPE_CHECKING:
    from zab.admin.bot import AdminServer

LOG = logging.getLogger("zab.admin.wakerdown")
#: Seconds without a `ready_at`, past which a workshop's waker is down. A restart (each
#: config change, a deploy) leaves it at most about 155 s old: the old run's last write, up to
#: 30 s before its exit, 5 s for systemd, then a minute of the new run and its next write.
DOWN_AFTER = 300.0
PERIOD = 60.0  # seconds between the alert's checks
TOPIC = "waker"
MAX_STATUS = 4 * 1024 * 1024  # bytes of the status file read, as the collector's
DOWN, UP, OFF = "down", "up", "off"


def utc(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _read(path: str) -> Any:
    """The status file's JSON, or None when it is missing, too large or not JSON."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_STATUS + 1)
        return json.loads(data) if len(data) <= MAX_STATUS else None
    except (OSError, ValueError):
        return None


def _time(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) else None


def state(status: str, config: str, workshop: str, now: float) -> tuple[str, float | None]:
    """`(DOWN, since)`, `(UP, None)`, or `(OFF, None)` for a workshop the waker's config doesn't
    opt in; `("", None)` when the config can't be read: nothing can be said."""
    opted, written = read_opt_ins(config)
    if opted is None:
        return "", None
    if not any(ws == workshop for ws, _ in opted):
        return OFF, None
    data = _read(status)
    data = data if isinstance(data, dict) else {}
    entries = data.get("workshops") if isinstance(data.get("workshops"), list) else []
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == workshop), None)
    since: float | None = written or None
    if entry is not None:
        ready = entry["ready_at"] if "ready_at" in entry else data.get("written_at")
        since = _time(ready) or since
    if since is not None and now - since <= DOWN_AFTER:
        return UP, None
    return DOWN, since


def status_line(status: str, config: str, workshop: str, now: float) -> str | None:
    """`!status`'s line, when the workshop's waker is down; None otherwise."""
    kind, since = state(status, config, workshop, now)
    return down_line(since) if kind == DOWN else None


def _since(since: float | None) -> str:
    return f"since {utc(since)}" if since is not None else "(no sign of it yet)"


def down_line(since: float | None) -> str:
    return (
        f"⚠ waker down {_since(since)}: no stopped VM wakes for its participant's messages. "
        "Start one with `!start <slug>` meanwhile, and call the deployer (ORGANIZERS §4.1)."
    )


def down_text(workshop: str, since: float | None) -> str:
    """The alert, in the workshop's `alerts_channel`."""
    when = _since(since)
    return (
        f"⚠ **waker down {when}:** no stopped VM of {workshop} wakes for its participant's "
        "messages. Meanwhile, start a waiting participant's VM with `!start <slug>` in the "
        "organizers channel; the deployer looks at the waker's journal (ADMIN §1.8)."
    )


def up_text(since: float | None, now: float) -> str:
    down = f", down since {utc(since)}" if since is not None else ""
    return f"✅ **waker back** at {utc(now)}{down}: stopped VMs wake again for their participants."


class Watch:
    """The bots' alert (module doc); `observe` takes the audit log at start."""

    def __init__(self, server: AdminServer) -> None:
        self.server = server
        self.down: dict[str, float | None] = {}  # workshop → since, as alerted

    def observe(self, record: dict[str, Any]) -> None:
        """A `waker` record: the workshop alerted as down, or not any more."""
        workshop = record.get("workshop")
        if record.get("event") != "waker" or not isinstance(workshop, str):
            return
        if record.get("state") == DOWN:
            self.down[workshop] = _time(record.get("since"))
        else:
            self.down.pop(workshop, None)

    async def run(self) -> None:
        """A check every `PERIOD`; a check that fails is logged, and the next one comes. No
        timeout of its own: each post's request has its HTTP timeouts, and a post cut short may
        have landed (Codex round 3 on #732)."""
        while True:
            try:
                await self.check()
            except Exception as exc:
                LOG.warning("a waker check failed (%s)", type(exc).__name__)
            await asyncio.sleep(PERIOD)

    async def tell(self, bot: Any, workshop: str, channel: int, kind: str, text: str,
                   since: float | None = None) -> None:  # fmt: skip
        """Post `text` in `channel` and record the change to `kind` (`DOWN` or `UP`). The change
        counts from before the post: a post Zulip surely never took (a 4xx, or no connection,
        `AdminBot.deliver`) undoes it, for the next pass to try again; any other outcome may
        have landed, and is recorded, never made twice. That includes a post cut short by a
        cancel (the bots' stop): its record says `alert: null` before the cancel goes on."""
        previous = self.down.get(workshop)
        if kind == DOWN:
            self.down[workshop] = since
        else:
            self.down.pop(workshop, None)
        fields: dict[str, Any] = {"workshop": workshop, "state": kind}
        fields |= {"since": since} if kind == DOWN else {}
        try:
            posted, never = await bot.deliver(type="stream", to=channel, topic=TOPIC, content=text)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.server.audit("waker", **fields, alert=None)
            raise
        if posted is None and never:
            if kind == DOWN:
                self.down.pop(workshop, None)
            else:
                self.down[workshop] = previous
            return
        await self.server.audit("waker", **fields, alert=posted)

    async def check(self) -> None:
        server = self.server
        status, config = server.paths.waker_status, f"{server.paths.layout.waker}/config.toml"
        for bot in server.bots:
            ws = bot.workshop
            if ws is None or ws.alerts_channel is None:
                continue
            now = server.clock()
            kind, since = await asyncio.to_thread(state, status, config, ws.name, now)
            alerted = ws.name in self.down
            if kind == DOWN and not alerted:
                text = down_text(ws.name, since)
                await self.tell(bot, ws.name, ws.alerts_channel, DOWN, text, since)
            elif kind == UP and alerted:
                text = up_text(self.down[ws.name], now)
                await self.tell(bot, ws.name, ws.alerts_channel, UP, text)
            elif kind == OFF and alerted:  # opted out while down: nothing comes back
                self.down.pop(ws.name)
                await server.audit("waker", workshop=ws.name, state=OFF)
