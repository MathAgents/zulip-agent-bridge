"""`zab dev claude-turn`: run one headless Claude turn and print the contract events (SPEC §13.1).

The live driver for the Claude backend: it exercises `build_argv`, `turn_env`, `ClaudeTurn` and
`StreamParser` against the real CLI (or `testing/fake_claude.py` via `--binary`). Use a test
account's `--config-dir`, never the account of the session you are working in.

    python -m zab.dev.claude_turn --config-dir ~/.claude2 --model haiku "Reply with pong."
    python -m zab.dev.claude_turn --config-dir ~/.claude2 --resume <uuid> -   # prompt on stdin

Exit code: 0 when the turn completed, 1 when it failed, 130 when interrupted with Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import signal
import sys
from typing import Any

from zab.backends.claude import ClaudeConfig, build_argv, new_session_id, new_turn_id, turn_env
from zab.backends.claude_stream import StreamParser, run_parsed_turn
from zab.core.types import (
    BackendEvent,
    ItemCompleted,
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

_MARK = {ItemStatus.COMPLETED: "✅", ItemStatus.FAILED: "❌", ItemStatus.DECLINED: "🚫"}
_PLAN = {"pending": "☐", "in_progress": "◐", "completed": "☑"}


def format_event(ev: BackendEvent) -> str:
    """One human-readable line per event, in the spirit of the status message (Appendix B.2)."""
    if isinstance(ev, TurnStarted):
        return f"▶ turn {ev.turn_id} (pgid {ev.pgid})"
    if isinstance(ev, ItemStarted):
        return f"⏳ {ev.item.kind}: {ev.item.title}"
    if isinstance(ev, ItemCompleted):
        mark = _MARK.get(ev.item.status, "•")
        extra = ""
        if ev.item.duration_ms is not None:
            extra = f" ({ev.item.duration_ms / 1000:.1f} s)"
        if ev.item.kind == "agent_message":
            return f"💬 {ev.item.title}"
        return f"{mark} {ev.item.kind}: {ev.item.title}{extra}"
    if isinstance(ev, PlanUpdated):
        return "Plan: " + " · ".join(f"{_PLAN.get(s, '?')} {t}" for t, s in ev.steps)
    if isinstance(ev, SessionChanged):
        return f"⚠ session id changed: {ev.session_id} → {ev.new_session_id}"
    if isinstance(ev, Notice):
        return f"⚠ {ev.level}: {ev.message}"
    if isinstance(ev, TurnCompleted):
        head = {
            TurnStatus.COMPLETED: "✅ Done",
            TurnStatus.FAILED: f"❌ Failed: {ev.error.kind if ev.error else 'unknown'}",
            TurnStatus.INTERRUPTED: "⏹ Stopped",
        }[ev.status]
        usage = ""
        if ev.usage and ev.usage.total_tokens is not None:
            usage = f" · {ev.usage.total_tokens} tokens"
            if ev.usage.cost_usd is not None:
                usage += f" · ${ev.usage.cost_usd:.4f}"
        err = f"\n   {ev.error.message}" if ev.error else ""
        return f"{head}{usage}{err}"
    return f"{type(ev).__name__}: {ev}"


def _event_json(ev: BackendEvent) -> str:
    return json.dumps({"event": type(ev).__name__, **dataclasses.asdict(ev)}, ensure_ascii=False)


async def _run(
    args: argparse.Namespace, prompt: str, config_dir: str, context: str, cwd: str
) -> int:
    config = ClaudeConfig(binary=args.binary, accounts={"test": config_dir}, default_account="test")
    if args.allowed_tools:
        config.allowed_tools = args.allowed_tools
    if args.disallowed_tools:
        config.disallowed_tools = args.disallowed_tools
    settings = SessionSettings(model=args.model, effort=args.effort, approval=args.permission_mode)
    session_id = args.resume or args.session_id or new_session_id()
    argv = build_argv(
        config, settings, session_id=session_id, resume=bool(args.resume), context_prompt=context
    )
    env = turn_env(config_dir, nested_ok=False)
    parser = StreamParser(session_id, new_turn_id(session_id), cwd=cwd)
    out = sys.stdout

    def on_event(ev: BackendEvent) -> None:
        print(_event_json(ev) if args.json else format_event(ev), file=out, flush=True)

    def on_raw(stream: str, line: str, t: float) -> None:
        if args.raw:
            print(f"[{t:7.3f} {stream}] {line}", file=sys.stderr, flush=True)

    holder: list[Any] = []
    tasks: list[asyncio.Task[None]] = []
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    interrupted = False

    def _sigint() -> None:
        nonlocal interrupted
        interrupted = True
        if holder:
            tasks.append(loop.create_task(holder[0].interrupt()))
        elif main_task is not None:
            main_task.cancel()  # Ctrl-C before the process exists: run_turn cleans up (§9.6)

    loop.add_signal_handler(signal.SIGINT, _sigint)
    print(f"session {session_id} · {' '.join(argv[:1])} … · cwd {cwd}", file=sys.stderr)
    try:
        info, code = await run_parsed_turn(
            argv,
            env=env,
            cwd=cwd,
            prompt=prompt,
            parser=parser,
            on_event=on_event,
            on_raw=on_raw,
            turn_holder=holder,
        )
    finally:
        # The interrupt keeps escalating after `claude` itself has exited, until every process
        # of the group is gone (§9.6); it must finish before the loop closes.
        loop.remove_signal_handler(signal.SIGINT)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if not args.json:
        print("", file=out)
        print(info.final_text or "(no final message)", file=out)
        print(
            f"\n— exit {code} · model {info.model} · mode {info.permission_mode} "
            f"· resume: CLAUDE_CONFIG_DIR={config_dir} claude --resume {info.session_id}",
            file=sys.stderr,
        )
    if interrupted:
        return 130
    return 0 if info.status is TurnStatus.COMPLETED else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zab dev claude-turn", description="Run one headless Claude turn; print events."
    )
    parser.add_argument("prompt", help="the turn's text, or - to read it from stdin")
    parser.add_argument("--config-dir", required=True, help="CLAUDE_CONFIG_DIR of a test account")
    parser.add_argument("--binary", default="claude")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--permission-mode", default="bypassPermissions")
    parser.add_argument("--allowed-tools", nargs="*")
    parser.add_argument("--disallowed-tools", nargs="*")
    parser.add_argument("--context-prompt-file", help="file passed as --append-system-prompt")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--session-id", help="UUID for a new session (default: random)")
    group.add_argument("--resume", help="UUID of an existing session to continue")
    parser.add_argument("--json", action="store_true", help="print events as JSON lines")
    parser.add_argument("--raw", action="store_true", help="echo raw stdout/stderr to stderr")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        parser.error("empty prompt")
    config_dir = os.path.abspath(os.path.expanduser(args.config_dir))
    if os.sep in args.binary:  # a path: resolve it here, the child runs in another cwd
        args.binary = os.path.abspath(args.binary)
    context = ""
    if args.context_prompt_file:
        with open(args.context_prompt_file, encoding="utf-8") as f:
            context = f.read()
    try:
        return asyncio.run(_run(args, prompt, config_dir, context, os.path.abspath(args.cwd)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
