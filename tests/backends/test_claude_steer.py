"""Claude mid-turn steering (`[backends.claude] steer`; PM on issue #36, Fri Oct 9).

The parser over the `steer_*` fixtures (recorded live with 2.1.283, `zab dev record-claude`),
`StreamInput`'s rule for closing stdin, and the backend end to end against `fake_claude.py`.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

from backends.claude_backend_helpers import FIXTURES, calls, drain, make_backend
from zab.backends import claude as cb
from zab.backends import claude_stream as cs
from zab.core.types import (
    ItemCompleted,
    ItemKind,
    ItemStarted,
    ItemStatus,
    SessionSpec,
    SteerRejected,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
    UserInput,
)


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def stdout(records: list[dict]) -> list[dict]:
    return [r["data"] for r in records if r["kind"] == "stdout"]


def results(name: str) -> list[dict]:
    return [o for o in stdout(load(name)) if o.get("type") == "result"]


def replay(name: str, *, multi_result: bool = True, upto: int | None = None, code: int = 0):
    objects = stdout(load(name))[:upto]
    sid = "11111111-2222-4333-8444-555555555555"
    parser = cs.StreamParser(sid, cb.new_turn_id(sid), multi_result=multi_result)
    events = [ev for obj in objects for ev in parser.feed(obj)]
    return parser, events + parser.finish(code, "")


def completions(events: list) -> list[TurnCompleted]:
    return [e for e in events if isinstance(e, TurnCompleted)]


# ---------------------------------------------------------------------------- the fixtures


def test_the_fixtures_show_when_a_steer_is_taken_in():
    """What 2.1.283 does with a second line (DECISIONS): its echo (`isReplay`, zab's uuid) comes
    when the model takes it in, and only a steer that misses a loop gets a further result."""
    for name, n in (("steer_tool", 1), ("steer_text", 2), ("steer_after_result", 2)):
        records = load(name)
        steer = [r for r in records if r["kind"] == "stdin"][1]
        echoes = [o["uuid"] for o in stdout(records) if o.get("isReplay")]
        assert steer["uuid"] in echoes and len(results(name)) == n
        assert steer["uuid"] in results(name)[-1]["user_message_uuids"]
    # during a tool call: written at 4.9 s, taken in after the call's result at 22.9 s
    records = load("steer_tool")
    steer_t = [r for r in records if r["kind"] == "stdin"][1]["t"]
    echo_t = [r["t"] for r in records if r["kind"] == "stdout" and r["data"].get("isReplay")][1]
    assert echo_t - steer_t > 15
    # a resume with a stopped task's notification: the held empty result, then one real result
    first, last = results("steer_resume")
    assert first["num_turns"] == 0 and not first["result"] and last["num_turns"] > 0


def test_a_steer_during_a_tool_call_is_answered_in_the_one_result():
    parser, events = replay("steer_tool")
    done = completions(events)
    assert len(done) == 1 and done[0].status is TurnStatus.COMPLETED
    assert "steered" in done[0].final_text
    # an echo of a line zab wrote is no event, and not "the last user text" of an error
    echo_obj = next(o for o in stdout(load("steer_tool")) if o.get("isReplay"))
    assert parser.feed({**echo_obj, "message": {"content": [{"type": "text", "text": "x"}]}}) == []
    assert parser._last_user_text is None


def steer_echoes(events: list) -> list[ItemCompleted]:
    return [
        e for e in events if isinstance(e, ItemCompleted) and e.item.kind is ItemKind.USER_MESSAGE
    ]


@pytest.mark.parametrize("name", ["steer_tool", "steer_text", "steer_after_result"])
def test_the_echo_of_a_steer_reports_that_claude_took_it_in(name):
    """#654: the parser reports a steer's echo, which comes when the model takes the steer in,
    as the steer's `ItemCompleted(USER_MESSAGE)`: the core marks its message read then. The
    prompt's echo is no event. During a tool call, the echo follows the call's result."""
    records = load(name)
    _prompt, steer = [r for r in records if r["kind"] == "stdin"]
    sid = "11111111-2222-4333-8444-555555555555"
    tid = cb.new_turn_id(sid)
    parser = cs.StreamParser(sid, tid, multi_result=True, steers={steer["uuid"]: "c2"})
    events = [ev for obj in stdout(records) for ev in parser.feed(obj)]
    (echo,) = steer_echoes(events)
    assert (echo.turn_id, echo.item.client_id, echo.item.text) == (tid, "c2", steer["text"])
    if name == "steer_tool":
        commands = [
            e for e in events if isinstance(e, ItemCompleted) and e.item.kind is ItemKind.COMMAND
        ]
        assert events.index(echo) == events.index(commands[0]) + 1  # just after `sleep 20` ends
    # without the steer's uuid (the prompt's, or an echo of another line) there is none
    plain = cs.StreamParser(sid, tid, multi_result=True)
    assert not steer_echoes([ev for obj in stdout(records) for ev in plain.feed(obj)])


@pytest.mark.parametrize("name", ["steer_tool", "steer_text", "steer_after_result"])
def test_a_steer_a_result_lists_without_its_echo_is_reported_once(name):
    """Review of #658: `StreamInput` counts a steer listed in a result's `user_message_uuids` as
    delivered even without its echo, so the parser reports it there, and only once (the test
    above has both, echo and listing)."""
    records = load(name)
    _prompt, steer = [r for r in records if r["kind"] == "stdin"]
    objects = [o for o in stdout(records) if not (o.get("isReplay") and o["uuid"] == steer["uuid"])]
    sid = "11111111-2222-4333-8444-555555555555"
    parser = cs.StreamParser(
        sid, cb.new_turn_id(sid), multi_result=True, steers={steer["uuid"]: "c2"}
    )
    events, at = [], None
    for obj in objects:
        events += parser.feed(obj)
        if at is None and steer_echoes(events):
            at = obj
    (echo,) = steer_echoes(events)
    assert echo.item.client_id == "c2" and echo.item.text is None
    assert at["type"] == "result" and steer["uuid"] in at["user_message_uuids"]


def test_a_running_command_is_in_progress_from_its_start():
    """#654: the status message times a running step from its `ItemStarted`, which the parser
    emits, `IN_PROGRESS`, at the `tool_use`: before the command starts (`task_started`)."""
    sid = "11111111-2222-4333-8444-555555555555"
    parser = cs.StreamParser(sid, cb.new_turn_id(sid))
    seen = {}  # of the first command, `sleep 20`
    for record in load("steer_tool"):
        if record["kind"] != "stdout":
            continue
        if record["data"].get("subtype") == "task_started":
            seen.setdefault("task", record["t"])
        for ev in parser.feed(record["data"]):
            if isinstance(ev, ItemStarted) and ev.item.kind is ItemKind.COMMAND:
                seen.setdefault("item", (record["t"], ev.item.status))
    assert seen["item"][1] is ItemStatus.IN_PROGRESS and seen["item"][0] < seen["task"]


@pytest.mark.parametrize("name", ["steer_text", "steer_after_result"])
def test_every_result_of_a_steered_turn_is_posted_in_order(name):
    """Item 3: a late steer's loop ends in its own result; the turn's text is both."""
    texts = [r["result"] for r in results(name)]
    parser, events = replay(name)
    done = completions(events)
    assert len(done) == 2  # one per result; the backend keeps the last
    assert done[0].final_text == texts[0]
    assert done[-1].final_text == texts[0] + "\n\n" + texts[1]
    out = sum(r["usage"]["output_tokens"] for r in results(name))
    assert done[-1].usage.output_tokens == out
    assert done[-1].usage.cost_usd == results(name)[-1]["total_cost_usd"]  # already a total
    assert parser.info.result == results(name)[-1]  # `modelUsage` adds up: for `!usage`
    # without multi_result (the flag off) a second result would stand alone: only the last text
    _parser, events = replay(name, multi_result=False)
    assert completions(events)[-1].final_text == texts[1]


def test_a_steered_resume_keeps_holding_the_empty_preamble_result():
    """#190 with stream-json input: the stopped task's empty `num_turns: 0` result still does
    not end the turn; the prompt and the steer are answered together."""
    answer = results("steer_resume")[-1]["result"]
    _parser, events = replay("steer_resume")
    done = completions(events)
    assert len(done) == 1 and done[0].final_text == answer and "steered" in answer


def test_a_further_loop_without_a_result_fails_the_turn_with_what_it_had():
    objects = stdout(load("steer_text"))
    second_text = max(i for i, o in enumerate(objects) if o.get("type") == "assistant")
    _parser, events = replay("steer_text", upto=second_text + 1, code=1)
    done = completions(events)
    assert done[-1].status is TurnStatus.FAILED
    first = results("steer_text")[0]["result"]
    assert done[-1].final_text.startswith(first + "\n\n# ")  # and the unfinished loop's text


def test_a_failed_further_loop_keeps_its_agent_text():
    """codex on #197: a second loop that wrote text and then failed (an interrupt, an API
    error) keeps that text after the first result's, as a failed single-result turn does."""
    objects = stdout(load("steer_text"))
    second_text = max(i for i, o in enumerate(objects) if o.get("type") == "assistant")
    sid = "11111111-2222-4333-8444-555555555555"
    parser = cs.StreamParser(sid, cb.new_turn_id(sid), multi_result=True)
    for obj in objects[: second_text + 1]:
        parser.feed(obj)
    failed = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "num_turns": 1,
        "result": "API Error: overloaded",
    }
    done = completions(parser.feed(failed) + parser.finish(0, ""))
    assert len(done) == 1 and done[0].status is TurnStatus.FAILED
    first = results("steer_text")[0]["result"]
    loop_text = next(
        b["text"] for b in objects[second_text]["message"]["content"] if b["type"] == "text"
    )
    assert done[0].final_text == first + "\n\n" + loop_text
    assert "overloaded" not in done[0].final_text and "overloaded" in done[0].error.message


# ------------------------------------------------------------------------------ StreamInput


class Writer:
    def __init__(self):
        self.lines: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        self.lines.append(json.loads(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def echo(uid: str) -> dict:
    return {"type": "user", "isReplay": True, "uuid": uid, "message": {"content": "x"}}


RESULT = {"type": "result", "subtype": "success", "num_turns": 1}
INIT = {"type": "system", "subtype": "init"}


async def test_stdin_closes_at_the_first_result_with_everything_taken_in():
    stream, writer = cb.StreamInput("go"), Writer()
    line = json.loads(stream.first_line())
    assert line["type"] == "user" and line["message"] == {"role": "user", "content": "go"}
    assert line["uuid"] == stream.prompt_uuid
    with pytest.raises(SteerRejected):  # before the prompt is written
        await stream.send("too early")
    stream.attach(writer)
    stream.observe(echo(stream.prompt_uuid))
    uid = await stream.send("also this")
    assert writer.lines[-1]["uuid"] == uid and writer.lines[-1]["message"]["content"] == "also this"
    stream.observe(echo(uid))
    stream.observe(RESULT)
    assert stream.closed and writer.closed and not stream.undelivered
    with pytest.raises(SteerRejected):
        await stream.send("after the end")


async def test_stdin_stays_open_for_a_steer_that_missed_the_result():
    stream, writer = cb.StreamInput("go"), Writer()
    stream.attach(writer)
    stream.observe(echo(stream.prompt_uuid))
    uid = await stream.send("late")
    stream.observe(RESULT)  # written, not taken in: a further loop follows
    assert stream.open
    stream.observe(INIT)
    await stream.send("later still")  # still accepted
    stream.observe({**RESULT, "user_message_uuids": [uid]})
    assert stream.open  # the second steer has not been taken in
    stream.observe(echo(stream.steers[-1]))
    stream.observe(RESULT)
    assert stream.closed


async def test_the_empty_preamble_result_does_not_close_stdin():
    stream, writer = cb.StreamInput("go"), Writer()
    stream.attach(writer)
    stream.observe({**RESULT, "num_turns": 0})  # before the prompt is taken in
    assert stream.open
    stream.observe(echo(stream.prompt_uuid))
    stream.observe(RESULT)
    assert stream.closed


async def test_stdin_closes_when_no_further_loop_comes():
    stream, writer = cb.StreamInput("go", idle_close_s=0.05), Writer()
    stream.attach(writer)
    stream.observe(echo(stream.prompt_uuid))
    await stream.send("never echoed")
    stream.observe(RESULT)
    assert stream.open
    await asyncio.sleep(0.2)
    assert stream.closed and writer.closed and len(stream.undelivered) == 1


async def test_a_further_loop_disarms_the_idle_close():
    stream, writer = cb.StreamInput("go", idle_close_s=0.05), Writer()
    stream.attach(writer)
    stream.observe(echo(stream.prompt_uuid))
    await stream.send("late")
    stream.observe(RESULT)
    stream.observe(INIT)
    await asyncio.sleep(0.2)
    assert stream.open


def tasks(*entries: tuple[str, str], **extra) -> dict:
    """A `background_tasks_changed`: every live background task, (id, type) (2.1.283's schema)."""
    listed = [{"task_id": i, "task_type": kind, "description": "d", **extra} for i, kind in entries]
    return {"type": "system", "subtype": "background_tasks_changed", "tasks": listed}


async def test_stdin_stays_open_while_a_background_subagent_runs():
    """#660: the CLI waits for a background sub-agent after the `result` of the loop that started
    it, and its notification starts a further loop; a message meanwhile gets a loop of its own.
    stdin used to close at that first `result`, so the message was held until the end."""
    stream, writer = cb.StreamInput("go", idle_close_s=0.05), Writer()
    stream.attach(writer)
    stream.observe(echo(stream.prompt_uuid))
    stream.observe(tasks(("a1", "local_agent")))
    stream.observe(RESULT)  # everything taken in, but the sub-agent runs on
    assert stream.open and stream.agents == {"a1"}
    await asyncio.sleep(0.2)  # the short idle close is not the sub-agent's
    assert stream.open
    uid = await stream.send("what's the status?")
    stream.observe(INIT)
    stream.observe(echo(uid))
    stream.observe(RESULT)  # the steer's own loop
    assert stream.open
    stream.observe(tasks())  # the sub-agent is done; its notification's loop follows
    stream.observe({"type": "system", "subtype": "task_notification", "task_id": "a1"})
    stream.observe(INIT)
    await asyncio.sleep(0.2)
    assert stream.open
    stream.observe(RESULT)
    assert stream.closed and writer.closed and not stream.undelivered


async def test_a_background_command_or_an_ambient_task_keeps_nothing_open():
    """`-p` stops a background `local_bash` command about 5 s after stdin closes (#36): only a
    sub-agent, which the CLI waits for, keeps stdin open. An `ambient` task is no activity."""
    for listed in (tasks(("b1", "local_bash")), tasks(("a1", "local_agent"), ambient=True)):
        stream, writer = cb.StreamInput("go"), Writer()
        stream.attach(writer)
        stream.observe(echo(stream.prompt_uuid))
        stream.observe(listed)
        stream.observe(RESULT)
        assert stream.closed


async def test_stdin_closes_when_the_subagent_outlives_its_wait():
    """A sub-agent that never ends, the model idle: stdin closes `subagent_idle_close_s` after
    the `result` (by default `SUBAGENT_IDLE_CLOSE_S`), and the CLI's own wait for it applies."""
    stream = cb.StreamInput("go", idle_close_s=0.05, subagent_idle_close_s=0.3)
    stream.attach(Writer())
    assert cb.StreamInput("x").subagent_idle_close_s == cb.SUBAGENT_IDLE_CLOSE_S == 600.0
    stream.observe(echo(stream.prompt_uuid))
    stream.observe(tasks(("a1", "local_agent")))
    stream.observe(RESULT)
    await asyncio.sleep(0.15)
    assert stream.open
    await asyncio.sleep(0.4)
    assert stream.closed and stream.agents == {"a1"}
    with pytest.raises(SteerRejected):
        await stream.send("held for the next turn")


async def test_task_list_updates_never_lengthen_the_subagents_wait():
    """Codex on #814: the wait runs from the last `result`. Updates about other tasks (a
    background command of the sub-agent's, an ambient watcher) used to restart it each time, so
    a stuck sub-agent could hold stdin open for ever, and the CLI's own wait never began. The
    last sub-agent's end shortens it to `idle_close_s`, and never lengthens it either."""
    stream = cb.StreamInput("go", idle_close_s=5.0, subagent_idle_close_s=1.0)
    stream.attach(Writer())
    stream.observe(echo(stream.prompt_uuid))
    stream.observe(tasks(("a1", "local_agent")))
    stream.observe(RESULT)
    for listed in (("b1", "local_bash"), ("m1", "monitor"), ("b2", "local_bash")):
        await asyncio.sleep(0.3)  # at 0.3, 0.6 and 0.9 s, before the 1 s deadline
        stream.observe(tasks(("a1", "local_agent"), listed))
    assert stream.open
    await asyncio.sleep(0.4)  # 1.3 s after the `result`
    assert stream.closed  # restarted at 0.9 s, it would close at 1.9 s
    # the sub-agent's end 0.2 s before a 0.5 s deadline: no new 5 s wait
    stream = cb.StreamInput("go", idle_close_s=5.0, subagent_idle_close_s=0.5)
    stream.attach(Writer())
    stream.observe(echo(stream.prompt_uuid))
    stream.observe(tasks(("a1", "local_agent")))
    stream.observe(RESULT)
    await asyncio.sleep(0.3)
    stream.observe(tasks())
    await asyncio.sleep(0.5)
    assert stream.closed


async def test_stdin_closes_when_no_loop_follows_the_subagents_end():
    """A sub-agent gone after the `result` (stopped, say) with no notification's loop after it:
    stdin closes `idle_close_s` later, as for a line no loop takes in."""
    stream = cb.StreamInput("go", idle_close_s=0.05, subagent_idle_close_s=60)
    stream.attach(Writer())
    stream.observe(echo(stream.prompt_uuid))
    stream.observe(tasks(("a1", "local_agent")))
    stream.observe(RESULT)
    stream.observe(tasks())
    assert stream.open
    await asyncio.sleep(0.2)
    assert stream.closed


def test_a_running_subagent_adds_no_background_note():
    """The note that `-p` stopped a background job at the turn's end (#36) is not about a
    sub-agent, which the CLI waits for: the turn's last `result` comes after it (#660)."""
    sid = "11111111-2222-4333-8444-555555555555"
    parser = cs.StreamParser(sid, cb.new_turn_id(sid), multi_result=True)
    text = {
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "text", "text": "ok"}]},
    }
    stream = [
        {"type": "system", "subtype": "init", "session_id": sid, "model": "m"},
        tasks(("a1", "local_agent")),
        text,
        {**RESULT, "result": "Started it."},
    ]
    (done,) = completions([e for obj in stream for e in parser.feed(obj)])
    assert done.final_text == "Started it."
    parser.feed(tasks(("a1", "local_agent"), ("b1", "local_bash")))
    (done,) = completions(parser.feed({**RESULT, "result": "And a command."}))
    assert done.final_text.endswith(cs.BACKGROUND_NOTE)  # the command's, as before


# ------------------------------------------------------------------- the backend, end to end


def integration(test):
    test = pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a shebang")(test)
    return pytest.mark.integration(test)


async def started(home, fixture, monkeypatch, *, steer=True, text="go"):
    path = fixture if isinstance(fixture, pathlib.Path) else FIXTURES / f"{fixture}.jsonl"
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(path))
    backend = make_backend(home, steer=steer)
    await backend.start()
    await backend._events.get()
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    tid = await backend.start_turn(sid, UserInput(text=text, client_id="c1"))
    return backend, sid, tid


async def steer_when_ready(backend, sid, tid, text):
    """The core steers whenever a message arrives; here, as soon as the prompt is written."""
    async with asyncio.timeout(10):
        while True:
            stream = backend._sessions[sid].stdin
            if stream is not None and stream.open:
                return await backend.steer(sid, tid, UserInput(text=text, client_id="c2"))
            await asyncio.sleep(0.01)


@integration
async def test_flag_off_runs_the_turn_exactly_as_before(home, monkeypatch):
    backend, sid, tid = await started(home, "simple", monkeypatch, steer=False, text="ping?")
    assert not backend.capabilities.steer
    with pytest.raises(SteerRejected, match="cannot take input"):
        await backend.steer(sid, tid, UserInput(text="x", client_id="c2"))
    await drain(backend)
    call = calls(home)[0]
    assert "--input-format" not in call["argv"] and "--replay-user-messages" not in call["argv"]
    assert call["stdin"] == "ping?" and "stdin_line" not in call
    assert backend._sessions[sid].stdin is None
    await backend.close()


@integration
async def test_a_steer_during_a_tool_call_reaches_the_turn(home, monkeypatch):
    backend, sid, tid = await started(home, "steer_tool", monkeypatch, text="sleep 20")
    assert backend.capabilities.steer
    async for ev in backend.events():  # the core steers when a message arrives mid-turn
        if isinstance(ev, ItemStarted):
            break
    await backend.steer(sid, tid, UserInput(text="also echo steered", client_id="c2"))
    events = await drain(backend, extra=1)
    done = completions(events)
    assert len(done) == 1 and done[0].status is TurnStatus.COMPLETED
    assert "steered" in done[0].final_text
    log = [json.loads(line) for line in home["log"].read_text().splitlines()]
    call = log[0]
    i = call["argv"].index("--input-format")
    assert call["argv"][i + 1] == "stream-json" and "--replay-user-messages" in call["argv"]
    assert call["stdin_line"]["message"] == {"role": "user", "content": "sleep 20"}
    steers = [entry["steer"] for entry in log if "steer" in entry]
    assert [s["message"]["content"] for s in steers] == ["also echo steered"]
    assert backend._sessions[sid].stdin is None  # gone with the turn
    with pytest.raises(SteerRejected):
        await backend.steer(sid, tid, UserInput(text="late", client_id="c3"))
    await backend.close()


@integration
async def test_the_backend_reports_the_steer_claude_took_in(home, monkeypatch):
    """#654: `steer()` keeps the input's client_id with the line's uuid, and the CLI's echo of
    that uuid comes out as the steer's `ItemCompleted(USER_MESSAGE)`, after the tool call."""
    backend, sid, tid = await started(home, "steer_tool", monkeypatch, text="sleep 20")
    async for ev in backend.events():
        if isinstance(ev, ItemStarted):
            break
    await backend.steer(sid, tid, UserInput(text="also echo steered", client_id="c2"))
    events = await drain(backend, extra=1)
    (echo,) = steer_echoes(events)
    assert (echo.session_id, echo.turn_id, echo.item.client_id) == (sid, tid, "c2")
    ended = next(
        e for e in events if isinstance(e, ItemCompleted) and e.item.kind is ItemKind.COMMAND
    )
    assert events.index(ended) < events.index(echo) < events.index(completions(events)[0])
    await backend.close()


@integration
async def test_a_steer_while_the_answer_is_written_adds_a_second_answer(home, monkeypatch):
    backend, sid, tid = await started(home, "steer_text", monkeypatch)
    await steer_when_ready(backend, sid, tid, "Also add a one-line title at the top.")
    done = completions(await drain(backend, extra=1))
    texts = [r["result"] for r in results("steer_text")]
    assert len(done) == 1 and done[0].final_text == texts[0] + "\n\n" + texts[1]
    await backend.close()


@integration
async def test_stdin_closes_at_the_result_and_a_later_steer_is_held(home, monkeypatch):
    """(c): the backend closes stdin at the first result (everything taken in), so the fake,
    waiting where the recording wrote its steer, sees EOF and exits like the real CLI; a steer
    from then on is rejected and the core holds it for the next turn."""
    backend, sid, tid = await started(home, "steer_after_result", monkeypatch)
    done = completions(await drain(backend, extra=1))
    assert len(done) == 1 and done[0].final_text == "pong"
    assert not [entry for entry in calls(home) if "steer" in entry]
    with pytest.raises(SteerRejected):
        await backend.steer(sid, tid, UserInput(text="ping", client_id="c2"))
    await backend.close()


@integration
async def test_a_steered_resume_after_a_stopped_task_answers(home, monkeypatch):
    backend, sid, tid = await started(home, "steer_resume", monkeypatch, text="Summarise.")
    await steer_when_ready(backend, sid, tid, "Also echo steered.")
    done = completions(await drain(backend, extra=1))
    assert len(done) == 1 and done[0].status is TurnStatus.COMPLETED
    assert done[0].final_text == results("steer_resume")[-1]["result"]
    await backend.close()


@integration
async def test_a_steer_that_never_reaches_claude_is_reported(home, monkeypatch):
    """A steer written before a result but never taken in: stdin closes after the idle wait
    and the answer says the message did not arrive (the core had already cleared it)."""
    monkeypatch.setattr(cb, "STEER_IDLE_CLOSE_S", 0.2)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_REALTIME", "1")  # the steer lands before the result
    backend, sid, tid = await started(home, "simple", monkeypatch)
    await steer_when_ready(backend, sid, tid, "and another thing")
    done = completions(await asyncio.wait_for(drain(backend, extra=1), 30))
    assert done[0].status is TurnStatus.COMPLETED
    assert done[0].final_text.startswith("pong")
    assert done[0].final_text.endswith(
        "\n\n⚠️ **Bridge** (not Claude): A message sent during this turn did not reach Claude; "
        "send it again."
    )
    await backend.close()


@integration
async def test_a_stop_with_stdin_open_does_not_hang(home, monkeypatch):
    """Item 4: a stop closes stdin first, then the §9.6 group signals."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_HANG", "1")
    backend, sid, tid = await started(home, "steer_tool", monkeypatch)
    await steer_when_ready(backend, sid, tid, "also this")
    await backend.interrupt(sid)
    with pytest.raises(SteerRejected):
        await backend.steer(sid, tid, UserInput(text="after the stop", client_id="c3"))
    done = completions(await asyncio.wait_for(drain(backend, extra=1), 30))
    assert done[0].status is TurnStatus.INTERRUPTED
    await backend.close()


@integration
async def test_a_steer_before_the_prompt_is_written_is_held(home, monkeypatch):
    """Before the process has the prompt there is nothing to steer: `SteerRejected`, and the
    core holds the message (§7.3). A wrong turn id is rejected too."""
    backend, sid, tid = await started(home, "steer_text", monkeypatch)
    with pytest.raises(SteerRejected):
        await backend.steer(sid, tid, UserInput(text="too early", client_id="c2"))
    first = await asyncio.wait_for(backend._events.get(), 5)
    assert isinstance(first, TurnStarted)
    await steer_when_ready(backend, sid, tid, "Also add a one-line title at the top.")
    with pytest.raises(SteerRejected):
        await backend.steer(sid, "another-turn", UserInput(text="x", client_id="c3"))
    await drain(backend, extra=1)
    await backend.close()


# ---------------------------------------------------- a background sub-agent (#660), end to end

STARTED = "I started a sub-agent in the background."
STATUS = "Status: the sub-agent is still running."
FINISHED = "The sub-agent finished; it replied: done."


def subagent_fixture(path: pathlib.Path, *, steer: bool = False, never_ends: bool = False):
    """A turn whose background sub-agent outlives its first `result`, in 2.1.283's shapes (the
    2026-10-25 rows of DECISIONS and the CLI's schemas; no recording yet: `zab dev record-claude
    --scenario subagent_background` makes one). With `steer`, the fake waits after that `result`
    for a line, which gets a loop of its own; `never_ends` stops the replay there for a signal.
    Otherwise the sub-agent ends, and its notification starts the last loop."""
    agent = {"task_id": "a1", "task_type": "local_agent", "description": "sleep, then done"}
    call = {
        "type": "tool_use",
        "id": "tu1",
        "name": "Agent",
        "input": {"description": "sleep, then done", "prompt": "…", "run_in_background": True},
    }
    launched = {
        "type": "tool_result",
        "tool_use_id": "tu1",
        "content": "Async agent launched successfully. You will be notified when it completes.",
    }
    ended = {"type": "system", "subtype": "task_notification", "status": "completed", **agent}

    def out(t, data, **extra):
        return {"kind": "stdout", "t": t, "data": {"session_id": "", **data}, **extra}

    def result(t, text, num_turns, uid, **extra):
        data = {**RESULT, "num_turns": num_turns, "result": text, "user_message_uuids": [uid]}
        return out(t, {**data, "usage": {"input_tokens": 1, "output_tokens": 1}}, **extra)

    def said(t, mid, text, **extra):
        message = {"id": mid, "content": [{"type": "text", "text": text}]}
        return out(
            t, {"type": "assistant", "message": message, "parent_tool_use_id": None}, **extra
        )

    records = [
        {"kind": "meta", "fixture": 1, "version": "2.1.283", "scenario": "subagent (synthetic)"},
        {"kind": "stdin", "t": 0.0, "uuid": "p", "text": "go"},
        out(0.5, INIT),
        out(1.0, {**echo("p"), "parent_tool_use_id": None}),
        out(2.0, {"type": "assistant", "message": {"id": "m1", "content": [call]}}),
        out(2.1, tasks(("a1", "local_agent"))),
        out(2.1, {"type": "system", "subtype": "task_started", "tool_use_id": "tu1", **agent}),
        out(2.2, {"type": "user", "message": {"content": [launched]}}),
        out(2.5, {"type": "assistant", "parent_tool_use_id": "tu1", "message": {"content": []}}),
        said(3.0, "m2", STARTED),
        result(3.9, STARTED, 2, "p"),
    ]
    if steer:
        records += [
            {"kind": "stdin", "t": 30.0, "uuid": "s", "text": "status?", "optional": True},
            out(30.1, INIT, **{"for": "s"}),
            out(30.2, {**echo("s"), "parent_tool_use_id": None}, **{"for": "s"}),
            said(31.0, "m3", STATUS, **{"for": "s"}),
            result(31.1, STATUS, 1, "s", **{"for": "s"}),
        ]
    if never_ends:
        records.append({"kind": "hang"})
    records += [
        out(97.0, tasks()),
        out(97.0, ended),
        out(97.1, INIT),
        out(97.2, {"type": "user", "message": {"role": "user", "content": "<task-notification>"}}),
        said(98.1, "m4", FINISHED),
        result(98.2, FINISHED, 1, "p"),
        {"kind": "exit", "t": 98.2, "code": 0},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


async def after_first_result(backend, sid):
    """Once the backend has read the first `result`: stdin kept open after it, or closed there
    (as it was before #660, whose `StreamInput` has no `_after_result`)."""
    async with asyncio.timeout(10):
        while True:
            stream = backend._sessions[sid].stdin
            if stream is not None and (stream.closed or getattr(stream, "_after_result", False)):
                return stream
            await asyncio.sleep(0.01)


@integration
async def test_a_steer_while_a_background_subagent_runs_gets_its_own_loop(
    home, monkeypatch, tmp_path
):
    """#660, case 1: the parent ended its loop while its sub-agent runs. stdin used to close at
    that first `result`, so the message was refused and held (⏳) until the sub-agent was done.
    Now it is written, taken in by a loop of its own, and answered before the sub-agent's end."""
    fixture = subagent_fixture(tmp_path / "subagent.jsonl", steer=True)
    backend, sid, tid = await started(home, fixture, monkeypatch)
    stream = await after_first_result(backend, sid)
    assert stream.open
    await backend.steer(sid, tid, UserInput(text="what's the status?", client_id="c2"))
    events = await asyncio.wait_for(drain(backend, extra=1), 30)
    (done,) = completions(events)
    assert done.status is TurnStatus.COMPLETED
    assert done.final_text == "\n\n".join((STARTED, STATUS, FINISHED))
    (taken,) = steer_echoes(events)
    assert taken.item.client_id == "c2"
    steers = [entry["steer"] for entry in calls(home) if "steer" in entry]
    assert [s["message"]["content"] for s in steers] == ["what's the status?"]
    assert backend._sessions[sid].stdin is None
    await backend.close()


async def test_the_quick_subagent_recording_keeps_stdin_open_until_its_last_loop():
    """`subagent_background_quick`, recorded live with 2.1.283 and #660's `StreamInput`: the CLI
    lists the `local_agent` before the first `result`; it leaves the list before its
    `task_notification`; and its notification starts a further loop by itself, stdin open
    (the assumption of DECISIONS' 2026-11-05 row, now seen). stdin closes at that loop's
    `result`, though the sub-agent's own background `local_bash` still runs: `-p` stops it."""
    objects = stdout(load("subagent_background_quick"))
    kinds = [(o.get("type"), o.get("subtype")) for o in objects]
    first, last = [i for i, k in enumerate(kinds) if k[0] == "result"]
    listings = [
        (i, {task["task_type"] for task in o["tasks"]})
        for i, o in enumerate(objects)
        if o.get("subtype") == "background_tasks_changed"
    ]
    assert any(i < first and "local_agent" in types for i, types in listings)
    gone = next(i for i, types in listings if i > first and "local_agent" not in types)
    notified = next(i for i, k in enumerate(kinds) if k == ("system", "task_notification"))
    loop = next(i for i, k in enumerate(kinds) if i > first and k == ("system", "init"))
    assert first < gone < notified < loop < last
    prompt = load("subagent_background_quick")[1]  # the `stdin` record of the prompt
    stream, writer = cb.StreamInput(prompt["text"]), Writer()
    stream.pending = {prompt["uuid"]}  # the recorded uuid, which the CLI echoes
    stream.attach(writer)
    seen = []
    for i, obj in enumerate(objects):
        stream.observe(obj)
        if i in (first, loop, last):
            seen.append(stream.open)
    assert seen == [True, True, False] and writer.closed
    assert "local_bash" in [types for i, types in listings if i < last][-1]  # still running


@integration
async def test_the_quick_subagent_recording_end_to_end(home, monkeypatch):
    """The recording replayed through the backend: both results, in order, and the note that
    a background job (the sub-agent's own `local_bash`, stopped 5 s after stdin closed) was
    stopped."""
    backend, _sid, _tid = await started(home, "subagent_background_quick", monkeypatch)
    (done,) = completions(await asyncio.wait_for(drain(backend, extra=1), 30))
    texts = [r["result"] for r in results("subagent_background_quick")]
    assert done.status is TurnStatus.COMPLETED
    assert done.final_text == "\n\n".join([*texts, cs.BACKGROUND_NOTE])
    await backend.close()


@integration
async def test_a_background_subagent_turn_ends_after_its_notification(home, monkeypatch, tmp_path):
    """No message meanwhile: stdin closes at the `result` of the sub-agent's notification loop,
    and the turn posts both results, without the note of a stopped background job."""
    fixture = subagent_fixture(tmp_path / "subagent.jsonl")
    backend, _sid, _tid = await started(home, fixture, monkeypatch)
    (done,) = completions(await asyncio.wait_for(drain(backend, extra=1), 30))
    assert done.status is TurnStatus.COMPLETED
    assert done.final_text == STARTED + "\n\n" + FINISHED
    await backend.close()


@integration
async def test_a_stop_while_a_subagent_never_ends_interrupts_the_turn(home, monkeypatch, tmp_path):
    """A sub-agent that never ends: `!stop` closes stdin and signals the turn's process group,
    which holds the sub-agent too (DECISIONS, 2026-10-26), and the turn is `INTERRUPTED`, not
    completed with the first `result`'s text and a note that a background job was stopped."""
    fixture = subagent_fixture(tmp_path / "subagent.jsonl", never_ends=True)
    backend, sid, tid = await started(home, fixture, monkeypatch)
    await after_first_result(backend, sid)
    turn = backend._sessions[sid].turn
    await backend.interrupt(sid)
    with pytest.raises(SteerRejected):
        await backend.steer(sid, tid, UserInput(text="after the stop", client_id="c3"))
    (done,) = completions(await asyncio.wait_for(drain(backend, extra=1), 30))
    assert done.status is TurnStatus.INTERRUPTED
    assert done.final_text == STARTED
    assert not turn.group_alive()
    await backend.close()


@integration
async def test_stdin_closes_after_the_subagent_wait_and_the_cli_goes_on(
    home, monkeypatch, tmp_path
):
    """The sub-agent's wait with the model idle ends (`SUBAGENT_IDLE_CLOSE_S`, shortened here):
    stdin closes, a later message is held for the next turn, and the CLI still runs the
    sub-agent's notification loop and its `result` (the fake skips the line it never got)."""
    monkeypatch.setattr(cb, "SUBAGENT_IDLE_CLOSE_S", 0.3)
    fixture = subagent_fixture(tmp_path / "subagent.jsonl", steer=True)
    backend, sid, tid = await started(home, fixture, monkeypatch)
    stream = await after_first_result(backend, sid)
    assert stream.open
    await asyncio.sleep(1.0)  # past the wait: closed by its timer
    assert stream.closed
    with pytest.raises(SteerRejected):
        await backend.steer(sid, tid, UserInput(text="too late", client_id="c2"))
    (done,) = completions(await asyncio.wait_for(drain(backend, extra=1), 30))
    assert done.status is TurnStatus.COMPLETED
    assert done.final_text == STARTED + "\n\n" + FINISHED
    assert not [entry for entry in calls(home) if "steer" in entry]
    await backend.close()
