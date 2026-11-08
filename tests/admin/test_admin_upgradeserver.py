"""`!upgrade-server` on the `[global]` bot (docs/ADMIN.md §1.6, §2.5; #569): its grammar, its
action, its plan in the deploy class, and the restart into the new release once `current`
moved."""

import dataclasses
import re
from pathlib import Path

import pytest

import zab
from zab.admin import planning, upgradeserver
from zab.admin.actions import DRY_RUN, ActionError
from zab.admin.config import parse_admin_config
from zab.admin.grammar import Command, Context, Rejected, parse
from zab.admin.plans import DONE, PENDING

from .botkit import ADMINS_CH, ALICE, DEE, DREW, GLOBAL, HARVARD, OLGA, incoming, make_site
from .test_admin_newworkshop import DoneRunner, _fail
from .test_admin_planning import LATE, bot_of, posts, request, settle

SHA = "ab" * 32
REPO = 'release_repo = "git@github.com:Example/zab.git"\n'
UPGRADE = f"!upgrade-server v0.3.1 sha256={SHA}"
FINAL = re.compile(r"v\d+\.\d+\.\d+", re.ASCII)


def context(roster=None):
    return Context(GLOBAL, "Workshop admin", FINAL, roster)


# ----------------------------------------------------------------------------- the grammar


def test_the_command_parses_on_the_servers_bot():
    found = parse(UPGRADE, context())
    assert isinstance(found, Command)
    assert (found.verb, found.version, found.sha256) == ("upgrade-server", "v0.3.1", SHA)
    assert found.keywords == frozenset()
    assert parse(found.text(), context()) == found  # its normalized text reads back


def test_downgrade_is_refused_with_the_way_back():
    """No in-band downgrades: the word is refused, wherever it stands, and the reason says how
    a deployer goes back."""
    for text in (f"{UPGRADE} downgrade", f"!upgrade-server v0.3.1 downgrade sha256={SHA}"):
        found = parse(text, context())
        assert isinstance(found, Rejected), text
        assert found.reason.startswith("`!upgrade-server` only goes forward: go back with ")
        assert "`deploy.sh --downgrade`" in found.reason and "BOOTSTRAP §13.4" in found.reason


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("!upgrade-server v0.3.1", "each setting is required"),
        (f"!upgrade-server v0.3.1 sha256={SHA[:-1]}", "takes the 64 hex digits of a SHA-256"),
        (f"!upgrade-server v0.3.1 sha256={SHA.upper()}", "takes the 64 hex digits of a SHA-256"),
        (f"!upgrade-server v0.3.1rc1 sha256={SHA}", "expected a release tag this server takes"),
        (f"!upgrade-server main sha256={SHA}", "expected a release tag this server takes"),
        (f"{UPGRADE} sha256={SHA}", "is given twice"),
        (f"{UPGRADE} now", "is not an argument of !upgrade-server"),
    ],
)
def test_a_malformed_command_is_refused(text, why):
    found = parse(text, context())
    assert isinstance(found, Rejected) and why in found.reason, found


def test_a_workshops_bot_refuses_it():
    from zab.admin.grammar import Roster

    found = parse(UPGRADE, context(Roster(("alice",))))
    assert isinstance(found, Rejected) and "is the server's" in found.reason


# ------------------------------------------------------------------------------ the action


@pytest.fixture
def site(tmp_path):
    made = make_site(tmp_path)
    text = made.config_path.read_text()
    made.config_path.write_text(text.replace("[global]\n", f"{REPO}\n[global]\n", 1))
    return made


def build(site, cmd=None):
    config = parse_admin_config(site.config_path.read_text(), roots=site.roots)
    cmd = cmd or Command("upgrade-server", version="v0.3.1", sha256=SHA)
    return upgradeserver.build(cmd, config, plan="P-9", bot=GLOBAL, layout=site.paths.layout)


def test_the_action_runs_the_script_with_the_commands_values(site):
    action = build(site)
    layout = site.paths.layout
    script = f"{layout.release}/workshop/{upgradeserver.SCRIPT}"
    want = (layout.python, "-E", "-s", "-B", "-u", script, "--plan", "P-9", "--release",
            "v0.3.1", "--sha256", SHA, "--state", layout.upgrade)  # fmt: skip
    (step,) = action.steps
    assert step.argv == want and step.dry_run == (*want, "--dry-run")
    assert (action.verb, action.workshop, action.bot, action.hashed) == (
        "upgrade-server", "", GLOBAL, DRY_RUN
    )  # fmt: skip
    assert action.targets == ("v0.3.1",) and action.timeout == 1800
    assert "--downgrade" not in step.argv and "downgrade" not in action.notes[0]


def test_without_release_repo_it_is_refused_before_any_script(site):
    text = site.config_path.read_text().replace(REPO, "")
    site.config_path.write_text(text)
    with pytest.raises(ActionError, match="the in-band upgrade is off on this server"):
        build(site)


# -------------------------------------------------------------------------- plan and apply


@pytest.fixture
async def server(site):
    runner, restarts = DoneRunner(), []
    srv = site.server(planner=planning.Planning(runner, restart=lambda: restarts.append(1)))
    srv.runner, srv.restarts = runner, restarts
    await srv.start()
    yield srv
    await srv.close()


async def ask(server, text=UPGRADE, sender=DEE, mid=5000):
    return await request(server, text, sender=sender, bot=GLOBAL, stream=ADMINS_CH, mid=mid)


async def say(server, sender, text, mid):
    await bot_of(server, GLOBAL).on_message(incoming(sender, text, stream=ADMINS_CH, mid=mid))
    await settle(server)


def moved(server, value: bool) -> None:
    """`current` as another release than this process's (True), or as this one."""
    here = Path(zab.__file__).resolve().parents[2]
    release = "/nonexistent/releases/v9.9.9" if value else str(here)
    layout = dataclasses.replace(server.paths.layout, release=release)
    server.paths = dataclasses.replace(server.paths, layout=layout)


async def test_one_deployer_approves_and_the_bots_restart_once_current_moved(site, server):
    """§2.5's deploy class: one deployer, no phrase; then, `current` having moved, the bots
    restart into it, holding every workshop until they do."""
    plan = await ask(server)
    assert plan.state == PENDING and (plan.workshop, plan.bot) == ("", GLOBAL)
    needs = plan.requirement
    assert (needs.klass, needs.needed, needs.phrase) == ("deploy", 1, ())
    text = posts(site, GLOBAL)[-2]["content"]
    assert f"`!upgrade-server v0.3.1 sha256={SHA}`" in text and "**Targets** (1): v0.3.1" in text
    assert "compare the plan's SHA-256 and release tree with your own review's" in text
    assert "widget_content" in posts(site, GLOBAL)[-1]  # one click: no phrase
    moved(server, True)
    await say(server, DEE, f"!approve {plan.name} {plan.hash[:6]}", LATE)
    (job,) = server.runner.applied
    assert job.fleet is None and job.action.verb == "upgrade-server"
    assert server.store.get(plan.id).state == DONE and server.restarts == [1]
    assert server.planner.busy("harvard-2026") == plan.id and server.planner.busy("") == plan.id


async def test_a_failed_apply_that_moved_current_restarts_the_bots_too(site, server):
    """#569: whatever the outcome, a process that runs another release than `current` restarts
    into it: its argument lists name `current`'s scripts (review of #894, N5)."""
    server.runner.apply = lambda job: _fail(job)
    plan = await ask(server)
    moved(server, True)
    await say(server, DEE, f"!approve {plan.name} {plan.hash[:6]}", LATE)
    assert server.store.get(plan.id).state == "failed" and server.restarts == [1]


async def test_an_apply_that_left_current_restarts_nothing(site, server):
    plan = await ask(server)
    moved(server, False)
    await say(server, DEE, f"!approve {plan.name} {plan.hash[:6]}", LATE)
    assert server.store.get(plan.id).state == DONE and server.restarts == []
    assert server.planner.busy("harvard-2026") is None and server.planner.busy("") is None


@pytest.mark.parametrize(
    ("sender", "answer"),
    [
        (ALICE, "❌ only a deployer requests `!upgrade-server`"),  # a global admin, no deployer
        (OLGA, "🚫 Olga Organizer is not an organizer here; nothing was done"),
    ],
)
async def test_only_a_deployer_requests_it(site, server, sender, answer):
    await say(server, sender, UPGRADE, 5000)
    assert posts(site, GLOBAL)[-1]["content"] == answer
    assert server.store.db.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


async def test_only_a_deployer_approves_it(site, server):
    plan = await ask(server, sender=DREW)  # a global admin and a deployer
    await say(server, ALICE, f"!approve {plan.name} {plan.hash[:6]}", LATE)
    assert server.store.get(plan.id).state == PENDING and not server.runner.applied
    await say(server, DEE, f"!approve {plan.name} {plan.hash[:6]}", LATE + 1)
    assert server.store.get(plan.id).state == DONE


async def test_the_workshops_bot_and_its_help_send_it_to_the_servers(site, server):
    await bot_of(server, HARVARD).on_message(incoming(DREW, UPGRADE, mid=5000))
    assert "server's: send it to the server's bot" in posts(site, HARVARD)[-1]["content"]
    await say(server, DEE, "!help upgrade-server", 5001)
    assert "!upgrade-server VERSION sha256=" in posts(site, GLOBAL)[-1]["content"]
