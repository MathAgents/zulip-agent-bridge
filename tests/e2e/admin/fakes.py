"""Fake EC2, IAM and Zulip for the admin harness's offline tests (issue #356): in memory, each
answering in the shapes boto3 and Zulip use, and recording every call."""

from __future__ import annotations

import copy
import itertools
from typing import Any


def fake_ids():
    """Zulip IDs as the setup finds them, for the rendered test admin.toml."""
    from e2e.admin.harness import rig as rigs
    from e2e.admin.harness.zulip_rig import ZulipIds

    found = ZulipIds(tester=11, tester2=12, site="https://zulip.example")
    found.bots = {
        rigs.E2E.bot: (21, "e2e-bot@zulip.example"),
        rigs.E2E2.bot: (22, "e2e2-bot@zulip.example"),
        rigs.GLOBAL_BOT: (23, "test-bot@zulip.example"),
    }
    found.channels = {rigs.E2E.channel: 31, rigs.E2E2.channel: 32, rigs.ADMINS_CHANNEL: 33,
                      rigs.E2E.alerts: 34, rigs.E2E2.alerts: 35,
                      rigs.E2E2.participants: 36}  # fmt: skip
    return found


def test_toml() -> str:
    from e2e.admin.harness import admin_toml
    from e2e.admin.harness import rig as rigs

    groups = {rigs.E2E.name: "sg-0123456789abcdef0", rigs.E2E2.name: "sg-0123456789abcdef1"}
    return admin_toml.render(fake_ids(), region="us-east-1", ami="ami-0123456789abcdef0",
                             groups=groups)  # fmt: skip


def render_policy() -> Any:
    """This checkout's `workshop/adminserver/render_policy.py`, as a module."""
    import importlib.util

    from e2e.harness.config import CHECKOUT

    path = CHECKOUT / "workshop" / "adminserver" / "render_policy.py"
    spec = importlib.util.spec_from_file_location("ae2e_render_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy() -> dict[str, Any]:
    """The role's policy, as render_policy.py renders it for the test admin.toml."""
    import tomllib

    return render_policy().render(tomllib.loads(test_toml()))


test_toml.__test__ = test_policy.__test__ = False  # helpers, not tests


class FakeError(Exception):
    """botocore's ClientError, as `gate.error_code` reads it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _tags(resource: dict[str, Any]) -> dict[str, str]:
    """`Tags`, or a network interface's `TagSet`, as EC2 describes each."""
    return {t["Key"]: t["Value"] for t in resource.get("Tags") or resource.get("TagSet") or []}


def _matches(resource: dict[str, Any], filters: list[dict[str, Any]], fields: dict) -> bool:
    tags = _tags(resource)
    for f in filters or []:
        name, values = f["Name"], f["Values"]
        if name.startswith("tag:"):
            if tags.get(name[4:]) not in values:
                return False
        elif name == "tag-key":
            if not set(values) & set(tags):
                return False
        elif name in fields and fields[name](resource) not in values:
            return False
    return True


INSTANCE_FIELDS = {"instance-state-name": lambda i: i["State"]["Name"]}
GROUP_FIELDS = {"group-name": lambda g: g["GroupName"], "vpc-id": lambda g: g["VpcId"]}
IMAGE_FIELDS = {"name": lambda i: i["Name"], "state": lambda i: "available"}


class FakeEc2:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ids = itertools.count(1)
        test = [{"Key": "zab-role", "Value": "test"}]
        self.groups: list[dict[str, Any]] = [
            {"GroupName": "zab-test-ssh", "GroupId": "sg-ssh", "VpcId": "vpc-1", "Tags": test,
             "IpPermissions": [_rule("203.0.113.7/32")]},
        ]  # fmt: skip
        self.images: list[dict[str, Any]] = [
            {"ImageId": "ami-test1", "Name": "zab-test-20261013b", "Tags": list(test),
             "CreationDate": "2026-10-13", "Owner": "self"},
            {"ImageId": "ami-ubuntu", "Name": "ubuntu-noble", "Tags": [], "Owner": "canonical",
             "CreationDate": "2026-10-01", "RootDeviceName": "/dev/sda1"},
        ]  # fmt: skip
        self.instances: list[dict[str, Any]] = []
        self.addresses: list[dict[str, Any]] = []
        self.volumes: list[dict[str, Any]] = []
        self.key_pairs: list[dict[str, Any]] = []
        self.interfaces: list[dict[str, Any]] = []
        self.subnets: list[dict[str, Any]] = [  # the default VPC's: us-east-1e offers no t3
            {"SubnetId": "subnet-e", "AvailabilityZone": "us-east-1e", "VpcId": "vpc-1"},
            {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b", "VpcId": "vpc-1"},
            {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a", "VpcId": "vpc-1"},
        ]
        self.offered = ["us-east-1a", "us-east-1b"]  # the zones that offer t3.medium
        self.protected: set[str] = set()
        self.console = ""

    def _log(self, op: str, params: dict[str, Any]) -> None:
        self.calls.append((op, params))

    def writes(self) -> list[str]:
        return [op for op, _ in self.calls if not op.startswith(("describe_", "get_"))]

    # reads
    def describe_vpcs(self, **p):
        self._log("describe_vpcs", p)
        return {"Vpcs": [{"VpcId": "vpc-1"}]}

    def describe_security_groups(self, **p):
        self._log("describe_security_groups", p)
        found = [g for g in self.groups if _matches(g, p.get("Filters"), GROUP_FIELDS)]
        return {"SecurityGroups": copy.deepcopy(found)}

    def describe_images(self, **p):
        self._log("describe_images", p)
        if "ImageIds" in p:
            return {"Images": [i for i in self.images if i["ImageId"] in p["ImageIds"]]}
        owner = "canonical" if p["Owners"] != ["self"] else "self"
        found = [i for i in self.images if i["Owner"] == owner]
        found = [i for i in found if _matches(i, p.get("Filters"), {})]
        return {"Images": copy.deepcopy(found)}

    def describe_instances(self, **p):
        self._log("describe_instances", p)
        if "InstanceIds" in p:
            found = [i for i in self.instances if i["InstanceId"] in p["InstanceIds"]]
        else:
            found = [i for i in self.instances if _matches(i, p.get("Filters"), INSTANCE_FIELDS)]
        return {"Reservations": [{"Instances": copy.deepcopy(found)}]}

    def describe_instance_attribute(self, **p):
        self._log("describe_instance_attribute", p)
        return {"DisableApiTermination": {"Value": p["InstanceId"] in self.protected}}

    def describe_addresses(self, **p):
        self._log("describe_addresses", p)
        return {"Addresses": [a for a in self.addresses if _matches(a, p.get("Filters"), {})]}

    def describe_volumes(self, **p):
        self._log("describe_volumes", p)
        fields = {"status": lambda v: v["State"]}
        return {"Volumes": [v for v in self.volumes if _matches(v, p.get("Filters"), fields)]}

    def delete_volume(self, **p):
        self._log("delete_volume", p)
        self.volumes = [v for v in self.volumes if v["VolumeId"] != p["VolumeId"]]

    def describe_key_pairs(self, **p):
        self._log("describe_key_pairs", p)
        names = p["Filters"][0]["Values"]
        return {"KeyPairs": [k for k in self.key_pairs if k["KeyName"] in names]}

    def get_console_output(self, **p):
        self._log("get_console_output", p)
        return {"Output": self.console}

    def describe_subnets(self, **p):
        self._log("describe_subnets", p)
        fields = {"vpc-id": lambda s: s["VpcId"], "default-for-az": lambda s: "true"}
        return {"Subnets": [s for s in self.subnets if _matches(s, p.get("Filters"), fields)]}

    def describe_instance_type_offerings(self, **p):
        self._log("describe_instance_type_offerings", p)
        assert p["LocationType"] == "availability-zone"
        return {"InstanceTypeOfferings": [{"Location": z, "InstanceType": "t3.medium"}
                                          for z in self.offered]}  # fmt: skip

    def describe_network_interfaces(self, **p):
        self._log("describe_network_interfaces", p)
        fields = {"network-interface-id": lambda i: i["NetworkInterfaceId"]}
        found = [i for i in self.interfaces if _matches(i, p.get("Filters"), fields)]
        return {"NetworkInterfaces": copy.deepcopy(found)}

    def create_network_interface(self, **p):
        self._log("create_network_interface", p)
        eni = {"NetworkInterfaceId": f"eni-{next(self.ids):08x}", "SubnetId": p["SubnetId"],
               "Groups": [{"GroupId": g} for g in p["Groups"]], "Status": "available",
               "TagSet": p["TagSpecifications"][0]["Tags"]}  # EC2's key here  # fmt: skip
        self.interfaces.append(eni)
        return {"NetworkInterface": copy.deepcopy(eni)}

    def delete_network_interface(self, **p):
        self._log("delete_network_interface", p)
        eni = p["NetworkInterfaceId"]
        self.interfaces = [i for i in self.interfaces if i["NetworkInterfaceId"] != eni]

    # changes
    def create_security_group(self, **p):
        self._log("create_security_group", p)
        gid = f"sg-{next(self.ids):04d}"
        tags = p["TagSpecifications"][0]["Tags"]
        self.groups.append({"GroupName": p["GroupName"], "GroupId": gid, "VpcId": p["VpcId"],
                            "Tags": tags, "IpPermissions": []})  # fmt: skip
        return {"GroupId": gid}

    def _group(self, gid: str) -> dict[str, Any]:
        return next(g for g in self.groups if g["GroupId"] == gid)

    def authorize_security_group_ingress(self, **p):
        self._log("authorize_security_group_ingress", p)
        self._group(p["GroupId"])["IpPermissions"] += p["IpPermissions"]

    def revoke_security_group_ingress(self, **p):
        self._log("revoke_security_group_ingress", p)
        gone = {r["CidrIp"] for perm in p["IpPermissions"] for r in perm.get("IpRanges", [])}
        gone6 = {r["CidrIpv6"] for perm in p["IpPermissions"] for r in perm.get("Ipv6Ranges", [])}
        group = self._group(p["GroupId"])
        for perm in group["IpPermissions"]:
            perm["IpRanges"] = [r for r in perm.get("IpRanges", []) if r["CidrIp"] not in gone]
            perm["Ipv6Ranges"] = [
                r for r in perm.get("Ipv6Ranges", []) if r["CidrIpv6"] not in gone6
            ]

    def create_tags(self, **p):
        self._log("create_tags", p)
        ids = ("ImageId", "InstanceId", "GroupId", "KeyPairId")
        for res in self.images + self.instances + self.groups + self.key_pairs:
            if next(res[k] for k in ids if k in res) in p["Resources"]:
                res["Tags"] = res.get("Tags", []) + p["Tags"]

    def run_instances(self, **p):
        self._log("run_instances", p)
        iid = f"i-{next(self.ids):08x}"
        tags = next(s["Tags"] for s in p["TagSpecifications"] if s["ResourceType"] == "instance")
        inst = {"InstanceId": iid, "Tags": tags, "State": {"Name": "running"},
                "PublicIpAddress": "198.51.100.10"}  # fmt: skip
        self.instances.append(inst)
        return {"Instances": [copy.deepcopy(inst)]}

    def add_instance(self, iid: str, tags: dict[str, str], state: str = "running") -> None:
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        self.instances.append({"InstanceId": iid, "Tags": tag_list, "State": {"Name": state},
                               "PublicIpAddress": "198.51.100.9"})  # fmt: skip

    def _set(self, ids: list[str], state: str) -> None:
        for inst in self.instances:
            if inst["InstanceId"] in ids:
                inst["State"] = {"Name": state}
                inst["PublicIpAddress"] = "198.51.100.9" if state == "running" else None

    def start_instances(self, **p):
        self._log("start_instances", p)
        self._set(p["InstanceIds"], "running")

    def stop_instances(self, **p):
        self._log("stop_instances", p)
        self._set(p["InstanceIds"], "stopped")

    def terminate_instances(self, **p):
        self._log("terminate_instances", p)
        if set(p["InstanceIds"]) & self.protected:
            raise FakeError("OperationNotPermitted")
        self._set(p["InstanceIds"], "terminated")
        for addr in self.addresses:
            if addr.get("InstanceId") in p["InstanceIds"]:
                addr.pop("InstanceId")

    def modify_instance_attribute(self, **p):
        self._log("modify_instance_attribute", p)
        if p.get("DisableApiTermination", {}).get("Value") is False:
            self.protected.discard(p["InstanceId"])

    def release_address(self, **p):
        self._log("release_address", p)
        self.addresses = [a for a in self.addresses if a["AllocationId"] != p["AllocationId"]]

    def delete_key_pair(self, **p):
        self._log("delete_key_pair", p)
        self.key_pairs = [k for k in self.key_pairs if k["KeyPairId"] != p["KeyPairId"]]


def _rule(cidr: str) -> dict[str, Any]:
    return {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": cidr}]}


class FakeIam:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.roles: dict[str, dict[str, Any]] = {}
        self.policies: dict[tuple[str, str], Any] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.attached: list[dict[str, str]] = []  # managed policies on the role

    def writes(self) -> list[str]:
        return [op for op, _ in self.calls if not op.startswith(("get_", "list_"))]

    def __getattr__(self, op: str):
        def call(**p):
            self.calls.append((op, p))
            return getattr(self, "_" + op)(**p)

        return call

    def _get_role(self, RoleName):
        if RoleName not in self.roles:
            raise FakeError("NoSuchEntity")
        return {"Role": self.roles[RoleName]}

    def _create_role(self, RoleName, Tags, **p):
        self.roles[RoleName] = {"RoleName": RoleName, "Tags": Tags}

    def _list_role_policies(self, RoleName):
        return {"PolicyNames": [n for r, n in self.policies if r == RoleName]}

    def _get_role_policy(self, RoleName, PolicyName):
        if (RoleName, PolicyName) not in self.policies:
            raise FakeError("NoSuchEntity")
        return {"PolicyDocument": self.policies[RoleName, PolicyName]}

    def _put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        import json

        self.policies[RoleName, PolicyName] = json.loads(PolicyDocument)

    def _list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": self.attached}

    def _delete_role_policy(self, RoleName, PolicyName):
        del self.policies[RoleName, PolicyName]

    def _get_instance_profile(self, InstanceProfileName):
        if InstanceProfileName not in self.profiles:
            raise FakeError("NoSuchEntity")
        return {"InstanceProfile": self.profiles[InstanceProfileName]}

    def _create_instance_profile(self, InstanceProfileName, Tags):
        self.profiles[InstanceProfileName] = {"Roles": [], "Tags": Tags}

    def _add_role_to_instance_profile(self, InstanceProfileName, RoleName):
        self.profiles[InstanceProfileName]["Roles"].append({"RoleName": RoleName})


class FakeZulip:
    """`zab-tester` (or `zab-tester2`) as `ZulipClient.request` sees Zulip."""

    site = "https://zulip.example"

    def __init__(self, me: int, *, users=None, bots=None, subs=None, members=None) -> None:
        self.me, self.calls = me, []
        self.users = users if users is not None else []
        self.bots = bots if bots is not None else []
        self.subs = subs if subs is not None else []
        self.members = members if members is not None else {}
        #: users the bulk GET /users leaves out, though GET /users/{email} finds them (live)
        self.hidden: list[dict[str, Any]] = []
        self.ids = itertools.count(100)

    def posts(self) -> list[tuple[str, dict[str, Any]]]:
        return [(e, p) for m, e, p in self.calls if m != "GET"]

    async def close(self) -> None:
        pass

    async def request(self, method: str, endpoint: str, **p: Any) -> dict[str, Any]:
        self.calls.append((method, endpoint, p))
        if (method, endpoint) == ("GET", "users/me"):
            return {"user_id": self.me}
        if (method, endpoint) == ("GET", "users"):
            return {"members": self.users}
        if (method, endpoint) == ("GET", "bots"):
            return {"bots": self.bots}
        if method == "GET" and endpoint.startswith("users/") and "@" in endpoint:
            from urllib.parse import unquote

            email = unquote(endpoint[6:])
            found = [u for u in self.users + self.hidden if u.get("email") == email]
            if not found:
                from zab.zulip.client import ZulipError

                raise ZulipError("BAD_REQUEST", 400)
            return {"user": found[0]}
        if method == "GET" and endpoint.startswith("users/") and endpoint[6:].isdigit():
            uid = int(endpoint.split("/")[1])
            return {"user": next(u for u in self.users if u["user_id"] == uid)}
        if (method, endpoint) == ("POST", "bots"):
            uid = next(self.ids)
            email = f"{p['short_name']}-bot@zulip.example"
            self.users.append({"user_id": uid, "email": email, "is_bot": True,
                               "is_active": True, "bot_owner_id": self.me})  # fmt: skip
            self.bots.append({"username": email, "full_name": p["full_name"], "api_key": f"k{uid}"})
            return {"user_id": uid, "api_key": f"k{uid}"}
        if (method, endpoint) == ("GET", "users/me/subscriptions"):
            return {"subscriptions": self.subs}
        if method == "GET" and endpoint.endswith("/members"):
            return {"subscribers": self.members.get(int(endpoint.split("/")[1]), [])}
        if (method, endpoint) == ("POST", "users/me/subscriptions"):
            for sub in p["subscriptions"]:
                found = [s for s in self.subs if s["name"] == sub["name"]]
                sid = found[0]["stream_id"] if found else next(self.ids)
                if not found:
                    self.subs.append({"name": sub["name"], "stream_id": sid})
                self.members.setdefault(sid, []).extend(p.get("principals", []))
            return {}
        if (method, endpoint) == ("GET", "get_stream_id"):
            return {
                "stream_id": next(s["stream_id"] for s in self.subs if s["name"] == p["stream"])
            }
        raise AssertionError(f"unexpected {method} {endpoint}")
