"""Scenario 1's new topic without a mention (#687), offline: the part runs against a fake
participant, so that its code is exercised before a live run."""

from __future__ import annotations

import re

from e2e import test_scenarios_basic as basic
from e2e.harness import report
from e2e.harness.flows import STATUS_RE, Chat

STREAM, BOTS = 5, {"claude": 41, "codex": 42}
NAMES = {"claude": "Claude · zab-test", "codex": "Codex · zab-test"}


class User:
    """zab-tester: what it sent, and each agent's answer, echoing the token it was asked for."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str]] = []
        self.waits: list[tuple[str, int, tuple[int, ...]]] = []
        self.watched: list[tuple[str, int, tuple[int, ...], float]] = []

    async def silent(self, narrow, *, after_id, senders, seconds):
        topic = next(n["operand"] for n in narrow if n["operator"] == "topic")
        self.watched.append((topic, after_id, tuple(senders), seconds))
        return []

    async def send(self, stream: int, topic: str, content: str) -> int:
        self.sent.append((stream, topic, content))
        return 100 + len(self.sent)

    async def wait_reply(self, narrow, *, after_id, senders, pattern, within):
        topic = next(n["operand"] for n in narrow if n["operator"] == "topic")
        self.waits.append((topic, after_id, tuple(senders)))
        token = re.search(r"`([^`]+)`", self.sent[after_id - 101][2])[1]
        assert re.search(pattern, token), "the wait would not take the agent's answer"
        assert not re.search(pattern, "✅ **Done** · " + token), "a status line is no answer"
        assert STATUS_RE.match("✅ **Done** · " + token)
        return {"id": after_id + 50, "sender_id": senders[0], "content": token}


async def test_scenario_1_new_topic_without_a_mention_waits_for_each_agent():
    user = User()
    row = report.ScenarioResult(1, "Basics, both agents")
    await basic.test_new_topic_without_a_mention(Chat(user, BOTS, NAMES, STREAM), row, "n")
    topic = "e2e-1-both-n"
    assert [(s, t) for s, t, _ in user.sent] == [(STREAM, topic)] * 2
    assert not any("@**" in content for _, _, content in user.sent), "no mention"
    assert sorted(user.waits) == sorted(
        (topic, mid, (bot,)) for mid in (101, 102) for bot in BOTS.values()
    )
    assert len(row.timings) == 4 and row.timings_ok


async def test_scenario_1_typed_mention_waits_for_codex_and_watches_claude():
    """#713's part of scenario 1: `@codex` typed, as plain text; Codex's answer; Claude's
    silence; then a follow-up without a mention, and each agent's answer."""
    user = User()
    row = report.ScenarioResult(1, "Basics, both agents")
    await basic.test_typed_mention(Chat(user, BOTS, NAMES, STREAM), row, "n")
    topic = "e2e-1-typed-n"
    (stream, sent_topic, content), (_, again_topic, again) = user.sent
    assert (stream, sent_topic, again_topic) == (STREAM, topic, topic)
    assert content.startswith("@codex ") and "@**" not in content, "typed, not completed"
    assert "@" not in again, "the follow-up mentions no bot"
    assert user.waits[0] == (topic, 101, (BOTS["codex"],))
    assert sorted(user.waits[1:]) == sorted((topic, 102, (bot,)) for bot in BOTS.values())
    assert user.watched == [(topic, 101, (BOTS["claude"],), basic.QUIET_S)]
    assert len(row.timings) == 3 and row.timings_ok
