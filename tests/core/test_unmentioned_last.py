"""The rule `last` for a message that mentions no bot (#878, SPEC §4.5): it goes to the bots the
topic's newest message with a mention named, and to all of them where none has been named.
`all` (#713) is the default.

The routers share one store, as the VM's bridges do (`zab run`); `route` computes the routers'
`addressees` and `siblings` as `BotBridge.route_message` does, sign-ins aside, which the bridge's
own tests below cover."""

import time

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_fork import BOTS
from test_fork import make_pair as make_pair
from test_login import dm
from test_unmentioned import message, other_bot, session_of

from zab.config import ConfigError, load_config
from zab.core import unmentioned
from zab.core.catchup import EXPIRED, Catchup
from zab.core.commands import HELP
from zab.core.router import UNMENTIONED_DM, Router
from zab.core.store import Store
from zab.core.unmentioned import addressees, follow, record, rule

pytestmark = pytest.mark.integration
NAMES = ["Codex · X", "Claude · X"]
KINDS = ["codex", "claude"]
OWNER = 20
CODEX, CLAUDE = (f"@**{name}**" for name in NAMES)


@pytest.fixture
def pair(tmp_path):
    store = Store(tmp_path)
    routers = [
        Router(store, str(i), i + 10, NAMES[i], frozenset({OWNER}), kind=KINDS[i]) for i in range(2)
    ]
    yield store, routers
    store.close()


def route(routers, mid, text, *, last=True, topic="topic", multi_party=False, order=(0, 1), **kw):
    """Each bot's route of one message, in `order`, under `last` or `all`."""
    store = routers[0].store
    msg = dict(id=mid, sender_id=OWNER, stream_id=1, subject=topic, content=text, type="stream")
    results = {}
    for i in order:
        other = NAMES[1 - i]
        unnamed = routers[i].unmentioned(text, {other, "Claude · Y"})
        named = addressees(store, 1, topic, mid) if unnamed and last else ()
        siblings = (other,) if unnamed and not multi_party else None
        if named and siblings is not None:
            siblings = tuple(n for n in siblings if n in named) if NAMES[i] in named else None
        results[i] = routers[i].route(
            msg,
            flags=["mentioned"] if f"@**{NAMES[i]}**" in text else [],
            sender_is_bot=False,
            private=kw.get("private", True),
            other_bot_names={other, "Claude · Y"},  # another participant's Claude
            multi_party=multi_party,
            siblings=siblings,
            sibling_kinds={other: KINDS[1 - i]},
            addressees=named or None,
        )
    return [results.get(0), results.get(1)]


def bind(store, i, card, topic="topic"):
    store.insert(
        "sessions", id=f"s{i}-{card}", bot=str(i), backend=str(i), native_id=f"n{i}-{card}",
        card_message_id=card, stream_id=1, topic=topic, workspace="/repo",
        workspace_mode="shared", repo_root="/repo", settings_json="{}", state="idle",
        shared=0, created_at=0, updated_at=0,
    )  # fmt: skip


def actions(result):
    return [r.action for r in result]


@pytest.mark.parametrize("last", [True, False])
def test_a_mention_of_one_bot_then_a_message_without_one(pair, last):
    store, routers = pair
    assert actions(route(routers, 1, f"{CLAUDE} prove it", last=last)) == ["ignore", "new"]
    assert addressees(store, 1, "topic") == (NAMES[1],)  # recorded under both rules
    bind(store, 1, 2)
    follow_up = route(routers, 3, "and the converse?", last=last)
    if last:
        assert actions(follow_up) == ["ignore", "deliver"]
        assert (follow_up[1].peers, follow_up[1].quiet) == ((), False)  # as if Claude was named
        audience = store.get("message_audience:0:3")  # Codex's: addressed to its peer (R3)
        assert (audience["addressed"], audience["peers"]) == (False, [NAMES[1]])
    else:
        assert actions(follow_up) == ["new", "deliver"]


def test_a_mention_of_both_then_one_then_none(pair):
    store, routers = pair
    assert actions(route(routers, 1, f"{CODEX} {CLAUDE} compare")) == ["new", "new"]
    bind(store, 0, 2)
    bind(store, 1, 3)
    both = route(routers, 4, "go on")
    assert actions(both) == ["deliver", "deliver"]
    assert [(r.peers, r.quiet) for r in both] == [((NAMES[1],), True), ((NAMES[0],), True)]
    assert store.get("addressed_peers:0:4") == [NAMES[1]]  # the co-addressed note
    assert actions(route(routers, 5, f"{CODEX} you")) == ["deliver", "ignore"]
    assert actions(route(routers, 6, "more")) == ["deliver", "ignore"]
    assert actions(route(routers, 7, "// an aside, recording nothing")) == ["ignore", "ignore"]
    assert actions(route(routers, 8, "still you")) == ["deliver", "ignore"]


@pytest.mark.parametrize("typed,expected", [("@claude", "Claude · X"), ("@Codex", "Codex · X")])
def test_a_typed_mention_records_its_bot(pair, typed, expected):
    store, routers = pair
    bind(store, 0, 1)
    bind(store, 1, 2)
    route(routers, 3, f"{typed} look at this")
    assert addressees(store, 1, "topic") == (expected,)
    result = route(routers, 4, "and this")
    assert actions(result) == ["deliver" if name == expected else "ignore" for name in NAMES]


def test_without_a_record_it_goes_to_all(pair):
    """No bot of the VM named in the topic yet: a new topic's first message (#687), and one
    after a mention of another participant's bot only, which records nothing."""
    store, routers = pair
    assert actions(route(routers, 1, "hello")) == ["new", "new"]
    assert addressees(store, 1, "topic") == ()
    assert actions(route(routers, 2, "@**Claude · Y** yours?", topic="t")) == ["ignore"] * 2
    assert addressees(store, 1, "t") == ()
    assert actions(route(routers, 3, "anyone?", topic="t")) == ["new", "new"]


def test_a_command_changes_no_record(pair):
    """Whom the participant talks to, not whom they send a command: `@codex !usage` in Claude's
    topic leaves the follow-ups with Claude; `!new` with a prompt is a prompt."""
    store, routers = pair
    bind(store, 0, 1)
    bind(store, 1, 2)
    route(routers, 3, f"{CLAUDE} work")
    assert actions(route(routers, 4, f"{CODEX} !usage")) == ["command", "ignore"]
    assert actions(route(routers, 5, f"{CODEX} !detach")) == ["command", "ignore"]
    assert addressees(store, 1, "topic") == (NAMES[1],)
    assert actions(route(routers, 6, "and then?")) == ["ignore", "deliver"]
    route(routers, 7, f"{CODEX} !new --model m try", topic="t")
    assert addressees(store, 1, "t") == (NAMES[0],)


def test_only_a_detach_its_actor_carries_out_amends_the_record(pair):
    """#891: a bot's actor detaches on exactly `!detach`, its own mentions taken off
    (`actor_text`): `@claude !detach now` detaches nothing, and amends nothing, nor does an
    unmentioned `!detach please`. With completed mentions, in `@**Codex** !detach @**Claude**`,
    Claude's actor reads `!detach` (its mention taken off) and Codex's doesn't: Claude alone is
    taken out. Typed, `@codex !detach @claude`, neither does (only completed mentions are taken
    off), and nothing is."""
    store, routers = pair
    bind(store, 0, 100)
    bind(store, 1, 101)
    route(routers, 1, f"{CLAUDE} {CODEX} compare")
    route(routers, 2, f"{CLAUDE} !detach now")
    route(routers, 3, "!detach please")
    assert addressees(store, 1, "topic") == tuple(sorted(NAMES))
    route(routers, 4, "@codex !detach @claude")
    assert addressees(store, 1, "topic") == tuple(sorted(NAMES))
    result = route(routers, 5, f"{CODEX} !detach {CLAUDE}")
    assert [r.text for r in result] == [f"!detach {CLAUDE}", "!detach"]
    assert addressees(store, 1, "topic") == (NAMES[0],)


@pytest.mark.parametrize("last", [True, False])
def test_a_detach_takes_its_bots_out_of_the_record(pair, last):
    """The one command that amends the record, by removal, under its own id: named, the bots
    it names; unmentioned, the recorded ones under `last`, all under `all`. Empty is none."""
    store, routers = pair
    route(routers, 1, f"{CODEX} {CLAUDE} compare")
    route(routers, 2, f"{CODEX} !detach", last=last)
    assert addressees(store, 1, "topic") == (NAMES[1],)
    assert addressees(store, 1, "topic", 2) == tuple(sorted(NAMES))  # before it: unchanged
    route(routers, 3, f"{CLAUDE} you", last=last)
    route(routers, 4, "!detach", last=last)  # under `last`: Claude's, the recorded one
    assert addressees(store, 1, "topic") == ()


def test_each_bot_reads_the_record_the_message_followed(pair):
    """A bot that reads its queue later (a catch-up replays one bot's, then the other's) finds
    the record of the message's own predecessors, not the topic's newest: both agree."""
    store, routers = pair
    bind(store, 0, 1)
    bind(store, 1, 2)
    texts = [f"{CLAUDE} one", "two", f"{CODEX} three", "four"]
    first = {mid: route(routers, mid, text, order=(0,)) for mid, text in enumerate(texts, 10)}
    later = {mid: route(routers, mid, text, order=(1,)) for mid, text in enumerate(texts, 10)}
    assert [first[mid][0].action for mid in (11, 13)] == ["ignore", "deliver"]
    assert [later[mid][1].action for mid in (11, 13)] == ["deliver", "ignore"]


def test_an_unmentioned_stop_stops_the_recorded_addressees_turns(pair):
    store, routers = pair
    bind(store, 0, 1)
    bind(store, 1, 2)
    route(routers, 3, f"{CLAUDE} work")
    stop = route(routers, 4, "!stop")
    assert actions(stop) == ["ignore", "command"]
    assert (stop[1].text, stop[1].quiet) == ("!stop", False)
    assert actions(route(routers, 5, "!help")) == ["ignore", "command"]  # Claude's, alone
    reply = route(routers, 6, "!unmentioned last")  # a topic's: one bot points to a DM
    assert actions(reply) == ["ignore", "reply"] and reply[1].text == UNMENTIONED_DM


def test_a_multi_party_channel_still_needs_a_mention(pair):
    _, routers = pair
    route(routers, 1, f"{CLAUDE} hi", multi_party=True)
    assert actions(route(routers, 2, "chat", multi_party=True)) == ["ignore", "ignore"]
    assert actions(route(routers, 3, f"{CLAUDE} you", multi_party=True)) == ["ignore", "new"]


def test_the_record_reads_only_the_messages_text_and_position(pair):
    """Not the channel's state, which may change between two bots' reads: a named `!detach` in a
    multi-party channel takes its bot out (an unmentioned one reaches none there), so that the
    channel alone again sends the next message to all; and a public channel's prompt records."""
    store, routers = pair
    route(routers, 1, f"{CLAUDE} hi", multi_party=True)
    route(routers, 2, "!detach", multi_party=True)
    assert addressees(store, 1, "topic") == (NAMES[1],)
    route(routers, 3, f"{CLAUDE} !detach", multi_party=True)
    assert addressees(store, 1, "topic") == ()
    assert actions(route(routers, 4, "anyone?")) == ["new", "new"]
    assert actions(route(routers, 5, f"{CODEX} hi", private=False)) == ["note", "ignore"]
    assert addressees(store, 1, "topic") == (NAMES[0],)


@pytest.mark.parametrize("newer", ["moved", "destination"])
def test_the_record_follows_a_moved_card_and_the_newer_wins(pair, newer):
    """A live move of Claude's card, which Codex reads first: Codex's `follow_cards` alone
    takes the record with it, from the move's source, though Claude's row has moved already
    (as by Claude's own `move_card`); into a topic with its own record, the newer wins."""
    store, routers = pair
    bind(store, 1, 2, topic="a")
    route(routers, 1 if newer == "destination" else 5, f"{CLAUDE} here", topic="a")
    route(routers, 3, f"{CODEX} there", topic="b")
    store.update("sessions", {"id": "s1-2"}, topic="b")  # Claude's row, without its record
    routers[0].follow_cards(dict(message_ids=[2], stream_id=1, orig_subject="a", subject="b"))
    winner = 1 if newer == "moved" else 0
    assert addressees(store, 1, "b") == (NAMES[winner],)
    expected = ["ignore", "ignore"]
    expected[winner] = "deliver" if winner == 1 else "new"
    assert actions(route(routers, 6, "continue", topic="b")) == expected
    assert addressees(store, 1, "a") == ((NAMES[1],))  # a copy: the source keeps its own


async def test_a_move_found_by_a_card_check_takes_the_record(bridge):
    """A topic moved while the bridge was down: the card's check (`verify_card`) moves the
    session, and the record with it, as a live move does."""
    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    fake.messages[actor.row["card_message_id"]]["subject"] = "moved while down"
    b.unverify()
    await b.verify_card(actor.row["id"])
    assert actor.row["topic"] == "moved while down"
    assert addressees(b.store, 1, "moved while down") == ("Test Bot",)
    del fake.messages[actor.row["card_message_id"]]  # then the card goes: no message says so
    b.unverify()
    await b.verify_card(actor.row["id"])
    assert actor.row["state"] == "detached" and b.store.get(unmentioned.card_gone(actor.row["id"]))


def test_a_bot_far_behind_still_finds_the_entry_its_message_follows(pair):
    """Codex reads a mention of itself, a follow-up and 101 more mentions before Claude reads
    any of them, as a catch-up replays one bot's queue, then the other's: Claude still finds the
    entry that the follow-up follows, and leaves it to Codex."""
    store, routers = pair
    bind(store, 0, 1)
    texts = [f"{CODEX} one", "two", *[f"{CODEX} more" for _ in range(101)]]
    ahead = [route(routers, mid, text, order=(0,))[0] for mid, text in enumerate(texts, 10)]
    behind = [route(routers, mid, text, order=(1,))[1] for mid, text in enumerate(texts[:2], 10)]
    assert (ahead[1].action, behind[1].action) == ("deliver", "ignore")


def test_an_entry_stays_until_the_next_is_a_day_old(tmp_path):
    store = Store(tmp_path)
    day = unmentioned.RETAIN_S
    record(store, 1, "t", 1, ["A"], now=0)
    record(store, 1, "t", 2, ["B"], now=10)
    record(store, 1, "t", 2, ["A"], now=day)  # the other bot: the names join, the first time stays
    assert addressees(store, 1, "t", 2) == ("A",) and addressees(store, 1, "t") == ("A", "B")
    record(store, 1, "t", 3, ["A"], now=day + 11)  # entry 2 is over a day old: entry 1 goes
    assert [entry[0] for entry in store.get("topic_addressees:1:t")] == [2, 3]
    assert addressees(store, 1, "t", 2) == () and addressees(store, 1, "t", 3) == ("A", "B")
    follow(store, (1, "t"), (2, "t"))
    assert addressees(store, 2, "t") == ("A",)
    store.close()


# ------------------------------------------------------------ the bridge: the setting, sign-in


async def test_the_dm_command_sets_the_vms_rule_and_status_shows_it(bridge):
    b, fake, _ = bridge

    async def ask(text, uid=OWNER):
        before = max(fake.messages, default=0)
        await dm(b, text, uid=uid)
        return [m["content"] for mid, m in fake.messages.items() if mid > before]

    assert await ask("!unmentioned") == [unmentioned.EXPLAIN["all"]]
    assert await ask("!Unmentioned last") == ["Done. " + unmentioned.EXPLAIN["last"]]
    assert b.store.get("unmentioned") == "last"
    assert await ask("@**Test Bot** !unmentioned") == [unmentioned.EXPLAIN["last"]]
    assert await ask("!unmentioned sometimes") == [unmentioned.USAGE]
    (status,) = await ask("!status")
    assert status.endswith(" · without a mention: `last`")
    assert not any(r.startswith("Done") for r in await ask("!unmentioned all", uid=21))
    assert rule(b.store, "all") == "last"  # a sender not on the allowlist changes nothing
    assert any("`!unmentioned" in line for line in (await ask("!help"))[0].splitlines())
    b.config.bridge.multi_party = False  # the kill switch: neither rule applies
    (answer,) = await ask("!unmentioned all")
    assert answer.startswith("Set to `all`, but it has no effect while `bridge.multi_party` is")
    assert (await ask("!unmentioned"))[0].startswith("`all` is set, but it has no effect")


def test_the_rule_survives_a_restart_and_overrides_the_config(tmp_path):
    store = Store(tmp_path)
    assert rule(store, "all") == "all" and rule(store, "last") == "last"  # the config's
    unmentioned.command(store, "last", "!unmentioned all")
    store.close()
    store = Store(tmp_path)  # a restart, a stop and a wake: the same database
    assert rule(store, "last") == "all"
    store.close()
    config = tmp_path / "config.toml"
    config.write_text('[bridge]\nunmentioned = "last"\n')
    assert load_config(config).bridge.unmentioned == "last"
    config.write_text('[bridge]\nunmentioned = "sometimes"\n')
    with pytest.raises(ConfigError, match="expected all or last"):
        load_config(config)


async def test_addressees_under_each_rule(bridge):
    """`BotBridge.addressees`: None under `all`, without a record, for a record whose bots
    can't take the message (renamed since, detached in the topic, not in the channel, not
    allowing the sender), and with `bridge.multi_party` off, whose rule stands."""
    b, fake, _ = bridge
    await other_bot(b, fake)
    record(b.store, 1, "topic", 5, ["Claude · X"])
    assert await b.addressees(1, "topic", 9, OWNER) is None
    b.store.set("unmentioned", "last")
    assert await b.addressees(1, "topic", 9, OWNER) == ("Claude · X",)
    assert await b.addressees(1, "topic", 5) is None and await b.addressees(1, "o", 9) is None
    assert await b.addressees(1, "topic", 9, sender=21) is None  # Claude doesn't allow 21
    session_of(b, "claude", card=51)
    b.store.update("sessions", {"id": "claude-topic"}, state="detached")
    # a `!detach`'s own entry takes Claude out after it; for a message before it, it could
    assert await b.addressees(1, "topic", 9) == ("Claude · X",)
    b.store.set(unmentioned.card_gone("claude-topic"), True)  # its card found gone
    assert await b.addressees(1, "topic", 9) is None  # waits for a mention (§4.6)
    b.store.update("sessions", {"id": "claude-topic"}, state="idle")
    b.subscribers[1] = (time.monotonic() + 180, {1, 20})  # Claude left the channel
    assert await b.addressees(1, "topic", 9) is None
    b.subscribers.clear()
    record(b.store, 1, "topic", 6, ["Claude · Old"])  # renamed since
    assert await b.addressees(1, "topic", 9) is None
    b.config.bridge.multi_party = False
    assert await b.addressees(1, "topic", 6) is None


async def test_a_detach_has_the_other_bots_confirm_their_cards_first(bridge):
    """#891: a `!detach`'s amendment reads the record's entry before it, which a card moved into
    the topic while the bridge was down brings (`confirm_others`), under either rule; a prompt
    with a mention reads none."""
    b, fake, _ = bridge
    await other_bot(b, fake)
    confirms = b.all_bridges[-1].ensure_location
    await user_message(b, fake, "@**Test Bot** hello")
    confirms.assert_not_awaited()
    await user_message(b, fake, "@**Test Bot** !Detach")
    confirms.assert_awaited_once_with(1, "topic")


@pytest.mark.parametrize("error", ["refused", "transient", "bug"])
async def test_another_bots_failure_to_confirm_its_cards(bridge, error):
    """Another bot's side fails (`confirm_others`): refused by Zulip, the message is routed as
    without a record, to all, and the failure logged; transient, it is raised, so the event is
    retried; any other error is raised too, a bug staying loud."""
    from zab.zulip.client import ZulipError

    b, fake, backend = bridge
    await other_bot(b, fake)
    b.store.set("unmentioned", "last")
    record(b.store, 1, "topic", 1, ["Claude · X"])
    failure = {"refused": ZulipError("DECOY", 401), "transient": ZulipError("DECOY", 502)}
    b.all_bridges[-1].ensure_location.side_effect = failure.get(error, KeyError("DECOY"))
    if error != "refused":
        with pytest.raises(ZulipError if error == "transient" else KeyError):
            await user_message(b, fake, "hello")
        backend.create_session.assert_not_awaited()
    else:
        await user_message(b, fake, "hello")  # as under `all`: this bot starts its session
        backend.create_session.assert_awaited_once()


@pytest.mark.parametrize(
    "named,own,other,expected",
    [
        (("Test Bot",), True, True, ()),  # Claude, not named, is no sibling
        (("Claude · X",), True, True, None),  # this bot, not named, stays out
        (("Claude · X", "Test Bot"), True, False, ()),  # Claude signed out: out, as under `all`
        (("Test Bot",), False, True, ()),  # none named is signed in: this one answers, its hint
        (None, False, True, None),  # under `all`, Claude is signed in: this one stays out
    ],
)
async def test_siblings_among_the_addressees(bridge, named, own, other, expected):
    from unittest.mock import AsyncMock

    b, fake, backend = bridge
    await other_bot(b, fake, other)
    backend.signed_in = AsyncMock(return_value=own)
    assert await b.siblings(message(), False, named) == expected


async def test_the_bots_that_answer_for_the_vm(bridge):
    """#899 (`BotBridge.answerers`): the VM's bots that the channel and their allowlists let a
    message without a mention reach, whatever their sessions (Claude's detached here) and
    sign-ins, and under `last` those of them the record names, or all where it names none of
    them; none to say with the kill switch, whose bots are those with a session (`siblings`)."""
    b, fake, _ = bridge
    await other_bot(b, fake, False)
    session_of(b, "claude", card=51)
    b.store.update("sessions", {"id": "claude-topic"}, state="detached")
    assert await b.answerers(message()) == ("Claude · X", "Test Bot")
    assert await b.answerers(message(), ("Test Bot",)) == ("Test Bot",)
    assert await b.answerers(message(), ("Claude · Old",)) == ("Claude · X", "Test Bot")
    b.all_bridges[-1].router.allowlist = frozenset({99})  # Claude doesn't allow the owner
    assert await b.answerers(message()) == ("Test Bot",)
    b.all_bridges[-1].router.allowlist = frozenset({OWNER})
    b.subscribers[1] = (time.monotonic() + 180, {1, 20})  # Claude left the channel
    assert await b.answerers(message()) == ("Test Bot",)
    b.config.bridge.multi_party = False
    assert await b.answerers(message()) is None


async def test_session_says_where_a_message_without_a_mention_goes(bridge):
    b, fake, _ = bridge
    fake.members.append(dict(user_id=OWNER, full_name="Owner", is_bot=False, is_active=True))
    await other_bot(b, fake)
    session_of(b, "bot")
    assert "to all of your agents (`!unmentioned all`" in await b.unmentioned_here(1, "topic")
    b.store.set("unmentioned", "last")
    assert "until a mention here names some" in await b.unmentioned_here(1, "topic")
    await user_message(b, fake, "@**Claude · X** you")
    line = await b.unmentioned_here(1, "topic")
    assert line.startswith("Without a mention: to ` Claude · X `, as the topic's last mention")
    await user_message(b, fake, "@**Test Bot** start", topic="mine")
    await user_message(b, fake, "!session", topic="mine")
    details = list(fake.messages.values())[-1]["content"].splitlines()
    assert details[-2] == "Without a mention: to ` Test Bot `, as the topic's last mention " + (
        "named (`!unmentioned last`, set in a direct message)"
    )
    fake.members.append(dict(user_id=21, full_name="Guest", is_bot=False, is_active=True))
    fake.subscribers[1].append(21)
    await b.refresh_users()
    b.subscribers.clear()
    assert "to no agent" in await b.unmentioned_here(1, "topic")


# ------------------------------------------------------- the VM's two bots, on fake backends


async def dm_to(pair, kind, text):
    """A direct message to `kind`'s bot from the owner: the answer it sends back."""
    message = pair.fake.post_as(OWNER, text)
    await pair.bridges[kind].handle({"type": "message", "message": message, "flags": []})
    return list(pair.fake.messages.values())[-1]["content"]


async def test_last_on_the_vm_and_back_to_all(make_pair):
    """Criterion 8's part, in process: a DM to either bot sets the rule for both."""
    pair = await make_pair()
    claude, codex = (f"@**{BOTS[kind][1]}**" for kind in ("claude", "codex"))
    assert (await dm_to(pair, "codex", "!unmentioned last")).startswith("Done.")
    await pair.say(f"{claude} remember orchid", "t")
    await pair.turns(1, "t", kinds=("claude",))
    await pair.say("which flower?", "t")
    await pair.turns(2, "t", kinds=("claude",))
    assert set(pair.rows("t")) == {"claude"}
    await pair.say(f"{claude} {codex} compare", "t")
    await pair.turns(1, "t")
    await pair.say("again", "t")
    await pair.turns(2, "t", kinds=("codex",))
    await pair.turns(4, "t", kinds=("claude",))
    assert (await dm_to(pair, "claude", "!unmentioned all")).startswith("Done.")
    await pair.say(f"{claude} only you", "u")
    await pair.turns(1, "u", kinds=("claude",))
    await pair.say("and you?", "u")
    await pair.turns(1, "u", kinds=("codex",))
    await pair.turns(2, "u", kinds=("claude",))


async def test_a_fork_topic_starts_with_the_bots_that_forked(make_pair):
    """Under `last`, an unmentioned follow-up in a fork's topic stays with the forked agent
    (SPEC §7.11); a fork of both bots, one `!fork` message, records both."""
    pair = await make_pair()
    await pair.parents()
    await dm_to(pair, "claude", "!unmentioned last")
    claude, codex = (f"@**{BOTS[kind][1]}**" for kind in ("claude", "codex"))
    await pair.say(f"{claude} !fork alone")
    await pair.turns(1, "topic ⑂ 1", kinds=("claude",))
    await pair.say("continue", "topic ⑂ 1")
    await pair.turns(2, "topic ⑂ 1", kinds=("claude",))
    assert set(pair.rows("topic ⑂ 1")) == {"claude"}
    await pair.say(f"{claude} {codex} !fork both")
    assert addressees(pair.store, 1, "topic ⑂ 2") == ("Claude · X", "Codex · X")
    await pair.turns(1, "topic ⑂ 2")


async def test_another_bots_card_moved_while_down_brings_the_record(make_pair):
    """Claude's topic moved while the bridge was down, and Codex, with no card there, reads the
    next message first: it has Claude's bridge find Claude's card (`confirm_others`), whose
    record leaves the message to Claude."""
    pair = await make_pair()
    await dm_to(pair, "codex", "!unmentioned last")
    await pair.say(f"@**{BOTS['claude'][1]}** remember orchid", "t")
    await pair.turns(1, "t", kinds=("claude",))
    (row,) = pair.rows("t").values()
    pair.fake.messages[row["card_message_id"]]["subject"] = "t2"
    for each in pair.bridges.values():
        each.unverify()  # as at a start
    await pair.say("which flower?", "t2", order=("codex", "claude"))
    await pair.turns(2, "t2", kinds=("claude",))
    assert set(pair.rows("t2")) == {"claude"}


@pytest.mark.parametrize("first", ["codex", "claude"])
async def test_a_detach_then_a_follow_up_in_either_order(make_pair, first):
    """As at a wake, the queue holds `@claude !detach` and an unmentioned follow-up after
    Claude's prompt, and one bot reads both before the other: in either order, the record after
    the `!detach` names nobody, so Codex takes the follow-up and Claude, which waits for a
    mention, doesn't (in the workshop's order, Codex first, nobody did)."""
    pair = await make_pair()
    claude = f"@**{BOTS['claude'][1]}**"
    await dm_to(pair, "claude", "!unmentioned last")
    await pair.say(f"{claude} remember orchid", "t")
    await pair.turns(1, "t", kinds=("claude",))
    sent = [pair.fake.post_as(OWNER, text, "t") for text in (f"{claude} !detach", "which?")]
    for kind in (first, "codex" if first == "claude" else "claude"):
        for posted in sent:
            event = {"type": "message", "message": dict(posted), "flags": posted["flags"]}
            await pair.bridges[kind].handle(event)
            if kind == "claude" and posted is sent[0]:  # its actor detaches the session
                await pair.until(lambda: "claude" not in pair.rows("t"), "the detach")
    await pair.turns(1, "t", kinds=("codex",))
    assert set(pair.rows("t")) == {"codex"}


async def test_a_later_detach_changes_no_earlier_message(make_pair):
    """Codex reads `@codex work`, `continue` and `@codex !detach` before Claude reads any: Claude
    still finds `continue` addressed to Codex, which took it, and stays out. The `!detach`
    amends the record from its own position on, and Codex's session detached by it is no
    reason to read the record before it as unusable."""
    pair = await make_pair()
    codex = f"@**{BOTS['codex'][1]}**"
    await dm_to(pair, "claude", "!unmentioned last")
    sent = [pair.fake.post_as(OWNER, text, "t") for text in (f"{codex} work", "continue")]
    sent.append(pair.fake.post_as(OWNER, f"{codex} !detach", "t"))
    for kind in ("codex", "claude"):
        for n, posted in enumerate(sent, 1):
            event = {"type": "message", "message": dict(posted), "flags": posted["flags"]}
            await pair.bridges[kind].handle(event)
            if kind == "codex" and n < 3:
                await pair.turns(n, "t", kinds=("codex",))
            elif kind == "codex":
                await pair.until(lambda: not pair.rows("t"), "the detach")
    assert not pair.rows("t") and {r["bot"] for r in pair.store.rows("sessions")} == {"codex"}


@pytest.mark.parametrize("last", [False, True])
@pytest.mark.parametrize("first", ["claude", "codex"])
@pytest.mark.parametrize(
    "start,texts",
    [
        ("both", ["!help", "@**Claude · X** !detach"]),
        ("unmentioned", ["!help", "@**Claude · X** !detach"]),
        ("both", ["@**Claude · X** !detach", "!help"]),
        ("both", ["!vm", "@**Claude · X** !detach"]),
        ("both", ["!tools", "@**Claude · X** !detach"]),
        ("both", ["!detach", "!help"]),  # both wait for a mention: Claude still answers
        ("both", ["!unmentioned", "@**Claude · X** !detach"]),  # its pointer to a DM
        ("both", ["@**Claude · X** !detach", "!unmentioned"]),
    ],
)
async def test_a_command_for_the_vm_next_to_a_detach_gets_one_answer(
    make_pair, last, first, start, texts
):
    """#899: the queue holds an unmentioned `!help`, `!vm` or `!tools` and a `!detach`, and one
    bot reads both before the other, whose actor has carried out the `!detach` when it is that
    bot's, as at a wake. The first by name of the bots that the channel and their allowlists
    let it reach answers, whatever their sessions (`BotBridge.answerers`): one answer, in
    either order and under either rule. Before, two came when Claude detached after answering,
    and none when Codex read both before Claude did, Claude's `!detach` first."""
    pair = await make_pair()
    if last:
        await dm_to(pair, "claude", "!unmentioned last")
    names = " ".join(f"@**{name}**" for _, name, _ in BOTS.values())
    await pair.say(f"{names} remember orchid" if start == "both" else "remember orchid")
    await pair.turns(1)
    sent = [pair.fake.post_as(OWNER, text, "topic") for text in texts]
    for kind in (first, "codex" if first == "claude" else "claude"):
        for posted in sent:
            event = {"type": "message", "message": dict(posted), "flags": posted["flags"]}
            await pair.bridges[kind].handle(event)
            if posted["content"] in (f"@**{BOTS[kind][1]}** !detach", "!detach"):
                await pair.until(lambda kind=kind: kind not in pair.rows("topic"), "the detach")
    await pair.quiet()
    bots = {uid for uid, _, _ in BOTS.values()}
    replies = [
        m["content"]
        for mid, m in pair.fake.messages.items()
        if mid > sent[0]["id"] and m["sender_id"] in bots
    ]
    assert len([r for r in replies if not r.startswith("Detached")]) == 1, replies


async def test_who_gets_a_message_without_a_mention_is_unchanged_under_all(make_pair):
    """#899 changes only which bot answers a command for the VM: under `all`, each prompt
    without a mention still goes where it did. After an unmentioned `!detach` in a topic without
    a session, the next one starts a session of each bot; after an expired `@claude !detach`,
    read by a catch-up, Claude's session, still live, gets the next one; and after `@codex
    !detach` and `@codex !attach` of its thread, so does Codex's."""
    pair = await make_pair()
    claude, codex = (f"@**{BOTS[kind][1]}**" for kind in ("claude", "codex"))
    await pair.say("!detach", "t1")
    await pair.say("remember orchid", "t1")
    await pair.turns(1, "t1")
    await pair.say("remember tulip", "t2")
    await pair.turns(1, "t2")
    expired = pair.fake.post_as(OWNER, f"{claude} !detach", "t2")
    expired["timestamp"] = time.time() - 10 * 86400
    for each in pair.bridges.values():
        await Catchup(each).expired(dict(expired))
    await pair.say("and the flower?", "t2")
    await pair.turns(2, "t2")
    await pair.say("remember iris", "t3")
    await pair.turns(1, "t3")
    thread = pair.rows("t3")["codex"]["native_id"]
    await pair.say(f"{codex} !detach", "t3")
    await pair.until(lambda: "codex" not in pair.rows("t3"), "the detach")
    await pair.say(f"{codex} !attach {thread}", "t3")
    await pair.until(lambda: "codex" in pair.rows("t3"), "the attach")
    await pair.say("and the flower?", "t3")
    await pair.turns(2, "t3", kinds=("claude",))
    await pair.turns(1, "t3", kinds=("codex",))  # the attached session's first turn here
    for topic in ("t2", "t3"):
        rows = pair.rows(topic)
        assert set(rows) == {"claude", "codex"}
        assert all("and the flower?" in pair.prompts(row)[-1] for row in rows.values())


@pytest.mark.parametrize("first", ["codex", "claude"])
async def test_under_last_the_answer_for_the_vm_reads_the_record_as_written(make_pair, first):
    """#899, review round 1: under `last` the record names only Codex, whose earlier card a
    check found gone, and whose new session `@codex !detach` then detaches. The bot that
    answers `!help` comes from the record as written (`unmentioned.addressees`), not as
    `BotBridge.addressees` finds it, which reads the old card as gone now: Codex answers once,
    in either order, though Claude, reading `!help` after the `!detach`, finds Codex unable."""
    pair = await make_pair()
    codex = f"@**{BOTS['codex'][1]}**"
    await dm_to(pair, "claude", "!unmentioned last")
    await pair.say(f"{codex} remember orchid")
    await pair.turns(1, kinds=("codex",))
    old = pair.rows("topic")["codex"]
    del pair.fake.messages[old["card_message_id"]]
    for each in pair.bridges.values():
        each.unverify()
    await pair.say(f"{codex} hello again")
    await pair.turns(1, kinds=("codex",))
    assert pair.store.get(unmentioned.card_gone(old["id"]))
    sent = [pair.fake.post_as(OWNER, text, "topic") for text in ("!help", f"{codex} !detach")]
    for kind in (first, "codex" if first == "claude" else "claude"):
        for posted in sent:
            event = {"type": "message", "message": dict(posted), "flags": posted["flags"]}
            await pair.bridges[kind].handle(event)
            if kind == "codex" and posted is sent[1]:
                await pair.until(lambda: not pair.rows("topic"), "the detach")
    await pair.quiet()
    assert [m["sender_id"] for m in pair.fake.messages.values() if m["content"] == HELP] == [
        BOTS["codex"][0]
    ]


async def test_an_expired_or_nameless_command_for_the_vm_is_as_before(make_pair):
    """#899 leaves two cases to the old path: an unmentioned `!help` that a catch-up reads after
    `MAX_AGE` gets each bot's expired note (§7.8.1), not an answer; and one in a topic without a
    name, where no session is: the one bot's "Please use a named topic for a session." again."""
    pair = await make_pair()
    old = pair.fake.post_as(OWNER, "!help", "topic")
    old["timestamp"] = time.time() - 10 * 86400
    for each in pair.bridges.values():
        await Catchup(each).expired(dict(old))
    notes = [m["content"] for m in pair.fake.messages.values() if m["id"] > old["id"]]
    assert notes == [pair.bridges[kind].note(EXPIRED) for kind in pair.bridges]
    mark = max(pair.fake.messages)
    await pair.say("!help", "")
    replies = [m["content"] for mid, m in pair.fake.messages.items() if mid > mark + 1]
    assert replies == [pair.bridges["claude"].note("Please use a named topic for a session.")]
