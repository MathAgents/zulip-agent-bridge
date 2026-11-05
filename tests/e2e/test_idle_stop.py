"""Scenario 11 of issue #263: idle stop (#207), then a DM wakes the VM.

This module's name sorts before the other scenario modules, so in a full run idle stop starts
right after the deploy, before any scenario's turn: the VM stays up for N minutes after zab's
last activity, so a turn just before would shift the window being measured.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

from e2e.harness.admin import push_config
from e2e.harness.cleanup import IDLE_STOP_LEFT, undo, vm_up
from e2e.harness.flows import DM_STATUS_RE, Chat

pytestmark = pytest.mark.e2e
MINUTES = 10
#: The idle clock starts at the last login the once-a-minute timer saw. Installing the timer
#: doesn't reset it, so the scenario keeps an SSH session open until a tick has recorded it
#: (`WAIT_FOR_LOGIN`, codex on #285), and the window starts when that session closes, a few
#: seconds after the recorded login.
LOW_S, HIGH_S = MINUTES * 60 - 30, 12 * 60
#: Run over SSH: this session is a login (`idle_stop.py`); wait, at most 3 minutes, until a tick
#: of the timer has seen it and touched /run/zab-idle-stop.login. Fixed text, no input.
WAIT_FOR_LOGIN = r"""start=$(date +%s)
for _ in $(seq 1 36); do
  seen=$(stat -c %Y /run/zab-idle-stop.login 2>/dev/null || echo 0)
  if [ "$seen" -ge "$start" ]; then exit 0; fi
  sleep 5
done
echo "idle stop recorded no login in 3 minutes" >&2
exit 1
"""


def this_runs_power_offs(log: str, since: float, until: float) -> list[str]:
    """The log's `power off: idle for` lines stamped (`2026-10-13T18:20:00Z …`, idle_stop's
    `_stamp`, UTC) between `since` and `until` (epoch seconds)."""
    found = []
    for line in log.splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            at = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if rest.startswith("power off: idle for") and since <= at.timestamp() <= until:
            found.append(line)
    return found


@pytest.mark.scenario(11)
async def test_idle_stop(chat: Chat, rig, ec2, instance_id, vm, scenario):
    """`push_config --idle-stop 10`, then nobody touches the VM: it stops within 10 to 12
    minutes; a DM wakes it; `/var/log/zab-idle-stop.log` shows the verdict."""
    try:
        push_config(rig, "--idle-stop", str(MINUTES))
        vm.script(WAIT_FOR_LOGIN, timeout=240)  # the clock starts at a login it recorded
        installed, installed_at = time.monotonic(), time.time()
        state = ec2.state(instance_id)
        while state not in ("stopping", "stopped") and time.monotonic() - installed < 20 * 60:
            await asyncio.sleep(20)  # AWS only: no SSH, no Zulip, nothing that counts as use
            state = ec2.state(instance_id)
        took, stopped_at = time.monotonic() - installed, time.time()
        assert state in ("stopping", "stopped"), f"still {state} after {took:.0f} s"
        scenario.timing("stop after the install", took, low=LOW_S, high=HIGH_S)
        ec2.wait_state(instance_id, "stopped", timeout=300)
        await asyncio.sleep(35)  # the waker's state refresh
        turn = await chat.dm("claude", "!status")
        _, took = await chat.reply(turn, DM_STATUS_RE, within=600)
        scenario.timing("woken by a DM: its answer", took)
        log = vm.run(["sudo", "tail", "-n", "20", "/var/log/zab-idle-stop.log"]).stdout
        # this run's power-off only: stamped between the install and the stop (reviewer on #285)
        verdicts = this_runs_power_offs(log, installed_at - 60, stopped_at + 60)
        assert verdicts, f"no power-off verdict from this run in the log: {log[-300:]!r}"
        scenario.notes.append("verdict: " + verdicts[-1][:200])
    finally:
        # off again: the other scenarios need the VM up. The timer must go even if the wake
        # failed: the VM started if need be, and SSH answering (a precaution: in #517's run SSH
        # was up; push_config's owner-check session reached the VM, then either push_config
        # refused the owner check or its next connection never reached sshd: a later run saw
        # the second kind, most likely packet loss on this machine's path, which `vm.run` retries,
        # #517 item 4); then push_config, retried: the mitigation for its own SSH. Else the run
        # stops here: a 10-minute idle stop would break the later scenarios (4's block, 8's).
        await undo(IDLE_STOP_LEFT, lambda: vm_up(ec2, instance_id, vm), attempts=1)
        await undo(IDLE_STOP_LEFT, lambda: push_config(rig, "--idle-stop", "0"))
