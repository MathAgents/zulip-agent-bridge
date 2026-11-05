#!/usr/bin/env python3
"""Move the participants' bots to `zab-provisioner`, or back to the caller (docs/ADMIN.md §1.5,
§8.4). A dry run unless `--apply`.

    uv run python workshop/admin/move_bots.py --roster ROSTER [--keep keep.txt | --only SLUG …] \\
        --zuliprc ~/.zuliprc (--to provisioner --provisioner-name zab-provisioner \\
        --provisioner-id ID [--provisioner-zuliprc FILE] | --to me) \\
        [--out-dir workshop/admin/out] [--apply]

Zulip lets a bot post wherever its owner can (`access_stream_for_send_message`), and a
participant's agent can read its bots' keys (SPEC §12.2). Bots that Drew owns can therefore post
in every private channel Drew is in, the admin bots' channels among them. Owned by
`zab-provisioner`, which is never subscribed to those, they can't (§1.5).

**Why the move comes before the server provisions the fleet.** The provisioner's key finds only
the bots it owns (`GET /bots`, the caller's bots). So, before the move, `zulip_provision.py`
with that key tries to create each row's bots again. Zulip refuses the second bot, whose e-mail
and full name are taken (`add_bot_backend`, `check_bot_name_available`), and the row fails, while
the dry run says `would create`. `--deactivate` reports the bots `absent` and leaves them
active. The same holds in reverse for Drew's key once the bots have moved: the fallback moves
them back first.

**The new owner can read every moved bot's key** (`GET /bots` returns it), so who that is, is
fixed:
- `--to provisioner`: the user whose ID, which can't change, is `--provisioner-id`, read by
  that ID (`GET /users/{id}`), and whose full name must be exactly `--provisioner-name`. The
  runbook gives both. Never by e-mail: a key can't read an address whose visibility excludes
  its role, as a Member's couldn't read the provisioner's on the rig on Oct 15. Its
  `delivery_email` is then null, its `email` a dummy, and `GET /users/{address}` answers "No
  such user" (#396). `--provisioner-zuliprc`, the provisioner's zuliprc, is optional, and only
  its e-mail is read, never its key. When it is given, its e-mail must also be the user's
  address, if the caller's key can read that address (an owner's can, unless the visibility
  is "nobody"). The dry run says whether it was checked;
- `--to me`: the caller, for the fallback (the cutover's step 2, ADMIN §8.4 at v0.2.15).

Either must be an active human, and no roster participant (by user ID, or by e-mail where the
key sees it) unless an organization owner, checked live: Drew, an owner, is a kept participant
himself. The dry run prints the target's name, e-mail (or that the key can't see it) and role.
A bot moves only when its full name is `Claude · <slug>` or `Codex · <slug>` for its own row.

Each row's bots are the ones whose keys `zulip_provision.py` wrote, `out/<slug>/codex.zuliprc`
and `claude.zuliprc` (only the e-mail is read). Each is looked up by that e-mail (`GET
/users/{email}`: a bot's address is visible to every key, a Member's too), moved with `PATCH
/bots/{bot_id}` and `bot_owner_id`, and read back (`GET /users/{id}`). `PATCH /bots/{bot_id}`
is the endpoint Zulip's web app uses; its API reference doesn't list it (VERIFY V15 in
docs/ADMIN.md). Moving changes no subscription, key or name. A bot the new owner has already is
left alone, so a rerun changes nothing. Without `--apply` it makes only GET requests. Keys are
never printed.

**`--zuliprc` must be an organization administrator's key** (role 200 or less: an organization
owner is one). Zulip's open-source code also lets the bot's owner change its owner
(`access_bot_by_id`), but since Nov 2, 2026 Zulip Cloud refuses it to any other key, the bot's
own owner's included: `400 UNAUTHORIZED_PRINCIPAL`, "Must be an organization administrator"
(#758; DECISIONS, verification log). The caller's role is read (`GET /users/me`), and a key
that isn't an administrator's is said so before the table. The dry run then fails (exit 1) when
a bot would move, each such row saying that it needs an administrator's key, since `--apply`
would be refused for it on Zulip Cloud; with nothing to move it passes, as `--apply` would
change nothing. `--apply` doesn't decide by the role: it asks, and Zulip's refusal fails the
row, naming the cause. **On a self-hosted Zulip**, whose policy may still let the bot's owner
change it, as the open-source code does, a dry run that fails this way doesn't mean `--apply`
would be refused there: the bot's owner may still run `--apply`, which then moves the bot or
fails with Zulip's own answer.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

import httpx
from admin_common import (
    format_table,
    keys_dir,
    load_roster,
    load_zuliprc,
    read_keep,
    select_rows,
    slug_of,
)
from zulip_provision import BOT_KINDS, OUT_DIR, ZulipAdmin, ZulipAdminError, bot_full_name

#: The names zulip_provision.py gives (README.md §1, step 2).
PARTICIPANT_BOTS = ("Claude · ", "Codex · ")
#: Zulip's roles (`UserProfile.ROLE_*`), as `GET /users/{id}` gives them.
ROLES = {100: "organization owner", 200: "administrator", 300: "moderator", 400: "member",
         600: "guest"}  # fmt: skip
OWNER, ADMINISTRATOR = 100, 200
#: Why this key can't change a bot's owner: Zulip Cloud's policy since Nov 2 (#758).
NEEDS_ADMIN = (
    "Zulip Cloud lets only an organization administrator change a bot's owner, not even the "
    "bot's owner (#758)"
)


def administrator(user: dict[str, Any]) -> bool:
    """Whether `user`, as `GET /users/me` gives it, is an organization administrator or an
    organization owner."""
    role = user.get("role")
    return type(role) is int and role <= ADMINISTRATOR


def not_administrator(me: dict[str, Any]) -> str:
    """The line that says the caller's key isn't an administrator's."""
    role = ROLES.get(me.get("role"), f"role {me.get('role')}")
    return (
        f"NOT AN ADMINISTRATOR'S KEY: {me.get('full_name')} ({me.get('user_id')}) is a {role}. "
        f"{NEEDS_ADMIN}: use an administrator's or an organization owner's key"
    )


def addresses(user: dict[str, Any]) -> set[str]:
    return {str(user.get(key) or "").casefold() for key in ("email", "delivery_email")} - {""}


def new_owner(
    admin: ZulipAdmin,
    to: str,
    me: dict[str, Any],
    provisioner: tuple[str | None, str | None, int | None],
    rows: list[dict],
) -> tuple[dict[str, Any], str]:
    """`--to`'s user, the provisioner or the caller, an active human; and what was checked of
    `--provisioner-zuliprc` ("" when there is none). Either is read by user ID (`GET
    /users/{id}`), never by e-mail (#396). A roster participant is refused unless an
    organization owner (checked live): an owner can manage every bot already, so the move gives
    them nothing new. Drew, an owner, is a kept participant (`andrew-sutherland`), and the
    fallback gives the bots back to him (the cutover's step 2, ADMIN §8.4 at v0.2.15)."""
    zuliprc, name, uid = provisioner
    if to == "me":
        user = admin.user(int(me["user_id"]))
    elif name and uid:
        user = admin.user(uid)
    else:
        raise ValueError("--to provisioner needs --provisioner-name and --provisioner-id")
    if user.get("is_bot") is not False or user.get("is_active") is not True:
        raise ValueError(f"--to {to}: not an active human user")
    people = {str(row.get("zulip_user_id") or "").strip() for row in rows} - {""}
    emails = {str(row.get("email") or "").strip().casefold() for row in rows} - {""}
    participant = str(user["user_id"]) in people or bool(addresses(user) & emails)
    if participant and user.get("role") != OWNER:
        raise ValueError(f"--to {to}: a participant of this roster, and no owner: refused")
    if to == "me":
        return user, ""
    if str(user.get("full_name", "")).strip() != name or user.get("user_id") != uid:
        raise ValueError(
            f"user {uid} is named {user.get('full_name')!r} ({user.get('user_id')}), "
            f"not {name!r}: refused"
        )
    return user, zuliprc_check(user, load_zuliprc(zuliprc)[1]) if zuliprc else ""


def hidden(user: dict[str, Any]) -> bool:
    """Whether the caller's key can't see this user's real address: `delivery_email` is null
    (Zulip 7.0, feature level 163, and later), and `email` is then a dummy (DECISIONS, Oct 4)."""
    return "delivery_email" in user and not user["delivery_email"]


def zuliprc_check(user: dict[str, Any], email: str) -> str:
    """What `--provisioner-zuliprc`'s e-mail says of `user`, the provisioner found by ID: that
    it is the user's address, or nothing, when the user's visibility keeps that address from
    the caller's key (#396). ValueError when the key sees another address."""
    if email.strip().casefold() in addresses(user):
        return "--provisioner-zuliprc: its e-mail is this user's"
    if not hidden(user):
        raise ValueError(
            f"--provisioner-zuliprc holds {email}, which is not the address of user "
            f"{user.get('user_id')}: refused"
        )
    return (
        "--provisioner-zuliprc: its e-mail is not checked, since this key can't see the user's "
        "address; the user is pinned by ID and full name"
    )


def bot_email(out_dir: pathlib.Path, row: dict[str, str], kind: str) -> str:
    path = keys_dir(out_dir, row)[0] / f"{kind}.zuliprc"
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"no regular {kind}.zuliprc for {slug_of(row)} in {path.parent}")
    return load_zuliprc(str(path))[1]


def move(
    admin: ZulipAdmin,
    email: str,
    name: str,
    owner: dict[str, Any],
    *,
    apply: bool,
    may: bool = True,
) -> str:
    """One bot's result. `may`: whether the caller's key is an administrator's. Without one, a
    dry run fails a bot that would move (ValueError), as `--apply` would."""
    bot = admin.user_by_email(email)
    if not bot.get("is_bot") or email.casefold() not in addresses(bot):
        raise ValueError(f"{email} is not a bot")
    if str(bot.get("full_name", "")).casefold() != name.casefold():
        raise ValueError(f"{email} is named {bot.get('full_name')!r}, not {name!r}: refused")
    if bot.get("is_active") is False:
        return "deactivated: left alone"
    old, uid = bot.get("bot_owner_id"), int(bot["user_id"])
    if old == owner["user_id"]:
        return "ok: owned already"
    if not apply and not may:
        raise ValueError(f"would move from user {old}, but needs an administrator's key")
    if not apply:
        return f"would move from user {old}"
    try:
        admin.request("PATCH", f"bots/{uid}", bot_owner_id=int(owner["user_id"]))
    except ZulipAdminError as exc:
        if exc.code == "UNAUTHORIZED_PRINCIPAL":
            raise ValueError(f"not moved: {NEEDS_ADMIN}; {exc}") from exc
        raise
    after = admin.user(uid).get("bot_owner_id")
    if after != owner["user_id"]:
        raise ValueError(f"the owner reads {after} after the move")
    return f"ok: moved from user {old}"


def still_owned(admin: ZulipAdmin, me: dict[str, Any]) -> str:
    """The participant bots the caller still owns (`GET /bots`), by name: never a key."""
    try:
        names = [str(bot.get("full_name", "")) for bot in admin.bots()]
    except (ZulipAdminError, httpx.HTTPError) as exc:
        return f"could not list the bots {me.get('full_name')} owns: {exc}"
    left = sorted(name for name in names if name.startswith(PARTICIPANT_BOTS))
    return f"{len(left)} participant bots still owned by {me.get('full_name')}" + (
        f": {', '.join(left)}" if left else ""
    )


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--roster", required=True)
    parser.add_argument(
        "--zuliprc",
        required=True,
        help="an organization administrator's or organization owner's key (#758)",
    )
    parser.add_argument("--to", required=True, choices=["provisioner", "me"])
    parser.add_argument(
        "--provisioner-zuliprc",
        help="with --to provisioner, optional: its e-mail only, checked where the key can see it",
    )
    parser.add_argument(
        "--provisioner-name", help="with --to provisioner: its exact full name (zab-provisioner)"
    )
    parser.add_argument(
        "--provisioner-id", type=int, help="with --to provisioner: its user ID, looked up by ID"
    )
    parser.add_argument("--out-dir", type=pathlib.Path, default=OUT_DIR)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--only", action="append", metavar="SLUG")
    which.add_argument("--keep", metavar="FILE", help="the kept slugs, one per line")
    parser.add_argument("--apply", action="store_true", help="move; without it, a dry run")
    args = parser.parse_args(argv)
    admin = None
    try:
        roster = load_roster(args.roster)
        rows = select_rows(roster, read_keep(args.keep) if args.keep else args.only)
        bots = [
            (slug_of(r), kind, bot_email(args.out_dir, r, kind), bot_full_name(kind, slug_of(r)))
            for r in rows
            for kind in BOT_KINDS
        ]
        admin = ZulipAdmin(*load_zuliprc(args.zuliprc), transport=transport)
        me = admin.me()
        provisioner = (args.provisioner_zuliprc, args.provisioner_name, args.provisioner_id)
        owner, checked = new_owner(admin, args.to, me, provisioner, roster)
        shown = owner.get("delivery_email") or owner.get("email")
        shown = "address hidden from this key" if hidden(owner) else shown
        role = ROLES.get(owner.get("role"), f"role {owner.get('role')}")
        print(
            f"{'' if args.apply else 'DRY RUN: '}as {me.get('full_name')} ({me.get('user_id')})"
            f", to {owner.get('full_name')} <{shown}> ({owner['user_id']}, {role})"
        )
        if checked:
            print(checked)
        may = administrator(me)
        if not may:
            print(not_administrator(me))
        table = []
        for slug, kind, email, name in bots:
            try:
                result = move(admin, email, name, owner, apply=args.apply, may=may)
            except (ValueError, KeyError, ZulipAdminError, httpx.HTTPError) as exc:
                result = f"FAILED: {exc}"
            table.append([slug, kind, result])
        print(format_table(["slug", "bot", "result"], table))
        print(still_owned(admin, me))
    except (ValueError, OSError, KeyError, ZulipAdminError, httpx.HTTPError) as exc:
        print(f"move refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if admin is not None:
            admin.close()
    failed = sum(row[2].startswith("FAILED") for row in table)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
