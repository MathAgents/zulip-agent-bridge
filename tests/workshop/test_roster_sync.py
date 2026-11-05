"""`workshop/admin/roster_sync.py` (#627): the roster from the workshop's channel, against a fake
Zulip organization that shows addresses as each key sees them (`member_realm.MemberRealm`), and
`!roster remove`, `restore` and `rename`; then the rows it makes, downstream."""

from __future__ import annotations

import csv
import io
import tomllib

import aws_common
import push_config
import push_waker
import pytest
import roster_sync as rs
import zulip_provision as zp

from .member_realm import ADMINS, MODERATOR, MemberRealm

PROVISIONER, DREW, LI, SOREN, OLGA, BOT, GONE = 150, 1234567, 12345678, 300, 103, 900, 301
CHANNEL = 55
SITE = "https://org.zulipchat.com"
KEY = "K" * 32
#: The kept fleet's roster, on the same server: Drew is `andrew-sutherland` there.
KEPT = "slug,first_name,last_name,email,zulip_user_id\nandrew-sutherland,Andrew,S,a@example.org,7\n"


class Ec2:
    """`roster_import.Checks.vm`: the instances tagged `participant=<tag>`, by region."""

    def __init__(self) -> None:
        self.tagged: set[tuple[str, str]] = set()
        self.fail = False

    def vm(self, tag: str, region: str) -> bool:
        if self.fail:
            raise RuntimeError("EC2 said no")
        return (tag, region) in self.tagged


@pytest.fixture
def realm() -> MemberRealm:
    org = MemberRealm()
    org.human(PROVISIONER, "zab-provisioner", "prov@example.org", role=MODERATOR)
    org.human(DREW, "Andrew Sutherland", "drew@example.edu")  # moderators may read it
    org.human(LI, "李明", "li@example.cn", visibility=ADMINS)  # hidden from the provisioner
    org.human(SOREN, "Søren  Kierkegaard-Østergård" + chr(0x200B), "soren@example.dk")
    org.human(OLGA, "Olga Organizer", "olga@example.org")
    org.bot(BOT, "Some bot", "some-bot@org.zulipchat.com", owner=PROVISIONER)
    org.human(GONE, "Gone Person", "gone@example.org", is_active=False)
    members = {PROVISIONER, DREW, LI, SOREN, OLGA, BOT, GONE}
    org.channel(CHANNEL, "Harvard Halloween", members)
    return org


@pytest.fixture
def ws(tmp_path):
    roster, out, kept = tmp_path / "roster.csv", tmp_path / "out", tmp_path / "kept.csv"
    out.mkdir()
    kept.write_text(KEPT)
    (tmp_path / "rc").write_text(f"[api]\nemail=prov@example.org\nkey={KEY}\nsite={SITE}\n")
    return {"roster": roster, "out": out, "kept": kept, "rc": tmp_path / "rc", "ec2": Ec2()}


def run(realm: MemberRealm, ws: dict, *mode: str, recheck: bool = True) -> int:
    """The script as the bot runs it: a sync's apply comes after its dry run (the runner's
    recheck at the apply's start), which writes the changes the apply compares (`--changes`);
    `recheck=False` runs the apply alone."""
    changes = ["--changes", str(ws["out"].parent / "P-1" / "roster-sync.txt")]
    argv = [
        "--roster", str(ws["roster"]), "--zuliprc", str(ws["rc"]), "--out-dir", str(ws["out"]),
        "--region", "us-east-2", "--tag", "workshop=harvard", "--tag", "zab-scope=mathagents",
        "--workshop-roster", f"nt-agents-2026={ws['kept']}", *mode,
        *(changes if "--sync" in mode else []),
    ]  # fmt: skip
    if recheck and "--sync" in mode and "--dry-run" not in mode:
        run(realm, ws, *mode, "--dry-run")
    admin = zp.ZulipAdmin(SITE, "prov@example.org", KEY, transport=realm.transport())
    return rs.main(argv, admin=admin, checks=ws["ec2"])


def rows(ws: dict) -> dict[str, dict[str, str]]:
    with open(ws["roster"], newline="", encoding="utf-8") as f:
        return {r["slug"]: r for r in csv.DictReader(f)}


def write(ws: dict, text: str) -> None:
    ws["roster"].write_text(text)


# ----------------------------------------------------------------------------- the slugs


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Andrew Sutherland", "andrew-sutherland"),
        ("Zoë Ångström", "zoe-angstrom"),  # NFKD's marks dropped
        ("Søren Kierkegaard-Østergård", "soren-kierkegaard-ostergard"),  # FOLD's letters
        ("Jörg Strauß", "jorg-strauss"),
        ("Łukasz Żółć", "lukasz-zolc"),
        ("  O'Brien,  Mary-Kate  (PhD) ", "o-brien-mary-kate-phd"),
        ("\uff21\uff22\uff23", "abc"),  # NFKD folds full-width letters
        ("李明", ""),  # no letter folds to ASCII: `p-<id>` (below)
        ("Αλέξανδρος", ""),
        ("Maximiliana Wilhelmina Hohenzollern-Sigmaringen", "maximiliana-wilhelmina"),  # at a -
        ("Abcdefghijklmnopqrstuvwxyzabcdefghij", "abcdefghijklmnopqrstuvwxyzabcdef"),  # no -
    ],
)
def test_the_base_slug_folds_cuts_and_trims(name, slug):
    assert rs.base_slug(name) == slug
    assert len(slug) <= 32


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("李明", "p-42"),
        ("", "p-42"),
        ("All", "p-42"),  # a keyword of the admin commands
        ("Hold", "p-42"),
        ("Zab Spare 3", "p-42"),  # a spare's name
        ("Andrew Sutherland", "andrew-sutherland"),
    ],
)
def test_an_unusable_slug_is_the_user_id(name, slug):
    assert rs.slug_for(name, 42, lambda s: False) == slug


def test_a_taken_slug_takes_the_next_free_suffix_and_still_fits():
    taken = {"andrew-sutherland", "andrew-sutherland-2"}
    assert rs.slug_for("Andrew Sutherland", 1, taken.__contains__) == "andrew-sutherland-3"
    long = "Maximiliana Wilhelmina Hohenzollern Sigmaringen-Veringen"
    base = rs.base_slug(long)
    assert base == "maximiliana-wilhelmina"
    full = "abcdefghij-klmnopqrst-uvwxyzabcd"  # 32 characters: `-2` cuts it at a hyphen
    assert rs.slug_for(full, 1, {full}.__contains__) == "abcdefghij-klmnopqrst-2"
    assert rs.slug_for("x" * 40, 1, {"x" * 32}.__contains__) == "x" * 30 + "-2"
    assert rs.slug_for("李明", 7, {"p-7"}.__contains__) == "p-7-2"


# ------------------------------------------------------------------------------- the sync


def test_the_sync_adds_each_subscriber_but_the_provisioner_bots_and_the_deactivated(
    realm, ws, capsys
):
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 0
    out = capsys.readouterr().out
    assert not ws["roster"].exists()  # a dry run writes nothing
    assert out.startswith("roster sync from #Harvard Halloween (channel 55): 7 subscribers; ")
    assert '"zab-provisioner" (user 150), whose key reads it, gets no row' in out
    assert "roster sync: 4 added, 0 renamed, 0 removed, 0 left the channel, 0 excluded" in out
    # Drew: `andrew-sutherland` is the kept fleet's, on the same server
    # one line each, for a phone (review of #641, 4)
    assert (
        '+ andrew-sutherland-2  (Zulip user 1234567)  "Andrew Sutherland"  "drew@example.edu"'
        "  (andrew-sutherland is taken)\n" in out
    )
    # 李明 hides the address: Zulip's placeholder, and a name that folds to nothing
    assert '+ p-12345678  (Zulip user 12345678)  "李明"  "user12345678@org.zulipchat.com"\n' in out
    # the name's double space and zero-width space are dropped
    assert '+ soren-kierkegaard-ostergard  (Zulip user 300)  "Søren Kierkegaard-Østergård"' in out
    assert "+ olga-organizer  (Zulip user 103)" in out  # organizers get rows too
    for absent in ("150)", "zab-provisioner  (", "Some bot", "Gone Person"):
        assert absent not in out.split("\n", 1)[1]
    assert "next, for the new rows: !launch, then !provision zulip" in out
    assert out.count("olga-organizer") == 1  # never repeated in `next`
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    written = rows(ws)
    assert list(written) == [
        "olga-organizer", "soren-kierkegaard-ostergard", "andrew-sutherland-2", "p-12345678",
    ]  # by user ID  # fmt: skip
    drew = written["andrew-sutherland-2"]
    assert (drew["zulip_user_id"], drew["name"], drew["email"]) == (
        "1234567", "Andrew Sutherland", "drew@example.edu",
    )  # fmt: skip
    assert written["p-12345678"]["email"] == "user12345678@org.zulipchat.com"
    assert drew["first_name"] == drew["last_name"] == drew["host"] == ""
    aws_common.read_roster(str(ws["roster"]))  # the scripts' own reader takes it


def test_the_apply_writes_only_the_changes_its_last_dry_run_listed(realm, ws, capsys):
    """Codex round 1 on #641: someone who joins after the apply's recheck (the runner's last
    dry run, whose list the approval's hash covers) is never added unapproved: the apply
    compares its own list with the one that dry run wrote, and writes nothing on a difference.
    Without that file, it refuses too."""
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, DREW}
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 0
    saved = (ws["out"].parent / "P-1" / "roster-sync.txt").read_text()
    assert "+ andrew-sutherland-2  (Zulip user 1234567)" in saved
    assert (ws["out"].parent / "P-1").stat().st_mode & 0o777 == 0o700
    realm.streams[CHANNEL]["subscribers"].add(OLGA)  # joins between the recheck and the apply
    capsys.readouterr()
    assert run(realm, ws, "--sync", str(CHANNEL), recheck=False) == 1
    err = capsys.readouterr().err
    assert "refused: the channel's subscribers or the roster changed since the plan" in err
    assert not ws["roster"].exists()
    realm.streams[CHANNEL]["subscribers"].discard(OLGA)
    assert run(realm, ws, "--sync", str(CHANNEL), recheck=False) == 0  # as approved: written
    assert [r["zulip_user_id"] for r in rows(ws).values()] == [str(DREW)]
    (ws["out"].parent / "P-1" / "roster-sync.txt").unlink()
    realm.streams[CHANNEL]["subscribers"].add(OLGA)
    assert run(realm, ws, "--sync", str(CHANNEL), recheck=False) == 1
    assert "refused: the approved changes:" in capsys.readouterr().err


def test_a_sync_needs_its_changes_file_and_nothing_else_takes_one(realm, ws, capsys):
    admin = zp.ZulipAdmin(SITE, "prov@example.org", KEY, transport=realm.transport())
    base = ["--roster", str(ws["roster"]), "--zuliprc", str(ws["rc"]), "--out-dir", str(ws["out"])]
    assert rs.main([*base, "--sync", "55"], admin=admin, checks=ws["ec2"]) == 2
    assert rs.main([*base, "--remove", "x", "--changes", "f"], admin=admin, checks=ws["ec2"]) == 2
    assert "--sync and --changes go together" in capsys.readouterr().err


def test_an_address_the_scripts_refuse_takes_zulips_placeholder(realm, ws, capsys):
    """Review of #641 (1, blocking): Zulip takes `mary.o'brien@uni.example.edu`, the scripts' e-mail
    rule doesn't. One such subscriber must not refuse the whole sync: their row takes Zulip's
    placeholder, as for a hidden address, and is linked by its ID; the plan says so."""
    realm.human(401, "Mary O'Brien", "mary.o'brien@uni.example.edu")  # moderators may read it
    # everyone's, so Zulip's `email` is the address too
    realm.human(402, "Seán O'Neill", "sean.o'neill@uni.example.ie", visibility=1)
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, DREW, 401, 402}
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    out = capsys.readouterr().out
    placeholder = "their address is not one the scripts take: a placeholder address instead"
    mary = '+ mary-o-brien  (Zulip user 401)  "Mary O\'Brien"  "user401@org.zulipchat.com"'
    assert f"{mary}  ({placeholder})\n" in out
    assert '+ sean-o-neill  (Zulip user 402)  "Seán O\'Neill"  "user402@org.zulipchat.com"' in out
    written = rows(ws)
    assert written["mary-o-brien"]["email"] == "user401@org.zulipchat.com"
    assert written["andrew-sutherland-2"]["email"] == "drew@example.edu"
    aws_common.read_roster(str(ws["roster"]))  # every script's reader takes it


@pytest.mark.parametrize("what", ["bot", "sandbox", "out"])
def test_a_new_rows_slug_skips_one_something_is_named_after(realm, ws, capsys, what):
    """Review of #641 (3): a slug free in every roster, but with a bot, a sandbox or an
    `out/<slug>/` of its name, would hand its new row what is there: it takes `-2`."""
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, OLGA}
    if what == "bot":
        realm.bot(901, "Claude · olga-organizer", "c-olga@org.zulipchat.com", owner=PROVISIONER)
    elif what == "sandbox":
        realm.channel(78, "olga-organizer-sandbox", set(), invite_only=False)
    else:
        (ws["out"] / "olga-organizer").mkdir()
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 0
    assert "+ olga-organizer-2  (Zulip user 103)" in capsys.readouterr().out


def test_a_recorded_github_invitation_alone_holds_the_row(realm, ws, capsys):
    """Review of #641 (3): `github_invite` alone, as `provision_repos.py` records it."""
    write(ws, "slug,email,zulip_user_id,github_invite\nbob,bob@example.org,999,bob-gh\n")
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER}
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == rs.NOTHING
    out = capsys.readouterr().out
    assert "= bob: left the channel (not in it, or deactivated), but provision_repos.py ran" in out


def test_a_roster_changed_during_the_run_is_never_overwritten(realm, ws, capsys, monkeypatch):
    """Review of #641 (3): the roster is written only if it is still the file this run read."""
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    real = rs.edit_row

    def meanwhile(rows, *args):
        said = real(rows, *args)
        with open(ws["roster"], "a", encoding="utf-8") as f:
            f.write("\n")  # another writer, between this run's read and its write
        return said

    monkeypatch.setattr(rs, "edit_row", meanwhile)
    before = ws["roster"].read_text()
    capsys.readouterr()
    assert run(realm, ws, "--remove", "olga-organizer") == 1
    assert "refused: the roster changed during this run; nothing written" in capsys.readouterr().err
    assert ws["roster"].read_text() == before + "\n"
    assert not rows(ws)["olga-organizer"].get("excluded")


def test_a_new_row_duplicating_another_rows_address_refuses_the_sync(realm, ws, capsys):
    """Review of #641 (3): `check_duplicates` over the new roster. An old row, linked to
    another user, keeps Drew's address: one address on two rows is refused, nothing written."""
    write(ws, "slug,email,zulip_user_id,host\nold,drew@example.edu,999,ws-old\n")
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, DREW}
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 1
    assert "refused: duplicate email" in capsys.readouterr().err


@pytest.mark.parametrize("who", ["bot", "deactivated"])
def test_an_id_of_a_bot_or_a_deactivated_user_is_never_linked(realm, who):
    """Review of #641 (3): `match_rows`' ID link takes an active human alone; a row recording a
    bot's or a deactivated user's ID, whose e-mail binds nobody, stays refused."""
    if who == "bot":
        uid = BOT
    else:
        uid = GONE
    admin = zp.ZulipAdmin(SITE, "prov@example.org", KEY, transport=realm.transport())
    row = {"slug": "x", "email": "nobody@example.org", "zulip_user_id": str(uid)}
    (report,) = zp.match_rows([row], admin.users(), None, no_rebind=True)
    assert report.blocked and report.matched == "kept" and report.user_id == uid
    assert report.notes[0].startswith("binding changed: refused")


def test_the_provisioner_never_gets_a_row(realm, ws, capsys):
    """The review on #627: `zab-provisioner` is a Moderator user in the channel, not a
    bot: the key that reads the channel is left out by its own `users/me`."""
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, DREW}
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    assert [r["zulip_user_id"] for r in rows(ws).values()] == [str(DREW)]
    assert "zab-provisioner" not in "".join(ws["roster"].read_text())


def test_a_second_sync_with_nothing_to_change_says_so(realm, ws, capsys):
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    before = ws["roster"].read_text()
    capsys.readouterr()
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == rs.NOTHING
    out = capsys.readouterr().out
    assert "roster sync: 0 added, 0 renamed, 0 removed, 0 left the channel, 0 excluded, 4 un" in out
    assert "nothing to change" in out
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0  # an apply that finds none: exit 0
    assert ws["roster"].read_text() == before


def test_a_row_is_matched_by_its_id_and_only_its_name_refreshed(realm, ws, capsys):
    """Drew renames himself and his address becomes visible differently: the row stays his, by
    its ID; only `name` follows Zulip."""
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    realm.users[DREW]["full_name"] = "Drew Sutherland"
    realm.users[DREW]["email"] = "drew@new.example.org"
    capsys.readouterr()
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    out = capsys.readouterr().out
    assert '~ andrew-sutherland-2: name "Andrew Sutherland" -> "Drew Sutherland"' in out
    assert "0 added, 1 renamed, 0 removed" in out
    drew = rows(ws)["andrew-sutherland-2"]
    assert (drew["name"], drew["email"]) == ("Drew Sutherland", "drew@example.edu")


def test_a_legacy_row_without_an_id_is_matched_by_its_email(realm, ws, capsys):
    """A CSV's row: its e-mail is Drew's address. Its name is set; its ID stays unset (the
    provisioning binds it), and no row is added for Drew."""
    write(ws, "slug,first_name,last_name,email\ndrew,Andrew,Sutherland,DREW@example.edu\n")
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, DREW}
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    out = capsys.readouterr().out
    assert '~ drew: name "" -> "Andrew Sutherland"' in out and "0 added" in out
    drew = rows(ws)["drew"]
    assert (drew["zulip_user_id"], drew["name"]) == ("", "Andrew Sutherland")


ROWS = "slug,email,zulip_user_id,host,ip,zulip_channel_id,repo_status,excluded\n"


@pytest.mark.parametrize(
    ("cells", "setup", "held"),
    [
        (",,,,", None, None),  # nothing: removed
        ("ws-bob,198.51.100.2,,,", None, "it has a VM (ws-bob)"),
        (",,,,", "ec2", "a VM tagged participant=bob is in us-east-2"),
        (",,,,", "key", "its claude bot's key is in out/bob/"),
        (",,,,", "bot", "a bot named Codex · bob exists"),
        (",,,,", "sandbox", "#bob-sandbox exists"),
        (",,77,,", None, "its sandbox is recorded (zulip_channel_id 77)"),
        (",,,ok,", None, "provision_repos.py ran for it: its repository may hold work"),
        (",,,,", "ec2 fails", "what it holds can't be checked (EC2 said no)"),
    ],
)
def test_someone_who_left_is_removed_only_if_nothing_is_provisioned(
    realm, ws, capsys, cells, setup, held
):
    write(ws, ROWS + f"bob,bob@example.org,999,{cells}\n")
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER}
    if setup == "ec2":
        ws["ec2"].tagged.add(("bob", "us-east-2"))
    elif setup == "ec2 fails":
        ws["ec2"].fail = True
    elif setup == "key":
        (ws["out"] / "bob").mkdir()
        (ws["out"] / "bob" / "claude.zuliprc").write_text("[api]\n")
    elif setup == "bot":
        realm.bot(901, "Codex · bob", "codex-bob-bot@org.zulipchat.com", owner=PROVISIONER)
    elif setup == "sandbox":
        realm.channel(77, "bob-sandbox", {901}, invite_only=False)
    dry = run(realm, ws, "--sync", str(CHANNEL), "--dry-run")
    planned = capsys.readouterr()
    code = run(realm, ws, "--sync", str(CHANNEL))
    out = capsys.readouterr().out
    if held is None:
        assert dry == code == 0 and "- bob: not in the channel, and nothing is provisioned" in out
        assert rows(ws) == {}
        return
    assert f"= bob: left the channel (not in it, or deactivated), but {held}: left" in out
    assert list(rows(ws)) == ["bob"]
    if setup == "ec2 fails":  # #708: a check that can't tell never ends in "nothing to change"
        assert dry == code == 1 and "nothing to change" not in planned.out
        assert f"refused: {rs.UNCHECKED.format('bob')}" in planned.err
    else:
        assert (dry, code) == (rs.NOTHING, 0) and "nothing to change" in planned.out


def test_an_unchecked_row_beside_other_changes_still_plans(realm, ws, capsys):
    """Review of #726 (1): only a sync with nothing else to change is refused for a row whose
    holdings can't be checked; one with an addition is planned, the row left alone."""
    write(ws, ROWS + "bob,bob@example.org,999,,,,,\n")
    realm.streams[CHANNEL]["subscribers"] = {PROVISIONER, SOREN}
    ws["ec2"].fail = True
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "+ soren-kierkegaard-ostergard  (Zulip user 300)" in out
    assert "= bob: left the channel (not in it, or deactivated), but what it holds can't" in out


def test_a_deactivated_member_counts_as_gone(realm, ws, capsys):
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    realm.users[SOREN]["is_active"] = False
    capsys.readouterr()
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    assert "- soren-kierkegaard-ostergard: not in the channel" in capsys.readouterr().out
    assert "soren-kierkegaard-ostergard" not in rows(ws)


def test_an_excluded_row_survives_later_syncs_and_comes_back_when_restored(realm, ws, capsys):
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    assert run(realm, ws, "--remove", "olga-organizer", "--dry-run") == 0
    assert rows(ws)["olga-organizer"].get("excluded", "") == ""  # a dry run writes nothing
    assert run(realm, ws, "--remove", "olga-organizer") == 0
    out = capsys.readouterr().out
    assert 'roster remove: olga-organizer ("Olga Organizer"): excluded' in out
    assert rows(ws)["olga-organizer"]["excluded"] == "yes"
    realm.users[OLGA]["full_name"] = "Olga O."
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == rs.NOTHING
    out = capsys.readouterr().out
    assert "x olga-organizer: excluded (!roster remove): not added again" in out
    assert "1 excluded, 3 unchanged" in out and "+ " not in out
    realm.streams[CHANNEL]["subscribers"].discard(OLGA)  # gone, but excluded: kept, listed
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == rs.NOTHING
    assert "x olga-organizer" in capsys.readouterr().out
    realm.streams[CHANNEL]["subscribers"].add(OLGA)
    assert run(realm, ws, "--restore", "olga-organizer") == 0
    assert rows(ws)["olga-organizer"]["excluded"] == ""
    assert run(realm, ws, "--restore", "olga-organizer") == 1  # not excluded any more
    assert "not excluded: nothing to restore" in capsys.readouterr().err
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    assert '~ olga-organizer: name "Olga Organizer" -> "Olga O."' in capsys.readouterr().out


def test_remove_and_rename_are_refused_once_anything_is_provisioned(realm, ws, capsys):
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    data = list(csv.DictReader(io.StringIO(ws["roster"].read_text())))
    for row in data:
        if row["slug"] == "olga-organizer":
            row["ip"], row["host"] = "198.51.100.9", "ws-olga-organizer"
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(data[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(data)
    write(ws, buffer.getvalue())
    capsys.readouterr()
    for mode in (["--remove", "olga-organizer"], ["--rename", "olga-organizer", "olga"]):
        assert run(realm, ws, *mode, "--dry-run") == 1, mode
        err = capsys.readouterr().err
        assert "it is refused: it has a VM (ws-olga-organizer); its end is !retire's" in err
    assert rows(ws)["olga-organizer"]["slug"] == "olga-organizer"


@pytest.mark.parametrize(
    ("new", "why"),
    [
        ("andrew-sutherland", "andrew-sutherland is in nt-agents-2026; a slug is never shared"),
        ("all", "not a slug"),
        ("zab-spare-2", "not a slug"),
        ("Olga", "not a slug"),
        ("p-12345678", "is a row of this roster already"),
        ("bob", "something is named after it already"),
    ],
)
def test_rename_checks_the_new_slug(realm, ws, capsys, new, why):
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    realm.channel(77, "bob-sandbox", set(), invite_only=False)
    capsys.readouterr()
    assert run(realm, ws, "--rename", "olga-organizer", new) == 1
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and why in err, err
    assert "olga-organizer" in rows(ws)


def test_rename_moves_the_slug_and_keeps_the_rest(realm, ws, capsys):
    assert run(realm, ws, "--sync", str(CHANNEL)) == 0
    assert run(realm, ws, "--rename", "olga-organizer", "olga") == 0
    assert 'roster rename: olga-organizer ("Olga Organizer") -> olga' in capsys.readouterr().out
    olga = rows(ws)["olga"]
    assert (olga["zulip_user_id"], olga["name"]) == (str(OLGA), "Olga Organizer")
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == rs.NOTHING  # still hers


def test_a_channel_the_provisioner_cant_read_refuses(realm, ws, capsys):
    realm.streams[CHANNEL]["subscribers"].discard(PROVISIONER)
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 1
    err = capsys.readouterr().err
    assert "refused: cannot read channel 55's subscribers" in err
    assert "subscribe zab-provisioner to it" in err and not ws["roster"].exists()


def test_a_public_channel_is_warned_about_at_every_sync(realm, ws, capsys):
    """Review of #638 (4): a channel made public after `!new-workshop` adopted it."""
    realm.streams[CHANNEL]["invite_only"] = False
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "WARNING: #Harvard Halloween is public: anyone in the organization can join it" in out


def test_two_members_matching_one_legacy_row_refuse(realm, ws, capsys):
    write(ws, "slug,email\ndrew,drew@example.edu\n")
    realm.users[OLGA]["email"] = "drew@example.edu"  # never in one Zulip org: refused
    assert run(realm, ws, "--sync", str(CHANNEL), "--dry-run") == 1
    assert "drew: two subscribers match it" in capsys.readouterr().err


# ---------------------------------------------------------------- downstream of such a row

PLACEHOLDER_ROW = {
    "slug": "p-12345678", "name": "李明", "email": "user12345678@org.zulipchat.com",
    "zulip_user_id": "12345678", "first_name": "", "last_name": "",
}  # fmt: skip


def test_git_takes_the_rows_name_then_first_and_last_then_the_slug():
    assert aws_common.git_identity(PLACEHOLDER_ROW) == ("李明", "user12345678@org.zulipchat.com")
    legacy = {"slug": "ada", "first_name": "Ada", "last_name": "L", "email": "a@example.org"}
    assert aws_common.git_identity(legacy) == ("Ada L", "a@example.org")
    assert aws_common.git_identity({**legacy, "name": "Ada Lovelace"})[0] == "Ada Lovelace"
    assert aws_common.git_identity({"slug": "ada", "email": "a@example.org"})[0] == "ada"


def test_the_allow_lists_take_the_id_of_a_row_with_a_placeholder_email():
    data = tomllib.loads(push_config.render_config_toml(PLACEHOLDER_ROW))
    assert all(b["allow_user_ids"] == [LI] for b in data["bots"])
    assert push_waker.allowlist(PLACEHOLDER_ROW, "p-12345678") == (
        [LI], ["user12345678@org.zulipchat.com"],
    )  # fmt: skip


def test_provisioning_binds_such_a_row_by_its_id(realm):
    """`!provision zulip` (`--no-rebind`): the row's placeholder is no longer what the key
    sees once Li widens the address's visibility; the row stays Li's, by its ID."""
    realm.users[LI]["visibility"] = 5  # moderators: the provisioner reads the real address now
    admin = zp.ZulipAdmin(SITE, "prov@example.org", KEY, transport=realm.transport())
    users = admin.users()
    row = dict(PLACEHOLDER_ROW)
    (report,) = zp.match_rows([row], users, None, no_rebind=True)
    assert (report.user_id, report.matched, report.blocked) == (LI, zp.ZULIP_ID, False)
    assert row["zulip_user_id"] == "12345678"
    other = {"slug": "x", "email": "li@example.cn", "zulip_user_id": ""}  # Li's real address
    _, second = zp.match_rows([dict(PLACEHOLDER_ROW), other], users, None, no_rebind=True)
    assert second.blocked and "is still recorded for p-12345678" in second.notes[0]


def test_the_scripts_nothing_and_reserved_slugs_are_the_bots():
    """Exit 3 refuses the plan (`actions.NOTHING`, `Verb.nothing`), and the slugs it makes are
    ones the bot's grammar takes."""
    from zab.admin.actions import NOTHING
    from zab.admin.grammar import RESERVED

    assert rs.NOTHING == NOTHING
    assert not any(rs.usable(word) for word in RESERVED)
