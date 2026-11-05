"""A scripted `Runner` for the workshop tools' tests."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

from zab.tools.common import RunResult

Rule = Callable[[list[str]], RunResult | None]


class FakeRunner:
    """Answers argv by the first matching rule; records every call.

    A rule is a callable returning a `RunResult` or None (not mine). `ok(prefix, stdout)` and
    `fail(prefix, stderr, code)` build rules keyed on a leading subsequence of argv.
    """

    def __init__(self, *rules: Rule, default: RunResult | None = None) -> None:
        self.rules = list(rules)
        self.default = default or RunResult(-1, "", "not found: (fake)")
        self.calls: list[dict] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float = 60.0,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> RunResult:
        self.calls.append(
            {"argv": list(argv), "cwd": cwd, "timeout_s": timeout_s, "env": dict(env or {})}
        )
        for rule in self.rules:
            res = rule(argv)
            if res is not None:
                return res
        return self.default

    def argvs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls]


def _prefix(prefix: list[str]) -> Callable[[list[str]], bool]:
    return lambda argv: argv[: len(prefix)] == prefix


def ok(prefix: list[str], stdout: str = "", side_effect: Callable[[list[str]], None] | None = None):
    match = _prefix(prefix)

    def rule(argv: list[str]) -> RunResult | None:
        if not match(argv):
            return None
        if side_effect:
            side_effect(argv)
        return RunResult(0, stdout, "")

    return rule


def fail(prefix: list[str], stderr: str = "boom", code: int = 1):
    match = _prefix(prefix)
    return lambda argv: RunResult(code, "", stderr) if match(argv) else None


def contains(needle: str, result: RunResult):
    return lambda argv: result if any(needle in a for a in argv) else None


def ran_tool(prefix: list[str], before: str = "", after: str = ""):
    """An agent that really runs the smoke prompt's `cat <file>` and replies with the output."""
    match = _prefix(prefix)

    def rule(argv: list[str]) -> RunResult | None:
        if not match(argv):
            return None
        found = re.search(r"cat (\S+)", argv[-1])
        output = Path(found.group(1)).read_text().strip() if found else ""
        return RunResult(0, before + output + after + "\n", "")

    return rule
