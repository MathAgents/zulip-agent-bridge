"""A13's probe script (`test_scenarios_9_checks.PROBE`), offline, with a fake boto3: the calls
it makes as the server's role, and which of them the scenario expects allowed (issue #356).
A12's sweep script (`SWEEP`), offline, on decoy files in a tmp dir (#519)."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from e2e.admin import test_scenarios_9_checks as checks
from e2e.admin.harness import rig as rigs
from e2e.harness import report
from e2e.harness.vm import Result
from e2e.test_recorder import inner_pytest

ROOTS = {"i-263": ["vol-263", 8], "i-server": ["vol-server", 30], "i-p1": ["vol-p1", 200]}
ARGV = ["us-east-1", "i-263", "i-server", "ami-1", "sg-fleet", "zab-admin-zab-admin-e2e",
        "t3.medium", "t3.small", "subnet-a", "eni-foreign", json.dumps(rigs.E2E.tags),
        json.dumps(ROOTS), "i-p1"]  # fmt: skip


class FakeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


#: The fake participants' Elastic IPs, as `describe_addresses` lists them (A4 associates one).
ADDRESSES = {"i-p1": [{"AllocationId": "eipalloc-p1", "InstanceId": "i-p1"}]}


def run_probe(
    monkeypatch, capsys, notes: list[str] | None = None
) -> tuple[dict[str, str], list[tuple[str, dict]]]:
    """PROBE run here with ARGV: (its codes, every call made); its notes added to `notes`."""
    calls: list[tuple[str, dict]] = []

    class Ec2:
        def describe_addresses(self, Filters):  # a read, not a DryRun call
            calls.append(("describe_addresses", {"Filters": Filters}))
            (held,) = Filters
            assert held["Name"] == "instance-id"
            return {"Addresses": [a for vm in held["Values"] for a in ADDRESSES.get(vm, [])]}

        def __getattr__(self, op):
            def call(**params):
                calls.append((op, params))
                raise FakeError("DryRunOperation")

            return call

    fake = types.ModuleType("boto3")
    fake.client = lambda service, region_name: Ec2()
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(sys, "argv", ["-c", *ARGV])
    exec(compile(checks.PROBE, "<probe>", "exec"), {"__name__": "__main__"})  # noqa: S102
    said = json.loads(capsys.readouterr().out)
    if notes is not None:
        notes += said["notes"]
    return said["codes"], calls


def test_the_probe_asks_for_an_existing_interface_beside_a_new_one(monkeypatch, capsys):
    """#356's V4 check: the workshop's own launch, but with the foreign interface in place of
    the fleet's group, is expected refused; a launch creating its own interface in the same
    subnet is its control, expected allowed."""
    codes, calls = run_probe(monkeypatch, capsys)
    launches = [p for op, p in calls if op == "run_instances"]
    dry = [p for op, p in calls if op != "describe_addresses"]
    assert all(p["DryRun"] is True for p in dry) and len(launches) == 6
    existing = [p for p in launches if "NetworkInterfaceId" in str(p.get("NetworkInterfaces"))]
    assert len(existing) == 1
    (attach,) = existing
    assert attach["NetworkInterfaces"] == [{"DeviceIndex": 0, "NetworkInterfaceId": "eni-foreign"}]
    assert "SecurityGroupIds" not in attach and "SubnetId" not in attach
    assert attach["InstanceType"] == "t3.medium" and attach["KeyName"] == ARGV[5]
    assert attach["ImageId"] == "ami-1"
    kinds = [spec["ResourceType"] for spec in attach["TagSpecifications"]]
    assert kinds == ["instance", "volume"]
    new = [p for p in launches if p.get("NetworkInterfaces") and p is not attach]
    assert [p["NetworkInterfaces"] for p in new] == [
        [{"DeviceIndex": 0, "SubnetId": "subnet-a", "Groups": ["sg-fleet"]}]
    ]
    # the scenario's rule: "(control)" in the name is expected allowed, anything else refused
    names = [name for name in codes if "RunInstances" in name]
    refused = [name for name in names if "(control)" not in name]
    assert any("eni-foreign" in name for name in refused)
    assert not any("eni-foreign" in name for name in names if name not in refused)
    assert any("a new interface" in name and "(control)" in name for name in names)
    assert set(codes.values()) == {"DryRunOperation"}


def test_the_probe_launches_without_a_workshop_or_in_another_scope(monkeypatch, capsys):
    """#422: beside the control, which carries the workshop's full scope, a launch that only
    lacks the `workshop` tag, and one that only has another `zab-scope`, both expected refused;
    the #263 VM is tagged neither into the workshop nor into the scope."""
    codes, calls = run_probe(monkeypatch, capsys)

    def tagged(params):
        specs = params["TagSpecifications"]
        assert [s["ResourceType"] for s in specs] == ["instance", "volume"]
        assert specs[0]["Tags"] == specs[1]["Tags"]
        return {t["Key"]: t["Value"] for t in specs[0]["Tags"]}

    launches = [tagged(p) for op, p in calls if op == "run_instances"]
    full = rigs.E2E.tags
    assert launches.count(full) == 4  # the type, the control, the two interfaces
    nameless = {k: v for k, v in full.items() if k != "workshop"}
    assert nameless in launches and full | {"zab-scope": "zab-foreign"} in launches
    for name in ("the scope without a workshop tag", "zab-scope=zab-foreign"):
        assert f"RunInstances, {name}" in codes  # no "(control)": expected refused
    retags = [(p["Resources"], p["Tags"]) for op, p in calls if op == "create_tags"]
    assert retags == [
        (["i-263"], [{"Key": "workshop", "Value": rigs.E2E.name}]),
        (["i-263"], [{"Key": "zab-scope", "Value": rigs.SCOPE[1]}]),
        # their control (review of #426): a retag the role may make, on a fake participant
        (["i-p1"], [{"Key": "participant", "Value": "zab-a13-dry-run"}]),
    ]
    assert "CreateTags participant, participant i-p1 (control)" in codes
    assert sum("CreateTags" in name and "(control)" not in name for name in codes) == 2


def test_the_probe_grows_the_servers_and_the_263_vms_volumes_beside_a_participants(
    monkeypatch, capsys
):
    """#553 (the review of #561, N1): `ModifyVolume` of the server's volume (no scope there) and of
    the #263 VM's (no scope), each by 1 GB, expected refused; a participant's, the control."""
    codes, calls = run_probe(monkeypatch, capsys)
    grows = [p for op, p in calls if op == "modify_volume"]
    assert grows == [
        {"DryRun": True, "VolumeId": "vol-server", "Size": 31},
        {"DryRun": True, "VolumeId": "vol-263", "Size": 9},
        {"DryRun": True, "VolumeId": "vol-p1", "Size": 201},
    ]
    names = [name for name in codes if name.startswith("ModifyVolume")]
    assert names == [
        "ModifyVolume, the server's volume (no scope; DenyServer)",
        "ModifyVolume, the #263 VM's volume",
        "ModifyVolume, participant i-p1's volume (control)",
    ]


def test_the_probe_moves_a_fleet_address_onto_the_foreign_interface_beside_its_instance(
    monkeypatch, capsys
):
    """#415: `Fleet` grants AssociateAddress on elastic-ip and instance only, so a fake
    participant's address onto the probe interface (outside the workshop) is expected refused;
    the same address onto its own instance is the control. Both may reassociate, so that only
    IAM can refuse them; the address is the participant's, found by its instance."""
    notes: list[str] = []
    codes, calls = run_probe(monkeypatch, capsys, notes)
    assert (
        "describe_addresses",
        {"Filters": [{"Name": "instance-id", "Values": ["i-p1"]}]},
    ) in calls
    moves = [p for op, p in calls if op == "associate_address"]
    common = {"DryRun": True, "AllocationId": "eipalloc-p1", "AllowReassociation": True}
    assert moves == [
        common | {"NetworkInterfaceId": "eni-foreign"},
        common | {"InstanceId": "i-p1"},
    ]
    names = [name for name in codes if name.startswith("AssociateAddress")]
    assert names == [
        "AssociateAddress, participant i-p1's address onto interface eni-foreign, outside the "
        "workshop",
        "AssociateAddress, participant i-p1's address onto its instance (control)",
    ]
    assert [checks.expected(name, "UnauthorizedOperation") for name in names] == [True, False]
    assert [checks.expected(name, "DryRunOperation") for name in names] == [False, True]
    assert notes == []


def test_without_an_address_or_a_participant_associate_address_is_a_note(monkeypatch, capsys):
    monkeypatch.setattr(sys.modules[__name__], "ADDRESSES", {})
    notes: list[str] = []
    codes, calls = run_probe(monkeypatch, capsys, notes)
    assert not [op for op, _ in calls if op == "associate_address"]
    assert notes == ["no Elastic IP on participant i-p1: AssociateAddress not probed"]
    monkeypatch.setattr(sys.modules[__name__], "ARGV", ARGV[:-1])  # no fake participant
    notes.clear()
    codes, calls = run_probe(monkeypatch, capsys, notes)
    assert not [op for op, _ in calls if op in ("describe_addresses", "associate_address")]
    assert notes == [] and not [name for name in codes if "participant" in name]


@pytest.mark.parametrize(
    ("call", "code", "taken"),
    [
        ("ModifyVolume, participant i-p1's volume (control)", "DryRunOperation", True),
        (
            "ModifyVolume, participant i-p1's volume (control)",
            "VolumeModificationRateExceeded",
            True,
        ),
        ("ModifyVolume, participant i-p1's volume (control)", "IncorrectModificationState", True),
        # none of these shows that IAM let it through (Codex round 1 on #561)
        ("ModifyVolume, participant i-p1's volume (control)", "UnauthorizedOperation", False),
        ("ModifyVolume, participant i-p1's volume (control)", "AccessDenied", False),
        ("ModifyVolume, participant i-p1's volume (control)", "EndpointConnectionError", False),
        ("ModifyVolume, participant i-p1's volume (control)", "ParamValidationError", False),
        ("ModifyVolume, participant i-p1's volume (control)", "no error", False),
        ("StopInstances, participant i-p1 (control)", "VolumeModificationRateExceeded", False),
        ("ModifyVolume, the server's volume (no scope; DenyServer)", "UnauthorizedOperation", True),
        (
            "ModifyVolume, the server's volume (no scope; DenyServer)",
            "VolumeModificationRateExceeded",
            False,
        ),
        ("ModifyVolume, the #263 VM's volume", "DryRunOperation", False),
    ],
)
def test_what_a13_takes_as_each_answer(call, code, taken):
    assert checks.expected(call, code) is taken


def test_the_probe_reads_the_scope_by_key(monkeypatch, capsys):
    """The #263 VM's two retags are the workshop's and the scope's, whatever the order of the
    scope's tags (review of #426)."""
    reordered = json.dumps(dict(reversed(list(rigs.E2E.tags.items()))))
    argv = [reordered if arg == json.dumps(rigs.E2E.tags) else arg for arg in ARGV]
    assert argv != ARGV
    monkeypatch.setattr(sys.modules[__name__], "ARGV", argv)
    _, calls = run_probe(monkeypatch, capsys)
    keys = [p["Tags"][0]["Key"] for op, p in calls if op == "create_tags"]
    assert keys == ["workshop", "zab-scope", "participant"]


DECOY = "DECOY-SECRET-519"  # a decoy, never a real key
MALFORMED = "ws/zab-admin-e2e/out/zab-test-a/claude.zuliprc"


class LocalServer:
    """Plays the server for A12's `sweep`: runs SWEEP here (`python3 -I -c`, the channel texts
    on stdin), on `root/etc` and `root/var` in place of the server's directories."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, remote, *, input=None, timeout=120, check=True) -> Result:
        assert remote[:4] == ["sudo", "python3", "-I", "-c"] and not check  # its own assert
        script = remote[4].replace('"/etc/zab-admin/secrets"', repr(str(self.root / "etc")))
        script = script.replace('"/var/lib/zab-admin"', repr(str(self.root / "var")))
        assert script.count(str(self.root)) == 2
        done = subprocess.run(  # noqa: S603 - an argument list
            [sys.executable, "-I", "-c", script], input=input, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
        return Result(done.returncode, done.stdout, done.stderr)


def malformed(root: Path) -> None:
    """A participant bot's zuliprc with no [api] header, its line a decoy key."""
    rc = root / "var" / MALFORMED
    rc.parent.mkdir(parents=True)
    rc.write_text(f"key={DECOY}\n")


def test_the_sweep_finds_the_servers_secrets(tmp_path):
    etc, out = tmp_path / "etc", tmp_path / "var" / "ws" / "zab-admin-e2e" / "out" / "zab-test-a"
    (etc / "bots").mkdir(parents=True)
    out.mkdir(parents=True)
    (etc / "github-token").write_text("github_pat_0123456789\n")
    (etc / "bots" / "zab-admin-e2e.zuliprc").write_text("[api]\nkey = abcdefghijklmnop\n")
    (out / "codex.zuliprc").write_text("[api]\nemail=c@z\nkey=k0123\n")
    (tmp_path / "var" / "audit.jsonl").write_text('{"said": "github_pat_0123456789"}\n')
    swept = checks.sweep(LocalServer(tmp_path), ["a message", "with k0123 in it"])
    codex = "ws/zab-admin-e2e/out/zab-test-a/codex.zuliprc"
    assert swept["secrets"] == ["bots/zab-admin-e2e.zuliprc", "github-token", codex]
    assert swept["hits"] == [
        ["github-token", str(tmp_path / "var" / "audit.jsonl")],
        [codex, "channel message 1"],
    ]


def test_a_malformed_secret_ends_the_sweep_named_never_quoted(tmp_path):
    """#519: configparser quotes the offending line; the old SWEEP's traceback ended with it,
    which reached pytest's output (the row kept the RemoteError's first line, the script's).
    Now the sweep ends with the file's label and the exception's type, and its assertion says
    them (reviewer on #522)."""
    malformed(tmp_path)
    with pytest.raises(AssertionError) as caught:
        checks.sweep(LocalServer(tmp_path), [])
    said = str(caught.value).splitlines()[0]
    assert said == f"the sweep: exit 1: {MALFORMED}: MissingSectionHeaderError"
    assert DECOY not in str(caught.value)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "github-token").write_bytes(b"\xff" + DECOY.encode())  # not UTF-8
    with pytest.raises(AssertionError) as caught:
        checks.sweep(LocalServer(tmp_path), [])
    assert str(caught.value).splitlines()[0].endswith(": github-token: UnicodeDecodeError")
    assert DECOY not in str(caught.value)


ROW = """
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.scenario(1)
def test_basics(scenario):
    from e2e.admin import test_scenarios_9_checks as checks
    from e2e.admin.test_harness_checks import LocalServer

    checks.sweep(LocalServer(Path("server").absolute()), [])
"""


def test_the_sweeps_failure_is_the_rows_reason(tmp_path):
    """Through the e2e conftest's `record`, in a pytest of its own: the row names the file and
    the type; neither the row nor pytest's output holds the key."""
    malformed(tmp_path / "server")
    done = inner_pytest(tmp_path, ROW)
    assert done.returncode == 1, done.stdout[-2000:] + done.stderr[-2000:]
    (row,) = report.Report.read(tmp_path / "r.json").results
    assert row.reason.endswith(f"the sweep: exit 1: {MALFORMED}: MissingSectionHeaderError")
    assert DECOY not in done.stdout + done.stderr + (tmp_path / "r.json").read_text()
