"""The admin bots' grammar (docs/ADMIN.md §2.2, §2.3): table-driven, offline.

The parser is the security boundary of bots that hold the fleet's keys: whatever a message
holds, the result is a `Command` built only from looked-up values, or no command at all.
"""

import re
import time

import pytest

from zab.admin.grammar import (
    _CODE_FENCE,
    LATER,
    MENTION_GAP,
    RESERVED,
    SPECS,
    Command,
    Context,
    NotCommand,
    NotInRoster,
    Rejected,
    Roster,
    Selection,
    code_wrapped,
    parse,
)

PRODUCTION = re.compile(r"^v\d+\.\d+\.\d+$", re.ASCII)
ROSTER = Roster(slugs=("alice", "bob", "carol", "dave"), spares=("zab-spare-1", "zab-spare-2"))
CTX = Context(4242, "Workshop admin", PRODUCTION, ROSTER)
GLOBAL = Context(4000, "Server admin", PRODUCTION, None)
PATH = "/user_uploads/2/ab/xyzXYZ_-12/roster.csv"
UPLOAD = f"[roster.csv]({PATH})"
ALL = Selection(all=True)


def sel(*slugs: str, spares: tuple[str, ...] = ()) -> Selection:
    return Selection(slugs=slugs, spare_names=spares)


VALID = [
    ("!help", Command("help")),
    ("!help rollout", Command("help", topic="rollout")),
    ("!help rebind", Command("help", topic="rebind")),
    ("!version", Command("version")),
    ("!plans", Command("plans")),
    ("!plan P-7", Command("plan", plan=7)),
    ("!audit", Command("audit")),
    ("!audit 20", Command("audit", count=20)),
    ("!console", Command("console")),
    ("!status all", Command("status", selection=ALL)),
    ("!status carol alice", Command("status", selection=sel("alice", "carol"))),
    ("!roster show", Command("roster show")),
    ("!roster show bob", Command("roster show", slug="bob")),
    (f"!roster import\n{UPLOAD}", Command("roster import", upload=PATH)),
    (f"!roster import {UPLOAD}", Command("roster import", upload=PATH)),
    (
        "!provision zulip hold all",
        Command("provision zulip", selection=ALL, keywords=frozenset({"hold"})),
    ),
    ("!provision zulip bob", Command("provision zulip", selection=sel("bob"))),
    (
        "!provision repos invites-only all",
        Command("provision repos", selection=ALL, keywords=frozenset({"invites-only"})),
    ),
    (
        "!provision repos alice no-invite",
        Command("provision repos", selection=sel("alice"), keywords=frozenset({"no-invite"})),
    ),
    ("!provision repos all", Command("provision repos", selection=ALL)),
    (
        "!launch bob alice spares=2 protect",
        Command("launch", selection=sel("alice", "bob"), keywords=frozenset({"protect"}), spares=2),
    ),
    ("!launch all spares=0", Command("launch", selection=ALL, spares=0)),
    ("!protect on alice", Command("protect", on=True, selection=sel("alice"))),
    ("!protect off all", Command("protect", on=False, selection=ALL)),
    ("!waker on all", Command("waker", on=True, selection=ALL)),  # stage 4 (#358)
    ("!waker off bob alice", Command("waker", on=False, selection=sel("alice", "bob"))),
    ("!rollout v0.2.2 all", Command("rollout", version="v0.2.2", selection=ALL)),
    (
        "!rollout v0.2.3 bob steer=off lean-shared restart-codex idle-stop=15 multi-party=off "
        "raw-log=meta",
        Command(
            "rollout",
            version="v0.2.3",
            selection=sel("bob"),
            keywords=frozenset({"lean-shared", "restart-codex"}),
            steer=False,
            idle_stop=15,
            multi_party=False,
            raw_log="meta",
        ),
    ),
    (
        "!rollout v0.2.3 all steer=on",
        Command("rollout", version="v0.2.3", selection=ALL, steer=True),
    ),
    (
        "!rollout v10.20.30 all raw-log=off multi-party=on idle-stop=0",
        Command(
            "rollout",
            version="v10.20.30",
            selection=ALL,
            idle_stop=0,
            multi_party=True,
            raw_log="off",
        ),
    ),
    ("!idle-stop 0 all", Command("idle-stop", idle_stop=0, selection=ALL)),
    ("!idle-stop 10 alice", Command("idle-stop", idle_stop=10, selection=sel("alice"))),
    ("!idle-stop 240 alice", Command("idle-stop", idle_stop=240, selection=sel("alice"))),
    ("!start spares", Command("start", selection=Selection(spares=True))),
    ("!stop all spares", Command("stop", selection=Selection(all=True, spares=True))),
    (
        "!stop zab-spare-2 alice",
        Command("stop", selection=sel("alice", spares=("zab-spare-2",))),
    ),
    ("!start bob", Command("start", selection=sel("bob"))),
    ("!adopt-spare dave zab-spare-1", Command("adopt-spare", slug="dave", spare="zab-spare-1")),
    ("!grow-disk alice 300GB", Command("grow-disk", slug="alice", size=300)),
    ("!grow-disk bob 1gb", Command("grow-disk", slug="bob", size=1)),
    ("!grow-disk bob 1000GB", Command("grow-disk", slug="bob", size=1000)),
    ("!approve P-42 3f9a2c", Command("approve", plan=42, hash6="3f9a2c")),
    (
        "!approve P-42 3f9a2c stop 35",
        Command("approve", plan=42, hash6="3f9a2c", phrase=("stop", "35")),
    ),
    ("!reject P-3", Command("reject", plan=3)),
    # addressed with a mention of this bot, in either of Zulip's forms
    ("@**Workshop admin** !stop all", Command("stop", selection=ALL)),
    ("@**Workshop admin|4242** !approve P-42 3f9a2c", Command("approve", plan=42, hash6="3f9a2c")),
    # trailing spaces, and lines after the first, are not part of the command
    ("!status all   ", Command("status", selection=ALL)),
    ("!stop alice\nbecause she asked", Command("stop", selection=sel("alice"))),
    ("!stop alice\n--yes", Command("stop", selection=sel("alice"))),
]


@pytest.mark.parametrize(("message", "expected"), VALID, ids=[m for m, _ in VALID])
def test_valid_commands(message, expected):
    assert parse(message, CTX) == expected


@pytest.mark.parametrize(("message", "expected"), VALID, ids=[m for m, _ in VALID])
def test_text_round_trips(message, expected):
    """`text()` is the normalized command: it parses back to the same command."""
    text = expected.text() + (f"\n{UPLOAD}" if expected.verb == "roster import" else "")
    assert parse(text, CTX) == expected
    assert "\n" not in expected.text()


def test_normalized_tokens():
    command = parse("!rollout v0.2.3 carol alice raw-log=meta lean-shared idle-stop=30", CTX)
    assert command.tokens() == (
        *("v0.2.3", "alice", "carol", "lean-shared", "idle-stop=30", "raw-log=meta"),
    )
    assert command.text() == "!rollout v0.2.3 alice carol lean-shared idle-stop=30 raw-log=meta"
    command = parse("!rollout v0.2.3 all multi-party=on steer=off", CTX)
    assert command.text() == "!rollout v0.2.3 all steer=off multi-party=on"


def test_steering_follows_push_config():
    """#318: steering is on by default; `steer=off` is `--no-claude-steer`. No bare keyword."""
    assert parse("!rollout v0.2.3 all", CTX).steer is None
    assert parse("!rollout v0.2.3 all steer=off", CTX).steer is False
    assert isinstance(parse("!rollout v0.2.3 all steer", CTX), Rejected)
    assert isinstance(parse("!rollout v0.2.3 all steer=yes", CTX), Rejected)
    assert isinstance(parse("!rollout v0.2.3 all steer=on steer=off", CTX), Rejected)
    # a bare `steer` (§2.2's old form) is refused as an option, never read as a slug
    result = parse("!rollout v0.2.3 alice steer", CTX)
    assert isinstance(result, Rejected) and not isinstance(result, NotInRoster)
    assert "not an option" in result.reason


def test_classes():
    """§2.2's class column: what reads, what changes, what approves."""
    kinds = {verb: spec.kind for verb, spec in SPECS.items()}
    # `!month-end cancel`: the schedule's veto, as `!reject` is a plan's (#570)
    vetoes = {"approve", "reject", "month-end cancel"}
    assert {v for v, k in kinds.items() if k == "approval"} == vetoes
    assert {v for v, k in kinds.items() if k == "read"} == {
        "help", "version", "plans", "plan", "audit", "console", "status", "roster show",
        "month-end show",
    }  # fmt: skip
    assert parse("!stop all", CTX).spec.kind == "change"


# Hostile messages: whatever they hold, no command comes out.
INJECTION = [
    # shell metacharacters, inside a word or as one
    "!stop alice;bob",
    "!stop alice; rm -rf /",
    "!stop $(reboot)",
    "!stop alice$(id)",
    "!stop `id`",
    "!stop alice`id`",
    "!stop alice|id",
    "!stop alice&&id",
    "!stop alice>x",
    "!stop 'alice'",
    '!stop "alice"',
    "!stop alice\\",
    "!stop ~alice",
    "!stop alice*",
    "!stop ../alice",
    "!stop @**all**",
    # option injection: `--yes` and friends wherever a value goes
    "!stop --yes",
    "!stop -x",
    "!stop alice --yes",
    "!adopt-spare --yes zab-spare-1",
    "!adopt-spare alice --yes",
    "!grow-disk --yes 300GB",
    "!grow-disk alice --size=300",
    "!grow-disk alice 300GB;id",
    "!roster show --yes",
    "!roster import --yes",
    "!rollout --yes all",
    "!rollout v0.2.2 --yes",
    "!rollout v0.2.2 all --claude-raw-log=content",
    "!launch all spares=--yes",
    "!launch all spares=1;id",
    "!launch all --spares=1",
    "!idle-stop --yes all",
    "!idle-stop 15 all idle-stop=--yes",
    "!protect --off all",
    "!approve P-1 abcdef --yes",
    "!approve P-1 --yes",
    "!reject --yes",
    "!help --yes",
    # line breaks other than the first `\n`, and other control characters
    "!stop alice\rbob",
    "!stop alice\r",
    "!stop alice\u2028--yes",
    "!stop alice\u2029all",
    "!stop alice\x85x",
    "!stop alice\x0ball",
    "!stop alice\x0call",
    "!stop\tall",
    "!stop all\t",
    "!stop alice\x00",
    "!stop alice\x1b[2J",
    # Unicode look-alikes and invisible characters
    "!stop \u0430lice",  # Cyrillic a
    "!stop alic\u0435",  # Cyrillic e
    "!stop \uff41\uff4c\uff4c",  # fullwidth all
    "!stop all\u200b",  # zero-width space
    "!stop \u200ball",
    "!stop\u00a0all",  # no-break space
    "!stop al\u00adice",  # soft hyphen
    "!stop alice\u0301",  # combining accent
    "!stop zab\u2010spare-1",  # Unicode hyphen
    "!stop zab-spare-\uff11",  # fullwidth digit
    "!st\u043ep all",  # Cyrillic o in the verb
    "!rollout v\uff10.2.2 all",
    "!rollout v0.2.\u0663 all",  # Arabic-Indic digit
    "!idle-stop \u0663\u0660 all",
    "!idle-stop \U0001d7d1\U0001d7ce all",  # mathematical digits
    "!audit \u0662",
    "!approve \u0420-1 abcdef",  # Cyrillic P
    "!approve P-1 abcde\uff46",
    "!approve P-\u0661 abcdef",
    "!stop ALICE",
    "!Stop all",
    "!stop aLice",
    "!stop \u212aalice",  # Kelvin sign
    "\uff01stop all",  # fullwidth !
    "\u01c3stop all",  # Latin letter retroflex click, looks like !
    "@**Workshop adm\u0456n** !stop all",  # Cyrillic i in the bot's name
]


def _no_command(result):
    assert not isinstance(result, Command), result
    if isinstance(result, Rejected):
        assert "\n" not in result.reason and result.reason.isascii() and result.reason.isprintable()
        for fragment in ("$(", "`id`", "@**", "rm -rf", "\\", "'alice'", "\x1b"):
            assert fragment not in result.reason, result.reason


@pytest.mark.parametrize("message", INJECTION, ids=[repr(m) for m in INJECTION])
def test_injection_is_never_a_command(message):
    _no_command(parse(message, CTX))


PAYLOADS = [
    ";", "$(id)", "`id`", "|id", "&id", ">x", "'", '"', "\\", "*", "@**all**", "\r", "\t", "\x00",
    "\u2028", "\u200b", "\u00a0", "\u0430", "\uff41", "\u0663", "\u0301", "\u00ad", "\u2010",
]  # fmt: skip
SINGLE_LINE = [m for m, _ in VALID if m.startswith("!") and "\n" not in m and "](" not in m]


@pytest.mark.parametrize("payload", PAYLOADS, ids=[repr(p) for p in PAYLOADS])
def test_a_payload_anywhere_spoils_the_command(payload):
    """Appended to any word of any valid command, or inserted as a word of its own, every
    payload turns the message into a refusal."""
    for message in SINGLE_LINE:
        words = message.rstrip(" ").split(" ")
        for i in range(len(words)):
            spoiled = [*words[:i], words[i] + payload, *words[i + 1 :]]
            _no_command(parse(" ".join(spoiled), CTX))
            inserted = [*words[: i + 1], payload, *words[i + 1 :]]
            _no_command(parse(" ".join(inserted), CTX))
            inserted = [*words[: i + 1], "--yes", *words[i + 1 :]]
            assert isinstance(parse(" ".join(inserted), CTX), Rejected)


# Leading mentions: only this bot's own, in one of its two forms, addresses a command.
NOT_ADDRESSED = [
    "@**Claude · bob** !approve P-42 3f9a2c",  # a click on a participant bot's widget
    "@**Claude · bob|17** !approve P-42 3f9a2c",
    "@**Workshop admin|4243** !approve P-42 3f9a2c",  # this name, another user's ID
    "@**Workshop admin|04242** !stop all",
    "@**Workshop admin|** !stop all",
    "@**Workshop Admin** !stop all",
    "@**Workshop admin2** !stop all",
    "@**Workshop admin ** !stop all",
    "@_**Workshop admin** !stop all",  # silent
    "@_**Workshop admin|4242** !stop all",
    "@**all** !stop all",
    "@**everyone** !stop all",
    "@**channel** !stop all",
    "@**topic** !stop all",
    "@*organizers* !stop all",
    "@**Alice Organizer** @**Workshop admin** !stop all",
    "@**Workshop admin** @**Workshop admin** !stop all",
    "@**Workshop admin** please stop everything",
    "@**Workshop admin**",
    "@**Workshop admin** \n!stop all",
    " !stop all",
    "\t!stop all",
    "\u200b!stop all",
    "> !stop all",
    "**!stop all**",
    "`!stop all`",
    "```\n!stop all\n```",
    "hello\n!stop all",
    "stop all",
    "",
]


@pytest.mark.parametrize("message", NOT_ADDRESSED, ids=[repr(m) for m in NOT_ADDRESSED])
def test_only_a_mention_of_this_bot_addresses_it(message):
    assert isinstance(parse(message, CTX), NotCommand)


#: Between the bot's mention and the `!` (#650): Zulip's autocomplete ends the mention with a
#: space and the sender types another; a phone or a paste gives U+00A0; a paste, a tab.
GAPS = [" ", "  ", "   ", "\u00a0", " \u00a0", "\u00a0 ", "\t", " \t ", " \u00a0\t "]
MENTIONS = ["@**Workshop admin**", "@**Workshop admin|4242**"]
DM_ONLY = "only !help and !console work in a direct message; use the channel"


@pytest.mark.parametrize("dm", [False, True], ids=["channel", "dm"])
@pytest.mark.parametrize("mention", MENTIONS)
@pytest.mark.parametrize("gap", GAPS, ids=[repr(g) for g in GAPS])
def test_any_run_of_blanks_after_the_mention(gap, mention, dm):
    """Each reads as the command after the `!`, as one space does, in a channel and a DM."""
    assert parse(f"{mention}{gap}!help", CTX, dm=dm) == Command("help")
    assert parse(f"{mention}{gap}!console", CTX, dm=dm) == Command("console")
    stop = parse(f"{mention}{gap}!stop all", CTX, dm=dm)
    assert stop == parse("!stop all", CTX, dm=dm)  # a DM refuses it, as it refuses `!stop all`
    assert stop == (Rejected(DM_ONLY) if dm else Command("stop", selection=ALL))


@pytest.mark.parametrize("dm", [False, True], ids=["channel", "dm"])
@pytest.mark.parametrize("mention", MENTIONS)
def test_a_mention_glued_to_the_command_gets_the_hint(mention, dm):
    """Never `!stop all` read off the end of `@**Workshop admin**!!stop all`: the blanks are
    dropped, and the command is what follows the `!` after them, whole."""
    for message in (f"{mention}!help", f"{mention}!stop all", f"{mention}!!stop all"):
        result = parse(message, CTX, dm=dm)
        assert isinstance(result, Rejected), message
        assert result.reason == "put a space between the mention and the command"
    result = parse(f"{mention}  !!stop all", CTX, dm=dm)
    assert result == parse("!!stop all", CTX, dm=dm)  # the verb `!stop`, which doesn't exist
    assert isinstance(result, Rejected) and result.reason.startswith("unknown command")


#: Blanks outside `MENTION_GAP`: refused with a hint, not read, and never silence.
ODD_GAPS = ["\u3000", "\u2002", "\u202f", " \u2028", "\u00a0\u3000 ", "\x0b", "\x1f"]


@pytest.mark.parametrize("gap", ODD_GAPS, ids=[repr(g) for g in ODD_GAPS])
def test_other_blanks_after_the_mention_get_a_hint(gap):
    for mention in MENTIONS:
        result = parse(f"{mention}{gap}!stop all", CTX)
        assert isinstance(result, Rejected), repr(gap)
        assert result.reason == "use plain spaces between the mention and the command"


def test_the_gap_is_spaces_no_break_spaces_and_tabs():
    assert MENTION_GAP == " \u00a0\t"  # docs/ADMIN.md §2.2's `gap`


TALK_GAPS = [*GAPS, "", "\u200b"]


@pytest.mark.parametrize("gap", TALK_GAPS, ids=[repr(g) for g in TALK_GAPS])
def test_a_mention_and_blanks_without_a_command_is_talk(gap):
    for tail in ("", "please stop everything", "\u200b!stop all", "@**Workshop admin** !stop"):
        assert isinstance(parse(f"@**Workshop admin**{gap}{tail}", CTX), NotCommand), (gap, tail)


def test_a_duplicate_name_uses_the_id_form():
    """Zulip's web client adds `|<id>` when two users share a name (`get_mention_syntax`): a
    widget click on an impostor named like the bot carries the impostor's ID."""
    assert isinstance(parse("@**Workshop admin|999** !approve P-1 abcdef", CTX), NotCommand)
    assert isinstance(parse("@**Workshop admin|4242** !approve P-1 abcdef", CTX), Command)


# `all` is required for a fleet-wide action: an empty selection is an error.
NO_SELECTION = [
    "!status",
    "!stop",
    "!start",
    "!rollout v0.2.2",
    "!rollout v0.2.2 steer=on",
    "!launch",
    "!launch protect spares=1",
    "!provision zulip hold",
    "!provision repos invites-only",
    "!protect on",
    "!waker on",
    "!idle-stop 15",
]


@pytest.mark.parametrize("message", NO_SELECTION)
def test_all_is_required(message):
    result = parse(message, CTX)
    assert isinstance(result, Rejected) and "no selection" in result.reason


def test_all_means_this_workshops_participants_only():
    selection = parse("!stop all", CTX).selection
    assert selection.targets(ROSTER) == ("alice", "bob", "carol", "dave")  # never the spares
    assert parse("!stop spares", CTX).selection.targets(ROSTER) == ("zab-spare-1", "zab-spare-2")
    assert parse("!stop all spares", CTX).selection.targets(ROSTER) == (
        *ROSTER.slugs,
        *ROSTER.spares,
    )
    assert parse("!stop dave zab-spare-2 bob", CTX).selection.targets(ROSTER) == (
        "bob",
        "dave",
        "zab-spare-2",
    )


NO_SPARES = Context(4242, "Workshop admin", PRODUCTION, Roster(slugs=("alice", "bob")))
NO_ROWS = Context(4242, "Workshop admin", PRODUCTION, Roster())


@pytest.mark.parametrize(
    ("ctx", "message", "reason"),
    [
        (NO_SPARES, "!start spares", "no spares available"),
        (NO_SPARES, "!stop spares", "no spares available"),
        (NO_SPARES, "!stop all spares", "no spares available"),
        (NO_ROWS, "!stop all", "no participants yet"),
        (NO_ROWS, "!start all", "no participants yet"),
        (NO_ROWS, "!rollout v0.2.3 all", "no participants yet"),
        (NO_ROWS, "!status all", "no participants yet"),
        (NO_ROWS, "!launch all spares=2", "no participants yet"),
        (NO_ROWS, "!provision zulip hold all", "no participants yet"),
        (NO_ROWS, "!stop spares", "no spares available"),
    ],
)
def test_all_or_spares_that_select_nothing_are_refused(ctx, message, reason):
    """The scripts read a missing `--only` as every participant (`admin_common.select_rows`),
    so `all` on an empty roster, or `spares` with none left, must never become a command."""
    result = parse(message, ctx)
    assert isinstance(result, Rejected) and reason in result.reason, result


@pytest.mark.parametrize(
    "fields",
    [
        {"all": "false"},
        {"all": 1},
        {"spares": "no"},
        {"slugs": ["alice"]},
        {"slugs": ("alice", 2)},
        {"spare_names": "zab-spare-1"},
    ],
)
def test_a_selection_has_real_bools_and_tuples(fields):
    """A truthy "false", read back from a stored plan, must never mean `all` (#333)."""
    with pytest.raises(ValueError, match="two bools and two tuples"):
        Selection(**{"slugs": ("alice",), **fields})


def test_no_selection_resolves_to_nothing():
    with pytest.raises(ValueError):
        Selection()
    with pytest.raises(ValueError):
        Selection(all=True).targets(Roster())
    with pytest.raises(ValueError):
        Selection(spares=True).targets(Roster(slugs=("alice",)))
    with pytest.raises(ValueError):  # spares asked for, none there: not just the participants
        Selection(all=True, spares=True).targets(Roster(slugs=("alice",)))
    assert Selection(all=True).targets(Roster(slugs=("alice",))) == ("alice",)


def test_named_targets_are_the_rosters_own():
    """The roster may change between the parse and the plan: a name it lost is refused."""
    for selection in (sel("eve"), sel("alice", "eve"), sel(spares=("zab-spare-9",))):
        with pytest.raises(ValueError, match="not in this roster"):
            selection.targets(ROSTER)
    with pytest.raises(ValueError, match="not in this roster"):  # a spare adopted since
        sel("bob", spares=("zab-spare-2",)).targets(Roster(ROSTER.slugs, ("zab-spare-1",)))
    assert sel("bob", spares=("zab-spare-2",)).targets(ROSTER) == ("bob", "zab-spare-2")


def test_a_command_built_directly_is_as_safe_as_a_parsed_one():
    """A verb of SPECS, and a selection exactly when the verb takes one (#323's re-review)."""
    for verb in ("nonverb", "rebind", "", ["stop"]):
        with pytest.raises(ValueError, match="not a verb"):
            Command(verb)
    for verb, spec in SPECS.items():
        if spec.selection:
            with pytest.raises(ValueError, match="selection exactly when"):
                Command(verb)
            assert Command(verb, selection=ALL).selection == ALL
        else:
            with pytest.raises(ValueError, match="selection exactly when"):
                Command(verb, selection=ALL)
            assert Command(verb).verb == verb


# Unknown, repeated or contradictory words reject the whole message.
REJECTED = [
    "!foo",
    "!stopp all",
    "!",
    "! stop all",
    "!stop  alice",
    "!stop alice  bob",
    "!status all please",
    "!version now",
    "!plans all",
    "!console alice",
    "!roster",
    "!roster delete",
    "!roster show alice bob",
    "!provision",
    "!provision github all",
    "!rollout v0.2.2 all turbo",
    "!rollout v0.2.2 all hold",  # a keyword of another verb
    "!launch all lean-shared",
    "!provision zulip invites-only all",
    "!stop all protect",
    "!status all spares",  # spares only where the verb allows them
    "!status zab-spare-1",
    "!launch zab-spare-1",
    "!stop zab-spare-3",  # not an available spare
    "!stop zab-spare-01",
    "!rollout v0.2.2 all idle-stop=5",
    "!rollout v0.2.2 all idle-stop=241",
    "!rollout v0.2.2 all idle-stop=010",
    "!rollout v0.2.2 all idle-stop=",
    "!rollout v0.2.2 all raw-log=content",  # left out on purpose (§2.2)
    "!rollout v0.2.2 all multi-party=yes",
    "!rollout v0.2.2 all spares=1",
    "!launch all spares=11",
    "!idle-stop 9 all",
    "!idle-stop all 15",
    "!audit 0",
    "!audit 51",
    "!audit 1 2",
    "!plan 42",
    "!plan P-0",
    "!plan P-042",
    "!plan p-42",
    "!approve P-42 3F9A2C",
    "!approve P-42 3f9a2",
    "!approve P-42",
    "!approve P-42 3f9a2c stop 35 more words",
    "!reject 42",
    "!adopt-spare alice",
    "!adopt-spare alice zab-spare-3",
    "!adopt-spare alice zab-spare-1 bob",
    # !grow-disk (#553): one row, a size in GB up to MAX_DISK_GB, nothing more
    "!grow-disk alice",
    "!grow-disk 300GB",
    "!grow-disk alice 300",
    "!grow-disk alice 300G",
    "!grow-disk alice 300GiB",
    "!grow-disk alice 300Gb",
    "!grow-disk alice 0GB",
    "!grow-disk alice 0300GB",
    "!grow-disk alice 1001GB",
    "!grow-disk alice 99999GB",
    "!grow-disk alice +300GB",
    "!grow-disk alice 300GB bob",
    "!grow-disk all 300GB",
    "!grow-disk zab-spare-1 300GB",
    "!grow-disk alice 300GB 400GB",
    "!help foo",
    "!help rollout status",
    "!rollout v0.2 all",
    "!rollout 0.2.2 all",
    "!rollout v0.2.2rc1 all",  # the production pattern takes no rc
    "!rollout main all",
    "!rebind alice",
    "!upgrade-server v0.3.0",
    "!new-workshop x",
    # repeated or contradictory
    "!stop alice alice",
    "!stop all alice",
    "!stop all all",
    "!stop spares zab-spare-1",
    "!stop spares spares",
    "!rollout v0.2.2 all steer=on steer=on",
    "!rollout v0.2.2 all steer=on steer=off",
    "!rollout v0.2.2 all idle-stop=15 idle-stop=30",
    "!rollout v0.2.2 all multi-party=on multi-party=off",
    "!provision repos invites-only no-invite all",
    "!launch all spares=1 spares=2",
    "!launch all protect protect",
    # one command per message, whatever the later line's indent or address
    "!stop alice\n!stop bob",
    "!stop alice\n  !stop all",
    "!stop alice\n\t!stop bob",
    "!stop alice\n\r!stop bob",
    "!stop alice\n\u00a0!stop bob",
    "!stop alice\n@**Workshop admin** !stop bob",
    "!stop alice\n@**Workshop admin|4242** !stop bob",
    "!stop alice\n  @**Workshop admin**  !stop bob",
    "@**Workshop admin** !stop alice\n!approve P-1 abcdef",
]


@pytest.mark.parametrize("message", REJECTED, ids=[repr(m) for m in REJECTED])
def test_rejected(message):
    result = parse(message, CTX)
    assert isinstance(result, Rejected), result  # NotInRoster too, for a slug-shaped word
    _no_command(result)


def test_an_unknown_word_rejects_even_with_valid_ones():
    result = parse("!stop alice bob eve", CTX)
    assert isinstance(result, Rejected)
    assert "eve" in result.reason


def test_later_verbs_say_so():
    for verb in sorted(LATER):
        result = parse(f"!{verb}", CTX)
        assert isinstance(result, Rejected) and "not offered" in result.reason


def test_unsafe_words_are_described_not_echoed():
    result = parse("!stop al\u0456ce", CTX)
    assert isinstance(result, Rejected)
    assert "\u0456" not in result.reason and "characters outside" in result.reason
    assert "`--yes`" in parse("!stop --yes", CTX).reason


# A slug this workshop lacks may be another's: the bot can point to that workshop's bot.
@pytest.mark.parametrize(
    ("message", "slug"),
    [
        ("!stop eve", "eve"),
        ("!stop alice eve", "eve"),
        ("!rollout v0.2.2 eve steer=on", "eve"),
        ("!status frank", "frank"),
        ("!roster show eve", "eve"),
        ("!adopt-spare eve zab-spare-1", "eve"),
        ("!grow-disk eve 300GB", "eve"),
        ("@**Workshop admin** !start eve", "eve"),
    ],
)
def test_a_slug_of_another_workshop(message, slug):
    result = parse(message, CTX)
    assert isinstance(result, NotInRoster) and isinstance(result, Rejected)
    assert result.slug == slug


@pytest.mark.parametrize(
    "message",
    [
        "!stop zab-spare-7",
        "!stop hold",
        "!stop alice.b",
        "!stop --eve",
        "!stop Eve",
        "!version eve",
        "!roster show all",  # a reserved word where a slug goes
        "!roster show steer",
        "!adopt-spare zab-spare-1 zab-spare-1",  # a spare where a slug goes
    ],
)
def test_not_every_unknown_word_is_a_slug(message):
    """Spares, keywords, malformed words and words where no slug goes get a plain refusal."""
    result = parse(message, CTX)
    assert isinstance(result, Rejected) and not isinstance(result, NotInRoster), result


def test_the_global_bot_takes_server_verbs_only():
    for message in ("!version", "!audit 5", "!help", "!plans", "!plan P-1", "!reject P-1"):
        assert isinstance(parse(message, GLOBAL), Command), message
    assert isinstance(parse("!approve P-1 abcdef", GLOBAL), Command)
    for message in ("!stop all", "!status all", "!roster show", f"!roster import {UPLOAD}"):
        result = parse(message, GLOBAL)
        assert isinstance(result, Rejected) and "server's bot" in result.reason, message
    result = parse("!stop alice", GLOBAL)
    assert type(result) is Rejected  # no roster: not a redirect


@pytest.mark.parametrize(
    "phrase, ok",
    [
        ("new-workshop " + "a" * 47, True),  # §2.9: a workshop's name, up to 47 characters
        ("new-workshop mit-2027", True),
        ("new-workshop " + "a" * 48, False),
        ("stop " + "a" * 33, False),  # elsewhere, phrase words are slugs
        ("new-workshop " + "a" * 33 + " x", False),
        ("rebind " + "a" * 33, False),
        ("new-workshop -mit", False),
    ],
)
def test_the_new_workshop_phrase_takes_a_workshop_name(phrase, ok):
    result = parse(f"!approve P-7 3f9a2c {phrase}", GLOBAL)
    if ok:
        assert result == Command("approve", plan=7, hash6="3f9a2c", phrase=tuple(phrase.split()))
    else:
        assert type(result) is Rejected and "phrase" in result.reason


NEW = "!new-workshop mit-2027 region=us-east-2 like=harvard-2026 organizers=105,106"


def test_new_workshop_parses_on_the_global_bot_only():
    """§2.2, §2.9 (#418): a name, then three settings, all required, in any order; one word of
    organizers; on the server's bot, never a workshop's."""
    want = Command(
        "new-workshop", ws_name="mit-2027", region="us-east-2", like="harvard-2026",
        organizers=(105, 106),
    )  # fmt: skip
    assert parse(NEW, GLOBAL) == want
    shuffled = "!new-workshop mit-2027 organizers=105,106 like=harvard-2026 region=us-east-2"
    assert parse(shuffled, GLOBAL) == want
    assert want.text() == NEW
    result = parse(NEW, CTX)
    assert type(result) is Rejected and "server's bot" in result.reason
    assert parse(NEW, GLOBAL, dm=True) == Rejected(
        "only !help and !console work in a direct message; use the channel"
    )


CAP_PLUS_ONE = ",".join(str(n) for n in range(101, 122))  # 21 distinct IDs


@pytest.mark.parametrize(
    ("message", "why"),
    [
        ("!new-workshop mit-2027 region=us-east-2 like=harvard-2026", "required"),
        ("!new-workshop mit-2027", "required"),
        ("!new-workshop region=us-east-2", "a new workshop's name"),
        ("!new-workshop " + "a" * 48 + " region=us-east-2 like=x organizers=1", "name"),
        ("!new-workshop Mit region=us-east-2 like=x organizers=1", "name"),
        ("!new-workshop mit region=US-east-2 like=x organizers=1", "region"),
        ("!new-workshop mit region=us-east-2 like=X organizers=1", "like="),
        ("!new-workshop mit region=us-east-2 like=x organizers=1,1", "each once"),
        ("!new-workshop mit region=us-east-2 like=x organizers=0", "user IDs"),
        ("!new-workshop mit region=us-east-2 like=x organizers=1,", "user IDs"),
        ("!new-workshop mit region=us-east-2 like=x organizers=" + CAP_PLUS_ONE, "at most 20"),
        ("!new-workshop mit region=us-east-2 region=us-west-2 like=x organizers=1", "twice"),
        ("!new-workshop mit region=us-east-2 like=x organizers=1 hold", "not an option"),
        ("!new-workshop mit extra region=us-east-2 like=x organizers=1", "not an argument"),
    ],
)
def test_a_malformed_new_workshop_is_rejected(message, why):
    result = parse(message, GLOBAL)
    assert type(result) is Rejected and why in result.reason, result


def test_twenty_organizers_are_taken_and_twenty_one_are_not():
    """Review of #424: the cap itself, with distinct IDs (the repeats are refused for being
    repeats, `each once`)."""
    cap = ",".join(str(n) for n in range(101, 121))
    result = parse(f"!new-workshop mit region=us-east-2 like=x organizers={cap}", GLOBAL)
    assert type(result) is Command and result.organizers == tuple(range(101, 121))
    result = parse(f"!new-workshop mit region=us-east-2 like=x organizers={CAP_PLUS_ONE}", GLOBAL)
    assert type(result) is Rejected and "at most 20" in result.reason


@pytest.mark.parametrize(
    ("words", "channel"),
    [
        ('channel="Harvard Halloween"', "Harvard Halloween"),
        ("channel=harvard-2026", "harvard-2026"),  # one word may go unquoted
        ('channel="Zürich: Ἀθῆναι & 東京 (2027)"', "Zürich: Ἀθῆναι & 東京 (2027)"),
        ('channel="' + "x" * 60 + '"', "x" * 60),
    ],
)
def test_new_workshop_takes_an_optional_channel_in_quotes(words, channel):
    """#627: the participants' channel, by its Zulip name, which may hold spaces and capitals:
    the one value in quotes, read before the words; anywhere among the settings. `text()`
    always quotes it, so that the plan's command parses back at its apply."""
    want = Command(
        "new-workshop", ws_name="mit-2027", region="us-east-2", like="harvard-2026",
        organizers=(105, 106), channel=channel,
    )  # fmt: skip
    assert parse(f"{NEW} {words}", GLOBAL) == want
    first = f"!new-workshop mit-2027 {words} region=us-east-2 like=harvard-2026 organizers=105,106"
    assert parse(first, GLOBAL) == want
    assert want.text() == f'{NEW} channel="{channel}"'
    assert parse(want.text(), GLOBAL) == want


@pytest.mark.parametrize(
    ("words", "why"),
    [
        ('channel=""', "channel's name in double quotes"),
        ('channel=" Harvard"', "channel's name"),
        ('channel="Harvard  Halloween"', "channel's name"),
        ('channel="' + "x" * 61 + '"', "channel's name"),
        ('channel="@**all**"', "channel's name"),  # nothing Zulip's Markdown reads
        ('channel="a`b"', "channel's name"),
        ('channel="a#b"', "channel's name"),
        ('channel="a' + chr(0x200B) + 'b"', "channel's name"),  # a format character
        ('channel="Harvard Halloween', "channel's name"),  # no closing quote: two words
        ('channel="a" channel="b"', "twice"),
        ('channel="a b" channel=c', "twice"),
        ("channel=a channel=b", "twice"),
    ],
)
def test_a_malformed_channel_is_rejected_without_echoing_it(words, why):
    result = parse(f"{NEW} {words}", GLOBAL)
    assert type(result) is Rejected and why in result.reason, result
    assert "Harvard" not in result.reason and "**" not in result.reason


@pytest.mark.parametrize(
    "quoted", ["\u201cHarvard Halloween\u201d", "\u201dHarvard Halloween\u201d",
               "\u201cHarvard Halloween\u201c", '"Harvard Halloween\u201d']
)  # fmt: skip
def test_a_phones_curly_quotes_are_taken_and_written_back_straight(quoted):
    """Review round 1 of #638, and its approval (6397329425, A): iOS's smart punctuation, on by
    default, picks an opening or a closing quote from the character before it, so either end
    may be any of `"`, `\u201c` and `\u201d`; `text()` writes them back straight."""
    result = parse(f"{NEW} channel={quoted}", GLOBAL)
    assert type(result) is Command and result.channel == "Harvard Halloween"
    assert result.text() == f'{NEW} channel="Harvard Halloween"'
    assert parse(result.text(), GLOBAL) == result


def test_a_quoted_channel_with_more_after_it_is_refused():
    for bad in ("\u201cHarvard\u201d x", "\u201cHar\u201dvard\u201d", "\u201cHarvard"):
        assert type(parse(f"{NEW} channel={bad}", GLOBAL)) is Rejected, bad


def test_a_channel_is_new_workshops_alone():
    ctx = Context(4242, "Workshop admin", PRODUCTION, Roster(slugs=("alice",)))
    result = parse('!status alice channel="Harvard Halloween"', ctx)
    assert type(result) is Rejected


def test_new_workshop_settings_reserve_no_slug():
    """`region`, `like`, `organizers` and `channel` stand only in `!new-workshop`, which takes
    no slug: a roster may hold such a slug (roster_import.py keeps a copy of RESERVED)."""
    assert not {"region", "like", "organizers", "channel"} & RESERVED
    ctx = Context(4242, "Workshop admin", PRODUCTION, Roster(slugs=("region", "like")))
    assert parse("!stop region like", ctx) == Command("stop", selection=sel("region", "like"))


def test_direct_messages_take_help_and_console_only():
    assert parse("!help", CTX, dm=True) == Command("help")
    assert parse("!console", CTX, dm=True) == Command("console")
    for message in ("!stop all", "!approve P-1 abcdef", "!status all", "!version"):
        result = parse(message, CTX, dm=True)
        assert isinstance(result, Rejected) and "direct message" in result.reason


def test_release_pattern_decides_on_release_candidates():
    rc = Context(4242, "Workshop admin", re.compile(r"^v\d+\.\d+\.\d+(rc\d+)?$"), ROSTER)
    assert parse("!rollout v0.3.0rc1 all", rc).version == "v0.3.0rc1"
    loose = Context(4242, "Workshop admin", re.compile(r".*"), ROSTER)
    for message in ("!rollout --yes all", "!rollout v0.2.2;id all", "!rollout main all"):
        assert isinstance(parse(message, loose), Rejected)


@pytest.mark.parametrize(
    "content",
    [
        "!roster import",
        f"!roster import {UPLOAD} [other.csv](/user_uploads/2/cd/efgh/other.csv)",
        "!roster import [r.csv](/user_uploads/2/ab/x/../../secret.csv)",
        "!roster import [r.csv](/user_uploads/2/ab/xyz/roster.txt)",
        "!roster import [r.csv](/user_uploads/2/ab/xyz/r\u00f4ster.csv)",
        "!roster import [r.csv](/user_uploads/2/ab/xyz/.csv)",
        "!roster import [r.csv](https://evil.example/user_uploads/2/ab/xyz/roster.csv)",
        "!roster import /user_uploads/2/ab/xyz/roster.csv",
    ],
)
def test_roster_import_needs_one_plain_csv_upload(content):
    assert isinstance(parse(content, CTX), Rejected)


def test_the_same_upload_linked_twice_is_one():
    assert parse(f"!roster import {UPLOAD}\n{UPLOAD}", CTX).upload == PATH


def test_the_upload_link_may_follow_on_the_first_line():
    """Zulip's compose box puts the link at the cursor; it is not a word of the command."""
    spaced = "[my roster.csv](/user_uploads/2/ab/xyz/my-roster.csv)"
    assert parse(f"!roster import {spaced}", CTX).upload == "/user_uploads/2/ab/xyz/my-roster.csv"
    assert isinstance(parse(f"!roster import {UPLOAD} --yes", CTX), Rejected)
    assert isinstance(parse(f"!roster import --yes {UPLOAD}", CTX), Rejected)
    assert isinstance(parse(f"!stop all {UPLOAD}", CTX), Rejected)  # only !roster import


@pytest.mark.parametrize(
    "roster",
    [
        {"slugs": ("all",)},
        {"slugs": ("spares",)},
        {"slugs": ("protect",)},
        {"slugs": ("steer",)},  # setting names too: `!rollout \u2026 alice steer` can't sweep it in
        {"slugs": ("idle-stop",)},
        {"slugs": ("multi-party",)},
        {"slugs": ("raw-log",)},
        {"slugs": ("hold",)},
        {"slugs": ("zab-spare-1",)},
        {"slugs": ("Alice",)},
        {"slugs": ("a;b",)},
        {"slugs": ("--yes",)},
        {"slugs": ("",)},
        {"slugs": ("alice", "alice")},
        {"spares": ("spare-1",)},
        {"spares": ("zab-spare-1", "zab-spare-1")},
    ],
)
def test_a_roster_the_grammar_cant_address_is_refused(roster):
    with pytest.raises(ValueError):
        Roster(**roster)


@pytest.mark.parametrize(
    ("user_id", "name"),
    [
        (0, "Workshop admin"),
        (-1, "Workshop admin"),
        (True, "Workshop admin"),
        ("4242", "W"),
        (4242, ""),
        (4242, "a*b"),
        (4242, "a|b"),
        (4242, "a\nb"),
    ],
)
def test_a_bad_bot_identity_is_refused(user_id, name):
    with pytest.raises(ValueError):
        Context(user_id, name, PRODUCTION, ROSTER)


def test_reserved_words_cover_every_keyword_and_setting():
    assert {"all", "spares", "hold", "protect", "lean-shared", "restart-codex"} <= RESERVED
    assert {"steer", "idle-stop", "multi-party", "raw-log"} <= RESERVED
    slugs_too = [spec for spec in SPECS.values() if not spec.only_global]  # !new-workshop: none
    assert all(set(spec.keywords + spec.settings) <= RESERVED for spec in slugs_too)


# ------------------------------------------------------------------ a command sent as code (#546)

#: A whole message that is one command formatted as code: the bot's hint, never a command.
AS_CODE = [
    "`!stop bob`",  # a copied code span, or a one-line code block, as Zulip's paste writes it
    "``!stop bob``",
    "`!approve P-42 3f9a2c stop 3`",
    "  `!help`  \n",  # blank space around it
    "` !status all `",  # CommonMark drops one space on each side
    "```\n!stop all\n```",
    "```text\n!approve P-42 3f9a2c stop 3\n```",  # #544's approval line, fenced by hand
    "```Text only\n!approve P-42 3f9a2c stop 3\n```",  # that block pasted with its newline
    "```TEXT\n!stop all\n```",  # Zulip reads the language in any case
    "````\n!stop all\n````",  # a longer fence
    "```\n\n!roster show\n\n```",  # blank lines inside
    "\n```text \n  !help  \n```\n",
    "```!stop bob```",  # one line: a code span, not a fence
    "`!roster import`",  # a verb group
    "`!new-workshop mit-2027`",  # the global bot's
    "`!stop alice;rm -rf /`",  # the verb decides: nothing inside is ever parsed or run
]
#: Code that is not one command: no answer at all.
NOT_AS_CODE = [
    "`ls`",  # a quoted snippet
    "`!login`",  # a participant bot's command
    "`!rebind bob`",  # a later verb (LATER), not offered through the bots
    "`!Stop all`",
    "`!stopall`",
    "`! stop all`",
    "`!`",
    "`stop all`",
    "please send `!stop bob`",  # a backticked command inside a sentence
    "`!stop bob` please",
    "`!stop` `bob`",
    "> `!stop all`",  # a quote
    "```quote\n!stop all\n```",
    "```spoiler P-42\n!stop all\n```",
    "```python\n!stop all\n```",
    "```\n!stop all\n!stop bob\n```",  # a block of several lines
    "```\n!stop all\nthen wait\n```",
    "```\n!stop all",  # never closed
    "```\n!stop all\n````",  # closed by another fence
    "```\n!stop all\n```\nand then?",  # more after it
    "```\n```",
    "`!stop\nbob`",
    "``",
    "` `",
    "",
    "!stop bob",  # a command: `parse`'s, not this check's
    "@**Workshop admin** `!stop bob`",
]


@pytest.mark.parametrize("message", AS_CODE, ids=[repr(m) for m in AS_CODE])
def test_a_command_sent_as_code_is_recognized_and_is_no_command(message):
    """The hint's check says yes, and the grammar still finds no command in the message as
    sent: the bot answers it, and nothing runs."""
    assert code_wrapped(message) is True
    for ctx in (CTX, GLOBAL):
        for dm in (False, True):
            assert isinstance(parse(message, ctx, dm=dm), NotCommand)


@pytest.mark.parametrize("message", NOT_AS_CODE, ids=[repr(m) for m in NOT_AS_CODE])
def test_other_code_is_not_a_command_sent_as_code(message):
    assert code_wrapped(message) is False


def test_every_verb_sent_as_code_is_recognized():
    for key in SPECS:
        assert code_wrapped(f"`!{key}`") and code_wrapped(f"```\n!{key}\n```"), key
    assert not any(code_wrapped(f"`!{verb}`") for verb in LATER)


#: A fence line, and whether it opens a block `code_wrapped` reads (#546): the info string
#: empty, `text` or `Text only`, in any case, with any spaces around and between.
FENCES = [
    ("```", True),
    ("````", True),
    ("```text", True),
    ("````text", True),
    ("```Text only", True),
    ("```TEXT  ONLY", True),
    ("```TeXt OnLy", True),
    ("``` text ", True),
    ("```text ", True),
    ("```text  ", True),
    ("```   ", True),
    ("```  Text   only  ", True),
    ("```textonly", False),
    ("```text only extra", False),
    ("```text text", False),
    ("```text\tonly", False),
    ("```only", False),
    ("``` only", False),
    ("```quote", False),
    ("```spoiler", False),
    ("```python", False),
    ("``` x", False),
    ("``", False),
    ("~~~text", False),
]
#: The fence's first form, quadratic on a fence, many spaces and anything else (review of
#: #547): the linear one must read every line of FENCES as it did.
FIRST_FENCE = re.compile(r"(`{3,}) *(?:text|text +only)? *", re.IGNORECASE)


@pytest.mark.parametrize(("line", "opens"), FENCES, ids=[repr(line) for line, _ in FENCES])
def test_the_fences_a_command_sent_as_code_may_have(line, opens):
    found, first = _CODE_FENCE.fullmatch(line), FIRST_FENCE.fullmatch(line)
    assert (found is not None) is opens is (first is not None)
    run = re.match("`*", line)[0]  # the fence, which closes the block too
    if opens:
        assert found[1] == first[1] == run
    assert code_wrapped(f"{line}\n!stop all\n{run}") is opens


@pytest.mark.timing
def test_a_long_fence_line_is_read_in_linear_time():
    """A fence, many spaces, then something else: the first form of `_CODE_FENCE` tried every
    split of the spaces between its two quantifiers (review of #547), 0.13 to 0.17 s at
    10,000 spaces, Zulip's longest message, and about 23 minutes at 1 MB. Now 0.2 ms and 23 ms
    here. The 1-MB line is timed only once the 10,000 spaces pass, so a regression fails in a
    second rather than hang the job. The bounds are absolute, a hundred and twenty times and
    twenty times what this takes here, as CI's runners are slower: CPU time, the least of three
    after a first untimed pass (#508)."""

    def cpu(n: int) -> float:
        text = "```" + " " * n + "x\n!stop all\n```"
        assert not code_wrapped(text)  # untimed: a fresh process's first call runs slow
        times = []
        for _ in range(3):
            start = time.process_time()
            code_wrapped(text)
            times.append(time.process_time() - start)
        return min(times)

    assert cpu(10_000) < 0.025, "quadratic: Zulip's 10,000 characters took this long"
    assert cpu(1_000_000) < 0.5
