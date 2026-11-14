#!/usr/bin/env python3
"""Power a kept workshop VM off once nobody is using it (issue #207; README.md §8).

    python3 idle_stop.py --minutes N [--max-load 0.5] [--dry-run]

Installed by `push_config.py --idle-stop N` as `/usr/local/lib/zab/idle_stop.py` and run once a
minute as root by `zab-idle-stop.timer`. N is at least 10. The VM stays up while any of these
holds:

- it has been up for less than N minutes, so a VM the waker has just started gets time to
  handle the message that woke it;
- a turn is running (`turns.state = 'running'` in zab's database). Age never decides (PM on
  #236: a wrong power-off destroys work unnoticed, a stale row costs a day's compute and shows):
  - a Claude row counts while `zab.service` is active (Claude turns die with the bridge) and
    its recorded process group still exists, at any age and in a detached session too
    (`!detach` keeps the running turn);
  - a Codex row counts while `zab.service` or any `codex-app-server*` unit is active (Codex
    turns run in the daemon, which outlives the bridge), at any age;
  - when the only thing holding the VM up is a row with no recorded process in a detached
    session, more than 24 hours old, the verdict names it as possibly stale ("staying up only
    for a detached codex turn running 31 h (possibly stale: check with `zab status`)"); such
    rows come from `!attach` and recovery, which are to settle them at the source;
- a background job runs (#655; SPEC §7.14): one of `agent`'s `zab-job-*` units, which
  `zab job start` makes, is active, whatever its load, since a job that sleeps or waits on the
  network or the disk would otherwise be powered off under it. Each job's own cap
  (`RuntimeMaxSec`, `[jobs] max_hours`, 12 hours by default) stops it, and the usual timeout
  applies after it ends, counted from the last tick that saw a job hold the VM
  (`/run/zab-idle-stop.job`'s mtime; "a background job ran 3 min ago"): zab's database shows
  the job's end only once the bridge's watcher has posted it and its report turn has started,
  up to its next pass later, so a tick in between would otherwise power the VM off before the
  end post (Codex round 1 on #970). The verdict says "2 background jobs running". No hold
  outlasts a cap, so that no error holds a VM up for good. A unit holds only while it has been
  active (`ActiveEnterTimestampMonotonic`) for less than its own cap (`RuntimeMaxUSec`) and
  `JOB_GRACE_S` (15 min); one without a cap (`infinity`: made by hand, or a job that lost its
  `RuntimeMaxSec`) never holds, and the verdict says "(ignoring a background job active 13 h,
  past its cap of 12 h)" or "(ignoring a background job without a time limit)". A `systemctl`
  that can't list them holds the VM up ("background jobs unknown") only until that has lasted
  the jobs' cap, the bridge's `[jobs] max_hours` (12 by default; `CONFIG`), and 15 min in a row,
  since no job can start while agent's manager doesn't answer, and any job running then has
  ended at its cap: then "(ignoring background jobs unknown for 12 h 15 min)". When that began
  is kept in `/run/zab-idle-stop.jobs-unknown` (tmpfs: a boot, which ends every job, starts it
  again);
- zab took a message in for its agents (a turn, a delivery to a running turn, an attach), held one
  for a busy session, or a turn started or ended, less than N minutes ago. Messages the bridge
  ignores don't count: `Store.claim` records every message a bot sees as `ignored` before it
  looks at the sender, so other people's posts in a shared channel would otherwise keep the VM
  up indefinitely (codex on #218). Commands such as `!status` stay `ignored` too; a VM the
  waker started for one is covered by the first rule;
- the 5-minute load average is at least `--max-load` (the participant's own computations);
- someone is logged in (a `who` line, or an SSH session without a terminal, such as one of a
  `push_config.py` run's connections), or was less than N minutes ago (`/run/zab-idle-stop.login`
  records the last time one was seen), so closing the last SSH session doesn't stop a VM whose
  detached job uses little CPU. The admin server's status reads are not logins (#507, below);
- a rollout or a final sync holds it: `push_config.py`, and `final_sync.py` around a sync (#370),
  write an expiry time (epoch seconds) to `/run/zab-idle-stop.hold` before their first change
  and remove the file at the end, so no tick between their SSH connections can stop the VM;
  an unreadable hold counts for an hour from its mtime. The verdict says "held by a rollout";
- the first boot's volume warm-up (`aws_common.WARM_FIRST_BOOT`) can still end (#715): its
  marker `/var/lib/zab-warm.done` is missing, and its unit `zab-warm-volume.service` runs, or
  cloud-init's last stage, which starts it, is still under way. A stop cuts the warm-up off for
  good: its unit doesn't outlive the boot, and cloud-init starts it at the first boot only. So a
  VM whose warm-up was cut off, or whose image has none, isn't held by it. The verdict says "the
  first boot's volume warm-up is running". Never past `WARM_MAX_S` (3 hours) of uptime: a
  cloud-final hung in `activating`, or an answer of systemctl's it can't read, then holds no
  longer, and the verdict says "ignoring the first boot's volume warm-up, unfinished after 3 h
  up" (review of #728; the warm-up takes about 15 minutes).

**A spare** (`--spare`, #715; ORGANIZERS §3.7): `spare_idle_stop.py` installs it on the spares,
with admin.toml's `spare_idle_stop` minutes. A spare has no bridge yet, so no zab database: its
absence is no reason to stay up there; every other rule holds as on a participant's VM. Its
verdicts go to `/var/log/zab-idle-stop-spare.log`, so that the log of a participant's idle stop,
which `push_config.py --idle-stop-default` reads, is never a spare's. `!adopt-spare` replaces it
with the row's idle stop (`push_config.py --adopt-spare`).

Otherwise it runs `systemctl poweroff`. An EBS-backed instance whose
`InstanceInitiatedShutdownBehavior` is `stop` (launch.py sets it) then stops, keeping its disk,
its Elastic IP and the agents' sign-ins. Anything it cannot read keeps the VM up: a missing,
unreadable or locked database, an unreadable load average, or a `systemctl` that fails never
powers a VM off.

**The read user** (#507). `status.py` reads a VM as `zab-monitor`, whose sessions without a terminal
are not logins. Its login shell is the read (`READ_LOGIN`): whatever a session asks for, and
however it logged in (EC2 Instance Connect too), it runs `status_read.sh` as `agent`, which
`/etc/sudoers.d/zab-monitor` allows and nothing more. The read's agent side is agent's login
shell (`.profile`) and `zab`, as before; status.py's 180 s timeout ends the session. Its
`authorized_keys`, root's, is rewritten from ubuntu's on every tick (`sync_read_keys`): each
plain key, `restrict`ed (no terminal, no forwarding) to the same command. Whoever may log in as
ubuntu may read, and a key removed there stops reading at the next tick. Only root could make
a session of that user more than the read, and root can stop idle stop anyway (an Instance
Connect key, which gets no `restrict`, may forward ports and sockets, and use sftp where sshd
serves it itself (`internal-sftp`), as it may for `ubuntu`).
`--setup-read-user` (push_config.py, as root) creates the user, or resets one that exists.

Every run prints its verdict, which goes to the journal (`journalctl -u zab-idle-stop`).
`/var/log/zab-idle-stop.log` gets a line only when the verdict changes, and before each power-off,
so it stays short. The database is read as `agent`, read-only, so root never creates files
in zab's state directory. Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

AGENT = "agent"
DB = "/home/agent/.local/state/zab/zab.db"
LOG = "/var/log/zab-idle-stop.log"
#: The previous verdict, so the log file gets a line only on a change (tmpfs: gone at boot).
LAST = "/run/zab-idle-stop.last"
#: When a login was last seen (its mtime), for the grace after logout.
LOGIN = "/run/zab-idle-stop.login"
#: A rollout's or a final sync's hold: an expiry in epoch seconds (push_config.py and
#: final_sync.py write and remove it, with `admin_common.IDLE_HOLD_ON_CMD` and `_OFF_CMD`).
HOLD = "/run/zab-idle-stop.hold"
#: A spare's log (`--spare`, #715): the log of a participant's idle stop stays its own.
SPARE_LOG = "/var/log/zab-idle-stop-spare.log"
#: The first boot's volume warm-up (`aws_common.WARM_FIRST_BOOT`, #715): its marker, written at
#: its end, and `systemctl is-active` of its unit and of cloud-init's last stage, which starts it.
WARM_DONE = "/var/lib/zab-warm.done"
WARM_ACTIVE = ["systemctl", "is-active", "zab-warm-volume.service", "cloud-final.service"]
#: `systemctl is-active`'s states of a unit that runs or starts, and of one that doesn't.
_UP = frozenset({"active", "activating", "reloading", "refreshing"})
_DOWN = frozenset({"inactive", "failed", "deactivating"})
#: The warm-up holds a VM no longer than this from its boot (module doc).
WARM_MAX_S = 3 * 3600
MIN_MINUTES = 10
STALE_ROW_S = 24 * 3600
POWEROFF = ["systemctl", "poweroff"]
_USER_SYSTEMCTL = ["systemctl", "--user", f"--machine={AGENT}@"]
ZAB_ACTIVE = [*_USER_SYSTEMCTL, "is-active", "zab.service"]
CODEX_ACTIVE = [
    *_USER_SYSTEMCTL,
    "list-units",
    "--type=service",
    "--state=active,activating,deactivating,reloading",
    "--no-legend",
    "--plain",
    "codex-app-server*.service",
]
#: agent's background jobs (#655): `zab job start` runs each as a transient unit
#: `zab-job-<id>.service` of agent's user manager; one stopping (its `ExecStopPost` writing how
#: it ended) still counts. The id's form is `zab job`'s, not checked here
JOBS_ACTIVE = [*CODEX_ACTIVE[:-1], "zab-job-*.service"]
_JOB_UNIT = re.compile(r"zab-job-\S+\.service")
#: ... and each one's cap and when it became active, on the monotonic clock (µs), as
#: `JOBS_SHOW + [unit, …]`
JOBS_SHOW = [
    *_USER_SYSTEMCTL,
    "show",
    "--property=Id",
    "--property=ActiveState",
    "--property=RuntimeMaxUSec",
    "--property=ActiveEnterTimestampMonotonic",
]
#: How long past its cap a job, or jobs that can't be listed, still hold the VM (module doc)
JOB_GRACE_S = 15 * 60
#: The bridge's config, for `[jobs] max_hours` (SPEC §7.14): 12 when it has none, and the
#: config's own bounds, above 0 and at most 168
CONFIG = "/home/agent/.config/zab/config.toml"
MAX_HOURS, MAX_HOURS_LIMIT = 12.0, 168.0
#: Since when the jobs couldn't be listed, in epoch seconds (tmpfs: gone at boot)
JOBS_UNKNOWN = "/run/zab-idle-stop.jobs-unknown"
#: When a background job was last seen holding the VM (its mtime), for the timeout after its end
JOB_SEEN = "/run/zab-idle-stop.job"
#: A systemd time span as `systemctl show` prints it: `12h`, `1d 12h`, `3min 30s`, `infinity`
_SPAN = re.compile(r"(\d+(?:\.\d+)?)(us|µs|ms|s|min|h|d|w|month|y)")
_SPAN_S = {"us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400,
           "w": 604800, "month": 2629800, "y": 31557600}  # fmt: skip
#: An SSH session's process title: `sshd: ubuntu@notty` (OpenSSH 9.6, Ubuntu 24.04) or
#: `sshd-session: ubuntu@pts/0` (9.8 and later). The listener and `[priv]` halves have no `@`.
_SSH_SESSION = re.compile(rb"^sshd(?:-session)?: [^\s@\[]+@")
#: The admin server's status reads (module doc, #507): this user's sessions without a terminal
#: are not logins. sshd names a session after the user it authenticated, so no one else's session
#: can carry the name; a terminal (which `restrict` forbids) would count.
READ_USER = "zab-monitor"
READ_SESSION = f"{READ_USER}@notty"
READ_HOME = "/var/lib/zab-monitor"
READ_KEYS = f"{READ_HOME}/.ssh/authorized_keys"
UBUNTU_KEYS = "/home/ubuntu/.ssh/authorized_keys"
#: The read (`status.READ_SCRIPT`), which push_config.py installs with the sudoers rule, and
#: the user's login shell, which runs it whatever it is asked (`READ_LOGIN_TEXT`).
READ_SCRIPT_PATH = "/usr/local/lib/zab/status_read.sh"
READ_COMMAND = f"/usr/bin/sudo -n -H -u {AGENT} {READ_SCRIPT_PATH}"
READ_OPTIONS = f'restrict,command="{READ_COMMAND}"'
READ_LOGIN = "/usr/local/lib/zab/status_read_login"
READ_LOGIN_TEXT = (
    f"#!/bin/sh\n# zab #507: zab-monitor's login shell, the status read\nexec {READ_COMMAND}\n"
)
#: ubuntu's `authorized_keys` is read only up to this size (`ubuntu_keys_text`).
MAX_KEYS_BYTES = 64 * 1024
#: `aws_common.PUBKEY_TYPES`: a plain key line is one of these, its base64, and a comment or not.
KEY_TYPES = tuple(
    "ssh-ed25519 ssh-rsa ecdsa-sha2-nistp256 ecdsa-sha2-nistp384 ecdsa-sha2-nistp521"
    " sk-ssh-ed25519@openssh.com sk-ecdsa-sha2-nistp256@openssh.com".split()
)
_KEY_LINE = re.compile(r"(\S+)[ \t]+([A-Za-z0-9+/]+={0,2})(?:[ \t\r]|$)")

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(slots=True)
class Facts:
    """What the checks look at; None where it couldn't be read."""

    now: float
    uptime: float | None
    load5: float | None
    logins: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)  # zab-monitor's sessions: not logins (#507)
    last_login: float | None = None  # epoch, from LOGIN's mtime
    hold_until: float | None = None  # epoch, from HOLD
    running_turns: int | None = 0  # rows that count, after the rules above
    running_jobs: int | None = 0  # agent's active `zab-job-*` units (#655)
    jobs_ignored: list[str] = field(default_factory=list)  # past their caps: no hold
    last_job: float | None = None  # epoch, from JOB_SEEN's mtime: a job last seen holding
    ignored_rows: list[str] = field(default_factory=list)  # why the others don't
    suspect_rows: list[str] = field(default_factory=list)  # counted, but possibly stale
    last_activity: float | None = None  # newest handled message or turn start/end (epoch)
    warming: bool | None = False  # the first boot's volume warm-up may still end (#715)
    warm_ignored: str = ""  # why a warm-up past `WARM_MAX_S` no longer holds it
    errors: list[str] = field(default_factory=list)


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)} min"


def hold_reasons(facts: Facts, minutes: int, max_load: float) -> list[str]:
    """Why the VM stays up; an empty list means power off."""
    idle = minutes * 60
    reasons = [f"cannot read {what}" for what in facts.errors]
    if facts.uptime is None:
        reasons.append("uptime unknown")
    elif facts.uptime < idle:
        reasons.append(f"up only {_minutes(facts.uptime)}")
    if facts.running_turns is None:
        reasons.append("running turns unknown")
    elif facts.running_turns and facts.running_turns == len(facts.suspect_rows):
        reasons.append(
            "staying up only for "
            + "; ".join(facts.suspect_rows)
            + " (possibly stale: check with `zab status`)"
        )
    elif facts.running_turns:
        reasons.append(f"{facts.running_turns} turn(s) running")
    if facts.running_jobs is None:  # for at most the jobs' cap in a row (`gather`)
        reasons.append("background jobs unknown")
    elif facts.running_jobs:
        jobs = "job" if facts.running_jobs == 1 else "jobs"
        reasons.append(f"{facts.running_jobs} background {jobs} running")
    elif facts.last_job is not None and facts.now - facts.last_job < idle:
        reasons.append(f"a background job ran {_minutes(max(0.0, facts.now - facts.last_job))} ago")
    if facts.last_activity is not None and facts.now - facts.last_activity < idle:
        reasons.append(f"zab active {_minutes(max(0.0, facts.now - facts.last_activity))} ago")
    if facts.load5 is None:
        reasons.append("load unknown")
    elif facts.load5 >= max_load:
        reasons.append(f"load {facts.load5:.2f}")
    if facts.logins:
        reasons.append("logged in: " + ", ".join(sorted(set(facts.logins))))
    elif facts.last_login is not None and facts.now - facts.last_login < idle:
        reasons.append(f"logged in {_minutes(max(0.0, facts.now - facts.last_login))} ago")
    if facts.hold_until is not None and facts.now < facts.hold_until:
        until = time.strftime("%H:%MZ", time.gmtime(facts.hold_until))
        reasons.append(f"held by a rollout until {until}")
    if facts.warming is None:
        reasons.append("the first boot's volume warm-up unknown")
    elif facts.warming:
        reasons.append("the first boot's volume warm-up is running")
    return reasons


# ------------------------------------------------------------------------------- readers


def read_uptime(proc: str = "/proc") -> float:
    return float(Path(proc, "uptime").read_text().split()[0])


def read_load5(proc: str = "/proc") -> float:
    return float(Path(proc, "loadavg").read_text().split()[1])


def ssh_sessions(proc: str = "/proc") -> list[str]:
    """`user@tty` of every SSH session, from the sshd process titles."""
    found = []
    for entry in os.scandir(proc):
        if not entry.name.isdigit():
            continue
        try:
            cmdline = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        title = cmdline.split(b"\0", 1)[0].strip()
        if _SSH_SESSION.match(title):
            found.append(title.split(b" ", 1)[1].decode(errors="replace"))
    return found


def who_logins(run: Run) -> list[str]:
    """`user@tty` for every `who` line."""
    res = run(["who"], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise OSError(f"who exited {res.returncode}")
    users = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            users.append(f"{parts[0]}@{parts[1]}")
    return users


def clamped(path: str, now: float, *, write: bool = True) -> float | None:
    """`path`'s mtime, as `read_mtime`; one in the future (the clock set back since it was
    written) counts as `now`, and is rewritten so unless `write` is off, so that a record
    holds the VM for one timeout at most, never for the clock's skew (review of #970, N5)."""
    mtime = read_mtime(path)
    if mtime is None or mtime <= now:
        return mtime
    if write:
        try:
            os.utime(path, (now, now))
        except OSError:
            pass
    return now


def read_mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def read_hold(path: str) -> float | None:
    """The hold's expiry; an unreadable one counts for an hour from its mtime."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    except OSError:
        text = ""
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        mtime = read_mtime(path)
        return mtime + 3600 if mtime is not None else None


def warm_up_running(run: Run, done: str = WARM_DONE) -> bool | None:
    """Whether the first boot's volume warm-up may still end (module doc, #715): its marker
    `done` missing, and its unit running, or cloud-init's last stage, which starts it, still
    starting; None when systemctl can't say."""
    if os.path.exists(done):
        return False
    res = run(WARM_ACTIVE, capture_output=True, text=True, timeout=30)
    states = res.stdout.split()
    if len(states) != 2 or not set(states) <= _UP | _DOWN:
        return None
    warm, final = states
    return warm in _UP or final == "activating"


def read_db(path: str) -> dict:
    """Running turns (with their backend, start and process group) and the newest activity,
    read-only. A missing database is reported as such: it holds the VM up (PM on #218)."""
    if not os.path.exists(path):
        return {"missing": True}
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        running = [
            {
                "backend": backend,
                "session_state": state,
                "started_at": started,
                "pgid": pgid,
                "approval": bool(approval),
            }
            for backend, state, started, pgid, approval in db.execute(
                "SELECT s.backend, s.state, t.started_at, t.pgid,"
                " EXISTS(SELECT 1 FROM approvals a"
                "  WHERE a.session_id = t.session_id AND a.state = 'pending')"
                " FROM turns t LEFT JOIN sessions s ON s.id = t.session_id"
                " WHERE t.state = 'running'"
            )
        ]
        last = db.execute(
            "SELECT max(t) FROM ("
            " SELECT max(handled_at) AS t FROM inbound WHERE disposition != 'ignored'"
            " UNION ALL SELECT max(received_at) FROM held"
            " UNION ALL SELECT max(started_at) FROM turns"
            " UNION ALL SELECT max(ended_at) FROM turns)"
        ).fetchone()[0]
    finally:
        db.close()
    return {"running": running, "last": last}


def read_db_as_agent(path: str, run: Run) -> dict:
    """`read_db` in a child process running as `agent` (no root-owned `-shm` in zab's state)."""
    res = run(
        [sys.executable, os.path.abspath(__file__), "--read-db", path],
        user=AGENT,
        group=AGENT,
        extra_groups=[],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/",
    )
    if res.returncode != 0:
        raise OSError((res.stderr.strip().splitlines() or [f"exit {res.returncode}"])[-1])
    return json.loads(res.stdout)


def zab_active(run: Run) -> bool | None:
    """Whether agent's `zab.service` is active; None when systemctl can't say."""
    res = run(ZAB_ACTIVE, capture_output=True, text=True, timeout=30)
    state = res.stdout.strip()
    if state in ("active", "reloading", "activating", "deactivating"):
        return True
    if state in ("inactive", "failed"):
        return False
    return None


def codex_active(run: Run) -> bool | None:
    """Whether any of agent's `codex-app-server*` units is active; None when systemctl can't say."""
    res = run(CODEX_ACTIVE, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return None
    return bool(res.stdout.strip())


def _hours(seconds: float) -> str:
    hours, minutes = divmod(int(seconds // 60), 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def span_seconds(text: str) -> float | None:
    """A time span as `systemctl show` prints it, in seconds: `math.inf` for the literal
    `infinity`, no cap; None for an empty value or anything else it can't read, which is
    unknown, not "no cap" (review round 4 of #924). A bare number is microseconds."""
    text = text.strip()
    if text == "infinity":
        return math.inf
    if text.isdigit():
        return int(text) / 1e6
    parts = text.split()
    found = [_SPAN.fullmatch(part) for part in parts]
    if not parts or not all(found):
        return None
    return sum(float(m[1]) * _SPAN_S[m[2]] for m in found if m)


def show_blocks(text: str) -> list[dict[str, str]]:
    """`systemctl show`'s output for several units, one dict of `key=value` lines per unit:
    the units are separated by a blank line, and within one the properties come in systemd's
    order, not the order asked for (systemd 255 prints `RuntimeMaxUSec` before `Id`)."""
    blocks = []
    for chunk in re.split(r"\n\s*\n", text):
        fields = dict(line.partition("=")[::2] for line in chunk.splitlines() if "=" in line)
        if fields:
            blocks.append(fields)
    return blocks


def max_hours(path: str = CONFIG) -> float:
    """The bridge's `[jobs] max_hours`, or `MAX_HOURS` when it has none or one out of bounds."""
    try:
        with open(path, "rb") as f:
            hours = tomllib.load(f).get("jobs", {}).get("max_hours", MAX_HOURS)
    except (OSError, ValueError, AttributeError):
        return MAX_HOURS
    ok = isinstance(hours, (int, float)) and not isinstance(hours, bool)
    return float(hours) if ok and 0 < hours <= MAX_HOURS_LIMIT else MAX_HOURS


def jobs_running(run: Run, monotonic: float) -> tuple[int, list[str]] | None:
    """How many of agent's background jobs hold the VM (`JOBS_ACTIVE`), and a note on each that
    doesn't: active on the `monotonic` clock (s) for its cap and `JOB_GRACE_S` or longer, or
    without a cap; None when systemctl can't say, or answers anything else than job units."""
    res = run(JOBS_ACTIVE, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return None
    units = sorted({line.split()[0] for line in res.stdout.splitlines() if line.strip()})
    if not all(_JOB_UNIT.fullmatch(unit) for unit in units):
        return None
    if not units:
        return 0, []
    res = run([*JOBS_SHOW, *units], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return None
    since: dict[str, int] = {}
    caps: dict[str, float] = {}
    gone: set[str] = set()
    for fields in show_blocks(res.stdout):
        unit, stamp = fields.get("Id", "").strip(), fields.get("ActiveEnterTimestampMonotonic")
        state = fields.get("ActiveState", "").strip()
        cap = span_seconds(fields.get("RuntimeMaxUSec", ""))
        if state in ("inactive", "failed") and unit:  # ended between the two calls
            gone.add(unit)
            continue
        if not unit or stamp is None or not stamp.strip().isdigit() or cap is None:
            return None  # a field missing or unreadable: unknown, held within its own limit
        since[unit], caps[unit] = int(stamp), cap
    if set(since) | gone != set(units):
        return None
    holding, old = 0, []
    for unit in units:
        if unit in gone:
            continue
        cap = caps[unit]
        age = monotonic - since[unit] / 1e6 if since[unit] else 0.0  # 0: not active yet
        if cap == math.inf:
            old.append("a background job without a time limit")
        elif age >= cap + JOB_GRACE_S:
            old.append(f"a background job active {_hours(age)}, past its cap of {_hours(cap)}")
        else:
            holding += 1
    return holding, old


def unknown_since(path: str, now: float, unknown: bool, *, write: bool = True) -> float | None:
    """Since when the jobs couldn't be listed, recorded at `path` the first time (a write that
    fails counts from `now`); None, and the record gone, once they could be. A time later than
    now, after the clock was set back, counts as now. Without `write` (`--dry-run`), the
    record is only read."""
    if not write:
        if not unknown:
            return None
        try:
            return min(float(Path(path).read_text().split()[0]), now)
        except (OSError, ValueError, IndexError):
            return now
    if not unknown:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"cannot remove {path}: {exc}", file=sys.stderr)
        return None
    try:
        return min(float(Path(path).read_text().split()[0]), now)
    except (OSError, ValueError, IndexError):
        pass
    try:
        Path(path).write_text(f"{now:.0f}\n")
    except OSError as exc:
        print(f"cannot write {path}: {exc}", file=sys.stderr)
    return now


def group_alive(pgid: int) -> bool:
    """Whether a process group exists (root may signal it; signal 0 only checks)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def count_running(
    rows: list[dict],
    now: float,
    run: Run,
    alive: Callable[[int], bool] = group_alive,
) -> tuple[int, list[str], list[str]]:
    """(rows that hold the VM up; notes on the rows that don't; descriptions of counted rows
    that are possibly stale: no recorded process, a detached session, more than 24 h old)."""
    if not rows:
        return 0, [], []
    try:
        zab = zab_active(run)
    except (OSError, subprocess.SubprocessError):
        zab = None
    codex: bool | str | None = "unchecked"
    counted, ignored, suspects = 0, [], []
    for row in rows:
        kind = row.get("backend") or "?"
        started = row.get("started_at")
        age = now - started if isinstance(started, (int, float)) else 0.0
        suspect = (
            row.get("session_state") == "detached"
            and age > STALE_ROW_S
            and not row.get("approval")
            and not (isinstance(row.get("pgid"), int) and row["pgid"] > 0)
        )
        if kind == "codex":
            if zab is False:
                if codex == "unchecked":
                    try:
                        codex = codex_active(run)
                    except (OSError, subprocess.SubprocessError):
                        codex = None
                if codex is False:
                    ignored.append("a codex row with the bridge and the daemon down")
                    continue
        else:  # Claude (and anything else): its turns run under the bridge
            if zab is False:
                ignored.append(f"a {kind} row with the bridge down")
                continue
            pgid = row.get("pgid")
            if isinstance(pgid, int) and pgid > 0 and not alive(pgid):
                ignored.append(f"a {kind} row whose process is gone")
                continue
        counted += 1  # live, or can't be shown not to be: age never decides (PM on #236)
        if suspect:
            suspects.append(f"a detached {kind} turn running {int(age // 3600)} h")
    return counted, ignored, suspects


def gather(
    db: str,
    run: Run,
    *,
    proc: str = "/proc",
    now: float | None = None,
    euid: int | None = None,
    login: str = LOGIN,
    hold: str = HOLD,
    alive: Callable[[int], bool] = group_alive,
    warm: str = WARM_DONE,
    spare: bool = False,
    jobs_unknown: str = JOBS_UNKNOWN,
    monotonic: float | None = None,
    config: str = CONFIG,
    dry_run: bool = False,
    job_seen: str = JOB_SEEN,
) -> Facts:
    """Every fact the checks need; what can't be read is recorded, never guessed. On a `spare`,
    a missing zab database is none to read (module doc)."""
    facts = Facts(now=time.time() if now is None else now, uptime=None, load5=None)
    try:
        facts.uptime = read_uptime(proc)
    except (OSError, ValueError, IndexError):
        pass
    try:
        facts.load5 = read_load5(proc)
    except (OSError, ValueError, IndexError):
        pass
    try:
        sessions = ssh_sessions(proc)
        facts.logins = who_logins(run) + [s for s in sessions if s != READ_SESSION]
        facts.reads = [s for s in sessions if s == READ_SESSION]
    except (OSError, subprocess.SubprocessError) as exc:
        facts.errors.append(f"logins ({exc})")
    facts.last_login = clamped(login, facts.now, write=not dry_run)
    facts.last_job = clamped(job_seen, facts.now, write=not dry_run)
    facts.hold_until = read_hold(hold)
    try:
        facts.warming = warm_up_running(run, warm)
    except (OSError, subprocess.SubprocessError):
        facts.warming = None
    if facts.warming is not False and facts.uptime is not None and facts.uptime >= WARM_MAX_S:
        facts.warming = False  # never for good (module doc)
        hours = WARM_MAX_S // 3600
        facts.warm_ignored = f"the first boot's volume warm-up, unfinished after {hours} h up"
    try:
        jobs = jobs_running(run, time.monotonic() if monotonic is None else monotonic)
    except (OSError, subprocess.SubprocessError):
        jobs = None
    facts.running_jobs, facts.jobs_ignored = jobs if jobs is not None else (None, [])
    since = unknown_since(jobs_unknown, facts.now, jobs is None, write=not dry_run)
    if since is not None and facts.now - since >= max_hours(config) * 3600 + JOB_GRACE_S:
        facts.running_jobs = 0  # no job could start since, and any then running has ended
        facts.jobs_ignored = [f"background jobs unknown for {_hours(facts.now - since)}"]
    as_root = (os.geteuid() if euid is None else euid) == 0
    try:
        state = read_db_as_agent(db, run) if as_root else read_db(db)
        if state.get("missing") and spare:  # no bridge yet: no turn, nothing taken in
            return facts
        if state.get("missing"):
            facts.running_turns = None
            facts.errors.append("zab's database: missing")
            return facts
        facts.last_activity = float(state["last"]) if state["last"] is not None else None
        facts.running_turns, facts.ignored_rows, facts.suspect_rows = count_running(
            list(state["running"]), facts.now, run, alive
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        subprocess.SubprocessError,  # a child that timed out
    ) as exc:
        facts.running_turns = None
        facts.errors.append(f"zab's database ({exc})")
    return facts


# ------------------------------------------------------------------- the read user (#507)


def read_keys(text: str) -> str:
    """`zab-monitor`'s `authorized_keys` for ubuntu's `text`: each plain key line (a known type
    first, so no options: `from=`, `command=`, `cert-authority` …) as the forced read, without
    its comment; anything else is left out, so a key never reads where ubuntu's line limits it.
    Lines split at `\\n` alone, as sshd splits them; a `\\r` ends the key as a blank would."""
    lines = []
    for line in text.split("\n"):
        m = _KEY_LINE.match(line.lstrip(" \t"))
        if m and m.group(1) in KEY_TYPES:
            lines.append(f"{READ_OPTIONS} {m.group(1)} {m.group(2)}\n")
    return "".join(dict.fromkeys(lines))


def ubuntu_keys_text(src: str, owner: int | None = None) -> str:
    """ubuntu's `authorized_keys`, as root and with care: its directory (`.ssh`), then the file
    in it, neither through a symlink and both `owner`'s (ubuntu's uid by default), so that no
    link or hard link points the read at another file; never waiting on a FIFO; only a regular
    file of at most `MAX_KEYS_BYTES`. "" when either is missing; OSError for anything else."""
    if owner is None:
        try:
            owner = pwd.getpwnam("ubuntu").pw_uid
        except KeyError:
            raise OSError("no user ubuntu") from None
    folder, name = os.path.split(src)
    try:
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    try:
        if os.fstat(dfd).st_uid != owner:
            raise OSError(f"{folder} is not ubuntu's")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            return ""
    finally:
        os.close(dfd)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{src} is not a regular file")
        if st.st_uid != owner:
            raise OSError(f"{src} is not ubuntu's")
        data = f.read(MAX_KEYS_BYTES + 1)
    if len(data) > MAX_KEYS_BYTES:
        raise OSError(f"{src} is larger than {MAX_KEYS_BYTES} bytes")
    return data.decode(errors="replace")


def sync_read_keys(
    src: str = UBUNTU_KEYS, dst: str = READ_KEYS, owner: int | None = None
) -> bool | None:
    """Rewrite `dst` from `src` (`read_keys`) when it differs, atomically and 0644; None when the
    read user isn't set up (no directory for `dst`). A missing `src` leaves no key, and so does
    one `ubuntu_keys_text` refuses: its OSError is raised once `dst` is empty."""
    if not os.path.isdir(os.path.dirname(dst)):
        return None
    try:
        want = read_keys(ubuntu_keys_text(src, owner))
    except OSError:
        _write_keys(dst, "")  # no key reads while ubuntu's can't be read
        raise
    return _write_keys(dst, want)


def _write_keys(dst: str, want: str) -> bool:
    try:
        if Path(dst).read_text() == want:
            return False
    except FileNotFoundError:
        pass
    tmp = f"{dst}.{os.getpid()}.new"  # a tick and --setup-read-user may overlap
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "w") as f:
        f.write(want)
    os.chmod(tmp, 0o644)  # whatever the umask
    os.replace(tmp, dst)
    return True


def setup_read_user(
    run: Run,
    *,
    home: str = READ_HOME,
    src: str = UBUNTU_KEYS,
    owner: int | None = None,
    chown: Callable = os.chown,
) -> None:
    """As root (push_config.py's `--idle-stop`): the user, with no password and the read as its
    login shell; one that exists gets that shell, no other group and its password locked, then
    this home if it had another, in a call of its own, which usermod refuses while a read runs
    (its uid and primary group stay). Then its home and `.ssh`, root's and 0755, so that neither
    it nor anyone but root writes its keys; then the keys."""
    try:
        entry = pwd.getpwnam(READ_USER)
    except KeyError:
        entry = None
        argv = ["useradd", "--system", "--no-create-home", "--home-dir", home]
        argv += ["--shell", READ_LOGIN]
    else:
        argv = ["usermod", "--shell", READ_LOGIN, "--groups", "", "--lock"]
    run([*argv, READ_USER], check=True, timeout=60)
    if entry is not None and entry.pw_dir != home:
        run(["usermod", "--home", home, READ_USER], check=True, timeout=60)
    for folder in (home, f"{home}/.ssh"):
        os.makedirs(folder, exist_ok=True)
        chown(folder, 0, 0)
        os.chmod(folder, 0o755)  # noqa: S103 - sshd reads the keys as zab-monitor
    sync_read_keys(src, f"{home}/.ssh/authorized_keys", owner)


# ----------------------------------------------------------------------------------- main


def _stamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def verdict(facts: Facts, reasons: Sequence[str], minutes: int) -> str:
    ignored = [*facts.ignored_rows, *([facts.warm_ignored] if facts.warm_ignored else [])]
    ignored += facts.jobs_ignored
    ignoring = f" (ignoring {'; '.join(ignored)})" if ignored else ""
    if reasons:
        return "stay up: " + "; ".join(reasons) + ignoring
    idle = (
        ""
        if facts.last_activity is None
        else f", zab idle {_minutes(facts.now - facts.last_activity)}"
    )
    return (
        f"power off: idle for {minutes} min (no turn, no job, no login, load {facts.load5:.2f}"
        f"{idle})" + ignoring
    )


def record(line: str, *, log: str, last: str, force: bool) -> None:
    """Print the verdict (the journal); append it to the log file when it changed, or `force`."""
    print(line, flush=True)
    body = line.split(" ", 1)[1]
    try:
        previous = Path(last).read_text()
    except OSError:
        previous = ""
    # the reasons with their minute counts change every run; compare the kinds only
    kind = re.sub(r"\d+", "N", body)
    if force or kind != previous:
        try:
            with open(log, "a") as f:
                f.write(line + "\n")
            Path(last).write_text(kind)
        except OSError as exc:
            print(f"cannot write {log}: {exc}", file=sys.stderr)


def main(
    argv: list[str] | None = None,
    *,
    run: Run = subprocess.run,
    proc: str = "/proc",
    now: float | None = None,
    log: str = LOG,
    last: str = LAST,
    login: str = LOGIN,
    hold: str = HOLD,
    euid: int | None = None,
    alive: Callable[[int], bool] = group_alive,
    read_home: str = READ_HOME,
    ubuntu_keys: str = UBUNTU_KEYS,
    keys_owner: int | None = None,
    warm: str = WARM_DONE,
    spare_log: str = SPARE_LOG,
    jobs_unknown: str = JOBS_UNKNOWN,
    monotonic: float | None = None,
    config: str = CONFIG,
    job_seen: str = JOB_SEEN,
) -> int:
    parser = argparse.ArgumentParser(prog="idle_stop.py", description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=int)
    parser.add_argument("--max-load", type=float, default=0.5)
    parser.add_argument(
        "--spare", action="store_true", help="a spare: no zab database yet, its own log (#715)"
    )
    parser.add_argument("--db", default=DB)
    parser.add_argument("--dry-run", action="store_true", help="say what it would do, do nothing")
    parser.add_argument("--read-db", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument(
        "--setup-read-user", action="store_true", help=f"as root: set up {READ_USER} (#507)"
    )
    args = parser.parse_args(argv)
    if args.read_db:  # the child of read_db_as_agent
        print(json.dumps(read_db(args.read_db)))
        return 0
    if args.setup_read_user:
        setup_read_user(run, home=read_home, src=ubuntu_keys, owner=keys_owner)
        return 0
    if args.minutes is None or args.minutes < MIN_MINUTES:
        parser.error(f"--minutes must be at least {MIN_MINUTES}")
    if not args.dry_run:
        try:  # never part of the verdict, whatever goes wrong
            sync_read_keys(ubuntu_keys, f"{read_home}/.ssh/authorized_keys", keys_owner)
        except Exception as exc:
            print(f"cannot copy ubuntu's keys for {READ_USER}: {exc!r}", file=sys.stderr)
    log = spare_log if args.spare else log
    facts = gather(
        args.db, run, proc=proc, now=now, euid=euid, login=login, hold=hold, alive=alive,
        warm=warm, spare=args.spare, jobs_unknown=jobs_unknown, monotonic=monotonic,
        config=config, dry_run=args.dry_run, job_seen=job_seen,
    )  # fmt: skip
    if facts.reads:  # the journal only: they change no verdict
        print(f"not logins: {', '.join(facts.reads)} (status reads, #507)", flush=True)
    if facts.logins:  # the grace after logout counts from the last time one was seen
        try:
            Path(login).touch()
        except OSError as exc:
            print(f"cannot write {login}: {exc}", file=sys.stderr)
    if facts.running_jobs and not args.dry_run:  # the timeout after a job counts from here
        try:
            Path(job_seen).touch()
            os.utime(job_seen, (facts.now, facts.now))
        except OSError as exc:
            print(f"cannot write {job_seen}: {exc}", file=sys.stderr)
    reasons = hold_reasons(facts, args.minutes, args.max_load)
    line = f"{_stamp(facts.now)} {verdict(facts, reasons, args.minutes)}"
    if reasons:
        record(line, log=log, last=last, force=False)
        return 0
    if args.dry_run:
        record(line + " (dry run: not powering off)", log=log, last=last, force=False)
        return 0
    record(line, log=log, last=last, force=True)
    res = run(POWEROFF, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"systemctl poweroff exited {res.returncode}: {res.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
