"""A13, role confinement, and A12, the secret sweep (docs/ADMIN.md §5, §2.7, §7; issue #356).

They run after the bot scenarios: A13 compares the role's view with the fake participants A4
launched, and A12 sweeps everything the run produced.

- **A13** asks EC2 with the server's own role, as `zab-admin`, in `DryRun` mode, so nothing is
  ever stopped or tagged: a call the role may make answers `DryRunOperation`, one it may not
  `UnauthorizedOperation` (AccessDenied). Each refusal has an allowed twin as its control (the
  allowed instance type, a fake participant), so a refusal can't come from a malformed call.
  #356's V4 check: a launch with an existing, detached network interface (another service's
  private address and security groups) is refused, beside its control, a launch that creates
  its own interface in the same subnet. That interface is the one resource A13 makes itself,
  from the development machine through the rig's gate (`AwsRig.probe_interface`), tagged
  `zab-role=test` and never `workshop=`, with the fleet's security group, so that only the interface
  sets the launch apart from the control; it is deleted afterwards, and by every teardown. The same
  interface takes a fake participant's Elastic IP in `AssociateAddress`, refused, beside the
  same address onto its own instance as the control (#415: `Fleet` grants the call on
  `elastic-ip` and `instance` only); with no fake participant or no address, a note. Since the
  role is scoped by the fleet-wide tag (#422), a launch with the scope but no `workshop` tag,
  and one with another `zab-scope`, are refused beside the same control, and the #263 VM can
  be tagged neither into the workshop nor into the scope, beside a retag the role may make (a
  fake participant's `participant`) as their control. `ModifyVolume` (#553), 1 GB more, is
  refused on the server's volume and on the #263 VM's, beside a fake participant's as the
  control. The rig server's volume carries no scope tag, so its refusal doesn't isolate
  `DenyServer`, which the offline IAM evaluator covers with the scope present. After A21's
  grow of the control's volume, EC2 may refuse it for EBS's 6 hours instead: then it must
  answer one of `COOLDOWN`'s codes, expected only past IAM; any other answer fails it (Codex
  round 1 on #561). The evidence that IAM lets the call through is A21's real grow of the same
  volumes, in the same run.
- **A12** runs a fixed script as root on the server: it reads every secret the server holds
  (the bots', the provisioner's and every participant bot's key, the GitHub token) and looks for
  each, as an exact substring, in the audit log, `runs/`, the console's snapshot and the
  channels' history (sent on standard input): the bots' channels, and the alerts channels,
  where the waker posts a failed wake with a participant bot's key. It prints labels and
  places, never a value. The
  run's own files on the development machine (`deploy.log`, the report) are swept for the rig's
  secrets too. `audit verify` then checks the chain against every head this run's results posted.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from e2e.admin.conftest import DONE, requires
from e2e.admin.harness.aws_rig import LIVE, tag_filters
from e2e.admin.harness.chat import heads
from e2e.admin.harness.gate import tags_of
from e2e.admin.harness.rig import ADMINS_CHANNEL, E2E, E2E2, PARTICIPANT_TYPE
from e2e.admin.harness.zulip_rig import secret_values
from e2e.harness import config as rig263

PYTHON = "/opt/zab-admin/current/.venv/bin/python"
DENIED, ALLOWED = "UnauthorizedOperation", "DryRunOperation"
#: EC2's refusals of a `ModifyVolume` for the volume's own state (one modification every 6 hours,
#: or one still under way), expected only once IAM has let the call through: what the
#: `ModifyVolume` control may answer instead of `DryRunOperation` after A21 grew its volume.
#: Their names are EC2's error codes as best known; another answer fails the control, loudly.
COOLDOWN = frozenset({"VolumeModificationRateExceeded", "IncorrectModificationState"})
#: As `zab-admin`, with the instance role: each call in DryRun mode, and its error code; notes
#: on what it could not probe.
PROBE = r"""
import json, sys
import boto3
region, vm263, server, ami, group, key, ok_type, bad_type, subnet, eni, tags, roots, *fleet = (
    sys.argv[1:])
roots = json.loads(roots)  # instance → [its root volume, that volume's size in GB]
ec2, out, notes = boto3.client("ec2", region_name=region), {}, []
def probe(name, call, **kw):
    try:
        call(DryRun=True, **kw)
        out[name] = "no error"
    except Exception as exc:
        out[name] = getattr(exc, "response", {}).get("Error", {}).get("Code") or type(exc).__name__
scope = [{"Key": k, "Value": v} for k, v in json.loads(tags).items()]  # the workshop's, in full
def launch_tagged(tagged):
    specs = [{"ResourceType": t, "Tags": tagged} for t in ("instance", "volume")]
    return dict(ImageId=ami, MinCount=1, MaxCount=1, KeyName=key, TagSpecifications=specs)
probe("StopInstances, the #263 VM", ec2.stop_instances, InstanceIds=[vm263])
by_key = {t["Key"]: t for t in scope}
for tag in (by_key["workshop"], by_key["zab-scope"]):  # never retagged into the scope
    probe(f"CreateTags {tag['Key']}={tag['Value']}, the #263 VM", ec2.create_tags,
          Resources=[vm263], Tags=[tag])
for vm in fleet[:1]:  # their control: a retag the role may make (adopt_spare's), on the fleet
    probe("CreateTags participant, participant " + vm + " (control)", ec2.create_tags,
          Resources=[vm], Tags=[{"Key": "participant", "Value": "zab-a13-dry-run"}])
probe("StopInstances, the server", ec2.stop_instances, InstanceIds=[server])
probe("TerminateInstances, the server", ec2.terminate_instances, InstanceIds=[server])
probe("ModifyInstanceAttribute, the server", ec2.modify_instance_attribute, InstanceId=server,
      DisableApiTermination={"Value": False})
launch = launch_tagged(scope)
probe("RunInstances, " + bad_type, ec2.run_instances, InstanceType=bad_type,
      SecurityGroupIds=[group], **launch)
probe("RunInstances, " + ok_type + " (control)", ec2.run_instances, InstanceType=ok_type,
      SecurityGroupIds=[group], **launch)
# #422: everything the role launches names a workshop, and carries the server's own scope
nameless = [t for t in scope if t["Key"] != "workshop"]
probe("RunInstances, the scope without a workshop tag", ec2.run_instances,
      InstanceType=ok_type, SecurityGroupIds=[group], **launch_tagged(nameless))
foreign = [t | {"Value": "zab-foreign"} if t["Key"] == "zab-scope" else t for t in scope]
probe("RunInstances, zab-scope=zab-foreign", ec2.run_instances, InstanceType=ok_type,
      SecurityGroupIds=[group], **launch_tagged(foreign))
# an interface in the request: no instance-level groups then (EC2 refuses both)
new = {"DeviceIndex": 0, "SubnetId": subnet, "Groups": [group]}
probe("RunInstances, a new interface in " + subnet + " (control)", ec2.run_instances,
      InstanceType=ok_type, NetworkInterfaces=[new], **launch)
probe("RunInstances, existing interface " + eni + ", outside the workshop", ec2.run_instances,
      InstanceType=ok_type, NetworkInterfaces=[{"DeviceIndex": 0, "NetworkInterfaceId": eni}],
      **launch)
for vm in fleet:
    probe("StopInstances, participant " + vm + " (control)", ec2.stop_instances, InstanceIds=[vm])
# #415: a fleet address onto that interface, outside the workshop (`Fleet` grants AssociateAddress
# on elastic-ip and instance only); its control, the same address onto its own instance
for vm in fleet[:1]:
    held = [{"Name": "instance-id", "Values": [vm]}]
    addresses = ec2.describe_addresses(Filters=held)["Addresses"]
    if not addresses:
        notes.append("no Elastic IP on participant " + vm + ": AssociateAddress not probed")
    for alloc in [a["AllocationId"] for a in addresses][:1]:
        probe("AssociateAddress, participant " + vm + "'s address onto interface " + eni
              + ", outside the workshop", ec2.associate_address, AllocationId=alloc,
              NetworkInterfaceId=eni, AllowReassociation=True)
        probe("AssociateAddress, participant " + vm + "'s address onto its instance (control)",
              ec2.associate_address, AllocationId=alloc, InstanceId=vm, AllowReassociation=True)
# #553: a volume grows only in the scope, never the server's (DenyServer)
def grow(name, instance):
    volume, size = roots[instance]
    probe(name, ec2.modify_volume, VolumeId=volume, Size=size + 1)
grow("ModifyVolume, the server's volume (no scope; DenyServer)", server)
grow("ModifyVolume, the #263 VM's volume", vm263)
for vm in fleet[:1]:
    grow("ModifyVolume, participant " + vm + "'s volume (control)", vm)
print(json.dumps({"codes": out, "notes": notes}))
"""
#: As `zab-admin`: the workshop's VMs as the server decides it (`starter.fleet_vm`, which the
#: runner's `provider` uses too; `Provider.vms()` of §6 is not on main yet).
FLEET = r"""
import json, sys
from zab.admin import starter
from zab.admin.config import load_admin_config
ws = load_admin_config("/etc/zab-admin/admin.toml").workshop(sys.argv[1])
boto3, config = starter.sdk()
ec2 = starter.Ec2(boto3.client("ec2", region_name=ws.aws.region))
filters = [{"Name": "tag:" + k, "Values": [v]} for k, v in ws.tags]
states = ["pending", "running", "stopping", "stopped"]
filters.append({"Name": "instance-state-name", "Values": states})
vms = [vm["InstanceId"] for vm in ec2.describe(filters) if starter.fleet_vm(vm, ws)]
print(json.dumps(sorted(vms)))
"""
#: As root: every secret the server holds, looked for in its files and in the texts on stdin.
#: A file it can't read or parse ends it with the file's label and the exception's type only:
#: configparser's message quotes the offending line, which pytest printed (#519).
SWEEP = r"""
import configparser, json, pathlib, sys
etc, var = pathlib.Path("/etc/zab-admin/secrets"), pathlib.Path("/var/lib/zab-admin")
secrets = {}
def value_of(path, label):
    try:
        if path.suffix != ".zuliprc":
            return path.read_text().strip()
        config = configparser.ConfigParser(interpolation=None)  # as ZulipClient: `key = ...`
        config.read_string(path.read_text(errors="replace"))
        return config.get("api", "key", fallback="").strip()
    except Exception as exc:
        kind = type(exc).__name__
    sys.exit(f"{label}: {kind}")
for path in sorted(etc.rglob("*")):
    if path.is_file():
        value = value_of(path, label := str(path.relative_to(etc)))
        if value and len(value) >= 12:
            secrets[label] = value
for path in sorted((var / "ws").glob("*/out/**/*.zuliprc")):
    if (value := value_of(path, label := str(path.relative_to(var)))):
        secrets[label] = value
places = [var / "audit.jsonl", var / "console" / "snapshot.json"]
places += [var / "console" / "audit-view.jsonl"]
places += sorted(p for p in (var / "runs").rglob("*") if p.is_file())
hits = []
for place in places:
    if place.is_file():
        text = place.read_bytes().decode("utf-8", "replace")
        hits += [[label, str(place)] for label, value in secrets.items() if value in text]
for n, text in enumerate(json.load(sys.stdin)):
    hits += [[label, f"channel message {n}"] for label, value in secrets.items() if value in text]
print(json.dumps({"secrets": sorted(secrets), "places": len(places), "hits": hits}))
"""


def sweep(server, texts: list[str]) -> dict:
    """SWEEP's findings. A failure is an assertion on stderr's last line, which the row's
    reason shows (`<label>: <type>` for a file it can't read): a RemoteError's first line is
    the script's, which starts with a newline (reviewer on #522)."""
    done = server.run(["sudo", "python3", "-I", "-c", SWEEP], input=json.dumps(texts),
                      timeout=600, check=False)  # fmt: skip
    last = (done.stderr.strip().splitlines() or [""])[-1][:300]
    assert done.ok, f"the sweep: exit {done.returncode}: {last}"
    return json.loads(done.stdout)


def expected(call: str, code: str) -> bool:
    """Whether A13 takes `code` as the answer to `call`: a control is allowed, anything else
    refused (`UnauthorizedOperation`); a `ModifyVolume` control may also answer one of
    `COOLDOWN`'s codes, expected only past IAM, and nothing else (Codex round 1 on
    #561)."""
    if "(control)" not in call:
        return code == DENIED
    return code == ALLOWED or (call.startswith("ModifyVolume") and code in COOLDOWN)


def root_volumes(aws, instances: list[str]) -> dict[str, list]:
    """Each instance's root volume and its size in GB, as EC2 describes them (#553's probes)."""
    found = aws.aws.ec2("describe_instances", InstanceIds=instances)["Reservations"]
    roots = {}
    for inst in (i for r in found for i in r["Instances"]):
        root = inst.get("RootDeviceName")
        for m in inst.get("BlockDeviceMappings") or []:
            if m.get("DeviceName") == root and (m.get("Ebs") or {}).get("VolumeId"):
                roots[inst["InstanceId"]] = m["Ebs"]["VolumeId"]
    volumes = aws.aws.ec2("describe_volumes", VolumeIds=sorted(set(roots.values())))["Volumes"]
    sizes = {v["VolumeId"]: v["Size"] for v in volumes}
    missing = sorted(set(instances) - set(roots))
    assert not missing, f"no root volume found for {missing}"
    return {iid: [vid, sizes[vid]] for iid, vid in roots.items()}


def _fleet_ids(aws, workshop: str) -> list[str]:
    return sorted(
        i["InstanceId"] for i in aws.participants() if tags_of(i).get("workshop") == workshop
    )


@pytest.fixture
def probe_interface(admin_rig, rigged):
    """(subnet, ID) of A13's detached interface, made from the development machine through the rig's
    gate (`zab-role=test`, never `workshop=`), and deleted when the probe is done, whatever its
    outcome; a run that dies first leaves it to the next teardown (`run.teardown`)."""
    import boto3

    from e2e.admin.harness.aws_rig import AwsRig
    from e2e.admin.harness.gate import Aws, Gate

    ec2 = boto3.client("ec2", region_name=admin_rig.region)
    rig = AwsRig(Aws(Gate(dry_run=False), ec2, boto3.client("iam")), admin_rig)
    try:
        yield rig.probe_interface(rigged.groups[E2E.name], PARTICIPANT_TYPE)
    finally:
        rig.drop_probe_interfaces()


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A13")
def test_role_confinement(scenario, admin_rig, rigged, server, aws, probe_interface):
    filters = tag_filters(rig263.load().instance_tags)
    filters.append({"Name": "instance-state-name", "Values": list(LIVE)})
    found = aws.aws.ec2("describe_instances", Filters=filters)["Reservations"]
    vms = [i["InstanceId"] for r in found for i in r["Instances"]]
    assert len(vms) == 1, f"expected the #263 test VM, found {vms}"
    fleet = _fleet_ids(aws, E2E.name)
    argv = [PYTHON, "-I", "-c", PROBE, admin_rig.region, vms[0], rigged.server, rigged.ami]
    argv += [rigged.groups[E2E.name], E2E.key_name, PARTICIPANT_TYPE, "t3.small"]
    argv += [*probe_interface, json.dumps(E2E.tags)]  # its subnet and ID; the scope
    argv += [json.dumps(root_volumes(aws, [vms[0], rigged.server, *fleet[:1]])), *fleet]
    done = server.as_admin(*argv)
    assert done.ok, f"the probe failed: {done.stderr.strip()[-300:]}"
    said = json.loads(done.stdout)
    for call, code in said["codes"].items():
        scenario.notes.append(f"{call}: {code}")
        assert expected(call, code), f"{call}: {code}"
    scenario.notes += said["notes"]
    if not fleet:
        scenario.notes.append(
            "no fake participant (A4 did not run): no StopInstances control, and AssociateAddress "
            "not probed"
        )


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A13")
@requires("participants")
def test_role_sees_only_the_fleet(scenario, admin_rig, server, aws):
    if "torn_down" in DONE:  # A9's step 6 ended them: nothing left to compare
        pytest.skip("A9 terminated the fake participants first: this part runs without A9")
    expected = _fleet_ids(aws, E2E.name)
    assert expected == sorted(DONE["participants"]), "the fake participants changed since A4"
    done = server.as_admin(PYTHON, "-I", "-c", FLEET, E2E.name)
    assert done.ok, done.stderr.strip()[-300:]
    assert json.loads(done.stdout) == expected, f"the server's fleet: {done.stdout.strip()}"
    script = "/opt/zab-admin/current/workshop/aws/teardown.py"
    tear = server.as_admin(PYTHON, "-E", "-s", script, "--tag", f"workshop={E2E.name}",
                           "--region", admin_rig.region, "--dry-run")  # fmt: skip
    assert tear.ok, f"teardown.py --dry-run: {(tear.stderr or tear.stdout).strip()[-300:]}"
    listed = sorted(set(re.findall(r"\bi-[0-9a-f]{8,17}\b", tear.stdout)))
    assert listed == expected, f"teardown.py --dry-run lists {listed}, not {expected}"
    scenario.notes.append(f"Provider (starter.fleet_vm) and teardown.py list {len(expected)} VMs")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A13")
def test_imds_only_for_the_key_holders(scenario, server):
    url = "http://169.254.169.254/latest/api/token"
    curl = ["curl", "-sS", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PUT"]
    curl += ["-H", "X-aws-ec2-metadata-token-ttl-seconds: 60", url]
    for user, reach in (("caddy", False), ("zab-console", False), ("zab-admin", True)):
        if not server.run(["id", "-u", user], check=False).ok:
            scenario.notes.append(f"{user}: not installed on this release")
            continue
        got = server.run(["sudo", "-u", user, *curl], check=False, timeout=30)
        reached = got.ok and got.stdout.strip() == "200"
        assert reached == reach, f"IMDS as {user}: exit {got.returncode}, {got.stdout.strip()}"
        scenario.notes.append(f"IMDS as {user}: {'reached' if reached else 'refused'}")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A12")
async def test_no_secret_anywhere(scenario, admin_rig, rigged, server, tester):
    ids = rigged.ids()
    texts, chain = [], []
    for name in (E2E.channel, E2E2.channel, ADMINS_CHANNEL, E2E.alerts, E2E2.alerts):
        narrow = [{"operator": "channel", "operand": ids.channels[name]}]
        for message in await tester.after(narrow, 0):
            texts.append(message["content"])
            if message["timestamp"] >= rigged.started and message["sender_id"] in _bots(ids):
                chain += heads(message["content"])
    swept = sweep(server, texts)
    scenario.notes.append(
        f"{len(swept['secrets'])} secrets, {swept['places']} files and {len(texts)} channel "
        "messages swept"
    )
    assert swept["secrets"], "the sweep found no secret to look for"
    assert not swept["hits"], f"secrets found: {swept['hits']}"
    verify = [PYTHON, "-I", "-m", "zab.admin", "audit", "verify", "/var/lib/zab-admin/audit.jsonl"]
    for seq, hexa in sorted(set(chain)):
        verify += ["--head", f"{seq}:{hexa}"]
    checked = server.as_admin(*verify)
    assert checked.ok, f"audit verify: {(checked.stdout + checked.stderr).strip()[-300:]}"
    scenario.notes.append(f"audit verify: {checked.stdout.strip()} ({len(set(chain))} heads)")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A12")
def test_no_secret_in_the_run_files(scenario, admin_rig):
    """The run's own files on the development machine (deploy.log, the report so far, the state)."""
    run_dir = Path(os.environ["ZAB_ADMIN_E2E_STATE"]).parent
    values = secret_values(admin_rig)
    assert values, "no rig secret to look for"
    found = []
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        text = path.read_bytes().decode("utf-8", "replace")
        found += [f"{label} in {path.name}" for label, value in values.items() if value in text]
    assert not found, f"secrets in the run's files: {found}"
    scenario.notes.append(f"{len(values)} rig secrets: none in {run_dir.name}")


def _bots(ids) -> set[int]:
    return {uid for uid, _ in ids.bots.values()}
