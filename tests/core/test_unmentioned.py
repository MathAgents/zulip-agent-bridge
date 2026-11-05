"""A message that names no bot goes to all of the VM's bots, whatever earlier messages in its
topic addressed (#713, SPEC §4.5 rule 6.2; #687 for a new topic's first message).

The two routers share one store, as the VM's bridges do (`zab run`): a session of either bot is
seen by both."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core import types as t
from zab.core.commands import BTW_NO_SESSION, FORK_NO_SESSION
from zab.core.router import HANDOVER_NAMED, NO_SESSION, Router
from zab.core.store import Store

pytestmark = pytest.mark.integration
NAMES = ["Codex · X", "Claude · X"]  # Claude's sorts first: it gives the VM's own answers
OWNER = 20


@pytest.fixture
def pair(tmp_path):
    store = Store(tmp_path)
    routers = [Router(store, str(i), i + 10, NAMES[i], frozenset({OWNER})) for i in range(2)]
    yield store, routers
    store.close()


def route(routers, mid, text, *, topic="topic", sender=OWNER, siblings=True, **kw):
    """Each bot's route of one message; `siblings` False: the bridge's None for every bot."""
    message = dict(
        id=mid, sender_id=sender, stream_id=1, subject=topic, content=text, type="stream"
    )
    return [
        r.route(
            message,
            flags=["mentioned"] if f"@**{NAMES[i]}" in text else [],
            sender_is_bot=kw.get("sender_is_bot", False),
            private=kw.get("private", True),
            other_bot_names={NAMES[1 - i]},
            multi_party=kw.get("multi_party", False),
            history_only=kw.get("history_only", False),
            siblings=(NAMES[1 - i],) if siblings else None,
        )
        for i, r in enumerate(routers)
    ]


def bind(store, i, card, topic="topic", state="idle"):
    store.insert(
        "sessions",
        id=f"s{i}-{card}",
        bot=str(i),
        backend=str(i),
        native_id=f"n{i}-{card}",
        card_message_id=card,
        stream_id=1,
        topic=topic,
        workspace="/repo",
        workspace_mode="shared",
        repo_root="/repo",
        settings_json="{}",
        state=state,
        shared=0,
        created_at=0,
        updated_at=0,
    )


def actions(result):
    return [r.action for r in result]


def test_no_mention_goes_to_both_whatever_came_before(pair):
    store, routers = pair
    first = route(routers, 1, "What is 2+2?")
    assert actions(first) == ["new", "new"]
    assert [r.text for r in first] == ["What is 2+2?"] * 2
    for i in range(2):
        assert first[i].peers == (NAMES[1 - i],)
        assert store.get(f"addressed_peers:{i}:1") == [NAMES[1 - i]]  # the co-addressed note
    bind(store, 0, 2)  # the cards the two new sessions post, after the message
    bind(store, 1, 3)
    follow = route(routers, 4, "And 3+3?")
    assert actions(follow) == ["deliver", "deliver"]
    assert [r.peers for r in follow] == [(NAMES[1],), (NAMES[0],)]
    assert actions(route(routers, 5, f"@**{NAMES[1]}** you only")) == ["ignore", "deliver"]
    assert actions(route(routers, 6, "go on")) == ["deliver", "deliver"]  # not Claude's alone
    assert actions(route(routers, 7, f"@**{NAMES[0]}** @**{NAMES[1]}** both")) == ["deliver"] * 2
    assert not store.rows("kv", key="topic_addressing:0:1:topic")  # no addressee record


def test_after_a_one_bot_exchange_the_other_starts_its_session(pair):
    """#713, criterion 2: a bot without a session in the topic starts one, with the note."""
    store, routers = pair
    assert actions(route(routers, 1, f"@**{NAMES[1]}** prove it")) == ["ignore", "new"]
    bind(store, 1, 2)
    follow = route(routers, 3, "and the converse?")
    assert actions(follow) == ["new", "deliver"]
    assert [r.peers for r in follow] == [(NAMES[1],), (NAMES[0],)]
    assert follow[0].text == "and the converse?"


def test_whichever_bot_routes_first_the_other_still_starts(pair):
    """The order of the two bots' queues doesn't matter: a sibling's session that this very
    message started changes nothing for the other."""
    store, routers = pair
    message = dict(id=7, sender_id=OWNER, stream_id=1, subject="t", content="hi", type="stream")

    def one(i):
        return routers[i].route(
            message, flags=[], sender_is_bot=False, private=True, siblings=(NAMES[1 - i],)
        )

    assert one(1).action == "new"
    bind(store, 1, 8, topic="t")
    assert one(0).action == "new"


def test_multi_party_unknown_membership_and_multi_party_off_without_a_session(pair):
    """The bridge passes None there (`BotBridge.siblings`); the router checks multi_party too."""
    _, routers = pair
    assert actions(route(routers, 1, "chat", siblings=False)) == ["ignore", "ignore"]
    assert actions(route(routers, 2, "chat", topic="t2", multi_party=True)) == ["ignore"] * 2


@pytest.mark.parametrize(
    "sender,is_bot,private,text",
    [
        (21, False, True, "hello"),  # not on the allowlist
        (OWNER, True, True, "hello"),  # a bot
        (OWNER, False, False, "hello"),  # a public channel, allow_public off: no refusal either
        (OWNER, False, True, "// an aside"),
    ],
)
def test_others_bots_public_channels_and_asides_start_nothing(pair, sender, is_bot, private, text):
    _, routers = pair
    result = route(routers, 1, text, sender=sender, sender_is_bot=is_bot, private=private)
    assert actions(result) == ["ignore", "ignore"]


def test_own_mention_without_the_flag_is_no_message_without_a_mention(pair):
    _, routers = pair
    message = dict(
        id=1, sender_id=OWNER, stream_id=1, subject="t", content=f"@**{NAMES[0]}** hi",
        type="stream",
    )  # fmt: skip
    route = routers[0].route(message, flags=[], sender_is_bot=False, private=True, siblings=())
    assert route.action == "ignore"


@pytest.mark.parametrize(
    "text,expected,reply",
    [
        ("!help", ["ignore", "command"], "!help"),  # the VM's answers, from one bot
        ("!vm", ["ignore", "command"], "!vm"),
        ("!tools", ["ignore", "command"], "!tools"),
        ("!attach", ["command", "command"], "!attach"),  # each lists its own
        ("!attach abc", ["command", "command"], "!attach abc"),
        ("!new --model m work", ["new", "new"], "!new --model m work"),
        ("!fork try it", ["ignore", "reply"], FORK_NO_SESSION),  # once: no bot has a session
        ("!btw what?", ["ignore", "reply"], BTW_NO_SESSION),  # likewise (#716)
        ("!stop", ["ignore", "reply"], NO_SESSION),
        ("!model opus", ["ignore", "reply"], NO_SESSION),
        ("!login", ["reply", "reply"], "Send !login to me in a direct message."),
        # as people type them (#709): read before the lead check, so `!Help` is `!help`
        ("!Help", ["ignore", "command"], "!help"),
        ("`!vm`", ["ignore", "command"], "!vm"),
        ("!Login", ["reply", "reply"], "Send !login to me in a direct message."),
        ("`!login`", ["reply", "reply"], "Send !login to me in a direct message."),
        ("!New --model M Work", ["new", "new"], "!new --model M Work"),
        ("`!Stop`", ["ignore", "reply"], NO_SESSION),
        *[
            (command, ["ignore", "reply"], NO_SESSION)
            for command in (
                "!status", "!session", "!usage", "!diff", "!queue", "!queue clear", "!model",
                "!effort", "!effort high", "!approve", "!approve-session", "!deny",
                "!file results.csv", "!handover", "!detach", "!foo",
            )
        ],
    ],
)  # fmt: skip
def test_commands_in_a_topic_without_sessions(pair, text, expected, reply):
    result = route(pair[1], 1, text)
    assert actions(result) == expected
    assert all(r.text == reply for r in result if r.action != "ignore")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("!help", ["ignore", "command"]),
        ("!vm", ["ignore", "command"]),
        ("!stop", ["command", "command"]),
        ("!status", ["command", "command"]),
        ("!model opus", ["command", "command"]),
        ("!diff", ["command", "command"]),
        ("!fork try it", ["command", "command"]),
        ("!btw what?", ["command", "command"]),  # each answers about its own (#716)
        ("!tools", ["ignore", "command"]),
        ("!handover", ["ignore", "reply"]),  # refused once: both would write HANDOVER.md
        ("!login", ["reply", "reply"]),
        ("!Stop", ["command", "command"]),  # as people type them (#709)
        ("`!help`", ["ignore", "command"]),
        ("`!Handover`", ["ignore", "reply"]),
        *[
            (command, ["command", "command"])
            for command in (
                "!session", "!usage", "!queue", "!queue clear", "!model", "!effort",
                "!effort high", "!approve", "!approve-session", "!deny", "!file results.csv",
                "!detach", "!attach", "!attach abc", "!new more", "!foo",
            )
        ],
    ],
)  # fmt: skip
def test_commands_in_a_topic_with_both_sessions(pair, text, expected):
    """Each acts on its own session, `quiet` where it doesn't act (§4.6, the actor's part)."""
    store, routers = pair
    bind(store, 0, 1)
    bind(store, 1, 2)
    result = route(routers, 3, text)
    assert actions(result) == expected
    assert all(r.quiet for r in result if r.action == "command")
    if text == "!handover":
        assert result[1].text == HANDOVER_NAMED
        assert not store.pending_deliveries("0") and not store.pending_deliveries("1")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("!stop", ["command", "ignore"]),  # Claude has no session: it leaves !stop to Codex
        ("!fork try it", ["command", "ignore"]),
        ("!btw what?", ["command", "ignore"]),
        ("!model opus", ["command", "ignore"]),
        ("!help", ["ignore", "command"]),  # the VM's answer, still from Claude
        ("!tools", ["ignore", "command"]),
        ("!new more", ["command", "new"]),
        ("!handover", ["command", "ignore"]),  # one agent here: it writes HANDOVER.md
        ("!attach", ["command", "command"]),
        ("!login", ["reply", "reply"]),
        *[
            (command, ["command", "ignore"])
            for command in (
                "!status", "!session", "!usage", "!diff", "!queue", "!queue clear", "!model",
                "!effort", "!effort high", "!approve", "!approve-session", "!deny",
                "!file results.csv", "!detach", "!foo",
            )
        ],
    ],
)  # fmt: skip
def test_commands_in_a_topic_with_one_session(pair, text, expected):
    store, routers = pair
    bind(store, 0, 1)
    assert actions(route(routers, 2, text)) == expected


def test_a_named_command_still_answers(pair):
    """A mention of a bot without a session gets its refusal, as before."""
    store, routers = pair
    bind(store, 0, 1)
    result = route(routers, 2, f"@**{NAMES[1]}** !stop")
    assert actions(result) == ["ignore", "reply"] and result[1].text == NO_SESSION
    assert not route(routers, 3, f"@**{NAMES[0]}** !stop")[0].quiet


def test_a_fork_topic_brings_in_the_other_bot(pair):
    """#713: after Claude's `!fork`, an unmentioned follow-up in the fork's topic goes to
    Claude's fork and starts Codex there, without the parent conversation (SPEC §7.11)."""
    store, routers = pair
    bind(store, 1, 5, topic="topic ⑂ 1")
    result = route(routers, 6, "continue", topic="topic ⑂ 1")
    assert actions(result) == ["new", "deliver"]
    assert [r.peers for r in result] == [(NAMES[1],), (NAMES[0],)]


def test_moved_cards_change_nothing(pair):
    """A card moved in, or out: each bot without a session in the topic starts one."""
    store, routers = pair
    bind(store, 0, 2, topic="a")
    routers[0].move_card(dict(message_ids=[2], subject="b"), private=True)
    assert actions(route(routers, 3, "continue", topic="b")) == ["deliver", "new"]
    bind(store, 1, 4, topic="T")
    bind(store, 0, 5, topic="T")
    assert routers[1].move_card(dict(message_ids=[4], subject="T2"), private=True)
    assert actions(route(routers, 6, "carry on here", topic="T")) == ["deliver", "new"]
    assert actions(route(routers, 7, "and here", topic="T2")) == ["new", "deliver"]


def test_an_unnamed_topic_gets_one_note_from_each_bot(pair):
    """Review of #688, finding 3: in general chat (no topic name), a message without a mention
    gets "Please use a named topic for a session." once from each bot, and then silence."""
    _, routers = pair
    first = route(routers, 1, "hello", topic="")
    assert actions(first) == ["note", "note"]
    assert all(r.text == "Please use a named topic for a session." for r in first)
    assert actions(route(routers, 2, "hello again", topic="")) == ["ignore", "ignore"]


def test_a_detached_session_waits_for_a_mention(pair):
    """`!detach` (SPEC §4.6): afterwards a mention starts a new session of that bot; a message
    without one doesn't. The other bot's session takes it."""
    store, routers = pair
    bind(store, 0, 2, state="detached")
    bind(store, 1, 3)
    assert actions(route(routers, 4, "more")) == ["ignore", "deliver"]
    assert actions(route(routers, 5, f"@**{NAMES[0]}** again")) == ["new", "ignore"]
    bind(store, 1, 6, topic="t", state="detached")
    assert actions(route(routers, 7, "hello", topic="t")) == ["new", "ignore"]


async def test_a_detached_bot_is_no_sibling(bridge):
    b, fake, _ = bridge
    await other_bot(b, fake)
    session_of(b, "claude", card=51)
    b.store.update("sessions", {"id": "claude-topic"}, state="detached")
    assert await b.siblings(message(), False) == ()
    session_of(b, "bot", topic="t")
    b.store.update("sessions", {"id": "bot-t"}, state="detached")
    assert await b.siblings(message(topic="t"), False) is None


def test_a_vm_with_one_bot(pair):
    _, routers = pair
    message = dict(id=1, sender_id=OWNER, stream_id=1, subject="t", content="hi", type="stream")
    result = routers[0].route(message, flags=[], sender_is_bot=False, private=True, siblings=())
    assert (result.action, result.peers, result.quiet) == ("new", (), False)
    message |= dict(id=2, content="!help")
    result = routers[0].route(message, flags=[], sender_is_bot=False, private=True, siblings=())
    assert result.action == "command"


def test_an_expired_message_is_each_bots_input(pair):
    store, routers = pair
    assert actions(route(routers, 1, "old question", history_only=True)) == ["expired"] * 2
    bind(store, 0, 2)
    assert actions(route(routers, 3, "another", history_only=True)) == ["expired"] * 2


# ------------------------------------------------------------ the bridge: siblings, sign-in


def sibling(user_id, signed_in, name="claude", allowlist=frozenset({OWNER})):
    """Another bridge of the process, whose agent's sign-in is `signed_in` (an exception:
    the hook failed)."""
    hook = AsyncMock(side_effect=signed_in) if isinstance(signed_in, Exception) else None
    hook = hook or AsyncMock(return_value=signed_in)
    return SimpleNamespace(
        router=SimpleNamespace(user_id=user_id, allowlist=allowlist),
        backend=SimpleNamespace(kind="claude", signed_in=hook),
        bot=SimpleNamespace(name=name),
    )


async def other_bot(b, fake, signed_in=True, subscribed=True):
    fake.members.append(dict(user_id=40, full_name="Claude · X", is_bot=True))
    if subscribed:
        fake.subscribers[1].append(40)
    await b.refresh_users()
    b.all_bridges.append(sibling(40, signed_in))


def message(text="hello", topic="topic", sender=OWNER):
    return dict(type="stream", stream_id=1, subject=topic, sender_id=sender, content=text, id=99)


def session_of(b, bot, topic="topic", card=50):
    b.store.insert(
        "sessions", id=f"{bot}-{topic}", bot=bot, backend=bot, native_id=f"n-{bot}-{topic}",
        card_message_id=card, stream_id=1, topic=topic, workspace="/repo",
        workspace_mode="shared", repo_root="/repo", settings_json="{}", state="idle",
        shared=0, created_at=0, updated_at=0,
    )  # fmt: skip


@pytest.mark.parametrize(
    "own,other,expected",
    [
        (True, True, ("Claude · X",)),
        (None, None, ("Claude · X",)),  # unknown: take part
        (True, False, ()),  # the signed-out bot stays out of the others' peers
        (False, True, None),  # this bot stays out
        (False, False, ("Claude · X",)),  # none signed in: both answer, with the sign-in hint
        (True, RuntimeError("DECOY"), ("Claude · X",)),  # a failed check: unknown
    ],
)
async def test_siblings_leave_out_signed_out_agents(bridge, own, other, expected):
    b, fake, backend = bridge
    await other_bot(b, fake, other)
    backend.signed_in = AsyncMock(return_value=own)
    assert await b.siblings(message(), False) == expected


async def test_a_sign_in_answer_is_reused_for_a_few_seconds(bridge, monkeypatch):
    """Review of #730: during a catch-up's messages without a mention, an agent's sign-in is
    asked once per `SIGNED_IN_CACHE_S`, an unknown answer included."""
    from zab.core import vm

    b, fake, backend = bridge
    await other_bot(b, fake, None)
    other = b.all_bridges[-1].backend.signed_in
    backend.signed_in = AsyncMock(return_value=True)
    for topic in ("one", "two", "three"):
        assert await b.siblings(message(topic=topic), False) == ("Claude · X",)
    assert other.await_count == 1 and backend.signed_in.await_count == 1
    monkeypatch.setattr(vm, "SIGNED_IN_CACHE_S", 0.0)
    assert await b.siblings(message(topic="four"), False) == ("Claude · X",)
    assert other.await_count == 2


async def test_a_bot_with_a_session_is_asked_nothing(bridge):
    """Only a bot that would start a session is asked for its sign-in: one with a session in
    the topic answers, with its sign-in hint if it needs one."""
    b, fake, backend = bridge
    await other_bot(b, fake, False)
    other = b.all_bridges[-1].backend.signed_in
    backend.signed_in = AsyncMock(return_value=False)
    session_of(b, "bot")
    assert await b.siblings(message(), False) == ()  # Claude, signed out and without one: out
    backend.signed_in.assert_not_awaited()
    session_of(b, "claude", card=51)
    other.reset_mock()
    assert await b.siblings(message(), False) == ("Claude · X",)
    other.assert_not_awaited()


async def test_siblings_need_a_single_human_channel_and_an_allowed_sender(bridge):
    b, fake, _ = bridge
    await other_bot(b, fake)
    assert await b.siblings(message(), True) is None  # multi-party
    assert await b.siblings(message(sender=21), False) is None  # not the owner
    assert await b.siblings(dict(message(), type="private"), False) is None
    b.all_bridges[-1].router.allowlist = frozenset({99})  # Claude doesn't allow the owner
    assert await b.siblings(message(), False) == ()


async def test_a_bot_not_in_the_channel_is_no_sibling(bridge):
    """Codex round 1 on #730: Claude, configured but not subscribed to the channel, never gets
    its messages. It is no peer, and doesn't take the VM's answers from this bot: `!help`
    without a mention is answered here, though Claude's name sorts first."""
    b, fake, _ = bridge
    await other_bot(b, fake, subscribed=False)
    b.subscribers[1] = (time.monotonic() + 180, {20, 1})  # as `multi_party` reads them
    assert await b.siblings(message(), False) == ()
    b.subscribers[1] = (time.monotonic() + 180, {20, 1, 40})
    assert await b.siblings(message(), False) == ("Claude · X",)
    b.subscribers.clear()  # read again from Zulip, without Claude, for the message below
    mark = max(fake.messages, default=0)
    sent = [await user_message(b, fake, "!help", topic="help")]
    assert any(r.startswith("In your own channel") for r in replies(fake, mark, sent))


async def test_with_the_kill_switch_a_bot_not_in_the_channel_is_no_sibling(bridge):
    """Codex round 2 on #730: with `multi_party` off, which reads no membership for its own
    rule, Claude has a session in the topic but has left the channel. It is no sibling, so this
    bot answers `!help` without a mention."""
    b, fake, backend = bridge
    actor = await started(b, fake, backend, subscribed=False)  # Claude not in the channel
    b.config.bridge.multi_party = False
    session_of(b, "claude", card=51)
    b.subscribers.clear()
    assert await b.siblings(message(), False) == ()
    fake.subscribers[1].append(40)
    b.subscribers.clear()
    assert await b.siblings(message(), False) == ("Claude · X",)
    fake.subscribers[1].remove(40)
    b.subscribers.clear()
    mark = max(fake.messages)
    sent = [await user_message(b, fake, "!help")]
    await actor.inbox.join()
    assert any(r.startswith("In your own channel") for r in replies(fake, mark, sent))


async def test_the_kill_switch_starts_no_session_without_a_mention(bridge):
    """`bridge.multi_party` off: a message without a mention goes to the bots with a session in
    the topic and starts none (SPEC §4.5, "Multi-party channels")."""
    b, fake, backend = bridge
    await other_bot(b, fake)
    b.config.bridge.multi_party = False
    assert await b.siblings(message(), False) is None
    session_of(b, "bot")
    assert await b.siblings(message(), False) == ()
    session_of(b, "claude", card=51)
    assert await b.siblings(message(), False) == ("Claude · X",)
    backend.signed_in.assert_not_awaited()


async def test_a_mentioned_message_asks_for_no_sign_in(bridge):
    b, fake, backend = bridge
    await other_bot(b, fake)
    backend.signed_in = AsyncMock(return_value=True)
    await user_message(b, fake, "@**Test Bot** hello", topic="m")
    await user_message(b, fake, "@claude hello", topic="n")
    await user_message(b, fake, "// a note", topic="o")
    backend.signed_in.assert_not_awaited()
    b.all_bridges[-1].backend.signed_in.assert_not_awaited()


@pytest.mark.parametrize("own_s,other_s", [(5, 0), (0, 5)])
async def test_a_slow_sign_in_check_counts_as_unknown(bridge, monkeypatch, own_s, other_s):
    """Past its wait, an agent's sign-in is unknown and it takes part: this bot's own wait
    (`SIGNED_IN_OWN_WAIT_S`) or the other bots' (`SIGNED_IN_WAIT_S`)."""
    from zab.core import vm

    b, fake, backend = bridge
    monkeypatch.setattr(vm, "SIGNED_IN_WAIT_S", 0.05)
    monkeypatch.setattr(vm, "SIGNED_IN_OWN_WAIT_S", 0.1)

    def slow(seconds):
        async def hook():
            await asyncio.sleep(seconds)
            return False

        return hook

    fake.members.append(dict(user_id=40, full_name="Claude · X", is_bot=True))
    fake.subscribers[1].append(40)
    await b.refresh_users()
    b.all_bridges.append(sibling(40, False))
    b.all_bridges[-1].backend.signed_in = slow(other_s)
    backend.signed_in = slow(own_s)
    if own_s:  # this bot, past its wait: it takes part; the other bot, signed out, doesn't
        assert await b.siblings(message(), False) == ()
    else:  # the other bot, past its wait: in; this bot, signed out: out
        assert await b.siblings(message(), False) is None


async def test_this_bots_own_agent_is_waited_for_longer(bridge, monkeypatch):
    """Review of #688, finding 4: after a wake, a Codex daemon still connecting answers within
    `SIGNED_IN_OWN_WAIT_S`, and a Codex never signed in stays out, silently; another bot's
    answer is waited for `SIGNED_IN_WAIT_S` only."""
    from zab.core import vm

    b, fake, backend = bridge
    monkeypatch.setattr(vm, "SIGNED_IN_WAIT_S", 0.05)
    monkeypatch.setattr(vm, "SIGNED_IN_OWN_WAIT_S", 2.0)
    await other_bot(b, fake, True)

    async def connecting():
        await asyncio.sleep(0.2)
        return False

    backend.signed_in = connecting
    assert await b.siblings(message(), False) is None


def hanging():
    async def hook():
        await asyncio.sleep(30)  # a Codex daemon that doesn't connect
        return False

    return hook


async def test_after_a_timed_out_wait_the_own_agent_gets_the_short_wait(bridge, monkeypatch):
    """Review round 2 of #688: a startup whose own agent's sign-in timed out (a broken Codex
    daemon) waits for it at most once: the next message's check takes `SIGNED_IN_WAIT_S`."""
    from zab.core import vm

    b, fake, backend = bridge
    monkeypatch.setattr(vm, "SIGNED_IN_WAIT_S", 0.05)
    monkeypatch.setattr(vm, "SIGNED_IN_OWN_WAIT_S", 0.4)
    monkeypatch.setattr(vm, "SIGNED_IN_CACHE_S", 0.0)  # each call asks
    b.own_wait_from = time.monotonic()  # the bridge just started
    await other_bot(b, fake, True)
    backend.signed_in = hanging()
    began = time.monotonic()
    assert await b.siblings(message(), False) == ("Claude · X",)  # unknown: it takes part
    assert time.monotonic() - began >= 0.3
    began = time.monotonic()
    assert await b.siblings(message(topic="other"), False) == ("Claude · X",)
    assert time.monotonic() - began < 0.25


async def test_long_after_the_start_the_own_agent_gets_the_short_wait(bridge, monkeypatch):
    """Review round 2 of #688: past `SIGNED_IN_OWN_WAIT_S` after the bridge's start (a Codex
    daemon down mid-session), its own agent's sign-in is waited for `SIGNED_IN_WAIT_S`."""
    from zab.core import vm

    b, fake, backend = bridge
    monkeypatch.setattr(vm, "SIGNED_IN_WAIT_S", 0.05)
    monkeypatch.setattr(vm, "SIGNED_IN_OWN_WAIT_S", 0.5)
    b.own_wait_from = time.monotonic() - 3600
    await other_bot(b, fake, True)
    backend.signed_in = hanging()
    began = time.monotonic()
    assert await b.siblings(message(), False) == ("Claude · X",)
    assert time.monotonic() - began < 0.4


async def test_an_unmentioned_message_starts_a_session_with_the_note(bridge):
    b, fake, backend = bridge
    await other_bot(b, fake)
    await user_message(b, fake, "What is 2+2?", topic="fresh")
    backend.create_session.assert_awaited_once()
    text = backend.start_turn.call_args.args[1].text
    assert "[Addressed to you and to Claude · X (other AI agents in this topic)" in text
    assert text.endswith("What is 2+2?")
    (card,) = [m for m in fake.messages.values() if m["content"].startswith("**zab session**")]
    assert card["content"] == (
        "**zab session** · 🤖 Session started. Write in this topic to continue (mention me if "
        "others are in the channel); !help lists commands, !session shows details."
    )


async def test_a_signed_out_agent_stays_out_silently(bridge):
    b, fake, backend = bridge
    await other_bot(b, fake)
    backend.signed_in = AsyncMock(return_value=False)
    before = len(fake.messages)
    await user_message(b, fake, "What is 2+2?", topic="fresh")
    backend.create_session.assert_not_awaited()
    assert len(fake.messages) == before + 1  # the user's message, and no reply
    await user_message(b, fake, "@**Test Bot** you, please", topic="fresh")  # a mention: as before
    backend.create_session.assert_awaited_once()


async def test_a_multi_party_channel_still_needs_a_mention(bridge):
    b, fake, backend = bridge
    fake.members.append(dict(user_id=21, full_name="Guest", is_bot=False, is_active=True))
    fake.subscribers[1].append(21)
    await b.refresh_users()
    await user_message(b, fake, "What is 2+2?", topic="fresh")
    backend.create_session.assert_not_awaited()
    await user_message(b, fake, "@codex what is 2+2?", topic="fresh")  # a typed one counts
    backend.create_session.assert_not_awaited()  # the bot's router has no kind in this rig
    b.router.kind = "codex"
    await user_message(b, fake, "@codex what is 2+2?", topic="fresh")
    backend.create_session.assert_awaited_once()


# ------------------------------------------------------- unmentioned commands kept quiet (§4.6)


async def started(b, fake, backend, subscribed=True):
    """This bot's session in "topic", and Claude, another bot of the VM, beside it."""
    await other_bot(b, fake, subscribed=subscribed)
    await user_message(b, fake, "@**Test Bot** start")
    actor = b.actors["native"]
    await actor.inbox.join()
    return actor


def replies(fake, after, sent):
    """The bot's messages after message `after`, not the ones the test `sent` as the owner."""
    return [m["content"] for mid, m in fake.messages.items() if mid > after and mid not in sent]


async def test_an_idle_stop_and_an_unknown_value_are_quiet_without_a_mention(bridge):
    b, fake, backend = bridge
    actor = await started(b, fake, backend)
    await actor.event(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    await actor.inbox.join()
    backend.update_settings.side_effect = t.InvalidSetting("model", "opus", ["gpt-5"])
    mark = max(fake.messages)
    sent = [await user_message(b, fake, text) for text in ("!stop", "!model opus", "!new more")]
    assert replies(fake, mark, sent) == []
    backend.interrupt.assert_not_awaited()
    sent.append(await user_message(b, fake, "@**Test Bot** !model opus"))  # named: as before
    assert any("Unknown model" in r for r in replies(fake, mark, sent))


async def test_an_unmentioned_model_this_backend_knows_is_set(bridge):
    b, fake, backend = bridge
    actor = await started(b, fake, backend)
    await actor.event(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    await actor.inbox.join()
    mark = max(fake.messages)
    sent = [await user_message(b, fake, "!model gpt-5")]
    assert any(r.startswith("model: ") and "gpt-5" in r for r in replies(fake, mark, sent))


async def test_unmentioned_effort_queue_session_and_approvals_at_the_actor(bridge):
    """Review of #730, R1: without a mention, an `!effort` its backend refuses says nothing;
    `!queue` and `!queue clear` answer from each bot; an approval command with no pending
    request says nothing, as when named."""
    b, fake, backend = bridge
    actor = await started(b, fake, backend)
    await actor.event(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    await actor.inbox.join()
    backend.update_settings.side_effect = t.InvalidSetting("effort", "max", ["low", "high"])
    mark = max(fake.messages)
    sent = [await user_message(b, fake, x) for x in ("!effort max", "!approve", "!deny")]
    assert replies(fake, mark, sent) == []
    sent += [await user_message(b, fake, x) for x in ("!queue", "!queue clear")]
    assert replies(fake, mark, sent) == ["0 messages held.", "Queue cleared."]


async def test_an_unmentioned_setting_must_be_verifiable(bridge):
    """Review of #730, R9: a `!model` without a mention reaches the backend inside
    `verified_only`, so that one that can't check the value refuses it (and so says nothing);
    a named one doesn't."""
    from zab.core.reason import must_verify

    b, fake, backend = bridge
    actor = await started(b, fake, backend)
    await actor.event(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    await actor.inbox.join()
    seen = []

    async def update(session_id, settings):
        seen.append(must_verify.get())
        return settings

    backend.update_settings.side_effect = update
    await user_message(b, fake, "!model gpt-5")
    await user_message(b, fake, "@**Test Bot** !model gpt-5")
    assert seen == [True, False]


async def test_detach_says_a_mention_is_needed_now(bridge):
    """Review of #730, R2: after `!detach`, a message without a mention doesn't reach the bot in
    the topic; its reply says so."""
    b, fake, backend = bridge
    await started(b, fake, backend)
    await user_message(b, fake, "@**Test Bot** !detach")
    assert any(
        "Mention me to start a new session in this topic." in m["content"]
        for m in fake.messages.values()
    )


async def test_a_completed_sign_in_drops_the_cached_answer(bridge):
    """Review of #730: after `!login`, the bot's sign-in is asked again at its next message
    without a mention, in every bridge's cache, not `SIGNED_IN_CACHE_S` later."""
    b, fake, backend = bridge
    await other_bot(b, fake)
    other = b.all_bridges[-1]
    other.sign_ins = {id(b): (time.monotonic(), False)}
    b.sign_ins[id(b)] = (time.monotonic(), False)
    b.sign_ins[id(other)] = (time.monotonic(), True)

    async def login(account, send, receive):
        return True

    backend.login.side_effect = login
    await b.handle(
        {
            "type": "message",
            "message": {
                "id": 3001, "type": "private", "sender_id": OWNER, "content": "!login",
                "display_recipient": [{"id": OWNER}, {"id": 1}], "stream_id": 1,
                "subject": "topic",
            },
        }
    )  # fmt: skip
    task = b.login.task
    await asyncio.wait_for(task, 1)
    assert id(b) not in b.sign_ins and id(b) not in other.sign_ins
    assert id(other) in b.sign_ins  # another agent's answer stays
    backend.signed_in = AsyncMock(return_value=True)
    assert await b.siblings(message(), False) == ("Claude · X",)
    backend.signed_in.assert_awaited_once()


async def test_a_new_event_queue_reads_memberships_again(bridge):
    """Review of #730: a queue Zulip dropped took its subscription events with it, so the
    membership cache goes with it (`channel_members` reads again)."""
    from zab.zulip.client import ZulipError

    b, _fake, _backend = bridge
    b.subscribers[1] = (time.monotonic() + 180, {1, 20})
    b.client.events = AsyncMock(
        side_effect=[ZulipError("BAD_EVENT_QUEUE_ID", 400), asyncio.CancelledError()]
    )
    b.refresh_cards = AsyncMock()
    b.reconcile_messages = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert 1 not in b.subscribers
