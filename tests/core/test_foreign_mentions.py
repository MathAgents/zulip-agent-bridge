"""Owner-only correction of leading foreign workshop bot mentions (§4.5)."""

from types import SimpleNamespace

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_router import bind, message
from test_router import routing as routing

pytestmark = pytest.mark.integration
FOREIGN = "Codex · other"
OTHER_KIND = "Claude · other"


@pytest.mark.parametrize(
    "text,action",
    [
        (f"@**{FOREIGN}** do it", "new"),
        (f"@**{FOREIGN}|30** do it", "new"),
        (f"@**{OTHER_KIND}** @**{FOREIGN}** do it", "new"),
        (f"look at what @**{FOREIGN}** said", "ignore"),
        (f"@**{OTHER_KIND}** do it", "ignore"),
        ("@**Spare · dev-gpt** do it", "ignore"),
        (f"`@**{FOREIGN}**` do it", "ignore"),
        (f"\\@**{FOREIGN}** do it", "ignore"),
        (f"@_**{FOREIGN}** do it", "ignore"),
        (f"@**{FOREIGN}** // aside", "ignore"),
        (f"@**{FOREIGN}** @**codex dev-gpt** do it", "ignore"),  # no direct-mention flag
    ],
)
def test_only_leading_foreign_same_kind_is_corrected(routing, text, action):
    _, router = routing
    route = router.route(
        message(text),
        flags=[],
        sender_is_bot=False,
        private=True,
        other_bot_names={FOREIGN, OTHER_KIND, "Spare · dev-gpt"},
        foreign_kind_mentions={(FOREIGN, 30)},
    )
    assert route.action == action
    if action == "new":
        assert route.text == "do it"


@pytest.mark.parametrize("sender,is_bot", [(21, False), (20, True)])
def test_correction_never_bypasses_owner_or_bot_gate(routing, sender, is_bot):
    _, router = routing
    route = router.route(
        message(f"@**{FOREIGN}** go", sender=sender),
        flags=[],
        sender_is_bot=is_bot,
        private=True,
        other_bot_names={FOREIGN},
        foreign_kind_mentions={(FOREIGN, 30)},
    )
    assert route.action == "ignore"


def test_corrected_command_updates_addressee_and_preserves_normal_followups(routing):
    store, router = routing
    bind(store)
    route = router.route(
        message(f"@**{FOREIGN}** !stop"),
        flags=[],
        sender_is_bot=False,
        private=True,
        other_bot_names={FOREIGN},
        foreign_kind_mentions={(FOREIGN, 30)},
    )
    assert (route.action, route.text, route.peers) == ("command", "!stop", ())
    assert store.get("message_audience:codex:1")["addressed"] is True
    followup = router.route(
        message("followup", 2), flags=[], sender_is_bot=False, private=True, siblings=()
    )
    assert followup.action == "deliver"


@pytest.mark.parametrize(
    "enabled,configured,kind,expected",
    [
        (True, False, "codex", True),
        (False, False, "codex", False),
        (True, True, "codex", False),
        (True, False, "claude", False),
    ],
)
async def test_bridge_uses_configured_bot_ids_not_zulip_owner(
    bridge, enabled, configured, kind, expected
):
    b, fake, backend = bridge
    b.config.bridge.multi_party = enabled
    backend.kind = kind
    fake.members.append(dict(user_id=30, full_name=FOREIGN, is_bot=True, bot_owner_id=2))
    await b.refresh_users()
    if configured:
        b.all_bridges.append(
            SimpleNamespace(
                router=SimpleNamespace(user_id=30, allowlist={20}),
                backend=SimpleNamespace(kind="codex"),
            )
        )
        b.config.bots.append(b.bot)  # Configured under another backend; peers remains [b].
    # Both workshop bots have Drew's bot_owner_id; only the configured bridge decides ownership.
    await user_message(b, fake, f"@**{FOREIGN}** prompt")
    assert bool(backend.create_session.await_count) is expected


async def test_correction_waits_for_all_configured_identities(bridge):
    b, fake, backend = bridge
    backend.kind = "codex"
    fake.members.append(dict(user_id=30, full_name=FOREIGN, is_bot=True))
    await b.refresh_users()
    b.config.bots.append(b.bot)
    await user_message(b, fake, f"@**{FOREIGN}** prompt")
    backend.create_session.assert_not_awaited()


@pytest.mark.parametrize("target,expected", [(30, True), (40, False)])
async def test_duplicate_name_qualified_mention_preserves_bot_identity(bridge, target, expected):
    b, fake, backend = bridge
    backend.kind = "codex"
    fake.members.extend(dict(user_id=uid, full_name=FOREIGN, is_bot=True) for uid in (30, 40))
    b.all_bridges.append(
        SimpleNamespace(
            router=SimpleNamespace(user_id=40, allowlist={20}),
            backend=SimpleNamespace(kind="codex"),
        )
    )
    b.config.bots.append(b.bot)
    await b.refresh_users()
    await user_message(b, fake, f"@**{FOREIGN}|{target}** prompt")
    assert bool(backend.create_session.await_count) is expected


@pytest.mark.parametrize("separator", [", ", ": ", ",:"])
def test_corrected_leading_list_drops_separators_and_only_corrected_peers(routing, separator):
    store, router = routing
    route = router.route(
        message(f"@**{FOREIGN}|30**{separator}@**{OTHER_KIND}**: do it"),
        flags=[],
        sender_is_bot=False,
        private=True,
        other_bot_names={FOREIGN, OTHER_KIND},
        foreign_kind_mentions={(FOREIGN, 30)},
        foreign_peer_replacements={(FOREIGN, 30): None, (OTHER_KIND, 31): "Claude · local"},
    )
    assert (route.action, route.text, route.peers) == ("new", "do it", ("Claude · local",))
    assert store.get("addressed_peers:codex:1") == ["Claude · local"]


@pytest.mark.parametrize("direct", [False, True])
async def test_leading_foreign_other_kind_becomes_local_sibling_and_stays_local(bridge, direct):
    b, fake, backend = bridge
    backend.kind = "codex"
    fake.members.extend(
        [
            dict(user_id=30, full_name=FOREIGN, is_bot=True),
            dict(user_id=31, full_name=OTHER_KIND, is_bot=True),
            dict(user_id=40, full_name="Claude · local", is_bot=True),
        ]
    )
    b.all_bridges.append(
        SimpleNamespace(
            router=SimpleNamespace(user_id=40, allowlist={20}),
            backend=SimpleNamespace(kind="claude"),
            bot=SimpleNamespace(name="claude"),
        )
    )
    b.config.bots.append(b.bot)
    await b.refresh_users()
    own = "Test Bot" if direct else FOREIGN
    fake.subscribers[1].append(40)  # Claude · local is in the channel: a peer (#713)
    await user_message(b, fake, f"@**{own}** @**{OTHER_KIND}|31** do it")
    actor = b.actors["native"]
    await actor.inbox.join()
    assert "Addressed to you and to Claude · local" in backend.start_turn.call_args.args[1].text
    await user_message(b, fake, "followup")  # no mention: both of the VM's bots (#713)
    await actor.inbox.join()
    assert "Addressed to you and to Claude · local" in backend.steer.call_args.args[2].text


def test_leading_separator_keeps_emoji_colon(routing):
    _, router = routing
    route = router.route(
        message("@**codex dev-gpt** :tada: works now?"),
        flags=["mentioned"],
        sender_is_bot=False,
        private=True,
    )
    assert route.text == ":tada: works now?"


@pytest.mark.parametrize("qualified", [False, True])
def test_dropped_foreign_peer_does_not_inherit_local_audience(routing, qualified):
    store, router = routing
    bind(store)
    router.route(
        message("@**codex dev-gpt** work"),
        flags=["mentioned"],
        sender_is_bot=False,
        private=True,
    )
    name = OTHER_KIND + ("|31" if qualified else "")
    route = router.route(
        message(f"@**{name}** !stop", 2),
        flags=[],
        sender_is_bot=False,
        private=True,
        other_bot_names={OTHER_KIND},
        # Both missing and ambiguous configured siblings map to None.
        foreign_peer_replacements={(OTHER_KIND, 31): None},
    )
    assert route.action == "ignore"
    audience = store.get("message_audience:codex:2")
    assert (audience["addressed"], audience["peers"]) == (False, [])
    followup = router.route(
        message("followup", 3), flags=[], sender_is_bot=False, private=True, siblings=()
    )
    assert followup.action == "deliver"  # no mention: whatever the one before addressed
