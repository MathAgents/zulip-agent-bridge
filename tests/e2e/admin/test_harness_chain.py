"""The scenarios' chain of prerequisites (`conftest.requires`), offline (issue #356): what runs
and what skips, and why, while Q9's provisioning key is still Drew's to give."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from types import SimpleNamespace

import pytest

from e2e.admin import conftest
from e2e.admin import test_scenarios_1_server as server
from e2e.admin import test_scenarios_2_bot as bot
from e2e.admin import test_scenarios_3_defaults as defaults
from e2e.admin import test_scenarios_3_late_joiner as joiner
from e2e.admin import test_scenarios_4_grow_disk as grow
from e2e.admin import test_scenarios_8_later as later
from e2e.admin import test_scenarios_8_the_end as end
from e2e.admin import test_scenarios_8_the_kept_fleet as kept
from e2e.admin import test_scenarios_9_checks as checks

#: The scenario parts, in the order pytest runs them (file, then definition order).
PARTS = [
    fn
    for module in (server, bot, defaults, joiner, grow, later, end, kept, checks)
    for name, fn in vars(module).items()
    if name.startswith("test_") and callable(fn)
]
#: The parts that skip themselves while the provisioning key is a placeholder (Q9).
Q9_GATED = (
    bot.test_a5_provision_zulip,
    end.test_a9_the_throwaway_rows_bots,
    kept.test_a18_the_bots_move_to_the_provisioner,
    later.test_a16_new_workshop,
    joiner.test_a20_a_late_joiner_gets_a_spare_with_the_waker,  # and the GitHub token
)


def test_the_parts_run_in_their_files_order():
    """Pytest runs the files in name order (conftest): A23 after the bot scenarios (A8's waker
    among them) and before A20, whose idle stop it must not find on its VM; A20 before A9,
    which finds the fleet as A20 leaves it; A9 after the later stages' hooks; A18 after A9,
    which ends the first workshop's VMs; then the checks that sweep what they did (A12,
    A13)."""
    modules = (server, bot, defaults, joiner, grow, later, end, kept, checks)
    assert sorted(m.__name__ for m in modules) == [m.__name__ for m in modules]


def reads(fn) -> set[str]:
    """What a part reads of the earlier ones' records (`DONE[...]`, not an assignment)."""
    return set(re.findall(r'DONE\["(\w+)"\](?!\s*=[^=])', inspect.getsource(fn)))


def establishes(fn) -> set[str]:
    """What a part records for the later ones when it passes (`DONE[...] = …`)."""
    return set(re.findall(r'DONE\["(\w+)"\]\s*=', inspect.getsource(fn)))


def walk(*, q9_answered: bool) -> dict[str, bool]:
    """Each part's name → whether it runs, every part that runs passing. A5's provisioning, A9's
    throwaway bots and A18's move skip themselves while the provisioning key is a placeholder
    (Q9), so they establish nothing then."""
    done: set[str] = set()
    ran = {}
    for fn in PARTS:
        runs = set(getattr(fn, "requires", ())) <= done
        if fn in Q9_GATED and not q9_answered:
            runs = False
        ran[fn.__name__] = runs
        if runs:
            done |= establishes(fn)
    return ran


#: Every part, and whether it runs while Q9 is open (its own skips, such as A14's `age` or a
#: later stage's hook, aside): a new part must say where it belongs (reviewer on #368).
WHILE_Q9_IS_OPEN = {
    "test_deploy_gate": True,
    "test_a2_the_first_import": True,
    "test_a3_binding": True,
    "test_a4_launch_and_protect": True,
    "test_a14_the_deployer_has_backups": True,
    "test_a2_a_duplicate_and_a_rename_are_refused": True,
    "test_a5_provision_zulip": False,
    "test_a5_provision_repos": False,
    "test_a5_invites_only_twice": False,  # #708: after the full runs, which need A5's bots
    "test_a5_invites_only_raises_a_test_login_to_admin": False,
    "test_a2_e_mail_changes": False,
    "test_a1_a_stranger_is_refused_once": True,
    "test_a1_a_command_sent_as_code_gets_the_hint": True,  # needs nothing but the bot (#546)
    "test_a1_a_participant_bot_is_never_obeyed": False,
    "test_a6_rollout": False,
    "test_a6_a_full_disk_names_its_free_space": True,  # A4's VMs only: status.py's read (#552)
    "test_a7_the_stop_plan_lists_no_running_turn": False,
    "test_a7_stop_and_start": True,
    "test_a15_two_workshops": True,
    "test_a8_a_start_that_cant_be_checked_alerts": True,  # zab-test-a is never bound
    "test_a8_a_start_with_no_message_alerts": False,  # the bots' keys and zulip_user_id: A5
    "test_a8_the_waker": False,
    "test_a8_the_waker_comes_up_after_a_busy_topic": False,  # #717: the same, and A5's bots
    # A23 (#704): A5's bots, for the waker and the rollout
    "test_a23_a_rollout_without_the_words_applies_the_defaults": False,
    # A20: A5's bots and A6's rollout first; its last part undoes what the others did, if any
    "test_a20_the_fleet_gets_the_waker_then_idle_stop": False,
    "test_a20_a_spare_stops_by_itself_after_its_warm_up": False,
    "test_a20_a_late_joiner_gets_a_spare_with_the_waker": False,
    "test_a20_the_fleet_as_a9_finds_it": True,
    # A21 (#553): a launched fleet is all it needs, and it leaves it as A20 does
    "test_a21_a_size_that_doesnt_grow_is_refused": True,
    "test_a21_a_running_vm_grows_online": True,
    "test_a21_a_stopped_vm_grows_at_its_next_boot": True,
    "test_later_stage": True,
    "test_a16_new_workshop": False,  # the provisioner's key makes its bot and channels
    # A9: the provisioner's key makes the throwaway row's bots, which !deactivate-bots ends; the
    # final sync runs its --strict refusal of a row without a repository, then skips (Q9)
    "test_a9_two_approvers": True,
    "test_a9_the_throwaway_row": True,
    "test_a9_the_throwaway_rows_bots": False,
    "test_a9_the_throwaway_rows_start_here": False,  # its bots post it (#709)
    "test_a9_a_spare_comes_and_goes": True,
    "test_a9_uninstall": True,
    "test_a9_final_sync": True,
    "test_a9_export": True,
    "test_a9_teardown_refuses_an_unsynced_row": True,
    "test_a9_a_protected_vm_is_kept_and_a_stale_plan_refused": True,
    "test_a9_teardown_all_unsynced": True,
    "test_a9_retire": True,  # the happy path waits for the token; the rest runs (#559)
    "test_a9_deactivate_bots": False,
    "test_a9_a_rebind_is_not_offered_yet": True,
    "test_a9_the_rig_config_again": True,
    # A18: only the move needs the test provisioner's key; the rest, the development machine's and
    # the bot's
    "test_a18_a_vm_launched_without_the_servers_key": True,
    "test_a18_a_slug_of_another_workshop_is_refused": True,
    "test_a18_the_stage_lands_on_the_server": True,
    "test_a18_the_vm_joins_over_the_break_glass_key": True,
    "test_a18_a_rollout_through_the_bot": True,
    "test_a18_the_bots_move_to_the_provisioner": False,
    "test_a18_the_waker_wakes_it": True,
    "test_a18_the_servers_key_comes_off": True,
    "test_a18_its_bots_are_deactivated": True,
    "test_role_confinement": True,
    "test_role_sees_only_the_fleet": True,
    "test_imds_only_for_the_key_holders": True,
    "test_no_secret_anywhere": True,
    "test_no_secret_in_the_run_files": True,
}


def test_while_q9_is_open_the_power_parts_run_and_the_rest_skip():
    assert walk(q9_answered=False) == WHILE_Q9_IS_OPEN


def test_each_part_requires_what_it_reads():
    """A part that reads an earlier part's record (`DONE["roster"]`) declares it, so it skips,
    naming it, instead of failing on a KeyError (reviewer on #368)."""
    for fn in PARTS:
        missing = reads(fn) - set(getattr(fn, "requires", ()))
        assert not missing, f"{fn.__name__} reads {sorted(missing)} without requiring it"
    assert reads(bot.test_a2_e_mail_changes) == {"roster"}  # the check reads what it should


def test_once_q9_is_answered_every_part_runs():
    assert all(walk(q9_answered=True).values())


def test_the_rollout_is_what_the_turns_check_and_the_waker_wait_for():
    assert "rolled_out" in establishes(bot.test_a6_rollout)
    assert "rolled_out" in bot.test_a7_the_stop_plan_lists_no_running_turn.requires
    assert "rolled_out" in bot.test_a8_the_waker.requires
    assert bot.test_a7_stop_and_start.requires == ("launched",)


@pytest.fixture
def done(monkeypatch):
    state: dict = {}
    monkeypatch.setattr(conftest, "DONE", state)
    return state


def test_the_skips_name_their_reason(done):
    done.update(roster=True, launched=True, participants=["i-a", "i-b"])
    with pytest.raises(pytest.skip.Exception, match="rolled_out"):
        asyncio.run(bot.test_a7_the_stop_plan_lists_no_running_turn(
            scenario=None, tester=None, rigged=None, nonce="x"))  # fmt: skip
    with pytest.raises(pytest.skip.Exception, match="provisioned, rolled_out"):
        asyncio.run(bot.test_a8_the_waker(scenario=None, tester=None, tester2=None, rigged=None,
                                          server=None, aws=None, nonce="x"))  # fmt: skip
    with pytest.raises(pytest.skip.Exception, match="Q9"):
        asyncio.run(bot.test_a5_provision_zulip(
            scenario=None, tester=None, tester2=None,
            rigged=SimpleNamespace(provisioner=False), nonce="x"))  # fmt: skip
    with pytest.raises(pytest.skip.Exception, match="throwaway_bots"):
        asyncio.run(end.test_a9_deactivate_bots(scenario=None, rigged=None, server=None,
                                                tester=None, tester2=None, nonce="x"))  # fmt: skip
    done.update(throwaway="zab-test-tmp-1")
    with pytest.raises(pytest.skip.Exception, match="Q9"):
        asyncio.run(end.test_a9_the_throwaway_rows_bots(
            scenario=None, rigged=SimpleNamespace(provisioner=False), server=None, tester=None,
            nonce="x"))  # fmt: skip
    with pytest.raises(pytest.skip.Exception, match="kept_rolled_out"):
        asyncio.run(kept.test_a18_the_waker_wakes_it(scenario=None, tester2=None, rigged=None,
                                                    server=None, aws=None, nonce="x"))  # fmt: skip
    done.update(kept_bots={"codex": "c@x", "claude": "k@x"})
    with pytest.raises(pytest.skip.Exception, match="Q9"):
        asyncio.run(kept.test_a18_the_bots_move_to_the_provisioner(
            scenario=None, admin_rig=None, tester=None, tester2=None, laptop=None,
            rigged=SimpleNamespace(provisioner=False)))  # fmt: skip


def test_a9_needs_two_approvers_where_it_tears_down():
    """Every A9 part that approves a destructive plan waits for the two-approver admin.toml,
    and nothing in A9 reads it back: its last part puts the rig's back whatever came before."""
    destructive = (end.test_a9_a_spare_comes_and_goes, end.test_a9_teardown_all_unsynced,
                   end.test_a9_a_protected_vm_is_kept_and_a_stale_plan_refused,
                   end.test_a9_retire, end.test_a9_deactivate_bots)  # fmt: skip
    assert all("two_approvers" in fn.requires for fn in destructive)
    assert "two_approvers" in establishes(end.test_a9_two_approvers)
    assert not hasattr(end.test_a9_the_rig_config_again, "requires")
    assert [fn.__name__ for fn in PARTS if fn.__module__ == end.__name__][-1] == (
        "test_a9_the_rig_config_again"
    )


def vm(slug: str, state: str, workshop: str = "zab-admin-e2e") -> dict:
    tags = [{"Key": "participant", "Value": slug}, {"Key": "workshop", "Value": workshop}]
    return {"InstanceId": f"i-{slug}-{workshop}", "State": {"Name": state}, "Tags": tags}


def test_a18_skips_naming_the_running_vms_when_more_would_run(monkeypatch):
    """§7: at most three instances run; A18 comes after A9, or A7's stop."""
    aws = SimpleNamespace(fleet=lambda: [vm("zab-test-a", "running"), vm("zab-test-b", "pending")])
    with pytest.raises(pytest.skip.Exception, match=r"zab-test-a', 'zab-test-b'\] run"):
        asyncio.run(kept.test_a18_a_vm_launched_without_the_servers_key(
            scenario=None, admin_rig=None, rigged=None, aws=aws, tester=None, tester2=None,
            laptop=None))  # fmt: skip


def test_a18_runs_beside_one_other_vm():
    """The boundary (review of #381): A8 leaves zab-test-b running, A7 stopped the rest; with
    the server and zab-test-c, that is three, which §7 allows."""
    for fleet in ([], [vm("zab-test-b", "running"), vm("zab-test-a", "stopped")],
                  [vm("zab-test-a", "stopping"), vm("zab-test-b", "stopped")]):  # fmt: skip
        assert kept.crowded(SimpleNamespace(fleet=lambda fleet=fleet: fleet)) == []
    two = [vm("zab-test-a", "pending"), vm("zab-test-b", "running")]
    assert kept.crowded(SimpleNamespace(fleet=lambda: two)) == ["zab-test-a", "zab-test-b"]
    assert kept.OTHERS == 1


def test_a18s_vm_is_its_own_workshops_alone():
    """A `participant=zab-test-c` of another workshop is never taken for A18's VM."""
    other, mine = vm("zab-test-c", "running"), vm("zab-test-c", "running", "zab-admin-e2e2")
    assert kept.vm_of(SimpleNamespace(participants=lambda: [other])) is None
    assert kept.vm_of(SimpleNamespace(participants=lambda: [other, mine])) is mine


class Bot:
    """A `Talk` whose `run` records each command, and fails when told to."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[str] = []
        self.fails = fails

    async def run(self, command: str):
        self.sent.append(command)
        if self.fails:
            raise AssertionError(f"{command}: the bot refused")


async def test_the_waker_entry_never_outlives_a18_or_a8():
    """`!waker off` in a finally (review of #381): after the body, and after a failure, whose
    error is the one reported; a failed `off` after a passed body is a failure."""
    bot_ = Bot()
    async with bot.waker_on(bot_, "zab-test-c"):
        pass
    assert bot_.sent == ["!waker off zab-test-c"]
    for fails in (False, True):
        bot_ = Bot(fails=fails)
        with pytest.raises(TimeoutError, match="no !login"):
            async with bot.waker_on(bot_, "zab-test-b"):
                raise TimeoutError("no !login")
        assert bot_.sent == ["!waker off zab-test-b"]
    with pytest.raises(AssertionError, match="refused"):
        async with bot.waker_on(Bot(fails=True), "zab-test-c"):
            pass
    assert "waker_on(t2, SLUG)" in inspect.getsource(kept.test_a18_the_waker_wakes_it)
    assert "waker_on(t, B)" in inspect.getsource(bot.test_a8_the_waker)


async def test_a8_leaves_zab_test_a_stopped(monkeypatch):
    """The pre-Q9 part (#359) stops zab-test-a before its direct start and again after it, as
    A7 leaves it; after a failure, the failure is the error reported."""
    states = {"zab-test-a": "running"}

    async def wait_state(aws, slug, state, within=600):
        states[slug] = state  # EC2 gets there

    monkeypatch.setattr(bot, "vm_state", lambda aws, slug: states[slug])
    monkeypatch.setattr(bot, "wait_state", wait_state)
    bot_ = Bot()
    async with bot.stopped_after(bot_, None, "zab-test-a"):
        assert bot_.sent == ["!stop zab-test-a"] and states["zab-test-a"] == "stopped"
        states["zab-test-a"] = "pending"  # the starter's start
    assert bot_.sent == ["!stop zab-test-a"] * 2 and states["zab-test-a"] == "stopped"
    bot_ = Bot()
    with pytest.raises(TimeoutError, match="no alert"):
        async with bot.stopped_after(bot_, None, "zab-test-a"):
            states["zab-test-a"] = "running"
            raise TimeoutError("no alert")
    assert bot_.sent == ["!stop zab-test-a"] and states["zab-test-a"] == "stopped"
    source = inspect.getsource(bot.test_a8_a_start_that_cant_be_checked_alerts)
    assert "stopped_after(t, aws, A)" in source


def test_a8_takes_a_held_start_only_with_its_earlier_alert():
    """A rerun within the hour (review of #391): a held direct start must have an alert of the
    same VM, for a start less than an hour before, on record; a "not opted in" one is another
    limit's."""

    def check(instance="i-a", **fields):
        return json.dumps({"seq": 1, "event": "wake_check", "instance": instance, **fields})

    lines = [check(start_t=1000.0, alert=7), check(start_t=2000.0, alert=8, outcome="failed"),
             check(start_t=2500.0, alert=9, outcome="not_opted_in"),
             check("i-b", start_t=4000.0, alert=10)]  # fmt: skip
    server = SimpleNamespace(read=lambda path: "\n".join(lines) + "\n")
    assert bot.earlier_alert(server, {"instance": "i-a", "start_t": 5000.0})["alert"] == 8
    assert bot.earlier_alert(server, {"instance": "i-a", "start_t": 5700.0}) is None
    assert bot.earlier_alert(server, {"instance": "i-b", "start_t": 3000.0}) is None


def test_a12_sweeps_every_channel_of_the_rig():
    """The alerts channels too (review of #381): the waker posts there with participant bots'
    keys, as the bots post plans and results in theirs."""
    swept = inspect.getsource(checks.test_no_secret_anywhere)
    for name in ("E2E.channel", "E2E2.channel", "ADMINS_CHANNEL", "E2E.alerts", "E2E2.alerts"):
        assert name in swept, f"A12 doesn't sweep {name}"


def test_a20_builds_on_the_rollout_and_puts_the_fleet_back_last():
    """A20 (#456) needs A5's bots and A6's rollout; both spare parts need the fleet's waker and
    idle stop from its first; its last part, which undoes what the others did, waits for
    nothing and comes last, as A9's does."""
    first, stops, late, after = (fn for fn in PARTS if fn.__module__ == joiner.__name__)
    assert set(first.requires) == {"launched", "provisioned", "rolled_out"}
    assert establishes(first) == {"waker_then_idle_stop"}
    assert stops.requires == late.requires == ("waker_then_idle_stop",)
    # #715: the late joiner adopts the spare that stopped by itself, when that part passed
    assert establishes(stops) == {"spare_stopped"} and establishes(late) == {"late_joiner"}
    assert 'DONE.get("spare_stopped")' in inspect.getsource(late)
    assert after is joiner.test_a20_the_fleet_as_a9_finds_it
    assert not hasattr(after, "requires")


def test_a20_skips_naming_its_reason(done):
    done.update(waker_then_idle_stop=240)
    with pytest.raises(pytest.skip.Exception, match=r"Q9: .*GitHub token"):
        asyncio.run(joiner.test_a20_a_late_joiner_gets_a_spare_with_the_waker(
            scenario=None, tester=None, rigged=SimpleNamespace(github_token=False), server=None,
            aws=None, admin_rig=None, nonce="x"))  # fmt: skip
    crowded = [vm("zab-test-b", "running"), vm("zab-test-c", "pending", "zab-admin-e2e2")]
    aws = SimpleNamespace(fleet=lambda: crowded)
    with pytest.raises(pytest.skip.Exception, match=r"\['zab-test-c'\] run besides"):
        asyncio.run(joiner.room_for(Bot(), aws, running=("zab-test-b",), beside=("zab-spare-1",)))


class Listing:
    """`server.run` for `no_bots`: `ls -A` of each directory in `dirs`, failing as `run` does
    (`RemoteError`, check=True) for one that isn't there or in `failing`."""

    def __init__(self, dirs: dict[str, list[str]], failing: tuple[str, ...] = ()) -> None:
        self.dirs, self.failing, self.listed = dirs, failing, []

    def run(self, argv):
        from e2e.harness.vm import RemoteError, Result

        assert argv[:4] == ["sudo", "ls", "-A", "--"], argv
        where = argv[4]
        self.listed.append(where.removeprefix(f"{end.WS}/"))
        if where in self.failing or where not in self.dirs:
            raise RemoteError(argv, Result(2, "", f"ls: cannot access '{where}'"))
        return Result(0, "\n".join(self.dirs[where]) + "\n", "")


def test_no_bots_lists_out_then_each_row_that_has_a_directory():
    """Codex on #566 and the review's N4: a row never provisioned has no `out/<row>/`, which is
    no key; a row's `*.zuliprc` fails the check, and so does any other failure to list."""
    out = f"{end.WS}/out"
    server = Listing({out: ["known_hosts", "zab-test-r"], f"{out}/zab-test-r": ["notes.txt"]})
    end.no_bots(server, ("zab-test-r", "zab-test-tmp-1-r"))
    assert server.listed == ["out", "out/zab-test-r"]  # the row with no directory: not listed
    keyed = Listing({out: ["zab-test-r"], f"{out}/zab-test-r": ["claude.zuliprc", "x.txt"]})
    with pytest.raises(AssertionError, match=r"zab-test-r has bot keys in out/: \['claude"):
        end.no_bots(keyed, ("zab-test-r",))
    from e2e.harness.vm import RemoteError

    for failing in (out, f"{out}/zab-test-r"):  # never read as "no key"
        broken = Listing({out: ["zab-test-r"], f"{out}/zab-test-r": []}, failing=(failing,))
        with pytest.raises(RemoteError):
            end.no_bots(broken, ("zab-test-r",))


def test_a20_changed_nothing_so_its_last_part_does_nothing(monkeypatch):
    monkeypatch.setattr(joiner, "MADE", {})
    scenario = SimpleNamespace(notes=[])
    asyncio.run(joiner.test_a20_the_fleet_as_a9_finds_it(
        scenario=scenario, tester=None, rigged=None, server=None, aws=None, nonce="x"))  # fmt: skip
    assert scenario.notes == ["A20 changed nothing: nothing to put back"]
