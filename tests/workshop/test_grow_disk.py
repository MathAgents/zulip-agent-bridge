"""`workshop/aws/grow_disk.py` (issue #553): its checks, dry run and apply against a fake EC2 and
a fake SSH, and its remote script `GROW` against a fake sysfs and fake tools."""

from __future__ import annotations

import datetime as dt
import os
import shlex
import stat
import subprocess
from pathlib import Path

import aws_common as ac
import grow_disk as gd
import pytest
from admin_common import RunResult

from zab.admin import costs
from zab.admin.grammar import MAX_DISK_GB

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)
SCOPE = {"workshop": "nt-agents-2026", "zab-scope": "mathagents"}
IP = "198.51.100.7"
ROWS = [
    {"slug": "alice", "ip": IP},
    {"slug": "bob", "ip": ""},
]


def tags(**kw: str) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in kw.items()]


class Ec2:
    """The four calls grow_disk.py makes. A modification moves on one state per read after it
    was requested (`steps`)."""

    def __init__(self, *, state="running", size=200, vtags=None, mods=(), kind="gp3"):
        self.calls: list[tuple] = []
        self.instances = [
            {
                "InstanceId": "i-1",
                "State": {"Name": state},
                "PublicIpAddress": IP if state == "running" else None,
                "NetworkInterfaces": [{"Association": {"PublicIp": IP}}],
                "RootDeviceName": "/dev/sda1",
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-1"}}],
                "Tags": tags(Name="ws-alice", participant="alice", **SCOPE),
            }
        ]
        self.volume = {
            "VolumeId": "vol-1",
            "Size": size,
            "VolumeType": kind,
            "Attachments": [{"InstanceId": "i-1"}],
            "Tags": tags(**(SCOPE | {"participant": "alice"} if vtags is None else vtags)),
        }
        self.mods = [dict(m) for m in mods]
        self.steps = ["modifying", "optimizing"]
        self.refuse = False

    def find_instances(self, wanted, states):
        self.calls.append(("find_instances", dict(wanted), states))
        return [
            i
            for i in self.instances
            if all(ac.tags_of(i).get(k) == v for k, v in wanted.items())
            and i["State"]["Name"] in states
        ]

    def volumes(self, ids):
        self.calls.append(("volumes", list(ids)))
        return [self.volume] if self.volume["VolumeId"] in ids else []

    def volume_modifications(self, vid):
        self.calls.append(("volume_modifications", vid))
        if self.mods and self.mods[-1].get("ours") and self.steps:
            self.mods[-1]["ModificationState"] = self.steps.pop(0)
        return [{k: v for k, v in m.items() if k != "ours"} for m in self.mods]

    def modify_volume(self, vid, size):
        self.calls.append(("modify_volume", vid, size))
        if self.refuse:
            raise ClientError("VolumeModificationRateExceeded", "wait 6 hours")
        self.mods = [{"StartTime": NOW, "TargetSize": size, "ours": True}]
        self.volume["Size"] = size

    def changed(self) -> bool:
        return any(c[0] == "modify_volume" for c in self.calls)


class ClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


DF_BEFORE = "/dev/root ext4 203056560 160000000 43039944 79% /"
DF_AFTER = "/dev/root ext4 305323820 160000000 145307004 53% /"


class Ssh:
    """`admin_common`'s runner: the `df` read, and `GROW`, answered as the VM would."""

    def __init__(self, *, ok: bool = True, grow_ok: bool = True) -> None:
        self.argvs: list[list[str]] = []
        self.ok, self.grow_ok = ok, grow_ok

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.argvs.append(list(argv))
        if not self.ok:
            return RunResult(255, "", "ssh: connect to host 198.51.100.7 port 22: timed out\n")
        if argv[-1] == gd.DF:
            return RunResult(0, "Filesystem Type 1024-blocks Used Available Capacity On\n"
                             f"{DF_BEFORE}\n", "")  # fmt: skip
        if not self.grow_ok:
            return RunResult(6, f"before: {DF_BEFORE}\ngrow: growpart /dev/nvme0n1 1 failed\n", "")
        return RunResult(
            0,
            f"before: {DF_BEFORE}\ngrow: growpart /dev/nvme0n1 1: grown\n"
            f"grow: ext4 on /dev/nvme0n1p1 resized\nafter: {DF_AFTER}\n",
            "",
        )


class Clock:
    def __init__(self) -> None:
        self.now, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def grow(ec2, *, size=300, slug="alice", dry_run=False, ssh=None, rows=ROWS, now=NOW):
    out: list[str] = []
    clock = Clock()
    code = gd.run(
        rows, ec2, slug=slug, size=size, scope=SCOPE, dry_run=dry_run,
        runner=ssh or Ssh(), known_hosts="/kh", log=out.append, sleep=clock.sleep, clock=clock,
        now=lambda: now,
    )  # fmt: skip
    return code, out, clock


# ----------------------------------------------------------------------------------- dry run


def test_the_dry_run_of_a_running_vm_names_the_volume_the_cost_and_the_disk_use():
    ec2, ssh = Ec2(), Ssh()
    code, out, _ = grow(ec2, dry_run=True, ssh=ssh)
    assert code == 0 and not ec2.changed()
    assert out[0] == (
        "alice i-1 (running): volume vol-1 gp3 200 GB → 300 GB, +100 GB: about $8.00 a month "
        "more, at $0.08 per GB-month"
    )
    assert out[1] == (
        "alice: root file system now: 194G, 153G used (79%), 41G free (ext4, /dev/root) "
        f"[df -PkT /: {DF_BEFORE}]"
    )
    assert "growpart and resize2fs over SSH" in out[2] and out[-1] == "(dry run: nothing changed)"
    [argv] = ssh.argvs
    assert argv[0] == "ssh" and argv[-3:] == [f"ubuntu@{IP}", "--", gd.DF]
    assert 'UserKnownHostsFile="/kh"' in argv and "StrictHostKeyChecking=yes" in argv


def test_the_dry_run_of_a_stopped_vm_reads_no_disk():
    ec2, ssh = Ec2(state="stopped"), Ssh()
    code, out, _ = grow(ec2, dry_run=True, ssh=ssh)
    assert code == 0 and not ssh.argvs and not ec2.changed()
    assert out[0].startswith("alice i-1 (stopped): volume vol-1 gp3 200 GB → 300 GB")
    assert "its file system grows at its next boot (cloud-init)" in out[1]


def test_a_vm_whose_ssh_doesnt_answer_is_said_not_refused():
    code, out, _ = grow(Ec2(), dry_run=True, ssh=Ssh(ok=False))
    assert code == 0
    assert out[1].startswith("alice: root file system now: not read over SSH (ssh: connect")


def test_another_type_is_priced_as_gp3_and_says_so():
    _, out, _ = grow(Ec2(kind="gp2"), size=250, dry_run=True)
    assert out[0].endswith("about $4.00 a month more, at $0.08 per GB-month, priced as gp3 "
                           "(it is gp2)")  # fmt: skip


def test_the_price_is_the_cost_estimates():
    assert gd.GP3_GB_MONTH == costs.GP3_GB_MONTH


def test_the_cap_is_the_bots():
    """A size the grammar takes, the script takes (#553)."""
    assert gd.MAX_GB == MAX_DISK_GB == 1000


# ---------------------------------------------------------------------------------- refusals


def six_hours_ago(minutes: int, state: str = "completed") -> dict:
    return {"StartTime": NOW - dt.timedelta(hours=6) + dt.timedelta(minutes=minutes),
            "ModificationState": state, "TargetSize": 200}  # fmt: skip


def two_vms() -> Ec2:
    ec2 = Ec2()
    ec2.instances.append(dict(ec2.instances[0], InstanceId="i-2"))
    return ec2


def admin_server() -> Ec2:
    ec2 = Ec2()
    ec2.instances[0]["Tags"] += tags(**{"zab-admin": "x"})
    return ec2


def no_root() -> Ec2:
    ec2 = Ec2()
    ec2.instances[0]["BlockDeviceMappings"] = []
    return ec2


def detached() -> Ec2:
    ec2 = Ec2()
    ec2.volume["Attachments"] = [{"InstanceId": "i-9"}]
    return ec2


def elsewhere() -> Ec2:
    ec2 = Ec2()
    ec2.instances[0]["PublicIpAddress"] = "198.51.100.8"
    ec2.instances[0]["NetworkInterfaces"] = []
    return ec2


@pytest.mark.parametrize(
    ("ec2", "kw", "said"),
    [
        (Ec2(), {"slug": "zab-spare-1"}, "zab-spare-1 is a spare: only a participant's row grows"),
        (Ec2(), {"slug": "carol"}, "no row carol in the roster"),
        (Ec2(), {"slug": "bob"}, "bob has no ip in the roster: it has no VM"),
        (Ec2(), {"size": 1001}, "1001 GB is outside 1 to 1000 GB"),
        (Ec2(), {"size": 0}, "0 GB is outside 1 to 1000 GB"),
        (
            Ec2(),
            {"size": 199},
            "alice's volume vol-1 is 200 GB already: ask for more (EBS only grows)",
        ),
        # the same size: the file system alone, but only on a running VM (#563)
        (
            Ec2(state="stopped"),
            {"size": 200},
            "alice's volume vol-1 is 200 GB already: ask for more (EBS only grows); a stopped "
            "VM's file system grows to it at its next boot",
        ),
        (
            Ec2(mods=[six_hours_ago(300, "modifying")]),
            {"size": 200},
            "vol-1's modification of 2026-10-22 11:00 UTC is still modifying: request again once "
            "it is optimizing",
        ),
        (Ec2(size=300), {"size": 250}, "alice's volume vol-1 is 300 GB already"),
        (
            Ec2(state="pending"),
            {},
            "alice's VM i-1 is pending: request again once it is running or stopped",
        ),
        (Ec2(state="stopping"), {}, "alice's VM i-1 is stopping"),
        (
            two_vms(),
            {},
            "EC2 has i-1, i-2 with workshop=nt-agents-2026, zab-scope=mathagents and "
            "participant=alice: expected one",
        ),
        (admin_server(), {}, "EC2 has none with"),
        (elsewhere(), {}, "the roster's ip for alice, 198.51.100.7, is not its VM i-1's address"),
        (no_root(), {}, "alice's VM i-1 has no EBS root volume (/dev/sda1)"),
        (detached(), {}, "vol-1 is not attached to alice's VM i-1"),
        (
            Ec2(vtags={"workshop": "nt-agents-2026", "participant": "alice"}),
            {},
            "alice's volume vol-1 lacks zab-scope=mathagents: the admin server's role may grow "
            "only a volume in its scope (docs/ADMIN.md §5); the account owner tags it",
        ),
        (
            Ec2(mods=[six_hours_ago(1)]),
            {},
            "vol-1 was modified at 2026-10-22 06:01 UTC (completed), and EBS allows one "
            "modification every 6 hours: request again from 2026-10-22 12:01 UTC",
        ),
        (
            Ec2(mods=[six_hours_ago(-1, "modifying")]),
            {},
            "vol-1's modification of 2026-10-22 05:59 UTC is still modifying",
        ),
        (
            Ec2(mods=[{"ModificationState": "completed"}]),
            {},
            "vol-1's last modification has no start time",
        ),
    ],
)
def test_refusals_change_nothing(ec2, kw, said):
    for dry_run in (True, False):
        ssh = Ssh()
        code, out, _ = grow(ec2, dry_run=dry_run, ssh=ssh, **kw)
        assert code == 1 and len(out) == 1, out
        assert out[0].startswith(f"refusing: {said}"), out[0]
        assert not ec2.changed() and not ssh.argvs


def test_a_modification_six_hours_old_or_optimizing_is_no_bar():
    for mod in (six_hours_ago(0), six_hours_ago(-60, "optimizing")):
        code, out, _ = grow(Ec2(mods=[mod]), dry_run=True)
        assert code == 0, out


def test_a_failed_ec2_lookup_is_one_line():
    class Broken(Ec2):
        def find_instances(self, wanted, states):
            raise ClientError("UnauthorizedOperation", "not allowed")

    code, out, _ = grow(Broken(), dry_run=True)
    assert code == 1 and out == [
        "refusing: the EC2 lookup failed: ClientError: An error occurred (UnauthorizedOperation): "
        "not allowed"
    ]


# ------------------------------------------------------------------------------------- apply


def test_the_apply_on_a_running_vm_grows_the_volume_then_the_file_system():
    ec2, ssh = Ec2(), Ssh()
    code, out, clock = grow(ec2, ssh=ssh)
    assert code == 0, out
    assert ("modify_volume", "vol-1", 300) in ec2.calls
    assert clock.slept == [gd.POLL_S]  # read once `modifying`, then `optimizing`
    assert "alice: ModifyVolume vol-1 200 GB → 300 GB: requested" in out
    assert "alice: vol-1 is optimizing after 5 s: the VM sees 300 GB" in out
    assert out[-1] == (
        "alice: root file system now: 291G, 153G used (53%), 139G free (ext4, /dev/root) "
        f"[df -PkT /: {DF_AFTER}]"
    )
    assert "alice: growpart /dev/nvme0n1 1: grown" in out
    read, grown = ssh.argvs  # the dry part's read, then GROW
    assert read[-1] == gd.DF
    assert grown[-3:-1] == [f"ubuntu@{IP}", "--"]
    assert shlex.split(grown[-1]) == ["bash", "-c", gd.GROW, "bash", "300"]


def test_the_apply_on_a_stopped_vm_leaves_the_file_system_to_its_next_boot():
    ec2, ssh = Ec2(state="stopped"), Ssh()
    code, out, _ = grow(ec2, ssh=ssh)
    assert code == 0 and ("modify_volume", "vol-1", 300) in ec2.calls and not ssh.argvs
    assert out[-1] == "alice: the VM is stopped: its file system grows to 300 GB at its next boot"


def test_a_vm_stopped_meanwhile_is_left_to_its_next_boot():
    class Stops(Ec2):
        def modify_volume(self, vid, size):
            super().modify_volume(vid, size)
            self.instances[0]["State"]["Name"] = "stopped"

    ssh = Ssh()
    code, out, _ = grow(Stops(), ssh=ssh)
    assert code == 0 and len(ssh.argvs) == 1  # the dry part's read only
    assert out[-1].endswith("grows to 300 GB at its next boot")


#: How a grow whose file system didn't grow is finished: the next boot, or the same size again,
#: the file system alone (#563).
FINISH = (
    "its file system grows at its next boot (!stop alice, then !start alice), or now, while the "
    "VM runs, with !grow-disk alice 300GB again: the file system alone, at no cost"
)


def test_a_failed_file_system_step_says_the_volume_grew_and_how_to_finish():
    code, out, _ = grow(Ec2(), ssh=Ssh(grow_ok=False))
    assert code == 1
    assert "alice: growpart /dev/nvme0n1 1 failed" in out
    assert out[-2] == "alice: FAILED: its file system was not grown (exit 6)"
    assert out[-1] == f"alice: the volume is 300 GB; {FINISH}"


def test_a_grow_that_times_out_says_how_far_it_got_and_how_to_finish():
    """The review of #557, N2: `run_keeping` keeps what `GROW` printed before its timeout."""

    class Slow(Ssh):
        def __call__(self, argv, **kwargs):
            if argv[-1] == gd.DF:
                return super().__call__(argv, **kwargs)
            self.argvs.append(list(argv))
            said = f"before: {DF_BEFORE}\ngrow: growpart /dev/nvme0n1 1: grown\n"
            return RunResult(-1, said, f"timed out after {gd.GROW_S:g} s: ssh")

    code, out, _ = grow(Ec2(), ssh=Slow())
    assert code == 1
    assert out[-3:] == [
        "alice: growpart /dev/nvme0n1 1: grown",
        "alice: FAILED: the grow over SSH didn't finish (timed out after 150 s: ssh)",
        f"alice: the volume is 300 GB; {FINISH}",
    ]


def interrupted(state: str = "optimizing") -> Ec2:
    """A grow to 300 GB ten minutes ago, within EBS's 6 hours, whose file system didn't grow."""
    began = NOW - dt.timedelta(minutes=10)
    mod = {"StartTime": began, "ModificationState": state, "TargetSize": 300}
    return Ec2(size=300, mods=[mod])


@pytest.mark.parametrize("state", ["optimizing", "completed"])
def test_the_same_size_on_a_running_vm_grows_its_file_system_alone(state):
    """#563: after an interrupted grow, within EBS's 6 hours, the volume's own size: no
    ModifyVolume and no wait, at no cost; the dry run says so, and the apply runs `GROW` alone
    with that size."""
    ec2, ssh = interrupted(state), Ssh()
    code, out, _ = grow(ec2, size=300, dry_run=True, ssh=ssh)
    assert code == 0 and not ec2.changed()
    assert out[0] == (
        "alice i-1 (running): volume vol-1 gp3 already 300 GB: its file system alone, no "
        "ModifyVolume"
    )
    assert out[1].startswith("alice: root file system now: 194G, ")
    assert out[2] == (
        "alice: on apply: no ModifyVolume; would grow its file system over SSH (no cost): "
        "growpart and resize2fs, online, which change nothing where it fills the volume already"
    )
    code, out, clock = grow(ec2, size=300, ssh=ssh)
    assert code == 0 and not ec2.changed() and clock.slept == []
    assert [c[0] for c in ec2.calls].count("volume_modifications") == 2  # each run's check
    assert shlex.split(ssh.argvs[-1][-1]) == ["bash", "-c", gd.GROW, "bash", "300"]
    assert "alice: ModifyVolume" not in " ".join(out)
    assert out[-1].startswith("alice: root file system now: 291G, ")


def test_a_file_system_alone_that_fails_says_how_to_finish():
    code, out, _ = grow(interrupted(), size=300, ssh=Ssh(grow_ok=False))
    assert code == 1
    assert out[-2:] == [
        "alice: FAILED: its file system was not grown (exit 6)",
        f"alice: the volume is 300 GB; {FINISH}",
    ]


def test_run_keeping_keeps_the_output_before_a_timeout():
    argv = ["/bin/sh", "-c", "echo 'before: x'; exec sleep 5"]
    res = gd.run_keeping(argv, timeout_s=0.5)
    assert (res.exit_code, res.stdout) == (-1, "before: x\n")
    assert res.stderr == "timed out after 0.5 s: /bin/sh"
    assert gd.run_keeping(["/bin/sh", "-c", "echo ok; exit 3"]) == RunResult(3, "ok\n", "")
    assert gd.run_keeping(["/nonexistent/zab-d553"]).stderr.startswith("cannot run ")


def test_a_failed_read_after_modify_volume_says_how_to_finish():
    class Lost(Ec2):
        def find_instances(self, wanted, states):
            if self.changed():
                raise ClientError("RequestLimitExceeded", "slow down")
            return super().find_instances(wanted, states)

    code, out, _ = grow(Lost())
    assert code == 1
    assert out[-2].startswith("alice: FAILED: reading EC2 after ModifyVolume: ClientError")
    assert out[-1] == f"alice: the volume is 300 GB, or will be; {FINISH}"


def test_a_refused_modify_volume_fails_the_apply():
    ec2 = Ec2()
    ec2.refuse = True
    code, out, _ = grow(ec2)
    assert code == 1 and out[-1].startswith(
        "alice: FAILED: ModifyVolume of vol-1 (VolumeModificationRateExceeded)"
    )


def test_a_failed_or_slow_modification_fails_the_apply():
    ec2 = Ec2()
    ec2.steps = ["modifying", "failed"]
    code, out, _ = grow(ec2)
    assert (
        code == 1
        and out[-1] == "alice: FAILED: EC2's modification of vol-1 failed: no reason given"
    )
    ec2 = Ec2()
    ec2.steps = ["modifying"] * 1000
    code, out, clock = grow(ec2)
    assert code == 1 and clock.now == gd.MODIFY_WAIT_S
    slow = "alice: FAILED: vol-1 is still modifying after 240 s: EC2 may finish it later; then"
    assert out[-1] == f"{slow} {FINISH}"


def test_the_waits_fit_the_bots_timeout():
    """`MODIFY_WAIT_S`, the SSH steps and a few EC2 calls stay under ADMIN §2.4's 15 minutes."""
    from zab.admin.runner import timeout

    # each EC2 call at most two tries of 10 s + 20 s (EC2_CONFIG): the checks' three, the
    # modification, the wait's last read and the state read after it
    ec2_calls = 6 * 60
    assert gd.MODIFY_WAIT_S + gd.POLL_S + gd.READ_S + gd.GROW_S + ec2_calls < timeout("grow-disk")


# ---------------------------------------------------------------------------- the Ec2 calls


class Paginated:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def paginate(self, **params):
        self.asked.append(params)
        return iter(self.pages)


class Client:
    def __init__(self):
        self.paginators = {
            "describe_volumes": Paginated([{"Volumes": []}, {"Volumes": [{"VolumeId": "vol-1"}]}]),
            "describe_volumes_modifications": Paginated([{"VolumesModifications": [{"x": 1}]}]),
        }
        self.modified = []

    def get_paginator(self, name):
        return self.paginators[name]

    def modify_volume(self, **params):
        self.modified.append(params)
        return {"VolumeModification": {}}


def test_the_ec2_calls_filter_by_volume_and_read_every_page():
    ec2 = object.__new__(ac.Ec2)
    ec2._ec2 = client = Client()
    assert ec2.volumes(["vol-1"]) == [{"VolumeId": "vol-1"}]
    assert ec2.volume_modifications("vol-1") == [{"x": 1}]
    for name in ("describe_volumes", "describe_volumes_modifications"):
        assert client.paginators[name].asked == [
            {"Filters": [{"Name": "volume-id", "Values": ["vol-1"]}]}
        ]
    ec2.modify_volume("vol-1", 300)
    assert client.modified == [{"VolumeId": "vol-1", "Size": 300}]


# ---------------------------------------------------------------------------------- GROW


GIB = 2**30 // 512  # sectors


def fake_vm(tmp: Path, *, disk_gb=300, start=4096, part_sectors=None, fstype="ext4",
            growpart_rc=0, resize_rc=0, partition=True,
            said="") -> tuple[str, dict[str, str]]:  # fmt: skip
    """A fake sysfs under `tmp/sys` and fake tools first on PATH; GROW with `sys` pointed there.
    The disk is `disk_gb`; growpart (as root, through the fake sudo) makes the partition fill it;
    each tool logs its argv to `tmp/log`, and the file system's tool prints `said`."""
    sysfs = tmp / "sys"
    disk = sysfs / "devices" / "nvme0n1"
    part = disk / "nvme0n1p1"
    part.mkdir(parents=True)
    (disk / "size").write_text(f"{disk_gb * GIB}\n")
    (part / "start").write_text(f"{start}\n")
    (part / "size").write_text(f"{part_sectors or 200 * GIB - start - 34}\n")
    if partition:
        (part / "partition").write_text("1\n")
    (sysfs / "dev" / "block").mkdir(parents=True)
    os.symlink(part, sysfs / "dev" / "block" / "259:1")
    bin_ = tmp / "bin"
    bin_.mkdir()
    log = tmp / "log"
    tools = {
        "findmnt": f'case "$2" in MAJ:MIN) echo "259:1 " ;; FSTYPE) echo "{fstype}" ;; esac',
        "df": f'echo "Filesystem Type 1024-blocks Used Available Capacity Mounted"; '
              f'echo "/dev/root {fstype} 1000 10 990 1% /"',
        "sudo": '[ "$1" = -n ] && shift; exec "$@"',
        "growpart": f'echo "growpart $* (TMPDIR=${{TMPDIR:-}})" >> {log}; '
                    f'[ {growpart_rc} -eq 0 ] && echo $(( $(cat {disk}/size) - {start} - 34 )) '
                    f'> {part}/size; echo "NOCHANGE or not"; exit {growpart_rc}',
        "resize2fs": f'echo "resize2fs $*" >> {log}; echo "{said}"; exit {resize_rc}',
        "xfs_growfs": f'echo "xfs_growfs $*" >> {log}; echo "{said}"; exit {resize_rc}',
        "sleep": f'echo "sleep $*" >> {log}',
    }  # fmt: skip
    for name, body in tools.items():
        path = bin_ / name
        path.write_text(f"#!/bin/bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    script = gd.GROW.replace("sys=/sys\n", f"sys={shlex.quote(str(sysfs))}\n")
    assert script != gd.GROW
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "LANG": "C"}
    return script, env


def run_grow(tmp: Path, size: str = "300", **kw) -> tuple[int, str, list[str]]:
    script, env = fake_vm(tmp, **kw)
    argv = ["/bin/bash", "-c", script, "bash", size]  # our own literal, as the VM's bash runs it
    done = subprocess.run(  # noqa: S603 - an argument list
        argv, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    log = tmp / "log"
    return done.returncode, done.stdout, log.read_text().splitlines() if log.exists() else []


def test_grow_grows_the_partition_then_ext4(tmp_path):
    code, out, log = run_grow(tmp_path)
    assert code == 0, out
    # its temporary directory off the full root (Codex round 1 on #557)
    assert log == ["growpart /dev/nvme0n1 1 (TMPDIR=/run)", "resize2fs /dev/nvme0n1p1"]
    assert out.splitlines() == [
        "before: /dev/root ext4 1000 10 990 1% /",
        "grow: growpart /dev/nvme0n1 1: grown",
        "grow: ext4 on /dev/nvme0n1p1 resized",
        "after: /dev/root ext4 1000 10 990 1% /",
    ]


def test_grow_takes_xfs_and_a_partition_that_fills_the_disk_already(tmp_path):
    said = "data blocks changed from 26214139 to 78642939"  # xfs_growfs, having grown it
    code, out, log = run_grow(
        tmp_path, fstype="xfs", part_sectors=300 * GIB - 4096 - 34, growpart_rc=1, said=said
    )
    assert code == 0, out
    assert "grow: growpart /dev/nvme0n1 1: unchanged" in out
    assert "grow: xfs on /dev/nvme0n1p1 resized" in out
    assert log == ["growpart /dev/nvme0n1 1 (TMPDIR=/run)", "xfs_growfs /"]


@pytest.mark.parametrize(
    ("fstype", "said"),
    [
        ("ext4", "The filesystem is already 78642939 (4k) blocks long.  Nothing to do!"),
        ("xfs", "meta-data=/dev/nvme0n1p1 isize=512 agcount=4"),  # no "data blocks changed"
    ],
)
def test_grow_says_when_the_file_system_fills_its_partition_already(tmp_path, fstype, said):
    """The review of #584: the file system alone on a disk grown all the way already, the
    no-op it allows; `GROW` says so rather than `resized`."""
    full = 300 * GIB - 4096 - 34
    code, out, _ = run_grow(tmp_path, fstype=fstype, part_sectors=full, growpart_rc=1, said=said)
    assert code == 0, out
    assert f"grow: {fstype} on /dev/nvme0n1p1 fills it already: nothing to resize" in out
    assert "resized" not in out


def test_grow_finishes_ext4_on_a_partition_grown_already(tmp_path):
    """#563, the file system alone after an interrupted grow: the partition fills the 300 GB
    disk already, so `growpart` says NOCHANGE (exit 1), and `resize2fs` still runs."""
    code, out, log = run_grow(tmp_path, part_sectors=300 * GIB - 4096 - 34, growpart_rc=1)
    assert code == 0, out
    assert "grow: growpart /dev/nvme0n1 1: unchanged" in out
    assert "grow: ext4 on /dev/nvme0n1p1 resized" in out
    assert log == ["growpart /dev/nvme0n1 1 (TMPDIR=/run)", "resize2fs /dev/nvme0n1p1"]


def test_grow_waits_for_the_disk_to_show_its_size_then_gives_up(tmp_path):
    code, out, log = run_grow(tmp_path, size="400")  # the disk shows 300 GB
    assert code == 5 and "grow: /dev/nvme0n1 still shows 300 GB after 60 s" in out
    assert log == ["sleep 2"] * 30  # and nothing grown


@pytest.mark.parametrize(
    ("kw", "code", "said"),
    [
        ({"size": "3e2"}, 2, "grow: the size is not a number"),
        ({"size": "$(id)"}, 2, "grow: the size is not a number"),
        ({"partition": False}, 3, "grow: the root file system is not on a disk partition"),
        ({"growpart_rc": 2}, 6, "grow: growpart /dev/nvme0n1 1 failed (2): NOCHANGE or not"),
        (
            {"growpart_rc": 1},
            7,
            "grow: /dev/nvme0n1p1 ends 102400 MB before the end of /dev/nvme0n1",
        ),
        ({"fstype": "btrfs"}, 8, "grow: the root file system is btrfs, neither ext4 nor xfs"),
        ({"resize_rc": 1}, 9, "grow: resizing ext4 failed (1)"),
    ],
)
def test_grow_refuses_what_it_cant_grow(tmp_path, kw, code, said):
    got, out, _ = run_grow(tmp_path, **kw)
    assert got == code and said in out, out
    assert "after:" not in out
