"""deploy.sh's check of the plans in flight (docs/ADMIN.md §2.4 step 6; #989), in the deploy rig
(`test_adminserver_deploy`): the server's `state.db` is a real one, its plans made through
`PlanStore` and its approvals in each state a restart reaches (`admin.flights`), and held open
by this process as the running bot holds it. An upgrade waits for a plan applying or approved,
then refuses, changing nothing; it names the plans pending or being planned and goes on;
`--interrupt-plans` goes on and the deploy record keeps why; in-band, the bot's own lock
decides, and the check is left out."""

import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from admin.flights import each_state, month_end_run, server_plan
from workshop.test_adminserver_deploy import (
    DEPLOY,
    _tools_missing,
    refused,
    server_half,
    systemctl_log,
)
from workshop.test_adminserver_deploy import bootstrapped as bootstrapped  # the fixtures
from workshop.test_adminserver_deploy import index as index
from workshop.test_adminserver_deploy import rig as rig
from workshop.test_adminserver_deploy import server as server
from zab.admin.plans import PlanStore

pytestmark = [
    pytest.mark.skipif(bool(_tools_missing()), reason=_tools_missing() or "ok"),
    pytest.mark.deploy_rig,  # a pull request runs it in CI's rigs (3.12) job (#988)
]
REFUSED = (
    r"a plan is applying or approved, or state\.db can't be read \(above\): nothing was "
    r"deployed\. Deploy again once its result is posted, or, for a plan that is stuck, with "
    r"--interrupt-plans REASON"
)


def state_dir(rig) -> pathlib.Path:
    return rig.prefix / "var/lib/zab-admin"


@pytest.fixture
def bot(server):
    """The server's state.db, open as its running bot holds it."""
    store = PlanStore(state_dir(server) / "state.db", ttl_minutes=10, floor=0)
    yield store
    store.close()


def unchanged(rig, log) -> None:
    """The server as a deploy refused before the switch leaves it: v0.3.0 current, no other
    release, no record, no unit touched."""
    assert rig.current().endswith("v0.3.0") and rig.releases() == ["v0.3.0"]
    assert [r["event"] for r in rig.records()] == ["bootstrap"] and not log.exists()


@pytest.mark.parametrize("which", ["applying", "approved", "month-end run", "server plan"])
def test_a_plan_applying_or_approved_refuses_the_upgrade(server, bot, which):
    log = systemctl_log(server)
    if which == "month-end run":
        month_end_run(bot)
        named = "P-1, applying on harvard-2026's bot (a month-end run) for 0 min"
    elif which == "server plan":
        server_plan(bot)
        named = "P-1, applying on the server's bot (!new-workshop) for 0 min"
    else:
        made = each_state(bot)
        if which == "approved":
            bot.finish(made["applying"].id, True)
        named = f"{made[which].name}, {which} on harvard-2026's bot (!stop) for 0 min"
    server.release("v0.3.1")
    result = server.deploy("v0.3.1", "--plan-wait", "0")
    refused(result, REFUSED)
    assert f"  {named}: the deploy refuses until it has ended." in result.stderr
    unchanged(server, log)


def test_plans_pending_or_being_planned_are_named_and_the_upgrade_goes_on(server, bot):
    made = each_state(bot)
    bot.finish(made["applying"].id, True)
    bot.begin_apply(made["approved"].id, made["approved"].hash)
    bot.finish(made["approved"].id, False)
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert server.current().endswith("v0.3.1")
    said = result.stderr
    assert "  P-1, being planned on harvard-2026's bot (!stop) for 0 min: the bots' restart" in said
    assert "  P-2, pending on harvard-2026's bot (!stop) for 0 min: it stays pending, but " in said
    assert said.index("The plans in flight on the bots:") < said.index("current is v0.3.1")
    assert "interrupt_plans" not in server.records()[-1]


def test_with_nothing_in_flight_it_says_so(server, bot):
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert "No plan is in flight on the bots.\n" in result.stderr


def checker_runs(where: pathlib.Path) -> bool:
    """Whether `python -m zab.admin.inflight --state-dir WHERE …` runs now."""
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"zab.admin.inflight" in argv and str(where).encode() in argv:
            return True
    return False


def test_the_upgrade_waits_for_the_apply_to_end(server, bot):
    """The plans end while the check waits: the upgrade goes on, the plan's result unharmed."""
    made = each_state(bot)
    server.release("v0.3.1")
    with ThreadPoolExecutor(1) as pool:
        deploying = pool.submit(server.deploy, "v0.3.1", "--plan-wait", "1")
        for _ in range(1200):  # the build first: a few seconds, 120 s at most
            if checker_runs(state_dir(server)) or deploying.done():
                break
            time.sleep(0.1)
        time.sleep(0.5)  # well into its wait
        bot.finish(made["applying"].id, True)
        bot.begin_apply(made["approved"].id, made["approved"].hash)
        bot.finish(made["approved"].id, True)
        result = deploying.result(timeout=120)
    assert result.returncode == 0, result.stderr
    assert server.current().endswith("v0.3.1")
    assert (
        "Waiting for P-3, approved on harvard-2026's bot (!stop) for 0 min; P-4, applying on "
        "harvard-2026's bot (!stop) for 0 min: at most 1 min more (--plan-wait)."
    ) in result.stderr
    assert "Still applying" not in result.stderr
    assert bot.get(made["applying"].id).state == "done"


def test_interrupt_plans_goes_on_and_the_record_keeps_why(server, bot):
    """For a plan that is stuck: no wait; the plans it cuts and the reason are in the deploy
    record, which the bot copies into the audit log."""
    each_state(bot)
    server.release("v0.3.1")
    why = "P-4 stuck since 10:00 (its VM hangs); see #989 @ 10:05"
    result = server.deploy("v0.3.1", "--plan-wait", "5", "--interrupt-plans", why)
    assert result.returncode == 0, result.stderr
    assert "  P-4, applying on harvard-2026's bot (!stop) for 0 min: the deploy interrupts it." in (
        result.stderr
    )
    record = server.records()[-1]
    assert (record["event"], record["version"]) == ("upgrade", "v0.3.1")
    assert (record["interrupt_plans"], record["interrupted"]) == (why, "P-1 P-3 P-4")


def test_a_bot_that_doesnt_run_leaves_nothing_in_flight(server, bot):
    """Its rows are its next start's to mark: a hand deploy that brings a failed bot back is
    never held by them."""
    each_state(bot)
    bot.close()
    server.release("v0.3.1")
    result = server.deploy("v0.3.1", "--plan-wait", "0")
    assert result.returncode == 0, result.stderr
    assert "No bot holds state.db (zab-admin-bot.service is stopped" in result.stderr


def test_in_band_the_bots_lock_decides(server, bot):
    """`!upgrade-server`'s own plan applies while its apply helper runs the server half, and the
    bot holds every workshop's lock: the check is left out."""
    server_plan(bot)  # its own plan, as it applies
    server.release("v0.3.1")
    flags = ["--base", "v0.3.0", "--diff-sha256", "a" * 64, "--in-band", "P-1"]
    result = server_half(server, "v0.3.1", *flags)
    assert result.returncode == 0, result.stderr
    assert "in flight" not in result.stderr and server.current().endswith("v0.3.1")


def test_a_check_that_fails_refuses_but_for_interrupt_plans(server, bot):
    """A state.db that can't be read: refused; with --interrupt-plans, on, its record saying
    the plans it cut are unknown."""
    (state_dir(server) / "state.db").write_bytes(b"not a database" * 100)
    server.release("v0.3.1")
    refused(server.deploy("v0.3.1", "--plan-wait", "0"), REFUSED)
    result = server.deploy("v0.3.1", "--interrupt-plans", "state.db is broken")
    assert result.returncode == 0, result.stderr
    assert "state.db can't be read (DatabaseError: file is not a database)" in result.stderr
    assert "The plans check failed (above): going on, as --interrupt-plans says." in result.stderr
    record = server.records()[-1]  # never "": that would say the restart cut none (B1)
    assert (record["interrupt_plans"], record["interrupted"]) == ("state.db is broken", "unknown")


def without_the_check(repo: pathlib.Path) -> None:
    """A release from before #989: no `zab.admin.inflight`."""
    (repo / "src/zab/admin/inflight.py").unlink()


@pytest.mark.parametrize("flag", [["--plan-wait", "5"], ["--interrupt-plans", "why"]])
def test_a_release_without_the_check_refuses_its_options(server, flag):
    """Review of #1034, B2: a downgrade's target, say, runs its own server half, which has no
    check: it would refuse `--plan-wait`, and never see `--interrupt-plans`. Refused before
    anything reaches the server."""
    log = systemctl_log(server)
    server.release("v0.3.1", edit=without_the_check)
    refused(
        server.deploy("v0.3.1", *flag),
        r"v0\.3\.1 has no check of the plans in flight \(#989\): it takes neither --plan-wait "
        r"nor --interrupt-plans",
    )
    unchanged(server, log)


def test_a_release_without_the_check_says_to_send_plans_first(server):
    server.release("v0.3.1", edit=without_the_check)
    result = server.deploy("v0.3.1")
    note = (
        "Note: v0.3.1 has no check of the plans in flight (#989): its deploy restarts the bots "
        "whatever they apply. Send !plans to every bot first, and wait for the result of any "
        "apply under way (BOOTSTRAP §13.4)."
    )
    assert note in result.stderr
    # before the prompt (this rig's release carries today's deploy.sh, whose server half then
    # misses the module; a release from before #989 has no check at all)
    assert result.stderr.index(note) < result.stderr.index("Type v0.3.1 to deploy it")


def test_a_release_with_the_check_has_no_such_note(server, bot):
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert result.returncode == 0 and "has no check of the plans in flight" not in result.stderr


@pytest.mark.parametrize(
    ("flags", "why"),
    [
        (["--plan-wait", "121"], "--plan-wait: minutes, from 0 to 120: 121"),
        (["--plan-wait", "05"], "--plan-wait: minutes, from 0 to 120: 05"),
        (["--plan-wait", ""], "--plan-wait needs a value"),
        (["--interrupt-plans", 'a "quote"'], "--interrupt-plans: a reason of letters"),
        (["--interrupt-plans", " lead"], "--interrupt-plans: a reason of letters"),
        (["--interrupt-plans", "x" * 201], "--interrupt-plans: a reason of letters"),
        (["--interrupt-plans", "a\nb"], "--interrupt-plans: a reason of letters"),
        (["--wheel", "--plan-wait", "5"], "--wheel goes alone"),
        (["--wheel", "--interrupt-plans", "x"], "--wheel goes alone"),
    ],
)
def test_the_laptop_refuses_a_bad_value_or_a_deploy_without_a_restart(server, flags, why):
    log = systemctl_log(server)
    server.release("v0.3.1")
    refused(server.deploy("v0.3.1", *flags), why)
    unchanged(server, log)


def test_neither_goes_with_a_review_or_a_bootstrap(rig):
    rig.release("v0.3.0")
    where = ["--repo", rig.repo, "--etc", rig.etc, "--out", rig.tmp / "out"]
    for flag, value in (("--plan-wait", "5"), ("--interrupt-plans", "why")):
        done = rig.run("bash", DEPLOY, *where, "--review", "HEAD", flag, value)
        refused(done, "--plan-wait and --interrupt-plans go with a deploy, not --review")
        refused(rig.deploy("v0.3.0", "--bootstrap", flag, value), "a bootstrap has no plan")
    assert rig.current() is None


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["--plan-wait", "121"], None),
        (["--plan-wait", "5", "--in-band", "P-1"], None),
        (["--plan-wait", "5", "--build-wheel"], None),
        ([], "a\x1b]0;x\x07"),
        (["--in-band", "P-1"], "a reason"),
    ],
)
def test_the_server_half_checks_them_again(server, bot, flags, reason):
    server.release("v0.3.1")

    def with_reason(payload):
        if reason is not None:
            (payload / "interrupt-plans").write_text(reason + "\n")

    flags = [*flags, *(["--base", "v0.3.0", "--diff-sha256", "a" * 64] if "--in-band" in flags
                       else [])]  # fmt: skip
    refused(server_half(server, "v0.3.1", *flags, edit=with_reason), "bad arguments")
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]
