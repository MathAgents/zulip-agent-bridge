"""Lazy command dispatcher; command modules own their arguments (SPEC §13.1)."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence

from zab import __version__

COMMANDS = {
    "init": "zab.commands.init",
    "doctor": "zab.commands.doctor",
    "run": "zab.commands.run",
    "status": "zab.commands.status",
    "service": "zab.commands.service",
    "setup-repo": "zab.tools.setup_repo",
    "preflight": "zab.tools.preflight",
    "sentinel": "zab.tools.sentinel",
    "history": "zab.tools.history",
    "share": "zab.tools.share",
}
DEV_COMMANDS = {
    "claude-turn": "zab.dev.claude_turn",
    "record-claude": "zab.dev.record_claude",
    "record-codex": "zab.dev.record_codex",
    "scrub": "zab.dev.scrub",
}


def _dispatch(module_name: str, argv: list[str]) -> int:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Only an absent hook is optional. A missing dependency inside an installed
        # hook is a real error and must retain its traceback.
        if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
            print(f"zab: {module_name} is not installed yet", file=sys.stderr)
            return 2
        raise
    return module.main(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to a synchronous ``main(argv: list[str]) -> int`` hook.

    Hooks parse their own flags (including --help), may use asyncio.run(), and
    return a process exit code. No optional backend is imported for root help.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="zab", description="Zulip Agent Bridge")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", choices=[*COMMANDS, "dev", "version"], nargs="?")
    # Parse only the command so child --help and all remaining tokens are untouched.
    parsed = parser.parse_args(args[:1])
    if parsed.command is None:
        parser.print_help()
        return 0
    if parsed.command == "version":
        if len(args) != 1:
            parser.error("version takes no arguments")
        print(__version__)
        return 0
    if parsed.command == "dev":
        dev = argparse.ArgumentParser(prog="zab dev", description="Developer tools")
        dev.add_argument("command", choices=DEV_COMMANDS, nargs="?")
        sub = dev.parse_args(args[1:2])
        if sub.command is None:
            dev.print_help()
            return 0
        return _dispatch(DEV_COMMANDS[sub.command], args[2:])
    return _dispatch(COMMANDS[parsed.command], args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
