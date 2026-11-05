"""Stage 3, the end of a workshop (docs/ADMIN.md §2.5, §3 action 7, §8.3 PR 19; #357):
`!final-sync`, `!uninstall`, `!teardown` and `!deactivate-bots` on the bots, with a fake realm
and a fake runner; the ledger's record of each row's uninstall and sync, a sync counting only
after the uninstall (decided on #367); the destructive class's distinct
approvers, phrase and veto, through the bot."""

import asyncio
import dataclasses

import pytest

from zab.admin import fleet
from zab.admin.actions import (
    DRY_RUN,
    INPUTS,
    RECONCILER,
    ActionError,
    Ending,
    Fleet,
    Layout,
    Live,
    Row,
    build,
    digests,
    normalize_teardown,
    plan_hash,
)
from zab.admin.approvals import ABOVE_HALF, CHANGE, DESTRUCTIVE, requirement
from zab.admin.fleet import ApplyEnd, Ledger
from zab.admin.grammar import Command, Context, Rejected, Roster, parse
from zab.admin.planning import Planning, Prepared
from zab.admin.plans import APPROVED, PENDING, REFUSED, REJECTED, Requirement

from .botkit import DREW, HARVARD, OLGA, OSCAR, incoming, make_site
from .conftest import CONFIG, FLEET, PRODUCTION
from .conftest import HARVARD as HARVARD_WS

LATE = 10**6


# ------------------------------------------------------------------------- the bot, end to end


class FakeRunner:
    """The runner's side, faked: every dry run succeeds, nothing is applied."""

    def __init__(self) -> None:
        self.applied: list = []

    async def prepare(self, plan, action, fleet):
        tmp = f"/var/lib/zab-admin/tmp/{plan.name}"
        live = Live(vms={"alice": "i-1", "bob": "i-2"}, tmpdir=tmp)
        return Prepared(True, "alice: would terminate i-1\n", live)

    async def read(self, action, fleet):
        return "{}"

    def busy(self, workshop):
        return None

    async def apply(self, job):
        self.applied.append(job)


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
async def server(site, runner):
    srv = site.server(planner=Planning(runner))
    await srv.start()
    yield srv
    await srv.close()


def bot(server):
    return next(b for b in server.bots if b.user_id == HARVARD)


def replies(site):
    return [p["content"] for p in site.realm.posts(HARVARD)]


async def settle(server):
    while server.planner.tasks:
        await asyncio.gather(*list(server.planner.tasks), return_exceptions=True)


async def request(server, text, sender=OLGA, mid=5000):
    await bot(server).on_message(incoming(sender, text, mid=mid))
    await settle(server)
    ids = [row[0] for row in server.store.db.execute("SELECT id FROM plans")]
    return server.store.get(max(ids))


async def say(server, sender, text, mid):
    await bot(server).on_message(incoming(sender, text, mid=mid))
    await settle(server)


async def applied(server, verb, *slugs, exit=0, ended=True):
    """A runner's `apply_start` and `apply_end` for the workshop's rows, their targets named;
    `ended=False`: the bot died, or the end's write failed, after the start."""
    fields = {"workshop": "harvard-2026", "plan": "P-1", "verb": verb, "args": list(slugs)}
    await server.audit("apply_start", **fields, **ApplyEnd.start_fields(slugs))
    if ended:
        end = ApplyEnd("harvard-2026", "P-1", verb, slugs, exit, slugs)
        await server.audit("apply_end", **end.fields())


async def test_teardown_needs_a_sync_after_the_uninstall(site, server, runner):
    """The review of #367: a sync counts only after the row's `!uninstall`, and any plan that
    names the row since forgets it, from its `apply_start`."""
    refused = "no !final-sync after its !uninstall on record for"
    plan = await request(server, "!teardown all")
    assert plan.state == REFUSED and f"{refused} alice, bob, carol:" in replies(site)[-1]
    await applied(server, "final-sync", "alice", "bob")  # while the agents ran: doesn't count
    plan = await request(server, "!teardown alice bob", mid=5001)
    assert plan.state == REFUSED and f"{refused} alice, bob:" in replies(site)[-1]
    await applied(server, "uninstall", "alice", "bob")
    await applied(server, "final-sync", "alice")
    plan = await request(server, "!teardown alice bob", mid=5002)
    assert plan.state == REFUSED and f"{refused} bob:" in replies(site)[-1]
    await applied(server, "final-sync", "bob")
    plan = await request(server, "!teardown all", mid=5003)  # carol has no ip: can't sync
    assert plan.state == REFUSED and f"{refused} carol:" in replies(site)[-1]
    plan = await request(server, "!teardown alice bob", mid=5006)
    assert plan.state == PENDING
    assert plan.requirement == Requirement(DESTRUCTIVE, 2, ("teardown", "2"))
    shown = replies(site)[-2]  # the plan message, then the approval message
    assert "**Participant rows** (2), 0 unsynced:" in shown
    assert "- alice: synced P-1 at " in shown and ", after its uninstall P-1 at " in shown
    # a !start that the bot never saw end (killed, or its apply_end not written): forgotten
    await applied(server, "start", "alice", ended=False)
    plan = await request(server, "!teardown alice bob", mid=5004)
    assert plan.state == REFUSED and f"{refused} alice:" in replies(site)[-1]
    await applied(server, "final-sync", "alice")  # the bridge is still uninstalled: counts
    assert (await request(server, "!teardown alice bob", mid=5005)).state == PENDING


async def test_the_plan_lists_the_unsynced_rows(site, server):
    await applied(server, "uninstall", "bob")
    plan = await request(server, "!teardown all unsynced")
    assert plan.state == PENDING
    shown = replies(site)[-2]
    assert "**Participant rows** (3), 3 unsynced:" in shown
    assert "- carol: no VM in the roster (one with its tag goes too), not synced: unsynced" in shown
    assert "- alice: not uninstalled, not synced: unsynced" in shown
    assert "- bob: uninstalled P-1 at " in shown and ", not synced since: unsynced" in shown


async def test_teardown_all_unsynced_takes_two_approvers_and_the_phrase(site, server, runner):
    """ADMIN §2.5: `destructive_approvals` (2) distinct approvers, each with `teardown <count>
    unsynced`; the requester may be one of them; the same person twice counts once."""
    plan = await request(server, "!teardown all unsynced")
    assert plan.state == PENDING
    assert plan.requirement == Requirement(DESTRUCTIVE, 2, ("teardown", "3", "unsynced"))
    assert "widget_content" not in site.realm.posts(HARVARD)[-1]  # the phrase is typed (#542)
    approve = f"!approve {plan.name} {plan.hash[:6]}"
    assert f"\n\n```text\n{approve} teardown 3 unsynced\n```\n\n" in replies(site)[-1]
    assert (
        "It takes 2 distinct approvers, or a global admin alone: each one sends that line."
        in replies(site)[-1]
    )
    await say(server, OLGA, approve, LATE)  # without the phrase
    assert "needs the phrase `teardown 3 unsynced`" in replies(site)[-1]
    await say(server, OLGA, f"{approve} teardown 3", LATE + 1)  # without `unsynced`
    assert "needs the phrase `teardown 3 unsynced`" in replies(site)[-1]
    assert server.approvals.approved_by(plan.id) == ()
    await say(server, OLGA, f"{approve} teardown 3 unsynced", LATE + 2)
    assert "1 of 2 approvals, from distinct approvers" in replies(site)[-1]
    await say(server, OLGA, f"{approve} teardown 3 unsynced", LATE + 3)
    assert "you approved" in replies(site)[-1]
    assert server.approvals.approved_by(plan.id) == (OLGA,)
    assert server.store.get(plan.id).state == PENDING and runner.applied == []
    await say(server, OSCAR, f"{approve} teardown 3 unsynced", LATE + 4)
    assert server.store.get(plan.id).state == APPROVED
    assert server.approvals.approved_by(plan.id) == (OLGA, OSCAR)
    assert replies(site)[-1].startswith(f"✅ {plan.name} approved (2 distinct approvers)")
    (job,) = runner.applied
    assert job.action.targets == ("alice", "bob", "carol")
    assert job.action.steps[0].argv[-1] == "--yes"


async def test_a_global_admins_approval_alone_applies_a_teardown(site, server, runner):
    """#558 (Drew, Oct 22): a global admin's approval completes a destructive plan, the phrase
    still typed; an organizer's alone is one of two."""
    plan = await request(server, "!teardown all unsynced")
    approve = f"!approve {plan.name} {plan.hash[:6]}"
    await say(server, DREW, f"{approve} teardown 3", LATE)  # the phrase, still
    assert "needs the phrase `teardown 3 unsynced`" in replies(site)[-1]
    assert server.store.get(plan.id).state == PENDING and runner.applied == []
    await say(server, DREW, f"{approve} teardown 3 unsynced", LATE + 1)
    assert replies(site)[-1].startswith(f"✅ {plan.name} approved (a global admin's approval)")
    assert server.approvals.approved_by(plan.id) == (DREW,)
    (job,) = runner.applied
    assert job.action.targets == ("alice", "bob", "carol")


async def test_a_veto_stops_a_destructive_plan(site, server, runner):
    plan = await request(server, "!deactivate-bots all")
    assert plan.requirement == Requirement(DESTRUCTIVE, 2, ("deactivate-bots", "3"))
    approve = f"!approve {plan.name} {plan.hash[:6]} deactivate-bots 3"
    await say(server, OLGA, approve, LATE)
    await say(server, OSCAR, f"!reject {plan.name}", LATE + 1)  # after one approval
    assert server.store.get(plan.id).state == REJECTED
    await say(server, DREW, approve, LATE + 2)
    assert server.store.get(plan.id).state == REJECTED and runner.applied == []
    assert f"{plan.name} is rejected, not pending" in replies(site)[-1]


async def test_the_other_ending_verbs_classes(site, server):
    one = await request(server, "!final-sync all")
    assert (one.requirement, one.targets) == (Requirement(CHANGE), ("alice", "bob"))
    two = await request(server, "!uninstall all", mid=5001)
    assert two.requirement == Requirement(ABOVE_HALF, 1, ("uninstall", "2"))
    three = await request(server, "!uninstall alice", mid=5002)
    assert three.requirement == Requirement(CHANGE)  # half is not more than half
    four = await request(server, "!export all", mid=5003)
    assert four.requirement == Requirement(CHANGE)


# ---------------------------------------------------------------------------------- the ledger


def end(verb, targets, exit=0, ws="harvard-2026", args=None, ts="2026-11-02T14:05:09Z"):
    record = ApplyEnd(ws, "P-3", verb, tuple(args or targets), exit, tuple(targets)).fields()
    return {"event": "apply_end", "ts": ts, **record}


def start(verb, targets, ws="harvard-2026"):
    fields = {"workshop": ws, "plan": "P-4", "verb": verb, "args": list(targets)}
    return {"event": "apply_start", **fields, **ApplyEnd.start_fields(tuple(targets))}


def counting(ledger, ws="harvard-2026"):
    """The rows whose sync counts for `!teardown`."""
    state = ledger.workshop(ws)
    return {row for row, e in state.ending().items() if e.counts} if state else set()


def test_a_sync_counts_only_after_the_uninstall():
    ledger = Ledger()
    ledger.observe(end("final-sync", ["alice", "bob"], args=["all"]))
    assert counting(ledger) == set()  # the agents still ran: shown, doesn't count
    assert ledger.workshop("harvard-2026").ending()["alice"] == Ending(
        "", "P-3 at 2026-11-02 14:05 UTC", False
    )
    ledger.observe(end("uninstall", ["alice", "bob"]))
    assert counting(ledger) == set()  # a sync before the uninstall never counts
    ledger.observe(end("final-sync", ["alice", "bob"]))
    ledger.observe(end("final-sync", ["carol"], exit=1))  # failed: nothing marked
    ledger.observe(end("final-sync", ["erin"], ws="zab-test"))  # not uninstalled there
    assert counting(ledger) == {"alice", "bob"} and counting(ledger, "zab-test") == set()
    ledger.observe(end("stop", ["alice"]))  # a stop changes nothing on the VM's disk
    ledger.observe(end("uninstall", ["alice"]))  # again: the sync still came after one
    assert counting(ledger) == {"alice", "bob"}
    assert ledger.workshop("harvard-2026").recorded() is False  # !adopt-spare's record apart


@pytest.mark.parametrize("verb", ["start", "rollout", "launch", "adopt-spare", "final-sync"])
def test_any_plan_naming_the_row_forgets_its_sync_from_its_start(verb):
    """The review of #367: at `apply_start`, before anything runs, so that an apply whose end
    is never written (a killed bot, a failed write) has forgotten it already."""
    ledger = Ledger()
    for record in (end("uninstall", ["alice", "bob"]), end("final-sync", ["alice", "bob"])):
        ledger.observe(record)
    ledger.observe(start(verb, ["alice"]))
    assert counting(ledger) == {"bob"}
    ending = ledger.workshop("harvard-2026").ending()
    # a !start leaves the bridge uninstalled; the others bring it back, or sync again
    assert bool(ending.get("alice", Ending()).uninstalled) == (verb in ("start", "final-sync"))
    ledger.observe(end("final-sync", ["alice"]))
    assert counting(ledger) == ({"alice", "bob"} if verb in ("start", "final-sync") else {"bob"})


def test_a_failed_final_sync_unmarks_its_rows():
    """Codex round 1 on #367: the last sync of a row decides."""
    ledger = Ledger()
    for record in (end("uninstall", ["alice", "bob"]), end("final-sync", ["alice", "bob"])):
        ledger.observe(record)
    ledger.observe(end("final-sync", ["alice"], exit=1))
    assert counting(ledger) == {"bob"}
    ledger.observe(end("final-sync", ["bob"], exit=-1))  # interrupted, or timed out
    assert counting(ledger) == set()
    ledger.observe(end("final-sync", ["alice", "bob"]))
    assert counting(ledger) == {"alice", "bob"}


@pytest.mark.parametrize("event", ["apply_start", "apply_end"])
@pytest.mark.parametrize("verb", ["start", "rollout", "final-sync"])
def test_a_record_without_targets_forgets_every_row(event, verb):
    """A runner older or newer than this code, that doesn't name its targets: fail closed."""
    ledger = Ledger()
    for record in (end("uninstall", ["alice", "bob"]), end("final-sync", ["alice", "bob"])):
        ledger.observe(record)
    record = start(verb, ["alice"]) if event == "apply_start" else end(verb, ["alice"])
    del record["targets"]
    ledger.observe(record)
    assert counting(ledger) == set()
    ending = ledger.workshop("harvard-2026").ending()
    assert all(bool(e.uninstalled) == (verb != "rollout") for e in ending.values())


def test_a_final_sync_without_targets_marks_nothing():
    ledger = Ledger()
    ledger.observe(end("uninstall", ["alice"]))
    record = end("final-sync", ["alice"])
    del record["targets"]
    ledger.observe(record)
    assert counting(ledger) == set()
    assert ApplyEnd.read(record) == ApplyEnd("harvard-2026", "P-3", "final-sync", ("alice",), 0)
    assert ApplyEnd.read(record).targets is None
    for bad in ("alice", [1], None):
        assert ApplyEnd.read({**end("final-sync", ["alice"]), "targets": bad}) is None


def test_read_fleet_gives_each_rows_end(tmp_path):
    srv = make_site(tmp_path).server()
    ws = srv.config.workshop("harvard-2026")
    ledger = Ledger()
    assert fleet.read_fleet(srv.config, ws, ledger).ending == {}
    ledger.observe(end("uninstall", ["bob"]))
    ledger.observe(end("final-sync", ["bob"]))
    got = fleet.read_fleet(srv.config, ws, ledger)
    when = "P-3 at 2026-11-02 14:05 UTC"
    assert got.ending == {"bob": Ending(when, when, True, when)} and got.settings is None
    assert fleet.read_fleet(srv.config, srv.config.workshop("zab-test"), ledger).ending == {}


# ------------------------------------------------------------------------- the actions


def make(message, the_fleet=FLEET):
    roster = Roster(the_fleet.slugs, the_fleet.spares)
    command = parse(message, Context(901, "Workshop admin", PRODUCTION, roster))
    assert isinstance(command, Command), command
    return build(command, the_fleet, layout=Layout())


UNINSTALLED = "P-1 at 2026-11-02 13:00 UTC"
DONE = Ending(UNINSTALLED, "P-2 at 2026-11-02 13:30 UTC", True, UNINSTALLED)


def test_teardown_names_the_unsynced_rows():
    """conftest's FLEET: alice and bob have a VM, carol and dave don't."""
    refused = r"no !final-sync after its !uninstall on record for "
    with pytest.raises(ActionError, match=refused + r"alice, bob, carol, dave: run"):
        make("!teardown all")
    with pytest.raises(ActionError, match=refused + "bob, carol:"):  # carol: no ip (Codex)
        make("!teardown bob carol")
    assert make("!teardown spares").targets == FLEET.spares
    assert make("!teardown spares").notes == () and make("!teardown spares").ends == ()
    early = Ending("", "P-2 at 2026-11-02 13:30 UTC", False)  # a sync while the agents ran
    with pytest.raises(ActionError, match=refused + "alice:"):
        make("!teardown alice", dataclasses.replace(FLEET, ending={"alice": early}))
    both = dataclasses.replace(FLEET, ending={"alice": DONE, "bob": DONE})
    action = make("!teardown alice bob", both)
    assert action.targets == ("alice", "bob")
    assert action.notes == (
        "**Participant rows** (2), 0 unsynced:",
        f"- alice: synced {DONE.synced}, after its uninstall {UNINSTALLED}",
        f"- bob: synced {DONE.synced}, after its uninstall {UNINSTALLED}",
    )
    many = dataclasses.replace(
        FLEET, rows=tuple(Row(f"p{i}", f"198.51.100.{i}") for i in range(8)), spares=()
    )
    with pytest.raises(ActionError, match=refused + r"p0, p1, p2, p3, p4 and 3 more:"):
        make("!teardown all", many)
    assert make("!teardown all unsynced", many).targets == many.slugs


def test_each_rows_state_reads_plainly():
    down = Ending("P-1 at 13:00", "", False)
    early = Ending("", "P-2 at 13:30", False)
    both = Ending("P-3 at 14:00", "P-2 at 13:30", False)  # synced, then uninstalled
    again = Ending("P-9 at 15:00", "P-5 at 14:00", True, "P-4 at 13:50")  # re-uninstalled
    assert [e.line("x") for e in (Ending(), down, early, both, DONE, again)] == [
        "x: not uninstalled, not synced: unsynced",
        "x: uninstalled P-1 at 13:00, not synced since: unsynced",
        "x: synced P-2 at 13:30 while its agents ran, not uninstalled: unsynced",
        "x: synced P-2 at 13:30 before its uninstall P-3 at 14:00: unsynced",
        "x: synced P-2 at 2026-11-02 13:30 UTC, after its uninstall P-1 at 2026-11-02 13:00 UTC",
        "x: synced P-5 at 14:00, after its uninstall P-4 at 13:50",  # times in order
    ]
    # what a STALE teardown names as changed (#376): the same words, without the times
    assert [Ending.said(e.flags()) for e in (Ending(), down, early, both, DONE, again)] == [
        "not uninstalled, not synced",
        "uninstalled, not synced since",
        "synced while its agents ran, not uninstalled",
        "synced before its uninstall",
        "synced after its uninstall",
        "synced after its uninstall",
    ]
    assert Ending.said(None) == "not in the plan"


def test_each_rows_end_is_an_ending():
    for bad in ({"alice": "synced"}, {1: DONE}, (("alice", DONE),)):
        with pytest.raises(ActionError, match="each row's end is an Ending"):
            dataclasses.replace(FLEET, ending=bad)


def test_the_unsynced_phrase_only_where_it_was_said():
    sync = dataclasses.replace(FLEET, ending={"alice": DONE, "bob": DONE})
    for message, phrase in [
        ("!teardown alice bob", ("teardown", "2")),
        ("!teardown all unsynced", ("teardown", "4", "unsynced")),
        ("!teardown unsynced alice", ("teardown", "1", "unsynced")),  # the words in any order
        ("!teardown spares", ("teardown", "2")),
        ("!deactivate-bots alice", ("deactivate-bots", "1")),
    ]:
        assert requirement(make(message, sync), sync, CONFIG).phrase == phrase
        tokens = parse(f"!approve P-1 abcdef {' '.join(phrase)}", Context(
            901, "Workshop admin", PRODUCTION, Roster(sync.slugs, sync.spares)
        ))  # fmt: skip
        assert isinstance(tokens, Command) and tokens.phrase == phrase


def test_each_verb_is_hashed_as_section_2_4_says(tmp_path):
    assert make("!final-sync all").hashed == RECONCILER
    assert make("!export all").hashed == RECONCILER
    assert make("!uninstall all").hashed == DRY_RUN
    assert make("!teardown spares").hashed == DRY_RUN  # Codex round 1 on #367
    assert make("!deactivate-bots all").hashed == INPUTS


def test_deactivate_bots_hashes_its_rows_bot_keys(tmp_path):
    """The bots' identities from out/ (§2.4): a key made or changed between plan and apply
    makes the plan stale. Only the named rows' files."""
    out = tmp_path / "out"
    ws = dataclasses.replace(HARVARD_WS, out_dir=str(out))
    here = dataclasses.replace(FLEET, workshop=ws)
    action = make("!deactivate-bots bob", here)
    assert action.inputs == (
        HARVARD_WS.roster, f"{out}/bob/claude.zuliprc", f"{out}/bob/codex.zuliprc",
    )  # fmt: skip
    live = Live()

    def hashed() -> str:
        return plan_hash(action, release="v0.3.0+abc", digests=digests(action), live=live)

    before = hashed()
    (out / "bob").mkdir(parents=True)
    (out / "bob" / "codex.zuliprc").write_text("[api]\nkey=" + "K" * 32 + "\n")
    assert hashed() != before
    after = hashed()
    (out / "alice").mkdir()
    (out / "alice" / "codex.zuliprc").write_text("x")  # another row's: not this plan's
    assert hashed() == after


def test_spares_and_keywords_only_where_the_verb_takes_them():
    ctx = Context(901, "Workshop admin", PRODUCTION, Roster(FLEET.slugs, FLEET.spares))
    for message in (
        "!final-sync spares",
        "!uninstall zab-spare-1",
        "!deactivate-bots spares",
        "!export zab-spare-1",
        "!final-sync all unsynced",
        "!teardown unsynced",  # no selection
        "!teardown all unsynced unsynced",
    ):
        assert isinstance(parse(message, ctx), Rejected), message
    with pytest.raises(ValueError, match="not usable in commands"):
        Roster(("unsynced",))  # a reserved word, never a slug


def test_fleet_default_has_no_row_ends():
    assert Fleet(HARVARD_WS, (), (), PRODUCTION).ending == {}


def test_final_sync_is_always_strict():
    """The review of #367: a row with a VM that final_sync.py would skip fails the run."""
    (step,) = make("!final-sync all").steps
    assert "--strict" in step.argv and "--strict" in step.dry_run


def test_normalize_teardown_sorts_and_drops_states():
    """Each `--only` is its own EC2 lookup, so a test through teardown.py can't reorder its
    lines: the normalizer itself must (the review of #367, a surviving mutant)."""
    one = (
        "terminate i-2 ws-b (running, participant b)\nterminate i-1 ws-a (stopped, participant a)\n"
    )
    two = (
        "terminate i-1 ws-a (running, participant a)\nterminate i-2 ws-b (stopping, participant b)"
    )
    tmp = "/var/lib/zab-admin/tmp/P-1"
    assert (
        normalize_teardown(one, tmp)
        == normalize_teardown(two, tmp)
        == ("terminate i-1 ws-a (participant a)\nterminate i-2 ws-b (participant b)")
    )
    kept = one.replace("terminate i-1", "keep i-1")
    assert normalize_teardown(kept, tmp) != normalize_teardown(one, tmp)


def test_a_second_uninstall_keeps_the_times_in_order():
    """The review of #367: the counting sync names the uninstall it followed."""
    ledger = Ledger()
    ledger.observe(end("uninstall", ["alice"], ts="2026-11-02T13:50:00Z"))
    ledger.observe(end("final-sync", ["alice"], ts="2026-11-02T14:00:00Z"))
    ledger.observe(end("uninstall", ["alice"], ts="2026-11-02T15:00:00Z"))
    e = ledger.workshop("harvard-2026").ending()["alice"]
    assert e.counts and e.line("alice") == (
        "alice: synced P-3 at 2026-11-02 14:00 UTC, after its uninstall P-3 at 2026-11-02 13:50 UTC"
    )


@pytest.mark.parametrize("exit", [False, 0.0, "0", None])
def test_only_the_int_0_marks_a_row(exit):
    """As `ApplyEnd.read` requires (the review of #367)."""
    ledger = Ledger()
    ledger.observe(end("uninstall", ["alice"]))
    ledger.observe({**end("final-sync", ["alice"]), "exit": exit})
    assert counting(ledger) == set()
    ledger.observe({**end("uninstall", ["bob"]), "exit": exit})
    assert "bob" not in ledger.workshop("harvard-2026").ending()


@pytest.mark.parametrize("plan", [None, 3, ["P-4"]])
def test_a_record_with_a_bad_plan_forgets_every_row(plan):
    ledger = Ledger()
    for record in (end("uninstall", ["alice", "bob"]), end("final-sync", ["alice", "bob"])):
        ledger.observe(record)
    ledger.observe({**start("start", ["alice"]), "plan": plan})
    assert counting(ledger) == set()  # bob's too: fail closed
    ledger.observe({**end("final-sync", ["alice"]), "plan": plan})
    assert counting(ledger) == set()  # and it marks nothing


def test_the_hash_covers_each_rows_state_not_its_times():
    """The review of #367: with `unsynced`, a plan naming a row between plan and apply (its
    sync forgotten, its bridge back) must make the teardown STALE; a restart, whose times may
    read a minute apart, must not."""
    roster = Roster(FLEET.slugs, FLEET.spares)
    command = parse("!teardown all unsynced", Context(901, "Workshop admin", PRODUCTION, roster))

    def hashed(ending) -> str:
        action = build(command, dataclasses.replace(FLEET, ending=ending), layout=Layout())
        live = Live(dry_run="terminate i-1 ws-alice (running, participant alice)", tmpdir="/t")
        return plan_hash(action, release="v0.3.0+abc", digests=digests(action), live=live)

    planned = hashed({"alice": DONE, "bob": DONE})
    minute = UNINSTALLED.replace("13:00", "13:01")
    later = Ending(minute, DONE.synced, True, minute)
    assert hashed({"alice": later, "bob": DONE}) == planned  # the same states, other minutes
    for alice in (Ending(UNINSTALLED), Ending(), Ending("", DONE.synced, False)):
        assert hashed({"alice": alice, "bob": DONE}) != planned, alice
    assert hashed({"bob": DONE}) != planned


def test_a_row_without_an_ip_needs_unsynced():
    """Codex round 2 on #367: teardown.py finds a VM by its participant tag, so a launch cut
    short before the roster recorded its `ip` leaves a VM that `!teardown carol` would take.
    Such a row can't be uninstalled or synced: it needs `unsynced`, and its state is hashed."""
    with pytest.raises(ActionError, match="on record for carol:"):
        make("!teardown carol")
    action = make("!teardown carol unsynced")
    assert action.notes[1] == (
        "- carol: no VM in the roster (one with its tag goes too), not synced: unsynced"
    )
    assert action.ends == (("carol", False, False, False),)
