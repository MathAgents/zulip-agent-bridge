"""#883 (ADMIN §11.5, ORGANIZERS §5.7): a warning that Zulip refused at every try stops its row
before any step, which stays in service, said in the schedule's topic with the commands that
end it; a restart keeps it stopped; a new schedule tries the warning again. A row with no
Zulip ID is named in the schedule's plan, whose approval is its consent: it runs at its end.
Through the bot, with `test_admin_retire`'s fake scripts and the fake realm, where a
deactivated user's DMs are refused with a 400, as Zulip refuses them."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from zab.admin import monthend
from zab.admin.monthend import NOTICE, NOTICE_S, REFUSED, REMINDER, REMINDER_S, SENT, TRIES
from zab.admin.plans import DONE

from .botkit import DREW, HARVARD, OLGA
from .test_admin_month_end_runs import ENDS, ROSTER, entry, look, ran, scheduled, states, waits
from .test_admin_month_end_warnings import answering
from .test_admin_retire import LATE, server, site
from .test_admin_runner import approve, posts, records, request, settle

assert site and server and waits  # fixtures, used by name

STOPPED_NOTE = (
    "Zulip refused its {kind} at every try: `!retire alice` ends it by hand, or "
    "`!keep-until <date> alice`, then `!month-end schedule`, gives it a new end"
)


def lines(site) -> list[str]:
    """The look's lines in the schedule's topic."""
    return [p for p in posts(site) if p.startswith("📨 ")]


def retires(site) -> list[dict]:
    """The runs that started: their `apply_start` records."""
    return [r for r in records(site, "apply_start") if r["verb"] == "retire"]


def restartable(site):
    """A server of `site` with the month-end's planner, made afresh at each call: a restart."""
    from zab.admin.planning import Planning
    from zab.admin.runner import Runner

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(Runner(srv, vms=lookup, tagged=lookup))
        return srv

    return started


ALONE = ROSTER.replace("MathAgents/bob,2027-02-15", "MathAgents/bob,")  # alice's row alone


async def refused_reminder(site, srv) -> None:
    """alice's notice goes; then, deactivated, her reminder is refused at every try."""
    await scheduled(site, srv, ALONE)
    await look(site, srv, ENDS - NOTICE_S + 60)
    site.realm.deactivated.add(2001)
    for minute in range(1, TRIES + 1):
        await look(site, srv, ENDS - REMINDER_S + 60 * minute)


async def refused_notice(site, srv) -> int:
    """alice deactivated: her notice refused at `TRIES` looks, a minute apart; bob's goes at
    the next, the look's DMs ending at each failure (review of #829, B3). The schedule."""
    schedule = await scheduled(site, srv)
    site.realm.deactivated.add(2001)
    for minute in range(1, TRIES + 2):
        await look(site, srv, ENDS - NOTICE_S + 60 * minute)
    return schedule


async def test_a_refused_notice_stops_its_row_and_nothing_of_it_is_torn_down(site, server, waits):
    schedule = await refused_notice(site, server)
    alice, bob = monthend.entries(server.store.db, "harvard-2026")
    assert (alice.state, alice.note) == (monthend.STOPPED, STOPPED_NOTE.format(kind=NOTICE))
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, REFUSED)]
    assert [(k, h) for k, _, h in bob.warned] == [(NOTICE, SENT)]
    said = next(line for line in lines(site) if "refused" in line)
    assert said.startswith(
        f"📨 **P-{schedule}**, the month end: Zulip refused the notice, at every try, to alice, "
        "whose row stopped before any step and stays in service: `!retire alice` ends it by "
        "hand, or `!keep-until <date> alice`, then `!month-end schedule`, gives it a new end. "
        "Tell those participants yourselves."
    ), said
    (record,) = [r for r in records(site, "month_end") if "notice refused" in r.get("rows", {})]
    assert record["rows"] == {"notice refused": ["alice"]} and record["targets"] == ["alice"]
    await look(site, server, ENDS + 60)  # the end: bob's run, without alice
    (run,) = retires(site)
    assert run["args"] == ["bob"]
    assert [s for r, s in ran(site) if r == "alice"] == []  # not started, synced, unprotected
    assert states(server)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=NOTICE))
    assert states(server)["bob"][0] == monthend.DONE
    assert set(server.ledger.workshop("harvard-2026").retired) == {"bob"}
    await look(site, server, ENDS + 3 * 86400)  # never later either
    assert len(retires(site)) == 1


async def test_a_refused_reminder_stops_its_row(site, server, waits):
    await refused_reminder(site, server)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert (alice.state, alice.note) == (monthend.STOPPED, STOPPED_NOTE.format(kind=REMINDER))
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT), (REMINDER, REFUSED)]
    assert "Zulip refused the reminder, at every try, to alice, whose row" in lines(site)[-1]
    await look(site, server, ENDS + 60)
    assert retires(site) == [] and [s for r, s in ran(site) if r == "alice"] == []


async def test_a_reminder_refused_with_its_end_come_before_its_next_try_stops_its_row(site, waits):
    """Its retries cut short by the end, after which no reminder is due: the refusal counts.
    It is on record, owed, from its first try, so a restart in between changes nothing, and
    its row can't run meanwhile (Codex round 1)."""
    started = restartable(site)
    srv = started()
    await srv.start()
    try:
        await scheduled(site, srv, ALONE)
        await look(site, srv, ENDS - NOTICE_S + 60)
        site.realm.deactivated.add(2001)
        await look(site, srv, ENDS - 30)  # the bot down for the day: its first try, refused
        (alice,) = monthend.entries(srv.store.db, "harvard-2026")
        assert alice.state == monthend.SCHEDULED and monthend.run_at(alice) is None
        assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT), (REMINDER, REFUSED)]
        assert monthend.due(alice, ENDS - 10) == REMINDER  # owed: due again until the end
    finally:
        await srv.close()
    srv = started()  # the tries in memory forgotten
    await srv.start()
    try:
        await look(site, srv, ENDS + 30)
        assert states(srv)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=REMINDER))
        assert retires(site) == [] and [s for r, s in ran(site) if r == "alice"] == []
    finally:
        await srv.close()


async def test_a_reminder_refused_then_unreached_stays_owed(site, server, waits, monkeypatch):
    """Review of #908, N1: a try that never reached Zulip (no connection, its rate limit) counts
    for nothing, and keeps a reminder refused at an earlier try on record as refused, owed
    (`Planning._warn`). Taken back, the notice alone on record, its row would run at its end
    with no reminder delivered."""
    await scheduled(site, server, ALONE)
    sent = answering(monkeypatch, server, (111, ""), (None, REFUSED), (None, monthend.UNREACHED))
    await look(site, server, ENDS - NOTICE_S + 60)
    await look(site, server, ENDS - REMINDER_S + 60)  # refused at its first try
    await look(site, server, ENDS - 60)  # never reaching Zulip: still owed
    assert len(sent) == 3  # that try was made
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT), (REMINDER, REFUSED)]
    assert alice.state == monthend.SCHEDULED and monthend.run_at(alice) is None
    await look(site, server, ENDS + 60)  # the end come, still refused: stopped
    assert states(server)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=REMINDER))
    assert retires(site) == [] and [s for r, s in ran(site) if r == "alice"] == []


async def test_an_unreached_reminder_never_refused_is_not_owed(site, server, waits, monkeypatch):
    """The other half of that arm: a reminder that never reached Zulip, and was never refused,
    is taken back, not recorded refused, so that a connection blip at its last try before the
    end doesn't stop a row whose notice went: it runs at its end."""
    await scheduled(site, server, ALONE)
    sent = answering(monkeypatch, server, (111, ""), (None, monthend.UNREACHED))
    await look(site, server, ENDS - NOTICE_S + 60)
    await look(site, server, ENDS - 60)  # the reminder's only try, unreached
    assert len(sent) == 2  # that try was made
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT)]
    await look(site, server, ENDS + 60)
    assert states(server)["alice"][0] != monthend.STOPPED
    assert [r["args"] for r in retires(site)] == [["alice"]]


async def rescheduled(site, srv, at: float):
    """A new `!month-end schedule` of the same rows and ends, requested at `at` and approved a
    minute later: alice's entry then."""
    site.clock.now = at
    again = await request(srv, "!month-end schedule", mid=7000)
    site.clock.now = at + 60
    await approve(srv, again, sender=DREW, phrase="month-end 1", mid=LATE + 2)
    await settle(srv)
    assert srv.store.get(again.id).state == DONE, srv.store.get(again.id).note
    (alice,) = monthend.entries(srv.store.db, "harvard-2026")
    assert alice.plan == again.id
    return alice


async def test_a_refused_reminder_rescheduled_at_its_end_keeps_its_row_stopped(site, server, waits):
    """Codex round 1: a new schedule of the same end, approved a minute before it, with no look
    in that minute to try the reminder again: it is carried over refused, so the row can't
    run, and the first look after the end stops it again, nothing of it run. (Approved after
    the end, the plan is STALE: its rebuild refuses an end that has passed.)"""
    await refused_reminder(site, server)
    alice = await rescheduled(site, server, ENDS - 120)
    assert alice.state == monthend.SCHEDULED and monthend.run_at(alice) is None
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT), (REMINDER, REFUSED)]
    await look(site, server, ENDS + 30)
    assert states(server)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=REMINDER))
    assert "Zulip refused the reminder, at every try, to alice" in lines(site)[-1]
    await look(site, server, ENDS + 3 * 86400)
    assert retires(site) == [] and [s for r, s in ran(site) if r == "alice"] == []
    assert states(server)["alice"][0] == monthend.STOPPED


async def test_a_refused_reminder_rescheduled_before_its_end_is_tried_again(site, server, waits):
    """Before the end, the new schedule's row is scheduled, its reminder still refused, so it
    can't run, and due again: delivered, the row runs at its end."""
    await refused_reminder(site, server)
    alice = await rescheduled(site, server, ENDS - 7200)
    assert alice.state == monthend.SCHEDULED and monthend.run_at(alice) is None
    site.realm.deactivated.discard(2001)
    await look(site, server, ENDS - 3600)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT), (REMINDER, SENT)]
    await look(site, server, ENDS + 60)
    assert retires(site)[-1]["args"] == ["alice"]


async def test_a_restart_between_a_refusal_and_the_run_keeps_the_row_stopped(site, waits):
    started = restartable(site)
    srv = started()
    await srv.start()
    try:
        await refused_notice(site, srv)
    finally:
        await srv.close()
    srv = started()  # no tries in memory: the record is the state's
    await srv.start()
    try:
        await look(site, srv, ENDS + 60)
        assert retires(site)[-1]["args"] == ["bob"]
        assert states(srv)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=NOTICE))
        assert [s for r, s in ran(site) if r == "alice"] == []
    finally:
        await srv.close()


async def test_a_new_schedule_tries_a_refused_notice_again(site, server, waits):
    """`store` carries no refused warning over, and the new approval has tries of its own."""
    await refused_notice(site, server)
    site.realm.deactivated.discard(2001)  # reactivated, say
    again = await request(server, "!month-end schedule", mid=7000)
    await approve(server, again, sender=DREW, phrase="month-end 2", mid=LATE + 2)
    await settle(server)
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert (alice.plan, alice.state, alice.warned) == (again.id, monthend.SCHEDULED, ())
    await look(site, server, ENDS - NOTICE_S + 60 * (TRIES + 3))
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT)]
    assert monthend.run_at(alice) == ENDS


async def test_a_new_approval_gets_tries_of_its_own(site, server, waits):
    """Review of #908, N2: `TRIES` of them, not what the old schedule's tries left
    (`Planning._key` holds the schedule's plan): refused again, the row stops only at the last."""
    await refused_notice(site, server)  # alice still deactivated
    again = await request(server, "!month-end schedule", mid=7000)
    await approve(server, again, sender=DREW, phrase="month-end 2", mid=LATE + 2)
    await settle(server)
    first = ENDS - NOTICE_S + 60 * (TRIES + 3)
    for n in range(TRIES - 1):
        await look(site, server, first + 60 * n)
        alice = monthend.entries(server.store.db, "harvard-2026")[0]
        assert (alice.plan, alice.state, alice.warned) == (again.id, monthend.SCHEDULED, ())
    await look(site, server, first + 60 * (TRIES - 1))
    assert states(server)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=NOTICE))


async def test_a_refusal_recorded_on_a_scheduled_row_stops_it_at_the_next_look(site, server):
    """A release before #883 recorded a refused notice and kept its row scheduled: it never
    runs (`run_at`), and the next look stops it, saying so."""
    await scheduled(site, server, ALONE)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    monthend.warn(server.store.db, alice, NOTICE, REFUSED, ENDS - NOTICE_S)
    (alice,) = monthend.entries(server.store.db, "harvard-2026")
    assert monthend.refused(alice, ENDS - NOTICE_S) == NOTICE and monthend.run_at(alice) is None
    await look(site, server, ENDS + 60)
    assert states(server)["alice"] == (monthend.STOPPED, STOPPED_NOTE.format(kind=NOTICE))
    assert "Zulip refused the notice, at every try, to alice" in lines(site)[-1]
    assert retires(site) == []


def test_a_refusal_stops_only_a_row_still_scheduled_for_its_end_and_recipient():
    db = sqlite3.connect(":memory:")
    monthend.setup(db)
    alice = entry()
    db.execute(
        "INSERT INTO month_end VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alice.workshop, alice.slug, alice.plan, alice.keep_until, alice.ends, alice.vms,
         alice.tagged, "[]", alice.zulip_user_id, "[]", alice.state, "", 0.0),
    )  # fmt: skip
    assert not monthend.refuse(db, entry(zulip_user_id="2009"), NOTICE, ENDS)  # another one
    monthend.cancel(db, "harvard-2026", ["alice"], "by Olga", ENDS)
    assert not monthend.refuse(db, alice, NOTICE, ENDS + 1)  # cancelled meanwhile
    (now,) = monthend.entries(db, "harvard-2026")
    assert (now.state, now.note) == (monthend.CANCELLED, "by Olga")
    assert [(k, h) for k, _, h in now.warned] == [(NOTICE, REFUSED)]  # its outcome kept


async def test_a_row_with_no_zulip_id_is_named_in_the_plan_and_runs_at_its_end(site, server, waits):
    await scheduled(site, server, ROSTER.replace(",2002,", ",,"))  # bob: no Zulip ID
    plan = next(p for p in posts(site) if "**The month end** of 2 rows" in p)
    note = (
        "**No Zulip ID in the roster, so no warning can reach them: approving this plan ends this "
        "row at its date all the same, with no notice or reminder.** Tell its participant "
        "yourselves, or, for the bot to warn them, bind them first (`!provision zulip`), then "
        "send `!month-end schedule` again. The row: bob."
    )
    assert note in plan and plan.index(note) < plan.index("**The month end** of 2 rows")
    await look(site, server, ENDS - NOTICE_S + 60)
    assert "no notice could go, with no Zulip ID in the roster, to bob, whose VM ends at its " \
        "date all the same, as approved." in lines(site)[-1]  # fmt: skip
    await look(site, server, ENDS + 60)
    assert retires(site)[-1]["args"] == ["alice", "bob"]
    assert states(server)["bob"][0] == monthend.DONE


async def test_the_plan_names_every_row_with_no_zulip_id(tmp_path):
    """Every one, not the first five, and first (#883): what the approvers consent to."""
    import dataclasses

    from zab.admin.planning import Planning

    @dataclasses.dataclass(frozen=True)
    class Planned:  # the fields of an `Action` that `Planning._noted` reads
        workshop: str
        targets: tuple[str, ...]
        notes: tuple[str, ...] = ()

    slugs = tuple(f"p{n}" for n in range(7))
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,zulip_user_id\n" + "".join(f"{s},\n" for s in slugs))
    db = sqlite3.connect(":memory:")
    monthend.setup(db)
    planner = SimpleNamespace(server=SimpleNamespace(store=SimpleNamespace(db=db)))
    fleet = SimpleNamespace(workshop=SimpleNamespace(roster=str(roster)))
    action = Planned("harvard-2026", slugs, ("**The month end** of 7 rows",))
    noted = await Planning._noted(planner, action, fleet)  # type: ignore[arg-type]
    assert noted.notes[0].startswith(
        "**No Zulip ID in the roster, so no warning can reach them: approving this plan ends "
        "each of these rows at its date all the same, with no notice or reminder.** Tell their "
        "participants yourselves"
    ), noted.notes[0]
    assert noted.notes[0].endswith(" The rows (7): p0, p1, p2, p3, p4, p5, p6."), noted.notes[0]
    assert noted.notes[1:] == ("**The month end** of 7 rows",)


async def test_a_plan_cut_to_fit_says_first_what_approving_does_to_rows_with_no_zulip_id(
    tmp_path,
):
    """Review of #908, N5: the note comes before its list of rows, which a plan cut to fit cuts
    short, the plan's notes given a quarter of its room (`plan_message`); the attachment has
    them all."""
    from zab.admin import planning
    from zab.admin.actions import TARGETS, Action
    from zab.admin.approvals import Requirement
    from zab.admin.audit import Head
    from zab.admin.plans import Plan

    slugs = tuple(f"participant-{n:03d}" for n in range(300))
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,zulip_user_id\n" + "".join(f"{s},\n" for s in slugs))
    db = sqlite3.connect(":memory:")
    monthend.setup(db)
    planner = SimpleNamespace(server=SimpleNamespace(store=SimpleNamespace(db=db)))
    fleet = SimpleNamespace(workshop=SimpleNamespace(roster=str(roster)))
    action = Action("month-end", "harvard-2026", HARVARD, ("schedule",), (), TARGETS, slugs, (),
                    notes=("**The month end** of 300 rows",))  # fmt: skip
    noted = await planning.Planning._noted(planner, action, fleet)  # type: ignore[arg-type]
    plan = Plan(7, "harvard-2026", HARVARD, OLGA, "!month-end schedule", "planning", 0.0, None,
                (), "", None, None, None, "", None)  # fmt: skip
    message = planning.plan_message(
        plan, noted, "ab" * 32, Requirement("change"), requester="Olga", expires=0.0,
        output="", head=Head(3, "c" * 64), limit=10000,
    )  # fmt: skip
    note = noted.notes[0]
    said = note[: note.index("The rows (300): participant-000, ")]
    assert "approving this plan ends each of these rows at its date all the same" in said
    assert message.full is not None and planning.ATTACH in message.text  # cut
    assert f"{said}The rows (300): participant-000, " in message.text
    assert "participant-299" not in message.text and note in message.full
