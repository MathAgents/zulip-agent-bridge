"""SSH to the test VM, with argument lists only (CONTRIBUTING.md, rule 3; issue #263).

Every remote command is an argument vector, joined with `shlex.join` for the VM's shell, so no
text reaches that shell unquoted. A few steps need a pipeline or a loop: those are fixed
literals in this repository (`Vm.script`), and their inputs travel as positional arguments
(`bash -c LITERAL bash ARG…`), never spliced into the script. The connection is pinned to the
host key in the rig's `known_hosts`, with the rig's own key, and forwards nothing.

A command whose connection failed before it ran is tried again (#517 item 4: connections from
the development machine that never reached the VM, most likely packet loss on its path, behind a
home NAT; not shown to be the VM): `Vm.call`, `connect_failure`.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RemoteError(RuntimeError):
    def __init__(self, argv: Sequence[str], result: Result) -> None:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        super().__init__(f"{shlex.join(argv)[:200]}: exit {result.returncode}: {tail[0][:300]}")
        self.result = result


Runner = Callable[[list[str], float], Result]


def subprocess_runner(argv: list[str], timeout: float) -> Result:
    try:
        done = subprocess.run(  # noqa: S603 - an argument list, never a shell string
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout:.0f} s")
    return Result(done.returncode, done.stdout, done.stderr)


#: ssh's `ConnectTimeout`: in OpenSSH 9.6 it bounds the TCP connect and the banner exchange.
CONNECT_TIMEOUT_S = 15
#: A command whose connection failed before it ran: its tries in all, and the pause between.
CONNECT_TRIES, CONNECT_PAUSE_S = 3, 5.0
#: A retry is made only when this much of the caller's budget is left after the pause: more
#: than ssh's own ConnectTimeout, so that a failed connect on the retry is ssh's to report,
#: never a race with the local timeout (reviewer on #535: a pause may overshoot).
MIN_TRY_S = CONNECT_TIMEOUT_S + 1.0


def _failure_forms(host: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """What OpenSSH 9.6 prints on stderr when a connection fails before the key exchange (its
    `sshconnect.c` and `kex.c`; reproduced locally, `test_harness_vm.RETRIED`, and seen in
    #517's runs): the cause, then perhaps `sshpkt_fatal`'s summary of it, naming the address.
    The TCP connect's line names the host it was given (`named`); the banner exchange's don't
    (`hostless`), so they count only beside the summary (`connect_failure`)."""
    h = re.escape(host)
    named = re.compile(
        rf"ssh: connect to host {h} port \d+: (?:Connection timed out|Connection refused"
        r"|No route to host|Network is unreachable)"
    )
    hostless = re.compile(
        r"kex_exchange_identification: (?:read: Connection reset by peer"
        r"|Connection closed by remote host)"  # an older client's form of the closed banner
        r"|Connection timed out during banner exchange"
    )
    summary = re.compile(
        rf"Connection (?:closed by|reset by) {h} port \d+|Connection to {h} port \d+ timed out"
    )
    return named, hostless, summary


def connect_failure(result: Result, host: str) -> str:
    """The line of ssh's stderr that shows `result` failed before the remote command ran (#517),
    so that trying it again is safe; else "". Strict: exit 255 (ssh's own), nothing on stdout,
    a cause above (the TCP connect to `host`, or the banner exchange: both before the key
    exchange, let alone the command), and every line one of the forms above. Never retried,
    then: a session cut short (`Connection to … closed by remote host.`, `Read from remote
    host …`, `Broken pipe`), a failed authentication, the remote command's own exit 255, or a
    bare `Connection closed by|reset by <host> port 22`: sshpkt_fatal's line for a failure at
    any stage of the handshake (9.6 may print only that for a banner closed at once), not proof
    alone that the command never started. Nor is a banner line without the summary naming
    `host` beside it: the local client prints both, so a lone one could only be forwarded
    from a remote command's own stderr (reviewer on #535)."""
    if result.returncode != 255 or result.stdout:
        return ""
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    named, hostless, summary = _failure_forms(host)
    causes = [x for x in lines if named.fullmatch(x) or hostless.fullmatch(x)]
    summaries = [x for x in lines if summary.fullmatch(x)]
    if not causes or len(causes) + len(summaries) != len(lines):  # a line of another form
        return ""
    if not summaries and not any(named.fullmatch(x) for x in causes):  # nothing names `host`
        return ""
    return causes[0]


def connect_line(line: str, host: str) -> bool:
    """Whether `line` is ssh's own line for a TCP connect to `host` that failed (`ssh: connect to
    host <host> port 22: Connection timed out`, …): the command never ran. For a line another
    program kept of ssh's stderr, such as a failure in push_config's table (#690)."""
    return bool(_failure_forms(host)[0].fullmatch(line.strip()))


def say(line: str) -> None:
    """A connect failure's line (`Vm.call`) on stderr: the runner's console, or the test's
    captured output."""
    print(f"e2e: {line}", file=sys.stderr, flush=True)


#: The agent's systemd user manager, for `systemctl --user` from `ubuntu` (as push_config does).
_USER_ENV = (
    "XDG_RUNTIME_DIR=/run/user/{uid}",
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
)
AGENT = "agent"
#: Where a command run as `agent` starts: SSH lands in ubuntu's home, which `agent` can't
#: enter, and `find -exec` then fails before it runs anything ("Failed to restore initial
#: working directory", scenario 17's first live run).
AGENT_HOME = "/home/agent"
UNIT = "zab.service"
_SIGNALS = frozenset({"STOP", "CONT", "TERM", "KILL"})
#: The units of a time span as `systemctl show` prints one (`5min`, `1min 30s`, `100ms`).
_SPAN_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400, "w": 604800,
               "month": 2629800, "y": 31557600}  # fmt: skip
_SPAN_PART = re.compile(r"(\d+(?:\.\d+)?)(us|ms|s|min|h|d|w|month|y)")


def span_seconds(text: str) -> float:
    """A time span as `systemctl show` prints it (`5min`, `1min 30s`, `0`, `infinity`), in
    seconds; ValueError for anything else."""
    if text.strip() == "infinity":
        return float("inf")
    if text.strip() == "0":
        return 0.0
    parts = [_SPAN_PART.fullmatch(p) for p in text.split()]
    if not parts or not all(parts):
        raise ValueError(f"not a systemd time span: {text!r}")
    return sum(float(m.group(1)) * _SPAN_UNITS[m.group(2)] for m in parts if m)


class Vm:
    def __init__(
        self,
        host: str,
        *,
        key: Path,
        known_hosts: Path,
        user: str = "ubuntu",
        runner: Runner = subprocess_runner,
        log: Callable[[str], None] = say,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = str(ipaddress.ip_address(host))  # an address, never a name to resolve
        if any(c in str(known_hosts) for c in '"\r\n'):
            raise ValueError("known_hosts path must not contain quotes or newlines")
        self.key, self.known_hosts, self.user, self.runner = key, known_hosts, user, runner
        self.log, self.sleep, self.clock = log, sleep, clock
        self._uid: int | None = None

    def ssh_argv(self, remote: Sequence[str]) -> list[str]:
        """`ssh … user@host -- <remote joined>`; `remote` must be a list or a tuple."""
        if isinstance(remote, str) or not remote:
            raise TypeError("remote commands are argument lists, never shell strings")
        return [
            "ssh",
            *("-i", str(self.key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"),
            *("-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "-o", "StrictHostKeyChecking=yes"),
            *("-o", f'UserKnownHostsFile="{self.known_hosts}"'),
            *("-o", "GlobalKnownHostsFile=/dev/null"),
            *("-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes"),
            f"{self.user}@{self.host}",
            "--",
            shlex.join(remote),
        ]

    def call(
        self, remote: Sequence[str], timeout: float, send: Runner, *, tries: int = CONNECT_TRIES
    ) -> Result:
        """`send(<ssh argv for remote>, seconds)`, tried again, at most `tries` times in all and
        CONNECT_PAUSE_S apart, while it fails before the remote command ran (`connect_failure`):
        safe, since the command never started (#517 item 4).

        `timeout` bounds the whole call, retries included, never each try: a retry gets what
        is left of it, and is made only when MIN_TRY_S are left after the pause. A failed
        connect ends within ConnectTimeout (15 s for the TCP connect and the banner together),
        so two retries cost about 40 s of the budget at most, and a long command (1800 s) is
        never given three times its time. A timing taken around a retried call includes the
        failed tries and the pause: the scenario's note says so.

        Each connect failure of a call that may retry (`tries` > 1) is logged, one line, with
        what came of it: the scenario's report notes (`conftest`), else stderr (`say`)."""
        argv = self.ssh_argv(remote)
        deadline = self.clock() + timeout
        attempt, budget = 1, float(timeout)
        while True:
            result = send(argv, budget)
            cause = connect_failure(result, self.host) if tries > 1 else ""
            if not cause:
                return result
            left = deadline - self.clock()
            again = attempt < tries and left - CONNECT_PAUSE_S >= MIN_TRY_S
            if again:
                then = f"again in {CONNECT_PAUSE_S:g} s"
            elif attempt == tries:
                then = "no tries left"
            else:  # whole seconds, rounded down: never more than its timeout
                then = f"not tried again: {int(max(left, 0))} s of its {timeout:g} s left"
            brief = " ".join(shlex.join(remote).split())
            brief = brief if len(brief) <= 60 else brief[:59] + "…"
            self.log(f"SSH connect failure ({brief}), try {attempt} of {tries}: {cause}; {then}")
            if not again:
                return result
            self.sleep(CONNECT_PAUSE_S)
            attempt, budget = attempt + 1, deadline - self.clock()

    def run(
        self,
        remote: Sequence[str],
        *,
        timeout: float = 120,
        check: bool = True,
        tries: int = CONNECT_TRIES,
    ) -> Result:
        result = self.call(remote, timeout, self.runner, tries=tries)
        if check and not result.ok:
            raise RemoteError(remote, result)
        return result

    def script(self, literal: str, *args: str, sudo: bool = False, timeout: float = 120) -> Result:
        """Run a fixed script of this repository with `args` as `$1…`: never spliced into it."""
        argv = [*(["sudo"] if sudo else []), "bash", "-c", literal, "bash", *args]
        return self.run(argv, timeout=timeout)

    def probe(self) -> Result:
        """One connection that runs `true`, never retried: its callers poll it, with
        their own pauses, and its stderr says why it failed."""
        return self.run(["true"], timeout=30, check=False, tries=1)

    def reachable(self) -> bool:
        return self.probe().ok

    # ------------------------------------------------------------------ the bridge's unit

    def agent_uid(self) -> int:
        if self._uid is None:
            self._uid = int(self.run(["id", "-u", AGENT]).stdout.strip())
        return self._uid

    def as_agent(self, *argv: str) -> list[str]:
        """`argv` run as `agent` with its user manager reachable (`systemctl --user`), from
        `AGENT_HOME`."""
        uid = self.agent_uid()
        env = [e.format(uid=uid) for e in _USER_ENV]
        return ["sudo", "-u", AGENT, "-H", "env", "-C", AGENT_HOME, *env, *argv]

    def unit(self, name: str = UNIT) -> dict[str, str]:
        """`MainPID`, `NRestarts`, `ActiveState` and `SubState` of the agent's user unit."""
        argv = self.as_agent("systemctl", "--user", "show", name, "--property=MainPID")
        argv += ["--property=NRestarts", "--property=ActiveState", "--property=SubState"]
        lines = self.run(argv).stdout.splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def unit_spans(self, *names: str, name: str = UNIT) -> dict[str, float]:
        """Time spans of the agent's user unit (`WatchdogUSec`, …), in seconds, as systemd
        has them: the installed unit's, defaults included."""
        argv = self.as_agent("systemctl", "--user", "show", name)
        lines = self.run([*argv, *(f"--property={n}" for n in names)]).stdout.splitlines()
        got = dict(line.split("=", 1) for line in lines if "=" in line)
        if missing := [n for n in names if n not in got]:
            raise RuntimeError(f"systemctl show {name}: no {', '.join(missing)}")
        return {n: span_seconds(got[n]) for n in names}

    def unit_action(self, action: str, name: str = UNIT) -> None:
        if action not in ("start", "stop", "restart"):
            raise ValueError(f"unit action {action!r}")
        self.run(self.as_agent("systemctl", "--user", action, name), timeout=180)

    def signal(self, pid: int, name: str) -> None:
        """`kill -<name> <pid>` as root (the watchdog scenario's SIGSTOP)."""
        if name not in _SIGNALS or type(pid) is not int or pid <= 1:
            raise ValueError(f"refusing kill -{name} {pid}")
        self.run(["sudo", "kill", f"-{name}", str(pid)])
