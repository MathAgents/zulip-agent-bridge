#!/usr/bin/env python3
"""`provision_repos.py --roster roster.csv [--only SLUG] [--invites-only] [--dry-run]`
(README.md §1b).

For every participant row (identified by `slug`), on Drew's laptop with his GitHub token:

1. create the private repository `MathAgents/<slug>` unless it exists, and push the workshop
   template (`workshop/template/`) as its first commit when it is empty;
2. on the participant's VM, over SSH as `ubuntu` with the host key pinned (`scan_hosts.py`),
   check that the VM says it belongs to this participant, then as `agent` generate the
   repository's deploy key (`~/.ssh/zab_<slug>`, the `zab setup-repo` convention) and read back
   its public half;
3. register that public key on the repository as a deploy key with write access, unless it is
   already there;
4. run `zab setup-repo MathAgents/<slug> --yes` on the VM as `agent`: the SSH host alias, the
   clone to `~/work/<slug>` (or the remote of an existing checkout), the record `zab preflight`
   reads, and a test push of a branch that is deleted again;
5. invite the participant to their own repository as its administrator (`PUT
   /repos/MathAgents/<slug>/collaborators/{login}` with `admin`, #714: they add collaborators,
   and may delete or transfer it), unless the login is already an admin or has a pending
   invitation (issue #36, Mon Oct 5: participants get no SSH, and no organization membership).
   A collaborator with less is raised to admin, and so is a pending invitation (`PATCH
   /repos/MathAgents/<slug>/invitations/{id}`): the row says `raised to admin`. The login comes
   from the "GitHub" custom profile field of the
   Zulip user bound to the row (`zulip_user_id`, filled in by `zulip_provision.py`), read with
   the organizer's zuliprc (`--zuliprc`); the roster's `github` is the fallback, as a manual
   override, only while that field is empty. The login is self-reported (a typo is the
   participant's), so it is checked for syntax and looked up on GitHub, never trusted further.
   When it differs from the login the roster recorded last time, the old login's pending
   invitation is withdrawn and its collaborator access removed first. With `--no-rebind` (the
   admin bot always passes it; docs/ADMIN.md §2.4) access is never removed: only the old
   login's pending invitations are withdrawn, then its permission is checked again, and if it
   has access the row is reported (`login changed: refused …`), the new login is not invited and
   the roster keeps the old one. A login claimed by two rows is invited for neither.

The roster gets `repo` (`MathAgents/<slug>`), `repo_status`, `github` (the login) and
`github_invite` (`invited`, `pending`, `collaborator` (an admin; an org owner counts too),
`collaborator; raised to admin`, `no login in Zulip`, `unknown login`, …), written back
atomically. Each row is handled on its own: a failure is reported in the table
and never stops the others; the exit code is non-zero if any row failed. `--dry-run` changes
nothing anywhere (it still reads GitHub and Zulip and runs the read-only owner check on each VM).

**The token never leaves the laptop.** It comes from `GH_TOKEN`, `GITHUB_TOKEN` or `gh auth
token`, is sent only to api.github.com and, for the template push, to github.com through git's
environment (`GIT_CONFIG_*`, never an argument or a URL), and is never printed, logged, written
to the roster or sent to a VM: the VMs only ever hold their own deploy key. Token scopes: see
`workshop/admin/README.md`.

`--invites-only` (PM on issue #36, Mon 16:45: Drew reruns it through Friday, maybe every 15
minutes) does step 5 alone, for rows already provisioned (`repo_status` = `ok`): no SSH, no
repository creation, no deploy keys. It reads the Zulip GitHub fields, invites new logins,
replaces a changed login (withdrawing the old one's invitation and access) and an expired
invitation, raises a collaborator or an invitation with less than admin, and notices accepted
ones. It prints only the rows whose state changed or failed,
then one summary line, so a run that changes nothing prints that line alone; it writes the
roster only when something changed. Rows never provisioned are counted, not touched. Its dry
run that would change nothing (`nothing_to_change`: nothing to invite, withdraw or raise to
admin, no state to record, no failure) says so on a line of its own and exits 3 (`NOTHING`),
which the admin bot answers with no plan (#708).

Run it after `launch.py`, `scan_hosts.py` and `zulip_provision.py`, and before `push_config.py`,
whose `default_project` is the checkout this script creates; rerun it any time (also during the
workshop) to pick up GitHub logins added to Zulip since. Standalone: `uv run python
workshop/admin/provision_repos.py …`.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import pathlib
import re
import shlex
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    Runner,
    column_snapshot,
    first_line,
    format_table,
    host_of,
    load_roster,
    load_zuliprc,
    redact,
    run_parallel,
    save_roster_changes,
    ssh_argv,
    subprocess_runner,
)
from zulip_provision import ZulipAdmin, _field_value, find_user, github_field_id, recorded_id

ORG = "MathAgents"
TEMPLATE_DIR = pathlib.Path(__file__).resolve().parents[1] / "template"
API = "https://api.github.com"
GIT_BASE = "https://github.com"
AGENT = "agent"
ZAB = "/home/agent/.local/bin/zab"  # installed for agent on the AMI (uv tool)
COMMITTER = ("MathAgents workshop", "mathagents@users.noreply.github.com")
#: A slug names the repository, the VM (`ws-<slug>`) and the key file: letters, digits and
#: hyphens only, so it is safe in a path, a hostname and the remote shell unquoted.
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
#: What participants paste into the profile field besides the bare login: a profile URL.
_PROFILE_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)
_PUBKEY_RE = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\s]+)?$")
HOST_MARK = "---ZAB-HOST"
#: `--invites-only --dry-run`'s exit when it would change nothing (#708): the admin bot's
#: `actions.NOTHING`, as roster_sync.py's. Never a full run's, nor an apply's.
NOTHING = 3
UNCHANGED_LINE = "nothing to change: no invitation to send or withdraw, and no acceptance to record"
#: Fixed literal: the VM's own record of its owner (written at first boot,
#: ../ami/README.md, "First boot").
OWNER_CMD = f"cat /etc/zab-participant.json 2>/dev/null; echo; echo {HOST_MARK}; hostname"


# ------------------------------------------------------------------------------------- token


def github_token(env: Mapping[str, str] | None = None, runner: Runner = subprocess_runner) -> str:
    """Drew's token: `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token`; never printed."""
    env = os.environ if env is None else env
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if env.get(name, "").strip():
            return env[name].strip()
    res = runner(["gh", "auth", "token"], timeout_s=30)
    if res.ok and res.stdout.strip():
        return res.stdout.strip()
    raise ValueError(
        "no GitHub token: set GH_TOKEN (scopes in workshop/admin/README.md) or run `gh auth login`"
    )


# ------------------------------------------------------------------------------------ GitHub


class GitHubError(RuntimeError):
    """An unexpected GitHub API answer; the message names the call, never the token."""

    def __init__(self, method: str, path: str, status: int, payload: Any) -> None:
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        super().__init__(f"GitHub {method} {path}: HTTP {status} {message}".strip())
        self.status = status


class GitHub:
    """The few REST calls this script makes (https://docs.github.com/en/rest)."""

    def __init__(self, token: str, *, urlopen: Callable[..., Any] = urllib.request.urlopen) -> None:
        self._token = token
        self._urlopen = urlopen

    def _call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        request = urllib.request.Request(  # noqa: S310 - fixed https base URL
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "zab-provision-repos",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with self._urlopen(request, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload

    def repo(self, org: str, name: str) -> dict[str, Any] | None:
        """The repository's metadata, or None when it does not exist."""
        status, payload = self._call("GET", f"/repos/{org}/{name}")
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise GitHubError("GET", f"/repos/{org}/{name}", status, payload)
        return payload

    def create_repo(self, org: str, name: str) -> None:
        body = {
            "name": name,
            "private": True,
            "description": "Workshop repository",  # no event or date: every workshop's (#692)
            "has_wiki": False,
            "has_projects": False,
            "auto_init": False,
        }
        status, payload = self._call("POST", f"/orgs/{org}/repos", body)
        if status != 201:
            raise GitHubError("POST", f"/orgs/{org}/repos", status, payload)

    def repo_empty(self, org: str, name: str) -> bool:
        """True for a repository without commits (the commits API answers 409 for those)."""
        status, payload = self._call("GET", f"/repos/{org}/{name}/commits?per_page=1")
        if status == 409:
            return True
        if status != 200:
            raise GitHubError("GET", f"/repos/{org}/{name}/commits", status, payload)
        return not payload

    def deploy_keys(self, org: str, name: str) -> list[dict[str, Any]]:
        status, payload = self._call("GET", f"/repos/{org}/{name}/keys?per_page=100")
        if status != 200 or not isinstance(payload, list):
            raise GitHubError("GET", f"/repos/{org}/{name}/keys", status, payload)
        return payload

    def delete_deploy_key(self, org: str, name: str, key_id: int) -> None:
        status, payload = self._call("DELETE", f"/repos/{org}/{name}/keys/{key_id}")
        if status != 204:
            raise GitHubError("DELETE", f"/repos/{org}/{name}/keys/{key_id}", status, payload)

    def add_deploy_key(self, org: str, name: str, title: str, key: str) -> None:
        body = {"title": title, "key": key, "read_only": False}
        status, payload = self._call("POST", f"/repos/{org}/{name}/keys", body)
        if status != 201:
            raise GitHubError("POST", f"/repos/{org}/{name}/keys", status, payload)

    def user(self, login: str) -> dict[str, Any] | None:
        """`GET /users/{login}` (`login` with GitHub's own capitals, `id`), or None for a 404."""
        status, payload = self._call("GET", f"/users/{login}")
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict) or not payload.get("login"):
            raise GitHubError("GET", f"/users/{login}", status, payload)
        return payload

    def permission(self, org: str, name: str, login: str) -> str:
        """`admin`, `write`, `read` or `none`: the login's access to the repository (`GET
        /repos/{owner}/{repo}/collaborators/{username}/permission`; `none` for a stranger)."""
        path = f"/repos/{org}/{name}/collaborators/{login}/permission"
        status, payload = self._call("GET", path)
        if status == 404:
            return "none"
        if status != 200 or not isinstance(payload, dict):
            raise GitHubError("GET", path, status, payload)
        return str(payload.get("permission") or "none")

    def private_repos(self, org: str, pages: int = 50) -> list[str]:
        """The names of `org`'s private repositories that the token lists, page by page, 100 a
        page; GitHubError if a page fails, or there are more than `pages` of them."""
        names: list[str] = []
        for page in range(1, pages + 1):
            path = f"/orgs/{org}/repos?type=private&per_page=100&page={page}"
            status, payload = self._call("GET", path)
            if status != 200 or not isinstance(payload, list):
                raise GitHubError("GET", path, status, payload)
            names += [
                str(r.get("name", "")) for r in payload if isinstance(r, dict) and r.get("private")
            ]
            if len(payload) < 100:
                return names
        raise GitHubError("GET", f"/orgs/{org}/repos", 0, {"message": f"over {pages} pages"})

    def invitations(self, org: str, name: str) -> list[dict[str, Any]]:
        """The repository's open invitations (`id`, `invitee.login`, `expired`, …)."""
        path = f"/repos/{org}/{name}/invitations?per_page=100"
        status, payload = self._call("GET", path)
        if status != 200 or not isinstance(payload, list):
            raise GitHubError("GET", path, status, payload)
        return payload

    def add_collaborator(self, org: str, name: str, login: str) -> bool:
        """Invite `login` as an admin (#714); True when an invitation was created (201), False
        when GitHub gave the login admin directly (204: a collaborator already, whose permission
        this raises, or an org member)."""
        path = f"/repos/{org}/{name}/collaborators/{login}"
        status, payload = self._call("PUT", path, {"permission": "admin"})
        if status not in (201, 204):
            raise GitHubError("PUT", path, status, payload)
        return status == 201

    def raise_invitation(self, org: str, name: str, invitation_id: int) -> None:
        """A pending invitation's permission raised to admin (#714)."""
        path = f"/repos/{org}/{name}/invitations/{int(invitation_id)}"
        status, payload = self._call("PATCH", path, {"permissions": "admin"})
        if status != 200:
            raise GitHubError("PATCH", path, status, payload)

    def delete_invitation(self, org: str, name: str, invitation_id: int) -> None:
        path = f"/repos/{org}/{name}/invitations/{int(invitation_id)}"
        status, payload = self._call("DELETE", path)
        if status != 204:
            raise GitHubError("DELETE", path, status, payload)

    def remove_collaborator(self, org: str, name: str, login: str) -> None:
        """Remove `login`'s direct access; a 404 (it had none) is fine."""
        path = f"/repos/{org}/{name}/collaborators/{login}"
        status, payload = self._call("DELETE", path)
        if status not in (204, 404):
            raise GitHubError("DELETE", path, status, payload)


def repo_gone(gh: GitHub, org: str, name: str) -> bool:
    """Whether `org/name` is gone from GitHub (#714): deleted, or transferred to another owner.
    Found under another owner, named in GitHub's answer after its redirect, is gone at once:
    GitHub itself says where it went (Codex on #738: the VM's deploy key and the redirect would
    push there). That is where this differs from the file server's rule, which confirms even a
    transfer: here a gone row is only reported, and its VM left as it is, never deleted. A 404 is
    the file server's rule (`workshop/files/README.md`, *Operations and bounds*): then the token
    lists the organization's private repositories, without it, and reads another of them; then
    not found again. Any other answer (a 200 without an owner's login among them), or a token
    that reads nothing private, is not gone, so a failure, an outage or a bad token never says a
    repository is gone."""

    def where(repo: str) -> str:
        """`here`, `elsewhere` (another owner's), `absent` (a 404), or `unknown`: a 200 that
        names no owner (reviewer on #738)."""
        meta = gh.repo(org, repo)  # None for a 404; a rename within `org` is followed
        if meta is None:
            return "absent"
        owner = meta.get("owner") if isinstance(meta, dict) else None
        login = owner.get("login") if isinstance(owner, dict) else None
        if not isinstance(login, str) or not login:
            return "unknown"
        return "here" if login.casefold() == org.casefold() else "elsewhere"

    try:
        if (first := where(name)) != "absent":
            return first == "elsewhere"  # `here`, `unknown`: not gone
        listed = {repo.casefold() for repo in gh.private_repos(org)}
        others = sorted(listed - {name.casefold()})
        if name.casefold() in listed or not others or where(others[0]) != "here":
            return False
        return where(name) in ("absent", "elsewhere")  # a second look without an owner: no
    except (GitHubError, OSError, http.client.HTTPException):
        return False


# ----------------------------------------------------------------------- template, on the laptop


def git_env(token: str) -> dict[str, str]:
    """git's environment for the template push: the token as an HTTP header set through
    `GIT_CONFIG_*` (git 2.31+), so it is in no argument list, URL, config file or credential
    store; no prompting; any configured credential helper switched off."""
    env = dict(os.environ)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0=f"http.{GIT_BASE}/.extraheader",
        GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
        GIT_CONFIG_KEY_1="credential.helper",
        GIT_CONFIG_VALUE_1="",
    )
    return env


def seed_repo(
    org: str,
    name: str,
    token: str,
    runner: Runner,
    *,
    template: pathlib.Path = TEMPLATE_DIR,
    remote_base: str = GIT_BASE,
) -> None:
    """Push the template as the repository's first commit, from a temporary directory."""
    with tempfile.TemporaryDirectory(prefix="zab-template-") as tmp:
        work = pathlib.Path(tmp) / name
        shutil.copytree(template, work)
        readme = work / "README.md"
        if readme.exists():
            text = readme.read_text(encoding="utf-8")
            readme.write_text(
                text.replace("{{repo}}", f"{org}/{name}").replace("{{name}}", name),
                encoding="utf-8",
            )
        env = git_env(token)
        user, email = COMMITTER
        for argv in (
            ["git", "-C", str(work), "init", "-q", "-b", "main"],
            ["git", "-C", str(work), "add", "-A"],
            [
                "git",
                "-C",
                str(work),
                "-c",
                f"user.name={user}",
                "-c",
                f"user.email={email}",
                "commit",
                "-q",
                "-m",
                "Workshop template",
            ],
            ["git", "-C", str(work), "push", "-q", f"{remote_base}/{org}/{name}.git", "main:main"],
        ):
            res = runner(argv, timeout_s=180, env=env)
            if not res.ok:
                why = first_line(res.stderr) or first_line(res.stdout) or f"exit {res.exit_code}"
                raise RuntimeError(f"template {argv[3]}: {redact(why)}")


# -------------------------------------------------------------------------------- on the VM


def as_agent(command: str) -> str:
    """The remote command `sudo -u agent -H bash -lc 'cd / && <command>'`, quoted for ssh
    (`cd /`: agent cannot read /home/ubuntu, where the ssh session starts; push_config.py)."""
    return shlex.join(["sudo", "-u", AGENT, "-H", "bash", "-lc", f"cd / && {command}"])


def keygen_command(slug: str) -> str:
    """As agent: `~/.ssh/zab_<slug>` (ed25519, no passphrase, the `zab setup-repo` convention and
    comment) unless it exists, then its public half. `slug` is validated: safe unquoted."""
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}")
    key = f"~/.ssh/zab_{slug}"
    return (
        f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"{{ test -f {key} || ssh-keygen -q -t ed25519 -N '' -C zab-ws-{slug}-{slug} -f {key}; }}"
        f" && cat {key}.pub"
    )


def identity(row: Mapping[str, str]) -> str:
    """A participant's name for the repository and the VM: `slug`, or `github` in a roster
    without slugs (the launch scripts' identity until they move to slugs; codex on #96)."""
    return (row.get("slug") or "").strip() or (row.get("github") or "").strip()


def owner_problem(stdout: str, row: Mapping[str, str]) -> str | None:
    """None when the VM's record and hostname name this participant, else why nothing may be
    done there: a wrong VM would get write access to this participant's repository.

    The VM is identified the way launch.py recorded it: by `slug` when its record has one,
    otherwise by the row's `github` login (records written before slugs existed); its hostname
    is `ws-<that name>`."""
    text = stdout.replace("\r", "")
    if HOST_MARK not in text:
        return "owner check: unexpected output"
    json_part, _, host_part = text.partition(HOST_MARK)
    host_lines = [line.strip() for line in host_part.strip().splitlines() if line.strip()]
    hostname = host_lines[-1] if host_lines else ""
    data: dict[str, Any] = {}
    if json_part.strip():
        try:
            parsed = json.loads(json_part)
        except json.JSONDecodeError:
            return "owner check: /etc/zab-participant.json is not JSON"
        if not isinstance(parsed, dict):
            return "owner check: /etc/zab-participant.json is not an object"
        data = parsed
    recorded_slug = str(data.get("slug") or "").strip()
    recorded_github = str(data.get("github") or "").strip()
    if data.get("spare") and not (recorded_slug or recorded_github):
        return f"VM is the spare {data['spare']!r}: hand it over with push_config.py --adopt-spare"
    if recorded_slug:
        owner, expected = recorded_slug, (row.get("slug") or "").strip() or identity(row)
    else:
        owner, expected = recorded_github, (row.get("github") or "").strip()
    if not owner or owner.casefold() != expected.casefold():
        who = owner or "(no record)"
        return f"VM belongs to {who!r} (hostname {hostname!r}), not {identity(row)!r}"
    if hostname.casefold() != f"ws-{owner}".casefold():
        return f"VM hostname is {hostname!r}, expected ws-{owner}"
    # A first boot before #142 imported the recorded login's SSH keys for `agent`: a VM
    # launched with a login the roster has since corrected or removed still trusts that account,
    # and must not get write access to this repository (codex round 3 on #96).
    roster_github = (row.get("github") or "").strip()
    if recorded_github and recorded_github.casefold() != roster_github.casefold():
        now = roster_github or "(none)"
        return (
            f"VM was set up for GitHub login {recorded_github!r} (its SSH keys are imported), "
            f"but the roster says {now!r}: relaunch it, or remove the keys by hand"
        )
    return None


# ------------------------------------------------------------------------------------- per row


@dataclass(slots=True)
class RowResult:
    slug: str
    host: str = ""
    repo: str = ""
    repo_action: str = ""  # created | exists | (dry run) would create
    seed: str = ""  # template | not empty | (dry run) would seed
    key: str = ""  # added | present
    checkout: str = ""  # the setup-repo verdict
    invite: str = ""  # the `github_invite` state (empty: invitations off)
    login: str | None = None  # the `github` to record (None: leave the column alone)
    skipped: str = ""  # why the row was left alone (its `repo` is someone else's)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class Options:
    org: str = ORG
    dry_run: bool = False
    invite: bool = True
    known_hosts: str = str(DEFAULT_KNOWN_HOSTS)
    template: pathlib.Path = TEMPLATE_DIR
    remote_base: str = GIT_BASE
    force: bool = False  # provision rows whose `repo` names another repository too
    no_rebind: bool = False  # never remove a previous login's access (`keep_access`)


# ------------------------------------------------------------------------------ the login


@dataclass(frozen=True, slots=True)
class Wanted:
    """The login a row's participant should have access with, or why there is none."""

    login: str = ""
    state: str = ""  # the `github_invite` value when there is no login


def normalize_login(value: str) -> str:
    """`@alice`, `alice` or `https://github.com/alice/` → `alice` (the field is free text)."""
    value = value.strip().lstrip("@")
    return _PROFILE_URL_RE.sub("", value).strip().strip("/")


@dataclass(frozen=True, slots=True)
class ZulipView:
    """What this script reads from Zulip: each human user's "GitHub" field, and the users, so
    that a row without `zulip_user_id` is bound the way `zulip_provision.py` binds it."""

    logins: Mapping[str, str] = field(default_factory=dict)  # user id → field value ("" empty)
    users: tuple[dict[str, Any], ...] = ()
    field_id: int | None = None

    def get(self, uid: str, default: str = "") -> str:
        return self.logins.get(uid, default)

    def __contains__(self, uid: object) -> bool:
        return uid in self.logins

    def user_id(self, row: Mapping[str, str], taken: frozenset[str] = frozenset()) -> str:
        """The row's `zulip_user_id`, else the user its roster e-mail binds (`find_user`: the
        e-mail alone, never the GitHub field), unless another row records that user (PM nit on
        #142: a late joiner is invited by a rerun of this script alone). Not written back:
        `zulip_provision.py` owns the column, and its run also subscribes the participant.
        The recorded id is `recorded_id`'s, as `logins` keys it (`0101` is 101, #327)."""
        uid = recorded_id(row)
        if uid or not self.users:
            return uid
        user, _, _ = find_user(list(self.users), dict(row), self.field_id)
        found = str(user["user_id"]) if user is not None else ""
        return "" if found in taken else found


def zulip_github_logins(admin: Any) -> ZulipView:
    """Zulip user id (as the roster writes it) → the value of that user's "GitHub" custom
    profile field (BOOTSTRAP §5.7), for every human user; "" when the field is empty."""
    field_id = github_field_id(admin.profile_fields())
    if field_id is None:
        raise ValueError("the Zulip organization has no custom profile field named GitHub")
    users = tuple(admin.users())
    logins = {
        str(user["user_id"]): normalize_login(_field_value(user, field_id))
        for user in users
        if not user.get("is_bot") and "user_id" in user
    }
    return ZulipView(logins=logins, users=users, field_id=field_id)


def wanted_login(
    row: Mapping[str, str],
    zulip: Mapping[str, str] | ZulipView,
    taken: frozenset[str] = frozenset(),
) -> Wanted:
    """The login from the Zulip user bound to the row (`zulip_user_id`, or the roster e-mail
    for a row without one), else the roster's `github` as a manual override; self-reported
    either way, so only its syntax is checked."""
    if isinstance(zulip, ZulipView):
        uid = zulip.user_id(row, taken)
    else:
        uid = recorded_id(row)
    field_value = zulip.get(uid, "") if uid else ""
    login = field_value or (row.get("github") or "").strip()
    if not login:
        if not uid:
            return Wanted(state="not on Zulip yet")
        if uid not in zulip:
            return Wanted(state=f"Zulip user {uid} not found")
        return Wanted(state="no login in Zulip")
    if not _LOGIN_RE.match(login):
        where = "in Zulip" if field_value else "in the roster"
        return Wanted(state=f"invalid login {login[:40]!r} {where}")
    return Wanted(login=login)


def wanted_logins(
    chosen: list[dict[str, str]],
    every: list[dict[str, str]],
    zulip: Mapping[str, str] | ZulipView,
) -> list[Wanted]:
    """`wanted_login` for each chosen row. A login is invited for a row only if no other row of
    the whole roster claims it, in Zulip today or as its recorded `github`: one person's
    repository would otherwise open to another, and the roster would hold a duplicate login
    (`check_duplicates`). The claims are computed for every row, `--only` or not, and a recorded
    login stays reserved until its row has moved off it (codex on #142)."""
    # Zulip users that some row records, normalized (`0101` is 101, #327): an e-mail lookup
    # never binds one of them to another row
    taken = frozenset(recorded_id(row) for row in every) - {""}
    claims: dict[str, set[str]] = {}
    for row in every:
        name = identity(row)
        if not name or row.get("spare"):
            continue
        for login in ((row.get("github") or "").strip(), wanted_login(row, zulip, taken).login):
            if login:
                claims.setdefault(login.casefold(), set()).add(name)
    out = []
    for row in chosen:
        want = wanted_login(row, zulip, taken)
        others = sorted(claims.get(want.login.casefold(), set()) - {identity(row)})
        if want.login and others:
            want = Wanted(state=f"login {want.login} also claimed by {', '.join(others)}")
        out.append(want)
    return out


def foreign_repo(repo: str, org: str, slug: str) -> str:
    """Why a row's `repo` is not this script's `<org>/<slug>`, or "" when it is (or is empty).

    `repo` may be `owner/name` or a bare checkout name (README.md §4); either one naming
    something else means the row is set up some other way, and rewriting it would change the
    checkout the bridge works in."""
    repo = repo.strip().rstrip("/")
    if not repo:
        return ""
    owner, _, name = repo.rpartition("/")
    name = name[:-4] if name.endswith(".git") else name
    if owner and owner.split("/")[-1].split(":")[-1].casefold() != org.casefold():
        return f"repo {repo!r} belongs to another owner"
    if name.casefold() != slug.casefold():
        return f"repo {repo!r} is not {org}/{slug}"
    return ""


def provision_row(
    row: Mapping[str, str],
    opts: Options,
    gh: GitHub,
    token: str,
    runner: Runner,
    wanted: Wanted | None,
) -> RowResult:
    slug = identity(row)
    result = RowResult(slug=slug)
    if not _SLUG_RE.match(slug):
        result.failures.append(f"invalid slug {slug!r}")
        return result
    org = opts.org
    result.repo = f"{org}/{slug}"
    if (why := foreign_repo(row.get("repo", ""), org, slug)) and not opts.force:
        # An organizer's own row (Drew's `AndrewVSutherland/nt-agents-test`) would get a new
        # repository and, at the next push, a new default_project (PM follow-up on #36).
        result.repo, result.skipped = (
            row.get("repo", ""),
            f"{why}; --force to provision {org}/{slug}",
        )
        return result
    try:
        result.host = host_of(row)
    except ValueError as exc:
        result.failures.append(str(exc))
        return result

    def ssh(remote: str, timeout_s: float = 120.0):
        return runner(
            ssh_argv(result.host, remote, known_hosts=opts.known_hosts), timeout_s=timeout_s
        )

    res = ssh(OWNER_CMD, 60)
    if not res.ok:
        result.failures.append(f"owner check: {first_line(res.stderr) or f'exit {res.exit_code}'}")
        return result
    if problem := owner_problem(res.stdout, row):
        result.failures.append(problem)
        return result

    if (existing := gh.repo(org, slug)) is not None:
        if existing.get("private") is not True:  # never hand a public repo to a participant
            result.failures.append(f"{org}/{slug} exists and is not private: nothing done")
            return result
        result.repo_action = "exists"
    elif opts.dry_run:
        result.repo_action = "would create"
    else:
        gh.create_repo(org, slug)
        result.repo_action = "created"
    if result.repo_action == "would create" or gh.repo_empty(org, slug):
        if opts.dry_run:
            result.seed = "would seed"
        else:
            seed_repo(
                org, slug, token, runner, template=opts.template, remote_base=opts.remote_base
            )
            result.seed = "template"
    else:
        result.seed = "not empty"

    if opts.dry_run:
        result.key = "would add"
        result.checkout = "would clone and test a push"
    else:
        res = ssh(as_agent(keygen_command(slug)))
        pub = res.stdout.strip().splitlines()[-1].strip() if res.ok and res.stdout.strip() else ""
        if not _PUBKEY_RE.match(pub):
            why = first_line(res.stderr) or ("no public key" if res.ok else f"exit {res.exit_code}")
            result.failures.append(f"deploy key on the VM: {why}")
            return result
        match = next(
            (
                k
                for k in gh.deploy_keys(org, slug)
                if k.get("key", "").split()[:2] == pub.split()[:2]
            ),
            None,
        )
        if match is not None and not match.get("read_only", False):
            result.key = "present"
        else:
            if match is not None:  # registered without write access: replace it (codex on #101)
                gh.delete_deploy_key(org, slug, match["id"])
            gh.add_deploy_key(org, slug, f"zab ws-{slug}", pub)
            result.key = "replaced (was read-only)" if match is not None else "added"
        res = ssh(as_agent(shlex.join([ZAB, "setup-repo", f"{org}/{slug}", "--yes"])), 900)
        verdict = [line for line in res.stdout.splitlines() if line.startswith("write access ")]
        result.checkout = first_line(verdict[-1] if verdict else "") or (
            "ok" if res.ok else f"exit {res.exit_code}"
        )
        if not res.ok:
            why = first_line(res.stderr) or result.checkout
            result.failures.append(f"setup-repo: {why}")
            return result

    if wanted is not None:
        previous = (row.get("github") or "").strip()
        try:
            result.invite, result.login = grant_access(
                gh,
                org,
                slug,
                wanted,
                previous,
                dry_run=opts.dry_run,
                repo_exists=result.repo_action != "would create",
                no_rebind=opts.no_rebind,
            )
        except GitHubError as exc:  # the repository is ready either way: report, do not fail
            result.invite, result.login = f"failed: {exc}", previous
    return result


def _invitations_for(gh: GitHub, org: str, name: str, login: str) -> list[dict[str, Any]]:
    return [
        inv
        for inv in gh.invitations(org, name)
        if str((inv.get("invitee") or {}).get("login") or "").casefold() == login.casefold()
    ]


#: The `github_invite` state of a row whose previous login keeps its access (`--no-rebind`).
REFUSED = "login changed: refused"


def keep_access(gh: GitHub, org: str, name: str, previous: str, *, dry_run: bool) -> str:
    """`--no-rebind` (docs/ADMIN.md §2.4): never remove a login that has access. "" when
    `previous` has none, once its pending invitations are withdrawn (not in a dry run); else the
    refusal to record. `remove_collaborator` is never called: the permission is checked again
    after the withdrawal, so an invitation accepted meanwhile is reported, never removed."""

    def refusal() -> str:
        permission = gh.permission(org, name, previous)
        if permission == "none":
            return ""
        return f"{REFUSED} ({previous} has {permission} access; a rebind is done by hand)"

    if (refused := refusal()) or dry_run:
        return refused
    for inv in _invitations_for(gh, org, name, previous):
        try:
            gh.delete_invitation(org, name, inv["id"])
        except GitHubError as exc:
            if exc.status != 404:  # 404: accepted meanwhile, which the check below reports
                raise
    return refusal()  # checked again: an acceptance that raced the withdrawal


def grant_access(
    gh: GitHub,
    org: str,
    name: str,
    wanted: Wanted,
    previous: str,
    *,
    dry_run: bool,
    repo_exists: bool = True,
    no_rebind: bool = False,
    writes: list[str] | None = None,
) -> tuple[str, str | None]:
    """Give `wanted.login` admin of `org/name` (#714); returns the `github_invite` state and the
    `github` to record (None: leave it alone). `writes`, if given, gets each kind of write to
    GitHub that the call makes, or in a dry run would make (`withdraw`, `invite`, `raise`): the
    record `--invites-only` reads to tell a run with nothing to change (#708), never the state's
    words.

    A login that differs from `previous` (the roster's `github` from the last run) first takes
    the previous login's access away: its open invitation is withdrawn and its collaborator
    access removed, because the participant now says that account is not theirs. With
    `no_rebind`, only an invitation is withdrawn: a previous login with access keeps it, and the
    row is refused (`keep_access`). The new login
    is then looked up (a 404 is reported), and invited unless it is already an admin (an org
    owner is too) or has an open invitation; an expired invitation is replaced. A collaborator
    with less than admin is raised to it, and so is an open invitation (`; raised to admin`, or
    `; would raise to admin` in a dry run). Every step is idempotent, so a run that fails
    half-way is simply repeated.
    """
    if not wanted.login:
        return wanted.state, None
    login = wanted.login
    removed = ""
    if previous and not _LOGIN_RE.match(previous) and no_rebind:
        # `@OldGH` or a profile URL, hand-edited: OldGH's access can't be checked, so the row is
        # refused rather than treated as a first login (reviewer on #320)
        return f"{REFUSED} (the roster's github {previous[:40]!r} is not a login; fix it)", None
    if previous and not _LOGIN_RE.match(previous):
        # a hand-edited roster value that no GitHub account can have (PM nit on #142): it is
        # never sent to the API (it would land in a URL path), and no account can hold access
        # under it, so there is nothing to withdraw
        removed = f"; ignored the roster's invalid login {previous[:40]!r}"
        previous = ""
    acts = writes if writes is not None else []
    if previous and previous.casefold() != login.casefold() and repo_exists:
        if no_rebind:
            if refused := keep_access(gh, org, name, previous, dry_run=dry_run):
                return refused, None
            acts.append("withdraw")
            removed = f"; {'would remove' if dry_run else 'removed'} {previous}"
        elif dry_run:
            acts.append("withdraw")
            removed = f"; would remove {previous}"
        else:
            acts.append("withdraw")
            for inv in _invitations_for(gh, org, name, previous):
                gh.delete_invitation(org, name, inv["id"])
            gh.remove_collaborator(org, name, previous)
            removed = f"; removed {previous}"
    user = gh.user(login)
    if user is None:
        return f"unknown login{removed}", login
    login = str(user["login"])  # GitHub's own capitals
    if not repo_exists:
        acts.append("invite")
        return f"would invite {login}{removed}", login
    permission = gh.permission(org, name, login)
    if permission == "admin":  # only admin is done (#714)
        return f"collaborator{removed}", login
    if permission != "none":  # a collaborator with less: the PUT raises it
        acts.append("raise")  # a write to GitHub: never "nothing to change" (#708)
        if dry_run:
            return f"collaborator; would raise to admin{removed}", login
        invited = gh.add_collaborator(org, name, login)
        return ("invited" if invited else f"collaborator; {RAISED}") + removed, login
    open_invitations = _invitations_for(gh, org, name, login)
    if live := [inv for inv in open_invitations if not inv.get("expired")]:
        low = [inv for inv in live if inv.get("permissions") != "admin"]
        if low:
            acts.append("raise")  # a write to GitHub: never "nothing to change" (#708)
        if low and dry_run:
            return f"pending; would raise to admin{removed}", login
        for inv in low:
            gh.raise_invitation(org, name, inv["id"])
        return f"pending{'; ' + RAISED if low else ''}{removed}", login
    acts.append("invite")
    if dry_run:
        return f"would invite {login}{removed}", login
    for inv in open_invitations:  # expired: GitHub does not resend those
        gh.delete_invitation(org, name, inv["id"])
    invited = gh.add_collaborator(org, name, login)
    return ("invited" if invited else "collaborator") + removed, login


# --------------------------------------------------------------------------- --invites-only


#: `github_invite` states that mean the same thing from one run to the next: an invitation sent
#: last run is still open this run. A `; removed …` note belongs to the run that removed.
_SAME_STATE = {"invited": "pending"}
#: The longest `github_invite` the roster records (`InviteResult.stored`).
STATE_CHARS = 200
#: The notes `grant_access` appends (`; raised to admin` or `; would raise to admin` (#714), then
#: `; removed <login>`, `; would remove <login>`, `; ignored the roster's invalid login …`).
#: Only those notes are set aside: a failure's own text may contain semicolons, and two different
#: failures must not compare equal (PM nit on #166).
_RUN_NOTE = re.compile(
    r"; (?:(?:raised|would raise) to admin.*|(?:removed|would remove|ignored) .*)\Z", re.DOTALL
)
#: The note of a collaborator or an invitation raised to admin in this run (#714).
RAISED = "raised to admin"


def _same_state(state: str) -> str:
    base = _RUN_NOTE.sub("", state).strip()
    return _SAME_STATE.get(base, base)


@dataclass(slots=True)
class InviteResult:
    slug: str
    old: tuple[str, str]  # (`github`, `github_invite`) before the run
    login: str | None = None  # the `github` to record (None: leave it)
    state: str = ""  # the new `github_invite`
    note: str = ""  # not provisioned | left alone (then nothing else is set)
    writes: list[str] = field(default_factory=list)  # `grant_access`'s, made or, dry, to make

    @property
    def failed(self) -> bool:
        return self.state.startswith("failed")

    @property
    def stored(self) -> str:
        """The state as the roster records it (`run_invites`): compared as such, so that a long
        one reads as unchanged on the next run (#708)."""
        return self.state[:STATE_CHARS]

    @property
    def refused(self) -> bool:  # shown on every run until a deployer settles it
        return self.state.startswith(REFUSED)

    @property
    def changed(self) -> bool:
        if self.note:
            return False
        old_login, old_state = self.old
        login = old_login if self.login is None else self.login
        same = _same_state(old_state) == _same_state(self.stored)  # as the roster has it
        # `invited`, `removed` and `raised` only ever come from a write in this run (an
        # invitation still open reads `pending`), e.g. an expired invitation sent again; `would
        # raise` from a dry run's (#714)
        acted = self.state.startswith("invited") or "withdraw" in self.writes
        acted = acted or "raise" in self.writes  # never the state's words (reviewer on #738)
        return login != old_login or not same or acted


def invite_row(row: Mapping[str, str], opts: Options, gh: GitHub, wanted: Wanted) -> InviteResult:
    """Step 5 alone for one row (`--invites-only`): never SSH, never a repository write."""
    slug = identity(row)
    result = InviteResult(
        slug=slug, old=((row.get("github") or "").strip(), (row.get("github_invite") or ""))
    )
    if foreign_repo(row.get("repo", ""), opts.org, slug) and not opts.force:
        result.note = "left alone"
        return result
    if (row.get("repo_status") or "").strip() != "ok" or not _SLUG_RE.match(slug):
        result.note = "not provisioned"
        return result
    if not wanted.login:  # nothing to ask GitHub
        result.state = wanted.state
        return result
    previous = result.old[0]
    try:
        repo = gh.repo(opts.org, slug)
        if repo is None:
            result.state = f"failed: {opts.org}/{slug} does not exist"
            result.login = previous
            return result
        if repo.get("private") is not True:  # as the full run: never open a public repository
            result.state = f"failed: {opts.org}/{slug} is not private: nothing done"
            result.login = previous
            return result
        result.state, result.login = grant_access(
            gh, opts.org, slug, wanted, previous, dry_run=opts.dry_run, no_rebind=opts.no_rebind,
            writes=result.writes,
        )  # fmt: skip
    except GitHubError as exc:
        result.state, result.login = f"failed: {exc}", previous
    return result


def run_invites(
    rows: list[dict[str, str]],
    opts: Options,
    gh: GitHub,
    *,
    zulip_logins: Mapping[str, str] | ZulipView,
    every: list[dict[str, str]],
    parallel: int = 4,
) -> list[InviteResult]:
    """`--invites-only` over `rows`; records the results in the rows unless a dry run.

    Every row with a login is checked against GitHub on every run (its account, then its access
    and invitations): three or four cheap reads per row, so an accepted invitation, an expired
    one or a collaborator removed by hand is noticed on the next run, not only on a full one."""
    wanted = wanted_logins(rows, every, zulip_logins)
    jobs = list(zip(rows, wanted, strict=True))

    def on_error(job: tuple[dict[str, str], Wanted], exc: BaseException) -> InviteResult:
        row = job[0]
        result = InviteResult(
            slug=identity(row),
            old=((row.get("github") or "").strip(), row.get("github_invite") or ""),
        )
        result.state = redact(f"failed: {type(exc).__name__}: {exc}")
        return result

    results = run_parallel(
        jobs, lambda job: invite_row(job[0], opts, gh, job[1]), parallel=parallel, on_error=on_error
    )
    if not opts.dry_run:
        for row, result in zip(rows, results, strict=True):
            if result.note or not result.changed:
                continue
            if result.login is not None:
                row["github"] = result.login
            row["github_invite"] = result.stored
    return results


def nothing_to_change(results: list[InviteResult]) -> bool:
    """Whether `--invites-only` would change nothing (#708): no write to GitHub, an invitation
    to send, withdraw or raise to admin (`writes`), no acceptance or other state to record, so
    no roster write (`changed`), and no failure. A row refused, never provisioned or left alone
    changes nothing: the summary line still names it."""
    return not any(r.failed or r.changed or r.writes for r in results)


def invites_report(results: list[InviteResult], *, dry_run: bool = False) -> tuple[str, str]:
    """(the table of changed, failed or refused rows, or "", the one summary line)."""
    shown = [r for r in results if r.changed or r.failed or r.refused]
    body = (
        format_table(
            ["slug", "github", "invite"],
            [[r.slug, r.login if r.login is not None else r.old[0], r.state] for r in shown],
        )
        if shown
        else ""
    )
    states = [r.state for r in shown]
    # what this run did, or would do, from its writes and its own words at the state's start,
    # never words a participant typed into the field, which a state may quote (reviewer on #738)
    invited = sum(st.startswith(("invited", "would invite")) for st in states)
    accepted = sum(
        st.startswith("collaborator") and not r.old[1].startswith("collaborator")
        for st, r in zip(states, shown, strict=True)
    )
    removed = sum("withdraw" in r.writes for r in shown)
    raised = sum("raise" in r.writes for r in shown)
    failed = sum(r.failed for r in results)
    unprovisioned = sorted(r.slug for r in results if r.note == "not provisioned")
    alone = sum(r.note == "left alone" for r in results)
    unchanged = sum(not (r.changed or r.failed or r.refused or r.note) for r in results)
    line = (
        f"invites: {invited} invited, "
        + (f"{raised} raised to admin, " if raised else "")
        + f"{accepted} accepted, {removed} removed, {failed} failed; {unchanged} unchanged"
    )
    if refused := sorted(r.slug for r in results if r.refused):
        line += f"; {len(refused)} login changes refused ({', '.join(refused)})"
    if unprovisioned:
        line += (
            f"; {len(unprovisioned)} not provisioned yet ({', '.join(unprovisioned)}): "
            "run provision_repos.py without --invites-only"
        )
    if alone:
        line += f"; {alone} left alone (another repository)"
    return body, line + (" (dry run)" if dry_run else "")


# ---------------------------------------------------------------------------------------- main


def select(rows: list[dict[str, str]], only: list[str] | None) -> list[dict[str, str]]:
    """The participant rows (a `slug`, or a `github` login in a roster without slugs),
    narrowed by `--only NAME`."""
    chosen = [r for r in rows if identity(r) and not r.get("spare")]
    # Two rows with one identity would give two VMs write access to one repository (codex on
    # #96): refuse the whole roster, before --only and before anything remote. An explicit slug
    # colliding with another row's fallback github login counts too.
    seen: dict[str, str] = {}
    for row in chosen:
        key = identity(row).casefold()
        if key in seen:
            raise ValueError(
                f"duplicate repository name {identity(row)!r} (rows {seen[key]!r} and "
                f"{row.get('email') or row.get('github') or row.get('slug')!r}): give each a slug"
            )
        seen[key] = row.get("email") or row.get("github") or row.get("slug") or "?"
    if not only:
        return chosen
    wanted = {o.casefold() for o in only}
    known = {identity(r).casefold() for r in chosen}
    if missing := sorted(wanted - known):
        raise ValueError(f"not in the roster: {', '.join(missing)}")
    return [r for r in chosen if identity(r).casefold() in wanted]


def run(
    rows: list[dict[str, str]],
    opts: Options,
    gh: GitHub,
    token: str,
    runner: Runner,
    *,
    parallel: int = 4,
    zulip_logins: Mapping[str, str] | ZulipView | None = None,
    every: list[dict[str, str]] | None = None,
) -> list[RowResult]:
    """Provision `rows` (`every` is the whole roster, for logins claimed by rows not in this
    run); `zulip_logins` (`zulip_github_logins`) is needed unless invitations are off."""
    if opts.invite:
        wanted: list[Wanted | None] = list(
            wanted_logins(rows, every if every is not None else rows, zulip_logins or {})
        )
    else:
        wanted = [None] * len(rows)
    jobs = list(zip(rows, wanted, strict=True))

    def on_error(job: tuple[dict[str, str], Wanted | None], exc: BaseException) -> RowResult:
        row = job[0]
        result = RowResult(slug=row.get("slug", "?"), repo=f"{opts.org}/{row.get('slug', '?')}")
        result.failures.append(redact(f"{type(exc).__name__}: {exc}"))
        return result

    results = run_parallel(
        jobs,
        lambda job: provision_row(job[0], opts, gh, token, runner, job[1]),
        parallel=parallel,
        on_error=on_error,
    )
    if not opts.dry_run:
        for row, result in zip(rows, results, strict=True):
            if result.skipped:
                continue  # left alone, roster included
            if result.repo_action in ("created", "exists"):
                row["repo"] = result.repo
            row["repo_status"] = "ok" if result.ok else result.failures[0][:200]
            if result.login is not None:
                row["github"] = result.login
            if result.invite:
                row["github_invite"] = result.invite[:STATE_CHARS]
    return results


def table(results: list[RowResult]) -> str:
    headers = ["slug", "host", "repo", "template", "deploy key", "checkout", "invite", "status"]
    body = [
        [
            r.slug,
            r.host,
            f"{r.repo} ({r.repo_action})" if r.repo_action else r.repo,
            r.seed,
            r.key,
            r.checkout,
            r.invite,
            ("skipped: " + r.skipped)
            if r.skipped
            else ("ok" if r.ok else "FAILED: " + "; ".join(r.failures)),
        ]
        for r in results
    ]
    return format_table(headers, body)


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = subprocess_runner,
    make_github: Callable[[str], GitHub] = GitHub,
    make_zulip: Callable[[str], Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--roster", required=True)
    parser.add_argument("--org", default=ORG)
    parser.add_argument("--only", action="append", metavar="SLUG", help="repeatable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--zuliprc",
        help="the organizer's Zulip zuliprc, to read each participant's GitHub profile field "
        "(needed unless --no-invite)",
    )
    parser.add_argument("--no-invite", action="store_true", help="skip the repository invitations")
    parser.add_argument(
        "--invites-only",
        action="store_true",
        help="only reconcile the invitations of rows already provisioned (no SSH); quiet unless "
        "something changed",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"also provision rows whose repo names another repository (they then get "
        f"{ORG}/<slug>)",
    )
    parser.add_argument(
        "--no-rebind",
        action="store_true",
        help="never remove a previous login that has access (only its pending invitation is "
        "withdrawn); such a row is reported (docs/ADMIN.md §2.4)",
    )
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    parser.add_argument("--template", default=str(TEMPLATE_DIR))
    args = parser.parse_args(argv)
    if not _SLUG_RE.match(args.org):
        print(f"invalid organization {args.org!r}", file=sys.stderr)
        return 2
    if args.invites_only and args.no_invite:
        print("--invites-only and --no-invite contradict each other", file=sys.stderr)
        return 2
    try:
        rows = load_roster(args.roster)
        before = column_snapshot(rows, OWN_COLUMNS)  # what this run's write-back is relative to
        for row in rows:
            recorded_id(row)  # an id that isn't ASCII digits (a hand edit) refuses (#327)
        chosen = select(rows, args.only)
        token = github_token(env, runner)
    except (OSError, ValueError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 2
    if not chosen:
        print("no participant rows (slug or github) in the roster", file=sys.stderr)
        return 2
    zulip_logins: ZulipView = ZulipView()
    if not args.no_invite:
        if not args.zuliprc:
            print("--zuliprc is needed for the invitations (or pass --no-invite)", file=sys.stderr)
            return 2
        try:
            admin = (make_zulip or _zulip_admin)(args.zuliprc)
            try:
                zulip_logins = zulip_github_logins(admin)
            finally:
                getattr(admin, "close", lambda: None)()
        except Exception as exc:  # one message, never a traceback with a key
            print(f"Zulip: {redact(str(exc))}", file=sys.stderr)
            return 2
    opts = Options(
        org=args.org,
        dry_run=args.dry_run,
        invite=not args.no_invite,
        known_hosts=args.known_hosts,
        template=pathlib.Path(args.template),
        force=args.force,
        no_rebind=args.no_rebind,
    )
    if args.invites_only:
        invites = run_invites(
            chosen,
            opts,
            make_github(token),
            zulip_logins=zulip_logins,
            every=rows,
            parallel=args.parallel,
        )
        saved = args.dry_run or save_roster(args.roster, rows, before)  # only what changed
        body, line = invites_report(invites, dry_run=args.dry_run)
        if body:
            print(body)
        print(line)
        if any(r.failed for r in invites) or not saved:
            return 1
        if args.dry_run and nothing_to_change(invites):
            print(UNCHANGED_LINE)
            return NOTHING
        return 0
    results = run(
        chosen,
        opts,
        make_github(token),
        token,
        runner,
        parallel=args.parallel,
        zulip_logins=zulip_logins,
        every=rows,
    )
    saved = args.dry_run or save_roster(args.roster, rows, before)
    print(table(results))
    failed = [r.slug for r in results if not r.ok]
    if failed:
        print(f"\n{len(failed)} of {len(results)} failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    if not saved:
        return 1
    skipped = [r.slug for r in results if r.skipped]
    ready = len(results) - len(skipped)
    tail = f", {len(skipped)} left alone ({', '.join(skipped)})" if skipped else ""
    if refused := [r.slug for r in results if r.invite.startswith(REFUSED)]:
        tail += f"; {len(refused)} login changes refused ({', '.join(refused)})"
    print(f"\n{ready} of {len(results)} ready{tail}" + (" (dry run)" if args.dry_run else ""))
    return 0


#: The roster columns this script writes (README.md §4): only their changes are written back.
OWN_COLUMNS = ("repo", "repo_status", "github", "github_invite")


def save_roster(path: str, rows: list[dict[str, str]], before: dict[str, dict[str, str]]) -> bool:
    """Write back only the columns this run changed, into the roster as it is now
    (`merge_roster_changes`): a `zulip_provision --only` run or a spare hand-over edit made
    meanwhile survives (PM on issue #36, Mon 20:15). False, with "rerun", when the roster no
    longer reads (PM nit, Mon 22:30)."""
    return save_roster_changes(path, rows, before, script="provision_repos.py")


def _zulip_admin(path: str) -> ZulipAdmin:
    site, email, key = load_zuliprc(path)
    return ZulipAdmin(site, email, key)


if __name__ == "__main__":
    raise SystemExit(main())
