"""The rigs' secrets files, read without ever quoting them (#519).

An exception's message can quote the file it came from: configparser's quotes the offending
line (`MissingSectionHeaderError`, `ParsingError`), so a malformed zuliprc's key would reach a
run problem, a report row or `pytest.log`, and `summary.md` is posted on PRs. The runners and
the harness read every secrets file through `read_secret`: the admin rig's directory
(`~/.config/zab/admin-e2e/`: `secrets/`, `provisioner.zuliprc`, `github-token`, A18's laptop),
the test users' zuliprcs and the bots' (`~/.config/zab/`). A failure becomes `SecretFileError`,
which says the file's label and the exception's type, never its message. The original is not
chained (`from None`): pytest prints a chained exception's message, and the arguments of its
innermost frame, which in configparser's case are the offending line itself.

No frame left in the traceback holds a secret either, which pytest's `-l` would print with its
locals (reviewer on #522): a function that holds a file's text, or values read from several
files, runs below `read_secret`, whose raise cuts it off. A SecretFileError from below is
raised afresh too, for that reason.

`described` formats an exception for a run problem: a configparser or decoding error that
`read_secret` did not wrap shows its type alone.
"""

from __future__ import annotations

import configparser
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from zab.zulip.client import error_kind

T = TypeVar("T")


class SecretFileError(Exception):
    """A secrets file that could not be read or parsed: its `label` and the exception's type
    (`kind`) only."""

    def __init__(self, label: str, kind: str) -> None:
        super().__init__(f"{label}: {kind}")
        self.label, self.kind = label, kind


def label_of(path: Path, base: Path | None = None) -> str:
    """`path` relative to `base` (the rig's directory; by default `~/.config/zab`, where the
    rigs' inputs live), else the whole path. Never anything read from the file."""
    base = Path.home() / ".config" / "zab" if base is None else base
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def read_secret(label: str, read: Callable[..., T], *args: Any) -> T:
    """`read(*args)`, which reads or parses the secrets file `label`; any failure is a
    SecretFileError, unchained: a SecretFileError from a read below keeps its label and type,
    but not its traceback, whose frames may hold secrets (`read`'s, for one). A malformed
    zuliprc's ZuliprcError (#527) gives the parse error's type, as configparser's did."""
    try:
        return read(*args)
    except SecretFileError as exc:
        raise SecretFileError(exc.label, exc.kind) from None
    except Exception as exc:
        raise SecretFileError(label, error_kind(exc)) from None


def described(exc: BaseException) -> str:
    """`Type: message`, for a run problem; the type alone when the message is empty
    (`CancelledError`, not `CancelledError:`) or may quote a secrets file: a configparser
    error quotes the offending line, a decoding error a byte of it."""
    message = str(exc)
    if not message or isinstance(exc, configparser.Error | UnicodeError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"
