"""Automatic growth of a filling root volume (#998; docs/ADMIN.md §3.4).

Every `PERIOD`, the bots read the collector's snapshot (`console/snapshot.json`, whose disk use
the console's Disk column shows): the collector sweeps every running VM every 10 minutes, as the
read user where idle stop runs (§4.1), and every `!status` feeds it too. For each workshop with
an `alerts_channel` and no plan applying, a row's VM that EC2 shows running, whose root file
system is `THRESHOLD`% used or more (`df`'s percentage) by a read at most `FRESH` old, made since
its boot and since its last try, grows: `grow_disk.py --auto-cap CAP_GB`, `!grow-disk`'s script
for that row (`actions.auto_grow`), run as the runner runs a step (§2.3). It grows the volume by
half, rounded up to whole GB, to at most `CAP_GB`, then the partition and the file system online,
with no restart, through `!grow-disk`'s role statement and SSH. At most `PARALLEL` at once.

- **No plan, no approval** (ADMIN §0, decision 4's exception): growth only, by at most half the
  volume and to at most `CAP_GB`, no more than once in EBS's 6 hours, each said in the alerts
  channel and the audit log. A full disk breaks a participant's agents within minutes; a plan
  would wait for an organizer.
- **EBS's 6 hours.** After a growth, the next try waits until EBS allows another modification,
  at the time the script names (`again`); a VM still above `THRESHOLD` then grows again, and its
  growth's line says so meanwhile. A try within them (another's `!grow-disk`) grows the file
  system alone, or says that it waits (`wait`).
- **Failures** (a refusal, `ModifyVolume` refused, `growpart` or `resize2fs` failing, a timeout)
  are said each, and tried again `RETRY` later, by a read made since, `TRIES` times in all; then
  no more, until a read shows the disk below `THRESHOLD`. A try after a volume that grew and a
  file system that didn't, within the 6 hours, grows the file system alone. At `CAP_GB` it says
  so once, and stops likewise. A VM not running at the try is neither grown nor started, and
  nothing is said: it is tried again `RETRY` later, if a read since shows it full.
- **The VM read is the VM grown.** A read counts only for the instance it names, the one
  `status.py` reached, and only when that is the row's VM in the snapshot: values kept by row
  from a VM handed over since, or a read naming none (before #998), grow nothing (Codex round
  4 on #1003). The script gets the instance whose disk was read
  (`--expect-instance`): a row whose VM is another instance by then, a spare handed over before
  the collector's next read, grows nothing (`replaced`, no line, not counted), and that VM's
  own reading decides (Codex round 3 on #1003).
- **Read again.** A try that waited for its slot reads the snapshot again first, and runs only
  on a fresh read of the same VM still at `THRESHOLD` or more; otherwise it is dropped, unsaid
  and uncounted, and the next look decides (Codex round 2 on #1003).
- **The workshop's lock.** A try holds its workshop as a plan's apply does (§2.4 step 5), from
  the start of its script to its end (`Planning.hold_growth`, with no await between its
  re-read of the snapshot and the mark): an approval in the workshop, or of a server plan, is
  answered `busy:` meanwhile, but a `!stop` or `!start` that doesn't name the VM growing
  (`planning.BESIDE_GROWTH`; the review of #1003, N1), and the month-end's runs wait; a try
  that finds a plan applying or approved there waits for the next look, uncounted (Codex round
  1 on #1003). So does one while a server plan is approved or applying, which holds every
  workshop (`Planning.busy`): `!upgrade-server`, or a rollout or trust fix of every workshop
  (#995), whose parts start and stop VMs.
- **Memory.** Each try is a `disk_grow` audit record (§2.8), with its failures in a row, which
  `observe` reads back at the bots' start; a read below `THRESHOLD` that clears failures or the
  cap is a record too (`cleared`), so a restart keeps it (Codex round 1 on #1003).
- **A line Zulip never took** (`AdminBot.deliver`: refused with a 4xx, or no connection made) is
  kept, its text in its try's record (`unsent`), and posted again at each look, the oldest
  first, until it lands; the resize never runs again for it. Each posted so is an `alerted`
  record naming its try's (`of`), so a restart keeps what is still owed. A line that may have
  landed (a 5xx, an answer lost) is not posted again, except when the bots' stop cuts a re-post
  short: it is still owed, and posted once more after their restart, a duplicate rather than a
  lost line (Codex round 5 and the review of #1003, B1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zab.admin.actions import ActionError, auto_grow
from zab.admin.collector import DOWN
from zab.admin.costs import GP3_GB_MONTH
from zab.admin.fleet import FleetError
from zab.admin.grammar import MAX_DISK_GB
from zab.admin.runner import Capture, Ran, script, timeout
from zab.admin.scrub import span

if TYPE_CHECKING:
    from zab.admin.bot import AdminBot, AdminServer
    from zab.admin.config import Workshop

LOG = logging.getLogger("zab.admin.autogrow")
#: The root file system's use, in `df`'s percent, from which it grows. A constant, as
#: `grammar.MAX_DISK_GB` is: an admin.toml setting would change `config.py` (D32).
THRESHOLD = 85
#: The largest size it grows a volume to, in GB: `!grow-disk` goes further, to `MAX_DISK_GB`.
CAP_GB = 400
PERIOD = 60.0  # seconds between looks at the snapshot
FRESH = 1800.0  # a read older than this grows nothing: three sweeps missed
RETRY = 1800.0  # the wait after a failed try, or a VM not running at the try
TRIES = 3  # failed tries in a row, then none until the disk is below THRESHOLD again
PARALLEL = 4  # growths at once, on the server
#: EBS's limit, as `grow_disk.MODIFY_EVERY_S` (a test keeps them equal): the wait after a
#: growth whose script named no time.
EVERY = 6 * 3600.0
TOPIC = "disk"
#: `grow_disk.py`'s last line (`grow_disk.AUTO`, a test keeps them equal), and its outcomes.
AUTO = "auto result: "
OUTCOMES = frozenset({"grown", "failed", "wait", "capped", "stopped", "replaced"})
CLEARED = "cleared"  # a record's own outcome: a read below THRESHOLD cleared the failures
ALERTED = "alerted"  # a record's own outcome: an unsent line (`of`, its try's seq) posted late
SNAPSHOT_MAX = 16 * 1024 * 1024  # bytes of the snapshot read, as the collector's reader
#: A try with no result of the script's own: its reason is added.
FAILED: dict[str, Any] = {"outcome": "failed", "instance": None, "from": None, "to": None,
                          "fs": "", "used": None, "again": None, "reason": ""}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Unsent:
    """A try's line that Zulip never took (module doc): its workshop, row, VM and text."""

    workshop: str
    slug: str
    instance: str
    text: str


@dataclass(slots=True)
class Vm:
    """One VM's tries: the last one's time, no try before `again`, its failures in a row, and
    `held`: no try until a read below `THRESHOLD`."""

    tried: float = 0.0
    again: float = 0.0
    failures: int = 0
    held: bool = False

    def note(
        self, outcome: str, at: float, again: float | None, failures: int | None = None
    ) -> None:
        """A try's outcome at `at`; `failures`, as its record has them (`observe`), counts in
        place of one more."""
        if outcome == CLEARED:
            self.failures, self.held = 0, False
            return
        self.tried = at
        if outcome == "grown":
            self.failures, self.held, self.again = 0, False, again or at + EVERY
        elif outcome == "wait":
            self.again = again or at + RETRY
        elif outcome == "capped":
            self.held = True
        elif outcome in ("stopped", "replaced"):
            self.again = at + RETRY
        else:
            self.failures = self.failures + 1 if failures is None else failures
            self.again, self.held = at + RETRY, self.failures >= TRIES


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) else None


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return None


def utc(ts: float) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts))


def result(output: str) -> dict[str, Any] | None:
    """The script's last line, `AUTO` and its JSON, checked field by field; None without one."""
    lines = [line for line in output.splitlines() if line.startswith(AUTO)]
    try:
        data = json.loads(lines[-1][len(AUTO) :]) if lines else None
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("outcome"), str):
        return None
    if data["outcome"] not in OUTCOMES:
        return None
    size = {k: v if type(v) is int and 0 < v <= MAX_DISK_GB else None
            for k, v in ((k, data.get(k)) for k in ("from", "to"))}  # fmt: skip
    used = data.get("used")
    instance, fs, reason = (data.get(k) for k in ("instance", "fs", "reason"))
    return {
        "outcome": data["outcome"],
        "instance": instance[:40] if isinstance(instance, str) else None,
        **size,
        "fs": fs if fs in ("grown", "next boot", "unchanged", "not grown") else "",
        "used": used if type(used) is int and 0 <= used <= 100 else None,
        "again": _number(data.get("again")),
        "reason": reason[:300] if isinstance(reason, str) else "",
    }


def suggest(size: int | None) -> str:
    """`!grow-disk`'s size for a next step by hand: half again, at most `MAX_DISK_GB`."""
    return f"{min(MAX_DISK_GB, math.ceil(size * 3 / 2))}GB" if size else "NGB"


def alert(slug: str, used: float, read: float, r: dict[str, Any], vm: Vm) -> str | None:
    """The line in the alerts channel for one try (module doc); None for a VM not running."""
    was = f"its root file system was {used:g}% used at {utc(read)}"
    then = f" from {utc(r['again'])}" if r["again"] else " 6 hours after its last change"
    old, new = r["from"], r["to"]
    if r["outcome"] == "grown":
        now = f", and is {r['used']}% now" if r["used"] is not None else ""
        if r["fs"] == "next boot":
            line = (f"✅ **{slug}**'s disk grew from {old} to {new} GB: {was}. Its VM stopped "
                    "meanwhile: its file system grows at its next start.")  # fmt: skip
        elif old == new:
            line = f"✅ **{slug}**'s file system grew to fill its {new} GB volume: {was}{now}."
        else:
            cost = (new - old) * GP3_GB_MONTH if old and new else 0.0
            line = (f"✅ **{slug}**'s disk grew from {old} to {new} GB, online: {was}{now}. "
                    f"About ${cost:,.2f} a month more.")  # fmt: skip
        if r["used"] is not None and r["used"] >= THRESHOLD:
            line += f" Still above {THRESHOLD}%: EBS allows its next growth{then}."
        return line
    if r["outcome"] == "wait":
        return (f"⏳ **{slug}**: {was}, and its volume changed less than 6 hours ago: EBS allows "
                f"its next growth{then}, when it grows if it still needs to. Until then "
                "`!grow-disk` can't grow it either: its participant may free space.")  # fmt: skip
    if r["outcome"] == "capped":
        return (f"⚠ **{slug}**: {was}, and its volume is {old or CAP_GB} GB, the cap of automatic "
                f"growth: it grows no more on its own. `!grow-disk {slug} {suggest(old)}` grows "
                "it further (ORGANIZERS §4.1).")  # fmt: skip
    if r["outcome"] != "failed":
        return None
    why = span(r["reason"]) if r["reason"] else "no reason given"  # its own words, as code
    line = f"⚠ **{slug}**'s disk didn't grow (try {vm.failures} of {TRIES}): {why}; {was}."
    if old and new and new > old and r["fs"] == "not grown":
        line += f" Its volume is {new} GB now; its file system not yet."
    if vm.held:
        size = f"{new}GB" if old and new and new > old else suggest(old)
        return line + f" No more automatic tries: `!grow-disk {slug} {size}` (ORGANIZERS §4.1)."
    return line + f" The next try comes after {utc(vm.again)}, if it is still above {THRESHOLD}%."


class Grower:
    """The bots' automatic growth (module doc); `observe` takes the audit log at start."""

    def __init__(self, server: AdminServer) -> None:
        self.server = server
        self.vms: dict[str, Vm] = {}  # instance ID → its tries
        self.growing: set[str] = set()
        self.slots = asyncio.Semaphore(PARALLEL)
        self.tasks: set[asyncio.Task[None]] = set()
        #: the VMs a read below THRESHOLD cleared at the last look, for their records
        self.cleared: list[tuple[str, str, float, float]] = []
        #: the lines Zulip never took, by their try's record's seq (Codex round 5 on #1003)
        self.unsent: dict[int, Unsent] = {}

    def observe(self, record: dict[str, Any]) -> None:
        """A `disk_grow` record: a try, as it ended, with its failures in a row, and its line
        if Zulip never took it (`unsent`); `cleared`; or `alerted`, that line posted late."""
        instance, at = record.get("instance"), _epoch(record.get("ts"))
        if record.get("event") != "disk_grow" or not isinstance(instance, str) or at is None:
            return
        seq, of = record.get("seq"), record.get("of")
        if record.get("outcome") == ALERTED:
            if type(of) is int:
                self.unsent.pop(of, None)
            return
        workshop, slug, text = (record.get(k) for k in ("workshop", "slug", "text"))
        if record.get("unsent") is True and type(seq) is int and isinstance(text, str):
            if isinstance(workshop, str) and isinstance(slug, str):
                self.unsent[seq] = Unsent(workshop, slug, instance, text)
        outcome, failures = record.get("outcome"), record.get("failures")
        failures = failures if type(failures) is int and failures >= 0 else None
        if isinstance(outcome, str) and (outcome in OUTCOMES or outcome == CLEARED):
            vm = self.vms.setdefault(instance, Vm())
            vm.note(outcome, at, _number(record.get("again")), failures)

    async def run(self) -> None:
        """A look every `PERIOD`; one that fails is logged, and the next one comes. Its growths
        end with it."""
        try:
            while True:
                try:
                    await self.check()
                except Exception as exc:
                    LOG.warning("a look at the disks failed (%s)", type(exc).__name__)
                await asyncio.sleep(PERIOD)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def reading(self, entry: Any, now: float) -> tuple[str, str, float, float] | None:
        """A snapshot's row's VM that EC2 shows running, with a read of that VM (its `instance`,
        the one `status.py` reached) at most `FRESH` old, made since its boot and since its last
        try: its slug, instance, use and read's time."""
        if not isinstance(entry, dict) or entry.get("kind") != "participant":
            return None
        slug, instance, status = entry.get("slug"), entry.get("instance"), entry.get("status")
        if entry.get("state") != "running" or not isinstance(status, dict):
            return None
        if not isinstance(slug, str) or not isinstance(instance, str):
            return None
        if status.get("instance") != instance:  # another VM's read, or one naming none: Codex
            return None  # round 4 on #1003 (a spare handed over since the read)
        used, read = _number(status.get("disk_percent")), _number(status.get("at"))
        down = isinstance(status.get("bridge"), str) and status["bridge"] in DOWN
        if used is None or read is None or down or now - read > FRESH:
            return None
        vm = self.vms.setdefault(instance, Vm())
        if read <= max(_epoch(entry.get("launched_at")) or 0.0, vm.tried):
            return None  # read before its boot, or before its last try: says nothing now
        return slug, instance, used, read

    def still_due(
        self, snapshot: Any, workshop: str, instance: str, now: float
    ) -> tuple[float, float] | None:
        """The instance's reading in `snapshot`, read again, use and time, when it still calls
        for a growth (`reading`, `THRESHOLD`); None otherwise (Codex round 2 on #1003)."""
        sections = snapshot.get("workshops") if isinstance(snapshot, dict) else None
        section = sections.get(workshop) if isinstance(sections, dict) else None
        entries = section.get("vms") if isinstance(section, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            found = self.reading(entry, now)
            if found is not None and found[1] == instance and found[2] >= THRESHOLD:
                return found[2], found[3]
        return None

    def due(self, entry: Any, now: float) -> tuple[str, str, float, float] | None:
        """A snapshot's VM to grow now (module doc): its slug, instance, use and read's time."""
        instance = entry.get("instance") if isinstance(entry, dict) else None
        if isinstance(instance, str) and instance in self.growing:
            return None
        found = self.reading(entry, now)
        if found is None:
            return None
        slug, instance, used, read = found
        vm = self.vms[instance]
        if used < THRESHOLD:
            if vm.failures or vm.held:  # said in a record (`check`), for a restart to keep
                vm.note(CLEARED, now, None)
                self.cleared.append((slug, instance, used, read))
            return None
        return None if vm.held or now < vm.again else found

    async def check(self) -> None:
        server = self.server
        if server.planner is None:
            return
        path = Path(server.paths.state, "console", "snapshot.json")
        snapshot = await asyncio.to_thread(_read, path)
        sections = snapshot.get("workshops") if isinstance(snapshot, dict) else None
        sections = sections if isinstance(sections, dict) else {}
        now = server.clock()
        for bot in server.bots:
            ws = bot.workshop
            section = sections.get(ws.name) if ws is not None else None
            if ws is None or ws.alerts_channel is None or not isinstance(section, dict):
                continue
            entries = section.get("vms")
            for entry in entries if isinstance(entries, list) else []:
                found = self.due(entry, now)
                if found is None or server.planner.busy(ws.name) is not None:
                    continue
                self.growing.add(found[1])
                task = asyncio.create_task(self.grow(bot, ws, *found))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            cleared, self.cleared = self.cleared, []
            for slug, instance, used, read in cleared:
                await server.audit("disk_grow", workshop=ws.name, slug=slug, instance=instance,
                                   outcome=CLEARED, read={"used": used, "at": read},
                                   failures=0)  # fmt: skip
        for bot in server.bots:  # after the growths are under way: a post may take a while
            ws = bot.workshop
            if ws is not None and ws.alerts_channel is not None:
                await self.resend(bot, ws.name, ws.alerts_channel)

    async def resend(self, bot: AdminBot, workshop: str, channel: int) -> None:
        """The workshop's lines Zulip never took, the oldest first, until one fails again: a
        line posted, or one that may have landed, is an `alerted` record (module doc). Nothing
        runs the resize again."""
        owed = sorted(seq for seq, line in self.unsent.items() if line.workshop == workshop)
        for seq in owed:
            line = self.unsent[seq]
            posted, never = await bot.deliver(
                type="stream", to=channel, topic=TOPIC, content=line.text
            )
            if posted is None and never:
                return  # Zulip still doesn't take it: the next look, in the same order
            self.unsent.pop(seq, None)
            await self.server.audit("disk_grow", workshop=workshop, slug=line.slug,
                                    instance=line.instance, outcome=ALERTED, of=seq,
                                    alert=posted)  # fmt: skip

    async def grow(
        self, bot: AdminBot, ws: Workshop, slug: str, instance: str, used: float, read: float
    ) -> None:
        """One try, holding its workshop (module doc): the script, then its line in the alerts
        channel and its record. None, and nothing said, when the snapshot, read again once the
        try has its slot, no longer calls for it (a fresh read below `THRESHOLD`, or none fresh:
        Codex round 2 on #1003), or with a plan applying or approved there: the next look."""
        planner = self.server.planner
        assert planner is not None  # `check`'s
        try:
            async with self.slots:
                path = Path(self.server.paths.state, "console", "snapshot.json")
                snapshot = await asyncio.to_thread(_read, path)
                still = self.still_due(snapshot, ws.name, instance, self.server.clock())
                if still is None:
                    return
                used, read = still
                if not planner.hold_growth(ws.name, slug):  # no await since the re-read:
                    return  # Codex rounds 1 and 2 on #1003
                try:
                    r = await self._run(ws, slug, instance)
                except Exception as exc:  # a try that can't run is a failed one: never a loop
                    r = FAILED | {"reason": f"the try could not run ({type(exc).__name__})"}
                finally:
                    planner.release_growth(ws.name, slug)
            vm = self.vms.setdefault(instance, Vm())
            vm.note(r["outcome"], self.server.clock(), r["again"])
            LOG.info("%s/%s: %s, %s → %s GB", ws.name, slug, r["outcome"], r["from"], r["to"])
            text = alert(slug, used, read, r, vm)
            posted, never = None, False
            if text is not None and ws.alerts_channel is not None:
                posted, never = await bot.deliver(
                    type="stream", to=ws.alerts_channel, topic=TOPIC, content=text
                )
            owed = {"unsent": True, "text": text} if posted is None and never else {}
            head = await self.server.audit(
                "disk_grow", workshop=ws.name, slug=slug, instance=r["instance"] or instance,
                outcome=r["outcome"], read={"used": used, "at": read}, from_gb=r["from"],
                to_gb=r["to"], fs=r["fs"], used=r["used"], again=r["again"], reason=r["reason"],
                failures=vm.failures, alert=posted, **owed,
            )  # fmt: skip
            if owed and text is not None:  # posted again at each look (`resend`)
                self.unsent[head.seq] = Unsent(ws.name, slug, r["instance"] or instance, text)
        except Exception as exc:
            LOG.warning("%s/%s: the disk's growth failed (%s)", ws.name, slug, type(exc).__name__)
        finally:
            self.growing.discard(instance)

    async def _run(self, ws: Workshop, slug: str, instance: str) -> dict[str, Any]:
        """`grow_disk.py --auto-cap` for the row, as the runner runs a step (§2.3), within
        `!grow-disk`'s time; its result, or a failure that says why there is none."""
        server = self.server
        assert server.planner is not None
        runner = server.planner.runner
        try:
            fleet = await server.read_fleet(ws)
            argv = auto_grow(fleet, slug, CAP_GB, instance, layout=server.paths.layout)
        except (ActionError, FleetError) as exc:
            return FAILED | {"reason": str(exc)}
        tmp = _tmpdir(server.paths.tmp)
        out, ran = Capture(), Ran(script(argv))
        try:
            deadline = time.monotonic() + timeout("grow-disk")
            await runner.run(argv, runner.environment(tmp), tmp, deadline, (out,), ran)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        r = result(out.kept())
        if r is None or ran.ended:  # no line of its own (a crash), or the runner ended it
            return (r or FAILED) | {"outcome": "failed", "reason": str(ran)}
        return r


def _tmpdir(root: str) -> str:
    """A new, empty TMPDIR for a try, 0700, where the runner makes a plan's (§2.3)."""
    Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="autogrow-", dir=root)


def _read(path: Path) -> Any:
    try:
        with path.open("rb") as f:
            data = f.read(SNAPSHOT_MAX + 1)
        return json.loads(data) if len(data) <= SNAPSHOT_MAX else None
    except (OSError, ValueError):
        return None
