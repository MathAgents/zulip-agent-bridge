"""`workshop/admin/set_workshop.py` (docs/ADMIN.md §2.10; #1043): the plan's dry run and the apply
of `!set-workshop`, with a fake EC2, through the real root helper (`zab.admin.confighelper`), as
`test_new_workshop.py` drives `!new-workshop`'s."""

from __future__ import annotations

import base64
import json

import new_workshop as nw
import pytest
import set_workshop as sw

from zab.admin.config import parse_admin_config

from .test_new_workshop import FakeEc2, server  # noqa: F401 - the fixture

OLD = "ami-0123456789abcdef0"  # the fixture's entry's
NEWEST = "ami-00000000000000001"  # FakeEc2's newer image of two
GIFTS = 'Accept your gifts: Claude Max through the "invitation" e-mail.'


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


@pytest.fixture
def ec2(server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sw, "Ec2", lambda region: server["ec2"])
    return server["ec2"]


def argv(s, *more, was=OLD):
    tmp = s["tmp"]
    record = ("--record", str(record_of(s))) if "newest" in more else ()
    return [
        "--plan", "P-9", "--name", "harvard-2026", "--region", "us-west-2",
        "--security-group", "sg-0123456789abcdef0", "--instance-type", "m7a.2xlarge",
        "--tag", "workshop=harvard-2026", "--scope", "zab-scope=mathagents",
        "--scope", "zab-role=test", "--ami-was", was,
        "--request-dir", str(tmp / "requests"), "--result", str(tmp / "helper-state/result.json"),
        *more, *record,
    ]  # fmt: skip


def record_of(s):
    """Where the bot points `--record`: the plan's own directory under runs/ (review of #1046)."""
    return s["tmp"] / "runs" / "P-9" / "set-workshop.json"


def planned(s, *more, was=OLD):
    """The plan's dry run, as the runner runs it before the apply: it picks `newest`."""
    assert sw.main(argv(s, *more, "--dry-run", was=was)) == 0


def entry(s):
    return parse_admin_config(s["config"].read_text(), roots=s["roots"]).workshop("harvard-2026")


def test_the_dry_run_shows_each_line_old_and_new_and_checks_the_image(server, ec2, capsys):  # noqa: F811
    """Criterion 2, and 1's checks: the newest image, picked as !new-workshop picks it, and a
    DryRun launch of the workshop's instance types from it, in its group, with its tags."""
    before = server["config"].read_text()
    assert (
        sw.main(argv(server, "--ami", "newest", "--gifts-step-b64", b64(GIFTS), "--dry-run")) == 0
    )
    out = capsys.readouterr().out
    assert f'- ami = "{OLD}"\n+ ami = "{NEWEST}"\n  affects: the VMs !launch makes from now' in out
    assert f"- gifts_step: none\n+ gifts_step = {json.dumps(GIFTS)}\n" in out
    assert (
        'affects: "Start here" posted from now on (!provision zulip, !adopt-spare), and the' in out
    )
    scope = "zab-scope=mathagents, zab-role=test"
    assert (
        f"  ok: image {NEWEST} (zab-1, made 2026-10-11T00:00:00.000Z), the region's newest at "
        f"the plan, tagged {scope}" in out
    )
    launch = "the role may launch m7a.2xlarge from it in sg-0123456789abcdef0, in us-west-2"
    assert f"  ok: {launch} (DryRun)" in out and "MISSING" not in out
    ((call, params),) = ec2.dry
    assert call == "run_instances" and params["ImageId"] == NEWEST
    assert params["SecurityGroupIds"] == ["sg-0123456789abcdef0"]
    tags = {t["Key"]: t["Value"] for t in params["TagSpecifications"][0]["Tags"]}
    assert tags == {"zab-scope": "mathagents", "zab-role": "test", "workshop": "harvard-2026"}
    assert server["config"].read_text() == before


@pytest.mark.parametrize(
    ("ami", "code", "line"),
    [
        ("ami-00000000000000000", 0, "ok: image ami-00000000000000000 (zab-0, made 2026-10-10"),
        ("ami-0aaaaaaaaaaaaaaaa", 1, "MISSING: no available image ami-0aaaaaaaaaaaaaaaa tagged"),
    ],
)
def test_a_named_image_must_be_the_regions_in_the_scope(server, ec2, capsys, ami, code, line):  # noqa: F811
    assert sw.main(argv(server, "--ami", ami, "--dry-run")) == code
    assert line in capsys.readouterr().out


def test_a_launch_the_role_may_not_make_refuses_the_plan(server, ec2, capsys):  # noqa: F811
    ec2.codes["run_instances"] = "UnauthorizedOperation"
    assert sw.main(argv(server, "--ami", "newest", "--dry-run")) == 1
    assert "MISSING: the role may launch m7a.2xlarge from it in sg-" in capsys.readouterr().out


@pytest.mark.parametrize("gifts", [(), ("--gifts-step-b64", b64(GIFTS))])
def test_the_newest_that_is_its_image_already_is_refused(server, ec2, capsys, gifts):  # noqa: F811
    """Criterion 1: a change that changes nothing is refused, saying why, alone or not; the
    plan is never offered, and nothing is launched even as a DryRun."""
    assert sw.main(argv(server, "--ami", "newest", *gifts, "--dry-run", was=NEWEST)) == 1
    said = f"MISSING: ami=newest: {NEWEST}, the region's newest image, is harvard-2026's already"
    assert said in capsys.readouterr().out and ec2.dry == []


def test_the_apply_changes_the_entry_through_the_helper(server, ec2, capsys):  # noqa: F811
    planned(server, "--ami", "newest", "--gifts-step-b64", b64(GIFTS))
    assert sw.main(argv(server, "--ami", "newest", "--gifts-step-b64", b64(GIFTS))) == 0
    out = capsys.readouterr().out
    assert (
        "the config helper: harvard-2026's entry changed in admin.toml; admin.toml.bak kept" in out
    )
    ws = entry(server)
    assert ws.aws.ami == NEWEST and ws.gifts_step == GIFTS
    assert not (server["tmp"] / "requests" / "request.json").exists()
    # then removed, the plan saying what it read
    more = ("--gifts-step-was-b64", b64(GIFTS), "--no-gifts-step")
    assert sw.main(argv(server, *more, was=NEWEST)) == 0
    assert entry(server).gifts_step is None and entry(server).aws.ami == NEWEST


def test_an_entry_changed_since_the_plan_is_refused_by_the_helper(server, ec2, capsys):  # noqa: F811
    before = server["config"].read_text()
    planned(server, "--ami", "newest", was="ami-0bbbbbbbbbbbbbbbb")
    assert sw.main(argv(server, "--ami", "newest", was="ami-0bbbbbbbbbbbbbbbb")) == 1
    assert "harvard-2026's entry changed since the plan" in capsys.readouterr().out
    assert server["config"].read_text() == before


def test_a_silent_helper_has_the_change_withdrawn(server, ec2, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr(nw, "WAIT", 0.01)
    monkeypatch.setattr(nw.time, "sleep", lambda seconds: None)
    planned(server, "--ami", "newest")
    assert sw.main(argv(server, "--ami", "newest")) == 1
    out = capsys.readouterr().out
    assert "the request is withdrawn, and nothing was changed" in out
    assert list((server["tmp"] / "requests").iterdir()) == []


@pytest.mark.parametrize(
    "more",
    [
        ("--tag", "zab-role=test", "--ami", "newest"),  # a tag beyond the workshop's
        ("--ami", OLD),  # what it holds already
        ("--ami", "ami-XYZ"),
        ("--no-gifts-step",),  # it has none
        ("--gifts-step-b64", "not base64!"),
        ("--gifts-step-b64", b64("-x")),  # outside the loader's rule
        (),  # nothing to change
    ],
)
def test_arguments_outside_the_rules_are_refused(server, ec2, capsys, more):  # noqa: F811
    assert sw.main(argv(server, *more, "--dry-run")) == 2
    assert capsys.readouterr().err.startswith("set_workshop.py: ")


def test_newest_is_picked_at_the_plan_and_that_image_is_applied(server, ec2, capsys):  # noqa: F811
    """Review of #1046, N2: `ami=newest` sets exactly the image the approver saw. The plan's dry
    run picks it and keeps it in the plan's own directory; an image tagged after it changes
    neither the dry run the runner runs again at apply (so the plan isn't STALE) nor what the
    apply sets, which only says that a newer one exists."""
    planned(server, "--ami", "newest")
    shown = capsys.readouterr().out
    assert json.loads(record_of(server).read_text()) == {"plan": "P-9", "ami": NEWEST}
    assert record_of(server).stat().st_mode & 0o777 == 0o600
    later = dict(ec2.images[-1], ImageId="ami-00000000000000002", Name="zab-2",
                 CreationDate="2026-10-12T00:00:00.000Z")  # fmt: skip
    ec2.images.append(later)
    planned(server, "--ami", "newest")  # the runner's dry run at apply: hashed, the same text
    assert capsys.readouterr().out == shown
    assert sw.main(argv(server, "--ami", "newest")) == 0
    out = capsys.readouterr().out
    assert (
        "note: ami-00000000000000002 (zab-2, made 2026-10-12T00:00:00.000Z) is the region's "
        f"newest image now; {NEWEST}, the plan's, is applied, as approved" in out
    )
    assert entry(server).aws.ami == NEWEST
    launched = {params["ImageId"] for call, params in ec2.dry if call == "run_instances"}
    assert launched == {NEWEST}  # every check was of the plan's image


def test_an_apply_with_no_pick_or_another_plans_is_refused(server, ec2, capsys):  # noqa: F811
    before = server["config"].read_text()
    assert sw.main(argv(server, "--ami", "newest")) == 1
    assert "MISSING: no image was picked at the plan: request it again" in capsys.readouterr().out
    record_of(server).parent.mkdir(parents=True)
    record_of(server).write_text(json.dumps({"plan": "P-8", "ami": NEWEST}))
    assert sw.main(argv(server, "--ami", "newest")) == 1
    assert "is not this plan's pick of an image" in capsys.readouterr().out
    assert sw.main(argv(server, "--ami", "newest", "--dry-run")) == 1  # nor a dry run over it
    assert server["config"].read_text() == before
    assert json.loads(record_of(server).read_text())["plan"] == "P-8"


def test_a_record_goes_with_newest_alone(server, ec2, capsys):  # noqa: F811
    more = ("--ami", "ami-00000000000000000", "--record", str(record_of(server)), "--dry-run")
    assert sw.main(argv(server, *more)) == 2
    assert "--record goes with --ami newest" in capsys.readouterr().err
    args = argv(server, "--ami", "newest", "--dry-run")
    del args[args.index("--record") : args.index("--record") + 2]
    assert sw.main(args) == 2
