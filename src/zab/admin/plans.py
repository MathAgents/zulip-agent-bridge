"""Plans: their IDs, hashes, expiry and staleness, in `state.db` (docs/ADMIN.md §2.4, §1.3).

```
begin ─▶ PLANNING ─post─▶ PENDING ─approvals complete─▶ APPROVED ─begin_apply─▶ APPLYING ─▶ DONE
            │                ├─ !reject ─▶ REJECTED                  │                    └─▶ FAILED
            ├─▶ REFUSED      └─ TTL ─────▶ EXPIRED                   └─ hash differs ─▶ STALE
            └─▶ UNCHANGED: its dry run says there is nothing to change (#708)
restart: PLANNING, APPROVED and APPLYING ─▶ INTERRUPTED; PENDING waits for its approvals or its TTL
```

- **IDs.** `P-<n>`, unique on the server, so that `P-42` names one plan whichever bot posted it
  (§2.1). SQLite's AUTOINCREMENT never hands out an ID twice, across restarts and even after a
  row is deleted. `state.db` is not in the backups (§1.7), and a rebuilt one looks like a new
  server's, so `floor` is required at every start: the highest plan ID in the audit log, or in
  the `runs/P-<n>/` names if higher (0 on a new server). For that, the bot (PR 9) audits every
  plan it begins, refused ones too, so the audit log holds every ID handed out.
- **Bound to its bot.** A plan records the workshop and the user ID of the bot that took the
  request, and its post refuses an action of another workshop, another bot or another command.
  `approvals.py` refuses an approval sent to any other bot. Its `Approvals.post` is the one way
  to post a plan: it computes the plan's class from the action itself.
- **Its own files.** Every `P-<n>` directory in its argument lists and inputs is its own, and a
  `!roster import` has its upload, `runs/P-<n>/roster-upload.csv`, among the inputs, so the
  upload's digest is in the hash: approving P-12 never applies P-13's upload.
- **Hash.** `fingerprint` is `actions.plan_hash` over the inputs as they are now. `!approve`
  names the first six hex digits (`approvals.py`); `begin_apply` compares the full hash,
  recomputed at apply, and any difference makes the plan STALE (§2.4, step 5).
- **TTL.** `plan_ttl_minutes` from the post for the approvals (§2.5), and again from the last
  approval for the apply to begin: an approved plan not begun within it EXPIRES, so an old
  approval never applies hours later.
- **Message order.** A plan keeps its plan message's and its approval message's IDs; an
  approval counts only if its own ID is greater than the approval message's (§2.5).

**One process owns the store.** `PlanStore` holds an exclusive `flock` on `state.db.lock`, so a
second opener (a second bot, or a script calling `recover()`) fails rather than marking a live
apply INTERRUPTED. The runner (PR 10) applies inside the bot's process, through the bot's store.
Its event loop calls the store synchronously, so nothing interleaves inside a call; each state
change is still a compare-and-set.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zab.admin.actions import Action, Live, digests, plan_hash

PLANNING, PENDING, APPROVED, APPLYING = "planning", "pending", "approved", "applying"
DONE, FAILED, REFUSED, REJECTED = "done", "failed", "refused", "rejected"
EXPIRED, STALE, INTERRUPTED, UNCHANGED = "expired", "stale", "interrupted", "unchanged"
_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PLAN_DIR = re.compile(r"/P-([0-9]+)(?=/|$)", re.ASCII)  # runs/P-<n>/…, tmp/P-<n>
_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workshop TEXT NOT NULL,     -- '' for a server-level plan, on the [global] bot
    bot INTEGER NOT NULL,       -- the user ID of the bot that took the request
    requester INTEGER NOT NULL,
    command TEXT NOT NULL,      -- normalized, as Command.text() gives it
    state TEXT NOT NULL,
    created REAL NOT NULL,
    requirement TEXT,           -- JSON [class, approvers needed, [phrase…]], from post on
    targets TEXT NOT NULL DEFAULT '[]', hash TEXT NOT NULL DEFAULT '', expires REAL,
    plan_message INTEGER, approval_message INTEGER, note TEXT NOT NULL DEFAULT '',
    approved REAL               -- when its approvals were complete
);
"""
_SELECT = (  # Plan's fields, in order
    "SELECT id, workshop, bot, requester, command, state, created, requirement, targets, hash, "
    "expires, plan_message, approval_message, note, approved FROM plans WHERE id = ?"
)


class PlanError(ValueError):
    """A call that the plan's state or record doesn't allow: nothing changed."""


@dataclass(frozen=True, slots=True)
class Requirement:
    """What approving a plan takes (§2.5): its class, the number of distinct approvers, and the
    typed phrase that follows `!approve P-n hash6`, empty when there is none."""

    klass: str
    needed: int = 1
    phrase: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    id: int
    workshop: str
    bot: int
    requester: int
    command: str
    state: str
    created: float
    requirement: Requirement | None  # from `post` on
    targets: tuple[str, ...]
    hash: str  # the full SHA-256, from `post` on
    expires: float | None
    plan_message: int | None
    approval_message: int | None
    note: str  # why it was refused, rejected, stale, expired or interrupted
    approved: float | None  # when its approvals were complete

    @property
    def name(self) -> str:
        return f"P-{self.id}"


def fingerprint(action: Action, *, release: str, live: Live) -> str:
    """The plan's hash as of now: the inputs' current digests and the verb's target material."""
    return plan_hash(action, release=release, digests=digests(action), live=live)


def _plan_dirs(action: Action) -> set[str]:
    """The `<n>` of every `P-<n>` directory named in the action's lists and inputs."""
    lists = [argv for step in action.steps for argv in (step.argv, step.dry_run or ())]
    words = [*(word for argv in lists for word in argv), *action.inputs]
    return {n for word in words for n in _PLAN_DIR.findall(word)}


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


class PlanStore:
    """The plans of every workshop on this server, in `state.db` (§1.3)."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_minutes: int,
        floor: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`floor`: the highest plan ID this server has used (above); the next is above it."""
        if not (_positive(ttl_minutes) and type(floor) is int and floor >= 0):
            raise PlanError("expected a positive TTL and a plan ID floor")
        self.ttl, self.clock = ttl_minutes * 60, clock
        self._lock = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock)
            raise PlanError(f"{path} is open in another process, which owns it") from None
        try:  # a store that fails to open owns nothing: the lock goes
            self.db = sqlite3.connect(str(path), timeout=5.0)
        except BaseException:
            os.close(self._lock)
            raise
        try:
            self._setup(floor)
        except BaseException:
            self.close()  # the connection, then the lock
            raise

    def _setup(self, floor: int) -> None:
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript(_SCHEMA)
        with self.db:
            seq = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'plans'")
            row = seq.fetchone()
            if row is None:
                self.db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('plans', 0)")
            if floor > (row[0] if row else 0):
                self.db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'plans'", (floor,))

    def close(self) -> None:
        self.db.close()
        if self._lock >= 0:  # once: the descriptor's number may be reused afterwards
            os.close(self._lock)
            self._lock = -1

    def begin(self, *, workshop: str, bot: int, requester: int, command: str) -> Plan:
        """A new plan, PLANNING, for `command` as `requester` sent it to `bot`. Its ID names the
        plan's TMPDIR and upload directory, so it comes before the action is built."""
        if not (_positive(bot) and _positive(requester) and command.startswith("!")):
            raise PlanError("a plan needs its bot, its requester and its command")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO plans (workshop, bot, requester, command, state, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (workshop, bot, requester, command, PLANNING, self.clock()),
            )
        assert cursor.lastrowid is not None
        return self.need(cursor.lastrowid)

    def _post(
        self,
        plan_id: int,
        action: Action,
        requirement: Requirement,
        plan_hash: str,
        *,
        plan_message: int,
        approval_message: int,
    ) -> Plan:
        """PLANNING → PENDING, once the plan and approval messages are posted: its TTL starts.
        Only through `Approvals.post`, which computes `requirement` from the action."""
        plan = self.need(plan_id)
        if (action.workshop, action.bot) != (plan.workshop, plan.bot):
            raise PlanError(f"{plan.name} was requested from another bot")
        if " ".join(("!" + action.verb, *action.args)) != plan.command:
            raise PlanError(f"the action is not {plan.name}'s command")
        if _plan_dirs(action) - {str(plan.id)}:
            raise PlanError(f"the action names another plan's files than {plan.name}'s")
        upload = f"/runs/{plan.name}/roster-upload.csv"
        if action.verb == "roster import" and not any(p.endswith(upload) for p in action.inputs):
            raise PlanError(f"{plan.name}'s upload is runs/{plan.name}/roster-upload.csv")
        if not _HASH.fullmatch(plan_hash):
            raise PlanError("expected the plan's full SHA-256")
        if not (_positive(requirement.needed) and isinstance(requirement.phrase, tuple)):
            raise PlanError("a requirement needs at least one approver")
        if not (_positive(plan_message) and _positive(approval_message)):
            raise PlanError("expected the messages' IDs")
        if approval_message <= plan_message:
            raise PlanError("the approval message follows the plan message")
        if plan.state != PLANNING:
            raise PlanError(f"{plan.name} is {plan.state}, not planning")
        needs = json.dumps([requirement.klass, requirement.needed, requirement.phrase])
        values = (needs, json.dumps(action.targets), plan_hash, self.clock() + self.ttl)
        with self.db:
            cursor = self.db.execute(
                "UPDATE plans SET state = ?, requirement = ?, targets = ?, hash = ?, expires = ?, "
                "plan_message = ?, approval_message = ? WHERE id = ? AND state = ?",
                (PENDING, *values, plan_message, approval_message, plan_id, PLANNING),
            )
        return self._check(cursor, plan)

    def refuse(self, plan_id: int, why: str) -> Plan:
        """PLANNING → REFUSED: validation or a dry run failed, nothing to approve (§2.4)."""
        return self.move(plan_id, (PLANNING,), REFUSED, why)

    def unchanged(self, plan_id: int, why: str) -> Plan:
        """PLANNING → UNCHANGED: its dry run says there is nothing to change, so there is nothing
        to approve, and nothing went wrong (#708)."""
        return self.move(plan_id, (PLANNING,), UNCHANGED, why)

    def record(self, plan_id: int, write: Callable[[sqlite3.Connection], bool]) -> Plan:
        """One transaction: `write` records an approval (`approvals.py`) and says whether the
        plan's approvals are now complete; if they are, PENDING → APPROVED, with the time. The
        approval and the move commit together or not at all, so a crash never leaves a plan
        PENDING with all its approvals recorded, which no resent approval could complete."""
        plan = self.need(plan_id)
        with self.db:  # an exception rolls the approval back too
            if write(self.db):
                cursor = self.db.execute(
                    "UPDATE plans SET state = ?, approved = ? WHERE id = ? AND state = ?",
                    (APPROVED, self.clock(), plan_id, PENDING),
                )
                if cursor.rowcount != 1:
                    raise PlanError(f"{plan.name} changed meanwhile")
        return self.need(plan_id)

    def begin_apply(self, plan_id: int, current: str) -> Plan:
        """APPROVED → APPLYING if the apply begins within one TTL of the last approval, and
        `current`, the plan's `fingerprint` recomputed now, is the approved hash. Otherwise →
        EXPIRED or STALE, and the organizer requests again (§2.4, step 5)."""
        if (late := self.late(plan_id)) is not None:
            return late
        plan = self.need(plan_id)
        if plan.state == APPROVED and current != plan.hash:
            return self.move(plan_id, (APPROVED,), STALE, f"hash now {current[:12]}")
        return self.move(plan_id, (APPROVED,), APPLYING)

    def late(self, plan_id: int) -> Plan | None:
        """An APPROVED plan whose apply hasn't begun within one TTL of its last approval →
        EXPIRED, as `begin_apply` makes it; None for any other plan, left as it is."""
        plan = self.need(plan_id)
        if plan.state == APPROVED and self.clock() >= (plan.approved or 0) + self.ttl:
            return self.move(plan_id, (APPROVED,), EXPIRED, "not applied within the TTL")
        return None

    def finish(self, plan_id: int, ok: bool) -> Plan:
        return self.move(plan_id, (APPLYING,), DONE if ok is True else FAILED)

    def expire_due(self) -> list[Plan]:
        """PENDING plans past their TTL → EXPIRED, for the bot to say so (§2.5)."""
        rows = self.db.execute(
            "SELECT id FROM plans WHERE state = ? AND expires <= ?", (PENDING, self.clock())
        ).fetchall()
        return [self.move(row[0], (PENDING,), EXPIRED) for row in rows]

    def recover(self) -> list[Plan]:
        """At start: a plan that was being planned, approved or applied → INTERRUPTED; nothing
        resumes on its own (§2.4, step 6). A PENDING plan stays pending until it expires."""
        live = (PLANNING, APPROVED, APPLYING)
        rows = self.db.execute("SELECT id FROM plans WHERE state IN (?, ?, ?)", live).fetchall()
        return [self.move(row[0], live, INTERRUPTED, "the bot restarted") for row in rows]

    def pending(self, bot: int) -> list[Plan]:
        """The bot's own pending plans, for `!plans`."""
        rows = self.db.execute(
            "SELECT id FROM plans WHERE bot = ? AND state = ? AND expires > ? ORDER BY id",
            (bot, PENDING, self.clock()),
        ).fetchall()
        return [self.need(row[0]) for row in rows]

    def get(self, plan_id: int) -> Plan | None:
        row = self.db.execute(_SELECT, (plan_id,)).fetchone()
        if row is None:
            return None
        needs = json.loads(row[7]) if row[7] else None
        requirement = Requirement(needs[0], needs[1], tuple(needs[2])) if needs else None
        return Plan(*row[:7], requirement, tuple(json.loads(row[8])), *row[9:])

    def need(self, plan_id: int) -> Plan:
        plan = self.get(plan_id)
        if plan is None:
            raise PlanError(f"no plan P-{plan_id} on this server")
        return plan

    def move(self, plan_id: int, before: tuple[str, ...], after: str, note: str = "") -> Plan:
        """A compare-and-set of the state: PlanError, and no change, if it isn't in `before`."""
        plan = self.need(plan_id)
        if plan.state not in before:
            raise PlanError(f"{plan.name} is {plan.state}, not {' or '.join(before)}")
        with self.db:
            cursor = self.db.execute(
                "UPDATE plans SET state = ?, note = ? WHERE id = ? AND state = ?",
                (after, note, plan_id, plan.state),
            )
        return self._check(cursor, plan)

    def _check(self, cursor: sqlite3.Cursor, plan: Plan) -> Plan:
        if cursor.rowcount != 1:
            raise PlanError(f"{plan.name} changed meanwhile")
        return self.need(plan.id)
