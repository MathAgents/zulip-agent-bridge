"""Claude mid-turn steering (`[backends.claude] steer`; PM on issue #36, Fri Oct 9).

The parser over the `steer_*` fixtures (recorded live with 2.1.283, `zab dev record-claude`),
`StreamInput`'s rule for closing stdin, and the backend end to end against `fake_claude.py`.
"""

from __future__ import annotations

import asyncio
import json
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


# ------------------------------------------------------------------- the backend, end to end


def integration(test):
    test = pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a shebang")(test)
    return pytest.mark.integration(test)


async def started(home, fixture, monkeypatch, *, steer=True, text="go"):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / f"{fixture}.jsonl"))
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
