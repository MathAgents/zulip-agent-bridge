"""`workshop/aws/adopt_spare.py` (ORGANIZERS §7.2): a spare VM for a late joiner (PM on issue
#36, Wed 21:20), against a fake EC2 and a fake port 22 (`wait_for_ssh`)."""

from __future__ import annotations

import json
import stat

import admin_common as ac_admin
import adopt_spare as ad
import aws_common as ac
import pytest

ROSTER = """slug,first_name,last_name,email,repo,host,ip,region,host_keys,zulip_user_id,notes
alice,Alice,Smith,alice@example.org,,ws-alice,203.0.113.5,us-west-2,SHA256:a,101,
late,Lena,Late,lena@example.org,,,,,,,joined Wednesday
"""
SPARE = "zab-spare-7"
KEY = f"nt-agents-2026/us-west-2/{SPARE}"


def tags(name: str) -> list[dict[str, str]]:
    return ac.tag_list({"Name": f"ws-{name}", "workshop": "nt-agents-2026", "participant": name})


class FakeEc2:
    def __init__(self) -> None:
        self.instances = [
            {
                "InstanceId": "i-spare7",
                "State": {"Code": 16, "Name": "running"},
                "Tags": tags(SPARE),
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-7"}}],
            }
        ]
        self.addresses = [
            {
                "AllocationId": "eipalloc-7",
                "PublicIp": "198.51.100.7",
                "InstanceId": "i-spare7",
                "Tags": tags(SPARE),
            }
        ]
        self.tagged: list[tuple[list[str], dict[str, str]]] = []
        self.events: list[tuple] = []  # the hold's steps, and the retag, in order
        self.ssh_up = True  # whether the spare's port 22 answers
        self.probed: list[tuple[str, float]] = []  # wait_for_ssh's calls: (ip, timeout_s)
        self.started: list[list[str]] = []
        #: the states a started spare goes through, one per lookup after the start (#715)
        self.boot = ["stopped", "pending", "running"]
        self.start_fails = ""

    def wait_for_ssh(self, ip, *, timeout_s):
        self.probed.append((ip, timeout_s))
        return self.ssh_up

    #: the spare's SSH, for the hold: `pinned`'s answer, and the hold's (`held`)
    pin_fails, hold_fails, system = "", "", "degraded"  # is-system-running's answer
    resume_fails, tag_fails = "", ""  # the timer started again; the retag (review of #729)

    def pinned(self, ip, printed, runner, folder):
        self.events.append(("pinned", ip, printed))
        return ("", self.pin_fails) if self.pin_fails else (f"{folder}/known_hosts", "")

    def ssh(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.events.append(("ssh", argv[-3], argv[-1]))
        if argv[-1] == ad.RESUME_CMD:
            return ac_admin.RunResult(1 if self.resume_fails else 0, "", self.resume_fails)
        said = "" if self.hold_fails else f"{self.system}\n"
        return ac_admin.RunResult(1 if self.hold_fails else 0, said, self.hold_fails)

    def start(self, ids):
        if self.start_fails:
            raise RuntimeError(self.start_fails)
        self.started.append(list(ids))

    def tick(self):
        """One lookup's worth of a started spare's boot."""
        if self.started and self.boot:
            self.instances[0]["State"] = {"Name": self.boot.pop(0)}

    @staticmethod
    def _match(resource, wanted):
        have = ac.tags_of(resource)
        return all(have.get(k) == v for k, v in wanted.items())

    def find_instances(self, wanted, states, **bound):
        self.tick()
        return [i for i in self.instances if self._match(i, wanted)]

    def find_addresses(self, wanted):
        return [a for a in self.addresses if self._match(a, wanted)]

    def create_tags(self, ids, new):
        if self.tag_fails:
            raise RuntimeError(self.tag_fails)
        self.tagged.append((list(ids), dict(new)))
        self.events.append(("retag",))
        for r in self.instances + self.addresses:
            if r.get("InstanceId") in ids or r.get("AllocationId") in ids:
                merged = {**ac.tags_of(r), **new}
                r["Tags"] = ac.tag_list(merged)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    spares = tmp_path / "spares.json"
    entry = {
        "name": SPARE,
        "workshop": "nt-agents-2026",
        "region": "us-west-2",
        "instance_id": "i-spare7",
        "ip": "198.51.100.7",
        "host_keys": "SHA256:s7a SHA256:s7b",
        "updated": "2026-10-07T12:00:00Z",
    }
    spares.write_text(json.dumps({KEY: entry}))
    ec2 = FakeEc2()
    monkeypatch.setattr(ad, "Ec2", lambda region, **config: ec2)
    monkeypatch.setattr(ad, "wait_for_ssh", ec2.wait_for_ssh)
    # the hold before the retag (Codex round 2 on #729): the spare's key pinned, then the hold
    monkeypatch.setattr(ad, "pinned", ec2.pinned)
    monkeypatch.setattr(ad, "subprocess_runner", ec2.ssh)
    argv = ["--roster", str(roster), "--slug", "late", "--spare", SPARE]
    return roster, spares, ec2, [*argv, "--spares-file", str(spares)]


def test_a_spare_is_retagged_and_the_row_filled(setup, capsys):
    roster, spares, ec2, argv = setup
    assert ad.main(argv) == 0
    assert ec2.probed == [("198.51.100.7", ad.SSH_WAIT_S)]  # its port 22, before the retag
    # instance, volume and Elastic IP now carry the participant's tags; the request names only
    # `Name` and `participant`, never the unchanged `workshop` (ADMIN.md §5's aws:TagKeys)
    assert ec2.tagged == [
        (["i-spare7", "vol-7", "eipalloc-7"], {"Name": "ws-late", "participant": "late"})
    ]
    for res in ec2.instances + ec2.addresses:  # the scope tag is still there
        want = {"Name": "ws-late", "workshop": "nt-agents-2026", "participant": "late"}
        assert ac.tags_of(res) == want
    row = next(r for r in ac.read_roster(str(roster)) if r["slug"] == "late")
    assert (row["host"], row["ip"], row["region"]) == ("ws-late", "198.51.100.7", "us-west-2")
    assert row["host_keys"] == "SHA256:s7a SHA256:s7b"
    assert row["notes"] == "joined Wednesday" and row["email"] == "lena@example.org"
    alice = next(r for r in ac.read_roster(str(roster)) if r["slug"] == "alice")
    assert alice["ip"] == "203.0.113.5"  # other rows untouched
    data = json.loads(spares.read_text())
    assert data[KEY]["adopted_by"] == "late" and data[KEY]["ip"] == "198.51.100.7"
    assert stat.S_IMODE(spares.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "--adopt-spare late" in out and "scan_hosts.py --only late" in out
    # never twice
    assert ad.main([*argv, "--force"]) == 1
    assert "already given to late" in capsys.readouterr().err


def test_the_spares_idle_stop_is_held_before_the_retag(setup, capsys):
    """Codex round 2 on #729: a spare stops itself once idle, and nothing of the hand-over
    connects to it before push_config.py's hold. So the apply holds it first, over SSH, its key
    pinned to spares.json's fingerprints, then retags it; a dry run holds nothing."""
    _roster, _spares, ec2, argv = setup
    assert ad.main([*argv, "--dry-run"]) == 0 and not ec2.events
    capsys.readouterr()
    assert ad.main(argv) == 0
    assert ec2.events == [
        ("pinned", "198.51.100.7", "SHA256:s7a SHA256:s7b"),
        ("ssh", "ubuntu@198.51.100.7", ad.HOLD_CHECK_CMD),
        ("retag",),
    ]
    assert "zab-spare-7: its idle stop held for the hand-over" in capsys.readouterr().out


def test_a_spare_whose_key_cant_be_pinned_is_refused_with_nothing_changed(setup, capsys):
    roster, spares, ec2, argv = setup
    ec2.pin_fails = "its key SHA256:x is not one its console printed: nothing installed"
    before = (roster.read_text(), spares.read_text())
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("refusing: zab-spare-7's idle stop could not be held for the hand-over")
    assert "its key" in err and "nothing changed; request again in a minute" in err
    assert unchanged(roster, spares, ec2, before) and [e[0] for e in ec2.events] == ["pinned"]


@pytest.mark.parametrize(
    ("resume", "said"),
    [
        ("", "its idle stop may have been paused, and was started again"),
        ("Connection reset", "its idle stop may be paused, and could not be started again "
         "(Connection reset): `ssh ubuntu@198.51.100.7 sudo systemctl start "
         "zab-idle-stop.timer` starts it, or the spare's next `!start`"),
    ],
)  # fmt: skip
def test_a_hold_whose_ssh_drops_resumes_the_spares_idle_stop(setup, capsys, resume, said):
    """Review of #729: an SSH that drops after the hold command ran may leave the spare's
    idle stop paused (the quiesce), on a spare that is still a spare: its timer is started
    again, best effort, over the pinned SSH; the refusal says what happened, never "nothing
    changed", and nothing is retagged."""
    roster, spares, ec2, argv = setup
    ec2.hold_fails, ec2.resume_fails = "Connection to 198.51.100.7 closed by remote host.", resume
    before = (roster.read_text(), spares.read_text())
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert err == (
        "refusing: zab-spare-7's idle stop could not be held for the hand-over (ssh: Connection "
        f"to 198.51.100.7 closed by remote host.); {said}: request again in a minute\n"
    )
    assert "nothing changed" not in err and unchanged(roster, spares, ec2, before)
    assert ("ssh", "ubuntu@198.51.100.7", ad.RESUME_CMD) in ec2.events


def test_a_retag_that_fails_resumes_the_spares_idle_stop(setup, capsys):
    """Review of #729: `create_tags` raising after the quiesce leaves a spare (no tag changed,
    or EC2 to check) whose idle stop is paused: started again, and the refusal says so."""
    roster, spares, ec2, argv = setup
    ec2.tag_fails = "RequestLimitExceeded: Request limit exceeded."
    before = (roster.read_text(), spares.read_text())
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert err == (
        "refusing: the retag failed (RuntimeError: RequestLimitExceeded: Request limit "
        "exceeded.): check zab-spare-7's tags in EC2; its idle stop may have been paused, and "
        "was started again: request again in a minute\n"
    )
    assert (roster.read_text(), spares.read_text()) == before
    assert ec2.events[-1] == ("ssh", "ubuntu@198.51.100.7", ad.RESUME_CMD)


def test_a_spare_already_shutting_down_is_refused_before_the_retag(setup, capsys):
    """Review of #728: a hold written as the spare's idle stop powers it off comes too late;
    `systemctl is-system-running` then says `stopping`, and nothing is retagged. Any other
    state (`degraded` is common on a VM, and makes the command exit non-zero) goes on."""
    roster, spares, ec2, argv = setup
    ec2.system = "stopping"
    before = (roster.read_text(), spares.read_text())
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert err == (
        "refusing: zab-spare-7 is shutting down: its idle stop powered it off just now, and its "
        "next boot starts it again; nothing else changed: request again in a minute or two\n"
    )
    assert unchanged(roster, spares, ec2, before)
    assert ad.HOLD_CHECK_CMD == (
        "echo $(( $(date +%s) + 3600 )) | sudo tee /run/zab-idle-stop.hold >/dev/null && "
        "{ sudo systemctl stop zab-idle-stop.timer zab-idle-stop.service 2>/dev/null; true; } && "
        "{ systemctl is-system-running || true; }"
    )  # the hold, then idle stop quiesced (Codex round 3 on #729), then the state
    ec2.system = "degraded"
    assert ad.main(argv) == 0 and ec2.tagged


def test_a_spare_without_host_keys_has_no_idle_stop_to_hold(setup, capsys):
    """spare_idle_stop.py refuses a spare without host_keys, so it has no timer: no hold, no
    SSH, and the hand-over goes on as before, with the warning of an unverified key."""
    _roster, spares, ec2, argv = setup
    data = json.loads(spares.read_text())
    data[KEY]["host_keys"] = ""
    spares.write_text(json.dumps(data))
    assert ad.main(argv) == 0 and ec2.events == [("retag",)]
    assert "warning: zab-spare-7 has no host_keys in spares.json, nor its own idle stop: none " \
        "held" in capsys.readouterr().out  # fmt: skip


def test_a_dry_run_changes_nothing(setup, capsys):
    roster, spares, ec2, argv = setup
    before = (roster.read_text(), spares.read_text())
    assert ad.main([*argv, "--dry-run"]) == 0
    assert not ec2.tagged and (roster.read_text(), spares.read_text()) == before
    assert "dry run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change,message",
    [
        (
            lambda r, s, e: e.instances.__setitem__(0, {**e.instances[0], "InstanceId": "i-x"}),
            "EC2 has i-x tagged zab-spare-7",
        ),
        (
            lambda r, s, e: e.addresses[0].update(PublicIp="198.51.100.99"),
            "Elastic IP is 198.51.100.99",
        ),
        (
            lambda r, s, e: e.instances.append({"InstanceId": "i-late", "Tags": tags("late")}),
            "already tagged participant=late: i-late",
        ),
        (  # the admin server, tagged as the spare by mistake, is never retagged
            lambda r, s, e: e.instances[0]["Tags"].append({"Key": "zab-admin", "Value": "nt"}),
            "EC2 has none tagged zab-spare-7, spares.json i-spare7",
        ),
        (
            lambda r, s, e: e.addresses[0]["Tags"].append({"Key": "zab-admin", "Value": "nt"}),
            "Elastic IP is none, spares.json says 198.51.100.7",
        ),
        (lambda r, s, e: s.write_text("{}"), "no spare nt-agents-2026/us-west-2/zab-spare-7"),
        (
            lambda r, s, e: r.write_text(ROSTER.replace("late,Lena", "later,Lena")),
            "no row 'late' in the roster",
        ),
        (
            lambda r, s, e: r.write_text(
                ROSTER.replace(",,,,,,,joined", ",ws-late,1.2.3.4,,,,joined")
            ),
            "late already has a VM",
        ),
    ],
)
def test_every_check_refuses_without_changing_anything(setup, capsys, change, message):
    roster, spares, ec2, argv = setup
    change(roster, spares, ec2)
    before = (roster.read_text(), spares.read_text())
    assert ad.main(argv) == 1
    assert message in capsys.readouterr().err
    assert not ec2.tagged and (roster.read_text(), spares.read_text()) == before


def unchanged(roster, spares, ec2, before) -> bool:
    return not ec2.tagged and (roster.read_text(), spares.read_text()) == before


@pytest.mark.parametrize(
    ("state", "said"),
    [
        ("stopping", "zab-spare-7 is stopping: request again in a minute or two, once it has "
         "stopped (this plan then starts it)"),
        ("pending", "zab-spare-7 is starting (pending): request again in a minute"),
    ],
)  # fmt: skip
@pytest.mark.parametrize("dry_run", [True, False])
def test_a_spare_stopping_or_pending_is_refused_with_the_wait_it_needs(
    setup, capsys, state, said, dry_run
):
    """#455, #715: the plan (`--dry-run`) and the apply refuse a spare on its way, in one line
    that says how long to wait, before any change and without trying its SSH or starting it."""
    roster, spares, ec2, argv = setup
    ec2.instances[0]["State"] = {"Code": 80, "Name": state}
    before = (roster.read_text(), spares.read_text())
    assert ad.main([*argv, "--dry-run"] if dry_run else argv) == 1
    out, err = capsys.readouterr()
    assert err == f"refusing: {ad.not_ready(SPARE, state)}\n" == f"refusing: {said}\n" and not out
    assert unchanged(roster, spares, ec2, before) and not ec2.probed and not ec2.started


def test_a_spare_with_no_state_is_refused(setup, capsys):
    _roster, _spares, ec2, argv = setup
    del ec2.instances[0]["State"]
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert "zab-spare-7 is in an unknown state, neither running nor stopped" in err
    assert not ec2.tagged and not ec2.started


@pytest.fixture
def no_wait(monkeypatch):
    """`start_spare`'s clock, moved by its sleeps alone; every port 22 answers but in `quiet`."""
    now, quiet = [0.0], set()

    def sleep(s):
        now[0] += s

    def start_spare(ec2, spare, iid, ip, workshop, **kw):
        kw = {"answers": lambda ip: ip not in quiet, "sleep": sleep, "clock": lambda: now[0]}
        return real(ec2, spare, iid, ip, workshop, **kw)

    real = ad.start_spare
    monkeypatch.setattr(ad, "start_spare", start_spare)
    return now, quiet


def test_a_stopped_spare_is_started_for_the_adoption(setup, capsys, no_wait):
    """#715: the plan says so, and the apply starts it, waits until it runs and answers, and
    only then adopts it as before."""
    _roster, spares, ec2, argv = setup
    ec2.instances[0]["State"] = {"Code": 80, "Name": "stopped"}
    assert ad.main([*argv, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert (
        out.splitlines()[0] == "zab-spare-7: stopped, started for the adoption" == ad.started(SPARE)
    )
    assert not ec2.started and not ec2.probed and not ec2.tagged  # the dry run starts nothing
    assert ad.main(argv) == 0
    out = capsys.readouterr().out
    assert ec2.started == [["i-spare7"]]
    assert "zab-spare-7: started; it runs, and its SSH answers" in out
    assert ec2.tagged == [
        (["i-spare7", "vol-7", "eipalloc-7"], {"Name": "ws-late", "participant": "late"})
    ]
    assert json.loads(spares.read_text())[KEY]["adopted_by"] == "late"
    assert no_wait[0][0] < ad.START_WAIT_S


@pytest.mark.parametrize(
    ("boot", "quiet", "said"),
    [
        (["stopped"] * 100, False, "was started, but it is still stopped after 300 s"),
        (["running"] * 100, True, "was started, but its SSH (198.51.100.7:22) doesn't answer "
         "after 300 s: request again in a minute"),
        (["stopping"], False, "was started, but it is stopping now: check it in EC2"),
    ],
)  # fmt: skip
def test_a_started_spare_that_doesnt_come_up_is_refused_before_the_retag(
    setup, capsys, no_wait, boot, quiet, said
):
    """The wait's bound, `START_WAIT_S`: after it, a `refusing:` line, and nothing changed
    but the start; the spare's own idle stop stops it again."""
    roster, spares, ec2, argv = setup
    ec2.instances[0]["State"] = {"Code": 80, "Name": "stopped"}
    ec2.boot = list(boot)
    if quiet:
        no_wait[1].add("198.51.100.7")
    before = (roster.read_text(), spares.read_text())
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("refusing: zab-spare-7 ") and said in err, err
    assert ec2.started == [["i-spare7"]] and unchanged(roster, spares, ec2, before)
    if len(boot) > 1:
        assert ad.START_WAIT_S <= no_wait[0][0] < ad.START_WAIT_S + ad.START_POLL_S + 1


def test_a_start_that_ec2_refuses_is_refused(setup, capsys, no_wait):
    _roster, _spares, ec2, argv = setup
    ec2.instances[0]["State"] = {"Code": 80, "Name": "stopped"}
    ec2.start_fails = "IncorrectInstanceState: the instance is not in a state to start"
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("refusing: zab-spare-7 could not be started: RuntimeError: Incorrect")
    assert not ec2.tagged


def test_the_wait_is_bounded_from_before_the_start_and_each_lookup_by_it(setup, monkeypatch):
    """Codex round 1 on #731: the deadline counts from before the start request, a slow one
    included; each lookup gets it (no page past it), and one that fails or runs past it is a
    refusal before the retag; the client's every request is bounded (`power.EC2_CONFIG`)."""
    _roster, _spares, ec2, argv = setup
    now, seen = [0.0], []

    def slow_start(ids):
        now[0] += 250.0  # a request that took 250 s of the 300

    def lookup(wanted, states, *, deadline=None, clock=None):
        seen.append(deadline)
        raise TimeoutError("the lookup's pages ran past the wait's deadline")

    slow = FakeEc2()
    slow.start, slow.find_instances = slow_start, lookup
    with pytest.raises(ad.AdoptError, match=r"was started, but the EC2 lookup failed: .*Timeout"):
        ad.start_spare(slow, SPARE, "i-spare7", "198.51.100.7", ac.parse_tags(None),
                       answers=lambda ip: True, sleep=lambda s: None,
                       clock=lambda: now[0])  # fmt: skip
    assert seen == [ad.START_WAIT_S]  # from before the start: 300, not 250 + 300
    made = {}
    monkeypatch.setattr(ad, "Ec2", lambda region, **config: made.update(config) or ec2)
    assert ad.main([*argv, "--dry-run"]) == 0
    assert made == {"config": ad.EC2_CONFIG}


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_running_spare_whose_ssh_doesnt_answer_is_refused(setup, capsys, dry_run):
    """#455: `scan_hosts.py`, the chain's next step, connects to the spare: a running spare whose
    port 22 doesn't answer yet (just started) is refused too, before any change."""
    roster, spares, ec2, argv = setup
    ec2.ssh_up = False
    before = (roster.read_text(), spares.read_text())
    assert ad.main([*argv, "--dry-run"] if dry_run else argv) == 1
    err = capsys.readouterr().err
    assert err == f"refusing: {ad.no_ssh(SPARE, '198.51.100.7')}\n"
    assert "198.51.100.7:22" in err and "request again in a minute" in err
    assert unchanged(roster, spares, ec2, before)
    assert ec2.probed == [("198.51.100.7", ad.SSH_WAIT_S)]


def test_the_apply_checks_again_a_spare_stopped_since_the_plan(setup, capsys, no_wait):
    """#455, #715: the plan passes while the spare runs; stopped by itself before the approval,
    the apply's first step starts it, then adopts it; stopping then, it refuses it before the
    retag, so the chain stops with nothing changed."""
    roster, spares, ec2, argv = setup
    before = (roster.read_text(), spares.read_text())
    assert ad.main([*argv, "--dry-run"]) == 0
    capsys.readouterr()
    ec2.instances[0]["State"] = {"Code": 80, "Name": "stopping"}
    assert ad.main(argv) == 1
    assert "zab-spare-7 is stopping: request again" in capsys.readouterr().err
    assert unchanged(roster, spares, ec2, before) and not ec2.started
    ec2.instances[0]["State"] = {"Code": 80, "Name": "stopped"}
    assert ad.main(argv) == 0
    assert ec2.started == [["i-spare7"]] and ec2.tagged


def test_the_other_checks_come_first(setup, capsys):
    """A stopped spare that is wrong in another way is refused for that: starting it would not
    help."""
    _roster, _spares, ec2, argv = setup
    ec2.instances[0]["State"] = {"Code": 80, "Name": "stopped"}
    ec2.addresses[0].update(PublicIp="198.51.100.99")
    assert ad.main(argv) == 1
    err = capsys.readouterr().err
    assert "Elastic IP is 198.51.100.99" in err and "!start" not in err


def test_launch_then_finds_the_vm_as_the_rows(setup):
    """After the hand-over, launch.py's lookups by the slug's tags find the instance and its
    Elastic IP (so it neither launches a second VM nor moves the address)."""
    import launch

    _roster, _spares, ec2, argv = setup
    assert ad.main(argv) == 0
    wanted = launch.lookup_tags("late")
    assert [i["InstanceId"] for i in ec2.find_instances(wanted, ad.STATES)] == ["i-spare7"]
    assert [a["AllocationId"] for a in ec2.find_addresses(wanted)] == ["eipalloc-7"]
    assert ec2.find_instances(launch.lookup_tags(SPARE), ad.STATES) == []


def test_the_retag_never_names_a_scope_tag(tmp_path, monkeypatch):
    """With several scope tags (the test rig) the request still names only `Name` and
    `participant`: IAM checks every key of a `CreateTags` request (ADMIN.md §3.2, §5)."""
    scope = {"zab-role": "test", "workshop": "zab-test"}
    ec2 = FakeEc2()
    for res in ec2.instances + ec2.addresses:
        res["Tags"] = ac.tag_list({"Name": f"ws-{SPARE}", **scope, "participant": SPARE})
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    spares = tmp_path / "spares.json"
    key = f"{ac.scope_label(scope)}/us-west-2/{SPARE}"
    spares.write_text(json.dumps({key: {"instance_id": "i-spare7", "ip": "198.51.100.7"}}))
    monkeypatch.setattr(ad, "Ec2", lambda region, **config: ec2)
    monkeypatch.setattr(ad, "wait_for_ssh", ec2.wait_for_ssh)
    argv = ["--roster", str(roster), "--slug", "late", "--spare", SPARE]
    argv += ["--spares-file", str(spares), "--tag", "zab-role=test", "--tag", "workshop=zab-test"]
    assert ad.main(argv) == 0
    ((ids, sent),) = ec2.tagged
    assert ids == ["i-spare7", "vol-7", "eipalloc-7"]
    assert sent == {"Name": "ws-late", "participant": "late"} and ad.RETAG_KEYS == tuple(sent)
    for res in ec2.instances + ec2.addresses:
        assert ac.tags_of(res) == {"Name": "ws-late", **scope, "participant": "late"}


def test_a_row_that_kept_the_spares_address_has_its_ip_cleared(setup, capsys):
    """#604: EC2 gave the spare an Elastic IP released with alice's VM, which her row kept. The
    hand-over clears alice's `ip` (her `host` stays), so that the roster never has two rows at
    one address, which every admin script refuses."""
    roster, _, _, argv = setup
    roster.write_text(ROSTER.replace("203.0.113.5", "198.51.100.7"))
    before = roster.read_text()
    assert ad.main([*argv, "--dry-run"]) == 0 and roster.read_text() == before
    said = "roster: alice's ip 198.51.100.7 cleared: released with its own VM, it is zab-spare-7's"
    assert said in capsys.readouterr().out
    assert ad.main(argv) == 0
    rows = {r["slug"]: r for r in ac.read_roster(str(roster))}
    assert (rows["late"]["ip"], rows["alice"]["ip"]) == ("198.51.100.7", "")
    assert rows["alice"]["host"] == "ws-alice" and rows["alice"]["zulip_user_id"] == "101"
    assert said in capsys.readouterr().out


def test_a_stale_rows_ip_fixed_meanwhile_is_not_cleared(setup, monkeypatch, capsys):
    """#611's criterion 5: the `expect` guard. The plan read alice at the spare's address, kept
    from her released VM; by the write the file has another `ip` for her (fixed by hand
    meanwhile), which stays. Without the guard, the write would clear it."""
    roster, _, _, argv = setup
    read = ad.read_roster  # what the script read: alice at the spare's address
    monkeypatch.setattr(ad, "read_roster", lambda path: [
        {**r, "ip": "198.51.100.7"} if r["slug"] == "alice" else r for r in read(path)
    ])  # fmt: skip
    roster.write_text(ROSTER.replace("203.0.113.5", "203.0.113.55"))  # the file, by then
    assert ad.main(argv) == 0
    rows = {r["slug"]: r for r in ac.read_roster(str(roster))}
    assert (rows["late"]["ip"], rows["alice"]["ip"]) == ("198.51.100.7", "203.0.113.55")


@pytest.mark.parametrize(
    "keys, why",
    [
        ("SHA256:s7a", "alice holds zab-spare-7's address 198.51.100.7 and host keys: "
                       "zab-spare-7 was handed over to alice by hand"),
        ("", "alice holds zab-spare-7's address 198.51.100.7, and the host keys can't tell why"),
    ],
)  # fmt: skip
def test_a_spare_handed_over_by_hand_is_never_given_again(setup, capsys, keys, why):
    """Review of #606: the hand-over by hand (ORGANIZERS §7.2, "A broken VM") gave alice
    zab-spare-7's address and host keys, and left the VM tagged as the spare, unmarked in
    spares.json. The VM is alice's: giving it to late is refused before any change, at the dry run
    too. Without fingerprints on her row the two cases can't be told apart, and that is refused
    too."""
    roster, spares, ec2, argv = setup
    roster.write_text(
        ROSTER.replace("203.0.113.5,us-west-2,SHA256:a", f"198.51.100.7,us-west-2,{keys}")
    )
    before = (roster.read_text(), spares.read_text())
    for extra in (["--dry-run"], []):
        assert ad.main([*argv, *extra]) == 1
        assert why in capsys.readouterr().err
    assert not ec2.tagged and (roster.read_text(), spares.read_text()) == before
