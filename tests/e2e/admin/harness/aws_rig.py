"""The rig's AWS side (docs/ADMIN.md §7, §5; issue #356), in Drew's account, test only.

Every resource is found again by its name and tags on each run, so a second run reuses what the
first made (idempotent), and a run that died half-way is finished by the next (resumable):

- **Groups.** The server's, `zab-test-admin-server` (`zab-admin=e2e`), admits SSH from the
  sources of the rig's `zab-test-ssh` (the development machine). Each workshop's participants'
  group, tagged with its scope (`workshop=…`, `zab-scope=admin-e2e`, `zab-role=test`) for the role's
  `RunInstances` condition (§5), admits SSH from the server's current address, set at each
  start: only from it for `zab-admin-e2e`; for `zab-admin-e2e2`, whose VM A18 launches from
  the development machine as on Oct 9, from the development machine too (the sources of
  `zab-test-ssh`), whose break-glass key scans and joins it (§8.4's `join_server.py`). The
  region's fleet group, `zab-fleet`, in the scope but in no workshop, as BOOTSTRAP §6 sets a
  region up, is the one `!new-workshop` finds (A16): SSH from the development machine only. A
  scope tag a group lacks (one made before #422) is added; one with another value is refused.
- **The test AMI** carries the rig's scope, `zab-scope=admin-e2e`, too: the role launches only
  from an image with every scope tag but `workshop` (§5, "LaunchFrom"; #269 built it with
  `zab-role=test` alone). So do the server's key pairs `zab-admin-<ws>`: a bootstrap imports
  them with the full scope, and the rig tags those an earlier one imported without it, as the
  account owner did in production (the v0.2.5 policy change, ADMIN §8.4 at v0.2.15).
- **IAM.** Role and instance profile `zab-test-admin`, with the policy that
  `workshop/adminserver/render_policy.py` renders from the test admin.toml, as inline policies
  `zab-test-admin-N` (test infrastructure: `zab-test-*` only).
- **The server:** a `t4g.small` on stock Ubuntu 24.04, tagged `zab-role=test`, `zab-admin=e2e`,
  never `workshop=`, with termination protection, IMDSv2 (hop limit 1) and a 30 GiB encrypted
  gp3 volume (§1.1). Its ed25519 host key is pinned from the fingerprints cloud-init prints to
  the console at its first boot, never trusted on first use.
- **Teardown:** every instance of the test workshops (`workshop=zab-admin-e2e*` and
  `zab-role=test`, with the scope or without) terminated (its termination protection
  lifted first: A4 protects one), their Elastic IPs released and their volumes deleted; the
  server stopped; A13's probe interface deleted, if a run left it. Nothing without
  `zab-role=test` is touched, nothing of another workshop is ended, and nothing with a
  `zab-admin` tag is (`gate.Aws`). A failure never stops the rest.
- **A13's probe interface** (#356's V4 check): a detached network interface tagged
  `PROBE_ENI_TAGS` (never `workshop=`), standing for another service's, which the role must not
  attach at a launch. It is made in a default subnet of the default VPC, in a zone that offers
  the fleet's instance type, and with the fleet's security group, so that only the interface
  itself sets it apart from a launch the role may make.
- **EC2 is eventually consistent:** whatever acts on, or checks, a change just made reads it
  back first, until EC2 shows it (`until`, `settle.py`): a lifted protection before the
  terminate, a terminated instance's addresses and volumes, a stopped server, an image's new
  tag, deleted key pairs; and a call refused for a state not yet left is tried again.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import re
import subprocess
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from e2e.admin.harness.gate import (
    OWN_ID,
    Aws,
    NotTestInfrastructure,
    check_policy,
    error_code,
    require_test,
    tags_of,
)
from e2e.admin.harness.rig import (
    E2E,
    E2E2,
    FLEET_GROUP,
    FLEET_TAGS,
    PROBE_ENI_TAGS,
    SCOPE,
    SERVER,
    SERVER_TAGS,
    SERVER_TYPE,
    TEST_ROLE,
    THROWAWAY,
    UBUNTU_NAME,
    UBUNTU_OWNER,
    WORKSHOPS,
    AdminRig,
)
from e2e.admin.harness.settle import EVERY_S, PROTECTION_S, WITHIN_S, attribute, until_blocking

LIVE = ("pending", "running", "stopping", "stopped", "shutting-down")
#: EC2's "not visible yet" answers for something just made (eventual consistency).
NOT_YET = frozenset({"InvalidInstanceID.NotFound", "InvalidGroup.NotFound"})
#: How long after its launch a server's console may still be waiting for its first boot's keys.
FIRST_BOOT_S = 20 * 60
#: How long a `running` read of a server this run did not start is confirmed for (`start`): EC2
#: shows a stop sent just before seconds late.
CONFIRM_S = 30
#: How long EC2 may take with a terminated instance's volumes (`volumes_settled`).
VOLUMES_S = WITHIN_S
#: The pauses between `TerminateInstances` refused as `OperationNotPermitted` and the next try,
#: 5 min 45 s in all: EC2 enforces a lifted protection later still than it shows it (the live
#: runs of Oct 15: refused right after the lift read False).
TERMINATE_RETRIES_S = (15, 30, 60, 60, 60, 60, 60)
TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
_FINGERPRINTS = re.compile(
    r"BEGIN SSH HOST KEY FINGERPRINTS-----(.*?)-----END SSH HOST KEY FINGERPRINTS", re.S
)
Keyscan = Callable[[str], str]
#: EC2 takes an interface's description only from `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*`, fewer than
#: 256 characters: no apostrophe (the first live A13 run, `InvalidParameterValue`).
PROBE_ENI_DESCRIPTION = "zab admin e2e (#356): A13 probe, an interface outside the workshop"


class Mistagged(RuntimeError):
    """A rig resource carries one of its tags with another value: fixed by hand, never here."""


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def tag_filters(tags: dict[str, str]) -> list[dict[str, Any]]:
    return [{"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()]


def fingerprint(public_key: str) -> str:
    """`ssh-keygen -l`'s `SHA256:…` of a `TYPE BASE64` key."""
    blob = base64.b64decode(public_key.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def console_fingerprints(text: str) -> set[str]:
    """The fingerprints cloud-init printed at first boot (`aws_common.console_host_keys`)."""
    block = _FINGERPRINTS.search(text or "")
    return set(re.findall(r"SHA256:[A-Za-z0-9+/]+", block.group(1))) if block else set()


def keyscan(ip: str) -> str:
    done = subprocess.run(  # noqa: S603 - an argument list
        ["ssh-keyscan", "-T", "10", "-t", "ed25519", ip],  # noqa: S607 - from PATH, as ssh is
        capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    return done.stdout


def newest(images: list[dict[str, Any]]) -> dict[str, Any]:
    if not images:
        raise LookupError("no image found")
    return max(images, key=lambda i: i.get("CreationDate", ""))


class AwsRig:
    def __init__(
        self,
        aws: Aws,
        rig: AdminRig,
        *,
        scan: Keyscan = keyscan,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.aws, self.rig, self.scan, self.sleep, self.clock = aws, rig, scan, sleep, clock

    @property
    def dry(self) -> bool:
        return self.aws.gate.dry_run

    # ------------------------------------------------------------------- groups

    def vpc(self) -> str:
        vpcs = self.aws.ec2("describe_vpcs", Filters=[{"Name": "is-default", "Values": ["true"]}])
        if len(vpcs["Vpcs"]) != 1:
            raise LookupError(f"{self.rig.region}: no default VPC")
        return str(vpcs["Vpcs"][0]["VpcId"])

    def group(self, name: str, vpc: str) -> dict[str, Any] | None:
        filters = [{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc]}]
        found = self.aws.ec2("describe_security_groups", Filters=filters)["SecurityGroups"]
        return found[0] if found else None

    def ssh_sources(self, vpc: str) -> list[str]:
        """The CIDRs the rig's `zab-test-ssh` admits on port 22: the development machine's. They are
        copied into the server's group and the second workshop's (A18), so each must be one address:
        a wider block is refused (RuntimeError), never copied."""
        sg = self.group(self.rig.ssh_group, vpc)
        if sg is None:
            raise LookupError(f"no security group {self.rig.ssh_group} (#263's)")
        cidrs = sorted(_ssh_cidrs(sg))
        wide = [c for c in cidrs if ipaddress.ip_network(c, strict=False).num_addresses != 1]
        if wide:
            raise RuntimeError(
                f"{self.rig.ssh_group} admits SSH from {', '.join(wide)}, wider than one address: "
                "not copied into the rig's groups. Narrow it to the development machine's /32"
            )
        return cidrs

    def ensure_group(
        self, name: str, tags: dict[str, str], vpc: str, sources: list[str] | None = None
    ) -> str:
        """The group, made if missing, with every one of `tags`: one it lacks is added (the
        scope, on a group made before #422), one with another value refused. With `sources`,
        admitting SSH from exactly those IPv4 CIDRs, and from no IPv6 range (rules on other
        ports are left alone)."""
        sg = self.group(name, vpc)
        if sg is None:
            made = self.aws.ec2(
                "create_security_group",
                dry={"GroupId": f"<new group {name}>"},
                GroupName=name, Description=f"zab admin e2e (#356): {name}", VpcId=vpc,
                TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list(tags)}],
            )  # fmt: skip
            sg = {"GroupId": made["GroupId"], "Tags": tag_list(tags), "IpPermissions": []}
        require_test(sg, what=name)
        sg = self.add_tags(sg, tags, what=name)
        gid = str(sg["GroupId"])
        if sources is None:
            return gid
        have, want, stray = _ssh_cidrs(sg), set(sources), _ssh_cidrs(sg, v6=True)
        if stray:  # the rig admits no IPv6 source: a stray one is removed
            self.settled(lambda: self.aws.ec2("revoke_security_group_ingress", guarded=sg,
                         GroupId=gid, IpPermissions=[_ssh_rule(stray, v6=True)]))  # fmt: skip
        if have - want:
            self.settled(lambda: self.aws.ec2("revoke_security_group_ingress", guarded=sg,
                         GroupId=gid, IpPermissions=[_ssh_rule(have - want)]))  # fmt: skip
        if want - have:  # a group just made may not be visible yet: `settled`
            self.settled(lambda: self.aws.ec2("authorize_security_group_ingress", guarded=sg,
                         GroupId=gid, IpPermissions=[_ssh_rule(want - have)]))  # fmt: skip
        return gid

    def fleet_group(self, vpc: str) -> str:
        """The region's fleet group `zab-fleet` (`FLEET_TAGS`: the scope, no workshop), as an
        account owner sets a region up (BOOTSTRAP §6): what `!new-workshop` finds (A16). It
        admits SSH from the development machine only (the sources of `zab-test-ssh`), never the
        internet."""
        return self.ensure_group(FLEET_GROUP, FLEET_TAGS, vpc, self.ssh_sources(vpc))

    def add_tags(self, resource: dict[str, Any], tags: dict[str, str], *, what: str) -> Any:
        """`resource`, as EC2 described it (test infrastructure: the gate checks it), with
        every one of `tags`: those it lacks added, one it carries with another value refused
        (`Mistagged`), never overwritten. Returns it as it is now tagged: the very object
        when it lacked nothing."""
        have = tags_of(resource)
        wrong = {k: have[k] for k, v in tags.items() if k in have and have[k] != v}
        if wrong:
            raise Mistagged(f"{what} is tagged {wrong}, not {tags}: fix it by hand")
        missing = {k: v for k, v in tags.items() if k not in have}
        if not missing:
            return resource
        own = next(str(resource[k]) for k in OWN_ID if resource.get(k))
        self.settled(lambda: self.aws.ec2("create_tags", guarded=resource, Resources=[own],
                                          Tags=tag_list(missing)))  # fmt: skip
        return {**resource, "Tags": tag_list(have | missing)}

    def until(
        self, read: Callable[[], Any], wanted: Callable[[Any], bool], *, what: str,
        within: float = WITHIN_S, every: float = EVERY_S, strict: bool = True,
    ) -> Any:  # fmt: skip
        """`read()` until `wanted` holds, bounded (`settle.until_blocking`, on the rig's
        clock): every read of what a change just made, so EC2 has shown it."""
        return until_blocking(read, wanted, what=what, within=within, every=every,
                              strict=strict, sleep=self.sleep, clock=self.clock)  # fmt: skip

    def settled(self, call: Callable[[], Any], within: float = 150) -> Any:
        """`call()`, again while EC2 says a resource it just made doesn't exist (eventual
        consistency: a new instance's or group's ID can be unknown for seconds), with a growing
        pause, for at most `within` seconds; any other error at once."""
        start, pause = self.clock(), 2.0
        while True:
            try:
                return call()
            except Exception as exc:
                if error_code(exc) not in NOT_YET or self.clock() - start >= within:
                    raise
            self.sleep(pause)
            pause = min(pause * 2, 20.0)

    # ------------------------------------------------------------------- images

    def test_image(self) -> dict[str, Any]:
        """The fake participants' AMI, tagged with the rig's scope (§5's LaunchFrom), as the
        account owner tags a region's image in production: `zab-scope`, beside `zab-role=test`.
        Its `workshop` tag, if any, is left as it is: LaunchFrom doesn't read it (#422)."""
        if self.rig.ami:
            image = newest(self.aws.ec2("describe_images", ImageIds=[self.rig.ami])["Images"])
        else:
            filters = [*tag_filters(dict([TEST_ROLE])), {"Name": "name", "Values": ["zab-test-*"]}]
            filters.append({"Name": "state", "Values": ["available"]})
            image = newest(
                self.aws.ec2("describe_images", Owners=["self"], Filters=filters)["Images"]
            )
        require_test(image, what=image["ImageId"])
        iid, scope = image["ImageId"], dict([SCOPE])
        try:
            tagged = self.add_tags(image, scope, what=iid)
        except Mistagged:  # only that: the gate's refusal and AWS's errors go through as they are
            raise Mistagged(
                f"{iid} is in another scope: the role launches only from an image of its own. "
                f"Copy it, tagged {scope} and zab-role=test, and set ZAB_ADMIN_E2E_AMI"
            ) from None
        if tagged is not image and not self.dry:  # A4's launches condition on it (LaunchFrom)
            image = self.until(
                lambda: newest(self.aws.ec2("describe_images", ImageIds=[iid])["Images"]),
                lambda img: tags_of(img).get(SCOPE[0]) == SCOPE[1],
                what=f"{iid} tagged {SCOPE[0]}={SCOPE[1]}",
            )
        return image

    def ubuntu_image(self) -> dict[str, Any]:
        filters = [{"Name": "name", "Values": [UBUNTU_NAME]}]
        filters += [{"Name": "state", "Values": ["available"]}]
        filters += [{"Name": "architecture", "Values": ["arm64"]}]
        return newest(
            self.aws.ec2("describe_images", Owners=[UBUNTU_OWNER], Filters=filters)["Images"]
        )

    # ---------------------------------------------------------------------- IAM

    def ensure_role(self, policies: list[dict[str, Any]]) -> None:
        """Role and instance profile `zab-test-admin`, holding exactly `policies`, each checked
        (`check_policy`) before anything is written, and no managed policy at all."""
        if not policies:
            raise NotTestInfrastructure("no policy document for the role")
        for doc in policies:  # whatever renderer the commit under test brought
            check_policy(doc)
        tags = tag_list(dict([TEST_ROLE]))
        role = self._get("get_role", "Role", RoleName=SERVER)
        if role is None:
            self.aws.iam("create_role", RoleName=SERVER, Tags=tags,
                         AssumeRolePolicyDocument=json.dumps(TRUST),
                         Description="zab admin e2e test server (#356)")  # fmt: skip
        else:
            require_test(role, what=f"role {SERVER}")
            attached = self.aws.iam("list_attached_role_policies", RoleName=SERVER)
            names = [p.get("PolicyName") for p in attached.get("AttachedPolicies", [])]
            if names:
                raise NotTestInfrastructure(
                    f"managed policies on {SERVER}: {names}: detach them; the rig puts its own"
                )
        wanted = {f"{SERVER}-{i}": doc for i, doc in enumerate(policies, 1)}
        have: set[str] = set()
        if role is not None:
            have = set(self.aws.iam("list_role_policies", RoleName=SERVER)["PolicyNames"])
        for name in sorted(have - set(wanted)):
            self.aws.iam("delete_role_policy", RoleName=SERVER, PolicyName=name)
        for name, doc in wanted.items():
            now = self._get("get_role_policy", "PolicyDocument", RoleName=SERVER, PolicyName=name)
            if now != doc:  # boto3 decodes the document
                self.aws.iam("put_role_policy", RoleName=SERVER, PolicyName=name,
                             PolicyDocument=json.dumps(doc))  # fmt: skip
        profile = self._get("get_instance_profile", "InstanceProfile", InstanceProfileName=SERVER)
        if profile is None:
            self.aws.iam("create_instance_profile", InstanceProfileName=SERVER, Tags=tags)
        elif "Tags" in profile:
            require_test(profile, what=f"instance profile {SERVER}")
        if profile is None or SERVER not in {r["RoleName"] for r in profile.get("Roles", [])}:
            self.aws.iam(
                "add_role_to_instance_profile", InstanceProfileName=SERVER, RoleName=SERVER
            )

    def _get(self, op: str, key: str, **params: Any) -> Any:
        try:
            return self.aws.iam(op, **params)[key]
        except Exception as exc:
            if error_code(exc) == "NoSuchEntity":
                return None
            raise

    # ------------------------------------------------------------------- the server

    def server(self) -> dict[str, Any] | None:
        filters = [*tag_filters(SERVER_TAGS), {"Name": "instance-state-name", "Values": list(LIVE)}]
        found = _instances(self.aws.ec2("describe_instances", Filters=filters))
        if len(found) > 1:
            ids = ", ".join(i["InstanceId"] for i in found)
            raise LookupError(f"more than one test admin server: {ids}")
        return found[0] if found else None

    def launch_server(self, group: str) -> dict[str, Any]:
        """The server, launched (once: `server()` finds it afterwards). IAM may take a few
        seconds to let a new instance profile be used: that refusal is retried."""
        image = self.ubuntu_image()
        tags = tag_list(SERVER_TAGS)
        params = dict(
            ImageId=image["ImageId"], InstanceType=SERVER_TYPE, MinCount=1, MaxCount=1,
            KeyName=self.rig.key_pair, SecurityGroupIds=[group],
            IamInstanceProfile={"Name": SERVER}, DisableApiTermination=True,
            MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 1,
                             "HttpEndpoint": "enabled"},
            BlockDeviceMappings=[{"DeviceName": image.get("RootDeviceName", "/dev/sda1"),
                                  "Ebs": {"VolumeSize": 30, "VolumeType": "gp3",
                                          "Encrypted": True, "DeleteOnTermination": True}}],
            TagSpecifications=[{"ResourceType": "instance", "Tags": tags},
                               {"ResourceType": "volume", "Tags": tags}],
        )  # fmt: skip
        dry = {"Instances": [{"InstanceId": "<new server>", "Tags": tags,
                              "State": {"Name": "running"}}]}  # fmt: skip
        for attempt in range(12):
            try:
                return dict(self.aws.ec2("run_instances", dry, **params)["Instances"][0])
            except Exception as exc:
                if error_code(exc) != "InvalidParameterValue" or attempt == 11:
                    raise
                self.sleep(10)
        raise AssertionError("unreachable")

    def describe(self, instance_id: str) -> dict[str, Any]:
        found = _instances(
            self.settled(lambda: self.aws.ec2("describe_instances", InstanceIds=[instance_id]))
        )
        if len(found) != 1 or found[0].get("InstanceId") != instance_id:
            raise LookupError(f"{instance_id}: not found")
        require_test(found[0])
        return found[0]

    def wait(self, instance_id: str, target: str, timeout: float = 900) -> dict[str, Any]:
        """The instance once EC2 shows it in `target` (a transition, and a read behind it)."""
        inst = self.until(lambda: self.describe(instance_id),
                          lambda found: found["State"]["Name"] == target,
                          what=f"{instance_id} {target}", within=timeout, every=10,
                          strict=False)  # fmt: skip
        if inst["State"]["Name"] != target:
            raise TimeoutError(f"{instance_id} still {inst['State']['Name']} after {timeout:.0f} s")
        return inst

    def start(self, inst: dict[str, Any]) -> str:
        """The server running; its public address, as read once EC2 shows it `running`.

        A server `stopping` is waited for until it is `stopped`, then started: EC2 refuses a
        start before, and the address it has then is one it is losing. A server read `running`
        that this run did not start is read again `CONFIRM_S` later, and its address is taken
        only if it still runs at the same one. On Oct 22 a run read the server `running`
        seconds after the run before had sent its stop (EC2's reads lag a change), wrote the
        address it was losing into `ssh_config`, and its deploy timed out on SSH."""
        iid = inst["InstanceId"]
        if self.dry:
            if inst["State"]["Name"] != "running":
                self.aws.ec2("start_instances", guarded=inst, InstanceIds=[iid])
            return inst.get("PublicIpAddress") or "<its address>"
        for _ in range(3):
            state = inst["State"]["Name"]
            if state == "stopping":
                inst, state = self.wait(iid, "stopped"), "stopped"
            if state == "stopped":  # as read: a stop just before may still run
                self._retried("start_instances", "IncorrectInstanceState", guarded=inst,
                              InstanceIds=[iid])  # fmt: skip
            if state != "running":  # started here, or a start under way (pending)
                return str(self.wait(iid, "running").get("PublicIpAddress") or "")
            self.sleep(CONFIRM_S)
            again = self.describe(iid)
            same = again.get("PublicIpAddress") == inst.get("PublicIpAddress")
            if again["State"]["Name"] == "running" and same:
                return str(again.get("PublicIpAddress") or "")
            inst = again
        raise RuntimeError(f"{iid}: still changing state after three reads; not started")

    def stop(self, inst: dict[str, Any], *, wait: bool = True) -> None:
        """The server stopped: one still `pending` (a run that died right after its launch) is
        first waited for until it runs, since EC2 refuses to stop it before; a stop refused
        mid-transition (`IncorrectInstanceState`) is tried again, within bounds."""
        iid, state = inst["InstanceId"], inst["State"]["Name"]
        if state == "pending" and not self.dry:
            inst = self.wait(iid, "running", timeout=600)
            state = "running"
        if state in ("running", "pending"):
            self._retried("stop_instances", "IncorrectInstanceState", guarded=inst,
                          InstanceIds=[iid])  # fmt: skip
        if wait and not self.dry and state != "stopped":
            self.wait(iid, "stopped")

    def _retried(
        self, op: str, code: str, pauses: tuple[float, ...] = (10,) * 11, **params: Any
    ) -> Any:
        """The change, tried again after each pause while EC2 refuses it as `code`: a refusal
        of a state a change just before has not yet left; any other error at once."""
        for pause in (*pauses, None):
            try:
                return self.aws.ec2(op, **params)
            except Exception as exc:
                if error_code(exc) != code or pause is None:
                    raise
            self.sleep(pause)
        raise AssertionError("unreachable")

    def pin_host_key(self, inst: dict[str, Any], ip: str, *, timeout: float = 900) -> str:
        """The server's ed25519 host key: read once from its first boot's console and kept in
        the rig's directory; every run then writes `known_hosts` for its current address.
        cloud-init prints the fingerprints at the first boot only: a server launched long ago
        whose console no longer shows them (it was rebooted) is refused at once, never trusted
        on a key it presents now."""
        path = self.rig.host_key
        if not path.exists():
            if self.dry:
                self.aws.gate.note("would pin the server's host key from its console")
                return ""
            start, key = self.clock(), ""
            young = _launched_within(inst, FIRST_BOOT_S)
            while not key:
                out = self.settled(lambda: self.aws.ec2(
                    "get_console_output", InstanceId=inst["InstanceId"], Latest=True))  # fmt: skip
                prints = console_fingerprints(out.get("Output", ""))
                if not prints and not young:
                    raise RuntimeError(
                        f"{inst['InstanceId']}: its first boot's host key fingerprints are no "
                        "longer on its console (rebooted?), and a key it shows now is not "
                        "trusted. Lift its termination protection, terminate it, and set up again"
                    )
                scanned = [
                    line.split(None, 1)[1]
                    for line in self.scan(ip).splitlines()
                    if len(line.split()) == 3 and not line.startswith("#")
                ]
                if prints:
                    match = [k for k in scanned if fingerprint(k) in prints]
                    if scanned and not match:
                        raise RuntimeError(f"{ip}'s host key is not the one its console printed")
                    key = match[0] if match else ""
                if not key and self.clock() - start >= timeout:
                    raise TimeoutError(f"no host key fingerprints on the server's console ({ip})")
                if not key:
                    self.sleep(15)
            path.write_text(key.strip() + "\n", encoding="utf-8")
            path.chmod(0o600)
        key = path.read_text(encoding="utf-8").strip()
        if self.dry:  # nothing written: the file must stay valid for the next run (Codex)
            self.aws.gate.note(f"would write {self.rig.known_hosts} for the server's address")
            return key
        self.rig.known_hosts.write_text(f"{ip} {key}\n", encoding="utf-8")
        return key

    # ------------------------------------------------------------- participants

    def fleet(self) -> list[dict[str, Any]]:
        """Every instance of the test workshops not yet terminated: tagged `workshop=zab-admin-
        e2e*` and `zab-role=test` (`ends`: with the scope or without), with a `participant` tag
        or without (a launch the code under test got wrong), never one tagged `zab-admin`."""
        found = []
        for ws in WORKSHOPS:
            filters = [*tag_filters(ws.ends)]
            filters.append({"Name": "instance-state-name", "Values": list(LIVE)})
            found += _instances(self.aws.ec2("describe_instances", Filters=filters))
        return [i for i in found if "zab-admin" not in tags_of(i)]

    def participants(self) -> list[dict[str, Any]]:
        """The fleet's fake participants (and spares): its instances with a `participant` tag."""
        return [i for i in self.fleet() if "participant" in tags_of(i)]

    def addresses(self) -> list[dict[str, Any]]:
        """Every Elastic IP of a test workshop (`ends`), never one tagged `zab-admin`."""
        found = []
        for ws in WORKSHOPS:
            got = self.aws.ec2("describe_addresses", Filters=tag_filters(ws.ends))["Addresses"]
            found += [a for a in got if "zab-admin" not in tags_of(a)]
        return found

    def volumes(self) -> list[dict[str, Any]]:
        """Every volume of a test workshop (`ends`) not being deleted, never one tagged
        `zab-admin`."""
        found = []
        states = {"Name": "status", "Values": ["creating", "available", "in-use"]}
        for ws in WORKSHOPS:
            filters = [*tag_filters(ws.ends), states]
            found += self.aws.ec2("describe_volumes", Filters=filters)["Volumes"]
        return [v for v in found if "zab-admin" not in tags_of(v)]

    def volumes_settled(self, *, gone: bool = False, within: float = VOLUMES_S) -> list[Any]:
        """The test workshops' volumes (`volumes`) once EC2 is done with them, waited for at
        most `within` seconds, then as they are. Right after its instance is terminated, EC2 may
        still show a root volume `in-use` before it deletes it (or, kept on termination, makes
        it `available`): the teardown waits for each to be `available` or gone; its check
        (`gone`), for each to be gone, since one just deleted may still show for a moment."""

        def done(found: list[Any]) -> bool:
            return not (found if gone else [v for v in found if v.get("State") != "available"])

        return self.until(self.volumes, done, what="the test workshops' volumes",
                          within=within, every=10, strict=False)  # fmt: skip

    def end(self, inst: dict[str, Any]) -> str:
        """One instance of the test workshops terminated; its ID. A protected one has its
        protection lifted first, and EC2 read until it shows the lift (`PROTECTION_S`): a
        terminate right after it was refused (the second live run). One refused still
        (`OperationNotPermitted`: EC2 enforces the lift later still) is tried again after each
        of `TERMINATE_RETRIES_S`, the protection read again first and lifted again if it reads
        on: the first read may have been a stale False, of a protection set just before
        (A4's, in a short run; review of #377)."""
        require_test(inst)
        iid = inst["InstanceId"]

        def protected() -> bool:
            return bool(attribute(self, iid, "disableApiTermination"))

        def lift() -> None:
            if not protected():
                return
            self.aws.ec2("modify_instance_attribute", guarded=inst, InstanceId=iid,
                         DisableApiTermination={"Value": False})  # fmt: skip
            if not self.dry:
                self.until(protected, lambda on: on is False, within=PROTECTION_S,
                           what=f"{iid}'s termination protection lifted")  # fmt: skip

        lift()
        for pause in (*TERMINATE_RETRIES_S, None):
            try:
                self.aws.ec2("terminate_instances", guarded=inst, InstanceIds=[iid])
                return str(iid)
            except Exception as exc:
                if error_code(exc) != "OperationNotPermitted" or pause is None:
                    raise
            self.sleep(pause)
            lift()
        raise AssertionError("unreachable")

    def terminate_participants(self) -> None:
        """Every instance of the test workshops terminated (`fleet`: a participant or not),
        protected ones included; then their Elastic IPs released, once EC2 shows them free of
        the instances terminated, and any volume left behind (one not deleted with its
        instance) deleted once EC2 has detached it; one still in use is left alone. A failure
        never stops the rest: all that can be is torn down, then a `RuntimeError` names what
        failed (the second live run: one refused terminate left two VMs, their addresses and
        their volumes)."""
        failed: list[str] = []

        def attempt(what: str, step: Callable[[], Any], default: Any = None) -> Any:
            try:
                return step()
            except Exception as exc:
                failed.append(f"{what}: {type(exc).__name__}: {exc}")
                return default

        ended = [attempt(i["InstanceId"], partial(self.end, i)) for i in self.fleet()]
        ended = [iid for iid in ended if iid]
        if not self.dry:
            ended = [iid for iid in ended if attempt(iid, partial(self.wait, iid, "terminated"))]

        def free(found: list[Any]) -> bool:  # a terminated instance's, shown associated still
            return not {a.get("InstanceId") for a in found} & set(ended)

        def addresses() -> list[Any]:
            if self.dry or not ended:
                return self.addresses()
            what = "the terminated instances' Elastic IPs free"
            return list(self.until(self.addresses, free, what=what, strict=False))

        for addr in attempt("the Elastic IPs", addresses, []):
            if addr.get("InstanceId") and not self.dry:
                continue  # still associated with something alive: not ours to release
            attempt(addr["AllocationId"], partial(self.aws.ec2, "release_address", guarded=addr,
                                                  AllocationId=addr["AllocationId"]))  # fmt: skip
        volumes = self.volumes if self.dry else self.volumes_settled
        for vol in attempt("the volumes", volumes, []):
            if vol.get("State") == "available":  # an attached one goes with its instance
                attempt(vol["VolumeId"], partial(self.aws.ec2, "delete_volume", guarded=vol,
                                                 VolumeId=vol["VolumeId"]))  # fmt: skip
        if failed:
            raise RuntimeError("not all torn down: " + "; ".join(failed))

    def allow_server(self, vpc: str, ip: str) -> None:
        """SSH into the fake participants from the server's current address; into the second
        workshop's (A18) from the development machine's addresses too."""
        self.ensure_group(E2E.group, E2E.tags, vpc, [f"{ip}/32"])
        self.ensure_group(E2E2.group, E2E2.tags, vpc, [f"{ip}/32", *self.ssh_sources(vpc)])

    def scope_key_pairs(self) -> None:
        """Before an upgrade: the server's key pairs `zab-admin-<ws>`, which only a bootstrap
        imports, tagged with their workshop's full scope. One imported before #422 lacks
        `zab-scope`, and the role launches only with a key pair in the scope (LaunchFrom); the
        role may not add it (DenyScopeRetag), so the rig does, as the account owner does in
        production (the v0.2.5 policy change, ADMIN §8.4 at v0.2.15)."""
        for ws in WORKSHOPS:
            filters = [{"Name": "key-name", "Values": [ws.key_name]}]
            for pair in self.aws.ec2("describe_key_pairs", Filters=filters)["KeyPairs"]:
                self.add_tags(pair, ws.tags, what=f"key pair {ws.key_name}")

    def drop_key_pairs(self) -> None:
        """Before a bootstrap only: the server imports its new fleet key as `zab-admin-<ws>`
        (§1.6), which an earlier server's leftover pair would refuse, as it would one deleted
        that EC2 still shows: read until it no longer does."""
        filters = [{"Name": "key-name", "Values": [ws.key_name for ws in WORKSHOPS]}]

        def pairs() -> list[Any]:
            return list(self.aws.ec2("describe_key_pairs", Filters=filters)["KeyPairs"])

        found = pairs()
        for pair in found:
            self.aws.ec2("delete_key_pair", guarded=pair, KeyPairId=pair["KeyPairId"])
        if found and not self.dry:
            self.until(pairs, lambda left: not left, what="the fleet key pairs deleted")

    def drop_throwaway_key_pairs(self) -> None:
        """A16's key pairs (#418): each run's throwaway workshop imports `zab-admin-zab-dev-e3-
        <hex>`, tagged `zab-role=test`, which the scenario's read-only view can't delete."""
        filters = [
            {"Name": "key-name", "Values": [f"zab-admin-{THROWAWAY}-*"]},
            {"Name": "tag:zab-role", "Values": ["test"]},
        ]
        for pair in self.aws.ec2("describe_key_pairs", Filters=filters)["KeyPairs"]:
            self.aws.ec2("delete_key_pair", guarded=pair, KeyPairId=pair["KeyPairId"])

    # ----------------------------------------------------- A13's probe interface

    def probe_subnet(self, instance_type: str) -> str:
        """A default subnet of the default VPC, where the fleet launches (admin.toml names no
        subnet), in the first zone that offers `instance_type`: a launch with an interface there
        is refused for the role alone, never for the type (us-east-1e offers no t3)."""
        filters = [{"Name": "vpc-id", "Values": [self.vpc()]}]
        filters.append({"Name": "default-for-az", "Values": ["true"]})
        subnets = self.aws.ec2("describe_subnets", Filters=filters)["Subnets"]
        offered = self.aws.ec2(
            "describe_instance_type_offerings", LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )["InstanceTypeOfferings"]  # fmt: skip
        zones = {o.get("Location") for o in offered}
        found = sorted((s["AvailabilityZone"], s["SubnetId"]) for s in subnets
                       if s.get("AvailabilityZone") in zones)  # fmt: skip
        if not found:
            raise LookupError(f"{self.rig.region}: no default subnet offers {instance_type}")
        return str(found[0][1])

    def probe_interfaces(self) -> list[dict[str, Any]]:
        """Every interface tagged as A13's probe (`PROBE_ENI_TAGS`): one a run left, or none."""
        found = self.aws.ec2("describe_network_interfaces", Filters=tag_filters(PROBE_ENI_TAGS))
        return list(found["NetworkInterfaces"])

    def probe_interface(self, group: str, instance_type: str) -> tuple[str, str]:
        """A13's detached interface, made after any one a run left is deleted: (its subnet, its
        ID), once EC2 shows it `available`. `group` is the fleet's security group."""
        self.drop_probe_interfaces()
        subnet = self.probe_subnet(instance_type)
        made = self.aws.ec2(
            "create_network_interface",
            dry={"NetworkInterface": {"NetworkInterfaceId": "<new interface>"}},
            SubnetId=subnet, Groups=[group],
            Description=PROBE_ENI_DESCRIPTION,
            TagSpecifications=[{"ResourceType": "network-interface",
                                "Tags": tag_list(PROBE_ENI_TAGS)}],
        )  # fmt: skip
        eni = str(made["NetworkInterface"]["NetworkInterfaceId"])
        if not self.dry:  # the server's probe must find it: read until EC2 shows it
            filters = [{"Name": "network-interface-id", "Values": [eni]}]
            self.until(
                lambda: self.aws.ec2("describe_network_interfaces", Filters=filters),
                lambda found: (
                    [i.get("Status") for i in found["NetworkInterfaces"]] == ["available"]
                ),
                what=f"{eni} available",
            )
        return subnet, eni

    def drop_probe_interfaces(self) -> None:
        """Every A13 probe interface deleted (the gate refuses any other, and one attached)."""
        for eni in self.probe_interfaces():
            self.aws.ec2("delete_network_interface", guarded=eni,
                         NetworkInterfaceId=eni["NetworkInterfaceId"])  # fmt: skip


def _ssh_cidrs(sg: dict[str, Any], *, v6: bool = False) -> set[str]:
    """The IPv4 (or, `v6`, IPv6) ranges the group admits on TCP 22."""
    ranges, key = ("Ipv6Ranges", "CidrIpv6") if v6 else ("IpRanges", "CidrIp")
    return {
        r[key]
        for p in sg.get("IpPermissions", [])
        if p.get("IpProtocol") == "tcp" and p.get("FromPort") == 22 and p.get("ToPort") == 22
        for r in p.get(ranges, [])
    }


def _ssh_rule(cidrs: set[str], *, v6: bool = False) -> dict[str, Any]:
    if v6:  # only ever revoked
        ranges: list[dict[str, str]] = [{"CidrIpv6": c} for c in sorted(cidrs)]
        return {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "Ipv6Ranges": ranges}
    ranges = [{"CidrIp": c, "Description": "zab admin e2e"} for c in sorted(cidrs)]
    return {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": ranges}


def _launched_within(inst: dict[str, Any], seconds: float) -> bool:
    """Whether `inst` was launched less than `seconds` ago (unknown: yes, it may be new)."""
    launched = inst.get("LaunchTime")
    if not isinstance(launched, dt.datetime):
        return True
    return (dt.datetime.now(dt.UTC) - launched).total_seconds() < seconds


def _instances(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for r in response.get("Reservations", []) for i in r.get("Instances", [])]
