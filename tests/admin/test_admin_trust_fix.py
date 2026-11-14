"""`!trust-fix SEL` (#992; ADMIN §3, ORGANIZERS §4.4): the VMs' trust fix as a plan, through
the bots of `botkit` with the real `Runner`, as in `test_admin_runner.py`, running the real
`workshop/admin/trust_fix.py` (its `main`) with its SSH answered by fake VMs, as
`test_admin_runner.ADOPT` runs `adopt_spare.py`. The plan is its dry run, a row per VM; the
approval applies it, and the result names each row's outcome; a stopped VM is shown, never
started, and fails the apply; `!reject` drops it. The trust fix and a rollout or a final sync
of one workshop never run beside each other. The script's own flag is tested in
`tests/workshop/test_trust_fix.py`."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from zab.admin import actions
from zab.admin.actions import Row, build
from zab.admin.approvals import CHANGE
from zab.admin.grammar import Command, Selection
from zab.admin.plans import DONE, FAILED, PENDING, PLANNING, REFUSED, REJECTED, Requirement

from .botkit import HARVARD, OLGA, incoming, until
from .test_admin_actions import FLEET
from .test_admin_runner import (
    GATED,
    LATE,
    approve,
    bot_of,
    calls,
    fake,
    posts,
    records,
    request,
    server,
    settle,
    site,
)

assert site and server  # fixtures, used by name

ADMIN = str(Path(__file__).resolve().parents[2] / "workshop" / "admin")
#: trust_fix.py itself, run by its `main`, its SSH to the VMs answered here: each VM reports as
#: one of the Oct 19 image would, its uv and Claude Code's store older at a dry run, fixed at
#: an apply. A VM whose address `STATE` lists `down` isn't reached, as a stopped one isn't;
#: with `STATE`'s `gate`, an apply waits there until `go`.
REAL = """
sys.path.insert(0, ADMIN)
import trust_fix
from admin_common import RunResult

STATE = json.loads(pathlib.Path(STATE_FILE).read_text())


def ssh(argv, *, timeout_s=120.0, env=None, stdin=None):
    host, remote = argv[argv.index("--") - 1].removeprefix("ubuntu@"), argv[-1]
    if host in STATE["down"]:
        return RunResult(255, "", f"ssh: connect to host {host} port 22: Connection timed out")
    if not remote.startswith(trust_fix.REMOTE_CMD):  # an apply's probe
        return RunResult(0, "", "")
    apply = "--apply" in remote.split()
    if apply and host in STATE.get("hang", []):  # until the runner's deadline kills it
        time.sleep(60)
    if apply and STATE.get("gate"):
        gate = pathlib.Path(STATE["gate"])
        (gate / "trust-fix.started").touch()
        for _ in range(3000):
            if (gate / "go").exists():
                break
            time.sleep(0.01)
        else:
            return RunResult(-1, "", "the gate never opened")
    steps = {
        "uv": "0.13.2 -> 0.13.7" if apply else "0.13.2: would install 0.13.7",
        "claude store": "unset -> system" if apply else "unset: would set system",
    }
    check = [] if apply else ["FAILED: claude: CLAUDE_CODE_CERT_STORE is unset"]
    report = {"apply": apply, "steps": steps, "check": check, "ok": apply}
    return RunResult(0, json.dumps(report) + "\\n", "")


trust_fix.subprocess_runner = ssh
sys.exit(trust_fix.main(sys.argv[1:]))
"""
#: status.py's answer (schema 1): alice running, bob as `BOB` gives.
STATUS = """\
vms = [{{"slug": "alice", "host": "198.51.100.1", "bridge": "active", "zab_version": "0.2.17",
        "running_turns": [], "idle_stop": None}},
       {{"slug": "bob", "host": "198.51.100.2", "bridge": {bob!r}, "zab_version": None,
        "running_turns": None, "idle_stop": None}}]
print(json.dumps({{"schema": 1, "vms": vms, "summary": {{}}, "ec2_error": None}}, indent=2))
"""
BOB_IP = "198.51.100.2"
NOT_REACHED = f"not reached: ssh: connect to host {BOB_IP} port 22: Connection timed out"


def trust_fix(site, *, down=(), gate=None, hang=()) -> None:
    state = site.tmp / "trust-fix-state.json"
    gated = str(gate) if gate else ""
    state.write_text(json.dumps({"down": list(down), "gate": gated, "hang": list(hang)}))
    fake(site, "admin/trust_fix.py", f"ADMIN, STATE_FILE = {ADMIN!r}, {str(state)!r}\n{REAL}")


def row(text: str, slug: str) -> str:
    """The script's table row of `slug` in `text`."""
    found = re.search(rf"^{slug} +198\.51\.100\.\d+ .*$", text, re.M)
    assert found, f"no row of {slug} in: {text[-800:]}"
    return found[0].rstrip()  # the table pads its last column


async def said(site, start: str) -> str:
    """The bot's post that starts with `start`, once it is posted: a refusal comes after its
    plan's state, and a running apply's progress may come after either."""
    await until(lambda: any(p.startswith(start) for p in posts(site)))
    return next(p for p in posts(site) if p.startswith(start))


def runs(site) -> list[list[str]]:
    return [c["argv"] for c in calls(site, "admin/trust_fix.py")]


def newest(srv) -> int:
    return max([row[0] for row in srv.store.db.execute("SELECT id FROM plans")], default=0)


async def meanwhile(srv, text, mid):
    """`text`'s plan once it is posted or refused, while an apply runs, which `request` would
    wait for too (`settle`)."""
    before = newest(srv)
    await bot_of(srv, HARVARD).on_message(incoming(OLGA, text, mid=mid))
    await until(lambda: newest(srv) > before and srv.store.get(newest(srv)).state != PLANNING)
    return srv.store.get(newest(srv))


async def reject(srv, plan, mid=LATE + 50):
    await bot_of(srv, HARVARD).on_message(incoming(OLGA, f"!reject {plan.name}", mid=mid))
    await settle(srv)


# ------------------------------------------------------------------ plan, apply, reject


async def test_the_plan_is_the_dry_run_of_each_row_with_a_vm(site, server):
    trust_fix(site)
    plan = await request(server, "!trust-fix all")
    assert plan.state == PENDING, plan.note
    assert (plan.requirement, plan.targets) == (Requirement(CHANGE), ("alice", "bob"))
    text = posts(site)[-2]
    dry = text.split("**Dry run**", 1)[1].split("**Status**", 1)[0]
    assert "── admin/trust_fix.py\nparticipant  host " in dry
    for slug in ("alice", "bob"):  # carol has no VM: `all` is the rows with one
        found = row(dry, slug)
        assert "0.13.2: would install 0.13.7" in found and found.endswith("FAILED"), found
    assert "\n2 VM(s) reached (dry run: nothing changed)\n── admin/trust_fix.py exited 0" in dry
    assert "**The trust fix** (workshop/admin/README.md §10)" in text
    (argv,) = runs(site)  # the plan's run: no --apply, and as the bot runs it
    assert "--apply" not in argv and "--admin-bot" in argv
    assert " ".join(argv).endswith("--admin-bot --only alice --only bob")
    assert [c["script"] for c in calls(site)] == ["admin/trust_fix.py", "admin/status.py"]


async def test_approved_it_applies_and_its_result_names_each_rows_outcome(site, server):
    trust_fix(site)
    plan = await request(server, "!trust-fix all")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    result = posts(site)[-1]
    assert result.startswith(f"✅ **{plan.name}** · `!trust-fix all`: done in "), result
    for slug in ("alice", "bob"):
        found = row(result, slug)
        assert "0.13.2 -> 0.13.7" in found and "unset -> system" in found, found
        assert found.endswith("  ok"), found
    assert "\n2 VM(s) fixed and checked\n── admin/trust_fix.py exited 0" in result
    # a reconciler: the apply runs no dry run again, only the apply itself
    assert ["--apply" in argv for argv in runs(site)] == [False, True]
    end = records(site, "apply_end")[-1]
    assert (end["verb"], end["targets"], end["exit"]) == ("trust-fix", ["alice", "bob"], 0)


async def test_a_stopped_row_is_shown_then_fails_the_apply_and_is_never_started(site, server):
    """As `!rollout` treats a stopped VM: the plan is offered, its status table says `stopped`
    and the dry run's row `not reached`; the apply goes on with the others and fails on it,
    naming the bot's command to send once it is started. Nothing starts it."""
    trust_fix(site, down=[BOB_IP])
    fake(site, "admin/status.py", STATUS.format(bob="stopped"))
    plan = await request(server, "!trust-fix all")
    assert plan.state == PENDING, plan.note
    text = posts(site)[-2]
    assert row(text, "bob").endswith(NOT_REACHED)
    assert "; not done: bob\n── admin/trust_fix.py exited 0" in text
    assert re.search(r"^bob +stopped ", text.split("**Status**", 1)[1], re.M), text
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    result = posts(site)[-1]
    assert result.startswith(
        f"❌ **{plan.name}** · `!trust-fix all`: failed at step 1 of 1: admin/trust_fix.py exited 1"
    ), result
    assert row(result, "alice").endswith("  ok") and row(result, "bob").endswith(NOT_REACHED)
    assert "Then rerun them:\n  !trust-fix bob\n" in result
    assert calls(site, "aws/power.py") == []


async def test_a_rejected_plan_never_applies(site, server):
    trust_fix(site)
    plan = await request(server, "!trust-fix bob")
    await reject(server, plan)
    assert server.store.get(plan.id).state == REJECTED
    assert posts(site)[-1].startswith(f"✅ {plan.name} rejected")
    assert ["--apply" in argv for argv in runs(site)] == [False]


async def test_each_vm_is_said_as_it_ends_and_a_timeout_keeps_those_done(site, server, monkeypatch):
    """Review round 1 on #994, N1: the apply prints each VM's outcome as it ends, so the
    progress shows it, and a run its deadline cuts short keeps the VMs done so far; the deadline
    grows with the rows (shortened here)."""
    monkeypatch.setattr(actions, "TRUST_FIX_FLOOR_S", 6)
    monkeypatch.setattr(actions, "TRUST_FIX_ROW_S", 1)
    trust_fix(site, hang=[BOB_IP])
    plan = await request(server, "!trust-fix all")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    result = posts(site)[-1]
    assert result.startswith(f"⏱ **{plan.name}** · `!trust-fix all`: timed out after 6 s"), result
    shown = "done: alice (198.51.100.1): ok (uv: 0.13.2 -> 0.13.7; claude store: unset -> system)"
    assert f"\n{shown}\n" in result, result
    assert "done: bob" not in result and "participant  host" not in result  # cut before both
    progress = [text for _, _, text in site.realm.edits if text.startswith("⏳")]
    assert any("done: alice" in text for text in progress), progress


def test_the_deadline_grows_with_the_rows():
    """An hour, or 2 minutes a row above 30: six VMs at a time, 12 minutes each at worst."""
    every = Command("trust-fix", selection=Selection(all=True))
    assert build(every, FLEET).timeout == actions.TRUST_FIX_FLOOR_S == 3600
    rows = tuple(Row(f"p{n}", f"198.51.100.{n + 1}", "us-west-2") for n in range(41))
    many = build(every, dataclasses.replace(FLEET, rows=rows))
    assert len(many.targets) == 41 and many.timeout == 41 * actions.TRUST_FIX_ROW_S == 4920


# ---------------------------------------------------------------- never beside each other


@pytest.mark.parametrize(
    "command, script",
    [("!rollout v0.2.5 all", "admin/push_config.py"), ("!final-sync all", "admin/final_sync.py")],
)
async def test_the_trust_fix_waits_for_a_rollout_or_a_final_sync(site, server, command, script):
    """#992: while a rollout or a final sync of the workshop is approved or applying, a
    `!trust-fix` is refused at its request, with nothing run, and a trust fix's plan already
    pending is answered `busy:` (§2.4 step 5); once the other ends, it plans and applies."""
    wheel = Path(site.paths.layout.wheels) / "zulip_agent_bridge-0.2.5-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    wheel.write_bytes(b"wheel")
    gate = site.tmp / "gate"
    gate.mkdir()
    fake(site, script, GATED.format(gate=str(gate)))
    trust_fix(site)
    waiting = await request(server, "!trust-fix alice", mid=4900)
    other = await request(server, command)
    assert other.state == PENDING, other.note
    phrase = " ".join(other.requirement.phrase) if other.requirement else ""
    await approve(server, other, phrase=phrase)
    await until(lambda: (gate / ".started").exists())
    refused = await meanwhile(server, "!trust-fix all", mid=5001)
    assert refused.state == REFUSED
    verb = command.split()[0]
    assert refused.note == (
        f"{other.name} (`{verb}`) is approved or applying here, and the trust fix never runs "
        "beside a rollout or a final sync: they share each VM's idle-stop hold. Request it "
        f"again once {other.name}'s result is posted"
    )
    await said(site, f"❌ {refused.name} refused: {other.name} (`{verb}`) is")
    await approve(server, waiting, mid=LATE + 1)
    await said(site, f"busy: {other.name} is applying")
    assert ["--apply" in argv for argv in runs(site)] == [False]  # waiting's plan alone
    (gate / "go").touch()
    await settle(server)
    assert server.store.get(other.id).state == DONE
    again = await request(server, "!trust-fix all", mid=5002)
    assert again.state == PENDING, again.note
    await approve(server, again, mid=LATE + 2)
    await settle(server)
    assert server.store.get(again.id).state == DONE


async def test_a_rollout_and_a_final_sync_wait_for_the_trust_fix(site, server):
    """#992, the other way: while a `!trust-fix` applies, a `!rollout` or a `!final-sync` of
    its workshop is refused at its request, before its dry run; any other plan is made, and its
    approval answered `busy:` as for any two plans of a workshop."""
    wheel = Path(site.paths.layout.wheels) / "zulip_agent_bridge-0.2.5-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    wheel.write_bytes(b"wheel")
    gate = site.tmp / "gate"
    gate.mkdir()
    trust_fix(site, gate=gate)
    fixing = await request(server, "!trust-fix all")
    await approve(server, fixing)
    await until(lambda: (gate / "trust-fix.started").exists())
    for n, command in enumerate(("!rollout v0.2.5 all", "!final-sync all")):
        refused = await meanwhile(server, command, mid=5001 + n)
        assert refused.state == REFUSED
        assert refused.note.startswith(
            f"{fixing.name} (`!trust-fix`) is approved or applying here, and the trust fix never "
            "runs beside a rollout or a final sync"
        ), refused.note
    assert calls(site, "admin/push_config.py") == calls(site, "admin/final_sync.py") == []
    other = await meanwhile(server, "!stop alice", mid=5010)
    assert other.state == PENDING, other.note
    await approve(server, other, mid=LATE + 1)
    await said(site, f"busy: {fixing.name} is applying")
    (gate / "go").touch()
    await settle(server)
    assert server.store.get(fixing.id).state == DONE
    fake(site, "admin/final_sync.py", 'print("would sync")')
    assert (await request(server, "!final-sync all", mid=5020)).state == PENDING


async def test_an_approved_plan_not_yet_applying_counts_too(site, server):
    """`Planning.busy` names a plan from its approval, before its apply has begun: a
    `!trust-fix` is refused then too."""
    fake(site, "admin/final_sync.py", 'print("would sync")')
    trust_fix(site)
    sync = await request(server, "!final-sync all")
    server.planner.handed["harvard-2026"] = sync.id
    try:
        assert server.planner._excluded("harvard-2026", "trust-fix").startswith(
            f"{sync.name} (`!final-sync`) is approved or applying here"
        )
        assert server.planner._excluded("harvard-2026", "stop") == ""
        assert server.planner._excluded("zab-test", "trust-fix") == ""
    finally:
        del server.planner.handed["harvard-2026"]
    assert server.planner._excluded("harvard-2026", "trust-fix") == ""
