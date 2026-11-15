"""A25, `!rollout` starts the stopped VMs it needs and stops them again (#995; docs/ADMIN.md §7),
through `Workshop admin · e2e`, on A4's VMs as A21 and A24 leave them: `zab-test-a` stopped and
`zab-test-b` running.

- **The plan,** `!rollout <rc tag> all`: the phrase `rollout 2`; its `push_config.py` step
  carries `--start-stopped 8`, the rig's region and each of the workshop's scope tags once
  (`--scope-tag`); its dry run, one fixed line for it (`stopped VMs: each started for its row,
  at most 8 at a time, …`), and no VM's state; its notes, `**Stopped VMs**`; its status table,
  `zab-test-a` `stopped` with `start, then stop` under `power`, and `zab-test-b` `left running`.
- **The apply,** approved with the phrase: `✅`; its output, `power: zab-test-a i-…: started for
  its row`, then `power: zab-test-a i-…: stopped again`, and no such line for `zab-test-b`; its
  table's `power` column, `started, stopped again` for `zab-test-a`, whose install passed, and
  `running` for `zab-test-b`; the line under it, `stopped VMs: 1 started, 1 stopped again`.
- **Then EC2:** `zab-test-a` stopped again within 10 minutes of the result, and `zab-test-b`
  running, as it was, rolled out too (`install` `ok`).
- **If `zab-test-a` comes back `started, left running: <why>`,** the part stops it itself, so
  that the fleet is left as it was found, and then fails, naming the reason: nothing on the rig
  uses that VM (a never-bound row's bots ignore every message, and the collector reads as
  `zab-monitor`, which the check leaves out), so such a verdict is most likely the check's false
  positive, which in the fleet would leave every VM a rollout starts running. If it was a stray
  event, run A25 again (review round 2 of #1027).

It leaves the fleet as it found it. It needs A4's VMs and A5's bots (`launched`, `provisioned`),
as A6 does, and no other VM: at most three instances run (§7), the server and these two.
"""

from __future__ import annotations

import re
import time

import pytest

from e2e.admin.conftest import requires
from e2e.admin.harness.chat import status_rows, table
from e2e.admin.harness.rig import E2E
from e2e.admin.test_scenarios_2_bot import talk, vm_state, wait_state
from e2e.admin.test_scenarios_3_late_joiner import room_for, values

A, B = "zab-test-a", "zab-test-b"
#: The cell of a VM the rollout started and left running: never right here (docstring).
LEFT = "started, left running: "
#: The bot's bound on the VMs one rollout has started at once (`actions.START_AT_ONCE`).
AT_ONCE = "8"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A25")
@requires("launched", "provisioned")
async def test_a25_a_rollout_starts_a_stopped_vm_and_stops_it_again(
    scenario, tester, rigged, admin_rig, aws, nonce
):
    t = talk(tester, rigged, E2E.name, f"e2e-A25-{nonce}")
    await room_for(t, aws, running=(B,), stopped=(A,))
    plan = await t.request(f"!rollout {rigged.release} all")
    assert plan.phrase == "rollout 2", f"the phrase: {plan.phrase!r}"
    (push,) = [c.split() for c in plan.commands if "push_config" in c]
    assert values(push, "--start-stopped") == [AT_ONCE], push
    assert values(push, "--region") == [admin_rig.region], push
    scope = values(push, "--scope-tag")
    assert f"workshop={E2E.name}" in scope and len(set(scope)) == len(scope), scope
    assert any(tag.startswith("zab-scope=") for tag in scope), scope
    assert f"\nstopped VMs: each started for its row, at most {AT_ONCE} at a time," in plan.text
    assert "**Stopped VMs**" in plan.text, "the plan's note (#995)"
    shown = status_rows(plan.text)
    assert shown[A]["state"] == "stopped" and shown[A]["power"] == "start, then stop", shown
    assert shown[B]["power"] == "left running", shown
    started = time.monotonic()
    _, text = await t.approve(plan)
    scenario.timing("rollout of 2 VMs, one started and stopped again", time.monotonic() - started)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    assert re.search(rf"^power: {A} i-[0-9a-f]+: started for its row$", text, re.M), text
    assert not re.search(rf"^power: {B} ", text, re.M), text[-1500:]
    rows = {r["participant"]: r for r in table(text, "participant")}
    assert rows[A]["install"] == rows[B]["install"] == "ok", (rows[A], rows[B])
    assert rows[B]["power"] == "running", rows[B]
    if rows[A]["power"].startswith(LEFT):  # no use on the rig: the check's false positive
        why = rows[A]["power"].removeprefix(LEFT)
        await t.run(f"!stop {A}", phrase=False)  # the fleet as it was found, then the failure
        await wait_state(aws, A, "stopped")
        pytest.fail(f"{A} left running: {why}: nothing on the rig uses it, so the check read a "
                    "use that isn't there; run A25 again if it was a stray event")  # fmt: skip
    assert rows[A]["power"] == "started, stopped again", rows[A]
    assert re.search(rf"^power: {A} i-[0-9a-f]+: stopped again$", text, re.M), text[-1500:]
    assert "\nstopped VMs: 1 started, 1 stopped again" in text, text[-1500:]
    await wait_state(aws, A, "stopped")
    assert vm_state(aws, B) == "running", "the rollout stopped a VM that was running"
