"""A turn that loses Claude Code's OAuth refresh race (SPEC §9.4, §7.9; release 0.2.3).

When the access token has expired and another `claude` process holds the refresh lock, or
exited holding it, the CLI fails the turn with an API error it calls transient. The texts below
are the CLI's own (2.1.283 bundle, DECISIONS). The account is still signed in: the backend
retries the turn and, if every try loses, reports a transient error, never AUTH (`!login`).
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
    ErrorKind,
    ItemCompleted,
    ItemKind,
    ItemStarted,
    SessionSpec,
    SteerRejected,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
    UsageUpdated,
    UserInput,
)
from zab.testing import fake_claude as fc

AGENT_MESSAGE = ItemKind.AGENT_MESSAGE
RACE_INTERACTIVE = (
    "Could not refresh your login because another Claude Code process is refreshing it (or "
    "exited mid-refresh) · Try again in a minute; if it keeps happening, close other Claude "
    "Code windows or sign in again with /login"
)
RACE_INTERNAL = (
    "OAuth access token could not be refreshed: another Claude Code process is holding the "
    "refresh lock"
)


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def upto(records: list[dict], match) -> list[dict]:
    """`records` up to and including the first stdout object `match` accepts."""
    i = next(i for i, r in enumerate(records) if r["kind"] == "stdout" and match(r["data"]))
    return records[: i + 1]


def until_init(name: str = "simple") -> list[dict]:
    return upto(load(name), lambda d: d.get("subtype") == "init")


def until_tool_result(*, text: bool = True) -> list[dict]:
    """The `tool_use` fixture up to its first tool result; without `text`, the agent's text
    blocks are dropped, so that only the tool call itself is output."""

    def tool_result(d: dict) -> bool:
        content = (d.get("message") or {}).get("content")
        return d.get("type") == "user" and isinstance(content, list)

    def has_text(r: dict) -> bool:
        data = r.get("data") or {}
        content = (data.get("message") or {}).get("content")
        return data.get("type") == "assistant" and any(
            isinstance(b, dict) and b.get("type") == "text" for b in content or []
        )

    records = upto(load("tool_use"), tool_result)
    return records if text else [r for r in records if not has_text(r)]


def stdout(records: list[dict]) -> list[dict]:
    return [r["data"] for r in records if r["kind"] == "stdout"]


def replay(records: list[dict]) -> tuple[cs.StreamParser, list]:
    parser = cs.StreamParser("s", "s:t")
    return parser, [ev for obj in stdout(records) for ev in parser.feed(obj)]


# ------------------------------------------------------------------------- classification


@pytest.mark.contract
@pytest.mark.parametrize(
    ("code", "text", "stderr"),
    [
        ("server_error", fc.REFRESH_RACE_TEXT, ""),  # `-p`, as the test VM's transcript showed
        (None, RACE_INTERACTIVE, ""),
        (None, "", RACE_INTERNAL),
    ],
)
def test_the_refresh_race_is_transient_not_auth(code, text, stderr):
    info = cs.classify_error(code, text, stderr)
    assert info.kind is ErrorKind.BACKEND_UNAVAILABLE
    assert info.message == cs.REFRESH_RACE_MESSAGE and "login" not in info.message
    assert info.retry_after_s == 60.0


@pytest.mark.contract
@pytest.mark.parametrize(
    ("code", "text"),
    [  # the CLI's sign-out texts in `-p` mode (2.1.283)
        (None, "Not logged in · Please run /login"),
        (None, "OAuth token revoked · Please run /login"),
        (None, "Login expired · Please run /login"),
        (
            "authentication_failed",
            "Failed to authenticate: OAuth session expired and could not be refreshed",
        ),
        (
            "authentication_failed",
            "Failed to authenticate: OAuth token revoked. Please log in again or contact your "
            "administrator.",
        ),
        ("authentication_failed", "Authentication required · Sign in again to continue"),
    ],
)
def test_a_real_sign_out_is_still_auth(code, text):
    assert not cs.is_refresh_race(text)
    assert cs.classify_error(code, text, "").kind is ErrorKind.AUTH


# --------------------------------------------------------------------------------- parser


@pytest.mark.contract
def test_the_parser_reports_a_lost_race_before_any_output():
    parser, events = replay([*until_init(), *fc.refresh_race_records(1.0)])
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.FAILED
    assert done[0].error is not None and done[0].error.kind is ErrorKind.BACKEND_UNAVAILABLE
    assert done[0].error.message == cs.REFRESH_RACE_MESSAGE and done[0].final_text is None
    assert parser.info.refresh_race and not parser.output_seen
    assert not any(isinstance(e, ItemStarted | ItemCompleted) for e in events)


@pytest.mark.contract
@pytest.mark.parametrize("text", [True, False], ids=["after-text", "tool-only"])
def test_a_race_after_a_tool_call_counts_as_output(text):
    head = until_tool_result(text=text)
    parser, events = replay([*head, *fc.refresh_race_records(9.0)])
    assert any(isinstance(e, ItemStarted) for e in events)
    said = [e for e in events if isinstance(e, ItemCompleted) and e.item.kind is AGENT_MESSAGE]
    assert bool(said) is text
    assert parser.info.refresh_race and parser.output_seen


@pytest.mark.contract
def test_a_race_with_an_empty_result_text_is_read_from_the_api_error_message():
    """`_result` reads the race from the API-error message too, as `finish` does."""
    tail = fc.refresh_race_records(1.0)
    tail[1]["data"] = {**tail[1]["data"], "result": ""}
    parser, events = replay([*until_init(), *tail])
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.error is not None and done.error.kind is ErrorKind.BACKEND_UNAVAILABLE
    assert done.error.message == cs.REFRESH_RACE_MESSAGE
    assert parser.info.refresh_race and not parser.output_seen


@pytest.mark.contract
def test_a_race_reported_only_on_stderr_is_still_transient():
    parser, events = replay(until_init())
    events = parser.finish(1, RACE_INTERNAL)
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.error is not None
    assert done.error.kind is ErrorKind.BACKEND_UNAVAILABLE
    assert done.error.message == cs.REFRESH_RACE_MESSAGE
    assert parser.info.refresh_race and not parser.output_seen


# ---------------------------------------------------------------------- retry schedule (#450)


@pytest.mark.contract
def test_the_first_retry_is_fast():
    for _ in range(200):
        delay = cb.refresh_retry_delay(0, 0.0)
        assert 2.0 <= cb.REFRESH_RETRY_DELAY_S <= delay
        assert delay <= cb.REFRESH_RETRY_DELAY_S + cb.REFRESH_RETRY_JITTER_S <= 5.0


@pytest.mark.contract
def test_the_last_try_starts_after_the_lock_that_beat_the_first_one_is_stale():
    """The CLI reports a lost race `CLI_REFRESH_LOCK_WAIT_S` or more after it first found the
    lock held, untouched since, so that lock is stale `STALE - WAIT` seconds after the first
    loss at the latest. The last try starts later than that, however long the tries between
    took (each loses after the CLI's own wait of 7.5 to 10 s)."""
    stale_by = cb.CLI_REFRESH_LOCK_STALE_S - cb.CLI_REFRESH_LOCK_WAIT_S
    assert cb.REFRESH_LAST_TRY_S >= stale_by + 1.0
    assert cb.REFRESH_RETRIES == 2
    jitter = cb.REFRESH_RETRY_JITTER_S
    for since in (0.0, 2.0 + 7.5, 5.0 + 10.0, 30.0, stale_by):
        for _ in range(50):
            start = since + cb.refresh_retry_delay(1, since)
            assert cb.REFRESH_LAST_TRY_S <= start
            assert start <= max(cb.REFRESH_LAST_TRY_S, since + cb.REFRESH_RETRY_DELAY_S) + jitter
    late = cb.refresh_retry_delay(1, 120.0)  # long past the staleness: no longer than a retry
    assert late <= cb.REFRESH_RETRY_DELAY_S + cb.REFRESH_RETRY_JITTER_S


@pytest.mark.contract
def test_the_issue_450_log_under_the_new_schedule():
    """#450's journal: try 1 lost after 10.5 s, try 2 lost after about 8 s; say the lock was
    left 2 s before try 1 started (`auth status` killed). It went stale 60 s after that; the old
    tries 27 s and 21 s apart won at the third try only by chance. Now the last try starts after
    the staleness always, and the fast retry costs one more short try at most."""
    left_at, first_loss = -2.0, 10.5
    stale_at = left_at + cb.CLI_REFRESH_LOCK_STALE_S
    for _ in range(50):
        second = first_loss + cb.refresh_retry_delay(0, 0.0)
        second_loss = second + 7.9  # the CLI's own wait, as in the log
        third = second_loss + cb.refresh_retry_delay(1, second_loss - first_loss)
        assert second < stale_at < third <= first_loss + 60.0


# -------------------------------------------------------------------------------- backend


def integration(test):
    test = pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a shebang")(test)
    return pytest.mark.integration(test)


@pytest.fixture
def races(monkeypatch):
    """`races(n)`: the next n `claude -p` runs lose the race; retries wait `delay` seconds."""

    def set_races(n: int, delay: float = 0.0) -> None:
        monkeypatch.setenv("ZAB_FAKE_CLAUDE_REFRESH_RACE", str(n))
        monkeypatch.setattr(cb, "REFRESH_RETRY_DELAY_S", delay)
        monkeypatch.setattr(cb, "REFRESH_RETRY_JITTER_S", 0.0)
        monkeypatch.setattr(cb, "REFRESH_LAST_TRY_S", delay)

    return set_races


async def started(home, text: str = "Reply with pong.", **kw):
    backend = make_backend(home, **kw)
    await backend.start()
    await backend._events.get()  # BackendStatus
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    tid = await backend.start_turn(sid, UserInput(text=text, client_id="c1"))
    return backend, sid, tid


async def waiting_to_retry(backend: cb.ClaudeBackend, sid: str) -> None:
    async with asyncio.timeout(10):
        while True:
            if backend._sessions[sid].retry_wake is not None:
                return
            await asyncio.sleep(0.01)


def fixture_file(tmp_path, records: list[dict]) -> str:
    path = tmp_path / "fixture.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


@integration
async def test_a_lost_race_is_retried_and_the_core_sees_one_turn(home, races):
    races(1)
    backend, sid, tid = await started(home)
    events = await drain(backend, extra=1)
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].turn_id == tid
    assert done[0].status is TurnStatus.COMPLETED and done[0].final_text == "pong"
    assert done[0].error is None
    starts = [e for e in events if isinstance(e, TurnStarted)]
    assert {e.turn_id for e in starts} == {tid}
    assert len({e.pgid for e in starts}) == 2  # one process per try; the core keeps the last
    assert isinstance(events[-1], UsageUpdated)
    first, second = calls(home)
    assert first["stdin"] == second["stdin"] == "Reply with pong."
    # the lost try left the prompt and its error in the transcript, so the retry resumes it
    assert "--session-id" in first["argv"]
    assert "--resume" in second["argv"] and "--session-id" not in second["argv"]
    assert not backend._sessions[sid].busy and backend._sessions[sid].retry_wake is None
    await backend.close()


@integration
async def test_tries_that_all_lose_end_with_the_transient_message(home, races):
    races(9)
    backend, _sid, _tid = await started(home)
    events = await drain(backend, extra=1)
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.FAILED
    assert done[0].error is not None and done[0].error.kind is ErrorKind.BACKEND_UNAVAILABLE
    assert done[0].error.message == cs.REFRESH_RACE_MESSAGE
    assert "login" not in done[0].error.message and done[0].final_text is None
    log = calls(home)
    assert len(log) == cb.REFRESH_RETRIES + 1
    assert all("--resume" in c["argv"] for c in log[1:])
    await backend.close()


@integration
async def test_a_real_sign_out_still_asks_for_login_and_is_not_retried(
    home, races, monkeypatch, tmp_path
):
    races(0)
    records = load("error")
    for rec in records:
        data = rec.get("data") or {}
        if data.get("type") == "assistant":
            data["error"] = "authentication_failed"
            data["message"]["content"] = [{"type": "text", "text": "Not logged in"}]
        elif data.get("type") == "result":
            data.pop("api_error_status", None)
            data["result"] = "Not logged in · Please run /login"
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", fixture_file(tmp_path, records))
    backend, _sid, _tid = await started(home)
    done = (await drain(backend))[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.error is not None and done.error.kind is ErrorKind.AUTH  # the core says !login
    assert len(calls(home)) == 1
    await backend.close()


@integration
@pytest.mark.parametrize("text", [True, False], ids=["after-text", "tool-only"])
async def test_a_race_after_tool_activity_is_not_retried(home, races, monkeypatch, tmp_path, text):
    races(0)
    head = until_tool_result(text=text)
    records = [*head, *fc.refresh_race_records(float(head[-1]["t"]))]
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", fixture_file(tmp_path, records))
    backend, _sid, _tid = await started(home)
    events = await drain(backend)
    assert any(isinstance(e, ItemStarted) for e in events)  # the tool ran: no second try
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.error is not None and done.error.kind is ErrorKind.BACKEND_UNAVAILABLE
    assert len(calls(home)) == 1
    await backend.close()


@integration
async def test_a_stop_during_the_wait_ends_the_turn_without_another_try(home, races):
    races(1, delay=60.0)
    backend, sid, _tid = await started(home)
    await waiting_to_retry(backend, sid)
    await backend.interrupt(sid)
    async with asyncio.timeout(5):
        events = await drain(backend)
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.INTERRUPTED
    assert done.error is None
    assert len(calls(home)) == 1
    await backend.close()


@integration
async def test_close_during_the_wait_returns_at_once(home, races):
    races(1, delay=60.0)
    backend, sid, _tid = await started(home)
    await waiting_to_retry(backend, sid)
    async with asyncio.timeout(5):
        await backend.close()
    done = [e for e in await drain(backend) if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.INTERRUPTED
    assert len(calls(home)) == 1


@integration
async def test_a_steered_turn_retries_with_a_fresh_stdin(home, races):
    races(1, delay=0.5)
    backend, sid, tid = await started(home, steer=True)
    await waiting_to_retry(backend, sid)
    with pytest.raises(SteerRejected):  # held by the core for the next turn (§7.3)
        await backend.steer(sid, tid, UserInput(text="also this", client_id="c2"))
    done = [e for e in await drain(backend) if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0].status is TurnStatus.COMPLETED
    assert done[0].final_text == "pong"
    first, second = calls(home)
    assert first["stdin"] == second["stdin"] == "Reply with pong."
    assert second["stdin_line"]["uuid"] != first["stdin_line"]["uuid"]  # a new StreamInput
    await backend.close()


@integration
async def test_the_last_try_is_timed_from_the_first_loss(home, races, monkeypatch):
    races(9)
    asked: list[tuple[int, float]] = []

    def delay(attempt: int, since_first_loss: float) -> float:
        asked.append((attempt, since_first_loss))
        return 0.0

    monkeypatch.setattr(cb, "refresh_retry_delay", delay)
    backend, _sid, _tid = await started(home)
    await drain(backend, extra=1)
    assert [attempt for attempt, _ in asked] == [0, 1]
    assert asked[0][1] == 0.0 and asked[1][1] > 0.0  # the second loss, after the second try
    await backend.close()
