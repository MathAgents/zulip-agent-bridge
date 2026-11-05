"""The marker on the bridge's own messages (SPEC §4.9, B.4; issue #677).

The other kinds of marked message are checked where they are tested already: the failed turn's
error (`test_session`), the recovery line (`test_reopen`), fork failures (`test_fork`), expired
messages (`test_catchup`, `test_zulip_outage`), the stopped background job and the denial
notice (`test_claude_stream`), lost steers (`test_claude_steer`) and Codex's notices.
"""

from unittest.mock import AsyncMock

import pytest
from test_session import actor as actor
from test_session import send

from zab.core import presentation as view
from zab.core import types as t
from zab.core.peer_context import CONTROL
from zab.core.session import StopTimeout

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("kind", "level", "expected"),
    [
        ("claude", "warning", "⚠️ **Bridge** (not Claude): x"),
        ("claude", "error", "⚠️ **Bridge** (not Claude): x"),
        ("codex", "info", "🔧 **Bridge** (not Codex): x"),
        ("other", "warning", "⚠️ **Bridge** (not the agent): x"),
    ],
)
def test_the_marker_names_the_bridge_and_the_agent_it_is_not(kind, level, expected):
    assert view.bridge_note(kind, "x", level) == expected


@pytest.mark.parametrize("level", ["info", "warning", "error"])
def test_peer_context_takes_marked_messages_for_control_lines(level):
    """Criterion 4: the other agent never reads them as an agent's words."""
    for kind in ("claude", "codex"):
        assert CONTROL.match(view.bridge_note(kind, "blocked\n- ` rm -f runs/* `", level))
    for body in ("**Bridge** (not Claude): x", "⚠️ Bridge is down", "🔧 **Bridge** says hi"):
        assert not CONTROL.match(body)


@pytest.mark.parametrize(
    ("kind", "level", "marker"),
    [
        ("claude", "warning", "⚠️ **Bridge** (not Claude): "),
        ("codex", "error", "⚠️ **Bridge** (not Codex): "),
        ("codex", "info", "🔧 **Bridge** (not Codex): "),
    ],
)
async def test_a_backend_notice_is_posted_as_the_bridges(actor, kind, level, marker):
    a, fake, backend = actor
    backend.kind = kind
    before = set(fake.messages)
    text = "Something was blocked:\n- ` rm -f runs/* `"
    await a.event(t.Notice(session_id="native", level=level, message=text))
    assert [m["content"] for i, m in fake.messages.items() if i not in before] == [marker + text]


async def test_a_notice_is_redacted_before_the_marker(actor):
    """The marker would defeat the zuliprc rule, which matches whole lines (§11.6, review)."""
    a, fake, _backend = actor
    key = "Q7xK" + "a1B2" * 7
    for raw in (f"key={key}", f"key={key[:16]}\x00{key[16:]}"):  # NULs dropped first (#678)
        await a.event(t.Notice(session_id="native", level="error", message=raw))
    assert not any(key[:8] in m["content"] for m in fake.messages.values())
    assert sum("[redacted" in m["content"] for m in fake.messages.values()) == 2


async def test_a_failed_turns_error_is_redacted_before_the_marker(actor):
    a, fake, _backend = actor
    key = "Q7xK" + "a1B2" * 7
    await send(a, fake, "first")
    error = t.ErrorInfo(
        kind=t.ErrorKind.BACKEND_UNAVAILABLE, message=f"key={key[:16]}\x00{key[16:]}"
    )
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.FAILED, error=error
        )
    )
    posted = [m["content"] for m in fake.messages.values()]
    assert any("[redacted" in p for p in posted) and not any(key[:8] in p for p in posted)


async def test_a_stop_without_confirmation_is_the_bridges_answer(actor):
    a, fake, backend = actor
    backend.kind = "claude"
    await send(a, fake, "first")
    a.put(StopTimeout("turn1"))
    await a.inbox.join()
    note = "⚠️ **Bridge** (not Claude): No stop confirmation after 30 seconds."
    assert any(m["content"] == note for m in fake.messages.values())


async def test_a_failed_session_operation_is_the_bridges_warning(actor):
    a, fake, backend = actor
    backend.kind = "codex"
    a.event = AsyncMock(side_effect=RuntimeError("private"))
    a.put(t.Notice(session_id="native", level="info", message="x"))
    await a.inbox.join()
    posted = [m["content"] for m in fake.messages.values()]
    assert posted[-1] == "⚠️ **Bridge** (not Codex): RuntimeError; use !status to retry."
