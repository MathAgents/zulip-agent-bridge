"""`workshop/ami/build_ami.py` (workshop/ami/README.md) against fakes for EC2 and SSH: the build
flow, the smoke test, cleanup on failure, the Magma tarball, the dry run. Nothing reaches AWS."""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import build_ami as ba
import m2_debs
import pytest

from .scope import with_build

#: The real ones: the autouse fixture `deps` replaces them for main()
DEPS_TARBALL = ba.deps_tarball
ORIGIN_REPO = m2_debs.origin_repo

CONSOLE = """
cloud-init[800]: -----BEGIN SSH HOST KEY FINGERPRINTS-----
cloud-init[800]: 256 SHA256:abcDEF123+/= root@ip (ED25519)
cloud-init[800]: 3072 SHA256:rsarsarsa root@ip (RSA)
cloud-init[800]: -----END SSH HOST KEY FINGERPRINTS-----
"""


class FakeEc2:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.instances: list[str] = []
        self.sg_busy = 0  # delete_security_group fails this many times first
        self.deny_ingress = False

    def identity(self):
        self.calls.append(("identity",))
        return ("123456789012", "arn:aws:iam::123456789012:user/drew")

    def image_name_taken(self, name):
        self.calls.append(("image_name_taken", name))
        return name == "taken"

    def latest_ubuntu_image(self):
        self.calls.append(("latest_ubuntu_image",))
        return "ami-base"

    def default_vpc(self):
        self.calls.append(("default_vpc",))
        return "vpc-1"

    def subnet(self, subnet_id):
        self.calls.append(("subnet", subnet_id))
        return {
            "SubnetId": subnet_id,
            "VpcId": "vpc-1",
            "MapPublicIpOnLaunch": subnet_id != "subnet-private",
        }

    def public_subnet(self, vpc_id):
        self.calls.append(("public_subnet", vpc_id))
        return "subnet-pub"

    def create_security_group(self, name, vpc, tags):
        self.calls.append(("create_security_group", name, vpc, tags))
        return "sg-tmp"

    def authorize_ssh(self, sg, cidr):
        self.calls.append(("authorize_ssh", sg, cidr))
        if self.deny_ingress:
            raise RuntimeError("UnauthorizedOperation")

    def delete_security_group(self, sg):
        if self.sg_busy:
            self.sg_busy -= 1
            raise RuntimeError("DependencyViolation")
        self.calls.append(("delete_security_group", sg))

    def import_key_pair(self, name, public_key, tags):
        self.calls.append(("import_key_pair", name, public_key, tags))
        return name

    def delete_key_pair(self, name):
        self.calls.append(("delete_key_pair", name))

    def run_instance(self, **params):
        iid = f"i-{len(self.instances) + 1:04d}"
        self.instances.append(iid)
        self.calls.append(("run_instance", params))
        return iid

    def wait_running(self, iid):
        self.calls.append(("wait_running", iid))

    def public_ip(self, iid):
        return "198.51.100.7"

    def console_output(self, iid):
        return CONSOLE

    def create_image(self, iid, name, description, tags):
        self.calls.append(("create_image", iid, name, description, tags))
        return "ami-new"

    def wait_image(self, ami):
        self.calls.append(("wait_image", ami))

    def terminate(self, ids):
        self.calls.append(("terminate", list(ids)))

    def wait_terminated(self, ids):
        self.calls.append(("wait_terminated", list(ids)))


class FakeSsh:
    def __init__(self, build_exit: int = 0, smoke_result: str = "SMOKE-RESULT 0") -> None:
        self.calls: list[tuple] = []
        self.build_exit = build_exit
        self.smoke_result = smoke_result
        self.ready = True

    def wait_port(self, ip, timeout_s=300.0):
        self.calls.append(("wait_port", ip))
        return self.ready

    def pin_host_key(self, ip):
        self.calls.append(("pin_host_key", ip))
        return "SHA256:abcDEF123+/="

    def wait_ready(self, ip, timeout_s=300.0):
        self.calls.append(("wait_ready", ip))
        return self.ready

    fail_cleanup = False

    def run(self, ip, command, *, timeout_s=3600.0, check=True):
        self.calls.append(("run", ip, command))
        if command == ba.FINAL_CLEANUP and self.fail_cleanup:
            raise RuntimeError("ssh 'rm -f …' failed (1): rm: cannot remove")
        out = ""
        if "ZAB_EOF" in command:  # a smoke script (which itself mentions /etc/machine-id)
            out = f"ok   something\n{self.smoke_result}\n"
        elif "cat /etc/machine-id" in command:
            out = "0123456789abcdef0123456789abcdef\n"
        elif "uptime -s" in command:
            out = "2026-10-02 10:00:00\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def stream(self, ip, command, timeout_s=7200.0):
        self.calls.append(("stream", ip, command))
        return self.build_exit

    def copy(self, ip, local, remote):
        self.calls.append(("copy", ip, Path(local).name, remote))


@pytest.fixture(autouse=True)
def deps(monkeypatch, tmp_path):
    """main() fetches the pinned sets first (#945), the Macaulay2 packages and PARI's data
    packages: here a tarball of nothing each, and the calls, so that no test runs `gh`."""
    calls: list[tuple] = []

    def fake(opts, work, log, kind=m2_debs.M2):
        prefix = kind.option.removeprefix("--").replace("-", "_")
        calls.append(
            (kind.attr, getattr(opts, f"{prefix}_release"), getattr(opts, f"{prefix}_dir"))
        )
        tar = work / kind.tarball
        tar.write_bytes(b"")
        return str(tar)

    monkeypatch.setattr(ba, "deps_tarball", fake)
    monkeypatch.setattr(ba.m2_debs, "origin_repo", lambda *a: "o/r")  # the checkout's origin
    return calls


@pytest.fixture
def quiet_time(monkeypatch):
    monkeypatch.setattr(ba.time, "sleep", lambda s: None)


@pytest.fixture
def pubkey(tmp_path) -> str:
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests drew@laptop\n")
    return str(key)


#: A deployer's licence setup script, as the build sees one: a #! line, then nothing it reads.
HOOK = "#!/bin/sh\n: the deployer's licence setup\n"


def opts(**kw):
    base = {
        "profile": "workshop",
        "account_id": "123456789012",
        "region": "us-west-2",
        "name": "nt-agents-2026-v1",
        "tag": [("workshop", "nt-agents-2026")],  # required (#808): the first fleet's, as data
        "instance_type": "m7a.xlarge",
        "smoke_instance_type": "m7a.2xlarge",
        "smoke_warm": False,
        "base_ami": None,
        "vpc_id": None,
        "subnet_id": None,
        "my_ip": "203.0.113.5",
        "ssh_public_key": "/dev/null",
        "ssh_key_file": None,
        "codex_version": "0.157.1",
        "claude_version": "2.1.283",
        "zab_tag": "v0.0.2",
        "zab_wheel": None,
        "pari_version": "2.17.4",
        "magma_dir": None,
        "magma_passfile": None,
        "magma_licence_setup": None,
        "deps_release": m2_debs.RELEASE,
        "deps_dir": None,
        "m2_debs": None,
        "pari_data_release": m2_debs.PARI_RELEASE,
        "pari_data_dir": None,
        "pari_data": None,
        "with_sage": False,
        "sage_version": "10.9",
        "smoke": False,
        "smoke_only": None,
        "assume_magma": False,
        "assume_sage": False,
        "keep": False,
        "dry_run": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def magma_tree(tmp_path: Path) -> Path:
    root = tmp_path / "magma"
    (root / "libs" / "data").mkdir(parents=True)
    (root / "package").mkdir()
    (root / "magma.exe").write_bytes(b"\x7fELF")
    (root / "magma").write_text("#!/bin/sh\n")
    (root / "libs" / "data" / "x").write_text("x")
    (root / "package" / "spec").write_text("spec")
    for name in ("magmapassfile", "magmapassfile_cloud", "oldmagmapassfile", "magmapassfile.txt"):
        (root / name).write_text("LICENCE SECRET")
    return root


def test_magma_tarball_excludes_every_licence_file(tmp_path):
    root = magma_tree(tmp_path)
    dest = ba.magma_tarball(str(root), str(tmp_path / "magma.tar.gz"))
    with tarfile.open(dest) as tar:
        names = tar.getnames()
        payload = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
    assert "magma/magma.exe" in names and "magma/libs/data/x" in names
    assert not any("passfile" in n for n in names)
    assert b"LICENCE SECRET" not in payload
    with pytest.raises(ValueError, match="not a Magma installation"):
        ba.magma_tarball(str(tmp_path), str(tmp_path / "x.tar.gz"))


def test_console_fingerprints_and_host_key_verification(quiet_time):
    assert ba.console_fingerprints(CONSOLE) == {"SHA256:abcDEF123+/=", "SHA256:rsarsarsa"}
    assert ba.console_fingerprints("nothing yet") == set()
    ec2 = FakeEc2()
    log: list[str] = []
    ba.verify_host_key(ec2, "i-1", "SHA256:abcDEF123+/=", log.append)
    assert any("matches the console output" in line for line in log)
    with pytest.raises(RuntimeError, match="not in the console output"):
        ba.verify_host_key(ec2, "i-1", "SHA256:somebodyelse", log.append)
    # a console that never prints fingerprints: warned, first contact trusted (tries bounded)
    ec2.console_output = lambda iid: ""
    ba.verify_host_key(ec2, "i-1", "SHA256:abcDEF123+/=", log.append, tries=2)
    assert any("WARNING" in line for line in log)


def test_launch_pins_and_verifies_the_host_key_before_any_ssh_session(quiet_time):
    """Codex round 3 on PR #56: the key ssh trusts must be the one checked against the console.
    Order: port open, keyscan into known_hosts, console check, then the first strict session."""
    ec2, ssh, res, log = FakeEc2(), FakeSsh(), ba.Resources(), []
    res.security_group, res.key_pair = "sg", "kp"
    instance, ip, pinned = ba.launch(ec2, ssh, opts(), res, "ami-base", "n", log.append)
    assert (instance, ip, pinned) == ("i-0001", "198.51.100.7", "SHA256:abcDEF123+/=")
    kinds = [c[0] for c in ssh.calls]
    assert kinds == ["wait_port", "pin_host_key", "wait_ready", "run"]
    assert ssh.calls[-1][2] == "sudo shutdown -h +120"  # the safety net, after the first session
    # a mismatch raises before wait_ready, i.e. before any session is opened
    bad = FakeSsh()
    bad.pin_host_key = lambda ip: "SHA256:intermediary"
    with pytest.raises(RuntimeError, match="not in the console output"):
        ba.launch(
            FakeEc2(),
            bad,
            opts(),
            ba.Resources(security_group="sg", key_pair="kp"),
            "ami-base",
            "n",
            log.append,
        )
    assert [c[0] for c in bad.calls] == ["wait_port"]


def test_pin_host_key_stores_the_scanned_key_and_ssh_is_strict(tmp_path, monkeypatch):
    known = tmp_path / "known_hosts"
    ssh = ba.Ssh(None, str(known), lambda s: None)
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[0] == "ssh-keyscan":
            return SimpleNamespace(
                returncode=0, stdout="# comment\n198.51.100.7 ssh-ed25519 AAAAC3fake\n", stderr=""
            )
        assert (
            argv[:2] == ["ssh-keygen", "-lf"]
            and kw["input"] == "198.51.100.7 ssh-ed25519 AAAAC3fake\n"
        )
        return SimpleNamespace(
            returncode=0, stdout="256 SHA256:abcDEF123+/= 198.51.100.7 (ED25519)\n", stderr=""
        )

    monkeypatch.setattr(ba.subprocess, "run", fake_run)
    assert ssh.pin_host_key("198.51.100.7") == "SHA256:abcDEF123+/="
    assert known.read_text() == "198.51.100.7 ssh-ed25519 AAAAC3fake\n"
    assert calls[0][:4] == ["ssh-keyscan", "-t", "ed25519", "-T"]
    opts_ = ssh._opts()
    assert "StrictHostKeyChecking=yes" in opts_ and not any("accept-new" in o for o in opts_)
    assert f"UserKnownHostsFile={known}" in opts_ and "ForwardAgent=no" in opts_

    def no_key(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    clock = FakeClock(monkeypatch)
    monkeypatch.setattr(ba.subprocess, "run", no_key)
    with pytest.raises(RuntimeError, match=r"no ed25519 host key scanned from \S+ in 120 s"):
        ssh.pin_host_key("198.51.100.7")
    assert clock.slept == [5.0] * 24  # 25 tries, 5 s apart, the last at 120 s


class FakeClock:
    """`time.monotonic` and `time.sleep` for the build: a sleep moves the clock."""

    def __init__(self, monkeypatch) -> None:
        self.now, self.slept = 1000.0, []
        monkeypatch.setattr(ba.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(ba.time, "sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_pin_host_key_tries_again_until_the_key_is_served(tmp_path, monkeypatch):
    """#953: a fresh instance's port 22 can accept before its first boot serves the host key,
    so `ssh-keyscan` finds nothing at first. The scan is tried again every 5 s: nothing twice,
    then the key, which is pinned. A scan that times out is a try like the others."""
    known = tmp_path / "known_hosts"
    ssh = ba.Ssh(None, str(known), lambda s: None)
    clock = FakeClock(monkeypatch)
    answers = [
        SimpleNamespace(returncode=1, stdout="", stderr="198.51.100.7: Connection closed\n"),
        subprocess.TimeoutExpired(["ssh-keyscan"], 60),
        SimpleNamespace(returncode=0, stdout="198.51.100.7 ssh-ed25519 AAAAC3late\n", stderr=""),
    ]
    scans = []

    def fake_run(argv, **kw):
        if argv[0] == "ssh-keyscan":
            scans.append(clock.now)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return SimpleNamespace(
            returncode=0, stdout="256 SHA256:late+key= 198.51.100.7 (ED25519)\n", stderr=""
        )

    monkeypatch.setattr(ba.subprocess, "run", fake_run)
    assert ssh.pin_host_key("198.51.100.7") == "SHA256:late+key="
    assert known.read_text() == "198.51.100.7 ssh-ed25519 AAAAC3late\n"
    assert scans == [1000.0, 1005.0, 1010.0] and clock.slept == [5.0, 5.0]


def test_pin_host_key_says_how_long_it_tried(tmp_path, monkeypatch):
    """#953: the failure names the time spent, the tries and the scan's last word."""
    ssh = ba.Ssh(None, str(tmp_path / "known_hosts"), lambda s: None)
    FakeClock(monkeypatch)

    def refused(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="198.51.100.7: Connection reset\n")

    monkeypatch.setattr(ba.subprocess, "run", refused)
    with pytest.raises(RuntimeError) as caught:
        ssh.pin_host_key("198.51.100.7", every_s=10.0, for_s=30.0)
    assert str(caught.value) == (
        "no ed25519 host key scanned from 198.51.100.7 in 30 s (4 tries, 10 s apart): "
        "198.51.100.7: Connection reset"
    )


def test_security_group_is_registered_before_its_rule(quiet_time):
    """Codex round 3 on PR #56: a failed ingress authorization must not leak the group."""
    ec2, res, log = FakeEc2(), ba.Resources(), []
    ec2.deny_ingress = True
    with pytest.raises(RuntimeError, match="UnauthorizedOperation"):
        ba.prepare(ec2, opts(), res, log.append)
    assert res.security_group == "sg-tmp" and res.key_pair == ""
    ba.cleanup(ec2, res, log.append)
    assert ("delete_security_group", "sg-tmp") in ec2.calls and res == ba.Resources()


def test_build_flow_copies_files_runs_build_sh_and_images(tmp_path, quiet_time):
    root = magma_tree(tmp_path)
    passfile = tmp_path / "pass"
    passfile.write_text("LICENCE SECRET")
    hook = tmp_path / "licence-setup.sh"
    hook.write_text(HOOK)
    wheel = tmp_path / "zulip_agent_bridge-0.0.2-py3-none-any.whl"
    wheel.write_bytes(b"PK")
    debs, pari = tmp_path / "m2-debs.tar", tmp_path / "pari-data.tar"
    debs.write_bytes(b"")
    pari.write_bytes(b"")
    o = opts(
        zab_wheel=str(wheel),
        magma_dir=str(root),
        magma_passfile=str(passfile),
        magma_licence_setup=str(hook),
        with_sage=True,
        m2_debs=str(debs),
        pari_data=str(pari),
    )
    ec2, ssh, res, log = FakeEc2(), FakeSsh(), ba.Resources(), []
    ba.prepare(ec2, o, res, log.append)
    sg = next(c for c in ec2.calls if c[0] == "create_security_group")
    assert sg[3]["workshop"] == "nt-agents-2026"
    assert ("authorize_ssh", "sg-tmp", "203.0.113.5/32") in ec2.calls
    assert res.security_group == "sg-tmp" and res.key_pair.startswith("zab-ami-builder-")
    ami, identity = ba.build(ec2, ssh, o, res, log.append)
    assert ami == "ami-new" and identity["BUILDER_MACHINE_ID"] == "0123456789abcdef0123456789abcdef"
    assert identity["BUILDER_HOST_FP"] == "SHA256:abcDEF123+/="
    run = next(c for c in ec2.calls if c[0] == "run_instance")[1]
    assert run["ImageId"] == "ami-base" and run["MetadataOptions"]["HttpTokens"] == "required"
    assert "IamInstanceProfile" not in run and run["SecurityGroupIds"] == ["sg-tmp"]
    assert (
        ba.tag_list({"Name": "zab-ami-builder", "workshop": "nt-agents-2026", "role": "ami-build"})
        == run["TagSpecifications"][0]["Tags"]
    )
    copied = [c[2] for c in ssh.calls if c[0] == "copy"]
    assert copied == [
        "build.sh",
        "ami-files.tar.gz",
        "m2-debs.tar",
        "pari-data.tar",
        wheel.name,
        "magma.tar.gz",
        "pass",
        "licence-setup.sh",
    ]
    # the deployer's script goes up as it is, under the name FINAL_CLEANUP removes, root-only
    assert ("copy", "198.51.100.7", "licence-setup.sh", "magma-licence-setup") in ssh.calls
    assert "rm -f magmapassfile magma.tar.gz magma-licence-setup " in ba.FINAL_CLEANUP
    assert "test ! -e magma-licence-setup" in ba.FINAL_CLEANUP
    assert " m2-debs.tar pari-data.tar" in ba.FINAL_CLEANUP.split("&&")[0]  # with the copies
    assert ("run", "198.51.100.7", "chmod 0600 magmapassfile") in ssh.calls
    assert ("run", "198.51.100.7", "chmod 0700 magma-licence-setup") in ssh.calls
    stream = next(c for c in ssh.calls if c[0] == "stream")[2]
    assert stream.endswith(" bash build.sh") and stream.startswith("env ")
    for knob in (
        "CODEX_VERSION=0.157.1",
        "CLAUDE_VERSION=2.1.283",
        "ZAB_TAG=v0.0.2",
        "PARI_VERSION=2.17.4",
        f"ZAB_WHEEL=/home/ubuntu/{wheel.name}",
        "MAGMA_TARBALL=/home/ubuntu/magma.tar.gz",
        "MAGMA_PASSFILE=/home/ubuntu/magmapassfile",
        "MAGMA_LICENCE_SETUP=/home/ubuntu/magma-licence-setup",
        "M2_DEBS=/home/ubuntu/m2-debs.tar",
        "PARI_DATA=/home/ubuntu/pari-data.tar",
        "WITH_SAGE=1",
        "SAGE_VERSION=10.9",
    ):
        assert knob in stream, knob
    runs = [c[2] for c in ssh.calls if c[0] == "run"]
    assert ba.FINAL_CLEANUP in runs and "rm -f magmapassfile magma.tar.gz" not in runs
    assert runs.index(ba.FINAL_CLEANUP) == len(runs) - 1  # the very last command on the builder
    assert "sudo shutdown -h +240" in runs  # the safety net (the instance terminates on halt)
    assert run["InstanceInitiatedShutdownBehavior"] == "terminate"
    image = next(c for c in ec2.calls if c[0] == "create_image")
    assert image[1] == "i-0001" and image[2] == "nt-agents-2026-v1"
    assert image[4] == {"workshop": "nt-agents-2026", "Name": "nt-agents-2026-v1"}
    assert ("wait_image", "ami-new") in ec2.calls
    ba.cleanup(ec2, res, log.append)
    kinds = [c[0] for c in ec2.calls]
    assert kinds[-3:] == ["wait_terminated", "delete_key_pair", "delete_security_group"]
    assert res == ba.Resources()


def test_knob_values_are_shell_quoted():
    cmd = ba.env_command({"ZAB_WHEEL": "/home/ubuntu/a b.whl", "X": "$(rm -rf /)"}, "bash build.sh")
    assert cmd == "env X='$(rm -rf /)' ZAB_WHEEL='/home/ubuntu/a b.whl' bash build.sh"


def test_smoke_runs_checks_reboots_and_checks_again(quiet_time):
    ec2, ssh, res, log = FakeEc2(), FakeSsh(), ba.Resources(), []
    res.security_group, res.key_pair = "sg-tmp", "kp"
    identity = {"BUILDER_MACHINE_ID": "builder-id", "BUILDER_HOST_FP": "SHA256:builder"}
    o = opts(magma_dir="/opt/magma", with_sage=True)
    assert ba.smoke(ec2, ssh, o, res, "ami-new", identity, log.append) is True
    runs = [c[2] for c in ssh.calls if c[0] == "run"]
    scripts = [r for r in runs if "ZAB_EOF" in r]
    assert len(scripts) == 2  # before and after the reboot
    first, second = scripts
    for needle in (
        "MAGMA=1",
        "SAGE=1",
        "BUILDER_MACHINE_ID=builder-id",
        "BUILDER_HOST_FP=SHA256:builder",
        "PARI_VERSION=2.17.4",
        "ellsea(ellinit(",
        "Auto-updates: disabled",
        "mcp get lmfdb",
        "/etc/machine-id",
        "ssh_host_ed25519_key.pub",
    ):
        assert needle in first, needle
    assert "BOOT_BEFORE='2026-10-02 10:00:00'" in second
    # Magma, before and after the reboot: the same checks (MAGMA_SMOKE), the licence's hook
    # where the image has one, and a computation that comes back
    assert all(ba.MAGMA_SMOKE in s and "magma-licence.service" in s for s in scripts)
    assert "@MAGMA_SMOKE@" not in ba.SMOKE_SCRIPT + ba.REBOOT_SCRIPT
    assert any("systemctl reboot" in r for r in runs)
    # the reboot dropped the first safety net: a new one is set before the second script
    reboot_at = next(i for i, r in enumerate(runs) if "systemctl reboot" in r)
    assert "sudo shutdown -h +60" in runs[reboot_at + 1 :]
    assert runs.index("sudo shutdown -h +60") < runs.index(second)
    assert ec2.instances == ["i-0001"] and res.instances == ["i-0001"]  # cleanup is the caller's
    # a failing check anywhere fails the smoke test
    bad = FakeSsh(smoke_result="SMOKE-RESULT 1")
    assert (
        ba.smoke(
            FakeEc2(),
            bad,
            o,
            ba.Resources(security_group="sg", key_pair="k"),
            "ami-new",
            identity,
            log.append,
        )
        is False
    )
    # no Magma, no Sage on the image: the scripts are told so
    plain = FakeSsh()
    ba.smoke(
        FakeEc2(),
        plain,
        opts(),
        ba.Resources(security_group="sg", key_pair="k"),
        "ami-new",
        identity,
        log.append,
    )
    assert "MAGMA=0" in plain.calls[-1][2] and "SAGE=0" in plain.calls[-1][2]


def test_smoke_scripts_parse():
    for script in (ba.SMOKE_SCRIPT, ba.REBOOT_SCRIPT):
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)  # noqa: S607
    cmd = ba.heredoc({"MAGMA": "1"}, "echo hi")
    assert cmd.startswith("env MAGMA=1 bash -s <<'ZAB_EOF'\n") and cmd.endswith("\nZAB_EOF")


def test_main_cleans_up_when_build_sh_fails(monkeypatch, pubkey, capsys, quiet_time):
    ec2, ssh = FakeEc2(), FakeSsh(build_exit=3)
    ec2.sg_busy = 2  # the group is busy until the instance is gone: retried
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
            "--smoke",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 1 and "build.sh exited 3" in out
    kinds = [c[0] for c in ec2.calls]
    assert "create_image" not in kinds
    assert kinds[-4:] == [
        "terminate",
        "wait_terminated",
        "delete_key_pair",
        "delete_security_group",
    ]
    # --keep leaves the builder for inspection but still removes nothing else than instances
    ec2, ssh = FakeEc2(), FakeSsh(build_exit=3)
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
            "--keep",
        ],
    )
    kinds = [c[0] for c in ec2.calls]
    assert rc == 1 and "terminate" not in kinds
    # the group and key pair stay as well: without them the kept instance is unreachable
    assert "delete_security_group" not in kinds and "delete_key_pair" not in kinds
    assert "--keep: leaving ['i-0001'] running with sg-tmp" in capsys.readouterr().out


def test_keep_also_applies_when_the_smoke_checks_fail(monkeypatch, pubkey, capsys, quiet_time):
    """Codex round 6 on PR #56: a failed check (no exception) is exactly when the instance should
    be kept for inspection; without --keep it is terminated as before."""
    for keep in (True, False):
        ec2, ssh = FakeEc2(), FakeSsh(smoke_result="SMOKE-RESULT 1")
        monkeypatch.setattr(ba, "Ec2", lambda region, profile, ec2=ec2: ec2)
        monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log, ssh=ssh: ssh)
        argv = [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
            "--smoke-only",
            "ami-x",
        ]
        rc = with_build(ba.main, argv + (["--keep"] if keep else []))
        out = capsys.readouterr().out
        kinds = [c[0] for c in ec2.calls]
        assert rc == 1 and "smoke test FAILED" in out
        if keep:
            assert "terminate" not in kinds and "delete_security_group" not in kinds
            assert "--keep: leaving ['i-0001']" in out
        else:
            assert kinds[-4:] == [
                "terminate",
                "wait_terminated",
                "delete_key_pair",
                "delete_security_group",
            ]


def test_main_builds_then_smokes_and_reports_the_ami(monkeypatch, pubkey, capsys, quiet_time):
    ec2, ssh = FakeEc2(), FakeSsh()
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
            "--smoke",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0 and "AMI ami-new" in out and "smoke test passed" in out
    assert ec2.instances == ["i-0001", "i-0002"]  # the builder, then the smoke instance
    terminated = [c[1] for c in ec2.calls if c[0] == "terminate"]
    assert terminated == [["i-0001"], ["i-0002"]]  # the builder before the smoke instance launches
    assert [c[0] for c in ec2.calls][-2:] == ["delete_key_pair", "delete_security_group"]
    # --smoke-only: no build, one instance, the image is not created
    ec2, ssh = FakeEc2(), FakeSsh()
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
            "--smoke-only",
            "ami-x",
            "--assume-magma",
        ],
    )
    assert rc == 0 and "create_image" not in [c[0] for c in ec2.calls]
    assert ec2.calls[[c[0] for c in ec2.calls].index("run_instance")][1]["ImageId"] == "ami-x"
    scripts = [c[2] for c in ssh.calls if c[0] == "run" and "ZAB_EOF" in c[2]]
    assert scripts and all("MAGMA=1" in s for s in scripts)


def test_dry_run_touches_nothing_and_needs_no_boto3(monkeypatch, capsys, tmp_path, pubkey):
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setattr(ba, "egress_ip", lambda *a: pytest.fail("no network in a dry run"))
    root = magma_tree(tmp_path)
    (tmp_path / "hook").write_text(HOOK)
    (tmp_path / "pass").write_text("x")
    magma = ["--magma-dir", str(root), "--magma-passfile", str(tmp_path / "pass")]
    base = ["--dry-run", "--account-id", "123456789012", "--zab-tag", "v0.0.2"]
    base += ["--ssh-public-key", pubkey, *magma]
    hook = ["--magma-licence-setup", str(tmp_path / "hook")]
    rc = with_build(ba.main, [*base, *hook, "--with-sage", "--smoke"])
    out = capsys.readouterr().out
    assert rc == 0 and "would launch one m7a.xlarge builder" in out
    assert "MAGMA_PASSFILE=/home/ubuntu/magmapassfile" in out and "WITH_SAGE=1" in out
    assert "magmapassfile, magma-licence-setup" in out
    assert "MAGMA_LICENCE_SETUP=/home/ubuntu/magma-licence-setup" in out
    assert "would create image" in out and "smoke-test checks" in out
    # the pinned Macaulay2 packages (#945): planned, not fetched (no network in a dry run)
    assert f"would fetch the Macaulay2 packages from release o/r@{m2_debs.RELEASE} (with gh)" in out
    assert "ami-files.tar.gz, m2-debs.tar, pari-data.tar, " in out
    assert (
        "M2_DEBS=/home/ubuntu/m2-debs.tar" in out and "PARI_DATA=/home/ubuntu/pari-data.tar" in out
    )
    assert f"PARI's data packages from release o/r@{m2_debs.PARI_RELEASE} (with gh)" in out
    # without the licence setup, Magma still goes on the image, and the plan says what it means
    assert with_build(ba.main, base) == 0
    out = capsys.readouterr().out
    assert "MAGMA_TARBALL=/home/ubuntu/magma.tar.gz" in out and "MAGMA_LICENCE_SETUP" not in out
    assert "no --magma-licence-setup: Magma will run only if its licence file alone suffices" in out


def test_main_refuses_half_magma_options_and_a_licence_setup_it_cannot_run(
    tmp_path, capsys, pubkey
):
    base = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    rc = with_build(ba.main, [*base, "--magma-dir", str(tmp_path)])
    assert rc == 2 and "go together" in capsys.readouterr().err
    (tmp_path / "pass").write_text("x")
    (tmp_path / "hook").write_text(HOOK)
    rc = with_build(ba.main, [*base, "--magma-licence-setup", str(tmp_path / "hook")])
    assert rc == 2 and "needs --magma-dir and --magma-passfile" in capsys.readouterr().err
    magma = [*base, "--magma-dir", str(tmp_path), "--magma-passfile", str(tmp_path / "pass")]
    rc = with_build(ba.main, [*magma, "--magma-licence-setup", str(tmp_path / "missing")])
    assert rc == 2 and "no such file" in capsys.readouterr().err
    # systemd runs the script itself, so without an interpreter line it could not start
    (tmp_path / "no-shebang").write_text(": no interpreter line\n")
    rc = with_build(ba.main, [*magma, "--magma-licence-setup", str(tmp_path / "no-shebang")])
    assert rc == 2 and "no #! line" in capsys.readouterr().err
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--ssh-public-key",
            str(tmp_path / "missing.pub"),
        ],
    )
    assert rc == 2 and "no such public key" in capsys.readouterr().err


def test_egress_ip_validates_the_answer():
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert ba.egress_ip(lambda url, timeout: Resp(b"203.0.113.5\n")) == "203.0.113.5"
    with pytest.raises(RuntimeError):
        ba.egress_ip(lambda url, timeout: Resp(b"<html>nope</html>"))


class LocalSsh(ba.Ssh):
    """`Ssh` whose "remote" command runs locally, to exercise `stream` for real."""

    def _argv(self, ip, command):
        return ["bash", "-c", command]


def test_stream_logs_lines_and_enforces_the_deadline(tmp_path):
    """Codex round 1 on PR #56: iterating stdout inline never reached the timed wait, so a hung
    build left the builder running forever. A silent process must be killed at the deadline."""
    log: list[str] = []
    ssh = LocalSsh(None, str(tmp_path / "kh"), log.append)
    assert ssh.stream("x", "echo one; echo two; exit 3", timeout_s=10) == 3
    assert log == ["    one", "    two"]
    log.clear()
    assert ssh.stream("x", "read -r x; echo got:$x", timeout_s=10) == 0  # stdin is /dev/null
    assert log == ["    got:"]
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ssh.stream("x", "sleep 30", timeout_s=0.3)
    assert time.monotonic() - t0 < 5  # killed at the deadline, not after 30 s


def test_vpc_and_subnet_selection(quiet_time):
    """Codex round 2 on PR #56: the group and the instance must share a VPC, and the subnet must
    assign a public address, or the launch fails (or lands in the default VPC regardless)."""
    log: list[str] = []
    # default: the default VPC, no SubnetId (EC2 picks its default subnet)
    ec2, res = FakeEc2(), ba.Resources()
    o = opts()
    ba.prepare(ec2, o, res, log.append)
    assert ("default_vpc",) in ec2.calls and o.subnet_id is None
    assert "SubnetId" not in ba.instance_params(o, "ami", "sg", "kp", "n")
    # --vpc-id: a public subnet of that VPC, group created there
    ec2, res = FakeEc2(), ba.Resources()
    o = opts(vpc_id="vpc-9")
    ba.prepare(ec2, o, res, log.append)
    assert ("public_subnet", "vpc-9") in ec2.calls and o.subnet_id == "subnet-pub"
    assert next(c for c in ec2.calls if c[0] == "create_security_group")[2] == "vpc-9"
    assert ba.instance_params(o, "ami", "sg", "kp", "n")["SubnetId"] == "subnet-pub"
    # --subnet-id: its VPC is used for the group
    ec2, res = FakeEc2(), ba.Resources()
    o = opts(subnet_id="subnet-x")
    ba.prepare(ec2, o, res, log.append)
    assert next(c for c in ec2.calls if c[0] == "create_security_group")[2] == "vpc-1"
    assert ba.instance_params(o, "ami", "sg", "kp", "n")["SubnetId"] == "subnet-x"
    # a private subnet, or one outside --vpc-id, is refused before anything is created
    for bad in (opts(subnet_id="subnet-private"), opts(subnet_id="subnet-x", vpc_id="vpc-9")):
        ec2, res = FakeEc2(), ba.Resources()
        with pytest.raises(RuntimeError):
            ba.prepare(ec2, bad, res, log.append)
        assert not any(c[0] == "create_security_group" for c in ec2.calls)


def test_smoke_reads_agent_files_as_agent():
    """Codex round 2 on PR #56: /home/agent is 0750, so ubuntu cannot grep the Codex config."""
    for line in ba.SMOKE_SCRIPT.splitlines():
        if "/home/agent/" in line and ("grep" in line or "test " in line or "cat " in line):
            assert "as_agent" in line or "sudo" in line, line


@pytest.mark.skipif(
    shutil.which("scp") is None or shutil.which("ssh") is None, reason="needs OpenSSH"
)
def test_ssh_and_scp_accept_the_shared_options(tmp_path):
    """Codex round 4 on PR #56: `-a` is an ssh flag that scp rejects (`unknown option -- a`), so
    every copy failed. Both programs parse the real argv here without any network: `ssh -G`
    only prints the resolved configuration, and scp is pointed at /bin/false as its ssh."""
    ssh = ba.Ssh(None, str(tmp_path / "kh"), lambda s: None)
    argv = ssh._scp_argv("192.0.2.1", "/dev/null", "x")
    argv.insert(1, "-S")
    argv.insert(2, "/bin/false")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert "unknown option" not in res.stderr and "usage:" not in res.stderr, res.stderr
    probe = ["ssh", "-G", *ssh._opts(), "ubuntu@192.0.2.1"]
    res = subprocess.run(probe, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert res.returncode == 0, res.stderr
    settings = dict(line.split(" ", 1) for line in res.stdout.splitlines() if " " in line)
    assert settings["forwardagent"] == "no" and settings["stricthostkeychecking"] in ("yes", "true")
    assert settings["batchmode"] == "yes" and settings["userknownhostsfile"] == str(tmp_path / "kh")
    assert settings["serveraliveinterval"] == "30"  # keepalive for the long, mostly silent build


def test_final_cleanup_is_checked_and_a_failure_prevents_imaging(
    monkeypatch, pubkey, capsys, quiet_time
):
    """PM review on #56: the builder's authorized_keys (ubuntu and root) and the copied files are
    removed by one checked command right before create_image; if it fails, no image is made and
    the builder is terminated."""
    assert (
        "~/.ssh/authorized_keys" in ba.FINAL_CLEANUP
        and "/root/.ssh/authorized_keys" in ba.FINAL_CLEANUP
    )
    assert "test ! -e magmapassfile" in ba.FINAL_CLEANUP and "shutdown -c" in ba.FINAL_CLEANUP
    ec2, ssh = FakeEc2(), FakeSsh()
    ssh.fail_cleanup = True
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
        ],
    )
    kinds = [c[0] for c in ec2.calls]
    assert rc == 1 and "create_image" not in kinds
    assert kinds[-4:] == [
        "terminate",
        "wait_terminated",
        "delete_key_pair",
        "delete_security_group",
    ]
    assert "cannot remove" in capsys.readouterr().out


def test_account_is_checked_before_anything_is_created(monkeypatch, pubkey, capsys):
    ec2, ssh = FakeEc2(), FakeSsh()
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "999999999999",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
        ],
    )
    assert rc == 2 and "not 999999999999" in capsys.readouterr().err
    assert [c[0] for c in ec2.calls] == ["identity"]
    rc = with_build(
        ba.main, ["--account-id", "12", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    )
    assert rc == 2 and "12-digit" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # required: no default
        with_build(ba.main, ["--ssh-public-key", pubkey, "--dry-run"])


def test_image_name_must_be_free(monkeypatch, pubkey, capsys, quiet_time):
    ec2, ssh = FakeEc2(), FakeSsh()
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
            "--name",
            "taken",
        ],
    )
    assert rc == 1 and "already exists" in capsys.readouterr().out
    assert "run_instance" not in [c[0] for c in ec2.calls]  # checked before the builder launches


def test_wheel_must_come_from_the_tag(tmp_path, pubkey, capsys):
    wrong = tmp_path / "zulip_agent_bridge-0.1.0.dev3-py3-none-any.whl"
    wrong.write_bytes(b"PK")
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--ssh-public-key",
            pubkey,
            "--zab-wheel",
            str(wrong),
            "--dry-run",
        ],
    )
    assert rc == 2 and "not a v0.0.2 wheel" in capsys.readouterr().err
    right = tmp_path / "zulip_agent_bridge-0.0.2-py3-none-any.whl"
    right.write_bytes(b"PK")
    assert (
        with_build(
            ba.main,
            [
                "--account-id",
                "123456789012",
                "--zab-tag",
                "v0.0.2",
                "--ssh-public-key",
                pubkey,
                "--zab-wheel",
                str(right),
                "--dry-run",
            ],
        )
        == 0
    )


def test_sigterm_runs_cleanup(monkeypatch, pubkey, capsys, quiet_time):
    """PM review on #56: a tool timeout or a closed session (SIGTERM/SIGHUP) must not leave the
    builder, the licence on it, the group or the key pair behind."""
    import os
    import signal as sig

    ec2, ssh = FakeEc2(), FakeSsh()

    def wait_running(iid):
        ec2.calls.append(("wait_running", iid))
        os.kill(os.getpid(), sig.SIGTERM)  # delivered to this thread at the next bytecode

    ec2.wait_running = wait_running
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    before = sig.getsignal(sig.SIGTERM)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
        ],
    )
    out = capsys.readouterr().out
    kinds = [c[0] for c in ec2.calls]
    assert rc == 1 and "ERROR Interrupted: SIGTERM" in out
    assert kinds[-4:] == [
        "terminate",
        "wait_terminated",
        "delete_key_pair",
        "delete_security_group",
    ]
    assert sig.getsignal(sig.SIGTERM) is before  # the previous handler is restored


def test_magma_dir_is_resolved_and_validated_before_aws(tmp_path, pubkey, capsys):
    """A symlinked /opt/magma would otherwise tar as a single link; a tree missing magma.exe is
    refused before any AWS call."""
    root = magma_tree(tmp_path)
    link = tmp_path / "magma-link"
    link.symlink_to(root)
    assert ba.validate_magma_dir(str(link)) == root.resolve()
    (tmp_path / "pass").write_text("x")
    (tmp_path / "hook").write_text(HOOK)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--ssh-public-key",
            pubkey,
            "--magma-dir",
            str(tmp_path / "nothing"),
            "--magma-passfile",
            str(tmp_path / "pass"),
            "--magma-licence-setup",
            str(tmp_path / "hook"),
            "--dry-run",
        ],
    )
    assert rc == 2 and "not a Magma installation" in capsys.readouterr().err


def test_smoke_never_looks_at_the_licence_setup(quiet_time, tmp_path):
    """#593: how the licence is met is the deployer's script's business. The smoke test sends
    nothing taken from it, and works the same with or without one."""
    hook = tmp_path / "hook"
    hook.write_text(HOOK + ": a line only the deployer knows\n")
    identity = {"BUILDER_MACHINE_ID": "b", "BUILDER_HOST_FP": "SHA256:b"}
    sent = []
    for setup in (str(hook), None):
        ec2, ssh, res, log = FakeEc2(), FakeSsh(), ba.Resources(), []
        res.security_group, res.key_pair = "sg", "kp"
        o = opts(magma_dir="/opt/magma", magma_passfile="/p", magma_licence_setup=setup)
        assert ba.smoke(ec2, ssh, o, res, "ami-new", identity, log.append) is True
        sent.append([c[2] for c in ssh.calls if c[0] == "run" and "ZAB_EOF" in c[2]])
    assert sent[0] == sent[1]
    assert not any("only the deployer knows" in s or str(hook) in s for s in sent[0])


def run_magma_smoke(tmp_path, *, magma_prints: str, unit: bool, active: bool, magma="1"):
    """`MAGMA_SMOKE` as the instance runs it, with a stand-in `magma` and `systemctl` on PATH
    and the unit directory moved under `tmp_path`."""
    bin_dir, units = tmp_path / "bin", tmp_path / "units"
    bin_dir.mkdir(exist_ok=True)
    units.mkdir(exist_ok=True)
    (bin_dir / "magma").write_text(f"#!/bin/sh\ncat >/dev/null\necho '{magma_prints}'\n")
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\nexit {0 if active else 3}\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    (units / "magma-licence.service").unlink(missing_ok=True)
    if unit:
        (units / "magma-licence.service").write_text("[Unit]\n")
    check = next(line for line in ba.REBOOT_SCRIPT.splitlines() if line.startswith("check()"))
    snippet = ba.MAGMA_SMOKE.replace("/etc/systemd/system/", f"{units}/")
    script = f'fail=0\n{check}\n{snippet}echo "SMOKE-RESULT $fail"\n'
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "MAGMA": magma}
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)  # noqa: S603, S607 - the module's own snippet
    return run.stdout


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs coreutils' timeout")
def test_magma_smoke_checks_that_magma_runs_and_that_the_hook_ran(tmp_path):
    """#593: the check is that Magma computes; an unlicensed Magma is reported as such; where
    the image has the licence setup's unit, it must have run at this boot."""
    out = run_magma_smoke(tmp_path, magma_prints="4", unit=False, active=False)
    assert "ok   magma -b runs print 2+2" in out and out.endswith("SMOKE-RESULT 0\n")
    assert "magma-licence" not in out  # an image without the licence setup
    out = run_magma_smoke(tmp_path, magma_prints="not licensed here", unit=False, active=False)
    assert "FAIL magma -b does not print 4 for print 2+2: is its licence accepted here?" in out
    assert out.endswith("SMOKE-RESULT 1\n")
    out = run_magma_smoke(tmp_path, magma_prints="4", unit=True, active=True)
    assert "ok   systemctl is-active magma-licence.service" in out
    assert out.endswith("SMOKE-RESULT 0\n")
    out = run_magma_smoke(tmp_path, magma_prints="4", unit=True, active=False)
    assert "FAIL systemctl is-active magma-licence.service" in out
    assert out.endswith("SMOKE-RESULT 1\n")
    out = run_magma_smoke(tmp_path, magma_prints="x", unit=True, active=False, magma="0")
    assert out == "SMOKE-RESULT 0\n"  # no Magma on the image: nothing checked


def test_smoke_runs_gp_without_f_to_see_any_gprc():
    """PM review on #60: every gp check used -f, which skips gprc, so a gprc that colours and
    truncates piped output (Ubuntu's /etc/gprc) was invisible. The plain check: 60 lines,
    no `[+++]`, no ESC."""
    assert 'out=$(echo "matid(60)" | gp -q 2>/dev/null)' in ba.SMOKE_SCRIPT
    assert 'check bash -c "$GP_PLAIN"' in ba.SMOKE_SCRIPT
    assert 'check as_agent bash -lc "$GP_PLAIN"' in ba.SMOKE_SCRIPT
    gp_plain = ba.SMOKE_SCRIPT.split("GP_PLAIN='", 1)[1].split("'\n", 1)[0]
    if shutil.which("gp") is None:
        pytest.skip("needs gp")
    with tempfile.TemporaryDirectory() as tmp:
        gprc = Path(tmp) / "gprc"
        gprc.write_text("colors = lightbg\nlines = 40\n")
        # GPRC=/dev/null switches gprc off; a GPRC that does not exist falls through to ~/.gprc
        # and /etc/gprc (gp(1)), so on a machine with Ubuntu's pari-gp the clean run would fail
        # for the wrong reason (PM review on #62)
        clean = subprocess.run(["bash", "-c", gp_plain], env={**os.environ, "GPRC": "/dev/null"})  # noqa: S603, S607
        bad = subprocess.run(["bash", "-c", gp_plain], env={**os.environ, "GPRC": str(gprc)})  # noqa: S603, S607
    assert clean.returncode == 0 and bad.returncode != 0


def test_a_second_signal_does_not_cut_the_cleanup_short(monkeypatch, pubkey, capsys, quiet_time):
    """PM note on #56: the handlers stayed installed during `finally`, so a second SIGTERM could
    abort the cleanup half-way (e.g. inside the group-deletion retry)."""
    import os
    import signal as sig

    ec2, ssh = FakeEc2(), FakeSsh(build_exit=3)

    def terminate(ids):
        ec2.calls.append(("terminate", list(ids)))
        os.kill(os.getpid(), sig.SIGTERM)  # a second signal, during cleanup: must be ignored

    ec2.terminate = terminate
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    rc = with_build(
        ba.main,
        [
            "--account-id",
            "123456789012",
            "--zab-tag",
            "v0.0.2",
            "--my-ip",
            "203.0.113.5",
            "--ssh-public-key",
            pubkey,
        ],
    )
    kinds = [c[0] for c in ec2.calls]
    assert rc == 1
    assert kinds[-4:] == [
        "terminate",
        "wait_terminated",
        "delete_key_pair",
        "delete_security_group",
    ]
    assert sig.getsignal(sig.SIGTERM) is not sig.SIG_IGN  # restored afterwards


def test_files_tarball_carries_the_ami_files(tmp_path):
    """AMI v2: instructions, smoke sources and comparator-run reach the builder as one tarball,
    which the final cleanup removes."""
    path = ba.files_tarball(str(tmp_path / "f.tar.gz"))
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        mode = tar.getmember("bin/comparator-run").mode
    assert {
        "agent-instructions.md",
        "bin/comparator-run",
        "smoke/smoke.tex",
        "smoke/smoke.bib",
        "smoke/Smoke.lean",
        "smoke/comparator/config.json",
    } <= names
    assert mode & 0o111
    assert "ami-files.tar.gz" in ba.FINAL_CLEANUP


def test_smoke_script_checks_the_v2_additions():
    for needle in (
        "codex-code-mode-host",
        "mcp get lean-lsp",
        "lean4@lean4-skills",
        "latexmk -pdf",
        "time first Mathlib import",
        "comparator-run config.json",
        "Landlock ABI",
    ):
        assert needle in ba.SMOKE_SCRIPT, needle


def test_the_smoke_instance_runs_on_the_fleet_type():
    """D23: the fleet is m7a.2xlarge; the builder can stay m7a.xlarge (PM on #36)."""
    o = opts()
    assert (
        ba.instance_params(o, "ami", "sg", "kp", "zab-ami-builder")["InstanceType"] == "m7a.xlarge"
    )
    assert (
        ba.instance_params(o, "ami", "sg", "kp", "zab-ami-smoke")["InstanceType"] == "m7a.2xlarge"
    )


def test_the_image_wait_covers_a_v2_sized_snapshot():
    """v2's image was still pending after 30 minutes (Sat Oct 3); wait well beyond that, and
    finish before the builder's safety shutdown."""
    minutes = ba.IMAGE_WAIT["Delay"] * ba.IMAGE_WAIT["MaxAttempts"] / 60
    assert 60 <= minutes < ba.SAFETY_SHUTDOWN_MIN["zab-ami-builder"] - 60


def test_dry_run_of_smoke_only_plans_no_build(capsys, pubkey):
    """`--smoke-only --dry-run` printed the full build plan (Sat Oct 3)."""
    argv = [
        "--dry-run",
        "--account-id",
        "123456789012",
        "--zab-tag",
        "v0.0.2",
        "--ssh-public-key",
        pubkey,
    ]
    assert (
        with_build(
            ba.main, [*argv, "--smoke-only", "ami-0123456789abcdef0", "--my-ip", "203.0.113.5"]
        )
        == 0
    )
    out = capsys.readouterr()
    text = out.out + out.err
    assert "m7a.2xlarge instance from ami-0123456789abcdef0" in text
    assert "builder" not in text and "build.sh" not in text


def test_smoke_warm_runs_the_fleet_warm_up_first(quiet_time):
    """--smoke-warm: the first-boot warm-up (aws_common.WARM_VOLUME), timed, before the checks."""
    import aws_common as ac

    ssh = FakeSsh()
    res = ba.Resources(security_group="sg", key_pair="k")
    ba.smoke(FakeEc2(), ssh, opts(smoke_warm=True), res, "ami-new", {}, lambda m: None)
    runs = [c[2] for c in ssh.calls if c[0] == "run"]
    warm = next(i for i, r in enumerate(runs) if "time warm-up" in r)
    first_script = next(i for i, r in enumerate(runs) if "ZAB_EOF" in r)
    assert warm < first_script and runs[warm].startswith("sudo bash -c ")
    assert "/home/agent/lean" in runs[warm] and ba.WARM_VOLUME == ac.WARM_VOLUME


def test_the_smoke_script_runs_from_a_readable_directory():
    """The v3 smoke VM: ssh starts in /home/ubuntu (0750), and GAP.jl's initialisation stats a
    relative path, so `using Oscar` as agent failed with EACCES there; the script starts in /."""
    lines = [ln for ln in ba.SMOKE_SCRIPT.splitlines() if ln and not ln.startswith("#")]
    assert lines[:2] == ["set -u", "cd /"]


@pytest.mark.parametrize("missing", ["--tag", "--region", "--name"])
def test_main_refuses_a_build_without_its_scope_region_or_name(missing, capsys):
    """#808: no default, which would name the first deployment: argparse stops a build without
    `--tag` or `--region`, and `main` one without `--name`, before any AWS call."""
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--dry-run", "--tag",
            "zab-build=ami", "--region", "us-east-2", "--name", "zab-workshop-1"]  # fmt: skip
    at = argv.index(missing)
    with pytest.raises(SystemExit) as stop:
        ba.main(argv[:at] + argv[at + 2 :])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    if missing == "--name":
        assert "--name is required to build an image" in err
    else:
        assert f"the following arguments are required: {missing}" in err


def test_tag_option_gives_the_scope_and_is_required():
    """`--tag` (repeatable) scopes a build: a test build's is `zab-role=test` (test
    infrastructure). There is no default scope, which would name a deployment (#808)."""
    assert ba.parse_tag("zab-role=test") == ("zab-role", "test")
    with pytest.raises(argparse.ArgumentTypeError):
        ba.parse_tag("zab-role")
    o = opts()
    assert ba.scope_tags(o) == {"workshop": "nt-agents-2026"}
    with pytest.raises(SystemExit, match="give the build's scope"):
        ba.scope_tags(opts(tag=None))
    o.tag = [("zab-role", "test"), ("workshop", "zab-test")]
    tags = {
        t["Key"]: t["Value"]
        for t in ba.instance_params(o, "ami", "sg", "kp", "n")["TagSpecifications"][0]["Tags"]
    }
    assert tags == {"Name": "n", "zab-role": "test", "workshop": "zab-test", "role": "ami-build"}


def test_main_checks_the_macaulay2_packages_before_anything_in_aws(
    monkeypatch, pubkey, capsys, deps, quiet_time
):
    """#945: the pinned packages are fetched and checked first; a missing or changed one stops
    the build before any AWS call, with the message that names it. `--smoke-only` needs none."""

    def mismatch(opts, work, log, kind):
        raise m2_debs.DepsError("gfan_0.8~beta-2~ubuntu24.04.1.dsc (gfan): sha256 x, pinned y")

    monkeypatch.setattr(ba, "deps_tarball", mismatch)
    monkeypatch.setattr(ba, "Ec2", lambda *a: pytest.fail("no AWS call before the packages"))
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    assert with_build(ba.main, argv) == 2
    assert "(gfan): sha256 x, pinned y" in capsys.readouterr().err
    monkeypatch.setattr(ba, "deps_tarball", lambda *a: pytest.fail("--smoke-only fetches none"))
    ec2 = FakeEc2()
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: FakeSsh())
    with_build(ba.main, [*argv, "--my-ip", "203.0.113.5", "--smoke-only", "ami-0123"])


def test_main_passes_the_release_or_the_directory_on(
    monkeypatch, pubkey, capsys, deps, tmp_path, quiet_time
):
    ec2, ssh = FakeEc2(), FakeSsh(build_exit=3)  # stop after build.sh: enough to see the copy
    monkeypatch.setattr(ba, "Ec2", lambda region, profile: ec2)
    monkeypatch.setattr(ba, "Ssh", lambda key, known_hosts, log: ssh)
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    argv += ["--my-ip", "203.0.113.5"]
    with_build(ba.main, [*argv, "--deps-release", "o/r@deps/x"])
    with_build(ba.main, [*argv, "--deps-dir", str(tmp_path), "--pari-data-release", "o/r@p"])
    with_build(ba.main, [*argv, "--pari-data-dir", str(tmp_path)])
    assert deps == [  # a directory needs no release
        ("m2_debs", "o/r@deps/x", None),
        ("pari_data", f"o/r@{m2_debs.PARI_RELEASE}", None),
        ("m2_debs", None, str(tmp_path)),
        ("pari_data", "o/r@p", None),
        ("m2_debs", f"o/r@{m2_debs.RELEASE}", None),
        ("pari_data", None, str(tmp_path)),
    ]
    assert ("copy", "198.51.100.7", "m2-debs.tar", "m2-debs.tar") in ssh.calls
    assert ("copy", "198.51.100.7", "pari-data.tar", "pari-data.tar") in ssh.calls
    stream = next(c for c in ssh.calls if c[0] == "stream")[2]
    assert "M2_DEBS=/home/ubuntu/m2-debs.tar" in stream
    assert "PARI_DATA=/home/ubuntu/pari-data.tar" in stream


def test_deps_tarball_holds_the_checked_files(monkeypatch, tmp_path):
    """The files m2_debs gives, checked, go into one tarball at its top level, for M2_DEBS."""
    seen = []

    def give(source, dest, *rest, kind=m2_debs.M2):
        seen.append((source, kind.attr))
        (dest / "a~1.deb").write_bytes(b"a")
        (dest / "a~1.dsc").write_bytes(b"d")
        return [dest / "a~1.deb", dest / "a~1.dsc"]

    monkeypatch.setattr(ba.m2_debs, "fetch", give)
    monkeypatch.setattr(ba.m2_debs, "from_dir", give)
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    log: list[str] = []
    tar = DEPS_TARBALL(opts(), tmp_path / "w1", log.append)  # the release, with gh
    with tarfile.open(tar) as t:
        assert sorted(t.getnames()) == ["a~1.deb", "a~1.dsc"]
        assert t.extractfile("a~1.dsc").read() == b"d"
    DEPS_TARBALL(opts(deps_dir="/copy"), tmp_path / "w2", log.append)
    (tmp_path / "w3").mkdir()
    pari = DEPS_TARBALL(opts(), tmp_path / "w3", log.append, m2_debs.PARI_DATA)
    assert Path(pari).name == "pari-data.tar"
    assert seen == [
        (m2_debs.RELEASE, "m2_debs"),
        (Path("/copy"), "m2_debs"),
        (m2_debs.PARI_RELEASE, "pari_data"),
    ]
    assert f"release {m2_debs.RELEASE} (gh)" in log[0] and "from /copy" in log[2]
    assert f"PARI's pinned data packages from release {m2_debs.PARI_RELEASE}" in log[4]


def test_the_release_is_resolved_and_checked_before_anything_else(
    monkeypatch, pubkey, capsys, deps
):
    """Review of #950: the default release is origin's; without an origin, or with a malformed
    `--deps-release`, the build stops with exit 2, a dry run too, before any AWS call."""
    monkeypatch.setattr(ba, "Ec2", lambda *a: pytest.fail("no AWS call"))
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    assert with_build(ba.main, [*argv, "--dry-run"]) == 0
    assert f"release o/r@{m2_debs.RELEASE} (with gh)" in capsys.readouterr().out
    assert with_build(ba.main, [*argv, "--dry-run", "--deps-release", "not a release"]) == 2
    assert "is not OWNER/REPO@TAG" in capsys.readouterr().err

    def no_origin(*a):
        raise m2_debs.DepsError("no GitHub origin …; pass --deps-release OWNER/REPO@TAG")

    monkeypatch.setattr(ba.m2_debs, "origin_repo", no_origin)
    assert with_build(ba.main, argv) == 2
    assert "pass --deps-release" in capsys.readouterr().err
    assert deps == []  # nothing fetched


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://x-access-token:ghp_FAKETOKEN0123@github.com/someone/zab.git", 0),
        ("https://ghp_FAKETOKEN0123@github.com/someone/zab.git", 0),
        ("https://ghp_FAKETOKEN0123@gitlab.com/someone/zab.git", 2),
    ],
)
def test_an_origin_with_a_token_never_reaches_the_output(monkeypatch, pubkey, capsys, url, code):
    """Review round 2 on #950: an origin carrying credentials resolves without them, or is
    refused without showing them; the build log never holds the token."""
    monkeypatch.setattr(ba.m2_debs, "origin_repo", ORIGIN_REPO)  # the real one, with a fake git

    def git(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, url + "\n", "")

    monkeypatch.setattr(ba.m2_debs.subprocess, "run", git)
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    assert with_build(ba.main, [*argv, "--dry-run"]) == code
    out = capsys.readouterr()
    assert "FAKETOKEN" not in out.out + out.err
    if code == 0:
        assert f"release someone/zab@{m2_debs.RELEASE} (with gh)" in out.out


def test_the_pari_data_release_is_resolved_like_the_macaulay2_one(
    monkeypatch, pubkey, capsys, deps
):
    """Review of #952, B1: no origin and no --pari-data-release (the Macaulay2 set given) stops the
    build before AWS, naming the PARI flag; --pari-data-dir needs no release."""
    monkeypatch.setattr(ba, "Ec2", lambda *a: pytest.fail("no AWS call"))

    def no_origin(run=None, flag="--deps-release"):
        raise m2_debs.DepsError(f"no GitHub origin …; pass {flag} OWNER/REPO@TAG")

    monkeypatch.setattr(ba.m2_debs, "origin_repo", no_origin)
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    argv += ["--dry-run", "--deps-release", "o/r@deps/x"]
    assert with_build(ba.main, argv) == 2
    assert "pass --pari-data-release" in capsys.readouterr().err
    assert with_build(ba.main, [*argv, "--pari-data-dir", "/copy"]) == 0
    assert "PARI's data packages from /copy" in capsys.readouterr().out


def test_the_pari_sets_refusal_never_shows_the_origins_token(monkeypatch, pubkey, capsys):
    """Review round 2 on #950, on #952's path: the Macaulay2 release given, the PARI set's comes
    from an origin that carries a token and isn't GitHub's; the refusal names --pari-data-release
    and never the token."""
    monkeypatch.setattr(ba.m2_debs, "origin_repo", ORIGIN_REPO)  # the real one, with a fake git
    url = "https://x-access-token:ghp_FAKETOKEN0123@gitlab.com/someone/zab.git"

    def git(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, url + "\n", "")

    monkeypatch.setattr(ba.m2_debs.subprocess, "run", git)
    argv = ["--account-id", "123456789012", "--zab-tag", "v0.0.2", "--ssh-public-key", pubkey]
    assert with_build(ba.main, [*argv, "--dry-run", "--deps-release", "o/r@deps/x"]) == 2
    out = capsys.readouterr()
    assert "pass --pari-data-release" in out.err and "FAKETOKEN" not in out.out + out.err
