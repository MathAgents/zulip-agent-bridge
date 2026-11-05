"""Independent bridges agree on a message's addressees (SPEC §4.5 R1/R2): the bots it mentions,
and, with no mention, each of the VM's bots, whatever earlier messages addressed (#713)."""

import pytest

from zab.core.router import Router
from zab.core.store import Store

pytestmark = pytest.mark.integration
NAMES = ["Codex · X", "Claude · X"]


@pytest.fixture
def pair(tmp_path):
    stores = [Store(tmp_path / str(i)) for i in range(2)]
    routers = [Router(stores[i], str(i), i + 10, NAMES[i], frozenset({20})) for i in range(2)]
    yield stores, routers
    for store in stores:
        store.close()


def route(routers, mid, text, sender=20, bot=False, topic="topic"):
    """Each bot's route, in a single-human channel of a VM with both bots (`siblings`)."""
    return [
        r.route(
            dict(id=mid, sender_id=sender, stream_id=1, subject=topic, content=text, type="stream"),
            flags=["mentioned"] if f"@**{NAMES[i]}" in text else [],
            sender_is_bot=bot,
            private=True,
            other_bot_names={NAMES[1 - i]},
            siblings=(NAMES[1 - i],),
        )
        for i, r in enumerate(routers)
    ]


def bind(stores, i):
    stores[i].insert(
        "sessions",
        id="s",
        bot=str(i),
        backend=str(i),
        native_id="n",
        card_message_id=100 + i,
        stream_id=1,
        topic="topic",
        workspace="/repo",
        workspace_mode="shared",
        repo_root="/repo",
        settings_json="{}",
        state="idle",
        shared=1,
        created_at=0,
        updated_at=0,
    )


def actions(result):
    return [r.action for r in result]


@pytest.mark.parametrize("first", [0, 1])
def test_one_both_one_none_and_restart(pair, tmp_path, first):
    stores, routers = pair
    assert actions(route(routers, 1, f"@**{NAMES[first]}** first"))[1 - first] == "ignore"
    bind(stores, first)
    both = f"@**{NAMES[0]}** @**{NAMES[1]}|11**"
    result = route(routers, 2, both + " !new --model m work")
    assert result[first].action == "command" and result[1 - first].action == "new"
    assert all(r.text == "!new --model m work" for r in result)
    bind(stores, 1 - first)
    assert actions(route(routers, 3, "compare answers")) == ["deliver", "deliver"]
    for i in range(2):
        assert stores[i].get(f"addressed_peers:{i}:3") == [NAMES[1 - i]]
    assert actions(route(routers, 4, f"@**{NAMES[0]}** focus")) == ["deliver", "ignore"]
    # the mention of one decides nothing for the next message, before or after a restart
    assert actions(route(routers, 5, "and you?")) == ["deliver", "deliver"]
    for i in range(2):
        stores[i].close()
        stores[i] = Store(tmp_path / str(i))
        routers[i].store = stores[i]
    assert actions(route(routers, 6, f"@**{NAMES[1]}** you")) == ["ignore", "deliver"]
    assert actions(route(routers, 7, "!stop")) == ["command", "command"]
    assert actions(route(routers, 8, both + " !stop")) == ["command", "command"]
    assert all(r.text == "!stop" for r in route(routers, 9, both + " !stop"))


def test_asides_unauthorized_and_bot_messages_are_ignored(pair):
    stores, routers = pair
    for i in range(2):
        bind(stores, i)
    for mid, sender, bot, text in [
        (2, 21, False, "hello"),
        (3, 20, True, "hello"),
        (4, 20, False, "// aside"),
        (5, 20, False, f"@**{NAMES[1]}** // aside"),
    ]:
        assert actions(route(routers, mid, text, sender, bot)) == ["ignore", "ignore"]
    assert actions(route(routers, 6, "continue")) == ["deliver", "deliver"]


def test_the_shared_display_flag_does_not_gate_delivery(pair):
    stores, routers = pair
    for i in range(2):
        bind(stores, i)
    assert actions(route(routers, 1, "!stop")) == ["command", "command"]


def test_card_moves_keep_the_rule(pair):
    """Each card moves with its session; a message without a mention in the renamed topic goes
    to both, as everywhere: nothing about the topic's past is carried over."""
    stores, routers = pair
    for i in range(2):
        bind(stores, i)
    route(routers, 1, f"@**{NAMES[0]}** focus")
    for i, router in enumerate(routers):
        router.move_card({"message_ids": [100 + i], "subject": "renamed"}, private=True)
    assert actions(route(routers, 2, "!stop", topic="renamed")) == ["command", "command"]


@pytest.mark.parametrize(
    "example",
    [
        "Example: `@**Codex · X** !stop`",
        "```text\n@**Codex · X** !stop\n```",
        "~~~\n@**Codex · X**",
        r"Example: \@**Codex · X**",
        "```quote\n@**Codex · X** said so\n```",
    ],
)
def test_literal_and_quoted_mentions_address_no_one(pair, example):
    stores, routers = pair
    for i in range(2):
        bind(stores, i)
    route(routers, 1, "@**Claude · X** work")
    assert actions(route(routers, 2, example)) == ["deliver", "deliver"]
