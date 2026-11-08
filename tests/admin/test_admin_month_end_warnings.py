"""The month-end schedule's warnings and drop-outs (ADMIN §11.5, #570): when each warning is due,
its record (never sent twice), the DMs' texts, the checks against the roster, and the bot's
look, once a minute from its tick, through the fake realm."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from zab.admin import monthend
from zab.admin.actions import Row
from zab.admin.grammar import keep_until_end
from zab.admin.monthend import (
    CLOSE_S,
    COVERED,
    NO_ID,
    NOTICE,
    NOTICE_S,
    REMINDER,
    REMINDER_S,
    SENT,
    UNSURE,
    Entry,
)
from zab.admin.plans import DONE

from .botkit import DREW, HARVARD
from .test_admin_actions import FLEET
from .test_admin_retire import LATE, server, site
from .test_admin_runner import approve, records, request, settle

assert site and server  # fixtures, used by name

ENDS = keep_until_end("2026-11-30")  # Monday, November 30, 2026, 15:00 UTC
NO_KEYS = (("claude", ""), ("codex", ""))


def entry(**fields) -> Entry:
    base = dict(
        workshop="harvard-2026", slug="bob", plan=50, keep_until="2026-11-30", ends=ENDS,
        vms="i-2", tagged="i-2", bots=NO_KEYS, zulip_user_id="2002", warned=(),
        state=monthend.SCHEDULED, note="", changed=0.0,
    )  # fmt: skip
    return Entry(**(base | fields))


# ---------------------------------------------------------------------------------- timing


def test_the_notice_is_due_a_week_ahead_and_the_reminder_a_day_ahead():
    e = entry()
    assert monthend.due(e, ENDS - NOTICE_S - 1) is None
    assert monthend.due(e, ENDS - NOTICE_S) == NOTICE
    assert monthend.due(e, ENDS - REMINDER_S) == NOTICE  # never sent: the notice first
    sent = entry(warned=((NOTICE, ENDS - NOTICE_S, SENT),))
    assert monthend.due(sent, ENDS - REMINDER_S - 1) is None
    assert monthend.due(sent, ENDS - REMINDER_S) == REMINDER
    both = entry(warned=((NOTICE, 0.0, SENT), (REMINDER, 0.0, SENT)))
    assert monthend.due(both, ENDS - 60) is None
    assert monthend.due(entry(), ENDS) == NOTICE  # late: the row waits for it (`run_at`)
    assert monthend.due(sent, ENDS) is None  # no reminder after the end
    assert monthend.due(entry(state=monthend.CANCELLED), ENDS - 60) is None


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    monthend.setup(conn)
    e = entry()
    with conn:
        conn.execute(
            "INSERT INTO month_end VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.workshop, e.slug, e.plan, e.keep_until, e.ends, e.vms, e.tagged,
             json.dumps(e.bots), e.zulip_user_id, "[]", e.state, e.note, e.changed),
        )  # fmt: skip
    yield conn
    conn.close()


def warned(db) -> tuple:
    return monthend.entries(db, "harvard-2026")[0].warned


def test_a_record_replaces_its_kind_a_late_notice_covers_the_reminder_and_none_takes_it_back(db):
    early = ENDS - NOTICE_S
    monthend.warn(db, entry(), NOTICE, SENT, early)
    assert warned(db) == ((NOTICE, early, SENT),)
    monthend.warn(db, entry(), NOTICE, UNSURE, early + 1)
    assert warned(db) == ((NOTICE, early + 1, UNSURE),)
    monthend.warn(db, entry(), NOTICE, None, early + 2)
    assert warned(db) == ()
    late = ENDS - CLOSE_S  # within 36 hours of the end: no second DM
    monthend.warn(db, entry(), NOTICE, SENT, late)
    assert warned(db) == ((NOTICE, late, SENT), (REMINDER, late, COVERED))
    assert monthend.due(monthend.entries(db, "harvard-2026")[0], ENDS - 60) is None
    monthend.warn(db, entry(ends=ENDS + 60), REMINDER, SENT, late)  # another end's: untouched
    assert len(warned(db)) == 2
    monthend.warn(db, entry(plan=51), NOTICE, UNSURE, late)  # its end's, whichever plan
    assert warned(db)[0] == (NOTICE, late, UNSURE)


def test_a_warning_is_claimed_only_while_the_schedule_still_holds_it(db):
    """Codex round 1 on #829: recorded before its post only for the same plan, the same end,
    still scheduled, and the warning still due; the caller sends only then."""
    at = ENDS - NOTICE_S
    assert not monthend.claim(db, entry(ends=ENDS + 60), NOTICE, SENT, at)  # another end
    assert not monthend.claim(db, entry(), NOTICE, SENT, at - 1)  # not due yet
    assert not monthend.claim(db, entry(), REMINDER, SENT, at)  # not this one
    assert warned(db) == ()
    assert monthend.claim(db, entry(), NOTICE, SENT, at)
    assert warned(db) == ((NOTICE, at, SENT),)
    assert not monthend.claim(db, entry(), NOTICE, SENT, at + 1)  # recorded already
    monthend.cancel(db, "harvard-2026", ["bob"], "by Olga", at + 2)
    assert not monthend.claim(db, entry(), REMINDER, SENT, ENDS - REMINDER_S)  # cancelled
    assert not monthend.claim(db, entry(slug="carol"), NOTICE, SENT, at)  # no such row


def test_a_late_notice_covers_the_reminder_only_once_sent_or_perhaps_sent(db):
    """Codex round 2 on #829: a notice within 36 hours of the end, taken back to be tried again
    or refused, takes its cover of the reminder with it; sent, or perhaps sent, it keeps it."""
    late = ENDS - 30 * 3600
    assert monthend.claim(db, entry(), NOTICE, SENT, late)  # provisionally: covered
    assert warned(db) == ((NOTICE, late, SENT), (REMINDER, late, COVERED))
    monthend.warn(db, entry(), NOTICE, None, late)  # surely refused: tried again
    assert warned(db) == ()
    assert monthend.claim(db, entry(), NOTICE, SENT, late + 60)
    monthend.warn(db, entry(), NOTICE, monthend.REFUSED, late + 60)  # refused, TRIES times
    assert warned(db) == ((NOTICE, late + 60, monthend.REFUSED),)
    current = monthend.entries(db, "harvard-2026")[0]
    assert monthend.due(current, ENDS - REMINDER_S) == REMINDER  # it still has its reminder
    monthend.warn(db, entry(), NOTICE, UNSURE, late + 120)  # perhaps sent: it covers
    assert warned(db) == ((NOTICE, late + 120, UNSURE), (REMINDER, late + 120, COVERED))
    monthend.warn(db, entry(), REMINDER, SENT, ENDS - 3600)  # a reminder sent is never undone
    monthend.warn(db, entry(), NOTICE, None, ENDS - 3600)
    assert warned(db) == ((REMINDER, ENDS - 3600, SENT),)


def test_a_row_without_a_zulip_id_is_not_covered(db):
    monthend.warn(db, entry(), NOTICE, NO_ID, ENDS - 3600)
    assert warned(db) == ((NOTICE, ENDS - 3600, NO_ID),)


# ---------------------------------------------------------------------------------- texts


NOTICE_TEXT = """\
**Your workshop VM ends on Monday, November 30, 2026, at 15:00 UTC.**

The organizers of harvard-2026 have scheduled the end of the VM that runs your bots \
Claude · bob and Codex · bob. From that time on, your bots may stop answering at any moment.

**What is kept:** before the VM is deleted, your agents stop, and the work in your \
repository on the VM, with all its topic branches, is committed and pushed to \
[MathAgents/bob](https://github.com/MathAgents/bob) on GitHub. Your repository and your \
Zulip channels stay as they are.

**What is not pushed:** your agents' session transcripts, anything outside your repository \
on the VM, files over 50 MB, files your `.gitignore` excludes, `.lake` build folders, and \
anything that looks like a credential. If you want an archive of your session transcripts \
and your work folder, or more time, ask the workshop's organizers before November 30. \
Nobody reads replies to this message.

**Before then,** sign the VM out of your accounts: ask one of your agents to run `codex \
logout` and `claude auth logout`. Once the VM is gone, revoke its sessions in your ChatGPT \
and Claude account settings."""
REMINDER_TEXT = (
    "**Reminder: your workshop VM ends on Monday, November 30, 2026, at 15:00 UTC.** From then "
    "on, your bots Claude · bob and Codex · bob may stop answering at any moment. The work in "
    "your repository on the VM is pushed to your repository on GitHub before the VM is "
    "deleted; session transcripts and anything outside the repository are not, unless you "
    "have asked the organizers for an archive. If you haven't yet, ask one of your agents to "
    "run `codex logout` and `claude auth logout`. Nobody reads replies to this message."
)


def test_the_texts_are_the_ones_the_maintainer_read():
    assert monthend.notice(entry(), "harvard-2026", "MathAgents/bob") == NOTICE_TEXT
    assert monthend.reminder(entry(), "harvard-2026", "") == REMINDER_TEXT  # no repo: words
    linked = REMINDER_TEXT.replace(  # #570's text: the link bare (review of #829, B1)
        "pushed to your repository on GitHub before",
        "pushed to [MathAgents/bob](https://github.com/MathAgents/bob) before",
    )
    assert monthend.reminder(entry(), "harvard-2026", "MathAgents/bob") == linked
    odd = monthend.notice(entry(), "harvard-2026", "MathAgents/bob) [x](https://evil")
    assert "your repository on GitHub" in odd and "evil" not in odd
    timed = entry(keep_until="2026-12-01T09:05", ends=keep_until_end("2026-12-01T09:05"))
    assert "ends on Tuesday, December 1, 2026, at 09:05 UTC.**" in monthend.reminder(timed, "w", "")


# ------------------------------------------------------------------------------- drop-outs


KEPT = dataclasses.replace(FLEET, rows=(Row("bob", "198.51.100.2", keep_until="2026-11-30"),))


@pytest.mark.parametrize(
    ("fleet", "people", "why"),
    [
        (KEPT, {"bob": "2002"}, ""),
        (dataclasses.replace(KEPT, rows=()), {}, "no longer in the roster"),
        (dataclasses.replace(KEPT, rows=(Row("bob", excluded=True, keep_until="2026-11-30"),)),
         {"bob": "2002"}, "excluded from the roster"),
        (dataclasses.replace(KEPT, retired={"bob": "P-61 at 2026-11-20 10:00 UTC"}),
         {"bob": "2002"}, "retired already (P-61 at 2026-11-20 10:00 UTC)"),
        (dataclasses.replace(KEPT, rows=(Row("bob", "198.51.100.2", keep_until="2026-12-07"),)),
         {"bob": "2002"}, "its keep_until changed to 2026-12-07 since the approval"),
        (dataclasses.replace(KEPT, rows=(Row("bob", "198.51.100.2"),)), {"bob": "2002"},
         "its keep_until cleared since the approval"),
        (KEPT, {"bob": "2003"}, "its participant's Zulip ID changed"),
    ],
)  # fmt: skip
def test_a_row_that_changed_since_its_approval_drops_out(fleet, people, why):
    got = monthend.changed(entry(), fleet, people)
    assert why in got if why else got == ""


def test_a_row_whose_bots_keys_changed_drops_out(tmp_path):
    ws = dataclasses.replace(FLEET.workshop, out_dir=str(tmp_path))
    fleet = dataclasses.replace(KEPT, workshop=ws)
    assert monthend.changed(entry(), fleet, {"bob": "2002"}) == ""
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "claude.zuliprc").write_text("[api]\nkey=new\n")
    assert "its bots' keys changed since the approval (out/bob/)" in monthend.changed(
        entry(), fleet, {"bob": "2002"}
    )


# ---------------------------------------------------------------------- through the bot


ROSTER = """\
slug,first_name,last_name,email,github,ip,region,zulip_user_id,repo,keep_until
alice,Alice,A,alice@example.com,alice-gh,198.51.100.1,us-west-2,2001,MathAgents/alice,2027-02-15
bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2,,MathAgents/bob,2027-02-16
carol,Carol,C,carol@example.com,carol-gh,,,,,
"""
ALICE_ENDS = keep_until_end("2027-02-15")


def dms(site) -> list[dict]:
    return [p for p in site.realm.posts(HARVARD) if p["type"] == "private"]


def topic(site) -> list[str]:
    return [p["content"] for p in site.realm.posts(HARVARD) if p["type"] == "stream"]


async def scheduled(site, srv, roster: str = ROSTER):
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(roster)
    site.realm.person(2002, "Bob B")
    site.realm.person(2001, "Alice A")  # the participant the DMs go to
    plan = await request(srv, "!month-end schedule")
    await approve(srv, plan, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(srv)
    assert srv.store.get(plan.id).state == DONE
    return plan


async def look(site, srv, at: float) -> None:
    site.clock.now = at
    srv.planner.month_end_next = 0.0  # due now, whatever the last look
    await srv.planner.tick()


async def test_the_warnings_go_once_each_by_dm_and_are_said_in_the_topic(site, server):
    plan = await scheduled(site, server)
    await look(site, server, ALICE_ENDS - NOTICE_S - 60)  # a minute early: nothing
    assert dms(site) == []
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    (dm,) = dms(site)
    assert json.loads(dm["to"]) == [2001] and dm["content"].startswith(
        "**Your workshop VM ends on Monday, February 15, 2027, at 15:00 UTC.**"
    )
    assert "[MathAgents/alice](https://github.com/MathAgents/alice) on GitHub" in dm["content"]
    line = topic(site)[-1]
    assert line.startswith(f"📨 **{plan.name}**, the month end: the notice went by DM to alice")
    (record,) = [r for r in records(site, "month_end") if r.get("what") == "look"]
    assert record["rows"] == {"notice sent": ["alice"]} and record["plan"] == plan.name
    await look(site, server, ALICE_ENDS - NOTICE_S + 120)  # the next look: nothing again
    assert len(dms(site)) == 1
    bob_ends = keep_until_end("2027-02-16")
    await look(site, server, bob_ends - NOTICE_S + 60)  # bob has no Zulip ID
    assert len(dms(site)) == 1
    assert "no notice could go, with no Zulip ID in the roster, to bob" in topic(site)[-1]
    assert "to bob, whose VM ends at its date all the same, as approved. Tell those " \
        "participants yourselves.\n`audit #" in topic(site)[-1]  # fmt: skip
    await look(site, server, ALICE_ENDS - REMINDER_S + 60)
    assert dms(site)[-1]["content"].startswith("**Reminder: your workshop VM ends on Monday, ")
    assert len(dms(site)) == 2
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert [(kind, how) for kind, _, how in alice.warned] == [(NOTICE, SENT), (REMINDER, SENT)]
    await look(site, server, ALICE_ENDS - 60)
    assert len(dms(site)) == 2  # nothing more before the end


async def test_a_dm_that_may_have_landed_is_never_sent_again(site, server):
    await scheduled(site, server)
    site.realm.broken.add(HARVARD)  # a 500: it may have landed
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    site.realm.broken.discard(HARVARD)
    await look(site, server, ALICE_ENDS - NOTICE_S + 120)
    assert dms(site) == [] or all("Reminder" not in d["content"] for d in dms(site))
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert [(kind, how) for kind, _, how in alice.warned] == [(NOTICE, UNSURE)]
    # the topic's line failed too, with the bot's posts: the record has it
    looks = [r["rows"] for r in records(site, "month_end") if r.get("what") == "look"]
    assert looks == [{"notice unsure": ["alice"]}]
    assert "may not have reached alice" in monthend.look_line("P-1", looks[0])


async def test_a_cancel_between_the_look_and_the_send_stops_the_dm(site, server, monkeypatch):
    """Codex round 1 on #829: the look reads the schedule, then awaits the fleet; a cancel
    landing then keeps its warning from going, and nothing is recorded or said for it."""
    await scheduled(site, server)
    real = server.read_fleet

    async def meanwhile(ws):  # the look's first await after its read of the schedule
        monthend.cancel(server.store.db, "harvard-2026", ["alice"], "by Olga", site.clock())
        return await real(ws)

    monkeypatch.setattr(server, "read_fleet", meanwhile)
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    assert dms(site) == []
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert (alice.state, alice.warned) == (monthend.CANCELLED, ())
    assert not any("the notice went" in line for line in topic(site))


async def test_a_replacement_between_the_look_and_the_send(site, server, monkeypatch):
    """Codex rounds 1 and 3 on #829: a replacing schedule landing while the look awaits. With
    another end, it takes the warning over: the look sends nothing, and the next look sends the
    new end's, once. With the same end, it is the same warning, which `store` carries over: it
    goes once."""
    await scheduled(site, server)
    real, replaced = server.read_fleet, []
    later = keep_until_end("2027-02-22")

    async def meanwhile(ws):  # a new schedule's store: alice's end moved, its warnings its own
        if not replaced:
            replaced.append(True)
            with server.store.db:
                server.store.db.execute(
                    "UPDATE month_end SET plan = 99, keep_until = '2027-02-22', ends = ?, "
                    "warned = '[]' WHERE slug = 'alice'", (later,),
                )  # fmt: skip
        return await real(ws)

    monkeypatch.setattr(server, "read_fleet", meanwhile)
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    roster.write_text(ROSTER.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,2027-02-22"))
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    assert dms(site) == []  # the old end's notice: never sent
    await look(site, server, later - NOTICE_S + 60)
    assert len(dms(site)) == 1 and "ends on Monday, February 22, 2027" in dms(site)[0]["content"]
    await look(site, server, later - NOTICE_S + 120)
    assert len(dms(site)) == 1


async def replaced_while_sending(site, server, monkeypatch, answer):
    """alice's notice, 30 hours before her end, with a replacing schedule of the same end
    landing while `delivered` awaits Zulip's answer (Codex round 3 on #829), which is
    `answer`."""
    await scheduled(site, server)
    bot = server.bot_of(server.config.workshops[0])
    real = bot.delivered

    async def delivered(**params):
        if params["type"] != "private":
            return await real(**params)
        with server.store.db:  # `store` carries the provisional record over: the same end
            server.store.db.execute("UPDATE month_end SET plan = 99 WHERE slug = 'alice'")
        return answer

    monkeypatch.setattr(bot, "delivered", delivered)
    await look(site, server, ALICE_ENDS - 30 * 3600)
    return monthend.entries(server.store.db, "harvard-2026")[0]


async def test_a_refusal_while_a_replacement_lands_is_taken_back_on_it(site, server, monkeypatch):
    """The refusal reaches the record the new plan holds: no `sent` and no cover left, so the
    notice is tried again."""
    alice = await replaced_while_sending(site, server, monkeypatch, (None, "refused"))
    assert (alice.plan, alice.warned) == (99, ())
    assert monthend.due(alice, ALICE_ENDS - 30 * 3600 + 60) == NOTICE


async def test_a_success_while_a_replacement_lands_stays_on_it(site, server, monkeypatch):
    alice = await replaced_while_sending(site, server, monkeypatch, (12345, ""))
    assert alice.plan == 99
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT), (REMINDER, COVERED)]
    assert monthend.due(alice, ALICE_ENDS - 60) is None  # nothing more to send


#: alice with no slug (hers is her GitHub login, lower-cased), every header in mixed case
MIXED = """\
Slug,First_Name,Last_Name,Email,GitHub,IP,Region,Zulip_User_ID,Repo,Keep_Until
,Alice,A,alice@example.com,Alice,198.51.100.1,us-west-2,2001,MathAgents/alice,2027-02-15
bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2,2002,MathAgents/bob,2027-02-16
"""


def test_the_hashed_bytes_are_read_as_every_roster_is(tmp_path):
    """Codex round 4 on #829: one reading of a roster, whether from its file or its bytes."""
    from zab.admin.fleet import read_roster, roster_rows

    path = tmp_path / "roster.csv"
    path.write_bytes(b"\xef\xbb\xbf" + MIXED.encode())  # a spreadsheet's byte-order mark too
    rows = roster_rows(path.read_bytes())
    assert rows == read_roster(str(path))
    assert [(r["slug"], r["zulip_user_id"]) for r in rows] == [("alice", "2001"), ("bob", "2002")]


async def test_a_row_without_a_slug_or_with_mixed_case_headers_keeps_its_id(site, server):
    """Codex round 4 on #829: the schedule stores the Zulip ID the looks read, so the row
    doesn't drop out, and its notice goes."""
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(MIXED)
    site.realm.person(2001, "Alice A")
    plan = await request(server, "!month-end schedule")
    await approve(server, plan, sender=DREW, phrase="month-end 2", mid=LATE + 1)
    await settle(server)
    alice, bob = monthend.entries(server.store.db, "harvard-2026")
    assert (alice.slug, alice.zulip_user_id, bob.zulip_user_id) == ("alice", "2001", "2002")
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    assert monthend.entries(server.store.db, "harvard-2026")[0].state == monthend.SCHEDULED
    assert [json.loads(d["to"]) for d in dms(site)] == [[2001]]


def test_a_warning_is_its_recipients_too(db):
    """Codex round 5 on #829: `warn` and `claim` touch the record of the same end and the same
    recipient only."""
    at = ENDS - NOTICE_S
    assert not monthend.claim(db, entry(zulip_user_id=""), NOTICE, NO_ID, at)  # another one
    assert monthend.claim(db, entry(), NOTICE, SENT, at)
    monthend.warn(db, entry(zulip_user_id="2003"), NOTICE, None, at)  # another one's outcome
    assert warned(db) == ((NOTICE, at, SENT),)


async def renewed(site, server, before: str, after: str):
    """A schedule whose bob is warned with the Zulip ID `before` (or none), then a new schedule
    of the same end with `after`: the warnings each gets (Codex round 5 on #829)."""
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    bob = "bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2,{},MathAgents/bob,2027-02-16"
    text = ROSTER.replace(bob.format(""), bob.format(before))
    text = text.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,")  # bob alone
    text = text.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,")  # bob alone
    roster.write_text(text)
    site.realm.person(2001, "Alice A")
    site.realm.person(2002, "Bob B")
    first = await request(server, "!month-end schedule")
    await approve(server, first, sender=DREW, phrase="month-end 1", mid=LATE + 1)
    await settle(server)
    bob_ends = keep_until_end("2027-02-16")
    await look(site, server, bob_ends - REMINDER_S + 60)  # both due: notice, reminder
    await look(site, server, bob_ends - REMINDER_S + 120)
    roster.write_text(text.replace(bob.format(before), bob.format(after)))
    second = await request(server, "!month-end schedule", mid=6000)
    await approve(server, second, sender=DREW, phrase="month-end 1", mid=LATE + 2)
    await settle(server)
    assert server.store.get(second.id).state == DONE, server.store.get(second.id).note
    sent = len(dms(site))
    await look(site, server, bob_ends - REMINDER_S + 180)
    await look(site, server, bob_ends - 3600)
    entry_ = next(e for e in monthend.entries(server.store.db, "harvard-2026") if e.slug == "bob")
    return entry_, dms(site)[sent:]


async def test_an_id_added_after_no_zulip_id_gets_both_warnings(site, server):
    """Codex round 5 on #829, its sequence: both warnings recorded `no Zulip ID`; the organizer
    adds the ID and approves a new schedule of the same end: the participant gets the notice
    (which, within 36 hours of the end, covers the reminder)."""
    bob, new = await renewed(site, server, "", "2002")
    assert [json.loads(d["to"]) for d in new] == [[2002]]
    assert new[0]["content"].startswith("**Your workshop VM ends on Tuesday, February 16, 2027")
    assert [(k, h) for k, _, h in bob.warned] == [(NOTICE, SENT), (REMINDER, COVERED)]


async def test_an_id_removed_records_no_zulip_id_for_the_new_schedule(site, server):
    """The other way: warned at 2002, then the ID removed and a new schedule approved: the new
    recipient, none, has warnings of its own: `no Zulip ID`, and no DM to the old one."""
    bob, new = await renewed(site, server, "2002", "")
    assert new == []
    assert [(k, h) for k, _, h in bob.warned] == [(NOTICE, NO_ID), (REMINDER, NO_ID)]


async def test_a_row_whose_end_changed_drops_out_once_and_gets_no_warning(site, server):
    plan = await scheduled(site, server)
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    roster.write_text(ROSTER.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,2027-02-22"))
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    assert dms(site) == []
    line = topic(site)[-1]
    assert line.startswith(f"📨 **{plan.name}**, the month end: dropped out of the schedule")
    assert "alice (its keep_until changed to 2027-02-22 since the approval)" in line
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert (alice.state, alice.note) == (
        monthend.DROPPED,
        "its keep_until changed to 2027-02-22 since the approval",
    )
    said = len(topic(site))
    await look(site, server, ALICE_ENDS - NOTICE_S + 120)
    assert len(topic(site)) == said  # said once


# ------------------------------------------------------------------ review of #829, round 1


def test_drop_says_whether_it_took_and_a_cut_line_points_to_show(db):
    """N2: a row cancelled meanwhile doesn't drop, nor is it said to; N4: a line naming more
    than five rows says where to read them all."""
    monthend.cancel(db, "harvard-2026", ["bob"], "by Olga", 1.0)
    assert monthend.drop(db, entry(), "its keep_until changed", 2.0) is False
    many = {"notice sent": [f"p{n}" for n in range(7)]}
    assert monthend.look_line("P-1", many).endswith("`!month-end show` lists every row.")
    assert "`!month-end show`" not in monthend.look_line("P-1", {"notice sent": ["a"]})


def test_the_bots_failure_words_are_the_warnings():
    from zab.admin import bot as bot_mod

    assert (bot_mod.REFUSED, bot_mod.UNREACHED, bot_mod.UNSURE) == (
        monthend.REFUSED, monthend.UNREACHED, monthend.UNSURE
    )  # fmt: skip


def answering(monkeypatch, server, *answers):
    """The workshop bot's DMs answered in turn by `answers`, `(id, how)` as `delivered`
    returns, or an exception; the last one again for the rest. Its posts in channels go."""
    bot = server.bot_of(server.config.workshops[0])
    real, sent = bot.delivered, []

    async def delivered(**params):
        if params["type"] != "private":
            return await real(**params)
        answer = answers[min(len(sent), len(answers) - 1)]
        sent.append(json.loads(params["to"]) if isinstance(params["to"], str) else params["to"])
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(bot, "delivered", delivered)
    return sent


async def test_a_restart_while_a_dm_is_out_leaves_it_unsure_never_sent(site, server, monkeypatch):
    """B2: claimed as `unsure`, `sent` only once Zulip gave its ID."""
    import asyncio

    await scheduled(site, server)
    answering(monkeypatch, server, asyncio.CancelledError())
    site.clock.now = ALICE_ENDS - NOTICE_S + 60
    with pytest.raises(asyncio.CancelledError):
        await server.planner.month_end(server.config.workshops[0])
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, UNSURE)]


TWO = ROSTER.replace("bob-gh,198.51.100.2,us-west-2,,MathAgents/bob,2027-02-16",
                     "bob-gh,198.51.100.2,us-west-2,2002,MathAgents/bob,2027-02-15")  # fmt: skip


async def test_a_failure_ends_the_looks_dms_and_the_next_rows_go_next_time(
    site, server, monkeypatch
):
    """B3: a DM that may have landed ends the look's DMs: the rows after it go at the next
    look, so that a blip costs one row's warning, not every row's."""
    await scheduled(site, server, TWO)
    sent = answering(monkeypatch, server, (None, UNSURE), (77, ""))
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    alice, bob = monthend.entries(server.store.db, "harvard-2026")
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, UNSURE)]
    assert bob.warned == () and len(sent) == 1  # bob's waits, unclaimed
    await look(site, server, ALICE_ENDS - NOTICE_S + 120)
    bob = monthend.entries(server.store.db, "harvard-2026")[1]
    assert [(k, h) for k, _, h in bob.warned] == [(NOTICE, SENT)] and len(sent) == 2


async def test_a_dm_that_never_reached_zulip_is_tried_again_uncounted(site, server, monkeypatch):
    """B3: no connection (or Zulip's rate limit) is no refusal: tried again at each look,
    never counted, and sent once Zulip answers."""
    await scheduled(site, server)
    outage = [(None, monthend.UNREACHED)] * (monthend.TRIES + 2)
    sent = answering(monkeypatch, server, *outage, (78, ""))
    for minute in range(1, monthend.TRIES + 4):
        await look(site, server, ALICE_ENDS - NOTICE_S + 60 * minute)
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, SENT)]
    assert len(sent) == monthend.TRIES + 3


@pytest.mark.parametrize("change", ["end", "recipient"])
async def test_refusals_are_counted_per_end_and_per_recipient(site, server, monkeypatch, change):
    """B4: round 2's test, restored, for the end and the recipient: a notice refused `TRIES`
    times is recorded `refused`, its row stopped (#883), and never tried again; then a new
    schedule with a new end, or with the same end and a new recipient, has tries of its own,
    so that its first refusal is taken back to be tried again (the approval of #829, N1: each
    from the original end)."""
    await scheduled(site, server)
    refusals = answering(monkeypatch, server, (None, monthend.REFUSED))
    for minute in range(1, monthend.TRIES + 1):
        await look(site, server, ALICE_ENDS - NOTICE_S + 60 * minute)
    alice = monthend.entries(server.store.db, "harvard-2026")[0]
    assert [(k, h) for k, _, h in alice.warned] == [(NOTICE, monthend.REFUSED)]
    assert alice.state == monthend.STOPPED and len(refusals) == monthend.TRIES
    await look(site, server, ALICE_ENDS - NOTICE_S + 60 * (monthend.TRIES + 1))
    assert len(refusals) == monthend.TRIES  # refused: never tried again
    if change == "end":
        ends = keep_until_end("2027-02-22")
        text = ROSTER.replace("MathAgents/alice,2027-02-15", "MathAgents/alice,2027-02-22")
        update = (
            "UPDATE month_end SET plan = plan + 1, keep_until = '2027-02-22', ends = :ends, "
            "warned = '[]', state = 'scheduled', note = '' WHERE slug = 'alice'"
        )
    else:  # the same end and plan, its tries used up: only the recipient tells them apart
        ends, text = ALICE_ENDS, ROSTER.replace(",2001,", ",2009,")
        site.realm.person(2009, "Alice New")
        update = (
            "UPDATE month_end SET zulip_user_id = '2009', warned = '[]', state = 'scheduled', "
            "note = '' WHERE slug = 'alice'"
        )
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(text)
    with server.store.db:  # a new schedule's store: its warning its own, untried
        server.store.db.execute(update, {"ends": ends})
    await look(site, server, ends - NOTICE_S + 60 * (monthend.TRIES + 2))
    alice = next(e for e in monthend.entries(server.store.db, "harvard-2026")
                 if e.slug == "alice")  # fmt: skip
    assert len(refusals) == monthend.TRIES + 1 and alice.warned == (), alice.warned


async def test_a_look_sends_at_most_its_cap_and_the_rest_next_time(site, server, monkeypatch):
    """N1: 30 rows due at once: 25 DMs, then 5 at the next look."""
    rows = "".join(
        f"p{n:02d},P,{n},p{n}@example.com,,198.51.100.{n + 10},us-west-2,{3000 + n},,2027-02-15\n"
        for n in range(30)
    )
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(
        ROSTER.splitlines()[0] + "\n" + rows
    )
    site.vms = {f"p{n:02d}": f"i-{n}" for n in range(30)}
    for n in range(30):
        site.realm.person(3000 + n, f"P {n}")
    plan = await request(server, "!month-end schedule")
    await approve(server, plan, sender=DREW, phrase="month-end 30", mid=LATE + 1)
    await settle(server)
    assert server.store.get(plan.id).state == DONE, server.store.get(plan.id).note
    await look(site, server, ALICE_ENDS - NOTICE_S + 60)
    assert len(dms(site)) == monthend.LOOK_DMS == 25
    await look(site, server, ALICE_ENDS - NOTICE_S + 120)
    assert len(dms(site)) == 30
    assert "`!month-end show` lists every row." in topic(site)[-2]  # 25 named: cut


async def test_the_plan_names_the_rows_no_warning_can_reach(site, server):
    """N6 (#570's plan): bob has no Zulip ID in the roster."""
    (site.tmp / "ws" / "harvard-2026" / "roster.csv").write_text(ROSTER)
    await request(server, "!month-end schedule")
    message = [p["content"] for p in site.realm.posts(HARVARD) if p["type"] == "stream"][-2]
    head = "**No Zulip ID in the roster, so no warning can reach them: approving this plan ends"
    assert head in message and "`!month-end schedule` again. The row: bob." in message
