#!/usr/bin/env python3
"""`roster_sync.py --roster R --zuliprc PROV --out-dir OUT --region RG --tag K=V…
[--retired FILE] [--workshop-roster WS=ROSTER…] (--sync CHANNEL | --remove SLUG | --restore SLUG |
--rename SLUG NEW) [--changes FILE] [--dry-run]` (docs/ADMIN.md §3.1a; #627).

The roster from the workshop's private Zulip channel, decided with Drew on Oct 25: organizers
invite people by e-mail with the channel ticked, and each subscriber becomes a row, linked by
Zulip user ID from the start, so that no row depends on matching an e-mail address.
`roster_import.py` stays, for the kept fleet and as the fallback.

**`--sync CHANNEL`**, the channel's stream ID (admin.toml's `channel`), read with the
provisioner's key, which must be subscribed to it: its active human subscribers, never the key's
own user (`users/me`: `zab-provisioner` is a Moderator user in the channel, not a bot).
- **A subscriber without a row** gets one: `zulip_user_id`, `name` (their Zulip full name, its
  control and format characters dropped) and `email` (their delivery address when the key sees
  it, else Zulip's `email` field, a placeholder such as `user123@<org>`). Its slug comes from the
  name (`slug_for`): NFKD folded to ASCII (with `FOLD` for the letters NFKD leaves alone, such
  as ø and ß), lower case, each run of other characters one hyphen, outer hyphens trimmed, cut
  to 32 characters at a hyphen where possible. A result that is empty, reserved
  (`RESERVED_SLUGS`, `zab-spare-N`) or otherwise unusable is `p-<user ID>`. It must be unique on
  the server for its whole life (ADMIN §2.1): a slug of this roster, of another workshop's
  (`--workshop-roster`) or retired (`--retired`), or one the provisioner's key sees something
  named after (a bot `Claude · <slug>`, a channel `#<slug>-sandbox`, or `out/<slug>/`), takes
  `-2`, `-3`, … instead, cut to fit.
- **A row** is matched by `zulip_user_id`, or, a legacy row without one, by its e-mail. Its
  `name` is refreshed, and nothing else changes.
- **A row matched by no subscriber** (they left, or were deactivated) is removed when nothing is
  provisioned for it (`holds`: no VM in the roster or in EC2, no bots, no sandbox, no
  repository); otherwise it is reported `left the channel` and left alone: its end is the
  organizers' (`!retire`). A check that can't be made keeps the row too.
- **An excluded row** (`--remove`) is never added again, refreshed or removed: it is listed.

**`--remove SLUG`** marks the row excluded (its `excluded` cell `yes`): `!roster sync` never adds
it again, and the bot's `all` leaves it out. Refused while anything is provisioned for it.
**`--restore SLUG`** clears the mark. **`--rename SLUG NEW`** takes `--remove`'s precondition,
and `NEW` the slug rules and uniqueness above, nothing named after it included.

Two dry runs on the same state print the same text, every invisible character escaped (ADMIN
§2.4). A sync's dry run also writes that text to `--changes FILE` (the plan's `runs/P-n/`), and
its apply writes the roster only if the changes it finds are those: the runner's recheck, the
last dry run, matched the approved plan, so nothing that changed after it is applied unapproved
(Codex round 1 on #641). Exit 0 with a change to make (or made), 1 refused, 2 invalid, 3
(`NOTHING`) a dry run with nothing to change, which the bot answers with no plan (#708), never
one where what a departed row holds can't be checked, which is refused (`UNCHECKED`). The
roster is written atomically with a `.bak`, only if it is still the file this run read; the
bot's workshop lock serializes the applies.
"""

from __future__ import annotations

import argparse
import csv
import os
import pathlib
import re
import sys
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    ROSTER_COLUMNS,
    check_duplicates,
    keys_dir,
    load_roster,
    load_zuliprc,
    redact,
    slug_of,
    validate_row,
    write_roster,
)
from aws_common import _EMAIL_RE, add_tag_argument, is_spare_name, parse_tags
from roster_import import (
    _REGION_RE,
    RESERVED_SLUGS,
    Checks,
    LiveChecks,
    digest,
    launched,
    taken_refusal,
    taken_slugs,
    vm_check,
)
from roster_import import _value as quoted
from zulip_provision import (
    BOT_KINDS,
    ZulipAdmin,
    ZulipAdminError,
    bot_full_name,
    channel_name,
    find_channel,
    recorded_id,
)

MAX_SLUG = 32
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
EXCLUDED, YES = "excluded", "yes"  # the column, and its mark (`--remove`)
NOTHING = 3  # a sync with nothing to change: the bot offers no plan, and says so with ☑ (#708)
#: Letters that NFKD doesn't decompose, folded as German, Scandinavian and Polish names write them
#: in ASCII; any other such letter is dropped, as NFKD's marks are.
FOLD = str.maketrans({
    "ß": "ss", "ẞ": "SS", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O",
    "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH", "ł": "l", "Ł": "L",
    "\u0131": "i", "ħ": "h", "Ħ": "H", "ŋ": "n", "Ŋ": "N",
})  # fmt: skip


# ------------------------------------------------------------------------------- the slugs


def usable(slug: str) -> bool:
    """A slug the scripts and the admin grammar take (`validate_row`, `RESERVED_SLUGS`)."""
    return bool(_SLUG_RE.match(slug)) and slug not in RESERVED_SLUGS and not is_spare_name(slug)


def cut(text: str, limit: int = MAX_SLUG) -> str:
    """`text` cut to `limit` characters, at a hyphen where one falls within them."""
    if len(text) <= limit:
        return text
    at = text[: limit + 1].rfind("-")
    return (text[:at] if at > 0 else text[:limit]).strip("-")


def base_slug(name: str) -> str:
    """The slug a full name gives, before uniqueness: maybe empty or reserved (module doc)."""
    ascii_ = unicodedata.normalize("NFKD", name.translate(FOLD)).encode("ascii", "ignore")
    return cut(re.sub(r"[^a-z0-9]+", "-", ascii_.decode("ascii").lower()).strip("-"))


def slug_for(name: str, user_id: int, taken: Callable[[str], bool]) -> str:
    """The new row's slug (module doc): `base_slug`, else `p-<user_id>`; then the first of it,
    `-2`, `-3`, … that is usable and not `taken`."""
    base = base_slug(name)
    if not usable(base):
        base = f"p-{user_id}"
    if not taken(base):
        return base
    for n in range(2, 10_000):
        suffix = f"-{n}"
        slug = cut(base, MAX_SLUG - len(suffix)) + suffix
        if usable(slug) and not taken(slug):
            return slug
    raise ValueError(f"no free slug for user {user_id}")


def clean_name(name: object) -> str:
    """A Zulip full name as a row keeps it: no control, format or private-use character, and
    single spaces."""
    text = "".join(c for c in str(name or "") if unicodedata.category(c)[0] != "C")
    return " ".join(text.split())


def address(user: Mapping[str, Any]) -> str:
    """The address the key sees: the delivery address, else Zulip's `email` (a placeholder for
    an address hidden from it, DECISIONS 2026-10-04)."""
    return str(user.get("delivery_email") or user.get("email") or "").strip()


#: The note on a new row whose address the scripts refuse (review of #641, 1).
PLACEHOLDER = "their address is not one the scripts take: a placeholder address instead"


def row_address(user: Mapping[str, Any], host: str) -> tuple[str, str]:
    """A new row's e-mail, and a note: `address`, unless `validate_row` refuses it (Zulip takes
    `mary.o'brien@uni.example.edu`, the scripts don't), when it is a placeholder instead: Zulip's
    `email`, else `user<id>@<host>`, as for an address hidden from the key; the row is linked by
    its ID (review of #641: one such address must not refuse the whole sync)."""
    found = address(user)
    if _EMAIL_RE.match(found):
        return found, ""
    shown = str(user.get("email") or "").strip()
    placeholder = shown if _EMAIL_RE.match(shown) else f"user{int(user['user_id'])}@{host}"
    return placeholder, PLACEHOLDER


# ------------------------------------------------------------------------------ the view


@dataclass
class View:
    """What the provisioner's key sees: itself, the channel and its members (the active humans
    but itself, by user ID), and what is named after slugs (bots, channels)."""

    me: dict[str, Any]
    bots: frozenset[str]  # every bot's full name the key sees, casefolded
    streams: list[dict[str, Any]]
    host: str = ""  # the organization's host, of Zulip's placeholder addresses
    channel: dict[str, Any] | None = None
    members: list[dict[str, Any]] = field(default_factory=list)
    subscribers: int = 0


def read_view(admin: ZulipAdmin, channel: int | None) -> View:
    """The view (`View`); with `channel`, its subscribers. Raises when the key can't read it."""
    me = admin.me()
    users = admin.users()
    bots = {str(u.get("full_name", "")).casefold() for u in users if u.get("is_bot")}
    bots |= {str(b.get("full_name", "")).casefold() for b in admin.bots()}
    host = urllib.parse.urlsplit(admin.site).hostname or ""
    view = View(me, frozenset(bots - {""}), admin.streams(), host)
    if channel is None:
        return view
    try:
        view.channel = admin.request("GET", f"streams/{int(channel)}")["stream"]
        ids = admin.request("GET", f"streams/{int(channel)}/members")["subscribers"]
    except ZulipAdminError as exc:
        who = me.get("full_name") or "the provisioner"
        raise LookupError(
            f"cannot read channel {channel}'s subscribers ({exc}): subscribe {who} to it"
        ) from None
    by_id = {int(u["user_id"]): u for u in users if "user_id" in u}
    view.subscribers = len(ids)
    for uid in sorted({int(uid) for uid in ids}):
        user = by_id.get(uid) or admin.user(uid)  # the bulk listing may leave one out
        if user.get("is_bot") or user.get("is_active") is False or uid == int(me["user_id"]):
            continue
        view.members.append(user)
    return view


# ------------------------------------------------------------------- what a row holds


def holds(
    row: Mapping[str, str], view: View, out_dir: pathlib.Path, checks: Checks, region: str
) -> str:
    """What is provisioned for the row, in words, or "" for nothing: a VM (the roster's, or one
    EC2 has with its tag), its bots (their keys in `out/`, or a bot of their names), its sandbox
    (recorded, or a channel of its name), its repository (`provision_repos.py`'s record). Raises
    when EC2 can't tell. A slug nothing is named after is free to name a new row."""
    slug = slug_of(dict(row))
    if launched(row):
        return f"it has a VM ({row.get('host') or row.get('ip')})"
    path, _ = keys_dir(out_dir, row)
    for kind in BOT_KINDS:
        if (path / f"{kind}.zuliprc").exists():
            return f"its {kind} bot's key is in out/{path.name}/"
        if bot_full_name(kind, slug).casefold() in view.bots:
            return f"a bot named {bot_full_name(kind, slug)} exists"
    if (row.get("zulip_channel_id") or "").strip():
        return f"its sandbox is recorded (zulip_channel_id {row['zulip_channel_id']})"
    if find_channel(view.streams, slug, "") is not None:
        return f"#{channel_name(slug)} exists"
    if row.get("repo_status") or row.get("github_invite"):
        return "provision_repos.py ran for it: its repository may hold work"
    if tag := vm_check(slug, dict(row), row.get("region") or region, checks):
        return f"a VM tagged participant={tag} is in {row.get('region') or region}"
    return ""


def named_after(slug: str, view: View, out_dir: pathlib.Path) -> bool:
    """Whether the key sees something named after `slug` (a bot, a sandbox) or `out/` has it:
    such a slug, free in every roster, would still hand its new row what is there."""
    if (out_dir / slug).exists() or find_channel(view.streams, slug, "") is not None:
        return True
    return any(bot_full_name(kind, slug).casefold() in view.bots for kind in BOT_KINDS)


# ------------------------------------------------------------------------------- the sync


@dataclass
class Sync:
    rows: list[dict[str, str]]  # the roster to write
    added: list[tuple[str, dict[str, str], str]] = field(default_factory=list)  # slug, row, note
    renamed: list[tuple[str, str, str]] = field(default_factory=list)  # slug, old, new name
    removed: list[tuple[str, str]] = field(default_factory=list)  # slug, its row's name
    left: list[tuple[str, str]] = field(default_factory=list)  # slug, what it holds
    unchecked: list[str] = field(default_factory=list)  # of `left`: what it holds can't be told
    excluded: list[str] = field(default_factory=list)
    unchanged: int = 0
    refusals: list[str] = field(default_factory=list)

    def changes(self) -> bool:
        return bool(self.added or self.renamed or self.removed)


def is_excluded(row: Mapping[str, str]) -> bool:
    return bool((row.get(EXCLUDED) or "").strip())


def plan_sync(
    current: list[dict[str, str]],
    view: View,
    out_dir: pathlib.Path,
    checks: Checks,
    region: str,
    taken: Mapping[str, str],
) -> Sync:
    """The new roster (module doc): the current rows, refreshed, but those removed, then the
    new members' rows in user ID order. Refusals collected, not raised."""
    sync = Sync(rows=[])
    people = [r for r in current if slug_of(r)]
    by_id: dict[str, dict[str, str]] = {}
    by_email: dict[str, dict[str, str]] = {}
    for row in people:
        try:
            uid = recorded_id(row)
        except ValueError as exc:
            sync.refusals.append(str(exc))
            continue
        if uid:
            by_id[uid] = row
        elif row.get("email"):
            by_email[row["email"].strip().casefold()] = row
    matched: dict[int, dict[str, str]] = {}  # id(row) → the member it matched
    new: list[dict[str, Any]] = []
    for user in view.members:
        uid = str(int(user["user_id"]))
        row = by_id.get(uid) or by_email.get(address(user).casefold())
        if row is None:
            new.append(user)
        elif id(row) in matched:  # two members, one legacy row: never guessed
            sync.refusals.append(f"{slug_of(row)}: two subscribers match it; fix the roster")
        else:
            matched[id(row)] = user
    slugs = {slug_of(r) for r in people}

    def taken_slug(slug: str) -> bool:
        return slug in slugs or slug in taken or named_after(slug, view, out_dir)

    for row in current:
        slug = slug_of(row)
        if not slug:
            sync.rows.append(row)  # not a participant: kept as it is
            continue
        if is_excluded(row):
            sync.excluded.append(slug)
            sync.rows.append(row)
            continue
        user = matched.get(id(row))
        if user is None:
            try:
                held = holds(row, view, out_dir, checks, region)
            except Exception as exc:  # fail closed: kept, and said
                held = f"what it holds can't be checked ({redact(str(exc))})"
                sync.unchecked.append(slug)
            if held:
                sync.left.append((slug, held))
                sync.rows.append(row)
            else:
                sync.removed.append((slug, row.get("name", "")))
            continue
        name = clean_name(user.get("full_name"))
        if name and name != row.get("name", ""):
            sync.renamed.append((slug, row.get("name", ""), name))
            row = {**row, "name": name}
        else:
            sync.unchanged += 1
        sync.rows.append(row)
    for user in sorted(new, key=lambda u: int(u["user_id"])):
        uid, name = int(user["user_id"]), clean_name(user.get("full_name"))
        slug = slug_for(name, uid, taken_slug)
        slugs.add(slug)
        email, placeholder = row_address(user, view.host)
        row = {c: "" for c in ROSTER_COLUMNS}
        row.update(slug=slug, name=name, email=email, zulip_user_id=str(uid))
        base = base_slug(name) if usable(base_slug(name)) else f"p-{uid}"
        notes = [f"{base} is taken" * (slug != base), placeholder]
        note = "; ".join(filter(None, notes))
        try:
            validate_row(row)
        except ValueError as exc:
            sync.refusals.append(f"user {uid}: {exc}")
            continue
        sync.added.append((slug, row, note))
        sync.rows.append(row)
    try:
        check_duplicates(sync.rows)
    except ValueError as exc:
        sync.refusals.append(str(exc))
    return sync


def render_sync(sync: Sync, view: View) -> str:
    me = view.me
    channel = view.channel or {}
    plural, caller = "s" * (view.subscribers != 1), quoted(clean_name(me.get("full_name")))
    lines = [
        f"roster sync from #{channel.get('name', '?')} (channel {channel.get('stream_id', '?')}): "
        f"{view.subscribers} subscriber{plural}; {caller} (user {me.get('user_id')}), whose key "
        "reads it, gets no row",
        f"roster sync: {len(sync.added)} added, {len(sync.renamed)} renamed, "
        f"{len(sync.removed)} removed, {len(sync.left)} left the channel, "
        f"{len(sync.excluded)} excluded, {sync.unchanged} unchanged",
    ]
    if channel and not channel.get("invite_only"):  # read at every sync (review of #638, 4)
        lines.insert(
            0,
            f"WARNING: #{channel.get('name')} is public: anyone in the organization can join it "
            "and get a row. Make it private, and read every addition below before approving",
        )
    for slug, row, note in sync.added:  # one line each, for a phone (review of #641, 4)
        said = f"  {quoted(row['name'])}  {quoted(row['email'])}" + f"  ({note})" * bool(note)
        lines.append(f"+ {slug}  (Zulip user {row['zulip_user_id']}){said}")
    for slug, old, new in sync.renamed:
        lines.append(f"~ {slug}: name {quoted(old)} -> {quoted(new)}")
    for slug, _ in sync.removed:
        lines.append(f"- {slug}: not in the channel, and nothing is provisioned for it: removed")
    for slug, held in sync.left:
        lines.append(
            f"= {slug}: left the channel (not in it, or deactivated), but {held}: left alone; "
            "its end is the organizers' (!retire)"
        )
    lines += [f"x {slug}: excluded (!roster remove): not added again" for slug in sync.excluded]
    if sync.added:
        lines.append(
            "next, for the new rows: !launch, then !provision zulip, !provision repos and "
            "!rollout (ORGANIZERS §3: with `all` on launch day), or !adopt-spare <slug> "
            "zab-spare-N for a late joiner"
        )
    return "\n".join(lines)


# ------------------------------------------------------------- remove, restore, rename


def row_name(row: Mapping[str, str]) -> str:
    """The row's name: `name`, else its first and last name (a CSV's)."""
    first_last = " ".join(filter(None, (row.get("first_name"), row.get("last_name"))))
    return row.get("name") or first_last


def one_row(rows: list[dict[str, str]], slug: str) -> dict[str, str]:
    found = [r for r in rows if slug_of(r) == slug]
    if len(found) != 1:
        raise LookupError(f"{slug}: no such row in the roster")
    return found[0]


def edit_row(
    rows: list[dict[str, str]],
    mode: str,
    slug: str,
    new: str,
    view: View,
    out_dir: pathlib.Path,
    checks: Checks,
    region: str,
    taken: Mapping[str, str],
) -> str:
    """`--remove`, `--restore` or `--rename` of `slug` in `rows`, in place: what was done, in
    words. Raises LookupError, with the refusal's words, when it is refused."""
    row = one_row(rows, slug)
    who = quoted(row_name(row))
    if mode == "restore":
        if not is_excluded(row):
            raise LookupError(f"{slug}: not excluded: nothing to restore")
        row[EXCLUDED] = ""
        return (
            f"roster restore: {slug} ({who}): back: `all` names it again, and !roster sync matches "
            "it (and removes it if they are not in the channel)"
        )
    if mode == "remove" and is_excluded(row):
        raise LookupError(f"{slug}: excluded already: nothing to change")
    try:
        held = holds(row, view, out_dir, checks, region)
    except Exception as exc:  # fail closed
        raise LookupError(f"{slug}: what it holds can't be checked: {redact(str(exc))}") from None
    if held:
        what = "excluding" if mode == "remove" else "renaming"
        raise LookupError(f"{slug}: {what} it is refused: {held}; its end is !retire's")
    if mode == "remove":
        row[EXCLUDED] = YES
        return (
            f"roster remove: {slug} ({who}): excluded: !roster sync won't add it again, and `all` "
            f"leaves it out; nothing is provisioned for it. !roster restore {slug} brings it back"
        )
    if not usable(new):
        raise LookupError(
            f"{new}: not a slug: 1 to 32 lower-case letters, digits and inner hyphens, not a "
            "keyword of the admin commands or zab-spare-N"
        )
    if any(slug_of(r) == new for r in rows):
        raise LookupError(f"{new} is a row of this roster already")
    if new in taken:
        raise LookupError(taken_refusal(new, taken[new]))
    if named_after(new, view, out_dir):
        raise LookupError(f"{new}: something is named after it already (a bot, a sandbox, out/)")
    row["slug"] = new
    row.pop("slug_given", None)
    return f"roster rename: {slug} ({who}) -> {new}; nothing was provisioned for it"


# ---------------------------------------------------------------------- the approved changes

#: Why a sync whose only news is a row that left, and whose holdings can't be checked, is
#: refused: that row is removed if it holds nothing, so "nothing to change" can't be told (#708).
UNCHECKED = (
    "what {} holds can't be checked, so whether this sync has anything to change can't be "
    "told: request !roster sync again"
)
#: Why an apply whose changes aren't the approved ones writes nothing (Codex round 1 on #641).
CHANGED = (
    "the channel's subscribers or the roster changed since the plan was last checked, so these "
    "are not the approved changes: request !roster sync again"
)


def save_changes(path: str, listing: str) -> None:
    """A dry run's list of changes (`render_sync`), for the apply to compare (`approved`): in
    the plan's own `runs/P-n/` (0700), 0600, replaced atomically, never through a link."""
    target = pathlib.Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(listing)
    os.replace(tmp, target)


def approved(path: str) -> str:
    """The list of changes the last dry run of this plan wrote: the apply's recheck's, which
    the runner checked against the approved plan's hash."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as f:
        return f.read()


# -------------------------------------------------------------------------------- main


def main(
    argv: list[str] | None = None, *, admin: ZulipAdmin | None = None, checks: Checks | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog="roster_sync.py", description="The roster from the workshop's channel (#627)."
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--zuliprc", required=True, help="the provisioner's zuliprc")
    parser.add_argument("--out-dir", required=True, help="the bots' keys, out/<slug>/")
    parser.add_argument("--region", required=True, help="the workshop's region")
    add_tag_argument(parser)
    parser.add_argument("--retired", metavar="FILE", help="the server's retired-slugs.txt")
    parser.add_argument("--workshop-roster", action="append", default=[], metavar="WS=ROSTER")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sync", type=int, metavar="CHANNEL", help="the channel's stream ID")
    parser.add_argument(
        "--changes", metavar="FILE", help="--sync: the approved changes (a dry run writes them)"
    )
    mode.add_argument("--remove", metavar="SLUG")
    mode.add_argument("--restore", metavar="SLUG")
    mode.add_argument("--rename", nargs=2, metavar=("SLUG", "NEW"))
    parser.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    args = parser.parse_args(argv)
    if not _REGION_RE.match(args.region) or (args.sync is not None and args.sync <= 0):
        print(f"invalid region {args.region!r} or channel", file=sys.stderr)
        return 2
    if (args.sync is not None) != bool(args.changes):
        print("--sync and --changes go together", file=sys.stderr)
        return 2
    try:
        scope = parse_tags(args.tag)
        before = digest(args.roster)
        current = load_roster(args.roster) if before else []
        taken = taken_slugs(args.retired, args.workshop_roster)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 2
    out_dir, region = pathlib.Path(args.out_dir), args.region
    checks = checks or LiveChecks(None, "MathAgents", scope)  # EC2 only: `vm`
    try:
        admin = admin or ZulipAdmin(*load_zuliprc(args.zuliprc))
    except (OSError, ValueError) as exc:
        print(f"zuliprc: {exc}", file=sys.stderr)
        return 2
    try:
        view = read_view(admin, args.sync)
    except (LookupError, ZulipAdminError, httpx.HTTPError, KeyError, TypeError) as exc:
        print(f"refused: {redact(str(exc))}", file=sys.stderr)
        print("nothing written", file=sys.stderr)
        return 1
    finally:
        admin.close()
    if args.sync is not None:
        sync = plan_sync(current, view, out_dir, checks, region, taken)
        if sync.refusals:
            for refusal in sync.refusals:
                print(f"refused: {refusal}", file=sys.stderr)
            print("nothing written", file=sys.stderr)
            return 1
        listing = render_sync(sync, view)
        print(listing)
        if not sync.changes() and sync.unchecked:  # #708: never "nothing" on a failed check
            print(f"refused: {UNCHECKED.format(', '.join(sync.unchecked))}", file=sys.stderr)
            print("nothing written", file=sys.stderr)
            return 1
        if not sync.changes():
            print("nothing to change: every subscriber has a row, and every row is theirs")
            return NOTHING if args.dry_run else 0
        try:
            if args.dry_run:
                save_changes(args.changes, listing)
            elif approved(args.changes) != listing:
                print(f"refused: {CHANGED}; nothing written", file=sys.stderr)
                return 1
        except OSError as exc:
            print(
                f"refused: the approved changes: {exc.strerror}; nothing written", file=sys.stderr
            )
            return 1
        rows = sync.rows
    else:
        rows = current
        verb, slug = next((m, v) for m in ("remove", "restore", "rename")
                          if (v := getattr(args, m)) is not None)  # fmt: skip
        slug, new = (slug[0], slug[1]) if verb == "rename" else (slug, "")
        try:
            print(edit_row(rows, verb, slug, new, view, out_dir, checks, region, taken))
            for row in rows:
                if slug_of(row):
                    validate_row(row)
            check_duplicates(rows)
        except (LookupError, ValueError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            print("nothing written", file=sys.stderr)
            return 1
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    try:
        if digest(args.roster) != before:
            print("refused: the roster changed during this run; nothing written", file=sys.stderr)
            return 1
        write_roster(args.roster, rows)
    except OSError as exc:
        print(f"roster NOT written: {exc}", file=sys.stderr)
        return 1
    kept = f" (the previous one is {args.roster}.bak)" if before else ""
    print(f"written: {args.roster}{kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
