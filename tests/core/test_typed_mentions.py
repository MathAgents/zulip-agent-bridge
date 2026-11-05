"""A typed `@claude` or `@codex` is a mention of the sender's bot of that kind (#713, SPEC §4.5
"Mention detection"), everywhere a completed mention counts.

The two routers share one store, as the VM's bridges do (`zab run`)."""

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core.router import Router
from zab.core.store import Store

pytestmark = pytest.mark.integration
KINDS = ["codex", "claude"]
NAMES = ["Codex · X", "Claude · X"]
OWNER = 20


@pytest.fixture
def pair(tmp_path):
    store = Store(tmp_path)
    routers = [
        Router(store, KINDS[i], i + 10, NAMES[i], frozenset({OWNER}), kind=KINDS[i])
        for i in range(2)
    ]
    yield store, routers
    store.close()


def route(routers, mid, text, *, topic="topic", sender=OWNER, **kw):
    """Each bot's route of one message, with Zulip's `mentioned` flag where a completed mention
    names it (none for a typed one)."""
    message = dict(
        id=mid, sender_id=sender, stream_id=1, subject=topic, content=text, type="stream"
    )
    return [
        r.route(
            message,
            flags=["mentioned"] if f"@**{NAMES[i]}" in text else [],
            sender_is_bot=False,
            private=True,
            other_bot_names={NAMES[1 - i]},
            multi_party=kw.get("multi_party", False),
            siblings=None if kw.get("multi_party") else (NAMES[1 - i],),
            sibling_kinds=kw.get("sibling_kinds", {NAMES[1 - i]: KINDS[1 - i]}),
        )
        for i, r in enumerate(routers)
    ]


def bind(store, i, card, topic="topic"):
    store.insert(
        "sessions", id=f"s{i}-{card}", bot=KINDS[i], backend=KINDS[i], native_id=f"n{i}-{card}",
        card_message_id=card, stream_id=1, topic=topic, workspace="/repo",
        workspace_mode="shared", repo_root="/repo", settings_json="{}", state="idle", shared=0,
        created_at=0, updated_at=0,
    )  # fmt: skip


def actions(result):
    return [r.action for r in result]


@pytest.mark.parametrize(
    ("text", "expected", "prompt"),
    [
        ("@codex prove it", ["new", "ignore"], "prove it"),
        ("@Claude prove it", ["ignore", "new"], "prove it"),
        ("@CODEX: prove it", ["new", "ignore"], "prove it"),
        ("compare with what @codex said", ["new", "ignore"], "compare with what @codex said"),
        ("@claude's idea, please", ["ignore", "new"], "@claude's idea, please"),
    ],
)
def test_a_typed_mention_of_one_bot(pair, text, expected, prompt):
    """Without Zulip's flag: the typed bot alone, a leading mention off the prompt and a later
    one left in it as written."""
    _, routers = pair
    result = route(routers, 1, text)
    assert actions(result) == expected
    assert result[expected.index("new")].text == prompt
    assert all(r.peers == () for r in result)


@pytest.mark.parametrize(
    "text",
    [
        "@claude @codex prove it",
        "@codex, @Claude: prove it",
        "@claude @**Codex · X** prove it",  # typed and completed, each way round
        "@**Claude · X|11** and @codex: prove it",
    ],
)
def test_a_typed_mention_of_both_or_with_a_completed_one_of_the_other(pair, text):
    store, routers = pair
    result = route(routers, 1, text)
    assert actions(result) == ["new", "new"]
    assert [r.peers for r in result] == [(NAMES[1],), (NAMES[0],)]
    for i in range(2):
        assert store.get(f"addressed_peers:{KINDS[i]}:1") == [NAMES[1 - i]]


def test_typed_mentions_in_a_session_topic_commands_and_asides(pair):
    store, routers = pair
    bind(store, 0, 2)
    bind(store, 1, 3)
    assert actions(route(routers, 4, "@codex !stop")) == ["command", "ignore"]
    assert route(routers, 5, "@claude: !status")[1].text == "!status"
    assert actions(route(routers, 6, "@codex // a note for people")) == ["ignore", "ignore"]
    assert actions(route(routers, 7, "@claude, @codex !stop")) == ["command", "command"]


@pytest.mark.parametrize(
    "text",
    [
        "`@codex` in code",
        "```quote\n@codex said so\n```\nwhat now?",
        "> @codex said so\n\nwhat now?",
        "\\@codex",
        "x@codex.ai",
        "@codexes",
        "@_**Codex · X** said",
    ],
)
def test_a_typed_mention_that_does_not_count_leaves_the_message_unmentioned(pair, text):
    """It isn't one: the message is a new topic's first without a mention, for both (#687)."""
    _, routers = pair
    assert actions(route(routers, 1, text)) == ["new", "new"]


def test_a_quoted_completed_mention_of_the_other_bot_no_longer_counts(pair):
    """Before #713 Claude took the quoted `@**Codex · X**` for a mention of Codex and ignored
    the message, and Codex, without Zulip's flag (Zulip silences mentions in a quote), did
    too: nobody answered. Now the quote is no address."""
    _, routers = pair
    text = "@_**Owner|20** [said](link):\n```quote\n@**Codex · X** prove it\n```\nand you?"
    assert actions(route(routers, 1, text)) == ["new", "new"]


def test_a_typed_mention_of_a_kind_the_vm_lacks_is_still_not_this_bot(pair):
    """`@codex` on a VM without a Codex bot (or one that doesn't allow the sender) names no bot
    of this VM, but it names one: Claude stays out, as for a mention of another bot."""
    _, routers = pair
    result = route(routers, 1, "@codex prove it", sibling_kinds={})
    assert actions(result) == ["new", "ignore"]


def test_a_typed_mention_in_a_multi_party_channel(pair):
    """Every prompt needs a mention there, and a typed one is one (SPEC §4.5)."""
    _, routers = pair
    assert actions(route(routers, 1, "chat", multi_party=True)) == ["ignore", "ignore"]
    assert actions(route(routers, 2, "@claude prove it", multi_party=True)) == ["ignore", "new"]


def test_a_sender_not_on_the_allowlist_is_ignored(pair):
    _, routers = pair
    assert actions(route(routers, 1, "@claude @codex prove it", sender=21)) == ["ignore"] * 2


async def test_the_bridge_reads_a_typed_mention_of_its_kind(bridge):
    """Through the bridge: `@codex` starts the Codex bot's session, without the flag; the VM's
    other bots that allow the sender are its `sibling_kinds`."""
    from types import SimpleNamespace

    b, fake, backend = bridge
    b.router.kind = "codex"
    fake.members.append(dict(user_id=40, full_name="Claude · X", is_bot=True))
    fake.members.append(dict(user_id=41, full_name="Claude · Y", is_bot=True))
    await b.refresh_users()
    for uid, allowlist in ((40, {OWNER}), (41, {99})):
        b.all_bridges.append(
            SimpleNamespace(
                router=SimpleNamespace(user_id=uid, allowlist=allowlist),
                backend=SimpleNamespace(kind="claude"),
            )
        )
    assert b.sibling_kinds(OWNER) == {"Claude · X": "claude"}
    await user_message(b, fake, "@claude what do you think?", topic="c")
    backend.create_session.assert_not_awaited()
    await user_message(b, fake, "@Codex what do you think?", topic="d")
    backend.create_session.assert_awaited_once()
    assert backend.start_turn.call_args.args[1].text.endswith("what do you think?")


def test_a_dm_command_after_a_typed_mention_of_this_bot(pair):
    """`@claude !help` in a DM to Claude's bot is `!help`; to Codex's, it is no command."""
    _, routers = pair
    message = dict(id=1, sender_id=OWNER, content="@claude !help", type="private")
    flags = dict(flags=[], sender_is_bot=False, private=True)
    claude = routers[1].route(message, **flags)
    assert (claude.action, claude.text) == ("dm", "!help")
    assert routers[0].route(dict(message, id=2), **flags).text == "@claude !help"


async def test_a_typed_mention_before_a_dm_login(bridge):
    """#709's reviewer on #713: `@codex !login` in a one-to-one DM to the Codex bot starts its
    sign-in, as `!login` does; `@claude !login` to it is no command."""
    import asyncio

    b, fake, backend = bridge
    b.router.kind = "codex"
    started = asyncio.Event()

    async def login(account, send, receive):
        started.set()
        return True

    backend.login.side_effect = login
    for mid, text in ((2001, "@claude !login"), (2002, "@Codex !login")):
        await b.handle(
            {
                "type": "message",
                "message": {
                    "id": mid, "type": "private", "sender_id": OWNER, "content": text,
                    "display_recipient": [{"id": OWNER}, {"id": 1}], "stream_id": 1,
                    "subject": "topic",
                },
            }
        )  # fmt: skip
        if mid == 2001:
            assert b.login.task is None
            assert "DMs only take commands" in list(fake.messages.values())[-1]["content"]
    task = b.login.task  # taken at once: the sign-in may end, and clear it, before the next line
    assert task is not None
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(task, 1)
    backend.login.assert_awaited_once()


def test_a_quoted_mention_of_this_bot_stays_in_the_prompt(pair):
    """Review of #723: the bot's own completed mention leaves the prompt, except in a quote,
    which counts as no address and arrives unchanged."""
    _, routers = pair
    text = "@codex compare:\n> @**Codex · X** said 42\n\nright, @**Codex · X**?"
    result = route(routers, 1, text)
    assert actions(result) == ["new", "ignore"]
    assert result[0].text == "compare:\n> @**Codex · X** said 42\n\nright, ?"


async def test_a_code_after_a_typed_mention_during_a_sign_in(bridge):
    """Review of #723: during a sign-in, `@codex CODE` passes on `CODE`, as `!login` after one
    starts it."""
    import asyncio

    b, _, backend = bridge
    b.router.kind = "codex"
    received = []

    async def login(account, send, receive):
        received.append(await receive(1))
        return True

    backend.login.side_effect = login
    for mid, text in ((2101, "@codex !login"), (2102, "@Codex: DECOY-CODE")):
        await b.handle(
            {
                "type": "message",
                "message": {
                    "id": mid, "type": "private", "sender_id": OWNER, "content": text,
                    "display_recipient": [{"id": OWNER}, {"id": 1}], "stream_id": 1,
                    "subject": "topic",
                },
            }
        )  # fmt: skip
        if mid == 2101:
            task = b.login.task
    await asyncio.wait_for(task, 1)
    assert received == ["DECOY-CODE"]
