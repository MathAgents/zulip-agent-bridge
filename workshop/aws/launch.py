"""Launch one workshop VM per roster row, plus spares (README.md, "launch.py").

    uv run --with boto3 python workshop/aws/launch.py --roster roster.csv --ami ami-… \\
        --security-group-id sg-… [--key-name NAME] --region REGION \\
        [--instance-type m7a.2xlarge] [--subnet-id subnet-…] [--spares N] [--only SLUG …] \\
        [--admin-key FILE.pub …] --tag KEY=VALUE [--tag …] [--ssh-from CIDR …] [--protect] \\
        [--spares-file FILE] [--dry-run]

Standalone script (boto3 is not a zab dependency), run by the admin bot or a deployer with AWS
credentials; agents run it only for test infrastructure, with `--tag zab-role=test` among the
scope tags. Idempotent: a row whose `participant` tag already has a live
instance in this workshop is not launched again (its Elastic IP is reconciled instead). The
address EC2 shows on a row's VM is cleared from any other row that kept it: EC2 can give an
Elastic IP released with one VM to the next, and two rows with one `ip` stop every admin script
(#604). A row at an existing spare's address is only reported: a spare handed over by hand
(ORGANIZERS §7.2, "A broken VM") gives the row that address and leaves the VM tagged as the spare.
Errors are handled per row and summarized; the roster is always written back (atomically, with a
`.bak`). `--dry-run` prints every action and runs only queries. Participants get no SSH (issue
#36, Mon Oct 5): the roster's GitHub columns play no part here, and no GitHub request is made;
`provision_repos.py` gives participants access to their repository instead. `--admin-key` adds
organizer keys for `ubuntu` at first boot. Once a new VM is up, its host-key fingerprints are
read from the console cloud-init writes and recorded in the roster's `host_keys` column, which
`scan_hosts.py` checks the scanned key against. Every instance stops, rather than terminates,
when it shuts itself down (idle stop, issue #207), and `--protect` turns on termination
protection (kept VMs; `protect.py` does both for existing ones).
`--tag` is repeatable: every resource gets every scope tag, and lookups require all of them (the
test VM, issue #263: `--tag zab-role=test --tag workshop=zab-test`, and `--ssh-from` the
development machine's address). `--only` is repeatable and names roster slugs or spares
(`zab-spare-N`, within `--spares N`); a name that is neither is refused before any AWS call.
`--spares-file` says where the spares are recorded (default `workshop/admin/out/spares.json`;
the admin server keeps it outside the release tree, docs/ADMIN.md §2.6).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aws_common import (
    Ec2,
    Scope,
    add_tag_argument,
    column_snapshot,
    console_host_keys,
    has_participant,
    is_spare,
    only_slugs,
    parse_cidr,
    parse_public_key,
    parse_tags,
    participants,
    read_roster,
    render_user_data,
    save_roster_changes,
    scope_label,
    scope_tags,
    scope_text,
    slug_of,
    spare_rows,
    ssh_config_snippet,
    stale_ip_rows,
    tag_list,
    validate_security_group,
    wait_for_ssh,
    write_spares,
)

#: The fleet type (DECISIONS D23, Drew, Sat Oct 3): 8 vCPUs and 32 GiB, for Mathlib's memory
#: (about 7.3 GB per process that imports it) with headroom for Sage and Magma.
INSTANCE_TYPE = "m7a.2xlarge"

LIVE_STATES = ("pending", "running", "stopping", "stopped")


#: One `(key, value)` tag, the form before `--tag` was repeatable, or every scope tag (#263).
Tag = Scope


def workshop_of(opts: argparse.Namespace) -> dict[str, str]:
    """The scope tags from `--tag` (repeatable, required: README.md, "The VMs")."""
    return parse_tags(getattr(opts, "tag", None))


def participant_tags(name: str, workshop: Tag) -> dict[str, str]:
    """The tags on a participant's instance, volume and Elastic IP (README.md, "The VMs"): every
    scope tag."""
    return {"Name": f"ws-{name}", **scope_tags(workshop), "participant": name}


def lookup_tags(name: str, workshop: Tag) -> dict[str, str]:
    """What existing resources must carry to count as this participant's in THIS workshop.

    Filtering on `participant` alone would adopt (or attach) another event's resources; the
    workshop's scope tags, all of them, scope every lookup the way creation and teardown are
    scoped.
    """
    return {**scope_tags(workshop), "participant": name}


def instance_params(row: dict[str, str], opts: argparse.Namespace) -> dict[str, Any]:
    """`run_instances` parameters for one participant (README.md, "The VMs")."""
    tags = participant_tags(slug_of(row), workshop_of(opts))
    params: dict[str, Any] = {
        "ImageId": opts.ami,
        "InstanceType": opts.instance_type,
        "UserData": render_user_data(row, getattr(opts, "admin_keys", None) or ()),
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                # 200 GB: a Lean project unpacks its own ~8 GB Mathlib build in each topic's
                # worktree, and a 100 GB VM filled up during the workshop (issue #205)
                "Ebs": {"VolumeSize": 200, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
        # explicit, although it is EBS's default: idle stop powers the VM off from inside, and
        # that must stop it, never terminate it (issue #207)
        "InstanceInitiatedShutdownBehavior": "stop",
        "SecurityGroupIds": [opts.security_group_id],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tag_list(tags)},
            {"ResourceType": "volume", "Tags": tag_list(tags)},
        ],
    }
    if getattr(opts, "protect", False):
        params["DisableApiTermination"] = True  # kept VMs: teardown.py skips them (protect.py)
    if opts.key_name:
        params["KeyName"] = opts.key_name
    if getattr(opts, "subnet_id", None):
        params["SubnetId"] = opts.subnet_id
    return params


def ensure_elastic_ip(
    ec2: Any,
    name: str,
    instance_id: str,
    *,
    workshop: Tag,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> str:
    """The participant's tagged Elastic IP on this instance, reconciled on every run.

    Reuses an address already associated with the instance, associates one that was allocated
    but never attached (a previous run died in between), or allocates a new one. Only addresses
    carrying this workshop's tag are considered. Returns the public IP, or "" in a dry run that
    would have to allocate.
    """
    addresses = _participants(ec2.find_addresses(lookup_tags(name, workshop)))
    attached = [a for a in addresses if a.get("InstanceId") == instance_id]
    if attached:
        return str(attached[0]["PublicIp"])
    free = [a for a in addresses if not a.get("InstanceId")]
    if dry_run:
        what = f"associate {free[0]['AllocationId']}" if free else "allocate and associate"
        log(f"[{name}] would {what} an Elastic IP")
        return str(free[0]["PublicIp"]) if free else ""
    if free:
        allocation = free[0]["AllocationId"]
    else:
        allocation = ec2.allocate_address(tag_list(participant_tags(name, workshop)))
    return str(ec2.associate_address(allocation, instance_id))


@dataclass
class Summary:
    """What happened to each row, for the report at the end and the exit code."""

    launched: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    old_vms: dict[str, str] = field(default_factory=dict)  # slug -> previous slug whose VM stays
    legacy: dict[str, str] = field(default_factory=dict)  # slug -> exact-case tag it still has
    other_region: dict[str, str] = field(default_factory=dict)  # slug -> why it was left alone
    host_keys: dict[str, str] = field(default_factory=dict)  # spare or row -> what happened
    # existing VMs that did not get --admin-key (first boot only): name -> ip
    admin_key_missed: dict[str, str] = field(default_factory=dict)
    # name -> the address EC2 shows on its VM now (not in a dry run), which no other row keeps:
    # a participant's VM, or a spare launched in this run (#604)
    addresses: dict[str, str] = field(default_factory=dict)
    stale: dict[str, str] = field(default_factory=dict)  # row -> its ip, cleared, and why (#604)
    # an existing spare -> its address, which a row may hold rightly: ORGANIZERS §7.2's hand-over
    # by hand of a broken VM gives the row the spare's ip and leaves the VM tagged as the spare
    # (review of #606)
    spare_addresses: dict[str, str] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)  # row -> what to check, left alone
    # row -> the stale ip cleared, which the write clears only if the file still has it (Codex)
    cleared: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        lines = [
            f"launched {len(self.launched)}, existing {len(self.existing)}, "
            f"skipped {len(self.skipped)}, failed {len(self.failed)}"
        ]
        lines += [
            f"  {n}: the previous name {old!r} may still have a VM (none after a dry run); "
            f"check with teardown.py --only {old} --dry-run"
            for n, old in self.old_vms.items()
        ]
        lines += [
            f"  {n}: reused the VM tagged participant={tag} (launched before slugs; "
            f"teardown.py --only {tag})"
            for n, tag in self.legacy.items()
        ]
        lines += [f"  skipped {n}: {why}" for n, why in self.skipped.items()]
        lines += [f"  other region {n}: {why}" for n, why in self.other_region.items()]
        lines += [f"  host keys {n}: {what}" for n, what in self.host_keys.items()]
        lines += [f"  stale ip {n}: {why}" for n, why in self.stale.items()]
        lines += [f"  check {n}: {why}" for n, why in self.held.items()]
        if self.admin_key_missed:
            vms = ", ".join(f"{n} ({ip or '?'})" for n, ip in self.admin_key_missed.items())
            lines.append(
                f"  WARNING --admin-key applies at first boot only; these VMs existed and may not "
                f"have it (unless an earlier run gave it): {vms}. Append it by hand if needed: "
                "ssh ubuntu@<ip> "
                "'cat >> ~/.ssh/authorized_keys' < key.pub (or relaunch the VM)"
            )
        lines += [f"  FAILED {n}: {why}" for n, why in self.failed.items()]
        return "\n".join(lines)


def legacy_tags(row: dict[str, str], slug: str) -> list[str]:
    """Exact-case `participant` tags a pre-slug launch may have used for this slug."""
    out = []
    host = row.get("host", "")
    recorded = host[len("ws-") :] if host.startswith("ws-") else ""
    if recorded and recorded != slug and recorded.lower() == slug:
        out.append(recorded)
    login = row.get("github", "")
    if not row.get("slug_given") and login and login != slug and login not in out:
        out.append(login)
    return out


def launch(
    rows: list[dict[str, str]],
    ec2: Any,
    opts: argparse.Namespace,
    *,
    log: Callable[[str], None] = print,
    ssh_ready: Callable[[str], bool] | None = None,
    summary: Summary | None = None,
    spares: list[dict[str, str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict[str, str]]:
    """Launch the missing instances; returns the rows updated with host, ip and host_keys.

    `ssh_ready` defaults to `wait_for_ssh`, looked up at call time so that tests can patch the
    module attribute. Errors are caught per row and recorded in `summary`; the loop always runs
    to the end. The host keys of the VMs launched now are collected afterwards, in one pass
    (`collect_host_keys`), so that no VM waits for another's console.
    """
    ssh_ready = ssh_ready or wait_for_ssh
    summary = summary if summary is not None else Summary()
    workshop = workshop_of(opts)
    only = set(only_slugs(getattr(opts, "only", None)))
    pending: list[tuple[dict[str, str], str, bool]] = []
    for row in participants(rows) + list(spares or []):
        name = slug_of(row)
        if only and name not in only:
            continue
        try:
            instance_id = _launch_one(row, ec2, opts, workshop, summary, ssh_ready, log)
        except Exception as exc:  # one row must never take the others down
            summary.failed[name] = f"{type(exc).__name__}: {exc}"
            log(f"[{name}] ERROR: {type(exc).__name__}: {exc}")
            continue
        finally:  # a failure after the address is recorded too
            _forget_stale(rows, name, summary, log)
        if instance_id:
            pending.append((row, *instance_id))
    collect_host_keys(pending, ec2, summary, log=log, sleep=sleep, clock=clock)
    return rows


def _forget_stale(
    rows: list[dict[str, str]], name: str, summary: Summary, log: Callable[[str], None]
) -> None:
    """Clear the `ip` of every other row at the address EC2 now shows on `name`'s VM (#604): an
    Elastic IP released with a row's VM, given to this one, which that row kept. Its `host`
    stays. This also unsticks a roster that two rows share an address in already: a run of the
    row whose VM has it clears the other's. A row at an existing spare's address is only
    reported: a spare handed over by hand (ORGANIZERS §7.2, "A broken VM") keeps its spare's tag,
    and the row that holds its address is the participant's (review of #606)."""
    ip = summary.addresses.get(name, "")
    for other in stale_ip_rows(rows, name, ip):
        why = f"its ip {ip} is on {name}'s VM now (released with its own, given again): cleared"
        summary.stale[slug_of(other)] = why
        summary.cleared[slug_of(other)] = ip
        log(f"[{slug_of(other)}] {why}")
        other["ip"] = ""
    ip = summary.spare_addresses.get(name, "")
    for other in stale_ip_rows(rows, name, ip):
        why = (
            f"holds {name}'s address {ip}: a spare handed over by hand (ORGANIZERS §7.2, 'A "
            "broken VM'), or an address it kept when its own VM was torn down; left alone, check"
        )
        summary.held[slug_of(other)] = why
        log(f"[{slug_of(other)}] {why}")


#: How long the launch waits, after the last VM, for cloud-init's fingerprints on the consoles.
#: They are printed at the end of the first boot (after the user data, whose MCP steps may take
#: minutes), and the latest console output lags by up to a few minutes.
HOST_KEY_WAIT_S = 900.0


def collect_host_keys(
    pending: list[tuple[dict[str, str], str, bool]],
    ec2: Any,
    summary: Summary,
    *,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wait_s: float = HOST_KEY_WAIT_S,
) -> None:
    """Record each VM's host-key fingerprints from its console in the row's `host_keys`
    (PM on issue #36, Mon 07:10: `scan_hosts.py` then compares without Drew).

    `pending` holds (row, instance id, new): a VM launched in this run is polled until its block
    appears or `wait_s` has passed; an existing VM without recorded keys is read once (the block
    is printed once per instance, so after a stop/start it is gone). A spare is not in the
    roster: its fingerprints are printed for the hand-over (ORGANIZERS §7.2)."""
    deadline = clock() + wait_s
    waiting = list(pending)
    while waiting:
        still: list[tuple[dict[str, str], str, bool]] = []
        for row, instance_id, new in waiting:
            name = slug_of(row)
            try:
                found = console_host_keys(ec2.console_output(instance_id))
            except Exception as exc:  # a console read never fails the launch
                log(f"[{name}] console output: {type(exc).__name__}: {exc}")
                found = []
            if found:
                row["host_keys"] = " ".join(found)
                log(f"[{name}] host keys from the console: {row['host_keys']}")
                if is_spare(row):
                    summary.host_keys[name] = (
                        f"{row['host_keys']} (copy into host_keys at hand-over)"
                    )
            elif new:
                still.append((row, instance_id, new))
            else:
                summary.host_keys[name] = (
                    "not on the console (stopped since its first boot?); scan_hosts.py will ask "
                    "for a manual comparison"
                )
        waiting = still
        if waiting and clock() >= deadline:
            for row, _, _ in waiting:
                summary.host_keys[slug_of(row)] = (
                    f"not on the console after {wait_s:.0f} s; rerun launch.py later to record them"
                )
            return
        if waiting:
            sleep(15.0)


def _launch_one(
    row: dict[str, str],
    ec2: Any,
    opts: argparse.Namespace,
    workshop: Tag,
    summary: Summary,
    ssh_ready: Callable[[str], bool],
    log: Callable[[str], None],
) -> tuple[str, bool] | None:
    """Launch or reconcile one row; returns (instance id, launched now) when its host keys are
    still to be read from the console, else None."""
    name = slug_of(row)
    # `host` is `ws-<slug>` as of the run that wrote it: a different slug now (a slug edited in
    # the roster, or a corrected login in a roster whose slug comes from the login) leaves the
    # old VM running, to be torn down separately (PM follow-up on #53)
    old = row.get("host", "")[len("ws-") :] if row.get("host", "").startswith("ws-") else ""
    if old and old.lower() != name and not is_spare(row):  # same VM in another case: reused
        summary.old_vms[name] = old
        log(f"[{name}] the previous name {old!r} may still have a VM; teardown.py --only {old}")
    region = getattr(opts, "region", None)
    recorded = (row.get("region") or "").strip()
    if region and recorded and recorded != region:
        # launch.py sees instances only in --region: a row launched elsewhere would get a second
        # VM here (PM follow-up on #36, Sun 13:45). Left alone, not a failure.
        summary.other_region[name] = f"its VM is in {recorded}; rerun with --region {recorded}"
        log(f"[{name}] recorded in {recorded}, not {region}: left alone")
        return None
    existing = _participants(ec2.find_instances(lookup_tags(name, workshop), LIVE_STATES))
    tagged = name
    if not existing:
        # A roster from before the slug launched this VM tagged with the login as written
        # (`participant=AndrewVSutherland`); EC2 tag values are case-sensitive, so the lower-case
        # slug would miss it and launch a second VM (codex round 1 on #97). The tag is the
        # recorded `host` (`ws-<tag>`, kept across roster saves; codex round 2) or, on the
        # first run from such a roster, the login the slug was derived from.
        for legacy in legacy_tags(row, name):
            existing = _participants(ec2.find_instances(lookup_tags(legacy, workshop), LIVE_STATES))
            if existing:
                tagged = legacy
                summary.legacy[name] = legacy
                log(f"[{name}] reusing the VM tagged participant={legacy} (launched before slugs)")
                break
    if (
        region
        and not existing
        and not is_spare(row)
        and row.get("host", "").lower() == f"ws-{name}"
    ):
        # The roster says ws-<slug> exists (`host`), but it is not in this region: launched
        # elsewhere, before the region column or under a region edited since (codex round 2 on
        # #103), or gone. Launching would duplicate a VM this lookup cannot see.
        summary.other_region[name] = (
            f"ws-{name} is recorded (host) but has no VM in {region}: if it is in another region, "
            "run launch.py there (and fix the region column); if it is gone, clear host and ip "
            "to launch a new one"
        )
        log(f"[{name}] host ws-{name} recorded, no VM in {region}: left alone")
        return None
    if existing:
        inst = existing[0]
        if is_spare(row):  # spares are not in the roster: `spares.json` needs the instance
            row["instance_id"] = str(inst["InstanceId"])
        row["host"] = f"ws-{tagged}"
        if region:
            row["region"] = region
        ip = ensure_elastic_ip(
            ec2, tagged, inst["InstanceId"], workshop=workshop, dry_run=opts.dry_run, log=log
        )
        row["ip"] = ip or row.get("ip", "")
        if ip and not opts.dry_run:  # in a dry run, a free address isn't associated yet
            # an existing spare's address may be a row's rightly (`_forget_stale`)
            (summary.spare_addresses if is_spare(row) else summary.addresses)[name] = ip
        summary.existing.append(name)
        log(
            f"[{name}] exists: {inst['InstanceId']} ({inst['State']['Name']}), "
            f"ip {ip or '?'}; not launching"
        )
        if getattr(opts, "admin_keys", None):
            summary.admin_key_missed[name] = row.get("ip", "")  # keys change at first boot only
        if row.get("host_keys") and not is_spare(row):
            return None
        return (str(inst["InstanceId"]), False)
    params = instance_params(row, opts)
    if opts.dry_run:
        log(
            f"[{name}] would run_instances {params['ImageId']} {params['InstanceType']} "
            f"tags={[t['Value'] for t in params['TagSpecifications'][0]['Tags']]}"
        )
        log(f"[{name}] would allocate and associate an Elastic IP, then wait for SSH")
        # nothing is recorded for a VM that does not exist: a saved `host` would make the next
        # real run take the row for one launched elsewhere (codex round 1 on #103)
        summary.launched.append(name)
        return None
    instance_id = ec2.run_instance(**params)
    if is_spare(row):
        row["instance_id"] = instance_id
    row["host_keys"] = ""  # a previous VM's keys must never vouch for this one
    if region:
        row["region"] = region  # recorded at once: a later failure must not hide the VM's region
    log(f"[{name}] launched {instance_id}; waiting for it to run")
    ec2.wait_running(instance_id)
    ip = ensure_elastic_ip(ec2, name, instance_id, workshop=workshop, log=log)
    row["host"] = f"ws-{name}"
    row["ip"] = ip
    summary.addresses[name] = ip
    summary.launched.append(name)
    log(f"[{name}] {ip}; waiting for SSH")
    log(f"[{name}] ssh {'ready' if ssh_ready(ip) else 'NOT reachable yet; check later'}")
    return (instance_id, True)


def _participants(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never the admin server, even tagged as a participant by mistake (docs/ADMIN.md §3.2)."""
    return [i for i in instances if has_participant(i)]


def check_security_group(
    ec2: Any,
    group_id: str,
    log: Callable[[str], None] = print,
    *,
    ssh_from: tuple[str, ...] | list[str] = (),
) -> None:
    """Refuse the VPC default group and anything but TCP 22 inbound (README.md), and with `ssh_from`
    any SSH source but those (`--ssh-from`, issue #263); raises ValueError."""
    sg = ec2.describe_security_group(group_id)
    if sg is None:  # dry run without boto3: nothing to check against
        log(f"security group {group_id}: not checked (no AWS access in this dry run)")
        return
    validate_security_group(sg, ssh_from)
    sources = f" from {', '.join(ssh_from)} only" if ssh_from else ""
    log(f"security group {group_id} ({sg.get('GroupName')}): inbound TCP 22{sources} only, OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch workshop VMs from the roster.")
    parser.add_argument("--roster", required=True)
    parser.add_argument("--ami", required=True)
    parser.add_argument(
        "--security-group-id",
        required=True,
        help="a group with inbound TCP 22 only (never the VPC default; see README)",
    )
    parser.add_argument("--region", required=True, help="the workshop's region")
    parser.add_argument("--instance-type", default=INSTANCE_TYPE)
    parser.add_argument("--key-name", help="EC2 key pair for the ubuntu user (admin access)")
    parser.add_argument("--subnet-id")
    parser.add_argument(
        "--spares", type=int, default=0, metavar="N", help="extra VMs zab-spare-1..N"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SLUG",
        help="one participant, or a spare zab-spare-N within --spares (repeatable)",
    )
    parser.add_argument(
        "--spares-file",
        metavar="FILE",
        help=f"where the spares' addresses and host keys are recorded (default {SPARES_FILE})",
    )
    parser.add_argument(
        "--admin-key",
        action="append",
        default=[],
        metavar="FILE",
        help="an OpenSSH public key file whose key is added for ubuntu at first boot, next to "
        "the key pair's (repeatable; participant VMs and spares)",
    )
    add_tag_argument(parser)
    parser.add_argument(
        "--ssh-from",
        action="append",
        default=[],
        metavar="CIDR",
        help="refuse a security group whose TCP 22 admits any other source (repeatable; "
        "the test VM: the development machine's public IP, issue #263)",
    )
    parser.add_argument(
        "--protect",
        action="store_true",
        help="termination protection on the new instances (kept VMs, issue #207; protect.py "
        "--off lifts it)",
    )
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv)
    try:
        scope = parse_tags(opts.tag)
        opts.ssh_from = [parse_cidr(c) for c in opts.ssh_from]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"scope: {scope_text(scope)}")
    if opts.spares < 0:
        print("--spares must be 0 or more", file=sys.stderr)
        return 1
    try:
        opts.admin_keys = [parse_public_key(_read(path), path) for path in opts.admin_key]
    except (OSError, ValueError) as exc:
        print(f"--admin-key: {exc}", file=sys.stderr)
        return 1
    try:
        rows = read_roster(opts.roster)  # refuses bad or reserved slugs before any AWS call
    except ValueError as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 1
    if not participants(rows) and not opts.spares:
        print("empty roster", file=sys.stderr)
        return 1
    spares = spare_rows(opts.spares)
    known = {slug_of(row) for row in participants(rows) + spares}
    unknown = [slug for slug in only_slugs(opts.only) if slug not in known]
    if unknown:
        print(
            f"--only {', '.join(unknown)}: no such roster row or spare (spares need --spares N)",
            file=sys.stderr,
        )
        return 1
    spares_file = pathlib.Path(opts.spares_file) if opts.spares_file else SPARES_FILE
    ec2 = _DryRunEc2(opts.region) if opts.dry_run else Ec2(opts.region)
    try:
        check_security_group(ec2, opts.security_group_id, ssh_from=opts.ssh_from)
    except ValueError as exc:
        print(f"refusing to launch: {exc}", file=sys.stderr)
        return 1
    summary = Summary()
    before = column_snapshot(rows, OWN_COLUMNS)
    saved = False
    try:
        launch(rows, ec2, opts, summary=summary, spares=spares)
    finally:
        # partial progress too; only these columns, into the roster as it is now: a launch takes
        # 20 to 40 minutes, and a `provision_repos --invites-only` loop or a spare hand-over may
        # write the roster meanwhile (PM on issue #36, Mon 22:30)
        # a stale ip only while the file still has it: a spare handed over meanwhile stays
        expect = {slug: {"ip": ip} for slug, ip in summary.cleared.items()}
        saved = save_roster_changes(opts.roster, rows, before, script="launch.py", expect=expect)
        if not opts.dry_run:
            names = record_spares(spares, spares_file, opts.region, scope_label(scope))
            if names:
                print(f"spares {', '.join(names)}: address and host keys in {spares_file}")
    print("\n# ~/.ssh/config")
    for row in participants(rows) + spares:
        if row.get("ip"):
            print(ssh_config_snippet(row))
    print(summary.text())
    if not saved:
        # again, last: the first warning is some 200 lines up, above a summary that may well say
        # "failed 0" (PM on issue #36, Tue 09:15)
        print(
            f"\nroster NOT written (see the changes printed above, after the launch): fix "
            f"{opts.roster} and rerun launch.py with the same flags",
            file=sys.stderr,
        )
    return 1 if summary.failed or not saved else 0


#: The roster columns launch.py writes (workshop/admin/README.md §4): only their changes are
#: written back.
OWN_COLUMNS = ("host", "ip", "region", "host_keys")


#: The spares' addresses and host-key fingerprints, kept for a hand-over days later (ORGANIZERS
#: §7.2; PM nit on #144): spares are not in the roster, and Wednesday's terminal output is gone
#: by Friday. Keyed by `<workshop tag value>/<region>/<name>` (codex on #154: `--spares 1` in two
#: regions makes two `zab-spare-1`; with several `--tag`s, `aws_common.scope_label` names the
#: scope): `{"nt-agents-2026/us-west-2/zab-spare-1": {"name", "workshop", "region", "instance_id",
#: "ip", "host_keys": "SHA256:… SHA256:…", "updated"}}`, in the git-ignored `workshop/admin/out/`
#: next to the bots' keys and `known_hosts`.
SPARES_FILE = pathlib.Path(__file__).resolve().parents[1] / "admin" / "out" / "spares.json"


def record_spares(
    spares: list[dict[str, str]],
    path: pathlib.Path,
    region: str,
    workshop: str,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Merge every spare with an address into `path` (0600, written atomically); the keys.

    For the same instance, an entry keeps its other fields (#611 in #607): `adopted_by`, the
    mark of a spare handed over by hand (ORGANIZERS §7.2), and its host keys once its console no
    longer shows them; a new instance gets a new entry. Another entry of the scope and region at
    an address recorded here, with another instance, is dropped (#607, as #606 clears a roster
    row's): EC2 gave that address to this spare once it was released with that one's VM, which
    `fleet.read_spares` would then offer as a second spare at one address."""
    known = [s for s in spares if s.get("ip")]
    if not known:
        return []
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    keys = []
    for spare in known:
        key = f"{workshop}/{region}/{slug_of(spare)}"
        keys.append(key)
        old = data.get(key) if isinstance(data.get(key), dict) else {}
        # a spare whose console no longer shows the block keeps what an earlier run read, but
        # only for the same instance: a replacement can reuse the tagged Elastic IP, and its
        # keys are never the old VM's (codex on #154)
        same_vm = bool(spare.get("instance_id")) and old.get("instance_id") == spare["instance_id"]
        data[key] = {
            **(old if same_vm else {}),
            "name": slug_of(spare),
            "workshop": workshop,
            "region": region,
            "instance_id": spare.get("instance_id", ""),
            "ip": spare["ip"],
            "host_keys": spare.get("host_keys") or (old.get("host_keys", "") if same_vm else ""),
            "updated": now,
        }
    for key in keys:
        entry, prefix = data[key], key.rsplit("/", 1)[0] + "/"
        for other in [k for k, e in data.items() if k != key and k.startswith(prefix)]:
            was = data[other] if isinstance(data[other], dict) else {}
            if was.get("ip") == entry["ip"] and was.get("instance_id") != entry["instance_id"]:
                log(f"spares.json: {other} dropped: its address {entry['ip']} is {key}'s now")
                del data[other]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_spares(path, data)
    return keys


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read(64 * 1024)


class _DryRunEc2:
    """Read-only: answers queries through boto3 (so idempotency is visible) and never mutates."""

    def __init__(self, region: str) -> None:
        try:
            self._real: Any = Ec2(region)
        except ImportError:
            self._real = None

    def find_instances(self, tags: dict[str, str], states: tuple[str, ...]) -> list[dict[str, Any]]:
        return self._real.find_instances(tags, states) if self._real else []

    def find_addresses(self, tags: dict[str, str]) -> list[dict[str, Any]]:
        return self._real.find_addresses(tags) if self._real else []

    def describe_security_group(self, group_id: str) -> dict[str, Any] | None:
        return self._real.describe_security_group(group_id) if self._real else None

    def console_output(self, instance_id: str) -> str:
        return self._real.console_output(instance_id) if self._real else ""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry run must not call {name}")


if __name__ == "__main__":
    raise SystemExit(main())
