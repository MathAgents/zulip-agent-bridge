"""`admin.toml`, the admin server's configuration (docs/ADMIN.md §2.1, §1.3).

One server runs several workshops at once, each through its own admin bot (Drew, Oct 15):
`[[workshop]]` entries, each with its roster, output directory, organizers, region and
`[workshop.bot]`. That bot is bound to its workshop alone, so organizers never type a workshop
or a region. `[global]` holds the fleet's scope, the same for every workshop (Drew, Oct 16;
§5): the tag `zab-scope=<scope>`, the regions and instance types the role allows, and any extra
scope tags; a workshop's scope is `workshop=<name>` and those tags. The optional global admin
bot, in the same table, serves the server-level verbs. Two allowlists, which mean different
things:

- `admins`, at the top: the global admins, who may act through any bot;
- `organizers`, in a `[[workshop]]`: scoped to that workshop's bot and to no other.

Every value a script needs comes from here, and the bot passes every flag explicitly (§2.1), so
the loader is strict: an unknown key, a wrong type or a value outside its rule is refused, never
replaced by a default. A value that ends up in an argument list can't begin with `-`. Each kind
of file lies under its own §1.3 directory (`Roots`), and no file serves two roles. The file
holds no secrets (§1.3), so errors quote values.

The loader reads only the text: the owners and modes of admin.toml, the zuliprcs and the admin
keys are checked by the bots at startup and by `selfcheck` (PR 9), as sshd's StrictModes does.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PATH = Path("/etc/zab-admin/admin.toml")
#: `release`: which tags the server deploys and rolls out (§1.6). Final tags in production; the
#: test server also takes release candidates (§7). No free regex: a typo can't widen the gate.
RELEASE_PATTERNS = {"final": r"^v\d+\.\d+\.\d+$", "rc": r"^v\d+\.\d+\.\d+(?:rc\d+)?$"}
#: The fleet-wide scope tag's key; its value is `[global] scope` (§5; Drew, Oct 16).
SCOPE_KEY = "zab-scope"
#: Per-participant tag keys (`aws_common.RESERVED_TAG_KEYS`), the server's own (§1.1), and the
#: two every scope has: never an extra scope tag.
_RESERVED_TAG_KEYS = frozenset({"Name", "participant", "zab-admin", "workshop", SCOPE_KEY})
#: The global admin bot's keys in `[global]`, beside the scope: all but `email`, or none.
_GLOBAL_BOT_KEYS = frozenset({"zuliprc", "user_id", "channels", "email"})
#: Why a v0.2.4 admin.toml, whose scope was per workshop, is refused (CHANGELOG 0.2.5 migrates it).
MIGRATE = (
    "since v0.2.5 the role is scoped by the fleet-wide tag zab-scope: [global] needs scope, "
    "regions and instance_types, and a workshop takes no tag (docs/ADMIN.md §5; to migrate, "
    "CHANGELOG.md 0.2.5's upgrade note)"
)

_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
_ORG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", re.ASCII)
_TAG_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,127}", re.ASCII)
_TAG_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,255}", re.ASCII)
_HTTPS = re.compile(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?", re.ASCII)
_SEGMENT = r"(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)"
_PATH = re.compile(_SEGMENT + "++", re.ASCII)  # absolute; no . or ..
# not possessive: the last segment must give `.pub` back
_PUBLIC_KEY_FILE = re.compile(_SEGMENT + r"+\.pub", re.ASCII)
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)++-[0-9]{1,2}", re.ASCII)
_AMI = re.compile(r"ami-[0-9a-f]{8,17}", re.ASCII)
_SECURITY_GROUP = re.compile(r"sg-[0-9a-f]{8,17}", re.ASCII)
_INSTANCE_TYPE = re.compile(r"[a-z][a-z0-9-]*\.[a-z0-9]+", re.ASCII)
_KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9.-]{1,253}", re.ASCII)
#: `release_repo` (#569): where `!upgrade-server`'s helper fetches the signed tags, with its
#: deploy key. GitHub over SSH only, since the helper pins GitHub's host keys.
RELEASE_REPO = re.compile(
    r"git@github\.com:[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    r"/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}\.git",
    re.ASCII,
)
#: `gifts_step` (#709): one line, no control characters, neither starting with whitespace or `-`
#: nor ending with whitespace, at most `GIFTS_STEP_BYTES` in UTF-8; zulip_provision.py's rule.
_NOT_TEXT = r"\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff"
_GIFTS_STEP = re.compile(rf"[^\s{_NOT_TEXT}-](?:[^{_NOT_TEXT}]*[^\s{_NOT_TEXT}])?")
GIFTS_STEP_BYTES = 500
#: `mention_after_s` (#711): the bridge's default, and a day at most, which bounds a typo
MENTION_AFTER_S, MENTION_AFTER_MAX = 120, 86400
_MISSING: Any = object()
#: `spare_idle_stop` (#715): a spare's idle stop, in minutes, when the entry names none; and its
#: bounds, idle stop's own (`idle_stop.MIN_MINUTES`, and `!idle-stop`'s most).
SPARE_IDLE_STOP, IDLE_STOP_MINUTES = 10, (10, 240)


class AdminConfigError(ValueError):
    """admin.toml is missing, malformed, or holds a value outside its rule."""


@dataclass(frozen=True, slots=True)
class Roots:
    """The directories each kind of file must lie under (§1.3), owned by root or `zab-admin`.
    Tests may pass others to the loader; admin.toml can't change them."""

    workshops: str = "/var/lib/zab-admin/ws"  # rosters and out_dirs, under <workshops>/<name>/
    secrets: str = "/etc/zab-admin/secrets"  # the bots' zuliprcs
    break_glass: str = "/etc/zab-admin/break-glass"  # the admin keys


ROOTS = Roots()


@dataclass(frozen=True, slots=True)
class Bot:
    """An admin bot: its key, the identity it checks at startup, and its command channels."""

    zuliprc: str
    user_id: int  # required, so that no bot can stand on an allowlist (checked at load)
    email: str | None
    channels: frozenset[int]  # Zulip stream IDs it takes commands from; no other bot's


@dataclass(frozen=True, slots=True)
class AwsSettings:
    region: str  # one per workshop (Drew, Oct 15), of `[global] regions`; workshops may differ
    ami: str  # in that region, as the security group and the key pair, all in the scope (§5)
    security_group: str
    instance_types: tuple[str, ...]  # the first is the default; of `[global] instance_types`
    key_name: str  # imported by the server from its own key
    admin_keys: tuple[str, ...]  # break-glass public key files, given to every VM (§1.7)


@dataclass(frozen=True, slots=True)
class Workshop:
    name: str
    #: The fleet's scope: `workshop=<name>`, then `[global]`'s `zab-scope=<scope>` and extra tags
    #: (the test server's `zab-role=test`, §7). Each becomes one `--tag KEY=VALUE`.
    tags: tuple[tuple[str, str], ...]
    github_org: str
    refserve_url: str
    roster: str  # the roster CSV
    out_dir: str  # participant bot zuliprcs, known_hosts, spares.json (§1.3)
    organizers: frozenset[int]  # Zulip user IDs scoped to this workshop's bot only
    bot: Bot  # this workshop's admin bot, bound to it alone
    aws: AwsSettings
    #: `#<ws>-fleet-alerts`, where the waker posts this fleet's wake failures (§3, action 5);
    #: never a bot's command channel. `!waker on` needs it; None until it exists.
    alerts_channel: int | None = None
    #: The participants' channel (#627): its stream ID, so that a rename keeps it. `!roster
    #: sync` reads the roster from its subscribers; never a bot's command channel, an alerts
    #: channel or another workshop's. None: the roster comes from `!roster import` alone.
    channel: int | None = None
    #: The first sentence of "Start here"'s step 1, and of the sandbox's description: this
    #: workshop's gift subscriptions, in its own words (#709). None: zulip_provision.py's
    #: `GIFTS_STEP`. Markdown, one line; `!provision zulip` passes it base64url-encoded.
    gifts_step: str | None = None
    #: The bridge's `[bridge] mention_after_s` on this fleet's VMs (SPEC §4.8; #711): a turn
    #: that ends after running this long mentions its owner. `!rollout` and `!adopt-spare` pass it.
    mention_after_s: int = MENTION_AFTER_S
    #: A spare's idle stop, in minutes (#715): `spare_idle_stop.py --minutes`, after `!launch`
    #: and `!start` of spares.
    spare_idle_stop: int = SPARE_IDLE_STOP


@dataclass(frozen=True, slots=True)
class AdminConfig:
    provider: str
    admins: frozenset[int]  # Zulip user IDs who may act through every bot
    deployers: frozenset[int]  # SSH and deploy; approve !upgrade-server (after Oct 31)
    destructive_approvals: int
    plan_ttl_minutes: int
    release: str  # "final" or "rc"
    release_pattern: re.Pattern[str]  # RELEASE_PATTERNS[release], compiled
    console_url: str  # "" until Q4 names the console's host
    global_bot: Bot | None  # `[global]`: !version, !audit; later !upgrade-server, !new-workshop
    workshops: tuple[Workshop, ...]
    #: `[global]` (§5): `zab-scope=<scope>`, then the extra tags, in every workshop's scope;
    #: the regions the role may act in, and the instance types it may launch
    scope_tags: tuple[tuple[str, str], ...]
    regions: tuple[str, ...]
    instance_types: tuple[str, ...]
    #: The repository `!upgrade-server` fetches releases from (§1.6, #569), or "": the in-band
    #: upgrade is then refused, and a deployer deploys with deploy.sh.
    release_repo: str = ""

    def workshop(self, name: str) -> Workshop | None:
        return next((ws for ws in self.workshops if ws.name == name), None)

    def approvers(self, workshop: Workshop) -> frozenset[int]:
        """Who may request and approve this workshop's plans, through its bot: the admins and
        its organizers. A workshop's organizers are no one else's."""
        return self.admins | workshop.organizers


class _Table:
    """One TOML table, read key by key; `done()` refuses the keys nobody asked for."""

    def __init__(self, data: Any, prefix: str = "") -> None:
        self.data, self.prefix, self.used = data, prefix, set[str]()

    def fail(self, key: str, rule: str, value: Any = _MISSING) -> AdminConfigError:
        got = self.data.get(key) if value is _MISSING else value
        return AdminConfigError(f"{self.prefix}{key}: {rule}, got {got!r}")

    def get(self, key: str, kind: type | tuple[type, ...], default: Any = _MISSING) -> Any:
        self.used.add(key)
        if key not in self.data:
            if default is _MISSING:
                raise AdminConfigError(f"{self.prefix}{key} is required")
            return default
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, kind):  # TOML booleans are ints
            raise self.fail(key, f"expected {getattr(kind, '__name__', 'a string or a list')}")
        return value

    def text(self, key: str, pattern: re.Pattern[str], default: Any = _MISSING) -> str:
        value = self.get(key, str, default)
        if value != default and not pattern.fullmatch(value):
            raise self.fail(key, f"expected a value matching {pattern.pattern}")
        return value

    def integer(self, key: str, lo: int, hi: int, default: Any = _MISSING) -> int:
        value = self.get(key, int, default)
        if not lo <= value <= hi:
            raise self.fail(key, f"expected {lo} to {hi}", value)
        return value

    def items(
        self, key: str, check: Any, *, empty: bool = False, default: Any = _MISSING
    ) -> tuple[Any, ...]:
        """A list without repeats, each item passing `check`; empty only if `empty`."""
        values = self.get(key, list, default)
        if not (values or empty) or not all(map(check, values)) or len(set(values)) != len(values):
            raise self.fail(key, "expected a list of distinct, valid values")
        return tuple(values)

    def table(self, key: str, data: Any = _MISSING) -> _Table:
        value = self.get(key, dict) if data is _MISSING else data
        return _Table(value, f"{self.prefix}{key}.")

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise AdminConfigError(f"unknown key {self.prefix}{unknown[0]}")


def _positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _matches(pattern: re.Pattern[str]) -> Any:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


def _under(root: str) -> Any:
    """A check for a path strictly below `root`. `_PATH` has already refused `.` and `..`."""
    return lambda value: isinstance(value, str) and value.startswith(root + "/")


def parse_admin_config(
    text: str, source: str = "admin.toml", *, roots: Roots = ROOTS
) -> AdminConfig:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise AdminConfigError(f"{source}: not valid TOML: {exc}") from None
    try:
        return _config(_Table(data), roots)
    except AdminConfigError as exc:
        raise AdminConfigError(f"{source}: {exc}") from None


def _config(top: _Table, roots: Roots) -> AdminConfig:
    provider = top.get("provider", str)
    if provider != "aws":
        raise top.fail("provider", "expected 'aws', the only provider so far (§6)")
    top.used.add("workshop")
    entries = top.data.get("workshop")
    if not isinstance(entries, list) or not entries:
        raise top.fail("workshop", "expected [[workshop]] entries, at least one")
    release = top.get("release", str, "final")
    if release not in RELEASE_PATTERNS:
        raise top.fail("release", "expected 'final' or 'rc'")
    if not isinstance(top.data.get("global"), dict) or "scope" not in top.data["global"]:
        raise AdminConfigError(f"[global] scope is required: {MIGRATE}")
    glob = top.table("global")
    scope = ((SCOPE_KEY, glob.text("scope", _NAME)), *_extra_tags(glob))
    regions = glob.items("regions", _matches(_REGION))
    types = glob.items("instance_types", _matches(_INSTANCE_TYPE))
    global_bot = _bot(glob, roots) if _GLOBAL_BOT_KEYS & set(glob.data) else None
    glob.done()
    config = AdminConfig(
        provider=provider,
        admins=frozenset(top.items("admins", _positive_id, empty=True)),
        deployers=frozenset(top.items("deployers", _positive_id)),
        destructive_approvals=top.integer("destructive_approvals", 1, 100, 2),
        plan_ttl_minutes=top.integer("plan_ttl_minutes", 1, 60, 10),
        release=release,
        release_pattern=re.compile(RELEASE_PATTERNS[release], re.ASCII),
        console_url=top.text("console_url", _HTTPS, ""),
        release_repo=top.text("release_repo", RELEASE_REPO, ""),
        global_bot=global_bot,
        workshops=tuple(
            _workshop(top.table(f"workshop[{i}]", entry), roots, scope)
            for i, entry in enumerate(entries)
        ),
        scope_tags=scope,
        regions=regions,
        instance_types=types,
    )
    top.done()
    _check_scope(config)
    _check_workshops(config)
    return config


def _workshop(table: _Table, roots: Roots, scope: tuple[tuple[str, str], ...]) -> Workshop:
    if not isinstance(table.data, dict):
        raise AdminConfigError(f"{table.prefix[:-1]}: expected a table")
    if "tag" in table.data:
        raise table.fail("tag", f"the scope is workshop=<name> and [global]'s ({MIGRATE})")
    name = table.text("name", _NAME)
    aws, bot = table.table("aws"), table.table("bot")
    home = f"{roots.workshops}/{name}"  # this workshop's own directory (§1.3)
    workshop = Workshop(
        name=name,
        tags=(("workshop", name), *scope),
        github_org=table.text("github_org", _ORG),
        refserve_url=table.text("refserve_url", _HTTPS),
        roster=_path(table, "roster", home),
        out_dir=_path(table, "out_dir", home),
        organizers=frozenset(table.items("organizers", _positive_id, empty=True, default=[])),
        bot=_bot(bot, roots),
        alerts_channel=table.get("alerts_channel", int, None),
        channel=table.get("channel", int, None),
        gifts_step=table.text("gifts_step", _GIFTS_STEP, None),
        mention_after_s=table.integer("mention_after_s", 0, MENTION_AFTER_MAX, MENTION_AFTER_S),
        spare_idle_stop=table.integer("spare_idle_stop", *IDLE_STOP_MINUTES, SPARE_IDLE_STOP),
        aws=AwsSettings(
            region=aws.text("region", _REGION),
            ami=aws.text("ami", _AMI),
            security_group=aws.text("security_group", _SECURITY_GROUP),
            instance_types=aws.items("instance_types", _matches(_INSTANCE_TYPE)),
            key_name=aws.text("key_name", _KEY_NAME),
            admin_keys=aws.items(
                "admin_keys",
                lambda v: _matches(_PUBLIC_KEY_FILE)(v) and _under(roots.break_glass)(v),
            ),
        ),
    )
    for done in (bot, aws, table):
        done.done()
    for key in ("alerts_channel", "channel"):
        value = getattr(workshop, key)
        if value is not None and not _positive_id(value):
            raise table.fail(key, "expected a Zulip channel ID")
    gifts = workshop.gifts_step
    if gifts is not None and len(gifts.encode("utf-8")) > GIFTS_STEP_BYTES:
        raise table.fail("gifts_step", f"expected at most {GIFTS_STEP_BYTES} bytes in UTF-8")
    return workshop


def _check_scope(config: AdminConfig) -> None:
    """Each workshop's region and instance types are among `[global]`'s: the role's policy
    allows those only (§5), so any other would fail at its first launch, not at load."""
    for i, ws in enumerate(config.workshops):
        where = f"workshop[{i}].aws."
        if ws.aws.region not in config.regions:
            raise AdminConfigError(f"{where}region: {ws.aws.region!r} is not in [global] regions")
        extra = sorted(set(ws.aws.instance_types) - set(config.instance_types))
        if extra:
            raise AdminConfigError(f"{where}instance_types: {extra} not in [global] instance_types")


def _path(table: _Table, key: str, root: str) -> str:
    value = table.text(key, _PATH)
    if not _under(root)(value):
        raise table.fail(key, f"expected a path under {root}/")
    return value


def _bot(table: _Table, roots: Roots) -> Bot:
    """A bot's keys. The caller checks that the table has no others: `[global]` holds the
    scope beside the global admin bot's keys."""
    user_id = table.get("user_id", int)
    if not _positive_id(user_id):
        raise table.fail("user_id", "expected a Zulip user ID")
    return Bot(
        zuliprc=_path(table, "zuliprc", roots.secrets),
        user_id=user_id,
        email=table.text("email", _EMAIL, None),
        channels=frozenset(table.items("channels", _positive_id)),
    )


def _extra_tags(table: _Table) -> tuple[tuple[str, str], ...]:
    """`[global] tags`: scope tags beyond `workshop` and `zab-scope`, on every resource and in
    every condition of the role's policy (the test server's `zab-role=test`, §7). IAM matches
    the key of `aws:ResourceTag/<key>` regardless of case (§5), so keys compare in lower case: a
    reserved key is refused in any case, and so are two keys that differ only in case."""
    tags: dict[str, str] = {}
    reserved = {key.lower() for key in _RESERVED_TAG_KEYS}
    for spec in table.get("tags", list, []):
        key, _, value = spec.partition("=") if isinstance(spec, str) else ("", "", "")
        if (
            not _TAG_KEY.fullmatch(key)
            or not _TAG_VALUE.fullmatch(value)
            or key.lower().startswith("aws:")
            or key.lower() in reserved
            or key.lower() in {k.lower() for k in tags}
        ):
            raise table.fail("tags", "expected KEY=VALUE extra scope tags, each key once")
        tags[key] = value
    return tuple(tags.items())


def _distinct(what: str, items: list[Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        values = [getattr(item, key) for item in items if getattr(item, key) is not None]
        if len(set(values)) != len(values):
            raise AdminConfigError(f"two {what} have the same {key}")


def _check_workshops(config: AdminConfig) -> None:
    """Workshops never share a name, a scope, a file or a bot, and each can approve its plans.

    The scope includes `workshop=<name>`, and each workshop's files lie under its own
    `<workshops>/<name>/`, so distinct names already give disjoint scopes and files; the checks
    on tags, rosters and out_dirs say so explicitly. No file serves two roles: a roster inside
    an out_dir would be written by scripts and carried by the backups (§1.7). Two bots in one
    channel would both obey a bare `!stop all` there, each against its own fleet, so a channel
    belongs to one bot. Bots are never obeyed (§2.1), so none may stand on an allowlist. A
    workshop's alerts channel is its own, and no bot's command channel: its participant bots
    post the waker's alerts there, and a bot never obeys them. A workshop's participants'
    channel (`channel`, #627) is its own too, and neither of those: its subscribers become its
    roster. A workshop can complete every plan when a global admin can approve it, whose
    approval alone completes one (#558), or when its approvers are `destructive_approvals` at
    least. A files host serves one GitHub organization (#693), so workshops that share a
    `refserve_url` share their `github_org`, compared ignoring case (#771)."""
    bots = [ws.bot for ws in config.workshops]
    if config.global_bot:
        bots.append(config.global_bot)
    _distinct("workshops", list(config.workshops), ("name", "tags", "roster", "out_dir"))
    _distinct("bots", bots, ("zuliprc", "user_id", "email"))
    channels = [channel for bot in bots for channel in bot.channels]
    if len(set(channels)) != len(channels):
        raise AdminConfigError("a channel is bound to two bots: each would obey its commands")
    alerts = [ws.alerts_channel for ws in config.workshops if ws.alerts_channel is not None]
    if len(set(alerts)) != len(alerts) or set(alerts) & set(channels):
        raise AdminConfigError(
            "an alerts_channel is another workshop's, or a bot's command channel: the waker's "
            "alerts name the fleet's VMs, and participant bots post them"
        )
    rosters = [ws.channel for ws in config.workshops if ws.channel is not None]
    if len(set(rosters)) != len(rosters) or set(rosters) & (set(channels) | set(alerts)):
        raise AdminConfigError(
            "a workshop's channel is another workshop's, a bot's command channel or an "
            "alerts_channel: its subscribers become its roster (!roster sync)"
        )
    people = config.admins | config.deployers
    for ws in config.workshops:
        people |= ws.organizers
        if not config.admins and len(config.approvers(ws)) < config.destructive_approvals:
            raise AdminConfigError(
                f"workshop {ws.name}: {len(config.approvers(ws))} admins and organizers can "
                f"approve its plans, fewer than destructive_approvals = "
                f"{config.destructive_approvals}"
            )
    for bot in bots:
        if bot.user_id in people:
            raise AdminConfigError(f"user {bot.user_id} is an admin bot and on an allowlist")
    hosts: dict[str, Workshop] = {}
    for ws in config.workshops:
        first = hosts.setdefault(ws.refserve_url.rstrip("/"), ws)
        if first.github_org.lower() != ws.github_org.lower():
            raise AdminConfigError(
                f"workshops {first.name} (refserve_url {first.refserve_url}, github_org "
                f"{first.github_org}) and {ws.name} (refserve_url {ws.refserve_url}, github_org "
                f"{ws.github_org}) share a files host, which serves one GitHub organization"
            )
    files = [(ws.roster, "roster") for ws in config.workshops]
    files += [(ws.out_dir, "out_dir") for ws in config.workshops]
    files += [(bot.zuliprc, "bot zuliprc") for bot in bots]
    files += [(key, "admin key") for ws in config.workshops for key in ws.aws.admin_keys]
    for path, role in files:
        for other, other_role in files:
            if role != other_role and (path == other or path.startswith(other + "/")):
                raise AdminConfigError(f"{path} ({role}) lies at or in {other} ({other_role})")


def load_admin_config(path: str | Path = DEFAULT_PATH, *, roots: Roots = ROOTS) -> AdminConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AdminConfigError(f"{path}: {exc}") from None
    return parse_admin_config(text, str(path), roots=roots)


def main(argv: list[str] | None = None) -> int:
    """`python -m zab.admin.config [--config PATH] WHAT`: what deploy.sh needs from admin.toml,
    read with the release's own loader (docs/ADMIN.md §1.6). `check` loads it and prints
    nothing. The others print one item per line, built from values whose patterns admit no
    whitespace: `console-host`, the host of console_url (nothing when it is unset); `key-pairs`,
    `NAME REGION KEY_NAME KEY=VALUE…` per workshop, its scope tags last; `zuliprcs`, every
    bot's zuliprc, the global bot's first."""
    parser = argparse.ArgumentParser(prog="python -m zab.admin.config")
    parser.add_argument("--config", default=str(DEFAULT_PATH))
    parser.add_argument("what", choices=["check", "console-host", "key-pairs", "zuliprcs"])
    args = parser.parse_args(argv)
    try:
        config = load_admin_config(args.config)
    except AdminConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    lines: list[str] = []
    if args.what == "console-host" and config.console_url:
        lines = [urlsplit(config.console_url).netloc]
    elif args.what == "key-pairs":
        for ws in config.workshops:
            tags = [f"{key}={value}" for key, value in ws.tags]
            lines.append(" ".join([ws.name, ws.aws.region, ws.aws.key_name, *tags]))
    elif args.what == "zuliprcs":
        bots = [config.global_bot] if config.global_bot else []
        lines = [bot.zuliprc for bot in [*bots, *(ws.bot for ws in config.workshops)]]
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
