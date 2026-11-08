#!/usr/bin/env python3
"""`new_workshop.py`: a new workshop, made for `!new-workshop` (docs/ADMIN.md §2.9; #418).

The admin server's global bot runs it, as `zab-admin`, with every value from admin.toml and the
approved command (`zab.admin.newworkshop`), never typed text:

    new_workshop.py --plan P-42 --name mit-2027 --region us-east-2 --zuliprc PROV \\
      --tag workshop=mit-2027 --scope zab-scope=mathagents [--scope zab-role=test] \\
      --instance-type m7a.2xlarge --github-org MathAgents --refserve-url https://… \\
      --admin-key /etc/zab-admin/break-glass/zab-deployer.pub --organizer 105 --admin 101 \\
      --ws-root /var/lib/zab-admin/ws --secrets /etc/zab-admin/secrets \\
      --public-key /var/lib/zab-admin/.ssh/id_ed25519.pub \\
      --request-dir /var/lib/zab-admin-config/requests \\
      --result /var/lib/zab-admin-config/result.json \\
      [--channel Harvard%20Halloween --bound-channel 42 …] [--dry-run]

**`--tag` and `--scope`.** `--tag` is the workshop's own tag, `workshop=<name>`, alone;
`--scope` is the fleet-wide scope, `zab-scope=<scope>` and `[global]`'s extra tags, which finds
the region's image and its group, named `GROUP`. Since #425 the loader gives every workshop the
scope `workshop=<name>` plus `[global]`'s, and refuses a `tag` in an entry: the entry names
none, and a `--tag` beyond the workshop's is refused. The DryRun launch and the key pair carry
both, as every launch of the workshop then does: the tags authorized are the tags launched
(Codex round 2 on #423).

**`--dry-run`, the plan,** changes nothing. It prints the new entry for admin.toml, with the
image and group of the region's setup (`region_setup`); the organizers' and the global admins'
names next to their IDs; what the apply will make; and each prerequisite. A missing one is named
and the run exits 1, so the plan is refused, never offered. What it can't check is said too
(`unchecked:`): the provisioner, a Moderator, sees no private channel it isn't in and no archived
one, so a channel name taken there shows only at the apply, which then fails at that channel.

**`--channel`, the participants' channel** (#627), its name percent-encoded (UTF-8), which the
bot writes so from `!new-workshop … channel="<name>"`. The entry records its ID (`channel`), so
that it may be renamed later, and `!roster sync` reads the roster from its subscribers:
- **adopted** when the provisioner is subscribed to a channel of that name (`GET
  /users/me/subscriptions`): its ID is recorded, and nothing of it changes. A public one is
  adopted with a warning: anyone in the organization can join it, and the next sync would offer
  them a row;
- **refused** when a channel of that name is visible to the provisioner, a public one, but it
  isn't subscribed: subscribe it first; and when the provisioner's channel of that name is one
  admin.toml binds already (`--bound-channel`: a bot's command channel, an alerts channel,
  another workshop's participants' channel), or is named like a workshop's `#…-organizers` or
  `#…-fleet-alerts`, which the loader would refuse only at the apply's end;
- **made** otherwise, private with shared history, with the organizers and the provisioner
  subscribed, the organizers its administrators. They and the global admins may add
  subscribers; so may any subscriber when the organization lets members subscribe others
  (DECISIONS, 2026-10-06), so `!roster sync`'s plan, which lists every addition, is the check.
  The provisioner sees no private channel it isn't in, so the plan says so
  (`unchecked:`): a private channel of that name it can't see fails the apply at that channel,
  before anything else in Zulip is made, and the answer is to subscribe the provisioner to it.

**The newest image.** Images carry dated names; the newest by `CreationDate` is the region's. Two
made at the same time are refused, both named, never a silent pick: someone untags one.

**The apply** checks everything again, then:
1. `ws/<name>/` and its `out/` (0700), with an empty roster (its header alone, 0600), first: the
   directory keeps `new-workshop.json`, the channels this run made and for whom: a rerun reuses
   them only for the same organizers, global admins and provisioner;
2. with the provisioner's key (`zulip_provision.ZulipAdmin`): first the participants'
   channel, when `--channel` names one to make (above; a rerun adopts it); then the generic bot
   `Workshop admin · <name>`, reused when the provisioner owns one of that name (a rerun); the
   private channel `#<name>-organizers`: the organizers, the global admins and the bot, never
   the provisioner unless it is a global admin, as on the test server (V11); the private channel
   `#<name>-fleet-alerts`: the organizers, the global admins and the provisioner, since the waker
   posts a failed wake there with the participant's own bot, which may post in a private channel
   only if it or its owner, the provisioner, is subscribed (§2.9; the kept fleet's cutover
   decided the same, ADMIN §8.4 at v0.2.15, O1; the risk is a spoofed alert, in a channel that
   takes no commands). The global admins administer both, and only they may add subscribers;
3. the key pair `zab-admin-<name>` in the region, from the server's fleet key, tagged with the
   fleet's scope (`import_key_pair.py`: present with this key is fine);
4. the entry, as JSON, and the bot's zuliprc, to the root helper `zab-admin-config`
   (`request.json`, 0600, renamed into place), then the helper's outcome for this plan, at most
   `WAIT` seconds. With none, the request is withdrawn by a rename, as the helper claims one, so
   exactly one of them has it: "withdrawn", nothing added; or the helper has it, and its outcome
   may still come (`LATE`). The bot restarts itself after the plan's result, which loads the
   new entry.

The new bot's key goes into the request alone: it is never printed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import load_zuliprc
from aws_common import (
    ROSTER_COLUMNS,
    Ec2,
    error_line,
    has_tags,
    parse_public_key,
    parse_tags,
    scope_text,
    tag_list,
)
from import_key_pair import import_key_pair
from upgrade_server import rename_noreplace
from zulip_provision import (
    ZulipAdmin,
    ZulipAdminError,
    _check_bot,
    _resolve_bot_id,
    index_bots,
)

WAIT = 60.0  # seconds for the config helper's outcome
LATE = 10.0  # more seconds, once the helper has taken the request
GROUP = "zab-fleet"  # the region's fleet security group, by name (BOOTSTRAP §6)
STATE = "new-workshop.json"  # in ws/<name>/: the channels a run made, and for whom
NEW = "<new>"  # what only the apply knows, in the dry run's entry
#: The suffixes of a workshop's own channels (`Workshop.channels`): never its participants'.
KINDS = ("-organizers", "-fleet-alerts")
#: What to do about a participants' channel the provisioner isn't in (#627).
SUBSCRIBE = (
    "subscribe {who} to it first (its channel settings, Subscribers), then request again, if it "
    "is the participants' channel you meant: never a bot's command channel, such as #zab-admins, "
    "which {who} must never join"
)


# ------------------------------------------------------------------------------ the region


@dataclass
class Setup:
    """The region's setup: the image and group the new fleet launches from, what was found,
    and each prerequisite that is missing, as lines."""

    image: dict[str, Any] | None = None
    group: dict[str, Any] | None = None
    found: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)  # what the plan can't check, said
    warnings: list[str] = field(default_factory=list)  # what holds, but needs a person's look


def region_setup(
    ec2: Any, region: str, scope: dict[str, str], tags: dict[str, str], instance_type: str
) -> Setup:
    """Whether `region` is set up for a new workshop: the one place the prerequisites model
    lives (§2.9). Drew's model (Oct 16): the account owner sets each region up once, with
    exactly one security group named `GROUP` and an image carrying the fleet's `scope`; the
    newest image when several do. The name sets the fleet's group apart from other scoped ones,
    such as the test rig's per-workshop groups. A DryRun launch of `instance_type` from them,
    tagged `tags`, shows that the role's policy covers the region and the launch (V12)."""
    setup, text = Setup(), scope_text(scope)
    groups = ec2.find_security_groups(scope, GROUP)
    if len(groups) == 1:
        setup.group = groups[0]
        setup.found.append(f"security group {groups[0]['GroupId']} ({GROUP}), tagged {text}")
    else:
        count = f"{len(groups)} security groups" if groups else "no security group"
        setup.problems.append(
            f"{count} named {GROUP} and tagged {text} in {region}: exactly one is needed"
        )
    images = sorted(ec2.find_images(scope), key=lambda image: image.get("CreationDate") or "")
    newest = [i for i in images if i.get("CreationDate") == images[-1].get("CreationDate")]
    if len(newest) > 1:  # never a silent pick between two (review of #423): a person decides
        named = ", ".join(f"{i['ImageId']} ({i.get('Name') or 'unnamed'})" for i in newest)
        setup.problems.append(
            f"{len(newest)} images tagged {text} in {region} are the newest, made "
            f"{images[-1].get('CreationDate') or '?'}: {named}: untag all but one"
        )
    elif images:
        setup.image = images[-1]
        more = f", the newest of {len(images)}" if len(images) > 1 else ""
        setup.found.append(
            f"image {setup.image['ImageId']} ({setup.image.get('Name') or 'unnamed'}, made "
            f"{setup.image.get('CreationDate') or '?'}){more}, tagged {text}"
        )
    else:
        setup.problems.append(f"no available image tagged {text} in {region}")
    if setup.image and setup.group:
        specs = [{"ResourceType": kind, "Tags": tag_list(tags)} for kind in ("instance", "volume")]
        code = ec2.dry_run(
            "run_instances", ImageId=setup.image["ImageId"], InstanceType=instance_type,
            SecurityGroupIds=[setup.group["GroupId"]], TagSpecifications=specs, MinCount=1,
            MaxCount=1,
        )  # fmt: skip
        _dry(setup, code, f"the role may launch {instance_type} from them in {region}")
    return setup


def key_pair_setup(ec2: Any, setup: Setup, name: str, key: str, tags: dict[str, str]) -> None:
    """The key pair `name`: absent, and the role may import it so tagged (DryRun); or present
    with this key and these tags, from an earlier run."""
    found = ec2.key_pair(name)
    if found is None:
        specs = [{"ResourceType": "key-pair", "Tags": tag_list(tags)}]
        code = ec2.dry_run(
            "import_key_pair", KeyName=name, PublicKeyMaterial=key.encode(), TagSpecifications=specs
        )
        _dry(setup, code, f"the role may import the key pair {name}")
    elif " ".join((found.get("PublicKey") or "").split()[:2]) != key or not has_tags(found, tags):
        setup.problems.append(f"the key pair {name} exists with another key or other tags")
    else:
        setup.found.append(f"the key pair {name}, from an earlier run, with this key")


def _dry(setup: Setup, code: str, what: str) -> None:
    if code == "DryRunOperation":
        setup.found.append(f"{what} (DryRun)")
    elif code == "UnauthorizedOperation":
        setup.problems.append(f"{what}: refused (DryRun): the role's policy doesn't cover it")
    else:
        setup.problems.append(f"{what}: the DryRun answered {code}")


# --------------------------------------------------------------------------- the workshop


@dataclass
class Workshop:
    """What the run makes, from its arguments."""

    plan: str
    name: str
    region: str
    tags: dict[str, str]  # its own: `workshop=<name>` alone (the loader's, never the entry's)
    scope: dict[str, str]  # the fleet's: the region's image and group carry it
    instance_types: list[str]
    github_org: str
    refserve_url: str
    admin_keys: list[str]
    organizers: list[int]
    admins: list[int]
    ws_root: str
    secrets: str
    channel: str | None = None  # the participants' channel's name (#627), or none
    bound: frozenset[int] = frozenset()  # the channels admin.toml binds already (#627)

    @property
    def home(self) -> pathlib.Path:
        return pathlib.Path(self.ws_root) / self.name

    @property
    def key_name(self) -> str:
        return f"zab-admin-{self.name}"

    @property
    def bot(self) -> str:
        return f"Workshop admin · {self.name}"

    @property
    def channels(self) -> dict[str, str]:
        return {"organizers": f"{self.name}-organizers", "alerts": f"{self.name}-fleet-alerts"}

    def launch_tags(self) -> dict[str, str]:
        return {**self.scope, **self.tags}

    def entry(self, setup: Setup, bot: dict[str, Any] | None, ids: dict[str, int]) -> dict:
        """The `[[workshop]]` entry, as `zab.admin.confighelper` takes it (`NEW` for what
        only the apply knows); `channel` only when `--channel` names one."""
        participants = {"channel": ids.get("participants", NEW)} if self.channel else {}
        return {
            "name": self.name,  # no `tag`: the loader's scope is workshop=<name> and [global]'s
            "github_org": self.github_org,
            "refserve_url": self.refserve_url,
            "roster": str(self.home / "roster.csv"),
            "out_dir": str(self.home / "out"),
            "organizers": self.organizers,
            "alerts_channel": ids.get("alerts", NEW),
            **participants,
            "bot": {
                "zuliprc": f"{self.secrets}/bots/{self.name}.zuliprc",
                "user_id": bot["user_id"] if bot else NEW,
                "email": bot["email"] if bot else NEW,
                "channels": [ids.get("organizers", NEW)],
            },
            "aws": {
                "region": self.region,
                "ami": setup.image["ImageId"] if setup.image else NEW,
                "security_group": setup.group["GroupId"] if setup.group else NEW,
                "instance_types": self.instance_types,
                "key_name": self.key_name,
                "admin_keys": self.admin_keys,
            },
        }


def toml(entry: dict[str, Any]) -> list[str]:
    """The entry as admin.toml will hold it (the helper renders it the same way)."""
    lines = ["[[workshop]]"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in entry.items() if not isinstance(v, dict)]
    for table in ("bot", "aws"):
        lines += [f"[workshop.{table}]"]
        lines += [f"{k} = {json.dumps(v)}" for k, v in entry[table].items()]
    return lines


@dataclass
class Survey:
    """What the plan shows, and the apply checks again."""

    setup: Setup
    me: dict[str, Any]
    names: dict[int, str]
    bot: dict[str, Any] | None
    made: dict[str, int]  # the channels an earlier run made (`STATE`)
    #: the participants' channel the provisioner is in, to adopt (#627); None: made, or none
    adopted: dict[str, Any] | None = None

    def ids(self) -> dict[str, int]:
        """The channels' IDs known before the apply makes any."""
        adopted = {"participants": int(self.adopted["stream_id"])} if self.adopted else {}
        return {**self.made, **adopted}


def survey(ws: Workshop, admin: ZulipAdmin, ec2: Any, key: str) -> Survey:
    setup = region_setup(ec2, ws.region, ws.scope, ws.launch_tags(), ws.instance_types[0])
    key_pair_setup(ec2, setup, ws.key_name, key, ws.launch_tags())
    problems = setup.problems
    me = admin.me()
    if me.get("user_id") in ws.organizers:  # it owns every participant bot (ADMIN §1.5)
        problems.append("the provisioner is named an organizer: it never joins that channel")
    names: dict[int, str] = {}
    for uid in dict.fromkeys([*ws.organizers, *ws.admins]):
        try:
            user = admin.user(uid)
        except ZulipAdminError:
            problems.append(f"user {uid} is not a user of this organization")
            continue
        names[uid] = " ".join(str(user.get("full_name") or f"user {uid}").split())
        if user.get("is_bot") is not False or user.get("is_active") is False:
            problems.append(f"user {uid} ({names[uid]}) is a bot or deactivated")
    users = admin.users()
    bot = None
    own = index_bots(admin, users).get(ws.bot.casefold())
    if own is not None:
        bot = own if own.get("user_id") is not None else _resolve_bot_id(admin, ws.bot, own)
        _check_bot(ws.bot, bot)
    elif any(str(u.get("full_name", "")).casefold() == ws.bot.casefold() for u in users):
        problems.append(f"a user named {ws.bot} exists, not the provisioner's bot")
    made, members = _state(ws)
    if made and members != _members(ws, me):  # Codex on #423: never others' channels reused
        problems.append(
            f"an earlier run made {' and '.join('#' + ws.channels[k] for k in made)} for other "
            "organizers, global admins or provisioner: choose another workshop name, or a "
            f"global admin renames those channels and a deployer removes {ws.home / STATE} "
            "first (an archived channel keeps its name taken)"
        )
    visible = {str(s.get("name", "")).casefold(): s for s in admin.streams()}
    for kind, name in ws.channels.items():
        stream = visible.get(name.casefold())
        if stream is not None and int(stream["stream_id"]) != made.get(kind):
            problems.append(f"a channel #{name} exists already: choose another name")
    # The provisioner, a Moderator, lists only the channels it can see: never a private one it
    # isn't in, nor an archived one, whose names stay taken (review of #423). No cheap call
    # tells such a name from a free one, so the plan says what it couldn't check.
    setup.unchecked.append(
        f"#{ws.channels['organizers']} and #{ws.channels['alerts']} are free only as far as the "
        "provisioner can see: a private channel it isn't in, or an archived one, of either name "
        "fails the apply at that channel, after the bot is made"
    )
    problems += home_problems(ws)
    adopted = participants_channel(ws, admin, visible, setup, me) if ws.channel else None
    return Survey(setup, me, names, bot, made, adopted)


def participants_channel(
    ws: Workshop,
    admin: ZulipAdmin,
    visible: dict[str, dict[str, Any]],
    setup: Setup,
    me: dict[str, Any],
) -> dict[str, Any] | None:
    """The participants' channel `--channel` names (#627, module doc): the provisioner's own
    channel of that name, to adopt, or None, to make; a problem when it can be neither."""
    assert ws.channel is not None
    name, key = ws.channel, ws.channel.casefold()
    who = str(me.get("full_name") or "the provisioner")
    if key in {other.casefold() for other in ws.channels.values()}:
        setup.problems.append(f"channel= names #{name}, one of the channels this run makes")
        return None
    if key.endswith(KINDS):  # whoever sees it: never advise subscribing to one (6397329425, B)
        setup.problems.append(
            f"#{name} is named like a workshop's organizers or alerts channel: name another "
            "channel for the participants"
        )
        return None
    mine = {str(s.get("name", "")).casefold(): s for s in admin.subscriptions()}
    found = mine.get(key) or visible.get(key)
    if found is not None and int(found["stream_id"]) in ws.bound:  # seen, in it or not
        setup.problems.append(
            f"#{found.get('name') or name} ({found['stream_id']}) is a channel admin.toml binds "
            "already: name another channel for the participants"
        )
        return None
    found = mine.get(key)
    if found is not None:
        shown = f"#{found.get('name') or name} ({found['stream_id']})"
        setup.found.append(f"{shown}, the participants' channel: {who} is in it, so it is adopted")
        if not found.get("invite_only"):
            setup.warnings.append(
                f"{shown} is public: anyone in the organization can join it, and !roster sync "
                "would then offer them a row; make it private"
            )
        return found
    if key in visible:
        setup.problems.append(
            f"#{name} exists, and {who} is not subscribed to it: {SUBSCRIBE.format(who=who)}"
        )
        return None
    setup.unchecked.append(
        f"#{name} is made only if no private channel has that name: {who} sees none it isn't "
        f"in, and one fails the apply at it, before anything else in Zulip is made: then "
        f"{SUBSCRIBE.format(who=who)}"
    )
    return None


def home_problems(ws: Workshop) -> list[str]:
    """`ws/<name>/` may hold only what an earlier run made: a workshop of that name before would
    hand the new one its roster and its participants' keys (§2.1: names, like slugs, stay)."""
    home = ws.home
    if not home.exists():
        return []
    extra = sorted({p.name for p in home.iterdir()} - {"roster.csv", "out", STATE})
    roster = home / "roster.csv"
    rows = roster.read_text(encoding="utf-8").splitlines()[1:] if roster.is_file() else []
    out = home / "out"
    if extra or any(r.strip() for r in rows) or (out.is_dir() and any(out.iterdir())):
        return [f"{home} holds an earlier workshop's files: choose another name"]
    return []


def _members(ws: Workshop, me: dict[str, Any]) -> dict[str, Any]:
    """Whom the channels are made for: a rerun reuses them only for the same people."""
    return {"organizers": sorted(ws.organizers), "admins": sorted(ws.admins),
            "provisioner": me.get("user_id")}  # fmt: skip


def _state(ws: Workshop) -> tuple[dict[str, int], dict[str, Any]]:
    """The channels an earlier run made (`STATE`), and whom it made them for."""
    try:
        found = json.loads((ws.home / STATE).read_text(encoding="utf-8"))
        channels, members = found["channels"], found["members"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}
    made = {k: v for k, v in channels.items() if k in ws.channels and type(v) is int}
    return made, members


def _save_state(ws: Workshop, made: dict[str, int], members: dict[str, Any]) -> None:
    path = ws.home / STATE
    fd = os.open(f"{path}.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"channels": made, "members": members}, f)
    os.replace(f"{path}.new", path)


def show(ws: Workshop, s: Survey, *, dry_run: bool) -> None:
    """The plan's text (the dry run) or the apply's first lines: everything but the key."""
    print(f"New workshop {ws.name}, in {ws.region}, scope {scope_text(ws.tags)}")
    print("\nIts entry for admin.toml" + (f" ({NEW}: made by the apply):" if dry_run else ":"))
    print("\n".join(toml(ws.entry(s.setup, s.bot, s.ids()))))
    print("\nOrganizers (this workshop's bot only):")
    print("\n".join(f"  {uid}  {s.names.get(uid, '?')}" for uid in ws.organizers))
    print("Global admins (every bot):")
    print("\n".join(f"  {uid}  {s.names.get(uid, '?')}" for uid in ws.admins))
    me = f"{s.me.get('full_name')} ({s.me.get('user_id')})"
    print("\nTo make:")
    if ws.channel and s.adopted:
        shown = s.adopted.get("name") or ws.channel
        print(f"  #{shown}, the participants' channel: adopted ({s.adopted['stream_id']})")
    elif ws.channel:
        print(f"  #{ws.channel} (private), the participants' channel: the organizers, {me}")
    print(f"  the bot {ws.bot}, owned by the provisioner {me}" + " (exists)" * bool(s.bot))
    print(f"  #{ws.channels['organizers']} (private): the organizers, the global admins, the bot")
    print(f"  #{ws.channels['alerts']} (private): the organizers, the global admins, {me}")
    print(f"  {ws.home}/ and out/, with an empty roster")
    print(f"  the key pair {ws.key_name} in {ws.region}, from the server's fleet key")
    print("  the entry, through the config helper; then the bots restart")
    print("\nPrerequisites:")
    print("\n".join([*(f"  ok: {line}" for line in s.setup.found),
                     *(f"  unchecked: {line}" for line in s.setup.unchecked),
                     *(f"  warning: {line}" for line in s.setup.warnings),
                     *(f"  MISSING: {line}" for line in s.setup.problems)]))  # fmt: skip


# --------------------------------------------------------------------------- the apply


def make_home(ws: Workshop) -> None:
    """`ws/<name>/` and its `out/`, 0700, and the roster's header alone, 0600 (§1.3)."""
    for path in (ws.home, ws.home / "out"):
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    roster = ws.home / "roster.csv"
    if not roster.exists():
        fd = os.open(roster, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(",".join(ROSTER_COLUMNS) + "\n")


#: Each channel's description, by kind.
DESCRIPTIONS = {
    "organizers": "zab: this workshop's admin bot and its organizers (docs/ORGANIZERS.md)",
    "alerts": "zab: the waker's alerts about this workshop's VMs; it takes no commands",
    "participants": "The workshop's participants",
}


class Unmade(Exception):
    """The participants' channel could not be made, and nothing in Zulip was (#627)."""


def make_channel(
    admin: ZulipAdmin, name: str, people: set[int], admins: list[int], description: str
) -> int:
    """A private channel with shared history and `people` alone; the global admins administer
    it and alone may add subscribers, so its creator, the provisioner, keeps no access unless it
    is one of `people` (V11). Its ID."""
    group = {"direct_members": sorted(admins), "direct_subgroups": []}
    found = admin.request(
        "POST", "channels/create", name=name, subscribers=sorted(people), invite_only=True,
        history_public_to_subscribers=True, announce=False, can_add_subscribers_group=group,
        can_administer_channel_group=group, description=description,
    )  # fmt: skip
    return int(found["id"])


def make_participants(ws: Workshop, admin: ZulipAdmin, s: Survey) -> int:
    """The participants' channel (#627): private, shared history, the organizers and the
    provisioner subscribed; the organizers administer it, and they and the global admins may
    add subscribers, so that an organizer invites into it (as may any subscriber, where the
    organization lets members subscribe others). Its ID; `Unmade` when Zulip refuses,
    most often for a private channel of that name that the provisioner can't see."""
    assert ws.channel is not None
    organizers = {"direct_members": sorted(ws.organizers), "direct_subgroups": []}
    adders = {"direct_members": sorted({*ws.organizers, *ws.admins}), "direct_subgroups": []}
    people = sorted({*ws.organizers, int(s.me["user_id"])})
    try:
        found = admin.request(
            "POST", "channels/create", name=ws.channel, subscribers=people, invite_only=True,
            history_public_to_subscribers=True, announce=False, can_add_subscribers_group=adders,
            can_administer_channel_group=organizers, description=DESCRIPTIONS["participants"],
        )  # fmt: skip
    except ZulipAdminError as exc:
        who = str(s.me.get("full_name") or "the provisioner")
        raise Unmade(
            f"#{ws.channel} could not be made ({exc}): if a private channel of that name exists, "
            f"{SUBSCRIBE.format(who=who)}. Nothing else in Zulip was made"
        ) from None
    return int(found["id"])


def make_zulip(ws: Workshop, admin: ZulipAdmin, s: Survey) -> tuple[dict[str, Any], dict]:
    """The participants' channel when one is to be made, then the bot, then the two channels
    (module doc); each channel's ID is kept as it is made."""
    ids = s.ids()
    if ws.channel and "participants" not in ids:
        ids["participants"] = make_participants(ws, admin, s)
        print(f"made #{ws.channel}, the participants' channel ({ids['participants']})")
    bot = s.bot
    if bot is None:
        made = admin.create_bot(ws.bot, f"{ws.name}-admin")
        user = admin.user(int(made["user_id"]))
        bot = {"user_id": int(made["user_id"]), "api_key": str(made.get("api_key") or ""),
               "email": str(user.get("email") or ""), "full_name": ws.bot}  # fmt: skip
        _check_bot(ws.bot, bot)
        print(f"made the bot {ws.bot}, user {bot['user_id']}")
    provisioner = int(s.me["user_id"])
    people = {
        "organizers": {*ws.organizers, *ws.admins, int(bot["user_id"])},
        "alerts": {*ws.organizers, *ws.admins, provisioner},
    }
    for kind, name in ws.channels.items():
        if kind in ids:
            print(f"#{name}: made by an earlier run ({ids[kind]})")
            continue
        ids[kind] = make_channel(admin, name, people[kind], ws.admins, DESCRIPTIONS[kind])
        _save_state(ws, {k: v for k, v in ids.items() if k in ws.channels}, _members(ws, s.me))
        print(f"made #{name} ({ids[kind]})")
    return bot, ids


def send_request(ws: Workshop, entry: dict, zuliprc: str, folder: str, result: str) -> int:
    """The helper's request (module doc, step 4), then its outcome for this plan: 0 when it
    added the entry."""
    target = pathlib.Path(folder) / "request.json"
    body = {"version": 1, "plan": ws.plan, "entry": entry, "zuliprc": zuliprc}
    earlier = _stamp(result)  # the helper replaces the file: an outcome must be a new one
    tmp = pathlib.Path(folder) / f"request.{os.getpid()}.new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:  # it holds the bot's key: never left behind half-written (review of #423)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            json.dump(body, f, ensure_ascii=True)
        # in one step, never over another request, with one link always (#898's Codex round 1)
        rename_noreplace(tmp, target)
    except FileExistsError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        print(f"{target} is there already: the helper hasn't taken an earlier request")
        return 1
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    outcome = _outcome(ws, target, result, earlier, WAIT)
    if outcome is None:
        return withdraw(ws, target, result, earlier)
    print(f"the config helper: {outcome.get('message')}")
    return 0 if outcome.get("ok") is True else 1


def _outcome(
    ws: Workshop, target: pathlib.Path, result: str, earlier: Any, seconds: float
) -> dict | None:
    """The helper's outcome of this request within `seconds`: a `result.json` written after it
    (Codex on #423: never an earlier one, of this plan or not), naming this plan, or none, which
    the helper writes for a request it couldn't read a plan in. One with none is ours only once
    `target` is gone, taken or removed by the helper (review of #423, round 2): while our
    request is still there, it is some earlier request's."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if _stamp(result) == earlier:
            continue
        try:
            outcome = json.loads(pathlib.Path(result).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(outcome, dict) or outcome.get("plan") not in (ws.plan, None):
            continue
        if outcome.get("plan") == ws.plan:
            return outcome
        if not os.path.lexists(target):
            return {**outcome, "ok": False}
    return None


def withdraw(ws: Workshop, target: pathlib.Path, result: str, earlier: Any) -> int:
    """No outcome in time: the request, which holds the bot's key, is taken back, so that a late
    helper never applies a plan reported failed (review of #423). By a rename, as the helper
    claims one (`zab.admin.confighelper.take`): of the two renames exactly one finds it."""
    withdrawn = target.with_name(f"request.{os.getpid()}.withdrawn")
    try:
        os.rename(target, withdrawn)
    except FileNotFoundError:  # the helper has it: its outcome may still come
        outcome = _outcome(ws, target, result, earlier, LATE)
        if outcome is not None:
            print(f"the config helper, late: {outcome.get('message')}")
            return 0 if outcome.get("ok") is True else 1
        waited = f"{WAIT + LATE:.0f} s"
        print(f"the config helper took the request but said nothing within {waited}: the entry "
              f"may be added: read {result} and journalctl -u zab-admin-config")  # fmt: skip
        return 1
    except OSError as exc:
        print(f"the request could not be withdrawn ({exc.strerror}): it is still pending, with "
              f"the new bot's key, at {target}: a deployer removes it, or the helper applies it "
              "when it next runs (journalctl -u zab-admin-config)")  # fmt: skip
        return 1
    try:
        ours = json.loads(withdrawn.read_text(encoding="ascii")).get("plan") == ws.plan
    except (OSError, ValueError, AttributeError):
        ours = False
    if not ours:  # never: the bot sends one request at a time; put back, never over another
        try:  # one link always, as the helper requires (#898's Codex round 1)
            rename_noreplace(withdrawn, target)
            fate = "put back, pending"
        except OSError as exc:  # never stranded as .withdrawn (review of #423, round 2)
            fate = f"not put back ({exc.strerror}), so dropped with its key: its run gets nothing"
            try:
                os.unlink(withdrawn)
            except OSError as gone:
                fate += f"; {withdrawn} is left ({gone.strerror}): a deployer removes it"
        print(f"{target} was another plan's request: {fate}")
        return 1
    with contextlib.suppress(OSError):
        os.unlink(withdrawn)
    print(f"the config helper said nothing within {WAIT:.0f} s: the request is withdrawn, and "
          "nothing was added: journalctl -u zab-admin-config")  # fmt: skip
    return 1


def _stamp(path: str) -> tuple[int, int] | None:
    """The file's inode and modification time: the helper writes a new file and renames it."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


def apply(ws: Workshop, admin: ZulipAdmin, ec2: Any, key: str, args: argparse.Namespace) -> int:
    s = survey(ws, admin, ec2, key)
    show(ws, s, dry_run=False)
    if s.setup.problems:
        print("\nNothing was made: a prerequisite is missing.")
        return 1
    make_home(ws)
    print(f"\nmade {ws.home}/ and out/, with an empty roster")
    try:
        bot, ids = make_zulip(ws, admin, s)
    except Unmade as exc:
        print(exc)
        return 1
    if import_key_pair(ec2, ws.key_name, key, ws.launch_tags()) != 0:
        return 1
    site = admin.site
    zuliprc = f"[api]\nemail={bot['email']}\nkey={bot['api_key']}\nsite={site}\n"
    entry = ws.entry(s.setup, bot, ids)
    return send_request(ws, entry, zuliprc, args.request_dir, args.result)


def workshop(args: argparse.Namespace) -> Workshop:
    tags = parse_tags(args.tag)
    if tags != {"workshop": args.name}:  # #425: the rest of the scope is [global]'s
        raise ValueError(f"--tag is workshop={args.name} alone; the fleet's scope is --scope")
    return Workshop(
        args.plan, args.name, args.region, tags, parse_tags(args.scope), args.instance_type,
        args.github_org, args.refserve_url, args.admin_key, args.organizer, args.admin,
        args.ws_root, args.secrets, channel_name(args.channel) if args.channel else None,
        frozenset(args.bound_channel),
    )  # fmt: skip


def channel_name(encoded: str) -> str:
    """`--channel`'s name, decoded (#627): 1 to 60 characters, all printable, no outer space;
    the bot's grammar allows fewer (`zab.admin.grammar.channel_name`)."""
    try:
        name = urllib.parse.unquote(encoded, errors="strict")
    except UnicodeDecodeError:
        name = ""
    again = urllib.parse.quote(name, safe="")  # as `zab.admin.newworkshop.channel_arg` writes it
    again = "%2D" + again[1:] if again.startswith("-") else again
    if again != encoded or not 1 <= len(name) <= 60 or name != name.strip():
        raise ValueError("--channel: not a channel's name, percent-encoded")
    if not name.isprintable():
        raise ValueError("--channel: not a channel's name, percent-encoded")
    return name


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="new_workshop.py", description=__doc__.split("\n\n")[0])
    for flag in ("plan", "name", "region", "zuliprc", "github-org", "refserve-url", "ws-root",
                 "secrets", "public-key", "request-dir", "result"):  # fmt: skip
        p.add_argument(f"--{flag}", required=True)
    for flag in ("tag", "scope", "instance-type", "admin-key"):
        p.add_argument(f"--{flag}", action="append", required=True)
    p.add_argument("--organizer", action="append", type=int, required=True)
    p.add_argument("--admin", action="append", type=int, required=True)
    p.add_argument("--channel", help="the participants' channel's name, percent-encoded (#627)")
    p.add_argument("--bound-channel", action="append", type=int, default=[],
                   help="a channel ID admin.toml binds already: never adopted (#627)")  # fmt: skip
    p.add_argument("--dry-run", action="store_true", help="the plan: read and print only")
    args = p.parse_args(argv)
    try:
        ws = workshop(args)
        text = pathlib.Path(args.public_key).read_text(encoding="ascii")
        key = " ".join(parse_public_key(text, "the fleet key").split()[:2])
        admin = ZulipAdmin(*load_zuliprc(args.zuliprc))
        ec2 = Ec2(args.region)
    except (OSError, ValueError, KeyError) as exc:
        print(f"new_workshop.py: {exc}", file=sys.stderr)
        return 2
    try:
        if not args.dry_run:
            return apply(ws, admin, ec2, key, args)
        s = survey(ws, admin, ec2, key)
        show(ws, s, dry_run=True)
        return 1 if s.setup.problems else 0
    except Exception as exc:  # Zulip's, httpx's, botocore's: one line, never a traceback
        print(f"new_workshop.py: {error_line(exc)}", file=sys.stderr)
        return 1
    finally:
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
