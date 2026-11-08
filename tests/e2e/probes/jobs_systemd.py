"""#655's probe (the design's VERIFY V1 to V5): background jobs as transient services of
`agent`'s systemd user manager, started the way `zab job start` will start them.

Run on the test VM as `agent`, with the installed zab's Python, which reads this file from
stdin (nothing is copied to the VM). It takes about 15 minutes:

    ssh … ubuntu@VM 'sudo -u agent -H env -C /home/agent \\
        /home/agent/.local/share/uv/tools/zulip-agent-bridge/bin/python -I -' \\
        < tests/e2e/probes/jobs_systemd.py

It restarts `zab.service` and the Codex daemon once each (`--skip restarts` leaves them), and
runs short turns on the VM's own agents: three of Claude and two of Codex. Each phase prints
JSON lines; a line with `check` and `ok` is a verdict, and the last line sums them up. It prints
unit names, cgroup paths, exit statuses, tool names, event types, counts and seconds: never a
message's text, an agent's command line or an environment variable's value. Exit code: 0 when
every check passed, 1 when one failed.

1. **host:** systemd's version, lingering, the user manager, and the cgroups of `zab.service`
   and the Codex daemon.
2. **V2, delegation:** the controllers the user manager has; a slice written as the design's
   `zab-jobs.slice` would be (`zab-probe655.slice`: `MemoryMax=75%`, `CPUWeight=50`,
   `TasksMax=4096`), and what its cgroup holds.
3. **V1, direct:** `systemd-run --user` with the design's properties, from the user manager's
   environment, without `DBUS_SESSION_BUS_ADDRESS`, and without `XDG_RUNTIME_DIR` too: how long
   it takes, where the unit lands, its process group, `oom_score_adj`, nice value and
   `NoNewPrivs`.
4. **V3, endings:** stand-ins for `zab job _exec` (it runs the job as its child, writes the log,
   and ends as the child did) and `zab job _ended` (`ExecStopPost`, recording `SERVICE_RESULT`,
   `EXIT_CODE` and `EXIT_STATUS`), for each ending: exit 0, exit 1, SIGKILL, `systemctl --user
   stop`, a stop that SIGTERM doesn't end, `RuntimeMaxSec`, a `setsid` grandchild after the job
   exits or is stopped, and out of memory under the slice's `MemoryMax` (V2, lowered to 256 MB
   for the test; if swap takes the excess instead, again with `MemorySwapMax=0` on the slice).
5. **V1 and V4, Claude:** a turn run as the bridge runs one (`build_argv`, `turn_env` over
   `zab.service`'s environment, `run_parsed_turn` with steering), whose `Bash` runs `start-job`,
   a stand-in for `zab job start`. The turn is the probe's child, so it runs in the probe's
   cgroup rather than `zab.service`'s: the user manager forks the unit either way. Then two
   turns asked to use `Monitor` and `ScheduleWakeup` and to end at once, reported without a
   verdict: their tools, background tasks, results and timings.
6. **V5, Codex:** a thread of the daemon with the bridge's parameters, whose turn runs
   `start-job` from an exec session; then the probe's connection closes, as at a bridge
   restart, and the unit is checked again after the daemon's unload delay. Again with the
   `workspace-write` sandbox (informative).
7. **restarts:** `systemctl --user restart zab.service`, then the Codex daemon: the units started
   from the turns must still run.

The clean-up stops the probe's slice, which stops every unit in it, removes the slice and the
probe's directory, and ends any process left with the probe's marker. SIGINT, SIGTERM and SIGHUP
reach it too.

**B3, a VM stop** (#923's review), apart, since it needs one: what a job's `ExecStopPost` sees
when a clean shutdown stops the user manager. `--shutdown arm` starts a unit whose
`ExecStopPost` records the ending, the boot id and the manager's and the system's states in
`~/zab-probe-655-shutdown/` (written to disk before it returns), and exits; then stop or reboot
the VM (an EC2 stop, `sudo systemctl poweroff`, or `sudo reboot`), start it, and run the probe
again with `--shutdown read`, which reports the record, checks that it came from the earlier
boot with the manager `stopping`, and removes the directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PREFIX = "zab-probe655"
SLICE = f"{PREFIX}.slice"
SLICE_TEXT = (
    "[Unit]\nDescription=zab #655 probe: background jobs\n"
    "[Slice]\nMemoryMax=75%\nCPUWeight=50\nTasksMax=4096\n"
)
#: Each job's properties in the design (§2.2): the 12-hour cap of J2 among them
PROPERTIES = (
    "RuntimeMaxSec=12h",
    "OOMScoreAdjust=500",
    "Nice=10",
    "NoNewPrivileges=yes",
    "TimeoutStopSec=15",
    "MemoryAccounting=yes",
    "CPUAccounting=yes",
)
OOM_LIMIT = "256M"
SKIPPABLE = ("endings", "claude", "monitor", "wakeup", "codex", "workspace-write", "restarts")
USER_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
TURN_S, RETURN_S, SETTLE_S = 600.0, 5.0, 2.0

#: `zab job _ended`'s stand-in, `ExecStopPost`: what systemd tells it of the ending
ENDED_PY = """\
import json, os, sys, time
data = {k: os.environ.get(k) for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")}
data["at"] = time.time()
with open(sys.argv[1] + ".tmp", "w") as f:
    json.dump(data, f)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
"""

#: `zab job _exec`'s stand-in: the job runs as its child, with no shell, its output in the log.
#: A stop's SIGTERM reaches every process of the unit; the wrapper's handler (reset at the
#: child's exec) keeps it waiting for the child, then it ends as the child did: a signal again,
#: so that systemd sees it, or the exit status.
EXEC_PY = """\
import os, signal, subprocess, sys
SPAWN = (
    "import subprocess, sys, time\\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', sys.argv[1]],"
    " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
    " stderr=subprocess.DEVNULL)\\n"
)
CHILD = {
    "exit0": "print('the job ran')",
    "exit1": "import sys; print('the job failed'); sys.exit(1)",
    "sleep": "import time; time.sleep(600)",
    "stubborn": "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(600)",
    "oom": "import time\\nb = []\\nfor _ in range(16):\\n    b.append(b'x' * (64 << 20))\\n"
    "    time.sleep(0.05)\\ntime.sleep(600)",
    "setsid-exit": SPAWN,
    "setsid-stop": SPAWN + "time.sleep(600)\\n",
}
case, marker, log = sys.argv[1:4]
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, lambda *_: None)
with open(log, "ab") as out:
    child = subprocess.Popen([sys.executable, "-c", CHILD[case], marker], stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT)
    code = child.wait()
if code < 0:
    try:
        signal.signal(-code, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(os.getpid(), -code)
sys.exit(code)
"""

#: B3's `ExecStopPost`: the ending, the boot, the manager's and the system's states, on disk
SHUTDOWN_PY = """\
import json, os, subprocess, sys, time
def state(*argv):
    try:
        return subprocess.run(["systemctl", *argv, "is-system-running"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception as exc:
        return "error: " + type(exc).__name__
data = {k: os.environ.get(k) for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")}
data["at"] = time.time()
data["boot_id"] = open("/proc/sys/kernel/random/boot_id").read().strip()
data["manager_state"] = state("--user")
data["system_state"] = state()
with open(sys.argv[1] + ".tmp", "w") as f:
    json.dump(data, f)
    f.flush()
    os.fsync(f.fileno())
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
fd = os.open(os.path.dirname(sys.argv[1]), os.O_RDONLY)
os.fsync(fd)
os.close(fd)
"""
SHUTDOWN_DIR = "zab-probe-655-shutdown"

#: `zab job start`'s stand-in, which the agents run: `start-job claude|codex`. Its settings are
#: in `start-job.json` beside it, written by the probe; it records where it ran and how
#: `systemd-run` went, and tries again with `XDG_RUNTIME_DIR` set when that was missing.
START_PY = """\
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
conf = json.loads((here / "start-job.json").read_text())
who = sys.argv[1] if len(sys.argv) > 1 else ""
if who not in conf["callers"]:
    sys.exit("usage: start-job " + "|".join(conf["callers"]))
unit = conf["prefix"] + "-" + who
argv = [conf["systemd_run"], "--user", "--unit=" + unit, "--slice=" + conf["slice"], "--collect",
        "--quiet", *("--property=" + p for p in conf["properties"]), "--", *conf["command"]]
cgroup = [l[3:] for l in Path("/proc/self/cgroup").read_text().splitlines() if l[:3] == "0::"]
rec = {"who": who, "pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0),
       "cgroup": (cgroup or [""])[0],
       "env": {k: k in os.environ for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                                            "ZAB_BOT")}}
def attempt(env):
    began = time.monotonic()
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"rc": 124, "seconds": 60.0, "error": "timed out"}
    error = (p.stderr.strip().splitlines() or [""])[-1][:200]
    return {"rc": p.returncode, "seconds": round(time.monotonic() - began, 3), "error": error}
rec["first"] = attempt(dict(os.environ))
if rec["first"]["rc"] and "XDG_RUNTIME_DIR" not in os.environ:
    rec["with_xdg"] = attempt({**os.environ, "XDG_RUNTIME_DIR": "/run/user/%d" % os.getuid()})
ok = rec["first"]["rc"] == 0 or rec.get("with_xdg", {}).get("rc") == 0
(here / ("caller-" + who + ".json")).write_text(json.dumps(rec))
print(("started " if ok else "failed to start ") + unit)
sys.exit(0 if ok else 1)
"""

CLAUDE_START = (
    "This is a test of the bridge (`{marker}`). Run this command with your Bash tool, in the "
    "foreground (not with run_in_background), exactly as written: `{start} claude`. It starts a "
    "background service and returns at once. Don't check on it, don't stop it and don't run "
    "anything else. Then end your turn with exactly `STARTED-{marker}`."
)
MONITOR = (
    "This is a test of the bridge (`{marker}`). Use your Monitor tool to watch the command "
    "`sleep 20; echo TICK-{marker}`, which prints one line 20 seconds from now. Don't wait for "
    "that line: start the monitor, then end your turn at once with exactly "
    "`MONITORING-{marker}`. If the line reaches you later, reply with exactly `TICK-{marker}`."
)
WAKEUP = (
    "This is a test of the bridge (`{marker}`). Use your ScheduleWakeup tool to wake yourself "
    "up in 60 seconds. Then end your turn at once with exactly `SCHEDULED-{marker}`. If you are "
    "woken up, reply with exactly `WOKE-{marker}`."
)
CODEX_START = (
    "This is a test of the bridge (`{marker}`). Run this shell command exactly as written: "
    "`{start} {who}`. It starts a background service and returns at once. Don't check on it, "
    "don't stop it and don't run anything else. Then end your turn with exactly "
    "`STARTED-{marker}`."
)


def emit(**fields: Any) -> None:
    print(json.dumps(fields, sort_keys=True, default=str), flush=True)


class Report:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, name: str, ok: Any, **details: Any) -> bool:
        emit(check=name, ok=bool(ok), **details)
        (self.passed if ok else self.failed).append(name)
        return bool(ok)


def pairs(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines (`systemctl show`, `show-environment`); a value systemd quoted
    (`$'…'`) is left out."""
    out = {}
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq and not value.startswith("$'"):
            out[key] = value
    return out


def under(path: str, parent: str) -> bool:
    """Whether cgroup `path` is `parent` or inside it, by whole components."""
    if not path or not parent:
        return False
    p, q = path.rstrip("/").split("/"), parent.rstrip("/").split("/")
    return p[: len(q)] == q


def proc_cgroup(pid: int | str) -> str:
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return ""
    return next((line[3:] for line in lines if line.startswith("0::")), "")


def proc_facts(pid: int) -> dict[str, Any]:
    """A process's group, session, nice value, `oom_score_adj` and `NoNewPrivs`."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        status = Path(f"/proc/{pid}/status").read_text()
        adj = int(Path(f"/proc/{pid}/oom_score_adj").read_text())
    except (OSError, ValueError):
        return {}
    fields = stat[stat.rindex(")") + 2 :].split()  # from field 3, the state
    nnp = re.search(r"^NoNewPrivs:\s*(\d+)", status, re.M)
    return {
        "pgid": int(fields[2]),
        "sid": int(fields[3]),
        "nice": int(fields[16]),
        "oom_score_adj": adj,
        "no_new_privs": int(nnp.group(1)) if nnp else None,
        "cgroup": proc_cgroup(pid),
    }


def marked(marker: str, *, prefix: bool = False) -> list[int]:
    """This user's processes with `marker` among their arguments (or an argument starting with
    it), from /proc."""
    pids, word = [], marker.encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")[1:]
        except OSError:
            continue
        if any(a == word or (prefix and a.startswith(word)) for a in argv):
            pids.append(int(entry.name))
    return pids


def in_group(pgid: int) -> list[int]:
    """The processes of process group `pgid`."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            facts = proc_facts(int(entry.name))
            if facts.get("pgid") == pgid:
                found.append(int(entry.name))
    return found


def end(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def unit_argv(
    systemd_run: str, unit: str, command: list[str], properties: tuple[str, ...]
) -> list[str]:
    """The design's `systemd-run` (§2.2), an argument list."""
    return [
        systemd_run, "--user", f"--unit={unit}", f"--slice={SLICE}", "--collect", "--quiet",
        *(f"--property={p}" for p in properties), "--", *command,
    ]  # fmt: skip


def with_properties(*changes: str) -> tuple[str, ...]:
    """`PROPERTIES` with each of `changes` (`Key=value`) in place of its key's."""
    keys = {c.split("=", 1)[0] for c in changes}
    return (*(p for p in PROPERTIES if p.split("=", 1)[0] not in keys), *changes)


@dataclass
class Host:
    uid: int
    env: dict[str, str]  # the probe's own, with XDG_RUNTIME_DIR
    systemd_run: str = "systemd-run"

    def run(
        self, argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603 - argument lists of the probe's own values
                argv,
                env=self.env if env is None else env,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout:.0f} s")

    def systemctl(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return self.run(["systemctl", "--user", *args], timeout=timeout)

    def show(self, unit: str, *props: str) -> dict[str, str]:
        return pairs(self.systemctl("show", unit, *(f"--property={p}" for p in props)).stdout)

    def active(self, unit: str) -> bool:
        return self.show(unit, "ActiveState").get("ActiveState") == "active"

    def manager_base(self) -> Path:
        return Path(f"/sys/fs/cgroup/user.slice/user-{self.uid}.slice/user@{self.uid}.service")


def last_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[-1][:200]


def meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "SwapTotal"):
            out[key] = int(rest.split()[0]) * 1024
    return out


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


@dataclass
class Probe:
    host: Host
    report: Report
    nonce: str
    workdir: Path
    config_path: str = ""
    skip: frozenset[str] = frozenset()
    claude_model: str = ""
    unload_s: float = 75.0
    shutdown: str = ""  # B3: "arm" or "read"
    units: dict[str, dict[str, str]] = field(default_factory=dict)
    manager_env: dict[str, str] = field(default_factory=dict)
    turn_units: list[str] = field(default_factory=list)
    slice_written: bool = False

    @property
    def marker(self) -> str:
        return f"{PREFIX}-{self.nonce}"

    def unit(self, name: str) -> str:
        return f"{self.marker}-{name}"

    def setup(self) -> None:
        self.workdir.mkdir(mode=0o700)
        python = sys.executable
        (self.workdir / "ended.py").write_text(ENDED_PY)
        (self.workdir / "exec.py").write_text(EXEC_PY)
        start = self.workdir / "start-job"
        start.write_text(f"#!{python} -I\n" + START_PY)
        start.chmod(0o700)
        sleep = shutil.which("sleep") or "/usr/bin/sleep"
        conf = {
            "callers": ["claude", "codex", "codex-workspace-write"],
            "prefix": self.marker,
            "slice": SLICE,
            "properties": list(PROPERTIES),
            "systemd_run": self.host.systemd_run,
            "command": [sleep, "900"],
        }
        (self.workdir / "start-job.json").write_text(json.dumps(conf))

    # ------------------------------------------------------------------------------- host

    def host_phase(self) -> None:
        h = self.host
        state = h.systemctl("is-system-running").stdout.strip()
        self.manager_env = pairs(h.systemctl("show-environment").stdout)
        listed = h.systemctl("list-units", "--all", "--plain", "--no-legend", "codex-app-server*")
        codex = [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]
        for name in ["zab.service", *codex]:
            self.units[name] = h.show(name, "ControlGroup", "MainPID", "ActiveState")
        user = pwd.getpwuid(h.uid).pw_name
        emit(
            phase="host",
            systemd=h.run(["systemctl", "--version"]).stdout.splitlines()[:1],
            kernel=os.uname().release,
            cpus=os.cpu_count(),
            memory=meminfo(),
            linger=Path(f"/var/lib/systemd/linger/{user}").exists(),
            user_manager=state,
            manager_env={k: k in self.manager_env for k in USER_ENV},
            probe_env={k: k in os.environ for k in USER_ENV},
            probe_cgroup=proc_cgroup("self"),
            units=self.units,
        )
        self.report.check(
            "host: the user manager runs", state in ("running", "degraded"), state=state
        )

    def daemon_groups(self) -> list[str]:
        return [
            u.get("ControlGroup", "")
            for name, u in self.units.items()
            if name.startswith("codex-app-server") and u.get("ControlGroup")
        ]

    def bridge_group(self) -> str:
        return self.units.get("zab.service", {}).get("ControlGroup", "")

    # ------------------------------------------------------------------------------- V2

    def delegation_phase(self) -> None:
        base = self.host.manager_base()
        controllers = read(base / "cgroup.controllers").split()
        subtree = read(base / "cgroup.subtree_control").split()
        emit(phase="V2 delegation", controllers=controllers, subtree_control=subtree)
        self.report.check(
            "V2: the user manager has the memory, cpu and pids controllers",
            {"memory", "cpu", "pids"} <= set(controllers),
            controllers=controllers,
        )
        directory = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SLICE).write_text(SLICE_TEXT)
        self.slice_written = True
        self.host.systemctl("daemon-reload")

    def slice_files(self) -> dict[str, str]:
        group = self.host.show(SLICE, "ControlGroup").get("ControlGroup", "")
        path = Path("/sys/fs/cgroup") / group.lstrip("/")
        names = ("memory.max", "memory.swap.max", "cpu.weight", "pids.max")
        return {"ControlGroup": group, **{n: read(path / n) for n in names}}

    # ------------------------------------------------------------------------------- V1 direct

    def direct_phase(self) -> None:
        h, r = self.host, self.report
        sleep = shutil.which("sleep") or "/usr/bin/sleep"
        env = dict(self.manager_env)
        no_bus = {k: v for k, v in env.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
        neither = {k: v for k, v in env.items() if k not in USER_ENV}
        variants = [("the user manager's", env), ("no bus address", no_bus), ("neither", neither)]
        for index, (label, variant) in enumerate(variants):
            unit = self.unit(f"direct{index}")
            began = time.monotonic()
            done = h.run(unit_argv(h.systemd_run, unit, [sleep, "600"], PROPERTIES), env=variant)
            seconds = round(time.monotonic() - began, 3)
            facts, info = {}, {}
            if done.returncode == 0:
                info = h.show(unit, "MainPID", "ControlGroup", "Slice", "ActiveState")
                facts = proc_facts(int(info.get("MainPID") or 0))
            emit(
                phase="V1 direct",
                environment=label,
                rc=done.returncode,
                seconds=seconds,
                error=last_line(done.stderr),
                unit=info,
                process=facts,
            )
            if index == 0:
                self.direct_checks(done.returncode, seconds, info, facts)
            if done.returncode == 0:
                h.systemctl("stop", f"{unit}.service")
        files = self.slice_files()
        total = meminfo()["MemTotal"]
        emit(phase="V2 slice", files=files, mem_total=total)
        try:
            share = int(files["memory.max"]) / total
        except ValueError:
            share = 0.0
        r.check(
            "V2: the slice's memory.max is 75% of the memory",
            0.74 <= share <= 0.76,
            share=round(share, 4),
        )
        r.check(
            "V2: the slice's cpu.weight is 50 and pids.max 4096",
            files["cpu.weight"] == "50" and files["pids.max"] == "4096",
        )

    def direct_checks(self, rc: int, seconds: float, info: dict, facts: dict) -> None:
        r = self.report
        r.check("V1: systemd-run --user works from the manager's environment", rc == 0)
        r.check("V1: systemd-run returns at once", rc == 0 and seconds < RETURN_S, seconds=seconds)
        group = info.get("ControlGroup", "")
        r.check(
            "V1: the unit is in the slice, outside zab.service, the daemon and the caller",
            f"/{SLICE}/" in group
            and not under(group, self.bridge_group())
            and not any(under(group, d) for d in self.daemon_groups())
            and not under(group, proc_cgroup("self")),
            cgroup=group,
        )
        own = proc_facts(os.getpid())
        r.check(
            "V1: the unit's process has its own group and session",
            facts and facts["pgid"] != own.get("pgid") and facts["sid"] != own.get("sid"),
        )
        r.check(
            "V1: OOMScoreAdjust=500, Nice=10 and NoNewPrivileges apply",
            facts.get("oom_score_adj") == 500
            and facts.get("nice") == 10
            and facts.get("no_new_privs") == 1,
            process={k: facts.get(k) for k in ("oom_score_adj", "nice", "no_new_privs")},
        )

    # ------------------------------------------------------------------------------- V3

    def endings_phase(self) -> None:
        cases = [
            ("exit0", "exit0", (), ""),
            ("exit1", "exit1", (), ""),
            ("sigkill", "sleep", (), "kill"),
            ("stop", "sleep", (), "stop"),
            ("stop-stubborn", "stubborn", ("TimeoutStopSec=5",), "stop"),
            ("runtime-max", "sleep", ("RuntimeMaxSec=5",), ""),
            ("setsid-exit", "setsid-exit", (), ""),
            ("setsid-stop", "setsid-stop", (), "stop"),
        ]
        for name, case, changes, action in cases:
            self.ending(name, case, with_properties(*changes), action)
        # V2: past the slice's MemoryMax (lowered for the test); then, if swap took the excess
        # instead, again with the slice's swap off from the start
        limit = f"MemoryMax={OOM_LIMIT}"
        ended = self.ending("oom", "oom", PROPERTIES, "", (limit,)) or {}
        killed = self.report.check(
            "V2: a job past the slice's MemoryMax ends with oom-kill",
            ended.get("SERVICE_RESULT") == "oom-kill",
            ended=ended,
            swap=meminfo()["SwapTotal"],
        )
        if not killed:
            again = self.ending("oom-no-swap", "oom", PROPERTIES, "", (limit, "MemorySwapMax=0"))
            self.report.check(
                "V2: with MemorySwapMax=0 on the slice too, it ends with oom-kill",
                (again or {}).get("SERVICE_RESULT") == "oom-kill",
                ended=again,
            )

    def ending(
        self,
        name: str,
        case: str,
        properties: tuple[str, ...],
        action: str,
        slice_properties: tuple[str, ...] = (),
    ) -> dict[str, str] | None:
        """One job, ended as `action` says (or by itself), and what `ExecStopPost` saw."""
        h, r, py = self.host, self.report, sys.executable
        unit = self.unit(name)
        out, log = self.workdir / f"ended-{name}.json", self.workdir / f"log-{name}.txt"
        post = f"ExecStopPost={py} -I {self.workdir / 'ended.py'} {out}"
        command = [py, "-I", str(self.workdir / "exec.py"), case, unit, str(log)]
        if slice_properties:
            h.systemctl("set-property", "--runtime", SLICE, *slice_properties)
        began, wall = time.monotonic(), time.time()
        done = h.run(unit_argv(h.systemd_run, unit, command, (*properties, post)))
        details: dict[str, Any] = {"stopped_by_probe": False}
        if not r.check(f"V3 {name}: started", done.returncode == 0, error=last_line(done.stderr)):
            return None
        if action:
            # the wrapper and its child, and for `setsid-stop` the grandchild too
            wait_until(lambda: len(marked(unit)) >= (3 if case == "setsid-stop" else 2), 10)
            details["processes"] = len(marked(unit))
            time.sleep(1.0)  # the child's own start: its SIGTERM handler
            if action == "kill":
                pid = int(h.show(unit, "MainPID").get("MainPID") or 0)
                if pid > 1:
                    os.kill(pid, signal.SIGKILL)
            else:
                t = time.monotonic()
                stopped = h.systemctl("stop", f"{unit}.service", timeout=90)
                details["stop"] = {
                    "rc": stopped.returncode,
                    "seconds": round(time.monotonic() - t, 1),
                }
        wait_until(out.exists, 60.0 if case == "oom" else 30.0)
        if slice_properties:
            details["slice"] = self.slice_files()
            if not out.exists():  # not killed: where the memory went
                group = h.show(unit, "ControlGroup").get("ControlGroup", "")
                path = Path("/sys/fs/cgroup") / group.lstrip("/")
                details["memory"] = {
                    n: read(path / n) for n in ("memory.current", "memory.swap.current")
                }
                h.systemctl("stop", f"{unit}.service", timeout=90)
                details["stopped_by_probe"] = True  # its ending is the probe's stop
                wait_until(out.exists, 20)
            h.systemctl(
                "set-property", "--runtime", SLICE, "MemoryMax=75%", "MemorySwapMax=infinity"
            )
        ended = json.loads(out.read_text()) if out.exists() else None
        time.sleep(1.0)
        left = marked(unit)
        if ended:
            details["seconds"] = round(ended.pop("at") - wall, 1)
        emit(
            phase="V3 endings",
            case=name,
            ended=ended,
            left=len(left),
            unit_after=h.show(unit, "LoadState", "ActiveState"),
            log_bytes=log.stat().st_size if log.exists() else None,
            took=round(time.monotonic() - began, 1),
            **details,
        )
        r.check(f"V3 {name}: ExecStopPost recorded the ending", ended is not None, ended=ended)
        r.check(f"V3 {name}: nothing of the job is left", not left, left=len(left))
        end(left)
        return ended

    # ------------------------------------------------------------------------------- turns

    def turn_base_env(self) -> dict[str, str]:
        """What `zab run` has: the user manager's environment, the user's own variables
        systemd sets for a service, and `zab.service`'s `Environment=` lines, less its
        `UnsetEnvironment=` ones."""
        user = pwd.getpwuid(self.host.uid)
        env = {
            "HOME": user.pw_dir,
            "USER": user.pw_name,
            "LOGNAME": user.pw_name,
            "SHELL": user.pw_shell,
            **self.manager_env,
        }
        from zab.commands.service import unit_directory

        try:
            text = (unit_directory() / "zab.service").read_text()
        except OSError:
            emit(phase="turns", note="no zab.service: the manager's environment only")
            return env
        for line in text.splitlines():
            if line.startswith("Environment="):
                for part in shlex.split(line.removeprefix("Environment=")):
                    key, _, value = part.replace("%%", "%").partition("=")
                    env[key] = value
            elif line.startswith("UnsetEnvironment="):
                for key in shlex.split(line.removeprefix("UnsetEnvironment=")):
                    env.pop(key, None)
        return env

    def caller(self, who: str) -> dict[str, Any] | None:
        path = self.workdir / f"caller-{who}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def check_turn_unit(self, label: str, who: str, turn_group: str) -> None:
        """The checks of a unit a turn started: the caller's record, the unit's cgroup and
        process group, and whether it still runs."""
        r, h = self.report, self.host
        rec = self.caller(who)
        unit = self.unit(who)
        info = h.show(unit, "MainPID", "ControlGroup", "ActiveState")
        facts = proc_facts(int(info.get("MainPID") or 0))
        emit(phase=label, caller=rec, unit=info, process=facts)
        if not r.check(f"{label}: the agent ran start-job", rec is not None):
            return
        first = rec["first"]
        r.check(
            f"{label}: systemd-run --user worked from the turn",
            first["rc"] == 0,
            first=first,
            retried_with_xdg=rec.get("with_xdg"),
        )
        r.check(
            f"{label}: systemd-run returned at once",
            first["seconds"] < RETURN_S,
            seconds=first["seconds"],
        )
        # the way `ZAB_SESSION` will take (design §2.3): `turn_env`, `shell_environment_policy`
        r.check(f"{label}: ZAB_BOT reached start-job", rec["env"].get("ZAB_BOT"), env=rec["env"])
        group = info.get("ControlGroup", "")
        r.check(
            f"{label}: the unit is outside the turn's cgroup, zab.service's and the daemon's",
            group
            and not under(group, rec["cgroup"])
            and not under(group, turn_group)
            and not under(group, self.bridge_group())
            and not any(under(group, d) for d in self.daemon_groups()),
            unit=group,
            caller=rec["cgroup"],
        )
        r.check(
            f"{label}: the unit's process is outside the turn's process group and session",
            facts and facts["pgid"] != rec["pgid"] and facts["sid"] != rec["sid"],
        )
        if info.get("ActiveState") == "active":
            self.turn_units.append(unit)
        r.check(f"{label}: the unit runs after the turn ended", info.get("ActiveState") == "active")

    def claude_setup(self) -> tuple[Any, Any, str, str] | None:
        from zab.backends.claude import ClaudeConfig
        from zab.config import load_config

        config = load_config(self.config_path or None)
        found = [(n, b) for n, b in config.backends.items() if b.kind == "claude"]
        if not found:
            return None
        name, backend = found[0]
        claude = ClaudeConfig.from_mapping(asdict(backend))
        settings = claude.defaults
        if self.claude_model:
            settings = replace(settings, model=self.claude_model)
        bot = next((b.name for b in config.bots if b.backend == name), "")
        return claude, settings, claude.config_dir(None), bot

    def claude_phase(self) -> None:
        setup = self.claude_setup()
        if setup is None:
            emit(phase="V1 claude", skipped="no Claude backend in the config")
            return
        base = self.turn_base_env()
        start = self.workdir / "start-job"
        runs = [
            ("claude", "V1 claude", CLAUDE_START.format(marker=self.marker, start=start)),
            ("monitor", "V4 monitor", MONITOR.format(marker=self.marker)),
            ("wakeup", "V4 wakeup", WAKEUP.format(marker=self.marker)),
        ]
        for key, label, prompt in runs:
            if key in self.skip:
                continue
            result = asyncio.run(claude_turn(setup, base, prompt, str(self.workdir), self.marker))
            if key != "claude":  # V4's other questions: what happens, no verdict
                emit(phase=label, informative=True, **result)
                continue
            emit(phase=label, **result)
            r = self.report
            ok = result["code"] == 0 and not result["timed_out"]
            r.check(f"{label}: the turn ended by itself", ok, code=result["code"])
            r.check(
                f"{label}: no background note, and no group killed for its pipes",
                not result["note"] and not result["group_killed"],
            )
            r.check(
                f"{label}: nothing is left in the turn's process group", not result["left_in_group"]
            )
            self.check_turn_unit(label, "claude", proc_cgroup("self"))

    def codex_phase(self) -> None:
        from zab.config import load_config

        config = load_config(self.config_path or None)
        found = [(n, b) for n, b in config.backends.items() if b.kind == "codex"]
        if not found:
            emit(phase="V5 codex", skipped="no Codex backend in the config")
            return
        name, backend = found[0]
        base = self.turn_base_env()
        home = str(Path(backend.codex_home or base.get("CODEX_HOME") or "~/.codex").expanduser())
        bot = next((b.name for b in config.bots if b.backend == name), "")
        defaults = dict(backend.defaults)
        for who, sandbox in (("codex", ""), ("codex-workspace-write", "workspace-write")):
            if (who == "codex" and "codex" in self.skip) or (
                who != "codex" and "workspace-write" in self.skip
            ):
                continue
            label = "V5 codex" if who == "codex" else "V5 codex workspace-write"
            prompt = CODEX_START.format(
                marker=self.marker, start=self.workdir / "start-job", who=who
            )
            params = {
                "home": home,
                "socket": backend.socket,
                "bot": bot,
                "cwd": str(self.workdir),
                "approval": defaults.get("approval") or "never",
                "sandbox": sandbox or defaults.get("sandbox") or "danger-full-access",
                "model": defaults.get("model") or "",
                "effort": defaults.get("effort") or "",
            }
            result = asyncio.run(codex_turn(params, prompt))
            emit(phase=label, **result)
            if who != "codex":
                rec = self.caller(who)
                emit(
                    phase=label,
                    informative=True,
                    caller=rec,
                    unit=self.host.show(self.unit(who), "ActiveState", "ControlGroup"),
                )
                self.host.systemctl("stop", f"{self.unit(who)}.service")
                continue
            self.report.check(
                f"{label}: the turn completed",
                result["status"] == "completed",
                status=result["status"],
            )
            rec = self.caller(who)
            daemon = self.daemon_groups()
            self.report.check(
                f"{label}: start-job ran in the daemon's cgroup",
                rec and any(under(rec["cgroup"], d) for d in daemon),
                caller=rec and rec["cgroup"],
                daemon=daemon,
            )
            self.check_turn_unit(label, who, "")
            time.sleep(self.unload_s)  # the connection closed: the daemon unloads the thread
            state = asyncio.run(thread_status(params, result["thread"])) if result["thread"] else ""
            self.report.check(
                f"{label}: the thread was unloaded, the probe's connection gone",
                state == "notLoaded",
                status=state,
                seconds=self.unload_s,
            )
            self.report.check(
                f"{label}: the unit runs after the daemon's unload delay",
                self.host.active(self.unit(who)),
                seconds=self.unload_s,
            )

    # ------------------------------------------------------------------------------- restarts

    def restarts_phase(self) -> None:
        h, r = self.host, self.report
        if not self.turn_units:
            emit(phase="restarts", skipped="no unit from a turn runs")
            return
        for name in self.units:
            if self.units[name].get("ActiveState") != "active":
                continue
            began = time.monotonic()
            done = h.systemctl("restart", name, timeout=300)
            back = wait_until(lambda n=name: h.active(n), 300)
            emit(
                phase="restarts",
                unit=name,
                rc=done.returncode,
                back=back,
                seconds=round(time.monotonic() - began, 1),
            )
            # a refused restart leaves the units as they were: their survival says nothing then
            r.check(
                f"restarts: {name} restarted",
                done.returncode == 0 and back,
                rc=done.returncode,
                back=back,
                error=last_line(done.stderr),
            )
            for unit in self.turn_units:
                r.check(f"restarts: {unit} runs after restarting {name}", h.active(unit))

    # ------------------------------------------------------------------------------- B3

    def shutdown_dir(self) -> Path:
        return self.workdir.parent / SHUTDOWN_DIR

    def arm_shutdown(self) -> None:
        """B3, before the stop: a unit whose `ExecStopPost` will record what it sees."""
        h, where = self.host, self.shutdown_dir()
        where.mkdir(mode=0o700, exist_ok=True)
        (where / "ended.py").write_text(SHUTDOWN_PY)
        record = where / "ended.json"
        record.unlink(missing_ok=True)
        unit = f"{PREFIX}-shutdown"
        post = f"ExecStopPost={sys.executable} -I {where / 'ended.py'} {record}"
        argv = [
            h.systemd_run, "--user", f"--unit={unit}", "--collect", "--quiet",
            "--property=RuntimeMaxSec=12h", "--property=TimeoutStopSec=15",
            f"--property={post}", "--", shutil.which("sleep") or "/usr/bin/sleep", "43200",
        ]  # fmt: skip
        done = h.run(argv)
        armed = {"boot_id": boot_id(), "at": time.time(), "unit": unit}
        (where / "armed.json").write_text(json.dumps(armed))
        self.report.check("B3: armed", done.returncode == 0 and h.active(unit),
                          error=last_line(done.stderr), **armed)  # fmt: skip
        emit(
            phase="B3 shutdown",
            next="stop or reboot the VM (an EC2 stop, `sudo systemctl poweroff` or `sudo "
            "reboot`), start it, then run the probe again with `--shutdown read`",
        )

    def read_shutdown(self) -> None:
        """B3, after the stop: what the armed unit's `ExecStopPost` saw."""
        h, r, where = self.host, self.report, self.shutdown_dir()
        armed, ended = read_record(where / "armed.json"), read_record(where / "ended.json")
        unit = f"{PREFIX}-shutdown"
        if h.active(unit):
            emit(phase="B3 shutdown", note="the armed unit still runs: stop the VM first")
            r.check("B3: the VM was stopped since the probe was armed", False)
            return
        emit(phase="B3 shutdown", armed=armed, ended=ended, boot_id=boot_id())
        r.check("B3: the probe was armed", armed is not None)
        if not r.check("B3: ExecStopPost ran during the stop", ended is not None):
            return
        r.check(
            "B3: it ran in the boot that was stopped",
            armed is not None and ended.get("boot_id") == armed.get("boot_id") != boot_id(),
        )
        r.check(
            "B3: it saw the user manager stopping",
            ended.get("manager_state") == "stopping",
            manager_state=ended.get("manager_state"),
            system_state=ended.get("system_state"),
            ending=[ended.get(k) for k in ("EXIT_CODE", "EXIT_STATUS", "SERVICE_RESULT")],
        )
        shutil.rmtree(where, ignore_errors=True)

    # ------------------------------------------------------------------------------- clean-up

    def clean_up(self) -> None:
        h = self.host
        stopped = h.systemctl("stop", SLICE, timeout=120)
        left = marked(self.marker, prefix=True)
        end(left)
        if self.slice_written:
            h.systemctl("revert", SLICE)
            directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            (directory / "systemd/user" / SLICE).unlink(missing_ok=True)
            h.systemctl("daemon-reload")
        listed = h.systemctl("list-units", "--all", "--plain", "--no-legend", f"{self.marker}-*")
        shutil.rmtree(self.workdir, ignore_errors=True)
        emit(
            event="clean-up",
            slice_stopped=stopped.returncode == 0,
            ended=len(left),
            units_left=[line.split()[0] for line in listed.stdout.splitlines() if line.strip()],
        )

    def run(self) -> None:
        self.setup()
        self.host_phase()
        self.delegation_phase()
        self.direct_phase()
        if "endings" not in self.skip:
            self.endings_phase()
        if not {"claude", "monitor", "wakeup"} <= self.skip:
            self.claude_phase()
        if not {"codex", "workspace-write"} <= self.skip:
            self.codex_phase()
        if "restarts" not in self.skip:
            self.restarts_phase()


def boot_id() -> str:
    return read(Path("/proc/sys/kernel/random/boot_id"))


def read_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def wait_until(condition: Any, seconds: float, pause: float = 0.5) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(pause)
    return True


class Stream:
    """What a Claude turn's stdout shows, without its text: event types, tool names and how long
    each call took, the background tasks' types and states, and the results."""

    def __init__(self) -> None:
        self.events: Counter[str] = Counter()
        self.tools: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.pending: dict[str, tuple[str, float]] = {}
        self.stderr_lines = 0

    def feed(self, stream: str, line: str, t: float) -> None:
        if stream != "stdout":
            self.stderr_lines += stream == "stderr"
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        kind, sub = obj.get("type"), obj.get("subtype")
        self.events[f"{kind}/{sub}" if sub else str(kind)] += 1
        content = (
            (obj.get("message") or {}).get("content") if kind in ("assistant", "user") else None
        )
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name"))
                background = bool((block.get("input") or {}).get("run_in_background"))
                self.tools.append({"tool": name, "background": background, "t": round(t, 1)})
                self.pending[str(block.get("id"))] = (name, t)
            elif block.get("type") == "tool_result":
                name, began = self.pending.pop(str(block.get("tool_use_id")), ("?", t))
                self.tools.append(
                    {
                        "result": name,
                        "seconds": round(t - began, 1),
                        "error": bool(block.get("is_error")),
                        "t": round(t, 1),
                    }
                )
        if kind == "system" and sub == "background_tasks_changed":
            tasks = obj.get("tasks")
            self.tasks.append(
                {
                    "t": round(t, 1),
                    "tasks": [
                        {k: task.get(k) for k in ("task_type", "status", "ambient")}
                        for task in (tasks if isinstance(tasks, list) else [])
                        if isinstance(task, dict)
                    ],
                }
            )
        if kind == "result":
            self.results.append({"t": round(t, 1), "subtype": sub, "error": obj.get("is_error")})

    def summary(self) -> dict[str, Any]:
        return {
            "events": dict(self.events),
            "tools": self.tools,
            "tasks": self.tasks,
            "results": self.results,
            "stderr_lines": self.stderr_lines,
        }


class Warnings(logging.Handler):
    """The bridge's warning that a turn's pipes outlived its process (`ClaudeTurn`)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.killed = False

    def emit(self, record: logging.LogRecord) -> None:
        self.killed |= "killing" in record.getMessage()


async def claude_turn(
    setup: tuple[Any, Any, str, str], base: dict[str, str], prompt: str, cwd: str, marker: str
) -> dict[str, Any]:
    """One turn as `ClaudeBackend._run` runs it (§9.1), with the probe's prompt."""
    from zab.backends.claude import StreamInput, build_argv, new_session_id, new_turn_id, turn_env
    from zab.backends.claude_stream import BACKGROUND_NOTE, StreamParser, run_parsed_turn
    from zab.core.types import TurnCompleted

    claude, settings, account, bot = setup
    session = new_session_id()
    argv = build_argv(claude, settings, session_id=session, resume=False)
    env = turn_env(account, base, connectors=claude.claudeai_connectors, bot=bot)
    stream, final, holder = Stream(), {}, []
    warnings = Warnings()
    logging.getLogger("zab.backends.claude").addHandler(warnings)

    def on_event(ev: Any) -> None:
        if isinstance(ev, TurnCompleted):
            text = ev.final_text or ""
            final.update(
                status=ev.status.value,
                note=BACKGROUND_NOTE in text,
                tokens=sorted(set(re.findall(r"\b([A-Z]+)-" + re.escape(marker), text))),
            )

    began = time.monotonic()
    stream_input = StreamInput(prompt) if claude.steer else None
    parser = StreamParser(  # as `ClaudeBackend._run` builds it: several results joined
        session,
        new_turn_id(session),
        cwd=cwd,
        effort=settings.effort,
        multi_result=stream_input is not None,
        steers=stream_input.client_ids if stream_input is not None else None,
    )
    task = asyncio.ensure_future(
        run_parsed_turn(
            argv,
            env=env,
            cwd=cwd,
            prompt=prompt,
            parser=parser,
            on_event=on_event,
            on_raw=stream.feed,
            turn_holder=holder,
            stream_input=stream_input,
        )
    )
    done, _ = await asyncio.wait({task}, timeout=TURN_S)
    timed_out = not done
    if timed_out and holder:
        await holder[0].interrupt()
    code = -1
    try:
        _, code = await task
    except Exception as exc:  # a probe reports and goes on
        final["error"] = type(exc).__name__
    finally:
        logging.getLogger("zab.backends.claude").removeHandler(warnings)
    total = time.monotonic() - began
    pgid = holder[0].pgid if holder else None
    await asyncio.sleep(SETTLE_S)  # whatever the turn left, before it is counted
    summary = stream.summary()
    last = summary["results"][-1]["t"] if summary["results"] else None
    return {
        "code": code,
        "timed_out": timed_out,
        "seconds": round(total, 1),
        "after_last_result_s": round(total - last, 1) if last is not None else None,
        "model": settings.model,
        "approval": settings.approval,
        "allowed_tools": claude.allowed_tools,
        "disallowed_tools": claude.disallowed_tools,
        "note": final.get("note", False),
        "group_killed": warnings.killed,
        "left_in_group": len(in_group(pgid)) if pgid else 0,
        "final": final,
        "stream": summary,
    }


async def codex_turn(params: dict[str, Any], prompt: str) -> dict[str, Any]:
    """One turn of a new thread with the bridge's parameters (`CodexBackend.create_session`);
    the connection closes after it, as at a bridge restart."""
    from zab.backends.codex import identity_config
    from zab.backends.codex_rpc import CodexRpc

    rpc = CodexRpc()
    await rpc.connect(home=params["home"], socket=params["socket"])
    thread_id, completed = "", asyncio.Event()
    status, items = "", Counter()

    async def pump() -> None:
        nonlocal status
        while True:
            message = await rpc.incoming.get()
            method, p = message.get("method"), message.get("params") or {}
            if p.get("threadId") != thread_id:
                continue
            if method in ("item/started", "item/completed"):
                item = p.get("item") or {}
                items[f"{method} {item.get('type')} {item.get('status') or ''}".strip()] += 1
            elif method == "turn/completed":
                status = (p.get("turn") or {}).get("status") or ""
                completed.set()

    reader = asyncio.create_task(pump())
    began = time.monotonic()
    try:
        read = await rpc.call("config/read", {"cwd": params["cwd"], "includeLayers": False})
        policy = read["config"].get("shell_environment_policy") or {}
        # identity_config reads git_identity, bot and, since #927, session ("": none set, and
        # an inherited ZAB_SESSION removed, as for a spec without one)
        spec = SimpleNamespace(git_identity=None, bot=params["bot"], session="")
        start = {
            "config": identity_config(spec, policy),
            "cwd": params["cwd"],
            "serviceName": "zab",
            "approvalPolicy": params["approval"],
            "sandbox": params["sandbox"],
            **({"model": params["model"]} if params["model"] else {}),
        }
        thread_id = (await rpc.call("thread/start", start))["thread"]["id"]
        turn = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]}
        if params["effort"]:
            turn["effort"] = params["effort"]
        turn_id = ((await rpc.call("turn/start", turn)).get("turn") or {}).get("id")
        try:
            await asyncio.wait_for(completed.wait(), TURN_S)
        except TimeoutError:
            status = "timeout"
            if turn_id:  # as the Claude path interrupts its turn
                with contextlib.suppress(Exception):
                    await rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
    finally:
        reader.cancel()
        await rpc.close()
    return {
        "thread": thread_id,
        "status": status,
        "seconds": round(time.monotonic() - began, 1),
        "sandbox": params["sandbox"],
        "approval": params["approval"],
        "items": dict(items),
    }


async def thread_status(params: dict[str, Any], thread_id: str) -> str:
    """A thread's state as a new connection reads it (`thread/read` loads nothing)."""
    from zab.backends.codex_rpc import CodexRpc

    rpc = CodexRpc()
    await rpc.connect(home=params["home"], socket=params["socket"])
    try:
        read = await rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        return str(((read.get("thread") or {}).get("status") or {}).get("type") or "")
    finally:
        await rpc.close()


def probe(argv: list[str], *, workdir: Path | None = None) -> Probe:
    parser = argparse.ArgumentParser(description="#655's probe of background jobs (V1 to V5)")
    parser.add_argument("--config", default="", help="zab's config (default: zab's own)")
    parser.add_argument("--skip", default="", help="phases to leave out: " + ",".join(SKIPPABLE))
    parser.add_argument("--claude-model", default="", help="instead of the config's default")
    parser.add_argument("--unload", type=float, default=75.0, help="seconds after disconnect")
    parser.add_argument("--shutdown", choices=("arm", "read"), default="",
                        help="B3, apart: arm before a VM stop, read after it")  # fmt: skip
    args = parser.parse_args(argv)
    skip = frozenset(s for s in args.skip.split(",") if s)
    if unknown := skip - set(SKIPPABLE):
        parser.error(f"unknown phases: {', '.join(sorted(unknown))}")
    uid = os.getuid()
    env = {**os.environ}
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")  # as `zab job` will
    host = Host(uid, env, shutil.which("systemd-run") or "/usr/bin/systemd-run")
    nonce = secrets.token_hex(3)
    where = (workdir or Path.home()) / f"zab-probe-655-{nonce}"
    return Probe(
        host, Report(), nonce, where, args.config, skip, args.claude_model, args.unload,
        args.shutdown,
    )  # fmt: skip


class Interrupted(BaseException):
    """SIGTERM or SIGHUP (a `kill`, a `timeout` wrapper, a hung-up ssh): raised so that the
    clean-up runs, as it does for SIGINT."""


def _interrupt(signum: int, frame: Any) -> None:
    raise Interrupted(signal.Signals(signum).name)


def main(found: Probe) -> int:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _interrupt)
    emit(event="probe", marker=found.marker, skip=sorted(found.skip))
    code = 0
    try:
        if found.shutdown == "arm":
            found.arm_shutdown()
        elif found.shutdown == "read":
            found.read_shutdown()
        else:
            found.run()
    except Interrupted as exc:
        emit(event="aborted", signal=str(exc))
        found.report.failed.append(f"interrupted: {exc}")
        code = 128 + signal.Signals[str(exc)].value
    except Exception as exc:  # the probe's last line says why it stopped
        emit(event="aborted", error=type(exc).__name__, detail=str(exc)[:300])
        found.report.failed.append(f"aborted: {type(exc).__name__}")
    finally:
        if found.shutdown != "arm":  # the armed unit must outlive the probe, until the stop
            found.clean_up()
    r = found.report
    emit(event="summary", passed=len(r.passed), failed=r.failed)
    return code or (1 if r.failed else 0)


if __name__ == "__main__":
    sys.exit(main(probe(sys.argv[1:])))
