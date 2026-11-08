"""A20, a late joiner gets a spare with the waker on (docs/ADMIN.md §7; #456): `!adopt-spare`'s
whole chain (ADMIN §3, action 7; by hand, ORGANIZERS §7.2), the waker's step of #440 included, as
ORGANIZERS §3.5 and §4.1 have an organizer send it, through `Workshop admin · e2e`.

**The fleet first** (§3.5's launch day, without `lean-shared`): `!waker on all`, then
`!rollout <rc> all idle-stop=240`, on A6's `zab-test-a` and `zab-test-b`, so that the bot records
the fleet's waker and idle stop (`fleet.Ledger`: only a successful plan of `all` counts). 240
minutes, the longest timeout: no VM of the rig powers itself off during the run if the last part
can't take idle stop off again, and it isn't the product's 60, so the adopted VM's timer is
visibly the fleet's.

**A spare stops by itself** (ORGANIZERS §3.7; #715): a spare launched afresh (`!launch
zab-test-b spares=1`, as A9 does; one already there is torn down first, so that this one has its
first boot's warm-up), whose plan ends with `spare_idle_stop.py --minutes 10` (the rig's
admin.toml names no `spare_idle_stop`: its default) and whose result says it installed it. Then
nothing is sent to it, and EC2 shows it `stopped` by itself (`Client.InstanceInitiatedShutdown`)
within `SPARE_STOP_S` of the launch: its own idle stop, held through the warm-up, which the late
joiner's part reads on the VM afterwards.

**The late joiner:** that spare, stopped (or one launched when there is none), the row
`zab-test-d` imported with `zab-tester`'s address, and `!adopt-spare zab-test-d zab-spare-1`. Its
plan's dry run says `zab-spare-1: stopped, started for the adoption` (#715), and it lists the
seven steps in order, the waker's between `zulip_provision.py` (held, as the rig's fleet always
is) and the first `push_config.py`, both of which carry the fleet's release and `--idle-stop
240`; it applies with every step exiting 0, the spare started by the first. Then: the instance,
its volume and its Elastic IP are the row's, the roster row has the spare's address and is bound
to `zab-tester`, `spares.json` says who adopted it; the row's two bots exist, `zab-tester`'s,
with their keys in `out/`; the waker's config lists them with the instance and `zab-tester`'s ID,
beside the fleet's; `!status`'s table (#439) reads the release and idle stop with the fleet's
minutes, as status.py read them on the VM; on the VM, the idle stop is the row's, not the
spare's (its unit runs `--minutes 240`, without `--spare`), and the spare's log has its warm-up
hold and its power-off; and `Codex · zab-test-d` answers `zab-tester`'s `!status` DM: the VM runs
as the row's bots, for its participant.

**Fixed rows, as `zab-test-a` and `zab-test-b` are:** every run reuses `zab-test-d`'s two bots,
which nothing deactivates (no verb reactivates a bot, only its owner or an administrator, §7),
and its repository `MathAgents/zab-test-d`, which the chain's `provision_repos.py` creates on the
first run. The rig's GitHub token must be able to create it, or Drew creates it once. The last
part also clears `zab-test-e`, the row of the stopped spare's refusal before #715, which an
earlier run may have left.

**At most three instances run,** the server's included (§7): both VMs for the fleet's plans,
then `zab-test-a` stopped while a spare runs beside `zab-test-b`. Any other VM of the rig that runs
makes a part skip, naming it.

**The last part puts the fleet back as A9 finds it**, whatever the parts before got to: the rows'
VMs torn down (`!teardown … unsynced`) and their addresses released, a spare left torn down, idle
stop taken off `zab-test-a` and `zab-test-b` (which `zab-test-a` is started for, then stopped
again, as A8 leaves it), then `!waker off all` (ORGANIZERS §5.2: idle stop off, then the waker),
and the two rows taken out of the roster, as root on the server: no bot verb removes a row that
had a VM. The run's teardown terminates whatever is still left (`run.teardown`).

**What outlives a run** (the review of #457): the audit log, and with it the ledger, survives the
setup's reset of `ws/`. After A20 the ledger still records `idle-stop=240` as `zab-admin-e2e`'s
fleet setting, since `!idle-stop 0` names the two rows (ORGANIZERS §5.2) and only a plan of
`all` counts; a later `!adopt-spare` replays it unless its scenario sends its own rollout of
`all` first, as A20's first part does. The setup doesn't reset the waker's `config.toml` either:
a run killed before the last part leaves `zab-test-d`'s entries there until the next A20, as A8's
would be left.
"""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from typing import Any

import pytest

from e2e.admin.conftest import DONE, requires
from e2e.admin.harness import ending, start_here
from e2e.admin.harness.admin_toml import MENTION_AFTER_S
from e2e.admin.harness.chat import Talk
from e2e.admin.harness.gate import tags_of
from e2e.admin.harness.rig import E2E
from e2e.admin.harness.settle import until
from e2e.admin.test_scenarios_2_bot import (
    ROSTER_COLUMNS,
    client_of,
    email_of,
    import_roster,
    last_seq,
    on_vm,
    records,
    row,
    talk,
    vm_state,
    vm_states,
    wait_state,
)
from e2e.admin.test_scenarios_8_the_end import (
    WS,
    addresses_of,
    applied,
    bot_users,
    statuses,
    terminated,
)
from e2e.harness.flows import DM_STATUS_RE
from e2e.harness.zulip_user import TestUser, dm_narrow

A, B, SPARE = "zab-test-a", "zab-test-b", "zab-spare-1"
#: The late joiner, and the row of the stopped spare's refusal before #715, cleared if an earlier
#: run left it (fixed slugs: module doc).
D, E = "zab-test-d", "zab-test-e"
ROSTER = f"{WS}/roster.csv"
WAKER = "/var/lib/zab-admin/waker/config.toml"  # push_waker.py --config-dir's
#: The fleet's idle-stop timeout (module doc).
MINUTES = 240
KINDS = ("codex", "claude")
#: `!adopt-spare`'s steps with the fleet's waker on (ADMIN §3, action 7), as `runner.script`
#: names them in a result.
CHAIN = (
    "aws/adopt_spare.py", "admin/scan_hosts.py", "admin/zulip_provision.py",
    "admin/push_waker.py", "admin/push_config.py", "admin/provision_repos.py",
    "admin/push_config.py",
)  # fmt: skip
#: `adopt_spare.py`'s dry-run line for a stopped spare, which the plan shows (#715; ORGANIZERS
#: §3.7 and §4.1 quote it, `test_harness_late_joiner.py` renders it with the script).
STARTED = f"{SPARE}: stopped, started for the adoption"
#: A spare launched stops itself within this of its launch (#715): its first boot's warm-up
#: (about 15 minutes on the image), the load it leaves (the 5-minute average then decays below
#: 0.5), and its 10 minutes after the install's SSH session; with room.
SPARE_STOP_S = 90 * 60.0
#: EC2's reason for a stop from inside the VM (idle stop's `systemctl poweroff`).
OWN_STOP = "Client.InstanceInitiatedShutdown"
#: On the VM: the idle-stop unit, and a spare's log (`idle_stop.SPARE_LOG`).
IDLE_UNIT = "/etc/systemd/system/zab-idle-stop.service"
SPARE_LOG = "/var/log/zab-idle-stop-spare.log"
#: The spare's verdicts that its log must have: the warm-up's hold (one of a `stay up:` line's
#: reasons), and its power-off.
WARM_HOLD = "the first boot's volume warm-up is running"
SPARE_OFF = "power off: idle for 10 min"
#: What A20 changed, for its last part to undo: `waker`, `idle_stop`, `spare` (one launched),
#: `rows` (the slugs imported). Set before each change is approved.
MADE: dict[str, Any] = {}
#: As root on the server: the roster of `$1` (zab-admin-e2e only) without the rows named after
#: it (zab-test-d and zab-test-e only), every other row and line as it was, as
#: `aws_common.write_roster` writes them; atomically, through a temporary file made afresh
#: (`O_EXCL`, after removing any left there), with the file's owner and mode. Prints how many
#: rows went.
DROP_ROWS = r"""
import contextlib, csv, os, sys
ws, *slugs = sys.argv[1:]
if ws != "zab-admin-e2e" or not slugs or any(s not in ("zab-test-d", "zab-test-e") for s in slugs):
    sys.exit(2)
path = os.path.join("/var/lib/zab-admin/ws", ws, "roster.csv")
with open(path, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))
at = [h.strip().lower() for h in rows[0]].index("slug")
kept = rows[:1] + [r for r in rows[1:] if len(r) <= at or r[at].strip().lower() not in slugs]
st = os.stat(path)
tmp = path + ".a20"
with contextlib.suppress(FileNotFoundError):  # a run's leftover: a link is removed, not followed
    os.unlink(tmp)
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
    csv.writer(f).writerows(kept)
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, st.st_mode & 0o777)
os.replace(tmp, path)
print(len(rows) - len(kept))
"""


# ------------------------------------------------------------------------------ reading


def script_of(command: str) -> str:
    """The script a plan's command line runs, named as `runner.script` names it in a result
    (`admin/push_waker.py`)."""
    path = next((word for word in command.split() if word.endswith(".py")), "")
    return "/".join(path.split("/")[-2:])


def values(argv: list[str], flag: str) -> list[str]:
    """Every value given to `flag` in `argv` (`--only a --only b` → [a, b])."""
    return [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == flag]


def waker_bots(text: str, workshop: str = E2E.name) -> dict[str, dict[str, Any]]:
    """The bots the waker's config (`push_waker.render_shared`) lists for `workshop`, by name
    (`codex-<slug>`); none for a workshop it doesn't list."""
    config = tomllib.loads(text) if text.strip() else {}
    entry = next((w for w in config.get("workshop", []) if w.get("name") == workshop), {})
    return {str(bot.get("name")): bot for bot in entry.get("bots", [])}


def read_waker(server: Any) -> dict[str, dict[str, Any]]:
    """`waker_bots` of the server's config as it is now (none before the first `!waker on`)."""
    found = server.run(["sudo", "cat", "--", WAKER], check=False)
    return waker_bots(found.stdout) if found.ok else {}


def idle(vm: dict[str, str]) -> str | None:
    """A VM's idle stop in `!status`'s table (`chat.status_of`, #439), as status.py read it on
    the VM: `240 min` for the timer enabled and active with its minutes, `off` for none
    (`planning._idle`); None without the cell."""
    return vm.get("idle stop")


def minutes_on(minutes: int) -> str:
    """The `idle stop` cell of a VM whose timer runs with `minutes` (`planning._idle`)."""
    return f"{minutes} min"


def adopted_by(spares: dict[str, Any], name: str) -> str | None:
    """Who adopted the spare `name`, as `spares.json` records it (`adopt_spare.py`), or None."""
    for key, entry in spares.items():
        if isinstance(entry, dict) and (entry.get("name") or key.rsplit("/", 1)[-1]) == name:
            return entry.get("adopted_by")
    return None


def address_of(aws: Any, participant: str, instance: str) -> dict[str, Any] | None:
    found = [a for a in addresses_of(aws, participant) if a.get("InstanceId") == instance]
    return found[0] if found else None


def described(aws: Any, instance: str) -> dict[str, Any]:
    """`aws.describe(instance)`, or, on a transient error of the read, a state that names it:
    a 90-minute wait doesn't end on one failed request."""
    try:
        return aws.describe(instance)
    except Exception as exc:  # botocore's, or a LookupError while EC2's read lags
        return {"InstanceId": instance, "State": {"Name": f"unread ({type(exc).__name__})"}}


#: As zab-admin on the server: the last lines of a spare's idle-stop log, over SSH as ubuntu
#: with the fleet's key, the spare's key pinned to one its console printed (as
#: `spare_idle_stop.pinned` does; the spare isn't in the workshop's `known_hosts`).
SPARE_LOG_TAIL = r"""
import os, subprocess, sys, tempfile
sys.path.insert(0, "/opt/zab-admin/current/workshop/admin")
from admin_common import host_key_fingerprint, ssh_argv
ip, printed, log = sys.argv[1], sys.argv[2].split(), sys.argv[3]
scan = subprocess.run(["ssh-keyscan", "-T", "10", "-t", "ed25519", "--", ip],
                      capture_output=True, text=True, timeout=60)
line = next((" ".join(ln.split()[:3]) for ln in scan.stdout.splitlines()
             if ln.split()[:2] == [ip, "ssh-ed25519"]), "")
if not line or host_key_fingerprint(line) not in printed:
    sys.exit(f"no key of {ip} that its console printed: not read")
with tempfile.TemporaryDirectory() as folder:
    known = os.path.join(folder, "known_hosts")
    with open(known, "w") as f:
        f.write(line + "\n")
    done = subprocess.run(ssh_argv(ip, "tail -n 20 " + log, known_hosts=known),
                          capture_output=True, text=True, timeout=120)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""


def spare_log_tail(server: Any, ip: str | None) -> str:
    """The spare's idle-stop log's last lines (`SPARE_LOG_TAIL`), or why they can't be read:
    for a failure's message, never a failure of its own."""
    if not ip:
        return "(no address in spares.json)"
    try:
        spares = json.loads(server.read(f"{WS}/out/spares.json"))
        keys = next((e.get("host_keys") or "" for e in spares.values()
                     if isinstance(e, dict) and e.get("ip") == ip), "")  # fmt: skip
        done = server.as_admin("/usr/bin/python3", "-I", "-c", SPARE_LOG_TAIL, ip, keys,
                               SPARE_LOG, timeout=240)  # fmt: skip
    except Exception as exc:
        return f"(not read: {type(exc).__name__}: {exc})"
    why = done.stderr.strip()[-300:]
    return done.stdout.strip() or f"(not read: exit {done.returncode}: {why})"


def volume_owners(aws: Any, instance: str) -> list[str | None]:
    """The `participant` tag of each volume attached to `instance` (none: not shown yet)."""
    attached = [
        v
        for v in aws.volumes()
        if any(a.get("InstanceId") == instance for a in v.get("Attachments") or [])
    ]
    return [tags_of(v).get("participant") for v in attached]


# ------------------------------------------------------------------------------ doing


async def room_for(
    t: Talk, aws: Any, *, running: tuple[str, ...], stopped: tuple[str, ...] = (),
    beside: tuple[str, ...] = (),
) -> None:  # fmt: skip
    """`stopped` stopped, then `running` started and running: at most three instances run, the
    server's included (§7), so any other VM of the rig that runs, but `beside`, makes the part
    skip, naming it."""
    for slug in stopped:
        if vm_state(aws, slug) in ("pending", "running"):
            await wait_state(aws, slug, "running", within=300)  # a start ends first
            await t.run(f"!stop {slug}", phrase=False)
        if vm_state(aws, slug) is not None:
            await wait_state(aws, slug, "stopped")
    others = [p for p in ending.running(aws.fleet()) if p not in (*running, *beside)]
    if others:
        pytest.skip(f"at most three instances run (§7): {others} run besides {list(running)}")
    for slug in running:  # a stop still under way: EC2 refuses a start until it is done
        if vm_state(aws, slug) == "stopping":
            await wait_state(aws, slug, "stopped")
    down = [s for s in running if vm_state(aws, s) not in ("pending", "running")]
    if down:
        await t.run(f"!start {' '.join(down)}", phrase=False)
    for slug in running:
        await wait_state(aws, slug, "running")


async def launch_spare(t: Talk, aws: Any) -> tuple[dict[str, Any], str]:
    """`zab-spare-1` launched beside `zab-test-b` (`launch.py` waits for its SSH), its own idle
    stop the plan's step right after the launch (#715): (its instance as EC2 shows it, the
    result's text)."""
    MADE["spare"] = True
    plan, text = await t.run(f"!launch {B} spares=1", phrase=False)
    assert plan.targets == [B, SPARE], f"the launch plan's targets: {plan.targets}"
    scripts = [script_of(c) for c in plan.commands]
    assert scripts[:2] == ["aws/launch.py", "aws/spare_idle_stop.py"], plan.commands
    step = plan.commands[1].split()
    assert values(step, "--minutes") == ["10"] and values(step, "--only") == [SPARE], step
    inst = await until(lambda: vm_states(aws).get(SPARE), lambda i: bool(i),
                       what=f"{SPARE} in EC2", within=600, every=10)  # fmt: skip
    return inst, text


async def a_spare(t: Talk, aws: Any) -> dict[str, Any]:
    """`zab-spare-1`: the one there, once it is running or stopped (a stop under way ends
    first), or one launched (`launch_spare`). Its instance, as EC2 shows it."""
    if vm_states(aws).get(SPARE) is None:
        await launch_spare(t, aws)
    return await until(lambda: vm_states(aws).get(SPARE),
                       lambda inst: bool(inst) and inst["State"]["Name"] in ("running", "stopped"),
                       what=f"{SPARE} running or stopped", within=600, every=10)  # fmt: skip


async def add_row(t: Talk, server: Any, new: dict[str, str]) -> None:
    """`new` added to the roster by `!roster import`, every row it has going in the upload as
    it is (A9's throwaway row), so that nothing else changes; nothing when it is there."""
    rows = ending.inputs(server.read(ROSTER), ROSTER_COLUMNS)
    if new["slug"] in {r["slug"] for r in rows}:
        return
    MADE.setdefault("rows", set()).add(new["slug"])
    plan = await import_roster(t, [*rows, new], f"{new['slug']}.csv")
    assert "roster import: 1 added, 0 changed" in plan.text, plan.text[-400:]
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]


# ----------------------------------------------------------------------- the fleet first


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A20")
@requires("launched", "provisioned", "rolled_out")
async def test_a20_the_fleet_gets_the_waker_then_idle_stop(
    scenario, tester, rigged, server, aws, nonce
):
    t = talk(tester, rigged, E2E.name, f"e2e-A20-fleet-{nonce}")
    await room_for(t, aws, running=(A, B))
    MADE["waker"] = True  # the last part sends `!waker off all`, whatever happens here
    plan, _ = await t.run("!waker on all", phrase=False)
    assert plan.targets == [A, B], f"the waker plan's targets: {plan.targets}"
    ids = {slug: vm_states(aws)[slug]["InstanceId"] for slug in (A, B)}
    bots = read_waker(server)
    listed = {s: {bots.get(f"{k}-{s}", {}).get("instance_id") for k in KINDS} for s in (A, B)}
    assert listed == {s: {ids[s]} for s in (A, B)}, f"the waker's config: {listed}, EC2: {ids}"
    await statuses(t, [A, B])  # reachable: the rollout connects to both
    plan = await t.request(f"!rollout {rigged.release} all idle-stop={MINUTES}")
    assert plan.targets == [A, B] and plan.phrase == "rollout 2", (plan.targets, plan.phrase)
    pushes = [c.split() for c in plan.commands if script_of(c) == "admin/push_config.py"]
    assert pushes and all(values(p, "--idle-stop") == [str(MINUTES)] for p in pushes), pushes
    MADE["idle_stop"] = True
    started = time.monotonic()
    _, text = await t.approve(plan)
    scenario.timing("rollout of 2 VMs with idle stop", time.monotonic() - started)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    vms = await statuses(t, [A, B])
    shown = {slug: idle(vms[slug]) for slug in (A, B)}
    assert shown == {s: minutes_on(MINUTES) for s in (A, B)}, f"idle stop on the VMs: {shown}"
    DONE["waker_then_idle_stop"] = MINUTES  # what !adopt-spare must give a new VM now
    scenario.notes.append(f"the fleet: the waker on, then idle stop after {MINUTES} min")


# ------------------------------------------------------------- a spare stops by itself


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A20")
@requires("waker_then_idle_stop")
async def test_a20_a_spare_stops_by_itself_after_its_warm_up(
    scenario, tester, rigged, server, aws, admin_rig, nonce
):
    """ORGANIZERS §3.7 (#715): a spare launched gets its own idle stop at 10 minutes, and stops
    by itself, nobody sending it anything, within `SPARE_STOP_S`; its log on the VM, read once
    it is adopted (the next part), shows the warm-up's hold first."""
    t = talk(tester, rigged, E2E.name, f"e2e-A20-spare-{nonce}")
    await room_for(t, aws, running=(B,), stopped=(A,), beside=(SPARE,))
    if vm_states(aws).get(SPARE) is not None:  # an earlier run's: its warm-up is long done
        await end_spare(t, aws)
    started = time.monotonic()
    spare, text = await launch_spare(t, aws)
    iid = spare["InstanceId"]
    installed = f"{SPARE} {iid}: idle stop after 10 min (a spare's), installed"
    assert installed in text.split("\n"), f"the launch's result: {text[-600:]}"
    since = last_seq(server)
    try:
        stopped = await until(lambda: described(aws, iid),
                              lambda i: i["State"]["Name"] == "stopped",
                              what=f"{SPARE} stopped by itself", within=SPARE_STOP_S,
                              every=30)  # fmt: skip
    except AssertionError as exc:  # why it stayed up, from its own idle stop's verdicts
        spare_ip = ending.spare_ip(json.loads(server.read(f"{WS}/out/spares.json")), SPARE)
        pytest.fail(f"{exc}\n{SPARE_LOG} on the spare:\n{spare_log_tail(server, spare_ip)}")
    took = time.monotonic() - started
    scenario.timing("a spare's launch to its own stop", took)
    reason = (stopped.get("StateReason") or {}).get("Code")
    assert reason == OWN_STOP, f"{SPARE} stopped, but not by itself: {stopped.get('StateReason')}"
    planned = [r for r in records(server, since) if r.get("event") == "apply_start"]
    assert not any(SPARE in (r.get("targets") or []) for r in planned), planned  # nobody did
    DONE["spare_stopped"] = iid
    scenario.notes.append(f"{SPARE} ({iid}) stopped by itself {took / 60:.0f} min after its launch")


# ---------------------------------------------------------------------- the late joiner


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A20")
@requires("waker_then_idle_stop")
async def test_a20_a_late_joiner_gets_a_spare_with_the_waker(
    scenario, tester, rigged, server, aws, admin_rig, nonce
):
    if not rigged.github_token:
        pytest.skip("Q9: the chain's provision_repos.py needs the test server's GitHub token")
    minutes = DONE["waker_then_idle_stop"]
    t = talk(tester, rigged, E2E.name, f"e2e-A20-adopt-{nonce}")
    await room_for(t, aws, running=(B,), stopped=(A,), beside=(SPARE,))
    spare = await a_spare(t, aws)
    iid, was = spare["InstanceId"], spare["State"]["Name"]
    eip = await until(lambda: address_of(aws, SPARE, iid), bool, what=f"{SPARE}'s Elastic IP")
    ids = rigged.ids()
    await add_row(t, server, row(D, await email_of(tester), admin_rig.region))
    plan = await t.request(f"!adopt-spare {D} {SPARE}")
    assert plan.targets == [D, SPARE] and not plan.phrase, (plan.targets, plan.phrase)
    if was == "stopped":  # #715: started for the adoption, as the plan says
        assert STARTED in plan.text.split("\n"), f"the plan's dry run: {plan.text[-800:]}"
    steps = [c.split() for c in plan.commands]
    assert [script_of(c) for c in plan.commands] == list(CHAIN), plan.commands
    assert "--hold" in steps[2], "zulip_provision.py without --hold: the rig's fleet is held"
    # the workshop's file server and gift step, as `!provision zulip` passes them (#709)
    assert not start_here.flag_problems(plan.commands[2]), start_here.flag_problems(
        plan.commands[2]
    )
    assert values(steps[3], "--only") == [D] and "--apply" in steps[3], steps[3]
    wheel = f"zulip_agent_bridge-{rigged.release[1:]}-py3-none-any.whl"
    for push in (steps[4], steps[6]):
        assert values(push, "--idle-stop") == [str(minutes)], push
        assert values(push, "--mention-after-s") == [str(MENTION_AFTER_S)], push  # #711
        assert any(word.endswith(f"/{wheel}") for word in values(push, "--wheel")), push
    assert values(steps[4], "--adopt-spare") == [D], steps[4]
    since = last_seq(server)
    started = time.monotonic()
    _, text = await t.approve(plan)
    scenario.timing("adopt-spare, approval to result", time.monotonic() - started)
    if "provision_repos.py" in (first := text.split("\n", 1)[0]):
        pytest.fail(f"{first}: the rig's GitHub token must be able to create MathAgents/{D}, "
                    "or the repository exist and be in it (module doc)")  # fmt: skip
    applied(server, since, plan, text, [D, SPARE])

    # the spare is the row's: its instance, address and volume, the roster and spares.json
    inst = await until(lambda: aws.describe(iid), lambda i: tags_of(i).get("participant") == D,
                       what=f"{iid} retagged participant={D}")  # fmt: skip
    tags = tags_of(inst)
    assert (tags.get("Name"), tags.get("workshop")) == (f"ws-{D}", E2E.name), tags
    await until(lambda: address_of(aws, D, iid), bool, what=f"the Elastic IP retagged {D}")
    await until(lambda: volume_owners(aws, iid), lambda found: bool(found) and set(found) == {D},
                what=f"its volume retagged {D}")  # fmt: skip
    await until(lambda: vm_states(aws), lambda found: SPARE not in found,
                what=f"no instance tagged participant={SPARE} any more")  # fmt: skip
    mine = ending.rows_by_slug(server.read(ROSTER))[D]
    shown = (mine.get("ip"), mine.get("host"), mine.get("zulip_user_id"))
    assert shown == (eip["PublicIp"], f"ws-{D}", str(ids.tester)), f"{D}'s row: {shown}"
    assert adopted_by(json.loads(server.read(f"{WS}/out/spares.json")), SPARE) == D

    # the row's bots, held as the fleet is (the plan's --hold), zab-tester's
    names = [f"{kind} · {D}" for kind in ("Claude", "Codex")]
    made = await bot_users(tester, names)
    assert sorted(made) == sorted(names), f"{D}'s bots: {sorted(made)}"
    for name, user in made.items():
        owned = (user.get("is_active"), user.get("is_bot"), user.get("bot_owner_id"))
        assert owned == (True, True, ids.tester), f"{name}: {owned}"
    files = server.run(["sudo", "ls", "--", f"{WS}/out/{D}"]).stdout.split()
    assert {"claude.zuliprc", "codex.zuliprc"} <= set(files), files
    owned = {b["full_name"]: b for b in (await tester.client.request("GET", "bots"))["bots"]}
    for name in names:  # each in the row's sandbox, as A5 checks zab-test-a's and zab-test-b's
        bot = TestUser(client_of(tester, owned[name]))
        try:
            subs = (await bot.client.request("GET", "users/me/subscriptions"))["subscriptions"]
        finally:
            await bot.close()
        assert any(sub["name"] == f"{D}-sandbox" for sub in subs), f"{name}: no {D}-sandbox"

    # the waker: the row beside the fleet's, with its instance and its participant's ID
    bots = read_waker(server)
    for kind in KINDS:
        entry = bots.get(f"{kind}-{D}") or {}
        found = (entry.get("instance_id"), entry.get("allow_user_ids"))
        assert found == (iid, [ids.tester]), f"the waker's {kind}-{D}: {found}"
    fleet = {f"{kind}-{slug}" for kind in KINDS for slug in (A, B)}
    assert fleet <= set(bots), f"the fleet's entries are gone: {sorted(fleet - set(bots))}"

    # on the VM: the fleet's release, and its idle stop, the row's and no longer the spare's
    vm = (await statuses(t, [D]))[D]
    assert vm.get("zab") == rigged.release[1:], f"{D} runs {vm.get('zab')}"
    assert idle(vm) == minutes_on(minutes), f"{D}'s idle stop: {idle(vm)}"
    unit = on_vm(server, eip["PublicIp"], "cat", IDLE_UNIT)
    assert unit.ok and f"--minutes {minutes}\n" in unit.stdout and "--spare" not in unit.stdout, (
        f"{D}'s idle-stop unit: {unit.stdout[-300:]}"
    )
    if DONE.get("spare_stopped") == iid:  # the spare of the part before: its whole first boot
        log = on_vm(server, eip["PublicIp"], "cat", SPARE_LOG)
        assert log.ok, f"{SPARE_LOG}: exit {log.returncode}: {log.stderr.strip()[-200:]}"
        lines = log.stdout.splitlines()
        held = [n for n, ln in enumerate(lines) if " stay up: " in ln and WARM_HOLD in ln]
        off = [n for n, ln in enumerate(lines) if SPARE_OFF in ln]
        assert held and off and held[0] < off[0], f"{SPARE_LOG}: {lines[-20:]}"

    # the VM answers as the row's bot, for its participant
    codex = int(made[f"Codex · {D}"]["user_id"])
    tester.restrict(channels=tester.channels, bots=[*tester.bots, codex])
    sent = await tester.send_dm(codex, "!status")
    await tester.wait_reply(dm_narrow(codex), after_id=sent, senders=[codex],
                            pattern=DM_STATUS_RE, within=300)  # fmt: skip
    DONE["late_joiner"] = D
    scenario.notes.append(f"{D} on {SPARE} ({iid}, {was}): every step ✅, the waker, then idle "
                          f"stop after {minutes} min, the spare's gone; Codex · {D} answers its "
                          "DM")  # fmt: skip


# --------------------------------------------------------------- the fleet as A9 finds it


async def end_late_rows(t: Talk, server: Any, aws: Any) -> list[str]:
    """`!teardown … unsynced` of A20's rows that have a VM, in the roster or in EC2; then each
    instance terminated and its address released. The rows torn down."""
    roster = ending.rows_by_slug(server.read(ROSTER))
    vms = vm_states(aws)
    slugs = [s for s in roster if s in (D, E) and (roster[s].get("ip") or s in vms)]
    if not slugs:
        return []
    iids = {s: vms[s]["InstanceId"] for s in slugs if s in vms}
    plan, _ = await ending.after_the_guard(t, f"!teardown {' '.join(slugs)} unsynced")
    assert plan.targets == slugs, f"the teardown plan's targets: {plan.targets}"
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    for slug, iid in iids.items():
        await terminated(aws, iid)
        await until(lambda slug=slug: addresses_of(aws, slug), lambda found: not found,
                    what=f"{slug}'s Elastic IP released")  # fmt: skip
    return slugs


async def end_spare(t: Talk, aws: Any) -> bool:
    """A spare A20 launched and left, torn down: A9 launches its own."""
    spare = vm_states(aws).get(SPARE)
    if spare is None:
        return False
    plan, _ = await ending.after_the_guard(t, "!teardown spares")
    assert plan.targets == [SPARE], f"the teardown plan's targets: {plan.targets}"
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    await terminated(aws, spare["InstanceId"])
    await until(lambda: addresses_of(aws, SPARE), lambda found: not found,
                what=f"{SPARE}'s Elastic IP released")  # fmt: skip
    return True


async def idle_stop_off(t: Talk, aws: Any) -> None:
    """Idle stop off on both VMs, which must run for it; `zab-test-a` stopped again after, as
    A8 leaves it."""
    others = [p for p in ending.running(aws.fleet()) if p not in (A, B)]
    assert not others, f"{others} still run: at most three instances (§7), so A isn't started"
    for slug in (A, B):
        if vm_state(aws, slug) == "stopping":
            await wait_state(aws, slug, "stopped")
    down = [s for s in (A, B) if vm_state(aws, s) not in ("pending", "running")]
    if down:
        await t.run(f"!start {' '.join(down)}", phrase=False)
    for slug in (A, B):
        await wait_state(aws, slug, "running")
    await statuses(t, [A, B])  # reachable: `!idle-stop` connects to both
    await t.run(f"!idle-stop 0 {A} {B}")
    vms = await statuses(t, [A, B])
    shown = {slug: idle(vms[slug]) for slug in (A, B)}
    assert shown == {s: "off" for s in (A, B)}, f"idle stop still on: {shown}"
    await t.run(f"!stop {A}", phrase=False)
    await wait_state(aws, A, "stopped")


async def waker_off(t: Talk, server: Any) -> None:
    """`!waker off all`: every row, VM or not (`push_waker.py --remove` makes no AWS call)."""
    await t.run("!waker off all", phrase=False)
    left = sorted(n for n in read_waker(server) if n.split("-", 1)[-1] in (A, B, D, E))
    assert not left, f"the waker's config still lists {left}"


def drop_rows(server: Any, aws: Any) -> list[str]:
    """A20's rows out of the roster, as root (`DROP_ROWS`): only once neither has a VM in EC2."""
    slugs = [s for s in ending.rows_by_slug(server.read(ROSTER)) if s in (D, E)]
    if not slugs:
        return []
    alive = [s for s in slugs if s in vm_states(aws)]
    assert not alive, f"{alive} still have a VM: their rows stay in the roster"
    server.run(["sudo", "python3", "-I", "-c", DROP_ROWS, E2E.name, *slugs])
    left = set(ending.rows_by_slug(server.read(ROSTER))) & set(slugs)
    assert not left, f"still in the roster: {sorted(left)}"
    return slugs


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A20")
async def test_a20_the_fleet_as_a9_finds_it(scenario, tester, rigged, server, aws, nonce):
    """Whatever the parts before got to (module doc): each step is tried whatever the others
    do, and what fails is this part's failure."""
    if not MADE:
        scenario.notes.append("A20 changed nothing: nothing to put back")
        return
    t = talk(tester, rigged, E2E.name, f"e2e-A20-after-{nonce}")
    problems: list[str] = []

    async def attempt(what: str, step: Any) -> Any:
        try:
            done = step()
            return await done if asyncio.iscoroutine(done) else done
        except (Exception, pytest.fail.Exception) as exc:  # `statuses` fails with pytest.fail
            problems.append(f"{what}: {type(exc).__name__}: {str(exc)[:300]}")
            return None

    if ended := await attempt("the rows' VMs", lambda: end_late_rows(t, server, aws)):
        scenario.notes.append(f"{', '.join(ended)}: torn down")
    if MADE.get("spare") and await attempt("the spare", lambda: end_spare(t, aws)):
        scenario.notes.append(f"{SPARE}: torn down")
    if MADE.get("idle_stop"):
        await attempt("idle stop off", lambda: idle_stop_off(t, aws))
    if MADE.get("waker"):
        await attempt("the waker off", lambda: waker_off(t, server))
    if dropped := await attempt("the rows", lambda: drop_rows(server, aws)):
        scenario.notes.append(f"{', '.join(dropped)}: out of the roster")
    if not problems:
        MADE.clear()
    assert not problems, "; ".join(problems)
