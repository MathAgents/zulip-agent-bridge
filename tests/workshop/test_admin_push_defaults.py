"""`push_config.py`'s defaults for a rollout that names neither `--lean-shared` nor
`--idle-stop` (#704): the shared Mathlib where a VM's isn't shared yet, and idle stop where the
waker is on and no timer is set, unless it was turned off on purpose."""

from __future__ import annotations

import subprocess

import admin_common as ac
import push_config as pc
import pytest

from .test_admin_push import (
    ALICE,
    ALICE_ROW,
    BOB,
    KH,
    STAGING,
    FakeRunner,
    Uploads,
    _release_is_this_checkout,
    out_dir,
    owner_stdout,
    roster,
)

__all__ = ["_release_is_this_checkout", "out_dir", "roster"]  # the fixtures, used by name

DEFAULT = 60
#: The VM's instance ID, as its probe reads it, and another VM's
VM, OTHER = "i-0aaaaaaaaaaaaaaaa", "i-0bbbbbbbbbbbbbbbb"


def opts(out_dir, **kw) -> pc.Options:
    base = {"tag": "v0.2.16", "out_dir": out_dir, "known_hosts": KH}
    return pc.Options(**(base | kw))


def idle(out_dir, *, woken=None) -> pc.Options:
    return opts(out_dir, idle_stop_default=DEFAULT, has_waker={"alice": VM} if woken is None
                else woken)  # fmt: skip


def probe(state: str, vm: str = VM) -> dict[str, str]:
    """A FakeRunner's answer to the probe: the state, then the VM's instance."""
    return {pc.IDLE_STOP_PROBE_CMD: f"{state}\n{pc.INSTANCE_MARK}{vm}\n"}


def idle_notes(result) -> list[str]:
    return [n for n in result.notes if n.startswith("idle stop")]


# ------------------------------------------------------------------------- idle stop (1, 2)


@pytest.mark.parametrize("woken", [{"alice": VM}, {"alice": ""}], ids=["its-vm", "hand-run"])
def test_idle_stop_is_turned_on_where_no_timer_is_set(out_dir, woken):
    """Never set: the probe, then `--idle-stop 60`'s step, last. A hand run's `--has-waker
    alice` names no instance, and any VM is the row's."""
    runner = Uploads(answers=probe(pc.IDLE_STOP_UNSET))
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir, woken=woken), runner)
    assert result.ok and idle_notes(result) == [f"idle stop: after {DEFAULT} min"]
    service = "zab-idle-stop.service"
    assert runner.sent[service] == pc.idle_stop_units(DEFAULT)[service]  # --minutes 60
    remotes = runner.remotes(ALICE)
    assert remotes[-3:] == [
        pc.IDLE_STOP_PROBE_CMD,
        pc.idle_stop_on_cmd(STAGING),
        f"rm -rf -- {STAGING}",
    ]
    assert remotes.index(pc.warm_on_cmd(STAGING)) < remotes.index(pc.IDLE_STOP_PROBE_CMD)


def test_a_timer_installed_but_not_enabled_is_turned_on_again(out_dir):
    """Review of #705: `!status` reads such a VM's idle stop as off; the default turns it on."""
    runner = FakeRunner(answers=probe(pc.IDLE_STOP_DISABLED))
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir), runner)
    assert result.ok and idle_notes(result) == [
        "idle stop: its timer was installed but not enabled: turned on again",
        f"idle stop: after {DEFAULT} min",
    ]
    assert runner.remotes(ALICE)[-2] == pc.idle_stop_on_cmd(STAGING)


@pytest.mark.parametrize(
    ("answer", "note"),
    [
        (pc.IDLE_STOP_ON, None),  # on already, whatever its minutes: quietly left as it is
        (pc.IDLE_STOP_OFF, "idle stop: left off, as it was turned off on purpose: "
         f"!idle-stop {DEFAULT} alice turns it on"),
    ],
)  # fmt: skip
def test_a_timer_on_or_idle_stop_off_on_purpose_is_left_as_it_is(out_dir, answer, note):
    runner = FakeRunner(answers=probe(answer))
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir), runner)
    assert result.ok, result.failures
    remotes = runner.remotes(ALICE)
    assert remotes[-2:] == [pc.IDLE_STOP_PROBE_CMD, f"rm -rf -- {STAGING}"]
    assert not any("idle_stop" in c[-1] for c in runner.calls if c[0] == "scp")
    assert idle_notes(result) == ([note] if note else [])


WITHOUT = "this row's waker isn't on"
ELSEWHERE = f"the waker names {OTHER}, not this VM ({VM})"


@pytest.mark.parametrize(
    ("woken", "answer", "note"),
    [
        ({}, pc.IDLE_STOP_UNSET, f"left as it is, since {WITHOUT}: !waker on alice, then "
         f"!idle-stop {DEFAULT} alice"),
        ({}, pc.IDLE_STOP_DISABLED, f"left as it is, since {WITHOUT}: !waker on alice, then "
         f"!idle-stop {DEFAULT} alice"),
        # the one state ORGANIZERS §4.2 forbids: said louder (review of #705)
        ({}, pc.IDLE_STOP_ON, f"WARNING: it is on, but {WITHOUT}: !waker on alice, or "
         "!idle-stop 0 alice"),
        # off on purpose, and no waker: never `!idle-stop 60` alone (review round 2)
        ({}, pc.IDLE_STOP_OFF, f"left off, as it was turned off on purpose, and {WITHOUT}: "
         f"!waker on alice, then !idle-stop {DEFAULT} alice"),
        ({"alice": OTHER}, pc.IDLE_STOP_OFF, "left off, as it was turned off on purpose, and "
         f"{ELSEWHERE}: !waker on alice, then !idle-stop {DEFAULT} alice"),
        # the waker names another VM, a broken one replaced by hand: as without the waker
        ({"alice": OTHER}, pc.IDLE_STOP_UNSET, f"left as it is, since {ELSEWHERE}: !waker on "
         f"alice, then !idle-stop {DEFAULT} alice"),
        ({"alice": OTHER}, pc.IDLE_STOP_ON, f"WARNING: it is on, but {ELSEWHERE}: !waker on "
         "alice, or !idle-stop 0 alice"),
    ],
)  # fmt: skip
def test_without_the_waker_for_this_vm_idle_stop_is_left_as_it_is(out_dir, woken, answer, note):
    """ORGANIZERS §4.2: the waker first, then idle stop. The probe, and no change; the note
    says what it found, and the commands."""
    runner = FakeRunner(answers=probe(answer))
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir, woken=woken), runner)
    assert result.ok and idle_notes(result) == [f"idle stop: {note}"]
    assert runner.remotes(ALICE)[-2] == pc.IDLE_STOP_PROBE_CMD
    assert not any("idle_stop" in c[-1] for c in runner.calls if c[0] == "scp")


@pytest.mark.parametrize("vm", ["", "iid-datasource-none", "i-0aaaaaaaaaaaaaaaa\x1b[31m"])
def test_a_vm_whose_instance_cant_be_read_is_not_the_wakers(out_dir, vm):
    """Nothing, or anything but an instance ID, is `unknown`: the VM's text stays out of the
    note (review round 2)."""
    runner = FakeRunner(answers=probe(pc.IDLE_STOP_UNSET, vm=vm))
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir), runner)
    assert result.ok and idle_notes(result) == [
        f"idle stop: left as it is, since the waker names {VM}, not this VM (unknown): "
        f"!waker on alice, then !idle-stop {DEFAULT} alice"
    ]


@pytest.mark.parametrize("fail", [True, False])
def test_a_probe_that_fails_or_says_nothing_known_fails_the_row(out_dir, fail):
    runner = (
        FakeRunner(fail={pc.IDLE_STOP_PROBE_CMD: "sudo: broken"})
        if fail
        else FakeRunner(answers={pc.IDLE_STOP_PROBE_CMD: "something else\n"})
    )
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir), runner)
    assert not result.ok and result.installed
    assert result.failures == ["idle-stop probe: " + ("sudo: broken" if fail else "something else")]
    assert pc.idle_stop_on_cmd(STAGING) not in runner.remotes(ALICE)


def test_an_explicit_idle_stop_wins_over_the_default(out_dir):
    """`idle-stop=N` keeps today's meaning: no probe, on whatever was set, 0 included."""
    for minutes, last in ((30, "idle stop: after 30 min"), (0, "idle stop: off")):
        runner = FakeRunner()
        o = opts(out_dir, idle_stop=minutes, idle_stop_default=DEFAULT, has_waker={"alice": VM})
        result = pc.rollout_vm(ALICE_ROW, o, runner)
        assert result.ok and result.notes[-1] == last
        assert pc.IDLE_STOP_PROBE_CMD not in runner.remotes(ALICE)


def test_turning_idle_stop_off_records_it_and_turning_it_on_clears_it():
    """`--idle-stop 0` (`!idle-stop 0`, `idle-stop=0`, `!retire`'s step) writes the record last,
    once the timer is gone; `--idle-stop N` removes it as soon as its timer runs."""
    assert pc.IDLE_STOP_OFF_CMD.endswith(
        " && sudo systemctl daemon-reload && sudo mkdir -p /etc/zab && "
        "sudo touch /etc/zab/idle-stop.off"
    )
    on = pc.idle_stop_on_cmd(STAGING).split(" && ")
    assert on[on.index("systemctl is-active zab-idle-stop.timer") + 1] == (
        "sudo rm -f /etc/zab/idle-stop.off"
    )


@pytest.mark.parametrize(
    ("timer", "enabled", "record", "log", "state", "service"),
    [
        (False, False, False, False, pc.IDLE_STOP_UNSET, None),  # never set
        (True, True, False, True, pc.IDLE_STOP_ON, False),  # on
        (True, True, True, True, pc.IDLE_STOP_ON, False),  # on again by hand: the timer wins
        (False, False, True, True, pc.IDLE_STOP_OFF, None),  # `--idle-stop 0`
        (False, False, False, True, pc.IDLE_STOP_OFF, None),  # off before the record existed
        (False, False, True, False, pc.IDLE_STOP_OFF, None),
        (True, False, False, True, pc.IDLE_STOP_DISABLED, False),  # disabled by hand
        (True, False, True, True, pc.IDLE_STOP_OFF, False),  # and the record: off on purpose
        # a spare's (#715), first of all, enabled or not: never the row's
        (True, True, False, False, pc.IDLE_STOP_SPARE, True),
        (True, False, False, True, pc.IDLE_STOP_SPARE, True),
    ],
)
def test_the_probe_reads_the_vm(tmp_path, timer, enabled, record, log, state, service):
    """The probe's own shell, its paths moved under `tmp_path`, `systemctl is-enabled` as a
    test of a file of its own, the instance ID cloud-init's file; the service, a spare's
    (`service` true), a row's, or none."""
    cmd = pc.IDLE_STOP_PROBE_CMD.replace(
        "systemctl is-enabled --quiet zab-idle-stop.timer", f"test -e {tmp_path / 'enabled'}"
    )
    units = pc.idle_stop_units(10, spare=bool(service))
    paths = {
        "/etc/systemd/system/zab-idle-stop.service": service is not None,
        "/etc/systemd/system/zab-idle-stop.timer": timer,
        pc.IDLE_STOP_OFF_RECORD: record,
        pc.IDLE_STOP_LOG: log,
        "enabled": enabled,
        pc.INSTANCE_ID_FILE: True,
    }
    for n, (path, present) in enumerate(paths.items()):
        if path != "enabled":
            assert path in cmd
            cmd = cmd.replace(path, str(tmp_path / str(n)))
        target = tmp_path / ("enabled" if path == "enabled" else str(n))
        if present:
            target.write_text(
                VM + "\n" if path == pc.INSTANCE_ID_FILE
                else units["zab-idle-stop.service"] if path.endswith(".service") else ""
            )  # fmt: skip
    done = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)  # noqa: S603, S607
    assert done.stdout == f"{state}\n{pc.INSTANCE_MARK}{VM}\n"


# --------------------------------------------------------------------- shared Mathlib (3)


def lean_runner(answer: str, **kw) -> FakeRunner:
    """The instructions step, which carries the probe, answers `answer`."""
    return FakeRunner(answers={pc.LEAN_PROJECT_PROBE: answer}, **kw)


@pytest.mark.parametrize(
    "answer", [pc.LEAN_NOT_SHARED + "\n", pc.LEAN_PROJECT_MISSING + "\n"], ids=["dir", "no-tool"]
)
def test_the_shared_mathlib_is_set_up_where_it_isnt_yet(out_dir, answer):
    """Not shared yet: `--lean-shared`'s step, with its check and its fail-safe rollback."""
    runner = lean_runner(answer)
    result = pc.rollout_vm(ALICE_ROW, opts(out_dir, lean_shared_default=True), runner)
    assert result.ok and "lean: shared Mathlib" in result.notes
    remotes = runner.remotes(ALICE)
    assert pc.lean_shared_on_cmd(STAGING) in remotes and pc.lean_check_cmd() in remotes
    assert any(c[0] == "scp" and c[-1].endswith("/lean-project") for c in runner.calls)


def test_the_default_rolls_back_as_lean_shared_does(out_dir):
    moved = f"lean-project: moved {pc.LEAN_PACKAGES} to /opt/lean/packages/v4.34.1\n"
    runner = FakeRunner(
        answers={pc.LEAN_PROJECT_PROBE: pc.LEAN_NOT_SHARED, pc.lean_shared_on_cmd(STAGING): moved},
        fail={pc.lean_check_cmd(): "too slow"},
    )
    result = pc.rollout_vm(ALICE_ROW, opts(out_dir, lean_shared_default=True), runner)
    assert not result.ok and result.failures == ["lean check: too slow"]
    assert pc.LEAN_SHARED_UNDO_CMD in runner.remotes(ALICE)
    assert pc.LEAN_ROLLED_BACK_WARNING in result.notes


def test_a_vm_whose_mathlib_is_shared_is_only_probed(out_dir):
    """Shared, lean-project there: the probe (in the instructions step) and nothing else; no
    timing build, no note."""
    runner = lean_runner("")
    result = pc.rollout_vm(ALICE_ROW, opts(out_dir, lean_shared_default=True), runner)
    assert result.ok and not [n for n in result.notes if n.startswith("lean")]
    remotes = runner.remotes(ALICE)
    assert pc.lean_check_cmd() not in remotes and pc.lean_shared_on_cmd(STAGING) not in remotes
    assert remotes[-3].endswith(pc.LEAN_PROJECT_PROBE)  # then the warm-up, then staging's end


def test_a_vm_with_no_lean_workspace_is_noted_never_failed(out_dir):
    runner = lean_runner(f"{pc.LEAN_PROJECT_MISSING}\n{pc.LEAN_NO_WORKSPACE}\n")
    result = pc.rollout_vm(ALICE_ROW, opts(out_dir, lean_shared_default=True), runner)
    assert result.ok and result.notes == [
        "lean: no Lean workspace (/home/agent/lean/workspace/.lake/packages): no shared Mathlib "
        "to set up"
    ]
    assert pc.lean_check_cmd() not in runner.remotes(ALICE)


def test_a_failed_instructions_step_leaves_the_mathlib_unprobed(out_dir):
    runner = FakeRunner(fail={f"/usr/bin/python3 {STAGING}/agent_instructions": "markers"})
    result = pc.rollout_vm(ALICE_ROW, opts(out_dir, lean_shared_default=True), runner)
    assert result.failures == ["agent instructions: markers"]
    assert pc.LEAN_NOT_PROBED in result.notes and pc.lean_check_cmd() not in runner.remotes(ALICE)


def test_an_explicit_lean_shared_keeps_its_timing_and_needs_no_probe(out_dir):
    runner = lean_runner("")
    result = pc.rollout_vm(ALICE_ROW, opts(out_dir, lean_shared=True), runner)
    assert result.ok and pc.lean_check_cmd() in runner.remotes(ALICE)
    assert not any(pc.LEAN_PROJECT_PROBE in r for r in runner.remotes(ALICE))


@pytest.mark.parametrize(
    ("link", "directory", "tool", "said"),
    [
        (True, False, True, ""),  # shared
        (False, True, True, pc.LEAN_NOT_SHARED),  # the image's own copy
        (True, False, False, pc.LEAN_PROJECT_MISSING),  # shared, lean-project gone
        (False, False, False, f"{pc.LEAN_PROJECT_MISSING}\n{pc.LEAN_NO_WORKSPACE}"),  # no Lean
    ],
)
def test_the_lean_probe_reads_the_vm(tmp_path, link, directory, tool, said):
    """The probe's own shell, its paths moved under `tmp_path`, `sudo test` as `test`."""
    packages, tool_path, shared = tmp_path / "packages", tmp_path / "tool", tmp_path / "shared"
    shared.mkdir()
    if link:
        packages.symlink_to(shared)
    if directory:
        packages.mkdir()
    if tool:
        tool_path.write_text("")
        tool_path.chmod(0o755)
    cmd = pc.LEAN_PROJECT_PROBE.replace("sudo test", "test")
    cmd = cmd.replace(pc.LEAN_PACKAGES, str(packages)).replace(pc.LEAN_PROJECT_PATH, str(tool_path))
    done = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)  # noqa: S603, S607
    assert done.stdout == (said + "\n" if said else "")


# ------------------------------------------------------------------------- the CLI (5, 7)


def test_a_dry_run_says_the_defaults_and_names_their_steps(roster, out_dir, monkeypatch, capsys):
    """The lines under multi-party and steering say the defaults; each VM's line names every
    step they may take, as on a new VM: the shared Mathlib, and idle stop where the waker is,
    the dry run answering as the VM the waker names."""
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    args = [
        *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4", "--dry-run"),
        *("--lean-shared-default", "--idle-stop-default", "60", "--has-waker", f"bob={OTHER}"),
    ]
    assert pc.main(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == pc.mention_after_line(120)  # beside the switches (#711)
    assert out[3:5] == [
        "shared Mathlib: by default, where a VM's isn't shared yet (only probed where it is)",
        "idle stop: by default 60 min, where the waker is on (1 of 2 VMs) and no timer is on, "
        "unless turned off on purpose",
    ]
    dry = {line.split(" (")[0]: line for line in out if line.startswith("dry-run ")}
    lean = "agent instructions; scp lean-project; lean-project + shared Mathlib; lean check; "
    assert lean in dry["dry-run alice"] and lean in dry["dry-run bob"]
    assert dry["dry-run bob"].endswith(
        "warm-up on; idle-stop probe; scp idle_stop.py zab-idle-stop.service zab-idle-stop.timer "
        "status_read.sh status_read_login zab-monitor.sudoers; idle stop installed; staging "
        "removed; hold released"
    )
    assert dry["dry-run alice"].endswith(  # probed, and left as it is (review of #705)
        "warm-up on; idle-stop probe; staging removed; hold released"
    )
    table = next(line for line in out if line.startswith("alice "))
    assert "note: idle stop: left as it is, since this row's waker isn't on" in table


def test_has_waker_all_is_every_selected_row(roster, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    args = [
        *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4", "--dry-run"),
        *("--idle-stop-default", "45", "--has-waker", "all", "--only", "bob"),
    ]
    assert pc.main(args) == 0
    out = capsys.readouterr().out
    assert "by default 45 min, where the waker is on (1 of 1 VMs)" in out
    assert f"dry-run bob ({BOB})" in out and "idle stop installed" in out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--lean-shared-default"], "the defaults (--lean-shared-default, --idle-stop-default)"),
        (["--idle-stop-default", "60"], "need a release"),
        (["--tag", "v1.0.0", "--uninstall", "--idle-stop-default", "60"], "need a release"),
        (["--tag", "v1.0.0", "--lean-shared", "--lean-shared-default"], "not both"),
        (["--tag", "v1.0.0", "--idle-stop", "0", "--idle-stop-default", "60"], "not both"),
        (["--tag", "v1.0.0", "--idle-stop-default", "5"], "at least 10 minutes"),
        (["--tag", "v1.0.0", "--idle-stop-default", "0"], "at least 10 minutes"),
        (["--tag", "v1.0.0", "--has-waker", "alice"], "only says where --idle-stop-default"),
        (["--tag", "v1.0.0", "--idle-stop-default", "60", "--has-waker", "alice=i-1"], "not "),
        (["--tag", "v1.0.0", "--idle-stop-default", "60", "--has-waker", "alice=x y"], "not "),
        # the text after the first `=`, whole (review round 2)
        (["--tag", "v1.0.0", "--idle-stop-default", "60", "--has-waker", f"alice={VM}=x"], "not "),
        (
            [
                "--tag",
                "v1.0.0",
                "--idle-stop-default",
                "60",
                "--has-waker",
                "all",
                "--has-waker",
                f"alice={VM}",
            ],
            "give it alone",
        ),
        # a slug given twice: the bare one would drop the instance check (review round 3)
        (
            [
                *("--tag", "v1.0.0", "--idle-stop-default", "60"),
                *("--has-waker", f"alice={VM}", "--has-waker", "alice"),
            ],
            "--has-waker names alice twice",
        ),
        (
            [
                *("--tag", "v1.0.0", "--idle-stop-default", "60"),
                *("--has-waker", f"alice={VM}", "--has-waker", f"ALICE={OTHER}"),
            ],
            "--has-waker names alice twice",
        ),
    ],
)
def test_the_default_flags_are_checked(roster, extra, message, capsys):
    with pytest.raises(SystemExit):
        pc.main(["--roster", str(roster), *extra])
    assert message in capsys.readouterr().err


def test_has_waker_names_a_row_of_the_roster(roster, out_dir, capsys):
    args = ["--roster", str(roster), "--tag", "v1.0.0", "--idle-stop-default", "60"]
    assert pc.main([*args, "--has-waker", "carol"]) == 1
    assert "roster: not in the roster: carol" in capsys.readouterr().err


def test_the_probe_is_a_fixed_command(out_dir):
    """Both probes are literals of this script: no roster text reaches them."""
    for cmd in (pc.IDLE_STOP_PROBE_CMD, pc.LEAN_PROJECT_PROBE, pc.SPARE_TIMER_OFF_CMD):
        assert "alice" not in cmd and ALICE not in cmd
    first = "if grep -qsF -- ' --spare' /etc/systemd/system/zab-idle-stop.service; then "
    assert ac.first_line(pc.IDLE_STOP_PROBE_CMD).startswith(first)
    assert pc.SPARE_TIMER_OFF_CMD.startswith(first)


# ------------------------------------------------------------- a spare's idle stop (#715)


def test_a_spares_units_run_idle_stop_as_a_spares():
    """`spare_idle_stop.py`'s units: `--spare` last, which the probe and the removal look for;
    the timer is the same."""
    spare, row = pc.idle_stop_units(10, spare=True), pc.idle_stop_units(10)
    assert spare["zab-idle-stop.timer"] == row["zab-idle-stop.timer"]
    run = "ExecStart=/usr/bin/python3 /usr/local/lib/zab/idle_stop.py --minutes 10"
    assert f"{run} --spare\n" in spare["zab-idle-stop.service"]
    assert f"{run}\n" in row["zab-idle-stop.service"] and "--spare" not in str(row)


def test_a_spares_timer_is_replaced_by_the_rows_where_the_waker_is_on(out_dir):
    """#715: a hand-over's default replaces the spare's 10 minutes with the row's 60."""
    runner = Uploads(answers=probe(pc.IDLE_STOP_SPARE))
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir), runner)
    assert result.ok and idle_notes(result) == [
        "idle stop: the spare's timer replaced by the row's",
        f"idle stop: after {DEFAULT} min",
    ]
    service = "zab-idle-stop.service"
    assert runner.sent[service] == pc.idle_stop_units(DEFAULT)[service]  # no --spare
    assert runner.remotes(ALICE)[-2] == pc.idle_stop_on_cmd(STAGING)


@pytest.mark.parametrize("woken", [{}, {"alice": OTHER}], ids=["no-waker", "another-vm"])
def test_a_spares_timer_goes_where_the_rows_waker_isnt_on(out_dir, woken):
    """#715: never the spare's minutes on a row that nothing wakes; no record of off on purpose,
    so that `!waker on`, then `!idle-stop 60` (or a later rollout's default) turns the row's on."""
    gone = {pc.SPARE_TIMER_OFF_CMD: f"{pc.SPARE_TIMER_GONE}\n"}
    runner = FakeRunner(answers={**probe(pc.IDLE_STOP_SPARE), **gone})
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir, woken=woken), runner)
    why = WITHOUT if not woken else ELSEWHERE
    assert result.ok and idle_notes(result) == [
        f"idle stop: the spare's timer removed, since {why}: !waker on alice, then "
        f"!idle-stop {DEFAULT} alice"
    ]
    assert runner.remotes(ALICE)[-3:-1] == [pc.IDLE_STOP_PROBE_CMD, pc.SPARE_TIMER_OFF_CMD]
    assert pc.IDLE_STOP_OFF_CMD not in runner.remotes(ALICE)
    assert not any("idle_stop" in c[-1] for c in runner.calls if c[0] == "scp")
    failed = FakeRunner(answers=probe(pc.IDLE_STOP_SPARE), fail={"ZAB-SPARE-TIMER": "boom"})
    result = pc.rollout_vm(ALICE_ROW, idle(out_dir, woken=woken), failed)
    assert result.failures == ["spare's idle stop removal: boom"]


def test_a_hand_over_without_idle_stop_flags_removes_a_spares_timer(out_dir):
    """#715: `--adopt-spare` with neither `--idle-stop` nor its default (a deployer's run by
    hand): the spare's minutes don't outlive the hand-over; a plain rollout leaves the timer."""
    gone = {pc.SPARE_TIMER_OFF_CMD: f"{pc.SPARE_TIMER_GONE}\n"}
    spare = {ALICE: owner_stdout("", spare="zab-spare-1")}
    runner = FakeRunner(owners=spare, answers=gone)
    row = {**ALICE_ROW, "email": "alice@example.org"}
    adopt = pc.Options(out_dir=out_dir, tag="v0.2.16", adopt_spares=frozenset({"alice"}))
    result = pc.rollout_vm(row, adopt, runner)
    assert result.ok and result.adopted == "zab-spare-1", result.failures
    assert runner.remotes(ALICE)[-2] == pc.SPARE_TIMER_OFF_CMD
    assert idle_notes(result) == ["idle stop: the spare's timer removed"]
    runner = FakeRunner()  # a VM that is the row's already: the same run says nothing
    result = pc.rollout_vm(row, adopt, runner)
    assert result.ok and not idle_notes(result) and not result.adopted
    assert runner.remotes(ALICE)[-2] == pc.SPARE_TIMER_OFF_CMD
    runner = FakeRunner()
    assert pc.rollout_vm(row, opts(out_dir), runner).ok
    assert pc.SPARE_TIMER_OFF_CMD not in runner.remotes(ALICE)


def test_a_spares_idle_stop_is_installed_only_on_the_spare_itself():
    """`install_spare_idle_stop` (#715): the owner check, which must find that very spare, then
    the script and a spare's units, staged, installed as root, the staging removed; no read
    user. Nothing is sent to a VM that isn't the spare any more."""
    spare = {ALICE: owner_stdout("", spare="zab-spare-1")}
    runner = FakeRunner(owners=spare)
    assert pc.install_spare_idle_stop(ALICE, "zab-spare-1", 10, runner, known_hosts=KH) == ""
    assert runner.remotes(ALICE) == [
        pc.OWNER_CMD,
        pc.MKTEMP_CMD,
        pc.spare_idle_stop_on_cmd(STAGING),
        f"rm -rf -- {STAGING}",
    ]
    sent = [c[-1].rsplit("/", 1)[1] for c in runner.calls if c[0] == "scp"]
    assert sent == ["idle_stop.py", "zab-idle-stop.service", "zab-idle-stop.timer"]
    on = pc.spare_idle_stop_on_cmd(STAGING)
    assert "--setup-read-user" not in on and "visudo" not in on and "idle-stop.off" not in on
    assert on.endswith("sudo systemctl enable --now zab-idle-stop.timer && "
                       "systemctl is-active zab-idle-stop.timer")  # fmt: skip
    for owner, said in (
        (owner_stdout("", spare="zab-spare-2"), "the VM says it is 'zab-spare-2'"),
        (owner_stdout("alice"), "the VM says it is 'alice' (hostname 'ws-alice')"),
    ):
        runner = FakeRunner(owners={ALICE: owner})
        why = pc.install_spare_idle_stop(ALICE, "zab-spare-1", 10, runner, known_hosts=KH)
        assert why.startswith(said) and why.endswith("not the spare zab-spare-1: nothing installed")
        assert runner.remotes(ALICE) == [pc.OWNER_CMD]
    failed = FakeRunner(owners=spare, fail={"systemctl enable": "Failed to enable"})
    why = pc.install_spare_idle_stop(ALICE, "zab-spare-1", 10, failed, known_hosts=KH)
    assert why == "idle stop: Failed to enable"
    assert failed.remotes(ALICE)[-1] == f"rm -rf -- {STAGING}"
    assert "at least 10" in pc.install_spare_idle_stop(ALICE, "zab-spare-1", 9, FakeRunner(),
                                                       known_hosts=KH)  # fmt: skip


@pytest.mark.parametrize(
    ("fail", "failed"),
    [
        # an upload, after the owner record
        ({"/config.toml": "scp: connection lost"}, "scp config.toml: scp: connection lost"),
        # before the staging even
        ({"mktemp -d": "mktemp: No space"}, "ssh: mktemp: No space"),
        # the idle-stop step itself
        ({"---ZAB-IDLE-STOP-UNSET": "sudo: broken"}, "idle-stop probe: sudo: broken"),
    ],
    ids=["upload", "staging", "idle-stop-step"],
)
def test_a_hand_over_that_fails_midway_removes_the_spares_timer(out_dir, fail, failed):
    """Review of #728: a hand-over that stops after the owner record (ADOPT_CMD) but before, or
    in, its idle-stop step leaves the row's VM with the spare's minutes, which would power it
    off: the removal runs whatever happened, and touches a ` --spare` unit only."""
    gone = {pc.SPARE_TIMER_OFF_CMD: f"{pc.SPARE_TIMER_GONE}\n"}
    spare = {ALICE: owner_stdout("", spare="zab-spare-1")}
    runner = FakeRunner(owners=spare, answers=gone, fail=fail)
    row = {**ALICE_ROW, "email": "alice@example.org"}
    adopt = opts(out_dir, adopt_spares=frozenset({"alice"}), idle_stop_default=DEFAULT,
                 has_waker={"alice": VM})  # fmt: skip
    result = pc.rollout_vm(row, adopt, runner)
    assert result.adopted == "zab-spare-1" and result.failures[0] == failed, result.failures
    remotes = runner.remotes(ALICE)
    assert any(r.startswith("sudo tee /etc/zab-participant.json") for r in remotes)
    assert pc.SPARE_TIMER_OFF_CMD in remotes
    assert "idle stop: the spare's timer removed" in idle_notes(result)


@pytest.mark.parametrize("spare", [True, False])
def test_the_spares_timer_removal_touches_only_a_spares(tmp_path, spare):
    """`SPARE_TIMER_OFF_CMD` in bash, its paths under `tmp_path` and `sudo`/`systemctl` as
    no-ops: a spare's units and script go, a row's stay, and it says which."""
    files = {
        "/etc/systemd/system/zab-idle-stop.service": pc.idle_stop_units(10, spare=spare)[
            "zab-idle-stop.service"
        ],
        "/etc/systemd/system/zab-idle-stop.timer": "[Timer]\n",
        pc.IDLE_STOP_PATH: "#!/usr/bin/env python3\n",
    }
    cmd = pc.SPARE_TIMER_OFF_CMD.replace("sudo systemctl", "true").replace("sudo ", "")
    for n, (path, text) in enumerate(files.items()):
        assert path in cmd
        cmd = cmd.replace(path, str(tmp_path / str(n)))
        (tmp_path / str(n)).write_text(text)
    done = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)  # noqa: S603, S607
    assert done.returncode == 0, done.stderr
    assert (pc.SPARE_TIMER_GONE in done.stdout) is spare
    assert [(tmp_path / str(n)).exists() for n in range(3)] == [not spare] * 3
    assert pc.dry_name(pc.SPARE_TIMER_OFF_CMD) == "spare's idle stop removal"
