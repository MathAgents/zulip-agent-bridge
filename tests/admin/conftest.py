"""A server for the plans and approvals tests: two workshops, each with its own bot and
organizers, and the `[global]` bot (docs/ADMIN.md §2.1)."""

import re

import pytest

from zab.admin.actions import TARGETS, Action, Fleet, Layout, Row, build
from zab.admin.approvals import Approvals
from zab.admin.config import RELEASE_PATTERNS, parse_admin_config
from zab.admin.grammar import Command, Context, Roster, parse
from zab.admin.plans import Plan, PlanStore

CONFIG = parse_admin_config(
    """\
provider = "aws"
admins = [101, 102]                 # global admins
deployers = [101, 106]              # 106 deploys, and organizes nothing
destructive_approvals = 2
plan_ttl_minutes = 10

[global]
scope = "mathagents"
regions = ["us-west-2", "us-east-1"]
instance_types = ["m7a.2xlarge", "t3.small"]
zuliprc = "/etc/zab-admin/secrets/global.zuliprc"
user_id = 900
channels = [41]

[[workshop]]
name = "harvard-2026"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "/var/lib/zab-admin/ws/harvard-2026/roster.csv"
out_dir = "/var/lib/zab-admin/ws/harvard-2026/out"
organizers = [103, 104]
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/bots/harvard-2026.zuliprc"
user_id = 901
channels = [42]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-harvard-2026"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub"]

[[workshop]]
name = "zab-test"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "/var/lib/zab-admin/ws/zab-test/roster.csv"
out_dir = "/var/lib/zab-admin/ws/zab-test/out"
organizers = [105]
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/bots/zab-test.zuliprc"
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
ROWS = (Row("alice", "198.51.100.1"), Row("bob", "198.51.100.2"), Row("carol"), Row("dave"))
FLEET = Fleet(HARVARD, ROWS, ("zab-spare-1", "zab-spare-2"), PRODUCTION, others=(TEST,))
OTHER = Fleet(TEST, (Row("erin", "203.0.113.5", "us-east-1"),), (), PRODUCTION, others=(HARVARD,))
HASH = "3f9a2c" + "5" * 58


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    plans = PlanStore(
        tmp_path / "state.db", ttl_minutes=CONFIG.plan_ttl_minutes, floor=0, clock=clock
    )
    yield plans
    plans.close()


@pytest.fixture
def wheels(tmp_path):
    """A layout whose wheels directory holds v0.2.2's, for `!rollout`."""
    (tmp_path / "wheels").mkdir()
    (tmp_path / "wheels" / "zulip_agent_bridge-0.2.2-py3-none-any.whl").write_bytes(b"wheel")
    return Layout(wheels=str(tmp_path / "wheels"))


def make(
    message: str, fleet: Fleet = FLEET, layout: Layout | None = None, upload: str | None = None
) -> Action:
    """`message` as the fleet's own bot parses and builds it; `upload`, for `!roster import`."""
    roster = Roster(fleet.slugs, fleet.spares)
    ctx = Context(
        fleet.workshop.bot.user_id, f"Workshop admin · {fleet.workshop.name}", PRODUCTION, roster
    )
    command = parse(message, ctx)
    assert isinstance(command, Command), command
    return build(command, fleet, layout=layout or Layout(), upload=upload)


def direct(
    verb: str,
    args: tuple[str, ...],
    targets: tuple[str, ...] = (),
    ws: str = "harvard-2026",
    inputs: tuple[str, ...] = (),
):
    """An action of a verb that no bot offers yet (stage 3, 6 or 7), built by hand."""
    bot = CONFIG.workshop(ws).bot.user_id if ws else CONFIG.global_bot.user_id
    return Action(verb, ws, bot, args, (), TARGETS, targets, inputs)


def posted(
    store: PlanStore,
    action: Action,
    *,
    fleet: Fleet | None = FLEET,
    requester: int = 103,
    rebinds: tuple[str, ...] = (),
    plan_hash: str = HASH,
    config=CONFIG,
) -> Plan:
    """A PENDING plan for `action`, its messages posted as P-n's IDs 100n and 100n + 1."""
    command = " ".join(("!" + action.verb, *action.args))
    plan = store.begin(
        workshop=action.workshop, bot=action.bot, requester=requester, command=command
    )
    ids = {"plan_message": 100 * plan.id, "approval_message": 100 * plan.id + 1}
    return Approvals(store, config).post(plan.id, action, fleet, plan_hash, rebinds=rebinds, **ids)
