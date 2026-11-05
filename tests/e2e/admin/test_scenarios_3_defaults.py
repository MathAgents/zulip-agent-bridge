"""A23, the rollout's defaults (docs/ADMIN.md §7; #704): a `!rollout` that names neither
`lean-shared` nor `idle-stop=` gives a VM whose waker is on the shared Mathlib and idle stop at
60 minutes (ORGANIZERS §3.5), through `Workshop admin · e2e`.

**A VM whose idle stop was never set.** The default leaves alone a VM whose timer is on, and one
whose idle stop was turned off on purpose: the record `--idle-stop 0` writes, or idle stop's
log on a VM turned off before that record existed. So A23 needs a VM with none of the three
(`NEVER_SET`), and checks that first, on the VM. `zab-test-b` is one here: A4 launches it
afresh each run, and nothing before A23 sets its idle stop. A6's rollout leaves out the words
too, but the waker is off then, so it only probes. A23 comes after A8 and before A20
(`test_scenarios_3_defaults.py`, by file name), whose explicit `idle-stop=240` and `!idle-stop
0` would make it a VM of the other kinds.

**Then:** `!waker on zab-test-b`, its config checked to name the VM's instance; `!rollout <rc>
zab-test-b`, without the words. Its plan passes `--lean-shared-default`, `--idle-stop-default
60` and `--has-waker zab-test-b=<its instance>`, its notes say both defaults, and its dry run
names their steps. Applied, the row notes `idle stop: after 60 min`, never `not this VM`: the
probe read cloud-init's `/var/lib/cloud/data/instance-id` as `ubuntu`, without sudo, and found
the instance the waker names (VERIFY V19). `!status zab-test-b` reads `60 min`. On the VM,
`ubuntu` reads that instance from the file, `lean-project` is installed, and the workspace's
`.lake/packages` is a link into the shared copy, `/opt/lean/packages/<tag>`.

**Which path the shared Mathlib took.** A23 reads first, as push_config.py's probe does,
whether the VM is shared already: its packages a link and `lean-project` there. On the rig's
image, which has the shared Mathlib from its build, and after A6, which applies the default
too, it should be. Then the default only probes: the row has no `lean: shared Mathlib` note,
so no timing build ran. On a VM not shared yet, the default sets it up, and the row has that
note. Either way the step's failures are absent, and the scenario's notes say which path ran.

**It puts the VM back as A8 leaves it,** in a `finally`, whatever happened: `!idle-stop 0
zab-test-b` once the rollout was approved, then `!waker off zab-test-b` (ORGANIZERS §5.2: idle
stop off, then the waker). The VM stays running, with neither, and with the record of idle stop
turned off on purpose, which A20's explicit `idle-stop=240` removes.

**At most three instances run,** the server's included (§7): `zab-test-b` alone runs here;
`zab-test-a` may run beside it. Any other VM of the rig that runs makes A23 skip, naming it.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import AsyncIterator

import pytest

from e2e.admin.conftest import requires
from e2e.admin.harness.chat import Plan, Talk
from e2e.admin.harness.rig import E2E
from e2e.admin.test_scenarios_2_bot import ip_of, on_vm, talk, vm_states, waker_on
from e2e.admin.test_scenarios_3_late_joiner import (
    KINDS,
    idle,
    minutes_on,
    read_waker,
    room_for,
    script_of,
    values,
)
from e2e.admin.test_scenarios_8_the_end import statuses

A, B = "zab-test-a", "zab-test-b"
#: The default's minutes (`actions.IDLE_STOP_DEFAULT`).
MINUTES = 60
#: What push_config.py's idle-stop probe reads (`IDLE_STOP_PROBE_CMD`): the record of idle stop
#: turned off on purpose, idle stop's log, and its timer. A VM with any of them is not one whose
#: idle stop was never set, which the default turns on.
NEVER_SET = (
    "/etc/zab/idle-stop.off",
    "/var/log/zab-idle-stop.log",
    "/etc/systemd/system/zab-idle-stop.timer",
)
#: Where cloud-init records the VM's instance ID, which the probe reads as `ubuntu` (V19).
INSTANCE_ID = "/var/lib/cloud/data/instance-id"
#: The shared Mathlib on the VM (push_config.py's `LEAN_PROJECT_PATH`, `LEAN_PACKAGES`; #205).
LEAN_PROJECT = "/usr/local/bin/lean-project"
PACKAGES = "/home/agent/lean/workspace/.lake/packages"
SHARED = "/opt/lean/packages/"
#: The plan's notes on the defaults (`actions._default_notes`), by their start.
NOTES = ("**Shared Mathlib (default):** ", f"**Idle stop: {MINUTES} min (default)** ")
#: The steps of the defaults that the dry run names, as on a new VM (push_config.py's
#: `_DRY_NAMES`): the shared Mathlib's, then idle stop's where the waker is on.
DRY_STEPS = ("lean-project + shared Mathlib", "lean check", "idle-stop probe",
             "idle stop installed")  # fmt: skip
#: The row's one idle-stop note on a VM whose idle stop was never set and whose waker is on: the
#: default turned it on (push_config.py's `idle_stop_step`). Any other `idle stop: …` note says
#: it left the VM as it is (`idle_stop_default_step`): `… not this VM (unknown)` where the probe
#: couldn't read the instance ID (V19), `… isn't on`, `left off`, or a `WARNING`.
INSTALLED = f"note: idle stop: after {MINUTES} min"
#: What the shared Mathlib's step leaves in the row when it runs (push_config.py's
#: `lean_shared_step`): passed, this note, with its timing build (`lean: shared Mathlib: a
#: Mathlib file in …`); failed, one of these failures instead. With neither, it didn't run: the
#: default only probes a VM shared already, its packages a link and lean-project there
#: (`lean_default`).
LEAN_NOTE = "note: lean: shared Mathlib"
LEAN_FAILED = ("scp lean-project: ", "lean shared: ", "lean check: ", "lean rollback: ")


# ------------------------------------------------------------------------------ reading


def pushes(plan: Plan) -> list[list[str]]:
    """The argument lists of the plan's push_config.py steps."""
    return [c.split() for c in plan.commands if script_of(c) == "admin/push_config.py"]


def default_flags(argv: list[str], slug: str, instance: str) -> list[str]:
    """What is wrong with a push_config.py step of a rollout without the words for `slug`,
    whose waker names `instance`: each a line; none when it passes the defaults alone."""
    wrong = []
    if "--lean-shared-default" not in argv or "--lean-shared" in argv:
        wrong.append("not --lean-shared-default alone")
    if values(argv, "--idle-stop-default") != [str(MINUTES)] or "--idle-stop" in argv:
        wrong.append(f"not --idle-stop-default {MINUTES} alone")
    if values(argv, "--has-waker") != [f"{slug}={instance}"]:
        wrong.append(f"--has-waker {values(argv, '--has-waker')}, not {slug}={instance}")
    return wrong


def dry_line(text: str, slug: str) -> str:
    """`slug`'s line of the dry run (`dry-run <slug> (<ip>): …`), or "": the longest, since a
    plan cut to fit shows it cut, and its attachment whole."""
    found = re.findall(rf"^dry-run {re.escape(slug)} \([^)\n]*\): [^\n]*$", text, re.M)
    return max(found, key=len, default="")


def row_lines(text: str, slug: str) -> list[str]:
    """Every line of `text` that starts a row of `slug` in a table (`<slug>`, then two spaces):
    push_config.py's result, in the message and again, whole, in its attachment when it was
    cut, and the status read's."""
    return re.findall(rf"^{re.escape(slug)}  [^\n]*$", text, re.M)


def idle_notes(rows: list[str]) -> list[str]:
    """The `note: idle stop: …` notes of `rows`, each once, a cut one (`… …`) left out."""
    found = {m.rstrip() for ln in rows for m in re.findall(r"note: idle stop: [^;\n]*", ln)}
    return sorted(note for note in found if not note.endswith("…"))


def lean_path(rows: list[str], *, shared: bool) -> tuple[str, list[str]]:
    """The path the shared Mathlib's default took in `rows`, and what is wrong with it, each a
    line. A VM `shared` before the rollout (its packages a link, lean-project there) is only
    probed: no sign of the step, so no timing build. Any other VM gets the step, which notes
    the shared copy. A failure of the step is wrong on either."""
    ran = any(LEAN_NOTE in ln for ln in rows)
    failed = [ln for ln in rows if any(word in ln for word in LEAN_FAILED)]
    wrong = [f"the shared Mathlib's step failed: {failed}"] if failed else []
    if shared and ran:
        wrong.append(f"the step ran, with its timing build, on a VM shared already: {rows}")
    if not shared and not ran:
        wrong.append(f"no `{LEAN_NOTE} …` on a VM not shared yet: {rows}")
    path = "only probed, shared already" if shared else "set up by the default"
    return path, wrong


# ------------------------------------------------------------------------------ doing


@contextlib.asynccontextmanager
async def idle_stop_off_after(t: Talk, slug: str, made: dict[str, bool]) -> AsyncIterator[None]:
    """`!idle-stop 0 <slug>` in a `finally` once `made["rollout"]` says that a rollout that may
    have turned it on was approved; checked when the body passed, best effort after a failure,
    whose own error is the one reported (as `waker_on`, which comes after it: idle stop off,
    then the waker, ORGANIZERS §5.2)."""
    passed = False
    try:
        yield
        passed = True
    finally:
        try:
            if made.get("rollout"):
                await t.run(f"!idle-stop 0 {slug}")
        except Exception:
            if passed:
                raise


# ------------------------------------------------------------------------------ A23


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A23")
@requires("launched", "provisioned")
async def test_a23_a_rollout_without_the_words_applies_the_defaults(
    scenario, tester, rigged, server, aws, nonce
):
    t = talk(tester, rigged, E2E.name, f"e2e-A23-{nonce}")
    await room_for(t, aws, running=(B,), beside=(A,))
    await statuses(t, [B])  # reachable: its SSH answers, for the checks on the VM
    ip, instance = ip_of(server, B), vm_states(aws)[B]["InstanceId"]
    checked = {path: on_vm(server, ip, "test", "-e", path).returncode for path in NEVER_SET}
    assert set(checked.values()) <= {0, 1}, f"`test -e` on {B} as ubuntu: {checked}"
    found = [path for path, code in checked.items() if code == 0]
    assert not found, (
        f"{B} is not a VM whose idle stop was never set, which A23 needs: it has {found}. "
        "A part before A23 set or turned off its idle stop (module doc)"
    )
    read = on_vm(server, ip, "cat", INSTANCE_ID)  # as ubuntu, without sudo (V19)
    assert read.ok and read.stdout.strip() == instance, (
        f"as ubuntu, {INSTANCE_ID} on {B}: exit {read.returncode}, {read.stdout.strip()[:40]!r}, "
        f"not {instance} (VERIFY V19)"
    )
    scenario.notes.append(f"V19: ubuntu reads {instance} from {INSTANCE_ID}, without sudo")
    link = on_vm(server, ip, "sudo", "-n", "test", "-L", PACKAGES).returncode
    tool = on_vm(server, ip, "test", "-x", LEAN_PROJECT).returncode
    assert {link, tool} <= {0, 1}, f"on {B}: `test -L` exit {link}, `test -x` exit {tool}"
    shared = link == 0 and tool == 0  # as push_config.py's probe reads it (`lean_default`)
    scenario.notes.append(
        f"before the rollout: packages a link {link == 0}, lean-project there {tool == 0}"
    )
    made: dict[str, bool] = {}
    async with waker_on(t, B):  # `!waker off zab-test-b` last, whatever happens
        plan, _ = await t.run(f"!waker on {B}", phrase=False)
        assert plan.targets == [B], f"the waker plan's targets: {plan.targets}"
        bots = read_waker(server)
        listed = {bots.get(f"{kind}-{B}", {}).get("instance_id") for kind in KINDS}
        assert listed == {instance}, f"the waker's config names {listed} for {B}, EC2 {instance}"
        async with idle_stop_off_after(t, B, made):  # `!idle-stop 0 zab-test-b` first
            plan = await t.request(f"!rollout {rigged.release} {B}")
            assert plan.targets == [B] and not plan.phrase, (plan.targets, plan.phrase)
            steps = pushes(plan)
            assert len(steps) == 1, f"the plan's push_config.py steps: {plan.commands}"
            wrong = default_flags(steps[0], B, instance)
            assert not wrong, f"{'; '.join(wrong)}: {' '.join(steps[0])}"
            for start in NOTES:
                assert any(ln.startswith(start) for ln in plan.text.split("\n")), (
                    f"no plan note `{start.strip()} …`: {plan.text[:1500]}"
                )
            assert "Not on " not in plan.text, f"a target without the waker: {plan.text[:1500]}"
            dry = dry_line(plan.text, B)
            missing = [step for step in DRY_STEPS if f"; {step};" not in dry]
            assert dry and not missing, f"the dry run of {B} lacks {missing}: {dry or plan.text}"
            made["rollout"] = True  # the `finally` turns idle stop off again, whatever happens
            started = time.monotonic()
            _, text = await t.approve(plan)
            scenario.timing("rollout of 1 VM with the defaults", time.monotonic() - started)
            assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
            rows = row_lines(text, B)
            noted = idle_notes(rows)
            assert noted == [INSTALLED], f"{B}'s idle-stop notes: {noted}, rows {rows or text}"
            path, wrong = lean_path(rows, shared=shared)
            assert not wrong, "; ".join(wrong)
            scenario.notes.append(f"the shared Mathlib: {path}")
            line = next(ln for ln in rows if INSTALLED in ln)
            scenario.notes.append(f"{B}'s row: {line.rstrip().rsplit('  ', 1)[-1][:300]}")
            vms = await statuses(t, [B])
            shown = idle(vms[B])
            assert shown == minutes_on(MINUTES), f"!status reads {B}'s idle stop as {shown}"
            checks = (
                ("lean-project", on_vm(server, ip, "test", "-x", LEAN_PROJECT)),
                ("the link", on_vm(server, ip, "sudo", "-n", "test", "-L", PACKAGES)),
            )
            for what, done in checks:
                assert done.ok, f"{B}: {what} of the shared Mathlib: exit {done.returncode}"
            target = on_vm(server, ip, "sudo", "-n", "readlink", "-f", PACKAGES).stdout.strip()
            assert target.startswith(SHARED), f"{PACKAGES} on {B} leads to {target!r}"
            scenario.notes.append(f"shared Mathlib: {PACKAGES} → {target}")
