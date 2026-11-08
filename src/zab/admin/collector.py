"""The collector: what the console shows (docs/ADMIN.md §4.1, §4.2; the cost, §11.2).

`python -m zab.admin collector` is zab-admin-collector.service, a loop, as `zab-admin`, with
the bot's keys used read-only. Every `INTERVAL` it reads, for each workshop of admin.toml:
- **EC2,** in the workshop's region, through the role: its VMs as the starter decides a fleet VM
  (`starter.fleet_vm`), their state, launch time and type, every `PROTECT_EVERY` each one's
  termination protection (`DescribeInstanceAttribute`, §5's Lifecycle statement), and every
  `VOLUME_EVERY` the size of their volumes (`DescribeVolumes`, §5's Read statement, #553), for
  the cost; where that read fails, the cost takes 200 GB (`costs.VOLUME_GB`); matched to
  the roster's rows by `participant` tag or address, as the plans' target sets are
  (`provider.targets`), and to the spares of `spares.json`;
- **the waker's status file** (§1.2): which VMs are opted in, the state it saw last, its last
  wake attempt;
- **the status values,** with their age: its own sweep, `status.py --json` every `SWEEP_EVERY`,
  since a sweep's SSH session restarts the grace period of an idle stop set up before #507
  (§4.1), only of a running VM whose last read shows its idle-stop timer neither enabled nor
  active, made since the VM's current boot and since the last plan that could change its idle
  stop (`IDLE_VERBS`) started or ended (`due`; the review of #494). A VM with no such read is
  never swept. A sweep that gets no answer for a VM notes the try, so it waits `SWEEP_EVERY`
  too. Every `!status` the bots answer feeds it (`note_status`), and is all it has of most
  VMs, since every fleet runs idle stop (Q14). A read that found the VM down keeps the values
  it last gave;
- **the audit log,** from where it left off: the audit view (`auditview`) and the open plans.

Then it writes `console/snapshot.json`, and `console/audit-view.jsonl` when it changed, both
through `Scrubber.record` with the bot's registry of secrets (§2.7: it reads what the bot reads,
`secret_files`) and under a temporary name, `fchmod` 0640, renamed into place (§1.3) for
`zab-console`. Its own state, the cost history and the last status values, is `collector/`,
0600, so that a restart (every deploy) keeps them.
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from zab.admin import auditview, fleet, provider, starter, wakerdown
from zab.admin.actions import LAYOUT, ActionError, Layout, build
from zab.admin.config import AdminConfig, AdminConfigError, Workshop, load_admin_config
from zab.admin.costs import KEEP, History, dollars, spares_said
from zab.admin.grammar import SPARE_RE, Command, Selection
from zab.admin.scrub import Scrubber, read_secrets

LOG = logging.getLogger("zab.admin.collector")
INTERVAL = 60.0
SWEEP_EVERY = 600.0
PROTECT_EVERY = 600.0
VOLUME_EVERY = 600.0
VOLUME_CHUNK = 200  # volume IDs per DescribeVolumes filter, EC2's most values in one
SWEEP_TIMEOUT = 900.0  # a `!status`'s own (§2.4 step 5)
#: The verbs whose apply may change a VM's idle stop: a read before one's `apply_start` or
#: `apply_end` that names the VM says nothing of its idle stop now (`due`).
IDLE_VERBS = frozenset({"idle-stop", "rollout", "launch", "adopt-spare"})
#: How fresh the snapshot must be for `!status` to quote its cost.
FRESH = 600.0
#: status.py's labels for a VM it couldn't read; such a read keeps the values last read.
DOWN = frozenset({"unreachable", "stopped", "stopping", "starting"})
#: The values of `status.py --json`'s VM (schema 1) that the console shows. `disk` is new in
#: #552: a read kept from before it has none, and the console shows its percentage alone;
#: `sentinel` in #588: none (null) reads `?`.
STATUS_KEYS = (
    "slug", "bridge", "sentinel", "zab_version", "codex_login", "claude_login", "git_push",
    "math_tools", "active_sessions", "running_turns", "last_activity", "load", "memory",
    "disk_percent", "disk", "strained", "idle_stop", "doctor_failures", "last_error",
)  # fmt: skip
MAX_ITEMS = 20  # list items kept of a VM's values
MAX_CHARS = 300  # characters kept of a VM's string
_TYPE = re.compile(r"[a-z0-9][a-z0-9.-]{0,40}", re.ASCII)  # an instance type, as named
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)++-[0-9]{1,2}", re.ASCII)  # an AWS region, as named
_FEED = threading.Lock()


@dataclass(frozen=True, slots=True)
class Paths:
    """The files it reads and writes (§1.3); tests pass others."""

    state: str = "/var/lib/zab-admin"  # HOME, as the runner sets it (§2.3)
    secrets: str = "/etc/zab-admin/secrets"
    waker: str = "/var/lib/zab-waker/status/waker.json"
    layout: Layout = LAYOUT

    @property
    def console(self) -> Path:
        return Path(self.state, "console")

    @property
    def own(self) -> Path:
        return Path(self.state, "collector")

    @property
    def audit(self) -> Path:
        return Path(self.state, "audit.jsonl")


def iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def write(path: Path, data: bytes, mode: int) -> None:
    """`path` replaced whole: a temporary name, `fchmod` (every unit has UMask=0077), rename."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _read_json(path: str | Path, limit: int = 16 * 1024 * 1024) -> Any:
    """The file's JSON, or None when it is missing, too large or not JSON."""
    try:
        with Path(path).open("rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def secret_files(config: AdminConfig, paths: Paths) -> tuple[list[Path], list[Path]]:
    """The registry of `zab-admin` (§2.7), as `AdminServer._secret_files` has it (a test keeps
    them equal): every admin bot's key, the provisioner's, every key in `secrets/bots/`, each
    participant bot's in every `out/`, the GitHub token and the fleet's SSH key."""
    bots = [ws.bot.zuliprc for ws in config.workshops]
    bots += [config.global_bot.zuliprc] if config.global_bot else []  # in the bot's order
    zuliprcs = [Path(p) for p in (*bots, paths.layout.provisioner_zuliprc)]
    zuliprcs += sorted(Path(paths.secrets, "bots").glob("*.zuliprc"))
    for ws in config.workshops:
        zuliprcs += sorted(Path(ws.out_dir).glob("*/*.zuliprc"))
    keys = [Path(paths.secrets, "github-token"), Path(paths.state, ".ssh", "id_ed25519")]
    return zuliprcs, keys


def status_values(vm: Any) -> dict[str, Any] | None:
    """The values the console shows of one VM of `status.py --json`, bounded; None for no VM."""
    if not isinstance(vm, dict) or not isinstance(vm.get("slug"), str):
        return None

    def bound(value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return value[:MAX_CHARS]
        if isinstance(value, list) and depth < 3:
            return [bound(v, depth + 1) for v in value[:MAX_ITEMS]]
        if isinstance(value, dict) and depth < 3:
            return {str(k)[:40]: bound(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
        return value if isinstance(value, int | float | bool) or value is None else None

    return {key: bound(vm.get(key)) for key in STATUS_KEYS}


def note_status(state: str, workshop: str, answer: Any, now: float) -> None:
    """A bot's `!status` read of `workshop` (its `status.py --json` answer, scrubbed), for the
    collector: into `collector/reads/<workshop>.json`, 0600, each VM's values with the time of
    its own last read, so that two reads between the collector's passes both count (Codex on
    #494); a VM's entry goes after `KEEP`. Each read is combined as the collector combines it
    (`combine`), so that a good read and a failed one between two passes both count (Codex on
    #494, 6260727975). The bot is one process: `_FEED` orders its writes."""
    vms = answer.get("vms") if isinstance(answer, dict) else None
    if not isinstance(vms, list):
        return
    reads = Path(state, "collector", "reads")
    reads.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = reads / f"{workshop}.json"
    with _FEED:
        kept = _read_json(path)
        entries = kept.get("vms") if isinstance(kept, dict) else None
        entries = {
            slug: entry
            for slug, entry in (entries if isinstance(entries, dict) else {}).items()
            if isinstance(entry, dict) and _time(entry.get("at")) >= now - KEEP
        }
        for values in filter(None, map(status_values, vms)):
            new = combine(entries.get(values["slug"]), values, now)
            if new is not None:
                entries[values["slug"]] = new
        write(path, json.dumps({"vms": entries}).encode(), 0o600)


def combine(
    old: dict[str, Any] | None, values: dict[str, Any], at: float, **extra: Any
) -> dict[str, Any] | None:
    """A VM's entry after a read of `values` made at `at` (with `extra`, its `source`), or None
    to keep `old`, whatever order the reads arrive in (Codex on #494, 6260900537). Its values
    are the newest good read's. A read that found the VM down (`DOWN`) keeps them and notes the
    try (`tried`), unless a later try is noted; a good read keeps a try made after it. Always a
    new dict: never one the snapshot is reading."""
    if old is None:
        return {**values, "at": at, **extra}
    if values["bridge"] in DOWN:
        if _tried(old) > at:
            return None
        return {**old, "tried": {"at": at, "bridge": values["bridge"]}}
    if old.get("bridge") in DOWN:  # it holds no good read: it is itself a try
        failed: Any = {"at": _time(old.get("at")), "bridge": old.get("bridge")}
        tried = old.get("tried")
        if isinstance(tried, dict) and _time(tried.get("at")) > failed["at"]:
            failed = tried
    elif _time(old.get("at")) > at:
        return None  # newer good values are kept
    else:
        failed = old.get("tried")
    new = {**values, "at": at, **extra}
    if isinstance(failed, dict) and _time(failed.get("at")) > at:
        new["tried"] = failed
    return new


def _time(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def cost_line(state: str, workshop: str, now: float) -> str | None:
    """`!status`'s line (§11.2): `cost (estimate): $412 this month, $1,050 projected · 9 of 40
    running · spares: 1 of 3 running`, the spares apart (#715: Harvard's `8 of 47 running` was 5
    of the table's VMs and 3 spares), from a snapshot no older than `FRESH`; None without one, or
    before the workshop's first EC2 read. The snapshot is written every pass, EC2 read or not,
    so a read older than `FRESH` is named: `cost (estimate, EC2 last read 2026-10-19 14:02
    UTC): …` (Codex on #494). A workshop in a region without prices reads `cost (estimate): no
    prices for eu-west-1 · …` (#499)."""
    snapshot = _read_json(Path(state, "console", "snapshot.json"))
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("at"), int | float):
        return None
    workshops = snapshot.get("workshops")
    ws = workshops.get(workshop) if isinstance(workshops, dict) else None
    cost = ws.get("cost") if isinstance(ws, dict) else None
    if now - snapshot["at"] > FRESH or not isinstance(cost, dict):
        return None
    ec2 = ws.get("ec2")  # type: ignore[union-attr]
    read = _epoch(ec2.get("at")) if isinstance(ec2, dict) else None
    if read is None:
        return None
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(read))
    label = "estimate" if now - read <= FRESH else f"estimate, EC2 last read {when}"
    region = cost.get("unpriced_region")
    try:
        running, total = int(cost["running"]), int(cost["vms"])
        if region is None:
            month, projected = dollars(cost["month"]), dollars(cost["projected"])
    except (KeyError, TypeError, ValueError):
        return None
    fleet = f"{running} of {total} running{spares_said(cost)}"
    if region is not None:  # no prices for the workshop's region: no dollars at all (#499)
        where = region if isinstance(region, str) and _REGION.fullmatch(region) else "its region"
        return f"cost ({label}): no prices for {where} · {fleet}"
    shown = f"{month} this month, {projected} projected"
    listed = cost.get("unpriced")
    listed = listed if isinstance(listed, list) else []
    unpriced = [t for t in listed if isinstance(t, str) and _TYPE.fullmatch(t)]
    # A VM with a run of a type without a price, or present with such a type now, leaves all
    # its dollars out of the totals (`Seen.cost`, `History.workshop`): say so (Codex on #494;
    # the reviews of #490, note 6, and of #494, note 6)
    if listed:
        kinds = ", ".join(unpriced) or "an odd type"
        shown += f", leaving out every VM that ran as, or is now, {kinds} (no price)"
    return f"cost ({label}): {shown} · {fleet}"


class Aws:
    """EC2 through the role, read only: one client per region (`provider.ec2`'s settings)."""

    def __init__(self) -> None:
        self.clients: dict[str, Any] = {}

    def client(self, region: str) -> Any:
        if region not in self.clients:
            boto3, config = starter.sdk()
            retries = {"total_max_attempts": 2}
            settings = config(connect_timeout=10, read_timeout=20, retries=retries)
            self.clients[region] = boto3.client("ec2", region_name=region, config=settings)
        return self.clients[region]

    def describe(self, region: str, filters: list[dict]) -> list[dict]:
        return starter.Ec2(self.client(region)).describe(filters)

    def volumes(self, region: str, ids: list[str]) -> dict[str, int]:
        """The size in GB of each volume of `ids` that EC2 shows."""
        sizes: dict[str, int] = {}
        pages = self.client(region).get_paginator("describe_volumes")
        for at in range(0, len(ids), VOLUME_CHUNK):
            chunk = [{"Name": "volume-id", "Values": ids[at : at + VOLUME_CHUNK]}]
            for page in pages.paginate(Filters=chunk):
                for volume in page.get("Volumes") or []:
                    if isinstance(volume.get("Size"), int):
                        sizes[str(volume.get("VolumeId"))] = volume["Size"]
        return sizes

    def protected(self, region: str, instance: str) -> bool:
        answer = self.client(region).describe_instance_attribute(
            InstanceId=instance, Attribute="disableApiTermination"
        )
        return answer["DisableApiTermination"]["Value"] is True


def _epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        with suppress(ValueError):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return None


def ec2_vm(raw: dict) -> dict[str, Any]:
    """What the collector keeps of DescribeInstances' instance (a `starter.fleet_vm`)."""
    tags = {t.get("Key"): t.get("Value") for t in raw.get("Tags") or [] if isinstance(t, dict)}
    mappings = [m for m in raw.get("BlockDeviceMappings") or [] if isinstance(m, dict)]
    attached = [_epoch((m.get("Ebs") or {}).get("AttachTime")) for m in mappings]
    volumes = [(m.get("Ebs") or {}).get("VolumeId") for m in mappings]
    return {
        "instance": str(raw.get("InstanceId")),
        "tag": str(tags.get("participant", "")).strip(),
        "state": str((raw.get("State") or {}).get("Name")),
        "type": str(raw.get("InstanceType") or "?"),
        "launched": _epoch(raw.get("LaunchTime")),
        "created": min((t for t in attached if t is not None), default=None),
        "ips": sorted(provider._ips(raw)),
        "volumes": sorted(v for v in volumes if isinstance(v, str) and v),
    }


Run = Callable[[list[str], dict[str, str], str], str]


class Closed(RuntimeError):
    """The collector is closing: no sweep starts."""


class Gate:
    """A collector's sweep processes: once `close` has run, none starts, and those running are
    killed, their process groups whole. A start and its registration happen under the same lock
    as the close, so none slips between (Codex on #494)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.groups: set[int] = set()
        self.closed = False

    def start(self, argv: list[str], env: dict[str, str], cwd: str) -> subprocess.Popen[bytes]:
        with self.lock:
            if self.closed:
                raise Closed("the collector is closing")
            process = subprocess.Popen(  # noqa: S603 (the bot's own argument list, actions.build)
                argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )  # fmt: skip
            self.groups.add(process.pid)
        return process

    def done(self, pid: int) -> None:
        with self.lock:
            self.groups.discard(pid)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for group in self.groups:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(group, signal.SIGKILL)


def run_status(argv: list[str], env: dict[str, str], cwd: str, gate: Gate | None = None) -> str:
    """`status.py --json`'s stdout, in its own process group, killed whole at the timeout or
    when `gate` closes."""
    gate = gate or Gate()
    process = gate.start(argv, env, cwd)
    try:
        out, _ = process.communicate(timeout=SWEEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    finally:
        gate.done(process.pid)
    return out.decode("utf-8", "replace")


class Collector:
    def __init__(
        self,
        config: AdminConfig,
        *,
        paths: Paths | None = None,
        aws: Any = None,
        run: Run = run_status,
        clock: Callable[[], float] = time.time,
        background: bool = True,
    ) -> None:
        self.config, self.paths, self.clock = config, paths or Paths(), clock
        self.gate = Gate()
        self.run: Run = partial(run_status, gate=self.gate) if run is run_status else run
        self.aws = aws if aws is not None else Aws()
        self.scrubber = Scrubber()
        self._signature: tuple[Any, ...] = ()
        own = _read_json(self.paths.own / "state.json")
        own = own if isinstance(own, dict) else {}
        self.history = History.of(own.get("history"))
        #: "<ws>/<slug>" → its last values, with their time (`at`) and `source`
        saved = own.get("status")
        self.status: dict[str, dict[str, Any]] = {
            k: v for k, v in (saved if isinstance(saved, dict) else {}).items()
            if isinstance(v, dict)
        }  # fmt: skip
        self.rows: dict[str, list[dict[str, str]]] = {}  # per workshop: its roster's rows
        self.vms: dict[str, list[dict[str, Any]]] = {}  # per workshop: the last EC2 read
        self.ec2: dict[str, dict[str, Any]] = {}
        self.protection: dict[str, tuple[float, bool | None]] = {}
        #: per workshop: when its volumes were last read, the IDs asked, and the sizes then
        self.sizes: dict[str, tuple[float, frozenset[str], dict[str, int]]] = {}
        self.reads: dict[str, int] = {}  # a feed file's mtime, once merged
        #: "<ws>/<slug>" (or "<ws>/*", for a record without its targets) → the time of the last
        #: `apply_start` or `apply_end` of `IDLE_VERBS` naming it, from every record of the log,
        #: which the view reads whole at each start (Codex round 4 on #494)
        self.idle_changed: dict[str, float] = {}
        self.view = auditview.View(config.plan_ttl_minutes * 60, on_record=self._noted)
        #: the ledger of the whole log, for the rows a `!retire` ended, which the snapshot leaves
        #: out once they have no VM (#559)
        self.ledger = fleet.Ledger()
        self.sweeps = ThreadPoolExecutor(1) if background else None
        self.sweep: Future[None] | None = None
        self.lock = threading.Lock()

    # ------------------------------------------------------------------------------- pass

    def tick(self) -> dict[str, Any]:
        """One pass (module doc); the snapshot as written."""
        now = self.clock()
        self._secrets()
        for ws in self.config.workshops:
            self._ec2(ws, now)
        self._feeds()
        if self.view.follow(self.paths.audit):
            records = [self.scrubber.record(r) for r in self.view.records()]
            data = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
            write(self.paths.console / "audit-view.jsonl", data.encode(), 0o640)
        snapshot = self._snapshot(now)
        data = json.dumps(self.scrubber.record(snapshot), indent=1, sort_keys=True)
        write(self.paths.console / "snapshot.json", data.encode(), 0o640)
        self._sweep(now)
        self._save()
        return snapshot

    def _secrets(self) -> None:
        zuliprcs, files = secret_files(self.config, self.paths)
        seen = []
        for path in [*zuliprcs, *files]:
            try:
                st = path.stat()
                seen.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                seen.append((str(path),))
        if tuple(seen) != self._signature:
            self.scrubber.set_secrets(read_secrets(zuliprcs=zuliprcs, files=files))
            self._signature = tuple(seen)

    def _ec2(self, ws: Workshop, now: float) -> None:
        """The workshop's roster and VMs, each VM matched to its row or spare, and seen by the
        cost history. A failed read keeps the last one, with the error."""
        try:
            self.rows[ws.name] = rows = fleet.read_roster(ws.roster)
            spares = provider.spare_addresses(ws)
        except Exception as exc:
            LOG.warning("%s: roster or spares not read (%s)", ws.name, type(exc).__name__)
            rows, spares = self.rows.get(ws.name, []), {}
        filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in ws.tags]
        filters += [
            {"Name": "tag-key", "Values": ["participant"]},
            {"Name": "instance-state-name", "Values": list(provider.STATES)},
        ]
        try:
            listed = self.aws.describe(ws.aws.region, filters)
        except Exception as exc:  # the last read stays, with the error and its time
            self.ec2.setdefault(ws.name, {"at": None})["error"] = starter.aws_code(exc)
            LOG.warning("%s: EC2 not read (%s)", ws.name, starter.aws_code(exc))
            return
        by_ip = {r["ip"]: r["slug"] for r in rows if r.get("ip")}
        slugs = {r["slug"] for r in rows}
        by_spare_ip = {ip: name for name, ip in spares.items()}
        vms = [ec2_vm(raw) for raw in listed if starter.fleet_vm(raw, ws)]
        sizes = self._sizes(ws, vms, now)
        for vm in vms:
            tag = vm["tag"].lower()
            row = tag if tag in slugs else next(filter(None, map(by_ip.get, vm["ips"])), None)
            spare = tag if SPARE_RE.fullmatch(tag) else None
            spare = spare or next(filter(None, map(by_spare_ip.get, vm["ips"])), None)
            vm["kind"] = "participant" if row else "spare" if spare else "unlisted"
            vm["slug"] = row or spare or vm["tag"]
            vm["protected"] = self._protected(ws, vm["instance"], now)
            known = [sizes[v] for v in vm["volumes"] if v in sizes]
            whole = bool(known) and len(known) == len(vm["volumes"])
            vm["volume_gb"] = sum(known) if whole else None  # else VOLUME_GB, in the cost
            self.history.observe(vm["instance"], ws.name, vm["slug"], vm, now)
        self.vms[ws.name], self.ec2[ws.name] = vms, {"at": now, "error": None}

    def _sizes(self, ws: Workshop, vms: list[dict[str, Any]], now: float) -> dict[str, int]:
        """The workshop's volumes' sizes, read again every `VOLUME_EVERY`, and at once for a
        volume the last read didn't ask for; a failed read keeps the last ones (none:
        `costs.VOLUME_GB`)."""
        at, asked, sizes = self.sizes.get(ws.name, (-VOLUME_EVERY, frozenset(), {}))
        ids = sorted({v for vm in vms for v in vm["volumes"]})
        if now - at < VOLUME_EVERY and set(ids) <= asked:
            return sizes
        try:
            sizes = self.aws.volumes(ws.aws.region, ids) if ids else {}
        except Exception as exc:  # no DescribeVolumes yet (ADMIN §8.4, v0.2.9): 200 GB each
            LOG.warning("%s: volume sizes not read (%s)", ws.name, starter.aws_code(exc))
        self.sizes[ws.name] = (now, frozenset(ids), sizes)
        return sizes

    def _protected(self, ws: Workshop, instance: str, now: float) -> bool | None:
        at, value = self.protection.get(instance, (-PROTECT_EVERY, None))
        if now - at < PROTECT_EVERY:
            return value
        try:
            value = bool(self.aws.protected(ws.aws.region, instance))
        except Exception as exc:
            code = starter.aws_code(exc)
            LOG.warning("%s: protection of %s not read (%s)", ws.name, instance, code)
            value = None
        self.protection[instance] = (now, value)
        return value

    def _feeds(self) -> None:
        """Each workshop's `!status` reads (`note_status`), when the file changed."""
        for ws in self.config.workshops:
            path = self.paths.own / "reads" / f"{ws.name}.json"
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if self.reads.get(ws.name) == mtime:
                continue
            self.reads[ws.name] = mtime
            feed = _read_json(path)
            entries = feed.get("vms") if isinstance(feed, dict) else None
            for entry in entries.values() if isinstance(entries, dict) else ():
                if isinstance(entry, dict) and _time(entry.get("at")):
                    self._merge(ws.name, [entry], _time(entry["at"]), "!status")
                    tried = entry.get("tried")  # a later read that found the VM down
                    if isinstance(tried, dict) and tried.get("bridge") in DOWN:
                        down = {"slug": entry.get("slug"), "bridge": tried["bridge"]}
                        self._merge(ws.name, [down], _time(tried.get("at")), "!status")

    def _merge(self, workshop: str, vms: Any, at: float, source: str) -> None:
        """Each VM's values, read at `at`, combined with those kept (`combine`): the newest
        good read's values; a read that found the VM down keeps them, and notes the try."""
        for vm in vms if isinstance(vms, list) else []:
            values = status_values(vm)
            if values is None:
                continue
            key = f"{workshop}/{values['slug']}"
            with self.lock:
                new = combine(self.status.get(key), values, at, source=source)
                if new is not None:
                    self.status[key] = new

    # ------------------------------------------------------------------------- the sweep

    def due(self, ws: Workshop, now: float) -> list[str]:
        """The roster slugs to sweep now (module doc): running; their last read shows the
        idle-stop timer neither enabled nor active, and was made after the VM's current boot
        and after every `apply_start` and `apply_end` of `IDLE_VERBS` that names the VM
        (`idle_changed`); read or tried `SWEEP_EVERY` ago or more."""
        slugs = []
        for vm in self.vms.get(ws.name, []):
            values = self.status.get(f"{ws.name}/{vm['slug']}")
            idle = values.get("idle_stop") if values else None
            read = _time(values.get("at")) if values else 0.0
            if (
                vm["state"] == "running"
                and vm["kind"] == "participant"
                and isinstance(idle, dict)
                and idle.get("enabled") is False
                and idle.get("active") is False
                and read > _time(vm.get("launched"))
                and read > self.idle_changed.get(f"{ws.name}/{vm['slug']}", 0.0)
                and read > self.idle_changed.get(f"{ws.name}/*", 0.0)
                and now - _tried(values) >= SWEEP_EVERY
            ):
                slugs.append(vm["slug"])
        return sorted(slugs)

    def _noted(self, record: dict[str, Any]) -> None:
        """An audit record, whole (`auditview.View`'s hook): an `apply_start` or `apply_end` of
        `IDLE_VERBS` marks each of its targets, all of them, as changed at its time; one without
        its targets (a runner older than `ApplyEnd`'s) marks the whole workshop (fails closed).
        The view reads the log from its start at each start, so nothing ages out (Codex round
        4 on #494: the view keeps 200 records per workshop, and 50 targets each)."""
        self.ledger.observe(record)
        workshop, ts = record.get("workshop"), record.get("ts")
        if (
            record.get("event") not in ("apply_start", "apply_end")
            or record.get("verb") not in IDLE_VERBS
            or not isinstance(workshop, str)
            or not isinstance(ts, str)
        ):
            return
        at = _epoch(ts) or 0.0
        targets = record.get("targets")
        slugs = [t for t in targets if isinstance(t, str)] if isinstance(targets, list) else ["*"]
        for slug in slugs:
            key = f"{workshop}/{slug}"
            self.idle_changed[key] = max(self.idle_changed.get(key, 0.0), at)

    def _sweep(self, now: float) -> None:
        if self.sweep is not None and not self.sweep.done():
            return
        work = [(ws, slugs) for ws in self.config.workshops if (slugs := self.due(ws, now))]
        if not work:
            return
        if self.sweeps is None:
            self._sweep_all(work)
        else:
            self.sweep = self.sweeps.submit(self._sweep_all, work)

    def _sweep_all(self, work: list[tuple[Workshop, list[str]]]) -> None:
        """Each workshop's sweep. A VM it gets no answer for (status.py failed, timed out,
        printed no JSON, or left it out) has the try noted, so that it waits `SWEEP_EVERY`
        before the next (the review of #494)."""
        for ws, slugs in work:
            if self.gate.closed:  # closing: no next workshop (Codex on #494)
                return
            at, vms = self.clock(), []
            try:
                vms = self._read(ws, slugs)
            except Exception as exc:
                LOG.warning("%s: the sweep failed (%s)", ws.name, type(exc).__name__)
            vms = vms if isinstance(vms, list) else []
            self._merge(ws.name, vms, at, "sweep")
            answered = {vm.get("slug") for vm in vms if isinstance(vm, dict)}
            with self.lock:
                for slug in slugs:
                    old = self.status.get(f"{ws.name}/{slug}")
                    if slug not in answered and old is not None and _tried(old) < at:
                        tried = {"at": at, "bridge": "no answer"}
                        self.status[f"{ws.name}/{slug}"] = {**old, "tried": tried}

    def _read(self, ws: Workshop, slugs: list[str]) -> Any:
        """`!status`'s own argument list (`actions.build`) for `slugs`, run as the runner would
        (§2.3), its answer's VMs."""
        current = fleet.read_fleet(self.config, ws, fleet.Ledger(), now=self.clock())
        command = Command("status", selection=Selection(slugs=tuple(slugs)))
        try:
            argv = list(build(command, current, layout=self.paths.layout).steps[0].argv)
        except ActionError as exc:
            LOG.warning("%s: no sweep (%s)", ws.name, exc)
            return []
        with tempfile.TemporaryDirectory(prefix="zab-sweep-") as tmp:
            env = {"PATH": "/usr/bin:/bin", "HOME": self.paths.state, "LANG": "C.UTF-8"}
            out = self.run(argv, env | {"TMPDIR": tmp}, tmp)
        try:
            answer = json.loads(out)
        except ValueError:
            return []
        return answer.get("vms") if isinstance(answer, dict) else []

    # ------------------------------------------------------------------------ the snapshot

    def _snapshot(self, now: float) -> dict[str, Any]:
        waker = _read_json(self.paths.waker, 4 * 1024 * 1024)
        waker = waker if isinstance(waker, dict) else {}
        written = waker.get("written_at")
        written = written if isinstance(written, int | float) else None
        entries = {
            e["name"]: e
            for e in waker.get("workshops") or []
            if isinstance(e, dict) and isinstance(e.get("name"), str)
        }
        workshops = {}
        config = f"{self.paths.layout.waker}/config.toml"
        for ws in self.config.workshops:
            opted = None if written is None else _opted(entries.get(ws.name) or {})
            workshops[ws.name] = self._workshop(ws, opted, now)
            kind, since = wakerdown.state(self.paths.waker, config, ws.name, now)
            workshops[ws.name]["waker_down"] = iso(since) if kind == wakerdown.DOWN else None
        self.history.prune(now)
        return {
            "schema": 1,
            "at": now,
            "written_at": iso(now),
            "waker_at": iso(written),
            "workshops": workshops,
        }

    def _workshop(self, ws: Workshop, opted: dict[str, Any] | None, now: float) -> dict[str, Any]:
        """One workshop's section: its VMs, its rows without one, its plans and its cost."""
        listed: list[dict[str, Any]] = []
        for vm in self.vms.get(ws.name, []):
            entry = self.history.seen.get(vm["instance"])
            cost = entry.cost(now, True, ws.aws.region) if entry else None
            waker = None if opted is None else opted.get(vm["instance"], {"opted_in": False})
            row = vm["kind"] == "participant"
            listed.append({
                "slug": vm["slug"], "kind": vm["kind"], "instance": vm["instance"],
                "state": vm["state"], "type": vm["type"], "launched_at": iso(vm["launched"]),
                "protected": vm["protected"], "waker": waker,
                "status": self.status.get(f"{ws.name}/{vm['slug']}") if row else None,
                "cost": cost.to_json() if cost else None,
            })  # fmt: skip
        launched = {vm["slug"] for vm in listed if vm["kind"] == "participant"}
        state = self.ledger.workshop(ws.name)
        retired = state.retired if state else {}
        for row in self.rows.get(ws.name, []):
            if row["slug"] not in launched and row["slug"] not in retired:
                status = self.status.get(f"{ws.name}/{row['slug']}")
                listed.append({"slug": row["slug"], "kind": "participant", "state": "no VM",
                               "status": status})  # fmt: skip
        present = {vm["instance"] for vm in self.vms.get(ws.name, [])}
        # the spares apart (#715): `!status`'s line and the console say them on their own
        spares = [vm for vm in listed if vm["kind"] == "spare"]
        running = sum(1 for vm in listed if vm["state"] == "running" and vm["kind"] != "spare")
        counts = {
            "running": running, "vms": len(present) - len(spares), "spares": len(spares),
            "spares_running": sum(1 for vm in spares if vm["state"] == "running"),
        }  # fmt: skip
        ec2 = self.ec2.get(ws.name, {})
        totals = self.history.workshop(ws.name, present, now, ws.aws.region)
        return {
            "region": ws.aws.region,
            "channels": sorted(ws.bot.channels),
            "site": _site(ws.bot.zuliprc),
            "ec2": {"at": iso(ec2.get("at")), "error": ec2.get("error")},
            "vms": listed,
            "plans": self.view.plans(ws.name, now),
            "cost": totals | counts,
        }

    def _save(self) -> None:
        """Its state, 0600: the cost history, and the status values of the workshops it serves
        that are less than `costs.KEEP` old."""
        names = {ws.name for ws in self.config.workshops}
        horizon = self.clock() - KEEP
        with self.lock:
            for key, values in list(self.status.items()):
                if key.partition("/")[0] not in names or _tried(values) < horizon:
                    del self.status[key]
            data = {"history": self.history.to_json(), "status": dict(self.status)}
        try:
            self.paths.own.mkdir(mode=0o700, exist_ok=True)
            write(self.paths.own / "state.json", json.dumps(data).encode(), 0o600)
        except OSError as exc:
            LOG.warning("its state not saved (%s)", exc.strerror)

    def close(self) -> None:
        """End the sweep, so that the exit (a restart for a new admin.toml) doesn't wait for it:
        the interpreter joins the sweep's thread at exit. The gate kills its running process
        groups and starts no other, and the sweep stops before its next workshop (the review of
        #494; Codex on #494)."""
        self.gate.close()
        if self.sweeps is not None:
            self.sweeps.shutdown(wait=False, cancel_futures=True)


def _tried(values: dict[str, Any]) -> float:
    """When a VM's values were last read, or a read of them last tried."""
    tried = values.get("tried")
    at = tried.get("at") if isinstance(tried, dict) else None
    return max(float(values.get("at") or 0), float(at or 0))


def _opted(waker: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The workshop's waker entry (the status file's), per instance: opted in, the state it saw
    last, its last wake attempt, and when it checked."""
    out: dict[str, dict[str, Any]] = {}
    checked = waker.get("checked_at")
    for bot in waker.get("bots") or []:
        if not isinstance(bot, dict) or not isinstance(bot.get("instance"), str):
            continue
        entry = out.setdefault(bot["instance"], {"opted_in": True, "last_wake": None})
        entry["state"] = str(bot.get("state", "unknown"))[:20]
        entry["at"] = iso(checked) if isinstance(checked, int | float) else None
        wake = bot.get("last_wake_attempt")
        if isinstance(wake, int | float) and not isinstance(wake, bool):
            entry["last_wake"] = max(filter(None, [entry["last_wake"], iso(wake)]))
    return out


_SITE = re.compile(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", re.ASCII)


def _site(zuliprc: str) -> str | None:
    """The bot's Zulip site, for the console's link to its channel; never its key."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(zuliprc, encoding="utf-8")
        site = parser.get("api", "site", fallback="").strip().rstrip("/")
    except (configparser.Error, UnicodeDecodeError):
        return None
    return site if _SITE.fullmatch(site) else None


def serve(
    collector: Collector,
    config: Path,
    *,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """A pass every `INTERVAL`, until admin.toml changes (`!new-workshop` writes it, Codex on
    #494): then it exits, and systemd starts it again on the new one (`Restart=always`), as the
    starter does (`starter.fingerprint`)."""
    seen = starter.fingerprint(config)
    while True:
        if starter.fingerprint(config) != seen:
            LOG.info("admin.toml changed: exiting, for systemd to start the collector again")
            return 0
        started = time.monotonic()
        try:
            collector.tick()
        except Exception as exc:  # its type only: a traceback could quote a VM's text
            LOG.warning("a pass failed (%s)", type(exc).__name__)
        if once:
            return 0
        sleep(max(1.0, INTERVAL - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    """`python -m zab.admin collector --config PATH [--once]`: the loop, or one pass."""
    parser = argparse.ArgumentParser(prog="python -m zab.admin collector")
    parser.add_argument("--config", default="/etc/zab-admin/admin.toml")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        config = load_admin_config(options.config)
    except AdminConfigError as exc:  # admin.toml holds no secrets (§1.3)
        print(f"zab-admin-collector: {exc}", flush=True)
        return 1
    collector = Collector(config, background=not options.once)
    try:
        return serve(collector, Path(options.config), once=options.once)
    except KeyboardInterrupt:
        return 0
    finally:
        collector.close()
