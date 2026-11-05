"""The rig's Zulip side, offline with a fake Zulip (issue #356): made once, then reused."""

from __future__ import annotations

import dataclasses

import pytest

from e2e.admin.fakes import FakeZulip
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.gate import Gate
from e2e.admin.harness.zulip_rig import (
    PLACEHOLDER,
    PLACEHOLDER_EMAIL,
    ZulipRig,
    secret_values,
    stage_secrets,
)

TESTER, TESTER2 = 11, 12
SITE, MINE = "https://zulip.example", frozenset({"t@zulip.example"})


def setup(tmp_path, *, dry=False, zulip=None):
    rig = rigs.load({"HOME": str(tmp_path)})
    zulip = zulip or FakeZulip(TESTER, users=[{"user_id": TESTER, "email": "t@zulip.example"}])
    lines: list[str] = []
    gate = Gate(dry_run=dry, out=lines.append)
    return ZulipRig(gate, zulip, FakeZulip(TESTER2), rig), zulip, rig, lines


async def test_a_fresh_realm_gets_the_bots_and_channels(tmp_path):
    zrig, zulip, rig, _ = setup(tmp_path)
    ids = await zrig.ensure()
    made = [p for e, p in zulip.posts() if e == "bots"]
    assert [p["full_name"] for p in made] == [rigs.E2E.bot, rigs.E2E2.bot, rigs.GLOBAL_BOT]
    assert all(p["bot_type"] == 1 for p in made)  # generic bots
    channels = {p["subscriptions"][0]["name"]: p for e, p in zulip.posts() if "subscr" in e}
    assert set(channels) == {"zab-dev-organizers", "zab-dev-organizers-2", "zab-dev-admins",
                             "zab-dev-alerts", "zab-dev-alerts-2",
                             "zab-dev-participants-2"}  # fmt: skip
    assert all(p["invite_only"] is True for p in channels.values())
    e2e_bot, global_bot = ids.bots[rigs.E2E.bot][0], ids.bots[rigs.GLOBAL_BOT][0]
    assert channels["zab-dev-organizers"]["principals"] == sorted({TESTER, TESTER2, e2e_bot})
    assert channels["zab-dev-admins"]["principals"] == sorted({TESTER, global_bot})
    # ADMIN §2.9: the organizers, the global admins and the provisioner; no bot of the rig's
    assert channels["zab-dev-alerts"]["principals"] == [TESTER]
    assert channels["zab-dev-alerts-2"]["principals"] == [TESTER, TESTER2]
    assert channels["zab-dev-participants-2"]["principals"] == [TESTER, TESTER2]  # A22
    rc = rig.secrets / "bots" / "zab-admin-e2e.zuliprc"
    assert rc.read_text() == (
        f"[api]\nemail=zab-dev-admin-e2e-bot@zulip.example\nkey=k{e2e_bot}\n"
        "site=https://zulip.example\n"
    )
    assert oct(rc.stat().st_mode & 0o777) == "0o600"
    assert oct(rc.parent.stat().st_mode & 0o777) == "0o700"
    assert (rig.secrets / "global.zuliprc").exists()
    assert set(ids.channels) == set(channels) and all(ids.channels.values())


async def test_a_second_run_posts_nothing_and_adds_only_who_is_missing(tmp_path):
    zrig, zulip, _, _ = setup(tmp_path)
    ids = await zrig.ensure()
    before = len(zulip.posts())
    again = await setup(tmp_path, zulip=zulip)[0].ensure()
    assert len(zulip.posts()) == before and again == ids
    sid = ids.channels["zab-dev-admins"]
    zulip.members[sid].remove(TESTER)  # someone unsubscribed zab-tester
    await setup(tmp_path, zulip=zulip)[0].ensure()
    assert zulip.posts()[-1] == (
        "users/me/subscriptions",
        {"principals": [TESTER], "subscriptions": [{"name": "zab-dev-admins"}]},
    )


async def test_a_dry_run_posts_and_writes_nothing(tmp_path):
    zrig, zulip, rig, lines = setup(tmp_path, dry=True)
    ids = await zrig.ensure()
    assert zulip.posts() == [] and not rig.secrets.exists()
    assert sum("would zulip POST bots" in line for line in lines) == 3
    assert ids.bots[rigs.E2E.bot][0] == 0  # a placeholder, in the printed admin.toml
    assert not any("k1" in line or "api_key=k" in line for line in lines)


def test_staged_secrets_and_the_sweep_values(tmp_path):
    """Q9: placeholders until Drew's token and his yes to zab-tester's key are given."""
    rig = rigs.load({"HOME": str(tmp_path)})
    gate = Gate(dry_run=False, out=lambda line: None)
    assert stage_secrets(gate, rig, SITE, MINE) == (False, False)
    token = (rig.secrets / "github-token").read_text()
    assert token.startswith(PLACEHOLDER) and len(token.strip()) == len(PLACEHOLDER) + 32
    provisioner = (rig.secrets / "provisioner.zuliprc").read_text()
    assert PLACEHOLDER_EMAIL in provisioner and "site=https://zulip.example" in provisioner
    assert stage_secrets(gate, rig, SITE, MINE) == (False, False)
    assert (rig.secrets / "github-token").read_text() == token  # kept: the server has it
    assert (rig.secrets / "provisioner.zuliprc").read_text() == provisioner
    for path in (rig.secrets, rig.secrets / "github-token", rig.secrets / "provisioner.zuliprc"):
        assert path.stat().st_mode & 0o077 == 0, path  # nothing for group or others
    rig.github_token.write_text("github_pat_real\n")
    rig.provisioner_zuliprc.write_text(  # someone else's key, a production one say: refused
        "[api]\nemail=provisioner@mathagents.example\nkey=abcdefghijklmnopqrstuvwxyz012345\n"
    )
    with pytest.raises(ValueError, match="not zab-tester's"):
        stage_secrets(gate, rig, SITE, MINE)
    assert (rig.secrets / "provisioner.zuliprc").read_text() == provisioner  # unchanged
    rig.provisioner_zuliprc.write_text(  # zab-tester's, but of another realm: refused
        "[api]\nemail=t@zulip.example\nkey=abcdefghijklmnopqrstuvwxyz012345\n"
        "site=https://other.zulip.example\n"
    )
    with pytest.raises(ValueError, match="another realm"):
        stage_secrets(gate, rig, SITE, MINE)
    assert (rig.secrets / "provisioner.zuliprc").read_text() == provisioner  # unchanged
    rig.provisioner_zuliprc.write_text(
        "[api]\nemail = T@zulip.example\nkey=abcdefghijklmnopqrstuvwxyz012345\n"
        "site = https://zulip.example/\n"
    )  # zab-tester's own, in any case, on the rig's realm
    assert stage_secrets(gate, rig, SITE, MINE) == (True, True)
    rig.github_token.unlink()
    assert stage_secrets(gate, rig, SITE, MINE) == (False, True)
    assert (rig.secrets / "github-token").read_text().startswith(PLACEHOLDER)  # given back
    rig.github_token.write_text("github_pat_real\n")
    assert stage_secrets(gate, rig, SITE, MINE) == (True, True)
    spaced = rig.secrets / "bots" / "spaced.zuliprc"  # INI allows `key = …` (Codex round 1)
    spaced.parent.mkdir(parents=True)
    spaced.write_text("[api]\nemail = b@zulip.example\nkey = ZYXWVUTSRQPONMLKJIHGFEDCBA987654\n")
    values = secret_values(rig)
    assert values == {
        "bots/spaced.zuliprc": "ZYXWVUTSRQPONMLKJIHGFEDCBA987654",
        "github-token": "github_pat_real",
        "provisioner.zuliprc": "abcdefghijklmnopqrstuvwxyz012345",
    }


async def test_only_zab_dev_channels_and_nothing_posted_otherwise(tmp_path, monkeypatch):
    """Only the dev org's `zab-dev*` channels, checked at run time before any call (review of #363,
    item 10)."""
    from e2e.admin.harness import zulip_rig

    zrig, zulip, _, _ = setup(tmp_path)
    monkeypatch.setattr(zulip_rig, "ADMINS_CHANNEL", "admins")
    with pytest.raises(ValueError, match="zab-dev"):
        await zrig.ensure()
    assert zulip.calls == []
    monkeypatch.undo()  # the alerts channels too (review of #381)
    monkeypatch.setattr(zulip_rig, "E2E2", dataclasses.replace(rigs.E2E2, alerts="alerts-2"))
    with pytest.raises(ValueError, match="zab-dev"):
        await zrig.ensure()
    assert zulip.calls == []


async def test_a_bot_of_that_name_owned_by_someone_else_is_never_used(tmp_path):
    """Review of #363, item 7: an admin's `GET /bots` may list others' bots."""
    users = [
        {"user_id": TESTER, "email": "t@zulip.example"},
        {"user_id": 50, "email": "other-bot@zulip.example", "is_bot": True, "bot_owner_id": 99},
    ]
    bots = [{"username": "other-bot@zulip.example", "full_name": rigs.E2E.bot, "api_key": "x"}]
    zrig, zulip, rig, _ = setup(tmp_path, zulip=FakeZulip(TESTER, users=users, bots=bots))
    with pytest.raises(LookupError, match="not owned by zab-tester"):
        await zrig.ensure()
    assert zulip.posts() == [] and not rig.secrets.exists()


async def test_a_bot_the_bulk_user_list_leaves_out_is_found_by_its_address(tmp_path):
    """The second live setup: `GET /users` omitted `Workshop admin · test` (made by the first
    run), though `GET /users/{email}` found it, active and zab-tester's. It is reused, not made
    again and not refused."""
    zrig, zulip, rig, _ = setup(tmp_path)
    ids = await zrig.ensure()
    global_bot = ids.bots[rigs.GLOBAL_BOT]
    zulip.hidden = [u for u in zulip.users if u["user_id"] == global_bot[0]]
    zulip.users = [u for u in zulip.users if u["user_id"] != global_bot[0]]
    before = len(zulip.posts())
    again = await setup(tmp_path, zulip=zulip)[0].ensure()
    assert again.bots[rigs.GLOBAL_BOT] == global_bot and len(zulip.posts()) == before
    assert (rig.secrets / "global.zuliprc").read_text().count(f"key=k{global_bot[0]}") == 1
