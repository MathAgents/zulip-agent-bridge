"""The runner: a plan's scripts, run (docs/ADMIN.md §2.3, §2.4, §2.7, §2.8; #342, PR 10).

`Runner` is `planning.Runner`. It runs in the bot's process, through the bot's plan store, audit
log and scrubber (`AdminServer`).

- **Processes** (`Runner.run`). Each step's argument list as `actions.build` made it: no shell
  (CONTRIBUTING.md, rule 3), the release venv's Python with `-E -s -B -u` (§2.3: not `-I`, since
  the scripts import their siblings). Stdin is /dev/null. The environment is built, never
  inherited: `PATH`, `HOME` (the state directory, whose `.ssh` holds the fleet key), `LANG`, the
  plan's own `TMPDIR`, and `GH_TOKEN` only for a step that names it. No AWS key (the role comes
  through IMDS), and no agent's credential (CONTRIBUTING.md, rule 2). Each run leads its own
  process group; when its leader ends, the rest of the group is killed. A process that leaves the
  group (`setsid`, as a daemonizing ssh master would; no script of the release does) is beyond
  `killpg`: the run waits `DRAIN` seconds for the output it holds, and the unit's cgroup ends it
  when the bot stops (`KillMode`, with the runner's second part).
- **Time** (§2.4). One deadline by verb (`TIMEOUTS`) for a plan's dry runs and status read, and
  for `!status`; the apply gets the same. At the deadline the group gets SIGTERM, then SIGKILL
  after `GRACE`, and the output says so. When the bot stops (a cancelled task), the same within
  `STOP_GRACE`: nothing of the group is left running. The waits are `asyncio.timeout`, never
  `asyncio.wait_for`: on Python 3.11 that returns the exit code for a stop that comes as the
  process ends (gh-86296), and an apply would go on to its next step (#491).
- **Output** (§2.7). Stdout and stderr, merged in order. A `Capture` keeps the first `KEEP`
  characters, cut at a line: the rest is counted, never kept or shown. So everything kept is
  scrubbed whole, in one call, before anything cuts it, and a secret across a later cut shows
  neither its head nor its tail. Bytes that aren't UTF-8 decode to lone surrogates, which the
  scrubber deletes like any invisible character, never to a visible U+FFFD that would split a
  secret. A line that a kill cut short (the deadline's, a stop's, or the SIGKILL of what the
  leader left) is dropped: it could hold a secret's head alone. What `prepare` and `read`
  return is already scrubbed, with the note on a cut after it, where a key block cut at the end
  can't take the note in. Each plan's scrubbed dry run is saved as `runs/P-<n>/dry-run.txt`.
- **Plans** (§2.4 steps 2 and 3, `prepare`). Every dry run, then the status read, in the plan's
  fresh TMPDIR (`tmp/P-<n>`, removed after); a failing dry run refuses the plan, and one that
  says there is nothing to change (`Unchanged`, #708) offers none. The status read is captured
  apart, for the plan's table, and never hashed. The hash covers
  the verb's live state: each target's VMs (`vms`, `provider.targets`), or the dry runs' output,
  which `actions.normalize_dry_run` makes deterministic. Every character hashed is shown: a
  hashed dry run is refused once it passes `KEEP // 2` characters, or once the output shown is
  cut, so the plan's attachment, one scrubber call with the plan's own lines, holds all of it.
- **Apply** (§2.4 step 5, `apply`). The workshop's apply lock is taken before the first await, a
  second line behind the planner's busy mark. The hash is recomputed from the fleet as it is now
  (the VMs, or the dry run again), and `begin_apply` makes the plan APPLYING, or else EXPIRED,
  or STALE, said with what changed since the plan: the release, the commands, an input file, a
  target's VMs (`alice: i-1 → i-9`), the dry run. Then `apply_start`; the steps in order up to
  the first failure, with one progress message edited at most every `PROGRESS` seconds, only
  when it changed, and at most 19 times (`AdminBot.edit`), from everything so far, scrubbed
  whole and only its whole lines; and `apply_end` (`fleet.ApplyEnd`) on every way out: done,
  failed, timed out, an error, or cancelled. Both name the plan's `targets`, which the ledger
  reads for each row's end of the workshop (stage 3, #367). The
  result shows the last lines of the scrubbed output and attaches the whole when that cuts it;
  `runs/P-<n>/apply.txt` keeps it. Its post is tried `POSTS` times while each try surely
  failed; the outcome is in the audit log, and the result says so when `apply_end` could not be
  written. §2.7's reloads come
  before each scrub that something is posted from: each progress edit, after its snapshot of the
  output, reloads the secrets if `out/` changed (`watch_secrets`: a key the snapshot holds was
  written to its file before it was printed), and `_end` reloads them all before the output is
  saved and the result posted. No step's output is posted in between: a progress edit shows the
  last line only, which is the running step's. After the result, still under the lock, the
  encrypted backup (`backup.Backups.after`, §1.7) if the apply changed a workshop's directory,
  or the last backup failed; its failure is said in the topic and changes nothing here. A
  cancelled apply stays APPLYING: the next start marks it INTERRUPTED (`PlanStore.recover`)
  and says so, and it never runs again on its own.
- **Rows** (`!retire`, #559). A plan whose steps name their row (`Step.row`) runs them in
  order, but a failure stops its row alone (`Rows`): the row's later steps are `not run`, and
  the other rows go on. A tolerated step's failure stops nothing. A step that needs the row's
  VM, for a row with none at apply (`Live.vms`, from the recheck), is `skipped: no VM` when
  `gone` (the plan says `unsynced`), and fails the row otherwise. Only the deadline, or a step
  of no row that fails, stops the rest. `apply_end` names each row's cells and whether it was
  retired (`fleet.ApplyEnd.rows`), for the ledger, and each step's row; the result shows them
  as a table, first, then what each stopped row's VM is left as and how to go on. The deadline
  is the action's (`Action.timeout`), which grows with its rows.
- **The VM synced is the VM torn down** (`!retire`; review of #564). Its scripts reach a row's
  VM by the roster's address or by its `participant` tag in any case (`Live.vms`), while its
  protection and teardown act on the VMs tagged with the slug exactly (`Live.tagged`). At plan
  time and at the apply's recheck, a row for which the two differ is refused, with what to fix,
  and so is a row with no VM found, unless the plan says `unsynced`; both are hashed. The plan
  shows each row's VMs as found.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zab.admin.actions import (
    DRY_RUN,
    IDLE_STOP_DEFAULT,
    NOTHING,
    RETIRE,
    TARGETS,
    VERBS,
    Action,
    Argv,
    Ending,
    Fleet,
    Live,
    Step,
    digests,
    target_material,
)
from zab.admin.fleet import ApplyEnd, RowEnd
from zab.admin.planning import NOTE_CHARS, Job, Prepared
from zab.admin.plans import APPLYING, APPROVED, EXPIRED, STALE, Plan, fingerprint
from zab.admin.reads import footer, short
from zab.admin.scrub import (
    ATTACHMENT_BYTES,
    INLINE_CHARS,
    INLINE_LINES,
    MAX_SCRUB_CHARS,
    fence,
    span,
)

if TYPE_CHECKING:
    from zab.admin.bot import AdminBot, AdminServer

LOG = logging.getLogger("zab.admin.runner")
#: §2.4: how long a verb's apply may take, in seconds; its dry runs and status read too.
#: `teardown`: up to 300 s of retries for a VM refused as protected (#378), then EC2's waiter
TIMEOUTS = {
    "launch": 3600.0,
    "rollout": 1800.0,
    "final-sync": 1800.0,
    "export": 1800.0,
    "teardown": 1800.0,
}
TIMEOUT = 900.0  # every other verb, `!status` included
GRACE = 30.0  # at a timeout: SIGTERM, then SIGKILL this much later (§2.4)
STOP_GRACE = 5.0  # the same when the bot stops, well within systemd's TimeoutStopSec
DRAIN = 5.0  # seconds to read what is left in the pipe once the group is gone
PROGRESS = 60.0  # one progress edit at most this often, 19 in all (`AdminBot.edit`, D41)
POSTS, BACKOFF = 3, 2.0  # a result is posted in at most this many tries, 2 s, then 4 s apart
#: The output characters kept per run: one scrubber call scrubs them all (`MAX_SCRUB_CHARS`).
KEEP = MAX_SCRUB_CHARS - 4096
_CHUNK = 65536
_CUT = "[output cut here: {} more characters, neither kept nor shown]\n"
_KILLED = "[its last line, cut short by the kill, is not shown]\n"
_MATERIAL = """
CREATE TABLE IF NOT EXISTS plan_material (
    plan INTEGER PRIMARY KEY REFERENCES plans (id),
    material TEXT NOT NULL  -- JSON: what the plan's hash covered at plan time, for a STALE's why
);
"""
#: Each of a plan's targets → the IDs of the VMs its scripts could act on (`provider.targets`),
#: comma-joined; run in a thread.
Vms = Callable[[Fleet, tuple[str, ...]], Mapping[str, str]]


class RunnerError(Exception):
    """The runner can't run this; one line of its own words, never output."""


class Unchanged(RunnerError):
    """A plan's dry run says there is nothing to change (`NOTHING`, `Verb.nothing`; #627, #708):
    no plan is offered, and the planner answers it as a success, not a refusal."""


#: Its words: the plan's note, and the `unchanged` record's reason.
NOTHING_TO_CHANGE = "nothing to change, as its dry run says"


def timeout(verb: str) -> float:
    return TIMEOUTS.get(verb, TIMEOUT)


def says_nothing(action: Action) -> bool:
    """Whether a dry run's `NOTHING` exit means the plan has nothing to change: a verb whose
    script says so (`Verb.nothing`), of one step, so that no other step's dry run is left
    unread, and whose dry run passes the verb's `nothing_flag`, if it has one (`!provision
    repos`: `--invites-only` alone). Anything else that exits `NOTHING` is a failed dry run."""
    verb = VERBS.get(action.verb)
    if verb is None or not verb.nothing or len(action.steps) != 1:
        return False
    return not verb.nothing_flag or verb.nothing_flag in (action.steps[0].dry_run or ())


def limit(action: Action) -> float:
    """The apply's deadline: the action's own (`!retire`'s, by its rows), or the verb's."""
    return action.timeout or timeout(action.verb)


def script(argv: Argv) -> str:
    """The script that `argv` runs, as `aws/power.py`."""
    path = next((word for word in argv if word.endswith(".py")), argv[0])
    return "/".join(Path(path).parts[-2:])


def took(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min {s % 60} s" if s < 3600 else f"{s // 3600} h {s % 3600 // 60} min"


class Capture:
    """Output as it arrives: its first `limit` characters, cut at a line, and a count of the
    rest. A cut between lines splits no secret in scope (§2.7 item 5), and nothing past it is
    ever kept, so it can't be shown."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = KEEP if limit is None else limit
        self.parts: list[str] = []
        self.size = self.dropped = 0
        self.full = False

    def feed(self, text: str) -> None:
        if self.full:
            self.dropped += len(text)
        elif self.size + len(text) <= self.limit:
            self.parts.append(text)
            self.size += len(text)
        else:
            whole = "".join(self.parts) + text
            cut = whole.rfind("\n", 0, self.limit) + 1
            self.parts, self.size, self.full = [whole[:cut]], cut, True
            self.dropped += len(whole) - cut

    def end_line(self, *, killed: bool) -> None:
        """End the last line before what follows: kept, or dropped when a kill may have cut it
        inside a secret, whose head alone no pattern matches."""
        kept = "".join(self.parts)
        if self.full or not kept or kept.endswith("\n"):
            return
        self.parts = [kept[: kept.rfind("\n") + 1] if killed else kept]
        self.size = len(self.parts[0])
        self.feed(_KILLED if killed else "\n")

    def kept(self, *, whole_lines: bool = False) -> str:
        """What is kept. `whole_lines`: without a last line still arriving, which could hold a
        secret's head without its tail."""
        kept = "".join(self.parts)
        return kept[: kept.rfind("\n") + 1] if whole_lines else kept

    def note(self) -> str:
        """The runner's own line saying what was dropped, if anything was. It goes after the
        scrubbed text, where a key block cut at the end can't take it in."""
        return _CUT.format(self.dropped) if self.full else ""

    def text(self) -> str:
        return self.kept() + self.note()


@dataclass(slots=True)
class Ran:
    """One run of a script: its exit code (negative: the signal that ended it); `ended` is
    "timeout" or "stopped" when the runner ended it, `killed` when that took SIGKILL for any of
    its group; `cut`, when the runner's own kill, or a drain cut short, may have cut its last
    line."""

    script: str
    code: int | None = None
    ended: str = ""
    killed: bool = False
    cut: bool = False
    row: str = ""  # the row it ran for (`Step.row`)

    @property
    def label(self) -> str:
        """The script, and the row it ran for: `aws/teardown.py for bob`."""
        return f"{self.script} for {self.row}" if self.row else self.script

    @property
    def ok(self) -> bool:
        """Exited 0 on its own: a script that exits 0 on the SIGTERM of a timeout still timed
        out (Codex on #354)."""
        return self.code == 0 and not self.ended

    def __str__(self) -> str:
        how, name = (", then SIGKILL" if self.killed else ""), self.label
        if self.code is None:
            return f"{name} could not be run"
        if self.ended == "timeout":
            return f"{name} timed out: SIGTERM{how}; exit {self.code}"
        if self.ended == "stopped":
            return f"{name} was stopped with the bot: SIGTERM{how}; exit {self.code}"
        return f"{name} exited {self.code}"

    @property
    def cell(self) -> str:
        """Its row's cell (`Rows`): `ok`, or why it failed, in a few words; a script's own exit
        that says why by itself (`EXITS`) is named."""
        if self.ok:
            return "ok"
        if self.code is None:
            return "failed: not run"
        if not self.ended and (why := EXITS.get((self.script, self.code))):
            return f"failed: {why}"
        return f"failed: {'timed out' if self.ended == 'timeout' else f'exit {self.code}'}"


class Rows:
    """Each row's cells as a per-row apply goes (module doc), in the action's order of rows and
    stages; `vms`, each row's VMs at apply (`Live.vms`)."""

    def __init__(self, action: Action, vms: Mapping[str, str]) -> None:
        mine = [step for step in action.applied if step.row]
        self.cells: dict[str, dict[str, str]] = {step.row: {} for step in mine}
        self.stages = list(dict.fromkeys(step.stage for step in mine))
        self.order = {row: [step.stage for step in mine if step.row == row] for row in self.cells}
        self.stopped: set[str] = set()
        self.seconds: dict[str, float] = {}  # each row's steps' time, in seconds
        self.vms = vms

    def skip(self, step: Step) -> bool:
        """Whether `step` doesn't run, its cell set: its row stopped, or no VM for it."""
        if not step.row:
            return False
        cells = self.cells[step.row]
        if step.row in self.stopped:
            cells[step.stage] = "not run"
            return True
        if step.vm and not self.vms.get(step.row):
            cells[step.stage] = "skipped: no VM" if step.gone else "failed: no VM"
            if not step.gone:
                self.stopped.add(step.row)
            return True
        return False

    def ran(self, step: Step, ran: Ran, seconds: float = 0.0) -> None:
        self.cells[step.row][step.stage] = ran.cell
        self.seconds[step.row] = self.seconds.get(step.row, 0.0) + seconds
        if not ran.ok and not step.tolerated:
            self.stopped.add(step.row)

    def retired(self, row: str) -> bool:
        """It ran to its last step, which exited 0: every step that could stop it passed."""
        return row not in self.stopped and self.cells[row].get(self.order[row][-1]) == "ok"

    def unsaved(self, row: str) -> list[str]:
        """The steps that save its work and failed, tolerated (`unsynced`): `final-sync`, …"""
        saving = ("start", "idle-stop", "uninstall", "final-sync", "export")
        return [s for s in saving if self.cells[row].get(s, "").startswith("failed")]

    def ends(self) -> dict[str, RowEnd]:
        """For `apply_end`: a step never reached (the deadline, the bot stopping) is `not run`."""
        return {
            row: RowEnd(tuple((s, cells.get(s, "not run")) for s in self.order[row]),
                        self.retired(row), round(self.seconds.get(row, 0.0)))
            for row, cells in self.cells.items()
        }  # fmt: skip

    def table(self) -> str:
        """`participant`, a column per stage, `time` and `retired`, as the scripts' tables are
        laid out (`admin_common.format_table`: two spaces between columns, a rule of dashes)."""
        ends = self.ends()
        head = ["participant", *self.stages, "time", "retired"]
        body = [[row, *(dict(end.cells).get(s, "") for s in self.stages), took(end.seconds),
                 "yes" if end.retired else "no"] for row, end in ends.items()]  # fmt: skip
        widths = [max(len(r[i]) for r in [head, *body]) for i in range(len(head))]
        lines = [head, ["-" * w for w in widths], *body]
        return "\n".join(
            "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in lines
        )


def _signal(group: int, sig: int) -> bool:
    """`sig` to every process left in the group: whether it reached any; none left is fine."""
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        return False
    except OSError as exc:  # never: the group is the bot's own uid's
        LOG.warning("signal %d to group %d failed (%s)", sig, group, type(exc).__name__)
        return False
    return True


async def _stop(proc: asyncio.subprocess.Process, grace: float, ran: Ran) -> None:
    """SIGTERM to the group, and SIGKILL if its leader is still there after `grace`."""
    _signal(proc.pid, signal.SIGTERM)
    try:
        async with asyncio.timeout(grace):
            await proc.wait()
    except TimeoutError:
        ran.killed = True
        _signal(proc.pid, signal.SIGKILL)
        await proc.wait()


async def _pump(reader: asyncio.StreamReader, out: tuple[Capture, ...]) -> None:
    # not "replace": U+FFFD is visible, and would split a secret the scrubber then can't join
    decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
    while chunk := await reader.read(_CHUNK):
        text = decoder.decode(chunk)
        for capture in out:
            capture.feed(text)
    text = decoder.decode(b"", final=True)
    for capture in out:
        capture.feed(text)


def _tail(text: str) -> tuple[str, bool]:
    """The last lines of scrubbed `text` that fit in a post (§2.7 item 5), fenced, and whether
    that leaves anything out. Only ever applied to text already scrubbed whole."""
    whole = text.removesuffix("\n") or "(no output)"
    body = "\n".join(whole.split("\n")[-INLINE_LINES:])
    while len(fence(body)) > INLINE_CHARS:
        cut = body.find("\n") + 1
        body = body[cut:] if cut else body[len(body) // 2 :]
    return fence(body), body != whole


def _fields(plan: Plan) -> dict[str, Any]:
    return {"plan": plan.name, "workshop": plan.workshop, "bot": plan.bot}


def _step_record(ran: Ran) -> dict[str, Any]:
    """A step as `apply_end` records it, with its row when it has one (review of #564)."""
    out: dict[str, Any] = {"script": ran.script, "exit": ran.code, "ended": ran.ended}
    return out | ({"row": ran.row} if ran.row else {})


#: Kept free in a result's message for its footer and the attachment's line (#559).
RESULT_ROOM = 600
#: A row's cell for a script's exit that says why by itself: final_sync.py's `GONE`, the row's
#: repository deleted or transferred by its participant (#714).
GONE_CELL = "repository gone"
EXITS = {("admin/final_sync.py", 4): GONE_CELL}


def vms_found(
    action: Action, fleet: Fleet, vms: Mapping[str, str], tagged: Mapping[str, str],
    addressed: Mapping[str, str] | None = None,
) -> tuple[str, str]:  # fmt: skip
    """`!retire`'s rows and their VMs as found (module doc): the lines the plan shows, and why
    it is refused, or "". A row is refused when the VMs its scripts reach (`vms`: by its
    address, or a `participant` tag in any case) are not exactly those tagged with its slug
    (`tagged`), which its protection and teardown end; when it has more than one, since its
    steps sync the one at its address and its teardown would end them all (Codex on #564); or
    when none is found, unless the plan says `unsynced` (review of #564). So a row's teardown
    ends one VM, the one its steps reach; a VM away from the row's address can't be reached,
    and its row stops before its teardown, unless `unsynced` says to end it all the same; the
    plan flags it, from `addressed` (review of #564)."""
    region, unsynced = fleet.workshop.aws.region, "unsynced" in action.args
    lines, wrong, none = [f"── the rows' VMs, as the bot found them in {region}"], [], []
    many = []
    for row in action.targets:
        found, exact = vms.get(row, ""), tagged.get(row, "")
        if not found:
            none.append(row)
            then = ": the steps that need one are skipped (`unsynced`)" if unsynced else ""
            lines.append(f"{row}: no VM found{then}")
        elif found != exact:
            wrong.append(row)
            lines.append(f"{row}: {found}, but tagged participant={row}: {exact or 'none'}")
        elif "," in found:
            many.append(row)
            lines.append(f"{row}: {found}: more than one VM")
        else:
            ip = next((r.ip for r in fleet.rows if r.slug == row), "")
            away = addressed is not None and ip and addressed.get(row) != found
            lines.append(f"{row}: {found}" + (
                f", not at its roster address {ip}: its steps can't reach it" if away else ""
            ))  # fmt: skip
    why = ""
    if many and not wrong:
        why = (
            f"{', '.join(many)}: more than one VM tagged with its slug: its steps sync the one at "
            "its roster address, and its teardown would end them all. End the others by hand, or "
            "tag them apart"
        )
    if wrong:
        why = (
            f"{', '.join(wrong)}: its steps reach VMs (by its address, or a tag in another case) "
            "other than those tagged participant=<slug>, which its teardown ends: tag the VM "
            "exactly (a spare handed over keeps participant=zab-spare-N), or end it by hand"
        )
    elif none and not unsynced:
        why = (
            f"no VM found in {region} for {', '.join(none)}: add `unsynced` to retire "
            f"{'it' if len(none) == 1 else 'them'} (the bots go, and the row is marked retired), "
            "in a plan of its own; or check the roster's ip and region"
        )
    return "\n".join(lines) + "\n", why


def stopped_row(fleet: Fleet | None, row: str, cells: Mapping[str, str]) -> str:
    """A stopped row's line under the result's table (review of #564): what its VM is left
    as, how to send it again, and how to put it back in service instead."""
    stage = next(s for s, c in cells.items() if c.startswith("failed"))
    if cells[stage] == f"failed: {GONE_CELL}":  # #714: final_sync.py's line names it too
        org = fleet.workshop.github_org if fleet is not None else "the organization"
        return (
            f"- {row} stopped at {stage}: `{org}/{row}` no longer exists here: the participant "
            f"deleted or transferred it, so its VM's work can't be pushed there. `!retire {row} "
            "unsynced` tears it down all the same, and loses the work that is only on its VM: "
            "what wasn't pushed before the repository went."
        )
    if cells.get("teardown") == "ok" or stage in ("teardown", "bots") or "no VM" in cells[stage]:
        return (f"- {row} stopped at {stage}: its VM may be gone. Send `!retire {row}` again; "
                f"if its plan finds no VM, `!retire {row} unsynced`.")  # fmt: skip
    left = [what for stage_, what in (("idle-stop", "idle stop off"), ("waker", "the waker off"),
                                     ("uninstall", "its bridge uninstalled"))
            if cells.get(stage_) == "ok"]  # fmt: skip
    state = f"its VM runs, with {', '.join(left)}" if left else "its VM is as it was"
    lines = [
        f"- {row} stopped at {stage} (`{cells[stage]}`): {state}. Fix the cause, then send "
        f"`!retire {row}` again, or `!retire {row} unsynced` to tear it down with its work."
    ]
    back = _put_back(fleet, row, cells)
    if back:
        lines.append(f"  To put it back in service instead: {back}.")
    return "\n".join(lines)


def _put_back(fleet: Fleet | None, row: str, cells: Mapping[str, str]) -> str:
    """The commands that undo a stopped row's idle stop off, waker off and uninstall, in the
    order of ORGANIZERS §3.5 (the waker before idle stop), from what the ledger has recorded of
    the workshop (review of #564): a `!rollout` replays the fleet's settings whole, as
    `!adopt-spare` does (`Fleet.settings`, `!rollout`'s own words), at its last release; the
    idle-stop minutes are the ledger's, or the rollout's default (#704), named all the same:
    the row's idle stop was turned off on purpose, which the default leaves off; the waker is
    put back for the row if the fleet's was on, else if it was opted in."""
    settings = fleet.settings if fleet is not None and fleet.settings else ()
    known = next((w.split("=", 1)[1] for w in settings if w.startswith("idle-stop=")), None)
    minutes = known or str(IDLE_STOP_DEFAULT)
    release = fleet.release if fleet is not None and fleet.release else "<release>"
    out = []
    if cells.get("waker") == "ok":
        whole = fleet is not None and fleet.waker
        out.append(f"`!waker on {row}`" + " (if it was opted in)" * (not whole))
    idle = cells.get("idle-stop") == "ok" and minutes != "0"
    if cells.get("uninstall") == "ok":
        words = [w for w in settings if not w.startswith("idle-stop=")]
        words += [f"idle-stop={minutes}"] * idle
        out.append(f"`!rollout {' '.join([release, row, *words])}`")
    elif idle:
        out.append(f"`!idle-stop {minutes} {row}`")
    return ", then ".join(out)


class Runner:
    """`planning.Runner` (module doc)."""

    def __init__(
        self, server: AdminServer, *, vms: Vms | None = None, tagged: Vms | None = None,
        addressed: Vms | None = None,
    ) -> None:  # fmt: skip
        """`vms` lists a workshop's VMs for the verbs whose hash covers their targets (§2.4);
        without it those verbs are refused. `tagged`, the VMs tagged with each name exactly,
        for `!retire` (#559), which is refused without it; `addressed`, those at each row's
        roster address, which its plan flags when they differ (shown, not hashed)."""
        self.server, self.vms, self.tagged = server, vms, tagged
        self.addressed = addressed
        self.applying: dict[str, Plan] = {}  # the apply lock: workshop → its plan applying

    # -------------------------------------------------------------------------- processes

    def environment(self, tmp: str, names: tuple[str, ...] = ()) -> dict[str, str]:
        """§2.3's environment, built and never inherited; `names` from `Step.env`."""
        env = {"PATH": "/usr/bin:/bin", "HOME": self.server.paths.state, "LANG": "C.UTF-8"}
        env["TMPDIR"] = tmp
        for name in names:
            if name != "GH_TOKEN":
                raise RunnerError(f"no step may have {name}")
            env[name] = self._token()
        return env

    def _token(self) -> str:
        try:
            token = Path(self.server.paths.github_token).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            token = ""
        if not token or any(c.isspace() for c in token):
            raise RunnerError("the GitHub token can't be read")
        return token

    async def run(
        self, argv: Argv, env: dict[str, str], cwd: str, deadline: float, out: tuple[Capture, ...],
        ran: Ran,
    ) -> Ran:  # fmt: skip
        """`argv` as the leader of a new process group, its merged output fed to `out`, until
        it exits or `deadline` (time.monotonic) passes. Every way out kills what is left of
        the group; a cancellation stops it first (`STOP_GRACE`), and is raised again."""
        loop = asyncio.get_running_loop()
        read_fd, write_fd = os.pipe()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=write_fd,
                stderr=asyncio.subprocess.STDOUT, env=env, cwd=cwd, start_new_session=True,
            )  # fmt: skip
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        pipe = os.fdopen(read_fd, "rb", buffering=0)
        transport: asyncio.BaseTransport | None = None
        pump: asyncio.Task[None] | None = None
        try:
            reader = asyncio.StreamReader()
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), pipe
            )
            pump = asyncio.create_task(_pump(reader, out))
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                await proc.wait()
        except TimeoutError:
            ran.ended = "timeout"
            await _stop(proc, GRACE, ran)
        except asyncio.CancelledError:
            ran.ended = "stopped"
            await _stop(proc, STOP_GRACE, ran)
            raise
        finally:
            if _signal(proc.pid, signal.SIGKILL):  # what is left of the group, or all of it
                ran.cut = True  # a process of the group may have been mid-line
                ran.killed = ran.killed or bool(ran.ended)
            try:
                if proc.returncode is None:
                    await proc.wait()
                ran.code = proc.returncode
                if pump is not None and not (await asyncio.wait([pump], timeout=DRAIN))[0]:
                    ran.cut = True  # a process outside the group holds the pipe
                    LOG.warning("%s: output left unread after its group ended", ran.script)
            finally:
                if pump is not None:
                    pump.cancel()
                    if pump.done() and not pump.cancelled() and pump.exception():
                        LOG.warning("%s: its output could not be read", ran.script)
                (transport or pipe).close()
        return ran

    async def _step(
        self, argv: Argv, env: dict[str, str], tmp: str, deadline: float, *out: Capture,
        ran: Ran | None = None, framed: bool = True,
    ) -> Ran:  # fmt: skip
        """One run, between a line naming it and a line with how it ended, in `out[0]` (the
        output shown); `framed=False` says only a failure."""
        ran = ran or Ran(script(argv))
        if framed:
            out[0].feed(f"── {ran.label}\n")
        try:
            await self.run(argv, env, tmp, deadline, out, ran)
        finally:
            for capture in out:
                capture.end_line(killed=bool(ran.ended) or ran.cut)
            if framed or not ran.ok:
                out[0].feed(f"── {ran}\n")
        return ran

    def _fresh(self, name: str | None) -> str:
        """A new, empty TMPDIR for a plan (§2.3), 0700, where an earlier run's goes; or, with
        no name, for a `!status`."""
        root = Path(self.server.paths.tmp)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if name is None:
            return tempfile.mkdtemp(prefix="status-", dir=root)
        path = root / name
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(mode=0o700)
        return str(path)

    def _write(self, plan: Plan, name: str, scrubbed: str) -> tuple[str | None, str | None]:
        """`runs/P-<n>/<name>`, new, 0600 (§1.3): its path as recorded, and its SHA-256."""
        path = Path(self.server.paths.layout.runs) / plan.name / name
        data = scrubbed.encode("utf-8")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            with os.fdopen(os.open(path, flags, 0o600), "wb") as f:
                f.write(data)
        except OSError as exc:
            LOG.warning("%s: %s not saved (%s)", plan.name, name, type(exc).__name__)
            return None, None
        return f"runs/{plan.name}/{name}", hashlib.sha256(data).hexdigest()

    # ----------------------------------------------------------------------- the protocol

    async def prepare(self, plan: Plan, action: Action, fleet: Fleet | None) -> Prepared:
        """§2.4 step 2: every dry run, then the status read, and what the hash covers. The
        status read is kept apart, for the plan's table, and saved after the dry runs."""
        shown, read, why, unchanged = Capture(), Capture(), "", False
        try:
            live = await self._live(plan, action, fleet, shown, planning=True, read=read)
        except RunnerError as exc:
            live, why, unchanged = Live(), str(exc), isinstance(exc, Unchanged)
        scrubbed = await asyncio.to_thread(self.server.scrub, shown.kept()) + shown.note()
        status = ""
        if read.kept():  # its JSON with its escapes undone: they hide what the strip deletes
            json_text = self.server.scrubber.json_text
            status = await asyncio.to_thread(json_text, read.kept()) + read.note()
        name = script(action.status) if action.status else ""
        saved = scrubbed + (f"── {name}, the status read\n{status}" if status else "")
        await asyncio.to_thread(self._write, plan, "dry-run.txt", saved)
        if not why:
            self._keep(plan, self._material(action, live))
        # the planner's second scrub keeps the notes
        return Prepared(not why, scrubbed, live, why, status=status, unchanged=unchanged)

    def _material(self, action: Action, live: Live) -> dict[str, Any]:
        """What the plan's hash covers (`actions.plan_hash`), a VM per target and a teardown's
        rows' ends (`Action.ends`, #376) as they stand, the rest as digests, to say what changed
        when a plan goes STALE (§2.4 step 5)."""

        def digest(value: object) -> str:
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

        targets = target_material(action, live)
        out: dict[str, Any] = {"release": self.server.release, "inputs": digests(action)}
        out["steps"] = digest([[step.argv, step.dry_run, step.env] for step in action.steps])
        out["vms"] = dict(targets.get("vms", []))
        if "tagged" in targets:
            out["tagged"] = dict(targets["tagged"])
        out["ends"] = {row: list(flags) for row, *flags in action.ends}
        out |= {key: digest(targets[key]) for key in ("dry_run",) if key in targets}
        return out

    def _keep(self, plan: Plan, material: dict[str, Any]) -> None:
        store = self.server.store
        assert store is not None
        store.db.executescript(_MATERIAL)
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO plan_material (plan, material) VALUES (?, ?)",
                (plan.id, json.dumps(material)),
            )

    def _changes(self, plan: Plan, now: dict[str, Any]) -> list[str]:
        """What changed since the plan, as words (§2.4 step 5: "the bot posts the difference")."""
        store = self.server.store
        assert store is not None
        store.db.executescript(_MATERIAL)
        row = store.db.execute(
            "SELECT material FROM plan_material WHERE plan = ?", (plan.id,)
        ).fetchone()
        if row is None:
            return []
        try:  # a row it can't read (another format) never costs the stale record or its post
            changes = self._diff(json.loads(row[0]), now)
        except Exception as exc:
            LOG.warning("%s: what changed can't be read (%s)", plan.name, type(exc).__name__)
            return []
        if len(changes) > 20:  # a teardown's rows can be many: the cut is said (review of #502)
            changes = [*changes[:20], f"and {len(changes) - 20} more"]
        return changes

    @staticmethod
    def _diff(then: dict[str, Any], now: dict[str, Any]) -> list[str]:
        out = list[str]()
        if then["release"] != now["release"]:
            out.append(f"the server's release, {then['release']} → {now['release']}")
        if then["steps"] != now["steps"]:
            out.append("the commands")
        for path in sorted(set(then["inputs"]) | set(now["inputs"])):
            was, new = then["inputs"].get(path, ""), now["inputs"].get(path, "")
            if was != new:
                how = "appeared" if not was else "gone" if not new else "changed"
                out.append(f"{Path(path).name} {how}")
        for name in [*then["vms"], *(n for n in now["vms"] if n not in then["vms"])]:
            was, new = then["vms"].get(name), now["vms"].get(name)
            if was != new:
                out.append(f"{name}: {was or 'no VM'} → {new or 'gone'}")
        tagged = then.get("tagged", {}), now.get("tagged", {})
        for name in [*tagged[0], *(n for n in tagged[1] if n not in tagged[0])]:
            was, new = tagged[0].get(name), tagged[1].get(name)
            if was != new:
                out.append(f"{name} tagged participant={name}: {was or 'none'} → {new or 'none'}")
        if "ends" in then:  # a material kept before #376 has none: its rows aren't named
            ends = then["ends"], now["ends"]
            for name in [*ends[0], *(n for n in ends[1] if n not in ends[0])]:
                was, new = (end.get(name) for end in ends)
                if was != new:
                    out.append(f"{name}: {Ending.said(was)} → {Ending.said(new)}")
        if then.get("dry_run") != now.get("dry_run"):
            out.append("the dry run")
        return out

    async def _live(
        self, plan: Plan, action: Action, fleet: Fleet | None, shown: Capture, *, planning: bool,
        read: Capture | None = None,
    ) -> Live:  # fmt: skip
        """The live state the plan's hash covers (§2.4): the VMs, or the dry runs' own output,
        run in a fresh TMPDIR. `planning`: every dry run, and the status read into `read`, to
        show, never to hash; at apply, only what the hash needs."""
        listing = action.hashed in (TARGETS, RETIRE)
        if (listing and self.vms is None) or (action.hashed == RETIRE and self.tagged is None):
            raise RunnerError("this server can't list its VMs yet: nothing runs")
        tmp = self._fresh(plan.name)
        deadline = time.monotonic() + timeout(action.verb)
        hashed = Capture(KEEP // 2)  # with the plan's own lines, one scrubber call holds it all
        try:
            for step in action.steps:
                if step.dry_run is not None and (planning or action.hashed in (DRY_RUN, RETIRE)):
                    env = self.environment(tmp, step.env)
                    ran = await self._step(step.dry_run, env, tmp, deadline, shown, hashed)
                    if ran.code == NOTHING and not ran.ended and says_nothing(action):  # #708
                        raise Unchanged(NOTHING_TO_CHANGE)
                    if not ran.ok:
                        raise RunnerError(f"the dry run failed: {ran}")
            if action.hashed in (DRY_RUN, RETIRE) and (shown.full or hashed.full):  # all seen
                too_long = f"the dry run printed over {hashed.limit} characters: too long to hash"
                raise RunnerError(too_long)
            if planning and action.status is not None:  # its own exit said only on a failure
                out = read if read is not None else shown
                env = self.environment(tmp)
                await self._step(action.status, env, tmp, deadline, out, framed=read is None)
            vms: Mapping[str, str] = {}
            tagged: Mapping[str, str] = {}
            if action.hashed in (TARGETS, RETIRE) and self.vms is not None:
                if fleet is None:  # never: a server verb's hash is its dry run's
                    raise RunnerError("a plan of no workshop can't list its VMs")
                vms = await asyncio.to_thread(self.vms, fleet, action.targets)
            if action.hashed == RETIRE and self.tagged is not None and fleet is not None:
                tagged = await asyncio.to_thread(self.tagged, fleet, action.targets)
                at: Mapping[str, str] | None = None
                if planning and self.addressed is not None:
                    at = await asyncio.to_thread(self.addressed, fleet, action.targets)
                found = vms_found(action, fleet, vms, tagged, at)
                if planning:
                    shown.feed(found[0])
                if found[1]:
                    raise RunnerError(found[1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        dry = hashed.text() if action.hashed in (DRY_RUN, RETIRE) else ""
        return Live(vms=dict(vms), dry_run=dry, tmpdir=tmp, tagged=dict(tagged))

    async def read(self, action: Action, fleet: Fleet) -> str:
        """`!status` (§3, action 6): read only, no plan. The output scrubbed whole, with its
        JSON's escapes undone (`Scrubber.json_text`), then the note on a cut; the planner's
        second scrub before it cuts changes nothing."""
        tmp = self._fresh(None)
        out = Capture()
        deadline = time.monotonic() + timeout(action.verb)
        try:
            for step in action.steps:
                env = self.environment(tmp, step.env)
                await self._step(step.argv, env, tmp, deadline, out, framed=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return await asyncio.to_thread(self.server.scrubber.json_text, out.kept()) + out.note()

    def busy(self, workshop: str) -> Plan | None:
        """The workshop's plan that is applying: in this process, or as `state.db` has it."""
        plan = self.applying.get(workshop)
        store = self.server.store
        if plan is None and store is not None:
            row = store.db.execute(
                "SELECT id FROM plans WHERE workshop = ? AND state = ? ORDER BY id LIMIT 1",
                (workshop, APPLYING),
            ).fetchone()
            plan = store.get(row[0]) if row else None
        return plan

    def _blocking(self, workshop: str) -> Plan | None:
        """What keeps a plan of `workshop` from applying (§2.4 step 5): its workshop's apply, or
        a server plan's (workshop ""), which holds every workshop's lock; for a server plan, any
        apply at all."""
        if workshop:
            return self.busy(workshop) or self.busy("")
        plan = next(iter(self.applying.values()), None)
        store = self.server.store
        if plan is None and store is not None:
            row = store.db.execute(
                "SELECT id FROM plans WHERE state = ? ORDER BY id LIMIT 1", (APPLYING,)
            ).fetchone()
            plan = store.get(row[0]) if row else None
        return plan

    async def apply(self, job: Job) -> None:
        """§2.4 step 5, under the workshop's apply lock (module doc)."""
        plan, store = job.plan, self.server.store
        assert store is not None
        other = self._blocking(plan.workshop)
        if other is not None:  # a second line: the planner marks the workshop busy first
            plan = store.move(plan.id, (APPROVED,), STALE, f"{other.name} was applying")
            head = await self.server.audit("stale", **_fields(plan), reason=plan.note)
            await self._say(job, f"❌ {plan.name}: {plan.note}; nothing was done.", head)
            return
        self.applying[plan.workshop] = plan
        try:
            await self._apply(job)
        finally:
            del self.applying[plan.workshop]

    # ------------------------------------------------------------------------- the apply

    async def _apply(self, job: Job) -> None:
        server, plan, action = self.server, job.plan, job.action
        store = server.store
        assert store is not None and server.approvals is not None
        try:  # the hash again, from the fleet as it is now
            live = await self._live(plan, action, job.fleet, Capture(), planning=False)
            current = fingerprint(action, release=server.release, live=live)
            now = self._material(action, live)
        except Exception as exc:
            why = str(exc) if isinstance(exc, RunnerError) else type(exc).__name__
            note = f"the recheck failed: {why}"[:NOTE_CHARS]
            plan = store.move(plan.id, (APPROVED,), STALE, note)
            head = await server.audit("stale", **_fields(plan), reason=plan.note)
            await self._say(job, f"❌ {plan.name} is stale: {plan.note}. Request again.", head)
            return
        plan = store.begin_apply(plan.id, current)
        if plan.state != APPLYING:
            event = "expired" if plan.state == EXPIRED else "stale"
            changes = self._changes(plan, now) if event == "stale" else []
            what = {"changes": changes} if changes else {}
            head = await server.audit(event, **_fields(plan), reason=plan.note, **what)
            since = f"; since the plan: {'; '.join(changes)}" if changes else ""
            text = f"❌ {plan.name} is {plan.state} ({plan.note}{since}): nothing was done."
            await self._say(job, f"{text} Request again.", head)
            return
        try:
            await server.audit(
                "apply_start",
                **_fields(plan),
                hash=plan.hash,
                verb=action.verb,
                args=list(action.args),
                approvers=list(server.approvals.approved_by(plan.id)),
                server=server.release,
                **ApplyEnd.start_fields(action.targets),  # the ledger forgets their syncs here
            )
        except Exception as exc:  # nothing has run, and nothing will
            LOG.warning("%s: apply_start not recorded (%s)", plan.name, type(exc).__name__)
            store.finish(plan.id, False)
            await self._say(job, f"❌ {plan.name} failed: the audit log can't be written.", None)
            return
        await self._steps(job, plan, Rows(action, live.vms))

    async def _steps(self, job: Job, plan: Plan, rows: Rows) -> None:
        """The steps in order, up to the first that fails, or each row's up to its own first
        failure (`Rows`); `apply_end` however it ends."""
        shown, steps, started = Capture(), list[Ran](), time.monotonic()
        deadline = started + limit(job.action)
        ticker: asyncio.Task[None] | None = None
        tmp, outcome, before = "", "error", None
        try:  # from here on, every way out writes apply_end
            before = await asyncio.to_thread(self.server.backups.state)  # what a backup holds
            tmp = self._fresh(plan.name)
            text = self._progress(job, steps, "")
            message = await job.bot.post(job.stream, job.topic, text)
            if message is not None:
                ticker = asyncio.create_task(self._ticker(job, message, steps, shown, text))
            failed = False
            for step in job.action.applied:
                if rows.skip(step):
                    continue
                env = self.environment(tmp, step.env)
                steps.append(ran := Ran(script(step.argv), row=step.row))
                began = time.monotonic()
                await self._step(step.argv, env, tmp, deadline, shown, ran=ran)
                if step.row:
                    rows.ran(step, ran, time.monotonic() - began)
                if ran.ended or not (ran.ok or step.tolerated or step.row):
                    failed = True
                    break
            timed_out = any(ran.ended for ran in steps)
            failed = failed or bool(rows.stopped)
            outcome = "timed out" if timed_out else "failed" if failed else "done"
        except asyncio.CancelledError:
            outcome = "interrupted"
            raise
        except Exception as exc:
            LOG.warning("%s: the apply failed (%s)", plan.name, type(exc).__name__, exc_info=exc)
            why = str(exc) if isinstance(exc, RunnerError) else type(exc).__name__
            shown.feed(f"── the runner failed ({why}): nothing more ran\n")
        finally:
            if ticker is not None:
                ticker.cancel()
                await asyncio.wait([ticker])
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            await self._end(job, plan, steps, shown, started, outcome, before, rows)

    async def _end(
        self, job: Job, plan: Plan, steps: list[Ran], shown: Capture, started: float, outcome: str,
        before: tuple[Any, ...] | None, rows: Rows,
    ) -> None:  # fmt: skip
        """`apply_end`, then (unless the bot is stopping) the plan's state, its result and the
        backup that follows a change."""
        server, action = self.server, job.action
        assert server.store is not None
        failed = next((ran for ran in steps if not ran.ok), None)
        code = 0 if outcome == "done" else (failed.code if failed and failed.code else -1)
        head = None
        await asyncio.to_thread(server.reload_secrets)  # what a cut-short step wrote, too
        scrubbed = await asyncio.to_thread(server.scrub, shown.kept()) + shown.note()
        saved, digest = await asyncio.to_thread(self._write, plan, "apply.txt", scrubbed)
        ends = rows.ends() if rows.cells else None
        end = ApplyEnd(plan.workshop, plan.name, action.verb, action.args, code, action.targets,
                       ends)  # fmt: skip
        try:
            head = await server.audit(
                "apply_end",
                **end.fields(),
                bot=plan.bot,
                hash=plan.hash,
                outcome=outcome,
                steps=[_step_record(r) for r in steps],
                argv=[list(step.argv) for step in action.steps],
                duration_s=round(time.monotonic() - started),
                output=saved,
                output_sha256=digest,
                server=server.release,
            )
        except Exception as exc:  # the result says so (review of #354)
            LOG.warning("%s: apply_end not recorded (%s)", plan.name, type(exc).__name__)
        if outcome == "interrupted":
            return  # still APPLYING: the next start marks it INTERRUPTED and says so
        server.store.finish(plan.id, code == 0)
        await self._result(job, plan, outcome, steps, scrubbed, started, head, rows)
        await server.backups.after(job, before)

    async def _result(
        self, job: Job, plan: Plan, outcome: str, steps: list[Ran], scrubbed: str,
        started: float, head: Any, rows: Rows | None = None,
    ) -> None:  # fmt: skip
        n, total = len(steps), len(job.action.applied)
        where = f"step {n} of {total}: {steps[-1]}" if steps else ""
        what = {
            "done": ("✅", f"done in {took(time.monotonic() - started)}, every step exited 0"),
            "failed": ("❌", f"failed at {where}. Nothing after it ran"),
            "timed out": ("⏱", f"timed out after {took(limit(job.action))}, at {where}"),
        }.get(outcome, ("❌", f"the runner failed, {n} of {total} steps begun. Nothing more ran"))
        table: list[str] = []
        if rows is not None and rows.cells:  # a row's failure stops that row alone
            what, table = self._rows(rows, outcome, started, what), [fence(rows.table())]
            table += self._after(job, rows)
        inline, cut = _tail(scrubbed)
        title = f"{what[0]} **{plan.name}** · `{short(plan.command)}`: {what[1]}"
        most = job.bot.client.limits.get("max_message_length", 10000) if job.bot else 10000
        if table and len("\n".join([title, *table, inline])) > most - RESULT_ROOM:
            # cut to fit, as a plan is (review of #564): the whole of it attached
            whole = "\n".join([title, rows.table() if rows else "", *table[1:], scrubbed])
            link = await self._attach(job.bot, f"{plan.name}-result.txt", whole)
            got = f"the whole result, its table included: {link}" if link else "attachment failed"
            table = [f"*Too long to show here: {got}.*", table[-1]]  # the reminder stays
        lines = [title, *table, inline]
        if cut:
            link = await self._attach(job.bot, f"{plan.name}-apply.txt", scrubbed)
            whole = f"the whole output: {link}" if link else "the whole output's attachment failed"
            lines.append(f"*The last lines; {whole}.*")
        lines.append(
            footer(head)
            if head is not None
            else "⚠ `apply_end` was not written to the audit log: tell a deployer. Until it is "
            "fixed, `!adopt-spare` replays the fleet's settings as they were before this apply."
        )
        await self._post(job, "\n".join(lines))

    @staticmethod
    def _rows(rows: Rows, outcome: str, started: float, what: tuple[str, str]) -> tuple[str, str]:
        """A per-row result's headline: how many rows ran to their end, those torn down after a
        failure that `unsynced` tolerated, and the first stopped (review of #564)."""
        done = [row for row in rows.cells if rows.retired(row)]
        said = f"{len(done)} of {len(rows.cells)} rows retired"
        lost = [f"{row} ({', '.join(rows.unsaved(row))} failed)" for row in done
                if rows.unsaved(row)]  # fmt: skip
        if lost:
            said += f"; torn down unsynced: {', '.join(lost)}"
        stopped = next((row for row in rows.cells if row in rows.stopped), None)
        if stopped is not None:
            stage = next(s for s, c in rows.cells[stopped].items() if c.startswith("failed"))
            said += f"; {stopped} stopped at {stage}" + " and more" * (len(rows.stopped) > 1)
        if outcome == "done":
            return "✅", f"done in {took(time.monotonic() - started)}, {said}"
        if outcome == "failed":
            return "❌", f"{said}. The other rows went on; send it again for those stopped"
        return what[0], f"{what[1]}; {said}"

    @staticmethod
    def _after(job: Job, rows: Rows) -> list[str]:
        """Under the table: each stopped row's VM as it is left and how to go on, each archive
        exported, and what the participants still do."""
        out = [stopped_row(job.fleet, row, cells) for row, cells in rows.cells.items()
               if row in rows.stopped]  # fmt: skip
        out += [f"- {row}'s archive: `out/exports/export-{row}.tar.gz`, on the server"
                for row, cells in rows.cells.items() if cells.get("export") == "ok"]  # fmt: skip
        out.append(
            "Remind the participants to revoke their other sessions in their ChatGPT and Claude "
            "account settings."
        )
        return out

    async def _post(self, job: Job, text: str) -> None:
        """A result in the plan's topic, tried `POSTS` times, `BACKOFF` seconds apart and
        doubling, but only while each try surely never landed (`AdminBot.deliver`); the outcome
        is in the audit log whatever happens to the post."""
        for attempt in range(POSTS):
            posted, unsent = await job.bot.deliver(
                type="stream", to=job.stream, topic=job.topic, content=text
            )
            if posted is not None:
                return
            if not unsent:  # it may have landed: never posted twice (re-review of #354)
                LOG.warning("%s: its result may have been posted; not sent again", job.plan.name)
                return
            if attempt + 1 < POSTS:
                await asyncio.sleep(BACKOFF * 2**attempt)
        LOG.warning("%s: its result was not posted", job.plan.name)

    async def _attach(self, bot: AdminBot, name: str, scrubbed: str) -> str | None:
        """Scrubbed text as an attachment of at most `ATTACHMENT_BYTES`: its link, or None."""
        data = scrubbed.encode("utf-8")
        if len(data) > ATTACHMENT_BYTES:
            note = f"\n[cut at {ATTACHMENT_BYTES} bytes]\n".encode()
            data = data[: ATTACHMENT_BYTES - len(note)].decode("utf-8", "ignore").encode() + note
        try:
            url = await bot.client.upload(name, data)
        except Exception as exc:
            LOG.warning("%s: the attachment failed (%s)", name, type(exc).__name__)
            return None
        return f"[{name}]({url})"

    def _progress(self, job: Job, steps: list[Ran], scrubbed: str) -> str:
        """The progress line: the step and the output's last whole line, no time, so that it
        changes only when they do (#754); the result says how long the apply took."""
        n, total = len(steps), len(job.action.applied)
        where = f"step {n} of {total}, {steps[-1].script}" if steps else "starting"
        where += f" for {steps[-1].row}" if steps and steps[-1].row else ""
        line = next((x for x in reversed(scrubbed.split("\n")) if x.strip()), "")
        said = f"\nlast line: {span(line[-200:])}" if line else ""
        return f"⏳ **{job.plan.name}** · `{short(job.plan.command)}`: applying, {where}{said}"

    async def _ticker(
        self, job: Job, message: int, steps: list[Ran], shown: Capture, posted: str
    ) -> None:
        """Edit the progress message every `PROGRESS` seconds, from everything so far, when it
        changed since the text `posted` last (#754)."""
        while True:
            await asyncio.sleep(PROGRESS)
            try:
                # The snapshot first: a key it holds was written to its file before it was
                # printed, so the check after it sees the file (re-review of #354).
                whole = shown.kept(whole_lines=True)
                await self.server.watch_secrets()
                scrubbed = await asyncio.to_thread(self.server.scrub, whole)
                text = self._progress(job, steps, scrubbed)
                if text != posted and await job.bot.edit(message, text):
                    posted = text
            except Exception as exc:
                LOG.warning("%s: no progress edit (%s)", job.plan.name, type(exc).__name__)

    async def _say(self, job: Job, text: str, head: Any) -> None:
        await self._post(job, f"{text}\n{footer(head)}" if head else text)
