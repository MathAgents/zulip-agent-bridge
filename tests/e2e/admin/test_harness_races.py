"""The rig's setup and teardown against an eventually consistent EC2, offline (issue #356):
each change made, and read back or acted on right after, as the live runs met it."""

from __future__ import annotations

import copy

import pytest

from e2e.admin.fakes import INSTANCE_FIELDS, FakeEc2, FakeError, _matches
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.aws_rig import TERMINATE_RETRIES_S
from e2e.admin.harness.settle import PROTECTION_S, WITHIN_S
from e2e.admin.run import teardown
from e2e.admin.test_harness_aws import Clock, make

SCOPE = dict(rigs.E2E.tags)  # the full scope, as the server imports and launches with it


class Late(FakeEc2):
    """EC2 whose every change shows only `stale` reads late, in what reads could show it, and
    refuses a call that depends on one until it shows (the live runs):
    - a protection lifted is still read on, and a terminate refused (`OperationNotPermitted`),
      and, `refused`, a few more times after; a protection set just before, `unshown`, is
      still read off (a terminate is refused all the same: it is on);
    - an instance terminated or stopped is still listed (by filters) in its old state, and a
      start right after a stop refused (`IncorrectInstanceState`);
    - a terminated instance's Elastic IP still shows it associated; one released is still
      listed;
    - a tag added to an image is not on it yet; a key pair deleted is still listed.
    Reads by ID (`InstanceIds`, as `AwsRig.wait`) are consistent."""

    def __init__(self, stale: int = 2) -> None:
        super().__init__()
        self.stale = stale
        self.refused: dict[str, int] = {}  # instance → terminates refused after the lift shows
        self.lifted: dict[str, int] = {}  # instance → reads still showing its protection
        self.lift_late = stale  # the reads a lift takes to show
        self.unshown: dict[str, int] = {}  # instance → reads not showing it yet
        self.shown: dict[str, list] = {}  # instance → [reads left, its old state]
        self.stopping: dict[str, int] = {}  # instance → starts still refused
        self.held: dict[str, list] = {}  # allocation → [reads left, the instance]
        self.released: dict[str, list] = {}  # allocation → [reads left, the address]
        self.untagged: list[list] = []  # [reads left, resources, tags]
        self.deleted: dict[str, list] = {}  # key pair → [reads left, the pair]

    def describe_instance_attribute(self, **p):
        found = super().describe_instance_attribute(**p)
        for late, value in ((self.lifted, True), (self.unshown, False)):
            if late.get(p["InstanceId"]):
                late[p["InstanceId"]] -= 1
                found["DisableApiTermination"]["Value"] = value
        return found

    def modify_instance_attribute(self, **p):
        super().modify_instance_attribute(**p)
        if p.get("DisableApiTermination", {}).get("Value") is False:
            self.lifted[p["InstanceId"]] = self.lift_late

    def terminate_instances(self, **p):
        for iid in p["InstanceIds"]:
            protected = iid in self.protected or self.lifted.get(iid)
            if protected or self.refused.get(iid):
                self._log("terminate_instances", p)
                if not protected:
                    self.refused[iid] -= 1
                raise FakeError("OperationNotPermitted")
        for inst in self.instances:
            if inst["InstanceId"] in p["InstanceIds"]:
                self.shown[inst["InstanceId"]] = [self.stale, inst["State"]["Name"]]
        for addr in self.addresses:
            if addr.get("InstanceId") in p["InstanceIds"]:
                self.held[addr["AllocationId"]] = [self.stale, addr["InstanceId"]]
        super().terminate_instances(**p)

    def stop_instances(self, **p):
        for inst in self.instances:
            if inst["InstanceId"] in p["InstanceIds"]:
                self.shown[inst["InstanceId"]] = [self.stale, inst["State"]["Name"]]
                self.stopping[inst["InstanceId"]] = self.stale
        super().stop_instances(**p)

    def start_instances(self, **p):
        if any(self.stopping.get(iid) for iid in p["InstanceIds"]):
            self._log("start_instances", p)
            for iid in p["InstanceIds"]:
                self.stopping[iid] = max(self.stopping.get(iid, 0) - 1, 0)
            raise FakeError("IncorrectInstanceState")
        super().start_instances(**p)

    def describe_instances(self, **p):
        if "InstanceIds" in p:
            return super().describe_instances(**p)
        self._log("describe_instances", p)
        found = []
        for inst in self.instances:
            late = self.shown.get(inst["InstanceId"])
            view = {**inst, "State": {"Name": late[1]}} if late and late[0] else inst
            if _matches(view, p.get("Filters"), INSTANCE_FIELDS):
                found.append(copy.deepcopy(view))
                if late and late[0]:
                    late[0] -= 1
        return {"Reservations": [{"Instances": found}]}

    def describe_addresses(self, **p):
        self._log("describe_addresses", p)
        found = []
        for addr in self.addresses + [r[1] for r in self.released.values() if r[0]]:
            late = self.held.get(addr["AllocationId"])
            view = {**addr, "InstanceId": late[1]} if late and late[0] else addr
            if _matches(view, p.get("Filters"), {}):
                found.append(copy.deepcopy(view))
                for left in (late, self.released.get(addr["AllocationId"])):
                    if left and left[0]:
                        left[0] -= 1
        return {"Addresses": found}

    def release_address(self, **p):
        addr = next(a for a in self.addresses if a["AllocationId"] == p["AllocationId"])
        super().release_address(**p)
        self.released[p["AllocationId"]] = [self.stale, addr]

    def create_tags(self, **p):
        self._log("create_tags", p)
        self.untagged.append([self.stale, p["Resources"], p["Tags"]])

    def describe_images(self, **p):
        for late in list(self.untagged):
            if late[0]:
                late[0] -= 1
                continue
            self.untagged.remove(late)
            for image in self.images:
                if image["ImageId"] in late[1]:
                    image["Tags"] = image.get("Tags", []) + late[2]
        return super().describe_images(**p)

    def delete_key_pair(self, **p):
        pair = next(k for k in self.key_pairs if k["KeyPairId"] == p["KeyPairId"])
        super().delete_key_pair(**p)
        self.deleted[p["KeyPairId"]] = [self.stale, pair]

    def describe_key_pairs(self, **p):
        found = super().describe_key_pairs(**p)["KeyPairs"]
        for late in self.deleted.values():
            if late[0]:
                late[0] -= 1
                found.append(late[1])
        return {"KeyPairs": found}


def rig_on(tmp_path, ec2: Late):
    clock = Clock()
    aws, _, _, _ = make(tmp_path, ec2=ec2, clock=clock)
    aws.sleep = clock.sleep
    return aws, clock


def fleet_of_two(ec2: Late) -> None:
    """A4's: zab-test-a protected; each with its Elastic IP; the server running."""
    for iid, slug in (("i-a", "zab-test-a"), ("i-b", "zab-test-b")):
        ec2.add_instance(iid, {**SCOPE, "participant": slug})
        tags = [{"Key": k, "Value": v} for k, v in SCOPE.items()]
        ec2.addresses.append({"AllocationId": f"eipalloc-{iid}", "InstanceId": iid, "Tags": tags})
    ec2.add_instance("i-s", rigs.SERVER_TAGS)
    ec2.protected = {"i-a"}


def calls_on(ec2: Late, iid: str) -> list[str]:
    ops = ("describe_instance_attribute", "modify_instance_attribute", "terminate_instances")
    return [
        op
        for op, p in ec2.calls
        if op in ops and iid in (p.get("InstanceId"), *p.get("InstanceIds", []))
    ]


def test_a_teardown_that_races_ec2_succeeds_and_checks_clean(tmp_path):
    """The second live run's teardown: the protection read on twice after its lift, the
    terminate refused once after; then the addresses, the server and the check, each read
    behind its change."""
    ec2 = Late()
    fleet_of_two(ec2)
    ec2.refused = {"i-a": 1}
    aws, clock = rig_on(tmp_path, ec2)
    assert teardown(aws, keep_server=False) == []
    assert calls_on(ec2, "i-a") == [
        "describe_instance_attribute",  # on
        "modify_instance_attribute",  # lifted
        *["describe_instance_attribute"] * 3,  # on, on (stale), off
        "terminate_instances",  # refused once more
        "describe_instance_attribute",  # off: nothing to lift again
        "terminate_instances",
    ]
    states = {i["InstanceId"]: i["State"]["Name"] for i in ec2.instances}
    assert states == {"i-a": "terminated", "i-b": "terminated", "i-s": "stopped"}
    assert ec2.addresses == [] and clock.now < 600


def test_a_lift_shown_minutes_late_is_waited_for(tmp_path):
    """The live runs: `disableApiTermination` showed a change 157 s late. A lift shown 200 s
    late (40 reads, 5 s apart) is past `WITHIN_S`, within `PROTECTION_S`."""
    ec2 = Late()
    fleet_of_two(ec2)
    ec2.lift_late = 40
    aws, clock = rig_on(tmp_path, ec2)
    assert WITHIN_S < 200 < PROTECTION_S
    assert teardown(aws, keep_server=False) == []
    assert calls_on(ec2, "i-a")[-1] == "terminate_instances" and clock.now >= 200


def test_a_protection_read_off_too_soon_is_lifted_once_a_terminate_is_refused(tmp_path):
    """Review of #377: A4's protection, set just before a short run's teardown, read off at
    first: nothing lifts it, and the terminate is refused; it is read again, now on, and
    lifted, and the next try goes through."""
    ec2 = Late()
    fleet_of_two(ec2)
    ec2.unshown = {"i-a": 1}
    aws, _ = rig_on(tmp_path, ec2)
    assert teardown(aws, keep_server=False) == []
    assert calls_on(ec2, "i-a") == [
        "describe_instance_attribute",  # off: stale
        "terminate_instances",  # refused: it is on
        "describe_instance_attribute",  # on
        "modify_instance_attribute",  # lifted
        *["describe_instance_attribute"] * 3,  # on, on (stale), off
        "terminate_instances",
    ]
    assert {i["InstanceId"]: i["State"]["Name"] for i in ec2.instances}["i-a"] == "terminated"


def test_a_refused_terminate_never_stops_the_rest(tmp_path):
    ec2 = Late()
    fleet_of_two(ec2)
    ec2.refused = {"i-a": 10**6}  # never
    ec2.volumes = [{"VolumeId": "vol-left", "State": "available",
                    "Tags": [{"Key": k, "Value": v} for k, v in SCOPE.items()]}]  # fmt: skip
    aws, clock = rig_on(tmp_path, ec2)
    problems = teardown(aws, keep_server=False)
    assert problems == [
        "teardown: the participants: RuntimeError: not all torn down: i-a: FakeError: "
        "OperationNotPermitted",
        "teardown check: instances not terminated: i-a",
        "teardown check: Elastic IPs not released: eipalloc-i-a",
    ]
    states = {i["InstanceId"]: i["State"]["Name"] for i in ec2.instances}
    assert states == {"i-a": "running", "i-b": "terminated", "i-s": "stopped"}
    assert [a["AllocationId"] for a in ec2.addresses] == ["eipalloc-i-a"] and ec2.volumes == []
    assert calls_on(ec2, "i-a").count("terminate_instances") == len(TERMINATE_RETRIES_S) + 1
    assert sum(TERMINATE_RETRIES_S) >= 300  # lagging enforcement: 5 minutes at least
    # every wait bounded: the retries, then the check's reads of what is left (instances and
    # addresses, `WITHIN_S` each), and a few reads behind each change
    assert clock.now <= sum(TERMINATE_RETRIES_S) + 2 * WITHIN_S + 60


def test_a_start_right_after_a_stop_is_tried_again(tmp_path):
    ec2 = Late()
    ec2.add_instance("i-s", rigs.SERVER_TAGS)
    aws, _ = rig_on(tmp_path, ec2)
    aws.stop(aws.server())
    assert aws.start({**aws.describe("i-s")}) == "198.51.100.9"
    assert [op for op in ec2.writes()] == ["stop_instances", *["start_instances"] * 3]


def test_the_image_tag_and_the_key_pairs_deleted_show_before_the_setup_goes_on(tmp_path):
    ec2 = Late()
    scope = [{"Key": k, "Value": v} for k, v in SCOPE.items()]  # as the server imports it
    ec2.key_pairs = [{"KeyPairId": "key-1", "KeyName": rigs.E2E.key_name, "Tags": scope}]
    aws, _ = rig_on(tmp_path, ec2)
    image = aws.test_image()
    assert {t["Key"]: t["Value"] for t in image["Tags"]}["zab-scope"] == rigs.SCOPE[1]
    aws.drop_key_pairs()
    names = {"Name": "key-name", "Values": [ws.key_name for ws in rigs.WORKSHOPS]}
    assert aws.aws.ec2("describe_key_pairs", Filters=[names])["KeyPairs"] == []


class Refusing(FakeEc2):
    """EC2 refusing every change as `code`."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def _log(self, op, params):
        super()._log(op, params)
        if not op.startswith(("describe_", "get_")):
            raise FakeError(self.code)


@pytest.mark.parametrize("change", ["terminate", "stop", "start"])
def test_only_the_refusal_of_a_state_not_yet_left_is_tried_again(tmp_path, change):
    """`_retried`: a refusal as `UnauthorizedOperation` is a finding, raised at once."""
    ec2 = Refusing("UnauthorizedOperation")
    ec2.add_instance("i-s", rigs.SERVER_TAGS, state="stopped" if change == "start" else "running")
    ec2.add_instance("i-a", {**SCOPE, "participant": "zab-test-a"})
    aws, clock = rig_on(tmp_path, ec2)
    act = {"terminate": lambda: aws.end(aws.describe("i-a")),
           "stop": lambda: aws.stop(aws.describe("i-s")),
           "start": lambda: aws.start(aws.describe("i-s"))}[change]  # fmt: skip
    with pytest.raises(FakeError, match="UnauthorizedOperation"):
        act()
    assert len(ec2.writes()) == 1 and clock.now == 0


def test_a_state_that_never_comes_is_a_timeout(tmp_path):
    ec2 = FakeEc2()
    ec2.add_instance("i-s", rigs.SERVER_TAGS)
    aws, clock = rig_on(tmp_path, ec2)
    with pytest.raises(TimeoutError, match="i-s still running after 60 s"):
        aws.wait("i-s", "stopped", timeout=60)
    assert clock.now == 60
