"""The rig's AWS side and its guard, offline with a fake EC2 and IAM (issue #356)."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest

from e2e.admin.fakes import FakeEc2, FakeError, FakeIam, test_policy
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.aws_rig import (
    PROBE_ENI_DESCRIPTION,
    VOLUMES_S,
    AwsRig,
    console_fingerprints,
    fingerprint,
)
from e2e.admin.harness.gate import Aws, Gate, NotTestInfrastructure, ReadOnly, check_policy, shown

POLICY = [test_policy()]
KEY = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 36).decode()


def make(tmp_path, *, dry=False, ec2=None, iam=None, scan=lambda ip: "", clock=None):
    rig = rigs.load({"HOME": str(tmp_path)})
    rig.dir.mkdir(parents=True)
    lines: list[str] = []
    gate = Gate(dry_run=dry, out=lines.append)
    ec2, iam = ec2 or FakeEc2(), iam or FakeIam()
    ticks = iter(range(0, 10**6, 60))  # a fake clock: a wait times out at once, never for real
    sleeps = iter(range(10_000))

    def sleep(seconds: float) -> None:  # a wait without a bound fails, never hangs
        if next(sleeps, None) is None:
            raise AssertionError("a wait without a bound")

    aws = AwsRig(Aws(gate, ec2, iam), rig, scan=scan, sleep=sleep,
                 clock=clock or (lambda: next(ticks)))  # fmt: skip
    return aws, ec2, iam, lines


def build(aws: AwsRig) -> dict:
    """The AWS half of the setup, in its order (`setup.Setup.run`)."""
    vpc = aws.vpc()
    tags = {k: v for k, v in rigs.SERVER_TAGS.items() if k != "Name"}
    server_group = aws.ensure_group(rigs.SERVER_GROUP, tags, vpc, aws.ssh_sources(vpc))
    groups = {ws.name: aws.ensure_group(ws.group, ws.tags, vpc) for ws in rigs.WORKSHOPS}
    fleet = aws.fleet_group(vpc)
    image = aws.test_image()
    aws.ensure_role(POLICY)
    inst = aws.server() or aws.launch_server(server_group)
    return {"group": server_group, "groups": groups, "fleet": fleet, "image": image,
            "server": inst}  # fmt: skip


def test_a_fresh_account_dry_run_plans_every_change_and_makes_none(tmp_path):
    aws, ec2, iam, lines = make(tmp_path, dry=True)
    build(aws)
    assert ec2.writes() == [] and iam.writes() == []
    planned = "\n".join(aws.aws.gate.writes)
    for op in ("create_security_group", "authorize_security_group_ingress", "create_tags",
               "create_role", "put_role_policy", "create_instance_profile",
               "add_role_to_instance_profile", "run_instances"):  # fmt: skip
        assert op in planned
    assert planned.count("create_security_group") == 4  # the server's, 2 workshops', zab-fleet
    assert "GroupName=zab-test-admin-server" in planned and "203.0.113.7/32" in planned
    assert "PolicyDocument=<" in planned  # shown by its length, not printed
    assert any(line.startswith("  read   ec2 describe_vpcs") for line in lines)


def test_a_second_run_reuses_everything(tmp_path):
    aws, ec2, iam, _ = make(tmp_path)
    first = build(aws)
    assert first["server"]["InstanceId"].startswith("i-")
    tags = {t["Key"]: t["Value"] for t in ec2.images[0]["Tags"]}
    assert tags == {"zab-role": "test", "zab-scope": "admin-e2e"}  # §5's LaunchFrom (#422)
    launched = next(op for op in ec2.calls if op[0] == "run_instances")[1]
    assert launched["InstanceType"] == "t4g.small" and launched["DisableApiTermination"] is True
    assert launched["MetadataOptions"]["HttpTokens"] == "required"
    assert launched["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    ebs = launched["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["Encrypted"] and ebs["VolumeSize"] == 30 and ebs["VolumeType"] == "gp3"
    instance_tags = {t["Key"]: t["Value"] for t in launched["TagSpecifications"][0]["Tags"]}
    assert "workshop" not in instance_tags and instance_tags["zab-admin"] == "e2e"
    before = (len(ec2.writes()), len(iam.writes()))
    second = build(aws)
    assert (len(ec2.writes()), len(iam.writes())) == before
    assert second["server"]["InstanceId"] == first["server"]["InstanceId"]
    assert second["groups"] == first["groups"]
    assert second["fleet"] == first["fleet"]


def test_the_region_is_set_up_with_one_fleet_group_new_workshop_finds(tmp_path):
    """A16 (#418): `zab-fleet`, in the rig's scope and in no workshop, SSH from the development
    machine only. The lookup `new_workshop.py` makes (the scope's tags and the name) finds it alone:
    the workshops' groups carry the scope too, so the scope alone would find three."""
    aws, ec2, _, _ = make(tmp_path)
    fleet = build(aws)["fleet"]
    group = next(g for g in ec2.groups if g["GroupId"] == fleet)
    assert group["GroupName"] == "zab-fleet"
    assert {t["Key"]: t["Value"] for t in group["Tags"]} == {
        "zab-scope": "admin-e2e", "zab-role": "test"
    }  # fmt: skip
    cidrs = [r["CidrIp"] for p in group["IpPermissions"] for r in p["IpRanges"]]
    assert cidrs == ["203.0.113.7/32"]  # `zab-test-ssh`'s: the development machine, not anywhere
    assert [p["FromPort"] for p in group["IpPermissions"]] == [22]
    scope = [{"Name": f"tag:{k}", "Values": [v]} for k, v in rigs.FLEET_TAGS.items()]
    assert len(ec2.describe_security_groups(Filters=scope)["SecurityGroups"]) == 3
    named = [*scope, {"Name": "group-name", "Values": ["zab-fleet"]}]
    found = ec2.describe_security_groups(Filters=named)["SecurityGroups"]
    assert [g["GroupId"] for g in found] == [fleet]


def test_a_changed_policy_is_put_and_a_stale_one_deleted(tmp_path):
    aws, _, iam, _ = make(tmp_path)
    aws.ensure_role(POLICY + POLICY)
    fewer = {**POLICY[0], "Statement": POLICY[0]["Statement"][:-1]}  # LaunchNetwork left out
    aws.ensure_role([fewer])
    assert set(iam.policies) == {("zab-test-admin", "zab-test-admin-1")}
    assert [op for op, _ in iam.calls if op == "delete_role_policy"] == ["delete_role_policy"]


def test_the_participants_group_follows_the_server_address(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    vpc = aws.vpc()
    aws.allow_server(vpc, "198.51.100.1")
    aws.allow_server(vpc, "198.51.100.2")
    group = next(g for g in ec2.groups if g["GroupName"] == rigs.E2E.group)
    cidrs = [r["CidrIp"] for p in group["IpPermissions"] for r in p["IpRanges"]]
    assert cidrs == ["198.51.100.2/32"]
    # A18's VM: the server, and the development machine's break-glass key (`zab-test-ssh`'s source)
    group = next(g for g in ec2.groups if g["GroupName"] == rigs.E2E2.group)
    cidrs = [r["CidrIp"] for p in group["IpPermissions"] for r in p["IpRanges"]]
    assert sorted(cidrs) == ["198.51.100.2/32", "203.0.113.7/32"]
    assert {t["Key"]: t["Value"] for t in group["Tags"]} == rigs.E2E2.tags
    # a stray IPv6 rule on port 22, in either group, is removed (review of #381)
    for name in (rigs.E2E.group, rigs.E2E2.group):
        stray = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [],
                 "Ipv6Ranges": [{"CidrIpv6": "2001:db8::/32"}]}  # fmt: skip
        next(g for g in ec2.groups if g["GroupName"] == name)["IpPermissions"].append(stray)
    aws.allow_server(vpc, "198.51.100.2")
    for name in (rigs.E2E.group, rigs.E2E2.group):
        group = next(g for g in ec2.groups if g["GroupName"] == name)
        assert [r for p in group["IpPermissions"] for r in p.get("Ipv6Ranges", [])] == [], name


def test_a_wide_ssh_source_is_never_copied_into_the_rigs_groups(tmp_path):
    """`zab-test-ssh`'s sources go into the server's group and e2e2's: one address each."""
    aws, ec2, _, _ = make(tmp_path)
    wide = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
            "IpRanges": [{"CidrIp": "198.51.100.0/24"}]}  # fmt: skip
    ec2.groups[0]["IpPermissions"].append(wide)  # zab-test-ssh's
    with pytest.raises(RuntimeError, match=r"198\.51\.100\.0/24, wider than one address"):
        aws.allow_server(aws.vpc(), "198.51.100.2")
    authorized = [p for op, p in ec2.calls if op == "authorize_security_group_ingress"]
    assert "198.51.100.0/24" not in str(authorized)
    assert all(g["GroupName"] != rigs.E2E2.group for g in ec2.groups)  # never touched


def test_an_image_of_another_scope_is_refused(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    ec2.images[0]["Tags"].append({"Key": "zab-scope", "Value": "mathagents"})
    with pytest.raises(RuntimeError, match="in another scope"):
        aws.test_image()
    assert ec2.writes() == []


def test_the_gates_refusal_is_never_reworded_as_another_scope(tmp_path, monkeypatch):
    """Review of #426: only a mistagged image reads "in another scope"; the gate's refusal (a
    RuntimeError too) and AWS's errors go through as they are."""
    aws, _, _, _ = make(tmp_path)

    def refused(*args, **kwargs):
        raise NotTestInfrastructure("refused by the gate")

    monkeypatch.setattr(aws, "add_tags", refused)
    with pytest.raises(NotTestInfrastructure, match="refused by the gate"):
        aws.test_image()


def test_what_a_run_before_the_scope_made_is_scoped(tmp_path):
    """#422: a group, the image and the server's key pairs made before the fleet-wide scope get
    `zab-scope=admin-e2e` added, their other tags kept; one in another scope is refused."""
    aws, ec2, _, _ = make(tmp_path)
    old = {"workshop": rigs.E2E.name, "zab-role": "test"}
    ec2.images[0]["Tags"].append({"Key": "workshop", "Value": rigs.E2E.name})
    vpc = aws.vpc()
    gid = aws.ensure_group(rigs.E2E.group, old, vpc)
    ec2.key_pairs = [{"KeyPairId": "key-0e2e", "KeyName": rigs.E2E.key_name, "Tags": tags(**old)}]
    aws.ensure_group(rigs.E2E.group, rigs.E2E.tags, vpc)
    aws.test_image()
    aws.scope_key_pairs()
    group = next(g for g in ec2.groups if g["GroupId"] == gid)
    for found in (group, ec2.images[0], ec2.key_pairs[0]):
        have = {t["Key"]: t["Value"] for t in found["Tags"]}
        assert have["zab-scope"] == "admin-e2e" and have["zab-role"] == "test", found
    assert {t["Key"]: t["Value"] for t in ec2.key_pairs[0]["Tags"]} == rigs.E2E.tags
    before = len(ec2.writes())
    aws.ensure_group(rigs.E2E.group, rigs.E2E.tags, vpc)
    aws.test_image()
    aws.scope_key_pairs()
    assert len(ec2.writes()) == before  # a second run adds nothing
    group["Tags"] = [t for t in group["Tags"] if t["Key"] != "zab-scope"]
    group["Tags"].append({"Key": "zab-scope", "Value": "mathagents"})
    with pytest.raises(RuntimeError, match="fix it by hand"):
        aws.ensure_group(rigs.E2E.group, rigs.E2E.tags, vpc)


def tags(**pairs):
    """EC2's tag list: `zab_role="test"` is `zab-role=test`."""
    return [{"Key": k.replace("_", "-"), "Value": v} for k, v in pairs.items()]


def test_teardown_covers_what_has_no_participant_tag(tmp_path):
    """Reviewer on #363: an instance the code under test launched with the scope tags but no
    `participant` tag, its address and its volume, are torn down too; the server never. Review
    of #368: nor an address or volume of another workshop (#263's test VM is `workshop=zab-test`,
    `zab-role=test`) or of none, the server's address, or a volume still in use."""
    aws, ec2, _, _ = make(tmp_path)
    scope = {"workshop": "zab-admin-e2e2", "zab-role": "test"}
    ec2.add_instance("i-stray", scope)
    ec2.add_instance("i-server", {"zab-role": "test", "zab-admin": "e2e"})
    tagged = tags(workshop="zab-admin-e2e2", zab_role="test")
    server = [*tagged, *tags(zab_admin="e2e")]
    other, none = tags(workshop="zab-test", zab_role="test"), tags(zab_role="test")
    ec2.addresses = [
        {"AllocationId": "eipalloc-stray", "Tags": tagged},
        {"AllocationId": "eipalloc-263", "Tags": other},
        {"AllocationId": "eipalloc-none", "Tags": none},
        {"AllocationId": "eipalloc-server", "Tags": server},
    ]
    ec2.volumes = [
        {"VolumeId": "vol-stray", "State": "available", "Tags": tagged},
        {"VolumeId": "vol-gone", "State": "deleting", "Tags": tagged},
        {"VolumeId": "vol-server", "State": "in-use", "Tags": server},
        {"VolumeId": "vol-263", "State": "available", "Tags": other},
        {"VolumeId": "vol-none", "State": "available", "Tags": none},
        {"VolumeId": "vol-attached", "State": "in-use", "Tags": tagged},
    ]
    assert [i["InstanceId"] for i in aws.fleet()] == ["i-stray"] and aws.participants() == []
    aws.terminate_participants()
    states = {i["InstanceId"]: i["State"]["Name"] for i in ec2.instances}
    assert states == {"i-stray": "terminated", "i-server": "running"}
    ended = [(op, p) for op, p in ec2.calls if op in ("release_address", "delete_volume")]
    assert ended == [("release_address", {"AllocationId": "eipalloc-stray"}),
                     ("delete_volume", {"VolumeId": "vol-stray"})]  # fmt: skip
    assert [a["AllocationId"] for a in ec2.addresses] == [
        "eipalloc-263", "eipalloc-none", "eipalloc-server"
    ]  # fmt: skip
    assert [v["VolumeId"] for v in ec2.volumes] == [
        "vol-gone", "vol-server", "vol-263", "vol-none", "vol-attached"
    ]  # fmt: skip
    assert aws.fleet() == [] and aws.addresses() == []
    assert [v["VolumeId"] for v in aws.volumes()] == ["vol-attached"]  # in use: reported


def test_the_teardown_check_sees_an_instance_without_a_participant_tag(tmp_path):
    """A stray instance teardown could not terminate is reported, by the real AwsRig."""
    from e2e.admin.run import teardown

    class Stuck(FakeEc2):
        def terminate_instances(self, **p):
            self._log("terminate_instances", p)  # accepted, never done

    aws, ec2, _, _ = make(tmp_path, ec2=Stuck())
    ec2.add_instance("i-stray", {"workshop": "zab-admin-e2e", "zab-role": "test"})
    problems = teardown(aws, keep_server=True)
    assert "teardown check: instances not terminated: i-stray" in problems


class Clock:
    """Fake time, moved on by `sleep` alone; a wait without a bound fails, never hangs."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now > 3600:
            raise AssertionError("a wait without a bound")


class Detaching(FakeEc2):
    """EC2 as it ends a terminated instance's volumes (review of #368), on a fake clock: each
    still shows `in-use` for `lag` seconds, then is gone, or `available` if `kept` on
    termination; a volume deleted still shows `available` for `lag` seconds."""

    def __init__(self, clock: Clock, lag: float = 45) -> None:
        super().__init__()
        self.clock, self.lag = clock, lag
        self.attached: dict[str, str] = {}  # volume → its instance
        self.kept: set[str] = set()
        self.later: dict[str, tuple[float, str | None]] = {}  # volume → (when, its state then)

    def describe_volumes(self, **p):
        for vid, (when, state) in list(self.later.items()):
            if self.clock() >= when:
                del self.later[vid]
                vol = next(v for v in self.volumes if v["VolumeId"] == vid)
                if state is None:
                    self.volumes.remove(vol)
                else:
                    vol["State"] = state
        return super().describe_volumes(**p)

    def terminate_instances(self, **p):
        super().terminate_instances(**p)
        for vid, iid in self.attached.items():
            if iid in p["InstanceIds"]:
                then = "available" if vid in self.kept else None
                self.later[vid] = (self.clock() + self.lag, then)

    def delete_volume(self, **p):
        self._log("delete_volume", p)
        self.later[p["VolumeId"]] = (self.clock() + self.lag, None)


def detaching(tmp_path):
    clock = Clock()
    aws, ec2, _, _ = make(tmp_path, ec2=Detaching(clock), clock=clock)
    aws.sleep = clock.sleep
    return aws, ec2, clock


def test_teardown_waits_for_ec2_to_let_go_of_a_terminated_instances_volumes(tmp_path):
    """Review of #368: right after a termination EC2 may still show its volumes in use. The
    teardown waits for each to go (or, kept on termination, to be available, and deletes it);
    its check waits for a volume just deleted to vanish, before it reports one."""
    from e2e.admin.run import teardown

    aws, ec2, clock = detaching(tmp_path)
    ec2.add_instance("i-a", {"workshop": "zab-admin-e2e", "zab-role": "test", "participant": "a"})
    for vid in ("vol-root", "vol-kept"):
        ec2.volumes.append({"VolumeId": vid, "State": "in-use",
                            "Tags": tags(workshop="zab-admin-e2e", zab_role="test")})  # fmt: skip
        ec2.attached[vid] = "i-a"
    ec2.kept = {"vol-kept"}
    assert teardown(aws, keep_server=True) == []
    assert ec2.volumes == []
    assert [p for op, p in ec2.calls if op == "delete_volume"] == [{"VolumeId": "vol-kept"}]
    assert clock.now <= 2 * VOLUMES_S


def test_a_volume_still_in_use_is_waited_for_within_bounds_then_reported(tmp_path):
    from e2e.admin.run import teardown

    aws, ec2, clock = detaching(tmp_path)
    ec2.volumes = [{"VolumeId": "vol-busy", "State": "in-use",
                    "Tags": tags(workshop="zab-admin-e2e2", zab_role="test")}]  # fmt: skip
    assert teardown(aws, keep_server=True) == ["teardown check: volumes not deleted: vol-busy"]
    assert ec2.writes() == [] and [v["State"] for v in ec2.volumes] == ["in-use"]
    assert 2 * VOLUMES_S <= clock.now <= 2 * (VOLUMES_S + 10)  # the teardown's, then the check's


def test_teardown_lifts_protection_terminates_and_releases(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    scope = {"workshop": "zab-admin-e2e", "zab-role": "test"}
    ec2.add_instance("i-a", {**scope, "participant": "zab-test-a"})
    ec2.add_instance("i-b", {**scope, "participant": "zab-test-b"}, state="stopped")
    ec2.add_instance("i-untagged", {"workshop": "zab-admin-e2e", "participant": "x"})
    ec2.add_instance("i-server", {**scope, "participant": "y", "zab-admin": "e2e"})
    ec2.protected = {"i-a"}
    eip_tags = [{"Key": k, "Value": v} for k, v in {**scope, "participant": "zab-test-a"}.items()]
    ec2.addresses = [{"AllocationId": "eipalloc-a", "InstanceId": "i-a", "Tags": eip_tags}]
    aws.terminate_participants()
    states = {i["InstanceId"]: i["State"]["Name"] for i in ec2.instances}
    assert states == {"i-a": "terminated", "i-b": "terminated", "i-untagged": "running",
                      "i-server": "running"}  # fmt: skip
    assert ec2.addresses == []
    assert aws.participants() == []


def test_the_teardown_ends_and_checks_a18s_vm(tmp_path):
    """A18's `zab-test-c`, launched from the development machine by `launch.py` (the rig's key pair,
    the second workshop's scope, its Elastic IP), is ended by every run's teardown and checked."""
    from e2e.admin.run import teardown

    aws, ec2, _, _ = make(tmp_path)
    tagged = {**rigs.E2E2.tags, "participant": "zab-test-c", "Name": "ws-zab-test-c"}
    ec2.add_instance("i-c", tagged)
    ec2.add_instance("i-server", {"zab-role": "test", "zab-admin": "e2e"})
    eip_tags = [{"Key": k, "Value": v} for k, v in tagged.items()]
    ec2.addresses = [{"AllocationId": "eipalloc-c", "InstanceId": "i-c", "Tags": eip_tags}]
    assert [i["InstanceId"] for i in aws.participants()] == ["i-c"]
    assert teardown(aws, keep_server=True) == []
    states = {i["InstanceId"]: i["State"]["Name"] for i in ec2.instances}
    assert states == {"i-c": "terminated", "i-server": "running"} and ec2.addresses == []


def test_the_gate_refuses_what_is_not_test_infrastructure(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    with pytest.raises(NotTestInfrastructure, match="names no resource"):
        aws.aws.ec2("stop_instances", InstanceIds=["i-1"])
    with pytest.raises(NotTestInfrastructure, match="not tagged zab-role=test"):
        aws.aws.ec2(
            "stop_instances", guarded={"InstanceId": "i-1", "Tags": []}, InstanceIds=["i-1"]
        )
    with pytest.raises(NotTestInfrastructure, match="without zab-role=test"):
        spec = {"ResourceType": "security-group", "Tags": [{"Key": "Name", "Value": "x"}]}
        aws.aws.ec2("create_security_group", GroupName="x", TagSpecifications=[spec])
    with pytest.raises(NotTestInfrastructure, match="zab-test-"):
        aws.aws.iam("create_role", RoleName="admin")
    assert ec2.writes() == []
    assert shown({"api_key": "s3cret", "PolicyDocument": "{...}"}) == (
        "api_key=<6 chars> PolicyDocument=<5 chars>"
    )


def test_the_host_key_is_pinned_from_the_console_never_trusted_on_first_use(tmp_path):
    other = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"z" * 36).decode()
    aws, ec2, _, _ = make(tmp_path, scan=lambda ip: f"# banner\n{ip} {other}\n")
    ec2.console = (
        "-----BEGIN SSH HOST KEY FINGERPRINTS-----\n256 "
        + fingerprint(KEY)
        + (" root@ip (ED25519)\n-----END SSH HOST KEY FINGERPRINTS-----\n")
    )
    inst = {"InstanceId": "i-s", "Tags": [{"Key": "zab-role", "Value": "test"}]}
    with pytest.raises(RuntimeError, match="not the one its console printed"):
        aws.pin_host_key(inst, "198.51.100.5")
    aws.scan = lambda ip: f"{ip} {KEY}\n"
    assert aws.pin_host_key(inst, "198.51.100.5") == KEY
    assert aws.rig.known_hosts.read_text() == f"198.51.100.5 {KEY}\n"
    aws.scan = lambda ip: pytest.fail("a pinned key is never scanned again")
    aws.pin_host_key(inst, "198.51.100.6")
    assert aws.rig.known_hosts.read_text() == f"198.51.100.6 {KEY}\n"
    assert oct(aws.rig.host_key.stat().st_mode & 0o777) == "0o600"


def test_fingerprints_as_ssh_keygen_prints_them():
    blob = base64.b64decode(KEY.split()[1])
    want = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert fingerprint(KEY) == want
    assert console_fingerprints("no block") == set()


def test_a_dry_run_leaves_the_pinned_key_and_known_hosts_alone(tmp_path):
    """Codex round 2 on #363: the dry run wrote `<its address> …` over a valid known_hosts."""
    aws, _, _, lines = make(tmp_path, dry=True, scan=lambda ip: pytest.fail("no scan"))
    aws.rig.host_key.write_text(KEY + "\n")
    aws.rig.known_hosts.write_text(f"198.51.100.5 {KEY}\n")
    inst = {"InstanceId": "i-s", "Tags": [{"Key": "zab-role", "Value": "test"}]}
    assert aws.pin_host_key(inst, "<its address>") == KEY
    assert aws.rig.known_hosts.read_text() == f"198.51.100.5 {KEY}\n"
    assert any("would write" in line and "known_hosts" in line for line in lines)


# --------------------------------------------------- the role's policy (review of #363, item 1)


def mutated(change):
    """The real policy with `change(statements)` applied."""
    import copy

    doc = copy.deepcopy(POLICY[0])
    change(doc["Statement"])
    return doc


def by_sid(statements, sid):
    return next(st for st in statements if st["Sid"] == sid)


def by_request_tag(action, resource="*"):
    """A change adding an Allow of `action` scoped by `aws:RequestTag/zab-role` alone."""
    condition = {"StringEquals": {"aws:RequestTag/zab-role": "test"}}

    def change(statements):
        statements.append({"Sid": "X", "Effect": "Allow", "Action": f"ec2:{action}",
                           "Resource": resource, "Condition": condition})  # fmt: skip

    return change


BAD_POLICIES = {
    "Allow * on *": lambda sts: sts.append(
        {"Sid": "X", "Effect": "Allow", "Action": "*", "Resource": "*"}
    ),
    "iam:PassRole": lambda sts: sts.append(
        {"Sid": "X", "Effect": "Allow", "Action": "iam:PassRole", "Resource": "*"}
    ),
    "ec2:*": lambda sts: sts.append(
        {"Sid": "X", "Effect": "Allow", "Action": "ec2:*", "Resource": "*"}
    ),
    "an unconditioned TerminateInstances": lambda sts: sts.append(
        {
            "Sid": "X",
            "Effect": "Allow",
            "Action": "ec2:TerminateInstances",
            "Resource": "arn:aws:ec2:us-east-1:*:instance/*",
        }
    ),
    "CreateTags by request tag without CreateAction": lambda sts: sts.append(
        {
            "Sid": "X",
            "Effect": "Allow",
            "Action": "ec2:CreateTags",
            "Resource": "*",
            "Condition": {"StringEquals": {"aws:RequestTag/zab-role": "test"}},
        }
    ),
    "NotAction": lambda sts: sts.append(
        {"Sid": "X", "Effect": "Allow", "NotAction": "iam:*", "Resource": "*"}
    ),
    "an unscoped DescribeInstanceAttribute": lambda sts: sts.append(
        {"Sid": "X", "Effect": "Allow", "Action": "ec2:DescribeInstanceAttribute", "Resource": "*"}
    ),
    "zab-role on two values": lambda sts: by_sid(sts, "UsEast1Fleet")["Condition"][
        "StringEquals"
    ].__setitem__("aws:ResourceTag/zab-role", ["test", "prod"]),
    "a deny left out": lambda sts: sts.remove(by_sid(sts, "DenyServer")),
    # what a request tag alone may scope: creating calls, on what each creates (review of #363)
    **{
        f"{action} by a request tag alone": by_request_tag(action)
        for action in ("CreateImage", "CreateSnapshot", "AuthorizeSecurityGroupIngress")
    },
    "RunInstances by a request tag on any image": by_request_tag("RunInstances"),
    "RunInstances beside ImportKeyPair, on any key pair": lambda sts: sts.append(
        {
            "Sid": "X",
            "Effect": "Allow",
            "Action": ["ec2:RunInstances", "ec2:ImportKeyPair"],
            "Resource": "arn:aws:ec2:us-east-1:*:key-pair/*",
            "Condition": {"StringEquals": {"aws:RequestTag/zab-role": "test"}},
        }
    ),
    "RunInstances of any instance type": lambda sts: by_sid(sts, "UsEast1Launch")["Condition"][
        "StringEquals"
    ].pop("ec2:InstanceType"),
    # review of #368: the same when it shares its statement with CreateTags at RunInstances
    "RunInstances beside CreateTags, of any instance type": lambda sts: sts.append(
        {
            "Sid": "X",
            "Effect": "Allow",
            "Action": ["ec2:RunInstances", "ec2:CreateTags"],
            "Resource": "arn:aws:ec2:us-east-1:*:instance/*",
            "Condition": {
                "StringEquals": {
                    "aws:RequestTag/zab-role": "test",
                    "ec2:CreateAction": "RunInstances",
                }
            },
        }
    ),
    "AllocateAddress by a request tag on instances": by_request_tag(
        "AllocateAddress", "arn:aws:ec2:us-east-1:*:instance/*"
    ),
    "CreateTags at CreateSnapshot": lambda sts: by_sid(sts, "UsEast1TagAtCreation")["Condition"][
        "StringEquals"
    ].__setitem__("ec2:CreateAction", ["RunInstances", "CreateSnapshot"]),
    "a hollowed-out DenyScopeRetag": lambda sts: by_sid(sts, "DenyScopeRetag").__setitem__(
        "Resource", "arn:aws:ec2:*:*:nothing/*"
    ),
    "DenyScopeRetag without zab-admin": lambda sts: by_sid(sts, "DenyScopeRetag")["Condition"][
        "ForAnyValue:StringEqualsIgnoreCase"
    ].__setitem__("aws:TagKeys", ["workshop", "zab-role", "zab-scope"]),
    # #422: the fleet-wide scope is kept from a retag, and in every scoped Allow
    "DenyScopeRetag without zab-scope": lambda sts: by_sid(sts, "DenyScopeRetag")["Condition"][
        "ForAnyValue:StringEqualsIgnoreCase"
    ].__setitem__("aws:TagKeys", ["workshop", "zab-role", "zab-admin"]),
    "the fleet without the rig's scope": lambda sts: by_sid(sts, "UsEast1Fleet")["Condition"][
        "StringEquals"
    ].pop("aws:ResourceTag/zab-scope"),
    "a launch in another scope": lambda sts: by_sid(sts, "UsEast1Launch")["Condition"][
        "StringEquals"
    ].__setitem__("aws:RequestTag/zab-scope", "mathagents"),
    "DenyServer on another tag": lambda sts: by_sid(sts, "DenyServer").__setitem__(
        "Condition", {"Null": {"aws:ResourceTag/zab-adminx": "false"}}
    ),
    "DenyUntag on CreateTags": lambda sts: by_sid(sts, "DenyUntag").__setitem__(
        "Action", "ec2:CreateTags"
    ),
    # #356's V4 check: LaunchNetwork's interfaces are only those a launch creates
    "no deny of an existing interface": lambda sts: sts.remove(
        by_sid(sts, "DenyExistingInterface")
    ),
    "DenyExistingInterface on one region only": lambda sts: by_sid(
        sts, "DenyExistingInterface"
    ).__setitem__("Resource", "arn:aws:ec2:us-east-1:*:network-interface/eni-*"),
    "DenyExistingInterface with a condition": lambda sts: by_sid(
        sts, "DenyExistingInterface"
    ).__setitem__("Condition", {"Null": {"aws:ResourceTag/workshop": "false"}}),
}


def test_the_rendered_policy_passes_the_check():
    check_policy(POLICY[0])
    # A21's `!grow-disk` (#553): the rig's role grows only a volume with the test tag
    [grow] = [st for st in POLICY[0]["Statement"] if st["Action"] == "ec2:ModifyVolume"]
    assert grow["Condition"]["StringEquals"]["aws:ResourceTag/zab-role"] == "test"
    assert grow["Resource"].endswith(":volume/*")


@pytest.mark.parametrize("what", sorted(BAD_POLICIES))
def test_a_policy_outside_the_test_scope_is_refused_before_any_iam_write(tmp_path, what):
    aws, _, iam, _ = make(tmp_path)
    with pytest.raises(NotTestInfrastructure):
        aws.ensure_role([mutated(BAD_POLICIES[what])])
    assert iam.writes() == [], what
    aws.ensure_role(POLICY)  # a role that exists: the gate checks each put too
    with pytest.raises(NotTestInfrastructure):
        aws.aws.iam("put_role_policy", RoleName="zab-test-admin", PolicyName="zab-test-admin-1",
                    PolicyDocument=json.dumps(mutated(BAD_POLICIES[what])))  # fmt: skip


def test_a_managed_policy_on_the_role_is_refused(tmp_path):
    aws, _, iam, _ = make(tmp_path)
    aws.ensure_role(POLICY)
    iam.attached = [{"PolicyName": "AdministratorAccess", "PolicyArn": "arn:…"}]
    before = len(iam.writes())
    with pytest.raises(NotTestInfrastructure, match="AdministratorAccess"):
        aws.ensure_role(POLICY)
    assert len(iam.writes()) == before


# ----------------------------------------------------- the gate (review of #363, items 5 and 8)


def test_the_gate_ties_the_change_to_the_resource_it_checked(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    mine = {"InstanceId": "i-mine", "Tags": [{"Key": "zab-role", "Value": "test"}]}
    with pytest.raises(NotTestInfrastructure, match="i-mine was checked"):
        aws.aws.ec2("stop_instances", guarded=mine, InstanceIds=["i-drews"])
    with pytest.raises(NotTestInfrastructure, match="i-mine was checked"):
        aws.aws.ec2("stop_instances", guarded=mine, InstanceIds=["i-mine", "i-drews"])
    server = {"InstanceId": "i-s", "Tags": [{"Key": "zab-role", "Value": "test"},
                                            {"Key": "zab-admin", "Value": "e2e"}]}  # fmt: skip
    with pytest.raises(NotTestInfrastructure, match="admin server"):
        aws.aws.ec2("terminate_instances", guarded=server, InstanceIds=["i-s"])
    with pytest.raises(NotTestInfrastructure, match="admin server"):
        aws.aws.ec2("modify_instance_attribute", guarded=server, InstanceId="i-s",
                    DisableApiTermination={"Value": False})  # fmt: skip
    assert ec2.writes() == []
    aws.aws.ec2("stop_instances", guarded=server, InstanceIds=["i-s"])  # stopping it is the rig's
    assert ec2.writes() == ["stop_instances"]


SCOPE = {"workshop": "zab-admin-e2e2", "zab_role": "test"}
#: What ends a resource, with the parameter that names it.
ENDING = {
    "release_address": ("AllocationId", lambda rid: {"AllocationId": rid}),
    "delete_volume": ("VolumeId", lambda rid: {"VolumeId": rid}),
    "terminate_instances": ("InstanceId", lambda rid: {"InstanceIds": [rid]}),
    "modify_instance_attribute": (
        "InstanceId", lambda rid: {"InstanceId": rid, "DisableApiTermination": {"Value": False}},
    ),
}  # fmt: skip


@pytest.mark.parametrize(
    ("op", "tagged", "state", "refusal"),
    [
        (op, tagged, "available", refusal)
        for op in ENDING
        for tagged, refusal in [
            ({**SCOPE, "zab_admin": "e2e"}, "admin server"),
            ({"workshop": "zab-test", "zab_role": "test"}, "workshop=zab-test, not a test one"),
            ({"zab_role": "test"}, "no workshop, not a test one"),
        ]
    ]
    + [("delete_volume", SCOPE, state, f"is {state}") for state in ("in-use", "creating")],
)
def test_the_gate_ends_only_what_a_test_workshop_owns(tmp_path, op, tagged, state, refusal):
    """Review of #368: an address released or a volume deleted, as an instance terminated or
    unprotected, is of a test workshop (not #263's `zab-test`, not of none) and not the
    server's; a volume deleted is available."""
    aws, ec2, _, _ = make(tmp_path)
    key, params = ENDING[op]
    resource = {key: "r-1", "State": state, "Tags": tags(**tagged)}
    with pytest.raises(NotTestInfrastructure, match=refusal):
        aws.aws.ec2(op, guarded=resource, **params("r-1"))
    assert ec2.writes() == []


def test_the_gate_lets_the_teardown_end_a_test_workshops_address_and_volume(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    for op in ("release_address", "delete_volume"):
        key, params = ENDING[op]
        resource = {key: "r-1", "State": "available", "Tags": tags(**SCOPE)}
        aws.aws.ec2(op, guarded=resource, **params("r-1"))
    assert ec2.writes() == ["release_address", "delete_volume"]


@pytest.mark.parametrize(
    "op", ["attach_role_policy", "update_assume_role_policy", "put_role_permissions_boundary",
           "delete_role", "tag_role"],
)  # fmt: skip
def test_only_the_rigs_own_iam_writes_are_sent(tmp_path, op):
    """Review of #363: the five IAM operations of `ensure_role`, and no other write."""
    aws, _, iam, _ = make(tmp_path)
    with pytest.raises(NotTestInfrastructure, match=op):
        aws.aws.iam(op, RoleName="zab-test-admin", PolicyArn="arn:aws:iam::aws:policy/Admin")
    assert iam.writes() == []


def test_the_scenarios_view_of_aws_is_read_only():
    ec2 = FakeEc2()
    aws = Aws(ReadOnly(), ec2, FakeIam())
    assert aws.ec2("describe_vpcs")["Vpcs"]
    mine = {"InstanceId": "i-mine", "Tags": [{"Key": "zab-role", "Value": "test"}]}
    with pytest.raises(NotTestInfrastructure, match="only read"):
        aws.ec2("stop_instances", guarded=mine, InstanceIds=["i-mine"])
    assert ec2.writes() == []


# ------------------------------------- EC2's eventual consistency (the first live --setup-only)


class Unsettled(FakeEc2):
    """EC2 as it answered the first live setup: a new instance's (or group's) ID unknown for a
    few calls; a `pending` instance that can't be stopped yet."""

    def __init__(self, misses=2):
        super().__init__()
        self.misses = {"describe_instances": misses, "authorize_security_group_ingress": misses}
        self.pending_reads = 0

    def describe_instances(self, **p):
        if "InstanceIds" in p and self.misses["describe_instances"] > 0:
            self.misses["describe_instances"] -= 1
            raise FakeError("InvalidInstanceID.NotFound")
        found = super().describe_instances(**p)
        for inst in self.instances:  # pending for two reads, then running
            if inst["State"]["Name"] == "pending":
                self.pending_reads += 1
                if self.pending_reads > 2:
                    inst["State"] = {"Name": "running"}
        return found

    def authorize_security_group_ingress(self, **p):
        if self.misses["authorize_security_group_ingress"] > 0:
            self.misses["authorize_security_group_ingress"] -= 1
            raise FakeError("InvalidGroup.NotFound")
        return super().authorize_security_group_ingress(**p)

    def stop_instances(self, **p):
        state = next(i for i in self.instances if i["InstanceId"] in p["InstanceIds"])["State"]
        if state["Name"] == "pending":
            raise FakeError("IncorrectInstanceState")
        return super().stop_instances(**p)


def test_a_new_server_unknown_for_a_moment_is_waited_for(tmp_path):
    aws, ec2, _, _ = make(tmp_path, ec2=Unsettled())
    made = build(aws)  # the groups' rules wait out InvalidGroup.NotFound too
    assert ec2.misses == {"describe_instances": 2, "authorize_security_group_ingress": 0}
    assert aws.start(made["server"]) == "198.51.100.10"  # describe-by-ID: NotFound twice
    assert ec2.misses["describe_instances"] == 0


def test_eventual_consistency_has_a_bound(tmp_path):
    aws, _, _, _ = make(tmp_path, ec2=Unsettled(misses=10**6))
    with pytest.raises(Exception, match="NotFound"):
        aws.describe("i-new")


@pytest.mark.parametrize(
    "code", ["UnauthorizedOperation", "InvalidVolume.NotFound", "IncorrectInstanceState", ""]
)
def test_settling_retries_only_what_is_not_visible_yet(tmp_path, code):
    """Review of #368: any other error, a refusal above all, is raised at once, not retried."""
    aws, _, _, _ = make(tmp_path)
    calls = []

    def call():
        calls.append(code)
        raise FakeError(code) if code else RuntimeError("not EC2's")

    with pytest.raises(Exception, match=code or "not EC2's"):
        aws.settled(call)
    assert calls == [code]


def test_a_pending_server_is_stopped_once_it_runs(tmp_path):
    ec2 = Unsettled(misses=0)
    aws, _, _, _ = make(tmp_path, ec2=ec2)
    ec2.add_instance("i-s", {"zab-role": "test", "zab-admin": "e2e"}, state="pending")
    aws.stop({"InstanceId": "i-s", "State": {"Name": "pending"},
              "Tags": [{"Key": "zab-role", "Value": "test"}]})  # fmt: skip
    assert ec2.instances[0]["State"]["Name"] == "stopped"
    assert [op for op in ec2.writes()] == ["stop_instances"]


OLD, NEW = "198.51.100.20", "198.51.100.21"


class Lagging(FakeEc2):
    """The rig's server as EC2 shows it, one scripted (state, address) per read, the last for
    good. A server read `stopped` stays so until it is started; a start of a server not read
    `stopped` fails the test (EC2 refuses one `stopping`)."""

    def __init__(self, *reads: tuple[str, str | None]) -> None:
        super().__init__()
        self.reads, self.started = list(reads), False
        self.add_instance("i-s", rigs.SERVER_TAGS, state=reads[0][0])

    def describe_instances(self, **p):
        held = self.instances[0]["State"]["Name"] == "stopped" and not self.started
        if not held:
            state, ip = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            self.instances[0].update(State={"Name": state}, PublicIpAddress=ip)
        return super().describe_instances(**p)

    def start_instances(self, **p):
        assert self.instances[0]["State"]["Name"] == "stopped", self.instances[0]["State"]
        self._log("start_instances", p)
        self.started = True


@pytest.mark.parametrize(
    ("reads", "address", "started"),
    [
        # Oct 22: a stop sent seconds before this run; EC2 still showed the server running,
        # at the address it was losing, and the deploy timed out on SSH. Confirmed 30 s
        # later, it reads stopping: waited for, started, and the new address taken
        (
            [("running", OLD), ("stopping", OLD), ("stopping", OLD), ("stopped", None),
             ("pending", None), ("running", NEW)],
            NEW,
            True,
        ),
        ([("stopping", OLD), ("stopped", None), ("pending", NEW), ("running", NEW)], NEW, True),
        ([("stopped", None), ("pending", None), ("running", NEW)], NEW, True),
        ([("pending", None), ("running", NEW)], NEW, False),  # a start under way: no other
        ([("running", OLD)], OLD, False),  # running, and still at the same address
    ],
)  # fmt: skip
def test_the_server_is_started_from_stopped_and_its_address_read_running(
    tmp_path, reads, address, started
):
    """A server `stopping` is started only once it is `stopped`, and its address read once it
    is `running`; one read `running` without a start here is read again before it is used."""
    ec2 = Lagging(*reads)
    aws, _, _, _ = make(tmp_path, ec2=ec2)
    assert aws.start(aws.server()) == address
    assert ec2.writes() == (["start_instances"] if started else [])


def test_a_server_whose_state_keeps_changing_is_not_used(tmp_path):
    flapping = [("running", OLD), ("running", NEW)] * 3 + [("running", OLD)]
    aws, ec2, _, _ = make(tmp_path, ec2=Lagging(*flapping))
    with pytest.raises(RuntimeError, match="still changing state"):
        aws.start(aws.server())
    assert ec2.writes() == []


def test_a_server_whose_first_boot_console_is_gone_is_refused(tmp_path):
    import datetime as dt

    aws, ec2, _, _ = make(tmp_path, scan=lambda ip: pytest.fail("never scanned: no fingerprints"))
    ec2.console = "Ubuntu 24.04 LTS ip-172-31-0-1 ttyS0\\nlogin:"  # a reboot's console
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    inst = {"InstanceId": "i-s", "Tags": [{"Key": "zab-role", "Value": "test"}],
            "LaunchTime": old}  # fmt: skip
    with pytest.raises(RuntimeError, match="no longer on its console"):
        aws.pin_host_key(inst, "198.51.100.5")
    assert not aws.rig.host_key.exists()
    young = {**inst, "LaunchTime": dt.datetime.now(dt.UTC)}  # its first boot still to come
    # sshd not up yet; or up (review of #368), presenting a key the console has not vouched for
    for scanned in ("", f"198.51.100.5 {KEY}\n"):
        aws.scan = lambda ip, scanned=scanned: scanned
        with pytest.raises(TimeoutError):  # waited for (on the fake clock), never trusted
            aws.pin_host_key(young, "198.51.100.5")
        assert not aws.rig.host_key.exists() and not aws.rig.known_hosts.exists()


# ------------------------------------------------- A13's probe interface (#356's V4 check)

PROBE_TAGS = tags(zab_role="test", Name="zab-admin-e2e-eni-probe")


def test_the_probe_interface_is_made_where_the_fleet_launches_and_tagged_as_a_test(tmp_path):
    """A default subnet of the default VPC, in the first zone that offers the fleet's type
    (us-east-1e offers no t3), with the fleet's group; tagged `zab-role=test`, never
    `workshop=`; a leftover is deleted first."""
    aws, ec2, _, _ = make(tmp_path)
    ec2.interfaces.append({"NetworkInterfaceId": "eni-old", "Status": "available",
                           "TagSet": PROBE_TAGS})  # fmt: skip
    subnet, eni = aws.probe_interface("sg-fleet", rigs.PARTICIPANT_TYPE)
    assert subnet == "subnet-a" and eni.startswith("eni-") and eni != "eni-old"
    assert [i["NetworkInterfaceId"] for i in ec2.interfaces] == [eni]
    made = next(p for op, p in ec2.calls if op == "create_network_interface")
    assert made["SubnetId"] == "subnet-a" and made["Groups"] == ["sg-fleet"]
    spec = made["TagSpecifications"][0]
    assert spec["ResourceType"] == "network-interface"
    assert {t["Key"]: t["Value"] for t in spec["Tags"]} == rigs.PROBE_ENI_TAGS
    assert "workshop" not in rigs.PROBE_ENI_TAGS
    assert ec2.writes() == ["delete_network_interface", "create_network_interface"]
    aws.drop_probe_interfaces()
    assert ec2.interfaces == []


def _descriptions(value) -> list[str]:
    """Every `Description` string anywhere in an EC2 call's parameters."""
    if isinstance(value, dict):
        own = [value["Description"]] if isinstance(value.get("Description"), str) else []
        return own + [d for v in value.values() for d in _descriptions(v)]
    if isinstance(value, list):
        return [d for v in value for d in _descriptions(v)]
    return []


def test_every_ec2_description_the_rig_sends_is_one_ec2_accepts(tmp_path):
    """Live round 1 on #414: `CreateNetworkInterface` refused "A13's probe, another service's
    interface" (`InvalidParameterValue: Invalid interface description. Valid descriptions are
    strings less than 256 characters from the following set: a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*`).
    The probe interface's, the groups' and their SSH rules' descriptions, as sent."""
    valid = re.compile(r"^[A-Za-z0-9. _\-:/()#,@\[\]+=&;{}!$*]{0,255}$")
    assert valid.fullmatch(PROBE_ENI_DESCRIPTION) and not valid.fullmatch("A13's probe")
    aws, ec2, _, _ = make(tmp_path)
    build(aws)
    aws.allow_server(aws.vpc(), "198.51.100.1")
    aws.probe_interface("sg-fleet", rigs.PARTICIPANT_TYPE)
    sent = [d for op, p in ec2.calls if op in ec2.writes() for d in _descriptions(p)]
    ops = {op for op, p in ec2.calls if _descriptions(p)}
    assert ops == {"create_security_group", "authorize_security_group_ingress",
                   "create_network_interface"}  # fmt: skip
    assert PROBE_ENI_DESCRIPTION in sent
    assert all(valid.fullmatch(d) for d in sent), [d for d in sent if not valid.fullmatch(d)]


def test_no_default_subnet_offering_the_type_is_refused_before_any_change(tmp_path):
    aws, ec2, _, _ = make(tmp_path)
    ec2.offered = ["us-east-1f"]
    with pytest.raises(LookupError, match=r"no default subnet offers t3\.medium"):
        aws.probe_interface("sg-fleet", rigs.PARTICIPANT_TYPE)
    assert ec2.writes() == []


@pytest.mark.parametrize(
    ("eni", "refusal"),
    [
        ({"Status": "available", "TagSet": tags(zab_role="test")}, "not A13's probe"),
        ({"Status": "available", "TagSet": tags(Name="zab-admin-e2e-eni-probe")},
         "eni-1 is not tagged zab-role=test"),  # named, not `?`
        ({"Status": "available", "TagSet": [*PROBE_TAGS, *tags(workshop="zab-admin-e2e")]},
         "not A13's probe"),
        ({"Status": "in-use", "TagSet": PROBE_TAGS}, "is in-use"),
    ],
)  # fmt: skip
def test_the_gate_deletes_only_a_detached_probe_interface(tmp_path, eni, refusal):
    aws, ec2, _, _ = make(tmp_path)
    resource = {"NetworkInterfaceId": "eni-1", **eni}
    with pytest.raises(NotTestInfrastructure, match=refusal):
        aws.aws.ec2("delete_network_interface", guarded=resource, NetworkInterfaceId="eni-1")
    probe = {**resource, "TagSet": PROBE_TAGS, "Status": "available"}
    with pytest.raises(NotTestInfrastructure, match="eni-1 was checked"):
        aws.aws.ec2("delete_network_interface", guarded=probe, NetworkInterfaceId="eni-2")
    assert ec2.writes() == []


def test_the_teardown_deletes_a_probe_interface_a_run_left_and_checks(tmp_path):
    from e2e.admin.run import teardown

    aws, ec2, _, _ = make(tmp_path)
    ec2.interfaces = [
        {"NetworkInterfaceId": "eni-left", "Status": "available", "TagSet": PROBE_TAGS},
        {"NetworkInterfaceId": "eni-other", "Status": "available", "TagSet": tags(zab_role="test")},
    ]
    assert teardown(aws, keep_server=True) == []
    assert [i["NetworkInterfaceId"] for i in ec2.interfaces] == ["eni-other"]  # never touched


def test_the_teardown_reports_a_probe_interface_it_could_not_delete(tmp_path):
    from e2e.admin.run import teardown

    aws, ec2, _, _ = make(tmp_path)
    ec2.interfaces = [{"NetworkInterfaceId": "eni-busy", "Status": "in-use", "TagSet": PROBE_TAGS}]
    problems = teardown(aws, keep_server=True)
    assert problems[0].startswith("teardown: A13's probe interface: NotTestInfrastructure")
    assert problems[1:] == ["teardown check: A13's probe interface not deleted: eni-busy"]
    assert ec2.writes() == []
