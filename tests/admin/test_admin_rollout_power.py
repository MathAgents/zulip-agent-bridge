"""`!rollout` starts the stopped VMs it needs and stops them again (#995): the plan's `power`
column and note, through the bot's own planner with the scripts' own dry runs (the harness of
`test_admin_plan_fit.py`), and the argument list and deadline that the apply gets."""

import dataclasses

import pytest

from zab.admin import actions, planning
from zab.admin.actions import ROLLOUT_ROW_S, START_AT_ONCE, STARTS_STOPPED, Row
from zab.admin.scrub import Scrubber

from . import test_admin_plan_fit as fit
from .botkit import TESS, TEST, TEST_CH
from .test_admin_actions import FLEET, START, layout, make
from .test_admin_plan_fit import ScriptRunner, essentials, fenced_after, fleet_files, plan_of, site

__all__ = ["layout", "site"]  # the fixtures, used by name
SLUGS = ["zab-test-a", "zab-test-b", "zab-test-c"]
HEALTHY = fit.vm_rows


def stopped(monkeypatch, *slugs: str) -> None:
    """The status read finds `slugs` stopped, as status.py reads them from EC2 (#439)."""
    healthy = HEALTHY

    def rows(names, turns):
        out = healthy(names, turns)
        for vm in out:
            if vm.slug in slugs:
                vm.bridge, vm.zab_version, vm.running_turns = "stopped", None, None
        return out

    monkeypatch.setattr(fit, "vm_rows", rows)


async def test_the_plan_says_which_vms_it_starts_and_stops(site, monkeypatch):
    fleet_files(site, "zab-test", SLUGS, "us-east-1")
    stopped(monkeypatch, "zab-test-b")
    runner = ScriptRunner()
    plan, text, attached = await plan_of(
        site, runner, "!rollout v0.2.4 all", sender=TESS, bot=TEST, stream=TEST_CH
    )
    essentials(plan, text)
    assert attached == [] and STARTS_STOPPED in text
    dry = fenced_after(text, "**Dry run**")
    line = f"stopped VMs: each started for its row, at most {START_AT_ONCE} at a time,"
    assert sum(x.startswith(line) for x in dry) == 1  # one fixed line, no VM's state
    table = fenced_after(text, "**Status**")
    assert table[0].split()[-2:] == ["turns", "power"]
    power = {row.split()[0]: row.split("  ")[-1].strip() for row in table[1:]}
    assert power == {"zab-test-a": "left running", "zab-test-b": "start, then stop",
                     "zab-test-c": "left running"}  # fmt: skip
    # the hash never depends on a VM's state: idle stop between plan and apply changes nothing
    stopped(monkeypatch, "zab-test-a", "zab-test-c")
    again, _, _ = await plan_of(
        site, ScriptRunner(), "!rollout v0.2.4 all", sender=TESS, bot=TEST, stream=TEST_CH
    )
    assert again.hash == plan.hash and again.name != plan.name


@pytest.mark.parametrize(
    ("state", "cell"),
    [("stopped", "start, then stop"), ("stopping", "start, then stop"),
     ("starting", "left running"), ("active, doctor FAIL", "left running"),
     ("unreachable", "?"), ("", "?")],
)  # fmt: skip
def test_the_power_cell_follows_the_state_read(state, cell):
    (vm,) = fit.vm_rows(["alice"], 0)
    vm.bridge = state
    answer = fit.json.dumps(fit.STATUS.fleet_json([vm]))
    read = planning.StatusRead.of(answer, Scrubber(), power=True)
    assert read.lines[0].split()[-1] == "power"
    assert read.lines[1].endswith(f"  {cell}")
    assert "power" not in planning.StatusRead.of(answer, Scrubber()).lines[0]


def test_the_rollout_passes_the_workshops_region_and_scope_and_grows_its_deadline(layout):
    action = make("!rollout v0.2.2 all", layout)
    push, _ = action.steps
    assert " ".join(push.argv).count(START) == 1 and " ".join(push.dry_run).count(START) == 1
    assert action.timeout == 1800.0  # four rows: the floor
    many = dataclasses.replace(
        FLEET, rows=tuple(Row(f"p{n}", f"198.51.100.{n}", "us-west-2") for n in range(1, 41))
    )
    assert make("!rollout v0.2.2 all", layout, many).timeout == 40 * ROLLOUT_ROW_S
    # an adoption's push_config steps start nothing: its spare's first step did
    adopt = make("!adopt-spare carol zab-spare-1", layout)
    assert not [s for s in adopt.steps if "--start-stopped" in s.argv]
    assert actions.STARTS_STOPPED.startswith("**Stopped VMs**")
