"""Scenario 17's helpers (#660), offline: a Claude transcript read for the parent's first
loop, the steer and the notification, and the marked processes listed and ended, against a
fake VM, so that their code runs before a live run."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
from types import SimpleNamespace

import pytest

from e2e import test_scenarios_subagents as s17
from e2e.harness import installed, report
from e2e.harness.cleanup import Leftover


def record(kind: str, at: str, *blocks, sidechain: bool = False, text: str | None = None):
    content = text if text is not None else list(blocks)
    message = {"role": kind, "content": content}
    return json.dumps(
        {"type": kind, "timestamp": f"2026-11-05T10:00:{at}Z", "isSidechain": sidechain,
         "message": message}
    )  # fmt: skip


AGENT = {"type": "tool_use", "name": "Agent", "input": {"run_in_background": True, "prompt": "x"}}
BASH = {"type": "tool_use", "name": "Bash", "input": {"command": "tail log"}}
RESULT = {"type": "tool_result", "tool_use_id": "t", "content": "Async agent launched"}


def text(words: str) -> dict:
    return {"type": "text", "text": words}


def test_the_first_loop_ends_at_its_last_text_after_the_launch():
    lines = [
        record("user", "00", text="This is a test of the bridge (`S17A-x`)."),
        record("assistant", "02", text("I'll start a sub-agent.")),
        record("assistant", "03", AGENT),
        record("user", "03", RESULT),
        record("assistant", "05", text("Sleeping."), sidechain=True),  # the sub-agent's
        record("assistant", "06", text("Started it.")),
        "not json",
        json.dumps({"type": "queue-operation", "operation": "enqueue"}),
    ]
    entries = s17.transcript("\n".join(lines))
    assert [e.kind for e in entries] == ["user", "assistant", "assistant", "user", "assistant"]
    end = s17.first_loop_end(entries)
    assert end == 4 and entries[end].text == "Started it." and entries[end].at > entries[0].at
    # while it works on (a tool call after its text), the loop isn't over
    working = s17.transcript("\n".join([*lines, record("assistant", "07", BASH)]))
    assert s17.first_loop_end(working) is None
    # no sub-agent yet: none either
    assert s17.first_loop_end(s17.transcript("\n".join(lines[:2]))) is None


def test_a_message_queued_during_a_call_is_a_user_entry_when_taken_in():
    """#922's review, B1: Claude Code 2.1.283 records a message that arrives during a tool call
    as an `attachment` (`queued_command`), written after the call's result and stamped with the
    time it was queued. It is a user entry at the time it was taken in, after the result, and a
    notification's call id is read; the queue's own records are left out. So parts 1 and 2 see
    a notification that ends the first loop during a call, as `first_loop_end` reads one."""
    note = (
        "<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n"
        "<status>completed</status>\n</task-notification>"
    )
    agent = {**AGENT, "id": "toolu_1"}
    attachment = json.dumps({
        "type": "attachment", "timestamp": "2026-11-05T10:00:05Z", "isSidechain": False,
        "attachment": {"type": "queued_command", "prompt": note,
                       "commandMode": "task-notification"},
    })  # fmt: skip
    lines = [
        record("user", "00", text="go S17A-x"),
        record("assistant", "01", agent),
        record("user", "01", RESULT),
        record("assistant", "02", BASH),
        json.dumps({"type": "queue-operation", "operation": "enqueue", "content": note}),
        record("user", "09", RESULT),
        attachment,
        json.dumps({"type": "attachment", "attachment": {"type": "prompt_snapshot"}}),
        record("assistant", "11", text("It finished.")),
    ]
    entries = s17.transcript("\n".join(lines))
    assert [e.kind for e in entries] == ["user", "assistant", "user", "assistant", "user", "user",
                                         "assistant"]  # fmt: skip
    queued = entries[5]
    assert queued.notified == "toolu_1" and queued.text == note and not queued.tool_result
    assert queued.at == entries[4].at  # taken in at the call's end (09), not queued (05)
    assert entries[1].agent_id == "toolu_1" and entries[1].background_agent
    assert all(e.notified is None for e in entries[:5])
    assert s17.first_loop_end(entries) is None  # the loop went on working: no end before it


def test_the_steer_its_answer_and_the_notification_are_found_in_order():
    lines = [
        record("assistant", "03", AGENT),
        record("user", "03", RESULT),
        record("assistant", "06", text("Started it.")),
        record("user", "10", text="What's the status? Reply with `STAT-x` and one sentence."),
        record("assistant", "13", text("STAT-x: it is still sleeping.")),
        record("user", "70", text="<task-notification>…SUB-x…</task-notification>"),
        record("assistant", "72", text("DONE-x SUB-x")),
    ]
    entries = s17.transcript("\n".join(lines))
    assert s17.first_loop_end(entries) == 2  # the steer ends the scan
    steer = s17.message_index(entries, "STAT-x")
    assert steer == 3 and entries[4].at - entries[steer].at == 3
    assert s17.message_index(entries, "SUB-x") == 5  # the notification, as a message
    assert s17.message_index(entries, "launched") is None  # a tool result is no message


class Vm:
    """`ps` lists the given lines; `kill` is recorded as (signal, pid), and a process ends on
    a signal in `dies_on`."""

    def __init__(self, listing: list[str], dies_on=("TERM", "KILL")) -> None:
        self.listing, self.dies_on, self.killed = list(listing), dies_on, []

    def run(self, argv, check=True, timeout=120):
        if argv[:1] == ["ps"]:
            return SimpleNamespace(ok=True, stdout="\n".join(self.listing))
        assert argv[:4] == ["sudo", "-u", "agent", "kill"] and argv[5] == "--", argv
        signal, pid = argv[4].removeprefix("-"), int(argv[-1])
        self.killed.append((signal, pid))
        if signal in self.dies_on:
            self.listing = [x for x in self.listing if x.split()[0] != str(pid)]
        return SimpleNamespace(ok=True, stdout="")


def test_marked_lists_the_python_command_not_its_shell_nor_ps():
    vm = Vm(
        [
            "  101 /bin/bash -c python3 -c 'import time; time.sleep(60)' S17A-x",
            "  102 python3 -c import time; time.sleep(60) S17A-x",
            "  103 /usr/bin/python3 -c import time; time.sleep(300) S17A-x",
            "  104 python3 -c import time; time.sleep(60) S17A-y",
            "  105 ps -eo pid=,args=",
            "  106 sudo -u agent grep S17A-x",
        ]
    )
    assert s17.marked(vm, "S17A-x") == [102, 103]
    s17.end_marked(vm, "S17A-x", sleep=lambda _: None)
    assert vm.killed == [("TERM", 102), ("TERM", 103)]  # gone: no SIGKILL
    assert s17.marked(vm, "S17A-x") == [] and "  104 python3" in "\n".join(vm.listing)


def test_end_marked_kills_what_ignores_sigterm_and_says_what_outlives_sigkill():
    line = "  102 python3 -c import time; time.sleep(60) S17A-x"
    vm = Vm([line], dies_on=("KILL",))
    s17.end_marked(vm, "S17A-x", wait=2, sleep=lambda _: None)
    assert vm.killed == [("TERM", 102), ("KILL", 102)]
    stuck = Vm([line], dies_on=())
    with pytest.raises(RuntimeError, match=r"pids \[102\] outlived SIGKILL"):
        s17.end_marked(stuck, "S17A-x", wait=2, sleep=lambda _: None)
    s17.end_marked(Vm([]), "S17A-x")  # nothing left: nothing to do


class Chat:
    """What `clean_up` sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.stream = 7
        self.user = SimpleNamespace(send=self.send)

    async def send(self, stream, topic, text):
        self.sent.append((topic, text))


async def test_clean_up_stops_a_running_turn_then_ends_the_command(monkeypatch):
    """N2 of #838's review: `!stop` first while the turn may run, then the kill, as an
    `undo` step whose failure is a `Leftover` after the part's own."""
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    line = "  102 python3 -c import time; time.sleep(60) S17A-x"
    chat, vm = Chat(), Vm([line])
    await s17.clean_up(chat, vm, "t", "S17A-x", running=True)
    assert chat.sent == [("t", "!stop")] and vm.killed == [("TERM", 102)]
    chat, vm = Chat(), Vm([line])
    await s17.clean_up(chat, vm, "t", "S17A-x", running=False)
    assert chat.sent == [] and vm.killed == [("TERM", 102)]
    monkeypatch.setattr(s17, "end_marked", _outlives)
    with pytest.raises(Leftover) as left:
        try:
            raise AssertionError("the part's own failure")
        finally:
            await s17.clean_up(Chat(), Vm([]), "t", "S17A-x", running=False)
    assert not left.value.stops and "the part's own failure" in left.value.after


def test_codex_compliance_is_told_from_a_missing_note():
    """N1 of #838's review: a Codex that waited for its sub-agent, or stopped it, is reported
    as such, apart from a bridge that gave no note."""
    running = [{"content": "✅ **Done** · 0m 12s · 2 actions\n- ✅ sub-agent /root/c started"}]
    assert s17.codex_left_it_running("STARTED-x", running, "x") is True
    assert "waited" in s17.codex_left_it_running("STARTED-x SUB-x", running, "x")
    for end in ("completed", "interrupted"):
        statuses = [{"content": running[0]["content"] + f"\n- ✅ sub-agent /root/c {end}"}]
        assert s17.codex_left_it_running("STARTED-x", statuses, "x").endswith(
            f"{end} within the turn"
        )


def _outlives(vm, marker, **_):
    raise RuntimeError("pids [102] outlived SIGKILL")


async def _nosleep(_):
    return None


def growing(stages):
    """A VM whose transcript read returns the next stage each time (the last one again)."""
    reads = iter(range(10**6))

    def run(argv, check=True, timeout=120):
        stage = stages[min(next(reads), len(stages) - 1)]
        return SimpleNamespace(ok=True, stdout="\n".join(stage))

    return SimpleNamespace(run=run)


FIRST_LOOP = [
    record("user", "00", text="go S17A-x"),
    record("assistant", "01", AGENT),
    record("user", "01", RESULT),
    record("assistant", "02", text("Started it.")),
]


async def test_first_loop_over_fails_when_claude_goes_on_working(monkeypatch):
    """N5 of #838's review (the reviewer's probe): a tool call within the 3 s check fails
    "went on working"."""
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    more = [*FIRST_LOOP, record("assistant", "03", BASH)]
    with pytest.raises(pytest.fail.Exception, match="went on working"):
        await s17.first_loop_over(growing([FIRST_LOOP, more]), "S17A-x")


async def test_first_loop_over_takes_a_sidechain_record_for_no_work(monkeypatch):
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    side = [*FIRST_LOOP, record("assistant", "03", text("sleeping"), sidechain=True)]
    entries = await s17.first_loop_over(growing([FIRST_LOOP, side]), "S17A-x")
    assert s17.first_loop_end(entries) == 3


async def test_poll_returns_the_first_answer_or_times_out(monkeypatch):
    answers = iter([[], [], [7]])
    assert await s17.poll(lambda: next(answers), within=5, every=0.01, what="x") == [7]
    with pytest.raises(TimeoutError, match="no thing within 0 s"):
        await s17.poll(lambda: None, within=0.05, every=0.01, what="thing")


def test_scenario_17_is_listed_with_the_fast_ones():
    assert report.SCENARIOS[17] == ("Sub-agents across a turn's end (#660)", "fast")
    assert 17 in report.select("fast") and 17 not in report.select("slow")


@pytest.mark.parametrize(
    "snippet",
    [
        installed.CLAUDE_SUBAGENT_STDIN,  # #814
        installed.CODEX_SUBAGENT_NOTE,  # #822
        installed.CODEX_SUBAGENT_STOP,  # #825
        installed.CODEX_IDLE_STOP,  # #851
        installed.CODEX_LOADED_STOP,  # #858
    ],
)
def test_the_probes_find_this_build(snippet, capsys):
    """Each probe runs against this checkout, which has all five fixes, and prints `True`,
    as `installed.has` reads it."""
    exec(snippet)  # noqa: S102 - a fixed snippet of `harness/installed.py`
    assert capsys.readouterr().out.strip() == "True"


def test_commands_run_as_agent_start_in_its_home():
    """Scenario 17's first live run: SSH lands in ubuntu's home, which `agent` can't enter,
    and there `find -exec` (`CLAUDE_TRANSCRIPT`) failed before it ran anything, so the part
    waited 180 s for a transcript it never read. Both ways of running as `agent` now start in
    its home."""
    from e2e import test_scenarios_features as features
    from e2e.harness.vm import AGENT_HOME, Vm

    assert AGENT_HOME == "/home/agent"
    assert features.as_agent("true")[4:7] == ["env", "-C", AGENT_HOME]
    machine = Vm("203.0.113.9", key=pathlib.Path("k"), known_hosts=pathlib.Path("h"))
    machine._uid = 1001
    assert machine.as_agent("true")[4:7] == ["env", "-C", AGENT_HOME]


#: Runs `${@:3}` from directory `$1`, closed to its user once there, as ubuntu's home is to
#: `agent` when SSH runs a command as `agent` from it.
FROM_A_CLOSED_DIRECTORY = 'cd "$1" && chmod 000 "$1" && "${@:3}"; s=$?; chmod 700 "$1"; exit $s'


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters a closed directory")
def test_the_transcript_script_needs_a_cwd_it_can_enter_and_gets_one(tmp_path):
    """The live failure reproduced: from a directory its user can't enter, the script's
    `find -exec` fails before it runs anything, and prints nothing; started with `env -C`, as
    `as_agent` now starts it, it finds the transcript."""
    projects = tmp_path / "home/agent/.claude/projects/p"
    projects.mkdir(parents=True)
    (projects / "s.jsonl").write_text('{"type": "user", "message": {"content": "MARK"}}\n')
    script = s17.CLAUDE_TRANSCRIPT.replace("/home/agent", str(tmp_path / "home/agent"))
    closed = tmp_path / "ubuntu"
    closed.mkdir()
    out = {}
    for name, prefix in (("closed", []), ("env -C", ["env", "-C", str(tmp_path)])):
        argv = ["bash", "-c", FROM_A_CLOSED_DIRECTORY, "bash", str(closed), "-", *prefix]
        argv += ["bash", "-c", script, "bash", "MARK"]
        run = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        out[name] = run.stdout
    assert "MARK" not in out["closed"]
    assert "MARK" in out["env -C"]


def test_the_answer_to_a_stop_with_no_turn_is_read_as_the_bridge_words_it():
    """Part 5 (#847) reads the bridge's own answers (`zab.core.commands`): a change of their
    wording fails here, not in a live run."""
    from zab.core import commands as c

    assert [s17.idle_stop_count(c.stopped_subagents(n)) for n in (1, 3)] == [1, 3]
    assert s17.idle_stop_count(c.stopped_subagents(2, unseen=True)) == 2
    assert s17.idle_stop_count(c.stopped_subagents(0, unseen=True)) is None  # unknown, not 0
    assert s17.idle_stop_count(c.NO_TURN_NOR_SUBAGENT) == s17.idle_stop_count(c.NO_TURN) == 0
    for other in (c.SUBAGENTS_UNCHECKED, c.TURN_ELSEWHERE, c.UNKNOWN, "⏹ **Stopped** · 0m 3s"):
        assert s17.idle_stop_count(other) is None, other
    answers = (c.stopped_subagents(1), c.NO_TURN_NOR_SUBAGENT, c.SUBAGENTS_UNCHECKED)
    for answer in (*answers, c.TURN_ELSEWHERE):  # each ends the wait, at once
        assert re.match(s17.IDLE_STOP, answer), answer
    assert not re.match(s17.IDLE_STOP, c.UNKNOWN)


def test_the_loaded_cost_is_the_newest_line_of_the_bridge_log():
    """Parts 5 and 6 note what the read of the loaded threads found and cost, from the line
    `CodexBackend._loaded_subagents` logs."""
    lines = [
        json.dumps({"event": "subagent_loaded", "msg": "Loaded threads read: 3, 1 sub-agents"}),
        "not json",
        json.dumps({"event": "zab.bridge", "msg": "unrelated"}),
        json.dumps({"event": "subagent_loaded", "msg": "Loaded threads read: 4, 1 sub-agents"}),
    ]

    def run(argv, check=True, timeout=120):
        assert "tail" in argv and s17.BRIDGE_LOG in argv, argv
        return SimpleNamespace(ok=True, stdout="\n".join(lines))

    vm = SimpleNamespace(run=run)
    assert s17.loaded_cost(vm) == "Loaded threads read: 4, 1 sub-agents"
    assert s17.loaded_cost(vm, "subagent_listing") is None
    lines[:] = lines[1:3]
    assert s17.loaded_cost(vm) is None


class Scenario:
    def __init__(self) -> None:
        self.timings, self.notes = [], []

    def timing(self, name, seconds, **limits):
        self.timings.append((name, limits))


class Rollouts:
    """The sub-agent's rollout as each read finds it (the last one again), and `ps`."""

    def __init__(self, reads: list[str], listing: list[str]) -> None:
        self.reads, self.listing, self.calls = reads, listing, 0

    def run(self, argv, check=True, timeout=120):
        if argv[:1] == ["ps"]:
            return SimpleNamespace(ok=True, stdout="\n".join(self.listing))
        assert s17.CODEX_CHILD_ROLLOUT in argv and argv[-1] == "S17E-x", argv
        read = self.reads[min(self.calls, len(self.reads) - 1)]
        self.calls += 1
        return SimpleNamespace(ok=True, stdout=read)


@pytest.mark.parametrize("survives", [True, False])
async def test_turn_aborted_waits_for_the_rollout_then_its_abort(monkeypatch, survives):
    """Parts 4 and 5: the sub-agent's rollout is found, then its `turn_aborted`; whether its
    command survives is noted, never asserted."""
    monkeypatch.setattr(s17.asyncio, "sleep", _nosleep)
    spawned = '{"payload":{"source":{"subagent":"thread_spawn"}}}'
    reads = ["", spawned, spawned, spawned + '\n{"type":"turn_aborted"}']
    line = "  102 python3 -c import time; time.sleep(240) S17E-x"
    vm, scenario = Rollouts(reads, [line] if survives else []), Scenario()
    await s17.turn_aborted(vm, scenario, "S17E-x", 0.0, "codex, no turn")
    assert vm.calls == 4
    assert scenario.timings == [("codex, no turn: the sub-agent's turn aborted", {"high": 30})]
    end = "survives !stop, as documented" if survives else "ended with it"
    assert scenario.notes == [f"codex, no turn: the sub-agent's command {end}"]


def test_the_loaded_counts_are_read_from_the_line():
    """Part 6 reads, from `CodexBackend._loaded_subagents`'s line, that a sub-agent came from
    the loaded threads (review of #861)."""
    assert s17.loaded_counts("Loaded threads read: 4, 1 sub-agents, 0.01 s") == (4, 1)
    assert s17.loaded_counts("Loaded threads read: 0, 0 sub-agents, 0.00 s, 1 failed") == (0, 0)
    assert s17.loaded_counts("") is None
