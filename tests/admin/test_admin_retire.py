"""`!retire` (#559): one plan that ends named rows, each in turn, through the separate verbs'
scripts; a row stops at its first failure, the others go on; the ledger records each row's
uninstall and sync as the separate verbs would, and the rows retired, which `all` leaves out.

The grammar and the action against `test_admin_actions`' fleet; the apply through the bots of
`botkit` with the real `Runner` and fake scripts, as `test_admin_runner`'s; the ledger from
records alone."""

import dataclasses
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from zab.admin import approvals, planning
from zab.admin import runner as runner_mod
from zab.admin.actions import (
    BACK,
    EXPORT_ROW_S,
    EXPORT_ROWS,
    REACH_VM,
    RETIRE,
    RETIRE_FLOOR_S,
    RETIRE_ROW_S,
    VERBS,
    ActionError,
    Layout,
    Live,
    Protection,
    Row,
    build,
    plan_hash,
)
from zab.admin.approvals import DESTRUCTIVE, requirement
from zab.admin.fleet import ApplyEnd, Ledger, RowEnd, retire_rows
from zab.admin.grammar import SPECS, Command, Rejected, parse
from zab.admin.planning import NOTE_CHARS, Planning
from zab.admin.plans import DONE, FAILED, PENDING, REFUSED, STALE, Requirement
from zab.admin.runner import Runner, limit

from .botkit import DREW, OLGA, OSCAR, ZULIP_ITALIC, make_site
from .test_admin_actions import CONFIG, FLEET, OUT, PY, TAG, R, command, context
from .test_admin_runner import approve, calls, fake, posts, records, request, settle, status

LATE = 10**6

# ------------------------------------------------------------------------- the grammar


@pytest.mark.parametrize(
    "message, why",
    [
        ("!retire all", "name the rows that end, never `all`"),
        ("!retire all unsynced", "name the rows that end, never `all`"),
        ("!retire", "no selection: name the rows that end"),
        ("!retire spares", "not an option of !retire"),
        ("!retire zab-spare-1", "no spares here"),
        ("!retire bob bob", "given twice"),
        ("!retire bob export export", "given twice"),
    ],
)
def test_the_grammar_takes_named_rows_only(message, why):
    parsed = parse(message, context(FLEET))
    assert isinstance(parsed, Rejected) and why in parsed.reason, parsed


def test_the_grammar_reads_the_keywords():
    parsed = parse("!retire dave bob unsynced export", context(FLEET))
    assert isinstance(parsed, Command) and parsed.verb == "retire"
    assert parsed.tokens() == ("bob", "dave", "export", "unsynced")


# -------------------------------------------------------------------------- the action


def make(message, fleet=FLEET, layout=None):
    return build(command(message, fleet), fleet, layout=layout or Layout())


def cells(action):
    return [(s.row, s.stage, s.tolerated, s.vm, s.gone) for s in action.steps if s.row]


def test_each_row_runs_the_separate_verbs_steps_alone():
    action = make("!retire bob dave")
    read, prelude, *rows = action.steps
    teardown = f"{PY}/aws/teardown.py {R} {TAG} --region us-west-2"  # --roster: #604
    teardown += f" --spares-file {OUT}/spares.json"  # #607
    assert read.plan_only and read.argv == read.dry_run
    assert " ".join(read.argv) == f"{teardown} --dry-run --only bob --only dave"
    assert action.dry_runs == (read.dry_run,) and action.applied == tuple(action.steps[1:])
    assert " ".join(prelude.argv).endswith("--start --only bob --only dave")  # no --wait
    assert prelude.tolerated and not prelude.row
    stages = ["start", "idle-stop", "waker", "uninstall", "final-sync", "protect", "teardown",
              "bots"]  # fmt: skip
    # without `unsynced`, nothing is tolerated: a failed protection stops its row too
    assert cells(action) == [
        (row, stage, False, stage not in ("waker", "teardown", "bots"), False)
        for row in ("bob", "dave") for stage in stages
    ]  # fmt: skip
    bob = {s.stage: " ".join(s.argv) for s in rows if s.row == "bob"}
    assert bob["start"].endswith("--start --wait --only bob")
    assert bob["idle-stop"].endswith("--idle-stop 0 --only bob")
    assert bob["waker"].endswith("--remove --only bob --apply")
    assert bob["uninstall"].endswith("--uninstall --only bob")
    assert bob["final-sync"].endswith(f"--out-dir {OUT} --strict --only bob")
    assert bob["protect"] == f"{PY}/aws/protect.py {TAG} --region us-west-2 --off --only bob"
    assert bob["teardown"] == f"{teardown} --yes --strict --only bob"
    assert bob["bots"].endswith("--deactivate --only bob")
    # the sync alone gets the token: it looks up the row's repository first (#714)
    assert {s.stage: s.env for s in rows if s.row == "bob"} == {
        stage: ("GH_TOKEN",) if stage == "final-sync" else () for stage in stages
    }
    assert all(s.dry_run is None for s in rows)
    assert action.targets == ("bob", "dave") and action.hashed == RETIRE
    assert action.timeout == RETIRE_FLOOR_S  # 60 minutes at least (review of #564)
    assert f"{OUT}/bob/claude.zuliprc" in action.inputs  # the bots' identities, as !deactivate-bots
    assert action.status is not None and action.status[-4:] == ("--only", "bob", "--only", "dave")


def test_unsynced_tolerates_the_work_saving_steps_and_takes_a_row_never_launched():
    with pytest.raises(ActionError, match=r"^carol never launched, or torn down already \(no ip"):
        make("!retire bob carol")
    action = make("!retire bob carol unsynced")
    tolerated = {(row, stage) for row, stage, tol, _, _ in cells(action) if tol}
    gone = {(row, stage) for row, stage, _, vm, gone in cells(action) if gone}
    for row in ("bob", "carol"):
        assert {(row, s) for s in ("start", "idle-stop", "uninstall", "final-sync")} \
            == {(r, s) for r, s in tolerated if r == row}  # fmt: skip
        assert {(row, s) for s in ("start", "idle-stop", "uninstall", "final-sync", "protect")} \
            == {(r, s) for r, s in gone if r == row}  # fmt: skip
    assert "**`unsynced`** covers every row of this plan" in "\n".join(action.notes)
    assert "covers every row" not in "\n".join(make("!retire bob").notes)
    assert " ".join(action.steps[1].argv).endswith("--start --only bob")  # carol has no ip
    notes = "\n".join(action.notes)
    assert "- carol: no ip in the roster; no bots; no VM to sync: torn down unsynced" in notes
    assert "- bob: VM at 198.51.100.2; no bots; synced, then torn down (unsynced if" in notes


def test_export_adds_a_step_and_its_time_and_takes_a_few_rows():
    action = make("!retire bob export")
    export = [s for s in action.steps if s.stage == "export"]
    assert len(export) == 1 and " ".join(export[0].argv).endswith(
        f"--out-dir {OUT}/exports --only bob"
    )
    stages = [s.stage for s in action.steps if s.row]
    assert stages.index("final-sync") < stages.index("export") < stages.index("protect")
    assert action.timeout == RETIRE_FLOOR_S + EXPORT_ROW_S and EXPORT_ROWS == 3
    rows = replace(FLEET, rows=tuple(replace(r, ip=r.ip or "198.51.100.3") for r in FLEET.rows))
    with pytest.raises(ActionError, match="`export` takes at most 3 rows"):
        make("!retire alice bob carol dave export", rows)
    three = make("!retire alice bob carol export", rows).timeout
    assert three == 3 * RETIRE_ROW_S + 3 * EXPORT_ROW_S


def test_the_protection_wait_refuses_it_as_a_teardown():
    """#378: within 5 minutes of a protection change, as `!teardown` (the guard's own tests)."""
    at = 1_800_000_000.0
    recent = replace(FLEET, protection=Protection("P-41", "protect", at), read_at=at + 60)
    with pytest.raises(ActionError, match="no !teardown or !retire for 5 minutes"):
        make("!retire bob", recent)
    later = replace(recent, read_at=at + 302)
    assert make("!retire bob", later).steps


def test_the_class_and_the_phrase():
    assert approvals.DESTRUCTIVE_VERBS == {"teardown", "deactivate-bots", "retire"}
    for message, phrase in [
        ("!retire bob", ("retire", "1")),
        ("!retire bob dave", ("retire", "2")),
        ("!retire bob carol unsynced", ("retire", "2", "unsynced")),
        ("!retire dave export", ("retire", "1")),
    ]:
        needs = requirement(make(message), FLEET, CONFIG)
        assert needs == Requirement(DESTRUCTIVE, CONFIG.destructive_approvals, phrase), message


def test_the_hash_covers_each_rows_vms_and_the_teardowns_dry_run():
    action = make("!retire bob dave")
    dry = "terminate i-2 ws-bob (running, participant bob)\nrelease eipalloc-2 198.51.100.2"
    live = Live(vms={"bob": "i-2", "dave": "i-4"}, dry_run=dry, tmpdir="/var/lib/zab-admin/tmp/P-1")

    def digest(**kw):
        return plan_hash(action, release="v1", digests=dict.fromkeys(action.inputs, ""),
                         live=replace(live, **kw))  # fmt: skip

    same = digest()
    assert digest(dry_run=dry.replace("running", "stopped")) == same  # idle stop: no change
    assert digest(dry_run="\n".join(reversed(dry.split("\n")))) == same  # EC2's order
    assert digest(vms={"bob": "i-2"}) != same  # dave's VM gone
    assert digest(vms={"bob": "i-2", "dave": "i-9"}) != same
    assert digest(dry_run=dry.replace("terminate", "keep")) != same  # protected since


# ---------------------------------------------------------------------- the ledger


def end_record(rows, *, exit=1, plan="P-50", targets=("alice", "bob"), ts="2026-10-29T14:02:11Z"):
    ends = {row: RowEnd(tuple(cells.items()), retired) for row, (cells, retired) in rows.items()}
    end = ApplyEnd("harvard-2026", plan, "retire", targets, exit, targets, ends)
    return {"event": "apply_end", "ts": ts, **end.fields()}


ALICE_DONE = dict.fromkeys(("start", "idle-stop", "waker", "uninstall", "final-sync", "protect",
                            "teardown", "bots"), "ok")  # fmt: skip
BOB_STOPPED = {**ALICE_DONE, "final-sync": "failed: exit 1", "protect": "not run",
               "teardown": "not run", "bots": "not run"}  # fmt: skip


def test_the_ledger_records_each_rows_steps_whatever_the_plans_exit():
    ledger = Ledger()
    start = {"event": "apply_start", "workshop": "harvard-2026", "plan": "P-50", "verb": "retire",
             "args": ["alice", "bob"], "targets": ["alice", "bob"]}  # fmt: skip
    synced = end_record({}, exit=0, plan="P-40", targets=("alice", "bob"))
    synced |= {"verb": "final-sync", "rows": None}
    del synced["rows"]
    ledger.observe({**synced, "event": "apply_end"})
    ledger.observe(start)  # a !retire forgets the syncs, as every TOUCH verb does
    assert ledger.workshop("harvard-2026").synced == {}
    ledger.observe(end_record({"alice": (ALICE_DONE, True), "bob": (BOB_STOPPED, False)}))
    state = ledger.workshop("harvard-2026")
    when = "P-50 at 2026-10-29 14:02 UTC"
    assert state.down == {"alice": when, "bob": when}
    assert state.synced == {"alice": (when, when)}  # after its uninstall: it counts
    assert state.retired == {"alice": when}
    ending = state.ending()
    assert ending["alice"].counts and not ending["bob"].counts


@pytest.mark.parametrize(
    "rows",
    [
        "alice",
        {"alice": {"cells": {"uninstall": "ok"}}},  # no `retired`
        {"alice": {"cells": {"uninstall": "ok"}, "retired": "yes"}},
        {"alice": {"cells": {"uninstall": 0}, "retired": True}},
        {"alice": {"cells": ["uninstall"], "retired": True}},
        {"erin": {"cells": {"uninstall": "ok"}, "retired": True}},  # not a target
        {"alice": {"cells": {"uninstall": "ok"}, "retired": True, "seconds": "60"}},
    ],
)
def test_rows_that_dont_read_back_exactly_mark_nothing(rows):
    record = end_record({}) | {"rows": rows}
    assert retire_rows(record, ("alice", "bob")) is None
    ledger = Ledger()
    ledger.observe(record)
    state = ledger.workshop("harvard-2026")
    assert (state.down, state.synced, state.retired) == ({}, {}, {})


@pytest.mark.parametrize("verb", ["launch", "adopt-spare"])
def test_a_launch_of_a_retired_row_puts_it_back_in_service_from_its_start(verb):
    """Review of #565, N1: from its `apply_start`, whatever its outcome: a launch that failed
    may have left a VM, which `all` must reach. A record without its targets puts every row
    back (it fails closed); a launch of another row puts none back."""
    ledger = Ledger()
    ledger.observe(end_record({"alice": (ALICE_DONE, True), "bob": (ALICE_DONE, True)}))
    start = {"event": "apply_start", "workshop": "harvard-2026", "plan": "P-51", "verb": verb,
             "args": ["carol"], "targets": ["carol"]}  # fmt: skip
    ledger.observe(start)
    assert set(ledger.workshop("harvard-2026").retired) == {"alice", "bob"}
    ledger.observe({**start, "args": ["alice"], "targets": ["alice"]})  # it may fail after
    assert set(ledger.workshop("harvard-2026").retired) == {"bob"}
    ledger.observe({k: v for k, v in start.items() if k != "targets"})
    assert ledger.workshop("harvard-2026").retired == {}


@pytest.mark.parametrize(
    "message, refused",
    [
        ("!start bob", True), ("!stop bob", True), ("!rollout v0.2.2 bob", True),
        ("!idle-stop 0 bob", True), ("!uninstall bob", True), ("!final-sync bob", True),
        ("!export bob", True), ("!protect off bob", True), ("!provision repos bob", True),
        ("!waker on bob", True), ("!grow-disk bob 300GB", True),
        ("!waker off bob", False), ("!status bob", False),
        ("!retire bob unsynced", False), ("!teardown bob unsynced", False),
        ("!deactivate-bots bob", False), ("!provision zulip bob", False),
    ],
)  # fmt: skip
def test_a_retired_row_named_is_refused_by_the_verbs_that_reach_its_vm(message, refused, wheels):
    """Review of #565, N3: its Elastic IP was released, and the address may be another VM's by
    now; only the verbs that bring it back, end it, read it or don't reach its VM name it."""
    retired = replace(FLEET, retired={"bob": "P-50 at 2026-10-29 14:02 UTC"})
    if not refused:
        assert make(message, retired, wheels).targets == ("bob",), message
        return
    why = r"^bob retired \(P-50 at 2026-10-29 14:02 UTC\): its VM is gone, .* To bring it back, "
    why += "a deployer clears its host and ip"
    with pytest.raises(ActionError, match=why):
        make(message, retired, wheels)


#: The verbs that may still name a retired row, each with why (#565): every other verb of
#: `actions.VERBS` reaches its VM, by its roster address or its tag, and is in `REACH_VM`.
NAME_A_RETIRED_ROW = {
    "launch": "brings it back, once a deployer cleared its host and ip (BACK)",
    "adopt-spare": "brings it back, likewise",
    "retire": "ends it again: its plan says `retired already`",
    "teardown": "finds VMs by their tag only, so a stray left with it goes too",
    "status": "a read",
    "deactivate-bots": "Zulip only",
    "provision zulip": "Zulip only",
    "roster import": "the roster only: it names no row",
    # #627: the roster only; the script refuses a row with a VM, bots or a sandbox
    "roster sync": "the roster only: it names no row, and keeps a row with anything provisioned",
    "roster remove": "the roster only: refused for a row with anything provisioned",
    "roster restore": "the roster only: its mark",
    "roster rename": "the roster only: refused for a row with anything provisioned",
    "new-workshop": "the server's: a new workshop has no row",
    "waker": "`off` only: `on` looks each VM up by its address (`actions._reaches_vm`)",
}


def test_every_verb_is_classified_for_a_retired_row():
    """The review of #565: a verb added later (as `!grow-disk` was, after #565's review) fails
    here until it is classified: in `REACH_VM`, refused for a retired row named, or allowed
    above with its reason. The grammar's verbs without an action are reads and approvals,
    which plan nothing."""
    assert REACH_VM.isdisjoint(NAME_A_RETIRED_ROW), REACH_VM & NAME_A_RETIRED_ROW.keys()
    unclassified = set(VERBS) - REACH_VM - NAME_A_RETIRED_ROW.keys()
    assert not unclassified, f"classify for a retired row (#565): {sorted(unclassified)}"
    assert not (REACH_VM | NAME_A_RETIRED_ROW.keys()) - set(VERBS)  # no stale entry
    assert {SPECS[verb].kind for verb in set(SPECS) - set(VERBS)} <= {"read", "approval"}


@pytest.mark.parametrize("message", ["!launch bob", "!adopt-spare bob zab-spare-1"])
def test_a_retired_row_comes_back_only_once_its_host_and_ip_are_cleared(message, wheels):
    """Review round 2 of #565: a retired row keeps its `host` and `ip` in the roster, with which
    launch.py leaves it alone (exit 0, no VM) and adopt_spare.py refuses it; its `apply_start`
    would have put it back in `all` with a released address. Refused until a deployer clears
    both; then it comes back."""
    when = "P-50 at 2026-10-29 14:02 UTC"
    for row in (Row("bob", "198.51.100.2"), Row("bob", host="ws-bob")):
        rows = tuple(row if r.slug == "bob" else r for r in FLEET.rows)
        kept = replace(FLEET, rows=rows, retired={"bob": when})
        why = (
            r"^bob retired \(P-50 at 2026-10-29 14:02 UTC\), and the roster keeps its host or "
            r"ip, so launch.py would leave it alone and adopt_spare.py refuse it. To bring it "
            r"back, a deployer clears its host and ip in the workshop's roster.csv"
        )
        with pytest.raises(ActionError, match=why):
            make(message, kept, wheels)
    cleared = tuple(Row("bob") if r.slug == "bob" else r for r in FLEET.rows)
    back = make(message, replace(FLEET, rows=cleared, retired={"bob": when}), wheels)
    assert back.targets[0] == "bob"
    assert make("!launch all", replace(FLEET, retired={"bob": when}), wheels).targets == (
        "alice", "carol", "dave",
    )  # fmt: skip


def test_the_longest_refusals_of_retired_rows_fit_a_plans_note(wheels):
    """#567: `BACK` says who runs the roster command and that the bots stay deactivated; the
    refusal of more rows than `_some` names, with the longest slugs and plan ID, still fits
    `NOTE_CHARS` whole, as `Planning._apply`'s STALE too."""
    slugs = [str(n) * 32 for n in range(1, 8)]  # grammar.SLUG_RE's longest
    rows = tuple(Row(s, f"198.51.100.{n}", host=f"ws-{s}") for n, s in enumerate(slugs, 1))
    when = "P-999999999 at 2026-10-29 14:02 UTC"
    fleet = replace(FLEET, rows=rows, retired=dict.fromkeys(slugs, when))
    for verb in ("!launch", "!stop"):  # `BACK`, then the verbs that reach a VM
        with pytest.raises(ActionError) as refused:
            make(f"{verb} {' '.join(slugs)}", fleet, wheels)
        note = f"it no longer builds: {refused.value}"
        assert "and 2 more retired" in note and note.endswith(BACK)
        assert len(note) <= NOTE_CHARS, len(note)
    assert "as zab-admin on the server (ADMIN §3.3)" in BACK
    assert "Its bots stay deactivated: only a Zulip organization administrator" in BACK


def test_all_leaves_the_retired_rows_out_and_they_never_count_in_the_fleet():
    retired = replace(FLEET, retired={"bob": "P-50 at 2026-10-29 14:02 UTC"})
    assert make("!stop all", retired).targets == ("alice", "carol", "dave")
    # power.py's `--all` would take bob, whose row keeps its ip: the rows are named instead,
    # those with an ip (carol has none, which `--all` skipped), then the spares (Codex on #565)
    for message, named in [
        ("!stop all", "--stop --only alice --only dave"),
        ("!start all spares", "--start --only alice --only dave --only zab-spare-1 "
                              "--only zab-spare-2"),
    ]:  # fmt: skip
        step = make(message, retired).steps[0]
        assert " ".join(step.argv).endswith(named) and "--all" not in step.argv, message
        assert step.dry_run == (*step.argv, "--dry-run")
    assert make("!stop all").steps[0].argv[-2:] == ("--stop", "--all")  # none retired: as before
    assert make("!uninstall all", retired).targets == ("alice", "dave")  # the rows with a VM
    assert make("!status bob", retired).targets == ("bob",)  # named: still served
    # the fleet is alice and dave now: both of them is above half, as `all` is
    needs = requirement(make("!stop alice dave", retired), retired, CONFIG)
    assert needs.phrase == ("stop", "2")
    assert requirement(make("!stop alice", retired), retired, CONFIG).phrase == ()
    gone = replace(FLEET, retired=dict.fromkeys(FLEET.slugs, "P-50 at …"))
    with pytest.raises(ActionError, match="nothing selected"):
        make("!stop all", gone)
    assert "retired already, P-50 at" in "\n".join(make("!retire bob unsynced", retired).notes)


@pytest.mark.parametrize("ip", ["198.51.100.2", ""])
def test_all_names_the_rows_only_while_a_retired_row_keeps_an_ip(ip):
    """The combined admin run of Oct 23 (#606): bob retired, his `ip` cleared by his teardown
    and his `host` kept (#604). `!stop all` and `!start all` pass power.py's `--all`, which
    takes only the rows with an ip (tests/workshop/test_power.py), so it leaves bob out by
    itself. While he keeps an ip (his VM gone by hand and retired `unsynced`, or retired before
    #604), the rows are named instead (Codex on #565). Either way no plan reaches him."""
    rows = tuple(replace(r, ip=ip, host="ws-bob") if r.slug == "bob" else r for r in FLEET.rows)
    retired = replace(FLEET, rows=rows, retired={"bob": "P-50 at 2026-10-29 14:02 UTC"})
    for message, flag in (("!stop all", "--stop"), ("!start all", "--start")):
        action = make(message, retired)
        argv = " ".join(action.steps[0].argv)
        assert "bob" not in action.targets and "bob" not in argv.split(), (message, argv)
        assert argv.endswith(f"{flag} --only alice --only dave" if ip else f"{flag} --all"), argv


def test_status_all_names_the_rows_it_leaves_out():
    retired = replace(FLEET, retired={"bob": "P-50 at 2026-10-29 14:02 UTC", "gone": "P-1"})
    line = planning.left_out(retired)
    assert line == "*Retired, so left out: bob (P-50 at 2026-10-29 14:02 UTC).*"
    many = replace(FLEET, retired={s: "P-50" for s in FLEET.slugs})
    assert planning.left_out(replace(FLEET)) == ""
    assert planning.left_out(many).count("(P-50)") == 4


def test_the_fleet_checks_its_retired_rows():
    with pytest.raises(ActionError, match="each retired row"):
        dataclasses.replace(FLEET, retired={"bob": 1})


# ------------------------------------------------------------------- through the bot


#: Fakes of the scripts a `!retire` runs, each one line per call; final_sync.py fails for a row
#: named in `fail-sync`, teardown.py --strict for a VM still protected (`protected`).
FAKES = {
    "aws/teardown.py": """\
        only = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--only"]
        prot = pathlib.Path({prot!r})
        for slug in only:
            if slug in ("alice", "bob"):
                if prot.exists() and slug in prot.read_text().split():
                    print(f"keep i-{{slug}} ws-{{slug}}: termination protection is on")
                    if "--strict" in sys.argv and not DRY:
                        print(f"refused i-{{slug}} ws-{{slug}} (participant {{slug}}): protected")
                        sys.exit(1)
                    continue
                print(f"terminate i-{{slug}} ws-{{slug}} (running, participant {{slug}})")
        if not DRY:
            print("terminating; waiting")
    """,
    "aws/protect.py": """\
        prot = pathlib.Path({prot!r})
        slug = sys.argv[sys.argv.index("--only") + 1]
        if prot.exists():
            prot.write_text(" ".join(s for s in prot.read_text().split() if s != slug))
        print(f"{{slug}}: termination protection off: set; EC2 shows it now")
    """,
    "aws/power.py": 'print("started" if "--wait" in sys.argv else "start requested")',
    "admin/push_config.py": 'print("idle off" if "--idle-stop" in sys.argv else "uninstalled")',
    "admin/push_waker.py": 'print("removed from the waker")',
    "admin/final_sync.py": """\
        slug = sys.argv[sys.argv.index("--only") + 1]
        fail = pathlib.Path({fail!r})
        if fail.exists() and slug in fail.read_text().split():
            print(f"{{slug}}: not synced"); sys.exit(1)
        gone = pathlib.Path({gone!r})
        if gone.exists() and slug in gone.read_text().split():
            print(f"MathAgents/{{slug}} no longer exists here: the participant deleted or "
                  "transferred it"); sys.exit(4)
        print(f"{{slug}}: synced; report: no problems")
    """,
    "admin/collect_exports.py": 'print("archives: 1; no problems")',
    "admin/zulip_provision.py": 'print("codex: deactivated, claude: deactivated")',
    "admin/status.py": 'print(json.dumps({"schema": 1, "vms": [], "summary": {}}))',
}


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    layout = replace(site.paths.layout, release=str(tmp_path / "release"), python=sys.executable)
    site.paths = replace(site.paths, layout=layout)
    for name, value in (("PROGRESS", 0.05), ("GRACE", 0.3), ("STOP_GRACE", 0.3), ("DRAIN", 1.0)):
        monkeypatch.setattr(runner_mod, name, value)
    marks = {"prot": str(tmp_path / "protected"), "fail": str(tmp_path / "fail-sync"),
             "gone": str(tmp_path / "gone-sync")}  # fmt: skip
    for name, body in FAKES.items():
        fake(site, name, body.format(**marks) if "!r}" in body else body)
    site.vms = {"alice": "i-1", "bob": "i-2"}
    site.tagged = None  # the VMs tagged with each slug exactly: the same as `vms`, unless set
    return site


@pytest.fixture
async def server(site):
    srv = site.server()

    def lookup(which):
        def found(fleet_, names):
            vms = getattr(site, which) if getattr(site, which) is not None else site.vms
            return {n: vms[n] for n in names if n in vms}

        return found

    srv.planner = Planning(Runner(srv, vms=lookup("vms"), tagged=lookup("tagged")))
    await srv.start()
    yield srv
    await srv.close()


def ran(site, slug):
    """The scripts that ran for `slug` alone, in order: not the plan's reads, nor the start
    of every row's VM before the rows (`power.py` without `--wait`)."""
    out = []
    for c in calls(site):
        only = [c["argv"][i + 1] for i, a in enumerate(c["argv"]) if a == "--only"]
        prelude = c["script"] == "aws/power.py" and "--wait" not in c["argv"]
        if not c["dry"] and only == [slug] and c["script"] != "admin/status.py" and not prelude:
            out.append(c["script"])
    return out


EVERY = ["aws/power.py", "admin/push_config.py", "admin/push_waker.py", "admin/push_config.py",
         "admin/final_sync.py", "aws/protect.py", "aws/teardown.py",
         "admin/zulip_provision.py"]  # fmt: skip


async def retire(srv, text, phrase, sender=DREW, mid=5000):
    plan = await request(srv, text, mid=mid)
    assert plan.state == PENDING, plan.note
    await approve(srv, plan, sender=sender, phrase=phrase, mid=LATE + mid)
    await settle(srv)
    return srv.store.get(plan.id)


async def test_a_failed_sync_spares_its_row_and_the_others_go_on(site, server):
    (site.tmp / "fail-sync").write_text("bob")
    plan = await retire(server, "!retire alice bob", "retire 2")
    assert plan.state == FAILED
    assert ran(site, "alice") == EVERY
    assert ran(site, "bob") == EVERY[:5]  # up to its failed sync: no teardown, no bots
    prelude = next(c for c in calls(site, "aws/power.py") if not c["dry"])
    assert prelude["argv"][-4:] == ["--only", "alice", "--only", "bob"]  # boots them together
    assert "--wait" not in prelude["argv"]
    result = posts(site)[-1]
    head = result.split("\n", 1)[0]
    assert head == (
        f"❌ **{plan.name}** · `!retire alice bob`: 1 of 2 rows retired; bob stopped at "
        "final-sync. The other rows went on; send it again for those stopped"
    )
    table = result.split("```text\n", 1)[1].split("\n```", 1)[0].split("\n")
    assert table[0].split() == ["participant", "start", "idle-stop", "waker", "uninstall",
                                "final-sync", "protect", "teardown", "bots", "time",
                                "retired"]  # fmt: skip
    alice, bob = (re.split(r" {2,}", line.strip()) for line in table[2:4])
    assert alice[:-2] == ["alice", *["ok"] * 8] and alice[-1] == "yes"
    assert re.fullmatch(r"\d+ s", alice[-2]), alice  # each row's time (review of #564)
    assert bob[:-2] == ["bob", *["ok"] * 4, "failed: exit 1", *["not run"] * 3]
    assert bob[-1] == "no"
    assert "Remind the participants to revoke their other sessions" in result
    end = records(site, "apply_end")[-1]
    assert end["exit"] == 1 and end["outcome"] == "failed"
    assert end["rows"]["alice"]["retired"] is True and end["rows"]["bob"]["retired"] is False
    assert end["rows"]["bob"]["cells"]["final-sync"] == "failed: exit 1"
    assert all(type(end["rows"][r]["seconds"]) is int for r in ("alice", "bob"))
    state = server.ledger.workshop("harvard-2026")
    assert set(state.retired) == {"alice"} and set(state.down) == {"alice", "bob"}
    assert state.ending()["alice"].counts and not state.ending()["bob"].counts
    # `all` leaves alice out from now on, and `!status all` says so
    stop = await request(server, "!stop all", mid=5001)
    assert stop.targets == ("bob", "carol")
    begin = await request(server, "!start all", mid=5003)  # through the bot (review of #565)
    power = [c for c in calls(site, "aws/power.py") if c["dry"]][-1]["argv"]
    assert begin.targets == ("bob", "carol") and "--all" not in power
    assert power[-3:] == ["--only", "bob", "--dry-run"]  # carol has no ip: power.py's --all skips
    await status(server, "!status all", mid=5002)
    read = [c for c in calls(site, "admin/status.py")][-1]["argv"]
    assert "alice" not in read and read[-4:] == ["--only", "bob", "--only", "carol"]
    assert re.search(r"\n\*Retired, so left out: alice \(P-\d+ at [-0-9: ]+ UTC\)\.\*\n",
                     posts(site)[-1])  # fmt: skip


async def test_unsynced_retires_a_row_whose_vm_is_gone(site, server):
    """#559: a VM terminated by hand (no live instance) is retired with
    `unsynced`: nothing that needs the VM runs; the waker, the teardown (which finds nothing)
    and the bots do."""
    del site.vms["bob"]
    plan = await retire(server, "!retire bob unsynced", "retire 1 unsynced")
    assert plan.state == DONE, posts(site)[-1][:300]
    assert ran(site, "bob") == ["admin/push_waker.py", "aws/teardown.py",
                                "admin/zulip_provision.py"]  # fmt: skip
    end = records(site, "apply_end")[-1]
    cells = end["rows"]["bob"]["cells"]
    assert [cells[s] for s in ("start", "uninstall", "final-sync", "protect")] == [
        "skipped: no VM"] * 4  # fmt: skip
    assert end["rows"]["bob"]["retired"] is True and end["exit"] == 0
    head = posts(site)[-1].split("\n", 1)[0]
    assert head.startswith(f"✅ **{plan.name}** · `!retire bob unsynced`: done in ")
    assert head.endswith(", 1 of 1 rows retired")
    assert set(server.ledger.workshop("harvard-2026").retired) == {"bob"}
    assert set(server.ledger.workshop("harvard-2026").down) == set()  # never uninstalled


async def test_without_unsynced_a_row_with_no_vm_is_refused_at_plan_time(site, server):
    """Review of #564, N1: a row with an `ip` whose VM the bot doesn't find (terminated by
    hand) is refused before the approval, naming it; the plan says so with `unsynced`."""
    del site.vms["bob"]
    plan = await request(server, "!retire bob")
    assert plan.state == REFUSED and "no VM found in us-west-2 for bob: add `unsynced`" in (
        plan.note
    )
    assert "bob: no VM found" in posts(site)[-1] and ran(site, "bob") == []
    plan = await request(server, "!retire bob unsynced", mid=5001)
    assert plan.state == PENDING
    assert "bob: no VM found: the steps that need one are skipped (`unsynced`)" in posts(site)[-2]


async def test_at_apply_a_row_whose_vm_went_fails_and_nothing_goes(site, server):
    """A VM gone between plan and apply makes the plan STALE (its VMs are hashed)."""
    plan = await request(server, "!retire bob")
    del site.vms["bob"]
    await approve(server, plan, sender=DREW, phrase="retire 1", mid=LATE)
    await settle(server)
    assert server.store.get(plan.id).state == STALE and ran(site, "bob") == []


async def test_a_row_never_launched_needs_unsynced(site, server):
    refused = await request(server, "!retire carol")
    assert refused.state == REFUSED and "carol never launched" in refused.note
    plan = await retire(server, "!retire carol unsynced", "retire 1 unsynced", mid=5001)
    assert plan.state == DONE
    assert ran(site, "carol") == ["admin/push_waker.py", "aws/teardown.py",
                                  "admin/zulip_provision.py"]  # fmt: skip


async def test_a_protected_row_is_unprotected_then_torn_down(site, server):
    (site.tmp / "protected").write_text("alice")
    plan = await request(server, "!retire alice")
    shown = posts(site)[-2]
    assert "keep i-alice ws-alice: termination protection is on" in shown
    assert "A VM the dry run lists `keep` is protected (kept)" in shown
    await approve(server, plan, sender=DREW, phrase="retire 1", mid=LATE)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    order = ran(site, "alice")
    assert order.index("aws/protect.py") < order.index("aws/teardown.py")
    assert (site.tmp / "protected").read_text() == ""


async def test_a_row_still_protected_after_the_wait_is_not_torn_down(site, server):
    """`teardown.py --strict` fails a VM EC2 still reads as protected: the row stops, its
    bots stay, and it is not retired."""
    (site.tmp / "protected").write_text("alice")
    fake(site, "aws/protect.py", 'print("alice: set; EC2 doesn\'t show it yet")')
    plan = await retire(server, "!retire alice", "retire 1")
    assert plan.state == FAILED
    cells = records(site, "apply_end")[-1]["rows"]["alice"]["cells"]
    assert (cells["protect"], cells["teardown"], cells["bots"]) == ("ok", "failed: exit 1",
                                                                    "not run")  # fmt: skip
    assert not server.ledger.workshop("harvard-2026").retired


async def test_an_organizer_needs_a_second_approver(site, server):
    plan = await request(server, "!retire alice")
    await approve(server, plan, sender=OLGA, phrase="retire 1", mid=LATE)
    await settle(server)
    assert server.store.get(plan.id).state == PENDING
    assert "1 of 2 approvals, from distinct approvers" in posts(site)[-1]
    await approve(server, plan, sender=OSCAR, phrase="retire 1", mid=LATE + 1)
    await settle(server)
    assert server.store.get(plan.id).state == DONE


async def test_a_vm_that_appears_makes_the_plan_stale(site, server):
    plan = await request(server, "!retire carol unsynced")
    site.vms["carol"] = "i-3"  # launched since, by hand
    await approve(server, plan, sender=DREW, phrase="retire 1 unsynced", mid=LATE)
    await settle(server)
    assert server.store.get(plan.id).state == STALE
    assert "carol: no VM → i-3" in posts(site)[-1]
    assert ran(site, "carol") == []


@pytest.mark.parametrize("length", [303, 2 * NOTE_CHARS])
async def test_a_plan_that_no_longer_builds_keeps_its_reason(site, server, monkeypatch, length):
    """#567, item 1: the STALE of a plan that no longer builds at apply (`Planning._apply`)
    keeps `NOTE_CHARS` of its reason, as a refusal does, and not 300: the refusal of one
    retired row by `!launch` (#565's `BACK`) is 303 characters, and comes back that way."""
    plan = await request(server, "!retire alice")
    why = "w" * length

    async def no_longer(bot, plan):
        raise ActionError(why)

    monkeypatch.setattr(server.planner, "rebuild", no_longer)
    await approve(server, plan, sender=DREW, phrase="retire 1", mid=LATE)
    await settle(server)
    note = f"it no longer builds: {why}"[:NOTE_CHARS]
    assert len(note) == min(21 + length, NOTE_CHARS)
    assert server.store.get(plan.id).state == STALE and server.store.get(plan.id).note == note
    assert posts(site)[-1].startswith(f"❌ {plan.name} is stale: {note}. Request again.")
    assert records(site, "stale")[-1]["reason"] == note and ran(site, "alice") == []


async def test_export_names_each_archive(site, server):
    plan = await retire(server, "!retire alice export", "retire 1")
    assert plan.state == DONE
    assert "admin/collect_exports.py" in ran(site, "alice")
    result = posts(site)[-1]
    assert "- alice's archive: `out/exports/export-alice.tar.gz`, on the server" in result


def test_the_apply_deadline_is_the_actions():
    action = make("!retire bob dave export")
    assert limit(action) == RETIRE_FLOOR_S + 2 * EXPORT_ROW_S
    assert limit(make("!stop bob")) == runner_mod.TIMEOUT
    assert json.dumps(ApplyEnd("w", "P-1", "stop", (), 0).fields())  # no rows: none written
    assert "rows" not in ApplyEnd("w", "P-1", "stop", (), 0).fields()


# --------------------------------------------- the VM synced is the VM torn down (review of #564)


@pytest.mark.parametrize(
    "vms, tagged, shown",
    [
        # a legacy tag in another case (participant=Alice): its scripts reach it, its teardown not
        ({"alice": "i-1"}, {}, "alice: i-1, but tagged participant=alice: none"),
        # a spare handed over by hand, at alice's address and still tagged participant=zab-spare-1
        ({"alice": "i-7"}, {}, "alice: i-7, but tagged participant=alice: none"),
        # the VM tagged alice, and another at her address, which her sync would reach
        (
            {"alice": "i-1,i-7"},
            {"alice": "i-1"},
            "alice: i-1,i-7, but tagged participant=alice: i-1",
        ),
    ],
)
async def test_a_row_whose_vms_differ_from_those_tagged_is_refused(
    site, server, vms, tagged, shown
):
    """B1: the VM its steps sync (by its address, or a tag in any case: `provider.targets`)
    must be the one its protection and teardown end (the tag exactly: `provider.tagged`).
    Refused before anything runs, naming the row and what to fix; bob alone is fine."""
    site.vms, site.tagged = {**vms, "bob": "i-2"}, {**tagged, "bob": "i-2"}
    plan = await request(server, "!retire alice bob")
    assert plan.state == REFUSED, plan.state
    assert plan.note.startswith("alice: its steps reach VMs (by its address, or a tag in another")
    assert plan.note.endswith(
        "tag the VM exactly (a spare handed over keeps participant=zab-spare-N), or end it by hand"
    )  # never cut (N5)
    assert shown in posts(site)[-1] and "bob: i-2" in posts(site)[-1]
    assert [c for c in calls(site) if not c["dry"] and c["script"] != "admin/status.py"] == []
    assert (await request(server, "!retire bob", mid=5001)).state == PENDING


async def test_a_row_with_two_vms_tagged_with_its_slug_is_refused(site, server):
    """Codex on #564: two VMs tagged participant=alice: her steps reach the one at her address,
    her teardown would end both, the other's work unsynced. Refused before anything runs."""
    site.vms = site.tagged = {"alice": "i-1,i-2", "bob": "i-2b"}
    plan = await request(server, "!retire alice bob")
    assert plan.state == REFUSED
    assert plan.note.startswith("alice: more than one VM tagged with its slug: its steps sync")
    assert "alice: i-1,i-2: more than one VM" in posts(site)[-1]
    assert [c for c in calls(site) if not c["dry"] and c["script"] != "admin/status.py"] == []


async def test_a_tag_changed_between_plan_and_apply_makes_it_stale(site, server):
    plan = await request(server, "!retire alice")
    site.tagged = {}  # alice's VM is no longer tagged participant=alice
    await approve(server, plan, sender=DREW, phrase="retire 1", mid=LATE)
    await settle(server)
    assert server.store.get(plan.id).state == STALE and ran(site, "alice") == []
    said = posts(site)[-1]
    assert "alice: its steps reach VMs" in said and "or end it by hand. Request again." in said


def test_the_exact_tag_lookup_is_the_teardowns(tmp_path):
    """`provider.tagged` finds a VM by `participant=<slug>` exactly, as protect.py and
    teardown.py select; `provider.targets` also by the address and in any case."""
    from zab.admin import provider, starter
    from zab.admin.fleet import read_fleet

    from .test_admin_runner import FakeEc2Client, _vm

    site = make_site(tmp_path)
    srv = site.server()
    current = read_fleet(srv.config, srv.config.workshop("harvard-2026"))
    scope = {"workshop": "harvard-2026", "zab-scope": "mathagents"}
    instances = [
        _vm("i-a", scope | {"participant": "Alice"}),  # a legacy tag
        _vm("i-s", scope | {"participant": "zab-spare-1"}, ip="198.51.100.2"),  # bob's, by hand
        _vm("i-b", scope | {"participant": "bob"}),
    ]
    names = ("alice", "bob")
    union = provider.targets(current, names, starter.Ec2(FakeEc2Client(list(instances))))
    exact = provider.tagged(current, names, starter.Ec2(FakeEc2Client(list(instances))))
    assert union == {"alice": "i-a", "bob": "i-b,i-s"} and exact == {"bob": "i-b"}
    at = provider.addressed(current, names, starter.Ec2(FakeEc2Client(list(instances))))
    assert at == {"bob": "i-s"}  # alice's VM is not at her address


# ----------------------------------------------------------------- the rows' failures (N6)


async def test_a_row_whose_repository_is_gone_says_so_and_how_to_end_it(site, server):
    """#714: the participant deleted or transferred their repository. The sync says so (its
    exit 4), the row's cell names it, and the note under the table points to `unsynced` and
    says what that loses."""
    (site.tmp / "gone-sync").write_text("bob")
    plan = await retire(server, "!retire alice bob", "retire 2")
    assert plan.state == FAILED and ran(site, "bob") == EVERY[:5]
    end = records(site, "apply_end")[-1]
    assert end["rows"]["bob"]["cells"]["final-sync"] == "failed: repository gone"
    result = posts(site)[-1]
    assert "MathAgents/bob no longer exists here: the participant deleted or transferred it" in (
        result
    )
    assert (
        "- bob stopped at final-sync: `MathAgents/bob` no longer exists here: the participant "
        "deleted or transferred it, so its VM's work can't be pushed there. `!retire bob "
        "unsynced` tears it down all the same, and loses the work that is only on its VM: what "
        "wasn't pushed before the repository went."
    ) in result


async def test_a_start_that_fails_stops_its_row_before_the_uninstall(site, server):
    """N6 (a), with N5: bob's start fails; nothing after it runs for him, alice goes on."""
    fake(
        site,
        "aws/power.py",
        """\
        only = sys.argv[sys.argv.index("--only") + 1]
        if "--wait" in sys.argv and only == "bob":
            print("bob: failed"); sys.exit(1)
        print("started")
    """,
    )
    plan = await retire(server, "!retire alice bob", "retire 2")
    assert plan.state == FAILED and ran(site, "bob") == ["aws/power.py"]
    cells = records(site, "apply_end")[-1]["rows"]["bob"]["cells"]
    assert cells["start"] == "failed: exit 1"
    assert {cells[s] for s in cells if s != "start"} == {"not run"}
    assert "- bob stopped at start (`failed: exit 1`): its VM is as it was." in posts(site)[-1]


async def test_a_row_stopped_after_idle_stop_says_how_to_put_it_back(site, server):
    """N5: bob stopped at his sync runs with idle stop off and his bridge uninstalled; the
    result says so, how to send him again, and the commands that put him back."""
    (site.tmp / "fail-sync").write_text("bob")
    await retire(server, "!retire bob", "retire 1")
    result = posts(site)[-1]
    said = ("- bob stopped at final-sync (`failed: exit 1`): its VM runs, with idle stop off, "
            "the waker off, its bridge uninstalled. Fix the cause, then send `!retire bob` "
            "again, or `!retire bob unsynced` to tear it down with its work.")  # fmt: skip
    assert said in result
    # no idle-stop minutes recorded: the rollout's default (#704), named, since the retire's
    # step turned bob's off on purpose, which the default leaves off
    assert ("  To put it back in service instead: `!waker on bob` (if it was opted in), then "
            "`!rollout <release> bob idle-stop=60`.") in result  # fmt: skip


async def test_the_deadline_in_a_row_stops_every_row_after_it(site, server, monkeypatch):
    """N6 (b): the deadline passes in alice's sync: `timed out`; bob's steps never run and
    nothing is marked for him; alice's uninstall, which exited 0, is."""
    from zab.admin import actions

    monkeypatch.setattr(actions, "RETIRE_FLOOR_S", 3.0)
    monkeypatch.setattr(actions, "RETIRE_ROW_S", 1.0)
    fake(site, "admin/final_sync.py", "time.sleep(30)")
    plan = await retire(server, "!retire alice bob", "retire 2")
    assert plan.state == FAILED
    end = records(site, "apply_end")[-1]
    assert end["outcome"] == "timed out" and end["rows"]["alice"]["cells"]["final-sync"] == (
        "failed: timed out"
    )
    assert set(end["rows"]["bob"]["cells"].values()) == {"not run"} and ran(site, "bob") == []
    assert posts(site)[-1].startswith(f"⏱ **{plan.name}** · `!retire alice bob`: timed out after ")
    state = server.ledger.workshop("harvard-2026")
    assert set(state.down) == {"alice"} and state.synced == {}


async def test_unsynced_tears_down_a_row_whose_sync_failed_and_says_so(site, server):
    """N6 (c), with N3: torn down, the ledger never marks it synced, the headline names it."""
    (site.tmp / "fail-sync").write_text("bob")
    plan = await retire(server, "!retire bob unsynced", "retire 1 unsynced")
    assert plan.state == DONE and "aws/teardown.py" in ran(site, "bob")
    head = posts(site)[-1].split("\n", 1)[0]
    assert head.endswith(", 1 of 1 rows retired; torn down unsynced: bob (final-sync failed)")
    state = server.ledger.workshop("harvard-2026")
    assert "bob" in state.down and "bob" not in state.synced


async def test_one_plan_mixes_a_row_through_a_failed_sync_and_a_row_with_no_vm(site, server):
    """N6 (d): alice through, bob's sync failing, carol with no VM, under `unsynced`."""
    (site.tmp / "fail-sync").write_text("bob")
    plan = await retire(server, "!retire alice bob carol unsynced", "retire 3 unsynced")
    assert plan.state == DONE
    rows = records(site, "apply_end")[-1]["rows"]
    assert all(rows[r]["retired"] for r in ("alice", "bob", "carol"))
    assert rows["alice"]["cells"]["final-sync"] == "ok"
    assert rows["bob"]["cells"]["final-sync"] == "failed: exit 1"
    assert rows["carol"]["cells"]["final-sync"] == "skipped: no VM"
    head = posts(site)[-1].split("\n", 1)[0]
    assert head.endswith("3 of 3 rows retired; torn down unsynced: bob (final-sync failed)")
    state = server.ledger.workshop("harvard-2026")
    assert set(state.synced) == {"alice"}


async def test_a_failed_protection_stops_its_row(site, server):
    """N6 (e), B1 (b): protect.py exits 1 for a row with a VM: its row stops there, nothing is
    torn down, and its bots stay, `unsynced` or not."""
    fake(site, "aws/protect.py", 'print("alice: FAILED: UnauthorizedOperation"); sys.exit(1)')
    for n, text in enumerate(("!retire alice", "!retire alice unsynced")):
        plan = await retire(server, text, "retire 1" + " unsynced" * n, mid=5000 + n)
        assert plan.state == FAILED
        cells = records(site, "apply_end")[-1]["rows"]["alice"]["cells"]
        stopped = (cells["protect"], cells["teardown"], cells["bots"])
        assert stopped == ("failed: exit 1", "not run", "not run"), cells
    assert "aws/teardown.py" not in ran(site, "alice")


async def test_each_step_names_its_row_in_the_output_and_the_record(site, server):
    """N7: `apply.txt` frames each run with its row, and `apply_end`'s steps name it."""
    plan = await retire(server, "!retire alice", "retire 1")
    end = records(site, "apply_end")[-1]
    assert end["steps"][0] == {"script": "aws/power.py", "exit": 0, "ended": ""}  # the prelude
    assert {s.get("row") for s in end["steps"][1:]} == {"alice"}
    saved = (Path(site.paths.state) / end["output"]).read_text()
    assert "── aws/teardown.py for alice\n" in saved
    assert "── aws/teardown.py for alice exited 0\n" in saved
    assert plan.state == DONE


async def test_a_result_too_long_to_show_is_attached_whole(site, server, monkeypatch):
    """N9: a result past Zulip's limit keeps its headline, the reminder and the footer, and
    attaches the whole of it, its table included."""
    monkeypatch.setattr(runner_mod, "RESULT_ROOM", 9990)
    await retire(server, "!retire alice", "retire 1")
    result = posts(site)[-1]
    assert "*Too long to show here: the whole result, its table included: [P-" in result
    (note,) = [line for line in result.split("\n") if line.startswith("*Too long to show here")]
    assert ZULIP_ITALIC.fullmatch(note), note  # #651: Zulip's italics, its link inside
    assert "Remind the participants to revoke their other sessions" in result
    whole = [v.decode() for k, v in site.realm.uploads.items() if k.endswith("-result.txt")]
    assert len(whole) == 1 and "participant  start" in whole[0]


# ------------------------------------------------- the commands that put a row back (round 2)


def test_the_put_back_commands_are_the_fleets_own():
    """Review round 2 of #564: the `!rollout` replays the fleet's settings whole, as
    `!adopt-spare` does (N2); the minutes are the ledger's, or the rollout's default, 60 (N3;
    #704), named all the same, since the default leaves an idle stop turned off on purpose off;
    the waker is put back for the row, plainly if the fleet's was on, else if it was opted in
    (N4)."""
    from zab.admin.runner import _put_back

    cells = dict.fromkeys(("start", "idle-stop", "waker", "uninstall"), "ok")
    words = ("steer=off", "multi-party=off", "lean-shared", "idle-stop=45")
    held = replace(FLEET, release="v0.2.2", waker=True, settings=words)
    assert _put_back(held, "bob", cells) == (
        "`!waker on bob`, then `!rollout v0.2.2 bob steer=off multi-party=off lean-shared "
        "idle-stop=45`"
    )
    assert _put_back(held, "bob", cells | {"uninstall": "not run"}) == (
        "`!waker on bob`, then `!idle-stop 45 bob`"
    )
    alone = replace(held, waker=False, settings=("steer=off",))
    assert _put_back(alone, "bob", cells) == (
        "`!waker on bob` (if it was opted in), then `!rollout v0.2.2 bob steer=off idle-stop=60`"
    )
    off = replace(held, settings=("idle-stop=0",))
    assert _put_back(off, "bob", cells) == "`!waker on bob`, then `!rollout v0.2.2 bob`"


async def test_a_vm_away_from_its_address_is_flagged_in_the_plan(site, server):
    """Review round 2 of #564, N1: bob's one VM, tagged exactly, isn't at his roster address:
    the plan says its steps can't reach it (shown, not refused: they would fail, and stop him
    before his teardown)."""
    server.planner.runner.addressed = lambda fleet_, names: {"alice": "i-1"}
    plan = await request(server, "!retire alice bob")
    assert plan.state == PENDING
    shown = posts(site)[-2]
    assert "\nalice: i-1\n" in shown
    assert "\nbob: i-2, not at its roster address 198.51.100.2: its steps can't reach it\n" in shown


async def test_status_all_with_every_row_retired_says_so(site, server):
    """Review of #565, N2: a kept fleet at its month end: no 'nothing selected'."""
    plan = await retire(server, "!retire alice bob carol unsynced", "retire 3 unsynced")
    assert plan.state == DONE
    await status(server, "!status all", mid=5002)
    answer = posts(site)[-1]
    assert answer.startswith("Every row is retired: nothing to read.\n\n*Retired, so left out: ")
    assert all(f"{r} (P-" in answer for r in ("alice", "bob", "carol"))
