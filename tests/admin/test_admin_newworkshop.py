"""`!new-workshop` on the `[global]` bot (docs/ADMIN.md §2.9, §2.5; #418): its action, built from
admin.toml; its plan, in the server class; its apply, which holds every workshop's lock and then
restarts the bots."""

import dataclasses
import re
from pathlib import Path

import pytest

from zab.admin import confighelper, newworkshop
from zab.admin.actions import DRY_RUN, ActionError, Layout
from zab.admin.bot import Paths as BotPaths
from zab.admin.config import parse_admin_config
from zab.admin.grammar import Command
from zab.admin.planning import Planning
from zab.admin.plans import APPROVED, DONE, PENDING

from .botkit import (
    ADMINS_CH,
    ALICE,
    DEE,
    DREW,
    GLOBAL,
    HARVARD,
    OLGA,
    PAT,
    TESS,
    incoming,
    make_site,
)
from .test_admin_planning import LATE, FakeRunner, bot_of, posts, records, request, settle

UNITS = Path(__file__).resolve().parents[2] / "workshop" / "adminserver"
NEW = "!new-workshop mit-2027 region=us-east-1 like=harvard-2026 organizers=103,105"
SCOPE = (("zab-scope", "mathagents"),)


def command(**changes):
    base = dict(ws_name="mit-2027", region="us-east-1", like="harvard-2026", organizers=(103, 105))
    return Command("new-workshop", **(base | changes))


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def config_of(site):
    return parse_admin_config(site.config_path.read_text(), roots=site.roots)


def build(site, cmd=None, config=None):
    return newworkshop.build(
        cmd or command(), config or config_of(site), plan="P-9", bot=GLOBAL,
        layout=site.paths.layout, workshops=site.paths.workshops, secrets=site.paths.secrets,
        config_path=str(site.config_path),
    )  # fmt: skip


def flags(argv):
    """`--flag value` pairs of an argument list, in order."""
    words = list(argv[argv.index("-u") + 2 :])
    pairs = [(words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
    return [pair for pair in pairs if pair[0] != "--dry-run"]


def test_the_action_copies_like_and_names_every_value(site):
    action = build(site)
    assert (action.verb, action.workshop, action.bot) == ("new-workshop", "", GLOBAL)
    assert action.hashed == DRY_RUN and action.targets == ("mit-2027",)
    assert action.inputs == (str(site.config_path),)  # a changed admin.toml: STALE
    (step,) = action.steps
    assert step.dry_run == (*step.argv, "--dry-run") and step.env == ()
    assert step.argv[-len(flags(step.argv)) * 2 - 1].endswith("/workshop/admin/new_workshop.py")
    etc, ws = site.tmp / "etc", site.tmp / "ws"
    assert flags(step.argv) == [
        ("--plan", "P-9"), ("--name", "mit-2027"), ("--region", "us-east-1"),
        ("--zuliprc", f"{etc}/secrets/provisioner.zuliprc"),
        ("--tag", "workshop=mit-2027"), ("--scope", "zab-scope=mathagents"),
        ("--instance-type", "m7a.2xlarge"), ("--github-org", "MathAgents"),
        ("--refserve-url", "https://files.mathagents.org"),
        ("--admin-key", f"{etc}/break-glass/drew.pub"),
        ("--organizer", "103"), ("--organizer", "105"), ("--admin", str(DREW)),
        ("--admin", str(ALICE)), ("--ws-root", str(ws)), ("--secrets", f"{etc}/secrets"),
        ("--public-key", "/var/lib/zab-admin/.ssh/id_ed25519.pub"),
        ("--request-dir", "/var/lib/zab-admin-config/requests"),
        ("--result", "/var/lib/zab-admin-config/result.json"),
    ]  # fmt: skip
    args = ("mit-2027", "region=us-east-1", "like=harvard-2026", "organizers=103,105")
    assert action.args == args


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("Harvard Halloween", "Harvard%20Halloween"),
        ("-x", "%2Dx"),  # never a leading `-`
        ("Zürich (2027)", "Z%C3%BCrich%20%282027%29"),
    ],
)
def test_the_participants_channel_goes_percent_encoded(site, name, encoded):
    """#627: `channel="…"` is the one value with spaces or any script's letters: it goes to the
    script percent-encoded, after every other flag; without it, no `--channel` at all."""
    action = build(site, command(channel=name))
    pairs = flags(action.steps[0].argv)
    at = pairs.index(("--channel", encoded))
    config = config_of(site)  # then every channel admin.toml binds, never adopted (#638)
    bound = sorted(newworkshop.bound_channels(config))
    assert pairs[at + 1 :] == [("--bound-channel", str(uid)) for uid in bound]
    for ws in config.workshops:
        assert ws.bot.channels <= set(bound) and ws.alerts_channel in {*bound, None}
    assert action.args[-1] == f'channel="{name}"'
    assert "its participants' channel, adopted or made" in action.notes[0]
    assert "--channel" not in build(site).steps[0].argv
    assert "participants" not in build(site).notes[0]


def test_every_channel_admin_toml_binds_is_bound(site):
    """The approval of #638 (6397329425, C): the bots' command channels, the global bot's
    included, the alerts channels and the workshops' participants' channels, each of which
    the loader refuses as a new workshop's `channel`."""
    config = config_of(site)
    assert config.global_bot is not None and config.global_bot.channels
    first = dataclasses.replace(config.workshops[0], channel=4747)
    config = dataclasses.replace(config, workshops=(first, *config.workshops[1:]))
    bound = newworkshop.bound_channels(config)
    assert config.global_bot.channels <= bound and 4747 in bound
    for ws in config.workshops:
        assert ws.bot.channels <= bound and {ws.alerts_channel, ws.channel} - {None} <= bound
    pairs = flags(build(site, command(channel="X"), config).steps[0].argv)
    assert ("--bound-channel", "4747") in pairs
    assert all(("--bound-channel", str(uid)) in pairs for uid in config.global_bot.channels)


def test_a_channel_that_is_no_name_is_refused(site):
    with pytest.raises(ActionError, match="channel=: not a channel's name"):
        build(site, command(channel="a  b"))


def test_the_bot_and_the_helper_agree_on_their_paths():
    """Codex round 3 on #424: the bot writes the request where the helper and its path unit
    look, and reads the outcome and the record where the helper writes them; deployed, a
    mismatch makes the bot, the channels and the key pair, then fails at the request."""
    helper, layout = confighelper.Paths(), Layout()
    assert layout.config_request == helper.requests
    assert layout.config_result == f"{helper.state}/result.json"
    assert BotPaths().config_changes == f"{helper.state}/changes.jsonl"
    unit = (UNITS / "zab-admin-config.path").read_text(encoding="utf-8")
    assert f"PathExists={helper.requests}/request.json\n" in unit
    tmpfiles = (UNITS / "tmpfiles.conf").read_text(encoding="utf-8")
    assert re.search(rf"^d\s+{re.escape(helper.requests)}\s+0700\s+zab-admin\s", tmpfiles, re.M)


def test_the_scope_is_globals_and_the_tag_the_workshops_alone(site):
    """#425: the fleet scope, `zab-scope` and `[global]`'s extra tags (the test server's
    `zab-role=test`, §7), is `--scope`, read from admin.toml, never copied from `like=`; `--tag`
    is `workshop=<name>` alone, so the entry names no tag and the loader adds the rest."""
    config = config_of(site)
    assert newworkshop.fleet_scope(config) == config.scope_tags == SCOPE
    test = dataclasses.replace(config, scope_tags=(*SCOPE, ("zab-role", "test")))
    pairs = flags(build(site, config=test).steps[0].argv)
    assert [v for k, v in pairs if k == "--tag"] == ["workshop=mit-2027"]
    assert [v for k, v in pairs if k == "--scope"] == ["zab-scope=mathagents", "zab-role=test"]


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        (command(ws_name="zab-test"), "zab-test is a workshop of this server already"),
        (command(like="nowhere"), "like=nowhere: no workshop of that name"),
        (command(organizers=(103, HARVARD)), "an organizer is an admin bot"),
        (command(organizers=(GLOBAL,)), "an organizer is an admin bot"),
        (command(organizers=None), "not a whole !new-workshop"),
    ],
)
def test_a_new_workshop_is_refused_before_any_script(site, cmd, why):
    with pytest.raises(ActionError, match=why):
        build(site, cmd)


def test_one_global_admin_is_enough_for_the_plan_itself(site):
    """#558: a global admin's approval alone completes a server plan, so one of them is enough
    whatever `destructive_approvals` says (review of #424 asked for that many before)."""
    config = dataclasses.replace(config_of(site), admins=frozenset({DREW}))
    assert config.destructive_approvals == 2
    assert build(site, command(organizers=(103, 105, 106)), config).verb == "new-workshop"


def test_no_global_admin_for_the_plan_itself_is_refused(site):
    """Review of #424: the plan is a server plan, which only global admins approve. With none,
    no plan is posted that nobody could approve, however many organizers are named."""
    config = dataclasses.replace(config_of(site), admins=frozenset())
    with pytest.raises(ActionError, match="this server has no global admin"):
        build(site, command(organizers=(103, 105, 106)), config)


def test_a_region_not_set_up_is_refused(site):
    """A region outside `[global] regions` is one the role's policy doesn't cover (#425):
    refused before any script, naming the regions that are set up."""
    with pytest.raises(ActionError, match="region=eu-north-1 is not set up for the fleet: "
                       r"\[global\] regions are us-west-2, us-east-1"):  # fmt: skip
        build(site, command(region="eu-north-1"))


# ----------------------------------------------------------------------------- the plan


class DoneRunner(FakeRunner):
    """Applies as the runner does, as far as the plan's state goes: APPLYING, then DONE."""

    async def apply(self, job):
        self.applied.append(job)
        store = job.bot.server.store
        store.begin_apply(job.plan.id, job.plan.hash)
        store.finish(job.plan.id, True)


@pytest.fixture
async def server(site):
    runner, restarts = DoneRunner(), []
    srv = site.server(planner=Planning(runner, restart=lambda: restarts.append(1)))
    srv.runner, srv.restarts = runner, restarts
    await srv.start()
    yield srv
    await srv.close()


async def ask(server, text=NEW, sender=DREW, mid=5000):
    return await request(server, text, sender=sender, bot=GLOBAL, stream=ADMINS_CH, mid=mid)


async def say(server, sender, text, mid):
    await bot_of(server, GLOBAL).on_message(incoming(sender, text, stream=ADMINS_CH, mid=mid))
    await settle(server)


async def test_one_global_admin_and_the_phrase_then_the_bots_restart(site, server):
    """#558: the server class is a global admin's alone, phrase included."""
    plan = await ask(server)
    assert plan.state == PENDING and (plan.workshop, plan.bot) == ("", GLOBAL)
    needs = plan.requirement
    assert (needs.klass, needs.needed, needs.phrase) == ("server", 2, ("new-workshop", "mit-2027"))
    text = posts(site, GLOBAL)[-2]["content"]
    assert text.startswith(f"**{plan.name}** · server · `{NEW}` · requested by ")
    assert "**Targets** (1): mit-2027" in text and "A new workshop, **mit-2027**" in text
    assert "widget_content" not in posts(site, GLOBAL)[-1]  # the phrase is typed (#542)
    phrase = f"!approve {plan.name} {plan.hash[:6]} new-workshop mit-2027"
    assert (
        f"\n\n```text\n{phrase}\n```\n\nIt takes 1 global admin. To reject, send "
        in posts(site, GLOBAL)[-1]["content"]
    )
    await say(server, DREW, f"!approve {plan.name} {plan.hash[:6]}", LATE)
    assert "needs the phrase `new-workshop mit-2027`" in posts(site, GLOBAL)[-1]["content"]
    assert server.store.get(plan.id).state == PENDING and not server.runner.applied
    await say(server, DREW, phrase, LATE + 1)
    said = [p["content"] for p in posts(site, GLOBAL)]
    assert any(t.startswith(f"✅ {plan.name} approved (a global admin's approval)") for t in said)
    (job,) = server.runner.applied
    assert job.fleet is None and job.action.verb == "new-workshop"
    pairs = flags(job.action.steps[0].argv)  # the real admin.toml's scope, no stand-in (#425)
    assert [v for k, v in pairs if k in ("--tag", "--scope")] == [
        "workshop=mit-2027", "zab-scope=mathagents"
    ]  # fmt: skip
    assert server.store.get(plan.id).state == DONE and server.restarts == [1]
    # the restart comes: every workshop stays busy until it does
    assert server.planner.busy("harvard-2026") == plan.id
    assert server.planner.busy("") == plan.id


async def test_a_failed_apply_frees_the_workshops_and_restarts_nothing(site, server):
    server.runner.apply = lambda job: _fail(job)
    plan = await ask(server)
    phrase = f"!approve {plan.name} {plan.hash[:6]} new-workshop mit-2027"
    await say(server, DREW, phrase, LATE)
    assert server.store.get(plan.id).state == "failed" and server.restarts == []
    assert server.planner.busy("harvard-2026") is None and server.planner.busy("") is None


async def _fail(job):
    store = job.bot.server.store
    store.begin_apply(job.plan.id, job.plan.hash)
    store.finish(job.plan.id, False)


async def test_it_waits_for_every_workshop_and_they_wait_for_it(site, server):
    """§2.4 step 5: a server plan needs every workshop's lock."""
    plan = await ask(server)
    phrase = f"!approve {plan.name} {plan.hash[:6]} new-workshop mit-2027"
    other = await request(server, "!stop alice", sender=OLGA, mid=5001)
    server.runner.applying["zab-test"] = other
    await say(server, DREW, phrase, LATE)
    assert posts(site, GLOBAL)[-1]["content"].startswith(f"busy: {other.name} is applying")
    assert server.approvals.approved_by(plan.id) == ()
    del server.runner.applying["zab-test"]
    server.runner.applying[""] = plan
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, f"!approve {other.name} {other.hash[:6]}", mid=LATE))
    assert posts(site, HARVARD)[-1]["content"].startswith(f"busy: {plan.name} is applying")


@pytest.mark.parametrize(
    ("sender", "answer"),
    [
        (DEE, "❌ only a global admin requests `!new-workshop`"),  # a deployer
        (OLGA, "🚫 Olga Organizer is not an organizer here; nothing was done"),
        (TESS, "🚫 Tess Tester is not an organizer here; nothing was done"),
        (PAT, "🚫 Pat Participant is not an organizer here; nothing was done"),
    ],
)
async def test_only_a_global_admin_requests_it(site, server, sender, answer):
    await say(server, sender, NEW, 5000)
    assert posts(site, GLOBAL)[-1]["content"] == answer
    assert server.store.db.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


async def test_a_workshops_bot_refuses_it(site, server):
    await bot_of(server, HARVARD).on_message(incoming(DREW, NEW, mid=5000))
    assert "server's: send it to the server's bot" in posts(site, HARVARD)[-1]["content"]
    # its help there names the server's bot too, never a workshop's (review of #424)
    await bot_of(server, HARVARD).on_message(incoming(DREW, "!help new-workshop", mid=5001))
    help_ = posts(site, HARVARD)[-1]["content"]
    assert "`!new-workshop` is not taken here: send it to the server's bot" in help_
    # and a workshop's verb asked of the server's bot names a workshop's bot
    asked = incoming(DREW, "!help stop", stream=ADMINS_CH, mid=5002)
    await bot_of(server, GLOBAL).on_message(asked)
    help_ = posts(site, GLOBAL)[-1]["content"]
    assert "`!stop` is not taken here: send it to a workshop's bot" in help_


async def test_an_existing_name_is_refused_with_no_plan(site, server):
    plan = await ask(server, NEW.replace("mit-2027", "zab-test"))
    assert plan.state == "refused" and "zab-test is a workshop of this server" in plan.note
    assert records(site, "refused")[-1]["plan"] == plan.name
    assert server.runner.prepared == []


async def test_the_apply_rebuilds_from_the_command(site, server):
    """At apply the plan is built again from its command, against admin.toml as it is then."""
    plan = await ask(server)
    command, action, fleet = await server.planner.rebuild(bot_of(server, GLOBAL), plan)
    assert fleet is None and command.ws_name == "mit-2027"
    assert action.steps == server.runner.prepared[0][1].steps
    assert server.store.get(plan.id).state in (PENDING, APPROVED)


def test_the_runbooks_commands_parse_on_the_global_bot():
    """docs/NEW-WORKSHOP.md: every command it shows parses on the server's bot."""
    import re
    from pathlib import Path

    from zab.admin.grammar import Context, parse

    text = (Path(__file__).resolve().parents[2] / "docs" / "NEW-WORKSHOP.md").read_text()
    ctx = Context(GLOBAL, "Workshop admin", re.compile(r"^v\d+\.\d+\.\d+$"), None)
    shown = re.findall(r"^(![a-z-]+ .*)$", text, re.M) + re.findall(r"`(![a-z-]+[^`]*)`", text)
    named = ("!new-workshop", "!approve", "!help")
    commands = [c for c in shown if c.split()[0] in named and len(c.split()) > 1]
    assert len(commands) >= 3
    for line in commands:
        assert isinstance(parse(line, ctx), Command), line
