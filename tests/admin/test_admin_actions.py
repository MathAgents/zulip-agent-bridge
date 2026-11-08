"""Each admin command's argument lists, target material and plan hash (docs/ADMIN.md §2.3 to §3).

Table-driven: every verb that runs a script maps to exact argument lists, for the dry run and
for apply. Nothing from another workshop is accepted, the selection is never left to a
script's default, and the hash is stable on identical inputs and ignores what it must ignore.
"""

import base64
import calendar
import dataclasses
import hashlib
import re

import pytest

from zab.admin import actions
from zab.admin.actions import (
    BOT_VERBS,
    DRY_RUN,
    INPUTS,
    KEYWORD_FLAGS,
    READ,
    RECONCILER,
    SETTING_FLAGS,
    TARGETS,
    VERBS,
    ActionError,
    Fleet,
    Layout,
    Live,
    Row,
    Step,
    build,
    digests,
    normalize_dry_run,
    plan_hash,
    target_material,
)
from zab.admin.config import RELEASE_PATTERNS, parse_admin_config
from zab.admin.grammar import SPECS, Command, Context, Roster, Selection, parse

CONFIG = parse_admin_config(
    """\
provider = "aws"
admins = [101]
deployers = [101]
destructive_approvals = 1

[global]
scope = "mathagents"
regions = ["us-west-2", "us-east-1"]
instance_types = ["m7a.2xlarge", "m7a.xlarge", "t3.small"]

[[workshop]]
name = "harvard-2026"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "/var/lib/zab-admin/ws/harvard-2026/roster.csv"
out_dir = "/var/lib/zab-admin/ws/harvard-2026/out"
alerts_channel = 44
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/harvard-2026.zuliprc"
user_id = 901
channels = [42]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge", "m7a.xlarge"]
key_name = "zab-admin-harvard-2026"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub", "/etc/zab-admin/break-glass/organizer2.pub"]

[[workshop]]
name = "zab-test"
github_org = "MathAgents-test"
refserve_url = "https://files.example.org"
roster = "/var/lib/zab-admin/ws/zab-test/roster.csv"
out_dir = "/var/lib/zab-admin/ws/zab-test/out"
alerts_channel = 45
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/zab-test.zuliprc"
user_id = 902
channels = [43]
[workshop.aws]
region = "us-east-1"
ami = "ami-00000000000000001"
security_group = "sg-00000000000000001"
instance_types = ["t3.small"]
key_name = "zab-admin-zab-test"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub"]
"""
)
HARVARD, TEST = CONFIG.workshops
PRODUCTION = re.compile(RELEASE_PATTERNS["final"], re.ASCII)
ROWS = (
    Row("alice", "198.51.100.1", "us-west-2"),
    Row("bob", "198.51.100.2"),
    Row("carol"),  # a late joiner: no VM yet
    Row("dave", "198.51.100.4", "us-west-2"),
)
SPARES = ("zab-spare-1", "zab-spare-2")
#: When the bot read the fleet: `!keep-until`'s ends are after it (#570).
READ_AT = float(calendar.timegm((2026, 11, 5, 12, 0, 0)))
FLEET = Fleet(
    HARVARD,
    ROWS,
    SPARES,
    PRODUCTION,
    release="v0.2.2",
    held=False,
    others=(TEST,),
    settings=(),
    read_at=READ_AT,
)
#: The other workshop on the same server, with its own rows: none of them is harvard's.
ERIN = (Row("erin", "203.0.113.5", "us-east-1"),)
OTHER = Fleet(TEST, ERIN, ("zab-spare-7",), PRODUCTION, others=(HARVARD,))
UPLOAD = "/var/lib/zab-admin/runs/P-12/roster-upload.csv"

PY = "/opt/zab-admin/current/.venv/bin/python -E -s -B -u /opt/zab-admin/current/workshop"
R = "--roster /var/lib/zab-admin/ws/harvard-2026/roster.csv"
OUT = "/var/lib/zab-admin/ws/harvard-2026/out"
KH = f"--known-hosts {OUT}/known_hosts"
PROV = "--zuliprc /etc/zab-admin/secrets/provisioner.zuliprc"
TAG = "--tag workshop=harvard-2026 --tag zab-scope=mathagents"  # #422: the fleet's too
WHL = "--wheel {W}/zulip_agent_bridge-0.2.2-py3-none-any.whl"
EVERYONE = "--only alice --only bob --only carol --only dave"
CLEAR_ALL = "--set alice=none --set bob=none --set carol=none --set dave=none"
STATUS = f"{PY}/admin/status.py {R} {KH} {TAG} --json"
PUSH = f"{PY}/admin/push_config.py {R} {{release}} --out-dir {OUT} {KH} "
PUSH += "--refserve-url https://files.mathagents.org"
#: push_config.py with a release, which writes config.toml: the workshop's github_org as the
#: file server's owner (#693) and its mention_after_s,
#: admin.toml's default here (#711)
ROLL = PUSH + " --refserve-owner MathAgents --mention-after-s 120"
ZULIP = f"{PY}/admin/zulip_provision.py {R} {PROV} --out-dir {OUT} --repo-owner MathAgents "
ZULIP += "--no-rebind --refserve-url https://files.mathagents.org"  # "Start here"'s host (#709)
REPOS = f"{PY}/admin/provision_repos.py {R} {PROV} --org MathAgents {KH} --no-rebind"
ROSTER_SYNC = f"{PY}/admin/roster_sync.py {R} {PROV} --out-dir {OUT} --region us-west-2 {TAG}"
LAUNCH = f"{PY}/aws/launch.py {R} --ami ami-0123456789abcdef0 --security-group-id "
LAUNCH += "sg-0123456789abcdef0 --key-name zab-admin-harvard-2026 --instance-type m7a.2xlarge "
LAUNCH += f"--region us-west-2 {TAG} --admin-key /etc/zab-admin/break-glass/drew.pub "
LAUNCH += f"--admin-key /etc/zab-admin/break-glass/organizer2.pub --spares-file {OUT}/spares.json"
POWER = f"{PY}/aws/power.py {R} --spares-file {OUT}/spares.json {TAG} --region us-west-2"
SPARE_IDLE = f"{PY}/aws/spare_idle_stop.py {R} --spares-file {OUT}/spares.json {TAG} "
SPARE_IDLE += "--region us-west-2"
DRY = "--dry-run"
TEARDOWN = f"{PY}/aws/teardown.py {R} {TAG} --region us-west-2"  # --roster: #604
TEARDOWN += f" --spares-file {OUT}/spares.json"  # #607
LAUNCHED = "--only alice --only bob --only dave"  # the rows with a VM
PARALLEL_DRY = "--parallel 1 --dry-run"
WAKER = (
    f"{PY}/admin/push_waker.py --config-dir /var/lib/zab-admin/waker --workshop harvard-2026 {R}"
)
NO_APPLY = "(the list without --apply)"  # push_waker.py's dry run is its run without --apply
DEFAULTS = "--lean-shared-default --idle-stop-default 60"  # a rollout's, without the words (#704)

#: (message, [(apply, dry run or None, env)]). Each string is split on spaces.
EXACT = [
    ("!status all", [(f"{STATUS} {EVERYONE}", None, ())]),
    ("!status bob", [(f"{STATUS} --only bob", None, ())]),
    (
        "!roster import [roster.csv](/user_uploads/2/ab/xyz/roster.csv)",
        [
            (
                f"{PY}/admin/roster_import.py {R} --input {UPLOAD} {PROV} --org MathAgents {TAG} "
                "--region us-west-2",
                DRY,
                ("GH_TOKEN",),
            )
        ],
    ),
    # #627: one row each; `!roster sync` and `!roster restore` need a channel and an excluded
    # row (tests/admin/test_admin_roster_sync.py)
    ("!roster remove carol", [(f"{ROSTER_SYNC} --remove carol", DRY, ())]),
    ("!roster rename carol carol-2", [(f"{ROSTER_SYNC} --rename carol carol-2", DRY, ())]),
    ("!provision zulip hold all", [(f"{ZULIP} --hold {EVERYONE}", DRY, ())]),
    ("!provision zulip carol", [(f"{ZULIP} --only carol", DRY, ())]),
    (
        "!provision repos invites-only alice",
        [(f"{REPOS} --invites-only --only alice", PARALLEL_DRY, ("GH_TOKEN",))],
    ),
    (
        "!provision repos no-invite all",
        [(f"{REPOS} --no-invite {EVERYONE}", PARALLEL_DRY, ("GH_TOKEN",))],
    ),
    (
        "!launch carol spares=2 protect",
        [
            (
                f"{LAUNCH} --protect --spares 2 --only carol --only zab-spare-1 --only zab-spare-2",
                DRY,
                (),
            ),
            # #715: the spares' own idle stop, with admin.toml's spare_idle_stop (10 here),
            # right after the launch: never after a scan_hosts.py that may fail (review of #729)
            (f"{SPARE_IDLE} --minutes 10 --only zab-spare-1 --only zab-spare-2", None, ()),
            (f"{PY}/admin/scan_hosts.py {R} {KH} --only carol", None, ()),
        ],
    ),
    (
        "!launch all",
        [
            (f"{LAUNCH} {EVERYONE}", DRY, ()),
            (f"{PY}/admin/scan_hosts.py {R} {KH} {EVERYONE}", None, ()),
        ],
    ),
    # protect.py refuses a named slug without a VM: `all` is the rows that have one
    (
        "!protect off all",
        [
            (
                f"{PY}/aws/protect.py {TAG} --region us-west-2 --off --only alice --only bob "
                "--only dave",
                DRY,
                (),
            )
        ],
    ),
    ("!protect on bob", [(f"{PY}/aws/protect.py {TAG} --region us-west-2 --only bob", DRY, ())]),
    (
        "!rollout v0.2.2 all steer=off lean-shared idle-stop=15 restart-codex multi-party=off "
        "raw-log=meta",
        [
            (
                ROLL.format(release=WHL) + " --lean-shared --restart-codex --no-claude-steer "
                f"--idle-stop 15 --no-multi-party --claude-raw-log meta {EVERYONE}",
                PARALLEL_DRY,
                (),
            ),
            (f"{STATUS} {EVERYONE}", None, ()),
        ],
    ),
    # steering is on by default (#318): no steer= passes neither flag; no lean-shared or
    # idle-stop= passes their defaults (#704), with no row whose waker is on (no waker config)
    (
        "!rollout v0.2.2 bob",
        [
            (ROLL.format(release=WHL) + f" {DEFAULTS} --only bob", PARALLEL_DRY, ()),
            (f"{STATUS} --only bob", None, ()),
        ],
    ),
    (
        "!rollout v0.2.2 bob steer=on multi-party=on raw-log=off",
        [
            (
                ROLL.format(release=WHL)
                + f" --claude-steer --multi-party --claude-raw-log off {DEFAULTS} --only bob",
                PARALLEL_DRY,
                (),
            ),
            (f"{STATUS} --only bob", None, ()),
        ],
    ),
    (
        "!idle-stop 0 alice dave",
        [(PUSH.format(release="") + " --idle-stop 0 --only alice --only dave", PARALLEL_DRY, ())],
    ),
    ("!start all", [(f"{POWER} --start --all", DRY, ())]),
    # #715: a start of spares gives each its own idle stop, a stop nothing more
    (
        "!start all spares",
        [
            (f"{POWER} --start --all --spares", DRY, ()),
            # --started: a spare EC2 still reads as stopped is waited for (Codex on #729)
            (
                f"{SPARE_IDLE} --minutes 10 --started --only zab-spare-1 --only zab-spare-2",
                None,
                (),
            ),
        ],
    ),
    (
        "!start zab-spare-2",
        [
            (f"{POWER} --start --only zab-spare-2", DRY, ()),
            (f"{SPARE_IDLE} --minutes 10 --started --only zab-spare-2", None, ()),
        ],
    ),
    ("!stop spares", [(f"{POWER} --stop --spares", DRY, ())]),
    ("!stop all spares", [(f"{POWER} --stop --all --spares", DRY, ())]),
    ("!stop dave zab-spare-2", [(f"{POWER} --stop --only dave --only zab-spare-2", DRY, ())]),
    (
        "!adopt-spare carol zab-spare-1",
        [
            (
                f"{PY}/aws/adopt_spare.py {R} --slug carol --spare zab-spare-1 --region us-west-2 "
                f"--spares-file {OUT}/spares.json {TAG}",
                DRY,
                (),
            ),
            (f"{PY}/admin/scan_hosts.py {R} {KH} --only carol", None, ()),
            (f"{ZULIP} --only carol", None, ()),
            (ROLL.format(release=WHL) + f" {DEFAULTS} --adopt-spare carol --only carol", None, ()),
            (f"{REPOS} --only carol", None, ("GH_TOKEN",)),
            (ROLL.format(release=WHL) + f" {DEFAULTS} --only carol", None, ()),
        ],
    ),
    # #553: one row, by its slug; the size as a number of GB
    (
        "!grow-disk dave 300GB",
        [
            (
                f"{PY}/aws/grow_disk.py {R} {KH} {TAG} --region us-west-2 --slug dave --size 300",
                DRY,
                (),
            )
        ],
    ),
    # #570: a kept VM's end, one --set per row; `none` clears it
    (
        "!keep-until 2026-11-30 bob dave",
        [(f"{PY}/admin/keep_until.py {R} --set bob=2026-11-30 --set dave=2026-11-30", DRY, ())],
    ),
    (
        "!keep-until none all",
        [(f"{PY}/admin/keep_until.py {R} {CLEAR_ALL}", DRY, ())],
    ),
    # push_waker.py looks VMs up by IP: `all` is the rows that have one
    (
        "!waker on all",
        [
            (
                f"{WAKER} --keys {OUT} --region us-west-2 {TAG} --organizers-channel 44 "
                "--only alice --only bob --only dave --apply",
                NO_APPLY,
                (),
            )
        ],
    ),
    ("!waker off bob", [(f"{WAKER} --remove --only bob --apply", NO_APPLY, ())]),
    # --remove makes no AWS call: `all` is every row, carol's (no VM) too
    ("!waker off all", [(f"{WAKER} --remove {EVERYONE} --apply", NO_APPLY, ())]),
    # workshop/admin/README.md §7.2: `all` is the rows with a VM; the archives go to out/exports/
    (
        "!export all",
        [
            (
                f"{PY}/admin/collect_exports.py {R} {KH} --out-dir {OUT}/exports --only alice "
                "--only bob --only dave",
                DRY,
                (),
            )
        ],
    ),
    (
        "!export bob",
        [(f"{PY}/admin/collect_exports.py {R} {KH} --out-dir {OUT}/exports --only bob", DRY, ())],
    ),
    # stage 3 (§3, action 7)
    (
        "!final-sync all",
        [
            # the token: each row's repository is looked up first (#714)
            (
                f"{PY}/admin/final_sync.py {R} {KH} --out-dir {OUT} --strict {LAUNCHED}",
                DRY,
                ("GH_TOKEN",),
            ),
        ],
    ),
    (
        "!final-sync dave bob",
        [
            (
                f"{PY}/admin/final_sync.py {R} {KH} --out-dir {OUT} --strict --only bob "
                "--only dave",
                DRY,
                ("GH_TOKEN",),
            )
        ],
    ),
    (
        "!uninstall all",
        [(PUSH.format(release="") + f" --uninstall {LAUNCHED}", PARALLEL_DRY, ())],
    ),
    # the dry run never has --yes; `unsynced` is the bot's own check, and passes no flag
    (
        "!teardown all unsynced",
        [(f"{TEARDOWN} {EVERYONE} --yes", f"{TEARDOWN} {EVERYONE} --dry-run", ())],
    ),
    (
        "!teardown spares",
        [
            (
                f"{TEARDOWN} --only zab-spare-1 --only zab-spare-2 --yes",
                f"{TEARDOWN} --only zab-spare-1 --only zab-spare-2 --dry-run",
                (),
            )
        ],
    ),
    (
        "!teardown carol zab-spare-2 unsynced",  # carol has no VM recorded: still unsynced
        [
            (
                f"{TEARDOWN} --only carol --only zab-spare-2 --yes",
                f"{TEARDOWN} --only carol --only zab-spare-2 --dry-run",
                (),
            )
        ],
    ),
    (
        "!deactivate-bots all",
        [
            (
                f"{PY}/admin/zulip_provision.py {R} {PROV} --out-dir {OUT} --deactivate {EVERYONE}",
                DRY,
                (),
            )
        ],
    ),
]


def context(fleet: Fleet) -> Context:
    roster = Roster(fleet.slugs, fleet.spares)
    return Context(fleet.workshop.bot.user_id, "Workshop admin", PRODUCTION, roster)


def command(message: str, fleet: Fleet = FLEET) -> Command:
    parsed = parse(message, context(fleet))
    assert isinstance(parsed, Command), parsed
    return parsed


@pytest.fixture
def layout(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "zulip_agent_bridge-0.2.2-py3-none-any.whl").write_bytes(b"wheel")
    return Layout(wheels=str(wheels), retired=str(tmp_path / "retired-slugs.txt"))


def make(message: str, layout: Layout, fleet: Fleet = FLEET, **kwargs) -> actions.Action:
    upload = UPLOAD if message.startswith("!roster import") else None
    return build(command(message, fleet), fleet, layout=layout, upload=upload, **kwargs)


def expected(steps, wheels: str) -> tuple[Step, ...]:
    """Each step's dry run is its apply list and `dry`'s words, or `dry` whole when it is a
    list of its own (`!teardown`'s, without `--yes`)."""
    out = []
    for apply, dry, env in steps:
        argv = tuple(apply.format(W=wheels).split())
        if dry == NO_APPLY:
            assert argv[-1] == "--apply"
            out.append(Step(argv, argv[:-1], env))
            continue
        words = tuple(dry.split()) if dry else ()
        dry_run = words if words[:1] == argv[:1] else argv + words if dry else None
        out.append(Step(argv, dry_run, env))
    return tuple(out)


@pytest.mark.parametrize("message, steps", EXACT, ids=[m.split("[")[0] for m, _ in EXACT])
def test_every_verb_maps_to_exact_argument_lists(message, steps, layout):
    action = make(message, layout)
    assert action.steps == expected(steps, layout.wheels)
    assert (action.workshop, action.bot) == ("harvard-2026", 901)
    assert action.args == command(message).tokens()


def test_the_table_covers_every_mapped_verb():
    covered = {command(message).verb for message, _ in EXACT}
    # !new-workshop and !upgrade-server are the [global] bot's, built by zab.admin.newworkshop
    # and zab.admin.upgradeserver (#569); !retire's steps
    # carry their rows (#559); !roster sync and restore need another fleet (#627), and
    # !month-end schedule rows with an end (#570): each has its own tests
    left = {"new-workshop", "upgrade-server", "retire", "roster sync", "roster restore"}
    left.add("month-end schedule")
    assert covered == set(VERBS) - left


def test_every_verb_is_mapped_or_answered_by_the_bot():
    """A verb added to the grammar fails here until it is mapped, or listed as the bot's own."""
    assert set(VERBS) | BOT_VERBS == set(SPECS)
    assert not set(VERBS) & BOT_VERBS


def test_every_keyword_and_setting_has_a_fixed_flag():
    for verb in set(VERBS) - {"new-workshop", "upgrade-server"}:  # their modules' own flags
        spec = SPECS[verb]
        assert set(spec.keywords) == set(KEYWORD_FLAGS.get(verb, {})), verb
        assert set(spec.settings) <= set(SETTING_FLAGS), verb
    assert set(KEYWORD_FLAGS) <= set(VERBS)
    hashed = {READ, TARGETS, DRY_RUN, actions.INPUTS, RECONCILER, actions.RETIRE}
    assert {VERBS[v].hashed for v in VERBS} == hashed
    for verb in VERBS.values():
        assert (verb.normalize is not None) == (verb.hashed in (DRY_RUN, actions.RETIRE))


PER_PARTICIPANT = {
    "collect_exports.py",
    "final_sync.py",
    "teardown.py",
    "status.py",
    "zulip_provision.py",
    "provision_repos.py",
    "launch.py",
    "scan_hosts.py",
    "protect.py",
    "push_config.py",
    "power.py",
    "adopt_spare.py",
    "push_waker.py",
}


@pytest.mark.parametrize("message", [m for m, _ in EXACT])
def test_no_list_ever_leaves_the_selection_to_the_script(message, layout):
    """The scripts read a missing `--only` as every participant (`select_rows`)."""
    action = make(message, layout)
    lists = [a for step in action.steps for a in (step.argv, step.dry_run) if a]
    lists += [action.status] if action.status else []
    for argv in lists:
        script = argv[5].rsplit("/", 1)[1]  # python -E -s -B -u <script>
        if script in PER_PARTICIPANT:
            chosen = {"--only", "--slug"} | (
                {"--all", "--spares"} if script == "power.py" else set()
            )
            assert chosen & set(argv), argv


def minimal(verb: str, selection: Selection) -> Command:
    fields = {
        "rollout": {"version": "v0.2.2"},
        "protect": {"on": True},
        "waker": {"on": True},
        "idle-stop": {"idle_stop": 15},
        "teardown": {"keywords": frozenset({"unsynced"})},
        "retire": {"keywords": frozenset({"unsynced"})},
        "keep-until": {"until": "2026-11-30"},
    }.get(verb, {})
    return Command(verb, selection=selection, **fields)


def forge(command: Command, **fields) -> Command:
    """A Command changed past its own checks (#323): actions.py must refuse it all the same."""
    for name, value in fields.items():
        object.__setattr__(command, name, value)
    return command


SELECTING = sorted(verb for verb in VERBS if SPECS[verb].selection)


def raw_selection(**fields) -> Selection:
    """A Selection past the grammar's own checks (#323): actions.py refuses it on its own."""
    selection = object.__new__(Selection)
    values = {"all": False, "spares": False, "slugs": (), "spare_names": (), **fields}
    for name, value in values.items():
        object.__setattr__(selection, name, value)
    return selection


def empty_selection() -> Selection:
    return raw_selection()


@pytest.mark.parametrize("verb", SELECTING)
def test_an_empty_selection_is_refused_for_every_verb(verb, layout):
    with pytest.raises(ValueError, match="empty selection"):
        Selection()
    assert empty_selection().tokens() == ()
    with pytest.raises(ValueError, match="selection exactly when"):
        minimal(verb, None)
    for selection in (None, empty_selection()):
        forged = forge(minimal(verb, ALL), selection=selection)
        with pytest.raises(ActionError, match=r"needs a selection|nothing selected"):
            build(forged, FLEET, layout=layout)
    nobody = dataclasses.replace(FLEET, rows=(), spares=())
    with pytest.raises(ActionError, match="selects nothing"):
        build(minimal(verb, Selection(all=True)), nobody, layout=layout)
    if SPECS[verb].named:  # !retire names its rows, never `all` (#559)
        with pytest.raises(ActionError, match="never `all`"):
            build(minimal(verb, Selection(all=True)), FLEET, layout=layout)
        build(minimal(verb, Selection(slugs=("bob",))), FLEET, layout=layout)  # the control
        return
    build(minimal(verb, Selection(all=True)), FLEET, layout=layout)  # the control


def test_empty_targets_are_refused_where_a_keyword_selects_nothing(layout):
    no_spares = dataclasses.replace(FLEET, spares=())
    with pytest.raises(ActionError, match="selects nothing"):
        build(Command("stop", selection=Selection(spares=True)), no_spares, layout=layout)
    # `all` on an empty roster selects nothing, even next to `spares` (the grammar's reading)
    nobody = dataclasses.replace(FLEET, rows=())
    with pytest.raises(ActionError, match="selects nothing"):
        build(Command("stop", selection=Selection(all=True, spares=True)), nobody, layout=layout)
    no_vms = dataclasses.replace(FLEET, rows=(Row("carol"),))
    with pytest.raises(ActionError, match=r"^!protect: nothing selected"):
        build(minimal("protect", Selection(all=True)), no_vms, layout=layout)
    with pytest.raises(ActionError):
        build(Command("adopt-spare"), FLEET, layout=layout)
    with pytest.raises(ActionError, match="nothing selected"):
        actions._only(())


def test_nothing_from_another_workshop_is_accepted(layout):
    """A command parsed against another workshop's roster names rows harvard doesn't have."""
    for message in ("!stop erin", "!stop zab-spare-7", "!rollout v0.2.2 erin", "!status erin"):
        with pytest.raises(ActionError, match=r"not in this (workshop's )?roster|not available"):
            build(command(message, OTHER), FLEET, layout=layout)
    with pytest.raises(ActionError):
        build(Command("adopt-spare", slug="carol", spare="zab-spare-7"), FLEET, layout=layout)
    with pytest.raises(ActionError):
        build(Command("adopt-spare", slug="erin", spare="zab-spare-1"), FLEET, layout=layout)


@pytest.mark.parametrize("message", [m for m, _ in EXACT])
def test_every_list_carries_only_its_own_workshop(message, layout):
    words = {w for step in make(message, layout).steps for w in step.argv}
    assert not any("zab-test" in word or "us-east-1" in word for word in words)
    assert not any("nt-agents-2026" in word for word in words)  # never the scripts' default


@pytest.mark.parametrize(
    "message",
    [
        "!launch all",
        "!protect on all",
        "!start all",
        "!stop spares",
        "!status all",
        "!waker on all",
        "!teardown all unsynced",
    ],
)
def test_aws_scripts_get_every_scope_tag_and_the_region(message, layout):
    fleet = dataclasses.replace(OTHER, release=None)
    for step in make(message, layout, fleet).steps:
        argv = " ".join(step.argv)
        if "/workshop/aws/" in argv or "/status.py " in argv or "/push_waker.py " in argv:
            assert "--tag workshop=zab-test --tag zab-scope=mathagents" in argv  # #422
        if "/workshop/aws/" in argv or "/push_waker.py " in argv:
            assert "--region us-east-1" in argv


def test_a_row_recorded_in_another_region_is_refused(layout):
    moved = dataclasses.replace(FLEET, rows=(*ROWS[:3], Row("dave", "198.51.100.4", "us-east-2")))
    for message in (
        "!stop dave",
        "!stop all",
        "!protect on all",
        "!launch dave",
        "!waker on dave",
        "!teardown dave unsynced",
    ):
        with pytest.raises(ActionError, match="outside this workshop's region us-west-2"):
            make(message, layout, moved)
    make("!stop alice", layout, moved)  # rows elsewhere aren't named
    make("!rollout v0.2.2 dave", layout, moved)  # works by address, not by region
    # a VM's row without a region cell reads as us-west-2, as power.py and status.py read it;
    # a row without a VM gets the workshop's region at launch
    aws = dataclasses.replace(HARVARD.aws, region="us-east-2")
    east = dataclasses.replace(FLEET, workshop=dataclasses.replace(HARVARD, aws=aws))
    east = dataclasses.replace(east, rows=(Row("bob", "198.51.100.2"), Row("carol")))
    with pytest.raises(ActionError, match="outside this workshop's region us-east-2"):
        make("!stop bob", layout, east)
    assert "--region us-east-2" in " ".join(make("!launch carol", layout, east).steps[0].argv)
    make("!stop bob", layout, dataclasses.replace(FLEET, rows=(Row("bob", "198.51.100.2"),)))


def test_waker_on_needs_the_workshops_alerts_channel(layout):
    """The waker posts wake failures with participant bots' keys: only to the workshop's own
    alerts channel, never a default. `!waker off` makes no post and needs none."""
    bare = dataclasses.replace(FLEET, workshop=dataclasses.replace(HARVARD, alerts_channel=None))
    with pytest.raises(ActionError, match="no alerts_channel"):
        make("!waker on bob", layout, bare)
    assert "--remove" in make("!waker off bob", layout, bare).steps[0].argv
    action = make("!waker on bob", layout)
    assert action.hashed == DRY_RUN and action.inputs == (HARVARD.roster,)
    assert "--apply" not in action.dry_runs[0] and action.steps[0].argv[-1] == "--apply"


def test_power_does_not_mix_keywords_and_names(layout):
    for message in ("!stop all zab-spare-1", "!start alice spares"):
        with pytest.raises(ActionError, match="not both"):
            make(message, layout)


def test_unmapped_verbs_keywords_and_settings_are_refused(layout, monkeypatch):
    for verb in BOT_VERBS:
        selection = ALL if SPECS[verb].selection else None  # `!month-end cancel SEL` (#570)
        with pytest.raises(ActionError, match="no script is mapped"):
            build(Command(verb, selection=selection), FLEET, layout=layout)
    with pytest.raises(ValueError, match="not a verb"):
        Command("rebind")
    for verb in ("rebind", ["status"]):
        with pytest.raises(ActionError, match="no script is mapped for it"):
            build(forge(Command("version"), verb=verb), FLEET, layout=layout)
    monkeypatch.delitem(VERBS, "launch")
    with pytest.raises(ActionError, match="no script is mapped for launch"):
        make("!launch all", layout)
    monkeypatch.delitem(KEYWORD_FLAGS["rollout"], "lean-shared")
    with pytest.raises(ActionError, match="without a mapped flag"):
        make("!rollout v0.2.2 all lean-shared", layout)
    monkeypatch.delitem(SETTING_FLAGS, "steer")
    with pytest.raises(ActionError, match="without a mapped flag"):
        make("!rollout v0.2.2 all steer=on", layout)


ALL = Selection(all=True)
BOTH_INVITE_KEYWORDS = frozenset(SPECS["provision repos"].keywords)
BAD = [
    (Command("status", selection=ALL, version="v0.2.2"), "takes no version"),
    (Command("launch", selection=ALL, keywords=frozenset({"hold"})), "a keyword it doesn't"),
    (Command("provision repos", selection=ALL, keywords=BOTH_INVITE_KEYWORDS), "give one of"),
    (Command("idle-stop", selection=ALL, idle_stop=5), "0 or 10 to 240"),
    (Command("idle-stop", selection=ALL, idle_stop=True), "0 or 10 to 240"),
    (Command("launch", selection=ALL, spares=11), "0 to 10"),
    (Command("rollout", selection=ALL, version="v0.2.2", raw_log="content"), "mapped flag"),
    (Command("rollout", selection=ALL, version="v0.2.2", steer=1), "on or off"),
    (Command("rollout", selection=ALL, version="v0.2.3rc1"), "a release tag this server"),
    (Command("rollout", selection=ALL, version="v0.9.9"), "no wheel for v0.9.9"),
    (Command("protect", selection=ALL), "on or off"),
    (Command("stop", selection=Selection(slugs=("alice", "alice"))), "named twice"),
    (Command("status", selection=Selection(spares=True)), "takes no spares"),
    (Command("status", selection=Selection(slugs=("--yes",))), "not in this roster"),
]


@pytest.mark.parametrize("cmd, error", BAD)
def test_values_outside_their_rule_are_refused(cmd, error, layout):
    with pytest.raises(ActionError, match=error):
        build(cmd, FLEET, layout=layout)


def test_the_upload_is_the_plans_own_file(layout):
    roster_import = command("!roster import [r.csv](/user_uploads/2/ab/xyz/r.csv)")
    elsewhere = (
        "/var/lib/zab-admin/ws/roster.csv",
        "/var/lib/zab-admin/runs/P-0/roster-upload.csv",
    )
    for upload in (None, *elsewhere, "/var/lib/zab-admin/runs/P-1/../x/roster-upload.csv"):
        with pytest.raises(ActionError, match=r"runs/P-<n>/roster-upload\.csv"):
            build(roster_import, FLEET, layout=layout, upload=upload)
    with pytest.raises(ActionError, match="takes no upload"):
        build(command("!status all"), FLEET, layout=layout, upload=UPLOAD)


def test_values_never_become_flags_or_leave_the_alphabet(layout):
    for bad in (
        dataclasses.replace(layout, python="--python"),
        dataclasses.replace(layout, release="/opt/zab admin"),
        dataclasses.replace(layout, provisioner_zuliprc="/etc/../x.zuliprc"),
    ):
        with pytest.raises(ActionError, match="safe alphabet"):
            make("!provision zulip all", bad)
    with pytest.raises(ActionError):
        Fleet(HARVARD, (Row("--yes"),), (), PRODUCTION)
    with pytest.raises(ActionError):
        Fleet(HARVARD, (Row("all"),), (), PRODUCTION)
    with pytest.raises(ActionError):
        Fleet(HARVARD, ROWS, ("spare-1",), PRODUCTION)
    with pytest.raises(ActionError, match="not a tag this server takes"):
        Fleet(HARVARD, ROWS, (), PRODUCTION, release="v0.2.3rc1")
    with pytest.raises(ActionError, match="held"):
        Fleet(HARVARD, ROWS, (), PRODUCTION, held=1)
    with pytest.raises(ActionError, match="waker"):
        Fleet(HARVARD, ROWS, (), PRODUCTION, waker=1)
    for path in ("..", "../etc", "/etc/zab-admin/..", "/a/../b"):
        with pytest.raises(ActionError, match="safe alphabet"):
            actions._value(path)


def test_a_builder_that_forgets_a_check_is_still_caught(layout, monkeypatch):
    """The last guard: every word of every list is in the safe alphabet, whatever a builder
    does; and a workshop whose scope lacks its own tag is refused (the loader ensures it)."""
    bad = Step(("/opt/zab-admin/current/.venv/bin/python", "alice; rm -rf /"))
    monkeypatch.setitem(
        VERBS, "status", dataclasses.replace(VERBS["status"], build=lambda j: (bad,))
    )
    with pytest.raises(ActionError, match="safe alphabet"):
        make("!status all", layout)
    stray = dataclasses.replace(HARVARD, tags=(("workshop", "nt-agents-2026"),))
    with pytest.raises(ActionError, match="lacks its workshop tag"):
        make("!stop all", layout, dataclasses.replace(FLEET, workshop=stray))


def test_the_workshops_gift_step_reaches_zulip_provision_in_the_alphabet(layout):
    """admin.toml's `gifts_step` is free text (#709): `!provision zulip` and `!adopt-spare`
    pass it base64url-encoded, a word of the safe alphabet (zulip_provision.py's decoding is
    tested with it); without one, no flag, and the script's default stands."""
    text = "Accept your gifts: *Claude Max* from Anthropic's e-mail, «ChatGPT Pro» at [the form]"
    text += "(https://forms.example.org/r?id=1&x=-)."
    fleet = dataclasses.replace(FLEET, workshop=dataclasses.replace(HARVARD, gifts_step=text))
    for message in ("!provision zulip all", "!adopt-spare carol zab-spare-1"):
        argv = next(s.argv for s in make(message, layout, fleet).steps if "--no-rebind" in s.argv)
        flag = argv.index("--gifts-step-b64")
        assert base64.urlsafe_b64decode(argv[flag + 1]).decode() == text
        assert "--gifts-step-b64" not in next(
            s.argv for s in make(message, layout).steps if "--no-rebind" in s.argv
        )


def test_adopt_spare_needs_the_recorded_release_hold_and_settings(layout):
    for unknown in ({"held": None}, {"release": None}, {"settings": None}):
        with pytest.raises(ActionError, match="recorded here yet"):
            make("!adopt-spare carol zab-spare-1", layout, dataclasses.replace(FLEET, **unknown))
    held = make("!adopt-spare carol zab-spare-1", layout, dataclasses.replace(FLEET, held=True))
    assert held.steps[2].argv[-3:] == ("--hold", "--only", "carol")


def test_the_plans_status_read_covers_the_named_rows(layout):
    assert make("!rollout v0.2.2 bob", layout).status == tuple(f"{STATUS} --only bob".split())
    assert make("!stop all", layout).status == tuple(f"{STATUS} {EVERYONE}".split())
    assert make("!stop spares", layout).status is None  # spares have no roster row
    assert make("!launch all", layout).status is None
    assert make("!provision zulip all", layout).status is None


def test_inputs_and_targets(layout):
    wheel = f"{layout.wheels}/zulip_agent_bridge-0.2.2-py3-none-any.whl"
    roster, spares = HARVARD.roster, f"{OUT}/spares.json"
    assert make("!rollout v0.2.2 all", layout).inputs == (roster, wheel)
    assert make("!stop all", layout).inputs == (roster, spares)
    assert make("!adopt-spare carol zab-spare-1", layout).inputs == (roster, spares, wheel)
    assert make("!roster import [r.csv](/user_uploads/2/a/r.csv)", layout).inputs == (
        roster,
        UPLOAD,
        layout.retired,
        TEST.roster,
    )
    assert make("!stop all spares", layout).targets == ("alice", "bob", "carol", "dave", *SPARES)
    assert make("!launch bob alice spares=1", layout).targets == ("alice", "bob", "zab-spare-1")
    assert make("!adopt-spare carol zab-spare-2", layout).targets == ("carol", "zab-spare-2")
    assert make("!grow-disk dave 300GB", layout).targets == ("dave",)
    assert make("!grow-disk dave 300GB", layout).inputs == (roster,)


def test_grow_disk_takes_one_row_of_this_workshop_and_a_size_in_range(layout):
    """#553: the size is range-checked again here, whatever built the Command; the row is this
    workshop's, in its region; a spare or a second row is no target."""
    action = make("!grow-disk alice 1000GB", layout)
    assert action.steps[0].argv[-4:] == ("--slug", "alice", "--size", "1000")
    assert action.args == ("alice", "1000GB") and action.status is None
    for size in (0, 1001, -5, True, "300", 2.5):
        with pytest.raises(ActionError, match="expected a size from 1 to 1000 GB"):
            build(forge(command("!grow-disk alice 300GB"), size=size), FLEET, layout=layout)
    with pytest.raises(ActionError, match="not in this workshop's roster"):
        build(Command("grow-disk", slug="erin", size=300), FLEET, layout=layout)
    with pytest.raises(ActionError, match="outside this workshop's region"):
        elsewhere = (Row("alice", "198.51.100.1", "us-east-1"), *ROWS[1:])
        build(command("!grow-disk alice 300GB"), dataclasses.replace(FLEET, rows=elsewhere),
              layout=layout)  # fmt: skip
    with pytest.raises(ActionError, match="takes no spare"):
        build(Command("grow-disk", slug="alice", size=300, spare="zab-spare-1"), FLEET,
              layout=layout)  # fmt: skip


# ---------------------------------------------------------------------------------- hashes

RELEASE = "v0.3.0+a1b2c3d"


def dry_output(plan: int, suffix: str, stamp: str) -> str:
    tmp = f"/var/lib/zab-admin/tmp/P-{plan}"
    return (
        f"started {stamp}\n"
        f"dry-run: scp -o BatchMode=yes {tmp}/zab-push-{suffix}/config.toml "
        "ubuntu@198.51.100.1:/tmp/zab-push.DRYRUN/config.toml\n"
        f"dry-run: scp {tmp}/zab-push-{suffix}/alice-claude.zuliprc "
        "ubuntu@198.51.100.1:/tmp/zab-push.DRYRUN/alice-claude.zuliprc\n"
        "alice  198.51.100.1  ok  ok  \n"
        f"done in 3.2s\n"
    )


def hashed(action, live, inputs=None):
    return plan_hash(action, release=RELEASE, digests=inputs or digests(action), live=live)


@pytest.mark.parametrize(
    "message", ["!rollout v0.2.2 all steer=off", "!idle-stop 30 alice", "!waker on bob"]
)
def test_two_dry_runs_on_identical_inputs_hash_identically(message, layout):
    """Each dry run gets a fresh TMPDIR and push_config.py fresh `zab-push-…` directories."""
    one, two = make(message, layout), make(message, layout)
    first = Live(dry_run=dry_output(12, "k3j2l1x_", "2026-10-31T14:02:11Z"), tmpdir=tmp(12))
    second = Live(dry_run=dry_output(13, "a9b8c7d6", "2026-10-31 14:07:45"), tmpdir=tmp(13))
    assert hashed(one, first) == hashed(two, second)
    changed = Live(dry_run=first.dry_run.replace("198.51.100.1", "198.51.100.9"), tmpdir=tmp(12))
    assert hashed(one, changed) != hashed(one, first)


def tmp(plan: int) -> str:
    return f"/var/lib/zab-admin/tmp/P-{plan}"


def test_the_normalizer():
    text = dry_output(12, "k3j2l1x_", "2026-10-31T14:02:11.5+02:00")
    assert normalize_dry_run(text, tmp(12) + "/") == (
        "started <time>\n"
        "dry-run: scp -o BatchMode=yes <tmp>/config.toml "
        "ubuntu@198.51.100.1:/tmp/zab-push.DRYRUN/config.toml\n"
        "dry-run: scp <tmp>/alice-claude.zuliprc "
        "ubuntu@198.51.100.1:/tmp/zab-push.DRYRUN/alice-claude.zuliprc\n"
        "alice  198.51.100.1  ok  ok  \n"
        "done in <duration>\n"
    )
    for tmpdir in ("", "/"):
        with pytest.raises(ActionError, match="TMPDIR"):
            normalize_dry_run(text, tmpdir)


@pytest.mark.parametrize(
    "message",
    ["!provision zulip hold all", "!provision repos alice", "!provision repos invites-only alice"],
)
def test_a_reconcilers_hash_ignores_changed_live_state(message, layout):
    """#493: an invitation accepted, or sent outside the bot, between plan and approval changes
    the dry run, not the hash, so the plan doesn't turn STALE; no live race is needed (ADMIN §7,
    A5). One that another plan's apply sends or notices rewrites the roster's `github_invite`, an
    input: that plan goes STALE (fail closed), as the changed roster below shows."""
    action = make(message, layout)
    assert action.hashed == RECONCILER
    before = Live(dry_run="alice: invited, pending\n", vms={"alice": "i-1"}, tmpdir=tmp(12))
    after = Live(dry_run="alice: joined, bound to 4242\n", vms={"alice": "i-2"})
    assert hashed(action, before) == hashed(action, after)
    # …but not changed inputs, or another list
    inputs = {HARVARD.roster: "1" * 64}
    assert hashed(action, before, inputs) != hashed(action, before, {HARVARD.roster: "2" * 64})
    other = make(message.replace("hold ", "").replace("alice", "bob"), layout)
    assert hashed(other, before) != hashed(action, before)


@pytest.mark.parametrize(
    "message",
    [
        "!launch all",
        "!protect on alice",
        "!stop all",
        "!adopt-spare carol zab-spare-1",
        "!grow-disk alice 250GB",
    ],
)
def test_a_target_set_verb_hashes_its_targets_not_its_dry_run(message, layout):
    action = make(message, layout)
    assert action.hashed == TARGETS
    vms = {"alice": "i-0a", "dave": "i-0d", "zab-spare-1": "i-05", "erin": "i-0e"}
    live = Live(vms=vms, dry_run="would start", tmpdir=tmp(1))
    assert hashed(action, live) == hashed(action, Live(vms=dict(vms), dry_run="other"))
    for target in {action.targets[0], action.targets[-1]}:  # every target, the spares too
        assert hashed(action, live) != hashed(action, Live(vms={**vms, target: "i-new"}))
    # an instance outside the targets doesn't count
    assert hashed(action, live) == hashed(action, Live(vms={**vms, "frank": "i-0f"}))


def test_roster_import_hashes_its_inputs_and_no_live_zulip_state(layout):
    """#589: no Zulip lookup is in the hash (`Live` has none): roster_import.py checks the old
    and new addresses at its dry run and again at apply, which refuses a join since the plan.
    Its dry run is shown, not hashed; its inputs are."""
    action = make("!roster import [r.csv](/user_uploads/2/a/r.csv)", layout)
    assert action.hashed == INPUTS and not hasattr(Live(), "zulip")
    assert hashed(action, Live(dry_run="x")) == hashed(action, Live(dry_run="y"))
    assert set(target_material(action, Live())) == {"workshop", "bot"}
    one, two = digests(action) | {HARVARD.roster: "1" * 64}, digests(action)
    assert hashed(action, Live(), one) != hashed(action, Live(), two)


def test_the_target_material_names_the_workshop_and_its_bot(layout):
    for message in ("!stop all", "!provision zulip all", "!idle-stop 0 all"):
        material = target_material(make(message, layout), Live(tmpdir=tmp(1)))
        assert (material["workshop"], material["bot"]) == ("harvard-2026", 901)
    one = make("!stop all", layout, dataclasses.replace(OTHER, rows=(Row("erin"),)))
    two = make("!stop all", layout, dataclasses.replace(FLEET, rows=(Row("erin"),)))
    assert target_material(one, Live()) != target_material(two, Live())


def test_the_release_and_every_part_of_every_step_are_hashed(layout):
    action = make("!provision repos alice", layout)  # a reconciler: steps and inputs only
    live, step = Live(), action.steps[0]
    base = hashed(action, live)
    assert plan_hash(action, release="v9.9.9+0000000", digests=digests(action), live=live) != base
    for change in ({"env": ()}, {"dry_run": step.argv}, {"argv": step.argv[:-2]}):
        other = dataclasses.replace(action, steps=(dataclasses.replace(step, **change),))
        assert hashed(other, live) != base, change


def test_reads_have_no_plan_and_every_input_is_hashed(layout):
    with pytest.raises(ActionError, match="is a read"):
        hashed(make("!status all", layout), Live())
    action = make("!rollout v0.2.2 all", layout)
    with pytest.raises(ActionError, match="not hashed"):
        plan_hash(action, release=RELEASE, digests={HARVARD.roster: ""}, live=Live(tmpdir="/t"))


def test_digests_read_the_files(tmp_path, layout):
    roster = tmp_path / "ws" / "roster.csv"
    workshop = dataclasses.replace(HARVARD, roster=str(roster))
    action = make("!rollout v0.2.2 bob", layout, dataclasses.replace(FLEET, workshop=workshop))
    wheel = action.inputs[1]
    assert digests(action) == {str(roster): "", wheel: hashlib.sha256(b"wheel").hexdigest()}
    roster.parent.mkdir()
    roster.write_text("slug\nbob\n")
    assert digests(action)[str(roster)] == hashlib.sha256(b"slug\nbob\n").hexdigest()


def test_roster_import_keeps_slugs_unique_on_the_server(tmp_path, layout):
    """§2.1: the retired slugs and the other workshops' rosters, each passed when it exists
    (roster_import.py refuses a named file it can't read); a file that appears makes the plan
    stale, since each is an input whether it exists or not."""
    message = "!roster import [r.csv](/user_uploads/2/a/r.csv)"
    retired, other = tmp_path / "retired-slugs.txt", tmp_path / "ws" / "zab-test" / "roster.csv"
    layout = dataclasses.replace(layout, retired=str(retired))
    test = dataclasses.replace(TEST, roster=str(other))
    fleet = dataclasses.replace(FLEET, others=(test,))
    before = make(message, layout, fleet)
    assert "--retired" not in before.steps[0].argv
    assert "--workshop-roster" not in before.steps[0].argv
    assert before.inputs[-2:] == (str(retired), str(other))
    retired.write_text("old-slug\n")
    other.parent.mkdir(parents=True)
    other.write_text("slug\nerin\n")
    after = make(message, layout, fleet)
    tail = ("--retired", str(retired), "--workshop-roster", f"zab-test={other}")
    assert after.steps[0].argv[-4:] == tail
    assert after.steps[0].dry_run[-5:] == (*tail, "--dry-run")
    live = Live()
    assert hashed(before, live, digests(before)) != hashed(after, live, digests(after))
    with pytest.raises(ActionError, match="other workshops are not known"):
        make(message, layout, dataclasses.replace(FLEET, others=None))
    for others in ((HARVARD,), (dataclasses.replace(TEST, roster=HARVARD.roster),), ("x",)):
        with pytest.raises(ActionError, match="other Workshops"):
            dataclasses.replace(FLEET, others=others)


@pytest.mark.parametrize("message", ["!start all", "!start spares", "!start bob", "!stop all"])
def test_start_and_stop_never_wait(message, layout):
    """The bot's !start and !stop never pass `--wait`: they return once EC2 has the requests.
    Only the month-end's per-row start waits (§11.5), under `--wait`'s one deadline (#334)."""
    action = make(message, layout)
    argvs = [argv for step in action.steps for argv in (step.argv, step.dry_run) if argv]
    assert all("--wait" not in argv for argv in argvs)


def test_the_spares_idle_stop_follows_their_launch_or_start(layout):
    """#715: after `!launch … spares=N` and `!start` of spares, a step of its own with the
    workshop's `spare_idle_stop`, which the plan's note names; none for rows alone or a stop."""
    for message in ("!launch carol spares=2", "!start spares", "!start bob zab-spare-1"):
        action = make(message, layout)
        last = action.steps[1]  # right after the launch, or the start (review of #729)
        assert action.steps[0].argv[5].endswith(("/aws/launch.py", "/aws/power.py"))
        assert last.argv[5].endswith("/workshop/aws/spare_idle_stop.py") and last.dry_run is None
        assert last.argv[last.argv.index("--minutes") + 1] == "10", last
        assert ("--started" in last.argv) is message.startswith("!start"), last  # Codex on #729
        assert any(n.startswith("**The spares' idle stop:** after 10 idle") for n in action.notes)
        # a failed launch stops before it: the plan says how to give it to them then
        after = "If the launch fails, its spares may run without it: once the cause is fixed"
        assert any(n.startswith(after) for n in action.notes) is message.startswith("!launch")
    fifteen = dataclasses.replace(FLEET, workshop=dataclasses.replace(HARVARD, spare_idle_stop=15))
    last = make("!start spares", layout, fifteen).steps[-1].argv
    assert last[last.index("--minutes") + 1] == "15"
    rows = ("!launch carol", "!start all", "!start bob", "!stop spares", "!stop all spares")
    for message in rows:
        action = make(message, layout)
        assert not any("spare_idle_stop.py" in " ".join(s.argv) for s in action.steps), message
        assert not any("spares' idle stop" in n for n in action.notes), message


def test_adopt_spare_replays_the_fleets_settings(layout):
    """push_config.py's defaults (multi-party and steering on, idle stop left as it is) would
    lift the kill switch on the new VM (ORGANIZERS §4.1): both push_config steps replay them."""
    settings = ("multi-party=off", "steer=off", "idle-stop=15", "lean-shared", "raw-log=meta")
    action = make(
        "!adopt-spare carol zab-spare-1", layout, dataclasses.replace(FLEET, settings=settings)
    )
    push = ROLL.format(release=WHL.format(W=layout.wheels)) + (
        " --no-multi-party --no-claude-steer --idle-stop 15 --lean-shared --claude-raw-log meta"
    )
    assert action.steps[3].argv == tuple(f"{push} --adopt-spare carol --only carol".split())
    assert action.steps[5].argv == tuple(f"{push} --only carol".split())
    on = make(
        "!adopt-spare carol zab-spare-1",
        layout,
        dataclasses.replace(
            FLEET, settings=("multi-party=on", "steer=on", "idle-stop=0", "raw-log=off")
        ),
    )
    # an explicit idle-stop=0 is replayed as it is; lean-shared, never named, is its default
    tail = (
        "--multi-party --claude-steer --idle-stop 0 --claude-raw-log off --lean-shared-default "
        "--only carol"
    )
    assert on.steps[5].argv[-9:] == tuple(tail.split())
    assert on.steps[3].argv[-11:] == tuple(
        tail.replace("--only", "--adopt-spare carol --only").split()
    )


def test_adopt_spare_opts_the_vm_in_to_the_fleets_waker_before_its_idle_stop(layout):
    """#440: with the fleet's waker on (its last `!waker` of `all`), the new VM is opted in
    once its bots exist, after zulip_provision.py, and before either push_config.py step can
    install the replayed idle stop: the waker before idle stop (ADMIN §8.4)."""
    fleet = dataclasses.replace(FLEET, waker=True, settings=("idle-stop=60", "lean-shared"))
    action = make("!adopt-spare carol zab-spare-1", layout, fleet)
    scripts = [next(w for w in s.argv if w.endswith(".py")).rsplit("/", 1)[1] for s in action.steps]
    assert scripts == [
        "adopt_spare.py", "scan_hosts.py", "zulip_provision.py", "push_waker.py",
        "push_config.py", "provision_repos.py", "push_config.py",
    ]  # fmt: skip
    waker = f"{WAKER} --keys {OUT} --region us-west-2 {TAG} --organizers-channel 44 --only carol"
    assert action.steps[3] == Step(tuple(f"{waker} --apply".split()))
    assert "--idle-stop" in action.steps[4].argv and "--idle-stop" in action.steps[6].argv
    off = make("!adopt-spare carol zab-spare-1", layout, dataclasses.replace(fleet, waker=False))
    assert [s.argv for s in off.steps] == [s.argv for s in action.steps if s != action.steps[3]]
    quiet = dataclasses.replace(fleet, workshop=dataclasses.replace(HARVARD, alerts_channel=None))
    refused = "the fleet's waker is on, and this workshop has no alerts_channel in admin.toml: "
    with pytest.raises(ActionError, match=re.escape(refused + "!adopt-spare refused")):
        make("!adopt-spare carol zab-spare-1", layout, quiet)
    # without the waker, the chain needs no alerts channel: as before this change
    plain = make("!adopt-spare carol zab-spare-1", layout, dataclasses.replace(quiet, waker=False))
    assert [s.argv for s in plain.steps] == [s.argv for s in off.steps]


@pytest.mark.parametrize("seconds", [0, 300])
def test_the_workshops_mention_after_s_goes_where_config_toml_is_written(layout, seconds):
    """#711: admin.toml's `mention_after_s` reaches each push_config.py step that installs a
    release, and so writes config.toml: `!rollout`'s, its dry run included, which prints the
    value (`mention after: …`), and both of `!adopt-spare`'s, beside the fleet's replayed
    settings. A step without a release writes no config, and gets none."""
    workshop = dataclasses.replace(HARVARD, mention_after_s=seconds)
    fleet = dataclasses.replace(FLEET, workshop=workshop, settings=("steer=off",))
    rollout = make("!rollout v0.2.2 all steer=off", layout, fleet)
    adopt = make("!adopt-spare carol zab-spare-1", layout, fleet)
    pushes = [s for s in (*rollout.steps, *adopt.steps) if any("push_config" in w for w in s.argv)]
    assert len(pushes) == 3 and rollout.steps[0].dry_run and not pushes[1].dry_run
    for argv in (*(s.argv for s in pushes), rollout.steps[0].dry_run):
        at = [i for i, word in enumerate(argv) if word == "--mention-after-s"]
        assert len(at) == 1 and argv[at[0] + 1] == str(seconds), argv
        assert "--no-claude-steer" in argv  # beside the command's words, or the replayed ones
    for message in ("!idle-stop 30 alice", "!uninstall all"):
        steps = make(message, layout, fleet).steps
        assert all("--mention-after-s" not in (*s.argv, *(s.dry_run or ())) for s in steps)


def test_the_workshops_organization_owns_its_file_links(layout):
    """#693 criterion 2: each push_config.py step that writes config.toml passes the workshop's
    `github_org` as `--refserve-owner`, beside its `refserve_url`, as `provision_repos.py` gets
    it as `--org`: another organization's workshop gets its own, never the scripts' default.
    A step without a release writes no config, and gets none."""
    workshop = dataclasses.replace(TEST, mention_after_s=120)
    fleet = dataclasses.replace(FLEET, workshop=workshop, rows=ROWS, others=(HARVARD,))
    rollout = make("!rollout v0.2.2 all", layout, fleet)
    adopt = make("!adopt-spare carol zab-spare-1", layout, fleet)
    pushes = [s for s in (*rollout.steps, *adopt.steps) if any("push_config" in w for w in s.argv)]
    assert len(pushes) == 3
    for argv in (*(s.argv for s in pushes), rollout.steps[0].dry_run):
        at = [i for i, word in enumerate(argv) if word == "--refserve-owner"]
        assert len(at) == 1 and argv[at[0] + 1] == "MathAgents-test", argv
        url = argv.index("--refserve-url")
        assert argv[url + 1] == "https://files.example.org" and url < at[0]
    repos = [s.argv for s in adopt.steps if any("provision_repos" in w for w in s.argv)]
    assert repos and all(a[a.index("--org") + 1] == "MathAgents-test" for a in repos)
    for message in ("!idle-stop 30 alice", "!uninstall all"):
        steps = make(message, layout, fleet).steps
        assert all("--refserve-owner" not in (*s.argv, *(s.dry_run or ())) for s in steps)


@pytest.mark.parametrize(
    "settings",
    [
        ("restart-codex",),
        ("steer=maybe",),
        ("steer",),
        ("multi-party",),
        ("idle-stop=5",),
        ("idle-stop=1e3",),
        ("raw-log=content",),
        ("steer=off", "steer=on"),
        ("lean-shared", "lean-shared"),
        ("hold",),
        ("--no-multi-party",),
        ["steer=off"],
        (False,),
    ],
)
def test_recorded_settings_outside_the_rollouts_flags_are_refused(settings):
    with pytest.raises(ActionError):
        dataclasses.replace(FLEET, settings=settings)


def test_a_selection_of_the_wrong_types_is_refused(layout):
    """A truthy "false" read back from a stored plan must never mean `all` (the review of
    #333): the grammar refuses it, and so does actions.py on its own."""
    for fields in ({"all": "false"}, {"spares": "no"}, {"slugs": ["alice"]}, {"slugs": (1,)}):
        with pytest.raises(ValueError, match="two bools"):
            Selection(**{"slugs": ("alice",), **fields})
    for verb in ("status", "provision zulip", "stop"):
        for fields in ({"all": "false"}, {"spares": "no"}, {"slugs": ["alice"]}):
            forged = forge(
                minimal(verb, ALL), selection=raw_selection(**{"slugs": ("alice",), **fields})
            )
            with pytest.raises(ActionError):
                build(forged, FLEET, layout=layout)
