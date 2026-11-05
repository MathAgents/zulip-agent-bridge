"""The e2e harness's configuration and guarded AWS, offline with fakes (issue #263)."""

from __future__ import annotations

import pytest

from e2e.harness import config
from e2e.harness.aws import NotTestInfrastructure, TestEc2

# ------------------------------------------------------------------------------- config


def test_config_defaults_live_in_the_home_config_dir(tmp_path):
    """The development machine's layout with no variable set: the rig's files under ~/.config/zab,
    the VM's key the default identity (paths only: nothing here names an instance or a channel
    id)."""
    cfg = config.load({"HOME": str(tmp_path)})
    base = tmp_path / ".config" / "zab"
    assert cfg.roster == base / "test-roster.csv" and cfg.tester_zuliprc == base / "tester.zuliprc"
    assert cfg.ssh_key == tmp_path / ".ssh" / "id_ed25519"
    assert cfg.out_dir == base / "test-out" and cfg.known_hosts == base / "test-known_hosts"
    assert cfg.instance_tags == {
        "zab-role": "test",
        "workshop": "zab-test",
        "participant": "zab-test",
    }
    assert len(cfg.missing()) == 5  # nothing exists in a fresh home: one clear list
    for name in ("tester.zuliprc", "tester2.zuliprc", "test-roster.csv", "test-known_hosts"):
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text("x")
    assert [m.split(":")[0] for m in cfg.missing()] == ["ZAB_E2E_SSH_KEY"]
    moved = config.load({"HOME": str(tmp_path), "ZAB_E2E_OUT_DIR": str(tmp_path / "out")})
    assert moved.out_dir == tmp_path / "out" and moved.known_hosts == cfg.known_hosts


@pytest.mark.parametrize(
    "env",
    [
        {"ZAB_E2E_SCOPE": "workshop=zab-test"},  # no zab-role=test: refused
        {"ZAB_E2E_SCOPE": "zab-role=prod"},
        {"ZAB_E2E_SCOPE": "zab-role=test,participant=x"},
        {"ZAB_E2E_SLUG": "Bad Slug"},
        {"ZAB_E2E_REGION": "mars"},
        {"ZAB_E2E_REPO": "no-owner"},
        {"ZAB_E2E_FILES_URL": "http://files.example"},
        {"ZAB_E2E_CHANNEL": "general"},  # the rig posts in zab-test* and zab-dev* only
        {"ZAB_E2E_SHARED_CHANNEL": "drew-sandbox"},
        {"ZAB_E2E_CHANNEL": "zab-test-sandbox; x"},
    ],
)
def test_config_refuses(env, tmp_path):
    with pytest.raises(ValueError):
        config.load({"HOME": str(tmp_path), **env})


def test_config_accepts_the_rigs_channels(tmp_path):
    env = {"HOME": str(tmp_path), "ZAB_E2E_CHANNEL": "zab-dev-2"}
    cfg = config.load({**env, "ZAB_E2E_SHARED_CHANNEL": "zab-test-shared"})
    assert (cfg.channel, cfg.shared_channel) == ("zab-dev-2", "zab-test-shared")


# ----------------------------------------------------------------------------------- AWS

SCOPE = {"zab-role": "test", "workshop": "zab-test"}
TEST_TAGS = [{"Key": k, "Value": v} for k, v in {**SCOPE, "participant": "zab-test"}.items()]
FLEET_TAGS = [{"Key": "workshop", "Value": "zab-test"}, {"Key": "participant", "Value": "zab-test"}]


class FakeClient:
    def __init__(self) -> None:
        self.instances = {
            "i-test": {"InstanceId": "i-test", "State": {"Name": "running"}, "Tags": TEST_TAGS},
            "i-other": {"InstanceId": "i-other", "State": {"Name": "running"}, "Tags": FLEET_TAGS},
        }
        self.mutations: list[tuple[str, list[str]]] = []

    def get_paginator(self, name):
        client = self

        class Pages:
            def paginate(self, Filters):
                # like EC2 here: answers every instance, so the harness's own check must hold
                return [{"Reservations": [{"Instances": list(client.instances.values())}]}]

        return Pages()

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [self.instances[i] for i in InstanceIds]}]}

    def stop_instances(self, InstanceIds):
        self.mutations.append(("stop", InstanceIds))
        for i in InstanceIds:
            self.instances[i]["State"]["Name"] = "stopped"

    def start_instances(self, InstanceIds):
        self.mutations.append(("start", InstanceIds))

    def reboot_instances(self, InstanceIds):
        self.mutations.append(("reboot", InstanceIds))


def test_aws_acts_only_on_instances_tagged_as_test_infrastructure():
    client = FakeClient()
    ec2 = TestEc2("us-east-1", SCOPE, client=client, sleep=lambda s: None)
    assert ec2.find({"participant": "zab-test"})["InstanceId"] == "i-test"
    for act in (ec2.stop, ec2.start, ec2.reboot):
        with pytest.raises(NotTestInfrastructure):
            act("i-other")
    assert client.mutations == []  # nothing was sent for the untagged instance
    ec2.stop("i-test")
    assert client.mutations == [("stop", ["i-test"])]
    assert ec2.wait_state("i-test", "stopped") < 1
    with pytest.raises(NotTestInfrastructure):
        TestEc2("us-east-1", {"workshop": "zab-test"}, client=client)


def test_wait_state_times_out():
    clock = iter(range(0, 10_000, 10))
    ec2 = TestEc2("us-east-1", SCOPE, client=FakeClient(), sleep=lambda s: None)
    ec2.clock = lambda: next(clock)
    with pytest.raises(TimeoutError):
        ec2.wait_state("i-test", "stopped", timeout=60)
    with pytest.raises(TimeoutError, match="i-test still running after 60 s"):
        ec2.wait_while("i-test", "running", timeout=60)
    assert ec2.wait_while("i-test", "stopping") == "running"


def test_describe_refuses_an_answer_about_another_instance():
    """Reviewer on #286: the tags checked must be the requested instance's own."""
    client = FakeClient()
    client.instances["i-asked"] = client.instances["i-test"]  # answers with i-test's record
    ec2 = TestEc2("us-east-1", SCOPE, client=client, sleep=lambda s: None)
    with pytest.raises(NotTestInfrastructure, match="asked for i-asked"):
        ec2.stop("i-asked")
    assert client.mutations == []
