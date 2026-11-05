"""A9, the end of a workshop (docs/ADMIN.md §7, "A9 in detail"; issues #356, #357): what the
scenario reads in the bot's plans, results and audit log, and the waits it needs. Pure where it
can be, and tested offline against what the product itself prints (`test_harness_ending.py`):

- the teardown plan: each row's end of the workshop (`participant_rows`, from
  `actions.Ending.line`) and its dry run (`dry_run`: `teardown.py`'s `terminate`, `keep` and
  `release` lines);
- the 5-minute guard after a `!protect` (#378, `actions.Protection.wait`): the time its refusal
  or STALE names to request again (`retry_at`), and the request sent again then
  (`after_the_guard`);
- the product's texts that A9 matches (`refused_unsynced`, `stale_by_the_guard`, `recorded`,
  `approved_by_two`, `approved_by_an_admin`, `needs_phrase`, `approved_already`), each rendered
  by the product in the offline tests;
- the rows of `!retire` (`RETIRE_KEEPER`, `retiring`, `never`; #559);
- the throwaway row (`throwaway`, §7: `zab-test-tmp-<runid>`), the roster's input columns
  (`inputs`), a spare's address in `spares.json` (`spare_ip`), and an earlier run's throwaway
  bots (`stale_throwaways`);
- the audit records of one plan (`of_plan`), and the rig's instances running (`running`);
- a VM's bridge after `!uninstall` (`INACTIVE`).
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from e2e.admin.harness.chat import Plan, Refused, Talk
from e2e.admin.harness.gate import tags_of

#: The guard's wait after an apply that changes termination protection, as
#: `zab.admin.actions.PROTECT_SETTLE` (#382; a test compares them once it is on main).
PROTECT_SETTLE_S = 300
#: Sent again this long after the time the guard names: the server's clock and the development
#: machine's.
GUARD_MARGIN_S = 15.0
#: `Protection.wait`: "P-41 (!protect) changed termination protection here at 14:48 UTC, …".
GUARD = re.compile(r"\((![a-z-]+(?: … protect)?)\) changed termination protection here at "
                   r"(\d\d):(\d\d) UTC")  # fmt: skip
RETRY = re.compile(r"Request it again from (\d\d):(\d\d) UTC")
#: `status.py --json`'s bridge of a VM whose bridge doesn't run (`systemctl --user is-active
#: zab` for a unit that is gone, or no zab at all): known values, never null (unknown).
INACTIVE = ("inactive", "failed", "no zab")
#: The prefix of the throwaway row's slug (§7, "The throwaway row").
THROWAWAY = "zab-test-tmp-"
_THROWAWAY_BOT = re.compile(rf"^(?:Claude|Codex) · ({THROWAWAY}[0-9a-f]+)$")
_TERMINATE = re.compile(r"^terminate (i-[0-9a-f]+) \S* \([a-z-]+, participant (\S+)\)$", re.M)
_KEEP = re.compile(
    r"^keep (i-[0-9a-f]+) \S*: termination protection is on \(protect\.py --off --only (\S+) ",
    re.M,
)
_RELEASE = re.compile(r"^release (eipalloc-[0-9a-f]+)\b", re.M)
#: `TOPICS.md` after a final sync that had nothing to write (`final_sync_remote.py`): unchanged,
#: or not written at all while the VM has no agent session, which none of the scenarios starts
#: (Codex round 1 on #385). `final_sync.py` takes both as fine.
SYNC_IDLE = ("unchanged", "skipped: no sessions")
#: A row's state in a teardown plan (`actions.Ending.line`): its sync counts, or it has none.
SYNCED = r"synced P-\d+ at [^,]+, after its uninstall P-\d+ at .+"
UNSYNCED = r"uninstalled P-\d+ at .+, not synced since: unsynced"
_ROWS = re.compile(r"^\*\*Participant rows\*\* \((\d+)\), (\d+) unsynced:$", re.M)
_ROW = re.compile(r"^- ([a-z0-9][a-z0-9-]*): (.+)$")


def refused_unsynced(slug: str) -> str:
    """`actions._build_teardown`'s refusal of a row whose sync doesn't count (§3, action 7)."""
    return f"no !final-sync after its !uninstall on record for {slug}"


def stale_by_the_guard(plan: str) -> str:
    """The pattern of `Planning._apply`'s STALE when the guard refuses the rebuilt teardown."""
    return (rf"^❌ {plan} is stale: it no longer builds: P-\d+ \(!protect\) changed termination "
            r"protection here at \d\d:\d\d UTC")  # fmt: skip


def recorded(plan: str) -> str:
    """The pattern of the answer to one approval of two (`Approvals._answer`, `recorded`)."""
    return rf"^✅ {plan}: 1 of 2 approvals, from distinct approvers"


#: Who completes a destructive plan, as its plan and approval message say (`approvals.takes`).
TWO_OR_AN_ADMIN = "2 distinct approvers, or a global admin alone"


def approved_by_two(plan: str) -> str:
    """`Approvals._answer`'s answer to the approval that completes a plan by the count."""
    return f"✅ {plan} approved (2 distinct approvers)"


def approved_by_an_admin(plan: str) -> str:
    """`Approvals._answer`'s answer to a global admin's approval that completes a plan of
    several approvers on its own (#558)."""
    return f"✅ {plan} approved (a global admin's approval)"


def needs_phrase(phrase: str) -> str:
    """`approvals._reason`'s `phrase` refusal, which names the phrase the plan needs."""
    return f"needs the phrase `{phrase}` after the hash"


def approved_already(plan: str) -> str:
    """`approvals._reason`'s `again`: the same approver a second time."""
    return f"you approved {plan} already: it needs another approver"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------------- the teardown plan


@dataclass
class TeardownDryRun:
    """What `teardown.py --dry-run` lists: participant (or spare) → instance, for the VMs it
    would terminate and those it keeps (protected); the Elastic IPs it would release."""

    terminate: dict[str, str] = field(default_factory=dict)
    keep: dict[str, str] = field(default_factory=dict)
    release: list[str] = field(default_factory=list)


def dry_run(text: str) -> TeardownDryRun:
    """The dry run of a teardown plan (`text`: the plan, whole). Its lines stand alone in the
    fenced block; the argument lists never begin with these words."""
    return TeardownDryRun(
        terminate={who: iid for iid, who in _TERMINATE.findall(text)},
        keep={who: iid for iid, who in _KEEP.findall(text)},
        release=sorted(set(_RELEASE.findall(text))),
    )


def participant_rows(text: str) -> tuple[int, int, dict[str, str]]:
    """The teardown plan's `**Participant rows** (n), m unsynced:` and each row's line after it
    (`- slug: …`, `actions.Ending.line`): (n, m, slug → its state). A plan cut to fit shows
    some of the rows, and its attachment all: the fullest list counts. AssertionError without."""
    best: tuple[int, int, dict[str, str]] | None = None
    for head in _ROWS.finditer(text):
        rows = {}
        for line in text[head.end() :].lstrip("\n").split("\n"):
            found = _ROW.match(line)
            if found is None:
                break
            rows[found.group(1)] = found.group(2)
        if best is None or len(rows) > len(best[2]):
            best = (int(head.group(1)), int(head.group(2)), rows)
    if best is None:
        raise AssertionError(f"no participant rows in the plan: {text[:300]}")
    return best


# ------------------------------------------------------------------------------- the guard


def retry_at(text: str, now: dt.datetime) -> dt.datetime | None:
    """When the guard's refusal (or STALE) in `text` says to request a `!teardown` again, as
    a time in UTC on or after `now`'s day (a time more than 12 hours before `now` is the next
    day's: past midnight); None when `text` is no such refusal."""
    if not GUARD.search(text) or not (found := RETRY.search(text)):
        return None
    at = now.astimezone(dt.UTC).replace(
        hour=int(found.group(1)), minute=int(found.group(2)), second=0, microsecond=0
    )
    if at < now - dt.timedelta(hours=12):
        at += dt.timedelta(days=1)
    return at


async def wait_until(
    when: dt.datetime,
    *,
    now: Callable[[], dt.datetime] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> float:
    """Until `GUARD_MARGIN_S` after `when`, the time the guard named (UTC): the seconds slept.
    AssertionError when that is further off than the guard can name (a misread, or a clock)."""
    now, sleep = now or utcnow, sleep or asyncio.sleep
    wait = (when - now()).total_seconds()
    assert wait <= PROTECT_SETTLE_S + 90, f"the guard names {when:%H:%M} UTC: {wait:.0f} s away"
    pause = max(0.0, wait) + GUARD_MARGIN_S
    await sleep(pause)
    return pause


async def after_the_guard(
    t: Talk,
    command: str,
    *,
    now: Callable[[], dt.datetime] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> tuple[Plan, str]:
    """`command`, a `!teardown`, requested; when the guard refuses it (a `!protect` applied
    within 5 minutes, #378), requested again from the time it names: (the plan, the guard's
    refusal or ""). Any other refusal is `Refused`; so is a second refusal by the guard."""
    now, sleep = now or utcnow, sleep or asyncio.sleep
    try:
        return await t.request(command), ""
    except Refused as exc:
        said = exc.message["content"]
        when = retry_at(said, now())
        if when is None:
            raise
    await wait_until(when, now=now, sleep=sleep)
    return await t.request(command), said.split("\n", 1)[0]


# ------------------------------------------------------------------- rows, spares and bots


#: The row of A9's `!retire` (#559) that runs the happy path: the same slug every run, so that
#: its repository, `MathAgents/zab-test-r`, which an owner made once (ADMIN §7) and each run's
#: `!provision repos` reaches with Q9's GitHub token, is synced to again.
#: It never has bots: the retire's bots step finds them absent.
RETIRE_KEEPER = "zab-test-r"


def retiring(started: float) -> str:
    """The run's own row of A9's `!retire`, never provisioned: its sync fails (#559)."""
    return f"{throwaway(started)}-r"


def never(started: float) -> str:
    """The run's row of A9's `!retire` that is never launched: no `ip` (#559)."""
    return f"{throwaway(started)}-n"


def throwaway(started: float) -> str:
    """The throwaway row's slug for the run that began at `started` (epoch seconds): lower
    case, letters and digits after the prefix, 32 characters at most (§2.2's slug)."""
    return f"{THROWAWAY}{int(started):x}"


def inputs(roster: str, columns: Iterable[str]) -> list[dict[str, str]]:
    """The roster's rows (its text, as on the server), with only `columns`: the input columns
    an upload carries (§3.1), so that an import of them adds or changes nothing else."""
    wanted = list(columns)
    out = []
    for raw in csv.DictReader(io.StringIO(roster.removeprefix("\N{BYTE ORDER MARK}"))):
        row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        if row.get("slug"):
            out.append({c: row.get(c, "") for c in wanted})
    return out


def torn_down(row: Mapping[str, str], slug: str) -> str:
    """Why `row` doesn't read as a row whose VM a teardown ended, or "" (#604): no `ip`, since
    its address was released and EC2 may give it to the next VM; its `host` kept, so that
    launch.py leaves the row alone."""
    if row.get("ip"):
        return f"{slug} keeps the ip {row['ip']}, released with its VM"
    if row.get("host") != f"ws-{slug}":
        return f"{slug}'s host is {row.get('host')!r}, not ws-{slug}"
    return ""


def leaves_out(shown: str, retired: Iterable[str], rows: Mapping[str, Mapping[str, str]]) -> str:
    """Why a plan's targets and commands (`shown`) would reach a retired row, or "" (#565,
    #604): naming one, or passing power.py's `--all`, which takes every roster row with an
    `ip`, while one of them still has an ip. A row its `!retire` tore down has none: the
    teardown cleared it (`teardown.py --roster`), so `--all` leaves it out by itself, and the
    planner names the rows instead only while a retired row keeps an ip (`_build_power`)."""
    names = "|".join(re.escape(slug) for slug in retired)
    if names and re.search(rf"(?<![a-z0-9-])({names})(?![a-z0-9-])", shown):
        return "it names a retired row"
    held = sorted(slug for slug in retired if rows.get(slug, {}).get("ip"))
    if held and re.search(r"(?<![\w-])--all(?![\w-])", shown):
        return f"it passes --all, which takes {', '.join(held)}: retired, with an ip still"
    return ""


def shared_ips(rows: Mapping[str, Mapping[str, str]]) -> dict[str, list[str]]:
    """Each `ip` that more than one row holds, with those rows: a roster every admin script
    refuses (`admin_common.check_duplicates`, #604)."""
    holders: dict[str, list[str]] = {}
    for slug, row in rows.items():
        if row.get("ip"):
            holders.setdefault(row["ip"], []).append(slug)
    return {ip: slugs for ip, slugs in holders.items() if len(slugs) > 1}


def rows_by_slug(roster: str) -> dict[str, dict[str, str]]:
    """Every column of the roster's rows, by slug (`repo_status`, `ip`, …)."""
    out = {}
    for raw in csv.DictReader(io.StringIO(roster.removeprefix("\N{BYTE ORDER MARK}"))):
        row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        if row.get("slug"):
            out[row["slug"]] = row
    return out


def spare_ip(spares: Mapping[str, Any], name: str) -> str | None:
    """The address `spares.json` records for the spare `name` (keyed `<scope>/<region>/<name>`,
    `launch.record_spares`), or None."""
    for key, entry in spares.items():
        if isinstance(entry, dict) and (entry.get("name") or key.rsplit("/", 1)[-1]) == name:
            ip = entry.get("ip")
            return ip if isinstance(ip, str) and ip else None
    return None


def stale_throwaways(names: Iterable[str], keep: str) -> list[str]:
    """The bots, by full name, of an earlier run's throwaway row (`Claude · zab-test-tmp-…`,
    `Codex · …`), any but `keep`'s: a run that failed before its `!deactivate-bots` left them."""
    return [n for n in names if (m := _THROWAWAY_BOT.match(n)) and m.group(1) != keep]


def of_plan(records: Iterable[Mapping[str, Any]], plan: str) -> dict[str, list[dict[str, Any]]]:
    """The audit records of `plan` (`P-n`), by event."""
    out: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("plan") == plan:
            out.setdefault(str(record.get("event")), []).append(dict(record))
    return out


def running(instances: Iterable[Mapping[str, Any]]) -> list[str]:
    """The participants (or, without the tag, the IDs) of the instances pending or running."""
    return sorted(
        tags_of(i).get("participant") or str(i.get("InstanceId"))
        for i in instances
        if (i.get("State") or {}).get("Name") in ("pending", "running")
    )
