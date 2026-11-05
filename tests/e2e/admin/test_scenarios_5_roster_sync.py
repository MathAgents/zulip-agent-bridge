"""A22, the roster from the workshop's channel on the rig (docs/ADMIN.md §3.1a, §7; #627), on
`Workshop admin · e2e2`, whose participants' channel is `#zab-dev-participants-2` (`channel` in the
test admin.toml), with `zab-tester` and `zab-tester2` subscribed (the setup's `ZulipRig`).
`zab-tester2`, e2e2's organizer, sends every command, as ORGANIZERS §3.1 has an organizer send
it, and approves its own change plans.

- **The roster set aside.** e2e2's roster may hold `zab-test-c` (A18): A22 moves it aside, as root
  on the server, so that the sync starts from an empty roster, and puts it back in its `finally`,
  whatever happened (`SET_ASIDE`).
- **`!roster sync` adds a row** for `zab-tester2`, linked by its Zulip ID, and none for
  `zab-tester`: on the rig, the provisioner's key is `zab-tester`'s, and the sync leaves out the
  account it reads the channel with (a Moderator user, as `zab-provisioner` is). The plan names
  the row's slug, made from `zab-tester2`'s full name, which the rest of A22 takes from it.
- **Repeatable:** a second sync has nothing to change: answered `☑`, a success, with no plan
  and no approval asked, and `!plan` reads it `unchanged` (#708).
- **`!roster remove`**, then a sync that doesn't add the row back (nothing to change again,
  `☑`, its dry run listing the row excluded), then **`!roster restore`**.
- **`!roster rename`** to `RENAMED`; `!roster show RENAMED` shows it, with the same Zulip ID.
- **Someone who leaves:** `zab-tester2` unsubscribes itself; the next sync removes its
  row, which nothing was provisioned for. `zab-tester` subscribes it again in the `finally`.

Nothing is provisioned for the row, so no VM, bot, sandbox or repository is made, and nothing is
left but the roster's `.bak` (put back too).
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import pytest

from e2e.admin.harness.rig import E2E2
from e2e.admin.test_scenarios_2_bot import PROVISIONER_Q9, nothing_to_change, talk

#: The row's slug after `!roster rename`: never provisioned, so free again at each run.
RENAMED = "zab-test-sync"
HOME = f"/var/lib/zab-admin/ws/{E2E2.name}"
#: As root: e2e2's roster and its `.bak` set aside (`away`) and put back (`back`), A22's alone.
#: `away` refuses when an earlier set-aside is there (a run cut short before its `finally`),
#: rather than overwrite e2e2's own roster with that run's (review of #641, 6).
SET_ASIDE = r"""set -eu
d=/var/lib/zab-admin/ws/zab-admin-e2e2
case $1 in
  away) for f in roster.csv roster.csv.bak; do
          if [ -e "$d/$f.a22" ]; then
            echo "$d/$f.a22 exists: an earlier A22 was cut short; put it back by hand" >&2
            exit 3
          fi
        done
        for f in roster.csv roster.csv.bak; do
          if [ -e "$d/$f" ]; then mv -- "$d/$f" "$d/$f.a22"; fi
        done ;;
  back) for f in roster.csv roster.csv.bak; do
          rm -f -- "$d/$f"
          if [ -e "$d/$f.a22" ]; then mv -f -- "$d/$f.a22" "$d/$f"; fi
        done ;;
  *) exit 2 ;;
esac
"""
#: roster_sync.py's own line, under the bot's `☑` (#708).
NOTHING = "nothing to change: every subscriber has a row, and every row is theirs"


def rows_of(server: Any) -> dict[str, dict[str, str]]:
    """e2e2's roster on the server, by slug; none when there is no file."""
    found = server.run(["sudo", "cat", "--", f"{HOME}/roster.csv"], check=False)
    text = found.stdout if found.ok else ""
    return {r["slug"]: r for r in csv.DictReader(io.StringIO(text)) if r.get("slug")}


def added(plan_text: str, user_id: int) -> str:
    """The slug of the row the sync's plan adds for `user_id` (`roster_sync.render_sync`)."""
    found = re.search(rf"^\+ ([a-z0-9-]+)  \(Zulip user {user_id}\)", plan_text, re.M)
    assert found, f"the plan adds no row for user {user_id}: {plan_text[:600]}"
    return found.group(1)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A22")
async def test_a22_the_roster_from_the_channel(scenario, tester, tester2, rigged, server, nonce):
    if not rigged.provisioner:
        pytest.skip(PROVISIONER_Q9)
    ids = rigged.ids()
    channel = E2E2.participants
    sid = ids.channels[channel]
    mine = talk(tester2, rigged, E2E2.name, f"e2e-A22-{nonce}")
    members = await tester.client.request("GET", f"streams/{sid}/members")
    if ids.tester2 not in members["subscribers"]:  # a run cut short left it out
        await tester.client.request("POST", "users/me/subscriptions",
                                    subscriptions=[{"name": channel}],
                                    principals=[ids.tester2])  # fmt: skip
    server.script(SET_ASIDE, "away", sudo=True)
    try:
        # the sync: a row for zab-tester2, by Zulip ID; none for zab-tester, the provisioner
        plan = await mine.request("!roster sync")
        slug = added(plan.text, ids.tester2)
        assert f"(user {ids.tester}), whose key reads it, gets no row" in plan.text
        assert f"(Zulip user {ids.tester})" not in plan.text
        assert "roster sync: 1 added, 0 renamed, 0 removed" in plan.text
        _, text = await mine.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**") and "written: " in text, text[:400]
        row = rows_of(server)[slug]
        name = (await tester2.me())["full_name"]
        assert (row["zulip_user_id"], row["name"]) == (str(ids.tester2), name)
        shown = await mine.ask(f"!roster show {slug}", r"zulip_user_id")
        assert f"zulip_user_id: {ids.tester2}" in shown["content"]
        # repeatable: nothing to change, a success with no plan (#708)
        said = await nothing_to_change(mine, "!roster sync")
        assert NOTHING in said and "❌" not in said, said[:600]
        # excluded: a later sync never adds it again
        _, text = await mine.approve(await mine.request(f"!roster remove {slug}"))
        assert text.startswith("✅ ") and rows_of(server)[slug]["excluded"] == "yes", text[:400]
        said = await nothing_to_change(mine, "!roster sync")
        assert NOTHING in said and f"x {slug}: excluded (!roster remove)" in said, said[:600]
        assert list(rows_of(server)) == [slug]
        listing = await mine.ask("!roster show", rf"^```|{slug}")
        line = next(x for x in listing["content"].splitlines() if x.startswith(slug))
        assert line.split()[-1] == "excluded", line
        _, text = await mine.approve(await mine.request(f"!roster restore {slug}"))
        assert text.startswith("✅ ") and not rows_of(server)[slug].get("excluded"), text[:400]
        # renamed: the same row, the same Zulip ID
        _, text = await mine.approve(await mine.request(f"!roster rename {slug} {RENAMED}"))
        assert text.startswith("✅ "), text[:400]
        renamed = rows_of(server)
        assert list(renamed) == [RENAMED] and renamed[RENAMED]["zulip_user_id"] == str(ids.tester2)
        # gone from the channel, nothing provisioned: the row goes
        await tester2.client.request("DELETE", "users/me/subscriptions", subscriptions=[channel])
        plan = await mine.request("!roster sync")
        assert f"- {RENAMED}: not in the channel, and nothing is provisioned for it: removed" in (
            plan.text
        )
        _, text = await mine.approve(plan)
        assert text.startswith("✅ ") and rows_of(server) == {}, text[:400]
        scenario.notes.append(f"{slug} added, excluded, restored, renamed {RENAMED}, removed")
    finally:
        problems = []
        try:
            await tester.client.request("POST", "users/me/subscriptions",
                                        subscriptions=[{"name": channel}],
                                        principals=[ids.tester2])  # fmt: skip
        except Exception as exc:
            problems.append(f"zab-tester2 not subscribed again: {exc}")
        try:
            server.script(SET_ASIDE, "back", sudo=True)
        except Exception as exc:
            problems.append(f"e2e2's roster not put back: {exc}")
        assert not problems, "; ".join(problems)
