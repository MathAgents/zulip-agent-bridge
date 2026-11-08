"""A17, the automated month-end (docs/ADMIN.md §7, §11.5; #570), on rows of its own, after A9 and
before A18.

A9 ends `zab-test-a` and `zab-test-b`, and its `!retire` ends `zab-test-r`. A17 brings back
`zab-test-r` (`ending.RETIRE_KEEPER`): a deployer's fix clears its `host` and `ip` (ADMIN §3.3's
"What brings it back"; it never had bots), then `!launch`. The repository `MathAgents/zab-test-r`
exists already: an owner made it for A9 (ADMIN §7). With the GitHub token, `!provision repos
no-invite` gives the new VM its checkout and deploy key, whatever the roster's `repo_status`
says of the VM A9 tore down; it must succeed, its `repo_status` then `ok`, or A17 fails. A17
also adds a new row, `zab-test-n`. At most three instances run, the server included (§7): A17
runs after A9 has ended its rows, and ends both of its own before A18 launches its VM.

**Alone or after the others.** The rig's setup leaves the test workshops with no `roster.csv`
(each run starts from none, so nothing of A17's rows carries over to the next run's A9). Run
alone (`--scenarios A17`), A17 reads that as an empty roster, and its import makes one with its
two rows; after A9, it adds them to A9's (`roster_text`). It needs nothing else of the other
scenarios: the rig's setup gives the workshop's bot, channel, AWS scope and fleet wheel, and its
GitHub token or not (Q9); a launch of a row the ledger has retired is allowed once the roster
has no `host` or `ip` for it, as a new import has.

**The participant.** `zab-test-r`'s `zulip_user_id` is set to `zab-tester2`'s by hand, as
zab-admin, before the schedule's plan: the row has no bots (Q9), and the warnings go to that ID.
A row that holds that ID already gives it up (`rebound`): after A9, `zab-test-b` does, its VM
torn down, and two rows may not share one, which the bot's roster writers refuse.
`zab-test-n` keeps none, so the plan names it, and its notice is recorded `no Zulip ID`.

**The run.**
1. `!protect on zab-test-r` and `!stop zab-test-r`.
2. `!keep-until <now + 15 min> zab-test-r zab-test-n`, then `!month-end schedule`, approved by
   `zab-tester` alone, a global admin, with the phrase.
3. Within a look: the notice reaches `zab-tester2` by DM, and the topic says so, and that
   `zab-test-n` has no Zulip ID.
4. `!keep-until <a day later> zab-test-n`: at the next look it drops out, with no new approval.
5. A refused notice (#883): `zab-test-n`'s `zulip_user_id` set by hand to one no user of the
   realm has (`NO_SUCH_USER`), whose DMs Zulip refuses (`Invalid user ID`, a 400), then
   `!month-end schedule` again, approved as the first, which replaces it: `zab-test-r` keeps its
   notice, sent once (`zab-tester2` has one notice DM from the bot, counted once the run has
   started). Within `TRIES` looks, a minute apart, `zab-test-n`'s notice is refused at every
   try, which stops the row before any step, its VM still running, and the topic says so,
   with the commands that end it. The rest of A17 follows this second schedule.
6. At the end, a run: `zab-test-r` is started and synced, its protection lifted.
7. While it waits out the 5 minutes before its teardown, the bots are restarted.
8. The run is INTERRUPTED, and the next look resumes it at `teardown`: no second start, no
   second sync. The teardown begins at least 300 s after the protection off ended (#378), and
   the instance is terminated.

**Without the GitHub token** (Q9), the final sync can't push to `MathAgents/zab-test-r`: the run
stops `zab-test-r` at its sync, its VM still running, which A17 checks; after the clean-up it
skips, naming what it couldn't check (the restart, the resume and the #378 bound) and Q9, so
that its row is never a full pass (`run.py --require A17` fails). So does a run that reached
its teardown's wait with less than a minute left for the restart. **Without the provisioner's
key,** the bots step fails after the teardown, and the row is `stopped` there, which A17 notes
and checks.

**The end.** Every row of A17's whose VM still runs (`zab-test-n`, stopped by its refused
notice; `zab-test-r` when its sync couldn't run) is retired `unsynced`, by a global admin
alone, and its instance terminated. A failure from the schedule's approval on first cancels
both rows' month end, best effort, so that no run starts `zab-test-r` later; their VMs are
left to the run's teardown, which terminates every test workshop's instance. A18 then runs
beside at most one of them (`zab-test-n`, three instances in all, as its own check allows), or
skips, naming them. A wait fails as soon as the row ends some other way than the one it waits
for, saying how.
"""

from __future__ import annotations

import inspect
import re
import time
from typing import Any

import pytest

from e2e.admin.harness import ending
from e2e.admin.harness.chat import Talk
from e2e.admin.harness.rig import E2E
from e2e.admin.harness.settle import until
from e2e.admin.test_scenarios_2_bot import (
    PROVISIONER_Q9,
    ROSTER_COLUMNS,
    import_roster,
    records,
    restart,
    row,
    talk,
    vm_state,
    vm_states,
    wait_state,
)
from e2e.admin.test_scenarios_8_the_end import admin_json, last_seq, terminated
from e2e.harness.zulip_user import dm_narrow

R, N = ending.RETIRE_KEEPER, "zab-test-n"
WS = f"/var/lib/zab-admin/ws/{E2E.name}"
#: How long after its end a row's run starts: within a look of the bot (a minute), with room.
LOOK_S = 150
#: A Zulip user ID that no user of the realm has: Zulip refuses a DM to it with a 400 (#883).
NO_SUCH_USER = "999999999"
#: A warning's tries, a look apart, before its refusal stops the row (`monthend.TRIES`).
TRIES = 3
#: How long a notice takes to be refused at every try: its tries, from the look after the
#: schedule's apply, and room.
REFUSED_S = (TRIES + 1) * 60 + LOOK_S
#: Why the run can't sync `zab-test-r` while Q9 is open (review of #839, N4).
GITHUB_R = (
    "Q9: the final sync pushes to MathAgents/zab-test-r, which needs the test server's GitHub "
    "token: `!provision repos no-invite` gives the new VM its checkout and deploy key"
)
#: A month-end row's states once it is out of the schedule (`monthend`).
ENDED = ("stopped", "done", "dropped", "cancelled")
#: What A17 waits for and reads in the bot's texts (`test_harness_chain` matches each against
#: the text the bot builds, with its own functions: Codex round 2 on #839).
NOTICE_RE = r"^\*\*Your workshop VM ends on "
SCHEDULED = ": 2 rows scheduled"


def unwarned(slug: str) -> str:
    """The end of the schedule's plan's note on a row with no Zulip ID, which names it after
    what approving does (`Planning._noted`)."""
    return f"`!month-end schedule` again. The row: {slug}."


def bots_named(slug: str) -> str:
    return f"Claude · {slug} and Codex · {slug}"


def looked(schedule: str) -> str:
    """A look's line in the schedule's topic (`monthend.look_line`)."""
    return rf"^📨 \*\*{schedule}\*\*, the month end: "


def warned(slug: str) -> str:
    return f"the notice went by DM to {slug}"


def unreached(slug: str) -> str:
    return f"with no Zulip ID in the roster, to {slug}"


def dropped_out(schedule: str) -> str:
    return looked(schedule) + "dropped out"


def dropped_why(slug: str, keep_until: str) -> str:
    return f"{slug} (its keep_until changed to {keep_until} since the approval)"


def refused_out(schedule: str) -> str:
    """A look's line naming a notice refused at every try (`monthend.look_line`, #883)."""
    return looked(schedule) + ".*Zulip refused the notice, at every try, to "


def stopped_by(slug: str) -> str:
    """What that line says of the row: stopped, and the command that ends it."""
    return (
        f"to {slug}, whose row stopped before any step and stays in service: `!retire {slug}` "
        "ends it by hand"
    )


#: As zab-admin: a row's `host` and `ip` cleared (ADMIN §3.3, "What brings it back"), or, with a
#: third argument, its `zulip_user_id` set (A17's participant). Atomic, as the bot's writes are.
def rebound(fields: list[str], rows: list[dict[str, str]], slug: str, uid: str) -> list[str]:
    """`slug`'s row given the Zulip ID `uid`, which every other row holding it gives up, so that
    no two rows share it, which the bot's roster writers refuse (`keep_until.py`: after A9,
    `zab-test-b` holds zab-tester2's, whose VM A9 tore down): the slugs that gave it up. Run
    on the server, within `EDIT`, and offline by the harness's tests."""
    if "zulip_user_id" not in fields:
        fields.append("zulip_user_id")
    cleared = []
    for r in rows:
        if (r.get("slug") or "").strip() != slug and (r.get("zulip_user_id") or "") == uid:
            r["zulip_user_id"] = ""
            cleared.append((r.get("slug") or "").strip())
    next(r for r in rows if (r.get("slug") or "").strip() == slug)["zulip_user_id"] = uid
    return cleared


EDIT = (
    "import csv, json, os, sys, tempfile\n"
    + inspect.getsource(rebound)
    + r"""
path, slug, *uid = sys.argv[1:]
if not path.startswith("/var/lib/zab-admin/ws/zab-admin-e2e/"):
    sys.exit(2)
with open(path, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fields, rows = list(reader.fieldnames), list(reader)
hits = [r for r in rows if (r.get("slug") or "").strip() == slug]
if len(hits) != 1:
    sys.exit(f"{slug}: {len(hits)} rows")
cleared = []
if uid:
    cleared = rebound(fields, rows, slug, uid[0])
else:
    hits[0].update(host="", ip="")
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="roster.csv.", suffix=".tmp")
with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields, restval="")
    writer.writeheader()
    writer.writerows(rows)
os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
print(json.dumps({"cleared": cleared}))
"""
)
#: As zab-admin, read-only: the workshop's month-end entries, and the steps recorded for one
#: schedule (`monthend`'s `month_end` and `month_end_steps`).
STEPS = r"""
import json, sqlite3, sys
db = sqlite3.connect("file:/var/lib/zab-admin/state.db?mode=ro", uri=True)
ws, schedule = sys.argv[1], int(sys.argv[2])
steps = db.execute("SELECT slug, stage, plan, started, ended FROM month_end_steps "
                   "WHERE workshop = ? AND schedule = ?", (ws, schedule)).fetchall()
entries = db.execute("SELECT slug, state, note FROM month_end WHERE workshop = ? AND plan = ?",
                     (ws, schedule)).fetchall()
print(json.dumps({"steps": steps, "entries": entries}))
"""


def at(ts: float) -> str:
    """`!keep-until`'s date and time in UTC, a whole minute: `2026-11-30T15:10`."""
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


def state(server: Any, schedule: str) -> dict[str, Any]:
    """The schedule's rows, `slug → (state, note)`, and its steps, `(slug, stage) → (run,
    started, ended)`, as `state.db` has them now."""
    found = admin_json(server, STEPS, E2E.name, schedule.removeprefix("P-"))
    steps = {(slug, stage): (plan, started, ended) for slug, stage, plan, started, ended
             in found["steps"]}  # fmt: skip
    return {"steps": steps, "entries": {slug: (s, note) for slug, s, note in found["entries"]}}


def roster_text(server: Any) -> str:
    """The e2e workshop's roster as the server has it, or "" when it has none: the rig's setup
    empties the test workshops and leaves no `roster.csv`, so A17 run alone starts from none
    and its import makes it (live run 1). Only `cat`'s own word for that path, with its exit
    1, says so: any other failure to read it, an ssh warning that names some other missing
    file included, fails A17, never read as an empty roster that its import would write over
    A9's (the review of live run 1)."""
    path = f"{WS}/roster.csv"
    done = server.run(["sudo", "cat", "--", path], timeout=300, check=False)
    if done.ok:
        return done.stdout
    said = [line.strip() for line in done.stderr.splitlines()]
    if done.returncode == 1 and f"cat: {path}: No such file or directory" in said:
        return ""
    raise AssertionError(f"roster.csv can't be read: exit {done.returncode}: "
                         f"{done.stderr.strip()[-200:]}")  # fmt: skip


def edit(server: Any, slug: str, *uid: str) -> list[str]:
    """`slug`'s `host` and `ip` cleared, or its Zulip ID set to `uid` (`rebound`): the rows
    that gave that ID up."""
    return admin_json(server, EDIT, f"{WS}/roster.csv", slug, *uid).get("cleared", [])


async def retire_unsynced(t: Talk, aws: Any, slugs: list[str], scenario: Any) -> None:
    """`!retire … unsynced` of `slugs`, by a global admin alone (#558), and their instances
    terminated: at most three instances run when A18 begins (§7)."""
    iids = {slug: vm_states(aws)[slug]["InstanceId"] for slug in slugs}
    plan = await t.request(f"!retire {' '.join(slugs)} unsynced")
    _, text = await t.approve(plan)
    scenario.notes.append(f"{plan.id}: {text.split(chr(10))[0][:200]}")
    for iid in iids.values():
        await terminated(aws, iid)


def crowded(aws: Any) -> list[str]:
    """The rig's instances running besides A17's own two: with the server, A17's two make the
    three §7 allows, so any other is one too many."""
    return [p for p in ending.running(aws.fleet()) if p not in (R, N)]


async def ended_as(server: Any, schedule: str, wanted: str, *, within: float) -> tuple[str, str]:
    """`zab-test-r`'s row, once it reaches `wanted`, or fails fast on any other end of a
    month-end row (review of #839, N2): its state and note."""
    found = await until(lambda: state(server, schedule)["entries"].get(R, ("", "")),
                        lambda e: e[0] == wanted or e[0] in ENDED, what=f"{R} {wanted}",
                        within=within, every=15)  # fmt: skip
    assert found[0] == wanted, f"{R} ended {found[0]}, not {wanted}: {found[1]}"
    return found


async def cancelled(t: Talk, scenario: Any) -> None:
    """Best effort, after a failure from the approval on (review of #839, B3): both rows out of
    the schedule, so that no run starts `zab-test-r` later, beside A18's VM."""
    try:
        answer = await t.ask(f"!month-end cancel {R} {N}", r"^(✅|❌|busy)")
        scenario.notes.append(f"after the failure, `!month-end cancel`: {answer['content'][:120]}")
    except Exception as exc:  # noted, never masking the failure
        scenario.notes.append(f"after the failure, `!month-end cancel` failed: {exc!r}"[:200])


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A17")
async def test_a17_month_end(scenario, admin_rig, rigged, server, aws, tester, tester2, nonce):
    if others := crowded(aws):
        pytest.skip(f"at most three instances run (§7): {others} run besides the server")
    t: Talk = talk(tester, rigged, E2E.name, f"e2e-A17-{nonce}")
    roster = ending.inputs(roster_text(server), ROSTER_COLUMNS)  # none when A17 runs alone
    new = [row(s, f"zab-admin-e2e-{s.removeprefix('zab-test-')}@example.com", admin_rig.region)
           for s in (R, N) if s not in {r["slug"] for r in roster}]  # fmt: skip
    if new:
        plan = await import_roster(t, [*roster, *new], f"a17-{nonce}.csv")
        _, text = await t.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    for slug in (R, N):  # a deployer's fix of a retired row (ADMIN §3.3); neither had bots
        edit(server, slug)
    await t.run(f"!launch {R} {N}")
    for slug in (R, N):
        await wait_state(aws, slug, "running")
    iid = vm_states(aws)[R]["InstanceId"]
    synced, why = bool(rigged.github_token), GITHUB_R
    if synced:  # always: the new VM needs its checkout and deploy key, whatever `repo_status`
        # says of the VM A9 tore down (Codex round 1 on #839). With the token, a failure is a
        # defect, not a missing prerequisite: it fails A17 (#839's approval, N1)
        await t.run(f"!provision repos no-invite {R}")
        status = ending.rows_by_slug(roster_text(server))[R].get("repo_status")
        assert status == "ok", f"{R}'s repo_status is {status!r} after its provisioning, not ok"
    else:
        scenario.notes.append(f"the run stops at {R}'s sync: {why}")
    if not rigged.provisioner:
        scenario.notes.append(f"the bots step fails after the teardown: {PROVISIONER_Q9}")
    me2 = (await tester2.me())["user_id"]
    if cleared := edit(server, R, str(me2)):  # A17's participant: no other row's (A9's B)
        scenario.notes.append(
            f"{', '.join(cleared)} held zab-tester2's Zulip ID {me2}, given up for {R}: no two "
            "rows may share one, and its VM is gone after A9"
        )
    await t.run(f"!protect on {R}")
    await t.run(f"!stop {R}")
    await wait_state(aws, R, "stopped")

    # the schedule
    end = (int(time.time()) // 60 + 16) * 60  # room for the second schedule (#883)
    await t.run(f"!keep-until {at(end)} {R} {N}")
    plan = await t.request("!month-end schedule")
    assert plan.phrase == "month-end 2", plan.phrase
    assert f"- {R}: ends " in plan.text and "**Commands:** none" in plan.text, plan.text[:500]
    assert unwarned(N) in plan.text, "the plan names no row unwarned"
    since = plan.approval_id  # before the apply: a look may warn before the result is posted
    over = False  # from the approval on, a failure cancels the schedule's rows (B3)
    try:
        _, text = await t.approve(plan)  # a global admin alone (#558)
        assert SCHEDULED in text.split("\n")[0], text.split("\n")[0]
        schedule = plan.id
        unchecked = await _the_month_end(scenario, server, aws, t, tester2, rigged, nonce,
                                         schedule, since, end, iid, synced, why)  # fmt: skip
        over = True
    finally:
        if not over:
            await cancelled(t, scenario)
    if unchecked:  # never a full pass: `run.py --require A17` must not count it (B2)
        pytest.skip(unchecked)


async def _refused(t: Talk, server: Any, aws: Any, after: int) -> str:
    """#883: `zab-test-n`, dropped out, given a Zulip ID that no user has, and `!month-end
    schedule` again, which replaces the schedule; its notice refused at every try stops the
    row, its VM still running, said in the topic with the commands. The new schedule."""
    edit(server, N, NO_SUCH_USER)
    plan = await t.request("!month-end schedule")
    assert plan.phrase == "month-end 2", plan.phrase
    assert unwarned(N) not in plan.text, "the plan names a row with a Zulip ID unwarned"
    _, text = await t.approve(plan)
    assert SCHEDULED in text.split("\n")[0], text.split("\n")[0]
    line = await t.reply(after, refused_out(plan.id), within=REFUSED_S)
    assert stopped_by(N) in line["content"], line["content"]
    entries = state(server, plan.id)["entries"]
    assert entries[N][0] == "stopped" and f"`!retire {N}`" in entries[N][1], entries[N]
    assert entries[R][0] in ("scheduled", "running"), entries[R]  # its notice carried over
    kept = vm_state(aws, N)
    assert kept in ("running", "stopped"), f"{N}'s VM is {kept} after its refused notice"
    return plan.id


async def _the_month_end(
    scenario: Any, server: Any, aws: Any, t: Talk, tester2: Any, rigged: Any, nonce: str,
    schedule: str, since: int, end: float, iid: str, synced: bool, why: str,
) -> str:  # fmt: skip
    """A17 from the approved schedule on: what it couldn't check, or ""."""
    # the warnings, then the extension
    notice = await tester2.wait_reply(
        dm_narrow(t.bot), after_id=since, senders=[t.bot],
        pattern=NOTICE_RE, within=LOOK_S,
    )  # fmt: skip
    assert bots_named(R) in notice["content"], notice["content"][:300]
    line = await t.reply(since, looked(schedule), within=LOOK_S)
    assert warned(R) in line["content"], line["content"]
    assert unreached(N) in line["content"], line["content"]
    await t.run(f"!keep-until {at(end + 86400)} {N}")
    dropped = await t.reply(line["id"], dropped_out(schedule), within=LOOK_S)
    assert dropped_why(N, at(end + 86400)) in dropped["content"], dropped["content"]
    assert state(server, schedule)["entries"][N][0] == "dropped"

    # a refused notice (#883), then the run, a restart while it waits out the 5 minutes, and
    # its resume: `seq` first, since the run may start while the refusals are awaited
    seq = last_seq(server)
    schedule = await _refused(t, server, aws, dropped["id"])
    await until(lambda: [r for r in records(server, seq) if r.get("event") == "apply_start"
                         and r.get("schedule") == schedule],
                bool, what="the run's apply_start", within=end - time.time() + LOOK_S + 60,
                every=10)  # fmt: skip
    run = next(r for r in records(server, seq) if r.get("event") == "apply_start"
               and r.get("schedule") == schedule)["plan"]  # fmt: skip
    # `zab-test-r`'s notice sent once (review of #908, N6): carried over to the second schedule,
    # never sent again. Counted now: its run has started, a running row gets no warning
    # (`monthend.due`, the look's scheduled rows), and a look posts its DMs before it starts a
    # run, so every notice it could get is in Zulip by now
    dms = await tester2.retrying(lambda: tester2.after(dm_narrow(t.bot), since),
                                 until=tester2.clock() + 120, what="the bot's DMs")  # fmt: skip
    notices = [m for m in dms if m["sender_id"] == t.bot and re.search(NOTICE_RE, m["content"])]
    assert [m["id"] for m in notices] == [notice["id"]], [m["content"][:80] for m in notices]
    if not synced:  # stopped at its sync: nothing torn down; then retired unsynced
        _, note = await ended_as(server, schedule, "stopped", within=1200)
        assert "final-sync" in note, note
        assert aws.describe(iid)["State"]["Name"] == "running", "torn down unsynced"  # N1
        await retire_unsynced(t, aws, [R, N], scenario)
        return (
            f"the restart during the wait before {R}'s teardown, the resume at it and the #378 "
            f"bound need the sync: {why}"
        )
    found = await until(
        lambda: (state(server, schedule)["steps"].get((R, "protect")),
                 state(server, schedule)["entries"].get(R, ("", ""))),
        lambda v: v[0] is not None or v[1][0] in ENDED,
        what=f"{R}'s protection off", within=1800, every=10,
    )  # fmt: skip
    assert found[0] is not None, f"{R} ended {found[1][0]} before its protection off: {found[1][1]}"
    protect_end = found[0][2]
    left = protect_end + 300 - time.time()
    unchecked = ""
    if left < 60:  # never a full pass without the restart (review of #839, B2)
        unchecked = f"the restart: {R}'s run reached its teardown's wait with {left:.0f} s left"
    else:
        await restart(server, talk(t.user, rigged, "global", f"e2e-A17-{nonce}"))
        assert any(r.get("event") == "interrupted" and r.get("plan") == run
                   for r in records(server, seq)), f"{run} is not recorded interrupted"  # fmt: skip
    finished, note = await until(lambda: state(server, schedule)["entries"].get(R, ("", "")),
                                 lambda e: e[0] in ENDED, what=f"{R}'s end", within=1200,
                                 every=15)  # fmt: skip
    ends = [r for r in records(server, seq) if r.get("event") == "apply_end"
            and R in (r.get("targets") or [])]  # fmt: skip
    if not unchecked:
        resumed = [r for r in ends if r.get("plan") != run]
        assert resumed, "no run resumed after the restart"
        scripts = [s["script"] for s in resumed[-1]["steps"] if s.get("row") == R]
        assert scripts[:1] == ["aws/teardown.py"], f"resumed at {scripts[:1]}, not its teardown"
        assert "admin/final_sync.py" not in scripts and "aws/power.py" not in scripts, scripts
    steps = [s for r in ends for s in r.get("steps") or [] if s.get("row") == R]
    teardown = next((s for s in steps if s["script"] == "aws/teardown.py"), None)
    assert teardown is not None, f"{R}'s teardown never ran: it ended {finished}, {note}"  # N3
    assert teardown["at"] >= protect_end + 300 - 1, (teardown["at"], protect_end)  # #378
    scenario.timing(f"the month end, {R}'s teardown after its protection off",
                    teardown["at"] - protect_end)  # fmt: skip
    await terminated(aws, iid)
    if rigged.provisioner:
        assert finished == "done", (finished, note)
    else:
        assert finished == "stopped" and "bots" in note, (finished, note)

    # the end: zab-test-n, stopped by its refused notice, retired unsynced
    await retire_unsynced(t, aws, [N], scenario)
    return unchecked
