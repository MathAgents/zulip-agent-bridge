"""The plans in flight on the bots, for a deploy that restarts them (docs/ADMIN.md §2.4 step 6;
#989).

A deploy restarts the bots, and the restart interrupts every plan being planned, approved or
applying (§2.4 step 6). So `deploy.sh`'s upgrade asks this module first, as `zab-admin`, from
the release it deploys (`python -m zab.admin.inflight`), after the build and before anything is
installed. The in-band upgrade's plan names the same plans in its notes (`notes`), and its
approval is answered `busy:` while one applies (the apply lock, §2.4 step 5). Each plan, by its
state:

- **applying, or approved** (about to apply; a month-end run applies too): the deploy waits for
  it, up to `--wait` seconds, then refuses, naming each with its bot;
- **pending**: it stays pending through the restart, but its hash names the running release
  (§2.4 step 3), so its approval after the deploy finds it STALE: named, so that it is
  requested again;
- **being planned**: named; the restart interrupts it, and its requester requests it again.

`state.db` is read read-only, never through `PlanStore`, whose lock the running bot holds: a
second store would fail to open, and one that took the lock would keep the bot from starting.
Whether a bot runs is that lock. Held, the rows are live. Free, no bot runs (stopped, or failing
to start): nothing is in flight, and the next start marks what the last one left (`recover`).
The probe takes it shared for an instant, non-blocking; a bot that starts in that instant fails
its own take, and systemd starts it again 5 s later (`Restart=always`).

**Exit status:** 0 when nothing applies or is approved, or no bot runs; 1 when something still
does after the wait, or the plans can't be read (`state.db`, its lock, or a row out of shape);
2 for a usage error. With `--report`, for `deploy.sh --interrupt-plans`, it never waits, and
exits 0 unless the plans can't be read: then 1, which `deploy.sh` records as `unknown`, never as
the empty list of a check that found none. Its lines for the deployer go to stderr, made of
fixed words and each plan's ID, workshop and verb, each checked (`_WORKSHOP`, `_VERB`); an
error is one line, never a traceback. Stdout is one line: the IDs of the plans the restart would
cut (applying, approved or being planned), which `deploy.sh` records with `--interrupt-plans`.

Standard library only: it reads what the running release's bot wrote, and runs in any release's
venv.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

#: `plans.py`'s states, the ones a restart reaches (a test keeps them equal).
PLANNING, PENDING, APPROVED, APPLYING = "planning", "pending", "approved", "applying"
LIVE = (PLANNING, PENDING, APPROVED, APPLYING)
STATE_DIR = "/var/lib/zab-admin"
EVERY = 2.0  # seconds between two reads while it waits
SAY_EVERY = 60.0  # seconds between two of its waiting lines
#: What may be shown of a row: a workshop name as admin.toml's loader takes it, and a verb.
_WORKSHOP = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
_VERB = re.compile(r"[a-z][a-z-]{0,39}", re.ASCII)
MONTH_END = "a month-end run of P-"  # the note of a month-end run's plan (`planning._run`)


@dataclass(frozen=True, slots=True)
class Flight:
    """A plan a restart would reach: planning, pending (not past its TTL), approved or applying."""

    id: int
    workshop: str  # "" for the server's bot
    verb: str  # the command's first word, without its `!`; "?" when it isn't one
    state: str
    since: float  # its approval for one approved or applying, else its request
    month_end: bool = False

    @property
    def name(self) -> str:
        return f"P-{self.id}"

    @property
    def holds(self) -> bool:
        """Applying, or about to: a deploy waits for it."""
        return self.state in (APPROVED, APPLYING)

    @property
    def cut(self) -> bool:
        """The restart interrupts it (a pending plan stays pending)."""
        return self.state != PENDING

    @property
    def what(self) -> str:
        return "a month-end run" if self.month_end else f"!{self.verb}"


def bot_name(workshop: str) -> str:
    return f"{workshop}'s bot" if workshop else "the server's bot"


def flights(db: sqlite3.Connection, now: float) -> list[Flight]:
    """Every plan of `db` that a restart would reach, by ID; a pending one past its TTL is
    left out (the bot's next tick expires it)."""
    rows = db.execute(
        "SELECT id, workshop, command, state, created, approved, expires, note FROM plans "
        "WHERE state IN (?, ?, ?, ?) ORDER BY id",
        LIVE,
    ).fetchall()
    found = []
    for pid, workshop, command, state, created, approved, expires, note in rows:
        if state == PENDING and not (isinstance(expires, (int, float)) and expires > now):
            continue
        verb = str(command).removeprefix("!").split(" ", 1)[0]
        workshop = str(workshop)
        found.append(
            Flight(
                id=int(pid),
                workshop=workshop if not workshop or _WORKSHOP.fullmatch(workshop) else "?",
                verb=verb if _VERB.fullmatch(verb) else "?",
                state=state,
                since=float(approved if state in (APPROVED, APPLYING) and approved else created),
                month_end=str(note).startswith(MONTH_END),
            )
        )
    return found


def line(flight: Flight, now: float) -> str:
    """One plan, for the deployer: `P-41, applying on zab-test's bot (!rollout) for 18 min`."""
    minutes = max(0, int((now - flight.since) // 60))
    state = "being planned" if flight.state == PLANNING else flight.state
    where = bot_name(flight.workshop)
    return f"{flight.name}, {state} on {where} ({flight.what}) for {minutes} min"


def notes(found: list[Flight]) -> list[str]:
    """The in-band upgrade's notes on the plans in flight (shown, never hashed: §2.4 step 3)."""

    def named(*states: str) -> str:
        return ", ".join(
            f"{f.name} on {bot_name(f.workshop)} (`{f.what}`)" for f in found if f.state in states
        )

    said = []
    if busy := named(APPLYING, APPROVED):
        said.append(
            f"**Applying now:** {busy}. This plan's approval is answered `busy:` until each "
            "has ended: the bots' restart, after this plan's result, would interrupt it."
        )
    if pending := named(PENDING):
        said.append(
            f"**Pending now:** {pending}. Each stays pending through the bots' restart, but its "
            "hash names the release that runs now: approved after the upgrade, it is STALE, and "
            "is requested again."
        )
    if planning := named(PLANNING):
        said.append(
            f"**Being planned now:** {planning}. The bots' restart, after this plan's result, "
            "interrupts each one still being planned then: its requester requests it again."
        )
    return said


# ------------------------------------------------------------------------- the state files


def bot_runs(state_dir: Path) -> bool:
    """Whether a bot holds `state.db.lock`, as `PlanStore` does while it runs (module doc)."""
    try:
        fd = os.open(state_dir / "state.db.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # the shared lock, if taken, goes with it
    return False


def read(state_dir: Path, now: float) -> list[Flight]:
    """The plans in flight, as `state.db` has them now, read read-only; [] without one."""
    path = state_dir / "state.db"
    if not path.exists():
        return []
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as db:
        return flights(db, now)


def _why(exc: BaseException) -> str:
    return re.sub(r"[^ -~]", "?", f"{type(exc).__name__}: {exc}")[:300]


def fate(flight: Flight, report: bool) -> str:
    """What the deploy's restart does to `flight`, for its line."""
    if flight.holds:
        return "the deploy interrupts it" if report else "the deploy refuses until it has ended"
    if flight.state == PENDING:
        return (
            "it stays pending, but its hash names the running release: approved after the "
            "deploy, it is STALE, and is requested again"
        )
    return "the bots' restart interrupts it, and its requester requests it again"


@dataclass
class Check:
    """One run of the check: its exit status, and the plans in flight at its end."""

    status: int
    found: list[Flight]


def check(
    state_dir: Path,
    *,
    wait: float,
    report: bool,
    err: TextIO,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Check:
    """The check (module doc): it reads, waits up to `wait` seconds while a plan applies or is
    approved (never with `report`), then says what it found."""
    deadline, said = monotonic() + wait, None
    while True:
        try:
            runs = bot_runs(state_dir)
        except OSError as exc:  # with --report too: unknown, never "none" (review of #1034, B1)
            err.write(f"state.db.lock can't be read ({_why(exc)}): the plans are unknown.\n")
            return Check(1, [])
        if not runs:
            err.write(
                "No bot holds state.db (zab-admin-bot.service is stopped, or doesn't start): "
                "nothing is in flight. Its next start marks interrupted any plan the last one "
                "left being planned, approved or applying (ADMIN 2.4, step 6).\n"
            )
            return Check(0, [])
        try:
            found = read(state_dir, clock())
        except Exception as exc:  # sqlite3's, the file's, or a row out of shape: one line (N3)
            err.write(f"state.db can't be read ({_why(exc)}): the plans in flight are unknown.\n")
            return Check(1, [])
        holding = [f for f in found if f.holds]
        if report or not holding or monotonic() >= deadline:
            break
        if said is None or monotonic() - said >= SAY_EVERY:
            left = max(0, int((deadline - monotonic() + 59) // 60))
            names = "; ".join(line(f, clock()) for f in holding)
            err.write(f"Waiting for {names}: at most {left} min more (--plan-wait).\n")
            said = monotonic()
        sleep(EVERY)
    now = clock()
    if not found:
        err.write("No plan is in flight on the bots.\n")
    else:
        err.write("The plans in flight on the bots:\n")
    for f in found:
        err.write(f"  {line(f, now)}: {fate(f, report)}.\n")
    if holding and not report:
        names = ", ".join(f.name for f in holding)
        err.write(f"Still applying or approved after the wait: {names}.\n")
        return Check(1, found)
    return Check(0, found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m zab.admin.inflight",
        description="The plans in flight on the admin bots, which a deploy's restart would "
        "reach (docs/ADMIN.md §2.4 step 6).",
    )
    parser.add_argument("--state-dir", type=Path, default=Path(STATE_DIR))
    parser.add_argument(
        "--wait", type=float, default=0.0, help="seconds to wait for those applying or approved"
    )
    parser.add_argument("--report", action="store_true", help="name them; never wait or refuse")
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= 7200:
        parser.error("--wait: 0 to 7200 seconds")
    done = check(args.state_dir, wait=args.wait, report=args.report, err=sys.stderr)
    print(" ".join(f.name for f in done.found if f.cut))
    return done.status


if __name__ == "__main__":
    sys.exit(main())
