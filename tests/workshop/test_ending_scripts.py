"""The stage-3 verbs' argument lists, run through the scripts themselves (docs/ADMIN.md §3,
action 7; ORGANIZERS §5; #357): `!teardown` against a fake EC2, `!deactivate-bots` against a
fake Zulip, and the other verbs' dry runs, offline. The lists come from `zab.admin.actions`,
for a workshop read from its own roster, `spares.json` and audit ledger (`fleet.read_fleet`);
each names this checkout's own script, whose `main` runs it."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import admin_common as ac
import collect_exports
import final_sync
import push_config
import pytest
import teardown
import zulip_provision

from zab.admin import fleet
from zab.admin.actions import Action, ActionError, Layout, Live, build, digests, plan_hash
from zab.admin.approvals import DESTRUCTIVE, requirement
from zab.admin.config import RELEASE_PATTERNS, Roots, parse_admin_config
from zab.admin.fleet import ApplyEnd, Ledger
from zab.admin.grammar import Command, Context, Roster, parse
from zab.admin.plans import Requirement

from .test_aws import FakeEc2

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = {
    "teardown.py": teardown,
    "zulip_provision.py": zulip_provision,
    "final_sync.py": final_sync,
    "push_config.py": push_config,
    "collect_exports.py": collect_exports,
}
HARVARD_ROSTER = (
    "slug,email,repo,ip,region,repo_status\n"
    "alice,alice@example.org,alice,198.51.100.1,us-west-2,ok\n"
    "bob,bob@example.org,bob,198.51.100.2,us-west-2,ok\n"
    "carol,carol@example.org,carol,,,\n"  # never launched
    "dave,dave@example.org,dave,198.51.100.4,us-west-2,ok\n"
)
TEST_ROSTER = (
    "slug,email,repo,ip,region\n"
    "erin,erin@example.org,erin,203.0.113.5,us-west-2\n"
    "alice-2,a2@example.org,alice-2,203.0.113.6,us-west-2\n"
)
SCOPE = "mathagents"  # [global] scope (#422): every lookup asks for it
SCOPED = {"zab-scope": SCOPE}
WORKSHOP = """
[[workshop]]
name = "{name}"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "{ws}/{name}/roster.csv"
out_dir = "{ws}/{name}/out"
organizers = [{organizer}]
[workshop.bot]
zuliprc = "{etc}/secrets/bots/{name}.zuliprc"
user_id = {bot}
channels = [{channel}]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-{name}"
admin_keys = ["{etc}/break-glass/drew.pub"]
"""


class Site:
    """Two workshops in one region on one server, each with its roster and out/."""

    def __init__(self, tmp: pathlib.Path) -> None:
        etc, ws = tmp / "etc", tmp / "ws"
        text = 'provider = "aws"\nadmins = [101]\ndeployers = [101]\ndestructive_approvals = 2\n'
        text += f'[global]\nscope = "{SCOPE}"\nregions = ["us-west-2"]\n'
        text += 'instance_types = ["m7a.2xlarge"]\n'
        for name, organizer, bot, channel in (
            ("harvard-2026", 103, 901, 42),
            ("zab-test", 105, 902, 43),
        ):
            text += WORKSHOP.format(
                name=name, ws=ws, etc=etc, organizer=organizer, bot=bot, channel=channel
            )
        roots = Roots(
            workshops=str(ws), secrets=str(etc / "secrets"), break_glass=str(etc / "break-glass")
        )
        self.config = parse_admin_config(text, roots=roots)
        self.harvard, self.test = self.config.workshops
        for w, roster in ((self.harvard, HARVARD_ROSTER), (self.test, TEST_ROSTER)):
            pathlib.Path(w.out_dir).mkdir(parents=True, mode=0o700)
            pathlib.Path(w.roster).write_text(roster)
        spares = {
            f"harvard-2026/us-west-2/zab-spare-{n}": {
                "name": f"zab-spare-{n}",
                "ip": f"198.51.100.{20 + n}",
            }
            for n in (1, 2)
        }
        (pathlib.Path(self.harvard.out_dir) / "spares.json").write_text(json.dumps(spares))
        (etc / "secrets").mkdir(parents=True)
        self.provisioner = etc / "secrets" / "provisioner.zuliprc"
        self.provisioner.write_text(
            "[api]\nemail=prov@org\nkey=" + "P" * 32 + "\nsite=https://org.zulipchat.com\n"
        )
        self.layout = Layout(release=str(REPO), provisioner_zuliprc=str(self.provisioner))
        self.ledger = Ledger()

    def applied(self, verb: str, *slugs: str, exit: int = 0) -> None:
        """An apply of `verb` on `slugs`, as the runner records its start and end."""
        start = {"workshop": "harvard-2026", "plan": "P-1", "verb": verb, "args": list(slugs)}
        self.ledger.observe({"event": "apply_start", **start, **ApplyEnd.start_fields(slugs)})
        end = ApplyEnd("harvard-2026", "P-1", verb, slugs, exit, slugs)
        self.ledger.observe({"event": "apply_end", "ts": "2026-11-02T14:05:00Z", **end.fields()})

    def sync(self, *slugs: str) -> None:
        """`!uninstall`, then a successful `!final-sync`, of `slugs`: they count as synced."""
        self.applied("uninstall", *slugs)
        self.applied("final-sync", *slugs)

    def action(self, message: str) -> Action:
        here = fleet.read_fleet(self.config, self.harvard, self.ledger)
        roster = Roster(here.slugs, here.spares)
        pattern = re.compile(RELEASE_PATTERNS["final"], re.ASCII)
        command = parse(message, Context(901, "Workshop admin", pattern, roster))
        assert isinstance(command, Command), command
        self.fleet = here
        return build(command, here, layout=self.layout)


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


def run(argv: tuple[str, ...]) -> int:
    """`argv` as the runner would run it, through its script's own `main`: the list names
    this checkout's script, by its path under the release."""
    i = next(i for i, word in enumerate(argv) if word.endswith(".py"))
    script = pathlib.Path(argv[i])
    module = SCRIPTS[script.name]
    assert script.resolve() == pathlib.Path(module.__file__).resolve()
    return module.main(list(argv[i + 1 :]))


# --------------------------------------------------------------------------------- teardown


def tags(**pairs: str) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in pairs.items()]


def fleet_ec2() -> FakeEc2:
    """harvard's VMs (bob's protected) and spares, each with its Elastic IP; zab-test's erin;
    and an untagged-participant instance in harvard's scope (the admin server, by mistake)."""
    ec2 = FakeEc2()
    vms = {
        "i-alice": ("harvard-2026", "alice"),
        "i-bob": ("harvard-2026", "bob"),
        "i-dave": ("harvard-2026", "dave"),
        "i-spare1": ("harvard-2026", "zab-spare-1"),
        "i-spare2": ("harvard-2026", "zab-spare-2"),
        "i-erin": ("zab-test", "erin"),
    }
    for iid, (ws, slug) in vms.items():
        ec2.instances[iid] = {
            "InstanceId": iid,
            "State": {"Name": "running" if slug != "dave" else "stopped"},
            "Tags": tags(Name=f"ws-{slug}", workshop=ws, participant=slug, **SCOPED),
        }
        aid = f"eipalloc-{iid[2:]}"
        ec2.addresses[aid] = {
            "AllocationId": aid,
            "PublicIp": f"203.0.113.{len(ec2.addresses) + 1}",
            "InstanceId": iid,
            "AssociationId": f"eipassoc-{iid[2:]}",
            "Tags": tags(workshop=ws, participant=slug, **SCOPED),
        }
    ec2.instances["i-server"] = {
        "InstanceId": "i-server",
        "State": {"Name": "running"},
        "Tags": tags(Name="zab-admin", workshop="harvard-2026", **SCOPED),
    }
    ec2.attributes["i-bob"] = [True, "stop"]  # protect.py on: a kept VM
    return ec2


@pytest.fixture
def ec2(monkeypatch):
    fake = fleet_ec2()

    def connect(region: str) -> FakeEc2:
        assert region == "us-west-2"
        return fake

    monkeypatch.setattr(teardown, "Ec2", connect)
    return fake


def states(ec2: FakeEc2) -> dict[str, str]:
    return {iid: inst["State"]["Name"] for iid, inst in ec2.instances.items()}


def test_teardown_all_unsynced_terminates_and_releases_keeping_the_protected(site, ec2, capsys):
    """ADMIN §7 A9's teardown, offline: refused while rows are unsynced; with `unsynced` (and its
    phrase, two approvers) the VMs go with their Elastic IPs; bob, protected, is listed `keep`
    and keeps his address; spares, the other workshop and the server are untouched."""
    site.sync("alice")
    with pytest.raises(ActionError, match="on record for bob, carol, dave:"):
        site.action("!teardown all")
    action = site.action("!teardown all unsynced")
    assert requirement(action, site.fleet, site.config) == Requirement(
        DESTRUCTIVE, 2, ("teardown", "4", "unsynced")
    )
    (step,) = action.steps
    before = (states(ec2), dict(ec2.addresses))
    assert run(step.dry_run) == 0
    assert (states(ec2), dict(ec2.addresses)) == before  # the plan's dry run changes nothing
    shown = capsys.readouterr().out
    assert "keep i-bob ws-bob: termination protection is on" in shown
    assert "terminate i-alice" in shown and "terminate i-dave" in shown
    assert "dry run: nothing changed" in shown
    assert run(step.argv) == 0  # --yes: the typed phrase in Zulip was the confirmation
    assert states(ec2) == {
        "i-alice": "shutting-down",
        "i-bob": "running",
        "i-dave": "shutting-down",
        "i-spare1": "running",
        "i-spare2": "running",
        "i-erin": "running",
        "i-server": "running",
    }
    assert sorted(ec2.addresses) == [
        "eipalloc-bob",
        "eipalloc-erin",
        "eipalloc-spare1",
        "eipalloc-spare2",
    ]


def teardown_hash(site, ec2, action: Action, capsys) -> str:
    """The plan's hash as the runner computes it, at plan time or again at apply: the dry run
    run now, normalized."""
    capsys.readouterr()
    assert run(action.steps[0].dry_run) == 0
    tmp = "/var/lib/zab-admin/tmp/P-7"
    live = Live(dry_run=capsys.readouterr().out, tmpdir=tmp)
    return plan_hash(action, release="v0.3.0+abc", digests=digests(action), live=live)


def test_the_teardown_hash_covers_what_it_keeps_terminates_and_releases(site, ec2, capsys):
    """Codex round 1 on #367: a plan that showed `keep i-bob` must not apply after `!protect
    off bob`, when it would terminate him: the dry run, hashed, changes. A state that idle
    stop changes, or the order EC2 lists things in, doesn't."""
    action = site.action("!teardown all unsynced")
    planned = teardown_hash(site, ec2, action, capsys)
    assert teardown_hash(site, ec2, action, capsys) == planned  # the same dry run again
    ec2.instances["i-alice"]["State"]["Name"] = "stopped"  # idle stop powered alice off
    ec2.instances = dict(reversed(ec2.instances.items()))  # listed in another order
    assert teardown_hash(site, ec2, action, capsys) == planned
    ec2.attributes["i-bob"][0] = False  # !protect off bob, between plan and apply
    unprotected = teardown_hash(site, ec2, action, capsys)
    assert unprotected != planned
    del ec2.addresses["eipalloc-dave"]  # an address released by hand
    assert teardown_hash(site, ec2, action, capsys) != unprotected


def test_teardown_spares_removes_only_the_spares(site, ec2):
    action = site.action("!teardown spares")
    assert action.targets == ("zab-spare-1", "zab-spare-2")
    assert run(action.steps[0].argv) == 0
    gone = {iid for iid, state in states(ec2).items() if state == "shutting-down"}
    assert gone == {"i-spare1", "i-spare2"}
    assert sorted(ec2.addresses) == [
        "eipalloc-alice",
        "eipalloc-bob",
        "eipalloc-dave",
        "eipalloc-erin",
    ]


def test_teardown_of_synced_rows_needs_no_unsynced(site, ec2):
    site.sync("alice", "dave")
    action = site.action("!teardown alice dave")
    assert requirement(action, site.fleet, site.config).phrase == ("teardown", "2")
    assert run(action.steps[0].argv) == 0
    assert states(ec2)["i-alice"] == states(ec2)["i-dave"] == "shutting-down"


# ------------------------------------------------------------------------- deactivate-bots


class FakeZulip:
    """The provisioner's `GET /bots` and `GET /users`: every workshop's participant bots, since
    one provisioner owns them all (§1.2), and the admin bots."""

    site = "https://org.zulipchat.com"

    def __init__(self) -> None:
        self.deleted: list[int] = []
        self._bots, self._users = [], []
        uid = 300
        for slug in ("alice", "bob", "carol", "erin", "alice-2"):  # dave was never provisioned
            for kind in ("claude", "codex"):
                uid += 1
                email = f"{kind}-{slug}-bot@org"
                self._bots.append(
                    {
                        "username": email,
                        "full_name": f"{kind.capitalize()} · {slug}",
                        "api_key": "K" * 32,
                    }
                )
                self._users.append({"user_id": uid, "email": email, "is_bot": True})
        self._users.append({"user_id": 901, "email": "harvard-admin-bot@org", "is_bot": True})
        self.ids = {u["email"]: u["user_id"] for u in self._users}

    def users(self) -> list[dict[str, Any]]:
        return self._users

    def bots(self) -> list[dict[str, Any]]:
        return self._bots

    def deactivate_bot(self, bot_id: int) -> None:
        self.deleted.append(bot_id)

    def close(self) -> None:
        pass


def zuliprcs(site: Site, *slugs: str, email=lambda kind, slug: f"{kind}-{slug}-bot@org") -> None:
    """Each row's bot zuliprcs in out/, as zulip_provision.py writes them."""
    for slug in slugs:
        for kind in ("claude", "codex"):
            path = pathlib.Path(site.harvard.out_dir) / slug / f"{kind}.zuliprc"
            path.parent.mkdir(exist_ok=True, mode=0o700)
            path.write_text(
                f"[api]\nemail={email(kind, slug)}\nkey={'K' * 32}\nsite=https://org.zulipchat.com\n"
            )


def test_deactivate_bots_all_takes_only_this_workshops_participant_bots(site, monkeypatch, capsys):
    zulip = FakeZulip()
    zuliprcs(site, "alice", "bob", "carol")
    opened: list[tuple[str, str]] = []

    def connect(url: str, email: str, key: str) -> FakeZulip:
        opened.append((url, email))
        return zulip

    monkeypatch.setattr(zulip_provision, "ZulipAdmin", connect)
    action = site.action("!deactivate-bots all")
    assert requirement(action, site.fleet, site.config) == Requirement(
        DESTRUCTIVE, 2, ("deactivate-bots", "4")
    )
    (step,) = action.steps
    assert run(step.dry_run) == 0 and zulip.deleted == []
    assert run(step.argv) == 0
    assert opened == [("https://org.zulipchat.com", "prov@org")] * 2  # the provisioner's key
    harvard = {
        f"{kind}-{slug}-bot@org"
        for slug in ("alice", "bob", "carol")
        for kind in ("claude", "codex")
    }
    assert sorted(zulip.deleted) == sorted(zulip.ids[email] for email in harvard)
    assert "dave" in capsys.readouterr().out  # reported: no bots to deactivate


# ------------------------------------------------------------------- the other verbs' dry runs


def test_final_sync_all_runs_the_rows_with_a_vm(site, monkeypatch, capsys):
    calls = []

    def fake_ssh(argv, *, timeout_s=120.0, env=None, stdin=None):
        calls.append(argv)
        return ac.RunResult(
            0, json.dumps({"worktrees": [], "branches": [], "index": "unchanged"}) + "\n", ""
        )

    monkeypatch.setattr(final_sync, "subprocess_runner", fake_ssh)
    action = site.action("!final-sync all")
    assert action.targets == ("alice", "bob", "dave")
    # the apply's three connections per VM: the idle-stop hold, the sync, the release (#370)
    for argv, per_vm in ((action.steps[0].dry_run, 1), (action.steps[0].argv, 3)):
        calls.clear()
        assert run(argv) == 0
        hosts = [f"ubuntu@198.51.100.{n}" for n in (1, 2, 4) for _ in range(per_vm)]
        assert sorted(a[-3] for a in calls) == hosts
        known = f'UserKnownHostsFile="{site.harvard.out_dir}/known_hosts"'
        assert all(known in a for a in calls)
    reports = list(pathlib.Path(site.harvard.out_dir).glob("final_sync-*.json"))
    assert reports  # in the workshop's out/, not the release tree


def test_uninstall_all_dry_run_is_offline(site, capsys):
    action = site.action("!uninstall all")
    assert run(action.steps[0].dry_run) == 0
    shown = capsys.readouterr().out  # one line per VM, its commands by name (#361)
    assert (
        shown.count(": service uninstall; warm-up removed\n") == 3 and "(198.51.100.4): " in shown
    )


def test_export_all_dry_run_is_offline(site, capsys):
    action = site.action("!export all")
    assert run(action.steps[0].dry_run) == 0
    assert "would export and fetch" in capsys.readouterr().out
    assert not (pathlib.Path(site.harvard.out_dir) / "exports").exists()


def test_deactivate_bots_acts_only_on_the_hashed_bots(site, monkeypatch, capsys):
    """The review of #367: each bot is the one whose e-mail its hashed zuliprc holds, and it
    must carry the row's name; a mismatch or a missing file is refused, and reported."""
    zulip = FakeZulip()
    monkeypatch.setattr(zulip_provision, "ZulipAdmin", lambda url, email, key: zulip)

    def email(kind: str, slug: str) -> str:
        if (kind, slug) == ("codex", "carol"):
            return "codex-alice-2-bot@org"  # another workshop's bot: refused
        return f"{kind}-{slug}-bot@org"

    zuliprcs(site, "alice", "carol", email=email)  # bob's are missing
    (step,) = site.action("!deactivate-bots alice bob carol").steps
    assert run(step.argv) == 1
    shown = capsys.readouterr()
    assert "out/carol/codex.zuliprc holds codex-alice-2-bot@org, which is not Codex · carol" in (
        shown.out
    )
    assert "out/bob/claude.zuliprc is missing" in shown.out
    deactivated = {e for e, uid in zulip.ids.items() if uid in zulip.deleted}
    assert deactivated == {"claude-alice-bot@org", "codex-alice-bot@org", "claude-carol-bot@org"}


def test_a_final_sync_that_reaches_no_vm_never_counts(site, monkeypatch):
    """The review of #367, its reproduction: a row with a VM whose `repo_status` isn't `ok`
    was skipped, exit 0, and so counted as synced. With `--strict` the run fails for it."""
    roster = pathlib.Path(site.harvard.roster)
    roster.write_text(
        roster.read_text().replace(
            "alice,alice@example.org,alice,198.51.100.1,us-west-2,ok",
            "alice,alice@example.org,alice,198.51.100.1,us-west-2,owner check: ssh: timed out",
        )
    )
    calls = []
    monkeypatch.setattr(final_sync, "subprocess_runner", lambda *a, **k: calls.append(a))
    site.applied("uninstall", "alice")
    (step,) = site.action("!final-sync alice").steps
    exit = run(step.argv)
    assert exit == 1 and calls == []  # it contacts no VM, and fails
    site.applied("final-sync", "alice", exit=exit)
    with pytest.raises(ActionError, match="on record for alice:"):
        site.action("!teardown alice")


@pytest.mark.parametrize("key", ["zab-admin", "ZAB-Admin"])
def test_teardown_never_takes_a_vm_tagged_zab_admin_in_any_case(site, ec2, capsys, key):
    """#354's case-folded rule (`aws_common.not_participant`), through the bot's own lists:
    the admin server, even tagged as a participant's VM, is skipped with its address."""
    ec2.instances["i-alice"]["Tags"].append({"Key": key, "Value": "e2e"})
    ec2.addresses["eipalloc-alice"]["Tags"].append({"Key": key, "Value": "e2e"})
    (step,) = site.action("!teardown alice unsynced").steps
    assert run(step.argv) == 0
    assert "skip i-alice ws-alice: tagged zab-admin" in capsys.readouterr().out
    assert states(ec2)["i-alice"] == "running" and "eipalloc-alice" in ec2.addresses


def test_deactivate_bots_refuses_a_bot_that_isnt_the_provisioners(site, monkeypatch, capsys):
    """The review of #367: a zuliprc naming a bot that isn't among the provisioner's active
    bots (someone else's, such as the kept fleet's, Drew's until the cutover; or one
    deactivated already) fails the row, never `absent` and done."""
    zulip = FakeZulip()
    zulip._bots = [b for b in zulip._bots if b["username"] != "codex-alice-bot@org"]
    monkeypatch.setattr(zulip_provision, "ZulipAdmin", lambda url, email, key: zulip)
    zuliprcs(site, "alice")
    (step,) = site.action("!deactivate-bots alice").steps
    assert run(step.argv) == 1
    assert (
        "out/alice/codex.zuliprc holds codex-alice-bot@org, which is not among the provisioner's "
        "active bots: not owned by the provisioner, or still active?"
    ) in capsys.readouterr().out
    assert zulip.deleted == [zulip.ids["claude-alice-bot@org"]]


@pytest.mark.parametrize("owner", [999, 123])
def test_a_rerun_recovers_after_a_partial_deactivation(site, monkeypatch, capsys, owner):
    """Codex round 2 on #367: Claude's bot deactivated, Codex's deactivation failed. A rerun
    must deactivate Codex's: Claude's, gone from the provisioner's active bots, is found in the
    users listing as that name and e-mail, deactivated and owned by the provisioner (999), and
    reads as done. Owned by someone else (123), it still fails the row."""
    zulip = FakeZulip()
    zulip.me = lambda: {"user_id": 999}
    gone = "claude-alice-bot@org"
    zulip._bots = [b for b in zulip._bots if b["username"] != gone]
    for user in zulip._users:
        if user["email"] == gone:
            user.update(is_active=False, full_name="Claude · alice", bot_owner_id=owner)
    monkeypatch.setattr(zulip_provision, "ZulipAdmin", lambda url, email, key: zulip)
    zuliprcs(site, "alice")
    (step,) = site.action("!deactivate-bots alice").steps
    if owner == 999:
        assert run(step.dry_run) == 0  # the plan's dry run passes again
        assert run(step.argv) == 0
        assert zulip.deleted == [zulip.ids["codex-alice-bot@org"]]
        assert "deactivated already" in capsys.readouterr().out
    else:
        assert run(step.argv) == 1
        assert "not among the provisioner's active bots" in capsys.readouterr().out
        assert zulip.deleted == [zulip.ids["codex-alice-bot@org"]]  # its own bot still goes


@pytest.mark.parametrize("case", ["another name", "still active"])
def test_only_the_named_bot_deactivated_reads_as_done(site, monkeypatch, capsys, case):
    """#376, `_deactivated_own`'s two other checks. **Another name:** alice's zuliprc holds the
    address of another bot the provisioner deactivated already (`Claude · old`), while
    `Claude · alice` is still active: never "deactivated already", which would leave her bot
    and its key working. **Still active:** the provisioner's own `Claude · alice`, active, but
    left out of `GET /bots` (a listing that lags, as on Oct 15, #396): never done either."""
    zulip = FakeZulip()
    zulip.me = lambda: {"user_id": 999}
    held = "claude-old-bot@org" if case == "another name" else "claude-alice-bot@org"
    if case == "another name":
        zulip._users.append({"user_id": 400, "email": held, "is_bot": True, "is_active": False,
                             "full_name": "Claude · old", "bot_owner_id": 999})  # fmt: skip
    else:
        zulip._bots = [b for b in zulip._bots if b["username"] != held]
        for user in zulip._users:
            if user["email"] == held:
                user.update(is_active=True, full_name="Claude · alice", bot_owner_id=999)
    monkeypatch.setattr(zulip_provision, "ZulipAdmin", lambda url, email, key: zulip)
    zuliprcs(
        site, "alice", email=lambda kind, slug: held if kind == "claude" else "codex-alice-bot@org"
    )
    (step,) = site.action("!deactivate-bots alice").steps
    assert run(step.argv) == 1
    shown = capsys.readouterr().out
    assert "deactivated already" not in shown
    assert f"out/alice/claude.zuliprc holds {held}, which is not among the provisioner's" in shown
    assert zulip.deleted == [zulip.ids["codex-alice-bot@org"]]  # Claude · alice is left alone
