"""Scenarios 1, 2 and 13 of issue #263: basics (with a new topic without a mention, #687, and a
typed mention, #713), catch-up, and zab-tester2 ignored."""

from __future__ import annotations

import asyncio
import time

import pytest

from e2e.harness.cleanup import BRIDGE_STOPPED, undo
from e2e.harness.flows import (
    ANSWER_S,
    DM_STATUS_RE,
    STATUS_RE,
    Chat,
    Turn,
    answer,
    echo,
    topic_name,
)
from e2e.harness.zulip_user import dm_narrow, mention, topic_narrow

pytestmark = pytest.mark.e2e
KINDS = ("claude", "codex")
#: `Bridge.wrong_bot_reply`: a human DMing someone else's bot is told, once an hour per bot,
#: whose it is and where theirs are. It is neither a command reply nor an agent's answer.
WRONG_BOT_HINT = "Yours are the two bots with your name"
#: The bot's reactions to a message delivered into a running turn (↪) or held for later (⏳)
STEERED, HELD = "21aa", "23f3"
#: A new topic's first answer to a trivial prompt: within about 15 s of Codex's, which takes
#: about 6 s (#450; Claude's took 74 s when the doctor's `auth status` left the refresh lock).
FIRST_ANSWER_TARGET_S = 20.0


@pytest.mark.scenario(1)
@pytest.mark.parametrize("kind", KINDS)
async def test_basics(kind, chat: Chat, scenario, nonce):
    """A DM round trip; a mention in a new topic; a follow-up without a mention, which
    continues the same session (it knows a codeword only that session was told). The VM's other
    agent gets the follow-up too, in a new session of its own (#713), whose answer this part
    doesn't wait for."""
    turn = await chat.dm(kind, "!status")
    _, took = await chat.reply(turn, DM_STATUS_RE, within=120)
    scenario.timing(f"{kind}: DM !status reply", took)

    topic, code, token = topic_name(1, nonce, kind), f"tulip-{nonce}", f"PONG-{nonce}"
    turn = await chat.say(kind, topic, f"Remember the codeword `{code}`. " + echo(token))
    _, took = await chat.reply(turn, answer(token))
    scenario.timing(
        f"{kind}: new topic, first answer", took, high=FIRST_ANSWER_TARGET_S, target=True
    )

    follow = await chat.say(
        kind,
        topic,
        "What was the codeword I gave you? Reply with exactly the codeword. Don't run any tools.",
        mention_bot=False,
    )
    _, took = await chat.reply(follow, answer(code))
    scenario.timing(f"{kind}: follow-up answer", took)


@pytest.mark.scenario(1)
async def test_new_topic_without_a_mention(chat: Chat, scenario, nonce):
    """#687: in the sandbox, whose only human is zab-tester, a new topic's first message
    without a mention gets an answer from each agent, and so does its follow-up without one
    (SPEC §4.5). Both agents must be signed in on the VM, as for the basics."""
    topic = topic_name(1, nonce, "both")
    first = await chat.user.send(chat.stream, topic, echo(f"BOTH-{nonce}"))
    for kind, took in await each_answers(chat, topic, first, f"BOTH-{nonce}"):
        scenario.timing(f"{kind}: new topic without a mention, its answer", took)
    again = await chat.user.send(chat.stream, topic, "And again: " + echo(f"AGAIN-{nonce}"))
    for kind, took in await each_answers(chat, topic, again, f"AGAIN-{nonce}"):
        scenario.timing(f"{kind}: its follow-up without a mention, the answer", took)


#: How long the bot a typed mention doesn't name is watched after the other's answer: its 👀
#: would come within seconds of the message, its answer by the time the named bot's does.
QUIET_S = 30.0


@pytest.mark.scenario(1)
async def test_typed_mention(chat: Chat, scenario, nonce):
    """#713: a typed `@codex`, plain text that Zulip's typeahead didn't complete, in a new topic
    of the sandbox: Codex answers it, and Claude neither answers nor reacts (SPEC §4.5,
    "Mention detection"). Then a follow-up without a mention reaches both: each answers it,
    Claude in a new session (rule 6.2). Both agents must be signed in on the VM, as for the
    basics."""
    topic, token = topic_name(1, nonce, "typed"), f"TYPED-{nonce}"
    narrow = topic_narrow(chat.stream, topic)
    sent = await chat.user.send(chat.stream, topic, "@codex " + echo(token))
    turn = Turn(narrow, sent, chat.bots["codex"], time.monotonic())
    _, took = await chat.reply(turn, answer(token))
    scenario.timing("codex: a typed @codex, its answer", took)
    others = await chat.user.silent(
        narrow, after_id=sent, senders=[chat.bots["claude"]], seconds=QUIET_S
    )
    first = others[0]["content"][:80] if others else ""
    assert not others, f"Claude answered a typed @codex: {first}"
    again = await chat.user.send(chat.stream, topic, "And you both: " + echo(f"BOTH-{token}"))
    for kind, took in await each_answers(chat, topic, again, f"BOTH-{token}"):
        scenario.timing(f"{kind}: the follow-up without a mention, its answer", took)


async def each_answers(chat: Chat, topic: str, mid: int, token: str) -> list[tuple[str, float]]:
    """Each agent's answer `token` to message `mid` in `topic`, at once: (kind, seconds)."""
    sent = time.monotonic()

    async def one(kind: str) -> tuple[str, float]:
        await chat.user.wait_reply(
            topic_narrow(chat.stream, topic), after_id=mid, senders=[chat.bots[kind]],
            pattern=answer(token), within=ANSWER_S,
        )  # fmt: skip
        return kind, time.monotonic() - sent

    return list(await asyncio.gather(*(one(kind) for kind in KINDS)))


@pytest.mark.scenario(2)
async def test_catch_up(chat: Chat, vm, scenario, nonce):
    """Stop the bridge, send a DM and a topic mention, start it: each is answered once."""
    vm.unit_action("stop")
    try:
        dm = await chat.dm("claude", "!status")
        token = f"CATCHUP-{nonce}"
        said = await chat.say("codex", topic_name(2, nonce), echo(token))
        await asyncio.sleep(5)
    finally:
        await undo(BRIDGE_STOPPED, lambda: vm.unit_action("start"))
    started = time.monotonic()
    await chat.reply(dm, DM_STATUS_RE, within=300)
    scenario.timing("DM answered, after the start", time.monotonic() - started)
    await chat.reply(said, answer(token))
    scenario.timing("topic answered, after the start", time.monotonic() - started)
    await asyncio.sleep(60)  # a second answer would come by now
    dm_answers = [m for m in await chat.bot_messages(dm) if "Bridge running" in m["content"]]
    topic_answers = [
        m
        for m in await chat.bot_messages(said)
        if token in m["content"] and not STATUS_RE.match(m["content"])
    ]
    assert len(dm_answers) == 1, f"the DM was answered {len(dm_answers)} times"
    assert len(topic_answers) == 1, f"the mention was answered {len(topic_answers)} times"
    # delivered once: a second delivery during the turn would be steered into it (one answer,
    # but ↪) or held for the next (⏳), so count the turns and look at the bot's reactions
    turns = await chat.statuses(said)
    assert len(turns) == 1, f"{len(turns)} turns for one caught-up message"
    reactions = (await chat.user.message(said.message_id)).get("reactions", [])
    extra = [r for r in reactions if r.get("user_id") == said.bot and r.get("emoji_code") in
             (STEERED, HELD)]  # fmt: skip
    assert not extra, f"the caught-up message was delivered again: {extra[0].get('emoji_name')}"


@pytest.mark.scenario(13)
async def test_tester2_is_ignored(rig, tester, tester2, bots, shared_stream, scenario, nonce):
    """`zab-tester2` is on no allowlist: no reply and no reaction, in a DM, in a new topic, and
    in a topic where zab-tester has a session (in the channel both testers share). There, a
    multi-party channel, zab-tester's own new topic without a mention goes to no bot either
    (SPEC §4.5, #687)."""
    stream, chat = shared_stream, Chat(tester, bots, rig.bots, shared_stream)
    topic = topic_name(13, nonce)
    own = await chat.say("claude", topic, echo(f"MINE-{nonce}"))
    await chat.reply(own, answer(f"MINE-{nonce}"))
    unmentioned = topic_name(13, nonce, "unmentioned")
    owners = await tester.send(stream, unmentioned, echo(f"NO-owner-{nonce}"))
    sent: list[tuple[list, int]] = []
    for kind, bot in bots.items():
        sent.append((dm_narrow(bot), await tester2.send_dm(bot, "!status")))
        other = topic_name(13, nonce, kind)
        text = f"{mention(chat.names[kind], bot)} " + echo(f"NO-{kind}-{nonce}")
        sent.append((topic_narrow(stream, other), await tester2.send(stream, other, text)))
    follow = await tester2.send(stream, topic, echo(f"NO-follow-{nonce}"))
    sent.append((topic_narrow(stream, topic), follow))
    mentioned = await tester2.send(
        stream, topic, f"{mention(chat.names['claude'], bots['claude'])} " + echo(f"NO-{nonce}")
    )
    sent.append((topic_narrow(stream, topic), mentioned))
    await asyncio.sleep(90)
    hints = 0
    for narrow, mid in sent:
        answers = await tester2.silent(narrow, after_id=mid, senders=bots.values(), seconds=0)
        where = narrow[0]["operator"]
        if where == "dm":  # the one-per-hour pointer to their own bots is no answer (SPEC §4.5)
            hint = [a for a in answers if WRONG_BOT_HINT in a["content"]]
            hints += len(hint)
            assert len(hint) <= 1, f"{len(hint)} wrong-bot hints in one DM conversation"
            answers = [a for a in answers if a not in hint]
        # silent() reports the bots' reactions on the message too (a 👀 is a response)
        assert not answers, f"a bot answered zab-tester2 ({where}): {answers[0]['content'][:80]}"
    answers = await tester.silent(
        topic_narrow(stream, unmentioned), after_id=owners, senders=bots.values(), seconds=0
    )
    first = answers[0]["content"][:80] if answers else ""
    assert not answers, f"a bot answered an unmentioned new topic, multi-party: {first}"
    scenario.notes.append(
        f"{len(sent)} messages from zab-tester2, none answered after 90 s "
        f"({hints} wrong-bot hint{'s' if hints != 1 else ''}); zab-tester's unmentioned new "
        "topic in the shared channel unanswered too"
    )
