"""Contract tests: recorded `claude -p` fixtures → contract events (SPEC §9.4, §15.1, §15.2).

The fixtures under tests/fixtures/claude/<version>/ were recorded with `zab dev record-claude`
on Claude Code 2.1.283 and scrubbed with `zab dev scrub`. `StreamParser` is exercised directly
on their stdout objects, and `testing/fake_claude.py` replays them through the real process
runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import pytest

from zab.backends import claude_stream as cs
from zab.backends.claude import PINNED_VERSION, ClaudeConfig, build_argv, new_turn_id, turn_env
from zab.core.types import (
    ErrorKind,
    ItemCompleted,
    ItemKind,
    ItemStarted,
    ItemStatus,
    Notice,
    PlanUpdated,
    SessionChanged,
    SessionSettings,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
)

pytestmark = pytest.mark.contract

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "claude" / PINNED_VERSION
FAKE = pathlib.Path(__file__).resolve().parents[2] / "src" / "zab" / "testing" / "fake_claude.py"


def load(name: str) -> tuple[dict, list[dict], int]:
    """(meta, stdout objects, exit code) of a fixture."""
    records = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    meta = next(r for r in records if r["kind"] == "meta")
    objects = [r["data"] for r in records if r["kind"] == "stdout"]
    code = next(r["code"] for r in records if r["kind"] == "exit")
    return meta, objects, code


def session_of(meta: dict) -> str:
    argv = meta["argv"]
    flag = "--resume" if "--resume" in argv else "--session-id"
    return argv[argv.index(flag) + 1]


def replay(name: str, *, session_id: str | None = None, cwd: str | None = None):
    meta, objects, code = load(name)
    sid = session_id or session_of(meta)
    parser = cs.StreamParser(sid, new_turn_id(sid), pgid=4242, cwd=cwd or meta["cwd"])
    events = []
    for obj in objects:
        events.extend(parser.feed(obj))
    events.extend(parser.finish(code, ""))
    return parser, events


def test_fixtures_present():
    names = {p.stem for p in FIXTURES.glob("*.jsonl")}
    assert {"simple", "tool_use", "error", "denial", "resume", "interrupt"} <= names
    assert "background_resume" in names


def test_simple_turn():
    parser, events = replay("simple")
    assert isinstance(events[0], TurnStarted) and events[0].pgid == 4242
    assert events[0].turn_id == parser.info.turn_id
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TurnStarted", "ItemCompleted", "TurnCompleted"]
    msg = events[1]
    assert isinstance(msg, ItemCompleted) and msg.item.kind is ItemKind.AGENT_MESSAGE
    assert msg.item.text == "pong"
    done = events[-1]
    assert isinstance(done, TurnCompleted)
    assert done.status is TurnStatus.COMPLETED and done.final_text == "pong" and done.error is None
    assert done.usage is not None and done.usage.total_tokens and done.usage.total_tokens > 0
    assert done.usage.cost_usd is not None and done.usage.context_window == 200000
    assert parser.info.model == "claude-haiku-4-5-20251001"
    assert parser.info.permission_mode == "bypassPermissions"
    assert parser.info.cli_version == PINNED_VERSION
    assert not any(isinstance(e, SessionChanged) for e in events)


def test_session_id_mismatch_is_reported():
    _parser, events = replay("simple", session_id="00000000-0000-4000-8000-000000000000")
    changed = [e for e in events if isinstance(e, SessionChanged)]
    assert len(changed) == 1
    assert changed[0].session_id == "00000000-0000-4000-8000-000000000000"
    assert changed[0].new_session_id == session_of(load("simple")[0])
    # later events carry the new id
    assert events[-1].session_id == changed[0].new_session_id


def test_tool_use_turn_items_and_plan():
    parser, events = replay("tool_use")
    started = [e for e in events if isinstance(e, ItemStarted)]
    completed = [
        e for e in events if isinstance(e, ItemCompleted) and e.item.kind != "agent_message"
    ]
    by_kind = {}
    for e in started:
        by_kind.setdefault(e.item.kind, []).append(e.item.title)
    assert by_kind[ItemKind.FILE_CHANGE] == ["hello.txt"]  # relative to the cwd
    assert by_kind[ItemKind.COMMAND] == ["cat /work/scratch/hello.txt"]  # verbatim (§9.4)
    assert by_kind[ItemKind.TOOL_CALL] == ["Read README.md"]
    assert by_kind[ItemKind.OTHER] == ["ToolSearch"]
    assert ItemKind.PLAN not in by_kind  # 3 TaskCreate + 6 TaskUpdate only move the plan
    assert len(started) == 4
    # every started item completes, in order, with the same id and title
    assert [e.item.item_id for e in completed] == [e.item.item_id for e in started]
    assert all(e.item.status is ItemStatus.COMPLETED for e in completed)
    assert all(e.item.duration_ms is not None and e.item.duration_ms >= 0 for e in completed)
    bash = next(e for e in completed if e.item.kind is ItemKind.COMMAND)
    assert bash.item.detail == "hello"
    plans = [e for e in events if isinstance(e, PlanUpdated)]
    assert plans, "TaskCreate/TaskUpdate must produce PlanUpdated"
    assert [s for _t, s in plans[-1].steps] == ["completed"] * 3
    assert all(t for t, _s in plans[-1].steps)
    assert any("in_progress" in [s for _t, s in p.steps] for p in plans)
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    assert done.final_text and "hello.txt" in done.final_text
    assert parser.info.num_tool_calls == len(started)


def test_error_turn_is_failed_with_classified_error():
    parser, events = replay("error")
    assert not any(isinstance(e, ItemCompleted) for e in events), (
        "the API error text is not an answer"
    )
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.final_text is None
    assert done.error is not None and done.error.kind is ErrorKind.OTHER
    assert "no-such-model-zab" in done.error.message
    assert parser.info.status is TurnStatus.FAILED


def test_denial_turn_notice_and_failed_item():
    _parser, events = replay("denial")
    started = [e for e in events if isinstance(e, ItemStarted)]
    assert [e.item.kind for e in started] == [ItemKind.COMMAND]
    failed = [
        e for e in events if isinstance(e, ItemCompleted) and e.item.status is ItemStatus.FAILED
    ]
    assert len(failed) == 1 and failed[0].item.title == "mkdir scratch-dir"
    assert failed[0].item.detail and "denied" in failed[0].item.detail
    notices = [e for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].level == "warning"
    # a `dontAsk` denial: its tool_result gives no "What required approval", so no reason line
    assert notices[0].message == denied("this command", "It") + "\n- ` mkdir scratch-dir `"
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    assert done.final_text and "denied" in done.final_text


def denied(what: str, it: str) -> str:
    """The first line of a denial notice (#677)."""
    return (
        f"Claude Code, the program Claude runs in, blocked {what} under its safety rules. {it} "
        f"needed approval, which can't be given from Zulip, so {it.lower()} did not run. Claude "
        "was told and can carry on."
    )


def test_denial_with_claude_codes_reason():
    """Issue #677, Claude Code 2.1.283 in bypassPermissions: `rm` of a relative glob after
    `cd …;` needs approval, which `--permission-prompts none` denies. The denied call's
    tool_result gives the reason after "What required approval: ", and the notice quotes its
    first two lines, clipped; `permission_denials` pairs with it by `tool_use_id`."""
    sid = "ace93447-93f1-42ca-93af-acf7dee24ea7"
    _parser, events = replay("denial_reason", session_id=sid)
    command = "cd /home/agent/work/demo/search && echo a; rm -f runs/*"
    assert [e.item.title for e in events if isinstance(e, ItemStarted)] == [command]
    failed = [e for e in events if isinstance(e, ItemCompleted) and e.item.kind is ItemKind.COMMAND]
    assert len(failed) == 1 and failed[0].item.status is ItemStatus.FAILED
    (notice,) = [e for e in events if isinstance(e, Notice)]
    assert notice.level == "warning"
    assert notice.message == (
        denied("this command", "It")
        + "\n- ` cd /home/agent/work/demo/search && echo a; rm -f runs/* `"
        + "\n  Claude Code's reason: ` Dangerous rm operation detected: "
        "'/home/agent/work/demo/runs/*' This command changes directories before the removal, "
        "so the relative glob target cannot be statically resolved. This requires explicit "
        "approval and cannot be auto-allowed by permission rules. `"  # cut back to a sentence
    )
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED


def test_several_denials_give_one_notice_once_each():
    """Several denials in one result give one notice; a reason given as a list of text blocks
    is read too, and a later result of the same process does not report them again."""
    parser = cs.StreamParser("s", "s:t", multi_result=True)
    calls = [("t1", "Bash", {"command": "rm -f a/*"}), ("t2", "Write", {"file_path": "/w/x.md"})]
    why = [{"type": "text", "text": "No. What required approval: Dangerous rm\nline 2\nline 3"}]
    for tid, name, inp in calls:
        block = {"type": "tool_use", "id": tid, "name": name, "input": inp}
        parser.feed({"type": "assistant", "message": {"content": [block]}})
    blocks = [
        {"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": why if i else "No"}
        for tid, i in (("t1", 1), ("t2", 0))
    ]
    parser.feed({"type": "user", "message": {"content": blocks}})
    denials = [{"tool_name": n, "tool_use_id": tid, "tool_input": inp} for tid, n, inp in calls]
    result = {"type": "result", "subtype": "success", "result": "ok", "permission_denials": denials}
    (notice,) = [e for e in parser.feed(result) if isinstance(e, Notice)]
    assert notice.message == (
        denied("these 2 actions", "They")
        + "\n- ` rm -f a/* `\n  Claude Code's reason: ` Dangerous rm line 2 `"
        + "\n- ` Write(/w/x.md) `"
    )
    assert not [e for e in parser.feed(result) if isinstance(e, Notice)]
    assert parser.info.denials == ["Bash(rm -f a/*)", "Write(/w/x.md)"]


def test_a_reason_is_redacted_without_nuls_and_cut_at_a_sentence():
    """Review of #678: a NUL inside a key is dropped before the redaction (§11.6), and a reason
    cut at 300 characters goes back to its last whole sentence."""
    key = "Q7xK" + "a1B2" * 7
    reason = cs._approval_reason(f"No. What required approval: key={key[:16]}\x00{key[16:]}\nx")
    assert reason == "[redacted zulip api key] x"
    cut = cs._approval_reason("What required approval: " + "One more sentence here. " * 20)
    assert cut is not None and cut.endswith("here.") and len(cut) <= 300
    assert cs._approval_reason("What required approval: " + "word " * 80).endswith("…")


def test_interrupt_fixture_fails_the_turn_with_context():
    """SIGINT mid-tool: the CLI rejects the tool use, echoes "[Request interrupted …]" and ends
    with `error_during_execution` (§9.6); the backend turns this into INTERRUPTED when it sent
    the signal itself, otherwise it stays a failure with that context."""
    _parser, events = replay("interrupt")
    started = [e for e in events if isinstance(e, ItemStarted)]
    assert [e.item.kind for e in started] == [ItemKind.COMMAND]
    ended = [e for e in events if isinstance(e, ItemCompleted) and e.item.kind is ItemKind.COMMAND]
    assert len(ended) == 1 and ended[0].item.status is ItemStatus.FAILED
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.FAILED
    assert done.final_text is None
    assert done.error is not None and done.error.kind is ErrorKind.OTHER
    assert "error_during_execution" in done.error.message
    assert "interrupted" in done.error.message.lower()


def test_resume_turn_keeps_session_id():
    meta, _objects, _code = load("resume")
    assert "--resume" in meta["argv"]
    _parser, events = replay("resume")
    assert not any(isinstance(e, SessionChanged) for e in events)
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.final_text == "kumquat"


ANSWER = (
    'I started a background shell command that sleeps for 4 minutes and then writes "built" to '
    "`build-done.txt`."
)


def test_a_background_task_notification_on_resume_does_not_end_the_turn():
    """issue #36 (Drew's end-to-end test, Wed Oct 7): a resumed session whose previous turn left
    a background task running gets that task's notification first, then an empty result
    (`num_turns: 0`), a second `init`, and only then the turn and its real result. Completing
    on the first result posted "(no final message)" and dropped the answer."""
    _meta, objects, _code = load("background_resume")
    results = [o for o in objects if o.get("type") == "result"]
    assert len(results) == 2 and results[0]["num_turns"] == 0 and not results[0].get("result")
    parser, events = replay("background_resume")
    done = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(done) == 1 and done[0] is events[-1]
    assert done[0].status is TurnStatus.COMPLETED and done[0].final_text == ANSWER
    assert parser.info.final_text == ANSWER and parser.info.usage.output_tokens == 154


def test_an_empty_result_alone_still_ends_the_turn():
    """A lone `/compact` ends with the same empty `num_turns: 0` result (DECISIONS): when the
    process exits with nothing after it, the held result is the turn's end; a failed exit
    after it is a failure, as for any turn without a result."""
    sid = "11111111-2222-4333-8444-555555555555"
    empty = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0}
    init = {"type": "system", "subtype": "init", "session_id": sid, "model": "m"}
    parser = cs.StreamParser(sid, new_turn_id(sid))
    events = parser.feed(init) + parser.feed(empty)
    assert not any(isinstance(e, TurnCompleted) for e in events) and not parser.completed
    events = parser.finish(0, "")
    assert [type(e).__name__ for e in events] == ["TurnCompleted"]
    assert events[0].status is TurnStatus.COMPLETED and events[0].final_text is None
    parser = cs.StreamParser(sid, new_turn_id(sid))
    parser.feed(init)
    parser.feed(empty)
    failed = parser.finish(1, "killed")[-1]
    assert isinstance(failed, TurnCompleted) and failed.status is TurnStatus.FAILED
    # an error result is never held, even with num_turns 0
    parser = cs.StreamParser(sid, new_turn_id(sid))
    events = parser.feed({**empty, "is_error": True, "result": "Invalid API key"})
    assert isinstance(events[-1], TurnCompleted) and events[-1].status is TurnStatus.FAILED


def test_process_death_without_result_fails_the_turn():
    sid = session_of(load("tool_use")[0])
    parser = cs.StreamParser(sid, new_turn_id(sid))
    _meta, objects, _code = load("tool_use")
    events = []
    for obj in objects:  # stop right after the first tool call, before its result
        events.extend(parser.feed(obj))
        if any(isinstance(e, ItemStarted) for e in events):
            break
    assert any(isinstance(e, ItemStarted) for e in events)
    tail = parser.finish(137, "some stderr\nKilled")
    assert isinstance(tail[-1], TurnCompleted) and tail[-1].status is TurnStatus.FAILED
    assert tail[-1].error is not None and "137" in tail[-1].error.message
    assert tail[-1].error.message.endswith(": Killed")
    # the pending tool call is closed as failed so the status log does not dangle
    assert any(isinstance(e, ItemCompleted) and e.item.status is ItemStatus.FAILED for e in tail)


def test_session_change_then_death_completes_under_the_new_id():
    parser = cs.StreamParser("old-id", "old-id:t")
    events = parser.feed({"type": "system", "subtype": "init", "session_id": "new-id"})
    assert [type(e).__name__ for e in events] == ["TurnStarted", "SessionChanged"]
    assert events[0].session_id == "old-id" and events[1].new_session_id == "new-id"
    events += parser.feed(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    )
    assert events[-1].session_id == "new-id"
    tail = parser.finish(1, "boom")
    assert isinstance(tail[-1], TurnCompleted) and tail[-1].session_id == "new-id"


def test_process_death_before_init_still_starts_and_completes():
    parser = cs.StreamParser("s", "s:t", pgid=7)
    events = parser.finish(
        1, "Error: When using --print, --output-format=stream-json requires --verbose"
    )
    assert [type(e).__name__ for e in events] == ["TurnStarted", "TurnCompleted"]
    assert events[1].error is not None and "--verbose" in events[1].error.message


@pytest.mark.parametrize(
    ("code", "text", "stderr", "kind"),
    [
        (None, "Not logged in. Please run /login", "", ErrorKind.AUTH),
        ("authentication_failed", "", "", ErrorKind.AUTH),
        (None, "", "API Error: 401 Unauthorized", ErrorKind.AUTH),
        (None, "You've hit your usage limit. It resets at 3pm", "", ErrorKind.USAGE_LIMIT),
        ("rate_limit", "", "", ErrorKind.USAGE_LIMIT),
        (None, "Prompt is too long for the context window", "", ErrorKind.CONTEXT_WINDOW),
        (None, "", "fetch failed: ECONNRESET", ErrorKind.NETWORK),
        ("model_not_found", "There's an issue with the selected model", "", ErrorKind.OTHER),
    ],
)
def test_classify_error(code, text, stderr, kind):
    info = cs.classify_error(code, text, stderr)
    assert info.kind is kind
    assert info.message


def test_classify_error_reset_time():
    info = cs.classify_error(None, "usage limit reached", "", reset_at=1000.0, now=400.0)
    assert info.kind is ErrorKind.USAGE_LIMIT and info.retry_after_s == 600.0
    info = cs.classify_error(None, "Rate limit hit, try again in 5 minutes", "")
    assert info.retry_after_s == 300.0
    long = cs.classify_error(None, "x" * 5000, "")
    assert len(long.message) == 1000


# Decoys (#661): a zuliprc key (32 alphanumerics) and a GitHub token (`ghp_` and 36).
ZKEY = "Q7xK" + "a1B2" * 7
GHP = "ghp_" + "Z9y8X7w6" * 4 + "V5u4"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n"
#: What `str.strip` takes and the zuliprc rule's blanks (` `, tab) don't, ending no line (#666).
STRIPPED = [
    c
    for c in map(chr, range(0x3001))
    if c.isspace() and c not in " \t" and len(f"a{c}b".splitlines()) == 1
]


@pytest.mark.parametrize(
    ("text", "stderr", "hidden"),
    [
        ("x" * 990 + "\nkey=" + ZKEY + "\nmore", "", ZKEY[:4]),  # the key crosses 1000
        ("y" * 990 + " " + GHP + " more", "", "ghp_"),  # the token crosses 1000
        ("", "boom\n" + PEM, "MIIEvQ"),  # stderr's last line, a key's body without its BEGIN
    ],
)
def test_an_error_is_redacted_before_it_is_cut(text, stderr, hidden):
    """#661: `classify_error` takes stderr's last line and cuts at 1000 characters; either can
    leave too little of a secret for its rule (SPEC §11.6), so it redacts first."""
    message = cs.classify_error(None, text, stderr).message
    assert hidden not in message and len(message) <= 1000 and "[redacted" in message


@pytest.mark.parametrize("blank", STRIPPED, ids=lambda c: f"U+{ord(c):04X}")
def test_an_error_is_stripped_before_it_is_redacted(blank):
    """#666: `.strip()` takes a blank that ends no line, so `key=<32>` and one is a whole
    zuliprc line only once stripped; the result text and stderr are stripped first."""
    line = "boom\nkey=" + ZKEY + blank
    for text, stderr in ((line, ""), ("", line), ("", blank + "key=" + ZKEY + blank)):
        message = cs.classify_error(None, text, stderr).message
        assert ZKEY[:4] not in message and "[redacted zulip api key]" in message


@pytest.mark.parametrize(
    ("stderr", "hidden"),
    [
        ("z" * 480 + " " + GHP, "ghp_"),  # the token crosses 500
        ("starting\n" + PEM, "MIIEvQ"),  # the last line, a key's body without its BEGIN
        ("boom\nkey=" + ZKEY + "\n", ZKEY[:4]),  # a whole zuliprc line, the last one
        ("progress 10%\rkey=" + ZKEY, ZKEY[:4]),  # a line `splitlines` ends at a lone `\r`
        # a whole line only once `.strip()` took the U+3000 (#666): the line's own redaction
        *[("boom\nkey=" + ZKEY + blank, ZKEY[:4]) for blank in STRIPPED],
    ],
)
def test_an_exit_without_a_result_is_redacted_before_its_cut(stderr, hidden):
    """#661: the same for `finish`'s message: the last line of the stderr tail, at 500. The
    line goes after `claude exited with code N without a result: `, so no whole-line rule
    matches it once there: on main a zuliprc key line showed whole (review of #663). A line
    `splitlines` ends at a lone carriage return, or another line end that `^` and `$` don't
    know, is a whole line too. So is one that `.strip()` makes whole after the redaction, which
    the taken line's own redaction is for (#666)."""
    sid = "11111111-2222-4333-8444-555555555555"
    (done,) = [
        e for e in cs.StreamParser(sid, "t").finish(1, stderr) if isinstance(e, TurnCompleted)
    ]
    assert hidden not in done.error.message and "[redacted" in done.error.message


def test_a_stderr_of_nuls_alone_still_gives_the_error():
    """Codex round 1 on #663: stderr that is empty once its NULs are dropped has no last line;
    the error then says only that the CLI exited."""
    assert cs.classify_error(None, "", "\x00\n").message == (
        "claude exited without a result (unknown error)"
    )
    sid = "11111111-2222-4333-8444-555555555555"
    (done,) = [
        e for e in cs.StreamParser(sid, "t").finish(1, "\x00\n") if isinstance(e, TurnCompleted)
    ]
    assert done.status is TurnStatus.FAILED
    assert done.error.message == "claude exited with code 1 without a result"


@pytest.mark.parametrize(
    ("name", "tool_input", "kind", "title"),
    [
        ("Bash", {"command": "pytest -q"}, ItemKind.COMMAND, "pytest -q"),
        ("Edit", {"file_path": "/work/scratch/src/a.py"}, ItemKind.FILE_CHANGE, "src/a.py"),
        ("Write", {"file_path": "/elsewhere/b.py"}, ItemKind.FILE_CHANGE, "/elsewhere/b.py"),
        (
            "NotebookEdit",
            {"notebook_path": "/work/scratch/n.ipynb"},
            ItemKind.FILE_CHANGE,
            "n.ipynb",
        ),
        ("Read", {"file_path": "/work/scratch/README.md"}, ItemKind.TOOL_CALL, "Read README.md"),
        ("Grep", {"pattern": "def main"}, ItemKind.TOOL_CALL, "Grep def main"),
        ("WebSearch", {"query": "zulip math"}, ItemKind.WEB_SEARCH, "zulip math"),
        ("WebFetch", {"url": "https://zulip.com"}, ItemKind.WEB_SEARCH, "https://zulip.com"),
        ("Task", {"description": "explore repo"}, ItemKind.SUBAGENT, "explore repo"),
        ("mcp__lmfdb__run_sql", {"sql": "select 1"}, ItemKind.TOOL_CALL, "lmfdb.run_sql"),
        ("TaskCreate", {"subject": "fix parser"}, ItemKind.PLAN, "fix parser"),
        ("ToolSearch", {"query": "x"}, ItemKind.OTHER, "ToolSearch"),
        ("Bash", {"command": "x" * 300}, ItemKind.COMMAND, "x" * 119 + "…"),
    ],
)
def test_classify_tool(name, tool_input, kind, title):
    assert cs.classify_tool(name, tool_input, "/work/scratch") == (kind, title)


@pytest.mark.parametrize(
    "obj",
    [
        {"type": "assistant", "message": None},
        {"type": "assistant", "message": "text"},
        {"type": "assistant", "message": {"content": "not a list"}},
        {"type": "assistant", "message": {"content": ["not a block", 7, None]}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": 5, "input": "x"}]},
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["a"]}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": {"x": 1}}]}},
        {"type": "system", "subtype": "init", "session_id": 12, "model": ["x"]},
        {"type": "rate_limit_event", "rate_limit_info": "nope"},
        {"type": "result", "usage": "none", "modelUsage": [], "permission_denials": "x"},
        {"type": ["weird"]},
        "not an object",
    ],
)
def test_feed_survives_malformed_objects(obj):
    """issue #36 item 6: one odd shape must not end the turn."""
    parser = cs.StreamParser("s", "s:t")
    parser.feed({"type": "system", "subtype": "init", "session_id": "s"})
    events = parser.feed(obj)  # no exception
    assert all(hasattr(e, "session_id") for e in events)
    tail = parser.finish(0, "")
    assert parser.completed or isinstance(tail[-1], TurnCompleted)


def test_plan_seed_and_provider_notices():
    """issue #36: the plan outlives a turn; API-key and third-party-provider use is noticed."""
    first = cs.StreamParser("s", "s:1")
    first.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "TaskCreate",
                        "input": {"subject": "a"},
                    }
                ]
            },
        }
    )
    first.feed(
        {
            "type": "user",
            "tool_use_result": {"task": {"id": "1", "subject": "a"}},
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        }
    )
    assert first.plan == {"1": ("a", "pending")}
    second = cs.StreamParser("s", "s:2", plan=first.plan)
    events = second.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "TaskUpdate",
                        "input": {"taskId": "1", "status": "completed"},
                    }
                ]
            },
        }
    )
    plan = [e for e in events if isinstance(e, PlanUpdated)]
    assert plan and plan[-1].steps == (("a", "completed"),)

    guard = cs.StreamParser("s", "s:3")
    events = guard.feed(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "s",
            "apiKeySource": "ANTHROPIC_API_KEY",
        }
    )
    notices = [e for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and "API key" in notices[0].message
    events = guard.feed(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "x",
            "modelUsage": {"m": {"provider": "bedrock", "contextWindow": 1}},
        }
    )
    notices = [e for e in events if isinstance(e, Notice)]
    assert len(notices) == 1 and "bedrock" in notices[0].message
    clean = cs.StreamParser("s", "s:4")
    assert not any(
        isinstance(e, Notice)
        for e in clean.feed(
            {"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none"}
        )
    )


def test_todowrite_plan_mapping():
    parser = cs.StreamParser("s", "s:t")
    events = parser.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "TodoWrite",
                        "input": {
                            "todos": [
                                {"content": "a", "status": "completed"},
                                {"content": "b", "status": "in_progress"},
                                {"content": "c", "status": "pending"},
                            ]
                        },
                    }
                ]
            },
        }
    )
    plan = [e for e in events if isinstance(e, PlanUpdated)]
    assert plan and plan[0].steps == (("a", "completed"), ("b", "in_progress"), ("c", "pending"))


# ----------------------------------------------------------------------- through the fake binary


def fake_env(fixture: str, **extra: str) -> dict[str, str]:
    env = turn_env("/nonexistent/.claude", {"PATH": os.environ["PATH"]})
    env["ZAB_FAKE_CLAUDE_NO_TRANSCRIPT"] = "1"
    env["ZAB_FAKE_CLAUDE_FIXTURE"] = str(FIXTURES / f"{fixture}.jsonl")
    env.update(extra)
    return env


async def _run_fake(fixture: str, tmp_path, *, session_id: str, env_extra: dict | None = None):
    config = ClaudeConfig(binary=str(FAKE))
    argv = build_argv(config, SessionSettings(model="haiku"), session_id=session_id, resume=False)
    parser = cs.StreamParser(session_id, new_turn_id(session_id), cwd=str(tmp_path))
    events = []
    holder: list = []
    info, code = await cs.run_parsed_turn(
        argv,
        env=fake_env(fixture, **(env_extra or {})),
        cwd=str(tmp_path),
        prompt="Reply with the single word pong.\n",
        parser=parser,
        on_event=events.append,
        turn_holder=holder,
    )
    return info, code, events, holder


@pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang")
async def test_fake_claude_end_to_end(tmp_path):
    log = tmp_path / "calls.jsonl"
    sid = "12345678-1234-4123-8123-123456789abc"
    info, code, events, holder = await _run_fake(
        "simple", tmp_path, session_id=sid, env_extra={"ZAB_FAKE_CLAUDE_LOG": str(log)}
    )
    assert code == 0 and info.status is TurnStatus.COMPLETED and info.final_text == "pong"
    assert isinstance(events[0], TurnStarted) and isinstance(events[0].pgid, int)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 2  # at spawn, then with system/init's model (SPEC B.2 amendment)
    assert started[0].model is None and started[1].model == "claude-haiku-4-5-20251001"
    assert started[1].effort_supported is False  # haiku: per_turn_effort_active false
    assert started[1].pgid == started[0].pgid == holder[0].pgid != os.getpgid(0)
    assert not any(isinstance(e, SessionChanged) for e in events)  # the fake rewrote the id
    call = json.loads(log.read_text().splitlines()[0])
    assert call["stdin"] == "Reply with the single word pong.\n"
    assert call["argv"][:2] == ["-p", "--output-format"] and sid in call["argv"]
    assert call["env"]["CLAUDE_CONFIG_DIR"] == "/nonexistent/.claude"
    assert "ANTHROPIC_API_KEY" not in call["env"]["names"]
    assert call["cwd"] == str(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang")
async def test_oversized_stdout_line_is_skipped(tmp_path):
    """issue #36 item 6: a line over the reader limit must not raise out of the turn."""
    from zab.backends import claude as cb

    script = tmp_path / "big.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        'print(\'{"type":"system","subtype":"init","session_id":"s"}\', flush=True)\n'
        f"sys.stdout.write('x' * {cb._LINE_LIMIT + 1024} + '\\n')\n"
        'print(\'{"type":"result","subtype":"success","is_error":false,"result":"ok",'
        '"session_id":"s"}\', flush=True)\n'
    )
    parser = cs.StreamParser("s", "s:t")
    events = []
    info, code = await cs.run_parsed_turn(
        [sys.executable, str(script)],
        env=os.environ,
        cwd=str(tmp_path),
        prompt="x",
        parser=parser,
        on_event=events.append,
    )
    assert code == 0 and info.status is TurnStatus.COMPLETED and info.final_text == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang")
async def test_oversized_line_after_exit_is_skipped(tmp_path):
    """codex round 1 on PR #49: the over-limit handler also covers lines read after the CLI
    has exited (a grandchild inherits stdout and writes them)."""
    from zab.backends import claude as cb

    script = tmp_path / "late_big.py"
    script.write_text(
        "import subprocess, sys\n"
        "sys.stdin.read()\n"
        'print(\'{"type":"system","subtype":"init","session_id":"s"}\', flush=True)\n'
        # a grandchild that, after the parent is gone, writes an oversized line then a result
        "subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(0.5); "
        f"sys.stdout.write(\\'x\\' * {cb._LINE_LIMIT + 1024} + \\'\\\\n\\'); "
        'sys.stdout.write(\\\'{"type":"result","subtype":"success","is_error":false,'
        '"result":"late","session_id":"s"}\\\\n\\\'); sys.stdout.flush()\'])\n'
    )
    parser = cs.StreamParser("s", "s:t")
    events = []
    info, code = await cs.run_parsed_turn(
        [sys.executable, str(script)],
        env=os.environ,
        cwd=str(tmp_path),
        prompt="x",
        parser=parser,
        on_event=events.append,
    )
    assert code == 0 and info.final_text == "late" and info.status is TurnStatus.COMPLETED


@pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang")
async def test_oversized_line_suffix_is_never_parsed(tmp_path):
    """codex round 1 on PR #44: the tail of an over-limit line must not become a record, even
    when it is valid JSON that would complete the turn."""
    from zab.backends import claude as cb

    script = tmp_path / "evil.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
        f"sys.stdout.write('x' * {cb._LINE_LIMIT + 1})\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'is_error': False, "
        "'result': 'false success', 'session_id': 's'}) + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'is_error': True, "
        "'subtype': 'error_during_execution', 'result': 'real', 'session_id': 's'}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    parser = cs.StreamParser("s", "s:t")
    info, code = await cs.run_parsed_turn(
        [sys.executable, str(script)], env=os.environ, cwd=str(tmp_path), prompt="x", parser=parser
    )
    assert code == 0
    assert info.status is TurnStatus.FAILED  # the real (second) result, not the smuggled one
    assert info.final_text is None


@pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang")
async def test_fake_claude_error_exit_code(tmp_path):
    info, code, events, _holder = await _run_fake(
        "error", tmp_path, session_id="12345678-1234-4123-8123-123456789abc"
    )
    assert code == 1 and info.status is TurnStatus.FAILED
    assert isinstance(events[-1], TurnCompleted) and events[-1].error is not None


@pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang")
async def test_fake_claude_interrupt_kills_the_group(tmp_path):
    sid = "12345678-1234-4123-8123-123456789abc"
    running = asyncio.Event()
    holder: list = []
    config = ClaudeConfig(binary=str(FAKE))
    argv = build_argv(config, SessionSettings(), session_id=sid, resume=False)
    parser = cs.StreamParser(sid, new_turn_id(sid))
    events = []

    def on_event(ev):
        events.append(ev)
        if isinstance(ev, ItemStarted):
            running.set()

    task = asyncio.create_task(
        cs.run_parsed_turn(
            argv,
            env=fake_env("tool_use", ZAB_FAKE_CLAUDE_HANG="1"),
            cwd=str(tmp_path),
            prompt="x",
            parser=parser,
            on_event=on_event,
            turn_holder=holder,
        )
    )
    # The fake sets its SIGINT handler (exit 130) before its first output: wait for that, not a
    # fixed sleep, which a loaded runner can outlast while the fake still starts (#548).
    await asyncio.wait_for(running.wait(), 30)
    await holder[0].interrupt(grace_s=2.0)
    info, code = await asyncio.wait_for(task, 10)
    assert code == 130
    assert info.status is TurnStatus.FAILED  # the M2 backend turns this into INTERRUPTED (§9.6)
    assert isinstance(events[-1], TurnCompleted)


@pytest.mark.parametrize(
    "scenario", ["simple", "tool_use", "error", "denial", "resume", "interrupt"]
)
def test_recorded_fixtures_raise_no_key_or_provider_notice(scenario):
    """All six recordings were made on the account login (apiKeySource "none", firstParty), so
    the guard must stay silent on them (PM review on PR #49)."""
    parser = cs.StreamParser("s", "s:f")
    parser.start()
    notices = []
    for line in (FIXTURES / f"{scenario}.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec.get("kind") != "stdout":
            continue
        for ev in parser.feed(rec["data"]):
            if isinstance(ev, Notice) and ("API key" in ev.message or "provider" in ev.message):
                notices.append(ev)
    assert notices == []


def test_result_with_odd_usage_fields_is_still_completed():
    """PM review on PR #44: a successful `result` whose usage, modelUsage or cost field has an
    unexpected shape was reported as FAILED; the turn completes and the usage is left empty."""
    parser = cs.StreamParser("s", "s:t")
    parser.feed({"type": "system", "subtype": "init", "session_id": "s"})
    events = parser.feed(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "s",
            "result": "done",
            "usage": "weird",
            "modelUsage": [1, 2],
            "total_cost_usd": "n/a",
            "permission_denials": "x",
        }
    )
    done = events[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    assert done.final_text == "done" and done.error is None
    assert done.usage is not None and done.usage.total_tokens == 0 and done.usage.cost_usd is None
    assert done.usage.context_window is None
    # odd values inside otherwise valid containers: counted as zero, not as a failure
    parser = cs.StreamParser("s", "s:t")
    parser.feed({"type": "system", "subtype": "init", "session_id": "s"})
    done = parser.feed(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "s",
            "result": "ok",
            "usage": {"input_tokens": "many", "output_tokens": 7, "cache_read_input_tokens": None},
            "modelUsage": {"m": {"contextWindow": "big", "provider": "firstParty"}, "n": 3},
            "total_cost_usd": True,
        }
    )[-1]
    assert isinstance(done, TurnCompleted) and done.status is TurnStatus.COMPLETED
    assert (done.usage.input_tokens, done.usage.output_tokens, done.usage.total_tokens) == (0, 7, 7)
    assert done.usage.cost_usd is None and done.usage.context_window == 0


def test_one_bad_block_does_not_drop_the_rest_of_the_message():
    """PM review on PR #44: the parser's guard was per event, so one malformed block dropped every
    event of its message. Each block is guarded on its own now."""
    parser = cs.StreamParser("s", "s:t")
    parser.feed({"type": "system", "subtype": "init", "session_id": "s"})
    events = parser.feed(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "tool_use", "name": 5, "input": "not a dict", "id": "t1"},
                    {"type": "text", "text": ["not", "a", "string"]},
                    {"type": "text", "text": "second"},
                ],
            },
        }
    )
    texts = [e.item.text for e in events if isinstance(e, ItemCompleted)]
    assert texts == ["first", "second"]
    # a tool result whose content is unreadable does not hide the one after it
    parser.feed(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "ok"}
                ]
            },
        }
    )
    events = parser.feed(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": {"x": 1}, "content": object},
                    {"type": "tool_result", "tool_use_id": "ok", "content": "listing"},
                ]
            },
        }
    )
    done = [e for e in events if isinstance(e, ItemCompleted)]
    assert len(done) == 1 and done[0].item.item_id == "ok" and done[0].item.detail == "listing"


def _turn_with_background(tasks_at_result: list, *, failed: bool = False) -> TurnCompleted:
    """A turn shaped as Claude Code 2.1.283 reports a `run_in_background` Bash call."""
    sid = "11111111-2222-4333-8444-555555555555"
    task = {"task_id": "b1", "task_type": "local_bash", "description": "sleep 240"}
    stream = [
        {"type": "system", "subtype": "init", "session_id": sid, "model": "m"},
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}]},
        },
        {"type": "system", "subtype": "background_tasks_changed", "tasks": [task]},
        {"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": "tu1"},
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": ""}]},
        },
        {"type": "system", "subtype": "background_tasks_changed", "tasks": tasks_at_result},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "ok"}]}},
        {
            "type": "result",
            "subtype": "error_during_execution" if failed else "success",
            "is_error": failed,
            "num_turns": 2,
            "result": "Started it." if not failed else "boom",
        },
    ]
    parser = cs.StreamParser(sid, new_turn_id(sid))
    events = [e for obj in stream for e in parser.feed(obj)]
    return [e for e in events if isinstance(e, TurnCompleted)][-1]


def test_a_turn_that_ends_with_a_background_job_running_says_so():
    """PM on issue #36 (Wed 17:40): under -p the CLI stops background jobs about 5 s after the
    result and nothing wakes the agent for them, so the answer gets one line saying so."""
    done = _turn_with_background([{"task_id": "b1"}])
    assert done.final_text == "Started it.\n\n" + cs.BACKGROUND_NOTE
    assert cs.BACKGROUND_NOTE.startswith("⚠️ **Bridge** (not Claude): A background job")  # #677
    # a job that finished during the turn (the CLI's list is empty again) gets nothing
    assert _turn_with_background([]).final_text == "Started it."
    # entries with a finished status (not seen in 2.1.283, whose entries have none) don't count
    finished = [{"task_id": "b1", "status": "completed"}, {"task_id": "b2", "status": "stopped"}]
    assert _turn_with_background(finished).final_text == "Started it."
    assert (
        cs.BACKGROUND_NOTE
        in _turn_with_background([{"task_id": "b1", "status": "running"}]).final_text
    )
    # a failed turn keeps its error presentation
    assert cs.BACKGROUND_NOTE not in (
        _turn_with_background([{"task_id": "b1"}], failed=True).final_text or ""
    )


@pytest.mark.parametrize("sep", ["", "\x00"])
def test_a_tool_title_is_redacted_before_it_is_put_on_one_line(sep):
    """Review of #658: a title is one line, which defeats the whole-line zuliprc rule (SPEC
    §11.6), so it is redacted first, NULs dropped before, as the renderer does."""
    key = "Q7xK" + "a1B2" * 3 + sep + "a1B2" * 4
    command = f"cat > ~/.zuliprc <<'EOF'\n[api]\nkey={key}\nEOF"
    kind, title = cs.classify_tool("Bash", {"command": command})
    assert kind is ItemKind.COMMAND
    assert title == "cat > ~/.zuliprc <<'EOF' [api] [redacted zulip api key] EOF"


@pytest.mark.parametrize("blank", STRIPPED, ids=lambda c: f"U+{ord(c):04X}")
def test_a_title_is_redacted_again_once_its_blanks_are_spaces(blank):
    """#666: one line's blanks are single spaces, which can make a zuliprc line, or a
    `ZULIP_API_KEY =`, that the first redaction didn't see; the title is redacted again."""
    for command in (
        "key=" + ZKEY + blank,
        f"key{blank}={blank}{ZKEY}",
        f"export ZULIP_API_KEY{blank}={blank}{ZKEY}",
        f"curl -u{blank}bot@x.y:{ZKEY} https://z/api",
    ):
        _kind, title = cs.classify_tool("Bash", {"command": command})
        assert ZKEY[:4] not in title and "[redacted zulip api key]" in title
