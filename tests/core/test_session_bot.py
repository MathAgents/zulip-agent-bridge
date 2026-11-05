"""Issue #248: every spec the core builds names its session's bot, which the backends export
as ZAB_BOT so that `zab history` in a turn reads as that bot (SPEC §5.6)."""

from dataclasses import replace

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_detached import detach

from zab.core import detached
from zab.core import types as t
from zab.core.recovery import session_spec

pytestmark = pytest.mark.integration


async def test_create_reopen_and_recovery_name_the_bot(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    assert backend.create_session.call_args.args[0].bot == b.bot.name == "bot"
    actor = b.actors["native"]
    assert session_spec(actor).bot == "bot"  # restart, reconnect and attach refresh
    backend.open_session.return_value = backend.create_session.return_value
    await b.reopen(actor)
    await actor.inbox.join()  # reconnects and restarts reopen through the same actor step
    assert backend.open_session.call_args.args[1].bot == "bot"


async def test_attach_names_the_bot(bridge):
    b, fake, backend = bridge
    backend.open_session.return_value = backend.create_session.return_value
    await user_message(b, fake, "@**Test Bot** !attach native")
    assert backend.open_session.call_args_list[0].args[1].bot == "bot"
    assert b.actors["native"].row["bot"] == "bot"


async def test_a_session_spec_names_the_bot_whose_session_it_is(bridge):
    """A peer bot that owns the native session now (cross-bot attach) gets its own name."""
    b, fake, _ = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    actor.row = actor.row | {"bot": "peer-bot"}
    assert session_spec(actor).bot == "peer-bot"


async def test_detached_recovery_without_an_actor_names_the_bot(bridge):
    b, fake, backend = bridge
    await detach(b, fake)
    backend.open_session.return_value = replace(
        backend.create_session.return_value,
        last_turn=t.TurnSummary(turn_id="turn1", status=t.TurnStatus.COMPLETED),
    )
    await detached.recover(b)
    assert backend.open_session.call_args.args[1].bot == "bot"


async def test_detached_recovery_of_a_peer_owned_session_names_the_owner(bridge):
    """The detached row is this bot's, but a peer bot's actor owns the native session now
    (cross-bot attach): the reopen is the owner's session, so it carries the owner's bot."""
    from types import SimpleNamespace

    b, fake, backend = bridge
    actor, _ = await detach(b, fake, forget=False)
    b.actors.pop("native")
    actor.row = actor.row | {"bot": "peer-bot"}
    b.peers.append(SimpleNamespace(bot=b.bot, actors={"native": actor}))
    backend.open_session.return_value = replace(
        backend.create_session.return_value,
        last_turn=t.TurnSummary(turn_id="turn1", status=t.TurnStatus.COMPLETED),
    )
    try:
        await detached.recover(b)
        assert backend.open_session.call_args.args[1].bot == "peer-bot"
    finally:
        await actor.close()
