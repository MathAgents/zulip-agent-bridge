"""Nothing to change (#708): a dry run that exits `NOTHING` for a verb that says it may
(`Verb.nothing`) is answered as a success, `☑`, with no plan and no approval asked, and recorded
as its own outcome, `unchanged`; anything else keeps `❌`. Through the bots of `botkit` with the
real `Runner`, as in `test_admin_runner.py`. `!roster sync`'s own case is in
`test_admin_roster_sync.py`; the scripts' exits are in `tests/workshop/test_admin_repos.py` and
`test_roster_import.py`.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from zab.admin import audit, auditview
from zab.admin.actions import NOTHING, VERBS
from zab.admin.planning import UNCHANGED_MARK
from zab.admin.plans import PENDING, REFUSED, UNCHANGED
from zab.admin.runner import NOTHING_TO_CHANGE, says_nothing

from .botkit import HARVARD, OLGA, incoming
from .test_admin_actions import layout, make
from .test_admin_runner import (
    _failing,
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

assert site and layout  # fixtures, used by name

#: provision_repos.py --invites-only as its dry run answers (#708): exit 3 when `NOTHING` is set.
REPOS = """\
print("invites: 0 invited, 0 accepted, 0 removed, 0 failed; 2 unchanged" + " (dry run)" * DRY)
if DRY and "--invites-only" in sys.argv and NOTHING:
    print("nothing to change: no invitation to send or withdraw, and no acceptance to record")
    sys.exit(3)
"""
#: roster_import.py's dry run of an upload that changes no row (#708).
IMPORT = """\
print("roster import: 0 added, 0 changed, 0 removed, 3 unchanged")
print("no changes: nothing to write")
sys.exit(3 if DRY else 0)
"""
FOOTER = re.compile(r"\n`audit #\d+ · chain [0-9a-f]{12}`$")


def repos(site, *, nothing: bool):
    fake(site, "admin/provision_repos.py", f"NOTHING = {nothing}\n" + REPOS)


def outcome(site, plan):
    """The events of the plan's audit records, in order."""
    return [r["event"] for r in records(site) if r.get("plan") == plan.name]


async def test_invites_only_with_nothing_to_change_gets_a_success_mark_and_no_plan(site):
    repos(site, nothing=True)
    srv = await start(site)
    try:
        before = len(posts(site))
        plan = await request(srv, "!provision repos invites-only all")
        assert plan.state == UNCHANGED and plan.note == NOTHING_TO_CHANGE
        said = posts(site)[before:]
        assert len(said) == 1, said  # no plan message, no approval message
        title = f"☑ **{plan.name}** · `{plan.command}`: {NOTHING_TO_CHANGE}"
        assert said[0].startswith(f"{title}; no plan to approve\n"), said[0]
        assert "nothing to change: no invitation to send or withdraw" in said[0]
        assert "❌" not in said[0] and FOOTER.search(said[0]), said[0]
        assert outcome(site, plan) == ["request", "unchanged"]  # never `refused`, nor `plan`
        (record,) = records(site, "unchanged")
        assert (record["verb"], record["reason"]) == ("provision repos", NOTHING_TO_CHANGE)
        assert [c["dry"] for c in calls(site, "admin/provision_repos.py")] == [True]
        # `!plan` says so, and an approval of it changes nothing: there is nothing to approve
        bot = bot_of(srv, HARVARD)
        await bot.on_message(incoming(OLGA, f"!plan {plan.name}", mid=6000))
        await settle(srv)
        assert f"`{plan.command}`: **unchanged** ({NOTHING_TO_CHANGE})" in (posts(site)[-1])
        await bot.on_message(incoming(OLGA, f"!approve {plan.name} 3f9a2c", mid=10**6))
        await settle(srv)
        assert posts(site)[-1].startswith("❌ ")  # as for a refused plan: nobody approves it
        assert srv.store.need(plan.id).state == UNCHANGED
        assert [c["dry"] for c in calls(site, "admin/provision_repos.py")] == [True]
    finally:
        await srv.close()


async def test_invites_only_with_something_to_change_is_planned_as_before(site):
    repos(site, nothing=False)
    srv = await start(site)
    try:
        plan = await request(srv, "!provision repos invites-only all")
        assert plan.state == PENDING, plan.note
        assert outcome(site, plan) == ["request", "plan"]
    finally:
        await srv.close()


@pytest.mark.parametrize(
    ("command", "script", "body", "why"),
    [
        # a failed dry run keeps ❌, for a verb that may say nothing to change too
        (
            "!provision repos invites-only all",
            "admin/provision_repos.py",
            "NOTHING = False\n" + REPOS + "sys.exit(1)\n",
            "the dry run failed: admin/provision_repos.py exited 1",
        ),
        # exit 3 from a full `!provision repos` is a failed dry run (review of #726, 2)
        (
            "!provision repos all",
            "admin/provision_repos.py",
            'print("0 of 2 ready (dry run)"); sys.exit(3)',
            "the dry run failed: admin/provision_repos.py exited 3",
        ),
        # exit 3 from a verb that never says nothing to change is a failed dry run
        (
            "!stop alice",
            "aws/power.py",
            'print("alice: would stop i-1"); sys.exit(3)',
            "the dry run failed: aws/power.py exited 3",
        ),
    ],
)
async def test_anything_else_keeps_its_refusal(site, command, script, body, why):
    fake(site, script, body)
    srv = await start(site)
    try:
        plan = await request(srv, command)
        assert plan.state == REFUSED and plan.note == why
        assert posts(site)[-1].startswith(f"❌ {plan.name} refused: {why}; nothing was done")
        assert outcome(site, plan) == ["request", "refused"]
        assert UNCHANGED_MARK not in posts(site)[-1]
    finally:
        await srv.close()


def test_only_a_one_step_verb_that_says_so_has_nothing_to_change(layout):
    """`runner.says_nothing`: `Verb.nothing`, one step, so that no other step's dry run is left
    unread, and the verb's `nothing_flag`: `!provision repos` says so with `invites-only` alone,
    by construction (review of #726, 2). The verbs that say so are the ones #708 names."""
    nothing = [v for v, verb in VERBS.items() if verb.nothing]
    # sync's: its own file; keep-until's (#570): a date every row named has already
    assert nothing == ["roster import", "roster sync", "provision repos", "keep-until"]
    for message in (
        "!roster import [r.csv](/user_uploads/2/a/r.csv)",
        "!provision repos invites-only all",
        "!provision repos invites-only alice",
        "!keep-until 2026-11-30 alice",
    ):
        action = make(message, layout)
        assert len(action.steps) == 1 and says_nothing(action), message
    for message in ("!provision repos all", "!provision repos no-invite alice", "!stop alice"):
        assert not says_nothing(make(message, layout)), message  # its exit 3 is a failure
    one = make("!provision repos invites-only all", layout)
    assert not says_nothing(dataclasses.replace(one, steps=one.steps * 2))
    assert NOTHING == 3


def test_unchanged_is_an_audit_event_of_its_own_that_opens_no_plan():
    assert "unchanged" in audit.EVENTS and "unchanged" in auditview.CLOSING
    view = auditview.View(900.0)
    record = {
        "seq": 7,
        "ts": "2026-11-01T10:00:00Z",
        "event": "unchanged",
        "plan": "P-9",
        "workshop": "harvard-2026",
        "verb": "roster sync",
        "reason": NOTHING_TO_CHANGE,
    }
    view.observe(record)
    (shown,) = view.records()
    assert (shown["event"], shown["plan"], shown["verb"]) == ("unchanged", "P-9", "roster sync")
    assert shown["reason"] == NOTHING_TO_CHANGE
    assert view.plans("harvard-2026", 0.0) == []


async def test_an_unrecorded_outcome_still_answers_and_never_leaves_the_plan_planning(
    site, monkeypatch
):
    """`Planning._unchanged` is best effort, as `_refuse` is: an audit log that can't be
    written leaves the plan UNCHANGED and its requester answered, with no chain head."""
    repos(site, nothing=True)
    srv = await start(site)
    try:
        monkeypatch.setattr(srv, "audit", _failing(srv, "unchanged"))
        plan = await request(srv, "!provision repos invites-only all")
        assert plan.state == UNCHANGED and records(site, "unchanged") == []
        said = posts(site)[-1]
        assert said.startswith(f"☑ **{plan.name}** · ") and "`audit #" not in said
    finally:
        await srv.close()


async def test_an_import_that_changes_no_row_gets_the_success_mark(site):
    """`!roster import` of the roster as it stands: roster_import.py's dry run exits 3."""
    fake(site, "admin/roster_import.py", IMPORT)
    srv = await start(site)
    try:
        path = site.realm.upload("same.csv", b"slug,first_name,email\nalice,Alice,a@example.com\n")
        plan = await request(srv, f"!roster import [same.csv]({path})")
        assert plan.state == UNCHANGED and outcome(site, plan) == ["request", "unchanged"]
        said = posts(site)[-1]
        assert said.startswith(f"☑ **{plan.name}** · `!roster import") and "no changes" in said
    finally:
        await srv.close()
