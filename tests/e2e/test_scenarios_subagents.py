"""Scenario 17 of the end-to-end suite (issue #263): sub-agents across a turn's end (#660).

1. **Claude: a steer while a background sub-agent works** (#814). Claude starts a sub-agent in
   the background, busy for about a minute in a foreground `python3` sleep (Claude Code 2.1.283
   refuses a long leading `sleep`), and ends its first loop. The steer goes 3 s after that loop
   is seen to end in the session's transcript, and gets ↪ (never ⏳) and ✅ within 15 s. Its
   answer comes before the sub-agent's notification. The turn ends ✅ after it, as one turn
   whose answer holds the sub-agent's word.
2. **Claude: `!stop` while the sub-agent works.** The turn ends ⏹, and no process of the
   sub-agent's is left.
3. **Codex: a turn that ends with a sub-agent running** (#822). Its answer carries the
   bridge's ⚠️ note. The next turn, which waits for the sub-agent, carries none.
4. **Codex: `!stop` with a sub-agent running** (#825). 45 s in, the turn ends ⏹, and the
   sub-agent's rollout shows its turn aborted. The scenario records whether the command it
   started survives, and expects it to: Codex keeps a turn's exec sessions past an interrupt
   (DECISIONS, 2026-11-05). The scenario's clean-up then ends that command.
5. **Codex: `!stop` with no turn running** (#847). A turn ends with its sub-agent still
   running, as in part 3. `!stop` then, with no turn running, stops it: the bot says it
   stopped 1 sub-agent, and the sub-agent's rollout shows its turn aborted, as in part 4.
6. **Codex: `!stop` after a bridge restart** (#858). As in part 5, but the bridge restarts
   while the sub-agent runs, so no event tells it of the sub-agent: `!stop` with no turn finds
   it among the daemon's loaded threads (`thread/list` lists none live) and stops it.
7. **Claude: notes while a background sub-agent works** (#668), in
   `test_scenarios_subagents_notes.py`: in three topics at once, the gaps between Claude's
   notes, from the sub-agent's launch to its notification, are at most 65 s (a target in a
   release candidate's run).

Each part skips when the installed zab lacks its fix (`harness/installed.py`), so that one
wheel with some of them runs the rest. The prompts ask the agents to keep or end their loops
against the installed instructions ("don't end your turn while a sub-agent runs"). Where the
agent didn't, the part fails, saying so: "Claude went on working after its first reply", or
"Codex didn't comply" (it waited for or stopped its sub-agent), apart from a missing note.

Each part's clean-up sends `!stop` in its topic when its turn may still run, then ends what is
left of its command and checks it went, as a `cleanup.undo` step: a failure there is reported
after the part's own.

Fast: about 20 minutes, part 7's 8 among them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime

import pytest

from e2e.harness import installed
from e2e.harness.cleanup import undo
from e2e.harness.flows import Chat, answer, topic_name
from e2e.test_scenarios_features import BRIDGE_LOG, as_agent

pytestmark = pytest.mark.e2e
STEER_CODE = "21aa"  # ↪ (`arrow_right_hook`): written into the running turn
HELD_CODE = "23f3"  # ⏳ (`hourglass`): held for the next turn
READ_CODE = "2705"  # ✅ (`check`): the agent took the steer in (#654)
STOPPED = r"^⏹ \*\*Stopped\*\*"
DONE = r"^✅ \*\*Done\*\*"
CODEX_NOTE = (
    r"⚠️ \*\*Bridge\*\* \(not Codex\): (?:A sub-agent was|\d+ sub-agents were) still running "
    r"when this turn ended"
)
#: How long the parent's first loop may take to end: a model call that starts the sub-agent,
#: then one that says so.
FIRST_LOOP_S = 180.0
#: How long until the sub-agent's command is seen running (`marked`).
COMMAND_S = 120.0
#: The bridge's answer to `!stop` with no turn running (#847, `zab.core.commands`)
IDLE_STOP = r"^(?:No turn is running|A turn started outside Zulip)"
#: The bridge log's line of what the read of the daemon's loaded threads found and cost
#: (#858), where `!stop` finds the sub-agents no event told of
LOADED_EVENT = "subagent_loaded"

#: The parent session's transcript holding `$1`: depth 2 of the account's projects (a
#: sub-agent's own transcript lies deeper), the newest such.
CLAUDE_TRANSCRIPT = r"""f=$(find /home/agent/.claude/projects -mindepth 2 -maxdepth 2 \
  -name '*.jsonl' -exec grep -l -F -- "$1" {} + 2>/dev/null | xargs -r ls -t | head -n 1)
if [ -n "$f" ]; then cat -- "$f"; fi
true
"""
#: The rollout of the sub-agent thread (source `thread_spawn`, on its first line) of the
#: thread whose prompt holds `$1`: the one that holds `$1` itself (its `exec_command` call, if
#: not its task, which v2 may pass encrypted), else the one whose first line names the
#: parent's id (`parent_thread_id`; the parent's is its first line's first `"id"`).
CODEX_CHILD_ROLLOUT = r"""files=$(find /home/agent/.codex/sessions -name 'rollout-*.jsonl' \
  -mmin -60 2>/dev/null)
parent=""
for f in $files; do
  if ! head -n 1 -- "$f" | grep -q thread_spawn && grep -q -F -- "$1" "$f"; then
    parent=$(head -n 1 -- "$f" | grep -o '"id":"[^"]*"' | head -n 1 | cut -d'"' -f4)
  fi
done
for f in $files; do
  first=$(head -n 1 -- "$f")
  case "$first" in *thread_spawn*) ;; *) continue ;; esac
  if grep -q -F -- "$1" "$f"; then cat -- "$f"; exit 0; fi
  if [ -n "$parent" ] && printf '%s' "$first" | grep -q -F -- "$parent"; then
    cat -- "$f"; exit 0
  fi
done
true
"""


# ------------------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class Entry:
    """One main-thread record of a Claude transcript."""

    kind: str  # "user" or "assistant"
    at: float  # its `timestamp`, in epoch seconds
    text: str  # its text blocks (a user record's string content too), joined
    tool_result: bool = False  # a user record carrying tool results, not a message
    background_agent: bool = False  # an assistant record starting a background sub-agent
    tool_use: bool = False  # an assistant record calling any tool
    agent_id: str = ""  # the id of the background sub-agent's call it starts, if any
    response: str = ""  # an assistant record's response (`message.id`): one per model call
    #: a task notification's `<tool-use-id>` (the call whose task ended), "" for one without
    #: it; None for any other record
    notified: str | None = None


def _seconds(stamp: object) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


#: A task notification's call: the tool call whose background task (a sub-agent, a command)
#: ended
_NOTIFIED = re.compile(r"<tool-use-id>([^<\s]*)</tool-use-id>")


def _notified(text: str) -> str | None:
    if "<task-notification>" not in text:
        return None
    found = _NOTIFIED.search(text)
    return found[1] if found else ""


def transcript(text: str) -> list[Entry]:
    """The main thread's user and assistant records of a Claude Code transcript (JSON lines),
    and the messages queued during a tool call: a sub-agent's (`isSidechain`) and any other
    record are left out.

    Claude Code 2.1.283, headless, records a message that arrives while a tool call runs (a
    task notification, a steer) as an `attachment` record of type `queued_command`, its text in
    `attachment.prompt`, written once the call has ended and the message is taken in, but
    stamped with the time it was queued; `queue-operation` records come before and after it,
    and no `user` record carries it (review of #922, B1). It is read as a user entry, at the
    time it was taken in: the later of its stamp and the record before it, the call's result.
    A message that arrives while Claude is idle is a `user` record, as before."""
    entries: list[Entry] = []
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("isSidechain"):
                continue
            if record.get("type") == "attachment":
                queued = record.get("attachment")
                if isinstance(queued, dict) and queued.get("type") == "queued_command":
                    prompt = str(queued.get("prompt") or "")
                    at = _seconds(record.get("timestamp"))
                    taken = max(at, entries[-1].at) if entries else at
                    entries.append(Entry("user", taken, prompt, notified=_notified(prompt)))
                continue
            if record.get("type") not in ("user", "assistant"):
                continue
            content = (record.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            texts, results, agent, tools, agent_id = [], False, False, False, ""
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_result":
                    results = True
                elif block.get("type") == "tool_use":
                    tools = True
                    starts = block.get("name") in ("Agent", "Task") and bool(
                        (block.get("input") or {}).get("run_in_background")
                    )
                    if starts and not agent:
                        agent, agent_id = True, str(block.get("id") or "")
            joined = "\n".join(texts)
            message = record.get("message") or {}
            entries.append(
                Entry(
                    record["type"],
                    _seconds(record.get("timestamp")),
                    joined,
                    tool_result=results,
                    background_agent=agent,
                    tool_use=tools,
                    agent_id=agent_id,
                    response=str(message.get("id") or "") if record["type"] == "assistant" else "",
                    notified=_notified(joined) if record["type"] == "user" else None,
                )
            )
    return entries


def first_loop_end(entries: list[Entry]) -> int | None:
    """The index of the text that ended the parent's first loop: its last assistant text after
    it started a background sub-agent, with no tool call after it, before the sub-agent's
    notification or any message after the prompt. None while that loop runs."""
    launched = next((i for i, e in enumerate(entries) if e.background_agent), None)
    if launched is None:
        return None
    end = None
    for i in range(launched + 1, len(entries)):
        entry = entries[i]
        if entry.kind == "user" and not entry.tool_result:
            break  # the notification, or a message: the first loop was over by then
        if entry.kind == "assistant" and entry.tool_use:
            end = None  # it went on working
        elif entry.kind == "assistant" and entry.text.strip():
            end = i
    return end


def message_index(entries: list[Entry], marker: str) -> int | None:
    """The index of the user message (not a tool result) holding `marker`."""
    return next(
        (
            i
            for i, e in enumerate(entries)
            if e.kind == "user" and not e.tool_result and marker in e.text
        ),
        None,
    )


def marked(vm, marker: str) -> list[int]:
    """The pids of the `python3` processes whose arguments hold `marker`. Listed with `ps`,
    never matched with `pgrep -f`, which would match the remote command line itself."""
    pids = []
    for line in vm.run(["ps", "-eo", "pid=,args="]).stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        words = args.split()
        if words and os.path.basename(words[0]).startswith("python3") and marker in words[1:]:
            pids.append(int(pid))
    return pids


def end_marked(vm, marker: str, *, wait: float = 10.0, sleep=time.sleep) -> None:
    """End what is left of the marked commands: SIGTERM, then SIGKILL to any still there
    `wait` seconds later; RuntimeError, naming the pids, if one is left even then."""
    for signal in ("TERM", "KILL"):
        pids = marked(vm, marker)
        for pid in pids:
            vm.run(["sudo", "-u", "agent", "kill", f"-{signal}", "--", str(pid)], check=False)
        for _ in range(int(wait)) if pids else ():
            if not marked(vm, marker):
                return
            sleep(1)
    if left := marked(vm, marker):
        raise RuntimeError(f"pids {left} outlived SIGKILL")


async def clean_up(chat: Chat, vm, topic: str, marker: str, *, running: bool) -> None:
    """A part's clean-up. `running`, the part ended before its turn did: `!stop` in its topic
    first, so that no sub-agent runs its command again. Then what is left of the command is
    ended, and checked gone, as a clean-up step (`cleanup.undo`, #517): a failure there is
    reported after the part's own, never instead of it."""
    if running:
        with contextlib.suppress(Exception):  # the stop is a courtesy; the kill below is the step
            await chat.user.send(chat.stream, topic, "!stop")
            await asyncio.sleep(10)  # SIGINT, then SIGTERM 5 s later (SPEC §9.6)
    await undo(
        f"the sub-agent's command {marker} is still running on the VM",
        lambda: asyncio.to_thread(end_marked, vm, marker),
        stops=False,
    )


async def poll(probe, *, within: float, every: float = 2.0, what: str):
    """`probe()` (blocking, run in a thread) until it returns something; TimeoutError after
    `within` seconds, naming `what`."""
    deadline = time.monotonic() + within
    while True:
        found = await asyncio.to_thread(probe)
        if found:
            return found
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no {what} within {within:.0f} s")
        await asyncio.sleep(every)


def idle_stop_count(content: str) -> int | None:
    """How many sub-agents the answer to `!stop` with no turn running says it stopped
    (`zab.core.commands`, #847): 0 when none ran, None for any other answer, such as one
    that couldn't stop or check them all."""
    if found := re.match(r"No turn is running; stopped (\d+) sub-agents? ", content):
        return int(found[1])
    return 0 if re.fullmatch(r"No turn is running(?:, and no sub-agent was)?\.", content) else None


def loaded_cost(vm, event: str = LOADED_EVENT) -> str | None:
    """The bridge log's newest `subagent_loaded` line (`CodexBackend._loaded_subagents`): how
    many loaded threads `!stop` read, how many were sub-agents, and in how many seconds."""
    found = None
    for line in vm.run(as_agent("tail", "-n", "2000", BRIDGE_LOG), check=False).stdout.splitlines():
        with contextlib.suppress(ValueError):
            record = json.loads(line)
            if isinstance(record, dict) and record.get("event") == event:
                found = str(record.get("msg", ""))[:200]
    return found


def loaded_counts(line: str) -> tuple[int, int] | None:
    """The loaded threads read and the sub-agents among them, from a `subagent_loaded` line
    (`Loaded threads read: 4, 1 sub-agents, 0.01 s`)."""
    found = re.match(r"Loaded threads read: (\d+), (\d+) sub-agents", line)
    return (int(found[1]), int(found[2])) if found else None


async def turn_aborted(vm, scenario, marker: str, sent: float, who: str) -> None:
    """After a stop sent at `sent` (monotonic): the sub-agent's rollout is found, then its
    `turn_aborted`, each within 30 s; then whether the command it started survives is noted,
    not asserted: Codex keeps a turn's exec sessions past an interrupt (parts 4 and 5)."""

    def rollout() -> str:
        return vm.run(as_agent("bash", "-c", CODEX_CHILD_ROLLOUT, "bash", marker)).stdout

    await poll(rollout, within=30, what="sub-agent rollout (its first line `thread_spawn`)")
    await poll(
        lambda: "turn_aborted" in rollout(),
        within=30,
        what="`turn_aborted` in the sub-agent's rollout, which was found",
    )
    scenario.timing(f"{who}: the sub-agent's turn aborted", time.monotonic() - sent, high=30)
    survives = bool(marked(vm, marker))
    scenario.notes.append(
        f"{who}: the sub-agent's command "
        + ("survives !stop, as documented" if survives else "ended with it")
    )


def claude_entries(vm, marker: str) -> list[Entry]:
    return transcript(vm.run(as_agent("bash", "-c", CLAUDE_TRANSCRIPT, "bash", marker)).stdout)


async def first_loop_over(vm, marker: str) -> list[Entry]:
    """The transcript once the parent's first loop has ended and stayed over for 3 s."""

    def ended() -> list[Entry] | None:
        entries = claude_entries(vm, marker)
        return entries if first_loop_end(entries) is not None else None

    seen = await poll(ended, within=FIRST_LOOP_S, what="end of Claude's first loop")
    await asyncio.sleep(3)
    again = await asyncio.to_thread(claude_entries, vm, marker)
    if len(again) != len(seen) or first_loop_end(again) is None:
        pytest.fail("Claude went on working after its first reply instead of ending its loop")
    return again


def background_task(marker: str, seconds: int, word: str) -> str:
    """A Claude prompt: a background sub-agent busy `seconds` in a foreground command."""
    return (
        f"This is a test of the bridge (`{marker}`). Start a sub-agent with the Agent tool in "
        "the background (run_in_background: true). Its task: run the shell command "
        f"`python3 -c 'import time; time.sleep({seconds})' {marker}` with the Bash tool in the "
        f"foreground (not in the background), then reply with exactly `{word}`. Don't wait "
        "for it and don't check on it: reply at once with one short sentence saying that you "
        "started it, and end your reply."
    )


def codex_left_it_running(answer_text: str, statuses: list[dict], nonce: str) -> bool | str:
    """Whether Codex left its sub-agent running at the turn's end, as part 3 asks: True, or
    what it did instead. Its answer holds the sub-agent's word only if it waited for it; the
    turn's status lists the sub-agent's end (`sub-agent … completed` or `… interrupted`, #822's
    step titles) if it waited for it or stopped it."""
    if f"SUB-{nonce}" in answer_text:
        return "it waited for its sub-agent (the answer holds its word)"
    for status in statuses:
        if found := re.search(r"sub-agent \S* (completed|interrupted)", status["content"]):
            return f"its sub-agent {found[1]} within the turn"
    return True


# ------------------------------------------------------------------------------ the parts


@pytest.mark.scenario(17)
async def test_claude_steer_while_a_subagent_works(chat: Chat, vm, scenario, nonce):
    """Part 1 (#814): a steer after the parent's first loop is written into the turn (↪) and
    answered in a loop of its own before the sub-agent ends; the turn ends ✅ after it."""
    if not installed.has(vm, installed.CLAUDE_SUBAGENT_STDIN):
        pytest.skip("the installed zab closes Claude's stdin at the first result (#814)")
    marker, sub, done, status = f"S17A-{nonce}", f"SUB-{nonce}", f"DONE-{nonce}", f"STAT-{nonce}"
    topic = topic_name(17, nonce, "claude")
    turn = await chat.say(
        "claude",
        topic,
        background_task(marker, 60, sub) + " When its result comes back to you, reply with "
        f"exactly `{done}` followed by the word it replied with.",
    )
    over = False
    try:
        await first_loop_over(vm, marker)
        sent = time.monotonic()
        mid = await chat.user.send(
            chat.stream, topic, f"What's the status? Reply with `{status}` and one sentence."
        )
        await chat.user.wait_reaction(mid, emoji_code=STEER_CODE, user_id=turn.bot, within=30)
        await chat.user.wait_reaction(mid, emoji_code=READ_CODE, user_id=turn.bot, within=60)
        scenario.timing("claude: steer taken in (✅)", time.monotonic() - sent, high=15)
        held = [
            r for r in (await chat.user.message(mid)).get("reactions", [])
            if r.get("emoji_code") == HELD_CODE
        ]  # fmt: skip
        assert not held, "the steer was held (⏳): stdin was closed while the sub-agent ran"
        reply, took = await chat.reply(turn, answer(re.escape(done)), within=600)
        scenario.timing("claude: the turn's answer", took)
        content = reply["content"]
        assert sub in content, f"the answer lacks the sub-agent's word: {content[:300]}"
        assert status in content.split(done)[0], f"the steer's answer is not first: {content}"
        entries = await asyncio.to_thread(claude_entries, vm, marker)
        first, steer = first_loop_end(entries), message_index(entries, status)
        notified = next((i for i, e in enumerate(entries) if "<task-notification>" in e.text), None)
        answered = next(
            (i for i, e in enumerate(entries) if e.kind == "assistant" and status in e.text), None
        )
        over = True  # the turn has answered: nothing to stop
        assert first is not None and steer is not None and answered is not None, entries
        assert first < steer, "the steer came before the first loop ended"
        assert steer < answered, "the transcript has no answer to the steer after it"
        assert notified is None or answered < notified, "the steer was answered after the end"
        scenario.timing(
            "claude: steer answered", entries[answered].at - entries[steer].at, high=15,
            target=True,
        )  # fmt: skip
        statuses = await chat.statuses(turn)
        assert len(statuses) == 1, f"{len(statuses)} turns ran; the steer must not start one"
        await chat.user.settled(statuses[0], DONE)  # Zulip may serve an older version (#877)
    finally:
        await clean_up(chat, vm, topic, marker, running=not over)


@pytest.mark.scenario(17)
async def test_claude_stop_while_a_subagent_works(chat: Chat, vm, scenario, nonce):
    """Part 2: `!stop` after the parent's first loop, while the sub-agent's command runs,
    ends the turn ⏹ (not ✅ with the first loop's text) and leaves no process of it."""
    if not installed.has(vm, installed.CLAUDE_SUBAGENT_STDIN):
        pytest.skip("the installed zab closes Claude's stdin at the first result (#814)")
    marker, topic = f"S17B-{nonce}", topic_name(17, nonce, "claude-stop")
    turn = await chat.say("claude", topic, background_task(marker, 300, f"SUB-{nonce}"))
    over = False
    try:
        await first_loop_over(vm, marker)
        await poll(lambda: marked(vm, marker), within=COMMAND_S, what="sub-agent's command")
        sent = time.monotonic()
        await chat.user.send(chat.stream, topic, "!stop")
        await chat.reply(turn, STOPPED, within=60)
        over = True
        scenario.timing("claude: ⏹ after !stop", time.monotonic() - sent, high=30)
        # SIGINT, SIGTERM 5 s later, SIGKILL 5 s after that (SPEC §9.6)
        with contextlib.suppress(TimeoutError):
            await poll(lambda: not marked(vm, marker), within=20, what="end of the command")
        left = marked(vm, marker)
        assert not left, f"the sub-agent's command outlived !stop: pids {left}"
        scenario.notes.append("claude: !stop ended the sub-agent's command")
    finally:
        await clean_up(chat, vm, topic, marker, running=not over)


@pytest.mark.scenario(17)
async def test_codex_turn_ends_with_a_subagent_running(chat: Chat, vm, scenario, nonce):
    """Part 3 (#822): a turn that ends while its sub-agent runs carries the ⚠️ note; the next
    turn, which waits for the sub-agent to finish, carries none."""
    if not installed.has(vm, installed.CODEX_SUBAGENT_NOTE):
        pytest.skip("the installed zab says nothing of a Codex sub-agent left running (#822)")
    marker, started, done = f"S17C-{nonce}", f"STARTED-{nonce}", f"DONE-{nonce}"
    topic = topic_name(17, nonce, "codex")
    turn = await chat.say(
        "codex",
        topic,
        f"This is a test of the bridge (`{marker}`). Start a sub-agent with spawn_agent. Its "
        f"task: run the shell command `python3 -c 'import time; time.sleep(45)' {marker}`, then "
        f"reply with exactly `SUB-{nonce}`. Leave it running: don't wait for it, don't stop "
        f"it, and end your turn at once with exactly `{started}`.",
    )
    over = False
    try:
        reply, took = await chat.reply(turn, answer(re.escape(started)), within=300)
        scenario.timing("codex: answer, the sub-agent running", took)
        complied = codex_left_it_running(reply["content"], await chat.statuses(turn), nonce)
        if complied is not True:
            pytest.fail(f"Codex didn't comply: {complied}, against the prompt", pytrace=False)
        assert re.search(CODEX_NOTE, reply["content"]), f"no note: {reply['content'][:400]}"
        follow = await chat.say(
            "codex",
            topic,
            "Now wait for your sub-agent with wait_agent, at most 50 seconds per call, until "
            f"it is done. Then reply with exactly `{done}` followed by the word it replied with.",
            mention_bot=False,
        )
        reply, took = await chat.reply(follow, answer(re.escape(done)), within=400)
        over = True
        scenario.timing("codex: the next turn's answer", took)
        assert f"SUB-{nonce}" in reply["content"], reply["content"][:300]
        assert not re.search(CODEX_NOTE, reply["content"]), f"a note again: {reply['content']}"
        scenario.notes.append("codex: the note at the first turn's end, none at the next")
    finally:
        await clean_up(chat, vm, topic, marker, running=not over)


@pytest.mark.scenario(17)
async def test_codex_stop_with_a_subagent_running(chat: Chat, vm, scenario, nonce):
    """Part 4 (#825): `!stop` about 45 s in, while the parent waits for its sub-agent, ends
    the turn ⏹ and aborts the sub-agent's turn (its rollout's `turn_aborted`). Whether the
    command it started survives is recorded, not asserted: Codex keeps a turn's exec
    sessions past an interrupt."""
    if not installed.has(vm, installed.CODEX_SUBAGENT_STOP):
        pytest.skip("the installed zab's !stop leaves a Codex sub-agent running (#825)")
    marker, topic = f"S17D-{nonce}", topic_name(17, nonce, "codex-stop")
    turn = await chat.say(
        "codex",
        topic,
        f"This is a test of the bridge (`{marker}`). Start a sub-agent with spawn_agent. Its "
        f"task: run the shell command `python3 -c 'import time; time.sleep(240)' {marker}` in "
        "an exec session, then reply with the single word done. Then wait for it with "
        f"wait_agent, at most 50 seconds per call, until it is done, and reply with "
        f"exactly `DONE-{nonce}`.",
    )
    over = False
    try:
        await poll(lambda: marked(vm, marker), within=COMMAND_S, what="sub-agent's command")
        await asyncio.sleep(max(0.0, 45 - (time.monotonic() - turn.sent_at)))
        sent = time.monotonic()
        await chat.user.send(chat.stream, topic, "!stop")
        await chat.reply(turn, STOPPED, within=60)
        over = True
        scenario.timing("codex: ⏹ after !stop", time.monotonic() - sent, high=30)
        await turn_aborted(vm, scenario, marker, sent, "codex")
    finally:
        await clean_up(chat, vm, topic, marker, running=not over)


@pytest.mark.scenario(17)
async def test_codex_stop_with_no_turn_running(chat: Chat, vm, scenario, nonce):
    """Part 5 (#847): a turn ends with its sub-agent running, as in part 3. `!stop` then,
    with no turn running, stops it: the bot says it stopped 1 sub-agent, and the sub-agent's
    rollout shows its turn aborted, as in part 4."""
    if not installed.has(vm, installed.CODEX_IDLE_STOP):
        pytest.skip("the installed zab's !stop does nothing with no turn running (#851)")
    marker, started = f"S17E-{nonce}", f"STARTED-{nonce}"
    topic = topic_name(17, nonce, "codex-idle-stop")
    turn = await chat.say(
        "codex",
        topic,
        f"This is a test of the bridge (`{marker}`). Start a sub-agent with spawn_agent. Its "
        f"task: run the shell command `python3 -c 'import time; time.sleep(240)' {marker}` in "
        f"an exec session, then reply with exactly `SUB-{nonce}`. Leave it running: don't "
        f"wait for it, don't stop it, and end your turn at once with exactly `{started}`.",
    )
    stopped = False
    try:
        reply, took = await chat.reply(turn, answer(re.escape(started)), within=300)
        scenario.timing("codex, no turn: answer, the sub-agent running", took)
        complied = codex_left_it_running(reply["content"], await chat.statuses(turn), nonce)
        if complied is not True:
            pytest.fail(f"Codex didn't comply: {complied}, against the prompt", pytrace=False)
        assert re.search(CODEX_NOTE, reply["content"]), f"no note: {reply['content'][:400]}"
        await poll(lambda: marked(vm, marker), within=COMMAND_S, what="sub-agent's command")
        stop = await chat.say("codex", topic, "!stop")
        found, took = await chat.reply(stop, IDLE_STOP, within=60)
        scenario.timing("codex, no turn: the answer to !stop", took, high=30)
        stopped = idle_stop_count(found["content"]) == 1
        scenario.notes.append(f"codex, no turn: {loaded_cost(vm) or 'not logged'}")
        assert stopped, f"the answer to !stop: {found['content'][:300]}"
        await turn_aborted(vm, scenario, marker, stop.sent_at, "codex, no turn")
    finally:
        await clean_up(chat, vm, topic, marker, running=not stopped)


@pytest.mark.scenario(17)
async def test_codex_stop_after_a_bridge_restart(chat: Chat, vm, scenario, nonce):
    """Part 6 (#858): as part 5, but the bridge restarts while the sub-agent runs, so no event
    of the new connection tells of it. `!stop` with no turn finds it among the daemon's loaded
    threads, which `thread/read` says are sub-agents, and stops it: the bot says it stopped 1,
    and the sub-agent's rollout shows its turn aborted."""
    if not installed.has(vm, installed.CODEX_LOADED_STOP):
        pytest.skip("the installed zab's !stop can't find a sub-agent across a restart (#858)")
    marker, started = f"S17F-{nonce}", f"STARTED-{nonce}"
    topic = topic_name(17, nonce, "codex-restart-stop")
    turn = await chat.say(
        "codex",
        topic,
        f"This is a test of the bridge (`{marker}`). Start a sub-agent with spawn_agent. Its "
        f"task: run the shell command `python3 -c 'import time; time.sleep(400)' {marker}` in "
        f"an exec session, then reply with exactly `SUB-{nonce}`. Leave it running: don't "
        f"wait for it, don't stop it, and end your turn at once with exactly `{started}`.",
    )
    stopped = False
    try:
        reply, took = await chat.reply(turn, answer(re.escape(started)), within=300)
        scenario.timing("codex, restart: answer, the sub-agent running", took)
        complied = codex_left_it_running(reply["content"], await chat.statuses(turn), nonce)
        if complied is not True:
            pytest.fail(f"Codex didn't comply: {complied}, against the prompt", pytrace=False)
        await poll(lambda: marked(vm, marker), within=COMMAND_S, what="sub-agent's command")
        restarted = time.monotonic()
        await asyncio.to_thread(vm.unit_action, "restart")
        scenario.timing("codex, restart: the bridge restarted", time.monotonic() - restarted)
        stop = await chat.say("codex", topic, "!stop")
        found, took = await chat.reply(stop, IDLE_STOP, within=180)
        scenario.timing("codex, restart: the answer to !stop", took, high=60)
        stopped = idle_stop_count(found["content"]) == 1
        line = loaded_cost(vm) or ""
        scenario.notes.append(f"codex, restart: {line or 'the loaded threads: not logged'}")
        assert stopped, f"the answer to !stop: {found['content'][:300]}"
        # after the restart no event tells of the sub-agent: only the loaded threads can
        counts = loaded_counts(line)
        assert counts and counts[1] >= 1, f"no sub-agent among the loaded threads: {line!r}"
        await turn_aborted(vm, scenario, marker, stop.sent_at, "codex, restart")
    finally:
        await clean_up(chat, vm, topic, marker, running=not stopped)
