"""`workshop/admin/new_workshop.py` (docs/ADMIN.md §2.9; #418): the plan's dry run and the apply
of `!new-workshop`, with a fake Zulip and a fake EC2, through the real root helper
(`zab.admin.confighelper`), which takes the script's request as the server's would."""

from __future__ import annotations

import base64
import json
import os

import new_workshop as nw
import pytest

from zab.admin import confighelper
from zab.admin.config import Roots, parse_admin_config

_HEAD = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY = "ssh-ed25519 " + base64.b64encode(_HEAD + bytes(range(32))).decode()
SCOPE = {"zab-scope": "mathagents", "zab-role": "test"}
DREW, ALICE, OLGA, PROVISIONER, BOT = 101, 102, 103, 150, 903
BOT_KEY = "B" * 32
SITE = "https://example.zulipchat.com"


class FakeAdmin:
    """`zulip_provision.ZulipAdmin`'s calls, as the provisioner (user 150)."""

    def __init__(self) -> None:
        self.site = SITE
        self.people = {
            DREW: {"user_id": DREW, "full_name": "Drew", "is_bot": False, "is_active": True},
            ALICE: {"user_id": ALICE, "full_name": "Alice", "is_bot": False, "is_active": True},
            OLGA: {"user_id": OLGA, "full_name": "Olga", "is_bot": False, "is_active": True},
            PROVISIONER: {"user_id": PROVISIONER, "full_name": "zab-provisioner",
                          "is_bot": False, "is_active": True},
        }  # fmt: skip
        self.own_bots: list[dict] = []
        self.streams_seen: list[dict] = []
        self.created: list[tuple] = []
        self.channels: list[dict] = []
        self.subscribed: list[dict] = []  # the provisioner's own channels (#627)

    def me(self):
        return {"user_id": PROVISIONER, "full_name": "zab-provisioner"}

    def user(self, uid):
        if uid not in self.people:
            raise nw.ZulipAdminError("BAD_REQUEST", 400, "No such user")
        return self.people[uid]

    def users(self):
        return list(self.people.values())

    def bots(self):
        return self.own_bots

    def streams(self):
        return self.streams_seen

    def subscriptions(self):
        return self.subscribed

    def create_bot(self, full, short):
        self.created.append((full, short))
        email = f"{short}-bot@example.zulipchat.com"
        self.people[BOT] = {"user_id": BOT, "full_name": full, "is_bot": True,
                            "is_active": True, "email": email}  # fmt: skip
        self.own_bots.append({"username": email, "full_name": full, "api_key": BOT_KEY})
        return {"user_id": BOT, "api_key": BOT_KEY}

    def request(self, method, endpoint, **params):
        assert (method, endpoint) == ("POST", "channels/create")
        self.channels.append(params)
        made = {"stream_id": 700 + len(self.channels), "name": params["name"],
                "invite_only": params["invite_only"]}  # fmt: skip
        self.streams_seen.append(made)
        if PROVISIONER in params["subscribers"]:  # it sees its own, private ones too
            self.subscribed.append(made)
        return {"id": made["stream_id"]}

    def close(self):
        pass


class FakeEc2:
    def __init__(self, groups=1, images=2, launch="DryRunOperation", key="DryRunOperation"):
        tags = [{"Key": k, "Value": v} for k, v in SCOPE.items()]
        self.groups = [{"GroupId": f"sg-0000000000000000{i}", "Tags": tags} for i in range(groups)]
        self.images = [
            {"ImageId": f"ami-0000000000000000{i}", "Name": f"zab-{i}", "Tags": tags,
             "CreationDate": f"2026-10-1{i}T00:00:00.000Z"}
            for i in range(images)
        ]  # fmt: skip
        self.codes = {"run_instances": launch, "import_key_pair": key}
        self.dry: list[tuple[str, dict]] = []
        self.pairs: dict[str, dict] = {}

    def find_security_groups(self, tags, name=None):
        assert tags == SCOPE and name == "zab-fleet"
        return self.groups

    def find_images(self, tags):
        assert tags == SCOPE
        return self.images

    def dry_run(self, call, **params):
        self.dry.append((call, params))
        return self.codes[call]

    def key_pair(self, name):
        return self.pairs.get(name)

    def import_key_pair(self, name, public_key, tags):
        self.pairs[name] = {"KeyName": name, "PublicKey": public_key,
                            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}  # fmt: skip


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A server tree: admin.toml with one workshop, the helper's directories, the fleet key."""
    etc, ws = tmp_path / "etc", tmp_path / "ws"
    (etc / "secrets" / "bots").mkdir(parents=True)
    (etc / "break-glass").mkdir()
    (etc / "break-glass" / "drew.pub").write_text(KEY + "\n")
    for path in (etc, etc / "break-glass", etc / "break-glass" / "drew.pub"):  # as deployed
        path.chmod(0o644 if path.is_file() else 0o755)  # the helper checks them as the bots do
    (ws / "harvard-2026").mkdir(parents=True)
    config = etc / "admin.toml"
    config.write_text(f"""provider = "aws"
admins = [{DREW}, {ALICE}]
deployers = [{DREW}]

[global]
scope = "mathagents"
regions = ["us-west-2", "us-east-2"]
instance_types = ["m7a.2xlarge"]
tags = ["zab-role=test"]
zuliprc = "{etc}/secrets/global.zuliprc"
user_id = 900
channels = [41]

[[workshop]]
name = "harvard-2026"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "{ws}/harvard-2026/roster.csv"
out_dir = "{ws}/harvard-2026/out"
organizers = [{OLGA}]
[workshop.bot]
zuliprc = "{etc}/secrets/bots/harvard-2026.zuliprc"
user_id = 901
channels = [42]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-harvard-2026"
admin_keys = ["{etc}/break-glass/drew.pub"]
""")
    for path in ("requests", "helper-state"):
        (tmp_path / path).mkdir(mode=0o700)
    (tmp_path / "fleet.pub").write_text(KEY + " zab-admin@server\n")
    (tmp_path / "provisioner.zuliprc").write_text(
        f"[api]\nemail=p@x.org\nkey={'P' * 32}\nsite={SITE}\n"
    )
    roots = Roots(
        workshops=str(ws), secrets=str(etc / "secrets"), break_glass=str(etc / "break-glass")
    )
    helper = confighelper.Paths(str(tmp_path / "requests"), str(config),
                                str(tmp_path / "helper-state"), roots)  # fmt: skip
    admin, ec2 = FakeAdmin(), FakeEc2()
    monkeypatch.setattr(nw, "ZulipAdmin", lambda *args: admin)
    monkeypatch.setattr(nw, "Ec2", lambda region: ec2)

    def the_helper_runs(seconds):  # the path unit's start, as root would run it
        confighelper.run(helper, os.getuid(), os.getgid())

    monkeypatch.setattr(nw.time, "sleep", the_helper_runs)
    return {"tmp": tmp_path, "etc": etc, "ws": ws, "config": config, "roots": roots,
            "admin": admin, "ec2": ec2}  # fmt: skip


def argv(s, *more):
    tmp, etc = s["tmp"], s["etc"]
    return [
        "--plan", "P-9", "--name", "mit-2027", "--region", "us-east-2",
        "--zuliprc", str(tmp / "provisioner.zuliprc"),
        "--tag", "workshop=mit-2027",
        "--scope", "zab-scope=mathagents", "--scope", "zab-role=test",
        "--instance-type", "m7a.2xlarge", "--github-org", "MathAgents",
        "--refserve-url", "https://files.mathagents.org",
        "--admin-key", f"{etc}/break-glass/drew.pub",
        "--organizer", str(OLGA), "--admin", str(DREW), "--admin", str(ALICE),
        "--ws-root", str(s["ws"]), "--secrets", f"{etc}/secrets",
        "--public-key", str(tmp / "fleet.pub"),
        "--request-dir", str(tmp / "requests"), "--result", str(tmp / "helper-state/result.json"),
        *more,
    ]  # fmt: skip


def test_the_dry_run_shows_the_entry_the_people_and_the_prerequisites(server, capsys):
    before = server["config"].read_text()
    assert nw.main(argv(server, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert 'name = "mit-2027"' in out and "\ntag = " not in out  # the scope is [global]'s
    assert 'ami = "ami-00000000000000001"' in out  # the newest of the two
    assert 'security_group = "sg-00000000000000000"' in out
    assert 'user_id = "<new>"' in out and 'alerts_channel = "<new>"' in out
    assert f"  {OLGA}  Olga" in out and f"  {ALICE}  Alice" in out
    assert "#mit-2027-organizers (private): the organizers, the global admins, the bot" in out
    assert (
        "#mit-2027-fleet-alerts (private): the organizers, the global admins, zab-provisioner"
        in out
    )
    assert (
        "ok: image ami-00000000000000001 (zab-1, made 2026-10-11T00:00:00.000Z), the newest of 2"
        in out
    )
    assert "ok: the role may launch m7a.2xlarge from them in us-east-2 (DryRun)" in out
    assert "ok: the role may import the key pair zab-admin-mit-2027 (DryRun)" in out
    assert "MISSING" not in out and BOT_KEY not in out
    (launch, params), (imp, _) = server["ec2"].dry
    assert (launch, imp) == ("run_instances", "import_key_pair")
    tags = {t["Key"]: t["Value"] for t in params["TagSpecifications"][0]["Tags"]}
    assert tags == {"zab-scope": "mathagents", "zab-role": "test", "workshop": "mit-2027"}
    assert server["config"].read_text() == before and not (server["ws"] / "mit-2027").exists()
    assert server["admin"].created == [] and server["admin"].channels == []


@pytest.mark.parametrize(
    ("ec2", "missing"),
    [
        (FakeEc2(groups=0), "no security group named zab-fleet and tagged zab-scope=mathagents"),
        (FakeEc2(groups=2), "2 security groups named zab-fleet and tagged"),
        (FakeEc2(images=0), "no available image tagged"),
        (
            FakeEc2(launch="UnauthorizedOperation"),
            "the role may launch m7a.2xlarge from them in us-east-2: refused (DryRun): the "
            "role's policy doesn't cover it",
        ),
        (FakeEc2(key="UnauthorizedOperation"), "import the key pair zab-admin-mit-2027: refused"),
        (FakeEc2(launch="InvalidAMIID.Malformed"), "the DryRun answered InvalidAMIID.Malformed"),
    ],
)
def test_a_missing_prerequisite_is_named_and_refuses_the_plan(
    server, monkeypatch, capsys, ec2, missing
):
    monkeypatch.setattr(nw, "Ec2", lambda region: ec2)
    assert nw.main(argv(server, "--dry-run")) == 1
    out = capsys.readouterr().out
    assert any(line.startswith("  MISSING: ") and missing in line for line in out.splitlines())


def test_the_entry_names_no_tag_and_the_tag_is_the_workshops_alone(server, capsys):
    """#425: the scope beyond `workshop=<name>` is `[global]`'s, which the bot passes as
    `--scope`. The entry names no tag, which the loader refuses; a `--tag` beyond the
    workshop's is refused before anything is looked up."""
    nw.main(argv(server, "--dry-run"))
    out = capsys.readouterr().out
    assert 'name = "mit-2027"' in out and "\ntag = " not in out
    args = argv(server, "--dry-run")
    i = args.index("workshop=mit-2027")
    assert nw.main([*args[: i + 1], "--tag", "zab-role=test", *args[i + 1 :]]) == 2
    assert "--tag is workshop=mit-2027 alone" in capsys.readouterr().err
    args[i] = "workshop=other"
    assert nw.main(args) == 2


def test_people_a_taken_bot_name_and_taken_channels_are_named(server, capsys):
    admin = server["admin"]
    admin.people[ALICE]["is_active"] = False
    admin.people[777] = {"user_id": 777, "full_name": "Workshop admin · mit-2027", "is_bot": True}
    admin.streams_seen = [{"name": "mit-2027-organizers", "stream_id": 5}]
    assert nw.main(argv(server, "--dry-run", "--organizer", "4242", "--organizer", "150")) == 1
    out = capsys.readouterr().out
    assert "MISSING: the provisioner is named an organizer" in out
    assert f"MISSING: user {ALICE} (Alice) is a bot or deactivated" in out
    assert "MISSING: user 4242 is not a user of this organization" in out
    assert (
        "MISSING: a user named Workshop admin · mit-2027 exists, not the provisioner's bot" in out
    )
    assert "MISSING: a channel #mit-2027-organizers exists already" in out


def test_an_earlier_workshops_directory_is_refused(server, capsys):
    home = server["ws"] / "mit-2027"
    (home / "out" / "alice").mkdir(parents=True)
    assert nw.main(argv(server, "--dry-run")) == 1
    assert "holds an earlier workshop's files: choose another name" in capsys.readouterr().out


def test_the_apply_makes_everything_and_the_helper_adds_the_entry(server, capsys):
    assert nw.main(argv(server)) == 0
    out = capsys.readouterr().out
    assert "the config helper: mit-2027 added to admin.toml; admin.toml.bak kept" in out
    assert BOT_KEY not in out
    admin = server["admin"]
    assert admin.created == [("Workshop admin · mit-2027", "mit-2027-admin")]
    organizers, alerts = admin.channels
    assert organizers["name"] == "mit-2027-organizers"
    assert organizers["subscribers"] == sorted({OLGA, DREW, ALICE, BOT})  # never the provisioner
    assert alerts["name"] == "mit-2027-fleet-alerts"
    assert alerts["subscribers"] == sorted({OLGA, DREW, ALICE, PROVISIONER})
    for made in (organizers, alerts):
        group = {"direct_members": [DREW, ALICE], "direct_subgroups": []}
        assert made["invite_only"] is True and made["history_public_to_subscribers"] is True
        assert made["can_add_subscribers_group"] == made["can_administer_channel_group"] == group
    home = server["ws"] / "mit-2027"
    assert sorted(p.name for p in home.iterdir()) == ["new-workshop.json", "out", "roster.csv"]
    assert home.stat().st_mode & 0o777 == (home / "out").stat().st_mode & 0o777 == 0o700
    assert (home / "roster.csv").stat().st_mode & 0o777 == 0o600
    assert (home / "roster.csv").read_text().startswith("slug,github,")
    assert server["ec2"].pairs["zab-admin-mit-2027"]["PublicKey"] == KEY
    config = parse_admin_config(server["config"].read_text(), roots=server["roots"])
    new = config.workshop("mit-2027")
    assert new.bot.user_id == BOT and new.bot.channels == {701} and new.alerts_channel == 702
    assert new.aws.ami == "ami-00000000000000001" and new.organizers == {OLGA}
    assert new.tags == (("workshop", "mit-2027"), ("zab-scope", "mathagents"), ("zab-role", "test"))
    # the scope the DryRun launch and the key pair carried is the scope the loader now gives the
    # workshop, which each of its launches sends as `--tag` (#425): authorized = launched
    launch = next(params for call, params in server["ec2"].dry if call == "run_instances")
    for spec in launch["TagSpecifications"]:
        assert {t["Key"]: t["Value"] for t in spec["Tags"]} == dict(new.tags)
    pair = server["ec2"].pairs["zab-admin-mit-2027"]["Tags"]
    assert {t["Key"]: t["Value"] for t in pair} == dict(new.tags)
    key = server["etc"] / "secrets" / "bots" / "mit-2027.zuliprc"
    assert f"key={BOT_KEY}" in key.read_text()
    assert not (server["tmp"] / "requests" / "request.json").exists()


def test_a_rerun_after_a_refusal_reuses_what_it_made(server, capsys, monkeypatch):
    """The helper refused (here: a key file in the way): a second run reuses the bot, the two
    channels (by their recorded IDs) and the key pair, and completes."""
    stale = server["etc"] / "secrets" / "bots" / "mit-2027.zuliprc"
    stale.write_text("in the way\n")
    assert nw.main(argv(server)) == 1
    assert "mit-2027.zuliprc exists: a deployer removes it first" in capsys.readouterr().out
    stale.unlink()
    assert nw.main(argv(server)) == 0
    out = capsys.readouterr().out
    assert "#mit-2027-organizers: made by an earlier run (701)" in out
    admin = server["admin"]
    assert len(admin.created) == 1 and len(admin.channels) == 2
    assert "the key pair zab-admin-mit-2027, from an earlier run, with this key" in out


def test_a_rerun_for_other_people_never_reuses_the_channels(server, capsys):
    """Codex on #423: an apply made the channels, then failed; a plan of the same name for other
    organizers must not reuse them, which would keep the first organizers in and leave the new
    ones out. It is refused before anything is made; the same people again may reuse them."""
    stale = server["etc"] / "secrets" / "bots" / "mit-2027.zuliprc"
    stale.write_text("in the way\n")
    assert nw.main(argv(server)) == 1  # the channels are made, then the helper refuses
    stale.unlink()
    capsys.readouterr()
    other = argv(server, "--organizer", str(DREW))
    assert nw.main(other) == 1
    out = capsys.readouterr().out
    made = "#mit-2027-organizers and #mit-2027-fleet-alerts"
    assert f"MISSING: an earlier run made {made} for other organizers" in out
    assert "Nothing was made" in out and len(server["admin"].channels) == 2
    assert nw.main(argv(server)) == 0  # the same people: reused


def test_an_earlier_outcome_of_the_same_plan_is_never_taken(server, monkeypatch, capsys):
    """Codex round 3 on #423: a result.json of this plan from an earlier run is not this
    request's outcome. Only a file the helper wrote after the request counts."""
    result = server["tmp"] / "helper-state" / "result.json"
    result.write_text(json.dumps({"plan": "P-9", "ok": False, "message": "an earlier refusal"}))
    monkeypatch.setattr(nw, "WAIT", 0.01)
    monkeypatch.setattr(nw.time, "sleep", lambda seconds: None)  # the helper is slow
    assert nw.main(argv(server)) == 1
    out = capsys.readouterr().out
    assert "an earlier refusal" not in out and "said nothing within" in out


def test_a_pending_request_is_never_overwritten(server, monkeypatch, capsys):
    pending = server["tmp"] / "requests" / "request.json"
    pending.write_text("{}")
    monkeypatch.setattr(nw.time, "sleep", lambda seconds: None)  # no helper: it stays
    assert nw.main(argv(server)) == 1
    assert "is there already" in capsys.readouterr().out and pending.read_text() == "{}"


def test_a_silent_helper_has_the_request_withdrawn(server, monkeypatch, capsys):
    """Review of #423: no outcome in time, and the request, which holds the bot's key, is
    withdrawn, so a late helper never applies a plan reported failed."""
    requests, seen = server["tmp"] / "requests", []

    def look(seconds):  # the request as the helper would find it, while it waits
        if not seen:
            path = requests / "request.json"
            seen.append((json.loads(path.read_text()), path.stat().st_mode & 0o777))

    monkeypatch.setattr(nw, "WAIT", 0.01)
    monkeypatch.setattr(nw.time, "sleep", look)
    assert nw.main(argv(server)) == 1
    out = capsys.readouterr().out
    assert "said nothing within 0 s: the request is withdrawn, and nothing was added" in out
    (request, mode), *_ = seen
    assert set(request) == {"version", "plan", "entry", "zuliprc"} and request["plan"] == "P-9"
    assert mode == 0o600 and list(requests.iterdir()) == []
    helper = confighelper.Paths(str(requests), str(server["config"]), str(server["tmp"] / "x"))
    assert confighelper.take(helper, os.getuid()) is None  # a late helper finds nothing


def test_a_request_the_helper_took_is_not_withdrawn(server, monkeypatch, capsys):
    """Review of #423: the helper claimed the request (a rename), then said nothing: the
    withdrawal finds none, and says the entry may be added."""
    requests = server["tmp"] / "requests"
    helper = confighelper.Paths(str(requests), str(server["config"]), str(server["tmp"] / "x"))
    monkeypatch.setattr(nw, "WAIT", 0.01)
    monkeypatch.setattr(nw, "LATE", 0.01)
    monkeypatch.setattr(nw.time, "sleep", lambda s: confighelper.take(helper, os.getuid()))
    assert nw.main(argv(server)) == 1
    out = capsys.readouterr().out
    assert "the config helper took the request but said nothing within 0 s: the entry may be" in out


def test_an_outcome_without_a_plan_is_a_refusal(server, monkeypatch, capsys):
    """Review of #423: the helper writes `plan: null` for a request it couldn't read a plan in:
    a fresh one is this request's refusal, said with its message."""
    requests, result = server["tmp"] / "requests", server["tmp"] / "helper-state" / "result.json"

    def refuse(seconds):
        (requests / "request.json").unlink(missing_ok=True)
        result.write_text(json.dumps({"plan": None, "ok": False, "message": "not JSON"}))

    monkeypatch.setattr(nw.time, "sleep", refuse)
    assert nw.main(argv(server)) == 1
    assert "the config helper: not JSON" in capsys.readouterr().out


def test_an_outcome_without_a_plan_while_ours_is_pending_is_not_ours(server, monkeypatch, capsys):
    """Review of #423, round 2: a fresh `plan: null` result while our request is still there is
    some earlier request's, never ours: ours is withdrawn when the wait ends."""
    requests, result = server["tmp"] / "requests", server["tmp"] / "helper-state" / "result.json"

    def stale(seconds):  # the helper answers another request; ours stays
        result.write_text(json.dumps({"plan": None, "ok": False, "message": "an earlier one"}))

    monkeypatch.setattr(nw, "WAIT", 0.05)
    monkeypatch.setattr(nw.time, "sleep", stale)
    assert nw.main(argv(server)) == 1
    out = capsys.readouterr().out
    assert "an earlier one" not in out
    assert "the request is withdrawn, and nothing was added" in out
    assert list(requests.iterdir()) == []


@pytest.mark.parametrize("link_fails", [False, True])
def test_another_plans_request_is_never_left_withdrawn(server, monkeypatch, capsys, link_fails):
    """Review of #423, round 2: the withdrawal found another plan's request (never: one at a
    time). It is put back; if that fails (a third is there), it is dropped, never left as
    `.withdrawn`, and the message says which."""
    requests = server["tmp"] / "requests"
    other = {"version": 1, "plan": "P-77", "entry": {}, "zuliprc": ""}

    def swapped(seconds):
        (requests / "request.json").write_text(json.dumps(other))

    def no_link(src, dst):
        raise OSError(17, "File exists")

    monkeypatch.setattr(nw, "WAIT", 0.01)
    monkeypatch.setattr(nw.time, "sleep", swapped)
    if link_fails:
        monkeypatch.setattr(nw.os, "link", no_link)
    assert nw.main(argv(server)) == 1
    out = capsys.readouterr().out
    assert not [p for p in requests.iterdir() if p.name.endswith(".withdrawn")]
    if link_fails:
        assert "another plan's request: not put back (File exists), so dropped" in out
        assert list(requests.iterdir()) == []
    else:
        assert "another plan's request: put back, pending" in out
        assert json.loads((requests / "request.json").read_text())["plan"] == "P-77"


def test_a_request_whose_write_fails_leaves_nothing(server, monkeypatch):
    """Review of #423: the temporary request holds the bot's key: removed when its write fails."""

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(nw.json, "dump", full)
    assert nw.main(argv(server)) == 1
    assert list((server["tmp"] / "requests").iterdir()) == []


def test_a_tie_for_the_newest_image_is_refused(server, monkeypatch, capsys):
    """Review of #423: two images tagged with the scope, made at the same time: no silent pick.
    The plan names both, and someone untags one."""
    ec2 = FakeEc2(images=2)
    for image in ec2.images:
        image["CreationDate"] = "2026-10-16T00:00:00.000Z"
    monkeypatch.setattr(nw, "Ec2", lambda region: ec2)
    assert nw.main(argv(server, "--dry-run")) == 1
    out = capsys.readouterr().out
    assert "MISSING: 2 images tagged zab-scope=mathagents, zab-role=test in us-east-2 are" in out
    assert "ami-00000000000000000 (zab-0), ami-00000000000000001 (zab-1): untag all but one" in out


def test_a_failure_between_the_channels_then_a_rerun(server, capsys):
    """Review of #423: the second channel fails after the first is made: a rerun of the same
    plan reuses the bot and the first channel, by its kept ID, and makes the second."""
    admin = server["admin"]
    real = admin.request
    calls = []

    def second_fails(method, endpoint, **params):
        calls.append(params["name"])
        if len(calls) == 2:
            raise nw.ZulipAdminError("BAD_REQUEST", 400, "Channel name already in use")
        return real(method, endpoint, **params)

    admin.request = second_fails
    assert nw.main(argv(server)) == 1
    assert "Channel name already in use" in capsys.readouterr().err
    state = json.loads((server["ws"] / "mit-2027" / "new-workshop.json").read_text())
    assert state["channels"] == {"organizers": 701}
    admin.request = real
    assert nw.main(argv(server)) == 0
    out = capsys.readouterr().out
    assert "#mit-2027-organizers: made by an earlier run (701)" in out
    assert "made #mit-2027-fleet-alerts (702)" in out and len(admin.created) == 1


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Boto:
    """boto3's EC2 client, for `aws_common.Ec2`'s region-setup calls."""

    def __init__(self):
        self.calls = []
        tags = [{"Key": "zab-scope", "Value": "mathagents"}]
        self.groups = [{"GroupId": "sg-1", "Tags": tags}, {"GroupId": "sg-2", "Tags": []}]
        self.images = [{"ImageId": "ami-1", "Tags": tags}]

    def describe_security_groups(self, **kw):
        self.calls.append(("describe_security_groups", kw))
        return {"SecurityGroups": self.groups}

    def describe_images(self, **kw):
        self.calls.append(("describe_images", kw))
        return {"Images": self.images}

    def run_instances(self, **kw):
        self.calls.append(("run_instances", kw))
        raise _ClientError("DryRunOperation")

    def import_key_pair(self, **kw):
        self.calls.append(("import_key_pair", kw))
        raise _ClientError("UnauthorizedOperation")


def test_the_ec2_calls_filter_by_every_tag_and_never_launch():
    import aws_common as ac

    ec2 = object.__new__(ac.Ec2)
    ec2._ec2 = boto = _Boto()
    scope = {"zab-scope": "mathagents"}
    assert [g["GroupId"] for g in ec2.find_security_groups(scope)] == ["sg-1"]  # checked again
    boto.groups[0]["GroupName"] = "zab-fleet"
    assert [g["GroupId"] for g in ec2.find_security_groups(scope, "zab-fleet")] == ["sg-1"]
    _, named = boto.calls[-1]
    assert {"Name": "group-name", "Values": ["zab-fleet"]} in named["Filters"]
    del boto.calls[-1]
    assert [i["ImageId"] for i in ec2.find_images(scope)] == ["ami-1"]
    assert ec2.dry_run("run_instances", ImageId="ami-1") == "DryRunOperation"
    assert ec2.dry_run("import_key_pair", KeyName="k") == "UnauthorizedOperation"
    _, groups = boto.calls[0]
    assert groups == {"Filters": [{"Name": "tag:zab-scope", "Values": ["mathagents"]}]}
    _, images = boto.calls[1]
    assert images["Owners"] == ["self"]
    assert {"Name": "state", "Values": ["available"]} in images["Filters"]
    assert all(kw.get("DryRun") is True for _, kw in boto.calls[2:])
    with pytest.raises(ValueError, match="untagged"):
        ec2.find_images({})


# ------------------------------------------------- the participants' channel (#627)

HALLOWEEN = "Harvard%20Halloween"  # as the bot passes `channel="Harvard Halloween"`


def test_a_channel_the_provisioner_is_in_is_adopted_as_it_is(server, capsys):
    """Harvard's case: "Harvard Halloween" exists with zab-provisioner in it. Its ID goes into
    the entry, matched by name in any case; nothing of it is made or changed."""
    admin = server["admin"]
    admin.subscribed = [{"stream_id": 555, "name": "Harvard Halloween", "invite_only": True}]
    assert nw.main(argv(server, "--dry-run", "--channel", "harvard%20HALLOWEEN")) == 0
    out = capsys.readouterr().out
    assert '\nalerts_channel = "<new>"\nchannel = 555\n' in out
    assert "ok: #Harvard Halloween (555), the participants' channel: zab-provisioner is in" in out
    assert "#Harvard Halloween, the participants' channel: adopted (555)" in out
    assert "warning:" not in out and "MISSING" not in out
    assert nw.main(argv(server, "--channel", HALLOWEEN)) == 0
    names = [made["name"] for made in admin.channels]
    assert names == ["mit-2027-organizers", "mit-2027-fleet-alerts"]  # none made for it
    config = parse_admin_config(server["config"].read_text(), roots=server["roots"])
    assert config.workshop("mit-2027").channel == 555


def test_a_public_channel_is_adopted_with_a_warning(server, capsys):
    admin = server["admin"]
    admin.subscribed = [{"stream_id": 555, "name": "Harvard Halloween", "invite_only": False}]
    assert nw.main(argv(server, "--dry-run", "--channel", HALLOWEEN)) == 0
    out = capsys.readouterr().out
    assert "warning: #Harvard Halloween (555) is public: anyone in the organization can" in out


def test_a_channel_the_provisioner_sees_but_is_not_in_is_refused(server, capsys):
    """A public channel without the provisioner: the plan says to subscribe it first."""
    server["admin"].streams_seen = [{"stream_id": 555, "name": "Harvard Halloween"}]
    assert nw.main(argv(server, "--dry-run", "--channel", HALLOWEEN)) == 1
    out = capsys.readouterr().out
    assert (
        "MISSING: #Harvard Halloween exists, and zab-provisioner is not subscribed to it: "
        "subscribe zab-provisioner to it first" in out
    )
    assert nw.main(argv(server, "--channel", HALLOWEEN)) == 1  # the apply checks again
    assert server["admin"].channels == [] and not (server["ws"] / "mit-2027").exists()


def test_a_channel_nobody_has_is_made_first_private_for_the_organizers(server, capsys):
    """Not visible to the provisioner: the plan says the apply makes it, and what a private
    channel of that name it can't see would do; the apply makes it before anything else."""
    assert nw.main(argv(server, "--dry-run", "--channel", HALLOWEEN)) == 0
    out = capsys.readouterr().out
    assert '\nchannel = "<new>"\n' in out
    assert "#Harvard Halloween (private), the participants' channel: the organizers, zab-" in out
    assert (
        "unchecked: #Harvard Halloween is made only if no private channel has that name: "
        "zab-provisioner sees none it isn't in" in out
    )
    admin = server["admin"]
    assert admin.channels == []
    assert nw.main(argv(server, "--channel", HALLOWEEN)) == 0
    assert "made #Harvard Halloween, the participants' channel (701)" in capsys.readouterr().out
    first, *rest = admin.channels
    assert first["name"] == "Harvard Halloween" and len(rest) == 2  # before the bot's channels
    assert first["subscribers"] == sorted({OLGA, PROVISIONER})  # not the global admins
    assert first["invite_only"] is True and first["history_public_to_subscribers"] is True
    assert first["can_administer_channel_group"] == {"direct_members": [OLGA],
                                                     "direct_subgroups": []}  # fmt: skip
    assert first["can_add_subscribers_group"]["direct_members"] == sorted({OLGA, DREW, ALICE})
    config = parse_admin_config(server["config"].read_text(), roots=server["roots"])
    new = config.workshop("mit-2027")
    assert new.channel == 701 and new.bot.channels == {702} and new.alerts_channel == 703


def test_a_channel_zulip_refuses_to_make_stops_the_apply_before_anything(server, capsys):
    """A private channel of that name that the provisioner can't see: Zulip refuses, and the
    answer says to subscribe the provisioner to it; nothing else in Zulip was made."""
    admin = server["admin"]
    real = admin.request

    def taken(method, endpoint, **params):
        if params["name"] == "Harvard Halloween":
            raise nw.ZulipAdminError("BAD_REQUEST", 400, "Channel name already in use")
        return real(method, endpoint, **params)

    admin.request = taken
    before = server["config"].read_text()
    assert nw.main(argv(server, "--channel", HALLOWEEN)) == 1
    out = capsys.readouterr().out
    assert "#Harvard Halloween could not be made (" in out and "already in use" in out
    assert "subscribe zab-provisioner to it first" in out
    assert admin.created == [] and admin.channels == []
    assert server["config"].read_text() == before


def test_a_rerun_adopts_the_channel_it_made(server, capsys):
    """The helper refused after the channel was made: the rerun finds the provisioner in it,
    and adopts it, making no second one."""
    stale = server["etc"] / "secrets" / "bots" / "mit-2027.zuliprc"
    stale.write_text("in the way\n")
    assert nw.main(argv(server, "--channel", HALLOWEEN)) == 1
    stale.unlink()
    capsys.readouterr()
    assert nw.main(argv(server, "--channel", HALLOWEEN)) == 0
    out = capsys.readouterr().out
    assert "#Harvard Halloween, the participants' channel: adopted (701)" in out
    assert [made["name"] for made in server["admin"].channels].count("Harvard Halloween") == 1


def test_a_channel_named_as_one_the_run_makes_is_refused(server, capsys):
    assert nw.main(argv(server, "--dry-run", "--channel", "mit-2027-Organizers")) == 1
    out = capsys.readouterr().out
    assert "MISSING: channel= names #mit-2027-Organizers, one of the channels this run makes" in out


@pytest.mark.parametrize("where", ["mine", "visible", "hidden"])
@pytest.mark.parametrize("name", ["harvard-2026-fleet-alerts", "Old-Organizers"])
def test_a_channel_named_like_a_workshops_own_is_never_adopted_or_made(server, capsys, name, where):
    """Review round 1 of #638, and its approval (6397329425, B): whether the provisioner is in
    it, sees it, or sees nothing, the plan is refused, and never says to subscribe it."""
    seen = {"stream_id": 44, "name": name, "invite_only": False}
    if where == "mine":
        server["admin"].subscribed = [seen]
    elif where == "visible":
        server["admin"].streams_seen = [seen]
    assert nw.main(argv(server, "--dry-run", "--channel", name)) == 1
    out = capsys.readouterr().out
    assert f"MISSING: #{name} is named like a workshop's organizers or alerts channel" in out
    assert "subscribe zab-provisioner" not in out
    assert nw.main(argv(server, "--channel", name)) == 1
    assert server["admin"].created == [] and server["admin"].channels == []


@pytest.mark.parametrize("where", ["mine", "visible"])
def test_a_channel_admin_toml_binds_is_never_adopted(server, capsys, where):
    """Review round 1 of #638: refused at the plan, not by the loader at the apply's end,
    after the bot, the channels and the key pair are made; and (6397329425, B) one the
    provisioner sees without being in it is refused the same, never "subscribe" to it."""
    seen = {"stream_id": 61, "name": "Some Channel", "invite_only": False}
    if where == "mine":
        server["admin"].subscribed = [seen]
    else:
        server["admin"].streams_seen = [seen]
    bound = ["--bound-channel", "60", "--bound-channel", "61"]  # another workshop's, by its ID
    assert nw.main(argv(server, "--dry-run", "--channel", "Some%20Channel", *bound)) == 1
    out = capsys.readouterr().out
    assert "MISSING: #Some Channel (61) is a channel admin.toml binds already" in out
    assert "subscribe zab-provisioner" not in out
    assert nw.main(argv(server, "--channel", "Some%20Channel", *bound)) == 1
    assert server["admin"].created == [] and server["admin"].channels == []


@pytest.mark.parametrize("bad", ["%20x", "x%00", "%ZZ", "%FF", "a" * 61])
def test_a_channel_argument_that_is_no_name_is_refused(server, capsys, bad):
    assert nw.main(argv(server, "--dry-run", "--channel", bad)) == 2
    assert "--channel: not a channel's name" in capsys.readouterr().err
