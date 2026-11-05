"""The approval bots' command grammar (docs/ADMIN.md §2.2, §2.3): a deterministic parser.

This is the security boundary of bots that hold the fleet's keys. No LLM is involved, and
nothing from a message reaches an argument list as it stands: every word is matched against a
fixed table, an ASCII pattern, the roster or the config, and the value that comes out is a
looked-up slug, spare, keyword or range-checked integer.

- **One bot per workshop** (Drew, Oct 15). Each admin bot is bound to one workshop, and the
  bot, not the message, supplies it: the parser gets that workshop's `Roster` and nothing
  else, so no command can name another workshop or its rows. A slug means a row of this
  roster; `all` means every participant row of this workshop, never its spares and never
  another workshop. The server's `[global]` bot has no roster and takes only the server-level
  verbs.
- **Addressing.** A message is a command only when its first line starts with `!`, or with a
  mention of this bot's own user ID (`@**<name>**` or `@**<name>|<id>**`), a run of blanks
  (`MENTION_GAP`) and `!`. Any other leading mention, of a person, a bot, a group or a silent
  one, makes it `NotCommand`: that is what makes a click on another sender's `zform` widget
  inert (§2.2). The blanks are dropped, never read: the command is what follows its `!`.
- **One line.** Only the first line is read. Nothing after the first `\\n` can reach a word;
  any other line break (CR, U+2028, …) inside the first line is outside every word's alphabet
  and rejects the message. A later line that starts with `!` rejects it too: one command per
  message.
- **Strict.** Words are separated by single spaces. An unknown verb, keyword or other word, a
  repeated or contradictory keyword, or a slug not in the roster rejects the whole message
  (`Rejected`, a one-line reason that never echoes unsafe text). Nothing runs. A slug that
  this roster lacks gives `NotInRoster`, so that the bot can point to the right workshop's bot.
- **Explicit selections.** A fleet-wide action needs `all`; `spares` and `zab-spare-N` only
  where the verb allows them (§2.3). `!retire` takes named rows only, never `all` (#559).
- **Code is not a command** (#546). A first line that starts with a backtick is `NotCommand`,
  whatever is inside. `code_wrapped` only tells the bot whether a whole message is one command
  formatted as code, so that it can answer with a hint: it returns a bool, never the text
  inside the code, and `parse` never sees that text on its own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", re.ASCII)
WS_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,45}[a-z0-9])?", re.ASCII)  # §2.2, §2.9
SPARE_RE = re.compile(r"zab-spare-[0-9]{1,3}", re.ASCII)
VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?", re.ASCII)
_PLAN_RE = re.compile(r"P-([1-9][0-9]{0,8})", re.ASCII)
_HASH6_RE = re.compile(r"[0-9a-f]{6}", re.ASCII)
_NUMBER_RE = re.compile(r"0|[1-9][0-9]{0,3}", re.ASCII)
_SHOWABLE = re.compile(r"!?[A-Za-z0-9._=-]{1,40}", re.ASCII)
_REGION_RE = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]{1,2}", re.ASCII)  # zab.admin.config's
_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)  # a workshop's, ditto
_ID_RE = re.compile(r"[1-9][0-9]{0,18}", re.ASCII)
_SIZE_RE = re.compile(r"([1-9][0-9]{0,3})(?:GB|gb)", re.ASCII)  # !grow-disk's `300GB`
#: The largest size `!grow-disk` grows a root volume to, in GB (#553). A constant, not an
#: admin.toml setting: that would change `config.py`, which is privileged (D32), as `costs.PRICES`
#: is a constant for the same reason. `grow_disk.MAX_GB` refuses above it too; a test keeps the
#: two equal.
MAX_DISK_GB = 1000
#: Zulip's compose box links an attachment as `[name](/user_uploads/<realm>/…/<name>)`.
_UPLOAD_LINK = re.compile(r"\]\((/user_uploads/[^\s)]*)\)")
_UPLOAD_MARKDOWN = re.compile(r" ?\[[^\]\n]*\]\(/user_uploads/[^\s)]*\)")  # the whole link
#: `!new-workshop … channel="<name>"` (#627): the participants' channel, by its Zulip name, the
#: one value that may hold spaces and capitals, and so the one in quotes. Zulip's names have at
#: most 60 characters (`Stream.MAX_NAME_LENGTH`). Letters, marks and digits of any script, and
#: a few marks of punctuation: none that Zulip's Markdown reads (`*`, `@`, `#`, `` ` ``, `$`,
#: `[`, `|`, `~`, …), since the command is shown in plans and the audit log.
#: Straight quotes, or a phone's curly ones (iOS's smart punctuation, on by default), each end
#: any of the three, since a keyboard picks `“` or `”` from the character before it (review of
#: #638): none is a name's character. `text()` writes them back straight.
_QUOTES = '"\u201c\u201d'
_CHANNEL_QUOTED = re.compile(f" channel=[{_QUOTES}]([^{_QUOTES}\n]*)[{_QUOTES}](?= |$)")
CHANNEL_MARKS = frozenset(" -_.,'&()+:!?/")
CHANNEL_RULE = (
    "a channel's name in double quotes, 1 to 60 characters: letters, digits, single inner "
    "spaces, and - _ . , ' & ( ) + : ! ? /"
)
_UPLOAD_PATH = re.compile(
    r"/user_uploads/[0-9]{1,10}(?:/[A-Za-z0-9_-]{1,64}){1,3}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}"
    r"\.(?:csv|CSV)",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class VerbSpec:
    kind: str  # "read", "change", "destructive" or "approval" (§2.2's class column)
    usage: str
    #: Fixed arguments, in order; `?` marks an optional last one, `*` takes the rest.
    head: tuple[str, ...] = ()
    selection: bool = False  # takes SEL, which may not be empty
    spares: bool = False  # SEL may also name `spares` and `zab-spare-N`
    keywords: tuple[str, ...] = ()  # bare keywords, each at most once
    exclusive: bool = False  # at most one of the keywords
    settings: tuple[str, ...] = ()  # `key=value` options, each at most once
    server: bool = False  # needs no workshop: also on the `[global]` bot
    dm: bool = False  # also works in a direct message to the bot
    only_global: bool = False  # on the `[global]` bot alone (§2.9's `!new-workshop`)
    named: bool = False  # SEL names its rows: `all` is refused (`!retire`, #559)
    optional: tuple[str, ...] = ()  # an `only_global` verb's settings that may be left out


#: The verbs the bots offer (§2.2's table). `roster` and `provision` take a subcommand.
SPECS: dict[str, VerbSpec] = {
    "help": VerbSpec("read", "!help [command]", ("topic?",), server=True, dm=True),
    "version": VerbSpec("read", "!version", server=True),
    "plans": VerbSpec("read", "!plans", server=True),
    "plan": VerbSpec("read", "!plan P-n", ("plan",), server=True),
    "audit": VerbSpec("read", "!audit [n]", ("count?",), server=True),
    "console": VerbSpec("read", "!console", server=True, dm=True),
    "status": VerbSpec("read", "!status SEL", selection=True),
    "roster show": VerbSpec("read", "!roster show [slug]", ("slug?",)),
    "roster import": VerbSpec("change", "!roster import, with the CSV attached"),
    # the roster from the workshop's channel (#627): its subscribers, linked by Zulip ID
    "roster sync": VerbSpec("change", "!roster sync"),
    "roster remove": VerbSpec("change", "!roster remove slug", ("slug",)),
    "roster restore": VerbSpec("change", "!roster restore slug", ("slug",)),
    "roster rename": VerbSpec("change", "!roster rename slug new-slug", ("slug", "new-slug")),
    "provision zulip": VerbSpec(
        "change", "!provision zulip [hold] SEL", selection=True, keywords=("hold",)
    ),
    "provision repos": VerbSpec(
        "change",
        "!provision repos [invites-only|no-invite] SEL",
        selection=True,
        keywords=("invites-only", "no-invite"),
        exclusive=True,
    ),
    "launch": VerbSpec(
        "change",
        "!launch SEL [spares=N] [protect]",
        selection=True,
        keywords=("protect",),
        settings=("spares",),
    ),
    "protect": VerbSpec("change", "!protect on|off SEL", ("on",), selection=True),
    # without `lean-shared` or `idle-stop=`, their defaults (#704; `actions.IDLE_STOP_DEFAULT`):
    # the shared Mathlib where a VM's isn't shared yet; idle stop at 60 minutes where the waker
    # is on and no timer is on, unless it was turned off on purpose: never what `idle-stop=60`
    # does, which overrides both (review of #705)
    "rollout": VerbSpec(
        "change",
        "!rollout VERSION SEL [steer=on|off] [lean-shared] [idle-stop=N] [restart-codex] "
        "[multi-party=on|off] [raw-log=off|meta] (defaults: the shared Mathlib; idle stop "
        "at 60 where the waker is on and none is on)",
        ("version",),
        selection=True,
        keywords=("lean-shared", "restart-codex"),
        # steer=on|off: --claude-steer / --no-claude-steer; steering is on by default (#318)
        settings=("steer", "idle-stop", "multi-party", "raw-log"),
    ),
    "idle-stop": VerbSpec("change", "!idle-stop N SEL", ("minutes",), selection=True),
    "start": VerbSpec("change", "!start SEL|spares", selection=True, spares=True),
    "stop": VerbSpec("change", "!stop SEL|spares", selection=True, spares=True),
    "adopt-spare": VerbSpec("change", "!adopt-spare slug zab-spare-N", ("slug", "spare")),
    # one row's root volume, to N GB (#553): EBS only grows, and bills every GB a month
    "grow-disk": VerbSpec("change", "!grow-disk slug NGB", ("slug", "size")),
    "waker": VerbSpec("change", "!waker on|off SEL", ("on",), selection=True),
    "final-sync": VerbSpec("change", "!final-sync SEL", selection=True),
    "export": VerbSpec("change", "!export SEL", selection=True),
    "uninstall": VerbSpec("change", "!uninstall SEL", selection=True),
    # `unsynced`: tear down rows with no successful !final-sync on record (§3, action 7)
    "teardown": VerbSpec(
        "destructive",
        "!teardown SEL|spares|zab-spare-N [unsynced]",
        selection=True,
        spares=True,
        keywords=("unsynced",),
    ),
    "deactivate-bots": VerbSpec("destructive", "!deactivate-bots SEL", selection=True),
    # #559: one plan that ends named rows: their VMs (after a sync, and an export with `export`)
    # and their bots; `unsynced` tears down a row whose work could not be synced
    "retire": VerbSpec(
        "destructive",
        "!retire slug… [export] [unsynced]",
        selection=True,
        keywords=("export", "unsynced"),
        named=True,
    ),
    # §2.9: a workshop made on the server's bot; its first three settings are required, and
    # `channel=`, its participants' channel, adopted or made, is optional (#627)
    "new-workshop": VerbSpec(
        "server",
        '!new-workshop NAME region=R like=WS organizers=ID,… [channel="NAME"]',
        ("ws-name",),
        settings=("region", "like", "organizers", "channel"),
        server=True,
        only_global=True,
        optional=("channel",),
    ),
    "approve": VerbSpec(
        "approval", "!approve P-n hash6 [phrase]", ("plan", "hash6", "phrase*"), server=True
    ),
    "reject": VerbSpec("approval", "!reject P-n", ("plan",), server=True),
}
#: Verbs of later stages (§8.1): not offered through the bots yet; a deployer runs them by hand.
LATER = frozenset({"rebind", "upgrade-server", "cancel"})
_GROUPS = frozenset(key.split()[0] for key in SPECS if " " in key)  # roster, provision
_TOPICS = frozenset(key.split()[0] for key in SPECS) | LATER
#: Words that are keywords or setting names wherever a slug may stand (`steer` included, so that
#: a bare `steer` can't select a participant of that name): no roster slug may be one of them.
RESERVED = frozenset({"all", "spares"}).union(  # not !new-workshop's: it takes no slug
    *(spec.keywords + spec.settings for spec in SPECS.values() if not spec.only_global)
)
#: How a reason names each fixed argument it expected.
_EXPECTED = {
    "version": "a release tag this server takes",
    "minutes": "minutes, 0 or 10 to 240",
    "count": "a count from 1 to 50",
    "on": "on or off",
    "plan": "a plan ID like P-12",
    "hash6": "the first 6 hex digits of the plan's hash",
    "slug": "a slug of this workshop's roster",
    "spare": "an available spare of this workshop",
    "size": f"a size like 300GB, at most {MAX_DISK_GB}GB",
    "topic": "a command name",
    "ws-name": "a new workshop's name: a-z, 0-9 and '-', at most 47 characters",
    "new-slug": "a new slug: a-z, 0-9 and inner '-', at most 32 characters, not a keyword",
}


@dataclass(frozen=True, slots=True)
class Roster:
    """This bot's workshop: its roster's slugs in roster order, and the available spares in
    its `spares.json`."""

    slugs: tuple[str, ...] = ()
    spares: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for slug in self.slugs:
            if not SLUG_RE.fullmatch(slug) or SPARE_RE.fullmatch(slug) or slug in RESERVED:
                raise ValueError(f"roster slug {slug!r} is not usable in commands")
        if not all(SPARE_RE.fullmatch(name) for name in self.spares):
            raise ValueError("a spare's name is not zab-spare-N")
        if len(set(self.slugs)) != len(self.slugs) or len(set(self.spares)) != len(self.spares):
            raise ValueError("a slug or spare is listed twice")


@dataclass(frozen=True, slots=True)
class Context:
    """What a message to one bot is checked against: the bot's identity (from `GET /users/me`
    and its `realm_user` events), the release tags the server takes (`AdminConfig.
    release_pattern`, set by admin.toml's `release`), and the bot's workshop's `Roster`, which
    is None for the server's `[global]` bot."""

    bot_user_id: int
    bot_full_name: str
    release_pattern: re.Pattern[str]
    roster: Roster | None

    def __post_init__(self) -> None:
        user_id = self.bot_user_id
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError(f"bad bot user ID {user_id!r}")
        if not self.bot_full_name or any(c in self.bot_full_name for c in "*|\n"):
            raise ValueError(f"bad bot name {self.bot_full_name!r}")
        if not (self.roster is None or isinstance(self.roster, Roster)):
            raise ValueError("the roster is a Roster, or None for the [global] bot")


@dataclass(frozen=True, slots=True)
class Selection:
    """SEL, within the bot's workshop. `all` is every participant row, never the spares;
    `spares` every available spare. Named slugs and spares keep roster and spares order.

    Never empty (§2.2: no implicit "everyone"): the scripts read a missing `--only` as every
    participant, so an empty selection, or one that resolves to no target, is an error."""

    all: bool = False
    spares: bool = False
    slugs: tuple[str, ...] = ()
    spare_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        names = (self.slugs, self.spare_names)
        if not (
            isinstance(self.all, bool)
            and isinstance(self.spares, bool)
            and all(isinstance(n, tuple) and all(isinstance(s, str) for s in n) for n in names)
        ):  # a truthy "false" read back from a stored plan must not mean `all`
            raise ValueError("a selection is two bools and two tuples of names")
        if not (self.all or self.spares or self.slugs or self.spare_names):
            raise ValueError("an empty selection")

    def tokens(self) -> tuple[str, ...]:
        people = ("all",) if self.all else self.slugs
        return people + (("spares",) if self.spares else self.spare_names)

    def targets(self, roster: Roster) -> tuple[str, ...]:
        """The selected slugs, then spare names, against the workshop's `roster`. Raises
        ValueError when `all` or `spares` finds nothing there, rather than return a list that
        selects less than was asked, or nothing, and when a named slug or spare isn't the
        roster's own (it may have changed since the command was parsed)."""
        if (self.all and not roster.slugs) or (self.spares and not roster.spares):
            raise ValueError("`all` or `spares` selects nothing in this roster")
        if not (
            set(self.slugs) <= set(roster.slugs) and set(self.spare_names) <= set(roster.spares)
        ):
            raise ValueError("a named slug or spare is not in this roster")
        people = roster.slugs if self.all else self.slugs
        return people + (roster.spares if self.spares else self.spare_names)


@dataclass(frozen=True, slots=True)
class Command:
    """A parsed command for the bot's own workshop, which the bot, not the command, knows.
    `verb` is a key of SPECS (`"rollout"`, `"roster import"`, …); only the fields that verb's
    spec names are set. `tokens()` is the normalized argument list."""

    verb: str
    selection: Selection | None = None
    keywords: frozenset[str] = frozenset()
    version: str | None = None  # !rollout
    on: bool | None = None  # !protect on|off
    idle_stop: int | None = None  # !idle-stop N, !rollout idle-stop=N: 0 or 10..240 minutes
    spares: int | None = None  # !launch spares=N: 0..10
    steer: bool | None = None  # !rollout steer=on|off
    multi_party: bool | None = None  # !rollout multi-party=on|off
    raw_log: str | None = None  # !rollout raw-log=off|meta
    slug: str | None = None  # !roster show [slug], remove, restore, rename; !adopt-spare
    new_slug: str | None = None  # !roster rename slug new-slug (#627)
    spare: str | None = None  # !adopt-spare
    size: int | None = None  # !grow-disk: the root volume's new size, in GB (1 to MAX_DISK_GB)
    plan: int | None = None  # !plan, !approve, !reject: P-<plan>, unique on the server
    hash6: str | None = None  # !approve
    phrase: tuple[str, ...] = ()  # !approve: the typed confirmation, e.g. ("stop", "35")
    count: int | None = None  # !audit [n]
    topic: str | None = None  # !help [command]
    upload: str | None = None  # !roster import: the attachment's /user_uploads/… path
    ws_name: str | None = None  # !new-workshop: the new workshop's name (§2.9)
    region: str | None = None  # !new-workshop region=
    like: str | None = None  # !new-workshop like=: the workshop whose settings it copies
    organizers: tuple[int, ...] | None = None  # !new-workshop organizers=: Zulip user IDs
    channel: str | None = None  # !new-workshop channel="…": the participants' channel (#627)

    def __post_init__(self) -> None:
        """A `Command` built directly is held to what `parse` guarantees: a verb of SPECS, and
        a selection exactly when the verb takes one."""
        if not isinstance(self.verb, str) or self.verb not in SPECS:
            raise ValueError("not a verb of SPECS")
        if (self.selection is not None) != self.spec.selection:
            raise ValueError(f"!{self.verb} takes a selection exactly when its spec says so")

    @property
    def spec(self) -> VerbSpec:
        return SPECS[self.verb]

    def tokens(self) -> tuple[str, ...]:
        spec, out = self.spec, list[str]()
        for kind in spec.head:
            name = kind.rstrip("?*")
            out += _render(name, getattr(self, _field(name)))
        out += self.selection.tokens() if self.selection else ()
        out += [word for word in spec.keywords if word in self.keywords]
        for key in spec.settings:
            out += [f"{key}={v}" for v in _render(key, getattr(self, _field(key)))]
        return tuple(out)

    def text(self) -> str:
        """The command as the bot shows it: `!` and the verb, then `tokens()`."""
        return " ".join(("!" + self.verb, *self.tokens()))


@dataclass(frozen=True, slots=True)
class Rejected:
    """A command that fails the grammar. `reason` is one line and quotes only safe words."""

    reason: str


@dataclass(frozen=True, slots=True)
class NotInRoster(Rejected):
    """Rejected because `slug`, slug-shaped and in a slug's place, is not in this workshop's
    roster. Nothing runs. The bot (PR 9) looks for it in the other workshops' rosters and,
    where one has it, answers with a hint to ask that workshop's bot (rate-limited as for
    foreign senders, §2.1); otherwise with `reason`. The parser never sees another roster."""

    slug: str


@dataclass(frozen=True, slots=True)
class NotCommand:
    """Not addressed to the bot as a command: no reply, no action."""

    why: str


class _Reject(Exception):
    def __init__(self, reason: str, slug: str | None = None) -> None:
        super().__init__(reason)
        self.slug = slug


def _field(name: str) -> str:
    """`Command`'s field for a head argument or setting: `!idle-stop N` and `idle-stop=N` both
    set `idle_stop`."""
    return "idle_stop" if name == "minutes" else name.replace("-", "_")


def _render(kind: str, value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bool):
        return ["on" if value else "off"]
    if kind == "plan":
        return [f"P-{value}"]
    if kind == "size":
        return [f"{value}GB"]
    if kind == "organizers":  # one word: `organizers=105,106`
        return [",".join(map(str, value))]  # type: ignore[call-overload]
    if kind == "channel":  # always quoted, so that `parse` reads `text()` back (#627)
        return [f'"{value}"']
    return list(value) if isinstance(value, tuple) else [str(value)]


def _show(word: str) -> str:
    if _SHOWABLE.fullmatch(word):
        return f"`{word}`"
    return "a word with characters outside a-z, 0-9, '-', '.' and '='"


def _missing_slug(word: str, reason: str) -> _Reject:
    """A slug-shaped word that this roster lacks may be another workshop's (`NotInRoster`)."""
    plain = not SLUG_RE.fullmatch(word) or SPARE_RE.fullmatch(word) or word in RESERVED
    return _Reject(reason, None if plain else word)


def _new_slug(word: str) -> bool:
    """A slug a row may take (`!roster rename`, #627): the slug's shape, no keyword, never
    `zab-spare-N`. Whether it is free is the script's to check, against every roster."""
    return bool(SLUG_RE.fullmatch(word)) and not SPARE_RE.fullmatch(word) and word not in RESERVED


def _number(word: str, lo: int, hi: int) -> int | None:
    if _NUMBER_RE.fullmatch(word) and lo <= int(word) <= hi:
        return int(word)
    return None


def _size(word: str) -> int | None:
    """`300GB` (or `300gb`) → 300, from 1 to `MAX_DISK_GB`."""
    match = _SIZE_RE.fullmatch(word)
    return int(match[1]) if match and int(match[1]) <= MAX_DISK_GB else None


def _minutes(word: str) -> int | None:
    value = _number(word, 0, 240)
    return None if value is None or 0 < value < 10 else value  # push_config.py's minimum


def channel_name(value: str) -> str | None:
    """`value` as `!new-workshop`'s `channel=` takes it (`CHANNEL_RULE`), or None (#627)."""
    if not 1 <= len(value) <= 60 or value != value.strip(" ") or "  " in value:
        return None
    if not all(c in CHANNEL_MARKS or unicodedata.category(c)[0] in "LMN" for c in value):
        return None
    return value


#: `key=value` options: each value's parser (None when invalid) and its rule, for the reason.
_SETTINGS = {
    "spares": (lambda v: _number(v, 0, 10), "0 to 10"),
    "idle-stop": (_minutes, "0 or 10 to 240"),
    "steer": (lambda v: {"on": True, "off": False}.get(v), "on or off"),
    "multi-party": (lambda v: {"on": True, "off": False}.get(v), "on or off"),
    "raw-log": (lambda v: v if v in ("off", "meta") else None, "off or meta"),
    # !new-workshop's (§2.9): shapes only; the bot checks them against admin.toml
    "region": (lambda v: v if _REGION_RE.fullmatch(v) else None, "a region like us-east-2"),
    "like": (lambda v: v if _NAME_RE.fullmatch(v) else None, "a workshop of this server"),
    "organizers": (lambda v: _ids(v), "Zulip user IDs, comma-separated, each once, at most 20"),
    # a one-word name may go unquoted; one in quotes is read before the words (`_command`)
    "channel": (channel_name, CHANNEL_RULE),
}


def _ids(word: str) -> tuple[int, ...] | None:
    """`105,106`: at most 20 distinct user IDs."""
    ids = [int(x) for x in word.split(",") if _ID_RE.fullmatch(x) and int(x) < 2**63]
    if len(ids) != len(word.split(",")) or len(set(ids)) != len(ids) or len(ids) > 20:
        return None
    return tuple(ids)


def _head(kind: str, word: str, ctx: Context, roster: Roster) -> object:
    """One fixed argument's value, or None when `word` isn't one."""
    if kind == "version":
        ok = VERSION_RE.fullmatch(word) and ctx.release_pattern.fullmatch(word)
        return word if ok else None
    if kind == "plan":
        match = _PLAN_RE.fullmatch(word)
        return int(match[1]) if match else None
    lookup = {
        "hash6": lambda: word if _HASH6_RE.fullmatch(word) else None,
        "minutes": lambda: _minutes(word),
        "count": lambda: _number(word, 1, 50),
        "on": lambda: {"on": True, "off": False}.get(word),
        "slug": lambda: word if word in roster.slugs else None,
        "spare": lambda: word if word in roster.spares else None,
        "size": lambda: _size(word),
        "topic": lambda: word if word in _TOPICS else None,
        "ws-name": lambda: word if WS_NAME_RE.fullmatch(word) else None,
        # `!roster rename`'s (#627): its shape here; roster_sync.py checks it is free
        "new-slug": lambda: word if _new_slug(word) else None,
    }
    return lookup[kind]()


def _arguments(spec: VerbSpec, args: list[str], ctx: Context, roster: Roster) -> dict[str, object]:
    fields: dict[str, object] = {}
    for kind in spec.head:
        name = kind.rstrip("?*")
        if kind.endswith("*"):  # the approval phrase: up to three words, as approvals.py
            # compares them; `new-workshop <name>` names a workshop, longer than a slug (§2.9)
            named = args[:1] == ["new-workshop"] and len(args) == 2
            word = WS_NAME_RE if named else SLUG_RE
            if len(args) > 3 or not all(word.fullmatch(arg) for arg in args):
                raise _Reject(f"the phrase is up to three words like `stop 35` ({spec.usage})")
            fields[name], args = tuple(args), []
        elif not args:
            if not kind.endswith("?"):
                raise _Reject(f"missing {_EXPECTED[name]}: {spec.usage}")
        else:
            value = _head(name, args[0], ctx, roster)
            if value is None:
                reason = f"expected {_EXPECTED[name]}, got {_show(args[0])}: {spec.usage}"
                raise _missing_slug(args[0], reason) if name == "slug" else _Reject(reason)
            fields[_field(name)], args = value, args[1:]
    seen: set[str] = set()
    for word in args:
        if word in seen:
            raise _Reject(f"{_show(word)} is given twice")
        seen.add(word)
        key, equals, value = word.partition("=")
        if equals and key in spec.settings:
            if _field(key) in fields:
                raise _Reject(f"`{key}=` is given twice")
            parser, rule = _SETTINGS[key]
            fields[_field(key)] = parser(value)
            if fields[_field(key)] is None:
                raise _Reject(f"`{key}=` takes {rule}")
        elif not (
            word in spec.keywords
            or (spec.selection and (word == "all" or word in roster.slugs))
            or (spec.spares and (word == "spares" or word in roster.spares))
        ):
            raise _unexpected(word, spec)
    keywords = [word for word in spec.keywords if word in seen]
    if spec.exclusive and len(keywords) > 1:
        raise _Reject(f"give one of {' or '.join(spec.keywords)}")
    fields["keywords"] = frozenset(keywords)
    if spec.selection:
        fields["selection"] = _selection(spec, seen, roster)
    return fields


def _selection(spec: VerbSpec, seen: set[str], roster: Roster) -> Selection:
    every, every_spare = "all" in seen, "spares" in seen
    slugs = tuple(slug for slug in roster.slugs if slug in seen)
    spare_names = tuple(name for name in roster.spares if name in seen)
    if every and spec.named:  # a kept VM must never go with `all` (ORGANIZERS §5.2)
        raise _Reject(f"name the rows that end, never `all` ({spec.usage})")
    if (every and slugs) or (every_spare and spare_names):
        raise _Reject("`all` and `spares` already select every row: don't also name them")
    if not (every or every_spare or slugs or spare_names):
        how = "the rows that end" if spec.named else "slugs, or write `all`"
        raise _Reject(f"no selection: name {how} ({spec.usage})")
    if every and not roster.slugs:  # the scripts would read no --only as everyone
        raise _Reject("this workshop's roster has no participants yet: nothing to select")
    if every_spare and not roster.spares:
        raise _Reject("no spares available in this workshop's spares.json: nothing to select")
    return Selection(every, every_spare, slugs, spare_names)


def _unexpected(word: str, spec: VerbSpec) -> _Reject:
    verb = spec.usage.split()[0]
    if SPARE_RE.fullmatch(word):
        why = f"{_show(word)} is not an available spare" if spec.spares else "no spares here"
    elif word in RESERVED or word.partition("=")[0] in _SETTINGS:
        why = f"{_show(word)} is not an option of {verb}"
    elif SLUG_RE.fullmatch(word) and spec.selection:
        return _missing_slug(word, f"no row {_show(word)} in this workshop's roster")
    else:
        why = f"{_show(word)} is not an argument of {verb}"
    return _Reject(f"{why} ({spec.usage})")


def _upload(content: str) -> str:
    found = set(_UPLOAD_LINK.findall(content))
    if len(found) != 1:
        raise _Reject("attach exactly one roster CSV to the !roster import message")
    path = found.pop()
    if not _UPLOAD_PATH.fullmatch(path):
        raise _Reject("attach a .csv whose name has only letters, digits, '.', '-' and '_'")
    return path


def _command(body: str, content: str, ctx: Context, dm: bool) -> Command:
    if body.startswith("roster import "):  # the compose box may put the CSV's link here
        body = _UPLOAD_MARKDOWN.sub("", body).rstrip(" ")
    quoted = _CHANNEL_QUOTED.findall(body) if body.startswith("new-workshop ") else []
    if len(quoted) > 1:
        raise _Reject("`channel=` is given twice")
    if quoted:  # the one value with spaces: read before the words, never split (#627)
        if channel_name(quoted[0]) is None:
            raise _Reject(f"`channel=` takes {CHANNEL_RULE}")
        body = _CHANNEL_QUOTED.sub("", body, count=1)
    words = body.split(" ")
    if not words[0]:
        raise _Reject("the command follows `!` directly, as in `!help`")
    if "" in words:
        raise _Reject("separate the words with single spaces")
    verb, args = words[0], words[1:]
    if verb in _GROUPS:
        subs = [key for key in SPECS if key.startswith(verb + " ")]
        if not args or f"{verb} {args[0]}" not in SPECS:
            raise _Reject("usage: " + "; ".join(SPECS[key].usage for key in subs))
        verb, args = f"{verb} {args[0]}", args[1:]
    elif verb in LATER:
        raise _Reject(f"`!{verb}` is not offered through the bots yet; a deployer runs it by hand")
    elif verb not in SPECS:
        raise _Reject(f"unknown command {_show('!' + verb)}: try !help")
    spec = SPECS[verb]
    if dm and not spec.dm:
        raise _Reject("only !help and !console work in a direct message; use the channel")
    if ctx.roster is None and not spec.server:
        raise _Reject("this is the server's bot: send workshop commands to the workshop's bot")
    if ctx.roster is not None and spec.only_global:
        raise _Reject(f"`!{verb}` is the server's: send it to the server's bot, in its channel")
    fields = _arguments(spec, args, ctx, ctx.roster or Roster())
    if quoted:
        if "channel" in fields:
            raise _Reject("`channel=` is given twice")
        fields["channel"] = quoted[0]
    required = [key for key in spec.settings if key not in spec.optional]
    if spec.only_global and any(_field(key) not in fields for key in required):
        raise _Reject(f"each setting but {', '.join(spec.optional)} is required: {spec.usage}")
    if verb == "roster import":
        fields["upload"] = _upload(content)
    return Command(verb, **fields)


#: What may stand between the bot's mention and the `!` of its command, in any number (#650):
#: spaces, no-break spaces (U+00A0, from a phone's keyboard or a paste) and tabs (a paste).
#: Zulip's autocomplete ends a mention with a space, and a sender types another; the rendered
#: message shows only a gap, and nothing tells a space from a no-break space or a tab. A
#: mention glued to its command (`@**…**!status`) still gets a hint, and so does any other
#: white-space character there (`str.isspace`: U+3000, U+2028, …). An invisible character that
#: isn't white space (U+200B, U+FEFF, …) leaves the message talk, which gets no answer: the
#: silent, safe side.
MENTION_GAP = " \u00a0\t"


def _after_mention(line: str, ctx: Context) -> str | None:
    """The rest of `line` after a leading mention of this bot, in either form, or None."""
    name, user_id = ctx.bot_full_name, ctx.bot_user_id
    for form in (f"@**{name}**", f"@**{name}|{user_id}**"):
        if line.startswith(form):
            return line[len(form) :]
    return None


def _looks_like_a_command(line: str, ctx: Context) -> bool:
    """A later line that would read as a command if it came first, whatever its indent."""
    line = line.lstrip()
    after = _after_mention(line, ctx)
    return line.startswith("!") or (after is not None and after.lstrip().startswith("!"))


#: A command formatted as code (#546): an inline code span, or a block fenced with backticks
#: whose info string is empty, `text` or `Text only`. Zulip renders a ```` ```text ```` block
#: with Pygments' name for its language, `data-code-language="Text only"`, and a paste of the
#: block's HTML writes that name after the fence (`web/src/compose_paste.ts`,
#: `get_code_block_language`). Any other info string (`quote`, `spoiler`, a language) is not one.
#: No two space quantifiers meet around an optional group, so a fence, many spaces and then
#: anything else is rejected in linear time (review of #547: the first form backtracked in
#: their square, 0.13 s at Zulip's 10,000 characters, minutes at 1 MB).
_CODE_SPAN = re.compile(r"(`+)([^`\n]+)\1")
_CODE_FENCE = re.compile(r"(`{3,}) *(?:text(?: +only)? *)?", re.IGNORECASE)
#: The verbs `code_wrapped` knows: the first word of each key of SPECS (not LATER's).
_VERBS = frozenset(key.split()[0] for key in SPECS)


def code_wrapped(content: str) -> bool:
    """Whether `content` is, whole, one command formatted as code (#546): one inline code span,
    or one fenced block (`_CODE_FENCE`) of one line, with only blank space around it, whose
    text starts with `!` and a verb of SPECS. The bot answers such a message with a hint, and
    nothing else.

    Only this bool leaves the function, never the text inside the code: `parse`, the one way to
    a `Command`, reads the message as it was sent, whose first line then starts with a
    backtick, and that is `NotCommand`. The check reads the verb alone, not the whole command,
    since nothing it accepts runs."""
    text = content.strip()
    span = _CODE_SPAN.fullmatch(text)
    if span is not None:
        inner = span[2]
    else:
        lines = [line.rstrip() for line in text.split("\n")]
        fence = _CODE_FENCE.fullmatch(lines[0])
        body = [line for line in lines[1:-1] if line.strip()]
        if fence is None or len(lines) < 3 or lines[-1] != fence[1] or len(body) != 1:
            return False
        inner = body[0]
    inner = inner.strip()
    return inner.startswith("!") and inner[1:].split(" ", 1)[0] in _VERBS


def parse(content: str, ctx: Context, *, dm: bool = False) -> Command | Rejected | NotCommand:
    """Parse a message's raw Markdown `content` (the event queue's, with `apply_markdown`
    false). `dm` is true for a direct message to the bot. `NotInRoster` is a `Rejected`."""
    first, _, later = content.partition("\n")
    line = first.rstrip(" ")
    if line.startswith("!"):
        body = line[1:]
    else:
        after = _after_mention(line, ctx)
        if after is None:
            return NotCommand("does not start with ! or a mention of this bot")
        if not after.lstrip().startswith("!"):
            return NotCommand("mentions this bot without a command")
        if after.startswith("!"):  # glued: `@**…**!status`, and `@**…**!!status` too
            return Rejected("put a space between the mention and the command")
        rest = after.lstrip(MENTION_GAP)
        if not rest.startswith("!"):  # white space outside MENTION_GAP
            return Rejected("use plain spaces between the mention and the command")
        body = rest[1:]
    if any(_looks_like_a_command(other, ctx) for other in later.split("\n")):
        return Rejected("one command per message: only the first line is read")
    try:
        return _command(body, content, ctx, dm)
    except _Reject as exc:
        return NotInRoster(str(exc), exc.slug) if exc.slug else Rejected(str(exc))
