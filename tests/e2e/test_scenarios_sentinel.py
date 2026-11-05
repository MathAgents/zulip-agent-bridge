"""Scenario 15 of the end-to-end suite (issue #263): `zab sentinel` (#204), the owner's DM `!vm`
and `!restart`, while the bridge runs and while it is stopped.

The rollout's `zab service install` installs `zab-sentinel.service`; from then on the bridge
leaves those two commands, in a one-to-one DM, to the sentinel (SPEC §4.6, §13.3):

0. no organizers' hold is left on the VM (a killed `push_config.py` leaves one), under which the
   sentinel would refuse `!restart` (review on #573, #588);
1. after the sentinel ran `QUIET_UP_S` with no DM, so that only Zulip's heartbeat events kept
   its mark fresh (`LISTENING_FRESH_S`, 3 minutes), DM `!vm` gets exactly one reply, the
   sentinel's, which says the bridge runs (a stale mark would have the bridge answer too); then
   `zab doctor`'s `<bot>:sentinel` checks read OK for every bot, with no warning (#588);
2. DM `!restart` says so, restarts `zab.service` (a new main process) and confirms within the
   sentinel's two minutes, with no other reply; a second one, to the other bot, within the
   minute is refused;
3. with `zab.service` stopped, DM `!vm` still answers, saying the bridge isn't running, and
   `!restart` brings it back;
4. the bridge's catch-up of the two DMs it missed while stopped adds no reply;
5. `!restart codex` restarts the Codex daemon (a new main process), where there is one.

It stops and restarts the bridge, so it runs in the `slow` group (about 10 minutes).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import pytest

from e2e.harness import installed
from e2e.harness.cleanup import BRIDGE_STOPPED, undo
from e2e.harness.flows import Chat, Turn
from e2e.test_scenarios_features import SENTINEL_VM, check_vm

pytestmark = pytest.mark.e2e
SENTINEL_UNIT = "zab-sentinel.service"
CODEX_UNIT = "codex-app-server.service"
#: The sentinel's replies (`zab.tools.sentinel`).
RUNNING = r"(?m)^bridge running, last Zulip event .+ ago · "
STOPPED = r"(?m)^bridge not running \(inactive\): send !restart · "
RESTARTING = r"^🔄 Restarting your bridge: running turns will be interrupted\.$"
BACK = r"^✅ Your bridge is back: it is processing Zulip events again \(.+ after the restart\)\.$"
REFUSED = r"^⏳ A restart ran less than a minute ago; try again in \d+ s\.$"
CODEX_RESTARTING = r"^🔄 Restarting Codex: its running turns will be interrupted\.$"
CODEX_BACK = r"^✅ Codex is running again\.$"
#: How long a second reply has to come before the scenario takes it that none will: the bridge
#: answers a DM command within a few seconds.
QUIET_S = 20
#: The sentinel waits two minutes for the restarted bridge (`RESTART_WAIT_S`); then it posts.
CONFIRM_S = 150
#: The sentinel's one restart a minute (`RESTART_EVERY_S`), with a margin.
RESTART_EVERY_S = 65
#: Step 1 waits until the sentinel has run this long, and this scenario this long without a DM
#: (the slow scenarios run one after the other), so that only Zulip's heartbeat events can have
#: kept the mark fresh: `LISTENING_FRESH_S` is 180 s (review on #573).
QUIET_UP_S = 200
#: The organizers' hold (`zab.tools.sentinel.HOLD`), under which the sentinel refuses `!restart`.
HOLD = "/run/zab-idle-stop.hold"
#: The VM's clock, then, when the hold `$1` exists, its mtime and first line: a fixed literal.
HOLD_READ = 'date +%s; [ -e "$1" ] || exit 0; stat -c %Y -- "$1"; head -n 1 -- "$1" || true'
#: The doctor as `push_config.py` and `status.py` run it, as `agent` in a login shell.
DOCTOR = "cd / && zab doctor --service-env --json"


def hold_left(stdout: str) -> float:
    """Seconds the organizers' hold has left, from `HOLD_READ`'s lines: 0 without one. A hold
    that can't be read counts for an hour from its mtime, as for the sentinel (`held`)."""
    now, *hold = stdout.splitlines()
    if not hold:
        return 0.0
    try:
        until = float(hold[1].split()[0])
    except (IndexError, ValueError):
        until = float(hold[0]) + 3600
    return max(0.0, until - float(now))


def sentinel_checks(stdout: str) -> list[dict[str, Any]]:
    """The `<bot>:sentinel` checks of `zab doctor --json`'s output; [] for anything else."""
    try:
        checks = json.loads(stdout)
    except ValueError:
        return []
    if not isinstance(checks, list):
        return []
    return [c for c in checks if isinstance(c, dict) and str(c.get("name")).endswith(":sentinel")]


def sentinel_up_for(vm) -> float:
    """Seconds since `zab-sentinel.service` became active: the VM's uptime less systemd's
    monotonic activation time (the same clock on a VM, which never suspends)."""
    entered = "--property=ActiveEnterTimestampMonotonic"
    argv = vm.as_agent("systemctl", "--user", "show", SENTINEL_UNIT, entered, "--value")
    since = int(vm.run(argv).stdout.strip() or 0) / 1e6
    return float(vm.run(["cat", "/proc/uptime"]).stdout.split()[0]) - since


async def replies(chat: Chat, turn: Turn, seconds: float = QUIET_S) -> list[str]:
    """Every message of the bot after `turn`'s, once `seconds` passed (its reactions apart)."""
    found = await chat.user.silent(
        turn.narrow, after_id=turn.message_id, senders=[turn.bot], seconds=seconds
    )
    return [m["content"] for m in found if "reaction" not in m]


def matching(texts: list[str], *patterns: str) -> bool:
    """`texts` are exactly one message per pattern, in order."""
    return len(texts) == len(patterns) and all(
        re.search(p, t) for p, t in zip(patterns, texts, strict=True)
    )


async def after_the_minute(since: float) -> None:
    await asyncio.sleep(max(0.0, RESTART_EVERY_S - (time.monotonic() - since)))


@pytest.mark.scenario(15)
async def test_sentinel(chat: Chat, vm, scenario):
    if not installed.has(vm, installed.HAS_SENTINEL_UNIT):
        pytest.skip("the installed zab has no zab-sentinel.service (#204)")
    unit = vm.unit(SENTINEL_UNIT)
    assert unit.get("ActiveState") == "active", f"{SENTINEL_UNIT} isn't running: {unit}"

    # 0. no hold left over: the sentinel would refuse `!restart` (HELD), not restart
    left = hold_left(vm.script(HOLD_READ, HOLD).stdout)
    if left:
        pytest.fail(
            f"the organizers' hold {HOLD} is on for {left:.0f} s more, and the sentinel refuses "
            "!restart under it: a push_config.py or final_sync.py killed mid-run leaves one. "
            f"Remove it (sudo rm {HOLD}) and run scenario 15 again."
        )

    # 1. one reply, the sentinel's, on a mark kept fresh by Zulip's heartbeats alone
    await asyncio.sleep(QUIET_UP_S)
    up = sentinel_up_for(vm)
    if up < QUIET_UP_S:  # it started meanwhile (a scenario before this one stopped the VM)
        await asyncio.sleep(QUIET_UP_S - up)
    scenario.notes.append(
        f"DM !vm after the sentinel ran {max(up, QUIET_UP_S):.0f} s and this scenario "
        f"{QUIET_UP_S} s without a DM: one reply means its mark stayed fresh on heartbeats"
    )
    asked = await chat.dm("claude", "!vm")
    reply, took = await chat.reply(asked, r"\S", within=120)
    scenario.timing("DM !vm: the sentinel's reply", took)
    check_vm(reply["content"], SENTINEL_VM, dm=True)
    assert re.search(RUNNING, reply["content"]), reply["content"][-300:]
    texts = await replies(chat, asked)
    assert texts == [reply["content"]], f"{len(texts)} replies to DM !vm: {texts[-1][:120]!r}"
    # ... and the doctor reads that sentinel as healthy, for each bot (#588)
    if installed.has(vm, installed.HAS_SENTINEL_CHECK):
        doctor = vm.run(vm.as_agent("bash", "-lc", DOCTOR), check=False, timeout=300)
        checks = sentinel_checks(doctor.stdout)
        assert checks, (  # a crash's traceback is on stderr
            f"zab doctor (exit {doctor.returncode}) has no <bot>:sentinel check: stdout "
            f"{doctor.stdout[-300:]!r}, stderr {doctor.stderr[-300:]!r}"
        )
        unwell = [c for c in checks if not c.get("ok") or c.get("warning")]
        assert not unwell, f"zab doctor's sentinel checks on a healthy VM: {unwell}"
        scenario.notes.append("zab doctor: " + ", ".join(f"{c['name']} OK" for c in checks))
    else:
        scenario.notes.append("the installed zab doctor has no sentinel check (#588): not read")

    # 2. `!restart` while the bridge runs; a second one too soon, to the other bot
    before = vm.unit()["MainPID"]
    restart = await chat.dm("codex", "!restart")
    await chat.reply(restart, RESTARTING, within=60)
    restarted = time.monotonic()
    # at once, while the first may take longer than its minute to confirm (codex on #573)
    soon = await chat.dm("claude", "!restart")
    await chat.reply(soon, REFUSED, within=30)
    _, took = await chat.reply(restart, BACK, within=CONFIRM_S)
    scenario.timing("DM !restart: confirmed", took, high=CONFIRM_S)
    after = vm.unit()
    assert after["MainPID"] not in ("0", before), f"zab.service's MainPID {before} → {after}"
    assert after["ActiveState"] == "active", after
    texts = await replies(chat, restart, seconds=0)
    assert matching(texts, RESTARTING, BACK), f"replies to !restart: {texts}"
    texts = await replies(chat, soon)
    assert matching(texts, REFUSED), f"replies to the second !restart: {texts}"

    # 3. the bridge stopped: `!vm` still answers, `!restart` brings it back
    await after_the_minute(restarted)
    vm.unit_action("stop")
    try:
        down = await chat.dm("claude", "!vm")
        reply, took = await chat.reply(down, r"\S", within=120)
        scenario.timing("DM !vm, the bridge stopped", took)
        assert re.search(STOPPED, reply["content"]), reply["content"][-300:]
        back = await chat.dm("claude", "!restart")
        await chat.reply(back, RESTARTING, within=60)
        restarted = time.monotonic()
        _, took = await chat.reply(back, BACK, within=CONFIRM_S)
        scenario.timing("DM !restart of the stopped bridge: confirmed", took, high=CONFIRM_S)
    finally:
        if vm.unit().get("ActiveState") != "active":
            await undo(BRIDGE_STOPPED, lambda: vm.unit_action("start"))
    assert vm.unit()["ActiveState"] == "active"

    # 4. the bridge read both DMs at its start (catch-up) and left them to the sentinel
    texts = await replies(chat, down)
    assert matching(texts, STOPPED, RESTARTING, BACK), f"replies since the stop: {texts}"

    # 5. `!restart codex`
    codex = vm.unit(CODEX_UNIT)["MainPID"]
    if codex == "0":
        scenario.notes.append(f"no {CODEX_UNIT} running on the VM: `!restart codex` not tried")
        return
    await after_the_minute(restarted)
    turn = await chat.dm("codex", "!restart codex")
    await chat.reply(turn, CODEX_RESTARTING, within=60)
    _, took = await chat.reply(turn, CODEX_BACK, within=120)
    scenario.timing("DM !restart codex: confirmed", took)
    now = vm.unit(CODEX_UNIT)["MainPID"]
    assert now not in ("0", codex), f"{CODEX_UNIT}'s MainPID {codex} → {now}"
    texts = await replies(chat, turn)
    assert matching(texts, CODEX_RESTARTING, CODEX_BACK), f"replies to !restart codex: {texts}"
