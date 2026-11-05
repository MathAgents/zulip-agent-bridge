"""The rig's Zulip side (docs/ADMIN.md §7; issue #356): the admin bots and their channels.

All of it is made as `zab-tester`, and reused when it exists (idempotent):

- the generic bots `Workshop admin · e2e`, `Workshop admin · e2e2` and `Workshop admin · test`,
  owned by `zab-tester`. `GET /bots` gives an owner its bots' keys, so a second run writes the
  same zuliprcs; a new bot comes from `POST /bots` (`zulip_provision.py`'s endpoints);
- the private channels `#zab-dev-organizers` (`zab-tester`, `zab-tester2`, the e2e bot),
  `#zab-dev-organizers-2` (`zab-tester2`, the organizer of e2e2; `zab-tester`, a global admin;
  the e2e2 bot) and `#zab-dev-admins` (`zab-tester` and the global bot). Missing subscribers are
  added; nobody is ever removed (each bot warns about strangers itself, §2.1). Only `zab-dev*`
  channels;
- `#zab-dev-participants-2`, e2e2's participants' channel (`channel`, #627): `zab-tester`, the
  test provisioner, which `!roster sync` reads it with and leaves out, and `zab-tester2` (A22);
- each workshop's alerts channel (`alerts_channel`, which `!waker on` needs: §3, action 5),
  private, as `!new-workshop` makes it (§2.9): the workshop's organizers, the global admins
  and the test provisioner, all of them `zab-tester` but e2e2's organizer `zab-tester2`, so
  `#zab-dev-alerts` (`zab-tester`) and `#zab-dev-alerts-2` (both). A participant bot may post
  the waker's failed wakes there only if it or its owner is subscribed: its owner is
  `zab-tester`, the provisioner, or, for A18's bots until they move, `zab-tester2`. No bot takes
  commands there.

The bots' zuliprcs go to the rig's `secrets/` (0600, in 0700 directories), which `deploy.sh
--bootstrap --secrets` installs on the server, with the provisioner's zuliprc and the GitHub
token. Both are still open with Drew (Q9), so each is a well-formed random placeholder until its
input is in the rig's directory (`provisioner.zuliprc`, which the operator of the rig places after
Drew's yes to `zab-tester`'s member key, §7; `github-token`); a later setup then installs it on
the server (`setup._sync_secrets`). The bots are matched by full name and by their owner,
`zab-tester`. No key is ever printed.
"""

from __future__ import annotations

import configparser
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from e2e.admin.harness.gate import Gate, shown
from e2e.admin.harness.rig import (
    ADMINS_CHANNEL,
    E2E,
    E2E2,
    GLOBAL_BOT,
    GLOBAL_SHORT,
    WORKSHOPS,
    AdminRig,
)
from e2e.harness.secret_files import label_of, read_secret
from zab.zulip.client import ZulipError

#: A placeholder GitHub token starts so: A5's repositories are skipped while it is one.
PLACEHOLDER = "zab-e2e-no-github-token-"
#: A placeholder provisioner zuliprc has this e-mail: provisioning is skipped while it is one.
PLACEHOLDER_EMAIL = "zab-e2e-placeholder@example.invalid"


@dataclass
class ZulipIds:
    """What admin.toml and the scenarios need: user IDs, the bots, the channels."""

    tester: int
    tester2: int
    site: str
    bots: dict[str, tuple[int, str]] = field(default_factory=dict)  # name → (user ID, e-mail)
    channels: dict[str, int] = field(default_factory=dict)  # name → stream ID


def bot_specs() -> list[tuple[str, str, str]]:
    """(full name, short name, zuliprc under secrets/) of each admin bot."""
    specs = [(ws.bot, ws.short, f"bots/{ws.name}.zuliprc") for ws in WORKSHOPS]
    return [*specs, (GLOBAL_BOT, GLOBAL_SHORT, "global.zuliprc")]


def zuliprc_text(site: str, email: str, key: str) -> str:
    return f"[api]\nemail={email}\nkey={key}\nsite={site}\n"


def private_dir(path: Path) -> None:
    """`path` a directory, 0700 even if it was there before (`mkdir`'s mode misses parents)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def private_file(path: Path, text: str) -> None:
    """`text` at `path`, 0600, in a 0700 directory, written so no other mode is ever seen."""
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class ZulipRig:
    def __init__(self, gate: Gate, tester: Any, tester2: Any, rig: AdminRig) -> None:
        self.gate, self.tester, self.tester2, self.rig = gate, tester, tester2, rig
        #: `zab-tester`'s addresses (`users/me`): a placed provisioner key must be one of them.
        #: Kept here, never in `ZulipIds` (which goes to the run's state file).
        self.tester_emails: frozenset[str] = frozenset()

    async def get(self, client: Any, endpoint: str, **params: Any) -> dict[str, Any]:
        self.gate.read(f"zulip GET {endpoint} {shown(params)}".rstrip())
        return await client.request("GET", endpoint, **params)

    async def own_bot(self, email: str, owner: int) -> dict[str, Any] | None:
        """The user `email`, if it is an active bot of `owner`'s, else None. Looked up one by
        one (`GET /users/{email}`): the bulk `GET /users` can leave a bot out (it omitted a new
        one on the first live run)."""
        try:
            user = (await self.get(self.tester, f"users/{quote(email, safe='@')}"))["user"]
        except ZulipError:
            return None
        ok = user.get("is_active") is True and user.get("is_bot") is True
        return user if ok and user.get("bot_owner_id") == owner else None

    async def post(self, endpoint: str, dry: dict[str, Any], **params: Any) -> dict[str, Any]:
        if not self.gate.write(f"zulip POST {endpoint} {shown(params)}".rstrip()):
            return dry
        return await self.tester.request("POST", endpoint, **params)

    async def ensure(self) -> ZulipIds:
        names = (E2E.channel, E2E2.channel, ADMINS_CHANNEL, E2E.alerts, E2E2.alerts,
                 E2E2.participants)  # fmt: skip
        if any(not name.startswith("zab-dev") for name in names):  # the dev org's zab-dev* only
            raise ValueError(f"{names}: the rig's channels are zab-dev* only")
        me = await self.get(self.tester, "users/me")
        self.tester_emails = frozenset(
            str(me[k]).lower() for k in ("email", "delivery_email") if me.get(k)
        )
        me2 = await self.get(self.tester2, "users/me")
        ids = ZulipIds(int(me["user_id"]), int(me2["user_id"]), self.tester.site)
        owned = (await self.get(self.tester, "bots"))["bots"]
        for full, short, rel in bot_specs():
            named = [b for b in owned if b.get("full_name") == full]
            found = [(b, user) for b in named
                     if (user := await self.own_bot(b["username"], ids.tester))]  # fmt: skip
            if named and not found:
                raise LookupError(
                    f"{full}: a bot of that name not owned by zab-tester, or inactive"
                )
            if found:
                email, key = found[0][0]["username"], found[0][0]["api_key"]
                uid = int(found[0][1]["user_id"])
            else:
                made = await self.post(
                    "bots", {"user_id": 0, "api_key": ""}, full_name=full, short_name=short,
                    bot_type=1,
                )  # fmt: skip
                uid, key = int(made["user_id"]), made["api_key"]
                email = f"<{short}-bot e-mail>"
                if uid:
                    email = (await self.get(self.tester, f"users/{uid}"))["user"]["email"]
            ids.bots[full] = (uid, email)
            if self.gate.write(f"local write {self.rig.secrets / rel} (the bot's zuliprc)"):
                private_file(self.rig.secrets / rel, zuliprc_text(ids.site, email, key))
        await self._channels(ids)
        return ids

    async def _channels(self, ids: ZulipIds) -> None:
        want = {
            E2E.channel: {ids.tester, ids.tester2, ids.bots[E2E.bot][0]},
            E2E2.channel: {ids.tester, ids.tester2, ids.bots[E2E2.bot][0]},
            ADMINS_CHANNEL: {ids.tester, ids.bots[GLOBAL_BOT][0]},
            E2E.alerts: {ids.tester},
            E2E2.alerts: {ids.tester, ids.tester2},
            # A22 (#627): its subscribers are e2e2's roster: zab-tester, the provisioner, whom
            # the sync leaves out, and zab-tester2, whom A22 unsubscribes and puts back
            E2E2.participants: {ids.tester, ids.tester2},
        }
        subs = (await self.get(self.tester, "users/me/subscriptions"))["subscriptions"]
        have = {s["name"]: int(s["stream_id"]) for s in subs}
        for name, people in want.items():
            if name in have:
                sid = have[name]
                members = await self.get(self.tester, f"streams/{sid}/members")
                missing = sorted(people - set(members["subscribers"]) - {0})
                if missing:
                    await self.post("users/me/subscriptions", {}, principals=missing,
                                    subscriptions=[{"name": name}])  # fmt: skip
                ids.channels[name] = sid
                continue
            await self.post(
                "users/me/subscriptions", {}, subscriptions=[{"name": name,
                "description": "zab admin e2e (#356): test only"}], invite_only=True,
                history_public_to_subscribers=True, principals=sorted(people - {0}),
            )  # fmt: skip
            ids.channels[name] = 0
            if not self.gate.dry_run:
                found = await self.get(self.tester, "get_stream_id", stream=name)
                ids.channels[name] = int(found["stream_id"])


def stage_secrets(
    gate: Gate, rig: AdminRig, site: str, tester_emails: frozenset[str]
) -> tuple[bool, bool]:
    """The secrets deploy.sh installs at a bootstrap, besides the bots' zuliprcs: the GitHub
    token and the provisioner's zuliprc. Each is the rig's input when there is one (Q9: Drew's
    PAT; `zab-tester`'s key once Drew agrees), else a well-formed placeholder, random so that no
    scrubber matches it by chance. A placed provisioner zuliprc must be `zab-tester`'s own (one
    of `tester_emails`): no other account's key, a production one above all, ever reaches the
    test server. (token is real, provisioner is real)."""
    given, base = rig.provisioner_zuliprc, rig.dir
    if given.is_file() and zuliprc_email(given, base) not in tester_emails:
        raise ValueError(f"{given} is not zab-tester's zuliprc: refused, nothing staged")
    if given.is_file() and zuliprc_site(given, base) != site.rstrip("/"):
        raise ValueError(f"{given} is for another realm than {site}: refused, nothing staged")
    token = _stage(gate, rig.github_token, rig.secrets / "github-token",
                   lambda: PLACEHOLDER + secrets.token_hex(16) + "\n",
                   lambda text: text.startswith(PLACEHOLDER), base)  # fmt: skip
    provisioner = _stage(gate, rig.provisioner_zuliprc, rig.secrets / "provisioner.zuliprc",
                         lambda: zuliprc_text(site, PLACEHOLDER_EMAIL, secrets.token_hex(16)),
                         lambda text: PLACEHOLDER_EMAIL in text, base)  # fmt: skip
    return token, provisioner


def _text(path: Path, base: Path | None) -> str:
    """A secrets file's text: a failure names the file, never quotes it (#519)."""
    return read_secret(label_of(path, base), path.read_text, "utf-8")


def _value(path: Path, key: str, base: Path | None) -> str:
    """The `[api]` option `key` of the zuliprc `path` (`_api`), "" if none; a failure names
    the file under `base`, never quotes it (#519): configparser quotes the line."""
    return read_secret(label_of(path, base), _api, path, key)


def _api(path: Path, key: str) -> str:
    """Read and parsed as ZulipClient does (INI: `key = …` too), below `read_secret`, so that
    no frame left in a traceback holds the file's text (`secret_files`)."""
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(path.read_text(encoding="utf-8"))
    return config.get("api", key, fallback="").strip()


def zuliprc_email(path: Path, base: Path | None = None) -> str:
    """The `[api] email` of a zuliprc, as ZulipClient reads it, in lower case ("" if none)."""
    return _value(path, "email", base).lower()


def zuliprc_site(path: Path, base: Path | None = None) -> str:
    """The `[api] site` of a zuliprc, without a final slash ("" if none)."""
    return _value(path, "site", base).rstrip("/")


def _stage(
    gate: Gate, given: Path, staged: Path, placeholder: Any, is_placeholder: Any, base: Path
) -> bool:
    """`staged` as `given` when there is one, else a placeholder (kept from run to run: the
    server has it); True when it is the real one. Files are named under `base`, never quoted;
    `_staging` runs below `read_secret`, as its frame holds both files' text."""
    args = (gate, given, staged, placeholder, is_placeholder, base)
    return read_secret(label_of(staged, base), _staging, *args)


def _staging(
    gate: Gate, given: Path, staged: Path, placeholder: Any, is_placeholder: Any, base: Path
) -> bool:
    if given.is_file():
        text = _text(given, base)
        current = _text(staged, base) if staged.exists() else None
        if current != text and gate.write(f"local copy {given} -> {staged}"):
            private_file(staged, text)
        return True
    if staged.exists() and is_placeholder(_text(staged, base)):
        return False
    if gate.write(f"local write {staged} (a placeholder: no {given.name} given, Q9)"):
        private_file(staged, placeholder())
    return False


def secret_values(rig: AdminRig) -> Mapping[str, str]:
    """Label → value of every secret the rig gave the server: the bots' and the provisioner's
    keys and the GitHub token, for the sweep (A12). Never printed: only the labels are. A file
    that can't be read or parsed is a SecretFileError naming it under the rig's directory
    (`secrets/bots/….zuliprc`), never quoting it (#519). The values are gathered below
    `read_secret`, so that no frame left in its traceback holds those read before."""
    return read_secret(label_of(rig.secrets, rig.dir), _secret_values, rig)


def _secret_values(rig: AdminRig) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(rig.secrets.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".zuliprc":  # INI, as ZulipClient reads it: `key = …` too (Codex)
            value = _value(path, "key", rig.dir)
        else:
            value = _text(path, rig.dir).strip()
        if value:
            found[str(path.relative_to(rig.secrets))] = value
    return found
