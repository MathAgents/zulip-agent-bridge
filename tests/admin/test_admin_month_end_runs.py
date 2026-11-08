"""The month-end runs (ADMIN §11.5, #570): `monthend.order`, `run_at` and `due_rows`; and,
through the bot with `test_admin_retire`'s fake scripts, a run of the due rows in two passes,
its wait before each teardown, its record of each step, its resume after a restart, the rows
that drop out or stop, a cancel during the run, what holds a run back, and a late notice."""

from __future__ import annotations

import dataclasses
import json

import pytest

from zab.admin import monthend
from zab.admin import runner as runner_mod
from zab.admin.actions import PROTECT_SETTLE, Protection, Step
from zab.admin.grammar import keep_until_end
from zab.admin.monthend import NOTICE, NOTICE_S, SENT, Entry
from zab.admin.plans import APPLYING, DONE, FAILED, INTERRUPTED, PENDING, PLANNING
from zab.admin.runner import RunHooks

from .botkit import DREW, HARVARD, OLGA, incoming
from .test_admin_retire import LATE, server, site
from .test_admin_runner import approve, bot_of, calls, posts, records, request, settle

assert site and server  # fixtures, used by name

ENDS = keep_until_end("2027-02-15")
ROSTER = """\
slug,first_name,last_name,email,github,ip,region,zulip_user_id,repo,keep_until
alice,Alice,A,alice@example.com,alice-gh,198.51.100.1,us-west-2,2001,MathAgents/alice,2027-02-15
bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2,2002,MathAgents/bob,2027-02-15
carol,Carol,C,carol@example.com,carol-gh,,,,,
"""
FIRST = ("start", "idle-stop", "waker", "uninstall", "final-sync", "protect")


def step(row: str, stage: str) -> Step:
    return Step((stage, row), row=row, stage=stage)


def entry(**fields) -> Entry:
    base = dict(
        workshop="harvard-2026", slug="alice", plan=1, keep_until="2027-02-15", ends=ENDS,
        vms="i-1", tagged="i-1", bots=(), zulip_user_id="2001", warned=(),
        state=monthend.SCHEDULED, note="", changed=0.0,
    )  # fmt: skip
    return Entry(**(base | fields))


# ---------------------------------------------------------------------------------- order


def test_a_runs_steps_go_in_two_passes_without_what_is_done_but_start():
    prelude = Step(("power", "--start"), tolerated=True)
    stages = (*FIRST, "teardown", "bots")
    steps = [prelude, *(step(r, s) for r in ("alice", "bob") for s in stages)]
    fresh = monthend.order(steps, {})
    assert fresh[0] == prelude  # every row still has its start: the VMs boot together
    assert [(s.row, s.stage) for s in fresh[1:]] == [
        *(("alice", s) for s in FIRST), *(("bob", s) for s in FIRST),
        ("alice", "teardown"), ("alice", "bots"), ("bob", "teardown"), ("bob", "bots"),
    ]  # fmt: skip
    synced = dict.fromkeys(("start", "idle-stop", "waker", "uninstall", "final-sync"), 0)
    resumed = monthend.order(steps, {"alice": synced})
    assert prelude not in resumed  # a row resumed: no prelude
    assert [(s.row, s.stage) for s in resumed if s.row == "alice"] == [
        ("alice", "protect"), ("alice", "teardown"), ("alice", "bots")
    ]  # fmt: skip
    started = {"alice": dict.fromkeys(("start", "idle-stop", "waker", "uninstall"), 0)}
    assert [s.stage for s in monthend.order(steps, started) if s.row == "alice"] == [
        "start", "final-sync", "protect", "teardown", "bots"
    ]  # fmt: skip  # the sync needs SSH: its start again (§11.5)


def test_a_row_runs_at_its_end_once_its_notice_is_on_record_or_a_day_after_a_late_one():
    assert monthend.run_at(entry()) is None  # no notice yet: it waits
    # short notice, given by the approvers, who approved the schedule 20 minutes before: its end
    early = entry(warned=((NOTICE, ENDS - 600, SENT),), changed=ENDS - 1200)
    assert monthend.run_at(early) == ENDS
    late = entry(warned=((NOTICE, ENDS + 3600, SENT),))
    assert monthend.run_at(late) == ENDS + 3600 + monthend.LATE_S
    # #840: approved a month before, the notice only 10 minutes before the end (the bot down)
    cut = entry(warned=((NOTICE, ENDS - 600, SENT),), changed=ENDS - 30 * 86400)
    assert monthend.run_at(cut) == ENDS - 600 + monthend.LATE_S
    on_time = entry(warned=((NOTICE, ENDS - monthend.REMINDER_S - 1, SENT),))  # a day and more
    assert monthend.run_at(on_time) == ENDS
    assert monthend.end_of(entry(), ENDS - monthend.REMINDER_S - 1) == ENDS
    assert monthend.end_of(entry(changed=ENDS - 1200), ENDS - 1) == ENDS
    assert monthend.end_of(entry(), ENDS - 600) == ENDS - 600 + monthend.LATE_S
    assert monthend.end_of(entry(), ENDS + 5) == ENDS + 5 + monthend.LATE_S
    rows = [early, late, entry(slug="bob", state=monthend.RUNNING), entry(slug="carol")]
    assert [e.slug for e in monthend.due_rows(rows, ENDS)] == ["alice", "bob"]


# ---------------------------------------------------------------------- through the bot


@pytest.fixture
def waits(monkeypatch, site):
    """Each wait of a run before a teardown, in seconds; the bot's clock moves by it."""
    seen: list[float] = []

    async def wait(seconds: float) -> None:
        seen.append(round(seconds))
        site.clock.now += seconds

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(wait))
    return seen


async def scheduled(site, srv, roster: str = ROSTER) -> int:
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(roster)
    site.realm.person(2001, "Alice A")
    site.realm.person(2002, "Bob B")
    plan = await request(srv, "!month-end schedule")
    count = roster.count("2027-")
    await approve(srv, plan, sender=DREW, phrase=f"month-end {count}", mid=LATE + 1)
    await settle(srv)
    assert srv.store.get(plan.id).state == DONE
    return plan.id


async def look(site, srv, at: float) -> None:
    site.clock.now = at
    srv.planner.month_end_next = 0.0
    await srv.planner.tick()
    await settle(srv)


def ran(site) -> list[tuple[str, str]]:
    """Each row's step that ran on apply, in order: (row, script)."""
    out = []
    for c in calls(site):
        only = [c["argv"][i + 1] for i, a in enumerate(c["argv"]) if a == "--only"]
        if not c["dry"] and len(only) == 1 and c["script"] != "admin/status.py":
            out.append((only[0], c["script"]))
    return out


#: A row's whole chain in a run, in order.
CHAIN = (*FIRST, "teardown", "bots")
#: The scripts of a resumed row's first pass up to its sync, done again (`monthend.unsaved`).
AGAIN = ["aws/power.py", "admin/push_config.py", "admin/push_waker.py", "admin/push_config.py",
         "admin/final_sync.py"]  # fmt: skip


async def stopped_gracefully(srv, run: int, done: dict[str, tuple[str, ...]]) -> None:
    """The audit records of run `P-<run>` cut short by a restart that let it end its apply, as
    a stop of the service does (`Runner._end` on CancelledError, A17's restart): its
    `apply_start`, then its `apply_end`, `interrupted`, each row's `done` stages `ok`. The
    ledger then has their uninstall and sync, as a resumed run reads it."""
    from zab.admin.fleet import RowEnd

    targets = list(done)
    rows = {row: RowEnd(tuple((s, "ok" if s in stages else "not run") for s in CHAIN), False,
                        0).fields() for row, stages in done.items()}  # fmt: skip
    head = dict(workshop="harvard-2026", plan=f"P-{run}", bot=901, verb="retire", args=targets)
    await srv.audit("apply_start", **head, hash="", targets=targets)
    await srv.audit("apply_end", **head, exit=-1, targets=targets, rows=rows,
                    outcome="interrupted")  # fmt: skip


async def logged(srv, run: str, row: str, stages) -> None:
    """The `retire_step` records a month-end run writes as `row`'s `stages` end (#855)."""
    head = dict(workshop="harvard-2026", plan=run, bot=901)
    for stage in stages:
        await srv.audit("retire_step", **head, row=row, stage=stage, cell="ok")


async def rolled_out(srv, rows: list[str], plan: str = "P-90") -> None:
    """A `!rollout` naming `rows`, applied: their bridge reinstalled, and the ledger's uninstall
    and sync of them forgotten (`fleet.TOUCH`, `REINSTALL`)."""
    head = dict(workshop="harvard-2026", plan=plan, bot=901, verb="rollout",
                args=["0.2.20", *rows], targets=rows)  # fmt: skip
    await srv.audit("apply_start", **head, hash="")
    await srv.audit("apply_end", **head, exit=0, outcome="done")


def states(srv) -> dict[str, tuple[str, str]]:
    return {e.slug: (e.state, e.note) for e in monthend.entries(srv.store.db, "harvard-2026")}


async def test_a_run_ends_each_due_row_in_two_passes_and_retires_it(site, server, waits):
    schedule = await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)  # the notices
    await look(site, server, ENDS - 60)  # not yet
    assert not records(site, "apply_start")[1:]
    await look(site, server, ENDS + 60)
    starts = records(site, "apply_start")
    run = starts[-1]
    assert (run["verb"], run["args"], run["schedule"]) == (
        "retire",
        ["alice", "bob"],
        f"P-{schedule}",
    )
    assert run["approvers"] == [DREW]  # the schedule's
    order = ran(site)
    pass_one = [
        (r, s) for r, s in order if s not in ("aws/teardown.py", "admin/zulip_provision.py")
    ]
    assert [r for r, _ in pass_one] == ["alice"] * 6 + ["bob"] * 6  # each row's first pass
    tail = order[len(pass_one) :]
    assert tail == [("alice", "aws/teardown.py"), ("alice", "admin/zulip_provision.py"),
                    ("bob", "aws/teardown.py"), ("bob", "admin/zulip_provision.py")]  # fmt: skip
    # alice's teardown waited; bob's was due by then, but for the seconds his steps took
    assert [w for w in waits if w >= 1] == [PROTECT_SETTLE]
    assert states(server) == {
        "alice": (monthend.DONE, f"retired by {run['plan']}"),
        "bob": (monthend.DONE, f"retired by {run['plan']}"),
    }
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    done = monthend.done(server.store.db, alice)
    assert set(done) == {*FIRST, "teardown", "bots"}
    assert done["teardown"][1] >= done["protect"][2] + PROTECT_SETTLE  # never sooner (#378)
    (end,) = [r for r in records(site, "apply_end") if r["plan"] == run["plan"]]
    assert end["outcome"] == "done" and all("at" in s and "s" in s for s in end["steps"])
    assert set(server.ledger.workshop("harvard-2026").retired) == {"alice", "bob"}
    result = next(p for p in posts(site) if p.startswith(f"✅ **{run['plan']}**"))
    assert "`!retire alice bob`: done in " in result and "2 of 2 rows retired" in result
    plan = server.store.get(int(run["plan"][2:]))
    assert (plan.state, plan.requester, plan.bot) == (DONE, plan.bot, plan.bot)


async def test_a_run_resumes_a_row_at_its_first_unfinished_step(site, server, waits):
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    done = ("start", "idle-stop", "waker", "uninstall")
    for stage in done:  # a run cut short by a restart
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 1)
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    await stopped_gracefully(server, 77, {"alice": done})  # the ledger: uninstalled
    await look(site, server, ENDS + 60)
    assert [s for r, s in ran(site) if r == "alice"] == [
        "aws/power.py",  # its start again: its sync needs SSH (§11.5)
        "admin/final_sync.py", "aws/protect.py", "aws/teardown.py", "admin/zulip_provision.py",
    ]  # fmt: skip
    assert states(server)["alice"][0] == monthend.DONE


async def test_a_resumed_row_past_its_protection_waits_out_only_what_is_left(site, server, waits):
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    for stage in FIRST:
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 100)
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    await stopped_gracefully(server, 77, {"alice": FIRST})
    await look(site, server, ENDS + 160)  # 60 s after its protection off
    assert [s for r, s in ran(site) if r == "alice"] == [
        "aws/teardown.py", "admin/zulip_provision.py"
    ]  # fmt: skip  # no second sync
    assert waits == [PROTECT_SETTLE - 60]


async def test_a_row_whose_vm_changed_drops_out_and_the_others_run(site, server, waits):
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    site.vms = {"alice": "i-9", "bob": "i-2"}  # alice's VM replaced since the approval
    await look(site, server, ENDS + 60)
    assert states(server)["alice"] == (
        monthend.DROPPED, "its VM changed since the approval: i-1 → i-9"
    )  # fmt: skip
    assert states(server)["bob"][0] == monthend.DONE
    assert not [s for r, s in ran(site) if r == "alice"]
    assert any("dropped out of the schedule: alice (its VM changed" in p for p in posts(site))


async def test_a_failed_sync_stops_its_row_and_the_others_go_on(site, server, waits):
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    (site.tmp / "fail-sync").write_text("bob")
    await look(site, server, ENDS + 60)
    run = records(site, "apply_start")[-1]["plan"]
    assert states(server) == {
        "alice": (monthend.DONE, f"retired by {run}"),
        "bob": (monthend.STOPPED, f"stopped at final-sync (failed: exit 1) in {run}"),
    }
    assert "aws/teardown.py" not in [s for r, s in ran(site) if r == "bob"]
    await look(site, server, ENDS + 120)  # a stopped row is never retried on its own
    assert records(site, "apply_start")[-1]["plan"] == run


async def test_a_cancel_during_the_run_stops_the_row_before_its_next_step(
    site, server, waits, monkeypatch
):
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    real = RunHooks.after

    def after(self, step_, ran_):
        real(self, step_, ran_)
        if (step_.row, step_.stage) == ("alice", "final-sync"):  # an organizer's cancel, now
            monthend.cancel(server.store.db, "harvard-2026", ["bob"], "by Olga", site.clock())

    monkeypatch.setattr(RunHooks, "after", after)
    await look(site, server, ENDS + 60)
    assert states(server)["bob"] == (monthend.CANCELLED, "by Olga")
    assert not [s for r, s in ran(site) if r == "bob" and s != "aws/power.py"]
    assert states(server)["alice"][0] == monthend.DONE


async def test_nothing_runs_while_the_workshop_is_busy_or_a_protect_settles(
    site, server, waits, monkeypatch
):
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    server.planner.handed["harvard-2026"] = 999  # a plan approved and handed over
    await look(site, server, ENDS + 60)
    assert len(records(site, "apply_start")) == 1  # the schedule's own
    del server.planner.handed["harvard-2026"]
    real = server.read_fleet

    async def protected(ws):  # a !protect a minute ago: its 5 minutes are a wait (§11.5)
        fleet = await real(ws)
        recent = Protection("P-41", "protect", site.clock() - 60)
        return dataclasses.replace(fleet, protection=recent)

    monkeypatch.setattr(server, "read_fleet", protected)
    await look(site, server, ENDS + 120)
    assert len(records(site, "apply_start")) == 1
    monkeypatch.setattr(server, "read_fleet", real)
    await look(site, server, ENDS + 600)
    assert records(site, "apply_start")[-1]["verb"] == "retire"


async def test_a_late_notice_postpones_the_end_by_a_day(site, server, waits):
    """A row whose end passed before any notice went (the server down, or a release without
    the warnings): its notice names an end a day later, and it runs then, not before."""
    await scheduled(site, server)
    await look(site, server, ENDS + 3600)
    dms = [p for p in site.realm.posts() if p["type"] == "private"]
    assert {tuple(json.loads(d["to"])) for d in dms} == {(2001,), (2002,)}
    assert all("ends on Tuesday, February 16, 2027, at 16:00 UTC" in d["content"] for d in dms)
    assert len(records(site, "apply_start")) == 1  # no run yet
    await look(site, server, ENDS + 3600 + monthend.LATE_S - 60)
    assert len(records(site, "apply_start")) == 1
    await look(site, server, ENDS + 3600 + monthend.LATE_S + 60)
    assert records(site, "apply_start")[-1]["verb"] == "retire"
    assert {s[0] for s in states(server).values()} == {monthend.DONE}


async def test_a_restart_marks_the_run_interrupted_and_its_rows_resume(site, waits):
    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    srv = started()
    await srv.start()
    try:
        await scheduled(site, srv)
        await look(site, srv, ENDS - NOTICE_S + 60)
        store, held = srv.store, monthend.entries(srv.store.db, "harvard-2026")
        run = store.begin(
            workshop="harvard-2026", bot=901, requester=901, command="!retire alice bob"
        )
        store.move(run.id, (PLANNING,), APPLYING)
        with store.db:
            store.db.execute("INSERT INTO plan_topics SELECT ?, stream, topic FROM plan_topics "
                             "WHERE plan = ?", (run.id, held[0].plan))  # fmt: skip
        monthend.begin_run(store.db, run.id, held[0].plan, held)
        for stage in FIRST:  # alice got through her first pass
            monthend.record(store.db, held[0], stage, run.id, ENDS, ENDS + 400)
        await stopped_gracefully(srv, run.id, {"alice": FIRST, "bob": ()})  # as at a stop
    finally:
        await srv.close()
    srv = started()
    await srv.start()
    try:
        assert srv.store.get(run.id).state == INTERRUPTED
        said = next(p for p in posts(site) if p.startswith(f"⏹ {run.name} was interrupted"))
        assert f"It is a month-end run of P-{held[0].plan}: the bot's next look resumes" in said
        await look(site, srv, ENDS + 1000)
        assert [s for r, s in ran(site) if r == "alice"] == [
            "aws/teardown.py", "admin/zulip_provision.py"
        ]  # fmt: skip
        assert {s[0] for s in states(srv).values()} == {monthend.DONE}
    finally:
        await srv.close()


# ------------------------------------------------------- Codex round 1 on #835: the guards


async def test_a_cancel_during_the_wait_before_the_teardown_stops_it(site, server, monkeypatch):
    """Codex's first sequence: `!month-end cancel` while the run waits out its 5 minutes. The
    row's VM is not torn down, nor its bots deactivated."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)

    async def cancelled_meanwhile(seconds: float) -> None:  # an organizer's cancel, mid-wait
        monthend.cancel(server.store.db, "harvard-2026", ["alice"], "by Olga", site.clock())
        site.clock.now += seconds

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(cancelled_meanwhile))
    await look(site, server, ENDS + 60)
    mine = [s for r, s in ran(site) if r == "alice"]
    assert "aws/protect.py" in mine and "aws/teardown.py" not in mine, mine
    assert "admin/zulip_provision.py" not in mine
    assert states(server)["alice"] == (monthend.CANCELLED, "by Olga")
    result = next(p for p in posts(site) if p.startswith("❌ **P-") and "!retire alice" in p)
    assert "alice stopped before teardown: its month end was cancelled during the run" in result


async def test_a_new_schedule_never_inherits_an_earlier_ones_progress(site, server, waits):
    """Codex's second sequence: a run of one schedule got as far as alice's sync, then she was
    cancelled, kept on, and scheduled again with a later end. The new schedule's run
    uninstalls and syncs her again before anything is torn down: the work since is saved."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    first = await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    for stage in ("start", "idle-stop", "waker", "uninstall", "final-sync"):
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 1)  # the first's run
    monthend.cancel(server.store.db, "harvard-2026", ["alice"], "by Olga", ENDS)
    later = keep_until_end("2027-02-22")
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(
        roster.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,2027-02-22")
    )
    site.clock.now = ENDS + 3600
    plan = await request(server, "!month-end schedule", mid=6000)
    await approve(server, plan, sender=DREW, phrase="month-end 1", mid=LATE + 2)
    await settle(server)
    assert plan.id != first
    await look(site, server, later - NOTICE_S + 60)
    await look(site, server, later + 60)
    assert [s for r, s in ran(site) if r == "alice"] == [
        "aws/power.py", "aws/power.py",  # every VM at once (alice alone), then hers, waited for
        "admin/push_config.py", "admin/push_waker.py", "admin/push_config.py",
        "admin/final_sync.py", "aws/protect.py", "aws/teardown.py", "admin/zulip_provision.py",
    ]  # fmt: skip  # every step again, its sync before its teardown
    assert states(server)["alice"][0] == monthend.DONE


@pytest.mark.parametrize("lookup", ["replaced", "fails"])
async def test_the_teardown_stops_unless_its_vm_is_still_the_approved_one(
    site, server, waits, monkeypatch, lookup
):
    """The guard reads the VMs afresh after the wait: another VM, or a read that fails, stops
    the row before its teardown; nothing uncertain proceeds."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    real = RunHooks.after

    def after(self, step_, ran_):
        real(self, step_, ran_)
        if step_.stage == "protect":
            if lookup == "replaced":
                site.tagged = {"alice": "i-9"}
            else:
                self.runner.tagged = None  # no lookup: as one that fails

    monkeypatch.setattr(RunHooks, "after", after)
    await look(site, server, ENDS + 60)
    mine = [s for r, s in ran(site) if r == "alice"]
    assert "aws/protect.py" in mine and "aws/teardown.py" not in mine, mine
    cell = "its VM changed (i-1 → i-9)" if lookup == "replaced" else "its VMs can't be read"
    assert states(server)["alice"][0] == monthend.STOPPED
    assert cell in states(server)["alice"][1], states(server)["alice"]
    # the result: its title and the row's line name the step the guard stopped it before, not
    # an earlier one (`runner.stopped_at`, as rebased on #827's)
    result = next(p for p in posts(site) if p.startswith("❌ **P-") and "!retire alice" in p)
    title = result.split("\n", 1)[0]
    assert title.endswith(": 0 of 1 rows retired; alice stopped at teardown. The other rows "
                          "went on; send it again for those stopped"), title  # fmt: skip
    assert (
        f"- alice stopped before teardown, which didn't run: {cell}. Check the row, then "
        "`!retire alice`." in result
    ), result


def test_a_row_stopped_before_a_step_stopped_there_whatever_failed_before():
    """A month-end run's row stopped by its guard or a cancel, before a step, stopped at that
    step, even after a failure its run tolerated (rebased on #827's `stopped_at`); a row that
    failed stopped at its last failure, as before."""
    tolerated = {"start": "ok", "waker": "failed: exit 1", "uninstall": "ok", "protect": "ok"}
    for cell in ("stopped: its VM changed (i-1 → i-9)", runner_mod.CANCELLED_CELL):
        cells = {**tolerated, "teardown": cell, "bots": "not run"}
        assert runner_mod.stopped_at(cells) == "teardown"
    assert runner_mod.stopped_at({**tolerated, "teardown": "failed: exit 2"}) == "teardown"
    assert runner_mod.stopped_at(tolerated) == "waker"


# ------------------------------------------------------ Codex round 2 on #835: the roster


async def test_a_keep_until_applied_before_the_run_starts_drops_its_row(
    site, server, waits, monkeypatch
):
    """Codex's sequence: a `!keep-until` that extends alice's date applies while the look
    awaits, after its own check of the roster: the run reads the roster again under the lock,
    drops alice, and ends bob alone."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    real = type(server.planner)._run

    async def extended_meanwhile(self, ws):  # the `!keep-until`'s apply, done just now
        roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
        roster.write_text(
            ROSTER.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,2027-02-22")
        )
        return await real(self, ws)

    monkeypatch.setattr(type(server.planner), "_run", extended_meanwhile)
    await look(site, server, ENDS + 60)
    assert not [s for r, s in ran(site) if r == "alice"]
    assert states(server)["alice"] == (
        monthend.DROPPED, "its keep_until changed to 2027-02-22 since the approval"
    )  # fmt: skip
    assert states(server)["bob"][0] == monthend.DONE


async def test_the_teardown_stops_when_the_roster_changed_since_the_run_began(
    site, server, waits, monkeypatch
):
    """The guard's last check reads the roster again: a `keep_until` changed by any way during
    the run (a hand edit here, since the run holds the workshop's lock) stops the teardown."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    real = RunHooks.after

    def after(self, step_, ran_):
        real(self, step_, ran_)
        if step_.stage == "protect":
            path = site.tmp / "ws" / "harvard-2026" / "roster.csv"
            path.write_text(roster.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,"))

    monkeypatch.setattr(RunHooks, "after", after)
    await look(site, server, ENDS + 60)
    mine = [s for r, s in ran(site) if r == "alice"]
    assert "aws/protect.py" in mine and "aws/teardown.py" not in mine, mine
    assert states(server)["alice"][0] == monthend.STOPPED
    assert "its keep_until cleared since the approval" in states(server)["alice"][1]


# ------------------------------------------------------ Codex round 3 on #835: the ledger


async def test_a_restart_before_the_runs_apply_end_still_retires_its_finished_row(site, waits):
    """Codex's sequence, against #855's design: the bot is killed after alice's `bots` step,
    before the run's `apply_end`. Each step was on the audit log as it ended: alice is retired
    in the ledger from her own step's record, at its place, and at the next look she is done,
    nothing run again; bob, his first pass on the log, resumes at his teardown, his sync being
    the ledger's from his own records, with no `apply_end` needed."""
    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    srv = started()
    await srv.start()
    try:
        await scheduled(site, srv)
        await look(site, srv, ENDS - NOTICE_S + 60)
        store, held = srv.store, monthend.entries(srv.store.db, "harvard-2026")
        run = store.begin(
            workshop="harvard-2026", bot=901, requester=901, command="!retire alice bob"
        )
        store.move(run.id, (PLANNING,), APPLYING)
        with store.db:
            store.db.execute("INSERT INTO plan_topics SELECT ?, stream, topic FROM plan_topics "
                             "WHERE plan = ?", (run.id, held[0].plan))  # fmt: skip
        monthend.begin_run(store.db, run.id, held[0].plan, held)
        await logged(srv, run.name, "alice", (*FIRST, "teardown", "bots"))  # alice's every step
        for n, stage in enumerate((*FIRST, "teardown", "bots")):
            monthend.record(store.db, held[0], stage, run.id, ENDS + 10 * n, ENDS + 10 * n + 4)
        await logged(srv, run.name, "bob", FIRST)  # bob's first pass
        for stage in FIRST:
            monthend.record(store.db, held[1], stage, run.id, ENDS, ENDS + 400)
    finally:
        await srv.close()
    srv = started()
    await srv.start()
    try:
        before = srv.ledger.workshop("harvard-2026")
        assert "alice" in before.retired and before.ending()["bob"].counts  # no apply_end
        await look(site, srv, ENDS + 1000)
        assert not [s for r, s in ran(site) if r == "alice"]  # nothing of hers again
        assert [s for r, s in ran(site) if r == "bob"] == [
            "aws/teardown.py", "admin/zulip_provision.py"
        ]  # fmt: skip
        assert states(srv)["alice"] == (monthend.DONE, "retired")
        assert not [r for r in records(site, "month_end") if r.get("what") == "retired"]
        assert set(srv.ledger.workshop("harvard-2026").retired) == {"alice", "bob"}
    finally:
        await srv.close()
    srv = started()  # the ledger read back from the audit log alone
    await srv.start()
    try:
        retired = srv.ledger.workshop("harvard-2026").retired
        assert set(retired) == {"alice", "bob"} and retired["alice"].startswith(f"{run.name} at ")
        held = srv.ledger.workshop("harvard-2026")
        assert "alice" in held.down and "alice" in held.synced  # as `!retire`'s record says
    finally:
        await srv.close()


async def test_a_run_whose_apply_end_cant_be_written_retires_its_rows_all_the_same(
    site, server, waits, monkeypatch
):
    """Codex round 4 on #835, against #855's design: every step succeeds, but the run's
    `apply_end` can't be written. Each row's bots step was on the log as it ended: the rows
    are retired in the ledger and done at once, and the next look runs nothing again."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    real, failing = server.audit, [True]

    async def audit(event, **fields):
        if failing[0] and event == "apply_end" and fields.get("verb") == "retire":
            raise OSError("no space left on the device")
        return await real(event, **fields)

    monkeypatch.setattr(server, "audit", audit)
    await look(site, server, ENDS + 60)
    assert {state for state, _ in states(server).values()} == {monthend.DONE}
    assert set(server.ledger.workshop("harvard-2026").retired) == {"alice", "bob"}
    steps = len(ran(site))
    failing[0] = False
    await look(site, server, ENDS + 120)
    assert len(ran(site)) == steps  # nothing ran again
    assert not [r for r in records(site, "month_end") if r.get("what") == "retired"]


# ------------------------------------------------- Codex round 5 on #835: never stuck busy


def run_plans(srv) -> list[tuple[int, str]]:
    return srv.store.db.execute(
        "SELECT id, state FROM plans WHERE command LIKE '!retire %' ORDER BY id"
    ).fetchall()


@pytest.mark.parametrize("where", ["rows", "request", "first step", "apply's end"])
async def test_a_run_an_error_ends_never_keeps_the_workshop_busy(
    site, server, waits, monkeypatch, where
):
    """Each way an error can end a run between its plan made APPLYING and its end: its rows
    marked `running` (`monthend.begin_run`), its `request` record, anything after `apply_start`
    before its first step (`RunHooks`), its own end before `apply_end` (`_end`). The plan is
    failed, the workshop free; at the next look the rows run, resume, or, every step done, are
    recorded retired, and are done."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    failing = [True]

    def fail() -> None:
        if failing[0]:
            raise OSError("no space left on the device")

    if where == "rows":
        begin = monthend.begin_run

        def begin_run(*args):
            fail()
            return begin(*args)

        monkeypatch.setattr(monthend, "begin_run", begin_run)
    elif where == "request":
        audit = server.audit

        async def audited(event, **fields):
            if event == "request" and "schedule" in fields:
                fail()
            return await audit(event, **fields)

        monkeypatch.setattr(server, "audit", audited)
    elif where == "first step":
        init = RunHooks.__init__

        def hooks(self, *args):
            fail()
            init(self, *args)

        monkeypatch.setattr(RunHooks, "__init__", hooks)
    else:
        reload = server.reload_secrets

        def reloaded():
            fail()
            reload()

        monkeypatch.setattr(server, "reload_secrets", reloaded)
    await look(site, server, ENDS + 60)
    ((_, state),) = run_plans(server)
    assert state == FAILED, state
    assert server.planner.busy("harvard-2026") is None and not server.planner.runner.applying
    left = {state for state, _ in states(server).values()}
    assert left == ({monthend.SCHEDULED} if where == "rows" else {monthend.RUNNING}), left
    steps = len(ran(site))
    assert steps == (8 * 2 if where == "apply's end" else 0), ran(site)
    # review of #835, N1: one line in the schedule's topic, and the next run held 2 minutes
    said = next(p for p in posts(site) if p.startswith("❌ P-") and "failed before it ended" in p)
    assert "(OSError). Its rows are taken up again in 2 min at the earliest" in said, said
    failing[0] = False
    await look(site, server, ENDS + 120)  # held: no run yet
    assert len(run_plans(server)) == 1 and len(ran(site)) == steps
    await look(site, server, ENDS + 60 + 121)
    assert len(ran(site)) == (steps or 8 * 2)  # every step once, none twice
    if where == "apply's end":  # every step done: recorded retired at the look (round 3)
        assert states(server) == {"alice": (monthend.DONE, "retired"),
                                  "bob": (monthend.DONE, "retired")}  # fmt: skip
        assert len(run_plans(server)) == 1
    else:
        (_, first), (again, done) = run_plans(server)
        assert (first, done) == (FAILED, DONE)
        assert states(server) == {"alice": (monthend.DONE, f"retired by P-{again}"),
                                  "bob": (monthend.DONE, f"retired by P-{again}")}  # fmt: skip
    assert set(server.ledger.workshop("harvard-2026").retired) == {"alice", "bob"}


async def test_runs_that_fail_before_their_end_back_off(site, server, waits, monkeypatch):
    """Review of #835, N1: a persistent error makes a failed run, and a line, at 2, 4, 8, …
    minutes, not every minute; the first run that ends lifts the hold."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    failing = [True]
    begin = monthend.begin_run

    def begin_run(*args):
        if failing[0]:
            raise OSError("no space left on the device")
        return begin(*args)

    monkeypatch.setattr(monthend, "begin_run", begin_run)
    for minute in range(1, 15):
        await look(site, server, ENDS + 60 * minute)
    tried = [p for p in posts(site) if "failed before it ended" in p]
    assert len(run_plans(server)) == len(tried) == 3, tried  # at 1, 3 and 7 minutes
    assert "in 8 min at the earliest" in tried[-1], tried[-1]
    failing[0] = False
    await look(site, server, ENDS + 60 * 15 + 1)  # the hold over
    assert {state for state, _ in states(server).values()} == {monthend.DONE}
    assert not server.planner.month_end_held


@pytest.mark.parametrize("blip", ["EC2", "roster"])
async def test_a_read_that_fails_at_the_runs_start_waits_for_the_next_look(
    site, server, waits, monkeypatch, blip
):
    """Review of #835, N8: a blip of EC2 or of the roster at the run's start stops no row: the
    rows wait, said in the topic, and run at the next look; only `READ_TRIES` looks in a row
    stop them."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    runner = server.planner.runner
    failing = [2]  # the first two runs' reads fail
    if blip == "EC2":
        tagged = runner.tagged

        def lookup(fleet_, names):
            if failing[0]:
                raise OSError("EC2 unreachable")
            return tagged(fleet_, names)

        monkeypatch.setattr(runner, "tagged", lookup)
    else:
        people = runner_mod._people

        async def roster(fleet_):
            if failing[0]:
                raise OSError("roster unreadable")
            return await people(fleet_)

        monkeypatch.setattr(runner_mod, "_people", roster)
    for n in (1, 2):
        await look(site, server, ENDS + 60 * n)
        failing[0] -= 1
        line = next(p for p in reversed(posts(site)) if "a month-end run of" in p)
        assert f"can't be read (OSError). Nothing ran: the next look tries again ({n} of 5)" in (
            line
        ), line
        assert {state for state, _ in states(server).values()} == {monthend.RUNNING}
    assert not ran(site)
    await look(site, server, ENDS + 180)
    assert {state for state, _ in states(server).values()} == {monthend.DONE}
    assert not runner.unread


async def test_reads_that_keep_failing_stop_the_rows(site, server, waits, monkeypatch):
    """N8's bound: `READ_TRIES` looks in a row of failed reads stop the rows, for a
    `!retire`."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)

    def lookup(fleet_, names):
        raise OSError("EC2 unreachable")

    monkeypatch.setattr(server.planner.runner, "tagged", lookup)
    for n in range(1, runner_mod.READ_TRIES + 1):
        await look(site, server, ENDS + 60 * n)
    assert {state for state, _ in states(server).values()} == {monthend.STOPPED}
    assert states(server)["alice"][1] == "its run failed: OSError"
    line = next(p for p in reversed(posts(site)) if "a month-end run of" in p)
    assert "can't be read (OSError), 5 looks in a row. Nothing ran." in line, line
    assert not ran(site)


async def test_a_dropped_row_is_on_record_before_the_others_run(site, server, waits):
    """Review of #835, B3: alice's VM changed; bob runs. Her drop has its `month_end`
    record, whose head the line in the topic carries."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    site.tagged = {"alice": "i-9", "bob": "i-2"}
    site.vms = {"alice": "i-9", "bob": "i-2"}
    await look(site, server, ENDS + 60)
    (out,) = [r for r in records(site, "month_end") if r.get("reason") == "out of the run"]
    assert out["dropped"] == {"alice": "its VM changed since the approval: i-1 → i-9"}
    assert out["stopped"] == {}
    line = next(p for p in posts(site) if p.startswith(f"⚠ {out['plan']}: dropped out"))
    assert "audit #" in line, line  # its footer
    assert states(server)["bob"][0] == monthend.DONE


async def test_a_row_gone_after_its_protection_off_stops_with_the_reason(site, server, waits):
    """Review of #835, N2: a restart during alice's teardown, which ended her VM before it was
    recorded. The resume stops her, saying so and what ends her bots, rather than dropping her
    as a changed VM; bob's resume goes on."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    held = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, held[0].plan, held)
    for e in held:
        for stage in FIRST:
            monthend.record(server.store.db, e, stage, 77, ENDS, ENDS + 1)
    site.vms, site.tagged = {"bob": "i-2"}, {"bob": "i-2"}  # alice's VM terminated
    await look(site, server, ENDS + 400)
    alice = states(server)["alice"]
    assert alice[0] == monthend.STOPPED
    assert alice[1].startswith(
        "its VM is gone after its protection off: its teardown may have "
        "run before a restart. `!retire alice unsynced` deactivates"
    )
    assert not [s for r, s in ran(site) if r == "alice"]
    assert states(server)["bob"][0] == monthend.DONE
    line = next(p for p in posts(site) if "stopped before the run: alice (its VM is gone" in p)
    assert line.startswith("⚠ P-")


async def test_the_teardown_needs_this_schedules_final_sync_on_record(site, server, waits):
    """Review of #835, N5: a row whose protection off is on record but not its final sync
    (never, as the order goes) is never torn down."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    done = ("start", "idle-stop", "waker", "uninstall", "protect")  # no final-sync
    for stage in done:
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 1)
    await stopped_gracefully(server, 77, {"alice": done})
    real = monthend.order

    def without_sync(steps, finished):  # an order that would skip the sync: never, as built
        finished = {row: {**stages, "final-sync": (77, ENDS, ENDS)} for row, stages
                    in finished.items()}  # fmt: skip
        return real(steps, finished)

    import zab.admin.runner as runner_module

    runner_module.monthend.order = without_sync
    try:
        await look(site, server, ENDS + 400)
    finally:
        runner_module.monthend.order = real
    assert not [s for r, s in ran(site) if r == "alice"]
    assert states(server)["alice"][0] == monthend.STOPPED
    assert "its final sync is not on record for this schedule" in states(server)["alice"][1]


async def test_a_cancel_after_the_teardown_says_the_vm_is_gone(site, server, waits, monkeypatch):
    """Review of #835, B1: `!month-end cancel alice` through the bot while the run holds
    before her bots, her teardown done. The answer names her run, and her VM as gone; the
    result's line says so too, and gives `!retire alice unsynced`."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    real, sent = RunHooks.before, []

    async def before(self, step_, shown):
        if (step_.row, step_.stage) == ("alice", "bots") and not sent:
            sent.append(1)
            await bot_of(server, HARVARD).on_message(
                incoming(OLGA, "!month-end cancel alice", mid=LATE + 50)
            )
        return await real(self, step_, shown)

    monkeypatch.setattr(RunHooks, "before", before)
    await look(site, server, ENDS + 60)
    run = next(r["plan"] for r in records(site, "apply_start") if r.get("schedule"))
    answer = next(p for p in posts(site) if p.startswith("✅ The month end of alice"))
    assert f"alice is in run {run} now: it stops before its next step, and what that run did " \
           "stays done (its result lists it)." in answer, answer  # fmt: skip
    assert "The VM of alice is gone, torn down already: `!retire alice unsynced`" in answer
    assert "stays until a `!retire`" not in answer, answer
    assert "admin/zulip_provision.py" not in [s for r, s in ran(site) if r == "alice"]
    result = next(p for p in posts(site) if p.startswith(f"❌ **{run}**"))
    assert "- alice stopped before bots: its month end was cancelled during the run. Its VM " \
           "is gone, torn down already: `!retire alice unsynced` deactivates its bots and " \
           "marks it retired." in result, result  # fmt: skip


def test_a_guard_stop_before_the_bots_says_the_vm_is_gone():
    """B1, the guard's branch: a roster that can't be read before the bots."""
    cells = {"teardown": "ok", "bots": "stopped: its roster can't be read"}
    assert runner_mod.stopped_row(None, "alice", cells) == (
        "- alice stopped before bots, which didn't run: its roster can't be read. Its VM is "
        "gone, torn down already: `!retire alice unsynced` deactivates its bots and marks it "
        "retired."
    )


# ------------------------------------------------- Codex round 6 on #835: the sync evidence


async def test_a_resumed_run_keeps_the_rows_sync_on_record_when_its_teardown_fails(site, waits):
    """Codex's sequence: a restart after alice's protection off; the resumed run's teardown
    fails. Its result and `apply_end` name the steps the first run did, `ok`, with no second
    run of them, so the ledger, which the resumed `apply_start` made forget her uninstall and
    sync, has them back: a `!teardown` would count her sync. So does the ledger read back from
    the audit log at the next start."""
    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    from .test_admin_runner import fake

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    srv = started()
    await srv.start()
    try:
        await scheduled(site, srv, roster)
        await look(site, srv, ENDS - NOTICE_S + 60)
        store, (alice,) = srv.store, monthend.entries(srv.store.db, "harvard-2026")
        run = store.begin(workshop="harvard-2026", bot=901, requester=901, command="!retire alice")
        store.move(run.id, (PLANNING,), APPLYING)
        with store.db:
            store.db.execute("INSERT INTO plan_topics SELECT ?, stream, topic FROM plan_topics "
                             "WHERE plan = ?", (run.id, alice.plan))  # fmt: skip
        monthend.begin_run(store.db, run.id, alice.plan, [alice])
        for stage in FIRST:  # through her protection off
            monthend.record(store.db, alice, stage, run.id, ENDS, ENDS + 400)
        await stopped_gracefully(srv, run.id, {"alice": FIRST})  # as a stop of the service
    finally:
        await srv.close()
    fake(site, "aws/teardown.py", 'import sys\nprint("teardown: refused")\nsys.exit(1)\n')
    srv = started()
    await srv.start()
    try:
        await look(site, srv, ENDS + 1000)
        assert [s for r, s in ran(site) if r == "alice"] == ["aws/teardown.py"]
        (end,) = [r for r in records(site, "apply_end")
                  if r["verb"] == "retire" and r["plan"] != run.name]  # fmt: skip
        cells = end["rows"]["alice"]["cells"]
        assert list(cells) == [*FIRST, "teardown", "bots"], cells
        assert [cells[s] for s in FIRST] == ["ok"] * len(FIRST)
        assert cells["teardown"].startswith("failed") and cells["bots"] == "not run"
        assert end["rows"]["alice"]["retired"] is False
        result = next(p for p in posts(site) if p.startswith(f"❌ **{end['plan']}**"))
        assert "final-sync" in result and "alice stopped at teardown" in result
        ending = srv.ledger.workshop("harvard-2026").ending()["alice"]
        assert ending.counts and ending.synced.startswith(end["plan"]), ending
    finally:
        await srv.close()
    srv = started()  # the audit log replayed
    await srv.start()
    try:
        ending = srv.ledger.workshop("harvard-2026").ending()["alice"]
        assert ending.counts and ending.synced.startswith(end["plan"]), ending
    finally:
        await srv.close()


# ------------------------------------- review round 2 of #835: a rollout before the resume


@pytest.mark.parametrize("teardown", ["succeeds", "fails"])
async def test_a_rollout_before_the_resume_makes_the_row_sync_again(site, server, waits, teardown):
    """The reviewer's sequence: a run stopped after alice's protection off, its `apply_end`
    written, the ledger counting her sync; then a rollout naming her reinstalls her bridge, and
    the ledger forgets it. The resume does her first pass again, its sync included, before
    her teardown, which either succeeds, the work since the rollout saved, or fails, and then
    her cells are this run's own: the ledger's sync is the one this run did."""
    from .test_admin_runner import fake

    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    for stage in FIRST:
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 100)
    await stopped_gracefully(server, 77, {"alice": FIRST})
    assert server.ledger.workshop("harvard-2026").ending()["alice"].counts
    await rolled_out(server, ["alice"])
    assert "alice" not in server.ledger.workshop("harvard-2026").ending()
    if teardown == "fails":
        fake(site, "aws/teardown.py", 'import sys\nprint("teardown: refused")\nsys.exit(1)\n')
    await look(site, server, ENDS + 1000)
    assert [s for r, s in ran(site) if r == "alice"] == [
        *AGAIN, "aws/teardown.py", *(["admin/zulip_provision.py"] * (teardown == "succeeds"))
    ]  # fmt: skip
    (out,) = [r for r in records(site, "month_end") if r.get("reason") == "out of the run"]
    assert out["redone"] == ["alice"]
    line = next(p for p in posts(site) if p.startswith(f"⚠ {out['plan']}: uninstalled and synced"))
    assert line.split("\n")[0].endswith("reinstalled the bridge): alice.")
    (end,) = [r for r in records(site, "apply_end") if r["plan"] == out["plan"]]
    synced = next(s for s in end["steps"] if s["script"] == "admin/final_sync.py")
    assert synced["exit"] == 0  # this run's own sync, not one restored
    ending = server.ledger.workshop("harvard-2026").ending()["alice"]
    assert ending.counts and ending.synced.startswith(out["plan"])
    if teardown == "succeeds":
        assert states(server)["alice"][0] == monthend.DONE
    else:
        assert end["rows"]["alice"]["cells"]["teardown"].startswith("failed")
        assert states(server)["alice"][0] == monthend.STOPPED


def test_the_work_holds_only_while_the_ledger_says_so():
    """`monthend.unsaved`: the first pass is kept while the ledger counts the sync, or, before
    the sync, has the row uninstalled; otherwise it is done again, and so are stages before an
    uninstall, of which the ledger says nothing."""
    from zab.admin.actions import Ending

    synced = Ending("P-7 at 2027-02-15 15:00 UTC", "P-7 at 2027-02-15 15:05 UTC", True, "P-7")
    first = dict.fromkeys(FIRST, (7, 0.0, 0.0))
    assert monthend.unsaved(first, synced) == []
    assert monthend.unsaved(first, None) == list(FIRST[:5])  # protect stands
    assert monthend.unsaved(first, Ending("P-7 at …")) == list(FIRST[:5])  # its sync forgotten
    upto = dict.fromkeys(("start", "idle-stop", "waker", "uninstall"), (7, 0.0, 0.0))
    assert monthend.unsaved(upto, Ending("P-7 at …")) == []
    assert monthend.unsaved(upto, None) == ["start", "idle-stop", "waker", "uninstall"]
    assert monthend.unsaved({"start": (7, 0.0, 0.0)}, synced) == ["start"]
    assert monthend.unsaved({"protect": (7, 0.0, 0.0)}, None) == []


async def test_a_cancel_of_a_row_held_by_a_run_not_applying_says_so(site, server, waits):
    """Round 2's N1: alice `running`, held by a run that failed (P-77, not applying): the
    cancel says no run takes her up again, not that she is in it now."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    run = server.store.begin(workshop="harvard-2026", bot=901, requester=901,
                             command="!retire alice")  # fmt: skip
    server.store.move(run.id, (PLANNING,), FAILED)
    monthend.begin_run(server.store.db, run.id, alice.plan, [alice])
    await bot_of(server, HARVARD).on_message(
        incoming(OLGA, "!month-end cancel alice", mid=LATE + 60)
    )
    said = posts(site)[-1]
    assert f"alice was held by run {run.name}, which isn't applying: no run takes it up " \
           "again, and what it did stays done." in said, said  # fmt: skip
    assert "is in run" not in said


# --------------------------------------------- Codex round 7 on #835: a kill after a teardown


async def test_a_hard_kill_after_the_teardown_goes_on_to_the_bots(site, server, waits):
    """Codex's sequence: the process killed after alice's teardown is recorded, before her
    bots and the run's `apply_end`, so the ledger has no sync of hers. Her VM is gone: there
    is nothing to sync again, and her teardown's guard had her sync on record (N5). The resume
    keeps what was done and goes on to her bots; she is retired, not stopped."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    for stage in (*FIRST, "teardown"):
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 400)
    site.vms, site.tagged = {}, {}  # torn down
    held = server.ledger.workshop("harvard-2026")
    assert held is None or "alice" not in held.ending()  # a hard kill: no `apply_end`
    await look(site, server, ENDS + 1000)
    assert [s for r, s in ran(site) if r == "alice"] == ["admin/zulip_provision.py"]
    assert states(server)["alice"][0] == monthend.DONE
    assert "alice" in server.ledger.workshop("harvard-2026").retired
    assert not [r for r in records(site, "month_end") if r.get("redone")]


def test_nothing_is_saved_again_once_the_teardown_is_on_record():
    """`monthend.unsaved`: after a teardown there is no VM to sync, whatever the ledger says."""
    done = dict.fromkeys((*FIRST, "teardown"), (7, 0.0, 0.0))
    assert monthend.unsaved(done, None) == []


# ------------------------------------- review round 3 of #835: a real stop of the service


async def test_a_stop_of_the_service_in_the_wait_writes_the_runs_end_and_the_resume_goes_on(
    site, monkeypatch
):
    """The reviewer's probe (N2), with no record written by hand: a real run, held in its wait
    before alice's teardown, cut by the server's own stop (`close`, as `systemctl stop` does).
    Its `apply_end` is written, `interrupted`, with her first pass `ok`; the ledger counts her
    sync; and the resume at the next start runs her teardown and bots alone, with no `redone`
    record: what `stopped_gracefully` stands for in the other tests."""
    import asyncio

    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    waiting = asyncio.Event()

    async def held(seconds: float) -> None:  # the wait before her teardown, until the stop
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(held))
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    srv = started()
    await srv.start()
    try:
        await scheduled(site, srv, roster)
        await look(site, srv, ENDS - NOTICE_S + 60)
        site.clock.now, srv.planner.month_end_next = ENDS + 60, 0.0
        await srv.planner.tick()  # the run starts, and goes as far as the wait
        await asyncio.wait_for(waiting.wait(), 60)
    finally:
        await srv.close()
    (end,) = [r for r in records(site, "apply_end") if r["verb"] == "retire"]
    cells = end["rows"]["alice"]["cells"]
    assert end["outcome"] == "interrupted", end["outcome"]
    assert [cells[s] for s in FIRST] == ["ok"] * len(FIRST), cells
    assert (cells["teardown"], cells["bots"]) == ("not run", "not run"), cells
    first = len(ran(site))

    async def wait(seconds: float) -> None:
        site.clock.now += seconds

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(wait))
    srv = started()
    await srv.start()
    try:
        assert srv.ledger.workshop("harvard-2026").ending()["alice"].counts
        await look(site, srv, ENDS + 1000)
        assert [s for r, s in ran(site)[first:] if r == "alice"] == [
            "aws/teardown.py", "admin/zulip_provision.py"
        ]  # fmt: skip
        assert not [r for r in records(site, "month_end") if r.get("redone")]
        assert states(srv)["alice"][0] == monthend.DONE
    finally:
        await srv.close()


# -------------------------------------------------------------------- #840's follow-ups


async def test_a_notice_cut_short_by_downtime_postpones_the_end_by_a_day(site, server, waits):
    """#840 (N4): the schedule approved weeks before; the bot down until an hour before the
    end, so the notice goes then: it names an end a day after it, and the run waits for it."""
    await scheduled(site, server)
    await look(site, server, ENDS - 3600)
    dms = [p for p in site.realm.posts() if p["type"] == "private"]
    assert {tuple(json.loads(d["to"])) for d in dms} == {(2001,), (2002,)}
    assert all("ends on Tuesday, February 16, 2027, at 14:00 UTC" in d["content"] for d in dms)
    await look(site, server, ENDS + 60)
    assert len(records(site, "apply_start")) == 1  # the schedule's only: no run at the end
    await look(site, server, ENDS - 3600 + monthend.LATE_S + 60)
    assert records(site, "apply_start")[-1]["verb"] == "retire"
    assert {s[0] for s in states(server).values()} == {monthend.DONE}


async def test_the_approvers_short_notice_stands(site, server, waits):
    """#840 (N4)'s other case, as A17's: a schedule approved 20 minutes before the end; its
    notice, minutes before it, names the end, and the run comes then."""
    site.clock.now = ENDS - 1200
    await scheduled(site, server)
    await look(site, server, ENDS - 1140)
    dms = [p for p in site.realm.posts() if p["type"] == "private"]
    assert dms and all("ends on Monday, February 15, 2027, at 15:00 UTC" in d["content"]
                       for d in dms)  # fmt: skip
    await look(site, server, ENDS + 60)
    assert records(site, "apply_start")[-1]["verb"] == "retire"
    assert {s[0] for s in states(server).values()} == {monthend.DONE}


async def test_a_row_recovered_as_retired_gets_its_line(site, server, waits):
    """#840 (N6): a row whose every step a run did before a restart cut its `apply_end` is
    marked retired at the next look, and the schedule's topic says so."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    plan = await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    await logged(server, "P-77", "alice", (*FIRST, "teardown", "bots"))  # as the run wrote them
    for stage in (*FIRST, "teardown", "bots"):
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 1)
    await look(site, server, ENDS + 600)
    assert states(server)["alice"] == (monthend.DONE, "retired")
    line = next(p for p in posts(site) if p.startswith(f"📨 **P-{plan}**, the month end: marked"))
    assert line.split("\n")[0] == (
        f"📨 **P-{plan}**, the month end: marked retired, every step of their run done before a "
        "restart or a record that failed: alice."
    )
    (look_record,) = [r for r in records(site, "month_end") if r.get("what") == "look"
                      and "retired" in r.get("rows", {})]  # fmt: skip
    assert look_record["rows"]["retired"] == ["alice"]


@pytest.mark.parametrize("between", ["replaced", "cancelled and scheduled again"])
async def test_a_late_notices_end_holds_whatever_schedule_comes_after(site, server, waits, between):
    """Codex round 1 on #852: the bot down until an hour before the end, the notice names an
    end a day later; then another schedule of the same rows, the same end, approved half an
    hour later, or a cancel and a new schedule. The notice is kept with the end its DM named,
    and the run waits for it, never ending the VM at the original end."""
    await scheduled(site, server)
    await look(site, server, ENDS - 3600)
    named = ENDS - 3600 + monthend.LATE_S
    assert all(e.named == named for e in monthend.entries(server.store.db, "harvard-2026"))
    site.clock.now = ENDS - 1800
    if between != "replaced":
        await bot_of(server, HARVARD).on_message(
            incoming(OLGA, "!month-end cancel alice bob", mid=LATE + 40)
        )
        assert {s[0] for s in states(server).values()} == {monthend.CANCELLED}
    plan = await request(server, "!month-end schedule", mid=6000)
    await approve(server, plan, sender=DREW, phrase="month-end 2", mid=LATE + 41)
    await settle(server)
    held = monthend.entries(server.store.db, "harvard-2026")
    assert {e.plan for e in held} == {plan.id} and all(e.named == named for e in held)
    assert all(e.changed == ENDS - 1800 for e in held)  # approved inside the day
    await look(site, server, ENDS - 1740)  # no second notice: the first is the row's
    dms = [p for p in site.realm.posts() if p["type"] == "private"]
    assert len(dms) == 2, [d["content"][:60] for d in dms]
    await look(site, server, ENDS + 60)
    assert not [r for r in records(site, "apply_start") if r["verb"] == "retire"]
    await look(site, server, named + 60)
    assert [r for r in records(site, "apply_start") if r["verb"] == "retire"]
    assert {s[0] for s in states(server).values()} == {monthend.DONE}


def test_the_notice_keeps_the_end_its_dm_named():
    """`warn` keeps the end with the notice from its claim on: recorded `sent` after its
    claim `unsure`, it keeps it; taken back, it goes; `run_at` reads it."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    monthend.setup(db)
    alice = entry(changed=ENDS - 30 * 86400)
    db.execute(
        "INSERT INTO month_end (workshop, slug, plan, keep_until, ends, vms, tagged, bots, "
        "zulip_user_id, warned, state, note, changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?)",
        (alice.workshop, alice.slug, alice.plan, alice.keep_until, alice.ends, alice.vms,
         alice.tagged, "[]", alice.zulip_user_id, "[]", alice.state, "", alice.changed),
    )  # fmt: skip
    monthend.warn(db, alice, NOTICE, monthend.UNSURE, ENDS - 600)
    (kept,) = monthend.entries(db, alice.workshop)
    assert kept.named == ENDS - 600 + monthend.LATE_S == monthend.run_at(kept)
    monthend.warn(db, kept, NOTICE, SENT, ENDS - 300)  # its answer, later: the same end
    (kept,) = monthend.entries(db, alice.workshop)
    assert kept.named == ENDS - 600 + monthend.LATE_S and kept.warned[0][:3:2] == (NOTICE, SENT)
    monthend.warn(db, kept, NOTICE, None, ENDS - 200)  # taken back
    (kept,) = monthend.entries(db, alice.workshop)
    assert kept.named is None and monthend.run_at(kept) is None


async def test_a_late_refused_notice_stops_its_row_with_no_reminder(site, server, waits):
    """#852's review, N1, then #883: the bot down until an hour before the end, the notice goes
    late, naming a day after it, and Zulip refuses it each time. The row stops: no reminder
    goes, which would name that end, and no run starts. A reminder names the end its notice
    kept (`run_at`), or the row's own without one."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.refuse(server.store.db, alice, NOTICE, ENDS - 3600)  # 3 tries
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert alice.named == ENDS - 3600 + monthend.LATE_S and alice.state == monthend.STOPPED
    await look(site, server, ENDS - 3000)
    await look(site, server, alice.named + 60)
    assert [p for p in site.realm.posts() if p["type"] == "private"] == []
    assert not [r for r in records(site, "apply_start") if r["verb"] == "retire"]
    kept = entry(warned=((NOTICE, ENDS - 3600, SENT),), named=ENDS - 3600 + monthend.LATE_S)
    assert monthend.reminder(kept, "w", "").startswith(
        "**Reminder: your workshop VM ends on Tuesday, February 16, 2027, at 14:00 UTC.**"
    )
    assert monthend.reminder(entry(), "w", "").startswith(  # no notice kept: its own date
        "**Reminder: your workshop VM ends on Monday, February 15, 2027, at 15:00 UTC.**"
    )


# ------------------------------------------- #855: each step of a run on the audit log


def test_a_retired_record_of_the_builds_before_step_records_still_retires_its_row():
    """#870: a `month_end` record of `what: "retired"`, as the development builds between #835
    and #860 wrote it (no release did) for a row whose run's steps were all done but whose
    `apply_end` was never written, still retires the row in the ledger; one not of that shape
    retires nothing."""
    from zab.admin.fleet import Ledger

    cells = {s: "ok" for s in (*FIRST, "teardown", "bots")}
    record = {
        "event": "month_end", "ts": "2026-11-05T21:00:00Z", "workshop": "harvard-2026",
        "plan": "P-61", "what": "retired", "schedule": "P-60", "verb": "retire",
        "targets": ["alice"], "rows": {"alice": {"cells": cells, "retired": True, "seconds": 0}},
    }  # fmt: skip
    ledger = Ledger()
    ledger.observe(record)
    state = ledger.workshop("harvard-2026")
    assert state is not None and state.retired == {"alice": "P-61 at 2026-11-05 21:00 UTC"}
    assert state.down["alice"] and state.synced["alice"]  # uninstalled and synced, as it says
    other = Ledger()
    other.observe(record | {"rows": {"bob": {"cells": cells, "retired": True, "seconds": 0}}})
    assert not getattr(other.workshop("harvard-2026"), "retired", {})  # bob is no target


async def test_a_runs_steps_are_on_the_audit_log_as_they_end(site, server, waits):
    """#855: a month-end run writes each row's step as it ends, as a `!retire` does
    (`retire_step`), and the ledger marks from them in place: alice and bob uninstalled,
    synced and retired; its `apply_end` is the summary."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    await look(site, server, ENDS + 60)
    run = next(r["plan"] for r in records(site, "apply_start") if r.get("schedule"))
    steps = [(r["row"], r["stage"], r["cell"]) for r in records(site, "retire_step")]
    chain = (*FIRST, "teardown", "bots")
    assert steps == [*((row, s, "ok") for row in ("alice", "bob") for s in FIRST),
                     *((row, s, "ok") for row in ("alice", "bob") for s in chain[6:])]  # fmt: skip
    assert all(r["plan"] == run for r in records(site, "retire_step"))
    assert set(server.ledger.workshop("harvard-2026").retired) == {"alice", "bob"}


async def test_a_hard_kill_in_the_wait_resumes_after_the_sync_the_log_has(site, monkeypatch):
    """#855: a real run, killed hard (no `apply_end`: `Runner._end` never runs) while it
    waits before alice's teardown. Her first pass is on the log, so the ledger has her sync,
    and the resume at the next start runs her teardown and bots alone, with no `redone`."""
    import asyncio

    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    waiting = asyncio.Event()

    async def held(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    async def killed(self, *args, **kwargs):  # the process gone: no `apply_end`
        return False

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(held))
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    srv = started()
    await srv.start()
    try:
        await scheduled(site, srv, roster)
        await look(site, srv, ENDS - NOTICE_S + 60)
        monkeypatch.setattr(Runner, "_end", killed)  # the run's, from here
        site.clock.now, srv.planner.month_end_next = ENDS + 60, 0.0
        await srv.planner.tick()
        await asyncio.wait_for(waiting.wait(), 60)
    finally:
        await srv.close()
    monkeypatch.undo()
    assert not [r for r in records(site, "apply_end") if r["verb"] == "retire"]
    first = len(ran(site))

    async def wait(seconds: float) -> None:
        site.clock.now += seconds

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(wait))
    srv = started()
    await srv.start()
    try:
        assert srv.ledger.workshop("harvard-2026").ending()["alice"].counts
        await look(site, srv, ENDS + 1000)
        assert [s for r, s in ran(site)[first:] if r == "alice"] == [
            "aws/teardown.py", "admin/zulip_provision.py"
        ]  # fmt: skip
        assert not [r for r in records(site, "month_end") if r.get("redone")]
        assert states(srv)["alice"][0] == monthend.DONE
        assert "alice" in srv.ledger.workshop("harvard-2026").retired
    finally:
        await srv.close()


async def test_a_run_whose_steps_the_log_lacks_stops_its_row(site, server, waits):
    """#855: every step of alice's run in `state.db`, but none on the audit log (a run of a
    development build between #835 and #860, before step records, which no release shipped):
    the ledger doesn't have her retired, and the look stops her, saying so and what ends her,
    rather than calling her done."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    plan = await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    for stage in (*FIRST, "teardown", "bots"):
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 1)
    await look(site, server, ENDS + 600)
    assert states(server)["alice"] == (monthend.STOPPED, monthend.UNLOGGED)
    line = next(p for p in posts(site) if p.startswith(f"📨 **P-{plan}**, the month end: stopped"))
    assert "`!retire <row> unsynced` ends each: alice." in line.split("\n")[0], line
    assert not [s for r, s in ran(site) if r == "alice"]


async def test_a_step_whose_record_cant_be_written_is_never_the_resumes(
    site, server, waits, monkeypatch
):
    """#855: a run's step record that can't be written stops its row, and the resume's own
    record of the step (`month_end_steps`) is never written ahead of the log: alice's uninstall
    is not hers to skip."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    real = server.audit

    async def audit(event, **fields):
        if event == "retire_step" and fields.get("stage") == "uninstall":
            raise OSError("no space left on the device")
        return await real(event, **fields)

    monkeypatch.setattr(server, "audit", audit)
    await look(site, server, ENDS + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert "uninstall" not in monthend.done(server.store.db, alice)
    assert {"start", "idle-stop", "waker"} <= set(monthend.done(server.store.db, alice))
    assert states(server)["alice"][0] == monthend.STOPPED


# ------------------------------------------------------------ #860's review, round 1


@pytest.mark.parametrize("cut", ["service stop", "hard kill"])
async def test_a_cut_between_the_bots_record_and_the_state_db_is_done(site, monkeypatch, cut):
    """B1: the run cut right after alice's bots step is on the log, before `state.db` has it.
    The ledger has her retired: the next look marks her done, with her line, never dropped
    as "retired already"."""
    import asyncio

    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    async def wait(seconds: float) -> None:
        site.clock.now += seconds

    real = RunHooks.after

    def after(self, step_, ran_):
        if step_.stage == "bots":  # on the log already: the cut comes here
            raise asyncio.CancelledError
        real(self, step_, ran_)

    async def killed(self, *args, **kwargs):  # the process gone: no `apply_end`
        return False

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(wait))
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    srv = started()
    await srv.start()
    try:
        plan = await scheduled(site, srv, roster)
        await look(site, srv, ENDS - NOTICE_S + 60)
        monkeypatch.setattr(RunHooks, "after", after)
        if cut == "hard kill":
            monkeypatch.setattr(Runner, "_end", killed)
        site.clock.now, srv.planner.month_end_next = ENDS + 60, 0.0
        await srv.planner.tick()
        for _ in range(200):
            if any(r.get("stage") == "bots" for r in records(site, "retire_step")):
                break
            await asyncio.sleep(0.05)
    finally:
        await srv.close()
    monkeypatch.undo()
    monkeypatch.setattr(RunHooks, "sleep", staticmethod(wait))
    srv = started()
    await srv.start()
    try:
        (alice,) = monthend.entries(srv.store.db, "harvard-2026")
        assert "teardown" in monthend.done(srv.store.db, alice)
        assert "bots" not in monthend.done(srv.store.db, alice)  # `state.db` never had it
        assert "alice" in srv.ledger.workshop("harvard-2026").retired
        first = len(ran(site))
        await look(site, srv, ENDS + 1000)
        assert states(srv)["alice"] == (monthend.DONE, "retired")
        assert len(ran(site)) == first  # nothing ran again
        line = next(p for p in posts(site)
                    if p.startswith(f"📨 **P-{plan}**, the month end: marked retired"))  # fmt: skip
        assert line.split("\n")[0].endswith(": alice."), line
        assert not [p for p in posts(site) if "retired already" in p]
    finally:
        await srv.close()


async def test_a_run_stopped_by_its_log_says_where_each_row_was_left(
    site, server, waits, monkeypatch
):
    """B2 and N2: alice's teardown record can't be written, which stops the run. Her line
    says her VM may be gone; bob, his first pass done and his protection off, not torn down,
    is said so, with what to send, and his note says where he was left."""
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    real = server.audit

    async def audit(event, **fields):
        if event == "retire_step" and (fields.get("row"), fields.get("stage")) == (
            "alice", "teardown"
        ):  # fmt: skip
            raise OSError("no space left on the device")
        return await real(event, **fields)

    monkeypatch.setattr(server, "audit", audit)
    await look(site, server, ENDS + 60)
    result = next(p for p in posts(site) if p.startswith("❌ **P-") and "!retire alice bob" in p)
    assert "- alice stopped at teardown: its VM may be gone. Send `!retire alice` again; if its " \
           "plan finds no VM, `!retire alice unsynced`." in result, result  # fmt: skip
    assert "failed: may not be on the audit log" in result
    assert (
        "- bob left before teardown, the run having stopped: its VM runs, synced, its "
        "termination protection off. Send `!retire bob` again, or `!protect on bob` to keep "
        "the VM."
    ) in result, result
    assert states(server)["bob"][1].startswith("left before teardown in P-")
    assert "not reached" not in result


async def test_a_row_cut_before_its_uninstall_is_not_said_to_sync_again(site, server, waits):
    """N1: alice's run cut after her idle stop off; the resume does her first pass again, but
    she was never uninstalled nor synced: no "uninstalled and synced again" line."""
    roster = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")
    await scheduled(site, server, roster)
    await look(site, server, ENDS - NOTICE_S + 60)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.begin_run(server.store.db, 77, alice.plan, [alice])
    for stage in ("start", "idle-stop"):
        monthend.record(server.store.db, alice, stage, 77, ENDS, ENDS + 1)
    await look(site, server, ENDS + 60)
    assert not [p for p in posts(site) if "uninstalled and synced again" in p]
    assert states(server)["alice"][0] == monthend.DONE


# ------------------------------------------------- #868: a `!keep-until` during a run


async def test_a_keep_until_during_a_run_says_its_row_is_ending_now(site, server, monkeypatch):
    """Review of #871, B1: `!keep-until` of a row in the run applying, sent while the run waits
    before its teardowns. Its plan says the row is ending now, in that run, and that
    `!month-end cancel` stops it, never that the date drops it out; its approval is answered
    `busy:`, and the run retires the row."""
    import asyncio

    from .test_admin_keep_until import real_script

    real_script(site)
    await scheduled(site, server)
    await look(site, server, ENDS - NOTICE_S + 60)
    seen: dict[str, str] = {}

    async def wait(seconds: float) -> None:
        if not seen:
            run = server.planner.busy("harvard-2026")
            seen["run"] = f"P-{run}"
            seen["states"] = repr(states(server))
            bot = bot_of(server, HARVARD)
            await bot.on_message(incoming(OLGA, "!keep-until 2027-03-01 alice", mid=7000))
            for _ in range(500):  # its dry run, in a thread: the run's task can't be settled
                await asyncio.sleep(0.01)
                if (plan := server.store.get(run + 1)) is not None and plan.state == PENDING:
                    break
            seen["plan"] = next(p for p in posts(site) if "**Kept until " in p)
            await bot.on_message(incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=7001))
            seen["approval"] = posts(site)[-1]
        site.clock.now += seconds

    monkeypatch.setattr(RunHooks, "sleep", staticmethod(wait))
    await look(site, server, ENDS + 60)
    run = seen["run"]
    assert seen["states"] == repr({"alice": ("running", run), "bob": ("running", run)})
    assert (
        f"**Ending now**, in the month-end run {run}: alice. This plan can't apply until that "
        "run ends, and the run goes on with it: `!month-end cancel alice` stops it before its "
        "next step (ORGANIZERS §5.7)."
    ) in seen["plan"]
    assert "drops alice out" not in seen["plan"] and "won't end" not in seen["plan"]
    assert seen["approval"].startswith(f"busy: {run} is applying.")
    assert states(server)["alice"] == (monthend.DONE, f"retired by {run}")
