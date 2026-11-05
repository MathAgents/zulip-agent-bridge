"""The roster from the workshop's channel on the bot's side (#627): the grammar of `!roster
sync`, `remove`, `restore` and `rename`, their argument lists and hashes, excluded rows in the
fleet verbs and in `!roster show`, and a sync with nothing to change, answered with no plan
(#708). The script itself is `tests/workshop/test_roster_sync.py`'s."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from zab.admin import actions
from zab.admin.actions import DRY_RUN, INPUTS, VERBS, ActionError, Live, Row
from zab.admin.approvals import CHANGE, requirement
from zab.admin.grammar import Command, NotInRoster, Rejected, parse
from zab.admin.plans import PENDING, REFUSED, STALE, UNCHANGED

from .botkit import HARVARD, OLGA, incoming
from .test_admin_actions import CONFIG, FLEET, OUT, PROV, PY, TAG, R, context, make
from .test_admin_runner import (
    approve,
    bot_of,
    calls,
    fake,
    posts,
    records,
    request,
    settle,
    site,
    start,
)

assert site  # a fixture, used by name

ROSTER_SYNC = f"{PY}/admin/roster_sync.py {R} {PROV} --out-dir {OUT} --region us-west-2 {TAG}"
WITH_CHANNEL = dataclasses.replace(
    FLEET, workshop=dataclasses.replace(FLEET.workshop, channel=46)
)  # fmt: skip
#: carol excluded (`!roster remove`): nothing provisioned, no VM
EXCLUDED = dataclasses.replace(
    WITH_CHANNEL,
    rows=tuple(dataclasses.replace(r, excluded=r.slug == "carol") for r in FLEET.rows),
)


# ------------------------------------------------------------------------------ the grammar


@pytest.mark.parametrize(
    ("message", "want"),
    [
        ("!roster sync", Command("roster sync")),
        ("!roster remove carol", Command("roster remove", slug="carol")),
        ("!roster restore carol", Command("roster restore", slug="carol")),
        (
            "!roster rename carol carol-c",
            Command("roster rename", slug="carol", new_slug="carol-c"),
        ),
    ],
)
def test_the_roster_verbs_parse(message, want):
    assert parse(message, context(FLEET)) == want
    assert want.text() == message


@pytest.mark.parametrize(
    ("message", "kind", "why"),
    [
        ("!roster remove nobody", NotInRoster, "a slug of this workshop's roster"),
        ("!roster rename nobody x", NotInRoster, "a slug of this workshop's roster"),
        ("!roster rename carol all", Rejected, "a new slug"),
        ("!roster rename carol zab-spare-1", Rejected, "a new slug"),
        ("!roster rename carol Carol", Rejected, "a new slug"),
        ("!roster rename carol -x", Rejected, "a new slug"),
        ("!roster rename carol", Rejected, "missing a new slug"),
        ("!roster sync all", Rejected, "not an option of !roster"),
        ("!roster remove all", Rejected, "a slug of this workshop's roster"),
    ],
)
def test_a_malformed_roster_verb_is_rejected(message, kind, why):
    result = parse(message, context(FLEET))
    assert type(result) is kind and why in result.reason, result


# ------------------------------------------------------------------------------- the actions


def test_a_sync_reads_the_workshops_channel_and_is_hashed_on_its_dry_run(layout):
    action = make("!roster sync", layout, WITH_CHANNEL, plan="P-7")
    (step,) = action.steps
    changes = f"--changes {layout.runs}/P-7/roster-sync.txt"  # Codex round 1 on #641
    assert step.argv == tuple(f"{ROSTER_SYNC} --sync 46 {changes}".split())
    assert step.dry_run == (*step.argv, "--dry-run") and step.env == ()
    assert action.hashed == DRY_RUN and VERBS["roster sync"].nothing
    assert action.targets == () and action.status is None
    # the retired slugs and the other workshops' rosters are inputs, missing ones too
    assert action.inputs == (FLEET.workshop.roster, layout.retired, CONFIG.workshops[1].roster)
    assert requirement(action, WITH_CHANNEL, CONFIG).klass == CHANGE
    one = actions.plan_hash(action, release="r", digests=actions.digests(action),
                            live=Live(dry_run="+ drew\n", tmpdir="/t/P-1"))  # fmt: skip
    two = actions.plan_hash(action, release="r", digests=actions.digests(action),
                            live=Live(dry_run="+ dora\n", tmpdir="/t/P-1"))  # fmt: skip
    assert one != two  # an addition between plan and approval: STALE


def test_a_syncs_hash_covers_every_character_of_its_listing(layout):
    """Review of #641 (2): `normalize_dry_run` would read `bob-5s` and `bob-6s` as durations,
    and hash both alike; the sync's normalizer leaves its text as it is."""
    action = make("!roster sync", layout, WITH_CHANNEL, plan="P-7")
    assert VERBS["roster sync"].normalize is actions.normalize_exact

    def hashed(text):
        live = Live(dry_run=text, tmpdir="/var/lib/zab-admin/tmp/P-7")
        return actions.plan_hash(action, release="r", digests=actions.digests(action), live=live)

    five = "- bob-5s: not in the channel, and nothing is provisioned for it: removed\n"
    assert hashed(five) != hashed(five.replace("5s", "6s"))
    assert hashed('+ ann  (Zulip user 7)  "2026-10-31 14:02"') != hashed(
        '+ ann  (Zulip user 7)  "2026-10-31 14:03"'
    )


def test_a_sync_without_a_channel_is_refused(layout):
    with pytest.raises(ActionError, match=r"no participants' channel .* `!roster import`"):
        make("!roster sync", layout, plan="P-7")
    with pytest.raises(ActionError, match="needs its plan's name"):
        make("!roster sync", layout, WITH_CHANNEL)


def test_the_other_rosters_and_the_retired_slugs_go_to_the_script(layout, tmp_path):
    other = tmp_path / "zab-test.csv"
    other.write_text("slug\nerin\n")
    Path(layout.retired).write_text("old\n")
    test = dataclasses.replace(CONFIG.workshops[1], roster=str(other))
    fleet = dataclasses.replace(WITH_CHANNEL, others=(test,))
    argv = make("!roster sync", layout, fleet, plan="P-7").steps[0].argv
    assert argv[argv.index("--retired") + 1] == layout.retired
    assert argv[argv.index("--workshop-roster") + 1] == f"zab-test={other}"


@pytest.mark.parametrize(
    ("message", "mode"),
    [
        ("!roster remove carol", "--remove carol"),
        ("!roster rename carol carol-c", "--rename carol carol-c"),
    ],
)
def test_one_rows_verbs_are_hashed_on_their_inputs(layout, message, mode):
    action = make(message, layout)
    assert action.steps[0].argv == tuple(f"{ROSTER_SYNC} {mode}".split())
    assert action.hashed == INPUTS and action.targets == ("carol",)
    assert requirement(action, FLEET, CONFIG).klass == CHANGE


def test_restore_takes_an_excluded_row_and_remove_one_that_isnt(layout):
    action = make("!roster restore carol", layout, EXCLUDED)
    assert action.steps[0].argv == tuple(f"{ROSTER_SYNC} --restore carol".split())
    with pytest.raises(ActionError, match="carol is not excluded: nothing to restore"):
        make("!roster restore carol", layout)
    with pytest.raises(ActionError, match="carol is excluded already: !roster restore carol"):
        make("!roster remove carol", layout, EXCLUDED)
    assert make("!roster rename carol carol-c", layout, EXCLUDED).targets == ("carol",)


# ------------------------------------------------------------------ excluded rows elsewhere


@pytest.mark.parametrize(
    "message",
    ["!status all", "!provision zulip all", "!provision repos all", "!deactivate-bots all",
     "!teardown all unsynced", "!launch all"],
)  # fmt: skip
def test_all_never_names_an_excluded_row(layout, message):
    assert "carol" not in make(message, layout, EXCLUDED).targets
    assert "carol" in make(message, layout, WITH_CHANNEL).targets


@pytest.mark.parametrize(
    "message",
    ["!launch carol", "!provision zulip carol", "!adopt-spare carol zab-spare-1",
     "!retire carol unsynced", "!deactivate-bots carol", "!teardown carol unsynced"],
)  # fmt: skip
def test_a_change_verb_naming_an_excluded_row_is_refused(layout, message):
    with pytest.raises(ActionError, match=r"carol excluded from the roster .*restore carol"):
        make(message, layout, EXCLUDED)


def test_the_read_still_names_an_excluded_row(layout):
    assert make("!status carol", layout, EXCLUDED).targets == ("carol",)


def test_the_fleet_reads_the_excluded_mark(tmp_path):
    from zab.admin import fleet

    ws = dataclasses.replace(CONFIG.workshops[0], roster=str(tmp_path / "roster.csv"))
    (tmp_path / "roster.csv").write_text("slug,excluded\nalice,\ncarol,yes\n")
    config = dataclasses.replace(CONFIG, workshops=(ws, CONFIG.workshops[1]))
    read = fleet.read_fleet(config, ws)
    assert read.excluded == {"carol"} and read.rows[1] == Row("carol", excluded=True)
    assert read.in_all("alice") and not read.in_all("carol")


@pytest.fixture
def layout(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "zulip_agent_bridge-0.2.2-py3-none-any.whl").write_bytes(b"wheel")
    return actions.Layout(wheels=str(wheels), retired=str(tmp_path / "retired-slugs.txt"))


# ---------------------------------------------------------------------- through the bot


def with_channel(site):
    """botkit's Harvard entry, with its participants' channel."""
    text = site.config_path.read_text()
    line = f'out_dir = "{site.tmp}/ws/harvard-2026/out"\n'
    site.config_path.write_text(text.replace(line, line + "channel = 46\n", 1))


SYNC = """\
out = "roster sync: 1 added, 0 renamed, 0 removed, 0 left the channel, 0 excluded, 3 unchanged"
if os.environ.get("NOTHING"):
    print(out.replace("1 added", "0 added")); print("nothing to change")
    sys.exit(3 if DRY else 0)
print(out); print("+ dora  (Zulip user 77)")
print("dry run: nothing written" if DRY else "written")
"""


async def test_a_sync_with_nothing_to_change_is_a_success_with_no_plan(site):
    """#708: `☑`, never `❌`: no plan message, no approval message, an `unchanged` record (no
    `refused`, no `plan`), and the dry run's own words under the answer."""
    with_channel(site)
    fake(site, "admin/roster_sync.py", SYNC.replace('os.environ.get("NOTHING")', "True"))
    srv = await start(site)
    try:
        before = len(posts(site))
        plan = await request(srv, "!roster sync")
        assert plan.state == UNCHANGED and plan.note == "nothing to change, as its dry run says"
        assert not plan.hash and plan.plan_message is None and plan.approval_message is None
        said = posts(site)[before:]
        assert len(said) == 1, said  # the answer alone
        title = f"☑ **{plan.name}** · `!roster sync`: nothing to change, as its dry run says"
        assert said[0].startswith(f"{title}; no plan to approve\n"), said[0]
        assert "0 added" in said[0] and "\nnothing to change\n" in said[0] and "❌" not in said[0]
        assert re.search(r"\n`audit #\d+ · chain [0-9a-f]{12}`$", said[0]), said[0][-80:]
        mine = [r for r in records(site) if r.get("plan") == plan.name]
        assert [r["event"] for r in mine] == ["request", "unchanged"]
        assert (mine[1]["verb"], mine[1]["reason"]) == ("roster sync", plan.note)
        assert [c["dry"] for c in calls(site, "admin/roster_sync.py")] == [True]
    finally:
        await srv.close()


#: roster_sync.py's dry run when a departed row's holdings can't be checked and nothing else
#: changes (review of #726, 1): refused, exit 1, never "nothing to change".
UNCHECKED = """\
print("roster sync: 0 added, 0 renamed, 0 removed, 1 left the channel, 0 excluded, 3 unchanged")
print("= bob: left the channel (not in it, or deactivated), but what it holds can't be checked "
      "(EC2 said no): left alone; its end is !retire's")
print("refused: what bob holds can't be checked, so whether this sync has anything to change "
      "can't be told: request !roster sync again", file=sys.stderr)
print("nothing written", file=sys.stderr)
sys.exit(1)
"""


async def test_a_sync_whose_check_cant_tell_is_refused_never_unchanged(site):
    with_channel(site)
    fake(site, "admin/roster_sync.py", UNCHECKED)
    srv = await start(site)
    try:
        plan = await request(srv, "!roster sync")
        assert plan.state == REFUSED
        assert plan.note == "the dry run failed: admin/roster_sync.py exited 1"
        said = posts(site)[-1]
        assert said.startswith(f"❌ {plan.name} refused: the dry run failed") and "☑" not in said
        assert "what bob holds can't be checked" in said
        assert [r["event"] for r in records(site) if r.get("plan") == plan.name] == [
            "request",
            "refused",
        ]
    finally:
        await srv.close()


async def test_a_sync_is_planned_approved_and_applied(site):
    with_channel(site)
    fake(site, "admin/roster_sync.py", SYNC)
    srv = await start(site)
    try:
        plan = await request(srv, "!roster sync")
        assert plan.state == PENDING, plan.note
        assert "+ dora  (Zulip user 77)" in posts(site)[-2]
        await approve(srv, plan)
        await settle(srv)
        ran = calls(site, "admin/roster_sync.py")
        assert [c["dry"] for c in ran] == [True, True, False]  # the plan, the recheck, the apply
        changes = f"{site.paths.layout.runs}/{plan.name}/roster-sync.txt"  # the plan's own
        assert ran[-1]["argv"][-4:] == ["--sync", "46", "--changes", changes]
        assert all(c["argv"] == ran[-1]["argv"] + ["--dry-run"] for c in ran[:2])
        assert "written" in posts(site)[-1]
    finally:
        await srv.close()


async def test_a_sync_whose_dry_run_changed_since_its_plan_is_stale(site):
    with_channel(site)
    fake(site, "admin/roster_sync.py", SYNC)
    srv = await start(site)
    try:
        plan = await request(srv, "!roster sync")
        fake(site, "admin/roster_sync.py", SYNC.replace("dora", "dora-2"))  # a late joiner
        await approve(srv, plan)
        await settle(srv)
        assert srv.store.get(plan.id).state == STALE
        assert "the dry run" in posts(site)[-1]
    finally:
        await srv.close()


async def test_roster_show_names_and_marks_excluded_rows(site):
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    roster.write_text(
        "slug,first_name,last_name,name,email,excluded\n"
        "alice,Alice,A,,alice@example.com,\n"
        "dora,,,Dora Zulip,dora@example.com,yes\n"
    )
    srv = await start(site)
    try:
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!roster show", mid=5100))
        await settle(srv)
        text = posts(site)[-1]
        assert "Alice A" in text and "Dora Zulip" in text
        dora = next(line for line in text.splitlines() if line.startswith("dora"))
        assert dora.split()[-1] == "excluded"
    finally:
        await srv.close()
