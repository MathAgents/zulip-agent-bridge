"""Plans and approvals on the admin bots (docs/ADMIN.md §2.4, §2.5; #341, part 2).

`Planning` is the bot's `Planner`. It turns a change command into a plan and answers `!approve`
and `!reject`, `!status` and `!console`. The scripts are the `Runner`'s (PR 10), which runs
inside the bot's process, through the bot's `PlanStore`.

- **Request → plan** (§2.4 steps 1 to 3), as its own task: the bot's queue never waits for a
  dry run, and at most `PREPARING` plans of one workshop are prepared at once. `store.begin`
  hands out the plan's ID, and a `request` record names it at once, so every ID handed out
  is in the audit log, refused ones included: the bot's `floor` at start reads those records
  (`fleet.Ledger`) and nothing an organizer types. No ID that the grammar can't name
  (`MAX_PLAN_ID`) is ever handed out. The plan is then built from the bot's own fleet
  (`actions.build`); a `!roster import`'s CSV is downloaded with the bot's key to
  `runs/P-<n>/roster-upload.csv` (§3.1: 1 MB, `DOWNLOAD_SECONDS` at most). The runner runs
  the dry runs and the status read (`prepare`), and the hash is `plans.fingerprint` of the
  action and the live state it read. A `plan` record, then two posts in the request's topic:
  - **the plan message** (`plan_message`): its header, the targets, every step's exact
    argument list, the scrubbed dry run, the status read as a table of one line per VM
    (`status_view`; shown, never hashed), the hash and how to approve, and its chain head.
    Those essentials are never cut: when the rest doesn't fit Zulip's limit, the message
    shows what fits, says what it leaves out, and the whole plan, scrubbed, is attached;
  - **the approval message:** the same commands as text, for clients without widgets, and a
    `zform` widget whose buttons send `!approve P-n hash6` and `!reject P-n` (§2.5). A plan
    that needs a typed phrase gets no widget, since the web client shows a widget instead of
    the text: its text alone, with the line to send in a code block of its own (#542).
  `Approvals.post` then binds the plan to its messages and makes it PENDING. Anything that
  goes wrong first refuses the plan (REFUSED), with a `refused` record and the reason. A dry
  run that says there is nothing to change (`Prepared.unchanged`, #708) is no refusal: the plan
  is UNCHANGED, with an `unchanged` record, and the answer is a success, `☑`, with no plan.
- **Approve, reject.** The sender is looked up afresh (`GET /users/{id}`: human, active). An
  approval of a plan whose workshop has one applying, or handed to the runner, is answered
  "busy" and not recorded (§2.4 step 5); that check and `Approvals` follow the lookup with
  no await in between, and an APPROVED plan's workshop is marked busy at once. `Approvals`
  makes every other check. Each answer is audited (`approval`, `reject` or `refused`) and
  shows the chain head. An APPROVED plan is rebuilt from its record, against the fleet as it
  is now, and handed to `Runner.apply` (`Job`); a plan that no longer builds is STALE.
- **Time and restarts.** Every tick expires the plans past their TTL (`expired`); at start,
  the plans a restart interrupted are marked so (`interrupted`); each is said in its topic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import re
import shlex
import sqlite3
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

import zab
from zab.admin import monthend, newworkshop, upgradeserver
from zab.admin.actions import Action, ActionError, Fleet, Layout, Live, build, protect_applying
from zab.admin.approvals import SERVER, Received, Requirement, Sender, requirement, takes
from zab.admin.collector import cost_line, note_status
from zab.admin.config import Workshop
from zab.admin.fleet import FleetError, RowEnd, protects, read_roster
from zab.admin.grammar import SLUG_RE, Command, Roster, parse
from zab.admin.pages import disk_gb, disk_space, sentinel, sentinel_down
from zab.admin.plans import (
    APPLYING,
    APPROVED,
    DONE,
    EXPIRED,
    FAILED,
    PLANNING,
    STALE,
    Plan,
    PlanError,
    PlanStore,
    fingerprint,
)
from zab.admin.reads import footer, mention, short, shown_name, utc
from zab.admin.scrub import Scrubber, fence, span
from zab.admin.signin import SignIns
from zab.admin.wakerdown import status_line
from zab.zulip.client import AttachmentRejected, ZulipError

if TYPE_CHECKING:
    from zab.admin.audit import Head
    from zab.admin.bot import AdminBot, AdminServer, Incoming

LOG = logging.getLogger("zab.admin.planning")
#: §3.1: the roster upload's limit, in bytes and in time.
MAX_UPLOAD = 1_000_000
DOWNLOAD_SECONDS = 60
#: The largest plan ID the grammar can name (`P-` and at most 9 digits, `grammar._PLAN_RE`).
MAX_PLAN_ID = 999_999_999
#: Plans of one workshop whose dry runs run at once; the others wait their turn.
PREPARING = 2
#: A plan's note at most: a refusal's reason, or a STALE's (the runner's, and `_apply`'s for a
#: plan that no longer builds): long enough for `runner.vms_found`'s with a few rows (review of
#: #564), and for a rebuild's refusal of one row (#567).
NOTE_CHARS = 600
#: `!status` reads every VM it names over SSH: one at a time per workshop (another is answered
#: at once), and at most this many on the server at once (the others wait their turn).
READING = 3
#: The mark of a command whose dry run says there is nothing to change (#708): a success of its
#: own, "already as wanted"; never `✅`, which says that something was done. U+2611 alone, as
#: `⚠` and `⏱` are here: the scrubber deletes a variation selector, an invisible character.
UNCHANGED_MARK = "☑"
#: Kept free below Zulip's message limit, and for the attachment's line.
MARGIN, NOTE_ROOM = 200, 300
#: The audit footer under a `!status` answer, at most: its line break, `audit #seq · chain` and
#: 12 hex digits in a code span, with room for a seq of 20 digits (#767).
FOOTER_ROOM = 64
#: The dry run's lines shown inline at most (§2.7 item 5); and the status table's, its header
#: included, in `!status`'s answer and in a plan's status read: more, so that a workshop of
#: about 40 VMs (§11) shows whole (#764). Zulip's message limit still binds through `_fit`.
DRY_LINES, STATUS_LINES = 40, 50
#: The status table's columns (§2.4 step 2), from `status.py --json`'s schema 1.
STATUS_COLUMNS = ("VM", "state", "zab", "idle stop", "running turns")
_STATUS_START = re.compile(r'\{\s*"schema":')
#: roster_import.py's refusal of a slug that another workshop has (`taken_refusal`), which
#: names the workshop only: the bot adds its own hint to it, naming the real bot, under §2.1's
#: rule (#347). `AdminServer.pointer` checks that workshop's roster: no line can make it name
#: a bot whose roster lacks the slug.
_TAKEN = re.compile(
    r"^refused: (?P<slug>[a-z0-9][a-z0-9-]*) is in (?P<ws>[a-z0-9][a-z0-9-]*); ", re.M | re.ASCII
)
#: Bounds on the table's work (§2.7 item 5), far above status.py's one answer of a workshop's
#: VMs: the places tried as an answer's start, each a decode of at most the text; the rows,
#: each four scrubs (about 0.1 s for all of them); the VMs past them are counted.
STATUS_TRIES, STATUS_ROWS = 4, 1000
#: A VM's running turns read by backend at most: status.py's `MAX_ITEMS`, the most turns a
#: workshop VM runs (10 per backend, push_config.py's `max_concurrent_turns`, #919). Raise
#: both with that: past status.py's cut, the `N more` here can't show.
MAX_TURNS = 20
#: `!status`'s lines under its table (#439), at most, and a VM's own text in one, at most. One
#: VM gets a few lines of its own (`_one`); several, a line each for those with something to
#: look at (`_brief`), then status.py's summary line. Each list holds at most 3 items.
DETAIL_LINES, DETAIL_CHARS, DETAIL_ITEMS = 20, 120, 3
_ISO_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2}):[0-9]{2}Z", re.ASCII)
_BACKEND = re.compile(r"[A-Za-z0-9_-]{1,40}", re.ASCII)  # status.py's `_NAME_RE`
_SIGNED_IN = {"ok": "signed in", "no": "not signed in", "FAIL": "FAIL"}
_STRAINED = {"load": "load", "mem": "memory", "disk": "disk"}
#: status.py's summary (`summary_counts`) as its own last line says it.
_SUMMARY = ("signed in: {both} both, {codex_only} Codex only, {claude_only} Claude only, "
            "{neither} neither; {unreachable} unreachable, {stopped} stopped, "
            "{doctor_fail} doctor FAIL")  # fmt: skip
#: The room an interrupted `!retire`'s message keeps for its footer and the bot's own lines.
REPORT_ROOM = 600
#: The steps of a `!retire` row that save its work: one that failed, tolerated under
#: `unsynced`, left the row torn down without it (as `runner.Rows.unsaved` has them).
SAVING = ("start", "idle-stop", "uninstall", "final-sync", "export")
#: The longest wait for a month-end run after errors that ended runs before their end
#: (`Planning._abandoned`, review of #835, N1).
MONTH_END_HELD = 3600
#: How often the month-end schedules are looked at (§11.5, #570): their warnings are due by the
#: day, and each look reads every roster that has one.
MONTH_END_EVERY = 60.0
_TABLE = """
CREATE TABLE IF NOT EXISTS plan_topics (
    plan INTEGER PRIMARY KEY REFERENCES plans (id),
    stream INTEGER NOT NULL,
    topic TEXT NOT NULL
);
"""


class Unavailable(Exception):
    """The runner can't do this on this server (yet)."""


@dataclass(frozen=True, slots=True)
class Prepared:
    """What the runner read at plan time (§2.4 step 2). `output`: the dry runs' text as the
    scripts printed it, which the bot scrubs and fences; `status`: the status read's, which the
    bot shows as a table (`status_view`). `live`: what the hash covers, never the status read.
    `why`: when the plan can't be offered, the runner's own one line, which the bot shows as it
    stands and keeps as the plan's note; never script or VM output, which belongs in `output`.
    `unchanged`: not offered because its dry run says there is nothing to change (#708), which
    the bot answers as a success, with no plan."""

    ok: bool
    output: str = ""
    live: Live = field(default_factory=Live)
    why: str = ""
    status: str = ""
    unchanged: bool = False


@dataclass(frozen=True, slots=True)
class Job:
    """An APPROVED plan for `Runner.apply`, rebuilt from its record against the fleet as it is
    now. The runner recomputes the hash (`plans.fingerprint`) and calls `begin_apply`, which
    makes it APPLYING, or STALE or EXPIRED; then it posts progress and the result in the
    plan's topic, through `bot`. Its workshop counts as busy until `apply` returns."""

    plan: Plan
    bot: AdminBot
    stream: int
    topic: str
    command: Command
    action: Action
    fleet: Fleet | None  # None for a server plan (`!new-workshop`), which acts on no fleet


class Runner(Protocol):
    """The scripts' side, PR 10: subprocess environment, timeouts, the apply lock per workshop,
    progress edits. It runs in the bot's process; the bot hosts it. It reloads the secrets
    before posting any output of an apply's steps (§2.7), and it writes each `apply_end` with
    `fleet.ApplyEnd` through `job.bot.server.audit`."""

    async def prepare(self, plan: Plan, action: Action, fleet: Fleet | None) -> Prepared: ...

    async def read(self, action: Action, fleet: Fleet) -> str:
        """`!status`'s output (§3, action 6): read only, no plan."""
        ...

    def busy(self, workshop: str) -> Plan | None:
        """The workshop's plan that is applying, if one is. `Planning` also counts a plan as
        busy from its approval until `apply` returns."""
        ...

    async def apply(self, job: Job) -> None: ...

    async def month_end(
        self, plan: Plan, bot: AdminBot, where: tuple[int, str], fleet: Fleet, schedule: Plan,
        due: list[monthend.Entry],
    ) -> None:  # fmt: skip
        """A month-end run (§11.5, #570): `plan` is APPLYING, its workshop marked busy."""
        ...


class NoRunner:
    """Until PR 10: every plan is refused, and nothing runs."""

    async def prepare(self, plan: Plan, action: Action, fleet: Fleet | None) -> Prepared:
        return Prepared(False, why="no runner on this server yet (PR 10): nothing runs")

    async def read(self, action: Action, fleet: Fleet) -> str:
        raise Unavailable("no runner on this server yet (PR 10)")

    def busy(self, workshop: str) -> Plan | None:
        return None

    async def apply(self, job: Job) -> None:
        raise Unavailable("no runner on this server yet (PR 10)")

    async def month_end(
        self, plan: Plan, bot: AdminBot, where: tuple[int, str], fleet: Fleet, schedule: Plan,
        due: list[monthend.Entry],
    ) -> None:  # fmt: skip
        raise Unavailable("no runner on this server yet (PR 10)")


def phrase(needs: Requirement) -> str:
    return " ".join(needs.phrase)


def approve_text(plan: Plan | int, digest: str, needs: Requirement) -> str:
    """The command that approves the plan, phrase included."""
    name = plan.name if isinstance(plan, Plan) else f"P-{plan}"
    return " ".join(filter(None, (f"!approve {name} {digest[:6]}", phrase(needs))))


def _choice(short_name: str, long_name: str, reply: str) -> dict[str, str]:
    """One `zform` choice. The web client's schema (`web/src/zform_data.ts`) requires `type` on
    every choice and drops the whole widget without it, though the server accepts it (V1). The
    button shows `short_name` as plain text, styled by Zulip's CSS: a bot can't colour it, so the
    labels carry the colour as emoji (Drew, Oct 15)."""
    return {"type": "multiple_choice", "short_name": short_name, "long_name": long_name,
            "reply": reply}  # fmt: skip


def approval_message(
    plan: Plan | int, digest: str, needs: Requirement, command: str
) -> tuple[str, dict[str, Any] | None]:
    """The approval message (§2.5): its text, and its `zform` widget, or None. Zulip's web
    client shows the widget *instead of* the text; clients without widgets show the text. A
    click sends a choice's `reply` as an ordinary message, after a mention of this message's
    sender in a channel.

    A plan that needs a typed phrase gets no widget (#542): no button can type the phrase, and
    a widget of Reject alone hid the one line to send. Its text shows that line by itself, as a
    one-line fenced code block, then the approvers it takes and how to reject. The web client
    gives a code block a copy button, which copies its text as plain text. A code span would
    not do: Zulip's paste handler (`compose_paste.ts`) pastes a copied lone `<code>` with its
    backticks, and a line that starts with one is no command (review of #544). Both texts start
    `P-n `command`: approve `, which the e2e harness waits for."""
    name = plan.name if isinstance(plan, Plan) else f"P-{plan}"
    approve, reject = approve_text(plan, digest, needs), f"!reject {name}"
    if needs.phrase:
        several = needs.needed > 1 and needs.klass != SERVER  # a server plan: an admin's
        each = f"It takes {takes(needs) or '1 approver'}"  # or one global admin's (#558)
        each += ": each one sends that line." if several else "."
        text = (
            f"{name} `{command}`: approve by sending this plan's approval line, exactly as it "
            "is. A plan like this one has no Approve button. Point at the line below and click "
            "its copy button, at its top right (or type the line), then send it as a new "
            f"message.\n\n```text\n{approve}\n```\n\n"
            f"{each} To reject, send `{reject}`."
        )
        return text, None
    many = f" ({takes(needs)})" * bool(takes(needs))
    text = f"{name} `{command}`: approve with `{approve}`{many}, or reject with `{reject}`."
    choices = [
        _choice("✅ Approve", f"Approve {name}", approve),
        _choice("❌ Reject", f"Reject {name}", reject),
    ]
    widget = {
        "widget_type": "zform",
        "extra_data": {"type": "choices", "heading": f"{name}: {command}", "choices": choices},
    }
    return text, widget


def _step(argv: tuple[str, ...], env: tuple[str, ...]) -> str:
    with_env = f"  # with {', '.join(env)}" if env else ""
    return shlex.join(argv) + with_env


def _fit(items: list[str], room: int, *, cap: int | None = None) -> list[str]:
    """The first items that fit in `room` characters, one separator each (at most `cap` of
    them); an item too long for the room left is cut, with ` …`."""
    shown: list[str] = []
    used = 0
    for item in items[:cap]:
        need = len(item) + 1
        if used + need <= room:
            shown.append(item)
            used += need
            continue
        if room - used >= 80:
            shown.append(item[: room - used - 3] + " …")
        break
    return shown


def _left(items: list[str], shown: list[str], one: str, many: str) -> str:
    """What a cut left out, for its note: `1 line of the table`, `3 targets`; "" for none."""
    n = len(items) - len(shown)
    return f"{n} {one if n == 1 else many}" if n else ""


def _status_json(text: str) -> tuple[dict[str, Any], int, int] | None:
    """The first whole `status.py --json` answer in `text` (schema 1), and where it lies."""
    decoder = json.JSONDecoder()
    for found in islice(_STATUS_START.finditer(text), STATUS_TRIES):
        try:
            value, end = decoder.raw_decode(text, found.start())
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(value, dict)
            and value.get("schema") == 1
            and isinstance(value.get("vms"), list)
        ):
            return value, found.start(), end
    return None


Scrub = Callable[[str], str]


def _cell(value: object, scrub: Scrub) -> str:
    """One cell: the VM's text scrubbed again, since decoding the JSON undid its escapes, and
    only then cut; on one line with no run of spaces (the columns' separator). `?` for unknown
    and for anything but a string: `str` of a list would escape it again (`repr`)."""
    text = " ".join(scrub(value).split()) if isinstance(value, str) else ""
    return (text if len(text) <= 32 else text[:31] + "…") or "?"


def _idle(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        return "?"
    if not value["enabled"]:
        return "off"
    minutes = value.get("minutes")
    on = f"{minutes} min" if type(minutes) is int else "on"
    return on if value.get("active") is True else f"{on}, inactive"


def _turns(value: object, scrub: Scrub) -> str:
    """`0`, `1 claude`, `2 claude, 1 codex`; `?` when the VM's zab can't say (null). The first
    `MAX_TURNS` by backend (status.py lists no more), and how many follow."""
    if not isinstance(value, list):
        return "?"
    backends = Counter(_cell(t.get("backend"), scrub) if isinstance(t, dict) else "?"
                       for t in value[:MAX_TURNS])  # fmt: skip
    more = [f"{len(value) - MAX_TURNS} more"] * (len(value) > MAX_TURNS)
    return ", ".join([*(f"{n} {b}" for b, n in sorted(backends.items())), *more]) or "0"


def status_view(text: str, scrub: Scrub) -> list[str]:
    """The plan's status read as shown (§2.4 step 2): from `status.py --json`, a table with one
    line per VM (slug, state, zab version, idle stop, running turns), then any other line the
    read printed (a failed EC2 lookup, a failed exit); when `text` holds no such answer, its own
    lines. `text` is scrubbed; each value decoded from its JSON is scrubbed again (`_cell`)."""
    return _view(text, _status_json(text), scrub)


def _view(text: str, found: tuple[dict[str, Any], int, int] | None, scrub: Scrub) -> list[str]:
    if found is None:
        return text.removesuffix("\n").split("\n")
    answer, start, end = found
    vms = [vm for vm in answer["vms"] if isinstance(vm, dict)]
    rows = [list(STATUS_COLUMNS)] + [
        [_cell(vm.get("slug"), scrub), _cell(vm.get("bridge"), scrub),
         _cell(vm.get("zab_version"), scrub), _idle(vm.get("idle_stop")),
         _turns(vm.get("running_turns"), scrub)]
        for vm in vms[:STATUS_ROWS]
    ]  # fmt: skip
    widths = [max(len(row[i]) for row in rows) for i in range(len(STATUS_COLUMNS))]
    table = [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
    ]
    table += [f"… and {len(vms) - STATUS_ROWS} more VMs"] * (len(vms) > STATUS_ROWS)
    rest = (text[:start] + "\n" + text[end:]).split("\n")
    return table + [line for line in rest if line.strip()]


@dataclass(frozen=True, slots=True)
class StatusRead:
    """The plan's status read: `lines` as the message shows them (`status_view`), and `text`,
    as status.py printed it, for the attachment when it holds more. Both scrubbed with the
    JSON's escapes undone (`Scrubber.json_text`): escaped, a token split by U+200B is out of
    the scan's sight, and the attachment would carry it whole."""

    lines: tuple[str, ...]
    text: str

    @classmethod
    def of(cls, text: str, scrubber: Scrubber) -> StatusRead:
        text = scrubber.json_text(text or "(no output)")
        return cls(tuple(status_view(text, scrubber)), text)


def _said(value: object, scrub: Scrub) -> str:
    """A VM's own text in a detail line: scrubbed again (as `_cell`), on one line, then cut, in
    an inline code span, which renders nothing; "" for anything but a non-empty string."""
    text = " ".join(scrub(value).split()) if isinstance(value, str) else ""
    return span(text if len(text) <= DETAIL_CHARS else text[: DETAIL_CHARS - 1] + "…") * bool(text)


def _when(value: object) -> str:
    """`2026-10-18 14:05 UTC` for status.py's `2026-10-18T14:05:00Z`; "" for anything else."""
    found = _ISO_TIME.fullmatch(value) if isinstance(value, str) else None
    return f"{found[1]} {found[2]} UTC" if found else ""


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _strained(vm: dict[str, Any]) -> list[str]:
    strained = vm.get("strained")
    return [_STRAINED[s] for s in strained if s in _STRAINED] if isinstance(strained, list) else []


def _free(vm: dict[str, Any]) -> str:
    """` (15 GB free)` for a disk under strain (status.py's ` !`, 90% full) whose read has its
    available space (#552); "" otherwise, as for a read made before #552. In binary GB, as the
    console shows the disk (`pages.disk_gb`)."""
    space = disk_space(vm.get("disk"))
    return f" ({disk_gb(space[2])} GB free)" if space and "disk" in _strained(vm) else ""


def _resources(vm: dict[str, Any]) -> str:
    """`load 0.4 of 8 CPUs, memory 2.9 of 32 GB, disk 42% (under strain: memory)`: what is
    known of it; "" when nothing is. Memory as the bridge's `!vm` says it (`zab.tools.vmstat`,
    which the admin server doesn't import): "GB" are binary (Drew: "GiB" confuses people), the
    use to a tenth, the size MemTotal rounded up to a whole one (`vmstat.gb(…, up=True)`). The
    disk stays a percentage, and names its free space when it is under strain: `disk 92% (15
    GB free)` (`_free`, #552)."""
    load, memory, disk = vm.get("load"), vm.get("memory"), vm.get("disk_percent")
    parts = []
    if isinstance(load, dict) and _number(load.get("load1")) and _number(load.get("cpus")):
        parts.append(f"load {load['load1']:.1f} of {load['cpus']:g} CPUs")
    if isinstance(memory, dict) and all(
        _number(memory.get(k)) for k in ("used_bytes", "total_bytes")
    ):
        used, total = (memory[k] / 2**30 for k in ("used_bytes", "total_bytes"))
        parts.append(f"memory {used:.1f} of {math.ceil(total)} GB")
    if _number(disk):
        parts.append(f"disk {disk:g}%{_free(vm)}")
    strained = _strained(vm)
    return ", ".join(parts) + f" (under strain: {', '.join(strained)})" * bool(strained and parts)


def _failures(vm: dict[str, Any], scrub: Scrub) -> tuple[list[str], str]:
    """The doctor's failures, each in its span, and the last error unless it is the first of
    them (status.py cuts the same text at 80 characters for it, and at 160 for the list)."""
    listed = vm.get("doctor_failures")
    failures = [f for f in listed if isinstance(f, str) and f] if isinstance(listed, list) else []
    last = vm.get("last_error")
    same = bool(failures) and isinstance(last, str) and failures[0].startswith(last.rstrip("…"))
    return [_said(f, scrub) for f in failures], "" if same else _said(last, scrub)


def _turn(turn: dict[str, Any]) -> str:
    """`claude since 2026-10-18 13:58 UTC`: its backend when it is a name, and its start."""
    backend, since = str(turn.get("backend")), _when(turn.get("started_at"))
    return (backend if _BACKEND.fullmatch(backend) else "?") + f" since {since}" * bool(since)


def _one(vm: dict[str, Any], scrub: Scrub) -> list[str]:
    """The lines under the table of one VM: what is known of its sign-ins, sentinel, last
    activity and running turns, resources, idle stop's last verdict, doctor failures and last
    error."""
    lines = []
    logins = [str(vm.get(k)) for k in ("codex_login", "claude_login")]
    if any(login in _SIGNED_IN for login in logins):
        codex, claude = (_SIGNED_IN.get(login, "?") for login in logins)
        lines.append(f"Codex {codex}, Claude {claude}")
    if (watch := sentinel(vm)) != "?":  # #588
        down = sentinel_down(watch)
        lines.append(f"sentinel {watch}" + f": {down}" * bool(down))
    if when := _when(vm.get("last_activity")):
        lines.append(f"last activity {when}")
    turns = vm.get("running_turns")
    turns = turns[:MAX_TURNS] if isinstance(turns, list) else []
    running = [_turn(t) for t in turns if isinstance(t, dict)]
    if running:
        more = f", and {len(running) - DETAIL_ITEMS} more" * (len(running) > DETAIL_ITEMS)
        lines.append("running: " + ", ".join(running[:DETAIL_ITEMS]) + more)
    if resources := _resources(vm):
        lines.append(resources)
    idle = vm.get("idle_stop")
    if isinstance(idle, dict) and (verdict := _said(idle.get("last_verdict"), scrub)):
        lines.append(f"idle stop's last verdict: {verdict}")
    failures, last = _failures(vm, scrub)
    lines += [f"doctor: {f}" for f in failures[:DETAIL_ITEMS]]
    if len(failures) > DETAIL_ITEMS:
        lines.append(f"and {len(failures) - DETAIL_ITEMS} more doctor failures")
    return [f"- {line}" for line in [*lines, *[f"last error: {last}"] * bool(last)]]


def _brief(vm: dict[str, Any], scrub: Scrub) -> str:
    """One VM's line under a table of several: what is under strain (a disk with its free
    space, `_free`), a sentinel that isn't `ok` (#588), its first doctor failure (and how many
    follow), its last error; "" when it has none of them."""
    strained = [s + _free(vm) if s == "disk" else s for s in _strained(vm)]
    failures, last = _failures(vm, scrub)
    parts = []
    if strained:
        parts.append(f"under strain: {', '.join(strained)}")
    if (watch := sentinel(vm)) not in ("ok", "?"):
        parts.append(f"sentinel {watch}")
    if failures:
        parts.append(
            f"doctor: {failures[0]}" + f" (and {len(failures) - 1} more)" * (len(failures) > 1)
        )
    if last:
        parts.append(f"last error: {last}")
    slug = vm.get("slug")
    name = (
        f"**{slug}**" if isinstance(slug, str) and SLUG_RE.fullmatch(slug) else _said(slug, scrub)
    )
    return f"- {name or '?'}: {'; '.join(parts)}" if parts else ""


def _summary(answer: dict[str, Any]) -> str:
    counts = answer.get("summary")
    if not isinstance(counts, dict):
        return ""
    keys = ("both", "codex_only", "claude_only", "neither", "unreachable", "stopped", "doctor_fail")
    if not all(type(counts.get(k)) is int for k in keys):
        return ""
    return _SUMMARY.format(**{k: counts[k] for k in keys})


@dataclass(frozen=True, slots=True)
class StatusReply:
    """`!status`'s answer (#439): `text`, the message, ends with `ATTACH` when `full`, the
    whole answer and the read as status.py printed it, must be attached: it was cut."""

    text: str
    full: str | None


def status_reply(text: str, scrub: Scrub, *, limit: int) -> StatusReply:
    """`!status`'s answer from the read (`text`, scrubbed with its JSON's escapes undone by the
    runner): the plans' table (`status_view`), fenced, then the detail lines, readable on a
    phone: one VM's own (`_one`), or a line for each of several VMs with something to look at
    (`_brief`) and status.py's summary line. Never the JSON itself; a read with no answer in it
    is shown as it is, fenced. The table is cut at `STATUS_LINES` lines, the details at
    `DETAIL_LINES`, the whole within `limit`; whatever is cut is named, and attached. A detail
    line is shown whole or not at all: cut, a code span would leave its VM text unfenced."""
    found = _status_json(text)
    table = _view(text, found, scrub)
    vms = [vm for vm in found[0]["vms"] if isinstance(vm, dict)] if found else []
    details = (
        _one(vms[0], scrub)
        if len(vms) == 1
        else [line for vm in vms[:STATUS_ROWS] if (line := _brief(vm, scrub))]
    )
    summary = _summary(found[0]) if found and len(vms) > 1 else ""

    def render(rows: list[str], lines: list[str], note: str) -> str:
        # a blank line ends the list: a line right after it would continue its last item
        tail = [f"\n{line}" for line in (summary, note) if line]
        return "\n".join([fence("\n".join(rows)), *lines, *tail])

    whole = render(table, details, "")
    if len(whole) <= limit - MARGIN and len(table) <= STATUS_LINES and len(details) <= DETAIL_LINES:
        return StatusReply(whole, None)
    room = limit - MARGIN - NOTE_ROOM - len(render([], [], ""))
    shown = _fit(table, room - room // 4, cap=STATUS_LINES)
    room -= sum(len(line) + 1 for line in shown)
    said: list[str] = []
    for line in details[:DETAIL_LINES]:
        room -= len(line) + 1
        if room < 0:
            break
        said.append(line)
    left = [
        _left(table, shown, "line of the table", "lines of the table"),
        _left(details, said, "line under it", "lines under it"),
    ]
    cut = any(line.endswith(" …") for line in shown)
    gone = ", ".join(filter(None, left)) or "nothing"
    note = f"*Cut to fit: {gone} left out{', and long lines cut' * cut}. {ATTACH}*"
    raw = text.removesuffix("\n").split("\n")
    full = "\n".join([*table, *details, *[summary] * bool(summary),
                      *(["The status read, as status.py printed it:", *raw]
                        if raw != table else [])])  # fmt: skip
    return StatusReply(render(shown, said, note), full)


def left_out(fleet: Fleet) -> str:
    """The line under `!status all` that names the rows `all` leaves out, those a `!retire`
    ended (#559), each once, with when: at most 10, then how many more."""
    named = [f"{slug} ({when})" for slug, when in fleet.retired.items() if slug in fleet.slugs]
    more = f", and {len(named) - 10} more" * (len(named) > 10)
    return f"*Retired, so left out: {', '.join(named[:10])}{more}.*" if named else ""


@dataclass(frozen=True, slots=True)
class PlanMessage:
    """`text`, the message, ends with `ATTACH` (for the attachment's line) when `full`, the
    whole plan as text, must be attached: the message leaves something out."""

    text: str
    full: str | None


ATTACH = "\x00attachment\x00"
#: The commands of a plan whose apply runs no script: `!month-end schedule`'s, whose runs build
#: their own, with the release then running (§11.5, #570).
NONE = "**Commands:** none: this plan's apply runs no script."


def unchanged_title(plan: Plan, why: str) -> str:
    """The first line of the answer to a command whose dry run says there is nothing to change
    (#708): a success of its own, `UNCHANGED_MARK`, as a result names its plan and command."""
    return f"{UNCHANGED_MARK} **{plan.name}** · `{short(plan.command)}`: {why}; no plan to approve"


def plan_message(
    plan: Plan,
    action: Action,
    digest: str,
    needs: Requirement,
    *,
    requester: str,
    expires: float,
    output: str,
    head: Head,
    limit: int,
    status: StatusRead | None = None,
) -> PlanMessage:
    """The plan message (§2.4 step 2), within `limit` characters. Its header, the hash with the
    approve and reject commands, and the chain head are always there; the action's notes (under
    the targets), the targets, the commands, the dry run (`output`, already scrubbed) and the
    status read's table (`status`, after the dry run) take what room is left, in that order,
    the dry run and the table at most `DRY_LINES` and `STATUS_LINES` lines; a long command is
    shortened (`short`). Whatever is left out is named, and the whole plan is attached, with
    the status read as status.py printed it, its values scrubbed (`StatusRead`)."""
    where = action.workshop or "server"
    shown_command = short(plan.command)
    top = (
        f"**{plan.name}** · {where} · `{shown_command}` · requested by {requester} · "
        f"expires {utc(expires)}"
    )
    many = f" ({takes(needs)})" * bool(takes(needs))
    hashed = (
        f"**Plan** `{digest[:12]}`: approve with `{approve_text(plan, digest, needs)}`{many}, "
        f"or reject with `!reject {plan.name}`."
    )
    targets, commands = list(action.targets), [_step(s.argv, s.env) for s in action.applied]
    dry = output.removesuffix("\n").split("\n") if action.dry_runs else []
    read = list(status.lines) if status is not None else []
    title = "**Dry run**" + (", every step:" if action.dry_runs else ": none for this command")
    read_title = "**Status** of the targets, read for this plan (shown, not hashed):"

    notes = list(action.notes)  # e.g. each row's end of the workshop, for !teardown (#367)

    def render(
        shown: list[str], steps: list[str], lines: list[str], rows: list[str], note: str,
        said: list[str],
    ) -> str:  # fmt: skip
        more = len(targets) - len(shown)
        listed = ", ".join(shown) + (f", … and {more} more" if more else "")
        parts = [top, f"**Targets** ({len(targets)}): {listed or 'none'}", *said]
        parts += ["**Commands**, in order:", fence("\n".join(steps))] if action.applied else [NONE]
        parts += [title]
        parts += [fence("\n".join(lines))] if action.dry_runs else []
        parts += [read_title, fence("\n".join(rows))] if status is not None else []
        return "\n".join([*parts, *([note] if note else []), hashed, footer(head)])

    whole = render(targets, commands, dry, read, "", notes)
    if (
        len(whole) <= limit - MARGIN
        and len(dry) <= DRY_LINES
        and len(read) <= STATUS_LINES
        and shown_command == plan.command
    ):
        return PlanMessage(whole, None)
    room = limit - MARGIN - NOTE_ROOM - len(render([], [], [], [], "", []))
    shown_notes = _fit(notes, room // 4)
    room -= sum(len(n) + 1 for n in shown_notes)
    shown_targets = _fit(targets, room // 6)
    room -= sum(len(t) + 2 for t in shown_targets) + 40
    shown_steps = _fit(commands, room // 2)
    room -= sum(len(s) + 1 for s in shown_steps)
    shown_dry = _fit(dry, room, cap=DRY_LINES)
    room -= sum(len(s) + 1 for s in shown_dry)
    shown_read = _fit(read, room, cap=STATUS_LINES)
    left = [
        _left(notes, shown_notes, "line of notes", "lines of notes"),
        _left(targets, shown_targets, "target", "targets"),
        _left(commands, shown_steps, "command", "commands"),
        _left(dry, shown_dry, "line of the dry run", "lines of the dry run"),
        _left(read, shown_read, "line of the status", "lines of the status"),
    ]
    cut = any(s.endswith(" …") for s in shown_notes + shown_steps + shown_dry + shown_read)
    said = ", ".join(filter(None, left)) or "nothing"
    named = ", the command shortened" * (shown_command != plan.command)
    note = f"*Cut to fit: {said} left out{', and long lines cut' * cut}{named}. {ATTACH}*"
    whole_top = top.replace(f"`{shown_command}`", f"`{plan.command}`")
    raw = status.text.removesuffix("\n").split("\n") if status is not None else []
    full = "\n".join(
        [
            whole_top,
            f"Targets ({len(targets)}): " + ", ".join(targets),
            *notes,
            "Commands, in order:" if action.applied else NONE,
            *commands,
            "Dry run:" if action.dry_runs else "Dry run: none",
            *dry,
            *(["Status of the targets, read for this plan (shown, not hashed):", *read]
              if status is not None else []),
            *(["The status read, as status.py printed it:", *raw] if raw != read else []),
            hashed,
        ]
    )  # fmt: skip
    shown = render(shown_targets, shown_steps, shown_dry, shown_read, note, shown_notes)
    return PlanMessage(shown, full)


def _save(path: Path, data: bytes) -> None:
    """A new file, 0600, in a new 0700 directory of its plan: never one written before."""
    path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.mkdir(mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def release_moved(layout: Layout) -> bool:
    """Whether `current` (`layout.release`) names another release than the one this process
    runs, the release its `zab` was imported from (#569): read afresh, never from a helper's
    outcome, which an error may have cut short (review of #894, N5)."""
    here = Path(zab.__file__).resolve().parents[2]  # releases/vX.Y.Z, from its src/zab/
    return Path(layout.release).resolve() != here


def next_plan_id(store: PlanStore) -> int:
    """The ID `store.begin` hands out next (SQLite's AUTOINCREMENT, above the floor)."""
    row = store.db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'plans'").fetchone()
    return (row[0] if row else 0) + 1


class Planning:
    """The bot's `Planner`: plans, approvals, `!status` and `!console` (§2.4, §2.5)."""

    def __init__(
        self,
        runner: Runner,
        *,
        console: SignIns | None = None,
        restart: Callable[[], None] | None = None,
    ) -> None:
        """`console`: the console's sign-in links (§4.4, `signin.SignIns`), or None without
        a `console_url`.
        `restart`: restarts the bots' process, after a `!new-workshop` apply (§2.9); without
        it, the new workshop waits for the next restart, and the log says so."""
        self.runner, self.console, self.restart = runner, console, restart
        self.tasks: set[asyncio.Task[None]] = set()
        #: workshop → the plan approved and handed to the runner, until its apply returns
        self.handed: dict[str, int] = {}
        self.preparing: dict[str, asyncio.Semaphore] = {}
        self.reading: set[str] = set()  # the workshops with a `!status` under way
        self.reads = asyncio.Semaphore(READING)
        self._server: AdminServer | None = None
        self.month_end_next = 0.0  # when the month-end's next look is due (`month_end`)
        #: workshop → (month-end runs an error ended in a row, no run before): `_abandoned`
        self.month_end_held: dict[str, tuple[int, float]] = {}
        #: a warning Zulip refused, by workshop, row, kind, end, recipient and schedule: its
        #: tries (`_key`; Codex rounds 2 and 5 on #829: a new end or recipient is a new warning,
        #: with tries its own; #883: so is a new approval, which tries a refused one again)
        self.tries: dict[tuple[str, str, str, float, str, int], int] = {}

    @property
    def server(self) -> AdminServer:
        assert self._server is not None, "Planning.start comes first"
        return self._server

    def spawn(
        self, work: Coroutine[Any, Any, None], then: Callable[[], None] | None = None
    ) -> None:
        """Run `work` beside the bot's queue (`Planner.handle` returns at once); `then` runs
        when it ends, however it ends."""
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        if then is not None:
            task.add_done_callback(lambda _: then())

    async def stop(self) -> None:
        """Cancel the plans' and reads' tasks and wait for them, before the server closes the
        clients and the store (`AdminServer.close`). A plan whose preparation or apply is cut
        short stays PLANNING or APPLYING, and the next start marks it INTERRUPTED (§2.4 step
        6). Tasks that a task spawns while it ends are stopped too."""
        while self.tasks:
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.tasks.difference_update(tasks)

    def _done(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # with its traceback: the root's only handler scrubs it (`bot._ScrubLogs`)
            LOG.warning("a plan's task failed (%s)", type(exc).__name__, exc_info=exc)

    async def start(self, server: AdminServer) -> None:
        """Mark the plans a restart interrupted (§2.4 step 6), and say so where each was."""
        self._server = server
        store = server.store
        assert store is not None
        store.db.executescript(_TABLE)
        monthend.setup(store.db)  # the month-end schedules (§11.5, #570)
        if next_plan_id(store) > MAX_PLAN_ID:
            LOG.error(
                "plan IDs above P-%d can't be named in commands: no plan can be made", MAX_PLAN_ID
            )
        for plan in store.recover():
            check = "Check `!status`, then request again."
            if ends := server.ledger.cut(plan.name):  # a `!retire`, from its own records (#837)
                check = self._left(plan, ends)
            try:  # the facts are on the log already, its steps' own: this record says the cut
                head: Head | None = await server.audit("interrupted", **self._fields(plan))
            except Exception as exc:
                LOG.warning("%s: interrupted, not recorded (%s)", plan.name, type(exc).__name__)
                head = None
            schedule = monthend.run_of(store.db, plan.id)
            if schedule is not None:  # a month-end run: the next look resumes it (§11.5)
                check = (
                    f"It is a month-end run of P-{schedule}: the bot's next look resumes each "
                    "of its rows at its first unfinished step, its uninstall and sync again "
                    "unless the ledger still has them."
                )
            elif plan.command == "!month-end schedule":  # review of #824, N6
                check = (
                    "Its schedule may be stored already: `!month-end show` says, and if it "
                    "isn't, request again."
                )
            lead = "." if schedule is not None else ": nothing resumes on its own."
            said = f"⏹ {plan.name} was interrupted by a restart of the bots{lead} {check}"
            if ends:  # a long report attached, a summary in its place (Codex round 1, rework)
                said = await self._fitted(plan, said, ends)
            await self._say(plan, said, head)  # a month-end run's rows: taken up (round 2, N7)

    async def _fitted(self, plan: Plan, said: str, ends: Mapping[str, RowEnd]) -> str:
        """An interrupted `!retire`'s message, as one Zulip message holds it: whole when it
        fits, room kept for its footer; else its first line, how many rows are retired and
        which are left, and the whole of it attached (as a long result is, `Runner._result`),
        so that no row's line is lost to the bot's cut (Codex round 1 on #849's rework). An
        attachment that fails keeps the whole lines that fit, and says how many it leaves."""
        bot = self._bot(plan)
        most = bot.client.limits.get("max_message_length", 10000) if bot else 10000
        if len(said) <= most - REPORT_ROOM or bot is None:
            return said
        head = said.split("\n", 1)[0].removesuffix(" What it did to each row:")
        left = [row for row, end in ends.items() if not end.retired]
        summary = (
            f"\n{len(ends) - len(left)} of {len(ends)} rows retired; left: "
            f"{monthend.listed(left) or 'none'}."
        )
        link = await self._attach(bot, f"{plan.name}-interrupted.txt", said)
        if link:
            return f"{head}{summary}\n*What it did to each row, too long to show here: {link}.*"
        lines, kept = said.split("\n")[1:], [f"{head}{summary}"]  # the summary kept (#855)
        for line in lines:
            if len("\n".join([*kept, line])) > most - REPORT_ROOM:
                break
            kept.append(line)
        left_out = len(lines) - (len(kept) - 1)
        return "\n".join([*kept, f"*… {left_out} more lines: the attachment failed.*"])

    @staticmethod
    def _left(plan: Plan, ends: Mapping[str, RowEnd]) -> str:
        """What a `!retire` a restart cut did to each row, and what is left, from its own
        `retire_step` records, as the ledger took them (#837): retired (torn down `unsynced`,
        as the result would say, when a step that saves the work failed under `unsynced`);
        its teardown done, or no VM found, so its VM is gone and its `ip` cleared (#604), for
        `!retire … unsynced`; cut or failed at its teardown, its VM perhaps gone, for the same
        `!retire` again and `unsynced` if that finds no VM; or else its VM as its steps left
        it, for the same `!retire` again, with the plan's own words (`export` until the row's
        archive is in, `unsynced`; #820). The rows it never reached share one line."""
        words = [w for w in plan.command.split()[1:] if w in ("export", "unsynced")]
        lines = ["What it did to each row:"]
        untouched = [row for row, end in ends.items() if all(c == "not run" for _, c in end.cells)]
        for row, end in ends.items():
            if row in untouched:
                continue
            cells = dict(end.cells)
            if end.retired:
                lost = [s for s in SAVING if cells.get(s, "").startswith("failed")]
                lines.append(f"- {row}: retired, torn down unsynced ({', '.join(lost)} failed)."
                             if lost else f"- {row}: retired, every step done.")  # fmt: skip
                continue
            stages = [s for s, _ in end.cells]
            tail = len(stages)  # the steps never ended: the one the restart cut, and after
            while tail and cells[stages[tail - 1]] == "not run":
                tail -= 1
            last = stages[tail - 1] if tail else ""
            at = last if cells.get(last, "").startswith("failed") else (
                stages[tail] if tail < len(stages) else last
            )  # fmt: skip
            done = [s for s in stages[:tail] if not cells[s].startswith("failed")]
            failed = [s for s in stages[:tail] if cells[s].startswith("failed")]
            said = f"- {row}: done {', '.join(done) or 'nothing'}"
            said += f"; failed {', '.join(failed)}" * bool(failed)
            said += f"; not done {', '.join(stages[tail:])}" * (tail < len(stages))
            again = " ".join([f"!retire {row}", *(w for w in words if not (
                w == "export" and cells.get("export") == "ok"))])  # fmt: skip
            gone = cells.get("teardown") == "ok" or cells.get("teardown", "").startswith("skip")
            if gone:
                said += f". Its VM is gone: `!retire {row} unsynced` ends its bots."
            elif at == "teardown" or "no VM" in cells.get(at, ""):  # as `stopped_row` says it
                said += (
                    f". Its VM may be gone: send `{again}` again; if its plan finds no VM, "
                    f"`!retire {row} unsynced`."
                )
            else:
                said += f". Its VM is as those steps left it: send `{again}` again."
            lines.append(said)
        if untouched:  # one line for the rows it never reached (Codex round 1, rework)
            again = " ".join(["!retire", *untouched, *words])
            lines.append(f"- not started: {', '.join(untouched)}. Send `{again}` again.")
        return "\n".join(lines)

    async def tick(self) -> None:
        """Expire the plans whose TTL has passed (§2.5), and say so in their topics; record
        each console sign-in since the last tick (§2.8, `console_login`)."""
        assert self.server.store is not None
        for plan in self.server.store.expire_due():
            head = await self.server.audit("expired", **self._fields(plan))
            await self._say(plan, f"⌛ {plan.name} expired: nothing was done. Request again.", head)
        if self.console is not None:
            for used in await asyncio.to_thread(self.console.used):
                await self.server.audit(
                    "console_login",
                    workshop=used.where,
                    bot=used.bot,
                    requester={"id": used.user_id, "name": used.name},
                )
        now = self.server.clock()  # a clock set back by more than a look looks at once too
        if now >= self.month_end_next or now < self.month_end_next - MONTH_END_EVERY:
            self.month_end_next = self.server.clock() + MONTH_END_EVERY
            for ws in self.server.config.workshops:
                try:  # one workshop's failure never keeps the others' warnings back
                    await self.month_end(ws)
                except Exception as exc:
                    LOG.warning("%s: the month end's look failed (%s)", ws.name,
                                type(exc).__name__, exc_info=exc)  # fmt: skip

    async def month_end(self, ws: Workshop) -> None:
        """The month-end schedule's look at `ws` (§11.5, #570; `zab.admin.monthend`): each
        scheduled row that changed since its approval drops out, and a row a restart left
        `running` too, unless all its steps are done; each warning due goes by DM from the
        workshop's bot; what happened is said once in the schedule's topic, with a `month_end`
        record; then the rows due start a run (`_run`)."""
        server = self.server
        assert server.store is not None
        db = server.store.db
        held = monthend.entries(db, ws.name)
        scheduled = [e for e in held if e.state == monthend.SCHEDULED]
        running = [e for e in held if e.state == monthend.RUNNING]
        if not (scheduled or running):
            return
        fleet = await server.read_fleet(ws)
        roster = await asyncio.to_thread(read_roster, ws.roster)
        people = {r["slug"]: r.get("zulip_user_id", "") for r in roster}
        repos = {r["slug"]: r.get("repo", "") for r in roster}
        said: dict[int, dict[str, list[str]]] = {}
        idle = self.busy(ws.name) is None  # a run applying now holds its rows: never touched
        for entry in running if idle else []:  # a run a restart cut short, before it resumes
            heard = said.setdefault(entry.plan, {})
            finished = monthend.done(db, entry)
            if entry.slug in fleet.retired:
                # retired on the log, by its bots step's record at its place (#855), even when
                # a cut came before `state.db` had it (#860's review, B1): done, with its line
                monthend.settle(db, entry, monthend.DONE, "retired", server.clock())
                heard.setdefault("retired", []).append(entry.slug)  # its line (#840)
            elif set(finished) >= set(monthend.LAST_STAGES):
                # every step in `state.db`, its bots' deactivation not on the log: a run of a
                # development build between #835 and #860, never a release, which wrote no step
                # records (#855), or a plan since; the log decides
                monthend.settle(db, entry, monthend.STOPPED, monthend.UNLOGGED, server.clock())
                heard.setdefault("unrecorded", []).append(entry.slug)
            elif why := await asyncio.to_thread(monthend.changed, entry, fleet, people):
                monthend.settle(db, entry, monthend.DROPPED, why, server.clock())
                heard.setdefault("dropped", []).append(f"{entry.slug} ({why})")
        sending, sent = True, 0  # the look's DMs stop at the first that fails, or its cap
        for entry in scheduled:
            heard = said.setdefault(entry.plan, {})
            try:  # one row's error never keeps the others back (review of #829, N3)
                now = server.clock()
                if why := await asyncio.to_thread(monthend.changed, entry, fleet, people):
                    if monthend.drop(db, entry, why, now):  # unless it changed meanwhile
                        heard.setdefault("dropped", []).append(f"{entry.slug} ({why})")
                    continue
                if stop := monthend.refused(entry, now):  # #883: a refusal stops the row
                    if monthend.refuse(db, entry, stop, now):
                        heard.setdefault(f"{stop} {monthend.REFUSED}", []).append(entry.slug)
                    continue
                kind = monthend.due(entry, now)
                if kind is None or not sending:
                    continue
                how = await self._warn(server.bot_of(ws), ws, entry, kind, repos, now)
            except Exception as exc:
                LOG.warning("%s: the month end's look at %s failed (%s)", ws.name, entry.slug,
                            type(exc).__name__, exc_info=exc)  # fmt: skip
                continue
            if how:
                heard.setdefault(f"{kind} {how}", []).append(entry.slug)
            sent += how not in (monthend.NO_ID, "")
            # B3 of #829's review: a failure of any kind ends the look's DMs, so that a blip
            # costs one row's warning at most; N1: so does the cap
            if how not in (monthend.SENT, monthend.NO_ID) or sent >= monthend.LOOK_DMS:
                sending = False
        for plan_id, heard in said.items():
            if heard:
                await self._warned(ws, plan_id, heard)
        await self._run(ws)

    async def _run(self, ws: Workshop) -> None:
        """A month-end run of `ws`'s due rows (`monthend.due_rows`), when no plan of the
        workshop applies or is handed over, and its last `!protect` is `PROTECT_SETTLE` old
        (§11.5: that refusal is a wait). The fleet is read afresh, then, with no await, the
        checks, the run's plan made APPLYING (a restart marks it INTERRUPTED), the workshop
        marked busy, and the run spawned (`Runner.month_end`)."""
        server = self.server
        assert server.store is not None
        store, bot = server.store, server.bot_of(ws)
        fleet = await server.read_fleet(ws)
        # From here to the busy mark: no await.
        now = server.clock()
        due = monthend.due_rows(monthend.entries(store.db, ws.name), now)
        if not due or self.busy(ws.name) is not None:
            return
        if fleet.protection is not None and fleet.protection.wait(now):
            return
        if now < self.month_end_held.get(ws.name, (0, 0.0))[1]:  # after errors (`_abandoned`)
            return
        if next_plan_id(store) > MAX_PLAN_ID:
            LOG.error("plan IDs above P-%d can't be named in commands: no run made", MAX_PLAN_ID)
            return
        schedule = store.get(due[0].plan)
        where = self._where(due[0].plan)
        if schedule is None or where is None:  # never: a schedule's plan has its topic
            return
        command = "!retire " + " ".join(e.slug for e in due)
        plan = store.begin(
            workshop=ws.name, bot=bot.user_id, requester=bot.user_id, command=command
        )
        plan = store.move(plan.id, (PLANNING,), APPLYING, f"a month-end run of {schedule.name}")
        try:  # APPLYING, so busy (`Runner.busy`): an error from here on fails it (round 5)
            with store.db:
                store.db.execute(
                    "INSERT INTO plan_topics (plan, stream, topic) VALUES (?, ?, ?)",
                    (plan.id, where[0], where[1]),
                )
            monthend.begin_run(store.db, plan.id, schedule.id, due)
        except Exception as exc:
            LOG.warning("%s: the month-end run can't begin (%s)", plan.name, type(exc).__name__)
            await self._abandoned(plan, type(exc).__name__, bot, where, schedule)
            return
        self.handed[ws.name] = plan.id
        self.spawn(self._started(plan, bot, where, fleet, schedule, due),
                   then=lambda: self._release(ws.name, plan.id))  # fmt: skip

    async def _started(
        self, plan: Plan, bot: AdminBot, where: tuple[int, str], fleet: Fleet, schedule: Plan,
        due: list[monthend.Entry],
    ) -> None:  # fmt: skip
        """The run's `request` record, which hands its ID out (`fleet.Ledger.floor`), then the
        run itself; an error that escapes either fails the plan if it is still APPLYING
        (`_abandoned`, Codex round 5 on #835). The bot stopping leaves it APPLYING, for the
        next start to mark INTERRUPTED."""
        try:
            await self.server.audit(
                "request", **self._fields(plan),
                requester={"id": bot.user_id, "name": f"the month-end schedule {schedule.name}"},
                verb="retire", args=[e.slug for e in due], schedule=schedule.name,
            )  # fmt: skip
            await self.runner.month_end(plan, bot, where, fleet, schedule, due)
        except Exception as exc:
            LOG.warning("%s: the month-end run failed (%s)", plan.name, type(exc).__name__,
                        exc_info=exc)  # fmt: skip
            await self._abandoned(plan, type(exc).__name__, bot, where, schedule)
        else:
            self.month_end_held.pop(plan.workshop, None)

    async def _abandoned(
        self, plan: Plan, why: str, bot: AdminBot, where: tuple[int, str], schedule: Plan
    ) -> None:
        """A month-end run that an error ended before its apply did (Codex round 5 on #835):
        FAILED, if it is still APPLYING, which would keep its workshop busy for every later
        run and approval until a restart (`Runner.busy` reads `state.db`). Its rows stay as
        they are: those `running` resume at a later look, at their first unfinished step, or,
        every step done, are recorded retired (`_retired`); those still `scheduled` run then.
        The next run of the workshop waits twice as long after each such error in a row, from
        2 minutes up to `MONTH_END_HELD` (review of #835, N1); each says so in the schedule's
        topic, with a `month_end` record if it can be written."""
        store = self.server.store
        assert store is not None
        current = store.get(plan.id)
        failures = self.month_end_held.get(plan.workshop, (0, 0.0))[0] + 1
        wait = min(MONTH_END_EVERY * 2**failures, MONTH_END_HELD)
        self.month_end_held[plan.workshop] = (failures, self.server.clock() + wait)
        if current is None or current.state != APPLYING:
            return
        note = f"the run failed before it ended ({why}): its rows are taken up at a later look"
        store.move(plan.id, (APPLYING,), FAILED, note[:NOTE_CHARS])
        head = None
        try:
            head = await self.server.audit(
                "month_end", **self._fields(plan), what="run", reason=note, wait_s=wait
            )
        except Exception as exc:
            LOG.warning("%s: its month_end record not written (%s)", plan.name,
                        type(exc).__name__)  # fmt: skip
        text = (
            f"❌ {plan.name}, a month-end run of {schedule.name}, failed before it ended ({why}). "
            f"Its rows are taken up again in {round(wait / 60)} min at the earliest, each from "
            "its first unfinished step. The bot's log says why: tell a deployer."
        )
        await bot.post(where[0], where[1], text + (f"\n{footer(head)}" if head else ""))

    async def _warn(
        self, bot: AdminBot, ws: Workshop, entry: monthend.Entry, kind: str,
        repos: Mapping[str, str], now: float,
    ) -> str:  # fmt: skip
        """One warning by DM, recorded before its post, and only while the schedule still holds
        it (`monthend.claim`: no cancel or replacing schedule since the look read it). How it
        went: `SENT` once Zulip gave the message's ID; `UNSURE` when it may have landed, never
        sent again; `REFUSED` once Zulip refused it with a 4xx `TRIES` times; or "" when it
        wasn't claimed, or wasn't sent but may be at the next look: refused fewer times, or
        never reaching Zulip (no connection, its rate limit), which is never counted as a try.
        A notice refused fewer times is taken back, a reminder recorded refused, which keeps
        its row from running until it goes (`monthend.refused`). Refused at every try, it stops
        its row (`monthend.refuse`, #883): "" when a cancel, or a schedule with another end or
        recipient, landed meanwhile, and there was none to stop.
        The claim records it `UNSURE`, which covers it as sent, and only Zulip's answer makes
        it `SENT`: a restart while the post is out leaves it `unsure`, never `sent` (review
        of #829, B2 and B3)."""
        db = self.server.store.db  # type: ignore[union-attr]
        if not entry.zulip_user_id.isdigit():
            claimed = monthend.claim(db, entry, kind, monthend.NO_ID, now)
            return monthend.NO_ID if claimed else ""
        write = monthend.notice if kind == monthend.NOTICE else monthend.reminder
        if kind == monthend.NOTICE:  # a late notice names its own end (`monthend.run_at`)
            text = monthend.notice(entry, ws.name, repos.get(entry.slug, ""),
                                   monthend.end_of(entry, now))  # fmt: skip
        else:
            text = write(entry, ws.name, repos.get(entry.slug, ""))
        # first, and then the post with no await between: never sent twice, nor once cancelled
        if not monthend.claim(db, entry, kind, monthend.UNSURE, now):
            return ""
        posted, failed = await bot.delivered(
            type="private", to=[int(entry.zulip_user_id)], content=text
        )
        if posted is not None:
            monthend.warn(db, entry, kind, monthend.SENT, now)
            return monthend.SENT
        if failed == monthend.UNSURE:  # it may have landed: never sent again
            return monthend.UNSURE
        # a reminder refused at a try stays refused until it goes: owed, its row never runs,
        # and once the end has come it stops the row (`monthend.refused`, Codex round 1)
        owed = monthend.REFUSED if kind == monthend.REMINDER else None
        if failed == monthend.UNREACHED:  # never reached Zulip: no try counted
            was = any((k, h) == (kind, monthend.REFUSED) for k, _, h in entry.warned)
            monthend.warn(db, entry, kind, owed if was else None, now)
            return ""
        key = self._key(ws, entry, kind)
        tried = self.tries[key] = self.tries.get(key, 0) + 1
        if tried < monthend.TRIES:
            monthend.warn(db, entry, kind, owed, now)  # taken back, or owed: tried again
            return ""
        return monthend.REFUSED if monthend.refuse(db, entry, kind, now) else ""

    @staticmethod
    def _key(
        ws: Workshop, entry: monthend.Entry, kind: str
    ) -> tuple[str, str, str, float, str, int]:
        """A warning's key in `tries`: its row's, its end's, its recipient's and its schedule's."""
        return (ws.name, entry.slug, kind, entry.ends, entry.zulip_user_id, entry.plan)

    async def _warned(self, ws: Workshop, plan_id: int, heard: dict[str, list[str]]) -> None:
        """One line in the schedule's topic for what one look did, and its `month_end`
        record."""
        server = self.server
        assert server.store is not None
        plan = server.store.get(plan_id)
        if plan is None:
            return
        head = await server.audit(
            "month_end", workshop=ws.name, bot=plan.bot, plan=plan.name, what="look",
            rows={key: list(rows) for key, rows in heard.items()},
            targets=sorted({row.split(" ")[0] for rows in heard.values() for row in rows}),
        )  # fmt: skip
        await self._say(plan, monthend.look_line(plan.name, heard), head)

    async def handle(
        self, bot: AdminBot, m: Incoming, command: Command, fleet: Fleet | None
    ) -> None:
        """Quick: a plan's preparation and `!status` run as their own tasks."""
        if command.verb in ("approve", "reject"):
            await self.answer(bot, m, command)
        elif command.verb == "month-end cancel":  # a workshop's verb: its bot has a fleet
            await self.cancel(bot, m, command, fleet)
        elif command.verb == "status":
            self.spawn(self.status(bot, m, command, fleet))
        elif command.verb == "console":
            await self.sign_in(bot, m, command)
        elif command.verb == "new-workshop":  # the grammar gives it to the [global] bot only
            if m.sender_id in self.server.config.admins:
                self.spawn(self.request(bot, m, command, None))
            else:  # a deployer, whom the [global] bot recognizes too (§2.9)
                await bot.say(m, "❌ only a global admin requests `!new-workshop`")
        elif command.verb == "upgrade-server":  # the [global] bot's alone, too (§1.6, #569)
            if m.sender_id in self.server.config.deployers:
                self.spawn(self.request(bot, m, command, None))
            else:  # a global admin who is not a deployer
                await bot.say(m, "❌ only a deployer requests `!upgrade-server`")
        elif fleet is None:  # the grammar gives workshop verbs to workshop bots only
            await bot.say(m, f"❌ `!{command.verb}` is a workshop's command")
        else:
            self.spawn(self.request(bot, m, command, fleet))

    # ----------------------------------------------------------------------- the plan

    async def request(
        self, bot: AdminBot, m: Incoming, command: Command, fleet: Fleet | None
    ) -> None:
        """A plan of `command`, against the bot's own `fleet`; None for a server verb."""
        server, store = self.server, self.server.store
        assert store is not None and m.stream is not None
        if next_plan_id(store) > MAX_PLAN_ID:  # never an ID no command could name
            LOG.error("plan IDs above P-%d can't be named in commands: no plan made", MAX_PLAN_ID)
            await bot.say(
                m,
                f"❌ this server can't make plans: its next plan ID is above P-{MAX_PLAN_ID}, "
                "which commands can't name. Ask a deployer.",
            )
            return
        plan = store.begin(
            workshop=bot.where, bot=bot.user_id, requester=m.sender_id, command=command.text()
        )
        output = ""
        try:  # whatever happens from here, the plan never stays PLANNING, and its requester hears
            with store.db:
                store.db.execute(
                    "INSERT INTO plan_topics (plan, stream, topic) VALUES (?, ?, ?)",
                    (plan.id, m.stream, m.topic),
                )
            await server.audit(
                "request",
                **self._fields(plan),
                requester={"id": m.sender_id, "name": m.sender_name},
                verb=command.verb,
                args=list(command.tokens()),
            )
            why, output = await self._plan(bot, m, plan, command, fleet)
        except Exception as exc:
            LOG.warning("%s: planning failed (%s)", plan.name, type(exc).__name__, exc_info=exc)
            why = f"planning failed ({type(exc).__name__})"
        if why:
            await self._refuse(plan, why, output, where=(m.stream, m.topic), asker=m)

    async def _plan(
        self, bot: AdminBot, m: Incoming, plan: Plan, command: Command, fleet: Fleet | None
    ) -> tuple[str, str]:
        """Post `plan` and make it PENDING: ("", ""), or why it can't be, and any output."""
        server = self.server
        assert server.approvals is not None and m.stream is not None
        upload = None
        if command.upload:
            try:
                upload = await self._download(bot, plan, command)
            except (AttachmentRejected, ZulipError) as exc:  # their text names no URL
                return f"the CSV could not be downloaded: {str(exc) or type(exc).__name__}", ""
            except httpx.HTTPStatusError as exc:  # its text holds the signed download URL
                return f"the CSV could not be downloaded: HTTP {exc.response.status_code}", ""
            except (httpx.HTTPError, TimeoutError) as exc:  # so may theirs: the type alone
                return f"the CSV could not be downloaded: {type(exc).__name__}", ""
        try:
            action = self._build(plan, command, fleet, upload)
        except ActionError as exc:
            return str(exc), ""
        if action.verb == "month-end schedule":
            action = await self._noted(action, fleet)
        elif action.verb == "keep-until" and command.until != "none":
            action = self._kept(action, command.until)
        if action.verb in ("teardown", "retire") and (why := self._protecting(bot.where)):
            return why, ""
        async with self.preparing.setdefault(bot.where, asyncio.Semaphore(PREPARING)):
            prepared = await self.runner.prepare(plan, action, fleet)
        if prepared.unchanged and not prepared.ok:
            await self._unchanged(plan, action, prepared, where=(m.stream, m.topic))
            return "", ""
        if not prepared.ok:
            return prepared.why or "the dry run failed", prepared.output
        digest = fingerprint(action, release=server.release, live=prepared.live)
        needs = requirement(action, fleet, server.config)
        read = None  # its scrub and its table's cells, a scrub each: in a thread
        if action.status:
            read = await asyncio.to_thread(StatusRead.of, prepared.status, server.scrubber)
        head = await server.audit(
            "plan",
            **self._fields(plan),
            verb=action.verb,
            args=list(action.args),
            hash=digest,
            argv=[list(step.argv) for step in action.steps],
            targets=list(action.targets),
            approval={"class": needs.klass, "approvers": needs.needed, "phrase": phrase(needs)},
        )
        message = plan_message(
            plan,
            action,
            digest,
            needs,
            requester=mention(m.sender_id, m.sender_name),
            expires=server.clock() + server.config.plan_ttl_minutes * 60,
            output=server.scrub(prepared.output or "(no output)"),
            head=head,
            limit=bot.client.limits.get("max_message_length", 10000),
            status=read,
        )
        text = message.text
        if message.full is not None:
            link = await self._attach(bot, f"{plan.name}-plan.txt", message.full)
            if link is None:
                return "the plan is too long to show, and its attachment failed", ""
            text = text.replace(ATTACH, f"The whole plan: {link}.")
        if len(text) > bot.client.limits.get("max_message_length", 10000) - MARGIN // 2:
            return "the plan doesn't fit in a message", ""  # never posted cut (review of #346)
        plan_post = await bot.post(m.stream, m.topic, text)
        if plan_post is None:
            return "the plan message could not be posted", ""
        command_text = server.scrub(short(plan.command))
        content, widget = approval_message(plan, digest, needs, command_text)
        buttons = (
            {} if widget is None else {"widget_content": json.dumps(widget, ensure_ascii=False)}
        )
        approval = await bot.send(
            type="stream", to=m.stream, topic=m.topic, content=content, **buttons
        )
        if approval is None:
            return "the approval message could not be posted", ""
        server.approvals.post(
            plan.id, action, fleet, digest, plan_message=plan_post, approval_message=approval
        )
        return "", ""

    async def _noted(self, action: Action, fleet: Fleet | None) -> Action:
        """A `!month-end schedule` plan with its notes on the schedule as it is: the rows it
        would bring back, which the schedule holds cancelled, so that its approval undoes that
        veto (review of #824, N3); and first, each row with no Zulip ID, whom no warning can
        reach, and whom approving the plan ends without one (#570's plan; review of #829, N6;
        #883). Notes are shown, never hashed."""
        assert self.server.store is not None
        notes = list(action.notes)
        held = monthend.entries(self.server.store.db, action.workshop)
        back = [e for e in held if e.state == monthend.CANCELLED and e.slug in action.targets]
        if back:
            named, it = (
                "; ".join(f"{e.slug} ({e.note})" for e in back),
                "them" if back[1:] else "it",
            )
            out = " ".join(f"`!keep-until none {e.slug}`" for e in back)
            notes.append(
                f"**Cancelled, and back with this plan:** {named}. Approving it schedules {it} "
                f"again; to keep {it} out, first {out}."
            )
        if fleet is not None:
            roster = await asyncio.to_thread(read_roster, fleet.workshop.roster)
            ids = {r["slug"]: r.get("zulip_user_id", "") for r in roster}
            unreached = [s for s in action.targets if not ids.get(s, "").isdigit()]
            if unreached:  # first, and every row named: what the approval consents to (#883)
                # what approving does before the list, which a plan cut to fit may cut short
                # (`plan_message` gives its notes a quarter of the room; review of #908, N5)
                n = len(unreached)
                rows, whose, listed = ("this row", "its participant", "The row") if n == 1 else (
                    "each of these rows", "their participants", f"The rows ({n})"
                )  # fmt: skip
                notes.insert(0, (
                    "**No Zulip ID in the roster, so no warning can reach them: approving this "
                    f"plan ends {rows} at its date all the same, with no notice or reminder.** "
                    f"Tell {whose} yourselves, or, for the bot to warn them, bind them first "
                    "(`!provision zulip`), then send `!month-end schedule` again. "
                    f"{listed}: {', '.join(unreached)}."
                ))  # fmt: skip
        return dataclasses.replace(action, notes=tuple(notes))

    def _kept(self, action: Action, until: str | None) -> Action:
        """A dated `!keep-until` plan with how its rows end, from the workshop's month-end
        schedule as it is (`monthend.keep_until_note`, #868), and the month-end run applying in
        the workshop, if one is (`busy`, `monthend.run_of`). One that can't be read leaves the
        plan as it is, with a note that says so (review of #871, N1). Notes are shown, never
        hashed."""
        assert self.server.store is not None and until is not None
        db, ws = self.server.store.db, action.workshop
        try:
            held = monthend.entries(db, ws)
            busy = self.busy(ws)
            applying = busy is not None and monthend.run_of(db, busy) is not None
            run = f"P-{busy}" if applying else None
            notes = monthend.keep_until_note(held, action.targets, until, ws, run)
        except (sqlite3.Error, ValueError) as exc:
            LOG.warning("%s: the month-end schedule can't be read (%s)", ws, type(exc).__name__)
            notes = [
                f"**The month-end schedule can't be read** ({type(exc).__name__}): "
                "`!month-end show` says whether it holds these rows (ORGANIZERS §5.7)."
            ]
        return dataclasses.replace(action, notes=(*action.notes, *notes))

    def _build(
        self, plan: Plan, command: Command, fleet: Fleet | None, upload: str | None = None
    ) -> Action:
        """The plan's action: against the bot's own fleet, or, for a server verb (`fleet`
        None), against admin.toml (`newworkshop`, §2.9)."""
        server = self.server
        if fleet is not None:
            return build(command, fleet, layout=server.paths.layout, upload=upload, plan=plan.name)
        if command.verb == "upgrade-server":  # §1.6, #569
            return upgradeserver.build(
                command, server.config, plan=plan.name, bot=plan.bot, layout=server.paths.layout
            )
        return newworkshop.build(
            command,
            server.config,
            plan=plan.name,
            bot=plan.bot,
            layout=server.paths.layout,
            workshops=server.paths.workshops,
            secrets=server.paths.secrets,
            config_path=server.config_path,
        )

    async def _download(self, bot: AdminBot, plan: Plan, command: Command) -> str:
        """`!roster import`'s CSV, with the bot's own key, to `runs/P-<n>/roster-upload.csv`:
        at most `MAX_UPLOAD` bytes within `DOWNLOAD_SECONDS`, however slowly they come."""
        assert command.upload is not None  # the grammar's `/user_uploads/…` path
        async with asyncio.timeout(DOWNLOAD_SECONDS):
            found = await bot.client.request("GET", command.upload.removeprefix("/"))
            data, _ = await bot.client.download(found["url"], MAX_UPLOAD)
        path = Path(self.server.paths.layout.runs) / plan.name / "roster-upload.csv"
        await asyncio.to_thread(_save, path, data)
        return str(path)

    async def _attach(self, bot: AdminBot, name: str, text: str) -> str | None:
        """`text`, scrubbed, as an attachment: its Markdown link, or None."""
        try:
            url = await bot.client.upload(name, self.server.scrub(text).encode("utf-8"))
        except Exception as exc:
            LOG.warning("%s: the attachment failed (%s)", name, type(exc).__name__)
            return None
        return f"[{name}]({url})"

    async def _block(self, bot: AdminBot, text: str, name: str) -> str:
        """Script output for a post: scrubbed and fenced, at most 40 lines, and the whole of
        it attached, with a note, when that cuts it (§2.7 item 5)."""
        block = self.server.scrubber.block(text or "(no output)")
        if not block.truncated:
            return block.inline
        link = await self._attach(bot, name, block.full)
        if link is None:
            return f"{block.inline}\n*Cut to 40 lines; the attachment of the rest failed.*"
        return f"{block.inline}\n*Cut to 40 lines: the whole of it is {link}.*"

    async def _refuse(
        self,
        plan: Plan,
        why: str,
        output: str = "",
        *,
        where: tuple[int, str] | None = None,
        asker: Incoming | None = None,
    ) -> None:
        """REFUSED, recorded and said in the plan's topic (`where`, if it has no record yet),
        with the hint for a refused `!roster import`'s taken slug last (`_hint`, from the
        request `asker`): each step best effort, so that a failing audit log still leaves the
        plan refused and its requester answered."""
        server = self.server
        assert server.store is not None
        why = " ".join(why.split())[:NOTE_CHARS]
        try:
            plan = server.store.refuse(plan.id, why)
        except PlanError:
            plan = server.store.need(plan.id)  # posted meanwhile: it stands as it is
            if plan.state != PLANNING:
                return
        head = None
        try:
            head = await server.audit("refused", **self._fields(plan), reason=why)
        except Exception as exc:
            LOG.warning("%s: its refusal was not audited (%s)", plan.name, type(exc).__name__)
        bot = self._bot(plan)
        shown = ""
        if output and bot is not None:
            shown = "\n" + await self._block(bot, output, f"{plan.name}-output.txt")
        hint = await self._hint(plan, output, asker) if asker is not None else ""
        text = f"❌ {plan.name} refused: {why}; nothing was done{shown}" + f"\n{hint}" * bool(hint)
        await self._say(plan, text, head, where=where)

    async def _unchanged(
        self, plan: Plan, action: Action, prepared: Prepared, *, where: tuple[int, str]
    ) -> None:
        """UNCHANGED (#708): its dry run says there is nothing to change, so no plan is posted
        and no approval asked. Recorded (`unchanged`, never `refused`) and said in the plan's
        topic as a success, `☑`, with the dry run's output under it, whose last line says why.
        Each step best effort, as `_refuse`'s."""
        server = self.server
        assert server.store is not None
        why = " ".join(prepared.why.split())[:NOTE_CHARS]
        try:
            plan = server.store.unchanged(plan.id, why)
        except PlanError:
            plan = server.store.need(plan.id)  # posted meanwhile: it stands as it is
            if plan.state != PLANNING:
                return
        head = None
        try:
            fields = self._fields(plan) | {"verb": action.verb}
            head = await server.audit("unchanged", **fields, reason=why)
        except Exception as exc:
            LOG.warning("%s: its outcome was not audited (%s)", plan.name, type(exc).__name__)
        bot = self._bot(plan)
        shown = ""
        if prepared.output and bot is not None:
            shown = "\n" + await self._block(bot, prepared.output, f"{plan.name}-output.txt")
        await self._say(plan, unchanged_title(plan, why) + shown, head, where=where)

    async def _hint(self, plan: Plan, output: str, asker: Incoming) -> str:
        """The bot's own hint under a `!roster import` refused for a slug that another workshop
        has (`_TAKEN`; `AdminServer.pointer`), once the refusal is recorded, so that a hint is
        spent only on a refusal that is posted; "" on any failure, logged by its type: the
        refusal is posted all the same (the review of #506)."""
        if plan.command.split()[:2] not in (["!roster", "import"], ["!roster", "rename"]):
            return ""  # its first two words (#510); a rename's new slug too (#627)
        if not (taken := _TAKEN.search(output)):
            return ""
        bot = self._bot(plan)
        if bot is None:
            return ""
        try:
            return await self.server.pointer(bot, asker, taken["slug"], only=taken["ws"])
        except Exception as exc:
            LOG.warning("%s: no hint for its taken slug (%s)", plan.name, type(exc).__name__)
            return ""

    # --------------------------------------------------------------------- approvals

    def busy(self, workshop: str) -> int | None:
        """The ID of the workshop's plan that is applying, or approved and handed over. A server
        plan (`workshop` "") needs every workshop's lock (§2.4 step 5): it is busy while any
        workshop is, and every workshop is while it is."""
        every = [ws.name for ws in self.server.config.workshops]
        names = [workshop, ""] if workshop else ["", *every]
        for name in names:
            plan = self.runner.busy(name)
            found = plan.id if plan is not None else self.handed.get(name)
            if found is not None:
                return found
        return None

    def _protecting(self, workshop: str) -> str:
        """#378: why a `!teardown` or `!retire` plan waits while the workshop's busy plan changes
        termination protection (`fleet.protects`), however long it applies; "" otherwise. After
        its end, the ledger's `Protection` takes over (`actions.build`, at plan time and at
        apply)."""
        assert self.server.store is not None
        busy = self.busy(workshop)
        plan = self.server.store.get(busy) if busy is not None else None
        if plan is None:
            return ""
        verb, *args = plan.command.removeprefix("!").split() or [""]
        return protect_applying(plan.name, verb) if protects(verb, args) else ""

    async def answer(self, bot: AdminBot, m: Incoming, command: Command) -> None:
        """`!approve P-n hash6 [phrase]` and `!reject P-n` (§2.5)."""
        server = self.server
        assert server.store is not None and server.approvals is not None
        assert command.plan is not None and m.stream is not None
        try:  # afresh, never from a cache: active and human at approval time (§2.5)
            user = (await bot.client.request("GET", f"users/{m.sender_id}"))["user"]
        except Exception as exc:
            LOG.warning("GET /users failed (%s)", type(exc).__name__)
            await bot.say(m, "❌ your account could not be checked: nothing was recorded")
            return
        # From here to the approval's record and the busy mark: no await (§2.4 step 5).
        plan = server.store.get(command.plan)
        if plan is not None and plan.bot == bot.user_id and plan.state == PLANNING:
            # it has no approvers yet: "not an approver" would mislead (re-review of #346)
            head = await server.audit(
                "refused",
                **(self._fields(plan) | {"bot": bot.user_id}),
                sender={"id": m.sender_id, "name": m.sender_name},
                code="planning",
                reason=f"{plan.name} is still being prepared",
            )
            await bot.say(
                m,
                f"❌ {plan.name} is still being prepared: answer its approval message once "
                f"it is posted. Nothing was recorded.\n{footer(head)}",
            )
            return
        if command.verb == "approve" and plan is not None and plan.bot == bot.user_id:
            busy = self.busy(plan.workshop)
            if busy is not None and busy != plan.id:
                await bot.say(
                    m,
                    f"busy: P-{busy} is applying. Nothing was recorded: send the approval "
                    f"again once it is done, before {plan.name} expires.",
                )
                return
        sender = Sender(m.sender_id, user.get("is_bot") is not False, user.get("is_active") is True)
        received = Received(m.id, bot.user_id, m.stream, sender)
        if command.verb == "approve":
            assert command.hash6 is not None
            decision = server.approvals.approve(
                received, command.plan, command.hash6, command.phrase
            )
        else:
            decision = server.approvals.reject(received, command.plan)
        approved = decision.plan if decision.code == "approved" else None
        if approved is not None:
            self.handed[approved.workshop] = approved.id
        spawned = False
        try:  # the mark lasts until the apply's task ends: released here if it never starts
            event = (
                ("reject" if decision.code == "rejected" else "approval")
                if decision.ok
                else "refused"
            )
            # A plan the store doesn't have is "named", never "plan": the floor of plan IDs
            # reads only what was handed out (a typo like P-999999999 must not move it).
            fields = (
                self._fields(decision.plan) if decision.plan else {"named": f"P-{command.plan}"}
            )
            head = await server.audit(
                event,
                **(fields | {"bot": bot.user_id}),
                sender={"id": m.sender_id, "name": m.sender_name},
                code=decision.code,
                reason=decision.reason,
            )
            if (
                decision.plan is not None
                and decision.plan.state == EXPIRED
                and decision.code == "expired"
            ):
                await server.audit("expired", **self._fields(decision.plan))
            mark = "✅" if decision.ok else "❌"
            await bot.say(m, f"{mark} {decision.reason}\n{footer(head)}")
            if approved is not None:
                spawned = await self._apply(bot, approved)
        finally:
            if approved is not None and not spawned:
                self._release(approved.workshop, approved.id)

    async def cancel(
        self, bot: AdminBot, m: Incoming, command: Command, fleet: Fleet | None
    ) -> None:
        """`!month-end cancel SEL` (§11.5, #570): the named rows out of the workshop's month-end
        schedule, at once, as `!reject` vetoes a plan: from an approver of the workshop, human
        and active as `GET /users/{id}` says now (§2.5). It only shrinks what the bot will do.
        Audited (`month_end`), with the rows it cancelled. Refused while a `!month-end schedule`
        of the workshop is approved or applying (Codex round 1 on #824): its apply replaces the
        schedule, and would bring a row cancelled meanwhile back; the check and the cancel come
        with no await between them, so nothing applies in between."""
        server, ws = self.server, bot.workshop
        assert server.store is not None and ws is not None and fleet is not None
        if m.sender_id not in server.config.approvers(ws):
            await self._uncancelled(bot, m, "not-approver", f"only an approver of {ws.name} "
                                    "cancels its month end: nothing was done")  # fmt: skip
            return
        try:  # afresh, as for an approval
            user = (await bot.client.request("GET", f"users/{m.sender_id}"))["user"]
        except Exception as exc:
            LOG.warning("GET /users failed (%s)", type(exc).__name__)
            await bot.say(m, "❌ your account could not be checked: nothing was done")
            return
        if user.get("is_bot") is not False or user.get("is_active") is not True:
            await self._uncancelled(bot, m, "inactive", "only an active human account "
                                    "cancels: nothing was done")  # fmt: skip
            return
        assert command.selection is not None
        try:  # the roster may have changed since the command was parsed
            named = command.selection.targets(Roster(fleet.slugs, fleet.spares))
        except ValueError as exc:
            await bot.say(m, f"❌ {exc}: nothing was done")
            return
        # From here to the cancel: no await (the docstring)
        busy = self.busy(ws.name)
        replacing = server.store.get(busy) if busy is not None else None
        if replacing is not None and replacing.command == "!month-end schedule":
            await self._uncancelled(
                bot, m, "busy",
                f"busy: {replacing.name}, a `!month-end schedule`, is applying, and replaces the "
                "schedule. Nothing was cancelled: send the cancel again once its result is "
                "posted.", mark="",
            )  # fmt: skip
            return
        now, who = server.clock(), shown_name(m.sender_name)
        db = server.store.db
        running = {e.slug: e for e in monthend.entries(db, ws.name)
                   if e.state == monthend.RUNNING and e.slug in named}  # fmt: skip
        done, other = monthend.cancel(db, ws.name, named, f"by {who}, {utc(now)}", now)
        head = await server.audit(
            "month_end",
            workshop=ws.name,
            bot=bot.user_id,
            requester={"id": m.sender_id, "name": m.sender_name},
            what="cancel",
            targets=done,  # the console's projection reads `targets` (review of #824, N5)
            named=list(named),
        )
        listed = monthend.listed
        if not done:
            text = (
                f"❌ {listed(other)}: not in the month-end schedule, or ended already: nothing "
                "was done. `!month-end show` shows the schedule."
            )
        else:
            text = (
                f"✅ The month end of {listed(done)} is cancelled: the schedule ends none of them."
            )
            text += self._mid_run([s for s in done if s in running], running)
            if rest := [s for s in done if s not in running]:
                text += (
                    f" {'Each VM' if len(rest) == len(done) else 'The VM of ' + listed(rest)} "
                    "stays until a `!retire`, or a new `!month-end schedule`."
                )
            text += f" Not in the schedule: {listed(other)}." * bool(other)
            pending = [p.name for p in server.store.pending(bot.user_id)
                       if p.command == "!month-end schedule"]  # fmt: skip
            if pending:
                text += (
                    f" {pending[0]}, pending, would schedule them again: `!reject {pending[0]}` "
                    "if it shouldn't."
                )
        await bot.say(m, f"{text}\n{footer(head)}")

    def _mid_run(self, mid: list[str], running: Mapping[str, monthend.Entry]) -> str:
        """What a cancel says of the rows it took out of a run (review of #835, B1): one applying
        now stops before its next step; one whose run failed or waits is never taken up again
        (round 2, N1); what the run did stays done either way; a row whose
        teardown is on record has no VM any more, and only `!retire … unsynced` ends its bots
        (the teardown cleared its `ip`, #604)."""
        if not mid:
            return ""
        store = self.server.store
        assert store is not None
        text = ""
        live = {s for s in mid if (p := store.get(int(running[s].note.removeprefix("P-"))))
                and p.state == APPLYING}  # fmt: skip
        for group, applying in (([s for s in mid if s in live], True),
                                ([s for s in mid if s not in live], False)):  # fmt: skip
            if not group:
                continue
            runs = " and ".join(sorted({running[s].note for s in group}))
            one = len(group) == 1
            if applying:
                text += (
                    f" {monthend.listed(group)} {'is' if one else 'are'} in run {runs} now: "
                    f"{'it stops before its' if one else 'they stop before their'} next step, "
                    "and what that run did stays done (its result lists it)."
                )
            else:  # its run failed, or is held: the next would have taken it up (round 2, N1)
                text += (
                    f" {monthend.listed(group)} {'was' if one else 'were'} held by run {runs}, "
                    "which isn't applying: no run takes "
                    f"{'it' if one else 'them'} up again, and what it did stays done."
                )
        gone = [s for s in mid if "teardown" in monthend.done(store.db, running[s])]
        if gone:
            text += (
                f" The VM of {monthend.listed(gone)} is gone, torn down already: "
                f"`!retire {gone[0]} unsynced` deactivates its bots and marks it retired."
            )
        return text

    async def _uncancelled(
        self, bot: AdminBot, m: Incoming, code: str, reason: str, mark: str = "❌ "
    ) -> None:
        """A `!month-end cancel` refused: a `refused` record, as a refused `!reject`'s, and the
        answer (review of #824, N5)."""
        head = await self.server.audit(
            "refused", workshop=bot.where, bot=bot.user_id, verb="month-end cancel",
            sender={"id": m.sender_id, "name": m.sender_name}, code=code, reason=reason,
        )  # fmt: skip
        await bot.say(m, f"{mark}{reason}\n{footer(head)}")

    def _release(self, workshop: str, plan_id: int) -> None:
        if self.handed.get(workshop) == plan_id:
            del self.handed[workshop]

    async def rebuild(self, bot: AdminBot, plan: Plan) -> tuple[Command, Action, Fleet | None]:
        """The plan's command, action and fleet, rebuilt from its record against the fleet as
        it is now (none for a server plan, against admin.toml). A `!roster import` uses the
        upload saved for it."""
        if bot.workshop is None:
            parsed = parse(plan.command, bot.context(None))
            if not isinstance(parsed, Command):
                raise ActionError(getattr(parsed, "reason", "the command no longer parses"))
            return parsed, self._build(plan, parsed, None), None
        fleet = await self.server.read_fleet(bot.workshop)
        upload = None
        if plan.command == "!roster import":
            command = Command("roster import")
            upload = str(Path(self.server.paths.layout.runs) / plan.name / "roster-upload.csv")
        else:
            parsed = parse(plan.command, bot.context(Roster(fleet.slugs, fleet.spares)))
            if not isinstance(parsed, Command):
                raise ActionError(getattr(parsed, "reason", "the command no longer parses"))
            command = parsed
        layout = self.server.paths.layout
        action = build(command, fleet, layout=layout, upload=upload, plan=plan.name)
        return command, action, fleet

    async def _apply(self, bot: AdminBot, plan: Plan) -> bool:
        """Hand the APPROVED `plan`, rebuilt, to the runner: True once its task is spawned,
        which releases the busy mark when it ends; False when it never starts (STALE)."""
        server = self.server
        assert server.store is not None
        where = self._where(plan.id)
        try:
            command, action, fleet = await self.rebuild(bot, plan)
        except (ActionError, FleetError, ValueError) as exc:
            why = " ".join(f"it no longer builds: {exc}".split())[:NOTE_CHARS]  # as a refusal's
            plan = server.store.move(plan.id, (APPROVED,), STALE, why)
            head = await server.audit("stale", **self._fields(plan), reason=plan.note)
            await self._say(plan, f"❌ {plan.name} is stale: {plan.note}. Request again.", head)
            return False
        if where is None:  # never: a plan is posted from its topic
            return False
        job = Job(plan, bot, where[0], where[1], command, action, fleet)
        self.spawn(self.runner.apply(job), then=lambda: self._applied(plan))
        return True

    def _applied(self, plan: Plan) -> None:
        """The apply's task has ended. A `!new-workshop` that is DONE has had its entry written
        by the config helper: the bots restart, which loads it (§2.9), and every workshop stays
        busy until then. Otherwise the workshop is free again."""
        store = self.server.store
        try:  # a done-callback: the store may be closing with the bot
            done = store is not None and store.need(plan.id).state == DONE
        except Exception:
            done = False
        if done and plan.command.startswith("!new-workshop "):
            if self.restart is not None:
                LOG.info("%s added a workshop: the bots restart to load it", plan.name)
                self.restart()
                return
            LOG.warning("%s added a workshop: restart the bots to load it", plan.name)
        if plan.command.startswith("!upgrade-server ") and release_moved(self.server.paths.layout):
            # whatever the outcome (#569): this process runs another release than `current`,
            # whose scripts its argument lists name, so it restarts into it; until then every
            # workshop stays busy (§2.4 step 5)
            if self.restart is not None:
                LOG.info("%s moved current: the bots restart into it", plan.name)
                self.restart()
                return
            LOG.warning("%s moved current: restart the bots, which run another release", plan.name)
        self._release(plan.workshop, plan.id)

    # -------------------------------------------------------------------- reads, console

    async def status(
        self, bot: AdminBot, m: Incoming, command: Command, fleet: Fleet | None
    ) -> None:
        """`!status SEL`: `status.py --json` through the runner (§3, action 6); no plan. The
        answer is the plans' table and a few readable lines (`status_reply`, #439), scrubbed,
        and the whole of it attached when cut (§2.7 item 5), then the workshop's cost line from
        the collector's snapshot (§11.2), the waker's line and the retired rows'; the answer is
        fitted with room for those and the audit footer (#767). The read is kept for the
        collector too (`collector.note_status`, §4.1)."""
        assert fleet is not None  # a workshop's command
        if bot.where in self.reading:
            await bot.say(m, "❌ a `!status` of this workshop is under way: wait for its answer")
            return
        self.reading.add(bot.where)
        # The read's time is its start: what it shows of a VM may predate anything that happens
        # while it runs, such as an idle-stop apply (the review of #494, round 2)
        started = self.server.clock()
        every = command.selection is not None and command.selection.all
        if every and fleet.slugs and not any(fleet.present(s) for s in fleet.slugs):
            self.reading.discard(bot.where)  # every row retired: nothing to read (#565)
            await bot.say(m, f"Every row is retired: nothing to read.\n\n{left_out(fleet)}")
            return
        try:
            async with self.reads:
                action = build(command, fleet, layout=self.server.paths.layout)
                text = await self.runner.read(action, fleet)
        except (ActionError, Unavailable) as exc:
            await bot.say(m, f"❌ {exc}")
            return
        except Exception as exc:  # its answer is the only report: the task runs alone
            LOG.warning("!status failed (%s)", type(exc).__name__, exc_info=exc)
            await bot.say(m, f"❌ `!status` failed ({type(exc).__name__}): nothing was changed")
            return
        finally:
            self.reading.discard(bot.where)
        state, now = self.server.paths.state, self.server.clock()
        found = _status_json(text or "")
        if found is not None:  # for the console: most VMs run idle stop, never swept (§4.1)
            try:
                await asyncio.to_thread(note_status, state, bot.where, found[0], started)
            except OSError as exc:
                LOG.warning("!status not kept for the console (%s)", exc.strerror)
        try:  # §11.2; a line it can't make is left out, never the answer (review of #494)
            cost = await asyncio.to_thread(cost_line, state, bot.where, now)
        except Exception as exc:
            LOG.warning("!status: no cost line (%s)", type(exc).__name__)
            cost = None
        paths = self.server.paths
        config = f"{paths.layout.waker}/config.toml"
        try:  # the waker down (#717): a line it can't make is left out too
            down = await asyncio.to_thread(status_line, paths.waker_status, config, bot.where, now)
        except Exception as exc:
            LOG.warning("!status: no waker line (%s)", type(exc).__name__)
            down = None
        under = [line for line in (cost, down, left_out(fleet) if every else "") if line]
        # The answer is fitted with room for the lines under it and the footer: a message over
        # Zulip's limit is cut at its end, which would take the footer (#767)
        limit = bot.client.limits.get("max_message_length", 10000)
        limit -= sum(len(line) + 2 for line in under) + FOOTER_ROOM
        reply = status_reply(text or "(no output)", self.server.scrubber, limit=limit)
        shown = "\n\n".join([reply.text, *under])
        if reply.full is not None:
            link = await self._attach(bot, "status.txt", reply.full)
            whole = f"The whole status: {link}." if link else "Its attachment failed."
            shown = shown.replace(ATTACH, whole)
        head = await self.server.audit(
            "read",
            workshop=bot.where,
            bot=bot.user_id,
            verb=command.verb,
            requester={"id": m.sender_id, "name": m.sender_name},
            args=list(command.tokens()),
        )
        await bot.say(m, f"{shown}\n{footer(head)}")

    async def sign_in(self, bot: AdminBot, m: Incoming, command: Command) -> None:
        """`!console`: a one-time sign-in link (§4.4), only ever in a one-to-one DM to the
        sender (#323), whether asked in the channel or by DM."""
        if self.console is None:
            await bot.say(m, "❌ the console is not set up on this server yet")
            return
        try:
            name = shown_name(m.sender_name)
            link = await asyncio.to_thread(self.console.issue, m.sender_id, name, bot.where,
                                           bot.user_id)  # fmt: skip
        except OSError as exc:
            LOG.warning("no sign-in link (%s)", exc.strerror)
            await bot.say(m, "❌ the sign-in link could not be made: nothing was sent")
            return
        head = await self.server.audit(
            "read",
            workshop=bot.where,
            bot=bot.user_id,
            verb=command.verb,
            requester={"id": m.sender_id, "name": m.sender_name},
            args=[],
        )
        sent = await bot.send(
            type="private",
            to=[m.sender_id],
            content=f"Your console sign-in link, for one use within 5 minutes: {link}",
        )
        if not m.dm:
            done = "sent you a sign-in link by DM" if sent else "❌ the DM could not be sent"
            await bot.say(m, f"{done}\n{footer(head)}")

    # ---------------------------------------------------------------------- helpers

    @staticmethod
    def _fields(plan: Plan) -> dict[str, Any]:
        return {"plan": plan.name, "workshop": plan.workshop, "bot": plan.bot}

    def _where(self, plan_id: int) -> tuple[int, str] | None:
        assert self.server.store is not None
        row = self.server.store.db.execute(
            "SELECT stream, topic FROM plan_topics WHERE plan = ?", (plan_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def _bot(self, plan: Plan) -> AdminBot | None:
        return next((b for b in self.server.bots if b.user_id == plan.bot), None)

    async def _say(
        self, plan: Plan, text: str, head: Any, *, where: tuple[int, str] | None = None
    ) -> None:
        """`text` in the plan's topic (`where`, when the store may lack it), from the plan's own
        bot, with the chain head when there is one."""
        where, bot = self._where(plan.id) or where, self._bot(plan)
        if where is not None and bot is not None:
            await bot.post(where[0], where[1], f"{text}\n{footer(head)}" if head else text)
