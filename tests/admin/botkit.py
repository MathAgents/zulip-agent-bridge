"""A test admin server in a temporary tree: two workshops, each with its bot, channel and
organizers, the `[global]` bot, and a fake realm with their people (docs/ADMIN.md §2.1, §7)."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zab.admin import checks
from zab.admin.actions import Layout
from zab.admin.bot import AdminServer, Incoming, Paths
from zab.admin.config import RELEASE_PATTERNS, Roots, parse_admin_config
from zab.zulip.client import ZulipClient

from .fake_realm import FakeRealm

DREW, ALICE = 101, 102  # global admins
OLGA, OSCAR = 103, 104  # organizers of harvard-2026
TESS = 105  # organizer of zab-test
DEE = 106  # a deployer, and nothing else
PAT = 107  # a participant: a person nobody's allowlist has
CODEX_BOB = 501  # a participant's bot
CODEX_CAROL = 502  # another participant's bot
NOTIFICATION_BOT = 100006  # Zulip's own, in `cross_realm_bots`
GLOBAL, HARVARD, TEST = 900, 901, 902  # the admin bots
PRODUCTION_PATTERN = re.compile(RELEASE_PATTERNS["final"], re.ASCII)
#: Zulip's italics, `zerver/lib/markdown`'s `EMPHASIS_RE` (#651): `*`, no blank after it, no
#: `*`, `^` or line break inside, no blank before the closing `*`. Zulip reads a link before
#: italics (priority 60 to 30), so a link inside is fine. There is no `_…_` emphasis.
ZULIP_ITALIC = re.compile(r"(\*)(?!\s+)([^\*^\n]+)(?<!\s)\*")
ADMINS_CH, HARVARD_CH, TEST_CH = 41, 42, 43
NAMES = {
    DREW: "Drew",
    ALICE: "Alice Admin",
    OLGA: "Olga Organizer",
    OSCAR: "Oscar Organizer",
    TESS: "Tess Tester",
    DEE: "Dee Deployer",
    PAT: "Pat Participant",
}
BOTS = {
    CODEX_BOB: "Codex · bob",
    CODEX_CAROL: "Codex · carol",
    NOTIFICATION_BOT: "Notification Bot",
    GLOBAL: "Workshop admin",
    HARVARD: "Workshop admin · harvard-2026",
    TEST: "Workshop admin · zab-test",
}
HARVARD_ROSTER = (
    "slug,first_name,last_name,email,github,ip,region\n"
    "alice,Alice,A,alice@example.com,alice-gh,198.51.100.1,us-west-2\n"
    "bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2\n"
    "carol,Carol,C,carol@example.com,carol-gh,,\n"
)
TEST_ROSTER = "slug,first_name,email,ip,region\nerin,Erin,erin@example.com,203.0.113.5,us-east-1\n"
#: A well-formed key no one holds: the bots start, and a backup made with `age` reaches no one.
#: The backup tests write their own (`test_admin_backup`).
RECIPIENT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def config_text(tmp: Path) -> str:
    etc, ws = tmp / "etc", tmp / "ws"
    workshop = """
[[workshop]]
name = "{name}"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "{ws}/{name}/roster.csv"
out_dir = "{ws}/{name}/out"
organizers = {organizers}
[workshop.bot]
zuliprc = "{etc}/secrets/bots/{name}.zuliprc"
user_id = {bot}
channels = [{channel}]
[workshop.aws]
region = "{region}"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-{name}"
admin_keys = ["{etc}/break-glass/drew.pub"]
"""
    head = f"""provider = "aws"
admins = [{DREW}, {ALICE}]
deployers = [{DREW}, {DEE}]
destructive_approvals = 2
plan_ttl_minutes = 10

[global]
scope = "mathagents"
regions = ["us-west-2", "us-east-1"]
instance_types = ["m7a.2xlarge"]
zuliprc = "{etc}/secrets/global.zuliprc"
user_id = {GLOBAL}
channels = [{ADMINS_CH}]
"""
    one = workshop.format(
        name="harvard-2026", ws=ws, etc=etc, organizers=[OLGA, OSCAR], bot=HARVARD,
        channel=HARVARD_CH, region="us-west-2",
    )  # fmt: skip
    two = workshop.format(
        name="zab-test", ws=ws, etc=etc, organizers=[TESS], bot=TEST, channel=TEST_CH,
        region="us-east-1",
    )  # fmt: skip
    return head + one + two


@dataclass
class Site:
    tmp: Path
    realm: FakeRealm
    clock: Clock
    config_path: Path
    paths: Paths
    modes: checks.Modes

    @property
    def roots(self) -> Roots:
        return Roots(
            workshops=str(self.tmp / "ws"),
            secrets=str(self.tmp / "etc" / "secrets"),
            break_glass=str(self.tmp / "etc" / "break-glass"),
        )

    def server(self, **kwargs: Any) -> AdminServer:
        config = parse_admin_config(self.config_path.read_text(), roots=self.roots)
        transport = self.realm.transport

        def connect(path: str) -> ZulipClient:
            return ZulipClient.from_zuliprc(path, transport=transport)

        options = {"paths": self.paths, "modes": self.modes, "connect": connect}
        return AdminServer(
            config, config_path=str(self.config_path), clock=self.clock, **(options | kwargs)
        )

    @property
    def audit(self) -> Path:
        return Path(self.paths.audit)


def make_site(tmp: Path) -> Site:
    realm = FakeRealm()
    for uid, name in NAMES.items():
        realm.person(uid, name)
    for uid, name in BOTS.items():
        realm.person(uid, name, bot=True, system=uid == NOTIFICATION_BOT)
    realm.channel(ADMINS_CH, DREW, ALICE, DEE, GLOBAL)
    realm.channel(HARVARD_CH, DREW, ALICE, OLGA, OSCAR, HARVARD)
    realm.channel(TEST_CH, DREW, ALICE, TESS, TEST)
    etc = tmp / "etc"
    etc.mkdir(mode=0o755)
    config_path = _write(etc / "admin.toml", config_text(tmp))
    for name, uid in (("global", GLOBAL), ("bots/harvard-2026", HARVARD), ("bots/zab-test", TEST)):
        _write(etc / "secrets" / f"{name}.zuliprc", realm.zuliprc(uid), 0o640)
    _write(etc / "secrets" / "provisioner.zuliprc", realm.zuliprc(DREW), 0o640)
    _write(etc / "secrets" / "github-token", "github_pat_" + "Z" * 40 + "\n", 0o640)
    _write(etc / "break-glass" / "drew.pub", "ssh-ed25519 AAAAC3Nza drew@laptop\n")
    _write(etc / "backup_recipients", f"# the deployers' keys\n{RECIPIENT} dee@laptop\n")
    _write(tmp / "ws" / "harvard-2026" / "roster.csv", HARVARD_ROSTER, 0o600)
    _write(tmp / "ws" / "zab-test" / "roster.csv", TEST_ROSTER, 0o600)
    for name in ("harvard-2026", "zab-test"):
        (tmp / "ws" / name / "out").mkdir(mode=0o700)
    (tmp / "state").mkdir(mode=0o700)
    (tmp / "starter" / "log").mkdir(mode=0o750, parents=True)  # the start log's (#359)
    (tmp / "starter").chmod(0o711)
    layout = Layout(
        provisioner_zuliprc=str(etc / "secrets" / "provisioner.zuliprc"),
        runs=str(tmp / "state" / "runs"),
        wheels=str(tmp / "wheels"),
        retired=str(tmp / "state" / "retired-slugs.txt"),
        waker=str(tmp / "state" / "waker"),  # its config.toml: the opt-ins (#359)
    )
    paths = Paths(
        layout=layout,
        state=str(tmp / "state"),
        deploys=str(tmp / "opt" / "deploys.jsonl"),
        secrets=str(etc / "secrets"),
        recipients=str(etc / "backup_recipients"),
        workshops=str(tmp / "ws"),
        age=str(tmp / "no-age"),  # a backup fails, and says so, unless a test gives it age
        start_log=str(tmp / "starter" / "log"),
        waker_status=str(tmp / "waker-status" / "waker.json"),  # none: no waker has run (#717)
    )
    modes = checks.Modes(
        owners=frozenset({os.getuid()}), group=os.getgid(), top=str(etc), starter=os.getuid()
    )
    return Site(tmp, realm, Clock(), config_path, paths, modes)


def incoming(
    sender: int,
    content: str,
    *,
    stream: int | None = HARVARD_CH,
    to: tuple[int, ...] = (),
    flags: tuple[str, ...] = (),
    mid: int = 5000,
    topic: str = "ops",
) -> Incoming:
    """A message as a bot's queue delivers it: in `stream`, or a DM to `to` when it is None."""
    name = (NAMES | BOTS)[sender]
    recipients = frozenset() if stream is not None else frozenset({sender, *to})
    return Incoming(mid, sender, name, stream, topic, content, frozenset(flags), recipients)


async def until(check: Callable[[], bool], seconds: float = 30.0) -> None:
    """`check` every 10 ms, for at least `seconds`. The default only bounds a failure: 5 s was
    thin for a dry run, a recheck and a step's start on a box running other suites (#473)."""
    for _ in range(int(seconds * 100)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("the condition never held")
