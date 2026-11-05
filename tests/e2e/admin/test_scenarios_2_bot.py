"""The bot scenarios of stages 1 and 2 (docs/ADMIN.md §7; issue #356): A1 to A7, A14, A15, A8.

They run in this file's order, which is their order of dependence: the roster (A2) before the
launch (A4), the backups after both (A14), the refusals of a launched row (A2) after A4, the
provisioning (A5) before the e-mail changes (A2), the participant bot's posts (A1), the rollout
(A6) and the waker (A8); A7 stops the fleet. Each records what it established in `DONE`; a
scenario whose prerequisite failed is skipped, naming it (`needs`).

Every command goes through the plan as an organizer would: `!command`, the plan message (whole,
its attachment fetched when cut), `!approve P-n hash6 [phrase]` as the plan says, then the
result. Posts are `zab-tester`'s and `zab-tester2`'s in the `zab-dev*` channels, and A1's
`Codex · zab-test-a`'s there, and nowhere else. Two bot restarts (A1, A15) start each 10-minute
answer window afresh and write the windows' audit records (§2.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import time
import tomllib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import pytest

from e2e.admin.conftest import DONE, requires
from e2e.admin.harness.admin_toml import MENTION_AFTER_S
from e2e.admin.harness.chat import (
    ANSWER,
    FOOTER,
    LINK,
    PLAN_HEAD,
    PLAN_S,
    STATUS_ANSWER,
    Refused,
    Talk,
    Unchanged,
    parse_plan,
    status_of,
    status_rows,
    table,
    whole,
    widget_replies,
)
from e2e.admin.harness.rig import ADMINS_CHANNEL, E2E, E2E2, GLOBAL_BOT
from e2e.admin.harness.settle import PROTECTION_S, attribute, until
from e2e.harness.flows import DM_STATUS_RE
from e2e.harness.zulip_user import TestUser, dm_narrow, topic_narrow
from zab.admin import pages
from zab.admin.bot import AS_CODE

AUDIT = "/var/lib/zab-admin/audit.jsonl"
#: Why the scenarios that provision skip until Drew agrees (ADMIN §10, Q9).
PROVISIONER_Q9 = (
    "Q9: zab-tester's key as the test provisioning key awaits Drew's yes; the server has a "
    "placeholder until ~/.config/zab/admin-e2e/provisioner.zuliprc is placed"
)
A, B, C = "zab-test-a", "zab-test-b", "zab-test-c"
#: Never invited, never a Zulip user's (§7): example.com is reserved (RFC 2606).
A_EMAIL, A_EMAIL2, B_EMAIL2 = (f"zab-admin-e2e-{x}@example.com" for x in ("a", "a2", "b2"))
#: As root: one more (blank) line at the end of the e2e roster: a new file hash, the same rows.
TOUCH_ROSTER = r"""set -eu
case $1 in zab-admin-e2e) ;; *) exit 2 ;; esac
printf '\n' >> "/var/lib/zab-admin/ws/$1/roster.csv"
"""
#: `!status` of a stopped VM answers within this many seconds, from send to answer (#439): EC2's
#: state, with no SSH attempt, which alone waited out `ConnectTimeout=15` before.
STOPPED_STATUS_S = 12.0
#: The roster's address of zab-test-b is zab-tester2's own (§7), read from Zulip, never written.
ROSTER_COLUMNS = ("slug", "first_name", "last_name", "email", "repo", "region", "notes")


def row(slug: str, email: str, region: str) -> dict[str, str]:
    last = slug.removeprefix("zab-test-").upper()
    return dict(zip(ROSTER_COLUMNS, (slug, "Zab", f"Test {last}", email, f"MathAgents/{slug}",
                                     region, "admin e2e (#356)"), strict=True))  # fmt: skip


def unsigned(text: str) -> str:
    """A read's answer without its chain head, which every answer moves on (§2.8)."""
    return FOOTER.sub("", text).rstrip("` \n")


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=ROSTER_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def talk(user: TestUser, rigged: Any, where: str, topic: str) -> Talk:
    """`user` and the admin bot of `where` (a workshop's name, or "global") in its channel."""
    ids = rigged.ids()
    if where == "global":
        return Talk(user, ids.bots[GLOBAL_BOT][0], ids.channels[ADMINS_CHANNEL], topic)
    ws = E2E if where == E2E.name else E2E2
    return Talk(user, ids.bots[ws.bot][0], ids.channels[ws.channel], topic)


#: `!plan`'s state of a plan whose dry run said there is nothing to change (#708).
UNCHANGED_STATE = "`: **unchanged** (nothing to change, as its dry run says)"


async def nothing_to_change(t: Talk, command: str) -> str:
    """#708: `command` answered `☑`, nothing to change, with no plan (`Talk.unchanged`) and the
    chain head; `!plan` then reads it `unchanged`, never pending. The answer, whole."""
    plan, text = await t.unchanged(command)
    assert FOOTER.search(text), f"{plan}: no chain head: {text[-200:]}"
    shown = (await t.ask(f"!plan {plan}", rf"^{plan} `"))["content"]
    assert UNCHANGED_STATE in shown, shown[:300]
    return text


async def import_roster(t: Talk, rows: list[dict[str, str]], name: str):
    """`!roster import` with the CSV attached: its plan; `Refused` when the bot refuses it."""
    url = await t.user.client.upload(name, csv_bytes(rows))
    return await t.request(f"!roster import\n[{name}]({url})")


async def refusal(t: Talk, exc: Refused) -> str:
    return await whole(t.user, exc.message)


async def email_of(user: TestUser) -> str:
    me = await user.me()
    return str(me.get("delivery_email") or me["email"])


async def restart(server: Any, admins: Talk) -> None:
    """The bots restarted (their answer windows closed and recorded, §2.1), then answering."""
    done = server.bot("restart")
    assert done.ok, f"systemctl restart: {done.stderr.strip()[-200:]}"
    await ready(admins)


async def ready(admins: Talk) -> None:
    """Once the bots answer again: a message before a queue is registered is never read."""
    for _ in range(18):
        try:
            await admins.ask("!version", r"^zab ", within=10)
            return
        except TimeoutError:
            await asyncio.sleep(5)
    pytest.fail("the bots did not answer within 4 minutes of a restart")


def records(server: Any, after: int = 0) -> list[dict[str, Any]]:
    """The audit log's records with `seq` above `after`."""
    found = [json.loads(ln) for ln in server.read(AUDIT).splitlines() if ln.strip()]
    return [r for r in found if int(r.get("seq", 0)) > after]


def last_seq(server: Any) -> int:
    found = records(server)
    return int(found[-1]["seq"]) if found else 0


async def state_of(t: Talk, plan_id: str) -> str:
    """The plan's state as `!plan` shows it (`pending`, `expired`, …)."""
    answer = await t.ask(f"!plan {plan_id}", rf"^({plan_id} `[^`]*`: \*\*|{plan_id} is not)")
    found = re.search(r"\*\*([a-z]+)\*\*", answer["content"])
    return found.group(1) if found else "unknown"


async def pending(t: Talk, plan_id: str) -> bool:
    return await state_of(t, plan_id) == "pending"


def vm_states(aws: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for inst in aws.participants():
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        if tags.get("workshop") == E2E.name:
            out[tags["participant"]] = inst
    return out


def vm_state(aws: Any, slug: str) -> str | None:
    """Its state as EC2 shows it now (None: not shown at all, as a read just after a launch
    may not)."""
    return vm_states(aws).get(slug, {}).get("State", {}).get("Name")


async def wait_state(aws: Any, slug: str, state: str, within: float = 600) -> None:
    """Until EC2 shows `slug` in `state`: after a bot's `!stop` or `!start`, a transition of
    minutes, and a read that may lag behind it (`settle`)."""
    await until(lambda: vm_state(aws, slug), lambda now: now == state,
                what=f"{slug} {state}", within=within, every=10)  # fmt: skip


# ------------------------------------------------------------------------------------ A2, A3


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A2")
async def test_a2_the_first_import(scenario, tester, tester2, rigged, admin_rig, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A2-{nonce}")
    email2 = await email_of(tester2)
    rows = [row(A, A_EMAIL, admin_rig.region), row(B, email2, admin_rig.region)]
    plan = await import_roster(t, rows, "zab-admin-e2e-roster.csv")
    assert "roster import: 2 added, 0 changed" in plan.text, plan.text[-500:]
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    shown = await t.ask("!roster show", rf"{B}")
    assert A in shown["content"] and B in shown["content"]
    DONE["roster"] = {"email2": email2, "rows": rows}


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A3")
@requires("roster")
async def test_a3_binding(scenario, tester, rigged, server, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A3-{nonce}")
    admins = talk(tester, rigged, "global", f"e2e-A3-{nonce}")
    # `!launch all`: its dry run only reads EC2 and needs no provisioner key (Q9); A3 never
    # lets it apply
    plan = await t.request("!launch all")
    wrong = plan.approve.replace(plan.hash6, "000000" if plan.hash6 != "000000" else "111111")
    answer = await t.ask(wrong, r"^(✅|❌) ")
    assert answer["content"].startswith("❌") and "hash" in answer["content"], answer["content"]
    assert await pending(t, plan.id), f"{plan.id} is no longer pending after a wrong hash"
    scenario.notes.append(f"wrong hash6: refused ({answer['content'].split(chr(10))[0]})")

    expired = await t.reply(plan.message_id, rf"^⌛ {plan.id} expired", within=4 * 60)
    answer = await t.ask(plan.approve, r"^(✅|❌) ")
    assert answer["content"].startswith("❌") and "expired" in answer["content"]
    scenario.notes.append(f"after the TTL: {expired['content'].split(chr(10))[0]}")

    plan = await t.request("!launch all")
    server.script(TOUCH_ROSTER, E2E.name, sudo=True)  # an input of every plan's hash
    _, text = await t.approve(plan)
    assert re.match(rf"^❌ {plan.id} is stale\b", text), text.split("\n")[0]
    assert "roster.csv changed" in text, f"STALE without the diff: {text.split(chr(10))[0]}"
    scenario.notes.append(f"roster edited: {text.split(chr(10))[0]}")

    plan = await t.request("!launch all")
    stopped = server.bot("stop")
    assert stopped.ok, f"systemctl stop: {stopped.stderr.strip()[-200:]}"
    try:
        sent = await t.say(plan.approve)
    finally:
        started = server.bot("start")
    assert started.ok, f"systemctl start: {started.stderr.strip()[-200:]}"
    await ready(admins)
    said = await t.silent(sent, 20)
    decided = [m for m in said if re.match(rf"^(✅|❌) {plan.id}\b", m["content"])]
    assert not decided, f"an approval sent while the bot was down was processed: {decided}"
    state = await state_of(t, plan.id)  # its TTL may be over by now: never approved, though
    assert state in ("pending", "expired"), f"{plan.id} is {state} after the restart"
    if state == "pending":
        await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")


# ------------------------------------------------------------------------------- A4, A14, A2


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A4")
@requires("roster")
async def test_a4_launch_and_protect(scenario, tester, rigged, server, aws, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A4-{nonce}")
    started = time.monotonic()
    plan, text = await t.run("!launch all")
    scenario.timing("launch, request to result", time.monotonic() - started)
    assert plan.targets == [A, B], f"the plan's targets: {plan.targets}"
    # every read of what the bot just did waits for EC2 to show it (`settle`)
    vms = await until(lambda: vm_states(aws), lambda found: sorted(found) == [A, B],
                      what="the fake participants, launched and tagged")  # fmt: skip
    for slug, inst in vms.items():
        tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
        assert tags.get("workshop") == E2E.name and tags.get("zab-role") == "test", tags
        assert inst["InstanceType"] == "t3.medium" and "zab-admin" not in tags
        await until(
            lambda iid=inst["InstanceId"]: attribute(aws, iid, "instanceInitiatedShutdownBehavior"),
            lambda value: value == "stop", what=f"{slug}'s shutdown behavior, stop",
        )  # fmt: skip
    assert re.search(r"\b2 matched the console\b", text), "scan_hosts did not verify both keys"
    pinned = server.read(f"/var/lib/zab-admin/ws/{E2E.name}/out/known_hosts").splitlines()
    assert len({line.split()[0] for line in pinned if line.strip()}) == 2, "known_hosts"
    DONE["participants"] = [inst["InstanceId"] for inst in vms.values()]
    DONE["launched"] = True

    plan, _ = await t.run("!protect on zab-test-a")
    assert plan.targets == [A], f"the protect plan's targets: {plan.targets}"  # B: never asked
    # the live runs read A's False for up to 157 s after the role set it True (PROTECTION_S);
    # B is read after A shows the change, so a protection set on B by the same run would show
    # by then too (and the plan named A alone)
    for slug, on in ((A, True), (B, False)):
        await until(
            lambda iid=vms[slug]["InstanceId"]: attribute(aws, iid, "disableApiTermination"),
            lambda value, on=on: value is on, what=f"{slug}'s termination protection, {on}",
            within=PROTECTION_S,
        )  # fmt: skip


#: As root: SHA-256 of the workshop's roster and of every file of its out/, by relative path.
LIST_WS = r"""set -eu
case $1 in zab-admin-e2e) ;; *) exit 2 ;; esac
cd "/var/lib/zab-admin/ws/$1"
python3 -I -c '
import hashlib, json, pathlib
files = [pathlib.Path("roster.csv")]
files += sorted(p for p in pathlib.Path("out").rglob("*") if p.is_file())
sums = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.exists()}
print(json.dumps(sums))'
"""


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A14")
@requires("roster", "launched")
async def test_a14_the_deployer_has_backups(scenario, admin_rig, rigged, server, tester, tmp_path):
    if shutil.which("age") is None:
        pytest.skip("`age` is not installed here (`sudo apt install age`): A14 decrypts")
    # a capability, not a module name guessed before #355: a release that makes backups reads
    # backup_recipients (§1.7), and none does before it
    probe = ["grep", "-rlq", "backup_recipients", "/opt/zab-admin/current/src/zab/admin"]
    if not server.run(probe, check=False).ok:
        pytest.skip("the release makes no encrypted backups yet: PR 13 (#355)")
    ids = rigged.ids()
    found: list[tuple[int, str]] = []
    for _ in range(12):  # the last apply's backup may still be on its way
        found = []
        for uid, _email in ids.bots.values():
            for m in await tester.after(dm_narrow(uid), 0):
                if m["sender_id"] == uid and m["timestamp"] >= rigged.started:
                    found += [(m["id"], p) for p in LINK.findall(m["content"]) if ".age" in p]
        if len(found) >= 2:
            break
        await asyncio.sleep(10)
    assert len(found) >= 2, f"the deployer received {len(found)} backups since the run began"
    meta = await tester.client.request("GET", max(found)[1].removeprefix("/"))
    data, _ = await tester.client.download(meta["url"], 100_000_000)
    (tmp_path / "backup.tar.age").write_bytes(data)
    argv = ["age", "-d", "-i", str(admin_rig.keys / "backup"), "-o", str(tmp_path / "backup.tar")]
    done = await asyncio.to_thread(
        subprocess.run,  # an argument list
        [*argv, str(tmp_path / "backup.tar.age")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"age could not decrypt the backup: {done.stderr.strip()[:200]}"
    members: dict[str, str] = {}
    with tarfile.open(tmp_path / "backup.tar") as tar:
        for member in tar.getmembers():
            body = tar.extractfile(member).read() if member.isfile() else b""  # type: ignore[union-attr]
            assert b"PRIVATE KEY" not in body, f"a private key in the backup: {member.name}"
            members[member.name] = hashlib.sha256(body).hexdigest()
    current = json.loads(server.script(LIST_WS, E2E.name, sudo=True).stdout)
    assert "roster.csv" in current, "the server has no roster"
    for rel, digest in current.items():
        mine = [h for name, h in members.items() if name.endswith(f"{E2E.name}/{rel}")]
        assert mine == [digest], f"{rel}: the backup has {mine or 'nothing'}, the server {digest}"
    scenario.notes.append(f"{len(found)} backups; the latest holds {len(current)} files, as now")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A2")
@requires("roster", "launched")
async def test_a2_a_duplicate_and_a_rename_are_refused(scenario, tester, rigged, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A2-{nonce}")
    rows = DONE["roster"]["rows"]
    twice = [rows[0], {**rows[1], "email": rows[0]["email"]}]
    renamed = [{**rows[0], "slug": "zab-test-x"}, rows[1]]
    cases = (("duplicate", twice, r"(?i)duplicate"), ("rename", renamed, r"(?i)renam|tear it"))
    for name, bad, why in cases:
        with pytest.raises(Refused) as refused:
            plan = await import_roster(t, bad, f"zab-admin-e2e-{name}.csv")
            pytest.fail(f"a plan for the {name}: {plan.id}")
        text = await refusal(t, refused.value)
        assert re.search(why, text), f"{name}: refused for another reason: {text[:300]}"
        scenario.notes.append(f"{name}: refused, no plan")


# ---------------------------------------------------------------------------------------- A5


async def participant_bots(tester: TestUser) -> dict[str, dict[str, Any]]:
    """The test participants' bots that `zab-tester` (the provisioner, §7) owns, by full name,
    with `user_id` added; their keys are only kept in memory, never logged or reported."""
    owned = (await tester.client.request("GET", "bots"))["bots"]
    me = (await tester.me())["user_id"]
    wanted = {f"{k} · {s}" for k in ("Claude", "Codex") for s in (A, B)}
    found = {}
    for bot in (b for b in owned if b["full_name"] in wanted):
        # one by one: the bulk GET /users can leave a bot out (the first live setup)
        path = f"users/{quote(bot['username'], safe='@')}"
        user = (await tester.client.request("GET", path))["user"]
        if user.get("is_active") and user.get("is_bot") and user.get("bot_owner_id") == me:
            found[bot["full_name"]] = {**bot, "user_id": user["user_id"]}
    return found


def key_digests(bots: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Each bot's key as a SHA-256, to compare runs without ever showing a key."""
    return {name: hashlib.sha256(b["api_key"].encode()).hexdigest() for name, b in bots.items()}


def client_of(tester: TestUser, bot: dict[str, Any]) -> Any:
    from zab.zulip.client import ZulipClient

    return ZulipClient(tester.client.site, bot["username"], bot["api_key"])


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A5")
@requires("roster")
async def test_a5_provision_zulip(scenario, tester, tester2, rigged, nonce):
    if not rigged.provisioner:
        pytest.skip(PROVISIONER_Q9)
    t = talk(tester, rigged, E2E.name, f"e2e-A5-{nonce}")
    await t.run("!provision zulip hold all")
    first = await participant_bots(tester)
    assert sorted(first) == sorted(f"{k} · {s}" for k in ("Claude", "Codex") for s in (A, B))
    roster = unsigned((await t.ask("!roster show zab-test-b", r"zulip_user_id"))["content"])
    assert re.search(rf"zulip_user_id: {rigged.ids().tester2}\b", roster), "B is not bound"
    for slug in (A, B):
        bot = TestUser(client_of(tester, first[f"Codex · {slug}"]))
        try:
            subs = (await bot.client.request("GET", "users/me/subscriptions"))["subscriptions"]
        finally:
            await bot.close()
        assert any(s["name"] == f"{slug}-sandbox" for s in subs), f"no sandbox {slug}-sandbox"
    mine = (await tester2.client.request("GET", "users/me/subscriptions"))["subscriptions"]
    assert all(s["name"] != f"{B}-sandbox" for s in mine), "held, yet zab-tester2 is subscribed"
    DONE["provisioned"] = first
    await t.run("!provision zulip hold all")
    again = await participant_bots(tester)
    ids_again = {n: b["user_id"] for n, b in again.items()}
    assert ids_again == {n: b["user_id"] for n, b in first.items()}, "a bot was made again"
    assert key_digests(again) == key_digests(first), "the second run regenerated a bot's key"
    again_roster = await t.ask("!roster show zab-test-b", r"zulip_user_id")
    assert unsigned(again_roster["content"]) == roster, "the second run changed zab-test-b's row"
    scenario.notes.append("held; a second run changed no bot, key or binding")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A5")
@requires("provisioned")
async def test_a5_provision_repos(scenario, tester, rigged, nonce):
    if not rigged.github_token:
        pytest.skip("Q9: no GitHub token for MathAgents/zab-test* on the test server yet")
    t = talk(tester, rigged, E2E.name, f"e2e-A5-{nonce}")
    await t.run("!provision repos all")
    await t.run("!provision repos all")
    DONE["repos"] = True  # both applied: test_a5_invites_only_twice reads their invitations
    pytest.skip(
        "repositories provisioned twice, both applied; what GitHub shows is not checked here. "
        "The reconciler race needs no live run (#493): its plan hashes no live state, and one "
        "plan applies at a time per workshop (ADMIN §7, A5)"
    )


#: provision_repos.py's line under its summary when `invites-only` would change nothing (#708).
INVITES_UNCHANGED = (
    "nothing to change: no invitation to send or withdraw, and no acceptance to record"
)


def github_api(token: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """One REST call to api.github.com with the rig's token (MathAgents/zab-test* only)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A5")
@requires("provisioned", "repos")
async def test_a5_invites_only_twice(scenario, tester, rigged, nonce):
    """#708, the Harvard case: `!provision repos invites-only all` twice in a row, after the
    full runs. The first is planned and applied, or has nothing to change already; the second
    has nothing to change: `☑`, no plan and no approval asked, its dry run's line under it."""
    if not rigged.github_token:
        pytest.skip("Q9: no GitHub token for MathAgents/zab-test* on the test server yet")
    t = talk(tester, rigged, E2E.name, f"e2e-A5-{nonce}")
    first = "planned and applied"
    try:
        await t.run("!provision repos invites-only all")
    except Unchanged:
        first = "nothing to change already"
    said = await nothing_to_change(t, "!provision repos invites-only all")
    assert INVITES_UNCHANGED in said, said[-600:]
    scenario.notes.append(f"invites-only: the first {first}; the second ☑, with no plan")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A5")
@requires("provisioned", "repos")
async def test_a5_invites_only_raises_a_test_login_to_admin(
    scenario, tester, rigged, admin_rig, nonce
):
    """#714: participants administer their own repository. zab-test-a's login is lowered to
    push (as an earlier release invited it), then `!provision repos invites-only` raises it,
    as a collaborator or as its pending invitation, and GitHub shows admin."""
    if not rigged.github_token:
        pytest.skip("Q9: no GitHub token for MathAgents/zab-test* on the test server yet")
    t = talk(tester, rigged, E2E.name, f"e2e-A5-{nonce}")
    shown = unsigned((await t.ask(f"!roster show {A}", r"github"))["content"])
    found = re.search(r"\bgithub: ([A-Za-z0-9-]+)", shown)
    if not found:
        pytest.skip(f"{A} has no GitHub login: none in zab-tester's Zulip profile")
    login, repo = found.group(1), f"/repos/MathAgents/{A}"
    token = admin_rig.github_token.read_text(encoding="utf-8").strip()
    status, body = github_api(token, "GET", f"{repo}/collaborators/{login}/permission")
    assert status == 200, f"GitHub answered {status} for {login}'s permission"
    pending = []
    if body["permission"] == "admin":  # a collaborator already: lowered, as 0.2.15 invited
        status, _ = github_api(
            token, "PUT", f"{repo}/collaborators/{login}", {"permission": "push"}
        )
        assert status == 204, f"lowering {login} to push: {status}"
        status, body = github_api(token, "GET", f"{repo}/collaborators/{login}/permission")
        if body["permission"] == "admin":
            pytest.skip(f"{login} is admin of {A} by another role (an org owner): nothing to raise")
    elif body["permission"] == "none":
        status, invitations = github_api(token, "GET", f"{repo}/invitations")
        pending = [i for i in invitations if i["invitee"]["login"].casefold() == login.casefold()]
        assert pending, f"{login} neither has access to {A} nor an invitation"
        status, _ = github_api(
            token, "PATCH", f"{repo}/invitations/{pending[0]['id']}", {"permissions": "write"}
        )
        assert status == 200, f"lowering {login}'s invitation to write: {status}"
    _, text = await t.run(f"!provision repos invites-only {A}")
    assert "raised to admin" in text, f"no raise in the result: {text[:400]}"
    status, body = github_api(token, "GET", f"{repo}/collaborators/{login}/permission")
    if pending:
        status, invitations = github_api(token, "GET", f"{repo}/invitations")
        mine = [i for i in invitations if i["id"] == pending[0]["id"]]
        assert mine and mine[0]["permissions"] == "admin", f"{login}'s invitation: {mine}"
        scenario.notes.append(f"{login}'s pending invitation to {A} raised to admin")
    else:
        assert body["permission"] == "admin", f"{login} has {body['permission']} on {A}"
        scenario.notes.append(f"{login}, a collaborator of {A} with push, raised to admin")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A2")
@requires("roster", "provisioned")
async def test_a2_e_mail_changes(scenario, tester, rigged, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A2-{nonce}")
    rows = DONE["roster"]["rows"]
    with pytest.raises(Refused) as refused:
        plan = await import_roster(t, [rows[0], {**rows[1], "email": B_EMAIL2}], "zab-b.csv")
        pytest.fail(f"a plan for a rebind of {B}: {plan.id}")
    assert re.search(r"(?i)rebind", await refusal(t, refused.value)), "B: not refused as a rebind"
    plan = await import_roster(t, [{**rows[0], "email": A_EMAIL2}, rows[1]], "zab-a.csv")
    assert "roster import: 0 added, 1 changed" in plan.text, plan.text[-400:]
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    shown = (await t.ask("!roster show zab-test-a", r"email"))["content"]
    assert A_EMAIL2 in shown, "zab-test-a's new e-mail is not in the roster"
    scenario.notes.append(f"{B}: refused as a rebind; {A}: a plain change, applied")
    # #628: `!provision zulip` sends no invitation. The new address, which no Zulip user has, is
    # reported `not on Zulip`, with the line that tells organizers to invite it in Zulip, in the
    # plan's dry run and in the result alike, and the provisioner (`zab-tester` on the rig, §7)
    # has no invitation open to it afterwards. Without `hold`: nobody is subscribed to the row's
    # sandbox, which gets its "Start here" and full description, and the fleet stays held (a
    # named row's run records nothing for `all`, §2.4)
    await withdraw(tester, A_EMAIL2)  # one an older harness sent (#322), open for an hour at most
    plan = await t.request(f"!provision zulip {A}")
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    line = NOT_ON_ZULIP.format(slug=A, email=A_EMAIL2)
    for said, what in ((plan.text, "the plan"), (text, "the result")):
        cells = {r["participant"]: r for r in table(said, "participant")}[A]
        assert cells["invite"] == "not on Zulip" and cells["zulip id"] == "-", (what, cells)
        assert line in said, f"{what}: no line to invite {A}'s address: {said[-600:]}"
    assert not await invitations_to(tester, A_EMAIL2), f"an invitation to {A}'s address is open"
    scenario.notes.append(f"{A}: !provision zulip reported it not on Zulip, and invited nobody")
    # changed back: a plain change, with no invitation to revoke
    plan = await import_roster(t, [rows[0], rows[1]], "zab-a-back.csv")
    assert "plain change: not on Zulip, and no open invitation" in plan.text, plan.text[-400:]
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    shown = (await t.ask("!roster show zab-test-a", r"email"))["content"]
    assert A_EMAIL in shown and A_EMAIL2 not in shown, "zab-test-a's e-mail is not changed back"
    scenario.notes.append(f"{A}: changed back, a plain change")


#: `zulip_provision.invite_line`'s line after the table for a row not on Zulip (#628).
NOT_ON_ZULIP = (
    "{slug}: not on Zulip: invite {email} in Zulip (Invite users, with the participants' "
    "channel ticked), then provision again once they have joined"
)


async def withdraw(user: TestUser, email: str) -> None:
    """Every open invitation to `email` that `user` lists, revoked."""
    for invite_id in await invitations_to(user, email):
        await user.client.request("DELETE", f"invites/{int(invite_id)}")


async def invitations_to(user: TestUser, email: str) -> list[int]:
    """The ids of the open personal invitations to `email` that `user`'s `GET /invites` lists
    (a Moderator's: their own; it leaves out used, revoked and expired ones)."""
    listed = (await user.client.request("GET", "invites"))["invites"]
    return [
        int(inv["id"])
        for inv in listed
        if not inv.get("is_multiuse") and str(inv.get("email", "")).casefold() == email.casefold()
    ]


# ---------------------------------------------------------------------------------------- A1


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A1")
@requires("launched")
async def test_a1_a_stranger_is_refused_once(scenario, tester, tester2, rigged, server, nonce):
    topic = f"e2e-A1-{nonce}"
    t, t2 = talk(tester, rigged, E2E.name, topic), talk(tester2, rigged, E2E.name, topic)
    await restart(server, talk(tester, rigged, "global", topic))  # fresh windows
    since = last_seq(server)
    sent = await t.say("!status all")  # an organizer's: the bot's 👀 at once, then the reads
    seen = await t.acknowledged(sent)
    await t.reply(sent, STATUS_ANSWER, within=PLAN_S)  # answered before the stranger speaks
    scenario.notes.append(f"👀 on zab-tester's !status all within {seen:.1f} s (#466)")
    plan = await t.request("!stop zab-test-b")  # zab-tester's, never approved
    first = await t2.say("!status all")
    answer = await t2.reply(first, r"^🚫")
    clicked = f"@**{E2E.bot}** {plan.approve}"  # a click on the plan's own button
    later = [await t2.say(text) for text in ("!stop zab-test-b", plan.approve, clicked)]
    said = [m for m in await t.silent(later[0], 30) if m["id"] != answer["id"]]
    assert not said, f"answered again within the window: {[m['content'][:80] for m in said]}"
    acked = {}
    for mid in (first, *later):  # a transient error retried, as `silent` does
        codes = await tester.retrying(
            lambda mid=mid: t.reacted(mid), until=tester.clock() + 120, what="the bot's reactions"
        )
        if codes:
            acked[mid] = codes
    assert not acked, f"zab-tester2's messages got the bot's reactions: {acked}"
    assert await pending(t, plan.id), f"{plan.id} changed after zab-tester2's approvals"
    await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")
    await restart(server, talk(tester, rigged, "global", topic))  # the window's record
    refused = [r for r in records(server, since) if r.get("event") == "refused"
               and (r.get("sender") or {}).get("id") == rigged.ids().tester2]  # fmt: skip
    assert [r.get("messages") for r in refused] == [4], f"refused records: {refused}"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A1")
async def test_a1_a_command_sent_as_code_gets_the_hint(scenario, tester, rigged, server, nonce):
    """#546: `!status all` in inline backticks, as a paste of a code span writes it, is no
    command. Each time, the bot answers its fixed hint, word for word, and nothing else: no 👀,
    no read, no plan. The second try, sent once the first is answered, gets it too (no window)."""
    t = talk(tester, rigged, E2E.name, f"e2e-A1-code-{nonce}")
    since = last_seq(server)
    first = await t.say("`!status all`")
    hint = await t.reply(first, r"^❌ not run: ")
    again = await t.say("`!status all`")
    hint2 = await t.reply(again, r"^❌ not run: ")
    assert hint["content"] == hint2["content"] == AS_CODE, hint["content"]
    # `silent` also returns the bot's reactions, which have no `id`: a 👀 fails here, readably
    said = [m for m in await t.silent(again, 30) if m.get("id") != hint2["id"]]
    assert not said, f"more than the hint: {[m['content'][:80] for m in said]}"
    for mid in (first, again):  # a transient error retried, as `silent` does
        codes = await tester.retrying(
            lambda mid=mid: t.reacted(mid), until=tester.clock() + 120, what="the bot's reactions"
        )
        assert not codes, f"the bot reacted to a command sent as code: {codes}"
    taken = [r for r in records(server, since) if r.get("event") in ("read", "request")
             and (r.get("requester") or {}).get("id") == rigged.ids().tester]  # fmt: skip
    assert not taken, f"read or planned: {[(r['event'], r.get('verb')) for r in taken]}"
    scenario.notes.append("`` `!status all` ``: the hint twice, no 👀, no read or plan (#546)")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A1")
@requires("launched", "provisioned")
async def test_a1_a_participant_bot_is_never_obeyed(scenario, tester, rigged, server, nonce):
    topic = f"e2e-A1-bot-{nonce}"
    t = talk(tester, rigged, E2E.name, topic)
    since = last_seq(server)
    plan = await t.request("!stop zab-test-b")
    # the bot's own widget, beside the fake one below: no phrase, so both buttons (#542)
    real = widget_replies(await t.approval(plan))
    assert real == [plan.approve, f"!reject {plan.id}"], f"the approval message's buttons: {real}"
    codex = DONE["provisioned"][f"Codex · {A}"]
    bot = TestUser(client_of(tester, codex))
    bot.restrict(channels=[t.channel], bots=[])
    reply = f"!approve {plan.id} {plan.hash6}"
    try:
        first = await bot.send(t.channel, topic, "!stop all")
        warned = await t.reply(first, r"^⚠ not from an organizer")
        fake = (f"**{plan.id}** · {E2E.name} · `!stop all` · requested by you\n"
                f"**Plan** `{'0' * 12}`: approve with `{reply}`")  # fmt: skip
        await bot.send(t.channel, topic, fake)
        # as the bot's own approval message builds it (`planning._choice`, #366): without its
        # `type` the web client drops the widget, and an attacker's would have one
        choice = {"type": "multiple_choice", "short_name": "✅ Approve",
                  "long_name": f"Approve {plan.id}", "reply": reply}  # fmt: skip
        widget = {
            "widget_type": "zform",
            "extra_data": {"type": "choices", "heading": plan.id, "choices": [choice]},
        }
        await bot.send(t.channel, topic, reply, widget=widget)  # through the channel guard
    finally:
        await bot.close()
    await t.say(f"@**{codex['full_name']}** {reply}")  # what Zulip sends for a click on it
    said = [m for m in await t.silent(first, 30) if m["id"] != warned["id"]]
    assert not said, f"the bot answered again: {[m['content'][:80] for m in said]}"
    assert await pending(t, plan.id), f"{plan.id} changed after the click"
    await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")
    await restart(server, talk(tester, rigged, "global", topic))
    foreign = [r for r in records(server, since) if r.get("event") == "foreign"
               and (r.get("sender") or {}).get("id") == codex["user_id"]]  # fmt: skip
    assert [r.get("messages") for r in foreign] == [3], f"foreign records: {foreign}"


# ---------------------------------------------------------------------------------------- A6


def steer(command: str) -> str:
    if re.search(r"(?<![\w-])--no-claude-steer\b", command):
        return "off"
    return "on" if re.search(r"(?<![\w-])--claude-steer\b", command) else ""


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A6")
@requires("launched", "provisioned")
async def test_a6_rollout(scenario, tester, rigged, server, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A6-{nonce}")
    version = rigged.release
    plan = await t.request(f"!rollout {version} all")
    assert plan.phrase == "rollout 2", f"the phrase: {plan.phrase!r}"
    assert {steer(c) for c in plan.commands} == {""}, "a steer flag without steer="
    # the workshop's mention_after_s (#711), not the default: on the step, and in its dry run
    pushes = [c.split() for c in plan.commands if "push_config" in c]
    assert [mention_after(c) for c in pushes] == [str(MENTION_AFTER_S)], plan.commands
    assert f"\nmention after: {MENTION_AFTER_S} s (" in plan.text, "the dry run's line (#711)"
    started = time.monotonic()
    _, text = await t.approve(plan)
    scenario.timing("rollout of 2 VMs", time.monotonic() - started)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    # A's row is never bound (A2, A8), so its bots' allowlists wait for an owner too (#401)
    for slug, pending in ((A, "owner+sign-in"), (B, "sign-in")):
        said = re.escape(f"ok ({pending} pending)")
        assert re.search(rf"{slug}\b[^\n]*{said}", text), f"{slug}'s line"
    status = await whole(tester, await t.ask("!status all", STATUS_ANSWER, within=PLAN_S))
    vms = status_of(status)  # the table (#439), the attachment's whole one when it is cut
    shown = {slug: (vms.get(slug) or {}).get("zab") for slug in (A, B)}
    assert shown == {A: version[1:], B: version[1:]}, f"!status shows {shown}"
    DONE["rolled_out"] = True  # the bridges run this release, with its config
    for word, line in (("off", "claude steer: OFF"), ("on", "claude steer: ON")):
        plan, text = await t.run(f"!rollout {version} {A} steer={word}", phrase=False)
        assert {steer(c) for c in plan.commands if "push_config" in c} == {word}, plan.commands
        assert line in text, f"steer={word}: no `{line}` in the result"
    for slug in (A, B):  # what each VM's bridge reads (#711)
        done = on_vm(server, ip_of(server, slug), "sudo", "cat", VM_CONFIG)
        assert done.ok, f"{VM_CONFIG} on {slug}: exit {done.returncode}: {done.stderr[-300:]}"
        written = tomllib.loads(done.stdout)["bridge"].get("mention_after_s")
        assert written == MENTION_AFTER_S, f"{slug}'s [bridge] mention_after_s: {written!r}"


def mention_after(argv: list[str]) -> str:
    """The value of a push_config.py step's one `--mention-after-s`, or what is wrong."""
    at = [i for i, word in enumerate(argv[:-1]) if word == "--mention-after-s"]
    return argv[at[0] + 1] if len(at) == 1 else f"{len(at)} --mention-after-s"


#: The bridge's config on a fleet VM, which push_config.py writes (its README §2, step 3).
VM_CONFIG = "/home/agent/.config/zab/config.toml"


#: As `zab-admin`: the words after the workshop and the address, run on that fleet VM through
#: the fleet's own pinned SSH, as status.py reaches it (`admin_common.ssh_argv`, the workshop's
#: `known_hosts`), each word quoted (`shlex.join`): an argument list, never text from Zulip.
ON_VM = r"""
import shlex, subprocess, sys
sys.path.insert(0, "/opt/zab-admin/current/workshop/admin")
from admin_common import ssh_argv
ws, ip, *remote = sys.argv[1:]
known = "/var/lib/zab-admin/ws/" + ws + "/out/known_hosts"
argv = ssh_argv(ip, shlex.join(remote), known_hosts=known)
done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""
#: The file that fills zab-test-a's root file system past 90% (#552), removed after: in
#: `ubuntu`'s home, which no other user can write, unlike a shared temporary directory.
FILL = "/home/ubuntu/zab-e2e-a6-full-disk"
#: What the fill leaves available, of `df`'s Used + Available: `df` then says 93% or so.
LEFT = 0.07
#: `!status`'s line of one VM's resources whose disk is under strain (#552).
FULL_DISK = re.compile(
    r"^- .*\bdisk (\d+)% \(([0-9.]+) GB free\) \(under strain: [^)\n]*\bdisk\)$", re.M
)


def on_vm(server: Any, ip: str, *remote: str) -> Any:
    """`remote` on the fleet VM at `ip`, from the server (`ON_VM`): its `Result`."""
    return server.as_admin("/usr/bin/python3", "-I", "-c", ON_VM, E2E.name, ip, *remote,
                           timeout=180)  # fmt: skip


def ip_of(server: Any, slug: str) -> str:
    text = server.read(f"/var/lib/zab-admin/ws/{E2E.name}/roster.csv")
    row = next((r for r in csv.DictReader(io.StringIO(text)) if r.get("slug") == slug), {})
    assert (row.get("ip") or "").strip(), f"{slug} has no ip in the roster: A4 first"
    return row["ip"].strip()


def root_df(text: str) -> tuple[int, int, int, int]:
    """`df -Pk /`'s data line: 1024-blocks, Used, Available (KiB) and Capacity (%)."""
    parts = text.strip().splitlines()[-1].split()
    return int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4].rstrip("%"))


def fill_kib(used: int, available: int) -> int:
    """KiB to allocate so that Available drops to `LEFT` of Used + Available."""
    return max(0, available - int((used + available) * LEFT))


def df_of(server: Any, ip: str) -> tuple[int, int, int, int]:
    done = on_vm(server, ip, "df", "-Pk", "/")
    assert done.ok, f"df on {ip}: exit {done.returncode}: {done.stderr.strip()[-300:]}"
    return root_df(done.stdout)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A6")
@requires("launched")
async def test_a6_a_full_disk_names_its_free_space(scenario, tester, rigged, server, nonce):
    """#552 on zab-test-a, running since A4 (A7 stops the fleet later): a file in `FILL` takes
    its root file system to about 93%; `!status zab-test-a` names the disk under strain with
    its free space, `df`'s Available in binary GB. The file is removed whatever happens, and
    checked gone; a `!status` after that shows the disk under no strain. Needs no provisioning
    (Q9): the disk is status.py's read, as `zab-monitor` or `ubuntu`."""
    t = talk(tester, rigged, E2E.name, f"e2e-A6-disk-{nonce}")
    ip = ip_of(server, A)
    _, used, available, _ = df_of(server, ip)
    try:
        made = on_vm(server, ip, "fallocate", "-l", f"{fill_kib(used, available)}KiB", FILL)
        assert made.ok, f"fallocate: exit {made.returncode}: {made.stderr.strip()[-300:]}"
        before = df_of(server, ip)
        assert before[3] >= 90, f"{A}'s disk at {before[3]}% after the fill"
        answer = await whole(tester, await t.ask(f"!status {A}", STATUS_ANSWER, within=PLAN_S))
        after = df_of(server, ip)
        found = FULL_DISK.search(answer)
        assert found, f"no disk under strain with its free space: {answer[:800]}"
        free = {pages.disk_gb(df[2] * 1024) for df in (before, after)}  # df's, around the read
        assert int(found[1]) >= 90 and found[2] in free, (found[0], before, after)
        scenario.notes.append(f"{A}'s disk filled to {before[3]}%: `{found[0][2:]}` (#552)")
    finally:
        removed = on_vm(server, ip, "rm", "-f", "--", FILL)
        gone = on_vm(server, ip, "test", "-e", FILL)
        assert removed.ok and gone.returncode == 1, (
            f"{FILL} may remain on {A} ({ip}): rm exit {removed.returncode}, test -e exit "
            f"{gone.returncode}; remove it by hand"
        )
    answer = await whole(tester, await t.ask(f"!status {A}", STATUS_ANSWER, within=PLAN_S))
    assert "GB free" not in answer, f"the disk still under strain: {answer[:800]}"


# ---------------------------------------------------------------------------------------- A7


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A7")
@requires("launched", "rolled_out")
async def test_a7_the_stop_plan_lists_no_running_turn(scenario, tester, rigged, nonce):
    """The plan's status table gives `0` running turns, which only a VM whose bridge A6 rolled
    out reports (`?` means unknown: `running_turns` null). The plan is rejected: the stop is the
    next part's."""
    t = talk(tester, rigged, E2E.name, f"e2e-A7-turns-{nonce}")
    plan = await t.request("!stop zab-test-b")
    turns = status_rows(plan.text).get(B, {}).get("running turns")
    await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")
    assert turns == "0", f"the plan's status table gives zab-test-b's running turns as {turns}"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A7")
@requires("launched")
async def test_a7_stop_and_start(scenario, tester, rigged, aws, nonce):
    """Needs no provisioning (Q9): power.py and status.py work on any launched VM."""
    t = talk(tester, rigged, E2E.name, f"e2e-A7-{nonce}")
    plan = await t.request("!stop zab-test-b")
    assert not plan.phrase, f"one of two VMs takes no phrase: {plan.phrase!r}"
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    await wait_state(aws, B, "stopped")
    started = time.monotonic()
    answer = await t.ask("!status zab-test-b", STATUS_ANSWER, within=PLAN_S)
    # #439: EC2's state, no SSH attempt; over the threshold, A7 fails but runs on (its stops)
    scenario.timing("!status of a stopped VM", time.monotonic() - started, high=STOPPED_STATUS_S)
    shown = status_of(await whole(tester, answer)).get(B, {}).get("state")
    assert shown == "stopped", f"!status shows zab-test-b {shown}: {answer['content'][:300]}"
    await t.run("!start zab-test-b")
    await wait_state(aws, B, "running")
    plan = await t.request("!stop all")
    assert plan.phrase == "stop 2", f"the phrase: {plan.phrase!r}"
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    for slug in (A, B):
        await wait_state(aws, slug, "stopped")
    DONE["stopped"] = True


# --------------------------------------------------------------------------------------- A15


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A15")
@requires("roster")
async def test_a15_two_workshops(scenario, tester, tester2, rigged, admin_rig, server, nonce):
    topic = f"e2e-A15-{nonce}"
    t2 = talk(tester2, rigged, E2E2.name, topic)
    await restart(server, talk(tester, rigged, "global", topic))  # fresh hint windows
    plan = await import_roster(t2, [row(C, "zab-admin-e2e-c@example.com", admin_rig.region)],
                               "zab-admin-e2e2-roster.csv")  # fmt: skip
    _, text = await t2.approve(plan)  # e2e2's own row, by its organizer
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    bot_hint = f"ask @_**{E2E.bot}|"  # the bot's own (`reads.mention`), to the real bot
    hinted = await t2.ask("!status zab-test-a", r"^❌")
    assert f"is in {E2E.name}: {bot_hint}" in hinted["content"], hinted["content"][:200]
    plain = await t2.ask("!status zab-test-a", r"^❌")
    assert bot_hint not in plain["content"], "a second hint within 10 minutes"
    both = [
        row(C, "zab-admin-e2e-c@example.com", admin_rig.region),
        row(A, A_EMAIL, admin_rig.region),
    ]
    with pytest.raises(Refused) as refused:
        await import_roster(t2, both, "zab-admin-e2e2-a.csv")
    said = await refusal(t2, refused.value)  # roster_import.taken_refusal, whole
    assert f"{A} is in {E2E.name}" in said, f"refused for another reason: {said[:300]}"
    assert bot_hint not in said, "the bot's hint again within 10 minutes, on the import"
    served = await talk(tester, rigged, E2E2.name, topic).ask("!roster show", r"zab-test-c|^🚫")
    assert C in served["content"], f"the global admin was not served: {served['content'][:200]}"
    t = talk(tester, rigged, E2E.name, topic)
    plan = await t.request("!stop zab-test-b")
    other = await talk(tester, rigged, E2E2.name, topic).ask(plan.approve, r"^(✅|❌) ")
    assert other["content"].startswith("❌") and "another bot" in other["content"]
    assert await pending(t, plan.id), f"{plan.id} changed after an approval on the other bot"
    await t.ask(f"!reject {plan.id}", rf"{plan.id} rejected")
    posted: dict[str, int] = {}  # plan ID → its plan messages this run, on either bot
    for where in (E2E.name, E2E2.name):
        w = talk(tester, rigged, where, topic)
        for m in await tester.after([{"operator": "channel", "operand": w.channel}], 0):
            head = PLAN_HEAD.match(m["content"])
            if head and m["timestamp"] >= rigged.started and m["sender_id"] == w.bot:
                posted[head.group(1)] = posted.get(head.group(1), 0) + 1
    twice = sorted(pid for pid, n in posted.items() if n > 1)  # on one bot or two, restarts too
    assert not twice, f"plan IDs used twice this run: {twice}"
    scenario.notes.append(f"{len(posted)} plans this run, each ID once")


# ---------------------------------------------------------------------------------------- A8


@contextlib.asynccontextmanager
async def waker_on(t: Talk, slug: str) -> AsyncIterator[None]:
    """The body runs while `slug` is opted in to the waker; `!waker off` comes in a `finally`,
    whatever happened, so that no entry outlives a failed part (review of #381): checked when
    the body passed, best effort after a failure, whose own error is the one reported."""
    passed = False
    try:
        yield
        passed = True
    finally:
        try:
            await t.run(f"!waker off {slug}")
        except Exception:
            if passed:
                raise


#: As root on the server: one start request to the starter, as the waker sends it (#359), the
#: workshop and the instance as arguments, never spliced in; prints the starter's answer.
DIRECT_START = (
    "import json, socket, sys\n"
    "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "s.settimeout(60)\n"
    "s.connect('/run/zab-admin-starter/start.sock')\n"
    "request = {'op': 'start', 'workshop': sys.argv[1], 'instance': sys.argv[2]}\n"
    "s.sendall(json.dumps(request).encode() + b'\\n')\n"
    "print(s.makefile('rb').readline().decode().strip())\n"
)
START_LOG = "/var/lib/zab-starter/log/starts.jsonl"
#: The wake monitor's pass comes every 2 minutes; then its check, then its post.
WAKE_CHECK_S = 420


async def wake_check(server: Any, instance: str, after: int, peer: str) -> dict[str, Any]:
    """The wake monitor's record of a start of `instance` that `peer` asked for, after `after`."""

    def found() -> dict[str, Any] | None:
        mine = [r for r in records(server, after) if r["event"] == "wake_check"
                and r.get("instance") == instance and r.get("peer") == peer]  # fmt: skip
        return mine[-1] if mine else None

    return await until(found, lambda r: r is not None, what=f"the wake check of {instance}",
                       within=WAKE_CHECK_S, every=15)  # fmt: skip


def earlier_alert(server: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    """The alert that held `record` back: a `wake_check` of the same VM that alerted, for a
    start less than an hour before this one (the monitor's per-VM limit, by the starts' times,
    `zab.admin.wakes.ALERT_EVERY`)."""
    found = [r for r in records(server) if r["event"] == "wake_check"
             and r.get("instance") == record["instance"] and type(r.get("alert")) is int
             and r.get("outcome") != "not_opted_in"
             and 0 <= record["start_t"] - float(r.get("start_t", "-inf")) < 3600]  # fmt: skip
    return found[-1] if found else None


async def started_by_root(
    scenario: Any, tester: TestUser, rigged: Any, server: Any, instance: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """`instance` started through the starter itself, as root, as the waker would ask (#359):
    its `wake_check` record, then the alert it names, from the global admin bot in its channel,
    naming the instance and "asked by root". A rerun within the hour of an alert for the same VM
    is `held` instead (the per-VM limit): then the earlier alert must be on record, and there is
    no new one (None). On the way: the start log names root, it is the bots' to read, and
    neither it, its directory nor the starter's state is the waker's to write."""
    after = last_seq(server)
    done = server.run(["sudo", "python3", "-I", "-c", DIRECT_START, E2E.name, instance])
    assert json.loads(done.stdout) == {"ok": True, "result": "started"}, done.stdout[-300:]
    record = await wake_check(server, instance, after, "root")
    alert = None
    if record.get("held") is True:
        earlier = earlier_alert(server, record)
        assert earlier is not None, f"held back, with no alert of {instance} in the hour before"
        scenario.notes.append(f"{instance}: no new alert, held back by the one for its start at "
                              f"{earlier['started']} (a rerun within the hour)")  # fmt: skip
    else:
        assert type(record.get("alert")) is int, record
        alert = await tester.message(record["alert"])
        ids = rigged.ids()
        where = (alert["sender_id"], alert.get("stream_id"))
        assert where == (ids.bots[GLOBAL_BOT][0], ids.channels[ADMINS_CHANNEL]), where
        assert instance in alert["content"] and "asked by root" in alert["content"], alert
    logged = [json.loads(ln) for ln in server.read(START_LOG).splitlines() if instance in ln]
    assert logged and logged[-1]["peer"] == "root", logged[-1:]
    log_dir = START_LOG.rsplit("/", 1)[0]
    for user, flag, path, want in (
        ("zab-waker", "-w", START_LOG, 1),
        ("zab-waker", "-w", log_dir, 1),
        ("zab-waker", "-w", log_dir.rsplit("/", 1)[0], 1),  # /var/lib/zab-starter: log/'s parent
        ("zab-admin", "-r", START_LOG, 0),
    ):
        got = server.run(["sudo", "-u", user, "test", flag, path], check=False).returncode
        assert got == want, f"as {user}, test {flag} {path}: exit {got}"
    return alert, record


@contextlib.asynccontextmanager
async def stopped_after(t: Talk, aws: Any, slug: str) -> AsyncIterator[None]:
    """`slug` stopped before the body and again after it, as A7 leaves it: in a `finally`,
    checked when the body passed, best effort after a failure (as `waker_on`)."""
    if vm_state(aws, slug) != "stopped":
        await t.run(f"!stop {slug}")
        await wait_state(aws, slug, "stopped")
    passed = False
    try:
        yield
        passed = True
    finally:
        try:
            if vm_state(aws, slug) in ("pending", "running"):
                await wait_state(aws, slug, "running", within=300)  # a start ends first
                await t.run(f"!stop {slug}")
                await wait_state(aws, slug, "stopped")
            assert vm_state(aws, slug) == "stopped", f"{slug} is {vm_state(aws, slug)}"
        except Exception:
            if passed:
                raise


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A8")
@requires("launched")
async def test_a8_a_start_that_cant_be_checked_alerts(scenario, tester, rigged, server, aws, nonce):
    """#359 without Q9: `zab-test-a`'s row is never bound (its address is never a Zulip user's:
    no `zulip_user_id` in any run, A2), so its start through the starter, as root, is a failed
    check, which alerts at once with its reason. `zab-test-a`, not `zab-test-b`: the provisioned
    part below starts `zab-test-b`, and a VM's alerts are limited to one an hour. It is stopped
    again after, as A7 leaves it."""
    t = talk(tester, rigged, E2E.name, f"e2e-A8-unbound-{nonce}")
    async with stopped_after(t, aws, A):
        instance = vm_states(aws)[A]["InstanceId"]
        alert, record = await started_by_root(scenario, tester, rigged, server, instance)
        assert record["outcome"] == "failed" and "zulip_user_id" in record["reason"], record
        if alert is not None:
            said = alert["content"]
            assert said.startswith("⚠ **Wake check failed:**") and "no zulip_user_id" in said


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A8")
@requires("launched", "provisioned")
async def test_a8_a_start_with_no_message_alerts(scenario, tester, rigged, server, aws, nonce):
    """#359: a start through the starter itself, as root, with no message from `zab-tester2` to
    `zab-test-b`'s bots before it (none earlier in the run; the waker's part comes next), alerts
    the global admins. `zab-test-b` is stopped again after, whatever happens."""
    t = talk(tester, rigged, E2E.name, f"e2e-A8-monitor-{nonce}")
    async with stopped_after(t, aws, B):
        instance = vm_states(aws)[B]["InstanceId"]
        alert, record = await started_by_root(scenario, tester, rigged, server, instance)
        assert record["outcome"] == "unmatched", (
            f"{record}: a message from zab-tester2 to zab-test-b's bots in the 10 minutes before "
            "explains this start (a rerun right after the waker's part?); run it 10 minutes later"
        )
        if alert is not None:
            assert alert["content"].startswith("⚠ **Wake check:**"), alert["content"][:300]


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A8")
@requires("launched", "provisioned", "rolled_out")
async def test_a8_the_waker(scenario, tester, tester2, rigged, server, aws, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A8-{nonce}")
    answer = await t.ask("!waker on zab-test-b", ANSWER, within=PLAN_S)
    later = ("is not offered through the bots yet", "is not available on this server yet")
    if any(said in answer["content"] for said in later):
        pytest.skip("`!waker` is not offered through the bots yet (stage 4: grammar.LATER)")
    plan = parse_plan(await whole(tester, answer), answer["id"])
    assert plan is not None, answer["content"][:200]
    await t.reply(answer["id"], rf"^{plan.id} `[^`]*`: approve ")  # pending only then
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    config = "/var/lib/zab-admin/waker/config.toml"  # push_waker.py --config-dir's
    async with waker_on(t, B):  # `!waker off zab-test-b` after it, whatever happens
        listed = server.run(["sudo", "grep", "-l", B, config], check=False)
        assert listed.returncode == 0, f"after `!waker on`, {config} does not list {B}"
        own = server.run(["sudo", "-u", "zab-waker", "test", "-r", config], check=False)
        assert own.ok, "zab-waker can't read its own config: the checks below prove nothing"
        if vm_state(aws, B) != "stopped":  # as A7 left it, or not
            await t.run("!stop zab-test-b")
            await wait_state(aws, B, "stopped")
        codex = DONE["provisioned"][f"Codex · {B}"]
        tester2.restrict(channels=tester2.channels, bots=[*tester2.bots, codex["user_id"]])
        after = last_seq(server)
        sent = await tester2.send_dm(codex["user_id"], "!status")
        await wait_state(aws, B, "running", within=300)
        # the bridge answers the DM (§7: `Bridge running`; "the `!login` hint may be missing
        # on a VM just booted": it needs the backend's doctor within 2 s)
        await tester2.wait_reply(dm_narrow(codex["user_id"]), after_id=sent,
                                 senders=[codex["user_id"]], pattern=DM_STATUS_RE,
                                 within=600)  # fmt: skip
        # the wake monitor finds the DM that asked for it (#359; VERIFY V16)
        record = await wake_check(server, vm_states(aws)[B]["InstanceId"], after, "zab-waker")
        assert (record["outcome"], record.get("message")) == ("matched", sent), record
    listed = server.run(["sudo", "grep", "-l", B, config], check=False)
    assert listed.returncode == 1, f"after `!waker off`: grep exit {listed.returncode}"
    secrets = ("secrets/provisioner.zuliprc", "secrets/github-token")
    paths = [f"/etc/zab-admin/{s}" for s in secrets] + ["/var/lib/zab-admin/.ssh/id_ed25519"]
    for path in paths:
        assert server.run(["sudo", "test", "-e", path], check=False).ok, f"no {path}"
        readable = server.run(["sudo", "-u", "zab-waker", "test", "-r", path], check=False)
        assert readable.returncode == 1, f"zab-waker and {path}: exit {readable.returncode}"


WAKER_STATUS = "/var/lib/zab-waker/status/waker.json"
WAKER = "zab-waker.service"
#: #717's busy topic: zab-tester2's asides to `Codex · zab-test-b` (`//`: the bridge leaves them
#: alone), each of about 9,000 characters and edited `BUSY_EDITS` times, so that its bots'
#: newest messages come with edit histories, as Zulip returns them after a workshop day.
BUSY, BUSY_EDITS, BUSY_CHARS = 12, 3, 9000
#: The waker's `ready_at` moves once a run has lasted a minute (`zab.waker.__main__.UP_AFTER`):
#: the exit within 30 s of the change, 5 s for systemd, a minute up, its next write in 30 s.
UP_S = 240
#: The bots say the waker is down once its `ready_at` is 300 s old (`wakerdown.DOWN_AFTER`),
#: at most 30 s after its last write, at their next check, every 60 s: by about 6.5 minutes.
DOWN_S = 600


def aside(nonce: str, i: int, edit: int) -> str:
    text = f"// e2e-A8-busy-{nonce} {i}.{edit}: " + "the waker catches up after a restart. " * 250
    return text[:BUSY_CHARS]


def waker_unit(server: Any) -> dict[str, str]:
    shown = server.run(["systemctl", "show", WAKER, "-p", "ActiveState", "-p", "NRestarts",
                        "-p", "MemoryPeak"]).stdout  # fmt: skip
    return dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)


async def waker_up(server: Any, since: float) -> float:
    """The test workshop's `ready_at` in the waker's status file once it is past `since` plus
    the minute a run needs (#717): the waker came up after a start at `since`."""

    def ready() -> float | None:
        status = json.loads(server.read(WAKER_STATUS) or "{}")
        entry = next((w for w in status.get("workshops", []) if w.get("name") == E2E.name), {})
        value = entry.get("ready_at")
        return float(value) if type(value) in (int, float) else None

    what = "the waker's ready_at after its start"
    return await until(ready, lambda r: r is not None and r > since + 60, what=what, within=UP_S)


async def newest_in(user: TestUser, channel: int) -> int:
    data = await user.client.request("GET", "messages", anchor="newest", num_before=1,
                                     num_after=0, narrow=[{"operator": "channel",
                                                           "operand": channel}])  # fmt: skip
    return max((int(m["id"]) for m in data["messages"]), default=0)


async def alert(user: TestUser, channel: int, bot: int, after: int, start: str, within: float):
    """The workshop bot's post in its alerts channel, topic `waker`, after `after`, starting
    `start` (#717)."""

    async def probe() -> dict[str, Any] | None:
        found = await user.after(topic_narrow(channel, "waker"), after)
        mine = [m for m in found if m["sender_id"] == bot and m["content"].startswith(start)]
        return mine[0] if mine else None

    return await user.wait_for(probe, within=within, what=f"the {start!r} post")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A8")
@requires("launched", "provisioned", "rolled_out")
async def test_a8_the_waker_comes_up_after_a_busy_topic(
    scenario, tester, tester2, rigged, server, aws, nonce
):
    """#717 (criterion 6, and the review of #727): the waker stopped (`systemctl stop`, as an
    OOM-killed waker is to its VMs) until the bots say so, in the alerts channel and `!status`;
    then a config change (`!waker on` again), a busy topic (`BUSY`) and a DM to the stopped VM's
    bot, and at once the waker's start. It comes up without an OOM kill, catches up on what was sent
    while it was down (its journal: `Caught up on`, no `Waker catch-up failed`), and the DM wakes
    the VM through that catch-up; the bots then say it is back, and the audit log has both."""
    t = talk(tester, rigged, E2E.name, f"e2e-A8-busy-{nonce}")
    codex = DONE["provisioned"][f"Codex · {B}"]
    tester2.restrict(channels=tester2.channels, bots=[*tester2.bots, codex["user_id"]])
    ids = rigged.ids()
    alerts, bot = ids.channels[E2E.alerts], ids.bots[E2E.bot][0]

    def clock() -> float:  # the server's, as the waker's ready_at is
        return float(server.run(["date", "+%s"]).stdout.strip())

    began = clock()
    await t.run("!waker on zab-test-b")
    async with waker_on(t, B):
        await waker_up(server, began)  # the opt-in's restart, done
        if vm_state(aws, B) != "stopped":
            await t.run("!stop zab-test-b")
            await wait_state(aws, B, "stopped")
        seq, last = last_seq(server), await newest_in(tester, alerts)
        started: float | None = None
        try:
            server.run(["sudo", "systemctl", "stop", WAKER])
            down = await alert(tester, alerts, bot, last, "⚠ **waker down since ", DOWN_S)
            shown = await t.ask("!status zab-test-b", STATUS_ANSWER, within=PLAN_S)
            assert "⚠ waker down since " in (await whole(tester, shown)), shown["content"][:300]
            # A config change while it is down, before what it must catch up on: the Codex bot
            # has no mark (the last part's `!waker off`, then this opt-in, forgot it), so its
            # catch-up starts from the config's write, 30 s back (the review of #732).
            await t.run("!waker on zab-test-b")
            for i in range(BUSY):  # within CATCH_UP_AGE of the start below
                sent = await tester2.send_dm(codex["user_id"], aside(nonce, i, 0))
                for edit in range(1, BUSY_EDITS + 1):
                    await tester2.edit(sent, aside(nonce, i, edit))
            dm = await tester2.send_dm(codex["user_id"], "!status")
        finally:
            with contextlib.suppress(Exception):  # the start comes, whatever the clock does
                started = clock()
            server.run(["sudo", "systemctl", "start", WAKER])
        assert started is not None, "the server's clock could not be read at the start"
        await wait_state(aws, B, "running", within=300)
        await tester2.wait_reply(dm_narrow(codex["user_id"]), after_id=dm,
                                 senders=[codex["user_id"]], pattern=DM_STATUS_RE,
                                 within=600)  # fmt: skip
        await waker_up(server, started)
        unit = waker_unit(server)
        assert unit["ActiveState"] == "active", unit
        journal = server.run(["sudo", "journalctl", "-u", WAKER, "--no-pager", "-q",
                              "--since", f"@{int(started)}"]).stdout  # fmt: skip
        assert "oom-kill" not in journal, journal[-2000:]
        assert "Caught up on" in journal and "Waker start: " in journal, journal[-2000:]
        assert "Waker catch-up failed" not in journal, journal[-2000:]
        await alert(tester, alerts, bot, down["id"], "✅ **waker back** at ", UP_S + 120)
        kept = [r for r in records(server, seq) if r["event"] == "waker"
                and r.get("workshop") == E2E.name]  # fmt: skip
        assert [r["state"] for r in kept] == ["down", "up"], kept
        scenario.notes.append(f"the waker's MemoryPeak after its start: {unit['MemoryPeak']}; "
                              + "; ".join(ln.split("]: ", 1)[-1] for ln in journal.splitlines()
                                          if "Waker start: " in ln))  # fmt: skip
