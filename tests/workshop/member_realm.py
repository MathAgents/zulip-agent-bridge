"""A fake Zulip Cloud organization as each caller's key sees it (#396, #397).

It serves the REST calls of `move_bots.py` and `zulip_provision.py --deactivate` through
`httpx.MockTransport`, so the scripts' own `ZulipAdmin` makes them, URL quoting included. The
caller is the user whose e-mail the request's basic auth names.

**Addresses,** as Zulip `main` gives them (`can_access_delivery_email`, `format_user_row`,
`ROLE_TO_ACCESSIBLE_EMAIL_ADDRESS_VISIBILITY_IDS`; feature level 163 and later):
- each user has a `visibility` (`email_address_visibility`): everyone, members, moderators
  ("Admins and moderators"), admins or nobody. Bots' is everyone;
- `email` is the real address only when the visibility is everyone. Otherwise it is a dummy,
  `user<id>@<the realm's host>`, for every caller, the user and an owner included;
- `delivery_email` is the real address for the user themself and for a caller whose role may
  read that visibility: a Member reads everyone and members; a Moderator, moderators too; an
  owner or administrator, admins too. Nobody else reads "nobody". For everyone else it is null;
- `GET /users/{address}` finds a user by its `email` or by a `delivery_email` the caller can
  read. Otherwise it answers "No such user" (HTTP 400, `BAD_REQUEST`). `GET /users/{id}` finds
  any user, deactivated ones and bots included.

A human's visibility is "Admins and moderators" unless given. That is `zab-tester2`'s, and on
Oct 15 every other human on the dev org hid their address from a Member (`zab-tester`'s key,
then a Member's) in the same way.

**The listing:** the bulk `GET /users` can leave out a bot made moments before (`unlisted`),
which `GET /users/{its e-mail}` and `GET /users/{id}` still return. `GET /bots` lists the
caller's own active bots, by `username` (the e-mail), with no user ID (`get_bots_backend`).
`PATCH /bots/{id}` with `bot_owner_id` and `DELETE /bots/{id}` are allowed to the bot's owner and
to an administrator (`access_bot_by_id`). **An owner change, though, only to an administrator or
an owner,** as Zulip Cloud has refused it to any other key since Nov 2, the bot's owner's
included (#758): HTTP 400 with `NOT_ADMINISTRATOR`, its exact answer. Zulip's open-source
`patch_bot_backend` has no such check.

**Channels** (#627, `channel`): `GET /streams`, `GET /streams/{id}` and `GET
/streams/{id}/members` show a private channel only to its subscribers and the administrators,
"Invalid channel ID" to anyone else; `GET /users/me/subscriptions` lists the caller's own.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

import httpx

OWNER, ADMIN, MODERATOR, MEMBER, GUEST = 100, 200, 300, 400, 600
#: `email_address_visibility` (`UserProfile.EMAIL_ADDRESS_VISIBILITY_*`).
EVERYONE, MEMBERS, ADMINS, NOBODY, MODERATORS = 1, 2, 3, 4, 5
#: The visibilities each role may read (`ROLE_TO_ACCESSIBLE_EMAIL_ADDRESS_VISIBILITY_IDS`).
READS = {
    OWNER: {EVERYONE, MEMBERS, MODERATORS, ADMINS},
    ADMIN: {EVERYONE, MEMBERS, MODERATORS, ADMINS},
    MODERATOR: {EVERYONE, MEMBERS, MODERATORS},
    MEMBER: {EVERYONE, MEMBERS},
    GUEST: {EVERYONE},
}
ADDRESSES = ("email", "delivery_email")
#: Zulip Cloud's answer to a bot owner change by a key that isn't an administrator's (#758).
NOT_ADMINISTRATOR = {
    "result": "error",
    "msg": "Must be an organization administrator",
    "code": "UNAUTHORIZED_PRINCIPAL",
}


class MemberRealm:
    def __init__(self, host: str = "org.zulipchat.com") -> None:
        self.host = host
        self.users: dict[int, dict[str, Any]] = {}  # the truth: real addresses
        self.unlisted: set[int] = set()  # left out of the bulk `GET /users`
        self.requests: list[tuple[str, str, str]] = []  # (caller's e-mail, method, path)
        #: stream ID → {"name", "invite_only", "subscribers": set of user IDs} (#627)
        self.streams: dict[int, dict[str, Any]] = {}

    # --- the organization

    def human(
        self,
        uid: int,
        name: str,
        email: str,
        role: int = MEMBER,
        visibility: int = MODERATORS,
        **extra: Any,
    ) -> None:
        self.users[uid] = {"user_id": uid, "full_name": name, "email": email, "role": role,
                           "is_bot": False, "is_active": True, "visibility": visibility,
                           **extra}  # fmt: skip

    def bot(self, uid: int, name: str, email: str, owner: int, **extra: Any) -> None:
        self.users[uid] = {"user_id": uid, "full_name": name, "email": email, "role": MEMBER,
                           "is_bot": True, "is_active": True, "bot_owner_id": owner,
                           "visibility": EVERYONE, **extra}  # fmt: skip

    def channel(self, sid: int, name: str, members: set[int], invite_only: bool = True) -> None:
        """A channel (#627): a private one is seen, with its members, only by its subscribers
        and the administrators, as Zulip's `access_stream_by_id` has it for metadata."""
        self.streams[sid] = {"name": name, "invite_only": invite_only, "subscribers": members}

    def sees(self, caller: dict[str, Any], sid: int) -> bool:
        stream = self.streams.get(sid)
        if stream is None:
            return False
        mine = caller["user_id"] in stream["subscribers"]
        return mine or not stream["invite_only"] or caller["role"] <= ADMIN

    def shown_stream(self, sid: int) -> dict[str, Any]:
        stream = self.streams[sid]
        return {"stream_id": sid, "name": stream["name"], "invite_only": stream["invite_only"]}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def asked(self, path: str) -> bool:
        return any(p == path for _, _, p in self.requests)

    # --- what a caller sees

    def seen(self, caller: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        """`user` as `caller`'s key gets it (feature level 163 and later)."""
        visibility = user["visibility"]
        readable = user["user_id"] == caller["user_id"] or visibility in READS[caller["role"]]
        shown = {k: v for k, v in user.items() if k != "visibility"}
        shown["delivery_email"] = user["email"] if readable else None
        if visibility != EVERYONE:
            shown["email"] = f"user{user['user_id']}@{self.host}"
        return shown

    def lookup(self, caller: dict[str, Any], who: str) -> dict[str, Any] | None:
        """`GET /users/{who}`: by ID, or by an address the caller can see (or the dummy)."""
        if who.isdigit():
            return self.users.get(int(who))
        for user in self.users.values():
            seen = self.seen(caller, user)
            if who.casefold() in {str(seen[k] or "").casefold() for k in ADDRESSES} - {""}:
                return user
        return None

    # --- the endpoints

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urllib.parse.unquote(request.url.path.removeprefix("/api/v1/"))
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        email = base64.b64decode(token).decode().partition(":")[0] if scheme == "Basic" else ""
        self.requests.append((email, request.method, path))
        callers = [u for u in self.users.values() if u["email"] == email and u["is_active"]]
        if not callers:
            return self.error(401, "UNAUTHORIZED", "Invalid API key")
        caller = callers[0]
        if request.method == "GET" and path == "users/me":
            return self.ok(**self.seen(caller, caller))
        if request.method == "GET" and path == "users/me/subscriptions":
            mine = [s for s, v in self.streams.items() if caller["user_id"] in v["subscribers"]]
            return self.ok(subscriptions=[self.shown_stream(s) for s in mine])
        if request.method == "GET" and path == "streams":
            return self.ok(
                streams=[self.shown_stream(s) for s in self.streams if self.sees(caller, s)]
            )
        if request.method == "GET" and path.startswith("streams/"):
            sid, _, rest = path.removeprefix("streams/").partition("/")
            if not sid.isdigit() or not self.sees(caller, int(sid)) or rest not in ("", "members"):
                return self.error(400, "BAD_REQUEST", "Invalid channel ID")
            if rest == "members":
                return self.ok(subscribers=sorted(self.streams[int(sid)]["subscribers"]))
            return self.ok(stream=self.shown_stream(int(sid)))
        if request.method == "GET" and path == "users":
            listed = [u for u in self.users.values() if u["user_id"] not in self.unlisted]
            return self.ok(members=[self.seen(caller, u) for u in listed])
        if request.method == "GET" and path.startswith("users/"):
            user = self.lookup(caller, path.removeprefix("users/"))
            if user is None:
                return self.error(400, "BAD_REQUEST", "No such user")
            return self.ok(user=self.seen(caller, user))
        if request.method == "GET" and path == "bots":
            mine = [
                {"username": u["email"], "full_name": u["full_name"], "api_key": "K" * 32}
                for u in self.users.values()
                if u["is_bot"] and u["is_active"] and u.get("bot_owner_id") == caller["user_id"]
            ]
            return self.ok(bots=mine)
        if request.method in ("PATCH", "DELETE") and path.startswith("bots/"):
            bot = self.users.get(int(path.removeprefix("bots/")))
            if bot is None or not bot["is_bot"]:
                return self.error(400, "BAD_REQUEST", "No such bot")
            if bot.get("bot_owner_id") != caller["user_id"] and caller["role"] > ADMIN:
                return self.error(400, "BAD_REQUEST", "Insufficient permission")
            if request.method == "DELETE":
                bot["is_active"] = False
                return self.ok()
            form = urllib.parse.parse_qs(request.content.decode())
            if "bot_owner_id" in form and caller["role"] > ADMIN:  # Zulip Cloud's rule (#758)
                return httpx.Response(400, json=NOT_ADMINISTRATOR)
            owner = self.users.get(json.loads(form["bot_owner_id"][0]))
            if owner is None or owner["is_bot"] or not owner["is_active"]:
                return self.error(400, "BAD_REQUEST", "Failed to change owner, no such user")
            bot["bot_owner_id"] = owner["user_id"]
            return self.ok(bot_owner=self.seen(caller, owner)["email"])
        return self.error(404, "NOT_FOUND", f"{request.method} {path}")

    @staticmethod
    def ok(**data: Any) -> httpx.Response:
        return httpx.Response(200, json={"result": "success", "msg": "", **data})

    @staticmethod
    def error(status: int, code: str, msg: str) -> httpx.Response:
        return httpx.Response(status, json={"result": "error", "code": code, "msg": msg})
