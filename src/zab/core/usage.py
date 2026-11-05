"""Usage tables with honest unknown values and accessible bars (SPEC §4.6); a limit whose
reset time has passed shows no stale figure (issue #647)."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from zab.core import types as t
from zab.core.presentation import code
from zab.render.bars import bar
from zab.render.markdown import redact_secrets
from zab.render.mentions import disarm_mentions

if TYPE_CHECKING:
    from zab.core.session import SessionActor


def stamp(value: float | None) -> str:
    if value is None:
        return "unknown time"
    try:
        return "<time:" + datetime.fromtimestamp(value, UTC).isoformat() + ">"
    except (TypeError, ValueError, OverflowError, OSError):  # TypeError: an odd value in a file
        return "unknown time"


def number(value: int) -> str:
    for scale, suffix in ((1_000_000, "M"), (1_000, "k")):
        if value >= scale:
            return f"{value / scale:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def cell(text: str) -> str:
    return (
        disarm_mentions(redact_secrets(text))
        .replace("|", r"\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_usage(usage: t.Usage, backend: str, machine: str, now: float | None = None) -> str:
    """The `!usage` card. A window whose reset time is at or before `now` has reset since it
    was reported, so it shows no figure at all (issue #647); the limits say when they were
    reported, apart from the session's own time."""
    now = time.time() if now is None else now
    lines = [f"**Usage** · {code(backend.capitalize())} · {code(machine)}"]
    missing = list(usage.unavailable)
    if usage.windows:
        # Claude reports limits only during a turn; Codex reads them for each `!usage`
        unknown = (
            "unknown until the next Claude turn"
            if backend == "claude"
            else "unknown; try !usage again"
        )
        lines.extend(["", "**Account limits** · as of " + stamp(usage.limits_as_of)])
        lines.extend(["", "| Limit | Used | Resets |", "|---|---|---|"])
        for window in usage.windows:
            if window.resets_at is not None and window.resets_at <= now:
                reset = "reset at " + stamp(window.resets_at)
                lines.append(f"| {cell(window.label)} | {unknown} | {reset} |")
                continue
            if window.used_fraction is None or not math.isfinite(window.used_fraction):
                missing.append(f"{window.label}: utilization unavailable")
                continue
            resets = stamp(window.resets_at) if window.resets_at is not None else "unknown"
            lines.append(f"| {cell(window.label)} | {bar(window.used_fraction)} | {resets} |")
    else:
        missing.append("Account limits unavailable")
    lines.extend(["", "**This session** · as of " + stamp(usage.as_of)])
    if (
        usage.context_tokens is not None
        and usage.context_window is not None
        and usage.context_window > 0
    ):
        percent = usage.context_tokens / usage.context_window * 100
        lines.append(
            f"Context: {number(usage.context_tokens)} of {number(usage.context_window)} "
            f"tokens ({math.floor(percent + 0.5)}%)"
        )
    elif usage.context_tokens is not None:
        lines.append(f"Context: {number(usage.context_tokens)} tokens; window size unavailable")
    else:
        missing.append("Context usage unavailable")
    for tokens in usage.models:
        if tokens.model:
            lines.append("\nModel: " + code(tokens.model))
        columns = [("Input", tokens.input_tokens), ("Output", tokens.output_tokens)]
        if backend == "codex":
            columns.insert(1, ("Cached", tokens.cache_read_tokens))
            columns.append(("Reasoning", tokens.reasoning_tokens))
        else:
            columns.extend(
                [
                    ("Cache read", tokens.cache_read_tokens),
                    ("Cache write", tokens.cache_write_tokens),
                ]
            )
            values = (tokens.input_tokens, tokens.cache_read_tokens, tokens.cache_write_tokens)
            if all(v is not None for v in values) and sum(values) > 0:
                lines.append(f"Cache hit: {tokens.cache_read_tokens / sum(values):.0%}")
        known = [(label, value) for label, value in columns if value is not None]
        if len(known) != len(columns):
            missing.append("Some token counters unavailable")
        if known:
            lines.extend(
                [
                    "",
                    "| " + " | ".join(label for label, _ in known) + " |",
                    "| " + " | ".join("---" for _ in known) + " |",
                    "| " + " | ".join(number(value) for _, value in known) + " |",
                ]
            )
    if not usage.models:
        missing.append("Session token totals unavailable")
    if missing:
        lines.append("\n" + "; ".join(cell(reason) for reason in dict.fromkeys(missing)) + ".")
    return "\n".join(lines)


async def usage_reply(actor: SessionActor) -> str:
    """The account and session usage, then the VM line, whose quarter-second CPU sample
    (`vm.USAGE_SAMPLE_S`) runs while the backend reports."""
    text, line = await asyncio.gather(_account_usage(actor), _vm_line(actor))
    return text + "\n\n" + line


async def _vm_line(actor: SessionActor) -> str:
    from zab.core import vm

    try:
        return await vm.summary(("/", actor.row["workspace"]))
    except Exception:
        return "VM: statistics unavailable"


async def _account_usage(actor: SessionActor) -> str:
    hook = getattr(actor.backend, "usage", None)
    if hook is None:
        return "Usage unavailable: this backend has not reported usage yet."
    try:
        async with asyncio.timeout(10):
            usage = await hook(actor.row["native_id"])
        if not isinstance(usage, t.Usage):
            return "Usage unavailable: no data reported yet."
        return render_usage(usage, actor.backend.kind, actor.machine)
    except Exception as exc:
        actor.log_error("Usage read", exc)
        return "Usage unavailable: the backend could not report it; try !usage again."
