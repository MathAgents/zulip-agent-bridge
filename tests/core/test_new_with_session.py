"""A `!new` that reaches a bot with a session in its topic (#985, SPEC §4.6). Named, or as the one
bot an unmentioned one reaches (under `last`, the record naming it), the bot answers that the
topic has its session and how to start a new one, and that the prompt wasn't sent when there was
one. Unmentioned beside the VM's other bots it says nothing, as before, and a bot without a
session starts one. It never starts a turn or a session of the bot that has one.

Before, each answered "Unknown command; use !help." and dropped the prompt without a word.

Both bots in one process (`test_fork.make_pair`): Claude on `fake_claude.py`, Codex on
`FakeAppServer`, one store and one Zulip."""

import pytest
from test_fork import BOTS, KINDS
from test_fork import make_pair as make_pair
from test_unmentioned_last import dm_to

from zab.core.commands import UNKNOWN, new_with_session

pytestmark = pytest.mark.integration
CLAUDE, CODEX = (f"@**{BOTS[kind][1]}**" for kind in KINDS)
BARE = (
    "This topic already has a Codex session; for a new one, start a new topic, or use !detach "
    "first."
)
NOT_SENT = (
    "This topic already has a Codex session, so your prompt wasn't sent to Codex. To continue "
    "that session, send the prompt again without !new; for a new one, start a new topic, or use "
    "!detach first."
)


@pytest.mark.parametrize(
    "command,prompt",
    [
        ("!new", False),
        ("!new --model m", False),
        ("!new --worktree --effort high", False),
        ("!new try again", True),
        ("!new --model m try again", True),
        ("!new\ntry again", True),
        ("!new -- --not-an-option", True),
        ("!new --unknown", False),  # options it can't read, and nothing else
        ("!new --model", False),
        ("!new --unknown try again", True),  # ... and words: a prompt is never dropped unsaid
    ],
)
def test_the_answer_says_whether_a_prompt_was_dropped(command, prompt):
    assert new_with_session("codex", command) == (NOT_SENT if prompt else BARE)
    claude = new_with_session("claude", command)
    assert claude == (NOT_SENT if prompt else BARE).replace("Codex", "Claude")


async def codex_topic(make_pair, last):
    """Topic "t" with Codex's session, after its first turn; `last`: under the rule `last`, the
    record naming Codex."""
    pair = await make_pair()
    if last:
        assert (await dm_to(pair, "codex", "!unmentioned last")).startswith("Done.")
    await pair.say(f"{CODEX} remember orchid", "t")
    await pair.turns(1, "t", kinds=("codex",))
    return pair


async def after(pair, text):
    """Each bot's posts in "t" after the owner's `text`, once every actor is done with it, and
    the Codex turns started meanwhile."""
    before = {kind: len(pair.posts("t", kind)) for kind in KINDS}
    starts = len(pair.server.requests("turn/start"))
    await pair.say(text, "t")
    await pair.quiet()
    posts = {kind: pair.posts("t", kind)[before[kind] :] for kind in KINDS}
    return posts, len(pair.server.requests("turn/start")) - starts


def unchanged(pair, row):
    """Codex's session in "t" is the one it had, with only its first prompt, and Claude has
    none."""
    rows = pair.store.rows("sessions", topic="t")
    assert [(r["bot"], r["id"]) for r in rows] == [("codex", row["id"])]
    assert pair.prompts(pair.rows("t")["codex"]) == ["remember orchid"]


@pytest.mark.parametrize("last", [False, True], ids=["all", "last"])
@pytest.mark.parametrize(
    "text,prompt",
    [
        (f"{CODEX} !new", False),
        ("@codex !new", False),
        (f"{CODEX} !new --model m", False),
        (f"{CODEX} !new try again", True),
        ("@codex !new try again", True),
        (f"{CODEX} !new --model m try again", True),
        (f"{CODEX} !New --effort high try again", True),  # as people type it (#709)
    ],
)
async def test_a_named_new_where_the_bot_has_a_session(make_pair, last, text, prompt):
    """Named, completed or typed, under either rule: one answer, from Codex, never "Unknown
    command", and nothing else happens."""
    pair = await codex_topic(make_pair, last)
    row = pair.rows("t")["codex"]
    posts, turns = await after(pair, text)
    assert posts == {"claude": [], "codex": [NOT_SENT if prompt else BARE]}
    assert turns == 0
    unchanged(pair, row)


@pytest.mark.parametrize(
    "text,prompt",
    [
        ("!new", False),
        ("!new --effort high", False),
        ("!new try again", True),
        ("!new --model m try again", True),
    ],
)
async def test_an_unmentioned_new_that_reaches_the_bot_alone(make_pair, text, prompt):
    """Under `last`, the record naming Codex: an unmentioned `!new` reaches Codex alone
    (`Route.quiet` off), which answers as when named."""
    pair = await codex_topic(make_pair, last=True)
    row = pair.rows("t")["codex"]
    posts, turns = await after(pair, text)
    assert posts == {"claude": [], "codex": [NOT_SENT if prompt else BARE]}
    assert turns == 0
    unchanged(pair, row)


@pytest.mark.parametrize("text", ["!new", "!new try again"])
async def test_an_unmentioned_new_beside_a_bot_without_a_session(make_pair, text):
    """Under `all`, an unmentioned `!new` reaches both bots: Codex, which has a session, says
    nothing (`Route.quiet`), and Claude, which has none, starts its session, as before."""
    pair = await codex_topic(make_pair, last=False)
    row = pair.rows("t")["codex"]
    before = len(pair.posts("t", "codex"))
    await pair.say(text, "t")
    await pair.turns(1, "t", kinds=("claude",))
    await pair.quiet()
    assert pair.posts("t", "codex")[before:] == []
    assert pair.rows("t")["codex"]["id"] == row["id"]
    assert pair.prompts(pair.rows("t")["codex"]) == ["remember orchid"]


@pytest.mark.parametrize("last", [False, True], ids=["all", "last"])
async def test_an_unmentioned_new_where_both_have_a_session_says_nothing(make_pair, last):
    """Both bots have a session, and under `last` the record names both: an unmentioned `!new`
    reaches both, and each says nothing (SPEC §4.6, `Route.quiet`) and starts nothing."""
    pair = await make_pair()
    if last:
        assert (await dm_to(pair, "codex", "!unmentioned last")).startswith("Done.")
    await pair.say(f"{CLAUDE} {CODEX} remember orchid", "t")
    await pair.turns(1, "t")
    rows = pair.rows("t")
    posts, turns = await after(pair, "!new try again")
    assert posts == {"claude": [], "codex": []}
    assert turns == 0
    assert {k: r["id"] for k, r in pair.rows("t").items()} == {k: r["id"] for k, r in rows.items()}
    assert all(len(pair.ended(r)) == 1 for r in pair.rows("t").values())


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
