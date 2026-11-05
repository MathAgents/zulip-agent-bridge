"""A18: a VM launched without the server's key joins a second workshop (docs/ADMIN.md §7;
#356, #358), the kept fleet's move onto the admin server (its runbook's O4 to O6 and C3, ADMIN
§8.4 at v0.2.15), rehearsed on `zab-admin-e2e2`.

the development machine plays Drew's laptop (`harness/kept.Laptop`). **Setup:** the bots as
they are after O6's move, the test provisioner's (`zab-tester`), by `zulip_provision.py --hold`
with zab-tester's key; `launch.py` with the rig's key pair `zab-test` (the development machine's
key, the break-glass key here) and the second workshop's scope; `scan_hosts.py`; and, a stand-in
until Q9, an empty git repository as the project checkout that `zab setup-repo` left on the kept
VMs (`kept.STANDIN`: the real one needs the MathAgents token, `provision_repos.py`). **Then:**
O4, the stage (on the development machine, then on the server; first a stage that also holds a
slug of `zab-admin-e2e`'s roster, refused); O5, `join_server.py` with `!status` before and
after; a rollout; O6, `move_bots.py`'s dry run (VERIFY V15) and the provisioner's dry run; C3,
`!waker on`, a wake and `!waker off`; the fallback's `join_server.py --remove`. At the end the
bots are deactivated. The run's teardown terminates the VM, as every test workshop's instance,
and checks it (`run.teardown`).

It runs after A9, or A7's stop, so that at most three instances run (§7): with more of the
rig's VMs running, the first part skips, naming them, and the others with it. A previous run's
bots, deactivated at its end, are first reactivated by their owner, zab-tester
(`kept.restore_bots`), and a sandbox zab-tester can't see is renamed out of its way
(`kept.restore_sandbox`).

**Needs an organization administrator's key (#758), and is not run:** `move_bots.py --apply`,
O6's move. Since Nov 2, Zulip Cloud lets only an organization administrator change a bot's owner,
the bot's owner included, and the rig's test users are a Moderator and a Member, by Drew's
decision. So A18 starts from the bots the move would leave, runs the move's dry run with
zab-tester2's key, which must say that the key isn't an administrator's, and names the step in
its notes. The fallback's `move_bots.py --to me` needs one too. `tests/workshop/test_kept_fleet.py`
covers both offline, against a fake realm that refuses as Zulip Cloud does.

**While Q9 is open,** every part runs but the move's dry run and the provisioner's: they need the
test provisioner's zuliprc, `zab-tester`'s, placed only after Drew's yes.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import time
from typing import Any
from urllib.parse import quote

import pytest

from e2e.admin.conftest import DONE, note_ssh, requires
from e2e.admin.harness import kept
from e2e.admin.harness.chat import (
    ANSWER,
    PLAN_S,
    STATUS_ANSWER,
    Talk,
    parse_plan,
    status_of,
    whole,
)
from e2e.admin.harness.gate import tags_of
from e2e.admin.harness.kept import KINDS, SLUG, TRANSITION_S, Laptop, row_of, table
from e2e.admin.harness.rig import E2E, E2E2
from e2e.admin.harness.settle import until
from e2e.admin.test_scenarios_2_bot import (
    PROVISIONER_Q9,
    email_of,
    last_seq,
    talk,
    wake_check,
    waker_on,
)
from e2e.harness.flows import DM_STATUS_RE
from e2e.harness.zulip_user import dm_narrow

WS = "/var/lib/zab-admin/ws"
WAKER = "/var/lib/zab-admin/waker/config.toml"  # push_waker.py --config-dir's


@pytest.fixture(scope="module")
def laptop(admin_rig):
    """§8.4's laptop on the development machine: a private directory in the rig's, with the roster
    and the bots' keys (`kept.laptop_dir`), removed after the module, whatever ended it."""
    with kept.laptop_dir(admin_rig) as path:
        yield Laptop(path, admin_rig)


#: The rig's other VMs that may run during A18: with the server and zab-test-c, three (§7).
OTHERS = 1


def crowded(aws) -> list[str]:
    """The rig's other VMs that run (or start), when more than `OTHERS` do, else []."""
    running = sorted(tags_of(i).get("participant") or i["InstanceId"] for i in aws.fleet()
                     if i["State"]["Name"] in ("pending", "running"))  # fmt: skip
    return running if len(running) > OTHERS else []


def vm_of(aws) -> dict[str, Any] | None:
    mine = (E2E2.name, SLUG)
    found = [i for i in aws.participants()
             if (tags_of(i).get("workshop"), tags_of(i).get("participant")) == mine]  # fmt: skip
    assert len(found) <= 1, f"two instances of {SLUG}: {[i['InstanceId'] for i in found]}"
    return found[0] if found else None


def state(aws) -> str:
    vm = vm_of(aws)
    return vm["State"]["Name"] if vm else "absent"


async def in_state(aws, wanted: str) -> None:
    """Until EC2 shows zab-test-c `wanted`, after a stop, a start or a wake (`settle`)."""
    await until(lambda: state(aws), lambda s: s == wanted, what=f"{SLUG} {wanted}",
                within=TRANSITION_S, every=10)  # fmt: skip


async def status(t: Talk) -> dict[str, Any]:
    """`!status zab-test-c` through the second workshop's bot: its cells in the answer's table,
    and its last error (`state`, …, `last error`; #439)."""
    answer = await t.ask(f"!status {SLUG}", STATUS_ANSWER, within=PLAN_S)
    assert not answer["content"].startswith("❌"), answer["content"][:300]
    return status_of(await whole(t.user, answer))[SLUG]


def joined(laptop: Laptop, **flags: bool) -> str:
    """join_server.py's result for the kept row (`ok: added`, `would remove 1`, `FAILED: …`),
    the only row it may touch: the torn-down one isn't in keep.txt."""
    done = laptop.join(**flags)
    touched = [r["slug"] for r in table(done.stdout, "slug")]
    assert touched == [SLUG], f"join_server.py --keep reached {touched}: {kept.tail(done)}"
    return row_of(done, "slug")["result"]


async def bots_now(user, emails: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The row's bots as Zulip has them now, by kind (`GET /users/{email}`): no key."""
    found = {}
    for kind, email in emails.items():
        found[kind] = (await user.client.request("GET", f"users/{quote(email, safe='@')}"))["user"]
    return found


def slugs_on(server, workshop: str) -> list[str]:
    """The slugs of a workshop's roster on the server."""
    text = server.read(f"{WS}/{workshop}/roster.csv")
    return [r["slug"] for r in csv.DictReader(io.StringIO(text)) if r.get("slug")]


def bound(server) -> bool:
    """Whether the server's row of zab-test-c has a `zulip_user_id`, which the monitor needs."""
    text = server.read(f"{WS}/{E2E2.name}/roster.csv")
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("slug") == SLUG]
    return bool(rows and (rows[0].get("zulip_user_id") or "").strip())


def opted_in(server) -> bool:
    """Whether the waker's config names zab-test-c (exit 1 of grep: it doesn't)."""
    found = server.run(["sudo", "grep", "-qF", "-e", SLUG, "--", WAKER], check=False)
    assert found.returncode in (0, 1), f"grep {WAKER}: exit {found.returncode}"
    return found.returncode == 0


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
async def test_a18_a_vm_launched_without_the_servers_key(
    scenario, admin_rig, rigged, aws, tester, tester2, laptop
):
    if running := crowded(aws):
        pytest.skip(f"A18 runs after A9 or A7's stop: at most three instances (§7); {running} run")
    ids = rigged.ids()
    emails = kept.bot_emails(ids.bots[E2E2.bot][1].rpartition("@")[2])
    scenario.notes.extend(await kept.restore_bots(tester.client, ids, emails))
    scenario.notes.append(await kept.restore_sandbox(tester.client, tester2.client))
    laptop.write_roster(await email_of(tester2), admin_rig.region)
    # the test provisioner's key: no test user may give it the bots (#758)
    done = laptop.provision(admin_rig.tester_zuliprc, "--hold")
    bots = row_of(done, "participant")["bots"]
    held = re.fullmatch(r"codex: (created|reused), claude: (created|reused)", bots)
    assert done.ok and held, kept.tail(done)
    made = {kind: laptop.bot_email(kind) for kind in KINDS}
    DONE["kept_bots"] = made
    assert made == emails, f"the next run's restore_bots looks for {emails}, not {made}"
    owners = {k: u.get("bot_owner_id") for k, u in (await bots_now(tester2, made)).items()}
    assert owners == dict.fromkeys(KINDS, ids.tester), f"the bots' owners: {owners}"
    scenario.notes.append(f"held with zab-tester's key, who owns the bots, as after O6: {bots}")
    # the server's address, and the development machine's
    sources = [f"{rigged.ip}/32", *aws.ssh_sources(aws.vpc())]
    started = time.monotonic()
    done = laptop.launch(ami=rigged.ami, group=rigged.groups[E2E2.name], ssh_from=sources)
    scenario.timing("launch.py from the development machine", time.monotonic() - started)
    assert done.ok, kept.tail(done)
    vm = await until(lambda: vm_of(aws), lambda v: v is not None, what=f"{SLUG}'s instance")
    tags = tags_of(vm)
    assert {k: tags.get(k) for k in E2E2.tags} == E2E2.tags and "zab-admin" not in tags, tags
    assert vm.get("KeyName") == admin_rig.key_pair, f"its key pair: {vm.get('KeyName')}"
    scan = row_of(laptop.scan(), "participant")
    assert (scan["key"], scan["console"]) == ("added", "verified"), scan
    # a kept VM has its project checkout (Oct 9's `zab setup-repo`), which the rollout's doctor
    # checks; the real one needs the MathAgents token (Q9), so an empty repository stands in
    done = laptop.stand_in_checkout(log=note_ssh)  # its connect failures: A18's notes
    checkout = done.stdout.strip()
    assert done.ok and checkout in ("initialized", "exists"), kept.tail(done)
    scenario.notes.append(
        f"STAND-IN until Q9: ~agent/work/{SLUG} is an empty git repository ({checkout}), not the "
        "checkout of MathAgents/zab-test-c that provision_repos.py makes once the token is placed"
    )
    DONE["kept_launched"] = vm["InstanceId"]


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("roster", "kept_launched")
def test_a18_a_slug_of_another_workshop_is_refused(scenario, server, laptop):
    """§2.1, O4 step 3: nothing lands in ws/ when a staged slug is another workshop's."""
    theirs = slugs_on(server, E2E.name)
    assert theirs, f"{E2E.name}'s roster on the server has no row"
    server.script(kept.FRESH, E2E2.name, sudo=True)
    before = server.run(["sudo", "ls", "-1A", "--", WS]).stdout.split()
    stage, refused = laptop.dir / "stage", laptop.dir / "stage-refused"
    try:
        assert (done := laptop.stage(stage)).ok, kept.tail(done)
        kept.refused_stage(stage, refused, theirs[0])
        done = server.script(kept.STAGE, E2E2.name, sudo=True, input=kept.stage_tar(refused),
                             check=False)  # fmt: skip
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(refused, ignore_errors=True)
    assert done.returncode == 2 and f"{theirs[0]} is in {E2E.name}" in done.stderr, kept.tail(done)
    after = server.run(["sudo", "ls", "-1A", "--", WS]).stdout.split()
    assert after == before and E2E2.name not in after, f"ws/ was {before}, is {after}"
    scenario.notes.append(f"a stage with {theirs[0]}: refused, nothing in ws/")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_launched")
def test_a18_the_stage_lands_on_the_server(scenario, server, laptop):
    """O4: the row, its bots' keys and known_hosts staged on the development machine, then on the
    server."""
    server.script(kept.FRESH, E2E2.name, sudo=True)
    stage = laptop.dir / "stage"
    try:
        done = laptop.stage(stage)  # the kept row alone: the torn-down one isn't in keep.txt
        assert done.ok and "staged 1 rows, 2 bot keys" in done.stdout, kept.tail(done)
        done = server.script(kept.STAGE, E2E2.name, sudo=True, input=kept.stage_tar(stage),
                             check=False)  # fmt: skip
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    assert done.ok and "staged 1 rows, 2 bot keys and known_hosts" in done.stdout, kept.tail(done)
    assert slugs_on(server, E2E2.name) == [SLUG], "the server's roster"
    for kind in KINDS:
        path = f"{WS}/{E2E2.name}/out/{SLUG}/{kind}.zuliprc"
        assert server.run(["sudo", "test", "-f", path], check=False).ok, f"no {path}"
    key = server.read("/var/lib/zab-admin/.ssh/id_ed25519.pub").strip()
    assert key.startswith("ssh-ed25519 "), "the server's public key"
    laptop.server_key.write_text(key + "\n", encoding="utf-8")
    DONE["kept_staged"] = True


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_staged")
async def test_a18_the_vm_joins_over_the_break_glass_key(scenario, tester2, rigged, laptop, nonce):
    t2 = talk(tester2, rigged, E2E2.name, f"e2e-A18-{nonce}")
    before = await status(t2)
    assert before.get("state") == "unreachable", f"before the join: {before}"
    scenario.notes.append(f"before the join: unreachable ({before.get('last error')})")
    results = [joined(laptop, dry_run=True), joined(laptop), joined(laptop)]
    assert results == ["would add", "ok: added", "ok: already there"], results
    after = await status(t2)
    assert after.get("state") not in (None, "?", "unreachable"), f"after the join: {after}"
    scenario.notes.append(f"after the join: {after.get('state')}")
    DONE["kept_joined"] = True


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_joined")
async def test_a18_a_rollout_through_the_bot(scenario, tester2, rigged, nonce):
    t2 = talk(tester2, rigged, E2E2.name, f"e2e-A18-{nonce}")
    started = time.monotonic()
    _, text = await t2.run(f"!rollout {rigged.release} {SLUG}")
    scenario.timing("rollout of 1 VM", time.monotonic() - started)
    assert re.search(rf"{SLUG}\b[^\n]*ok \(sign-in pending\)", text), text[-500:]
    DONE["kept_rolled_out"] = True


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_bots")
async def test_a18_the_bots_move_to_the_provisioner(
    scenario, admin_rig, rigged, tester, tester2, laptop
):
    """O6, as far as the rig's keys go: the move's dry run with zab-tester2's key, a Member's,
    as the runbook's "How to tell it worked" expected it after the move (`ok: owned already`),
    the kept row's bots only (`--keep`), and saying that the key isn't an administrator's. The
    move itself, `--apply`, **needs an organization administrator's key** (#758), and the rig
    has none: it is named in the notes, and covered offline (`tests/workshop/test_kept_fleet.py`).
    Then the provisioner's dry run reuses the bots."""
    if not rigged.provisioner:
        pytest.skip(PROVISIONER_Q9)
    ids, emails = rigged.ids(), DONE["kept_bots"]
    name = str((await tester.me())["full_name"])
    rc, provisioner = admin_rig.tester2_zuliprc, admin_rig.provisioner_zuliprc
    done = laptop.move(rc, provisioner, name, ids.tester, apply=False)
    head = done.stdout.split("\n", 1)[0]
    assert done.ok and f"to {name} <" in head and f"({ids.tester}, " in head, kept.tail(done)
    assert head.startswith("DRY RUN: "), head
    who = (await tester2.me())["full_name"]
    said = f"NOT AN ADMINISTRATOR'S KEY: {who} ({ids.tester2}) is a "
    lines = [line for line in done.stdout.splitlines() if line.startswith(said)]
    assert len(lines) == 1 and "only an organization administrator" in lines[0], kept.tail(done)
    rows = table(done.stdout, "slug")
    assert {r["slug"] for r in rows} == {SLUG}, f"move_bots.py --keep reached {rows}"
    moved = {r["bot"]: r["result"] for r in rows}
    assert moved == dict.fromkeys(KINDS, "ok: owned already"), moved
    owners = {k: u.get("bot_owner_id") for k, u in (await bots_now(tester2, emails)).items()}
    assert owners == dict.fromkeys(KINDS, ids.tester), f"after the dry run: {owners}"
    scenario.notes.append(
        f"the move's dry run with zab-tester2's key: ok: owned already, by {name} "
        f"({ids.tester}); the key is said not to be an administrator's"
    )
    scenario.notes.append(
        "NEEDS AN ORGANIZATION ADMINISTRATOR'S KEY, not run: move_bots.py --apply (O6). Zulip "
        "Cloud refuses a bot's owner change to anyone else since Nov 2 (#758), and no test user "
        "is one; covered offline (tests/workshop/test_kept_fleet.py)"
    )
    dry = row_of(laptop.provision(provisioner, "--dry-run"), "participant")
    assert dry["bots"] == "codex: reused, claude: reused", dry
    scenario.notes.append(f"the provisioner's dry run: {dry['bots']}; channel {dry['channel']}")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_rolled_out", "kept_bots")
async def test_a18_the_waker_wakes_it(scenario, tester2, rigged, server, aws, nonce):
    """C3, rehearsed: the plan shows this workshop's waker entry only (§2.1)."""
    t2 = talk(tester2, rigged, E2E2.name, f"e2e-A18-waker-{nonce}")
    answer = await t2.ask(f"!waker on {SLUG}", ANSWER, within=PLAN_S)
    plan = parse_plan(await whole(tester2, answer), answer["id"])
    assert plan is not None, answer["content"][:300]
    names = set(re.findall(r'^name = "([^"]+)"$', plan.text, re.M))
    assert names == {E2E2.name, f"codex-{SLUG}", f"claude-{SLUG}"}, f"the plan's entries: {names}"
    assert not re.search(r"\bzab-test-[ab]\b", plan.text), "the plan names the other workshop's"
    await t2.reply(answer["id"], rf"^{plan.id} `[^`]*`: approve ")  # pending only then
    _, text = await t2.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    async with waker_on(t2, SLUG):  # `!waker off zab-test-c` after it, whatever happens
        assert opted_in(server), f"after `!waker on`, {WAKER} does not list {SLUG}"
        if state(aws) != "stopped":
            await t2.run(f"!stop {SLUG}")
            await in_state(aws, "stopped")
        email = DONE["kept_bots"]["codex"]
        codex = int((await bots_now(tester2, {"codex": email}))["codex"]["user_id"])
        tester2.restrict(channels=tester2.channels, bots=[*tester2.bots, codex])
        after = last_seq(server)
        sent = await tester2.send_dm(codex, "!status")
        await in_state(aws, "running")
        # the bridge answers it (§7: `Bridge running`). Not `!login`: its hint needs the
        # backend's doctor within 2 s, which a VM just booted often misses
        await tester2.wait_reply(dm_narrow(codex), after_id=sent, senders=[codex],
                                 pattern=DM_STATUS_RE, within=600)  # fmt: skip
        # the starter logged the wake (a start it can't log, it doesn't make), and the wake
        # monitor checked it (#359): the row is bound to zab-tester2 (`--hold` binds it, and
        # §8.4 requires it of every kept row before `!waker on`), so it finds the DM that
        # asked for it, with the bots' keys that came with the stage (VERIFY V16)
        instance = vm_of(aws)["InstanceId"]
        record = await wake_check(server, instance, after, "zab-waker")
        assert bound(server), f"the server's row of {SLUG} has no zulip_user_id: V16 untested"
        assert (record["outcome"], record.get("message")) == ("matched", sent), record
    assert not opted_in(server), f"after `!waker off`, {WAKER} still lists {SLUG}"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_joined")
async def test_a18_the_servers_key_comes_off(scenario, tester2, rigged, aws, laptop, nonce):
    """The fallback's step 5 (ADMIN §8.4 at v0.2.15): a stopped VM is started first, through
    the bot."""
    t2 = talk(tester2, rigged, E2E2.name, f"e2e-A18-{nonce}")
    if state(aws) != "running":
        await t2.run(f"!start {SLUG}")
        await in_state(aws, "running")
    dry = await until(lambda: joined(laptop, dry_run=True, remove=True),
                      lambda r: not r.startswith("FAILED"), what="SSH to the VM",
                      within=TRANSITION_S, every=10)  # fmt: skip
    results = [dry, joined(laptop, remove=True), joined(laptop, remove=True)]
    assert results == ["would remove 1", "ok: removed 1", "ok: not there"], results
    after = await status(t2)
    assert after.get("state") == "unreachable", f"after the removal: {after}"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A18")
@requires("kept_bots")
async def test_a18_its_bots_are_deactivated(scenario, admin_rig, rigged, tester2, laptop):
    """With their owner's key, zab-tester's, who reactivates them at the next run's start
    (`kept.restore_bots`); the VM goes with the run's teardown."""
    ids = rigged.ids()
    owners = {u.get("bot_owner_id") for u in (await bots_now(tester2, DONE["kept_bots"])).values()}
    assert owners == {ids.tester}, f"the bots' owners: {owners}"
    done = laptop.provision(admin_rig.tester_zuliprc, "--deactivate")
    assert row_of(done, "participant")["bots"] == "codex: deactivated, claude: deactivated"
    gone = await bots_now(tester2, DONE["kept_bots"])
    assert all(u.get("is_active") is False for u in gone.values()), "a bot is still active"
