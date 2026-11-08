"""The root helper `zab-admin-config`: a new workshop's entry into admin.toml (docs/ADMIN.md §2.9,
§1.2, §1.3; #418, #332).

The bot runs as `zab-admin`, which can write neither `/etc/zab-admin/admin.toml` nor a key under
`secrets/`. So `!new-workshop`'s apply writes a request, and `zab-admin-config.path` starts this,
as root, from the running release: `zab-admin-config.service` runs `main` with the system's
`/usr/bin/python3 -I -B -S`, never the venv's interpreter, so no site processing, and with
`sys.path` the standard library and the release's `src/` alone (ADMIN §1.6, #437). Keep its
imports to `checks`, `config` and the standard library: root runs whatever they import
(`tests/admin/test_admin_imports.py`, `HELPER_IMPORTS`).

**The request,** `/var/lib/zab-admin-config/requests/request.json`, a JSON object written by
`zab-admin` and renamed into place:

    {"version": 1, "plan": "P-42", "entry": {…}, "zuliprc": "[api]\\nemail=…\\nkey=…\\nsite=…\\n"}

`entry` is one `[[workshop]]` table as JSON, with `bot` and `aws` as its two subtables; `zuliprc`
is the new workshop bot's key. `requests/` is `zab-admin`'s, inside the helper's root-owned
state, so the bot can't swap the directory for another. The helper claims the request by a
rename: its writer withdraws an unanswered one the same way, and exactly one of the two renames
finds it. Then it deletes it, whatever it holds (a link, a FIFO, an empty directory), so the key
never lingers there.

**The order** (#332). Every check comes before any write:
1. the request's shape, and the key's (`[api]` with `email`, `key` and `site` alone);
2. a brand-new workshop: the name is in no entry of admin.toml, and no key file of that name
   exists; its bot's key is `secrets/bots/<name>.zuliprc`, the file the helper writes;
3. the whole new admin.toml through the strict loader (`zab.admin.config`), and the new file
   equals the old one plus exactly this entry: `admins`, `deployers`, `[global]`, every other
   key and every other workshop unchanged. The entry is rendered here, from JSON values, so the
   request supplies no TOML text;
4. the entry's break-glass keys pass the bots' own file checks (`checks.check_file`): without
   them no bot would start.

Then the writes: the key, `root:zab-admin` 0640, with `O_EXCL`, never over a file; the new
admin.toml beside the old one, and a hard link to the old one; the record of the change, a line
of `changes.jsonl` in the helper's own state directory (root's: the bot can neither change nor
remove it); then two renames, the link to `admin.toml.bak` and the new file to `admin.toml`. A
failure before the renames removes the key and the new files, and writes no record. So a
refused request changes no file, a request naming an existing workshop included. A failed
rename leaves admin.toml as it was and removes the key and the new files too; only the record
stays, which the outcome says. Once the new
file is in place the change is made: a later failure (the directories' fsync) is a warning on
an applied outcome.

**The outcome** goes to `result.json` in the state directory (0644: it holds no secret),
whatever happened, an unexpected error included, which the request's plan reads, and to the
journal. The helper restarts nothing: the bot that asked
waits for this outcome, posts its result, then restarts itself to load the new entry (§2.9), and
the starter restarts itself whenever admin.toml changes (§1.2). A restart from here would cut
the bot's apply short.

**What it can't check.** The approvals live in the bot's `state.db`. A compromised `zab-admin`
can therefore add a workshop with organizers it picks (§2.9, "The helper's residual risk"): the
root-owned record of each change, and deploy.sh's diff of admin.toml against the deployer's copy,
are what show it.
"""

from __future__ import annotations

import argparse
import contextlib
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import sys
import time
import tomllib
from dataclasses import dataclass
from typing import Any

from zab.admin import checks
from zab.admin.config import (
    DEFAULT_PATH,
    ROOTS,
    AdminConfig,
    AdminConfigError,
    Roots,
    parse_admin_config,
)

USER = "zab-admin"  # who writes the request; the group that reads the key (§1.3)
REQUEST = "request.json"
MAX_REQUEST = 65536
_PLAN = re.compile(r"P-[1-9][0-9]{0,8}", re.ASCII)
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)  # the loader's, first
_KEY = re.compile(r"[A-Za-z0-9]{32}", re.ASCII)
_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9.-]{1,253}", re.ASCII)
_SITE = re.compile(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", re.ASCII)
_KEY_NAME = re.compile(r"[a-z_]{1,32}", re.ASCII)
#: The keys of an entry, in the order they are written; `bot` and `aws` are its subtables. No
#: `tag`: the scope is `workshop=<name>` and `[global]`'s (#425). The loader decides which are
#: required and what each may hold.
ENTRY = ("name", "github_org", "refserve_url", "roster", "out_dir", "organizers",
         "alerts_channel", "channel")  # fmt: skip
SUBTABLES = {
    "bot": ("zuliprc", "user_id", "email", "channels"),
    "aws": ("region", "ami", "security_group", "instance_types", "key_name", "admin_keys"),
}


class Refused(Exception):
    """The request is not applied, and no file changed; one line, with no key in it."""


@dataclass(frozen=True, slots=True)
class Paths:
    """Where the helper reads and writes (§1.3); tests pass others."""

    # zab-admin's 0700, in root's state: the bot can't swap it for another (review of #421)
    requests: str = "/var/lib/zab-admin-config/requests"
    config: str = str(DEFAULT_PATH)  # /etc/zab-admin/admin.toml
    state: str = "/var/lib/zab-admin-config"  # root 0755 (tmpfiles.conf)
    roots: Roots = ROOTS

    @property
    def keys(self) -> str:
        return f"{self.roots.secrets}/bots"


@dataclass(frozen=True, slots=True)
class Request:
    plan: str
    entry: dict[str, Any]
    zuliprc: str  # canonical: `[api]`, then email, key and site

    @property
    def name(self) -> str:
        return self.entry["name"]


def take(paths: Any, owner: int, name: str = REQUEST) -> bytes | None:
    """The request `name` in `paths.requests`: its bytes, None when there is none; the request
    is gone afterwards. Its directory and the file must be `owner`'s (`zab-admin`), the file a
    regular one with one link, neither reached through a symbolic link. The upgrade helpers take
    theirs the same way (`zab.admin.upgradehelper`, #569)."""
    try:
        folder = os.open(paths.requests, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Refused(f"{paths.requests}: {exc.strerror}") from None
    try:
        st = os.fstat(folder)
        if st.st_uid != owner or st.st_mode & 0o022:
            _remove(name, folder)  # unread, but gone: the path unit fires once
            raise Refused(f"{paths.requests} is not {USER}'s alone")
        # Claimed by a rename, never opened in place: its writer withdraws an unanswered request
        # the same way (new_workshop.py), and of two renames of one name exactly one succeeds,
        # so a request is either taken here, whole, or withdrawn, never both (review of #423).
        claimed = f"{name}.{os.getpid()}.taken"
        try:
            os.rename(name, claimed, src_dir_fd=folder, dst_dir_fd=folder)
        except FileNotFoundError:
            return None  # none, or withdrawn
        except OSError as exc:
            _remove(name, folder)
            raise Refused(f"{name}: {exc.strerror}") from None
        try:
            fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        except OSError as exc:  # a symbolic link, say: removed all the same
            _remove(claimed, folder)
            raise Refused(f"{name}: {exc.strerror}") from None
        try:
            st = os.fstat(fd)  # before the unlink: one link, its only name the claimed one
            _remove(claimed, folder)  # first: whatever follows, it doesn't linger
            if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or st.st_nlink != 1:
                raise Refused(f"{name} is not a regular file of {USER}'s")
            data = os.read(fd, MAX_REQUEST + 1)
        finally:
            os.close(fd)
    finally:
        os.close(folder)
    if len(data) > MAX_REQUEST:
        raise Refused(f"{name} is over {MAX_REQUEST} bytes")
    return data


def _remove(name: str, folder: int) -> None:
    """`name` gone from `folder`, whatever it is: a file, a link, a FIFO, or an empty directory
    (never a tree: a non-empty one stays, under its claimed name, and is refused; review of
    #421). Best effort."""
    try:
        os.unlink(name, dir_fd=folder)
    except IsADirectoryError:
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=folder)
    except OSError:
        pass


def parse(data: bytes) -> Request:
    """The request, checked for shape; its key in canonical form."""
    try:
        found = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # deep nesting too (#421's review)
        raise Refused("the request is not JSON") from None
    if not isinstance(found, dict) or set(found) != {"version", "plan", "entry", "zuliprc"}:
        raise Refused("the request has other keys than version, plan, entry and zuliprc")
    if type(found["version"]) is not int or found["version"] != 1:  # not `true`, not 1.0
        raise Refused("the request is not version 1, or names no plan")
    if not isinstance(found["plan"], str):
        raise Refused("the request is not version 1, or names no plan")
    if not _PLAN.fullmatch(found["plan"]):
        raise Refused("the request's plan is not P-<n>")
    entry, key = found["entry"], found["zuliprc"]
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise Refused("the request's entry is not a table with a name")
    if not _NAME.fullmatch(entry["name"]):  # it names a file before the loader reads it all
        raise Refused("the entry's name is not a workshop name")
    if not isinstance(key, str):
        raise Refused("the request's zuliprc is not text")
    return Request(found["plan"], entry, canonical_zuliprc(key))


def canonical_zuliprc(text: str) -> str:
    """`[api]` with `email`, `key` and `site`, each once, and nothing else: rewritten so."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines[:1] != ["[api]"]:
        raise Refused("the zuliprc does not start with [api]")
    values: dict[str, str] = {}
    for line in lines[1:]:
        name, equals, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not equals or name not in ("email", "key", "site") or name in values:
            raise Refused("the zuliprc holds other lines than email, key and site, once each")
        values[name] = value
    rules = {"email": _EMAIL, "key": _KEY, "site": _SITE}
    if set(values) != set(rules) or not all(rules[k].fullmatch(v) for k, v in values.items()):
        raise Refused("the zuliprc's email, key or site is missing or malformed")
    return "[api]\n" + "".join(f"{k}={values[k]}\n" for k in ("email", "key", "site"))


def _value(value: Any) -> str:
    """A TOML value: a string, an integer, or a list of them (JSON's forms are TOML's)."""
    if isinstance(value, list) and all(isinstance(v, str | int) for v in value):
        items = value
    elif isinstance(value, str | int):
        items = [value]
    else:
        raise Refused("an entry value is not a string, an integer or a list of them")
    if any(isinstance(v, bool) for v in items):
        raise Refused("an entry value is a boolean")
    return json.dumps(value, ensure_ascii=True)


def render(entry: dict[str, Any]) -> str:
    """The entry as one `[[workshop]]` table, its keys in `ENTRY`'s order, then `bot` and `aws`.
    A key outside them is refused, as the loader would; so is a subtable elsewhere."""
    known = set(ENTRY) | set(SUBTABLES)
    if not all(isinstance(k, str) and _KEY_NAME.fullmatch(k) for k in entry) or set(entry) - known:
        raise Refused("the entry has a key admin.toml doesn't know")
    lines = ["[[workshop]]"] + [f"{k} = {_value(entry[k])}" for k in ENTRY if k in entry]
    for table, keys in SUBTABLES.items():
        sub = entry.get(table)
        if not isinstance(sub, dict) or set(sub) - set(keys):
            raise Refused(f"the entry's {table} table is missing or has an unknown key")
        lines += [f"[workshop.{table}]"] + [f"{k} = {_value(sub[k])}" for k in keys if k in sub]
    return "\n".join(lines) + "\n"


def new_text(old: str, request: Request, paths: Paths) -> str:
    """admin.toml with the entry added, once every check of the module doc passes."""
    try:
        before = parse_admin_config(old, paths.config, roots=paths.roots)
    except AdminConfigError as exc:
        raise Refused(f"the current admin.toml is refused: {exc}") from None
    name = request.name
    if before.workshop(name) is not None:
        raise Refused(f"{name} is a workshop already: only a new one is added")
    key = f"{paths.keys}/{name}.zuliprc"
    bot = request.entry.get("bot")
    if not isinstance(bot, dict) or bot.get("zuliprc") != key:
        raise Refused(f"the new bot's key must be {key}")
    if bot.get("email") not in (None, _field(request.zuliprc, "email")):
        raise Refused("the entry's bot e-mail is not the zuliprc's")
    text = old + ("" if old.endswith("\n") or not old else "\n") + "\n" + render(request.entry)
    try:
        after = parse_admin_config(text, paths.config, roots=paths.roots)
    except AdminConfigError as exc:
        raise Refused(f"the new admin.toml is refused: {exc}") from None
    # The bots refuse to start without every break-glass key (checks.check_files), so an entry
    # naming a missing one would stop them all (review of #421): checked as they check it.
    modes = checks.Modes(
        owners=frozenset({0, os.geteuid()}), top=os.path.dirname(paths.roots.break_glass)
    )
    for problem in filter(None, (checks.check_file(k, modes) for k in request_keys(after, name))):
        raise Refused(f"a break-glass key of the entry: {problem}")
    was, now = tomllib.loads(old), tomllib.loads(text)
    rest = [ws for ws in now.get("workshop", []) if ws.get("name") != name]
    if (
        {k: v for k, v in was.items() if k != "workshop"}
        != {k: v for k, v in now.items() if k != "workshop"}
        or rest != was.get("workshop")
        or after.workshops[:-1] != before.workshops
        or after.workshops[-1].name != name
    ):
        raise Refused("the new admin.toml would change more than the new entry")
    return text


def request_keys(config: AdminConfig, name: str) -> tuple[str, ...]:
    """The break-glass key files the workshop `name` names."""
    found = config.workshop(name)
    return found.aws.admin_keys if found else ()


def _field(zuliprc: str, name: str) -> str:
    return next(line[len(name) + 1 :] for line in zuliprc.splitlines() if line.startswith(name))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_new(directory: str, name: str, data: bytes, mode: int, gid: int = 0) -> None:
    """A new file, never over one: `O_EXCL`, root-owned, `mode`, fsync'd. A failure after it was
    created removes it (Codex round 1 on #421: a key left half-written would refuse every retry);
    a file that was there before is never touched."""
    path = f"{directory}/{name}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        if gid:
            os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    finally:
        os.close(fd)


def _replace(path: str, data: bytes, mode: int) -> None:
    """`path`'s directory gets `<name>.new`, renamed over `<name>` (state files, root's)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path + ".new")
    folder, name = os.path.split(path)
    _write_new(folder, name + ".new", data, mode)
    os.replace(path + ".new", path)


def apply(
    request: Request, paths: Paths, key_group: int, now: float | None = None
) -> tuple[str, str]:
    """Add the entry (module doc): the new admin.toml's SHA-256, and a warning ("" for none)
    when something failed after the change was made."""
    with open(paths.config, encoding="utf-8") as f:  # root's own file: the loader reads it so
        old = f.read()
    text = new_text(old, request, paths)
    name = request.name
    key = f"{name}.zuliprc"
    if os.path.lexists(f"{paths.keys}/{key}"):
        raise Refused(f"{paths.keys}/{key} exists: a deployer removes it first")
    config_dir, config_name = os.path.split(paths.config)
    try:
        _write_new(paths.keys, key, request.zuliprc.encode("ascii"), 0o640, key_group)
    except OSError as exc:
        raise Refused(f"the key was not written: {exc.strerror}") from None
    new, bak = paths.config + ".new", paths.config + ".bak.new"
    try:  # everything that can fail, before the record and the two renames
        for leftover in (new, bak):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(leftover)
        _write_new(config_dir, config_name + ".new", text.encode("utf-8"), 0o644)
        os.link(paths.config, bak)  # the old file, kept as admin.toml.bak
        _record(paths, request, old, text, now)
    except OSError as exc:
        for leftover in (f"{paths.keys}/{key}", new, bak):
            with contextlib.suppress(OSError):
                os.unlink(leftover)
        raise Refused(f"admin.toml was not written: {exc.strerror}") from None
    # Renames within root's own directory: they fail only with the filesystem. Either failing
    # leaves admin.toml as it was, so what was staged goes and a retry works (Codex round 3).
    try:
        os.replace(bak, paths.config + ".bak")
        os.replace(new, paths.config)
    except OSError as exc:  # admin.toml is as it was; the record says otherwise: said
        for leftover in (f"{paths.keys}/{key}", new, bak):
            with contextlib.suppress(OSError):
                os.unlink(leftover)
        why = f"admin.toml was not written: {exc.strerror} (changes.jsonl has a line for it)"
        raise Refused(why) from None
    try:  # the change is made: what fails from here is said, never a refusal (review of #421)
        for directory in (config_dir, paths.keys):
            _sync(directory)
    except OSError as exc:
        return _sha256(text), f"the directories' fsync failed: {exc.strerror}"
    return _sha256(text), ""


def _record(paths: Paths, request: Request, old: str, text: str, now: float | None) -> None:
    """A line of `changes.jsonl`, root's (module doc): the change, by its digests. Whole or not
    at all (Codex round 2 on #421): a short write is continued, and a failure cuts the file back
    to its length before, so no torn line is left for the next one to follow. One helper runs at
    a time (a oneshot), so that length is the file's."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "event": "new-workshop",
        "plan": request.plan,
        "workshop": request.name,
        "organizers": request.entry.get("organizers", []),
        "bot": request.entry["bot"].get("user_id"),
        "before_sha256": _sha256(old),
        "after_sha256": _sha256(text),
    }
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(f"{paths.state}/changes.jsonl", flags, 0o644)
    try:
        # The unit's UMask=0077 makes a new file 0600; the bot reads it (Codex on #424)
        os.fchmod(fd, 0o644)
        before = os.fstat(fd).st_size
        try:
            view = memoryview(line.encode("ascii"))
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, before)
            raise
    finally:
        os.close(fd)


def _sync(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(paths: Paths, owner: int, key_group: int) -> int:
    """One pass: 0 when there was no request or it was applied, 1 when it was refused. The
    outcome goes to `result.json` (module doc) and to the journal, never with the key."""
    plan, name = None, None
    try:
        data = take(paths, owner)
        if data is None:
            return 0
        request = parse(data)
        plan, name = request.plan, request.name
        digest, warning = apply(request, paths, key_group)
        said = f"{name} added to admin.toml; admin.toml.bak kept"
        outcome = {"ok": True, "message": said + (f"; but {warning}" if warning else "")}
        outcome["admin_toml_sha256"] = digest
    except Refused as exc:
        outcome = {"ok": False, "message": str(exc)}
    except OSError as exc:  # admin.toml unreadable, say: nothing was written
        outcome = {"ok": False, "message": f"{exc.filename}: {exc.strerror}"}
    except Exception as exc:  # never without an outcome (review of #421); nothing was renamed
        outcome = {"ok": False, "message": f"internal error: {type(exc).__name__}"}
    result = {"version": 1, "plan": plan, "workshop": name, **outcome}
    print(f"zab-admin-config: {plan or 'a request'}: {result['message']}", file=sys.stderr)
    _replace(f"{paths.state}/result.json", (json.dumps(result) + "\n").encode("ascii"), 0o644)
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    """zab-admin-config.service, as root (module doc): its ExecStart imports this and calls it."""
    parser = argparse.ArgumentParser(prog="zab-admin-config")
    # not `Paths.config`: in a slotted dataclass, that is the slot (Codex round 1 on #421)
    parser.add_argument("--config", default=str(DEFAULT_PATH))
    options = parser.parse_args(argv)
    paths = Paths(config=options.config)
    return run(paths, pwd.getpwnam(USER).pw_uid, grp.getgrnam(USER).gr_gid)
