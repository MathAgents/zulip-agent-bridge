"""What a bot reads about its workshop: the roster, `spares.json`, and the audit log's record of
what has been applied (docs/ADMIN.md §2.1, §2.6, §2.8; §3, action 7).

- **The fleet.** `read_fleet` reads a workshop's roster and `spares.json` afresh for each
  command into the `Fleet` that `actions.build` takes, as the scripts read them
  (`aws_common.read_roster`, `power.select`), without importing `workshop/`.
- **The ledger.** The audit log is the server's memory, and it is backed up (§1.7); `state.db`
  is not. At start the bot reads every record into a `Ledger`, and then each record it appends:
  - `floor`, the highest plan ID handed out, for `PlanStore`: only from the `request` records,
    which the bot writes as it hands each ID out, never from a name someone typed (an
    `!approve P-999999999` of no plan is recorded as `named`; review of #346);
  - which lines of `deploys.jsonl` are in the chain already (`upgrade` records), by digest,
    so that a server restored from another's backup (§1.7) still copies its own;
  - the release the server runs, from the last deploy;
  - per workshop, what `!adopt-spare` must give a new VM (§3, action 7): whether the fleet is
    held, its last rollout's release, the settings its VMs now have (that rollout's, and
    the `STICKY` ones as last given), and whether its waker is on (its last `!waker`; #440),
    from successful `apply_end` records of the whole fleet
    (`all`): a canary, a single VM's change or a late joiner never changes what a spare gets
    (decided on #344). The runner (PR 10)
    writes those with `ApplyEnd`, the one definition of their fields. A workshop with no such
    record has nothing recorded, and `!adopt-spare` stays refused (`actions.build`), as it
    does for a record that doesn't read back exactly;
  - per workshop, each row's end of the workshop (`Ending`; §3, action 7; the
    decision on #367): its last successful `!uninstall` and `!final-sync`. The sync counts,
    and `!teardown` takes the row without `unsynced`, only if it came after the uninstall, so
    that no agent could work on after it, and no plan has named the row since:
    - the `apply_start` of any plan that could let someone work on, or that syncs again
      (`TOUCH`: `!launch`, `!adopt-spare`, `!start`, `!rollout`, `!final-sync`, `!retire`),
      forgets the row's sync, before anything runs, and so does its `apply_end`, whatever its
      exit: an apply whose end is never written (a killed bot, a failed write) has forgotten it
      already;
    - those that bring the bridge back (`REINSTALL`: `!launch`, `!adopt-spare`, `!rollout`)
      forget the uninstall too; a `!start` doesn't, since the bridge stays uninstalled;
    - only a successful `apply_end` marks anything, and only the `targets` it names;
    - a record of those verbs without its `targets`, from a runner older or newer than this
      code, forgets every row of its workshop: it fails closed;
    - a `!retire` (#559) runs each row's steps alone, so its `apply_end` says per row which of
      them exited 0 (`ApplyEnd.rows`), whatever the plan's exit: a row's uninstall that did
      marks it uninstalled, and its sync, which follows it, synced, as the separate verbs
      would; a row whose steps all ran to their end is retired (`Recorded.retired`), and `all`
      leaves it out from then on. A later `!launch` or `!adopt-spare` of it makes it a row in
      service again, from its `apply_start`, whatever its outcome: a launch that failed may
      have left a VM, which `all` must reach (review of #565). Both are refused for a retired
      row until a deployer clears its `host` and `ip` in the roster (`actions.BACK`): with
      them, `launch.py` would leave it alone and exit 0, and the row would be back in `all`
      with no VM and a released address. A `rows` field that doesn't read back exactly marks
      nothing.

    The waker (§3, action 5) can start a VM outside any plan, but after `!uninstall` no agent
    answers on it.
  - per workshop, its last apply that changes termination protection (`Protection`: `!protect`,
    `!launch … protect`; #378), whatever its outcome: the time of its latest `apply_start` or
    `apply_end`, or of the `interrupted` record of one whose end was never written. A start
    counts, for the apply's first `actions.PROTECT_SETTLE`; while it is busy after that, the
    planner refuses a `!teardown` plan itself (`Planning._protecting`), and the check at apply
    catches any plan made before. The `interrupted` record counts, so that the wait covers an
    apply cut short by a killed bot, from the restart. `!teardown` waits `PROTECT_SETTLE` after
    it, from the time `read_fleet` is given.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zab.admin.actions import ActionError, Ending, Fleet, Protection, Row
from zab.admin.config import SCOPE_KEY, AdminConfig, Workshop
from zab.admin.grammar import SPARE_RE

#: A roster or spares.json larger than this is refused rather than read.
MAX_FILE = 4 * 1024 * 1024
_PLAN = re.compile(r"P-([1-9][0-9]{0,17})", re.ASCII)
_VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{7,64}", re.ASCII)
DEPLOYS = "deploys.jsonl"  # an `upgrade` record's `source` when it copies a deploys.jsonl line
CHANGES = "changes.jsonl"  # a `config` record's: a line of the config helper's (#418)


class FleetError(ValueError):
    """A workshop's roster or spares.json can't be read: no command runs against it."""


def _text(path: str) -> str | None:
    """The file's text, or None when it doesn't exist yet."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_FILE + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FleetError(f"{Path(path).name}: {exc.strerror}") from None
    if len(data) > MAX_FILE:
        raise FleetError(f"{Path(path).name} is larger than {MAX_FILE} bytes")
    try:
        return data.decode("utf-8-sig")  # a byte-order mark, as aws_common.read_roster
    except UnicodeDecodeError:
        raise FleetError(f"{Path(path).name} is not UTF-8") from None


def read_roster(path: str) -> list[dict[str, str]]:
    """The roster's participant rows, each column stripped and its name lower-cased; a row's
    `slug` is its `github` login in lower case when it has none, as the scripts read it. A
    missing roster has no rows: a new workshop's."""
    text = _text(path)
    if text is None:
        return []
    rows = []
    for raw in csv.DictReader(io.StringIO(text, newline="")):
        row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        row["slug"] = row.get("slug") or row.get("github", "").lower()
        if row["slug"]:
            rows.append(row)
    return rows


def scope_label(ws: Workshop) -> str:
    """The workshop's name in `spares.json` keys (`aws_common.scope_label`): its tag's value
    when it has one tag, else every `key=value`, sorted and comma-joined; `zab-scope`, the same
    for every workshop of the server, left out."""
    tags = [(k, v) for k, v in ws.tags if k != SCOPE_KEY] or list(ws.tags)
    if len(tags) == 1:
        return tags[0][1]
    return ",".join(f"{k}={v}" for k, v in sorted(tags))


def read_spares(ws: Workshop, rows: list[dict[str, str]]) -> tuple[str, ...]:
    """The workshop's available spares in `spares.json`, as `power.py --spares` selects them:
    this scope and region, not adopted, with an address that no roster row has taken."""
    text = _text(f"{ws.out_dir}/spares.json")
    if text is None:
        return ()
    try:
        data = json.loads(text)
    except ValueError:
        raise FleetError("spares.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise FleetError("spares.json is not a JSON object")
    prefix = f"{scope_label(ws)}/{ws.aws.region}/"
    taken = {row["ip"] for row in rows if row.get("ip")}
    names = []
    for key, entry in data.items():
        if not (isinstance(entry, dict) and key.startswith(prefix)):
            continue
        name = entry.get("name") or key[len(prefix) :]
        ip = entry.get("ip")
        if not isinstance(name, str) or not SPARE_RE.fullmatch(name):
            continue
        if entry.get("adopted_by") or not isinstance(ip, str) or not ip or ip in taken:
            continue
        names.append(name)
    return tuple(sorted(set(names), key=lambda n: int(n.rsplit("-", 1)[1])))


def read_fleet(
    config: AdminConfig, ws: Workshop, ledger: Ledger | None = None, now: float | None = None
) -> Fleet:
    """`ws` as `actions.build` takes it, read `now` (the bot's clock). What the ledger recorded
    for `!adopt-spare` is added only when it reads back as a valid `Fleet`; otherwise it stays
    unrecorded."""
    rows = read_roster(ws.roster)
    others = tuple(other for other in config.workshops if other.name != ws.name)
    base = {
        "workshop": ws,
        "rows": tuple(  # `excluded`: any mark at all, so that `all` leaves the row out (#627)
            Row(
                r["slug"],
                r.get("ip", ""),
                r.get("region", ""),
                r.get("host", ""),
                excluded=bool(r.get("excluded")),
            )
            for r in rows
        ),
        "spares": read_spares(ws, rows),
        "release_pattern": config.release_pattern,
        "others": others,
        "read_at": now,
    }
    state = ledger.workshop(ws.name) if ledger else None
    if state is not None:
        base["ending"] = state.ending()
        base["protection"] = state.protection
        base["retired"] = dict(state.retired)
    try:
        fleet = Fleet(**base)
    except ActionError as exc:
        raise FleetError(f"the roster of {ws.name}: {exc}") from None
    if state is None or not state.recorded():
        return fleet
    try:
        return Fleet(
            **base,
            release=state.release,
            held=state.held,
            settings=state.settings,
            waker=state.waker,
        )
    except ActionError:
        return fleet  # a record that doesn't read back exactly: !adopt-spare stays refused


def roster_slugs(ws: Workshop) -> frozenset[str]:
    """The slugs of `ws`'s roster, for the hint to another workshop's bot (§2.1); none when the
    roster can't be read."""
    try:
        return frozenset(row["slug"] for row in read_roster(ws.roster))
    except FleetError:
        return frozenset()


# ------------------------------------------------------------------------------------ ledger


@dataclass(frozen=True, slots=True)
class ApplyEnd:
    """The fields of an `apply_end` audit record that the ledger reads back (§2.8). The runner
    (PR 10) writes every one with `fields()`, and may add others (`argv`, `duration_s`, …).
    `args` are the plan's `Command.tokens()`; `exit` 0 means every step succeeded; `targets`
    are its `Action.targets`, the slugs and spares acted on (`all` listed), and the runner
    writes them in its `apply_start` record too (`start_fields`). None: not written. `rows`,
    a `!retire`'s alone (#559): each row → each of its stages → `ok`, `skipped: …`, `failed:
    …` or `not run`, and whether the row was retired (`retire_rows` reads it back)."""

    workshop: str
    plan: str  # "P-<n>"
    verb: str  # a key of grammar.SPECS, such as "rollout" or "provision zulip"
    args: tuple[str, ...]
    exit: int
    targets: tuple[str, ...] | None = None
    rows: Mapping[str, RowEnd] | None = None

    def fields(self) -> dict[str, Any]:
        out = {
            "workshop": self.workshop,
            "plan": self.plan,
            "verb": self.verb,
            "args": list(self.args),
            "exit": self.exit,
        }
        out |= {} if self.targets is None else {"targets": list(self.targets)}
        if self.rows is not None:
            out["rows"] = {row: end.fields() for row, end in self.rows.items()}
        return out

    @staticmethod
    def start_fields(targets: tuple[str, ...]) -> dict[str, Any]:
        """What `apply_start` adds for the ledger: the plan's targets."""
        return {"targets": list(targets)}

    @classmethod
    def read(cls, record: Mapping[str, Any]) -> ApplyEnd | None:
        """The record as an ApplyEnd, or None when a field is missing or of the wrong type;
        `targets` may be missing, and is then None."""
        args, targets = record.get("args"), _words(record.get("targets"))
        values = (record.get("workshop"), record.get("plan"), record.get("verb"))
        if not all(isinstance(v, str) for v in values) or type(record.get("exit")) is not int:
            return None
        if _words(args) is None or (targets is None and "targets" in record):
            return None
        assert isinstance(args, list)
        return cls(*values, tuple(args), record["exit"], targets)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RowEnd:
    """One row of a `!retire` (#559): each stage's cell, in order, whether it was retired, and
    the seconds its steps took (review of #564: the organizers size their batches by them)."""

    cells: tuple[tuple[str, str], ...]
    retired: bool
    seconds: int = 0

    def fields(self) -> dict[str, Any]:
        return {"cells": dict(self.cells), "retired": self.retired, "seconds": self.seconds}


def retire_rows(record: Mapping[str, Any], targets: tuple[str, ...]) -> dict[str, RowEnd] | None:
    """A `!retire`'s `apply_end` `rows`, as `ApplyEnd.fields` writes them, for its `targets`
    only; None when anything in it is not exactly that shape."""
    rows = record.get("rows")
    if not isinstance(rows, dict) or not set(rows) <= set(targets):
        return None
    out = {}
    for row, end in rows.items():
        cells = end.get("cells") if isinstance(end, dict) else None
        if not (isinstance(cells, dict) and isinstance(end.get("retired"), bool)):
            return None
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in cells.items()):
            return None
        seconds = end.get("seconds", 0)
        if type(seconds) is not int:
            return None
        out[row] = RowEnd(tuple(cells.items()), end["retired"], seconds)
    return out


def _words(value: object) -> tuple[str, ...] | None:
    if isinstance(value, list) and all(isinstance(word, str) for word in value):
        return tuple(value)
    return None


#: The settings `push_config.py` leaves as they are when a rollout omits them: the idle-stop
#: timer (`idle_stop` None) and the shared Mathlib, which nothing uninstalls. The others
#: (`steer=`, `multi-party=`, `raw-log=`) are written back to their defaults by every rollout.
#: Since #704 a rollout without them applies their defaults where a VM lacks them (the shared
#: Mathlib; idle stop where the waker is on, unless turned off on purpose), and `!adopt-spare`
#: gives a new VM those defaults where nothing was recorded (`actions.IDLE_STOP_DEFAULT`).
STICKY = ("idle-stop", "lean-shared")
#: A plan of these that names a row forgets its sync, from its `apply_start` (module doc).
TOUCH = frozenset({"launch", "adopt-spare", "start", "rollout", "final-sync", "retire"})
#: These bring a row's bridge back: they forget its uninstall too.
REINSTALL = frozenset({"launch", "adopt-spare", "rollout"})


@dataclass(slots=True)
class Recorded:
    """One workshop's fleet as its last successful applies left it; None: not recorded yet."""

    held: bool | None = None  # its last `!provision zulip` had `hold`
    release: str | None = None  # its last `!rollout`'s version
    rollout: tuple[str, ...] | None = None  # that rollout's settings that the next one resets
    sticky: dict[str, str] = field(default_factory=dict)  # STICKY: the last word given for each
    down: dict[str, str] = field(default_factory=dict)  # row → its last uninstall, "P-n at …"
    #: row → (when, the uninstall it followed or ""): it counts when that isn't ""
    synced: dict[str, tuple[str, str]] = field(default_factory=dict)
    protection: Protection | None = None  # its last apply that changes termination protection
    waker: bool = False  # its last `!waker` was `on` (#440)
    retired: dict[str, str] = field(default_factory=dict)  # row → the `!retire` that ended it

    def ending(self) -> dict[str, Ending]:
        rows = self.down.keys() | self.synced.keys()
        out = {}
        for row in sorted(rows):
            when, after = self.synced.get(row, ("", ""))
            out[row] = Ending(self.down.get(row, ""), when, bool(after), after)
        return out

    @property
    def settings(self) -> tuple[str, ...] | None:
        """The words `!adopt-spare` replays (`Fleet.settings`), once there has been a rollout."""
        return None if self.rollout is None else self.rollout + tuple(self.sticky.values())

    def recorded(self) -> bool:
        return None not in (self.held, self.release, self.settings)

    def copy(self) -> Recorded:
        """A copy with dicts of its own, for `read_fleet` in a thread while the event loop's
        `Ledger.observe` changes this one (the review of #344); the rest is immutable."""
        mine = {"sticky": dict(self.sticky), "down": dict(self.down), "synced": dict(self.synced),
                "retired": dict(self.retired)}  # fmt: skip
        return replace(self, **mine)


def _key(word: str) -> str:
    return word.partition("=")[0]


def protects(verb: object, args: object) -> bool:
    """An apply of this command changes termination protection: `!protect`, `!launch … protect`."""
    if verb == "launch":
        return isinstance(args, list) and "protect" in args
    return verb == "protect"


def _epoch(ts: object) -> float | None:
    """A record's `ts` (`2026-10-31T14:02:11Z`, §2.8) in seconds since the epoch, or None."""
    try:
        when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") if isinstance(ts, str) else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC).timestamp() if when else None


@dataclass(slots=True)
class Ledger:
    floor: int = 0  # the highest plan ID handed out (its `request` record)
    deployed: set[str] = field(default_factory=set)  # the SHA-256 of each line copied
    release: str = ""  # "vX.Y.Z+<commit>", from the last bootstrap or upgrade deploy
    workshops: dict[str, Recorded] = field(default_factory=dict)
    #: plan → (workshop, verb) of each apply that changes termination protection, from its
    #: `apply_start` to its `apply_end`: an `interrupted` record of it counts as its end
    protecting: dict[str, tuple[str, str]] = field(default_factory=dict)

    def workshop(self, name: str) -> Recorded | None:
        return self.workshops.get(name)

    def copy_of(self, name: str) -> Ledger:
        """A ledger of the workshop `name` alone, all `read_fleet` reads, copied (`Recorded.copy`):
        taken on the event loop, which alone changes this one, and read in a thread."""
        state = self.workshops.get(name)
        return Ledger(workshops={} if state is None else {name: state.copy()})

    def observe(self, record: Mapping[str, Any]) -> None:
        """Take one audit record into account; anything unexpected in it is ignored."""
        event, plan = record.get("event"), record.get("plan")
        if event == "request" and isinstance(plan, str) and (m := _PLAN.fullmatch(plan)):
            self.floor = max(self.floor, int(m[1]))
        if event == "upgrade" and record.get("source") == DEPLOYS:
            self._deploy(record)
        if event == "config" and record.get("source") == CHANGES:
            if isinstance(digest := record.get("line_sha256"), str):
                self.deployed.add(digest)  # copied once, as a deploys.jsonl line
        if event in ("apply_start", "apply_end"):
            self._ending(record)
        if event in ("apply_start", "apply_end", "interrupted"):
            self._protection(record)
        if event == "apply_end" and (end := ApplyEnd.read(record)) and end.exit == 0:
            self._applied(end)

    def _protection(self, record: Mapping[str, Any]) -> None:
        """The workshop's last apply that changes termination protection (module doc)."""
        event, plan = record.get("event"), record.get("plan")
        if not (isinstance(plan, str) and _PLAN.fullmatch(plan)):
            return
        if event == "interrupted":
            if (known := self.protecting.pop(plan, None)) is None:
                return
            ws, verb = known
        else:
            ws, verb = record.get("workshop"), record.get("verb")
            if not (isinstance(ws, str) and isinstance(verb, str)):
                return
            if not protects(verb, record.get("args")):
                return
            if event == "apply_start":
                self.protecting[plan] = (ws, verb)
            else:
                self.protecting.pop(plan, None)
        at = _epoch(record.get("ts"))
        if at is None:
            return
        state = self.workshops.setdefault(ws, Recorded())
        if state.protection is None or at >= state.protection.at:
            state.protection = Protection(plan, verb, at)

    def _deploy(self, record: Mapping[str, Any]) -> None:
        digest, deploy = record.get("line_sha256"), record.get("deploy")
        if not isinstance(digest, str):
            return
        self.deployed.add(digest)
        if isinstance(deploy, dict) and deploy.get("event") in ("bootstrap", "upgrade"):
            version, commit = deploy.get("version"), deploy.get("commit")
            if isinstance(version, str) and _VERSION.fullmatch(version):
                ok = isinstance(commit, str) and _COMMIT.fullmatch(commit)
                self.release = f"{version}+{commit[:12] if ok else 'unknown'}"

    def _ending(self, record: Mapping[str, Any]) -> None:
        """Each row's uninstall and sync, from any plan's start and end, not only the whole
        fleet's (module doc)."""
        ws, verb, plan = record.get("workshop"), record.get("verb"), record.get("plan")
        if not (isinstance(ws, str) and isinstance(verb, str)) or verb not in TOUCH | {"uninstall"}:
            return
        state = self.workshops.setdefault(ws, Recorded())
        # a record that isn't exactly the runner's forgets every row (the review of #367)
        targets = _words(record.get("targets")) if isinstance(plan, str) else None
        for verbs, rows in ((TOUCH, state.synced), (REINSTALL, state.down)):
            if verb in verbs:
                for row in list(rows) if targets is None else targets:
                    rows.pop(row, None)
        if verb in ("launch", "adopt-spare"):  # a VM, or one on its way: back in service (#559)
            for row in list(state.retired) if targets is None else targets:
                state.retired.pop(row, None)
        exit = record.get("exit")
        if record.get("event") != "apply_end" or type(exit) is not int:
            return
        if targets is None or not isinstance(plan, str):
            return
        ts = record.get("ts")
        when = f"{plan} at {ts[:16].replace('T', ' ')} UTC" if isinstance(ts, str) else plan
        if verb == "retire":  # each row's own steps, whatever the plan's exit (#559)
            self._retired(state, record, targets, when)
            return
        if exit != 0:
            return  # exactly the int 0, as `ApplyEnd.read` requires: never False or 0.0
        if verb == "uninstall":
            state.down.update((row, when) for row in targets)
        elif verb == "final-sync":  # it counts only after the row's uninstall
            state.synced.update((row, (when, state.down.get(row, ""))) for row in targets)

    @staticmethod
    def _retired(
        state: Recorded, record: Mapping[str, Any], targets: tuple[str, ...], when: str
    ) -> None:
        """A `!retire`'s rows (module doc): uninstalled, synced after it, retired."""
        for row, end in (retire_rows(record, targets) or {}).items():
            cells = dict(end.cells)
            if cells.get("uninstall") == "ok":
                state.down[row] = when
            if cells.get("final-sync") == "ok":
                state.synced[row] = (when, state.down.get(row, ""))
            if end.retired:
                state.retired[row] = when

    def _applied(self, end: ApplyEnd) -> None:
        if "all" not in end.args:  # `all` is reserved: no slug can be it
            return  # a subset of the fleet: what a spare gets stays as the fleet has it
        state = self.workshops.setdefault(end.workshop, Recorded())
        if end.verb == "provision zulip":
            state.held = "hold" in end.args
        elif end.verb == "rollout" and end.args:
            # the words `actions._replay` maps: `key=value` settings and `lean-shared`, never the
            # version, the selection or `restart-codex`
            given = [w for w in end.args[1:] if "=" in w or w == "lean-shared"]
            state.release = end.args[0]
            state.rollout = tuple(w for w in given if _key(w) not in STICKY)
            state.sticky.update((_key(w), w) for w in given if _key(w) in STICKY)
        elif end.verb == "idle-stop" and end.args:
            state.sticky["idle-stop"] = f"idle-stop={end.args[0]}"
        elif end.verb == "waker" and end.args:
            state.waker = end.args[0] == "on"


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """The audit log's records, in order; a line that is no JSON object is skipped (`verify`
    reports it)."""
    try:
        f = path.open("rb")
    except FileNotFoundError:
        return
    with f:
        for raw in f:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def plan_dirs_floor(runs: str) -> int:
    """The highest `<n>` of the `runs/P-<n>/` directories (§1.3), or 0."""
    try:
        names = os.listdir(runs)
    except FileNotFoundError:
        return 0
    return max((int(m[1]) for name in names if (m := _PLAN.fullmatch(name))), default=0)
