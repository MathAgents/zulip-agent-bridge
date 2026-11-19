"""A `!new` that reaches a bot with a session in its topic (#985, SPEC §4.6). Named, or as the one
bot an unmentioned one reaches (under `last`, the record naming it), the bot answers that the
topic has its session and how to start a new one, and that the prompt wasn't sent when there was
one. Unmentioned beside the VM's other bots it says nothing, and a bot without a session starts
one; where every bot it reaches has a session, so that none starts one, the first of them by
name answers for the topic, naming their sessions (#987). It never starts a turn or a session of
the bot that has one. A `!new` in a DM is answered that it works in a channel topic, and that a
prompt wasn't sent (#987).

Before, each answered "Unknown command; use !help." and dropped the prompt without a word (#985),
and so did a DM; an unmentioned one where both bots had a session got no answer (#987).

Both bots in one process (`test_fork.make_pair`): Claude on `fake_claude.py`, Codex on
`FakeAppServer`, one store and one Zulip."""

import asyncio
import json

import pytest
from test_fork import BOTS, KINDS, OWNER, calls
from test_fork import make_pair as make_pair
from test_unmentioned_last import dm_to

from zab.core.commands import (
    NEW_DM,
    UNKNOWN,
    new_in_dm,
    new_with_session,
    new_with_sessions,
)

pytestmark = pytest.mark.integration
CLAUDE, CODEX = (f"@**{BOTS[kind][1]}**" for kind in KINDS)
BARE = (
    "This topic already has a Codex session; for a new one, start a new topic, or use !detach "
    "first, then mention me."
)
SETTINGS = BARE + " To change this session's model or effort, use !model or !effort."
NOT_SENT = (
    "This topic already has a Codex session, so your prompt wasn't sent to Codex. To continue "
    "that session, send the prompt again without !new; for a new one, start a new topic, or use "
    "!detach first, then mention me."
)
BOTH = "This topic already has a Claude session and a Codex session"
BOTH_BARE = (
    BOTH + "; for new ones, start a new topic, or use !detach first, then mention the agents you "
    "want."
)
BOTH_SETTINGS = BOTH_BARE + " To change those sessions' model or effort, use !model or !effort."
BOTH_NOT_SENT = (
    BOTH + ", so your prompt wasn't sent to Claude or Codex. To continue those sessions, send the "
    "prompt again without !new; for new ones, start a new topic, or use !detach first, then "
    "mention the agents you want."
)
DM_NOT_SENT = (
    "Your prompt wasn't sent to {}: !new works in a channel topic, so send it in a new topic, and "
    "it starts a session there with your prompt."
)
#: How the two bots read one message: one after the other, either way, or at once.
ORDERS = {
    "claude-first": dict(order=("claude", "codex")),
    "codex-first": dict(order=("codex", "claude")),
    "together": dict(together=True),
}


@pytest.mark.parametrize(
    "command,form",
    [
        ("!new", "bare"),
        ("!new --worktree", "bare"),
        ("!new --model m", "settings"),
        ("!new --worktree --effort high", "settings"),
        ("!new try again", "prompt"),
        ("!new --model m try again", "prompt"),
        ("!new\ntry again", "prompt"),
        ("!new -- --not-an-option", "prompt"),
        ("!new --unknown", "bare"),  # options it can't read, and nothing else
        ("!new --model", "bare"),
        ("!new --unknown try again", "prompt"),  # ... and words: a prompt is never dropped unsaid
    ],
)
def test_the_answer_says_whether_a_prompt_was_dropped(command, form):
    """One bot's answer (#985), the joint one (#987) and a DM's (#987), for each form."""
    one = {"bare": BARE, "settings": SETTINGS, "prompt": NOT_SENT}[form]
    assert new_with_session("codex", command) == one
    assert new_with_session("claude", command) == one.replace("Codex", "Claude")
    both = {"bare": BOTH_BARE, "settings": BOTH_SETTINGS, "prompt": BOTH_NOT_SENT}[form]
    assert new_with_sessions(["claude", "codex"], command) == both
    for kind, agent in (("claude", "Claude"), ("codex", "Codex")):
        dm = DM_NOT_SENT.format(agent) if form == "prompt" else NEW_DM
        assert new_in_dm(kind, command) == dm


async def codex_topic(make_pair, last):
    """Topic "t" with Codex's session, after its first turn; `last`: under the rule `last`, the
    record naming Codex."""
    pair = await make_pair()
    if last:
        assert (await dm_to(pair, "codex", "!unmentioned last")).startswith("Done.")
    await pair.say(f"{CODEX} remember orchid", "t")
    await pair.turns(1, "t", kinds=("codex",))
    return pair


async def after(pair, text, **how):
    """Each bot's posts in "t" after the owner's `text`, once every actor is done with it, and
    the agent runs started meanwhile (Codex's turns and Claude's runs); `how`: `Pair.say`'s
    order of the bots' reads."""
    before = {kind: len(pair.posts("t", kind)) for kind in KINDS}
    starts = len(pair.server.requests("turn/start")) + len(calls(pair.tmp))
    await pair.say(text, "t", **how)
    await pair.quiet()
    posts = {kind: pair.posts("t", kind)[before[kind] :] for kind in KINDS}
    return posts, len(pair.server.requests("turn/start")) + len(calls(pair.tmp)) - starts


def unchanged(pair, row):
    """Codex's session in "t" is the one it had, with only its first prompt, and Claude has
    none."""
    rows = pair.store.rows("sessions", topic="t")
    assert [(r["bot"], r["id"]) for r in rows] == [("codex", row["id"])]
    assert pair.prompts(pair.rows("t")["codex"]) == ["remember orchid"]


@pytest.mark.parametrize("last", [False, True], ids=["all", "last"])
@pytest.mark.parametrize(
    "text,answer",
    [
        (f"{CODEX} !new", BARE),
        ("@codex !new", BARE),
        (f"{CODEX} !new --model m", SETTINGS),
        (f"{CODEX} !new try again", NOT_SENT),
        ("@codex !new try again", NOT_SENT),
        (f"{CODEX} !new --model m try again", NOT_SENT),
        (f"{CODEX} !New --effort high try again", NOT_SENT),  # as people type it (#709)
    ],
)
async def test_a_named_new_where_the_bot_has_a_session(make_pair, last, text, answer):
    """Named, completed or typed, under either rule: one answer, from Codex, never "Unknown
    command", and nothing else happens."""
    pair = await codex_topic(make_pair, last)
    row = pair.rows("t")["codex"]
    posts, turns = await after(pair, text)
    assert posts == {"claude": [], "codex": [answer]}
    assert turns == 0
    unchanged(pair, row)


@pytest.mark.parametrize(
    "text,answer",
    [
        ("!new", BARE),
        ("!new --effort high", SETTINGS),
        ("!new try again", NOT_SENT),
        ("!new --model m try again", NOT_SENT),
    ],
)
async def test_an_unmentioned_new_that_reaches_the_bot_alone(make_pair, text, answer):
    """Under `last`, the record naming Codex: an unmentioned `!new` reaches Codex alone
    (`Route.quiet` off), which answers as when named."""
    pair = await codex_topic(make_pair, last=True)
    row = pair.rows("t")["codex"]
    posts, turns = await after(pair, text)
    assert posts == {"claude": [], "codex": [answer]}
    assert turns == 0
    unchanged(pair, row)


async def test_a_bare_unmentioned_new_beside_a_bot_without_a_session(make_pair):
    """Under `all`, a bare unmentioned `!new` reaches both bots: Codex, which has a session, says
    nothing (`Route.quiet`), and Claude, which has none, starts its session, as before."""
    pair = await codex_topic(make_pair, last=False)
    row = pair.rows("t")["codex"]
    before = len(pair.posts("t", "codex"))
    await pair.say("!new", "t")
    await pair.turns(1, "t", kinds=("claude",))
    await pair.quiet()
    assert pair.posts("t", "codex")[before:] == []
    assert pair.rows("t")["codex"]["id"] == row["id"]
    assert pair.prompts(pair.rows("t")["codex"]) == ["remember orchid"]


@pytest.mark.parametrize("how", ORDERS)
@pytest.mark.parametrize("has", KINDS)
async def test_an_unmentioned_new_beside_a_bot_without_a_session(make_pair, has, how):
    """Under `all`, one bot has a session in "t" and the other none: an unmentioned `!new` with a
    prompt reaches both. The one without starts its session with the prompt, and the one with
    says nothing, whichever reads the message first. Claude, the first by name, never says that
    the prompt wasn't sent, also after Codex has read the message and started its session with
    it (`BotBridge.new_for_topic` reads Codex's claim, #987)."""
    pair = await make_pair()
    other = next(kind for kind in KINDS if kind != has)
    await pair.say(f"@{has} remember orchid", "t")
    await pair.turns(1, "t", kinds=(has,))
    row = pair.rows("t")[has]
    before = len(pair.posts("t", has))
    await pair.say("!new try again", "t", **ORDERS[how])
    await pair.turns(1, "t", kinds=(other,))
    await pair.quiet()
    assert pair.posts("t", has)[before:] == []
    assert not [text for text in pair.posts("t") if "wasn't sent" in text]
    assert pair.rows("t")[has]["id"] == row["id"]
    assert pair.prompts(pair.rows("t")[has]) == ["remember orchid"]
    assert pair.prompts(pair.rows("t")[other])[-1].endswith("try again")


async def test_an_unmentioned_new_while_the_other_bot_starts_its_session_with_it(make_pair):
    """Under `all`, Claude has a session in "t" and Codex none: Codex reads an unmentioned `!new`
    first and is still creating its session with it, held inside its backend's creation, when
    Claude reads it. Codex's claim then names no session, so Claude says nothing, and Codex's
    session gets the prompt (`BotBridge.new_for_topic`'s claim without a session, #987)."""
    pair = await make_pair()
    await pair.say("@claude remember orchid", "t")
    await pair.turns(1, "t", kinds=("claude",))
    before = len(pair.posts("t", "claude"))
    codex = pair.bridges["codex"].backend
    create, inside, go = codex.create_session, asyncio.Event(), asyncio.Event()

    async def held(spec):
        inside.set()
        await go.wait()
        return await create(spec)

    codex.create_session = held
    message = pair.fake.post_as(OWNER, "!new try again", "t")
    event = {"type": "message", "message": message, "flags": message["flags"]}
    starting = asyncio.create_task(pair.bridges["codex"].handle(json.loads(json.dumps(event))))
    await asyncio.wait_for(inside.wait(), 30)
    claim = pair.store.rows("inbound", bot="codex", message_id=message["id"])
    assert [(c["session_id"], c["disposition"]) for c in claim] == [(None, "ignored")]
    await pair.bridges["claude"].handle(json.loads(json.dumps(event)))
    go.set()
    await starting
    await pair.turns(1, "t", kinds=("codex",))
    await pair.quiet()
    assert pair.posts("t", "claude")[before:] == []
    assert not [text for text in pair.posts("t") if "wasn't sent" in text]
    assert pair.prompts(pair.rows("t")["codex"])[-1].endswith("try again")


async def both_topic(make_pair, last):
    """Topic "t" with a session of each bot, after their first turns; `last`: under the rule
    `last`, the record naming both."""
    pair = await make_pair()
    if last:
        assert (await dm_to(pair, "codex", "!unmentioned last")).startswith("Done.")
    await pair.say(f"{CLAUDE} {CODEX} remember orchid", "t")
    await pair.turns(1, "t")
    return pair


@pytest.mark.parametrize("how", ORDERS)
@pytest.mark.parametrize("last", [False, True], ids=["all", "last"])
async def test_an_unmentioned_new_where_both_have_a_session(make_pair, last, how):
    """Both bots have a session, and under `last` the record names both: an unmentioned `!new`
    with a prompt reaches both, and neither starts a session with it. Claude, the first by name,
    answers for the topic, naming both sessions and saying that the prompt wasn't sent to either,
    whichever bot reads the message first; Codex says nothing (#987, SPEC §4.6). Before, neither
    said anything, and the prompt went nowhere."""
    pair = await both_topic(make_pair, last)
    rows = pair.rows("t")
    posts, turns = await after(pair, "!new try again", **ORDERS[how])
    assert posts == {"claude": [BOTH_NOT_SENT], "codex": []}
    assert turns == 0
    assert {k: r["id"] for k, r in pair.rows("t").items()} == {k: r["id"] for k, r in rows.items()}
    assert all(len(pair.ended(r)) == 1 for r in pair.rows("t").values())
    assert pair.prompts(pair.rows("t")["codex"])[-1].endswith("remember orchid")


@pytest.mark.parametrize(
    "text,answer",
    [
        ("!new", BOTH_BARE),
        ("!new --model m", BOTH_SETTINGS),
        ("!New --effort high try again", BOTH_NOT_SENT),  # as people type it (#709)
        ("`!new try again`", BOTH_NOT_SENT),
    ],
)
async def test_each_form_of_an_unmentioned_new_where_both_have_a_session(make_pair, text, answer):
    """... and the answer to each form, bare and with options too: one, from Claude."""
    pair = await both_topic(make_pair, last=False)
    posts, turns = await after(pair, text)
    assert posts == {"claude": [answer], "codex": []}
    assert turns == 0


async def test_a_new_naming_both_where_one_has_a_session(make_pair):
    """`@claude @codex !new …` where only Codex has a session: Claude starts its session with
    the prompt, and Codex says that the prompt wasn't sent to Codex."""
    pair = await codex_topic(make_pair, last=False)
    row = pair.rows("t")["codex"]
    before = len(pair.posts("t", "codex"))
    await pair.say(f"{CLAUDE} {CODEX} !new try again", "t")
    await pair.turns(1, "t", kinds=("claude",))
    await pair.quiet()
    assert pair.posts("t", "codex")[before:] == [NOT_SENT]
    assert pair.rows("t")["codex"]["id"] == row["id"]
    assert pair.prompts(pair.rows("t")["codex"]) == ["remember orchid"]
    assert not [m for m in pair.fake.messages.values() if m["content"] == UNKNOWN]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "text,prompt",
    [
        ("!new try again", True),
        ("!New --model m try again", True),  # as people type it (#709)
        ("@{kind} !new try again", True),  # a typed mention of the bot (#713)
        ("!new", False),
        ("!new --model m", False),
    ],
)
async def test_a_new_in_a_dm(make_pair, kind, text, prompt):
    """A DM `!new` starts nothing: the bot answers that `!new` works in a channel topic, and,
    with a prompt, that the prompt wasn't sent to its agent (#987). Before, "Unknown command"."""
    pair = await make_pair()
    answer = await dm_to(pair, kind, text.format(kind=kind))
    agent = {"claude": "Claude", "codex": "Codex"}[kind]
    assert answer == (DM_NOT_SENT.format(agent) if prompt else NEW_DM)
    assert not pair.store.rows("sessions")
    assert not pair.server.requests("turn/start") and not calls(pair.tmp)
