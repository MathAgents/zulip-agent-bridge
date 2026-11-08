"""Each admin command's scripts, as exact argument lists (docs/ADMIN.md §2.3, §2.4, §3).

`build(command, fleet)` maps a parsed `Command` (`zab.admin.grammar`) to an `Action`: every
step's argument list on apply and, where the step has one, in the plan's dry run. `plan_hash`
hashes a plan (§2.4, step 3) through the verb's target material and normalizer.

- **Fail closed.** A verb without an entry in `VERBS` is refused, never guessed, as is a keyword
  or setting without a flag in `KEYWORD_FLAGS` or `SETTING_FLAGS`, or a field the verb's spec
  doesn't name. A test keeps `VERBS` and `BOT_VERBS` equal to the grammar's `SPECS`.
- **Only this workshop.** `Fleet` is the bot's own workshop. Every slug and spare is checked
  against it again, and every value in a list comes from it, from `Layout` or from a fixed flag,
  through `_value` (a safe alphabet, never a leading `-`). Nothing is copied from a message.
- **Never an implicit everyone.** The scripts read a missing `--only` as every participant
  (`admin_common.select_rows`), so each per-participant step names its targets: `--only` for
  each (`all` is every roster slug, listed), or `power.py --all`/`--spares`. An empty target
  list is refused.
- **Scope and region.** Every AWS script gets each scope tag (`--tag K=V`) and the workshop's
  one region; a selected row recorded in another region is refused (§2.6).
- **Retired rows** (#559). `all` never names a row that a `!retire` ended (`Fleet.retired`);
  naming it still works.
- **Excluded rows** (#627). `all` never names a row `!roster remove` excluded (`Row.excluded`),
  and a change verb that names one is refused, but for `!roster restore` and `!roster rename`:
  nothing is provisioned for it, and nothing is until it is restored.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import os
import re
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from zab.admin.config import SCOPE_KEY, Workshop
from zab.admin.grammar import (
    MAX_DISK_GB,
    SPECS,
    VERSION_RE,
    Command,
    Roster,
    Selection,
    keep_until_end,
)

Argv = tuple[str, ...]
#: How a verb's plan is hashed beyond its lists and inputs (§2.4's target-material table).
READ, TARGETS, DRY_RUN, INPUTS, RECONCILER = "read", "targets", "dry-run", "inputs", "reconciler"
RETIRE = "retire"  # each target's VMs and the teardown's dry run (#559)
#: A dry run's exit code for "nothing to change" (`Verb.nothing`; the `NOTHING` of roster_sync.py,
#: roster_import.py and provision_repos.py): no plan is offered for a change that makes none
#: (#627), and the bot answers it as a success, with ☑ (#708).
NOTHING = 3
#: A value from config or roster never starts with `-`; every word of a list is in `_WORD`.
_VALUE = re.compile(r"[A-Za-z0-9_./:=@+~,][A-Za-z0-9_./:=@+~,-]{0,1023}", re.ASCII)
_WORD = re.compile(r"[A-Za-z0-9_./:=@+~,-]{1,1024}", re.ASCII)
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", re.ASCII
)
_DURATION = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s)\b", re.ASCII)
_GH = ("GH_TOKEN",)
BOT_KINDS = ("claude", "codex")  # zulip_provision.py's bots per row, out/<slug>/<kind>.zuliprc


class ActionError(ValueError):
    """The command can't be mapped exactly: nothing runs. One line of safe words."""


@dataclass(frozen=True, slots=True)
class Layout:
    """The admin server's fixed paths (§1.3, §2.3, #329); tests pass others."""

    release: str = "/opt/zab-admin/current"  # the signed tree, scripts under workshop/
    python: str = "/opt/zab-admin/current/.venv/bin/python"  # -E -s -B -u, not -I (§2.3)
    provisioner_zuliprc: str = "/etc/zab-admin/secrets/provisioner.zuliprc"
    wheels: str = "/var/lib/zab-admin/releases/wheels"  # placed by deploy.sh --wheel (§2.6)
    runs: str = "/var/lib/zab-admin/runs"  # runs/P-<n>/roster-upload.csv (§3.1)
    retired: str = "/var/lib/zab-admin/retired-slugs.txt"  # slugs never reused (§2.1)
    waker: str = "/var/lib/zab-admin/waker"  # the waker's config and opted-in keys (§1.3)
    fleet_key: str = "/var/lib/zab-admin/.ssh/id_ed25519.pub"  # its public half: key pairs
    config_request: str = "/var/lib/zab-admin-config/requests"  # to the config helper (§2.9)
    config_result: str = "/var/lib/zab-admin-config/result.json"  # the helper's outcome
    upgrade: str = "/var/lib/zab-admin-upgrade"  # the upgrade helpers' requests/, results/ (#569)


LAYOUT = Layout()


@dataclass(frozen=True, slots=True)
class Ending:
    """A row's end of the workshop, as the audit log has it (`fleet.Ledger`; §3, action 7):
    its latest successful `!uninstall` and its latest successful `!final-sync`, each `P-n at
    <time>` or "", and whether that sync counts for `!teardown`: it came after an uninstall
    (`after`, the one it followed: no agent could work on after it), and no plan has named the
    row since."""

    uninstalled: str = ""
    synced: str = ""
    counts: bool = False
    after: str = ""  # the uninstall the counting sync followed

    def line(self, slug: str) -> str:
        """The row's state, for the teardown plan (decided on #367); its times in
        order, whatever uninstall came later."""
        if self.counts:
            return f"{slug}: synced {self.synced}, after its uninstall {self.after}"
        if self.synced and self.uninstalled:
            return f"{slug}: synced {self.synced} before its uninstall {self.uninstalled}: unsynced"
        if self.synced:
            return f"{slug}: synced {self.synced} while its agents ran, not uninstalled: unsynced"
        if self.uninstalled:
            return f"{slug}: uninstalled {self.uninstalled}, not synced since: unsynced"
        return f"{slug}: not uninstalled, not synced: unsynced"

    def flags(self) -> tuple[bool, bool, bool]:
        """What the plan hash covers of it (the review of #367): the states, never the times,
        whose minutes may read differently after a restart."""
        return (bool(self.uninstalled), bool(self.synced), self.counts)

    @staticmethod
    def said(flags: Sequence[bool] | None) -> str:
        """A row's `flags()` in `line`'s words, without its times: what a STALE teardown names
        as changed since its plan (`Runner._diff`, #376). None: the plan doesn't name the row."""
        if flags is None:
            return "not in the plan"
        uninstalled, synced, counts = flags
        if counts:
            return "synced after its uninstall"
        if synced and uninstalled:
            return "synced before its uninstall"
        if synced:
            return "synced while its agents ran, not uninstalled"
        return "uninstalled, not synced since" if uninstalled else "not uninstalled, not synced"


#: How long after an apply that changes termination protection `!teardown` is refused in its
#: workshop, at plan time and again at apply (#378). EC2 showed such a change 157 s late on Oct
#: 15, and enforced it late too: within this, a teardown's dry run may read the old protection,
#: and its `TerminateInstances` meet the old enforcement, so that a VM just protected is
#: terminated. A test pins it.
PROTECT_SETTLE = 300


@dataclass(frozen=True, slots=True)
class Protection:
    """The workshop's last apply that changes termination protection (`fleet.Ledger`): `!protect`,
    or `!launch … protect`. Its plan and verb, and its last record's `ts` (its `apply_start`,
    `apply_end` or `interrupted`) in seconds since the epoch: cut to the second, so the record
    was written up to 1 s after `at`."""

    plan: str
    verb: str
    at: float

    @property
    def until(self) -> float:
        """When `!teardown` may run again: `PROTECT_SETTLE` after the end of the record's second,
        so never early, whenever in that second it was written."""
        return self.at + 1 + PROTECT_SETTLE

    def wait(self, now: float | None) -> str:
        """Why `!teardown` waits, or "" from `until` on. `now` None (unknown) waits: fail closed.
        The time to retry is a whole minute, never early."""
        if now is not None and now >= self.until:
            return ""
        since, retry = _hhmm(self.at), _hhmm(math.ceil(self.until / 60) * 60)
        return (
            f"{self.plan} ({_what(self.verb)}) changed termination protection here at {since} "
            f"UTC, and EC2 can take minutes to show and enforce that: no !teardown or !retire "
            f"for {PROTECT_SETTLE // 60} minutes after it. Request it again from {retry} UTC"
        )


def protect_applying(plan: str, verb: str) -> str:
    """Why `!teardown` waits while `plan`, an apply that changes termination protection, is busy
    in its workshop (`Planning`): its end, and so the time to retry, isn't known yet."""
    return (
        f"{plan} ({_what(verb)}) is changing termination protection here now, and EC2 can take "
        f"minutes to show and enforce that: no !teardown or !retire for {PROTECT_SETTLE // 60} "
        f"minutes after it ends. Request it again {PROTECT_SETTLE // 60} minutes after its result"
    )


def _what(verb: str) -> str:
    return "!protect" if verb == "protect" else f"!{verb} … protect"


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts))


@dataclass(frozen=True, slots=True)
class Row:
    """A roster row as the scripts read it: its slug (`slug_of`), `ip` and `region` cells, and
    its `host` (`ws-<slug>` once launched), with which `launch.py` leaves a row alone;
    `excluded`, its `excluded` cell set by `!roster remove` (#627); `keep_until`, its kept VM's
    end as `!keep-until` wrote it (§11.5, #570)."""

    slug: str
    ip: str = ""
    region: str = ""
    host: str = ""
    excluded: bool = False
    keep_until: str = ""


@dataclass(frozen=True, slots=True)
class Fleet:
    """The bot's own workshop: its admin.toml entry, its participant rows in roster order, the
    available spares of its `spares.json`, the pattern of admin.toml's `release`, what the
    bot records for `!adopt-spare` (§3, action 7), and the server's other workshops, whose
    slugs `!roster import` may not take (§2.1; only their names and roster paths are used).
    For `!adopt-spare`, which must give the new VM what the rest of the fleet has: the release
    of the workshop's last `!rollout`; whether it is held (its last `!provision zulip` had
    `hold`); and its push_config settings, as the grammar's words of `!rollout`
    (`multi-party=off`, `steer=off`, `idle-stop=15`, `lean-shared`, …; `()` for none), kept
    current by the bot (a later `!idle-stop` included); `lean-shared` or `idle-stop=` left out
    is the rollout's default (#704). `restart-codex` is a one-time step.
    `waker`: the workshop's last `!waker` of the whole fleet was `on`, so the new VM is opted
    in too, before its idle stop (#440).
    For `!teardown`: `ending`, each row's `Ending`, and `protection`, the last apply that changed
    termination protection (`fleet.Ledger`); `read_at`, when the bot read the fleet.
    `retired`: each row a `!retire` ended → `P-n at <time>` (#559), which `all` leaves out."""

    workshop: Workshop
    rows: tuple[Row, ...]
    spares: tuple[str, ...]
    release_pattern: re.Pattern[str]
    release: str | None = None
    held: bool | None = None
    others: tuple[Workshop, ...] | None = None
    settings: tuple[str, ...] | None = None
    ending: Mapping[str, Ending] = field(default_factory=dict)
    protection: Protection | None = None
    read_at: float | None = None
    waker: bool = False
    retired: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        rows_ok = all(
            isinstance(r, Row) and isinstance(r.ip, str) and isinstance(r.region, str)
            for r in self.rows
        )
        if not (isinstance(self.workshop, Workshop) and rows_ok):
            raise ActionError("a fleet is a Workshop and its Rows")
        if not isinstance(self.release_pattern, re.Pattern):
            raise ActionError("the release pattern is a compiled pattern")
        try:
            Roster(self.slugs, self.spares)  # the grammar's slug and spare rules, no repeats
        except ValueError as exc:
            raise ActionError(str(exc)) from None
        if self.release is not None and not _version(self.release, self.release_pattern):
            raise ActionError("the recorded release is not a tag this server takes")
        if not (self.held is None or self.held is True or self.held is False):
            raise ActionError("held is unknown, true or false")
        if not (self.waker is True or self.waker is False):
            raise ActionError("waker is true or false")
        mine = (self.workshop.name, self.workshop.roster)
        if self.others is not None and not all(
            isinstance(ws, Workshop) and not {ws.name, ws.roster} & set(mine) for ws in self.others
        ):
            raise ActionError("the other workshops are other Workshops")
        if self.settings is not None:
            _replay(self.settings)
        if not (
            isinstance(self.ending, Mapping)
            and all(isinstance(k, str) and type(v) is Ending for k, v in self.ending.items())
        ):
            raise ActionError("each row's end is an Ending")
        if not (self.protection is None or type(self.protection) is Protection):
            raise ActionError("the last protection change is a Protection")
        if not (self.read_at is None or type(self.read_at) in (int, float)):
            raise ActionError("the fleet's read time is a number")
        if not (
            isinstance(self.retired, Mapping)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in self.retired.items())
        ):
            raise ActionError("each retired row is a slug and when")

    def present(self, slug: str) -> bool:
        """The row is in service: no `!retire` ended it (#559)."""
        return slug not in self.retired

    @property
    def excluded(self) -> frozenset[str]:
        """The rows `!roster remove` excluded (#627)."""
        return frozenset(row.slug for row in self.rows if row.excluded)

    def in_all(self, slug: str) -> bool:
        """`all` names the row: in service, and not excluded."""
        return self.present(slug) and slug not in self.excluded

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(row.slug for row in self.rows)


@dataclass(frozen=True, slots=True)
class Step:
    """One script run: `argv` on apply; `dry_run` in the plan, None when the step has none or
    depends on an earlier step (§3); `env`, what the runner adds to the environment (§2.3).

    `!retire`'s steps (#559) also say: their `row`, and their `stage`, a column of the result
    (`STAGES`); a row's steps stop at its first failure, unless the step is `tolerated`; a `vm`
    step needs the row's VM: for a row without one (`Live.vms`) it is skipped if `gone` (the
    plan says `unsynced`), and fails the row otherwise. A step of no row that is tolerated never
    stops the plan. A `plan_only` step is a read: its dry run runs at plan time, and again at
    apply for the hash, and nothing else."""

    argv: Argv
    dry_run: Argv | None = None
    env: tuple[str, ...] = ()
    row: str = ""
    stage: str = ""
    tolerated: bool = False
    vm: bool = False
    plan_only: bool = False
    gone: bool = False


@dataclass(frozen=True, slots=True)
class Action:
    verb: str
    workshop: str
    bot: int  # the user ID of the workshop's bot: a plan is bound to it (§2.5)
    args: tuple[str, ...]  # `Command.tokens()`, the normalized arguments
    steps: tuple[Step, ...]
    hashed: str  # READ, TARGETS, DRY_RUN, INPUTS or RECONCILER
    targets: tuple[str, ...]  # the slugs, then the spares, acted on
    inputs: tuple[str, ...]  # the files whose SHA-256 the hash covers
    status: Argv | None = None  # the plan's read of the targets' state: shown, never hashed
    notes: tuple[str, ...] = ()  # lines the plan shows under its targets: never hashed
    #: For `!teardown`: each row with a VM, and its `Ending.flags()`; hashed, so that a plan
    #: naming one of them between plan and apply makes the teardown STALE (the review of #367).
    ends: tuple[tuple[str, bool, bool, bool], ...] = ()
    #: The apply's deadline in seconds when it isn't the verb's (`runner.TIMEOUTS`): `!retire`'s
    #: grows with its rows (#559).
    timeout: float | None = None

    @property
    def dry_runs(self) -> tuple[Argv, ...]:
        return tuple(step.dry_run for step in self.steps if step.dry_run)

    @property
    def applied(self) -> tuple[Step, ...]:
        """The steps an apply runs: all but the plan's reads (`Step.plan_only`)."""
        return tuple(step for step in self.steps if not step.plan_only)


@dataclass(frozen=True, slots=True)
class Live:
    """What the bot read at plan time, and again at apply; the verb's hash covers only its part
    (§2.4). `vms`: each target → its VMs' IDs (`provider.targets`); `dry_run`: the dry runs'
    output; `tmpdir`: the plan's TMPDIR. `tagged`, for `!retire`: each target → the VMs tagged
    with its name exactly (`provider.tagged`), which its protection and teardown act on (#559).
    No Zulip lookups (#589): `!roster import`'s and the roster verbs' scripts check Zulip at
    their dry run and again at apply, which refuses what changed since."""

    vms: Mapping[str, str] = field(default_factory=dict)
    dry_run: str = ""
    tmpdir: str = ""
    tagged: Mapping[str, str] = field(default_factory=dict)


def normalize_dry_run(text: str, tmpdir: str) -> str:
    """§2.4's normalization: paths under the plan's TMPDIR (push_config.py's `zab-push-…`
    directories) become `<tmp>`; timestamps and durations go. `--parallel 1` fixes the order."""
    if not tmpdir.strip("/"):
        raise ActionError("a hashed dry run needs the plan's TMPDIR")
    text = re.sub(re.escape(tmpdir.rstrip("/")) + r"(?:/zab-push-[A-Za-z0-9_]+)?", "<tmp>", text)
    return _DURATION.sub("<duration>", _TIMESTAMP.sub("<time>", text))


def normalize_exact(text: str, tmpdir: str) -> str:
    """§2.4 for a dry run that prints no path, time or duration, `!roster sync`'s: its text as it
    is, so that every slug, name and e-mail it lists is hashed (`bob-5s` is never `bob-6s`, as
    `normalize_dry_run`'s durations would make them; review of #641)."""
    return text


#: An instance's state in teardown.py's `terminate <id> <name> (<state>, participant <p>)`.
_STATE = re.compile(r"\((?:pending|running|stopping|stopped), (participant )", re.ASCII)


def normalize_teardown(text: str, tmpdir: str) -> str:
    """§2.4 for `!teardown` (Codex round 1 on #367): its dry run's lines, what it would
    terminate, keep, skip and release, sorted, so that the order EC2 lists them in doesn't
    count; without each instance's state, which idle stop may change between plan and apply
    without changing what is torn down. A VM protected or unprotected since, or an address
    gained or lost, changes the hash: the plan becomes STALE."""
    lines = normalize_dry_run(text, tmpdir).splitlines()
    return "\n".join(sorted(_STATE.sub(r"(\1", line) for line in lines if line.strip()))


class _Job:
    """One command against one fleet, with the workshop's flags, each value checked."""

    def __init__(self, command: Command, fleet: Fleet, layout: Layout, upload: str | None):
        ws = fleet.workshop
        self.command, self.fleet, self.layout, self.upload = command, fleet, layout, upload
        self.plan: str | None = None  # the plan's name, `P-n`, where a verb needs it (#627)
        self.scheduled = False  # a month-end run's `!retire` (`build_run`, #570)
        self.slugs, self.spares = _targets(command, fleet)
        self.notes: list[str] = []
        self.ends: list[tuple[str, bool, bool, bool]] = []
        self.timeout: float | None = None
        out = _value(ws.out_dir)
        self.roster = ("--roster", _value(ws.roster))
        self.out = ("--out-dir", out)
        self.kh = ("--known-hosts", f"{out}/known_hosts")
        self.spares_file = ("--spares-file", f"{out}/spares.json")
        self.prov = ("--zuliprc", _value(layout.provisioner_zuliprc))
        self.org = _value(ws.github_org)
        self.tags = _cat(*(("--tag", _value(f"{key}={value}")) for key, value in ws.tags))
        self.region = ("--region", _value(ws.aws.region))
        self.refserve = ("--refserve-url", _value(ws.refserve_url))
        # "Start here"'s gift step (#709): free text, so base64url, which is in the alphabet
        gifts = ws.gifts_step and base64.urlsafe_b64encode(ws.gifts_step.encode()).decode()
        self.gifts = ("--gifts-step-b64", _value(gifts)) if gifts else ()
        self.mention = ("--mention-after-s", _value(str(ws.mention_after_s)))  # #711

    def run(self, script: str, *parts: str | Argv) -> Argv:
        path = _value(f"{self.layout.release}/workshop/{script}")
        # -B: nothing is written into the release; -u: output as it comes (-E ignores PYTHON*)
        return _cat(_value(self.layout.python), "-E", "-s", "-B", "-u", path, *parts)

    def wheel(self, version: object) -> Argv:
        if not isinstance(version, str) or not _version(version, self.fleet.release_pattern):
            raise ActionError("expected a release tag this server takes")
        path = f"{self.layout.wheels}/zulip_agent_bridge-{version[1:]}-py3-none-any.whl"
        if not os.path.isfile(path):
            raise ActionError(f"no wheel for {version} on this server: deploy.sh --wheel first")
        return ("--wheel", _value(path))

    def flags(self) -> Argv:
        """The command's keywords, then its settings, through their fixed flags."""
        spec, out = self.command.spec, list[str]()
        for word in spec.keywords:
            if word in self.command.keywords:
                out += _lookup(KEYWORD_FLAGS.get(self.command.verb, {}), word)
        for key in spec.settings:
            value = getattr(self.command, _field(key))
            if value is not None:
                out += _lookup(SETTING_FLAGS, key)(value)
        return tuple(out)


def _status(j: _Job, slugs: Argv) -> Argv:
    return j.run("admin/status.py", j.roster, j.kh, j.tags, "--json", _only(slugs))


def _zulip(j: _Job, slugs: Argv, hold: Argv) -> Argv:
    owner = ("--repo-owner", j.org, "--no-rebind", *j.refserve, *j.gifts)
    return j.run("admin/zulip_provision.py", j.roster, j.prov, j.out, owner, hold, _only(slugs))


def _repos(j: _Job, slugs: Argv, flags: Argv) -> Argv:
    org = ("--org", j.org, *j.kh, "--no-rebind")
    return j.run("admin/provision_repos.py", j.roster, j.prov, org, flags, _only(slugs))


def _push(j: _Job, release: Argv, *parts: str | Argv) -> Argv:
    """push_config.py. With a release it writes each VM's config.toml, so it gets the
    workshop's `mention_after_s` (#711) and its `github_org` as the file server's owner
    (#693): `!rollout`'s and `!adopt-spare`'s steps."""
    owner = ("--refserve-owner", j.org) if release else ()
    mention = j.mention if release else ()
    head = (j.roster, release, j.out, j.kh, j.refserve, owner, mention)
    return j.run("admin/push_config.py", *head, *parts)


def _dry(argv: Argv, *extra: str, env: tuple[str, ...] = ()) -> Step:
    return Step(argv, _cat(argv, *extra, "--dry-run"), env)


def _build_status(j: _Job) -> tuple[Step, ...]:
    return (Step(_status(j, j.slugs)),)


def _build_roster_import(j: _Job) -> tuple[Step, ...]:
    """The upload, then what keeps slugs unique on the server (§2.1): the retired slugs and the
    other workshops' rosters, each passed when it exists (a missing one takes no slug, and
    roster_import.py refuses a named file it can't read). They are inputs of the hash too."""
    shape = re.escape(j.layout.runs.rstrip("/")) + r"/P-[1-9][0-9]{0,8}/roster-upload\.csv"
    if not isinstance(j.upload, str) or not re.fullmatch(shape, j.upload, re.ASCII):
        raise ActionError("the uploaded roster is runs/P-<n>/roster-upload.csv")
    if j.fleet.others is None:
        raise ActionError("the server's other workshops are not known: refused")
    retired = ("--retired", _value(j.layout.retired)) if os.path.isfile(j.layout.retired) else ()
    others = [ws for ws in j.fleet.others if os.path.isfile(ws.roster)]
    taken = [("--workshop-roster", _value(f"{ws.name}={ws.roster}")) for ws in others]
    upload = ("--input", _value(j.upload), *j.prov, "--org", j.org)
    argv = j.run("admin/roster_import.py", j.roster, upload, j.tags, j.region, retired, *taken)
    return (_dry(argv, env=_GH),)


def _roster_sync(j: _Job, *mode: str) -> Argv:
    """roster_sync.py (#627), with what keeps slugs unique on the server, as `!roster import`
    (§2.1): the retired slugs and the other workshops' rosters; the bots' keys in out/, the
    provisioner's key and EC2, for what a row holds."""
    if j.fleet.others is None:
        raise ActionError("the server's other workshops are not known: refused")
    retired = ("--retired", _value(j.layout.retired)) if os.path.isfile(j.layout.retired) else ()
    others = [ws for ws in j.fleet.others if os.path.isfile(ws.roster)]
    taken = [("--workshop-roster", _value(f"{ws.name}={ws.roster}")) for ws in others]
    head = (*j.prov, *j.out, *j.region, *j.tags, *retired, *(w for t in taken for w in t))
    return j.run("admin/roster_sync.py", j.roster, head, mode)  # mode: fixed flags, values


def _build_roster_sync(j: _Job) -> tuple[Step, ...]:
    """The workshop's channel's subscribers (`Workshop.channel`): hashed on its dry run, which
    lists every change (§2.4). Each dry run writes that list to `runs/P-n/roster-sync.txt`
    (`--changes`), the apply's recheck last, and the apply writes the roster only if the list
    it computes is the same, so that a join or a departure after the recheck is never applied
    unapproved (Codex round 1 on #641)."""
    channel = j.fleet.workshop.channel
    if channel is None:
        raise ActionError(
            "this workshop has no participants' channel (`channel` in admin.toml): import its "
            "roster with `!roster import` and a CSV, or a deployer records its channel"
        )
    if not isinstance(j.plan, str) or not re.fullmatch(r"P-[1-9][0-9]{0,8}", j.plan, re.ASCII):
        raise ActionError("!roster sync needs its plan's name, for its approved changes")
    changes = ("--changes", _value(f"{j.layout.runs.rstrip('/')}/{j.plan}/roster-sync.txt"))
    return (_dry(_roster_sync(j, "--sync", str(channel), *changes)),)


def _build_roster_edit(j: _Job) -> tuple[Step, ...]:
    """`!roster remove`, `restore` or `rename` of one row (#627): the script refuses one with
    anything provisioned (remove, rename) at its dry run and again at apply, and checks a new
    slug against every roster; the roster and the others' are the hash's inputs."""
    verb, slug = j.command.verb.split()[1], _value(j.command.slug)
    excluded = slug in j.fleet.excluded
    if verb == "remove" and excluded:
        raise ActionError(f"{slug} is excluded already: !roster restore {slug} brings it back")
    if verb == "restore" and not excluded:
        raise ActionError(f"{slug} is not excluded: nothing to restore")
    mode = (
        ("--rename", slug, _value(j.command.new_slug)) if verb == "rename" else (f"--{verb}", slug)
    )
    return (_dry(_roster_sync(j, *mode)),)


def _build_provision_zulip(j: _Job) -> tuple[Step, ...]:
    return (_dry(_zulip(j, j.slugs, j.flags())),)


def _build_provision_repos(j: _Job) -> tuple[Step, ...]:
    return (_dry(_repos(j, j.slugs, j.flags()), "--parallel", "1", env=_GH),)


def _build_launch(j: _Job) -> tuple[Step, ...]:
    """launch.py, then the spares' idle stop (#715), then the rows' host keys: the spares' step
    right after the launch, which it needs (it pins each spare from `spares.json` itself), so
    that no failure of `scan_hosts.py` leaves the new spares running without it (review of
    #729). A failed launch stops the plan before it: the note says to `!start spares` then."""
    aws = j.fleet.workshop.aws
    image = ("--ami", _value(aws.ami), "--security-group-id", _value(aws.security_group))
    kind = ("--key-name", _value(aws.key_name), "--instance-type", _value(aws.instance_types[0]))
    keys = _cat(*(("--admin-key", _value(key)) for key in aws.admin_keys))
    targets = _only(j.slugs + j.spares)
    argv = j.run("aws/launch.py", j.roster, image, kind, j.region, j.tags, keys, j.spares_file)
    scan = j.run("admin/scan_hosts.py", j.roster, j.kh, _only(j.slugs)) if j.slugs else None
    spares = _spare_idle_stop(j)
    if spares:
        j.notes.append(
            "If the launch fails, its spares may run without it: once the cause is fixed, send "
            "`!start spares`, which gives it to them."
        )
    return (
        _dry(_cat(argv, j.flags(), targets)),
        *spares,
        *([Step(scan)] if scan else []),
    )


def _spare_idle_stop(j: _Job, *, started: bool = False) -> tuple[Step, ...]:
    """The spares' own idle stop (#715): `spare_idle_stop.py` with admin.toml's
    `spare_idle_stop` minutes, on each spare the plan names, after its launch or start; the
    plan's note says so. None without spares. No dry run: it needs what the step before does.
    After a start (`started`), `--started`: EC2 may still read a spare it was just asked to
    start as stopped, which the script then waits for rather than skips (Codex round 1 on
    #729)."""
    if not j.spares:
        return ()
    minutes = j.fleet.workshop.spare_idle_stop
    j.notes.append(
        f"**The spares' idle stop:** after {minutes} idle minutes (admin.toml's "
        "`spare_idle_stop`), never before the first boot's volume warm-up has ended; installed "
        "on each spare named once it runs. A stopped one gets it at its next `!start`; "
        "`!adopt-spare` replaces it with the row's."
    )
    head = j.run("aws/spare_idle_stop.py", j.roster, j.spares_file, j.tags, j.region)
    flags = ("--started",) if started else ()
    return (Step(_cat(head, "--minutes", _minutes(minutes), flags, _only(j.spares))),)


def _build_protect(j: _Job) -> tuple[Step, ...]:
    off = () if _flag(j.command.on) else ("--off",)
    return (_dry(j.run("aws/protect.py", j.tags, j.region, off, _only(j.slugs))),)


def _build_rollout(j: _Job) -> tuple[Step, ...]:
    """push_config.py with the release and the command's words, then the targets' status. Each
    of `lean-shared` and `idle-stop=` left out is its default (#704), which the plan's notes say;
    the rows whose waker is on are read from its config (`waker_rows`) and named, so that one
    opted in or out between plan and apply makes the plan STALE."""
    c = j.command
    lean, idle = "lean-shared" not in c.keywords, c.idle_stop is None
    woken = waker_rows(f"{j.layout.waker}/config.toml", j.fleet.workshop.name) if idle else None
    defaults = _defaults(j, lean=lean, idle=idle, woken=woken or {})
    push = _push(j, j.wheel(c.version), j.flags(), defaults, _only(j.slugs))
    j.notes += _default_notes(j, lean=lean, idle=idle, woken=woken)
    return (_dry(push, "--parallel", "1"), Step(_status(j, j.slugs)))


#: What a `!rollout` that leaves out `lean-shared` or `idle-stop=` gives each VM instead (#704):
#: the shared Mathlib where it isn't shared yet, and idle stop at these minutes where the waker
#: is on and no timer is set, unless it was turned off on purpose (push_config.py's
#: `--lean-shared-default`, `--idle-stop-default` and `--has-waker`). `!adopt-spare` gives a new
#: VM the same, where the fleet's recorded settings leave them out.
IDLE_STOP_DEFAULT = 60
_WAKER_CONFIG = 1024 * 1024  # bytes of the waker's config read, as `wakes.MAX_CONFIG`
_INSTANCE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", re.ASCII)  # as push_waker.py's


def waker_rows(path: str, workshop: str) -> dict[str, str] | None:
    """The rows of `workshop` whose waker is on (#704), each with the instance it names: both
    their bots, `codex-<slug>` and `claude-<slug>`, in its entry of the waker's config at `path`
    (`push_waker.py --config-dir`, which `!waker on` and `!waker off` write), with one instance
    ID between them; none without a config. None when it can't be read: then no row counts as
    opted in. The instance is the VM `!waker on` found at the row's address: push_config.py
    turns idle stop on only on that VM (`--has-waker SLUG=INSTANCE`; review of #705), since a
    VM replaced by hand since then is one the waker can't start."""
    try:
        with open(path, "rb") as f:
            raw = f.read(_WAKER_CONFIG + 1)
        if len(raw) > _WAKER_CONFIG:  # never a part of it, read as the whole
            return None
        data = tomllib.loads(raw.decode())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    entries = data.get("workshop")
    bots: dict[str, set[object]] = {}
    for entry in entries if isinstance(entries, list) else ():
        listed = entry.get("bots") if isinstance(entry, dict) else None
        if isinstance(listed, list) and entry.get("name") == workshop:
            for bot in (b for b in listed if isinstance(b, dict)):
                vm = bot.get("instance_id")  # a TOML array or table would be unhashable
                bots.setdefault(str(bot.get("name")), set()).add(
                    vm if isinstance(vm, str) else None
                )
    out = {}
    for name, ids in bots.items():
        slug, vm = name.removeprefix("codex-"), next(iter(ids)) if len(ids) == 1 else None
        valid = isinstance(vm, str) and _INSTANCE.fullmatch(vm)
        if name.startswith("codex-") and valid and bots.get(f"claude-{slug}") == {vm}:
            out[slug] = str(vm)
    return out


def _defaults(j: _Job, *, lean: bool, idle: bool, woken: Mapping[str, str]) -> Argv:
    """push_config.py's flags for the defaults (#704): `lean`, the shared Mathlib; `idle`, idle
    stop at `IDLE_STOP_DEFAULT`, on the targets in `woken` alone, each named with the instance
    the waker names for it, or alone where that is "" (`--has-waker`)."""
    out: Argv = ("--lean-shared-default",) if lean else ()
    if idle:
        out += ("--idle-stop-default", str(IDLE_STOP_DEFAULT))
        named = (f"{s}={woken[s]}" if woken[s] else s for s in j.slugs if s in woken)
        out += _cat(*(("--has-waker", _value(word)) for word in named))
    return out


def _default_notes(
    j: _Job,
    *,
    lean: bool,
    idle: bool,
    woken: Mapping[str, str] | None,
    dry: bool = True,
    again: bool = True,
) -> list[str]:
    """The plan's lines on the defaults it applies (#704), so that the approver sees them
    without knowing them: the targets without the waker are named, with the commands, the
    waker first, then this rollout `again` or `!idle-stop` (`!adopt-spare`'s plan has no
    rollout to send again: review round 3 of #705); and, with a `dry` run, what that shows."""
    out = []
    if lean:
        out.append(
            "**Shared Mathlib (default):** set up as `lean-shared` does on each VM whose Mathlib "
            "isn't shared yet; a VM that has it is only probed, one with no Lean workspace noted."
        )
    if idle:
        m = IDLE_STOP_DEFAULT
        out.append(
            f"**Idle stop: {m} min (default)** where the waker is on for the VM and no timer is "
            "on, unless it was turned off on purpose (`!idle-stop 0`); a timer that is on stays "
            "as it is. Each row's result says what it found."
        )
        missing = [s for s in j.slugs if s not in (woken or {})]
        sel = j.command.selection
        every = sel is not None and sel.all and len(missing) == len(j.slugs)
        names = " ".join(missing) if len(missing) <= 5 else "<row>"
        why = "the waker's config can't be read" if woken is None else "the waker isn't on there"
        rerun = "this rollout again, or " if again else ""
        tail = ", which turns it on even where it was off on purpose" if again else ""
        if missing:  # never `!idle-stop 60 all`, which turns on what was off on purpose too
            out.append(
                f"Not on {_some(missing)}: {why}. The waker first: "
                f"`!waker on {'all' if every else names}`, then {rerun}"
                f"`!idle-stop {m} {names}`{tail}."
            )
    if dry and (lean or idle):
        out.append("The dry run shows every step the defaults may take, as on a new VM.")
    return out


def _build_idle_stop(j: _Job) -> tuple[Step, ...]:
    minutes = ("--idle-stop", _minutes(j.command.idle_stop))
    return (_dry(_push(j, (), minutes, _only(j.slugs)), "--parallel", "1"),)


def _build_power(j: _Job) -> tuple[Step, ...]:
    sel = j.command.selection
    assert sel is not None  # _targets refused a command without one
    if (sel.all or sel.spares) and (sel.slugs or sel.spare_names):
        raise ActionError("power.py takes `all` and `spares`, or names, not both: send two")
    which = ("--all",) * _flag(sel.all) + ("--spares",) * _flag(sel.spares)
    if sel.all and any(r.ip and not j.fleet.in_all(r.slug) for r in j.fleet.rows):
        # power.py's `--all` is every row with an `ip`, a retired one's too (#559; Codex on
        # #565): the rows `all` names that have one, then the spares, each named instead
        launched = [r.slug for r in j.fleet.rows if r.ip and r.slug in j.slugs]
        which = _only(tuple(launched) + j.spares)
    head = j.run("aws/power.py", j.roster, j.spares_file, j.tags, j.region)
    power = _dry(_cat(head, "--" + j.command.verb, which or _only(j.slugs + j.spares)))
    return (power, *(_spare_idle_stop(j, started=True) if j.command.verb == "start" else ()))


def _build_adopt_spare(j: _Job) -> tuple[Step, ...]:
    """The hand-over chain of ORGANIZERS §7.2 (§3, action 7). Later steps depend on the first: no
    dry runs. Both push_config steps replay the fleet's settings: push_config.py's defaults
    (multi-party and steering on, idle stop left as it is) would undo a kill switch on the new VM.
    With the fleet's waker on (`Fleet.waker`), the VM is opted in once its bots exist and before
    either push_config step can install the replayed idle stop: the waker before idle stop, so that
    it never stops without a way to wake (ADMIN §8.4; #440). The first push_config step counts the
    row's checkout, which provision_repos clones next, as pending while it is absent
    (`--adopt-spare`, #458); the last one, without the flag, checks the doctor in full. The
    settings the fleet's record leaves out, `lean-shared` and `idle-stop=`, are the rollout's
    defaults (#704): the shared Mathlib, and idle stop at `IDLE_STOP_DEFAULT` after the waker,
    where the fleet's is on. `mention_after_s` is admin.toml's, as for a rollout (`_push`)."""
    fleet = j.fleet
    if fleet.held is None or fleet.release is None or fleet.settings is None:
        raise ActionError("no !provision zulip or !rollout recorded here yet: run them first")
    slug, spare = _value(j.command.slug), ("--spare", _value(j.command.spare))
    adopt = j.run("aws/adopt_spare.py", j.roster, "--slug", slug, spare, j.region, j.spares_file)
    one, wheel, same = _only((slug,)), j.wheel(fleet.release), _replay(fleet.settings)
    keys = {word.partition("=")[0] for word in fleet.settings}
    lean, idle = "lean-shared" not in keys, "idle-stop" not in keys
    woken = {slug: ""} if fleet.waker else {}  # opted in by the chain, before
    same += _defaults(j, lean=lean, idle=idle, woken=woken)
    j.notes += _default_notes(j, lean=lean, idle=idle, woken=woken, dry=False, again=False)
    refused = f"the fleet's waker is on, and {NO_ALERTS}: !adopt-spare refused"
    waker = (Step(_cat(_waker_on(j, (slug,), refused), "--apply")),) if fleet.waker else ()
    return (
        _dry(_cat(adopt, j.tags)),
        Step(j.run("admin/scan_hosts.py", j.roster, j.kh, one)),
        Step(_zulip(j, (slug,), ("--hold",) if fleet.held else ())),
        *waker,
        Step(_push(j, wheel, same, "--adopt-spare", slug, one)),
        Step(_repos(j, (slug,), ()), env=_GH),
        Step(_push(j, wheel, same, one)),
    )


def _build_grow_disk(j: _Job) -> tuple[Step, ...]:
    """One row's root volume (#553; §3, action 3): `grow_disk.py` finds the VM by its scope and
    `participant` tag, checks it against the roster's `ip`, and refuses what can't grow (a size
    that doesn't, a modification less than 6 hours old, a volume outside the scope); its dry
    run shows the volume, the cost a month and the disk's use. SSH as the other scripts'."""
    size = ("--size", _gigabytes(j.command.size))
    slug = ("--slug", _value(j.command.slug))
    return (_dry(j.run("aws/grow_disk.py", j.roster, j.kh, j.tags, j.region, slug, size)),)


#: How far ahead `!keep-until` may set a kept VM's end (ADMIN §11.5): a month, extended a few
#: times, stays well within it, and a typo of the year doesn't.
KEEP_UNTIL_DAYS = 366


def _build_keep_until(j: _Job) -> tuple[Step, ...]:
    """§11.5 (#570): each named row's `keep_until`, the end of its kept VM, written by
    keep_until.py (`--set S=VALUE`, one per row); `none` clears it. An end must be in the
    future, as the bot's clock read the fleet (`Fleet.read_at`), and at most `KEEP_UNTIL_DAYS`
    ahead: checked at plan time and again at apply (`Planning.rebuild`), so a plan approved
    after its end is STALE. A row a `!retire` ended has no VM left to keep: a date for it is
    refused, and `none` clears it, which reaches no VM (review of #817, N3)."""
    value = j.command.until
    end = keep_until_end(value) if isinstance(value, str) else None
    if value != "none" and end is None:
        raise ActionError("expected a date like 2026-11-30, or none")
    if end is not None:
        now = j.fleet.read_at
        if now is None:  # fail closed: the end can't be checked
            raise ActionError("the bot's clock is not known: refused")
        when = utc_words(end)
        if end <= now:
            raise ActionError(f"{when} has passed: give an end in the future")
        if end > now + KEEP_UNTIL_DAYS * 86400:
            raise ActionError(f"{when} is more than {KEEP_UNTIL_DAYS} days ahead: check the year")
    gone = [s for s in j.slugs if not j.fleet.present(s)]
    if gone and end is not None:
        raise ActionError(
            f"{_some(gone)} retired ({j.fleet.retired[gone[0]]}): its VM is gone, so there is "
            "nothing to keep"
        )
    pairs = _cat(*(("--set", _value(f"{slug}={value}")) for slug in j.slugs))
    if end is None:
        j.notes.append(f"**Not kept until any date:** {_some(j.slugs)}.")
    else:  # how each ends, from the month-end schedule in `state.db`: `Planning._kept` (#868)
        j.notes.append(f"**Kept until {when}:** {_some(j.slugs)}.")
    return (_dry(j.run("admin/keep_until.py", j.roster, pairs)),)


def utc_words(ts: float) -> str:
    """A time as the plans say it: `Monday, November 30, 2026, 15:00 UTC`."""
    t = time.gmtime(ts)
    return time.strftime(f"%A, %B {t.tm_mday}, %Y, %H:%M UTC", t)


#: The schedule's verb (§11.5, #570): `zab.admin.monthend` keeps what its apply stores.
MONTH_END = "month-end schedule"
#: How the schedule's rows end, as its plan says (§11.5, #570).
MONTH_END_HOW = (
    "**At each end,** a run of the bot ends the row with these steps, under the workshop's "
    "lock, and posts its result in this topic. A row it stops is ended by hand, with "
    "`!retire`."
)


def _build_month_end(j: _Job) -> tuple[Step, ...]:
    """§11.5 (#570): the workshop's schedule, every row in service with a `keep_until`
    (`_targets`), each to end at its date with `!retire`'s steps.

    Refused for a row whose end has passed by the bot's clock (`Fleet.read_at`), or is more than
    `KEEP_UNTIL_DAYS` ahead, as `!keep-until` refuses it (a `!roster import` checks only its
    form); and for one without an `ip`, which `!retire` takes only `unsynced`. The plan reads
    the teardown's dry run of every row, which the hash covers with each row's VMs (`RETIRE`),
    as `!retire`'s does, and `runner.vms_found` refuses a row as it does there: no VM found,
    more than one, or VMs found by its address (or a `participant` tag in any case) other than
    those tagged with its slug exactly. A single VM tagged with its slug but away from its
    roster address is flagged in the plan, not refused (review of #824, N4). A recent
    `!protect` doesn't hold the plan back, since it tears nothing down now."""
    now = j.fleet.read_at
    if now is None:  # fail closed: the ends can't be checked
        raise ActionError("the bot's clock is not known: refused")
    rows = {row.slug: row for row in j.fleet.rows}
    ends = {slug: keep_until_end(rows[slug].keep_until) for slug in j.slugs}
    bad = [slug for slug, end in ends.items() if end is None]
    if bad:
        raise ActionError(
            f"{_some(bad)}: its keep_until is not a date like 2026-11-30: set it with !keep-until"
        )
    past = [slug for slug, end in ends.items() if end is not None and end <= now]
    if past:
        raise ActionError(
            f"{_some(past)}: its keep_until has passed: `!retire {past[0]}` ends it, or "
            "`!keep-until` gives it a later date"
        )
    # as `!keep-until`'s: an import checks only the form (review of #817, N1)
    far = [slug for slug, end in ends.items() if end and end > now + KEEP_UNTIL_DAYS * 86400]
    if far:
        raise ActionError(
            f"{_some(far)}: its keep_until is more than {KEEP_UNTIL_DAYS} days ahead: check "
            "the year, and set it again with !keep-until"
        )
    never = [slug for slug in j.slugs if not rows[slug].ip]
    if never:
        raise ActionError(
            f"{_some(never)}: no ip in the roster, so no VM to sync: `!retire {never[0]} "
            f"unsynced` ends it, or `!keep-until none {never[0]}` leaves it out"
        )
    out = j.out[1]
    j.notes.append(
        f"**The month end** of {len(j.slugs)} rows, each at its own end, with `!retire`'s steps: "
        "started, idle stop and the waker off, uninstalled, synced, protection off, then, 5 "
        "minutes later, torn down, and its bots deactivated. A row stops at its first failure: "
        "a row whose sync fails is not torn down."
    )
    for slug in j.slugs:
        kinds = [k for k in BOT_KINDS if os.path.isfile(f"{out}/{slug}/{k}.zuliprc")]
        bots = " and ".join(f"{k.capitalize()} · {slug}" for k in kinds) or "no bots"
        j.notes.append(f"- {slug}: ends {utc_words(ends[slug] or 0)}; {bots}")
    j.notes += [
        "**What approving it approves:** these rows, their ends and those steps. Each run builds "
        "its argument lists with the release then running, and checks its rows again first: a "
        "row whose `keep_until`, VM or bots changed since, or that was retired or excluded, "
        "drops out, and only a new `!month-end schedule` ends it. This replaces the workshop's "
        "schedule, if it has one; `!month-end cancel` takes rows out of it at once.",
        MONTH_END_HOW,
    ]
    head = j.run("aws/teardown.py", j.roster, j.tags, j.region, j.spares_file)
    read = _cat(head, "--dry-run", _only(j.slugs))
    return (Step(read, read, plan_only=True),)


#: Why `!waker on` is refused without the workshop's alerts channel (§3, action 5).
NO_ALERTS = "this workshop has no alerts_channel in admin.toml"


def _waker_head(j: _Job) -> Argv:
    ws = j.fleet.workshop
    return ("--config-dir", _value(j.layout.waker), "--workshop", _value(ws.name), *j.roster)


def _waker_on(j: _Job, slugs: Argv, refused: str = f"{NO_ALERTS}: !waker on refused") -> Argv:
    """push_waker.py --config-dir's opt-in of `slugs`, without `--apply`: each VM looked up in
    the workshop's scope and region, its bots' keys from `out/`, alerts to the workshop's
    `alerts_channel`; without one, refused with `refused`."""
    ws = j.fleet.workshop
    if ws.alerts_channel is None:
        raise ActionError(refused)
    alerts = ("--organizers-channel", _value(str(ws.alerts_channel)))
    keys = ("--keys", _value(ws.out_dir), *j.region, *j.tags)
    return j.run("admin/push_waker.py", _waker_head(j), keys, alerts, _only(slugs))


def _build_waker(j: _Job) -> tuple[Step, ...]:
    """push_waker.py --config-dir (§3, action 5): its run without `--apply` is the dry run. On:
    `_waker_on`. Off: `--remove`, which makes no AWS call."""
    if not _flag(j.command.on):
        argv = j.run("admin/push_waker.py", _waker_head(j), "--remove", _only(j.slugs))
    else:
        argv = _waker_on(j, j.slugs)
    return (Step(_cat(argv, "--apply"), argv),)


def _build_final_sync(j: _Job) -> tuple[Step, ...]:
    """workshop/admin/README.md §7.1: each row's work to its repository; the JSON report goes to
    out/. `--strict`: a row with a VM that the script would skip fails the run, so that its exit 0,
    which marks the rows synced (`fleet.Ledger`), means each was synced (review of #367). With
    the GitHub token, which looks up each row's repository first: one the participant deleted
    or transferred is reported, and not synced (#714)."""
    argv = j.run("admin/final_sync.py", j.roster, j.kh, j.out, "--strict", _only(j.slugs))
    return (_dry(argv, env=_GH),)


def _build_uninstall(j: _Job) -> tuple[Step, ...]:
    return (_dry(_push(j, (), "--uninstall", _only(j.slugs)), "--parallel", "1"),)


def _build_teardown(j: _Job) -> tuple[Step, ...]:
    """§3, action 7. Every participant row named needs a sync that counts (`Ending.counts`: a
    successful `!final-sync` after its `!uninstall`, as decided on #367),
    unless the command says `unsynced` (and the phrase then says it too,
    `approvals.requirement`); the plan lists each row's state (`Action.notes`). A row without an
    `ip` too: teardown.py finds a VM by its participant tag, and a launch cut short may have
    left one that the roster doesn't record (Codex round 2 on #367); such a row can't be
    uninstalled or synced, so it needs `unsynced`. Spares hold no participant's work.
    teardown.py itself keeps a protected VM (`keep`) and releases each terminated VM's Elastic
    IP. The dry run never has `--yes`. Refused, after the rows' check, which lasts, within
    `PROTECT_SETTLE` of the workshop's last apply that changed termination protection
    (`Protection.wait`, #378); `Planning.rebuild` builds again at apply, so an approval within
    it makes the plan STALE."""
    launched = list(j.slugs)  # every participant row named (spares apart)
    recorded = {row.slug for row in j.fleet.rows if row.ip}
    ends = {s: j.fleet.ending.get(s, Ending()) for s in launched}
    missing = [s for s in launched if not ends[s].counts]
    if missing and "unsynced" not in j.command.keywords:
        more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        raise ActionError(
            f"no !final-sync after its !uninstall on record for {', '.join(missing[:5])}{more}: "
            "run !uninstall, then !final-sync, or add `unsynced` to tear down their work"
        )
    if j.fleet.protection is not None and (why := j.fleet.protection.wait(j.fleet.read_at)):
        raise ActionError(why)
    j.ends = [(s, *ends[s].flags()) for s in launched]  # and hashed (`plan_hash`)
    if launched:  # each row's state, shown in the plan (decided on #367)
        j.notes += [f"**Participant rows** ({len(launched)}), {len(missing)} unsynced:"]
        j.notes += [
            f"- {ends[s].line(s)}"
            if s in recorded or ends[s] != Ending()
            else f"- {s}: no VM in the roster (one with its tag goes too), not synced: unsynced"
            for s in launched
        ]
    # --roster: the rows whose addresses it releases lose their `ip` (#604); --spares-file:
    # the spares.json entries at them go (#607)
    head = j.run("aws/teardown.py", j.roster, j.tags, j.region, j.spares_file)
    head = _cat(head, _only(j.slugs + j.spares))
    return (Step(_cat(head, "--yes"), _cat(head, "--dry-run")),)


#: `!retire`'s stages for each row, in order (#559): the separate verbs' steps, run for that
#: row alone; each is a column of the result. `export` only with the keyword.
STAGES = ("start", "idle-stop", "waker", "uninstall", "final-sync", "export", "protect",
          "teardown", "bots")  # fmt: skip
#: `!retire`'s apply deadline: per row, at least `RETIRE_FLOOR_S` (one row's worst case: the
#: start's wait, a 30-minute sync, the protection's read-back and the teardown's retries), and
#: per row with `export` (collect_exports.py's own limits: 20 minutes to export, 20 to fetch, 15
#: to check, on one VM); and the rows `export` takes at most in one plan, so that the deadline
#: stays within a few hours (ORGANIZERS §5.3).
RETIRE_ROW_S, RETIRE_FLOOR_S, EXPORT_ROW_S, EXPORT_ROWS = 1200.0, 3600.0, 3300.0, 3


def _build_retire(j: _Job) -> tuple[Step, ...]:
    """#559: one plan that ends the named rows. Each row in turn, alone (`--only`), runs the
    separate verbs' steps (`STAGES`): its VM started and waited for, idle stop off, the waker
    off, `!uninstall`, `!final-sync`, `!export` with `export`, its termination protection off
    (`protect.py --off` reads it back until EC2 shows it), `!teardown` with `--strict` (a VM
    EC2 still reads as protected fails the row), and `!deactivate-bots`. A row stops at its
    first failure, the others go on. `unsynced` tolerates a failure of the steps that save the
    row's work (start to export), and lets a row with no VM (gone, or never launched) be
    retired: its bots deactivated, anything with its tag torn down. Before the rows, one
    `power.py --start` (no wait) of every row with an `ip`, so that their VMs boot together.
    The plan reads the teardown's dry run of every row, which the hash covers with each row's
    VMs (`RETIRE`). Named rows only, never `all` (the grammar's too); a row with no `ip` needs
    `unsynced`, as for `!teardown`; `export` takes at most `EXPORT_ROWS` rows. Refused as
    `!teardown` is within `PROTECT_SETTLE` of a protection change (#378)."""
    sel, words = j.command.selection, j.command.keywords
    unsynced, export = "unsynced" in words, "export" in words
    if sel is None or sel.all or sel.spares or j.spares:
        raise ActionError("!retire names the rows that end, never `all` or the spares")
    ips = {row.slug: row.ip for row in j.fleet.rows}
    never = [s for s in j.slugs if not ips.get(s)]
    if never and not unsynced and not j.scheduled:  # a run's resumed row: its teardown cleared it
        raise ActionError(
            f"{_some(never)} never launched, or torn down already (no ip in the roster): nothing "
            "to uninstall or sync; add `unsynced` to retire it (its bots, and any VM with its "
            "tag, go)"
        )
    if export and len(j.slugs) > EXPORT_ROWS:
        raise ActionError(
            f"`export` takes at most {EXPORT_ROWS} rows in one !retire (one VM's export may take "
            "55 minutes): retire the rows to export in plans of their own"
        )
    # a month-end run waits that out instead (§11.5: its refusal is a wait)
    if (
        not j.scheduled
        and j.fleet.protection is not None
        and (why := j.fleet.protection.wait(j.fleet.read_at))
    ):
        raise ActionError(why)
    out = j.out[1]
    # --roster and --spares-file: as `!teardown`'s (#604, #607)
    head = j.run("aws/teardown.py", j.roster, j.tags, j.region, j.spares_file)
    read = _cat(head, "--dry-run", _only(j.slugs))
    steps = [Step(read, read, plan_only=True)]
    power = j.run("aws/power.py", j.roster, j.spares_file, j.tags, j.region, "--start")
    launched = tuple(s for s in j.slugs if ips.get(s))
    steps += [Step(_cat(power, _only(launched)), tolerated=True)] if launched else []
    bots = j.run("admin/zulip_provision.py", j.roster, j.prov, j.out, "--deactivate")
    exports = ("--out-dir", _value(f"{out}/exports"))
    for slug in j.slugs:
        one = _only((slug,))
        work = [
            ("start", _cat(power, "--wait", one)),
            ("idle-stop", _push(j, (), ("--idle-stop", "0"), one)),
            ("uninstall", _push(j, (), "--uninstall", one)),
            ("final-sync", j.run("admin/final_sync.py", j.roster, j.kh, j.out, "--strict", one)),
            *([("export", j.run("admin/collect_exports.py", j.roster, j.kh, exports, one))]
              * export),
        ]  # fmt: skip
        waker = _cat(j.run("admin/push_waker.py", _waker_head(j), "--remove", one), "--apply")
        # the sync looks up the row's repository with the token first (#714)
        mine = [Step(argv, row=slug, stage=stage, tolerated=unsynced, vm=True, gone=unsynced,
                     env=_GH if stage == "final-sync" else ()) for stage, argv in work]  # fmt: skip
        mine.insert(2, Step(waker, row=slug, stage="waker"))
        steps += [
            *mine,
            # a failed protection stops a row with a VM, `unsynced` or not (review of #564)
            Step(j.run("aws/protect.py", j.tags, j.region, "--off", one), row=slug,
                 stage="protect", vm=True, gone=unsynced),
            Step(_cat(head, "--yes", "--strict", one), row=slug, stage="teardown"),
            Step(_cat(bots, one), row=slug, stage="bots"),
        ]  # fmt: skip
    j.timeout = max(RETIRE_FLOOR_S, len(j.slugs) * RETIRE_ROW_S) + export * len(j.slugs) * (
        EXPORT_ROW_S
    )
    j.timeout += PROTECT_SETTLE * j.scheduled  # a run's wait before its last teardown
    j.notes += _retire_notes(j, ips, unsynced=unsynced, export=export)
    return tuple(steps)


def _retire_notes(j: _Job, ips: Mapping[str, str], *, unsynced: bool, export: bool) -> list[str]:
    """What the plan shows of each row (#559): its address, its bots, and what becomes of its
    work; then the reminder, before the participants' agents stop answering."""
    out, n = j.out[1], len(j.slugs)
    then = ", exported" * export
    lines = [
        f"**Rows that end** ({n}), one after the other: started, idle stop and the waker off, "
        f"uninstalled, synced{then}, protection off, torn down, bots deactivated. A row stops "
        "at its first failure" + (
            "; with `unsynced`, a failed start, uninstall, sync or export doesn't stop it, and "
            "a row with no VM is retired all the same" if unsynced
            else ", and a row whose sync fails is not torn down"
        ) + ". A VM the dry run lists `keep` is protected (kept): its protection is turned off "
        "first.",
    ]  # fmt: skip
    for slug in j.slugs:
        kinds = [k for k in BOT_KINDS if os.path.isfile(f"{out}/{slug}/{k}.zuliprc")]
        bots = " and ".join(f"{k.capitalize()} · {slug}" for k in kinds) or "no bots"
        where = f"VM at {ips[slug]}" if ips.get(slug) else "no ip in the roster"
        fate = (
            "no VM to sync: torn down unsynced (a VM with its tag goes too)" if not ips.get(slug)
            else f"synced{then}, then torn down" + " (unsynced if its sync fails)" * unsynced
        )  # fmt: skip
        when = j.fleet.retired.get(slug)
        retired = f"; retired already, {when}" if when else ""
        lines.append(f"- {slug}: {where}; {bots}; {fate}{retired}")
    if unsynced:  # it covers every row named (review of #564)
        lines.append(
            "**`unsynced`** covers every row of this plan: a row whose sync fails is torn down "
            "with its work. Retire the rows that need it in a plan of their own."
        )
    lines.append(
        "**Before you approve:** have these participants sign their VM out of Codex and Claude "
        "(their agents can run `codex logout` and `claude auth logout`): after the uninstall, "
        "no agent answers."
    )
    return lines


#: The verbs that reach a row's VM, by its roster address or its tag: refused for a row a
#: `!retire` ended, which only `!launch`, `!adopt-spare`, `!retire`, `!teardown` (by its tag,
#: harmless), `!status` (a read), `!deactivate-bots` and `!provision zulip` still name (#559).
REACH_VM = frozenset({"start", "stop", "rollout", "idle-stop", "uninstall", "final-sync",
                      "export", "protect", "provision repos", "grow-disk"})  # fmt: skip


#: What brings a retired row back into service (round 2 of #565): `launch.py` leaves a row alone
#: whose `host` is recorded, and `adopt_spare.py` refuses one with a `host` or an `ip`. Its bots
#: stay deactivated until their owner, or an organization administrator, reactivates them (Zulip's
#: `can_admin_user`; #762), which no verb does, and `!provision zulip` can't make new ones, whose
#: e-mail addresses the old ones keep (#567). Their owner is the provisioner, since
#: `zulip_provision.py --deactivate` deactivates no other bot, so its key on the server does it.
#: Seven retired rows of 32 characters still fit `planning.NOTE_CHARS`, as a STALE too.
BACK = (
    "To bring it back, a deployer, as zab-admin on the server, clears its host and ip in "
    "roster.csv and reactivates its bots with the provisioner's key (only their owner or a Zulip "
    "administrator can; ADMIN §3.3); then !launch or !adopt-spare it"
)


def _reaches_vm(command: Command) -> bool:
    """`!waker on` looks each VM up by its address; `!waker off` makes no AWS call."""
    return command.verb in REACH_VM or (command.verb == "waker" and command.on is True)


def _some(slugs: Sequence[str]) -> str:
    more = f" and {len(slugs) - 5} more" if len(slugs) > 5 else ""
    return ", ".join(slugs[:5]) + more


def _build_deactivate_bots(j: _Job) -> tuple[Step, ...]:
    """workshop/admin/README.md §7.3: `Claude · <slug>` and `Codex · <slug>`, by name; slugs are
    unique on the server (§2.1), so no other workshop's bot has one of these names."""
    argv = j.run("admin/zulip_provision.py", j.roster, j.prov, j.out, "--deactivate")
    return (_dry(_cat(argv, _only(j.slugs))),)


def _build_export(j: _Job) -> tuple[Step, ...]:
    """workshop/admin/README.md §7.2, steps 1 to 4: the archives go to the workshop's
    `out/exports/`, 0600."""
    exports = ("--out-dir", _value(f"{j.out[1]}/exports"))
    return (_dry(j.run("admin/collect_exports.py", j.roster, j.kh, exports, _only(j.slugs))),)


def _server_verb(j: _Job) -> tuple[Step, ...]:
    raise ActionError(f"!{j.command.verb} is the server's: it acts on no workshop's fleet")


@dataclass(frozen=True, slots=True)
class Verb:
    build: Callable[[_Job], tuple[Step, ...]]
    hashed: str
    region: bool = False  # a one-region script: a row recorded in another region is refused
    status: bool = False  # the plan also reads the targets' status (§2.4, step 2)
    launched: bool = False  # `all` is the rows with a VM (an `ip`): the script refuses others
    normalize: Callable[[str, str], str] | None = None  # DRY_RUN: its output's normalizer
    nothing: bool = False  # its one step's dry run may exit `NOTHING`: nothing to change (#708)
    nothing_flag: str = ""  # with `nothing`: only a dry run that passes this flag may say so


#: Every verb that runs a script (§3): missing here means refused, never guessed.
VERBS: dict[str, Verb] = {
    "status": Verb(_build_status, READ),
    # exit 3 (`NOTHING`): an upload that changes no row, so no plan (#708)
    "roster import": Verb(_build_roster_import, INPUTS, nothing=True),
    # #627: the channel's subscribers, hashed on its dry run (every addition, rename, removal
    # and exclusion); exit 3 (`NOTHING`): nothing to change, so no plan (#708)
    "roster sync": Verb(_build_roster_sync, DRY_RUN, normalize=normalize_exact, nothing=True),
    # one row each: the roster and the others' are the inputs; the script checks Zulip and EC2
    # again at apply (#589's rule for `!roster import`)
    "roster remove": Verb(_build_roster_edit, INPUTS),
    "roster restore": Verb(_build_roster_edit, INPUTS),
    "roster rename": Verb(_build_roster_edit, INPUTS),
    "provision zulip": Verb(_build_provision_zulip, RECONCILER),
    # exit 3 (`NOTHING`) from `invites-only`'s dry run alone: nothing to change (#708)
    "provision repos": Verb(
        _build_provision_repos, RECONCILER, nothing=True, nothing_flag="--invites-only"
    ),
    "launch": Verb(_build_launch, TARGETS, region=True),
    "protect": Verb(_build_protect, TARGETS, region=True, status=True, launched=True),
    "rollout": Verb(_build_rollout, DRY_RUN, status=True, normalize=normalize_dry_run),
    "idle-stop": Verb(_build_idle_stop, DRY_RUN, status=True, normalize=normalize_dry_run),
    # No --wait: !start returns once EC2 has the requests. --wait has one deadline for all VMs
    # since #334 (10 minutes and a round, under the 15-minute timeout); the month-end's per-row
    # `power.py --start --only <slug> --wait` uses it (§11.5).
    "start": Verb(_build_power, TARGETS, region=True, status=True),
    "stop": Verb(_build_power, TARGETS, region=True, status=True),
    "adopt-spare": Verb(_build_adopt_spare, TARGETS, region=True),
    # its target material, the row's VMs: one replaced between plan and apply is STALE; the
    # volume's size and modifications are read again by the script at apply
    "grow-disk": Verb(_build_grow_disk, TARGETS, region=True),
    # §11.5 (#570): the roster is its input; exit 3 (`NOTHING`): no row changes, so no plan
    "keep-until": Verb(_build_keep_until, INPUTS, nothing=True),
    # §11.5 (#570): hashed as !retire's, on each row's VMs and the teardown's dry run, with the
    # roster (so the ends) and the rows' bot keys among its inputs
    MONTH_END: Verb(
        _build_month_end, RETIRE, region=True, status=True, normalize=normalize_teardown
    ),
    # Hashed on its dry run: the workshop's waker entry as it will be written (VMs, allowlists,
    # keys). push_waker.py --config-dir prints only this workshop's entry (§2.1).
    "waker": Verb(_build_waker, DRY_RUN, region=True, launched=True, normalize=normalize_dry_run),
    # stage 3 (§3, action 7). `all` is the rows with a VM where a row without an `ip` has
    # nothing to act on; teardown.py also finds a VM by its participant tag alone
    "final-sync": Verb(_build_final_sync, RECONCILER, status=True, launched=True),
    "export": Verb(_build_export, RECONCILER, launched=True),
    "uninstall": Verb(
        _build_uninstall, DRY_RUN, status=True, launched=True, normalize=normalize_dry_run
    ),
    # what it terminates, keeps and releases, recomputed at apply: not only the instance IDs
    "teardown": Verb(_build_teardown, DRY_RUN, region=True, normalize=normalize_teardown),
    # its target material: each row's bot zuliprcs in out/, among the inputs (§2.4)
    "deactivate-bots": Verb(_build_deactivate_bots, INPUTS),
    # #559: hashed on each row's VMs and on the teardown's dry run, as !teardown's
    "retire": Verb(_build_retire, RETIRE, region=True, status=True, normalize=normalize_teardown),
    # the server's (§2.9): `zab.admin.newworkshop` builds it, from admin.toml, with no fleet;
    # hashed on its dry run, which names the entry, the people and the region's setup
    "new-workshop": Verb(_server_verb, DRY_RUN, normalize=normalize_dry_run),
    # #569, §1.6: `zab.admin.upgradeserver` builds it; hashed on its dry run whole, the plan
    # helper's text (both tags, the reviewed SHA-256, the privileged diff), which names no time
    "upgrade-server": Verb(_server_verb, DRY_RUN, normalize=normalize_exact),
}
#: The verbs that may name an excluded row (#627): the read, and the roster's own verbs.
KEEP_EXCLUDED = frozenset({"status", "roster remove", "roster restore", "roster rename"})
#: The verbs the bot answers itself, with no script (§2.2).
BOT_VERBS = frozenset(
    {"help", "version", "plans", "plan", "audit", "console", "roster show", "approve", "reject"}
    | {"month-end show", "month-end cancel"}  # the schedule's reading and its veto (#570)
)
#: Bare keywords → their fixed flags (§2.3).
KEYWORD_FLAGS: dict[str, dict[str, Argv]] = {
    "provision zulip": {"hold": ("--hold",)},
    "provision repos": {"invites-only": ("--invites-only",), "no-invite": ("--no-invite",)},
    "launch": {"protect": ("--protect",)},
    "rollout": {"lean-shared": ("--lean-shared",), "restart-codex": ("--restart-codex",)},
    "teardown": {"unsynced": ()},  # the bot's own check: no flag (_build_teardown)
    "retire": {"export": (), "unsynced": ()},  # which steps it has (_build_retire): no flag
}
#: `key=value` settings → flags. An absent one passes nothing: steering is on by default
#: (#318, D25), multi-party too, and raw logs are off.
SETTING_FLAGS: dict[str, Callable[[object], Argv]] = {
    "steer": lambda v: ("--claude-steer",) if _flag(v) else ("--no-claude-steer",),
    "multi-party": lambda v: ("--multi-party",) if _flag(v) else ("--no-multi-party",),
    "raw-log": lambda v: ("--claude-raw-log", _lookup({"off": "off", "meta": "meta"}, v)),
    "idle-stop": lambda v: ("--idle-stop", _minutes(v)),
    "spares": lambda v: ("--spares", _count(v)),
}
#: A recorded `!rollout` setting's word → its value, which `SETTING_FLAGS` checks and maps.
_RECORDED: dict[str, Callable[[str], object]] = {
    "steer": lambda w: {"on": True, "off": False}.get(w),
    "multi-party": lambda w: {"on": True, "off": False}.get(w),
    "raw-log": lambda w: w,
    "idle-stop": lambda w: int(w) if w.isascii() and w.isdigit() and len(w) <= 3 else None,
}


def _replay(words: object) -> Argv:
    """The push_config flags of a fleet's recorded settings (`Fleet.settings`): each word is
    one of `!rollout`'s keywords other than `restart-codex`, or one of its `key=value`
    settings, at most once, mapped through the same tables as `!rollout` itself."""
    if not isinstance(words, tuple) or not all(isinstance(word, str) for word in words):
        raise ActionError("the recorded settings are a tuple of words")
    out, keys = list[str](), [word.partition("=")[0] for word in words]
    for word, key in zip(words, keys, strict=True):
        if keys.count(key) > 1 or key == "restart-codex":
            raise ActionError("a recorded setting twice, or one that is not replayed")
        if key in _RECORDED:  # a bare `steer` has no value, and its parser refuses it
            out += SETTING_FLAGS[key](_RECORDED[key](word.partition("=")[2]))
        else:
            out += _lookup(KEYWORD_FLAGS["rollout"], word)
    return tuple(out)


def build(
    command: Command,
    fleet: Fleet,
    *,
    layout: Layout = LAYOUT,
    upload: str | None = None,
    plan: str | None = None,
    scheduled: bool = False,
) -> Action:
    """The action for `command` against the bot's own `fleet`; `upload` is where the bot saved
    the CSV of `!roster import`; `plan`, the plan's name (`P-n`), for `!roster sync`'s file of
    approved changes in `runs/P-n/`; `scheduled`, a month-end run's `!retire` (`build_run`).
    Raises `ActionError` on anything but an exact mapping."""
    name = command.verb if isinstance(command, Command) else None
    verb = VERBS.get(name) if isinstance(name, str) else None
    if verb is None or not isinstance(fleet, Fleet):
        shown = name if isinstance(name, str) and name in SPECS else "it"
        raise ActionError(f"no script is mapped for {shown}: refused")
    _check_fields(command)
    if upload is not None and name != "roster import":
        raise ActionError(f"!{name} takes no upload")
    ws = fleet.workshop
    if ("workshop", ws.name) not in ws.tags or SCOPE_KEY not in dict(ws.tags):
        raise ActionError("the workshop's scope lacks its workshop tag or the fleet's zab-scope")
    job = _Job(command, fleet, layout, upload)
    job.plan, job.scheduled = plan, scheduled is True and name == "retire"
    # `!waker off` (`push_waker.py --remove`) makes no AWS call: `all` is every row, so that a
    # row whose `ip` was cleared still leaves the waker (review of #362)
    launched = verb.launched and not (name == "waker" and command.on is False)
    if launched and command.selection and command.selection.all:
        job.slugs = tuple(r.slug for r in fleet.rows if r.ip and fleet.in_all(r.slug))
    if not (job.slugs or job.spares or name in ("roster import", "roster sync")):
        raise ActionError(f"!{name}: nothing selected, so nothing runs")
    out = [s for s in job.slugs if s in fleet.excluded] if name not in KEEP_EXCLUDED else []
    if out:  # nothing is provisioned for it (#627): not until it is restored
        raise ActionError(
            f"{_some(out)} excluded from the roster (!roster remove): !roster restore {out[0]} "
            "first"
        )
    gone = [s for s in job.slugs if not fleet.present(s)] if _reaches_vm(command) else []
    if gone:  # named: its address may be another VM's now (review of #565)
        raise ActionError(
            f"{_some(gone)} retired ({fleet.retired[gone[0]]}): its VM is gone, and its address "
            f"may now be another VM's. {BACK}"
        )
    # the ledger's retired mark, and a `host`, which launch.py leaves alone, or an `ip`, which
    # adopt_spare.py refuses; its teardown cleared the ip and kept the host (#604)
    kept = {r.slug for r in fleet.rows if r.ip or r.host}
    back = [s for s in job.slugs if not fleet.present(s) and s in kept]
    if name in ("launch", "adopt-spare") and back:  # it would launch nothing (round 2 of #565)
        raise ActionError(
            f"{_some(back)} retired ({fleet.retired[back[0]]}), and the roster keeps its host "
            f"or ip, so launch.py would leave it alone and adopt_spare.py refuse it. {BACK}"
        )
    here = ws.aws.region  # a VM's row without a region reads as us-west-2, as in power.py
    if verb.region and any(  # and status.py; a row without a VM gets this region at launch
        (r.region or ("us-west-2" if r.ip else here)) != here
        for r in fleet.rows
        if r.slug in job.slugs
    ):
        raise ActionError(f"a selected row's VM is outside this workshop's region {here}")
    steps = verb.build(job)
    for argv in [a for step in steps for a in (step.argv, step.dry_run) if a]:
        if not all(isinstance(word, str) and _WORD.fullmatch(word) for word in argv):
            raise ActionError("an argument outside the safe alphabet: refused")
    files = [ws.roster, *([upload] if upload else [])]
    if name.startswith("roster "):  # missing ones too: a file that appears makes it stale
        files += [layout.retired, *(other.roster for other in fleet.others or ())]
    if name in ("deactivate-bots", "retire", MONTH_END):  # the bots: a key made since is stale
        files += [f"{ws.out_dir}/{s}/{kind}.zuliprc" for s in job.slugs for kind in BOT_KINDS]
    files += [w for step in steps for w in step.argv if w.endswith((".whl", "/spares.json"))]
    return Action(
        verb=name,
        workshop=ws.name,
        bot=ws.bot.user_id,
        args=command.tokens(),
        steps=steps,
        hashed=verb.hashed,
        targets=job.slugs + job.spares,
        inputs=tuple(dict.fromkeys(files)),
        status=_status(job, job.slugs) if verb.status and job.slugs else None,
        notes=tuple(job.notes),
        ends=tuple(job.ends),
        timeout=job.timeout,
    )


def build_run(fleet: Fleet, slugs: Sequence[str], *, layout: Layout = LAYOUT) -> Action:
    """A month-end run's action (§11.5, #570): `!retire`'s steps for `slugs`, each argument list
    built with the release running now. Without two of `!retire`'s refusals: a row without an
    `ip` (one resumed past its teardown, which cleared it, #604), whose VM steps are done, and a
    recent `!protect`, which the run waits out (§11.5: that refusal is a wait). Its apply
    deadline has `PROTECT_SETTLE` more, for its wait before a teardown."""
    command = Command("retire", selection=Selection(slugs=tuple(slugs)))
    return build(command, fleet, layout=layout, scheduled=True)


def digests(action: Action) -> dict[str, str]:
    """Each input file's SHA-256, or "" for one that doesn't exist (yet)."""
    out = {}
    for path in action.inputs:
        try:
            with open(path, "rb") as f:
                out[path] = hashlib.file_digest(f, "sha256").hexdigest()
        except FileNotFoundError:
            out[path] = ""
    return out


def target_material(action: Action, live: Live) -> dict[str, object]:
    """The verb's part of the hash (§2.4). Always the workshop and its bot. A reconciler adds
    nothing: its dry run shows live state, which may change before apply without harm."""
    out: dict[str, object] = {"workshop": action.workshop, "bot": action.bot}
    if action.hashed == TARGETS:
        out["vms"] = [[name, live.vms.get(name)] for name in action.targets]
    elif action.hashed == DRY_RUN:
        normalize = VERBS[action.verb].normalize
        assert normalize is not None  # a test checks that every DRY_RUN verb has one
        out["dry_run"] = normalize(live.dry_run, live.tmpdir)
    elif action.hashed == RETIRE:  # each row's VMs, and what the teardown would do (#559)
        out["vms"] = [[name, live.vms.get(name)] for name in action.targets]
        out["tagged"] = [[name, live.tagged.get(name)] for name in action.targets]
        out["dry_run"] = normalize_teardown(live.dry_run, live.tmpdir)
    elif action.hashed not in (INPUTS, RECONCILER):
        raise ActionError(f"!{action.verb} is a read: it has no plan")
    return out


def plan_hash(action: Action, *, release: str, digests: Mapping[str, str], live: Live) -> str:
    """SHA-256 of the canonical JSON of a plan (§2.4, step 3): the server's release, the verb
    and its arguments, every step, the inputs' digests, the target material, and, for a
    teardown, each row's end of the workshop (`Action.ends`)."""
    if any(path not in digests for path in action.inputs):
        raise ActionError("an input file was not hashed")
    material = {
        "release": release,
        "verb": action.verb,
        "args": action.args,
        "steps": [[step.argv, step.dry_run, step.env] for step in action.steps],
        "inputs": [[path, digests[path]] for path in action.inputs],
        "targets": target_material(action, live),
        "ends": [list(end) for end in action.ends],
    }
    text = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _targets(command: Command, fleet: Fleet) -> tuple[Argv, Argv]:
    """The slugs and spares acted on, in roster and spares order, each one of this fleet's."""
    if command.verb in ("roster import", "roster sync"):
        return (), ()
    if command.verb == MONTH_END:  # every row in service with an end (§11.5, #570)
        kept = tuple(r.slug for r in fleet.rows if r.keep_until and fleet.in_all(r.slug))
        if not kept:
            raise ActionError(
                "no row in service has a keep_until: give the kept rows their end with "
                "!keep-until first"
            )
        return kept, ()
    if command.verb in ("roster remove", "roster restore", "roster rename"):
        if command.slug not in fleet.slugs:
            raise ActionError("a slug that is not in this workshop's roster: refused")
        return (command.slug,), ()
    if command.verb == "adopt-spare":
        slugs, spares = (command.slug,), (command.spare,)
    elif command.verb == "grow-disk":
        slugs, spares = (command.slug,), ()
    else:
        sel = command.selection
        if not isinstance(sel, Selection):
            raise ActionError(f"!{command.verb} needs a selection")
        every = _flag(sel.all)  # a truthy "false" is not `all`
        if (_flag(sel.spares) or sel.spare_names) and not command.spec.spares:
            raise ActionError(f"!{command.verb} takes no spares")
        try:  # the grammar's own reading: `all` or `spares` that finds nothing, a stray name
            chosen = sel.targets(Roster(fleet.slugs, fleet.spares))
        except (TypeError, ValueError) as exc:
            raise ActionError(f"!{command.verb}: {exc}") from None
        people = len(fleet.slugs) if every else len(sel.slugs)
        slugs, spares = chosen[:people], chosen[people:]
        if every:  # never a row a !retire ended (#559), nor one excluded (#627)
            slugs = tuple(slug for slug in slugs if fleet.in_all(slug))
        if command.verb == "launch":  # zab-spare-1..N, within --spares N (launch.py --only)
            count = 0 if command.spares is None else int(_count(command.spares))
            spares = tuple(f"zab-spare-{n}" for n in range(1, count + 1))
    if not all(slug in fleet.slugs for slug in slugs):
        raise ActionError("a slug that is not in this workshop's roster: refused")
    if command.verb != "launch" and not all(spare in fleet.spares for spare in spares):
        raise ActionError("a spare that is not available in this workshop: refused")
    if len(set(slugs)) != len(slugs) or len(set(spares)) != len(spares):
        raise ActionError("a target is named twice")
    return tuple(s for s in fleet.slugs if s in slugs), tuple(spares)


def _check_fields(command: Command) -> None:
    """Only the fields the verb's spec names may be set: fail closed on a hand-built one."""
    spec = command.spec
    names = {"verb", "keywords", *(_field(k.rstrip("?*")) for k in spec.head)}
    names |= {_field(key) for key in spec.settings}
    names |= {"selection"} if spec.selection else set()
    names |= {"upload"} if command.verb == "roster import" else set()
    for f in dataclasses.fields(command):
        if f.name not in names and getattr(command, f.name) != f.default:
            raise ActionError(f"!{command.verb} takes no {f.name.replace('_', '-')}")
    if not isinstance(command.keywords, frozenset) or not command.keywords <= set(spec.keywords):
        raise ActionError(f"!{command.verb}: a keyword it doesn't take")
    if spec.exclusive and len(command.keywords) > 1:
        raise ActionError(f"!{command.verb}: give one of {' or '.join(spec.keywords)}")


def _cat(*parts: str | Argv) -> Argv:
    return tuple(word for part in parts for word in ((part,) if isinstance(part, str) else part))


def _only(names: Argv) -> Argv:
    if not names:
        raise ActionError("nothing selected, so nothing runs")  # never a missing --only
    return _cat(*(("--only", _value(name)) for name in names))


def _value(value: object) -> str:
    if not isinstance(value, str) or not _VALUE.fullmatch(value) or ".." in value.split("/"):
        raise ActionError("a value outside the safe alphabet: refused")
    return value


def _lookup(table: Mapping[str, object], key: object) -> object:
    if not isinstance(key, str) or key not in table:
        raise ActionError("a keyword or setting without a mapped flag: refused")
    return table[key]


def _flag(value: object) -> bool:
    if value is not True and value is not False:
        raise ActionError("expected on or off")
    return value


def _count(value: object) -> str:
    if type(value) is not int or not 0 <= value <= 10:
        raise ActionError("expected 0 to 10")
    return str(value)


def _gigabytes(value: object) -> str:
    if type(value) is not int or not 1 <= value <= MAX_DISK_GB:
        raise ActionError(f"expected a size from 1 to {MAX_DISK_GB} GB")
    return str(value)


def _minutes(value: object) -> str:
    if type(value) is not int or not (value == 0 or 10 <= value <= 240):
        raise ActionError("expected minutes, 0 or 10 to 240")
    return str(value)


def _version(word: str, pattern: re.Pattern[str]) -> bool:
    return bool(VERSION_RE.fullmatch(word) and pattern.fullmatch(word))


def _field(name: str) -> str:
    return "idle_stop" if name == "minutes" else name.replace("-", "_")
