"""`workshop/admin/provision_repos.py` (workshop/admin/README.md §1b) against a fake GitHub and a
fake SSH runner; the template push also runs for real, into a local bare repository."""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import shutil
import subprocess
import urllib.error

import admin_common as ac
import provision_repos as pr
import pytest

TOKEN = "ghp_" + "T" * 36  # a decoy in the token's format: must never reach argv or output
ROSTER = """slug,github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,AliceGH,Alice,Smith,alice@example.org,,,ws-alice,203.0.113.5,101,
bob,,Bob,Jones,bob@example.org,,,ws-bob,203.0.113.7,102,
"""
ALICE, BOB = "203.0.113.5", "203.0.113.7"
PUB = {
    ALICE: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAliceKeyAliceKeyAliceKey zab-ws-alice-alice",
    BOB: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBobKeyBobKeyBobKeyBobK zab-ws-bob-bob",
}


def owner(slug: str, *, hostname: str | None = None, spare: str = "", key: str = "slug") -> str:
    record = {"spare": spare} if spare else {key: slug, "repo": ""}
    host = hostname if hostname is not None else f"ws-{slug or spare}"
    return f"{json.dumps(record)}\n\n{pr.HOST_MARK}\n{host}\n"


class FakeGitHub:
    def __init__(
        self, *, repos=None, users=None, collaborators=None, invites=None, org_members=(), fail=None
    ):
        self.repos = repos if repos is not None else {}  # name -> {"empty": bool, "keys": [..]}
        # the accounts that exist: casefolded login -> GitHub's own capitals
        self.users = users if users is not None else {"alicegh": "AliceGH", "bobgh": "BobGH"}
        self.collaborators = collaborators or {}  # repo -> {casefolded login: permission}
        self.invites = invites or {}  # repo -> [{"id", "invitee": {"login"}, "expired"}]
        self.org_members = {m.casefold() for m in org_members}  # PUT adds these directly
        self.fail = fail or {}
        self.writes: list[tuple] = []

    def repo(self, org, name):
        assert org == "MathAgents"
        if name not in self.repos:
            return None
        return {"name": name, "private": self.repos[name].get("private", True)}

    def create_repo(self, org, name):
        if "create" in self.fail:
            raise pr.GitHubError(
                "POST", f"/orgs/{org}/repos", 403, {"message": self.fail["create"]}
            )
        self.writes.append(("create", name))
        self.repos[name] = {"empty": True, "keys": []}

    def repo_empty(self, org, name):
        return self.repos[name]["empty"]

    def deploy_keys(self, org, name):
        # a key is "type base64" (write access) or ("type base64", "ro") for a read-only one
        return [
            {"id": i, "key": k if isinstance(k, str) else k[0], "read_only": not isinstance(k, str)}
            for i, k in enumerate(self.repos[name]["keys"])
        ]

    def delete_deploy_key(self, org, name, key_id):
        self.writes.append(("delete-key", name, key_id))
        del self.repos[name]["keys"][key_id]

    def add_deploy_key(self, org, name, title, key):
        self.writes.append(("key", name, title, key))
        self.repos[name]["keys"].append(" ".join(key.split()[:2]))

    def user(self, login):
        if "user" in self.fail:
            raise pr.GitHubError("GET", f"/users/{login}", 502, {"message": self.fail["user"]})
        if "user-transport" in self.fail:  # a failure before GitHub answers (#799)
            raise urllib.error.URLError(self.fail["user-transport"])
        canonical = self.users.get(login.casefold())
        return {"login": canonical, "id": 1000 + len(canonical)} if canonical else None

    def permission(self, org, name, login):
        return self.collaborators.get(name, {}).get(login.casefold(), "none")

    def invitations(self, org, name):
        return list(self.invites.get(name, []))

    def add_collaborator(self, org, name, login):
        """As GitHub's PUT with `admin` (#714): an org member, or a collaborator already, gets
        admin at once (204); anyone else an invitation (201)."""
        self.writes.append(("invite", name, login))
        if login.casefold() in self.org_members or self.permission(org, name, login) != "none":
            self.collaborators.setdefault(name, {})[login.casefold()] = "admin"
            return False
        number = 100 + len(self.writes)
        invitation = {"id": number, "invitee": {"login": login}, "permissions": "admin"}
        self.invites.setdefault(name, []).append(invitation)
        return True

    def raise_invitation(self, org, name, invitation_id):
        self.writes.append(("raise", name, invitation_id))
        for invitation in self.invites.get(name, []):
            if invitation["id"] == invitation_id:
                invitation["permissions"] = "admin"

    def delete_invitation(self, org, name, invitation_id):
        self.writes.append(("withdraw", name, invitation_id))
        self.invites[name] = [i for i in self.invites.get(name, []) if i["id"] != invitation_id]

    def remove_collaborator(self, org, name, login):
        self.writes.append(("remove", name, login))
        self.collaborators.get(name, {}).pop(login.casefold(), None)


class FakeZulip:
    """`GET /realm/profile_fields` and `GET /users` (with custom profile fields) as Drew."""

    def __init__(self, fields=None, *, field_name="GitHub", emails=None):
        self.fields = {"101": "AliceGH", "102": ""} if fields is None else fields
        self.field_name = field_name
        self.emails = emails or {}  # user id -> delivery e-mail
        self.closed = False

    def profile_fields(self):
        return [{"id": 3, "name": "Pronouns"}, {"id": 7, "name": self.field_name}]

    def users(self):
        users = [{"user_id": 900, "is_bot": True, "profile_data": {"7": {"value": "botgh"}}}]
        for uid, value in self.fields.items():
            data = {"7": {"value": value}} if value else {}
            user = {"user_id": int(uid), "is_bot": False, "profile_data": data}
            if uid in self.emails:
                user["delivery_email"] = self.emails[uid]
            users.append(user)
        return users

    def close(self):
        self.closed = True


class FakeRunner:
    """Answers the owner check, the key generation and `zab setup-repo`; local `git` succeeds
    and marks the repository non-empty in the fake GitHub."""

    def __init__(self, gh: FakeGitHub, *, owners=None, setup_fail=None):
        self.gh = gh
        self.owners = owners or {}
        self.setup_fail = setup_fail or set()
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append(list(argv))
        self.envs.append(dict(env) if env is not None else None)
        if argv[0] == "git":
            if "push" in argv:
                self.gh.repos[argv[-2].rsplit("/", 1)[-1].removesuffix(".git")]["empty"] = False
            return ac.RunResult(0, "", "")
        if argv[0] == "gh":
            return ac.RunResult(1, "", "not logged in")
        host = argv[-3].split("@", 1)[1]
        remote = argv[-1]
        if remote == pr.OWNER_CMD:
            slug = {ALICE: "alice", BOB: "bob"}[host]
            return ac.RunResult(0, self.owners.get(host, owner(slug)), "")
        if "ssh-keygen" in remote:
            return ac.RunResult(0, PUB[host] + "\n", "")
        if "setup-repo" in remote:
            if host in self.setup_fail:
                return ac.RunResult(1, "write access FAILED: push rejected (denied)\n", "")
            return ac.RunResult(
                0,
                "key: …\ncloned: /home/agent/work/x\n"
                "write access OK: pushed and deleted zab-preflight-ws\n",
                "",
            )
        return ac.RunResult(0, "", "")


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    path.chmod(0o600)
    return path


def run_main(roster, gh, runner, *extra, zulip=None):
    zulip = zulip if zulip is not None else FakeZulip()
    return pr.main(
        ["--roster", str(roster), "--known-hosts", "/tmp/kh", "--zuliprc", "z.rc", *extra],  # noqa: S108
        runner=runner,
        make_github=lambda token: gh,
        make_zulip=lambda path: zulip,
        env={"GH_TOKEN": TOKEN},
    )


def rows_of(path) -> dict[str, dict[str, str]]:
    return {r["slug"]: r for r in csv.DictReader(io.StringIO(pathlib.Path(path).read_text()))}


def test_new_participants_get_a_seeded_repo_a_deploy_key_and_a_checkout(roster, capsys):
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner) == 0
    assert ("create", "alice") in gh.writes and ("create", "bob") in gh.writes
    keys = [w for w in gh.writes if w[0] == "key"]
    assert {(w[1], w[2]) for w in keys} == {("alice", "zab ws-alice"), ("bob", "zab ws-bob")}
    assert ("invite", "alice", "AliceGH") in gh.writes  # bob has no GitHub login in Zulip
    assert not any(w[0] == "invite" and w[1] == "bob" for w in gh.writes)
    setup = [c[-1] for c in runner.calls if c[0] == "ssh" and "setup-repo" in c[-1]]
    assert len(setup) == 2 and all("MathAgents/" in s and "--yes" in s for s in setup)
    assert all(c[0] != "ssh" or "StrictHostKeyChecking=yes" in c for c in runner.calls)
    rows = rows_of(roster)
    assert rows["alice"]["repo"] == "MathAgents/alice" and rows["alice"]["repo_status"] == "ok"
    assert rows["alice"]["github_invite"] == "invited" and rows["alice"]["github"] == "AliceGH"
    assert rows["bob"]["github_invite"] == "no login in Zulip" and rows["bob"]["github"] == ""
    assert pathlib.Path(str(roster) + ".bak").exists()
    out = capsys.readouterr()
    assert "2 of 2 ready" in out.out


def test_the_token_never_reaches_an_argument_a_vm_or_the_output(roster, capsys):
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    run_main(roster, gh, runner)
    assert not any(TOKEN in arg for call in runner.calls for arg in call)
    pushes = [i for i, c in enumerate(runner.calls) if c[0] == "git" and "push" in c]
    assert pushes and all(TOKEN not in runner.calls[i][-2] for i in pushes)  # not in the URL
    for i in pushes:  # only git's environment carries it, as an HTTP header
        env = runner.envs[i]
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_VALUE_1"] == ""
    ssh_envs = [runner.envs[i] for i, c in enumerate(runner.calls) if c[0] == "ssh"]
    assert all(e is None or TOKEN not in json.dumps(e) for e in ssh_envs)
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and TOKEN not in pathlib.Path(roster).read_text()


def test_a_rerun_changes_nothing_but_still_verifies(roster):
    gh = FakeGitHub(
        repos={n: {"empty": False, "keys": [" ".join(PUB[ip].split()[:2])]}
               for n, ip in (("alice", ALICE), ("bob", BOB))},
        collaborators={"alice": {"alicegh": "admin"}},
    )  # fmt: skip
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner) == 0
    assert gh.writes == []
    assert not any(c[0] == "git" for c in runner.calls)  # no template push into a full repo
    assert sum("setup-repo" in c[-1] for c in runner.calls if c[0] == "ssh") == 2
    assert rows_of(roster)["alice"]["github_invite"] == "collaborator"


def test_a_read_only_deploy_key_is_replaced_with_a_write_key(roster):
    """codex on #101: a matching key without write access would fail every push test."""
    ro = (" ".join(PUB[ALICE].split()[:2]), "ro")
    gh = FakeGitHub(
        repos={"alice": {"empty": False, "keys": [ro]}},
        collaborators={"alice": {"alicegh": "admin"}},
    )
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice") == 0
    assert [w[0] for w in gh.writes] == ["delete-key", "key"]
    assert gh.deploy_keys("MathAgents", "alice") == [
        {"id": 0, "key": " ".join(PUB[ALICE].split()[:2]), "read_only": False}
    ]


def test_a_pending_invitation_is_not_repeated(roster):
    invitation = {"id": 5, "invitee": {"login": "ALICEGH"}, "permissions": "admin"}
    gh = FakeGitHub(invites={"alice": [invitation]})
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    assert not any(w[0] in ("invite", "raise") for w in gh.writes)
    assert rows_of(roster)["alice"]["github_invite"] == "pending"


# ------------------------------------------------------------------------ admin (#714)


def test_a_new_invitation_is_for_admin(roster):
    gh = FakeGitHub()
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    assert ("invite", "alice", "AliceGH") in gh.writes
    assert [i["permissions"] for i in gh.invites["alice"]] == ["admin"]
    assert rows_of(roster)["alice"]["github_invite"] == "invited"


@pytest.mark.parametrize("permission", ["write", "maintain", "triage", "read"])
def test_a_collaborator_with_less_than_admin_is_raised(roster, permission):
    gh = FakeGitHub(collaborators={"alice": {"alicegh": permission}})
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice") == 0
    assert ("invite", "alice", "AliceGH") in gh.writes  # the PUT with admin: 204, raised
    assert gh.collaborators["alice"] == {"alicegh": "admin"} and not gh.invites.get("alice")
    assert invite_state(roster) == ("AliceGH", "collaborator; raised to admin")


def test_an_admin_is_done_and_an_org_owner_counts(roster):
    """Only admin is done; an org owner's permission reads `admin` (GitHub's permission API)."""
    gh = FakeGitHub(collaborators={"alice": {"alicegh": "admin"}})
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice") == 0
    assert not any(w[0] in ("invite", "raise") for w in gh.writes)
    assert invite_state(roster) == ("AliceGH", "collaborator")


def test_a_pending_invitation_with_less_than_admin_is_raised(roster):
    invitation = {"id": 5, "invitee": {"login": "ALICEGH"}, "permissions": "write"}
    gh = FakeGitHub(invites={"alice": [invitation]})
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice") == 0
    assert ("raise", "alice", 5) in gh.writes and not any(w[0] == "invite" for w in gh.writes)
    assert invitation["permissions"] == "admin"
    assert invite_state(roster) == ("AliceGH", "pending; raised to admin")


def test_invites_only_raises_both_shows_them_in_its_dry_run_and_then_counts_them_done(
    roster, capsys
):
    gh = FakeGitHub()
    provisioned(roster, gh, zulip=FakeZulip({"101": "AliceGH", "102": "bobgh"}))
    capsys.readouterr()
    # what an earlier release left: alice accepted, with push (counted as accepted here), and
    # bob's invitation still open, for push
    gh.collaborators["alice"] = {"alicegh": "write"}
    gh.invites["alice"] = []
    for invitation in gh.invites["bob"]:
        invitation["permissions"] = "write"
    zulip = FakeZulip({"101": "AliceGH", "102": "bobgh"})
    before = pathlib.Path(roster).read_text()
    gh.writes.clear()
    code, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=zulip)
    assert code == 0 and gh.writes == [] and pathlib.Path(roster).read_text() == before
    assert "collaborator; would raise to admin" in out and "pending; would raise to admin" in out
    assert out.splitlines()[-1] == (
        "invites: 0 invited, 2 raised to admin, 1 accepted, 0 removed, 0 failed; 0 unchanged "
        "(dry run)"
    )
    code, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0
    assert gh.writes == [
        ("invite", "alice", "AliceGH"),
        ("raise", "bob", gh.invites["bob"][0]["id"]),
    ]
    assert out.splitlines()[-1] == (
        "invites: 0 invited, 2 raised to admin, 1 accepted, 0 removed, 0 failed; 0 unchanged"
    )
    assert invite_state(roster) == ("AliceGH", "collaborator; raised to admin")
    assert invite_state(roster, "bob") == ("BobGH", "pending; raised to admin")
    # the next run: both done, nothing changed, nothing counted as accepted
    gh.writes.clear()
    code, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0 and gh.writes == []
    assert out == "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 2 unchanged\n"


@pytest.mark.parametrize(
    "answer,why",
    [
        (owner("bob", hostname="ws-alice"), "belongs to 'bob'"),
        (owner("alice", hostname="ws-bob"), "hostname is 'ws-bob'"),
        (owner("", spare="zab-spare-1"), "spare"),
        ("not json\n\n" + pr.HOST_MARK + "\nws-alice\n", "not JSON"),
    ],
)
def test_a_vm_that_is_not_the_participants_gets_nothing(roster, answer, why, capsys):
    gh = FakeGitHub()
    runner = FakeRunner(gh, owners={ALICE: answer})
    assert run_main(roster, gh, runner) == 1
    assert ("create", "alice") not in gh.writes and ("create", "bob") in gh.writes
    assert not any(ALICE in " ".join(c) and "ssh-keygen" in c[-1] for c in runner.calls)
    assert why in rows_of(roster)["alice"]["repo_status"]
    assert rows_of(roster)["bob"]["repo_status"] == "ok"  # the other row is unaffected


def test_vm_identity_follows_what_launch_recorded():
    """codex on #96: launch records `github` and names the VM ws-<github> until it moves to
    slugs; a record with `slug` is matched on the slug."""
    row = {"slug": "alice", "github": "AliceGH"}
    assert pr.owner_problem(owner("alice"), row) is None
    assert pr.owner_problem(owner("ALICE"), row) is None
    assert pr.owner_problem(owner("AliceGH", key="github"), row) is None  # the old launch
    assert "belongs to" in pr.owner_problem(owner("bob", key="github"), row)
    assert "belongs to" in pr.owner_problem(
        owner("AliceGH"), row
    )  # a slug record must match the slug
    no_slug = {"slug": "", "github": "carol"}
    assert pr.identity(no_slug) == "carol"
    assert pr.owner_problem(owner("carol", key="github"), no_slug) is None


def test_a_roster_without_slugs_uses_the_github_login(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text(
        "github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes\n"
        "alice,Alice,Smith,alice@example.org,,1,ws-alice,203.0.113.5,101,\n"
    )
    gh = FakeGitHub()
    runner = FakeRunner(gh, owners={ALICE: owner("alice", key="github")})
    assert run_main(path, gh, runner) == 0
    assert ("create", "alice") in gh.writes
    # the slug is written back, so the login from Zulip can replace the old identity login
    row = rows_of(path)["alice"]
    assert row["repo"] == "MathAgents/alice" and row["github"] == "AliceGH"


def test_an_existing_public_repository_is_refused(roster):
    gh = FakeGitHub(repos={"alice": {"empty": True, "keys": [], "private": False}})
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner) == 1
    assert "not private" in rows_of(roster)["alice"]["repo_status"]
    assert not any(w[1] == "alice" for w in gh.writes if w[0] == "key")
    assert not any(c[0] == "git" and "alice.git" in c[-2] for c in runner.calls if len(c) > 2)


def test_a_failed_push_test_fails_the_row(roster):
    gh = FakeGitHub()
    assert run_main(roster, gh, FakeRunner(gh, setup_fail={BOB})) == 1
    assert "setup-repo" in rows_of(roster)["bob"]["repo_status"]
    assert rows_of(roster)["alice"]["repo_status"] == "ok"


def test_a_github_error_is_reported_per_row_without_the_token(roster, capsys):
    gh = FakeGitHub(fail={"create": f"Bad credentials {TOKEN}"})
    assert run_main(roster, gh, FakeRunner(gh)) == 1
    status = rows_of(roster)["alice"]["repo_status"]
    assert "HTTP 403" in status and TOKEN not in status and "[redacted]" in status


def test_dry_run_writes_nothing_anywhere(roster, capsys):
    before = pathlib.Path(roster).read_text()
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner, "--dry-run") == 0
    assert gh.writes == []
    assert all(c[0] == "ssh" and c[-1] == pr.OWNER_CMD for c in runner.calls)  # read-only
    assert pathlib.Path(roster).read_text() == before
    assert "would create" in capsys.readouterr().out


def test_only_and_slug_validation(roster, tmp_path, capsys):
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner, "--only", "BOB") == 0
    assert [w[1] for w in gh.writes if w[0] == "create"] == ["bob"]
    assert run_main(roster, gh, runner, "--only", "carol") == 2
    assert "not in the roster: carol" in capsys.readouterr().err
    bad = tmp_path / "bad.csv"
    bad.write_text(ROSTER.replace("bob,,Bob", "bob;rm,,Bob"))
    other = FakeGitHub()
    # the roster reader (#97) refuses an invalid slug anywhere before any remote call
    capsys.readouterr()
    assert run_main(bad, other, FakeRunner(other), "--only", "alice") == 2
    assert "slug" in capsys.readouterr().err and other.writes == []
    with pytest.raises(ValueError):
        pr.keygen_command("bob;rm")


def test_token_sources(monkeypatch):
    assert pr.github_token({"GH_TOKEN": " a "}) == "a"
    assert pr.github_token({"GITHUB_TOKEN": "b"}) == "b"
    gh_cli = lambda argv, **kw: ac.RunResult(0, "c\n", "")  # noqa: E731
    assert pr.github_token({}, gh_cli) == "c"
    with pytest.raises(ValueError, match="GH_TOKEN"):
        pr.github_token({}, lambda argv, **kw: ac.RunResult(1, "", "no"))


class FakeResponse(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(json.dumps(payload).encode() if payload is not None else b"")
        self.status = status


def test_a_new_repository_is_private_and_its_description_names_no_event():
    """#692's review: every workshop's `!provision repos` creates its repositories with this
    body, so the About line, like the template, names no event, audience or date."""
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return FakeResponse(201, {"id": 1})

    pr.GitHub(TOKEN, urlopen=urlopen).create_repo("MathAgents", "alice")
    (req,) = seen
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.github.com/orgs/MathAgents/repos"
    body = json.loads(req.data)
    assert body == {
        "name": "alice",
        "private": True,
        "description": "Workshop repository",
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,
    }
    assert not re.search(r"\b20\d\d\b|number theorists|Oct", body["description"])


def test_the_http_layer_sends_the_token_only_as_a_bearer_header():
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        if req.full_url.endswith("/commits?per_page=1"):
            raise urllib.error.HTTPError(
                req.full_url,
                409,
                "Conflict",
                {},
                io.BytesIO(b'{"message":"Git Repository is empty."}'),
            )
        if "/repos/MathAgents/ghost" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if req.full_url.endswith("/users/nobody"):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        if req.full_url.endswith("/collaborators/x/permission"):
            return FakeResponse(200, {"permission": "write", "user": {"login": "X"}})
        if req.method == "PUT":
            assert json.loads(req.data) == {"permission": "admin"}  # #714
            return FakeResponse(201, {"id": 9})
        if req.method == "PATCH":
            assert json.loads(req.data) == {"permissions": "admin"}
            return FakeResponse(200, {"id": 9, "permissions": "admin"})
        if req.method == "DELETE":
            return FakeResponse(204, None)
        return FakeResponse(200, {"id": 7, "login": "X"})

    gh = pr.GitHub(TOKEN, urlopen=urlopen)
    assert gh.repo("MathAgents", "ghost") is None
    assert gh.repo_empty("MathAgents", "alice") is True
    assert gh.user("x") == {"id": 7, "login": "X"} and gh.user("nobody") is None
    assert gh.permission("MathAgents", "alice", "x") == "write"
    assert gh.add_collaborator("MathAgents", "alice", "x") is True
    gh.raise_invitation("MathAgents", "alice", 9)
    gh.delete_invitation("MathAgents", "alice", 9)
    gh.remove_collaborator("MathAgents", "alice", "x")
    assert [r.get_method() for r in seen[-4:]] == ["PUT", "PATCH", "DELETE", "DELETE"]
    assert seen[-3].full_url.endswith("/repos/MathAgents/alice/invitations/9")
    assert seen[-2].full_url.endswith("/repos/MathAgents/alice/invitations/9")
    assert all(r.get_header("Authorization") == f"Bearer {TOKEN}" for r in seen)
    assert all(
        TOKEN not in r.full_url and r.full_url.startswith("https://api.github.com/") for r in seen
    )
    err = pr.GitHubError("POST", "/orgs/MathAgents/repos", 422, {"message": "name already exists"})
    assert str(err) == "GitHub POST /orgs/MathAgents/repos: HTTP 422 name already exists"


def test_the_template(tmp_path):
    files = {p.name for p in pr.TEMPLATE_DIR.iterdir()}
    assert files >= {"README.md", "AGENTS.md", "CLAUDE.md", "HANDOVER.md", ".gitignore"}
    assert (pr.TEMPLATE_DIR / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    assert ".zab/" in (pr.TEMPLATE_DIR / ".gitignore").read_text().splitlines()
    readme = (pr.TEMPLATE_DIR / "README.md").read_text()
    assert "private to" not in readme and "can read it" in readme  # codex on #96
    # #692: every workshop seeds from it, so it names no event and no date
    assert readme.startswith("# {{repo}}\n\nYour workshop repository. It is not public:\n")
    for path in pr.TEMPLATE_DIR.iterdir():
        assert not re.search(r"\b20\d\d\b|number theorists", path.read_text()), path.name
    agents = (pr.TEMPLATE_DIR / "AGENTS.md").read_text()
    # #692: Sage and Magma are optional image components; `!tools` lists what the VM has
    assert "installed too" not in agents
    assert "Sage and Magma are available too, where the image has them (`!tools`" in agents
    for needle in (
        "numerically",
        "PARI/GP",
        "cypari2",
        "parameters",
        "small and often",
        "HANDOVER.md",
        # issue #36, Mon 18:15 and 20:15: worktrees per topic, shared by its two agents; merge
        # main in the worktree first, then fast-forward the main checkout
        "## Topics, worktrees and branches",
        "zab/<topic>-<6 hex>",
        "Both agents of a topic share it",
        "git -C <main checkout> merge --ff-only",
        "git -C <main checkout> push origin main",
        "git merge main",
        ".zab/worktrees/",
        "never edit them",
        "Gitignored files",
    ):
        assert needle in agents
    # the HTML-page advice moved to the VM-level agent instructions (PM on #36, Mon 19:00)
    assert "self-contained HTML" not in agents
    section = agents.split("## Topics, worktrees and branches", 1)[1]
    assert len(section.strip().splitlines()) <= 14  # short: participants read it too
    assert "own branch" in readme


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_seeding_pushes_one_commit_with_the_template_into_a_real_repository(tmp_path):
    remote = tmp_path / "remote" / "MathAgents" / "alice.git"
    remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)  # noqa: S603, S607
    pr.seed_repo(
        "MathAgents",
        "alice",
        TOKEN,
        ac.subprocess_runner,
        remote_base=f"file://{tmp_path / 'remote'}",
    )
    log = git_out(remote, "log", "--format=%an|%s", "main").split("\n")
    assert log[0] == "MathAgents workshop|Workshop template" and log[1] == ""
    tree = git_out(remote, "ls-tree", "--name-only", "main").split()
    assert set(tree) >= {".gitignore", "AGENTS.md", "CLAUDE.md", "HANDOVER.md", "README.md"}
    readme = git_out(remote, "show", "main:README.md")
    assert readme.startswith("# MathAgents/alice") and "~/work/alice" in readme


def git_out(git_dir, *args: str) -> str:
    cmd = ["git", "--git-dir", str(git_dir), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # noqa: S603


@pytest.mark.parametrize(
    "second",
    [
        "carol,CarolGH,Carol,Roe,carol@example.org,,,,203.0.113.9,103,",  # explicit slug
        ",ALICE,Al,Ice,al@example.org,,,,203.0.113.9,103,",  # fallback github login
    ],
)
def test_duplicate_repository_names_refuse_the_whole_roster(tmp_path, second, capsys):
    """codex on #96: two rows with one name would give two VMs write access to one repo."""
    text = (
        ROSTER.rstrip("\n").replace("carol", "alice")
        + "\n"
        + second.replace("carol", "alice", 1)
        + "\n"
    )
    path = tmp_path / "dup.csv"
    path.write_text(text)
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(path, gh, runner, "--only", "bob") == 2
    # refused by the roster reader (#97: "duplicate slug") before provision_repos' own check
    assert "duplicate" in capsys.readouterr().err
    assert gh.writes == [] and runner.calls == []


def test_a_vm_set_up_for_another_github_login_is_refused():
    """codex round 3 on #96: the first boot imported the recorded login's SSH keys, so a VM
    whose login the roster has since corrected (or removed) must not get write access."""

    def record(github: str) -> str:
        data = {"slug": "alice", "github": github, "repo": ""}
        return f"{json.dumps(data)}\n\n{pr.HOST_MARK}\nws-alice\n"

    corrected = {"slug": "alice", "github": "CorrectLogin"}
    why = pr.owner_problem(record("WrongLogin"), corrected)
    assert why and "WrongLogin" in why and "CorrectLogin" in why
    removed = {"slug": "alice", "github": ""}
    assert "WrongLogin" in (pr.owner_problem(record("WrongLogin"), removed) or "")
    assert pr.owner_problem(record("correctlogin"), corrected) is None  # case only
    assert pr.owner_problem(record(""), removed) is None  # launched without a login


def test_a_row_whose_repo_is_someone_elses_is_left_alone(tmp_path, capsys):
    """PM follow-up on #36: an organizer's row (Drew's `AndrewVSutherland/nt-agents-test`) must
    not get a MathAgents repository, nor a new default_project at the next push."""
    path = tmp_path / "roster.csv"
    old = "alice,AliceGH,Alice,Smith,alice@example.org,,"
    new = "alice,AliceGH,Alice,Smith,alice@example.org,AndrewVSutherland/nt-agents-test,"
    path.write_text(ROSTER.replace(old, new))
    path.chmod(0o600)
    before = rows_of(path)["alice"]
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(path, gh, runner) == 0
    out = capsys.readouterr().out
    assert "skipped: repo 'AndrewVSutherland/nt-agents-test' belongs to another owner" in out
    assert "1 of 2 ready, 1 left alone (alice)" in out
    assert all(w[1] != "alice" for w in gh.writes)  # bob was provisioned, alice untouched
    assert not any(ALICE in " ".join(c) for c in runner.calls)  # no SSH to alice's VM
    after = rows_of(path)["alice"]
    assert {k: after[k] for k in before} == before and not after.get("repo_status")
    # --force provisions it and points the row at the new repository
    assert run_main(path, gh, runner, "--force", "--only", "alice") == 0
    assert rows_of(path)["alice"]["repo"] == "MathAgents/alice"


@pytest.mark.parametrize(
    "repo,foreign",
    [
        ("", False),
        ("MathAgents/alice", False),
        ("mathagents/Alice", False),
        ("alice", False),
        ("git@github.com:MathAgents/alice.git", False),
        ("https://github.com/MathAgents/alice", False),
        ("AndrewVSutherland/nt-agents-test", True),
        ("MathAgents/bob", True),
        ("thesis", True),
    ],
)
def test_foreign_repo(repo, foreign):
    assert bool(pr.foreign_repo(repo, "MathAgents", "alice")) is foreign


# ------------------------------------------------------------------- invitations from Zulip


def invite_state(roster, slug="alice"):
    row = rows_of(roster)[slug]
    return row["github"], row["github_invite"]


@pytest.mark.parametrize(
    "value,login",
    [
        ("alice", "alice"),
        ("@Alice", "Alice"),
        (" https://github.com/Alice-GH/ ", "Alice-GH"),
        ("github.com/alice", "alice"),
        ("http://www.github.com/alice", "alice"),
    ],
)
def test_normalize_login(value, login):
    assert pr.normalize_login(value) == login


def test_the_zulip_field_wins_over_the_roster_and_a_change_removes_the_old_login(roster):
    """PM on #36 (Mon 06:40): the login comes from the Zulip user's GitHub field; when it
    differs from the recorded one, the old login's invitation and access go first."""
    gh = FakeGitHub(
        users={"alicegh": "AliceGH", "oldgh": "OldGH"},
        collaborators={"alice": {"oldgh": "write"}},
        invites={"alice": [{"id": 5, "invitee": {"login": "OldGH"}}]},
    )
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,OldGH,")
    pathlib.Path(roster).write_text(text)
    zulip = FakeZulip({"101": "https://github.com/AliceGH", "102": ""})
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice", zulip=zulip) == 0
    access = [w for w in gh.writes if w[0] in ("withdraw", "remove", "invite")]
    assert access == [
        ("withdraw", "alice", 5),
        ("remove", "alice", "OldGH"),
        ("invite", "alice", "AliceGH"),
    ]
    assert invite_state(roster) == ("AliceGH", "invited; removed OldGH")
    assert zulip.closed
    # a rerun with the same field changes nothing
    gh.writes.clear()
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice", zulip=zulip) == 0
    assert not [w for w in gh.writes if w[0] in ("withdraw", "remove", "invite")]
    assert invite_state(roster) == ("AliceGH", "pending")


def test_an_empty_field_falls_back_to_the_roster_login(roster):
    gh = FakeGitHub()
    assert run_main(roster, gh, FakeRunner(gh), zulip=FakeZulip({"101": "", "102": ""})) == 0
    assert ("invite", "alice", "AliceGH") in gh.writes
    assert invite_state(roster) == ("AliceGH", "invited")
    assert invite_state(roster, "bob") == ("", "no login in Zulip")


@pytest.mark.parametrize(
    "fields,state",
    [
        ({"101": "nobody-here", "102": ""}, "unknown login"),
        ({"101": "a b", "102": ""}, "invalid login 'a b' in Zulip"),
        ({"102": ""}, "Zulip user 101 not found"),
    ],
)
def test_logins_that_cannot_be_invited_are_reported(roster, fields, state):
    gh = FakeGitHub()
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,,")
    pathlib.Path(roster).write_text(text)
    assert run_main(roster, gh, FakeRunner(gh), zulip=FakeZulip(fields)) == 0  # not a failure
    assert not any(w[0] == "invite" for w in gh.writes)
    assert invite_state(roster)[1] == state


def test_a_participant_not_on_zulip_yet(roster):
    text = pathlib.Path(roster).read_text().replace("ws-bob,203.0.113.7,102", "ws-bob,203.0.113.7,")
    pathlib.Path(roster).write_text(text)
    gh = FakeGitHub()
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    assert invite_state(roster, "bob") == ("", "not on Zulip yet")


def test_a_login_claimed_by_two_rows_is_invited_for_neither(roster):
    gh = FakeGitHub()
    zulip = FakeZulip({"101": "AliceGH", "102": "alicegh"})
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert not any(w[0] == "invite" for w in gh.writes)
    # the field is compared without case (GitHub logins are case-insensitive)
    assert rows_of(roster)["alice"]["github_invite"] == "login alicegh also claimed by bob"
    assert rows_of(roster)["bob"]["github_invite"] == "login alicegh also claimed by alice"
    # a row outside --only keeps its recorded login: bob cannot take it
    gh.writes.clear()
    assert run_main(roster, gh, FakeRunner(gh), "--only", "bob", zulip=zulip) == 0
    assert not any(w[0] == "invite" for w in gh.writes)
    assert ac.load_roster(str(roster))  # no duplicate login was written


def test_only_still_sees_the_other_rows_zulip_claims(roster):
    """codex P1 on #142: with --only alice, bob's current Zulip field counts too, even though
    his row is not provisioned and has no recorded login."""
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,,")
    pathlib.Path(roster).write_text(text)
    gh = FakeGitHub(users={"samegh": "SameGH"})
    zulip = FakeZulip({"101": "SameGH", "102": "samegh"})
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice", zulip=zulip) == 0
    assert not any(w[0] == "invite" for w in gh.writes)
    assert invite_state(roster) == ("", "login samegh also claimed by bob")


def test_a_recorded_login_stays_reserved_until_its_row_moves_off_it(roster):
    """codex P2 on #142: alice moves from OldGH to NewGH while bob asks for OldGH. Bob waits
    for a later run, so a failure on alice's side can never leave OldGH on both rows."""
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,OldGH,")
    pathlib.Path(roster).write_text(text)
    gh = FakeGitHub(users={"oldgh": "OldGH", "newgh": "NewGH"})
    zulip = FakeZulip({"101": "NewGH", "102": "OldGH"})
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert invite_state(roster) == ("NewGH", "invited; removed OldGH")
    assert invite_state(roster, "bob") == ("", "login oldgh also claimed by alice")
    assert ac.load_roster(str(roster))
    # the next run gives OldGH to bob
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert invite_state(roster, "bob") == ("OldGH", "invited")


def test_a_failed_row_keeps_its_login_and_nobody_else_takes_it(roster):
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,OldGH,")
    pathlib.Path(roster).write_text(text)
    gh = FakeGitHub(users={"oldgh": "OldGH", "newgh": "NewGH"})
    zulip = FakeZulip({"101": "NewGH", "102": "OldGH"})
    runner = FakeRunner(gh, owners={ALICE: owner("bob", hostname="ws-alice")})
    assert run_main(roster, gh, runner, zulip=zulip) == 1  # alice's owner check fails
    assert rows_of(roster)["alice"]["github"] == "OldGH"
    assert invite_state(roster, "bob") == ("", "login oldgh also claimed by alice")
    assert ac.load_roster(str(roster))  # no duplicate login


def test_an_org_member_is_added_directly_and_an_expired_invitation_is_replaced(roster):
    gh = FakeGitHub(
        users={"alicegh": "AliceGH", "bobgh": "BobGH"},
        collaborators={"alice": {"alicegh": "read"}},  # base permission from an old org invite
        org_members={"alicegh"},
        invites={"bob": [{"id": 8, "invitee": {"login": "BobGH"}, "expired": True}]},
    )
    zulip = FakeZulip({"101": "AliceGH", "102": "BobGH"})
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert invite_state(roster) == ("AliceGH", "collaborator; raised to admin")  # read: raised
    assert ("withdraw", "bob", 8) in gh.writes and ("invite", "bob", "BobGH") in gh.writes
    assert invite_state(roster, "bob") == ("BobGH", "invited")


def test_a_github_error_in_the_invitation_is_reported_not_fatal(roster):
    gh = FakeGitHub(fail={"user": "Server Error"})
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    login, state = invite_state(roster)
    assert login == "AliceGH" and state.startswith("failed: GitHub GET /users/alicegh: HTTP 502")
    assert rows_of(roster)["alice"]["repo_status"] == "ok"


def test_a_transport_failure_in_the_invitation_is_reported_not_fatal(roster):
    """#799: a failure before GitHub answers, such as `URLError` or a timeout, is the row's
    invitation's alone, as GitHub's own errors are: the repository is ready, so the row stays
    `ok` and the run exits 0 (ORGANIZERS §3.4)."""
    gh = FakeGitHub(fail={"user-transport": "timed out"})
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    login, state = invite_state(roster)
    assert login == "AliceGH" and state == "failed: URLError: <urlopen error timed out>"
    assert rows_of(roster)["alice"]["repo_status"] == "ok"


def test_invitations_need_the_zuliprc_unless_no_invite(roster, capsys):
    gh = FakeGitHub()
    argv = ["--roster", str(roster), "--known-hosts", "/tmp/kh"]  # noqa: S108
    env = {"GH_TOKEN": TOKEN}
    assert pr.main(argv, runner=FakeRunner(gh), make_github=lambda t: gh, env=env) == 2
    assert "--zuliprc" in capsys.readouterr().err and gh.writes == []

    def no_zulip(path):
        raise AssertionError("no Zulip access with --no-invite")

    before = rows_of(roster)["alice"]
    assert (
        pr.main(
            [*argv, "--no-invite"],
            runner=FakeRunner(gh),
            make_github=lambda t: gh,
            make_zulip=no_zulip,
            env=env,
        )
        == 0
    )
    assert not any(w[0] == "invite" for w in gh.writes)
    after = rows_of(roster)["alice"]
    assert after["github"] == before["github"] and not after.get("github_invite")


def test_a_zulip_org_without_the_github_field_stops_before_anything(roster, capsys):
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner, zulip=FakeZulip(field_name="Website")) == 2
    assert "GitHub" in capsys.readouterr().err and gh.writes == [] and runner.calls == []


def test_dry_run_reports_the_invitation_and_the_removal(roster, capsys):
    gh = FakeGitHub(repos={"alice": {"empty": False, "keys": []}}, users={"newgh": "NewGH"})
    zulip = FakeZulip({"101": "newgh", "102": ""})
    assert run_main(roster, gh, FakeRunner(gh), "--dry-run", "--only", "alice", zulip=zulip) == 0
    assert gh.writes == []
    assert "would invite NewGH; would remove AliceGH" in capsys.readouterr().out


# ------------------------------------------------------ PM nits on #142 (Mon 09:30, item 4)


def without_zulip_id(roster, slug="bob", ip=BOB, uid="102"):
    text = pathlib.Path(roster).read_text()
    pathlib.Path(roster).write_text(text.replace(f"ws-{slug},{ip},{uid}", f"ws-{slug},{ip},"))


def test_a_late_joiner_is_found_by_the_roster_email(roster):
    """A rerun of provision_repos alone invites someone who joined Zulip after zulip_provision
    ran: the roster e-mail binds the user (never the GitHub field); zulip_user_id stays empty,
    since zulip_provision owns it."""
    without_zulip_id(roster)
    gh = FakeGitHub()
    zulip = FakeZulip({"101": "AliceGH", "102": "BobGH"}, emails={"102": "BOB@example.org"})
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert ("invite", "bob", "BobGH") in gh.writes
    assert invite_state(roster, "bob") == ("BobGH", "invited")
    assert rows_of(roster)["bob"]["zulip_user_id"] == ""


def test_an_email_match_never_takes_a_user_another_row_records(roster):
    without_zulip_id(roster)
    gh = FakeGitHub()
    # user 101 is alice's (recorded), but carries bob's address: bob stays unbound
    zulip = FakeZulip({"101": "AliceGH"}, emails={"101": "bob@example.org"})
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert invite_state(roster, "bob") == ("", "not on Zulip yet")
    assert not any(w[0] == "invite" and w[1] == "bob" for w in gh.writes)


def test_a_hand_edited_zero_padded_id_still_reserves_its_user(roster):
    """`0101` is user 101, as `zulip_provision.recorded_id` reads it (#327): bob's e-mail, which
    user 101 has, never binds that user to bob, and alice reads user 101's GitHub field."""
    without_zulip_id(roster)
    text = pathlib.Path(roster).read_text()
    pathlib.Path(roster).write_text(text.replace("203.0.113.5,101,", "203.0.113.5,0101,"))
    gh = FakeGitHub()
    zulip = FakeZulip({"101": "AliceGH"}, emails={"101": "bob@example.org"})
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    assert invite_state(roster, "bob") == ("", "not on Zulip yet")
    assert invite_state(roster) == ("AliceGH", "invited")
    assert not any(w[0] == "invite" and w[1] == "bob" for w in gh.writes)


def test_an_id_that_is_not_digits_refuses_the_roster(roster, capsys):
    """As `zulip_provision.main` refuses it: a hand edit proves nothing (#327)."""
    text = pathlib.Path(roster).read_text()
    pathlib.Path(roster).write_text(text.replace("203.0.113.7,102,", "203.0.113.7,1o2,"))
    gh = FakeGitHub()
    runner = FakeRunner(gh)
    assert run_main(roster, gh, runner, "--only", "alice") == 2
    assert "invalid zulip_user_id '1o2' on row bob" in capsys.readouterr().err
    assert gh.writes == [] and runner.calls == []


def test_an_invalid_recorded_login_is_never_sent_to_github(roster):
    """#142 removed the github check from validate_row: a hand-typed roster value must not
    reach `DELETE …/collaborators/{previous}` (a URL path)."""
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,bad/../login,")
    pathlib.Path(roster).write_text(text)
    gh = FakeGitHub()
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice") == 0
    assert not any(w[0] in ("withdraw", "remove") for w in gh.writes)
    assert ("invite", "alice", "AliceGH") in gh.writes
    login, state = invite_state(roster)
    assert login == "AliceGH"
    assert state == "invited; ignored the roster's invalid login 'bad/../login'"


# ------------------------------------------------- --invites-only (PM on #36, Mon 16:45)


def provisioned(roster, gh, zulip=None):
    """A full run first: both rows `ok`, alice invited, bob without a login."""
    assert run_main(roster, gh, FakeRunner(gh), zulip=zulip) == 0
    gh.writes.clear()


def invites_only(roster, gh, capsys, *extra, zulip=None):
    runner = FakeRunner(gh)
    code = run_main(roster, gh, runner, "--invites-only", *extra, zulip=zulip)
    out = capsys.readouterr()
    assert not any(c[0] == "ssh" for c in runner.calls)  # never SSH
    return code, out.out, out.err


def test_invites_only_with_nothing_new_prints_one_line_and_writes_nothing(roster, capsys):
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    before = pathlib.Path(roster).stat().st_mtime_ns
    code, out, _ = invites_only(roster, gh, capsys)
    assert code == 0 and gh.writes == []
    # alice's invitation of the full run is still open (invited -> pending is no change)
    assert out == "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 2 unchanged\n"
    assert pathlib.Path(roster).stat().st_mtime_ns == before  # the roster is not rewritten


def test_invites_only_invites_a_new_login_and_notices_an_accepted_one(roster, capsys):
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    gh.collaborators["alice"] = {"alicegh": "admin"}  # alice accepted
    zulip = FakeZulip({"101": "AliceGH", "102": "bobgh"})  # bob filled in his field
    code, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0 and gh.writes == [("invite", "bob", "BobGH")]
    assert (
        out.splitlines()[-1] == "invites: 1 invited, 1 accepted, 0 removed, 0 failed; 0 unchanged"
    )
    assert "bob" in out and "BobGH" in out and "alice" in out  # the two changed rows
    assert invite_state(roster) == ("AliceGH", "collaborator")
    assert invite_state(roster, "bob") == ("BobGH", "invited")


def test_invites_only_replaces_a_changed_login_and_an_expired_invitation(roster, capsys):
    gh = FakeGitHub(users={"alicegh": "AliceGH", "newgh": "NewGH", "bobgh": "BobGH"})
    provisioned(roster, gh, zulip=FakeZulip({"101": "AliceGH", "102": "bobgh"}))
    capsys.readouterr()
    for inv in gh.invites["bob"]:
        inv["expired"] = True  # bob never accepted, and GitHub let it expire
    zulip = FakeZulip({"101": "NewGH", "102": "bobgh"})
    code, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0
    assert ("remove", "alice", "AliceGH") in gh.writes and ("invite", "alice", "NewGH") in gh.writes
    assert any(w[0] == "withdraw" and w[1] == "bob" for w in gh.writes)
    assert ("invite", "bob", "BobGH") in gh.writes
    assert (
        out.splitlines()[-1] == "invites: 2 invited, 0 accepted, 1 removed, 0 failed; 0 unchanged"
    )
    assert invite_state(roster) == ("NewGH", "invited; removed AliceGH")


def test_invites_only_leaves_rows_that_were_never_provisioned_alone(roster, capsys):
    gh = FakeGitHub()
    asked = []
    real_repo, real_user = gh.repo, gh.user
    gh.repo = lambda org, name: asked.append(name) or real_repo(org, name)
    gh.user = lambda login: asked.append(login) or real_user(login)
    before = pathlib.Path(roster).read_text()  # no full run: nothing is provisioned
    code, out, _ = invites_only(
        roster, gh, capsys, zulip=FakeZulip({"101": "AliceGH", "102": "bobgh"})
    )
    assert code == 0 and asked == [] and gh.writes == []
    assert out == (
        "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 0 unchanged; 2 not provisioned yet "
        "(alice, bob): run provision_repos.py without --invites-only\n"
    )
    assert pathlib.Path(roster).read_text() == before


def test_invites_only_reports_failures_with_a_nonzero_exit(roster, capsys):
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    gh.fail["user"] = "Server Error"
    code, out, _ = invites_only(roster, gh, capsys)
    assert code == 1 and "failed: GitHub GET /users/" in out
    assert out.splitlines()[-1].startswith("invites: 0 invited, 0 accepted, 0 removed, 1 failed")
    assert invite_state(roster)[0] == "AliceGH"  # the recorded login stays


def test_invites_only_refuses_a_repository_that_became_public(roster, capsys):
    """codex on #162: like the full run, never give anyone access to a public repository."""
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    gh.repos["bob"]["private"] = False
    code, out, _ = invites_only(
        roster, gh, capsys, zulip=FakeZulip({"101": "AliceGH", "102": "bobgh"})
    )
    assert code == 1 and "MathAgents/bob is not private" in out
    assert not any(w[0] == "invite" and w[1] == "bob" for w in gh.writes)
    assert invite_state(roster, "bob")[0] == ""  # nothing recorded


def test_invites_only_dry_run_and_flag_conflict(roster, capsys):
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    before = pathlib.Path(roster).read_text()
    zulip = FakeZulip({"101": "AliceGH", "102": "bobgh"})
    code, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=zulip)
    assert code == 0 and gh.writes == [] and pathlib.Path(roster).read_text() == before
    assert "would invite BobGH" in out and out.rstrip().endswith("(dry run)")
    code, _, err = invites_only(roster, gh, capsys, "--no-invite")
    assert code == 2 and "contradict" in err


# ----------------------------------------- nothing to change: exit 3 from the dry run (#708)


def no_change(gh):
    return FakeZulip()


def to_invite(gh):  # bob filled in his field
    return FakeZulip({"101": "AliceGH", "102": "bobgh"})


def accepted(gh):  # alice accepted her invitation, for admin (#714)
    gh.collaborators["alice"] = {"alicegh": "admin"}
    gh.invites["alice"] = []
    return FakeZulip()


def to_withdraw(gh):  # alice changed her field: AliceGH's invitation goes
    gh.users["newgh"] = "NewGH"
    return FakeZulip({"101": "NewGH", "102": ""})


def failing(gh):
    gh.fail["user"] = "Server Error"
    return FakeZulip()


def to_record(gh):  # bob's Zulip user is gone: no invitation, but a state to write
    return FakeZulip({"101": "AliceGH"})


@pytest.mark.parametrize(
    ("case", "code"),
    [
        (no_change, pr.NOTHING),
        (to_invite, 0),
        (accepted, 0),
        (to_withdraw, 0),
        (to_record, 0),
        (failing, 1),
    ],
)
def test_an_invites_only_dry_run_exits_nothing_only_when_it_changes_nothing(
    roster, capsys, case, code
):
    """#708: the admin bot offers no plan for exit 3 (ADMIN §3, action 2). Nothing to invite, no
    acceptance to record, nothing to withdraw, no failure, so no roster write; a failure is
    never "nothing to change". The apply exits as before, 0 or 1, and writes every other case."""
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    zulip = case(gh)
    before = pathlib.Path(roster).read_text()
    got, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=zulip)
    assert (got, gh.writes, pathlib.Path(roster).read_text()) == (code, [], before)
    lines = out.splitlines()
    if code == pr.NOTHING:
        assert lines == [
            "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 2 unchanged (dry run)",
            pr.UNCHANGED_LINE,
        ]
    else:
        assert pr.UNCHANGED_LINE not in out and lines[-1].endswith("(dry run)")
    applied, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert applied == (1 if case is failing else 0) and pr.UNCHANGED_LINE not in out
    assert (pathlib.Path(roster).read_text() == before) is (case is no_change)  # a failure too


def test_a_second_invites_only_in_a_row_has_nothing_to_change(roster, capsys):
    """The Harvard case (#708): the second run in a row of one that invited bob."""
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    zulip = to_invite(gh)
    assert invites_only(roster, gh, capsys, zulip=zulip)[0] == 0
    code, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=zulip)
    assert code == pr.NOTHING and out.endswith(f"; 2 unchanged (dry run)\n{pr.UNCHANGED_LINE}\n")


def test_a_refused_or_unprovisioned_row_alone_changes_nothing_and_is_still_shown(roster, capsys):
    """A row refused on every run (`--no-rebind`) or never provisioned changes nothing that an
    apply could: exit 3, with the summary that names it above the line."""
    gh = no_rebind_gh()
    provisioned(roster, gh)
    capsys.readouterr()
    gh.collaborators["alice"] = {"alicegh": "write"}
    gh.users["newgh"] = "NewGH"
    zulip = FakeZulip({"101": "NewGH", "102": ""})
    assert invites_only(roster, gh, capsys, "--no-rebind", zulip=zulip)[0] == 0  # recorded once
    code, out, _ = invites_only(roster, gh, capsys, "--no-rebind", "--dry-run", zulip=zulip)
    assert code == pr.NOTHING and "login changed: refused (AliceGH has write access" in out
    assert out.splitlines()[-2:] == [
        "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 1 unchanged; 1 login changes "
        "refused (alice) (dry run)",
        pr.UNCHANGED_LINE,
    ]
    pathlib.Path(roster).write_text(ROSTER)  # no full run: nothing is provisioned
    code, out, _ = invites_only(roster, FakeGitHub(), capsys, "--dry-run")
    assert code == pr.NOTHING and "2 not provisioned yet (alice, bob)" in out


def test_a_stable_state_that_quotes_would_has_nothing_to_change(roster, capsys):
    """Review of #726 (3): GitHub writes are told by `InviteResult.writes`, never by the
    state's words, which may quote what a participant typed."""
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    zulip = FakeZulip({"101": "AliceGH", "102": "I would like"})
    assert invites_only(roster, gh, capsys, zulip=zulip)[0] == 0
    assert invite_state(roster, "bob") == ("", "invalid login 'i would like' in Zulip")
    code, _, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=zulip)
    assert code == pr.NOTHING and gh.writes == []


@pytest.mark.parametrize("field", ["raise to admin", "i removed it", "invite me", "would raise"])
def test_a_field_that_quotes_the_runs_own_words_is_no_change(roster, capsys, field):
    """Reviewer on #738: a raise, a removal and an invitation are told by `writes` and the
    state's own start, never by words a participant typed, which the state quotes: such a row
    is shown once, then unchanged, counted as nothing it did, and the next dry run has nothing
    to change."""
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    zulip = FakeZulip({"101": "AliceGH", "102": field})
    assert invites_only(roster, gh, capsys, zulip=zulip)[0] == 0
    assert invite_state(roster, "bob") == ("", f"invalid login {field!r} in Zulip")
    code, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0 and gh.writes == []
    assert out == "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 2 unchanged\n"
    code, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=zulip)
    assert code == pr.NOTHING, out


def test_the_listing_of_private_repositories_is_read_page_by_page():
    """The reviewer's #733 finding 5, for `repo_gone` too: more than 100 private repositories
    after a few workshops."""
    pages = {1: [{"name": f"r{i}", "private": True} for i in range(100)], 2: [{"name": "z"}]}
    pages[2] = [{"name": "last", "private": True}]
    seen = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        page = int(req.full_url.rsplit("page=", 1)[1])
        return FakeResponse(200, pages[page])

    gh = pr.GitHub(TOKEN, urlopen=urlopen)
    names = gh.private_repos("MathAgents")
    assert len(names) == 101 and names[-1] == "last" and len(seen) == 2
    pages[2] = [{"name": f"s{i}", "private": True} for i in range(100)]
    with pytest.raises(pr.GitHubError, match="over 2 pages"):
        gh.private_repos("MathAgents", pages=2)


def test_a_long_state_is_compared_as_the_roster_stores_it(roster, capsys):
    """Review of #726 (3): the roster keeps `STATE_CHARS` of a state; the next run compares
    that, so a long state is unchanged then, and the roster isn't rewritten each run."""
    others = [f"row{n:02d}-with-a-long-slug" for n in range(12)]
    lines = ["slug,github,email,repo,repo_status,zulip_user_id"]
    lines.append("alice,,alice@example.org,MathAgents/alice,ok,101")
    lines += [f"{s},,{s}@example.org,MathAgents/{s},ok,{200 + n}" for n, s in enumerate(others)]
    pathlib.Path(roster).write_text("\n".join(lines) + "\n")
    gh = FakeGitHub(users={"sharedgh": "SharedGH"})
    # each row's participant claims the same login in Zulip: it is invited for none of them
    zulip = FakeZulip({str(uid): "SharedGH" for uid in (101, *range(200, 212))})
    code, out, _ = invites_only(roster, gh, capsys, "--only", "alice", zulip=zulip)
    assert code == 0 and "also claimed by row00-with-a-long-slug, row01" in out
    stored = invite_state(roster)[1]
    assert len(stored) == pr.STATE_CHARS
    before = pathlib.Path(roster).read_text()
    code, out, _ = invites_only(roster, gh, capsys, "--only", "alice", zulip=zulip)
    assert (
        code == 0 and out.endswith("; 1 unchanged\n") and pathlib.Path(roster).read_text() == before
    )
    code, _, _ = invites_only(roster, gh, capsys, "--only", "alice", "--dry-run", zulip=zulip)
    assert code == pr.NOTHING


@pytest.mark.parametrize("what", ["collaborator", "invitation"])
def test_a_dry_run_whose_only_change_is_a_raise_is_not_nothing_to_change(roster, capsys, what):
    """#714 over #708: raising a collaborator or an invitation to admin is a write to GitHub
    (`writes` holds `raise`), so its dry run is a plan to approve, never exit 3; once it is
    applied, the next dry run has nothing to change."""
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    if what == "collaborator":  # as 0.2.15 left an accepted invitation: push
        gh.collaborators["alice"] = {"alicegh": "write"}
        gh.invites["alice"] = []
    else:
        for invitation in gh.invites["alice"]:
            invitation["permissions"] = "write"
    state = "collaborator" if what == "collaborator" else "pending"
    gh.writes.clear()
    before = pathlib.Path(roster).read_text()
    code, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=FakeZulip())
    assert code == 0 and pr.UNCHANGED_LINE not in out
    assert f"{state}; would raise to admin" in out and "1 raised to admin" in out
    assert gh.writes == [] and pathlib.Path(roster).read_text() == before
    opts = pr.Options(org="MathAgents", dry_run=True)
    wanted = pr.Wanted(login="AliceGH")
    row = pr.load_roster(str(roster))[0]
    assert pr.invite_row(row, opts, gh, wanted).writes == ["raise"]
    assert invites_only(roster, gh, capsys, zulip=FakeZulip())[0] == 0
    assert invite_state(roster) == ("AliceGH", f"{state}; raised to admin")
    code, out, _ = invites_only(roster, gh, capsys, "--dry-run", zulip=FakeZulip())
    assert code == pr.NOTHING and out.endswith(f"\n{pr.UNCHANGED_LINE}\n")


def test_the_scripts_nothing_is_the_bots():
    from zab.admin.actions import NOTHING

    assert pr.NOTHING == NOTHING


# --------------------------------- the roster write merges (PM on #36, Mon 20:15, item B)


def edit_roster(path, slug, *, delete=False, **values):
    """What another script does meanwhile: `zulip_provision --only`, a spare hand-over edit."""
    rows = ac.read_roster(str(path))
    if delete:
        rows = [r for r in rows if r["slug"] != slug]
    for r in rows:
        if r["slug"] == slug:
            r.update(values)
    ac.write_roster(str(path), rows)


def during_invite(gh, action):
    """Run `action` once, in the middle of the run (at the first invitation)."""
    real = gh.add_collaborator
    done = []

    def add_collaborator(org, name, login):
        if not done:
            done.append(1)
            action()
        return real(org, name, login)

    gh.add_collaborator = add_collaborator


def test_a_full_run_keeps_what_another_script_wrote_meanwhile(roster, capsys):
    gh = FakeGitHub()
    during_invite(gh, lambda: edit_roster(roster, "bob", zulip_user_id="999", ip="198.51.100.9"))
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    rows = rows_of(roster)
    assert rows["bob"]["zulip_user_id"] == "999" and rows["bob"]["ip"] == "198.51.100.9"
    assert rows["bob"]["repo_status"] == "ok"  # this run's own columns still recorded
    assert rows["alice"]["github_invite"] == "invited" and rows["alice"]["repo_status"] == "ok"


def test_invites_only_keeps_what_another_script_wrote_meanwhile(roster, capsys):
    gh = FakeGitHub()
    provisioned(roster, gh)
    capsys.readouterr()
    during_invite(gh, lambda: edit_roster(roster, "alice", ip="198.51.100.10", notes="spare"))
    zulip = FakeZulip({"101": "AliceGH", "102": "bobgh"})  # bob filled in his field
    code, _, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0
    rows = rows_of(roster)
    assert rows["alice"]["ip"] == "198.51.100.10" and rows["alice"]["notes"] == "spare"
    assert invite_state(roster, "bob") == ("BobGH", "invited")


def test_a_row_removed_meanwhile_is_not_written_back(roster, capsys):
    gh = FakeGitHub()
    during_invite(gh, lambda: edit_roster(roster, "bob", delete=True))
    assert run_main(roster, gh, FakeRunner(gh)) == 0
    assert "bob" not in rows_of(roster)  # not resurrected
    assert rows_of(roster)["alice"]["repo_status"] == "ok"
    assert "no longer in the roster, not recorded: bob" in capsys.readouterr().err


def test_a_removal_note_is_no_change_on_the_next_run(roster, capsys):
    """PM nit on #162: `invited; removed X` followed by `pending` is not a change."""
    gh = FakeGitHub(users={"alicegh": "AliceGH", "newgh": "NewGH"})
    provisioned(roster, gh)
    capsys.readouterr()
    zulip = FakeZulip({"101": "NewGH", "102": ""})
    invites_only(roster, gh, capsys, zulip=zulip)
    assert invite_state(roster) == ("NewGH", "invited; removed AliceGH")
    before = pathlib.Path(roster).read_text()
    code, out, _ = invites_only(roster, gh, capsys, zulip=zulip)
    assert code == 0 and out == "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 2 unchanged\n"
    assert pathlib.Path(roster).read_text() == before  # nothing changed, nothing written


def test_a_roster_that_no_longer_reads_is_left_alone_with_rerun(roster, capsys):
    """PM nit on #166 (Mon 22:30): a roster broken meanwhile (here a reserved slug) is not
    written; the run says to fix it and rerun, prints its changes, and exits 1."""
    gh = FakeGitHub()

    def break_roster():
        pathlib.Path(roster).write_text(
            pathlib.Path(roster).read_text() + "zab-spare-9,,,,x@example.org\n"
        )

    during_invite(gh, break_roster)
    assert run_main(roster, gh, FakeRunner(gh)) == 1
    broken = pathlib.Path(roster).read_text()
    err = capsys.readouterr().err
    assert "roster NOT written" in err and "rerun provision_repos.py" in err
    assert "alice: " in err and "repo_status=ok" in err
    assert "zab-spare-9" in broken and "repo_status" not in broken.split("\n", 1)[1]


def test_only_the_run_note_is_set_aside_when_states_are_compared():
    """PM nit on #166: `_same_state` splits off `; removed …`/`; ignored …` only; a failure's
    own semicolons stay, so two different failures still differ."""
    same = pr._same_state
    assert same("invited; removed AliceGH") == same("pending")
    assert same("pending; would remove X") == "pending"
    assert same("collaborator; ignored the roster's invalid login 'a;b'") == "collaborator"
    assert same("failed: 422; quota") != same("failed: 422; other")


# ------------------------------------------------------ --no-rebind (docs/ADMIN.md §2.4, #312)

REFUSED_ALICE = "login changed: refused (OldGH has write access; a rebind is done by hand)"


class NoRemoveGitHub(FakeGitHub):
    """`remove_collaborator` must never run with --no-rebind; `on_withdraw` plays an acceptance
    that races the withdrawal (`ok`: the DELETE still answered 204)."""

    on_withdraw: str = ""

    def remove_collaborator(self, org, name, login):
        raise AssertionError(f"remove_collaborator({name}, {login}) under --no-rebind")

    def delete_invitation(self, org, name, invitation_id):
        if self.on_withdraw:
            invitee = next(i["invitee"]["login"] for i in self.invites[name])
            self.collaborators.setdefault(name, {})[invitee.casefold()] = "write"
            if self.on_withdraw == "404":
                self.invites[name] = []
                raise pr.GitHubError("DELETE", "/invitations", 404, {"message": "Not Found"})
        super().delete_invitation(org, name, invitation_id)


def old_login_roster(roster):
    text = pathlib.Path(roster).read_text().replace("alice,AliceGH,", "alice,OldGH,")
    pathlib.Path(roster).write_text(text)


def no_rebind_gh(**kwargs) -> NoRemoveGitHub:
    users = {"alicegh": "AliceGH", "oldgh": "OldGH"}
    return NoRemoveGitHub(repos={"alice": {"empty": False, "keys": []}}, users=users, **kwargs)


def access_writes(gh):
    return [w for w in gh.writes if w[0] in ("withdraw", "remove", "invite")]


def test_no_rebind_never_removes_a_previous_login_that_has_access(roster, capsys):
    old_login_roster(roster)
    gh = no_rebind_gh(collaborators={"alice": {"oldgh": "write"}})
    args = ("--only", "alice", "--no-rebind")
    assert run_main(roster, gh, FakeRunner(gh), *args, "--dry-run") == 0
    assert REFUSED_ALICE in capsys.readouterr().out and gh.writes == []
    assert run_main(roster, gh, FakeRunner(gh), *args) == 0  # the repository is ready anyway
    out = capsys.readouterr().out
    assert REFUSED_ALICE in out and "1 login changes refused (alice)" in out
    assert access_writes(gh) == []  # nothing withdrawn, removed or invited
    assert invite_state(roster) == ("OldGH", REFUSED_ALICE)  # the roster keeps the old login


def test_no_rebind_replaces_a_login_whose_invitation_is_still_pending(roster, capsys):
    """workshop/admin/README.md §1b: a participant corrects a typo in their Zulip GitHub field
    before they accepted; the old login never had access, so its invitation is withdrawn and
    replaced."""
    old_login_roster(roster)
    gh = no_rebind_gh(invites={"alice": [{"id": 5, "invitee": {"login": "OldGH"}}]})
    args = ("--only", "alice", "--no-rebind")
    assert run_main(roster, gh, FakeRunner(gh), *args, "--dry-run") == 0
    assert "would invite AliceGH; would remove OldGH" in capsys.readouterr().out
    assert run_main(roster, gh, FakeRunner(gh), *args) == 0
    assert access_writes(gh) == [("withdraw", "alice", 5), ("invite", "alice", "AliceGH")]
    assert invite_state(roster) == ("AliceGH", "invited; removed OldGH")


@pytest.mark.parametrize("delete_answer", ["404", "ok"])
def test_an_acceptance_that_races_the_withdrawal_is_reported_never_removed(
    roster, capsys, delete_answer
):
    old_login_roster(roster)
    gh = no_rebind_gh(invites={"alice": [{"id": 5, "invitee": {"login": "OldGH"}}]})
    gh.on_withdraw = delete_answer  # OldGH accepts while the invitation is being withdrawn
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice", "--no-rebind") == 0
    assert not any(w[0] in ("remove", "invite") for w in gh.writes)
    assert invite_state(roster) == ("OldGH", REFUSED_ALICE)
    assert gh.collaborators["alice"] == {"oldgh": "write"}


def test_invites_only_no_rebind_shows_a_refused_row_on_every_run(roster, capsys):
    gh = no_rebind_gh()
    provisioned(roster, gh)
    capsys.readouterr()
    gh.collaborators["alice"] = {"alicegh": "admin"}  # alice accepted, then changed her field
    gh.users["newgh"] = "NewGH"
    zulip = FakeZulip({"101": "NewGH", "102": ""})
    for _ in range(2):
        code, out, _ = invites_only(roster, gh, capsys, "--no-rebind", zulip=zulip)
        assert code == 0 and access_writes(gh) == []
        refused = "login changed: refused (AliceGH has admin access; a rebind is done by hand)"
        assert refused in out
        assert out.splitlines()[-1] == (
            "invites: 0 invited, 0 accepted, 0 removed, 0 failed; 1 unchanged; "
            "1 login changes refused (alice)"
        )
    assert invite_state(roster) == ("AliceGH", refused)


@pytest.mark.parametrize("recorded", ["@OldGH", "https://github.com/OldGH", "bad/../login"])
def test_no_rebind_refuses_a_recorded_login_it_cannot_check(roster, capsys, recorded):
    """Reviewer on #320 (4): a hand-edited `github` is not ignored under --no-rebind (OldGH
    may have access): the row is refused, nothing is sent to GitHub, the roster keeps it."""
    pathlib.Path(roster).write_text(
        pathlib.Path(roster).read_text().replace("alice,AliceGH,", f"alice,{recorded},")
    )
    gh = no_rebind_gh(collaborators={"alice": {"oldgh": "write"}})
    assert run_main(roster, gh, FakeRunner(gh), "--only", "alice", "--no-rebind") == 0
    state = f"login changed: refused (the roster's github {recorded!r} is not a login; fix it)"
    assert state in capsys.readouterr().out and access_writes(gh) == []
    assert invite_state(roster) == (recorded, state)
    provisioned_state = invite_state(roster)
    code, out, _ = invites_only(roster, gh, capsys, "--no-rebind", "--only", "alice")
    assert code == 0 and state in out and "1 login changes refused (alice)" in out
    assert access_writes(gh) == [] and invite_state(roster) == provisioned_state
