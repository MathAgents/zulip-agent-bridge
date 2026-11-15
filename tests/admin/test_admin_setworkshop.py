"""`!set-workshop` and `!show-workshop` on the `[global]` bot (docs/ADMIN.md §2.10; #1043): the
grammar, the action built from admin.toml, the plan in the server class, the restart after it,
the read, and the root helper's change of an existing entry (`confighelper.Change`)."""

import base64
import dataclasses
import json
import re
import tomllib
from pathlib import Path

import pytest

from zab.admin import confighelper, setworkshop
from zab.admin.actions import DRY_RUN, ActionError
from zab.admin.config import parse_admin_config
from zab.admin.grammar import Command, Context, Rejected, parse
from zab.admin.planning import Planning
from zab.admin.plans import DONE, PENDING

from .botkit import ADMINS_CH, DEE, DREW, GLOBAL, HARVARD, OLGA, incoming, make_site
from .test_admin_confighelper import Helper, refused
from .test_admin_newworkshop import DoneRunner, flags
from .test_admin_planning import LATE, bot_of, posts, request, settle

AMI = "ami-0123456789abcdef0"  # botkit's, every workshop's
NEW_AMI = "ami-0fedcba9876543210"
GIFTS = 'Accept your gifts: Claude Max through the "invitation" e-mail, and [the form](https://f.example.org/x).'
GLOBAL_CTX = Context(GLOBAL, "Workshop admin", re.compile(r"^v\d+\.\d+\.\d+$"), None)


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


# ------------------------------------------------------------------------------ the grammar


@pytest.mark.parametrize(
    ("line", "fields"),
    [
        ("!set-workshop harvard-2026 ami=newest", {"ami": "newest"}),
        (f"!set-workshop harvard-2026 ami={NEW_AMI}", {"ami": NEW_AMI}),
        (f'!set-workshop harvard-2026 gifts_step="{GIFTS}"', {"gifts_step": GIFTS}),
        (f"!set-workshop harvard-2026 ami=newest gifts_step=“{GIFTS}”", {"ami": "newest",
                                                                         "gifts_step": GIFTS}),
        ("!set-workshop harvard-2026 gifts_step=none", {"gifts_step": ""}),
        ('!set-workshop harvard-2026 gifts_step="none"', {"gifts_step": "none"}),
    ],
)  # fmt: skip
def test_set_workshop_parses_and_reads_its_own_text_back(line, fields):
    """The gift step is the one free text: quoted (a phone's curly quotes too), last on the line,
    where it runs to the line's last quote, so it may hold quotes; `none` unquoted removes it."""
    found = parse(line, GLOBAL_CTX)
    assert isinstance(found, Command) and found.workshop == "harvard-2026"
    assert {k: getattr(found, k) for k in fields} == fields
    assert parse(found.text(), GLOBAL_CTX) == found  # the plan is rebuilt from its text


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("!set-workshop harvard-2026", "name what changes"),
        ("!set-workshop harvard-2026 region=us-east-1", "`region=us-east-1` is not an option"),
        ("!set-workshop harvard-2026 tag=x", "is not an argument of !set-workshop"),
        ("!set-workshop harvard-2026 ami=newest ami=newest", "is given twice"),
        (f"!set-workshop harvard-2026 ami=newest ami={NEW_AMI}", "`ami=` is given twice"),
        ("!set-workshop harvard-2026 ami=ami-XYZ", "`ami=` takes ami-<hex>, or newest"),
        ('!set-workshop harvard-2026 gifts_step="a" ami=newest', "`gifts_step=` takes"),
        ('!set-workshop harvard-2026 gifts_step=none gifts_step="a"', "is given twice"),
        ('!set-workshop harvard-2026 gifts_step="Hi @**all**"', "no mention"),
        ('!set-workshop harvard-2026 gifts_step="Hi @_**Olga Organizer|103**"', "no mention"),
        ('!set-workshop harvard-2026 gifts_step="Hi @*organizers*"', "no mention"),
        ('!set-workshop harvard-2026 gifts_step="-x"', "not starting with -"),
        ('!set-workshop harvard-2026 gifts_step=" x"', "no outer space"),
        ('!set-workshop harvard-2026 gifts_step=""', "`gifts_step=` takes"),
        (f'!set-workshop harvard-2026 gifts_step="{"é" * 251}"', "at most 500 bytes"),
        ("!set-workshop Harvard ami=newest", "expected a workshop's name"),
        # review of #1046, N1: the plan's header shows the command as code
        ('!set-workshop harvard-2026 gifts_step="Run `claude` once."', "takes no backtick"),
        # N5: only the first line is read: never a gift step cut at its first line's last quote
        ('!set-workshop harvard-2026 gifts_step="line one"\nline two."', "is one line"),
        ("!set-workshop harvard-2026 ami=newest\nand the gifts", "is one line"),
    ],
)
def test_set_workshop_refuses_at_once(line, why):
    """Criterion 1: an unknown key, a repeated one, or a value outside the loader's rule is
    refused by the grammar, saying why; nothing is planned."""
    found = parse(line, GLOBAL_CTX)
    assert isinstance(found, Rejected) and why in found.reason, found


def test_both_verbs_are_the_servers_bots():
    harvard = Context(HARVARD, "Workshop admin · harvard-2026", GLOBAL_CTX.release_pattern, None)
    from zab.admin.grammar import Roster

    ws = dataclasses.replace(harvard, roster=Roster(("alice",)))
    for line in ("!set-workshop harvard-2026 ami=newest", "!show-workshop harvard-2026"):
        found = parse(line, ws)
        assert isinstance(found, Rejected) and "the server's: send it to the server's bot" in (
            found.reason
        )
    shown = parse("!show-workshop harvard-2026", GLOBAL_CTX)
    assert isinstance(shown, Command) and shown.workshop == "harvard-2026"


# ------------------------------------------------------------------------------- the action


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def config_of(site):
    return parse_admin_config(site.config_path.read_text(), roots=site.roots)


def build(site, config=None, **fields):
    cmd = Command("set-workshop", workshop="harvard-2026", **fields)
    return setworkshop.build(
        cmd, config or config_of(site), plan="P-9", bot=GLOBAL, layout=site.paths.layout,
        config_path=str(site.config_path),
    )  # fmt: skip


def test_the_action_names_the_entry_and_the_change(site):
    action = build(site, ami="newest", gifts_step=GIFTS)
    assert (action.verb, action.workshop, action.bot) == ("set-workshop", "", GLOBAL)
    assert action.hashed == DRY_RUN and action.targets == ("harvard-2026",)
    assert action.inputs == (str(site.config_path),)  # admin.toml changed in between: STALE
    (step,) = action.steps
    assert step.dry_run == (*step.argv, "--dry-run")
    assert step.argv[step.argv.index("-u") + 1].endswith("/workshop/admin/set_workshop.py")
    assert flags(step.argv) == [
        ("--plan", "P-9"), ("--name", "harvard-2026"), ("--region", "us-west-2"),
        ("--security-group", "sg-0123456789abcdef0"), ("--instance-type", "m7a.2xlarge"),
        ("--tag", "workshop=harvard-2026"), ("--scope", "zab-scope=mathagents"),
        ("--ami-was", AMI), ("--ami", "newest"),
        ("--record", f"{site.paths.layout.runs}/P-9/set-workshop.json"),  # the plan's pick, N2
        ("--gifts-step-b64", b64(GIFTS)),
        ("--request-dir", "/var/lib/zab-admin-config/requests"),
        ("--result", "/var/lib/zab-admin-config/result.json"),
    ]  # fmt: skip
    assert action.args == ("harvard-2026", "ami=newest", f'gifts_step="{GIFTS}"')
    assert "its ami and gifts_step, through the config helper" in action.notes[0]
    named = flags(build(site, ami="ami-0fedcba9876543210").steps[0].argv)
    assert ("--ami", "ami-0fedcba9876543210") in named and "--record" not in dict(named)


def test_a_removal_passes_the_old_text_and_no_new_one(site):
    ws = dataclasses.replace(config_of(site).workshops[0], gifts_step=GIFTS)
    config = dataclasses.replace(config_of(site), workshops=(ws, *config_of(site).workshops[1:]))
    argv = build(site, config, gifts_step="").steps[0].argv
    assert argv[-1] == "--no-gifts-step" and ("--gifts-step-was-b64", b64(GIFTS)) in flags(argv)
    assert "--gifts-step-b64" not in argv and "--ami" not in argv


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"ami": AMI}, f"ami={AMI} is harvard-2026's image already: nothing to change"),
        ({"gifts_step": ""}, "harvard-2026 has no gifts_step: nothing to remove"),
        ({}, "not a whole !set-workshop command"),
    ],
)
def test_a_change_that_changes_nothing_is_refused_before_any_script(site, fields, why):
    with pytest.raises(ActionError, match=re.escape(why)):
        build(site, **fields)


def test_the_same_gift_step_and_an_unknown_workshop_are_refused(site):
    ws = dataclasses.replace(config_of(site).workshops[0], gifts_step=GIFTS)
    config = dataclasses.replace(config_of(site), workshops=(ws, *config_of(site).workshops[1:]))
    with pytest.raises(ActionError, match="gifts_step: harvard-2026's is that text already"):
        build(site, config, gifts_step=GIFTS)
    with pytest.raises(ActionError, match="mit-2027: no workshop of that name on this server"):
        setworkshop.build(
            Command("set-workshop", workshop="mit-2027", ami="newest"), config, plan="P-9",
            bot=GLOBAL, layout=site.paths.layout, config_path=str(site.config_path),
        )  # fmt: skip
    nobody = dataclasses.replace(config, admins=frozenset())
    with pytest.raises(ActionError, match="this server has no global admin"):
        build(site, nobody, ami="newest")


def test_the_entry_is_shown_as_loaded_without_reading_its_files(site):
    """Criterion 4: the entry in admin.toml's form, with the defaults it takes, its files named
    by their paths alone: a secret in the bot's key file is never read."""
    key = site.tmp / "etc" / "secrets" / "bots" / "harvard-2026.zuliprc"
    text = setworkshop.show(config_of(site), "harvard-2026")
    lines = text.split("\n")
    assert lines[0] == "[[workshop]]" and 'name = "harvard-2026"' in lines
    assert f'zuliprc = "{key}"' in lines and f'ami = "{AMI}"' in lines
    assert "mention_after_s = 120" in lines and "spare_idle_stop = 10" in lines
    assert not any(line.startswith("gifts_step") for line in lines)
    assert lines[-1] == "# its scope: workshop=harvard-2026, zab-scope=mathagents"
    assert "key=" not in text and setworkshop.show(config_of(site), "mit-2027") == ""
    entry = tomllib.loads("\n".join(lines[:-1]))["workshop"][0]
    assert entry["aws"]["instance_types"] == ["m7a.2xlarge"] and entry["organizers"] == [103, 104]


# --------------------------------------------------------------------------------- the plan


@pytest.fixture
async def server(site):
    runner, restarts = DoneRunner(), []
    srv = site.server(planner=Planning(runner, restart=lambda: restarts.append(1)))
    srv.runner, srv.restarts = runner, restarts
    await srv.start()
    yield srv
    await srv.close()


SET = "!set-workshop harvard-2026 ami=newest gifts_step=none"


async def say(server, sender, text, mid):
    await bot_of(server, GLOBAL).on_message(incoming(sender, text, stream=ADMINS_CH, mid=mid))
    await settle(server)


async def test_one_global_admins_approval_then_the_bots_restart(site, server):
    """Criteria 2 and 3: a server plan, approved by one global admin with no phrase; its apply
    holds every workshop's lock, and the bots restart after it, which loads the entry."""
    plan = await request(server, SET.replace(" gifts_step=none", ""), sender=DREW, bot=GLOBAL,
                         stream=ADMINS_CH)  # fmt: skip
    assert plan.state == PENDING and (plan.workshop, plan.bot) == ("", GLOBAL)
    assert (plan.requirement.klass, plan.requirement.phrase) == ("server", ())
    text = posts(site, GLOBAL)[-2]["content"]
    assert text.startswith(f"**{plan.name}** · server · `!set-workshop harvard-2026 ami=newest`")
    assert "A change of **harvard-2026**'s entry in admin.toml: its ami" in text
    await say(server, DREW, f"!approve {plan.name} {plan.hash[:6]}", LATE)
    assert server.store.get(plan.id).state == DONE and server.restarts == [1]
    (job,) = server.runner.applied
    assert job.fleet is None and job.action.verb == "set-workshop"
    assert server.planner.busy("harvard-2026") == plan.id  # until the restart
    assert server.planner.busy("zab-test") == plan.id


async def test_a_refused_change_is_said_with_no_plan_to_approve(site, server):
    plan = await request(server, SET, sender=DREW, bot=GLOBAL, stream=ADMINS_CH)
    assert plan.state == "refused" and "has no gifts_step: nothing to remove" in plan.note
    assert server.runner.prepared == []


@pytest.mark.parametrize(
    ("sender", "answer"),
    [
        (DEE, "❌ only a global admin requests `!set-workshop`"),  # a deployer
        (OLGA, "🚫 Olga Organizer is not an organizer here; nothing was done"),
    ],
)
async def test_only_a_global_admin_requests_it(site, server, sender, answer):
    await say(server, sender, "!set-workshop harvard-2026 ami=newest", 5000)
    assert posts(site, GLOBAL)[-1]["content"] == answer
    assert server.store.db.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


async def test_show_workshop_answers_on_the_servers_bot(site, server):
    await say(server, DREW, "!show-workshop harvard-2026", 5000)
    answer = posts(site, GLOBAL)[-1]["content"]
    assert answer.startswith("**harvard-2026**, as the bots loaded it:\n```")
    assert f'ami = "{AMI}"' in answer and "audit #" in answer
    await say(server, DREW, "!show-workshop mit-2027", 5001)
    assert "`mit-2027` is not a workshop of this server" in posts(site, GLOBAL)[-1]["content"]


# --------------------------------------------------------------------------- the root helper


@pytest.fixture
def helper(tmp_path):
    return Helper(tmp_path)


def change(helper, values, was, name="harvard-2026", plan="P-43"):
    body = {"version": 1, "plan": plan, "workshop": name, "set": values, "was": was}
    path = Path(helper.paths.requests) / "request.json"
    path.write_text(json.dumps(body))
    return path


def test_the_helper_changes_the_lines_of_those_keys_alone(helper):
    """Criterion 3: the image's line rewritten in place, its comment kept; a gift step added
    under the entry's last channel or name line; the old file kept as admin.toml.bak; the
    record names the workshop and each key old and new, a gift step in full."""
    config = helper.site.config_path
    old = config.read_text().replace(f'ami = "{AMI}"', f'ami = "{AMI}"  # the Oct 19 image', 1)
    config.write_text(old)
    others = {k: v for k, v in helper.files().items() if k != "admin.toml"}
    change(helper, {"ami": NEW_AMI, "gifts_step": GIFTS}, {"ami": AMI, "gifts_step": None})
    assert helper.run() == 0
    after = helper.files()
    assert {k: v for k, v in after.items() if k not in ("admin.toml", "admin.toml.bak")} == others
    text = config.read_text()
    assert (helper.etc / "admin.toml.bak").read_text() == old
    assert f'ami = "{NEW_AMI}"  # the Oct 19 image' in text and text.count(AMI) == 1
    assert f"organizers = [{OLGA}, 104]\ngifts_step = {json.dumps(GIFTS)}\n[workshop.bot]" in text
    diff = [line for line in text.split("\n") if line not in old.split("\n")]
    assert len(diff) == 2 and len(text.split("\n")) == len(old.split("\n")) + 1
    loaded = parse_admin_config(text, roots=helper.site.roots)
    assert loaded.workshops[0].aws.ami == NEW_AMI and loaded.workshops[0].gifts_step == GIFTS
    assert loaded.workshops[1] == parse_admin_config(old, roots=helper.site.roots).workshops[1]
    result = helper.result()
    assert result["ok"] is True and result["workshop"] == "harvard-2026"
    assert result["message"] == "harvard-2026's entry changed in admin.toml; admin.toml.bak kept"
    (line,) = (Path(helper.paths.state) / "changes.jsonl").read_text().splitlines()
    record = json.loads(line)
    assert (record["event"], record["plan"], record["workshop"]) == ("set-workshop", "P-43",
                                                                     "harvard-2026")  # fmt: skip
    assert record["changes"] == {"ami": {"old": AMI, "new": NEW_AMI},
                                 "gifts_step": {"old": None, "new": GIFTS}}  # fmt: skip


def test_a_gift_step_is_changed_then_removed_in_place(helper):
    config = helper.site.config_path
    first = config.read_text().replace(f"organizers = [{OLGA}, 104]\n",
                         f'organizers = [{OLGA}, 104]\nalerts_channel = 44\ngifts_step = "Old."\n',
                         1)  # fmt: skip
    config.write_text(first)
    change(helper, {"gifts_step": GIFTS}, {"gifts_step": "Old."})
    assert helper.run() == 0
    assert config.read_text() == first.replace('"Old."', json.dumps(GIFTS))
    change(helper, {"gifts_step": None}, {"gifts_step": GIFTS}, plan="P-44")
    assert helper.run() == 0
    assert config.read_text() == first.replace('gifts_step = "Old."\n', "")


@pytest.mark.parametrize(
    ("values", "was", "reason"),
    [
        ({"ami": NEW_AMI}, {"ami": "ami-00000000000000000"}, "entry changed since the plan"),
        ({"ami": NEW_AMI}, {"ami": AMI, "gifts_step": None}, "says what each was"),
        ({"region": "us-east-1"}, {"region": "us-west-2"}, "a change sets ami or gifts_step"),
        ({"ami": AMI}, {"ami": AMI}, "the change's ami is unchanged"),
        ({"ami": None}, {"ami": AMI}, "the change's ami is not text"),
        ({}, {}, "the change sets nothing"),
        ({"ami": "ami-xyz"}, {"ami": AMI}, "the new admin.toml is refused: "),
        ({"gifts_step": "-x"}, {"gifts_step": None}, "workshop[0].gifts_step: expected"),
        ({"gifts_step": "a\nb"}, {"gifts_step": None}, "workshop[0].gifts_step: expected"),
    ],
)
def test_a_refused_change_changes_no_file(helper, values, was, reason):
    change(helper, values, was)
    refused(helper, reason)


def test_a_change_of_no_workshop_or_a_hand_formed_line_is_refused(helper):
    change(helper, {"ami": NEW_AMI}, {"ami": AMI}, name="mit-2027")
    refused(helper, "mit-2027 is no workshop of admin.toml")
    config = helper.site.config_path
    config.write_text(config.read_text().replace(f'ami = "{AMI}"', f"ami = '{AMI}'", 1))
    change(helper, {"ami": NEW_AMI}, {"ami": AMI})
    refused(helper, 'ami is not a line `ami = "…"` of the entry: edit it by hand')


def test_the_new_file_must_equal_the_old_but_for_the_change(helper, monkeypatch):
    """Whatever `_rewrite` does, the result's TOML is the old one's but for these values."""
    real = confighelper._rewrite
    monkeypatch.setattr(confighelper, "_rewrite", lambda *a: real(*a).replace("104]", "105]", 1))
    change(helper, {"ami": NEW_AMI}, {"ami": AMI})
    refused(helper, "the new admin.toml would change more than these keys")
