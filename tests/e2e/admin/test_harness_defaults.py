"""A23's readers and its clean-up (#704), offline, against what the product itself writes: a
`!rollout` without the words as `actions.build` makes it and the plan shows it, the paths and
step names of `push_config.py`'s defaults, and its result table."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from admin.test_admin_rollout_defaults import CONFIG, A, layout, make, waker
from e2e.admin import test_scenarios_3_defaults as a23
from e2e.admin.test_harness_late_joiner import script
from zab.admin import planning
from zab.admin.actions import IDLE_STOP_DEFAULT

__all__ = ["layout"]  # the fixture, used by name


def pc():
    return script("ae2e_push_config", "workshop/admin/push_config.py")


def plan_of(action) -> SimpleNamespace:
    """The plan's commands as it shows them (`planning._step`), and its notes as its text."""
    shown = [planning._step(step.argv, step.env) for step in action.steps]
    return SimpleNamespace(commands=shown, text="\n".join(action.notes))


def test_the_flags_are_the_rollouts_without_the_words(layout):
    """alice's waker is on, for the VM `A` (`CONFIG`): the defaults alone, and alice named with
    that VM. The words, or a row without the waker, are each named."""
    waker(layout, CONFIG)
    plan = plan_of(make("!rollout v0.2.2 alice", layout))
    steps = a23.pushes(plan)
    assert len(steps) == 1 and a23.default_flags(steps[0], "alice", A) == []
    assert a23.default_flags(steps[0], "alice", "i-0ffffffffffffffff") == [
        f"--has-waker ['alice={A}'], not alice=i-0ffffffffffffffff"
    ]
    for words, wrong in (
        ("lean-shared", "not --lean-shared-default alone"),
        ("idle-stop=60", f"not --idle-stop-default {a23.MINUTES} alone"),
    ):
        explicit = a23.pushes(plan_of(make(f"!rollout v0.2.2 alice {words}", layout)))[0]
        assert wrong in a23.default_flags(explicit, "alice", A), (words, explicit)
    bob = a23.pushes(plan_of(make("!rollout v0.2.2 bob", layout)))[0]
    assert a23.default_flags(bob, "bob", A) == [f"--has-waker [], not bob={A}"]


def test_the_notes_are_the_plans(layout):
    waker(layout, CONFIG)
    notes = make("!rollout v0.2.2 alice", layout).notes
    assert all(any(n.startswith(start) for n in notes) for start in a23.NOTES), notes
    assert not any(n.startswith("Not on ") for n in notes)
    without = make("!rollout v0.2.2 bob", layout).notes
    assert any(n.startswith("Not on bob: ") for n in without), without


def test_the_paths_steps_and_minutes_are_push_configs():
    module = pc()
    timer = f"{module._UNIT_DIR}/{module.IDLE_STOP_UNIT}.timer"
    assert a23.NEVER_SET == (module.IDLE_STOP_OFF_RECORD, module.IDLE_STOP_LOG, timer)
    assert all(f"test -e {path}" in module.IDLE_STOP_PROBE_CMD for path in a23.NEVER_SET)
    assert a23.INSTANCE_ID == module.INSTANCE_ID_FILE
    assert (a23.LEAN_PROJECT, a23.PACKAGES) == (module.LEAN_PROJECT_PATH, module.LEAN_PACKAGES)
    assert set(a23.DRY_STEPS) <= set(module._DRY_NAMES.values())
    assert a23.MINUTES == IDLE_STOP_DEFAULT


def test_the_dry_line_is_the_longest_of_the_rows():
    """A plan cut to fit shows the line cut; its attachment, whole."""
    whole = "dry-run zab-test-b (198.51.100.2): owner check; " + "; ".join(a23.DRY_STEPS) + "; x"
    cut = whole[:60] + " …"
    text = f"```\n{cut}\n```\ndry-run zab-test-a (198.51.100.1): owner check\n{whole}"
    assert a23.dry_line(text, "zab-test-b") == whole
    assert all(f"; {step};" in a23.dry_line(text, "zab-test-b") for step in a23.DRY_STEPS)
    assert a23.dry_line(text, "zab-test-c") == ""


def test_the_rows_idle_stop_notes_are_read_from_push_configs_table():
    """The one note where the default turned idle stop on; any other is named, a cut one left
    out, and the status read's row of the same slug adds none."""
    module = pc()

    def table(*notes: str) -> str:
        rows = [module.VmResult("zab-test-b", "ws-zab-test-b", installed=True, doctor_ok=True,
                                notes=["sage: skipped: x", *notes]),
                module.VmResult("zab-test-a", "ws-zab-test-a", installed=True, doctor_ok=True,
                                notes=["idle stop: left as it is, since …"])]  # fmt: skip
        return module.render(rows) + "\nzab-test-b  active  0.2.16rc1  60 min  0\n"

    on = table(f"idle stop: after {a23.MINUTES} min")
    assert a23.idle_notes(a23.row_lines(on, "zab-test-b")) == [a23.INSTALLED]
    unknown = (  # push_config.py's, where the probe couldn't read the instance ID (V19)
        f"idle stop: left as it is, since the waker names {A}, not this VM (unknown): "
        f"!waker on zab-test-b, then !idle-stop {a23.MINUTES} zab-test-b"
    )
    shown = a23.idle_notes(a23.row_lines(table(unknown), "zab-test-b"))
    assert shown == [f"note: {unknown}"]
    cut = on.replace(f"after {a23.MINUTES} min", "after 6 …")
    assert a23.idle_notes(a23.row_lines(cut, "zab-test-b")) == []


def lean_rows(*, fails: str = "") -> list[str]:
    """`zab-test-b`'s rows of push_config.py's result table after its shared-Mathlib step ran
    (`lean_shared_step`, every command answered by a fake VM: the check's timing, or a failure
    of the command whose text holds `fails`)."""
    module = pc()
    result = module.VmResult("zab-test-b", "ws-zab-test-b", installed=True, doctor_ok=True)

    def runner(argv, timeout_s):
        if fails and fails in " ".join(argv):
            return module.RunResult(1, "", f"{fails} went wrong")
        return module.RunResult(0, "lean-project: checked in 52.1 s\nchecked in 9.8 s\n", "")

    opts = module.Options(tag="v0.2.16", known_hosts="/tmp/kh")  # noqa: S108 - never opened
    module.lean_shared_step(result, opts, runner, "/tmp/zab-push.x")  # noqa: S108 - a name
    result.notes.append(f"idle stop: after {a23.MINUTES} min")
    return a23.row_lines(module.render([result]), "zab-test-b")


def skipped_rows() -> list[str]:
    """The rows of a VM the default only probed: its step never ran, so it noted nothing."""
    module = pc()
    result = module.VmResult("zab-test-b", "ws-zab-test-b", installed=True, doctor_ok=True,
                             notes=[f"idle stop: after {a23.MINUTES} min"])  # fmt: skip
    return a23.row_lines(module.render([result]), "zab-test-b")


def test_a_vm_shared_already_is_only_probed():
    """The rig's image has the shared Mathlib, and A6 applied the default before: no sign of
    the step, so no timing build. The step's note there is wrong."""
    assert a23.lean_path(skipped_rows(), shared=True) == ("only probed, shared already", [])
    path, wrong = a23.lean_path(lean_rows(), shared=True)
    assert path == "only probed, shared already"
    assert wrong and "the step ran, with its timing build, on a VM shared already" in wrong[0]


def test_a_vm_not_shared_yet_gets_the_step_and_its_note():
    rows = lean_rows()
    assert any(f"{a23.LEAN_NOTE}: a Mathlib file in 52.1 s, then 9.8 s" in ln for ln in rows)
    assert a23.lean_path(rows, shared=False) == ("set up by the default", [])
    path, wrong = a23.lean_path(skipped_rows(), shared=False)
    assert path == "set up by the default"
    assert wrong == [f"no `{a23.LEAN_NOTE} …` on a VM not shared yet: {skipped_rows()}"]


@pytest.mark.parametrize(
    ("fails", "failure"),
    [
        ("lean-project --check", "lean check: "),
        ("--install", "lean shared: "),
        ("scp", "scp lean-project: "),
    ],
)
def test_a_failed_step_is_wrong_on_either_path(fails, failure):
    """Each failure the step records (`_fail`) is one of `LEAN_FAILED`: a check that failed
    after the step moved the packages rolls them back, and is still a failure."""
    rows = lean_rows(fails=fails)
    assert any(failure in ln for ln in rows), rows
    for shared in (True, False):
        _, wrong = a23.lean_path(rows, shared=shared)
        assert wrong and wrong[0].startswith("the shared Mathlib's step failed: "), wrong


class Bot:
    """A `Talk` whose `run` records each command, and fails on the ones in `fails`."""

    def __init__(self, fails: tuple[str, ...] = ()) -> None:
        self.sent: list[str] = []
        self.fails = fails

    async def run(self, command: str, *, phrase: bool = True):
        self.sent.append(command)
        if any(command.startswith(f) for f in self.fails):
            raise AssertionError(f"{command}: the bot refused")


async def a23_body(bot: Bot, *, rollout: bool, raises: Exception | None = None) -> None:
    """A23's clean-up nesting, as the scenario has it: the waker's outside, idle stop's in."""
    made: dict[str, bool] = {}
    async with a23.waker_on(bot, a23.B):
        async with a23.idle_stop_off_after(bot, a23.B, made):
            made["rollout"] = rollout
            if raises is not None:
                raise raises


def test_idle_stop_goes_off_before_the_waker_whatever_happens():
    """ORGANIZERS §5.2: idle stop off, then the waker; idle stop's only after the rollout was
    approved; after a failure the body's error is the one reported, and a failed clean-up after
    a passed body is a failure."""
    bot = Bot()
    asyncio.run(a23_body(bot, rollout=True))
    assert bot.sent == ["!idle-stop 0 zab-test-b", "!waker off zab-test-b"]
    bot = Bot()
    asyncio.run(a23_body(bot, rollout=False))
    assert bot.sent == ["!waker off zab-test-b"]
    for fails in ((), ("!idle-stop",), ("!idle-stop", "!waker")):
        bot = Bot(fails)
        with pytest.raises(TimeoutError, match="no result"):
            asyncio.run(a23_body(bot, rollout=True, raises=TimeoutError("no result")))
        assert bot.sent == ["!idle-stop 0 zab-test-b", "!waker off zab-test-b"]
    with pytest.raises(AssertionError, match="!idle-stop 0 zab-test-b: the bot refused"):
        asyncio.run(a23_body(Bot(("!idle-stop",)), rollout=True))


def test_a23_is_the_waker_stages_as_a8_and_a20_are():
    """ADMIN §8.1: a scenario is listed under the stage it needs; A23 needs `!waker on`."""
    from e2e.admin.harness import rig as rigs

    assert rigs.SCENARIOS[23][2] == rigs.SCENARIOS[20][2] == rigs.SCENARIOS[8][2] == 4
    assert 23 not in rigs.GO


def test_a23_needs_the_launch_and_the_bots_and_establishes_nothing():
    fn = a23.test_a23_a_rollout_without_the_words_applies_the_defaults
    assert fn.requires == ("launched", "provisioned")
    source = inspect.getsource(fn)
    assert "DONE[" not in source
    assert source.index("waker_on(t, B)") < source.index("idle_stop_off_after(t, B, made)")
    assert source.index('made["rollout"] = True') < source.index("t.approve(plan)")
