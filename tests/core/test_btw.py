"""`!btw` end to end offline (issue #716, SPEC §7.13): `BotBridge` on the fake Zulip with the
real workspace manager and each real backend (the rig of `test_fork`). A side question is
answered in the topic, marked, while a running turn goes on, and leaves no trace: no session
or turn row, no change to the native history, the worktree or the settings, nothing in peer
context or in a fork's answer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_fork import FIXTURES, KINDS, OWNER, STORY, git, parent
from test_fork import make_rig as make_rig
from test_peer_context import enable_peer, peer_message
from test_session import actor as actor
from test_session import send

from zab.backends import claude as cb
from zab.core import btw
from zab.core import types as t
from zab.core.commands import BTW_DM, BTW_NO_SESSION, HELP
from zab.core.peer_context import bot_address, peer_context, side_message
from zab.core.router import Router
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer

pytestmark = pytest.mark.integration
ANSWERS = {"claude": "pong", "codex": "FAKE_OK "}  # the fakes' answers (Codex: and a number)
MARK = "💬 [btw](https://zulip.example/#narrow/channel/1/near/{})"


def side_answers(rig, topic="topic"):
    return [p for p in rig.posts(topic) if p.startswith("💬 [btw](")]


def trace(rig, row) -> dict:
    """Everything a side question must leave as it was."""
    work = Path(row["repo_root"])
    native = rig.transcript(row) if rig.kind == "claude" else rig.thread(row)
    sessions = rig.store.rows("sessions")
    return dict(
        sessions=sorted((r["id"], r["state"], r["settings_json"]) for r in sessions),
        turns=sorted((r["id"], r["state"]) for r in rig.store.rows("turns")),
        native=native,
        status=git(work, "status", "--porcelain"),
        head=git(work, "rev-parse", "HEAD"),
        branches=git(work, "branch", "--list"),
        refs=git(work, "for-each-ref", "--format=%(refname)"),
        forks=rig.store.get(f"fork_answers:{row['id']}"),
    )


def held_side_turn(rig):
    """A side question's ephemeral thread once its turn runs (`hold_side`), else None."""
    return next(
        (
            x
            for x in rig.server.threads.values()
            if x.get("ephemeral") and any(t["status"] == "inProgress" for t in x["turns"])
        ),
        None,
    )


async def answered(rig, mid, topic="topic"):
    return await rig.until(
        lambda: next((p for p in side_answers(rig, topic) if p.startswith(MARK.format(mid))), None),
        f"the side answer to {mid}",
    )


async def ended(bridge):
    """The bridge's background runs, its `!btw` answers among them, have ended and left
    `bridge.background` (#782). An answer is seen as soon as the fake Zulip has it, from the
    poster's worker: the answer's task resumes after that, takes its entry out of
    `side_questions` and ends, and asyncio runs the done callback that takes the task out of
    `background` on a later turn of the loop still. A check right after `answered` can fall
    in either gap."""
    async with asyncio.timeout(30):
        while bridge.background:  # noqa: ASYNC110 - done callbacks, on later turns of the loop
            await asyncio.sleep(0.01)


async def warned(rig, text, topic="topic"):
    note = rig.bridge.note(text)
    await rig.until(lambda: note in rig.posts(topic), text)


@pytest.mark.parametrize("kind", KINDS)
async def test_btw_answers_and_leaves_no_trace(make_rig, kind):
    rig = await make_rig(kind)
    row = await parent(rig)
    (Path(row["repo_root"]) / "notes.txt").write_text("an uncommitted edit\n")
    before = trace(rig, row)
    if kind == "codex":  # an approval that arrives anyway is declined, never posted
        rig.server.side_request = "item/commandExecution/requestApproval"

    mid = await rig.say("!btw what was the word?")
    reply = await answered(rig, mid)
    assert reply.startswith(f"{MARK.format(mid)}: {ANSWERS[kind]}")  # fast: no mention
    assert trace(rig, row) == before
    assert not rig.store.rows("approvals")
    assert not any("approval needed" in p for p in rig.posts("topic"))
    assert rig.row()["state"] == "idle" and len(rig.ended()) == 1
    # taken in, so idle stop counts it as activity (`commands.status.LAST_ACTIVITY_SQL`)
    (inbound,) = rig.store.rows("inbound", bot="bot", message_id=mid)
    assert inbound["disposition"] == "side_question" and inbound["session_id"] == row["id"]
    reactions = {r["emoji_name"] for r in rig.fake.messages[mid]["reactions"]}
    assert reactions == {"eyes"}
    await ended(rig.bridge)  # #782: the answer's task ended, and out of `background`
    assert not rig.bridge.side_questions
    if kind == "claude":
        call = rig.calls()[-1]
        assert call["stdin"] == cb.SIDE_QUESTION_FRAME + "what was the word?"
        argv = call["argv"]
        assert argv[argv.index("--resume") + 1] == row["native_id"]
        assert {"--fork-session", "--no-session-persistence", "--strict-mcp-config"} <= set(argv)
        assert argv[argv.index("--tools") + 1] == ""
    else:
        (fork,) = rig.server.requests("thread/fork")
        assert fork["ephemeral"] and fork["sandbox"] == "read-only"
        child = rig.server.requests("turn/start")[-1]["threadId"]
        assert child not in rig.server.threads  # unsubscribed, gone
        await rig.until(lambda: rig.server.responses, "the declined approval")
        (declined,) = rig.server.responses.values()
        assert declined["result"] == {"decision": "decline"}


async def test_an_ended_answer_leaves_background_a_turn_of_the_loop_later():
    """#782: the gap `ended` waits out, deterministically. `btw.start`'s task is taken out of
    `bridge.background` by its done callback, which asyncio runs on the turn of the loop after
    the one the task ends in: a check in between sees an ended task still there."""
    send = AsyncMock()
    bridge = SimpleNamespace(
        background=set(),
        bot=SimpleNamespace(name="bot"),
        note=lambda text: text,
        poster=SimpleNamespace(send=send),
        renderer=SimpleNamespace(render_inline=lambda text: text),
    )
    message = dict(id=7, stream_id=1, subject="topic", sender_id=OWNER)
    btw.start(bridge, message, "!btw", {"full_name": "Owner"})  # no question: the usage line
    (task,) = bridge.background
    while not task.done():  # noqa: ASYNC110 - one turn of the loop at a time
        await asyncio.sleep(0)
    assert task in bridge.background  # ended, and not yet taken out: #782's failure
    await ended(bridge)
    assert not bridge.background
    send.assert_awaited_once_with(1, "topic", btw.USAGE)


@pytest.mark.parametrize("kind", KINDS)
async def test_btw_while_a_turn_runs_takes_no_slot_and_leaves_it_running(
    make_rig, kind, monkeypatch
):
    rig = await make_rig(kind, slots=1)  # the running turn holds the only slot
    row = await parent(rig)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_SIDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    rig.hold(True, monkeypatch)
    await rig.say(STORY)
    await rig.held(STORY)
    rig.hold(False, monkeypatch)
    turns = rig.store.rows("turns", session_id=row["id"])

    mid = await rig.say("!btw how far along are you?")
    await answered(rig, mid)
    assert rig.row()["state"] == "running"  # the turn goes on, untouched
    assert rig.store.rows("turns", session_id=row["id"]) == turns
    assert rig.bridge.slots.locked()  # still the turn's alone
    await rig.release()
    ended = await rig.turns(2)
    assert ended[-1]["state"] == "completed"
    history = rig.history(rig.row())
    assert not any("how far along" in prompt for prompt in history)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_second_btw_waits_for_the_first(make_rig, kind, monkeypatch):
    rig = await make_rig(kind)
    await parent(rig)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LINGER", "1")  # each answer takes a second
    if kind == "codex":
        rig.server.hold_side = True  # until the second one waits
    first = await rig.say("!btw one?")
    second = await rig.say("!btw two?")
    if kind == "codex":
        await rig.until(
            lambda: any(
                r["emoji_name"] == "hourglass" for r in rig.fake.messages[second]["reactions"]
            ),
            "the second one waiting",
        )
        # The first one's held turn, not just its fork: ⏳ can come before the fork's
        # `turn/start`, and the hold must still apply when that turn starts (#748).
        held = await rig.until(lambda: held_side_turn(rig), "the first one's held turn")
        rig.server.hold_side = False
        await rig.server.finish(held["id"])
    await answered(rig, second)
    one, two = side_answers(rig)
    assert one.startswith(MARK.format(first)) and two.startswith(MARK.format(second))
    reactions = {r["emoji_name"] for r in rig.fake.messages[second]["reactions"]}
    assert reactions == {"hourglass", "eyes"}
    await ended(rig.bridge)  # #782: the second answer's task may not have resumed yet
    assert not rig.bridge.side_questions


@pytest.mark.parametrize("kind", KINDS)
async def test_refusals_and_the_dm(make_rig, kind, monkeypatch):
    rig = await make_rig(kind)
    rig.fake.post_as(OWNER, "!btw anything")  # a DM
    message = list(rig.fake.messages.values())[-1]
    await rig.bridge.handle({"type": "message", "message": dict(message)})
    assert list(rig.fake.messages.values())[-1]["content"] == BTW_DM
    rig.fake.post_as(OWNER, f"@{kind} !btw anything")  # a DM with a typed address (#713)
    message = list(rig.fake.messages.values())[-1]
    await rig.bridge.handle({"type": "message", "message": dict(message)})
    assert list(rig.fake.messages.values())[-1]["content"] == BTW_DM

    await rig.say("@**Test Bot** !btw anything", "elsewhere")  # no session of this bot
    assert rig.posts("elsewhere")[-1] == BTW_NO_SESSION

    rig.hold(True, monkeypatch)
    await rig.say("@**Test Bot** hello")
    await rig.held("hello")
    await rig.say("!btw too early")  # the first turn hasn't ended
    await warned(rig, "There is no completed turn to ask about yet.")
    rig.hold(False, monkeypatch)
    await rig.release()
    await rig.turns(1)

    await rig.say("!btw")
    await warned(rig, btw.USAGE)
    capabilities = rig.bridge.backend.capabilities
    rig.bridge.backend.capabilities = t.Capabilities(
        steer=False, approvals=False, live_attach=False, rename=False
    )
    await rig.say("!btw unsupported")
    await warned(rig, btw.NOT_SUPPORTED)
    rig.bridge.backend.capabilities = capabilities
    rig.store.update("sessions", {"id": rig.row()["id"]}, state="error")
    await rig.say("!btw in error")
    await warned(rig, btw.NOT_READY)
    assert not side_answers(rig)
    assert len(rig.store.rows("sessions")) == 1 and len(rig.ended()) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_a_timeout_and_a_failure_are_one_line(make_rig, kind, monkeypatch):
    rig = await make_rig(kind)
    await parent(rig)
    monkeypatch.setattr(btw, "TIMEOUT_S", 1.0)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    if kind == "codex":
        rig.server.hold_side = True
    await rig.say("!btw slow?")
    await warned(rig, btw.timed_out())
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_HANG")
    if kind == "codex":  # a tool the turn reached for: stopped (tests/backends for failures)
        rig.server.hold_side = False
        rig.server.side_items = [{"type": "webSearch", "query": "anything", "action": None}]
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_SIDE_FIXTURE", str(FIXTURES / "error.jsonl"))
    await rig.say("!btw failing?")
    failure = "Claude couldn't answer" if kind == "claude" else "Codex reached for a tool"
    await rig.until(
        lambda: rig.posts("topic")[-1].startswith(rig.bridge.note(failure)), "the failure"
    )
    assert not side_answers(rig) and len(rig.ended()) == 1
    assert rig.row()["state"] == "idle"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_slow_answer_mentions_its_asker(make_rig, kind, monkeypatch):
    rig = await make_rig(kind)
    await parent(rig)
    monkeypatch.setattr(btw, "SLOW_S", 0.0)
    mid = await rig.say("!btw slow?")
    reply = await answered(rig, mid)
    assert reply.startswith(f"{MARK.format(mid)} @**Owner|{OWNER}**: ")


@pytest.mark.parametrize("kind", KINDS)
async def test_btw_stays_out_of_a_forks_answer(make_rig, kind, monkeypatch):
    rig = await make_rig(kind)
    top = await parent(rig)
    await rig.say("!fork count the cases")
    await rig.turns(1, "topic ⑂ 1")
    await rig.until(lambda: rig.store.get(f"fork_answers:{top['id']}"), "the fork's answer")
    answers = rig.store.get(f"fork_answers:{top['id']}")
    mid = await rig.say("!btw what did you find?", "topic ⑂ 1")
    await answered(rig, mid, "topic ⑂ 1")
    mid = await rig.say("!btw and here?")
    await answered(rig, mid)
    assert rig.store.get(f"fork_answers:{top['id']}") == answers
    assert len(rig.ended("topic ⑂ 1")) == 1


async def test_peer_context_leaves_side_questions_and_answers_out(actor):
    a, fake, _ = actor
    enable_peer(a, fake)
    await send(a, fake, "first")
    url = "https://zulip.example/#narrow/channel/1/near/1"
    await peer_message(a, fake, "PEER ANSWER")
    await peer_message(a, fake, f"💬 [btw]({url}): SIDE ANSWER")
    await peer_message(a, fake, f"💬 [btw]({url})\n\nSIDE PART TWO\n\n(2/2)")
    asked = await peer_message(a, fake, "@**Peer** !btw SIDE QUESTION", sender=20)
    a.store.set(f"message_audience:b:{asked}", dict(addressed=False, peers=["Peer"]))
    trigger = await peer_message(a, fake, "compare", sender=20)
    a.store.set(f"addressed_peers:b:{trigger}", ["Peer"])
    text, _ = await peer_context(a, [trigger])
    assert "PEER ANSWER" in text
    for excluded in ("SIDE ANSWER", "SIDE PART TWO", "SIDE QUESTION"):
        assert excluded not in text
    a.bridge_bot_ids = lambda: {30}
    # the organization's other bots, whose mentions are addresses; a person's is none (#725)
    for uid, name in ((40, "Claude · other"), (41, "Peer · a*b")):
        fake.members.append(dict(user_id=uid, full_name=name, is_bot=True))
    fake.members.append(dict(user_id=42, full_name="Bob", is_bot=False))
    await peer_message(
        a, fake, "@**Bob** !btw is a PERSON PROMPT, as the routers read it", sender=21
    )
    # another participant's side question to their own agent, and a plain message of theirs
    await peer_message(a, fake, "@**Claude · other** !btw OTHER SIDE QUESTION", sender=21)
    await peer_message(a, fake, "OTHER PARTICIPANT SAYS", sender=21)
    await peer_message(a, fake, "@**Peer · a*b** // STARRED ASIDE", sender=20)  # an aside
    # the side questions the old regex missed (N1 of #737's review), another participant's
    await peer_message(a, fake, "``!btw DOUBLE SIDE?``", sender=21)
    await peer_message(a, fake, "` !btw PADDED SIDE? `", sender=21)
    trigger = await peer_message(a, fake, "compare again", sender=20)
    text, _ = await peer_context(a, [trigger], multi_party=True, sender="Owner", context_id="x")
    assert "PEER ANSWER" in text and "OTHER PARTICIPANT SAYS" in text and "PERSON PROMPT" in text
    excluded = ("SIDE ANSWER", "SIDE PART TWO", "SIDE QUESTION", "STARRED ASIDE", "DOUBLE SIDE")
    for word in (*excluded, "PADDED SIDE"):
        assert word not in text, word


async def test_the_answer_is_rendered_and_every_part_marked():
    posted: list[str] = []
    context = t.RenderContext(
        workspace=SimpleNamespace(root="/w", cwd="/w"),
        max_message_length=600,
        max_upload_bytes=0,
        upload=None,
    )
    actor = SimpleNamespace(
        poster=SimpleNamespace(client=SimpleNamespace(site="https://z")),
        mention_after=120,
        context=context,
        renderer=MarkdownRenderer(),
        post=AsyncMock(side_effect=lambda text: posted.append(text)),
    )
    message = {"id": 7, "stream_id": 1, "sender_id": OWNER}
    await btw.post(actor, message, "Owner", "Yes: $x^2$ and @**all**.", 1.0)
    # math converted, the wildcard mention made code and disarmed (§11.2, §11.5)
    disarmed = "`@\u200b**all**`"
    assert posted == [f"💬 [btw](https://z/#narrow/channel/1/near/7): Yes: $$x^2$$ and {disarmed}."]
    posted.clear()
    await btw.post(actor, message, "Owner", "- one\n- two", 1.0)
    assert posted == ["💬 [btw](https://z/#narrow/channel/1/near/7):\n\n- one\n- two"]
    posted.clear()
    # a minute or more mentions the asker, with the default 120 s `mention_after_s` (N5)
    await btw.post(actor, message, "Owner", "\n\n".join(["word " * 60] * 4), 61.0)
    assert len(posted) > 1 and all(p.startswith("💬 [btw](https://z/") for p in posted)
    assert all(len(p) <= 600 for p in posted)
    assert posted[0].startswith(f"💬 [btw](https://z/#narrow/channel/1/near/7) @**Owner|{OWNER}**:")
    assert "@**Owner" not in "".join(posted[1:])
    posted.clear()
    actor.mention_after = 0  # every turn mentions; a fast side answer still doesn't
    await btw.post(actor, message, "Owner", "Yes.", 59.9)
    assert posted == ["💬 [btw](https://z/#narrow/channel/1/near/7): Yes."]


@pytest.mark.parametrize(
    "body",
    [
        "!btw what?",
        "@**Claude · bob** !btw what?",
        "@**Claude · bob|31** @**Codex · bob**, !btw what?",
        "@**Claude · a*b** !btw what?",  # a `*` in the name: still an address
        "@claude !btw what?",  # a typed address (#713)
        "@Codex: !btw what?",
        "!Btw what?",  # as another participant's bridge reads it (#709)
        "`!btw what?`",
        "``!btw what?``",  # any run of backticks (#709's `as_command`, N1 of #737's review)
        "` !btw what? `",  # spaces inside the span
        "@claude `!BTW what?`",
        "!btw",
        "💬 [btw](https://z/#narrow/channel/1/near/7): an answer",
    ],
)
def test_a_side_message_however_it_is_addressed_or_typed(body):
    assert side_message(body)


#: The VM's two bots (the owner's), by kind, and another participant's: the organization's bots.
VM_BOTS = {"claude": "Claude · X", "codex": "Codex · X"}
ORG_BOTS = (*VM_BOTS.values(), "Claude · other")
#: (who sends it, the message): the owner, `OWNER`, or another participant, 21, whom the VM's
#: bots don't allow
ROUTED = [
    (OWNER, "!btw what?"),
    (OWNER, "!Btw what?"),
    (OWNER, "@claude !btw what?"),  # typed (#713)
    (OWNER, "@Codex: !btw what?"),
    (OWNER, "@**Claude · X** !btw what?"),  # completed
    (OWNER, "@**Claude · X|10** @**Codex · X**, !btw what?"),  # both bots, one with its ID
    (OWNER, "@claude @**Codex · X** !btw what?"),  # typed, then completed
    (OWNER, "`!btw what?`"),  # code spans (#709)
    (OWNER, "``!btw what?``"),
    (OWNER, "` !btw what? `"),
    (OWNER, "@claude `!BTW what?`"),
    (OWNER, "@claude `!BTW` what?"),  # a span and words: a prompt
    (OWNER, "`!btw what?"),  # an unclosed span: a prompt
    (OWNER, "`!btw` is a command"),
    (OWNER, "```\n!btw what?\n```"),  # a fence
    (OWNER, "> !btw quoted"),  # a quote
    (OWNER, "!btw what? @**Claude · X**"),  # a trailing mention
    (OWNER, "@**Bob** !btw what?"),  # a person's mention: no address, a prompt (#725's review)
    (OWNER, "@_**Claude · X** !btw what?"),  # a silent mention: no address
    (OWNER, "btw, !btw is a command"),
    (OWNER, "!btwx"),
    (21, "@**Claude · other** !btw what?"),  # another participant's, to their own bots
    (21, "!btw what?"),
]
#: Another participant's side questions: no bridge of this VM runs them (they don't allow the
#: sender), but they are side questions, to their own bots, or unaddressed, which no bridge runs
#: (a channel with other people needs a mention), and every history leaves them out.
NOT_OURS = {(21, "@**Claude · other** !btw what?"), (21, "!btw what?")}


@pytest.mark.parametrize(("sender", "body"), ROUTED)
def test_a_side_message_is_what_the_routers_run_as_one(tmp_path, sender, body):
    """The review of #725 (4): each message through two real routers, the VM's Claude and Codex
    bots with a session each in the topic, in a single-human channel; the bridge answers
    `!btw` where a router's route is a command whose text `btw.is_btw` takes (`bridge.py`).
    `side_message`, with the organization's bots as the address (`bot_address`), says the same,
    but for `NOT_OURS`."""
    store = Store(tmp_path)
    try:
        routers = {}
        for i, (kind, name) in enumerate(VM_BOTS.items()):
            routers[kind] = Router(store, kind, 10 + i, name, frozenset({OWNER}), kind=kind)
            store.insert(
                "sessions", id=f"s-{kind}", bot=kind, backend=kind, native_id=f"n-{kind}",
                card_message_id=100 + i, stream_id=1, topic="topic", workspace="/repo",
                workspace_mode="shared", repo_root="/repo", settings_json="{}", state="idle",
                shared=1, created_at=0, updated_at=0,
            )  # fmt: skip
        message = dict(
            id=200, sender_id=sender, stream_id=1, subject="topic", content=body, type="stream"
        )
        routes = [
            router.route(
                dict(message),
                flags=["mentioned"] if f"@**{VM_BOTS[kind]}" in body else [],
                sender_is_bot=False,
                private=True,
                other_bot_names=set(ORG_BOTS) - {VM_BOTS[kind]},
                siblings=tuple(n for k, n in VM_BOTS.items() if k != kind),
            )
            for kind, router in routers.items()
        ]
    finally:
        store.close()
    runs = any(r.action == "command" and btw.is_btw(r.text) for r in routes)
    side = side_message(body, bot_address(ORG_BOTS))
    assert side is (runs or (sender, body) in NOT_OURS), [(r.action, r.text) for r in routes]
    if (sender, body) in NOT_OURS:
        assert not runs and side  # the one intended difference


@pytest.mark.parametrize(
    "body",
    [
        "btw, !btw is a command",
        "!btwx",
        "> !btw quoted",
        "@claude's !btw",
        "@_**Claude · bob** !btw a silent mention is no address",
        "the 💬 [btw](x): marker later",
        # what the bridge doesn't run as `!btw` either: a prompt, as `as_command` reads it
        "@claude `!BTW` what?",
        "`!btw what?",  # an unclosed span
        "`!btw` is a command",
        "```\n!btw what?\n```",  # a fenced block
    ],
)
def test_not_a_side_message(body):
    assert not side_message(body)


def test_help_and_the_command():
    assert "`!btw question`" in HELP
    assert btw.is_btw("!btw what?") and btw.is_btw("!btw") and not btw.is_btw("!btwx")
