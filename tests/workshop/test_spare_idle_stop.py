"""`workshop/aws/spare_idle_stop.py` (#715): each spare named gets its own idle stop, against a
fake EC2 and a fake SSH."""

from __future__ import annotations

import base64
import csv
import io
import json

import admin_common as adc
import aws_common as ac
import push_config as pc
import pytest
import spare_idle_stop as sis

from .scope import with_scope

W = {"workshop": "nt-agents-2026"}
SCOPE = ac.parse_tags(["workshop=nt-agents-2026"])
ROSTER = "slug,email,host,ip,region\nalice,alice@example.org,ws-alice,203.0.113.1,us-west-2\n"
KEY = "ssh-ed25519 " + base64.b64encode(b"the spare's host key").decode()
FINGERPRINT = adc.host_key_fingerprint(f"x {KEY}")
ONE, TWO, THREE = "203.0.113.11", "203.0.113.12", "203.0.113.13"
SPARES = {
    "nt-agents-2026/us-west-2/zab-spare-1": {
        "name": "zab-spare-1", "ip": ONE, "host_keys": f"SHA256:other {FINGERPRINT}",
    },
    "nt-agents-2026/us-west-2/zab-spare-2": {"name": "zab-spare-2", "ip": TWO, "host_keys": ""},
    "nt-agents-2026/us-west-2/zab-spare-3": {
        "name": "zab-spare-3", "ip": THREE, "adopted_by": "erin", "host_keys": FINGERPRINT,
    },
    # a spare handed over by hand (ORGANIZERS §7.2, "A broken VM"): alice holds its address
    "nt-agents-2026/us-west-2/zab-spare-4": {"name": "zab-spare-4", "ip": "203.0.113.1"},
    "nt-agents-2026/us-east-2/zab-spare-5": {"name": "zab-spare-5", "ip": "198.51.100.5"},
}  # fmt: skip
STAGING = "/tmp/zab-push.abc123"  # noqa: S108 - the VM's, as mktemp names it


def spare_record(name: str) -> str:
    record = json.dumps({"slug": "", "repo": "", "spare": name})
    mark = pc.OWNER_MARK
    return f"{mark}JSON\n{record}\n\n{mark}HOST\nws-{name}\n{mark}END\n"


class FakeEc2:
    def __init__(self, *instances: tuple[str, str, str, str], down: bool = False) -> None:
        self.instances = [
            {
                "InstanceId": iid,
                "State": {"Name": state},
                "PublicIpAddress": ip,
                "Tags": ac.tag_list({**W, "Name": f"ws-{tag}", "participant": tag}),
            }
            for iid, ip, state, tag in instances
        ]
        self.down = down

    def find_instances(self, tags, states):
        if self.down:
            raise RuntimeError("ExpiredToken: the security token is expired")
        return [i for i in self.instances if ac.has_tags(i, tags) and i["State"]["Name"] in states]

    def __getattr__(self, name):
        raise AssertionError(f"spare_idle_stop.py must not call {name}")


class FakeSsh:
    """`ssh-keyscan`, `ssh` and `scp`: the keyscan answers `KEY`, the owner check a spare's
    record (or `owners`), mktemp `STAGING`; `fail` fails a command that contains its key."""

    def __init__(self, *, owners=None, key=KEY, fail=None) -> None:
        self.calls: list[list[str]] = []
        self.owners, self.key, self.fail = owners or {}, key, fail or {}
        self.pinned: dict[str, str] = {}  # host → its known_hosts file's text, at the time

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append(list(argv))
        if argv[0] == "ssh-keyscan":
            return adc.RunResult(0, f"{argv[-1]} {self.key}\n", "")
        known = next(
            a.split("=", 1)[1].strip('"') for a in argv if a.startswith("UserKnownHostsFile=")
        )
        target = argv[-1] if argv[0] == "scp" else argv[-3]
        host = target.split("@", 1)[1].split(":", 1)[0]
        with open(known, encoding="utf-8") as f:
            self.pinned[host] = f.read()
        remote = argv[-1]
        for needle, err in self.fail.items():
            if needle in remote:
                return adc.RunResult(1, "", err)
        if argv[0] == "ssh" and remote == pc.OWNER_CMD:
            name = {ONE: "zab-spare-1", TWO: "zab-spare-2"}.get(host, "zab-spare-1")
            return adc.RunResult(0, self.owners.get(host, spare_record(name)), "")
        if argv[0] == "ssh" and remote == pc.MKTEMP_CMD:
            return adc.RunResult(0, STAGING + "\n", "")
        return adc.RunResult(0, "", "")

    def remotes(self, host: str) -> list[str]:
        return [c[-1] for c in self.calls if c[0] == "ssh" and c[-3] == f"ubuntu@{host}"]

    def sent(self) -> dict[str, str]:
        """Each file sent, by its staged name, read now (the run's temporary copies are gone,
        so the units are compared by what `idle_stop_units` renders)."""
        return {c[-1].rsplit("/", 1)[1]: c[-2] for c in self.calls if c[0] == "scp"}


def give(ec2, ssh, names, *, dry_run=False, minutes=10):
    """`sis.give` with every port 22 answering at once; (exit code, lines)."""
    lines: list[str] = []
    code = sis.give(
        ec2, roster_rows(ROSTER), SPARES, names, minutes=minutes, scope=SCOPE,
        region="us-west-2", dry_run=dry_run, runner=ssh, probe=set, log=lines.append,
        sleep=lambda s: None,
    )  # fmt: skip
    return code, lines


def roster_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


RUNNING = ("i-0aaa", ONE, "running", "zab-spare-1")


def test_a_running_spare_gets_its_idle_stop_pinned_to_its_consoles_key():
    ssh = FakeSsh()
    code, lines = give(FakeEc2(RUNNING), ssh, ["zab-spare-1"])
    assert code == 0, lines
    assert lines == [
        "zab-spare-1 i-0aaa: idle stop after 10 min (a spare's), installed",
        "installed 1, skipped 0, failed 0",
    ]
    assert ssh.calls[0] == sis.keyscan_argv(ONE)
    # the key the console printed, alone, in a known_hosts of this run: never the workshop's
    assert set(ssh.pinned) == {ONE} and ssh.pinned[ONE] == f"{ONE} {KEY}\n"
    assert all(str(adc.DEFAULT_KNOWN_HOSTS) not in " ".join(c) for c in ssh.calls)
    assert ssh.remotes(ONE) == [
        pc.OWNER_CMD,
        pc.MKTEMP_CMD,
        pc.spare_idle_stop_on_cmd(STAGING),
        f"rm -rf -- {STAGING}",
    ]
    assert sorted(ssh.sent()) == ["idle_stop.py", "zab-idle-stop.service", "zab-idle-stop.timer"]
    assert ssh.sent()["idle_stop.py"] == str(pc.IDLE_STOP_SCRIPT)


def test_the_units_are_a_spares_with_the_minutes_given(tmp_path, monkeypatch):
    """The service's text as staged: `--minutes 30 --spare`."""
    seen = {}

    class Reading(FakeSsh):
        def __call__(self, argv, **kw):
            if argv[0] == "scp" and argv[-2].endswith(".service"):
                with open(argv[-2], encoding="utf-8") as f:
                    seen["service"] = f.read()
            return super().__call__(argv, **kw)

    code, _ = give(FakeEc2(RUNNING), Reading(), ["zab-spare-1"], minutes=30)
    assert code == 0
    assert seen["service"] == pc.idle_stop_units(30, spare=True)["zab-idle-stop.service"]
    assert "--minutes 30 --spare\n" in seen["service"]


def test_spares_that_are_a_participants_or_stopped_are_skipped():
    """Adopted, handed over by hand, not in spares.json (or another region's): a participant's
    VM, or none; stopped or stopping: it gets idle stop at its next start. No SSH at all."""
    ec2 = FakeEc2(
        ("i-0bbb", TWO, "stopped", "zab-spare-2"),
        ("i-0ccc", "203.0.113.1", "running", "zab-spare-4"),
    )
    ssh = FakeSsh()
    names = ["zab-spare-2", "zab-spare-3", "zab-spare-4", "zab-spare-5", "zab-spare-9"]
    code, lines = give(ec2, ssh, names)
    assert code == 0 and not ssh.calls
    assert lines == [
        "zab-spare-2 i-0bbb (stopped): skipped: it gets idle stop at its next !start zab-spare-2",
        "skip zab-spare-3: adopted by erin: a participant's VM",
        "skip zab-spare-4: its address is alice's (handed over): a participant's VM",
        "skip zab-spare-5: not in spares.json for this scope and region",
        "skip zab-spare-9: not in spares.json for this scope and region",
        "installed 0, skipped 5, failed 0",
    ]


@pytest.mark.parametrize(
    ("ssh", "why"),
    [
        (FakeSsh(key="ssh-ed25519 " + base64.b64encode(b"an impostor").decode()),
         "is not one its console printed: nothing installed"),
        (FakeSsh(owners={ONE: spare_record("zab-spare-2")}),
         "the VM says it is 'zab-spare-2' (hostname 'ws-zab-spare-2'), not the spare "
         "zab-spare-1: nothing installed"),
        (FakeSsh(owners={ONE: spare_record("zab-spare-1").replace('"slug": ""', '"slug": "dave"')}),
         "the VM says it is 'dave' (hostname 'ws-zab-spare-1')"),
        (FakeSsh(fail={"systemctl enable": "Failed to enable unit"}),
         "idle stop: Failed to enable unit"),
        (FakeSsh(fail={"ZAB-OWNER": "Connection refused"}), "ssh: Connection refused"),
    ],
)  # fmt: skip
def test_a_spare_that_isnt_what_it_should_be_fails_and_gets_nothing(ssh, why):
    code, lines = give(FakeEc2(RUNNING), ssh, ["zab-spare-1"])
    assert code == 1 and lines[-1] == "installed 0, skipped 0, failed 1"
    assert lines[0].startswith("zab-spare-1 i-0aaa: FAILED: ") and why in lines[0], lines[0]
    if "Failed to enable" in why:  # the staging goes, whatever happened
        assert ssh.remotes(ONE)[-1] == f"rm -rf -- {STAGING}"
    else:
        assert not any(c[0] == "scp" for c in ssh.calls)


def test_after_a_start_a_spare_ec2_still_reads_as_stopped_is_waited_for():
    """Codex round 1 on #729: `!start` asks EC2 to start the spare, without `--wait`, and EC2
    may still read it `stopped` just after; with `--started` it is waited for, as a pending one
    is, and gets its idle stop once its port 22 answers. One that never answers fails, never
    skips."""
    for state in ("stopped", "stopping"):
        ec2, ssh, lines, tries = FakeEc2(("i-0aaa", ONE, state, "zab-spare-1")), FakeSsh(), [], []

        def probe(ips, tries=tries):
            tries.append(list(ips))
            return set(ips) if len(tries) > 2 else set()  # up at the third round

        code = sis.give(
            ec2, roster_rows(ROSTER), SPARES, ["zab-spare-1"], minutes=10, scope=SCOPE,
            region="us-west-2", runner=ssh, probe=probe, log=lines.append, sleep=lambda s: None,
            started=True,
        )  # fmt: skip
        assert code == 0 and len(tries) == 3, (lines, tries)
        assert lines[0] == "zab-spare-1 i-0aaa: idle stop after 10 min (a spare's), installed"
    now, lines = [0.0], []

    def sleep(s):
        now[0] += s

    code = sis.give(
        FakeEc2(("i-0aaa", ONE, "stopped", "zab-spare-1")), roster_rows(ROSTER), SPARES,
        ["zab-spare-1"], minutes=10, scope=SCOPE, region="us-west-2", runner=FakeSsh(),
        probe=lambda ips: set(), log=lines.append, sleep=sleep, clock=lambda: now[0],
        started=True,
    )  # fmt: skip
    assert code == 1 and lines[-1] == "installed 0, skipped 0, failed 1"
    assert lines[0] == f"zab-spare-1 i-0aaa: FAILED: its SSH ({ONE}:22) doesn't answer after 300 s"


def test_started_is_a_flag_of_the_command_line(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(sis, "Ec2", lambda region, config: "ec2")
    monkeypatch.setattr(sis, "give", lambda *a, **kw: seen.update(kw) or 0)
    roster, spares = tmp_path / "roster.csv", tmp_path / "spares.json"
    roster.write_text(ROSTER)
    spares.write_text(json.dumps(SPARES))
    argv = ["--roster", str(roster), "--minutes", "10", "--only", "zab-spare-1"]
    argv += ["--spares-file", str(spares)]
    assert with_scope(sis.main, argv) == 0 and seen["started"] is False
    assert with_scope(sis.main, [*argv, "--started"]) == 0 and seen["started"] is True


def test_a_spare_without_host_keys_is_refused_before_any_ssh():
    ec2 = FakeEc2(("i-0bbb", TWO, "running", "zab-spare-2"))
    ssh = FakeSsh()
    code, lines = give(ec2, ssh, ["zab-spare-2"])
    assert code == 1 and not ssh.calls
    assert lines[0] == (
        "zab-spare-2 i-0bbb: FAILED: no host_keys in spares.json to check its key against: "
        "rerun launch.py"
    )


def test_a_spare_whose_ssh_never_answers_fails_and_the_others_go_on():
    """One deadline for all, then each is tried: the one that answers gets its idle stop."""
    ec2 = FakeEc2(RUNNING, ("i-0ddd", THREE, "pending", "zab-spare-3"))
    spares = dict(SPARES)
    spares["nt-agents-2026/us-west-2/zab-spare-3"] = {
        "name": "zab-spare-3", "ip": THREE, "host_keys": FINGERPRINT,
    }  # fmt: skip
    ssh, lines, tries, now = FakeSsh(), [], [], [0.0]

    def probe(ips):
        tries.append(list(ips))
        return {ONE} & set(ips)

    def sleep(s):
        now[0] += s

    code = sis.give(
        ec2, roster_rows(ROSTER), spares, ["zab-spare-1", "zab-spare-3"], minutes=10,
        scope=SCOPE, region="us-west-2", runner=ssh, probe=probe, log=lines.append,
        sleep=sleep, clock=lambda: now[0],
    )  # fmt: skip
    assert sis.WAIT_S <= now[0] < sis.WAIT_S + 6
    assert code == 1
    assert lines == [
        "zab-spare-1 i-0aaa: idle stop after 10 min (a spare's), installed",
        f"zab-spare-3 i-0ddd: FAILED: its SSH ({THREE}:22) doesn't answer after 300 s",
        "installed 1, skipped 0, failed 1",
    ]
    assert tries[0] == [ONE, THREE] and all(t == [THREE] for t in tries[1:])
    assert not ssh.remotes(THREE)


def test_wait_ssh_has_one_deadline():
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    up = sis.wait_ssh(["a", "b"], probe=lambda ips: set(), clock=lambda: clock[0], sleep=sleep)
    assert up == set() and sis.WAIT_S <= clock[0] < sis.WAIT_S + 6


def test_the_vm_at_the_address_must_be_the_spares():
    ec2 = FakeEc2(("i-0eee", ONE, "running", "bob"))
    code, lines = give(ec2, FakeSsh(), ["zab-spare-1"])
    assert code == 1
    assert lines[0] == (
        f"zab-spare-1 {ONE}: FAILED: the instance at its address is tagged participant=bob"
    )
    code, lines = give(FakeEc2(), FakeSsh(), ["zab-spare-1"])
    assert lines[0].endswith("the instance at its address is no live VM of this scope")


def test_a_dry_run_reads_ec2_and_makes_no_ssh():
    ssh = FakeSsh()
    code, lines = give(FakeEc2(RUNNING), ssh, ["zab-spare-1"], dry_run=True)
    assert code == 0 and not ssh.calls
    assert lines == [
        "zab-spare-1 i-0aaa (running): would install idle stop after 10 min",
        "would install 1, skipped 0, failed 0",
        "dry run: nothing changed",
    ]


def test_a_failed_lookup_is_one_line():
    code, lines = give(FakeEc2(down=True), FakeSsh(), ["zab-spare-1"])
    assert code == 1 and lines == [
        "FAILED: the EC2 lookup: RuntimeError: ExpiredToken: the security token is expired"
    ]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--minutes", "10"], "name the spares"),
        (["--minutes", "10", "--only", "alice"], "name the spares"),
        (["--minutes", "9", "--only", "zab-spare-1"], "--minutes takes 10 to 240"),
        (["--minutes", "241", "--only", "zab-spare-1"], "--minutes takes 10 to 240"),
    ],
)
def test_the_command_line_is_checked(tmp_path, argv, error, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    with pytest.raises(SystemExit):
        with_scope(sis.main, ["--roster", str(roster), *argv])
    assert error in capsys.readouterr().err


def test_a_missing_spares_file_is_refused(tmp_path, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    argv = ["--roster", str(roster), "--minutes", "10", "--only", "zab-spare-1"]
    assert with_scope(sis.main, [*argv, "--spares-file", str(tmp_path / "none.json")]) == 1
    assert "refusing:" in capsys.readouterr().err
