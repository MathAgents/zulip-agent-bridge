"""Issue #655: every spec the core builds carries its zab session's id, which the backends export
as ZAB_SESSION so that `zab job` knows the session that started a background job (SPEC §5.6,
§7.14). The core chooses the id before it creates, forks or attaches the native session, so that
the session's first turn has it, and the row it then inserts has that same id."""

from dataclasses import replace

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_detached import detach
from test_fork import make_rig as make_rig
from test_fork import parent

from zab.core import detached
from zab.core import types as t
from zab.core.recovery import session_spec

pytestmark = pytest.mark.integration
KINDS = ("claude", "codex")


async def test_create_reopen_and_recovery_carry_the_rows_id(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    session = actor.row["id"]
    assert backend.create_session.call_args.args[0].session == session
    assert b.store.rows("sessions", id=session)  # the row has the id the first turn had
    assert session_spec(actor).session == session  # restart, reconnect and !status reopen
    backend.open_session.return_value = backend.create_session.return_value
    await b.reopen(actor)
    await actor.inbox.join()
    assert backend.open_session.call_args.args[1].session == session


async def test_attach_carries_the_new_rows_id(bridge):
    b, fake, backend = bridge
    backend.open_session.return_value = backend.create_session.return_value
    await user_message(b, fake, "@**Test Bot** !attach native")
    session = b.actors["native"].row["id"]
    assert backend.open_session.call_args_list[0].args[1].session == session


async def test_a_new_session_in_a_topic_gets_a_new_id(bridge):
    """After !detach a mention starts a new session there: its own id, not the detached one's."""
    b, fake, backend = bridge
    actor, _ = await detach(b, fake)
    await user_message(b, fake, "@**Test Bot** begin again")
    spec = backend.create_session.call_args.args[0]
    assert spec.session != actor.row["id"]
    assert b.store.rows("sessions", id=spec.session)[0]["state"] != "detached"


async def test_detached_recovery_without_an_actor_carries_the_detached_rows_id(bridge):
    b, fake, backend = bridge
    actor, _ = await detach(b, fake)
    backend.open_session.return_value = replace(
        backend.create_session.return_value,
        last_turn=t.TurnSummary(turn_id="turn1", status=t.TurnStatus.COMPLETED),
    )
    await detached.recover(b)
    assert backend.open_session.call_args.args[1].session == actor.row["id"]


@pytest.mark.parametrize("kind", KINDS)
async def test_the_tools_of_a_session_and_of_its_fork_see_their_own_ids(make_rig, kind):
    """Through the real backends: the fake Claude logs each turn's environment, and the Codex
    app-server gets the shell policy that its tool processes run with (§5.6)."""
    rig = await make_rig(kind)
    top = await parent(rig)
    await rig.say("!fork --name side check the first cases")
    await rig.turns(1, "topic ⑂ side")
    child = rig.row("topic ⑂ side")
    assert top["id"] != child["id"]
    if kind == "claude":
        seen = [call["env"]["ZAB_SESSION"] for call in rig.calls()]
        assert seen == [top["id"], child["id"]]
    else:
        (start,) = rig.server.requests("thread/start")
        (fork,) = rig.server.requests("thread/fork")
        assert start["config"]["shell_environment_policy.set"]["ZAB_SESSION"] == top["id"]
        assert fork["config"]["shell_environment_policy.set"]["ZAB_SESSION"] == child["id"]
        assert "ZAB_SESSION" in fork["config"]["shell_environment_policy.exclude"]
