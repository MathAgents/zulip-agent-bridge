"""`zab dev record-claude` against the fake CLI: a steer that can't be written is recorded and
reported as a mismatch, not lost silently (#660: a steer refused after stdin closed read as
"stdin closed before result#1")."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from zab.dev import record_claude as rc

FAKE = pathlib.Path(__file__).resolve().parents[2] / "src" / "zab" / "testing" / "fake_claude.py"
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "claude" / "2.1.283"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a shebang"),
]


async def record(tmp_path, steers):
    scenario = rc.Scenario("t", ("Reply with pong.",), rc._std, steers=steers)
    argv = [str(FAKE), "-p", "--output-format", "stream-json", "--verbose"]
    argv += ["--input-format", "stream-json", "--replay-user-messages"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ZAB_FAKE_", "CLAUDE"))}
    env |= {
        "ZAB_FAKE_CLAUDE_FIXTURE": str(FIXTURES / "simple.jsonl"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "account"),  # never a real one
        "ZAB_FAKE_CLAUDE_NO_TRANSCRIPT": "1",
    }
    return await rc._record_turn(
        scenario,
        argv,
        env=env,
        cwd=str(tmp_path),
        prompt="Reply with pong.",
        steers=list(steers),
        hold_open=0,
    )


async def test_a_steer_after_stdin_closed_is_recorded_and_reported(tmp_path):
    """The `simple` turn ends at its one `result`, where stdin closes: a steer due 0.2 s after
    it is refused. The recording shows it (`stdin-refused`), and the run lists a mismatch."""
    recording, code = await record(tmp_path, (rc.Steer("result#1", "and ping?", 0.2),))
    assert code == 0
    (refused,) = [r for r in recording.records if r["kind"] == "stdin-refused"]
    assert (refused["after"], refused["text"]) == ("result#1", "and ping?")
    (problem,) = recording.problems
    assert problem.startswith("steer refused at ") and "(result#1 + 0.2 s)" in problem
    assert not [r for r in recording.records if r["kind"] == "stdin" and r["text"] == "and ping?"]


async def test_a_steer_whose_trigger_never_comes_is_reported(tmp_path):
    recording, _code = await record(tmp_path, (rc.Steer("result#2", "and ping?"),))
    assert recording.problems == ["steer not written: result#2 never came"]
