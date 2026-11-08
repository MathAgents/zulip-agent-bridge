"""A9, the end of a workshop (docs/ADMIN.md §7, "A9 in detail"; issues #356, #357): the ending
verbs through `Workshop admin · e2e`, in ORGANIZERS §5's order (`!uninstall`, `!final-sync`,
`!export`, `!teardown`, `!deactivate-bots`), with two approvers or a global admin alone (#558)
and the typed phrases, and each effect checked on AWS, on Zulip, in the server's `ws/` and in its
audit log. After each apply:
the result (✅, every step exited 0), its `audit #… · chain …` footer, and the plan's
`apply_start` and `apply_end` records (`exit` 0, `targets` the rows acted on).

**Two approvers.** The rig's admin.toml has `destructive_approvals = 1` (§7). A9's first part
installs `admin_toml.ending` (two approvers, `zab-tester2` a second organizer of
`zab-admin-e2e`, a 15-minute plan TTL) and its last part puts the rig's back; after a run that
died between them, the next run's setup does. A global admin's approval alone completes a
destructive plan (#558; Drew, Oct 22), and `zab-tester` is one: the spare's `!teardown` is
approved by `zab-tester` alone, with the phrase. Each other `!teardown` and `!deactivate-bots`
is approved first by `zab-tester2`, an organizer who is no global admin, with the phrase, which
is recorded as one of two and leaves the plan pending, then by `zab-tester`, which applies it.

**The order.** At most three instances run, the server's included (§7): the spare (§7's steps
1 and 7; spares hold no participant's work) is launched while `zab-test-a` is stopped, and
torn down before `zab-test-a` is started again. Then §7's steps 2 to 6, 6b (`!retire`, #559, on
two rows of its own, launched once step 6 has torn the others down) and 8; step 9's rebind is
not offered through the bots yet (`grammar.LATER`), and A2 has its import refused as one.

**The guard after a `!protect`** (#378, PR #382): the bot refuses a `!teardown` within 5
minutes of the workshop's last `!protect` apply, and one approved within them goes STALE at
apply. Step 4 goes STALE so. Step 6 sends `!protect off zab-test-a` again just before its
first request, which the guard then surely refuses, and sends it again at the time named. Any
other teardown it refuses (A4's `!protect on` just before, with `--scenarios A4,A9`) is sent
again then too (`ending.after_the_guard`).

**While Q9 is open** every part runs but two: the throwaway row's bots, and with them its
`!deactivate-bots`, need the provisioner's key. The final sync's push to `MathAgents/zab-test-a`
needs the GitHub token and A5's repositories: without a repository, the final sync checks that
`--strict` refuses the row, then skips naming Q9; the export runs, checks that it skipped both
rows as not provisioned and made `out/exports` 0700, then skips naming Q9 (the archives' checks
need a repository); and step 4 asks for `zab-test-a`'s teardown with `unsynced`, which goes
STALE as it does with Q9 answered.

A9 terminates the run's VMs: the next run's A4 launches them again, and A5 reuses their bots.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

import pytest

from e2e.admin.conftest import DONE, requires
from e2e.admin.harness import admin_toml, ending, start_here
from e2e.admin.harness.aws_rig import tag_filters
from e2e.admin.harness.chat import (
    FOOTER,
    PLAN_S,
    STATUS_ANSWER,
    Refused,
    Talk,
    status_of,
    table,
    whole,
)
from e2e.admin.harness.chat import widget_replies as buttons
from e2e.admin.harness.ending import SYNCED, UNSYNCED
from e2e.admin.harness.gate import tags_of
from e2e.admin.harness.rig import E2E
from e2e.admin.harness.settle import PROTECTION_S, WITHIN_S, attribute, until
from e2e.admin.test_scenarios_2_bot import (
    PROVISIONER_Q9,
    ROSTER_COLUMNS,
    client_of,
    import_roster,
    participant_bots,
    pending,
    records,
    refusal,
    restart,
    row,
    talk,
    vm_state,
    vm_states,
    wait_state,
)
from e2e.harness import config as rig263
from e2e.harness.zulip_user import TestUser, topic_narrow
from zab.admin.config import parse_admin_config

A, B, SPARE = "zab-test-a", "zab-test-b", "zab-spare-1"
WS = f"/var/lib/zab-admin/ws/{E2E.name}"
ADMIN_TOML = "/etc/zab-admin/admin.toml"
CONSOLE_SNAPSHOT = "/var/lib/zab-admin/console/snapshot.json"  # the collector's (§4.1)
#: Why the final sync's push skips while Q9 is open (ADMIN §10).
GITHUB_Q9 = (
    "Q9: the final sync pushes to MathAgents/zab-test-a, which needs the test server's GitHub "
    "token and A5's repositories (repo_status ok); without them --strict refuses the row, as "
    "checked here"
)
#: The addresses step 6's teardown released → the row that had each (#604): EC2 may give one to
#: a VM that step 6b launches, as it gave zab-test-a's to zab-test-r on Oct 23.
RELEASED: dict[str, str] = {}
#: A VM's state in `!status` while it can't be reached (status.py's `EC2_LABELS`, unreachable).
DOWN = ("unreachable", "stopped", "stopping", "starting")
#: As `zab-admin`, whose 0700 `ws/` it is (never root, to read an archive a VM made): the workshop's
#: out/exports/ (`collect_exports.py`), its mode, and each named row's archive: mode, owner, size,
#: member count, and any member at a credential's path (workshop/admin/README.md §7.2).
EXPORTS = r"""
import json, pathlib, pwd, stat, sys, tarfile
ws, *slugs = sys.argv[1:]
if ws != "zab-admin-e2e":
    sys.exit(2)
where = pathlib.Path("/var/lib/zab-admin/ws") / ws / "out" / "exports"
out = {"dir": oct(stat.S_IMODE(where.lstat().st_mode)) if where.is_dir() else None}
names = ("auth.json", ".credentials.json")
for slug in slugs:
    path = where / f"export-{slug}.tar.gz"
    if path.is_symlink() or not path.is_file():
        out[slug] = None
        continue
    st = path.lstat()
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getnames()
    bad = [m for m in members if m.casefold().endswith(names) or m.casefold().endswith(".zuliprc")
           or ".config/zab/" in m.casefold() + "/"]
    out[slug] = {"mode": oct(stat.S_IMODE(st.st_mode)), "owner": pwd.getpwuid(st.st_uid).pw_name,
                 "size": st.st_size, "members": len(members), "credentials": bad}
print(json.dumps(out))
"""
#: As `zab-admin`: the newest final_sync.py report in the workshop's out/ (not a dry run's).
SYNC_REPORT = r"""
import json, pathlib, sys
if sys.argv[1] != "zab-admin-e2e":
    sys.exit(2)
out = pathlib.Path("/var/lib/zab-admin/ws") / sys.argv[1] / "out"
found = sorted(p for p in out.glob("final_sync-*.json") if not p.name.endswith("-dry.json"))
print(json.dumps({"name": found[-1].name, "report": json.loads(found[-1].read_text())}
                 if found else {}))
"""


def pair(tester: Any, tester2: Any, rigged: Any, topic: str) -> tuple[Talk, Talk]:
    """`zab-tester` and `zab-tester2` with the e2e bot, in one topic."""
    return talk(tester, rigged, E2E.name, topic), talk(tester2, rigged, E2E.name, topic)


async def approved_by_two(t: Talk, t2: Talk, plan: Any) -> str:
    """`zab-tester2`'s approval with the phrase, an organizer's, recorded as one of two, the plan
    still pending (one organizer isn't enough); then `zab-tester`'s, a global admin's, which
    completes it by the count and applies it: the outcome, whole."""
    said = await t2.endorse(plan)
    assert re.match(ending.recorded(plan.id), said["content"]), said["content"][:200]
    assert await pending(t, plan.id), f"{plan.id} is no longer pending after one of two approvals"
    _, text = await t.approve(plan)
    assert plan.answered == ending.approved_by_two(plan.id), plan.answered
    return text


async def approved_alone(t: Talk, plan: Any, server: Any, since: int) -> str:
    """`zab-tester`'s approval alone, a global admin's, with the phrase (#558): the answer names
    the rule, and the plan applies with one approver on record: the outcome, whole."""
    _, text = await t.approve(plan)
    assert plan.answered == ending.approved_by_an_admin(plan.id), plan.answered
    starts = ending.of_plan(records(server, since), plan.id).get("apply_start", [])
    assert len(starts) == 1 and len(starts[0].get("approvers") or []) == 1, starts
    return text


def applied(server: Any, since: int, plan: Any, text: str, targets: list[str]) -> None:
    """The apply as §7 checks each: ✅, every step exited 0, the footer, and the plan's
    `apply_start` and `apply_end` records, `exit` 0, naming `targets`."""
    first = text.split("\n", 1)[0]
    assert first.startswith(f"✅ **{plan.id}**") and "every step exited 0" in first, first
    assert FOOTER.search(text), f"{plan.id}: no `audit #… · chain …` footer"
    found = ending.of_plan(records(server, since), plan.id)
    starts, ends = found.get("apply_start", []), found.get("apply_end", [])
    assert len(starts) == 1 and len(ends) == 1, f"{plan.id}: records {sorted(found)}"
    assert starts[0].get("targets") == targets, f"apply_start targets {starts[0].get('targets')}"
    assert ends[0].get("targets") == targets, f"apply_end targets {ends[0].get('targets')}"
    assert ends[0].get("exit") == 0, f"apply_end exit {ends[0].get('exit')}"


def admin_json(server: Any, script: str, *args: str) -> Any:
    """A fixed Python script of this file's, as `zab-admin`, its JSON answer."""
    done = server.as_admin("/usr/bin/python3", "-I", "-c", script, *args)
    assert done.ok, f"as zab-admin: exit {done.returncode}: {done.stderr.strip()[-300:]}"
    return json.loads(done.stdout)


async def terminated(aws: Any, instance: str) -> None:
    """Until EC2 shows `instance` terminated (not `shutting-down`), read by its ID (`settle`)."""
    await until(lambda: aws.describe(instance)["State"]["Name"], lambda s: s == "terminated",
                what=f"{instance} terminated", within=WITHIN_S, every=10)  # fmt: skip


def running_now(aws: Any) -> dict[str, str | None]:
    """zab-test-a's and zab-test-b's states as EC2 shows them now."""
    return {slug: vm_state(aws, slug) for slug in (A, B)}


def last_seq(server: Any) -> int:
    found = records(server)
    return int(found[-1]["seq"]) if found else 0


def addresses_of(aws: Any, participant: str) -> list[dict[str, Any]]:
    return [a for a in aws.addresses() if tags_of(a).get("participant") == participant]


def vm263(aws: Any) -> dict[str, str]:
    """The #263 test VM, as EC2 shows it: instance → state."""
    filters = tag_filters(rig263.load().instance_tags)
    found = aws.aws.ec2("describe_instances", Filters=filters)["Reservations"]
    return {i["InstanceId"]: i["State"]["Name"] for r in found for i in r["Instances"]
            if i["State"]["Name"] != "terminated"}  # fmt: skip


async def statuses(t: Talk, slugs: list[str], *, tries: int = 12) -> dict[str, dict[str, Any]]:
    """`!status` of `slugs`, again until each VM answers (sshd comes up a little after EC2 says
    `running`): slug → its cells in the answer's table (`state`, …; #439)."""
    vms: dict[str, dict[str, str]] = {}
    for _ in range(tries):
        answer = await t.ask(f"!status {' '.join(slugs)}", STATUS_ANSWER, within=PLAN_S)
        assert not answer["content"].startswith("❌"), answer["content"][:300]
        vms = status_of(await whole(t.user, answer))
        if all(s in vms and vms[s].get("state") not in (None, "?", *DOWN) for s in slugs):
            return vms
        await asyncio.sleep(20)
    shown = {s: (vms.get(s) or {}).get("state") for s in slugs}
    pytest.fail(f"!status never reached {slugs}: {shown}")


async def bot_users(tester: Any, names: list[str]) -> dict[str, dict[str, Any]]:
    """`zab-tester`'s own bots of these full names, as `GET /users/{email}` has them (active
    or not): name → user, never its key."""
    owned = (await tester.client.request("GET", "bots"))["bots"]
    found = {}
    for bot in (b for b in owned if b.get("full_name") in names):
        user = await tester.client.request("GET", f"users/{quote(bot['username'], safe='@')}")
        found[bot["full_name"]] = user["user"]
    return found


async def active(tester: Any, user_id: int) -> bool:
    return bool((await tester.client.request("GET", f"users/{user_id}"))["user"]["is_active"])


# ------------------------------------------------------------------------- two approvers


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
async def test_a9_two_approvers(scenario, admin_rig, rigged, server, tester, nonce):
    ids = rigged.ids()
    shape = dict(region=admin_rig.region, ami=rigged.ami, groups=rigged.groups)
    mine = (admin_rig.etc / "admin.toml").read_text(encoding="utf-8")
    assert admin_toml.render(ids, **shape) == mine, "the rig's admin.toml is not the harness's"
    text = admin_toml.ending(ids, **shape)
    config = parse_admin_config(text)  # the strict loader, before the server gets it
    e2e = config.workshop(E2E.name)
    assert config.destructive_approvals == 2 and e2e is not None
    assert config.approvers(e2e) == {ids.tester, ids.tester2}, config.approvers(e2e)
    server.install_etc("admin.toml", text)
    await restart(server, talk(tester, rigged, "global", f"e2e-A9-{nonce}"))
    DONE["two_approvers"] = True
    scenario.notes.append("destructive_approvals = 2: zab-tester and zab-tester2 approve")


# ---------------------------------------------------------------------- the throwaway row


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("roster")
async def test_a9_the_throwaway_row(scenario, admin_rig, rigged, server, tester, nonce):
    """§7: a row for this run only, with an e-mail nobody uses, whose bots `!deactivate-bots`
    may end. Every row the roster has goes in the upload, as it is, so nothing else changes."""
    t = talk(tester, rigged, E2E.name, f"e2e-A9-{nonce}")
    slug = ending.throwaway(rigged.started)
    rows = ending.inputs(server.read(f"{WS}/roster.csv"), ROSTER_COLUMNS)
    assert slug not in {r["slug"] for r in rows}, f"{slug} is in the roster already"
    email = f"zab-admin-e2e-{slug.removeprefix('zab-test-')}@example.com"  # RFC 2606
    plan = await import_roster(t, [*rows, row(slug, email, admin_rig.region)], f"{slug}.csv")
    assert "roster import: 1 added, 0 changed" in plan.text, plan.text[-400:]
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    assert slug in ending.rows_by_slug(server.read(f"{WS}/roster.csv")), "not in the roster"
    DONE["throwaway"] = slug
    scenario.notes.append(f"the throwaway row {slug}")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("throwaway")
async def test_a9_the_throwaway_rows_bots(scenario, rigged, server, tester, nonce):
    if not rigged.provisioner:
        pytest.skip(PROVISIONER_Q9)
    slug = DONE["throwaway"]
    names = [f"{kind} · {slug}" for kind in ("Claude", "Codex")]
    # a run that failed before its !deactivate-bots left its row's bots active: their owner
    # zab-tester deactivates them (`DELETE /bots/{id}`, as zulip_provision.py --deactivate)
    owned = [b["full_name"] for b in (await tester.client.request("GET", "bots"))["bots"]]
    me = (await tester.me())["user_id"]
    for name, user in (await bot_users(tester, ending.stale_throwaways(owned, slug))).items():
        if user.get("is_bot") and user.get("is_active") and user.get("bot_owner_id") == me:
            await tester.client.request("DELETE", f"bots/{user['user_id']}")
            scenario.notes.append(f"an earlier run's {name}: deactivated")
    t = talk(tester, rigged, E2E.name, f"e2e-A9-{nonce}")
    await t.run(f"!provision zulip hold {slug}")
    made = await bot_users(tester, names)
    assert sorted(made) == sorted(names), f"the throwaway row's bots: {sorted(made)}"
    assert all(u.get("is_active") and u.get("is_bot") for u in made.values()), made
    files = server.run(["sudo", "ls", "--", f"{WS}/out/{slug}"]).stdout.split()
    assert sorted(files) == ["claude.zuliprc", "codex.zuliprc"], files
    DONE["throwaway_bots"] = {name: int(u["user_id"]) for name, u in made.items()}


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("throwaway", "throwaway_bots")
async def test_a9_the_throwaway_rows_start_here(scenario, rigged, tester, nonce):
    """#709: `!provision zulip` releases the throwaway row's held sandbox, which has never had a
    "Start here" (`harness/start_here.py`). The plan passes the workshop's file server and its
    gift step (`admin_toml.GIFTS_STEP`, base64url) and shows the step in its dry run; the post
    is the product's text, word for word, its file host in code and its bot names links to their
    DMs; the description is the product's, under Zulip's 1024 characters. Read with the row's
    Claude bot's key, a member of the private sandbox, which `zab-tester` owns (§7)."""
    slug = DONE["throwaway"]
    ids = start_here.bot_ids(DONE["throwaway_bots"], slug)
    t = talk(tester, rigged, E2E.name, f"e2e-A9-start-here-{nonce}")
    plan = await t.request(f"!provision zulip {slug}")
    command = next((c for c in plan.commands if start_here.SCRIPT in c), "")
    assert not start_here.flag_problems(command), start_here.flag_problems(command)
    assert start_here.shown_line() in plan.text, "the dry run doesn't show the gift step"
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    posted = {r["participant"]: r for r in table(text, "participant")}[slug]["start here"]
    assert posted == "posted", f"start here: {posted}"
    owned = {b["full_name"]: b for b in (await tester.client.request("GET", "bots"))["bots"]}
    claude = TestUser(client_of(tester, owned[f"Claude · {slug}"]))
    try:
        subs = (await claude.client.request("GET", "users/me/subscriptions"))["subscriptions"]
        sandbox = next(s for s in subs if s["name"] == f"{slug}-sandbox")
        narrow = json.dumps(topic_narrow(int(sandbox["stream_id"]), start_here.TOPIC))
        found = {}
        for markdown in (False, True):
            got = await claude.client.request("GET", "messages", anchor="oldest", num_before=0,
                                              num_after=20, narrow=narrow,
                                              apply_markdown=markdown)  # fmt: skip
            found[markdown] = [m for m in got["messages"] if m["sender_id"] == ids["claude"]]
    finally:
        await claude.close()
    assert [len(found[False]), len(found[True])] == [1, 1], "not one post in Start here"
    assert found[False][0]["content"] == start_here.expected_post(slug, ids), "another text"
    problems = start_here.rendered_problems(found[True][0]["content"], ids)
    assert not problems, problems
    description = sandbox["description"]
    assert description == start_here.expected_description(slug, ids), description
    assert admin_toml.GIFTS_STEP in description and len(description) < 1024, len(description)
    scenario.notes.append(f"{slug}-sandbox: Start here and its description as the product's, "
                          "with the workshop's gift step (#709)")  # fmt: skip


# ------------------------------------------------------------------------ the spare (1, 7)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("launched", "two_approvers")
async def test_a9_a_spare_comes_and_goes(scenario, rigged, server, aws, tester, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A9-spare-{nonce}")
    # at most three instances run (§7): the server, zab-test-b (whose key launch.py's
    # scan_hosts.py reads again) and the spare; zab-test-a waits, stopped
    if vm_state(aws, A) in ("pending", "running"):
        await t.run(f"!stop {A}")
    await wait_state(aws, A, "stopped")
    if vm_state(aws, B) != "running":
        await t.run(f"!start {B}")
        await wait_state(aws, B, "running")
    others = [p for p in ending.running(aws.fleet()) if p != B]
    if others:
        pytest.skip(f"at most three instances run (§7): {others} run besides {B} and the server")
    before = {slug: inst["InstanceId"] for slug, inst in vm_states(aws).items()}
    plan, _ = await t.run(f"!launch {B} spares=1", phrase=False)
    assert plan.targets == [B, SPARE], f"the launch plan's targets: {plan.targets}"
    spare = await until(lambda: vm_states(aws).get(SPARE), lambda inst: bool(inst) and
                        inst["State"]["Name"] == "running", what=f"{SPARE} running",
                        within=600, every=10)  # fmt: skip
    address = (await until(
        lambda: [a for a in addresses_of(aws, SPARE) if a.get("InstanceId") == spare["InstanceId"]],
        bool, what=f"{SPARE}'s Elastic IP"))[0]  # fmt: skip
    spares = json.loads(server.read(f"{WS}/out/spares.json"))
    assert ending.spare_ip(spares, SPARE) == address["PublicIp"], "spares.json's address"
    now = {slug: inst["InstanceId"] for slug, inst in vm_states(aws).items() if slug != SPARE}
    assert now == before, f"launch.py changed the participants: {before} → {now}"

    rig = (vm263(aws), (aws.server() or {}).get("InstanceId"))
    since = last_seq(server)
    plan, guarded = await ending.after_the_guard(t, "!teardown spares")
    if guarded:
        scenario.notes.append(f"the guard after A4's !protect on: {guarded}; requested again")
    assert plan.targets == [SPARE] and plan.phrase == "teardown 1", (plan.targets, plan.phrase)
    assert ending.TWO_OR_AN_ADMIN in plan.text, "the plan doesn't say who completes it"
    dry = ending.dry_run(plan.text)
    assert dry.terminate == {SPARE: spare["InstanceId"]} and not dry.keep, dry
    assert dry.release == [address["AllocationId"]], dry
    text = await approved_alone(t, plan, server, since)  # a global admin's, alone (#558)
    applied(server, since, plan, text, [SPARE])
    await terminated(aws, spare["InstanceId"])
    await until(lambda: addresses_of(aws, SPARE), lambda found: not found,
                what=f"{SPARE}'s Elastic IP released")  # fmt: skip
    # #607: the spare torn down leaves spares.json, so that no plan offers it any more
    removed = rf"(?m)^spares\.json: \S+/{SPARE} removed \(its address "
    removed += rf"{re.escape(address['PublicIp'])} released\)$"
    assert re.search(removed, text), f"the teardown's result: {text[-600:]}"
    left = json.loads(server.read(f"{WS}/out/spares.json"))
    assert ending.spare_ip(left, SPARE) is None, f"spares.json still has {SPARE}: {left}"
    after = {slug: inst["InstanceId"] for slug, inst in vm_states(aws).items()}
    assert after == before, f"the participants changed with the spare's teardown: {after}"
    server_now = aws.server() or {}
    assert (vm263(aws), server_now.get("InstanceId")) == rig, "the rig's other instances"
    assert server_now.get("State", {}).get("Name") == "running", "the admin server"
    scenario.notes.append(
        f"{SPARE} launched and torn down on a global admin's approval alone; "
        "the #263 VM and the server as before"
    )


# ------------------------------------------------------------------------ uninstall (2)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("launched")
async def test_a9_uninstall(scenario, rigged, server, aws, tester, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A9-uninstall-{nonce}")
    others = [p for p in ending.running(aws.fleet()) if p not in (A, B)]
    if others:
        pytest.skip(f"at most three instances run (§7): {others} run besides {A} and {B}")
    for slug in (A, B):  # a stop still under way: EC2 refuses a start until it is done
        if vm_state(aws, slug) == "stopping":
            await wait_state(aws, slug, "stopped")
    down = [slug for slug in (A, B) if vm_state(aws, slug) != "running"]
    if down:
        await t.run(f"!start {' '.join(down)}")
    for slug in (A, B):
        await wait_state(aws, slug, "running")
    await statuses(t, [A, B])  # reachable: the uninstall is a command over SSH
    plan = await t.request("!uninstall all")
    assert plan.targets == [A, B] and plan.phrase == "uninstall 2", (plan.targets, plan.phrase)
    bare = f"!approve {plan.id} {plan.hash6}"
    answer = await t.ask(bare, r"^(✅|❌) ")
    said = answer["content"]
    assert said.startswith("❌") and ending.needs_phrase(plan.phrase) in said, said[:200]
    assert await pending(t, plan.id), f"{plan.id} changed after an approval without the phrase"
    since = last_seq(server)
    _, text = await t.approve(plan)
    applied(server, since, plan, text, [A, B])
    rows = {r["participant"]: r for r in table(text, "participant")}
    assert {s: rows.get(s, {}).get("uninstall") for s in (A, B)} == {A: "ok", B: "ok"}, rows
    bridges = {s: vm.get("state") for s, vm in (await statuses(t, [A, B])).items()}
    assert all(b in ending.INACTIVE for b in bridges.values()), bridges
    DONE["uninstalled"] = True
    scenario.notes.append(f"uninstalled; the bridges now: {bridges}")


# ------------------------------------------------------------------------ final sync (3)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("uninstalled")
async def test_a9_final_sync(scenario, rigged, server, tester, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A9-sync-{nonce}")
    status = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))[A].get("repo_status")
    if status != "ok":  # Q9: no repository, so nothing can be pushed; --strict refuses it
        with pytest.raises(Refused) as refused:
            plan = await t.request(f"!final-sync {A}")
            await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")
            pytest.fail(f"a final sync of {A}, which has no repository: {plan.id}")
        said = await refusal(t, refused.value)
        assert "not synced" in said and "repo_status" in said, said[:400]
        scenario.notes.append(f"{A} without a repository: --strict refused it")
        pytest.skip(GITHUB_Q9)
    plan = await t.request(f"!final-sync {A}")
    assert plan.targets == [A] and not plan.phrase, (plan.targets, plan.phrase)
    syncs = [c.split() for c in plan.commands if "final_sync.py" in c]
    assert syncs and all("--strict" in argv for argv in syncs), plan.commands
    since = last_seq(server)
    _, text = await t.approve(plan)
    applied(server, since, plan, text, [A])
    DONE["synced"] = True  # the ledger counts it now: steps 4 and 6 follow the bot, not the rest
    first = {r["participant"]: r for r in table(text, "participant")}[A]
    assert not first["problems"], f"{A}: {first['problems']}"
    report = admin_json(server, SYNC_REPORT, E2E.name)
    assert report and report["report"][A]["problems"] == [], report
    assert report["report"][A].get("idle_hold") == "released", report  # #370
    since = last_seq(server)
    plan, text = await t.run(f"!final-sync {A}")  # a rerun: everything is on GitHub already
    applied(server, since, plan, text, [A])
    again = {r["participant"]: r for r in table(text, "participant")}[A]
    assert (again["committed"], again["pushed"]) == ("0", "0"), again
    assert again["TOPICS.md"] in ending.SYNC_IDLE, again
    assert not again["problems"], again
    scenario.notes.append(f"{A}: synced ({first['pushed']} pushed); the rerun had nothing to do")


# ---------------------------------------------------------------------------- export


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("uninstalled")
async def test_a9_export(scenario, rigged, server, tester, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A9-export-{nonce}")
    roster = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    ours = [s for s in (A, B) if roster[s].get("repo_status") == "ok"]
    plan = await t.request("!export all")
    assert plan.targets == [A, B] and not plan.phrase, (plan.targets, plan.phrase)
    since = last_seq(server)
    _, text = await t.approve(plan)
    applied(server, since, plan, text, [A, B])
    rows = {r["participant"]: r for r in table(text, "participant")}
    for slug in (A, B):
        result = rows[slug]["result"]
        assert result == "ok" if slug in ours else result.startswith("skipped"), (slug, result)
    found = admin_json(server, EXPORTS, E2E.name, *ours)
    assert found["dir"] == "0o700", f"out/exports is {found['dir']}"  # made even when none is
    if not ours:
        pytest.skip(
            "Q9: no row has a repository (A5's), so both were skipped as not provisioned; the "
            "archives' mode, owner and credential checks need one"
        )
    for slug in ours:
        archive = found[slug]
        assert archive is not None, f"no archive of {slug} in out/exports"
        assert (archive["mode"], archive["owner"]) == ("0o600", "zab-admin"), archive
        assert archive["size"] > 0 and archive["members"] > 0 and not archive["credentials"]
    scenario.notes.append(f"archives of {', '.join(ours)}: 0600, no credential")


# ----------------------------------------------------------------------- teardowns (4-6)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("uninstalled")
async def test_a9_teardown_refuses_an_unsynced_row(scenario, rigged, server, aws, tester, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A9-unsynced-{nonce}")
    before = vm_states(aws)[B]
    since = last_seq(server)
    with pytest.raises(Refused) as refused:
        plan = await t.request(f"!teardown {B}")
        pytest.fail(f"a teardown of {B}, never synced: {plan.id}")
    said = await refusal(t, refused.value)
    why = ending.refused_unsynced(B)
    assert why in said, said[:300]
    logged = [r for r in records(server, since)
              if r.get("event") == "refused" and why in str(r.get("reason"))]  # fmt: skip
    assert len(logged) == 1, f"refused records: {logged}"
    after = vm_states(aws)[B]
    assert (after["InstanceId"], after["State"]["Name"]) == (
        before["InstanceId"],
        before["State"]["Name"],
    ), "EC2 changed"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("uninstalled", "two_approvers")
async def test_a9_a_protected_vm_is_kept_and_a_stale_plan_refused(
    scenario, rigged, server, aws, tester, tester2, nonce
):
    """§7 step 4: the plan keeps the protected VM; `!protect off` then; the teardown's approval
    within the guard's 5 minutes goes STALE before anything runs (#382)."""
    t, t2 = pair(tester, tester2, rigged, f"e2e-A9-stale-{nonce}")
    iid = vm_states(aws)[A]["InstanceId"]
    await until(lambda: attribute(aws, iid, "disableApiTermination"), lambda on: on is True,
                what=f"{A}'s termination protection (A4's)", within=PROTECTION_S)  # fmt: skip
    synced = "synced" in DONE  # A's sync counts: step 3 applied (Q9 answered)
    if not synced:  # refused as zab-test-b is in step 5, then asked with `unsynced`
        with pytest.raises(Refused) as refused:
            plan = await t.request(f"!teardown {A}")
            pytest.fail(f"a teardown of {A}, whose sync doesn't count: {plan.id}")
        said = await refusal(t, refused.value)
        assert ending.refused_unsynced(A) in said, said[:300]
    plan, guarded = await ending.after_the_guard(t, f"!teardown {A}" + " unsynced" * (not synced))
    if guarded:
        scenario.notes.append(f"the guard after A4's !protect on: {guarded}; requested again")
    count, unsynced, rows = ending.participant_rows(plan.text)
    assert (count, unsynced) == (1, 0 if synced else 1), (count, unsynced)
    state = SYNCED if synced else UNSYNCED
    assert re.fullmatch(state, rows[A]), rows[A]
    assert plan.phrase == "teardown 1" + " unsynced" * (not synced), plan.phrase
    assert ending.TWO_OR_AN_ADMIN in plan.text, "the plan doesn't say who completes it"
    dry = ending.dry_run(plan.text)
    assert dry.keep == {A: iid} and not dry.terminate and not dry.release, dry
    _, text = await t.run(f"!protect off {A}")
    shown = re.search(rf"{A} {iid}: termination protection off: set; EC2 (shows it now|doesn't "
                      r"show it yet)", text)  # fmt: skip
    assert shown, f"the !protect off result says nothing of EC2 (#382): {text[:300]}"
    scenario.notes.append(f"!protect off: EC2 {shown.group(1)}")
    since = last_seq(server)
    text = await approved_by_two(t, t2, plan)
    assert re.match(ending.stale_by_the_guard(plan.id), text), text.split("\n")[0]
    assert ending.retry_at(text, ending.utcnow()) is not None, "no time to request again"
    found = ending.of_plan(records(server, since), plan.id)
    assert "stale" in found and "apply_start" not in found, sorted(found)
    assert vm_states(aws)[A]["InstanceId"] == iid and vm_state(aws, A) == "running"
    scenario.notes.append(f"STALE: {text.split(chr(10))[0][:200]}")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("uninstalled", "two_approvers")
async def test_a9_teardown_all_unsynced(scenario, rigged, server, aws, tester, tester2, nonce):
    t, t2 = pair(tester, tester2, rigged, f"e2e-A9-teardown-{nonce}")
    iids = {slug: vm_states(aws)[slug]["InstanceId"] for slug in (A, B)}
    releases = sorted(a["AllocationId"] for slug in (A, B) for a in addresses_of(aws, slug))
    # §7 step 6 is refused at once by the guard (#378). `!protect off` again, right before it,
    # makes that certain however long step 4 took, or if it never lifted A's protection: the
    # guard counts any `!protect` apply, and this one changes nothing that step 4 did
    await t.run(f"!protect off {A}")
    command = "!teardown all unsynced"
    since = last_seq(server)
    with pytest.raises(Refused) as refused:
        plan = await t.request(command)
        pytest.fail(f"a teardown just after a !protect (#378): {plan.id}")
    said = await refusal(t, refused.value)
    when = ending.retry_at(said, ending.utcnow())
    assert when is not None, f"refused, but not by the guard: {said[:300]}"
    logged = [r for r in records(server, since) if r.get("event") == "refused"
              and "changed termination protection" in str(r.get("reason"))]  # fmt: skip
    assert len(logged) == 1, f"refused records: {logged}"
    assert running_now(aws) == {A: "running", B: "running"}, "EC2 changed"
    scenario.notes.append(f"refused at once: {said.split(chr(10))[0][:200]}")
    await ending.wait_until(when)  # then sent again from the time it names
    plan = await t.request(command)

    roster = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    slugs = list(roster)
    before = {slug: roster[slug].get("ip", "") for slug in (A, B)}  # released below (#604)
    n = len(slugs)
    assert plan.targets == slugs and plan.phrase == f"teardown {n} unsynced", plan.approve
    assert ending.TWO_OR_AN_ADMIN in plan.text, "the plan doesn't say who completes it"
    # no widget, which the web client would show instead of the text, and the line to send as
    # a one-line code block, which has a copy button (#542, the review of #544)
    message = await t.approval(plan)
    shown = buttons(message)
    assert shown is None, f"the approval message's buttons: {shown}"
    said = message["content"]
    assert f"\n\n```text\n{plan.approve}\n```\n\nIt takes {ending.TWO_OR_AN_ADMIN}:" in said, \
        said[:400]  # fmt: skip
    count, unsynced, rows = ending.participant_rows(plan.text)
    synced = "synced" in DONE
    assert (count, unsynced) == (n, n - synced), (count, unsynced)
    assert re.fullmatch(SYNCED if synced else UNSYNCED, rows[A]), rows
    assert re.fullmatch(UNSYNCED, rows[B]), rows
    for slug in (s for s in slugs if s not in (A, B)):  # the throwaway row: no VM
        assert rows[slug] == "no VM in the roster (one with its tag goes too), not synced: " \
                             "unsynced", rows[slug]  # fmt: skip
    dry = ending.dry_run(plan.text)
    assert dry.terminate == iids and not dry.keep and dry.release == releases, dry

    since = last_seq(server)
    bare = f"!approve {plan.id} {plan.hash6}"
    for who in (t, t2):  # no phrase; the phrase without `unsynced`: a global admin's too
        for wrong in (bare, f"{bare} teardown {n}"):
            said = (await who.ask(wrong, r"^(✅|❌) "))["content"]
            assert said.startswith("❌") and ending.needs_phrase(plan.phrase) in said, said[:200]
    assert await pending(t, plan.id), f"{plan.id} changed after a wrong phrase"
    said = (await t2.endorse(plan))["content"]  # an organizer's: one of two
    assert re.match(ending.recorded(plan.id), said), said[:200]
    again = (await t2.ask(plan.approve, r"^(✅|❌) "))["content"]
    assert again.startswith("❌") and ending.approved_already(plan.id) in again, again[:200]
    assert await pending(t, plan.id), f"{plan.id} is no longer pending after one approver"
    assert "apply_start" not in ending.of_plan(records(server, since), plan.id), "it applied"
    assert running_now(aws) == {A: "running", B: "running"}, "a VM went with one approval of two"
    since = last_seq(server)
    _, text = await t.approve(plan)  # a global admin's: the second
    assert plan.answered == ending.approved_by_two(plan.id), plan.answered
    applied(server, since, plan, text, slugs)
    for slug in (A, B):
        await terminated(aws, iids[slug])
        await until(lambda slug=slug: addresses_of(aws, slug), lambda found: not found,
                    what=f"{slug}'s Elastic IP released")  # fmt: skip
    after = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    for slug in (A, B):  # its ip cleared with the address released, its host kept (#604)
        assert not (bad := ending.torn_down(after[slug], slug)), bad
    RELEASED.update({ip: slug for slug, ip in before.items() if ip})
    DONE["torn_down"] = True
    scenario.notes.append(f"{plan.id}: both VMs terminated, both addresses released")


# ------------------------------------------------------------------------ !retire (6b, #559)

#: A `!retire` row's stages, as the result's table names its columns (`actions.STAGES`).
STAGES = ("start", "idle-stop", "waker", "uninstall", "final-sync", "export", "protect",
          "teardown", "bots")  # fmt: skip


def cells(text: str) -> dict[str, dict[str, str]]:
    """The result's table (`runner.Rows.table`, the first table headed `participant`)."""
    return {r["participant"]: r for r in table(text, "participant")}


def no_bots(server: Any, slugs: tuple[str, ...]) -> None:
    """Neither row has a bot key in `out/`: the retire's bots step, `zulip_provision.py
    --deactivate --only <row>`, deactivates only a bot named `<Kind> · <row>` whose key is there
    (the review of #367), so it finds both absent, and never names zab-test-a's or zab-test-b's
    bots, which every run reuses (§7, the throwaway row). Checked before every approval. A row
    never provisioned may have no `out/<row>/` at all (Codex on #566): none, as root, is no key;
    any other failure to list it is a failure here."""
    present = server.run(["sudo", "ls", "-A", "--", f"{WS}/out"]).stdout.split()
    for slug in slugs:
        if slug not in present:  # never provisioned: no out/<row>/, so no key
            continue
        listed = server.run(["sudo", "ls", "-A", "--", f"{WS}/out/{slug}"]).stdout.split()
        keys = [name for name in listed if name.endswith(".zuliprc")]
        assert not keys, f"{slug} has bot keys in out/: {keys}; its !retire would deactivate them"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("torn_down", "two_approvers")
async def test_a9_retire(scenario, admin_rig, rigged, server, aws, tester, tester2, nonce):
    """§7 step 6b (#559): `!retire`, once step 6 has torn zab-test-a and zab-test-b down (at most
    three instances), on three rows of its own, none ever with bots (`no_bots`):

    - `zab-test-r` (`ending.RETIRE_KEEPER`) runs the happy path, as a workshop's end does, from
      a stopped VM: started, uninstalled, synced (with a GitHub token that covers
      MathAgents/zab-test-r, to it), exported, torn down, with no `unsynced`;
    - the run's own row (`ending.retiring`), protected, never provisioned, stops at its sync,
      and nothing of it is torn down; then `!retire … unsynced`, by a global admin alone, ends
      it, its protection lifted first;
    - a row never launched (`ending.never`), no `ip`, in that second plan: its VM steps are
      skipped, and it is retired.

    Then, with every row retired (the bots step needs the provisioner's key), `all` leaves them
    out (`!stop all`, `!start all`), `!status all` names them, the console leaves them out, and
    `!launch` of one whose roster keeps its host is refused with what brings it back
    (#565). The `!protect` just before refuses the first request (#378). Each row's time is
    recorded."""
    t, t2 = pair(tester, tester2, rigged, f"e2e-A9-retire-{nonce}")
    r1, r2, r3 = (ending.RETIRE_KEEPER, ending.retiring(rigged.started),
                  ending.never(rigged.started))  # fmt: skip
    others = [p for p in ending.running(aws.fleet()) if p not in (r1, r2)]
    if others:
        pytest.skip(f"at most three instances run (§7): {others} run besides the server")
    roster = ending.inputs(server.read(f"{WS}/roster.csv"), ROSTER_COLUMNS)
    new = [row(s, f"zab-admin-e2e-{s.removeprefix('zab-test-')}@example.com", admin_rig.region)
           for s in (r1, r2, r3) if s not in {r["slug"] for r in roster}]  # fmt: skip
    if new:
        plan = await import_roster(t, [*roster, *new], f"{r2}.csv")
        _, text = await t.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    await t.run(f"!launch {r1} {r2}")  # r3 stays as it is: never launched
    for slug in (r1, r2):
        await wait_state(aws, slug, "running")
    iids = {slug: vm_states(aws)[slug]["InstanceId"] for slug in (r1, r2)}
    launched = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    shared = ending.shared_ips(launched)  # every admin script would refuse the roster (#604)
    assert not shared, f"rows share an address: {shared}"
    for slug in (r1, r2):
        if (drew := launched[slug].get("ip", "")) in RELEASED:  # the case of Oct 23, now handled
            scenario.notes.append(f"{slug} drew {RELEASED[drew]}'s released address {drew} (#604)")
    happy, why = False, GITHUB_Q9
    if rigged.github_token:  # MathAgents/zab-test-r: an owner made it (ADMIN §7); seeded once
        try:
            await t.run(f"!provision repos no-invite {r1}")
            why = ""
        except AssertionError as exc:  # a refusal or a failed step: the happy path waits
            why = f"!provision repos no-invite {r1}: {str(exc)[:200]}"
        status = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))[r1].get("repo_status")
        happy = not why and status == "ok"
        why = why or ("" if happy else f"{r1}'s repo_status is {status!r}, not ok")
    if why:
        scenario.notes.append(f"the happy path can't run: {why}")
    bots = "ok" if rigged.provisioner else "failed"  # the step reads Zulip with that key
    if not rigged.provisioner:
        scenario.notes.append(f"the bots step fails: {PROVISIONER_Q9}")
    await t.run(f"!stop {r1}")  # a stopped row: the prelude and its own start bring it up
    await wait_state(aws, r1, "stopped")
    await t.run(f"!protect on {r2}")
    kept = {name: bot["user_id"] for name, bot in (await participant_bots(tester)).items()}

    command = f"!retire {r1} {r2} export"
    plan, guarded = await ending.after_the_guard(t, command)
    assert "no !teardown or !retire for 5 minutes" in guarded, f"not held back: {guarded!r}"
    assert plan.targets == [r1, r2] and plan.phrase == "retire 2", (plan.targets, plan.phrase)
    assert ending.TWO_OR_AN_ADMIN in plan.text, "the plan doesn't say who completes it"
    assert "**Rows that end** (2)" in plan.text and "**Before you approve:**" in plan.text
    assert f"\n{r1}: {iids[r1]}\n" in plan.text and f"\n{r2}: {iids[r2]}\n" in plan.text, (
        "the rows' VMs, as found, are not those launched"
    )
    dry = ending.dry_run(plan.text)
    assert dry.terminate == {r1: iids[r1]} and dry.keep == {r2: iids[r2]}, dry
    no_bots(server, (r1, r2))
    since = last_seq(server)
    text = await approved_by_two(t, t2, plan)  # an organizer's, then a global admin's
    first, rows = text.split("\n", 1)[0], cells(text)
    end = ending.of_plan(records(server, since), plan.id)["apply_end"][0]
    for slug in (r1, r2):
        scenario.timing(f"!retire, {slug}'s steps", end["rows"][slug]["seconds"])
    count = sum(rows[slug]["retired"] == "yes" for slug in (r1, r2))
    assert first.startswith(f"❌ **{plan.id}** · `{command}`: {count} of 2 rows retired; "), first
    assert rows[r1]["start"] == "ok", rows[r1]  # it was stopped
    want = dict.fromkeys(STAGES, "ok") | {"bots": bots}
    if happy:  # uninstalled, synced (it counts: no `unsynced`), exported, torn down
        assert {s: rows[r1][s].split(":")[0] for s in STAGES} == want, rows[r1]
        assert f"- {r1}'s archive: `out/exports/export-{r1}.tar.gz`, on the server" in text
        assert rows[r1]["retired"] == ("yes" if rigged.provisioner else "no"), rows[r1]
    assert [rows[r2][s] for s in STAGES[:4]] == ["ok"] * 4, rows[r2]
    assert rows[r2]["final-sync"].startswith("failed: "), rows[r2]  # no repository: --strict
    assert [rows[r2][s] for s in STAGES[5:]] == ["not run"] * 4 and rows[r2]["retired"] == "no"
    assert f"- {r2} stopped at final-sync (`failed: " in text, "no line for the stopped row"
    assert end["exit"] != 0 and end["rows"][r2]["retired"] is False, end.get("rows")
    if rows[r1]["teardown"] == "ok":
        await terminated(aws, iids[r1])
        await until(lambda: addresses_of(aws, r1), lambda found: not found,
                    what=f"{r1}'s Elastic IP released")  # fmt: skip
    assert vm_state(aws, r2) == "running", "a row stopped at its sync was torn down"
    assert attribute(aws, iids[r2], "disableApiTermination") is True, "its protection went"
    scenario.notes.append(f"{plan.id}: {first[:200]}")
    retired = [slug for slug in (r1, r2) if rows[slug]["retired"] == "yes"]

    left = tuple(slug for slug in (r1, r2) if rows[slug]["teardown"] != "ok")  # not torn down
    command = f"!retire {' '.join((*left, r3))} unsynced"  # the rows that need it, on their own
    plan = await t.request(command)
    n = len(left) + 1
    assert plan.phrase == f"retire {n} unsynced", plan.phrase
    assert f"\n{r3}: no VM found: the steps that need one are skipped (`unsynced`)" in plan.text
    assert "**`unsynced`** covers every row of this plan" in plan.text
    no_bots(server, (*left, r3))
    since = last_seq(server)
    text = await approved_alone(t, plan, server, since)  # a global admin's, alone (#558)
    first, rows = text.split("\n", 1)[0], cells(text)
    end = ending.of_plan(records(server, since), plan.id)["apply_end"][0]
    for slug in (*left, r3):
        scenario.timing(f"!retire unsynced, {slug}'s steps", end["rows"][slug]["seconds"])
    assert rows[r2]["final-sync"].startswith("failed: "), rows[r2]  # tolerated: `unsynced`
    for slug in left:
        assert (rows[slug]["protect"], rows[slug]["teardown"]) == ("ok", "ok"), rows[slug]
    vm_steps = ("start", "idle-stop", "uninstall", "final-sync", "protect")
    assert [rows[r3][s] for s in vm_steps] == ["skipped: no VM"] * 5, rows[r3]
    assert (rows[r3]["waker"], rows[r3]["teardown"]) == ("ok", "ok"), rows[r3]
    for slug in (*left, r3):
        assert rows[slug]["bots"].split(":")[0] == bots, rows[slug]
    if rigged.provisioner:
        assert first.startswith(f"✅ **{plan.id}** · `{command}`: done in "), first
        lost = ", ".join(f"{slug} (final-sync failed)" for slug in left)  # r1 too, unhappy
        assert first.endswith(f", {n} of {n} rows retired; torn down unsynced: {lost}"), first
    for slug in left:
        await terminated(aws, iids[slug])
        await until(lambda slug=slug: addresses_of(aws, slug), lambda found: not found,
                    what=f"{slug}'s Elastic IP released")  # fmt: skip
    after = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    for slug in (r1, r2):  # each torn down by one plan or the other: its ip cleared (#604)
        assert not (bad := ending.torn_down(after[slug], slug)), bad
    scenario.notes.append(f"{plan.id}: {first[:200]}")
    retired += [slug for slug in (*left, r3) if rows[slug]["retired"] == "yes"]

    if rigged.provisioner:  # every row ran to its bots: all three are retired, either way
        assert sorted(retired) == sorted((r1, r2, r3)), retired
    if retired:  # the ledger (#565): after both plans, so that it runs without the happy path
        await the_retired_rows(t, server, retired, scenario)
    else:  # never quietly cut a check: the report says what wasn't checked, and why
        scenario.notes.append(
            "NOT CHECKED: `all`, `!status all`, the console and `!launch` with retired rows: "
            f"none was retired, since the bots step failed ({PROVISIONER_Q9})"
        )
    still = {name: await active(tester, uid) for name, uid in kept.items()}
    assert all(still.values()), f"other bots deactivated: {still}"


async def the_retired_rows(t: Talk, server: Any, retired: list[str], scenario: Any) -> None:
    """With `retired` retired (#565): `all` leaves them out, `!status all` names them and reads
    none, the console leaves them out, and a `!launch` of one whose roster keeps its host
    (each launched row's does) is refused, saying what brings it back (`actions.BACK`)."""
    await all_leaves_them_out(t, server, retired, scenario)
    answer = await t.ask("!status all", STATUS_ANSWER, within=PLAN_S)
    said = await whole(t.user, answer)
    named = re.search(r"^\*Retired, so left out: (.+)\.\*$", said, re.M)
    assert named and all(r in named.group(1) for r in retired), said[-400:]
    assert not set(retired) & set(status_of(said)), status_of(said)
    await until(lambda: console_rows(server), lambda listed: not set(retired) & listed,
                what=f"the console without {retired}", within=240, every=20)  # fmt: skip
    rows = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    kept = [slug for slug in retired if rows[slug].get("host") or rows[slug].get("ip")]
    for slug in kept[:1]:
        with pytest.raises(Refused) as refused:
            plan = await t.request(f"!launch {slug}")
            await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")
            pytest.fail(f"a !launch of {slug}, retired, whose roster keeps its host")
        said = await refusal(t, refused.value)
        assert "the roster keeps its host or ip" in said, said[:400]
        assert "clears its host and ip in roster.csv and reactivates its bots" in said, said
        scenario.notes.append(f"!launch {slug}: refused, its roster keeping its host")
    scenario.notes.append(f"retired, and left out of all, !status all and the console: {retired}")


async def all_leaves_them_out(t: Talk, server: Any, slugs: list[str], scenario: Any) -> None:
    """`!stop all` and `!start all` never reach a retired row (Codex on #565): their plans'
    targets and commands don't name one, and pass power.py's `--all`, which takes every row
    with an `ip`, only while none of them has one (`ending.leaves_out`; #604 clears a torn-down
    row's). Then rejected; or, refused (the rows torn down in step 6 have no VM), the refusal
    names none of them."""
    names = "|".join(re.escape(slug) for slug in slugs)
    alone = re.compile(rf"(?<![a-z0-9-])({names})(?![a-z0-9-])")
    rows = ending.rows_by_slug(server.read(f"{WS}/roster.csv"))
    for command in ("!stop all", "!start all"):
        try:
            plan = await t.request(command)
        except Refused as exc:
            said = await refusal(t, exc)
            assert not alone.search(said), f"{command} names a retired row: {said[:300]}"
            scenario.notes.append(f"{command}: refused, naming no retired row")
            continue
        shown = " ".join([*plan.targets, *plan.commands])
        why = ending.leaves_out(shown, slugs, rows)
        assert not why, f"{command}: {why}: {plan.targets}, {plan.commands}"
        if " --all" in shown:
            scenario.notes.append(f"{command}: --all, every retired row without an ip (#604)")
        await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")


def console_rows(server: Any) -> set[str]:
    """The rows the console's snapshot lists for the workshop (`collector`, §4.1)."""
    snapshot = json.loads(server.read(CONSOLE_SNAPSHOT))
    return {vm.get("slug") for vm in snapshot["workshops"][E2E.name]["vms"]}


# --------------------------------------------------------------------- deactivate-bots (8)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
@requires("throwaway", "throwaway_bots", "two_approvers")
async def test_a9_deactivate_bots(scenario, rigged, server, tester, tester2, nonce):
    t, t2 = pair(tester, tester2, rigged, f"e2e-A9-bots-{nonce}")
    slug, made = DONE["throwaway"], DONE["throwaway_bots"]
    kept = {name: bot["user_id"] for name, bot in (await participant_bots(tester)).items()}
    kept |= {name: uid for name, (uid, _) in rigged.ids().bots.items()}  # the admin bots
    plan = await t.request(f"!deactivate-bots {slug}")
    assert plan.targets == [slug] and plan.phrase == "deactivate-bots 1", plan.approve
    assert ending.TWO_OR_AN_ADMIN in plan.text, "the plan doesn't say who completes it"
    since = last_seq(server)
    text = await approved_by_two(t, t2, plan)
    applied(server, since, plan, text, [slug])
    bots = {r["participant"]: r for r in table(text, "participant")}[slug]["bots"]
    assert bots == "codex: deactivated, claude: deactivated", bots
    for name, uid in made.items():
        assert not await active(tester, uid), f"{name} is still active"
    still = {name: await active(tester, uid) for name, uid in kept.items()}
    assert all(still.values()), f"other bots deactivated: {still}"
    scenario.notes.append(f"{slug}'s two bots deactivated; {len(kept)} other bots active")


# --------------------------------------------------------------------------- rebind (9)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
async def test_a9_a_rebind_is_not_offered_yet(scenario, rigged, tester, nonce):
    """§7 step 9: until the rebind verb comes (a follow-up of PR 19), `!rebind` is refused and
    a deployer rebinds by hand; A2 has the e-mail change refused as a rebind."""
    t = talk(tester, rigged, E2E.name, f"e2e-A9-rebind-{nonce}")
    answer = await t.ask(f"!rebind {B}", r"^(❌|\*\*P-\d+\*\* · )")
    if "is not offered through the bots yet" in answer["content"]:
        scenario.notes.append("`!rebind` is not offered yet: A2 has the import refused as one")
        return
    if (plan := re.match(r"^\*\*(P-\d+)\*\*", answer["content"])) is not None:
        await t.ask(f"!reject {plan.group(1)}", rf"{plan.group(1)} rejected")
    pytest.fail(f"`!rebind` is offered now: A9's step 9 is due (§7): {answer['content'][:200]}")


# ------------------------------------------------------------------- the rig's admin.toml


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A9")
async def test_a9_the_rig_config_again(scenario, admin_rig, rigged, server, tester, nonce):
    """The rig's admin.toml back on the server (`destructive_approvals = 1`, A1's and A15's
    stranger), however A9 went; nothing to do when it is there."""
    mine = (admin_rig.etc / "admin.toml").read_text(encoding="utf-8")
    if server.read(ADMIN_TOML) == mine:
        scenario.notes.append("the rig's admin.toml is in place")
        return
    server.install_etc("admin.toml", mine)
    await restart(server, talk(tester, rigged, "global", f"e2e-A9-{nonce}"))
    assert server.read(ADMIN_TOML) == mine, "the rig's admin.toml did not stay"
    scenario.notes.append("the rig's admin.toml is back")
