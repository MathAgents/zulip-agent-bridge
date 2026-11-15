"""`zab.admin.inflight` (docs/ADMIN.md §2.4 step 6; #989): the plans in flight that a deploy's
restart would reach, read from a real `state.db` that a `PlanStore` holds open as the running
bot does, in each state; the wait, the refusal, `--report`, a bot that doesn't run, a state.db
that can't be read, and what may be shown."""

import fcntl
import io
import os
from pathlib import Path

import pytest

from zab.admin import inflight, plans
from zab.admin.plans import PlanStore

from .conftest import CONFIG, make, posted
from .flights import approve, each_state, month_end_run, server_plan


class Waits:
    """`check`'s clocks and sleep: each sleep moves time on by `step` seconds and runs `then`."""

    def __init__(self, clock, step: float = 2.0, then=None) -> None:
        self.clock, self.step, self.then, self.slept = clock, step, then, 0
        self.mono = 0.0

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.slept += 1
        self.mono += self.step
        self.clock.now += self.step
        if self.then is not None:
            self.then(self.slept)


def where(store: PlanStore) -> Path:
    """The state directory of `store`'s state.db."""
    return Path(store.db.execute("PRAGMA database_list").fetchone()[2]).parent


def run(store, *, wait=0.0, report=False, waits=None, state_dir=None):
    err = io.StringIO()
    waits = waits or Waits(store.clock)
    done = inflight.check(
        state_dir or where(store), wait=wait, report=report, err=err, clock=store.clock,
        monotonic=waits.monotonic, sleep=waits.sleep,
    )  # fmt: skip
    return done, err.getvalue()


@pytest.fixture
def held(store):
    """The `store` fixture's PlanStore, open as the running bot holds it."""
    return store


def test_its_states_are_the_stores():
    assert inflight.LIVE == (plans.PLANNING, plans.PENDING, plans.APPROVED, plans.APPLYING)


def test_each_state_as_a_deploy_finds_it(held):
    """Applying or approved: the deploy waits, then refuses; pending: it stays, named; being
    planned: the restart cuts it, named. Every line names the plan, its state and its bot."""
    made = each_state(held)
    held.clock.now += 180
    done, said = run(held)
    assert done.status == 1
    assert [f.name for f in done.found] == ["P-1", "P-2", "P-3", "P-4"]
    assert [f.name for f in done.found if f.cut] == ["P-1", "P-3", "P-4"]  # not the pending one
    lines = said.splitlines()
    assert lines[0] == "The plans in flight on the bots:"
    assert lines[1] == (
        "  P-1, being planned on harvard-2026's bot (!stop) for 3 min: the bots' restart "
        "interrupts it, and its requester requests it again."
    )
    assert lines[2].startswith("  P-2, pending on harvard-2026's bot (!stop) for 3 min: it stays")
    assert "approved after the deploy, it is STALE, and is requested again." in lines[2]
    assert lines[3] == (
        "  P-3, approved on harvard-2026's bot (!stop) for 3 min: the deploy refuses until it "
        "has ended."
    )
    assert lines[4].startswith("  P-4, applying on harvard-2026's bot (!stop) for 3 min")
    assert lines[5] == "Still applying or approved after the wait: P-3, P-4."
    assert held.get(made["applying"].id).state == plans.APPLYING  # read, never written


@pytest.mark.parametrize("state", ["planning", "pending"])
def test_a_plan_pending_or_being_planned_lets_the_deploy_go_on(held, state):
    if state == "planning":
        held.begin(workshop="harvard-2026", bot=901, requester=103, command="!stop alice")
    else:
        posted(held, make("!stop alice"))
    done, said = run(held)
    assert done.status == 0 and [f.state for f in done.found] == [state]
    assert "Still applying" not in said


def test_nothing_in_flight(held):
    each_state(held)
    held.recover()  # a store reopened by a new bot: the restart's own marks
    for plan in held.pending(901):
        held.move(plan.id, (plans.PENDING,), plans.EXPIRED)
    done, said = run(held)
    assert (done.status, done.found, said) == (0, [], "No plan is in flight on the bots.\n")


def test_a_pending_plan_past_its_ttl_is_not_in_flight(held):
    each_state(held)
    held.clock.now += CONFIG.plan_ttl_minutes * 60
    done, _ = run(held)
    assert "P-2" not in [f.name for f in done.found]


def test_it_waits_for_the_apply_to_end_then_lets_the_deploy_go_on(held):
    made = each_state(held)

    def ends(slept: int) -> None:
        if slept == 3:
            held.finish(made["applying"].id, True)
            held.begin_apply(made["approved"].id, made["approved"].hash)
        if slept == 5:
            held.finish(made["approved"].id, True)

    waits = Waits(held.clock, then=ends)
    done, said = run(held, wait=900, waits=waits)
    assert done.status == 0 and waits.slept == 5
    assert [f.name for f in done.found] == ["P-1", "P-2"]
    waiting = [line for line in said.splitlines() if line.startswith("Waiting for ")]
    assert waiting == [
        "Waiting for P-3, approved on harvard-2026's bot (!stop) for 0 min; P-4, applying on "
        "harvard-2026's bot (!stop) for 0 min: at most 15 min more (--plan-wait)."
    ]  # once a minute at most: the next would come after 60 s of waiting


def test_it_says_it_waits_once_a_minute(held):
    each_state(held)
    waits = Waits(held.clock, step=20.0)
    done, said = run(held, wait=300, waits=waits)
    assert done.status == 1 and waits.slept == 15  # 300 s, in steps of 20
    assert [line[:30] for line in said.splitlines() if line.startswith("Waiting")] == [
        "Waiting for P-3, approved on h"
    ] * 5  # at 0, 60, 120, 180 and 240 s


def test_report_names_them_and_never_waits(held):
    each_state(held)
    waits = Waits(held.clock)
    done, said = run(held, wait=900, report=True, waits=waits)
    assert done.status == 0 and waits.slept == 0
    assert [f.name for f in done.found if f.cut] == ["P-1", "P-3", "P-4"]
    assert "P-4, applying on harvard-2026's bot (!stop) for 0 min: the deploy interrupts it." in (
        said
    )


def test_a_month_end_run_and_a_server_plan_are_named_so(held):
    month_end_run(held)
    server_plan(held)
    done, said = run(held)
    assert done.status == 1
    assert "  P-1, applying on harvard-2026's bot (a month-end run) for 0 min" in said
    assert "  P-2, applying on the server's bot (!new-workshop) for 0 min" in said


def test_a_bot_that_doesnt_run_has_nothing_in_flight(store, tmp_path):
    """The rows a stopped bot left are its next start's to mark (`recover`): the deploy goes
    on. The probe takes the lock for an instant only: the bot opens its store afterwards."""
    each_state(store)
    store.close()
    done, said = run(store, state_dir=tmp_path)
    assert (done.status, done.found) == (0, [])
    assert said.startswith("No bot holds state.db (zab-admin-bot.service is stopped, or doesn't")
    PlanStore(tmp_path / "state.db", ttl_minutes=10, floor=0).close()  # not "open elsewhere"


def test_no_state_db_at_all(tmp_path):
    done = inflight.check(tmp_path, wait=0, report=False, err=io.StringIO())
    assert (done.status, done.found) == (0, [])


def test_a_state_db_that_cant_be_read_says_so_even_in_a_report(tmp_path):
    """Unknown, never "none": with `--report` too it exits 1, which `deploy.sh
    --interrupt-plans` records as `unknown` (review of #1034, B1)."""
    (tmp_path / "state.db").write_bytes(b"not a database\x1b[31m" * 100)
    fd = os.open(tmp_path / "state.db.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # a bot holds it
    try:
        for report in (False, True):
            err = io.StringIO()
            done = inflight.check(tmp_path, wait=60, report=report, err=err, sleep=pytest.fail)
            assert (done.status, done.found) == (1, [])
            said = err.getvalue()
            assert said.startswith("state.db can't be read (DatabaseError: file is not a database")
            assert said.isascii() and "\x1b" not in said
    finally:
        os.close(fd)


def test_a_lock_that_cant_be_opened_says_so_even_in_a_report(tmp_path):
    """`state.db.lock` as a link (never followed) or unreadable: whether a bot runs is unknown,
    with `--report` too."""
    (tmp_path / "elsewhere").write_text("")
    (tmp_path / "state.db.lock").symlink_to(tmp_path / "elsewhere")
    for report in (False, True):
        err = io.StringIO()
        done = inflight.check(tmp_path, wait=60, report=report, err=err, sleep=pytest.fail)
        assert (done.status, done.found) == (1, [])
        assert err.getvalue().startswith("state.db.lock can't be read (OSError: ")


#: A value no plan row holds, where `flights` reads a number.
OUT_OF_SHAPE = {
    "planning": "UPDATE plans SET created = 'x' WHERE id = ?",
    "applying": "UPDATE plans SET approved = 'x' WHERE id = ?",
}


@pytest.mark.parametrize("state", sorted(OUT_OF_SHAPE))
def test_a_row_out_of_shape_is_one_line_not_a_traceback(held, state):
    """Review of #1034, N3: a row out of shape is a check that can't read the plans, said in one
    line, with `--report` too."""
    made = each_state(held)
    with held.db:
        held.db.execute(OUT_OF_SHAPE[state], (made[state].id,))
    for report in (False, True):
        done, said = run(held, report=report)
        assert (done.status, done.found) == (1, [])
        assert said.startswith("state.db can't be read (ValueError: ") and said.count("\n") == 1


def test_it_never_writes_state_db(held, tmp_path):
    each_state(held)
    before = (tmp_path / "state.db").read_bytes()
    run(held)
    run(held, report=True)
    assert (tmp_path / "state.db").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.db", "state.db.lock"]


def test_only_checked_words_are_shown(held):
    """A row's workshop and verb are shown only as a workshop name and a verb can be: anything
    else is `?`, so that nothing the store holds reaches the deployer's terminal as is."""
    plan = held.begin(workshop="harvard-2026", bot=901, requester=103, command="!stop alice")
    with held.db:
        held.db.execute(
            "UPDATE plans SET workshop = ?, command = ? WHERE id = ?",
            ("bad\x1b]0;x\x07", "!st\x1bop alice", plan.id),
        )
    done, said = run(held)
    assert (done.found[0].workshop, done.found[0].verb) == ("?", "?")
    assert "P-1, being planned on ?'s bot (!?) for 0 min" in said and said.isascii()


def test_the_in_band_upgrades_notes(held):
    """`!upgrade-server`'s plan: one note per kind, each plan with its bot and verb; none when
    nothing is in flight (`planning.Planning._flying` shows them)."""
    assert inflight.notes([]) == []
    each_state(held)
    server_plan(held)
    applying, pending, planning = inflight.notes(inflight.flights(held.db, held.clock()))
    assert applying == (
        "**Applying now:** P-3 on harvard-2026's bot (`!stop`), P-4 on harvard-2026's bot "
        "(`!stop`), P-5 on the server's bot (`!new-workshop`). This plan's approval is answered "
        "`busy:` until each has ended: the bots' restart, after this plan's result, would "
        "interrupt it."
    )
    assert pending.startswith("**Pending now:** P-2 on harvard-2026's bot (`!stop`). Each stays")
    assert planning.startswith("**Being planned now:** P-1 on harvard-2026's bot (`!stop`). The")


def test_main_prints_the_ids_the_restart_cuts(held, tmp_path, capsys):
    made = each_state(held)
    held.finish(made["applying"].id, True)
    held.begin_apply(made["approved"].id, made["approved"].hash)
    held.finish(made["approved"].id, True)
    approve(held, made["pending"])  # approved now: in flight, and cut
    assert inflight.main(["--state-dir", str(tmp_path), "--report"]) == 0
    out, err = capsys.readouterr()
    assert out == "P-1 P-2\n" and "P-2, approved on harvard-2026's bot" in err
    assert inflight.main(["--state-dir", str(tmp_path)]) == 1
    with pytest.raises(SystemExit) as caught:
        inflight.main(["--state-dir", str(tmp_path), "--wait", "-1"])
    assert caught.value.code == 2
