"""Shared pieces of the workshop tools: a subprocess seam and output hygiene.

Every command runs as an argument list (CONTRIBUTING.md, rule 3; SPEC §10.6), never a shell string.
Tests inject a fake `Runner`; `subprocess_runner` is the real one.
"""

from __future__ import annotations

import re
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int  # -1 when the command timed out or could not be started
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Runner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float = 60.0,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> RunResult: ...


def subprocess_runner(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout_s: float = 60.0,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> RunResult:
    """Run an argument vector; a timeout or a missing program is reported, never raised."""
    try:
        proc = subprocess.run(  # noqa: S603 - argument list from our own code, no shell
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(-1, "", f"timed out after {timeout_s:g} s: {argv[0]}")
    except FileNotFoundError:
        return RunResult(-1, "", f"not found: {argv[0]}")
    except OSError as exc:
        return RunResult(-1, "", f"cannot run {argv[0]}: {exc}")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


_SECRET_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)


def redact(text: str) -> str:
    """The SPEC §11.6 patterns, so that no tool output can echo a token."""
    for pattern in _SECRET_RES:
        text = pattern.sub("[redacted]", text)
    return text


def first_line(text: str, limit: int = 200) -> str:
    """The first non-empty line of some output, redacted and shortened, for a detail field."""
    for line in text.splitlines():
        if line.strip():
            line = redact(line.strip())
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


_HOST_RE = re.compile(r"[^A-Za-z0-9-]+")


def short_host(hostname: Callable[[], str] = socket.gethostname) -> str:
    """The machine's short host name reduced to `[A-Za-z0-9-]` (used in key comments, branches)."""
    name = hostname().split(".")[0]
    return _HOST_RE.sub("-", name).strip("-") or "host"
