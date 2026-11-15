"""A deploy's restart and the plans in flight (docs/ADMIN.md §2.4 step 6; #989), on the bots of
`botkit` with the real `Runner` and fake scripts (`test_admin_runner`'s rig). A restart is the
server's close and a new start, as systemd's restart of the bots is; a deploy, a new line of
`deploys.jsonl`, which the new start copies, so that the server runs another release.

What a restart does to each state, as #989 recorded it: a plan being planned is interrupted; a
pending one stays pending, and its approval then finds it STALE, since its hash names the old
release (applying and approved: `test_admin_runner`'s restart tests). Then the in-band upgrade:
its approval is answered `busy:` while a plan of any workshop applies, naming that plan's bot,
and a workshop's approval while the upgrade applies names the server's; its plan names the
plans in flight when it is made."""

import json
from pathlib import Path

import pytest

from zab.admin.plans import APPLYING, INTERRUPTED, PENDING, STALE

from .botkit import (
    ADMINS_CH,
    DEE,
    GLOBAL,
    HARVARD,
    OLGA,
    OSCAR,
    TESS,
    TEST,
    TEST_CH,
    incoming,
    until,
)
from .test_admin_runner import LATE, approve, bot_of, fake, posts, request, settle, start
from .test_admin_runner import site as site  # the runner's rig: its fixture

DEPLOY = {"ts": "2026-10-20T10:00:00Z", "event": "bootstrap", "version": "v0.3.0",
          "commit": "a" * 40, "tree": "b" * 64, "deployer": "drew"}  # fmt: skip
UPGRADE = DEPLOY | {"ts": "2026-10-21T10:00:00Z", "event": "upgrade", "version": "v0.3.1",
                    "commit": "c" * 40}  # fmt: skip
REPO = 'release_repo = "git@github.com:Example/zab.git"\n'
SHA = "ab" * 32


def deployed(site, *records) -> None:
    """`deploys.jsonl` as deploy.sh leaves it: root's, 0644, one line per deploy."""
    path = Path(site.paths.deploys)
    path.parent.mkdir(mode=0o755, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    path.chmod(0o644)


def held(site, name: str, *, dry: bool) -> Path:
    """A fake `workshop/<name>` whose dry run (`dry`) or apply waits, once it has begun, until
    the file it returns exists."""
    hold, began = site.tmp / f"hold-{name.replace('/', '-')}", site.tmp / "began"
    test = "DRY" if dry else "not DRY"
    fake(site, name, f"""\
        if {test}:
            pathlib.Path({str(began)!r}).write_text("x")
            while not pathlib.Path({str(hold)!r}).exists():
                time.sleep(0.02)
    """)  # fmt: skip
    return hold


async def sent(srv, text, *, sender=OLGA, bot=HARVARD, stream=None, mid=5000):
    """`text` to `bot`, without waiting for the plans' tasks: one of them is held."""
    where = {"stream": stream} if stream is not None else {}
    await bot_of(srv, bot).on_message(incoming(sender, text, mid=mid, **where))


# ------------------------------------------------------------------ a restart, by state


async def test_a_restart_interrupts_a_plan_being_planned(site):
    held(site, "aws/power.py", dry=True)
    srv = await start(site)
    try:
        await sent(srv, "!stop alice")
        await until(lambda: (site.tmp / "began").exists())
        assert srv.store.get(1).state == "planning"
    finally:
        await srv.close()  # the restart
    srv = await start(site)
    try:
        assert (srv.store.get(1).state, srv.store.get(1).note) == (INTERRUPTED, "the bot restarted")
    finally:
        await srv.close()
    assert posts(site)[-1].startswith(
        "⏹ P-1 was interrupted by a restart of the bots: nothing resumes on its own. Check "
        "`!status`, then request again."
    )


async def test_a_pending_plan_outlives_a_deploy_but_its_approval_finds_it_stale(site):
    deployed(site, DEPLOY)
    srv = await start(site)
    try:
        plan = await request(srv, "!stop alice")
        assert (srv.release, plan.state) == ("v0.3.0+aaaaaaaaaaaa", PENDING)
    finally:
        await srv.close()
    deployed(site, DEPLOY, UPGRADE)
    srv = await start(site)
    try:
        assert (srv.release, srv.store.get(plan.id).state) == ("v0.3.1+cccccccccccc", PENDING)
        await approve(srv, plan)
        await settle(srv)
        stale = srv.store.get(plan.id)
    finally:
        await srv.close()
    assert stale.state == STALE and stale.note.startswith("hash now ")
    assert posts(site)[-1].startswith(
        f"❌ {plan.name} is stale ({stale.note}; since the plan: the server's release, "
        "v0.3.0+aaaaaaaaaaaa → v0.3.1+cccccccccccc): nothing was done. Request again."
    )


# ---------------------------------------------------------------------------- in-band


@pytest.fixture
def upgradable(site):
    """The runner's rig with `release_repo`, and a fake upgrade script whose dry run is the
    plan and whose apply returns at once."""
    text = site.config_path.read_text()
    site.config_path.write_text(text.replace("[global]\n", f"{REPO}\n[global]\n", 1))
    fake(site, "admin/upgrade_server.py", 'print("the plan" if DRY else "installed")')
    return site


async def planned(srv, text, *, sender, bot, stream, mid):
    """`text` requested; its plan once it is posted (or refused), without waiting for the
    other plans' tasks."""
    before = srv.store.db.execute("SELECT COALESCE(MAX(id), 0) FROM plans").fetchone()[0]
    await sent(srv, text, sender=sender, bot=bot, stream=stream, mid=mid)
    await until(lambda: (p := srv.store.get(before + 1)) is not None and p.state != "planning")
    return srv.store.get(before + 1)


async def test_the_upgrades_approval_is_busy_naming_the_bot_of_the_plan_applying(upgradable):
    site = upgradable
    hold = held(site, "aws/power.py", dry=False)
    srv = await start(site)
    try:
        plan = await request(srv, "!stop alice")
        await approve(srv, plan)
        await until(lambda: (site.tmp / "began").exists())
        assert srv.store.get(plan.id).state == APPLYING
        up = await planned(srv, f"!upgrade-server v0.3.1 sha256={SHA}", sender=DEE, bot=GLOBAL,
                           stream=ADMINS_CH, mid=5001)  # fmt: skip
        assert up.state == PENDING, up.note
        notes = posts(site, GLOBAL)[-2]
        assert (
            f"**Applying now:** {plan.name} on harvard-2026's bot (`!stop`). This plan's approval "
            "is answered `busy:` until each has ended"
        ) in notes
        await approve(srv, up, sender=DEE, bot=GLOBAL, stream=ADMINS_CH, mid=LATE + 1)
        assert posts(site, GLOBAL)[-1] == (
            f"busy: {plan.name} is applying on harvard-2026's bot. Nothing was recorded: send "
            f"the approval again once it is done, before {up.name} expires."
        )
        assert srv.store.get(up.id).state == PENDING
        hold.write_text("go")
        await settle(srv)
    finally:
        await srv.close()


async def test_a_workshops_approval_names_the_servers_bot_while_the_upgrade_applies(upgradable):
    site = upgradable
    hold = held(site, "admin/upgrade_server.py", dry=False)
    srv = await start(site)
    try:
        plan = await request(srv, "!stop alice")
        up = await planned(srv, f"!upgrade-server v0.3.1 sha256={SHA}", sender=DEE, bot=GLOBAL,
                           stream=ADMINS_CH, mid=5001)  # fmt: skip
        await approve(srv, up, sender=DEE, bot=GLOBAL, stream=ADMINS_CH, mid=LATE)
        await until(lambda: (site.tmp / "began").exists())
        await approve(srv, plan, mid=LATE + 1)
        assert posts(site)[-1] == (
            f"busy: {up.name} is applying on the server's bot. Nothing was recorded: send the "
            f"approval again once it is done, before {plan.name} expires."
        )
        hold.write_text("go")
        await settle(srv)
    finally:
        await srv.close()


async def test_a_plan_of_the_same_bot_keeps_its_busy_answer(site):
    hold = held(site, "aws/power.py", dry=False)
    srv = await start(site)
    try:
        one, two = await request(srv, "!stop alice"), await request(srv, "!stop bob", mid=5001)
        await approve(srv, one)
        await until(lambda: (site.tmp / "began").exists())
        await approve(srv, two, sender=OSCAR, mid=LATE + 1)
        assert posts(site)[-1].startswith(f"busy: {one.name} is applying. Nothing was recorded")
        hold.write_text("go")
        await settle(srv)
    finally:
        await srv.close()


async def test_the_upgrade_plan_names_the_plans_in_flight(upgradable):
    """Pending on one workshop's bot, being planned on another's: named, with what the bots'
    restart does to each; the upgrade's own plan is never among them."""
    site = upgradable
    hold = held(site, "aws/power.py", dry=True)
    srv = await start(site)
    try:
        fake(site, "aws/power.py", 'print("would stop")')  # harvard's plan, at once
        pending = await request(srv, "!stop alice")
        hold = held(site, "aws/power.py", dry=True)  # zab-test's, held in its dry run
        await sent(srv, "!stop erin", sender=TESS, bot=TEST, stream=TEST_CH, mid=5001)
        await until(lambda: (site.tmp / "began").exists())
        up = await planned(srv, f"!upgrade-server v0.3.1 sha256={SHA}", sender=DEE, bot=GLOBAL,
                           stream=ADMINS_CH, mid=5002)  # fmt: skip
        text = posts(site, GLOBAL)[-2]
        assert (
            f"**Pending now:** {pending.name} on harvard-2026's bot (`!stop`). Each stays pending "
            "through the bots' restart, but its hash names the release that runs now: approved "
            "after the upgrade, it is STALE, and is requested again."
        ) in text
        assert (
            "**Being planned now:** P-2 on zab-test's bot (`!stop`). The bots' restart, after "
            "this plan's result, interrupts each one still being planned then"
        ) in text
        assert "Applying now" not in text and f"{up.name} on the server's bot" not in text
        hold.write_text("go")
        await settle(srv)
    finally:
        await srv.close()


async def test_with_nothing_in_flight_the_upgrade_plan_has_no_such_note(upgradable):
    srv = await start(upgradable)
    try:
        up = await planned(srv, f"!upgrade-server v0.3.1 sha256={SHA}", sender=DEE, bot=GLOBAL,
                           stream=ADMINS_CH, mid=5000)  # fmt: skip
        assert up.state == PENDING
        text = posts(upgradable, GLOBAL)[-2]
        assert not any(w in text for w in ("Applying now", "Pending now", "Being planned now"))
    finally:
        await srv.close()
