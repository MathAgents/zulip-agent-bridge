"""The month-end schedule (ADMIN §11.5, #570): `!month-end schedule`, `show` and `cancel`.

The grammar of the three; the schedule's action (every row in service with a `keep_until`, its
refusals, its hash and its class); the store in `state.db` (`zab.admin.monthend`): a new schedule
replaces the old, keeping a warning sent for an unchanged end; and the whole chain through the
bot, with `test_admin_retire`'s fake scripts: plan, approval with its phrase, apply, `show`, and
the veto, `cancel`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3

import pytest

from zab.admin import monthend
from zab.admin import runner as runner_mod
from zab.admin.actions import (
    BOT_KINDS,
    MONTH_END,
    MONTH_END_HOW,
    RETIRE,
    VERBS,
    ActionError,
    Live,
    Row,
    build,
    digests,
)
from zab.admin.approvals import DESTRUCTIVE, requirement
from zab.admin.grammar import Command, Rejected, Selection, keep_until_end, parse
from zab.admin.plans import DONE, PENDING, REFUSED

from .botkit import ALICE, DREW, OLGA, incoming
from .test_admin_actions import CONFIG, FLEET, OUT, PY, TAG, R, context, layout
from .test_admin_retire import LATE, server, site
from .test_admin_runner import approve, bot_of, posts, records, request, settle

assert site and server and layout  # fixtures, used by name

#: bob and dave kept, ending at two dates; alice ends the day after the workshop; carol has no VM
KEPT = dataclasses.replace(
    FLEET,
    rows=(
        Row("alice", "198.51.100.1", "us-west-2"),
        Row("bob", "198.51.100.2", keep_until="2026-12-01"),
        Row("carol"),
        Row("dave", "198.51.100.4", "us-west-2", keep_until="2026-11-30T09:30"),
    ),
)
TEARDOWN = f"{PY}/aws/teardown.py {R} {TAG} --region us-west-2 --spares-file {OUT}/spares.json"


# ------------------------------------------------------------------------------ the grammar


def test_the_grammar_reads_the_three_and_nothing_else():
    ctx = context(FLEET)
    assert parse("!month-end schedule", ctx) == Command(MONTH_END)
    assert parse("!month-end show", ctx) == Command("month-end show")
    assert parse("!month-end cancel all", ctx) == Command(
        "month-end cancel", selection=Selection(all=True)
    )
    for message in ("!month-end", "!month-end schedule bob", "!month-end show bob",
                    "!month-end cancel", "!month-end run"):  # fmt: skip
        assert isinstance(parse(message, ctx), Rejected), message


# ------------------------------------------------------------------------------ the action


def test_the_schedule_takes_every_row_in_service_with_an_end(layout):
    action = build(Command(MONTH_END), KEPT, layout=layout)
    assert action.targets == ("bob", "dave")  # in roster order
    assert VERBS[MONTH_END].hashed == RETIRE  # each row's VMs and the teardown's dry run
    (step,) = action.steps
    assert step.plan_only and step.argv == step.dry_run
    assert " ".join(step.argv) == f"{TEARDOWN} --dry-run --only bob --only dave"
    assert not action.applied  # its apply runs no script: it stores the schedule
    bots = [f"{OUT}/{s}/{k}.zuliprc" for s in ("bob", "dave") for k in BOT_KINDS]
    # the ends, the bots' keys, and spares.json, which the teardown's dry run reads
    assert action.inputs == (KEPT.workshop.roster, *bots, f"{OUT}/spares.json")
    needs = requirement(action, KEPT, CONFIG)
    assert (needs.klass, needs.phrase) == (DESTRUCTIVE, ("month-end", "2"))
    notes = "\n".join(action.notes)
    assert "- bob: ends Tuesday, December 1, 2026, 15:00 UTC; no bots" in notes
    assert "- dave: ends Monday, November 30, 2026, 09:30 UTC; no bots" in notes
    assert notes.endswith(MONTH_END_HOW)
    left = dataclasses.replace(
        KEPT, retired={"bob": "P-50 at 2026-11-06 14:02 UTC"},
        rows=(*KEPT.rows[:2], dataclasses.replace(KEPT.rows[2], excluded=True), *KEPT.rows[3:]),
    )  # fmt: skip
    assert build(Command(MONTH_END), left, layout=layout).targets == ("dave",)


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        ((Row("bob", "198.51.100.2"),), "no row in service has a keep_until: give the kept rows"),
        ((Row("bob", "198.51.100.2", keep_until="2026-11-05T11:59"),),
         r"^bob: its keep_until has passed: `!retire bob` ends it"),
        ((Row("bob", "198.51.100.2", keep_until="2027-11-07"),),
         r"^bob: its keep_until is more than 366 days ahead: check the year"),
        ((Row("bob", "198.51.100.2", keep_until="30/11/2026"),),
         "bob: its keep_until is not a date like 2026-11-30"),
        ((Row("bob", keep_until="2026-12-01"),),
         r"^bob: no ip in the roster, so no VM to sync: `!retire bob unsynced` ends it"),
    ],
)  # fmt: skip
def test_the_schedule_refuses_an_end_passed_a_bad_one_or_a_row_without_a_vm(layout, rows, why):
    """`FLEET.read_at` is noon UTC on Thursday, November 5, 2026."""
    fleet = dataclasses.replace(KEPT, rows=rows)
    with pytest.raises(ActionError, match=why):
        build(Command(MONTH_END), fleet, layout=layout)
    with pytest.raises(ActionError, match="the bot's clock is not known"):
        build(Command(MONTH_END), dataclasses.replace(KEPT, read_at=None), layout=layout)


def test_a_recent_protect_doesnt_hold_the_schedule_back(layout):
    """It tears nothing down at once, unlike `!retire` (#378's guard)."""
    from zab.admin.actions import Protection

    recent = dataclasses.replace(KEPT, protection=Protection("P-41", "protect", KEPT.read_at - 60))
    assert build(Command(MONTH_END), recent, layout=layout).targets == ("bob", "dave")
    with pytest.raises(ActionError, match="no !teardown or !retire for 5 minutes"):
        build(Command("retire", selection=Selection(slugs=("bob",))), recent, layout=layout)


# ------------------------------------------------------------------------------- the store


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    monthend.setup(conn)
    yield conn
    conn.close()


def stored(db, layout, fleet=KEPT, plan=50, vms=None, now=1.0, keys=True):
    action = build(Command(MONTH_END), fleet, layout=layout)
    live = Live(
        vms=vms or {"bob": "i-2", "dave": "i-4"}, tagged=vms or {"bob": "i-2", "dave": "i-4"}
    )
    fake = {path: "ab" * 32 for path in action.inputs if path.endswith("/claude.zuliprc")}
    return monthend.store(
        db, plan=plan, action=action, fleet=fleet, live=live,
        digests=digests(action) | (fake if keys else {}),
        people={"bob": "2002", "dave": ""}, now=now,
    )  # fmt: skip


def test_the_apply_stores_each_row_with_its_targets_and_no_release(db, layout):
    text = stored(db, layout)
    assert text.splitlines() == [
        "the month end of harvard-2026, as P-50 schedules it:",
        "bob: ends Tuesday, December 1, 2026, 15:00 UTC, VM i-2",  # in roster order
        "dave: ends Monday, November 30, 2026, 09:30 UTC, VM i-4",
    ]
    dave, bob = monthend.entries(db, "harvard-2026")  # by end
    assert (bob.slug, bob.plan, bob.keep_until, bob.ends) == (
        "bob",
        50,
        "2026-12-01",
        keep_until_end("2026-12-01"),
    )
    assert (bob.vms, bob.tagged, bob.zulip_user_id, bob.warned) == ("i-2", "i-2", "2002", ())
    assert bob.bots == (("claude", "ab" * 32), ("codex", ""))  # a missing key: ""
    assert (bob.state, bob.note, bob.changed) == (monthend.SCHEDULED, "", 1.0)
    assert dave.zulip_user_id == "" and monthend.entries(db, "zab-test") == []
    row = db.execute("SELECT * FROM month_end WHERE slug = 'bob'").fetchone()
    assert not any("v0." in str(value) for value in row)  # no release in it (§11.5)


def test_a_new_schedule_replaces_the_old_and_keeps_a_warning_for_an_unchanged_end(db, layout):
    stored(db, layout)
    with db:
        db.execute("UPDATE month_end SET warned = ?", (json.dumps([["notice", 1.5, "sent"]]),))
    later = dataclasses.replace(
        KEPT, rows=(*KEPT.rows[:3], dataclasses.replace(KEPT.rows[3], keep_until="2026-12-07"))
    )
    text = stored(db, layout, later, plan=51, now=2.0)
    bob, dave = monthend.entries(db, "harvard-2026")
    assert (bob.plan, bob.warned) == (51, (("notice", 1.5, "sent"),))  # the same end: sent already
    assert (dave.plan, dave.warned, dave.keep_until) == (51, (), "2026-12-07")  # a new end
    only_bob = dataclasses.replace(KEPT, rows=KEPT.rows[:3])
    text = stored(db, layout, only_bob, plan=52, vms={"bob": "i-2"})
    assert [e.slug for e in monthend.entries(db, "harvard-2026")] == ["bob"]
    assert text.endswith("no longer in the schedule: dave\n")


def test_cancel_takes_out_only_rows_the_schedule_would_still_end(db, layout):
    stored(db, layout)
    done, other = monthend.cancel(db, "harvard-2026", ["bob", "carol"], "by Olga", 5.0)
    assert (done, other) == (["bob"], ["carol"])
    bob = next(e for e in monthend.entries(db, "harvard-2026") if e.slug == "bob")
    assert (bob.state, bob.note, bob.changed) == (monthend.CANCELLED, "by Olga", 5.0)
    assert monthend.cancel(db, "harvard-2026", ["bob"], "again", 6.0) == ([], ["bob"])
    assert monthend.cancel(db, "zab-test", ["dave"], "elsewhere", 6.0) == ([], ["dave"])


def test_show_lists_the_schedule_then_the_dates_it_doesnt_hold(db, layout):
    assert monthend.show(db, FLEET)[0].startswith("No month end is scheduled in harvard-2026.\n")
    assert "No row in service has a `keep_until` either" in monthend.show(db, FLEET)[0]
    assert (
        "Kept until a date, not in the schedule: bob (2026-12-01), dave"
        in monthend.show(db, KEPT)[0]
    )
    stored(db, dataclasses.replace(layout), dataclasses.replace(KEPT, rows=KEPT.rows[:3]),
           vms={"bob": "i-2"})  # fmt: skip
    monthend.cancel(db, "harvard-2026", ["bob"], "by Olga Organizer, 2026-11-20 10:00 UTC", 5.0)
    text, whole = monthend.show(db, KEPT, 10_000)
    assert whole is None  # it fits
    shown = text.splitlines()
    assert shown == [
        "**The month end of harvard-2026**, as P-50 scheduled it:",
        "- bob: ends Tuesday, December 1, 2026, 15:00 UTC; cancelled (by Olga Organizer, "
        "2026-11-20 10:00 UTC)",
        "Kept until a date, not in the schedule: dave (2026-11-30T09:30). `!month-end schedule` "
        "schedules every row in service with a `keep_until`.",
    ]


#: The scale ADMIN §11 plans for: 170 kept VMs, here in one workshop, each slug 32 characters.
MANY = dataclasses.replace(
    FLEET,
    rows=tuple(
        Row(f"participant-of-a-long-name-{n:05d}", f"198.51.{n // 250}.{n % 250 + 1}",
            keep_until="2026-12-01")
        for n in range(170)
    ),
)  # fmt: skip


def test_show_at_scale_is_cut_to_fit_with_the_whole_to_attach(db, layout):
    """Codex round 2 on #824: 170 rows are over Zulip's 10,000 characters. The answer keeps its
    head, a count by state, the rows that fit, the line that says what is cut, where the
    attachment goes, and the notice; the whole of it is returned to attach."""
    vms = {row.slug: f"i-{n}" for n, row in enumerate(MANY.rows)}
    stored(db, layout, MANY, vms=vms, keys=False)  # as out/ has them: none
    monthend.cancel(db, "harvard-2026", [MANY.rows[-1].slug], "by Olga", 5.0)
    limit = 10_000 - 64  # Zulip's default, less the footer's room (reads.FOOTER_ROOM)
    text, whole = monthend.show(db, MANY, limit)
    assert whole is not None and len(whole) > 10_000
    assert len(text.replace(monthend.ATTACH, "x" * monthend.LINK_ROOM)) <= limit  # its link too
    lines = text.splitlines()
    assert lines[0] == "**The month end of harvard-2026**, as P-50 scheduled it:"
    assert lines[1] == "170 rows: 169 scheduled, 1 cancelled."
    shown = [line for line in lines if line.startswith("- ")]
    assert 50 < len(shown) < 170 and all(line in whole for line in shown)
    assert lines[-1] == (
        f"*Cut to fit: {len(shown)} of 170 rows shown. The whole schedule: {monthend.ATTACH}*"
    )
    assert whole.count("\n- ") == 170 and "cancelled (by Olga)" in whole
    assert monthend.show(db, MANY, len(whole)) == (whole, None)  # just fits: whole


# ---------------------------------------------------------------------- through the bot


def keep(site, **ends: str) -> None:
    """botkit's Harvard roster, with a `keep_until` per row named."""
    path = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    lines = path.read_text().splitlines()
    out = [lines[0] + ",keep_until"]
    out += [f"{line},{ends.get(line.split(',', 1)[0], '')}" for line in lines[1:]]
    path.write_text("\n".join(out) + "\n")


async def test_a_schedule_is_planned_approved_stored_shown_and_cancelled(site, server):
    """botkit's clock is Friday, January 15, 2027, 08:00 UTC; Olga and Oscar are Harvard's
    organizers, Drew a global admin."""
    keep(site, alice="2027-02-15", bob="2027-02-16T12:00")
    plan = await request(server, "!month-end schedule")
    assert plan.state == PENDING, plan.note
    message, approval = posts(site)[-2:]
    assert "**Commands:** none: this plan's apply runs no script." in message
    assert "- alice: ends Monday, February 15, 2027, 15:00 UTC" in message
    assert "terminate i-alice ws-alice (running, participant alice)" in message
    assert f"!approve {plan.name} {plan.hash[:6]} month-end 2" in approval
    await approve(server, plan, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    result = next(p for p in posts(site) if p.startswith(f"✅ **{plan.name}**"))
    assert "`!month-end schedule`: done in " in result and ": 2 rows scheduled" in result
    assert "bob: ends Tuesday, February 16, 2027, 12:00 UTC, VM i-2" in result
    alice, bob = monthend.entries(server.store.db, "harvard-2026")
    assert (alice.slug, alice.vms, alice.plan) == ("alice", "i-1", plan.id)
    assert bob.bots == (("claude", ""), ("codex", ""))
    await bot_of(server, plan.bot).on_message(incoming(OLGA, "!month-end show", mid=LATE + 10))
    assert "- bob: ends Tuesday, February 16, 2027, 12:00 UTC; scheduled" in posts(site)[-1]
    await bot_of(server, plan.bot).on_message(
        incoming(OLGA, "!month-end cancel bob carol", mid=LATE + 20)
    )
    said = posts(site)[-1]
    assert said.startswith("✅ The month end of bob is cancelled: the schedule ends none of them.")
    assert "Not in the schedule: carol." in said
    (cancel,) = records(site, "month_end")
    assert (cancel["what"], cancel["targets"], cancel["named"]) == (
        "cancel",
        ["bob"],
        ["bob", "carol"],
    )
    assert cancel["requester"]["id"] == OLGA
    assert [e.state for e in monthend.entries(server.store.db, "harvard-2026")] == [
        monthend.SCHEDULED,
        monthend.CANCELLED,
    ]
    await bot_of(server, plan.bot).on_message(
        incoming(ALICE, "!month-end cancel bob", mid=LATE + 30)
    )
    assert posts(site)[-1].startswith("❌ bob: not in the month-end schedule, or ended already")


async def test_a_cancel_waits_for_a_replacing_schedule_to_apply(site, server, monkeypatch):
    """Codex round 1 on #824: a cancel between a replacing schedule's approval and its store
    would be undone by the store. It is refused as busy until the apply ends, then taken."""
    keep(site, alice="2027-02-15", bob="2027-02-16")
    first = await request(server, "!month-end schedule")
    await approve(server, first, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    gate = asyncio.Event()
    store = monthend.store

    async def held(self, job, plan, live):  # the apply, held before it stores the schedule
        await gate.wait()
        return store(
            server.store.db, plan=plan.id, action=job.action, fleet=job.fleet, live=live,
            digests=digests(job.action), people={}, now=site.clock(),
        )  # fmt: skip

    monkeypatch.setattr(runner_mod.Runner, "_schedule", held)
    second = await request(server, "!month-end schedule", mid=6000)
    await approve(server, second, sender=DREW, phrase="month-end 2", mid=LATE + 2)
    bot = bot_of(server, second.bot)
    await bot.on_message(incoming(OLGA, "!month-end cancel bob", mid=LATE + 3))
    assert posts(site)[-1].startswith(
        f"busy: {second.name}, a `!month-end schedule`, is applying, and replaces the schedule. "
        "Nothing was cancelled"
    )
    assert records(site, "month_end") == []  # nothing recorded
    gate.set()
    await settle(server)
    assert server.store.get(second.id).state == DONE
    entries = monthend.entries(server.store.db, "harvard-2026")
    assert [(e.slug, e.plan, e.state) for e in entries] == [
        ("alice", second.id, monthend.SCHEDULED), ("bob", second.id, monthend.SCHEDULED)
    ]  # fmt: skip
    await bot.on_message(incoming(OLGA, "!month-end cancel bob", mid=LATE + 4))
    assert posts(site)[-1].startswith("✅ The month end of bob is cancelled")
    assert monthend.entries(server.store.db, "harvard-2026")[1].state == monthend.CANCELLED


async def test_show_at_scale_through_the_bot_attaches_the_whole(site, server):
    """Codex round 2 on #824, through the bot: the answer fits Zulip's limit with its footer,
    and the attachment holds every row."""
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    roster.write_text("slug,email,ip,region,keep_until\n" + "".join(
        f"{r.slug},{r.slug}@example.com,{r.ip},us-west-2,2027-02-15\n" for r in MANY.rows
    ))  # fmt: skip
    site.vms = {row.slug: f"i-{n}" for n, row in enumerate(MANY.rows)}
    plan = await request(server, "!month-end schedule")
    assert plan.state == PENDING, plan.note
    await approve(server, plan, sender=DREW, phrase="month-end 170", mid=LATE + 1)
    await settle(server)
    assert len(monthend.entries(server.store.db, "harvard-2026")) == 170
    await bot_of(server, plan.bot).on_message(incoming(OLGA, "!month-end show", mid=LATE + 10))
    answer = posts(site)[-1]
    assert len(answer) <= 10_000 and answer.endswith("`")  # whole: its footer last
    assert "of 170 rows shown. The whole schedule: [month-end.txt](/user_uploads/" in answer
    (attached,) = [v for k, v in site.realm.uploads.items() if k.endswith("month-end.txt")]
    assert attached.decode().count("\n- ") == 170


async def test_a_pending_schedule_is_named_by_a_cancel_and_a_past_end_is_refused(site, server):
    keep(site, alice="2027-02-15", bob="2027-02-16")
    first = await request(server, "!month-end schedule")
    await approve(server, first, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    second = await request(server, "!month-end schedule", mid=6000)
    assert second.state == PENDING
    await bot_of(server, first.bot).on_message(
        incoming(OLGA, "!month-end cancel alice", mid=LATE + 40)
    )
    assert (
        f"{second.name}, pending, would schedule them again: `!reject {second.name}`"
        in posts(site)[-1]
    )
    keep(site, alice="2027-01-15T07:59", bob="2027-02-16")
    refused = await request(server, "!month-end schedule", mid=7000)
    assert refused.state == REFUSED
    assert refused.note.startswith("alice: its keep_until has passed")


# ------------------------------------------------------------------ review of #824's notes


def test_show_says_a_scheduled_row_drops_out_once_it_no_longer_matches(db, layout):
    """N2: retired by hand, or its `keep_until` changed: `show` says so before the look drops
    it."""
    stored(db, layout, keys=False)
    moved = dataclasses.replace(
        KEPT, rows=(*KEPT.rows[:3], dataclasses.replace(KEPT.rows[3], keep_until="2026-12-07")),
        retired={"bob": "P-61 at 2026-11-20 10:00 UTC"},
    )  # fmt: skip
    text, _ = monthend.show(db, moved)
    assert (
        "- bob: ends Tuesday, December 1, 2026, 15:00 UTC; scheduled, but drops out at the "
        "bot's next look: retired already (P-61 at 2026-11-20 10:00 UTC)" in text
    )
    assert (
        "dave: ends Monday, November 30, 2026, 09:30 UTC; scheduled, but drops out at the "
        "bot's next look: its keep_until changed to 2026-12-07 since the approval" in text
    )
    assert (
        "- bob: ends Tuesday, December 1, 2026, 15:00 UTC; scheduled"
        in monthend.show(db, KEPT)[0].splitlines()
    )  # unchanged: scheduled, and nothing more


def test_counts_are_singular_for_one():
    """N8."""
    assert (monthend.counted(1), monthend.counted(170)) == ("1 row", "170 rows")


async def test_a_plan_names_the_cancelled_rows_it_brings_back(site, server):
    """N3: a new schedule takes a cancelled row again: its plan says so."""
    keep(site, alice="2027-02-15", bob="2027-02-16")
    first = await request(server, "!month-end schedule")
    await approve(server, first, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    bot = bot_of(server, first.bot)
    await bot.on_message(incoming(OLGA, "!month-end cancel bob", mid=LATE + 2))
    await request(server, "!month-end schedule", mid=6000)
    message = posts(site)[-2]
    assert "**Cancelled, and back with this plan:** bob (by Olga Organizer, " in message
    assert "Approving it schedules it again; to keep it out, first `!keep-until none bob`." in (
        message
    )


async def test_a_refused_cancel_is_audited(site, server, monkeypatch):
    """N5: as a refused `!reject` is, with the sender, a code and the reason; a cancel's own
    record names its rows as `targets`, which the console reads (the chain test above)."""
    keep(site, alice="2027-02-15", bob="2027-02-16")
    first = await request(server, "!month-end schedule")
    await approve(server, first, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    gate = asyncio.Event()
    real = runner_mod.Runner._schedule

    async def held(self, job, plan, live):
        await gate.wait()
        return await real(self, job, plan, live)

    monkeypatch.setattr(runner_mod.Runner, "_schedule", held)
    second = await request(server, "!month-end schedule", mid=6000)
    await approve(server, second, sender=DREW, phrase="month-end 2", mid=LATE + 2)
    await bot_of(server, first.bot).on_message(
        incoming(OLGA, "!month-end cancel bob", mid=LATE + 3)
    )
    answer = posts(site)[-1]
    gate.set()
    await settle(server)
    (refused,) = [r for r in records(site, "refused") if r.get("verb") == "month-end cancel"]
    assert (refused["code"], refused["sender"]["id"]) == ("busy", OLGA)
    assert refused["reason"].startswith(f"busy: {second.name}, a `!month-end schedule`")
    assert answer.startswith(f"busy: {second.name}") and "`audit #" in answer


async def test_an_interrupted_schedule_plan_points_to_show(site):
    """N6: a schedule plan cut short may have stored its schedule."""
    from zab.admin.planning import Planning
    from zab.admin.plans import APPLYING

    def lookup(fleet_, names):
        return {n: site.vms[n] for n in names if n in site.vms}

    def started():
        srv = site.server()
        srv.planner = Planning(runner_mod.Runner(srv, vms=lookup, tagged=lookup))
        return srv

    keep(site, alice="2027-02-15")
    srv = started()
    await srv.start()
    try:
        plan = await request(srv, "!month-end schedule")
        assert plan.state == PENDING, plan.note
        srv.store.move(plan.id, (PENDING,), APPLYING)
    finally:
        await srv.close()
    srv = started()
    await srv.start()
    await srv.close()
    said = [p for p in posts(site) if p.startswith(f"⏹ {plan.name} was interrupted")]
    assert said and "Its schedule may be stored already: `!month-end show` says, and if it " \
        "isn't, request again." in said[0]  # fmt: skip


async def test_the_apply_stores_the_keys_and_roster_it_hashed(site, server, monkeypatch):
    """N7: a roster changed by hand after the recheck refuses the store: nothing is stored."""
    keep(site, alice="2027-02-15", bob="2027-02-16")
    plan = await request(server, "!month-end schedule")
    real = runner_mod.Runner._steps

    async def edited(self, job, plan, rows, live=None, *args, **kwargs):  # after the recheck
        roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
        roster.write_text(roster.read_text() + "# a hand edit\n")
        return await real(self, job, plan, rows, live, *args, **kwargs)

    monkeypatch.setattr(runner_mod.Runner, "_steps", edited)
    await approve(server, plan, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    assert monthend.entries(server.store.db, "harvard-2026") == []
    assert any("the roster changed since the recheck: request again" in p for p in posts(site))
