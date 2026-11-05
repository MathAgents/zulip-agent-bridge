#!/usr/bin/env python3
"""A scripted stand-in for the `claude` executable (SPEC §15.2).

It replays one recorded fixture (`tests/fixtures/claude/<version>/<scenario>.jsonl`, written by
`zab dev record-claude`) as if it were `claude -p --output-format stream-json --verbose`:
stdout objects, stderr lines and the exit code. Tests point `backends.claude.binary` at this
file, which is executable and uses only the standard library.

Environment:

- `ZAB_FAKE_CLAUDE_FIXTURE`: path of the fixture to replay (required, except for `--version`).
- `ZAB_FAKE_CLAUDE_VERSION`: what `--version` prints (default: the fixture's version, else the
  pinned one).
- `ZAB_FAKE_CLAUDE_REALTIME=1`: honour the recorded timing (capped at 2 s per gap); default: no
  delays.
- `ZAB_FAKE_CLAUDE_HANG=1`: emit everything except the `result` line, then wait for a signal;
  SIGINT or SIGTERM make it exit with code 130 without a result (the interrupt path of §9.6).
- `ZAB_FAKE_CLAUDE_HANG=graceful`: like `1`, but a signal makes it answer the way the real CLI
  does (the `interrupt` fixture): a `user` record "[Request interrupted by user]", then a
  `result` with `subtype: "error_during_execution"` and `is_error: true`, and exit code 0.
  In both modes the signal waits while a line is being written, and is handled once it is out
  (#548).
- `ZAB_FAKE_CLAUDE_LOG`: append one JSON line per invocation (argv, cwd, stdin, and under `env`
  the *names* of the `CLAUDE*`, `ANTHROPIC*` and `AWS_*` variables plus the values of
  `CLAUDE_CONFIG_DIR`, `ENABLE_CLAUDEAI_MCP_SERVERS` and git's four identity variables, never
  other values) so a test can assert on how it was called.
- `ZAB_FAKE_CLAUDE_GIT_COMMIT=1`: a turn first runs `git commit --allow-empty` in its cwd, as
  the Bash tool would, with the turn's environment (issue #214).
- `ZAB_FAKE_CLAUDE_LINGER=<seconds>`: keep running that long after the last record, with stdout
  open, before exiting (the real CLI exits a little after its `result` line).
- `ZAB_FAKE_CLAUDE_LOGGED_IN=0`: make `auth status` report `loggedIn: false` and exit 1, as the
  real CLI does (until `auth login` succeeds in that config directory).
- `ZAB_FAKE_CLAUDE_AUTH_DELAY=<seconds>`: `auth status` takes that long, as one that refreshes
  the OAuth token can (#450).
- `ZAB_FAKE_CLAUDE_AUTH_LOG=<path>`: `auth status` appends `{"auth_status": "start"}` there when
  it starts and `{"auth_status": "end"}` when it ends normally (not when killed), and each `-p`
  run's `ZAB_FAKE_CLAUDE_LOG` record gets `auth_running`: how many had started and not ended
  when it started (#307 item 1).
- `auth login` prints what the real CLI prints (2.1.282 to 2.1.289): the sign-in URL as an OSC 8
  link, then `Paste code here if prompted > `, and reads one line from the terminal. The code in
  `ZAB_FAKE_CLAUDE_LOGIN_CODE` (default `fake-code#fake-state`) succeeds and marks the config
  directory signed in; anything else fails like the real CLI (`Login failed: … 400`, exit 1).
  `ZAB_FAKE_CLAUDE_LOGIN=nourl` prints no URL and waits; `=hang` prints the URL and never reads;
  `=split` writes the URL in two pieces, cut inside it, half a second apart.
- `ZAB_FAKE_CLAUDE_REPORT_SESSION=<uuid>`: report that session id in every event (and write the
  transcript under it) instead of the one given in argv, the way the CLI does when it replaces
  a session (`SessionChanged`, SPEC §9.3).
- `ZAB_FAKE_CLAUDE_NO_TRANSCRIPT=1`: do not write the transcript side effect (below).
- `ZAB_FAKE_CLAUDE_SIDE_FIXTURE`: the fixture a side question (`!btw`: `--resume`,
  `--fork-session` and `--no-session-persistence` together, #716) replays instead, so that it
  can answer while a turn of the same session replays a held fixture.
- `ZAB_FAKE_CLAUDE_REFRESH_RACE=<n>`: the first n `-p` runs in a config directory (counted in
  `$CLAUDE_CONFIG_DIR/.fake-refresh-races`) lose the OAuth refresh race, as 2.1.283 reports
  it in `-p` mode (its bundle, DECISIONS): the fixture up to `system/init`, then an API-error
  `assistant` message (`error: "server_error"`, `is_api_error_message`) and a failed `result`
  with `REFRESH_RACE_TEXT`, exit 1. The prompt and the error still go to the transcript. Later
  runs replay the fixture.
- `--model` is resolved as the real CLI does (measured with 2.1.289, DECISIONS): an alias, in
  any case and with an optional `[1m]`, becomes a full name (`opus` → `claude-opus-5-5`); a
  `claude-…` name stays as it is; `system/init` then reports that model, and
  `per_turn_effort_active` false for haiku only. Any other name, or one listed in
  `ZAB_FAKE_CLAUDE_MISSING_MODELS` (comma-separated), replays the `error` fixture next to the
  given one (the recorded answer to a missing model) under that name instead. Without
  `--model` the fixture's own `system/init` is kept.

Like the real CLI, after a replay without `--no-session-persistence` it writes a minimal
transcript to
`$CLAUDE_CONFIG_DIR/projects/<cwd with / and . as ->/<session id>.jsonl` (the prompt and the
result text, in the record shapes `list_sessions` and `open_session` read, §9.8), appending
on `--resume`.

The `session_id` of every replayed event is rewritten to the id given with `--session-id` or
`--resume`, so a fixture serves any session. The prompt is read from stdin to EOF, like the
real CLI.

With `--input-format stream-json` (steering, PM on issue #36, Fri Oct 9) stdin is read line by
line, as the real CLI does: the first line is the prompt. Each later `stdin` record of the
fixture (the `steer_*` scenarios) waits there for the next line, and the recorded uuid of each
line becomes the one received, in the echoes (`isReplay`) and the results'
`user_message_uuids`. EOF at such a point ends the replay with exit 0: the real CLI, with
nothing left to run, exits. After the last record the fake waits for EOF, like the real CLI. A
fixture without `stdin` records gets an echo of the prompt before its first `assistant` record
under `--replay-user-messages`. `ZAB_FAKE_CLAUDE_LOG` then also gets `{"steer": <line>}` for
each later line.
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any

PINNED_VERSION = "2.1.283"


def _session_from_argv(argv: list[str]) -> str | None:
    for flag in ("--session-id", "--resume"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
    return None


def _rewrite(obj: Any, session_id: str | None, uuids: dict[str, str] | None = None) -> Any:
    if session_id and isinstance(obj, dict) and isinstance(obj.get("session_id"), str):
        obj = dict(obj)
        obj["session_id"] = session_id
    if uuids and isinstance(obj, dict):  # stream-json input: the uuids of the lines received
        obj = dict(obj)
        if obj.get("isReplay") and obj.get("uuid") in uuids:
            obj["uuid"] = uuids[obj["uuid"]]
        if obj.get("user_message_uuid") in uuids:
            obj["user_message_uuid"] = uuids[obj["user_message_uuid"]]
        if isinstance(obj.get("user_message_uuids"), list):
            obj["user_message_uuids"] = [uuids.get(u, u) for u in obj["user_message_uuids"]]
    return obj


def _read_line() -> dict[str, Any] | None:
    """The next stream-json input line; None at EOF (or for a line that is not an object)."""
    raw = sys.stdin.buffer.readline()
    if not raw:
        return None
    try:
        line = json.loads(raw)
    except ValueError:
        return None
    return line if isinstance(line, dict) else None


def _line_text(line: dict[str, Any] | None) -> str:
    content = ((line or {}).get("message") or {}).get("content")
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return str(content or "")


def _with_prompt_echo(
    records: list[dict[str, Any]], prompt: dict[str, Any]
) -> list[dict[str, Any]]:
    """An old fixture under `--replay-user-messages`: the prompt's echo, as 2.1.283 sends it, just
    before the first `assistant` record (else before the last record)."""
    echo = {
        "kind": "stdout",
        "data": {
            "type": "user",
            "message": {"role": "user", "content": _line_text(prompt)},
            "session_id": "",
            "parent_tool_use_id": None,
            "uuid": prompt.get("uuid") or "",
            "isReplay": True,
        },
    }
    kinds = [
        (r.get("data") or {}).get("type") if r.get("kind") == "stdout" else None for r in records
    ]
    at = kinds.index("assistant") if "assistant" in kinds else None
    if at is None:  # no model output: before the last result
        at = max((i for i, k in enumerate(kinds) if k == "result"), default=len(records))
    if at < len(records):
        echo["t"] = records[at].get("t")
    return [*records[:at], echo, *records[at:]]


FAKE_MODELS = {
    "default": "claude-opus-5-5",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5-1",
    "best": "claude-fable-5-1",
    "opusplan": "claude-sonnet-5-5",
}
_RECORDED_MISSING = "no-such-model-zab"  # the model of the `error` fixture


def _resolve_model(name: str) -> str | None:
    """The full name the CLI would report for `--model name`; None for a missing model."""
    base, suffix = (name[:-4], name[-4:]) if name.lower().endswith("[1m]") else (name, "")
    if base.lower() in FAKE_MODELS:
        return FAKE_MODELS[base.lower()] + suffix
    missing = {m for m in os.environ.get("ZAB_FAKE_CLAUDE_MISSING_MODELS", "").split(",") if m}
    return name if base.startswith("claude-") and name not in missing else None


def _with_model(records: list[dict[str, Any]], fixture: str, model: str) -> list[dict[str, Any]]:
    """`records` as the CLI would answer them under `--model model` (see the module doc)."""
    resolved = _resolve_model(model)
    if resolved is None:
        error = os.path.join(os.path.dirname(fixture), "error.jsonl")
        text = json.dumps(load_fixture(error)).replace(_RECORDED_MISSING, model.replace('"', ""))
        return json.loads(text)
    out = []
    for rec in records:
        data = rec.get("data")
        if rec.get("kind") == "stdout" and isinstance(data, dict) and data.get("subtype") == "init":
            data = {**data, "model": resolved, "per_turn_effort_active": "haiku" not in resolved}
            rec = {**rec, "data": data}
        out.append(rec)
    return out


#: Claude Code's `-p` text for a lost OAuth refresh race (2.1.251 to 2.1.299, DECISIONS).
REFRESH_RACE_TEXT = (
    "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited "
    "mid-refresh. This is usually transient; retry in a minute, and if it persists close other "
    "Claude Code processes or sign in again"
)


def _refresh_race_due() -> bool:
    """Whether this run loses the refresh race (`ZAB_FAKE_CLAUDE_REFRESH_RACE`); counts it."""
    try:
        races = int(os.environ.get("ZAB_FAKE_CLAUDE_REFRESH_RACE") or 0)
    except ValueError:
        return False
    if races <= 0:
        return False
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.path.join(config_dir, ".fake-refresh-races")
    try:
        with open(path, encoding="utf-8") as f:
            lost = int(f.read().strip() or 0)
    except (OSError, ValueError):
        lost = 0
    if lost >= races:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{lost + 1}\n")
    return True


def _with_refresh_race(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`records` up to `system/init`, then the CLI's answer to a lost refresh race."""
    inits = [
        i
        for i, r in enumerate(records)
        if r.get("kind") == "stdout" and (r.get("data") or {}).get("subtype") == "init"
    ]
    head = records[: inits[0] + 1] if inits else [r for r in records if r.get("kind") == "meta"]
    return [*head, *refresh_race_records(float(head[-1].get("t") or 0.0) if head else 0.0)]


def refresh_race_records(t: float) -> list[dict[str, Any]]:
    """The CLI's lost refresh race at time `t`: the API-error message, the result, exit 1."""
    error = {
        "type": "assistant",
        "message": {
            "id": "fake-refresh-race",
            "model": "<synthetic>",
            "role": "assistant",
            "type": "message",
            "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": REFRESH_RACE_TEXT}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
        "parent_tool_use_id": None,
        "session_id": "",
        "error": "server_error",
        "is_api_error_message": True,
    }
    result = {
        "type": "result",
        "subtype": "success",  # sic, as for every API error (the `error` fixture)
        "is_error": True,
        "num_turns": 1,
        "result": REFRESH_RACE_TEXT,
        "terminal_reason": "api_error",
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "modelUsage": {},
        "permission_denials": [],
        "session_id": "",
    }
    return [
        {"kind": "stdout", "t": t, "data": error},
        {"kind": "stdout", "t": t, "data": result},
        {"kind": "exit", "t": t, "code": 1},
    ]


#: The signals that stop a hanging replay. They are held while a line is written: their handler
#: writes too, and run inside a write's flush it would fail as a reentrant call (#548).
STOP_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})


def _put(stream: Any, text: str) -> None:
    """Write `text` and flush it, with `STOP_SIGNALS` held until it is out."""
    held = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        stream.write(text)
        stream.flush()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)


def load_fixture(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


LOGIN_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=fake-client&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=fake-state"
)


def _auth_log(event: str) -> None:
    """One `auth status` event in `ZAB_FAKE_CLAUDE_AUTH_LOG`, if set."""
    path = os.environ.get("ZAB_FAKE_CLAUDE_AUTH_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"auth_status": event}) + "\n")


def _auth_running() -> dict[str, int]:
    """`{"auth_running": n}`: the `auth status` runs started and not ended, with
    `ZAB_FAKE_CLAUDE_AUTH_LOG`; else nothing."""
    path = os.environ.get("ZAB_FAKE_CLAUDE_AUTH_LOG")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            events = [json.loads(line).get("auth_status") for line in f if line.strip()]
    except OSError:
        events = []
    return {"auth_running": events.count("start") - events.count("end")}


def _login_marker() -> str:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(config_dir, ".fake-logged-in")


def _auth_login(args: list[str]) -> int:
    """`claude auth login` as the real CLI does it in a terminal (see the module docstring)."""
    log_path = os.environ.get("ZAB_FAKE_CLAUDE_LOG")
    if log_path:  # names only, and never what is typed at the prompt
        env = {k: "<set>" for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC", "AWS_"))}
        env["CLAUDE_CONFIG_DIR"] = os.environ.get("CLAUDE_CONFIG_DIR", "<unset>")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "env": env}) + "\n")
    mode = os.environ.get("ZAB_FAKE_CLAUDE_LOGIN", "")
    out = sys.stdout
    out.write("Opening browser to sign in\u2026\r\n")
    if mode == "nourl":
        out.flush()
        signal.pause()
    text = (
        "If the browser didn't open, visit: "
        f"\x1b]8;;{LOGIN_URL}\x07\x1b[94m{LOGIN_URL}\x1b[39m\x1b]8;;\x07\r\n"
        "Paste code here if prompted > "
    )
    if mode == "split":  # two writes, cut inside the URL, as a terminal read may see them
        cut = text.index("client_id=") + len("client_id=")
        out.write(text[:cut])
        out.flush()
        time.sleep(0.5)
        text = text[cut:]
    out.write(text)
    out.flush()
    if mode == "hang":
        signal.pause()
    code = sys.stdin.readline().strip()
    if code == os.environ.get("ZAB_FAKE_CLAUDE_LOGIN_CODE", "fake-code#fake-state"):
        with open(_login_marker(), "w", encoding="utf-8") as f:
            f.write("signed in\n")
        out.write("Login successful.\r\n")
        out.flush()
        return 0
    out.write("Login failed: Request failed with status code 400\r\n\x1b[?25h")
    out.flush()
    return 1


def _side_question(argv: list[str]) -> bool:
    """`--resume <id> --fork-session --no-session-persistence`: a side question (`!btw`)."""
    return {"--resume", "--fork-session", "--no-session-persistence"} <= set(argv)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    fixture_path = os.environ.get("ZAB_FAKE_CLAUDE_FIXTURE")
    if _side_question(args) and os.environ.get("ZAB_FAKE_CLAUDE_SIDE_FIXTURE"):
        fixture_path = os.environ["ZAB_FAKE_CLAUDE_SIDE_FIXTURE"]
    records = load_fixture(fixture_path) if fixture_path else []
    meta = next((r for r in records if r.get("kind") == "meta"), {})
    if args and args[0] in ("--version", "-v", "version"):
        version = os.environ.get("ZAB_FAKE_CLAUDE_VERSION") or meta.get("version") or PINNED_VERSION
        print(f"{version} (Claude Code)")
        return 0
    if args[:2] == ["auth", "login"]:
        return _auth_login(args)
    if args[:2] == ["auth", "status"]:
        _auth_log("start")
        time.sleep(float(os.environ.get("ZAB_FAKE_CLAUDE_AUTH_DELAY") or 0))
        logged_in = os.environ.get("ZAB_FAKE_CLAUDE_LOGGED_IN", "1") != "0" or os.path.exists(
            _login_marker()
        )
        status = {  # decoy identity fields: the doctor must never show them
            "loggedIn": logged_in,
            "authMethod": "claude.ai" if logged_in else None,
            "apiProvider": "firstParty",
            "configDirectory": os.environ.get("CLAUDE_CONFIG_DIR", ""),
            "email": "decoy-account@example.com",
            "orgId": "00000000-0000-4000-8000-0000000decoy",
            "orgName": "Decoy Org",
            "subscriptionType": "max" if logged_in else None,
        }
        print(json.dumps(status, indent=2))
        _auth_log("end")
        return 0 if logged_in else 1  # like the real CLI (2.1.283 to 2.1.289)
    if not fixture_path:
        print("fake_claude: set ZAB_FAKE_CLAUDE_FIXTURE to a fixture path", file=sys.stderr)
        return 2
    if "--model" in args and args.index("--model") + 1 < len(args):
        records = _with_model(records, fixture_path, args[args.index("--model") + 1])
    if _refresh_race_due():
        records = _with_refresh_race(records)
    i = args.index("--input-format") if "--input-format" in args else -1
    stream = i >= 0 and args[i + 1 : i + 2] == ["stream-json"]
    first: dict[str, Any] | None = None
    uuids: dict[str, str] = {}  # recorded uuid -> the uuid of the line received
    inputs = [r for r in records if r.get("kind") == "stdin"]
    if stream:
        first = _read_line()
        prompt = _line_text(first)
        if inputs and first and first.get("uuid"):
            uuids[str(inputs[0].get("uuid"))] = str(first["uuid"])
        elif not inputs and first and "--replay-user-messages" in args:
            records = _with_prompt_echo(records, first)
    else:
        prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    log_path = os.environ.get("ZAB_FAKE_CLAUDE_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "argv": args,
                        "cwd": os.getcwd(),
                        "stdin": prompt,
                        **({"stdin_line": first} if stream else {}),
                        **_auth_running(),
                        "env": {
                            "names": sorted(
                                k
                                for k in os.environ
                                if k.startswith(("CLAUDE", "ANTHROPIC", "AWS_"))
                            ),
                            "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR"),
                            "ENABLE_CLAUDEAI_MCP_SERVERS": os.environ.get(
                                "ENABLE_CLAUDEAI_MCP_SERVERS"
                            ),
                            **{
                                k: os.environ.get(k)
                                for k in (
                                    "GIT_AUTHOR_NAME",
                                    "GIT_AUTHOR_EMAIL",
                                    "GIT_COMMITTER_NAME",
                                    "GIT_COMMITTER_EMAIL",
                                    "ZAB_BOT",
                                )
                            },
                        },
                    }
                )
                + "\n"
            )
    if os.environ.get("ZAB_FAKE_CLAUDE_GIT_COMMIT") == "1":
        # what the Bash tool's `git commit` would record (issue #214)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "fake agent commit"],  # noqa: S607
            check=True,
        )
    session_id = os.environ.get("ZAB_FAKE_CLAUDE_REPORT_SESSION") or _session_from_argv(args)
    realtime = os.environ.get("ZAB_FAKE_CLAUDE_REALTIME") == "1"
    hang_mode = os.environ.get("ZAB_FAKE_CLAUDE_HANG", "")
    hang = hang_mode in ("1", "graceful")

    def _on_signal(signum: int, _frame: Any) -> None:
        if hang_mode == "graceful":
            # What Claude Code 2.1.283 emits when its group gets SIGINT mid-turn (DECISIONS §9.6)
            answer = (
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "[Request interrupted by user]"}],
                    },
                    "parent_tool_use_id": None,
                    "session_id": session_id or "",
                },
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "num_turns": 1,
                    "result": None,
                    "permission_denials": [],
                    "terminal_reason": "aborted_tools",
                    "session_id": session_id or "",
                },
            )
            _put(sys.stdout, "".join(json.dumps(d, separators=(",", ":")) + "\n" for d in answer))
            raise SystemExit(0)
        sys.stdout.flush()
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    def steer_line() -> dict[str, Any] | None:
        line = _read_line()
        if line is not None and log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"steer": line}) + "\n")
        return line

    last_t = 0.0
    exit_code = 0
    result_text: str | None = None
    for rec in records:
        kind = rec.get("kind")
        if kind == "meta" or (kind == "stdin" and inputs and rec is inputs[0]):
            continue
        if kind == "stdin":
            if not stream:
                continue
            line = steer_line()  # where the recording wrote its next line
            if line is None:  # stdin closed: with nothing left to run, the CLI exits
                exit_code = 0
                break
            uuids[str(rec.get("uuid"))] = str(line.get("uuid") or "")
            continue
        if realtime:
            t = float(rec.get("t") or 0.0)
            gap = min(max(t - last_t, 0.0), 2.0)
            if gap:
                time.sleep(gap)
            last_t = t
        if kind == "stdout":
            data = rec.get("data")
            if hang and isinstance(data, dict) and data.get("type") == "result":
                while True:  # until SIGINT/SIGTERM (handled above) or SIGKILL
                    time.sleep(3600)
            if isinstance(data, dict) and data.get("type") == "result":
                result_text = data.get("result")
            data = _rewrite(data, session_id, uuids)
            _put(sys.stdout, json.dumps(data, separators=(",", ":")) + "\n")
        elif kind == "stdout-text":
            _put(sys.stdout, str(rec.get("line", "")) + "\n")
        elif kind == "stderr":
            _put(sys.stderr, str(rec.get("line", "")) + "\n")
        elif kind == "exit":
            exit_code = int(rec.get("code") or 0)
    else:
        while stream and steer_line() is not None:  # the real CLI waits for EOF
            pass
    if "--no-session-persistence" not in args:  # like the real CLI: nothing saved (#716)
        _write_transcript(session_id, prompt, result_text, fork_from=_fork_source(args))
    linger = float(os.environ.get("ZAB_FAKE_CLAUDE_LINGER") or 0)
    if linger > 0:
        time.sleep(linger)
    return exit_code


def _fork_source(argv: list[str]) -> str | None:
    """`--resume <id> --fork-session`: the session whose history the new one starts from."""
    if "--fork-session" in argv and "--resume" in argv:
        i = argv.index("--resume")
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


def _write_transcript(
    session_id: str | None, prompt: str, result_text: str | None, *, fork_from: str | None = None
) -> None:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir or not session_id or os.environ.get("ZAB_FAKE_CLAUDE_NO_TRANSCRIPT") == "1":
        return
    history: list[dict[str, Any]] = []
    if fork_from:
        # like the real CLI (2.1.283, DECISIONS): the source is found under any project
        # directory, copied with the new session id and cwd, and left unchanged itself
        for source in glob.glob(os.path.join(config_dir, "projects", "*", f"{fork_from}.jsonl")):
            with open(source, encoding="utf-8") as f:
                history = [
                    {**json.loads(line), "sessionId": session_id, "cwd": os.getcwd()}
                    for line in f
                    if line.strip()
                ]
            break
    # Claude Code 2.1.283 turns every non-alphanumeric character into "-" (DECISIONS, §9.8).
    directory = os.path.join(config_dir, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        print(f"fake_claude: transcript dir not created: {exc}", file=sys.stderr)
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    lines = [
        *history,
        {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "sessionId": session_id,
            "cwd": os.getcwd(),
            "timestamp": now,
        },
    ]
    if isinstance(result_text, str) and result_text:
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": result_text}],
                },
                "sessionId": session_id,
                "cwd": os.getcwd(),
                "timestamp": now,
            }
        )
    try:
        with open(os.path.join(directory, f"{session_id}.jsonl"), "a", encoding="utf-8") as f:
            for rec in lines:
                f.write(json.dumps(rec) + "\n")
    except OSError as exc:  # an unwritable config dir is not the replay's problem
        print(f"fake_claude: transcript not written: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
