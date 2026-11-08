#!/usr/bin/env python3
"""`zulip_provision.py --roster roster.csv --zuliprc ~/.zuliprc` (README.md §1, §7.3).

For every roster row: look the participant's Zulip user up by the roster e-mail (the only
thing that binds a user: the custom profile field "GitHub", BOOTSTRAP §5.7, is self-reported and
unverified, so it is only reported, never followed); create the generic bots `Codex · <slug>`
and `Claude · <slug>`; create the private channel `<slug>-sandbox`, with shared history, with
both bots (and the participant, if already a user) subscribed and the organizer NOT subscribed;
write `out/<slug>/{codex,claude}.zuliprc` (mode 0600). The participant's Claude bot posts the
two workshop steps in a "Start here" topic, and the channel description carries them too.
`zulip_user_id` and `zulip_invite` are filled in in the roster.

It sends no invitation (#628, Drew, Oct 25): the organizers invite participants by e-mail in
Zulip's own UI, with the participants' channel ticked (ORGANIZERS §1.5). A row whose address
matches no Zulip user that this key can see is reported as `not on Zulip` in the invite column,
with a line after the table that tells the organizers to invite that address in Zulip; it is not an
error, and the next run binds the participant once they have joined. A participant who joined
but hides their address from this key is reported the same way (ORGANIZERS §3.3). Before 0.2.10
it sent a personal invitation (`POST /invites`), which a Moderator's key may not send (#610).

An organizer who is not subscribed to a private channel may add subscribers to it only through
the channel's `can_subscribe_group` or `can_add_subscribers_group`, both Nobody by default
(`filter_stream_authorization_for_adding_subscribers`, zerver/lib/streams.py on Zulip's main
branch, server feature level 511). So each sandbox is created (`POST /channels/create`, feature
level 417) with `can_add_subscribers_group` = the organizer running the script, who stays
unsubscribed but can subscribe the participant once they have joined and repair its membership
on reruns.
The participant administers their sandbox (Drew, issue #36, Tue 06:30): they may invite
collaborators, who see its whole history (`history_public_to_subscribers`), make it public and
change its history. A channel administrator may add subscribers, bots they don't own included,
to a channel they can access (`get_streams_to_which_user_cannot_add_subscribers`,
`bulk_principals_to_user_profiles` with `allow_bots=True`), change its history, and make it public
if they may create public channels (`update_stream_backend`); DECISIONS. A sandbox created for a
joined participant gets `can_administer_channel_group` = them; one created earlier (`--hold`, or
before they joined Zulip) is handed over on their first run as a member, that is
while the roster's `zulip_invite` is not yet `joined` (`hand_over_channel`: they are added to the
administrators, and protected history becomes shared). Later runs never change either, so a
participant who switches back to protected history keeps it, and they never change a sandbox's
privacy: a public sandbox is reconciled like a private one. The roster's `zulip_channel_id`
finds a sandbox its participant renamed.
`--hold` (PM on issue #36, Sun 22:40: provisioning happens on Wednesday, the participants start on
Friday) creates or reuses the bots and writes their zuliprc files, which `push_config.py` needs,
and creates the sandbox with only the two bots subscribed and a neutral description
(`HOLD_DESCRIPTION`). It subscribes no participant and posts no "Start
here"; the report says `held`, and the roster changes only in `zulip_user_id` and
`zulip_channel_id`. The same command
without `--hold` (Friday morning) then does everything above, and a held sandbox gets its full
description, its participant and "Start here". With `--hold`, an existing channel's description is
left as it is (only a new channel gets `HOLD_DESCRIPTION`), so a `--hold` run after a full one
changes nothing a participant can see.
In "Start here" and in the full description the two bot names are links that open a direct
message with that bot (`#narrow/dm/<user id>-<slug>`, `dm_link`), so that a participant picking
a bot for `!login` out of ~80 does not DM someone else's (PM on issue #36, Mon 07:55).
A Zulip user matched by two rows is bound to neither, and a row that is ambiguous or unmatched
has its `zulip_user_id` cleared, so a stale id never survives a doubtful run (PM review of PR
#47). Idempotent: existing bots and channels are reused, and a bot's key is only regenerated
with `--regenerate-keys`. `--deactivate` deactivates the roster's bots, whose keys then stop
working; a reactivation revives every copy of the old key, which `--regenerate-keys` replaces
(README.md §7.3). `--dry-run` performs only GET requests.
`--no-rebind` (the admin bot always passes it; docs/ADMIN.md §2.4) never replaces or clears a
recorded `zulip_user_id`: a row whose e-mail now matches another user, or none, keeps its id, gets
no bot, channel or permission change, and is reported (`binding changed: refused`).
`--repo-owner` is the owner "Start here" names when the roster's `repo` has none (MathAgents).
`--refserve-url` is the file server whose host it names, as `push_config.py` gives it to the
bridges, and `--gifts-step` (the admin bot's `--gifts-step-b64`, admin.toml's `gifts_step`) the
workshop's own first sentence of its step 1 and of the description, about gift subscriptions
(#709).

Bot endpoints (README.md §1; shapes read from Zulip's `zerver/views/users.py`, `get_bots_backend`
and `add_bot_backend`, 2026-10-02): `GET /bots` lists the caller's bots with `username` (the bot's
e-mail), `full_name` and `api_key` but no `user_id`; `POST /bots` (`full_name`, `short_name`,
`bot_type=1`) returns `user_id` and `api_key` but no e-mail. The ids of existing bots therefore
come from the `GET /users` listing (bots included, `email` → `user_id`), or, for a bot the
listing leaves out, from `GET /users/{its e-mail}` (Oct 8 in issue #36, and #396), and a new
bot's e-mail from `GET /users/{user_id}`. A zuliprc is written only when both are present.
`POST /bots` is the endpoint the web app uses but the API reference does not list: VERIFY it on
the org before the rollout. The custom field query relies on
`GET /users?include_custom_profile_fields=true` returning `profile_data` keyed by the field id.

Standalone: `uv run python workshop/admin/zulip_provision.py …` from a checkout. Uses `httpx`
(a zab dependency). Keys are written to the zuliprc files only; nothing prints them.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    REFSERVE_URL,
    format_table,
    load_roster,
    load_zuliprc,
    move_legacy_keys,
    read_roster,
    select_rows,
    slug_of,
    write_roster,
)

GITHUB_FIELD = "github"  # the custom profile field's name, compared case-insensitively
BOT_KINDS = ("codex", "claude")
OUT_DIR = pathlib.Path(__file__).resolve().parent / "out"  # git-ignored (.gitignore)
REPO_OWNER = "MathAgents"  # the GitHub organization of the workshop repositories (issue #36)
#: A GitHub user or organization name (`--repo-owner`), as provision_repos.py checks `--org`.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
#: The invite column of a row with no Zulip user at its address visible to this key: the
#: organizers invite it in Zulip (#628), and `invite_line`'s line is printed after the table.
NOT_ON_ZULIP = "not on Zulip"
START_TOPIC = "Start here"
#: What participants are told about agent subscriptions, by default: the first sentence of step 1
#: of "Start here", and of the sandbox's description. A workshop says its own with `--gifts-step`
#: (admin.toml's `gifts_step`, which the admin bot passes; #709): its redemption e-mail, a form's
#: address; docs/quickstart.md's step 1 says it generically. "Any", not
#: "your": not everyone gets the gifts (Drew, Wed Oct 7). "Start here" is posted once, but a
#: full run rewrites a sandbox's description that differs, so an edit reaches existing ones.
#: The gifts are Claude Max and ChatGPT Pro (Drew, Oct 18: SLmath's participants who don't
#: already have them get both; #475).
GIFTS_STEP = (
    "Accept any gift subscriptions: Claude Max (from Anthropic) and ChatGPT Pro (from OpenAI)."
)
#: A `--gifts-step` is one line of at most this many bytes in UTF-8, without control characters,
#: starting with neither whitespace nor `-` and not ending with whitespace: admin.toml's rule for
#: `gifts_step` (`zab.admin.config`), checked again here, for a hand run and for what the admin
#: bot's `--gifts-step-b64` decodes to.
GIFTS_STEP_BYTES = 500
#: The dry run's line for a workshop's own gift step, which the plan shows (#709).
GIFTS_SHOWN = '"Start here" step 1 and the description begin: {}'
_NOT_TEXT = r"\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff"
_GIFTS_STEP_RE = re.compile(rf"[^\s{_NOT_TEXT}-](?:[^{_NOT_TEXT}]*[^\s{_NOT_TEXT}])?")
#: The sandbox's description until the workshop starts (`--hold`).
HOLD_DESCRIPTION = (
    "Your workshop sandbox. Instructions will be posted here on the first morning of the workshop."
)


# ------------------------------------------------------------------------------------- client


#: Zulip Cloud's per-user API rate limit (PM on issue #36, Wed 10:39: Wednesday's `--hold` run hit
#: it after about 10 of 35 rows). A 429 is refused before the request is processed
#: (`RateLimitMiddleware`), so every call, POST and PATCH included, is retried after the server's
#: wait, at most `RATE_LIMIT_ATTEMPTS` attempts in all, each wait capped.
RATE_LIMIT_ATTEMPTS = 6
RATE_LIMIT_MAX_WAIT_S = 90.0
RATE_LIMIT_DEFAULT_WAIT_S = 5.0
_sleep = time.sleep  # tests replace both
_clock = time.time


def _seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds == seconds else None  # not NaN


def _until_reset(response: httpx.Response) -> float | None:
    """Seconds until `X-RateLimit-Reset` (an epoch second, truncated by the server: +1)."""
    reset = _seconds(response.headers.get("X-RateLimit-Reset"))
    return None if reset is None else reset - _clock() + 1


def rate_limit_wait(response: httpx.Response, data: Mapping[str, Any] | None) -> float:
    """How long to wait after a 429: `Retry-After`, else the body's `retry-after`, else until
    `X-RateLimit-Reset`, else `RATE_LIMIT_DEFAULT_WAIT_S`; at least 0, at most the cap."""
    candidates = (
        _seconds(response.headers.get("Retry-After")),
        _seconds((data or {}).get("retry-after")),
        _until_reset(response),
    )
    wait = next((w for w in candidates if w is not None), RATE_LIMIT_DEFAULT_WAIT_S)
    return min(max(wait, 0.0), RATE_LIMIT_MAX_WAIT_S)


class ZulipAdminError(Exception):
    def __init__(self, code: str, status: int, msg: str = "") -> None:
        super().__init__(f"Zulip {code} (HTTP {status}){': ' + msg if msg else ''}")
        self.code = code
        self.status = status


class ZulipAdmin:
    """The handful of REST calls the script makes, as Drew's admin user.

    Tests replace it with a fake that records calls. Values that are not strings are
    JSON-encoded, as the Zulip API expects. Error messages never include request bodies.
    """

    def __init__(
        self, site: str, email: str, key: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.site = site.rstrip("/")
        self.http = httpx.Client(
            base_url=self.site + "/api/v1/",
            auth=(email, key),
            headers={"User-Agent": "zab-workshop-admin"},
            timeout=httpx.Timeout(60, connect=10),
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, endpoint: str, **params: Any) -> dict[str, Any]:
        if endpoint.startswith("/") or ":" in endpoint or ".." in endpoint:
            raise ValueError("expected a relative Zulip API endpoint")
        encoded = {
            k: v if isinstance(v, str) else json.dumps(v)
            for k, v in params.items()
            if v is not None
        }
        kwargs = {"params": encoded} if method == "GET" else {"data": encoded}
        for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
            response = self.http.request(method, endpoint, **kwargs)
            try:
                data = response.json()
            except ValueError:
                data = None
            limited = response.status_code == 429 or (
                isinstance(data, dict) and data.get("code") == "RATE_LIMIT_HIT"
            )
            if limited and attempt < RATE_LIMIT_ATTEMPTS:
                wait = rate_limit_wait(response, data if isinstance(data, dict) else None)
                print(
                    f"zulip: rate limited, waiting {wait:.0f} s "
                    f"(attempt {attempt + 1}/{RATE_LIMIT_ATTEMPTS})",
                    file=sys.stderr,
                )
                _sleep(wait)
                continue
            break
        if not isinstance(data, dict):
            raise ZulipAdminError("INVALID_RESPONSE", response.status_code)
        if response.is_error or data.get("result") == "error":
            raise ZulipAdminError(
                str(data.get("code", "HTTP_ERROR")), response.status_code, str(data.get("msg", ""))
            )
        if response.headers.get("X-RateLimit-Remaining", "").strip() == "0":
            # the last call of this window: wait for the reset now rather than meet a 429 on the
            # next call (PM on issue #36, Wed 10:39)
            wait = min(max(_until_reset(response) or 0.0, 0.0), RATE_LIMIT_MAX_WAIT_S)
            if wait > 0:
                print(f"zulip: rate limit reached, pausing {wait:.0f} s", file=sys.stderr)
                _sleep(wait)
        return data

    # --- reads

    def profile_fields(self) -> list[dict[str, Any]]:
        return self.request("GET", "realm/profile_fields")["custom_fields"]

    def users(self) -> list[dict[str, Any]]:
        return self.request("GET", "users", include_custom_profile_fields=True)["members"]

    def bots(self) -> list[dict[str, Any]]:
        """The caller's own bots with `username` (e-mail), `full_name` and `api_key`; no id."""
        return self.request("GET", "bots")["bots"]

    def user(self, user_id: int) -> dict[str, Any]:
        """One user (bots included): `user_id`, `email`, `full_name`, `is_bot`, …"""
        return self.request("GET", f"users/{int(user_id)}")["user"]

    def user_by_email(self, email: str) -> dict[str, Any]:
        """One user (bots included) by e-mail: `GET /users/{email}`."""
        return self.request("GET", f"users/{urllib.parse.quote(email, safe='@')}")["user"]

    def streams(self) -> list[dict[str, Any]]:
        return self.request("GET", "streams", include_all_active=True)["streams"]

    def subscriptions(self) -> list[dict[str, Any]]:
        """The caller's own channels (`GET /users/me/subscriptions`): `stream_id`, `name`,
        `invite_only`, … A private channel the caller isn't in is never among them (#627)."""
        return self.request("GET", "users/me/subscriptions")["subscriptions"]

    def me(self) -> dict[str, Any]:
        return self.request("GET", "users/me")

    def invites(self) -> list[dict[str, Any]]:
        """Open invitations this organizer controls (`email`, `expiry_date`, `is_multiuse`, …)."""
        return self.request("GET", "invites")["invites"]

    def revoke_invite(self, invite_id: int) -> None:
        """`DELETE /invites/{id}`: any key may revoke its own invitations, an administrator's
        others' too (`access_invite_by_id`, zerver/views/invite.py)."""
        self.request("DELETE", f"invites/{int(invite_id)}")

    def topic_messages(self, stream_id: int, topic: str) -> list[dict[str, Any]]:
        narrow = [
            {"operator": "channel", "operand": int(stream_id)},
            {"operator": "topic", "operand": topic},
        ]
        return self.request(
            "GET", "messages", anchor="newest", num_before=5, num_after=0, narrow=narrow
        )["messages"]

    # --- writes

    def create_bot(self, full_name: str, short_name: str) -> dict[str, Any]:
        """`{"user_id", "api_key", …}`, no e-mail (`add_bot_backend`); VERIFY on the org
        (README.md §1)."""
        return self.request("POST", "bots", full_name=full_name, short_name=short_name, bot_type=1)

    def regenerate_key(self, bot_id: int) -> str:
        return self.request("POST", f"bots/{bot_id}/api_key/regenerate")["api_key"]

    def create_channel(
        self,
        name: str,
        description: str,
        subscribers: list[int],
        organizer_id: int,
        admin_id: int | None = None,
    ) -> int:
        """A private channel with shared history and these subscribers only; the organizer may
        add subscribers without being one, and `admin_id` (the
        participant) administers it; without one, Zulip's default makes the organizer, its
        creator, the administrator. Returns its id."""
        return int(
            self.request(
                "POST",
                "channels/create",
                name=name,
                description=description,
                subscribers=subscribers,
                invite_only=True,
                history_public_to_subscribers=True,
                announce=False,
                can_add_subscribers_group={
                    "direct_members": [organizer_id],
                    "direct_subgroups": [],
                },
                can_administer_channel_group=(
                    None
                    if admin_id is None
                    else {"direct_members": [admin_id], "direct_subgroups": []}
                ),
            )["id"]
        )

    def send_message(self, stream_id: int, topic: str, content: str) -> None:
        self.request(
            "POST", "messages", type="stream", to=int(stream_id), topic=topic, content=content
        )

    def subscribe(self, name: str, description: str, principals: list[int]) -> dict[str, Any]:
        return self.request(
            "POST",
            "users/me/subscriptions",
            subscriptions=[{"name": name, "description": description}],
            principals=principals,
            invite_only=True,
            announce=False,
            authorization_errors_fatal=False,
        )

    def set_description(self, stream_id: int, description: str) -> None:
        self.request("PATCH", f"streams/{stream_id}", description=description)

    def update_channel(self, stream_id: int, **changes: Any) -> None:
        """`PATCH /streams/{id}`: e.g. `history_public_to_subscribers`, or a group setting as
        `{"new": …, "old": …}` (refused when `old` no longer matches)."""
        self.request("PATCH", f"streams/{stream_id}", **changes)

    def deactivate_bot(self, bot_id: int) -> None:
        """`DELETE /bots/{id}` (`deactivate_bot_backend`). Not `DELETE /users/{id}`: that one
        refuses bots with "No such user" (`access_user_by_id(..., allow_bots=False)`,
        zerver/views/users.py on main; seen on the org on Sun Oct 11)."""
        self.request("DELETE", f"bots/{bot_id}")


# ---------------------------------------------------------------------------------- matching


def github_field_id(fields: list[dict[str, Any]]) -> int | None:
    """The id of the custom profile field named GitHub (BOOTSTRAP §5.7), or None if there is
    none."""
    for f in fields:
        if str(f.get("name", "")).strip().lower() == GITHUB_FIELD:
            return int(f["id"])
    return None


def _field_value(user: dict[str, Any], field_id: int | None) -> str:
    if field_id is None:
        return ""
    data = user.get("profile_data") or {}
    entry = data.get(str(field_id)) or data.get(field_id) or {}
    value = str(entry.get("value", "") if isinstance(entry, dict) else entry)
    return value.strip().lstrip("@").lower()


def _user_email(user: dict[str, Any]) -> str:
    return str(user.get("delivery_email") or user.get("email") or "").strip().casefold()


def find_user(
    users: list[dict[str, Any]], row: dict[str, str], field_id: int | None
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """The participant's user, how it was matched, and warnings; or (None, reason, [])
    (README.md §1).

    Only the roster e-mail binds a user: the participant gave it, and Zulip verified it at
    signup. The GitHub profile field is self-reported and unverified, so a user carrying the
    login is never bound through it, however unique the claim (anyone can set the field to a
    login before its owner joins; codex round 4 on PR #47). The field is only reported: as a
    hint for an unmatched row, and as a warning when it disagrees with the e-mail match.
    """
    humans = [u for u in users if not u.get("is_bot") and u.get("is_active", True)]
    login = (row.get("github") or "").casefold()  # optional (issue #36): only a hint anyway
    email = (row.get("email") or "").strip().casefold()
    by_email = [u for u in humans if email and _user_email(u) == email]
    by_field = [u for u in humans if login and _field_value(u, field_id) == login]
    if len(by_email) > 1:
        return None, f"ambiguous: {len(by_email)} users have e-mail {email}", []
    if len(by_email) == 1:
        user = by_email[0]
        warnings = []
        others = [u for u in by_field if u.get("user_id") != user.get("user_id")]
        if others:
            ids = ", ".join(str(u.get("user_id")) for u in others)
            warnings.append(
                f"GitHub field {row['github']} is also set by user(s) {ids}; e-mail wins"
            )
        return user, "e-mail", warnings
    warnings = []
    if by_field:
        ids = ", ".join(str(u.get("user_id")) for u in by_field)
        warnings.append(
            f"user(s) {ids} claim GitHub {row['github']} in the profile field, which is "
            "unverified: check with the participant whether the roster e-mail is right"
        )
    return None, NOT_JOINED, warnings


NOT_JOINED = "not joined"
ZULIP_ID = "zulip id"  # how a row linked by its recorded id is matched (`match_rows`, #627)


def bot_full_name(kind: str, slug: str) -> str:
    return f"{kind.capitalize()} · {slug}"


def bot_short_name(kind: str, slug: str) -> str:
    return f"{kind}-{slug}".lower()


def channel_name(slug: str) -> str:
    return f"{slug}-sandbox"


def qualified_repo(row: dict[str, str], owner: str = REPO_OWNER) -> str:
    """`owner/name` of the participant's repository: the roster's `repo` when it names an owner,
    else `<owner>/<repo or slug>` (`--repo-owner`, MathAgents by default)."""
    repo = (row.get("repo") or "").strip().rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if "/" in repo:
        return "/".join(repo.split("/")[-2:])
    return f"{owner}/{repo or slug_of(row)}"


#: Characters Zulip's web app replaces with `-` in the name part of a DM narrow
#: (`get_slug_from_full_name` in web/src/people.ts): space, `"%/<>` and backtick, controls.
_DM_SLUG_DROP = re.compile(r'[ "%/<>`\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\ufeff]+')
#: `internal_url.encodeHashComponent`: encodeURIComponent, then `%` → `.`, and `(`, `)`, `.`
#: escaped with `.` so that the result stays one hash component.
_HASH_REPLACEMENTS = {"%": ".", "(": ".28", ")": ".29", ".": ".2E"}


def dm_link(user_id: int, full_name: str) -> str:
    """The relative URL that opens a direct message with one user: `#narrow/dm/<id>-<slug>`.

    Zulip's own form (`people.user_ids_to_slug` → `get_slug_from_full_name` →
    `encodeHashComponent`, web/src on main; `dm` since feature level 177). Only the leading id is
    read back (`people.slug_to_user_ids`), so the name part is cosmetic; it is built the same way
    so that the link matches the one the web app shows. Zulip's Markdown keeps fragment-only
    links (`sanitize_url` in zerver/lib/markdown), in messages and in channel descriptions.
    """
    name = _DM_SLUG_DROP.sub("-", full_name)
    encoded = urllib.parse.quote(name, safe="-_!~*'().")
    encoded = re.sub(r"[%().]", lambda m: _HASH_REPLACEMENTS[m.group(0)], encoded)
    return f"#narrow/dm/{int(user_id)}-{encoded}"


def bot_names(slug: str, bot_ids: Mapping[str, int] | None) -> dict[str, str]:
    """`Claude · <slug>` and `Codex · <slug>`, each a DM link when its user id is known (a dry
    run before the bots exist knows none)."""
    out = {}
    for kind in ("claude", "codex"):
        name = bot_full_name(kind, slug)
        uid = (bot_ids or {}).get(kind)
        out[kind] = f"[{name}]({dm_link(uid, name)})" if uid is not None else name
    return out


def gifts_step_ok(text: str) -> bool:
    """Whether `text` may be step 1's gift sentence (`GIFTS_STEP_BYTES`, `_GIFTS_STEP_RE`)."""
    return (
        _GIFTS_STEP_RE.fullmatch(text) is not None
        and len(text.encode("utf-8")) <= GIFTS_STEP_BYTES  # no surrogate: the pattern refused it
    )


def decode_gifts_step(value: str) -> str:
    """`--gifts-step-b64`: the admin bot's form of `--gifts-step`, the base64url (RFC 4648 §5) of
    its UTF-8, since the bot's argument lists hold only a safe alphabet (docs/ADMIN.md §2.3).
    ValueError unless it decodes, strictly, to a text that `gifts_step_ok` takes, and is that
    text's one encoding (padded, as `zab.admin.actions` writes it): no other spelling passes."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value, re.ASCII):
        raise ValueError("not base64url")
    try:
        text = base64.b64decode(value, altchars=b"-_", validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("not the base64url of UTF-8 text") from exc
    if base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii") != value:
        raise ValueError("not the text's canonical base64url")
    if not gifts_step_ok(text):
        raise ValueError("not one line of text that may be step 1")
    return text


@dataclass(frozen=True, slots=True)
class Wording:
    """What "Start here" and the sandbox's description say that differs between workshops: step
    1's first sentence (`GIFTS_STEP`, or the workshop's `--gifts-step`), and the file server the
    agents' file links go to (`--refserve-url`, as `push_config.py` gives it to the bridges; ""
    when they post none, and then "Start here" names no host)."""

    gifts: str = GIFTS_STEP
    refserve_url: str = REFSERVE_URL

    @property
    def files_host(self) -> str:
        return urllib.parse.urlsplit(self.refserve_url).netloc if self.refserve_url else ""


DEFAULT_WORDING = Wording()


def channel_description(
    slug: str, bot_ids: Mapping[str, int] | None = None, wording: Wording = DEFAULT_WORDING
) -> str:
    """The workshop steps, short. Zulip turns newlines in descriptions into spaces
    (`ChannelDescription` in zerver/views/streams.py), so the description cannot also hold a
    line `repo: owner/name` for `clone_project`; the workshop does not enable `auto_clone`
    (push_config) and its repositories are cloned into `~/work/<slug>` beforehand, so the
    description carries the steps (PM on #36) and the repository goes in "Start here". The bot
    names link to a DM with each bot; with a 32-character slug and the longest gift step, the
    whole stays below Zulip's 1024 characters (`Stream.MAX_DESCRIPTION_LENGTH`)."""
    names = bot_names(slug, bot_ids)
    return (
        f"Your workshop sandbox. Start here: {wording.gifts} In ChatGPT's settings, under "
        "security, turn on device code sign-in for Codex. Then send `!login` in a **direct "
        f"message** to each bot ({names['claude']}, {names['codex']})."
    )


def start_here_text(
    slug: str,
    repo: str,
    bot_ids: Mapping[str, int] | None = None,
    wording: Wording = DEFAULT_WORDING,
) -> str:
    """The "Start here" topic, posted by the Claude bot: the sign-ins first, each as its agent's
    flow goes rather than as today's menus are, and no git terms (#709; Drew, Oct 31 and Nov 1).
    The bot names in steps 3 and 4 open a DM with each bot (PM on #36, Mon 07:55). The file host
    is code, so that Zulip doesn't link it: its root answers `Not found` (Drew, Oct 31)."""
    names = bot_names(slug, bot_ids)
    files = (
        f" Links to `{wording.files_host}` in the agents' answers open for anyone who has the "
        "link, and from any of them the rest of your repository as it was at that answer. To "
        "share one file or folder alone, ask your agent for a share link."  # #710
        if wording.files_host
        else ""
    )
    return (
        "**Welcome!** This private channel is your workshop sandbox, shared with your two "
        f"agents, **Claude · {slug}** and **Codex · {slug}**. You can invite others into it "
        "when you want to share; they see its whole history.\n\n"
        "Before you start:\n"
        f"1. {wording.gifts} Sign in to Claude and ChatGPT with the accounts that received "
        "them.\n"
        "2. In ChatGPT's settings, under security, turn on **device code sign-in** for Codex.\n"
        f"3. **Claude:** click **{names['claude']}** to open a direct message with it, and send "
        "`!login`. It replies with a link: open it, sign in, and approve. The page then shows a "
        "code: **copy it and paste it back** into the direct message. The bot answers ✅ once "
        "you're connected.\n"
        f"4. **Codex:** click **{names['codex']}** and send `!login`. It replies with a link and "
        "a short code: open the link, sign in, and **type the code there**. Nothing goes back "
        "into the direct message: the bot answers ✅ by itself once you're done.\n\n"
        "Each sign-in waits 10 minutes; if it runs out, send `!login` again.\n\n"
        "Then start a new topic in this channel and write your request: both agents answer. To "
        "talk to just one, type `@claude` or `@codex`. `!help` lists the commands.\n\n"
        'Tell your agents about yourself, e.g. "@Claude remember that I work on … and my '
        'home page is …"; both agents will know it in new sessions.\n\n'
        f"**Your files** are kept in the GitHub repository `{repo}`. To see it on GitHub, put "
        "your GitHub username in the **GitHub** field of your Zulip profile "
        "(*Settings → Profile*), and GitHub will e-mail you an invitation." + files
    )


# ------------------------------------------------------------------------------------ actions


@dataclass(slots=True)
class Report:
    slug: str
    user_id: int | None = None
    matched: str = ""
    bots: dict[str, str] = field(
        default_factory=dict
    )  # kind → "reused" | "created" | "would create"
    channel: str = ""
    invite: str = ""  # joined | not on Zulip (NOT_ON_ZULIP) | held
    start_here: str = ""  # posted | exists | would post | held
    notes: list[str] = field(default_factory=list)  # problems: the row needs attention
    warnings: list[str] = field(default_factory=list)  # reported, not failures
    blocked: bool = False  # ambiguous or shared user: nothing is provisioned for the row
    email: str = ""  # the roster's address of a row not on Zulip, for `invite_line`

    @property
    def ok(self) -> bool:
        reached = self.user_id is not None or self.invite in (NOT_ON_ZULIP, "held")
        return reached and not self.notes


def write_zuliprc(path: pathlib.Path, site: str, email: str, key: str) -> None:
    """A zuliprc readable by its owner only; the directory is 0700 too (README.md §1, step 2)."""
    if not email or not key or "\n" in email + key + site:
        raise ValueError(f"refusing to write {path.name} without an e-mail and a key")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    content = f"[api]\nemail={email}\nkey={key}\nsite={site}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o600)


def index_bots(admin: ZulipAdmin, users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`{full_name casefolded: {"user_id", "email", "api_key", "full_name"}}` for the caller's
    bots. Case-insensitive: bots created before slugs carry the login as written (`Codex ·
    AliceW`), and the slug is lower-case (codex round 2 on #97).

    `GET /bots` carries the key and the e-mail (as `username`) but no id (`get_bots_backend`);
    the id comes from the `GET /users` listing, which includes bots. A bot that the listing
    does not know keeps `user_id=None` and is reported when a row needs it.
    """
    ids = {_user_email(u): u.get("user_id") for u in users if u.get("is_bot")}
    out: dict[str, dict[str, Any]] = {}
    for bot in admin.bots():
        email = str(bot.get("username") or bot.get("email") or "").strip()
        uid = ids.get(email.casefold())
        out[str(bot.get("full_name", "")).casefold()] = {
            "user_id": int(uid) if uid is not None else None,
            "email": email,
            "api_key": str(bot.get("api_key") or ""),
            "full_name": str(bot.get("full_name", "")),
        }
    return out


def ensure_bot(
    admin: ZulipAdmin,
    bots_by_name: dict[str, dict[str, Any]],
    kind: str,
    slug: str,
    *,
    dry_run: bool,
    regenerate: bool,
) -> tuple[dict[str, Any] | None, str]:
    """Reuse the bot named `Codex · <slug>` / `Claude · <slug>` or create it (README.md §1, step 2).

    Raises `ZulipAdminError` when the bot cannot be used safely: no id (neither the listing nor
    a lookup by its e-mail knows it), no e-mail or no key (`POST /bots` returns neither an e-mail
    nor, on failure, a key).
    """
    name = bot_full_name(kind, slug)
    bot = bots_by_name.get(name.casefold())
    if bot is not None:
        if bot.get("user_id") is None:
            bot = bots_by_name[name.casefold()] = _resolve_bot_id(admin, name, bot)
        if regenerate and not dry_run:
            bot = dict(bot, api_key=admin.regenerate_key(int(bot["user_id"])))
            bots_by_name[name.casefold()] = bot
            _check_bot(name, bot)
            return bot, "key regenerated"
        _check_bot(name, bot)
        return bot, "reused"
    if dry_run:
        return None, "would create"
    created = admin.create_bot(name, bot_short_name(kind, slug))
    user_id = created.get("user_id")
    if user_id is None:
        raise ZulipAdminError("NO_USER_ID", 0, f"POST /bots for {name} returned no user_id")
    profile = admin.user(int(user_id))  # the e-mail is not in the creation reply
    bot = {
        "user_id": int(user_id),
        "full_name": name,
        "email": str(profile.get("email") or profile.get("delivery_email") or "").strip(),
        "api_key": str(created.get("api_key") or ""),
    }
    bots_by_name[name.casefold()] = bot
    _check_bot(name, bot)
    return bot, "created"


def _resolve_bot_id(admin: ZulipAdmin, name: str, bot: dict[str, Any]) -> dict[str, Any]:
    """The bot with its user id, looked up by the e-mail `GET /bots` gave (`GET /users/{email}`).

    `GET /users` can leave out a bot that exists and is active: on Thu Oct 8 the org listed
    "Codex · patrick-shafto" but not "Claude · patrick-shafto", both created in the same run,
    while `GET /users/<its e-mail>` returned it (id, `is_bot`, active; issue #36). The lookup is
    by the bot's own address from `GET /bots`, and the answer must be an active bot with that
    address, so nothing is guessed; otherwise the row keeps its NO_USER_ID error."""
    email = str(bot.get("email") or "").strip()
    profile: Any = None
    if email:
        try:
            profile = admin.user_by_email(email)
        except (ZulipAdminError, httpx.HTTPError):
            profile = None
    uid = profile.get("user_id") if isinstance(profile, dict) else None
    if (
        uid is None
        or not profile.get("is_bot")
        or profile.get("is_active") is False
        or _user_email(profile) != email.casefold()
    ):
        raise ZulipAdminError("NO_USER_ID", 0, f"{name} is not in the users listing")
    return dict(bot, user_id=int(uid))


def _check_bot(name: str, bot: dict[str, Any]) -> None:
    if not bot.get("email"):
        raise ZulipAdminError("NO_EMAIL", 0, f"{name} has no e-mail; no zuliprc written")
    if not bot.get("api_key"):
        raise ZulipAdminError("NO_API_KEY", 0, f"{name} has no API key; no zuliprc written")


def administrators_with(current: Any, user_id: int) -> dict[str, list[int]] | None:
    """`can_administer_channel_group` with `user_id` added, or None if they are already a direct
    member. Zulip gives the setting as a named group's id or as an anonymous group
    `{"direct_members": [...], "direct_subgroups": [...]}`; whoever administers it already
    keeps doing so."""
    if isinstance(current, dict):
        members = [int(m) for m in current.get("direct_members") or []]
        if user_id in members:
            return None
        subgroups = [int(g) for g in current.get("direct_subgroups") or []]
        return {"direct_members": [*members, user_id], "direct_subgroups": subgroups}
    subgroups = [int(current)] if isinstance(current, int) else []
    return {"direct_members": [user_id], "direct_subgroups": subgroups}


def hand_over_channel(
    admin: ZulipAdmin, stream: Mapping[str, Any], participant_id: int, report: Report
) -> bool:
    """The participant's first subscription (Drew, issue #36, Tue 06:30): they administer their
    sandbox, so they can invite collaborators and change its privacy and history, and its
    history is shared. Run once per participant (`provision` decides when), so a participant who
    later switches their sandbox back to protected history keeps that. False if Zulip refused."""
    changes: dict[str, Any] = {}
    group = administrators_with(stream.get("can_administer_channel_group"), participant_id)
    if group is not None:
        changes["can_administer_channel_group"] = {"new": group}
        if "can_administer_channel_group" in stream:
            changes["can_administer_channel_group"]["old"] = stream["can_administer_channel_group"]
    if stream.get("history_public_to_subscribers") is False:
        changes["history_public_to_subscribers"] = True
    if not changes:
        return True
    try:
        admin.update_channel(int(stream["stream_id"]), **changes)
    except (ZulipAdminError, httpx.HTTPError) as exc:
        report.notes.append(
            f"channel: could not make the participant its administrator: {exc}; rerun"
        )
        return False
    return True


def find_channel(
    streams: list[dict[str, Any]], slug: str, recorded_id: str
) -> dict[str, Any] | None:
    """The sandbox: by the id recorded in the roster (`zulip_channel_id`) if that channel still
    exists, since its administrator, the participant, may have renamed it; otherwise by the
    name `<slug>-sandbox` (case-insensitive, as Zulip compares channel names)."""
    recorded = recorded_id.strip()
    if recorded.isdigit():
        for stream in streams:
            if int(stream.get("stream_id", -1)) == int(recorded):
                return stream
    name = channel_name(slug).casefold()
    for stream in streams:
        if str(stream.get("name", "")).casefold() == name:
            return stream
    return None


def ensure_channel(
    admin: ZulipAdmin,
    existing: dict[str, Any] | None,
    slug: str,
    principals: list[int],
    organizer_id: int,
    *,
    dry_run: bool,
    hold: bool = False,
    bot_ids: Mapping[str, int] | None = None,
    admin_id: int | None = None,
    wording: Wording = DEFAULT_WORDING,
) -> tuple[str, int | None]:
    """The sandbox `<slug>-sandbox` (`existing`, from `find_channel`, or a new private channel
    with shared history) with the right description and members (README.md §1, step 3), and its id
    (None in a dry run before it exists).

    The organizer is not subscribed: only `principals` are (README.md §1). A new channel lets the
    organizer add subscribers (`can_add_subscribers_group`), which subscribing the participant
    and adding members on a rerun need (see the module docstring), and `admin_id`, the
    participant when they have joined, administers it. With `hold`, a new channel gets the neutral
    `HOLD_DESCRIPTION` and an existing one keeps whatever description it has: a held run after a
    full run takes nothing back, even once the full text has changed (PM review on #36, Mon
    09:30). An existing channel keeps its name, privacy and history: the participant
    administers it and may have made it public (Drew, issue #36, Tue 06:30).
    """
    description = channel_description(slug, bot_ids, wording)
    if hold:
        description = (
            HOLD_DESCRIPTION if existing is None else str(existing.get("description") or "")
        )
    if dry_run:
        return ("exists", int(existing["stream_id"])) if existing else ("would create", None)
    if existing is None:
        stream_id = admin.create_channel(
            channel_name(slug), description, principals, organizer_id, admin_id
        )
        return "created", stream_id
    stream_id = int(existing["stream_id"])
    if (existing.get("description") or "") != description:
        admin.set_description(stream_id, description)
    name = str(existing["name"])  # its current name, which the participant may have changed
    result = admin.subscribe(name, description, principals)  # idempotent for existing members
    if result.get("unauthorized"):
        raise ZulipAdminError(
            "NO_ACCESS",
            0,
            f"{name} exists, but this organizer cannot add subscribers to it (created before "
            'this script set its permissions?); set "Who can subscribe others" to include '
            "you in its settings, or recreate it, and rerun",
        )
    return "exists", stream_id


def open_invitations(admin: ZulipAdmin, now: float) -> dict[str, list[Any]]:
    """Each e-mail (casefolded) with an unexpired personal invitation, and the invitations' ids,
    for `roster_import.py`'s checks: provisioning reads none (#628).
    `GET /invites` lists the ones the key controls: its own, every one for an administrator's
    key (`do_get_invites_controlled_by_user`, zerver/actions/invites.py)."""
    out: dict[str, list[Any]] = {}
    for inv in admin.invites():
        expiry = inv.get("expiry_date")
        if inv.get("is_multiuse") or (expiry is not None and expiry <= now):
            continue
        out.setdefault(str(inv.get("email", "")).strip().casefold(), []).append(inv.get("id"))
    return out


def invite_state(row: Mapping[str, str], report: Report) -> None:
    """The invite column (#628): `joined` for a row bound to a Zulip user, else `not on Zulip`.
    No invitation is sent and none is read: the organizers invite in Zulip (ORGANIZERS §1.5), and
    a Moderator's `GET /invites` lists only its own. The same in a dry run and a real one, so a
    plan and its result say the same. A row without an e-mail can't be invited or bound."""
    if report.user_id is not None:
        report.invite = "joined"
        return
    email = (row.get("email") or "").strip()
    if not email:
        report.notes.append("no e-mail in the roster: add one, then invite it in Zulip")
        return
    report.invite, report.email = NOT_ON_ZULIP, email


def invite_line(report: Report) -> str:
    """The line after the table for a row `not on Zulip`: what the organizers do in Zulip."""
    return (
        f"{report.slug}: {NOT_ON_ZULIP}: invite {report.email} in Zulip (Invite users, with the "
        "participants' channel ticked), then provision again once they have joined"
    )


def ensure_start_here(
    bot_client: Callable[[str, str], ZulipAdmin],
    bot: dict[str, Any] | None,
    stream_id: int | None,
    slug: str,
    repo: str,
    *,
    dry_run: bool,
    bot_ids: Mapping[str, int] | None = None,
    wording: Wording = DEFAULT_WORDING,
) -> str:
    """The "Start here" topic, posted once by the participant's Claude bot (a subscriber; the
    organizer is not). Bot messages never start a turn on the bridge (SPEC §4.5 rule 1)."""
    if bot is None or stream_id is None:
        return "would post" if dry_run else ""
    if dry_run:
        return "would post"
    client = bot_client(str(bot["email"]), str(bot["api_key"]))
    try:
        if client.topic_messages(stream_id, START_TOPIC):
            return "exists"
        client.send_message(stream_id, START_TOPIC, start_here_text(slug, repo, bot_ids, wording))
        return "posted"
    finally:
        client.close()


def match_rows(
    rows: list[dict[str, str]],
    users: list[dict[str, Any]],
    field_id: int | None,
    *,
    no_rebind: bool = False,
    reserved: Mapping[str, str] | None = None,
) -> list[Report]:
    """README.md §1, step 1, for every row, then the cross-row rule: one Zulip user, one row.

    Rows that end up without a user (unmatched, ambiguous, or sharing their user with another
    row) get `zulip_user_id` cleared, so a stale id from an earlier run cannot be pushed to a VM.
    `reserved` maps the ids recorded on the roster's other rows (`--only` leaves them out of
    `rows`) to their slugs: no row is bound to one of them, as `provision_repos.wanted_logins`
    reserves the logins of the whole roster.

    `no_rebind` (docs/ADMIN.md §2.4; the admin bot always passes it): an existing `zulip_user_id`
    is never replaced or cleared. A row whose e-mail binds nobody, and whose recorded id is an
    active human's, is matched by that id (#627: a row `roster_sync.py` made is linked by id from
    the start, and its e-mail may be Zulip's placeholder, which a later change of the user's
    visibility replaces by the real address): no other row may take that user. A row whose
    e-mail now binds another user, or that binds nobody and records no active human, keeps its
    id, is blocked (no bots, channel or permission change) and is reported; only a
    row without an id may be bound. Without `no_rebind` (a deployer's rebind by hand,
    ADMIN §2.5) the e-mail alone binds, as before.
    """
    reports: list[Report] = []
    humans = {
        str(u.get("user_id")): u
        for u in users
        if not u.get("is_bot") and u.get("is_active", True) and u.get("user_id") is not None
    }
    for row in rows:
        report = Report(slug=slug_of(row))
        if field_id is None:
            report.warnings.append("no custom profile field named GitHub; matched by e-mail only")
        user, how, warnings = find_user(users, row, field_id)
        linked = humans.get(recorded_id(row)) if no_rebind else None
        if linked is not None and user is None and how == NOT_JOINED:
            user, how = linked, ZULIP_ID  # keyed on the Zulip ID (#627): its e-mail binds nobody
        report.warnings.extend(warnings)
        if user is None:
            if how != NOT_JOINED:  # ambiguous: nothing is provisioned for the row
                report.notes.append(how)
                report.blocked = True
        else:
            report.user_id = int(user["user_id"])
            report.matched = how
            old = recorded_id(row)
            if old and old != str(report.user_id) and not no_rebind:
                report.warnings.append(f"zulip_user_id changed from {old} to {report.user_id}")
        reports.append(report)
    owners: dict[int, list[Report]] = {}
    for report in reports:
        if report.user_id is not None:
            owners.setdefault(report.user_id, []).append(report)
    for uid, group in owners.items():
        if sum(r.matched == ZULIP_ID for r in group) == 1:
            continue  # the row that records the user keeps it; the others are refused below
        if len(group) > 1:
            names = ", ".join(r.slug for r in group)
            for report in group:
                report.notes.append(f"Zulip user {uid} matches {len(group)} rows ({names})")
                report.user_id = None
                report.matched = ""
                report.blocked = True
    kept = dict(reserved or {})  # normalized zulip_user_id -> the slug that keeps it
    kept |= {str(r.user_id): r.slug for r in reports if r.matched == ZULIP_ID}
    refused: set[int] = set()  # the rows that keep their id (no_rebind)
    for i, (row, report) in enumerate(zip(rows, reports, strict=True)):
        old = recorded_id(row)
        if no_rebind and old and old != str(report.user_id):
            now = "no single user" if report.user_id is None else f"user {report.user_id}"
            report.notes.append(
                f"binding changed: refused (zulip_user_id {old} kept; the roster e-mail now "
                f"matches {now}); a rebind is done by hand"
            )
            report.user_id, report.matched, report.blocked = int(old), "kept", True
            kept[old] = report.slug
            refused.add(i)
    for i, (row, report) in enumerate(zip(rows, reports, strict=True)):
        if i in refused:
            continue  # the row keeps its id, and nothing is provisioned for it
        if report.matched == ZULIP_ID:
            continue  # linked by the id it records, which stays as it is
        uid = str(report.user_id) if report.user_id is not None else ""
        if uid and uid != recorded_id(row) and uid in kept:
            # never bind a user that another row records: one user, one row
            report.notes.append(f"Zulip user {uid} is still recorded for {kept[uid]}: not bound")
            report.user_id, report.matched, report.blocked, uid = None, "", True, ""
        row["zulip_user_id"] = uid
    return reports


def recorded_id(row: Mapping[str, str]) -> str:
    """The row's `zulip_user_id`, normalized (`0101` is 101), or "" when it has none. Anything
    but ASCII digits (a hand edit) raises ValueError: `main` refuses such a roster."""
    value = (row.get("zulip_user_id") or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"[0-9]{1,18}", value):
        raise ValueError(
            f"invalid zulip_user_id {value[:20]!r} on row {slug_of(row) or '?'} "
            "(ASCII digits only): fix the roster"
        )
    return str(int(value))


def provision(
    rows: list[dict[str, str]],
    admin: ZulipAdmin,
    *,
    out_dir: pathlib.Path,
    dry_run: bool,
    regenerate: bool,
    bot_client: Callable[[str, str], ZulipAdmin] | None = None,
    hold: bool = False,
    no_rebind: bool = False,
    repo_owner: str = REPO_OWNER,
    reserved: Mapping[str, str] | None = None,
    wording: Wording = DEFAULT_WORDING,
) -> list[Report]:
    """README.md §1 for every row; rows get `zulip_user_id` and `zulip_invite` filled in (the caller
    writes the roster). `bot_client(email, key)` posts as a bot (default: a `ZulipAdmin`).
    `hold`: bots, zuliprc files and a bots-only sandbox, nothing the participant sees.
    `no_rebind`, `reserved`: see `match_rows`. `repo_owner`: the owner "Start here" names;
    `wording`, the workshop's gift step and file host there and in the description.
    No invitation is sent (`invite_state`, #628)."""
    if bot_client is None:
        site = admin.site
        bot_client = lambda email, key: ZulipAdmin(site, email, key)  # noqa: E731
    organizer_id = int(admin.me()["user_id"])
    field_id = github_field_id(admin.profile_fields())
    users = admin.users()
    bots_by_name = index_bots(admin, users)
    streams = admin.streams()
    reports = match_rows(rows, users, field_id, no_rebind=no_rebind, reserved=reserved)
    for row, report in zip(rows, reports, strict=True):
        if report.blocked:
            continue
        slug = slug_of(row)
        moved = move_legacy_keys(out_dir, row, dry_run=dry_run)  # before any key is written
        if moved:
            report.warnings.append(moved)
        principals = [report.user_id] if report.user_id is not None and not hold else []
        claude_bot = None
        bot_ids: dict[str, int] = {}
        for kind in BOT_KINDS:
            try:
                bot, status = ensure_bot(
                    admin, bots_by_name, kind, slug, dry_run=dry_run, regenerate=regenerate
                )
            except ZulipAdminError as exc:
                report.notes.append(f"{kind} bot: {exc}")
                continue
            report.bots[kind] = status
            if bot is not None:
                principals.append(int(bot["user_id"]))
                bot_ids[kind] = int(bot["user_id"])
                if kind == "claude":
                    claude_bot = bot
                if not dry_run:
                    write_zuliprc(
                        out_dir / slug / f"{kind}.zuliprc",
                        admin.site,
                        str(bot["email"]),
                        str(bot["api_key"]),
                    )
        existing = find_channel(streams, slug, row.get("zulip_channel_id", ""))
        participant = report.user_id if not hold else None
        try:
            report.channel, stream_id = ensure_channel(
                admin,
                existing,
                slug,
                principals,
                organizer_id,
                dry_run=dry_run,
                hold=hold,
                bot_ids=bot_ids,
                admin_id=participant,
                wording=wording,
            )
        except ZulipAdminError as exc:
            report.notes.append(f"channel: {exc}")
            continue
        if stream_id is not None:
            row["zulip_channel_id"] = str(stream_id)
        # The participant's first run as a member (the roster does not say "joined" yet: the
        # Friday run after `--hold`, or the first run after they join Zulip) hands
        # the sandbox over to them; a new channel was created that way already. Never again on
        # later runs, so what the participant changes stays (Drew, issue #36, Tue 06:30).
        handed_over = True
        first_run_as_member = (row.get("zulip_invite") or "").strip() != "joined"
        if participant is not None and existing is not None and first_run_as_member:
            if not dry_run:
                handed_over = hand_over_channel(admin, existing, participant, report)
        if hold:
            report.invite = report.start_here = "held"
            continue
        invite_state(row, report)
        if report.invite == "joined" and handed_over:  # else the next run tries again
            row["zulip_invite"] = "joined"
        try:
            report.start_here = ensure_start_here(
                bot_client,
                claude_bot,
                stream_id,
                slug,
                qualified_repo(row, repo_owner),
                dry_run=dry_run,
                bot_ids=bot_ids,
                wording=wording,
            )
        except (ZulipAdminError, httpx.HTTPError) as exc:
            report.notes.append(f"start here: {type(exc).__name__}: {exc}")
    return reports


def merge_roster(path: str, rows: list[dict[str, str]]) -> None:
    """Write the provisioned rows' `zulip_user_id`, `zulip_invite` and `zulip_channel_id` back
    into the roster file, keeping the rows that were not provisioned (other slugs with --only,
    rows without a slug)."""
    full = read_roster(path)
    done = {slug_of(r): r for r in rows}
    for r in full:
        if slug_of(r) in done:
            for column in ("zulip_user_id", "zulip_invite", "zulip_channel_id"):
                r[column] = done[slug_of(r)].get(column, "")
    write_roster(path, full)


def _zuliprc_email(path: pathlib.Path) -> str:
    """The e-mail in a bot's zuliprc, or "" when it can't be read (`load_zuliprc`'s errors)."""
    try:
        return load_zuliprc(str(path))[1].strip()
    except (OSError, ValueError):
        return ""


def _deactivated_own(
    admin: ZulipAdmin, user: Mapping[str, Any] | None, name: str, me: list[int]
) -> bool:
    """Whether `user`, the user of a zuliprc's e-mail (`_bot_user`), is a bot of that
    name, deactivated, and owned by the provisioner: one that an earlier run deactivated
    (Codex round 2 on #367). Anything else, someone else's bot above all, is not."""
    if not user or user.get("is_active") is not False:
        return False
    if str(user.get("full_name", "")).casefold() != name.casefold():
        return False
    if not me:
        me.append(int(admin.me()["user_id"]))
    return user.get("bot_owner_id") == me[0]


def _bot_user(
    admin: ZulipAdmin, listed: Mapping[str, Mapping[str, Any]], email: str
) -> Mapping[str, Any] | None:
    """The users listing's entry for a bot's e-mail, or, for a bot the listing leaves out,
    `GET /users/{its e-mail}`, which a Member's key may call too: a bot's address is never
    hidden (#396). None when neither knows a bot with that address."""
    user = listed.get(email.casefold())
    if user is not None:
        return user
    try:
        user = admin.user_by_email(email)
    except (ZulipAdminError, httpx.HTTPError):
        return None
    if not isinstance(user, dict) or not user.get("is_bot"):
        return None
    return user if _user_email(user) == email.casefold() else None


def deactivate(
    rows: list[dict[str, str]],
    admin: ZulipAdmin,
    *,
    dry_run: bool,
    out_dir: pathlib.Path | None = None,
) -> list[Report]:
    """Deactivate the roster's bots, whose keys stop working until a reactivation (README.md §7.3).

    With `out_dir` (as `main` always runs it): each bot is the one whose e-mail its
    `out/<slug>/<kind>.zuliprc` holds, the file the admin bot's plan hashed (docs/ADMIN.md
    §2.4), and it must also be named `<Kind> · <slug>`. A bot of that name with another e-mail,
    or with no zuliprc there, is left alone and reported, and so is a zuliprc whose e-mail is
    not among the provisioner's active bots: someone else's bot (the kept fleet's are Drew's
    until the cutover; the review of #367). Each fails the run. A bot of that name and e-mail
    that the provisioner owns and that is deactivated already is done: a rerun after a partial
    run recovers (Codex round 2 on #367).

    The bulk `GET /users` can leave out a bot made moments before, as it did with a Member's
    key on Oct 15 (#396). Such a bot is looked up by its own e-mail (`GET /users/{email}`; a
    bot's address is visible to a Member) and reported only if that fails too."""
    users = admin.users()  # deactivated ones included
    bots_by_name = index_bots(admin, users)
    by_email = {b["email"].casefold(): b for b in bots_by_name.values() if b.get("email")}
    every_bot = {_user_email(u): u for u in users if u.get("is_bot")}
    me: list[int] = []  # the provisioner's user ID, read once, when needed
    reports: list[Report] = []
    for row in rows:
        report = Report(slug=slug_of(row))
        for kind in BOT_KINDS:
            name = bot_full_name(kind, report.slug)
            bot = bots_by_name.get(name.casefold())
            if out_dir is not None:
                email = _zuliprc_email(out_dir / report.slug / f"{kind}.zuliprc")
                chosen = by_email.get(email.casefold()) if email else None
                if not email and bot is None:
                    report.bots[kind] = "absent"  # never provisioned: nothing to do
                    continue
                if (
                    email
                    and chosen is None
                    and _deactivated_own(admin, _bot_user(admin, every_bot, email), name, me)
                ):
                    report.bots[kind] = "deactivated already"  # a rerun after a partial run
                    continue
                if email and chosen is None:  # someone else's bot, or still active
                    why = (
                        f"holds {email}, which is not among the provisioner's active bots: "
                        "not owned by the provisioner, or still active?"
                    )
                elif chosen is None or chosen["full_name"].casefold() != name.casefold():
                    why = f"holds {email}, which is not {name}" if email else "is missing"
                else:
                    why = ""
                if why:
                    report.notes.append(f"{kind} bot: out/{report.slug}/{kind}.zuliprc {why}")
                    continue
                bot = chosen
            if bot is None:
                report.bots[kind] = "absent"
                continue
            if bot.get("user_id") is None:  # left out of the listing: by its own e-mail (#396)
                try:
                    bot = _resolve_bot_id(admin, name, bot)
                except ZulipAdminError:
                    report.notes.append(
                        f"{kind} bot: not in the users listing, nor found by its e-mail; "
                        "deactivate it in the UI"
                    )
                    continue
            if dry_run:
                report.bots[kind] = "would deactivate"
                continue
            try:
                admin.deactivate_bot(int(bot["user_id"]))
                report.bots[kind] = "deactivated"
            except ZulipAdminError as exc:
                report.notes.append(f"{kind} bot: {exc}")
        reports.append(report)
    return reports


def render(reports: list[Report]) -> str:
    rows = [
        [
            r.slug,
            str(r.user_id) if r.user_id is not None else "-",
            r.matched or "-",
            r.invite or "-",
            ", ".join(f"{k}: {v}" for k, v in r.bots.items()) or "-",
            r.channel or "-",
            r.start_here or "-",
            "; ".join(r.notes + [f"warning: {w}" for w in r.warnings]),
        ]
        for r in reports
    ]
    headers = ["participant", "zulip id", "matched by", "invite", "bots", "channel", "start here"]
    table = format_table([*headers, "notes"], rows)
    lines = [invite_line(r) for r in reports if r.invite == NOT_ON_ZULIP]
    return "\n".join([table, "", *lines]) if lines else table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zulip_provision.py",
        description="Provision workshop bots and channels (README.md §1).",
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--zuliprc", required=True, help="Drew's admin zuliprc")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="where the bot zuliprcs go")
    parser.add_argument("--only", action="append", metavar="SLUG")
    parser.add_argument("--dry-run", action="store_true", help="GET requests only")
    parser.add_argument("--regenerate-keys", action="store_true")
    parser.add_argument(
        "--hold",
        action="store_true",
        help="bots, zuliprc files and bots-only sandboxes with a neutral description; no "
        "participant subscribed, no Start here (rerun without it to release)",
    )
    parser.add_argument(
        "--deactivate", action="store_true", help="README.md §7.3: deactivate the bots"
    )
    parser.add_argument(
        "--no-rebind",
        action="store_true",
        help="never replace or clear a recorded zulip_user_id: a row whose e-mail now matches "
        "another user, or none, is left untouched and reported (docs/ADMIN.md §2.4)",
    )
    parser.add_argument(
        "--repo-owner",
        default=REPO_OWNER,
        help=f"the GitHub owner of a roster repo given without one (default {REPO_OWNER})",
    )
    parser.add_argument(
        "--refserve-url",
        default=REFSERVE_URL,
        help="the file server of the agents' links, as push_config.py's, whose host \"Start "
        "here\" names; '' names none",
    )
    gifts = parser.add_mutually_exclusive_group()
    gifts.add_argument(
        "--gifts-step",
        metavar="TEXT",
        help='the first sentence of "Start here"\'s step 1 and of the description, about the '
        "workshop's gift subscriptions (default: GIFTS_STEP)",
    )
    gifts.add_argument(
        "--gifts-step-b64", metavar="B64", help="the same, base64url-encoded (the admin bot's)"
    )
    args = parser.parse_args(argv)
    if not _OWNER_RE.match(args.repo_owner):
        parser.error(f"invalid --repo-owner {args.repo_owner!r}")
    if args.refserve_url and (
        not args.refserve_url.startswith("https://") or any(c.isspace() for c in args.refserve_url)
    ):
        parser.error("--refserve-url must be an https:// URL (or '' for none)")
    try:
        b64 = args.gifts_step_b64
        step = args.gifts_step if b64 is None else decode_gifts_step(b64)
    except ValueError as exc:
        parser.error(f"--gifts-step-b64: {exc}")
    if step is not None and not gifts_step_ok(step):
        parser.error(
            f"--gifts-step must be one line of at most {GIFTS_STEP_BYTES} bytes, without control "
            "characters, starting with neither a space nor -"
        )
    wording = Wording(gifts=step or GIFTS_STEP, refserve_url=args.refserve_url.rstrip("/"))

    try:
        every = load_roster(args.roster)
        rows = select_rows(every, args.only)
        chosen = {id(r) for r in rows}
        # the ids recorded on the rows --only leaves out; every id is checked (`recorded_id`)
        ids = [] if args.deactivate else [(recorded_id(r), r) for r in every]
        reserved = {uid: slug_of(r) or "?" for uid, r in ids if uid and id(r) not in chosen}
    except ValueError as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 1
    try:
        site, email, key = load_zuliprc(args.zuliprc)
    except (OSError, ValueError) as exc:  # the file named, never its text (#523)
        print(f"zuliprc: {exc}", file=sys.stderr)
        return 1
    admin = ZulipAdmin(site, email, key)
    try:
        if args.deactivate:
            out = pathlib.Path(args.out_dir)
            reports = deactivate(rows, admin, dry_run=args.dry_run, out_dir=out)
        else:
            reports = provision(
                rows,
                admin,
                out_dir=pathlib.Path(args.out_dir),
                dry_run=args.dry_run,
                regenerate=args.regenerate_keys,
                hold=args.hold,
                no_rebind=args.no_rebind,
                repo_owner=args.repo_owner,
                reserved=reserved,
                wording=wording,
            )
            if not args.dry_run:
                merge_roster(args.roster, rows)
    finally:
        admin.close()
    print(render(reports))
    if args.dry_run and step is not None and not args.hold and not args.deactivate:
        # the plan's approvers read the text, not only its base64 in the command (#709)
        print("\n" + GIFTS_SHOWN.format(step))
    failed = [r for r in reports if r.notes]
    if failed:
        print(f"\n{len(failed)} participant(s) need attention", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
