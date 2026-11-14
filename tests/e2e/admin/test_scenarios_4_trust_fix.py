"""A24, `!trust-fix` on the rig (docs/ADMIN.md §7; #992): the VMs' trust fix as a plan through
`Workshop admin · e2e`, as ORGANIZERS §4.4 has an organizer send it before a rollout, on A4's
VMs. They are launched from the rig's participants' image (`ZAB_ADMIN_E2E_AMI`), a copy of the
Oct 19 image: its uv older than 0.13.4, certifi 2026.7.22, and, until unattended upgrades or a
rollout fix them, the older `ca-certificates` and the expired Macaulay2 apt source
(`workshop/admin/README.md` §10).

- **Claude Code's store, as the fleet's.** The image's managed settings lack
  `CLAUDE_CODE_CERT_STORE`, but since #885 `launch.py`'s first boot writes `system` there on
  every VM it launches, A4's included; the fleet launched before #885 lacks it. So first, over
  the fleet's SSH from the server, as root, the key is taken out of `zab-test-b`'s managed
  settings (`UNSET`), every other key kept, and read back gone: step 5 then has its work, as on
  that fleet. Nothing puts it back but the trust fix itself.
- **The plan,** `!trust-fix all`, with `zab-test-a` stopped and `zab-test-b` running, as A21
  leaves them: one approver, no phrase; its one step `trust_fix.py --admin-bot`, whose dry run
  is the same list without `--apply`. The dry run's table has a row per VM: `zab-test-b`'s with
  `FAILED` under `check` and `…: would set system` under `claude store`, from which the first
  apply's cell follows (`store_after`), `zab-test-a`'s `not reached`; it ends
  `1 VM(s) reached (dry run: nothing changed); not done: zab-test-a`, and exits 0 all the same.
  The status table reads `zab-test-a` `stopped`.
- **The apply,** approved: it ends `❌`, its one step exiting 1, since `zab-test-a` wasn't
  reached, and its last line names `!trust-fix zab-test-a`. `zab-test-b`'s row reads `ok` under
  `check` (or `ok, <n> left (README §10)`), and `unset -> system` under `claude store`, as its
  dry run's cell said; before the table, a line per VM as it ended. `zab-test-a` is
  still stopped: nothing started it. A row whose step names apt's lock, unattended upgrades
  soon after the VM's start, is sent again some minutes later, as an organizer would; the
  first apply's own changes are checked on its row, since a later one reads them `current`.
- **On the VM,** as `ubuntu` over the fleet's SSH from the server: Claude Code's managed settings
  hold `CLAUDE_CODE_CERT_STORE=system` in their `env`, and no idle-stop hold is left.
- **Then `!trust-fix zab-test-b`:** its dry run reads `ok` under `check`, with nothing it
  `would` change; `!reject`ed.

It leaves the fleet as A21 does: `zab-test-a` stopped, `zab-test-b` running. It needs A4's VMs
alone (`launched`), no provisioning (Q9). Run again on the same VMs, the key is taken out
again, so step 5 runs again, and the other steps read `current`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import pytest

from e2e.admin.conftest import requires
from e2e.admin.harness.chat import Plan, Talk, status_rows, table
from e2e.admin.harness.rig import E2E
from e2e.admin.harness.settle import until
from e2e.admin.test_scenarios_2_bot import ip_of, on_vm, talk, vm_state
from e2e.admin.test_scenarios_3_late_joiner import room_for

A, B = "zab-test-a", "zab-test-b"
SCRIPT = "admin/trust_fix.py"
MANAGED = "/etc/claude-code/managed-settings.json"
HOLD = "/run/zab-idle-stop.hold"
#: A row's `check` once the fix is done: what agents installed is reported and left (§10).
DONE = re.compile(r"ok(, \d+ left \(README §10\))?")
#: apt's lock, which unattended upgrades hold soon after a boot: a few minutes more (§10).
APT_LOCK = "Could not get lock"
#: How often, and how long apart, a row stopped on apt's lock is sent again.
TRIES, AGAIN_S = 3, 240.0
#: As root on the VM: `CLAUDE_CODE_CERT_STORE` out of Claude Code's managed settings, every other
#: key kept, the file's mode and owner too; it prints what is left.
UNSET = (
    "import json, pathlib; p = pathlib.Path('/etc/claude-code/managed-settings.json'); "
    "d = json.loads(p.read_text()); d.get('env', {}).pop('CLAUDE_CODE_CERT_STORE', None); "
    "p.write_text(json.dumps(d)); print(json.dumps(d))"
)
#: A `claude store` cell with step 5's work to do (`trust_fix_remote.step_claude`).
WOULD = ": would set system"


def rows(text: str) -> dict[str, dict[str, str]]:
    """trust_fix.py's table in `text`, by participant."""
    return {r["participant"]: r for r in table(text, "participant")}


def steps_of(row: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in row.items() if k not in ("participant", "host", "check")}


async def plan_of(t: Talk, command: str, targets: list[str]) -> Plan:
    plan = await t.request(command)
    assert plan.targets == targets, f"{plan.id}'s targets: {plan.targets}"
    assert not plan.phrase, f"{plan.id} asks for the phrase {plan.phrase!r}"
    (step,) = plan.commands  # the apply's list (`planning._step`); the dry run's lacks --apply
    assert SCRIPT in step and "--admin-bot" in step.split(), step
    assert step.split()[-1] == "--apply" and step.count("--apply") == 1, step
    assert f"── {SCRIPT} exited 0" in plan.text, plan.text[-1500:]
    return plan


def changed(row: dict[str, str]) -> dict[str, str]:
    """The cells of `row` that an apply changed (`old -> new`)."""
    return {k: v for k, v in steps_of(row).items() if " -> " in v}


async def until_fixed(t: Talk, scenario: Any, first: dict[str, str]) -> dict[str, str]:
    """`zab-test-b`'s row once its fix is done: `first`, the first apply's, or, while a step of
    the last apply names apt's lock, the row of `!trust-fix zab-test-b` sent again, up to
    `TRIES` times, `AGAIN_S` apart; the last one otherwise, for the caller to fail on. A step the
    first apply did reads `…, current` in a later one's row (`claude store`, which never waits
    for apt: `trust_fix_remote.run_steps`), so the first apply's own changes are checked on
    `first` (`fixed_rows`, Codex round 1 on #994)."""
    row = first
    for n in range(TRIES):
        if DONE.fullmatch(row["check"]) or APT_LOCK not in json.dumps(row):
            break
        scenario.notes.append(f"{B} waited on apt's lock (try {n + 1}): {row}")
        await asyncio.sleep(AGAIN_S)
        again = await plan_of(t, f"!trust-fix {B}", [B])
        _, text = await t.approve(again)
        row = rows(text)[B]
        scenario.notes.append(f"{B}'s apply again: {changed(row)}")
    return row


def store_after(cell: str) -> str:
    """The first apply's `claude store` cell that follows from the dry run's (`step_claude`):
    `<value>: would set system` becomes `<value> -> system`; `system, current` stays."""
    if cell == "system, current":
        return cell
    assert cell.endswith(WOULD), f"a `claude store` cell that no apply sets: {cell}"
    return f"{cell.removesuffix(WOULD)} -> system"


def planned(row: dict[str, str]) -> str:
    """`zab-test-b`'s dry-run row, once its key is out (`UNSET`): `FAILED` under `check`, and
    step 5 with its work to do. The first apply's `claude store` cell that follows from it."""
    assert row["check"] == "FAILED", f"{B}'s dry run: {row}"
    assert row["claude store"].endswith(WOULD), f"step 5 has nothing to do on {B}: {row}"
    return store_after(row["claude store"])


def fixed_rows(first: dict[str, str], last: dict[str, str], *, store: str) -> None:
    """The fix done on `zab-test-b`: Claude Code's store as the first apply set it (`store`, from
    the dry run's cell), and the last apply's `check` passing."""
    assert first["claude store"] == store, f"the first apply's row, not {store!r}: {first}"
    assert DONE.fullmatch(last["check"]), f"{B}'s row after the apply: {last}"


def unset(server: Any, ip: str) -> None:
    """`CLAUDE_CODE_CERT_STORE` out of the VM's managed settings, as the fleet launched before
    #885 has them, read back gone."""
    done = on_vm(server, ip, "sudo", "/usr/bin/python3", "-I", "-c", UNSET)
    assert done.ok, f"{MANAGED} on {B}: exit {done.returncode}: {done.stderr[-300:]}"
    read = on_vm(server, ip, "sudo", "cat", MANAGED)
    assert read.ok, f"{MANAGED} on {B}: exit {read.returncode}: {read.stderr[-300:]}"
    left = json.loads(read.stdout)
    assert "CLAUDE_CODE_CERT_STORE" not in left.get("env", {}), read.stdout


async def ssh_answers(server: Any, ip: str) -> None:
    """The VM answers the fleet's SSH from the server, a minute or so after its start."""
    await until(lambda: on_vm(server, ip, "true").ok, lambda ok: ok,
                what=f"{ip}'s SSH from the server", within=300, every=10)  # fmt: skip


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A24")
@requires("launched")
async def test_a24_the_trust_fix_as_a_plan(scenario, tester, rigged, server, aws, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A24-{nonce}")
    await room_for(t, aws, running=(B,), stopped=(A,))
    await ssh_answers(server, ip_of(server, B))
    unset(server, ip_of(server, B))
    plan = await plan_of(t, "!trust-fix all", [A, B])
    shown = rows(plan.text)
    assert shown[A]["check"].startswith("not reached: "), shown[A]
    assert status_rows(plan.text).get(A, {}).get("state") == "stopped", plan.text[-1500:]
    assert "1 VM(s) reached (dry run: nothing changed); not done: zab-test-a" in plan.text
    store = planned(shown[B])
    would = {k: v for k, v in steps_of(shown[B]).items() if "would " in v}
    scenario.notes.append(f"{B}'s dry run: {would}")
    started = time.monotonic()
    _, text = await t.approve(plan)
    scenario.timing("!trust-fix of one VM, one stopped, approval to result",
                    time.monotonic() - started)  # fmt: skip
    head = f"❌ **{plan.id}** · `!trust-fix all`: failed at step 1 of 1: {SCRIPT} exited 1"
    assert text.startswith(head), text.split("\n")[0]
    assert "Then rerun them:\n  !trust-fix zab-test-a" in text, text[-1500:]
    done = rows(text)
    assert done[A]["check"].startswith("not reached: "), done[A]
    assert f"\ndone: {B} (" in text and f"\ndone: {A} (" in text, text[-1500:]  # as each ended
    scenario.notes.append(f"{B}'s apply: {changed(done[B])}")
    fixed_rows(done[B], await until_fixed(t, scenario, done[B]), store=store)
    assert vm_state(aws, A) == "stopped", "the trust fix started a stopped VM"
    ip = ip_of(server, B)
    read = on_vm(server, ip, "sudo", "cat", MANAGED)
    assert read.ok, f"{MANAGED} on {B}: exit {read.returncode}: {read.stderr[-300:]}"
    assert json.loads(read.stdout)["env"]["CLAUDE_CODE_CERT_STORE"] == "system", read.stdout
    assert on_vm(server, ip, "test", "-e", HOLD).returncode == 1, f"{HOLD} left on {B}"
    after = await plan_of(t, f"!trust-fix {B}", [B])
    row = rows(after.text)[B]
    assert DONE.fullmatch(row["check"]), row
    assert not [v for v in steps_of(row).values() if "would " in v], row
    await t.ask(f"!reject {after.id}", rf"{after.id} rejected")
