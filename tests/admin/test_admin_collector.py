"""The collector (docs/ADMIN.md §4.1, §11.2; `zab.admin.collector`): a pass over a fake EC2, the
waker's status file, the audit log and the bots' `!status` reads, written for the console."""

import json
import os
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zab.admin import collector, costs
from zab.admin.collector import Collector, Paths, cost_line, note_status, secret_files
from zab.admin.config import parse_admin_config

from .botkit import OLGA, make_site

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC
SCOPE = (("workshop", "harvard-2026"), ("zab-scope", "mathagents"))
#: A participant bot's key, in its out/ (the registry's), which a VM's text and a record quote.
LEAKED = "Lk" + "7" * 30
#: The snapshot of `test_the_snapshot`, as written and reviewed.
GOLDEN = Path(__file__).parent / "golden" / "collector-snapshot.json"


def at(seconds_ago: float) -> datetime:
    return datetime.fromtimestamp(NOW - seconds_ago, UTC)


def vm(iid, participant, ip, *, state="running", launched=3600.0, tags=SCOPE, region="us-west-2",
       created=3 * 86400.0, kind="m7a.2xlarge"):  # fmt: skip
    return {
        "InstanceId": iid,
        "State": {"Name": state},
        "InstanceType": kind,
        "LaunchTime": at(launched),
        "Placement": {"AvailabilityZone": f"{region}a"},
        "PublicIpAddress": ip if state == "running" else None,
        "NetworkInterfaces": [{"Association": {"PublicIp": ip}}],
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {"AttachTime": at(created), "VolumeId": f"vol-{iid}"},
            }
        ],
        "Tags": [{"Key": k, "Value": v} for k, v in (*tags, ("participant", participant))],
    }


class FakeAws:
    def __init__(self) -> None:
        self.instances = {
            "us-west-2": [
                vm("i-a", "alice", "198.51.100.1"),
                vm("i-b", "Bob", "198.51.100.2", state="stopped", launched=2 * 86400),
                vm("i-s", "zab-spare-1", "198.51.100.9", launched=600),
                vm("i-z", "zed", "198.51.100.7"),  # a row no roster has (any more)
                vm("i-x", "mallory", "198.51.100.8", tags=SCOPE[:1]),  # not in the scope
            ],
            "us-east-1": [vm("i-e", "erin", "203.0.113.5", region="us-east-1",
                             tags=(("workshop", "zab-test"), ("zab-scope", "mathagents")))],
        }  # fmt: skip
        self.protect = {"i-a"}
        self.sizes: dict[str, int] = {}  # none: as without DescribeVolumes, 200 GB each
        self.failing: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def describe(self, region, filters):
        self.calls.append(("describe", region))
        if region in self.failing:
            raise RuntimeError("no route")
        return self.instances[region]

    def protected(self, region, instance):
        self.calls.append(("protected", instance))
        return instance in self.protect

    def volumes(self, region, ids):
        self.calls.append(("volumes", region))
        if "volumes" in self.failing:
            raise RuntimeError("UnauthorizedOperation")
        return {v: self.sizes[v] for v in ids if v in self.sizes}


class FakeRun:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.answer: dict = {"schema": 1, "vms": []}

    def __call__(self, argv, env, cwd):
        self.argvs.append(argv)
        assert env["HOME"].endswith("state") and env["TMPDIR"] == cwd
        return json.dumps(self.answer)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


#: status.py's `disk` (#552) of the m7a.2xlarge's `df -Pk /` line (test_admin_status's LOAD).
DISK = {"size_bytes": 103_865_303_040, "used_bytes": 42_585_522_176,
        "available_bytes": 61_263_003_648}  # fmt: skip


def status(slug, *, idle=True, bridge="active", error=None):
    return {
        "slug": slug, "host": f"ws-{slug}", "bridge": bridge, "sentinel": "ok",  # #588
        "zab_version": "0.2.6", "codex_login": "ok", "claude_login": "FAIL", "git_push": "ok",
        "math_tools": "ok",
        "active_sessions": 1, "running_turns": [], "last_activity": "2027-01-15T07:30:00Z",
        "load": {"load1": 0.5, "cpus": 8}, "memory": {"used_bytes": 2**33, "total_bytes": 2**35},
        "disk_percent": 42, "disk": dict(DISK), "strained": [],
        "idle_stop": {"enabled": idle, "active": idle, "minutes": 60 if idle else None,
                      "last_verdict": "busy: 1 session"},
        "doctor_failures": ["claude: not signed in"], "last_error": error, "extra": "dropped",
    }  # fmt: skip


@pytest.fixture
def site(tmp_path):
    site = make_site(tmp_path)
    out = tmp_path / "ws" / "harvard-2026" / "out"
    (out / "alice").mkdir()
    (out / "alice" / "codex.zuliprc").write_text(f"[api]\nkey={LEAKED}\nemail=a@x\nsite=x\n")
    spares = {"harvard-2026/us-west-2/zab-spare-1": {"ip": "198.51.100.9", "name": "zab-spare-1"}}
    (out / "spares.json").write_text(json.dumps(spares))
    waker = {
        "written_at": NOW - 20,
        "workshops": [{"name": "harvard-2026", "checked_at": NOW - 25, "bots": [
            {"bot": "Claude · alice", "instance": "i-a", "state": "running",
             "last_wake_attempt": NOW - 7200},
            {"bot": "Codex · alice", "instance": "i-a", "state": "running",
             "last_wake_attempt": None},
        ]}],
    }  # fmt: skip
    (tmp_path / "waker.json").write_text(json.dumps(waker))
    requester = {"id": OLGA, "name": "Olga Organizer"}
    records = [
        {"seq": 1, "ts": "2027-01-15T07:58:00Z", "event": "request", "workshop": "harvard-2026",
         "plan": "P-7", "verb": "stop", "requester": requester, "args": ["bob"]},
        {"seq": 2, "ts": "2027-01-15T07:58:05Z", "event": "plan", "workshop": "harvard-2026",
         "plan": "P-7", "verb": "stop", "targets": ["bob"], "argv": [["power.py"]],
         "approval": {"class": "change", "approvers": 1, "phrase": ""}},
        {"seq": 3, "ts": "2027-01-15T07:59:00Z", "event": "refused", "workshop": "harvard-2026",
         "plan": "P-7", "sender": {"id": 107, "name": "Pat"}, "code": "not an approver",
         "reason": f"quoting {LEAKED}"},
    ]  # fmt: skip
    state = Path(site.paths.state)
    (state / "audit.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (state / "console").mkdir()
    return site


def make(site, **kwargs) -> Collector:
    config = parse_admin_config(site.config_path.read_text(), roots=site.roots)
    paths = Paths(state=site.paths.state, secrets=site.paths.secrets,
                  waker=str(site.tmp / "waker.json"), layout=site.paths.layout)  # fmt: skip
    options = {"aws": FakeAws(), "run": FakeRun(), "clock": Clock(), "background": False}
    return Collector(config, paths=paths, **(options | kwargs))


def written(site, name="snapshot.json"):
    return (Path(site.paths.state) / "console" / name).read_text()


def test_the_snapshot_says_since_when_the_waker_is_down(site):
    """#717: harvard-2026 opted in, its waker's `ready_at` an hour old (`zab.admin.wakerdown`):
    the console's snapshot says since when; zab-test, not opted in, nothing."""
    config = Path(site.paths.layout.waker) / "config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[[workshop]]\nname = "harvard-2026"\n[[workshop.bots]]\n'
                      'name = "codex-alice"\ninstance_id = "i-a"\n')  # fmt: skip
    waker = json.loads((site.tmp / "waker.json").read_text())
    waker["workshops"][0]["ready_at"] = NOW - 3600
    (site.tmp / "waker.json").write_text(json.dumps(waker))
    workshops = make(site).tick()["workshops"]
    assert workshops["harvard-2026"]["waker_down"] == "2027-01-15T07:00:00Z"
    assert workshops["zab-test"]["waker_down"] is None
    waker["workshops"][0]["ready_at"] = NOW - 30
    (site.tmp / "waker.json").write_text(json.dumps(waker))
    assert make(site).tick()["workshops"]["harvard-2026"]["waker_down"] is None


def test_the_snapshot(site):
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [
        status("alice", error=f"token {LEAKED}"), status("carol", bridge="unreachable")]},
        NOW - 300)  # fmt: skip
    snapshot = make(site).tick()
    ws = snapshot["workshops"]["harvard-2026"]
    assert snapshot["written_at"] == "2027-01-15T08:00:00Z"
    assert snapshot["waker_at"] == "2027-01-15T07:59:40Z"
    assert ws["region"] == "us-west-2" and ws["channels"] == [42]
    assert ws["site"] == "https://zulip.example.com"
    assert ws["ec2"] == {"at": "2027-01-15T08:00:00Z", "error": None}
    rows = {row["slug"]: row for row in ws["vms"]}
    assert list(rows) == ["alice", "bob", "zab-spare-1", "zed", "carol"]  # mallory's not here
    alice = rows["alice"]
    assert {k: alice[k] for k in ("kind", "instance", "state", "type", "protected")} == {
        "kind": "participant", "instance": "i-a", "state": "running", "type": "m7a.2xlarge",
        "protected": True,
    }  # fmt: skip
    assert alice["launched_at"] == "2027-01-15T07:00:00Z"
    assert alice["waker"] == {
        "opted_in": True,
        "state": "running",
        "last_wake": "2027-01-15T06:00:00Z",
        "at": "2027-01-15T07:59:35Z",
    }
    assert alice["status"]["source"] == "!status" and alice["status"]["at"] == NOW - 300
    assert alice["status"]["idle_stop"]["minutes"] == 60 and "extra" not in alice["status"]
    assert alice["status"]["doctor_failures"] == ["claude: not signed in"]
    assert alice["cost"]["hours_today"] == 1.0  # running since 07:00
    assert rows["bob"]["kind"] == "participant" and rows["bob"]["state"] == "stopped"
    assert rows["bob"]["waker"] == {"opted_in": False} and rows["bob"]["protected"] is False
    assert rows["zab-spare-1"]["kind"] == "spare" and rows["zab-spare-1"]["status"] is None
    assert rows["zed"]["kind"] == "unlisted"
    carol = {k: v for k, v in status("carol", bridge="unreachable").items() if k != "host"}
    del carol["extra"]
    assert rows["carol"] == {"slug": "carol", "kind": "participant", "state": "no VM",
                             "status": carol | {"at": NOW - 300, "source": "!status"}}  # fmt: skip
    # the spares apart (#715): alice and zed of alice, bob and zed; zab-spare-1 on its own
    assert {k: ws["cost"][k] for k in ("running", "vms", "spares", "spares_running")} == {
        "running": 2, "vms": 3, "spares": 1, "spares_running": 1,
    }  # fmt: skip
    assert ws["plans"] == [{
        "plan": "P-7", "workshop": "harvard-2026", "verb": "stop", "state": "pending",
        "since": "2027-01-15T07:58:05Z", "requester": "Olga Organizer", "targets": ["bob"],
        "approvals": 0, "needed": 1, "expires_at": "2027-01-15T08:08:05Z",
    }]  # fmt: skip
    test = snapshot["workshops"]["zab-test"]
    assert [row["slug"] for row in test["vms"]] == ["erin"] and test["plans"] == []
    # what the console reads is the scrubbed snapshot (§2.7)
    on_disk = json.loads(written(site))
    assert LEAKED not in written(site) and LEAKED in json.dumps(snapshot)
    assert on_disk["workshops"]["harvard-2026"]["vms"][0]["status"]["last_error"] == (
        "token [redacted secret]"
    )
    # the whole of it, as reviewed (each VM's cost by hand: alice, created 72 hours ago and
    # running for one, 72 * $0.02692 + $0.46368 = $2.40), against the golden file
    assert on_disk == json.loads(GOLDEN.read_text())


def test_the_audit_view_has_no_argument_list_and_no_secret(site):
    make(site).tick()
    lines = [json.loads(line) for line in written(site, "audit-view.jsonl").splitlines()]
    assert [r["seq"] for r in lines] == [1, 2, 3]
    text = written(site, "audit-view.jsonl")
    assert "power.py" not in text and '"bob"]' in text and LEAKED not in text
    assert lines[2]["reason"] == "quoting [redacted secret]"


def test_the_collector_learns_a_key_in_any_zuliprc_form(site):
    """#526: the collector's registry reads `Key: …` as the bot's does."""
    rc = site.tmp / "ws" / "harvard-2026" / "out" / "alice" / "codex.zuliprc"
    rc.write_text("[api]\nemail : a@x\nsite: x\n")
    make(site).tick()
    assert f"quoting {LEAKED}" in written(site, "audit-view.jsonl")  # only the registry hides it
    rc.write_text(f"[api]\nemail : a@x\nKey:\t{LEAKED}\nsite: x\n")
    make(site).tick()
    text = written(site, "audit-view.jsonl")
    assert LEAKED not in text and "quoting [redacted secret]" in text


def test_the_files_the_console_reads_are_0640_under_umask_0077(site):
    old = os.umask(0o077)
    try:
        note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW)
        make(site).tick()
    finally:
        os.umask(old)
    state = Path(site.paths.state)
    for path, mode in (
        (state / "console" / "snapshot.json", 0o640),
        (state / "console" / "audit-view.jsonl", 0o640),
        (state / "collector" / "state.json", 0o600),
        (state / "collector" / "reads" / "harvard-2026.json", 0o600),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == mode, path
    assert stat.S_IMODE((state / "collector").stat().st_mode) == 0o700
    assert not [p for p in (state / "console").iterdir() if p.name.startswith(".")]


def test_idle_stop_vms_are_never_swept(site):
    """A VM is swept only when its last read shows idle stop off (module doc); never one with no
    read, or whose read shows it on."""
    run = FakeRun()
    run.answer = {"schema": 1, "vms": [status("alice", idle=False) | {"zab_version": "0.2.7"}]}
    clock = Clock()
    c = make(site, run=run, clock=clock)
    c.tick()
    assert run.argvs == []  # no status known: idle stop may be on, so no sweep
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW)
    clock.now += 3600
    c.tick()
    assert run.argvs == []  # idle stop on
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [
        status("alice", idle=False), status("bob", idle=False)]}, clock.now)  # fmt: skip
    c.tick()
    assert run.argvs == []  # read just now
    clock.now += collector.SWEEP_EVERY
    snapshot = c.tick()
    assert len(run.argvs) == 1  # alice only: bob's VM is stopped
    argv = run.argvs[0]
    assert argv[argv.index("--only") + 1] == "alice" and argv.count("--only") == 1
    assert "--json" in argv and argv[argv.index("--tag") + 1] == "workshop=harvard-2026"
    assert argv[0].endswith("/.venv/bin/python") and argv[1:5] == ["-E", "-s", "-B", "-u"]
    alice = c.status["harvard-2026/alice"]
    assert (alice["source"], alice["zab_version"], alice["at"]) == ("sweep", "0.2.7", clock.now)
    assert snapshot["workshops"]["harvard-2026"]["vms"][0]["status"]["zab_version"] == "0.2.6"
    # swept now: not again for SWEEP_EVERY
    clock.now += 60
    c.tick()
    assert len(run.argvs) == 1


def test_a_read_that_finds_the_vm_down_keeps_its_values(site):
    run = FakeRun()
    run.answer = {"schema": 1, "vms": [status("alice", idle=False, bridge="unreachable")]}
    clock = Clock()
    c = make(site, run=run, clock=clock)
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [
        status("alice", idle=False)]}, NOW - collector.SWEEP_EVERY)  # fmt: skip
    c.tick()
    alice = c.status["harvard-2026/alice"]
    assert alice["bridge"] == "active" and alice["tried"] == {"at": NOW, "bridge": "unreachable"}
    clock.now += 60
    c.tick()
    assert len(run.argvs) == 1  # tried a minute ago: not every minute while it can't be reached


def test_a_failed_ec2_read_keeps_the_last(site):
    aws, clock = FakeAws(), Clock()
    c = make(site, aws=aws, clock=clock)
    c.tick()
    aws.failing.add("us-west-2")
    clock.now += 60
    ws = c.tick()["workshops"]["harvard-2026"]
    assert ws["ec2"] == {"at": "2027-01-15T08:00:00Z", "error": "RuntimeError"}
    assert [row["slug"] for row in ws["vms"]][:2] == ["alice", "bob"]


def test_protection_is_read_every_ten_minutes(site):
    aws, clock = FakeAws(), Clock()
    c = make(site, aws=aws, clock=clock)
    for _ in range(3):
        c.tick()
        clock.now += 60
    reads = [call for call in aws.calls if call == ("protected", "i-a")]
    assert len(reads) == 1
    clock.now += collector.PROTECT_EVERY
    c.tick()
    assert len([call for call in aws.calls if call == ("protected", "i-a")]) == 2


def volume_reads(aws: FakeAws) -> int:
    return len([call for call in aws.calls if call[0] == "volumes"])


def test_volume_sizes_are_read_every_ten_minutes_and_priced(site):
    """#553: each VM's volumes' size, for its cost; read again every `VOLUME_EVERY`, and at once
    for a volume not read yet."""
    aws, clock = FakeAws(), Clock()
    aws.sizes = {"vol-i-a": 300, "vol-i-b": 200, "vol-i-s": 200, "vol-i-z": 200}
    c = make(site, aws=aws, clock=clock)
    c.tick()
    assert c.history.seen["i-a"].volumes == [[NOW - 3 * 86400, 300]]
    assert c.history.seen["i-b"].volume_gb == 200
    clock.now += 60
    c.tick()
    assert volume_reads(aws) == 2  # harvard-2026 and zab-test, once each
    aws.sizes["vol-i-a"] = 500  # grown: seen at the next read
    clock.now += collector.VOLUME_EVERY
    c.tick()
    assert volume_reads(aws) == 4 and c.history.seen["i-a"].volume_gb == 500
    aws.instances["us-west-2"].append(vm("i-n", "nora", "198.51.100.3"))
    aws.sizes["vol-i-n"] = 250
    clock.now += 60
    c.tick()  # a new volume: read at once
    assert volume_reads(aws) == 5 and c.history.seen["i-n"].volume_gb == 250


def test_the_volume_read_filters_by_id_in_chunks_and_reads_every_page():
    asked: list[list[str]] = []

    class Pages:
        def paginate(self, Filters):  # boto3's name
            [wanted] = Filters
            assert wanted["Name"] == "volume-id"
            asked.append(wanted["Values"])
            yield {"Volumes": []}  # a page of none, as a filter may give
            yield {"Volumes": [{"VolumeId": v, "Size": 200} for v in wanted["Values"][:2]]}
            yield {"Volumes": [{"VolumeId": "vol-odd", "Size": "big"}]}  # not a size: left out

    aws = collector.Aws()
    aws.clients["us-west-2"] = type("Client", (), {"get_paginator": lambda self, name: Pages()})()
    ids = [f"vol-{n:03d}" for n in range(250)]
    found = aws.volumes("us-west-2", ids)
    assert [len(chunk) for chunk in asked] == [200, 50]
    assert found == {"vol-000": 200, "vol-001": 200, "vol-200": 200, "vol-201": 200}


def test_volume_sizes_not_read_are_200_gb(site, caplog):
    """Without `DescribeVolumes` (ADMIN §8.4, v0.2.9), or for a volume EC2 doesn't show, the cost
    takes `costs.VOLUME_GB`, as before #553."""
    aws, clock = FakeAws(), Clock()
    aws.failing.add("volumes")
    c = make(site, aws=aws, clock=clock)
    c.tick()
    assert "volume sizes not read" in caplog.text
    assert all(entry.volumes == [] for entry in c.history.seen.values())
    aws.failing.clear()
    aws.sizes = {"vol-i-a": 300}  # i-b's not shown
    clock.now += collector.VOLUME_EVERY
    c.tick()
    assert c.history.seen["i-a"].volume_gb == 300 and c.history.seen["i-b"].volumes == []


def test_its_state_survives_a_restart(site):
    clock = Clock()
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW)
    make(site, clock=clock).tick()
    clock.now += 600
    again = make(site, clock=clock)
    assert again.status["harvard-2026/alice"]["at"] == NOW
    assert again.history.seen["i-a"].runs == [[NOW - 3600, NOW, "m7a.2xlarge"]]
    again.status["retired-2025/x"] = {"slug": "x", "at": NOW}  # no such workshop any more
    again.status["harvard-2026/old"] = {"slug": "old", "at": NOW - 41 * 86400}
    again.tick()
    assert again.history.seen["i-a"].runs == [[NOW - 3600, NOW + 600, "m7a.2xlarge"]]
    saved = json.loads((Path(site.paths.state) / "collector" / "state.json").read_text())
    assert list(saved["status"]) == ["harvard-2026/alice"]


def test_reads_kept_from_before_552_show_the_disk_as_a_percentage(site):
    """#552: the collector's store holds reads made before status.py gave `disk`, in its own
    state (alice) and in a bot's `!status` feed (bob, stopped). The console still shows each
    one's disk, as its percentage alone; a new read shows its sizes."""
    from zab.admin import pages

    def old(slug):
        return {k: v for k, v in status(slug).items() if k not in ("disk", "extra", "host")}

    own = Path(site.paths.state) / "collector"
    (own / "reads").mkdir(mode=0o700, parents=True)
    kept = {"harvard-2026/alice": old("alice") | {"at": NOW - 600, "source": "sweep"}}
    (own / "state.json").write_text(json.dumps({"status": kept}))
    (own / "reads" / "harvard-2026.json").write_text(
        json.dumps({"vms": {"bob": old("bob") | {"at": NOW - 300}}})
    )
    clock = Clock()
    c = make(site, clock=clock)
    rows = {row["slug"]: row for row in c.tick()["workshops"]["harvard-2026"]["vms"]}
    assert "disk" not in rows["alice"]["status"] and rows["bob"]["status"]["disk"] is None
    for slug in ("alice", "bob"):
        disk = pages.resources(rows[slug])[2]
        assert disk.endswith('<span class="num">42%</span>') and "w4 green" in disk, slug
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW)
    clock.now += 60
    rows = {row["slug"]: row for row in c.tick()["workshops"]["harvard-2026"]["vms"]}
    assert pages.resources(rows["alice"])[2].endswith('"num">40 of 97 GB (42%)</span>')


def test_the_cost_line_of_status(site):
    make(site).tick()
    line = cost_line(site.paths.state, "harvard-2026", NOW + 60)
    assert line is not None
    assert line.startswith("cost (estimate): $")
    assert line.endswith(" projected · 2 of 3 running · spares: 1 of 1 running")  # #715
    assert cost_line(site.paths.state, "harvard-2026", NOW + collector.FRESH + 1) is None
    assert cost_line(site.paths.state, "nowhere", NOW) is None
    assert cost_line(str(site.tmp / "empty"), "harvard-2026", NOW) is None


async def test_its_registry_is_the_bots(site):
    server = site.server()
    config = parse_admin_config(site.config_path.read_text(), roots=site.roots)
    paths = Paths(state=site.paths.state, secrets=site.paths.secrets, layout=site.paths.layout)
    assert secret_files(config, paths) == server._secret_files()
    assert any(p.name == "codex.zuliprc" for p in secret_files(config, paths)[0])


def test_main_refuses_a_bad_config(tmp_path, capsys):
    bad = tmp_path / "admin.toml"
    bad.write_text("provider = 'gcp'\n")
    assert collector.main(["--config", str(bad), "--once"]) == 1
    assert "zab-admin-collector:" in capsys.readouterr().out


def test_two_status_reads_between_passes_both_count(site):
    """Codex on #494: `!status alice`, then `!status bob`, before the collector's next pass."""
    clock = Clock()
    c = make(site, clock=clock)
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW)
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("bob")]}, NOW + 5)
    c.tick()
    assert c.status["harvard-2026/alice"]["at"] == NOW
    assert c.status["harvard-2026/bob"]["at"] == NOW + 5
    # a read older than KEEP leaves the feed at the next write
    later = NOW + collector.KEEP + 10
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("bob")]}, later)
    feed = json.loads((Path(site.paths.state) / "collector/reads/harvard-2026.json").read_text())
    assert list(feed["vms"]) == ["bob"] and feed["vms"]["bob"]["at"] == later


def test_a_good_read_then_a_down_one_between_passes_keeps_the_good(site):
    """Codex on #494 (6260727975): a read of alice, then one that finds her VM unreachable, both
    before the collector's next pass: the first one's values count, and the try is noted."""
    clock = Clock()
    c = make(site, clock=clock)
    note_status(
        site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW - 600
    )
    c.tick()
    assert c.status["harvard-2026/alice"]["zab_version"] == "0.2.6"
    newer = status("alice") | {"zab_version": "0.2.7"}
    down = status("alice", bridge="unreachable") | {"zab_version": None}
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [newer]}, NOW + 10)
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [down]}, NOW + 20)
    clock.now += 60
    c.tick()
    alice = c.status["harvard-2026/alice"]
    assert (alice["zab_version"], alice["at"], alice["source"]) == ("0.2.7", NOW + 10, "!status")
    assert alice["tried"] == {"at": NOW + 20, "bridge": "unreachable"}
    # a good read after it replaces both
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [status("alice")]}, NOW + 70)
    feed = json.loads((Path(site.paths.state) / "collector/reads/harvard-2026.json").read_text())
    assert feed["vms"]["alice"]["at"] == NOW + 70 and "tried" not in feed["vms"]["alice"]


def test_a_good_read_that_arrives_after_a_later_failed_one_counts(site):
    """Codex on #494 (6260900537): values of 0.2.6 read at 100, a sweep that found the VM
    unreachable at 120, then a `!status` read of 0.2.7 made at 110: the values are 0.2.7's, the
    try at 120 still noted; an older good read, or an older try, changes nothing."""
    c = make(site)
    newer = status("alice") | {"zab_version": "0.2.7"}
    c._merge("harvard-2026", [status("alice")], 100.0, "sweep")
    c._merge("harvard-2026", [status("alice", bridge="unreachable")], 120.0, "sweep")
    c._merge("harvard-2026", [newer], 110.0, "!status")
    alice = c.status["harvard-2026/alice"]
    assert (alice["zab_version"], alice["at"], alice["source"]) == ("0.2.7", 110.0, "!status")
    assert alice["tried"] == {"at": 120.0, "bridge": "unreachable"}
    c._merge("harvard-2026", [status("alice")], 105.0, "sweep")  # older than the values
    c._merge("harvard-2026", [status("alice", bridge="stopped")], 115.0, "sweep")  # older try
    assert c.status["harvard-2026/alice"] == alice
    # a VM first found down, then a good read made before that: its values, and the try
    c._merge("harvard-2026", [status("bob", bridge="unreachable")], 120.0, "sweep")
    c._merge("harvard-2026", [status("bob")], 110.0, "!status")
    bob = c.status["harvard-2026/bob"]
    assert (bob["bridge"], bob["at"]) == ("active", 110.0)
    assert bob["tried"] == {"at": 120.0, "bridge": "unreachable"}


def test_status_reads_that_end_out_of_order_feed_the_newest_values(site):
    """Two `!status` reads: the later one finds the VM down and ends first (the feed's own
    order, Codex on #494)."""
    state = site.paths.state
    note_status(state, "harvard-2026", {"vms": [status("alice", bridge="unreachable")]}, NOW + 5)
    note_status(state, "harvard-2026", {"vms": [status("alice")]}, NOW)
    feed = json.loads((Path(state) / "collector/reads/harvard-2026.json").read_text())
    alice = feed["vms"]["alice"]
    assert (alice["bridge"], alice["at"]) == ("active", NOW)
    assert alice["tried"] == {"at": NOW + 5, "bridge": "unreachable"}
    c = make(site)
    c.tick()
    kept = c.status["harvard-2026/alice"]
    assert (kept["bridge"], kept["at"], kept["tried"]["at"]) == ("active", NOW, NOW + 5)


def test_the_cost_line_names_the_unpriced_types(site):
    """Codex on #494: the dollars leave out what ran as a type without a price, and say so."""
    aws = FakeAws()
    aws.instances["us-west-2"][0]["InstanceType"] = "x9.huge"
    make(site, aws=aws).tick()
    line = cost_line(site.paths.state, "harvard-2026", NOW)
    assert (
        line is not None
        and ", leaving out every VM that ran as, or is now, x9.huge (no price) · " in line
    )


@pytest.mark.parametrize(
    ("cost", "said"),
    [
        ({"spares": 3, "spares_running": 1}, " · spares: 1 of 3 running"),
        ({"spares": 3, "spares_running": 0}, " · spares: 0 of 3 running"),
        ({"spares": 0, "spares_running": 0}, ""),  # no spares: nothing said
        ({}, ""),  # a snapshot from before #715
        ({"spares": "3", "spares_running": 1}, ""),
        ({"spares": 3, "spares_running": 4}, ""),
        ({"spares": True, "spares_running": 0}, ""),
        (None, ""),
    ],
)
def test_the_spares_are_said_apart_from_the_vms(cost, said):
    """#715: Harvard's `8 of 47 running` was 5 VMs of the table and 3 spares; the collector
    counts them apart, and `!status`'s line and the console say them alike."""
    assert costs.spares_said(cost) == said


def test_the_status_line_and_the_console_count_the_spares_alike(site):
    from zab.admin import pages

    make(site).tick()
    line = cost_line(site.paths.state, "harvard-2026", NOW + 60)
    ws = json.loads(written(site))["workshops"]["harvard-2026"]
    fleet = line.split(" projected · ", 1)[1]
    assert fleet == "2 of 3 running · spares: 1 of 1 running"
    assert f"(estimates) · {fleet} · " in pages.workshop("harvard-2026", ws, [], NOW, user="O")
    assert "<td>2 of 3 · spares 1 of 1</td>" in pages.index(
        {"workshops": {"harvard-2026": ws}, "at": NOW}, ["harvard-2026"], NOW, user="O"
    )


def test_a_workshop_in_a_region_without_prices_is_named_not_priced(site, monkeypatch):
    """#499: harvard-2026's region (us-west-2) taken out of the price table: the snapshot has
    no dollars, per VM or in total, and names the region, and `!status`'s line says so."""
    monkeypatch.setattr(costs, "REGIONS", ("us-east-1", "us-east-2"))
    make(site).tick()
    ws = json.loads(written(site))["workshops"]["harvard-2026"]
    assert (ws["cost"]["month"], ws["cost"]["projected"]) == (None, None)
    assert ws["cost"]["unpriced_region"] == "us-west-2"
    assert {vm["cost"]["month"] for vm in ws["vms"] if vm.get("cost")} == {None}
    line = cost_line(site.paths.state, "harvard-2026", NOW)
    assert line == (
        "cost (estimate): no prices for us-west-2 · 2 of 3 running · spares: 1 of 1 running"
    ), line
    assert "no prices" not in (cost_line(site.paths.state, "zab-test", NOW) or "")


def test_it_exits_when_admin_toml_changes(site):
    """Codex on #494: a `!new-workshop` rewrites admin.toml; the collector exits, for systemd
    to start it again on the new file."""
    c = make(site)
    passes = []

    def sleep(seconds):
        passes.append(seconds)
        if len(passes) == 2:
            site.config_path.write_text(site.config_path.read_text() + "\n# a new workshop\n")

    assert collector.serve(c, site.config_path, sleep=sleep) == 0
    assert len(passes) == 2  # two passes, then the change was seen before a third
    assert collector.serve(c, site.config_path, once=True, sleep=sleep) == 0
    assert len(passes) == 2


def test_the_cost_line_names_an_old_ec2_read(site):
    """Codex on #494: the snapshot is written every pass, EC2 read or not; a read older than
    FRESH is named, and a workshop never read has no line."""
    aws, clock = FakeAws(), Clock()
    c = make(site, aws=aws, clock=clock)
    c.tick()
    aws.failing.add("us-west-2")
    clock.now += collector.FRESH + 60
    c.tick()  # a fresh snapshot, with the last EC2 read kept
    line = cost_line(site.paths.state, "harvard-2026", clock.now)
    assert line is not None and line.startswith(
        "cost (estimate, EC2 last read 2027-01-15 08:00 UTC): $"
    )
    test = cost_line(site.paths.state, "zab-test", clock.now)
    assert test is not None and test.startswith("cost (estimate): $")  # read on time
    down = FakeAws()  # a collector that never read EC2: no line
    down.failing.update({"us-west-2", "us-east-1"})
    make(site, aws=down, clock=Clock()).tick()
    assert cost_line(site.paths.state, "harvard-2026", NOW) is None


def sweep_site(site, clock, run, *, idle=None, at=None):
    """A collector whose alice was last read `at` (default: SWEEP_EVERY ago), idle stop off
    unless `idle` says otherwise."""
    values = status("alice", idle=False)
    if idle is not None:
        values["idle_stop"] = idle
    note_status(site.paths.state, "harvard-2026", {"schema": 1, "vms": [values]},
                NOW - collector.SWEEP_EVERY if at is None else at)  # fmt: skip
    return make(site, run=run, clock=clock)


def test_a_timer_active_but_not_enabled_is_not_swept(site):
    """The review of #494: `enabled: false, active: true` (a timer started, not enabled) is
    idle stop on."""
    run = FakeRun()
    on = {"enabled": False, "active": True, "minutes": 60, "last_verdict": "stay up"}
    sweep_site(site, Clock(), run, idle=on).tick()
    assert run.argvs == []


def test_a_read_before_the_boot_says_nothing_of_idle_stop_now(site):
    """The review of #494: alice was read off before her current boot (she runs since NOW - 3600
    in FakeAws): that read doesn't make her sweepable."""
    run = FakeRun()
    sweep_site(site, Clock(), run, at=NOW - 3601).tick()
    assert run.argvs == []
    run2 = FakeRun()
    sweep_site(site, Clock(), run2, at=NOW - 3599).tick()  # read since the boot: swept
    assert len(run2.argvs) == 1


@pytest.mark.parametrize("event", ["apply_start", "apply_end"])
@pytest.mark.parametrize("verb", sorted(collector.IDLE_VERBS))
def test_a_plan_that_may_change_idle_stop_voids_the_read_before_it(site, event, verb):
    """The review of #494: a read of idle stop off, then a plan that may turn it on (its
    apply_start or apply_end names the VM): no sweep until a read after it."""
    audit = Path(site.paths.state) / "audit.jsonl"
    record = {"seq": 9, "ts": "2027-01-15T07:55:00Z", "event": event, "verb": verb,
              "workshop": "harvard-2026", "plan": "P-9", "targets": ["alice"]}  # fmt: skip
    with audit.open("a") as f:
        f.write(json.dumps(record) + "\n")
    run = FakeRun()
    sweep_site(site, Clock(), run, at=NOW - collector.SWEEP_EVERY - 60).tick()  # 07:49
    assert run.argvs == []
    other = {**record, "targets": ["bob"]}  # another VM's plan voids nothing of alice's
    audit.write_text(audit.read_text().replace(json.dumps(record), json.dumps(other)))
    run2 = FakeRun()
    sweep_site(site, Clock(), run2, at=NOW - collector.SWEEP_EVERY - 60).tick()
    assert len(run2.argvs) == 1


def test_a_sweep_without_an_answer_waits_too(site):
    """The review of #494: a sweep that gets no answer for a VM (no JSON, a failure, the VM
    left out) notes the try, so the next waits SWEEP_EVERY, not one pass."""
    clock = Clock()

    class Garbage(FakeRun):
        def __call__(self, argv, env, cwd):
            self.argvs.append(argv)
            return "not json"

    run = Garbage()
    c = sweep_site(site, clock, run)
    c.tick()
    assert len(run.argvs) == 1
    tried = c.status["harvard-2026/alice"]["tried"]
    assert tried == {"at": NOW, "bridge": "no answer"}
    for _ in range(5):
        clock.now += 60
        c.tick()
    assert len(run.argvs) == 1
    clock.now += collector.SWEEP_EVERY
    c.tick()
    assert len(run.argvs) == 2


def test_close_ends_a_running_sweep(site, tmp_path):
    """The review of #494: the exit after an admin.toml change doesn't wait for a sweep, whose
    thread the interpreter joins at exit: `close` ends its process group, and the gate starts
    no other (Codex on #494)."""
    c = make(site, run=collector.run_status)
    done = []
    env = {"PATH": "/usr/bin:/bin"}
    worker = threading.Thread(
        target=lambda: done.append(c.run(["sleep", "60"], env, str(tmp_path))), daemon=True
    )
    worker.start()
    for _ in range(500):
        if c.gate.groups:
            break
        time.sleep(0.01)
    assert c.gate.groups
    c.close()
    worker.join(10)
    assert not worker.is_alive() and done == [""] and not c.gate.groups
    with pytest.raises(collector.Closed):
        c.run(["true"], env, str(tmp_path))


def test_close_stops_the_sweep_before_its_next_workshop(site):
    """Codex on #494: a sweep of several workshops stops at the next one once closed."""
    c = make(site)
    seen = []

    def run(argv, env, cwd):
        seen.append(argv)
        c.close()  # as main's exit would, while this workshop's sweep runs
        return json.dumps({"schema": 1, "vms": []})

    c.run = run
    harvard, test = c.config.workshops
    c._sweep_all([(harvard, ["alice"]), (test, ["erin"])])
    assert len(seen) == 1


@pytest.mark.parametrize("ws", [
    {"ec2": "x", "cost": {"month": 1, "projected": 2, "running": 1, "vms": 1}},
    {"ec2": {"at": "2027-01-15T08:00:00Z"}, "cost": {"month": "x", "projected": 2}},
    {"ec2": {"at": "2027-01-15T08:00:00Z"},
     "cost": {"month": 1, "projected": 2, "running": 1, "vms": 1, "unpriced": "x9.huge"}},
    "not a section",
])  # fmt: skip
def test_a_malformed_snapshot_gives_no_cost_line(site, ws):
    """The review of #494: `cost_line` never raises on what it reads; at worst no line."""
    state = Path(site.paths.state)
    snapshot = {"at": NOW, "workshops": {"harvard-2026": ws}}
    (state / "console" / "snapshot.json").write_text(json.dumps(snapshot))
    line = cost_line(site.paths.state, "harvard-2026", NOW)
    assert line is None or line.startswith("cost (estimate): $1 this month")
    (state / "console" / "snapshot.json").write_text(json.dumps({"at": NOW, "workshops": [1]}))
    assert cost_line(site.paths.state, "harvard-2026", NOW) is None


@pytest.mark.parametrize("case", ["evicted", "wide", "no targets"])
def test_an_idle_stop_change_counts_from_the_whole_log(site, case):
    """Codex round 4 on #494: the change is kept from every record, whole: after 200 more
    records of the workshop (the view keeps 200), past the view's 50 targets, and, for a
    record without its targets, for the whole workshop."""
    audit = Path(site.paths.state) / "audit.jsonl"
    targets = [f"p{n}" for n in range(60)] + ["alice"] if case == "wide" else ["alice"]
    change = {"seq": 9, "ts": "2027-01-15T07:55:00Z", "event": "apply_end", "verb": "rollout",
              "workshop": "harvard-2026", "plan": "P-9", "exit": 0, "targets": targets}  # fmt: skip
    if case == "no targets":
        del change["targets"]
    lines = [change]
    if case == "evicted":
        lines += [{"seq": 10 + n, "ts": "2027-01-15T07:56:00Z", "event": "read",
                   "workshop": "harvard-2026", "verb": "status"} for n in range(250)]  # fmt: skip
    with audit.open("a") as f:
        f.write("".join(json.dumps(r) + "\n" for r in lines))
    run = FakeRun()
    c = sweep_site(site, Clock(), run, at=NOW - collector.SWEEP_EVERY - 60)
    c.tick()
    assert run.argvs == []
    assert c.idle_changed[f"harvard-2026/{'*' if case == 'no targets' else 'alice'}"] == NOW - 300


def test_a_retired_row_without_a_vm_is_left_out(site):
    """#559: a row a `!retire` ended has no VM any more: the console leaves it out, from the
    whole log. One whose VM is still there (a launch by hand since, say) is listed with it."""
    audit = Path(site.paths.state) / "audit.jsonl"
    rows = {s: {"cells": {"teardown": "ok"}, "retired": True} for s in ("alice", "carol")}
    record = {"seq": 9, "ts": "2027-01-15T07:55:00Z", "event": "apply_end", "verb": "retire",
              "workshop": "harvard-2026", "plan": "P-9", "args": ["alice", "carol"], "exit": 0,
              "targets": ["alice", "carol"], "rows": rows}  # fmt: skip
    with audit.open("a") as f:
        f.write(json.dumps(record) + "\n")
    listed = [row["slug"] for row in make(site).tick()["workshops"]["harvard-2026"]["vms"]]
    assert "carol" not in listed and "alice" in listed and "bob" in listed
