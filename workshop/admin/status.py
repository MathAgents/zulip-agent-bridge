#!/usr/bin/env python3
"""`status.py --roster roster.csv --tag K=V… [--csv out.csv]` (README.md §3).

Gathers `~/.local/state/zab/preflight.json`, `zab doctor --service-env --json` (what
`push_config.py` runs), `zab status --json` and the service state from every VM over SSH (as
`zab-monitor`, whose sessions idle stop doesn't count as logins, where `push_config.py
--idle-stop` set it up (#507; `READ_SCRIPT`); else as `ubuntu`, running the agent's commands with
`sudo -u agent`, including the read of `preflight.json`, since `agent`'s home is not traversable
by `ubuntu`) and prints one table:
participant, host, Codex login, Claude login, git push, math tools, bridge, sentinel, active
sessions, last error, then one summary line (who is signed in to which agent). Unreachable
hosts are rows, not crashes. Three more columns show the VM's load (`/proc/loadavg`,
1-minute average over the CPU count), memory in use (`MemTotal` - `MemAvailable`) and the
root file system's use (`df -Pk /`), read with no sudo of their own; each gets a `!` when
the VM is under strain (load at least the CPU count, under 10% of memory available, disk 90%
full), and a line before the summary names those VMs (Drew, Sat Oct 10). A disk under strain
also names its free space, `92% ! (15 GB free)`: `df`'s available space, in binary GB as
`df -h` shows them (`disk_gb`, #552). A bridge whose only failing doctor checks are the
agents' sign-ins reads `active, sign-in pending`, not `active, doctor FAIL`
(`admin_common.sign_in_pending`); on a row with no `zulip_user_id` yet, the bots' allowlist
checks are pending too, as in push_config.py: `active, owner+sign-in pending` (#401;
`admin_common.allowlist_pending`). The `sentinel` column (#588) is the doctor's
`<bot>:sentinel` checks: `ok`, or the states of those that warn (`not installed`, `down`,
`no mark`, `stale`, in which DM `!restart` is unavailable and the bridge answers DM `!vm`
itself; `unknown`, the doctor couldn't ask systemd); `?` from a zab before #588. Those
checks read the unit's state and the sentinel's marks, nothing that idle stop counts, and
they come with the doctor's read, which is `zab-monitor`'s where idle stop is set up.

Kept VMs stop themselves when idle (issue #207), so EC2 is read first (`DescribeInstances` by
the workshop tag, once per region in the `region` column of the rows that have an `ip`,
us-west-2 when empty, for a row launched before launch.py recorded it; the instance whose
public address is the row's `ip`, the one SSH would try; a region that fails is said, and the
others are still read). A VM that EC2 says is
stopped, stopping or starting is reported from that state alone, with no SSH attempt, which would
wait out `ConnectTimeout` or a shutdown (#439): its bridge column reads `stopped`, `stopping` or
`starting`, and the summary counts those apart. The others are read over SSH; one that SSH can't
reach is looked up again, since it may have stopped meanwhile, and stays `unreachable` unless EC2
now explains it. That needs boto3 and a credential (`AWS_PROFILE=… uv run --with boto3 python
workshop/admin/status.py …`); without them every VM is tried over SSH, and when one is
unreachable a line says why the lookup failed. `--no-ec2` skips the lookups.

Nobody runs `zab preflight` since participants have no SSH (PM on issue #36, Wed 13:00), so
each column takes the first source that has its checks: Codex login, the doctor's `codex:login`
(FAIL when only a failed `codex:connection` is there), else preflight; Claude login, the
doctor's `claude account|auth|config <name>` checks, else preflight; git push, preflight, else
the roster's `repo_status` (`provision_repos.py` sets `ok` after the VM pushed); math tools, the
doctor's `service:python` and `service:gp` (FAIL when gp is "not found"), else preflight. The
live doctor comes first, so a stale `preflight.json` cannot hide a sign-out. From the doctor, a
login column reads `ok` when its checks pass, `no` when only sign-in checks fail (`codex:login`,
`claude account <name>`: not signed in yet), and `FAIL` for any other failure, which wins over
`no` (Wed 13:35).
Run it every 30 minutes during the workshop (README.md §3).

`--json` prints the same rows as JSON instead (docs/ADMIN.md §4.1: the admin bot and the console
read it), with `?` as null and four things the table does not show: the zab version (`zab
--version`), idle stop (whether `zab-idle-stop.timer` is enabled and active, its minutes, and the
last line of `/var/log/zab-idle-stop.log`, its last verdict), the last activity (`zab status
--json`'s `last_activity`, newer than 0.2.3; null before), and the root file system's size, use
and available space in bytes (`disk`, from the same `df -Pk /` line as the percentage, #552).
`--tag KEY=VALUE` (repeatable, required: #808) is the EC2 lookup's scope, as for the
`workshop/aws/` scripts. A VM's owner controls what it answers: in the JSON, a value zab would
not write (a name that is not a config name, an implausible time or number) is null, long lists
are cut, and the output is strict JSON (no NaN).

The remote command is a fixed literal; nothing from the roster reaches a shell.
Standalone: `uv run python workshop/admin/status.py …`.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    Runner,
    RunResult,
    allowlist_pending,
    first_line,
    format_table,
    host_of,
    load_roster,
    pending_label,
    run_parallel,
    select_rows,
    sign_in_check,
    sign_in_pending,
    slug_of,
    ssh_argv,
    subprocess_runner,
)
from aws_common import (  # on the path through admin_common
    Ec2,
    Scope,
    add_tag_argument,
    parse_tags,
    scope_tags,
)

#: The region of a row with a VM but no region recorded: one launched before launch.py
#: recorded it, which the first fleet's were, in us-west-2. launch.py records it for every
#: VM it makes (#808: no script takes it as the workshop's region any more). As the admin
#: bot reads such a row (`zab.admin.actions`).
UNRECORDED_REGION = "us-west-2"
#: EC2 states that explain an unreachable VM, and how the bridge column shows them.
EC2_LABELS = {"stopped": "stopped", "stopping": "stopping", "pending": "starting"}

MARK = "---ZAB-"
#: What `push_config.py --idle-stop` installs (`IDLE_STOP_UNIT` in `_UNIT_DIR`; `idle_stop.LOG`).
IDLE_UNIT = "zab-idle-stop"
IDLE_LOG = "/var/log/zab-idle-stop.log"
#: Each section is bracketed by markers so a missing file or a failing command cannot shift the
#: others. `2>/dev/null` keeps the sections to the JSON the tools print. `run` runs one of the
#: agent's commands as `agent`, with its runtime directory: through sudo from `ubuntu`
#: (`STATUS_CMD`), directly in `READ_SCRIPT`, which runs as `agent`.
_ENV = "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus"
_RUN = _ENV + ' bash -lc "cd / && $1" 2>/dev/null; }; '
_SECTIONS = (
    f"echo {MARK}PREFLIGHT; run 'cat ~/.local/state/zab/preflight.json'; echo; "
    f"echo {MARK}DOCTOR; run 'zab doctor --service-env --json'; echo; "
    f"echo {MARK}STATUS; run 'zab status --json'; echo; "
    f"echo {MARK}VERSION; run 'zab --version'; echo; "
    f"echo {MARK}SERVICE; run 'systemctl --user is-active zab'; echo; "
    f"echo {MARK}IDLE; "
    f'echo "enabled=$(systemctl is-enabled {IDLE_UNIT}.timer 2>/dev/null)"; '
    f'echo "active=$(systemctl is-active {IDLE_UNIT}.timer 2>/dev/null)"; '
    f"echo \"minutes=$(grep -o -- '--minutes [0-9]*' /etc/systemd/system/{IDLE_UNIT}.service "
    '2>/dev/null)"; '
    f'echo "last=$(tail -n 1 {IDLE_LOG} 2>/dev/null)"; echo; '
    f"echo {MARK}LOAD; cat /proc/loadavg; nproc; "
    "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; df -Pk / | tail -n 1; echo; "
    f"echo {MARK}END"
)
STATUS_CMD = "uid=$(id -u agent); run() { sudo -u agent -H " + _RUN + _SECTIONS
#: The read as `zab-monitor` (#507; idle_stop.py's module doc), whose sessions idle stop doesn't
#: count as logins: push_config.py installs this as `idle_stop.READ_SCRIPT_PATH`, which that
#: user's forced command runs as `agent`. Where the user can't log in (no idle stop, or one set
#: up before #507) or its read fails on the VM, `STATUS_CMD` reads as `ubuntu`, a login.
READ_USER = "zab-monitor"
READ_SCRIPT = (
    "#!/bin/bash\n# zab's status read (workshop/admin/status.py, #507), as agent\n"
    '[ "$(id -un)" = agent ] || { echo "status_read.sh: run it as agent" >&2; exit 2; }\n'
    "uid=$(id -u); run() { " + _RUN + _SECTIONS + "\n"
)
#: What the read user asks for: its forced command runs instead (sshd keeps this in
#: `SSH_ORIGINAL_COMMAND`).
READ_REQUEST = "status"
COLUMNS = [
    "participant",
    "host",
    "codex login",
    "claude login",
    "git push",
    "math tools",
    "bridge",
    "sentinel",
    "sessions",
    "load",
    "mem",
    "disk",
    "last error",
]


@dataclass(slots=True)
class VmStatus:
    slug: str
    host: str = "-"
    codex_login: str = "?"
    claude_login: str = "?"
    git_push: str = "?"
    math_tools: str = "?"
    bridge: str = "?"
    sentinel: str = "?"  # sentinel_state()
    sessions: str = "?"
    load: str = "?"
    mem: str = "?"
    disk: str = "?"
    last_error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # --json only (ADMIN.md §4.1), never table columns
    zab_version: str | None = None
    last_activity: float | None = None
    idle_stop: dict[str, Any] | None = None
    active_sessions: int | None = None
    running_turns: list[dict[str, Any]] | None = None  # running_turns_json()
    doctor_failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)  # load_numbers()

    def row(self) -> list[str]:
        return [
            self.slug,
            self.host,
            self.codex_login,
            self.claude_login,
            self.git_push,
            self.math_tools,
            self.bridge,
            self.sentinel,
            self.sessions,
            self.load,
            self.mem,
            self.disk,
            self.last_error,
        ]

    def strained(self) -> list[str]:
        """The resources marked `!` (under strain), by column name."""
        return [
            name
            for name, value in (("load", self.load), ("mem", self.mem), ("disk", self.disk))
            if "!" in value.split()  # `9.7/8 !`, `92% ! (15 GB free)`
        ]


def split_sections(stdout: str) -> dict[str, str]:
    """`{"PREFLIGHT": "...", "DOCTOR": "...", "STATUS": "...", "SERVICE": "..."}`."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(MARK):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[len(MARK) :].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None and current != "END":
        sections[current] = "\n".join(buf).strip()
    return sections


def _json(text: str) -> Any:
    """The section's JSON, or None. `ValueError`, not only `JSONDecodeError`: an integer of more
    than 4300 digits raises the plain one (#327); and `RecursionError`, for JSON nested
    deeper than the parser's stack (the review of #576)."""
    try:
        return json.loads(text) if text else None
    except (ValueError, RecursionError):
        return None


def _list(value: Any) -> list[Any]:
    """A list the VM sent, or `[]` for anything else (its owner controls that text, #327)."""
    return value if isinstance(value, list) else []


def _check(checks: list[dict[str, Any]], *names: str) -> str:
    """`ok`/`FAIL`/`?` for the checks whose name matches any of `names` (all must pass)."""
    hits = [c for c in checks if any(n in str(c.get("name", "")).lower() for n in names)]
    if not hits:
        return "?"
    return "ok" if all(bool(c.get("ok")) for c in hits) else "FAIL"


def _named(doctor: Any, *prefixes: str) -> list[dict[str, Any]]:
    """The doctor's checks whose name is one of `prefixes` or starts with it."""
    if not isinstance(doctor, list):
        return []
    return [
        c for c in doctor if isinstance(c, dict) and str(c.get("name", "")).startswith(prefixes)
    ]


def _passed(hits: list[dict[str, Any]]) -> str:
    return "ok" if all(bool(c.get("ok")) for c in hits) else "FAIL"


def _login(hits: list[dict[str, Any]]) -> str:
    """A login column: `ok` when every check passes; `no` when every failing one is a sign-in
    check (not signed in yet, as on every VM until the participant's `!login`); `FAIL` for any
    other failure, which wins over `no` (Drew via the PM on issue #36, Wed 13:35)."""
    failing = [c for c in hits if not c.get("ok")]
    if not failing:
        return "ok"
    return "no" if all(sign_in_check(str(c.get("name", ""))) for c in failing) else "FAIL"


def doctor_columns(doctor: Any) -> dict[str, str]:
    """Codex login, Claude login and math tools from `zab doctor --service-env --json`, for the
    columns whose checks it has (PM on issue #36, Wed 13:00)."""
    out: dict[str, str] = {}
    login = _named(doctor, "codex:login")
    connection = _named(doctor, "codex:connection")
    if login:
        out["codex_login"] = _login(login + connection)
    elif connection and not all(c.get("ok") for c in connection):
        out["codex_login"] = "FAIL"  # the daemon is down: no login check at all
    # `claude account <name>` is the sign-in; `claude auth|config <name>` are operational
    claude = _named(doctor, "claude account ", "claude auth ", "claude config ")
    if claude:
        out["claude_login"] = _login(claude)
    python, gp = _named(doctor, "service:python"), _named(doctor, "service:gp")
    # service:gp is always ok (informational); its detail says whether gp is on the PATH. The
    # doctor emits service:python only for a config with a service environment, so without it
    # the doctor can report a failure but not success (codex on #186): preflight, or `?`, stays
    missing = any(str(c.get("detail", "")).startswith("not found") for c in gp)
    if missing or (python and _passed(python) == "FAIL"):
        out["math_tools"] = "FAIL"
    elif python:
        out["math_tools"] = "ok"
    return out


#: The sentinel's states that are not `ok` (`zab.tools.sentinel.UNHEALTHY`, a copy that a test
#: keeps equal): the first words of a warning `<bot>:sentinel` check of the doctor (#588).
SENTINEL_STATES = ("not installed", "down", "no mark", "stale", "unknown")


def sentinel_state(doctor: Any) -> str:
    """The `sentinel` column (#588), from the doctor's `<bot>:sentinel` checks, which read the
    unit and the marks on the VM, nothing idle stop counts: `ok` when none of them warns; else
    the states of those that do (`down`, `stale, no mark`), `warn` for one this copy doesn't
    know; `?` without such a check (a zab before #588, or no doctor output)."""
    checks = [c for c in _list(doctor) if isinstance(c, dict)]
    hits = [c for c in checks if str(c.get("name", "")).endswith(":sentinel")]
    states: list[str] = []
    for check in hits:
        if check.get("ok") and not check.get("warning"):
            continue
        state = str(check.get("detail", "")).split(":", 1)[0]
        state = state if state in SENTINEL_STATES else "warn"
        states += [state] * (state not in states)
    return (", ".join(states) or "ok") if hits else "?"


def load_numbers(text: str) -> dict[str, Any]:
    """`load1` and `cpus`, `mem_total_kib` and `mem_available_kib`, and `disk_percent` with
    `disk_size_kib`, `disk_used_kib` and `disk_available_kib` from the LOAD section:
    `/proc/loadavg`, `nproc`, the two `/proc/meminfo` lines and `df -Pk /`'s data line (its
    1024-blocks, Used, Available and Capacity), each group only when it is consistent. A part
    that is missing or odd is left out; the percentage is kept without the sizes."""
    load1: float | None = None
    ncpu: int | None = None
    total = avail = None
    disk: int | None = None
    blocks: list[int] = []
    for line in text.splitlines():
        parts = line.split()
        try:
            if line.startswith("MemTotal:") and len(parts) >= 2:
                total = int(parts[1])  # KiB
            elif line.startswith("MemAvailable:") and len(parts) >= 2:
                avail = int(parts[1])
            elif len(parts) == 5 and "/" in parts[3] and load1 is None:  # /proc/loadavg
                load1 = float(parts[0])
            elif len(parts) == 1 and parts[0].isdigit() and ncpu is None:  # nproc
                ncpu = int(parts[0])
            elif len(parts) >= 6 and parts[4].endswith("%") and disk is None:  # df -Pk
                disk = int(parts[4][:-1])
                blocks = [int(p) for p in parts[1:4]]  # KiB: size, used, available
        except ValueError:
            continue
    out: dict[str, Any] = {}
    if load1 is not None and ncpu:
        out.update(load1=load1, cpus=ncpu)
    if total and avail is not None and 0 <= avail <= total:
        out.update(mem_total_kib=total, mem_available_kib=avail)
    if disk is not None:
        out["disk_percent"] = disk
    if blocks and blocks[0] > 0 and all(0 <= b <= blocks[0] for b in blocks[1:]):
        out.update(disk_size_kib=blocks[0], disk_used_kib=blocks[1], disk_available_kib=blocks[2])
    return out


def disk_gb(n: int) -> str:
    """Bytes as binary "GB" (2**30 bytes), rounded up as `df -h` rounds them: to a tenth below
    10 GB, whole from there; `0` for none. The disk's unit (#552) is memory's and `!vm`'s (#204;
    SPEC §13.5: "GB means binary gigabytes"), and `df -h`'s; `!vm` rounds
    its size to the nearest GB, so the two can differ by one (the fleet's 200 GiB root, 193.6
    GiB, reads 194 in both). The console and `!status` use the same (`zab.admin.pages.disk_gb`;
    a test keeps the two equal)."""
    if n <= 0:
        return "0"
    tenths = -(-n * 10 // 2**30)
    return f"{tenths // 10}.{tenths % 10}" if tenths < 100 else str(-(-n // 2**30))


def load_columns(text: str) -> dict[str, str]:
    """`load`, `mem` and `disk` for the table, from `load_numbers`; a missing part stays `?`. A
    disk under strain names its available space when `df` gave it: `92% ! (15 GB free)`."""
    n = load_numbers(text)
    out: dict[str, str] = {}
    if "load1" in n:
        load1, ncpu = n["load1"], n["cpus"]
        out["load"] = f"{load1:.1f}/{ncpu}" + (" !" if load1 >= ncpu else "")
    if "mem_total_kib" in n:
        total, avail = n["mem_total_kib"], n["mem_available_kib"]
        gib = 1024 * 1024
        out["mem"] = f"{(total - avail) / gib:.0f}/{total / gib:.0f}G" + (
            " !" if avail < 0.1 * total else ""
        )
    if "disk_percent" in n:
        disk = n["disk_percent"]
        out["disk"] = f"{disk}%"
        if disk >= 90:
            out["disk"] += " !"
            if "disk_available_kib" in n:
                out["disk"] += f" ({disk_gb(n['disk_available_kib'] * 1024)} GB free)"
    return out


_VERSION_RE = re.compile(r"^\d+\.\d+[0-9A-Za-z.+-]*$")


def zab_version(text: str) -> str | None:
    """`zab --version`'s answer, when it looks like a version."""
    line = first_line(text, 40)
    return line if _VERSION_RE.match(line) else None


def idle_stop_state(text: str) -> dict[str, Any] | None:
    """The IDLE section: whether the timer is enabled and active, its minutes (the unit's
    `--minutes N`), and the log's last line, which is the last verdict; None without it."""
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if not values:
        return None
    minutes = re.fullmatch(r"--minutes ([0-9]{1,6})", values.get("minutes", "").strip())
    return {
        "enabled": values.get("enabled", "").strip() == "enabled",
        "active": values.get("active", "").strip() == "active",
        "minutes": int(minutes.group(1)) if minutes else None,
        "last_verdict": first_line(values.get("last", ""), 300) or None,  # cut, and redacted
    }


#: A bot or backend name as zab's config allows it (`[A-Za-z0-9_-]+`, config.py), short.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
#: --json lists at most this many running turns and doctor failures per VM. It is the most
#: turns a workshop VM runs, 10 per backend (push_config.py's `max_concurrent_turns`, #919):
#: raise it with that, or `!status` silently lists fewer turns than run (planning's `MAX_TURNS`;
#: `test_status_lists_every_turn_a_vm_may_run` ties them).
MAX_ITEMS = 20


def _name(value: Any) -> str | None:
    return value if isinstance(value, str) and _NAME_RE.fullmatch(value) else None


def running_turns_json(sessions: Any, running: Any) -> list[dict[str, Any]] | None:
    """`{"bot", "backend", "started_at"}` per running turn of `zab status --json`; None unless
    `running_turns` is a list of objects. The VM's owner controls that text, so a field that is
    not what zab writes (a short name, a plausible epoch) is null, and the list is cut."""
    if not isinstance(running, list) or not all(isinstance(t, dict) for t in running):
        return None
    by_id = {
        s["id"]: s
        for s in (sessions if isinstance(sessions, list) else [])
        if isinstance(s, dict) and isinstance(s.get("id"), str)
    }
    out = []
    for turn in running[:MAX_ITEMS]:
        sid = turn.get("session_id")
        session = by_id.get(sid, {}) if isinstance(sid, str) else {}
        out.append(
            {
                "bot": _name(session.get("bot")),
                "backend": _name(session.get("backend")),
                "started_at": iso_time(turn.get("started_at")),
            }
        )
    return out


def strain_line(rows: list[VmStatus]) -> str | None:
    """`under strain: alice (mem), bob (load, disk)`, or None when no VM is."""
    hits = [f"{r.slug} ({', '.join(r.strained())})" for r in rows if r.strained()]
    return "under strain: " + ", ".join(hits) if hits else None


def summarize(row: Mapping[str, str], stdout: str) -> VmStatus:
    """One table row from the four sections (README.md §3)."""
    st = VmStatus(slug=slug_of(row), host=row.get("host") or row.get("ip") or "-")
    sections = split_sections(stdout)
    preflight = _json(sections.get("PREFLIGHT", ""))
    checks = preflight.get("checks", []) if isinstance(preflight, dict) else []
    if isinstance(checks, list):
        checks = [c for c in checks if isinstance(c, dict)]  # the VM's: anything else skipped
        # `codex login status` can pass with a stale token; the smoke request is the real test
        st.codex_login = _check(checks, "codex login", "codex smoke")
        st.claude_login = _check(checks, "claude")
        st.git_push = _check(checks, "git push")
        st.math_tools = _check(checks, "gp", "cypari2")
    if st.git_push == "?":
        # `provision_repos.py` sets `ok` once `zab setup-repo` pushed from this VM
        repo_status = (row.get("repo_status") or "").strip()
        if repo_status:
            st.git_push = "ok" if repo_status == "ok" else "FAIL"
    doctor = _json(sections.get("DOCTOR", ""))
    if isinstance(doctor, list):
        doctor = [c for c in doctor if isinstance(c, dict)]  # likewise
    for column, value in doctor_columns(doctor).items():
        setattr(st, column, value)  # live, so ahead of a possibly stale preflight.json
    for column, value in load_columns(sections.get("LOAD", "")).items():
        setattr(st, column, value)
    st.metrics = load_numbers(sections.get("LOAD", ""))
    st.zab_version = zab_version(sections.get("VERSION", ""))
    st.idle_stop = idle_stop_state(sections.get("IDLE", ""))
    st.sentinel = sentinel_state(doctor)
    status = _json(sections.get("STATUS", ""))
    service = sections.get("SERVICE", "").strip()
    if service:
        st.bridge = (
            service if service in ("active", "inactive", "failed") else first_line(service, 20)
        )
    elif doctor is None and status is None:
        st.bridge = "no zab"
    errors: list[str] = []
    if isinstance(doctor, list):
        failing = [c for c in doctor if not c.get("ok") and not c.get("warning")]
        # an unbound row's allowlist: the participant hasn't joined Zulip yet (#401)
        bad = [c for c in failing if not allowlist_pending(c, row)]
        owner, signing = len(bad) < len(failing), sign_in_pending(bad)
        if signing or (owner and not bad):
            # only the agents' sign-ins are missing, as on every VM until the participants'
            # `!login` (PM on #36, Mon 13:45; the same rule as push_config.py), and the owner
            # on an unbound row
            if st.bridge == "active":
                st.bridge = f"active, {pending_label(owner=owner, sign_in=signing)}"
        else:
            if bad and st.bridge == "active":
                st.bridge = "active, doctor FAIL"
            errors.extend(f"{c.get('name', '?')}: {c.get('detail', '')}" for c in bad)
            st.doctor_failures = [first_line(e, 160) for e in errors[:MAX_ITEMS]]
    if isinstance(status, dict):
        if "error" in status:
            errors.append(f"status: {status['error']}")
        sessions = [s for s in _list(status.get("sessions")) if isinstance(s, dict)]
        active = [s for s in sessions if s.get("state") in ("idle", "running", "awaiting_approval")]
        running = _list(status.get("running_turns"))
        st.sessions = f"{len(active)} ({len(running)} running)"
        st.active_sessions = len(active)
        st.running_turns = running_turns_json(status.get("sessions"), status.get("running_turns"))
        last = status.get("last_activity")  # only in zab newer than 0.2.3 (ADMIN.md §9 V7)
        st.last_activity = float(last) if iso_time(last) else None
    if not errors and isinstance(checks, list):
        failed = [c for c in checks if not c.get("ok") and not c.get("warning")]
        errors.extend(f"preflight {c.get('name', '?')}: {c.get('detail', '')}" for c in failed)
    st.last_error = first_line(errors[0], 80) if errors else ""
    st.raw = {"preflight": preflight, "doctor": doctor, "status": status, "service": service}
    return st


#: ssh's failures before it reaches the VM's sshd: a second try would fail alike.
_UNREACHABLE = ("connect to host", "Host key verification failed")


def as_ubuntu(res: RunResult) -> bool:
    """Whether to read as `ubuntu` after the read user's try failed: unless it timed out or never
    reached the VM (`_UNREACHABLE`), which would only wait as long again. So a VM without the
    user, or whose sshd, PAM or sudo refuse it, is read as before."""
    return res.exit_code != -1 and not any(s in res.stderr for s in _UNREACHABLE)


def gather_vm(
    row: Mapping[str, str], runner: Runner, *, known_hosts: str = str(DEFAULT_KNOWN_HOSTS)
) -> VmStatus:
    """One VM's row: read as `READ_USER`, else as `ubuntu` (`as_ubuntu`)."""
    try:
        host = host_of(row)  # the roster's ip; host keys pinned to known_hosts (README.md §0)
    except ValueError as exc:
        return VmStatus(slug=slug_of(row), last_error=str(exc))
    res = runner(
        ssh_argv(host, READ_REQUEST, user=READ_USER, known_hosts=known_hosts), timeout_s=180
    )
    if MARK not in res.stdout and as_ubuntu(res):
        res = runner(ssh_argv(host, STATUS_CMD, known_hosts=known_hosts), timeout_s=180)
    if not res.ok and MARK not in res.stdout:
        st = VmStatus(slug=slug_of(row), host=host)
        st.bridge = "unreachable"
        st.last_error = first_line(res.stderr, 60) or f"ssh exit {res.exit_code}"
        return st
    st = summarize(dict(row, host=host), res.stdout)
    return st


def region_of(row: Mapping[str, str]) -> str:
    return (row.get("region") or "").strip() or UNRECORDED_REGION


def instance_ips(inst: Mapping[str, Any]) -> set[str]:
    """The public addresses of an instance: its Elastic IP stays listed while it is stopped."""
    ips = {inst.get("PublicIpAddress") or ""}
    for nic in inst.get("NetworkInterfaces") or []:
        ips.add((nic.get("Association") or {}).get("PublicIp") or "")
    return ips - {""}


def ec2_states(
    regions: set[str], make_ec2: Any = None, *, tag: Scope
) -> tuple[dict[str, str], str]:
    """`{public ip: state}` for the workshop's instances, and "" or why the lookup failed (no
    boto3, no credential, no permission), in which case the result is partial: a region that
    fails is said, and the others are still read. One reason for every region is said alone;
    otherwise each reason names its regions.

    Keyed by address, not by the `participant` tag: after a hand-over the roster's `ip` is a
    spare's (ORGANIZERS §7.2), whose tag still names the spare, while the participant's broken
    original may be stopped under her name (codex on #220). Only the instance SSH tried can explain
    why SSH failed."""
    states: dict[str, str] = {}
    failed: dict[str, list[str]] = {}  # why → its regions
    for region in sorted(regions):
        try:
            ec2 = (make_ec2 or Ec2)(region)
            for inst in ec2.find_instances(scope_tags(tag), (*EC2_LABELS, "running")):
                for ip in instance_ips(inst):
                    states[ip] = inst["State"]["Name"]
        except Exception as exc:  # boto3/botocore errors: say why, read the other regions
            why = f"{type(exc).__name__}: {first_line(str(exc), 100)}"
            failed.setdefault(why, []).append(region)
    if list(failed.values()) == [sorted(regions)]:  # no boto3, no credential: all alike
        return states, next(iter(failed))
    return states, "; ".join(f"{why} (in {', '.join(where)})" for why, where in failed.items())


def ec2_label(row: Mapping[str, str], states: dict[str, str]) -> str:
    """`stopped`, `stopping` or `starting` when EC2 says the row's instance is so; else ""."""
    return EC2_LABELS.get(states.get((row.get("ip") or "").strip(), ""), "")


def gather_or_down(
    row: Mapping[str, str],
    runner: Runner,
    states: dict[str, str],
    *,
    known_hosts: str = str(DEFAULT_KNOWN_HOSTS),
) -> VmStatus:
    """`gather_vm`, unless EC2 already said the VM is not running (`states`, read first): then
    its row from that state alone, with no SSH attempt (#439)."""
    label = ec2_label(row, states)
    if not label:
        return gather_vm(row, runner, known_hosts=known_hosts)
    try:
        host = host_of(row)
    except ValueError:
        host = "-"  # never: EC2 matched the row's ip
    return VmStatus(slug=slug_of(row), host=host, bridge=label)


def mark_stopped(
    results: list[VmStatus], rows: list[Mapping[str, str]], states: dict[str, str]
) -> None:
    """An unreachable VM whose instance is stopped, stopping or starting says so (issue #207)."""
    for st, row in zip(results, rows, strict=True):
        if st.bridge != "unreachable":
            continue
        label = ec2_label(row, states)
        if label:
            st.bridge = label
            st.last_error = ""


def summary_counts(rows: list[VmStatus]) -> dict[str, int]:
    """Who is signed in to which agent (PM on issue #36, Wed 13:00): each reachable row in exactly
    one of four buckets, `ok` against anything else (`?` and `FAIL` alike)."""
    keys = ("both", "codex_only", "claude_only", "neither", "unreachable", "stopped")
    n = dict.fromkeys((*keys, "doctor_fail"), 0)
    for r in rows:
        if r.bridge == "active, doctor FAIL":
            n["doctor_fail"] += 1
        if r.bridge in ("unreachable", "no zab"):
            n["unreachable"] += 1
            continue
        if r.bridge in EC2_LABELS.values():
            n["stopped"] += 1
            continue
        has_codex, has_claude = r.codex_login == "ok", r.claude_login == "ok"
        if has_codex and has_claude:
            n["both"] += 1
        elif has_codex:
            n["codex_only"] += 1
        elif has_claude:
            n["claude_only"] += 1
        else:
            n["neither"] += 1
    return n


def summary_line(rows: list[VmStatus]) -> str:
    n = summary_counts(rows)
    return (
        f"signed in: {n['both']} both, {n['codex_only']} Codex only, {n['claude_only']} Claude "
        f"only, {n['neither']} neither; {n['unreachable']} unreachable, {n['stopped']} stopped, "
        f"{n['doctor_fail']} doctor FAIL"
    )


#: 2100-01-01: a later "epoch" from a VM is not a time zab wrote.
_LAST_EPOCH = 4102444800


def _bounded(value: Any, top: float) -> int | float | None:
    """A number from VM output when it is finite and in [0, top], else None (strict JSON)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if 0 <= value <= top else None  # NaN fails both comparisons


def iso_time(epoch: Any) -> str | None:
    """`2026-10-14T17:05:00Z` for epoch seconds; None for anything else."""
    if _bounded(epoch, _LAST_EPOCH) is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def vm_json(st: VmStatus) -> dict[str, Any]:
    """One VM for `--json` (ADMIN.md §4.1): the table's values with `?` as null, and numbers
    where the table shows text; memory in bytes, so that the console can show GB (#204), and
    the root file system's size, use and available space, in bytes too (`disk`, #552)."""

    def known(value: str) -> str | None:
        return None if value == "?" else value

    m = st.metrics  # the VM's own text: implausible numbers are null, never NaN or Infinity
    load, cpus = _bounded(m.get("load1"), 1e6), _bounded(m.get("cpus"), 1e6)
    total = _bounded(m.get("mem_total_kib"), 2**40)  # 1 PiB
    memory = None
    if total is not None:  # load_numbers keeps MemAvailable within [0, MemTotal]
        used = total - m["mem_available_kib"]
        memory = {"used_bytes": used * 1024, "total_bytes": total * 1024}
    size = _bounded(m.get("disk_size_kib"), 2**40)  # 1 PiB
    disk = None
    if size is not None:  # load_numbers keeps Used and Available within [0, the size]
        disk = {
            "size_bytes": size * 1024,
            "used_bytes": m["disk_used_kib"] * 1024,
            "available_bytes": m["disk_available_kib"] * 1024,
        }
    return {
        "slug": st.slug,
        "host": st.host,
        "bridge": known(st.bridge),
        "sentinel": known(st.sentinel),  # new in #588: older readers ignore it
        "zab_version": st.zab_version,
        "codex_login": known(st.codex_login),
        "claude_login": known(st.claude_login),
        "git_push": known(st.git_push),
        "math_tools": known(st.math_tools),
        "active_sessions": st.active_sessions,
        "running_turns": st.running_turns,
        "last_activity": iso_time(st.last_activity),
        "load": None if load is None or cpus is None else {"load1": load, "cpus": cpus},
        "memory": memory,
        "disk_percent": _bounded(m.get("disk_percent"), 100),
        "disk": disk,  # new in #552: older readers ignore it
        "strained": st.strained(),
        "idle_stop": st.idle_stop,
        "doctor_failures": st.doctor_failures,
        "last_error": st.last_error or None,
    }


def fleet_json(rows: list[VmStatus], ec2_error: str = "") -> dict[str, Any]:
    """`--json`'s whole answer. `schema` changes when a field changes its meaning. `summary` is
    the table's last line: `unreachable` counts the VMs SSH can't reach and those without zab
    (`no zab`); `stopped`, those EC2 says are stopped, stopping or starting."""
    return {
        "schema": 1,
        "vms": [vm_json(r) for r in rows],
        "summary": summary_counts(rows),
        "ec2_error": ec2_error or None,
    }


def write_csv(path: str, rows: list[VmStatus]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow(r.row())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="status.py",
        description="Gather preflight, doctor and status from every VM (README.md §3).",
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--only", action="append", metavar="SLUG")
    parser.add_argument("--csv", help="also write the table as CSV here")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument(
        "--known-hosts",
        default=str(DEFAULT_KNOWN_HOSTS),
        help="the VMs' host keys as recorded by scan_hosts.py (pinned; nothing else is trusted)",
    )
    parser.add_argument(
        "--no-ec2",
        action="store_true",
        help="don't read EC2 first, nor for unreachable VMs: SSH tries every VM (EC2 needs boto3 "
        "and a credential)",
    )
    add_tag_argument(parser)
    parser.add_argument(
        "--json", action="store_true", help="print JSON (docs/ADMIN.md §4.1), not the table"
    )
    args = parser.parse_args(argv)
    try:
        scope = parse_tags(args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        rows = select_rows(load_roster(args.roster), args.only)
    except ValueError as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 1
    states: dict[str, str] = {}
    problem = ""
    if not args.no_ec2:  # first, so that no SSH waits on a VM that isn't running (#439)
        # the regions of rows with a VM: a row without an `ip` matches no instance
        launched = {region_of(row) for row in rows if (row.get("ip") or "").strip()}
        states, problem = ec2_states(launched, tag=scope)
    results = run_parallel(
        rows,
        lambda row: gather_or_down(row, subprocess_runner, states, known_hosts=args.known_hosts),
        parallel=args.parallel,
        on_error=lambda row, exc: VmStatus(
            slug=slug_of(row), last_error=f"{type(exc).__name__}: {exc}"
        ),
    )
    unreachable = [row for row, r in zip(rows, results, strict=True) if r.bridge == "unreachable"]
    if not unreachable:
        problem = ""  # the lookup explained nothing SSH needed: no line about it, as before
    elif not args.no_ec2:
        if not problem:  # stopped since the first lookup, or stopping while SSH tried
            again, problem = ec2_states({region_of(row) for row in unreachable}, tag=scope)
            states = {**states, **again}
        mark_stopped(results, rows, states)
        if problem:
            print(
                f"EC2 lookup failed ({problem}): unreachable VMs may be stopped; run with "
                "`uv run --with boto3` and AWS_PROFILE set, or pass --no-ec2",
                file=sys.stderr,
            )
    if args.json:
        print(json.dumps(fleet_json(results, problem), indent=2))
    else:
        print(format_table(COLUMNS, [r.row() for r in results]))
        strain = strain_line(results)
        if strain:
            print(strain)
        print(summary_line(results))
    if args.csv:
        write_csv(args.csv, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
