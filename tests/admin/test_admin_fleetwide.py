"""One rollout, or one trust fix, of every workshop from the server's bot (#995; ADMIN §2.2,
§2.4; `zab.admin.fleetwide`): the grammar's `every-workshop`, the plan's parts and class, and,
through the bots of `botkit` with the real `Runner` and the fake scripts of
`test_admin_runner.py` and `test_admin_trust_fix.py`, one plan approved once, each workshop's
part applied in turn with its own records and result, then one line for them all."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from zab.admin import runner as runner_mod
from zab.admin.actions import (
    STARTS_HALTED,
    STARTS_HALTED_FAILED,
    ActionError,
    Row,
    halt_starts,
)
from zab.admin.approvals import SERVER, requirement
from zab.admin.auditview import View
from zab.admin.fleet import ApplyEnd, Ledger
from zab.admin.fleetwide import HALTS, LOCKS, build_fleetwide, part_command
from zab.admin.grammar import EVERY_WORKSHOP, Command, Context, Roster, Selection, parse
from zab.admin.plans import DONE, FAILED, PENDING, REFUSED, STALE, Requirement
from zab.admin.runner import Runner

from .botkit import ADMINS_CH, DEE, DREW, GLOBAL, incoming
from .test_admin_actions import CONFIG, FLEET, OTHER, PRODUCTION, layout
from .test_admin_runner import approve, calls, fake, posts, records, request, server, settle, site
from .test_admin_trust_fix import trust_fix

__all__ = ["layout", "server", "site"]  # fixtures, used by name
GLOBAL_CTX = Context(GLOBAL, "Workshop admin", PRODUCTION, None)


def every(text: str) -> Command:
    parsed = parse(text, GLOBAL_CTX)
    assert isinstance(parsed, Command), parsed
    return parsed


# ------------------------------------------------------------------------------ the grammar


def test_every_workshop_is_the_server_bots_selection():
    rollout = every("!rollout v0.2.2 every-workshop restart-codex steer=off")
    assert rollout.selection == Selection(every_workshop=True)
    assert rollout.text() == "!rollout v0.2.2 every-workshop restart-codex steer=off"
    assert every("!trust-fix every-workshop").tokens() == (EVERY_WORKSHOP,)
    refusals = {
        "!rollout v0.2.2 all": "this is the server's bot",
        "!rollout v0.2.2 every-workshop all": "already selects every workshop's rows",
        "!stop every-workshop": "this is the server's bot",
        "!status every-workshop": "this is the server's bot",
    }
    for text, why in refusals.items():
        assert why in getattr(parse(text, GLOBAL_CTX), "reason", ""), text
    workshop = Context(901, "Workshop admin · harvard-2026", PRODUCTION, Roster(("alice",)))
    said = parse("!rollout v0.2.2 every-workshop", workshop)
    assert "is the server's bot's" in getattr(said, "reason", ""), said
    with pytest.raises(ValueError, match="stands alone"):
        Selection(all=True, every_workshop=True)
    with pytest.raises(ValueError, match="names no row"):
        Selection(every_workshop=True).targets(Roster(("alice",)))


# --------------------------------------------------------------------------------- the plan


def test_the_plan_is_each_workshops_own_action(layout):
    command = every("!rollout v0.2.2 every-workshop restart-codex")
    action = build_fleetwide(command, [FLEET, OTHER], layout=layout, bot=GLOBAL)
    own = [part_command(command), part_command(command)]
    assert own[0].text() == "!rollout v0.2.2 all restart-codex"
    assert (action.workshop, action.bot, action.targets) == ("", GLOBAL, ("harvard-2026",
                                                                           "zab-test"))  # fmt: skip
    harvard, test = action.parts
    assert harvard.targets == ("alice", "bob", "carol", "dave") and test.targets == ("erin",)
    assert harvard.args == ("v0.2.2", "all", "restart-codex")  # what the ledger reads
    assert action.steps == harvard.steps + test.steps and action.hashed == harvard.hashed
    assert action.notes[-1] == LOCKS and "**zab-test:** " not in "".join(action.notes[:2])
    needs = requirement(action, None, CONFIG)
    assert needs == Requirement(SERVER, 1, ("rollout", "5"))  # every part's rows
    trust = build_fleetwide(every("!trust-fix every-workshop"), [FLEET, OTHER], layout=layout,
                            bot=GLOBAL)  # fmt: skip
    assert requirement(trust, None, CONFIG) == Requirement(SERVER, 1, ())


def test_a_workshop_without_a_vm_is_left_out_and_a_part_that_fails_names_its_workshop(layout):
    bare = dataclasses.replace(OTHER, rows=(Row("erin"),))
    action = build_fleetwide(every("!trust-fix every-workshop"), [FLEET, bare], layout=layout,
                             bot=GLOBAL)  # fmt: skip
    assert action.targets == ("harvard-2026",)
    assert "Left out, with no row in service that has a VM: zab-test." in action.notes
    with pytest.raises(ActionError, match="no workshop has a row with a VM"):
        build_fleetwide(every("!trust-fix every-workshop"), [bare], layout=layout, bot=GLOBAL)
    with pytest.raises(ActionError, match=r"^harvard-2026: no wheel for v0\.2\.9"):
        build_fleetwide(every("!rollout v0.2.9 every-workshop"), [FLEET], layout=layout,
                        bot=GLOBAL)  # fmt: skip


# -------------------------------------------------------------------- through the bots


async def ask(srv, text, sender=DREW, mid=5000):
    return await request(srv, text, sender=sender, bot=GLOBAL, stream=ADMINS_CH, mid=mid)


def by_workshop(site, event):
    return {r["workshop"]: r for r in records(site, event)}


#: status.py, faked: each row it is asked for (`--only`), running.
STATUS = """\
names = [sys.argv[i + 1] for i, w in enumerate(sys.argv) if w == "--only"]
vms = [{"slug": n, "host": "198.51.100.9", "bridge": "active", "zab_version": "0.2.4",
        "running_turns": [], "idle_stop": None} for n in names]
print(json.dumps({"schema": 1, "vms": vms, "summary": {}, "ec2_error": None}, indent=2))
"""


async def test_one_trust_fix_of_every_workshop_approved_once(site, server):
    """Both workshops' parts in one plan of the server's bot, each workshop's status table;
    approved once, by a global admin; each part applied with its own records and result,
    then one line for both."""
    trust_fix(site)
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!trust-fix every-workshop")
    assert plan.state == PENDING, plan.note
    assert plan.workshop == "" and plan.targets == ("harvard-2026", "zab-test")
    text = posts(site, by=GLOBAL)[-2]
    status = text.split("**Status**", 1)[1]
    assert "\nharvard-2026:\nVM " in status and "\nzab-test:\nVM " in status, status
    assert re.search(r"^erin +active .* left running$", status, re.M), status
    assert LOCKS in text
    await approve(server, plan, sender=DREW, bot=GLOBAL, stream=ADMINS_CH)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    said = posts(site, by=GLOBAL)
    for ws in ("harvard-2026", "zab-test"):
        head = f"✅ **{plan.name}** · {ws} · `!trust-fix all`: done in "
        assert any(p.startswith(head) for p in said), (ws, said[-3:])
    assert said[-1].startswith(f"✅ **{plan.name}** · `!trust-fix every-workshop`: 2 of 2 "
                               "workshops done in "), said[-1]  # fmt: skip
    starts, ends = by_workshop(site, "apply_start"), by_workshop(site, "apply_end")
    assert set(starts) == set(ends) == {"harvard-2026", "zab-test"}
    assert ends["zab-test"]["targets"] == ["erin"] and ends["zab-test"]["exit"] == 0
    assert ends["harvard-2026"]["args"] == ["all"] and ends["harvard-2026"]["plan"] == plan.name


async def test_one_rollout_of_every_workshop_is_each_workshops_rollout_of_all(site, server):
    """Its phrase counts every part's rows; a part that fails doesn't stop the next; the
    ledger reads each workshop's part as its own `!rollout … all`, which `!adopt-spare`
    replays; the plan is FAILED, and its last line says which failed."""
    wheel = Path(site.paths.layout.wheels) / "zulip_agent_bridge-0.2.5-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    wheel.write_bytes(b"wheel")
    fake(site, "admin/push_config.py", 'sys.exit(int(not DRY and "harvard-2026" in str(sys.argv)))')
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!rollout v0.2.5 every-workshop")
    assert plan.state == PENDING and plan.requirement.phrase == ("rollout", "4"), plan.note
    await approve(server, plan, sender=DREW, bot=GLOBAL, stream=ADMINS_CH, phrase="rollout 4")
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    last = posts(site, by=GLOBAL)[-1]
    assert last.startswith(f"❌ **{plan.name}** · `!rollout v0.2.5 every-workshop`: 1 of 2 "
                           "workshops done in "), last  # fmt: skip
    assert last.split("\n")[0].endswith("; harvard-2026: failed"), last
    ends = by_workshop(site, "apply_end")
    assert (ends["harvard-2026"]["exit"], ends["zab-test"]["exit"]) == (1, 0)
    assert server.ledger.workshop("zab-test").release == "v0.2.5"
    assert server.ledger.workshop("harvard-2026") is None or not (
        server.ledger.workshop("harvard-2026").release
    )  # its part failed: nothing recorded for `all`
    pushes = [c["argv"] for c in calls(site, "admin/push_config.py", dry=False)]
    assert [any("zab-test" in w for w in argv) for argv in pushes] == [False, True]


async def test_only_a_global_admin_requests_it(site, server):
    await server_says(server, DEE, "!trust-fix every-workshop")
    assert (
        posts(site, by=GLOBAL)[-1] == "❌ only a global admin requests `!trust-fix every-workshop`"
    )
    assert not records(site, "plan")


async def server_says(srv, sender, text):
    bot = next(b for b in srv.bots if b.user_id == GLOBAL)
    await bot.on_message(incoming(sender, text, stream=ADMINS_CH, mid=4000))
    await settle(srv)


async def test_a_part_that_doesnt_build_refuses_the_plan(site, server):
    """Its refusal names the workshop (`build_fleetwide`): no wheel for the release here."""
    plan = await ask(server, "!rollout v0.2.9 every-workshop")
    assert plan.state == REFUSED and plan.note.startswith("harvard-2026: no wheel for v0.2.9")
    assert not calls(site)


# --------------------------------------------------------------- review of #1030's notes


def wheel(site) -> None:
    path = Path(site.paths.layout.wheels) / "zulip_agent_bridge-0.2.5-py3-none-any.whl"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"wheel")


async def test_a_dry_run_too_long_to_hash_says_the_way_round(site, server, monkeypatch):
    """N1: the rollout's dry runs together are bounded by the hash's limit; its refusal says to
    roll out through each workshop's own bot."""
    wheel(site)
    monkeypatch.setattr(runner_mod, "KEEP", 400)
    fake(site, "admin/push_config.py", 'print("dry-run " + "x" * 300)')
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!rollout v0.2.5 every-workshop")
    assert plan.state == REFUSED, plan.note
    assert plan.note.endswith("too long to hash: roll out through each workshop's own bot "
                              "instead"), plan.note  # fmt: skip


async def test_a_change_in_any_workshop_makes_the_plan_stale(site, server):
    """N6: a VM gained by one workshop between plan and approval: rebuilt against every fleet
    as it is then, the plan's hash differs; nothing runs."""
    trust_fix(site)
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!trust-fix every-workshop")
    roster = site.tmp / "ws" / "zab-test" / "roster.csv"
    roster.write_text(roster.read_text() + "fay,Fay,fay@example.com,203.0.113.6,us-east-1\n")
    await approve(server, plan, sender=DREW, bot=GLOBAL, stream=ADMINS_CH)
    await settle(server)
    assert server.store.get(plan.id).state == STALE
    assert not [c for c in calls(site, "admin/trust_fix.py") if "--apply" in c["argv"]]
    assert not records(site, "apply_start")


async def test_an_error_past_a_parts_handling_still_finishes_the_plan(site, server, monkeypatch):
    """N5: the plan is FAILED, its end recorded, and every workshop's lock released; no
    further part runs."""
    trust_fix(site)
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!trust-fix every-workshop")

    async def broken(self, job, *args, **kwargs):
        raise RuntimeError("an error no part handled")

    monkeypatch.setattr(Runner, "_steps", broken)
    await approve(server, plan, sender=DREW, bot=GLOBAL, stream=ADMINS_CH)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    assert (
        posts(site, by=GLOBAL)[-1]
        .split("\n")[0]
        .endswith(
            "0 of 2 workshops done in 0 s; harvard-2026: error (RuntimeError); not begun: zab-test"
        )
    ), posts(site, by=GLOBAL)[-1]
    assert server.planner.runner.applying == {} and server.planner.busy("zab-test") is None
    (end,) = records(site, "plan_end")
    assert end["parts"] == {"harvard-2026": "error (RuntimeError)"} and end["exit"] == 1


async def test_the_console_shows_the_plan_applying_until_its_end(site, server):
    """N2: each part's records say `part`, and the console's view keeps the plan applying past
    the first part's `apply_end`, until `plan_end`. N4: each part's result says where its
    commands go."""
    trust_fix(site)
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!trust-fix every-workshop")
    await approve(server, plan, sender=DREW, bot=GLOBAL, stream=ADMINS_CH)
    await settle(server)
    view, states = View(600.0), []
    for record in records(site):
        view.observe(record)
        if record.get("plan") == plan.name and record["event"] in ("apply_end", "plan_end"):
            states.append(view.open.get(plan.name, {}).get("state"))
    assert states == ["applying", "applying", None]
    assert all(r.get("part") is True for r in records(site, "apply_end"))
    part = next(p for p in posts(site, by=GLOBAL) if p.startswith(f"✅ **{plan.name}** · zab-test"))
    assert "goes to zab-test's own bot, in its organizers channel" in part


# ------------------------------------------------------------------ one breaker (#1031)


@pytest.mark.parametrize("code", [STARTS_HALTED, STARTS_HALTED_FAILED])
async def test_a_part_that_halts_its_starts_halts_them_for_the_later_parts(site, server, code):
    """#1031 (review of #1030, N7): harvard-2026's push_config.py halts its starts (its exit,
    with every row done or not), so zab-test's part runs with `--halted-in harvard-2026`,
    recorded so in its `apply_end`, and starts no VM; the last line names the halt. The plan's
    note says so."""
    wheel(site)
    halts = '"--halted-in" not in sys.argv and "harvard-2026" in str(sys.argv)'
    fake(site, "admin/push_config.py", f"sys.exit({code} * (not DRY and {halts}))")
    fake(site, "admin/status.py", STATUS)
    plan = await ask(server, "!rollout v0.2.5 every-workshop")
    assert plan.state == PENDING, plan.note
    assert HALTS in posts(site, by=GLOBAL)[-2]
    await approve(server, plan, sender=DREW, bot=GLOBAL, stream=ADMINS_CH, phrase="rollout 4")
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    pushes = [c["argv"] for c in calls(site, "admin/push_config.py", dry=False)]
    assert [argv[argv.index("--halted-in") + 1] if "--halted-in" in argv else "" for argv in
            pushes] == ["", "harvard-2026"]  # fmt: skip
    assert all("--start-stopped" in argv for argv in pushes)
    last = posts(site, by=GLOBAL)[-1].split("\n")[0]
    assert "`: 1 of 2 workshops done in " in last, last
    assert last.endswith(
        "; harvard-2026: failed; starts halted in harvard-2026: zab-test started no VM"
    ), last
    ends = by_workshop(site, "apply_end")
    assert (ends["harvard-2026"]["exit"], ends["zab-test"]["exit"]) == (code, 0)
    (push,) = [a for a in ends["zab-test"]["argv"] if "--start-stopped" in a]
    assert push[-2:] == ["--halted-in", "harvard-2026"], push
    assert not any("--halted-in" in a for a in ends["harvard-2026"]["argv"])


def test_halt_starts_changes_only_the_steps_that_start_vms(layout):
    """`halt_starts`: `--halted-in WHERE` on each step with `--start-stopped`, the rest as they
    are; the dry runs, which the hash covers, unchanged."""
    plan = build_fleetwide(every("!trust-fix every-workshop"), [FLEET, OTHER], layout=layout,
                           bot=GLOBAL)  # fmt: skip
    part = plan.parts[1]
    halted = halt_starts(part, "harvard-2026")
    for before, after in zip(part.steps, halted.steps, strict=True):
        assert after.dry_run == before.dry_run
        tail = ("--halted-in", "harvard-2026") * ("--start-stopped" in before.argv)
        assert after.argv == (*before.argv, *tail)
    assert any("--halted-in" in step.argv for step in halted.steps)


@pytest.mark.parametrize("code", [STARTS_HALTED, STARTS_HALTED_FAILED])
async def test_a_rollout_that_halts_its_starts_fails_and_with_every_row_done_counts(
    site, server, code
):
    """Review round 1 of #1033, N2: a workshop's own `!rollout v0.2.5 all` whose push_config.py
    halts its starts fails. With every row done (exit 3), the status read still runs, the title
    says why and to tell a deployer, and the ledger records the release, which `!adopt-spare`
    replays (`ApplyEnd.halted`); with a row not done (exit 4), it is any failure: nothing after
    it runs, and nothing is recorded."""
    wheel(site)
    fake(site, "admin/push_config.py", f"sys.exit({code} * (not DRY))")
    fake(site, "admin/status.py", STATUS)
    plan = await request(server, "!rollout v0.2.5 all")
    assert plan.state == PENDING, plan.note
    phrase = " ".join(plan.requirement.phrase) if plan.requirement.phrase else ""
    planned = len(calls(site, "admin/status.py"))  # the plan's own status read
    await approve(server, plan, phrase=phrase)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    title = posts(site)[-1].split("\n")[0]
    (end,) = records(site, "apply_end")
    statuses = calls(site, "admin/status.py")[planned:]  # the apply's
    recorded = server.ledger.workshop("harvard-2026")
    if code == STARTS_HALTED:
        assert title.endswith(
            "failed at step 1 of 2: admin/push_config.py exited 3: it halted its starts, as the "
            "use of 3 VMs in a row that it started couldn't be read, and did every row all the "
            "same: tell a deployer. The steps after it ran"
        ), title
        assert len(statuses) == 1 and end["halted"] is True and end["exit"] == STARTS_HALTED
        assert recorded is not None and recorded.release == "v0.2.5"
    else:
        assert title.endswith("failed at step 1 of 2: admin/push_config.py exited 4. Nothing "
                              "after it ran"), title  # fmt: skip
        assert not statuses and "halted" not in end
        assert recorded is None or not recorded.release


def test_the_ledger_takes_a_halted_rollout_with_every_row_done_as_done():
    """`ApplyEnd.halted` is written only when set, read back, and makes the ledger record a
    rollout of `all` as an exit 0 would; an exit 3 without it records nothing."""
    end = ApplyEnd("harvard-2026", "P-3", "rollout", ("v0.2.5", "all"), 3, halted=True)
    assert end.fields()["halted"] is True and ApplyEnd.read(end.fields()) == end
    assert "halted" not in ApplyEnd("w", "P-1", "rollout", ("v0.2.5", "all"), 0).fields()
    for halted, release in ((True, "v0.2.5"), (False, "")):
        ledger = Ledger()
        record = ApplyEnd("harvard-2026", "P-3", "rollout", ("v0.2.5", "all"), 3, halted=halted)
        ledger.observe({"event": "apply_end", **record.fields()})
        state = ledger.workshop("harvard-2026")
        assert ((state.release if state else "") or "") == release


def test_only_the_two_scripts_ending_on_their_own_halt():
    """`Ran.halted` and `halted_done`: push_config.py's or trust_fix.py's exit 3 or 4, never one
    the runner ended (a timeout's), nor another script's 3 (roster_sync.py's `NOTHING`, say)."""
    Ran = runner_mod.Ran
    assert Ran("admin/push_config.py", code=3).halted_done
    assert (
        Ran("admin/trust_fix.py", code=4).halted
        and not Ran("admin/trust_fix.py", code=4).halted_done
    )
    assert not Ran("admin/push_config.py", code=3, ended="timeout").halted
    assert (
        not Ran("admin/roster_sync.py", code=3).halted and not Ran("admin/status.py", code=3).halted
    )
    assert not Ran("admin/push_config.py", code=1).halted
