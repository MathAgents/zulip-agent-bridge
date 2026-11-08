"""`!keep-until DATE|none SEL` on the bot's side (ADMIN §11.5, #570): the grammar of a kept VM's
end, its argument list and refusals, and the whole chain through the bot with the real
`keep_until.py`: plan, approval, apply, `!roster show`, nothing to change, and an end that
passes before the approval. The script itself is `tests/workshop/test_keep_until.py`'s."""

from __future__ import annotations

import calendar
import dataclasses
import shutil
import sqlite3
from pathlib import Path

import pytest

from zab.admin import monthend
from zab.admin.actions import INPUTS, KEEP_UNTIL_DAYS, VERBS, ActionError, build
from zab.admin.approvals import CHANGE, requirement
from zab.admin.grammar import (
    END_HOUR,
    SPECS,
    Command,
    Rejected,
    Selection,
    keep_until_end,
    parse,
)
from zab.admin.plans import PENDING, REFUSED, STALE, UNCHANGED

from .test_admin_actions import CONFIG, FLEET, PY, READ_AT, R, context, layout, make
from .test_admin_runner import approve, bot_of, posts, request, settle, site, start

assert site and layout  # fixtures, used by name

WORKSHOP = Path(__file__).resolve().parents[2] / "workshop"
KEEP = f"{PY}/admin/keep_until.py {R}"
#: How a dated `!keep-until`'s rows end, as its plan says (`monthend.keep_until_note`, #868).
HOW = (
    "ends each row at that time, with its notice by DM 7 days before (at once, when approved "
    "later than that); or end each VM with `!retire` then (ORGANIZERS §5.7)."
)
NONE_SCHEDULED = (
    "**No row of harvard-2026 is scheduled to end**, so nothing ends on its own at that time. "
    f"Once this applies, `!month-end schedule` {HOW}"
)
#: A row the schedule holds at that end already: its warnings (review of #871, B2).
HELD = (
    "with its warnings: the notice by DM 7 days before (at once, when approved later than "
    "that), and a reminder 1 day before unless the notice covers it. `!month-end show` says "
    "which went, and when each row ends."
)


def utc(*when: int) -> float:
    return float(calendar.timegm((*when, 0, 0, 0)[:6]))


# ------------------------------------------------------------------------------ the grammar


@pytest.mark.parametrize(
    ("message", "until", "slugs"),
    [
        ("!keep-until 2026-11-30 bob", "2026-11-30", ("bob",)),
        ("!keep-until 2026-11-30T18:30 bob dave", "2026-11-30T18:30", ("bob", "dave")),
        ("!keep-until 2026-11-30t18:30 bob", "2026-11-30T18:30", ("bob",)),  # `t` read as `T`
        ("!keep-until none dave", "none", ("dave",)),
    ],
)
def test_the_grammar_reads_a_date_a_time_or_none(message, until, slugs):
    got = parse(message, context(FLEET))
    assert got == Command("keep-until", selection=Selection(slugs=slugs), until=until)
    assert parse(got.text(), context(FLEET)) == got  # its text reads back the same
    assert SPECS["keep-until"].kind == "change"


@pytest.mark.parametrize(
    "message",
    [
        "!keep-until 2026-11-31 bob",  # no such day
        "!keep-until 2026-11-30T24:00 bob",
        "!keep-until 11/30/2026 bob",
        "!keep-until 2026-11-30 15:00 bob",  # the time is part of the date, after a T
        "!keep-until 2026-11-30Z bob",
        "!keep-until None bob",
        "!keep-until bob 2026-11-30",
    ],
)
def test_the_grammar_refuses_anything_else(message):
    got = parse(message, context(FLEET))
    assert isinstance(got, Rejected), got
    assert "a date like 2026-11-30, or 2026-11-30T15:00 (UTC), or none" in got.reason or (
        "15:00" in message and "not an argument" in got.reason
    )


def test_a_malformed_time_is_echoed_in_its_refusal():
    """Review of #817, N4: `T` and `:` are shown, as a valid end has them."""
    got = parse("!keep-until 2026-11-30T8:30 bob", context(FLEET))
    assert isinstance(got, Rejected) and "got `2026-11-30T8:30`" in got.reason, got


def test_a_date_alone_ends_at_the_hour_and_a_time_is_utc():
    assert END_HOUR == 15
    assert keep_until_end("2026-11-30") == utc(2026, 11, 30, 15)
    assert keep_until_end("2026-11-30T09:05") == utc(2026, 11, 30, 9) + 5 * 60
    assert keep_until_end("none") is None and keep_until_end("2026-02-30") is None


# ------------------------------------------------------------------------------ the action


def test_its_list_names_each_row_and_its_value_and_its_hash_is_the_roster(layout):
    action = make("!keep-until 2026-11-30T18:30 dave bob", layout)
    assert [" ".join(step.argv) for step in action.steps] == [
        f"{KEEP} --set bob=2026-11-30T18:30 --set dave=2026-11-30T18:30"  # in roster order
    ]
    assert action.steps[0].dry_run == (*action.steps[0].argv, "--dry-run")
    assert action.targets == ("bob", "dave") and action.inputs == (FLEET.workshop.roster,)
    assert VERBS["keep-until"].hashed == INPUTS and VERBS["keep-until"].nothing
    assert requirement(action, FLEET, CONFIG).klass == CHANGE  # one approver, no phrase
    # how each row ends is the plan's, from the month-end schedule (`Planning._kept`, #868)
    assert action.notes == ("**Kept until Monday, November 30, 2026, 18:30 UTC:** bob, dave.",)
    cleared = make("!keep-until none all", layout)
    assert cleared.notes == ("**Not kept until any date:** alice, bob, carol, dave.",)


@pytest.mark.parametrize(
    ("until", "why"),
    [
        ("2026-11-05", "Thursday, November 5, 2026, 15:00 UTC is ahead"),  # the control: ok
        ("2026-11-05T12:00", "Thursday, November 5, 2026, 12:00 UTC has passed: give an end in"),
        ("2026-11-01", "Sunday, November 1, 2026, 15:00 UTC has passed"),
        ("2027-11-07", f"Sunday, .* is more than {KEEP_UNTIL_DAYS} days ahead: check the year"),
    ],
)
def test_an_end_must_be_ahead_of_the_bots_clock_and_within_a_year(layout, until, why):
    """`Fleet.read_at` is noon UTC on Thursday, November 5, 2026."""
    assert READ_AT == utc(2026, 11, 5, 12)
    command = Command("keep-until", selection=Selection(slugs=("bob",)), until=until)
    if why.endswith("is ahead"):
        build(command, FLEET, layout=layout)
        last = dataclasses.replace(command, until="2027-11-06T12:00")  # 366 days, to the minute
        build(last, FLEET, layout=layout)
        with pytest.raises(ActionError, match="more than 366 days ahead"):
            build(dataclasses.replace(last, until="2027-11-06T12:01"), FLEET, layout=layout)
        return
    with pytest.raises(ActionError, match=f"^{why}"):
        build(command, FLEET, layout=layout)


def test_an_unknown_clock_a_bad_value_or_a_retired_row_is_refused(layout):
    command = Command("keep-until", selection=Selection(slugs=("bob",)), until="2026-11-30")
    with pytest.raises(ActionError, match="the bot's clock is not known"):
        build(command, dataclasses.replace(FLEET, read_at=None), layout=layout)
    build(dataclasses.replace(command, until="none"), dataclasses.replace(FLEET, read_at=None),
          layout=layout)  # fmt: skip
    for bad in ("2026-11-31", "30-11-2026", "", None, "--yes"):
        forged = dataclasses.replace(command, until="2026-11-30")
        object.__setattr__(forged, "until", bad)  # past the grammar's own checks (#323)
        with pytest.raises(ActionError, match="expected a date like 2026-11-30, or none"):
            build(forged, FLEET, layout=layout)
    retired = dataclasses.replace(FLEET, retired={"bob": "P-50 at 2026-10-29 14:02 UTC"})
    with pytest.raises(ActionError, match=r"^bob retired \(P-50 at .*nothing to keep"):
        build(command, retired, layout=layout)
    # its date cleared, which reaches no VM (review of #817, N3)
    cleared = build(dataclasses.replace(command, until="none"), retired, layout=layout)
    assert cleared.targets == ("bob",) and cleared.steps[0].argv[-2:] == ("--set", "bob=none")
    every = build(dataclasses.replace(command, selection=Selection(all=True)), retired,
                  layout=layout)  # fmt: skip
    assert every.targets == ("alice", "carol", "dave")  # `all`: never a retired row


# ----------------------------------------------------------------- how the rows end (#868)


def entry(slug: str, keep_until: str, state: str = monthend.SCHEDULED, plan: int = 60):
    """A row of harvard-2026's month-end schedule, as `monthend.entries` reads it."""
    return monthend.Entry(
        workshop="harvard-2026", slug=slug, plan=plan, keep_until=keep_until,
        ends=keep_until_end(keep_until) or 0.0, vms="i-1", tagged="i-1", bots=(),
        zulip_user_id="", warned=(), state=state, note="", changed=0.0,
    )  # fmt: skip


def note(held, *slugs: str, value: str = "2026-12-01", run: str | None = None) -> list[str]:
    return monthend.keep_until_note(held, slugs, value, "harvard-2026", run)


@pytest.mark.parametrize(
    "state", [monthend.DONE, monthend.STOPPED, monthend.DROPPED, monthend.CANCELLED]
)
def test_with_no_month_end_scheduled_nothing_ends_on_its_own(state):
    """No schedule, or one that would end no row any more: `!month-end schedule`, or `!retire`
    on the day."""
    assert note([], "bob") == [NONE_SCHEDULED]
    assert note([entry("bob", "2026-12-01", state)], "bob", "dave") == [NONE_SCHEDULED]


def test_a_schedule_ends_a_row_it_holds_at_that_end_and_no_other():
    """A row scheduled at another end drops out at the next look (`monthend.changed`), and one
    the schedule doesn't hold isn't in it: a new `!month-end schedule` ends them, or `!retire`.
    One scheduled at that end keeps its place and its warnings (review of #871, B2)."""
    held = [entry("alice", "2026-12-01"), entry("bob", "2026-11-30"),
            entry("carol", "2026-11-30", monthend.CANCELLED)]  # fmt: skip
    new = f"Once this applies, a new `!month-end schedule`, which replaces P-60's, {HOW}"
    assert note(held, "bob") == [
        "**The month-end schedule** (P-60) won't end bob at that time: this new date drops bob "
        f"out of it at the bot's next look. {new}"
    ]
    assert note(held, "carol", "dave") == [
        "**The month-end schedule** (P-60) won't end carol, dave at that time: carol, dave are "
        f"not in it. {new}"
    ]
    assert note(held, "alice") == [
        f"**Already in the month-end schedule** (P-60) at that end: alice, {HELD}"
    ]
    assert note(held, "alice", "bob", "dave") == [
        f"**Already in the month-end schedule** (P-60) at that end: alice, {HELD}",
        "**The month-end schedule** (P-60) won't end bob, dave at that time: this new date drops "
        f"bob out of it at the bot's next look, and dave is not in it. {new}",
    ]
    # the same end in another form is another `keep_until`, which `changed` drops
    assert "drops alice out of it" in note(held, "alice", value="2026-12-01T15:00")[0]


def test_a_row_in_a_month_end_run_is_ending_now_whatever_the_date():
    """Review of #871, B1: a `running` row is in a run. While it applies (`run`), this plan
    can't apply until the run ends, which goes on with the row; a run cut short resumes at a
    later look, unless this applies first. Either way, `!month-end cancel` stops it. Never
    `same` or a drop-out, whatever its `keep_until`."""
    held = [entry("alice", "2026-11-30", monthend.RUNNING),
            entry("bob", "2026-12-01", monthend.RUNNING), entry("carol", "2026-11-30")]  # fmt: skip
    assert note(held, "alice", run="P-62") == [
        "**Ending now**, in the month-end run P-62: alice. This plan can't apply until that run "
        "ends, and the run goes on with it: `!month-end cancel alice` stops it before its next "
        "step (ORGANIZERS §5.7)."
    ]
    assert note(held, "alice", "bob") == [
        "**In a month-end run cut short**, which a later look of the bot resumes: alice, bob. "
        "That run goes on with them unless this applies first, which drops them out: "
        "`!month-end cancel alice bob` stops each at once (ORGANIZERS §5.7)."
    ]
    both = note(held, "alice", "carol", run="P-62")
    assert len(both) == 2 and both[0].startswith("**Ending now**, in the month-end run P-62: ")
    assert both[1].startswith("**The month-end schedule** (P-60) won't end carol at that time")


# ---------------------------------------------------------------------- through the bot


def real_script(site) -> None:
    """The release's own keep_until.py and the two modules it imports, where the runner runs
    the release's scripts."""
    release = Path(site.paths.layout.release) / "workshop"
    for name in ("admin/keep_until.py", "admin/admin_common.py", "aws/aws_common.py"):
        (release / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(WORKSHOP / name, release / name)


def roster(site) -> str:
    return (site.tmp / "ws" / "harvard-2026" / "roster.csv").read_text(encoding="utf-8")


async def test_a_kept_vms_end_is_planned_approved_written_and_shown(site):
    """botkit's clock is Friday, January 15, 2027, 08:00 UTC."""
    real_script(site)
    srv = await start(site)
    try:
        plan = await request(srv, "!keep-until 2027-02-15 alice bob")
        assert plan.state == PENDING, plan.note
        message = posts(site)[-2]
        assert "alice: keep_until (none) → 2027-02-15 (its VM ends Monday, February 15, 2027, " \
            "15:00 UTC)" in message  # fmt: skip
        assert "**Kept until Monday, February 15, 2027, 15:00 UTC:** alice, bob." in message
        assert NONE_SCHEDULED in message  # no schedule in state.db (#868)
        assert plan.requirement is not None and plan.requirement.klass == CHANGE
        await approve(srv, plan)
        await settle(srv)
        result = f"✅ **{plan.name}** · `!keep-until 2027-02-15 alice bob`: done in "
        assert any(post.startswith(result) for post in posts(site))
        lines = roster(site).splitlines()
        assert lines[0].endswith(",keep_until") and lines[1].endswith(",2027-02-15")
        assert lines[3].endswith(",") and "carol" in lines[3]  # not named: none
        await bot_of(srv, plan.bot).on_message(_incoming("!roster show", 7000))
        shown = posts(site)[-1]
        assert "kept until" in shown and "2027-02-15" in shown
        again = await request(srv, "!keep-until 2027-02-15 alice", mid=7100)
        assert again.state == UNCHANGED  # every row named has that date already: no plan
        assert posts(site)[-1].startswith(f"☑ **{again.name}** · `!keep-until 2027-02-15 alice`")
        past = await request(srv, "!keep-until 2027-01-15T07:00 carol", mid=7200)
        assert past.state == REFUSED
        assert past.note.startswith("Friday, January 15, 2027, 07:00 UTC has passed")
    finally:
        await srv.close()


async def test_the_plan_says_how_its_rows_end_as_the_schedule_stands(site):
    """#868: read from `state.db` when the plan is made; `none` says nothing of it."""
    real_script(site)
    srv = await start(site)
    try:
        db = srv.store.db
        for e in (entry("alice", "2027-02-01"), entry("bob", "2027-02-15")):
            db.execute(
                "INSERT INTO month_end VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (e.workshop, e.slug, e.plan, e.keep_until, e.ends, e.vms, e.tagged, "[]",
                 e.zulip_user_id, "[]", e.state, e.note, e.changed),
            )  # fmt: skip
        db.commit()
        plan = await request(srv, "!keep-until 2027-02-15 alice bob carol")
        assert plan.state == PENDING, plan.note
        message = posts(site)[-2]
        assert f"**Already in the month-end schedule** (P-60) at that end: bob, {HELD}" in message
        assert (
            "**The month-end schedule** (P-60) won't end alice, carol at that time: this new "
            "date drops alice out of it at the bot's next look, and carol is not in it. Once "
            f"this applies, a new `!month-end schedule`, which replaces P-60's, {HOW}"
        ) in message
        await approve(srv, plan)
        await settle(srv)
        cleared = await request(srv, "!keep-until none alice", mid=5200)
        assert cleared.state == PENDING, cleared.note
        message = posts(site)[-2]
        assert "**Not kept until any date:** alice." in message
        assert "month-end schedule" not in message
    finally:
        await srv.close()


async def test_a_schedule_that_cant_be_read_leaves_the_plan_with_a_note(site, monkeypatch):
    """Review of #871, N1: the plan is still made, and says the schedule can't be read."""
    real_script(site)
    srv = await start(site)

    def broken(db, workshop):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(monthend, "entries", broken)
    try:
        plan = await request(srv, "!keep-until 2027-02-15 alice")
        assert plan.state == PENDING, plan.note
        assert (
            "**The month-end schedule can't be read** (OperationalError): `!month-end show` says "
            "whether it holds these rows (ORGANIZERS §5.7)."
        ) in posts(site)[-2]
    finally:
        await srv.close()


async def test_an_end_that_passes_before_the_approval_makes_the_plan_stale(site):
    """`Planning.rebuild` builds the plan's action again at apply, against the clock then."""
    real_script(site)
    srv = await start(site)
    try:
        plan = await request(srv, "!keep-until 2027-01-15T08:05 alice")
        assert plan.state == PENDING, plan.note
        site.clock.now += 6 * 60  # within the plan's 10 minutes, past its end
        await approve(srv, plan)
        await settle(srv)
        assert srv.store.get(plan.id).state == STALE
        assert "has passed" in srv.store.get(plan.id).note
        assert "keep_until" not in roster(site)
    finally:
        await srv.close()


def _incoming(text: str, mid: int):
    from .botkit import OLGA, incoming

    return incoming(OLGA, text, mid=mid)
