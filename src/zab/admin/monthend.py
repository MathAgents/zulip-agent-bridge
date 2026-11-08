"""The automated month-end of the kept VMs (docs/ADMIN.md §11.5; #570, stage 8): the schedule.

- **The plan.** `!month-end schedule` is a destructive plan (§2.5) of every row in service with
  a `keep_until` (`actions._build_month_end`), hashed as `!retire`'s: each row's VMs, the VMs
  tagged with its slug and the teardown's dry run, with the roster (so the ends) and the rows'
  bot keys among its inputs. Its apply stores it here (`store`), with nothing to run.
- **One schedule per workshop,** in `state.db` (§1.3), one entry per row (`Entry`): the plan
  that approved it, the row's `keep_until` and its end, its VMs (`vms`, `tagged`) as the apply's
  recheck found them, its bots' identities (the SHA-256 of `out/<slug>/<kind>.zuliprc`, as
  `!deactivate-bots` hashes them: `out/` holds no bot user IDs), the participant's
  `zulip_user_id`, the warnings sent, and a state. Nothing in it names the server's release:
  what the approvers approve is the rows, their ends and `!retire`'s steps, and each run builds
  its argument lists with the release then running (§11.5, "Across restarts and upgrades"). A
  new schedule replaces the workshop's entries; a warning already sent for an unchanged end is
  kept, so that it is never sent again.
- **The warnings** (§11.5): a DM from the workshop's bot to the row's participant, the notice
  `NOTICE_S` before its end (or at once, when the schedule is approved later than that), and
  the reminder `REMINDER_S` before it, unless the notice went within `CLOSE_S` of the end,
  which then covers it (`due`, `warn`). New messages only, never edited (Zulip Cloud's rate
  guidance, #754). Each is recorded before its post, so that none is ever sent twice: one that
  Zulip surely refused (`AdminBot.deliver`) is tried at the next ticks, `TRIES` times in all;
  one that may have landed is never sent again. Their texts are `notice` and `reminder`.
- **A refusal stops the row** (#883): a warning that Zulip refused at every try stops its row
  before any step (`refuse`), which stays in service for the organizers to end by hand; the
  schedule's topic says so, with the commands. A row with no Zulip ID gets no warning: the
  approval of the plan that names it is its consent, and it ends at its date.
- **Drop-outs.** Before its warnings, each scheduled row is checked against the roster as it
  is now (`changed`): a row gone, excluded or retired, or whose `keep_until`, participant or
  bots changed since the approval, drops out of the schedule, said once in its topic. A new
  `!month-end schedule` is what brings it back.
- **The runs** (§11.5, "The end"). Once a row's end has passed, a look of the bot starts a run
  of the workshop's due rows (`due_rows`), when no plan applies in the workshop and the last
  `!protect` is 5 minutes old (its refusal is a wait). A run is a plan of its own, created
  APPLYING (`Planning._run`), so that it holds the workshop's apply lock and a restart marks it
  INTERRUPTED; `month_end_runs` names its schedule. Before its steps, it checks each row's VMs
  against the approval while a step that reaches the VM remains (`vm_changed`): another VM
  drops the row. Its steps are `!retire`'s (`actions.build_run`), in two passes (`order`): each
  row up to its protection off, then each row's teardown and bots, each teardown no sooner than
  `PROTECT_SETTLE` after its row's protection off ended. Each step that exits 0 is recorded
  (`month_end_steps`, keyed by the approved VMs, as it ends: `record`), and a later run never
  repeats one (`order`), but `start`, while a step that needs SSH remains (§11.5: a VM that
  stopped meanwhile would fail its sync). A row cancelled while the run applies stops before
  its next step. A row ends `done` (retired), or `stopped`, with the step that failed: only a
  restart leaves it `running`, for the next look to resume.
- **The veto.** `!month-end cancel SEL` takes rows out at once (`cancel`), from an approver of
  the workshop, as `!reject` vetoes a plan: it only shrinks what the bot will do.
- **The reading.** `!month-end show` (`show`): each entry, and the rows with a `keep_until`
  that the schedule doesn't hold.

`state.db` is not in the backups (§1.7): a rebuilt server has no schedule, and the organizers
approve one again. The audit log keeps each schedule's plan and each cancel.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from zab.admin.actions import (
    BOT_KINDS,
    Action,
    Ending,
    Fleet,
    Live,
    Step,
    utc_words,
)
from zab.admin.grammar import keep_until_end

SCHEDULED, RUNNING, DONE, STOPPED, DROPPED, CANCELLED = (
    "scheduled", "running", "done", "stopped", "dropped", "cancelled"
)  # fmt: skip
#: The states in which the schedule will still end a row.
OPEN = (SCHEDULED, RUNNING)
#: The warnings (§11.5), and how long before a row's end each is due; a notice sent within
#: `CLOSE_S` of the end covers the reminder, so that two DMs never come hours apart.
NOTICE, REMINDER = "notice", "reminder"
NOTICE_S, REMINDER_S, CLOSE_S = 7 * 86400, 86400, 36 * 3600
#: How a warning went (`warn`): sent; not sent, for a row with no Zulip ID; refused by Zulip
#: `TRIES` times, which stops the row (`refuse`); perhaps sent (an answer that may hide a post
#: that landed); covered by the notice.
SENT, NO_ID, REFUSED, UNSURE, COVERED = "sent", "no Zulip ID", "refused", "unsure", "covered"
#: A DM that never reached Zulip (`AdminBot.delivered`): no connection, or its rate limit still
#: on. It can't have landed, and is no refusal: tried again at the next look, never counted.
UNREACHED = "unreached"
#: How a notice went for it to cover the reminder: sent, or perhaps sent (Codex round 2 on #829).
COVERS = (SENT, UNSURE)
TRIES = 3
#: The DMs of one look at most: the rest go at the next looks, a minute apart, so that a look
#: of 170 rows due at once never holds the bot's loop for its whole burst, nor sends on a
#: snapshot of the roster minutes old (review of #829, N1).
LOOK_DMS = 25
#: A late notice's lead (`run_at`): a day, as the reminder's.
LATE_S = REMINDER_S
#: The note of a row whose run's steps are all on `state.db` but whose bots step is not on
#: the audit log, so the ledger doesn't have it retired (#855): a run of a development build
#: between #835 and #860, never a release, which wrote no step records; or a plan that named
#: the row since.
UNLOGGED = (
    "every step of its run done, but its bots' deactivation is not on the audit log: "
    "`!retire <row> unsynced` ends it"
)
#: A roster's `repo`, `<owner>/<name>`, as provision_repos.py writes it: linked in the DMs.
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", re.ASCII)
SCHEMA = """
CREATE TABLE IF NOT EXISTS month_end (
    workshop TEXT NOT NULL,
    slug TEXT NOT NULL,
    plan INTEGER NOT NULL,          -- the `!month-end schedule` that approved it
    keep_until TEXT NOT NULL,       -- as the roster had it
    ends REAL NOT NULL,             -- its end, in seconds since the epoch
    vms TEXT NOT NULL,              -- its VMs' IDs, comma-joined (Live.vms)
    tagged TEXT NOT NULL,           -- those tagged participant=<slug> (Live.tagged)
    bots TEXT NOT NULL,             -- JSON [[kind, SHA-256 of its zuliprc, or ""], ...]
    zulip_user_id TEXT NOT NULL,    -- the participant, for the warnings; "" for none
    warned TEXT NOT NULL,           -- JSON [[kind, when, how(, end)], ...]: `warn`
    state TEXT NOT NULL,
    note TEXT NOT NULL,
    changed REAL NOT NULL,          -- when its state last changed
    PRIMARY KEY (workshop, slug)
);
CREATE TABLE IF NOT EXISTS month_end_steps (
    workshop TEXT NOT NULL,
    slug TEXT NOT NULL,
    schedule INTEGER NOT NULL,      -- the schedule it ran for: another schedule, other steps
    vms TEXT NOT NULL,              -- the VMs the schedule approved: another VM, other steps
    stage TEXT NOT NULL,            -- actions.STAGES
    plan INTEGER NOT NULL,          -- the run that did it
    started REAL NOT NULL,
    ended REAL NOT NULL,
    PRIMARY KEY (workshop, slug, schedule, vms, stage)
);
CREATE TABLE IF NOT EXISTS month_end_runs (
    plan INTEGER PRIMARY KEY,       -- a run, a plan of its own
    schedule INTEGER NOT NULL       -- the `!month-end schedule` it runs
);
"""
#: The stages of a row that reach its VM over SSH: `start` runs again while one remains.
SSH_STAGES = ("idle-stop", "uninstall", "final-sync", "export")
#: The stages of a run's second pass, each row's after every row's first (`order`).
LAST_STAGES = ("teardown", "bots")
#: `Entry`'s fields, in order: what `entries` reads, and `store` writes.
_SELECT = (
    "SELECT workshop, slug, plan, keep_until, ends, vms, tagged, bots, zulip_user_id, warned, "
    "state, note, changed FROM month_end WHERE workshop = ? ORDER BY ends, slug"
)
_INSERT = (
    "INSERT INTO month_end (workshop, slug, plan, keep_until, ends, vms, tagged, bots, "
    "zulip_user_id, warned, state, note, changed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One row of a workshop's schedule (module doc)."""

    workshop: str
    slug: str
    plan: int
    keep_until: str
    ends: float
    vms: str
    tagged: str
    bots: tuple[tuple[str, str], ...]
    zulip_user_id: str
    warned: tuple[tuple[str, float, str], ...]  # (kind, when, how): `warn`
    state: str
    note: str
    changed: float
    #: the end the notice on record named (`warn`, stored with it): the run's (`run_at`)
    named: float | None = None


def setup(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def entries(db: sqlite3.Connection, workshop: str) -> list[Entry]:
    """The workshop's schedule, by end, then slug."""
    rows = db.execute(_SELECT, (workshop,)).fetchall()
    return [_entry(row) for row in rows]


def _entry(row: Sequence[object]) -> Entry:
    values = list(row)
    values[7] = tuple(tuple(pair) for pair in json.loads(str(values[7])))
    items = json.loads(str(values[9]))
    named = next((float(i[3]) for i in items if i[0] == NOTICE and len(i) > 3), None)
    values[9] = tuple(tuple(item[:3]) for item in items)
    return Entry(*values, named=named)  # type: ignore[arg-type]


def _items(entry: Entry) -> list[list[object]]:
    """`entry`'s warnings as `state.db` keeps them: its notice with the end it named."""
    return [[*item, entry.named] if item[0] == NOTICE and entry.named is not None else list(item)
            for item in entry.warned]  # fmt: skip


def store(
    db: sqlite3.Connection,
    *,
    plan: int,
    action: Action,
    fleet: Fleet,
    live: Live,
    digests: Mapping[str, str],
    people: Mapping[str, str],
    now: float,
) -> str:
    """The apply of `!month-end schedule` (module doc): the workshop's entries replaced by the
    plan's rows, each with its VMs as the apply's recheck found them (`live`), its bots' key
    digests (`digests`, the inputs') and its participant's Zulip ID (`people`, the roster's).
    A row of the same end and recipient keeps its warnings (`_carried`, #883). One
    transaction. Returns the lines the apply shows."""
    rows = {row.slug: row for row in fleet.rows}
    out = fleet.workshop.out_dir
    before = {e.slug: e for e in entries(db, action.workshop)}
    lines = [f"the month end of {action.workshop}, as {_name(plan)} schedules it:"]
    with db:
        db.execute("DELETE FROM month_end WHERE workshop = ?", (action.workshop,))
        for slug in action.targets:
            value = rows[slug].keep_until
            ends = keep_until_end(value)
            if ends is None:  # never: the plan was refused for it, and the roster is hashed
                raise ValueError(f"{slug}: its keep_until is not a date")
            bots = [[kind, digests.get(f"{out}/{slug}/{kind}.zuliprc", "")] for kind in BOT_KINDS]
            old = before.get(slug)
            who = people.get(slug, "")
            same = old is not None and (old.ends, old.zulip_user_id) == (ends, who)
            warned = _carried(old) if same else []  # the same warnings, their end too
            db.execute(
                _INSERT,
                (
                    action.workshop, slug, plan, value, ends, live.vms.get(slug, ""),
                    live.tagged.get(slug, ""), json.dumps(bots), people.get(slug, ""),
                    json.dumps(warned), SCHEDULED, "", now,
                ),
            )  # fmt: skip
            lines.append(f"{slug}: ends {utc_words(ends)}, VM {live.vms.get(slug) or '-'}")
    gone = sorted(set(before) - set(action.targets))
    if gone:
        lines.append(f"no longer in the schedule: {', '.join(gone)}")
    return "\n".join(lines) + "\n"


def cancel(
    db: sqlite3.Connection, workshop: str, slugs: Sequence[str], note: str, now: float
) -> tuple[list[str], list[str]]:
    """`!month-end cancel`: the named rows the schedule would still end, cancelled with `note`;
    and the named rows it holds no longer, or never held."""
    open_ = {e.slug for e in entries(db, workshop) if e.state in OPEN}
    done = [slug for slug in slugs if slug in open_]
    with db:
        for slug in done:
            db.execute(
                "UPDATE month_end SET state = ?, note = ?, changed = ? "
                "WHERE workshop = ? AND slug = ? AND state IN (?, ?)",
                (CANCELLED, note, now, workshop, slug, *OPEN),
            )
    return done, [slug for slug in slugs if slug not in open_]


def _carried(old: Entry) -> list[list[object]]:
    """`old`'s warnings as a new schedule of the same end and recipient carries them over
    (`store`, `claim`), with the end its notice named. A refused notice, which no one got, is
    not: due until it goes, with the row waiting for it (`run_at`), it is tried again whenever
    it is approved. A refused reminder stays refused (#883, Codex round 1): due only until the
    end, it is tried again while the end hasn't come, with the row unable to run meanwhile
    (`run_at`), and once the end has come, it stops the row at the next look (`refused`). (A
    schedule can't be approved after a row's end: its apply's rebuild refuses it, STALE.)"""
    return [i for i in _items(old) if i[0] != NOTICE or i[2] != REFUSED]


def due(entry: Entry, now: float) -> str | None:
    """The warning `entry` is due for at `now`: `NOTICE`, `REMINDER`, or None (module doc). A
    notice not given by the end is still due after it: the row then waits for it (`run_at`).
    A reminder refused at a try is owed (#883): due again until the end."""
    if entry.state != SCHEDULED:
        return None
    given = {kind for kind, _, how in entry.warned if (kind, how) != (REMINDER, REFUSED)}
    if NOTICE not in given and now >= entry.ends - NOTICE_S:
        return NOTICE
    if REMINDER not in given and entry.ends - REMINDER_S <= now < entry.ends:
        return REMINDER
    return None


def run_at(entry: Entry) -> float | None:
    """When a run may end the scheduled row (§11.5, #570): None until its notice is on record,
    sent, perhaps sent (`unsure`), or none for a row with no Zulip ID, whose approval is its
    consent; and never with a warning refused (#883), which stops the row (`refuse`): a notice
    on record is not a participant told (review of #835, B2). At the row's end when the notice
    is on record before it, however short the notice the approvers gave; and `LATE_S` after
    the notice when it is late (`late`): on record only after the end, or less than a day
    before it although the schedule was approved earlier (the server down, or a release
    without the warnings, until then), so that a notice that goes reaches its participant a
    day before the VM ends. The notice's text names that end (`end_of`)."""
    notice = next((item for item in entry.warned if item[0] == NOTICE), None)
    if notice is None or any(item[2] == REFUSED for item in entry.warned):
        return None
    if entry.named is not None:  # the end its DM named, kept with it (Codex round 1 on #852)
        return entry.named
    return notice[1] + LATE_S if late(entry, notice[1]) else entry.ends


def late(entry: Entry, at: float) -> bool:
    """Whether a notice sent at `at` is late, and so names an end `LATE_S` after it: sent after
    the row's end; or sent less than `REMINDER_S` before it, when the schedule holding the row
    was approved before that window (`Entry.changed`, its apply: a scheduled row's state
    changes at no other time), so that what cut the notice short was the bot, down until
    then, not the approvers, whose own short notice stands (#840)."""
    window = entry.ends - REMINDER_S
    return at >= entry.ends or (at >= window and entry.changed < window)


def end_of(entry: Entry, now: float) -> float:
    """The end a notice sent at `now` names: the row's, or a late notice's (`run_at`)."""
    return now + LATE_S if late(entry, now) else entry.ends


def claim(db: sqlite3.Connection, entry: Entry, kind: str, how: str, now: float) -> bool:
    """Record warning `kind` of `entry` as `how` before it is sent, only if, as `state.db` has
    it now, the row is still scheduled for the same end, and the warning still due (`due`):
    whether it was recorded, and so may be sent (Codex round 1 on #829). A look reads the
    schedule, then awaits the roster and each DM before it: a cancel or a replacing schedule
    with another end may land meanwhile, and its warning must then not go. The caller sends only
    on True, with no await between the two.

    A warning is its row's, its end's and its recipient's (the Zulip ID, or none), whichever
    plan holds them (Codex rounds 3 and 5 on #829): a replacing schedule with the same end and
    recipient carries its warnings over (`store`), so it is the same warning, and a delivery's
    outcome lands on it (`warn`); another end or another recipient is another warning, which
    nothing recorded of the old one touches."""
    row = db.execute(
        "SELECT workshop, slug, plan, keep_until, ends, vms, tagged, bots, zulip_user_id, "
        "warned, state, note, changed FROM month_end WHERE workshop = ? AND slug = ?",
        (entry.workshop, entry.slug),
    ).fetchone()
    if row is None:
        return False
    now_entry = _entry(row)
    if (now_entry.ends, now_entry.zulip_user_id) != (entry.ends, entry.zulip_user_id):
        return False  # another end, or another recipient: another warning, the new one's own
    if due(now_entry, now) != kind:  # cancelled, dropped, or this warning recorded already
        return False
    warn(db, entry, kind, how, now)
    return True


def warn(db: sqlite3.Connection, entry: Entry, kind: str, how: str | None, now: float) -> None:
    """Record warning `kind` of `entry` as `how` went (module doc), replacing an earlier record
    of it; None takes it back, to be tried again. The record is the row's for that end and
    that recipient, whichever plan holds it now (Codex rounds 3 and 5 on #829), never another
    end's or another recipient's.

    A notice within `CLOSE_S` of the end covers the reminder only while it was sent or may have
    landed (`COVERS`): a notice taken back, or refused, takes its cover back too, and the
    reminder is due again (Codex round 2 on #829).

    A notice is kept with the end its DM names (`end_of`, as its claim computes it, the same
    `now` as the DM's text), and keeps it while it is recorded again, sent or perhaps sent, or
    carried over by a schedule that replaces this one (`store`): the run reads it (`run_at`),
    never recomputes it from a later approval (Codex round 1 on #852)."""
    with db:
        _record(db, entry, kind, how, now)


def _record(db: sqlite3.Connection, entry: Entry, kind: str, how: str | None, now: float) -> None:
    """`warn`'s record, in the caller's transaction."""
    row = db.execute(
        "SELECT warned FROM month_end WHERE workshop = ? AND slug = ? AND ends = ? "
        "AND zulip_user_id = ?",
        (entry.workshop, entry.slug, entry.ends, entry.zulip_user_id),
    ).fetchone()
    if row is None:  # replaced meanwhile, with another end or recipient: not this warning
        return
    old = json.loads(row[0])
    prior = next((item for item in old if item[0] == kind), None)
    items = [item for item in old if item[0] != kind]
    if kind == NOTICE:  # its cover goes with it, and comes back only as below
        items = [item for item in items if item[:1] != [REMINDER] or item[2] != COVERED]
    if how is not None:
        item: list[object] = [kind, now, how]
        if kind == NOTICE:  # the end its DM names, kept with it from its claim on (#852)
            item.append(prior[3] if prior is not None and len(prior) > 3 else end_of(entry, now))
        items.append(item)
        given = {item[0] for item in items}
        if kind == NOTICE and how in COVERS and now >= entry.ends - CLOSE_S:
            items += [[REMINDER, now, COVERED]] * (REMINDER not in given)
    db.execute(
        "UPDATE month_end SET warned = ? WHERE workshop = ? AND slug = ? AND ends = ? "
        "AND zulip_user_id = ?",
        (json.dumps(items), entry.workshop, entry.slug, entry.ends, entry.zulip_user_id),
    )


def refuse(db: sqlite3.Connection, entry: Entry, kind: str, now: float) -> bool:
    """Warning `kind` of `entry` refused by Zulip at every try (#883): recorded `REFUSED`, as
    `warn` records it, and its row stopped, if it is still scheduled for that end and recipient
    (whichever plan holds it), in one transaction: whether it stopped it. The row is never torn
    down, retired or unprotected: it stays in service until an organizer ends it by hand, or
    gives it a new end and a new schedule, which tries its warnings again (`store`); its note
    says how (`refused_note`)."""
    with db:
        _record(db, entry, kind, REFUSED, now)
        cursor = db.execute(
            "UPDATE month_end SET state = ?, note = ?, changed = ? WHERE workshop = ? AND "
            "slug = ? AND ends = ? AND zulip_user_id = ? AND state = ?",
            (STOPPED, refused_note(entry.slug, kind), now, entry.workshop, entry.slug,
             entry.ends, entry.zulip_user_id, SCHEDULED),
        )  # fmt: skip
    return cursor.rowcount == 1


def refused_note(slug: str, kind: str) -> str:
    """The note of a row a refused warning stopped (`refuse`): what ends it now."""
    return (
        f"Zulip refused its {kind} at every try: `!retire {slug}` ends it by hand, or "
        f"`!keep-until <date> {slug}`, then `!month-end schedule`, gives it a new end"
    )


def refused(entry: Entry, now: float) -> str | None:
    """The refused warning that stops `entry` at `now` (`refuse`, #883), if one does: a notice
    on record as refused, which only a release before #883 left on a scheduled row; or a
    reminder still refused once its end has come, when it can't be tried again (`due`). A
    reminder refused at a try stays on record so until it goes (`Planning._warn`), so that its
    row never runs while it waits (`run_at`), a restart or a new schedule included (Codex
    round 1)."""
    for kind, _, how in entry.warned:
        if how == REFUSED and (kind == NOTICE or now >= entry.ends):
            return kind
    return None


def key_digests(out_dir: str, slug: str) -> tuple[tuple[str, str], ...]:
    """The row's bots' identities as `store` keeps them: each `out/<slug>/<kind>.zuliprc`'s
    SHA-256, or "" when there is none."""
    found = []
    for kind in BOT_KINDS:
        try:
            with open(f"{out_dir}/{slug}/{kind}.zuliprc", "rb") as f:
                found.append((kind, hashlib.file_digest(f, "sha256").hexdigest()))
        except FileNotFoundError:
            found.append((kind, ""))
    return tuple(found)


def changed(entry: Entry, fleet: Fleet, people: Mapping[str, str]) -> str:
    """Why the scheduled row `entry` no longer matches its approval, from the roster as it is now
    (`fleet`, and `people`, each row's Zulip ID); "" when it still does (module doc)."""
    row = next((r for r in fleet.rows if r.slug == entry.slug), None)
    if row is None:
        return "no longer in the roster"
    if row.excluded:
        return "excluded from the roster (`!roster remove`)"
    if not fleet.present(entry.slug):
        return f"retired already ({fleet.retired[entry.slug]})"
    if row.keep_until != entry.keep_until:
        then = f"changed to {row.keep_until}" if row.keep_until else "cleared"
        return f"its keep_until {then} since the approval"
    if people.get(entry.slug, "") != entry.zulip_user_id:
        return "its participant's Zulip ID changed since the approval"
    if key_digests(fleet.workshop.out_dir, entry.slug) != entry.bots:
        return f"its bots' keys changed since the approval (out/{entry.slug}/)"
    return ""


def due_rows(held: Sequence[Entry], now: float) -> list[Entry]:
    """The rows a run takes now: those scheduled whose `run_at` has passed, and those a restart
    left `running`, to resume."""
    return [
        e for e in held
        if (e.state == SCHEDULED and (at := run_at(e)) is not None and at <= now)
        or e.state == RUNNING
    ]  # fmt: skip


def done(db: sqlite3.Connection, entry: Entry) -> dict[str, tuple[int, float, float]]:
    """Each stage of `entry` a run of its own schedule did, for its approved VMs: stage → (run,
    start, end). Another schedule's progress is never this one's (Codex round 1 on #835): a
    row cancelled after its sync, put back in service and scheduled again has work since,
    which its new schedule's run uninstalls and syncs again before any teardown."""
    rows = db.execute(
        "SELECT stage, plan, started, ended FROM month_end_steps "
        "WHERE workshop = ? AND slug = ? AND schedule = ? AND vms = ?",
        (entry.workshop, entry.slug, entry.plan, entry.vms),
    ).fetchall()
    return {stage: (plan, started, ended) for stage, plan, started, ended in rows}


#: A row's first pass up to its sync, what keeps its work: what a plan that reinstalls its
#: bridge, or touches the row, undoes (`unsaved`).
SAVING = ("start", "idle-stop", "waker", "uninstall", "final-sync", "export")


def unsaved(stages: Mapping[str, object], ending: Ending | None) -> list[str]:
    """The stages an earlier run did (`done`) that a resumed run does again (review of #835,
    round 2, B1): its whole first pass up to its sync (`SAVING`), unless the fleet's ledger,
    read under the run's lock, still has its work saved: its sync counting (`Ending.counts`)
    when the sync is on record, or the row uninstalled since (`Ending.uninstalled`) when only
    the uninstall is. A plan that reinstalled the bridge meanwhile (a rollout) or touched the
    row made the ledger forget them, and so did a hard kill before the run's `apply_end`: the
    cautious side. Stages before an uninstall, of which the ledger says nothing, are done
    again too. [] when the work still holds, and once the teardown is on record: the VM is
    gone, nothing can be synced again, and the teardown's guard had the sync on record before
    it (`RunHooks.before`), so the row goes on to its bots (Codex round 7 on #835)."""
    saved = [stage for stage in SAVING if stage in stages]
    if not saved or "teardown" in stages:
        return []
    if "final-sync" in stages:
        holds = ending is not None and ending.counts
    elif "uninstall" in stages:
        holds = ending is not None and bool(ending.uninstalled)
    else:
        holds = False
    return [] if holds else saved


def forget(db: sqlite3.Connection, entry: Entry, stages: Sequence[str]) -> None:
    """`stages` of `entry` no longer on record: a resumed run does them again (`unsaved`)."""
    with db:
        db.executemany(
            "DELETE FROM month_end_steps WHERE workshop = ? AND slug = ? AND schedule = ? AND "
            "vms = ? AND stage = ?",
            [(entry.workshop, entry.slug, entry.plan, entry.vms, stage) for stage in stages],
        )


def record(
    db: sqlite3.Connection, entry: Entry, stage: str, plan: int, started: float, ended: float
) -> None:
    """A stage of `entry` that exited 0, as it ends (§11.5: "each completed step")."""
    with db:
        db.execute(
            "INSERT OR REPLACE INTO month_end_steps (workshop, slug, schedule, vms, stage, plan, "
            "started, ended) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.workshop, entry.slug, entry.plan, entry.vms, stage, plan, started, ended),
        )


def vm_changed(entry: Entry, stages: Mapping[str, object], vms: str, tagged: str) -> str:
    """Why `entry`'s VMs no longer match its approval, while a step that reaches them remains
    (up to its teardown); "" when they still do, or when only its bots are left."""
    if "teardown" in stages:
        return ""
    if (vms, tagged) != (entry.vms, entry.tagged):
        return f"its VM changed since the approval: {entry.vms or 'none'} → {vms or 'none'}"
    return ""


def order(steps: Sequence[Step], finished: Mapping[str, Mapping[str, object]]) -> list[Step]:
    """A run's steps (module doc): every row's first pass, then every row's last
    (`LAST_STAGES`); without the stages a run did already (`finished`, each row's), but `start`
    while a stage that needs SSH remains; and the start of every VM at once before the rows
    only when no row is resumed."""
    mine = [step for step in steps if step.row]
    left = [step for step in mine if step.stage not in finished.get(step.row, {})]
    ssh = {step.row for step in left if step.stage in SSH_STAGES}
    keep = [step for step in mine if step in left or (step.stage == "start" and step.row in ssh)]
    whole = not any(finished.get(step.row) for step in mine)  # no row resumed
    first = [step for step in steps if not step.row and whole]
    return [
        *first,
        *(step for step in keep if step.stage not in LAST_STAGES),
        *(step for step in keep if step.stage in LAST_STAGES),
    ]


def run_of(db: sqlite3.Connection, plan: int) -> int | None:
    """The schedule a run runs, or None for any other plan."""
    row = db.execute("SELECT schedule FROM month_end_runs WHERE plan = ?", (plan,)).fetchone()
    return row[0] if row else None


def begin_run(db: sqlite3.Connection, plan: int, schedule: int, rows: Sequence[Entry]) -> None:
    """A run begins: its plan names its schedule, and its rows are `running`."""
    with db:
        db.execute("INSERT INTO month_end_runs (plan, schedule) VALUES (?, ?)", (plan, schedule))
        for e in rows:
            db.execute(
                "UPDATE month_end SET state = ?, note = ? WHERE workshop = ? AND slug = ? "
                "AND plan = ? AND state IN (?, ?)",
                (RUNNING, f"P-{plan}", e.workshop, e.slug, e.plan, *OPEN),
            )


def settle(db: sqlite3.Connection, entry: Entry, state: str, note: str, now: float) -> None:
    """A row's state once its run has ended: `done`, `stopped` or `dropped`; a cancelled row
    stays cancelled."""
    with db:
        db.execute(
            "UPDATE month_end SET state = ?, note = ?, changed = ? WHERE workshop = ? "
            "AND slug = ? AND plan = ? AND state IN (?, ?)",
            (state, note, now, entry.workshop, entry.slug, entry.plan, *OPEN),
        )


def current(db: sqlite3.Connection, entry: Entry) -> Entry | None:
    """`entry` as `state.db` has it now, if it is still the same plan's."""
    for e in entries(db, entry.workshop):
        if e.slug == entry.slug and e.plan == entry.plan:
            return e
    return None


def drop(db: sqlite3.Connection, entry: Entry, why: str, now: float) -> bool:
    """`entry` dropped out, if it is still scheduled under its plan: whether it was (review of
    #829, N2: a cancel or a replacement may have landed meanwhile)."""
    with db:
        cursor = db.execute(
            "UPDATE month_end SET state = ?, note = ?, changed = ? "
            "WHERE workshop = ? AND slug = ? AND plan = ? AND state = ?",
            (DROPPED, why, now, entry.workshop, entry.slug, entry.plan, SCHEDULED),
        )
    return cursor.rowcount == 1


def _day(ts: float) -> tuple[str, str, str]:
    """`Monday, November 30, 2026`, `15:00` and `November 30`, in UTC."""
    t = time.gmtime(ts)
    month = time.strftime("%B", t)
    day = time.strftime(f"%A, {month} {t.tm_mday}, %Y", t)
    return day, time.strftime("%H:%M", t), f"{month} {t.tm_mday}"


def _repo(repo: str, where: str = " on GitHub") -> str:
    """The repository as the DMs name it: linked, followed by `where`, or in words when the
    roster has none. The reminder's text has the link bare (review of #829, B1)."""
    if _REPO.fullmatch(repo):
        return f"[{repo}](https://github.com/{repo}){where}"
    return "your repository on GitHub"


def notice(entry: Entry, workshop: str, repo: str, end: float | None = None) -> str:
    """The first warning's text (§11.5; the maintainer read it on #570), naming `end`, the
    row's own unless the notice is late (`end_of`)."""
    day, at, short = _day(entry.ends if end is None else end)
    slug = entry.slug
    return (
        f"**Your workshop VM ends on {day}, at {at} UTC.**\n\n"
        f"The organizers of {workshop} have scheduled the end of the VM that runs your bots "
        f"Claude · {slug} and Codex · {slug}. From that time on, your bots may stop answering "
        "at any moment.\n\n"
        "**What is kept:** before the VM is deleted, your agents stop, and the work in your "
        "repository on the VM, with all its topic branches, is committed and pushed to "
        f"{_repo(repo)}. Your repository and your Zulip channels stay as they are.\n\n"
        "**What is not pushed:** your agents' session transcripts, anything outside your "
        "repository on the VM, files over 50 MB, files your `.gitignore` excludes, `.lake` "
        "build folders, and anything that looks like a credential. If you want an archive of "
        "your session transcripts and your work folder, or more time, ask the workshop's "
        f"organizers before {short}. Nobody reads replies to this message.\n\n"
        "**Before then,** sign the VM out of your accounts: ask one of your agents to run "
        "`codex logout` and `claude auth logout`. Once the VM is gone, revoke its sessions in "
        "your ChatGPT and Claude account settings."
    )


def reminder(entry: Entry, workshop: str, repo: str) -> str:
    """The second warning's text (§11.5; the maintainer read it on #570), naming the end the
    run keeps (`run_at`): the notice's, kept with it, which a late notice moved a day after it
    (#852's review, N1), or the row's own."""
    day, at, _ = _day(entry.named if entry.named is not None else entry.ends)
    slug = entry.slug
    return (
        f"**Reminder: your workshop VM ends on {day}, at {at} UTC.** From then on, your bots "
        f"Claude · {slug} and Codex · {slug} may stop answering at any moment. The work in your "
        f"repository on the VM is pushed to {_repo(repo, '')} before the VM is deleted; session "
        "transcripts and anything outside the repository are not, unless you have asked the "
        "organizers for an archive. If you haven't yet, ask one of your agents to run "
        "`codex logout` and `claude auth logout`. Nobody reads replies to this message."
    )


#: Where a cut `show` names its attachment (`reads.month_end`, Codex round 2 on #824).
ATTACH = "\x00attachment\x00"
#: The room a cut `show` keeps for what replaces `ATTACH`: the attachment's Markdown link,
#: `[month-end.txt](/user_uploads/<realm>/<..>/<key>/month-end.txt).`, about 80 characters.
LINK_ROOM = 160


def show(db: sqlite3.Connection, fleet: Fleet, limit: int | None = None) -> tuple[str, str | None]:
    """`!month-end show`: the schedule's entries, then the rows with a `keep_until` it doesn't
    hold; every time in UTC. A scheduled row that no longer matches its approval (`changed`,
    but its Zulip ID, which `fleet` doesn't hold) says that it drops out at the next look
    (review of #824, N2).

    Within `limit` characters: the whole of it, and None. Otherwise a bounded answer, with
    `ATTACH` where the attachment's link goes and `LINK_ROOM` kept for it: its head, a count of
    its rows by state, the entries that fit, the line of the rows it doesn't hold and the
    notice; and the whole of it, to attach (Codex round 2 on #824: at 170 rows, the whole is
    over Zulip's 10,000 characters, and `AdminBot.deliver` would cut its end)."""
    ws = fleet.workshop.name
    setup(db)  # a server without a planner made none
    held = entries(db, ws)
    rows = [f"- {e.slug}: ends {utc_words(e.ends)}; {_state(e, fleet)}" for e in held]
    if held:
        plans = ", ".join(_name(plan) for plan in sorted({e.plan for e in held}))
        head = f"**The month end of {ws}**, as {plans} scheduled it:"
    else:
        head = f"No month end is scheduled in {ws}."
    lines = []
    inside = {e.slug for e in held}
    rest = [r for r in fleet.rows if r.keep_until and fleet.in_all(r.slug) and r.slug not in inside]
    if rest:
        named = ", ".join(f"{r.slug} ({r.keep_until})" for r in rest[:20])
        more = f", and {len(rest) - 20} more" * (len(rest) > 20)
        lines.append(
            f"Kept until a date, not in the schedule: {named}{more}. `!month-end schedule` "
            "schedules every row in service with a `keep_until`."
        )
    elif not held:
        lines.append(
            "No row in service has a `keep_until` either: `!keep-until` gives the kept rows "
            "their end, then `!month-end schedule` schedules them."
        )
    whole = "\n".join([head, *rows, *lines])
    if limit is None or len(whole) <= limit:
        return whole, None
    tally = Counter(_WORDS.get(e.state, e.state) for e in held)
    count = f"{counted(len(held))}: " + ", ".join(f"{n} {s}" for s, n in tally.items()) + "."
    cut = "*Cut to fit: {} of {} shown. The whole schedule: " + ATTACH + "*"
    fixed = "\n".join([head, count, cut.format(len(held), counted(len(held))), *lines])
    room = limit - LINK_ROOM - len(fixed)
    shown = []
    for row in rows:
        room -= len(row) + 1
        if room < 0:
            break
        shown.append(row)
    return "\n".join(
        [head, count, *shown, cut.format(len(shown), counted(len(held))), *lines]
    ), whole


def keep_until_note(
    held: Sequence[Entry], slugs: Sequence[str], value: str, workshop: str, run: str | None
) -> list[str]:
    """A dated `!keep-until`'s notes on how its rows (`slugs`) end, from the workshop's schedule
    as `state.db` has it when the plan is made (`held`, `entries`; #868, ORGANIZERS §5.7).

    - A row `running` is ending now, whatever this date. In `run`, the month-end run applying
      in the workshop, this plan can't apply until that run ends (its approval is answered
      `busy:`). Without one, it is in a run cut short (a restart, an error), which a later look
      resumes, unless this applies first and drops the row. `!month-end cancel` stops it.
    - A row `scheduled` at that same `keep_until` keeps its place, and its warnings.
    - No other row ends on its own until a schedule approved after this plan's apply holds it:
      one scheduled at another end drops out at the bot's next look (`changed`), and a row the
      schedule doesn't hold, or every row when none is open, is in no schedule.

    Shown in the plan, never hashed."""
    live = {e.slug: e for e in held if e.state in OPEN}
    plans = ", ".join(_name(plan) for plan in sorted({e.plan for e in live.values()}))
    ending = [s for s in slugs if s in live and live[s].state == RUNNING]
    same = [s for s in slugs if s in live and s not in ending and live[s].keep_until == value]
    moved = [s for s in slugs if s in live and s not in ending and s not in same]
    left = [s for s in slugs if s not in live or s in moved]
    days = NOTICE_S // 86400
    notes = []
    if ending:
        it, each = ("it", "it") if len(ending) == 1 else ("them", "each")
        rows = " ".join(ending) if len(ending) <= 5 else "<row>"
        if run is not None:
            notes.append(
                f"**Ending now**, in the month-end run {run}: {listed(ending)}. This plan can't "
                f"apply until that run ends, and the run goes on with {it}: "
                f"`!month-end cancel {rows}` stops {each} before its next step (ORGANIZERS §5.7)."
            )
        else:
            notes.append(
                "**In a month-end run cut short**, which a later look of the bot resumes: "
                f"{listed(ending)}. That run goes on with {it} unless this applies first, which "
                f"drops {it} out: `!month-end cancel {rows}` stops {each} at once (ORGANIZERS "
                "§5.7)."
            )
    if same:
        notes.append(
            f"**Already in the month-end schedule** ({plans}) at that end: {listed(same)}, with "
            f"its warnings: the notice by DM {days} days before (at once, when approved later "
            f"than that), and a reminder {REMINDER_S // 86400} day before unless the notice "
            "covers it. `!month-end show` says which went, and when each row ends."
        )
    if not left:
        return notes
    new = (
        f"a new `!month-end schedule`, which replaces {plans}'s,"
        if live
        else "`!month-end schedule`"
    )
    how = (
        f"Once this applies, {new} ends each row at that time, with its notice by DM {days} days "
        "before (at once, when approved later than that); or end each VM with `!retire` then "
        "(ORGANIZERS §5.7)."
    )
    if not live:
        notes.append(
            f"**No row of {workshop} is scheduled to end**, so nothing ends on its own at that "
            f"time. {how}"
        )
        return notes
    rest = [s for s in left if s not in moved]
    why = []
    if moved:
        why.append(f"this new date drops {listed(moved)} out of it at the bot's next look")
    if rest:
        why.append(f"{listed(rest)} {'is' if len(rest) == 1 else 'are'} not in it")
    notes.append(
        f"**The month-end schedule** ({plans}) won't end {listed(left)} at that time: "
        f"{', and '.join(why)}. {how}"
    )
    return notes


#: Each state as `!month-end show` says it.
_WORDS = {
    SCHEDULED: "scheduled",
    RUNNING: "ending now",
    DONE: "ended",
    STOPPED: "stopped",
    DROPPED: "dropped out",
    CANCELLED: "cancelled",
}


#: How `look_line` names each outcome of a look, in its order.
_LOOK = (
    (f"{NOTICE} {SENT}", "the notice went by DM to"),
    (f"{REMINDER} {SENT}", "the reminder went by DM to"),
    (f"{NOTICE} {NO_ID}", "no notice could go, with no Zulip ID in the roster, to"),
    (f"{REMINDER} {NO_ID}", "no reminder could go, with no Zulip ID in the roster, to"),
    (f"{NOTICE} {REFUSED}", "Zulip refused the notice, at every try, to"),
    (f"{REMINDER} {REFUSED}", "Zulip refused the reminder, at every try, to"),
    (f"{NOTICE} {UNSURE}", "the notice may not have reached"),
    (f"{REMINDER} {UNSURE}", "the reminder may not have reached"),
    ("dropped", "dropped out of the schedule, to end only with a new `!month-end schedule` or a "
                "`!retire`:"),
    ("retired", "marked retired, every step of their run done before a restart or a record "
                "that failed:"),
    ("unrecorded", "stopped, every step of their run done but their bots' deactivation not on "
                   "the audit log, so not retired: `!retire <row> unsynced` ends each:"),
)  # fmt: skip


def look_line(plan: str, heard: Mapping[str, Sequence[str]]) -> str:
    """What one look of the schedule did (`Planning.month_end`), for its topic: the rows each
    warning went to, those it couldn't, and the rows that dropped out, each with why; when a
    list is cut, where to read it whole (review of #829, N4)."""
    parts = [f"{said} {listed(list(heard[key]))}{_after(key, heard[key])}"
             for key, said in _LOOK if heard.get(key)]  # fmt: skip
    told = "" if all(k in ("dropped", "retired", "unrecorded") or k.endswith(SENT)
                     for k in heard) else (
        " Tell those participants yourselves."
    )  # fmt: skip
    cut = any(len(rows) > 5 for rows in heard.values())
    whole = " `!month-end show` lists every row." * cut
    return f"📨 **{plan}**, the month end: " + "; ".join(parts) + "." + told + whole


def _after(key: str, rows: Sequence[str]) -> str:
    """What follows the rows of `key` in a look's line (#883): for a notice that can't go, with
    no Zulip ID, that the row ends all the same, as its approval said; for a refused warning,
    that the row stopped, and what ends it now (`refuse`)."""
    one = len(rows) == 1
    if key == f"{NOTICE} {NO_ID}":
        return ", whose VM ends at its date all the same, as approved" if one else (
            ", whose VMs end at their dates all the same, as approved"
        )  # fmt: skip
    if not key.endswith(f" {REFUSED}"):
        return ""
    row, whose = (rows[0], "whose row stopped before any step and stays") if one else (
        "<row>", "whose rows stopped before any step and stay"
    )  # fmt: skip
    return (
        f", {whose} in service: `!retire {row}` ends {'it' if one else 'each'} by hand, or "
        f"`!keep-until <date> {row}`, then `!month-end schedule`, gives it a new end"
    )


def _state(e: Entry, fleet: Fleet) -> str:
    if e.state == SCHEDULED and (why := changed(e, fleet, {e.slug: e.zulip_user_id})):
        return f"scheduled, but drops out at the bot's next look: {why}"
    at = run_at(e)
    if e.state == SCHEDULED and at is not None and at != e.ends:
        return f"its notice went late: it ends {utc_words(at)}, a day after it" + "".join(
            f"; {kind} {how}" for kind, _, how in e.warned if how != COVERED
        )
    warned = [f"{kind} {how} {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(at))}"
              if how != COVERED else f"{kind} covered by the notice"
              for kind, at, how in e.warned]  # fmt: skip
    return _said(e) + "".join(f"; {w}" for w in warned)


def _said(e: Entry) -> str:
    said = _WORDS.get(e.state, e.state)
    return f"{said} ({e.note})" if e.note else said


def _name(plan: int) -> str:
    return f"P-{plan}"


def counted(n: int) -> str:
    """`1 row`, `170 rows`."""
    return f"{n} row" + "s" * (n != 1)


def listed(slugs: Sequence[str]) -> str:
    """Rows as an answer names them: the first five, then how many more."""
    more = f" and {len(slugs) - 5} more" if len(slugs) > 5 else ""
    return ", ".join(slugs[:5]) + more
