"""`!new-workshop`'s action (docs/ADMIN.md §2.9; #418): a server verb, on the `[global]` bot.

`build` maps the parsed command to one step, `workshop/admin/new_workshop.py`, whose dry run is
the plan and whose run makes the workshop. Every value comes from admin.toml, from the command's
checked words, or from the server's fixed paths (`Layout`):

- the name must be new on this server, and `like=` one of its workshops, whose GitHub
  organization, file server, instance types and break-glass keys the new workshop takes. Its
  scope is `workshop=<name>` and `[global]`'s (#425), so the entry names no tag;
- the organizers are user IDs, never an admin bot's. The server needs a global admin: the
  plan is a server plan, which only they approve, and one's approval completes it (review of
  #424; #558). The new workshop then meets the loader's rule for its approvers too;
- the region must be one of `[global] regions`, and the fleet-wide scope (`fleet_scope`) finds
  its image and its security group `zab-fleet`, which the account owner sets up once per region
  (Drew, Oct 16; BOOTSTRAP §6): no per-workshop AWS step;
- `channel="<name>"`, optional, the participants' channel (#627), which the script adopts or
  makes: its name goes to the script percent-encoded (`channel_arg`), the one value outside
  the argument lists' alphabet, since a Zulip name may hold spaces and any script's letters.
  It is one of two free-text values: the other, a workshop's `gifts_step` (#709), goes to
  `zulip_provision.py` base64url-encoded, inside the alphabet (`actions._Job`).

The plan is hashed on its dry run (§2.4), with admin.toml among its inputs: an entry added or
changed between plan and apply makes it STALE.
"""

from __future__ import annotations

import re
import urllib.parse

from zab.admin.actions import (
    DRY_RUN,
    Action,
    ActionError,
    Argv,
    Layout,
    Step,
    _cat,
    _value,
)
from zab.admin.config import AdminConfig
from zab.admin.grammar import Command, channel_name

SCRIPT = "admin/new_workshop.py"
#: The plan's note, with a participants' channel (#627).
PARTICIPANTS = ", and its participants' channel, adopted or made (the dry run says which)"
#: A channel's name, percent-encoded: never a leading `-`, which `channel_arg` encodes too.
_ENCODED = re.compile(r"[A-Za-z0-9_.~%][A-Za-z0-9_.~%-]{0,1023}", re.ASCII)


def channel_arg(name: object) -> str:
    """`--channel`'s value (#627): the grammar's channel name (`grammar.channel_name`), its
    UTF-8 percent-encoded, with a leading `-` encoded too, so that it is never read as a
    flag. `new_workshop.channel_name` decodes it."""
    if not isinstance(name, str) or channel_name(name) is None:
        raise ActionError("channel=: not a channel's name: refused")
    encoded = urllib.parse.quote(name, safe="")
    encoded = "%2D" + encoded[1:] if encoded.startswith("-") else encoded
    if not _ENCODED.fullmatch(encoded):
        raise ActionError("channel=: not a channel's name: refused")
    return encoded


def bound_channels(config: AdminConfig) -> frozenset[int]:
    """Every channel admin.toml binds (#627): the bots' command channels, the alerts channels
    and the participants' channels. A new workshop's `channel` may be none of them (the
    loader's rule), so `new_workshop.py` refuses to adopt one at the plan, not at the apply."""
    bots = [ws.bot for ws in config.workshops] + ([config.global_bot] if config.global_bot else [])
    found = {uid for bot in bots for uid in bot.channels}
    found |= {ws.alerts_channel for ws in config.workshops if ws.alerts_channel is not None}
    return frozenset(found | {ws.channel for ws in config.workshops if ws.channel is not None})


def fleet_scope(config: AdminConfig) -> tuple[tuple[str, str], ...]:
    """The fleet-wide scope tags: `zab-scope=<[global] scope>` and `[global]`'s extra tags
    (#425). Every region's image and security group carry them (Drew, Oct 16), and every
    workshop's scope is `workshop=<name>` and these."""
    return config.scope_tags


def run(layout: Layout, script: str, *parts: str | Argv) -> Argv:
    """As `actions._Job.run`: the release's Python, `-E -s -B -u`, a script of the release."""
    path = _value(f"{layout.release}/workshop/{script}")
    return _cat(_value(layout.python), "-E", "-s", "-B", "-u", path, *parts)


def build(
    command: Command,
    config: AdminConfig,
    *,
    plan: str,
    bot: int,
    layout: Layout,
    workshops: str,
    secrets: str,
    config_path: str,
) -> Action:
    """The action of `!new-workshop` for plan `plan` on the `[global]` bot `bot`. `workshops`
    and `secrets` are the roots of §1.3 the new entry's files go under; `config_path` is
    admin.toml, an input of the hash. Raises `ActionError`, one line, on anything else."""
    name, like_name, ids = command.ws_name, command.like, command.organizers
    if command.verb != "new-workshop" or not (name and like_name and command.region and ids):
        raise ActionError("not a whole !new-workshop command: refused")
    if config.workshop(name) is not None:
        raise ActionError(f"{name} is a workshop of this server already: choose a new name")
    like = config.workshop(like_name)
    if like is None:
        raise ActionError(f"like={like_name}: no workshop of that name on this server")
    bots = {ws.bot.user_id for ws in config.workshops}
    bots |= {config.global_bot.user_id} if config.global_bot else set()
    if bots & set(ids):
        raise ActionError("an organizer is an admin bot: bots never approve")
    if not config.admins:  # the plan's own (review of #424): a global admin approves it alone
        raise ActionError(
            "this server has no global admin, and only one approves a server plan: nobody "
            "could approve it; a deployer adds a global admin first"
        )
    if command.region not in config.regions:
        raise ActionError(
            f"region={command.region} is not set up for the fleet: [global] regions are "
            f"{', '.join(config.regions)} (BOOTSTRAP §6 sets a region up; a deployer adds it)"
        )
    scope = fleet_scope(config)
    # the loader adds [global]'s scope to every workshop's (#425): the entry names no tag
    tags = [("workshop", name)]
    aws = like.aws
    pairs = [
        ("plan", plan), ("name", name), ("region", command.region),
        ("zuliprc", layout.provisioner_zuliprc),
        *(("tag", f"{k}={v}") for k, v in tags),
        *(("scope", f"{k}={v}") for k, v in scope),
        *(("instance-type", t) for t in aws.instance_types),
        ("github-org", like.github_org), ("refserve-url", like.refserve_url),
        *(("admin-key", key) for key in aws.admin_keys),
        *(("organizer", str(uid)) for uid in ids),
        *(("admin", str(uid)) for uid in sorted(config.admins)),
        ("ws-root", workshops), ("secrets", secrets), ("public-key", layout.fleet_key),
        ("request-dir", layout.config_request), ("result", layout.config_result),
    ]  # fmt: skip
    # each value checked: the safe alphabet, never a leading `-` (actions._value)
    argv = run(layout, SCRIPT, *((f"--{flag}", _value(value)) for flag, value in pairs))
    if command.channel is not None:  # the participants' channel (#627), never one bound already
        bound = sorted(bound_channels(config))
        more = (w for uid in bound for w in ("--bound-channel", str(uid)))
        argv = (*argv, "--channel", channel_arg(command.channel), *more)
    return Action(
        verb="new-workshop",
        workshop="",
        bot=bot,
        args=command.tokens(),
        steps=(Step(argv, (*argv, "--dry-run")),),
        hashed=DRY_RUN,
        targets=(name,),
        inputs=(config_path,),
        notes=(
            f"A new workshop, **{name}**, in {command.region}, with {like_name}'s settings: "
            f"its bot, its two channels, its directory, its key pair and its admin.toml entry"
            f"{PARTICIPANTS * (command.channel is not None)}. The bots restart once the result "
            "is posted.",
        ),
    )
