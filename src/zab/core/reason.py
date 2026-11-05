"""What asked for a doctor check, for the backends' log lines (#483)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

#: What the current `Backend.doctor()` call is for (`!status`, `!vm`, `!login`, `zab doctor`):
#: the Claude backend names it when it starts `claude auth status`.
doctor_reason: ContextVar[str] = ContextVar("doctor_reason", default="a doctor check")


#: Whether the current `Backend.update_settings` call must refuse a value its backend can't
#: verify (Codex's model list unavailable), rather than accept it as it does for a command that
#: names the bot: a `!model` or `!effort` without a mention, which may be the other agent's
#: (SPEC §4.6, #713).
must_verify: ContextVar[bool] = ContextVar("must_verify", default=False)


@contextlib.contextmanager
def verified_only(on: bool = True) -> Iterator[None]:
    """Inside this block, `Backend.update_settings` refuses what it can't verify (`must_verify`)."""
    token = must_verify.set(on)
    try:
        yield
    finally:
        must_verify.reset(token)


@contextlib.contextmanager
def asked_by(reason: str) -> Iterator[None]:
    """Name what asks for the doctor checks run inside this block (`doctor_reason`)."""
    token = doctor_reason.set(reason)
    try:
        yield
    finally:
        doctor_reason.reset(token)
