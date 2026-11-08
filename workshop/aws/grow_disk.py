#!/usr/bin/env python3
"""Grow a participant's root volume, then its file system (issue #553; docs/ADMIN.md §3).

    uv run --with boto3 python workshop/aws/grow_disk.py --roster roster.csv --slug SLUG \\
        --size GB --tag KEY=VALUE [--tag …] --region R [--known-hosts FILE] [--dry-run]

Backs the admin bot's `!grow-disk SLUG NGB`; a deployer may run it by hand (ORGANIZERS §7.1).
`--size` is the volume's new size in GB, as EBS counts them (GiB).

**What it checks,** at the dry run and again at the apply, before anything changes. Any failed
check is one `refusing: …` line and exit 1:
- the row is the roster's, not a spare, with an `ip`;
- EC2 has exactly one live instance with every scope tag and `participant=SLUG`, and no
  `zab-admin` tag (`has_participant`, docs/ADMIN.md §3.2); it is `running` or `stopped`, and
  the row's `ip` is one of its addresses, so that SSH reaches this VM and no other;
- its root device is an EBS volume attached to it, which carries every scope tag: the admin
  server's role may modify only such a volume (ADMIN §5, the `Volume` statement);
- the new size is larger than the volume's (EBS volumes only grow), and at most `MAX_GB`; or
  equal to it on a running VM, for the file system alone (below);
- the volume's last modification started more than 6 hours ago and is no longer `modifying`:
  EBS allows one modification per volume every 6 hours (`DescribeVolumesModifications`). That is
  the limit as EBS documented it when this was written; should EC2 apply another, its refusal of
  `ModifyVolume` fails the apply cleanly, with its error and nothing changed. The file system
  alone needs only the second half: a modification no longer `modifying`, whatever its age.

**The file system alone** (#563): a size equal to the volume's, on a running VM, finishes a grow
whose volume grew and whose file system didn't (its SSH step failed or timed out), without
waiting out EBS's 6 hours or stopping the VM. No `ModifyVolume`, so no cost: the apply runs
`GROW` alone, which changes nothing where the partition and the file system fill the volume
already, and says so (`growpart … unchanged`, `… fills it already: nothing to resize`). On a
stopped VM the same size is refused, as any size that doesn't grow: its next boot grows it.

**The dry run** prints the volume (its ID, type and size, the new size) and what the added
size costs a month, at gp3's `GP3_GB_MONTH` (`zab.admin.costs`'s; a test keeps them equal);
then, on a running VM, the root file system's use, from `df -PkT /` over SSH (a VM whose SSH
doesn't answer is said, not refused: its volume can still grow, and its file system at its
next boot).

**The apply:**
1. `ModifyVolume` to the new size;
2. waits, every `POLL_S`, for the modification to reach `optimizing` or `completed`, when the
   VM sees the new size, for `MODIFY_WAIT_S` at most; `failed`, or the wait running out, is
   exit 1;
3. reads the VM's state again. **Running:** over SSH as `ubuntu`, with sudo, the fixed script
   `GROW`, the size its one argument (CONTRIBUTING.md, rule 3): it waits for the disk to show the
   new size, then `growpart` grows the root partition and `resize2fs` (ext4) or `xfs_growfs` (xfs)
   its file system, online; it prints `df -PkT /` before and after. **Stopped:** nothing more.
   Its file system grows at its next boot: the workshop image is Ubuntu's cloud image, whose
   cloud-init runs `growpart` and `resizefs` at every boot, which is how the image's 100 GB
   root fills `launch.py`'s 200 GB volume. The apply doesn't start it: a start costs, and the
   waker or `!start` boots it when it is needed;
4. reports the root file system's new size, or why it didn't grow. The volume has grown by
   then: a failed SSH step is exit 1, and the file system grows at the next boot (`!stop`, then
   `!start`), or now with the same size again, the file system alone (`finish`).

SSH is `admin_common`'s: the host key pinned in `--known-hosts`, no forwarding.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "admin"))

from admin_common import DEFAULT_KNOWN_HOSTS, Runner, RunResult, ssh_argv
from aws_common import (
    Ec2,
    Scope,
    add_tag_argument,
    error_code,
    error_line,
    has_participant,
    has_tags,
    instance_ips,
    is_spare_name,
    parse_tags,
    read_roster,
    scope_tags,
    scope_text,
    slug_of,
    tags_of,
)

#: The largest size the script grows a volume to, in GB: a constant, as the bot's own cap
#: (`zab.admin.grammar.MAX_DISK_GB`, kept equal by a test). It is not in admin.toml: that is a
#: change of the bot's loader, `config.py`, which is privileged (DECISIONS D32).
MAX_GB = 1000
#: gp3's price, USD per GB-month (`zab.admin.costs.GP3_GB_MONTH`, kept equal by a test).
GP3_GB_MONTH = 0.08
#: EBS's limit, as documented when this was written: one modification of a volume every 6
#: hours. Should EC2 apply another, its refusal fails the apply cleanly (module doc).
MODIFY_EVERY_S = 6 * 3600
#: How long the apply waits for the modification to reach `optimizing`, and how often it reads
#: it. With the SSH step, the whole stays under the admin bot's 15 minutes (ADMIN §2.4).
MODIFY_WAIT_S = 240.0
POLL_S = 5.0
#: Each SSH command's own limit: the read of `df`, and `GROW`, which waits up to 60 s for the
#: disk to show its new size.
READ_S = 30.0
GROW_S = 150.0
LIVE = ("pending", "running", "stopping", "stopped")
#: botocore's limits for each call: two tries, 10 s to connect and 20 s to read (power.py's).
EC2_CONFIG = {"connect_timeout": 10, "read_timeout": 20, "retries": {"total_max_attempts": 2}}
#: The root file system's use, in KiB (`df -P`), with its type.
DF = "LC_ALL=C df -PkT /"
#: Run on the VM as `ubuntu` (sudo), with the new size in GB as `$1`. A fixed literal: nothing
#: else of the roster, the command or EC2 reaches it. The root partition and its disk are found
#: from the root file system's device numbers in sysfs, so that `/dev/root` works too. (`sys`
#: is `/sys`; the tests put a fake tree there.) `growpart` makes its temporary directory under
#: `$TMPDIR`, `/tmp` by default, on the root file system that is full: it gets `/run`, a tmpfs
#: only root writes (Codex round 1 on #557; AWS's own advice for a full root is a tmpfs `/tmp`).
GROW = r"""set -u
sys=/sys
case ${1:-} in ''|*[!0-9]*) echo "grow: the size is not a number"; exit 2 ;; esac
want=$(( $1 * 1073741824 ))
mm=$(findmnt -no MAJ:MIN / | tr -d ' ')
part=$(readlink -f "$sys/dev/block/$mm")
if [ -z "$mm" ] || [ ! -r "$part/partition" ]; then
  echo "grow: the root file system is not on a disk partition"; exit 3
fi
disk=$(dirname "$part"); num=$(cat "$part/partition")
dev=/dev/$(basename "$part"); whole=/dev/$(basename "$disk")
fstype=$(findmnt -no FSTYPE / | tr -d ' ')
echo "before: $(LC_ALL=C df -PkT / | tail -n 1)"
if ! command -v growpart >/dev/null; then
  echo "grow: no growpart on the VM (cloud-guest-utils)"; exit 4
fi
waited=0
while [ $(( $(cat "$disk/size") * 512 )) -lt "$want" ]; do
  if [ "$waited" -ge 60 ]; then
    echo "grow: $whole still shows $(( $(cat "$disk/size") / 2097152 )) GB after 60 s"; exit 5
  fi
  sleep 2; waited=$(( waited + 2 ))
done
out=$(sudo -n env TMPDIR=/run growpart "$whole" "$num" 2>&1); rc=$?
if [ "$rc" -gt 1 ]; then
  echo "grow: growpart $whole $num failed ($rc): $(printf '%s\n' "$out" | tail -n 1)"; exit 6
fi
echo "grow: growpart $whole $num: $([ "$rc" -eq 0 ] && echo grown || echo unchanged)"
gap=$(( $(cat "$disk/size") - $(cat "$part/start") - $(cat "$part/size") ))
if [ "$gap" -gt 131072 ]; then
  echo "grow: $dev ends $(( gap / 2048 )) MB before the end of $whole"; exit 7
fi
case $fstype in
  ext2|ext3|ext4) out=$(sudo -n env LC_ALL=C resize2fs "$dev" 2>&1); rc=$? ;;
  xfs) out=$(sudo -n env LC_ALL=C xfs_growfs / 2>&1); rc=$? ;;
  *) echo "grow: the root file system is $fstype, neither ext4 nor xfs"; exit 8 ;;
esac
if [ "$rc" -ne 0 ]; then
  echo "grow: resizing $fstype failed ($rc): $(printf '%s\n' "$out" | tail -n 1)"; exit 9
fi
grown=yes  # unless the tool says the file system fills its partition already
case $fstype in
  xfs) case $out in *"data blocks changed"*) ;; *) grown= ;; esac ;;
  *) case $out in *"Nothing to do"*) grown= ;; esac ;;
esac
if [ -n "$grown" ]; then
  echo "grow: $fstype on $dev resized"
else
  echo "grow: $fstype on $dev fills it already: nothing to resize"
fi
echo "after: $(LC_ALL=C df -PkT / | tail -n 1)"
"""


def run_keeping(
    argv: list[str],
    *,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> RunResult:
    """`admin_common.subprocess_runner`, except that a timeout keeps what the command printed
    by then: `GROW`'s lines say how far it got (the review of #557)."""
    try:
        proc = subprocess.run(  # noqa: S603 - an argument list from this script, no shell
            argv,
            env=dict(env) if env is not None else None,
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""  # bytes, even in text mode, from some Python versions
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        return RunResult(-1, out, f"timed out after {timeout_s:g} s: {argv[0]}")
    except OSError as exc:
        return RunResult(-1, "", f"cannot run {argv[0]}: {exc}")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


class GrowError(Exception):
    """A check failed: nothing was changed."""


@dataclass(frozen=True, slots=True)
class Target:
    """What the checks found: the row's VM and its root volume."""

    slug: str
    ip: str
    instance: str
    state: str
    volume: str
    kind: str  # the volume's type: gp3
    size: int  # GB, now
    new: int  # GB, asked for

    @property
    def monthly(self) -> float:
        """USD a month the added size costs, at gp3's price."""
        return (self.new - self.size) * GP3_GB_MONTH

    @property
    def fs_only(self) -> bool:
        """The volume's own size: its file system alone grows (#563)."""
        return self.new == self.size


def find(
    rows: list[dict[str, str]],
    ec2: Any,
    *,
    slug: str,
    size: int,
    scope: Scope,
    now: dt.datetime,
) -> Target:
    """Every check of the module doc, in order (raises GrowError); the row's VM and volume."""
    tags = scope_tags(scope)
    if is_spare_name(slug):
        raise GrowError(f"{slug} is a spare: only a participant's row grows")
    row = next((r for r in rows if slug_of(r) == slug), None)
    if row is None:
        raise GrowError(f"no row {slug} in the roster")
    ip = (row.get("ip") or "").strip()
    if not ip:
        raise GrowError(f"{slug} has no ip in the roster: it has no VM")
    if not 1 <= size <= MAX_GB:
        raise GrowError(f"{size} GB is outside 1 to {MAX_GB} GB")
    found = [
        i for i in ec2.find_instances({**tags, "participant": slug}, LIVE) if has_participant(i)
    ]
    if len(found) != 1:
        ids = ", ".join(i["InstanceId"] for i in found) or "none"
        raise GrowError(
            f"EC2 has {ids} with {scope_text(tags)} and participant={slug}: expected one"
        )
    inst = found[0]
    iid, state = inst["InstanceId"], (inst.get("State") or {}).get("Name") or "?"
    if state not in ("running", "stopped"):
        raise GrowError(
            f"{slug}'s VM {iid} is {state}: request again once it is running or stopped"
        )
    if ip not in instance_ips(inst):
        raise GrowError(f"the roster's ip for {slug}, {ip}, is not its VM {iid}'s address")
    root = inst.get("RootDeviceName")
    mapping = next(
        (m for m in inst.get("BlockDeviceMappings") or [] if m.get("DeviceName") == root), None
    )
    vid = ((mapping or {}).get("Ebs") or {}).get("VolumeId")
    if not vid:
        raise GrowError(f"{slug}'s VM {iid} has no EBS root volume ({root or 'no root device'})")
    volumes = [v for v in ec2.volumes([vid]) if v.get("VolumeId") == vid]
    if len(volumes) != 1:
        raise GrowError(f"EC2 doesn't describe {slug}'s root volume {vid}")
    vol = volumes[0]
    if iid not in {a.get("InstanceId") for a in vol.get("Attachments") or []}:
        raise GrowError(f"{vid} is not attached to {slug}'s VM {iid}")
    if not has_tags(vol, tags):
        have = tags_of(vol)
        missing = ", ".join(f"{k}={v}" for k, v in tags.items() if have.get(k) != v)
        raise GrowError(
            f"{slug}'s volume {vid} lacks {missing}: the admin server's role may grow only a "
            "volume in its scope (docs/ADMIN.md §5); the account owner tags it"
        )
    current = int(vol.get("Size") or 0)
    same = size == current  # the file system alone, on a running VM (#563)
    fs_only = same and state == "running"
    if size <= current and not fs_only:
        boot = "; a stopped VM's file system grows to it at its next boot" if same else ""
        raise GrowError(
            f"{slug}'s volume {vid} is {current} GB already: ask for more (EBS only grows){boot}"
        )
    last = latest(ec2.volume_modifications(vid))
    if last is not None:
        started, mstate = last.get("StartTime"), last.get("ModificationState") or "?"
        if not isinstance(started, dt.datetime):
            raise GrowError(f"{vid}'s last modification has no start time")
        again = started + dt.timedelta(seconds=MODIFY_EVERY_S)
        if now < again and not fs_only:
            raise GrowError(
                f"{vid} was modified at {utc(started)} ({mstate}), and EBS allows one "
                f"modification every 6 hours: request again from {utc(again)}"
            )
        if mstate == "modifying":  # the VM may not see the size yet
            raise GrowError(
                f"{vid}'s modification of {utc(started)} is still modifying: request again "
                "once it is optimizing"
            )
    return Target(slug, ip, iid, state, vid, str(vol.get("VolumeType") or "?"), current, size)


def latest(modifications: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The modification that started last (EC2 lists only a volume's latest), or one without a
    start time, which `find` refuses."""
    for m in modifications:
        if not isinstance(m.get("StartTime"), dt.datetime):
            return m
    return max(modifications, key=lambda m: m["StartTime"], default=None)


def utc(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def volume_line(t: Target) -> str:
    if t.fs_only:
        return (
            f"{t.slug} {t.instance} ({t.state}): volume {t.volume} {t.kind} already {t.size} GB: "
            "its file system alone, no ModifyVolume"
        )
    priced = "" if t.kind == "gp3" else f", priced as gp3 (it is {t.kind})"
    return (
        f"{t.slug} {t.instance} ({t.state}): volume {t.volume} {t.kind} {t.size} GB → {t.new} GB, "
        f"+{t.new - t.size} GB: about ${t.monthly:,.2f} a month more, at ${GP3_GB_MONTH} per "
        f"GB-month{priced}"
    )


def finish(t: Target) -> str:
    """How an organizer finishes a grow whose file system didn't grow: the VM's next boot, or,
    while it runs, the same size again, the file system alone (#563)."""
    return (
        f"its file system grows at its next boot (!stop {t.slug}, then !start {t.slug}), or now, "
        f"while the VM runs, with !grow-disk {t.slug} {t.new}GB again: the file system alone, "
        "at no cost"
    )


def df_line(line: str) -> str:
    """`df -PkT /`'s data line, readable: `194G, 150G used (78%), 44G free (ext4, /dev/root)`."""
    parts = line.split()
    if len(parts) < 7 or not all(p.isdigit() for p in parts[2:5]):
        return f"unreadable df line: {line.strip()[:200]!r}"
    device, fstype, size, used, free, share = parts[:6]
    return (
        f"{gib(size)}, {gib(used)} used ({share}), {gib(free)} free ({fstype}, {device}) "
        f"[df -PkT /: {' '.join(parts)}]"
    )


def gib(kib: str) -> str:
    return f"{int(kib) / 2**20:.0f}G"


def read_df(t: Target, runner: Runner, known_hosts: str) -> str:
    """The root file system's use now, or why it wasn't read."""
    res = runner(ssh_argv(t.ip, DF, known_hosts=known_hosts), timeout_s=READ_S)
    lines = [line for line in res.stdout.splitlines() if line.strip()]
    if not res.ok or not lines:
        why = (res.stderr.strip().splitlines() or [f"exit {res.exit_code}"])[-1][:200]
        return f"not read over SSH ({why})"
    return df_line(lines[-1])


def wait_modified(
    ec2: Any,
    t: Target,
    *,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Until the modification to `t.new` reaches `optimizing` or `completed` (raises GrowError
    when it fails, or after `MODIFY_WAIT_S`)."""
    start = clock()
    state = "?"
    while True:
        found = [m for m in ec2.volume_modifications(t.volume) if m.get("TargetSize") == t.new]
        state = (latest(found) or {}).get("ModificationState") or "not listed yet"
        if state in ("optimizing", "completed"):
            took = clock() - start
            log(f"{t.slug}: {t.volume} is {state} after {took:.0f} s: the VM sees {t.new} GB")
            return
        if state == "failed":
            why = (latest(found) or {}).get("StatusMessage") or "no reason given"
            raise GrowError(f"EC2's modification of {t.volume} failed: {why}")
        if clock() - start >= MODIFY_WAIT_S:
            raise GrowError(
                f"{t.volume} is still {state} after {MODIFY_WAIT_S:.0f} s: EC2 may finish it "
                f"later; then {finish(t)}"
            )
        sleep(POLL_S)


def grow_fs(t: Target, runner: Runner, known_hosts: str, log: Callable[[str], None]) -> bool:
    """`GROW` on the VM; True when its root file system grew to fill the volume."""
    remote = shlex.join(["bash", "-c", GROW, "bash", str(t.new)])
    res = runner(ssh_argv(t.ip, remote, known_hosts=known_hosts), timeout_s=GROW_S)
    for line in res.stdout.splitlines():
        label, _, rest = line.partition(": ")
        if label in ("before", "after"):
            when = "now" if label == "after" else label
            log(f"{t.slug}: root file system {when}: {df_line(rest)}")
        elif label == "grow":
            log(f"{t.slug}: {rest}")
    if not res.ok:
        why = (res.stderr.strip().splitlines() or [f"exit {res.exit_code}"])[-1][:200]
        if res.exit_code == -1:  # a timeout: it may have grown, the lines above say how far
            log(f"{t.slug}: FAILED: the grow over SSH didn't finish ({why})")
        else:
            log(f"{t.slug}: FAILED: its file system was not grown ({why})")
    return res.ok


def run(
    rows: list[dict[str, str]],
    ec2: Any,
    *,
    slug: str,
    size: int,
    scope: Scope,
    dry_run: bool,
    runner: Runner = run_keeping,
    known_hosts: str = str(DEFAULT_KNOWN_HOSTS),
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] | None = None,  # time.sleep, looked up at the call
    clock: Callable[[], float] | None = None,  # time.monotonic, likewise
    now: Callable[[], dt.datetime] | None = None,
) -> int:
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    when = (now or (lambda: dt.datetime.now(dt.UTC)))()
    try:
        t = find(rows, ec2, slug=slug, size=size, scope=scope, now=when)
    except GrowError as exc:
        log(f"refusing: {exc}")
        return 1
    except Exception as exc:  # botocore: an expired credential, UnauthorizedOperation
        log(f"refusing: the EC2 lookup failed: {error_line(exc)}")
        return 1
    log(volume_line(t))
    if t.state == "running":
        log(f"{t.slug}: root file system now: {read_df(t, runner, known_hosts)}")
        after = "then growpart and resize2fs over SSH, online"
    else:
        after = "its file system grows at its next boot (cloud-init)"
    if dry_run:
        if t.fs_only:
            log(f"{t.slug}: on apply: no ModifyVolume; would grow its file system over SSH (no "
                "cost): growpart and resize2fs, online, which change nothing where it fills the "
                "volume already")  # fmt: skip
        else:
            log(f"{t.slug}: on apply: ModifyVolume, then wait for EC2's optimizing state; {after}")
        log("(dry run: nothing changed)")
        return 0
    if t.fs_only:  # the VM was running at `find`: no ModifyVolume, no wait (#563)
        if grow_fs(t, runner, known_hosts, log):
            return 0
        log(f"{t.slug}: the volume is {t.new} GB; {finish(t)}")
        return 1
    try:
        ec2.modify_volume(t.volume, t.new)
    except Exception as exc:
        code = error_code(exc)
        log(
            f"{t.slug}: FAILED: ModifyVolume of {t.volume} ({code or 'no code'}): {error_line(exc)}"
        )
        return 1
    log(f"{t.slug}: ModifyVolume {t.volume} {t.size} GB → {t.new} GB: requested")
    try:
        wait_modified(ec2, t, log=log, sleep=sleep, clock=clock)
        found = [i for i in ec2.find_instances({**scope_tags(scope), "participant": slug}, LIVE)
                 if i.get("InstanceId") == t.instance]  # fmt: skip
    except GrowError as exc:
        log(f"{t.slug}: FAILED: {exc}")
        return 1
    except Exception as exc:  # the modification was requested: the volume may have grown
        log(f"{t.slug}: FAILED: reading EC2 after ModifyVolume: {error_line(exc)}")
        log(f"{t.slug}: the volume is {t.new} GB, or will be; {finish(t)}")
        return 1
    state = (found[0].get("State") or {}).get("Name") if found else "gone"
    if state == "stopped":
        log(f"{t.slug}: the VM is stopped: its file system grows to {t.new} GB at its next boot")
        return 0
    if state != "running":
        log(f"{t.slug}: FAILED: the VM is {state} now: its file system grows at its next boot "
            f"(!stop {t.slug}, then !start {t.slug})")  # fmt: skip
        return 1
    if grow_fs(t, runner, known_hosts, log):
        return 0
    log(f"{t.slug}: the volume is {t.new} GB; {finish(t)}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grow_disk.py", description="Grow a participant's root volume and file system."
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--slug", required=True, help="the participant's row")
    parser.add_argument(
        "--size", required=True, type=int, help=f"the new size in GB, at most {MAX_GB}"
    )
    parser.add_argument("--region", required=True, help="the workshop's region")
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    add_tag_argument(parser)
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv)
    try:
        scope = parse_tags(opts.tag)
        rows = read_roster(opts.roster)
    except (OSError, ValueError) as exc:
        print(f"refusing: {exc}")
        return 1
    return run(
        rows,
        Ec2(opts.region, config=EC2_CONFIG),
        slug=opts.slug,
        size=opts.size,
        scope=scope,
        dry_run=opts.dry_run,
        runner=run_keeping,  # looked up here, so that a test can replace it
        known_hosts=opts.known_hosts,
    )


if __name__ == "__main__":
    raise SystemExit(main())
