from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zab.commands.run import Runtime

pytestmark = pytest.mark.integration


async def test_detached_actor_still_receives_completion_to_release_slot():
    from zab.core.types import TurnCompleted, TurnStatus

    event = TurnCompleted(session_id="native", turn_id="t", status=TurnStatus.COMPLETED)
    actor = SimpleNamespace(row={"state": "detached"}, put=Mock())
    bridge = SimpleNamespace(
        bot=SimpleNamespace(name="bot", backend="codex"),
        actors={"native": actor},
        attaching={},
        store=SimpleNamespace(rows=lambda *args, **kwargs: []),
    )

    async def events():
        yield event

    runtime = Runtime.__new__(Runtime)
    runtime.bridges = [bridge]
    await runtime.pump("codex", SimpleNamespace(events=events))
    actor.put.assert_called_once_with(event)
