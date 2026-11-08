"""Scenario 17 part 7's helpers (#668), offline: Claude's wait on a background sub-agent read
from its transcript, the status message's versions, the prompt, the skip probe against this
checkout's instructions, and the part itself against a fake chat and VM."""

from __future__ import annotations

import importlib.util
import json
import pathlib
from types import SimpleNamespace

import pytest

from e2e import test_scenarios_subagents as s17
from e2e import test_scenarios_subagents_notes as s17n
from e2e.harness import installed, report
from e2e.harness.cleanup import Leftover
from e2e.harness.rig_failure import RigFailure

WORKSHOP = pathlib.Path(__file__).resolve().parents[2] / "workshop"
FILES = WORKSHOP / "ami" / "files"
#: `workshop/admin/agent_instructions.py`, which writes the installed file, loaded by its path
_SPEC = importlib.util.spec_from_file_location(
    "agent_instructions", WORKSHOP / "admin" / "agent_instructions.py"
)
assert _SPEC is not None and _SPEC.loader is not None
ai = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ai)
AGENT = {
    "type": "tool_use", "id": "toolu_agent", "name": "Agent",
    "input": {"run_in_background": True, "prompt": "x"},
}  # fmt: skip
BASH = {"type": "tool_use", "name": "Bash", "input": {"command": "tail log"}}
RESULT = {"type": "tool_result", "tool_use_id": "t", "content": "ok"}


def notification(call: str = "toolu_agent", what: str = "Agent") -> str:
    """A task notification as Claude Code 2.1.283 words it: the ended task's call id first."""
    return (
        f"<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>{call}</tool-use-id>\n"
        f"<output-file>/tmp/x/tasks/a1.output</output-file>\n<status>completed</status>\n"
        f'<summary>{what} "check" finished</summary>\n</task-notification>'
    )


NOTIFIED = notification()


def record(kind: str, at: int, *blocks, text: str | None = None, sidechain: bool = False):
    """A transcript line `at` seconds after 10:00:00."""
    content = text if text is not None else list(blocks)
    minutes, seconds = divmod(at, 60)
    return json.dumps(
        {"type": kind, "timestamp": f"2026-11-07T10:{minutes:02d}:{seconds:02d}.000Z",
         "isSidechain": sidechain, "message": {"role": kind, "content": content}}
    )  # fmt: skip


def say(words: str) -> dict:
    return {"type": "text", "text": words}


def check(at: int, note: str | None) -> list[str]:
    """A check of the log at `at` (its call and result), then the note after it, if any."""
    lines = [record("assistant", at, BASH), record("user", at + 40, RESULT)]
    return lines + ([record("assistant", at + 42, say(note))] if note is not None else [])


def waited(*checks: tuple[int, str | None], end: int = 300) -> list[s17.Entry]:
    lines = [
        record("user", 0, text="This is a test of the bridge (`S17N0-x`)."),
        record("assistant", 4, say("I'll give it to a sub-agent.")),
        record("assistant", 5, AGENT),
        record("user", 6, RESULT),
        record("assistant", 8, say("Started it; checking its log every 40 s.")),
        record("assistant", 30, say("sub-agent thinking"), sidechain=True),
    ]
    for at, note in checks:
        lines += check(at, note)
    lines += [record("user", end, text=NOTIFIED), record("assistant", end + 20, say("Done."))]
    return s17.transcript("\n".join(lines))


def test_the_gaps_run_from_the_launch_through_each_note_to_the_notification():
    wait = s17n.waiting(waited((10, "40 s in"), (60, "1 min 30 s in"), (110, None), (160, "x")))
    assert isinstance(wait, s17n.Wait)
    # the launch at 5; notes at 8, 52, 102, 202; the notification at 300
    assert wait.gaps == (3.0, 44.0, 50.0, 100.0, 98.0)
    assert wait.longest == 100.0 and wait.seconds == 295.0
    assert (wait.notes, wait.checks, wait.silent) == (4, 4, 1)  # the check at 160 was silent
    assert wait.text().startswith("waited 295 s, 4 notes, 4 checks (1 right after another")
    assert wait.text().endswith("gaps between notes 3, 44, 50, 100, 98 s")
    # the sub-agent's own records and the answer after the notification are not notes
    every = s17n.waiting(waited(*((t, "n") for t in range(10, 250, 50)), end=300))
    assert isinstance(every, s17n.Wait) and every.silent == 0 and every.longest <= 50


def stamp(at: int) -> str:
    minutes, seconds = divmod(at, 60)
    return f"2026-11-07T10:{minutes:02d}:{seconds:02d}.000Z"


def queued(at: int, prompt: str) -> list[str]:
    """A message that arrived during a tool call, as Claude Code 2.1.283 (sdk-cli) records it
    once the call has ended: the queue's `enqueue`, written at once, then, after the call's
    result, an `attachment` of type `queued_command` stamped with the enqueue time, and the
    queue's `remove` (the test account's transcripts; review of #922, B1). Here the part
    before the call's result."""
    return [json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(at),
                        "content": prompt})]  # fmt: skip


def taken(at: int, enqueued: int, prompt: str) -> list[str]:
    """... and the part after it: the attachment, stamped `enqueued`, and the `remove` at `at`."""
    attachment = {
        "type": "attachment", "timestamp": stamp(enqueued), "isSidechain": False,
        "attachment": {"type": "queued_command", "prompt": prompt,
                       "commandMode": "task-notification", "timestamp": stamp(enqueued)},
    }  # fmt: skip
    remove = {"type": "queue-operation", "operation": "remove", "timestamp": stamp(at),
              "content": prompt}  # fmt: skip
    return [json.dumps(attachment), json.dumps(remove)]


def compliant(*, other: bool = False) -> list[str]:
    """A perfect waiter in 2.1.283's shape: a note every 45 s, the sub-agent ending during a
    check and taken in at its end. With `other`, a background command of Claude's own ends
    first, its notification naming another call."""
    lines = [
        record("user", 0, text="go `S17N0-x`"),
        record("assistant", 5, AGENT),
        record("user", 6, RESULT),
    ]
    for t in range(10, 235, 45):
        lines += [
            record("assistant", t, say(f"{t} s in, its log unchanged")),
            record("assistant", t + 1, BASH),
        ]
        if other and t == 55:
            lines += queued(70, notification("toolu_bash", "Background command"))
        lines.append(record("user", t + 41, RESULT))
        if other and t == 55:
            lines += taken(t + 41, 70, notification("toolu_bash", "Background command"))
    lines += [record("assistant", 235, say("235 s in")), record("assistant", 236, BASH)]
    lines += queued(270, NOTIFIED)
    lines += [record("user", 276, RESULT), *taken(276, 270, NOTIFIED)]
    return [*lines, record("assistant", 290, say("Its findings: ... DONE"))]


def test_a_notification_taken_in_after_a_check_ends_the_wait():
    """B1 of #922's review: a compliant Claude is always inside a check when its sub-agent ends,
    so the notification comes as an attachment, and no `user` record carries it. It ends the
    window when it was taken in, at the check's end (276 s), not when it was queued (270 s)."""
    entries = s17.transcript("\n".join(compliant()))
    note = next(e for e in entries if e.notified is not None)
    assert note.kind == "user" and note.notified == "toolu_agent" and note.at > 270
    wait = s17n.waiting(entries)
    assert isinstance(wait, s17n.Wait), wait
    assert wait.seconds == 271.0 and wait.longest == 45.0 and wait.gaps[-1] == 41.0
    assert (wait.notes, wait.checks, wait.silent) == (6, 6, 0)


def test_only_the_sub_agents_own_notification_ends_the_wait():
    """B1: a background command of Claude's own sends a notification too, which must not end
    the window early: the one taken in matches the Agent call's id."""
    entries = s17.transcript("\n".join(compliant(other=True)))
    assert [e.notified for e in entries if e.notified is not None] == ["toolu_bash", "toolu_agent"]
    wait = s17n.waiting(entries)
    assert isinstance(wait, s17n.Wait) and wait.seconds == 271.0
    # a notification that names no call still ends a wait whose call it can't tell apart
    bare = "<task-notification><status>completed</status></task-notification>"
    lines = [record("assistant", 5, AGENT), record("user", 100, text=bare)]
    assert isinstance(s17n.waiting(s17.transcript("\n".join(lines))), s17n.Wait)


def test_parallel_calls_of_one_response_count_as_one_check():
    """N5 of #922's review: Claude Code writes each block of a response as its own record, so
    two calls made at once are two records of one response: one check, never a silent one."""

    def call(at: int, response: str) -> str:
        line = json.loads(record("assistant", at, BASH))
        line["message"]["id"] = response
        return json.dumps(line)

    lines = [
        record("assistant", 5, AGENT),
        call(10, "msg_1"),
        call(10, "msg_1"),
        record("user", 50, RESULT),
        record("assistant", 52, say("47 s in")),
        call(53, "msg_2"),
        record("user", 90, RESULT),
        call(91, "msg_3"),  # a second check with no note before it: silent
        record("user", 120, text=NOTIFIED),
    ]
    wait = s17n.waiting(s17.transcript("\n".join(lines)))
    assert isinstance(wait, s17n.Wait) and (wait.checks, wait.silent) == (3, 1)


def test_a_record_with_a_note_and_a_call_counts_the_note_first():
    lines = [
        record("assistant", 5, AGENT),
        record("assistant", 50, say("45 s in"), BASH),
        record("assistant", 95, say("1 min 30 s in"), BASH),
        record("user", 120, text=NOTIFIED),
    ]
    wait = s17n.waiting(s17.transcript("\n".join(lines)))
    assert isinstance(wait, s17n.Wait)
    assert (wait.notes, wait.checks, wait.silent) == (2, 2, 0)
    assert wait.gaps == (45.0, 45.0, 25.0)


def test_a_wait_that_cannot_be_measured_says_why():
    no_launch = s17.transcript(record("assistant", 5, say("I'll do it myself.")))
    assert s17n.waiting(no_launch) == "Claude started no background sub-agent"
    unfinished = s17.transcript(record("assistant", 5, AGENT))
    assert "no notification" in s17n.waiting(unfinished)


def test_the_status_messages_versions_are_timed_from_its_post():
    history = [{"timestamp": 1000}, {"timestamp": 1062}, {"timestamp": 1363.0}]
    assert s17n.edit_times(history) == [62.0, 363.0]
    assert s17n.edit_times([{"timestamp": 5}]) == [] and s17n.edit_times([]) == []
    # N5 of #922's review: a version without a time is left out, never read as 0
    assert s17n.edit_times([{"timestamp": 1000}, {}, {"timestamp": 1061}]) == [61.0]
    assert s17n.edit_times([{}, {"timestamp": 1061}]) == []


def test_the_prompt_runs_the_marked_command_and_says_nothing_of_notes():
    prompt = s17n.notes_task("S17N1-x", "Prove it.", "DONE-x-1")
    assert "(`S17N1-x`)" in prompt and "Prove it." in prompt and "`DONE-x-1`." in prompt
    assert f"time.sleep({s17n.SLEEP_S})' S17N1-x`" in prompt and "twice" in prompt
    assert not {"note", "progress", "check", "log"} & set(prompt.lower().replace(".", " ").split())
    # each run of the command fits the Bash tool's default timeout of 2 minutes
    assert s17n.SLEEP_S < 120 and 2 * s17n.SLEEP_S > s17n.WAIT_S
    assert len(s17n.TASKS) >= 3  # #668: at least 3 topics


def test_the_skip_probe_finds_this_checkouts_instructions():
    """The installed block holds the text with its line breaks: the probe reads across them."""
    block = installed.MANAGED.search(ai.block((FILES / "agent-instructions.md").read_text()))
    assert block and installed.NOTES_WHILE_WAITING.search(block.group(1))
    assert not installed.NOTES_WHILE_WAITING.search("with a progress note after every check")


def test_part_7_is_a_part_of_scenario_17():
    marks = s17n.test_claude_notes_while_a_subagent_works.pytestmark
    assert any(m.name == "scenario" and m.args == (17,) for m in marks)
    assert report.SCENARIOS[17][1] == "fast"


# ---------------------------------------------------------------- the part, against fakes


class Vm:
    """The test VM as the part reads it: the installed instructions, each marker's
    transcript, and a process list with nothing marked."""

    def __init__(self, transcripts: dict[str, list[str]], told: bool = True) -> None:
        self.transcripts = transcripts
        text = (FILES / "agent-instructions.md").read_text()
        self.file = ai.block(text if told else "old text\n")

    def run(self, argv, check=True, timeout=120):
        if argv[-2:] == ["--", installed.INSTRUCTIONS]:
            return SimpleNamespace(ok=True, stdout=self.file, stderr="")
        if argv[:2] == ["ps", "-eo"]:
            return SimpleNamespace(ok=True, stdout="", stderr="")
        assert s17.CLAUDE_TRANSCRIPT in argv, argv
        return SimpleNamespace(ok=True, stdout="\n".join(self.transcripts[argv[-1]]), stderr="")


class Chat:
    """`zab-tester` in the sandbox: each turn answers at once, with one status message."""

    def __init__(
        self, fail: dict[str, BaseException] | None = None, history_fails: bool = False
    ) -> None:
        self.fail, self.stream, self.sent = fail or {}, 7, []
        self.history_fails = history_fails
        self.user = SimpleNamespace(history=self.history, send=self.send)

    async def say(self, kind, topic, text):
        assert kind == "claude"
        return SimpleNamespace(topic=topic, marker=text.split("`")[1])

    async def reply(self, turn, pattern, within):
        assert within == s17n.TURN_S
        if turn.topic.split("-")[3] in self.fail:
            raise self.fail[turn.topic.split("-")[3]]
        return {"content": "DONE"}, 1.0

    async def statuses(self, turn):
        return [{"id": 41, "content": "✅ **Done**"}]

    async def history(self, message_id):
        if self.history_fails:
            raise ConnectionError("Zulip answered 502")
        return [{"timestamp": 1000}, {"timestamp": 1061}, {"timestamp": 1400}]

    async def send(self, stream, topic, text):
        self.sent.append((topic, text))


def lines_for(marker: str, *checks: tuple[int, str | None]) -> list[str]:
    head = [record("user", 0, text=f"go `{marker}`"), record("assistant", 5, AGENT)]
    body = [line for at, note in checks for line in check(at, note)]
    return [*head, *body, record("user", 280, text=NOTIFIED)]


def steady(marker: str) -> list[str]:
    return lines_for(marker, *((t, "still running") for t in range(10, 240, 45)))


async def _nosleep(_):
    return None


async def test_the_part_reports_each_topics_gaps():
    nonce = "100000-abc"
    markers = [f"S17N{i}-{nonce}" for i in range(3)]
    vm = Vm({m: steady(m) for m in markers})
    scenario = report.ScenarioResult(17, "x")
    await s17n.test_claude_notes_while_a_subagent_works(Chat(), vm, scenario, nonce)
    assert [t.name for t in scenario.timings] == [
        f"claude {label}: longest gap between notes" for label, _ in s17n.TASKS
    ]
    assert all(t.ok for t in scenario.timings) and scenario.effective == "passed"
    assert scenario.notes[0].startswith("claude curve: waited 275 s, 6 notes, 6 checks (0 ")
    assert scenario.notes[0].endswith("; status edits at +61, +400 s")
    # a silent check: the timing misses its bound, so the row fails
    vm.transcripts[markers[1]] = lines_for(markers[1], (0, "a"), (45, None), (90, "b"))
    scenario = report.ScenarioResult(17, "x")
    await s17n.test_claude_notes_while_a_subagent_works(Chat(), vm, scenario, nonce)
    assert [t.ok for t in scenario.timings] == [True, False, True]
    assert scenario.effective == "failed" and "1 right after another" in scenario.notes[1]


async def test_the_part_fails_for_each_topic_it_could_not_measure(monkeypatch):
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)  # the clean-up's wait after `!stop`
    nonce = "100000-abd"
    markers = [f"S17N{i}-{nonce}" for i in range(3)]
    vm = Vm({markers[0]: steady(markers[0]), markers[1]: [record("assistant", 5, say("hi"))]})
    chat = Chat(fail={"class": TimeoutError("no reply within 900 s")})
    with pytest.raises(pytest.fail.Exception) as failed:
        await s17n.test_claude_notes_while_a_subagent_works(
            chat, vm, report.ScenarioResult(17, "x"), nonce
        )
    message = str(failed.value)
    assert "claude squares: Claude started no background sub-agent" in message
    assert "claude class-number: TimeoutError: no reply within 900 s" in message
    assert "curve" not in message
    # the topic whose turn didn't answer gets its `!stop` (part 1's clean-up)
    assert [text for _, text in chat.sent] == ["!stop"]


async def test_a_rig_failure_runs_the_part_again_and_a_leftover_comes_first(monkeypatch):
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    nonce = "100000-abe"
    vm = Vm({f"S17N{i}-{nonce}": steady(f"S17N{i}-{nonce}") for i in range(3)})
    rig = RigFailure("Zulip served an older version")
    with pytest.raises(RigFailure):  # nothing else failed: the part runs again
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(fail={"squares": rig}), vm, report.ScenarioResult(17, "x"), nonce
        )
    left = Leftover("the command is still running", RuntimeError("x"), stops=False, after="a")
    more = Leftover("another command is still running", RuntimeError("y"), stops=False)
    with pytest.raises(Leftover) as raised:
        fails = {"squares": rig, "curve": left, "class": more}
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(fail=fails), vm, report.ScenarioResult(17, "x"), nonce
        )
    assert raised.value is left
    # every failure, the rig's line and the other leftover (N4) go with the first leftover
    assert raised.value.after == (
        f"claude curve: a; claude squares: {rig}; also left: another command is still running"
    )


async def test_a_rig_failure_never_erases_a_failure_seen_in_another_topic(monkeypatch):
    """B2 of #922's review, its case: a rerun would drop this run's ❌ timing and its failures
    (`refusal.retry`), and a passing rerun would PASS the row. The part fails instead, saying
    all three."""
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    nonce = "100000-abf"
    markers = [f"S17N{i}-{nonce}" for i in range(3)]
    vm = Vm({
        markers[0]: lines_for(markers[0], (0, "a"), (45, None), (90, "b")),  # a 148 s gap
        markers[1]: [record("assistant", 5, say("I'll do it myself."))],
    })  # fmt: skip
    scenario = report.ScenarioResult(17, "x")
    rig = RigFailure("Zulip served an older version")
    with pytest.raises(pytest.fail.Exception) as failed:
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(fail={"class": rig}), vm, scenario, nonce
        )
    message = str(failed.value)
    assert "claude squares: Claude started no background sub-agent" in message
    assert f"claude class-number: {rig}" in message and "older version" in message
    assert [(t.seconds, t.ok) for t in scenario.timings] == [(148.0, False)]
    # a gap over its bound alone, beside a rig failure, fails too
    vm.transcripts[markers[1]] = steady(markers[1])
    with pytest.raises(pytest.fail.Exception, match="class-number: a rig failure"):
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(fail={"class": rig}), vm, report.ScenarioResult(17, "x"), nonce
        )


async def test_in_a_release_candidates_run_the_bound_is_a_target(monkeypatch):
    """#922 (N1, the maintainer's decision): hard by default; `run.py --require rc` sets
    `ZAB_E2E_RC=1`, and the gap is then shown, never a failure. What measured nothing still
    fails, and a rig failure beside a missed target runs the part again."""
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    monkeypatch.setenv(s17n.RC_ENV, "1")
    nonce = "100000-ac0"
    markers = [f"S17N{i}-{nonce}" for i in range(3)]
    vm = Vm({m: steady(m) for m in markers})
    vm.transcripts[markers[0]] = lines_for(markers[0], (0, "a"), (45, None), (90, "b"))
    scenario = report.ScenarioResult(17, "x")
    await s17n.test_claude_notes_while_a_subagent_works(Chat(), vm, scenario, nonce)
    assert [t.target for t in scenario.timings] == [True] * 3
    assert not scenario.timings[0].ok and scenario.effective == "passed"
    assert "⚠️" in scenario.timings[0].text()
    with pytest.raises(RigFailure):
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(fail={"class": RigFailure("x")}), vm, report.ScenarioResult(17, "x"), nonce
        )
    vm.transcripts[markers[1]] = [record("assistant", 5, say("I'll do it myself."))]
    with pytest.raises(pytest.fail.Exception, match="no background sub-agent"):
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(), vm, report.ScenarioResult(17, "x"), nonce
        )


async def test_an_unread_edit_history_fails_no_topic():
    """N3 of #922's review: the status message's versions are only shown."""
    nonce = "100000-ac1"
    vm = Vm({f"S17N{i}-{nonce}": steady(f"S17N{i}-{nonce}") for i in range(3)})
    scenario = report.ScenarioResult(17, "x")
    await s17n.test_claude_notes_while_a_subagent_works(
        Chat(history_fails=True), vm, scenario, nonce
    )
    assert scenario.effective == "passed" and len(scenario.timings) == 3
    assert scenario.notes[0].endswith("; status edits unread (ConnectionError: Zulip answered 502)")


async def test_the_part_skips_on_instructions_before_668():
    with pytest.raises(pytest.skip.Exception, match="predate #668"):
        await s17n.test_claude_notes_while_a_subagent_works(
            Chat(), Vm({}, told=False), report.ScenarioResult(17, "x"), "n"
        )
