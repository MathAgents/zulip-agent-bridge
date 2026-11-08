"""Termination protection and stop-on-shutdown for kept VMs (issue #207; README.md, "protect.py").

    uv run --with boto3 python workshop/aws/protect.py --tag workshop=NAME [--tag …] \\
        --region REGION [--only SLUG …] [--off] [--dry-run] [--no-wait]

Standalone script (boto3 is not a zab dependency), run by the admin bot or a deployer for a
workshop; agents run it only on test infrastructure, resources tagged `zab-role=test`. For
every live instance with the workshop tag and a `participant` tag (or only the `--only`
participants) it makes sure that:

- **termination protection** is on (`DisableApiTermination`), so a stray `teardown.py`, or a
  click in the console, cannot delete a VM somebody kept; `--off` turns it off again, the step
  before tearing that VM down;
- an OS shutdown **stops** the instance (`InstanceInitiatedShutdownBehavior=stop`, whatever
  `--off` says): idle stop powers a kept VM off from inside, and that must never terminate it.

An instance without a `participant` tag is never touched (listed as `skip`): only a
participant's VM or a spare is ever selected (docs/ADMIN.md §3.2).

Idempotent: it reads both attributes first and changes only what differs, printing one line per
instance. `--dry-run` reads and prints, and changes nothing. A failure on one instance is
reported and the others still run; the exit code is then 1.

**EC2 lags** (#378). After a successful `ModifyInstanceAttribute`, EC2 went on reading, and
enforcing, the old termination protection for up to 157 s (Oct 15). So once it has set it, the
script reads it back every `POLL_SECONDS` until EC2 shows it on every VM it changed, for at most
`SETTLE_SECONDS` in all, and says per VM "set; EC2 shows it now" or "set; EC2 doesn't show it
yet (it can take a few minutes)". A read that lags is never a failure: the change was made.
`--no-wait` skips the reads, for scripts. Until EC2 shows it, `teardown.py` may read, and EC2
may enforce, the old protection: the admin bot refuses `!teardown` for 5 minutes after a
`!protect` (docs/ADMIN.md §3, action 7).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from aws_common import (
    Ec2,
    Scope,
    add_tag_argument,
    error_line,
    has_participant,
    not_participant,
    only_slugs,
    parse_tags,
    scope_tags,
    scope_text,
    tags_of,
)

LIVE_STATES = ("pending", "running", "stopping", "stopped")
#: How long, at most, to read a changed protection back until EC2 shows it, for all the VMs
#: changed together (#378: it showed 157 s late on Oct 15).
SETTLE_SECONDS = 300.0
#: How often each VM that doesn't show it yet is read again.
POLL_SECONDS = 10.0


def protect(
    ec2: Any,
    tag: Scope,
    *,
    only: str | Sequence[str] = (),
    off: bool = False,
    dry_run: bool = False,
    wait: bool = True,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] | None = None,  # time.sleep, looked up at the call
    clock: Callable[[], float] | None = None,  # time.monotonic, likewise
) -> int:
    only = only_slugs(only)  # a bare string is one slug, not a set of characters
    try:
        found = ec2.find_instances(scope_tags(tag), LIVE_STATES)
    except Exception as exc:  # botocore: an expired credential, UnauthorizedOperation
        log(f"FAILED: the EC2 lookup: {error_line(exc)}")
        return 1
    # never anything but a participant's VM or a spare (docs/ADMIN.md §3.2): `--off` must not
    # lift the protection of an admin server tagged `workshop=…` by mistake
    instances = [i for i in found if has_participant(i)]
    for inst in [] if only else found:
        if not has_participant(inst):
            why = f"{not_participant(inst)} (not a participant's VM or a spare)"
            log(f"skip {inst['InstanceId']} {tags_of(inst).get('Name', '')}: {why}")
    if only:
        instances = [i for i in instances if tags_of(i).get("participant") in set(only)]
        missing = sorted(set(only) - {tags_of(i).get("participant") for i in instances})
        for slug in missing:
            log(f"{slug}: no live instance with {scope_text(tag)}")
        if missing:
            return 1
    if not instances:
        log("nothing matches")
        return 0
    failed = 0
    changed: list[tuple[str, str]] = []  # (who, instance) whose termination protection was set
    for inst in sorted(instances, key=lambda i: tags_of(i).get("participant", "")):
        iid = inst["InstanceId"]
        who = tags_of(inst).get("participant", "?")
        try:
            protected, behavior = ec2.protection(iid)
            changes = []
            if protected == off:
                changes.append(f"termination protection {'off' if off else 'on'}")
                if not dry_run:
                    ec2.set_termination_protection(iid, not off)
                    changed.append((who, iid))
            if behavior != "stop":
                changes.append(f"on shutdown: {behavior} → stop")
                if not dry_run:
                    ec2.set_shutdown_stop(iid)
        except Exception as exc:  # botocore errors: report this instance and go on
            failed += 1
            log(f"{who} {iid}: FAILED: {exc}")
            continue
        state = inst["State"]["Name"]
        if not changes:
            now = "protected" if protected else "not protected"
            log(f"{who} {iid} ({state}): unchanged ({now}, stops on shutdown)")
        else:
            verb = "would set" if dry_run else "set"
            log(f"{who} {iid} ({state}): {verb} " + "; ".join(changes))
    if dry_run:
        log("dry run: nothing changed")
    elif changed:
        sleep, clock = sleep or time.sleep, clock or time.monotonic
        read_back(ec2, changed, on=not off, wait=wait, log=log, sleep=sleep, clock=clock)
    return 1 if failed else 0


def read_back(
    ec2: Any,
    changed: Sequence[tuple[str, str]],
    *,
    on: bool,
    wait: bool,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Say per VM whether EC2 shows the termination protection just set (module doc): read
    each VM that doesn't show it yet every `POLL_SECONDS`, for `SETTLE_SECONDS` at most in all;
    with `wait` False, not at all. Never a failure: the change was made, and the read lags."""
    word = "on" if on else "off"
    if not wait:
        for who, iid in changed:
            log(f"{who} {iid}: termination protection {word}: set; not read back (--no-wait): "
                "EC2 can take a few minutes to show it")  # fmt: skip
        return
    log(f"reading it back until EC2 shows it, for {SETTLE_SECONDS:.0f} s at most")
    pending = {iid: "" for _, iid in changed}  # instance → why its last read failed, or ""
    deadline = clock() + SETTLE_SECONDS
    while True:
        for iid in list(pending):
            try:
                shows = bool(ec2.termination_protection(iid)) == on
            except Exception as exc:  # botocore: one more read that doesn't show it
                pending[iid] = error_line(exc)
                continue
            if shows:
                del pending[iid]
            else:
                pending[iid] = ""
        left = deadline - clock()
        if not pending or left <= 0:
            break
        sleep(min(POLL_SECONDS, left))
    for who, iid in changed:
        if iid not in pending:
            log(f"{who} {iid}: termination protection {word}: set; EC2 shows it now")
            continue
        why = f"; its last read failed: {pending[iid]}" if pending[iid] else ""
        log(f"{who} {iid}: termination protection {word}: set; EC2 doesn't show it yet "
            f"(it can take a few minutes){why}")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Termination protection and stop-on-shutdown for kept workshop VMs."
    )
    add_tag_argument(parser)
    parser.add_argument("--region", required=True, help="the workshop's region")
    parser.add_argument(
        "--only", action="append", default=[], metavar="SLUG", help="one participant (repeatable)"
    )
    parser.add_argument(
        "--off", action="store_true", help="turn termination protection off (before teardown)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help=f"don't read the protection back until EC2 shows it ({SETTLE_SECONDS:.0f} s at most)",
    )
    opts = parser.parse_args(argv)
    try:
        tag = parse_tags(opts.tag)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return protect(
        Ec2(opts.region),
        tag,
        only=opts.only,
        off=opts.off,
        dry_run=opts.dry_run,
        wait=not opts.no_wait,
    )


if __name__ == "__main__":
    raise SystemExit(main())
