#!/usr/bin/env python3
"""Give the spares their own idle stop (#715; ORGANIZERS §3.7; workshop/admin/README.md §8).

    uv run --with boto3 python workshop/aws/spare_idle_stop.py --roster roster.csv \\
        --minutes N --only zab-spare-N … [--tag KEY=VALUE …] [--region R] [--spares-file FILE] \\
        [--started] [--dry-run]

A spare gets neither the waker nor a row's idle stop: it has no bots, and `all` never names it.
This gives each spare named `idle_stop.py --spare --minutes N`, run once a minute by its timer,
so that a spare nobody adopts powers itself off once it has been idle for N minutes, never before
the first boot's volume warm-up has ended (idle_stop.py's module doc). The admin bot runs it
after `!launch … spares=N` and after `!start` of spares, with admin.toml's `spare_idle_stop`
(docs/ADMIN.md §2.1). A spare that has it gets the same files again, its minutes included: a
changed `spare_idle_stop` reaches each spare at its next `!start`. `!adopt-spare` replaces it
with the row's (`push_config.py --adopt-spare`).

For each spare named, in turn; one that fails is reported, and the others go on (exit 1):
1. **Its entry** in `spares.json` (`launch.py`), for this scope and region. A spare adopted, or
   at an address a roster row has (handed over by hand, ORGANIZERS §7.2), is skipped: it is a
   participant's VM now. So is a name without an entry.
2. **Its VM in EC2**: the instance at the entry's address, tagged as the spare (as `power.py`
   finds it). One pending or running is waited for until its port 22 answers, under one
   deadline for all of them, `WAIT_S`. One stopped or stopping is skipped: it gets idle stop at
   its next `!start`. With `--started` (the bot's `!start`, after `power.py --start` without
   `--wait`), it is waited for as well: EC2 can still read a VM it was just asked to start as
   `stopped` (`power.settle`), so a spare skipped then would run on without its idle stop
   (Codex round 1 on #729). One whose port 22 still doesn't answer at the deadline fails.
3. **Its host key**: `ssh-keyscan`'s ed25519 key, whose fingerprint must be one of the entry's
   `host_keys`, which `launch.py` read from the spare's console at its first boot. It is pinned
   in a `known_hosts` file of this run alone, never the workshop's. A spare without `host_keys`
   is refused.
4. **On the VM** (`push_config.install_spare_idle_stop`), as ubuntu: the owner check, which must
   find the spare still (no participant in `/etc/zab-participant.json`, its hostname the
   spare's); then `idle_stop.py` and its units, as root, and the timer (re)started.

`--dry-run` reads `spares.json` and EC2, and changes nothing: no SSH.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "admin"))

from admin_common import Runner, first_line, host_key_fingerprint, subprocess_runner
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
    tags_of,
)
from launch import SPARES_FILE
from power import EC2_CONFIG, LIVE_STATES, POLL_S, answering
from push_config import IDLE_STOP_MIN, install_spare_idle_stop
from scan_hosts import key_line, keyscan_argv

#: How long a pending or running spare's port 22 is waited for, for all of them at once: a
#: spare `!start` has just started answers within a minute or two.
WAIT_S = 300.0
#: The most minutes, as `!idle-stop`'s (the grammar's).
MAX_MINUTES = 240


def entries(spares: dict[str, Any], scope: Scope, region: str) -> dict[str, dict[str, Any]]:
    """This scope's and region's spares in `spares.json`, by name."""
    prefix = f"{scope_label(scope)}/{region}/"
    return {
        str(e.get("name") or key[len(prefix) :]): e
        for key, e in spares.items()
        if key.startswith(prefix) and isinstance(e, dict)
    }


def unavailable(name: str, entry: dict[str, Any] | None, taken: dict[str, str]) -> str:
    """Why the spare `name` is skipped (step 1), or ""."""
    if entry is None:
        return "not in spares.json for this scope and region"
    if entry.get("adopted_by"):
        return f"adopted by {entry['adopted_by']}: a participant's VM"
    ip = entry.get("ip")
    if not isinstance(ip, str) or not ip:
        return "no address in spares.json: rerun launch.py"
    if ip in taken:
        return f"its address is {taken[ip]}'s (handed over): a participant's VM"
    return ""


def pinned(ip: str, printed: str, runner: Runner, folder: str) -> tuple[str, str]:
    """(a `known_hosts` file that pins the spare's ed25519 key, "") once its fingerprint is
    one the console printed (step 3); ("", why) otherwise."""
    if not printed.split():
        return "", "no host_keys in spares.json to check its key against: rerun launch.py"
    res = runner(keyscan_argv(ip), timeout_s=30)
    line = key_line(res.stdout, ip)
    if line is None:
        return "", f"ssh-keyscan: {first_line(res.stderr, 80) or f'exit {res.exit_code}'}"
    try:
        found = host_key_fingerprint(line)
    except ValueError as exc:
        return "", f"ssh-keyscan: {exc}"
    if found not in printed.split():
        return "", f"its key {found} is not one its console printed: nothing installed"
    path = pathlib.Path(folder) / "known_hosts"
    path.write_text(line + "\n", encoding="utf-8")
    return str(path), ""


def wait_ssh(
    ips: Sequence[str],
    *,
    probe: Callable[[Sequence[str]], set[str]],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    """The addresses whose port 22 answers within `WAIT_S`, all tried together each round."""
    deadline, left, up = clock() + WAIT_S, list(ips), set[str]()
    while left:
        up |= probe(left)
        left = [ip for ip in left if ip not in up]
        if not left or clock() >= deadline:
            break
        sleep(min(POLL_S, max(0.0, deadline - clock())))
    return up


def give(
    ec2: Any,
    rows: list[dict[str, str]],
    spares: dict[str, Any],
    names: Sequence[str],
    *,
    minutes: int,
    scope: Scope,
    region: str,
    dry_run: bool = False,
    started: bool = False,
    runner: Runner = subprocess_runner,
    probe: Callable[[Sequence[str]], set[str]] = answering,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Steps 1 to 4 for each of `names`; one line each, then a summary. 1 on any failure.
    `started`: the spares were just asked to start, so a stopped one is waited for too."""
    ours = entries(spares, scope, region)
    taken = {r["ip"]: r.get("slug", "") for r in participants(rows) if r.get("ip")}
    done = skipped = failed = 0
    todo: list[tuple[str, str, str]] = []  # (name, ip, instance) to wait for
    try:
        found = ec2.find_instances(scope_tags(scope), LIVE_STATES)
    except Exception as exc:  # botocore: an expired credential, UnauthorizedOperation
        log(f"FAILED: the EC2 lookup: {error_line(exc)}")
        return 1
    by_ip = {ip: i for i in found if has_participant(i) for ip in instance_ips(i)}
    for name in names:
        entry = ours.get(name)
        why = unavailable(name, entry, taken)
        if why:
            skipped += 1
            log(f"skip {name}: {why}")
            continue
        assert entry is not None
        ip = str(entry["ip"])
        inst = by_ip.get(ip)
        if inst is None or tags_of(inst).get("participant") != name:
            failed += 1
            tag = tags_of(inst).get("participant") if inst else None
            what = f"is tagged participant={tag}" if inst else "is no live VM of this scope"
            log(f"{name} {ip}: FAILED: the instance at its address {what}")
            continue
        iid, state = inst["InstanceId"], inst["State"]["Name"]
        if state in ("stopped", "stopping") and not started:
            skipped += 1
            log(f"{name} {iid} ({state}): skipped: it gets idle stop at its next !start {name}")
            continue
        if dry_run:
            done += 1
            log(f"{name} {iid} ({state}): would install idle stop after {minutes} min")
            continue
        todo.append((name, ip, iid))
    ips = [ip for _, ip, _ in todo]
    up = wait_ssh(ips, probe=probe, clock=clock, sleep=sleep) if todo else set()
    with tempfile.TemporaryDirectory(prefix="zab-spare-kh-") as folder:
        for name, ip, iid in todo:
            if ip not in up:
                failed += 1
                log(f"{name} {iid}: FAILED: its SSH ({ip}:22) doesn't answer after {WAIT_S:.0f} s")
                continue
            entry = ours[name]
            known_hosts, why = pinned(ip, str(entry.get("host_keys") or ""), runner, folder)
            why = why or install_spare_idle_stop(ip, name, minutes, runner, known_hosts=known_hosts)
            if why:
                failed += 1
                log(f"{name} {iid}: FAILED: {why}")
                continue
            done += 1
            log(f"{name} {iid}: idle stop after {minutes} min (a spare's), installed")
    would = "would install" if dry_run else "installed"
    log(f"{would} {done}, skipped {skipped}, failed {failed}")
    if dry_run:
        log("dry run: nothing changed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Give the spares their own idle stop (#715).")
    parser.add_argument("--roster", required=True)
    parser.add_argument("--minutes", type=int, required=True, help="admin.toml's spare_idle_stop")
    parser.add_argument(
        "--only", action="append", default=[], metavar="SPARE", help="zab-spare-N (repeatable)"
    )
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--spares-file", metavar="FILE", help=f"default {SPARES_FILE}")
    add_tag_argument(parser)
    parser.add_argument(
        "--started",
        action="store_true",
        help="the spares were just asked to start: wait for a stopped one too (step 2)",
    )
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv)
    names = only_slugs(opts.only)
    if not names or not all(is_spare_name(name) for name in names):
        parser.error("name the spares, each zab-spare-N, with --only")
    if not IDLE_STOP_MIN <= opts.minutes <= MAX_MINUTES:
        parser.error(f"--minutes takes {IDLE_STOP_MIN} to {MAX_MINUTES}")
    try:
        scope = parse_tags(opts.tag)
        rows = read_roster(opts.roster)
        path = pathlib.Path(opts.spares_file) if opts.spares_file else SPARES_FILE
        spares = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(spares, dict):
            raise ValueError(f"{path} is not a JSON object")
    except (OSError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    print(f"scope: {scope_text(scope)}; region {opts.region}")
    try:
        ec2 = Ec2(opts.region, config=EC2_CONFIG)
    except ImportError:
        print("refusing: it needs boto3 (uv run --with boto3 python …)", file=sys.stderr)
        return 1
    return give(
        ec2, rows, spares, names, minutes=opts.minutes, scope=scope, region=opts.region,
        dry_run=opts.dry_run, started=opts.started,
    )  # fmt: skip


if __name__ == "__main__":
    raise SystemExit(main())
