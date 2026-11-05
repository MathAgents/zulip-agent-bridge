import pytest

from zab.core.router import Router
from zab.core.store import Store

pytestmark = pytest.mark.integration


@pytest.fixture
def routing(tmp_path):
    store = Store(tmp_path)
    router = Router(store, "codex", 10, "codex dev-gpt", frozenset({20}))
    yield store, router
    store.close()


def message(content, mid=1, sender=20, topic="topic"):
    return dict(
        id=mid, sender_id=sender, stream_id=1, subject=topic, content=content, type="stream"
    )


def bind(store):
    store.insert(
        "sessions",
        id="s",
        bot="codex",
        backend="codex",
        native_id="n",
        card_message_id=100,
        stream_id=1,
        topic="topic",
        workspace="/repo",
        workspace_mode="shared",
        repo_root="/repo",
        settings_json="{}",
        state="idle",
        created_at=0,
        updated_at=0,
    )


@pytest.mark.parametrize(
    "text,flags,action",
    [
        ("@**codex dev-gpt** hello", ["mentioned"], "new"),
        ("@**codex dev-gpt|10** hello", ["mentioned"], "new"),
        ("@**codex dev-gpt|11** hello", ["mentioned"], "ignore"),
        ("@**codex dev-gpt** hello", [], "ignore"),
        ("@_**codex dev-gpt** hello", ["mentioned"], "ignore"),
        ("@**all** hello", ["mentioned"], "ignore"),
        ("@**codex dev-gpt** !help", ["mentioned"], "command"),
        ("@**codex dev-gpt** !stop", ["mentioned"], "reply"),
        ("@**codex dev-gpt** // aside", ["mentioned"], "ignore"),
    ],
)
def test_initial_routing(routing, text, flags, action):
    _, router = routing
    assert (
        router.route(message(text), flags=flags, sender_is_bot=False, private=True).action == action
    )


@pytest.mark.parametrize(
    "text, action, routed",
    [
        ("!Stop", "command", "!stop"),
        ("`!stop`", "command", "!stop"),
        ("@**codex dev-gpt** `!Status`", "command", "!status"),
        ("!File Results/T.csv", "command", "!file Results/T.csv"),
        ("!Login", "reply", "Send !login to me in a direct message."),
        ("`!x` is not a command here", "deliver", "`!x` is not a command here"),
    ],
)
def test_topic_commands_as_people_type_them(routing, text, action, routed):
    """#709: in a session's topic, a command's verb in any case, or the whole message one code
    span; what follows the verb, and any prompt, as typed."""
    store, router = routing
    bind(store)
    flags = ["mentioned"] if text.startswith("@") else []
    # a VM of one bot, in a single-human channel: a message without a mention reaches it (#713)
    route = router.route(message(text), flags=flags, sender_is_bot=False, private=True, siblings=())
    assert (route.action, route.text) == (action, routed)


def test_dm_commands_reach_the_bridge_as_typed_commands(routing):
    """#709: the DM route carries the command, without the bot's leading mention."""
    _, router = routing
    dm = dict(message("@**codex dev-gpt** `!Status`"), type="private")
    assert router.route(dm, flags=[], sender_is_bot=False, private=True).text == "!status"
    dm = dict(message("a pasted code `!x`", 2), type="private")
    assert router.route(dm, flags=[], sender_is_bot=False, private=True).text == dm["content"]


@pytest.mark.parametrize(
    "text, routed",
    [
        ("@codex !Help", "!help"),  # a typed mention of its kind (#713), then the verb (#709)
        ("@Codex `!login cancel`", "!login cancel"),
        ("@codex: `` !VM ``", "!vm"),
        ("@codex @**codex dev-gpt** !Status", "!status"),  # typed, then completed
        ("@codex CODE-123", "CODE-123"),  # a pasted code: no command, without the typed mention
        ("@claude !help", "@claude !help"),  # another kind's: no address of this bot's
        # completed, then typed: the typed mention isn't first, so it is no address (#713)
        ("@**codex dev-gpt** @codex !help", "@**codex dev-gpt** @codex !help"),
    ],
)
def test_a_dm_composes_a_typed_mention_with_the_typed_forms(tmp_path, text, routed):
    """`as_command(without_typed_address(text, kind), mention)`, as the router's DM route, the
    sign-in and the sentinel read a one-to-one DM (#709 on #713)."""
    store = Store(tmp_path)
    try:
        router = Router(store, "codex", 10, "codex dev-gpt", frozenset({20}), kind="codex")
        dm = dict(message(text), type="private")
        assert router.route(dm, flags=[], sender_is_bot=False, private=True).text == routed
    finally:
        store.close()


def test_allowlist_and_bots_are_ignored(routing):
    _, router = routing
    for mid, sender, is_bot in [(1, 21, False), (2, 20, True), (3, 10, True)]:
        assert (
            router.route(
                message("@**codex dev-gpt** run", mid, sender),
                flags=["mentioned"],
                sender_is_bot=is_bot,
                private=True,
            ).action
            == "ignore"
        )


def test_public_refusal_once_and_empty_topic(routing):
    _, router = routing
    text = "@**codex dev-gpt** hello"
    assert (
        router.route(message(text), flags=["mentioned"], sender_is_bot=False, private=False).action
        == "note"  # posted marked as the bridge's (#677)
    )
    assert (
        router.route(
            message(text, 2), flags=["mentioned"], sender_is_bot=False, private=False
        ).action
        == "ignore"
    )
    assert (
        router.route(
            message(text, 3, topic=""), flags=["mentioned"], sender_is_bot=False, private=True
        ).action
        == "note"
    )


def test_followup_dedup_shared_and_card_move(routing):
    """A follow-up without a mention, on a VM of one bot in a single-human channel (`siblings`
    empty, SPEC §4.5 rule 6.2); without `siblings` (a multi-party channel, say), none."""
    store, router = routing
    bind(store)
    follow = dict(flags=[], sender_is_bot=False, private=True, siblings=())
    assert router.route(message("continue"), **follow).action == "deliver"
    assert router.route(message("continue"), **follow).action == "ignore"
    router.route(message("**zab session**", 2, 30), flags=[], sender_is_bot=True, private=True)
    assert router.route(message("!stop", 3), **follow).action == "command"
    assert (
        router.route(message("!stop", 5), flags=[], sender_is_bot=False, private=True).action
        == "ignore"
    )
    assert (
        router.route(
            message("@**codex dev-gpt** !stop", 4),
            flags=["mentioned"],
            sender_is_bot=False,
            private=True,
        ).action
        == "command"
    )
    assert not router.move_card({"message_ids": [999], "subject": "other"}, private=True)
    moved = router.move_card(
        {"message_ids": [100], "subject": "✔ renamed", "new_stream_id": 2}, private=True
    )
    assert moved[0]["topic"] == "✔ renamed"
    assert moved[0]["stream_id"] == 2
    assert router.move_card({"message_ids": [100]}, private=False)[0]["state"] == "readonly"


def test_occupied_card_destination_detaches_incoming_binding(routing):
    store, router = routing
    bind(store)
    other = store.rows("sessions")[0] | {
        "id": "other",
        "native_id": "n2",
        "card_message_id": 101,
        "topic": "occupied",
    }
    store.insert("sessions", **other)
    (moved,) = router.move_card({"message_ids": [100], "subject": "occupied"}, private=True)
    assert moved["topic"] == "occupied" and moved["state"] == "detached"
    assert (
        router.route(message("continue"), flags=[], sender_is_bot=False, private=True).action
        == "ignore"
    )
    assert store.rows("sessions", id="other")[0]["state"] == "idle"
