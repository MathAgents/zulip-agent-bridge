#!/usr/bin/env python3
"""Start or stop workshop VMs and spares (docs/ADMIN.md §3, action 6; README.md, "power.py").

    uv run --with boto3 python workshop/aws/power.py --roster roster.csv (--start | --stop) \\
        (--all [--spares] | --spares | --only SLUG …) --tag KEY=VALUE [--tag …] --region R \\
        [--spares-file FILE] [--wait] [--dry-run]

Replaces the raw `aws ec2 start-instances` / `stop-instances` (README.md, "power.py", by hand),
and backs the admin bot's `!start` and `!stop`.

- **Targets**, always named (docs/ADMIN.md §2.2: `all` never includes the spares, and there is
  no implicit "everyone"): `--all`, every roster row with an `ip` in the run's region;
  `--spares`, every spare of this scope and region in `spares.json` that is neither adopted nor
  handed over (its address in a roster row); both, for the whole fleet; or `--only`
  (repeatable), roster slugs and spares (`zab-spare-N`), with neither of the others. What
  `--all` or `--spares` can't serve is listed as `skip`. A name given with `--only` that can't
  be served is refused, with every other refusal, before any AWS call: no `ip`, another region,
  unknown, adopted, or handed over.
- **Two rows with one `ip`** are a roster error, since either would act on the other's VM: both
  are refused under `--only`, and otherwise listed as `skip` and counted as failures (exit 1).
- **Matching.** A row's VM is the instance whose public address is the row's `ip`, as
  `workshop/admin/status.py` finds it: after a hand-over by hand, that is a spare tagged
  `participant=zab-spare-N`. A spare's VM is the instance at its address in `spares.json`. Only
  instances that carry every scope tag **and** a `participant` tag, and no `zab-admin` tag, are
  candidates (docs/ADMIN.md §3.2): the admin server is never started or stopped, even if the
  roster names its address. An instance tagged as another participant (a typo in `ip`) is
  refused too.
- **One region,** `--region`, required (#808): a row whose `region` column names another is
  refused or skipped. A row with a VM and an empty `region` is one launched before launch.py
  recorded it, in us-west-2, as `status.py` reads it (`UNRECORDED_REGION`).
- **Idempotent.** A VM already in the wanted state, or on its way there, is left alone. One
  that is on its way to the other state (`stopping` for `--start`, `pending` for `--stop`) is a
  failure: rerun in a minute. A failure on one VM is reported and the others still run; the exit
  code is then 1.
- `--wait` waits until each VM it serves is stopped, or running with its port 22 answering
  (docs/ADMIN.md §11.5: the month-end's sync needs a reachable VM). It waits for all of them at
  once, under **one deadline**, `WAIT_S` (10 minutes) after the last request (#334). In each
  round, one EC2 lookup reads every VM's state, and the port 22 of each running VM is tried, in
  parallel (`answering`); the next round starts `POLL_S` (5 s) after one ends. Every VM is
  read at least once (unless the first lookup fails or outlasts the deadline), and tried
  whenever it runs. The rounds stop at the deadline, so the wait takes at most 10 minutes plus
  one last round, however many VMs fail. That round is one lookup, which asks for no further
  page past the deadline and whose every request gives up within about a minute (`EC2_CONFIG`:
  two tries, each at most 10 s to connect and 20 s to read), and 5 s of SSH tries per
  `SSH_PARALLEL` (128) running VMs whose port 22 is silent. A lookup cut short (or failed)
  fails every VM still waiting. For up to 128 such VMs that is at most about 11 minutes, below
  the admin bot's 15-minute timeout (docs/ADMIN.md §2.4). A VM not there by then fails, as does
  one moving the other way or gone; it counts as `failed` only, never also as `start`, `stop`
  or `unchanged`.
- `--dry-run` reads EC2 and prints one line per target, the same as a real run but with "would";
  it changes nothing. A failed EC2 lookup is one `FAILED` line and exit 1, not a traceback.
- `StartStop` (#995) is not this script's: `push_config.py --start-stopped` and `trust_fix.py`'s
  start each row's stopped VM for that row's work, and stop it again as soon as it is done.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypeVar

from aws_common import (
    Ec2,
    Scope,
    add_tag_argument,
    error_line,
    has_participant,
    instance_ips,
    is_spare_name,
    only_slugs,
    parse_tags,
    participants,
    read_roster,
    scope_label,
    scope_tags,
    scope_text,
    slug_of,
    ssh_answers,
    tags_of,
)
from launch import SPARES_FILE

LIVE_STATES = ("pending", "running", "stopping", "stopped")
#: The region of a row with a VM but no region recorded: one launched before launch.py
#: recorded it, which the first fleet's were, in us-west-2. launch.py records it for every
#: VM it makes (#808: no script takes it as the workshop's region any more). As the admin
#: bot reads such a row (`zab.admin.actions`).
UNRECORDED_REGION = "us-west-2"
#: Keyed by `start`: the states in which a VM needs no call (already there, or on its way), and
#: the one it is started or stopped from. The fourth live state is on its way to the other.
DONE = {True: ("running", "pending"), False: ("stopped", "stopping")}
FROM = {True: "stopped", False: "running"}
#: `--wait`'s one deadline for every VM it serves, from the last request (#334), and how often
#: it reads them all (one EC2 lookup) and tries the port 22 of those running.
WAIT_S = 600.0
POLL_S = 5.0
#: How many SSH tries run at once: for up to this many VMs, a round takes one try's 5 s.
SSH_PARALLEL = 128
#: The EC2 client's botocore `Config`: every request (one page of a lookup, a start, a stop)
#: gives up within about a minute, two tries of at most 10 s to connect and 20 s to read, instead
#: of botocore's 5 tries of 60 s and 60 s; a lookup may make several (one per page), and only
#: `settle`'s stop at the deadline. So a round of `--wait` stays short (#334). As
#: `zab.admin.provider`'s client.
EC2_CONFIG = {"connect_timeout": 10, "read_timeout": 20, "retries": {"total_max_attempts": 2}}


class SelectionError(Exception):
    """The roster or `spares.json` refuses the selection: nothing was looked up or changed."""


@dataclass
class Target:
    name: str
    ip: str
    spare: bool = False
    host: str = ""  # the row's recorded `ws-<tag>`, for a VM tagged before slugs


@dataclass
class Selection:
    targets: list[Target] = field(default_factory=list)
    #: (`skip …` line, a failure): left out by `--all` or `--spares`; a failure when the roster
    #: is wrong (two rows with one `ip`)
    left: list[tuple[str, bool]] = field(default_factory=list)


def region_of(row: dict[str, str]) -> str:
    return (row.get("region") or "").strip() or UNRECORDED_REGION


def load_spares(path: pathlib.Path, *, given: bool = False) -> dict[str, Any]:
    """`spares.json` (launch.py), or {} when the file doesn't exist yet. With `given` (a file
    named with `--spares-file` while spares are selected) it must exist: a typo would quietly
    leave every spare running. A selection of rows needs no file: `launch.py` writes it only
    when it launches spares, so a workshop without any has none, and a missing one reads as
    empty (#333). An existing file is still read, and a corrupt one still refuses the run."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if given:
            raise SelectionError(f"--spares-file {path} not found") from None
        return {}
    except ValueError as exc:
        raise SelectionError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SelectionError(f"{path} is not a JSON object")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def select(
    rows: list[dict[str, str]],
    spares: dict[str, Any],
    *,
    only: str | Sequence[str] | None = None,
    every_row: bool = False,
    every_spare: bool = False,
    region: str,
    scope: Scope,
) -> Selection:
    """The targets of `--only`, or of `--all` and/or `--spares`, with a `skip` line for each row
    or spare those leave out. Raises SelectionError, listing every refused name, for a bad
    `--only`."""
    prefix = f"{scope_label(scope)}/{region}/"
    ours = {e.get("name") or k[len(prefix) :]: e for k, e in spares.items() if k.startswith(prefix)}
    by_slug = {slug_of(r): r for r in participants(rows)}
    used = {r["ip"]: slug for slug, r in by_slug.items() if r.get("ip")}

    def row_problem(name: str, row: dict[str, str]) -> tuple[str, bool]:
        """(why the row can't be served, the roster is wrong)"""
        if region_of(row) != region:
            return f"its VM is in {region_of(row)}, not {region}", False
        if not row.get("ip"):
            return "no ip in the roster (no VM yet)", False
        twins = [s for s, r in by_slug.items() if s != name and r.get("ip") == row["ip"]]
        if twins:
            return f"its ip {row['ip']} is also {', '.join(twins)}'s: fix the roster", True
        return "", False

    def spare_problem(entry: dict[str, Any]) -> str:
        if entry.get("adopted_by"):
            return f"adopted by {entry['adopted_by']}: name that participant"
        if not entry.get("ip"):
            return "no address in spares.json: rerun launch.py"
        owner = used.get(entry["ip"])
        return f"its address is {owner}'s now (handed over): name {owner}" if owner else ""

    out = Selection()
    refused: list[str] = []
    names = only_slugs(only)
    every = list(by_slug) if every_row else []
    every += sorted(ours, key=_spare_number) if every_spare else []
    for name in names or every:
        row, entry, broken = by_slug.get(name), ours.get(name), False
        if row is not None:
            why, broken = row_problem(name, row)
            target = Target(name, row.get("ip", ""), host=row.get("host", ""))
        elif entry is not None and is_spare_name(name):
            why = spare_problem(entry)
            target = Target(name, str(entry.get("ip", "")), spare=True)
        else:
            why = f"no such roster row, nor a spare {prefix}{name} in spares.json"
        if why and names:
            refused.append(f"{name}: {why}")
        elif why:
            out.left.append((f"skip {name}: {why}", broken))
        else:
            out.targets.append(target)
    if refused:
        raise SelectionError("; ".join(refused))
    return out


def _spare_number(name: str) -> tuple[int, str]:
    tail = name.rsplit("-", 1)[-1]
    return (int(tail), name) if tail.isdigit() else (1 << 30, name)


def owner_problem(target: Target, inst: dict[str, Any]) -> str:
    """Why the instance at the target's address is not the target's VM, or "".

    A spare's must carry its own tag. A row's may carry the row's slug, the tag recorded in its
    `host` (a VM tagged before slugs), or a spare's (a hand-over by hand, as `status.py` allows);
    another participant's tag means the roster's `ip` is wrong."""
    tag = tags_of(inst).get("participant", "")
    if tag == target.name:
        return ""
    if not target.spare and (is_spare_name(tag) or target.host == f"ws-{tag}"):
        return ""
    return f"the VM at {target.ip} is tagged participant={tag}, not {target.name}"


@dataclass
class Served:
    """A VM that `--wait` waits for: started or stopped by this run (`requested`), or already
    on its way (`unchanged`)."""

    name: str
    iid: str
    ip: str
    requested: bool
    state: str = ""  # as the last lookup read it


def answering(ips: Sequence[str]) -> set[str]:
    """The addresses whose port 22 accepts a connection at one try (`ssh_answers`, looked up at
    call time), `SSH_PARALLEL` at a time: 5 s per 128 silent addresses, not 5 s per address."""
    if not ips:
        return set()
    with ThreadPoolExecutor(max_workers=min(len(ips), SSH_PARALLEL)) as pool:
        ok = list(pool.map(ssh_answers, ips))
    return {ip for ip, yes in zip(ips, ok, strict=True) if yes}


def settle(
    ec2: Any,
    scope: Scope,
    vms: list[Served],
    *,
    start: bool,
    log: Callable[[str], None],
    probe: Callable[[Sequence[str]], set[str]],
) -> list[Served]:
    """Wait for all of `vms` at once until each is stopped, or running with its port 22
    answering; return those that fail. One deadline, `WAIT_S` from now, for all of them (#334):
    each round reads every VM with one EC2 lookup, then tries every running one's port 22 with
    one `probe`, and the next starts `POLL_S` after. The rounds stop at the deadline, so this
    takes at most `WAIT_S` plus one round (a lookup that asks for no page past the deadline,
    each request bounded by `EC2_CONFIG`, and 5 s of tries per `SSH_PARALLEL` silent VMs),
    however many VMs fail. Every VM is read at least once (unless the first lookup fails or
    outlasts the deadline) and tried whenever it runs. One
    moving the other way, or gone, fails at once; one still where it was (`stopped` after a
    start: EC2's reads lag its writes) is read again until the deadline."""
    want = "running" if start else "stopped"
    deadline = time.monotonic() + WAIT_S
    left = {v.iid: v for v in vms}
    failed: list[Served] = []

    def fail(v: Served, why: str) -> None:
        failed.append(left.pop(v.iid))
        log(f"{v.name} {v.iid}: FAILED waiting: {why}")

    while left:
        try:
            found = ec2.find_instances(
                scope_tags(scope), LIVE_STATES, deadline=deadline, clock=time.monotonic
            )
        except Exception as exc:  # botocore (an expired credential), its pages past the deadline
            for v in list(left.values()):
                fail(v, f"the EC2 lookup: {error_line(exc)}")
            break
        states = {i["InstanceId"]: i["State"]["Name"] for i in found}
        for v in list(left.values()):
            v.state = states.get(v.iid, "")
            if not v.state:
                fail(v, f"it is gone: no live instance with {scope_text(scope)} has its id")
            elif v.state not in DONE[start] and v.state != FROM[start]:
                fail(v, f"it is {v.state}, on its way to {FROM[start]}")
            elif v.state == want and not start:
                log(f"{v.name} {v.iid}: stopped")
                del left[v.iid]
        up = [v for v in left.values() if start and v.state == want]
        ok = probe([v.ip for v in up]) if up else set()
        for v in up:
            if v.ip in ok:
                log(f"{v.name} {v.iid}: running, SSH answers")
                del left[v.iid]
        now = time.monotonic()
        if not left or now >= deadline:
            break
        time.sleep(min(POLL_S, deadline - now))
    after = f"after {WAIT_S / 60:g} minutes"
    for v in list(left.values()):
        if v.state == want:  # running, and tried in the last round
            fail(v, f"running, but SSH at {v.ip} does not answer {after}")
        else:
            fail(v, f"{v.state}, not {want}, {after}")
    return failed


def power(
    ec2: Any,
    targets: list[Target],
    scope: Scope,
    *,
    start: bool,
    dry_run: bool = False,
    wait: bool = False,
    failed: int = 0,
    log: Callable[[str], None] = print,
    probe: Callable[[Sequence[str]], set[str]] | None = None,
) -> int:
    """Start or stop each target's VM; one line per target, then a summary that also counts the
    `failed` found in the selection. 1 on any failure.

    `wait`: then wait until each VM served is running and its port 22 answers (`probe`,
    `answering` by default, looked up at call time), or stopped (`settle`); the month-end's
    sync needs a kept VM reachable (docs/ADMIN.md §11.5), started by this run or already on its
    way. A VM that fails the wait is counted as failed instead of `start`/`stop`/`unchanged`,
    so the summary counts each VM once."""
    verb = "start" if start else "stop"
    try:
        found = ec2.find_instances(scope_tags(scope), LIVE_STATES)
    except Exception as exc:  # botocore: an expired credential, UnauthorizedOperation
        log(f"FAILED: the EC2 lookup: {error_line(exc)}")
        return 1
    # never anything but a participant's VM or a spare (docs/ADMIN.md §3.2)
    by_ip = {ip: i for i in found if has_participant(i) for ip in instance_ips(i)}
    taken: dict[str, str] = {}  # instance id -> the target that took it
    served: list[Served] = []  # what `wait` waits for
    changed = unchanged = 0
    for t in targets:
        inst = by_ip.get(t.ip)
        if inst is None:
            failed += 1
            why = f"no live instance with {scope_text(scope)} and a participant tag has it"
            log(f"{t.name} {t.ip}: FAILED: {why}")
            continue
        iid, state = inst["InstanceId"], inst["State"]["Name"]
        why = owner_problem(t, inst)
        if not why and iid in taken:
            log(f"{t.name} {iid}: the same VM as {taken[iid]}, above")
            continue
        if not why and state not in DONE[start] and state != FROM[start]:
            why = f"it is {state}: rerun once it is {FROM[start]}"
        if why:
            failed += 1
            log(f"{t.name} {iid} ({state}): FAILED: {why}")
            continue
        taken[iid] = t.name
        if state in DONE[start]:
            unchanged += 1
            served.append(Served(t.name, iid, t.ip, requested=False))
            log(f"{t.name} {iid} ({state}): unchanged")
            continue
        if dry_run:
            changed += 1
            log(f"{t.name} {iid} ({state}): would {verb}")
            continue
        try:
            (ec2.start if start else ec2.stop)([iid])
        except Exception as exc:  # botocore errors: report this VM and go on
            failed += 1
            log(f"{t.name} {iid} ({state}): FAILED: {error_line(exc)}")
            continue
        changed += 1
        served.append(Served(t.name, iid, t.ip, requested=True))
        log(f"{t.name} {iid} ({state}): {verb} requested")
    if wait and not dry_run:
        for v in settle(ec2, scope, served, start=start, log=log, probe=probe or answering):
            failed += 1
            if v.requested:
                changed -= 1
            else:
                unchanged -= 1
    would = "would " if dry_run else ""
    log(f"{would}{verb} {changed}, unchanged {unchanged}, failed {failed}")
    if dry_run:
        log("dry run: nothing changed")
    return 1 if failed else 0


# ------------------------------------------------------------ a stopped VM for its row (#995)

#: `StartStop`'s waits, as `adopt_spare.py`'s: from a start request until the VM runs, its port 22
#: answers and it lets a login in, and for a VM `stopping` (idle stop powering it off) until it
#: is stopped. After a start whose answer was lost, how long EC2 is read for a start to show.
START_WAIT_S, LOST_START_S = 300.0, 30.0
#: A row's cell in the script's table: a VM left as it was, and one started and stopped again.
RUNNING, STOPPED_AGAIN = "running", "started, stopped again"
#: The start of the cell of a VM this run started whose stop failed: `!stop <slug>` follows.
NOT_STOPPED_AGAIN = "started, NOT stopped again"
#: The start of the cell of a VM up after a start that failed, never this run's (`_unclaimed`).
START_FAILED = "start failed"
#: The start of a reason why a VM's use couldn't be read, `vm_in_use`'s and `_after`'s, beside
#: `in_use`'s own (`idle_stop.UNREAD`, which `StartStop` is given): never a word matched anywhere,
#: which a login's name could hold (review round 2 of #1026, N1).
UNREAD_PREFIX = "its use can't be read: "
#: After this many VMs in a row that the run started and whose use it couldn't read, it starts
#: no more: a check broken on every VM would otherwise start the whole fleet and leave it up.
UNREAD_LIMIT = 3
#: Errors that EC2 may answer with a server status (HTTP 5xx) though the request did nothing: no
#: capacity for the VM, or the request throttled (`answered`).
REFUSALS = ("RequestLimitExceeded", "EC2ThrottledException")  # botocore's two for EC2, at 503
R = TypeVar("R")


class StartStop:
    """A stopped VM started for its row's work, and stopped again as soon as that is done (#995;
    docs/ADMIN.md §3, action 4): `push_config.py --start-stopped N`, and `trust_fix.py`'s.

    - **Which VMs.** One lookup when it is made maps each row's `ip` to its VM, as `power` does:
      every scope tag, a participant tag, the row's own (`owner_problem`). A row without one runs
      its work as before, with no start or stop; so does every row when that lookup fails
      (`off`, which the script prints).
    - **At its row** (`around`), EC2's state is read again: `running` or `pending`, the work runs
      and the VM is left running. `stopping` is waited for (`START_WAIT_S`). `stopped` takes one
      of `at_once` slots and is started. EC2's `PreviousState` says whose start it was: anything
      but `stopped`, or none, is someone else's (the waker's), and the VM is left running.
      **A start that raises is never this run's** (`_unclaimed`): someone else's may have come
      first. A VM EC2 then shows up is left running, and said so; one still stopped is refused,
      and, unless EC2 answered the error (so this start didn't act), said to be one that a start
      may still reach (`LOST_START_S` of reads first). This run's start is waited for until
      the VM runs, its port 22 answers and, with `ready`, it lets a login in (sshd answers
      before a boot allows logins), EC2's lookups retried until `START_WAIT_S`. Then the work
      runs, and then, however the work ended, `in_use(row)` gives the reasons it was used since
      its start, read on the VM (`idle_stop.py --in-use`): with none it is stopped at once
      (`StopInstances`), with any it is left running.
    - **The bound.** The slot is held from the start to that decision: at most `at_once` VMs
      that this run started are being started, worked on or checked at once. A VM left running
      gives its slot back and stays up; one stopped is still stopping for a while.
    - **A check that can't be read** (`unread_reasons`, `idle_stop.UNREAD`, or `UNREAD_PREFIX`)
      leaves its VM running, as unknown use does in idle stop. After `UNREAD_LIMIT` such VMs in
      a row the run starts no more (`halted`, and `tripped`): their rows are refused, and the
      line under the table says why. `halted_in`, the workshop whose run of the same plan
      halted before this one (#1031): this run starts no VM from the first.
    - **Failures.** A VM that can't be started, or is still stopping, isn't worked on: its row
      gets `refused(why)`. One started that doesn't come up in time is stopped again, and
      refused. A stop that fails is said in the row's cell, with the command that stops it.
    - A line as each VM is started and stopped (`log`), each row's outcome as its cell of the
      script's table (`cells`), and `summary`, under the table. Thread-safe: the script's
      workers call `around` at once."""

    def __init__(
        self,
        ec2: Any,
        scope: Scope,
        rows: Sequence[dict[str, str]],
        *,
        at_once: int,
        in_use: Callable[[dict[str, str]], Sequence[str]],
        unread_reasons: Collection[str],
        halted_in: str = "",
        log: Callable[[str], None] = print,
        answers: Callable[[str], bool] = ssh_answers,
        ready: Callable[[dict[str, str]], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if at_once < 1:
            raise ValueError("at least one VM at a time")
        self.ec2, self.scope, self.in_use, self.log = ec2, scope, in_use, log
        self.answers, self.ready, self.sleep, self.clock = answers, ready, sleep, clock
        self.slots = threading.BoundedSemaphore(at_once)
        self.cells: dict[str, str] = {}
        self.unread_reasons = frozenset(unread_reasons)
        self.off, self.halted = "", halted(halted_in) if halted_in else ""
        self.tripped = False  # this run's own breaker halted it (`admin_common.HALTED_EXIT`)
        self.unread = 0  # started VMs in a row whose use couldn't be read
        self.vms: dict[str, tuple[str, str]] = {}  # slug → (instance id, address)
        self._lock = threading.Lock()
        try:
            found = ec2.find_instances(scope_tags(scope), LIVE_STATES)
        except Exception as exc:  # botocore: an expired credential, UnauthorizedOperation
            self.off = f"the EC2 lookup failed: {error_line(exc)}"
            return
        by_ip = {ip: i for i in found if has_participant(i) for ip in instance_ips(i)}
        for row in rows:
            slug, ip = slug_of(row), (row.get("ip") or "").strip()
            inst = by_ip.get(ip) if ip else None
            target = Target(slug, ip, host=row.get("host", ""))
            if inst is not None and not owner_problem(target, inst):
                self.vms[slug] = (inst["InstanceId"], ip)

    def around(
        self, row: dict[str, str], work: Callable[[], R], refused: Callable[[str], R],
        keep: Callable[[R], str] | None = None,
    ) -> R:  # fmt: skip
        """The row's `work`, its VM started first and stopped after if it is stopped (above);
        `keep`, why the work's result leaves the VM running whatever its use (a repair that may
        still run there, `trust_fix.py`'s), or ""."""
        slug = slug_of(row)
        if slug not in self.vms:
            return work()
        iid, ip = self.vms[slug]
        try:
            state = self._settled(iid)
        except Exception as exc:  # unknown: as before, the work alone
            self._cell(slug, f"? (EC2: {error_line(exc)})")
            return work()
        if state == "stopped":
            with self.slots:
                if self.halted:  # said once, when it halted
                    return refused(self._cell(slug, f"not started: {self.halted}"))
                state, why = self._start(row, slug, iid, ip)
                if why:
                    return refused(why)
                if state == "started":
                    done: list[R] = []
                    try:
                        done.append(work())
                    finally:
                        held = keep(done[0]) if keep is not None and done else ""
                        self._after(row, slug, iid, held)
                    return done[0]
        if state == START_FAILED:  # up after a start that failed: never stopped, its cell set
            self._up(row, iid, ip, self.clock() + START_WAIT_S)
            return work()
        if state not in DONE[True]:
            after = f" after {START_WAIT_S / 60:g} minutes" * (state == "stopping")
            return refused(self._cell(slug, f"not started: it is {state or 'gone'}{after}"))
        if state == "pending":  # someone else's start, the waker's say: up first, left running
            self._up(row, iid, ip, self.clock() + START_WAIT_S)
        self._cell(slug, RUNNING)
        return work()

    def summary(self) -> str:
        """One line on the VMs started, or "" when none was."""
        with self._lock:
            cells = dict(self.cells)

        def named(test: Callable[[str], bool]) -> list[str]:
            return [slug for slug, cell in cells.items() if test(cell)]

        started = named(lambda c: c.startswith("started"))
        failed = named(lambda c: c.startswith(("not started", "started, but")))
        unclaimed = named(lambda c: c.startswith(START_FAILED))
        if not started and not failed and not unclaimed:
            return ""
        again = named(lambda c: c == STOPPED_AGAIN or c.endswith(": stopped again"))
        out = f"stopped VMs: {len(started)} started, {len(again)} stopped again"
        for label, names in (
            ("left running", named(lambda c: c.startswith("started, left running"))),
            ("NOT stopped again", named(lambda c: "NOT stopped again" in c)),
            ("failed", failed),
            ("up after a failed start, left running", unclaimed),
        ):
            out += f"; {label}: {', '.join(names)}" * bool(names)
        return out + f"; {self.halted}" * bool(self.halted)

    def _cell(self, slug: str, cell: str) -> str:
        with self._lock:
            self.cells[slug] = cell
        return cell

    def _say(self, line: str) -> None:
        with self._lock:
            self.log(f"power: {line}")

    def _state(self, iid: str) -> str:
        found = self.ec2.find_instances(scope_tags(self.scope), LIVE_STATES)
        return next((str(i["State"]["Name"]) for i in found if i.get("InstanceId") == iid), "")

    def _settled(self, iid: str) -> str:
        """Its state, once it isn't `stopping`, or after `START_WAIT_S`."""
        deadline = self.clock() + START_WAIT_S
        state = self._state(iid)
        while state == "stopping" and self.clock() < deadline:
            self.sleep(POLL_S)
            state = self._state(iid)
        return state

    def _shown(self, iid: str) -> str:
        """After a start whose answer was lost: `pending` or `running` once EC2 shows it so,
        within `LOST_START_S`; else its last state read, "" when none could be."""
        deadline, state = self.clock() + LOST_START_S, ""
        while True:
            try:
                state = self._state(iid)
            except Exception:  # read again, up to the deadline
                state = state or ""
            if state in DONE[True] or self.clock() >= deadline:
                return state
            self.sleep(POLL_S)

    def _start(self, row: dict[str, str], slug: str, iid: str, ip: str) -> tuple[str, str]:
        """(`started` once this run started it and it is up, `pending` for someone else's
        start, `START_FAILED` for a VM up after this run's start failed; why its row is
        refused, or "")."""
        deadline = self.clock() + START_WAIT_S
        try:
            answer = self.ec2.start([iid]) or []
            before = next(
                (str((s.get("PreviousState") or {}).get("Name") or "") for s in answer
                 if isinstance(s, dict) and s.get("InstanceId") == iid),
                "",
            )  # fmt: skip
        except Exception as exc:  # refused, or its answer lost: never this run's (`_unclaimed`)
            return self._unclaimed(slug, iid, exc)
        if before != "stopped":  # someone else's start, or one EC2 didn't say: left running
            said = f"{before} already, started by someone else" if before else (
                "EC2 didn't say it was stopped before this start")  # fmt: skip
            self._say(f"{slug} {iid}: {said}: left running")
            return "pending", ""
        self._say(f"{slug} {iid}: started for its row")
        why = self._up(row, iid, ip, deadline)
        if not why:
            return "started", ""
        stopped = self._stop(iid)
        again = "stopped again" if not stopped else f"NOT stopped again ({stopped}): !stop {slug}"
        self._say(f"{slug} {iid}: {why}: {again}")
        return "", self._cell(slug, f"started, but {why}: {again}")

    def _unclaimed(self, slug: str, iid: str, exc: Exception) -> tuple[str, str]:
        """After `StartInstances` raised: never this run's start (review round 2 of #1026; Codex's
        P2 there), since someone else's, the waker's, may have come between this run's read and
        its request. An error EC2 answered (`answered`) means this start didn't act: one read
        says whether the VM is up all the same. Any other error (a timeout, a lost connection,
        a server error) may hide a start that acted, or will: EC2 is read for `LOST_START_S`. A
        VM up is left running, never stopped, and said so; one still stopped is refused, with
        whether a start may still reach it."""
        state = self._read(iid) if answered(exc) else self._shown(iid)
        if state in DONE[True]:
            cell = f"{START_FAILED} ({error_line(exc)}), VM up: left running"
            self._say(f"{slug} {iid}: {cell}")
            self._cell(slug, cell)
            return START_FAILED, ""
        if answered(exc):
            return "", self._cell(slug, f"not started: {error_line(exc)}")
        unread = "its state can't be read" if not state else "a start may still reach it"
        return "", self._cell(slug, f"not started: {error_line(exc)}; {unread}: look at it")

    def _read(self, iid: str) -> str:
        """One read of its state, "" when it fails."""
        try:
            return self._state(iid)
        except Exception:
            return ""

    def _up(self, row: dict[str, str], iid: str, ip: str, deadline: float) -> str:
        """ "" once it runs, its port 22 answers and it lets a login in (`ready`); why not, at
        the deadline or before. A failed EC2 lookup is read again until the deadline."""
        state, failed, reached = "stopped", "", False
        while True:
            try:
                state, failed = self._state(iid), ""
            except Exception as exc:  # throttled, say
                failed = f"the EC2 lookup failed: {error_line(exc)}"
            if not failed:
                if state == "running" and self.answers(ip):
                    reached = True
                    if self.ready is None or self._ready(row):
                        return ""
                if state not in ("pending", "running", "stopped"):  # EC2's reads lag its writes
                    return f"it is {state or 'gone'} now"
            if self.clock() >= deadline:
                break
            self.sleep(POLL_S)
        after = f"after {START_WAIT_S / 60:g} minutes"
        if failed:
            return f"{failed}, still {after}"
        if state == "running":
            return f"its SSH ({ip}:22) " + (
                f"lets no login in {after}" if reached else f"doesn't answer {after}")  # fmt: skip
        return f"it is still {state} {after}"

    def _ready(self, row: dict[str, str]) -> bool:
        try:
            return bool(self.ready(row)) if self.ready is not None else True
        except Exception:
            return False

    def _stop(self, iid: str) -> str:
        """ "" once EC2 has the request; its error otherwise."""
        try:
            self.ec2.stop([iid])
        except Exception as exc:
            return error_line(exc)
        return ""

    def _after(self, row: dict[str, str], slug: str, iid: str, held: str = "") -> None:
        """A VM this run started, its work done: stopped, unless its work `held` it, it was used
        since its start, or its use couldn't be read (`UNREAD`, `UNREAD_PREFIX`; counted towards
        `UNREAD_LIMIT`)."""
        try:
            reasons = [held] if held else [str(r) for r in self.in_use(row)]
        except Exception as exc:  # unknown: left running
            reasons = [f"{UNREAD_PREFIX}{type(exc).__name__}"]
        if not held:  # a work that holds its VM is no read of its use
            unread = self.unread_reasons
            self._count(any(r in unread or r.startswith(UNREAD_PREFIX) for r in reasons))
        why = "; ".join(reasons)
        if why:
            self._say(f"{slug} {iid}: left running: {why}")
            self._cell(slug, f"started, left running: {why}")
            return
        stopped = self._stop(iid)
        if stopped:
            self._say(f"{slug} {iid}: NOT stopped again: {stopped}")
            self._cell(slug, f"{NOT_STOPPED_AGAIN} ({stopped}): !stop {slug}")
            return
        self._say(f"{slug} {iid}: stopped again")
        self._cell(slug, STOPPED_AGAIN)

    def _count(self, unread: bool) -> None:
        """One more started VM whose use couldn't be read, or none: at `UNREAD_LIMIT` in a row,
        the run starts no more."""
        with self._lock:
            self.unread = self.unread + 1 if unread else 0
            halt = self.unread >= UNREAD_LIMIT and not self.halted
            if halt:
                self.halted, self.tripped = halted(), True
        if halt:
            self._say(self.halted)


def halted(where: str = "") -> str:
    """Why `StartStop` starts no more VMs: its own breaker, or that of the run of the same plan
    in workshop `where` (#1031)."""
    return (f"the use of {UNREAD_LIMIT} started VMs in a row couldn't be read"
            + f" in {where}" * bool(where) + ", so no more are started")  # fmt: skip


def answered(exc: BaseException) -> bool:
    """Whether EC2 answered a request with `exc`, so that the request did nothing
    (`StartStop._unclaimed`): botocore's `ClientError` carries EC2's `response`; a timeout or a
    lost connection doesn't. A server error (HTTP 5xx: `InternalError`, `ServiceUnavailable`) is
    no proof that EC2 didn't act (#1031; review of #1026, N1), unless it says why it couldn't:
    no capacity (`Insufficient…Capacity`) or the request throttled (`REFUSALS`). An answer
    without its HTTP status, which botocore always gives, is taken as answered, as before."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    meta, error = response.get("ResponseMetadata"), response.get("Error")
    status = meta.get("HTTPStatusCode") if isinstance(meta, dict) else None
    if type(status) is not int or status < 500:
        return True
    code = str(error.get("Code") or "") if isinstance(error, dict) else ""
    return code in REFUSALS or (code.startswith("Insufficient") and code.endswith("Capacity"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start or stop workshop VMs and spares.")
    parser.add_argument("--roster", required=True)
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--start", action="store_true")
    what.add_argument("--stop", action="store_true")
    parser.add_argument("--all", action="store_true", help="every roster row; never the spares")
    parser.add_argument("--spares", action="store_true", help="every spare not adopted")
    parser.add_argument(
        "--only", action="append", default=[], metavar="SLUG", help="a participant or a spare"
    )
    parser.add_argument("--region", required=True, help="the workshop's region")
    parser.add_argument("--spares-file", metavar="FILE", help=f"default {SPARES_FILE}")
    add_tag_argument(parser)
    parser.add_argument("--wait", action="store_true", help="until stopped, or SSH answers")
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv)
    if opts.only and (opts.all or opts.spares):
        parser.error("--only can't be combined with --all or --spares")
    if not (opts.only or opts.all or opts.spares):
        parser.error("say what to act on: --all, --spares (or both), or --only SLUG …")
    try:
        scope = parse_tags(opts.tag)
        rows = read_roster(opts.roster)
        region = opts.region
        path = pathlib.Path(opts.spares_file) if opts.spares_file else SPARES_FILE
        wants_spares = opts.spares or any(is_spare_name(name) for name in only_slugs(opts.only))
        spares = load_spares(path, given=bool(opts.spares_file) and wants_spares)
        chosen = select(
            rows,
            spares,
            only=opts.only,
            every_row=opts.all,
            every_spare=opts.spares,
            region=region,
            scope=scope,
        )
    except (OSError, ValueError, SelectionError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    print(f"scope: {scope_text(scope)}; region {region}")
    for line, _ in chosen.left:
        print(line)
    broken = sum(1 for _, bad in chosen.left if bad)
    if not chosen.targets:
        print("nothing to do")
        return 1 if broken else 0
    try:
        ec2 = Ec2(region, config=EC2_CONFIG)
    except ImportError:
        print("refusing: power.py needs boto3 (uv run --with boto3 python …)", file=sys.stderr)
        return 1
    return power(
        ec2,
        chosen.targets,
        scope,
        start=opts.start,
        dry_run=opts.dry_run,
        wait=opts.wait,
        failed=broken,
    )


if __name__ == "__main__":
    raise SystemExit(main())
