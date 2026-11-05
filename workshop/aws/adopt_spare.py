#!/usr/bin/env python3
"""`adopt_spare.py --roster roster.csv --slug <slug> --spare zab-spare-N [--region R]`
(ORGANIZERS §7.2).

Gives a spare VM to a roster row that has no VM yet: a late joiner (PM on issue #36, Wed 21:20).
1. Reads the spare's entry `<workshop>/<region>/<spare>` in `workshop/admin/out/spares.json`
   (written by `launch.py`): its instance id, address and host-key fingerprints.
2. Checks in EC2 that the instance with the spare's tags is that instance, that its Elastic IP
   is that address, and that nothing carries the slug's tags yet; last, that the spare can be
   reached (#455): the chain's next step, `scan_hosts.py`, connects to it, and a chain that
   fails after this script's retag (3. below) leaves the spare the row's, for a deployer to
   finish by hand. A `running` spare's port 22 must answer (`no_ssh`). A `stopped` one is
   started for the adoption (#715): the dry run says so (`STARTED`), and the apply starts it,
   then waits until it runs and its port 22 answers, `START_WAIT_S` at most, before anything
   else changes. Spares stop themselves when idle (`spare_idle_stop.py`), so no spare needs to
   be kept running for a late joiner. One `pending` or `stopping` is refused, with the wait it
   needs (`not_ready`), at plan time and again at the apply.
3. Retags the instance, its volumes and its Elastic IP as the participant's
   (`participant=<slug>`, `Name=ws-<slug>`), so that `launch.py` counts the VM as the row's and
   keeps its address, and `teardown.py --only <slug>` finds it. The request carries these two
   keys only: the spare has every scope tag already (its lookup requires them), and the admin
   server's role refuses a retag that names any other key, `workshop` included (ADMIN.md §5).
4. Writes the row's `host` (`ws-<slug>`), `ip`, `region` and `host_keys`, merged into the
   roster as it is now (only these columns). Another row that kept the spare's address, which
   EC2 gave the spare once it was released with that row's VM, has its `ip` cleared (#604), but
   only when its `host_keys` and the spare's share no fingerprint. One that shares one was given
   the spare by hand (ORGANIZERS §7.2, "A broken VM"), and so is refused at step 2, as is a row that
   can't be told apart (no fingerprints on either side).
5. Marks the entry in `spares.json` as adopted, so it is never handed over twice.

**The spare's own idle stop is held first** (#715; Codex round 2 on #729). A spare powers itself
off once idle (`spare_idle_stop.py`), and nothing of the hand-over connects to it before the
chain's `push_config.py`, whose hold then covers it: a spare at the end of its idle minutes could
stop after the retag, which can't be undone, and before that hold. So the apply, before the
retag (and after a stopped spare's start), writes the idle-stop hold
(`admin_common.IDLE_HOLD_ON_CMD`, an hour ahead) over SSH as ubuntu, its host key pinned to a
fingerprint `spares.json`'s `host_keys` has (`spare_idle_stop.pinned`); `push_config.py` removes
it at its end. Then it stops idle stop's timer, and a tick under way, which may have read its
facts before the hold (`HOLD_CHECK_CMD`); `push_config.py`'s idle-stop step gives the VM the
row's, or none, and a reboot starts the timer again. One whose key can't be pinned is refused,
nothing changed, and so is one whose system is `stopping` by then: its idle stop powered it off
before the hold. When the hold's SSH fails, or the retag after it, the command may have paused
the idle stop of what is still a spare: its timer is started again, best effort (`RESUME_CMD`,
over the pinned SSH), and the refusal says whether it was. A spare without `host_keys` has no
idle stop of its own (the script refuses it), so nothing is held there.

`--dry-run` reads EC2, tries a running spare's port 22, and changes nothing. The VM itself is
handed
over afterwards by `push_config.py --adopt-spare <slug>` (owner record, hostname, git identity);
ORGANIZERS §7.2 has the whole sequence. `launch.py --spares N` launches every missing
`zab-spare-1` … `zab-spare-N`, the adopted one included, so it is not a total count: run
`launch.py` without `--spares` afterwards unless a replacement for the adopted spare is wanted.

Needs the region's credential (`AWS_PROFILE`) and boto3, and the fleet's SSH key: `uv run --with
boto3 python workshop/aws/adopt_spare.py …`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import tempfile
import time
from collections.abc import Callable
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "admin"))

from admin_common import IDLE_HOLD_ON_CMD, Runner, first_line, ssh_argv, subprocess_runner
from aws_common import (
    Ec2,
    Scope,
    add_tag_argument,
    error_line,
    has_participant,
    merge_roster_changes,
    parse_tags,
    read_roster,
    scope_label,
    slug_of,
    ssh_answers,
    stale_ip_rows,
    tags_of,
    wait_for_ssh,
    write_spares,
)
from launch import SPARES_FILE, lookup_tags, participant_tags
from power import EC2_CONFIG
from spare_idle_stop import pinned

STATES = ("pending", "running", "stopping", "stopped")
#: How long the check waits for the spare's port 22 (`wait_for_ssh`): a spare that has just
#: started may need a few more seconds.
SSH_WAIT_S = 10.0
#: A fixed literal: the hold; then idle stop's timer and a tick under way stopped (`systemctl
#: stop` waits for it, and ends one that read its facts before the hold, Codex round 3 on #729),
#: until a boot or `push_config.py`'s idle-stop step starts it again; then the system's state,
#: which is `stopping` once a power-off has begun, so that a hold come too late is seen before
#: the retag (review of #728). Units that aren't there, and the state's own exit code (non-zero
#: for `degraded`, as many VMs are), decide nothing.
QUIESCE = "sudo systemctl stop zab-idle-stop.timer zab-idle-stop.service 2>/dev/null"
HOLD_CHECK_CMD = (
    f"{IDLE_HOLD_ON_CMD} && {{ {QUIESCE}; true; }} && {{ systemctl is-system-running || true; }}"
)
#: A stopped spare started for the adoption (#715): how long the apply waits, from the start
#: request, until it runs and its port 22 answers, and how often it looks. A stopped fleet VM
#: runs and answers within a minute or two.
START_WAIT_S, START_POLL_S = 300.0, 5.0

#: The only tag keys the retag sends (docs/ADMIN.md §3.2, §5): IAM checks every key in a
#: `CreateTags` request, changed or not, against `aws:TagKeys`.
RETAG_KEYS = ("Name", "participant")


class AdoptError(Exception):
    """A check failed: nothing was changed."""


def not_ready(spare: str, state: str) -> str:
    """Why a spare `pending` or `stopping` is refused (#455, #715): one line, with the wait it
    needs, which ORGANIZERS §3.7 quotes. A stopped spare is started instead (`STARTED`)."""
    if state == "stopping":
        return (
            f"{spare} is stopping: request again in a minute or two, once it has stopped "
            "(this plan then starts it)"
        )
    if state == "pending":
        return f"{spare} is starting (pending): request again in a minute"
    return f"{spare} is {state}, neither running nor stopped: check it in EC2"


def started(spare: str) -> str:
    """The dry run's line for a stopped spare (#715), which the plan shows."""
    return f"{spare}: stopped, started for the adoption"


def no_ssh(spare: str, ip: str) -> str:
    """Why a running spare whose port 22 doesn't answer is refused (#455)."""
    return (
        f"{spare} is running, but its SSH ({ip}:22) doesn't answer yet: request again in a minute"
    )


def load_spares(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AdoptError(
            f"{path} not found: copy workshop/admin/out/ from launch.py's machine"
        ) from None
    except ValueError as exc:
        raise AdoptError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AdoptError(f"{path} is not a JSON object")
    return data


def plan(
    rows: list[dict[str, str]],
    spares: dict[str, Any],
    ec2: Any,
    *,
    slug: str,
    spare: str,
    region: str,
    workshop: Scope,
    force: bool = False,
) -> dict[str, Any]:
    """Every check (raises AdoptError); what to change."""
    row = next((r for r in rows if slug_of(r) == slug), None)
    if row is None:
        raise AdoptError(f"no row {slug!r} in the roster: add the participant's row first")
    if (row.get("host") or row.get("ip")) and not force:
        raise AdoptError(
            f"{slug} already has a VM (host {row.get('host') or '-'}, ip {row.get('ip') or '-'}); "
            "a broken VM is replaced as in ORGANIZERS §7.2, 'A broken VM'"
        )
    key = f"{scope_label(workshop)}/{region}/{spare}"
    entry = spares.get(key)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(k for k in spares if not spares[k].get("adopted_by"))) or "none"
        raise AdoptError(f"no spare {key} in spares.json (available: {known})")
    if entry.get("adopted_by"):
        raise AdoptError(f"{key} was already given to {entry['adopted_by']}")
    instance_id, ip = entry.get("instance_id") or "", entry.get("ip") or ""
    if not instance_id or not ip:
        raise AdoptError(f"{key} has no instance id or address in spares.json: rerun launch.py")
    # never the admin server, even tagged as this spare by mistake (docs/ADMIN.md §3.2)
    found = ec2.find_instances(lookup_tags(spare, workshop), STATES)
    found = [i for i in found if has_participant(i)]
    if [i["InstanceId"] for i in found] != [instance_id]:
        ids = ", ".join(i["InstanceId"] for i in found) or "none"
        raise AdoptError(f"EC2 has {ids} tagged {spare}, spares.json {instance_id}")
    instance = found[0]
    taken = ec2.find_instances(lookup_tags(slug, workshop), STATES) + ec2.find_addresses(
        lookup_tags(slug, workshop)
    )
    if taken:
        what = ", ".join(r.get("InstanceId") or r.get("AllocationId", "?") for r in taken)
        raise AdoptError(f"resources are already tagged participant={slug}: {what}")
    addresses = [
        a
        for a in ec2.find_addresses(lookup_tags(spare, workshop))
        if a.get("InstanceId") == instance_id and has_participant(a)
    ]
    if [a.get("PublicIp") for a in addresses] != [ip]:
        got = ", ".join(str(a.get("PublicIp")) for a in addresses) or "none"
        raise AdoptError(f"{spare}'s Elastic IP is {got}, spares.json says {ip}")
    holders = stale_ip_rows(rows, slug, ip)
    for other in holders:
        refuse_handed_over(other, spare, ip, entry.get("host_keys") or "")
    state = (instance.get("State") or {}).get("Name") or "in an unknown state"
    if state not in ("running", "stopped"):
        raise AdoptError(not_ready(spare, state))
    if state == "running" and not wait_for_ssh(ip, timeout_s=SSH_WAIT_S):
        raise AdoptError(no_ssh(spare, ip))
    volumes = [
        m["Ebs"]["VolumeId"]
        for m in instance.get("BlockDeviceMappings", [])
        if isinstance(m.get("Ebs"), dict) and m["Ebs"].get("VolumeId")
    ]
    return {
        "key": key,
        "instance_id": instance_id,
        "start": state == "stopped",  # started for the adoption, before the retag (#715)
        "resources": [instance_id, *volumes, *(a["AllocationId"] for a in addresses)],
        "tags": {k: v for k, v in participant_tags(slug, workshop).items() if k in RETAG_KEYS},
        "row": {
            "host": f"ws-{slug}",
            "ip": ip,
            "region": region,
            "host_keys": entry.get("host_keys") or "",
        },
        "was": tags_of(instance).get("Name", ""),
        # EC2 shows the address on the spare, and the row's host keys aren't the spare's: it
        # kept an address released with its own VM, which EC2 gave the spare (#604)
        "stale": [slug_of(r) for r in holders],
    }


def hold_spare(spare: str, ip: str, printed: str, runner: Runner) -> str:
    """The spare's idle stop held before its retag (module doc; Codex round 2 on #729): what
    to print, or AdoptError. `printed`: its `host_keys` in spares.json."""
    if not printed.split():
        return f"warning: {spare} has no host_keys in spares.json, nor its own idle stop: none held"
    with tempfile.TemporaryDirectory(prefix="zab-adopt-kh-") as folder:
        known_hosts, why = pinned(ip, printed, runner, folder)
        if why:  # nothing reached the VM
            raise AdoptError(
                f"{spare}'s idle stop could not be held for the hand-over ({why}): nothing "
                "changed; request again in a minute"
            )
        res = runner(ssh_argv(ip, HOLD_CHECK_CMD, known_hosts=known_hosts), timeout_s=60)
    said = res.stdout.split()
    if res.ok and said and said[-1] == "stopping":  # a hold too late (review of #728)
        raise AdoptError(
            f"{spare} is shutting down: its idle stop powered it off just now, and its next boot "
            "starts it again; nothing else changed: request again in a minute or two"
        )
    if not res.ok:  # the command may have run, its idle stop paused: resumed (review of #729)
        detail = first_line(res.stderr) or first_line(res.stdout) or f"exit {res.exit_code}"
        raise AdoptError(
            f"{spare}'s idle stop could not be held for the hand-over (ssh: {detail}); "
            f"{resume_idle_stop(ip, printed, runner)}: request again in a minute"
        )
    return f"{spare}: its idle stop held for the hand-over (push_config.py releases it)"


#: A fixed literal: idle stop's timer started again, on a spare whose hand-over stopped after
#: `HOLD_CHECK_CMD` paused it (review of #729); the hold expires by itself within the hour.
RESUME_CMD = "sudo systemctl start zab-idle-stop.timer"


def resume_idle_stop(ip: str, printed: str, runner: Runner) -> str:
    """Best effort, over SSH as ubuntu, the key pinned again: start the spare's idle-stop timer,
    which `HOLD_CHECK_CMD` may have stopped, so that a spare whose hand-over failed doesn't run
    on with none. What happened, for the refusal's text; never an exception."""
    try:
        with tempfile.TemporaryDirectory(prefix="zab-adopt-kh-") as folder:
            known_hosts, why = pinned(ip, printed, runner, folder)
            if not why:
                res = runner(ssh_argv(ip, RESUME_CMD, known_hosts=known_hosts), timeout_s=60)
                if res.ok:
                    return "its idle stop may have been paused, and was started again"
                why = first_line(res.stderr) or first_line(res.stdout) or f"exit {res.exit_code}"
    except Exception as exc:  # a refusal must still be said
        why = f"{type(exc).__name__}: {exc}"
    return (
        f"its idle stop may be paused, and could not be started again ({why}): "
        f"`ssh ubuntu@{ip} {RESUME_CMD}` starts it, or the spare's next `!start`"
    )


def start_spare(
    ec2: Any,
    spare: str,
    instance_id: str,
    ip: str,
    workshop: Scope,
    *,
    answers: Callable[[str], bool] = ssh_answers,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Start a stopped spare for its adoption (#715), then wait until EC2 shows it `running`
    and its port 22 answers; raises AdoptError, which says what to do, when it doesn't get
    there. Nothing else has changed yet: the spare is still the spare, and its own idle stop
    stops it again when idle. **The bound** (Codex round 1 on #731): `START_WAIT_S` from before
    the start request, the request included; the rounds stop at the deadline, and each lookup
    asks for no page past it. Every EC2 request gives up within about a minute (`main`'s client,
    `power.EC2_CONFIG`: two tries, each at most 10 s to connect and 20 s to read), so the whole
    wait takes at most `START_WAIT_S` plus one such request and one 5 s try of port 22."""
    deadline = clock() + START_WAIT_S
    try:
        ec2.start([instance_id])
    except Exception as exc:  # botocore: IncorrectInstanceState (stopping meanwhile), a credential
        raise AdoptError(f"{spare} could not be started: {error_line(exc)}") from None
    state = "stopped"
    while True:
        try:
            listed = ec2.find_instances(
                lookup_tags(spare, workshop), STATES, deadline=deadline, clock=clock
            )
        except Exception as exc:  # botocore, or its pages past the deadline (TimeoutError)
            raise AdoptError(
                f"{spare} was started, but the EC2 lookup failed: {error_line(exc)}: request "
                "again in a minute"
            ) from None
        found = [i for i in listed if i.get("InstanceId") == instance_id]
        state = (found[0].get("State") or {}).get("Name", "") if found else "gone"
        if state == "running" and answers(ip):
            return
        if state not in ("pending", "running", "stopped"):  # EC2's reads lag its writes
            raise AdoptError(f"{spare} was started, but it is {state} now: check it in EC2")
        if clock() >= deadline:
            break
        sleep(START_POLL_S)
    if state == "running":
        raise AdoptError(
            f"{spare} was started, but its SSH ({ip}:22) doesn't answer after "
            f"{START_WAIT_S:.0f} s: request again in a minute"
        )
    raise AdoptError(
        f"{spare} was started, but it is still {state} after {START_WAIT_S:.0f} s: "
        "request again in a minute"
    )


def refuse_handed_over(row: dict[str, str], spare: str, ip: str, printed: str) -> None:
    """Raise AdoptError unless `row`, at the spare's address, is stale for certain: its
    `host_keys` and the spare's entry both have fingerprints, and none in common (#604). One in
    common is ORGANIZERS §7.2's hand-over by hand ("A broken VM"), which gives the row the spare's
    `ip` and `host_keys` and leaves the VM tagged as the spare, unmarked in `spares.json`: the
    VM is that participant's, and it must not go to another row (review of #606). Without both
    fingerprints, the two cases can't be told apart, so the hand-over is refused too."""
    slug = slug_of(row)
    theirs, ours = set((row.get("host_keys") or "").split()), set(printed.split())
    if theirs and ours and not theirs & ours:
        return
    if theirs & ours:
        raise AdoptError(
            f"{slug} holds {spare}'s address {ip} and host keys: {spare} was handed over to "
            f"{slug} by hand (ORGANIZERS §7.2, 'A broken VM'). Choose another spare, and mark this "
            f"one adopted_by {slug} in spares.json"
        )
    raise AdoptError(
        f"{slug} holds {spare}'s address {ip}, and the host keys can't tell why: a hand-over "
        f"by hand (ORGANIZERS §7.2, 'A broken VM'), or an address {slug} kept when its own VM was "
        f"torn down. Check; in the second case clear {slug}'s ip in the roster first"
    )


def main(argv: list[str] | None = None, *, runner: Runner | None = None) -> int:
    run = runner if runner is not None else subprocess_runner  # looked up at call time: tests
    parser = argparse.ArgumentParser(
        prog="adopt_spare.py",
        description="Give a spare VM to a roster row without one (ORGANIZERS §7.2).",
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--slug", required=True, help="the participant's row")
    parser.add_argument("--spare", required=True, help="the spare's name, e.g. zab-spare-7")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--spares-file", default=str(SPARES_FILE))
    add_tag_argument(parser)
    parser.add_argument("--force", action="store_true", help="the row may already have a host")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        workshop = parse_tags(args.tag)
        rows = read_roster(args.roster)
        spares_path = pathlib.Path(args.spares_file)
        spares = load_spares(spares_path)
        ec2 = Ec2(args.region, config=EC2_CONFIG)  # bounded requests (Codex round 1 on #731)
        todo = plan(
            rows,
            spares,
            ec2,
            slug=args.slug,
            spare=args.spare,
            region=args.region,
            workshop=workshop,
            force=args.force,
        )
    except (AdoptError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    tags = ", ".join(f"{k}={v}" for k, v in todo["tags"].items())
    if todo["start"]:
        print(started(args.spare))
    print(f"{args.spare} ({todo['was']}) → {args.slug}: tag {', '.join(todo['resources'])} {tags}")
    print("roster: " + ", ".join(f"{k}={v or '-'}" for k, v in todo["row"].items()))
    for other in todo["stale"]:  # #604
        print(f"roster: {other}'s ip {todo['row']['ip']} cleared: released with its own VM, it is "
              f"{args.spare}'s now")  # fmt: skip
    if not todo["row"]["host_keys"]:
        print("warning: no host_keys in spares.json: scan_hosts.py will record the key unverified")
    if args.dry_run:
        print("(dry run: nothing changed)")
        return 0
    try:
        if todo["start"]:
            start_spare(ec2, args.spare, todo["instance_id"], todo["row"]["ip"], workshop)
            print(f"{args.spare}: started; it runs, and its SSH answers")
        # before the retag, which can't be undone (Codex round 2 on #729)
        print(hold_spare(args.spare, todo["row"]["ip"], todo["row"]["host_keys"], run))
    except AdoptError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    try:
        ec2.create_tags(todo["resources"], todo["tags"])
    except Exception as exc:  # the spare is still the spare: its idle stop goes on (review of #729)
        resumed = resume_idle_stop(todo["row"]["ip"], todo["row"]["host_keys"], run)
        print(
            f"refusing: the retag failed ({error_line(exc)}): check {args.spare}'s tags in "
            f"EC2; {resumed}: request again in a minute",
            file=sys.stderr,
        )
        return 1
    try:
        stale = {other: {"ip": ""} for other in todo["stale"]}
        expect = {other: {"ip": todo["row"]["ip"]} for other in todo["stale"]}  # still stale
        gone = merge_roster_changes(args.roster, {**stale, args.slug: todo["row"]}, expect)
    except (OSError, ValueError) as exc:
        print(
            f"roster NOT written ({exc}): the VM is tagged {args.slug}; set these columns in "
            "its row by hand: " + ", ".join(f"{k}={v}" for k, v in todo["row"].items()),
            file=sys.stderr,
        )
        return 1
    if args.slug in gone:  # a stale row gone meanwhile has nothing left to clear
        print(f"warning: the row {args.slug} left the roster meanwhile", file=sys.stderr)
        return 1
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    spares[todo["key"]] = {**spares[todo["key"]], "adopted_by": args.slug, "adopted": stamp}
    write_spares(spares_path, spares)
    print(
        f"done. Next (ORGANIZERS §7.2): scan_hosts.py --only {args.slug}; zulip_provision.py "
        f"--only {args.slug}; push_config.py --wheel … --only {args.slug} --adopt-spare "
        f"{args.slug}; provision_repos.py --zuliprc … --only {args.slug}; push_config.py --wheel "
        f"… --only {args.slug}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
