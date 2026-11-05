"""The read mark end to end offline (issue #654): `BotBridge` on the fake Zulip, Claude on
`fake_claude.py` and Codex on `FakeAppServer`, with the rig of `test_fork.py`. A message sent
while a turn runs gets ↪️ when it is written into the turn, then ✅ beside it once the backend
reports that the agent took it in."""

from __future__ import annotations

import pytest
from test_fork import KINDS, STORY, make_rig, parent  # noqa: F401 - make_rig is the rig fixture

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", KINDS)
async def test_a_message_sent_during_a_turn_is_marked_read(make_rig, kind, monkeypatch):  # noqa: F811
    rig = await make_rig(kind)
    await parent(rig)
    rig.hold(True, monkeypatch)
    await rig.say(STORY)
    await rig.held(STORY)
    rig.hold(False, monkeypatch)
    # Claude's held turn (the `steer_text` fixture) takes this steer in and echoes it
    mid = await rig.say("Also add a one-line title at the top.")

    def marks():
        return [r["emoji_name"] for r in rig.fake.messages[mid]["reactions"]]

    await rig.until(lambda: "check" in marks(), "the read mark")
    if kind == "codex":
        await rig.release()  # FakeAppServer took the steer in at once; now it ends the turn
    turns = await rig.turns(2)
    assert marks() == ["arrow_right_hook", "check"]
    assert len(turns) == 2  # the parent's turn and the steered one: no turn of its own
