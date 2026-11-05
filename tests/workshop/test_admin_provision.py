"""`workshop/admin/zulip_provision.py` (workshop/admin/README.md §1, §7.3) against a fake Zulip
admin client."""

from __future__ import annotations

import base64
import os
import pathlib
from typing import Any

import pytest
import zulip_provision as zp

from .member_realm import MemberRealm

KEY_A = "A" * 32
KEY_B = "B" * 32
KEY_C = "C" * 32
KEY_NEW = "N" * 32

ROSTER = """github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,Alice,Smith,alice@example.org,alice/thesis,1,,,,
bob,Bob,Jones,bob@example.org,sandbox,2,,,,
carol,Carol,Lee,carol@example.org,carol/x,3,,,999,
dave,Dave,Kim,dave@example.org,dave/x,4,,,,
"""


class FakeAdmin:
    """Canned org state; records every call as (method, endpoint, payload)."""

    site = "https://org.zulipchat.com"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._fields = [{"id": 7, "name": "GitHub", "type": 7}, {"id": 8, "name": "Phone"}]
        self._users = [
            {
                "user_id": 101,
                "email": "u101@org",
                "delivery_email": "alice@example.org",
                "is_bot": False,
                "profile_data": {"7": {"value": "@Alice"}},
            },
            {
                "user_id": 102,
                "email": "u102@org",
                "delivery_email": "bob@example.org",
                "is_bot": False,
                "profile_data": {},
            },
            {
                "user_id": 103,
                "email": "u103@org",
                "delivery_email": "c1@example.org",
                "is_bot": False,
                "profile_data": {"7": {"value": "carol"}},
            },
            {
                "user_id": 104,
                "email": "u104@org",
                "delivery_email": "c2@example.org",
                "is_bot": False,
                "profile_data": {"7": {"value": "CAROL"}},
            },
            {
                "user_id": 105,
                "email": "u105@org",
                "delivery_email": "impostor@example.org",
                "is_bot": False,
                "profile_data": {"7": {"value": "bob"}},  # claims bob's login (unverified field)
            },
            {"user_id": 201, "email": "codex-alice-bot@org", "is_bot": True, "profile_data": {}},
        ]
        # `GET /bots` (zerver/views/users.py get_bots_backend): username = the bot's e-mail,
        # full_name, api_key, …; there is NO user_id.
        self._bots = [
            {
                "username": "codex-alice-bot@org",
                "full_name": "Codex · alice",
                "api_key": KEY_A,
                "bot_type": 1,
            },
        ]
        self._streams = [
            {
                "stream_id": 31,
                "name": "alice-sandbox",
                "description": "repo: alice/old",
                "invite_only": True,
            },
        ]
        self._next_id = 300
        self.fail_update = False
        self.bot_posts: dict[
            int, list[tuple[str, str, str]]
        ] = {}  # stream -> (sender, topic, text)

    # reads
    def me(self):
        self.calls.append(("GET", "users/me", {}))
        return {"user_id": 1, "email": "drew@org"}

    def invites(self):
        """`GET /invites`: provisioning reads no invitation (#628)."""
        raise AssertionError("zulip_provision.provision read GET /invites")

    def profile_fields(self):
        self.calls.append(("GET", "realm/profile_fields", {}))
        return list(self._fields)

    def users(self):
        self.calls.append(("GET", "users", {"include_custom_profile_fields": True}))
        return [dict(u) for u in self._users]

    def bots(self):
        self.calls.append(("GET", "bots", {}))
        return [dict(b) for b in self._bots]

    def user(self, user_id):
        self.calls.append(("GET", f"users/{user_id}", {}))
        for u in self._users:
            if u["user_id"] == user_id:
                return dict(u)
        raise zp.ZulipAdminError("BAD_REQUEST", 400, "No such user")

    def user_by_email(self, email):
        self.calls.append(("GET", f"users/{email}", {}))
        for u in self._users:
            if (u.get("delivery_email") or u.get("email")) == email:
                return dict(u)
        raise zp.ZulipAdminError("BAD_REQUEST", 400, "No such user")

    def streams(self):
        self.calls.append(("GET", "streams", {}))
        return [dict(s) for s in self._streams]

    # writes
    def create_bot(self, full_name, short_name):
        """`POST /bots` (add_bot_backend): user_id, api_key, avatar_url, default_*; NO e-mail."""
        self.calls.append(("POST", "bots", {"full_name": full_name, "short_name": short_name}))
        self._next_id += 1
        key = {"Claude · alice": KEY_B, "Codex · bob": KEY_C}.get(full_name, KEY_NEW)
        email = f"{short_name}-bot@org"
        self._bots.append(
            {"username": email, "full_name": full_name, "api_key": key, "bot_type": 1}
        )
        self._users.append(
            {
                "user_id": self._next_id,
                "email": email,
                "full_name": full_name,
                "is_bot": True,
                "profile_data": {},
            }
        )
        return {
            "user_id": self._next_id,
            "api_key": key,
            "avatar_url": "https://org/avatar",
            "default_sending_stream": None,
            "default_events_register_stream": None,
            "default_all_public_streams": False,
        }

    def regenerate_key(self, bot_id):
        self.calls.append(("POST", f"bots/{bot_id}/api_key/regenerate", {}))
        email = next(u["email"] for u in self._users if u["user_id"] == bot_id)
        for b in self._bots:
            if b["username"] == email:
                b["api_key"] = KEY_NEW
        return KEY_NEW

    def subscribe(self, name, description, principals):
        self.calls.append(
            (
                "POST",
                "users/me/subscriptions",
                {"name": name, "description": description, "principals": principals},
            )
        )
        if not any(s["name"] == name for s in self._streams):
            self._streams.append(
                {
                    "stream_id": 90 + len(self._streams),
                    "name": name,
                    "description": description,
                    "invite_only": True,
                }
            )
        return {"subscribed": {}, "already_subscribed": {}}

    def create_channel(self, name, description, subscribers, organizer_id, admin_id=None):
        # Zulip's default administrator is the channel's creator (the organizer)
        admins = {"direct_members": [admin_id or organizer_id], "direct_subgroups": []}
        self.calls.append(
            (
                "POST",
                "channels/create",
                {
                    "name": name,
                    "description": description,
                    "subscribers": subscribers,
                    "can_add_subscribers_group": {
                        "direct_members": [organizer_id],
                        "direct_subgroups": [],
                    },
                    "admin_id": admin_id,
                },
            )
        )
        sid = 90 + len(self._streams)
        self._streams.append(
            {
                "stream_id": sid,
                "name": name,
                "description": description,
                "invite_only": True,
                "history_public_to_subscribers": True,
                "can_administer_channel_group": admins,
            }
        )
        return sid

    def update_channel(self, stream_id, **changes):
        self.calls.append(("PATCH", f"streams/{stream_id}", dict(changes)))
        if self.fail_update:
            raise zp.ZulipAdminError("BAD_REQUEST", 400, "'old' value does not match")
        for s in self._streams:
            if s["stream_id"] == stream_id:
                if "history_public_to_subscribers" in changes:
                    s["history_public_to_subscribers"] = changes["history_public_to_subscribers"]
                if "can_administer_channel_group" in changes:
                    s["can_administer_channel_group"] = changes["can_administer_channel_group"][
                        "new"
                    ]

    def invite(self, *args, **kwargs):
        """`POST /invites`: zulip_provision.py never sends one (#628)."""
        raise AssertionError("zulip_provision.provision sent an invitation")

    # main() builds bot clients with `ZulipAdmin(site, email, key)`, patched to this fake
    def topic_messages(self, stream_id, topic):
        return self.bot_client("bot", "").topic_messages(stream_id, topic)

    def send_message(self, stream_id, topic, content):
        self.bot_client("bot", "").send_message(stream_id, topic, content)

    def bot_client(self, email, key):
        admin = self

        class Bot:
            def topic_messages(self, stream_id, topic):
                admin.calls.append(("GET", f"messages as {email}", {"topic": topic}))
                return [m for m in admin.bot_posts.get(stream_id, []) if m[1] == topic]

            def send_message(self, stream_id, topic, content):
                admin.calls.append(("POST", f"messages as {email}", {"topic": topic}))
                admin.bot_posts.setdefault(stream_id, []).append((email, topic, content))

            def close(self):
                pass

        return Bot()

    def set_description(self, stream_id, description):
        self.calls.append(("PATCH", f"streams/{stream_id}", {"description": description}))
        for s in self._streams:
            if s["stream_id"] == stream_id:
                s["description"] = description

    def deactivate_bot(self, bot_id):
        self.calls.append(("DELETE", f"bots/{bot_id}", {}))

    def close(self):
        pass

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    return path


def rows_of(path):
    return zp.read_roster(str(path))


# ------------------------------------------------------------------------------ matching


def test_github_field_id_is_case_insensitive():
    assert zp.github_field_id([{"id": 3, "name": "github"}]) == 3
    assert zp.github_field_id([{"id": 3, "name": "GitHub"}]) == 3
    assert zp.github_field_id([{"id": 3, "name": "Phone"}]) is None


def test_find_user_binds_by_email_only():
    """PM review of PR #47 and codex round 4: the self-reported GitHub field never binds a user,
    neither over an e-mail match nor in its absence (anyone can claim a login before its owner
    joins); it is reported as a hint or a warning."""
    admin = FakeAdmin()
    users = admin.users()
    # alice: e-mail and field agree
    user, how, warnings = zp.find_user(users, {"github": "ALICE", "email": "Alice@Example.org"}, 7)
    assert user["user_id"] == 101 and how == "e-mail" and warnings == []
    # bob: user 105 claims the field "bob", but bob's e-mail belongs to 102: e-mail wins, warned
    user, how, warnings = zp.find_user(users, {"github": "bob", "email": "bob@example.org"}, 7)
    assert user["user_id"] == 102 and how == "e-mail"
    assert warnings == ["GitHub field bob is also set by user(s) 105; e-mail wins"]
    # no e-mail match and a unique field claim: NOT bound (Mallory sets the field to "alice"
    # before Alice joins); the claim is reported so the roster e-mail can be checked
    user, how, warnings = zp.find_user(users, {"github": "alice", "email": "other@x"}, 7)
    assert user is None and how == zp.NOT_JOINED
    assert "user(s) 101 claim GitHub alice in the profile field, which is unverified" in warnings[0]
    user, how, warnings = zp.find_user(users, {"github": "bob", "email": "nobody@example.org"}, 7)
    assert user is None and "user(s) 105 claim GitHub bob" in warnings[0]
    # no e-mail match, two users carry the field: still not joined, both claims listed
    user, how, warnings = zp.find_user(users, {"github": "carol", "email": "carol@example.org"}, 7)
    assert user is None and how == zp.NOT_JOINED and "user(s) 103, 104 claim" in warnings[0]
    user, how, warnings = zp.find_user(users, {"github": "dave", "email": "dave@example.org"}, 7)
    assert user is None and how == zp.NOT_JOINED and warnings == []
    # no GitHub login at all (issue #36): the e-mail alone decides
    user, how, _ = zp.find_user(users, {"slug": "b", "github": "", "email": "bob@example.org"}, 7)
    assert user["user_id"] == 102
    # bots never match, even by e-mail
    user, how, _ = zp.find_user(users, {"github": "zz", "email": "codex-alice-bot@org"}, 7)
    assert user is None
    # without the field, the e-mail still works and nothing else does
    user, how, _ = zp.find_user(users, {"github": "alice", "email": "alice@example.org"}, None)
    assert user["user_id"] == 101 and how == "e-mail"


def test_match_rows_refuses_one_user_for_two_rows_and_clears_stale_ids(roster):
    rows = rows_of(roster)
    rows.append(
        {
            "github": "eve",
            "email": "ALICE@example.org",  # the same person as alice, by e-mail
            "repo": "",
            "zulip_user_id": "101",
        }
    )
    admin = FakeAdmin()
    reports = zp.match_rows(rows, admin.users(), 7)
    by = {r.slug: r for r in reports}
    # alice and eve both resolve to 101: neither is bound, both say so
    assert by["alice"].user_id is None and by["eve"].user_id is None
    assert by["alice"].notes == ["Zulip user 101 matches 2 rows (alice, eve)"]
    assert by["eve"].notes == by["alice"].notes
    # carol had only field claims and carried a stale id 999: cleared, not kept
    assert by["carol"].user_id is None and rows[2]["zulip_user_id"] == ""
    assert {r["github"]: r["zulip_user_id"] for r in rows} == {
        "alice": "",
        "bob": "102",
        "carol": "",
        "dave": "",
        "eve": "",
    }
    # an id that changes since the last run is reported
    rows = rows_of(roster)[:2]
    rows[1]["zulip_user_id"] = "7"
    reports = zp.match_rows(rows, admin.users(), 7)
    assert reports[1].user_id == 102 and reports[1].ok
    assert "zulip_user_id changed from 7 to 102" in reports[1].warnings
    assert "GitHub field bob is also set by user(s) 105; e-mail wins" in reports[1].warnings


# ----------------------------------------------------------------------------- provision


def test_provision_creates_reuses_and_writes_zuliprc(tmp_path, roster, capsys):
    admin = FakeAdmin()
    rows = rows_of(roster)
    out = tmp_path / "out"
    reports = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=False, regenerate=False
    )
    by = {r.slug: r for r in reports}
    # alice: Codex reused, Claude created, channel existed, description corrected
    assert by["alice"].user_id == 101 and by["alice"].matched == "e-mail"
    assert by["alice"].bots == {"codex": "reused", "claude": "created"}
    assert by["alice"].channel == "exists" and by["alice"].ok
    description = zp.channel_description("alice", {"codex": 201, "claude": 301})
    assert ("PATCH", "streams/31", {"description": description}) in admin.calls
    assert "[Claude · alice](#narrow/dm/301-Claude-.C2.B7-alice)" in description
    assert "!login" in description and "device code sign-in for Codex" in description
    sub = next(
        c
        for c in admin.calls
        if c[1] == "users/me/subscriptions" and c[2]["name"] == "alice-sandbox"
    )
    assert sub[2]["principals"] == [101, 201, 301]  # the organizer (1) is not among them
    assert by["alice"].invite == "joined" and rows[0]["zulip_invite"] == "joined"
    # bob: matched by e-mail, both bots created, channel created
    assert by["bob"].matched == "e-mail" and by["bob"].bots == {
        "codex": "created",
        "claude": "created",
    }
    assert by["bob"].channel == "created"
    create = next(c for c in admin.calls if c[1] == "channels/create")
    assert create[2]["name"] == "bob-sandbox" and create[2]["subscribers"] == [102, 302, 303]
    assert create[2]["can_add_subscribers_group"] == {"direct_members": [1], "direct_subgroups": []}
    assert ("POST", "bots", {"full_name": "Codex · bob", "short_name": "codex-bob"}) in admin.calls
    # carol (two unverified field claims, no e-mail match) and dave have not joined: they get
    # bots and a sandbox with only the bots in it, and are reported `not on Zulip`, which is not
    # a failure; no invitation is sent or read (#628): the organizers invite in Zulip
    assert by["carol"].invite == "not on Zulip" and by["carol"].ok
    assert "user(s) 103, 104 claim GitHub carol" in by["carol"].warnings[0]
    assert by["dave"].invite == "not on Zulip" and by["dave"].ok and not by["dave"].notes
    assert not [c for c in admin.calls if c[1].startswith("invites")]
    assert rows[2]["zulip_invite"] == rows[3]["zulip_invite"] == ""
    # Start here, once per sandbox, by the participant's Claude bot (the organizer is not in it)
    assert by["alice"].start_here == "posted" and by["dave"].start_here == "posted"
    posts = admin.bot_posts[31]
    assert [(p[0], p[1]) for p in posts] == [("claude-alice-bot@org", "Start here")]
    assert "!login" in posts[0][2] and "Claude · alice" in posts[0][2]
    # roster rows got their ids; carol's stale 999 is gone
    assert rows[0]["zulip_user_id"] == "101" and rows[1]["zulip_user_id"] == "102"
    assert rows[2]["zulip_user_id"] == "" and rows[3]["zulip_user_id"] == ""
    # the existing bot's id came from the users listing, the new bots' e-mails from GET /users/{id}
    assert ("GET", "users/301", {}) in admin.calls and ("GET", "users/302", {}) in admin.calls
    # zuliprc files: 0600, owner-only directory, correct contents
    codex_rc = out / "alice" / "codex.zuliprc"
    claude_rc = out / "alice" / "claude.zuliprc"
    assert oct(codex_rc.stat().st_mode & 0o777) == "0o600"
    assert oct((out / "alice").stat().st_mode & 0o777) == "0o700"
    assert (
        codex_rc.read_text()
        == f"[api]\nemail=codex-alice-bot@org\nkey={KEY_A}\nsite={admin.site}\n"
    )
    assert f"key={KEY_B}" in claude_rc.read_text()
    assert "email=claude-alice-bot@org\n" in claude_rc.read_text()  # fetched, not from POST
    assert f"key={KEY_C}" in (out / "bob" / "codex.zuliprc").read_text()
    assert "email=\n" not in codex_rc.read_text() + claude_rc.read_text()
    # nothing printed leaks a key
    print(zp.render(reports))
    text = capsys.readouterr().out
    assert KEY_A not in text and KEY_B not in text and KEY_C not in text and KEY_NEW not in text
    # one line per row not on Zulip, after the table, telling the organizers what to do
    assert text.endswith(f"\n\n{not_on_zulip('carol')}\n{not_on_zulip('dave')}\n")


def test_provision_is_idempotent(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)[:2]
    zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
    )
    creates = [c for c in admin.calls if c[1] == "bots" and c[0] == "POST"]
    assert len(creates) == 3
    admin.calls.clear()
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
    )
    assert all(r.bots == {"codex": "reused", "claude": "reused"} for r in reports)
    assert not [c for c in admin.calls if c[1] == "bots" and c[0] == "POST"]
    assert not [c for c in admin.calls if c[0] == "PATCH"]  # description already right
    assert not [c for c in admin.calls if "regenerate" in c[1]]
    assert all(r.start_here == "exists" for r in reports)  # posted once, not again
    # a row not on Zulip is reported the same way on every run, and nothing is sent
    rows = rows_of(roster)[3:4]  # dave
    zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
    )
    admin.calls.clear()
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
    )
    assert reports[0].invite == "not on Zulip" and reports[0].ok
    assert not [c for c in admin.calls if c[1].startswith("invites")]


def test_an_existing_public_sandbox_is_reconciled_and_stays_public(tmp_path, roster):
    """Drew (issue #36, Tue 06:30): participants may make their sandboxes public. A public
    `<slug>-sandbox` gets its description and members like a private one, and its privacy is
    never changed (codex round 2 on PR #47 refused it while sessions in public channels were
    off)."""
    admin = FakeAdmin()
    admin._streams[0]["invite_only"] = False
    rows = rows_of(roster)[:1]
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
    )
    assert reports[0].ok and reports[0].channel == "exists" and not reports[0].notes
    assert sandbox_writes(admin, "alice-sandbox")[:2] == [
        ("description", zp.channel_description("alice", {"codex": 201, "claude": 301})),
        ("subscribe", [101, 201, 301]),
    ]
    assert not any("invite_only" in c[2] or "is_private" in c[2] for c in admin.writes())
    assert admin._streams[0]["invite_only"] is False


def test_regenerate_keys_rotates_existing_bots_only(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)[:1]
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=True,
    )
    assert reports[0].bots["codex"] == "key regenerated"
    assert ("POST", "bots/201/api_key/regenerate", {}) in admin.calls
    assert f"key={KEY_NEW}" in (tmp_path / "out" / "alice" / "codex.zuliprc").read_text()


def test_dry_run_only_reads(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=True,
        regenerate=True,
    )
    assert all(c[0] == "GET" for c in admin.calls)
    assert not (tmp_path / "out").exists()
    by = {r.slug: r for r in reports}
    assert by["alice"].bots == {"codex": "reused", "claude": "would create"}
    assert by["bob"].channel == "would create" and by["alice"].channel == "exists"
    assert by["dave"].invite == "not on Zulip" and by["dave"].start_here == "would post"
    assert rows[0]["zulip_user_id"] == "101"  # the lookup result is still reported


def test_missing_github_field_is_only_a_warning(tmp_path, roster):
    admin = FakeAdmin()
    admin._fields = [{"id": 8, "name": "Phone"}]
    rows = rows_of(roster)[:1]
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=True,
        regenerate=False,
    )
    assert reports[0].user_id == 101 and reports[0].matched == "e-mail" and reports[0].ok
    assert any("no custom profile field" in w for w in reports[0].warnings)
    assert "warning: no custom profile field" in zp.render(reports)


def test_bot_without_email_or_id_gets_no_zuliprc(tmp_path, roster):
    """PM review of PR #47 (3): `POST /bots` returns no e-mail and `GET /bots` no id; a bot
    whose e-mail or id cannot be resolved is an error for the row, never an empty `email=`."""

    class NoEmail(FakeAdmin):
        def user(self, user_id):
            data = super().user(user_id)
            data["email"] = ""
            return data

    admin = NoEmail()
    rows = rows_of(roster)[:2]
    out = tmp_path / "out"
    reports = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=False, regenerate=False
    )
    by = {r.slug: r for r in reports}
    assert by["alice"].bots == {"codex": "reused"}  # the existing bot's e-mail comes from GET /bots
    assert any("claude bot" in n and "no e-mail" in n for n in by["alice"].notes)
    assert not (out / "alice" / "claude.zuliprc").exists()
    assert (out / "alice" / "codex.zuliprc").exists()
    assert not by["bob"].ok and not (out / "bob").exists()
    # an existing bot the users listing does not know has no id: reported, not guessed
    admin = FakeAdmin()
    admin._users = [u for u in admin._users if u["user_id"] != 201]
    reports = zp.provision(
        rows[:1], admin, bot_client=admin.bot_client, out_dir=out, dry_run=True, regenerate=False
    )
    assert any("codex bot" in n and "not in the users listing" in n for n in reports[0].notes)
    with pytest.raises(ValueError):
        zp.write_zuliprc(tmp_path / "x" / "a.zuliprc", "https://s", "", "K" * 32)


def test_a_bot_missing_from_the_users_listing_is_found_by_its_email(tmp_path, roster):
    """Thu Oct 8 on the org (issue #36): `GET /users` left out an active bot that
    `GET /users/<e-mail>` returned. The lookup by the bot's own address fills in the id; an
    answer that is not an active bot with that address is still an error, never a guess."""

    class Unlisted(FakeAdmin):
        def users(self):  # the listing omits the bot; the lookup by e-mail still knows it
            return [u for u in super().users() if u["user_id"] != 201]

    admin = Unlisted()
    rows = rows_of(roster)[:1]
    out = tmp_path / "out"
    reports = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=False, regenerate=False
    )
    assert reports[0].bots["codex"] == "reused", reports[0].notes
    assert not any("codex bot" in n for n in reports[0].notes)
    assert ("GET", "users/codex-alice-bot@org", {}) in admin.calls
    assert "codex-alice-bot@org" in (out / "alice" / "codex.zuliprc").read_text()

    # the address answers, but not with an active bot of that address: still NO_USER_ID
    for change in ({"is_bot": False}, {"is_active": False}, {"delivery_email": "other@org"}):
        admin = Unlisted()
        admin._users = [dict(u, **change) if u["user_id"] == 201 else u for u in admin._users]
        reports = zp.provision(
            rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=True, regenerate=False
        )
        assert any("codex bot" in n and "not in the users listing" in n for n in reports[0].notes)


def test_deactivate(roster):
    admin = FakeAdmin()
    rows = rows_of(roster)[:2]
    reports = zp.deactivate(rows, admin, dry_run=True)
    assert reports[0].bots == {"codex": "would deactivate", "claude": "absent"}
    assert not admin.writes()
    reports = zp.deactivate(rows, admin, dry_run=False)
    assert reports[0].bots == {"codex": "deactivated", "claude": "absent"}
    assert ("DELETE", "bots/201", {}) in admin.calls  # the id resolved through GET /users
    assert reports[1].bots == {"codex": "absent", "claude": "absent"}


TMP = "zab-test-tmp-6ad1428c"  # A9's throwaway row on Oct 15


def member_deactivation(tmp_path, monkeypatch) -> tuple[MemberRealm, list[str]]:
    """A9's `!deactivate-bots` on the rig, Oct 15 (#396): the provisioner `zab-tester` (30), a
    Member, owns the throwaway row's bots, and the bulk `GET /users` leaves out the Claude bot,
    made moments before. `main`'s arguments, as the admin bot gives them."""
    realm = MemberRealm()
    realm.human(7, "Drew", "drew@example.test", role=100)
    realm.human(30, "zab-tester", "tester@example.test")
    realm.human(41, "zab-tester2", "tester2@example.test")
    out = tmp_path / "out"
    for uid, kind in ((300, "codex"), (301, "claude")):
        email = f"{kind}-{TMP}-bot@org.zulipchat.com"
        realm.bot(uid, zp.bot_full_name(kind, TMP), email, owner=30)
        zp.write_zuliprc(out / TMP / f"{kind}.zuliprc", f"https://{realm.host}", email, "K" * 32)
    realm.unlisted = {301}
    roster = tmp_path / "roster.csv"
    roster.write_text(f"slug,email,zulip_user_id\n{TMP},nobody@example.test,\n")
    rc = tmp_path / "provisioner.zuliprc"
    rc.write_text(f"[api]\nemail=tester@example.test\nkey={'P' * 32}\nsite=https://{realm.host}\n")
    real = zp.ZulipAdmin
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a: real(*a, transport=realm.transport()))
    argv = ["--roster", str(roster), "--zuliprc", str(rc), "--out-dir", str(out), "--deactivate"]
    return realm, argv


def test_deactivate_finds_a_bot_left_out_of_the_listing_by_its_email(tmp_path, monkeypatch, capsys):
    """#396: the dry run failed with `claude bot: not in the users listing`. A bot that `GET
    /bots` lists and `GET /users` leaves out is looked up by its own e-mail, which a Member's
    key may see, and deactivated; a rerun finds it deactivated already, the same way."""
    realm, argv = member_deactivation(tmp_path, monkeypatch)
    admin = zp.ZulipAdmin(f"https://{realm.host}", "tester@example.test", "P" * 32)
    listed = {u["user_id"]: u for u in admin.users()}  # what the provisioner's key gets
    admin.close()
    assert 300 in listed and 301 not in listed
    assert listed[41]["email"] == f"user41@{realm.host}" and listed[41]["delivery_email"] is None
    realm.requests.clear()
    assert zp.main([*argv, "--dry-run"]) == 0
    assert "codex: would deactivate, claude: would deactivate" in capsys.readouterr().out
    assert realm.asked(f"users/claude-{TMP}-bot@org.zulipchat.com")
    assert all(u["is_active"] for u in realm.users.values())
    assert zp.main(argv) == 0
    assert "codex: deactivated, claude: deactivated" in capsys.readouterr().out
    assert not realm.users[300]["is_active"] and not realm.users[301]["is_active"]
    assert zp.main(argv) == 0  # the rerun: neither is among the provisioner's active bots now
    assert "codex: deactivated already, claude: deactivated already" in capsys.readouterr().out


def test_deactivate_reports_a_bot_that_no_lookup_finds(tmp_path, monkeypatch, capsys):
    """Only when the lookup by e-mail fails too is the bot reported, and left alone."""
    realm, argv = member_deactivation(tmp_path, monkeypatch)
    handler = realm.handler

    def no_lookup(request):
        if request.url.path.endswith(f"/users/claude-{TMP}-bot@org.zulipchat.com"):
            return realm.error(400, "BAD_REQUEST", "No such user")
        return handler(request)

    realm.handler = no_lookup
    assert zp.main(argv) == 1
    assert (
        "claude bot: not in the users listing, nor found by its e-mail; deactivate it in the UI"
    ) in capsys.readouterr().out
    assert not realm.users[300]["is_active"] and realm.users[301]["is_active"]


def test_main_writes_roster_and_reports_failures(tmp_path, roster, monkeypatch, capsys):
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rc = tmp_path / "admin.zuliprc"
    rc.write_text("[api]\nemail=drew@org\nkey=" + "Z" * 32 + "\nsite=https://org.zulipchat.com\n")
    code = zp.main(
        ["--roster", str(roster), "--zuliprc", str(rc), "--out-dir", str(tmp_path / "o")]
    )
    assert code == 0  # carol and dave are not on Zulip, which is not a failure (#628)
    out = capsys.readouterr()
    assert "alice" in out.out and "claim GitHub carol" in out.out  # a warning only
    assert out.out.endswith(f"\n\n{not_on_zulip('carol')}\n{not_on_zulip('dave')}\n")
    assert "Z" * 32 not in out.out + out.err
    rows = rows_of(roster)
    assert {r["github"]: r["zulip_user_id"] for r in rows} == {
        "alice": "101",
        "bob": "102",
        "carol": "",  # the stale 999 was cleared
        "dave": "",
    }
    # --only merges ids back without dropping other rows, cleared ids included
    for r in rows:
        if r["github"] == "bob":
            r["zulip_user_id"] = "999"
    zp.write_roster(str(roster), rows)
    # bob left the org (and nobody else carries his login in the GitHub field)
    admin._users = [u for u in admin._users if u["user_id"] not in (102, 105)]
    code = zp.main(
        [
            "--roster",
            str(roster),
            "--zuliprc",
            str(rc),
            "--out-dir",
            str(tmp_path / "o"),
            "--only",
            "bob",
        ]
    )
    assert code == 0  # bob left: not on Zulip, and nothing is sent
    assert capsys.readouterr().out.endswith(f"\n\n{not_on_zulip('bob')}\n")
    after = rows_of(roster)
    assert [r["github"] for r in after] == ["alice", "bob", "carol", "dave"]
    assert {r["github"]: r["zulip_user_id"] for r in after}["bob"] == ""
    assert {r["github"]: r["zulip_user_id"] for r in after}["alice"] == "101"
    invite = {r["github"]: r["zulip_invite"] for r in after}
    # kept: `roster_import.py` counts `joined` as a binding once the id is cleared (ADMIN §3.1)
    assert invite["bob"] == "joined" and invite["alice"] == "joined"


def test_main_refuses_a_roster_with_duplicates(tmp_path, roster, monkeypatch, capsys):
    """PM review of PR #47 (1): two rows sharing an e-mail (or login, host, ip, id) stop the run
    before any request is made."""
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rc = tmp_path / "admin.zuliprc"
    rc.write_text("[api]\nemail=drew@org\nkey=" + "Z" * 32 + "\nsite=https://org.zulipchat.com\n")
    roster.write_text(ROSTER + "eve,Eve,Adams,Alice@Example.org,eve/x,5,,,,\n")
    code = zp.main(["--roster", str(roster), "--zuliprc", str(rc)])
    assert code == 1 and not admin.calls
    assert "roster: duplicate email" in capsys.readouterr().err


def test_zulip_admin_deactivates_a_bot_through_the_bots_endpoint():
    """`DELETE /users/{id}` answers "No such user" for a bot (Sun Oct 11): `DELETE /bots/{id}`."""
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE" and request.url.path.endswith("/api/v1/bots/201"):
            return httpx.Response(200, json={"result": "success", "msg": ""})
        return httpx.Response(
            400, json={"result": "error", "code": "BAD_REQUEST", "msg": "No such user"}
        )

    admin = zp.ZulipAdmin(
        "https://org.zulipchat.com/", "d@org", "k" * 32, transport=httpx.MockTransport(handler)
    )
    admin.deactivate_bot(201)
    assert [(r.method, r.url.path) for r in seen] == [("DELETE", "/api/v1/bots/201")]
    admin.close()


def test_zulip_admin_request_encodes_and_raises(tmp_path):
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"result": "success", "members": []})
        if request.url.path.endswith("/users/7"):
            return httpx.Response(
                200, json={"result": "success", "user": {"user_id": 7, "email": "b@org"}}
            )
        return httpx.Response(400, json={"result": "error", "code": "BAD_REQUEST", "msg": "nope"})

    admin = zp.ZulipAdmin(
        "https://org.zulipchat.com/", "d@org", "k" * 32, transport=httpx.MockTransport(handler)
    )
    assert admin.users() == []
    assert seen[0].url.params["include_custom_profile_fields"] == "true"
    assert admin.user(7) == {"user_id": 7, "email": "b@org"} and seen[1].url.path.endswith(
        "/users/7"
    )
    assert seen[0].headers["Authorization"].startswith("Basic ")
    with pytest.raises(zp.ZulipAdminError) as exc:
        admin.create_bot("X · y", "x-y")
    assert exc.value.code == "BAD_REQUEST" and "k" * 32 not in str(exc.value)
    with pytest.raises(ValueError):
        admin.request("GET", "/users")
    admin.close()


def test_write_zuliprc_mode(tmp_path):
    path = tmp_path / "d" / "x.zuliprc"
    zp.write_zuliprc(path, "https://s", "e@s", "K" * 32)
    zp.write_zuliprc(path, "https://s", "e@s", "L" * 32)  # overwrite keeps the mode
    assert oct(path.stat().st_mode & 0o777) == "0o600" and "L" * 32 in path.read_text()
    assert os.access(path, os.R_OK)
    assert isinstance(pathlib.Path(zp.OUT_DIR), pathlib.Path) and zp.OUT_DIR.name == "out"


def test_legacy_mixed_case_bots_and_channels_are_reused(tmp_path, roster):
    """Codex round 2 on #97: a login-only roster created `Codex · Alice` and `Alice-sandbox`;
    the derived slug `alice` must find them, not create duplicates (Zulip refuses those)."""
    admin = FakeAdmin()
    admin._bots[0]["full_name"] = "Codex · Alice"
    admin._streams[0]["name"] = "Alice-sandbox"
    rows = rows_of(roster)[:1]
    reports = zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
    )
    assert reports[0].bots["codex"] == "reused" and reports[0].channel == "exists"
    assert ("POST", "bots", {"full_name": "Codex · alice", "short_name": "codex-alice"}) not in (
        admin.calls
    )
    assert zp.deactivate(rows, admin, dry_run=True)[0].bots["codex"] == "would deactivate"


def test_descriptions_name_a_qualified_repository():
    """Codex round 3 on #97: the repository is `owner/name`, the workshop's owner the MathAgents
    organization. Zulip strips newlines from descriptions, so the steps take the description
    and the repository goes in "Start here"."""
    assert zp.qualified_repo({"slug": "ada", "repo": ""}) == "MathAgents/ada"
    assert zp.qualified_repo({"slug": "ada", "repo": "thesis"}) == "MathAgents/thesis"
    assert zp.qualified_repo({"slug": "ada", "repo": "alice/thesis.git"}) == "alice/thesis"
    text = zp.start_here_text("ada", "MathAgents/ada")
    assert "repository `MathAgents/ada`" in text
    # not everyone gets the gifts (Drew, Wed Oct 7), named as the quick-start does (#475)
    assert "Claude Max (from Anthropic) and ChatGPT Pro (from OpenAI)" in zp.GIFTS_STEP
    assert zp.GIFTS_STEP.startswith("Accept any gift subscriptions")
    # one place to edit them (#440), or the workshop's own (#709): step 1, and the description
    assert f"\n1. {zp.GIFTS_STEP} Sign in to Claude and ChatGPT with the accounts that " in text
    assert f"Start here: {zp.GIFTS_STEP} In ChatGPT's settings" in zp.channel_description("ada")
    # the setting by what it does, not by today's menu (Drew, Nov 1)
    assert "\n2. In ChatGPT's settings, under security, turn on **device code sign-in** " in text
    assert "Allow device code login" not in text + zp.channel_description("ada")
    # how to talk to one agent (#713 adapts the mention)
    assert "To talk to just one, type `@claude` or `@codex`. `!help` lists the commands." in text
    assert "pick the one with your name" not in text  # a typed mention counts (#713)
    assert "**Claude · ada** and **Codex · ada**" in text
    # repository access comes from the GitHub profile field (PM on issue #36, Mon 06:40)
    assert "put your GitHub username in the **GitHub** field of your Zulip profile" in text
    assert "and GitHub will e-mail you an invitation" in text
    # the personal section of ~/.codex/AGENTS.md (PM on issue #36, Mon 19:00)
    assert "Tell your agents about yourself" in text
    assert "both agents will know it in new sessions" in text
    # file links go to the central file server (Drew on issue #36, Mon 04:01)
    assert "open for anyone who has the link, and from any of them the rest of your " in text
    # no git terms, and no warning that can't come up in the sandbox (#709)
    for gone in ("branch", "merge", "main", "is not subscribed", "several times a day"):
        assert gone not in text, gone
    # the repository last, after the sign-ins and the first request (#709)
    order = ["Before you start:", "\n3. **Claude:**", "\n4. **Codex:**", "Then start", "Your files"]
    assert [text.index(part) for part in order] == sorted(text.index(part) for part in order)
    assert "invitation" not in zp.channel_description("ada")
    assert "\n" not in zp.channel_description("ada") and len(zp.channel_description("ada")) < 1024


def test_start_here_names_the_file_host_in_code_and_the_workshops_gift_step():
    """#709: the host comes from the bridges' own `--refserve-url`, in code, so that Zulip
    doesn't link the server's root, which answers `Not found` (Drew, Oct 31); with no file
    server, no sentence about its links. The gift step is the workshop's own when it has one."""
    text = zp.start_here_text("ada", "MathAgents/ada")
    assert "Links to `files.mathagents.org` in the agents' answers" in text
    # #710: right after it, the share link, one sentence, which ends the text
    assert text.endswith(
        "as it was at that answer. To share one file or folder alone, ask your agent for a "
        "share link."
    )
    other = zp.Wording(refserve_url="https://files.example.org:8443")
    assert "Links to `files.example.org:8443` in" in zp.start_here_text("ada", "x/ada", None, other)
    none = zp.start_here_text("ada", "x/ada", None, zp.Wording(refserve_url=""))
    assert none.endswith("and GitHub will e-mail you an invitation.") and "Links to" not in none
    gifts = "Accept your gifts: Claude Max from the e-mail of Oct 30, ChatGPT Pro at [the form](x)."
    ours = zp.Wording(gifts=gifts)
    assert f"\n1. {gifts} Sign in to Claude" in zp.start_here_text("ada", "x/ada", None, ours)
    assert f"Start here: {gifts} In ChatGPT's" in zp.channel_description("ada", None, ours)
    assert zp.GIFTS_STEP not in zp.start_here_text("ada", "x/ada", None, ours)


def test_the_gift_step_flags_take_one_line_of_text(tmp_path, roster, monkeypatch, capsys):
    """`--gifts-step` for a hand run, `--gifts-step-b64` for the admin bot, which encodes it as
    `zab.admin.actions` does (base64url of its UTF-8); each refused unless it is admin.toml's
    rule, `gifts_step_ok`."""
    from zab.admin import config

    assert (zp._GIFTS_STEP_RE.pattern, zp.GIFTS_STEP_BYTES) == (
        config._GIFTS_STEP.pattern,
        config.GIFTS_STEP_BYTES,
    )
    text = "Accept your gifts: «Claude Max» from Anthropic's e-mail, ChatGPT Pro at [a form](x)."
    encoded = base64.urlsafe_b64encode(text.encode()).decode()
    assert zp.decode_gifts_step(encoded) == text
    for bad in ("", "-x", "two\nlines", "a\x07b", " a", "a ", "é" * 251):
        assert not zp.gifts_step_ok(bad), bad
        with pytest.raises(ValueError):
            zp.decode_gifts_step(base64.urlsafe_b64encode(bad.encode()).decode())
    for bad in ("not base64!", "QQ", "QQ=", "/w==", "_w=="):  # not base64url, or not UTF-8
        with pytest.raises(ValueError):
            zp.decode_gifts_step(bad)
    assert (zp.decode_gifts_step("QQ=="), zp.decode_gifts_step("QUI=")) == ("A", "AB")
    for other in ("QR==", "QUJ="):  # "A" and "AB" again, with padding bits set: not canonical
        with pytest.raises(ValueError, match="canonical"):
            zp.decode_gifts_step(other)
    assert zp.gifts_step_ok("é" * 250) and zp.gifts_step_ok("A")
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rc = tmp_path / "admin.zuliprc"
    rc.write_text(ADMIN_RC)
    argv = ["--roster", str(roster), "--zuliprc", str(rc), "--out-dir", str(tmp_path / "o")]
    shown = f'"Start here" step 1 and the description begin: {text}'
    assert zp.GIFTS_SHOWN.format(text) == shown
    assert zp.main([*argv, "--only", "dave", "--gifts-step-b64", encoded, "--dry-run"]) == 0
    assert capsys.readouterr().out.count(shown) == 1  # the plan's approvers read the text
    assert zp.main([*argv, "--only", "dave", "--gifts-step-b64", encoded]) == 0
    assert shown not in capsys.readouterr().out  # the apply's result doesn't repeat it
    posts = [post for sent in admin.bot_posts.values() for _, _, post in sent]
    assert len(posts) == 1 and f"\n1. {text} Sign in" in posts[0]
    assert zp.main([*argv, "--only", "dave", "--dry-run"]) == 0
    assert "step 1 and the description begin" not in capsys.readouterr().out  # the default
    for flags in (
        ["--gifts-step", "-x"],
        ["--gifts-step=-x"],
        ["--gifts-step", ""],
        ["--gifts-step-b64", "QQ="],
        ["--gifts-step-b64", ""],
        ["--gifts-step", "a", "--gifts-step-b64", encoded],
        ["--refserve-url", "http://files.example.org"],
    ):
        with pytest.raises(SystemExit):
            zp.main([*argv, *flags])


def not_on_zulip(slug: str) -> str:
    """The line after the table for a row whose address no Zulip user has (#628)."""
    return (
        f"{slug}: not on Zulip: invite {slug}@example.org in Zulip (Invite users, with the "
        "participants' channel ticked), then provision again once they have joined"
    )


def test_no_invitation_is_sent_and_a_row_not_on_zulip_is_bound_once_they_join(tmp_path, roster):
    """#628: the organizers invite in Zulip, so a run never calls `POST /invites` (nor reads
    `GET /invites`), in a dry run or a real one, and both report the row the same way: `not on
    Zulip`, with one line to invite the address, and no failure. A legacy `zulip_invite` (an
    invitation sent before 0.2.10) is left alone. Once the participant joins under the roster's
    address, the next run binds them, subscribes them, hands their sandbox over and records
    `joined`."""
    admin = FakeAdmin()
    rows = rows_of(roster)[3:4]  # dave, not joined
    rows[0]["zulip_invite"] = "sent 2026-10-20 channel 77"
    kw = {"bot_client": admin.bot_client, "out_dir": tmp_path / "out", "regenerate": False}
    planned = zp.provision(rows, admin, dry_run=True, **kw)
    done = zp.provision(rows, admin, dry_run=False, **kw)
    for reports in (planned, done):
        assert reports[0].invite == "not on Zulip" and reports[0].ok and not reports[0].notes
        assert zp.render(reports).endswith(f"\n\n{not_on_zulip('dave')}")
    assert not [c for c in admin.calls if c[1].startswith("invites")]
    assert rows[0]["zulip_invite"] == "sent 2026-10-20 channel 77"
    assert done[0].start_here == "posted"  # the sandbox is ready for them
    sid = int(rows[0]["zulip_channel_id"])
    admin._users.append(
        {"user_id": 106, "email": "u106@org", "delivery_email": "dave@example.org",
         "is_bot": False, "profile_data": {}}
    )  # fmt: skip
    admin.calls.clear()
    joined = zp.provision(rows, admin, dry_run=False, **kw)
    assert joined[0].invite == "joined" and joined[0].ok and rows[0]["zulip_invite"] == "joined"
    assert rows[0]["zulip_user_id"] == "106" and "not on Zulip" not in zp.render(joined)
    subscribed = [c[2]["principals"] for c in admin.writes() if c[1] == "users/me/subscriptions"]
    assert subscribed and all(106 in p for p in subscribed)
    assert ("hand over", ["can_administer_channel_group"]) in sandbox_writes(admin, "dave-sandbox")
    assert int(rows[0]["zulip_channel_id"]) == sid
    assert not [c for c in admin.calls if c[1].startswith("invites")]


def test_a_row_without_an_e_mail_needs_attention():
    report = zp.Report(slug="erin")
    zp.invite_state({"slug": "erin", "email": " "}, report)
    assert report.invite == "" and not report.ok
    assert report.notes == ["no e-mail in the roster: add one, then invite it in Zulip"]


def test_zulip_admin_has_no_way_to_invite():
    """#628: the client the script uses has no invitation call left, so none can be made."""
    assert not hasattr(zp.ZulipAdmin, "invite")


def test_a_legacy_login_folder_is_moved_to_the_slug(tmp_path, roster):
    """Drew's rc4 push (issue #36): keys an rc3 run wrote to `out/<github>/` move to `out/<slug>/`
    before any key is written, so the reused bots keep their keys."""
    rows = rows_of(roster)[:1]
    rows[0]["github"], rows[0]["slug"] = "Alice", "alice"
    out = tmp_path / "out"
    (out / "Alice").mkdir(parents=True)
    (out / "Alice" / "codex.zuliprc").write_text("[api]\nkey=old\n")
    admin = FakeAdmin()
    dry = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=True, regenerate=False
    )
    assert "would move out/Alice/ to out/alice/" in dry[0].warnings and (out / "Alice").is_dir()
    reports = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=False, regenerate=False
    )
    assert "moved out/Alice/ to out/alice/" in reports[0].warnings
    assert not (out / "Alice").exists() and (out / "alice" / "claude.zuliprc").is_file()
    again = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=False, regenerate=False
    )
    assert not any("move" in w for w in again[0].warnings)


# ------------------------------------------------------- --hold (PM on issue #36, Sun 22:40)


def provision_rows(admin, rows, tmp_path, **kw):
    return zp.provision(
        rows,
        admin,
        bot_client=admin.bot_client,
        out_dir=tmp_path / "out",
        dry_run=False,
        regenerate=False,
        **kw,
    )


def sandbox_writes(admin, name):
    """The subscribers and descriptions the run gave the channel `name`."""
    sid = next((s["stream_id"] for s in admin._streams if s["name"] == name), None)
    out = []
    for method, endpoint, payload in admin.writes():
        if endpoint == "channels/create" and payload["name"] == name:
            out.append(("create", payload["subscribers"], payload["description"]))
        elif endpoint == "users/me/subscriptions" and payload["name"] == name:
            out.append(("subscribe", payload["principals"]))
        elif method == "PATCH" and endpoint == f"streams/{sid}" and "description" in payload:
            out.append(("description", payload["description"]))
        elif method == "PATCH" and endpoint == f"streams/{sid}":
            out.append(("hand over", sorted(payload)))
    return out


def test_hold_makes_bots_and_bots_only_sandboxes_and_nothing_else(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)
    before = [dict(r) for r in rows]
    reports = provision_rows(admin, rows, tmp_path, hold=True)
    by = {r.slug: r for r in reports}
    assert all(r.ok and r.invite == "held" and r.start_here == "held" for r in reports)
    # the zuliprc files push_config needs on Wednesday
    assert (tmp_path / "out" / "bob" / "codex.zuliprc").exists()
    assert (tmp_path / "out" / "dave" / "claude.zuliprc").exists()
    # bob has joined, but only his two bots are subscribed, under the neutral description
    assert sandbox_writes(admin, "bob-sandbox") == [("create", [302, 303], zp.HOLD_DESCRIPTION)]
    # alice's existing channel: the bots only, and the neutral description
    # alice's existing channel keeps its description (PM review, Mon 09:30): the bots only
    assert sandbox_writes(admin, "alice-sandbox") == [("subscribe", [201, 301])]
    assert not [c for c in admin.calls if c[1].startswith("invites")]  # no invitation
    assert not admin.bot_posts  # no "Start here"
    # the roster changes only in zulip_user_id and the sandbox's zulip_channel_id
    ids = ("zulip_user_id", "zulip_channel_id")
    for old, new in zip(before, rows, strict=True):
        assert {k: v for k, v in new.items() if k not in ids} == {
            k: v for k, v in old.items() if k not in ids
        }
    assert rows[0]["zulip_channel_id"] == "31" and rows[1]["zulip_channel_id"] == "91"
    assert rows[0]["zulip_user_id"] == "101" and by["alice"].user_id == 101
    assert "Instructions will be posted here" in zp.HOLD_DESCRIPTION


def test_a_full_run_after_hold_releases_everything_once(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)
    provision_rows(admin, rows, tmp_path, hold=True)
    admin.calls.clear()
    reports = {r.slug: r for r in provision_rows(admin, rows, tmp_path)}
    full = zp.channel_description("bob", {"codex": 302, "claude": 303})
    assert sandbox_writes(admin, "bob-sandbox") == [
        ("description", full),
        ("subscribe", [102, 302, 303]),  # bob joins his sandbox now
        ("hand over", ["can_administer_channel_group"]),  # and administers it (Tue 06:30)
    ]
    bob = next(c for c in admin._streams if c["name"] == "bob-sandbox")
    # Zulip made the organizer (the creator) administrator of the held channel; bob joins them
    assert bob["can_administer_channel_group"] == {
        "direct_members": [1, 102],
        "direct_subgroups": [],
    }
    assert reports["bob"].invite == "joined" and reports["bob"].start_here == "posted"
    assert reports["dave"].invite == "not on Zulip" and rows[3]["zulip_invite"] == ""
    assert all(len(posts) == 1 for posts in admin.bot_posts.values())
    assert len(admin.bot_posts) == 4
    # and a second full run changes nothing
    admin.calls.clear()
    reports = {r.slug: r for r in provision_rows(admin, rows, tmp_path)}
    assert not [c for c in admin.writes() if c[0] == "PATCH" or c[1] in ("invites", "bots")]
    assert all(r.start_here == "exists" for r in reports.values())


def test_hold_after_a_full_run_takes_nothing_back(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)
    provision_rows(admin, rows, tmp_path)
    posts = {sid: list(p) for sid, p in admin.bot_posts.items()}
    admin.calls.clear()
    reports = provision_rows(admin, rows, tmp_path, hold=True)
    assert all(r.ok and r.invite == "held" for r in reports)
    # descriptions stay the full ones, nobody is removed (Zulip subscriptions only add), no post
    assert not [c for c in admin.writes() if c[0] == "PATCH" or c[1] in ("invites", "bots")]
    subscribed = [c[2]["principals"] for c in admin.writes() if c[1] == "users/me/subscriptions"]
    assert subscribed and all(101 not in p and 102 not in p for p in subscribed)
    assert admin.bot_posts == posts
    assert rows[3]["zulip_invite"] == ""  # dave never joined: nothing recorded


def test_main_hold_flag(tmp_path, roster, monkeypatch, capsys):
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rc = tmp_path / "admin.zuliprc"
    rc.write_text("[api]\nemail=drew@org\nkey=" + "Z" * 32 + "\nsite=https://org.zulipchat.com\n")
    argv = ["--roster", str(roster), "--zuliprc", str(rc), "--out-dir", str(tmp_path / "o")]
    assert zp.main([*argv, "--hold"]) == 0
    out = capsys.readouterr().out
    assert out.count("held") >= 8 and not admin.bot_posts
    assert all(r["zulip_invite"] == "" for r in rows_of(roster))


# ------------------------------------------- DM links in "Start here" (PM on #36, Mon 07:55)


@pytest.mark.parametrize(
    "uid,name,link",
    [
        (301, "Claude · alice", "#narrow/dm/301-Claude-.C2.B7-alice"),
        (7, 'A (b). "x"/y', "#narrow/dm/7-A-.28b.29.2E-x-y"),  # Zulip's hash encoding
        (9, "tab\there", "#narrow/dm/9-tab-here"),
    ],
)
def test_dm_link_has_zulips_form(uid, name, link):
    """`#narrow/dm/<id>-<slug>` as web/src/people.ts builds it (`user_ids_to_slug`,
    `get_slug_from_full_name`, `encodeHashComponent`); the web app reads only the id."""
    assert zp.dm_link(uid, name) == link


def test_start_here_links_both_bots_in_their_sign_in_steps():
    """#709: each agent's sign-in as its flow goes, the bot's name a link to its DM: Claude's code
    comes back into the DM, Codex's is typed at the link, and nothing goes back."""
    ids = {"claude": 301, "codex": 201}
    text = zp.start_here_text("alice", "MathAgents/alice", ids)
    claude = next(line for line in text.splitlines() if line.startswith("3. "))
    codex = next(line for line in text.splitlines() if line.startswith("4. "))
    assert claude.startswith(
        "3. **Claude:** click **[Claude · alice](#narrow/dm/301-Claude-.C2.B7-alice)** to open a "
        "direct message with it, and send `!login`."
    )
    assert "**copy it and paste it back** into the direct message" in claude
    assert codex.startswith(
        "4. **Codex:** click **[Codex · alice](#narrow/dm/201-Codex-.C2.B7-alice)** and send "
        "`!login`."
    )
    assert "**type the code there**. Nothing goes back into the direct message" in codex
    assert "\n\nEach sign-in waits 10 minutes; if it runs out, send `!login` again.\n\n" in text
    # a dry run before the bots exist knows no ids: plain names, no broken links
    plain = zp.start_here_text("alice", "MathAgents/alice")
    assert "#narrow" not in plain and "click **Claude · alice** to open" in plain


def test_the_description_links_the_bots_and_fits_on_one_line():
    longest = "a" * 32  # the longest slug (aws_common._SLUG_RE)
    text = zp.channel_description(longest, {"claude": 9_999_999, "codex": 9_999_998})
    assert f"(#narrow/dm/9999999-Claude-.C2.B7-{longest})" in text
    assert f"(#narrow/dm/9999998-Codex-.C2.B7-{longest})" in text
    assert "\n" not in text and len(text) < 1024  # Stream.MAX_DESCRIPTION_LENGTH
    assert "Then send `!login` in a **direct message** to each bot" in text  # #709
    # with the longest gift step a workshop may give (#709): 500 bytes, so 500 characters at most
    gifts = zp.Wording(gifts="x" * zp.GIFTS_STEP_BYTES)
    ids = {"claude": 9_999_999_999, "codex": 9_999_999_998}
    assert len(zp.channel_description(longest, ids, gifts)) < 1024


def test_provisioned_start_here_and_description_use_the_real_bot_ids(tmp_path, roster):
    admin = FakeAdmin()
    provision_rows(admin, rows_of(roster), tmp_path)
    post = admin.bot_posts[31][0][2]  # alice: Codex 201 (reused), Claude 301 (created)
    assert "(#narrow/dm/301-Claude-.C2.B7-alice)" in post
    assert "(#narrow/dm/201-Codex-.C2.B7-alice)" in post
    bob = next(s for s in admin._streams if s["name"] == "bob-sandbox")
    assert "(#narrow/dm/303-Claude-.C2.B7-bob)" in bob["description"]


def test_hold_never_changes_an_existing_description(tmp_path, roster):
    """PM review (Mon 09:30): a `--hold` run after a full run must not put the hold text back,
    also when the full text has changed since (here: from before the DM links). The next full
    run brings it up to date."""
    admin = FakeAdmin()
    old_full = zp.channel_description("alice")  # no links: as provisioned before this change
    admin._streams[0]["description"] = old_full
    rows = rows_of(roster)[:1]
    provision_rows(admin, rows, tmp_path, hold=True)
    assert not [c for c in admin.writes() if c[0] == "PATCH"]
    assert admin._streams[0]["description"] == old_full
    provision_rows(admin, rows, tmp_path)
    assert "(#narrow/dm/301-Claude-.C2.B7-alice)" in admin._streams[0]["description"]


# ------------------------------------------- participants administer their sandboxes (Tue 06:30)


def join(admin, user_id, email):
    admin._users.append(
        {"user_id": user_id, "email": f"u{user_id}@org", "delivery_email": email, "is_bot": False}
    )


def test_a_participant_administers_their_sandbox_from_their_first_run_as_member(tmp_path, roster):
    """Drew (issue #36, Tue 06:30): shared history, and the participant administers their
    sandbox (invite collaborators, change privacy and history). A new sandbox of a joined
    participant is created that way; one created before they joined is handed over on their first
    run as a member, and never touched again."""
    admin = FakeAdmin()
    rows = rows_of(roster)
    provision_rows(admin, rows, tmp_path)
    creates = {c[2]["name"]: c[2] for c in admin.writes() if c[1] == "channels/create"}
    assert creates["bob-sandbox"]["admin_id"] == 102  # joined: bob administers it
    assert creates["dave-sandbox"]["admin_id"] is None  # not on Zulip: Zulip's default for now
    # only alice's sandbox, which existed before her first run as a member, is handed over
    handed = [c[1] for c in admin.writes() if c[0] == "PATCH" and "description" not in c[2]]
    assert handed == ["streams/31"]
    # dave joins Zulip, through the organizers' invitation; the next run hands it over
    join(admin, 106, "dave@example.org")
    admin.calls.clear()
    reports = {r.slug: r for r in provision_rows(admin, rows, tmp_path)}
    dave = next(s for s in admin._streams if s["name"] == "dave-sandbox")
    assert reports["dave"].ok and rows[3]["zulip_invite"] == "joined"
    assert sandbox_writes(admin, "dave-sandbox")[-1] == (
        "hand over",
        ["can_administer_channel_group"],
    )
    assert dave["can_administer_channel_group"] == {
        "direct_members": [1, 106],
        "direct_subgroups": [],
    }
    # dave switches it to protected history and makes it his alone: later runs leave it be
    dave["history_public_to_subscribers"] = False
    dave["can_administer_channel_group"] = {"direct_members": [106], "direct_subgroups": []}
    admin.calls.clear()
    provision_rows(admin, rows, tmp_path)
    assert not [c for c in admin.writes() if c[0] == "PATCH"]
    assert dave["history_public_to_subscribers"] is False


def test_a_failed_hand_over_is_retried_on_the_next_run(tmp_path, roster):
    admin = FakeAdmin()
    rows = rows_of(roster)[:2]
    provision_rows(admin, rows, tmp_path, hold=True)
    admin.fail_update = True
    reports = {r.slug: r for r in provision_rows(admin, rows, tmp_path)}
    assert not reports["bob"].ok
    assert any(
        "could not make the participant its administrator" in n for n in reports["bob"].notes
    )
    assert rows[1]["zulip_invite"] == ""  # not "joined": the next run tries again
    admin.fail_update = False
    admin.calls.clear()
    reports = {r.slug: r for r in provision_rows(admin, rows, tmp_path)}
    assert reports["bob"].ok and rows[1]["zulip_invite"] == "joined"
    assert sandbox_writes(admin, "bob-sandbox")[-1][0] == "hand over"


def test_a_protected_sandbox_gets_shared_history_when_handed_over(tmp_path, roster):
    """A sandbox created before shared history (a rehearsal's) is switched at the participant's
    first run as a member; the update names the administrators it replaces (`old`), so a change
    made in the meantime makes Zulip refuse it rather than be overwritten."""
    admin = FakeAdmin()
    old = {"direct_members": [1], "direct_subgroups": []}
    admin._streams[0].update(history_public_to_subscribers=False, can_administer_channel_group=old)
    rows = rows_of(roster)[:1]
    provision_rows(admin, rows, tmp_path)
    patch = next(c[2] for c in admin.writes() if c[0] == "PATCH" and "description" not in c[2])
    assert patch == {
        "can_administer_channel_group": {
            "new": {"direct_members": [1, 101], "direct_subgroups": []},
            "old": old,
        },
        "history_public_to_subscribers": True,
    }


def test_a_renamed_sandbox_is_found_by_its_recorded_id(tmp_path, roster):
    """Drew (Tue 06:30, nit): a participant may rename their sandbox; the roster's
    `zulip_channel_id` finds it, so a rerun creates no second `<slug>-sandbox`."""
    admin = FakeAdmin()
    rows = rows_of(roster)[:1]
    provision_rows(admin, rows, tmp_path)
    assert rows[0]["zulip_channel_id"] == "31"
    admin._streams[0]["name"] = "Elliptic curves with Alice"
    admin.calls.clear()
    reports = provision_rows(admin, rows, tmp_path)
    assert reports[0].ok and reports[0].channel == "exists"
    assert not [c for c in admin.writes() if c[1] == "channels/create"]
    subscribe = next(c[2] for c in admin.writes() if c[1] == "users/me/subscriptions")
    assert subscribe["name"] == "Elliptic curves with Alice"
    # an id that no longer exists (the channel was archived) falls back to the name
    assert zp.find_channel(admin._streams, "alice", "999") is None
    assert (
        zp.find_channel([{"stream_id": 5, "name": "Alice-Sandbox"}], "alice", "999")["stream_id"]
        == 5
    )


def test_merge_roster_records_the_channel_id(tmp_path, roster):
    rows = rows_of(roster)[:1]
    rows[0].update(zulip_user_id="101", zulip_invite="joined", zulip_channel_id="31")
    zp.merge_roster(str(roster), rows)
    after = rows_of(roster)
    assert after[0]["zulip_channel_id"] == "31" and after[1]["zulip_channel_id"] == ""


def test_administrators_with_keeps_everyone_already_there():
    assert zp.administrators_with({"direct_members": [1], "direct_subgroups": [9]}, 7) == {
        "direct_members": [1, 7],
        "direct_subgroups": [9],
    }
    assert zp.administrators_with({"direct_members": [7], "direct_subgroups": []}, 7) is None
    # a named group (its id) becomes a subgroup next to the participant
    assert zp.administrators_with(12, 7) == {"direct_members": [7], "direct_subgroups": [12]}
    assert zp.administrators_with(None, 7) == {"direct_members": [7], "direct_subgroups": []}


def test_create_channel_sends_shared_history_and_the_administrator():
    import httpx

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"result": "success", "id": 77})

    admin = zp.ZulipAdmin(
        "https://org.zulipchat.com", "d@org", "k" * 32, transport=httpx.MockTransport(handler)
    )
    assert admin.create_channel("a-sandbox", "d", [5, 6], 1, 9) == 77
    assert admin.create_channel("b-sandbox", "d", [5, 6], 1) == 77
    admin.update_channel(77, history_public_to_subscribers=True)
    admin.close()
    import json

    assert seen[0]["invite_only"] == "true" and seen[0]["history_public_to_subscribers"] == "true"
    assert json.loads(seen[0]["can_administer_channel_group"]) == {
        "direct_members": [9],
        "direct_subgroups": [],
    }
    assert "can_administer_channel_group" not in seen[1]  # Zulip's default: the creator
    assert seen[2] == {"history_public_to_subscribers": "true"}


# ------------------------------------------ the API rate limit (PM on issue #36, Wed 10:39)

LIMITED = {"result": "error", "code": "RATE_LIMIT_HIT", "msg": "API usage exceeded rate limit"}


@pytest.fixture
def slept(monkeypatch):
    """No real sleeping; the waits are recorded, and the clock is fixed at 1000."""
    waits: list[float] = []
    monkeypatch.setattr(zp, "_sleep", waits.append)
    monkeypatch.setattr(zp, "_clock", lambda: 1000.0)
    return waits


def scripted_admin(responses):
    """A real `ZulipAdmin` over a transport that answers with `responses` in turn."""
    import httpx

    seen: list[httpx.Request] = []
    answers = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(answers)

    admin = zp.ZulipAdmin(
        "https://org.zulipchat.com", "d@org", "k" * 32, transport=httpx.MockTransport(handler)
    )
    return admin, seen


def ok(**headers):
    import httpx

    return httpx.Response(200, json={"result": "success", "id": 7}, headers=headers)


def limited(json_body=LIMITED, **headers):
    import httpx

    return httpx.Response(429, json=json_body, headers=headers)


def test_a_429_is_retried_after_retry_after(slept, capsys):
    admin, seen = scripted_admin(
        [limited({**LIMITED, "retry-after": 3}, **{"Retry-After": "12"}), ok()]
    )
    assert admin.request("POST", "channels/create", name="x") == {"result": "success", "id": 7}
    assert len(seen) == 2 and seen[0].content == seen[1].content  # the same request again
    assert slept == [12.0]  # the header wins over the body
    assert "zulip: rate limited, waiting 12 s (attempt 2/6)" in capsys.readouterr().err


def test_a_429_with_only_the_body_wait(slept):
    admin, seen = scripted_admin([limited({**LIMITED, "retry-after": 2.5}), ok()])
    assert admin.request("GET", "users")["id"] == 7
    assert slept == [2.5] and len(seen) == 2


def test_the_wait_falls_back_to_the_reset_then_to_5_s_and_is_capped(slept):
    admin, _ = scripted_admin(
        [
            limited(**{"X-RateLimit-Reset": "1020"}),  # 20 s from the fixed clock, +1 (truncated)
            limited(),  # nothing at all: 5 s
            limited(**{"Retry-After": "3600"}),  # capped
            ok(),
        ]
    )
    admin.request("GET", "users")
    assert slept == [21.0, zp.RATE_LIMIT_DEFAULT_WAIT_S, zp.RATE_LIMIT_MAX_WAIT_S]


def test_six_429s_in_a_row_raise_as_before(slept):
    admin, seen = scripted_admin([limited(**{"Retry-After": "1"})] * 6)
    with pytest.raises(zp.ZulipAdminError) as exc:
        admin.request("POST", "messages", type="stream", to=31, topic="t", content="c")
    assert exc.value.code == "RATE_LIMIT_HIT" and exc.value.status == 429
    assert len(seen) == zp.RATE_LIMIT_ATTEMPTS == 6 and len(slept) == 5


def test_a_rate_limit_error_code_without_429_is_retried_too(slept):
    import httpx

    admin, seen = scripted_admin([httpx.Response(400, json=LIMITED), ok()])
    assert admin.request("GET", "users")["id"] == 7 and len(seen) == 2


def test_no_calls_left_pauses_until_the_reset(slept, capsys):
    admin, seen = scripted_admin(
        [ok(**{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1009"}), ok()]
    )
    assert admin.request("GET", "users")["id"] == 7
    assert slept == [10.0] and len(seen) == 1  # paused before returning, nothing retried
    assert "zulip: rate limit reached, pausing 10 s" in capsys.readouterr().err
    admin.request("GET", "users")  # calls left (no header): no pause
    assert slept == [10.0]


def test_other_errors_are_not_retried(slept):
    import httpx

    admin, seen = scripted_admin(
        [httpx.Response(400, json={"result": "error", "code": "BAD_REQUEST", "msg": "nope"})]
    )
    with pytest.raises(zp.ZulipAdminError) as exc:
        admin.request("POST", "bots", full_name="x")
    assert exc.value.code == "BAD_REQUEST" and len(seen) == 1 and slept == []
    admin, seen = scripted_admin([httpx.Response(502, text="<html>bad gateway</html>")])
    with pytest.raises(zp.ZulipAdminError) as exc:
        admin.request("GET", "users")
    assert exc.value.code == "INVALID_RESPONSE" and len(seen) == 1 and slept == []


# ------------------------------------ --no-rebind and --repo-owner (docs/ADMIN.md §2.4, #312)

#: alice's e-mail now belongs to user 105, bob's to nobody; carol keeps a stale 999; dave has no
#: id; eve has none either, and her e-mail is alice's old one (user 101, which alice keeps).
REBIND_ROSTER = """github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,Alice,Smith,impostor@example.org,alice/thesis,1,,,101,
bob,Bob,Jones,nobody@example.org,sandbox,2,,,102,
carol,Carol,Lee,carol@example.org,carol/x,3,,,999,
dave,Dave,Kim,dave@example.org,dave/x,4,,,,
eve,Eve,Adams,alice@example.org,eve/x,5,,,,
"""
ADMIN_RC = "[api]\nemail=drew@org\nkey=" + "Z" * 32 + "\nsite=https://org.zulipchat.com\n"


@pytest.fixture
def rebind_roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(REBIND_ROSTER)
    return path


def test_no_rebind_keeps_every_recorded_id_and_touches_nothing_for_those_rows(
    tmp_path, rebind_roster
):
    rows = rows_of(rebind_roster)
    for row in rows[:3]:
        row["zulip_invite"] = "joined"
    rows[0]["zulip_channel_id"] = "31"  # alice-sandbox
    before = [dict(r) for r in rows]
    admin = FakeAdmin()
    out = tmp_path / "out"
    reports = zp.provision(
        rows, admin, bot_client=admin.bot_client, out_dir=out, dry_run=False, regenerate=False,
        no_rebind=True,
    )  # fmt: skip
    by = {r.slug: r for r in reports}
    refused = "binding changed: refused (zulip_user_id {} kept; the roster e-mail now matches {})"
    assert by["alice"].notes[0].startswith(refused.format(101, "user 105"))
    assert by["carol"].notes[0].startswith(refused.format(999, "no single user"))
    assert by["eve"].notes == ["Zulip user 101 is still recorded for alice: not bound"]
    for slug in ("alice", "carol", "eve"):
        assert by[slug].blocked and not by[slug].ok and not by[slug].bots
    assert (by["alice"].user_id, by["alice"].matched) == (101, "kept")
    assert not any("changed from" in w for w in by["alice"].warnings)
    # bob's e-mail binds nobody, and his recorded 102 is an active human: linked by that ID
    # (#627), as a row roster_sync.py made is, whose e-mail may be Zulip's placeholder
    assert (by["bob"].user_id, by["bob"].matched, by["bob"].blocked) == (102, "zulip id", False)
    assert by["bob"].bots and not by["bob"].notes
    # the refused rows keep every column; dave and bob get bots and a channel, and no invitation
    assert rows[0] == before[0] and rows[2] == before[2] and rows[4] == before[4]
    assert rows[1]["zulip_user_id"] == "102"
    writes = admin.writes()
    assert writes and all("dave" in repr(c) or "bob" in repr(c) for c in writes)
    assert sorted(p.name for p in out.iterdir()) == ["bob", "dave"]  # none for a refused row
    assert by["dave"].invite == "not on Zulip" and rows[3]["zulip_invite"] == ""


def test_without_no_rebind_the_ids_follow_the_e_mail_as_before(rebind_roster):
    rows = rows_of(rebind_roster)
    reports = zp.match_rows(rows, FakeAdmin().users(), 7)
    assert [r["zulip_user_id"] for r in rows] == ["105", "", "", "", "101"]
    assert "zulip_user_id changed from 101 to 105" in reports[0].warnings


def test_no_rebind_binds_a_row_without_an_id_and_keeps_an_unchanged_one(rebind_roster):
    rows = rows_of(rebind_roster)
    rows[0]["email"] = "alice@example.org"  # alice's e-mail still binds her recorded 101
    rows[1]["zulip_user_id"] = ""  # bob: no id, and his e-mail matches nobody
    rows[4]["email"] = "bob@example.org"  # eve: no id, her e-mail binds 102, which no row keeps
    reports = zp.match_rows(rows, FakeAdmin().users(), 7, no_rebind=True)
    assert [r.notes for r in reports[:2]] == [[], []] and reports[4].notes == []
    assert [r["zulip_user_id"] for r in rows] == ["101", "", "999", "", "102"]
    assert reports[2].blocked and reports[2].notes  # carol's stale 999 is kept, and reported


def test_no_rebind_keeps_the_id_of_every_refused_row():
    """Refused rows are skipped by position: two rows recording one id (written `0101` and
    `101`) both keep what they have."""
    rows = [
        {"slug": s, "email": f"{s}@example.net", "github": "", "zulip_user_id": uid}
        for s, uid in (("ann", "0101"), ("ben", "101"))
    ]
    reports = zp.match_rows(rows, FakeAdmin().users(), 7, no_rebind=True)
    assert [r["zulip_user_id"] for r in rows] == ["0101", "101"]
    assert all(r.blocked and r.user_id == 101 for r in reports)


def test_a_recorded_id_is_normalized_so_0101_reserves_101(tmp_path, monkeypatch, capsys):
    """Reviewer on #320 (3): `0101` is user 101, on a selected row and on one --only leaves
    out; and a roster with both `0101` and `101` is a duplicate."""
    assert zp.recorded_id({"zulip_user_id": " 0101 "}) == "101"
    assert zp.recorded_id({"zulip_user_id": ""}) == ""
    rows = [
        {"slug": "ann", "email": "gone@example.net", "github": "", "zulip_user_id": "0101"},
        {"slug": "ben", "email": "alice@example.org", "github": "", "zulip_user_id": ""},
    ]
    reports = zp.match_rows(rows, FakeAdmin().users(), 7, no_rebind=True)
    assert reports[1].notes == ["Zulip user 101 is still recorded for ann: not bound"]
    assert [r["zulip_user_id"] for r in rows] == ["0101", ""]
    reports = zp.match_rows(rows[1:], FakeAdmin().users(), 7, reserved={"101": "ann"})
    assert reports[0].blocked and rows[1]["zulip_user_id"] == ""
    roster = tmp_path / "roster.csv"
    roster.write_text(REBIND_ROSTER.replace(",,,,\n", ",,,0101,\n", 1))  # dave records 0101
    with pytest.raises(ValueError, match="duplicate zulip_user_id '0101'"):
        zp.load_roster(str(roster))


@pytest.mark.parametrize("bad", ["x7", "²", "-1", "1e3", "1" * 19])
def test_an_id_that_is_not_ascii_digits_refuses_the_run(
    tmp_path, rebind_roster, monkeypatch, capsys, bad
):
    """Reviewer on #320 (3): a hand-edited id is refused before any request, even on a row
    --only leaves out, since it would reserve nothing reliably."""
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rebind_roster.write_text(REBIND_ROSTER.replace(",,,999,", f",,,{bad},"))  # carol
    rc = tmp_path / "admin.zuliprc"
    rc.write_text(ADMIN_RC)
    argv = ["--roster", str(rebind_roster), "--zuliprc", str(rc), "--only", "dave"]
    assert zp.main([*argv, "--no-rebind"]) == 1
    assert "roster: invalid zulip_user_id" in capsys.readouterr().err and not admin.calls
    with pytest.raises(ValueError, match="ASCII digits only"):
        zp.match_rows(rows_of(rebind_roster), admin.users(), 7)


def test_main_no_rebind_reports_the_row_and_fails(tmp_path, rebind_roster, monkeypatch, capsys):
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rc = tmp_path / "admin.zuliprc"
    rc.write_text(ADMIN_RC)
    argv = ["--roster", str(rebind_roster), "--zuliprc", str(rc), "--out-dir", str(tmp_path / "o")]
    assert zp.main([*argv, "--no-rebind", "--only", "alice", "--dry-run"]) == 1
    out = capsys.readouterr()
    assert "binding changed: refused" in out.out and "need attention" in out.err
    assert zp.main([*argv, "--no-rebind", "--only", "alice"]) == 1
    assert not admin.writes() and rows_of(rebind_roster)[0]["zulip_user_id"] == "101"


@pytest.mark.parametrize("no_rebind", [True, False])
def test_only_never_binds_a_user_that_an_unselected_row_records(
    tmp_path, rebind_roster, monkeypatch, capsys, no_rebind
):
    """Reviewer on #320: the admin bot always passes --only. eve (no id, alice's old e-mail)
    must not take user 101, which alice, not selected, still records: one user, one row (the
    next load_roster would refuse the duplicate). Without --no-rebind too: alice keeps 101."""
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    rc = tmp_path / "admin.zuliprc"
    rc.write_text(ADMIN_RC)
    argv = ["--roster", str(rebind_roster), "--zuliprc", str(rc), "--out-dir", str(tmp_path / "o")]
    argv += ["--only", "eve", *(["--no-rebind"] if no_rebind else [])]
    assert zp.main(argv) == 1
    out = capsys.readouterr().out
    assert "Zulip user 101 is still recorded for alice: not bound" in out
    assert not admin.writes()
    rows = {r["github"]: r for r in rows_of(rebind_roster)}
    assert rows["eve"]["zulip_user_id"] == "" and rows["alice"]["zulip_user_id"] == "101"
    assert zp.load_roster(str(rebind_roster))  # still loads: no duplicate id


def test_repo_owner_names_the_repository_in_start_here(tmp_path, roster, monkeypatch):
    assert zp.qualified_repo({"slug": "ada", "repo": ""}, "Harvard-Agents") == "Harvard-Agents/ada"
    assert zp.qualified_repo({"slug": "ada", "repo": "x/thesis"}, "Harvard-Agents") == "x/thesis"
    admin = FakeAdmin()
    monkeypatch.setattr(zp, "ZulipAdmin", lambda *a, **k: admin)
    roster.write_text(ROSTER.replace("dave/x", "notes"))  # a repository without an owner
    rc = tmp_path / "admin.zuliprc"
    rc.write_text(ADMIN_RC)
    argv = ["--roster", str(roster), "--zuliprc", str(rc), "--out-dir", str(tmp_path / "o")]
    assert zp.main([*argv, "--only", "dave", "--repo-owner", "Harvard-Agents"]) == 0
    posts = [text for posts in admin.bot_posts.values() for _, _, text in posts]
    assert len(posts) == 1 and "`Harvard-Agents/notes`" in posts[0]
    with pytest.raises(SystemExit):
        zp.main([*argv, "--repo-owner", "a b; rm"])  # never reaches a message unchecked
