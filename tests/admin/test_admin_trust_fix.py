"""`!trust-fix SEL` (#992; ADMIN §3, ORGANIZERS §4.4): the VMs' trust fix as a plan, through
the bots of `botkit` with the real `Runner`, as in `test_admin_runner.py`, running the real
`workshop/admin/trust_fix.py` (its `main`) with its SSH answered by fake VMs, as
`test_admin_runner.ADOPT` runs `adopt_spare.py`. The plan is its dry run, a row per VM; the
approval applies it, and the result names each row's outcome; a stopped VM is shown `not
reached`, then started for its fix and stopped again (#995), through `power.StartStop` with a
fake EC2; `!reject` drops it. The trust fix and a rollout or a final sync
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
#: an apply. A VM whose address `STATE` lists `down` is stopped, and isn't reached until the
#: fake EC2 starts it (#995), which logs each start and stop to `STATE`'s `ec2`; nobody used
#: it since (`idle_stop.py --in-use`'s answer). With `STATE`'s `gate`, an apply waits there
#: until `go`.
REAL = """
sys.path.insert(0, ADMIN)
import admin_common
import idle_stop
import power
import trust_fix
from admin_common import RunResult

STATE = json.loads(pathlib.Path(STATE_FILE).read_text())
SLUGS = {"198.51.100.1": "alice", "198.51.100.2": "bob", "203.0.113.5": "erin"}
UP = {ip for ip in SLUGS if ip not in STATE["down"]}
UNUSED = {"now": 1e9, "uptime": 60.0, "turns": 0, "activity": None, "jobs": 0, "logins": []}


class Ec2:
    def __init__(self, scope):
        self.scope = scope

    def find_instances(self, tags, states):
        return [{"InstanceId": f"i-{ip[-1]}", "PublicIpAddress": ip,
                 "State": {"Name": "running" if ip in UP else "stopped"},
                 "Tags": [{"Key": k, "Value": v}
                          for k, v in {**self.scope, "participant": slug}.items()]}
                for ip, slug in SLUGS.items()]

    def _log(self, verb, ids):
        with open(STATE["ec2"], "a") as f:
            f.write(verb + " " + " ".join(ids) + chr(10))

    def start(self, ids):  # EC2's answer: each one's state before (review of #1026, N1)
        self._log("start", ids)
        before = [{"InstanceId": i, "PreviousState": {"Name": "running" if ip in UP else
                   "stopped"}} for ip in SLUGS if (i := f"i-{ip[-1]}") in ids]
        UP.update(ip for ip in SLUGS if f"i-{ip[-1]}" in ids)
        return before

    def stop(self, ids):
        self._log("stop", ids)
        UP.difference_update(ip for ip in SLUGS if f"i-{ip[-1]}" in ids)


def start_stop(opts, rows, runner, *, log):
    scope = dict(tag.split("=", 1) for tag in opts.scope_tag)
    kh = opts.known_hosts
    return power.StartStop(
        Ec2(scope), scope, rows, at_once=opts.start_stopped, log=log, answers=lambda ip: True,
        sleep=lambda s: None, unread_reasons=idle_stop.UNREAD, halted_in=opts.halted_in or "",
        in_use=lambda row: admin_common.vm_in_use(runner, admin_common.host_of(row), kh),
    )


def ssh(argv, *, timeout_s=120.0, env=None, stdin=None):
    host, remote = argv[argv.index("--") - 1].removeprefix("ubuntu@"), argv[-1]
    if host not in UP:
        return RunResult(255, "", f"ssh: connect to host {host} port 22: Connection timed out")
    if remote == admin_common.IN_USE_CMD:
        return RunResult(0, json.dumps(UNUSED) + chr(10), "")
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
trust_fix.start_stop = start_stop
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
#: ... and the time its one try began, UTC (#1009): bob never answered, so no retry.
NOT_REACHED_AT = re.compile(re.escape(NOT_REACHED) + r" \(began \d\d:\d\d:\d\dZ\)$")


def trust_fix(site, *, down=(), gate=None, hang=()) -> None:
    state = site.tmp / "trust-fix-state.json"
    gated, ec2 = str(gate) if gate else "", str(site.tmp / "ec2.log")
    state.write_text(
        json.dumps({"down": list(down), "gate": gated, "hang": list(hang), "ec2": ec2})
    )
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
    assert "── admin/trust_fix.py\nstopped VMs: each started for its row, at most 6" in dry
    assert "\nparticipant  host " in dry  # the dry run's table, with no power column (#995)
    assert "power" not in dry.split("\nparticipant  host ", 1)[1].split("\n", 1)[0]
    for slug in ("alice", "bob"):  # carol has no VM: `all` is the rows with one
        found = row(dry, slug)
        assert "0.13.2: would install 0.13.7" in found and found.endswith("FAILED"), found
    assert "\n2 VM(s) reached (dry run: nothing changed)\n── admin/trust_fix.py exited 0" in dry
    assert "**The trust fix** (workshop/admin/README.md §10)" in text
    (argv,) = runs(site)  # the plan's run: no --apply, and as the bot runs it
    assert "--apply" not in argv and "--admin-bot" in argv
    scope = "--scope-tag workshop=harvard-2026 --scope-tag zab-scope=mathagents"
    assert " ".join(argv).endswith(
        f"--admin-bot --start-stopped 6 --region us-west-2 {scope} --only alice --only bob"
    )
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
        assert re.search(r"  ok +running$", found), found  # both running: left so (#995)
    assert "\n2 VM(s) fixed and checked\n── admin/trust_fix.py exited 0" in result
    # a reconciler: the apply runs no dry run again, only the apply itself
    assert ["--apply" in argv for argv in runs(site)] == [False, True]
    end = records(site, "apply_end")[-1]
    assert (end["verb"], end["targets"], end["exit"]) == ("trust-fix", ["alice", "bob"], 0)


async def test_a_stopped_row_is_started_fixed_and_stopped_again(site, server):
    """#995, as `!rollout` treats a stopped VM: the plan is offered, its status table says
    `stopped` and `start, then stop`, and the dry run's row `not reached` (a dry run starts
    nothing); the apply starts the VM for its fix, fixes it, and stops it again, nobody having
    used it; alice, running, is fixed and left running."""
    trust_fix(site, down=[BOB_IP])
    fake(site, "admin/status.py", STATUS.format(bob="stopped"))
    plan = await request(server, "!trust-fix all")
    assert plan.state == PENDING, plan.note
    text = posts(site)[-2]
    assert NOT_REACHED_AT.search(row(text, "bob")), row(text, "bob")
    assert "; not done: bob\n── admin/trust_fix.py exited 0" in text
    status = text.split("**Status**", 1)[1]
    assert re.search(r"^bob +stopped .* start, then stop$", status, re.M), status
    assert re.search(r"^alice +active .* left running$", status, re.M), status
    assert not (site.tmp / "ec2.log").exists()  # the plan started nothing
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    result = posts(site)[-1]
    assert result.startswith(f"✅ **{plan.name}** · `!trust-fix all`: done in "), result
    assert re.search(r"  ok +running$", row(result, "alice")), row(result, "alice")
    assert re.search(r"  ok +started, stopped again$", row(result, "bob")), row(result, "bob")
    assert (site.tmp / "ec2.log").read_text() == "start i-2\nstop i-2\n"
    assert "\npower: bob i-2: started for its row\n" in result
    assert "\nstopped VMs: 1 started, 1 stopped again\n" in result
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
    """An hour, or 2.5 minutes a row above 24: six VMs at a time, 12 minutes each at worst and a
    stopped one's start and stop (#995)."""
    every = Command("trust-fix", selection=Selection(all=True))
    assert build(every, FLEET).timeout == actions.TRUST_FIX_FLOOR_S == 3600
    rows = tuple(Row(f"p{n}", f"198.51.100.{n + 1}", "us-west-2") for n in range(41))
    many = build(every, dataclasses.replace(FLEET, rows=rows))
    assert len(many.targets) == 41 and many.timeout == 41 * actions.TRUST_FIX_ROW_S == 6150


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
