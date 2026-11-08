#!/usr/bin/env python3
"""`upgrade_server.py`: the bot's side of `!upgrade-server` (docs/ADMIN.md §1.6; #569).

The admin server's global bot runs it, as `zab-admin`, with every value from the approved
command (`zab.admin.upgradeserver`), never typed text:

    upgrade_server.py --plan P-42 --release v0.2.20 --sha256 <64 hex> \\
      --state /var/lib/zab-admin-upgrade [--dry-run]

It holds no key and changes nothing itself: it writes a request to one of the upgrade's root
helpers (`zab.admin.upgradehelper`) and prints the helper's outcome.

- **`--dry-run`, the plan:** `requests/plan.json`, then `results/<plan>.plan.json`. The plan
  helper fetches the release and the running one's tags from `release_repo`, checks them as
  deploy.sh does, has the running release's deploy.sh review the release, and refuses unless the
  review's SHA-256 is `--sha256`, the deployer's own. This prints its text, which the plan shows
  and is hashed on; the runner runs it again before the apply, so a tag that moved since makes
  the plan STALE.
- **The apply:** the commit the plan helper's outcome for this plan names (just written by that
  recheck), then `requests/apply.json` with it, then `results/<plan>.apply.json`. The apply
  helper installs the release with its own deploy.sh and restarts every unit but the bots',
  which restart themselves once this apply's result is posted. This prints the outcome and
  deploy.sh's last lines, and whether `current` moved.

**The request** is written under a temporary name, 0600, and renamed into place with
renameat2's `RENAME_NOREPLACE`, so that it never replaces another one and has one link from the
moment it is there, as the helper requires: a request of the same kind still waiting for its
helper is waited for, at most `SLOT` seconds. Each carries a nonce, which the outcome echoes, so
an earlier run's outcome is never taken for this one. With no outcome in time, the request is
withdrawn by a rename, as the helper claims one (`confighelper.take`): of the two renames
exactly one finds it. When the helper has it already, its outcome may still come (`LATE`).

**What it prints** may hold text from `release_repo` (git's errors, the CHANGELOG section,
deploy.sh's log): the helper quotes it or makes what a terminal would hide visible, and the bot
shows this output scrubbed and fenced, as any script's.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import pathlib
import re
import secrets
import sys
import time
from typing import Any

PLAN_WAIT = 1260.0  # seconds for the plan helper's outcome: its unit stops it at 1200
APPLY_WAIT = 1560.0  # for the apply helper's: its unit stops it at 1500
SLOT = 60.0  # for an earlier request of the same kind to be taken
LATE = 30.0  # more, once the helper has taken the request
POLL = 1.0
_PLAN = re.compile(r"P-[1-9][0-9]{0,8}", re.ASCII)
_VERSION = re.compile(r"v[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(?:rc[0-9]{1,6})?", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", re.ASCII)


class Failed(Exception):
    """One line: why nothing (more) can be said."""


def _read(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def _stamp(path: pathlib.Path) -> tuple[int, int] | None:
    """A file's inode and modification time: the helper writes a new file and renames it."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


#: Linux's renameat2(2): `RENAME_NOREPLACE` renames only onto no file, atomically (glibc 2.28+).
_AT_FDCWD, _RENAME_NOREPLACE = -100, 1


def rename_noreplace(source: pathlib.Path, target: pathlib.Path) -> None:
    """`source` renamed to `target`, which must not exist: FileExistsError if it does. One
    step, so the request has one link from the moment it is there, which the helper requires
    (`confighelper.take`); a link then an unlink would show it with two (Codex round 1 on
    #898). Python has no renameat2, so ctypes calls libc's."""
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.renameat2
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))  # EEXIST: FileExistsError


def send(state: pathlib.Path, action: str, body: dict[str, Any]) -> pathlib.Path:
    """The request, renamed into place once no other of its kind waits (module doc)."""
    target = state / "requests" / f"{action}.json"
    tmp = target.with_name(f"{action}.{os.getpid()}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            json.dump(body, f, ensure_ascii=True)
        deadline = time.monotonic() + SLOT
        while True:
            try:
                rename_noreplace(tmp, target)  # never over another request, one link always
                return target
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise Failed(
                        f"{target} is still there: the helper hasn't taken an earlier request "
                        f"within {SLOT:.0f} s (journalctl -u zab-admin-upgrade*)"
                    ) from None
                time.sleep(POLL)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def outcome(state: pathlib.Path, action: str, body: dict[str, Any], wait: float) -> dict:
    """The helper's outcome of the request `body`, sent now: `results/<plan>.<action>.json`
    with its nonce, or the outcome of a request it couldn't read (`unread.<action>.json`, newer
    than the request, once the request is gone). Raises Failed with none in time, after the
    request is withdrawn, or once the helper took it and said nothing `LATE` seconds more."""
    results = state / "results"
    unread = results / f"unread.{action}.json"
    earlier = _stamp(unread)
    target = send(state, action, body)

    def look() -> dict | None:
        found = _read(results / f"{body['plan']}.{action}.json")
        if found is not None and found.get("nonce") == body["nonce"]:
            return found
        if not os.path.lexists(target) and _stamp(unread) != earlier:
            other = _read(unread)
            if other is not None and other.get("nonce") in (body["nonce"], None):
                return {**other, "ok": False}
        return None

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(POLL)
        if (found := look()) is not None:
            return found
    withdrawn = target.with_name(f"{action}.{os.getpid()}.withdrawn")
    try:
        os.rename(target, withdrawn)
    except FileNotFoundError:  # the helper has it: its outcome may still come
        late = time.monotonic() + LATE
        while time.monotonic() < late:
            time.sleep(POLL)
            if (found := look()) is not None:
                return found
        raise Failed(
            f"the helper took the request but said nothing within {wait + LATE:.0f} s: "
            f"journalctl -u zab-admin-upgrade{'-plan' * (action == 'plan')}"
        ) from None
    mine = (_read(withdrawn) or {}).get("nonce") == body["nonce"]
    if not mine:  # never: one request of a kind at a time; put back, never over another
        with contextlib.suppress(OSError):
            rename_noreplace(withdrawn, target)  # one link, as for any request
    with contextlib.suppress(OSError):
        os.unlink(withdrawn)  # ours, or another's that could not go back
    raise Failed(f"the helper said nothing within {wait:.0f} s: the request is withdrawn")


def request(args: argparse.Namespace, **more: str) -> dict[str, Any]:
    """A request's body: the command's checked values, a fresh nonce, and `more`."""
    return {
        "version": 1, "plan": args.plan, "nonce": secrets.token_hex(16), "release": args.release,
        "sha256": args.sha256, **more,
    }  # fmt: skip


def plan(args: argparse.Namespace, state: pathlib.Path) -> int:
    found = outcome(state, "plan", request(args), PLAN_WAIT)
    if found.get("ok") is not True:
        print(f"the plan helper refused it: {found.get('message')}")
        return 1
    print(found.get("text", "").rstrip("\n"))
    return 0


def apply(args: argparse.Namespace, state: pathlib.Path) -> int:
    planned = _read(state / "results" / f"{args.plan}.plan.json") or {}
    commit = planned.get("commit")
    if planned.get("ok") is not True or planned.get("release") != args.release:
        raise Failed(f"{args.plan}'s check on record didn't pass for {args.release}")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise Failed(f"{args.plan}'s check on record names no commit")
    found = outcome(state, "apply", request(args, commit=commit), APPLY_WAIT)
    print(f"the apply helper: {found.get('message')}")
    log = found.get("log")
    if isinstance(log, list) and log:
        print("deploy.sh's last lines:")
        for line in log:
            print(f"  {line}")
    if found.get("current") == args.release:
        print(f"The bots restart into {args.release} now: `!version` answers from it.")
    return 0 if found.get("ok") is True else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="upgrade_server.py", description=__doc__.split("\n\n")[0])
    for flag in ("plan", "release", "sha256", "state"):
        p.add_argument(f"--{flag}", required=True)
    p.add_argument("--dry-run", action="store_true", help="the plan: the plan helper's check")
    args = p.parse_args(argv)
    checks = ((_PLAN, args.plan), (_VERSION, args.release), (_SHA256, args.sha256))
    if not all(pattern.fullmatch(value) for pattern, value in checks):
        print("upgrade_server.py: --plan, --release or --sha256 is malformed", file=sys.stderr)
        return 2
    state = pathlib.Path(args.state)
    try:
        return plan(args, state) if args.dry_run else apply(args, state)
    except (Failed, OSError) as exc:
        print(f"upgrade_server.py: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
