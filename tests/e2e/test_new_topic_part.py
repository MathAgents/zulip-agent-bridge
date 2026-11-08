"""Scenario 1's new topic without a mention (#687), typed mention (#713) and rule `last` (#878),
offline: the parts run against a fake participant, so that their code is exercised before a
live run."""

from __future__ import annotations

import re

import pytest

from e2e import test_scenarios_basic as basic
from e2e.harness import report
from e2e.harness.flows import RESET_FAILED, RULE_RESET_RE, STATUS_RE, Chat, reset_rule, set_rule
from zab.core.unmentioned import command

STREAM, BOTS = 5, {"claude": 41, "codex": 42}
NAMES = {"claude": "Claude · zab-test", "codex": "Codex · zab-test"}


class User:
    """zab-tester: what it sent, and each agent's answer, echoing the token it was asked for."""

    def __init__(self) -> None:
        self.rule: dict[str, str] = {}  # the bridge's store, for `!unmentioned`
        self.multi_party = True
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

    async def send_dm(self, bot: int, content: str) -> int:
        return await self.send(0, f"dm {bot}", content)

    async def wait_reply(self, narrow, *, after_id, senders, pattern, within):
        topic = next((n["operand"] for n in narrow if n["operator"] == "topic"), "dm")
        self.waits.append((topic, after_id, tuple(senders)))
        if (sent := self.sent[after_id - 101][2]).startswith("!unmentioned "):
            text = command(Kv(self.rule), "all", sent, self.multi_party)  # the bridge's (#878)
            assert text and re.search(pattern, text), "the wait would not take the answer"
            return {"id": after_id + 50, "sender_id": senders[0], "content": text}
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


class Kv:
    """The part of the bridge's store `unmentioned.command` uses."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


@pytest.mark.parametrize("multi_party", [True, False])
async def test_the_runs_reset_takes_the_bridges_answers(multi_party):
    """The `chat` fixture's reset to `all` at a run's start: the bridge's answer, set, or set
    with `bridge.multi_party` off; and a release without the setting's `Unknown command`."""
    from zab.core.commands import UNKNOWN

    user = User()
    user.multi_party = multi_party
    await set_rule(Chat(user, BOTS, NAMES, STREAM), "claude", "all", pattern=RULE_RESET_RE)
    assert user.sent == [(0, f"dm {BOTS['claude']}", "!unmentioned all")]
    assert user.rule == {"unmentioned": "all"}
    assert re.search(RULE_RESET_RE, UNKNOWN)
    assert not re.search(RULE_RESET_RE, command(Kv({}), "all", "!unmentioned last") or "")


class Unanswered(User):
    """A bridge that doesn't answer a DM."""

    async def wait_reply(self, narrow, *, after_id, senders, pattern, within):
        raise TimeoutError("no reply")


async def test_the_runs_reset_is_tried_once():
    tried: list[bool] = []
    user = User()
    await reset_rule(Chat(user, BOTS, NAMES, STREAM), tried)
    await reset_rule(Chat(user, BOTS, NAMES, STREAM), tried)
    assert tried == [True] and len(user.sent) == 1
    tried, user = [], Unanswered()
    with pytest.raises(TimeoutError):
        await reset_rule(Chat(user, BOTS, NAMES, STREAM), tried)
    with pytest.raises(AssertionError, match=re.escape(RESET_FAILED)):
        await reset_rule(Chat(user, BOTS, NAMES, STREAM), tried)
    assert len(user.sent) == 1  # the later use sent nothing and waited for nothing


class Answered(User):
    """Codex answers what it shouldn't: the part fails after `!unmentioned last`."""

    async def silent(self, narrow, *, after_id, senders, seconds):
        return [{"content": "an answer"}]


async def test_scenario_1_unmentioned_last_then_all():
    """#878's part of scenario 1: `!unmentioned last` to Codex's bot; a mention of Claude, a
    follow-up without one that Codex leaves alone; both named, then neither; `!unmentioned all`
    to Claude's bot, a mention of Claude and a follow-up both answer."""
    user = User()
    row = report.ScenarioResult(1, "Basics, both agents")
    await basic.test_unmentioned_last(Chat(user, BOTS, NAMES, STREAM), row, "n")
    last, back = "e2e-1-last-n", "e2e-1-all-n"
    sent = [(topic, content.split(" Reply")[0]) for _, topic, content in user.sent]
    claude = "@**Claude · zab-test|41**"
    both = f"{claude} @**Codex · zab-test|42**"
    assert sent == [
        (f"dm {BOTS['codex']}", "!unmentioned last"),
        (last, claude),
        (last, "And now:"),
        (last, both),
        (last, "Again:"),
        (f"dm {BOTS['claude']}", "!unmentioned all"),
        (back, claude),
        (back, "And you both:"),
    ]
    assert user.waits[2] == (last, 103, (BOTS["claude"],))
    assert user.watched == [(last, 103, (BOTS["codex"],), basic.QUIET_S)]
    assert sorted(user.waits[3:7]) == sorted(
        (last, mid, (bot,)) for mid in (104, 105) for bot in BOTS.values()
    )
    assert sorted(user.waits[-2:]) == sorted((back, 108, (bot,)) for bot in BOTS.values())
    assert len(row.timings) == 5 and row.timings_ok


async def test_scenario_1_unmentioned_last_restores_all_after_a_failure():
    user = Answered()
    row = report.ScenarioResult(1, "Basics, both agents")
    with pytest.raises(AssertionError, match="Codex took a follow-up to Claude under last"):
        await basic.test_unmentioned_last(Chat(user, BOTS, NAMES, STREAM), row, "n")
    assert user.sent[-1] == (0, f"dm {BOTS['claude']}", "!unmentioned all")
