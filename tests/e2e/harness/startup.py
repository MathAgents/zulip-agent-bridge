"""A started machine's SSH, waited for until it answers steadily, and the timeline of its
start-up for the report (#690, #804). Shared by the participant runner (`run.py`), the admin
rig's setup (`admin/harness/setup.py`) and a scenario's clean-up (`cleanup.vm_up`).

#690: one answer was taken for SSH being up. Three times right after a start, the deploy's
first connection, made a few seconds after that answer, then timed out at the TCP connect (no
answer to any SYN for ssh's 15 s), and the run ended before any scenario; with the VM started
90 s before the run, it never did. One connection that succeeds just after a start doesn't
vouch for the next.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from e2e.harness.vm import say

#: SSH is up once it has answered this many connections in a row, STEADY_GAP_S apart (#690):
#: one answer just after a start doesn't vouch for the next connection.
STEADY_PROBES, STEADY_GAP_S = 3, 10.0
#: The pause after a probe that failed, and how long SSH is waited for in all.
PROBE_PAUSE_S, SSH_WAIT_S = 5.0, 600.0
#: The bound of `cloud-init status --wait`, run once SSH first answers: the boot's last stages.
BOOT_WAIT_S = 300.0
#: The pauses before a deploy that could not connect runs again (#690, #804): 3 tries in all.
DEPLOY_PAUSES_S = (15.0, 30.0)


@dataclass
class Timeline:
    """The start-up, one line per event: EC2's states, each SSH probe, cloud-init, a deploy run
    again. Each line starts with the UTC time its step began, to set beside the machine's
    journal, then the seconds since the first line. Printed (`log`) and kept (`lines`) for the
    report's *Start-up*."""

    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time  # the lines' UTC times
    log: Callable[[str], None] = say
    lines: list[str] = field(default_factory=list)
    _t0: float | None = field(default=None, repr=False)

    def mark(self) -> tuple[float, float]:
        """Now, on the clock and in UTC: when a step of the timeline began."""
        return self.clock(), self.wall()

    def note(self, event: str, began: tuple[float, float] | None = None) -> None:
        """One line: the UTC time the step began (`mark`, else now), the seconds since the
        first line, the event."""
        mono, wall = began or self.mark()
        self._t0 = mono if self._t0 is None else self._t0
        at = dt.datetime.fromtimestamp(wall, dt.UTC)
        line = f"{at:%H:%M:%S}.{at.microsecond // 100_000}Z +{mono - self._t0:.1f} s: {event}"
        self.lines.append(line)
        self.log(line)


def steady_ssh(
    vm: Any, ip: str, timeline: Timeline, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Return once SSH has answered STEADY_PROBES connections in a row (`vm.probe()`: one
    connection, never retried), STEADY_GAP_S apart, with the boot's cloud-init waited for after
    the first answer, for what is left of SSH_WAIT_S at most; TimeoutError after SSH_WAIT_S.
    Each probe is a line of the timeline, which also gives the clock."""
    clock = timeline.clock
    deadline, streak, booted = clock() + SSH_WAIT_S, 0, False
    while True:
        began = timeline.mark()
        probe = vm.probe()
        took = clock() - began[0]
        streak = streak + 1 if probe.ok else 0
        if probe.ok:
            timeline.note(f"SSH answered in {took:.1f} s ({streak} of {STEADY_PROBES})", began)
        else:
            said = (probe.stderr or probe.stdout).strip().splitlines()[:1]
            why = (said or [f"exit {probe.returncode}"])[0][:200]
            timeline.note(f"SSH failed after {took:.1f} s: {why}", began)
        if streak and not booted:
            booted, began = True, timeline.mark()
            status = boot_status(vm, max(deadline - clock(), 1.0))
            timeline.note(f"cloud-init {status}, after {clock() - began[0]:.1f} s", began)
        if streak >= STEADY_PROBES:
            return
        if clock() >= deadline:
            raise TimeoutError(f"{ip}: SSH not steady {SSH_WAIT_S:.0f} s after the start")
        sleep(STEADY_GAP_S if streak else PROBE_PAUSE_S)


def boot_status(vm: Any, left: float) -> str:
    """`cloud-init status --wait` on the machine, at most BOOT_WAIT_S and `left` seconds: its
    `status:` line and exit status (0 done, 1 error, 2 done with recoverable errors). Never
    fatal: the probes decide."""
    wait = min(BOOT_WAIT_S, left)
    done = vm.run(["cloud-init", "status", "--wait"], timeout=wait, check=False)
    lines = [x.strip() for x in (done.stdout + "\n" + done.stderr).splitlines() if x.strip()]
    said = next((x for x in lines if x.startswith("status:")), "")
    return f"{said or (lines[-1:] or ['no output'])[0][:200]} (exit {done.returncode})"
