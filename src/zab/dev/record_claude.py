"""`zab dev record-claude`: record stream-json fixtures from the real `claude -p` (SPEC §15.2).

Each scenario runs in a fresh scratch git repository under the system temp directory, against
the account given with `--config-dir` (never the account running the developer's own session),
on the cheapest model. The raw transcript goes to `<out>/<version>/raw/<scenario>.jsonl` (kept
out of git by `.gitignore`) and a scrubbed copy to `<out>/<version>/<scenario>.jsonl`, which is
what `testing/fake_claude.py` replays and the contract tests read.

Fixture format, one JSON object per line:

    {"kind": "meta", "fixture": 1, "version": …, "scenario": …, "argv": […], "prompt": …,
     "cwd": …, "recorded_at": …}
    {"kind": "stdout", "t": <seconds since start>, "data": <the parsed stream-json object>}
    {"kind": "stdout-text", "t": …, "line": <a non-JSON stdout line>}
    {"kind": "stderr", "t": …, "line": …}
    {"kind": "exit", "t": …, "code": <exit code>}

Scenarios (§15.2): `simple`, `tool_use`, `error`, `denial`, `resume` (two turns; the second is
the fixture). `interrupt` is recorded in M2 once the backend's stop path exists.
`background_resume` (two turns): a background task left running by the first turn is stopped
when that process exits, and the second turn's process reports it first, with an empty result.

The `steer_*` scenarios run with `--input-format stream-json` (`ClaudeConfig.steer`) and write a
second user line during the turn, through the backend's own `StreamInput` (PM on issue #36, Fri
Oct 9): `steer_tool` during a tool call, `steer_text` while the model writes its final text,
`steer_after_result` just after the first `result` (stdin is held open for it), and
`steer_resume` (two turns) on a resume with a stopped task's notification pending, and
`subagent_background` (#660): a background sub-agent, busy in a foreground command for a
minute, outlives the first `result`; stdin stays open for it, and a steer 3 s after that
`result` is taken in by a loop of its own before the sub-agent's notification starts the last
loop. `subagent_background_quick` takes stream-json input with no steer: its sub-agent ends
within seconds, and the notification starts the last loop by itself, stdin open. Each line
written is recorded as `{"kind": "stdin", "t": …, "uuid": …, "text": …}`, the prompt first; a
steer refused because stdin had closed by then, as `{"kind": "stdin-refused", "t": …, "after":
<its trigger>, "text": …}`. A steer not written makes the run report a MISMATCH and exit 1.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from zab.backends.claude import (
    ClaudeConfig,
    StreamInput,
    build_argv,
    new_session_id,
    run_turn,
    turn_env,
)
from zab.core.types import SessionSettings, SteerRejected
from zab.dev.scrub import Scrubber, default_rules


@dataclass(frozen=True, slots=True)
class Steer:
    """A user line written during the last turn: `delay` seconds after `on`, which is `start`
    (the process exists), `echo` (the prompt was echoed), `tool_use:<name>` or `result#<n>`."""

    on: str
    text: str
    delay: float = 0.0


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    prompts: tuple[str, ...]
    settings: Callable[[str], SessionSettings]  # model → settings
    note: str = ""
    interrupt_after_tool: str | None = None  # send the §9.6 interrupt once this tool starts
    steers: tuple[Steer, ...] = ()  # every turn then takes stream-json input
    hold_open: int = 0  # results after which stdin stays open, for a steer written there
    stream_input: bool = False  # stream-json input (`StreamInput`) even without a steer


def _std(model: str) -> SessionSettings:
    return SessionSettings(model=model, approval="bypassPermissions")


_STEER_ECHO = "Also run the shell command `echo steered` and include its output in your answer."


SCENARIOS: dict[str, Scenario] = {
    "simple": Scenario("simple", ("Reply with the single word pong.",), _std, "one text answer"),
    "tool_use": Scenario(
        "tool_use",
        (
            "Do these steps, tracking them as tasks: create the task list first with TaskCreate "
            "(one task per step) and mark each task completed with TaskUpdate as you finish it. "
            "(1) Create a file hello.txt containing the single line hello. (2) Run the shell "
            "command `cat hello.txt`. (3) Read README.md. Then reply with one sentence saying "
            "what you did.",
        ),
        _std,
        "Write, Bash, Read and the task tools",
    ),
    "error": Scenario(
        "error",
        ("Reply with pong.",),
        lambda _model: SessionSettings(model="no-such-model-zab", approval="bypassPermissions"),
        "API error: unknown model",
    ),
    "denial": Scenario(
        "denial",
        (
            "Create a directory named scratch-dir with mkdir, then delete it with the shell "
            "command `rm -rf scratch-dir`. If a tool call is denied, say so in one sentence "
            "and stop.",
        ),
        lambda model: SessionSettings(model=model, approval="dontAsk"),
        "a command denied under dontAsk (acceptEdits allowed rm -rf in -p mode; DECISIONS §9.5)",
    ),
    "resume": Scenario(
        "resume",
        (
            "Remember the code word kumquat and reply only with OK.",
            "What is the code word? Reply with just the word.",
        ),
        _std,
        "second turn resumes the first",
    ),
    "background_resume": Scenario(
        "background_resume",
        (
            "Start the shell command `sleep 240; echo built > build-done.txt` with the Bash tool "
            "as a background task (run_in_background), standing in for a long build. Do not wait "
            "for it. Then reply with one sentence saying what you started.",
            "Summarise what you did in your last turn in one sentence.",
        ),
        _std,
        "the first turn leaves a background task running; its notification is delivered when "
        "the second resumes, as an empty `num_turns: 0` result before the turn's own (issue #36)",
    ),
    "interrupt": Scenario(
        "interrupt",
        (
            "Run the shell command `python3 -c 'import time; time.sleep(40)'` with the Bash "
            "tool, then reply with the word done.",
        ),
        _std,
        "SIGINT to the process group one second after the Bash tool starts (§9.6)",
        interrupt_after_tool="Bash",
    ),
    "steer_tool": Scenario(
        "steer_tool",
        (
            "Run the shell command `sleep 20` with the Bash tool in the foreground (not in the "
            "background), then reply with one short sentence.",
        ),
        _std,
        "a steer 2 s into a foreground tool call: taken in after it, in the same result",
        steers=(Steer("tool_use:Bash", _STEER_ECHO, 2.0),),
    ),
    "steer_text": Scenario(
        "steer_text",
        ("Write a 200-word story about a lighthouse keeper. Do not use any tools.",),
        _std,
        "a steer while the model writes its final text: a second loop and result",
        steers=(Steer("echo", "Also add a one-line title at the top.", 0.5),),
    ),
    "steer_after_result": Scenario(
        "steer_after_result",
        ("Reply with the single word pong.",),
        _std,
        "a steer just after the first result, stdin held open for it: a second loop and result",
        steers=(Steer("result#1", "Now reply with the single word ping."),),
        hold_open=1,
    ),
    "steer_resume": Scenario(
        "steer_resume",
        (
            "Start the shell command `sleep 240; echo built > build-done.txt` with the Bash tool "
            "as a background task (run_in_background), standing in for a long build. Do not wait "
            "for it. Then reply with one sentence saying what you started.",
            "Summarise what you did in your last turn in one sentence.",
        ),
        _std,
        "a resume with a stopped task's notification pending (the empty preamble result) and a "
        "steer written right after the prompt",
        steers=(Steer("start", _STEER_ECHO, 0.3),),
    ),
    "subagent_background": Scenario(
        "subagent_background",
        (
            "Start a sub-agent with the Agent tool in the background (run_in_background: true) "
            "that runs the shell command `python3 -c 'import time; time.sleep(60)'` with the "
            "Bash tool in the foreground (not in the background), and then replies with the "
            "single word done. Do not wait for it and do not check on it: reply at once with one "
            "short sentence saying that you started it.",
        ),
        _std,
        "a background sub-agent busy for a minute outlives the first result; stdin stays open "
        "for it, a steer 3 s after that result gets a loop of its own, then the sub-agent's "
        "notification starts the last loop (#660). Not `sleep 60`: 2.1.283's Bash tool refuses "
        "a standalone sleep in the foreground, and the sub-agent then ran it in the background",
        steers=(Steer("result#1", "What's the status? Reply in one short sentence.", 3.0),),
    ),
    "subagent_background_quick": Scenario(
        "subagent_background_quick",
        (
            "Start a sub-agent with the Agent tool in the background (run_in_background: true) "
            "that runs the shell command `sleep 45` with the Bash tool and then replies with the "
            "single word done. Do not wait for it and do not check on it: reply at once with one "
            "short sentence saying that you started it.",
        ),
        _std,
        "stream-json input, no steer: the sub-agent's foreground `sleep 45` is refused, so it "
        "runs it in the background and ends within seconds; its notification starts the last "
        "loop by itself, with stdin open, which closes at that loop's result (#660)",
        stream_input=True,
    ),
}


@dataclass(slots=True)
class Recording:
    records: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # steers planned and not written

    def raw(self, stream: str, line: str, t: float) -> None:
        if stream == "stdout":
            self.records.append({"kind": "stdout", "t": round(t, 3), "data": json.loads(line)})
        else:
            self.records.append({"kind": stream, "t": round(t, 3), "line": line})


def _git(cwd: str, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argument list
        [
            shutil.which("git") or "/usr/bin/git",
            "-c",
            "user.name=zab",
            "-c",
            "user.email=zab@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def make_scratch_repo() -> str:
    """A fresh git repository with a README, under the system temp directory."""
    path = tempfile.mkdtemp(prefix="zab-record-")
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
        f.write("# scratch\n\nA scratch repository used to record zab fixtures.\n")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "Initial commit")
    return path


def claude_version(binary: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argument list
        [binary, "--version"], check=True, capture_output=True, text=True, timeout=60
    ).stdout
    return out.strip().split()[0]


async def record_scenario(
    scenario: Scenario,
    *,
    config: ClaudeConfig,
    config_dir: str,
    model: str,
    cwd: str,
) -> tuple[Recording, list[str], str, int]:
    """Run the scenario's turns; return the last turn's recording, argv, prompt and exit code."""
    session_id = new_session_id()
    env = turn_env(config_dir, nested_ok=False)
    config = replace(config, steer=bool(scenario.steers) or scenario.stream_input)
    recording = Recording()
    argv: list[str] = []
    code = 0
    for i, prompt in enumerate(scenario.prompts):
        last = i == len(scenario.prompts) - 1
        argv = build_argv(config, scenario.settings(model), session_id=session_id, resume=i > 0)
        recording, code = await _record_turn(
            scenario,
            argv,
            env=env,
            cwd=cwd,
            prompt=prompt,
            steers=list(scenario.steers) if last else [],
            hold_open=scenario.hold_open if last else 0,
        )
    return recording, argv, scenario.prompts[-1], code


class _HeldInput(StreamInput):
    """The backend's `StreamInput`, but stdin stays open past the first `hold` results, so that
    a steer can be written just after one (`steer_after_result`)."""

    def __init__(self, prompt: str, hold: int) -> None:
        super().__init__(prompt)
        self.hold = hold

    def observe(self, obj: Mapping[str, Any]) -> None:
        if obj.get("type") == "result" and self.hold > 0:
            self.hold -= 1
            return
        super().observe(obj)


async def _record_turn(
    scenario: Scenario,
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    prompt: str,
    steers: list[Steer],
    hold_open: int,
) -> tuple[Recording, int]:
    recording = Recording()
    streamed = bool(scenario.steers) or scenario.stream_input
    stdin = _HeldInput(prompt, hold_open) if streamed else None
    if stdin is not None:
        recording.records.append(
            {"kind": "stdin", "t": 0.0, "uuid": stdin.prompt_uuid, "text": prompt}
        )
    holder: list[Any] = []
    pending: list[asyncio.Task[None]] = []
    results = 0

    async def write(steer: Steer) -> None:
        assert stdin is not None
        await asyncio.sleep(steer.delay)
        for _ in range(40):  # `start`: the prompt may still be on its way
            try:
                uid = await stdin.send(steer.text)
                break
            except SteerRejected:
                if stdin.closed:
                    # the trigger came, but stdin had closed by the time the line was due: the
                    # recording shows it, and the run reports it (`Recording.problems`)
                    t = round(time.monotonic() - (holder[0].started_at or 0.0), 3)
                    refused = {
                        "kind": "stdin-refused",
                        "t": t,
                        "after": steer.on,
                        "text": steer.text,
                    }
                    recording.records.append(refused)
                    recording.problems.append(
                        f"steer refused at {t:.2f} s ({steer.on} + {steer.delay:g} s): "
                        "stdin was already closed"
                    )
                    return
                await asyncio.sleep(0.05)
        else:
            recording.problems.append(f"steer not written: the prompt never went out ({steer.on})")
            return
        t = round(time.monotonic() - (holder[0].started_at or 0.0), 3)
        recording.records.append({"kind": "stdin", "t": t, "uuid": uid, "text": steer.text})

    def fire(trigger: str) -> None:
        for steer in [s for s in steers if s.on == trigger]:
            steers.remove(steer)
            pending.append(asyncio.create_task(write(steer)))

    async def on_object(obj: dict[str, Any]) -> None:
        nonlocal results
        names = [
            b.get("name")
            for b in (obj.get("message") or {}).get("content") or []
            if obj.get("type") == "assistant" and b.get("type") == "tool_use"
        ]
        for name in names:
            fire(f"tool_use:{name}")
        if stdin is not None and obj.get("isReplay") and obj.get("uuid") == stdin.prompt_uuid:
            fire("echo")
        if obj.get("type") == "result":
            results += 1
            fire(f"result#{results}")
        if (
            scenario.interrupt_after_tool is not None
            and not pending
            and (scenario.interrupt_after_tool in names)
        ):

            async def stop() -> None:
                await asyncio.sleep(1.0)
                await holder[0].interrupt()

            pending.append(asyncio.create_task(stop()))

    code, _stderr = await run_turn(
        argv,
        env=env,
        cwd=cwd,
        prompt=prompt,
        on_object=on_object,
        on_raw=recording.raw,
        on_start=lambda _turn: fire("start"),
        turn_holder=holder,
        stream_input=stdin,
    )
    for task in pending:
        await task
    recording.problems += [f"steer not written: {s.on} never came" for s in steers]
    t = max((r["t"] for r in recording.records), default=0.0)
    recording.records.append({"kind": "exit", "t": t, "code": code})
    return recording, code


def write_fixture(
    path: str, meta: dict[str, Any], recording: Recording, scrubber: Scrubber | None
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [json.dumps(meta, ensure_ascii=False, separators=(",", ":"))]
    lines += [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in recording.records]
    if scrubber is not None:
        lines = [scrubber.line(line) for line in lines]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _summary(recording: Recording) -> str:
    results = [
        r["data"]
        for r in recording.records
        if r.get("kind") == "stdout" and r["data"].get("type") == "result"
    ]
    result = results[-1] if results else None
    tools = [
        b.get("name")
        for r in recording.records
        if r.get("kind") == "stdout" and r["data"].get("type") == "assistant"
        for b in (r["data"].get("message") or {}).get("content") or []
        if b.get("type") == "tool_use"
    ]
    if result is None:
        return "no result event"
    text = str(result.get("result") or "").replace("\n", " ")[:80]
    return (
        f"results={len(results)} is_error={result.get('is_error')} "
        f"subtype={result.get('subtype')} "
        f"denials={len(result.get('permission_denials') or [])} tools={tools} result={text!r}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zab dev record-claude", description="Record claude -p stream-json fixtures."
    )
    parser.add_argument("--config-dir", required=True, help="CLAUDE_CONFIG_DIR of a test account")
    parser.add_argument("--binary", default="claude")
    parser.add_argument("--model", default="haiku", help="model alias for the recordings")
    parser.add_argument("--out", default="tests/fixtures/claude", help="fixture root")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(SCENARIOS),
        help="scenario to record (repeatable; default: all)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch repositories")
    args = parser.parse_args(argv)

    config_dir = os.path.abspath(os.path.expanduser(args.config_dir))
    if os.path.abspath(os.path.expanduser("~/.claude")) == config_dir and not os.environ.get(
        "ZAB_RECORD_ALLOW_MAIN"
    ):
        print("refusing to record against ~/.claude; use a test account", file=sys.stderr)
        return 2
    config = ClaudeConfig(binary=args.binary, accounts={"test": config_dir}, default_account="test")
    version = claude_version(args.binary)
    names = args.scenario or sorted(SCENARIOS)
    status = 0
    for name in names:
        scenario = SCENARIOS[name]
        cwd = make_scratch_repo()
        print(f"[{name}] {scenario.note}; cwd {cwd}", file=sys.stderr)
        recording, turn_argv, prompt, code = asyncio.run(
            record_scenario(
                scenario, config=config, config_dir=config_dir, model=args.model, cwd=cwd
            )
        )
        meta = {
            "kind": "meta",
            "fixture": 1,
            "version": version,
            "scenario": name,
            "argv": turn_argv,
            "prompt": prompt,
            "cwd": cwd,
            "recorded_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        }
        raw_path = os.path.join(args.out, version, "raw", f"{name}.jsonl")
        write_fixture(raw_path, meta, recording, None)
        scrubber = Scrubber(
            default_rules(home=os.path.expanduser("~"), cwd=cwd, config_dir=config_dir)
        )
        out_path = os.path.join(args.out, version, f"{name}.jsonl")
        write_fixture(out_path, meta, recording, scrubber)
        print(f"[{name}] exit {code}; {_summary(recording)}", file=sys.stderr)
        for problem in recording.problems:  # the scenario did not happen as planned
            print(f"[{name}] MISMATCH: {problem}", file=sys.stderr)
            status = 1
        print(f"[{name}] wrote {out_path} ({len(recording.records)} records)", file=sys.stderr)
        if not args.keep:
            subprocess.run(["rm", "-rf", "--", cwd], check=False)  # noqa: S603, S607
        if name == "error" and code == 0:
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
