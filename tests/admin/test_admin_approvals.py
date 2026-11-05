"""Approval rules (docs/ADMIN.md §2.5): who approves, the classes, the phrase, two approvers
and the veto.

Table-driven. `REFUSED` holds the refused approvals; for each guard of `GUARDS` one case,
marked `alone`, fails that guard and no other. The mutation test removes each guard in turn
and requires its case to go through: every guard is load-bearing and tested, and no case is
refused by another guard's accident.
"""

import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from zab.admin import approvals
from zab.admin.actions import Row, build
from zab.admin.approvals import (
    ABOVE_HALF,
    CHANGE,
    DEPLOY,
    DESTRUCTIVE,
    GUARDS,
    REBIND,
    SERVER,
    Approvals,
    Received,
    Sender,
    approvers,
    requirement,
)
from zab.admin.grammar import Command, Context, Roster, parse
from zab.admin.plans import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    STALE,
    PlanError,
    PlanStore,
    Requirement,
)

from .conftest import CONFIG, FLEET, HARVARD, HASH, OTHER, PRODUCTION, TEST, direct, make, posted

H6 = HASH[:6]
LATE = 10**6  # a message ID after every plan's approval message (P-n's is 100n + 1)


def received(user=103, *, bot=901, channel=42, message=LATE, is_bot=False, is_active=True):
    return Received(message, bot, channel, Sender(user, is_bot, is_active))


# ---------------------------------------------------------------------------------- classes

#: (message, (class, distinct approvers, phrase)) against FLEET: 4 rows, of which alice and bob
#: have a VM, and 2 spares. The fleet is the rows with a VM; the count is every target.
CLASSES = [
    ("!stop all", (ABOVE_HALF, 1, ("stop", "4"))),
    ("!stop alice bob carol dave", (ABOVE_HALF, 1, ("stop", "4"))),  # however it is written
    ("!stop dave alice bob", (ABOVE_HALF, 1, ("stop", "3"))),
    ("!stop alice bob", (ABOVE_HALF, 1, ("stop", "2"))),  # every VM there is (review of #339)
    ("!stop alice", (CHANGE, 1, ())),  # half is not more than half
    ("!stop carol dave", (ABOVE_HALF, 1, ("stop", "2"))),  # no VM: the count only adds phrases
    ("!stop all spares", (ABOVE_HALF, 1, ("stop", "6"))),
    ("!stop spares", (ABOVE_HALF, 1, ("stop", "2"))),
    ("!idle-stop 30 bob", (CHANGE, 1, ())),
    ("!rollout v0.2.2 all steer=off", (ABOVE_HALF, 1, ("rollout", "4"))),
    ("!rollout v0.2.2 alice bob carol", (ABOVE_HALF, 1, ("rollout", "3"))),
    ("!rollout v0.2.2 alice", (CHANGE, 1, ())),
    ("!idle-stop 30 all", (ABOVE_HALF, 1, ("idle-stop", "4"))),
    ("!idle-stop 0 bob carol dave", (ABOVE_HALF, 1, ("idle-stop", "3"))),
    ("!start all", (CHANGE, 1, ())),  # §2.5 and §1.5 name !stop, not !start
    ("!launch all spares=2", (CHANGE, 1, ())),
    ("!protect off all", (CHANGE, 1, ())),
    ("!provision zulip all", (CHANGE, 1, ())),
    # stage 3: `unsynced` is said again in the phrase; `all` of !uninstall is the rows with a VM
    ("!teardown carol dave spares unsynced", (DESTRUCTIVE, 2, ("teardown", "4", "unsynced"))),
    ("!teardown all unsynced", (DESTRUCTIVE, 2, ("teardown", "4", "unsynced"))),
    ("!teardown zab-spare-1", (DESTRUCTIVE, 2, ("teardown", "1"))),  # however few
    ("!deactivate-bots bob", (DESTRUCTIVE, 2, ("deactivate-bots", "1"))),
    ("!deactivate-bots all", (DESTRUCTIVE, 2, ("deactivate-bots", "4"))),
    ("!uninstall all", (ABOVE_HALF, 1, ("uninstall", "2"))),
    ("!uninstall alice", (CHANGE, 1, ())),
    ("!final-sync all", (CHANGE, 1, ())),
    ("!export all", (CHANGE, 1, ())),
]


@pytest.mark.parametrize("message, expected", CLASSES)
def test_each_plan_gets_its_class(message, expected, wheels):
    assert requirement(make(message, layout=wheels), FLEET, CONFIG) == Requirement(*expected)


def test_unlaunched_rows_never_dilute_the_fleet():
    """10 rows, 4 with a VM: naming the 4 launched slugs stops every VM, and needs the phrase
    just as `!stop all` does (review of #339)."""
    rows = tuple(Row(f"p{i}", f"198.51.100.{i}" if i < 4 else "") for i in range(10))
    fleet = dataclasses.replace(FLEET, rows=rows, spares=())
    ctx = Context(901, "Workshop admin · harvard-2026", PRODUCTION, Roster(fleet.slugs))
    for message, phrase in [("!stop p0 p1 p2 p3", ("stop", "4")), ("!stop p0 p1", ())]:
        action = build(parse(message, ctx), fleet)
        assert requirement(action, fleet, CONFIG).phrase == phrase


#: Verbs no bot offers yet (the rebind verb; stages 6, 7): (action, fleet, rebinds,
#: requirement).
LATER = [
    (direct("rebind", ("bob",), ("bob",)), FLEET, (), (REBIND, 2, ("rebind", "bob"))),
    (direct("rebind", ("bob",), ("bob",)), FLEET, ("bob",), (REBIND, 2, ("rebind", "bob"))),
    (direct("roster import", ()), FLEET, ("alice",), (REBIND, 2, ("rebind", "alice"))),
    (direct("upgrade-server", ("v0.3.0",), ws=""), None, (), (DEPLOY, 1, ())),
    (
        direct("new-workshop", ("mit-2027", "region=us-east-2"), ws=""),
        None,
        (),
        (SERVER, 2, ("new-workshop", "mit-2027")),
    ),
]


@pytest.mark.parametrize("action, fleet, rebinds, expected", LATER, ids=[a.verb for a, *_ in LATER])
def test_the_later_classes(action, fleet, rebinds, expected):
    assert requirement(action, fleet, CONFIG, rebinds=rebinds) == Requirement(*expected)
    one = dataclasses.replace(CONFIG, destructive_approvals=1)  # the test server's setting
    assert requirement(action, fleet, one, rebinds=rebinds).needed == 1


@pytest.mark.parametrize(
    "action, fleet, rebinds, error",
    [
        (make("!stop alice"), None, (), "its own fleet"),
        (make("!stop alice"), OTHER, (), "its own fleet"),
        (make("!stop erin", OTHER), FLEET, (), "its own fleet"),
        (direct("upgrade-server", ("v0.3.0",)), None, (), "server verb"),
        (direct("new-workshop", ("x",), ws=""), FLEET, (), "server verb"),
        (direct("rebind", ("erin",), ("erin",)), FLEET, (), "a row of this workshop"),
        (direct("roster import", ()), FLEET, ("erin",), "a row of this workshop"),
        (direct("roster import", ()), FLEET, ("alice", "bob"), "one row"),  # one per plan
        (direct("rebind", ("bob",), ("bob",)), FLEET, ("alice",), "one row"),
        (direct("roster import", ()), FLEET, "alice", "a tuple of slugs"),
    ],
)
def test_a_plan_is_classed_against_its_own_fleet(action, fleet, rebinds, error):
    with pytest.raises(PlanError, match=error):
        requirement(action, fleet, CONFIG, rebinds=rebinds)


def test_who_approves_which_plan(store):
    harvard, test = (
        posted(store, make("!stop alice")),
        posted(store, make("!stop erin", OTHER), fleet=OTHER),
    )
    deploy = posted(store, direct("upgrade-server", ("v0.3.0",), ws=""), fleet=None, requester=101)
    server = posted(store, direct("new-workshop", ("mit-2027",), ws=""), fleet=None, requester=101)
    assert approvers(CONFIG, harvard) == {101, 102, 103, 104}  # never 105, zab-test's
    assert approvers(CONFIG, test) == {101, 102, 105}
    assert approvers(CONFIG, deploy) == {101, 106}
    assert approvers(CONFIG, server) == {101, 102}
    forged = [
        dataclasses.replace(harvard, requirement=Requirement(DEPLOY)),
        dataclasses.replace(harvard, requirement=Requirement(SERVER, 2)),
        dataclasses.replace(harvard, requirement=None),
        dataclasses.replace(deploy, requirement=Requirement(CHANGE)),
        dataclasses.replace(harvard, workshop="gone-2025"),
    ]
    assert [approvers(CONFIG, plan) for plan in forged] == [frozenset()] * len(forged)


# --------------------------------------------------------------------------------- refusals

PLANS: dict[str, Callable] = {
    "change": lambda: make("!stop alice"),
    "above-half": lambda: make("!stop all"),
    "destructive": lambda: make("!teardown spares"),  # two spares: no participant's work
}


@dataclass(frozen=True)
class Case:
    id: str
    code: str
    plan: str  # a key of PLANS, requested by 103
    who: dict = field(default_factory=dict)  # received()'s arguments
    hash6: str = H6
    phrase: tuple[str, ...] = ()
    prep: Callable | None = None  # (approvals, plan, clock) before the approval
    alone: bool = False  # fails this guard and no other


TWO = ("teardown", "2")
REFUSED = [
    Case("sent to another workshop's bot", "other-bot", "change", {"bot": 902}, alone=True),
    Case(
        "an admin, through another bot",
        "other-bot",
        "change",
        {"user": 101, "bot": 902, "channel": 43},
    ),
    Case(
        "an admin, through the global bot",
        "other-bot",
        "change",
        {"user": 101, "bot": 900, "channel": 41},
    ),
    Case("in another bot's channel", "channel", "change", {"channel": 43}, alone=True),
    Case("in a DM", "channel", "change", {"channel": 0}),
    Case(
        "a bot on the allowlist", "bot-sender", "change", {"user": 104, "is_bot": True}, alone=True
    ),
    Case(
        "a deactivated organizer",
        "inactive",
        "change",
        {"user": 104, "is_active": False},
        alone=True,
    ),
    Case("a non-organizer", "not-approver", "change", {"user": 999}, alone=True),
    Case("an organizer of another workshop", "not-approver", "change", {"user": 105}),
    Case("a deployer who organizes nothing", "not-approver", "change", {"user": 106}),
    Case("the bot itself", "bot-sender", "change", {"user": 901, "is_bot": True}),
    Case(
        "a rejected plan",
        "not-pending",
        "destructive",
        phrase=TWO,
        prep=lambda a, plan, clock: a.reject(received(104), plan.id),
        alone=True,
    ),
    Case(
        "an expired plan",
        "expired",
        "change",
        prep=lambda a, p, clock: setattr(clock, "now", p.expires),
        alone=True,
    ),
    Case("the approval message's own ID", "early", "change", {"message": 101}, alone=True),
    Case("older than the plan", "early", "change", {"message": 100}),
    Case("much older", "early", "change", {"message": 7}),
    Case("a wrong hash", "hash", "change", hash6="3f9a2d", alone=True),
    Case("another plan's hash", "hash", "change", hash6="000000"),
    Case("an empty hash", "hash", "change", hash6=""),  # exact, never a prefix
    Case("five hex digits", "hash", "change", hash6=H6[:5]),
    Case("the full hash", "hash", "change", hash6=HASH),
    Case("upper case", "hash", "change", hash6=H6.upper()),
    Case("no phrase, above half the fleet", "phrase", "above-half", alone=True),
    Case("the wrong count", "phrase", "above-half", phrase=("stop", "3")),
    Case("the wrong verb", "phrase", "above-half", phrase=("start", "4")),
    Case("a trailing word", "phrase", "above-half", phrase=("stop", "4", "now")),
    Case("half the phrase", "phrase", "above-half", phrase=("stop",)),
    Case("a phrase where none is needed", "phrase", "change", phrase=("stop", "1")),
    Case("no phrase, destructive", "phrase", "destructive"),
    Case(
        "the same approver twice",
        "again",
        "destructive",
        phrase=TWO,
        prep=lambda a, plan, clock: a.approve(received(103, message=LATE - 1), plan.id, H6, TWO),
        alone=True,
    ),
]


def attempt(case: Case, store, clock):
    plan = posted(store, PLANS[case.plan]())
    deck = Approvals(store, CONFIG)
    if case.prep:
        case.prep(deck, plan, clock)
    before = (store.get(plan.id), deck.approved_by(plan.id))
    decision = deck.approve(received(**case.who), plan.id, case.hash6, case.phrase)
    return deck, before, decision


@pytest.mark.parametrize("case", REFUSED, ids=[case.id for case in REFUSED])
def test_refused_approvals_change_nothing(case, store, clock):
    deck, (plan, done), decision = attempt(case, store, clock)
    assert (decision.ok, decision.code) == (False, case.code)
    after = store.get(plan.id)
    assert decision.plan == after
    assert after == (dataclasses.replace(plan, state=EXPIRED) if case.code == "expired" else plan)
    assert deck.approved_by(plan.id) == done


def test_every_guard_has_a_case_that_fails_it_alone():
    codes = [code for code, _, _ in GUARDS]
    assert len(set(codes)) == len(codes)
    assert sorted(case.code for case in REFUSED if case.alone) == sorted(codes)


@pytest.mark.parametrize("code", [code for code, _, _ in GUARDS])
def test_without_its_guard_the_case_goes_through(code, store, clock, monkeypatch):
    """Mutation test: each guard removed in turn lets exactly its own case through."""
    monkeypatch.setattr(approvals, "GUARDS", tuple(g for g in GUARDS if g[0] != code))
    case = next(case for case in REFUSED if case.code == code and case.alone)
    _, _, decision = attempt(case, store, clock)
    assert decision.ok, decision


def by_id(name: str) -> Case:
    return next(case for case in REFUSED if case.id == name)


def test_the_reasons_say_what_to_do(store, clock):
    _, _, decision = attempt(by_id("no phrase, above half the fleet"), store, clock)
    assert decision.reason == "P-1 needs the phrase `stop 4` after the hash"
    _, _, decision = attempt(by_id("a phrase where none is needed"), store, clock)
    assert decision.reason == "P-2 takes no phrase"
    for case in REFUSED:
        _, _, decision = attempt(case, store, clock)
        assert "\n" not in decision.reason and decision.reason


def test_an_unknown_plan(store):
    deck = Approvals(store, CONFIG)
    for decision in (deck.approve(received(), 42, H6), deck.reject(received(), 42)):
        assert (decision.ok, decision.code, decision.plan) == (False, "unknown", None)
        assert decision.reason == "no plan P-42 on this server"


# --------------------------------------------------------------------------------- accepted


@pytest.mark.parametrize(
    "who",
    [
        received(103),  # the requester: a change may be self-approved (§2.5)
        received(104),
        received(101),  # a global admin, through the workshop's bot
        received(102),
    ],
)
def test_a_change_takes_one_approval(store, who):
    plan = posted(store, make("!stop alice"), requester=103)
    decision = Approvals(store, CONFIG).approve(who, plan.id, H6)
    assert (decision.ok, decision.code, decision.plan.state) == (True, "approved", APPROVED)


def test_each_workshop_approves_only_its_own_plans(store):
    """An organizer of zab-test approves zab-test's plan on its bot, never harvard's."""
    deck = Approvals(store, CONFIG)
    test = posted(store, make("!stop erin", OTHER), fleet=OTHER, requester=105)
    harvard = posted(store, make("!stop bob"))
    assert deck.approve(received(103, bot=902, channel=43), test.id, H6).code == "not-approver"
    assert deck.approve(received(105), harvard.id, H6).code == "not-approver"
    assert deck.approve(received(105, bot=902, channel=43), harvard.id, H6).code == "other-bot"
    assert deck.approve(received(105, bot=902, channel=43), test.id, H6).code == "phrase"
    on_test = received(105, bot=902, channel=43)  # erin is all of zab-test's one row
    assert deck.approve(on_test, test.id, H6, ("stop", "1")).code == "approved"


@pytest.mark.parametrize("message", ["!stop all", "!stop dave carol bob alice"])
def test_above_half_the_fleet_takes_the_phrase_however_written(store, message):
    plan = posted(store, make(message))
    deck = Approvals(store, CONFIG)
    assert deck.approve(received(), plan.id, H6).code == "phrase"
    assert deck.approve(received(), plan.id, H6, ("stop", "4")).code == "approved"


def test_two_distinct_approvers_and_the_phrase(store):
    plan = posted(store, direct("teardown", ("all",), ("alice", "bob", "carol")), requester=103)
    deck, phrase = Approvals(store, CONFIG), ("teardown", "3")
    first = deck.approve(received(103, message=LATE), plan.id, H6, phrase)  # the requester
    assert (first.ok, first.code, first.plan.state) == (True, "recorded", PENDING)
    assert first.reason == "P-1: 1 of 2 approvals, from distinct approvers"
    assert deck.approve(received(103, message=LATE + 1), plan.id, H6, phrase).code == "again"
    assert deck.approve(received(104, message=LATE + 2), plan.id, H6).code == "phrase"
    second = deck.approve(received(104, message=LATE + 3), plan.id, H6, phrase)
    assert (second.code, second.plan.state) == ("approved", APPROVED)
    assert deck.approved_by(plan.id) == (103, 104)


def test_a_rebind_takes_two_approvers_and_its_slug(store):
    upload = ("/var/lib/zab-admin/runs/P-1/roster-upload.csv",)  # the first plan's
    plan = posted(store, direct("roster import", (), inputs=upload), rebinds=("alice",))
    deck = Approvals(store, CONFIG)
    assert deck.approve(received(101), plan.id, H6, ("rebind", "bob")).code == "phrase"
    assert deck.approve(received(103), plan.id, H6, ("rebind", "alice")).code == "recorded"
    assert (
        deck.approve(received(104, message=LATE + 1), plan.id, H6, ("rebind", "alice")).code
        == "approved"
    )


# ------------------------------------------------------------ a global admin alone (#558)

#: The plans of several approvers: destructive, and rebind (the class's rule, ahead of its verb).
SEVERAL = {
    "teardown": (lambda: direct("teardown", ("bob",), ("bob",)), ("teardown", "1")),
    "deactivate-bots": (lambda: make("!deactivate-bots bob"), ("deactivate-bots", "1")),
    "rebind": (lambda: direct("rebind", ("bob",), ("bob",)), ("rebind", "bob")),
}


@pytest.mark.parametrize("name", SEVERAL)
def test_an_organizer_alone_gets_one_of_two(store, name):
    build_it, phrase = SEVERAL[name]
    plan = posted(store, build_it(), requester=103)
    decision = Approvals(store, CONFIG).approve(received(103), plan.id, H6, phrase)
    assert (decision.code, decision.plan.state) == ("recorded", PENDING)
    assert decision.reason == f"{plan.name}: 1 of 2 approvals, from distinct approvers"


@pytest.mark.parametrize("name", SEVERAL)
@pytest.mark.parametrize("admin", [101, 102])
def test_a_global_admin_alone_completes_it(store, name, admin):
    """Drew, Oct 22: a global admin's approval alone completes a plan of several approvers,
    the requester's own included (a global admin may request it)."""
    build_it, phrase = SEVERAL[name]
    plan = posted(store, build_it(), requester=admin)
    deck = Approvals(store, CONFIG)
    decision = deck.approve(received(admin), plan.id, H6, phrase)
    assert (decision.ok, decision.code, decision.plan.state) == (True, "approved", APPROVED)
    assert decision.reason == f"{plan.name} approved (a global admin's approval)"
    assert deck.approved_by(plan.id) == (admin,)


def test_an_organizer_then_a_global_admin_completes_it(store):
    plan = posted(store, make("!deactivate-bots bob"), requester=103)
    deck, phrase = Approvals(store, CONFIG), ("deactivate-bots", "1")
    assert deck.approve(received(103), plan.id, H6, phrase).code == "recorded"
    decision = deck.approve(received(101, message=LATE + 1), plan.id, H6, phrase)
    assert (decision.code, decision.plan.state) == ("approved", APPROVED)
    assert decision.reason == f"{plan.name} approved (2 distinct approvers)"
    assert deck.approved_by(plan.id) == (103, 101)


def test_a_global_admin_completes_it_where_the_count_would_not(store):
    """With `destructive_approvals = 3`, an organizer's approval, then a global admin's: the
    admin's completes it, which the count would not."""
    three = dataclasses.replace(CONFIG, destructive_approvals=3)
    plan = posted(store, make("!deactivate-bots bob"), config=three)
    deck, phrase = Approvals(store, three), ("deactivate-bots", "1")
    first = deck.approve(received(103), plan.id, H6, phrase)
    assert first.reason == f"{plan.name}: 1 of 3 approvals, from distinct approvers"
    decision = deck.approve(received(102, message=LATE + 1), plan.id, H6, phrase)
    assert decision.reason == f"{plan.name} approved (a global admin's approval)"


def test_a_global_admin_who_organizes_the_workshop_completes_it(store):
    """A global admin who is also one of the workshop's organizers is a global admin."""
    harvard = dataclasses.replace(HARVARD, organizers=HARVARD.organizers | {101})
    both = dataclasses.replace(CONFIG, workshops=(harvard, TEST))
    plan = posted(store, make("!deactivate-bots bob"), config=both)
    decision = Approvals(store, both).approve(received(101), plan.id, H6, ("deactivate-bots", "1"))
    assert (decision.code, decision.reason) == (
        "approved", f"{plan.name} approved (a global admin's approval)"
    )  # fmt: skip


@pytest.mark.parametrize("phrase", [(), ("deactivate-bots", "2"), ("teardown", "1")])
def test_a_global_admin_still_types_the_phrase(store, phrase):
    plan = posted(store, make("!deactivate-bots bob"), requester=101)
    deck = Approvals(store, CONFIG)
    decision = deck.approve(received(101), plan.id, H6, phrase)
    assert (decision.ok, decision.code) == (False, "phrase")
    assert (store.get(plan.id).state, deck.approved_by(plan.id)) == (PENDING, ())


def test_every_other_guard_holds_for_a_global_admin(store, clock):
    """The rule completes a plan; it never lets through an approval that a guard refuses."""
    deck, phrase = Approvals(store, CONFIG), ("deactivate-bots", "1")
    plan = posted(store, make("!deactivate-bots bob"), requester=101)
    refused = {
        "other-bot": received(101, bot=902, channel=43),
        "channel": received(101, channel=0),
        "inactive": received(101, is_active=False),
        "bot-sender": received(101, is_bot=True),
        "early": received(101, message=101),
    }
    for code, who in refused.items():
        assert deck.approve(who, plan.id, H6, phrase).code == code
    assert deck.approve(received(101), plan.id, "000000", phrase).code == "hash"
    clock.now = plan.expires
    assert deck.approve(received(101), plan.id, H6, phrase).code == "expired"
    assert deck.approved_by(plan.id) == ()


def test_a_completed_plan_takes_no_more_approvals_from_a_global_admin(store):
    """`not-pending`, after a global admin's approval completed it: from the same admin, and
    from the other. A global admin's own approval completes the plan, so their second one
    meets `not-pending` first, which comes before `again` in `GUARDS`. `again` does meet a
    global admin who approved while still an organizer, and was made a global admin since (an
    admin.toml installed and the bots restarted, within the TTL): their first approval counts
    once, as an organizer's, and the plan waits for another approver (review round 2 of #562)."""
    deck, phrase = Approvals(store, CONFIG), ("deactivate-bots", "1")
    plan = posted(store, make("!deactivate-bots bob"), requester=103)
    assert deck.approve(received(101), plan.id, H6, phrase).code == "approved"
    for admin in (101, 102):
        decision = deck.approve(received(admin, message=LATE + admin), plan.id, H6, phrase)
        assert (decision.code, decision.plan.state) == ("not-pending", APPROVED)
    assert deck.approved_by(plan.id) == (101,)
    codes = [code for code, _, _ in GUARDS]
    assert codes.index("not-pending") < codes.index("again")
    other = posted(store, make("!deactivate-bots alice"), requester=103)
    assert deck.approve(received(103), other.id, H6, phrase).code == "recorded"  # an organizer
    promoted = Approvals(store, dataclasses.replace(CONFIG, admins=CONFIG.admins | {103}))
    again = promoted.approve(received(103, message=LATE + 1), other.id, H6, phrase)
    assert (again.code, again.plan.state) == ("again", PENDING)


def test_a_global_admin_removed_from_admin_toml_no_longer_completes_it(store):
    """The rule reads admin.toml as it is at approval time: a former global admin who is not an
    organizer is no approver; one who still organizes the workshop is an organizer, `1 of 2`."""
    plan = posted(store, make("!deactivate-bots bob"), requester=103)
    gone = dataclasses.replace(CONFIG, admins=frozenset({102}))
    decision = Approvals(store, gone).approve(received(101), plan.id, H6, ("deactivate-bots", "1"))
    assert decision.code == "not-approver"
    harvard = dataclasses.replace(HARVARD, organizers=HARVARD.organizers | {101})
    organizes = dataclasses.replace(gone, workshops=(harvard, TEST))
    deck = Approvals(store, organizes)
    decision = deck.approve(received(101), plan.id, H6, ("deactivate-bots", "1"))
    assert (decision.code, decision.plan.state) == ("recorded", PENDING)
    assert decision.reason == f"{plan.name}: 1 of 2 approvals, from distinct approvers"


def test_one_approval_plans_say_nothing_more(store):
    plan = posted(store, make("!stop alice"))
    decision = Approvals(store, CONFIG).approve(received(101), plan.id, H6)
    assert decision.reason == f"{plan.name} approved"


def test_one_approver_where_admin_toml_asks_for_one(store):
    """The test server's `destructive_approvals = 1` (§7)."""
    one = dataclasses.replace(CONFIG, destructive_approvals=1)
    plan = posted(store, direct("teardown", ("bob",), ("bob",)), config=one)
    assert plan.requirement == Requirement(DESTRUCTIVE, 1, ("teardown", "1"))
    assert (
        Approvals(store, one).approve(received(), plan.id, H6, ("teardown", "1")).code == "approved"
    )


def test_the_deploy_and_server_classes_are_the_global_bots(store):
    deploy = posted(store, direct("upgrade-server", ("v0.3.0",), ws=""), fleet=None, requester=101)
    server = posted(store, direct("new-workshop", ("mit-2027",), ws=""), fleet=None, requester=101)
    deck, phrase = Approvals(store, CONFIG), ("new-workshop", "mit-2027")
    on_global = {"bot": 900, "channel": 41}
    assert deck.approve(received(103, **on_global), deploy.id, H6).code == "not-approver"
    assert deck.approve(received(102, **on_global), deploy.id, H6).code == "not-approver"
    assert deck.approve(received(106), deploy.id, H6).code == "other-bot"
    assert deck.approve(received(106, **on_global), deploy.id, H6).code == "approved"
    assert deck.approve(received(106, **on_global), server.id, H6, phrase).code == "not-approver"
    alone = deck.approve(received(101, **on_global), server.id, H6, phrase)  # #558
    assert (alone.code, alone.reason) == ("approved", "P-2 approved (a global admin's approval)")


@pytest.mark.parametrize("many", [1, 2])
def test_a_server_plan_is_one_global_admins_at_any_setting(store, many):
    """ADMIN §2.5 (#558): a server plan takes one global admin, and says so, whatever
    `destructive_approvals` is: 2 in production, 1 on the rig."""
    config = dataclasses.replace(CONFIG, destructive_approvals=many)
    action = direct("new-workshop", ("mit-2027",), ws="")
    plan = posted(store, action, fleet=None, requester=101, config=config)
    assert approvals.takes(plan.requirement) == "1 global admin"
    phrase = ("new-workshop", "mit-2027")
    decision = Approvals(store, config).approve(received(102, bot=900, channel=41), plan.id, H6,
                                                phrase)  # fmt: skip
    assert decision.reason == f"{plan.name} approved (a global admin's approval)"


def test_a_plan_stays_bound_to_the_bot_that_posted_it(store):
    """If admin.toml gives the workshop another bot, the plan's approvals are refused."""
    plan = posted(store, make("!stop alice"))
    bot = dataclasses.replace(HARVARD.bot, user_id=911)
    moved = dataclasses.replace(CONFIG, workshops=(dataclasses.replace(HARVARD, bot=bot), TEST))
    deck = Approvals(store, moved)
    assert deck.approve(received(103, bot=901), plan.id, H6).code == "channel"
    assert deck.approve(received(103, bot=911), plan.id, H6).code == "other-bot"
    assert deck.reject(received(103, bot=901), plan.id).code == "channel"


def test_the_last_approval_and_its_move_commit_together(store, tmp_path, monkeypatch):
    """A crash between recording the last approval and moving the plan to APPROVED keeps
    neither, so after a restart the same approver can send it again (Codex on #339)."""
    plan = posted(store, make("!stop alice"))
    deck, real, calls = Approvals(store, CONFIG), Approvals.approved_by, []

    def dies_after_the_insert(self, plan_id):
        calls.append(plan_id)
        if len(calls) == 2:  # the count, inside the transaction, after the insert
            raise RuntimeError("the process dies here")
        return real(self, plan_id)

    monkeypatch.setattr(Approvals, "approved_by", dies_after_the_insert)
    with pytest.raises(RuntimeError, match="dies here"):
        deck.approve(received(103), plan.id, H6)
    monkeypatch.undo()
    store.close()
    restarted = PlanStore(tmp_path / "state.db", ttl_minutes=10, floor=0, clock=store.clock)
    deck = Approvals(restarted, CONFIG)
    assert (restarted.get(plan.id).state, deck.approved_by(plan.id)) == (PENDING, ())
    assert deck.approve(received(103), plan.id, H6).code == "approved"
    restarted.close()


def test_a_recorded_approval_rolls_back_when_the_move_fails(store, monkeypatch):
    """The plan changed between the read and the write: the approval isn't kept either."""
    plan = posted(store, make("!stop alice"))
    deck, real = Approvals(store, CONFIG), store.record

    def raced(plan_id, write):
        store.move(plan_id, (PENDING,), REJECTED, "a veto in between")
        return real(plan_id, write)

    monkeypatch.setattr(store, "record", raced)
    with pytest.raises(PlanError, match="changed meanwhile"):
        deck.approve(received(103), plan.id, H6)
    assert (store.get(plan.id).state, deck.approved_by(plan.id)) == (REJECTED, ())


def test_an_approved_plan_that_went_stale_is_never_applied(store):
    plan = posted(store, make("!stop alice"))
    deck = Approvals(store, CONFIG)
    assert deck.approve(received(), plan.id, H6).code == "approved"
    assert store.begin_apply(plan.id, "b" * 64).state == STALE
    assert deck.approve(received(104, message=LATE + 1), plan.id, H6).code == "not-pending"


# ------------------------------------------------------------------------------------- veto


def test_the_veto(store):
    plan = posted(store, make("!stop alice"), requester=103)
    deck = Approvals(store, CONFIG)
    refused = [
        (received(999), "not-approver"),
        (received(105), "not-approver"),  # zab-test's organizer
        (received(106), "not-approver"),
        (received(104, bot=902), "other-bot"),
        (received(104, channel=43), "channel"),
        (received(104, is_bot=True), "bot-sender"),
        (received(104, is_active=False), "inactive"),
    ]
    for who, code in refused:
        decision = deck.reject(who, plan.id)
        assert (decision.ok, decision.code, decision.plan.state) == (False, code, PENDING)
    decision = deck.reject(received(104), plan.id)
    assert (decision.ok, decision.code, decision.plan.state) == (True, "rejected", REJECTED)
    assert decision.plan.note == "rejected by user 104"
    assert decision.reason == "P-1 rejected: nothing was done"
    assert deck.approve(received(103, message=LATE + 1), plan.id, H6).code == "not-pending"
    assert deck.reject(received(101), plan.id).code == "not-pending"


@pytest.mark.parametrize(
    "vetoer", [104, 103, 101]
)  # another organizer, the first approver, an admin
def test_the_veto_after_one_of_two_approvals(store, vetoer):
    plan = posted(store, direct("teardown", ("bob",), ("bob",)))
    deck, phrase = Approvals(store, CONFIG), ("teardown", "1")
    assert deck.approve(received(103), plan.id, H6, phrase).code == "recorded"
    assert deck.reject(received(vetoer, message=LATE + 1), plan.id).code == "rejected"
    assert deck.approve(received(102, message=LATE + 2), plan.id, H6, phrase).code == "not-pending"


def test_the_veto_takes_no_hash_phrase_or_order(store, clock):
    """`!reject P-n` carries no hash or phrase; a veto is never refused for being early."""
    plan = posted(store, make("!stop all"))
    assert Approvals(store, CONFIG).reject(received(104, message=1), plan.id).code == "rejected"
    late = posted(store, make("!stop alice"))
    clock.now = late.expires
    decision = Approvals(store, CONFIG).reject(received(104), late.id)
    assert (decision.code, decision.plan.state) == ("expired", EXPIRED)


# ------------------------------------------------------------- the entry points and the grammar


def test_post_classes_the_plan_itself(store):
    """No caller hands `post` a class: a teardown is destructive whoever posts it."""
    assert "requirement" not in inspect.signature(Approvals.post).parameters
    plan = posted(store, direct("teardown", ("bob",), ("bob",)))
    assert plan.requirement == Requirement(DESTRUCTIVE, 2, ("teardown", "1"))
    with pytest.raises(PlanError, match="its own fleet"):
        posted(store, make("!stop alice"), fleet=OTHER)


@pytest.mark.parametrize("hash6, phrase", [(None, ()), (3, ()), (H6, ["stop", "4"])])
def test_an_approval_names_its_hash_and_its_phrase(store, hash6, phrase):
    """A missing hash6 is an error, never a veto: the veto is `reject` alone."""
    plan = posted(store, make("!stop all"))
    with pytest.raises(PlanError, match="names the plan's hash"):
        Approvals(store, CONFIG).approve(received(), plan.id, hash6, phrase)
    assert store.get(plan.id) == plan


def plans_of_every_class():
    """(action, fleet, rebinds, the context of the bot it is approved on)."""
    workshop = Context(901, "Workshop admin · harvard-2026", PRODUCTION, Roster(FLEET.slugs))
    server = Context(900, "Workshop admin", PRODUCTION, None)
    named = "a" * 47  # §2.9: a new workshop's name has at most 47 characters
    out = [
        (make(message), FLEET, (), workshop) for message, _ in CLASSES if "rollout" not in message
    ]
    out += [(a, f, r, workshop if f else server) for a, f, r, _ in LATER]
    out.append((direct("new-workshop", (named, "region=us-east-2"), ws=""), None, (), server))
    return out


@pytest.mark.parametrize("action, fleet, rebinds, ctx", plans_of_every_class())
def test_every_phrase_parses_back_through_the_grammar(store, action, fleet, rebinds, ctx):
    """Whatever a plan asks for can be typed: `!approve P-n hash6 <phrase>` parses to it."""
    needs = requirement(action, fleet, CONFIG, rebinds=rebinds)
    parsed = parse(" ".join(("!approve P-1", H6, *needs.phrase)), ctx)
    assert isinstance(parsed, Command), parsed
    assert parsed.phrase == needs.phrase
