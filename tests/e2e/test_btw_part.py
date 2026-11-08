"""Scenario 12's `!btw` helpers (#716), offline: the `lean-lsp-mcp` count and its sampling
while a reply is awaited, and the bridge log's side-question lines, against a fake VM, so that
their code is exercised before a live run."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from e2e import test_scenarios_features as features
from e2e.harness.flows import Chat, answer
from e2e.test_harness_zulip import BOT, Zulip, user
from zab.core import btw


class Vm:
    """`ps` answers each listing in turn (the last one again), `tail` the given log."""

    def __init__(self, listings: list[list[str]], log: str = "") -> None:
        self.listings, self.log, self.calls = listings, log, 0

    def run(self, argv, check=True, timeout=120):
        if argv[:1] == ["ps"]:
            listing = self.listings[min(self.calls, len(self.listings) - 1)]
            self.calls += 1
            return SimpleNamespace(ok=True, stdout="\n".join(listing))
        assert "tail" in argv and features.BRIDGE_LOG in argv, argv
        return SimpleNamespace(ok=True, stdout=self.log)


SERVER = "/home/agent/.local/share/uv/tools/lean-lsp-mcp/bin/python /usr/bin/lean-lsp-mcp"


def test_lean_servers_counts_the_servers_not_ps_itself():
    vm = Vm([[SERVER, SERVER, "ps -eo args=", "bash -c lean-lsp-mcp-free", "codex app-server"]])
    assert features.lean_servers(vm) == 2


async def test_the_peak_is_sampled_while_the_reply_is_awaited(monkeypatch):
    vm = Vm([[SERVER], [SERVER, SERVER, SERVER], [SERVER]])

    async def reply():
        await asyncio.sleep(0.1)
        return "answered"

    real_wait_for = asyncio.wait_for

    async def quick(awaitable, seconds):  # sample every 20 ms instead of 2 s
        return await real_wait_for(awaitable, min(seconds, 0.02))

    monkeypatch.setattr(features.asyncio, "wait_for", quick)
    waiting = asyncio.ensure_future(reply())
    assert await features.peak_lean_servers(vm, waiting) == 3
    assert await waiting == "answered" and vm.calls >= 3


def test_side_log_keeps_the_newest_line_of_each_event():
    lines = [
        {"event": "side_question_config", "msg": "old"},
        {"event": "zab.bridge", "msg": "unrelated"},
        {"event": "side_question_config", "msg": "config/read reported mcp_servers; off: …"},
        {"event": "side_question_mcp", "msg": "lean-lsp=disabled, lmfdb=disabled"},
    ]
    log = "\n".join([*(json.dumps(x) for x in lines), "not json", "[1, 2]"])
    assert features.side_log(Vm([[]], log)) == {
        "side_question_config": "config/read reported mcp_servers; off: …",
        "side_question_mcp": "lean-lsp=disabled, lmfdb=disabled",
    }


@pytest.mark.parametrize("slow", [True, False])
async def test_a_side_answer_quoting_the_done_word_is_not_the_running_turns_answer(slow):
    """#843: Claude's side answer (message 637909298 of run `20261105T175825Z-811be8f4`) said
    it hadn't sent the running turn's done word, quoting it, and the running turn's wait took
    it for that turn's answer: both waits returned the same message. A side answer is never
    a turn's answer (`flows.answer`); the side answer's own wait still finds it. The parts'
    heads are `btw.post`'s: a slow answer mentions its asker in its first part only, a fast
    one never (review of #844: a pattern needing the mention must fail here)."""
    fake = Zulip()
    chat = Chat(user(fake), {"claude": BOT}, {"claude": "Claude · zab-test"}, 5)
    topic, done = "e2e-12-btw-claude-x", "DONE-130739-214f17"
    running = await chat.say(
        "claude", topic, f"Run the shell command `sleep 150`. Then reply with exactly `{done}`."
    )
    side = await chat.say("claude", topic, "!btw Which command runs?", mention_bot=False)
    url = f"https://zulip.example/#narrow/channel/5/near/{side.message_id}"
    heads = [btw.marker(url, "@**zab-tester|10**" if slow else ""), btw.marker(url)]
    parts = [
        f"The current turn runs `sleep 150`; I therefore haven't sent `{done}`.",
        f"(2/2) Once it ends, it replies `{done}`.",  # a later part, cut from a long answer
    ]
    for i, part in enumerate(parts):
        fake.add(BOT, heads[min(i, 1)] + " " + part, stream=5, topic=topic)
        if i == 0:
            pattern = features.BTW_ANSWER.format(side.message_id)
            found, _ = await chat.reply(side, pattern, within=5)
        with pytest.raises(TimeoutError):
            await chat.reply(running, answer(re.escape(done)), within=5)
    fake.add(BOT, done, stream=5, topic=topic)
    ended, _ = await chat.reply(running, answer(re.escape(done)), within=5)
    assert found["id"] < ended["id"] and ended["content"] == done
