"""The admin e2e rig's names and inputs (docs/ADMIN.md §7; issue #356).

Everything the rig creates is named here, and is test infrastructure: AWS resources carry
`zab-role=test`, IAM names start with `zab-test-`, Zulip channels with `zab-dev`. The inputs come
from `ZAB_ADMIN_E2E_*` variables, with defaults on the development machine; the keys, the bots'
zuliprcs and the test `admin.toml` with real IDs live under `~/.config/zab/admin-e2e/` (the rig's
directory), never in git (CONTRIBUTING.md, rule 1). Only paths are kept here: a file's contents are
read where they are used, and never printed.

| Variable | Default |
|---|---|
| `ZAB_ADMIN_E2E_DIR` | `~/.config/zab/admin-e2e` (keys, secrets, etc, the release repo) |
| `ZAB_ADMIN_E2E_REGION` | `us-east-1`, the rig's (#263) |
| `ZAB_ADMIN_E2E_AMI` | the account's newest image tagged `zab-role=test`, named `zab-test-*` |
| `ZAB_ADMIN_E2E_SSH_KEY` | `~/.ssh/id_ed25519`, the private key of the key pair below |
| `ZAB_ADMIN_E2E_KEY_PAIR` | `zab-test`, the rig's key pair: `ubuntu` on the admin server |
| `ZAB_ADMIN_E2E_SSH_GROUP` | `zab-test-ssh`: the server's group admits SSH from its sources |
| `ZAB_ADMIN_E2E_REPORT_DIR` | `~/.local/state/zab-admin-e2e` |
| `ZAB_E2E_TESTER_ZULIPRC`, `ZAB_E2E_TESTER2_ZULIPRC` | as for #263 (`harness/config.py`) |

Optional inputs (ADMIN §10, Q9), in the rig's directory: `github-token`, the test server's PAT
for `MathAgents/zab-test*`; `provisioner.zuliprc`, `zab-tester`'s zuliprc as the test
provisioning key, placed once Drew agrees. Until each is there, the server gets a random
placeholder and the scenarios that need it are skipped. `tester3.zuliprc`, a third test user,
for the two-approver rule.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# ------------------------------------------------------------------------------- names

TEST_ROLE = ("zab-role", "test")
#: The rig's fleet-wide scope (ADMIN §5, §7; #422): `[global] scope` of the test admin.toml, on
#: every resource the server launches or launches from, beside `zab-role=test`. Production's is
#: `mathagents`, in another account.
SCOPE = ("zab-scope", "admin-e2e")
#: The rig's region set up as an account owner sets one up (BOOTSTRAP §6; ADMIN §5): its fleet
#: group `zab-fleet`, in the scope and in no workshop, which `!new-workshop` finds by its name
#: and scope and records in the new entry (A16, #418). The scope alone can't find it: the
#: workshops' own groups carry it too. SSH from the development machine only: no VM is launched from
#: it.
FLEET_GROUP = "zab-fleet"
FLEET_TAGS = {SCOPE[0]: SCOPE[1], "zab-role": "test"}
#: The server: its Name tag, its IAM role and instance profile (`zab-test-*`, test infrastructure).
SERVER = "zab-test-admin"
#: The server's own tags: never `workshop=` (ADMIN §1.1).
SERVER_TAGS = {"zab-role": "test", "zab-admin": "e2e", "Name": SERVER}
SERVER_TYPE = "t4g.small"
SERVER_GROUP = "zab-test-admin-server"
#: Canonical's Ubuntu 24.04 images for arm64 (stock Ubuntu, not the workshop AMI: ADMIN §1.1).
UBUNTU_OWNER = "099720109477"
UBUNTU_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"
PARTICIPANT_TYPE = "t3.medium"
#: A13's detached network interface, standing for another service's: tagged as test
#: infrastructure, never `workshop=`. The scenario makes it and deletes it, and every teardown
#: deletes one a run left (`AwsRig.drop_probe_interfaces`).
PROBE_ENI_TAGS = {"zab-role": "test", "Name": "zab-admin-e2e-eni-probe"}
#: The SSH alias deploy.sh is given (`--host`); the rig's ssh config maps it to the server.
HOST_ALIAS = "zab-test-admin"


@dataclass(frozen=True)
class Workshop:
    name: str
    group: str  # its participants' security group
    bot: str  # its admin bot's full name
    short: str  # the bot's short name (its e-mail's local part)
    channel: str  # the bot's private channel
    alerts: str  # its `alerts_channel`: the waker's failed wakes, posted by participant bots
    #: its participants' channel (`channel` in admin.toml, #627), whose subscribers `!roster
    #: sync` makes rows of (A22): the test provisioner and `zab-tester2`; "" for none
    participants: str = ""

    @property
    def tags(self) -> dict[str, str]:
        """Its scope, as the loader makes it: every resource of its fleet carries all three."""
        return {"workshop": self.name, SCOPE[0]: SCOPE[1], TEST_ROLE[0]: TEST_ROLE[1]}

    @property
    def ends(self) -> dict[str, str]:
        """What a teardown looks for: the workshop and the test tag, not the scope, so that
        what a run left before the scope existed (#422) is ended too."""
        return {"workshop": self.name, TEST_ROLE[0]: TEST_ROLE[1]}

    @property
    def key_name(self) -> str:
        return f"zab-admin-{self.name}"


E2E = Workshop(
    "zab-admin-e2e", "zab-test-admin-e2e", "Workshop admin · e2e", "zab-dev-admin-e2e",
    "zab-dev-organizers", "zab-dev-alerts",
)  # fmt: skip
E2E2 = Workshop(
    "zab-admin-e2e2", "zab-test-admin-e2e2", "Workshop admin · e2e2", "zab-dev-admin-e2e2",
    "zab-dev-organizers-2", "zab-dev-alerts-2", "zab-dev-participants-2",
)  # fmt: skip
WORKSHOPS = (E2E, E2E2)
#: The global admin bot (`[global]`), its short name and its channel.
GLOBAL_BOT, GLOBAL_SHORT = "Workshop admin · test", "zab-dev-admin-test"
ADMINS_CHANNEL = "zab-dev-admins"
#: A16's throwaway workshop (#418): `zab-dev-e3-<hex>`, a new name each run, its channels
#: `zab-dev-e3-<hex>-organizers` and `-fleet-alerts` (the dev org's zab-dev* only).
THROWAWAY = "zab-dev-e3"

# --------------------------------------------------------------------------- scenarios

#: ADMIN §7's scenarios: number → (label, title, stage of §8.1). A10b is 110. The later
#: stages' scenarios are listed so that a selection can name them: until their stage lands,
#: each is a hook that skips with its reason.
SCENARIOS: dict[int, tuple[str, str, int]] = {
    1: ("A1", "Refusals", 1),
    2: ("A2", "Roster", 1),
    3: ("A3", "Binding", 1),
    4: ("A4", "Launch and protect", 1),
    5: ("A5", "Provisioning", 1),
    6: ("A6", "Rollout", 1),
    7: ("A7", "Stop and start", 1),
    8: ("A8", "Waker", 4),
    9: ("A9", "The end of a workshop", 3),
    10: ("A10", "Deploy gate", 2),
    11: ("A11", "Console", 5),
    12: ("A12", "Secret sweep", 2),
    13: ("A13", "Role confinement", 2),
    14: ("A14", "Backup", 2),
    15: ("A15", "Two workshops", 1),
    16: ("A16", "!new-workshop", 7),
    17: ("A17", "Month-end", 8),
    18: ("A18", "A VM launched without the server's key joins", 4),
    19: ("A19", "The export sandbox", 3),
    20: ("A20", "A spare stops by itself; a late joiner gets it, the waker on", 4),
    21: ("A21", "!grow-disk, online and at the next boot", 1),
    22: ("A22", "The roster from the workshop's channel", 1),
    23: ("A23", "The rollout's defaults: the shared Mathlib and idle stop", 4),
    110: ("A10b", "!upgrade-server", 6),
}
#: The go criteria (ADMIN §8.2): A1 to A7, A10 and A12 to A15.
GO = (1, 2, 3, 4, 5, 6, 7, 10, 12, 13, 14, 15)
LABELS = {label.lower(): n for n, (label, _, _) in SCENARIOS.items()}


def select(spec: str) -> list[int]:
    """`go` (ADMIN §8.2's), `all`, or labels and ranges (`A1,A10,A12-A15`) → numbers, sorted."""
    chosen: set[int] = set()
    for part in filter(None, (p.strip().lower() for p in spec.split(","))):
        if part in ("all", "go"):
            chosen |= set(SCENARIOS) if part == "all" else set(GO)
            continue
        lo, _, hi = part.partition("-")
        if lo not in LABELS or (hi and hi not in LABELS):
            raise ValueError(f"no scenario {part!r}: A1-A23, A10b, go or all")
        first, last = LABELS[lo], LABELS[hi or lo]
        numbers = {n for n in SCENARIOS if first <= n <= last and n < 100} | {first, last}
        chosen |= numbers
    if not chosen:
        raise ValueError("no scenario selected")
    return sorted(chosen)


def label(number: int) -> str:
    return SCENARIOS[number][0]


# ------------------------------------------------------------------------------ inputs

_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[0-9]$")
_AMI_RE = re.compile(r"^(ami-[0-9a-f]{8,17})?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class AdminRig:
    dir: Path
    region: str
    ami: str
    ssh_key: Path
    key_pair: str
    ssh_group: str
    report_dir: Path
    tester_zuliprc: Path
    tester2_zuliprc: Path

    # the rig's own files, under `dir`
    @property
    def keys(self) -> Path:
        return self.dir / "keys"

    @property
    def secrets(self) -> Path:
        return self.dir / "secrets"

    @property
    def etc(self) -> Path:
        return self.dir / "etc"

    @property
    def repo(self) -> Path:
        """The release repository: a clone of this checkout that holds the test tags only."""
        return self.dir / "repo"

    @property
    def host_key(self) -> Path:
        """The server's ed25519 host key, pinned from its first boot's console."""
        return self.dir / "server-host-key"

    @property
    def known_hosts(self) -> Path:
        return self.dir / "known_hosts"

    @property
    def ssh_config(self) -> Path:
        return self.dir / "ssh_config"

    @property
    def ssh_wrapper(self) -> Path:
        """`ssh -F ssh_config`, for deploy.sh (`ZAB_DEPLOY_SSH`)."""
        return self.dir / "ssh"

    @property
    def github_token(self) -> Path:
        return self.dir / "github-token"

    @property
    def provisioner_zuliprc(self) -> Path:
        return self.dir / "provisioner.zuliprc"

    @property
    def tester3_zuliprc(self) -> Path:
        return self.dir / "tester3.zuliprc"

    def missing(self) -> list[str]:
        """The inputs that are not there (paths only)."""
        files = {
            "ZAB_ADMIN_E2E_SSH_KEY": self.ssh_key,
            "the key's public half": self.ssh_key.with_name(self.ssh_key.name + ".pub"),
            "ZAB_E2E_TESTER_ZULIPRC": self.tester_zuliprc,
            "ZAB_E2E_TESTER2_ZULIPRC": self.tester2_zuliprc,
        }
        return [f"{what}: {path}" for what, path in files.items() if not path.is_file()]


def load(env: Mapping[str, str] | None = None) -> AdminRig:
    """The rig from the environment (`os.environ` by default); ValueError on a bad value."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home()).absolute()

    def path(var: str, default: Path) -> Path:
        value = env.get(var)
        return Path(value).expanduser().absolute() if value else default

    def text(var: str, default: str) -> str:
        return (env.get(var) or default).strip()

    config = home / ".config" / "zab"
    rig = AdminRig(
        dir=path("ZAB_ADMIN_E2E_DIR", config / "admin-e2e"),
        region=text("ZAB_ADMIN_E2E_REGION", "us-east-1"),
        ami=text("ZAB_ADMIN_E2E_AMI", ""),
        ssh_key=path("ZAB_ADMIN_E2E_SSH_KEY", home / ".ssh" / "id_ed25519"),
        key_pair=text("ZAB_ADMIN_E2E_KEY_PAIR", "zab-test"),
        ssh_group=text("ZAB_ADMIN_E2E_SSH_GROUP", "zab-test-ssh"),
        report_dir=path("ZAB_ADMIN_E2E_REPORT_DIR", home / ".local" / "state" / "zab-admin-e2e"),
        tester_zuliprc=path("ZAB_E2E_TESTER_ZULIPRC", config / "tester.zuliprc"),
        tester2_zuliprc=path("ZAB_E2E_TESTER2_ZULIPRC", config / "tester2.zuliprc"),
    )
    if not _REGION_RE.match(rig.region):
        raise ValueError(f"ZAB_ADMIN_E2E_REGION: invalid region {rig.region!r}")
    if not _AMI_RE.match(rig.ami):
        raise ValueError(f"ZAB_ADMIN_E2E_AMI: invalid image ID {rig.ami!r}")
    for var, name in (("KEY_PAIR", rig.key_pair), ("SSH_GROUP", rig.ssh_group)):
        if not _NAME_RE.match(name):
            raise ValueError(f"ZAB_ADMIN_E2E_{var}: invalid name {name!r}")
    return rig
