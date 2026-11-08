"""The admin server's file checks, and the scripts' part of the self-check (docs/ADMIN.md §1.3,
§1.6).

**Owners and modes,** in the manner of sshd's StrictModes, at the bots' start
(`AdminServer.start`, and `python -m zab.admin bot --check` for an operator, as `zab-admin`).
The bots refuse to start when a file they trust could have been written by someone else, or a
key read by someone else:

- `admin.toml` and the break-glass public keys (`admin_keys`): regular files, owned by root, not
  writable by group or others. A missing break-glass key is refused too: every VM gets them at
  first boot (§1.7), and `launch.py` would fail on the day;
- the zuliprcs (every admin bot's and the provisioner's) and the GitHub token: also not
  readable by others, and readable by group only for root's or the bot's own group (§1.3:
  root:zab-admin 0640);
- every directory from such a file up to `/etc/zab-admin`: owned by root, not writable by
  group or others. A symbolic link is refused wherever it stands;
- the starter's start log (`check_start_log`, #359), which the wake monitor trusts: its
  directory and that directory's parent owned by `zab-starter` and writable by it alone, the
  directory the bot's group's to read and nobody else's. So nothing of the waker's can write
  or replace what the monitor reads.

**`selfcheck`** (`zab.admin.__main__`), which `deploy.sh` runs as `zab-build` before it switches
`current` (§1.6), runs where none of these files can be checked: `zab-build` can't read the
secrets, a bootstrap runs it before they exist, and its environment is empty. Its part here is
`check_scripts`: every script the actions run answers `--help` from the release's own tree.
"""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import zab
from zab.admin.config import AdminConfig

#: Every script `actions.py`, `newworkshop.py` and `upgradeserver.py` run, relative to the
#: release's `workshop/` (a test keeps it equal to the `run(...)` calls there).
SCRIPTS = (
    "admin/collect_exports.py",
    "admin/final_sync.py",
    "admin/keep_until.py",  # #570
    "admin/new_workshop.py",  # zab.admin.newworkshop's
    "admin/push_config.py",
    "admin/push_waker.py",
    "admin/provision_repos.py",
    "admin/roster_import.py",
    "admin/roster_sync.py",  # #627
    "admin/scan_hosts.py",
    "admin/status.py",
    "admin/upgrade_server.py",  # zab.admin.upgradeserver's (#569)
    "admin/zulip_provision.py",
    "aws/adopt_spare.py",
    "aws/grow_disk.py",
    "aws/launch.py",
    "aws/power.py",
    "aws/protect.py",
    "aws/spare_idle_stop.py",  # #715
    "aws/teardown.py",
)
HELP_TIMEOUT = 60
STARTER = "zab-starter"  # the user that owns the start log (#359)


def starter_uid() -> int:
    """`zab-starter`'s UID, or -1 where there is no such user (then no start log passes)."""
    try:
        return pwd.getpwnam(STARTER).pw_uid
    except KeyError:
        return -1


@dataclass(frozen=True, slots=True)
class Modes:
    """Who may own the checked files and their directories, the bot's group that may read a
    key, the directory the checks stop at, and the start log's owner. Tests pass their own."""

    owners: frozenset[int] = frozenset({0})
    group: int = field(default_factory=os.getgid)
    top: str = "/etc/zab-admin"
    starter: int = field(default_factory=starter_uid)


def _problem(path: Path, st: os.stat_result, modes: Modes, *, private: bool, kind: str) -> str:
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a symbolic link"
    if kind == "file" and not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file"
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        return f"{path} is not a directory"
    if st.st_uid not in modes.owners:
        return f"{path} is owned by uid {st.st_uid}"
    if st.st_mode & 0o022:
        return f"{path} is writable by group or others (mode {stat.S_IMODE(st.st_mode):04o})"
    if private and st.st_mode & 0o007:
        return f"{path} is readable by others (mode {stat.S_IMODE(st.st_mode):04o})"
    if private and st.st_mode & 0o070 and st.st_gid not in (0, modes.group):
        return f"{path} is readable by group {st.st_gid}"
    return ""


def check_file(path: str | Path, modes: Modes, *, private: bool = False) -> str:
    """The first problem with `path` and its directories up to `modes.top`, or ""."""
    path = Path(path)
    top = Path(modes.top)
    if path != top and top not in path.parents:
        return f"{path} is outside {top}"
    try:
        problem = _problem(path, os.lstat(path), modes, private=private, kind="file")
    except FileNotFoundError:
        return f"{path} is missing"
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    for parent in path.parents:
        if problem or (parent != top and top not in parent.parents):
            break
        try:
            problem = _problem(parent, os.lstat(parent), modes, private=False, kind="dir")
        except OSError as exc:
            problem = f"{parent}: {exc.strerror}"
    return problem


def check_start_log(directory: str | Path, modes: Modes) -> str:
    """The first problem with the start log's directory and its parent (module doc), or ""."""
    path = Path(directory)
    for each in (path.parent, path):
        try:
            st = os.lstat(each)
        except FileNotFoundError:
            return f"{each} is missing"
        except OSError as exc:
            return f"{each}: {exc.strerror}"
        if stat.S_ISLNK(st.st_mode):
            return f"{each} is a symbolic link"
        if not stat.S_ISDIR(st.st_mode):
            return f"{each} is not a directory"
        if st.st_uid != modes.starter:
            return f"{each} is owned by uid {st.st_uid}, not {STARTER}"
        if st.st_mode & 0o022:
            return f"{each} is writable by group or others (mode {stat.S_IMODE(st.st_mode):04o})"
    if st.st_gid != modes.group:
        return f"{path} is group {st.st_gid}, not the bot's"
    if st.st_mode & 0o007:
        return f"{path} is open to others (mode {stat.S_IMODE(st.st_mode):04o})"
    return ""


def secret_paths(cfg: AdminConfig, provisioner_zuliprc: str, github_token: str) -> list[str]:
    """The keys the bot process reads: each admin bot's zuliprc, the provisioner's, the token.
    (Not `actions.Layout`: the root helper imports this module, and nothing of the actions'.)"""
    bots = [ws.bot for ws in cfg.workshops] + ([cfg.global_bot] if cfg.global_bot else [])
    return [*(b.zuliprc for b in bots), provisioner_zuliprc, github_token]


def check_files(
    cfg: AdminConfig, config_path: str, modes: Modes, *, secrets: Iterable[str] = ()
) -> list[str]:
    """Every problem with admin.toml, the break-glass keys and `secrets`."""
    keys = sorted({key for ws in cfg.workshops for key in ws.aws.admin_keys})
    found = [check_file(config_path, modes)]
    found += [check_file(key, modes) for key in keys]
    found += [check_file(path, modes, private=True) for path in secrets]
    return [problem for problem in found if problem]


def release_root() -> Path:
    """The release tree this code runs from: `<release>/src/zab/__init__.py` (§1.6)."""
    return Path(zab.__file__).resolve().parents[2]


def check_scripts(root: Path, scripts: Iterable[str] = SCRIPTS) -> list[str]:
    """Each script's `--help`, run as the bot would run it (-E -s, a fixed environment), and
    with -B: `deploy.sh` checks the built tree file by file after this, so nothing may be
    written into it (-E would ignore PYTHONDONTWRITEBYTECODE)."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    problems = []
    for script in scripts:
        argv = [sys.executable, "-E", "-s", "-B", str(root / "workshop" / script), "--help"]
        try:  # our own interpreter and a fixed path of this release: no shell, no message text
            done = subprocess.run(  # noqa: S603
                argv, env=env, capture_output=True, timeout=HELP_TIMEOUT, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{script} --help: {type(exc).__name__}")
            continue
        if done.returncode != 0:
            last = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            problems.append(f"{script} --help exited {done.returncode}: {last[0][:200]}")
    return problems
