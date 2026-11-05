"""Scenarios 5 and 7 of issue #263: steering, and the agents' commit identity (#214)."""

from __future__ import annotations

import re

import pytest

from e2e.harness import github
from e2e.harness.flows import WORKING_S, Chat, answer, topic_name

pytestmark = pytest.mark.e2e
KINDS = ("claude", "codex")
STEER_CODE = "21aa"  # ↪ (`arrow_right_hook`, zab.core.bridge)
READ_CODE = "2705"  # ✅ (`check`, zab.core.bridge): the agent took the steer in (#654)


@pytest.mark.scenario(5)
@pytest.mark.parametrize("kind", KINDS)
async def test_steering(kind, chat: Chat, scenario, nonce):
    """A message sent mid-turn gets ↪, then ✅ once the agent takes it in, at the end of the
    running `sleep` (#654), and is answered within the same turn: the turn's one answer
    carries the word it asked for, and no second turn starts."""
    token, steer = f"DONE-{nonce}", f"STEER-{nonce}"
    turn = await chat.say(
        kind,
        topic_name(5, nonce, kind),
        f"Run the shell command `sleep 150`. When it has finished, reply with exactly `{token}`, "
        "followed by any extra words I ask you to add while you work.",
    )
    # the turn is running: its status shows the `sleep` at the first look after it started,
    # a minute into the turn at the earliest (#754), so the sleep runs on for the steer
    await chat.reply(turn, r"^⏳ \*\*Working\*\*.*sleep", within=WORKING_S)
    mid = await chat.user.send(chat.stream, topic_name(5, nonce, kind), f"Also add `{steer}`.")
    await chat.user.wait_reaction(mid, emoji_code=STEER_CODE, user_id=turn.bot, within=60)
    # read when the agent's next step begins: at the latest once the 150 s sleep is over,
    # about 90 s after the steer
    await chat.user.wait_reaction(mid, emoji_code=READ_CODE, user_id=turn.bot, within=180)
    reply, took = await chat.reply(turn, answer(re.escape(token)), within=600)
    scenario.timing(f"{kind}: steered turn's answer", took)
    assert steer in reply["content"], f"the answer lacks the steered word: {reply['content'][:200]}"
    statuses = await chat.statuses(turn)
    assert len(statuses) == 1, f"{len(statuses)} turns ran; the steer must not start another"


@pytest.mark.scenario(7)
@pytest.mark.parametrize("kind", KINDS)
async def test_commit_identity(kind, chat: Chat, rig, vm, scenario, nonce):
    """A commit by each agent has exactly one author, the bot (`Claude · zab-test`, `Codex ·
    zab-test`), and no Co-Authored-By trailer (#214). Checked on GitHub when the agent pushed,
    else in the VM's checkout."""
    path = f"e2e/{kind}-{nonce}.txt"
    turn = await chat.say(
        kind,
        topic_name(7, nonce, kind),
        f"Create the file `{path}` containing `{nonce}`, commit it with the message "
        f"`e2e {nonce}`, push the branch to origin, then reply with the full commit hash only.",
    )
    reply, took = await chat.reply(turn, answer(r"\b[0-9a-f]{40}\b"), within=600)
    scenario.timing(f"{kind}: commit answer", took)
    sha = re.findall(r"\b[0-9a-f]{40}\b", reply["content"])[-1]
    try:
        seen = github.commit_authorship(rig.repo, sha)
        where = "GitHub"
    except RuntimeError as exc:  # not pushed: the commit is in the VM's repository anyway
        checkout = f"/home/agent/work/{rig.repo.split('/')[-1]}"
        show = ["git", "-C", checkout, "show", "-s", f"--format={github.GIT_SHOW_FORMAT}", sha]
        text = vm.run(["sudo", "-u", "agent", "-H", *show]).stdout
        seen, where = github.parse_git_show(sha, text), f"the VM ({str(exc)[:80]})"
    scenario.notes.append(f"{kind}: {sha[:10]} checked on {where}")
    # the commit this run asked for, not any hash the agent named (reviewer on #284)
    assert seen.subject == f"e2e {nonce}", f"{sha[:10]} is {seen.subject!r}, not this run's"
    problems = seen.problems(rig.bots[kind])
    assert not problems, f"{kind} commit {sha[:10]}: " + "; ".join(problems)
