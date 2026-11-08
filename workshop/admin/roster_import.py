#!/usr/bin/env python3
"""`roster_import.py --roster R --input U [--dry-run] [--zuliprc PROV] [--org ORG] --tag K=V…
--region RG [--retired FILE] [--workshop-roster WS=ROSTER…]` (ADMIN §3.1).

Replaces the roster's participant rows (README.md §4) with an organizer's upload, the CSV of
`!roster import`, and prints the diff the plan shows: rows added, changed column by column, and
removed, in the upload's order, then what to run next. Two dry runs on the same inputs print the
same text (ADMIN §2.4), with every invisible character escaped. The upload supplies only the
input columns; every other column comes from the current roster, matched by slug, except that a
new e-mail clears `zulip_invite`, the old address's record. ADMIN §3.1 has the rules.

**Refused**, and nothing is written: an invalid upload (exit 2), a slug the admin grammar reads
as a keyword (`RESERVED_SLUGS`) included; a new row taking a slug that another workshop on the
server uses or a retired one (`--workshop-roster`, `--retired`, ADMIN §2.1); a region that is not
the workshop's (`--region`, ADMIN §2.6); a duplicate in the result; removing or renaming a row
that has a VM (in the roster or in EC2), a participant on Zulip (a `zulip_user_id`,
`zulip_invite = joined`, or a user with its address), an open invitation, a provisioned
repository or a recorded login holding anything on it, since its slug, re-added for someone
else, would get `<slug>-sandbox` and `<org>/<slug>`; changing a VM's region; and a rebind (ADMIN
§2.5), until stage 3: a new `email` for a row on Zulip, a new `github` whose recorded login has
access to `<org>/<slug>` or an open invitation to it, or is malformed.

An e-mail change of a row not on Zulip is accepted while an invitation to the old address is
open, if `GET /invites` lists it (the invitations the key controls: its own, or every one for an
administrator's key): the apply revokes it (`DELETE /invites/{id}`) before it writes the
roster, and one it cannot revoke refuses the import, nothing written (#322). One the key does
not list, sent by another user, needs a Zulip administrator (ADMIN §3.1). The key is the
server's, so when another workshop's row has the old address (`--workshop-roster`), whose
invitation it is can't be told: refused, for a deployer (`elsewhere_check`).

The live checks: `GET /users` and `GET /invites` with `--zuliprc` (an address hidden from the
key makes "nobody has it" unprovable, so it refuses), the old login's permission and
invitations with `GH_TOKEN` (a 404 counts only when it proves nothing is held), and EC2 for an
instance with the `--tag` scope and `participant=<slug>` in the row's region (or `--region`).
They run for the rows they concern, again on apply, and a check that cannot be made refuses.
The roster is written atomically with a `.bak` (`write_roster`), and only if it is still the
file this run read. It is personal data: never committed, and nothing here prints a key. An
upload that changes no row writes nothing; its dry run exits 3 (`NOTHING`), which the admin bot
answers with no plan (#708).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    ROSTER_COLUMNS,
    check_duplicates,
    keep_until_value,
    load_roster,
    load_zuliprc,
    read_roster,
    redact,
    slug_of,
    validate_row,
    write_roster,
)
from aws_common import Ec2, add_tag_argument, parse_tags
from launch import legacy_tags
from provision_repos import (
    _LOGIN_RE,
    ORG,
    GitHub,
    GitHubError,
    _invitations_for,
    github_token,
    normalize_login,
)
from zulip_provision import ZulipAdmin, _user_email, open_invitations, recorded_id

#: What an organizer supplies (ORGANIZERS §3.1, ADMIN §3.1); `slug` is the key. `keep_until`, a
#: kept VM's end (ADMIN §11.5, #570), is `!keep-until`'s too.
INPUT_COLUMNS = (
    "slug", "first_name", "last_name", "email", "repo", "region", "notes", "github", "keep_until"
)  # fmt: skip
#: Input columns the scripts write too: an empty cell keeps the recorded value, so that an upload
#: made before a column existed, or with it blank, never clears what a script recorded.
SHARED_COLUMNS = frozenset({"region", "repo", "github", "keep_until"})
#: Compared case-insensitively (`check_duplicates`, `zab.config.allowed_users`, GitHub logins).
CASELESS_COLUMNS = frozenset({"email", "github"})
#: Filled by the scripts (README.md §4): always the current roster's.
SCRIPT_COLUMNS = frozenset(
    {"host", "ip", "host_keys", "zulip_user_id", "zulip_invite", "zulip_channel_id"}
    | {"repo_status", "github_invite"}
    | {"name", "excluded"}  # roster_sync.py's (#627): kept from the current roster
)
#: Words the admin grammar reads as keywords or setting keys wherever a slug may stand
#: (`zab.admin.grammar.RESERVED`, #323): a row with such a slug could never be named in a
#: command, and `!rollout v0.2.3 alice steer` must not sweep in a participant named `steer`.
#: Copied, since the `workshop/` scripts do not import `zab.admin`; a test keeps the two equal.
RESERVED_SLUGS = frozenset(
    {"all", "spares", "hold", "protect", "lean-shared", "restart-codex"}
    | {"invites-only", "no-invite", "unsynced", "steer", "idle-stop", "multi-party", "raw-log"}
    | {"export"}  # !retire's (#559)
)
_WORKSHOP_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_UPLOAD_BYTES = 1 << 20  # the bot downloads at most 1 MB (§3.1)
NOTHING = 3  # a dry run that changes no row: the admin bot's `actions.NOTHING` (#708)
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)++-[0-9]{1,2}$")
REBIND = "a rebind (ADMIN §2.5), which a deployer does by hand until stage 3"
#: Its holder, joining, would be subscribed to `<slug>-sandbox` (and, before the rollout, admitted
#: by the VM's `allow_emails`): the invitation must go before the record of it does. The apply
#: revokes one the key lists for an e-mail change (`revoke`, #322); this is the rest.
REVOKE = (
    "its sender or a Zulip administrator revokes it first (Organization settings → Users → "
    "Invitations), then import again"
)
#: An e-mail change's note when the apply revokes the invitation to the old address (#322).
REVOKES = "not on Zulip; the apply first revokes the open invitation to the old address"
#: A slug re-added for someone else would hand them the old participant's sandbox.
SANDBOX = (
    "removing or renaming it is refused (a new row with its slug would inherit {slug}-sandbox "
    "and its history); remove it by hand after teardown"
)
#: Likewise its repository: the participant's work, pushed from their VM.
REPOSITORY = (
    "removing or renaming it is refused (a new row with its slug would inherit its repository "
    "and the work in it); remove it by hand after teardown"
)
#: Every state but `terminated`: a stopped or half-launched VM still belongs to its slug.
VM_STATES = ("pending", "running", "shutting-down", "stopping", "stopped")


# ------------------------------------------------------------------------------- live checks


class Checks(Protocol):
    """The live lookups of ADMIN §2.5; each raises when it cannot tell."""

    def zulip_user(self, email: str) -> bool:
        """Whether a Zulip user (active or not) has this address; raises when a hidden address
        makes "nobody" unprovable."""
        ...

    def zulip_invited(self, email: str) -> bool:
        """Whether an unexpired invitation to this address is open (`GET /invites`)."""
        ...

    def revoke_invitations(self, email: str) -> int:
        """Revoke the open invitations to this address that `zulip_invited` saw; how many.
        Raises when one cannot be revoked."""
        ...

    def vm(self, tag: str, region: str) -> bool:
        """Whether an instance tagged `participant=<tag>` and the workshop scope exists in
        `region`, in any state but terminated."""
        ...

    def github_hold(self, slug: str, login: str) -> str:
        """What `login` holds on the row's repository `<org>/<slug>`: `access`, `invited` (an
        open invitation) or ""; `NoRepository` when the token sees no such repository."""
        ...


class NoRepository(LookupError):
    """GitHub answers 404 for a repository that does not exist and for one the token cannot
    see (codex round 1 on #317), so this alone proves nothing."""


class StrictGitHub(GitHub):
    """`GitHub.permission` takes every 404 for `none`; here only a proven one counts."""

    def hold(self, org: str, name: str, login: str) -> str:
        if self.repo(org, name) is None:
            raise NoRepository(f"the token sees no repository {org}/{name}")
        path = f"/repos/{org}/{name}/collaborators/{login}/permission"
        status, payload = self._call("GET", path)
        if status == 200 and isinstance(payload, dict):
            if str(payload.get("permission") or "none") != "none":
                return "access"
        elif status != 404 or self.user(login) is not None:  # 404: only for no such login
            raise GitHubError("GET", path, status, payload)
        if any(not inv.get("expired") for inv in _invitations_for(self, org, name, login)):
            return "invited"
        return ""


class LiveChecks:
    """`Checks` against Zulip (`--zuliprc`), GitHub (`GH_TOKEN`) and EC2 (the `--tag` scope),
    each opened on first use."""

    def __init__(
        self,
        zuliprc: str | None,
        org: str,
        scope: Mapping[str, str],
        env: Mapping[str, str] | None = None,
        make_ec2: Callable[[str], Any] | None = None,  # `Ec2`, looked up when first needed
    ):
        self.zuliprc, self.org, self.scope, self.env = zuliprc, org, dict(scope), env
        self.make_ec2 = make_ec2
        self._zulip: tuple[frozenset[str], dict[str, list[Any]], int] | None = None
        self._github: StrictGitHub | None = None
        self._ec2: dict[str, Any] = {}

    def _admin(self) -> ZulipAdmin:
        if not self.zuliprc:
            raise LookupError("no --zuliprc to look addresses up in Zulip")
        return ZulipAdmin(*load_zuliprc(self.zuliprc))

    def _view(self) -> tuple[frozenset[str], dict[str, list[Any]], int]:
        """(every address the key sees, the open invitations' ids by address, how many humans'
        addresses are hidden)."""
        if self._zulip is None:
            admin = self._admin()
            try:
                users, invited = admin.users(), open_invitations(admin, time.time())
            finally:
                admin.close()
            # a null `delivery_email` is an address the key cannot see, and `email` then a
            # synthetic one (DECISIONS, 2026-10-04); before feature level 163 there is no such
            # key, and `email` is the address
            hidden = sum(
                1
                for u in users
                if not u.get("is_bot") and "delivery_email" in u and u["delivery_email"] is None
            )
            emails = frozenset(_user_email(u) for u in users) - {""}
            self._zulip = emails, invited, hidden
        return self._zulip

    def zulip_user(self, email: str) -> bool:
        emails, _, hidden = self._view()
        if hidden:
            raise LookupError(
                f"{hidden} user(s) hide their address from this key, so no lookup can show that "
                "nobody has one: a deployer does this by hand"
            )
        return email.strip().casefold() in emails

    def zulip_invited(self, email: str) -> bool:
        return email.strip().casefold() in self._view()[1]

    def revoke_invitations(self, email: str) -> int:
        """The ids come from the listing the checks read, so the apply revokes what it checked
        (an invitation sent within the seconds since is not in it)."""
        ids = self._view()[1].get(email.strip().casefold(), [])
        if ids:
            admin = self._admin()
            try:
                for invite_id in ids:
                    admin.revoke_invite(invite_id)
            finally:
                admin.close()
        return len(ids)

    def vm(self, tag: str, region: str) -> bool:
        if region not in self._ec2:
            self._ec2[region] = (self.make_ec2 or Ec2)(region)
        tags = {**self.scope, "participant": tag}
        return bool(self._ec2[region].find_instances(tags, VM_STATES))

    def github_hold(self, slug: str, login: str) -> str:
        if self._github is None:
            self._github = StrictGitHub(github_token(self.env))
        return self._github.hold(self.org, slug, login)


# ------------------------------------------------------------------------------------ upload


def read_upload(path: str) -> tuple[list[dict[str, str]], frozenset[str], list[str]]:
    """The upload's rows, the input columns it has, and warnings; ValueError when invalid."""
    if os.path.getsize(path) > MAX_UPLOAD_BYTES:
        raise ValueError(f"larger than {MAX_UPLOAD_BYTES} bytes")
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = [line for line in csv.reader(f) if line]  # DictReader skips empty lines too
    names = [c.strip().lower() for c in (lines[0] if lines else [])]
    header = [c for c in names if c]
    if len(set(header)) != len(header):
        raise ValueError("a column name appears twice")
    for required in ("slug", "email"):
        if required not in header:
            raise ValueError(f"no {required} column")
    # the `slug` cell itself: `read_roster` derives a slug from `github` and keeps any column,
    # an uploaded `slug_given` too (reviewer on #317)
    at = names.index("slug")
    cells = [line[at].strip() if at < len(line) else "" for line in lines[1:]]
    parsed = read_roster(path)
    if len(parsed) != len(cells):
        raise ValueError("the rows do not parse consistently")
    rows: list[dict[str, str]] = []
    for n, (row, cell) in enumerate(zip(parsed, cells, strict=True), start=1):
        if not any(row.get(c) for c in header):
            continue  # an empty line of a spreadsheet export
        for column in INPUT_COLUMNS:  # `keep_until` is not one of every roster's columns
            row.setdefault(column, "")
        slug = row["slug"]
        if not cell or cell != slug:
            raise ValueError(f"row {n}: no slug")
        if slug in RESERVED_SLUGS:
            raise ValueError(f"row {n}: the slug {slug!r} is a keyword of the admin commands")
        for column in INPUT_COLUMNS:
            allowed = "\n\t" if column == "notes" else ""
            if any(unicodedata.category(ch) == "Cc" and ch not in allowed for ch in row[column]):
                raise ValueError(f"row {n}: a control character in {column}")
        if not row["email"]:
            raise ValueError(f"{slug}: no e-mail (required, ORGANIZERS §3.1)")
        if row["region"] and not _REGION_RE.match(row["region"]):
            raise ValueError(f"{slug}: invalid region {row['region']!r}")
        if row["keep_until"]:  # as `!keep-until` takes it, which also checks that it is ahead
            value = keep_until_value(row["keep_until"])
            if value is None:
                raise ValueError(
                    f"{slug}: keep_until {row['keep_until']!r} is not a date like 2026-11-30, "
                    "or 2026-11-30T15:00 (UTC)"
                )
            row["keep_until"] = value
        # `@alice` or a profile URL, as organizers copy it: the login, as provision_repos.py
        # reads the Zulip field (#327)
        row["github"] = normalize_login(row["github"])
        if row["github"] and not _LOGIN_RE.match(row["github"]):
            raise ValueError(f"{slug}: invalid GitHub login {row['github']!r}")
        rows.append(row)
    warnings = []
    ignored = sorted(set(header) - set(INPUT_COLUMNS))
    if script := [c for c in ignored if c in SCRIPT_COLUMNS]:
        warnings.append(f"columns ignored, the scripts fill them: {', '.join(script)}")
    if other := [_visible(c) for c in ignored if c not in SCRIPT_COLUMNS]:
        warnings.append(f"columns ignored, not roster input: {', '.join(other)}")
    return rows, frozenset(header) & frozenset(INPUT_COLUMNS), warnings


# -------------------------------------------------------------------------------------- diff


@dataclass(frozen=True, slots=True)
class Change:
    column: str
    old: str
    new: str
    note: str = ""


#: (the change, with its note when it is allowed; why it is refused, or None)
Verdict = tuple[Change, str | None]


@dataclass(slots=True)
class Import:
    rows: list[dict[str, str]] = field(default_factory=list)  # the roster to write
    added: list[tuple[str, list[Change]]] = field(default_factory=list)
    changed: list[tuple[str, list[Change]]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    revoke: list[tuple[str, str]] = field(default_factory=list)  # (slug, old e-mail), #322


def launched(row: Mapping[str, str]) -> bool:
    """Whether the row has a VM: `launch.py` records its `host` and `ip`."""
    return bool(row.get("host") or row.get("ip"))


def same(column: str, old: str, new: str) -> bool:
    if column in CASELESS_COLUMNS:
        return old.casefold() == new.casefold()
    return old == new


def bound(row: Mapping[str, str]) -> str:
    """How the roster says the row's participant is on Zulip, or "". `zulip_invite = joined`
    counts as much as the id, which a run without `--no-rebind` clears for a user it no longer
    matches (deactivated, or hidden from the key; reviewer on #317). ValueError for a bad id."""
    if uid := recorded_id(row):
        return f"bound to Zulip user {uid}"
    if (row.get("zulip_invite") or "").strip() == "joined":
        return "recorded as joined (zulip_invite)"
    return ""


def address_check(email: str, checks: Checks) -> str:
    """ "user" when a Zulip user has `email`, "invited" when an invitation to it is open, else
    empty. An exception when that cannot be told."""
    if checks.zulip_user(email):
        return "user"
    return "invited" if checks.zulip_invited(email) else ""


def email_change(slug: str, old: Mapping[str, str], change: Change, checks: Checks) -> Verdict:
    """A rebind when the row is bound, or a Zulip user has the old address (ADMIN §2.5). An open
    invitation to the old address gets the note `REVOKES`: the change drops its record, so the
    apply revokes it first (#322)."""
    try:
        if why := bound(old):
            return change, f"{slug}: e-mail change of a row {why}: {REBIND}"
        held = address_check(change.old, checks) if change.old else ""
    except Exception as exc:  # fail closed: an unchecked change is refused
        why = redact(str(exc))
        return change, f"{slug}: e-mail change: cannot check the old address: {why}"
    if held == "user":
        return change, f"{slug}: e-mail change: a Zulip user has the old address: {REBIND}"
    if held:
        return dataclasses.replace(change, note=REVOKES), None
    # `GET /invites` lists only the key's own unless it is an administrator's (ADMIN §3.1)
    note = (
        "plain change: not on Zulip, and no open invitation to the old address that the "
        "provisioner sees"
    )
    return dataclasses.replace(change, note=note), None


def provisioned(row: Mapping[str, str]) -> bool:
    """Whether `provision_repos.py` ran for the row: `<org>/<slug>` may then exist and hold the
    participant's work, pushed from their VM with its deploy key."""
    return bool(row.get("repo_status") or row.get("github_invite"))


def github_holding(
    slug: str, row: Mapping[str, str], login: str, checks: Checks
) -> tuple[str, str]:
    """("access" or "invited", "") when the recorded `login` holds that on `<org>/<slug>`, else
    ("", why nothing). Raises when that cannot be told: a malformed login proves nothing, which
    `provision_repos.grant_access(no_rebind=True)` holds too (codex round 6 on #317)."""
    if not login:
        return "", "no login recorded"
    if not _LOGIN_RE.match(login):
        raise LookupError("not a GitHub login: a deployer fixes it in the roster first")
    try:
        return checks.github_hold(slug, login), f"{login} has no access and no open invitation"
    except NoRepository:
        if provisioned(row):
            raise
        return "", "no repository yet, and provision_repos.py never ran for the row"


def github_change(slug: str, old: Mapping[str, str], change: Change, checks: Checks) -> Verdict:
    """A rebind when the recorded login has access to the repository (ADMIN §2.5); refused too
    while it has an open invitation, which only the roster's record lets `provision_repos.py`
    withdraw (codex round 1 on #317)."""
    login = change.old
    try:
        held, why = github_holding(slug, old, login, checks)
    except Exception as exc:  # fail closed
        what, why = _visible(login[:40]), redact(str(exc))
        return change, f"{slug}: GitHub change: cannot check {what}: {why}"
    if held == "access":
        return change, f"{slug}: GitHub change: {login} has access to the repository: {REBIND}"
    if held:
        return change, (
            f"{slug}: GitHub change: {login} has an open invitation to the repository: withdraw "
            "it first, or have the participant correct the GitHub field of their Zulip profile "
            "and run !provision repos"
        )
    return dataclasses.replace(change, note=f"plain change: {why}"), None


def vm_check(slug: str, row: Mapping[str, str], region: str, checks: Checks) -> str:
    """The `participant` tag of the row's instance in `region`, or empty when EC2 has none; an
    exception when that cannot be told. `region` alone proves nothing (organizers fill it in
    before the launch), and `host` and `ip` miss a launch that failed half-way (reviewer on
    #317)."""
    for tag in (slug, *legacy_tags(dict(row), slug)):
        if checks.vm(tag, region):
            return tag
    return ""


def check_change(
    slug: str, old: Mapping[str, str], change: Change, checks: Checks, region: str
) -> Verdict:
    if change.column == "region":
        where = change.old or region
        if launched(old):
            return (
                change,
                f"{slug}: has a VM: its recorded region never changes (workshop/aws/README.md)",
            )
        try:
            tag = vm_check(slug, old, where, checks)
        except Exception as exc:  # fail closed
            return change, f"{slug}: region change: cannot look its VM up: {redact(str(exc))}"
        if tag:
            why = f"a VM tagged participant={tag} is in {where}"
            return change, f"{slug}: {why}: its region never changes (workshop/aws/README.md)"
        return dataclasses.replace(change, note=f"plain change: no VM in {where}"), None
    if change.column == "email":
        return email_change(slug, old, change, checks)
    if change.column == "github":
        return github_change(slug, old, change, checks)
    return change, None


def removal(
    slug: str, row: Mapping[str, str], to: str | None, checks: Checks, region: str
) -> str | None:
    """Why removing or renaming the row is refused, or None.

    A row stays while it has a VM (in the roster, or in EC2 after a launch that failed half-way),
    a participant on Zulip, an open invitation, a provisioned repository, or a login holding
    anything on it: its slug, re-added for someone else, would get `<slug>-sandbox` or
    `<org>/<slug>` and what is in them (79b78ba; codex rounds 4 and 6, reviewer on #317)."""
    hint = f", renamed to {to}?" if to else ""
    teardown = "removing or renaming it is refused; tear it down first"
    if launched(row):
        return f"{slug}: has a VM ({row.get('host') or row.get('ip')}{hint}): {teardown}"
    where = row.get("region") or region
    try:
        if why := bound(row):
            return f"{slug}: {why}{hint}: {SANDBOX.format(slug=slug)}"
        if provisioned(row):
            return f"{slug}: provision_repos.py ran for it{hint}: {REPOSITORY}"
        if tag := vm_check(slug, row, where, checks):
            return f"{slug}: a VM tagged participant={tag} is in {where}{hint}: {teardown}"
        held = address_check(row["email"], checks) if row.get("email") else ""
        login = row.get("github", "")
        if not held and (holds := github_holding(slug, row, login, checks)[0]):
            held = f"login {holds}"
    except Exception as exc:  # fail closed
        why = redact(str(exc))
        return f"{slug}: removing it: cannot check its VM, address and login: {why}"
    if held == "user":
        return f"{slug}: a Zulip user has its address{hint}: {SANDBOX.format(slug=slug)}"
    if held == "invited":
        return f"{slug}: an invitation to its address is open{hint}: {REVOKE}"
    if held:
        what = "has access to" if held == "login access" else "has an open invitation to"
        return f"{slug}: its recorded login {login} {what} its repository{hint}: {REPOSITORY}"
    return None


def taken_slugs(retired: str | None, workshops: list[str]) -> dict[str, str]:
    """Slugs no new row may take, unique on the server for its whole life (ADMIN §2.1): slug ->
    the other workshop that has it, or "" for a retired one. ValueError when unreadable."""
    taken: dict[str, str] = {}
    if retired:
        with open(retired, encoding="utf-8") as f:
            for line in f:
                if slug := line.split("#", 1)[0].strip().lower():
                    taken[slug] = ""
    for spec in workshops:
        ws, _, path = spec.partition("=")
        if not _WORKSHOP_RE.match(ws) or not path:
            raise ValueError(f"--workshop-roster {spec!r}: expected WS=ROSTER")
        for row in read_roster(path):
            if slug := slug_of(row):
                taken.setdefault(slug, ws)
    return taken


def taken_addresses(workshops: list[str]) -> dict[str, str]:
    """The other workshops' e-mails, casefolded -> the first workshop with a row that has it
    (`--workshop-roster`). The provisioner's key is the server's, so `GET /invites` lists their
    invitations too, and an address is unique only within a workshop (review of #576).
    ValueError when unreadable, as `taken_slugs`."""
    found: dict[str, str] = {}
    for spec in workshops:
        ws, _, path = spec.partition("=")
        if not _WORKSHOP_RE.match(ws) or not path:
            raise ValueError(f"--workshop-roster {spec!r}: expected WS=ROSTER")
        for row in read_roster(path):
            if email := (row.get("email") or "").strip().casefold():
                found.setdefault(email, ws)
    return found


def elsewhere_check(slug: str, change: Change, elsewhere: Mapping[str, str]) -> Verdict:
    """An e-mail change that would revoke the invitations to the old address, refused when
    another workshop has a row with that address: those may be its own, which this import must
    not touch (review of #576), or this workshop's, which must not stay open (#322), and
    `GET /invites` doesn't say which sandbox an invitation subscribes to. This row's
    `zulip_invite` proves nothing either way: `zulip_provision.py` recorded an invitation, before
    0.2.10, only once Zulip's answer arrived (Codex round 1 on #576). A deployer, who can
    see who sent it, makes the change by hand."""
    ws = elsewhere.get(change.old.strip().casefold())
    if not ws:
        return change, None
    return change, (
        f"{slug}: e-mail change: an invitation to the old address is open, and {ws} has a row "
        "with that address: whose invitation it is can't be told here, so a deployer makes this "
        "change by hand (Organization settings → Users → Invitations names who sent it)"
    )


def taken_refusal(slug: str, ws: str) -> str:
    """Which workshop has the slug, and no more: the admin bot adds its own hint, naming its
    real bot, at most once per sender every 10 minutes (ADMIN §2.1; #347). A name made from
    the convention could name a bot that isn't the workshop's, and ignore that rule."""
    if not ws:
        return f"{slug}: a retired workshop's slug, never reused on the server (ADMIN §2.1)"
    return f"{slug} is in {ws}; a slug is never shared between workshops (ADMIN §2.1)"


def plan_import(
    current: list[dict[str, str]],
    upload: list[dict[str, str]],
    columns: frozenset[str],
    checks: Checks,
    region: str,
    taken: Mapping[str, str] | None = None,
    elsewhere: Mapping[str, str] | None = None,
) -> Import:
    """The new roster and its diff: the upload's rows in its order, each keeping the current
    row's other columns, then the current rows without a slug; refusals collected, not raised.
    `elsewhere`: the other workshops' addresses (`taken_addresses`)."""
    imp = Import()
    present = {slug_of(r): r for r in current if slug_of(r)}
    for up in upload:
        slug = slug_of(up)
        old = present.get(slug)
        if up["region"] and up["region"] != region:  # one region per workshop (ADMIN §2.6)
            imp.refusals.append(f"{slug}: region {up['region']} is not the workshop's, {region}")
        if old is None:
            if taken and slug in taken:
                imp.refusals.append(taken_refusal(slug, taken[slug]))
            row = {c: "" for c in ROSTER_COLUMNS}
            row.update({c: up[c] for c in INPUT_COLUMNS if c in columns})
            added = [Change(c, "", row[c]) for c in INPUT_COLUMNS[1:] if row.get(c)]
            imp.added.append((slug, added))
            imp.rows.append(row)
            continue
        row = dict(old)
        changes = []
        for column in INPUT_COLUMNS[1:]:
            new, before = up[column], old.get(column, "")
            if column not in columns or (column in SHARED_COLUMNS and not new):
                continue
            if same(column, before, new) or (column == "region" and new != region):
                continue  # (another region is refused above)
            change, refusal = check_change(slug, old, Change(column, before, new), checks, region)
            if not refusal and column == "email" and change.note == REVOKES:
                change, refusal = elsewhere_check(slug, change, elsewhere or {})
                if not refusal:
                    imp.revoke.append((slug, before))
            changes.append(change)
            if refusal:
                imp.refusals.append(refusal)
            row[column] = new
            if column == "email" and row.get("zulip_invite"):
                # the old address's record: before 0.2.10, `zulip_provision.py` took an open
                # invitation to the new one for this sandbox's (codex round 2 on #317)
                changes.append(Change("zulip_invite", row["zulip_invite"], "", "the old address's"))
                row["zulip_invite"] = ""
        if changes:
            imp.changed.append((slug, changes))
        else:
            imp.unchanged += 1
        imp.rows.append(row)
    kept = {slug_of(r) for r in upload}
    new_slugs = {up["email"].casefold(): slug_of(up) for up in upload if slug_of(up) not in present}
    for row in current:
        slug = slug_of(row)
        if not slug:
            imp.rows.append(row)  # not a participant: kept as it is
        elif slug not in kept:
            imp.removed.append(slug)
            to = new_slugs.get(row.get("email", "").casefold())
            if refusal := removal(slug, row, to, checks, region):
                imp.refusals.append(refusal)
    try:
        for row in imp.rows:
            if slug_of(row):
                validate_row(row)
        check_duplicates(imp.rows)
    except ValueError as exc:
        imp.refusals.append(str(exc))
    return imp


#: Characters that would hide or reorder what an organizer approves: controls, format
#: characters (bidi controls, zero-width), line and paragraph separators, private use,
#: surrogates and unassigned code points (reviewer on #317).
_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"})


def _visible(text: str) -> str:
    return "".join(
        (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        if unicodedata.category(c) in _INVISIBLE
        else c
        for c in text
    )


def _value(text: str) -> str:
    return _visible(json.dumps(text, ensure_ascii=False))  # quoted, every invisible escaped


def next_steps(imp: Import) -> list[str]:
    def touched(column: str) -> str:
        return " ".join(s for s, changes in imp.changed if any(c.column == column for c in changes))

    out = []
    if added := " ".join(s for s, _ in imp.added):
        out.append(
            f"next, new rows: !launch {added}, then !provision zulip {added}, "
            f"!provision repos {added} and !rollout VERSION {added}"
        )
    if emails := touched("email"):
        out.append(f"next, new e-mails: !provision zulip {emails}, then !rollout VERSION {emails}")
    if logins := touched("github"):
        out.append(f"next, new GitHub logins: !provision repos {logins}")
    return out


def render(imp: Import) -> str:
    lines = [
        f"roster import: {len(imp.added)} added, {len(imp.changed)} changed, "
        f"{len(imp.removed)} removed, {imp.unchanged} unchanged"
    ]
    lines += [f"warning: {w}" for w in imp.warnings]
    for mark, entries in (("+", imp.added), ("~", imp.changed)):
        for slug, changes in entries:
            lines.append(f"{mark} {slug}")
            for c in changes:
                value = _value(c.new) if mark == "+" else f"{_value(c.old)} -> {_value(c.new)}"
                lines.append(f"    {c.column}: {value}" + (f"  ({c.note})" if c.note else ""))
    lines += [f"- {slug}" for slug in imp.removed]
    return "\n".join(lines + next_steps(imp))


# -------------------------------------------------------------------------------------- main


def revoke(imp: Import, checks: Checks) -> bool:
    """The apply's first change: each open invitation to a corrected row's old address, revoked
    before the roster drops its record (#322). False, the roster unwritten, when one cannot be:
    the run says which. A revocation before it stays done, and printed: the import, run again,
    finds that row's old address without an invitation, a plain change."""
    for slug, email in imp.revoke:
        try:
            count = checks.revoke_invitations(email)
        except Exception as exc:  # fail visibly, never with the invitation open and unrecorded
            why = redact(str(exc))
            print(
                f"refused: {slug}: cannot revoke the invitation to the old address: {why}; "
                f"{REVOKE}",
                file=sys.stderr,
            )
            print("nothing written", file=sys.stderr)
            return False
        print(f"revoked: {slug}: {count} open invitation(s) to the old address")
    return True


def digest(path: str) -> str:
    """The roster file's SHA-256, or "" when there is none yet."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return ""


def main(argv: list[str] | None = None, *, checks: Checks | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="roster_import.py", description="Import an organizer's roster (ADMIN §3.1)."
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--input", required=True, help="the organizer's CSV")
    parser.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    parser.add_argument("--zuliprc", help="provisioner zuliprc, to check an e-mail change")
    parser.add_argument(
        "--org", default=ORG, help="GitHub organization, to check a login change (GH_TOKEN)"
    )
    add_tag_argument(parser)
    parser.add_argument(
        "--region",
        required=True,
        help="the workshop's region, the only one a row may name",
    )
    parser.add_argument("--retired", metavar="FILE", help="the server's retired-slugs.txt")
    parser.add_argument(
        "--workshop-roster",
        action="append",
        default=[],
        metavar="WS=ROSTER",
        help="another workshop on the server and its roster (repeatable): its slugs are taken",
    )
    args = parser.parse_args(argv)
    if not _LOGIN_RE.match(args.org) or not _REGION_RE.match(args.region):
        print(f"invalid organization {args.org!r} or region {args.region!r}", file=sys.stderr)
        return 2
    try:
        scope = parse_tags(args.tag)
    except ValueError as exc:
        print(f"--tag: {exc}", file=sys.stderr)
        return 2
    try:
        upload, columns, warnings = read_upload(args.input)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"upload: {exc}", file=sys.stderr)
        return 2
    try:
        before = digest(args.roster)
        current = load_roster(args.roster) if before else []
        taken = taken_slugs(args.retired, args.workshop_roster)
        elsewhere = taken_addresses(args.workshop_roster)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 2
    checks = checks or LiveChecks(args.zuliprc, args.org, scope)
    imp = plan_import(current, upload, columns, checks, args.region, taken, elsewhere)
    imp.warnings[:0] = warnings
    if imp.refusals:
        for refusal in imp.refusals:
            print(f"refused: {refusal}", file=sys.stderr)
        print("nothing written", file=sys.stderr)
        return 1
    print(render(imp))
    if not (imp.added or imp.changed or imp.removed):
        print("no changes: nothing to write")
        return NOTHING if args.dry_run else 0
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    try:
        if digest(args.roster) != before:
            print("refused: the roster changed during the import; nothing written", file=sys.stderr)
            return 1
        if not revoke(imp, checks):
            return 1
        write_roster(args.roster, imp.rows)
    except OSError as exc:
        print(f"roster NOT written: {exc}", file=sys.stderr)
        return 1
    kept = f" (the previous one is {args.roster}.bak)" if before else ""
    print(f"written: {args.roster}{kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
