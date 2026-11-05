"""`workshop/admin/roster_import.py` (ADMIN §3.1): the roster diff and its refusals.

Synthetic rosters only: fake names, `example.org` addresses, documentation IPs. No test reaches
Zulip, GitHub or EC2: the live checks are fakes, and `Ec2` and `github_token` refuse (`offline`).
"""

from __future__ import annotations

import csv
import io
import json
import os
import stat
import urllib.error
from typing import ClassVar

import pytest
import roster_import as ri
import zulip_provision as zp

HEADER = (
    "slug,first_name,last_name,email,repo,github,host,ip,region,host_keys,zulip_user_id,"
    "zulip_invite,zulip_channel_id,notes,repo_status,github_invite"
)
# alice: a VM, bound to Zulip user 101, her login a collaborator
# bob: a VM, not on Zulip yet (an invitation went out), an open GitHub invitation
# carol: no VM, no login; erin: no VM, a login typed by an organizer, never provisioned
CURRENT = f"""{HEADER}
alice,Alice,Example,alice@example.org,MathAgents/alice,alice-gh,ws-alice,203.0.113.10,us-west-2,SHA256:aa,101,joined,201,,ok,collaborator
bob,Bob,Example,bob@example.org,MathAgents/bob,bob-old,ws-bob,203.0.113.11,us-west-2,SHA256:bb,,sent 2026-10-28 channel 202,202,,ok,pending
carol,Carol,Example,carol@example.org,,,,,,,,,,early leaver,,
erin,Erin,Example,erin@example.org,,erin-gh,,,,,,,,,,
"""  # noqa: E501
SAME = """slug,first_name,last_name,email,notes
alice,Alice,Example,alice@example.org,
bob,Bob,Example,bob@example.org,
carol,Carol,Example,carol@example.org,early leaver
erin,Erin,Example,erin@example.org,
"""
WITHOUT_CAROL = SAME.replace("carol,Carol,Example,carol@example.org,early leaver\n", "")
WITHOUT_ERIN = SAME.replace("erin,Erin,Example,erin@example.org,\n", "")
LOOKUPS = frozenset({"zulip", "invited", "github", "vm", "revoke"})


class FakeChecks:
    def __init__(
        self, users=(), holds=None, *, invited=(), vms=(), no_repo=(), fail=(), during=None
    ):
        self.users = {u.casefold() for u in users}
        self.invited = {u.casefold() for u in invited}
        self.revoked: list[str] = []
        self.vms = set(vms)  # (participant tag, region)
        self.holds = holds or {}  # (slug, login) -> "access" | "invited"
        self.no_repo = set(no_repo)  # slugs whose repository the token does not see
        self.fail = LOOKUPS if fail is True else set(fail)
        self.during = during  # called on every lookup (to edit the roster meanwhile)
        self.calls: list[tuple[str, ...]] = []

    def _lookup(self, kind, *args):
        self.calls.append((kind, *args))
        if self.during:
            self.during()
        if kind in self.fail:
            raise RuntimeError(f"{kind} lookup failed")

    def zulip_user(self, email):
        self._lookup("zulip", email)
        return email.casefold() in self.users

    def zulip_invited(self, email):
        self._lookup("invited", email)
        return email.casefold() in self.invited

    def revoke_invitations(self, email):
        self._lookup("revoke", email)
        self.revoked.append(email)
        self.invited.discard(email.casefold())
        return 1

    def vm(self, tag, region):
        self._lookup("vm", tag, region)
        return (tag, region) in self.vms

    def github_hold(self, slug, login):
        self._lookup("github", slug, login)
        if slug in self.no_repo:
            raise ri.NoRepository(f"the token sees no repository MathAgents/{slug}")
        return self.holds.get((slug, login), "")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """A check left to its live default must fail closed here, never reach AWS or `gh`."""

    def refuse(*args, **kwargs):
        raise RuntimeError("no live lookups in tests")

    monkeypatch.setattr(ri, "Ec2", refuse)
    monkeypatch.setattr(ri, "github_token", refuse)


@pytest.fixture
def paths(tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text(CURRENT)
    roster.chmod(0o600)
    return roster, tmp_path / "upload.csv"


def run(paths, upload, *extra, checks=None):
    roster, up = paths
    up.write_text(upload, encoding="utf-8")
    args = ["--roster", str(roster), "--input", str(up), *extra]
    return ri.main(args, checks=FakeChecks() if checks is None else checks)


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["slug"]: r for r in csv.DictReader(f)}


def edit(text, old, new):
    assert old in text
    return text.replace(old, new)


GOLDEN = """\
roster import: 1 added, 1 changed, 1 removed, 2 unchanged
warning: columns ignored, the scripts fill them: host
warning: columns ignored, not roster input: affiliation
+ dave
    first_name: "Dave"
    last_name: "Example"
    email: "dave@example.org"
~ carol
    first_name: "Carol" -> "Caroline"
    notes: "early leaver" -> ""
- erin
next, new rows: !launch dave, then !provision zulip dave, !provision repos dave and \
!rollout VERSION dave
"""


def test_diff_is_deterministic_in_upload_order(paths, capsys):
    # a spreadsheet export: byte-order mark, capitalised headers, an empty trailing line
    upload = (
        "\ufeffSlug,First_Name,Last_Name,Email,Notes,Host,Affiliation\n"
        "dave,Dave,Example,dave@example.org,,,Example University\n"
        "carol,Caroline,Example,carol@example.org,,,\n"
        "alice,Alice,Example,ALICE@example.org,,ws-elsewhere,\n"  # case only: no change
        "bob,Bob,Example,bob@example.org,,,\n"
        ",,,,,,\n"
    )
    outputs = []
    for _ in range(2):
        assert run(paths, upload, "--dry-run") == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1] == GOLDEN + "dry run: nothing written\n"
    assert paths[0].read_text() == CURRENT
    assert run(paths, upload) == 0
    assert capsys.readouterr().out.startswith(GOLDEN + "written: ")
    assert list(rows(paths[0])) == ["dave", "carol", "alice", "bob"]


def test_unchanged_upload_writes_nothing(paths, capsys):
    """#708: its dry run exits 3, the admin bot's `NOTHING`, so the bot offers no plan; the
    apply, which writes nothing either, exits 0 as before."""
    from zab.admin.actions import NOTHING

    assert run(paths, SAME, "--dry-run") == ri.NOTHING == NOTHING
    assert capsys.readouterr().out.endswith("no changes: nothing to write\n")
    assert run(paths, SAME) == 0
    assert "no changes: nothing to write" in capsys.readouterr().out
    assert paths[0].read_text() == CURRENT
    assert not (paths[0].parent / "roster.csv.bak").exists()


def test_keeps_script_filled_columns_and_writes_atomically_with_bak(paths, capsys):
    upload = (
        "slug,first_name,last_name,email,notes,repo,github,region,host,ip,zulip_user_id,"
        "repo_status\n"
        "alice,Alice,Example,alice@example.org,moved rooms,,,,ws-x,198.51.100.1,999,failed\n"
        "bob,Bob,Example,bob@example.org,,,,,,,,\n"
        "carol,Carol,Example,carol@example.org,early leaver,,,,,,,\n"
        "erin,Erin,Example,erin@example.org,,,,,,,,\n"
    )
    old = {slug: dict(row) for slug, row in rows(paths[0]).items()}
    assert run(paths, upload) == 0
    out = capsys.readouterr().out
    assert "columns ignored, the scripts fill them: host, ip, repo_status, zulip_user_id" in out
    assert '    notes: "" -> "moved rooms"' in out
    new = rows(paths[0])
    assert new["alice"] == {**old["alice"], "notes": "moved rooms"}
    assert new["bob"] == old["bob"]
    # atomic, with the previous version kept and the roster's mode preserved (personal data)
    bak = paths[0].parent / "roster.csv.bak"
    assert bak.read_text() == CURRENT
    assert stat.S_IMODE(os.stat(paths[0]).st_mode) == 0o600
    assert sorted(p.name for p in paths[0].parent.iterdir()) == [
        "roster.csv",
        "roster.csv.bak",
        "upload.csv",
    ]


def test_a_new_row_ignores_script_columns(paths, capsys):
    """A new row carrying a removed row's `zulip_channel_id` would be handed that sandbox
    through `find_channel` (reviewer on #317)."""
    script = sorted(ri.SCRIPT_COLUMNS)
    upload = (
        f"slug,first_name,last_name,email,notes,{','.join(script)}\n"
        + "".join(line + "," * len(script) + "\n" for line in SAME.splitlines()[1:])
        + "frank,Frank,Example,frank@example.org,,"
        + ",".join(f"{c}-of-alice" for c in script)
        + "\n"
    )
    assert run(paths, upload) == 0
    out = capsys.readouterr().out
    assert '+ frank\n    first_name: "Frank"\n    last_name: "Example"\n    email: ' in out
    frank = rows(paths[0])["frank"]
    assert {c: frank.get(c, "") for c in script} == dict.fromkeys(script, "")
    assert rows(paths[0])["alice"]["zulip_channel_id"] == "201"


def test_first_import_creates_the_roster(tmp_path, capsys):
    roster, up = tmp_path / "roster.csv", tmp_path / "upload.csv"
    upload = (
        "slug,first_name,last_name,email\n"
        "zab-test-a,Ada,Example,ada@example.org\nzab-test-b,Ben,Example,ben@example.org\n"
    )
    assert run((roster, up), upload, "--dry-run") == 0
    assert not roster.exists()
    assert run((roster, up), upload) == 0
    assert "2 added, 0 changed, 0 removed, 0 unchanged" in capsys.readouterr().out
    assert stat.S_IMODE(os.stat(roster).st_mode) == 0o600
    assert roster.read_text().splitlines()[0] == ",".join(ri.ROSTER_COLUMNS)
    assert list(rows(roster)) == ["zab-test-a", "zab-test-b"]
    assert not (tmp_path / "roster.csv.bak").exists()


@pytest.mark.parametrize(
    ("upload", "code", "reason"),
    [
        (SAME + "alice,Alicia,Other,alicia@example.org,\n", 2, "duplicate slug"),
        (SAME + "frank,Frank,Example,BOB@example.org,\n", 2, "duplicate email"),
        (SAME + "zab-spare-2,Spare,Example,spare@example.org,\n", 2, "spare VM names"),
        (SAME + "Frank,Frank,Example,frank@example.org,\n", 2, "invalid slug"),
        (SAME.replace(",notes\n", ",notes,github\n").replace("leaver\n", "leaver,alice-gh\n"), 1,
         "duplicate github"),
    ],
)  # fmt: skip
def test_refuses_duplicates_and_spare_slugs(paths, capsys, upload, code, reason):
    assert run(paths, upload) == code
    assert reason in capsys.readouterr().err
    assert paths[0].read_text() == CURRENT


# ------------------------------------------------------------------------------- removals


def test_refuses_removing_or_renaming_a_launched_row(paths, capsys):
    without_bob = edit(SAME, "bob,Bob,Example,bob@example.org,\n", "")
    assert run(paths, without_bob) == 1
    err = capsys.readouterr().err
    assert "refused: bob: has a VM (ws-bob): removing or renaming it is refused" in err
    assert "nothing written" in err
    renamed = edit(SAME, "alice,Alice", "alice2,Alice")
    assert run(paths, renamed) == 1
    assert "alice: has a VM (ws-alice, renamed to alice2?)" in capsys.readouterr().err
    assert paths[0].read_text() == CURRENT
    # a row with no VM, nobody on Zulip and no invitation may be renamed (or removed: erin)
    checks = FakeChecks()
    assert run(paths, edit(SAME, "carol,Carol", "caro,Carol"), "--dry-run", checks=checks) == 0
    out = capsys.readouterr().out
    assert "+ caro\n" in out and "- carol\n" in out
    assert checks.calls == [
        ("vm", "carol", "us-west-2"),
        ("zulip", "carol@example.org"),
        ("invited", "carol@example.org"),
    ]


def test_refuses_removing_or_renaming_a_bound_row_without_a_vm(paths, capsys):
    """Its slug, re-added for someone else, would get `<slug>-sandbox` and its history."""
    bound = edit(CURRENT, "carol@example.org,,,,,,,,,,", "carol@example.org,,,,,,,103,joined,203,")
    paths[0].write_text(bound)
    checks = FakeChecks()
    assert run(paths, WITHOUT_CAROL, checks=checks) == 1
    err = capsys.readouterr().err
    assert (
        "refused: carol: bound to Zulip user 103: removing or renaming it is refused (a new row "
        "with its slug would inherit carol-sandbox and its history); remove it by hand after "
        "teardown"
    ) in err
    assert checks.calls == []  # the roster alone decides it
    assert run(paths, edit(SAME, "carol,Carol", "caro,Carol")) == 1
    assert "carol: bound to Zulip user 103, renamed to caro?" in capsys.readouterr().err
    assert paths[0].read_text() == bound
    assert not (paths[0].parent / "roster.csv.bak").exists()


def test_joined_without_an_id_counts_as_bound(tmp_path, capsys):
    """The reviewer's reproduction on #317: a run without `--no-rebind` cleared the id of a
    user it no longer matched, and `zulip_invite = joined` stayed. Nobody has the address in
    the listing, yet the row is the participant's."""
    current = f"""{HEADER}
alice,Alice,Ex,alice@example.org,,,,,,,,joined,201,,,
bob,Bob,Ex,bob@example.org,,,,,,,,,,,,
"""
    paths = (tmp_path / "roster.csv", tmp_path / "upload.csv")
    paths[0].write_text(current)
    checks = FakeChecks()
    upload = "slug,first_name,last_name,email\nalice,Alice,Ex,stranger@example.net\n"
    assert run(paths, upload + "bob,Bob,Ex,bob@example.org\n", checks=checks) == 1
    assert (
        "alice: e-mail change of a row recorded as joined (zulip_invite): a rebind"
        in capsys.readouterr().err
    )
    assert run(paths, "slug,first_name,last_name,email\nbob,Bob,Ex,bob@example.org\n") == 1
    assert (
        "alice: recorded as joined (zulip_invite): removing or renaming it is refused (a new row "
        "with its slug would inherit alice-sandbox"
    ) in capsys.readouterr().err
    assert checks.calls == []
    assert paths[0].read_text() == current
    # a hand-edited id is no proof of anything either way
    paths[0].write_text(current.replace(",,joined,201", ",x1,,201"))
    assert run(paths, upload + "bob,Bob,Ex,bob@example.org\n") == 1
    assert "alice: e-mail change: cannot check the old address: invalid zulip_user_id 'x1'" in (
        capsys.readouterr().err
    )


def test_refuses_removing_a_row_whose_participant_joined_unrecorded(paths, capsys):
    """Joined through the invitation before `zulip_provision.py` recorded the user: only the
    live lookup sees it (codex round 4)."""
    checks = FakeChecks(users=["Carol@example.org"])
    assert run(paths, WITHOUT_CAROL, checks=checks) == 1
    assert (
        "refused: carol: a Zulip user has its address: removing or renaming it is refused (a new "
        "row with its slug would inherit carol-sandbox and its history)"
    ) in capsys.readouterr().err
    assert checks.calls == [("vm", "carol", "us-west-2"), ("zulip", "carol@example.org")]
    assert run(paths, edit(SAME, "carol,Carol", "caro,Carol"), checks=checks) == 1
    assert "carol: a Zulip user has its address, renamed to caro?" in capsys.readouterr().err
    # a lookup that cannot be made refuses (fail closed)
    for kind in ("vm", "zulip", "invited"):
        assert run(paths, WITHOUT_CAROL, checks=FakeChecks(fail={kind})) == 1
        assert (
            f"carol: removing it: cannot check its VM, address and login: {kind} lookup failed"
            in (capsys.readouterr().err)
        )
    no_zuliprc = ri.LiveChecks(None, "MathAgents", {"workshop": "ws"}, make_ec2=FakeEc2)
    assert run(paths, WITHOUT_CAROL, checks=no_zuliprc) == 1
    assert "carol: removing it: cannot check its VM, address and login: no --zuliprc" in (
        capsys.readouterr().err
    )
    assert paths[0].read_text() == CURRENT


def test_refuses_removing_a_row_with_an_open_invitation(paths, capsys):
    """Its holder, joining later, would get the sandbox of whoever has the slug then. Only an
    e-mail change revokes the invitation itself (#322)."""
    checks = FakeChecks(invited=["erin@example.org"])
    assert run(paths, WITHOUT_ERIN, checks=checks) == 1
    assert (
        "refused: erin: an invitation to its address is open: its sender or a Zulip "
        "administrator revokes it first (Organization settings → Users → Invitations), then "
        "import again"
    ) in capsys.readouterr().err
    assert paths[0].read_text() == CURRENT and checks.revoked == []


def test_refuses_removing_a_row_whose_launch_failed_half_way(paths, capsys):
    """`launch.py` records `region` right after `run_instances`; a later failure leaves no
    `host` or `ip`. Only EC2 knows (reviewer on #317)."""
    paths[0].write_text(edit(CURRENT, "erin-gh,,,,", "erin-gh,,,us-east-2,"))
    checks = FakeChecks(vms={("erin", "us-east-2")})
    assert run(paths, WITHOUT_ERIN, checks=checks) == 1
    assert (
        "refused: erin: a VM tagged participant=erin is in us-east-2: removing or renaming it is "
        "refused; tear it down first"
    ) in capsys.readouterr().err
    assert checks.calls == [("vm", "erin", "us-east-2")]
    # a row without a region is looked up where launch.py would have put it: --region
    checks = FakeChecks(vms={("carol", "eu-west-1")})
    assert run(paths, WITHOUT_CAROL, "--region", "eu-west-1", checks=checks) == 1
    assert "carol: a VM tagged participant=carol is in eu-west-1" in capsys.readouterr().err
    # a row from before slugs: its VM may carry the login, with its capitals (launch.legacy_tags)
    paths[0].write_text(CURRENT + ",Gina,Example,gina@example.org,,Gina-GH,,,,,,,,,,\n")
    checks = FakeChecks(vms={("Gina-GH", "us-west-2")})
    assert run(paths, SAME, checks=checks) == 1
    assert "gina-gh: a VM tagged participant=Gina-GH is in us-west-2" in capsys.readouterr().err
    assert checks.calls == [("vm", "gina-gh", "us-west-2"), ("vm", "Gina-GH", "us-west-2")]


def test_refuses_removing_a_row_whose_repository_or_login_holds_anything(paths, capsys):
    """Re-added for someone else, the slug would get `<org>/<slug>`, which `provision_repos.py`
    reuses with the work in it, and the old login's access or invitation (codex round 6)."""
    # codex's reproduction: provisioned (repo_status ok, an invitation pending), no VM, no Zulip
    gwen = "gwen,Gwen,Example,gwen@example.org,MathAgents/gwen,gwen-gh,,,,,,,,,ok,pending\n"
    paths[0].write_text(CURRENT + gwen)
    checks = FakeChecks()
    assert run(paths, SAME, checks=checks) == 1
    assert (
        "refused: gwen: provision_repos.py ran for it: removing or renaming it is refused (a new "
        "row with its slug would inherit its repository and the work in it); remove it by hand "
        "after teardown"
    ) in capsys.readouterr().err
    assert checks.calls == []  # the roster alone decides it
    # never provisioned, yet the recorded login holds something on the repository
    paths[0].write_text(CURRENT)
    for held, what in (("access", "has access to"), ("invited", "has an open invitation to")):
        checks = FakeChecks(holds={("erin", "erin-gh"): held})
        assert run(paths, WITHOUT_ERIN, checks=checks) == 1
        assert f"erin: its recorded login erin-gh {what} its repository" in capsys.readouterr().err
        assert checks.calls[-1] == ("github", "erin", "erin-gh")
    assert run(paths, WITHOUT_ERIN, checks=FakeChecks(fail={"github"})) == 1
    assert "erin: removing it: cannot check its VM, address and login: github lookup failed" in (
        capsys.readouterr().err
    )
    # no repository, and provision_repos.py never ran: nothing is held
    assert run(paths, WITHOUT_ERIN, "--dry-run", checks=FakeChecks(no_repo={"erin"})) == 0
    assert "- erin\n" in capsys.readouterr().out


def test_a_malformed_recorded_login_proves_nothing(paths, capsys):
    """`provision_repos.grant_access(no_rebind=True)` refuses one; overwriting it unchecked would
    bypass that (codex round 6)."""
    paths[0].write_text(edit(CURRENT, ",erin-gh,", ",@erin-old,"))
    base = SAME.replace(",notes\n", ",notes,github\n")
    upload = edit(base, "erin@example.org,", "erin@example.org,,erin-new")
    checks = FakeChecks()
    assert run(paths, upload, checks=checks) == 1
    assert (
        "erin: GitHub change: cannot check @erin-old: not a GitHub login: a deployer fixes it in "
        "the roster first"
    ) in capsys.readouterr().err
    assert checks.calls == []  # never sent to GitHub
    assert run(paths, WITHOUT_ERIN, checks=FakeChecks()) == 1
    assert "erin: removing it: cannot check its VM, address and login: not a GitHub login" in (
        capsys.readouterr().err
    )


# --------------------------------------------------------------------------------- regions


def test_regions(paths, capsys):
    regions = "".join(line + ",\n" for line in SAME.splitlines()).replace(
        ",notes,", ",notes,region"
    )
    # empty cells keep the recorded regions
    assert run(paths, regions) == 0
    assert "no changes" in capsys.readouterr().out
    # a workshop has one region (ADMIN §2.6): a row may name no other
    to_east = edit(regions, "early leaver,", "early leaver,us-east-2")
    assert run(paths, to_east) == 1
    assert "carol: region us-east-2 is not the workshop's, us-west-2" in capsys.readouterr().err
    # a VM's recorded region never changes
    moved = edit(regions, "alice@example.org,,", "alice@example.org,,us-east-2")
    assert run(paths, moved, "--region", "us-east-2") == 1
    assert "alice: has a VM: its recorded region never changes" in capsys.readouterr().err
    checks = FakeChecks()
    assert run(paths, to_east, "--region", "us-east-2", checks=checks) == 0
    assert '    region: "" -> "us-east-2"  (plain change: no VM in us-east-2)' in (
        capsys.readouterr().out
    )
    assert checks.calls == [("vm", "carol", "us-east-2")]
    assert rows(paths[0])["carol"]["region"] == "us-east-2"
    # a recorded region proves nothing about a VM: EC2 decides, in that region
    back = edit(regions, "early leaver,", "early leaver,us-west-2")
    assert run(paths, back, checks=FakeChecks(vms={("carol", "us-east-2")})) == 1
    assert (
        "carol: a VM tagged participant=carol is in us-east-2: its region never changes"
        in capsys.readouterr().err
    )
    assert run(paths, back, checks=FakeChecks(fail={"vm"})) == 1
    assert "carol: region change: cannot look its VM up: vm lookup failed" in (
        capsys.readouterr().err
    )
    assert run(paths, edit(regions, "erin@example.org,,", "erin@example.org,,us east")) == 2
    assert "erin: invalid region 'us east'" in capsys.readouterr().err


# --------------------------------------------------------------------------------- e-mails


def test_email_change_of_a_bound_row_is_a_rebind(paths, capsys):
    checks = FakeChecks()
    assert run(paths, edit(SAME, "alice@example.org", "alice.new@example.org"), checks=checks) == 1
    err = capsys.readouterr().err
    assert "alice: e-mail change of a row bound to Zulip user 101: a rebind" in err
    assert checks.calls == []  # the roster alone decides it
    assert paths[0].read_text() == CURRENT


def test_email_change_when_a_zulip_user_has_the_old_address_is_a_rebind(paths, capsys):
    checks = FakeChecks(users=["BOB@example.org"])
    assert run(paths, edit(SAME, "bob@example.org", "bob.b@example.org"), checks=checks) == 1
    assert (
        "bob: e-mail change: a Zulip user has the old address: a rebind" in capsys.readouterr().err
    )
    assert checks.calls == [("zulip", "bob@example.org")]
    assert paths[0].read_text() == CURRENT


REVOKES = "(not on Zulip; the apply first revokes the open invitation to the old address)"


def test_email_change_revokes_an_open_invitation_to_the_old_address(paths, capsys):
    """Until `!rollout`, the VM's `allow_emails` still admits the old address; joining, its
    holder would also get the sandbox (reviewer on #317). The apply revokes the invitation
    before it writes the roster, which drops its record; the dry run revokes nothing (#322)."""
    upload = edit(SAME, "bob@example.org", "bob.b@example.org")
    checks = FakeChecks(invited=["bob@example.org"])
    assert run(paths, upload, "--dry-run", checks=checks) == 0
    out = capsys.readouterr().out
    assert f'    email: "bob@example.org" -> "bob.b@example.org"  {REVOKES}\n' in out
    assert '    zulip_invite: "sent 2026-10-28 channel 202" -> ""  (the old address\'s)\n' in out
    assert "dry run: nothing written" in out and "revoked" not in out
    assert checks.revoked == [] and paths[0].read_text() == CURRENT
    assert run(paths, upload, checks=checks) == 0
    out = capsys.readouterr().out
    assert "revoked: bob: 1 open invitation(s) to the old address\nwritten: " in out
    assert checks.revoked == ["bob@example.org"]
    new = rows(paths[0])
    assert (new["bob"]["email"], new["bob"]["zulip_invite"]) == ("bob.b@example.org", "")


def test_an_invitation_the_apply_cannot_revoke_refuses_the_import(paths, capsys):
    """Fail visibly (#322): the roster keeps the old address and its record. A revocation made
    before the failure stays, and is said; nothing is revoked when the roster changed."""

    class FailsForCarol(FakeChecks):
        def revoke_invitations(self, email):
            if email == "carol@example.org":
                raise RuntimeError("Zulip BAD_REQUEST (HTTP 400): No such invitation")
            return super().revoke_invitations(email)

    upload = edit(SAME, "bob@example.org", "bob.b@example.org")
    upload = edit(upload, "carol@example.org", "carol.c@example.org")
    checks = FailsForCarol(invited=["bob@example.org", "carol@example.org"])
    assert run(paths, upload, checks=checks) == 1
    captured = capsys.readouterr()
    assert "revoked: bob: 1 open invitation(s) to the old address\n" in captured.out
    assert (
        "refused: carol: cannot revoke the invitation to the old address: Zulip BAD_REQUEST "
        "(HTTP 400): No such invitation; its sender or a Zulip administrator revokes it first "
        "(Organization settings → Users → Invitations), then import again\nnothing written\n"
    ) in captured.err
    assert paths[0].read_text() == CURRENT and checks.revoked == ["bob@example.org"]

    def meanwhile():
        paths[0].write_text(CURRENT + "\n")

    checks = FakeChecks(invited=["bob@example.org"], during=meanwhile)
    assert run(paths, edit(SAME, "bob@example.org", "bob.b@example.org"), checks=checks) == 1
    assert "the roster changed during the import" in capsys.readouterr().err
    assert checks.revoked == []


def test_another_workshops_invitation_to_the_old_address_is_never_revoked(paths, tmp_path, capsys):
    """Review of #576: the provisioner's key is the server's, so `GET /invites` lists the other
    workshops' invitations too, and an address is unique only within a workshop. With another
    workshop's row at the old address, whose invitation is open can't be told, so the change is
    refused, for a deployer, whether or not this row records one: carol records none, and yet
    this workshop's may be open, sent with its answer lost (Codex round 1 on #576); bob records
    his own. Nothing is revoked, and an address no other workshop has is revoked as before."""
    other = tmp_path / "harvard.csv"
    other.write_text(
        "slug,first_name,email,zulip_invite,zulip_channel_id\n"
        "zoe,Zoe,Carol@example.org,sent 2026-10-28 channel 900,900\n"
        "yan,Yan,bob@example.org,,\n"
    )
    ws = ("--workshop-roster", f"harvard-2026={other}")
    for who in ("carol", "bob"):
        upload = edit(SAME, f"{who}@example.org", f"{who}.x@example.org")
        checks = FakeChecks(invited=[f"{who}@example.org"])
        assert run(paths, upload, *ws, checks=checks) == 1
        assert (
            f"refused: {who}: e-mail change: an invitation to the old address is open, and "
            "harvard-2026 has a row with that address: whose invitation it is can't be told "
            "here, so a deployer makes this change by hand"
        ) in capsys.readouterr().err
        assert checks.revoked == [] and paths[0].read_text() == CURRENT
    other.write_text("slug,first_name,email\nzoe,Zoe,zoe@example.org\n")  # no shared address
    checks = FakeChecks(invited=["carol@example.org"])
    upload = edit(SAME, "carol@example.org", "carol.x@example.org")
    assert run(paths, upload, *ws, checks=checks) == 0
    assert checks.revoked == ["carol@example.org"]


def test_email_change_of_an_unbound_unknown_address_is_a_plain_change(paths, capsys):
    """The day-of fix (ADMIN §2.5): nobody on Zulip has the old address, and no invitation to
    it is open."""
    upload = edit(SAME, "bob@example.org", "bob.b@example.org")
    upload = edit(upload, "carol@example.org", "carol.c@example.org")
    checks = FakeChecks(users=["bob.b@example.org"])  # bob joined under the right address
    assert run(paths, upload, checks=checks) == 0
    out = capsys.readouterr().out
    plain = (
        "(plain change: not on Zulip, and no open invitation to the old address that the "
        "provisioner sees)"
    )
    assert f'    email: "bob@example.org" -> "bob.b@example.org"  {plain}\n' in out
    assert f'    email: "carol@example.org" -> "carol.c@example.org"  {plain}\n' in out
    assert "next, new e-mails: !provision zulip bob carol, then !rollout VERSION bob carol" in out
    assert checks.calls == [
        ("zulip", "bob@example.org"),
        ("invited", "bob@example.org"),
        ("zulip", "carol@example.org"),
        ("invited", "carol@example.org"),
    ]
    assert '    zulip_invite: "sent 2026-10-28 channel 202" -> ""  (the old address\'s)\n' in out
    new = rows(paths[0])
    assert (new["bob"]["email"], new["bob"]["zulip_invite"]) == ("bob.b@example.org", "")
    assert new["bob"]["zulip_channel_id"] == "202"  # the sandbox stays the row's
    # provisioning then reads no invitation (#628): bob is `not on Zulip` until he joins
    report = zp.Report(slug="bob")
    zp.invite_state(new["bob"], report)
    assert (report.invite, report.email, report.notes) == ("not on Zulip", "bob.b@example.org", [])


def test_unchecked_email_change_fails_closed(paths, capsys):
    upload = edit(SAME, "carol@example.org", "carol.c@example.org")
    no_zuliprc = ri.LiveChecks(None, "MathAgents", {"workshop": "ws"}, make_ec2=FakeEc2)
    assert run(paths, upload, checks=no_zuliprc) == 1
    assert "carol: e-mail change: cannot check the old address: no --zuliprc" in (
        capsys.readouterr().err
    )
    for kind in ("zulip", "invited"):
        assert run(paths, upload, checks=FakeChecks(fail={kind})) == 1
        assert f"cannot check the old address: {kind} lookup failed" in capsys.readouterr().err
    assert paths[0].read_text() == CURRENT


# --------------------------------------------------------------------------------- GitHub


def test_github_change_is_refused_while_the_old_login_holds_anything(paths, capsys):
    base = SAME.replace(",notes\n", ",notes,github\n")
    holds = {("alice", "alice-gh"): "access", ("bob", "bob-old"): "invited"}
    upload = edit(base, "alice@example.org,", "alice@example.org,,alice-new")
    assert run(paths, upload, checks=FakeChecks(holds=holds)) == 1
    err = capsys.readouterr().err
    assert "alice: GitHub change: alice-gh has access to the repository: a rebind" in err
    # an open invitation: provision_repos.py withdraws it only while the roster records the login
    upload = edit(base, "bob@example.org,", "bob@example.org,,bob-gh")
    assert run(paths, upload, checks=FakeChecks(holds=holds)) == 1
    assert "bob: GitHub change: bob-old has an open invitation to the repository: withdraw " \
        "it first" in capsys.readouterr().err  # fmt: skip
    # a repository the token does not see proves nothing once provision_repos.py ran
    assert run(paths, upload, checks=FakeChecks(no_repo={"bob"})) == 1
    assert "bob: GitHub change: cannot check bob-old: the token sees no repository " \
        "MathAgents/bob" in capsys.readouterr().err  # fmt: skip
    # nor does a lookup that fails (reviewer on #317: no plan-level test fed one)
    assert run(paths, upload, checks=FakeChecks(fail={"github"})) == 1
    assert "bob: GitHub change: cannot check bob-old: github lookup failed" in (
        capsys.readouterr().err
    )
    assert paths[0].read_text() == CURRENT


def test_github_change_is_a_plain_change_when_the_old_login_holds_nothing(paths, capsys):
    base = SAME.replace(",notes\n", ",notes,github\n")
    upload = edit(base, "bob@example.org,", "bob@example.org,,bob-gh")  # invitation withdrawn
    upload = edit(upload, "early leaver", "early leaver,carol-gh")
    upload = edit(upload, "erin@example.org,", "erin@example.org,,erin-real")
    upload = edit(upload, "alice@example.org,", "alice@example.org,,ALICE-GH")  # case only
    checks = FakeChecks(holds={("alice", "alice-gh"): "access"}, no_repo={"erin"})
    assert run(paths, upload, checks=checks) == 0
    out = capsys.readouterr().out
    assert '    github: "bob-old" -> "bob-gh"  (plain change: bob-old has no access and no ' \
        "open invitation)" in out  # fmt: skip
    assert '    github: "" -> "carol-gh"  (plain change: no login recorded)' in out
    assert '    github: "erin-gh" -> "erin-real"  (plain change: no repository yet, and ' \
        "provision_repos.py never ran for the row)" in out  # fmt: skip
    assert "next, new GitHub logins: !provision repos bob carol erin" in out
    assert "~ alice" not in out
    assert checks.calls == [("github", "bob", "bob-old"), ("github", "erin", "erin-gh")]
    assert rows(paths[0])["alice"]["github"] == "alice-gh"
    assert run(paths, edit(base, "erin@example.org,", "erin@example.org,,-bad")) == 2
    assert "erin: invalid GitHub login '-bad'" in capsys.readouterr().err


def test_a_login_given_as_a_handle_or_a_profile_url_is_the_login(paths, capsys):
    """`@alice` and `https://github.com/alice/` are what organizers copy; the import reads the
    login, as provision_repos.py reads the Zulip field (`normalize_login`, #327)."""
    base = SAME.replace(",notes\n", ",notes,github\n")
    upload = edit(base, "alice@example.org,", "alice@example.org,,@alice-gh")  # the recorded one
    upload = edit(upload, "early leaver", "early leaver,https://github.com/carol-gh/")
    upload += "dave,Dave,Example,dave@example.org,, @Dave-GH \n"
    checks = FakeChecks()
    assert run(paths, upload, checks=checks) == 0
    out = capsys.readouterr().out
    assert "~ alice" not in out and checks.calls == []
    assert '    github: "" -> "carol-gh"  (plain change: no login recorded)' in out
    assert '    github: "Dave-GH"\n' in out
    new = rows(paths[0])
    assert [new[s]["github"] for s in ("alice", "carol", "dave")] == [
        "alice-gh",
        "carol-gh",
        "Dave-GH",
    ]
    paths[0].write_text(CURRENT)
    assert run(paths, edit(base, "erin@example.org,", "erin@example.org,,@")) == 0  # empty: kept
    assert "no changes: nothing to write" in capsys.readouterr().out
    assert run(paths, edit(base, "erin@example.org,", "erin@example.org,,github.com/a/b")) == 2
    assert "erin: invalid GitHub login 'a/b'" in capsys.readouterr().err


# ------------------------------------------------------------------------- upload and diff


@pytest.mark.parametrize(
    ("upload", "reason"),
    [
        ("slug,first_name,email\n,Nobody,nobody@example.org\n", "row 1: no slug"),
        # `slug_given` is `read_roster`'s own key: an uploaded column must not stand in for the
        # slug, nor may a slug come from `github` (reviewer on #317)
        ("slug,email,slug_given\n,x@example.org,1\n", "row 1: no slug"),
        ("slug,email,slug_given,github\n,y@example.org,1,Zed-GH\n", "row 1: no slug"),
        # words the admin grammar reads as keywords where a slug may stand (#323)
        ("slug,email\nall,a@example.org\n", "row 1: the slug 'all' is a keyword"),
        ("slug,email\nlean-shared,l@example.org\n", "the slug 'lean-shared' is a keyword"),
        ("slug,first_name,email\nfrank,Frank,\n", "frank: no e-mail"),
        ("slug,first_name\nfrank,Frank\n", "no email column"),
        ("slug,email,Email\nfrank,f@example.org,g@example.org\n", "a column name appears twice"),
        ("slug,email\nfrank,f@example\0.org\n", "row 1: a control character in email"),
        ("slug,email,notes\nfrank,f@example.org,a\x1b[2Jb\n", "control character in notes"),
        ("slug,email,notes\nfrank,f@example.org," + "x" * 200_000 + "\n", "field larger"),
        ("slug,email,notes\n" + "frank,f@example.org," + "x" * ri.MAX_UPLOAD_BYTES + "\n",
         "larger than"),
    ],
)  # fmt: skip
def test_refuses_malformed_uploads(paths, capsys, upload, reason):
    assert run(paths, upload) == 2
    assert reason in capsys.readouterr().err
    assert paths[0].read_text() == CURRENT


def test_a_new_row_may_not_take_another_workshops_or_a_retired_slug(paths, tmp_path, capsys):
    """Slugs are unique on the server for its whole life: reuse would hand the newcomer
    `<slug>-sandbox` and `<org>/<slug>` (ADMIN §2.1)."""
    retired = tmp_path / "retired-slugs.txt"
    retired.write_text("# nt-agents-2026, retired\nzoe\n\nOld-Slug  # by hand\n")
    other = tmp_path / "other.csv"
    other.write_text("slug,first_name,last_name,email\nyan,Yan,Example,yan@example.org\n")
    args = ("--retired", str(retired), "--workshop-roster", f"harvard-2026={other}")
    upload = SAME + (
        "zoe,Zoe,Example,zoe@example.org,\n"
        "yan,Yan,Other,yan.other@example.org,\n"
        "old-slug,Olga,Example,olga@example.org,\n"
    )
    assert run(paths, upload, *args) == 1
    err = capsys.readouterr().err
    assert "refused: zoe: a retired workshop's slug, never reused on the server" in err
    assert "refused: old-slug: a retired workshop's slug" in err
    assert (  # the workshop only: the bot adds its own hint (#347)
        "refused: yan is in harvard-2026; a slug is never shared between workshops (ADMIN §2.1)"
    ) in err
    assert ": ask " not in err and "@_**" not in err
    # what the admin bot reads in this very output to add its hint (`planning._TAKEN`, the
    # review of #506): the taken slug and its workshop, and no retired slug's line
    from zab.admin.planning import _TAKEN

    assert [found.group("slug", "ws") for found in _TAKEN.finditer(err)] == [
        ("yan", "harvard-2026")
    ]
    # a rename into one is a new row too; the workshop's own rows are not new
    assert run(paths, edit(SAME, "carol,Carol", "zoe,Carol"), *args) == 1
    assert "zoe: a retired workshop's slug" in capsys.readouterr().err
    assert run(paths, SAME, *args) == 0
    # inputs that cannot be read refuse everything
    assert run(paths, SAME, "--retired", str(tmp_path / "missing.txt")) == 2
    assert run(paths, SAME, "--workshop-roster", f"Bad Name={other}") == 2
    assert "expected WS=ROSTER" in capsys.readouterr().err
    assert paths[0].read_text() == CURRENT


def test_reserved_slugs_are_the_grammars():
    """A copy of `zab.admin.grammar.RESERVED`: the `workshop/` scripts do not import `zab.admin`.
    The second half runs once the grammar (#323) is on the branch."""
    assert ri.RESERVED_SLUGS == {
        "all", "spares", "hold", "protect", "lean-shared", "restart-codex", "invites-only",
        "no-invite", "unsynced", "steer", "idle-stop", "multi-party", "raw-log", "export",
    }  # fmt: skip
    for slug in ri.RESERVED_SLUGS:  # each passes the slug pattern: only this check stops it
        ri.validate_row({"slug": slug})
    grammar = pytest.importorskip("zab.admin.grammar")
    assert ri.RESERVED_SLUGS == grammar.RESERVED


def test_the_diff_shows_every_invisible_character(paths, capsys):
    """A participant-supplied name or note must not reorder or break the lines an organizer
    approves (reviewer on #317): line separators, bidi controls, zero-width characters."""
    upload = SAME.replace(",notes\n", ",notes,x\u202eevil\n", 1) + (
        "dave,Dave\u2028- alice,Ex,dave@exam\u200bple.org,x\u202egro.elpmaxe@evE\u202c\n"
    )
    assert run(paths, upload, "--dry-run") == 0
    out = capsys.readouterr().out
    assert '    first_name: "Dave\\u2028- alice"\n' in out
    assert '    email: "dave@exam\\u200bple.org"\n' in out
    assert '    notes: "x\\u202egro.elpmaxe@evE\\u202c"\n' in out
    assert "not roster input: x\\u202eevil" in out
    assert not any(ch in out for ch in "\u2028\u2029\u200b\u202c\u202e")


def test_refuses_when_the_roster_changes_during_the_import(paths, capsys):
    def meanwhile():
        if paths[0].read_text() == CURRENT:  # another script records a launch
            paths[0].write_text(edit(CURRENT, "erin-gh,,,", "erin-gh,ws-erin,203.0.113.12,"))

    upload = edit(SAME, "carol@example.org", "carol.c@example.org")
    assert run(paths, upload, checks=FakeChecks(during=meanwhile)) == 1
    assert "the roster changed during the import; nothing written" in capsys.readouterr().err
    assert "ws-erin" in paths[0].read_text()
    assert not (paths[0].parent / "roster.csv.bak").exists()


# ------------------------------------------------------------------------- the live checks


class FakeZulipAdmin:
    instances: ClassVar[list[FakeZulipAdmin]] = []
    users_answer: ClassVar[list[dict]] = []
    refuse: ClassVar[set[int]] = set()  # invitation ids `DELETE /invites/{id}` refuses

    def __init__(self, site, email, key):
        self.listed, self.closed, self.revoked = 0, False, []
        FakeZulipAdmin.instances.append(self)

    def users(self):
        self.listed += 1
        return FakeZulipAdmin.users_answer

    def invites(self):
        return [
            {"id": 5, "email": "Pending@example.org", "expiry_date": None, "is_multiuse": False},
            {"id": 6, "email": "gone@example.org", "expiry_date": 1, "is_multiuse": False},  # past
            {"id": 7, "email": "", "expiry_date": None, "is_multiuse": True},
            {"id": 8, "email": "pending@example.org", "expiry_date": None, "is_multiuse": False},
        ]

    def revoke_invite(self, invite_id):
        if invite_id in FakeZulipAdmin.refuse:
            raise zp.ZulipAdminError("BAD_REQUEST", 400, "Must be an organization administrator")
        self.revoked.append(invite_id)

    def close(self):
        self.closed = True


@pytest.fixture
def zulip(monkeypatch):
    FakeZulipAdmin.instances, FakeZulipAdmin.refuse = [], set()
    monkeypatch.setattr(ri, "ZulipAdmin", FakeZulipAdmin)
    monkeypatch.setattr(ri, "load_zuliprc", lambda path: ("https://z.example.org", "p@x", "k"))
    return FakeZulipAdmin


def test_live_zulip_checks(zulip):
    zulip.users_answer = [
        {"user_id": 1, "delivery_email": "Old@Example.org", "is_active": False},
        {"user_id": 2, "email": "legacy@example.org"},  # no `delivery_email` key: an old server
        {"user_id": 3, "is_bot": True, "delivery_email": None, "email": "bot@example.org"},
    ]
    live = ri.LiveChecks("prov.zuliprc", "MathAgents", {"workshop": "ws"})
    # deactivated users count: they may have used the bots before
    assert live.zulip_user(" old@example.org ") is True
    assert live.zulip_user("legacy@example.org") is True
    assert live.zulip_user("nobody@example.org") is False
    assert live.zulip_invited("pending@example.org") is True
    assert live.zulip_invited("gone@example.org") is False
    (admin,) = zulip.instances  # one listing, closed
    assert (admin.listed, admin.closed) == (1, True)
    with pytest.raises(LookupError, match="no --zuliprc"):
        ri.LiveChecks(None, "MathAgents", {"workshop": "ws"}).zulip_user("old@example.org")


def test_live_revocation_takes_the_ids_the_check_listed(zulip):
    """`DELETE /invites/{id}` for each open invitation to the address in the listing the checks
    read, with a client of its own, closed (#322); none for an address without one."""
    live = ri.LiveChecks("prov.zuliprc", "MathAgents", {"workshop": "ws"})
    assert live.zulip_invited("PENDING@example.org") is True
    assert live.revoke_invitations(" Pending@Example.org ") == 2
    listing, revoking = zulip.instances
    assert revoking.revoked == [5, 8] and revoking.closed and listing.listed == 1
    assert live.revoke_invitations("gone@example.org") == 0 and len(zulip.instances) == 2
    zulip.refuse = {8}
    with pytest.raises(zp.ZulipAdminError, match="Must be an organization administrator"):
        live.revoke_invitations("pending@example.org")
    assert zulip.instances[-1].closed


def test_the_client_revokes_by_id(monkeypatch):
    """`ZulipAdmin.revoke_invite`: `DELETE /api/v1/invites/{id}`, the id an integer."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return zp.httpx.Response(200, json={"result": "success", "msg": ""})

    admin = zp.ZulipAdmin(
        "https://z.example.org", "p@x", "k", transport=zp.httpx.MockTransport(handler)
    )
    admin.revoke_invite(42)
    with pytest.raises(ValueError):
        admin.revoke_invite("../users")
    admin.close()
    assert seen == [("DELETE", "/api/v1/invites/42")]


def test_a_hidden_address_makes_the_zulip_lookup_fail_closed(zulip):
    """A null `delivery_email` is an address the key cannot see, and `email` then synthetic
    (DECISIONS, 2026-10-04): that user might be anyone (reviewer on #317)."""
    zulip.users_answer = [
        {"user_id": 1, "delivery_email": "old@example.org"},
        {"user_id": 7, "delivery_email": None, "email": "user7@zulip.example.org"},
    ]
    live = ri.LiveChecks("prov.zuliprc", "MathAgents", {"workshop": "ws"})
    with pytest.raises(LookupError, match=r"1 user\(s\) hide their address from this key"):
        live.zulip_user("alice@example.org")
    with pytest.raises(LookupError):
        live.zulip_user("user7@zulip.example.org")  # the synthetic address proves nothing


def test_live_github_check(monkeypatch):
    monkeypatch.setattr(ri, "github_token", lambda env: env["GH_TOKEN"])
    env = {"GH_TOKEN": "fake-for-tests"}
    live = ri.LiveChecks(None, "MathAgents", {"workshop": "ws"}, env=env)
    monkeypatch.setattr(ri.StrictGitHub, "hold", lambda self, org, name, login: (org, name))
    assert live.github_hold("alice", "alice-gh") == ("MathAgents", "alice")
    assert live._github._token == env["GH_TOKEN"]


class FakeEc2:
    """`aws_common.Ec2.find_instances`, on a few instances (every region's, for simplicity)."""

    made: ClassVar[list[str]] = []
    instances: ClassVar[list[dict]] = []

    def __init__(self, region):
        self.region = region
        self.calls: list[tuple] = []
        FakeEc2.made.append(region)

    def find_instances(self, tags, states):
        self.calls.append((tags, states))
        return [
            i
            for i in FakeEc2.instances
            if i["Region"] == self.region
            and i["State"]["Name"] in states
            and all({t["Key"]: t["Value"] for t in i["Tags"]}.get(k) == v for k, v in tags.items())
        ]


def instance(slug, region, state="running", workshop="harvard-2026"):
    tags = [{"Key": "workshop", "Value": workshop}, {"Key": "participant", "Value": slug}]
    return {"Region": region, "State": {"Name": state}, "Tags": tags}


def test_live_vm_check():
    FakeEc2.made = []
    FakeEc2.instances = [
        instance("carol", "us-east-2", "stopped"),
        instance("erin", "us-east-2", "terminated"),
        instance("frank", "us-east-2", workshop="another"),
        instance("dave", "us-west-2", "pending"),
    ]
    live = ri.LiveChecks(None, "MathAgents", {"workshop": "harvard-2026"}, make_ec2=FakeEc2)
    assert live.vm("carol", "us-east-2") is True  # stopped still counts
    assert live.vm("erin", "us-east-2") is False  # terminated does not
    assert live.vm("frank", "us-east-2") is False  # another workshop's
    assert live.vm("dave", "us-east-2") is False and live.vm("dave", "us-west-2") is True
    assert FakeEc2.made == ["us-east-2", "us-west-2"]  # one client per region
    tags, states = live._ec2["us-east-2"].calls[0]
    assert tags == {"workshop": "harvard-2026", "participant": "carol"}
    assert "terminated" not in states and {"stopped", "shutting-down"} <= set(states)


def test_the_workshop_tag_and_region_come_from_the_command_line(paths, capsys, monkeypatch):
    seen = {}

    class Recording(ri.LiveChecks):
        def __init__(self, zuliprc, org, scope, **kw):
            seen["scope"] = scope
            super().__init__(zuliprc, org, scope, make_ec2=FakeEc2)

    monkeypatch.setattr(ri, "LiveChecks", Recording)
    FakeEc2.made, FakeEc2.instances = [], [instance("carol", "eu-west-1", workshop="w2")]
    args = ["--roster", str(paths[0]), "--input", str(paths[1]), "--tag", "workshop=w2"]
    paths[1].write_text(WITHOUT_CAROL)
    assert ri.main([*args, "--region", "eu-west-1"]) == 1
    assert seen["scope"] == {"workshop": "w2"} and FakeEc2.made == ["eu-west-1"]
    assert "carol: a VM tagged participant=carol is in eu-west-1" in capsys.readouterr().err
    assert ri.main([*args, "--tag", "participant=x"]) == 2
    assert ri.main([*args, "--region", "us west"]) == 2


def github_answers(routes):
    """A `urlopen` for `GitHub`: path -> (status, payload); anything else is a test error."""

    def urlopen(req, timeout):
        path = req.full_url.removeprefix("https://api.github.com")
        status, payload = routes[path]
        body = io.BytesIO(json.dumps(payload).encode())
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, body)
        body.status = status
        return body

    return urlopen


REPO = "/repos/MathAgents/bob"
PERM = REPO + "/collaborators/bob-old/permission"
INVITES = REPO + "/invitations?per_page=100"


def invitation(login, *, expired=False):
    return {"id": 7, "invitee": {"login": login}, "expired": expired}


@pytest.mark.parametrize(
    ("routes", "held"),
    [
        ({PERM: (200, {"permission": "write"})}, "access"),
        ({PERM: (200, {"permission": "read"})}, "access"),
        ({PERM: (200, {"permission": "none"}), INVITES: (200, [invitation("Bob-Old")])}, "invited"),
        ({PERM: (200, {"permission": "none"}),
          INVITES: (200, [invitation("bob-old", expired=True), invitation("bob-gh")])}, ""),
        # a 404 for a login GitHub does not know: it holds nothing
        ({PERM: (404, {}), "/users/bob-old": (404, {}), INVITES: (200, [])}, ""),
    ],
)  # fmt: skip
def test_strict_github_hold(routes, held):
    gh = ri.StrictGitHub("fake-for-tests", urlopen=github_answers({REPO: (200, {}), **routes}))
    assert gh.hold("MathAgents", "bob", "bob-old") == held


@pytest.mark.parametrize(
    ("routes", "error"),
    [
        ({REPO: (404, {})}, ri.NoRepository),  # does not exist, or the token cannot see it
        # a 404 for a login that exists: the token may lack the permission (codex round 1)
        ({PERM: (404, {}), "/users/bob-old": (200, {"login": "bob-old"})}, ri.GitHubError),
        ({PERM: (403, {"message": "Resource not accessible"})}, ri.GitHubError),
        ({PERM: (200, {"permission": "none"}), INVITES: (404, {})}, ri.GitHubError),
    ],
)  # fmt: skip
def test_strict_github_hold_fails_closed(routes, error):
    gh = ri.StrictGitHub("fake-for-tests", urlopen=github_answers({REPO: (200, {}), **routes}))
    with pytest.raises(error):
        gh.hold("MathAgents", "bob", "bob-old")
