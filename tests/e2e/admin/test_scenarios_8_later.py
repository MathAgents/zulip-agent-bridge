"""The later stages' scenarios (docs/ADMIN.md §7, "Later stages"; issue #356): A16.

Each is selectable (`--scenarios A16`, or `all`). A stage that hasn't landed gets a hook here that
reports a skip naming the stage and the PRs it waits for, never a silent gap; the go criteria
(`go`, §8.2) don't include them. When a stage lands, its scenario replaces its hook, to its pass
criteria in §7, with the harness of the other files: `Talk.request` and `Talk.approve` for plans
and phrases, `aws` for the instances, `server` for the files and units on the server. A read of
AWS just after a bot changed it goes through `settle.until`: EC2 may show the change only
seconds later. A9 has its own file, `test_scenarios_8_the_end.py`.

**A16** (stage 7, #418) makes a throwaway workshop through the global admin bot and takes it
apart again: its name is new each run (`THROWAWAY`), since `!new-workshop` makes a new bot and
never reactivates one, and an archived channel keeps its name. The bot is deactivated and the
channels archived (as `zab-tester`, the rig's provisioner and global admin), and the entry, the
key file and the workshop's directory removed, in the scenario's `finally`; the key pair goes
with the run's teardown (`AwsRig.drop_throwaway_key_pairs`), the scenario's AWS view being
read-only.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from typing import Any

import pytest

from e2e.admin.harness.chat import READ_S, Refused, Talk, whole
from e2e.admin.harness.rig import E2E, FLEET_GROUP, GLOBAL_BOT, THROWAWAY
from e2e.admin.test_scenarios_2_bot import PROVISIONER_Q9, records, restart, talk
from e2e.harness.zulip_user import dm_narrow

#: As root: what A16's workshop left on the server, by name (`THROWAWAY-<hex>` only).
FORGET = r"""set -eu
case $1 in zab-dev-e3-[0-9a-f]*) ;; *) echo "refused: $1" >&2; exit 2 ;; esac
rm -rf -- "/var/lib/zab-admin/ws/$1"
rm -f -- "/etc/zab-admin/secrets/bots/$1.zuliprc"
"""
#: A region the rig never sets up, not in its `[global] regions`: refused before any script.
NOT_SET_UP = "eu-north-1"


async def _version_lists(admins: Talk, name: str) -> None:
    """The bots restarted after the apply and loaded the entry: `!version` lists it."""
    for _ in range(24):
        try:
            answer = await admins.ask("!version", r"^zab ", within=10)
        except TimeoutError:
            continue
        if f"- {name}: " in answer["content"]:
            return
    raise AssertionError(f"!version never listed {name}: the bots did not load the entry")


def _stat(server: Any, path: str) -> str:
    return server.run(["sudo", "stat", "-c", "%U:%G %a", "--", path]).stdout.strip()


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A16")
async def test_a16_new_workshop(scenario, tester, tester2, rigged, admin_rig, server, aws, nonce):
    if not rigged.provisioner:
        pytest.skip(PROVISIONER_Q9)
    ids = rigged.ids()
    name = f"{THROWAWAY}-{nonce.rsplit('-', 1)[1]}"
    topic = f"e2e-A16-{nonce}"
    admins = talk(tester, rigged, "global", topic)
    command = f"!new-workshop {name} region={admin_rig.region} like={E2E.name} "
    command += f"organizers={ids.tester2}"
    rig_toml = (admin_rig.etc / "admin.toml").read_text(encoding="utf-8")
    entry: dict[str, Any] = {}
    approved = False
    try:
        # zab-tester2 is no global admin: the server's bot doesn't know them. Its refusal comes
        # once per window (§2.1): the bots restart first, as A1's do, so that a rerun within a
        # window of a run that failed before the apply's restart gets it too (review of #424)
        await restart(server, admins)
        bot = ids.bots[GLOBAL_BOT][0]
        sent = await tester2.send_dm(bot, command)
        await tester2.wait_reply(dm_narrow(bot), after_id=sent, senders=[bot], within=READ_S,
                                 pattern=r"^🚫 .* is not an organizer here")  # fmt: skip
        # a region that isn't set up: refused, nothing made
        with pytest.raises(Refused) as refused:
            await admins.request(command.replace(admin_rig.region, NOT_SET_UP, 1))
        said = await whole(tester, refused.value.message)
        assert said.startswith("❌ P-") and "refused" in said.split("\n")[0], said[:300]
        assert f"region={NOT_SET_UP} is not set up for the fleet" in said, said[:300]
        # the plan, and one global admin's approval, the server class's (#558)
        try:
            plan = await admins.request(command)
        except Refused as exc:
            said = await whole(tester, exc.message)
            raise AssertionError(f"the plan was refused: {said[:600]}") from None
        tester2_name = (await tester2.me())["full_name"]
        assert plan.phrase == f"new-workshop {name}" and plan.workshop == "server"
        fleet = aws.group(FLEET_GROUP, aws.vpc())  # the rig's region set up (BOOTSTRAP §6)
        assert fleet is not None, f"no {FLEET_GROUP} group: the setup makes it"
        for shown in (f'name = "{name}"', f"{ids.tester2}  {tester2_name}",
                      f"#{name}-organizers (private)",
                      f"ok: security group {fleet['GroupId']} ({FLEET_GROUP})",
                      "ok: the role may launch"):  # fmt: skip
            assert shown in plan.text, f"the plan lacks {shown!r}"
        assert "MISSING" not in plan.text and "\ntag = " not in plan.text  # [global]'s scope
        before = hashlib.sha256(server.read("/etc/zab-admin/admin.toml").encode()).hexdigest()
        approved = True
        _, text = await admins.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
        assert f"{name} added to admin.toml; admin.toml.bak kept" in text
        await _version_lists(admins, name)
        # the entry, the .bak, the key, the directory, the record
        toml = tomllib.loads(server.read("/etc/zab-admin/admin.toml"))
        entry = next(ws for ws in toml["workshop"] if ws["name"] == name)
        assert "tag" not in entry  # its scope: workshop=<name> and [global]'s (#425)
        assert entry["organizers"] == [ids.tester2] and entry["aws"]["region"] == admin_rig.region
        assert entry["aws"]["security_group"] == fleet["GroupId"]
        bak = server.read("/etc/zab-admin/admin.toml.bak").encode()
        assert hashlib.sha256(bak).hexdigest() == before
        assert _stat(server, f"/etc/zab-admin/secrets/bots/{name}.zuliprc") == "root:zab-admin 640"
        home = f"/var/lib/zab-admin/ws/{name}"
        assert _stat(server, home) == _stat(server, f"{home}/out") == "zab-admin:zab-admin 700"
        assert _stat(server, f"{home}/roster.csv") == "zab-admin:zab-admin 600"
        change = server.read("/var/lib/zab-admin-config/changes.jsonl").splitlines()[-1]
        assert f'"plan":"{plan.id}"' in change and f'"workshop":"{name}"' in change
        assert any(r["event"] == "config" and r["change"]["plan"] == plan.id
                   for r in records(server))  # fmt: skip
        pairs = aws.aws.ec2("describe_key_pairs", Filters=[
            {"Name": "key-name", "Values": [f"zab-admin-{name}"]}])["KeyPairs"]  # fmt: skip
        assert len(pairs) == 1, "the key pair was not imported"
        # the bot and the channels, read by zab-tester, a member of both
        new_bot, organizers = entry["bot"]["user_id"], entry["bot"]["channels"][0]
        alerts = entry["alerts_channel"]
        members = {}
        for sid in (organizers, alerts):
            found = await tester.client.request("GET", f"streams/{sid}/members")
            members[sid] = set(found["subscribers"])
        assert members[organizers] == {ids.tester, ids.tester2, new_bot}
        assert members[alerts] == {ids.tester, ids.tester2}  # the provisioner is zab-tester
        scenario.notes.append(
            "V11: the provisioner, zab-tester, is a global admin here, so subscribed as one"
        )
        # the new bot answers its organizer, in its channel
        tester2.restrict(channels=[*tester2.channels, organizers], bots=[*tester2.bots, new_bot])
        mine = Talk(tester2, new_bot, organizers, topic)
        answer = await mine.ask("!version", r"^zab ")
        assert f"- {name}: {admin_rig.region}, 1 organizers" in answer["content"]
        # the same name again: refused
        with pytest.raises(Refused) as again:
            await admins.request(command)
        assert "is a workshop of this server already" in await whole(tester, again.value.message)
    finally:
        if approved:
            await _forget(tester, server, name, rig_toml)


async def _forget(tester: Any, server: Any, name: str, toml: str) -> None:
    """A16's workshop taken apart (module doc), each part tried whatever the others do, and
    whatever the apply got to: the bot found by its name among `zab-tester`'s, the channels by
    the IDs the script kept (`new-workshop.json`)."""
    problems, calls = [], []
    try:
        owned = (await tester.client.request("GET", "bots"))["bots"]
        mine = [b for b in owned if b.get("full_name") == f"Workshop admin · {name}"]
        for found in mine:
            user = await tester.client.request("GET", f"users/{found['username']}")
            calls.append(("DELETE", f"bots/{user['user']['user_id']}"))
        state = server.run(["sudo", "cat", "--", f"/var/lib/zab-admin/ws/{name}/new-workshop.json"],
                           check=False)  # fmt: skip
        kept = json.loads(state.stdout) if state.ok and state.stdout.strip() else {}
        made = kept.get("channels") or {}  # new_workshop.py's STATE
        calls += [("DELETE", f"streams/{sid}") for sid in made.values() if type(sid) is int]
    except Exception as exc:
        problems.append(f"what it made: {exc}")
    for method, endpoint in calls:
        try:
            await tester.client.request(method, endpoint)
        except Exception as exc:
            problems.append(f"{method} {endpoint}: {exc}")
    try:
        server.install_etc("admin.toml", toml)
        server.script(FORGET, name, sudo=True)
        server.bot("restart")
    except Exception as exc:
        problems.append(f"the server: {exc}")
    assert not problems, "; ".join(problems)
