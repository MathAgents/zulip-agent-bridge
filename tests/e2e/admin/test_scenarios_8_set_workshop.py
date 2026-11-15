"""A27, an existing workshop's entry changed from Zulip (docs/ADMIN.md §2.10, §7; #1043):
`!set-workshop` and `!show-workshop` on `Workshop admin · test`, as `zab-tester`, the rig's
global admin.

It changes e2e2's entry (`zab-admin-e2e2`), whose image only A18 launches from, and depends on
no other scenario: it reads the server's admin.toml first, installs it again with e2e2's `ami`
set to `PLACEHOLDER` (an image ID that exists nowhere) and no `gifts_step`, and restarts the
bots, so that `ami=newest` has something to change whatever the rig's images are; its `finally`
puts the server's admin.toml back and restarts the bots. The region's newest image in the rig's
scope, which `ami=newest` must pick, is read from AWS (read-only), by `!new-workshop`'s rule.

1. **Refused at once,** with no plan: an unknown key, a repeated one, `gifts_step=none` on an
   entry without one, and `ami=` the image the entry has.
2. **A named image that isn't the region's:** the plan is refused, its dry run's `MISSING:`.
3. **`ami=newest` and a `gifts_step`:** the plan, in the server class with no phrase, shows each
   line old and new and the image's `ok:` checks; approved, its result ends with the config
   helper's line; the bots restart, and `!show-workshop` then shows both values; `admin.toml.bak`
   is the file before; the helper's record and the audit chain's `config` record name the
   workshop and each key, old and new.
4. **`gifts_step=none`,** approved: `!show-workshop` shows none.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from typing import Any

import pytest

from e2e.admin.harness.aws_rig import newest, tag_filters
from e2e.admin.harness.chat import Refused, Talk, whole
from e2e.admin.harness.rig import E2E2, SCOPE, TEST_ROLE
from e2e.admin.test_scenarios_2_bot import records, restart, talk

ETC = "/etc/zab-admin/admin.toml"
#: e2e2's image while A27 runs: no image has it, so the newest is always a change.
PLACEHOLDER = "ami-0e2e0000000000a27"
NOWHERE = "ami-0e2e0000000000b27"  # a named image that isn't the region's
GIFTS = 'Accept your "gift" plans: Claude Max through [the A27 form](https://example.org/a27).'


def with_ami(toml: str, name: str, ami: str) -> str:
    """`toml` with workshop `name`'s `ami` set to `ami` and its `gifts_step` taken out, that
    entry's lines alone (the rig's workshops share one image)."""
    head, *entries = toml.split("[[workshop]]\n")
    out = []
    for entry in entries:
        if f'\nname = "{name}"\n' in f"\n{entry}":
            entry = re.sub(r'(?m)^ami = "ami-[0-9a-f]+"$', f'ami = "{ami}"', entry, count=1)
            entry = re.sub(r"(?m)^gifts_step = .*\n", "", entry)
        out.append(entry)
    return "[[workshop]]\n".join([head, *out])


def entry_of(toml: str, name: str) -> dict[str, Any]:
    return next(ws for ws in tomllib.loads(toml)["workshop"] if ws["name"] == name)


def newest_image(aws: Any) -> str | None:
    """The region's newest available image of this account in the rig's scope (`zab-scope`,
    and `[global]`'s `zab-role=test`), as `new_workshop.newest_image` picks it; None for a tie,
    which the plan would refuse."""
    filters = [*tag_filters(dict([SCOPE, TEST_ROLE])), {"Name": "state", "Values": ["available"]}]
    images = aws.aws.ec2("describe_images", Owners=["self"], Filters=filters)["Images"]
    top = newest(images)
    ties = [i for i in images if i.get("CreationDate") == top.get("CreationDate")]
    return top["ImageId"] if len(ties) == 1 else None


async def shown(admins: Talk, name: str, want: str, *, gone: str = "") -> str:
    """`!show-workshop`'s answer once the bots, restarted, show `want` (and no `gone`)."""
    content = ""
    for _ in range(24):
        try:
            answer = await admins.ask(f"!show-workshop {name}", rf"^\*\*{name}\*\*", within=10)
        except TimeoutError:
            continue
        content = answer["content"]
        if want in content and not (gone and gone in content):
            return content
    raise AssertionError(f"!show-workshop never showed {want!r}: {content[:600]}")


async def refused_at_once(admins: Talk, command: str, why: str) -> None:
    with pytest.raises(Refused) as refused:
        await admins.request(command)
    said = await whole(admins.user, refused.value.message)
    assert said.startswith("❌") and why in said, said[:300]


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A27")
async def test_a27_set_workshop(scenario, tester, rigged, server, aws, nonce):
    name = E2E2.name
    admins = talk(tester, rigged, "global", f"e2e-A27-{nonce}")
    image = newest_image(aws)
    if image is None:
        pytest.skip("two images of the rig's scope are the newest: !set-workshop refuses a tie")
    rig_toml = server.read(ETC)
    start = with_ami(rig_toml, name, PLACEHOLDER)
    assert entry_of(start, name)["aws"]["ami"] == PLACEHOLDER
    assert "gifts_step" not in entry_of(start, name)
    try:
        server.install_etc("admin.toml", start)
        await restart(server, admins)
        await shown(admins, name, f'ami = "{PLACEHOLDER}"')
        # 1. refused at once: no plan
        set_ = f"!set-workshop {name}"
        await refused_at_once(admins, f"{set_} region=us-east-2", "is not an option")
        await refused_at_once(admins, f"{set_} ami=newest ami=newest", "is given twice")
        await refused_at_once(admins, f"{set_} gifts_step=none", "has no gifts_step")
        await refused_at_once(admins, f"{set_} ami={PLACEHOLDER}", "nothing to change")
        # 2. a named image that isn't the region's: the plan refused
        with pytest.raises(Refused) as refused:
            await admins.request(f"{set_} ami={NOWHERE}")
        said = await whole(tester, refused.value.message)
        assert f"MISSING: no available image {NOWHERE} tagged" in said, said[:600]
        # 3. the newest image and a gift step
        plan = await admins.request(f'{set_} ami=newest gifts_step="{GIFTS}"')
        assert plan.workshop == "server" and plan.phrase == ""
        for line in (f'- ami = "{PLACEHOLDER}"', f'+ ami = "{image}"', "- gifts_step: none",
                     f"+ gifts_step = {json.dumps(GIFTS)}", f"ok: image {image} (",
                     "ok: the role may launch"):  # fmt: skip
            assert line in plan.text, f"the plan lacks {line!r}"
        assert "MISSING" not in plan.text
        before = hashlib.sha256(server.read(ETC).encode()).hexdigest()
        _, text = await admins.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
        assert f"{name}'s entry changed in admin.toml; admin.toml.bak kept" in text
        after = await shown(admins, name, f'ami = "{image}"')
        assert f"gifts_step = {json.dumps(GIFTS)}" in after
        bak = server.read("/etc/zab-admin/admin.toml.bak").encode()
        assert hashlib.sha256(bak).hexdigest() == before
        now = entry_of(server.read(ETC), name)
        assert now["aws"]["ami"] == image and now["gifts_step"] == GIFTS
        change = json.loads(server.read("/var/lib/zab-admin-config/changes.jsonl").splitlines()[-1])
        assert (change["event"], change["plan"], change["workshop"]) == (
            "set-workshop", plan.id, name)  # fmt: skip
        assert change["changes"] == {"ami": {"old": PLACEHOLDER, "new": image},
                                     "gifts_step": {"old": None, "new": GIFTS}}  # fmt: skip
        assert any(r["event"] == "config" and r["change"].get("plan") == plan.id
                   for r in records(server))  # fmt: skip
        # 4. the gift step removed
        plan = await admins.request(f"{set_} gifts_step=none")
        for line in (f"- gifts_step = {json.dumps(GIFTS)}", "+ gifts_step: none"):
            assert line in plan.text, f"the plan lacks {line!r}"
        _, text = await admins.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
        await shown(admins, name, f'ami = "{image}"', gone="gifts_step")
        assert "gifts_step" not in entry_of(server.read(ETC), name)
    finally:
        server.install_etc("admin.toml", rig_toml)
        await restart(server, admins)
    scenario.notes.append(f"ami=newest picked {image}, the newest image of the rig's scope")
