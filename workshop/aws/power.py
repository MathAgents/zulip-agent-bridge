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
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

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
