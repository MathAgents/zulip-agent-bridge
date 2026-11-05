"""`workshop/aws/power.py` (docs/ADMIN.md §3 action 6, §3.2; issue #325) against a fake EC2."""

from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import aws_common as ac
import power
import pytest

ROSTER = """slug,email,host,ip,region
alice,alice@example.org,ws-alice,203.0.113.1,us-west-2
bob,bob@example.org,ws-bob,203.0.113.2,us-west-2
carol,carol@example.org,,,
dave,dave@example.org,ws-dave,198.51.100.4,us-east-2
"""
W = {"workshop": "nt-agents-2026"}
#: The admin server, tagged with the fleet's scope by mistake: no `participant` tag.
SERVER = {"Name": "zab-admin", **W, "zab-admin": "nt"}
SPARES = {
    "nt-agents-2026/us-west-2/zab-spare-1": {"name": "zab-spare-1", "ip": "203.0.113.11"},
    "nt-agents-2026/us-west-2/zab-spare-2": {
        "name": "zab-spare-2",
        "ip": "203.0.113.12",
        "adopted_by": "erin",
    },
    "nt-agents-2026/us-east-2/zab-spare-1": {"name": "zab-spare-1", "ip": "198.51.100.11"},
    "other-event/us-west-2/zab-spare-3": {"name": "zab-spare-3", "ip": "203.0.113.13"},
}
NO_VM = "FAILED: no live instance with workshop=nt-agents-2026 and a participant tag has it"


def vm(name: str, **extra: str) -> dict[str, str]:
    return {"Name": f"ws-{name}", **W, "participant": name, **extra}


class FakeTime:
    """`power.time`: a clock that only `sleep` moves (and a test's own probe)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        assert s > 0
        self.now += s


class FakeEc2:
    """Time passes between lookups: after each one, a VM `pending` runs and one `stopping`
    stops, unless it is `slow`."""

    def __init__(self) -> None:
        self.instances: list[dict] = []
        self.calls: list[tuple] = []
        self.dry = False  # a mutation fails the test
        self.broken: set[str] = set()  # start/stop fails
        self.slow: set[str] = set()  # never gets where it is going
        self.after: dict[str, str] = {}  # the state a start/stop leaves, instead of the usual
        self.down = False  # the lookup fails
        self.down_after = 0  # ... from the lookup after this many (0: all of them)
        self.no_ssh: set[str] = set()  # addresses whose port 22 never answers
        self.ssh_checks: list[str] = []
        self.time = FakeTime()

    def ssh_ready(self, ip: str) -> bool:
        if self.dry:
            pytest.fail("a dry run waited for SSH")
        self.ssh_checks.append(ip)
        return ip not in self.no_ssh

    def add(self, iid: str, ip: str, state: str, tags: dict[str, str]) -> None:
        inst = {"InstanceId": iid, "State": {"Name": state}, "PublicIpAddress": ip}
        self.instances.append({**inst, "Tags": ac.tag_list(tags)})

    def state(self, iid: str) -> str:
        return next(i["State"]["Name"] for i in self.instances if i["InstanceId"] == iid)

    def find_instances(self, tags, states, *, deadline=None, clock=None):
        self.calls.append(("find_instances", dict(tags), states))
        if self.down and len(self.lookups()) > self.down_after:
            raise RuntimeError("ExpiredToken: the security token included in the request\nmore")
        found = [i for i in self.instances if ac.has_tags(i, tags) and i["State"]["Name"] in states]
        found = copy.deepcopy(found)
        for inst in self.instances:
            moved = {"pending": "running", "stopping": "stopped"}.get(inst["State"]["Name"])
            if moved and inst["InstanceId"] not in self.slow:
                inst["State"]["Name"] = moved
        return found

    def lookups(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "find_instances"]

    def _change(self, call: str, ids: list[str], new: str, broken: set[str]) -> None:
        if self.dry:
            pytest.fail(f"a dry run called {call}")
        self.calls.append((call, list(ids)))
        for inst in self.instances:
            if inst["InstanceId"] in ids:
                if inst["InstanceId"] in broken:
                    raise RuntimeError("IncorrectInstanceState: not now\nsecond line")
                inst["State"]["Name"] = self.after.get(inst["InstanceId"], new)

    def start(self, ids):
        self._change("start", ids, "pending", self.broken)

    def stop(self, ids):
        self._change("stop", ids, "stopping", self.broken)

    def changes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] != "find_instances"]


def fleet() -> FakeEc2:
    ec2 = FakeEc2()
    ec2.add("i-alice", "203.0.113.1", "running", vm("alice"))
    ec2.add("i-bob", "203.0.113.2", "stopped", vm("bob"))
    ec2.add("i-spare1", "203.0.113.11", "running", vm("zab-spare-1"))
    ec2.add("i-server", "203.0.113.99", "running", SERVER)
    return ec2


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """(argv prefix, fake EC2, regions Ec2 was made for, roster path, spares path)."""
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    spares = tmp_path / "spares.json"
    spares.write_text(json.dumps(SPARES))
    ec2 = fleet()
    regions: list[str] = []

    def connect(region, **kw):
        assert kw == {"config": power.EC2_CONFIG}  # every call gives up soon (#334)
        regions.append(region)
        return ec2

    monkeypatch.setattr(power, "Ec2", connect)
    monkeypatch.setattr(power, "ssh_answers", ec2.ssh_ready)  # no network
    monkeypatch.setattr(power, "time", ec2.time)  # no sleeping
    monkeypatch.setattr(power, "SPARES_FILE", tmp_path / "out" / "spares.json")  # none
    argv = ["--roster", str(roster), "--spares-file", str(spares), "--region", "us-west-2"]
    return argv, ec2, regions, roster, spares


def lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


ROW_SKIPS = [
    "skip carol: no ip in the roster (no VM yet)",
    "skip dave: its VM is in us-east-2, not us-west-2",
]
SPARE_SKIPS = ["skip zab-spare-2: adopted by erin: name that participant"]


# ------------------------------------------------------------------------------ selection


def test_all_and_spares_stop_the_fleet_and_never_the_server(setup, capsys):
    argv, ec2, regions, _, _ = setup
    assert power.main([*argv, "--stop", "--all", "--spares"]) == 0
    assert lines(capsys) == [
        "scope: workshop=nt-agents-2026; region us-west-2",
        *ROW_SKIPS,
        *SPARE_SKIPS,
        "alice i-alice (running): stop requested",
        "bob i-bob (stopped): unchanged",
        "zab-spare-1 i-spare1 (running): stop requested",
        "stop 2, unchanged 1, failed 0",
    ]
    assert ec2.changes() == [("stop", ["i-alice"]), ("stop", ["i-spare1"])]
    assert ec2.state("i-server") == "running" and regions == ["us-west-2"]
    assert ec2.calls[0] == ("find_instances", W, power.LIVE_STATES)  # every scope tag


def test_all_never_includes_the_spares_and_spares_never_the_rows(setup, capsys):
    """ADMIN §2.2: `all` means every participant row and never the spares."""
    argv, ec2, _, _, _ = setup
    assert power.main([*argv, "--stop", "--all"]) == 0
    assert lines(capsys)[1:] == [
        *ROW_SKIPS,
        "alice i-alice (running): stop requested",
        "bob i-bob (stopped): unchanged",
        "stop 1, unchanged 1, failed 0",
    ]
    assert power.main([*argv, "--stop", "--spares"]) == 0
    assert lines(capsys)[1:] == [
        *SPARE_SKIPS,
        "zab-spare-1 i-spare1 (running): stop requested",
        "stop 1, unchanged 0, failed 0",
    ]
    assert ec2.changes() == [("stop", ["i-alice"]), ("stop", ["i-spare1"])]


def test_all_leaves_out_a_row_whose_teardown_cleared_its_ip(setup, capsys):
    """#604, #606: a row a teardown ended keeps its `host`, and its `ip` is cleared. `--all`
    takes only rows with an ip, so it skips that row by itself; which is why the admin bot
    passes `--all` even with rows retired, once none of them keeps an ip (`_build_power`)."""
    argv, ec2, _, roster, _ = setup
    roster.write_text(ROSTER.replace("ws-alice,203.0.113.1,", "ws-alice,,"))
    assert power.main([*argv, "--stop", "--all"]) == 0
    assert lines(capsys)[1:] == [
        "skip alice: no ip in the roster (no VM yet)",
        *ROW_SKIPS,
        "bob i-bob (stopped): unchanged",
        "stop 0, unchanged 1, failed 0",
    ]
    assert ec2.changes() == [] and ec2.state("i-alice") == "running"


@pytest.mark.parametrize("extra", [[], ["--only", "alice", "--all"], ["--only", "x", "--spares"]])
def test_no_selection_or_only_with_all_is_a_usage_error(setup, capsys, extra):
    """There is no implicit "everyone" (ADMIN §2.2), and `--only` stands alone."""
    argv, ec2, regions, _, _ = setup
    with pytest.raises(SystemExit) as exc:
        power.main([*argv, "--stop", *extra])
    assert exc.value.code == 2 and regions == [] and ec2.calls == []
    assert "--all" in capsys.readouterr().err


def test_a_dry_run_prints_the_same_lines_and_changes_nothing(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.dry = True
    assert power.main([*argv, "--stop", "--all", "--spares", "--wait", "--dry-run"]) == 0
    assert lines(capsys)[1:] == [
        *ROW_SKIPS,
        *SPARE_SKIPS,
        "alice i-alice (running): would stop",
        "bob i-bob (stopped): unchanged",
        "zab-spare-1 i-spare1 (running): would stop",
        "would stop 2, unchanged 1, failed 0",
        "dry run: nothing changed",
    ]
    assert power.main([*argv, "--start", "--all", "--wait", "--dry-run"]) == 0  # no SSH wait
    assert lines(capsys)[-3:] == [
        "bob i-bob (stopped): would start",
        "would start 1, unchanged 1, failed 0",
        "dry run: nothing changed",
    ]
    assert ec2.changes() == [] and ec2.state("i-alice") == "running" and not ec2.ssh_checks


def test_only_is_repeatable_and_takes_spares(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.instances[2]["State"]["Name"] = "stopped"
    rc = power.main([*argv, "--start", "--only", "zab-spare-1", "--only", "bob", "--wait"])
    assert rc == 0
    assert lines(capsys)[1:] == [
        "zab-spare-1 i-spare1 (stopped): start requested",
        "bob i-bob (stopped): start requested",
        "zab-spare-1 i-spare1: running, SSH answers",
        "bob i-bob: running, SSH answers",
        "start 2, unchanged 0, failed 0",
    ]
    assert ec2.changes() == [("start", ["i-spare1"]), ("start", ["i-bob"])]
    assert sorted(ec2.ssh_checks) == ["203.0.113.11", "203.0.113.2"]
    assert len(ec2.lookups()) == 3 and ec2.time.now == power.POLL_S  # both pending, then up


def test_start_wait_needs_ssh_and_covers_a_vm_already_on_its_way(setup, capsys):
    """The month-end (ADMIN §11.5): `--start --only <slug> --wait` returns once the VM runs and
    SSH answers, whether this run started it or the waker already had (`pending`). A VM that
    fails the wait counts as failed only (#334)."""
    argv, ec2, _, _, _ = setup
    ec2.instances[0]["State"]["Name"] = "pending"
    ec2.no_ssh.add("203.0.113.1")
    assert power.main([*argv, "--start", "--only", "alice", "--only", "bob", "--wait"]) == 1
    assert lines(capsys)[1:] == [
        "alice i-alice (pending): unchanged",
        "bob i-bob (stopped): start requested",
        "bob i-bob: running, SSH answers",
        "alice i-alice: FAILED waiting: running, but SSH at 203.0.113.1 does not answer after "
        "10 minutes",
        "start 1, unchanged 0, failed 1",
    ]
    assert ec2.changes() == [("start", ["i-bob"])]
    assert ec2.time.now == power.WAIT_S  # the fake probe takes no time


@pytest.mark.parametrize(
    "only,message",
    [
        (["carol"], "carol: no ip in the roster (no VM yet)"),
        (["dave"], "dave: its VM is in us-east-2, not us-west-2"),
        (["zab-spare-2"], "zab-spare-2: adopted by erin: name that participant"),
        (["zab-spare-3"], "no such roster row, nor a spare nt-agents-2026/us-west-2/zab-spare-3"),
        (["zab-spare-9"], "no such roster row, nor a spare"),
        (["nobody"], "nobody: no such roster row"),
        (["alice", "carol", "nobody"], "carol: no ip in the roster (no VM yet); nobody: no such"),
    ],
)
def test_a_bad_only_is_refused_before_any_aws_call(setup, capsys, only, message):
    argv, ec2, regions, _, _ = setup
    extra = [arg for name in only for arg in ("--only", name)]
    assert power.main([*argv, "--stop", *extra]) == 1
    assert message in capsys.readouterr().err
    assert regions == [] and ec2.calls == []


def test_a_spares_json_entry_that_is_not_a_spare_name_is_never_a_spare(setup, capsys):
    argv, ec2, regions, _, spares = setup
    spares.write_text(json.dumps({"nt-agents-2026/us-west-2/frank": {"ip": "203.0.113.1"}}))
    assert power.main([*argv, "--stop", "--only", "frank"]) == 1
    assert "frank: no such roster row, nor a spare" in capsys.readouterr().err
    assert power.main([*argv, "--stop", "--spares"]) == 0
    assert lines(capsys)[1:] == [
        "skip frank: no such roster row, nor a spare nt-agents-2026/us-west-2/frank in spares.json",
        "nothing to do",
    ]
    assert regions == [] and ec2.calls == []


def test_a_spare_whose_address_a_row_uses_is_refused(setup, capsys):
    """Handed over by hand (push_config --adopt-spare without adopt_spare.py): stopping
    `zab-spare-1` would stop alice's VM, so it must be named as alice."""
    argv, ec2, regions, roster, _ = setup
    roster.write_text(ROSTER.replace("ws-alice,203.0.113.1", "ws-alice,203.0.113.11"))
    assert power.main([*argv, "--stop", "--only", "zab-spare-1"]) == 1
    assert "zab-spare-1: its address is alice's now (handed over): name alice" in (
        capsys.readouterr().err
    )
    assert regions == [] and ec2.calls == []
    assert power.main([*argv, "--stop", "--spares"]) == 0
    assert "skip zab-spare-1: its address is alice's now (handed over): name alice" in (
        lines(capsys)
    )
    assert power.main([*argv, "--stop", "--all"]) == 0
    assert "alice i-spare1 (running): stop requested" in lines(capsys)
    assert ec2.changes() == [("stop", ["i-spare1"])]


def test_two_rows_with_one_ip_are_refused_or_failed(setup, capsys):
    """The review of 591686e: erin was given zab-spare-1 by hand (never marked adopted), then
    its address was pasted into alice's row too. Neither row may act on that VM."""
    argv, ec2, regions, roster, _ = setup
    shared = ROSTER.replace("ws-alice,203.0.113.1", "ws-alice,203.0.113.11")
    roster.write_text(shared + "erin,erin@example.org,ws-erin,203.0.113.11,us-west-2\n")
    assert power.main([*argv, "--stop", "--only", "alice"]) == 1
    assert "alice: its ip 203.0.113.11 is also erin's: fix the roster" in capsys.readouterr().err
    assert regions == [] and ec2.calls == []
    assert power.main([*argv, "--stop", "--all"]) == 1
    assert lines(capsys)[1:] == [
        "skip alice: its ip 203.0.113.11 is also erin's: fix the roster",
        *ROW_SKIPS,
        "skip erin: its ip 203.0.113.11 is also alice's: fix the roster",
        "bob i-bob (stopped): unchanged",
        "stop 0, unchanged 1, failed 2",
    ]
    assert ec2.changes() == [] and ec2.state("i-spare1") == "running"
    roster.write_text(
        "slug,email,host,ip,region\n"
        "alice,alice@example.org,ws-alice,203.0.113.11,us-west-2\n"
        "erin,erin@example.org,ws-erin,203.0.113.11,us-west-2\n"
    )
    assert power.main([*argv, "--stop", "--all"]) == 1  # nothing else to do: still a failure
    assert lines(capsys)[-1] == "nothing to do" and regions == ["us-west-2"]


def test_the_region_comes_from_the_roster_or_is_refused(setup, capsys):
    argv, _, regions, roster, _ = setup
    argv = argv[:-2]  # no --region
    assert power.main([*argv, "--stop", "--all"]) == 1
    err = capsys.readouterr().err
    assert "give --region: the roster's VMs are in us-east-2, us-west-2" in err
    assert regions == []
    without_dave = "".join(ROSTER.splitlines(keepends=True)[:4])
    roster.write_text(without_dave.replace("203.0.113.1,us-west-2", "203.0.113.1,"))
    assert power.main([*argv, "--stop", "--all", "--dry-run"]) == 0
    assert regions == ["us-west-2"]  # an empty region column is us-west-2, as in status.py
    roster.write_text("slug,email\ncarol,carol@example.org\n")
    assert power.main([*argv, "--stop", "--all"]) == 1
    assert "no row has an ip" in capsys.readouterr().err


def test_a_missing_spares_file_is_an_error_only_when_named(setup, capsys):
    argv, ec2, regions, roster, spares = setup
    spares.unlink()
    for selection in (["--spares"], ["--all", "--spares"], ["--only", "zab-spare-1"]):
        assert power.main([*argv, "--stop", *selection]) == 1
        assert f"--spares-file {spares} not found" in capsys.readouterr().err
        assert regions == []
    default = [arg for arg in argv if arg not in ("--spares-file", str(spares))]
    roster.write_text("slug,email\ncarol,carol@example.org\n")
    assert power.main([*default, "--start", "--all", "--spares"]) == 0  # none launched yet
    assert lines(capsys)[1:] == ["skip carol: no ip in the roster (no VM yet)", "nothing to do"]
    assert regions == [] and ec2.calls == []


@pytest.mark.parametrize("selection", [["--all"], ["--only", "alice"], ["--only", "bob"]])
def test_rows_alone_need_no_spares_file(setup, capsys, selection):
    """A workshop launched without spares has no spares.json (launch.py writes it only for
    spares), and the admin bot always names it (ADMIN §2.6): a selection of rows only works
    (#333)."""
    argv, _, regions, _, spares = setup
    spares.unlink()
    assert power.main([*argv, "--stop", *selection, "--dry-run"]) == 0, capsys.readouterr().err
    assert regions == ["us-west-2"]
    assert any(line.startswith(("alice ", "bob ")) for line in lines(capsys))


# ------------------------------------------------------- never an instance without the tag


def test_the_server_is_never_selected_even_at_a_roster_address(setup, capsys):
    """A roster `ip` that is the server's, or a spare's address that is a leftover AMI builder's
    (`build_ami.py --keep`), finds nothing: neither has a `participant` tag. Nor does a VM whose
    tag is blank."""
    argv, ec2, _, roster, spares = setup
    roster.write_text(ROSTER.replace("203.0.113.1,", "203.0.113.99,"))
    ec2.add("i-builder", "203.0.113.98", "stopped", {"Name": "zab-ami-build", **W, "role": "x"})
    data = dict(SPARES)
    data["nt-agents-2026/us-west-2/zab-spare-1"] = {"name": "zab-spare-1", "ip": "203.0.113.98"}
    spares.write_text(json.dumps(data))
    ec2.instances[1]["Tags"] = ac.tag_list({**vm("bob"), "participant": " "})
    rc = power.main([*argv, "--start", "--only", "alice", "--only", "zab-spare-1", "--only", "bob"])
    assert rc == 1
    assert lines(capsys)[1:] == [
        f"alice 203.0.113.99: {NO_VM}",
        f"zab-spare-1 203.0.113.98: {NO_VM}",
        f"bob 203.0.113.2: {NO_VM}",
        "start 0, unchanged 0, failed 3",
    ]
    assert ec2.changes() == [] and ec2.state("i-server") == "running"
    assert ec2.state("i-builder") == "stopped"


@pytest.mark.parametrize("key", ["zab-admin", "ZAB-ADMIN", "Zab-Admin"])
def test_the_server_is_never_selected_even_tagged_as_a_participant(setup, capsys, key):
    """Defence in depth for a manual run, where no IAM deny applies: a `zab-admin` tag wins, in
    any case, as in the bot's own rule (`starter.fleet_vm`; the review of #354)."""
    argv, ec2, _, _, _ = setup
    ec2.instances[0]["Tags"] = ac.tag_list(vm("alice", **{key: "nt"}))
    assert power.main([*argv, "--stop", "--only", "alice"]) == 1
    assert lines(capsys)[1] == f"alice 203.0.113.1: {NO_VM}"
    assert ec2.changes() == [] and ec2.state("i-alice") == "running"


def test_another_workshops_vm_is_never_matched(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.instances[0]["Tags"] = ac.tag_list({**vm("alice"), "workshop": "another-event"})
    assert power.main([*argv, "--stop", "--only", "alice"]) == 1
    assert lines(capsys)[1] == f"alice 203.0.113.1: {NO_VM}"
    assert ec2.changes() == []


def test_a_two_tag_scope_needs_both_tags(setup, capsys):
    """The rig's scope (`zab-role=test`, `workshop=zab-test`): one lookup with both tags, and a
    VM carrying only one of them is not matched."""
    argv, ec2, _, _, spares = setup
    rig = {"zab-role": "test", "workshop": "zab-test"}
    ec2.instances[0]["Tags"] = ac.tag_list({**vm("alice"), **rig})
    ec2.instances[1]["Tags"] = ac.tag_list({**vm("bob"), "workshop": "zab-test"})
    ec2.instances[2]["Tags"] = ac.tag_list({**vm("zab-spare-1"), **rig})
    key = f"{ac.scope_label(rig)}/us-west-2/zab-spare-1"
    spares.write_text(json.dumps({key: {"name": "zab-spare-1", "ip": "203.0.113.11"}}))
    tags = ["--tag", "zab-role=test", "--tag", "workshop=zab-test"]
    assert power.main([*argv, *tags, "--stop", "--all", "--spares"]) == 1
    assert lines(capsys)[3:] == [
        "alice i-alice (running): stop requested",
        "bob 203.0.113.2: FAILED: no live instance with zab-role=test, workshop=zab-test and a "
        "participant tag has it",
        "zab-spare-1 i-spare1 (running): stop requested",
        "stop 2, unchanged 0, failed 1",
    ]
    assert ec2.calls[0] == ("find_instances", rig, power.LIVE_STATES)


def test_a_wrong_ip_never_stops_another_participant(setup, capsys):
    """The VM at a row's address must carry its slug, the tag its `host` records (before slugs),
    or a spare's (a hand-over by hand, as status.py allows); a spare's must carry its own."""
    argv, ec2, _, roster, _ = setup
    ec2.add("i-frank", "203.0.113.50", "running", vm("frank"))  # not in this roster
    roster.write_text(ROSTER.replace("ws-alice,203.0.113.1", "ws-alice,203.0.113.50"))
    ec2.instances[2]["Tags"] = ac.tag_list(vm("zab-spare-4"))  # not spare-1's VM
    assert power.main([*argv, "--stop", "--only", "alice", "--only", "zab-spare-1"]) == 1
    assert lines(capsys)[1:3] == [
        "alice i-frank (running): FAILED: the VM at 203.0.113.50 is tagged participant=frank, "
        "not alice",
        "zab-spare-1 i-spare1 (running): FAILED: the VM at 203.0.113.11 is tagged "
        "participant=zab-spare-4, not zab-spare-1",
    ]
    assert ec2.changes() == []


def test_a_spare_handed_over_by_hand_and_a_tag_from_before_slugs_are_accepted(setup, capsys):
    argv, ec2, _, roster, _ = setup
    roster.write_text(
        ROSTER.replace("ws-alice,203.0.113.1", "ws-alice,203.0.113.11").replace(
            "ws-bob,203.0.113.2", "ws-Bob,203.0.113.2"
        )
    )
    ec2.instances[1]["Tags"] = ac.tag_list(vm("Bob"))
    assert power.main([*argv, "--start", "--only", "alice", "--only", "bob"]) == 0
    assert lines(capsys)[1:3] == [
        "alice i-spare1 (running): unchanged",  # the spare she was given, tagged zab-spare-1
        "bob i-bob (stopped): start requested",  # tagged Bob, as his `host` records
    ]
    assert ec2.changes() == [("start", ["i-bob"])]


# ------------------------------------------------------------------------- states and errors


@pytest.mark.parametrize("start,state", [(True, "pending"), (False, "stopping")])
def test_a_vm_on_its_way_there_is_left_alone(setup, capsys, start, state):
    argv, ec2, _, _, _ = setup
    ec2.instances[0]["State"]["Name"] = state
    assert power.main([*argv, "--start" if start else "--stop", "--only", "alice"]) == 0
    assert lines(capsys)[1] == f"alice i-alice ({state}): unchanged"
    assert ec2.changes() == []


def test_a_vm_on_its_way_to_the_other_state_fails_and_the_others_still_run(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.instances[0]["State"]["Name"] = "stopping"
    assert power.main([*argv, "--start", "--only", "alice", "--only", "bob"]) == 1
    assert lines(capsys)[1:] == [
        "alice i-alice (stopping): FAILED: it is stopping: rerun once it is stopped",
        "bob i-bob (stopped): start requested",
        "start 1, unchanged 0, failed 1",
    ]
    ec2.calls.clear()
    assert power.main([*argv, "--stop", "--only", "bob"]) == 1  # now pending
    want = "bob i-bob (pending): FAILED: it is pending: rerun once it is running"
    assert lines(capsys)[1] == want
    assert ec2.changes() == []


def test_an_api_error_is_reported_and_only_the_vms_served_are_waited_for(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.broken.add("i-alice")
    assert power.main([*argv, "--stop", "--all", "--spares", "--wait"]) == 1
    assert lines(capsys)[4:] == [
        "alice i-alice (running): FAILED: RuntimeError: IncorrectInstanceState: not now",
        "bob i-bob (stopped): unchanged",
        "zab-spare-1 i-spare1 (running): stop requested",
        "bob i-bob: stopped",
        "zab-spare-1 i-spare1: stopped",
        "stop 1, unchanged 1, failed 1",
    ]
    assert len(ec2.lookups()) == 3  # the selection; bob stopped, the spare stopping; both
    assert not ec2.ssh_checks  # a stop never waits for SSH


@pytest.mark.parametrize(
    "verb,after,why",
    [
        ("--stop", None, "stopping, not stopped, after 10 minutes"),  # slow
        ("--stop", "pending", "it is pending, on its way to running"),  # the waker
        ("--stop", "terminated", "it is gone: no live instance with workshop=nt-agents-2026"),
        ("--start", "stopped", "stopped, not running, after 10 minutes"),  # read again
    ],
)
def test_a_failed_wait_is_counted_as_failed_only(setup, capsys, verb, after, why):
    argv, ec2, _, _, _ = setup
    iid = "i-alice" if verb == "--stop" else "i-bob"
    ec2.slow.add(iid)
    if after:
        ec2.after[iid] = after
    assert power.main([*argv, verb, "--only", "alice", "--only", "bob", "--wait"]) == 1
    out = lines(capsys)
    (line,) = [line for line in out if "FAILED" in line]
    assert line.startswith(f"{iid[2:]} {iid}: FAILED waiting: {why}")
    assert out[-1] == f"{verb[2:]} 0, unchanged 1, failed 1"
    at_once = after in ("pending", "terminated")  # in the first round: no sleep at all
    assert ec2.time.now == (0 if at_once else power.WAIT_S)


def test_a_failed_lookup_while_waiting_fails_each_vm_left(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.down, ec2.down_after = True, 1
    assert power.main([*argv, "--stop", "--all", "--spares", "--wait"]) == 1
    expired = "RuntimeError: ExpiredToken: the security token included in the request"
    assert lines(capsys)[-4:] == [
        f"alice i-alice: FAILED waiting: the EC2 lookup: {expired}",
        f"bob i-bob: FAILED waiting: the EC2 lookup: {expired}",
        f"zab-spare-1 i-spare1: FAILED waiting: the EC2 lookup: {expired}",
        "stop 0, unchanged 0, failed 3",
    ]


@pytest.mark.parametrize(
    "round_s,up", [(4.0, ["p40", "p44", "p41"]), (power.WAIT_S + 60, ["p40", "p44"])]
)
def test_one_deadline_bounds_the_wait_however_many_vms_fail(monkeypatch, round_s, up):
    """#334: 45 VMs, 40 of whose port 22 never answers (from a laptop the security group doesn't
    admit) and two never running. Before, each was waited for in turn, up to 10 + 5 minutes
    apiece: hours. Now the wait ends within one round of the one deadline, a round of SSH tries
    taking `round_s`: 4 s (a try times out at 5; 4 cuts the last sleep short), or longer than
    the deadline. Every VM is read, and every running one tried."""
    ec2 = FakeEc2()
    monkeypatch.setattr(power, "time", ec2.time)
    targets = []
    for n in range(45):  # the odd ones stopped, so started by the run; the even ones running
        name, ip = f"p{n:02d}", f"198.51.100.{n + 1}"
        ec2.add(f"i-{name}", ip, "stopped" if n % 2 else "running", vm(name))
        targets.append(power.Target(name, ip))
    ec2.instances[42]["State"]["Name"] = "pending"  # already on its way: unchanged
    ec2.slow |= {"i-p42", "i-p43"}  # both pending for ever
    rounds: list[list[str]] = []

    def probe(ips):
        rounds.append(list(ips))
        ec2.time.now += round_s  # every try at once, none longer than a round
        return {ip for ip in ips if int(ip.rsplit(".", 1)[1]) > 40}  # p40 and up answer

    out: list[str] = []
    rc = power.power(ec2, targets, W, start=True, wait=True, log=out.append, probe=probe)
    assert rc == 1
    assert ec2.time.now <= power.WAIT_S + round_s  # the bound that ADMIN §3 states
    assert rounds[0] == [t.ip for t in targets[::2] if t.name != "p42"]  # all running at once
    assert len(ec2.lookups()) == len(rounds) + 1  # the selection, then one lookup a round
    waits = out[45:-1]  # one line per VM after its 45 request lines: answered or failed
    assert sorted(line.split()[0] for line in waits) == [t.name for t in targets]
    assert [line.split()[0] for line in waits if "SSH answers" in line] == up
    failed = sum("FAILED waiting" in line for line in waits)
    assert failed == 45 - len(up)
    started = int("p41" in up)  # each VM counted once (22 started, 23 unchanged)
    assert out[-1] == f"start {started}, unchanged 2, failed {failed}"
    late = "after 10 minutes"
    assert (
        f"p00 i-p00: FAILED waiting: running, but SSH at 198.51.100.1 does not answer {late}" in out
    )
    assert f"p42 i-p42: FAILED waiting: pending, not running, {late}" in out


def test_a_lookup_of_many_pages_stops_at_the_deadline(monkeypatch):
    """Codex on #503: `EC2_CONFIG` bounds each request, not a lookup of many pages. The real
    `find_instances` asks for no page once the deadline has passed, and `settle` fails every
    VM still waiting rather than call one gone: ten pages of 50 s end one page past the
    deadline, not after 1,005 s."""
    clock = FakeTime()
    monkeypatch.setattr(power, "time", clock)
    tags = ac.tag_list(vm("alice"))
    alice = {"InstanceId": "i-alice", "State": {"Name": "running"}, "Tags": tags}
    asked: list[int] = []

    def client(pages: int) -> ac.Ec2:
        def paginate(**kw):  # lazy, as botocore's: a page is asked for when it is needed
            for n in range(pages):
                asked.append(n)
                clock.now += 50
                more = {"NextToken": str(n + 1)} if n < pages - 1 else {}
                yield {"Reservations": [{"Instances": [alice] if n == pages - 1 else []}], **more}

        ec2 = object.__new__(ac.Ec2)
        ec2._ec2 = SimpleNamespace(get_paginator=lambda name: SimpleNamespace(paginate=paginate))
        return ec2

    ec2 = client(10)
    out: list[str] = []
    served = [power.Served("alice", "i-alice", "203.0.113.1", requested=False)]
    failed = power.settle(ec2, W, served, start=True, log=out.append, probe=lambda ips: set())
    assert failed == served and clock.now == 605 <= power.WAIT_S + 50  # one page past it
    assert asked == [*range(10), 0, 1]  # all ten in the first round; none past the deadline
    timeout = "TimeoutError: the lookup's pages ran past the wait's deadline"
    assert out == [f"alice i-alice: FAILED waiting: the EC2 lookup: {timeout}"]
    assert ec2.find_instances(W, power.LIVE_STATES) == [alice]  # no deadline: every page
    asked.clear()
    with pytest.raises(TimeoutError):  # a deadline already past: the first page, then no more
        ec2.find_instances(W, power.LIVE_STATES, deadline=0, clock=clock.monotonic)
    assert asked == [0]
    one = client(1).find_instances(W, power.LIVE_STATES, deadline=0, clock=clock.monotonic)
    assert one == [alice]  # a last page past the deadline is still a whole answer


def test_answering_tries_every_address_at_once(monkeypatch):
    """A round of SSH tries takes one try's 5 s, not 5 s an address: each fake try below waits
    for all the others, which would time out if they ran one after another."""
    ips = [f"198.51.100.{n}" for n in range(1, 41)]
    together = threading.Barrier(len(ips), timeout=10)

    def try_once(ip):
        together.wait()
        return ip.endswith("0")

    monkeypatch.setattr(power, "ssh_answers", try_once)
    assert power.answering(ips) == {ip for ip in ips if ip.endswith("0")}
    assert power.answering([]) == set()


def test_ssh_answers_is_one_tcp_try_at_port_22():
    tried = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def refused(addr, timeout):
        tried.append((addr, timeout))
        raise ConnectionRefusedError

    assert ac.ssh_answers("192.0.2.1", connect=lambda a, t: Conn()) is True
    assert ac.ssh_answers("192.0.2.1", connect=refused) is False
    assert tried == [(("192.0.2.1", 22), 5.0)]


def test_a_failed_lookup_is_one_line_not_a_traceback(setup, capsys):
    argv, ec2, _, _, _ = setup
    ec2.down = True
    assert power.main([*argv, "--stop", "--all"]) == 1
    assert lines(capsys)[-1] == (
        "FAILED: the EC2 lookup: RuntimeError: ExpiredToken: the security token included in the "
        "request"
    )
    assert ec2.changes() == []


def test_one_vm_at_two_addresses_is_acted_on_once(setup, capsys):
    argv, ec2, _, roster, _ = setup
    nic = {"Association": {"PublicIp": "203.0.113.21"}}
    ec2.instances[0]["NetworkInterfaces"] = [nic]
    roster.write_text(ROSTER + "alice2,alice2@example.org,ws-alice,203.0.113.21,us-west-2\n")
    assert power.main([*argv, "--stop", "--only", "alice", "--only", "alice2"]) == 0
    assert lines(capsys)[1:3] == [
        "alice i-alice (running): stop requested",
        "alice2 i-alice: the same VM as alice, above",
    ]
    assert ec2.changes() == [("stop", ["i-alice"])]


# ------------------------------------------------------------------------------ the real Ec2


class _Waiter:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def wait(self, **kw):
        self.log.append((self.name, kw))


class _Boto:
    def __init__(self):
        self.log: list[tuple] = []

    def start_instances(self, **kw):
        self.log.append(("start_instances", kw))

    def stop_instances(self, **kw):
        self.log.append(("stop_instances", kw))

    def get_waiter(self, name):
        return _Waiter(self.log, name)


def test_the_real_client_starts_and_stops_by_id():
    ec2 = object.__new__(ac.Ec2)
    ec2._ec2 = _Boto()
    ec2.start(["i-1"])
    ec2.stop(["i-2"])
    ec2.wait_running("i-1")
    assert ec2._ec2.log == [
        ("start_instances", {"InstanceIds": ["i-1"]}),
        ("stop_instances", {"InstanceIds": ["i-2"]}),
        ("instance_running", {"InstanceIds": ["i-1"]}),
    ]


def test_power_pys_client_gives_up_within_a_minute(monkeypatch):
    """#334: a round of `--wait` is one lookup, so power.py's client (`EC2_CONFIG`) gives up
    within about a minute, not botocore's 5 tries of 60 s and 60 s; the other scripts' clients
    keep botocore's defaults. The README and ADMIN §3 state these numbers."""
    import sys
    import types

    made = []
    boto3 = types.ModuleType("boto3")

    def client(name, **kw):
        made.append(copy.deepcopy((name, kw)))
        if "config" in kw:  # as botocore's client does, into the dict its Config keeps
            kw["config"][1]["retries"]["mode"] = "legacy"
        return object()

    boto3.client = client
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kw: ("Config", kw)
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    ac.Ec2("us-east-1", config=power.EC2_CONFIG)
    ac.Ec2("us-east-1")
    assert made == [
        ("ec2", {"region_name": "us-east-1", "config": ("Config", power.EC2_CONFIG)}),
        ("ec2", {"region_name": "us-east-1"}),
    ]
    tries, connect, read = 2, 10, 20  # as documented, and the constant never written into
    assert power.EC2_CONFIG == {
        "connect_timeout": connect,
        "read_timeout": read,
        "retries": {"total_max_attempts": tries},
    }
    round_s = tries * (connect + read) + 1 + 5  # a lookup, its one backoff, 128 silent tries
    assert power.WAIT_S + round_s < 11.5 * 60 < 15 * 60  # "about 11 minutes" (ADMIN §2.4)


def test_instance_ips_reads_the_address_of_a_stopped_vm_too():
    nic = {"Association": {"PublicIp": "203.0.113.7"}}
    assert ac.instance_ips({"PublicIpAddress": "203.0.113.7", "NetworkInterfaces": [nic]}) == {
        "203.0.113.7"
    }
    assert ac.instance_ips({"NetworkInterfaces": [nic, {}]}) == {"203.0.113.7"}
    assert ac.instance_ips({}) == set()


def test_error_line_is_one_capped_line():
    assert ac.error_line(RuntimeError("a\nb")) == "RuntimeError: a"
    assert ac.error_line(ValueError("")) == "ValueError: "
    assert len(ac.error_line(RuntimeError("x" * 500))) == len("RuntimeError: ") + 200


ADMIN_TOML = """\
provider = "aws"
admins = [101]
deployers = [101]
destructive_approvals = 1
[global]
scope = "mathagents"
regions = ["us-west-2"]
instance_types = ["m7a.2xlarge"]
[[workshop]]
name = "nt-agents-2026"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "/var/lib/zab-admin/ws/nt-agents-2026/roster.csv"
out_dir = "/var/lib/zab-admin/ws/nt-agents-2026/out"
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/nt.zuliprc"
user_id = 900
channels = [40]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-nt"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub"]
"""


def test_the_admin_bots_lists_run_on_a_fleet_launched_without_spares(tmp_path, monkeypatch, capsys):
    """#333: the lists `zab.admin.actions` builds for !start and !stop name `--spares-file`
    (ADMIN §2.6), which `launch.py` writes only when it launches spares. On such a fleet they
    run, as dry runs and for real, and `!stop spares` can't even be built."""
    import dataclasses
    import re

    from zab.admin import actions
    from zab.admin.config import RELEASE_PATTERNS, parse_admin_config
    from zab.admin.grammar import Command, Selection

    out = tmp_path / "out"
    out.mkdir()  # no spares.json
    roster = tmp_path / "roster.csv"
    roster.write_text("\n".join(ROSTER.splitlines()[:4]) + "\n")  # alice, bob, carol
    (workshop,) = parse_admin_config(ADMIN_TOML).workshops
    workshop = dataclasses.replace(workshop, roster=str(roster), out_dir=str(out))
    rows = (actions.Row("alice", "203.0.113.1"), actions.Row("bob", "203.0.113.2"))
    pattern = re.compile(RELEASE_PATTERNS["final"])
    fleet = actions.Fleet(workshop, (*rows, actions.Row("carol")), (), pattern)
    ec2 = FakeEc2()
    scoped = {"zab-scope": "mathagents"}  # #422: the bot's lists ask for the fleet-wide scope
    ec2.add("i-alice", "203.0.113.1", "running", vm("alice", **scoped))
    ec2.add("i-bob", "203.0.113.2", "stopped", vm("bob", **scoped))
    monkeypatch.setattr(power, "Ec2", lambda region, **kw: ec2)
    monkeypatch.setattr(power, "ssh_answers", ec2.ssh_ready)
    for verb, selection in (("stop", Selection(all=True)), ("start", Selection(slugs=("bob",)))):
        step = actions.build(Command(verb, selection=selection), fleet).steps[0]
        assert step.argv[5].endswith("/workshop/aws/power.py")  # python -E -s -B -u <script>
        ec2.dry = True
        assert power.main(list(step.dry_run[6:])) == 0, capsys.readouterr().err
        ec2.dry = False
        assert power.main(list(step.argv[6:])) == 0, capsys.readouterr().err
    assert ec2.changes() == [("stop", ["i-alice"]), ("start", ["i-bob"])]
    with pytest.raises(actions.ActionError, match="selects nothing"):
        actions.build(Command("stop", selection=Selection(spares=True)), fleet)
