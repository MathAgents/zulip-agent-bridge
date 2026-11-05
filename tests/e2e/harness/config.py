"""Where the rig is (issue #263): the test VM's scope, the test users, the keys, the channel.

Everything comes from `ZAB_E2E_*` environment variables, with defaults in `~/.config/zab/` on
the development machine. Nothing here is ever committed (CONTRIBUTING.md, rule 1), and only
paths are kept: a
file's contents are read where they are used, and never printed.

| Variable | Default |
|---|---|
| `ZAB_E2E_SLUG` | `zab-test` (the roster row, the `participant` tag) |
| `ZAB_E2E_REGION` | `us-east-1` |
| `ZAB_E2E_SCOPE` | `zab-role=test,workshop=zab-test` (must contain `zab-role=test`) |
| `ZAB_E2E_ROSTER` | `~/.config/zab/test-roster.csv` |
| `ZAB_E2E_TESTER_ZULIPRC` | `~/.config/zab/tester.zuliprc` (`zab-tester`, SPEC §15.3) |
| `ZAB_E2E_TESTER2_ZULIPRC` | `~/.config/zab/tester2.zuliprc` (`zab-tester2`) |
| `ZAB_E2E_SSH_KEY` | `~/.ssh/id_ed25519` (the private key of the VM's EC2 key pair, for `ubuntu`) |
| `ZAB_E2E_OUT_DIR` | `~/.config/zab/test-out` (bot zuliprcs, `zulip_provision.py --out-dir`) |
| `ZAB_E2E_KNOWN_HOSTS` | `~/.config/zab/test-known_hosts` (`scan_hosts.py --known-hosts`) |
| `ZAB_E2E_CHANNEL` | `zab-test-sandbox` (zab-tester and the bots only, as a participant's) |
| `ZAB_E2E_SHARED_CHANNEL` | `zab-test-shared` (both testers and the bots: scenario 13) |
| `ZAB_E2E_CLAUDE_BOT`, `ZAB_E2E_CODEX_BOT` | `Claude · zab-test`, `Codex · zab-test` |
| `ZAB_E2E_REPO` | `MathAgents/zab-test` |
| `ZAB_E2E_FILES_URL` | `https://files.mathagents.org` |
| `ZAB_E2E_REPORT_DIR` | `~/.local/state/zab-e2e` |
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Every resource the harness touches carries this tag: test infrastructure only.
TEST_ROLE = ("zab-role", "test")
CHECKOUT = Path(__file__).resolve().parents[3]
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[0-9]$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_TAG_RE = re.compile(r"^[A-Za-z0-9 _.:/=+@-]{1,128}$")
#: The rig posts in the zab-dev* channels and the test participant's (zab-test*) only
_CHANNEL_RE = re.compile(r"^zab-(test|dev)[A-Za-z0-9_.-]{0,50}$")


@dataclass(frozen=True)
class RigConfig:
    slug: str
    region: str
    scope: tuple[tuple[str, str], ...]
    roster: Path
    tester_zuliprc: Path
    tester2_zuliprc: Path
    ssh_key: Path
    out_dir: Path
    known_hosts: Path
    #: zab-tester and the bots only, like a participant's sandbox: a follow-up needs no mention
    channel: str
    #: zab-tester, zab-tester2 and the bots (scenario 13): multi-party, mentions required
    shared_channel: str
    claude_bot: str
    codex_bot: str
    repo: str
    files_url: str
    report_dir: Path
    ssh_user: str = "ubuntu"

    @property
    def instance_tags(self) -> dict[str, str]:
        """What the test VM carries: every scope tag and `participant=<slug>`."""
        return {**dict(self.scope), "participant": self.slug}

    @property
    def bots(self) -> dict[str, str]:
        """Backend kind → the bot's full name."""
        return {"claude": self.claude_bot, "codex": self.codex_bot}

    def missing(self) -> list[str]:
        """The input files that are not there (paths only), for one clear error up front."""
        files = {
            "ZAB_E2E_ROSTER": self.roster,
            "ZAB_E2E_TESTER_ZULIPRC": self.tester_zuliprc,
            "ZAB_E2E_TESTER2_ZULIPRC": self.tester2_zuliprc,
            "ZAB_E2E_SSH_KEY": self.ssh_key,
            "ZAB_E2E_KNOWN_HOSTS": self.known_hosts,
        }
        return [f"{var}: {path}" for var, path in files.items() if not path.is_file()]


def parse_scope(text: str) -> tuple[tuple[str, str], ...]:
    """`k=v,k=v` → the scope; it must contain `zab-role=test` (test infrastructure only)."""
    scope: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        key, sep, value = part.partition("=")
        if not sep or not _TAG_RE.match(key) or not _TAG_RE.match(value):
            raise ValueError(f"ZAB_E2E_SCOPE: expected KEY=VALUE, got {part!r}")
        if key in ("participant", "Name"):
            raise ValueError(f"ZAB_E2E_SCOPE: {key} is set per participant")
        scope[key] = value
    if scope.get(TEST_ROLE[0]) != TEST_ROLE[1]:
        raise ValueError("ZAB_E2E_SCOPE must contain zab-role=test: the rig acts on nothing else")
    return tuple(scope.items())


def load(env: Mapping[str, str] | None = None) -> RigConfig:
    """The rig from the environment (`os.environ` by default); ValueError on a bad value."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home()).absolute()

    def path(var: str, default: Path) -> Path:
        value = env.get(var)
        # absolute: the runner and pytest work in other directories (codex on #283)
        return Path(value).expanduser().absolute() if value else default

    def text(var: str, default: str) -> str:
        return (env.get(var) or default).strip()

    config_dir = home / ".config" / "zab"
    out_dir = path("ZAB_E2E_OUT_DIR", config_dir / "test-out")
    cfg = RigConfig(
        slug=text("ZAB_E2E_SLUG", "zab-test"),
        region=text("ZAB_E2E_REGION", "us-east-1"),
        scope=parse_scope(text("ZAB_E2E_SCOPE", "zab-role=test,workshop=zab-test")),
        roster=path("ZAB_E2E_ROSTER", config_dir / "test-roster.csv"),
        tester_zuliprc=path("ZAB_E2E_TESTER_ZULIPRC", config_dir / "tester.zuliprc"),
        tester2_zuliprc=path("ZAB_E2E_TESTER2_ZULIPRC", config_dir / "tester2.zuliprc"),
        ssh_key=path("ZAB_E2E_SSH_KEY", home / ".ssh" / "id_ed25519"),
        out_dir=out_dir,
        known_hosts=path("ZAB_E2E_KNOWN_HOSTS", config_dir / "test-known_hosts"),
        channel=text("ZAB_E2E_CHANNEL", "zab-test-sandbox"),
        shared_channel=text("ZAB_E2E_SHARED_CHANNEL", "zab-test-shared"),
        claude_bot=text("ZAB_E2E_CLAUDE_BOT", "Claude · zab-test"),
        codex_bot=text("ZAB_E2E_CODEX_BOT", "Codex · zab-test"),
        repo=text("ZAB_E2E_REPO", "MathAgents/zab-test"),
        files_url=text("ZAB_E2E_FILES_URL", "https://files.mathagents.org").rstrip("/"),
        report_dir=path("ZAB_E2E_REPORT_DIR", home / ".local" / "state" / "zab-e2e"),
    )
    if not _SLUG_RE.match(cfg.slug):
        raise ValueError(f"ZAB_E2E_SLUG: invalid slug {cfg.slug!r}")
    if not _REGION_RE.match(cfg.region):
        raise ValueError(f"ZAB_E2E_REGION: invalid region {cfg.region!r}")
    if not _REPO_RE.match(cfg.repo):
        raise ValueError(f"ZAB_E2E_REPO: expected owner/name, got {cfg.repo!r}")
    if not cfg.files_url.startswith("https://"):
        raise ValueError("ZAB_E2E_FILES_URL must be an https:// URL")
    for var, name in (
        ("ZAB_E2E_CHANNEL", cfg.channel),
        ("ZAB_E2E_SHARED_CHANNEL", cfg.shared_channel),
    ):
        if not _CHANNEL_RE.match(name):  # the only channels the rig may post in
            raise ValueError(f"{var}: {name!r} is not a zab-test* or zab-dev* channel")
    return cfg
