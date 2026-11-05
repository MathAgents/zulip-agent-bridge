"""#378: EC2 shows and enforces a change of termination protection minutes late, so `!teardown`
is refused for `PROTECT_SETTLE` after the workshop's last `!protect` (or `!launch … protect`),
at plan time and again at apply (docs/ADMIN.md §3, action 7)."""

import dataclasses
import time

import pytest

from zab.admin import fleet
from zab.admin.actions import PROTECT_SETTLE, ActionError, Protection
from zab.admin.fleet import ApplyEnd, Ledger
from zab.admin.planning import Planning
from zab.admin.plans import PENDING, REFUSED, STALE

from .botkit import OLGA, OSCAR, make_site
from .conftest import FLEET
from .test_admin_ending import LATE, FakeRunner, make, replies, request, say

AT = 1_800_000_000.0  # 2027-01-15 08:00:00 UTC, botkit's Clock
TS = "2027-01-15T08:00:00Z"


def test_the_window_is_five_minutes():
    assert PROTECT_SETTLE == 300


def test_the_wait_says_why_and_when_to_retry():
    last = Protection("P-41", "protect", AT + 12)  # 08:00:12
    why = last.wait(AT + 60)
    assert why == (
        "P-41 (!protect) changed termination protection here at 08:00 UTC, and EC2 can take "
        "minutes to show and enforce that: no !teardown or !retire for 5 minutes after it. "
        "Request it again from 08:06 UTC"  # 08:05:13, as a whole minute never early
    )
    assert last.wait(None)  # an unknown time waits: fail closed
    assert last.wait(AT - 3600)  # a clock set back: still waits
    launch = Protection("P-7", "launch", AT).wait(AT)
    assert launch.startswith("P-7 (!launch … protect) changed") and "from 08:06 UTC" in launch
    assert len(why) < 300  # the refusal's reason is cut at 300 (`Planning._refuse`)


@pytest.mark.parametrize("written", [0.0, 0.5, 0.999])
def test_the_wait_is_never_a_moment_short(written):
    """The record's `ts` is cut to the second: written at `at + written`, the apply's record
    is followed by 300 s of waiting, wherever in that second it was written."""
    last = Protection("P-41", "protect", AT)
    assert last.until == AT + 1 + PROTECT_SETTLE
    assert last.wait(AT + written + PROTECT_SETTLE - 0.001)  # 300 s after the true time: not yet
    assert last.wait(AT + PROTECT_SETTLE + 0.999)
    assert last.wait(AT + PROTECT_SETTLE + 1) == ""
    assert "from 08:06 UTC" in last.wait(AT)  # 08:05:01, as a whole minute


def with_protection(at: float, now: float | None):
    return dataclasses.replace(FLEET, protection=Protection("P-41", "protect", at), read_at=now)


def test_only_teardown_waits():
    recent = with_protection(AT, AT + 60)
    with pytest.raises(ActionError, match=r"^P-41 \(!protect\) changed termination protection"):
        make("!teardown all unsynced", recent)
    with pytest.raises(ActionError, match="no !teardown or !retire for 5 minutes"):
        make("!teardown spares", recent)  # the whole workshop, spares too
    with pytest.raises(ActionError, match="no !final-sync after its !uninstall on record"):
        make("!teardown all", recent)  # the lasting refusal first: waiting wouldn't help
    for other in ("!stop all", "!protect off alice", "!uninstall all", "!final-sync all"):
        make(other, recent)
    with pytest.raises(ActionError, match="no !teardown or !retire for 5 minutes"):
        make("!teardown all unsynced", with_protection(AT, AT + PROTECT_SETTLE))
    assert make("!teardown all unsynced", with_protection(AT, AT + PROTECT_SETTLE + 1)).steps


def test_the_fleet_checks_its_new_fields():
    with pytest.raises(ActionError, match="a Protection"):
        dataclasses.replace(FLEET, protection=("P-41", "protect", AT))
    with pytest.raises(ActionError, match="read time"):
        dataclasses.replace(FLEET, read_at="now")


# ---------------------------------------------------------------------------------- the ledger


def record(event, verb="protect", plan="P-41", ts=TS, ws="harvard-2026", args=("on", "alice")):
    fields = {"event": event, "ts": ts, "workshop": ws, "plan": plan}
    if event != "interrupted":
        fields |= {"verb": verb, "args": list(args), "targets": ["alice"]}
    return fields


def last(ledger, ws="harvard-2026"):
    state = ledger.workshop(ws)
    return state.protection if state else None


def test_the_ledger_keeps_the_last_protect_apply_whatever_its_outcome():
    ledger = Ledger()
    ledger.observe(record("apply_start"))
    assert last(ledger) == Protection("P-41", "protect", AT)  # still applying: it counts
    ledger.observe(record("apply_end", ts="2027-01-15T08:04:30Z") | {"exit": 1})
    assert last(ledger) == Protection("P-41", "protect", AT + 270)
    assert ledger.protecting == {}
    ledger.observe(record("apply_end", "stop", "P-42", "2027-01-15T09:00:00Z") | {"exit": 0})
    ledger.observe(record("apply_end", "launch", "P-43", "2027-01-15T09:00:00Z", args=["all"]))
    assert last(ledger).plan == "P-41"  # neither changes protection
    ledger.observe(record("apply_start", "launch", "P-44", "2027-01-15T09:10:00Z",
                          args=["all", "protect"]))  # fmt: skip
    assert last(ledger) == Protection("P-44", "launch", AT + 4200)
    assert last(ledger, "zab-test") is None


def test_an_interrupted_protect_counts_from_its_interrupted_record():
    """A bot killed mid-apply writes no `apply_end`; its next start writes `interrupted`."""
    ledger = Ledger()
    ledger.observe(record("apply_start"))
    ledger.observe(record("interrupted", plan="P-9", ts="2027-01-15T08:30:00Z"))  # another plan
    assert last(ledger).at == AT
    ledger.observe(record("interrupted", ts="2027-01-15T08:20:00Z"))
    assert last(ledger) == Protection("P-41", "protect", AT + 1200)
    assert ledger.protecting == {}


@pytest.mark.parametrize(
    "bad", [{"ts": "yesterday"}, {"ts": None}, {"plan": "P-x"}, {"workshop": 3}, {"verb": None}]
)
def test_a_record_it_cannot_read_is_ignored(bad):
    ledger = Ledger()
    ledger.observe(record("apply_end") | bad)
    assert last(ledger) is None


def test_read_fleet_gives_the_last_change_and_the_time(tmp_path):
    srv = make_site(tmp_path).server()
    ws = srv.config.workshop("harvard-2026")
    ledger = Ledger()
    ledger.observe(record("apply_end"))
    got = fleet.read_fleet(srv.config, ws, ledger, now=AT + 5)
    assert (got.protection, got.read_at) == (Protection("P-41", "protect", AT), AT + 5)
    other = fleet.read_fleet(srv.config, srv.config.workshop("zab-test"), ledger, now=AT)
    assert other.protection is None


# ------------------------------------------------------------------------ the bot, end to end


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


async def protected(server, plan="P-20", word="off"):
    """A runner's `!protect` apply, as it writes it (`fleet.ApplyEnd`)."""
    fields = {"workshop": "harvard-2026", "plan": plan, "verb": "protect", "args": [word, "alice"]}
    await server.audit("apply_start", **fields, **ApplyEnd.start_fields(("alice",)))
    end = ApplyEnd("harvard-2026", plan, "protect", (word, "alice"), 0, ("alice",))
    await server.audit("apply_end", **end.fields())


async def test_a_teardown_plan_waits_five_minutes_after_a_protect(site, server, runner):
    await protected(server)
    plan = await request(server, "!teardown all unsynced")
    assert plan.state == REFUSED
    assert replies(site)[-1].startswith(
        f"❌ {plan.name} refused: P-20 (!protect) changed termination protection here at 08:00 "
        "UTC, and EC2 can take minutes to show and enforce that: no !teardown or !retire for 5 "
        "minutes after it. Request it again from 08:06 UTC; nothing was done"
    )
    site.clock.now += PROTECT_SETTLE  # the record's second may have ended later: not yet
    assert (await request(server, "!teardown all unsynced", mid=5001)).state == REFUSED
    site.clock.now += 1
    assert (await request(server, "!teardown all unsynced", mid=5002)).state == PENDING


@pytest.mark.parametrize(
    ("command", "what"),
    [("!protect off alice", "!protect"), ("!launch carol protect", "!launch … protect")],
)
async def test_a_teardown_plan_waits_while_a_protect_applies(site, server, command, what):
    """However long the apply takes: the ledger's start counts for 5 minutes only, so the
    planner asks which plan is busy (`Planning._protecting`)."""
    busy = await request(server, command)
    assert busy.state == PENDING
    server.planner.handed["harvard-2026"] = busy.id  # approved, handed to the runner
    plan = await request(server, "!teardown all unsynced", mid=5001)
    assert plan.state == REFUSED
    assert replies(site)[-1].startswith(
        f"❌ {plan.name} refused: {busy.name} ({what}) is changing termination protection here "
        "now, and EC2 can take minutes to show and enforce that: no !teardown or !retire for 5 "
        "minutes after it ends. Request it again 5 minutes after its result; nothing was done"
    )
    plan = await request(server, "!teardown alice", mid=5002)  # the rows' refusal first
    assert plan.state == REFUSED and "no !final-sync after its !uninstall" in replies(site)[-1]
    del server.planner.handed["harvard-2026"]
    assert (await request(server, "!teardown all unsynced", mid=5003)).state == PENDING


async def test_another_busy_plan_holds_no_teardown(site, server):
    stop = await request(server, "!stop bob")
    server.planner.handed["harvard-2026"] = stop.id
    assert (await request(server, "!teardown all unsynced", mid=5001)).state == PENDING


async def test_a_protect_between_plan_and_approval_makes_the_teardown_stale(site, server, runner):
    """A9 step 4: the teardown planned, then `!protect off`, then its approval: STALE at
    apply, before anything runs, since `Planning.rebuild` builds it again."""
    plan = await request(server, "!teardown all unsynced")
    assert plan.state == PENDING
    site.clock.now += 60
    await protected(server)
    approve = f"!approve {plan.name} {plan.hash[:6]} teardown 3 unsynced"
    await say(server, OLGA, approve, LATE)
    await say(server, OSCAR, approve, LATE + 1)
    assert server.store.get(plan.id).state == STALE and runner.applied == []
    assert (
        f"❌ {plan.name} is stale: it no longer builds: P-20 (!protect) changed termination "
        "protection here at 08:01 UTC"
    ) in replies(site)[-1]


async def test_the_wait_survives_a_restart(site, server, runner):
    """The ledger is read from the audit log at start (`AdminServer._read_ledger`): its `ts`,
    which the log writes from the wall clock, as the bot's clock is in production."""
    site.clock.now = time.time()
    await protected(server)
    await server.close()
    again = site.server(planner=Planning(runner))
    await again.start()
    try:
        assert again.ledger.workshop("harvard-2026").protection.plan == "P-20"
        assert (await request(again, "!teardown all unsynced", mid=5003)).state == REFUSED
    finally:
        await again.close()
