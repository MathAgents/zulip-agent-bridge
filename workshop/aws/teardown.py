"""Terminate the workshop VMs and release their Elastic IPs (README.md, "teardown.py").

    uv run --with boto3 python workshop/aws/teardown.py --tag workshop=nt-agents-2026 \\
        [--tag KEY=VALUE …] [--region us-west-2] [--only SLUG …] [--dry-run] [--yes] [--strict] \\
        [--roster roster.csv]

Standalone script (boto3 is not a zab dependency), run by Drew for a workshop; agents run it only
on test infrastructure, with `--tag zab-role=test` among the scope tags, so
nothing else matches. Lists what it would terminate and asks for confirmation unless `--yes`.
Root volumes are deleted with the instances (`DeleteOnTermination`, README.md, "The VMs"). An
instance with termination protection (a kept VM, `protect.py`; issue #207) is left alone, and so is
the Elastic IP associated with it; the list says so, and `protect.py --off --only SLUG` lifts the
protection first if that VM is really to go.

Only a participant's VM or a spare is ever selected: an instance or Elastic IP without a
`participant` tag is listed as `skip` and left alone, even under the scope tags alone (the admin
server, tagged `workshop=…` by mistake; docs/ADMIN.md §3.2). So is an Elastic IP associated with
anything this run does not terminate (another instance, or a network interface alone): in a
default VPC, `ReleaseAddress` would silently disassociate it. Instances that carry the scope
tags without a `participant` tag, such as a builder or smoke instance that `build_ami.py --keep`
left running, are therefore removed by hand.

**EC2 lags** (#378). EC2 enforced a lifted termination protection minutes late on Oct 15: a
`TerminateInstances` sent right after `protect.py --off` was refused (`OperationNotPermitted`).
One such VM refuses the whole call, so the VMs are then terminated one at a time, and each VM
refused as protected is tried again, `RETRY_FIRST` s later and twice as long each time after (at
most `RETRY_MAX`), until `RETRY_SECONDS` have passed. A VM still refused is reported on its own
line (`refused …`) with its Elastic IP kept, the others are torn down, and the exit code is 1.
Any other refusal, or a failed wait or release, is reported the same way and never stops the
rest. The opposite case, a VM protected moments ago that EC2 still reads, and lets terminate, as
unprotected, is what the admin bot's 5-minute wait after a `!protect` is for (docs/ADMIN.md §3,
action 7); by hand, wait until `protect.py` says EC2 shows it.

**`--strict`** (the admin bot's `!retire`, #559): every VM selected must go. One that EC2 still
reads as protected, which would be kept, is reported `refused …` instead, with its Elastic IP
kept, and the exit code is 1: `!retire` lifts the protection with `protect.py --off`, whose
read-back waits until EC2 shows it, and a VM still protected after that must not count as torn
down. The others are torn down as without it.

**`--roster`** (the admin bot's `!teardown` and `!retire`, #604): the `ip` of every roster row
at an address this run released is cleared, and its `host` kept. EC2 can give a released
Elastic IP to the next address allocated, in this workshop too: a row that kept it would share
it with that VM's row, which every admin script then refuses, and would reach that VM as its
own. The `host` (`ws-<slug>`) stays, so that `launch.py` still leaves the row alone until a
deployer clears it too. A roster that can't be read or written is reported, and the exit code
is 1.

**`--spares-file`** (the admin bot's `!teardown` and `!retire`, #607): every entry of the scope
and region in `spares.json` at an address this run released goes, written atomically: a spare
torn down, or an adopted one's entry once the row's VM is. Kept, `fleet.read_spares` would go
on offering a torn-down spare, and a later spare given its recycled address could be taken for
it. A file that can't be read or written is reported, with the entries to remove by hand, and
the exit code is 1.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from aws_common import (
    Ec2,
    Scope,
    add_tag_argument,
    column_snapshot,
    error_code,
    error_line,
    has_participant,
    not_participant,
    only_slugs,
    parse_tags,
    participants,
    read_roster,
    save_roster_changes,
    scope_label,
    scope_tags,
    slug_of,
    tags_of,
    write_spares,
)

LIVE_STATES = ("pending", "running", "stopping", "stopped")
#: EC2's error for a VM whose termination protection it still enforces.
NOT_PERMITTED = "OperationNotPermitted"
#: How long a VM refused as protected is tried again (#378: enforced 157 s late on Oct 15).
RETRY_SECONDS = 300.0
#: The first wait between tries, doubled after each, up to the second.
RETRY_FIRST, RETRY_MAX = 5.0, 30.0


def plan(
    ec2: Any,
    tag: Scope,
    only: str | Sequence[str] | None = None,
    *,
    skipped: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """(instances, addresses, protected instances) that carry every scope tag and a
    `participant` tag (with `--only`, one of those slugs, each looked up by its own tag).

    Protected instances (termination protection on) and the addresses associated with them are
    kept out of the first two lists: `terminate_instances` refuses the whole batch for one of
    them, and their Elastic IPs must stay (issue #207). A resource without a `participant` tag
    is never selected (docs/ADMIN.md §3.2), and neither is an address associated with anything
    this run does not terminate: another instance, or a network interface alone. In a default
    VPC, releasing it would silently disassociate it. Both go to `skipped`, when given, to be
    listed.
    """
    scope = scope_tags(tag)
    lookups = [{**scope, "participant": slug} for slug in only_slugs(only)] or [scope]
    found = [inst for tags in lookups for inst in ec2.find_instances(tags, LIVE_STATES)]
    out = [inst for inst in found if not has_participant(inst)]
    found = [inst for inst in found if has_participant(inst)]
    protected = [inst for inst in found if ec2.protection(inst["InstanceId"])[0]]
    kept = {inst["InstanceId"] for inst in protected}
    instances = [inst for inst in found if inst["InstanceId"] not in kept]
    going = {inst["InstanceId"] for inst in instances}
    addresses = []
    for addr in (a for tags in lookups for a in ec2.find_addresses(tags)):
        attached = addr.get("InstanceId")
        if attached in kept:
            continue  # stays with its protected VM, which is listed as `keep`
        # associated with a network interface alone, an address has no `InstanceId` but is not
        # free: in a default VPC, `ReleaseAddress` would silently disassociate it
        free = not (attached or addr.get("AssociationId") or addr.get("NetworkInterfaceId"))
        if has_participant(addr) and (free or attached in going):
            addresses.append(addr)
        else:
            out.append(addr)
    if skipped is not None:
        skipped.extend(out)
    return instances, addresses, protected


def describe(
    instances: list[dict],
    addresses: list[dict],
    protected: Sequence[dict] = (),
    skipped: Sequence[dict] = (),
) -> str:
    lines = []
    for inst in protected:
        t = tags_of(inst)
        lines.append(
            f"keep {inst['InstanceId']} {t.get('Name', '')}: termination protection is on "
            f"(protect.py --off --only {t.get('participant', '?')} first, if it is to go)"
        )
    for res in skipped:
        if "AllocationId" not in res:
            why = f"{not_participant(res)} (not a participant's VM or a spare)"
            lines.append(f"skip {res['InstanceId']} {tags_of(res).get('Name', '')}: {why}")
            continue
        where = res.get("InstanceId") or res.get("NetworkInterfaceId") or res.get("AssociationId")
        why = not_participant(res) or f"attached to {where}, which this run does not terminate"
        lines.append(f"skip {res['AllocationId']} {res.get('PublicIp', '')}: {why}")
    for inst in instances:
        t = tags_of(inst)
        lines.append(
            f"terminate {inst['InstanceId']} {t.get('Name', '')} ({inst['State']['Name']}, "
            f"participant {t.get('participant', '?')})"
        )
    for addr in addresses:
        lines.append(f"release {addr['AllocationId']} {addr.get('PublicIp', '')}")
    return "\n".join(lines) if lines else "nothing matches"


def terminate(
    ec2: Any,
    ids: list[str],
    *,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict[str, tuple[str, str]]:
    """Terminate `ids`, and return those EC2 never took: instance → (error code, error line).
    One call for all; when EC2 refuses it, one call per VM, and a VM refused as protected is
    tried again until `RETRY_SECONDS` have passed (module doc)."""
    try:
        ec2.terminate(ids)
        return {}
    except Exception as exc:  # one protected VM refuses the whole call
        log(f"EC2 refused the call ({error_line(exc)}): one VM at a time")
    refused: dict[str, tuple[str, str]] = {}
    pending, pause, deadline = list(ids), RETRY_FIRST, clock() + RETRY_SECONDS
    while True:
        again = []
        for iid in pending:
            try:
                ec2.terminate([iid])
            except Exception as exc:  # botocore: this VM, not the others
                refused[iid] = (error_code(exc), error_line(exc))
                if refused[iid][0] == NOT_PERMITTED:
                    again.append(iid)
                continue
            refused.pop(iid, None)
        left = deadline - clock()
        if not again or left <= 0:
            return refused
        pause = min(pause, left)
        log(f"EC2 still refuses {', '.join(again)} as protected: trying again in {pause:.0f} s")
        sleep(pause)
        pending, pause = again, min(pause * 2, RETRY_MAX)


def teardown(
    ec2: Any,
    tag: Scope,
    *,
    only: str | Sequence[str] | None = None,
    dry_run: bool = False,
    yes: bool = False,
    strict: bool = False,
    confirm: Callable[[str], str] = input,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] | None = None,  # time.sleep, looked up at the call
    clock: Callable[[], float] | None = None,  # time.monotonic, likewise
    roster: str | None = None,
    spares_file: str | None = None,
    region: str = "us-west-2",
) -> int:
    skipped: list[dict] = []
    try:
        instances, addresses, protected = plan(ec2, tag, only, skipped=skipped)
    except Exception as exc:  # botocore: an expired credential, UnauthorizedOperation
        log(f"FAILED: the EC2 lookup: {error_line(exc)}")
        return 1
    log(describe(instances, addresses, protected, skipped))
    kept = bool(strict and protected and not dry_run)
    for inst in protected if kept else []:
        t = tags_of(inst)
        log(f"refused {inst['InstanceId']} {t.get('Name', '')} (participant "
            f"{t.get('participant', '?')}): EC2 still reads its termination protection as on "
            "(--strict: every VM selected must go)")  # fmt: skip
    if not instances and not addresses:
        return 1 if kept else 0
    if dry_run:
        log("dry run: nothing changed")
        return 0
    if not yes:
        question = (
            f"Terminate {len(instances)} instance(s) and release {len(addresses)} "
            "address(es)? [y/N] "
        )
        answer = confirm(question)
        if answer.strip().lower() not in ("y", "yes"):
            log("aborted")
            return 2
    ids = [inst["InstanceId"] for inst in instances]
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    refused = terminate(ec2, ids, log=log, sleep=sleep, clock=clock) if ids else {}
    for inst in instances:
        if inst["InstanceId"] not in refused:
            continue
        code, line = refused[inst["InstanceId"]]
        t = tags_of(inst)
        why = (
            f"EC2 still refuses it as protected after {RETRY_SECONDS:.0f} s: its termination "
            "protection is on, or was lifted moments ago; check, then run teardown.py again"
            if code == NOT_PERMITTED
            else f"EC2 refused it: {line}"
        )
        log(f"refused {inst['InstanceId']} {t.get('Name', '')} (participant "
            f"{t.get('participant', '?')}): {why}")  # fmt: skip
    going = [iid for iid in ids if iid not in refused]
    stays = set(refused)  # instances that may still be there: their addresses stay too
    if going:
        log(f"terminating {len(going)}; waiting")
        try:
            ec2.wait_terminated(going)
        except Exception as exc:  # botocore's WaiterError: they may still be shutting down
            log(f"FAILED: waiting for {', '.join(going)} to terminate: {error_line(exc)}")
            stays.update(going)
    failed = bool(stays)
    released: set[str] = set()
    for addr in addresses:
        if addr.get("InstanceId") in stays:
            log(f"keep {addr['AllocationId']} {addr.get('PublicIp', '')}: its instance "
                f"{addr['InstanceId']} is not terminated; run teardown.py again")  # fmt: skip
            continue
        try:
            ec2.release_address(addr["AllocationId"])
        except Exception as exc:  # botocore: this address, not the others
            log(f"FAILED: release {addr['AllocationId']}: {error_line(exc)}")
            failed = True
            continue
        log(f"released {addr['AllocationId']}")
        released.add(str(addr.get("PublicIp") or ""))
    if roster and not forget(roster, released - {""}, log):
        failed = True
    prefix = f"{scope_label(tag)}/{region}/"
    if spares_file and not forget_spares(spares_file, released - {""}, prefix, log):
        failed = True
    return 1 if failed or kept else 0


def forget_spares(path: str, released: set[str], prefix: str, log: Callable[[str], None]) -> bool:
    """Drop every entry of `spares.json` under `prefix` (the scope and region) at an address this
    run released (`--spares-file`, module doc). False when the file can't be read or written:
    the entries to remove by hand are named. A file that doesn't exist has nothing to drop."""
    if not released:
        return True
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return True
    except (OSError, ValueError) as exc:
        log(f"FAILED: {path} ({exc}): remove by hand its entries at {', '.join(sorted(released))}, "
            "released")  # fmt: skip
        return False
    gone = [
        key for key, entry in data.items()
        if key.startswith(prefix) and isinstance(entry, dict) and entry.get("ip") in released
    ]  # fmt: skip
    if not gone:
        return True
    for key in gone:
        log(f"spares.json: {key} removed (its address {data[key]['ip']} released)")
        del data[key]
    try:
        write_spares(path, data)
    except OSError as exc:
        log(f"FAILED: {path} not written ({exc}): remove by hand {', '.join(gone)}")
        return False
    return True


def forget(path: str, released: set[str], log: Callable[[str], None]) -> bool:
    """Clear the roster's `ip` of every row at an address this run released (`--roster`, module
    doc); its `host` stays. Only that column is written, into the roster as it is now. False when
    the roster can't be read or written: the addresses to clear by hand are named."""
    if not released:
        return True
    try:
        rows = read_roster(path)
    except (OSError, ValueError) as exc:
        log(f"FAILED: the roster ({exc}): clear by hand the ip of its rows at "
            f"{', '.join(sorted(released))}, released")  # fmt: skip
        return False
    before = column_snapshot(rows, ("ip",))
    expect = {}  # cleared only while the file still has it, as launch.py clears (Codex on #606)
    for row in participants(rows):
        if row.get("ip") in released:
            log(f"roster: {slug_of(row)}'s ip {row['ip']} cleared (released; its host stays)")
            expect[slug_of(row)] = {"ip": row["ip"]}
            row["ip"] = ""
    return save_roster_changes(path, rows, before, script="teardown.py", expect=expect)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Terminate workshop VMs and release their IPs.")
    add_tag_argument(parser)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SLUG",
        help="one participant or spare (the slug; repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument(
        "--strict", action="store_true", help="a VM selected but protected fails the run (exit 1)"
    )
    parser.add_argument(
        "--roster", help="clear the ip of the rows at the addresses this run releases (#604)"
    )
    parser.add_argument(
        "--spares-file",
        metavar="FILE",
        help="drop the spares.json entries at the addresses this run releases (#607)",
    )
    opts = parser.parse_args(argv)
    try:
        tag = parse_tags(opts.tag)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return teardown(
        Ec2(opts.region), tag, only=opts.only, dry_run=opts.dry_run, yes=opts.yes,
        strict=opts.strict, roster=opts.roster, spares_file=opts.spares_file, region=opts.region,
    )  # fmt: skip


if __name__ == "__main__":
    raise SystemExit(main())
